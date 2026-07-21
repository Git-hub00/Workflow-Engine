// services/spa/src/generic.jsx
//
// Generic, PDD-driven UI building blocks (P5, slice 1). These render from a
// process definition instead of hardcoding invoice, so ANY published process can
// be launched from the catalog. Reuses the existing App.css classes for styling.
import { useEffect, useState } from 'react'
import { get, post, upload } from './api'

// data_schema is {fieldName: "string"|"number"} in the PDD. Turn it into the
// fields[] shape DynamicForm renders.
function schemaToFields(dataSchema) {
  if (!dataSchema || typeof dataSchema !== 'object') return []
  return Object.entries(dataSchema).map(([key, type]) => ({
    key,
    type: type === 'number' ? 'number' : 'string',
  }))
}

// Render a set of inputs from a fields spec: [{key, type, values?, required?}].
// Shared by the Start form now and (slice 2) the task form.
export function DynamicForm({ fields, values, onChange, disabled = false }) {
  return (
    <div className="config-field-grid three-columns">
      {fields.map((field) => (
        <label className="config-field" key={field.key}>
          <span>{(field.label || field.key)}{field.required ? ' *' : ''}</span>
          {field.type === 'enum' ? (
            <select
              value={values[field.key] ?? ''}
              disabled={disabled}
              onChange={(event) => onChange(field.key, event.target.value)}
            >
              <option value="">—</option>
              {(field.values || []).map((v) => (
                <option key={v} value={v}>{v}</option>
              ))}
            </select>
          ) : (
            <input
              type={field.type === 'number' ? 'number' : 'text'}
              value={values[field.key] ?? ''}
              disabled={disabled}
              onChange={(event) => onChange(field.key, event.target.value)}
            />
          )}
        </label>
      ))}
    </div>
  )
}

// Renders a human task's action form from its node.form_schema and submits the
// collected values. Used by the Task Inbox (slice 2) for non-quorum tasks.
export function GenericTaskForm({ fields, values, onChange, onSubmit, busy = false }) {
  return (
    <form
      className="reject-reason-form"
      onSubmit={(event) => {
        event.preventDefault()
        onSubmit()
      }}
    >
      <DynamicForm fields={fields} values={values} onChange={onChange} disabled={busy} />
      <button className="primary-button" type="submit" disabled={busy}>
        {busy ? 'Submitting…' : 'Submit'}
      </button>
    </form>
  )
}


// Catalog + generic launcher: pick a published process, fill the form built from
// its data_schema (optionally auto-filled from a PDF), and start a transaction.
export function StartProcess() {
  const [processes, setProcesses] = useState([])
  const [selected, setSelected] = useState('')
  const [pdd, setPdd] = useState(null)
  const [values, setValues] = useState({})
  const [busy, setBusy] = useState(false)
  const [extracting, setExtracting] = useState(false)
  const [feedback, setFeedback] = useState(null)

  useEffect(() => {
    get('/v1/definitions')
      .then((rows) => {
        const list = rows || []
        setProcesses(list)
        setSelected((current) => current || (list[0] ? list[0].process_key : ''))
      })
      .catch((error) => setFeedback({ type: 'error', message: error.message || String(error) }))
  }, [])

  useEffect(() => {
    if (!selected) return
    setPdd(null)
    setValues({})
    setFeedback(null)
    get(`/v1/definitions/${encodeURIComponent(selected)}`)
      .then(setPdd)
      .catch((error) => setFeedback({ type: 'error', message: error.message || String(error) }))
  }, [selected])

  const fields = pdd ? schemaToFields(pdd.data_schema) : []

  function updateValue(key, value) {
    setValues((current) => ({ ...current, [key]: value }))
  }

  async function handleFile(file) {
    if (!file) return
    setExtracting(true)
    setFeedback(null)
    try {
      const form = new FormData()
      form.append('file', file)
      form.append('process_key', selected)
      const result = await upload('/v1/extract', form)
      const extracted = result.fields || {}
      setValues((current) => {
        const next = { ...current }
        for (const key of Object.keys(extracted)) {
          if (extracted[key] !== null && extracted[key] !== undefined) next[key] = String(extracted[key])
        }
        return next
      })
      if (result.error) setFeedback({ type: 'error', message: `Auto-fill partial: ${result.error}` })
    } catch (error) {
      setFeedback({ type: 'error', message: error.message || String(error) })
    } finally {
      setExtracting(false)
    }
  }

  async function submit(event) {
    event.preventDefault()
    setBusy(true)
    setFeedback(null)
    try {
      const data = { ...values }
      for (const field of fields) {
        if (field.type === 'number' && data[field.key] !== undefined && data[field.key] !== '') {
          data[field.key] = Number(data[field.key])
        }
      }
      const result = await post('/v1/transactions', { process_key: selected, data })
      setFeedback({ type: 'success', message: `Started. Transaction ${result.transaction_id}.` })
      setValues({})
    } catch (error) {
      setFeedback({ type: 'error', message: error.message || String(error) })
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="view" aria-labelledby="start-process-heading">
      <div className="view-heading">
        <div>
          <p className="eyebrow">Any process</p>
          <h2 id="start-process-heading">Start a Process</h2>
          <p>Pick a published process and start it — the form is built from its definition.</p>
        </div>
      </div>

      <div className="toolbar">
        <label className="config-field">
          <span>Process</span>
          <select value={selected} onChange={(event) => setSelected(event.target.value)}>
            {processes.length === 0 && <option value="">No published processes</option>}
            {processes.map((p) => (
              <option key={p.process_key} value={p.process_key}>
                {p.process_key} (v{p.latest_version})
              </option>
            ))}
          </select>
        </label>
      </div>

      {selected && !pdd && <div className="loading-panel">Loading definition…</div>}

      {pdd && (
        <form className="config-form" onSubmit={submit}>
          <section className="config-section">
            <div className="config-section-heading">
              <div>
                <h3>Details</h3>
                <p>Fields for <span className="mono">{selected}</span>.</p>
              </div>
            </div>
            {fields.length ? (
              <DynamicForm fields={fields} values={values} onChange={updateValue} />
            ) : (
              <p className="muted">This process defines no data_schema fields.</p>
            )}
          </section>

          <section className="config-section">
            <div className="config-section-heading">
              <div>
                <h3>Upload PDF (optional)</h3>
                <p>Auto-fill the fields from a document, then review before starting.</p>
              </div>
            </div>
            <label className="pdf-dropzone">
              <input
                type="file"
                accept="application/pdf"
                className="pdf-file-input"
                onChange={(event) => handleFile(event.target.files?.[0])}
              />
              {extracting ? 'Extracting fields…' : 'Click to choose a PDF'}
            </label>
          </section>

          <div className="form-footer">
            {feedback && (
              <p className={`feedback ${feedback.type}`} role={feedback.type === 'error' ? 'alert' : 'status'}>
                {feedback.message}
              </p>
            )}
            <button className="primary-button" type="submit" disabled={busy || !selected}>
              {busy ? 'Starting…' : 'Start'}
            </button>
          </div>
        </form>
      )}

      {!selected && feedback && (
        <p className={`feedback ${feedback.type}`}>{feedback.message}</p>
      )}
    </section>
  )
}


// ---- PDD-driven process-flow diagram (slice 3) ----------------------------

const NODE_FILL = {
  start: '#e0e7ff', end: '#e0e7ff', automated: '#ccfbf1', llm_decision: '#ffedd5',
  human_task: '#dcfce7', gateway_exclusive: '#ede9fe', gateway_fork: '#ede9fe',
  gateway_join: '#ede9fe', timer: '#dbeafe',
}
const NODE_STROKE = {
  start: '#6366f1', end: '#6366f1', automated: '#14b8a6', llm_decision: '#f97316',
  human_task: '#22c55e', gateway_exclusive: '#8b5cf6', gateway_fork: '#8b5cf6',
  gateway_join: '#8b5cf6', timer: '#3b82f6',
}

// All outgoing (to, label) edges of a node, across the three PDD edge shapes:
// next (string), edges map {EDGE: node}, edges list [{when, to}].
function edgesOf(node) {
  const out = []
  if (typeof node.next === 'string') out.push({ to: node.next, label: '' })
  const edges = node.edges
  if (edges && !Array.isArray(edges) && typeof edges === 'object') {
    for (const [label, to] of Object.entries(edges)) out.push({ to, label })
  } else if (Array.isArray(edges)) {
    for (const item of edges) if (item && item.to) out.push({ to: item.to, label: item.when || '' })
  }
  return out
}

// Simple layered layout: BFS depth from start = column; stack within a column.
function computeLayout(pdd) {
  const nodes = Array.isArray(pdd.nodes) ? pdd.nodes : []
  const byId = Object.fromEntries(nodes.map((n) => [n.id, n]))
  const start = nodes.find((n) => n.type === 'start') || nodes[0]
  const level = {}
  const queue = []
  if (start) { level[start.id] = 0; queue.push(start.id) }
  while (queue.length) {
    const id = queue.shift()
    const nd = byId[id]
    if (!nd) continue
    for (const { to } of edgesOf(nd)) {
      if (byId[to] && level[to] === undefined) { level[to] = level[id] + 1; queue.push(to) }
    }
  }
  let maxLevel = 0
  for (const n of nodes) if (level[n.id] !== undefined) maxLevel = Math.max(maxLevel, level[n.id])
  for (const n of nodes) if (level[n.id] === undefined) level[n.id] = maxLevel + 1  // unreachable -> last column
  const cols = {}
  for (const n of nodes) (cols[level[n.id]] = cols[level[n.id]] || []).push(n)
  const COLW = 220, ROWH = 92, BW = 168, BH = 56, MX = 24, MY = 34
  const pos = {}
  let maxRows = 1
  for (const lv of Object.keys(cols)) maxRows = Math.max(maxRows, cols[lv].length)
  for (const lv of Object.keys(cols)) {
    cols[lv].forEach((n, idx) => { pos[n.id] = { x: MX + Number(lv) * COLW, y: MY + idx * ROWH } })
  }
  const links = []
  for (const n of nodes) for (const { to, label } of edgesOf(n)) if (pos[to]) links.push({ from: n.id, to, label })
  return {
    nodes, pos, links, BW, BH,
    width: Math.max(MX * 2 + (maxLevel + 1) * COLW + BW, 420),
    height: Math.max(MY * 2 + maxRows * ROWH, 220),
  }
}

// Pure diagram from a PDD object (used by the Builder's live preview). Renders a
// hint until there are nodes to lay out.
export function FlowDiagram({ pdd }) {
  const layout = pdd && Array.isArray(pdd.nodes) && pdd.nodes.length ? computeLayout(pdd) : null
  if (!layout) return <p className="muted">Add a start node and steps to see the diagram.</p>
  return (
    <div className="flow-canvas" style={{ overflowX: 'auto' }}>
      <svg
        viewBox={`0 0 ${layout.width} ${layout.height}`}
        width={layout.width}
        height={layout.height}
        role="img"
        aria-label="Process flow diagram"
      >
        <defs>
          <marker id="bld-arrow" markerWidth="9" markerHeight="7" refX="8" refY="3.5" orient="auto">
            <polygon points="0 0, 9 3.5, 0 7" fill="#94a3b8" />
          </marker>
        </defs>
        {layout.links.map((link, idx) => {
          const a = layout.pos[link.from]
          const b = layout.pos[link.to]
          if (!a || !b) return null
          const x1 = a.x + layout.BW
          const y1 = a.y + layout.BH / 2
          const x2 = b.x
          const y2 = b.y + layout.BH / 2
          const mx = (x1 + x2) / 2
          const d = `M ${x1} ${y1} C ${mx} ${y1}, ${mx} ${y2}, ${x2} ${y2}`
          const label = link.label && link.label.length > 16 ? `${link.label.slice(0, 15)}…` : link.label
          return (
            <g key={`bld-e-${idx}`}>
              <path d={d} fill="none" stroke="#94a3b8" strokeWidth="1.3" markerEnd="url(#bld-arrow)" />
              {label && (
                <text x={mx} y={(y1 + y2) / 2 - 4} textAnchor="middle" fontSize="10" fill="#64748b">{label}</text>
              )}
            </g>
          )
        })}
        {layout.nodes.map((n) => {
          const p = layout.pos[n.id]
          if (!p) return null
          return (
            <g key={n.id}>
              <rect
                x={p.x}
                y={p.y}
                width={layout.BW}
                height={layout.BH}
                rx="12"
                fill={NODE_FILL[n.type] || '#f1f5f9'}
                stroke={NODE_STROKE[n.type] || '#cbd5e1'}
                strokeWidth="1.5"
              />
              <text x={p.x + layout.BW / 2} y={p.y + 24} textAnchor="middle" fontSize="13" fontWeight="600" fill="#1e293b">{n.id}</text>
              <text x={p.x + layout.BW / 2} y={p.y + 42} textAnchor="middle" fontSize="10" fill="#64748b">{n.type}</text>
            </g>
          )
        })}
      </svg>
    </div>
  )
}

export function ProcessFlowDynamic() {
  const [processes, setProcesses] = useState([])
  const [selected, setSelected] = useState('')
  const [pdd, setPdd] = useState(null)
  const [error, setError] = useState('')

  useEffect(() => {
    get('/v1/definitions')
      .then((rows) => {
        const list = rows || []
        setProcesses(list)
        setSelected((current) => current || (list[0] ? list[0].process_key : ''))
      })
      .catch((e) => setError(e.message || String(e)))
  }, [])

  useEffect(() => {
    if (!selected) return
    setPdd(null)
    setError('')
    get(`/v1/definitions/${encodeURIComponent(selected)}`)
      .then(setPdd)
      .catch((e) => setError(e.message || String(e)))
  }, [selected])

  const layout = pdd ? computeLayout(pdd) : null

  return (
    <section className="view" aria-labelledby="flow-heading">
      <div className="view-heading">
        <div>
          <p className="eyebrow">Process definition</p>
          <h2 id="flow-heading">Process Flow</h2>
          <p>The diagram is generated from the selected process&apos;s nodes and edges.</p>
        </div>
      </div>

      <div className="toolbar">
        <label className="config-field">
          <span>Process</span>
          <select value={selected} onChange={(e) => setSelected(e.target.value)}>
            {processes.length === 0 && <option value="">No published processes</option>}
            {processes.map((p) => (
              <option key={p.process_key} value={p.process_key}>{p.process_key} (v{p.latest_version})</option>
            ))}
          </select>
        </label>
      </div>

      {error && <p className="feedback error">{error}</p>}
      {selected && !pdd && !error && <div className="loading-panel">Loading definition…</div>}

      {layout && (
        <div className="flow-canvas" style={{ overflowX: 'auto' }}>
          <svg
            viewBox={`0 0 ${layout.width} ${layout.height}`}
            width={layout.width}
            height={layout.height}
            role="img"
            aria-label={`Flow diagram for ${selected}`}
          >
            <defs>
              <marker id="pf-arrow" markerWidth="9" markerHeight="7" refX="8" refY="3.5" orient="auto">
                <polygon points="0 0, 9 3.5, 0 7" fill="#94a3b8" />
              </marker>
            </defs>
            {layout.links.map((link, idx) => {
              const a = layout.pos[link.from]
              const b = layout.pos[link.to]
              if (!a || !b) return null
              const x1 = a.x + layout.BW
              const y1 = a.y + layout.BH / 2
              const x2 = b.x
              const y2 = b.y + layout.BH / 2
              const mx = (x1 + x2) / 2
              const d = `M ${x1} ${y1} C ${mx} ${y1}, ${mx} ${y2}, ${x2} ${y2}`
              const label = link.label && link.label.length > 16 ? `${link.label.slice(0, 15)}…` : link.label
              return (
                <g key={`edge-${idx}`}>
                  <path d={d} fill="none" stroke="#94a3b8" strokeWidth="1.3" markerEnd="url(#pf-arrow)" />
                  {label && (
                    <text x={mx} y={(y1 + y2) / 2 - 4} textAnchor="middle" fontSize="10" fill="#64748b">
                      {label}
                    </text>
                  )}
                </g>
              )
            })}
            {layout.nodes.map((n) => {
              const p = layout.pos[n.id]
              if (!p) return null
              return (
                <g key={n.id}>
                  <rect
                    x={p.x}
                    y={p.y}
                    width={layout.BW}
                    height={layout.BH}
                    rx="12"
                    fill={NODE_FILL[n.type] || '#f1f5f9'}
                    stroke={NODE_STROKE[n.type] || '#cbd5e1'}
                    strokeWidth="1.5"
                  />
                  <text x={p.x + layout.BW / 2} y={p.y + 24} textAnchor="middle" fontSize="13" fontWeight="600" fill="#1e293b">
                    {n.id}
                  </text>
                  <text x={p.x + layout.BW / 2} y={p.y + 42} textAnchor="middle" fontSize="10" fill="#64748b">
                    {n.type}
                  </text>
                </g>
              )
            })}
          </svg>
        </div>
      )}
    </section>
  )
}
