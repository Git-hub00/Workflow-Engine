# services/api/app/main.py
#
# WHY this file exists:
# The FastAPI application for the workflow engine's HTTP API. Section 9.1 is the
# Transaction API: POST /v1/transactions is the public entrypoint that KICKS OFF
# a new invoice run. It does exactly what our end-to-end test previously did BY
# HAND — look up the published process definition, seed a `transaction` row, and
# start the Temporal InvoiceWorkflow — but now as a real, callable endpoint.

import json
import os
import sys
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import jwt
import requests
from jwt import PyJWKClient
from jwt.exceptions import PyJWKClientConnectionError, PyJWKClientError
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy import bindparam, create_engine, text
from sqlalchemy.exc import IntegrityError, ProgrammingError
from temporalio.client import Client
from temporalio.service import RPCError

# WHY sys.path manipulation: the API is a normal process, so it is free to adjust
# sys.path. It adds the worker's `activities` dir so the quorum/task helpers can be
# imported by bare module name. This file lives at services/api/app/main.py, so
# services/ is parents[2].
SERVICES_DIR = Path(__file__).resolve().parents[2]
_ACTIVITIES_DIR = SERVICES_DIR / "worker" / "activities"
if str(_ACTIVITIES_DIR) not in sys.path:
    sys.path.insert(0, str(_ACTIVITIES_DIR))
# scripts/ holds the shared PDD validator (also used by the CLI and seeds).
_SCRIPTS_DIR = SERVICES_DIR.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

# (The old Temporal-only interpreter was removed. LangGraph is now THE engine:
#  the API starts GraphOrchestratorWorkflow by name, so it never imports it.)
from pdd_validation import validate_pdd  # noqa: E402  (structural PDD checks)

# Module-level SQLAlchemy engine: created once, connection-pooled, reused by every
# request. (These calls are synchronous/blocking; fine for the MVP. Under real
# load the DB work should be offloaded to a thread, e.g. asyncio.to_thread.)
# Connection targets are env-driven so the SAME code runs both as a host process
# (systemd; defaults point at localhost) and inside a container (compose sets these
# to Docker service names: postgres / temporal).
DB_URL = os.getenv("DATABASE_URL", "postgresql+psycopg://app:app@localhost:5432/workflow_app")
# pool_pre_ping: without it, the FIRST request after a database restart (or after an
# idle connection is reaped by the network) fails with a 500, because a dead pooled
# connection is handed out and only discovered mid-statement. pool_pre_ping checks
# the connection is alive and transparently replaces it instead.
engine = create_engine(DB_URL, pool_pre_ping=True)
TEMPORAL_ADDRESS = os.getenv("TEMPORAL_ADDRESS", "localhost:7233")


# WHY lifespan: connecting to Temporal is relatively costly and should happen
# ONCE, not per request. We open a single client at startup and stash it on
# app.state so every request reuses the same connection; it is released when the
# app shuts down. (temporalio's Client has no explicit close() — its underlying
# connection is torn down when the process/loop ends.)
@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.temporal = await Client.connect(TEMPORAL_ADDRESS)
    yield


app = FastAPI(title="Workflow Engine API", lifespan=lifespan)

# WHY CORS: the SPA is served from a different origin (http://localhost:5173) than
# the API (http://localhost:8000). Browsers block cross-origin XHR/fetch unless the
# API returns CORS headers, so we allow the SPA origin explicitly.
#
# Env-driven: the origin was hardcoded to localhost:5173, so ANY deployment where
# the SPA is not same-origin (nginx proxying /api hides this; a direct API host does
# not) failed with an opaque browser CORS error and an apparently dead UI. Set
# CORS_ORIGINS to a comma-separated list of origins, or "*" to allow any.
from fastapi.middleware.cors import CORSMiddleware

_cors_env = os.getenv("CORS_ORIGINS", "http://localhost:5173").strip()
_cors_origins = ["*"] if _cors_env == "*" else [o.strip() for o in _cors_env.split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    # Credentials cannot be combined with a wildcard origin (the browser rejects it).
    allow_credentials=_cors_origins != ["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Keycloak JWT auth (Section 10). Tokens are RS256, signed by the "workflow"
# realm; we fetch the realm's public signing keys via JWKS and verify the
# signature + expiry on every request. Roles come from realm_access.roles.
# ---------------------------------------------------------------------------

# JWKS endpoint for the "workflow" realm. PyJWKClient fetches the signing keys
# lazily (on first use), so importing this module does no network I/O.
# Built from KEYCLOAK_URL so the API can reach Keycloak by its internal Docker
# service name (http://keycloak:8080) in containers, or localhost:8081 on the host.
KEYCLOAK_JWKS_URL = (
    f"{os.getenv('KEYCLOAK_URL', 'http://localhost:8081').rstrip('/')}"
    f"/realms/{os.getenv('KEYCLOAK_REALM', 'workflow')}/protocol/openid-connect/certs"
)

# JWKS hardening — prevents false 401s after a Keycloak signing-key rotation:
#   * cache_keys / max_cached_keys: cache resolved signing keys (bounded LRU) so we
#     don't re-parse the JWK set on every request.
#   * lifespan=300: the cached JWK set is treated as fresh for at most 5 minutes,
#     so keys are re-pulled periodically even absent a rotation.
#   * Rotation handling: PyJWKClient AUTOMATICALLY re-fetches the JWK set when a
#     token's `kid` is NOT present in the cache and retries the match — so a newly
#     rotated key is picked up on the very next request, not only after the cache
#     lifespan expires. That is what stops a rotated key from causing false 401s.
_jwks_client = PyJWKClient(
    KEYCLOAK_JWKS_URL,
    cache_keys=True,
    max_cached_keys=16,
    lifespan=300,
    timeout=10,
)


async def current_user(authorization: str | None = Header(default=None)) -> dict:
    # Escape hatch for local dev / existing tests: AUTH_DISABLED=1 bypasses token
    # verification and returns a fake admin holding every role, so flows can run
    # without minting real tokens.
    if os.getenv("AUTH_DISABLED") == "1":
        # ops_admin was missing, so with auth disabled EVERY /v1/admin/* endpoint
        # answered 403 "requires role 'ops_admin'" — the Admin UI was unusable in the
        # very mode meant to bypass auth. _DEV_ALL_ROLES makes the per-task role gate
        # pass for any workflow's role names too, not just the invoice ones.
        return {"username": "dev-admin", "roles": _DEV_ALL_ROLES, "_dev_all_roles": True}

    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing or malformed Authorization header")

    token_str = authorization.split(" ", 1)[1].strip()
    try:
        signing_key = _jwks_client.get_signing_key_from_jwt(token_str)
        # Verify signature + expiry. We deliberately skip audience verification
        # (Keycloak's aud varies by client); signature/expiry are the
        # security-critical checks for the MVP.
        #
        # leeway=60: tolerate up to 60s of clock skew between the Keycloak
        # container and this host. Without it, a token whose `iat`/`nbf` is a few
        # seconds in the future (Keycloak's VM clock running ahead of the host —
        # common with Docker Desktop) is rejected as ImmatureSignatureError
        # ("token is not yet valid") and surfaces here as a false 401.
        claims = jwt.decode(
            token_str,
            signing_key.key,
            algorithms=["RS256"],
            leeway=60,
            options={"verify_aud": False},
        )
    except PyJWKClientConnectionError as exc:
        # Keycloak / JWKS UNREACHABLE is not a bad token. Reporting it as 401
        # "invalid or expired token" made every user log out and re-login during a
        # Keycloak restart — which also failed — and sent support hunting a token bug
        # that did not exist. 503 says what is actually wrong.
        raise HTTPException(status_code=503,
                            detail=f"identity provider unavailable: {exc}") from exc
    except PyJWKClientError:
        # "Unable to find a signing key that matches …" — the JWKS was READ fine, the
        # token simply does not belong to it (a rotated key, or another realm). That
        # IS a bad token, so it must stay a 401 or the SPA never re-authenticates.
        raise HTTPException(status_code=401, detail="invalid or expired token")
    except jwt.PyJWTError:
        # A genuinely bad token (bad signature, expired, malformed) -> 401.
        raise HTTPException(status_code=401, detail="invalid or expired token")
    except Exception as exc:
        # Network/DNS failures inside the JWKS fetch also mean "IdP unreachable".
        raise HTTPException(status_code=503,
                            detail=f"could not verify token: {exc}") from exc

    roles = claims.get("realm_access", {}).get("roles", []) or []
    return {"username": claims.get("preferred_username") or claims.get("sub"), "roles": roles}


def _has_role(user: dict, role: str) -> bool:
    """Role check that also honours the AUTH_DISABLED dev super-user."""
    if not user:
        return False
    if user.get("_dev_all_roles"):
        return True
    return role in (user.get("roles") or [])


def require_role(role: str):
    # Returns a FastAPI dependency that 403s unless the caller holds `role`. Handy
    # for whole-endpoint gating.
    async def _dep(user: dict = Depends(current_user)) -> dict:
        if not _has_role(user, role):
            raise HTTPException(status_code=403, detail=f"requires role '{role}'")
        return user

    return _dep


def _validate_author_finance_config(config: dict) -> tuple[int, int, bool]:
    """Return the configured Finance quorum or reject invalid author values."""
    quorum = config.get("quorum") if isinstance(config, dict) else None
    required = quorum.get("n") if isinstance(quorum, dict) else None
    capacity = quorum.get("of") if isinstance(quorum, dict) else None
    reject_short_circuits = config.get("rejectShortCircuits") if isinstance(config, dict) else None
    if (
        type(required) is not int
        or type(capacity) is not int
        or not 1 <= required <= capacity
    ):
        raise HTTPException(
            status_code=422,
            detail="invalid Finance quorum: expected integers satisfying 1 <= quorum.n <= quorum.of",
        )
    if type(reject_short_circuits) is not bool:
        raise HTTPException(
            status_code=422,
            detail="invalid Finance configuration: rejectShortCircuits must be a boolean",
        )
    return required, capacity, reject_short_circuits


def _validate_finance_completion(comp: dict, node_id: str = "") -> None:
    """Validate a quorum NODE's completion (n / of / rejectShortCircuits). The
    guided Builder stores quorum on the node, and both engines read it from there,
    so this is the correct place to check — NOT top-level config.

    The message now names the STEP and the ACTUAL numbers: the old text ("expected
    integers satisfying 1 <= n <= of") gave no clue which step or which values were
    wrong, so a failing submission was very hard to diagnose."""
    where = f"step '{node_id}': " if node_id else ""
    n = comp.get("n")
    of = comp.get("of")
    rsc = comp.get("rejectShortCircuits")
    ok_ints = (isinstance(n, int) and not isinstance(n, bool)
               and isinstance(of, int) and not isinstance(of, bool) and 1 <= n <= of)
    if not ok_ints:
        raise HTTPException(
            status_code=422,
            detail=(f"{where}invalid approval settings — needs {n!r} approval(s) out of "
                    f"{of!r} approver(s). Both must be whole numbers with "
                    "1 <= needed <= approvers. Fix it in the Builder and save again."))
    if not isinstance(rsc, bool):
        raise HTTPException(
            status_code=422,
            detail=f"{where}'one rejection ends it' must be true or false (got {rsc!r})")


CORE_APP_ROLES = ("ops_admin", "process_author")

# Every role name any workflow could use is unknowable, so the AUTH_DISABLED dev
# user is flagged with _dev_all_roles and _has_role() short-circuits for it. This
# list is only what a token would literally contain, for display/debug.
_DEV_ALL_ROLES = ["ops_admin", "process_author", "vendor", "ap_manager", "finance", "admin"]


def _user_processes(username: str) -> list:
    """Workflows this person takes part in (set by an admin).

    Returns None ONLY when the user_process table does not exist yet (migration not
    run), so a pending migration can't lock everyone out of their inbox.
    ANY OTHER database error is re-raised. It used to swallow every exception and
    return None — and None means UNRESTRICTED — so a momentary connection blip or
    statement timeout silently widened every business user's view to ALL workflows,
    leaving nothing behind but a line on stdout."""
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text("SELECT process_key FROM user_process WHERE username = :u ORDER BY process_key"),
                {"u": username},
            ).scalars().all()
        return list(rows)
    except ProgrammingError as exc:
        # UndefinedTable — the assignment table has not been created yet.
        print(f"user_process table missing (migration not run?): {exc}")
        return None


def _allowed_processes(user: dict | None):
    """None = unrestricted (admins, authors, or table missing).
    []   = assigned to nothing -> sees nothing.
    An UNKNOWN caller (no/failed token) gets [] — not None. Treating 'unknown' as
    'unrestricted' meant an anonymous request, or a merely EXPIRED token, saw MORE
    than a properly scoped user."""
    if not user:
        return []
    if user.get("_dev_all_roles"):
        return None
    roles = user.get("roles") or []
    if any(r in roles for r in CORE_APP_ROLES):
        return None
    return _user_processes(user.get("username") or "")


def _process_scope_sql(allowed, txn_col: str) -> str:
    """SQL fragment limiting rows to the caller's assigned workflows.

    Uses an expanding bind parameter (:scope_keys) instead of interpolating the
    values into the SQL text — callers must pass the params from
    _process_scope_params(). Hand-rolled quote escaping on a security predicate is
    one refactor away from a scope BYPASS, so it is gone."""
    if allowed is None:
        return ""
    if not allowed:
        return " AND 1 = 0 "          # assigned to nothing -> sees nothing
    return (f" AND {txn_col} IN (SELECT tr.id FROM \"transaction\" tr "
            "JOIN definition_version dv ON dv.id = tr.definition_version_id "
            "JOIN process_definition pd ON pd.id = dv.definition_id "
            "WHERE pd.process_key IN :scope_keys) ")


def _process_scope_params(allowed) -> dict:
    """Bind values for the fragment above (empty when no filter is applied)."""
    return {"scope_keys": tuple(allowed)} if allowed else {}


def _scoped_text(sql: str, allowed):
    """text() with :scope_keys marked as an expanding IN-list when it is present."""
    stmt = text(sql)
    if allowed:
        stmt = stmt.bindparams(bindparam("scope_keys", expanding=True))
    return stmt


def _process_key_of(conn, txn_id: str):
    """Which workflow a transaction belongs to (for write-side scope checks)."""
    return conn.execute(
        text('SELECT pd.process_key FROM "transaction" tr '
             "JOIN definition_version dv ON dv.id = tr.definition_version_id "
             "JOIN process_definition pd ON pd.id = dv.definition_id "
             "WHERE tr.id = CAST(:id AS uuid)"),
        {"id": str(txn_id)},
    ).scalar_one_or_none()


def _require_process_access(user: dict, txn_id: str) -> None:
    """403 unless this person is assigned to the transaction's workflow.

    The process scope was READ-ONLY: /v1/tasks hid other teams' tasks, but claim and
    complete never checked, so anyone holding a generic role name like 'manager' who
    obtained a token for another workflow's task could approve it."""
    allowed = _allowed_processes(user)
    if allowed is None:
        return
    with engine.connect() as conn:
        process_key = _process_key_of(conn, txn_id)
    if process_key is not None and process_key not in allowed:
        raise HTTPException(
            status_code=403,
            detail=f"you are not assigned to the workflow '{process_key}'")


def _uuid_or_422(value: str, what: str = "id") -> str:
    """Validate a uuid BEFORE it reaches Postgres. CAST('abc' AS uuid) raises a
    DataError that surfaced as an opaque 500; a bad id is a client mistake (422)."""
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(status_code=422, detail=f"{what} is not a valid uuid")


def _is_quorum_policy(policy) -> bool:
    """True when a task is a MULTI-APPROVER (quorum) task.

    Decided from the task's stored completion policy — NOT from the step being
    called 'finance'. This is what lets a quorum step be named 'panel',
    'committee', 'board' or anything else in any workflow."""
    if not isinstance(policy, dict):
        return False
    quorum = policy.get("quorum")
    if isinstance(quorum, dict):
        return quorum.get("n") is not None and quorum.get("of") is not None
    # legacy shape: n/of stored at the top level of the policy
    return policy.get("n") is not None and policy.get("of") is not None


def _finance_policy_values(policy: dict) -> tuple[int, int, bool]:
    """Read a Finance task's immutable policy, including the legacy n/of shape."""
    if not isinstance(policy, dict):
        raise HTTPException(status_code=409, detail="Finance task has no completion_policy")
    quorum = policy.get("quorum")
    if isinstance(quorum, dict):
        required = quorum.get("n")
        capacity = quorum.get("of")
    else:
        # Finance tasks created before participant slots stored n/of at the top level.
        required = policy.get("n")
        capacity = policy.get("of")
    reject_short_circuits = policy.get("rejectShortCircuits")
    if (
        type(required) is not int
        or type(capacity) is not int
        or not 1 <= required <= capacity
    ):
        raise HTTPException(
            status_code=409,
            detail="Finance task has invalid completion_policy quorum; expected 1 <= n <= of",
        )
    if type(reject_short_circuits) is not bool:
        raise HTTPException(
            status_code=409,
            detail="Finance task completion_policy is missing boolean rejectShortCircuits",
        )
    return required, capacity, reject_short_circuits


def _ensure_finance_slots(conn, task_id: str, capacity: int) -> None:
    """Lazily fill participant slots for a legacy Finance task under its row lock."""
    current = conn.execute(
        text("SELECT count(*) FROM participant_task WHERE task_id = CAST(:task_id AS uuid)"),
        {"task_id": task_id},
    ).scalar_one()
    if current > capacity:
        raise HTTPException(
            status_code=409,
            detail="Finance task has more participant rows than its stored capacity",
        )
    for _ in range(capacity - current):
        conn.execute(
            text(
                "INSERT INTO participant_task (id, task_id, status) "
                "VALUES (CAST(:id AS uuid), CAST(:task_id AS uuid), 'open')"
            ),
            {"id": str(uuid.uuid4()), "task_id": task_id},
        )


# WHY TransactionIn: the request contract for starting a run. `process_key`
# selects WHICH process definition to run; `data` is the raw invoice payload that
# becomes the transaction's data_snapshot (and what extract_fields reads back).
# WHY _optional_user: POST /v1/transactions is public (no login required), but WHEN
# a caller IS authenticated (e.g. a vendor submitting via the SPA) we record WHO
# submitted the invoice so the vendor portal can show request_info tasks for THEIR
# invoices. Resolves the user if a valid Bearer token is present; returns None
# otherwise, so the endpoint stays usable without a token (unchanged for anon callers).
async def _optional_user(authorization: str | None = Header(default=None)) -> dict | None:
    # NO header -> anonymous (the email adapter). A header that IS present but fails
    # verification is a REAL error and is reported: swallowing it turned an expired
    # token into an anonymous caller, which used to mean "unrestricted" — a silent
    # privilege ESCALATION every time somebody's session aged out.
    #
    # Header-less callers stay anonymous even under AUTH_DISABLED. Resolving them to
    # the dev admin made create_transaction prefer that name over the adapter's
    # submitted_by, so in dev mode every emailed request was attributed to 'dev-admin'
    # and the real submitter could never be told about it.
    if not authorization:
        return None
    return await current_user(authorization)


class TransactionIn(BaseModel):
    process_key: str
    data: dict
    # Honored ONLY for tokenless callers (e.g. the email adapter). An authenticated
    # caller's token identity always wins, so this can't be spoofed via the UI.
    submitted_by: str | None = None


# WHY /health: a trivial liveness probe for compose/k8s and quick manual checks.
@app.get("/health")
async def health():
    return {"status": "ok"}


# WHY POST /v1/transactions: the public "start an invoice" endpoint. This is the
# productized version of what the e2e test did manually — resolve the published
# definition, insert the transaction row, and launch the workflow.
@app.post("/v1/transactions")
async def create_transaction(body: TransactionIn, user: dict | None = Depends(_optional_user)):
    # 1. Resolve the PUBLISHED definition_version for this process_key (highest
    #    version). We need its id (to link the transaction) and its pdd (to build
    #    the cfg the workflow/decision node run on).
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT dv.id AS version_id, dv.pdd AS pdd "
                "FROM definition_version dv "
                "JOIN process_definition pd ON pd.id = dv.definition_id "
                "WHERE pd.process_key = :pk AND dv.status = 'published' "
                "ORDER BY dv.version DESC "
                "LIMIT 1"
            ),
            {"pk": body.process_key},
        ).mappings().first()

    # No published definition => nothing to run. 404 rather than a 500.
    if row is None:
        raise HTTPException(
            status_code=404,
            detail=f"No published definition found for process_key '{body.process_key}'",
        )

    # Build the runtime cfg. Note the PDD keeps config values under "config" and
    # role mappings at the top level, but the workflow expects cfg["roles"] too,
    # so we fold roles into the config dict (same shape the e2e test used).
    pdd = row["pdd"]  # jsonb -> dict (psycopg)
    cfg = {**pdd.get("config", {}), "roles": pdd.get("roles", {})}
    # Only enforce the finance-quorum config when this process actually uses a
    # quorum node — a process with no finance approval needs no quorum config.
    # The Builder stores quorum settings on the NODE (completion.n/of/
    # rejectShortCircuits), which both engines read — so validate the node, not
    # top-level config. (The old model kept quorum in config; that mismatch made
    # every quorum process 422 at start, so no transaction was ever created.)
    for _node in pdd.get("nodes", []):
        _comp = _node.get("completion") or {}
        if _comp.get("mode") == "quorum":
            _validate_finance_completion(_comp, _node.get("id") or "")

    # 2. Create the transaction row in ONE transaction: new uuid, linked to the
    #    resolved definition_version, status 'running', snapshot = posted data.
    txn_id = str(uuid.uuid4())
    with engine.begin() as conn:
        conn.execute(
            text(
                'INSERT INTO "transaction" '
                "(id, definition_version_id, temporal_workflow_id, status, data_snapshot, submitted_by) "
                "VALUES (CAST(:id AS uuid), CAST(:dv AS uuid), :workflow_id, :st, CAST(:snap AS jsonb), :submitted_by)"
            ),
            {
                "id": txn_id,
                "dv": str(row["version_id"]),
                "workflow_id": txn_id,
                "st": "running",
                "snap": json.dumps(body.data),
                # Token identity wins; a tokenless caller (email adapter) may pass
                # submitted_by in the body. NULL for anonymous callers with neither.
                "submitted_by": user["username"] if user else body.submitted_by,
            },
        )

    # 3. Start the Temporal workflow, using the transaction id as the workflow id
    #    (one workflow per transaction). The reused app.state.temporal client
    #    dispatches to the main "invoice-tq" queue the worker polls.
    # LangGraph is THE engine: GraphOrchestratorWorkflow turns this PDD into a
    # LangGraph graph and walks it, pausing at human steps while Temporal holds
    # the durable wait. Started BY NAME so the API needn't import langgraph.
    try:
        await app.state.temporal.start_workflow(
            "GraphOrchestratorWorkflow", args=[txn_id, pdd], id=txn_id, task_queue="invoice-tq")
    except Exception as exc:
        # The row is already committed as 'running'. If the workflow could not be
        # started (Temporal down, wrong task queue, oversized payload) the run would
        # sit in the Monitor as a phantom 'running' transaction forever — no workflow,
        # no task, and nothing in the product able to close it. Mark it failed and
        # tell the caller honestly.
        try:
            with engine.begin() as conn:
                conn.execute(
                    text('UPDATE "transaction" SET status = \'failed\', '
                         "closed_at = COALESCE(closed_at, now()) WHERE id = CAST(:i AS uuid)"),
                    {"i": txn_id})
        except Exception as cleanup_exc:
            print(f"could not mark transaction {txn_id} failed: {cleanup_exc}")
        raise HTTPException(
            status_code=503,
            detail=f"could not start the workflow engine for this request: {exc}") from exc

    # 4. Hand the caller the id they use to track/act on this run.
    return {"transaction_id": txn_id}


# WHY POST /v1/extract: document-upload
# assist for ANY process. Reads the PDF text and asks the LLM to pull the fields
# the process's PDD declares (extraction.fields, else data_schema keys). It NEVER
# creates a transaction and NEVER 500s — on any failure it returns
# {"fields": {}, "error": ...} (200) so the user can still fill the form by hand.
def _pdd_extract_fields(process_key: str) -> list:
    with engine.connect() as conn:
        pdd = conn.execute(
            text("SELECT dv.pdd FROM definition_version dv "
                 "JOIN process_definition pd ON pd.id = dv.definition_id "
                 "WHERE pd.process_key = :pk AND dv.status = 'published' "
                 "ORDER BY dv.version DESC LIMIT 1"),
            {"pk": process_key},
        ).scalar_one_or_none()
    if isinstance(pdd, dict):
        ext = pdd.get("extraction") or {}
        if isinstance(ext.get("fields"), list) and ext["fields"]:
            return ext["fields"]
        data_schema = pdd.get("data_schema")
        if isinstance(data_schema, dict) and data_schema:
            return list(data_schema.keys())
    # No fallback field list. It used to return the INVOICE field names for ANY
    # workflow, so uploading a document to a leave or refund process asked the model
    # for poNumber/taxId, got nulls, and autofilled nothing — looking like a broken
    # extractor rather than a definition with no declared fields.
    return []


@app.post("/v1/extract")
async def extract_document(file: UploadFile = File(...),
                           process_key: str = Form(...)):
    # process_key is REQUIRED. It defaulted to "invoice_approval", so a client that
    # omitted it silently extracted against the invoice definition.
    raw = await file.read()
    fields = _pdd_extract_fields(process_key)
    if not fields:
        return {"fields": {},
                "error": f"the workflow '{process_key}' declares no fields to extract "
                         "(add them in the Builder's Request details)"}
    # Blocking work (pypdf + LLM HTTP) runs on a thread so a slow model can NEVER
    # stall the API event loop for other requests.
    import asyncio
    return await asyncio.to_thread(_extract_document_sync, raw, fields)


def _extract_document_sync(raw: bytes, fields: list):
    import io
    import re
    from pypdf import PdfReader
    from openai import OpenAI

    try:
        reader = PdfReader(io.BytesIO(raw))
        text_content = "\n".join((page.extract_text() or "") for page in reader.pages)
    except Exception as exc:
        return {"fields": {}, "error": f"could not read PDF: {exc}"}

    if not text_content.strip():
        return {"fields": {}, "error": "no extractable text in PDF", "raw_text_len": 0}

    field_list = ", ".join(fields) if fields else "all relevant fields"
    try:
        client = OpenAI(
            base_url=os.getenv("LLM_BASE_URL", "http://localhost:11434/v1"),
            api_key="ollama",
            timeout=45,  # never hang on a slow/cold model; falls to {fields:{}, error}
        )
        completion = client.chat.completions.create(
            model=os.getenv("LLM_MODEL", "llama3.2:1b"),
            temperature=0,
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"Extract these fields as JSON only: {field_list}. "
                        "Use null for anything not found. Output ONLY JSON.\n\n"
                        f"Document text:\n{text_content[:6000]}"
                    ),
                }
            ],
        )
        content = completion.choices[0].message.content or ""
    except Exception as exc:
        return {"fields": {}, "error": f"LLM error: {exc}", "raw_text_len": len(text_content)}

    # Defensive parse: strip ``` fences, then take the first {...} block.
    cleaned = re.sub(r"^```(?:json)?|```$", "", content.strip(), flags=re.MULTILINE).strip()
    match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    try:
        parsed = json.loads(match.group(0) if match else cleaned)
    except Exception:
        return {"fields": {}, "error": "could not parse LLM output as JSON", "raw_text_len": len(text_content)}
    if not isinstance(parsed, dict):
        return {"fields": {}, "error": "LLM did not return a JSON object", "raw_text_len": len(text_content)}

    fields_out = {key: parsed.get(key) for key in fields}
    return {"fields": fields_out, "raw_text_len": len(text_content)}


# WHY EventIn: the request contract for the Event Ingress (Section 9.2) — the way
# the outside world (task-inbox UI, an email-reply handler, an API caller) feeds
# a decision back into a durably-paused workflow. `idempotency_key` is mandatory
# because the SAME decision can legitimately arrive more than once (a double
# click, a client retry, or the user replying via both the app AND email), and
# we must apply it EXACTLY once.
class EventIn(BaseModel):
    transaction_id: str
    task_token: str | None = None
    idempotency_key: str
    kind: str = "human"   # "human" (default) or "finance"
    payload: dict         # human: {"decision": "approve"}; finance: {"participant": "...", "decision": "approve"}


# WHY POST /v1/events: this is the RESUME path. A running InvoiceWorkflow is
# parked in _await_human / _finance_quorum on workflow.wait_condition(); it stays
# there (durably, across restarts) until a SIGNAL arrives. This endpoint records
# the inbound decision, then delivers that signal — which is what wakes the
# workflow and lets it continue.
def _release_idempotency_key(key: str) -> None:
    """Un-claim an idempotency key after the SIGNAL failed.

    The database commits (audit event + task marked done) BEFORE the signal is sent.
    If the signal then fails for any reason other than 'workflow already finished',
    the decision is recorded but the workflow is still parked — and because the key
    was consumed, the client's retry answered 'duplicate-ignored', actively asserting
    the work was done. The run became unresumable through the product. Releasing the
    key makes the retry actually re-attempt the signal."""
    try:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM idempotency_key WHERE key = :k"), {"k": key})
    except Exception as exc:                       # best effort; never mask the real error
        print(f"could not release idempotency key {key!r} after a failed signal: {exc}")


def _internal_key_ok(x_internal_key: str | None) -> bool:
    """True when the caller presented the configured backend service key.

    The email adapter is a trusted backend process with no user token. Set
    INTERNAL_API_KEY in services/api/.env (BOTH the API and the adapter load that
    file) and anonymous decision posting stops being accepted."""
    expected = os.getenv("INTERNAL_API_KEY")
    return bool(expected) and x_internal_key == expected


@app.post("/v1/events")
async def ingest_event(evt: EventIn,
                       user: dict | None = Depends(_optional_user),
                       x_internal_key: str | None = Header(default=None)):
    # AUTHORISATION. This endpoint marks a task done and signals the workflow to
    # resume — i.e. it APPROVES things — but it used to accept any anonymous caller,
    # so the per-task role gate on /v1/tasks/{token}/complete was bypassable by
    # posting here instead. Now:
    #   * a signed-in caller must hold the task's role AND be assigned to its
    #     workflow (exactly like the complete endpoint), and
    #   * a tokenless caller is only accepted while INTERNAL_API_KEY is unset, or
    #     when it presents that key.
    trusted = _internal_key_ok(x_internal_key)
    if not trusted and user is None and os.getenv("INTERNAL_API_KEY"):
        raise HTTPException(status_code=401,
                            detail="this endpoint requires a user token or the internal service key")
    if evt.kind == "finance":
        raise HTTPException(
            status_code=400,
            detail="Finance decisions must use the task complete endpoint after claiming a participant slot",
        )
    evt.transaction_id = _uuid_or_422(evt.transaction_id, "transaction_id")

    # A human decision MUST actually say something. An empty/garbage payload used to
    # latch the task done and wake the workflow with no decision at all, so the step
    # was consumed and the run took whatever the default branch was — irreversibly,
    # because the task was already closed.
    decision = evt.payload.get("decision") if isinstance(evt.payload, dict) else None
    if not isinstance(decision, str) or not decision.strip():
        raise HTTPException(
            status_code=422,
            detail="payload.decision is required (e.g. 'approve', 'reject' or 'resubmit')")

    # Resolve the task ONCE: role/scope gate, quorum guard, and identity for the audit.
    task_row = None
    if evt.task_token is not None:
        with engine.connect() as conn:
            task_row = conn.execute(
                text("SELECT transaction_id, node_id, assigned_role, completion_policy "
                     "FROM task WHERE token = :token"),
                {"token": evt.task_token},
            ).mappings().first()
        if task_row is None:
            raise HTTPException(status_code=404, detail="task not found")
        if not trusted and user is not None:
            role = task_row["assigned_role"]
            if role is not None and not _has_role(user, role):
                raise HTTPException(status_code=403, detail=f"requires role '{role}'")
            _require_process_access(user, str(task_row["transaction_id"]))
        # A MULTI-APPROVER step cannot be settled by a single decision. Closing it
        # here left every participant slot unvoted and the workflow parked on its
        # vote count FOREVER, with no task visible to anyone.
        if _is_quorum_policy(task_row["completion_policy"]):
            raise HTTPException(
                status_code=409,
                detail=(f"step '{task_row['node_id']}' needs a multi-approver vote — "
                        "claim a slot and decide via /v1/tasks/{token}/complete"))

    # The transaction must exist, else the event insert fails an FK and returns 500.
    with engine.connect() as conn:
        if conn.execute(text('SELECT 1 FROM "transaction" WHERE id = CAST(:i AS uuid)'),
                        {"i": evt.transaction_id}).first() is None:
            raise HTTPException(status_code=404,
                                detail=f"transaction '{evt.transaction_id}' not found")

    # The audit event's type reflects what kind of decision this is.
    event_type = "FINANCE_VOTE" if evt.kind == "finance" else "HUMAN_DECISION"
    event_id = str(uuid.uuid4())
    # Attribute the decision to the person who made it. "EventIngress" for everyone
    # destroyed accountability for exactly the approvals that most need it.
    actor = (user or {}).get("username") or ("email-adapter" if trusted else "EventIngress")

    # IDEMPOTENCY FIRST. In ONE transaction we (a) write the audit event, then
    # (b) claim the idempotency key by inserting (key, event_id). The PRIMARY KEY
    # on idempotency_key.key is the guard: if this key was already used, the claim
    # insert raises a unique-violation IntegrityError, we roll the WHOLE thing back
    # (no duplicate event, no task change, no signal) and report duplicate-ignored.
    #
    # NOTE: we scope the duplicate detection to the idempotency_key insert only
    # (via `dup`), so that a DIFFERENT integrity error — e.g. an FK violation from
    # a bad transaction_id on the EVENT insert — is NOT masked as a duplicate but
    # surfaces honestly (same principle as the append_event fix).
    dup = False
    task_already_done = False
    try:
        with engine.begin() as conn:
            # (a) record the decision in the immutable event log.
            conn.execute(
                text(
                    "INSERT INTO event (id, transaction_id, type, payload, actor) "
                    "VALUES (CAST(:id AS uuid), CAST(:txn AS uuid), :type, "
                    "        CAST(:payload AS jsonb), :actor)"
                ),
                {
                    "id": event_id,
                    "txn": evt.transaction_id,
                    "type": event_type,
                    "payload": json.dumps(evt.payload),
                    "actor": actor,
                },
            )
            # (b) claim the idempotency key (duplicate => IntegrityError here).
            try:
                conn.execute(
                    text(
                        "INSERT INTO idempotency_key (key, event_id) "
                        "VALUES (:key, CAST(:event_id AS uuid))"
                    ),
                    {"key": evt.idempotency_key, "event_id": event_id},
                )
            except IntegrityError:
                dup = True
                raise  # abort the whole transaction (rolls back the event too)

            # (c) mark the human task done — the SINGLE-USE LATCH that gives
            # first-wins dedup across channels (both app Send and email reply pass
            # task_token). The atomic open->done flip picks the winner: if it
            # changes NO row the task was ALREADY done (the OTHER channel won), so
            # we skip the signal below to avoid a double-apply.
            if evt.task_token is not None and evt.kind != "finance":
                # claimed_by comes from the VERIFIED token, never from the request
                # body — a caller could otherwise record the approval against
                # somebody else's name.
                updated = conn.execute(
                    text(
                        "UPDATE task SET status = 'done', "
                        "claimed_by = COALESCE(:claimed_by, claimed_by) "
                        "WHERE token = :token AND status <> 'done'"
                    ),
                    {"claimed_by": (user or {}).get("username"), "token": evt.task_token},
                )
                task_already_done = updated.rowcount == 0
    except IntegrityError:
        if dup:
            # Already processed — safe no-op. No signal, no other side effects.
            return {"status": "duplicate-ignored"}
        raise  # a real integrity failure (e.g. unknown transaction_id) — surface it

    # First-wins: a task_token whose task was ALREADY done means the OTHER channel
    # completed it first — the audit event is recorded, but we do NOT signal again.
    if task_already_done:
        return {"status": "task-already-done"}

    # (d) Signal the durably-paused workflow to RESUME. The workflow id is the
    # transaction id (set when the run was started). finance votes go to the
    # finance_vote signal; everything else to human_decision.
    #
    # node_id travels with the payload so the workflow can ignore a decision that
    # belongs to a step it has already moved past (see the correlation guard in
    # graph_orchestrator.human_decision).
    handle = app.state.temporal.get_workflow_handle(evt.transaction_id)
    sig = "finance_vote" if evt.kind == "finance" else "human_decision"
    signal_payload = dict(evt.payload)
    if task_row is not None and task_row["node_id"]:
        signal_payload.setdefault("node_id", task_row["node_id"])
    try:
        await handle.signal(sig, signal_payload)
    except RPCError as e:
        # A decision can legitimately arrive AFTER the workflow finished (late email reply,
        # retry, double-submit). The decision is already recorded in the event log above;
        # there is simply no running workflow left to resume, so treat it as a graceful
        # no-op rather than a server error.
        msg = str(e).lower()
        if "already completed" in msg or "not found" in msg:
            return {"status": "workflow-already-closed"}
        _release_idempotency_key(evt.idempotency_key)
        raise   # any other RPC error is a real failure — surface it
    except Exception:
        _release_idempotency_key(evt.idempotency_key)
        raise

    return {"status": "accepted"}


# WHY GET /v1/transactions/{txn_id}/open-task: an OPEN read the email adapter uses
# to resolve a transaction's CURRENT open human task — its token (to complete the
# exact task, enabling first-wins dedup with the app), node_id (resubmit vs
# approve/reject) and missing fields. Returns {"open_task": null} when none is open.
@app.get("/v1/transactions/{txn_id}/open-task")
async def transaction_open_task(txn_id: str):
    txn_id = _uuid_or_422(txn_id, "transaction_id")
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT token, node_id, assigned_role, completion_policy "
                "FROM task WHERE transaction_id = CAST(:t AS uuid) AND status = 'open' "
                "ORDER BY created_at DESC, node_id"
            ),
            {"t": txn_id},
        ).mappings().all()
        process_key = _process_key_of(conn, txn_id)
    if not rows:
        return {"open_task": None, "open_tasks": []}

    def _shape(row):
        policy = row["completion_policy"] if isinstance(row["completion_policy"], dict) else {}
        need = policy.get("need") if isinstance(policy.get("need"), list) else None
        return {
            "token": row["token"],
            "node_id": row["node_id"],
            "assigned_role": row["assigned_role"],
            "need": need,
            # is_quorum lets the email adapter refuse to settle a multi-approver step
            # with a single reply (which used to hang the run permanently).
            "is_quorum": _is_quorum_policy(row["completion_policy"]),
            "process_key": process_key,
        }

    shaped = [_shape(r) for r in rows]
    # open_task keeps the original single-task shape for existing callers; open_tasks
    # exposes ALL of them, because a parallel (fork) step can have several at once and
    # answering with only the newest let an emailed reply close the wrong branch.
    return {"open_task": shaped[0], "open_tasks": shaped}


# ===========================================================================
# Task Service (Section 9.4): the role-based human task inbox — list open work,
# atomically claim a task, and complete it (which funnels into the event ingress).
# ===========================================================================


# WHY GET /v1/tasks: the "pull my queue" endpoint a role-based task inbox uses —
# a manager's UI asks for the open tasks assigned to their role.
@app.get("/v1/tasks")
async def list_tasks(
    role: str | None = None,
    status: str = "open",
    user: dict = Depends(current_user),
):
    # (CAST(:role AS text) IS NULL OR assigned_role = :role) makes `role` optional
    # in one query; the CAST avoids Postgres "could not determine parameter type"
    # when role is NULL.
    # PROCESS SCOPING: a person only sees tasks from workflows they are assigned
    # to in Admin. Both "invoice" and "leave" may use the role `manager`, so the
    # role alone is not enough — manager1 (invoice) must not see leave tasks.
    # Admins and authors are not restricted.
    allowed = _allowed_processes(user)
    with engine.connect() as conn:
        rows = conn.execute(
            _scoped_text(
                "SELECT t.id, t.transaction_id, t.node_id, t.token, t.assigned_role, "
                "       t.status, t.claimed_by, t.completion_policy, t.created_at "
                "FROM task t "
                "WHERE t.status = :status "
                "AND (CAST(:role AS text) IS NULL OR t.assigned_role = CAST(:role AS text)) "
                + _process_scope_sql(allowed, "t.transaction_id") +
                " ORDER BY t.created_at",
                allowed,
            ),
            {"status": status, "role": role, **_process_scope_params(allowed)},
        ).mappings().all()

        result = []
        for row in rows:
            item = {
                "id": str(row["id"]),
                "transaction_id": str(row["transaction_id"]),
                "node_id": row["node_id"],
                "token": row["token"],
                "assigned_role": row["assigned_role"],
                "status": row["status"],
                "claimed_by": row["claimed_by"],
                "completion_policy": row["completion_policy"],
                "created_at": str(row["created_at"]) if row["created_at"] is not None else None,
            }
            # is_quorum drives the voting UI. Derived from the task's policy, so
            # ANY step name can be a quorum step (panel, committee, board…).
            item["is_quorum"] = _is_quorum_policy(row["completion_policy"])
            if item["is_quorum"]:
                try:
                    required, capacity, reject_short_circuits = _finance_policy_values(
                        row["completion_policy"]
                    )
                except HTTPException:
                    # DURABLE FIX: a malformed / legacy finance policy (e.g. one
                    # missing a boolean rejectShortCircuits) must NEVER 409 the
                    # WHOLE /v1/tasks endpoint — that blanks every user's inbox.
                    # Degrade THIS card only: mark it unavailable, skip enrichment,
                    # and let the rest of the list render normally.
                    item["finance_unavailable"] = True
                else:
                    participants = conn.execute(
                        text(
                            "SELECT id, claimed_by, decision, status "
                            "FROM participant_task WHERE task_id = CAST(:task_id AS uuid)"
                        ),
                        {"task_id": str(row["id"])},
                    ).mappings().all()
                    claimed = [participant for participant in participants if participant["claimed_by"]]
                    completed = [participant for participant in participants if participant["decision"]]
                    approvals = [
                        participant
                        for participant in completed
                        if participant["decision"].get("decision") == "approve"
                    ]
                    rejections = [
                        participant
                        for participant in completed
                        if participant["decision"].get("decision") == "reject"
                    ]
                    current_participant = next(
                        (
                            participant
                            for participant in participants
                            if participant["claimed_by"] == user["username"]
                        ),
                        None,
                    )
                    current_decision = (
                        current_participant["decision"].get("decision")
                        if current_participant and current_participant["decision"]
                        else None
                    )
                    unmaterialized_slots = max(capacity - len(participants), 0)
                    available_slots = sum(
                        1
                        for participant in participants
                        if participant["claimed_by"] is None and participant["status"] == "open"
                    ) + unmaterialized_slots
                    has_role = row["assigned_role"] in user["roles"]
                    item.update(
                        {
                            "required_approvals": required,
                            "participant_capacity": capacity,
                            "claimed_count": len(claimed),
                            "approval_count": len(approvals),
                            "rejection_count": len(rejections),
                            "completed_count": len(completed),
                            "available_slots": available_slots,
                            "current_user_claimed": current_participant is not None,
                            "current_user_participant_id": (
                                str(current_participant["id"]) if current_participant else None
                            ),
                            "current_user_decision": current_decision,
                            "can_claim": (
                                row["status"] == "open"
                                and has_role
                                and current_participant is None
                                and available_slots > 0
                            ),
                            "can_decide": (
                                row["status"] == "open"
                                and has_role
                                and current_participant is not None
                                and current_decision is None
                            ),
                            "reject_short_circuits": reject_short_circuits,
                        }
                    )
            result.append(item)

    # Cast uuids/timestamps to str so the payload is JSON-serializable.
    return result


# WHY ClaimIn: who is taking ownership of the task.
# claimed_by is IGNORED — ownership is always the verified token identity. It used to
# be honoured, so a user could claim a task in somebody else's name and the inbox's
# "owned by" column was caller-controlled. Kept optional so existing clients (the SPA
# still sends it) do not break.
class ClaimIn(BaseModel):
    claimed_by: str | None = None


# WHY POST /v1/tasks/{token}/claim: let a user grab a task so two people don't
# both work it.
@app.post("/v1/tasks/{token}/claim")
async def claim_task(token: str, body: ClaimIn, user: dict = Depends(current_user)):
    # Role gate (Section 10): the caller must hold THIS task's assigned_role.
    # Roles are per-task, so we look it up here rather than gating the whole
    # endpoint with a fixed role. (Unknown token -> None -> fall through to the
    # atomic claim below, which reports 409.)
    with engine.connect() as conn:
        task_row = conn.execute(
            text("SELECT assigned_role, node_id, transaction_id, completion_policy "
                 "FROM task WHERE token = :token"),
            {"token": token},
        ).mappings().first()
    assigned_role = task_row["assigned_role"] if task_row is not None else None
    if assigned_role is not None and not _has_role(user, assigned_role):
        raise HTTPException(status_code=403, detail=f"requires role '{assigned_role}'")
    # Process scope on the WRITE path too. Role names like 'manager' are shared
    # between workflows, so the role gate alone let someone assigned to invoices
    # claim a leave task if they got hold of its token.
    if task_row is not None:
        _require_process_access(user, str(task_row["transaction_id"]))

    if task_row is not None and _is_quorum_policy(task_row["completion_policy"]):
        with engine.begin() as conn:
            finance_task = conn.execute(
                text(
                    "SELECT id, status, completion_policy FROM task "
                    "WHERE token = :token FOR UPDATE"
                ),
                {"token": token},
            ).mappings().first()
            if finance_task is None:
                raise HTTPException(status_code=404, detail="task not found")
            required, capacity, _ = _finance_policy_values(finance_task["completion_policy"])

            # Serialize repeat claims by this user even when two browser requests race.
            conn.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:claim_key, 0))"),
                {"claim_key": f"{finance_task['id']}:{user['username']}"},
            )
            existing = conn.execute(
                text(
                    "SELECT id, status, decision FROM participant_task "
                    "WHERE task_id = CAST(:task_id AS uuid) AND claimed_by = :username "
                    "LIMIT 1"
                ),
                {"task_id": str(finance_task["id"]), "username": user["username"]},
            ).mappings().first()
            if existing is not None:
                return {
                    "status": "finance-slot-claimed",
                    "participant_id": str(existing["id"]),
                    "claimed_by": user["username"],
                    "decision": existing["decision"],
                    "required_approvals": required,
                    "participant_capacity": capacity,
                }
            if finance_task["status"] != "open":
                raise HTTPException(status_code=409, detail="Finance task is no longer open")
            _ensure_finance_slots(conn, str(finance_task["id"]), capacity)

            participant = conn.execute(
                text(
                    "UPDATE participant_task SET participant = :username, "
                    "claimed_by = :username, status = 'claimed' "
                    "WHERE id = ("
                    "  SELECT id FROM participant_task "
                    "  WHERE task_id = CAST(:task_id AS uuid) "
                    "    AND claimed_by IS NULL AND status = 'open' "
                    "  ORDER BY id FOR UPDATE SKIP LOCKED LIMIT 1"
                    ") RETURNING id, status"
                ),
                {"task_id": str(finance_task["id"]), "username": user["username"]},
            ).mappings().first()
            if participant is None:
                raise HTTPException(status_code=409, detail="All Finance participant slots are claimed")
            return {
                "status": "finance-slot-claimed",
                "participant_id": str(participant["id"]),
                "claimed_by": user["username"],
                "required_approvals": required,
                "participant_capacity": capacity,
            }

    # WHY the single UPDATE ... WHERE status='open' ... RETURNING is atomic: two
    # users racing to claim the same task both run this exact statement, but the
    # row-level lock means only ONE sees status='open' and flips it to 'claimed';
    # the loser's WHERE matches nothing and RETURNING yields zero rows. This
    # guarantees "a claimed task can't be claimed twice" WITHOUT a read-then-write
    # gap that a separate SELECT + UPDATE would open.
    who = user["username"]
    with engine.begin() as conn:
        row = conn.execute(
            text(
                "UPDATE task SET claimed_by = :who, status = 'claimed' "
                "WHERE token = :token AND status = 'open' "
                "RETURNING id, token, claimed_by, status"
            ),
            {"who": who, "token": token},
        ).mappings().first()

    if row is None:
        # No row flipped => not open: already claimed by someone else, or the
        # token doesn't exist / task isn't in 'open' state.
        raise HTTPException(
            status_code=409,
            detail="task not open (already claimed or does not exist)",
        )
    return {"status": "claimed", "token": token, "claimed_by": who}


# WHY CompleteIn: completing a task carries the decision payload plus the
# idempotency key that makes a resubmit safe.
class CompleteIn(BaseModel):
    idempotency_key: str
    payload: dict
    kind: str = "human"


async def _complete_finance_task(token: str, body: CompleteIn, user: dict) -> dict:
    decision = body.payload.get("decision")
    # Rejection reason travels with the vote so the audit (FINANCE_VOTE event) and
    # the terminal vendor rejection email can surface WHO rejected and WHY.
    reason = body.payload.get("reason")
    if decision not in {"approve", "reject"}:
        raise HTTPException(
            status_code=422,
            detail="Finance decision must be either 'approve' or 'reject'",
        )

    duplicate = False
    signal_payload = None
    response = None
    try:
        with engine.begin() as conn:
            if conn.execute(
                text("SELECT 1 FROM idempotency_key WHERE key = :key"),
                {"key": body.idempotency_key},
            ).first() is not None:
                return {"status": "duplicate-ignored"}

            task_row = conn.execute(
                text(
                    # Token is unique, so no node-name filter: a quorum step may be
                    # called anything (panel, committee, board…).
                    "SELECT id, transaction_id, status, completion_policy FROM task "
                    "WHERE token = :token FOR UPDATE"
                ),
                {"token": token},
            ).mappings().first()
            if task_row is None:
                raise HTTPException(status_code=404, detail="Approval task not found")
            # RE-CHECK the idempotency key now that we hold the task lock. The
            # pre-check above is outside the lock, so two concurrent retries of the
            # SAME request (a double-clicked Vote button) both passed it; the second
            # then blocked here and failed with 409 "already decided" — telling the
            # voter their successful vote had errored. Under the lock the first
            # request's key is visible, so the retry is correctly a duplicate.
            if conn.execute(
                text("SELECT 1 FROM idempotency_key WHERE key = :key"),
                {"key": body.idempotency_key},
            ).first() is not None:
                return {"status": "duplicate-ignored"}
            if task_row["status"] != "open":
                raise HTTPException(status_code=409, detail="This approval step is no longer open")

            required, capacity, reject_short_circuits = _finance_policy_values(
                task_row["completion_policy"]
            )
            participant = conn.execute(
                text(
                    "SELECT id, decision, status FROM participant_task "
                    "WHERE task_id = CAST(:task_id AS uuid) AND claimed_by = :username "
                    "FOR UPDATE"
                ),
                {"task_id": str(task_row["id"]), "username": user["username"]},
            ).mappings().first()
            if participant is None:
                raise HTTPException(
                    status_code=409,
                    detail="Claim a Finance participant slot before submitting a decision",
                )
            if participant["decision"] is not None or participant["status"] == "done":
                raise HTTPException(status_code=409, detail="This Finance user has already decided")

            conn.execute(
                text(
                    "UPDATE participant_task SET decision = CAST(:decision AS jsonb), status = 'done' "
                    "WHERE id = CAST(:id AS uuid)"
                ),
                {
                    "id": str(participant["id"]),
                    "decision": json.dumps(
                        {"decision": decision, **({"reason": reason} if reason else {})}
                    ),
                },
            )

            counts = conn.execute(
                text(
                    "SELECT "
                    " count(*) FILTER (WHERE claimed_by IS NOT NULL) AS claimed_count, "
                    " count(*) FILTER (WHERE decision->>'decision' = 'approve') AS approval_count, "
                    " count(*) FILTER (WHERE decision->>'decision' = 'reject') AS rejection_count, "
                    " count(*) FILTER (WHERE decision IS NOT NULL) AS completed_count "
                    "FROM participant_task WHERE task_id = CAST(:task_id AS uuid)"
                ),
                {"task_id": str(task_row["id"])},
            ).mappings().one()
            approval_count = counts["approval_count"]
            rejection_count = counts["rejection_count"]
            completed_count = counts["completed_count"]
            remaining_undecided_slots = max(capacity - completed_count, 0)

            finance_result = "pending"
            if reject_short_circuits and rejection_count > 0:
                finance_result = "rejected"
            elif approval_count >= required:
                finance_result = "approved"
            elif (
                not reject_short_circuits
                and approval_count + remaining_undecided_slots < required
            ):
                finance_result = "rejected"

            event_id = str(uuid.uuid4())
            event_payload = {
                "participant": user["username"],
                "participant_id": str(participant["id"]),
                "decision": decision,
                **({"reason": reason} if reason else {}),
            }
            conn.execute(
                text(
                    "INSERT INTO event (id, transaction_id, type, payload, actor) "
                    "VALUES (CAST(:id AS uuid), CAST(:txn AS uuid), 'FINANCE_VOTE', "
                    "        CAST(:payload AS jsonb), :actor)"
                ),
                {
                    "id": event_id,
                    "txn": str(task_row["transaction_id"]),
                    "payload": json.dumps(event_payload),
                    "actor": user["username"],
                },
            )
            try:
                conn.execute(
                    text(
                        "INSERT INTO idempotency_key (key, event_id) "
                        "VALUES (:key, CAST(:event_id AS uuid))"
                    ),
                    {"key": body.idempotency_key, "event_id": event_id},
                )
            except IntegrityError:
                duplicate = True
                raise

            if finance_result != "pending":
                conn.execute(
                    text("UPDATE task SET status = 'done' WHERE id = CAST(:id AS uuid)"),
                    {"id": str(task_row["id"])},
                )
                conn.execute(
                    text(
                        "UPDATE participant_task SET status = 'done' "
                        "WHERE task_id = CAST(:task_id AS uuid) AND status <> 'done'"
                    ),
                    {"task_id": str(task_row["id"])},
                )
                signal_payload = {
                    "terminal": True,
                    "decision": "approve" if finance_result == "approved" else "reject",
                }

            response = {
                "status": "accepted",
                "finance_status": finance_result,
                "claimed_count": counts["claimed_count"],
                "approval_count": approval_count,
                "rejection_count": rejection_count,
                "completed_count": completed_count,
                "remaining_undecided_slots": remaining_undecided_slots,
                "required_approvals": required,
                "participant_capacity": capacity,
            }
    except IntegrityError:
        if duplicate:
            return {"status": "duplicate-ignored"}
        raise

    if signal_payload is not None:
        handle = app.state.temporal.get_workflow_handle(str(task_row["transaction_id"]))
        try:
            await handle.signal("finance_vote", signal_payload)
        except RPCError as exc:
            message = str(exc).lower()
            if "already completed" in message or "not found" in message:
                response["status"] = "workflow-already-closed"
            else:
                _release_idempotency_key(body.idempotency_key)
                raise
        except Exception:
            # Same reasoning as in ingest_event: the vote is committed but the
            # workflow never woke up. Free the key so a retry can re-send the signal
            # instead of being told it was a duplicate.
            _release_idempotency_key(body.idempotency_key)
            raise
    return response


# WHY POST /v1/tasks/{token}/complete: completing a task IS submitting its
# decision. Rather than duplicate the record-event + dedupe + signal logic, we
# resolve the task's transaction and funnel through the SAME ingest_event path,
# passing the task_token so the task is marked done there.
@app.post("/v1/tasks/{token}/complete")
async def complete_task(token: str, body: CompleteIn, user: dict = Depends(current_user)):
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT transaction_id, assigned_role, node_id, completion_policy "
                 "FROM task WHERE token = :token"),
            {"token": token},
        ).mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail="task not found")

    # Role gate (Section 10): the caller must hold this task's assigned_role.
    assigned_role = row["assigned_role"]
    if assigned_role is not None and not _has_role(user, assigned_role):
        raise HTTPException(status_code=403, detail=f"requires role '{assigned_role}'")
    # …and be assigned to this task's workflow (shared role names, see claim_task).
    _require_process_access(user, str(row["transaction_id"]))

    # Multi-approver (quorum) tasks take the voting path. Decided by the task's
    # policy so the step can be named anything, in any workflow.
    is_quorum = _is_quorum_policy(row["completion_policy"])
    if is_quorum:
        return await _complete_finance_task(token, body, user)

    evt = EventIn(
        transaction_id=str(row["transaction_id"]),
        task_token=token,
        idempotency_key=body.idempotency_key,
        kind="human",
        payload=body.payload,
    )
    # The gates above already ran, so pass the verified user straight through.
    return await ingest_event(evt, user=user, x_internal_key=None)


# ===========================================================================
# Config Service (Section 9.6): read and update the runtime config that lives in
# the published definition_version's pdd JSON (the "Config tab").
# ===========================================================================


# WHY GET /v1/config/{process_key}: expose the current knobs (thresholds, quorum,
# approved vendors, SLA, ...) for a process so a UI can display/edit them.
@app.get("/v1/config/{process_key}")
async def get_config(process_key: str):
    with engine.connect() as conn:
        pdd = conn.execute(
            text(
                "SELECT dv.pdd FROM definition_version dv "
                "JOIN process_definition pd ON pd.id = dv.definition_id "
                "WHERE pd.process_key = :pk AND dv.status = 'published' "
                "ORDER BY dv.version DESC "
                "LIMIT 1"
            ),
            {"pk": process_key},
        ).scalar_one_or_none()

    if pdd is None:
        raise HTTPException(
            status_code=404,
            detail=f"No published definition found for process_key '{process_key}'",
        )
    return pdd.get("config", {})


# WHY ConfigIn: the new config block to store (replaces the existing one wholesale).
class ConfigIn(BaseModel):
    config: dict


# WHY PUT /v1/config/{process_key}: update the config knobs in place.
# WHY gated by process_author: editing the PUBLISHED process rules is a
# privileged, publish-like action (it changes what every future run does), so per
# the Section 10 acceptance ("only process_author can publish definitions") only
# a caller holding the process_author realm role may do it. Reading config (GET)
# stays open. require_role() 403s callers without the role (and 401s no/invalid token).
@app.put("/v1/config/{process_key}")
async def put_config(process_key: str, body: ConfigIn, user: dict = Depends(require_role("process_author"))):
    # Validate the finance quorum only if this config actually carries one.
    if body.config.get("quorum") is not None:
        _validate_author_finance_config(body.config)

    # Read AND write inside ONE transaction, holding the definition row lock, then
    # publish a NEW immutable version.
    #
    # THREE bugs this fixes. The old code (a) UPDATEd the published
    # definition_version row in place — the very row in-flight transactions are
    # pinned to, so replaying an old run showed config that never applied to it, with
    # no audit of who changed what; (b) read on one connection and wrote on another
    # with no lock, so two concurrent edits lost one silently, and if a Builder save
    # published a newer version in between, the write landed on the SUPERSEDED
    # version and had no effect at all — while still answering {"status":"updated"};
    # (c) never re-validated the merged definition.
    with engine.begin() as conn:
        row = conn.execute(
            text(
                "SELECT dv.pdd AS pdd FROM definition_version dv "
                "JOIN process_definition pd ON pd.id = dv.definition_id "
                "WHERE pd.process_key = :pk AND dv.status = 'published' "
                "ORDER BY dv.version DESC LIMIT 1 FOR UPDATE OF dv"
            ),
            {"pk": process_key},
        ).mappings().first()
        if row is None:
            raise HTTPException(
                status_code=404,
                detail=f"No published definition found for process_key '{process_key}'",
            )
        if not isinstance(row["pdd"], dict):
            raise HTTPException(status_code=409,
                                detail=f"the stored definition for '{process_key}' is not readable")

        # Replace ONLY the "config" key; keep process_key, roles, nodes and the rest.
        new_pdd = {**row["pdd"], "config": body.config}
        errors = _quorum_errors(new_pdd)
        if errors:
            raise HTTPException(status_code=422, detail={"errors": errors})

        new_version = _next_version_locked(conn, process_key)
        new_pdd["version"] = new_version
        conn.execute(
            text("INSERT INTO definition_version "
                 "(id, definition_id, version, pdd, status, published_at) "
                 "VALUES (CAST(:id AS uuid), "
                 "        (SELECT id FROM process_definition WHERE process_key = :pk), "
                 "        :v, CAST(:pdd AS jsonb), 'published', now())"),
            {"id": str(uuid.uuid4()), "pk": process_key, "v": new_version,
             "pdd": json.dumps(new_pdd)},
        )

    # New transactions pick this up automatically: POST /v1/transactions reads the
    # LATEST published version at start time. In-flight workflows keep the version
    # they were started with — which is exactly why we add a version instead of
    # rewriting one.
    return {"status": "updated", "process_key": process_key,
            "version": new_version, "config": body.config}


# ===========================================================================
# Definition Service (Section 5.1 / P3): author, validate, and publish PDDs.
# Static validation on publish (graph structure + referenced roles exist in
# Keycloak). New versions are immutable; in-flight transactions stay pinned.
# ===========================================================================


_BUILTIN_ROLE_PREFIXES = ("default-roles",)
_BUILTIN_ROLES = {"offline_access", "uma_authorization", "admin", "create-realm"}


def _is_builtin_role(name: str) -> bool:
    """Keycloak's own plumbing roles — never business roles."""
    n = str(name or "")
    return n in _BUILTIN_ROLES or n.startswith(_BUILTIN_ROLE_PREFIXES)


def _keycloak_realm_roles() -> set | None:
    # Names of all realm roles in Keycloak; None if unreachable (publish then warns
    # instead of hard-failing on a transient Keycloak outage).
    kc = os.getenv("KEYCLOAK_URL", "http://localhost:8081").rstrip("/")
    realm = os.getenv("KEYCLOAK_REALM", "workflow")
    try:
        token = requests.post(
            f"{kc}/realms/master/protocol/openid-connect/token",
            data={"client_id": "admin-cli", "grant_type": "password",
                  "username": os.getenv("KEYCLOAK_ADMIN", "admin"),
                  "password": os.getenv("KEYCLOAK_ADMIN_PASSWORD", "admin")},
            timeout=10,
        ).json().get("access_token")
        if not token:
            return None
        roles = requests.get(
            f"{kc}/admin/realms/{realm}/roles",
            headers={"Authorization": f"Bearer {token}"}, timeout=10,
        ).json()
        return {r["name"] for r in roles if isinstance(r, dict) and r.get("name")}
    except Exception as exc:
        print(f"definitions: keycloak role fetch failed: {exc}")
        return None


def _quorum_errors(pdd: dict) -> list:
    """Design-time check of every multi-approver step.

    WHY: nothing validated quorum settings at PUBLISH time — only
    create_transaction did, at 422. So an author could save "2 of 1" (or a quorum
    missing rejectShortCircuits), see a green "saved", and only discover it when
    EVERY submission to that workflow was rejected with an error they had no UI path
    to fix. Now it is caught at Save, naming the step and the numbers."""
    errors = []
    for node in (pdd.get("nodes") or []):
        if not isinstance(node, dict):
            continue
        comp = node.get("completion")
        if not isinstance(comp, dict) or comp.get("mode") != "quorum":
            continue
        nid = node.get("id") or "(unnamed step)"
        n, of = comp.get("n"), comp.get("of")
        ints = (isinstance(n, int) and not isinstance(n, bool)
                and isinstance(of, int) and not isinstance(of, bool))
        if not ints:
            errors.append(f"step '{nid}': approvals needed and number of approvers "
                          f"must both be whole numbers (got n={n!r}, of={of!r})")
        elif n < 1:
            errors.append(f"step '{nid}': needs at least 1 approval (got {n})")
        elif n > of:
            errors.append(f"step '{nid}': needs {n} approvals but only has {of} "
                          f"approver(s) — a request could never be approved")
        if not isinstance(comp.get("rejectShortCircuits"), bool):
            errors.append(f"step '{nid}': 'one rejection ends it' must be true or false")
    return errors


def _check_pdd(pdd: dict) -> tuple[list, list]:
    # Structural validation (shared validator) + quorum sanity + Keycloak roles.
    errors, warnings = validate_pdd(pdd)
    errors.extend(_quorum_errors(pdd))
    roles_map = pdd.get("roles", {}) if isinstance(pdd.get("roles"), dict) else {}
    realm_roles = _keycloak_realm_roles()
    if realm_roles is None:
        warnings.append("could not reach Keycloak to verify roles exist")
    else:
        for logical, kc_role in roles_map.items():
            if kc_role not in realm_roles:
                errors.append(f"role '{kc_role}' (mapped from '{logical}') does not exist in Keycloak")
    return errors, warnings


# Open, read-only list of realm role names so authors can PICK roles in the
# Builder. Creating/deleting roles stays admin-only under /v1/admin/roles.
@app.get("/v1/roles")
async def list_roles(user: dict = Depends(current_user)):
    # Requires a token: every call performs a Keycloak MASTER-realm admin login, so
    # while this was open an anonymous caller could enumerate all role names and
    # hammer Keycloak's admin token endpoint.
    # Hide Keycloak's own built-in roles — they are not business roles and must
    # not be offered when assigning people to workflow steps.
    return sorted(r for r in (_keycloak_realm_roles() or []) if not _is_builtin_role(r))


def _next_version_locked(conn, process_key: str) -> int:
    """Reserve the next version number for this workflow, under a row lock.

    The definition row is created if needed with ON CONFLICT DO NOTHING and then
    SELECT ... FOR UPDATE, so concurrent first-publishes of the same process_key
    cannot create TWO process_definition rows. That mattered: version numbers are
    counted per definition_id, so both copies started at version 1 and every later
    read ('latest published version') picked between two different workflows at
    random, while the catalog's GROUP BY hid the duplication completely."""
    conn.execute(
        text("INSERT INTO process_definition (id, process_key) "
             "VALUES (CAST(:id AS uuid), :pk) ON CONFLICT (process_key) DO NOTHING"),
        {"id": str(uuid.uuid4()), "pk": process_key},
    )
    definition_id = conn.execute(
        text("SELECT id FROM process_definition WHERE process_key = :pk FOR UPDATE"),
        {"pk": process_key},
    ).scalar_one_or_none()
    if definition_id is None:
        raise HTTPException(status_code=500,
                            detail=f"could not create the definition for '{process_key}'")
    max_version = conn.execute(
        text("SELECT COALESCE(MAX(version), 0) FROM definition_version "
             "WHERE definition_id = CAST(:d AS uuid)"),
        {"d": str(definition_id)},
    ).scalar_one()
    return int(max_version) + 1


class DefinitionIn(BaseModel):
    pdd: dict
    publish: bool = True


# WHY POST /v1/definitions/validate: dry-run the checks so an author sees errors
# BEFORE publishing. Open (read-like); it writes nothing.
@app.post("/v1/definitions/validate")
async def validate_definition(body: DefinitionIn,
                              user: dict = Depends(require_role("process_author"))):
    # Same reasoning as /v1/roles: this performs a Keycloak admin login, and only an
    # author has any reason to dry-run a definition.
    errors, warnings = _check_pdd(body.pdd)
    return {"valid": not errors, "errors": errors, "warnings": warnings}


# WHY POST /v1/definitions: author/publish a NEW version of a process. Gated by
# process_author. Validates first; a new immutable version is created (max+1).
@app.post("/v1/definitions")
async def create_definition(body: DefinitionIn, user: dict = Depends(require_role("process_author"))):
    pdd = body.pdd
    process_key = pdd.get("process_key")
    if not process_key:
        raise HTTPException(status_code=400, detail="pdd.process_key is required")

    errors, warnings = _check_pdd(pdd)
    if errors:
        raise HTTPException(status_code=400, detail={"errors": errors, "warnings": warnings})

    status = "published" if body.publish else "draft"
    try:
        with engine.begin() as conn:
            new_version = _next_version_locked(conn, process_key)
            pdd_to_store = {**pdd, "version": new_version}  # stored version is authoritative
            conn.execute(
                text("INSERT INTO definition_version "
                     "(id, definition_id, version, pdd, status, published_at) "
                     "VALUES (CAST(:id AS uuid), "
                     "        (SELECT id FROM process_definition WHERE process_key = :pk), "
                     "        :v, CAST(:pdd AS jsonb), :st, "
                     "        CASE WHEN :st = 'published' THEN now() ELSE NULL END)"),
                {"id": str(uuid.uuid4()), "pk": process_key, "v": new_version,
                 "pdd": json.dumps(pdd_to_store), "st": status},
            )
    except IntegrityError as exc:
        # Two authors saving the same workflow at the same instant both computed the
        # same next version number and collided on the unique (definition, version)
        # constraint. That surfaced as a bare 500; say what happened instead.
        raise HTTPException(
            status_code=409,
            detail="somebody else saved this workflow at the same moment — "
                   "reload the Builder and save again") from exc
    return {"process_key": process_key, "version": new_version, "status": status, "warnings": warnings}


# WHY GET /v1/definitions: list processes with their latest version for a catalog UI.
@app.get("/v1/definitions")
async def list_definitions():
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT pd.process_key AS process_key, "
            "       MAX(dv.version) AS latest_version, "
            "       COUNT(*) FILTER (WHERE dv.status = 'published') AS published_versions "
            "FROM process_definition pd "
            "LEFT JOIN definition_version dv ON dv.definition_id = pd.id "
            "GROUP BY pd.process_key ORDER BY pd.process_key"
        )).mappings().all()
    return [dict(r) for r in rows]


# WHY GET /v1/definitions/{process_key}: fetch the latest PUBLISHED pdd (nodes,
# edges, forms) so a UI can render the process. 404 if none published.
@app.get("/v1/definitions/{process_key}")
async def get_definition(process_key: str):
    with engine.connect() as conn:
        pdd = conn.execute(text(
            "SELECT dv.pdd FROM definition_version dv "
            "JOIN process_definition pd ON pd.id = dv.definition_id "
            "WHERE pd.process_key = :pk AND dv.status = 'published' "
            "ORDER BY dv.version DESC LIMIT 1"
        ), {"pk": process_key}).scalar_one_or_none()
    if pdd is None:
        raise HTTPException(status_code=404, detail=f"No published definition for '{process_key}'")
    return pdd


# (The old /v1/active-process endpoint was removed: the engine now runs MANY
# workflows side by side. The Builder lists them via GET /v1/definitions and
# loads one with GET /v1/definitions/{process_key}.)


# WHY GET /v1/my-processes: the workflows the SIGNED-IN person may look at —
# the ones an admin assigned to them, or ALL of them for an admin/author.
# Deliberately a NEW endpoint: /v1/definitions must stay unfiltered because the
# email adapter uses it to find which workflow owns a mailbox.
@app.get("/v1/my-processes")
async def my_processes(user: dict = Depends(current_user)):
    with engine.connect() as conn:
        all_keys = conn.execute(text(
            "SELECT DISTINCT pd.process_key FROM process_definition pd "
            "JOIN definition_version dv ON dv.definition_id = pd.id "
            "WHERE dv.status = 'published' ORDER BY pd.process_key"
        )).scalars().all()
    allowed = _allowed_processes(user)          # None = unrestricted
    if allowed is None:
        return {"processes": list(all_keys), "unrestricted": True}
    return {"processes": [k for k in all_keys if k in set(allowed)], "unrestricted": False}


# ===========================================================================
# Admin identity (Ops/Admin, Section 5.8): manage Keycloak roles + users in-app,
# gated to 'ops_admin'. Per the spec, process authors REFERENCE roles while an
# admin MANAGES them (separation of duties). Uses the Keycloak Admin REST API.
# ===========================================================================


def _kc_admin():
    kc = os.getenv("KEYCLOAK_URL", "http://localhost:8081").rstrip("/")
    realm = os.getenv("KEYCLOAK_REALM", "workflow")
    try:
        token = requests.post(
            f"{kc}/realms/master/protocol/openid-connect/token",
            data={"client_id": "admin-cli", "grant_type": "password",
                  "username": os.getenv("KEYCLOAK_ADMIN", "admin"),
                  "password": os.getenv("KEYCLOAK_ADMIN_PASSWORD", "admin")},
            timeout=10,
        ).json().get("access_token")
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"keycloak unreachable: {exc}")
    if not token:
        raise HTTPException(status_code=502, detail="could not obtain Keycloak admin token")
    return kc, realm, {"Authorization": f"Bearer {token}"}


@app.get("/v1/admin/roles")
async def admin_list_roles(user: dict = Depends(require_role("ops_admin"))):
    kc, realm, h = _kc_admin()
    roles = _kc_check(requests.get(f"{kc}/admin/realms/{realm}/roles", headers=h, timeout=10),
                      "list roles").json()
    # An error object would iterate as its KEYS and silently produce [] — the Admin UI
    # then said "no roles exist" instead of reporting the failure.
    if not isinstance(roles, list):
        raise HTTPException(status_code=502, detail="Keycloak returned an unexpected role list")
    # Only real business roles — Keycloak's built-ins are noise in the Admin UI.
    return sorted(r["name"] for r in roles
                  if isinstance(r, dict) and r.get("name") and not _is_builtin_role(r["name"]))


class RoleIn(BaseModel):
    name: str


@app.post("/v1/admin/roles")
async def admin_create_role(body: RoleIn, user: dict = Depends(require_role("ops_admin"))):
    kc, realm, h = _kc_admin()
    r = requests.post(f"{kc}/admin/realms/{realm}/roles", headers=h, json={"name": body.name}, timeout=10)
    if r.status_code not in (201, 409):
        raise HTTPException(status_code=502, detail=f"create role failed ({r.status_code})")
    return {"name": body.name, "status": "exists" if r.status_code == 409 else "created"}


@app.get("/v1/admin/users")
async def admin_list_users(user: dict = Depends(require_role("ops_admin"))):
    kc, realm, h = _kc_admin()
    users = requests.get(f"{kc}/admin/realms/{realm}/users", headers=h, params={"max": 500}, timeout=10).json()
    out = []
    for u in (users if isinstance(users, list) else []):
        roles = []
        roles_ok = True
        try:
            rm = requests.get(f"{kc}/admin/realms/{realm}/users/{u.get('id')}/role-mappings/realm",
                              headers=h, timeout=10)
            data = rm.json() if rm.status_code == 200 else None
            if isinstance(data, list):
                roles = [r["name"] for r in data if isinstance(r, dict) and r.get("name")]
            else:
                roles_ok = False
        except Exception as exc:
            roles_ok = False
            print(f"admin: could not read roles for {u.get('username')!r}: {exc}")
        # roles_ok tells the UI the list is UNKNOWN, not empty. Swallowing the error
        # and returning [] was dangerous: the Admin UI loaded an empty role list, and
        # saving any other field then submitted roles=[] — which removes EVERY realm
        # role the person had. A silent privilege wipe caused by a transient error.
        out.append({"username": u.get("username"), "email": u.get("email"), "roles": roles,
                    "roles_ok": roles_ok,
                    "processes": _user_processes(u.get("username") or "") or []})
    return out


def _set_user_processes(username: str, processes: list | None) -> None:
    """Replace this person's workflow assignments (admin-controlled).

    Only a MISSING TABLE is tolerated (migration not run) — every other failure is
    raised. Swallowing all of them meant the admin saw "updated", the assignments
    were never stored, and the person's inbox stayed wrong with no clue why."""
    if processes is None:
        return
    keys = [str(p).strip() for p in processes if str(p).strip()]
    try:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM user_process WHERE username = :u"), {"u": username})
            for key in dict.fromkeys(keys):      # de-duplicate, keep order
                conn.execute(
                    text("INSERT INTO user_process (username, process_key) VALUES (:u, :p)"),
                    {"u": username, "p": key})
    except ProgrammingError as exc:
        print(f"user_process table missing; assignments for {username!r} not stored: {exc}")
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"the account was saved but its workflow assignments were not: {exc}") from exc


CORE_ROLES = {"ops_admin", "process_author"}


def _kc_check(resp, what: str):
    """Raise 502 with Keycloak's own message when an admin call fails.

    None of these calls used to be checked, so admin_update_user answered
    {"status": "updated"} even when Keycloak rejected the password policy, the
    duplicate username, or the whole request."""
    if resp.status_code >= 400:
        try:
            body = resp.json()
            detail = body.get("errorMessage") or body.get("error") or str(body)
        except Exception:
            detail = (resp.text or "").strip()[:200]
        raise HTTPException(status_code=502,
                            detail=f"{what} failed: Keycloak said {resp.status_code} {detail}")
    return resp


def _kc_user_id(kc, realm, h, username):
    found = requests.get(f"{kc}/admin/realms/{realm}/users", headers=h,
                         params={"username": username, "exact": "true"}, timeout=10).json()
    # isinstance guard: on an error Keycloak returns an OBJECT, and found[0] then
    # raised KeyError: 0 -> an opaque 500 during user update/delete.
    if isinstance(found, list) and found and isinstance(found[0], dict):
        return found[0].get("id")
    return None


@app.delete("/v1/admin/roles/{name}")
async def admin_delete_role(name: str, user: dict = Depends(require_role("ops_admin"))):
    if name in CORE_ROLES:
        raise HTTPException(status_code=400, detail=f"cannot delete core role '{name}'")
    kc, realm, h = _kc_admin()
    r = requests.delete(f"{kc}/admin/realms/{realm}/roles/{name}", headers=h, timeout=10)
    if r.status_code not in (204, 404):
        raise HTTPException(status_code=502, detail=f"delete role failed ({r.status_code})")
    return {"name": name, "status": "deleted"}


@app.delete("/v1/admin/users/{username}")
async def admin_delete_user(username: str, user: dict = Depends(require_role("ops_admin"))):
    if username == "admin1":
        raise HTTPException(status_code=400, detail="cannot delete the bootstrap admin")
    kc, realm, h = _kc_admin()
    uid = _kc_user_id(kc, realm, h, username)
    if not uid:
        raise HTTPException(status_code=404, detail=f"user '{username}' not found")
    r = requests.delete(f"{kc}/admin/realms/{realm}/users/{uid}", headers=h, timeout=10)
    if r.status_code not in (204, 404):
        raise HTTPException(status_code=502, detail=f"delete user failed ({r.status_code})")
    return {"username": username, "status": "deleted"}


class UserUpdate(BaseModel):
    email: str | None = None
    password: str | None = None
    roles: list[str] | None = None
    new_username: str | None = None
    processes: list[str] | None = None   # which workflows this person takes part in


@app.put("/v1/admin/users/{username}")
async def admin_update_user(username: str, body: UserUpdate, user: dict = Depends(require_role("ops_admin"))):
    kc, realm, h = _kc_admin()
    uid = _kc_user_id(kc, realm, h, username)
    if not uid:
        raise HTTPException(status_code=404, detail=f"user '{username}' not found")
    rep_resp = _kc_check(requests.get(f"{kc}/admin/realms/{realm}/users/{uid}",
                                      headers=h, timeout=10), "read user")
    rep = rep_resp.json()
    if not isinstance(rep, dict):
        raise HTTPException(status_code=502, detail="Keycloak returned an unexpected user record")
    changed = False
    if body.email is not None:
        rep["email"] = body.email
        rep["emailVerified"] = True
        changed = True
    if body.new_username:
        rep["username"] = body.new_username
        changed = True
    if changed:
        _kc_check(requests.put(f"{kc}/admin/realms/{realm}/users/{uid}", headers=h,
                               json=rep, timeout=10), "update user")
    if body.password:
        _kc_check(requests.put(
            f"{kc}/admin/realms/{realm}/users/{uid}/reset-password", headers=h,
            json={"type": "password", "value": body.password, "temporary": False},
            timeout=10), "set password")
    if body.roles is not None:
        cur_resp = _kc_check(requests.get(
            f"{kc}/admin/realms/{realm}/users/{uid}/role-mappings/realm",
            headers=h, timeout=10), "read current roles")
        current = cur_resp.json()
        # If the CURRENT roles cannot be read we must NOT proceed: `to_remove` would
        # be empty and `to_add` complete, or worse the caller's empty list would look
        # like "remove everything" against an unknown baseline.
        if not isinstance(current, list):
            raise HTTPException(status_code=502,
                                detail="could not read this user's current roles; nothing was changed")
        current = [r for r in current if isinstance(r, dict) and r.get("name")]
        want = set(body.roles)
        to_remove = [r for r in current if r["name"] not in want]
        if to_remove:
            _kc_check(requests.delete(
                f"{kc}/admin/realms/{realm}/users/{uid}/role-mappings/realm",
                headers=h, json=to_remove, timeout=10), "remove roles")
        have = {r["name"] for r in current}
        to_add = []
        missing_roles = []
        for role in want - have:
            rr = requests.get(f"{kc}/admin/realms/{realm}/roles/{role}", headers=h, timeout=10)
            if rr.status_code == 200:
                to_add.append({"id": rr.json()["id"], "name": role})
            else:
                missing_roles.append(role)
        if missing_roles:
            # Silently dropping unknown roles left the admin believing they were
            # granted; the user then hit 403s nobody could explain.
            raise HTTPException(
                status_code=400,
                detail=f"these roles do not exist in Keycloak: {', '.join(sorted(missing_roles))}")
        if to_add:
            _kc_check(requests.post(
                f"{kc}/admin/realms/{realm}/users/{uid}/role-mappings/realm",
                headers=h, json=to_add, timeout=10), "add roles")
    # Workflow assignments follow a rename so the person keeps their inbox.
    final_username = body.new_username or username
    if body.new_username and body.new_username != username:
        with engine.begin() as conn:
            conn.execute(text("UPDATE user_process SET username = :new WHERE username = :old"),
                         {"new": body.new_username, "old": username})
    _set_user_processes(final_username, body.processes)
    return {"username": final_username, "status": "updated"}


class UserIn(BaseModel):
    username: str
    email: str | None = None
    password: str = "12345"
    roles: list[str] = []
    processes: list[str] = []            # which workflows this person takes part in


@app.post("/v1/admin/users")
async def admin_create_user(body: UserIn, user: dict = Depends(require_role("ops_admin"))):
    kc, realm, h = _kc_admin()
    payload = {"username": body.username, "enabled": True, "firstName": body.username, "lastName": "User"}
    if body.email:
        payload["email"] = body.email
        payload["emailVerified"] = True
    created = requests.post(f"{kc}/admin/realms/{realm}/users", headers=h, json=payload, timeout=10)

    # Prefer the id Keycloak returns in the Location header (most reliable).
    uid = None
    if created.status_code in (200, 201):
        location = created.headers.get("Location") or ""
        if "/" in location:
            uid = location.rstrip("/").rsplit("/", 1)[-1]
    if not uid:
        uid = _kc_user_id(kc, realm, h, body.username)
    if not uid:
        # Surface the ACTUAL reason instead of a blank "create/lookup failed".
        # The usual causes are a duplicate username, or a duplicate EMAIL when the
        # realm has duplicateEmailsAllowed=false.
        try:
            reason = created.json()
            reason = reason.get("errorMessage") or reason.get("error") or str(reason)
        except Exception:
            reason = (created.text or "").strip()[:200]
        hint = ""
        low = str(reason).lower()
        if "email" in low and "exist" in low:
            hint = (" — another user already has this email. Use a different email, "
                    "or allow duplicate emails in the Keycloak realm settings.")
        elif "user exists" in low or created.status_code == 409:
            hint = " — that username already exists."
        raise HTTPException(
            status_code=400 if created.status_code in (400, 409) else 502,
            detail=f"Could not create user '{body.username}': "
                   f"Keycloak said {created.status_code} {reason or 'no detail'}{hint}")
    _kc_check(requests.put(
        f"{kc}/admin/realms/{realm}/users/{uid}/reset-password", headers=h,
        json={"type": "password", "value": body.password, "temporary": False},
        timeout=10), "set password")
    assign = []
    missing_roles = []
    for role in body.roles:
        rr = requests.get(f"{kc}/admin/realms/{realm}/roles/{role}", headers=h, timeout=10)
        if rr.status_code == 200:
            assign.append({"id": rr.json()["id"], "name": role})
        else:
            missing_roles.append(role)
    if assign:
        _kc_check(requests.post(f"{kc}/admin/realms/{realm}/users/{uid}/role-mappings/realm",
                                headers=h, json=assign, timeout=10), "assign roles")
    _set_user_processes(body.username, body.processes)
    return {"username": body.username, "roles_assigned": [a["name"] for a in assign],
            # Reported rather than silently dropped, so the admin knows the account
            # exists but is missing a role they asked for.
            "roles_not_found": missing_roles,
            "processes": body.processes}


# ===========================================================================
# Read-only views (Section 11 prep): feed a monitor/dashboard UI — list recent
# runs and replay a single run's audit timeline. Left open (no auth) like the
# other GETs.
# ===========================================================================


# WHY GET /v1/transactions/stats: per-status counts across ALL transactions so
# the Monitor's KPI boxes reflect the full dataset, not just the current page.
@app.get("/v1/transactions/stats")
async def transaction_stats(process_key: str | None = None,
                            user: dict = Depends(current_user)):
    # Counts for ONE workflow when process_key is given, else across all the
    # workflows this person is allowed to see. Requires a token: an anonymous caller
    # used to be treated as "unrestricted" and got totals across every workflow.
    clauses = []
    params: dict = {}
    if process_key:
        clauses.append('tr.definition_version_id IN (SELECT dv.id FROM definition_version dv '
                       "JOIN process_definition pd ON pd.id = dv.definition_id "
                       "WHERE pd.process_key = :pk)")
        params["pk"] = process_key
    allowed = _allowed_processes(user)
    scope = _process_scope_sql(allowed, "tr.id").strip()
    if scope:
        clauses.append(scope[4:] if scope.startswith("AND ") else scope)
        params.update(_process_scope_params(allowed))
    where = (" WHERE " + " AND ".join(clauses) + " ") if clauses else ""
    with engine.connect() as conn:
        rows = conn.execute(
            _scoped_text(
                f'SELECT tr.status AS status, count(*) AS n FROM "transaction" tr{where} '
                "GROUP BY tr.status", allowed if scope else None),
            params,
        ).mappings().all()
    counts = {r["status"]: r["n"] for r in rows}
    known = ("running", "approved", "rejected")
    return {
        "total": sum(counts.values()),
        "running": counts.get("running", 0),
        "approved": counts.get("approved", 0),
        "rejected": counts.get("rejected", 0),
        # Workflows may end in any outcome the author names (paid, declined,
        # completed…), and a failed start is 'failed'. Without these the KPI boxes
        # simply did not add up to the total, with nowhere for the rest to go.
        "other": sum(n for s, n in counts.items() if s not in known),
        "by_status": counts,
    }


# WHY GET /v1/transactions: the dashboard's "recent runs" list. Optional
# status/limit/offset keep the RESPONSE SHAPE (a JSON array) backward compatible
# for existing callers (vendor inbox, email adapter) while enabling the Monitor's
# pagination + KPI filtering.
@app.get("/v1/transactions")
async def list_transactions(status: str | None = None, limit: int = 100, offset: int = 0,
                            process_key: str | None = None, ids: str | None = None,
                            user: dict = Depends(current_user)):
    # process_key gives each workflow its OWN monitor view (generic — the value
    # comes from whatever workflows exist, nothing is hardcoded). Rows are ALSO
    # limited to the workflows this person is assigned to, so a business user cannot
    # see another team's runs. Admins/authors see everything.
    #
    # Requires a token now. Anonymous callers used to be "unrestricted" and could
    # read every workflow's runs INCLUDING each data_snapshot. The email adapter does
    # not use this endpoint (it only POSTs transactions), so nothing else is affected.
    #
    # `ids` (comma-separated) fetches specific transactions regardless of recency, so
    # the Task Inbox can resolve a long-pending task's request instead of hoping it is
    # still inside the newest 100 rows.
    limit = max(1, min(int(limit), 200))
    offset = max(0, int(offset))
    allowed = _allowed_processes(user)
    scope_sql = _process_scope_sql(allowed, "tr.id")
    id_list = []
    if ids:
        id_list = [_uuid_or_422(i.strip(), "ids") for i in ids.split(",") if i.strip()][:200]
    id_sql = " AND tr.id IN :want_ids " if id_list else ""
    stmt_sql = (
        "SELECT tr.id, pd.process_key, tr.definition_version_id, "
        "       dv.version AS definition_version, tr.status, tr.data_snapshot, "
        "       tr.submitted_by, "
        "       tr.created_at, tr.closed_at, "
        "       COALESCE(tr.temporal_workflow_id, CAST(tr.id AS text)) AS temporal_workflow_id, "
        "       tr.temporal_run_id "
        'FROM "transaction" tr '
        "LEFT JOIN definition_version dv ON dv.id = tr.definition_version_id "
        "LEFT JOIN process_definition pd ON pd.id = dv.definition_id "
        "WHERE (CAST(:status AS text) IS NULL OR tr.status = CAST(:status AS text)) "
        "AND (CAST(:pk AS text) IS NULL OR pd.process_key = CAST(:pk AS text)) "
        + scope_sql + id_sql +
        "ORDER BY tr.created_at DESC, tr.id DESC LIMIT :limit OFFSET :offset"
    )
    stmt = _scoped_text(stmt_sql, allowed)
    if id_list:
        stmt = stmt.bindparams(bindparam("want_ids", expanding=True))
    with engine.connect() as conn:
        rows = conn.execute(
            stmt,
            {"status": status, "pk": process_key, "limit": limit, "offset": offset,
             **_process_scope_params(allowed),
             **({"want_ids": tuple(id_list)} if id_list else {})},
        ).mappings().all()
    # Stringify uuid/timestamp so the payload is JSON-serializable; data_snapshot
    # is jsonb and already deserializes to a dict.
    return [
        {
            "id": str(r["id"]),
            "process_key": r["process_key"],
            "definition_version_id": (
                str(r["definition_version_id"]) if r["definition_version_id"] is not None else None
            ),
            "definition_version": r["definition_version"],
            "status": r["status"],
            "data_snapshot": r["data_snapshot"],
            "submitted_by": r["submitted_by"],
            "created_at": str(r["created_at"]) if r["created_at"] is not None else None,
            "closed_at": str(r["closed_at"]) if r["closed_at"] is not None else None,
            "temporal_workflow_id": r["temporal_workflow_id"],
            "temporal_run_id": r["temporal_run_id"],
        }
        for r in rows
    ]


# WHY GET /v1/transactions/{txn_id}/history: replay ONE run's immutable audit log
# in order — the timeline a monitor UI shows (started -> LLM decision -> task ->
# notify -> ... -> outcome).
@app.get("/v1/transactions/{txn_id}/history")
async def transaction_history(txn_id: str, format: str | None = None,
                              user: dict = Depends(current_user)):
    # The audit log is the most sensitive read in the API — every decision, every
    # rejection reason, every vote and the full request data. It had NO auth and NO
    # scope check at all, so anyone who knew (or guessed from the open list endpoint)
    # a transaction id could replay another team's approvals.
    txn_id = _uuid_or_422(txn_id, "transaction_id")
    _require_process_access(user, txn_id)
    with engine.connect() as conn:
        # 404 rather than returning an empty list for a txn that doesn't exist.
        exists = conn.execute(
            text('SELECT 1 FROM "transaction" WHERE id = CAST(:id AS uuid)'),
            {"id": txn_id},
        ).first()
        if exists is None:
            raise HTTPException(status_code=404, detail="transaction not found")

        rows = conn.execute(
            text(
                "SELECT type, actor, payload, occurred_at FROM event "
                "WHERE transaction_id = CAST(:id AS uuid) "
                "ORDER BY occurred_at, seq"
            ),
            {"id": txn_id},
        ).mappings().all()

    out = []
    for r in rows:
        payload = r["payload"]  # jsonb -> dict (or None)
        detail = payload.get("detail") if isinstance(payload, dict) else None
        out.append(
            {
                "type": r["type"],
                "actor": r["actor"],
                "payload": payload,
                "occurred_at": str(r["occurred_at"]) if r["occurred_at"] is not None else None,
                "detail": detail,
            }
        )
    if format == "narrative":
        # LLM refinement is blocking HTTP — run on a thread so a slow model never
        # stalls the API event loop.
        import asyncio
        return await asyncio.to_thread(_narrate_events, txn_id, out)
    return out


# ---------------------------------------------------------------------------
# AI-narrated audit (Monitor drill-down). Deterministic per-event formatter is
# the SOURCE OF TRUTH fallback; an LLM pass (same env-configured Ollama client
# as ai_review: LLM_BASE_URL / LLM_MODEL, bounded by NARRATE_TIMEOUT) may refine
# the sentences. The Monitor must NEVER see raw JSON or hang on a slow model.
# ---------------------------------------------------------------------------

_ROUTE_TEXT = {
    "AUTO_APPROVE": "automatic approval",
    "REQUEST_INFO": "a request for missing information",
    "MANAGER_ONLY": "manager approval",
    "MANAGER_THEN_FINANCE": "manager approval followed by a finance quorum",
}

# In-memory narrative cache keyed by (txn_id, event_count, last_timestamp): a
# repeat view of an unchanged history never re-calls the LLM. (In-memory chosen
# over a DB table: narratives are cheap to regenerate and per-process caching is
# enough for the single-API dev topology; swap for a table if scaled out.)
_NARRATIVE_CACHE: dict = {}


def _fallback_sentence(event: dict) -> str:
    # Deterministic, code-built plain-English sentence for one audit event.
    #
    # WORDING IS WORKFLOW-NEUTRAL. It used to say "invoice", "vendor", "finance
    # member" and "ERP system" for EVERY process, so a leave request's or a refund's
    # audit trail read as somebody else's invoice story.
    # It also never raises: these sentences are built OUTSIDE the try/except that
    # guards the LLM pass, so a payload whose "data" was a list (not a dict), or a
    # missing-fields list holding non-strings, made the whole narrative endpoint 500
    # while the plain history worked fine.
    etype = event.get("type")
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}

    def _names(value) -> list:
        if isinstance(value, dict):
            return [str(k) for k in value.keys()]
        if isinstance(value, (list, tuple, set)):
            return [str(v) for v in value]
        return [str(value)] if value not in (None, "") else []

    try:
        if etype == "WORKFLOW_RUNNING":
            return "The request was received and the workflow started."
        if etype == "LLM_DECISION":
            route = _ROUTE_TEXT.get(payload.get("route"), payload.get("route") or "review")
            missing = _names(payload.get("missing"))
            extra = f" (still needed: {', '.join(missing)})" if missing else ""
            return f"The request was assessed and routed to {route}{extra}."
        if etype == "TASK_CREATED":
            role = payload.get("role") or "a user"
            node = payload.get("node_id") or "a step"
            return f"A task for role '{role}' was created at the '{node}' step."
        if etype == "NOTIFY":
            recipients = _names(payload.get("recipients") or payload.get("recipient"))
            base = "A notification email was sent" + (f" to {', '.join(recipients)}" if recipients else "")
            reason = payload.get("reason")
            detail = event.get("detail") or ""
            what = detail.split(":", 1)[1].strip() if ":" in detail else detail
            return f"{base}: {what}." + (f" Reason: {reason}." if reason else "")
        if etype == "HUMAN_DECISION":
            decision = payload.get("decision")
            if decision == "resubmit":
                fields = ", ".join(_names(payload.get("data"))) or "the requested details"
                return f"The requester supplied the information asked for ({fields})."
            reason = payload.get("reason")
            return (f"A reviewer decided to {decision or 'act'}."
                    + (f" Reason: {reason}." if reason else ""))
        if etype == "FINANCE_VOTE":
            who = payload.get("participant") or "An approver"
            decision = payload.get("decision") or "vote"
            reason = payload.get("reason")
            return f"Approver {who} voted to {decision}." + (f" Reason: {reason}." if reason else "")
        if etype == "ERP_POSTED":
            return "The approved request was posted to the system of record."
    except Exception as exc:                # never break the whole narrative
        return f"{etype or 'An'} event was recorded (details unreadable: {exc})."
    return f"{etype or 'An'} event was recorded."


def _narrate_events(txn_id: str, events: list) -> list:
    if not events:
        return []
    cache_key = (txn_id, len(events), events[-1]["occurred_at"])
    cached = _NARRATIVE_CACHE.get(cache_key)
    if cached is not None:
        return cached

    fallback = [_fallback_sentence(e) for e in events]
    sentences = fallback
    try:
        # ONE bounded LLM call for the whole history (model-agnostic: strict JSON
        # contract, no model-specific parsing). Any failure -> deterministic text.
        import re as _re
        from openai import OpenAI

        client = OpenAI(
            base_url=os.getenv("LLM_BASE_URL", "http://localhost:11434/v1"),
            api_key="ollama",
            timeout=float(os.getenv("NARRATE_TIMEOUT", "30")),
        )
        brief = [
            {"type": e["type"], "detail": e.get("detail"), "payload": e.get("payload")}
            for e in events
        ]
        completion = client.chat.completions.create(
            model=os.getenv("LLM_MODEL", "llama3.2:1b"),
            temperature=0,
            messages=[{
                "role": "user",
                "content": (
                    "Rewrite each workflow audit event below as ONE clear, concise, "
                    "past-tense sentence for a business user. Return ONLY a JSON array "
                    f"of exactly {len(brief)} strings, in the same order.\n\n"
                    f"Events: {json.dumps(brief, default=str)[:6000]}\n\n"
                    f"Draft sentences you may improve: {json.dumps(fallback)}"
                ),
            }],
        )
        content = completion.choices[0].message.content or ""
        cleaned = _re.sub(r"^```(?:json)?|```$", "", content.strip(), flags=_re.MULTILINE).strip()
        match = _re.search(r"\[.*\]", cleaned, _re.DOTALL)
        parsed = json.loads(match.group(0) if match else cleaned)
        if (
            isinstance(parsed, list)
            and len(parsed) == len(events)
            and all(isinstance(s, str) and s.strip() for s in parsed)
        ):
            sentences = [s.strip() for s in parsed]
    except Exception as exc:
        print(f"narrate: LLM unavailable, using deterministic text: {exc}")

    result = [
        {"occurred_at": e["occurred_at"], "type": e["type"], "text": s}
        for e, s in zip(events, sentences)
    ]
    _NARRATIVE_CACHE[cache_key] = result
    return result
