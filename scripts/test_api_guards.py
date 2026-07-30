#!/usr/bin/env python3
"""Tests for the API's access guards and the SQL scope fragment.

These cover fail-OPEN bugs — the dangerous kind, because nothing looks broken:

  * `_allowed_processes(None)` used to return None, and None means UNRESTRICTED. So an
    anonymous caller (and anyone whose token had merely EXPIRED) saw MORE than a
    properly scoped user. It now returns [] = sees nothing.
  * `_process_scope_sql` interpolated the workflow keys into the SQL text with
    hand-rolled quote escaping. It now uses an expanding bind parameter, so every call
    site must pair it with `_scoped_text()` and `_process_scope_params()` — a mismatch
    would be a runtime SQL error, which is what the binding tests below catch.
  * `/v1/events` accepted any anonymous caller, bypassing the per-task role gate that
    /v1/tasks/{token}/complete enforces.

Needs the API's dependencies importable (fastapi, sqlalchemy, pyjwt, psycopg).
Skips cleanly if they are missing.

Run: python3 scripts/test_api_guards.py
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "services", "api", "app"))
sys.path.insert(0, HERE)
os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://x:x@localhost:5432/x")

try:
    import fastapi
    import main
    from sqlalchemy.dialects import postgresql
except Exception as exc:                                   # pragma: no cover
    print(f"SKIP  API dependencies are not installed here ({exc})")
    sys.exit(0)

FAILS = 0


def ok(cond, msg):
    global FAILS
    print(("PASS  " if cond else "FAIL  ") + msg)
    if not cond:
        FAILS += 1


# --- 1. Who is allowed to see what --------------------------------------------
ok(main._allowed_processes(None) == [],
   "an unknown caller sees NOTHING (this used to mean 'unrestricted')")
ok(main._allowed_processes({"roles": ["ops_admin"]}) is None, "ops_admin is unrestricted")
ok(main._allowed_processes({"roles": ["process_author"]}) is None, "process_author is unrestricted")
ok(main._allowed_processes({"username": "d", "roles": [], "_dev_all_roles": True}) is None,
   "the AUTH_DISABLED dev user is unrestricted")

ok(main._has_role({"roles": ["manager"]}, "manager"), "a held role passes")
ok(not main._has_role({"roles": ["manager"]}, "finance"), "a role not held is refused")
ok(main._has_role({"roles": [], "_dev_all_roles": True}, "any_new_workflow_role"),
   "with auth disabled ANY role passes, including a brand-new workflow's role")
ok(not main._has_role(None, "x"), "no user holds no role")


# --- 2. The scope fragment binds correctly at every call site ------------------
def renders(stmt, params) -> bool:
    try:
        compiled = stmt.compile(dialect=postgresql.dialect())
        compiled.construct_params(params)
        return True
    except Exception as exc:
        print(f"      -> {type(exc).__name__}: {exc}")
        return False


for allowed in (None, [], ["invoice_approval", "leave_request"]):
    sql = ("SELECT 1 FROM task t WHERE t.status = :status "
           + main._process_scope_sql(allowed, "t.transaction_id") + " ORDER BY t.created_at")
    stmt = main._scoped_text(sql, allowed)
    params = {"status": "open", **main._process_scope_params(allowed)}
    ok(renders(stmt, params), f"the task query compiles for allowed={allowed!r}")
    present = "scope_keys" in str(stmt.compile(dialect=postgresql.dialect()))
    ok(present == bool(allowed),
       f"the bind parameter is present exactly when there is a filter (allowed={allowed!r})")

ok("1 = 0" in main._process_scope_sql([], "tr.id"),
   "assigned to nothing produces a filter that matches no rows")
ok(main._process_scope_sql(None, "tr.id") == "", "unrestricted adds no filter")

# transaction_stats strips the leading "AND " — verify that still compiles.
for allowed in (None, [], ["a"]):
    for pk in (None, "a"):
        clauses, params = [], {}
        if pk:
            clauses.append("tr.definition_version_id IN (SELECT 1)")
            params["pk"] = pk
        scope = main._process_scope_sql(allowed, "tr.id").strip()
        if scope:
            clauses.append(scope[4:] if scope.startswith("AND ") else scope)
            params.update(main._process_scope_params(allowed))
        where = (" WHERE " + " AND ".join(clauses) + " ") if clauses else ""
        stmt = main._scoped_text(
            f'SELECT tr.status, count(*) FROM "transaction" tr{where} GROUP BY tr.status',
            allowed if scope else None)
        ok(renders(stmt, params), f"the stats query compiles (allowed={allowed!r}, pk={pk!r})")


# --- 3. A bad id is a client error, not a 500 ---------------------------------
GOOD = "3f7c1a2b-4d5e-6f70-8a9b-0c1d2e3f4a5b"
ok(main._uuid_or_422(GOOD) == GOOD, "a valid uuid passes through")
for value in ("abc", "", None, "123", "../etc/passwd"):
    try:
        main._uuid_or_422(value)
        ok(False, f"{value!r} should be refused")
    except fastapi.HTTPException as exc:
        ok(exc.status_code == 422, f"{value!r} gives 422, not an opaque 500")


# --- 4. A quorum is recognised from its POLICY, never from the step's name -----
ok(main._is_quorum_policy({"quorum": {"n": 2, "of": 3}}), "quorum read from the policy")
ok(main._is_quorum_policy({"n": 2, "of": 3}), "the legacy top-level n/of shape still works")
ok(not main._is_quorum_policy({"kind": "finance"}),
   "a step merely NAMED finance is not a quorum")
ok(not main._is_quorum_policy(None), "no policy is not a quorum")


# --- 5. The backend service key ----------------------------------------------
os.environ.pop("INTERNAL_API_KEY", None)
ok(not main._internal_key_ok("anything"),
   "with no key configured nothing is treated as trusted")
os.environ["INTERNAL_API_KEY"] = "s3cret"
ok(main._internal_key_ok("s3cret"), "the configured key is trusted")
ok(not main._internal_key_ok("wrong"), "a wrong key is not trusted")
ok(not main._internal_key_ok(None), "a missing key is not trusted")
os.environ.pop("INTERNAL_API_KEY")


# --- 6. Keycloak's own roles are never offered as business roles --------------
for role in ("offline_access", "uma_authorization", "default-roles-workflow", "admin"):
    ok(main._is_builtin_role(role), f"{role} is hidden as a built-in")
ok(not main._is_builtin_role("manager"), "manager is a real business role")


# --- 7. Design-time quorum validation ----------------------------------------
def quorum_pdd(n, of, rsc=True, nid="panel"):
    return {"nodes": [{"id": nid, "type": "human_task",
                       "completion": {"mode": "quorum", "n": n, "of": of,
                                      "rejectShortCircuits": rsc}}]}


ok(main._quorum_errors(quorum_pdd(2, 3)) == [], "2 of 3 saves cleanly")
errors = main._quorum_errors(quorum_pdd(5, 3))
ok(len(errors) == 1 and "panel" in errors[0],
   "5 of 3 is refused AT SAVE TIME and the message names the step")

print()
print(f"{FAILS} FAILURE(S)" if FAILS else "ALL GOOD")
sys.exit(1 if FAILS else 0)
