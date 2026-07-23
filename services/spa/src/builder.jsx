// services/spa/src/builder.jsx
//
// Process Builder (design-time) — GUIDED, plain-English redesign.
//
// Left  = a vertical list of "step cards" in friendly language (Approval,
//         Decision, Collect info, Automatic step, Wait, Finish). The engine's
//         `start` node is created automatically. Roles are picked from a live
//         dropdown of what the admin created. Config supports list values (chips).
// Right = the live, zoomable/pannable flow diagram, rebuilt as you edit.
//
// Everything assembles into a PDD and publishes via the Definition Service
// (/v1/definitions[/validate]). No engine change.
import { useEffect, useMemo, useState } from 'react'
import { get, post } from './api'
import { FlowDiagram } from './generic'

let _seq = 0
const uid = () => `u${++_seq}`

// Reverse of buildPdd: turn a published PDD back into the guided-form state so
// the single active process is EDITABLE anytime. Generic — any workflow.
const OP_RE = /^\s*([A-Za-z_]\w*)\s*(<=|>=|<|>|==|!=|not in|in)\s*(.+?)\s*$/
function _parseRule(when, to) {
  const w = String(when || '').trim()
  if (w === 'has_missing' || w === 'anomaly') return { id: uid(), field: w, op: '>=', value: '', to }
  const m = w.match(OP_RE)
  if (m) {
    const val = m[3].trim().replace(/^'(.*)'$/, '$1').replace(/^"(.*)"$/, '$1')
    return { id: uid(), field: m[1], op: m[2], value: val, to }
  }
  return { id: uid(), field: w, op: '>=', value: '', to }
}
function pddToState(pdd) {
  const config = Object.entries(pdd.config || {}).map(([key, v]) => (
    Array.isArray(v) ? { id: uid(), key, kind: 'list', value: '', items: v }
      : typeof v === 'number' ? { id: uid(), key, kind: 'number', value: String(v), items: [] }
        : { id: uid(), key, kind: 'text', value: String(v), items: [] }
  ))
  const dataFields = Object.entries(pdd.data_schema || {}).map(([name, type]) => ({ id: uid(), name, type }))
  const steps = []
  for (const node of pdd.nodes || []) {
    if (node.type === 'start') continue
    const base = { key: uid(), name: node.id }
    const to = node.timeout || {}
    if (node.type === 'automated') steps.push({ ...base, kind: 'automatic', action: node.action || '', next: node.next || '' })
    else if (node.type === 'timer') steps.push({ ...base, kind: 'wait', hours: to.seconds ? String(to.seconds / 3600) : '', next: node.next || '' })
    else if (node.type === 'end') steps.push({ ...base, kind: 'finish', outcome: node.outcome || '' })
    else if (node.type === 'llm_decision') {
      const edges = node.edges || {}
      const rules = []
      let otherwiseTo = ''
      for (const r of node.routes || []) {
        const tgt = edges[r.edge] || ''
        if (String(r.when || '').trim() === 'default' || r.edge === 'OTHERWISE') { otherwiseTo = tgt; continue }
        rules.push(_parseRule(r.when, tgt))
      }
      steps.push({ ...base, kind: 'decision', rules: rules.length ? rules : [{ id: uid(), field: '', op: '>=', value: '', to: '' }], otherwiseTo })
    } else if (node.type === 'human_task') {
      const role = (node.assignment || {}).role || ''
      if (Array.isArray(node.edges)) {
        const quorum = (node.completion || {}).mode === 'quorum'
        let approveTo = '', rejectTo = ''
        for (const e of node.edges) {
          const w = String(e.when || '').trim()
          if (w === 'quorum_approved' || w === "decision == 'approve'") approveTo = e.to
          else rejectTo = e.to
        }
        steps.push({
          ...base, kind: 'approval', role, completion: quorum ? 'quorum' : 'single',
          quorumN: quorum ? String((node.completion || {}).n || '') : '',
          quorumOf: quorum ? String((node.completion || {}).of || '') : '',
          rejectShortCircuits: !!(node.completion || {}).rejectShortCircuits,
          slaHours: to.slaHours ? String(to.slaHours) : '48', onTimeout: to.on_timeout || 'remind',
          approveTo, rejectTo,
        })
      } else {
        const fields = ((node.form_schema || {}).fields || []).map((f) => f.key)
        steps.push({ ...base, kind: 'collect', role, fields, slaHours: to.slaHours ? String(to.slaHours) : '', next: node.next || '' })
      }
    }
  }
  const notifications = (pdd.notifications || []).map((n) => ({
    id: uid(), on: n.on || '', target: n.to === 'submitter' ? 'submitter' : (n.to_role || ''), template: n.template || '',
  }))
  return { processKey: pdd.process_key || '', mailbox: pdd.mailbox || '', config, dataFields, steps, notifications }
}

const KIND_META = {
  automatic: { label: 'Automatic step', badge: 'auto', hint: 'Runs a built-in action (extract data, post to system of record).' },
  decision: { label: 'Decision', badge: 'dec', hint: 'Sends the request different ways based on rules.' },
  approval: { label: 'Approval', badge: 'appr', hint: 'A person — or a quorum — approves or rejects.' },
  collect: { label: 'Collect info', badge: 'coll', hint: 'Ask the submitter for missing details, then continue.' },
  wait: { label: 'Wait', badge: 'wait', hint: 'Pause for a set amount of time.' },
  finish: { label: 'Finish', badge: 'fin', hint: 'Ends the process with an outcome.' },
}
const KIND_ORDER = ['approval', 'decision', 'collect', 'automatic', 'wait', 'finish']
const ACTIONS = ['extract_fields', 'post_to_erp']
const TIMEOUT_ACTIONS = ['remind', 'escalate', 'auto_approve', 'auto_reject']
const OPS = ['<', '<=', '>', '>=', '==', '!=', 'in', 'not in']

function Err({ msg }) {
  return msg ? <span className="field-error">{msg}</span> : null
}

function Chips({ items, onChange, placeholder = 'add value…' }) {
  const [t, setT] = useState('')
  const add = () => {
    const v = t.trim()
    if (v && !(items || []).includes(v)) onChange([...(items || []), v])
    setT('')
  }
  return (
    <div className="chips">
      {(items || []).map((it, i) => (
        <span className="chip" key={`${it}-${i}`}>{it}
          <button type="button" onClick={() => onChange(items.filter((_, j) => j !== i))}>×</button>
        </span>
      ))}
      <input value={t} placeholder={placeholder} onChange={(e) => setT(e.target.value)}
        onKeyDown={(e) => { if (e.key === 'Enter') { e.preventDefault(); add() } }} />
      <button type="button" className="link-btn" onClick={add}>add</button>
    </div>
  )
}

export function ProcessBuilder() {
  const [processKey, setProcessKey] = useState('')
  const [mailbox, setMailbox] = useState('')
  const [roles, setRoles] = useState([])
  const [config, setConfig] = useState([])
  const [dataFields, setDataFields] = useState([])
  const [steps, setSteps] = useState([])
  const [notifications, setNotifications] = useState([])
  const [validation, setValidation] = useState(null)
  const [feedback, setFeedback] = useState(null)
  const [busy, setBusy] = useState(false)

  const [loadedKey, setLoadedKey] = useState(null)

  useEffect(() => { get('/v1/roles').then((r) => setRoles(Array.isArray(r) ? r : [])).catch(() => setRoles([])) }, [])

  // One workflow at a time: load the active process so it's editable. The author
  // edits this and re-publishes (publishing updates the single active process).
  useEffect(() => {
    get('/v1/active-process').then((pdd) => {
      if (pdd && Array.isArray(pdd.nodes) && pdd.nodes.length) {
        const s = pddToState(pdd)
        setProcessKey(s.processKey); setMailbox(s.mailbox); setConfig(s.config)
        setDataFields(s.dataFields); setSteps(s.steps); setNotifications(s.notifications)
        setLoadedKey(s.processKey)
      }
    }).catch(() => {})
  }, [])

  function newBlank() {
    setProcessKey(''); setMailbox(''); setConfig([]); setDataFields([]); setSteps([]); setNotifications([])
    setValidation(null); setFeedback(null); setLoadedKey(null)
  }

  // ---- derived option lists --------------------------------------------
  const stepNames = steps.map((s) => s.name).filter(Boolean)
  const goTo = (selfName) => stepNames.filter((n) => n !== selfName)
  const fieldNames = dataFields.map((f) => f.name).filter(Boolean)
  const configKeys = config.map((c) => c.key).filter(Boolean)
  const fieldOptions = [
    ...fieldNames.map((n) => ({ v: n, label: n })),
    ...configKeys.map((k) => ({ v: k, label: `${k} (config)` })),
    { v: 'has_missing', label: 'has missing required fields' },
    { v: 'anomaly', label: 'flagged as anomaly' },
  ]
  const isBool = (f) => f === 'has_missing' || f === 'anomaly'
  const notifEvents = Array.from(new Set([
    ...steps.filter((s) => (s.kind === 'approval' || s.kind === 'collect') && s.name).map((s) => `task_created:${s.name}`),
    ...steps.filter((s) => s.kind === 'finish' && s.outcome).map((s) => `completed:${s.outcome}`),
  ]))

  // ---- state helpers ----------------------------------------------------
  const patchStep = (key, patch) => setSteps((ss) => ss.map((s) => (s.key === key ? { ...s, ...patch } : s)))
  const patchRow = (setter, id, patch) => setter((rows) => rows.map((r) => (r.id === id ? { ...r, ...patch } : r)))
  const removeRow = (setter, id) => setter((rows) => rows.filter((r) => r.id !== id))

  const uniqName = (base) => {
    const used = new Set(steps.map((s) => s.name))
    if (!used.has(base)) return base
    let i = 2
    while (used.has(base + i)) i++
    return base + i
  }
  function addStep(kind) {
    const base = { automatic: 'step', decision: 'decision', approval: 'approval', collect: 'request_info', wait: 'wait', finish: 'finish' }[kind]
    const name = uniqName(base)
    const defaults = {
      automatic: { action: '', next: '' },
      decision: { rules: [{ id: uid(), field: '', op: '>=', value: '', valueMode: 'literal', to: '' }], otherwiseTo: '' },
      approval: { role: '', completion: 'single', quorumN: '', quorumOf: '', rejectShortCircuits: false, slaHours: '48', onTimeout: 'remind', approveTo: '', rejectTo: '' },
      collect: { role: '', fields: [], slaHours: '', next: '' },
      wait: { hours: '', next: '' },
      finish: { outcome: kind === 'finish' && !steps.some((s) => s.kind === 'finish') ? 'approved' : '' },
    }[kind]
    setSteps((ss) => [...ss, { key: uid(), name, kind, ...defaults }])
  }
  const moveStep = (idx, dir) => setSteps((ss) => {
    const a = [...ss]
    const j = idx + dir
    if (j < 0 || j >= a.length) return a
    const tmp = a[idx]; a[idx] = a[j]; a[j] = tmp
    return a
  })

  // ---- assemble the PDD -------------------------------------------------
  function ruleWhen(r) {
    if (isBool(r.field)) return r.field
    const op = r.op || '=='
    const val = String(r.value ?? '').trim()
    // A known config key or data field is compared as a REFERENCE (unquoted, so
    // "amount < financeThreshold" reads the value); a bare number stays a number;
    // anything else is treated as a quoted string literal ("vendor == 'Acme'").
    const isRef = configKeys.includes(val) || fieldNames.includes(val)
    const isNum = /^-?\d+(\.\d+)?$/.test(val)
    if (op === 'in' || op === 'not in' || isRef || isNum) return `${r.field} ${op} ${val}`
    return `${r.field} ${op} '${val.replace(/'/g, '')}'`
  }
  function buildPdd() {
    const rolesUsed = new Set()
    steps.forEach((s) => { if ((s.kind === 'approval' || s.kind === 'collect') && s.role) rolesUsed.add(s.role) })
    const rolesMap = {}
    rolesUsed.forEach((r) => { rolesMap[r] = r })

    const dataSchema = {}
    dataFields.forEach((f) => { if (f.name) dataSchema[f.name] = f.type || 'string' })

    const cfg = {}
    config.forEach((c) => {
      if (!c.key) return
      if (c.kind === 'list') cfg[c.key] = c.items || []
      else if (c.kind === 'number') cfg[c.key] = Number(String(c.value ?? '').trim() || 0)
      else cfg[c.key] = c.value ?? ''
    })

    const outNodes = [{ id: 'start', type: 'start', ...(stepNames[0] ? { next: stepNames[0] } : {}) }]
    steps.forEach((s) => {
      if (s.kind === 'automatic') {
        outNodes.push({ id: s.name, type: 'automated', action: s.action || '', ...(s.next ? { next: s.next } : {}) })
      } else if (s.kind === 'wait') {
        outNodes.push({ id: s.name, type: 'timer', ...(s.hours ? { timeout: { seconds: Number(s.hours) * 3600 } } : {}), ...(s.next ? { next: s.next } : {}) })
      } else if (s.kind === 'finish') {
        outNodes.push({ id: s.name, type: 'end', outcome: s.outcome || 'completed' })
      } else if (s.kind === 'collect') {
        const fields = (s.fields || []).map((k) => ({ key: k, type: (dataFields.find((d) => d.name === k) || {}).type || 'string', required: true }))
        outNodes.push({
          id: s.name, type: 'human_task', assignment: { role: s.role || '' },
          ...(fields.length ? { form_schema: { fields } } : {}),
          ...(s.slaHours ? { timeout: { slaHours: Number(s.slaHours) } } : {}),
          ...(s.next ? { next: s.next } : {}),
        })
      } else if (s.kind === 'approval') {
        const node = {
          id: s.name, type: 'human_task', assignment: { role: s.role || '' },
          form_schema: { fields: [{ key: 'decision', type: 'enum', values: ['approve', 'reject'], required: true }, { key: 'reason', type: 'string', required: false }] },
        }
        if (s.completion === 'quorum' && s.quorumN && s.quorumOf) {
          node.completion = { mode: 'quorum', n: Number(s.quorumN), of: Number(s.quorumOf), rejectShortCircuits: !!s.rejectShortCircuits }
        }
        const timeout = {}
        if (s.slaHours) timeout.slaHours = Number(s.slaHours)
        if (s.onTimeout && s.onTimeout !== 'remind') timeout.on_timeout = s.onTimeout
        if (Object.keys(timeout).length) node.timeout = timeout
        const edges = []
        if (s.completion === 'quorum') {
          if (s.approveTo) edges.push({ when: 'quorum_approved', to: s.approveTo })
          if (s.rejectTo) edges.push({ when: 'default', to: s.rejectTo })
        } else {
          if (s.approveTo) edges.push({ when: "decision == 'approve'", to: s.approveTo })
          if (s.rejectTo) edges.push({ when: 'default', to: s.rejectTo })
        }
        if (edges.length) node.edges = edges
        outNodes.push(node)
      } else if (s.kind === 'decision') {
        const routes = []
        const edges = {}
        ;(s.rules || []).forEach((r, i) => {
          if (!r.field || !r.to) return
          const edge = r.field === 'has_missing' ? 'REQUEST_INFO' : `R${i + 1}`
          routes.push({ edge, when: ruleWhen(r) })
          edges[edge] = r.to
        })
        if (s.otherwiseTo) { routes.push({ edge: 'OTHERWISE', when: 'default' }); edges.OTHERWISE = s.otherwiseTo }
        outNodes.push({ id: s.name, type: 'llm_decision', routes, edges })
      }
    })

    const pdd = { process_key: processKey, version: 1, roles: rolesMap, nodes: outNodes }
    if (mailbox) pdd.mailbox = mailbox
    if (Object.keys(cfg).length) pdd.config = cfg
    if (Object.keys(dataSchema).length) pdd.data_schema = dataSchema
    const notifs = notifications.filter((x) => x.on).map((x) => ({
      on: x.on,
      ...(x.target === 'submitter' ? { to: 'submitter' } : x.target ? { to_role: x.target } : {}),
      template: x.template || '',
    }))
    if (notifs.length) pdd.notifications = notifs
    return pdd
  }
  const pdd = useMemo(buildPdd, [processKey, mailbox, config, dataFields, steps, notifications])

  // ---- inline validation ------------------------------------------------
  const errs = useMemo(() => {
    const perStep = {}
    const general = []
    if (!processKey.trim()) general.push('Give the process a name (key).')
    if (steps.length === 0) general.push('Add at least one step.')
    if (!steps.some((s) => s.kind === 'finish')) general.push('Add a Finish step.')
    const names = steps.map((s) => s.name)
    steps.forEach((s) => {
      const e = {}
      if (!s.name) e.name = 'Name this step'
      else if (names.filter((n) => n === s.name).length > 1) e.name = 'Duplicate step name'
      if (s.kind === 'automatic') { if (!s.action) e.action = 'Pick an action'; if (!s.next) e.next = 'Choose the next step' }
      if (s.kind === 'wait') { if (!s.hours) e.hours = 'Set the wait time'; if (!s.next) e.next = 'Choose the next step' }
      if (s.kind === 'finish') { if (!s.outcome) e.outcome = 'Set an outcome' }
      if (s.kind === 'collect') {
        if (!s.role) e.role = 'Choose who provides it'
        if (!(s.fields || []).length) e.fields = 'Pick at least one field'
        if (!s.next) e.next = 'Choose the next step'
      }
      if (s.kind === 'approval') {
        if (!s.role) e.role = 'Choose an approver role'
        if (s.completion === 'quorum' && (!s.quorumN || !s.quorumOf)) e.quorum = 'Set N and M'
        if (!s.approveTo) e.approveTo = 'Set where approved goes'
        if (!s.rejectTo) e.rejectTo = 'Set where rejected goes'
      }
      if (s.kind === 'decision') {
        const ok = (s.rules || []).some((r) => r.field && r.to)
        if (!ok && !s.otherwiseTo) e.rules = 'Add a rule → step (or an otherwise target)'
      }
      perStep[s.key] = e
    })
    const count = general.length + steps.reduce((a, s) => a + Object.keys(perStep[s.key] || {}).length, 0)
    return { perStep, general, count }
  }, [processKey, steps, dataFields, config])

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

  const RoleSelect = ({ value, onChange }) => (
    <select value={value || ''} onChange={(e) => onChange(e.target.value)}>
      <option value="">choose role…</option>
      {roles.map((r) => <option key={r} value={r}>{r}</option>)}
      {value && !roles.includes(value) && <option value={value}>{value}</option>}
    </select>
  )
  const StepSelect = ({ value, onChange, selfName, placeholder = 'choose step…' }) => (
    <select value={value || ''} onChange={(e) => onChange(e.target.value)}>
      <option value="">{placeholder}</option>
      {goTo(selfName).map((n) => <option key={n} value={n}>{n}</option>)}
    </select>
  )

  return (
    <section className="view builder" aria-labelledby="builder-heading">
      <div className="view-heading">
        <div>
          <p className="eyebrow">Design time</p>
          <h2 id="builder-heading">Process Builder</h2>
          <p>{loadedKey
            ? `Editing “${loadedKey}”. Change anything and Publish to update the active workflow.`
            : 'Add steps on the left in plain language — the flow draws itself on the right. Fix any red hints, then Publish.'}</p>
        </div>
        <button type="button" className="secondary-button" onClick={newBlank}>New blank process</button>
      </div>

      <div className="builder-layout">
        {/* ================= LEFT: guided config ================= */}
        <div className="builder-config">
          <section className="panel">
            <h3>Process</h3>
            <div className="grid2">
              <label className="config-field"><span>Name (key) *</span>
                <input value={processKey} placeholder="e.g. invoice_approval" onChange={(e) => setProcessKey(e.target.value)} />
                <Err msg={errs.general.includes('Give the process a name (key).') ? 'Required' : null} /></label>
              <label className="config-field"><span>Mailbox (optional)</span>
                <input value={mailbox} placeholder="e.g. invoice" onChange={(e) => setMailbox(e.target.value)} /></label>
            </div>
          </section>

          <section className="panel">
            <div className="panel-head">
              <h3>Steps</h3>
              <span className="muted">{steps.length} step{steps.length === 1 ? '' : 's'} · connect them with the “go to” pickers</span>
            </div>

            {steps.length === 0 && <p className="empty-hint">No steps yet. Add your first step below — the process starts at whichever step is first.</p>}

            {steps.map((s, idx) => {
              const meta = KIND_META[s.kind]
              const e = errs.perStep[s.key] || {}
              return (
                <div className={`step-card kind-${meta.badge}`} key={s.key}>
                  <div className="step-head">
                    <span className={`step-badge ${meta.badge}`}>{meta.label}</span>
                    {idx === 0 && <span className="start-pill">starts here</span>}
                    <input className="step-name" value={s.name} onChange={(ev) => patchStep(s.key, { name: ev.target.value })} />
                    <div className="step-tools">
                      <button type="button" className="icon-btn" title="Move up" disabled={idx === 0} onClick={() => moveStep(idx, -1)}>↑</button>
                      <button type="button" className="icon-btn" title="Move down" disabled={idx === steps.length - 1} onClick={() => moveStep(idx, 1)}>↓</button>
                      <button type="button" className="icon-btn danger" title="Remove" onClick={() => setSteps((ss) => ss.filter((x) => x.key !== s.key))}>×</button>
                    </div>
                  </div>
                  {e.name && <Err msg={e.name} />}
                  <p className="step-hint">{meta.hint}</p>

                  {s.kind === 'automatic' && (
                    <div className="grid2">
                      <label className="config-field"><span>Action</span>
                        <select value={s.action || ''} onChange={(ev) => patchStep(s.key, { action: ev.target.value })}>
                          <option value="">choose…</option>
                          {ACTIONS.map((a) => <option key={a} value={a}>{a}</option>)}
                        </select><Err msg={e.action} /></label>
                      <label className="config-field"><span>Then go to →</span>
                        <StepSelect value={s.next} selfName={s.name} onChange={(v) => patchStep(s.key, { next: v })} /><Err msg={e.next} /></label>
                    </div>
                  )}

                  {s.kind === 'wait' && (
                    <div className="grid2">
                      <label className="config-field"><span>Wait (hours)</span>
                        <input type="number" min="0" value={s.hours || ''} onChange={(ev) => patchStep(s.key, { hours: ev.target.value })} /><Err msg={e.hours} /></label>
                      <label className="config-field"><span>Then go to →</span>
                        <StepSelect value={s.next} selfName={s.name} onChange={(v) => patchStep(s.key, { next: v })} /><Err msg={e.next} /></label>
                    </div>
                  )}

                  {s.kind === 'finish' && (
                    <label className="config-field"><span>Outcome</span>
                      <input value={s.outcome || ''} placeholder="approved / rejected" onChange={(ev) => patchStep(s.key, { outcome: ev.target.value })} /><Err msg={e.outcome} /></label>
                  )}

                  {s.kind === 'collect' && (
                    <div className="editor">
                      <div className="grid2">
                        <label className="config-field"><span>Who provides it</span>
                          <RoleSelect value={s.role} onChange={(v) => patchStep(s.key, { role: v })} /><Err msg={e.role} /></label>
                        <label className="config-field"><span>Time limit (hours, optional)</span>
                          <input type="number" min="0" value={s.slaHours || ''} onChange={(ev) => patchStep(s.key, { slaHours: ev.target.value })} /></label>
                      </div>
                      <span className="mini-label">Fields to ask for</span>
                      <div className="chips-pick">
                        {fieldNames.length === 0 && <span className="muted">Add data fields below first.</span>}
                        {fieldNames.map((n) => {
                          const on = (s.fields || []).includes(n)
                          return (
                            <button type="button" key={n} className={`pick ${on ? 'on' : ''}`}
                              onClick={() => patchStep(s.key, { fields: on ? s.fields.filter((x) => x !== n) : [...(s.fields || []), n] })}>{n}</button>
                          )
                        })}
                      </div>
                      <Err msg={e.fields} />
                      <label className="config-field"><span>Then go to →</span>
                        <StepSelect value={s.next} selfName={s.name} onChange={(v) => patchStep(s.key, { next: v })} /><Err msg={e.next} /></label>
                    </div>
                  )}

                  {s.kind === 'approval' && (
                    <div className="editor">
                      <div className="grid2">
                        <label className="config-field"><span>Who approves</span>
                          <RoleSelect value={s.role} onChange={(v) => patchStep(s.key, { role: v })} /><Err msg={e.role} /></label>
                        <label className="config-field"><span>How many</span>
                          <select value={s.completion || 'single'} onChange={(ev) => patchStep(s.key, { completion: ev.target.value })}>
                            <option value="single">one person</option>
                            <option value="quorum">a quorum (N of M)</option>
                          </select></label>
                      </div>
                      {s.completion === 'quorum' && (
                        <div className="grid3">
                          <label className="config-field"><span>Approvals needed (N)</span>
                            <input type="number" min="1" value={s.quorumN || ''} onChange={(ev) => patchStep(s.key, { quorumN: ev.target.value })} /></label>
                          <label className="config-field"><span>Out of (M)</span>
                            <input type="number" min="1" value={s.quorumOf || ''} onChange={(ev) => patchStep(s.key, { quorumOf: ev.target.value })} /></label>
                          <label className="config-field toggle-field">
                            <input type="checkbox" checked={!!s.rejectShortCircuits} onChange={(ev) => patchStep(s.key, { rejectShortCircuits: ev.target.checked })} />
                            <span>one reject ends it</span></label>
                          <Err msg={e.quorum} />
                        </div>
                      )}
                      <div className="grid2">
                        <label className="config-field"><span>Time limit (hours)</span>
                          <input type="number" min="0" value={s.slaHours || ''} onChange={(ev) => patchStep(s.key, { slaHours: ev.target.value })} /></label>
                        <label className="config-field"><span>If time runs out</span>
                          <select value={s.onTimeout || 'remind'} onChange={(ev) => patchStep(s.key, { onTimeout: ev.target.value })}>
                            {TIMEOUT_ACTIONS.map((t) => <option key={t} value={t}>{t}</option>)}
                          </select></label>
                      </div>
                      <div className="grid2">
                        <label className="config-field"><span>When approved →</span>
                          <StepSelect value={s.approveTo} selfName={s.name} onChange={(v) => patchStep(s.key, { approveTo: v })} /><Err msg={e.approveTo} /></label>
                        <label className="config-field"><span>When rejected →</span>
                          <StepSelect value={s.rejectTo} selfName={s.name} onChange={(v) => patchStep(s.key, { rejectTo: v })} /><Err msg={e.rejectTo} /></label>
                      </div>
                    </div>
                  )}

                  {s.kind === 'decision' && (
                    <div className="editor">
                      {(s.rules || []).map((r) => (
                        <div className="rule-row" key={r.id}>
                          <span className="rule-lead">If</span>
                          <select value={r.field || ''} onChange={(ev) => patchStep(s.key, { rules: s.rules.map((x) => x.id === r.id ? { ...x, field: ev.target.value } : x) })}>
                            <option value="">field…</option>
                            {fieldOptions.map((f) => <option key={f.v} value={f.v}>{f.label}</option>)}
                          </select>
                          {!isBool(r.field) && (
                            <>
                              <select value={r.op || ''} onChange={(ev) => patchStep(s.key, { rules: s.rules.map((x) => x.id === r.id ? { ...x, op: ev.target.value } : x) })}>
                                {OPS.map((o) => <option key={o} value={o}>{o}</option>)}
                              </select>
                              <input className="rule-val" placeholder="value or config key" value={r.value || ''}
                                onChange={(ev) => patchStep(s.key, { rules: s.rules.map((x) => x.id === r.id ? { ...x, value: ev.target.value } : x) })} />
                            </>
                          )}
                          <span className="rule-lead">→</span>
                          <StepSelect value={r.to} selfName={s.name} onChange={(v) => patchStep(s.key, { rules: s.rules.map((x) => x.id === r.id ? { ...x, to: v } : x) })} />
                          <button type="button" className="icon-btn" onClick={() => patchStep(s.key, { rules: s.rules.filter((x) => x.id !== r.id) })}>×</button>
                        </div>
                      ))}
                      <button type="button" className="link-btn" onClick={() => patchStep(s.key, { rules: [...(s.rules || []), { id: uid(), field: '', op: '>=', value: '', valueMode: 'literal', to: '' }] })}>+ Add rule</button>
                      <div className="rule-row otherwise">
                        <span className="rule-lead">Otherwise →</span>
                        <StepSelect value={s.otherwiseTo} selfName={s.name} onChange={(v) => patchStep(s.key, { otherwiseTo: v })} />
                      </div>
                      <Err msg={e.rules} />
                    </div>
                  )}
                </div>
              )
            })}

            <div className="add-step">
              <span className="mini-label">Add a step</span>
              <div className="add-step-btns">
                {KIND_ORDER.map((k) => (
                  <button type="button" key={k} className={`add-btn ${KIND_META[k].badge}`} onClick={() => addStep(k)}>+ {KIND_META[k].label}</button>
                ))}
              </div>
            </div>
          </section>

          {/* Advanced (collapsible) — data, config, notifications */}
          <details className="panel adv" open={dataFields.length > 0}>
            <summary>Data fields <span className="muted">— what a request carries</span></summary>
            {dataFields.map((f) => (
              <div className="cfg-row" key={f.id}>
                <input placeholder="field name (e.g. amount)" value={f.name} onChange={(e) => patchRow(setDataFields, f.id, { name: e.target.value })} />
                <select value={f.type} onChange={(e) => patchRow(setDataFields, f.id, { type: e.target.value })}>
                  <option value="string">string</option>
                  <option value="number">number</option>
                </select>
                <button type="button" className="icon-btn" onClick={() => removeRow(setDataFields, f.id)}>×</button>
              </div>
            ))}
            <button type="button" className="link-btn" onClick={() => setDataFields((x) => [...x, { id: uid(), name: '', type: 'string' }])}>+ Add field</button>
          </details>

          <details className="panel adv">
            <summary>Config values <span className="muted">— thresholds &amp; lists your rules use</span></summary>
            {config.map((c) => (
              <div className="cfg-row wide" key={c.id}>
                <input placeholder="name (e.g. financeThreshold)" value={c.key} onChange={(e) => patchRow(setConfig, c.id, { key: e.target.value })} />
                <select value={c.kind} onChange={(e) => patchRow(setConfig, c.id, { kind: e.target.value })}>
                  <option value="number">number</option>
                  <option value="text">text</option>
                  <option value="list">list</option>
                </select>
                {c.kind === 'list'
                  ? <Chips items={c.items || []} onChange={(items) => patchRow(setConfig, c.id, { items })} />
                  : <input placeholder="value" value={c.value ?? ''} onChange={(e) => patchRow(setConfig, c.id, { value: e.target.value })} />}
                <button type="button" className="icon-btn" onClick={() => removeRow(setConfig, c.id)}>×</button>
              </div>
            ))}
            <button type="button" className="link-btn" onClick={() => setConfig((x) => [...x, { id: uid(), key: '', kind: 'number', value: '', items: [] }])}>+ Add value</button>
          </details>

          <details className="panel adv">
            <summary>Notifications <span className="muted">— when X happens, email who</span></summary>
            {notifications.map((n) => (
              <div className="cfg-row wide" key={n.id}>
                <select value={n.on || ''} onChange={(e) => patchRow(setNotifications, n.id, { on: e.target.value })}>
                  <option value="">event…</option>
                  {notifEvents.map((ev) => <option key={ev} value={ev}>{ev}</option>)}
                </select>
                <select value={n.target || ''} onChange={(e) => patchRow(setNotifications, n.id, { target: e.target.value })}>
                  <option value="">to…</option>
                  <option value="submitter">the submitter</option>
                  {roles.map((r) => <option key={r} value={r}>{r}</option>)}
                </select>
                <input placeholder="message" value={n.template || ''} onChange={(e) => patchRow(setNotifications, n.id, { template: e.target.value })} />
                <button type="button" className="icon-btn" onClick={() => removeRow(setNotifications, n.id)}>×</button>
              </div>
            ))}
            <button type="button" className="link-btn" onClick={() => setNotifications((x) => [...x, { id: uid(), on: '', target: '', template: '' }])}>+ Add notification</button>
          </details>

          <div className="form-footer">
            {feedback && <p className={`feedback ${feedback.type}`}>{feedback.message}</p>}
            <button type="button" className="secondary-button" onClick={validate} disabled={busy}>Validate on server</button>
            <button type="button" className="primary-button" onClick={publish} disabled={busy || errs.count > 0}>
              {errs.count > 0 ? `Fix ${errs.count} issue${errs.count === 1 ? '' : 's'}` : 'Publish'}
            </button>
          </div>
        </div>

        {/* ================= RIGHT: live preview ================= */}
        <div className="builder-preview">
          <div className="preview-sticky">
            <h3>Live preview <span className="muted">— scroll to zoom, drag to move</span></h3>
            <FlowDiagram pdd={pdd} height={520} />
            {errs.general.length > 0 && (
              <ul className="issue-list">{errs.general.map((g, i) => <li key={`g-${i}`}>{g}</li>)}</ul>
            )}
            {validation && (
              <div className={`feedback ${validation.valid ? 'success' : 'error'}`}>
                {validation.valid ? 'Server validation passed ✓' : `Server found ${(validation.errors || []).length} error(s)`}
                {(validation.errors || []).map((er, i) => <div key={`ve-${i}`} className="issue-line">• {er}</div>)}
              </div>
            )}
          </div>
        </div>
      </div>
    </section>
  )
}
