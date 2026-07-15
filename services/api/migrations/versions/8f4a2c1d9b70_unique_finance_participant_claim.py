"""ensure one participant claim per user and task

Revision ID: 8f4a2c1d9b70
Revises: 25e751407d6a
Create Date: 2026-07-14 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "8f4a2c1d9b70"
down_revision: Union[str, Sequence[str], None] = "25e751407d6a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Prevent one authenticated user from owning two slots on one task."""
    op.create_index(
        "uq_participant_task_task_claimed_by",
        "participant_task",
        ["task_id", "claimed_by"],
        unique=True,
        postgresql_where=sa.text("claimed_by IS NOT NULL"),
    )


def downgrade() -> None:
    """Remove the participant-claim uniqueness guard."""
    op.drop_index(
        "uq_participant_task_task_claimed_by",
        table_name="participant_task",
    )
