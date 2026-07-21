// services/spa/src/builder.jsx
//
// Process Builder (design-time) — a FORM-BASED PDD creator/editor with a live
// flow diagram. The author fills structured panels; this assembles the PDD JSON
// and uses the P3 Definition Service (/v1/definitions/validate, /v1/definitions).
// No engine change: it just produces a valid PDD and publishes a new version.
import { useMemo, useState } from 'react'
import { post } from './api'
import { FlowDiagram } from './generic'

const NODE_TYPES = ['start', 'automated', 'llm_decision', 'human_task', 'timer', 'end']

let _seq = 0
const uid = () => `k${++_seq}`

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

  // --- state helpers ------------------------------------------------------
  const patchNode = (key, patch) => setNodes((ns) => ns.map((n) => (n.key === key ? { ...n, ...patch } : n)))
  const patchList = (setter, idx, patch) => setter((rows) => rows.map((r, i) => (i === idx ? { ...r, ...patch } : r)))
  const removeAt = (setter, idx) => setter((rows) => rows.filter((_, i) => i !== idx))

  // --- assemble the PDD ---------------------------------------------------
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
      if (n.type === 'start' || n.type === 'timer') {
        if (n.next) node.next = n.next
      } else if (n.type === 'automated') {
        node.action = n.action || ''
        if (n.next) node.next = n.next
      } else if (n.type === 'end') {
        node.outcome = n.outcome || 'completed'
      } else if (n.type === 'human_task') {
        node.assignment = { role: n.role || '' }
        const fields = (n.formFields || []).filter((f) => f.key).map((f) => {
          const field = { key: f.key, type: f.type || 'string', required: !!f.required }
          if (f.type === 'enum' && f.values) {
            field.values = f.values.split(',').map((s) => s.trim()).filter(Boolean)
          }
          return field
        })
        if (fields.length) node.form_schema = { fields }
        if (n.quorumN && n.quorumOf) {
          node.completion = {
            mode: 'quorum', n: Number(n.quorumN), of: Number(n.quorumOf),
            rejectShortCircuits: !!n.rejectShortCircuits,
          }
        }
        if (n.slaHours) node.timeout = { slaHours: Number(n.slaHours) }
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

  const pdd = useMemo(
    buildPdd,
    [processKey, mailbox, roles, configRows, dataFields, nodes, notifications],
  )

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

  const logicalRoles = roles.map((r) => r.logical).filter(Boolean)

  return (
    <section className="view" aria-labelledby="builder-heading">
      <div className="view-heading">
        <div>
          <p className="eyebrow">Design time</p>
          <h2 id="builder-heading">Process Builder</h2>
          <p>Create a process visually — the diagram and PDD update as you type; then Validate and Publish.</p>
        </div>
      </div>

      <div className="config-form">
        {/* Basics */}
        <section className="config-section">
          <div className="config-section-heading"><div><h3>Basics</h3></div></div>
          <div className="config-field-grid three-columns">
            <label className="config-field"><span>Process key</span>
              <input type="text" value={processKey} placeholder="e.g. purchase_order" onChange={(e) => setProcessKey(e.target.value)} /></label>
            <label className="config-field"><span>Mailbox (optional)</span>
              <input type="text" value={mailbox} placeholder="e.g. procurement" onChange={(e) => setMailbox(e.target.value)} /></label>
          </div>
        </section>

        {/* Roles */}
        <section className="config-section">
          <div className="config-section-heading"><div><h3>Roles</h3><p>Map a logical name to a Keycloak realm role.</p></div></div>
          {roles.map((r, i) => (
            <div className="kv-row" key={`role-${i}`}>
              <input type="text" placeholder="logical (e.g. manager)" value={r.logical} onChange={(e) => patchList(setRoles, i, { logical: e.target.value })} />
              <input type="text" placeholder="keycloak role (e.g. ap_manager)" value={r.kc} onChange={(e) => patchList(setRoles, i, { kc: e.target.value })} />
              <button type="button" className="secondary-button" onClick={() => removeAt(setRoles, i)}>Remove</button>
            </div>
          ))}
          <button type="button" className="secondary-button" onClick={() => setRoles((x) => [...x, { logical: '', kc: '' }])}>Add role</button>
        </section>

        {/* Config */}
        <section className="config-section">
          <div className="config-section-heading"><div><h3>Config values</h3><p>Named numbers/strings your rules can reference (e.g. financeThreshold = 5000).</p></div></div>
          {configRows.map((c, i) => (
            <div className="kv-row" key={`cfg-${i}`}>
              <input type="text" placeholder="key" value={c.key} onChange={(e) => patchList(setConfigRows, i, { key: e.target.value })} />
              <input type="text" placeholder="value" value={c.value ?? ''} onChange={(e) => patchList(setConfigRows, i, { value: e.target.value })} />
              <button type="button" className="secondary-button" onClick={() => removeAt(setConfigRows, i)}>Remove</button>
            </div>
          ))}
          <button type="button" className="secondary-button" onClick={() => setConfigRows((x) => [...x, { key: '', value: '' }])}>Add config value</button>
        </section>

        {/* Data fields */}
        <section className="config-section">
          <div className="config-section-heading"><div><h3>Data fields</h3><p>Fields a request carries (drives the Start form).</p></div></div>
          {dataFields.map((f, i) => (
            <div className="kv-row" key={`data-${i}`}>
              <input type="text" placeholder="field name" value={f.name} onChange={(e) => patchList(setDataFields, i, { name: e.target.value })} />
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
        <section className="config-section">
          <div className="config-section-heading"><div><h3>Steps</h3><p>The process nodes. Connect them by choosing target steps.</p></div></div>
          {nodes.map((n) => (
            <div className="task-card" key={n.key}>
              <div className="config-field-grid three-columns">
                <label className="config-field"><span>Step id</span>
                  <input type="text" value={n.id} onChange={(e) => patchNode(n.key, { id: e.target.value })} /></label>
                <label className="config-field"><span>Type</span>
                  <select value={n.type} onChange={(e) => patchNode(n.key, { type: e.target.value })}>
                    {NODE_TYPES.map((t) => <option key={t} value={t}>{t}</option>)}
                  </select></label>
                <div className="config-field" style={{ justifyContent: 'flex-end' }}>
                  <button type="button" className="secondary-button" onClick={() => removeAt(setNodes, nodes.indexOf(n))}>Remove step</button>
                </div>
              </div>

              {(n.type === 'start' || n.type === 'timer') && (
                <label className="config-field"><span>Next step</span>
                  <select value={n.next || ''} onChange={(e) => patchNode(n.key, { next: e.target.value })}>
                    <option value="">—</option>
                    {nodeIds.map((id) => <option key={id} value={id}>{id}</option>)}
                  </select></label>
              )}

              {n.type === 'automated' && (
                <div className="config-field-grid three-columns">
                  <label className="config-field"><span>Action</span>
                    <input type="text" value={n.action || ''} placeholder="extract_fields / post_to_erp" onChange={(e) => patchNode(n.key, { action: e.target.value })} /></label>
                  <label className="config-field"><span>Next step</span>
                    <select value={n.next || ''} onChange={(e) => patchNode(n.key, { next: e.target.value })}>
                      <option value="">—</option>
                      {nodeIds.map((id) => <option key={id} value={id}>{id}</option>)}
                    </select></label>
                </div>
              )}

              {n.type === 'end' && (
                <label className="config-field"><span>Outcome</span>
                  <input type="text" value={n.outcome || ''} placeholder="approved / rejected" onChange={(e) => patchNode(n.key, { outcome: e.target.value })} /></label>
              )}

              {n.type === 'human_task' && (
                <div>
                  <div className="config-field-grid three-columns">
                    <label className="config-field"><span>Role</span>
                      <select value={n.role || ''} onChange={(e) => patchNode(n.key, { role: e.target.value })}>
                        <option value="">—</option>
                        {logicalRoles.map((r) => <option key={r} value={r}>{r}</option>)}
                      </select></label>
                    <label className="config-field"><span>SLA hours</span>
                      <input type="number" value={n.slaHours || ''} onChange={(e) => patchNode(n.key, { slaHours: e.target.value })} /></label>
                    <label className="config-field"><span>Quorum N of M (optional)</span>
                      <span style={{ display: 'flex', gap: '6px' }}>
                        <input type="number" style={{ width: '60px' }} value={n.quorumN || ''} placeholder="N" onChange={(e) => patchNode(n.key, { quorumN: e.target.value })} />
                        <input type="number" style={{ width: '60px' }} value={n.quorumOf || ''} placeholder="M" onChange={(e) => patchNode(n.key, { quorumOf: e.target.value })} />
                      </span></label>
                  </div>

                  <p className="muted">Form fields the person fills:</p>
                  {(n.formFields || []).map((f, fi) => (
                    <div className="kv-row" key={`ff-${n.key}-${fi}`}>
                      <input type="text" placeholder="field key (e.g. decision)" value={f.key || ''} onChange={(e) => patchNode(n.key, { formFields: (n.formFields || []).map((x, i) => i === fi ? { ...x, key: e.target.value } : x) })} />
                      <select value={f.type || 'string'} onChange={(e) => patchNode(n.key, { formFields: (n.formFields || []).map((x, i) => i === fi ? { ...x, type: e.target.value } : x) })}>
                        <option value="string">string</option>
                        <option value="number">number</option>
                        <option value="enum">enum</option>
                      </select>
                      <input type="text" placeholder="enum values csv" value={f.values || ''} onChange={(e) => patchNode(n.key, { formFields: (n.formFields || []).map((x, i) => i === fi ? { ...x, values: e.target.value } : x) })} />
                      <label className="toggle"><input type="checkbox" checked={!!f.required} onChange={(e) => patchNode(n.key, { formFields: (n.formFields || []).map((x, i) => i === fi ? { ...x, required: e.target.checked } : x) })} /><span>required</span></label>
                      <button type="button" className="secondary-button" onClick={() => patchNode(n.key, { formFields: (n.formFields || []).filter((_, i) => i !== fi) })}>×</button>
                    </div>
                  ))}
                  <button type="button" className="secondary-button" onClick={() => patchNode(n.key, { formFields: [...(n.formFields || []), { key: '', type: 'string' }] })}>Add form field</button>

                  <p className="muted">Where it goes next (rule → step). Use <span className="mono">default</span> for the fallback.</p>
                  {(n.edges || []).map((e, ei) => (
                    <div className="kv-row" key={`edge-${n.key}-${ei}`}>
                      <input type="text" placeholder="when (e.g. decision == 'reject')" value={e.when || ''} onChange={(ev) => patchNode(n.key, { edges: (n.edges || []).map((x, i) => i === ei ? { ...x, when: ev.target.value } : x) })} />
                      <select value={e.to || ''} onChange={(ev) => patchNode(n.key, { edges: (n.edges || []).map((x, i) => i === ei ? { ...x, to: ev.target.value } : x) })}>
                        <option value="">to…</option>
                        {nodeIds.map((id) => <option key={id} value={id}>{id}</option>)}
                      </select>
                      <button type="button" className="secondary-button" onClick={() => patchNode(n.key, { edges: (n.edges || []).filter((_, i) => i !== ei) })}>×</button>
                    </div>
                  ))}
                  <button type="button" className="secondary-button" onClick={() => patchNode(n.key, { edges: [...(n.edges || []), { when: 'default', to: '' }] })}>Add rule → step</button>
                </div>
              )}

              {n.type === 'llm_decision' && (
                <div>
                  <p className="muted">Routes (bounded options). Each: an edge name, a rule, and the step it leads to.</p>
                  {(n.routes || []).map((r, ri) => (
                    <div className="kv-row" key={`route-${n.key}-${ri}`}>
                      <input type="text" placeholder="edge (e.g. AUTO_APPROVE)" value={r.edge || ''} onChange={(e) => patchNode(n.key, { routes: (n.routes || []).map((x, i) => i === ri ? { ...x, edge: e.target.value } : x) })} />
                      <input type="text" placeholder="when (e.g. amount < 1000)" value={r.when || ''} onChange={(e) => patchNode(n.key, { routes: (n.routes || []).map((x, i) => i === ri ? { ...x, when: e.target.value } : x) })} />
                      <select value={r.to || ''} onChange={(e) => patchNode(n.key, { routes: (n.routes || []).map((x, i) => i === ri ? { ...x, to: e.target.value } : x) })}>
                        <option value="">to…</option>
                        {nodeIds.map((id) => <option key={id} value={id}>{id}</option>)}
                      </select>
                      <button type="button" className="secondary-button" onClick={() => patchNode(n.key, { routes: (n.routes || []).filter((_, i) => i !== ri) })}>×</button>
                    </div>
                  ))}
                  <button type="button" className="secondary-button" onClick={() => patchNode(n.key, { routes: [...(n.routes || []), { edge: '', when: 'default', to: '' }] })}>Add route</button>
                </div>
              )}
            </div>
          ))}
          <button type="button" className="secondary-button" onClick={() => setNodes((x) => [...x, { key: uid(), id: '', type: 'human_task' }])}>Add step</button>
        </section>

        {/* Notifications */}
        <section className="config-section">
          <div className="config-section-heading"><div><h3>Notifications</h3><p>When something happens → who to email.</p></div></div>
          {notifications.map((x, i) => (
            <div className="kv-row" key={`notif-${i}`}>
              <input type="text" placeholder="on (task_created:manager / completed:approved)" value={x.on || ''} onChange={(e) => patchList(setNotifications, i, { on: e.target.value })} />
              <input type="text" placeholder="to_role (logical) " value={x.to_role || ''} onChange={(e) => patchList(setNotifications, i, { to_role: e.target.value })} />
              <input type="text" placeholder="to (e.g. submitter)" value={x.to || ''} onChange={(e) => patchList(setNotifications, i, { to: e.target.value })} />
              <input type="text" placeholder="template" value={x.template || ''} onChange={(e) => patchList(setNotifications, i, { template: e.target.value })} />
              <button type="button" className="secondary-button" onClick={() => removeAt(setNotifications, i)}>×</button>
            </div>
          ))}
          <button type="button" className="secondary-button" onClick={() => setNotifications((z) => [...z, { on: '', template: '' }])}>Add notification</button>
        </section>

        {/* Live diagram */}
        <section className="config-section">
          <div className="config-section-heading"><div><h3>Live preview</h3></div></div>
          <FlowDiagram pdd={pdd} />
        </section>

        {/* Validation results */}
        {validation && (
          <section className="config-section">
            <div className="config-section-heading"><div><h3>Validation</h3></div></div>
            <p className={`feedback ${validation.valid ? 'success' : 'error'}`}>
              {validation.valid ? 'Valid ✓' : `Invalid — ${validation.errors.length} error(s)`}
            </p>
            {(validation.errors || []).map((e, i) => <p key={`ve-${i}`} className="feedback error">• {e}</p>)}
            {(validation.warnings || []).map((w, i) => <p key={`vw-${i}`} className="muted">• {w}</p>)}
          </section>
        )}

        <div className="form-footer">
          {feedback && <p className={`feedback ${feedback.type}`}>{feedback.message}</p>}
          <button type="button" className="secondary-button" onClick={validate} disabled={busy}>Validate</button>
          <button type="button" className="primary-button" onClick={publish} disabled={busy || !processKey}>Publish</button>
        </div>
      </div>
    </section>
  )
}
