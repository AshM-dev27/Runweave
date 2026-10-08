from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class GateRow(Base):
    __tablename__ = "workspace_gate"
    id: Mapped[int] = mapped_column(primary_key=True)


class AgentRow(Base):
    __tablename__ = "agents"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    config: Mapped[dict] = mapped_column(JSON)


class SessionRow(Base):
    __tablename__ = "sessions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    history: Mapped[list] = mapped_column(JSON, default=list)


class RegistrationRow(Base):
    __tablename__ = "model_registrations"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    config: Mapped[dict] = mapped_column(JSON)


class RunRow(Base):
    __tablename__ = "runs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), index=True)
    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id"))
    key: Mapped[str] = mapped_column(String(128), unique=True)
    fingerprint: Mapped[str] = mapped_column(String(64))
    config: Mapped[dict] = mapped_column(JSON)
    registration_id: Mapped[str | None] = mapped_column(ForeignKey("model_registrations.id"), nullable=True)
    approval_wait_seconds: Mapped[int] = mapped_column(Integer, default=86400)
    input: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), index=True, default="queued")
    output: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(String(100), nullable=True)
    approvals: Mapped[list] = mapped_column(JSON, default=list)
    decisions: Mapped[dict] = mapped_column(JSON, default=dict)
    usage: Mapped[dict] = mapped_column(JSON, default=dict)
    next_event: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class EventRow(Base):
    __tablename__ = "events"
    __table_args__ = (UniqueConstraint("run_id", "dedupe"),)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), primary_key=True)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    dedupe: Mapped[str] = mapped_column(String(200))
    type: Mapped[str] = mapped_column(String(80))
    data: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class OutboxRow(Base):
    __tablename__ = "outbox"
    id: Mapped[str] = mapped_column(String(160), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    kind: Mapped[str] = mapped_column(String(20))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    delivered: Mapped[bool] = mapped_column(default=False, index=True)


class NoteRow(Base):
    __tablename__ = "notes"
    operation_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"))
    text: Mapped[str] = mapped_column(Text)


class Database:
    def __init__(self, url: str, schema: str = "public"):
        options = (
            {"connect_args": {"server_settings": {"search_path": schema}}}
            if url.startswith("postgresql")
            else {}
        )
        self.engine = create_async_engine(url, pool_pre_ping=True, **options)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    async def create_test_schema(self):
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with self.sessions.begin() as db:
            if await db.get(GateRow, 1) is None:
                db.add(GateRow(id=1))

    async def close(self):
        await self.engine.dispose()


class ToolkitRunRow(Base):
    __tablename__ = "toolkit_runs"
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), primary_key=True)
    root_id: Mapped[str] = mapped_column(String(36), index=True)
    parent_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    state: Mapped[dict] = mapped_column(JSON)


Index(
    "ix_toolkit_runs_pending_cleanup",
    ToolkitRunRow.run_id,
    postgresql_where=ToolkitRunRow.state["cleanup_state"].as_string() == "pending",
    sqlite_where=ToolkitRunRow.state["cleanup_state"].as_string() == "pending",
)


class ArtifactRow(Base):
    __tablename__ = "artifacts"
    __table_args__ = (
        CheckConstraint(
            "(content IS NOT NULL AND blob_key IS NULL) OR (content IS NULL AND blob_key IS NOT NULL)",
            name="ck_artifacts_content_location",
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    key: Mapped[str] = mapped_column(String(160), unique=True)
    sha256: Mapped[str] = mapped_column(String(64))
    media_type: Mapped[str] = mapped_column(String(80))
    filename: Mapped[str] = mapped_column(String(100))
    size_bytes: Mapped[int] = mapped_column(Integer)
    content: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    blob_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    producer_run_id: Mapped[str | None] = mapped_column(ForeignKey("runs.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ToolkitOperationRow(Base):
    __tablename__ = "toolkit_operations"
    id: Mapped[str] = mapped_column(String(160), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    digest: Mapped[str] = mapped_column(String(64))
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
