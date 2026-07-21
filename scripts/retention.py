#!/usr/bin/env python3
"""Data-retention report + guarded purge (P7).

Reads each process_definition.retention_policy (JSONB) merged over a global
default, classifies CLOSED transactions by closed_at age, and reports how many
are purge-due. DEFAULT = REPORT ONLY (no changes). Pass --apply to actually purge
(delete) purge-due transactions and their children in FK-safe order. Transactions
under legal_hold are always skipped.

Retention policy fields (all optional; per-process overrides the default):
    { "hot_months": 12, "archive_after_months": 12, "purge_after_months": 84,
      "legal_hold": false }

Usage:
    python scripts/retention.py            # safe report (dry run)
    python scripts/retention.py --apply    # purge purge-due transactions
"""
import os
import sys
from datetime import datetime, timezone

from sqlalchemy import create_engine, text

DB_URL = os.getenv("DATABASE_URL", "postgresql+psycopg://app:app@localhost:5432/workflow_app")

# SMB defaults from the spec: hot 12mo -> archive 12mo -> purge 7 years (84mo).
DEFAULT_POLICY = {
    "hot_months": 12,
    "archive_after_months": 12,
    "purge_after_months": 84,
    "legal_hold": False,
}


def months_between(earlier: datetime, later: datetime) -> int:
    return (later.year - earlier.year) * 12 + (later.month - earlier.month)


def _purge_txn(engine, txn_id: str) -> None:
    # Delete children before parents (no ON DELETE CASCADE assumed).
    with engine.begin() as conn:
        conn.execute(text(
            "DELETE FROM idempotency_key WHERE event_id IN "
            "(SELECT id FROM event WHERE transaction_id = CAST(:t AS uuid))"), {"t": txn_id})
        conn.execute(text(
            "DELETE FROM participant_task WHERE task_id IN "
            "(SELECT id FROM task WHERE transaction_id = CAST(:t AS uuid))"), {"t": txn_id})
        conn.execute(text("DELETE FROM event WHERE transaction_id = CAST(:t AS uuid)"), {"t": txn_id})
        conn.execute(text("DELETE FROM task WHERE transaction_id = CAST(:t AS uuid)"), {"t": txn_id})
        conn.execute(text('DELETE FROM "transaction" WHERE id = CAST(:t AS uuid)'), {"t": txn_id})


def main(argv) -> int:
    apply = "--apply" in argv[1:]
    engine = create_engine(DB_URL)
    now = datetime.now(timezone.utc)

    with engine.connect() as conn:
        procs = conn.execute(
            text("SELECT id, process_key, retention_policy FROM process_definition")
        ).mappings().all()

    total_purge_due = 0
    total_purged = 0
    for p in procs:
        policy = {**DEFAULT_POLICY, **(p["retention_policy"] or {})}
        purge_after = policy["purge_after_months"]
        legal_hold = bool(policy.get("legal_hold"))
        with engine.connect() as conn:
            rows = conn.execute(
                text('SELECT tr.id, tr.closed_at FROM "transaction" tr '
                     "JOIN definition_version dv ON dv.id = tr.definition_version_id "
                     "WHERE dv.definition_id = CAST(:d AS uuid) AND tr.closed_at IS NOT NULL"),
                {"d": str(p["id"])},
            ).mappings().all()
        purge_due = [r for r in rows if months_between(r["closed_at"], now) >= purge_after]
        total_purge_due += len(purge_due)
        print(f"process {p['process_key']!r}: {len(rows)} closed, {len(purge_due)} purge-due "
              f"(purge_after={purge_after}mo, legal_hold={legal_hold})")
        if apply and purge_due and not legal_hold:
            for r in purge_due:
                _purge_txn(engine, str(r["id"]))
                total_purged += 1

    engine.dispose()
    if apply:
        print(f"\nPURGED {total_purged} transaction(s).")
    else:
        print(f"\nDRY RUN — {total_purge_due} transaction(s) are purge-due. Re-run with --apply to delete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
