// services/spa/src/generic.jsx
//
// Generic, PDD-driven UI building blocks (P5, slice 1). These render from a
// process definition instead of hardcoding invoice, so ANY published process can
// be launched from the catalog. Reuses the existing App.css classes for styling.
import { useEffect, useRef, useState } from 'react'
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
  const COLW = 270, ROWH = 120, BW = 186, BH = 64, MX = 34, MY = 40
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
export function FlowDiagram({ pdd, height = 520 }) {
  const layout = pdd && Array.isArray(pdd.nodes) && pdd.nodes.length ? computeLayout(pdd) : null
  const [view, setView] = useState({ s: 1, x: 0, y: 0 })
  const drag = useRef(null)
  const clamp = (v, a, b) => Math.max(a, Math.min(b, v))
  if (!layout) return <p className="muted">Add a start step (and connect steps) to see the diagram.</p>

  const zoom = (f) => setView((v) => ({ ...v, s: clamp(v.s * f, 0.3, 2.5) }))
  const onWheel = (e) => { e.preventDefault(); zoom(e.deltaY < 0 ? 1.1 : 0.9) }
  const onDown = (e) => { drag.current = { x: e.clientX, y: e.clientY, ox: view.x, oy: view.y } }
  const onMove = (e) => {
    if (!drag.current) return
    setView((v) => ({ ...v, x: drag.current.ox + (e.clientX - drag.current.x), y: drag.current.oy + (e.clientY - drag.current.y) }))
  }
  const onUp = () => { drag.current = null }

  return (
    <div className="flow-canvas" style={{ height, position: 'relative', overflow: 'hidden' }}>
      <div className="flow-zoom">
        <button type="button" onClick={() => zoom(1.2)} title="Zoom in">+</button>
        <button type="button" onClick={() => zoom(0.8)} title="Zoom out">-</button>
        <button type="button" onClick={() => setView({ s: 1, x: 0, y: 0 })} title="Reset view">Reset</button>
      </div>
      <svg
        width="100%"
        height="100%"
        viewBox={`0 0 ${layout.width} ${layout.height}`}
        preserveAspectRatio="xMidYMid meet"
        role="img"
        aria-label="Process flow diagram"
        style={{ cursor: drag.current ? 'grabbing' : 'grab', userSelect: 'none' }}
        onWheel={onWheel}
        onMouseDown={onDown}
        onMouseMove={onMove}
        onMouseUp={onUp}
        onMouseLeave={onUp}
      >
        <defs>
          <marker id="bld-arrow" markerWidth="9" markerHeight="7" refX="8" refY="3.5" orient="auto">
            <polygon points="0 0, 9 3.5, 0 7" fill="#94a3b8" />
          </marker>
        </defs>
        <g transform={`translate(${view.x} ${view.y}) scale(${view.s})`}>
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
            const label = link.label && link.label.length > 18 ? `${link.label.slice(0, 17)}...` : link.label
            return (
              <g key={`bld-e-${idx}`}>
                <path d={d} fill="none" stroke="#94a3b8" strokeWidth="1.4" markerEnd="url(#bld-arrow)" />
                {label && (
                  <text x={mx} y={(y1 + y2) / 2 - 5} textAnchor="middle" fontSize="10" fill="#475569"
                        stroke="#ffffff" strokeWidth="3" paintOrder="stroke">{label}</text>
                )}
              </g>
            )
          })}
          {layout.nodes.map((n) => {
            const p = layout.pos[n.id]
            if (!p) return null
            return (
              <g key={n.id}>
                <rect x={p.x} y={p.y} width={layout.BW} height={layout.BH} rx="12"
                      fill={NODE_FILL[n.type] || '#f1f5f9'} stroke={NODE_STROKE[n.type] || '#cbd5e1'} strokeWidth="1.5" />
                <text x={p.x + layout.BW / 2} y={p.y + 26} textAnchor="middle" fontSize="13" fontWeight="600" fill="#1e293b">{n.id}</text>
                <text x={p.x + layout.BW / 2} y={p.y + 44} textAnchor="middle" fontSize="10" fill="#64748b">{n.type}</text>
              </g>
            )
          })}
        </g>
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
