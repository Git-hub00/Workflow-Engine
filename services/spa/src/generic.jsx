// services/spa/src/generic.jsx
//
// Generic, PDD-driven UI building blocks (P5, slice 1). These render from a
// process definition instead of hardcoding invoice, so ANY published process can
// be launched from the catalog. Reuses the existing App.css classes for styling.
import { useEffect, useMemo, useRef, useState } from 'react'
import { get } from './api'

// Render a set of inputs from a fields spec: [{key, type, values?, required?}].
function DynamicForm({ fields, values, onChange, disabled = false }) {
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
//   visited  step ids the run actually passed through
//   taken    "from>to" connections the run actually followed
//   current  the step the run is sitting at right now
//   outcome  'approved' | 'rejected' -> tints the finished step green / red
// Passing none of them keeps the plain design-time look (Builder preview).
//
// IMPORTANT design notes (these fixed real bugs):
//  * The layout is MEMOISED. It used to be recalculated on every render, so every
//    mouse-move while dragging re-ran the whole graph layout — that is what made
//    dragging heavy enough to hang/reload the tab.
//  * The "fit" transform is DERIVED, never stored in state. Storing it in state
//    from an effect keyed on a derived value could bounce
//    (state -> render -> new value -> state ...) into React's update-depth crash,
//    which looks exactly like the page reloading itself.
//  * The SVG viewBox matches the BOX IN PIXELS, and the graph is scaled+centred
//    into it. Previously the viewBox was the graph's own size with "meet", so a
//    wide, short graph fitted the width and left the bottom half of the box empty.
//  * The wheel only zooms with SHIFT held; a plain wheel is left alone so the page
//    scrolls normally.
export function FlowDiagram({ pdd, height = 520, visited, taken, inferred, current, outcome, legend }) {
  const layout = useMemo(
    () => (pdd && Array.isArray(pdd.nodes) && pdd.nodes.length ? computeLayout(pdd) : null),
    [pdd],
  )
  const boxRef = useRef(null)
  const [box, setBox] = useState({ w: 0, h: 0 })
  const [view, setView] = useState(null)      // null = follow the auto-fit
  const drag = useRef(null)
  const clamp = (v, a, b) => Math.max(a, Math.min(b, v))

  const vSet = useMemo(() => new Set(visited || []), [visited])
  const tSet = useMemo(() => new Set(taken || []), [taken])
  const iSet = useMemo(() => new Set(inferred || []), [inferred])
  // An EMPTY array is truthy, so `Boolean(visited)` switched highlighting on with
  // nothing to highlight — every step rendered at 40% opacity with no coloured
  // connections, which looks exactly like a broken render. Require real content.
  const highlighting = Boolean((visited && visited.length) || current)

  // Keep the box's pixel size (so the graph can be scaled to fill it).
  useEffect(() => {
    const el = boxRef.current
    if (!el) return undefined
    const measure = () => setBox((b) => {
      const w = el.clientWidth
      const h = el.clientHeight
      return b.w === w && b.h === h ? b : { w, h }
    })
    measure()
    const ro = typeof ResizeObserver !== 'undefined' ? new ResizeObserver(measure) : null
    if (ro) ro.observe(el)
    window.addEventListener('resize', measure)
    return () => {
      if (ro) ro.disconnect()
      window.removeEventListener('resize', measure)
    }
  }, [])

  // Scale + centre the graph so it FILLS the box. Pure maths, no state.
  const fit = useMemo(() => {
    if (!layout || !box.w || !box.h) return { s: 1, x: 0, y: 0 }
    const pad = 18
    const raw = Math.min((box.w - pad * 2) / layout.width, (box.h - pad * 2) / layout.height)
    const s = Math.max(0.15, Math.min(raw, 1.75))
    return { s, x: (box.w - layout.width * s) / 2, y: (box.h - layout.height * s) / 2 }
  }, [layout, box.w, box.h])

  const v = view || fit

  // Keep the graph reachable: it can never be dragged completely out of the box.
  const clampView = (nv) => {
    if (!layout) return nv
    const w = layout.width * nv.s
    const h = layout.height * nv.s
    const slack = 60
    return {
      s: nv.s,
      x: clamp(nv.x, Math.min(0, box.w - w) - slack, Math.max(0, box.w - w) + slack),
      y: clamp(nv.y, Math.min(0, box.h - h) - slack, Math.max(0, box.h - h) + slack),
    }
  }

  // Zoom around the middle of the box.
  const zoomBy = (f) => setView(() => {
    const s = clamp(v.s * f, 0.15, 3)
    const k = s / v.s
    return clampView({ s, x: box.w / 2 - (box.w / 2 - v.x) * k, y: box.h / 2 - (box.h / 2 - v.y) * k })
  })

  // SHIFT + wheel zooms; a plain wheel is ignored so the PAGE scrolls as usual.
  useEffect(() => {
    const el = boxRef.current
    if (!el) return undefined
    const onWheel = (e) => {
      if (!e.shiftKey) return                 // let the page scroll
      e.preventDefault()
      zoomBy(e.deltaY < 0 ? 1.12 : 0.89)
    }
    el.addEventListener('wheel', onWheel, { passive: false })
    return () => el.removeEventListener('wheel', onWheel)
  })

  const onDown = (e) => {
    drag.current = { x: e.clientX, y: e.clientY, ox: v.x, oy: v.y }
    if (e.currentTarget.setPointerCapture && e.pointerId != null) {
      try { e.currentTarget.setPointerCapture(e.pointerId) } catch { /* ignore */ }
    }
  }
  const onMove = (e) => {
    if (!drag.current) return
    const d = drag.current
    setView(() => clampView({ s: v.s, x: d.ox + (e.clientX - d.x), y: d.oy + (e.clientY - d.y) }))
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
    return '#f8fafc'
  }
  const nodeStroke = (n) => {
    if (!highlighting) return NODE_STROKE[n.type] || '#cbd5e1'
    if (n.id === current && n.type === 'end') return END_STROKE[outcome] || '#0284c7'
    if (n.id === current) return '#0284c7'
    if (vSet.has(n.id)) return NODE_STROKE[n.type] || '#cbd5e1'
    return '#e2e8f0'
  }
  const nodeOpacity = (n) => (highlighting && !vSet.has(n.id) && n.id !== current ? 0.4 : 1)
  // 'on'       = proven from the audit trail  -> solid bold blue
  // 'inferred' = filled in to bridge a gap    -> DASHED blue, so a guessed hop is
  //              never presented as fact
  // 'off'      = not part of this run         -> faded grey
  const edgeState = (a, b) => {
    if (!highlighting) return 'on'
    const key = `${a}>${b}`
    if (tSet.has(key)) return 'on'
    if (iSet.has(key)) return 'inferred'
    return 'off'
  }

  return (
    <div className="flow-canvas" ref={boxRef}
         style={{ height, position: 'relative', overflow: 'hidden' }}>
      <div className="flow-zoom">
        <button type="button" onClick={() => zoomBy(1.2)} title="Zoom in">+</button>
        <button type="button" onClick={() => zoomBy(0.8)} title="Zoom out">-</button>
        <button type="button" onClick={() => setView(null)} title="Fit the whole flow">Fit</button>
      </div>

      {!layout && (
        <p className="muted" style={{ padding: 16 }}>Add a start step (and connect steps) to see the diagram.</p>
      )}

      {layout && box.w > 0 && (
        <svg
          width="100%"
          height="100%"
          viewBox={`0 0 ${box.w} ${box.h}`}
          role="img"
          aria-label="Process flow diagram"
          style={{ cursor: 'grab', userSelect: 'none', touchAction: 'none', display: 'block' }}
          onPointerDown={onDown}
          onPointerMove={onMove}
          onPointerUp={onUp}
          onPointerLeave={onUp}
          onPointerCancel={onUp}
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
              const BW = layout.BW
              const BH = layout.BH
              const back = b.x <= a.x
              let d
              let lx
              let ly
              if (back) {
                const sx = a.x + BW / 2
                const sy = a.y
                const ex = b.x + BW / 2
                const ey = b.y
                const arc = Math.min(sy, ey) - 42
                d = `M ${sx} ${sy} C ${sx} ${arc}, ${ex} ${arc}, ${ex} ${ey}`
                lx = (sx + ex) / 2
                ly = arc - 4
              } else {
                const x1 = a.x + BW
                const y1 = a.y + BH / 2
                const x2 = b.x
                const y2 = b.y + BH / 2
                const mx = (x1 + x2) / 2
                d = `M ${x1} ${y1} C ${mx} ${y1}, ${mx} ${y2}, ${x2} ${y2}`
                lx = mx
                ly = (y1 + y2) / 2 - 5
              }
              const label = link.label && link.label.length > 18 ? `${link.label.slice(0, 17)}...` : link.label
              const state = edgeState(link.from, link.to)
              const lit = highlighting && state !== 'off'
              return (
                <g key={`bld-e-${idx}`} opacity={state === 'off' ? 0.3 : 1}>
                  <path d={d} fill="none" stroke={lit ? '#2563eb' : '#94a3b8'}
                        strokeWidth={lit ? 2.6 : 1.4}
                        strokeDasharray={state === 'inferred' ? '6 4' : undefined}
                        markerEnd={lit ? 'url(#bld-arrow-on)' : 'url(#bld-arrow)'} />
                  {label && (
                    <text x={lx} y={ly} textAnchor="middle" fontSize="10"
                          fill={lit ? '#1d4ed8' : '#475569'}
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
                  <text x={p.x + layout.BW / 2} y={p.y + 26} textAnchor="middle" fontSize="13"
                        fontWeight="600" fill="#1e293b">{n.id}</text>
                  <text x={p.x + layout.BW / 2} y={p.y + 44} textAnchor="middle" fontSize="10"
                        fill="#64748b">{n.type}</text>
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
      )}

      {legend && <div className="flow-legend-corner">{legend}</div>}
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
    let alive = true
    get('/v1/my-processes')
      .then((res) => {
        if (!alive) return
        const list = (res && res.processes) || []
        setProcesses(list)
        setSelected((current) => current || list[0] || '')
      })
      .catch((e) => { if (alive) setError(e.message || String(e)) })
    return () => { alive = false }
  }, [])

  // `alive` guard: switching chips quickly (or leaving the tab, which unmounts this
  // view) could let an OLDER definition resolve last and be displayed under the
  // newly highlighted chip, and set state after unmount.
  useEffect(() => {
    if (!selected) return undefined
    let alive = true
    setPdd(null)
    setError('')
    get(`/v1/definitions/${encodeURIComponent(selected)}`)
      .then((p) => { if (alive) setPdd(p) })
      .catch((e) => { if (alive) setError(e.message || String(e)) })
    return () => { alive = false }
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
