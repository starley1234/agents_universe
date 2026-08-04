"""initial schema

Revision ID: 0001_initial
Revises:
Create Date: 2026-08-04
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None

class Vector(sa.types.UserDefinedType):
    cache_ok = True
    def __init__(self, dimensions: int):
        self.dimensions = dimensions
    def get_col_spec(self, **kw):
        return f"vector({self.dimensions})"

TASK_STATUS = postgresql.ENUM(
    "PENDING",
    "RUNNING",
    "PAUSED",
    "AWAITING_USER",
    "SLEEPING",
    "FAILED",
    "COMPLETED",
    "ROLLED_BACK",
    name="taskstatus",
    create_type=False,
)


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'taskstatus') THEN
                CREATE TYPE taskstatus AS ENUM (
                    'PENDING', 'RUNNING', 'PAUSED', 'AWAITING_USER',
                    'SLEEPING', 'FAILED', 'COMPLETED', 'ROLLED_BACK'
                );
            END IF;
        END
        $$;
        """
    )
    op.create_table(
        "tasks",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("goal", sa.Text(), nullable=False),
        sa.Column("status", TASK_STATUS, nullable=False, server_default="PENDING"),
        sa.Column("current_state_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="{}"),
        sa.Column("budget_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="{}"),
        sa.Column("workspace_path", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_tasks_status", "tasks", ["status"])
    op.create_table(
        "task_snapshots",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("task_id", sa.Uuid(), sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("iteration", sa.Integer(), nullable=False),
        sa.Column("state_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("confidence", sa.Numeric(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_snapshots_task_iteration", "task_snapshots", ["task_id", "iteration"])
    op.create_table(
        "task_events",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("task_id", sa.Uuid(), sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("payload_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_events_task_created", "task_events", ["task_id", "created_at"])
    op.create_table(
        "artifacts",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("task_id", sa.Uuid(), sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("metadata_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_table(
        "memory_items",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("task_id", sa.Uuid(), sa.ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("embedding", Vector(1024), nullable=True),
        sa.Column("metadata_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("memory_items")
    op.drop_table("artifacts")
    op.drop_index("ix_events_task_created", table_name="task_events")
    op.drop_table("task_events")
    op.drop_index("ix_snapshots_task_iteration", table_name="task_snapshots")
    op.drop_table("task_snapshots")
    op.drop_index("ix_tasks_status", table_name="tasks")
    op.drop_table("tasks")
    op.execute("DROP TYPE IF EXISTS taskstatus")
