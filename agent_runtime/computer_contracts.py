"""Runweave-owned computer identities; provider handles and credentials stay private."""

from datetime import datetime
from typing import Literal

from pydantic import Field

from .general_contracts import Contract


class ComputerSession(Contract):
    id: str
    session_id: str
    name: str
    provider: str
    status: Literal["creating", "ready", "busy", "closing", "closed", "unknown"]
    created_at: datetime
    expires_at: datetime
    idle_expires_at: datetime
    operation_count: int = Field(ge=0)


class ComputerCleanupAttestation(Contract):
    evidence_ref: str = Field(min_length=1, max_length=500)


def describe(row):
    return ComputerSession(
        id=row.id,
        session_id=row.session_id,
        name=row.name,
        provider=row.provider,
        status=row.status,
        created_at=row.created_at,
        expires_at=row.expires_at,
        idle_expires_at=row.idle_expires_at,
        operation_count=row.data.get("operation_count", 0),
    )
