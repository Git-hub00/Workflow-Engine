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
from jwt import PyJWKClient
from fastapi import Depends, FastAPI, Header, HTTPException
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

from invoice_workflow import InvoiceWorkflow  # noqa: E402  (after sys.path tweak)

# Module-level SQLAlchemy engine: created once, connection-pooled, reused by every
# request. (These calls are synchronous/blocking; fine for the MVP. Under real
# load the DB work should be offloaded to a thread, e.g. asyncio.to_thread.)
DB_URL = "postgresql+psycopg://app:app@localhost:5432/workflow_app"
engine = create_engine(DB_URL)


# WHY lifespan: connecting to Temporal is relatively costly and should happen
# ONCE, not per request. We open a single client at startup and stash it on
# app.state so every request reuses the same connection; it is released when the
# app shuts down. (temporalio's Client has no explicit close() — its underlying
# connection is torn down when the process/loop ends.)
@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.temporal = await Client.connect("localhost:7233")
    yield


app = FastAPI(title="Workflow Engine API", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Keycloak JWT auth (Section 10). Tokens are RS256, signed by the "workflow"
# realm; we fetch the realm's public signing keys via JWKS and verify the
# signature + expiry on every request. Roles come from realm_access.roles.
# ---------------------------------------------------------------------------

# JWKS endpoint for the "workflow" realm. PyJWKClient fetches the signing keys
# lazily (on first use), so importing this module does no network I/O.
KEYCLOAK_JWKS_URL = "http://localhost:8081/realms/workflow/protocol/openid-connect/certs"

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
            "roles": ["ap_clerk", "ap_manager", "finance", "process_author", "admin"],
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
    return {"username": claims.get("preferred_username"), "roles": roles}


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


# WHY TransactionIn: the request contract for starting a run. `process_key`
# selects WHICH process definition to run; `data` is the raw invoice payload that
# becomes the transaction's data_snapshot (and what extract_fields reads back).
class TransactionIn(BaseModel):
    process_key: str
    data: dict


# WHY /health: a trivial liveness probe for compose/k8s and quick manual checks.
@app.get("/health")
async def health():
    return {"status": "ok"}


# WHY POST /v1/transactions: the public "start an invoice" endpoint. This is the
# productized version of what the e2e test did manually — resolve the published
# definition, insert the transaction row, and launch the workflow.
@app.post("/v1/transactions")
async def create_transaction(body: TransactionIn):
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
    cfg = {**pdd["config"], "roles": pdd["roles"]}

    # 2. Create the transaction row in ONE transaction: new uuid, linked to the
    #    resolved definition_version, status 'running', snapshot = posted data.
    txn_id = str(uuid.uuid4())
    with engine.begin() as conn:
        conn.execute(
            text(
                'INSERT INTO "transaction" (id, definition_version_id, status, data_snapshot) '
                "VALUES (CAST(:id AS uuid), CAST(:dv AS uuid), :st, CAST(:snap AS jsonb))"
            ),
            {
                "id": txn_id,
                "dv": str(row["version_id"]),
                "st": "running",
                "snap": json.dumps(body.data),
            },
        )

    # 3. Start the Temporal workflow, using the transaction id as the workflow id
    #    (one workflow per transaction). The reused app.state.temporal client
    #    dispatches to the main "invoice-tq" queue the worker polls.
    await app.state.temporal.start_workflow(
        InvoiceWorkflow.run,
        args=[txn_id, cfg],
        id=txn_id,
        task_queue="invoice-tq",
    )

    # 4. Hand the caller the id they use to track/act on this run.
    return {"transaction_id": txn_id}


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

            # (c) best-effort: mark the human task done (only if a token was given).
            # COALESCE keeps any existing claimed_by when the payload doesn't carry one.
            if evt.task_token is not None:
                conn.execute(
                    text(
                        "UPDATE task SET status = 'done', "
                        "claimed_by = COALESCE(:claimed_by, claimed_by) "
                        "WHERE token = :token"
                    ),
                    {"claimed_by": evt.payload.get("claimed_by"), "token": evt.task_token},
                )
    except IntegrityError:
        if dup:
            # Already processed — safe no-op. No signal, no other side effects.
            return {"status": "duplicate-ignored"}
        raise  # a real integrity failure (e.g. unknown transaction_id) — surface it

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


# ===========================================================================
# Task Service (Section 9.4): the role-based human task inbox — list open work,
# atomically claim a task, and complete it (which funnels into the event ingress).
# ===========================================================================


# WHY GET /v1/tasks: the "pull my queue" endpoint a role-based task inbox uses —
# a manager's UI asks for the open tasks assigned to their role.
@app.get("/v1/tasks")
async def list_tasks(role: str | None = None, status: str = "open"):
    # (CAST(:role AS text) IS NULL OR assigned_role = :role) makes `role` optional
    # in one query; the CAST avoids Postgres "could not determine parameter type"
    # when role is NULL.
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT id, transaction_id, node_id, token, assigned_role, "
                "       status, claimed_by, created_at "
                "FROM task "
                "WHERE status = :status "
                "AND (CAST(:role AS text) IS NULL OR assigned_role = CAST(:role AS text)) "
                "ORDER BY created_at"
            ),
            {"status": status, "role": role},
        ).mappings().all()

    # Cast uuids/timestamps to str so the payload is JSON-serializable.
    return [
        {
            "id": str(r["id"]),
            "transaction_id": str(r["transaction_id"]),
            "node_id": r["node_id"],
            "token": r["token"],
            "assigned_role": r["assigned_role"],
            "status": r["status"],
            "claimed_by": r["claimed_by"],
            "created_at": str(r["created_at"]) if r["created_at"] is not None else None,
        }
        for r in rows
    ]


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
        assigned_role = conn.execute(
            text("SELECT assigned_role FROM task WHERE token = :token"),
            {"token": token},
        ).scalar_one_or_none()
    if assigned_role is not None and assigned_role not in user["roles"]:
        raise HTTPException(status_code=403, detail=f"requires role '{assigned_role}'")

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


# WHY POST /v1/tasks/{token}/complete: completing a task IS submitting its
# decision. Rather than duplicate the record-event + dedupe + signal logic, we
# resolve the task's transaction and funnel through the SAME ingest_event path,
# passing the task_token so the task is marked done there.
@app.post("/v1/tasks/{token}/complete")
async def complete_task(token: str, body: CompleteIn, user: dict = Depends(current_user)):
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT transaction_id, assigned_role FROM task WHERE token = :token"),
            {"token": token},
        ).mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail="task not found")

    # Role gate (Section 10): the caller must hold this task's assigned_role.
    assigned_role = row["assigned_role"]
    if assigned_role is not None and assigned_role not in user["roles"]:
        raise HTTPException(status_code=403, detail=f"requires role '{assigned_role}'")

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
@app.put("/v1/config/{process_key}")
async def put_config(process_key: str, body: ConfigIn):
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
