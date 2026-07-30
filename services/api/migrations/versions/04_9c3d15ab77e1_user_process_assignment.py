"""user -> process assignment (which workflows a person takes part in)

Many workflows now run side by side and they share role names: "manager" exists
in both invoice_approval and leave_request. This table says WHICH workflows each
person belongs to, so a manager task in invoice only reaches the managers
assigned to invoice.

Keyed by process_key (the stable process name) rather than a version id, so
assignments survive every re-publish of a workflow.

Revision ID: 9c3d15ab77e1
Revises: 2b8af91d5853
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "9c3d15ab77e1"
down_revision: Union[str, Sequence[str], None] = "2b8af91d5853"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "user_process",
        sa.Column("username", sa.Text(), nullable=False),
        sa.Column("process_key", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("username", "process_key", name="pk_user_process"),
    )
    op.create_index("ix_user_process_username", "user_process", ["username"])
    op.create_index("ix_user_process_process_key", "user_process", ["process_key"])


def downgrade() -> None:
    op.drop_index("ix_user_process_process_key", table_name="user_process")
    op.drop_index("ix_user_process_username", table_name="user_process")
    op.drop_table("user_process")
