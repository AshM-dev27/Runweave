"""Additive v3 persistence; legacy rows and JSON are never migrated in place."""

from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base, now


class GeneralRunRow(Base):
    __tablename__ = "general_runs"
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), primary_key=True)
    root_id: Mapped[str] = mapped_column(String(36), index=True)
    parent_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    data: Mapped[dict] = mapped_column(JSON)


Index(
    "ix_general_runs_pending_cleanup",
    GeneralRunRow.run_id,
    postgresql_where=GeneralRunRow.data["cleanup_state"].as_string() == "pending",
    sqlite_where=GeneralRunRow.data["cleanup_state"].as_string() == "pending",
)


class GeneralRecordRow(Base):
    __tablename__ = "general_records"
    __table_args__ = (UniqueConstraint("run_id", "kind", "sequence"),)
    id: Mapped[str] = mapped_column(String(160), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("general_runs.run_id"), index=True)
    kind: Mapped[str] = mapped_column(String(32))
    sequence: Mapped[int] = mapped_column(Integer)
    data: Mapped[dict] = mapped_column(JSON)


class GeneralOperationRow(Base):
    __tablename__ = "general_operations"
    id: Mapped[str] = mapped_column(String(160), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("general_runs.run_id"), index=True)
    fingerprint: Mapped[str] = mapped_column(String(64))
    data: Mapped[dict] = mapped_column(JSON)


class GeneralAttemptRow(Base):
    __tablename__ = "general_attempts"
    __table_args__ = (UniqueConstraint("operation_id", "ordinal"),)
    id: Mapped[str] = mapped_column(String(160), primary_key=True)
    operation_id: Mapped[str] = mapped_column(ForeignKey("general_operations.id"))
    ordinal: Mapped[int] = mapped_column(Integer)
    data: Mapped[dict] = mapped_column(JSON)


class ExtensionSlotRow(Base):
    """Durable provider admission: uncertainty and cleanup retain their capacity."""

    __tablename__ = "extension_slots"
    __table_args__ = (Index("ix_extension_slots_handler_active", "handler", "active"),)
    operation_id: Mapped[str] = mapped_column(ForeignKey("general_operations.id"), primary_key=True)
    handler: Mapped[str] = mapped_column(String(200))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ProjectWorkspaceRow(Base):
    __tablename__ = "project_workspaces"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    key: Mapped[str] = mapped_column(String(160), unique=True)
    fingerprint: Mapped[str] = mapped_column(String(64))
    principal: Mapped[str] = mapped_column(String(64))
    initial_revision: Mapped[str] = mapped_column(String(64))


class ProjectRevisionRow(Base):
    __tablename__ = "project_revisions"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("project_workspaces.id"), index=True)
    data: Mapped[dict] = mapped_column(JSON)


class ProjectBlobRow(Base):
    __tablename__ = "project_blobs"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    content: Mapped[bytes] = mapped_column(LargeBinary)
    length: Mapped[int] = mapped_column(Integer)


class ProjectBranchRow(Base):
    __tablename__ = "project_branches"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("project_workspaces.id"))
    run_id: Mapped[str] = mapped_column(ForeignKey("general_runs.run_id"), unique=True)
    head: Mapped[str] = mapped_column(ForeignKey("project_revisions.id"))
    version: Mapped[int] = mapped_column(Integer, default=1)


class ProjectGrantRow(Base):
    __tablename__ = "project_grants"
    run_id: Mapped[str] = mapped_column(ForeignKey("general_runs.run_id"), primary_key=True)
    branch_id: Mapped[str] = mapped_column(ForeignKey("project_branches.id"))
    data: Mapped[dict] = mapped_column(JSON)


class GeneralEffectRow(Base):
    __tablename__ = "general_effect_records"
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    version: Mapped[int] = mapped_column(Integer)
    value: Mapped[str] = mapped_column(String(4000))
