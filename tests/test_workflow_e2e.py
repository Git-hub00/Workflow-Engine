# WHY this file exists:
# A true END-TO-END test: it seeds a real invoice, starts the InvoiceWorkflow on
# a LIVE Temporal server, waits for the workflow to durably PAUSE on the manager
# approval, sends the approval signal, and asserts the workflow completes with
# "approved". It then prints the audit event log so we can SEE the journey
# (started -> ai_review -> task created/notify -> ERP post -> approved).
#
# It does NOT start the worker — a worker (services/worker/main.py) must already
# be running in another terminal to actually execute the workflow + activities.

import sys
import json
import uuid
import asyncio
from pathlib import Path

from sqlalchemy import create_engine, text
from temporalio.client import Client

# STEP 1 — make the activities + workflows modules importable (relative to this
# file), then import the workflow. (Importing the workflow pulls in the
# activities via its imports_passed_through block.)
ROOT = Path(__file__).resolve().parent.parent
ACTIVITIES_DIR = ROOT / "services" / "worker" / "activities"
WORKFLOWS_DIR = ROOT / "services" / "worker" / "workflows"
for d in (ACTIVITIES_DIR, WORKFLOWS_DIR):
    if str(d) not in sys.path:
        sys.path.insert(0, str(d))

from invoice_workflow import InvoiceWorkflow  # noqa: E402  (after sys.path tweak)

DB_URL = "postgresql+psycopg://app:app@localhost:5432/workflow_app"
engine = create_engine(DB_URL)

# A Globex invoice: all required fields present, approved vendor, amount 4800.
# Under the seeded config (autoApproveUnder=500, financeThreshold=5000) this is
# NOT auto-approve and NOT finance-threshold -> deterministic route MANAGER_ONLY,
# so the workflow will pause waiting for a single manager decision.
SAMPLE_INVOICE = {
    "vendor": "Globex Inc",
    "amount": 4800,
    "poNumber": "PO-9",
    "costCenter": "CC-9",
    "taxId": "TX-9",
}

# Pre-generate ids so cleanup can run even if something fails mid-way.
pd_id = str(uuid.uuid4())
dv_id = str(uuid.uuid4())
txn_id = str(uuid.uuid4())


async def main():
    passed = False
    handle = None  # set once the workflow starts; used by cleanup to terminate it
    try:
        # STEP 2 — load config and seed the parent transaction chain.
        # NOTE: the workflow needs cfg["roles"] (which lives at the TOP level of
        # the PDD, not inside "config"), plus everything under config. We load
        # config as the base and fold in roles so the workflow has both.
        doc = json.loads((ROOT / "definitions" / "invoice.pdd.json").read_text(encoding="utf-8"))
        cfg = {**doc["config"], "roles": doc["roles"]}

        with engine.begin() as conn:
            conn.execute(
                text("INSERT INTO process_definition (id, process_key) "
                     "VALUES (CAST(:id AS uuid), :pk)"),
                {"id": pd_id, "pk": "invoice_e2e"},
            )
            conn.execute(
                text("INSERT INTO definition_version (id, definition_id, version, pdd, status) "
                     "VALUES (CAST(:id AS uuid), CAST(:def AS uuid), :v, CAST(:pdd AS jsonb), :st)"),
                {"id": dv_id, "def": pd_id, "v": 1, "pdd": json.dumps(doc), "st": "published"},
            )
            conn.execute(
                text('INSERT INTO "transaction" (id, definition_version_id, status, data_snapshot) '
                     "VALUES (CAST(:id AS uuid), CAST(:dv AS uuid), :st, CAST(:snap AS jsonb))"),
                {"id": txn_id, "dv": dv_id, "st": "running", "snap": json.dumps(SAMPLE_INVOICE)},
            )
        print(f"[seed] transaction id = {txn_id}")

        # STEP 3 — connect to the live Temporal server.
        client = await Client.connect("localhost:7233")

        # STEP 4 — start the workflow on the MAIN task queue.
        wf_id = f"invoice-{txn_id}"
        handle = await client.start_workflow(
            InvoiceWorkflow.run,
            args=[txn_id, cfg],
            id=wf_id,
            task_queue="invoice-tq",
        )
        print(f"[start] workflow started: id={wf_id}, task_queue=invoice-tq")

        # STEP 5 — poll until the workflow durably PAUSES on the manager task.
        # Allow ~180s: the very first ai_review call may COLD-START the 1b model
        # (loading it into memory + generating on CPU) which can take a while.
        print("[wait] polling for the open 'manager' task (workflow paused on approval)...")
        manager_seen = False
        for attempt in range(60):  # 60 * 3s = ~180s
            await asyncio.sleep(3)
            with engine.connect() as conn:
                n = conn.execute(
                    text("SELECT count(*) FROM task "
                         "WHERE transaction_id = CAST(:t AS uuid) "
                         "AND node_id = 'manager' AND status = 'open'"),
                    {"t": txn_id},
                ).scalar_one()
            if n > 0:
                manager_seen = True
                print(f"[wait] manager task appeared after ~{(attempt + 1) * 3}s — workflow is paused waiting for a decision")
                break
        if not manager_seen:
            raise TimeoutError("manager task did not appear within ~180s (is the worker running?)")

        # STEP 6 — send the manager's approval signal, unblocking the workflow.
        await handle.signal(InvoiceWorkflow.human_decision, {"decision": "approve"})
        print("[signal] sent human_decision approve")

        # STEP 7 — wait for the workflow to finish and print its result.
        result = await handle.result()
        print(f"[result] workflow returned: {result!r}")

        # STEP 8 — print the full audit journey from the event log.
        with engine.connect() as conn:
            rows = conn.execute(
                text("SELECT type, payload->>'detail' AS detail FROM event "
                     "WHERE transaction_id = CAST(:t AS uuid) "
                     "ORDER BY occurred_at, seq"),
                {"t": txn_id},
            ).mappings().all()
        print("\n[audit] event log (the journey):")
        for i, r in enumerate(rows, 1):
            print(f"  {i}. {r['type']:<18} {r['detail']}")

        # STEP 9 — assert and report.
        assert result == "approved", f"expected 'approved', got {result!r}"
        passed = True
        print("\nE2E WORKFLOW TEST PASSED")

    finally:
        # STEP 10a — terminate the workflow BEFORE deleting any rows. This is the
        # BUG A fix: if the test bailed out (e.g. poll timeout) while the workflow
        # was still running, deleting its transaction row out from under it would
        # leave a zombie that retries forever against missing rows. Terminating
        # first guarantees no live workflow depends on what we are about to delete.
        # Wrapped in try/except so it is a safe no-op if the workflow already
        # completed or was never started.
        if handle is not None:
            try:
                await handle.terminate(reason="e2e test cleanup")
                print("[cleanup] terminated workflow (if it was still running)")
            except Exception as e:
                print(f"[cleanup] workflow terminate skipped ({type(e).__name__}: already closed or gone)")

        # STEP 10b — clean up ALL created rows in reverse FK order, always.
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM participant_task WHERE task_id IN "
                              "(SELECT id FROM task WHERE transaction_id = CAST(:t AS uuid))"),
                         {"t": txn_id})
            conn.execute(text("DELETE FROM task WHERE transaction_id = CAST(:t AS uuid)"), {"t": txn_id})
            conn.execute(text("DELETE FROM idempotency_key WHERE event_id IN "
                              "(SELECT id FROM event WHERE transaction_id = CAST(:t AS uuid))"),
                         {"t": txn_id})
            conn.execute(text("DELETE FROM event WHERE transaction_id = CAST(:t AS uuid)"), {"t": txn_id})
            conn.execute(text('DELETE FROM "transaction" WHERE id = CAST(:id AS uuid)'), {"id": txn_id})
            conn.execute(text("DELETE FROM definition_version WHERE id = CAST(:id AS uuid)"), {"id": dv_id})
            conn.execute(text("DELETE FROM process_definition WHERE id = CAST(:id AS uuid)"), {"id": pd_id})
        print("[cleanup] removed all test rows (participant_task -> task -> idempotency_key -> "
              "event -> transaction -> definition_version -> process_definition)")
        engine.dispose()

    if not passed:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
