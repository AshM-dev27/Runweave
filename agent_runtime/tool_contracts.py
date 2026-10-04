"""Application-owned toolkit contracts, independent of execution frameworks."""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class DTO(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SubagentSpec(DTO):
    name: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,39}$")
    agent_id: str
    description: str = Field(max_length=512)


class ArtifactRef(DTO):
    id: str
    sha256: str
    media_type: str
    size_bytes: int
    filename: str
    created_at: datetime
    producer_run_id: str | None = None


class ToolDescriptor(DTO):
    alias: str
    registration_id: str
    schema_version: int = 1
    description: str
    arguments_schema: dict[str, Any]
    result_schema: dict[str, Any]
    effect: Literal["read", "artifact_write", "approval_write", "delegate"]
    permissions: list[str]
    requires_approval: bool
    timeout_seconds: int = 30
    input_bytes: int = 16384
    result_bytes: int = 16384
    retry_policy: str = "reconcile"
    availability: Literal["available", "unverified", "unavailable"] = Field(
        default="unverified",
        description="Installed registration; external service health is unverified, even when configured.",
    )


class TaskSpec(DTO):
    specialist: str
    instruction: str = Field(min_length=1, max_length=4000)
    artifact_ids: list[str] = Field(default_factory=list, max_length=8)


class EvidenceRef(DTO):
    artifact_id: str
    sha256: str
    path: str | None = None
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    quote: str = Field(max_length=512)


class TaskResult(DTO):
    status: Literal["completed", "failed", "cancelled"]
    summary: str = Field(max_length=2000)
    evidence: list[EvidenceRef] = Field(default_factory=list, max_length=12)
    artifacts: list[ArtifactRef] = Field(default_factory=list, max_length=8)
    usage: dict[str, int] = Field(default_factory=dict)
    error: str | None = None


class RunBudget(DTO):
    """V2 root ledger, or v1 recorded usage with unmeasured counters/total-token limit null."""

    execution_version: Literal[1, 2, 3]
    accounting_mode: Literal["recorded_successful_usage", "shared_ledger"]
    requests: int | None
    tool_calls: int | None
    reported_tokens: int | None
    reserved_tokens: int | None
    max_requests: int
    max_tool_calls: int
    max_total_tokens: int | None
    v3: dict | None = None
    adaptive: dict | None = None
    successful_usage: dict[str, int] | None = Field(
        default=None,
        description="V1 persisted Run.usage; empty means no usage recorded yet, not zero physical attempts. V2 uses the shared ledger.",
    )
