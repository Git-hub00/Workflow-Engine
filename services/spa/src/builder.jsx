// services/spa/src/builder.jsx
//
// Process Builder (design-time). Two-column layout: LEFT = ordered configuration
// panels with inline validation; RIGHT = a sticky, zoomable/pannable live diagram
// that rebuilds as you type. Assembles the PDD JSON and publishes via the P3
// Definition Service (/v1/definitions/validate, /v1/definitions). No engine change.
import { useMemo, useState } from 'react'
import { post } from './api'
import { FlowDiagram } from './generic'

const NODE_TYPES = ['start', 'automated', 'llm_decision', 'human_task', 'timer', 'gateway_exclusive', 'end']
const ACTIONS = ['extract_fields', 'post_to_erp']            // actions the engine can run
const TIMEOUT_ACTIONS = ['remind', 'escalate', 'auto_approve', 'auto_reject']

let _seq = 0
const uid = () => `k${++_seq}`

function Err({ msg }) {
  return msg ? <span className="field-error">{msg}</span> : null
}

export function ProcessBuilder() {
  const [processKey, setProcessKey] = useState('')
  const [mailbox, setMailbox] = useState('')
  const [roles, setRoles] = useState([{ logical: '', kc: '' }])
  const [configRows, setConfigRows] = useState([])
  const [dataFields, setDataFields] = useState([{ name: '', type: 'string' }])
  const [nodes, setNodes] = useState([
    { key: uid(), id: 'start', type: 'start', next: '' },
    { key: uid(), id: 'end_done', type: 'end', outcome: 'approved' },
  ])
  const [notifications, setNotifications] = useState([])
  const [validation, setValidation] = useState(null)
  const [feedback, setFeedback] = useState(null)
  const [busy, setBusy] = useState(false)

  const nodeIds = nodes.map((n) => n.id).filter(Boolean)
  const logicalRoles = roles.map((r) => r.logical).filter(Boolean)

  const patchNode = (key, patch) => setNodes((ns) => ns.map((n) => (n.key === key ? { ...n, ...patch } : n)))
  const patchList = (setter, idx, patch) => setter((rows) => rows.map((r, i) => (i === idx ? { ...r, ...patch } : r)))
  const removeAt = (setter, idx) => setter((rows) => rows.filter((_, i) => i !== idx))

  // ---- assemble the PDD -------------------------------------------------
  function buildPdd() {
    const rolesMap = {}
    for (const r of roles) if (r.logical && r.kc) rolesMap[r.logical] = r.kc
    const dataSchema = {}
    for (const f of dataFields) if (f.name) dataSchema[f.name] = f.type
    const config = {}
    for (const c of configRows) {
      if (!c.key) continue
      const raw = String(c.value ?? '').trim()
      config[c.key] = /^-?\d+(\.\d+)?$/.test(raw) ? Number(raw) : c.value
    }
    const outNodes = nodes.map((n) => {
      const node = { id: n.id, type: n.type }
      if (n.type === 'start') {
        if (n.next) node.next = n.next
      } else if (n.type === 'automated') {
        node.action = n.action || ''
        if (n.next) node.next = n.next
      } else if (n.type === 'timer') {
        if (n.waitHours) node.timeout = { seconds: Number(n.waitHours) * 3600 }
        if (n.next) node.next = n.next
      } else if (n.type === 'end') {
        node.outcome = n.outcome || 'completed'
      } else if (n.type === 'gateway_exclusive') {
        const edges = (n.edges || []).filter((e) => e.to).map((e) => ({ when: e.when || 'default', to: e.to }))
        if (edges.length) node.edges = edges
      } else if (n.type === 'human_task') {
        node.assignment = { role: n.role || '' }
        const fields = (n.formFields || []).filter((f) => f.key).map((f) => {
          const field = { key: f.key, type: f.type || 'string', required: !!f.required }
          if (f.type === 'enum' && f.values) field.values = f.values.split(',').map((s) => s.trim()).filter(Boolean)
          return field
        })
        if (fields.length) node.form_schema = { fields }
        if (n.completionMode === 'quorum' && n.quorumN && n.quorumOf) {
          node.completion = {
            mode: 'quorum', n: Number(n.quorumN), of: Number(n.quorumOf),
            rejectShortCircuits: !!n.rejectShortCircuits,
          }
        }
        const timeout = {}
        if (n.slaHours) timeout.slaHours = Number(n.slaHours)
        if (n.onTimeout && n.onTimeout !== 'remind') timeout.on_timeout = n.onTimeout
        if (Object.keys(timeout).length) node.timeout = timeout
        const edges = (n.edges || []).filter((e) => e.to).map((e) => ({ when: e.when || 'default', to: e.to }))
        if (edges.length) node.edges = edges
        else if (n.next) node.next = n.next
      } else if (n.type === 'llm_decision') {
        node.routes = (n.routes || []).filter((r) => r.edge).map((r) => ({ edge: r.edge, when: r.when || 'default' }))
        node.edges = {}
        for (const r of n.routes || []) if (r.edge && r.to) node.edges[r.edge] = r.to
      }
      return node
    })
    const pdd = { process_key: processKey, version: 1, roles: rolesMap, nodes: outNodes }
    if (mailbox) pdd.mailbox = mailbox
    if (Object.keys(config).length) pdd.config = config
    if (Object.keys(dataSchema).length) pdd.data_schema = dataSchema
    const notifs = notifications.filter((x) => x.on).map((x) => ({
      on: x.on,
      ...(x.to_role ? { to_role: x.to_role } : {}),
      ...(x.to ? { to: x.to } : {}),
      template: x.template || '',
    }))
    if (notifs.length) pdd.notifications = notifs
    return pdd
  }

  const pdd = useMemo(buildPdd, [processKey, mailbox, roles, configRows, dataFields, nodes, notifications])

  // ---- inline validation (client-side, advisory) ------------------------
  const errs = useMemo(() => {
    const ids = nodes.map((n) => n.id)
    const perNode = {}
    for (const n of nodes) {
      const e = {}
      if (!n.id) e.id = 'Step id is required'
      else if (ids.filter((x) => x === n.id).length > 1) e.id = 'Duplicate step id'
      if (n.type === 'automated' && !n.action) e.action = 'Pick an action'
      if ((n.type === 'start' || n.type === 'automated' || n.type === 'timer') && !n.next) e.next = 'Choose the next step'
      if (n.type === 'end' && !n.outcome) e.outcome = 'Set an outcome'
      if (n.type === 'human_task') {
        if (!n.role) e.role = 'Select a role'
        else if (!logicalRoles.includes(n.role)) e.role = 'This role is not defined above'
        if (n.completionMode === 'quorum' && (!n.quorumN || !n.quorumOf)) e.quorum = 'Set N and M'
        if (!(n.edges || []).some((x) => x.to) && !n.next) e.edges = 'Add a rule → step (or a next step)'
      }
      if (n.type === 'llm_decision' && !(n.routes || []).some((r) => r.edge && r.to)) e.routes = 'Add a route with an edge name and target'
      perNode[n.key] = e
    }
    const general = []
    if (!processKey.trim()) general.push('Process key is required')
    if (nodes.filter((n) => n.type === 'start').length !== 1) general.push('Exactly one start step is required')
    if (!nodes.some((n) => n.type === 'end')) general.push('At least one end step is required')
    const count = general.length + nodes.reduce((s, n) => s + Object.keys(perNode[n.key]).length, 0)
    return { perNode, general, count }
  }, [processKey, nodes, logicalRoles])

  async function validate() {
    setBusy(true); setFeedback(null)
    try { setValidation(await post('/v1/definitions/validate', { pdd })) }
    catch (e) { setFeedback({ type: 'error', message: e.message || String(e) }) }
    finally { setBusy(false) }
  }
  async function publish() {
    setBusy(true); setFeedback(null)
    try {
      const res = await post('/v1/definitions', { pdd })
      setFeedback({ type: 'success', message: `Published '${res.process_key}' v${res.version}.` })
    } catch (e) {
      setFeedback({ type: 'error', message: e.message || String(e) })
    } finally { setBusy(false) }
  }

  const notifEvents = [
    ...nodes.filter((n) => n.type === 'human_task' && n.id).map((n) => `task_created:${n.id}`),
    'completed:approved', 'completed:rejected',
  ]

  return (
    <section className="view" aria-labelledby="builder-heading">
      <div className="view-heading">
        <div>
          <p className="eyebrow">Design time</p>
          <h2 id="builder-heading">Process Builder</h2>
          <p>Add engines and rules on the left; the flow builds live on the right. Fix any red notes, then Publish.</p>
        </div>
      </div>

      <div className="builder-layout">
        {/* ---------------- LEFT: configuration ---------------- */}
        <div className="builder-config">
          {/* Basics */}
          <section className="panel">
            <h3>1 · Basics</h3>
            <div className="config-field-grid three-columns">
              <label className="config-field"><span>Process key *</span>
                <input value={processKey} placeholder="e.g. purchase_order" onChange={(e) => setProcessKey(e.target.value)} />
                <Err msg={errs.general.includes('Process key is required') ? 'Required' : null} /></label>
              <label className="config-field"><span>Mailbox (optional)</span>
                <input value={mailbox} placeholder="e.g. procurement" onChange={(e) => setMailbox(e.target.value)} /></label>
            </div>
          </section>

          {/* Roles */}
          <section className="panel">
            <h3>2 · Roles</h3>
            <p className="muted">Map a friendly role name to a Keycloak realm role (create the role in Admin first).</p>
            {roles.map((r, i) => (
              <div className="kv-row" key={`role-${i}`}>
                <input placeholder="logical (e.g. manager)" value={r.logical} onChange={(e) => patchList(setRoles, i, { logical: e.target.value })} />
                <input placeholder="keycloak role (e.g. ap_manager)" value={r.kc} onChange={(e) => patchList(setRoles, i, { kc: e.target.value })} />
                <button type="button" className="secondary-button" onClick={() => removeAt(setRoles, i)}>Remove</button>
              </div>
            ))}
            <button type="button" className="secondary-button" onClick={() => setRoles((x) => [...x, { logical: '', kc: '' }])}>Add role</button>
          </section>

          {/* Config */}
          <section className="panel">
            <h3>3 · Config values</h3>
            <p className="muted">Named numbers/strings your rules reference (e.g. financeThreshold = 5000).</p>
            {configRows.map((c, i) => (
              <div className="kv-row" key={`cfg-${i}`}>
                <input placeholder="key" value={c.key} onChange={(e) => patchList(setConfigRows, i, { key: e.target.value })} />
                <input placeholder="value" value={c.value ?? ''} onChange={(e) => patchList(setConfigRows, i, { value: e.target.value })} />
                <button type="button" className="secondary-button" onClick={() => removeAt(setConfigRows, i)}>Remove</button>
              </div>
            ))}
            <button type="button" className="secondary-button" onClick={() => setConfigRows((x) => [...x, { key: '', value: '' }])}>Add value</button>
          </section>

          {/* Data fields */}
          <section className="panel">
            <h3>4 · Data fields</h3>
            <p className="muted">Fields a request carries (used by extraction and email intake).</p>
            {dataFields.map((f, i) => (
              <div className="kv-row" key={`data-${i}`}>
                <input placeholder="field name" value={f.name} onChange={(e) => patchList(setDataFields, i, { name: e.target.value })} />
                <select value={f.type} onChange={(e) => patchList(setDataFields, i, { type: e.target.value })}>
                  <option value="string">string</option>
                  <option value="number">number</option>
                </select>
                <button type="button" className="secondary-button" onClick={() => removeAt(setDataFields, i)}>Remove</button>
              </div>
            ))}
            <button type="button" className="secondary-button" onClick={() => setDataFields((x) => [...x, { name: '', type: 'string' }])}>Add field</button>
          </section>

          {/* Steps */}
          <section className="panel">
            <h3>5 · Steps</h3>
            <p className="muted">Each step is an engine node. Connect them by choosing target steps.</p>
            {nodes.map((n) => {
              const e = errs.perNode[n.key] || {}
              return (
                <div className="step-card" key={n.key}>
                  <div className="config-field-grid three-columns">
                    <label className="config-field"><span>Step id</span>
                      <input value={n.id} onChange={(ev) => patchNode(n.key, { id: ev.target.value })} />
                      <Err msg={e.id} /></label>
                    <label className="config-field"><span>Type</span>
                      <select value={n.type} onChange={(ev) => patchNode(n.key, { type: ev.target.value })}>
                        {NODE_TYPES.map((t) => <option key={t} value={t}>{t}</option>)}
                      </select></label>
                    <div className="config-field end-align">
                      <button type="button" className="secondary-button" onClick={() => removeAt(setNodes, nodes.indexOf(n))}>Remove step</button>
                    </div>
                  </div>

                  {(n.type === 'start' || n.type === 'automated' || n.type === 'timer') && (
                    <div className="config-field-grid three-columns">
                      {n.type === 'automated' && (
                        <label className="config-field"><span>Action</span>
                          <select value={n.action || ''} onChange={(ev) => patchNode(n.key, { action: ev.target.value })}>
                            <option value="">—</option>
                            {ACTIONS.map((a) => <option key={a} value={a}>{a}</option>)}
                          </select>
                          <Err msg={e.action} /></label>
                      )}
                      {n.type === 'timer' && (
                        <label className="config-field"><span>Wait (hours)</span>
                          <input type="number" value={n.waitHours || ''} onChange={(ev) => patchNode(n.key, { waitHours: ev.target.value })} /></label>
                      )}
                      <label className="config-field"><span>Next step</span>
                        <select value={n.next || ''} onChange={(ev) => patchNode(n.key, { next: ev.target.value })}>
                          <option value="">—</option>
                          {nodeIds.map((id) => <option key={id} value={id}>{id}</option>)}
                        </select>
                        <Err msg={e.next} /></label>
                    </div>
                  )}

                  {n.type === 'end' && (
                    <label className="config-field"><span>Outcome</span>
                      <input value={n.outcome || ''} placeholder="approved / rejected" onChange={(ev) => patchNode(n.key, { outcome: ev.target.value })} />
                      <Err msg={e.outcome} /></label>
                  )}

                  {n.type === 'human_task' && (
                    <div>
                      <div className="config-field-grid three-columns">
                        <label className="config-field"><span>Role</span>
                          <select value={n.role || ''} onChange={(ev) => patchNode(n.key, { role: ev.target.value })}>
                            <option value="">—</option>
                            {logicalRoles.map((r) => <option key={r} value={r}>{r}</option>)}
                          </select>
                          <Err msg={e.role} /></label>
                        <label className="config-field"><span>Completion</span>
                          <select value={n.completionMode || 'single'} onChange={(ev) => patchNode(n.key, { completionMode: ev.target.value })}>
                            <option value="single">single approver</option>
                            <option value="quorum">quorum (N of M)</option>
                          </select></label>
                        <label className="config-field"><span>SLA (hours)</span>
                          <input type="number" value={n.slaHours || ''} onChange={(ev) => patchNode(n.key, { slaHours: ev.target.value })} /></label>
                      </div>

                      {n.completionMode === 'quorum' && (
                        <div className="config-field-grid three-columns">
                          <label className="config-field"><span>Approvals needed (N)</span>
                            <input type="number" value={n.quorumN || ''} onChange={(ev) => patchNode(n.key, { quorumN: ev.target.value })} /></label>
                          <label className="config-field"><span>Of participants (M)</span>
                            <input type="number" value={n.quorumOf || ''} onChange={(ev) => patchNode(n.key, { quorumOf: ev.target.value })} /></label>
                          <label className="config-field toggle-field">
                            <input type="checkbox" checked={!!n.rejectShortCircuits} onChange={(ev) => patchNode(n.key, { rejectShortCircuits: ev.target.checked })} />
                            <span>one reject ends it</span></label>
                          <Err msg={e.quorum} />
                        </div>
                      )}

                      <label className="config-field"><span>On SLA timeout</span>
                        <select value={n.onTimeout || 'remind'} onChange={(ev) => patchNode(n.key, { onTimeout: ev.target.value })}>
                          {TIMEOUT_ACTIONS.map((t) => <option key={t} value={t}>{t}</option>)}
                        </select></label>

                      <p className="muted">Form fields the person fills:</p>
                      {(n.formFields || []).map((f, fi) => (
                        <div className="kv-row" key={`ff-${n.key}-${fi}`}>
                          <input placeholder="field key (e.g. decision)" value={f.key || ''} onChange={(ev) => patchNode(n.key, { formFields: (n.formFields || []).map((x, i) => i === fi ? { ...x, key: ev.target.value } : x) })} />
                          <select value={f.type || 'string'} onChange={(ev) => patchNode(n.key, { formFields: (n.formFields || []).map((x, i) => i === fi ? { ...x, type: ev.target.value } : x) })}>
                            <option value="string">string</option>
                            <option value="number">number</option>
                            <option value="enum">enum</option>
                          </select>
                          <input placeholder="enum values csv" value={f.values || ''} onChange={(ev) => patchNode(n.key, { formFields: (n.formFields || []).map((x, i) => i === fi ? { ...x, values: ev.target.value } : x) })} />
                          <label className="toggle"><input type="checkbox" checked={!!f.required} onChange={(ev) => patchNode(n.key, { formFields: (n.formFields || []).map((x, i) => i === fi ? { ...x, required: ev.target.checked } : x) })} /><span>required</span></label>
                          <button type="button" className="secondary-button" onClick={() => patchNode(n.key, { formFields: (n.formFields || []).filter((_, i) => i !== fi) })}>×</button>
                        </div>
                      ))}
                      <button type="button" className="secondary-button" onClick={() => patchNode(n.key, { formFields: [...(n.formFields || []), { key: '', type: 'string' }] })}>Add form field</button>

                      <p className="muted">Where it goes next (rule → step; use <span className="mono">default</span> for the fallback):</p>
                      {(n.edges || []).map((ed, ei) => (
                        <div className="kv-row" key={`edge-${n.key}-${ei}`}>
                          <input placeholder="when (e.g. decision == 'reject')" value={ed.when || ''} onChange={(ev) => patchNode(n.key, { edges: (n.edges || []).map((x, i) => i === ei ? { ...x, when: ev.target.value } : x) })} />
                          <select value={ed.to || ''} onChange={(ev) => patchNode(n.key, { edges: (n.edges || []).map((x, i) => i === ei ? { ...x, to: ev.target.value } : x) })}>
                            <option value="">to…</option>
                            {nodeIds.map((id) => <option key={id} value={id}>{id}</option>)}
                          </select>
                          <button type="button" className="secondary-button" onClick={() => patchNode(n.key, { edges: (n.edges || []).filter((_, i) => i !== ei) })}>×</button>
                        </div>
                      ))}
                      <button type="button" className="secondary-button" onClick={() => patchNode(n.key, { edges: [...(n.edges || []), { when: 'default', to: '' }] })}>Add rule → step</button>
                      <Err msg={e.edges} />
                    </div>
                  )}

                  {(n.type === 'llm_decision' || n.type === 'gateway_exclusive') && (
                    <div>
                      <p className="muted">
                        {n.type === 'llm_decision' ? 'Routes (bounded options): edge name, rule, and target step.' : 'Branches: rule → step.'}
                      </p>
                      {(n.type === 'llm_decision' ? (n.routes || []) : (n.edges || [])).map((r, ri) => {
                        const listKey = n.type === 'llm_decision' ? 'routes' : 'edges'
                        const list = n[listKey] || []
                        return (
                          <div className="kv-row" key={`r-${n.key}-${ri}`}>
                            {n.type === 'llm_decision' && (
                              <input placeholder="edge (e.g. AUTO_APPROVE)" value={r.edge || ''} onChange={(ev) => patchNode(n.key, { [listKey]: list.map((x, i) => i === ri ? { ...x, edge: ev.target.value } : x) })} />
                            )}
                            <input placeholder="when (e.g. amount < financeThreshold)" value={r.when || ''} onChange={(ev) => patchNode(n.key, { [listKey]: list.map((x, i) => i === ri ? { ...x, when: ev.target.value } : x) })} />
                            <select value={r.to || ''} onChange={(ev) => patchNode(n.key, { [listKey]: list.map((x, i) => i === ri ? { ...x, to: ev.target.value } : x) })}>
                              <option value="">to…</option>
                              {nodeIds.map((id) => <option key={id} value={id}>{id}</option>)}
                            </select>
                            <button type="button" className="secondary-button" onClick={() => patchNode(n.key, { [listKey]: list.filter((_, i) => i !== ri) })}>×</button>
                          </div>
                        )
                      })}
                      <button type="button" className="secondary-button"
                        onClick={() => n.type === 'llm_decision'
                          ? patchNode(n.key, { routes: [...(n.routes || []), { edge: '', when: 'default', to: '' }] })
                          : patchNode(n.key, { edges: [...(n.edges || []), { when: 'default', to: '' }] })}>
                        {n.type === 'llm_decision' ? 'Add route' : 'Add branch'}
                      </button>
                      <Err msg={e.routes} />
                    </div>
                  )}
                </div>
              )
            })}
            <button type="button" className="secondary-button" onClick={() => setNodes((x) => [...x, { key: uid(), id: '', type: 'human_task' }])}>Add step</button>
          </section>

          {/* Notifications */}
          <section className="panel">
            <h3>6 · Notifications</h3>
            <p className="muted">When something happens → who to email.</p>
            {notifications.map((x, i) => (
              <div className="kv-row" key={`notif-${i}`}>
                <select value={x.on || ''} onChange={(e) => patchList(setNotifications, i, { on: e.target.value })}>
                  <option value="">event…</option>
                  {notifEvents.map((ev) => <option key={ev} value={ev}>{ev}</option>)}
                </select>
                <select value={x.to_role || ''} onChange={(e) => patchList(setNotifications, i, { to_role: e.target.value, to: '' })}>
                  <option value="">to role…</option>
                  {logicalRoles.map((r) => <option key={r} value={r}>{r}</option>)}
                </select>
                <label className="toggle"><input type="checkbox" checked={x.to === 'submitter'} onChange={(e) => patchList(setNotifications, i, { to: e.target.checked ? 'submitter' : '', to_role: '' })} /><span>submitter</span></label>
                <input placeholder="message" value={x.template || ''} onChange={(e) => patchList(setNotifications, i, { template: e.target.value })} />
                <button type="button" className="secondary-button" onClick={() => removeAt(setNotifications, i)}>×</button>
              </div>
            ))}
            <button type="button" className="secondary-button" onClick={() => setNotifications((z) => [...z, { on: '', template: '' }])}>Add notification</button>
          </section>

          <div className="form-footer">
            {feedback && <p className={`feedback ${feedback.type}`}>{feedback.message}</p>}
            <button type="button" className="secondary-button" onClick={validate} disabled={busy}>Validate</button>
            <button type="button" className="primary-button" onClick={publish} disabled={busy || errs.count > 0}>
              {errs.count > 0 ? `Fix ${errs.count} issue(s)` : 'Publish'}
            </button>
          </div>
        </div>

        {/* ---------------- RIGHT: live preview ---------------- */}
        <div className="builder-preview">
          <div className="preview-sticky">
            <h3>Live preview <span className="muted">— scroll to zoom, drag to move</span></h3>
            <FlowDiagram pdd={pdd} height={520} />
            {errs.general.length > 0 && (
              <ul className="issue-list">
                {errs.general.map((g, i) => <li key={`g-${i}`}>{g}</li>)}
              </ul>
            )}
            {validation && (
              <div className={`feedback ${validation.valid ? 'success' : 'error'}`}>
                {validation.valid ? 'Server validation passed ✓' : `Server found ${validation.errors.length} error(s)`}
                {(validation.errors || []).map((er, i) => <div key={`ve-${i}`} className="issue-line">• {er}</div>)}
              </div>
            )}
          </div>
        </div>
      </div>
    </section>
  )
}
