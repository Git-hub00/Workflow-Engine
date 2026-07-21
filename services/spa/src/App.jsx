import { useCallback, useEffect, useMemo, useState } from 'react'
import { get, post, put, upload } from './api'
import keycloak from './keycloak'
import { uuid } from './uuid'
import { StartProcess, GenericTaskForm } from './generic'
import './App.css'

const PROCESS_KEY = 'invoice_approval'

const tabs = [
  { id: 'tasks', label: 'Task Inbox' },
  { id: 'monitor', label: 'Monitor' },
  { id: 'configuration', label: 'Configuration' },
  { id: 'flow', label: 'Process Flow' },
  { id: 'start', label: 'Start Process' },
]

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

function TagEditor({ id, label, values, onChange, placeholder }) {
  const [draft, setDraft] = useState('')

  function addValue() {
    const value = draft.trim()
    if (!value || values.includes(value)) return
    onChange([...values, value])
    setDraft('')
  }

  function removeValue(valueToRemove) {
    onChange(values.filter((value) => value !== valueToRemove))
  }

  return (
    <fieldset className="config-list-field">
      <legend>{label}</legend>
      <div className="config-chip-list">
        {values.map((value) => (
          <span className="config-chip" key={value}>
            {value}
            <button
              type="button"
              onClick={() => removeValue(value)}
              aria-label={`Remove ${value}`}
            >
              ×
            </button>
          </span>
        ))}
        {values.length === 0 && <span className="config-list-empty">No items added</span>}
      </div>
      <div className="config-list-input">
        <input
          id={id}
          type="text"
          value={draft}
          placeholder={placeholder}
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === 'Enter') {
              event.preventDefault()
              addValue()
            }
          }}
        />
        <button
          className="secondary-button"
          type="button"
          onClick={addValue}
          disabled={!draft.trim() || values.includes(draft.trim())}
        >
          Add
        </button>
      </div>
    </fieldset>
  )
}

function ReadOnlyChips({ label, values }) {
  return (
    <fieldset className="config-list-field">
      <legend>{label}</legend>
      <div className="config-chip-list">
        {values.map((value) => (
          <span className="config-chip read-only" key={value}>
            {value}
          </span>
        ))}
        {values.length === 0 && <span className="config-list-empty">No items</span>}
      </div>
    </fieldset>
  )
}

// Read-only mirror of the Configuration form for users WITHOUT process_author.
// Shows the same values (thresholds, quorum, reject toggle, field/vendor lists)
// as non-editable display — no inputs, no chip add/remove, no Save button.
function ReadOnlyConfig({ form }) {
  return (
    <div className="config-form read-only-config">
      <section className="config-section">
        <div className="config-section-heading">
          <div>
            <h3>Routing thresholds</h3>
            <p>The amount boundaries and response window applied to new invoices.</p>
          </div>
        </div>
        <dl className="config-readonly-grid">
          <div>
            <dt>Auto-approve under ($)</dt>
            <dd>{formatMoney(form.autoApproveUnder)}</dd>
          </div>
          <div>
            <dt>Finance threshold ($)</dt>
            <dd>{formatMoney(form.financeThreshold)}</dd>
          </div>
          <div>
            <dt>SLA (hours)</dt>
            <dd>{form.slaHours === '' ? '—' : form.slaHours}</dd>
          </div>
        </dl>
      </section>

      <section className="config-section">
        <div className="config-section-heading">
          <div>
            <h3>Approval policy</h3>
            <p>Finance voting and rejection behavior.</p>
          </div>
        </div>
        <dl className="config-readonly-grid">
          <div>
            <dt>Quorum (N of M)</dt>
            <dd>{`${form.quorum.n === '' ? '—' : form.quorum.n} of ${form.quorum.of === '' ? '—' : form.quorum.of}`}</dd>
          </div>
          <div>
            <dt>Reject short-circuits quorum</dt>
            <dd>{form.rejectShortCircuits ? 'On' : 'Off'}</dd>
          </div>
        </dl>
      </section>

      <section className="config-section">
        <div className="config-section-heading">
          <div>
            <h3>Invoice data</h3>
            <p>Required invoice fields and vendors eligible for rule-based routing.</p>
          </div>
        </div>
        <div className="config-list-grid">
          <ReadOnlyChips label="Required fields" values={form.requiredFields} />
          <ReadOnlyChips label="Approved vendors" values={form.approvedVendors} />
        </div>
      </section>
    </div>
  )
}

function Configuration({ canEdit }) {
  const [form, setForm] = useState(null)
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [feedback, setFeedback] = useState(null)

  const loadConfig = useCallback(async () => {
    setLoading(true)
    setFeedback(null)
    try {
      const config = await get(`/v1/config/${PROCESS_KEY}`)
      setForm({
        autoApproveUnder: config.autoApproveUnder ?? '',
        financeThreshold: config.financeThreshold ?? '',
        requiredFields: Array.isArray(config.requiredFields) ? [...config.requiredFields] : [],
        quorum: {
          n: config.quorum?.n ?? '',
          of: config.quorum?.of ?? '',
        },
        rejectShortCircuits: Boolean(config.rejectShortCircuits),
        slaHours: config.slaHours ?? '',
        approvedVendors: Array.isArray(config.approvedVendors) ? [...config.approvedVendors] : [],
      })
    } catch (error) {
      setFeedback({ type: 'error', message: apiErrorMessage(error) })
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    loadConfig()
  }, [loadConfig])

  function updateField(field, value) {
    setForm((current) => ({ ...current, [field]: value }))
  }

  function updateQuorum(field, value) {
    setForm((current) => ({
      ...current,
      quorum: { ...current.quorum, [field]: value },
    }))
  }

  async function saveConfig(event) {
    event.preventDefault()
    setFeedback(null)

    const numericValues = [
      form.autoApproveUnder,
      form.financeThreshold,
      form.quorum.n,
      form.quorum.of,
      form.slaHours,
    ]
    if (numericValues.some((value) => value === '' || !Number.isFinite(Number(value)))) {
      setFeedback({ type: 'error', message: 'Enter a valid number in every numeric field.' })
      return
    }

    const config = {
      autoApproveUnder: Number(form.autoApproveUnder),
      financeThreshold: Number(form.financeThreshold),
      requiredFields: [...form.requiredFields],
      quorum: {
        n: Number(form.quorum.n),
        of: Number(form.quorum.of),
      },
      rejectShortCircuits: form.rejectShortCircuits,
      slaHours: Number(form.slaHours),
      approvedVendors: [...form.approvedVendors],
    }

    if (config.quorum.n > config.quorum.of) {
      setFeedback({ type: 'error', message: 'Quorum N cannot be greater than M.' })
      return
    }

    setSaving(true)
    try {
      await put(`/v1/config/${PROCESS_KEY}`, { config })
      setForm(config)
      setFeedback({ type: 'success', message: 'Configuration updated successfully.' })
    } catch (error) {
      setFeedback({ type: 'error', message: apiErrorMessage(error) })
    } finally {
      setSaving(false)
    }
  }

  return (
    <section className="view" aria-labelledby="configuration-heading">
      <div className="view-heading">
        <div>
          <p className="eyebrow">Published process rules</p>
          <h2 id="configuration-heading">Configuration</h2>
          <p>Edit the live config block for <span className="mono">{PROCESS_KEY}</span>.</p>
        </div>
        <button className="secondary-button" type="button" onClick={loadConfig} disabled={loading || saving}>
          Reload
        </button>
      </div>

      {canEdit ? (
        <div className="config-note">
          Changes apply to new transactions. In-flight workflows keep the configuration they started with.
        </div>
      ) : (
        <div className="config-note info">
          You have read-only access. Only process_author can edit configuration.
        </div>
      )}

      {loading ? (
        <LoadingPanel label="Loading live configuration…" />
      ) : !form ? (
        <Feedback feedback={feedback} />
      ) : canEdit ? (
        <form className="config-form" onSubmit={saveConfig}>
          <section className="config-section" aria-labelledby="routing-rules-heading">
            <div className="config-section-heading">
              <div>
                <h3 id="routing-rules-heading">Routing thresholds</h3>
                <p>Set the amount boundaries and response window for new invoices.</p>
              </div>
            </div>
            <div className="config-field-grid three-columns">
              <label className="config-field">
                <span>Auto-approve under ($)</span>
                <input
                  type="number"
                  min="0"
                  step="any"
                  required
                  value={form.autoApproveUnder}
                  onChange={(event) => updateField('autoApproveUnder', event.target.value)}
                />
              </label>
              <label className="config-field">
                <span>Finance threshold ($)</span>
                <input
                  type="number"
                  min="0"
                  step="any"
                  required
                  value={form.financeThreshold}
                  onChange={(event) => updateField('financeThreshold', event.target.value)}
                />
              </label>
              <label className="config-field">
                <span>SLA (hours)</span>
                <input
                  type="number"
                  min="1"
                  step="1"
                  required
                  value={form.slaHours}
                  onChange={(event) => updateField('slaHours', event.target.value)}
                />
              </label>
            </div>
          </section>

          <section className="config-section" aria-labelledby="approval-policy-heading">
            <div className="config-section-heading">
              <div>
                <h3 id="approval-policy-heading">Approval policy</h3>
                <p>Control finance voting and rejection behavior.</p>
              </div>
            </div>
            <div className="config-policy-grid">
              <fieldset className="quorum-fieldset">
                <legend>Quorum: N of M</legend>
                <div>
                  <label className="config-field">
                    <span>N</span>
                    <input
                      type="number"
                      min="1"
                      step="1"
                      required
                      value={form.quorum.n}
                      onChange={(event) => updateQuorum('n', event.target.value)}
                    />
                  </label>
                  <span className="quorum-separator">of</span>
                  <label className="config-field">
                    <span>M</span>
                    <input
                      type="number"
                      min="1"
                      step="1"
                      required
                      value={form.quorum.of}
                      onChange={(event) => updateQuorum('of', event.target.value)}
                    />
                  </label>
                </div>
              </fieldset>

              <label className="config-switch">
                <input
                  type="checkbox"
                  checked={form.rejectShortCircuits}
                  onChange={(event) => updateField('rejectShortCircuits', event.target.checked)}
                />
                <span className="switch-control" aria-hidden="true" />
                <span className="switch-copy">
                  <strong>Reject short-circuits quorum</strong>
                  <small>End finance review as soon as a rejection is received.</small>
                </span>
              </label>
            </div>
          </section>

          <section className="config-section" aria-labelledby="invoice-data-heading">
            <div className="config-section-heading">
              <div>
                <h3 id="invoice-data-heading">Invoice data</h3>
                <p>Maintain the required invoice fields and vendors eligible for rule-based routing.</p>
              </div>
            </div>
            <div className="config-list-grid">
              <TagEditor
                id="required-field-input"
                label="Required fields"
                values={form.requiredFields}
                onChange={(values) => updateField('requiredFields', values)}
                placeholder="e.g. poNumber"
              />
              <TagEditor
                id="approved-vendor-input"
                label="Approved vendors"
                values={form.approvedVendors}
                onChange={(values) => updateField('approvedVendors', values)}
                placeholder="e.g. Acme Supplies"
              />
            </div>
          </section>

          <div className="form-footer">
            <Feedback feedback={feedback} />
            <button className="primary-button" type="submit" disabled={saving}>
              {saving ? 'Saving…' : 'Save configuration'}
            </button>
          </div>
        </form>
      ) : (
        <ReadOnlyConfig form={form} />
      )}
    </section>
  )
}

function FlowNode({ x, y, width = 220, height = 82, title, lines = [], tone = 'default' }) {
  return (
    <g className={`flow-node ${tone}`}>
      <rect x={x} y={y} width={width} height={height} rx="14" />
      <text className="flow-title" x={x + width / 2} y={y + 29} textAnchor="middle">
        {title}
      </text>
      <text className="flow-copy" x={x + width / 2} y={y + 51} textAnchor="middle">
        {lines.map((line, index) => (
          <tspan x={x + width / 2} dy={index === 0 ? 0 : 17} key={`${line}-${index}`}>
            {line}
          </tspan>
        ))}
      </text>
    </g>
  )
}

function ProcessFlow() {
  const [config, setConfig] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  const loadConfig = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      setConfig(await get(`/v1/config/${PROCESS_KEY}`))
    } catch (requestError) {
      setError(apiErrorMessage(requestError))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    loadConfig()
  }, [loadConfig])

  const autoApproveUnder = formatMoney(config?.autoApproveUnder)
  const financeThreshold = formatMoney(config?.financeThreshold)
  const quorum = config?.quorum
    ? `${config.quorum.n}-of-${config.quorum.of}`
    : '—'
  const requiredFields = Array.isArray(config?.requiredFields)
    ? config.requiredFields.join(', ')
    : '—'

  return (
    <section className="view" aria-labelledby="flow-heading">
      <div className="view-heading">
        <div>
          <p className="eyebrow">Live decision rules</p>
          <h2 id="flow-heading">Process Flow</h2>
          <p>The decision tree is rendered from the current <span className="mono">{PROCESS_KEY}</span> config.</p>
        </div>
        <button className="secondary-button" type="button" onClick={loadConfig} disabled={loading}>
          Refresh rules
        </button>
      </div>

      {loading && <LoadingPanel label="Loading live flow rules…" />}
      {error && <Feedback feedback={{ type: 'error', message: error }} />}
      {!loading && !error && config && (
        <>
          <div className="flow-legend">
            <span><strong>Auto approve:</strong> under {autoApproveUnder}</span>
            <span><strong>Finance:</strong> from {financeThreshold}</span>
            <span><strong>Quorum:</strong> {quorum}</span>
          </div>
          <div className="flow-canvas">
            <svg viewBox="0 0 1200 680" role="img" aria-labelledby="flow-svg-title flow-svg-description">
              <title id="flow-svg-title">Invoice approval decision flow</title>
              <desc id="flow-svg-description">
                The invoice moves from start through extraction and AI review, then follows auto approval,
                request information, manager only, or manager then finance branches before finishing.
              </desc>
              <defs>
                <marker id="arrowhead" markerWidth="10" markerHeight="7" refX="9" refY="3.5" orient="auto">
                  <polygon points="0 0, 10 3.5, 0 7" />
                </marker>
              </defs>

              <rect className="lane automated" x="10" y="35" width="1180" height="285" rx="18" />
              <rect className="lane human" x="10" y="335" width="1180" height="330" rx="18" />
              <text className="lane-label" x="32" y="66">AUTOMATION</text>
              <text className="lane-label" x="32" y="366">HUMAN APPROVAL</text>

              <g className="flow-paths">
                <path d="M 165 177 L 205 177" />
                <path d="M 355 177 L 405 177" />
                <path d="M 585 177 C 620 177 620 95 660 95" />
                <path d="M 585 177 C 620 177 620 230 660 230" />
                <path d="M 495 218 C 495 390 600 403 660 403" />
                <path d="M 495 218 C 495 540 600 548 660 548" />
                <path d="M 900 95 C 980 95 1000 265 1015 306" />
                <path d="M 900 403 C 970 403 980 355 1015 350" />
                <path d="M 900 548 L 940 548" />
                <path d="M 1080 507 C 1080 445 1085 405 1085 382" />
                <path className="loop-path" d="M 660 230 C 615 230 625 290 575 290 C 420 290 385 265 445 218" />
              </g>

              <FlowNode x={45} y={140} width={120} height={74} title="START" tone="start" />
              <FlowNode x={205} y={136} width={150} height={82} title="EXTRACT" lines={['invoice fields']} />
              <FlowNode x={405} y={136} width={180} height={82} title="AI REVIEW" lines={['rules + rationale']} tone="decision" />

              <FlowNode
                x={660}
                y={55}
                width={240}
                height={80}
                title="AUTO_APPROVE"
                lines={[`Amount < ${autoApproveUnder}`]}
                tone="success"
              />
              <FlowNode
                x={660}
                y={185}
                width={240}
                height={90}
                title="REQUEST_INFO"
                lines={['Missing required fields', requiredFields]}
                tone="warning"
              />
              <FlowNode
                x={660}
                y={360}
                width={240}
                height={86}
                title="MANAGER_ONLY"
                lines={[`Below ${financeThreshold}`, 'manager decision']}
                tone="human"
              />
              <FlowNode
                x={660}
                y={505}
                width={240}
                height={86}
                title="MANAGER_THEN_FINANCE"
                lines={[`Amount ≥ ${financeThreshold}`, `Quorum ${quorum}`]}
                tone="human"
              />
              <FlowNode
                x={940}
                y={507}
                width={190}
                height={82}
                title="FINANCE VOTE"
                lines={[quorum]}
                tone="finance"
              />
              <FlowNode
                x={1015}
                y={305}
                width={150}
                height={78}
                title="FINISH"
                lines={['approved / rejected']}
                tone="finish"
              />
              <text className="loop-label" x="548" y="279">updated data loops back</text>
            </svg>
          </div>
        </>
      )}
    </section>
  )
}

const REQUIRED_INVOICE_FIELDS = ['poNumber', 'costCenter', 'taxId']

// WHY SendInvoice: the vendor portal's "create an invoice" screen. Submits
// POST /v1/transactions {process_key:"invoice_approval", data:{...}} — the exact
// contract verified in main.py. Amount is coerced to a number so the decision
// rules (data["amount"] comparisons) work.
function SendInvoice({ defaultVendor }) {
  const [vendor, setVendor] = useState(defaultVendor || '')
  const [amount, setAmount] = useState('')
  const [poNumber, setPoNumber] = useState('')
  const [costCenter, setCostCenter] = useState('')
  const [taxId, setTaxId] = useState('')
  const [customFields, setCustomFields] = useState([]) // [{ id, key, value }]
  const [busy, setBusy] = useState(false)
  const [feedback, setFeedback] = useState(null)
  const [extracting, setExtracting] = useState(false)
  const [extractError, setExtractError] = useState('')
  const [dragOver, setDragOver] = useState(false)

  function updateCustom(id, prop, value) {
    setCustomFields((current) => current.map((row) => (row.id === id ? { ...row, [prop]: value } : row)))
  }

  async function extractFromFile(chosen) {
    if (!chosen) return
    const isPdf = chosen.type === 'application/pdf' || chosen.name.toLowerCase().endsWith('.pdf')
    if (!isPdf) {
      setExtractError('Please choose a PDF file.')
      return
    }
    setExtractError('')
    setExtracting(true)
    try {
      const form = new FormData()
      form.append('file', chosen)
      const result = await upload('/v1/extract-invoice', form)
      const fields = result.fields || {}
      // Pre-fill only the values the LLM actually found (null => leave as-is).
      if (fields.vendor != null) setVendor(String(fields.vendor))
      if (fields.amount != null) setAmount(String(fields.amount))
      if (fields.poNumber != null) setPoNumber(String(fields.poNumber))
      if (fields.costCenter != null) setCostCenter(String(fields.costCenter))
      if (fields.taxId != null) setTaxId(String(fields.taxId))
      if (result.error) setExtractError(`Auto-fill partial: ${result.error}. Fill any missing fields manually.`)
    } catch (error) {
      setExtractError(apiErrorMessage(error))
    } finally {
      setExtracting(false)
    }
  }

  async function submitInvoice(event) {
    event.preventDefault()
    setFeedback(null)

    const amountNumber = Number(amount)
    if (!vendor.trim()) {
      setFeedback({ type: 'error', message: 'Vendor is required.' })
      return
    }
    if (amount === '' || Number.isNaN(amountNumber)) {
      setFeedback({ type: 'error', message: 'Enter a valid numeric amount.' })
      return
    }

    const data = {
      vendor: vendor.trim(),
      amount: amountNumber,
      poNumber: poNumber.trim(),
      costCenter: costCenter.trim(),
      taxId: taxId.trim(),
    }
    for (const { key, value } of customFields) {
      const name = key.trim()
      if (name && !(name in data)) data[name] = value
    }

    setBusy(true)
    try {
      const result = await post('/v1/transactions', { process_key: 'invoice_approval', data })
      setFeedback({ type: 'success', message: `Invoice submitted. Transaction ${result.transaction_id}.` })
      setAmount('')
      setPoNumber('')
      setCostCenter('')
      setTaxId('')
      setCustomFields([])
    } catch (error) {
      setFeedback({ type: 'error', message: apiErrorMessage(error) })
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="view" aria-labelledby="send-invoice-heading">
      <div className="view-heading">
        <div>
          <p className="eyebrow">Vendor portal</p>
          <h2 id="send-invoice-heading">Send Invoice</h2>
          <p>Submit an invoice for approval. Filling the required fields helps it route automatically.</p>
        </div>
      </div>

      <form className="config-form" onSubmit={submitInvoice}>
        <section className="config-section">
          <div className="config-section-heading">
            <div>
              <h3>Invoice details</h3>
              <p>Vendor, amount, and the required reference fields.</p>
            </div>
          </div>
          <div className="config-field-grid three-columns">
            <label className="config-field">
              <span>Vendor</span>
              <input type="text" value={vendor} onChange={(event) => setVendor(event.target.value)} required />
            </label>
            <label className="config-field">
              <span>Amount ($)</span>
              <input type="number" min="0" step="any" value={amount} onChange={(event) => setAmount(event.target.value)} required />
            </label>
            <label className="config-field">
              <span>PO number</span>
              <input type="text" value={poNumber} onChange={(event) => setPoNumber(event.target.value)} />
            </label>
            <label className="config-field">
              <span>Cost center</span>
              <input type="text" value={costCenter} onChange={(event) => setCostCenter(event.target.value)} />
            </label>
            <label className="config-field">
              <span>Tax ID</span>
              <input type="text" value={taxId} onChange={(event) => setTaxId(event.target.value)} />
            </label>
          </div>
        </section>

        <section className="config-section">
          <div className="config-section-heading">
            <div>
              <h3>Additional fields</h3>
              <p>Optional custom key/value pairs included with the invoice data.</p>
            </div>
          </div>
          {customFields.length === 0 && <p className="muted">No additional fields.</p>}
          {customFields.map((row) => (
            <div className="kv-row" key={row.id}>
              <input
                type="text"
                placeholder="Field name"
                value={row.key}
                onChange={(event) => updateCustom(row.id, 'key', event.target.value)}
              />
              <input
                type="text"
                placeholder="Value"
                value={row.value}
                onChange={(event) => updateCustom(row.id, 'value', event.target.value)}
              />
              <button
                type="button"
                className="secondary-button"
                onClick={() => setCustomFields((current) => current.filter((item) => item.id !== row.id))}
              >
                Remove
              </button>
            </div>
          ))}
          <button
            type="button"
            className="secondary-button"
            onClick={() => setCustomFields((current) => [...current, { id: uuid(), key: '', value: '' }])}
          >
            Add field
          </button>
        </section>

        <section className="config-section">
          <div className="config-section-heading">
            <div>
              <h3>Upload PDF (optional)</h3>
              <p>Drop a PDF invoice to auto-fill the fields, then review before submitting.</p>
            </div>
          </div>
          <label
            className={`pdf-dropzone ${dragOver ? 'drag-over' : ''}`}
            onDragOver={(event) => {
              event.preventDefault()
              setDragOver(true)
            }}
            onDragLeave={() => setDragOver(false)}
            onDrop={(event) => {
              event.preventDefault()
              setDragOver(false)
              extractFromFile(event.dataTransfer.files?.[0])
            }}
          >
            <input
              type="file"
              accept="application/pdf"
              className="pdf-file-input"
              onChange={(event) => extractFromFile(event.target.files?.[0])}
            />
            {extracting ? 'Extracting fields…' : 'Drag a PDF here, or click to choose a file'}
          </label>
          {extractError && <Feedback feedback={{ type: 'error', message: extractError }} />}
        </section>

        <div className="form-footer">
          <Feedback feedback={feedback} />
          <button className="primary-button" type="submit" disabled={busy}>
            {busy ? 'Submitting…' : 'Submit invoice'}
          </button>
        </div>
      </form>
    </section>
  )
}

// WHY VendorInbox: shows request_info tasks for invoices THIS vendor submitted
// (matched via transaction.submitted_by === username) and lets them supply the
// missing fields. Sending posts to the OPEN /v1/events endpoint (not the
// role-gated complete endpoint) with {decision:"resubmit", data:{...}}, which
// signals human_decision → the workflow merges the corrected data and re-reviews.
function VendorInbox({ username }) {
  const [items, setItems] = useState([])
  const [inputs, setInputs] = useState({}) // { token: { field: value } }
  const [busyToken, setBusyToken] = useState('')
  const [feedback, setFeedback] = useState({})
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState('')

  const loadInbox = useCallback(
    async (showLoading = true) => {
      if (showLoading) setLoading(true)
      try {
        const [openTasks, transactions] = await Promise.all([
          get('/v1/tasks?status=open'),
          get('/v1/transactions'),
        ])
        const txById = Object.fromEntries(transactions.map((transaction) => [transaction.id, transaction]))
        const mine = openTasks
          .filter((task) => task.node_id === 'request_info')
          .filter((task) => txById[task.transaction_id]?.submitted_by === username)
          .map((task) => {
            const invoice = txById[task.transaction_id]?.data_snapshot ?? {}
            const need = Array.isArray(task.completion_policy?.need) && task.completion_policy.need.length
              ? task.completion_policy.need
              : REQUIRED_INVOICE_FIELDS.filter((field) => !invoice[field])
            return { task, invoice, missing: need }
          })
        setItems(mine)
        setLoadError('')
      } catch (error) {
        setLoadError(apiErrorMessage(error))
      } finally {
        if (showLoading) setLoading(false)
      }
    },
    [username],
  )

  useEffect(() => {
    loadInbox()
    const timer = window.setInterval(() => loadInbox(false), 5000)
    return () => window.clearInterval(timer)
  }, [loadInbox])

  function updateInput(token, field, value) {
    setInputs((current) => ({ ...current, [token]: { ...current[token], [field]: value } }))
  }

  async function sendCorrection(task, missing) {
    const fields = missing.length ? missing : REQUIRED_INVOICE_FIELDS
    const filled = {}
    for (const field of fields) {
      const value = inputs[task.token]?.[field]
      if (value !== undefined && value !== '') filled[field] = value
    }

    setBusyToken(task.token)
    setFeedback((current) => ({ ...current, [task.token]: null }))
    try {
      await post('/v1/events', {
        transaction_id: task.transaction_id,
        task_token: task.token,
        idempotency_key: uuid(),
        kind: 'human',
        payload: { decision: 'resubmit', data: filled },
      })
      setFeedback((current) => ({
        ...current,
        [task.token]: { type: 'success', message: 'Details sent. The invoice is being re-reviewed.' },
      }))
      await loadInbox(false)
    } catch (error) {
      setFeedback((current) => ({
        ...current,
        [task.token]: { type: 'error', message: apiErrorMessage(error) },
      }))
    } finally {
      setBusyToken('')
    }
  }

  return (
    <section className="view" aria-labelledby="vendor-inbox-heading">
      <div className="view-heading">
        <div>
          <p className="eyebrow">Vendor portal</p>
          <h2 id="vendor-inbox-heading">Inbox</h2>
          <p>Invoices you submitted that need more information.</p>
        </div>
        <button className="secondary-button" type="button" onClick={() => loadInbox()} disabled={loading}>
          Refresh
        </button>
      </div>

      {loading && <LoadingPanel label="Loading your invoices…" />}
      {!loading && loadError && <Feedback feedback={{ type: 'error', message: loadError }} />}
      {!loading && !loadError && items.length === 0 && (
        <div className="empty-state">
          <h3>Nothing needs your attention</h3>
          <p>When an invoice you submitted needs more details, it will show up here.</p>
        </div>
      )}

      <div className="task-list">
        {items.map(({ task, invoice, missing }) => (
          <article className="task-card" key={task.token}>
            <div className="task-card-header">
              <div>
                <span className="status-pill">needs info</span>
                <h3>{invoice.vendor || 'Invoice'}</h3>
              </div>
              <span className="role-badge">request info</span>
            </div>

            <div className="task-detail-grid">
              <div>
                <h4>Invoice</h4>
                <InvoiceFields data={invoice} />
              </div>
              <div className="ai-summary">
                <h4>Missing information</h4>
                <p>{missing.length ? `Please provide: ${missing.join(', ')}` : 'Review and resend the invoice.'}</p>
              </div>
            </div>

            <form
              className="vendor-fill-form"
              onSubmit={(event) => {
                event.preventDefault()
                sendCorrection(task, missing)
              }}
            >
              <div className="config-field-grid three-columns">
                {(missing.length ? missing : REQUIRED_INVOICE_FIELDS).map((field) => (
                  <label className="config-field" key={field}>
                    <span>{field}</span>
                    <input
                      type="text"
                      value={inputs[task.token]?.[field] ?? ''}
                      onChange={(event) => updateInput(task.token, field, event.target.value)}
                    />
                  </label>
                ))}
              </div>
              <div className="vendor-fill-actions">
                <button className="primary-button" type="submit" disabled={busyToken === task.token}>
                  {busyToken === task.token ? 'Sending…' : 'Send'}
                </button>
              </div>
            </form>

            <Feedback feedback={feedback[task.token]} />
          </article>
        ))}
      </div>
    </section>
  )
}

function App() {
  const username = keycloak.tokenParsed?.preferred_username || 'Unknown user'
  const roles = keycloak.tokenParsed?.realm_access?.roles || []
  const isVendor = roles.includes('vendor')
  const parsed = keycloak.tokenParsed || {}
  const defaultVendor =
    parsed.name ||
    [parsed.given_name, parsed.family_name].filter(Boolean).join(' ') ||
    username
  // Vendors get a restricted portal (Send Invoice + Inbox only); everyone else
  // keeps the full operator UI. Hidden tabs are NOT rendered for vendors.
  const navTabs = isVendor
    ? [
        { id: 'send', label: 'Send Invoice' },
        { id: 'inbox', label: 'Inbox' },
      ]
    : tabs
  const [activeTab, setActiveTab] = useState(navTabs[0].id)

  return (
    <div className="app-shell">
      <header className="app-header">
        <div className="brand">
          <span className="brand-mark" aria-hidden="true">W</span>
          <div>
            <h1>Workflow Engine</h1>
            <p>Invoice approval</p>
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
        {isVendor ? (
          <>
            {activeTab === 'send' && <SendInvoice defaultVendor={defaultVendor} />}
            {activeTab === 'inbox' && <VendorInbox username={username} />}
          </>
        ) : (
          <>
            {activeTab === 'tasks' && <TaskInbox roles={roles} username={username} />}
            {activeTab === 'monitor' && <Monitor />}
            {activeTab === 'configuration' && <Configuration canEdit={roles.includes('process_author')} />}
            {activeTab === 'flow' && <ProcessFlow />}
            {activeTab === 'start' && <StartProcess />}
          </>
        )}
      </main>
    </div>
  )
}

export default App
