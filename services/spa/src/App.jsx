import { useCallback, useEffect, useMemo, useState } from 'react'
import { get, post, put, upload } from './api'
import keycloak from './keycloak'
import { uuid } from './uuid'
import { FlowDiagram, GenericTaskForm, ProcessFlowDynamic } from './generic'
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

// Work out ONE transaction's journey through its workflow, using the audit
// events plus the workflow definition:
//   * AI decision events record which decision step ran and which route it chose
//   * task-created events record which human step was entered
//   * automated / timer steps in between are filled in by following the definition
// Returns the steps visited, the connections actually followed, where it is now,
// and the outcome (so a finished step can be tinted green / red).
function deriveJourney(pdd, events, status) {
  const nodes = Array.isArray(pdd && pdd.nodes) ? pdd.nodes : []
  if (!nodes.length) return null
  const byId = {}
  nodes.forEach((n) => { byId[n.id] = n })
  const nextOf = (n) => (n && typeof n.next === 'string' ? n.next : null)

  const anchors = []                 // human/decision steps really entered, in order
  const routes = {}                  // decision step -> the routes it chose, in order
  for (const e of events || []) {
    const p = e.payload || {}
    if (e.type === 'LLM_DECISION' && p.node_id) {
      anchors.push(p.node_id)
      ;(routes[p.node_id] = routes[p.node_id] || []).push(p.route)
    } else if (e.type === 'TASK_CREATED' && p.node_id) {
      anchors.push(p.node_id)
    }
  }

  const visited = new Set()
  const taken = new Set()
  const order = []
  const used = {}
  const targetOf = (id) => {
    const n = byId[id]
    if (!n) return null
    if (n.type === 'llm_decision' || n.type === 'decision') {
      const q = routes[id] || []
      const i = used[id] || 0
      used[id] = i + 1
      const route = q[i]
      return route == null ? null : ((n.edges || {})[route] || null)
    }
    return nextOf(n)
  }

  // Every step a node can lead to (used to fill gaps we can't compute directly).
  const outgoing = (n) => {
    if (!n) return []
    const out = []
    if (typeof n.next === 'string') out.push(n.next)
    if (n.edges && !Array.isArray(n.edges)) Object.values(n.edges).forEach((t) => out.push(t))
    if (Array.isArray(n.edges)) n.edges.forEach((e) => { if (e && e.to) out.push(e.to) })
    return out.filter(Boolean)
  }
  // Shortest route between two steps, so automated steps sitting between two
  // recorded points (e.g. finance -> finalize -> end_approved) are included.
  const shortestPath = (from, to) => {
    if (from === to) return [from]
    const queue = [[from]]
    const seen = new Set([from])
    while (queue.length) {
      const path = queue.shift()
      for (const nb of outgoing(byId[path[path.length - 1]])) {
        if (seen.has(nb)) continue
        const nextPath = [...path, nb]
        if (nb === to) return nextPath
        seen.add(nb)
        queue.push(nextPath)
      }
    }
    return null
  }

  const startNode = nodes.find((n) => n.type === 'start')
  let cur = startNode ? nextOf(startNode) : nodes[0].id
  let ai = 0
  for (let guard = 0; guard < 300 && cur; guard += 1) {
    visited.add(cur)
    order.push(cur)
    while (anchors[ai] === cur) ai += 1          // consume matching anchors
    const n = byId[cur]
    if (!n || n.type === 'end') break

    const direct = targetOf(cur)
    if (direct) {                                // decisions + single-exit steps
      taken.add(`${cur}>${direct}`)
      cur = direct
      continue
    }

    // A human step: the definition alone can't say which way it went. Aim for
    // the next step the events show was entered; if the run has finished, aim
    // for the matching end step. Then fill in everything on the way.
    let aim = anchors[ai] || null
    if (!aim && status && status !== 'running') {
      const endNode = nodes.find((x) => x.type === 'end' && x.outcome === status)
      aim = endNode ? endNode.id : null
    }
    if (!aim || aim === cur) break
    const path = shortestPath(cur, aim)
    if (!path) break
    for (let k = 1; k < path.length; k += 1) {
      taken.add(`${path[k - 1]}>${path[k]}`)
      visited.add(path[k])
      order.push(path[k])
    }
    cur = aim
  }

  let current
  if (status && status !== 'running') {
    const endNode = nodes.find((x) => x.type === 'end' && x.outcome === status)
    current = endNode ? endNode.id : order[order.length - 1]
  } else {
    const lastTask = [...(events || [])].reverse()
      .find((e) => e.type === 'TASK_CREATED' && (e.payload || {}).node_id)
    current = lastTask ? lastTask.payload.node_id : order[order.length - 1]
  }
  if (current) visited.add(current)

  return {
    visited: [...visited],
    taken: [...taken],
    current,
    outcome: status === 'approved' || status === 'rejected' ? status : undefined,
  }
}

// Show whatever fields THIS process carries (invoice: vendor/amount;
// leave: employee/days; ...). Nothing is hardcoded to one workflow.
function summarize(data, max = 3) {
  if (!data || typeof data !== 'object') return '—'
  const parts = Object.entries(data)
    .filter(([, v]) => v !== null && v !== undefined && v !== '')
    .slice(0, max)
    .map(([k, v]) => `${k}: ${typeof v === 'number' ? v.toLocaleString() : String(v)}`)
  return parts.length ? parts.join(' · ') : '—'
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

// A plain animated spinner — no wording, used inside the transaction popup.
function Spinner() {
  return (
    <div className="spinner-wrap" role="status" aria-label="Loading">
      <span className="spinner" />
    </div>
  )
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
      if (task.is_quorum) {
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
            task.is_quorum
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
    const isFinanceTask = !!task.is_quorum
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
          const isFinanceTask = !!task.is_quorum
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
  const [processes, setProcesses] = useState([])
  const [proc, setProc] = useState('')            // '' = all workflows
  const [transactions, setTransactions] = useState([])
  const [stats, setStats] = useState(null)
  // KPI filter ('total' = all) + 1-based page within that filter.
  const [filter, setFilter] = useState('total')
  const [page, setPage] = useState(1)
  const [selected, setSelected] = useState(null)
  const [tab, setTab] = useState('flow')          // popup tab: 'flow' | 'log'
  const [journey, setJourney] = useState(null)    // visited steps / path / current
  const [journeyPdd, setJourneyPdd] = useState(null)
  const [flowLoading, setFlowLoading] = useState(false)
  const [flowError, setFlowError] = useState('')
  const [history, setHistory] = useState([])
  const [loading, setLoading] = useState(true)
  const [historyLoading, setHistoryLoading] = useState(false)
  const [error, setError] = useState('')
  const [historyError, setHistoryError] = useState('')

  useEffect(() => {
    // Only the workflows this person is allowed to see (admins/authors get all).
    get('/v1/my-processes')
      .then((res) => setProcesses((res && res.processes) || []))
      .catch(() => setProcesses([]))
  }, [])

  useEffect(() => {
    let active = true
    let polling = false

    async function pollTransactions() {
      if (polling) return
      polling = true

      try {
        const statusParam = filter === 'total' ? '' : `&status=${encodeURIComponent(filter)}`
        const procParam = proc ? `&process_key=${encodeURIComponent(proc)}` : ''
        const [rows, counts] = await Promise.all([
          get(`/v1/transactions?limit=${PAGE_SIZE}&offset=${(page - 1) * PAGE_SIZE}${statusParam}${procParam}`),
          get(`/v1/transactions/stats${proc ? `?process_key=${encodeURIComponent(proc)}` : ''}`),
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
  }, [filter, page, proc])

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

  // Opening the popup loads only the FAST things (raw events + the workflow
  // definition) so the Flow tab appears immediately. The AI-narrated audit is
  // slow (it asks the model to write a sentence per event), so it is fetched
  // ONLY when the Log tab is actually opened.
  // Clicking a transaction starts BOTH loads at once: the flow (fast) renders
  // immediately, while the AI-narrated audit keeps loading in the background so
  // it is usually ready by the time the Log tab is opened.
  async function selectTransaction(transaction) {
    setSelected(transaction)
    setTab('flow')
    setHistory([])
    setHistoryError('')
    setJourney(null)
    setFlowError('')
    setFlowLoading(true)
    setHistoryLoading(true)

    const id = encodeURIComponent(transaction.id)

    // Fast path: raw events + the workflow definition -> the highlighted flow.
    try {
      const [events, def] = await Promise.all([
        get(`/v1/transactions/${id}/history`),
        transaction.process_key
          ? get(`/v1/definitions/${encodeURIComponent(transaction.process_key)}`)
          : Promise.resolve(null),
      ])
      setJourneyPdd(def)
      setJourney(deriveJourney(def, events || [], normalizeStatus(transaction.status)))
    } catch (requestError) {
      setFlowError(apiErrorMessage(requestError))
    } finally {
      setFlowLoading(false)
    }

    // Slow path (AI writes a sentence per event) — runs on its own, never blocks
    // the flow. Deterministic text is used by the backend if the model is slow.
    get(`/v1/transactions/${id}/history?format=narrative`)
      .then((events) => setHistory(events || []))
      .catch((requestError) => setHistoryError(apiErrorMessage(requestError)))
      .finally(() => setHistoryLoading(false))
  }

  function closeModal() { setSelected(null) }

  // Escape closes the popup.
  useEffect(() => {
    if (!selected) return undefined
    const onKey = (e) => { if (e.key === 'Escape') closeModal() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [selected])

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

      {/* One monitor per workflow — generic, built from whatever exists. */}
      {processes.length > 0 && (
        <div className="process-strip" role="tablist" aria-label="Workflows">
          <button type="button" role="tab" aria-selected={proc === ''}
            className={`process-chip ${proc === '' ? 'active' : ''}`}
            onClick={() => { setProc(''); setPage(1); setSelected(null) }}>All workflows</button>
          {processes.map((key) => (
            <button type="button" key={key} role="tab" aria-selected={proc === key}
              className={`process-chip ${proc === key ? 'active' : ''}`}
              onClick={() => { setProc(key); setPage(1); setSelected(null) }}>{key}</button>
          ))}
        </div>
      )}

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
                    <th>Process</th>
                    <th>Details</th>
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
                      <td>{transaction.process_key || '—'}</td>
                      <td>{summarize(transaction.data_snapshot)}</td>
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

      {/* Popup: blurred backdrop, ~85% of the screen, Flow tab first then Log.
          Closes on ×, on a backdrop click, or with Escape. */}
      {selected && (
        <div className="modal-backdrop" onMouseDown={(e) => { if (e.target === e.currentTarget) closeModal() }}>
        <section className="modal-panel" role="dialog" aria-modal="true" aria-labelledby="audit-heading">
          <div className="audit-heading">
            <div>
              <h3 id="audit-heading">{selected.process_key || 'Transaction'}</h3>
              <p className="mono">{selected.id}</p>
            </div>
            <button className="icon-button" type="button" onClick={closeModal} aria-label="Close">
              ×
            </button>
          </div>

          <div className="modal-tabs" role="tablist">
            <button type="button" role="tab" aria-selected={tab === 'flow'}
              className={tab === 'flow' ? 'active' : ''} onClick={() => setTab('flow')}>Flow</button>
            <button type="button" role="tab" aria-selected={tab === 'log'}
              className={tab === 'log' ? 'active' : ''} onClick={() => setTab('log')}>Log</button>
          </div>

          {/* Status / Process / Created / Closed belong to the Log tab only —
              the Flow tab is kept clear so the diagram gets the whole area. */}
          {tab === 'log' && (
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
          )}

          <div className={`modal-body ${tab === 'flow' ? 'no-scroll' : ''}`}>
            {tab === 'flow' && (
              <>
                {flowLoading && <Spinner />}
                {flowError && <Feedback feedback={{ type: 'error', message: flowError }} />}
                {!flowLoading && !flowError && !journeyPdd && (
                  <p className="muted">This transaction&apos;s workflow definition could not be loaded.</p>
                )}
                {!flowLoading && journeyPdd && (
                  <FlowDiagram
                    pdd={journeyPdd}
                    height="100%"
                    visited={journey ? journey.visited : undefined}
                    taken={journey ? journey.taken : undefined}
                    current={journey ? journey.current : undefined}
                    outcome={journey ? journey.outcome : undefined}
                    legend="Bold blue = path travelled · ringed = where it is now · faded = never used · Shift+scroll to zoom"
                  />
                )}
              </>
            )}

            {tab === 'log' && (
              <>
                {historyLoading && <Spinner />}
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
              </>
            )}
          </div>
        </section>
        </div>
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
  // "Business" = holds a role that isn't a core app role. Those users do tasks
  // and watch the monitor for their workflow. Admin administers; author builds.
  const isBusiness = roles.some((r) => r !== 'process_author' && r !== 'ops_admin')
  const navTabs = []
  if (isBusiness) navTabs.push({ id: 'tasks', label: 'Task Inbox' })
  if (isAuthor) navTabs.push({ id: 'builder', label: 'Builder' })
  // Everyone can view the current process flow.
  navTabs.push({ id: 'flow', label: 'Process Flow' })
  if (isBusiness || isOps) navTabs.push({ id: 'monitor', label: 'Monitor' })
  if (isOps) navTabs.push({ id: 'admin', label: 'Admin' })
  const [activeTab, setActiveTab] = useState(navTabs[0].id)

  // Measure the sticky header+tabs once (and on resize) so anything that must
  // sit below them (the Builder's sticky preview) always lines up.
  useEffect(() => {
    const measure = () => {
      const el = document.querySelector('.app-top')
      if (el) document.documentElement.style.setProperty('--top-h', `${el.offsetHeight}px`)
    }
    measure()
    window.addEventListener('resize', measure)
    return () => window.removeEventListener('resize', measure)
  }, [])

  return (
    <div className="app-shell">
      <div className="app-top">
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
      </div>

      <main>
        {activeTab === 'tasks' && <TaskInbox roles={roles} username={username} />}
        {activeTab === 'flow' && <ProcessFlowDynamic />}
        {activeTab === 'builder' && <ProcessBuilder />}
        {activeTab === 'monitor' && <Monitor roles={roles} />}
        {activeTab === 'admin' && <AdminPanel />}
      </main>
    </div>
  )
}

export default App
