"""Durable conversation ownership, exclusive use and retained provider admission."""

from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base, now


class ComputerSessionRow(Base):
    __tablename__ = "computer_sessions"
    __table_args__ = (
        UniqueConstraint("session_id", "name", name="uq_computer_session_name"),
        Index("ix_computer_sessions_cleanup", "status", "idle_expires_at", "expires_at"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), index=True)
    name: Mapped[str] = mapped_column(String(40))
    provider: Mapped[str] = mapped_column(String(40))
    created_by_run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"))
    capacity_operation_id: Mapped[str] = mapped_column(ForeignKey("general_operations.id"), unique=True)
    status: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    idle_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    data: Mapped[dict] = mapped_column(JSON)
