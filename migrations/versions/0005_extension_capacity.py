"""Durable extension capacity and indexed cleanup discovery; retain historical intents."""

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "extension_slots",
        sa.Column("operation_id", sa.String(160), sa.ForeignKey("general_operations.id"), primary_key=True),
        sa.Column("handler", sa.String(200), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_extension_slots_handler_active", "extension_slots", ["handler", "active"])
    op.create_index(
        "ix_toolkit_runs_pending_cleanup",
        "toolkit_runs",
        ["run_id"],
        postgresql_where=sa.text("CAST(state ->> 'cleanup_state' AS VARCHAR) = 'pending'"),
    )
    op.create_index(
        "ix_general_runs_pending_cleanup",
        "general_runs",
        ["run_id"],
        postgresql_where=sa.text("CAST(data ->> 'cleanup_state' AS VARCHAR) = 'pending'"),
    )
    # Conservatively account for E2B acquisitions already dispatched by the first adapter.
    op.execute("""
        INSERT INTO extension_slots (operation_id, handler, active, created_at)
        SELECT id, 'e2b.python.v1', true, CURRENT_TIMESTAMP FROM general_operations
        WHERE data -> 'handler_state' ->> 'operation_digest' IS NOT NULL
          AND data -> 'handler_state' ->> 'phase' != 'finished'
          AND data ->> 'result' IS NULL
    """)


def downgrade():
    raise RuntimeError(
        "Provider capacity and recovery evidence must be retained; automatic downgrade is disabled"
    )
