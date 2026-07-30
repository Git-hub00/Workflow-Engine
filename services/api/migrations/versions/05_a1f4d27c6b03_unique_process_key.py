"""one process_definition row per process_key

WHY: process_definition.process_key had no unique constraint. Two concurrent
first-publishes of the same workflow therefore created TWO definition rows, and
because definition_version.version is numbered per definition_id, both started at
version 1. Every later "latest published version" read (GET /v1/definitions/{key},
POST /v1/transactions, GET /v1/config/{key}) then picked between two DIFFERENT
workflows nondeterministically, while the catalog's GROUP BY process_key hid the
duplication entirely — a workflow that silently changed shape between runs.

The unique index also lets create_definition use
INSERT ... ON CONFLICT (process_key) DO NOTHING, which closes the race properly.

Duplicates that already exist are merged first: the OLDEST definition row is kept,
its versions are renumbered to sit after the ones already there, and the extra
definition rows are removed. Transactions keep pointing at their own
definition_version row, so in-flight runs are unaffected.

Revision ID: a1f4d27c6b03
Revises: 9c3d15ab77e1
"""
from typing import Sequence, Union

from alembic import op

revision: str = "a1f4d27c6b03"
down_revision: Union[str, Sequence[str], None] = "9c3d15ab77e1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()

    # 1. Move every version from a duplicate definition onto the kept (oldest) one.
    #    Versions are renumbered with a SINGLE window function over the whole group,
    #    so N duplicates cannot collide with each other. (Computing one MAX-based
    #    offset per duplicate, all from the same snapshot, gave two duplicates sitting
    #    at v1 the SAME new number and the migration failed on the unique constraint.)
    conn.exec_driver_sql(
        """
        WITH ranked AS (
            SELECT id, process_key,
                   ROW_NUMBER() OVER (PARTITION BY process_key ORDER BY id) AS rn
            FROM process_definition
        ),
        keeper AS (SELECT process_key, id AS keep_id FROM ranked WHERE rn = 1),
        mapped AS (
            SELECT r.id AS old_id, k.keep_id, r.rn
            FROM ranked r JOIN keeper k ON k.process_key = r.process_key
        ),
        renumbered AS (
            SELECT dv.id AS dv_id, m.keep_id,
                   ROW_NUMBER() OVER (PARTITION BY m.keep_id
                                      ORDER BY m.rn, dv.version, dv.id) AS new_version
            FROM definition_version dv
            JOIN mapped m ON m.old_id = dv.definition_id
        )
        UPDATE definition_version dv
        SET definition_id = r.keep_id,
            version = r.new_version
        FROM renumbered r
        WHERE dv.id = r.dv_id
        """
    )

    # 2. Delete the now-empty duplicate definition rows.
    conn.exec_driver_sql(
        """
        DELETE FROM process_definition pd
        WHERE EXISTS (
            SELECT 1 FROM process_definition other
            WHERE other.process_key = pd.process_key AND other.id < pd.id
        )
        """
    )

    # 3. Enforce it from now on.
    op.create_unique_constraint(
        "uq_process_definition_process_key", "process_definition", ["process_key"]
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_process_definition_process_key", "process_definition", type_="unique"
    )
