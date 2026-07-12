# WHY this file exists:
# Prove every activity in invoice_activities.py is individually callable against
# a real database (and, for ai_review, the real local LLM). Temporal activities
# are just async functions, so we drive each one directly with asyncio.run over a
# real parent transaction chain, watch the audit log grow event-by-event, assert
# the key behaviors (extraction, quorum fan-out, audit count), and clean up.

import sys
import json
import uuid
import asyncio
from pathlib import Path

from sqlalchemy import create_engine, text

# STEP 0 — make the activities importable.
# WHY: they live in services/worker/activities/invoice_activities.py, not on
# sys.path. Compute that dir RELATIVE to this file so it works from any CWD.
ROOT = Path(__file__).resolve().parent.parent
ACTIVITIES_DIR = ROOT / "services" / "worker" / "activities"
sys.path.insert(0, str(ACTIVITIES_DIR))

from invoice_activities import (  # noqa: E402  (import after sys.path tweak)
    append_event,
    extract_fields,
    ai_review,
    create_human_task,
    notify,
    post_to_erp,
)

DB_URL = "postgresql+psycopg://app:app@localhost:5432/workflow_app"
engine = create_engine(DB_URL)

# Sample invoice stored as the transaction's data_snapshot; extract_fields reads
# it back and ai_review reasons over it.
SAMPLE_INVOICE = {
    "vendor": "Acme Supplies",
    "amount": 180,
    "poNumber": "PO-1",
    "costCenter": "CC-1",
    "taxId": "TX-1",
}

# Pre-generate parent-chain ids so cleanup can run even if setup/asserts fail.
pd_id = str(uuid.uuid4())   # process_definition
dv_id = str(uuid.uuid4())   # definition_version
txn_id = str(uuid.uuid4())  # transaction (FK target for tasks + events)


def event_count() -> int:
    # Helper: current number of audit events for our transaction.
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT count(*) FROM event WHERE transaction_id = CAST(:txn AS uuid)"),
            {"txn": txn_id},
        ).scalar_one()


def participant_count() -> int:
    # Helper: participant_task rows belonging to any task of our transaction.
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT count(*) FROM participant_task pt "
                 "JOIN task t ON t.id = pt.task_id "
                 "WHERE t.transaction_id = CAST(:txn AS uuid)"),
            {"txn": txn_id},
        ).scalar_one()


def main():
    passed = False
    try:
        # STEP 1 — load the real published config.
        cfg = json.loads((ROOT / "definitions" / "invoice.pdd.json").read_text(encoding="utf-8"))["config"]

        # STEP 2 — create the parent transaction chain with the sample invoice
        # as data_snapshot (parent-first to satisfy FKs).
        with engine.begin() as conn:
            conn.execute(
                text("INSERT INTO process_definition (id, process_key) "
                     "VALUES (CAST(:id AS uuid), :pk)"),
                {"id": pd_id, "pk": "activities_test"},
            )
            conn.execute(
                text("INSERT INTO definition_version (id, definition_id, version, pdd, status) "
                     "VALUES (CAST(:id AS uuid), CAST(:def AS uuid), :v, CAST(:pdd AS jsonb), :st)"),
                {"id": dv_id, "def": pd_id, "v": 1, "pdd": "{}", "st": "draft"},
            )
            conn.execute(
                text('INSERT INTO "transaction" (id, definition_version_id, status, data_snapshot) '
                     "VALUES (CAST(:id AS uuid), CAST(:dv AS uuid), :st, CAST(:snap AS jsonb))"),
                {"id": txn_id, "dv": dv_id, "st": "running", "snap": json.dumps(SAMPLE_INVOICE)},
            )
        print(f"[setup] transaction id = {txn_id}")
        print(f"[setup] event rows at start = {event_count()}  (expected 0)\n")

        # STEP 3a — extract_fields: should return the stored snapshot (not audited).
        fields = asyncio.run(extract_fields(txn_id))
        print(f"[extract_fields] -> {fields}")

        # STEP 3b — ai_review: bounded decision + audit (LLM_DECISION event).
        review = asyncio.run(ai_review(txn_id, fields, cfg))
        print(f"[ai_review] route = {review['route']}")
        print(f"[ai_review] rationale = {review['rationale']}")
        print(f"[ai_review] event rows now = {event_count()}\n")

        # STEP 3c — create_human_task with a quorum of 3 -> 3 participant rows,
        # plus a TASK_CREATED audit event.
        token = asyncio.run(create_human_task(
            txn_id, "manager_approval", "ap_manager", {"quorum": {"n": 2, "of": 3}}
        ))
        parts = participant_count()
        print(f"[create_human_task] token = {token}")
        print(f"[create_human_task] participant_task rows = {parts}  (expected 3)")
        print(f"[create_human_task] event rows now = {event_count()}\n")

        # STEP 3d — notify: audit-only stub (NOTIFY event).
        asyncio.run(notify(txn_id, "email", "please approve"))
        print("[notify] notify ok")
        print(f"[notify] event rows now = {event_count()}\n")

        # STEP 3e — post_to_erp: audit-only stub (ERP_POSTED event).
        asyncio.run(post_to_erp(txn_id, {}))
        print("[post_to_erp] erp ok")
        final_events = event_count()
        print(f"[post_to_erp] event rows now = {final_events}\n")

        # STEP 4 — assertions.
        assert fields.get("vendor") == "Acme Supplies", f"extract_fields vendor wrong: {fields!r}"
        assert parts == 3, f"expected 3 participant rows, got {parts}"
        assert final_events >= 4, f"expected >=4 event rows, got {final_events}"
        passed = True
        print("ACTIVITIES TEST PASSED")

    finally:
        # STEP 5 — cleanup ALL rows in reverse FK order, always.
        # Order: participant_task -> task -> idempotency_key -> event ->
        # transaction -> definition_version -> process_definition.
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM participant_task WHERE task_id IN "
                              "(SELECT id FROM task WHERE transaction_id = CAST(:txn AS uuid))"),
                         {"txn": txn_id})
            conn.execute(text("DELETE FROM task WHERE transaction_id = CAST(:txn AS uuid)"),
                         {"txn": txn_id})
            conn.execute(text("DELETE FROM idempotency_key WHERE event_id IN "
                              "(SELECT id FROM event WHERE transaction_id = CAST(:txn AS uuid))"),
                         {"txn": txn_id})
            conn.execute(text("DELETE FROM event WHERE transaction_id = CAST(:txn AS uuid)"),
                         {"txn": txn_id})
            conn.execute(text('DELETE FROM "transaction" WHERE id = CAST(:id AS uuid)'),
                         {"id": txn_id})
            conn.execute(text("DELETE FROM definition_version WHERE id = CAST(:id AS uuid)"),
                         {"id": dv_id})
            conn.execute(text("DELETE FROM process_definition WHERE id = CAST(:id AS uuid)"),
                         {"id": pd_id})
        print("[cleanup] removed all test rows (participant_task -> task -> "
              "idempotency_key -> event -> transaction -> definition_version -> process_definition)")
        engine.dispose()

    if not passed:
        sys.exit(1)


if __name__ == "__main__":
    main()
