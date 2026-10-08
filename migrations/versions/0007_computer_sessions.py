"""Reusable computer ownership, expiry and durable capacity identity."""

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "computer_sessions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("session_id", sa.String(36), sa.ForeignKey("sessions.id"), nullable=False),
        sa.Column("name", sa.String(40), nullable=False),
        sa.Column("provider", sa.String(40), nullable=False),
        sa.Column("created_by_run_id", sa.String(36), sa.ForeignKey("runs.id"), nullable=False),
        sa.Column(
            "capacity_operation_id",
            sa.String(160),
            sa.ForeignKey("general_operations.id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("idle_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.UniqueConstraint("session_id", "name", name="uq_computer_session_name"),
    )
    op.create_index("ix_computer_sessions_session_id", "computer_sessions", ["session_id"])
    op.create_index(
        "ix_computer_sessions_cleanup", "computer_sessions", ["status", "idle_expires_at", "expires_at"]
    )


def downgrade():
    raise RuntimeError("Computer handles and retained admission must not be dropped automatically")
