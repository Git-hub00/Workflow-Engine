// Tests for deriveJourney(): does one transaction's real audit trail produce the
// correct highlighted path, current step and outcome?
// Run: node scripts/test_journey.mjs
import fs from 'fs'

// Pull deriveJourney out of App.jsx so the test exercises the SHIPPED code.
const src = fs.readFileSync(new URL('../services/spa/src/App.jsx', import.meta.url), 'utf8')
const start = src.indexOf('function deriveJourney')
const end = src.indexOf('// Show whatever fields THIS process carries', start)
const body = src.slice(start, end)
const normalizeStatus = (v) => (v ? String(v).trim().toLowerCase() : 'unknown')
const deriveJourney = new Function(`${body}; return deriveJourney;`)()

let fails = 0
const ok = (cond, msg) => { console.log((cond ? 'PASS  ' : 'FAIL  ') + msg); if (!cond) fails++ }

// The invoice workflow as the Builder saves it.
const INVOICE = { process_key: 'invoice_approval', nodes: [
  { id: 'start', type: 'start', next: 'extract' },
  { id: 'extract', type: 'automated', action: 'extract_fields', next: 'review' },
  { id: 'review', type: 'llm_decision',
    routes: [{ edge: 'REQUEST_INFO', when: 'has_missing' }, { edge: 'R2', when: 'amount < autoApproveUnder' }, { edge: 'OTHERWISE', when: 'default' }],
    edges: { REQUEST_INFO: 'request_info', R2: 'finalize', OTHERWISE: 'manager' } },
  { id: 'request_info', type: 'human_task', assignment: { role: 'vendor' }, next: 'review' },
  { id: 'manager', type: 'human_task', assignment: { role: 'manager' },
    edges: [{ when: "decision == 'approve'", to: 'afterManager' }, { when: 'default', to: 'end_rejected' }] },
  { id: 'afterManager', type: 'llm_decision',
    routes: [{ edge: 'R1', when: 'amount >= financeThreshold' }, { edge: 'OTHERWISE', when: 'default' }],
    edges: { R1: 'finance', OTHERWISE: 'finalize' } },
  { id: 'finance', type: 'human_task', assignment: { role: 'finance' },
    completion: { mode: 'quorum', n: 2, of: 3 },
    edges: [{ when: 'quorum_approved', to: 'finalize' }, { when: 'default', to: 'end_rejected' }] },
  { id: 'finalize', type: 'automated', action: 'post_to_record', next: 'end_approved' },
  { id: 'end_approved', type: 'end', outcome: 'approved' },
  { id: 'end_rejected', type: 'end', outcome: 'rejected' },
]}

const ev = (type, payload) => ({ type, payload, occurred_at: '2026-07-28T00:00:00Z' })

// ---- Case 1: YOUR real audit trail (missing fields -> request_info, running)
let j = deriveJourney(INVOICE, [
  ev('WORKFLOW_RUNNING', {}),
  ev('LLM_DECISION', { node_id: 'review', route: 'REQUEST_INFO', missing: ['poNumber', 'costCenter', 'taxId'] }),
  ev('TASK_CREATED', { node_id: 'request_info', role: 'vendor' }),
  ev('NOTIFY', {}),
], 'running')
ok(j.visited.includes('extract') && j.visited.includes('review') && j.visited.includes('request_info'),
   'case1: visited = extract, review, request_info')
ok(!j.visited.includes('manager') && !j.visited.includes('finance'),
   'case1: manager/finance NOT marked visited')
ok(j.taken.includes('extract>review') && j.taken.includes('review>request_info'),
   'case1: path edges extract>review, review>request_info')
ok(!j.taken.includes('review>manager'), 'case1: the untaken OTHERWISE branch is not highlighted')
ok(j.current === 'request_info', `case1: current = request_info (got ${j.current})`)
ok(j.outcome === undefined, 'case1: running -> no outcome tint')

// ---- Case 2: the 12000 finance invoice, fully approved
j = deriveJourney(INVOICE, [
  ev('WORKFLOW_RUNNING', {}),
  ev('LLM_DECISION', { node_id: 'review', route: 'OTHERWISE' }),
  ev('TASK_CREATED', { node_id: 'manager', role: 'manager' }),
  ev('HUMAN_DECISION', { decision: 'approve' }),
  ev('LLM_DECISION', { node_id: 'afterManager', route: 'R1' }),
  ev('TASK_CREATED', { node_id: 'finance', role: 'finance' }),
  ev('FINANCE_VOTE', { decision: 'approve' }),
], 'approved')
ok(['extract','review','manager','afterManager','finance','finalize','end_approved'].every((n) => j.visited.includes(n)),
   'case2: full path visited through finance to end_approved')
ok(j.taken.includes('review>manager') && j.taken.includes('afterManager>finance') && j.taken.includes('finance>finalize'),
   'case2: manager + finance branch edges highlighted')
ok(!j.visited.includes('request_info'), 'case2: request_info never visited')
ok(!j.visited.includes('end_rejected'), 'case2: end_rejected not visited')
ok(j.current === 'end_approved', `case2: current = end_approved (got ${j.current})`)
ok(j.outcome === 'approved', 'case2: outcome approved -> green tint')

// ---- Case 3: manager rejected
j = deriveJourney(INVOICE, [
  ev('WORKFLOW_RUNNING', {}),
  ev('LLM_DECISION', { node_id: 'review', route: 'OTHERWISE' }),
  ev('TASK_CREATED', { node_id: 'manager', role: 'manager' }),
  ev('HUMAN_DECISION', { decision: 'reject' }),
], 'rejected')
ok(j.current === 'end_rejected', `case3: current = end_rejected (got ${j.current})`)
ok(j.outcome === 'rejected', 'case3: outcome rejected -> red tint')
ok(j.visited.includes('manager'), 'case3: manager visited')
ok(!j.visited.includes('finance'), 'case3: finance not visited')

// ---- Case 4: auto-approved small invoice (no humans at all)
j = deriveJourney(INVOICE, [
  ev('WORKFLOW_RUNNING', {}),
  ev('LLM_DECISION', { node_id: 'review', route: 'R2' }),
], 'approved')
ok(j.taken.includes('review>finalize'), 'case4: auto-approve edge review>finalize')
ok(j.visited.includes('end_approved') && j.current === 'end_approved', 'case4: ends at end_approved')
ok(!j.visited.includes('manager'), 'case4: no human steps visited')

// ---- Case 5: the collect-info LOOP (asked twice, then routed on)
j = deriveJourney(INVOICE, [
  ev('LLM_DECISION', { node_id: 'review', route: 'REQUEST_INFO' }),
  ev('TASK_CREATED', { node_id: 'request_info', role: 'vendor' }),
  ev('HUMAN_DECISION', { decision: 'resubmit' }),
  ev('LLM_DECISION', { node_id: 'review', route: 'OTHERWISE' }),
  ev('TASK_CREATED', { node_id: 'manager', role: 'manager' }),
], 'running')
ok(j.taken.includes('request_info>review'), 'case5: loop-back edge request_info>review highlighted')
ok(j.taken.includes('review>manager'), 'case5: second decision then went to manager')
ok(j.current === 'manager', `case5: current = manager (got ${j.current})`)

// ---- Case 6: robustness — no events / no definition must not crash
ok(deriveJourney(INVOICE, [], 'running') !== null, 'case6: no events -> still returns a journey')
ok(deriveJourney({ nodes: [] }, [], 'running') === null, 'case6: empty definition -> null (caller shows a message)')
ok(deriveJourney(null, [], 'running') === null, 'case6: null definition -> null, no crash')

// ---- Case 7: a DIFFERENT workflow shape (leave) still works
const LEAVE = { nodes: [
  { id: 'start', type: 'start', next: 'extract' },
  { id: 'extract', type: 'automated', next: 'check' },
  { id: 'check', type: 'llm_decision', routes: [{ edge: 'A', when: 'x' }, { edge: 'OTHERWISE', when: 'default' }],
    edges: { A: 'finalize', OTHERWISE: 'manager' } },
  { id: 'manager', type: 'human_task', edges: [{ when: "decision == 'approve'", to: 'finalize' }, { when: 'default', to: 'end_rejected' }] },
  { id: 'finalize', type: 'automated', next: 'end_approved' },
  { id: 'end_approved', type: 'end', outcome: 'approved' },
  { id: 'end_rejected', type: 'end', outcome: 'rejected' },
]}
j = deriveJourney(LEAVE, [
  ev('LLM_DECISION', { node_id: 'check', route: 'OTHERWISE' }),
  ev('TASK_CREATED', { node_id: 'manager', role: 'manager' }),
], 'approved')
ok(j.visited.includes('manager') && j.current === 'end_approved', 'case7: a different workflow shape works too')

console.log()
console.log(fails ? `${fails} FAILURE(S)` : 'ALL GOOD')
process.exit(fails ? 1 : 0)
