# WHY this file exists:
# Prove the core durability guarantee of append_event: calling it twice with the
# SAME idempotency_key writes EXACTLY ONE event (a no-op replay returning the
# original id), not two. This is what makes Temporal's at-least-once activity
# retries safe. The test creates a real parent transaction (to satisfy the
# event.transaction_id foreign key), fires append_event twice with one key,
# asserts a single event exists, then cleans everything up.

import sys
import uuid
import asyncio
from pathlib import Path

from sqlalchemy import create_engine, text

# STEP 0 — make the activity importable.
# WHY: append_event lives in services/worker/activities/invoice_activities.py,
# which is not on sys.path. Compute that dir RELATIVE to this test file so it
# works regardless of the current working directory, then import.
ROOT = Path(__file__).resolve().parent.parent
ACTIVITIES_DIR = ROOT / "services" / "worker" / "activities"
sys.path.insert(0, str(ACTIVITIES_DIR))

from invoice_activities import append_event  # noqa: E402  (import after sys.path tweak)

DB_URL = "postgresql+psycopg://app:app@localhost:5432/workflow_app"
IDEMPOTENCY_KEY = "dup-key-123"

engine = create_engine(DB_URL)

# Pre-generate the parent-chain ids so cleanup can delete them even if setup or
# the assertions fail partway through.
pd_id = str(uuid.uuid4())   # process_definition
dv_id = str(uuid.uuid4())   # definition_version
txn_id = str(uuid.uuid4())  # transaction (the FK target for our events)


def main():
    passed = False
    try:
        # STEP 1 — create a real parent transaction chain.
        # WHY: event.transaction_id is a FK to transaction.id, which chains up to
        # definition_version and process_definition. We insert a self-contained
        # chain (parent-first) so append_event's event row has a valid FK target.
        with engine.begin() as conn:
            conn.execute(
                text("INSERT INTO process_definition (id, process_key) "
                     "VALUES (CAST(:id AS uuid), :pk)"),
                {"id": pd_id, "pk": "idem_test"},
            )
            conn.execute(
                text("INSERT INTO definition_version (id, definition_id, version, pdd, status) "
                     "VALUES (CAST(:id AS uuid), CAST(:def AS uuid), :v, CAST(:pdd AS jsonb), :st)"),
                {"id": dv_id, "def": pd_id, "v": 1, "pdd": "{}", "st": "draft"},
            )
            conn.execute(
                text("INSERT INTO \"transaction\" (id, definition_version_id, status, data_snapshot) "
                     "VALUES (CAST(:id AS uuid), CAST(:dv AS uuid), :st, CAST(:snap AS jsonb))"),
                {"id": txn_id, "dv": dv_id, "st": "running", "snap": "{}"},
            )
        print(f"[1] Created parent transaction id={txn_id}")

        # STEP 2 — first call: writes a new event and claims the idempotency key.
        # WHY asyncio.run on the raw coroutine: append_event is an @activity.defn
        # async function; outside a Temporal worker it is still a normal coroutine
        # we can drive directly with asyncio.run.
        event_id_1 = asyncio.run(append_event(
            txn_id, "test", "SMOKE", "tester", "first call", {"x": 1},
            idempotency_key=IDEMPOTENCY_KEY,
        ))
        print(f"[2] First  append_event -> event_id_1 = {event_id_1}")

        # STEP 3 — second call with the SAME key: must be a no-op replay.
        # WHY: this is the idempotency check — it should NOT create a 2nd event,
        # and should return the SAME id as the first call.
        event_id_2 = asyncio.run(append_event(
            txn_id, "test", "SMOKE", "tester", "second call (duplicate key)", {"x": 2},
            idempotency_key=IDEMPOTENCY_KEY,
        ))
        print(f"[3] Second append_event -> event_id_2 = {event_id_2}")

        # STEP 4 — verify exactly ONE event exists for this transaction.
        # WHY count on (transaction_id, type): proves the duplicate call did not
        # append a second SMOKE event despite being invoked with a fresh payload.
        with engine.connect() as conn:
            count = conn.execute(
                text("SELECT count(*) FROM event "
                     "WHERE transaction_id = CAST(:txn AS uuid) AND type = :type"),
                {"txn": txn_id, "type": "SMOKE"},
            ).scalar_one()
        print(f"[4] SMOKE event rows for this transaction = {count}  (expected 1)")

        # STEP 5 — assertions + verdict.
        assert event_id_1 == event_id_2, f"ids differ: {event_id_1} != {event_id_2}"
        assert count == 1, f"expected exactly 1 event, found {count}"
        passed = True
        print("\nIDEMPOTENCY TEST PASSED")

    finally:
        # STEP 6 — cleanup, ALWAYS (even on failure) so the DB is left clean.
        # WHY reverse FK order: idempotency_key -> event -> transaction ->
        # definition_version -> process_definition. idempotency_key.event_id is a
        # FK to event.id, so the key row MUST be deleted before the event row.
        # We delete by the parent transaction id / key, so leftovers are removed
        # regardless of how far the test got.
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM idempotency_key WHERE key = :key"),
                         {"key": IDEMPOTENCY_KEY})
            conn.execute(text("DELETE FROM event WHERE transaction_id = CAST(:txn AS uuid)"),
                         {"txn": txn_id})
            conn.execute(text('DELETE FROM "transaction" WHERE id = CAST(:id AS uuid)'),
                         {"id": txn_id})
            conn.execute(text("DELETE FROM definition_version WHERE id = CAST(:id AS uuid)"),
                         {"id": dv_id})
            conn.execute(text("DELETE FROM process_definition WHERE id = CAST(:id AS uuid)"),
                         {"id": pd_id})
        print("[6] Cleaned up test rows (idempotency_key -> event -> transaction -> "
              "definition_version -> process_definition)")
        engine.dispose()

    # Non-zero exit on failure so a caller/CI can detect it.
    if not passed:
        sys.exit(1)


if __name__ == "__main__":
    main()
