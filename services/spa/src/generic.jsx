// services/spa/src/generic.jsx
//
// Generic, PDD-driven UI building blocks (P5, slice 1). These render from a
// process definition instead of hardcoding invoice, so ANY published process can
// be launched from the catalog. Reuses the existing App.css classes for styling.
import { useEffect, useMemo, useRef, useState } from 'react'
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
  const COLW = 270, ROWH = 120, BW = 186, BH = 64, MX = 34, MY = 66
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

// Pure diagram from a PDD object.
//
// Optional highlighting (used by the Monitor to show ONE transaction's journey):
//   visited  Set/array of step ids the run actually passed through
//   taken    Set/array of "from>to" edge keys the run actually followed
//   current  the step the run is sitting at right now
//   outcome  'approved' | 'rejected' -> tints the finished step green / red
// Passing none of them keeps the plain design-time look (Builder preview).
export function FlowDiagram({ pdd, height = 520, visited, taken, current, outcome }) {
  const layout = pdd && Array.isArray(pdd.nodes) && pdd.nodes.length ? computeLayout(pdd) : null
  const [view, setView] = useState(null)          // null until we auto-fit
  const drag = useRef(null)
  const boxRef = useRef(null)
  const clamp = (v, a, b) => Math.max(a, Math.min(b, v))

  const vSet = useMemo(() => new Set(visited || []), [visited])
  const tSet = useMemo(() => new Set(taken || []), [taken])
  const highlighting = Boolean(visited || current)

  // Fit the whole graph on first paint (and when the graph changes) so nothing is
  // ever cut off inside the box. Previously the default zoom could push the lower
  // half out of a fixed-height box with no way back except Reset.
  const fitScale = layout
    ? clamp(Math.min(1, (height - 24) / layout.height), 0.25, 1)
    : 1
  useEffect(() => { setView({ s: fitScale, x: 0, y: 0 }) }, [fitScale, layout && layout.width, layout && layout.height])

  // Wheel zoom must be a NON-PASSIVE listener, otherwise the browser ignores
  // preventDefault() and scrolls the PAGE instead of zooming the graph (that was
  // the "the whole page jumps / I had to reload" glitch).
  useEffect(() => {
    const el = boxRef.current
    if (!el) return undefined
    const onWheelNative = (e) => {
      e.preventDefault()
      setView((v) => ({ ...(v || { x: 0, y: 0 }), s: clamp((v ? v.s : 1) * (e.deltaY < 0 ? 1.1 : 0.9), 0.25, 2.5) }))
    }
    el.addEventListener('wheel', onWheelNative, { passive: false })
    return () => el.removeEventListener('wheel', onWheelNative)
  }, [])

  if (!layout) return <p className="muted">Add a start step (and connect steps) to see the diagram.</p>
  const v = view || { s: fitScale, x: 0, y: 0 }

  // Keep the graph inside the box: panning is limited to the scaled size.
  const limitX = Math.max(120, layout.width * v.s * 0.6)
  const limitY = Math.max(120, layout.height * v.s * 0.6)
  const zoom = (f) => setView((c) => ({ ...(c || v), s: clamp((c ? c.s : v.s) * f, 0.25, 2.5) }))
  const onDown = (e) => { drag.current = { x: e.clientX, y: e.clientY, ox: v.x, oy: v.y } }
  const onMove = (e) => {
    if (!drag.current) return
    setView((c) => ({
      ...(c || v),
      x: clamp(drag.current.ox + (e.clientX - drag.current.x), -limitX, limitX),
      y: clamp(drag.current.oy + (e.clientY - drag.current.y), -limitY, limitY),
    }))
  }
  const onUp = () => { drag.current = null }

  // Colours for a highlighted run.
  const END_FILL = { approved: '#dcfce7', rejected: '#fee2e2' }
  const END_STROKE = { approved: '#16a34a', rejected: '#dc2626' }
  const nodeFill = (n) => {
    if (!highlighting) return NODE_FILL[n.type] || '#f1f5f9'
    if (n.id === current && n.type === 'end') return END_FILL[outcome] || '#e0f2fe'
    if (n.id === current) return '#e0f2fe'
    if (vSet.has(n.id)) return NODE_FILL[n.type] || '#f1f5f9'
    return '#f8fafc'                                  // untouched -> pale
  }
  const nodeStroke = (n) => {
    if (!highlighting) return NODE_STROKE[n.type] || '#cbd5e1'
    if (n.id === current && n.type === 'end') return END_STROKE[outcome] || '#0284c7'
    if (n.id === current) return '#0284c7'
    if (vSet.has(n.id)) return NODE_STROKE[n.type] || '#cbd5e1'
    return '#e2e8f0'
  }
  const nodeOpacity = (n) => (highlighting && !vSet.has(n.id) && n.id !== current ? 0.45 : 1)
  const edgeOn = (a, b) => !highlighting || tSet.has(`${a}>${b}`)

  return (
    <div className="flow-canvas" ref={boxRef}
         style={{ height, position: 'relative', overflow: 'hidden' }}>
      <div className="flow-zoom">
        <button type="button" onClick={() => zoom(1.2)} title="Zoom in">+</button>
        <button type="button" onClick={() => zoom(0.8)} title="Zoom out">-</button>
        <button type="button" onClick={() => setView({ s: fitScale, x: 0, y: 0 })} title="Fit the whole flow">Fit</button>
      </div>
      <svg
        width="100%"
        height="100%"
        viewBox={`0 0 ${layout.width} ${layout.height}`}
        preserveAspectRatio="xMidYMid meet"
        role="img"
        aria-label="Process flow diagram"
        style={{ cursor: 'grab', userSelect: 'none' }}
        onMouseDown={onDown}
        onMouseMove={onMove}
        onMouseUp={onUp}
        onMouseLeave={onUp}
      >
        <defs>
          <marker id="bld-arrow" markerWidth="9" markerHeight="7" refX="8" refY="3.5" orient="auto">
            <polygon points="0 0, 9 3.5, 0 7" fill="#94a3b8" />
          </marker>
          <marker id="bld-arrow-on" markerWidth="10" markerHeight="8" refX="8" refY="4" orient="auto">
            <polygon points="0 0, 10 4, 0 8" fill="#2563eb" />
          </marker>
        </defs>
        <g transform={`translate(${v.x} ${v.y}) scale(${v.s})`}>
          {layout.links.map((link, idx) => {
            const a = layout.pos[link.from]
            const b = layout.pos[link.to]
            if (!a || !b) return null
            const BW = layout.BW, BH = layout.BH
            const back = b.x <= a.x        // loop-back / same-column edge
            let d, lx, ly
            if (back) {
              // Arc OVER the top so a return edge (e.g. request_info -> review) is
              // clearly visible instead of hiding straight behind the boxes.
              const sx = a.x + BW / 2, sy = a.y
              const ex = b.x + BW / 2, ey = b.y
              const arc = Math.min(sy, ey) - 42
              d = `M ${sx} ${sy} C ${sx} ${arc}, ${ex} ${arc}, ${ex} ${ey}`
              lx = (sx + ex) / 2; ly = arc - 4
            } else {
              const x1 = a.x + BW, y1 = a.y + BH / 2
              const x2 = b.x, y2 = b.y + BH / 2
              const mx = (x1 + x2) / 2
              d = `M ${x1} ${y1} C ${mx} ${y1}, ${mx} ${y2}, ${x2} ${y2}`
              lx = mx; ly = (y1 + y2) / 2 - 5
            }
            const label = link.label && link.label.length > 18 ? `${link.label.slice(0, 17)}...` : link.label
            const on = edgeOn(link.from, link.to)
            return (
              <g key={`bld-e-${idx}`} opacity={on ? 1 : 0.3}>
                <path d={d} fill="none" stroke={on && highlighting ? '#2563eb' : '#94a3b8'}
                      strokeWidth={on && highlighting ? 2.6 : 1.4}
                      markerEnd={on && highlighting ? 'url(#bld-arrow-on)' : 'url(#bld-arrow)'} />
                {label && (
                  <text x={lx} y={ly} textAnchor="middle" fontSize="10"
                        fill={on && highlighting ? '#1d4ed8' : '#475569'}
                        stroke="#ffffff" strokeWidth="3" paintOrder="stroke">{label}</text>
                )}
              </g>
            )
          })}
          {layout.nodes.map((n) => {
            const p = layout.pos[n.id]
            if (!p) return null
            const isCurrent = n.id === current
            return (
              <g key={n.id} opacity={nodeOpacity(n)}>
                {isCurrent && (
                  <rect x={p.x - 5} y={p.y - 5} width={layout.BW + 10} height={layout.BH + 10} rx="15"
                        fill="none" stroke={nodeStroke(n)} strokeWidth="2.5" opacity="0.45" />
                )}
                <rect x={p.x} y={p.y} width={layout.BW} height={layout.BH} rx="12"
                      fill={nodeFill(n)} stroke={nodeStroke(n)}
                      strokeWidth={isCurrent ? 2.8 : 1.5} />
                <text x={p.x + layout.BW / 2} y={p.y + 26} textAnchor="middle" fontSize="13" fontWeight="600" fill="#1e293b">{n.id}</text>
                <text x={p.x + layout.BW / 2} y={p.y + 44} textAnchor="middle" fontSize="10" fill="#64748b">{n.type}</text>
                {isCurrent && (
                  <text x={p.x + layout.BW / 2} y={p.y - 10} textAnchor="middle" fontSize="10"
                        fontWeight="800" fill={nodeStroke(n)}>
                    {n.type === 'end' ? (outcome === 'rejected' ? 'REJECTED' : 'COMPLETED') : 'NOW HERE'}
                  </text>
                )}
              </g>
            )
          })}
        </g>
      </svg>
    </div>
  )
}


export function ProcessFlowDynamic() {
  // Only the workflows THIS person works on (admins/authors get all of them).
  // Uses /v1/my-processes, NOT /v1/definitions, so the email adapter's use of
  // /v1/definitions stays untouched.
  const [processes, setProcesses] = useState([])
  const [selected, setSelected] = useState('')
  const [pdd, setPdd] = useState(null)
  const [error, setError] = useState('')

  useEffect(() => {
    get('/v1/my-processes')
      .then((res) => {
        const list = (res && res.processes) || []
        setProcesses(list)
        setSelected((current) => current || list[0] || '')
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

  return (
    <section className="view" aria-labelledby="flow-heading">
      <div className="view-heading">
        <div>
          <p className="eyebrow">Process definition</p>
          <h2 id="flow-heading">Process Flow</h2>
          <p>The diagram is built from the selected workflow&apos;s own steps and connections.</p>
        </div>
      </div>

      {/* Side-by-side buttons, exactly like the Builder's workflow strip. */}
      <div className="process-strip" role="tablist" aria-label="Your workflows">
        {processes.map((key) => (
          <button type="button" key={key} role="tab" aria-selected={selected === key}
            className={`process-chip ${selected === key ? 'active' : ''}`}
            onClick={() => setSelected(key)}>{key}</button>
        ))}
        {processes.length === 0 && !error && (
          <span className="muted">You are not assigned to any workflow yet — ask an admin.</span>
        )}
      </div>

      {error && <p className="feedback error">{error}</p>}
      {selected && !pdd && !error && <div className="loading-panel">Loading definition…</div>}
      {pdd && <FlowDiagram pdd={pdd} height={560} />}
    </section>
  )
}
