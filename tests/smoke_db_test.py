# WHY this file exists:
# A one-off, self-contained smoke test that proves the freshly-migrated
# workflow_app schema is actually usable end-to-end from the api service's
# environment: we can INSERT the core row chain (process_definition ->
# definition_version -> transaction -> event) inside a SINGLE database
# transaction, READ it back, and then fully clean up so the database is left
# exactly as we found it. It is intentionally not a pytest test — it is a
# runnable script so it can be executed directly against a live dev database.

import sys
import uuid
import traceback

from sqlalchemy import create_engine, text

# WHY hard-code the URL here: this is a throwaway dev smoke test that targets
# the docker-compose Postgres directly; there is no app config to import yet.
DB_URL = "postgresql+psycopg://app:app@localhost:5432/workflow_app"


# WHY a shared cleanup helper: the delete chain is needed both on the happy
# path (step 3) and as a safety net in `finally` if something fails after the
# inserts committed. Keeping it in one place guarantees the two stay identical.
# Deletes run in REVERSE FK order (child -> parent) so we never violate a
# foreign key while removing rows.
def cleanup(conn, ids):
    conn.execute(text('DELETE FROM event WHERE id = CAST(:id AS uuid)'), {"id": ids["event"]})
    conn.execute(text('DELETE FROM "transaction" WHERE id = CAST(:id AS uuid)'), {"id": ids["transaction"]})
    conn.execute(text('DELETE FROM definition_version WHERE id = CAST(:id AS uuid)'), {"id": ids["definition_version"]})
    conn.execute(text('DELETE FROM process_definition WHERE id = CAST(:id AS uuid)'), {"id": ids["process_definition"]})


def main():
    engine = create_engine(DB_URL)

    # WHY generate all ids up front: we need to reference each id across the
    # insert, read-back, and cleanup phases. uuid.uuid4() gives us the values;
    # we bind them as strings and CAST(... AS uuid) so the server coerces them
    # into the UUID columns explicitly (no reliance on implicit adaptation).
    ids = {
        "process_definition": str(uuid.uuid4()),
        "definition_version": str(uuid.uuid4()),
        "transaction": str(uuid.uuid4()),
        "event": str(uuid.uuid4()),
    }
    print("Generated ids:")
    for k, v in ids.items():
        print(f"  {k:20s} = {v}")

    inserts_committed = False
    try:
        # STEP 1 — insert the full chain inside ONE transaction.
        # WHY a single `with engine.begin()` block: it opens one DB transaction
        # and commits only if all four inserts succeed (otherwise it rolls the
        # whole thing back). WHY this exact order: each row has a FK to the row
        # above it, so parents must exist before children are inserted.
        with engine.begin() as conn:
            # process_definition: the top-level parent; nothing references
            # anything yet, so it must be inserted first.
            conn.execute(
                text(
                    "INSERT INTO process_definition (id, process_key) "
                    "VALUES (CAST(:id AS uuid), :process_key)"
                ),
                {"id": ids["process_definition"], "process_key": "smoke_test"},
            )

            # definition_version: FK -> process_definition, so it comes second.
            # pdd is JSONB, so bind '{}' and CAST(... AS jsonb).
            conn.execute(
                text(
                    "INSERT INTO definition_version (id, definition_id, version, pdd, status) "
                    "VALUES (CAST(:id AS uuid), CAST(:definition_id AS uuid), :version, CAST(:pdd AS jsonb), :status)"
                ),
                {
                    "id": ids["definition_version"],
                    "definition_id": ids["process_definition"],
                    "version": 1,
                    "pdd": "{}",
                    "status": "draft",
                },
            )

            # transaction: FK -> definition_version, so it comes third.
            # Table name is quoted because TRANSACTION is a SQL keyword.
            conn.execute(
                text(
                    'INSERT INTO "transaction" (id, definition_version_id, status, data_snapshot) '
                    "VALUES (CAST(:id AS uuid), CAST(:definition_version_id AS uuid), :status, CAST(:data_snapshot AS jsonb))"
                ),
                {
                    "id": ids["transaction"],
                    "definition_version_id": ids["definition_version"],
                    "status": "running",
                    "data_snapshot": "{}",
                },
            )

            # event: FK -> transaction, so it is inserted last.
            conn.execute(
                text(
                    "INSERT INTO event (id, transaction_id, type, payload, actor) "
                    "VALUES (CAST(:id AS uuid), CAST(:transaction_id AS uuid), :type, CAST(:payload AS jsonb), :actor)"
                ),
                {
                    "id": ids["event"],
                    "transaction_id": ids["transaction"],
                    "type": "SMOKE",
                    "payload": "{}",
                    "actor": "smoke-test",
                },
            )
        inserts_committed = True
        print("\n[1] Inserted process_definition + definition_version + transaction + event in ONE transaction (committed).")

        # STEP 2 — read the data back in a SEPARATE connection.
        # WHY a separate read: proving the rows survived the commit (i.e. they
        # are really persisted, not just visible inside the writing transaction)
        # requires querying them again on a fresh connection.
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT t.id AS tx_id, t.status AS tx_status, "
                    "       e.id AS ev_id, e.type AS ev_type, e.actor AS ev_actor "
                    'FROM "transaction" t '
                    "JOIN event e ON e.transaction_id = t.id "
                    "WHERE t.id = CAST(:tx_id AS uuid)"
                ),
                {"tx_id": ids["transaction"]},
            ).mappings().all()

        print(f"\n[2] Read back {len(rows)} joined transaction+event row(s):")
        for r in rows:
            print(f"    {dict(r)}")
        if len(rows) != 1:
            raise AssertionError(f"expected exactly 1 joined row, got {len(rows)}")

        # STEP 3 — clean up so no test data remains.
        # WHY reverse order: delete children before parents to respect the FKs.
        with engine.begin() as conn:
            cleanup(conn, ids)
        inserts_committed = False  # cleanup succeeded; nothing left to remove
        print("\n[3] Cleaned up all inserted rows (event -> transaction -> definition_version -> process_definition).")

        # STEP 4 — final proof of success.
        print("\nSMOKE TEST PASSED")

    except Exception:
        # WHY print AND re-raise: make the failure obvious in the output and
        # also exit non-zero so a caller/CI can detect it.
        print("\nSMOKE TEST FAILED", file=sys.stderr)
        traceback.print_exc()
        raise
    finally:
        # Safety net: if the inserts committed but we never reached the normal
        # cleanup (e.g. the read-back assertion failed), remove the leftovers so
        # the test is still self-cleaning and re-runnable.
        if inserts_committed:
            try:
                with engine.begin() as conn:
                    cleanup(conn, ids)
                print("[cleanup] removed leftover test rows in finally block")
            except Exception:
                print("[cleanup] WARNING: safety-net cleanup failed; manual cleanup may be needed", file=sys.stderr)
        engine.dispose()


if __name__ == "__main__":
    main()
