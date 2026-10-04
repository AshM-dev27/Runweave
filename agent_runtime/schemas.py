"""Versioned public contracts. No execution-framework types belong here."""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .approval_preview import ApprovalPreview

# Owned public v3 DTO exports; framework-specific model messages stay private.
from .general_contracts import CapabilityDescriptor as CapabilityDescriptor
from .general_contracts import CheckSpec as CheckSpec
from .general_contracts import CompletionAssessment, GeneralPolicy, TaskGoal, TaskState, WorkspaceAttachment
from .general_contracts import Criterion as Criterion
from .general_contracts import DelegationPolicy as DelegationPolicy
from .general_contracts import GeneralLimits as GeneralLimits
from .general_contracts import ProjectManifest as ProjectManifest
from .general_contracts import TaskSnapshot as TaskSnapshot
from .general_contracts import VerificationResult as VerificationResult
from .task_outcomes import OutcomeReason, TaskOutcome
from .tool_contracts import ArtifactRef, SubagentSpec, TaskResult


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AgentConfig(Contract):
    name: str = Field(min_length=1, max_length=100)
    provider: str = Field(min_length=1, max_length=100, pattern=r"^[a-zA-Z0-9._-]+$")
    model: str = Field(min_length=1, max_length=100, pattern=r"^[a-zA-Z0-9._-]+$")
    instructions: str = Field(default="Use tools when appropriate. Return a concise answer.", max_length=8000)
    tools: list[str] = Field(default_factory=lambda: ["add"], max_length=16)
    general: GeneralPolicy | None = None
    subagents: list[SubagentSpec] = Field(default_factory=list, max_length=2)
    max_children: int = Field(default=2, ge=0, le=2)
    delegation_mode: Literal["parallel_read", "sequential"] = "parallel_read"
    adaptive: bool = Field(
        default=True,
        description="Size each new task from its inputs and adapt output/context within pinned limits.",
    )
    max_total_tokens: int | None = Field(default=None, ge=1024, le=1000000)

    @model_validator(mode="after")
    def unique_tools(self):
        if self.general and (self.subagents or "delegate" in self.tools):
            raise ValueError("V3 uses dynamic delegation policy")
        if (
            self.general
            and self.general.delegation
            and not set(self.general.delegation.tools).issubset(self.tools)
        ):
            raise ValueError("Child capabilities must be a subset")
        if len(self.tools) != len(set(self.tools)):
            raise ValueError("Duplicate tool selection")
        if len({s.name for s in self.subagents}) != len(self.subagents):
            raise ValueError("Duplicate specialist name")
        if self.subagents and self.max_children == 0:
            raise ValueError("Delegation requires a positive child limit")
        if bool(self.subagents) != ("delegate" in self.tools):
            raise ValueError("Delegation requires specialists and delegate tool")
        return self

    max_requests: int | None = Field(default=None, ge=1, le=128)
    max_tool_calls: int | None = Field(default=None, ge=1, le=512)
    max_tokens: int | None = Field(
        default=None,
        ge=128,
        le=65536,
        description="Hard per-response output cap; null lets adaptive sizing use the registered model ceiling.",
    )
    timeout_seconds: int | None = Field(default=None, ge=5, le=3600)


class Agent(Contract):
    id: str
    config: AgentConfig


class Session(Contract):
    id: str
    created_at: datetime


class RunCreate(Contract):
    agent_id: str
    session_id: str | None = None
    input: str = Field(min_length=1, max_length=16000)
    artifact_ids: list[str] = Field(default_factory=list, max_length=8)
    task: TaskGoal | None = None
    workspace: WorkspaceAttachment | None = None


class Answer(Contract):
    answer: str = Field(max_length=16000)
    value: float | None = None


Status = Literal[
    "queued", "running", "awaiting_approval", "paused_budget", "completed", "failed", "cancelled"
]


class Approval(Contract):
    preview: ApprovalPreview | None = None
    id: str
    tool: str
    arguments: dict[str, Any]
    origin_run_id: str | None = None


class Run(Contract):
    id: str
    session_id: str | None
    agent_id: str
    config: AgentConfig
    status: Status
    outcome: TaskOutcome | None = Field(
        default=None, description="Task result, separate from workflow lifecycle status."
    )
    outcome_reason: OutcomeReason | None = None
    output: Answer | None = None
    error: str | None = None
    approvals: list[Approval] = Field(default_factory=list)
    usage: dict[str, int] = Field(default_factory=dict)
    created_at: datetime
    root_run_id: str | None = None
    parent_run_id: str | None = None
    specialist: str | None = None
    depth: int = 0
    cleanup_state: Literal["pending", "complete"] = "complete"
    artifact_ids: list[str] = Field(default_factory=list)
    artifacts: list[ArtifactRef] = Field(default_factory=list)
    tool_registration_ids: dict[str, str] = Field(default_factory=dict)
    task_result: TaskResult | None = None
    execution_version: int | None = None
    task_state: TaskState | None = None
    completion_assessment: CompletionAssessment | None = None
    workspace: dict | None = None
    stop_reason: str | None = None
    assignment: dict | None = None
    effective_grants: dict | None = None
    cleanup_detail: dict | None = None


class Decision(Contract):
    approved: bool


class Event(Contract):
    id: int
    run_id: str
    type: str
    data: dict[str, Any]
    created_at: datetime
