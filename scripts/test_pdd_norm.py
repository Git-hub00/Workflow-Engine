#!/usr/bin/env python3
# Tests for the tolerant PDD reader: different dialects must normalize to the
# same canonical shape, and quorum must be detected on ANY step name.
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "services", "worker", "graph"))

from pdd_norm import normalize_pdd, canon_type, is_quorum, role_for  # noqa: E402

fails = []


def ok(cond, msg):
    print(("PASS  " if cond else "FAIL  ") + msg)
    if not cond:
        fails.append(msg)


# --- dialect 1: the Builder's own shape -----------------------------------
A = {
    "process_key": "invoice_approval",
    "roles": {"ap_manager": "ap_manager", "finance": "finance"},
    "nodes": [
        {"id": "start", "type": "start", "next": "extract"},
        {"id": "extract", "type": "automated", "action": "extract_fields", "next": "review"},
        {"id": "review", "type": "llm_decision",
         "routes": [{"edge": "R1", "when": "amount >= t"}, {"edge": "OTHERWISE", "when": "default"}],
         "edges": {"R1": "panel", "OTHERWISE": "pay"}},
        {"id": "panel", "type": "human_task", "assignment": {"role": "finance"},
         "completion": {"mode": "quorum", "n": 2, "of": 3, "rejectShortCircuits": True},
         "timeout": {"slaHours": 48, "on_timeout": "auto_reject"},
         "edges": [{"when": "quorum_approved", "to": "pay"}, {"when": "default", "to": "no"}]},
        {"id": "pay", "type": "automated", "action": "post_to_erp", "next": "yes"},
        {"id": "yes", "type": "end", "outcome": "approved"},
        {"id": "no", "type": "end", "outcome": "rejected"},
    ],
}

# --- dialect 2: totally different spelling, SAME meaning ------------------
B = {
    "key": "invoice_approval",
    "roles": {"ap_manager": "ap_manager", "finance": "finance"},
    "steps": [
        {"name": "start", "kind": "begin", "then": "extract"},
        {"name": "extract", "kind": "service", "activity": "extract", "then": "review"},
        {"name": "review", "kind": "ai_decision",
         "options": [{"name": "R1", "if": "amount >= t", "target": "panel"},
                     {"name": "OTHERWISE", "if": "default", "target": "pay"}]},
        {"name": "panel", "kind": "approval", "role": "finance",
         "quorum": {"required": 2, "capacity": 3, "reject_short_circuits": True},
         "sla_hours": 48, "on_timeout": "auto_reject",
         "transitions": [{"condition": "quorum_approved", "target": "pay"},
                         {"condition": "default", "target": "no"}]},
        {"name": "pay", "kind": "action", "do": "post", "goto": "yes"},
        {"name": "yes", "kind": "finish", "result": "approved"},
        {"name": "no", "kind": "finish", "result": "rejected"},
    ],
}

for label, pdd in (("dialect-A", A), ("dialect-B", B)):
    n = normalize_pdd(pdd)
    ok(n["process_key"] == "invoice_approval", f"{label}: process_key")
    ok(n["start_id"] == "extract", f"{label}: start resolves to 'extract' (got {n['start_id']})")
    byid = n["nodes_by_id"]
    ok(byid["extract"]["type"] == "automated", f"{label}: extract is automated")
    ok(byid["extract"]["action"] == "extract_fields", f"{label}: action alias -> extract_fields")
    ok(byid["extract"]["next"] == "review", f"{label}: next/then/goto")
    ok(byid["review"]["type"] == "decision", f"{label}: decision type")
    ok(byid["panel"]["type"] == "human", f"{label}: human type")
    ok(byid["panel"]["role"] == "finance", f"{label}: role from assignment OR flat")
    ok(is_quorum(byid["panel"]), f"{label}: QUORUM detected on a step NOT named 'finance'")
    c = byid["panel"]["completion"]
    ok(c["n"] == 2 and c["of"] == 3 and c["rejectShortCircuits"] is True, f"{label}: quorum 2-of-3 + reject")
    ok(byid["panel"]["sla_hours"] == 48, f"{label}: sla hours")
    ok(byid["panel"]["on_timeout"] == "auto_reject", f"{label}: on_timeout")
    ok(byid["pay"]["action"] == "post_to_record", f"{label}: post_to_erp alias -> post_to_record")
    ok(byid["yes"]["outcome"] == "approved", f"{label}: outcome/result")
    edges = byid["panel"]["edges"]
    ok([e["to"] for e in edges] == ["pay", "no"], f"{label}: edge order preserved")

# routes -> edges mapping for dict-style vs list-style decisions
na, nb = normalize_pdd(A), normalize_pdd(B)
ra = {e["edge"]: e["to"] for e in na["nodes_by_id"]["review"]["edges"]}
ok(ra.get("R1") == "panel" and ra.get("OTHERWISE") == "pay", "dialect-A: decision edges map")
rb = {r["edge"]: r["to"] for r in nb["nodes_by_id"]["review"]["routes"]}
ok(rb.get("R1") == "panel" and rb.get("OTHERWISE") == "pay", "dialect-B: decision routes carry targets")

# other shapes / robustness
ok(canon_type("Human-Task") == "human", "type alias is case/dash tolerant")
ok(canon_type("gateway_exclusive") == "gateway", "gateway stays a plain rule gateway (no AI)")
single = normalize_pdd({"nodes": [{"id": "a", "type": "approval", "role": "mgr"}]})
ok(single["nodes_by_id"]["a"]["completion"] is None, "single approver -> no quorum")
ok(role_for({"role": "mgr"}, {"mgr": "ap_manager"}) == "ap_manager", "logical role -> real role")
dictnodes = normalize_pdd({"nodes": {"s": {"type": "start", "next": "x"}, "x": {"type": "end"}}})
ok(dictnodes["start_id"] == "x", "nodes given as a dict/object also work")
hours = normalize_pdd({"nodes": [{"id": "w", "type": "wait", "hours": 24, "next": "z"}]})
ok(hours["nodes_by_id"]["w"]["sleep_seconds"] == 86400.0, "wait hours -> seconds")

print()
print("ALL GOOD" if not fails else f"{len(fails)} FAILURE(S): {fails}")
sys.exit(1 if fails else 0)
