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

import requests
from dotenv import load_dotenv
from temporalio import activity
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

# Load services/api/.env at import so notify() can read SMTP config from the
# environment. This file lives at services/worker/activities/, so the project
# root is parents[3] and the env file is under services/api/.
load_dotenv(Path(__file__).resolve().parents[3] / "services" / "api" / ".env")

DB_URL = os.getenv("DATABASE_URL", "postgresql+psycopg://app:app@localhost:5432/workflow_app")

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
async def ai_review(txn_id: str, data: dict, cfg: dict, node: dict | None = None) -> dict:
    # Bounded decision. Prefer the GENERIC engine (decision_engine.decide reads the
    # routes from the PDD llm_decision node and runs a real LangGraph StateGraph);
    # fall back to the legacy invoice-specific rules only if no node/routes given.
    decisions_dir = Path(__file__).resolve().parent.parent / "decisions"
    if str(decisions_dir) not in sys.path:
        sys.path.insert(0, str(decisions_dir))
    from decision_engine import decide
    if node and node.get("routes"):
        result = decide(node, data, cfg)
    else:
        # A well-formed llm_decision node always carries routes; without them
        # there is nothing to choose (the interpreter surfaces this as an error).
        result = {"route": None, "missing": [], "anomalies": [], "rationale": "no routes on decision node"}
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

    # A MULTI-APPROVER (quorum) step is recognised from its policy, never from the
    # step being named "finance" — so a quorum step can be called panel,
    # committee, board, … in any workflow.
    _q = policy.get("quorum") if isinstance(policy, dict) else None
    is_quorum = isinstance(_q, dict) and _q.get("n") is not None and _q.get("of") is not None

    finance_capacity = None
    if is_quorum:
        required = _q.get("n")
        finance_capacity = _q.get("of")
        reject_short_circuits = policy.get("rejectShortCircuits")
        if (
            type(required) is not int
            or type(finance_capacity) is not int
            or not 1 <= required <= finance_capacity
        ):
            raise ValueError(
                f"invalid quorum on step '{node_id}': expected integers satisfying 1 <= n <= of"
            )
        if type(reject_short_circuits) is not bool:
            raise ValueError(
                f"invalid quorum on step '{node_id}': rejectShortCircuits must be a boolean"
            )

    # The task row, any participant_task rows, AND the TASK_CREATED audit event
    # all commit in ONE transaction: we pass this same `conn` to
    # append_event(conn=conn) so the audit event is written together with the
    # business rows (strict atomicity — never a task without its audit event, or
    # an audit event without its task).
    with engine.begin() as conn:
        if is_quorum:
            # Lock the transaction projection so an overlapping Temporal activity
            # retry cannot create a second parent task for this quorum step.
            conn.execute(
                text('SELECT id FROM "transaction" WHERE id = CAST(:txn AS uuid) FOR UPDATE'),
                {"txn": txn_id},
            )
            existing = conn.execute(
                text(
                    "SELECT id, token, status, completion_policy FROM task "
                    "WHERE transaction_id = CAST(:txn AS uuid) AND node_id = :node_id "
                    "ORDER BY created_at LIMIT 1"
                ),
                {"txn": txn_id, "node_id": node_id},
            ).mappings().first()
            if existing is not None:
                existing_policy = existing["completion_policy"] or {}
                existing_quorum = existing_policy.get("quorum")
                if isinstance(existing_quorum, dict):
                    existing_capacity = existing_quorum.get("of")
                else:
                    # A pre-slot Finance task stored n/of at the top level.
                    existing_capacity = existing_policy.get("of")
                if type(existing_capacity) is not int or existing_capacity < 1:
                    raise ValueError("existing Finance task has invalid completion_policy")
                participant_count = conn.execute(
                    text(
                        "SELECT count(*) FROM participant_task "
                        "WHERE task_id = CAST(:task_id AS uuid)"
                    ),
                    {"task_id": str(existing["id"])},
                ).scalar_one()
                if participant_count > existing_capacity:
                    raise ValueError(
                        "existing Finance task has more participant rows than its capacity"
                    )
                if existing["status"] != "done":
                    for _ in range(existing_capacity - participant_count):
                        conn.execute(
                            text(
                                "INSERT INTO participant_task (id, task_id, status) "
                                "VALUES (CAST(:id AS uuid), CAST(:task_id AS uuid), 'open')"
                            ),
                            {"id": str(uuid.uuid4()), "task_id": str(existing["id"])},
                        )
                return existing["token"]

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
        total = finance_capacity if is_quorum else None
        if type(total) is int and total > 0:
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
def _submitted_by_email(txn_id: str) -> str | None:
    # Resolve the SUBMITTING vendor's Keycloak email from transaction.submitted_by.
    # Returns None on null submitted_by / missing email / any Keycloak error.
    with engine.connect() as conn:
        submitted_by = conn.execute(
            text('SELECT submitted_by FROM "transaction" WHERE id = CAST(:t AS uuid)'),
            {"t": txn_id},
        ).scalar_one_or_none()
    if not submitted_by:
        return None
    if "@" in submitted_by:
        return submitted_by  # already an email address (e.g. an email-started transaction)
    try:
        kc = os.getenv("KEYCLOAK_URL", "http://localhost:8081")
        realm = os.getenv("KEYCLOAK_REALM", "workflow")
        token = requests.post(
            f"{kc}/realms/master/protocol/openid-connect/token",
            data={
                "client_id": "admin-cli",
                "grant_type": "password",
                "username": os.getenv("KEYCLOAK_ADMIN", "admin"),
                "password": os.getenv("KEYCLOAK_ADMIN_PASSWORD", "admin"),
            },
            timeout=10,
        ).json().get("access_token")
        if not token:
            return None
        users = requests.get(
            f"{kc}/admin/realms/{realm}/users",
            params={"username": submitted_by, "exact": "true"},
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        ).json()
        for user in users if isinstance(users, list) else []:
            if user.get("email"):
                return user["email"]
    except Exception as exc:
        print(f"notify: vendor email lookup failed: {exc}")
    return None


def _vendor_email_for_txn(txn_id: str) -> str | None:
    # For a request_info step only: target the submitting vendor. Other steps
    # return None so the caller falls back to NOTIFY_TO.
    with engine.connect() as conn:
        node = conn.execute(
            text(
                "SELECT node_id FROM task WHERE transaction_id = CAST(:t AS uuid) "
                "AND status = 'open' ORDER BY created_at DESC LIMIT 1"
            ),
            {"t": txn_id},
        ).scalar_one_or_none()
    if node != "request_info":
        return None
    return _submitted_by_email(txn_id)


def _latest_reject_reason(txn_id: str) -> str | None:
    # Most recent reject decision's reason (manager HUMAN_DECISION or FINANCE_VOTE)
    # from the immutable event log — no workflow-arg plumbing needed.
    with engine.connect() as conn:
        return conn.execute(
            text(
                "SELECT payload->>'reason' FROM event "
                "WHERE transaction_id = CAST(:t AS uuid) "
                "AND type IN ('HUMAN_DECISION','FINANCE_VOTE') "
                "AND payload->>'decision' = 'reject' "
                "ORDER BY occurred_at DESC LIMIT 1"
            ),
            {"t": txn_id},
        ).scalar_one_or_none()


def _kc_admin_token(kc: str) -> str | None:
    try:
        return requests.post(
            f"{kc}/realms/master/protocol/openid-connect/token",
            data={
                "client_id": "admin-cli",
                "grant_type": "password",
                "username": os.getenv("KEYCLOAK_ADMIN", "admin"),
                "password": os.getenv("KEYCLOAK_ADMIN_PASSWORD", "admin"),
            },
            timeout=10,
        ).json().get("access_token")
    except Exception as exc:
        print(f"notify: keycloak admin token failed: {exc}")
        return None


def _process_key_for_txn(txn_id: str):
    """Which workflow this transaction belongs to."""
    try:
        with engine.connect() as conn:
            return conn.execute(
                text('SELECT pd.process_key FROM "transaction" tr '
                     "JOIN definition_version dv ON dv.id = tr.definition_version_id "
                     "JOIN process_definition pd ON pd.id = dv.definition_id "
                     "WHERE tr.id = CAST(:id AS uuid)"),
                {"id": txn_id}).scalar_one_or_none()
    except Exception as exc:
        print(f"notify: process lookup failed: {exc}")
        return None


def _usernames_for_process(process_key: str):
    """Who is assigned to this workflow. None = table missing -> do not filter."""
    if not process_key:
        return None
    try:
        with engine.connect() as conn:
            return set(conn.execute(
                text("SELECT username FROM user_process WHERE process_key = :p"),
                {"p": process_key}).scalars().all())
    except Exception:
        return None


def _emails_for_role(role: str, process_key: str | None = None) -> list:
    # Every Keycloak user holding `role` — narrowed to the people assigned to THIS
    # workflow. Both invoice and leave may use the role `manager`, so an invoice
    # task must only reach the managers assigned to invoice.
    if not role:
        return []
    kc = os.getenv("KEYCLOAK_URL", "http://localhost:8081")
    realm = os.getenv("KEYCLOAK_REALM", "workflow")
    token = _kc_admin_token(kc)
    if not token:
        return []
    try:
        users = requests.get(
            f"{kc}/admin/realms/{realm}/roles/{role}/users",
            headers={"Authorization": f"Bearer {token}"}, timeout=10,
        ).json()
    except Exception as exc:
        print(f"notify: role-email lookup failed for {role!r}: {exc}")
        return []
    holders = [u for u in users if isinstance(u, dict) and u.get("email")]
    assigned = _usernames_for_process(process_key)
    if assigned is not None:
        scoped = [u for u in holders if u.get("username") in assigned]
        if not scoped:
            print(f"notify: nobody with role {role!r} is assigned to process "
                  f"{process_key!r} — no recipients")
        return [u["email"] for u in scoped]
    return [u["email"] for u in holders]


def _fallback_recipients() -> list:
    to = os.getenv("NOTIFY_TO")
    return [to] if to else []


def _resolve_recipients(txn_id: str, recipient: dict | None) -> list:
    # Turn a recipient spec (from the PDD notification rule the interpreter passes)
    # into concrete emails:
    #   {"to_email": "x@y"} -> literal
    #   {"to": "submitter"} -> the user/vendor who submitted this transaction
    #   {"to_role": "<kc>"} -> every Keycloak user holding that realm role
    #   None / unresolved   -> NOTIFY_TO fallback (so nothing is silently dropped)
    if not recipient:
        return _fallback_recipients()
    if recipient.get("to_email"):
        return [recipient["to_email"]]
    if recipient.get("to") == "submitter":
        email = _submitted_by_email(txn_id)
        return [email] if email else _fallback_recipients()
    if recipient.get("to_role"):
        # Scope role recipients to the people assigned to THIS workflow. No
        # NOTIFY_TO fallback here: mailing an unrelated inbox would leak another
        # team's work — an empty result is logged instead.
        return _emails_for_role(recipient["to_role"], _process_key_for_txn(txn_id))
    return _fallback_recipients()


def _get_mailbox(name):
    # Resolve the SENDER mailbox (address + app password + hosts) by name from the
    # registry; falls back to the default (legacy GMAIL_*). The PDD carries only the
    # mailbox NAME — the credentials live in env/secrets, never in the PDD.
    notifier_dir = Path(__file__).resolve().parent.parent / "notifier"
    if str(notifier_dir) not in sys.path:
        sys.path.insert(0, str(notifier_dir))
    from mailboxes import get_mailbox
    return get_mailbox(name)


def _known_details(data: dict) -> str:
    if not isinstance(data, dict) or not data:
        return ""
    return "\n".join(f"  - {k}: {v}" for k, v in data.items() if v not in (None, ""))


def _compose_email(context: dict, fallback: str) -> tuple[str, str]:
    """Write the subject + body for a notification.

    The LLM only writes the WORDING. Every fact that must be exact — the list of
    missing fields and the reply format — is appended by code afterwards, so a
    slow or creative model can never lose or invent them. Any failure falls back
    to a clear deterministic message; email must never break the workflow."""
    context = context or {}
    kind = context.get("kind") or "task"
    process = context.get("process") or "request"
    step = context.get("step") or ""
    missing = [m for m in (context.get("missing") or []) if m]
    data = context.get("data") or {}
    outcome = context.get("outcome")

    # 1. Deterministic baseline (always correct, used as-is if the LLM is down).
    if kind == "request_info":
        subject = f"More information needed for your {process.replace('_', ' ')}"
        body = ("Hello,\n\nWe received your request but we still need a few details "
                "before it can continue.")
    elif kind == "outcome":
        subject = f"Your {process.replace('_', ' ')} was {outcome}"
        body = f"Hello,\n\nYour request has been {outcome}."
    else:
        subject = f"Action needed: {step or process}"
        body = (f"Hello,\n\nA request in '{process}' is waiting for your approval "
                f"at the '{step}' step.")

    # 2. Optional LLM rewrite of just the greeting/explanation. Kept on a SHORT
    #    leash: a slow model must never hold up the workflow (set EMAIL_AI=0 to
    #    skip it entirely and always use the plain wording above).
    try:
        import os
        if os.getenv("EMAIL_AI", "1").strip().lower() in ("0", "false", "no"):
            raise RuntimeError("EMAIL_AI disabled")
        from langchain_openai import ChatOpenAI
        llm = ChatOpenAI(
            base_url=os.getenv("LLM_BASE_URL", "http://localhost:11434/v1"),
            api_key="ollama", model=os.getenv("LLM_MODEL", "llama3.2:1b"),
            temperature=0, timeout=float(os.getenv("LLM_EMAIL_TIMEOUT", "20")), max_retries=0,
        )
        prompt = (
            f"Write a short, polite business email body (3 sentences max, no subject "
            f"line, no placeholders, no markdown) for this workflow event.\n"
            f"Process: {process}\nStep: {step}\nSituation: {kind}"
            + (f"\nOutcome: {outcome}" if outcome else "")
            + (f"\nDetails we already have: {data}" if data else "")
            + (f"\nInformation still required: {', '.join(missing)}" if missing else "")
            + "\nDo NOT invent facts, amounts or dates. Do not list the required "
              "fields yourself; they are appended separately."
        )
        text_out = (llm.invoke(prompt).content or "").strip()
        if text_out:
            body = text_out
    except Exception as exc:
        print(f"notify: LLM email wording unavailable, using plain text ({exc})")

    # 3. Append the authoritative facts (never LLM-generated).
    details = _known_details(data)
    if kind == "request_info":
        wanted = missing or ["the missing details"]
        body += ("\n\nWhat we already have:\n" + details if details else "")
        body += ("\n\nPlease REPLY to this email with one line per item, exactly like this:\n"
                 + "\n".join(f"{field}: <value>" for field in wanted))
        body += "\n\nJust reply to this message — you do not need any reference number."
    elif kind == "task":
        body += ("\n\nRequest details:\n" + details if details else "")
        body += "\n\nOpen the app to approve or reject, or reply with 'approve' or 'reject'."
    else:
        body += ("\n\nRequest details:\n" + details if details else "")
    return subject, body or fallback


@activity.defn
async def notify(txn_id: str, channel: str, message: str, recipient: dict | None = None,
                 mailbox: str | None = None, context: dict | None = None) -> None:
    # Recipients come from the PDD notification rule the interpreter passes
    # (role / submitter / literal), NOT hardcoded — so manager tasks reach
    # managers, finance tasks reach finance, and approvals/rejections reach the
    # submitter. {reason} in a template is filled from the latest reject decision.
    reason = None
    if message and "{reason}" in message:
        reason = _latest_reject_reason(txn_id)
        message = message.replace("{reason}", reason or "No reason recorded")

    recipients = _resolve_recipients(txn_id, recipient) if channel == "email" else []

    # 1. Durable audit write — records the ACTUAL recipients so the audit is truthful.
    await append_event(
        txn_id, "notify", "NOTIFY", "NotificationService",
        f"{channel}: {message}",
        payload={k: v for k, v in {"recipients": recipients, "reason": reason}.items() if v},
        idempotency_key=None,
    )

    # 2. Best-effort REAL send, only for the email channel.
    if channel != "email":
        return

    # Sender mailbox is chosen by the process's PDD `mailbox` name (per-process),
    # resolved to real credentials from the registry (falls back to default).
    box = _get_mailbox(mailbox)
    if not (box and recipients):
        print("notify: no sender mailbox configured or no recipients resolved; skipping send")
        return

    # Subject + body: AI writes the wording, code guarantees the facts (missing
    # fields, reply format). Without context we simply send the plain message.
    if context:
        subject, body = _compose_email(context, message)
    else:
        subject, body = message, message

    msg = EmailMessage()
    # The [invoice-<txn_id>] tag ties replies back to this run (email adapter 9.3).
    msg["Subject"] = f"[invoice-{txn_id}] {subject}"
    msg["From"] = box["address"]
    msg["To"] = ", ".join(recipients)
    # Send just the message. (The old hardcoded "reply with approve/reject/return"
    # line was wrong on final notifications and triggered Gmail's smart-reply
    # buttons. If a specific step wants a reply hint, put it in that step's
    # notification template in the PDD.)
    msg.set_content(body)

    try:
        with smtplib.SMTP(box["smtp_host"], box["smtp_port"]) as server:
            server.starttls()
            server.login(box["address"], box["app_password"])
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
        "Posted to system of record (stub)", idempotency_key=None,
    )


# WHY set_transaction_status: the transaction row is created with status='running'
# and nothing ever updated it, so the Monitor UI showed every run as 'running'
# even after it finished. A workflow can't touch the DB directly (only activities
# can), so _finish calls this at the end of a run to persist the terminal outcome
# ('approved' / 'rejected') to transaction.status — making the DB reflect the true
# final state.
@activity.defn
async def set_transaction_status(txn_id: str, status: str) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                'UPDATE "transaction" SET status = :status, '
                'closed_at = COALESCE(closed_at, now()) '
                'WHERE id = CAST(:id AS uuid)'
            ),
            {"status": status, "id": txn_id},
        )
        conn.execute(
            text(
                "UPDATE task SET status = 'done' "
                "WHERE transaction_id = CAST(:id AS uuid) AND status <> 'done'"
            ),
            {"id": txn_id},
        )
        conn.execute(
            text(
                "UPDATE participant_task SET status = 'done' "
                "WHERE task_id IN ("
                "    SELECT id FROM task WHERE transaction_id = CAST(:id AS uuid)"
                ") AND status IS DISTINCT FROM 'done'"
            ),
            {"id": txn_id},
        )
