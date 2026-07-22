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
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from temporalio.client import Client
from temporalio.service import RPCError

# WHY sys.path manipulation here (unlike the workflow file): the API is a normal
# process, NOT a Temporal workflow, so it is free to touch the filesystem and
# adjust sys.path. We add the worker's `workflows` and `activities` dirs so we
# can import InvoiceWorkflow (which in turn imports invoice_activities by bare
# name). This file lives at services/api/app/main.py, so services/ is parents[2].
SERVICES_DIR = Path(__file__).resolve().parents[2]
for _sub in ("workflows", "activities"):
    _d = SERVICES_DIR / "worker" / _sub
    if str(_d) not in sys.path:
        sys.path.insert(0, str(_d))
# scripts/ holds the shared PDD validator (also used by the CLI and seeds).
_SCRIPTS_DIR = SERVICES_DIR.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from process_interpreter import ProcessInterpreterWorkflow  # noqa: E402  (generic engine)
from pdd_validation import validate_pdd  # noqa: E402  (structural PDD checks)

# Module-level SQLAlchemy engine: created once, connection-pooled, reused by every
# request. (These calls are synchronous/blocking; fine for the MVP. Under real
# load the DB work should be offloaded to a thread, e.g. asyncio.to_thread.)
# Connection targets are env-driven so the SAME code runs both as a host process
# (systemd; defaults point at localhost) and inside a container (compose sets these
# to Docker service names: postgres / temporal).
DB_URL = os.getenv("DATABASE_URL", "postgresql+psycopg://app:app@localhost:5432/workflow_app")
engine = create_engine(DB_URL)
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
from fastapi.middleware.cors import CORSMiddleware

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_credentials=True,
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
        return {
            "username": "dev-admin",
            "roles": ["vendor", "ap_manager", "finance", "process_author", "admin"],
        }

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
    except Exception:
        # Any failure (bad signature, expired, malformed) -> 401.
        raise HTTPException(status_code=401, detail="invalid or expired token")

    roles = claims.get("realm_access", {}).get("roles", []) or []
    return {"username": claims.get("preferred_username") or claims.get("sub"), "roles": roles}


def require_role(role: str):
    # Returns a FastAPI dependency that 403s unless the caller holds `role`. Handy
    # for whole-endpoint gating.
    # NOTE (Section 10): publishing a definition version would be gated with
    # Depends(require_role("process_author")) — but no publish endpoint exists yet,
    # so this helper is provided for when one is added.
    async def _dep(user: dict = Depends(current_user)) -> dict:
        if role not in user["roles"]:
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
    if not authorization:
        return None
    try:
        return await current_user(authorization)
    except HTTPException:
        return None


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
    if any((n.get("completion") or {}).get("mode") == "quorum" for n in pdd.get("nodes", [])):
        _validate_author_finance_config(cfg)

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
    # Start the GENERIC interpreter with the full PDD (nodes/edges). The interpreter
    # derives cfg/roles from it and walks the graph — invoice is just one PDD.
    await app.state.temporal.start_workflow(
        ProcessInterpreterWorkflow.run,
        args=[txn_id, pdd],
        id=txn_id,
        task_queue="invoice-tq",
    )

    # 4. Hand the caller the id they use to track/act on this run.
    return {"transaction_id": txn_id}


# WHY POST /v1/extract (and legacy alias /v1/extract-invoice): document-upload
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
    return ["vendor", "amount", "poNumber", "costCenter", "taxId"]  # invoice default


@app.post("/v1/extract")
@app.post("/v1/extract-invoice")
async def extract_document(file: UploadFile = File(...),
                           process_key: str = Form("invoice_approval")):
    raw = await file.read()
    fields = _pdd_extract_fields(process_key)
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
@app.post("/v1/events")
async def ingest_event(evt: EventIn):
    if evt.kind == "finance":
        raise HTTPException(
            status_code=400,
            detail="Finance decisions must use the task complete endpoint after claiming a participant slot",
        )
    # The audit event's type reflects what kind of decision this is.
    event_type = "FINANCE_VOTE" if evt.kind == "finance" else "HUMAN_DECISION"
    event_id = str(uuid.uuid4())

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
                    "actor": "EventIngress",
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
                updated = conn.execute(
                    text(
                        "UPDATE task SET status = 'done', "
                        "claimed_by = COALESCE(:claimed_by, claimed_by) "
                        "WHERE token = :token AND status <> 'done'"
                    ),
                    {"claimed_by": evt.payload.get("claimed_by"), "token": evt.task_token},
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
    handle = app.state.temporal.get_workflow_handle(evt.transaction_id)
    sig = "finance_vote" if evt.kind == "finance" else "human_decision"
    try:
        await handle.signal(sig, evt.payload)
    except RPCError as e:
        # A decision can legitimately arrive AFTER the workflow finished (late email reply,
        # retry, double-submit). The decision is already recorded in the event log above;
        # there is simply no running workflow left to resume, so treat it as a graceful
        # no-op rather than a server error.
        msg = str(e).lower()
        if "already completed" in msg or "not found" in msg:
            return {"status": "workflow-already-closed"}
        raise   # any other RPC error is a real failure — surface it

    return {"status": "accepted"}


# WHY GET /v1/transactions/{txn_id}/open-task: an OPEN read the email adapter uses
# to resolve a transaction's CURRENT open human task — its token (to complete the
# exact task, enabling first-wins dedup with the app), node_id (resubmit vs
# approve/reject) and missing fields. Returns {"open_task": null} when none is open.
@app.get("/v1/transactions/{txn_id}/open-task")
async def transaction_open_task(txn_id: str):
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT token, node_id, assigned_role, completion_policy "
                "FROM task WHERE transaction_id = CAST(:t AS uuid) AND status = 'open' "
                "ORDER BY created_at DESC LIMIT 1"
            ),
            {"t": txn_id},
        ).mappings().first()
    if row is None:
        return {"open_task": None}
    policy = row["completion_policy"] if isinstance(row["completion_policy"], dict) else {}
    need = policy.get("need") if isinstance(policy.get("need"), list) else None
    return {
        "open_task": {
            "token": row["token"],
            "node_id": row["node_id"],
            "assigned_role": row["assigned_role"],
            "need": need,
        }
    }


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
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT id, transaction_id, node_id, token, assigned_role, "
                "       status, claimed_by, completion_policy, created_at "
                "FROM task "
                "WHERE status = :status "
                "AND (CAST(:role AS text) IS NULL OR assigned_role = CAST(:role AS text)) "
                "ORDER BY created_at"
            ),
            {"status": status, "role": role},
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
            if row["node_id"] == "finance":
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
class ClaimIn(BaseModel):
    claimed_by: str


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
            text("SELECT assigned_role, node_id FROM task WHERE token = :token"),
            {"token": token},
        ).mappings().first()
    assigned_role = task_row["assigned_role"] if task_row is not None else None
    if assigned_role is not None and assigned_role not in user["roles"]:
        raise HTTPException(status_code=403, detail=f"requires role '{assigned_role}'")

    if task_row is not None and task_row["node_id"] == "finance":
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
    with engine.begin() as conn:
        row = conn.execute(
            text(
                "UPDATE task SET claimed_by = :who, status = 'claimed' "
                "WHERE token = :token AND status = 'open' "
                "RETURNING id, token, claimed_by, status"
            ),
            {"who": body.claimed_by, "token": token},
        ).mappings().first()

    if row is None:
        # No row flipped => not open: already claimed by someone else, or the
        # token doesn't exist / task isn't in 'open' state.
        raise HTTPException(
            status_code=409,
            detail="task not open (already claimed or does not exist)",
        )
    return {"status": "claimed", "token": token, "claimed_by": body.claimed_by}


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
                    "SELECT id, transaction_id, status, completion_policy FROM task "
                    "WHERE token = :token AND node_id = 'finance' FOR UPDATE"
                ),
                {"token": token},
            ).mappings().first()
            if task_row is None:
                raise HTTPException(status_code=404, detail="Finance task not found")
            if task_row["status"] != "open":
                raise HTTPException(status_code=409, detail="Finance task is no longer open")

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
            text("SELECT transaction_id, assigned_role, node_id FROM task WHERE token = :token"),
            {"token": token},
        ).mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail="task not found")

    # Role gate (Section 10): the caller must hold this task's assigned_role.
    assigned_role = row["assigned_role"]
    if assigned_role is not None and assigned_role not in user["roles"]:
        raise HTTPException(status_code=403, detail=f"requires role '{assigned_role}'")

    if row["node_id"] == "finance":
        return await _complete_finance_task(token, body, user)

    evt = EventIn(
        transaction_id=str(row["transaction_id"]),
        task_token=token,
        idempotency_key=body.idempotency_key,
        kind=body.kind,
        payload=body.payload,
    )
    return await ingest_event(evt)


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
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT dv.id AS version_id, dv.pdd AS pdd FROM definition_version dv "
                "JOIN process_definition pd ON pd.id = dv.definition_id "
                "WHERE pd.process_key = :pk AND dv.status = 'published' "
                "ORDER BY dv.version DESC "
                "LIMIT 1"
            ),
            {"pk": process_key},
        ).mappings().first()

    if row is None:
        raise HTTPException(
            status_code=404,
            detail=f"No published definition found for process_key '{process_key}'",
        )

    # Replace ONLY the "config" key; keep process_key, version, roles, and any
    # other pdd fields intact.
    new_pdd = {**row["pdd"], "config": body.config}
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE definition_version SET pdd = CAST(:pdd AS jsonb) WHERE id = CAST(:id AS uuid)"),
            {"pdd": json.dumps(new_pdd), "id": str(row["version_id"])},
        )

    # WHY new transactions pick this up automatically: POST /v1/transactions reads
    # cfg FRESH from the published pdd at start time, so the very next invoice runs
    # with these new values (this mirrors the prototype's Config tab). In-flight
    # workflows keep the cfg snapshot they were started with — only new runs change.
    return {"status": "updated", "process_key": process_key, "config": body.config}


# ===========================================================================
# Definition Service (Section 5.1 / P3): author, validate, and publish PDDs.
# Static validation on publish (graph structure + referenced roles exist in
# Keycloak). New versions are immutable; in-flight transactions stay pinned.
# ===========================================================================


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


def _check_pdd(pdd: dict) -> tuple[list, list]:
    # Structural validation (shared validator) + Keycloak role-existence.
    errors, warnings = validate_pdd(pdd)
    roles_map = pdd.get("roles", {}) if isinstance(pdd.get("roles"), dict) else {}
    realm_roles = _keycloak_realm_roles()
    if realm_roles is None:
        warnings.append("could not reach Keycloak to verify roles exist")
    else:
        for logical, kc_role in roles_map.items():
            if kc_role not in realm_roles:
                errors.append(f"role '{kc_role}' (mapped from '{logical}') does not exist in Keycloak")
    return errors, warnings


class DefinitionIn(BaseModel):
    pdd: dict
    publish: bool = True


# WHY POST /v1/definitions/validate: dry-run the checks so an author sees errors
# BEFORE publishing. Open (read-like); it writes nothing.
@app.post("/v1/definitions/validate")
async def validate_definition(body: DefinitionIn):
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
    with engine.begin() as conn:
        definition_id = conn.execute(
            text("SELECT id FROM process_definition WHERE process_key = :pk"),
            {"pk": process_key},
        ).scalar_one_or_none()
        if definition_id is None:
            definition_id = uuid.uuid4()
            conn.execute(
                text("INSERT INTO process_definition (id, process_key) VALUES (CAST(:id AS uuid), :pk)"),
                {"id": str(definition_id), "pk": process_key},
            )
        max_version = conn.execute(
            text("SELECT COALESCE(MAX(version), 0) FROM definition_version "
                 "WHERE definition_id = CAST(:d AS uuid)"),
            {"d": str(definition_id)},
        ).scalar_one()
        new_version = int(max_version) + 1
        pdd_to_store = {**pdd, "version": new_version}  # keep the stored version authoritative
        conn.execute(
            text("INSERT INTO definition_version "
                 "(id, definition_id, version, pdd, status, published_at) "
                 "VALUES (CAST(:id AS uuid), CAST(:d AS uuid), :v, CAST(:pdd AS jsonb), :st, "
                 "        CASE WHEN :st = 'published' THEN now() ELSE NULL END)"),
            {"id": str(uuid.uuid4()), "d": str(definition_id), "v": new_version,
             "pdd": json.dumps(pdd_to_store), "st": status},
        )
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
    roles = requests.get(f"{kc}/admin/realms/{realm}/roles", headers=h, timeout=10).json()
    return sorted(r["name"] for r in roles if isinstance(r, dict) and r.get("name"))


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
    return [{"username": u.get("username"), "email": u.get("email")}
            for u in (users if isinstance(users, list) else [])]


class UserIn(BaseModel):
    username: str
    email: str | None = None
    password: str = "12345"
    roles: list[str] = []


@app.post("/v1/admin/users")
async def admin_create_user(body: UserIn, user: dict = Depends(require_role("ops_admin"))):
    kc, realm, h = _kc_admin()
    payload = {"username": body.username, "enabled": True, "firstName": body.username, "lastName": "User"}
    if body.email:
        payload["email"] = body.email
        payload["emailVerified"] = True
    requests.post(f"{kc}/admin/realms/{realm}/users", headers=h, json=payload, timeout=10)  # 409 if exists: fine
    found = requests.get(f"{kc}/admin/realms/{realm}/users", headers=h,
                         params={"username": body.username, "exact": "true"}, timeout=10).json()
    if not found:
        raise HTTPException(status_code=502, detail="user create/lookup failed")
    uid = found[0]["id"]
    requests.put(f"{kc}/admin/realms/{realm}/users/{uid}/reset-password", headers=h,
                 json={"type": "password", "value": body.password, "temporary": False}, timeout=10)
    assign = []
    for role in body.roles:
        rr = requests.get(f"{kc}/admin/realms/{realm}/roles/{role}", headers=h, timeout=10)
        if rr.status_code == 200:
            assign.append({"id": rr.json()["id"], "name": role})
    if assign:
        requests.post(f"{kc}/admin/realms/{realm}/users/{uid}/role-mappings/realm", headers=h, json=assign, timeout=10)
    return {"username": body.username, "roles_assigned": [a["name"] for a in assign]}


# ===========================================================================
# Read-only views (Section 11 prep): feed a monitor/dashboard UI — list recent
# runs and replay a single run's audit timeline. Left open (no auth) like the
# other GETs.
# ===========================================================================


# WHY GET /v1/transactions/stats: per-status counts across ALL transactions so
# the Monitor's KPI boxes reflect the full dataset, not just the current page.
@app.get("/v1/transactions/stats")
async def transaction_stats():
    with engine.connect() as conn:
        rows = conn.execute(
            text('SELECT status, count(*) AS n FROM "transaction" GROUP BY status')
        ).mappings().all()
    counts = {r["status"]: r["n"] for r in rows}
    return {
        "total": sum(counts.values()),
        "running": counts.get("running", 0),
        "approved": counts.get("approved", 0),
        "rejected": counts.get("rejected", 0),
    }


# WHY GET /v1/transactions: the dashboard's "recent runs" list. Optional
# status/limit/offset keep the RESPONSE SHAPE (a JSON array) backward compatible
# for existing callers (vendor inbox, email adapter) while enabling the Monitor's
# pagination + KPI filtering.
@app.get("/v1/transactions")
async def list_transactions(status: str | None = None, limit: int = 100, offset: int = 0):
    limit = max(1, min(int(limit), 100))
    offset = max(0, int(offset))
    with engine.connect() as conn:
        rows = conn.execute(
            text(
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
                "ORDER BY tr.created_at DESC, tr.id DESC LIMIT :limit OFFSET :offset"
            ),
            {"status": status, "limit": limit, "offset": offset},
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
async def transaction_history(txn_id: str, format: str | None = None):
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
    etype = event.get("type")
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    if etype == "WORKFLOW_RUNNING":
        return "The invoice workflow started."
    if etype == "LLM_DECISION":
        route = _ROUTE_TEXT.get(payload.get("route"), payload.get("route") or "review")
        missing = payload.get("missing") or []
        extra = f" (missing: {', '.join(missing)})" if missing else ""
        return f"The AI reviewed the invoice and routed it to {route}{extra}."
    if etype == "TASK_CREATED":
        role = payload.get("role") or "a user"
        node = payload.get("node_id") or "a step"
        return f"A human task for role '{role}' was created at the '{node}' step."
    if etype == "NOTIFY":
        recipient = payload.get("recipient")
        base = "A notification email was sent" + (f" to {recipient}" if recipient else "")
        reason = payload.get("reason")
        detail = event.get("detail") or ""
        what = detail.split(":", 1)[1].strip() if ":" in detail else detail
        return f"{base}: {what}." + (f" Reason: {reason}." if reason else "")
    if etype == "HUMAN_DECISION":
        decision = payload.get("decision")
        if decision == "resubmit":
            fields = ", ".join((payload.get("data") or {}).keys()) or "the requested details"
            return f"The vendor supplied the requested information ({fields})."
        reason = payload.get("reason")
        return f"A reviewer decided to {decision or 'act'}." + (f" Reason: {reason}." if reason else "")
    if etype == "FINANCE_VOTE":
        who = payload.get("participant") or "A finance member"
        decision = payload.get("decision") or "vote"
        reason = payload.get("reason")
        return f"Finance member {who} voted to {decision}." + (f" Reason: {reason}." if reason else "")
    if etype == "ERP_POSTED":
        return "The approved invoice was posted to the ERP system."
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
