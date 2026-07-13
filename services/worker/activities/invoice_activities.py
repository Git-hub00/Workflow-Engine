# worker/activities/invoice_activities.py
#
# WHY this file exists:
# Temporal activities are the ONLY place a workflow is allowed to touch the
# outside world (here: the workflow_app database). This module holds the
# invoice-related activities; append_event is the primitive every other activity
# builds on — it writes one row to the immutable `event` audit log.
#
# append_event provides TWO guarantees that matter for a durable workflow engine,
# where Temporal WILL retry an activity after a crash/timeout:
#   * ATOMIC    — the event row and its idempotency-key row are written inside a
#                 SINGLE database transaction, so they commit together or not at
#                 all (you never get an event without its key, or vice versa).
#                 Callers that already hold an open transaction can pass their
#                 own connection via `conn=` so their business write AND this
#                 audit event commit together as one atomic unit.
#   * IDEMPOTENT — calling append_event twice with the SAME idempotency_key
#                 produces EXACTLY ONE event; the second call is a no-op that
#                 returns the original event id. This makes Temporal's
#                 at-least-once activity delivery safe.

import json
import os
import smtplib
import sys
import uuid
from email.message import EmailMessage
from pathlib import Path

from dotenv import load_dotenv
from temporalio import activity
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

# Load services/api/.env at import so notify() can read SMTP config from the
# environment. This file lives at services/worker/activities/, so the project
# root is parents[3] and the env file is under services/api/.
load_dotenv(Path(__file__).resolve().parents[3] / "services" / "api" / ".env")

DB_URL = "postgresql+psycopg://app:app@localhost:5432/workflow_app"

# Module-level engine: created once and reused across activity invocations
# (SQLAlchemy pools connections internally).
engine = create_engine(DB_URL)


# WHY append_event: it is the single choke point for appending to the event log.
# The idempotency_key table (PRIMARY KEY on `key`) is what makes retries safe —
# claiming the key and writing the event happen together, so the UNIQUE key
# constraint guarantees "same key applied twice => one event".
#
# The optional `conn` parameter controls the transaction boundary:
#   * conn is None  -> append_event opens its OWN transaction (engine.begin())
#                      and commits the event (+ key) by itself. Use this when the
#                      audit event is the only write.
#   * conn provided -> append_event writes into the CALLER's already-open
#                      transaction (it does NOT open a new one), so a business
#                      write and its audit event commit together as ONE atomic
#                      unit. The caller owns the commit/rollback.
# Both paths run identical SELECT/INSERT logic and use conn.begin_nested() for
# the savepoint, and both return the event_id as a string.
@activity.defn
async def append_event(
    txn_id: str,
    layer: str,
    type_: str,
    actor: str,
    detail: str,
    payload: dict | None = None,
    idempotency_key: str | None = None,
    conn=None,
) -> str:
    # Fold `layer` and `detail` into the payload jsonb: the event table has
    # type/payload/actor columns but no dedicated layer/detail columns, so we
    # carry them inside payload (explicit keys win nothing over caller data;
    # caller payload is spread last so it can override if it really wants to).
    full_payload = {"layer": layer, "detail": detail, **(payload or {})}
    payload_json = json.dumps(full_payload)

    # NOTE (future enhancement): before_hash/after_hash are intentionally left
    # unset. The schema defines these columns for a tamper-evident hash chain,
    # but the MVP does not yet define the hashing scheme, so we omit them here.

    # Shared insert/idempotency logic, run against whichever connection we use.
    # Both the "own transaction" and "caller's transaction" paths call this same
    # helper, so there is exactly ONE copy of the SELECT/INSERT/savepoint logic.
    def _append(active_conn) -> str:
        # ---- IDEMPOTENT fast path -------------------------------------------
        # If the caller gave a key and we have already processed it, do NOT write
        # a second event; just return the event id we recorded the first time.
        if idempotency_key is not None:
            existing = active_conn.execute(
                text("SELECT event_id FROM idempotency_key WHERE key = :key"),
                {"key": idempotency_key},
            ).scalar_one_or_none()
            if existing is not None:
                return str(existing)

        # ---- New-event path -------------------------------------------------
        # Generate the id up front so we can reference it in both the event row
        # and the idempotency_key row.
        event_id = uuid.uuid4()
        try:
            # A SAVEPOINT wraps BOTH inserts. Under concurrency two callers could
            # both pass the SELECT above; whichever loses the race to INSERT the
            # key hits a PK conflict here, and rolling back to the savepoint
            # undoes the event insert too — so no duplicate event is ever left
            # behind (this is the IDEMPOTENT guarantee, made race-safe).
            with active_conn.begin_nested():
                active_conn.execute(
                    text(
                        "INSERT INTO event (id, transaction_id, type, payload, actor) "
                        "VALUES (CAST(:id AS uuid), CAST(:txn AS uuid), :type, "
                        "        CAST(:payload AS jsonb), :actor)"
                    ),
                    {
                        "id": str(event_id),
                        "txn": txn_id,
                        "type": type_,
                        "payload": payload_json,
                        "actor": actor,
                    },
                )
                # Claim the key in the SAME transaction => event + key are ATOMIC.
                # (Inserted AFTER the event so the event_id FK target exists.)
                if idempotency_key is not None:
                    active_conn.execute(
                        text(
                            "INSERT INTO idempotency_key (key, event_id) "
                            "VALUES (:key, CAST(:event_id AS uuid))"
                        ),
                        {"key": idempotency_key, "event_id": str(event_id)},
                    )
        except IntegrityError:
            # An IntegrityError is ONLY the benign "already processed" case when we
            # were deduping on an idempotency_key AND a row for that key now exists
            # (another caller won the race to claim it). Our savepoint rolled back,
            # so no duplicate event was left behind — return the winner's event id.
            # ANY other IntegrityError — no key to dedupe on, or an FK / not-null
            # violation — is a REAL failure and must surface honestly rather than
            # be masked (e.g. an FK violation must NOT become a misleading
            # NoResultFound from looking up a NULL key).
            if idempotency_key is not None:
                existing = active_conn.execute(
                    text("SELECT event_id FROM idempotency_key WHERE key = :key"),
                    {"key": idempotency_key},
                ).scalar_one_or_none()
                if existing is not None:
                    return str(existing)
            # No key, or key not actually present => not a dedupe conflict; re-raise
            # the original IntegrityError so the true cause is reported.
            raise

        return str(event_id)

    # conn is None -> open and own our transaction (unchanged original behavior).
    if conn is None:
        with engine.begin() as own_conn:
            return _append(own_conn)
    # conn provided -> write inside the caller's open transaction so the business
    # write and this audit event form ONE combined atomic commit.
    return _append(conn)


# WHY extract_fields: first step of the invoice flow — it represents the
# OCR/parse stage that turns a raw document into normalized fields. For the MVP
# it is a stub that simply returns the transaction's stored data_snapshot; later
# it will call a real extraction service. Consumed by the workflow before
# ai_review so the decision node has structured data to reason over.
@activity.defn
async def extract_fields(txn_id: str) -> dict:
    with engine.connect() as conn:
        snapshot = conn.execute(
            text('SELECT data_snapshot FROM "transaction" WHERE id = CAST(:id AS uuid)'),
            {"id": txn_id},
        ).scalar_one_or_none()
    # data_snapshot is jsonb (psycopg returns a dict). Missing row or empty
    # snapshot -> return {} so downstream steps always get a dict.
    return snapshot or {}


# WHY ai_review: the bounded decision step of the invoice flow. It runs the
# deterministic-rules-plus-LLM-rationale node (review_invoice) to pick a route,
# then records that decision in the audit log. Called by the workflow after
# extract_fields; its returned route drives the next branch (auto-approve /
# request-info / manager / manager-then-finance).
@activity.defn
async def ai_review(txn_id: str, data: dict, cfg: dict) -> dict:
    # review_invoice lives at services/worker/decisions/invoice_review.py, which
    # is NOT importable as a package (worker.decisions). Resolve it robustly by
    # adding the decisions directory (relative to THIS file) to sys.path, then
    # importing by module name.
    decisions_dir = Path(__file__).resolve().parent.parent / "decisions"
    if str(decisions_dir) not in sys.path:
        sys.path.insert(0, str(decisions_dir))
    from invoice_review import review_invoice

    result = review_invoice(data, cfg)
    # The audit event is this activity's ONLY database write (there is no
    # separate business row), so calling append_event WITHOUT conn — letting it
    # open its own transaction — is already atomic for ai_review.
    await append_event(
        txn_id, "llm", "LLM_DECISION", "LangGraph", result["rationale"],
        payload=result, idempotency_key=None,
    )
    return result


# WHY create_human_task: materializes a human step in the flow (e.g. manager or
# finance approval). It creates the task row the task-inbox UI polls, and — when
# the completion policy requires a quorum — pre-creates one participant_task per
# required approver so votes can be recorded against them. Called by the
# workflow whenever the route needs a human (MANAGER_ONLY, MANAGER_THEN_FINANCE,
# REQUEST_INFO). Returns the task token used to complete/claim the task.
@activity.defn
async def create_human_task(txn_id: str, node_id: str, role: str, policy: dict | None = None) -> str:
    task_id = uuid.uuid4()
    token = uuid.uuid4().hex  # opaque unique claim token for this task
    policy_json = json.dumps(policy) if policy is not None else None

    # The task row, any participant_task rows, AND the TASK_CREATED audit event
    # all commit in ONE transaction: we pass this same `conn` to
    # append_event(conn=conn) so the audit event is written together with the
    # business rows (strict atomicity — never a task without its audit event, or
    # an audit event without its task).
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO task (id, transaction_id, node_id, token, assigned_role, "
                "                  status, completion_policy) "
                "VALUES (CAST(:id AS uuid), CAST(:txn AS uuid), :node_id, :token, :role, "
                "        :status, CAST(:policy AS jsonb))"
            ),
            {
                "id": str(task_id),
                "txn": txn_id,
                "node_id": node_id,
                "token": token,
                "role": role,
                "status": "open",
                "policy": policy_json,  # None -> CAST(NULL AS jsonb) -> NULL
            },
        )

        # If the policy defines a quorum {"n": needed, "of": total}, pre-create
        # `of` participant_task rows (participant left NULL until assigned).
        quorum = policy.get("quorum") if isinstance(policy, dict) else None
        total = quorum.get("of") if isinstance(quorum, dict) else None
        if isinstance(total, int) and total > 0:
            for _ in range(total):
                conn.execute(
                    text(
                        "INSERT INTO participant_task (id, task_id, status) "
                        "VALUES (CAST(:id AS uuid), CAST(:task_id AS uuid), :status)"
                    ),
                    {"id": str(uuid.uuid4()), "task_id": str(task_id), "status": "open"},
                )

        # Audit the creation INSIDE the same transaction (conn passed through),
        # so the event commits atomically with the task + participant rows.
        await append_event(
            txn_id, "task", "TASK_CREATED", "TaskService",
            f"Created human task for role '{role}' at node '{node_id}'",
            payload={"task_id": str(task_id), "node_id": node_id, "role": role, "token": token},
            idempotency_key=None,
            conn=conn,
        )
    return token


# WHY notify: the "tell a human something happened" step — e.g. email the
# approver that a task awaits them. It ALWAYS writes the NOTIFY audit event (the
# durable source of truth), and additionally sends a REAL email when
# channel == "email".
#
# WHY email failures are logged, not raised: notifications are best-effort side
# effects. The workflow's correctness rests on the audit event (already written)
# and the human's eventual reply — NOT on the email actually leaving — so an SMTP
# hiccup, or missing config, must never fail the activity/workflow. The
# "[invoice-<txn_id>]" subject tag is what lets a reply be correlated back to
# this run by the email adapter (Section 9.3).
@activity.defn
async def notify(txn_id: str, channel: str, message: str) -> None:
    # 1. Durable audit write (unchanged) — the source of truth. append_event
    #    without conn opens its own transaction, which is atomic on its own.
    await append_event(
        txn_id, "notify", "NOTIFY", "NotificationService",
        f"{channel}: {message}", idempotency_key=None,
    )

    # 2. Best-effort REAL send, only for the email channel.
    if channel != "email":
        return

    smtp_host = os.getenv("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    gmail_address = os.getenv("GMAIL_ADDRESS")
    gmail_app_password = os.getenv("GMAIL_APP_PASSWORD")
    notify_to = os.getenv("NOTIFY_TO")

    # If SMTP isn't fully configured, skip sending — the audit event already
    # succeeded, so this is not an error.
    if not (gmail_address and gmail_app_password and notify_to):
        print("notify: SMTP not configured, skipping send")
        return

    msg = EmailMessage()
    # The [invoice-<txn_id>] tag ties replies back to this run (email adapter 9.3).
    msg["Subject"] = f"[invoice-{txn_id}] {message}"
    msg["From"] = gmail_address
    msg["To"] = notify_to
    msg.set_content(
        f"{message}\n\n"
        "Reply to this email with one of: approve / reject / return."
    )

    try:
        with smtplib.SMTP(smtp_host, smtp_port) as server:
            server.starttls()
            server.login(gmail_address, gmail_app_password)
            server.send_message(msg)
    except Exception as e:
        # A notification failure must NOT fail the workflow — the audit event is
        # the source of truth. Log and carry on.
        print(f"notify: email send failed: {e}")


# WHY post_to_erp: the terminal side-effect for an approved invoice — pushing it
# into the finance/ERP system of record. MVP stub: it does NOT call a real ERP;
# it only appends an audit event marking that the post occurred, so the flow can
# be exercised end-to-end without external systems.
@activity.defn
async def post_to_erp(txn_id: str, data: dict) -> None:
    # The audit event is this stub's ONLY database write, so append_event without
    # conn (its own transaction) is already atomic here.
    await append_event(
        txn_id, "ext", "ERP_POSTED", "post_to_erp",
        "Invoice posted to ERP (stub)", idempotency_key=None,
    )
