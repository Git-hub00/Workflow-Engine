"""add submitted_by to transaction

Revision ID: 2b8af91d5853
Revises: 8f4a2c1d9b70
Create Date: 2026-07-15 10:46:33.714678

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '2b8af91d5853'
down_revision: Union[str, Sequence[str], None] = '8f4a2c1d9b70'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # WHY: track WHO submitted an invoice so a vendor can see the request_info
    # tasks for THEIR OWN invoices in the vendor portal. Nullable so existing
    # rows (and any anonymous/untokened POST /v1/transactions) remain valid.
    op.add_column("transaction", sa.Column("submitted_by", sa.Text(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("transaction", "submitted_by")
