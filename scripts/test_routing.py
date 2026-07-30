#!/usr/bin/env python3
"""Tests for step-to-step routing and design-time quorum validation.

Two silent-failure bugs are covered here:

  1. COMPARISONS WERE UNSUPPORTED. A gateway rule like "amount >= 5000" was not
     understood, fell through to a truthiness check on the literal string, matched
     NOTHING, and the run went straight to the graph's END — where the outcome
     defaults to "completed". A broken condition therefore looked exactly like a
     successful finish, i.e. a silent auto-approval.

  2. A DEAD END LOOKED LIKE SUCCESS. Any step that resolved to no next step
     returned None and ended the run the same way. Now it raises DefinitionError
     with a message naming the step, and the activity reports it non-retryably.

Run: python3 scripts/test_routing.py
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "services", "worker", "graph"))
sys.path.insert(0, os.path.join(HERE, "..", "services", "api", "app"))

from pdd_graph import DefinitionError, compute_next  # noqa: E402

FAILS = 0


def ok(cond, msg):
    global FAILS
    print(("PASS  " if cond else "FAIL  ") + msg)
    if not cond:
        FAILS += 1


def raises(fn, msg):
    try:
        fn()
    except DefinitionError:
        ok(True, msg)
        return
    except Exception as exc:
        ok(False, f"{msg} (raised {type(exc).__name__} instead)")
        return
    ok(False, f"{msg} (did NOT raise)")


# --- 1. Numeric comparisons on a gateway ------------------------------------
GATE = {"id": "route_by_amount", "type": "gateway", "edges": [
    {"when": "amount >= 5000", "to": "big"},
    {"when": "default", "to": "small"},
]}
ok(compute_next(GATE, {"data": {"amount": 9000}}) == "big", "amount 9000 >= 5000 -> big")
ok(compute_next(GATE, {"data": {"amount": 100}}) == "small", "amount 100 -> default branch")
ok(compute_next(GATE, {"data": {"amount": 5000}}) == "big", ">= includes the boundary")
# Values arrive from forms and emails as TEXT; they must still compare numerically.
ok(compute_next(GATE, {"data": {"amount": "9000"}}) == "big",
   "a numeric STRING '9000' still compares as a number, not as text")
ok(compute_next(GATE, {"data": {"amount": "600"}}) == "small",
   "'600' is NOT greater than 5000 (text comparison would have said it was)")

for op, hi, lo in ((">", "big", "small"), ("<", "small", "big")):
    node = {"id": "g", "type": "gateway", "edges": [
        {"when": f"amount {op} 500", "to": "hit"}, {"when": "default", "to": "miss"}]}
    ok(compute_next(node, {"data": {"amount": 900}}) == ("hit" if op == ">" else "miss"),
       f"operator '{op}' is honoured")

# "<=" must not be read as "<".
LE = {"id": "g", "type": "gateway", "edges": [
    {"when": "days <= 3", "to": "short"}, {"when": "default", "to": "long"}]}
ok(compute_next(LE, {"data": {"days": 3}}) == "short", "'<=' matches the boundary (not parsed as '<')")
ok(compute_next(LE, {"data": {"days": 4}}) == "long", "'<=' rejects above the boundary")

# Equality/inequality and plain truthiness still work.
EQ = {"id": "g", "type": "gateway", "edges": [
    {"when": "type == 'urgent'", "to": "fast"}, {"when": "default", "to": "normal"}]}
ok(compute_next(EQ, {"data": {"type": "urgent"}}) == "fast", "string equality still works")
ok(compute_next(EQ, {"data": {"type": "other"}}) == "normal", "non-matching equality falls to default")

# A field compared against a CONFIG value by name.
REF = {"id": "g", "type": "gateway", "edges": [
    {"when": "amount >= threshold", "to": "big"}, {"when": "default", "to": "small"}]}
ok(compute_next(REF, {"data": {"amount": 100, "threshold": 50}}) == "big",
   "one field can be compared against another by name")

# A missing field must not match, and must not blow up.
ok(compute_next(GATE, {"data": {}}) == "small", "a missing field falls to the default branch")


# --- 2. Dead ends now fail loudly -------------------------------------------
raises(lambda: compute_next({"id": "post", "type": "automated"}, {}),
       "an automated step with no next step raises instead of silently completing")
raises(lambda: compute_next({"id": "check", "type": "gateway", "edges": [
    {"when": "amount > 10", "to": "x"}]}, {"data": {"amount": 1}}),
    "a gateway where NO condition matches raises (add a default)")
raises(lambda: compute_next({"id": "review", "type": "decision", "routes": [], "edges": []}, {}),
       "a decision that chose no route raises (add an Otherwise rule)")
raises(lambda: compute_next({"id": "review", "type": "decision",
                             "edges": [{"edge": "A", "to": "x"}]}, {"route": "B"}),
       "a decision whose chosen route is not connected raises")
raises(lambda: compute_next({"id": "approve", "type": "human", "edges": [
    {"when": "decision == 'approve'", "to": "next"}]}, {"decision": {"decision": "reject"}}),
    "a human step with no branch for the decision raises")

# An end step is terminal BY DESIGN and must stay silent.
ok(compute_next({"id": "done", "type": "end", "outcome": "approved"}, {}) is None,
   "an end step still returns None (terminal by design)")

# The error message names the step, so an author can find it.
try:
    compute_next({"id": "finalize_payment", "type": "automated"}, {})
except DefinitionError as exc:
    ok("finalize_payment" in str(exc), "the error message names the offending step")


# --- 3. Design-time quorum validation ---------------------------------------
# Pull _quorum_errors out of the SHIPPED api module by source, so the test exercises
# the real code without needing FastAPI and the rest of the API's dependencies
# installed (same trick scripts/test_journey.mjs uses for deriveJourney).
def _load_quorum_errors():
    import ast
    path = os.path.join(HERE, "..", "services", "api", "app", "main.py")
    with open(path, encoding="utf-8") as fh:
        source = fh.read()
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "_quorum_errors":
            namespace: dict = {}
            exec(compile(ast.Module(body=[node], type_ignores=[]), path, "exec"), namespace)
            return namespace["_quorum_errors"]
    return None


_quorum_errors = _load_quorum_errors()
if _quorum_errors is None:
    ok(False, "could not find _quorum_errors in services/api/app/main.py")
else:
    def q(n, of, rsc=True, nid="panel"):
        return {"nodes": [{"id": nid, "type": "human_task",
                           "completion": {"mode": "quorum", "n": n, "of": of,
                                          "rejectShortCircuits": rsc}}]}

    ok(_quorum_errors(q(2, 3)) == [], "2 of 3 is valid")
    ok(_quorum_errors(q(3, 3)) == [], "3 of 3 is valid")
    errs = _quorum_errors(q(5, 3))
    ok(len(errs) == 1 and "panel" in errs[0] and "5" in errs[0] and "3" in errs[0],
       "5 of 3 is rejected AT SAVE TIME, naming the step and the numbers")
    ok(_quorum_errors(q(0, 3)) != [], "0 approvals is rejected")
    ok(_quorum_errors(q("two", 3)) != [], "a non-numeric count is rejected")
    ok(_quorum_errors(q(2, 3, rsc=None)) != [], "a missing rejectShortCircuits is rejected")
    ok(_quorum_errors({"nodes": [{"id": "a", "type": "human_task"}]}) == [],
       "a single-approver step is untouched")
    ok(_quorum_errors({}) == [], "an empty definition produces no quorum errors")

print()
print(f"{FAILS} FAILURE(S)" if FAILS else "ALL GOOD")
sys.exit(1 if FAILS else 0)
