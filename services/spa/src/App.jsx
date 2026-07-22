import { useCallback, useEffect, useMemo, useState } from 'react'
import { get, post, put, upload } from './api'
import keycloak from './keycloak'
import { uuid } from './uuid'
import { StartProcess, GenericTaskForm, ProcessFlowDynamic } from './generic'
import { ProcessBuilder } from './builder'
import { AdminPanel } from './admin'
import './App.css'

// Tabs are built per-role inside App() (see navTabs).

const monitorKpis = [
  { key: 'total', label: 'Total' },
  { key: 'running', label: 'Running' },
  { key: 'approved', label: 'Approved' },
  { key: 'rejected', label: 'Rejected' },
]

function normalizeStatus(value) {
  return value ? String(value).trim().toLowerCase() : 'unknown'
}

function statusPillClass(status) {
  const knownStatus = ['running', 'approved', 'rejected'].includes(status)
    ? status
    : 'other'
  return `status-pill status-pill-${knownStatus}`
}

function apiErrorMessage(error) {
  if (!error?.message) return 'Something went wrong.'

  try {
    const body = JSON.parse(error.message)
    return body.detail || error.message
  } catch {
    return error.message
  }
}

function formatDate(value) {
  if (!value) return '—'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString()
}

function formatMoney(value) {
  if (value === null || value === undefined || value === '') return '—'
  const number = Number(value)
  return Number.isNaN(number) ? String(value) : `$${number.toLocaleString()}`
}

function pretty(value) {
  if (typeof value === 'string') return value
  return JSON.stringify(value, null, 2)
}

function Feedback({ feedback }) {
  if (!feedback) return null
  return (
    <p className={`feedback ${feedback.type}`} role={feedback.type === 'error' ? 'alert' : 'status'}>
      {feedback.message}
    </p>
  )
}

function LoadingPanel({ label = 'Loading…' }) {
  return <div className="loading-panel">{label}</div>
}

function InvoiceFields({ data }) {
  if (!data) {
    return <p className="muted">Invoice snapshot is unavailable in the recent transaction list.</p>
  }

  const fields = Object.entries(data)
  if (!fields.length) return <p className="muted">The invoice snapshot is empty.</p>

  return (
    <dl className="field-grid">
      {fields.map(([name, value]) => (
        <div key={name}>
          <dt>{name}</dt>
          <dd>{name === 'amount' ? formatMoney(value) : pretty(value)}</dd>
        </div>
      ))}
    </dl>
  )
}

function TaskInbox({ roles, username }) {
  const [tasks, setTasks] = useState([])
  const [details, setDetails] = useState({})
  const [onlyMyRoles, setOnlyMyRoles] = useState(true)
  // rejectDrafts[token] !== undefined => the required-reason reject form is open
  // for that task; its value is the typed reason text.
  const [rejectDrafts, setRejectDrafts] = useState({})
  const [pdds, setPdds] = useState({})            // process_key -> PDD (for form_schema)
  const [taskDrafts, setTaskDrafts] = useState({}) // token -> {field: value} for generic forms
  const [feedback, setFeedback] = useState({})
  const [busyAction, setBusyAction] = useState('')
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState('')

  const loadTasks = useCallback(async (showLoading = true) => {
    if (showLoading) setLoading(true)
    setLoadError('')
    if (showLoading) setDetails({})

    try {
      // Load the three sources INDEPENDENTLY (allSettled) so one failing query —
      // e.g. a malformed finance task making /v1/tasks return an error — can NEVER
      // blank the whole inbox. We render whatever succeeded and surface a scoped
      // warning for whatever failed.
      const [openResult, claimedResult, txResult] = await Promise.allSettled([
        get('/v1/tasks?status=open'),
        get('/v1/tasks?status=claimed'),
        get('/v1/transactions'),
      ])

      const openTaskRows = openResult.status === 'fulfilled' ? openResult.value : []
      const claimedTaskRows = claimedResult.status === 'fulfilled' ? claimedResult.value : []
      const transactionRows = txResult.status === 'fulfilled' ? txResult.value : []

      const failures = [openResult, claimedResult, txResult]
        .filter((result) => result.status === 'rejected')
        .map((result) => apiErrorMessage(result.reason))
      setLoadError(failures.length ? `Some data could not be loaded: ${failures[0]}` : '')

      const taskRows = [...openTaskRows, ...claimedTaskRows]
      setTasks(taskRows)

      const transactionById = Object.fromEntries(
        transactionRows.map((transaction) => [transaction.id, transaction]),
      )
      const transactionIds = [...new Set(taskRows.map((task) => task.transaction_id))]
      const detailEntries = await Promise.all(
        transactionIds.map(async (transactionId) => {
          const processKey = transactionById[transactionId]?.process_key ?? null
          try {
            const history = await get(`/v1/transactions/${encodeURIComponent(transactionId)}/history`)
            return [
              transactionId,
              {
                invoice: transactionById[transactionId]?.data_snapshot ?? null,
                history,
                error: '',
                process_key: processKey,
              },
            ]
          } catch (error) {
            return [
              transactionId,
              {
                invoice: transactionById[transactionId]?.data_snapshot ?? null,
                history: [],
                error: apiErrorMessage(error),
                process_key: processKey,
              },
            ]
          }
        }),
      )
      setDetails(Object.fromEntries(detailEntries))

      // Fetch each process's PDD once so task action forms can render from the
      // node's form_schema (falls back to the classic approve/reject UI).
      const processKeys = [...new Set(transactionRows.map((t) => t.process_key).filter(Boolean))]
      const pddEntries = await Promise.all(
        processKeys.map(async (pk) => {
          try {
            return [pk, await get(`/v1/definitions/${encodeURIComponent(pk)}`)]
          } catch {
            return [pk, null]
          }
        }),
      )
      setPdds(Object.fromEntries(pddEntries))
    } catch (error) {
      setLoadError(apiErrorMessage(error))
    } finally {
      if (showLoading) setLoading(false)
    }
  }, [])

  useEffect(() => {
    loadTasks()
    const timer = window.setInterval(() => loadTasks(false), 5000)
    return () => window.clearInterval(timer)
  }, [loadTasks])

  const visibleTasks = onlyMyRoles
    ? tasks.filter((task) => roles.includes(task.assigned_role))
    : tasks

  async function claimTask(task) {
    setBusyAction(`${task.token}:claim`)
    setFeedback((current) => ({ ...current, [task.token]: null }))

    try {
      const result = await post(`/v1/tasks/${encodeURIComponent(task.token)}/claim`, {
        claimed_by: username,
      })
      if (task.node_id === 'finance') {
        await loadTasks(false)
      } else {
        setTasks((current) =>
          current.map((item) =>
            item.token === task.token
              ? { ...item, status: 'claimed', claimed_by: username }
              : item,
          ),
        )
      }
      setFeedback((current) => ({
        ...current,
        [task.token]: {
          type: 'success',
          message:
            task.node_id === 'finance'
              ? `Finance slot ${result.participant_id} claimed. You can now submit one decision.`
              : 'Task claimed. You can now submit a decision.',
        },
      }))
    } catch (error) {
      setFeedback((current) => ({
        ...current,
        [task.token]: { type: 'error', message: apiErrorMessage(error) },
      }))
    } finally {
      setBusyAction('')
    }
  }

  async function completeTask(task, decisionArg, reason) {
    // decisionArg comes from the Approve/Reject buttons; `reason` is REQUIRED by
    // the UI for rejections and travels in the payload so it lands in the audit
    // event (HUMAN_DECISION / FINANCE_VOTE) and the vendor rejection email.
    const decision = decisionArg || 'approve'
    const isFinanceTask = task.node_id === 'finance'
    setBusyAction(`${task.token}:complete`)
    setFeedback((current) => ({ ...current, [task.token]: null }))

    try {
      const result = await post(`/v1/tasks/${encodeURIComponent(task.token)}/complete`, {
        idempotency_key: uuid(),
        payload: { decision, ...(reason ? { reason } : {}) },
        kind: isFinanceTask ? 'finance' : 'human',
      })
      setRejectDrafts((current) => {
        const next = { ...current }
        delete next[task.token]
        return next
      })
      if (isFinanceTask) {
        await loadTasks(false)
        setFeedback((current) => ({
          ...current,
          [task.token]: {
            type: 'success',
            message:
              result.finance_status === 'pending'
                ? 'Decision submitted. The Finance task remains open until the quorum is decided.'
                : `Finance quorum ${result.finance_status}.`,
          },
        }))
      } else {
        setTasks((current) => current.filter((item) => item.token !== task.token))
      }
    } catch (error) {
      setFeedback((current) => ({
        ...current,
        [task.token]: { type: 'error', message: apiErrorMessage(error) },
      }))
    } finally {
      setBusyAction('')
    }
  }

  // Resolve a task's action form from the process PDD's node.form_schema. Returns
  // null when unavailable -> the classic approve/reject UI is used as a fallback.
  function formSchemaFor(task) {
    const processKey = details[task.transaction_id]?.process_key
    const pdd = processKey ? pdds[processKey] : null
    if (!pdd || !Array.isArray(pdd.nodes)) return null
    const node = pdd.nodes.find((n) => n.id === task.node_id)
    const fields = node?.form_schema?.fields
    return Array.isArray(fields) && fields.length ? fields : null
  }

  async function submitGenericTask(task, fields) {
    const values = taskDrafts[task.token] || {}
    const missing = fields.filter((f) => f.required && !values[f.key])
    if (missing.length) {
      setFeedback((current) => ({
        ...current,
        [task.token]: { type: 'error', message: `Please fill: ${missing.map((f) => f.key).join(', ')}` },
      }))
      return
    }
    setBusyAction(`${task.token}:complete`)
    setFeedback((current) => ({ ...current, [task.token]: null }))
    try {
      await post(`/v1/tasks/${encodeURIComponent(task.token)}/complete`, {
        idempotency_key: uuid(),
        payload: { ...values },
        kind: 'human',
      })
      setTasks((current) => current.filter((item) => item.token !== task.token))
      setFeedback((current) => ({
        ...current,
        [task.token]: { type: 'success', message: 'Decision submitted.' },
      }))
    } catch (error) {
      setFeedback((current) => ({
        ...current,
        [task.token]: { type: 'error', message: apiErrorMessage(error) },
      }))
    } finally {
      setBusyAction('')
    }
  }

  return (
    <section className="view" aria-labelledby="task-inbox-heading">
      <div className="view-heading">
        <div>
          <p className="eyebrow">Human work queue</p>
          <h2 id="task-inbox-heading">Task Inbox</h2>
          <p>Claim work assigned to your realm roles, then submit a decision.</p>
        </div>
        <button className="secondary-button" type="button" onClick={() => loadTasks()} disabled={loading}>
          Refresh
        </button>
      </div>

      <div className="toolbar">
        <label className="toggle">
          <input
            type="checkbox"
            checked={onlyMyRoles}
            onChange={(event) => setOnlyMyRoles(event.target.checked)}
          />
          <span>Only my roles</span>
        </label>
        <span className="muted">
          Showing {visibleTasks.length} of {tasks.length} active tasks
        </span>
      </div>

      {loading && <LoadingPanel label="Loading active tasks and invoice history…" />}
      {!loading && loadError && <Feedback feedback={{ type: 'error', message: loadError }} />}
      {!loading && !loadError && visibleTasks.length === 0 && (
        <div className="empty-state">
          <h3>No matching active tasks</h3>
          <p>{onlyMyRoles ? 'Turn off “Only my roles” to see the full queue.' : 'The queue is clear.'}</p>
        </div>
      )}

      <div className="task-list">
        {visibleTasks.map((task) => {
          const taskDetails = details[task.transaction_id]
          const llmEvents = taskDetails?.history?.filter((event) => event.type === 'LLM_DECISION') || []
          const latestLlmDecision = llmEvents.at(-1)
          const aiSummary =
            latestLlmDecision?.payload?.rationale ||
            latestLlmDecision?.detail ||
            latestLlmDecision?.payload?.detail
          const isFinanceTask = task.node_id === 'finance'
          const isClaimedByUser = task.status === 'claimed' && task.claimed_by === username
          // Generic form fields from the PDD (non-finance only; finance keeps the quorum widget).
          const taskFields = isFinanceTask ? null : formSchemaFor(task)
          const canSubmitFinanceDecision =
            task.current_user_claimed && !task.current_user_decision && task.can_decide
          // Did GET /v1/tasks actually enrich this finance task with quorum fields?
          // A stale/old API omits them; without this guard the card shows blank
          // counts and a misleading "you need the role" message.
          const financeReady = typeof task.required_approvals === 'number'
          // Client-side role signal from the Keycloak token, so a user who HAS the
          // task's role is never told they lack it (independent of backend fields).
          const userHasFinanceRole = roles.includes(task.assigned_role)

          return (
            <article className="task-card" key={task.token}>
              <div className="task-card-header">
                <div>
                  <span className="status-pill">{task.status}</span>
                  <h3>{task.node_id}</h3>
                </div>
                <span className="role-badge">{task.assigned_role || 'No role'}</span>
              </div>

              <dl className="record-grid">
                <div>
                  <dt>Transaction</dt>
                  <dd className="mono">{task.transaction_id}</dd>
                </div>
                <div>
                  <dt>Node</dt>
                  <dd>{task.node_id}</dd>
                </div>
                <div>
                  <dt>Assigned role</dt>
                  <dd>{task.assigned_role || '—'}</dd>
                </div>
                <div>
                  <dt>Claimed by</dt>
                  <dd>{task.claimed_by || 'Unclaimed'}</dd>
                </div>
              </dl>

              <div className="task-detail-grid">
                <div>
                  <h4>Invoice</h4>
                  {!taskDetails && <p className="muted">Loading invoice snapshot…</p>}
                  {taskDetails && <InvoiceFields data={taskDetails.invoice} />}
                </div>
                <div className="ai-summary">
                  <h4>AI review summary</h4>
                  {!taskDetails && <p className="muted">Loading review history…</p>}
                  {taskDetails?.error && <Feedback feedback={{ type: 'error', message: taskDetails.error }} />}
                  {taskDetails && !taskDetails.error && (
                    <p>{aiSummary || 'No LLM_DECISION rationale has been recorded yet.'}</p>
                  )}
                </div>
              </div>

              {isFinanceTask ? (
                <div className="task-actions finance-vote-actions">
                  <div className="finance-vote-progress">
                    <span className="voting-badge">Finance quorum</span>
                    <div className="finance-quorum-metrics">
                      <strong>
                        Approvals: {task.approval_count ?? '—'} of {task.required_approvals ?? '—'}
                      </strong>
                      <span>
                        Slots claimed: {task.claimed_count ?? '—'} of {task.participant_capacity ?? '—'}
                      </span>
                      {task.rejection_count > 0 && (
                        <span>Rejections: {task.rejection_count}</span>
                      )}
                    </div>
                  </div>

                  {task.current_user_decision ? (
                    <p className="finance-voted">
                      You have voted: <strong>{task.current_user_decision}</strong>. The task stays
                      open until the quorum is decided.
                    </p>
                  ) : task.current_user_claimed ? (
                    rejectDrafts[task.token] === undefined ? (
                      <div className="finance-vote-buttons">
                        <button
                          className="primary-button"
                          type="button"
                          onClick={() => completeTask(task, 'approve')}
                          disabled={!canSubmitFinanceDecision || busyAction === `${task.token}:complete`}
                        >
                          {busyAction === `${task.token}:complete` ? 'Submitting…' : 'Approve'}
                        </button>
                        <button
                          className="danger-button"
                          type="button"
                          onClick={() =>
                            setRejectDrafts((current) => ({ ...current, [task.token]: '' }))
                          }
                          disabled={!canSubmitFinanceDecision || busyAction === `${task.token}:complete`}
                        >
                          Reject
                        </button>
                      </div>
                    ) : (
                      <form
                        className="reject-reason-form"
                        onSubmit={(event) => {
                          event.preventDefault()
                          completeTask(task, 'reject', rejectDrafts[task.token].trim())
                        }}
                      >
                        <label>
                          Rejection reason
                          <input
                            type="text"
                            required
                            placeholder="Why is this invoice rejected?"
                            value={rejectDrafts[task.token]}
                            onChange={(event) =>
                              setRejectDrafts((current) => ({
                                ...current,
                                [task.token]: event.target.value,
                              }))
                            }
                          />
                        </label>
                        <button
                          className="danger-button"
                          type="submit"
                          disabled={
                            !rejectDrafts[task.token].trim() ||
                            busyAction === `${task.token}:complete`
                          }
                        >
                          {busyAction === `${task.token}:complete` ? 'Submitting…' : 'Confirm reject'}
                        </button>
                        <button
                          className="secondary-button"
                          type="button"
                          onClick={() =>
                            setRejectDrafts((current) => {
                              const next = { ...current }
                              delete next[task.token]
                              return next
                            })
                          }
                        >
                          Cancel
                        </button>
                      </form>
                    )
                  ) : task.can_claim ? (
                    <button
                      className="secondary-button finance-claim-button"
                      type="button"
                      onClick={() => claimTask(task)}
                      disabled={busyAction === `${task.token}:claim`}
                    >
                      {busyAction === `${task.token}:claim` ? 'Claiming…' : 'Claim'}
                    </button>
                  ) : !financeReady ? (
                    <p className="finance-vote-status">
                      Finance quorum details are missing from the API response — the API needs a
                      restart to enable voting.
                    </p>
                  ) : !userHasFinanceRole ? (
                    <p className="finance-vote-status">You need the {task.assigned_role} role to vote.</p>
                  ) : task.available_slots === 0 ? (
                    <p className="finance-vote-status">All finance slots are claimed.</p>
                  ) : (
                    <p className="finance-vote-status">No finance slot is available to claim right now.</p>
                  )}
                </div>
              ) : (
                <div className="task-actions">
                  <button
                    className="secondary-button"
                    type="button"
                    onClick={() => claimTask(task)}
                    disabled={task.status !== 'open' || busyAction === `${task.token}:claim`}
                  >
                    {busyAction === `${task.token}:claim` ? 'Claiming…' : 'Claim'}
                  </button>

                  {/* After claiming: exactly Approve + Reject. Reject requires a
                      typed reason (recorded in the audit + vendor email). The
                      workflow's "return" branch still exists in the backend; it is
                      intentionally not exposed in this UI. */}
                  {isClaimedByUser && taskFields && (
                    <GenericTaskForm
                      fields={taskFields}
                      values={taskDrafts[task.token] || {}}
                      onChange={(key, value) =>
                        setTaskDrafts((current) => ({
                          ...current,
                          [task.token]: { ...(current[task.token] || {}), [key]: value },
                        }))
                      }
                      onSubmit={() => submitGenericTask(task, taskFields)}
                      busy={busyAction === `${task.token}:complete`}
                    />
                  )}
                  {isClaimedByUser && !taskFields &&
                    (rejectDrafts[task.token] === undefined ? (
                      <div className="finance-vote-buttons">
                        <button
                          className="primary-button"
                          type="button"
                          onClick={() => completeTask(task, 'approve')}
                          disabled={busyAction === `${task.token}:complete`}
                        >
                          {busyAction === `${task.token}:complete` ? 'Submitting…' : 'Approve'}
                        </button>
                        <button
                          className="danger-button"
                          type="button"
                          onClick={() =>
                            setRejectDrafts((current) => ({ ...current, [task.token]: '' }))
                          }
                          disabled={busyAction === `${task.token}:complete`}
                        >
                          Reject
                        </button>
                      </div>
                    ) : (
                      <form
                        className="reject-reason-form"
                        onSubmit={(event) => {
                          event.preventDefault()
                          completeTask(task, 'reject', rejectDrafts[task.token].trim())
                        }}
                      >
                        <label>
                          Rejection reason
                          <input
                            type="text"
                            required
                            placeholder="Why is this invoice rejected?"
                            value={rejectDrafts[task.token]}
                            onChange={(event) =>
                              setRejectDrafts((current) => ({
                                ...current,
                                [task.token]: event.target.value,
                              }))
                            }
                          />
                        </label>
                        <button
                          className="danger-button"
                          type="submit"
                          disabled={
                            !rejectDrafts[task.token].trim() ||
                            busyAction === `${task.token}:complete`
                          }
                        >
                          {busyAction === `${task.token}:complete` ? 'Submitting…' : 'Confirm reject'}
                        </button>
                        <button
                          className="secondary-button"
                          type="button"
                          onClick={() =>
                            setRejectDrafts((current) => {
                              const next = { ...current }
                              delete next[task.token]
                              return next
                            })
                          }
                        >
                          Cancel
                        </button>
                      </form>
                    ))}
                </div>
              )}

              {!isFinanceTask && !isClaimedByUser && task.status === 'open' && (
                <p className="action-hint">Claim this task to enable the completion form.</p>
              )}
              <Feedback feedback={feedback[task.token]} />
              <p className={`email-note ${isFinanceTask ? 'finance-note' : ''}`}>
                {isFinanceTask
                  ? 'Finance approval needs a quorum: claim a slot, then vote approve or reject. The task stays open until the quorum is reached.'
                  : 'You can also act on this task by replying to the notification email with approve, reject, or return.'}
              </p>
            </article>
          )
        })}
      </div>
    </section>
  )
}

const PAGE_SIZE = 10

function Monitor() {
  const [transactions, setTransactions] = useState([])
  const [stats, setStats] = useState(null)
  // KPI filter ('total' = all) + 1-based page within that filter.
  const [filter, setFilter] = useState('total')
  const [page, setPage] = useState(1)
  const [selected, setSelected] = useState(null)
  const [history, setHistory] = useState([])
  const [loading, setLoading] = useState(true)
  const [historyLoading, setHistoryLoading] = useState(false)
  const [error, setError] = useState('')
  const [historyError, setHistoryError] = useState('')

  useEffect(() => {
    let active = true
    let polling = false

    async function pollTransactions() {
      if (polling) return
      polling = true

      try {
        const statusParam = filter === 'total' ? '' : `&status=${encodeURIComponent(filter)}`
        const [rows, counts] = await Promise.all([
          get(`/v1/transactions?limit=${PAGE_SIZE}&offset=${(page - 1) * PAGE_SIZE}${statusParam}`),
          get('/v1/transactions/stats'),
        ])
        if (active) {
          setTransactions(rows)
          setStats(counts)
          setSelected((current) => (
            current ? rows.find((transaction) => transaction.id === current.id) || current : null
          ))
          setError('')
        }
      } catch (pollError) {
        if (active) setError(apiErrorMessage(pollError))
      } finally {
        polling = false
        if (active) setLoading(false)
      }
    }

    pollTransactions()
    const timer = window.setInterval(pollTransactions, 5000)
    return () => {
      active = false
      window.clearInterval(timer)
    }
  }, [filter, page])

  const displayTransactions = useMemo(
    () => transactions.map((transaction) => ({
      ...transaction,
      status: normalizeStatus(transaction.status),
    })),
    [transactions],
  )

  // Counts come from /v1/transactions/stats: they reflect ALL transactions,
  // not just the visible page.
  const monitorCounts = stats || { total: 0, running: 0, approved: 0, rejected: 0 }
  const totalForFilter = monitorCounts[filter] ?? 0
  const totalPages = Math.max(1, Math.ceil(totalForFilter / PAGE_SIZE))

  function selectKpi(key) {
    setFilter(key)
    setPage(1)
    setSelected(null)
  }

  async function selectTransaction(transaction) {
    setSelected(transaction)
    setHistory([])
    setHistoryError('')
    setHistoryLoading(true)

    try {
      // AI-narrated audit: one clean sentence per event (the backend falls back
      // to deterministic text when the LLM is slow/unavailable — never raw JSON).
      const events = await get(
        `/v1/transactions/${encodeURIComponent(transaction.id)}/history?format=narrative`,
      )
      setHistory(events)
    } catch (requestError) {
      setHistoryError(apiErrorMessage(requestError))
    } finally {
      setHistoryLoading(false)
    }
  }

  return (
    <section className="view" aria-labelledby="monitor-heading">
      <div className="view-heading">
        <div>
          <p className="eyebrow">Live operations</p>
          <h2 id="monitor-heading">Monitor</h2>
          <p>Recent workflow runs refresh automatically every five seconds.</p>
        </div>
        <span className="live-indicator"><span /> Live</span>
      </div>

      {error && <Feedback feedback={{ type: 'error', message: error }} />}
      {loading ? (
        <LoadingPanel label="Loading recent transactions…" />
      ) : (
        <>
          <div className="kpi-grid">
            {monitorKpis.map((kpi) => (
              <button
                className={`kpi-card kpi-card-${kpi.key} ${filter === kpi.key ? 'kpi-selected' : ''}`}
                type="button"
                key={kpi.key}
                onClick={() => selectKpi(kpi.key)}
                aria-pressed={filter === kpi.key}
              >
                <span>{kpi.label}</span>
                <strong>{monitorCounts[kpi.key]}</strong>
              </button>
            ))}
          </div>

          <div className="table-panel">
            <div className="table-scroll">
              <table>
                <thead>
                  <tr>
                    <th>ID</th>
                    <th>Status</th>
                    <th>Created</th>
                    <th>Closed</th>
                    <th>Vendor</th>
                    <th>Amount</th>
                  </tr>
                </thead>
                <tbody>
                  {displayTransactions.map((transaction) => (
                    <tr
                      className={selected?.id === transaction.id ? 'selected-row' : ''}
                      key={transaction.id}
                      onClick={() => selectTransaction(transaction)}
                    >
                      <td className="mono">{transaction.id}</td>
                      <td><span className={statusPillClass(transaction.status)}>{transaction.status}</span></td>
                      <td>{formatDate(transaction.created_at)}</td>
                      <td>{formatDate(transaction.closed_at)}</td>
                      <td>{transaction.data_snapshot?.vendor || '—'}</td>
                      <td>{formatMoney(transaction.data_snapshot?.amount)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {displayTransactions.length === 0 && <p className="table-empty">No transactions have been recorded.</p>}
            </div>
            <div className="pagination">
              <button
                className="secondary-button"
                type="button"
                onClick={() => setPage((current) => Math.max(1, current - 1))}
                disabled={page <= 1}
              >
                Previous
              </button>
              <span className="muted">Page {page} of {totalPages}</span>
              <button
                className="secondary-button"
                type="button"
                onClick={() => setPage((current) => Math.min(totalPages, current + 1))}
                disabled={page >= totalPages}
              >
                Next
              </button>
            </div>
          </div>
        </>
      )}

      {selected && (
        <section className="audit-panel" aria-labelledby="audit-heading">
          <div className="audit-heading">
            <div>
              <p className="eyebrow">Transaction drill-down</p>
              <h3 id="audit-heading">Audit trail</h3>
              <p className="mono">{selected.id}</p>
            </div>
            <button className="icon-button" type="button" onClick={() => setSelected(null)} aria-label="Close audit trail">
              ×
            </button>
          </div>

          <dl className="monitor-detail-grid">
            <div>
              <dt>Status</dt>
              <dd>
                <span className={statusPillClass(normalizeStatus(selected.status))}>
                  {normalizeStatus(selected.status)}
                </span>
              </dd>
            </div>
            <div>
              <dt>Process</dt>
              <dd>{selected.process_key || '—'}</dd>
            </div>
            <div>
              <dt>Created</dt>
              <dd>{formatDate(selected.created_at)}</dd>
            </div>
            <div>
              <dt>Closed</dt>
              <dd>{formatDate(selected.closed_at)}</dd>
            </div>
          </dl>

          {historyLoading && <LoadingPanel label="Loading ordered event history…" />}
          {historyError && <Feedback feedback={{ type: 'error', message: historyError }} />}
          {!historyLoading && !historyError && history.length === 0 && (
            <p className="muted">No audit events have been recorded for this transaction.</p>
          )}
          <ol className="timeline">
            {history.map((event, index) => (
              <li key={`${event.occurred_at}-${event.type}-${index}`}>
                <span className="timeline-dot" />
                <div className="timeline-card">
                  <p className="narrative-line">
                    <time>{formatDate(event.occurred_at)}</time>
                    {' — '}
                    {event.text}
                  </p>
                </div>
              </li>
            ))}
          </ol>
        </section>
      )}
    </section>
  )
}

function App() {
  const username = keycloak.tokenParsed?.preferred_username || 'Unknown user'
  const roles = keycloak.tokenParsed?.realm_access?.roles || []
  // Generic, role-based tabs — no process-specific role names. Anyone can hold
  // tasks and start a request; authors get the Builder; admins get Monitor + Admin.
  const isAuthor = roles.includes('process_author')
  const isOps = roles.includes('ops_admin')
  const navTabs = []
  navTabs.push({ id: 'tasks', label: 'Task Inbox' })
  navTabs.push({ id: 'start', label: 'Start Process' })
  navTabs.push({ id: 'flow', label: 'Process Flow' })
  if (isAuthor) navTabs.push({ id: 'builder', label: 'Builder' })
  if (isOps) navTabs.push({ id: 'monitor', label: 'Monitor' })
  if (isOps) navTabs.push({ id: 'admin', label: 'Admin' })
  const [activeTab, setActiveTab] = useState(navTabs[0].id)

  return (
    <div className="app-shell">
      <header className="app-header">
        <div className="brand">
          <span className="brand-mark" aria-hidden="true">W</span>
          <div>
            <h1>Workflow Engine</h1>
            <p>Configurable workflows</p>
          </div>
        </div>
        <div className="user-panel">
          <div>
            <strong>{username}</strong>
            <div className="role-list" aria-label="User roles">
              {roles.length > 0 ? roles.map((role) => <span key={role}>{role}</span>) : <span>No realm roles</span>}
            </div>
          </div>
          <button
            className="logout-button"
            type="button"
            onClick={() => keycloak.logout({ redirectUri: window.location.origin })}
          >
            Logout
          </button>
        </div>
      </header>

      <nav className="tab-nav" aria-label="Workflow views">
        {navTabs.map((tab) => (
          <button
            className={activeTab === tab.id ? 'active' : ''}
            type="button"
            key={tab.id}
            onClick={() => setActiveTab(tab.id)}
            aria-current={activeTab === tab.id ? 'page' : undefined}
          >
            {tab.label}
          </button>
        ))}
      </nav>

      <main>
        {activeTab === 'tasks' && <TaskInbox roles={roles} username={username} />}
        {activeTab === 'start' && <StartProcess />}
        {activeTab === 'flow' && <ProcessFlowDynamic />}
        {activeTab === 'builder' && <ProcessBuilder />}
        {activeTab === 'monitor' && <Monitor />}
        {activeTab === 'admin' && <AdminPanel />}
      </main>
    </div>
  )
}

export default App
