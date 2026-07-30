"""create workflow_app core tables

Revision ID: 25e751407d6a
Revises: 
Create Date: 2026-07-10 13:47:28.737145

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = '25e751407d6a'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# upgrade(): builds the entire workflow_app core schema in one revision.
# Tables are created in dependency order so every FK target already exists
# before the referencing table is created.
def upgrade() -> None:
    """Upgrade schema."""

    # process_definition: the catalog of workflow "process keys" — the stable
    # identity of a business process. Owned/edited via the API; every concrete
    # version in definition_version points back here.
    op.create_table(
        'process_definition',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('process_key', sa.Text(), nullable=False),
        sa.Column('retention_policy', postgresql.JSONB(), server_default=sa.text("'{}'")),
    )

    # definition_version: immutable, versioned snapshots of a process definition
    # document (pdd). One row per (definition, version); draft→published lifecycle
    # via status. A transaction pins the exact version it runs, so old runs stay
    # reproducible after newer versions ship.
    op.create_table(
        'definition_version',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('definition_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('process_definition.id')),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('pdd', postgresql.JSONB(), nullable=False),
        sa.Column('status', sa.Text(), nullable=False, server_default='draft'),
        sa.Column('published_at', sa.TIMESTAMP(timezone=True)),
        sa.UniqueConstraint('definition_id', 'version'),
    )

    # transaction: a single running instance (case) of a definition_version,
    # bound to its Temporal workflow/run ids. This is the central runtime record
    # read by the API, the worker, and the monitor; data_snapshot holds the
    # current process variables.
    op.create_table(
        'transaction',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('definition_version_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('definition_version.id')),
        sa.Column('temporal_workflow_id', sa.Text()),
        sa.Column('temporal_run_id', sa.Text()),
        sa.Column('status', sa.Text(), nullable=False),
        sa.Column('data_snapshot', postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column('created_at', sa.TIMESTAMP(timezone=True), server_default=sa.text('now()')),
        sa.Column('closed_at', sa.TIMESTAMP(timezone=True)),
    )

    # event: the immutable, append-only audit log — one row per action taken on a
    # transaction, chained via before_hash/after_hash for tamper-evidence. The
    # monotonic seq (BIGSERIAL) gives a global ordering; read by the monitor and
    # auditors, and referenced by idempotency_key.
    op.create_table(
        'event',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('transaction_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('transaction.id')),
        sa.Column('seq', sa.BigInteger(), autoincrement=True),
        sa.Column('type', sa.Text()),
        sa.Column('payload', postgresql.JSONB()),
        sa.Column('actor', sa.Text()),
        sa.Column('before_hash', sa.Text()),
        sa.Column('after_hash', sa.Text()),
        sa.Column('occurred_at', sa.TIMESTAMP(timezone=True), server_default=sa.text('now()')),
    )

    # task: a unit of work spawned by a transaction node (typically a human task).
    # Carries the assignment (assigned_role), a claim token, and open→done status.
    # Powers the task-inbox UI and worker polling; ix_task_role_status makes the
    # "open tasks for my role" query fast.
    op.create_table(
        'task',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('transaction_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('transaction.id')),
        sa.Column('node_id', sa.Text()),
        sa.Column('token', sa.Text(), unique=True),
        sa.Column('assigned_role', sa.Text()),
        sa.Column('status', sa.Text(), nullable=False, server_default='open'),
        sa.Column('completion_policy', postgresql.JSONB()),
        sa.Column('claimed_by', sa.Text()),
        sa.Column('created_at', sa.TIMESTAMP(timezone=True), server_default=sa.text('now()')),
    )

    # participant_task: per-participant fan-out of a task for multi-actor decisions
    # (e.g. parallel approvals/votes). Each participant's individual claim and
    # decision is recorded here and aggregated against the parent task's
    # completion_policy.
    op.create_table(
        'participant_task',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('task_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('task.id')),
        sa.Column('participant', sa.Text()),
        sa.Column('claimed_by', sa.Text()),
        sa.Column('decision', postgresql.JSONB()),
        sa.Column('status', sa.Text(), server_default='open'),
    )

    # idempotency_key: dedup guard mapping an externally-supplied idempotency key
    # to the event it produced. Lets command handlers safely retry without
    # emitting duplicate events / side effects.
    op.create_table(
        'idempotency_key',
        sa.Column('key', sa.Text(), primary_key=True),
        sa.Column('event_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('event.id')),
    )

    # ix_task_role_status: composite index backing the hot "give me open tasks for
    # role X" queue query issued by the task-inbox UI and the worker.
    op.create_index('ix_task_role_status', 'task', ['assigned_role', 'status'])


# downgrade(): tears the schema back down in exact reverse dependency order so a
# child table (and its FKs) is always removed before the parent it references.
def downgrade() -> None:
    """Downgrade schema."""

    # Drop the index first (created last), then the tables in reverse order.
    op.drop_index('ix_task_role_status', table_name='task')
    op.drop_table('idempotency_key')
    op.drop_table('participant_task')
    op.drop_table('task')
    op.drop_table('event')
    op.drop_table('transaction')
    op.drop_table('definition_version')
    op.drop_table('process_definition')
