"""Small task outcomes and factual progress for the convenience client."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .schemas import Approval, Run
from .task_outcomes import OutcomeReason, TaskOutcome, classify
from .tool_contracts import ArtifactRef


class RunProgress(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    stage: Literal[
        "uploading",
        "submitted",
        "resuming",
        "queued",
        "running",
        "awaiting_approval",
        "paused_budget",
        "cleaning_up",
        "completed",
        "failed",
        "cancelled",
    ]
    message: str
    outcome: TaskOutcome | None = None
    outcome_reason: OutcomeReason | None = None
    run_id: str | None = None
    idempotency_key: str | None = None


class RunResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: str
    session_id: str | None
    status: Literal["completed", "awaiting_approval", "paused_budget", "failed", "cancelled"]
    outcome: TaskOutcome
    outcome_reason: OutcomeReason | None = None
    answer: str | None = None
    value: float | None = None
    files: list[ArtifactRef] = Field(default_factory=list)
    workspace: dict | None = None
    message: str
    next_action: str | None = None
    approvals: list[Approval] = Field(default_factory=list)
    resources: dict | None = None
    idempotency_key: str | None = None
    details: Run = Field(exclude=True, repr=False)


MESSAGES = {
    "queued": "Task received; waiting for a worker.",
    "resuming": "Decisions recorded; waiting for the worker to resume.",
    "running": "The agent is working on your task.",
    "awaiting_approval": "Your approval is needed before the proposed action can proceed.",
    "paused_budget": "The task is paused at a resource limit.",
    "cleaning_up": "Execution has stopped; cleanup is still in progress.",
    "completed": "Execution finished.",
    "failed": "The task could not be completed.",
    "cancelled": "Task cancelled; cleanup is complete.",
}

FAILURES = {
    "output_limit": "The response could not fit within the permitted output size.",
    "context_limit": "The task exceeded its permitted context size; use smaller source sections.",
    "budget_exhausted": "The task reached its configured resource limit.",
    "provider_not_configured": "The selected provider is not configured on the worker.",
    "run_timeout": "The task reached its execution time limit.",
    "resource_pause_timeout": "The task stopped because its resource pause expired.",
    "approval_timeout": "The task stopped because the approval waiting period expired.",
    "tool_denied": "The task could not proceed after a tool action was denied.",
    "budget_exceeded": "The task reached its configured execution limit.",
    "sandbox_unavailable": "The sandbox service was unavailable.",
}


def outcome(run, resources=None, idempotency_key=None):
    state, reason = (
        (run.outcome, run.outcome_reason)
        if run.outcome
        else classify(run.status, error=run.error, tracked=False, pending=bool(run.approvals))
    )
    message = MESSAGES[run.status]
    next_action = None
    if run.status == "awaiting_approval":
        if len(run.approvals) == 1 and run.approvals[0].preview:
            preview = run.approvals[0].preview
            message = preview.title + ": " + "; ".join(f"{f.label}: {f.value}" for f in preview.facts)
        next_action = (
            "Review each proposed tool and its exact arguments, then approve or deny its approval ID."
        )
    elif run.status == "paused_budget":
        block = ((resources or {}).get("pause") or {}).get("block") or {}
        if block.get("limit_type") == "ceiling":
            message = (
                "The task reached an operator or model ceiling. A task budget increase cannot bypass it."
            )
            next_action = "Inspect the resource details; cancel the task or contact the operator."
        else:
            next_action = (
                "Inspect the resource details; explicitly increase the allowed budget or cancel the task."
            )
    elif run.status == "failed":
        message = FAILURES.get(run.error, message)
        next_action = (
            "Inspect this run's error and effect receipts before starting another task; "
            "completed external actions may already have taken effect."
        )
    if state == "succeeded":
        message = "Task succeeded."
    elif state == "blocked":
        message = {
            "approval_denied": "Task blocked: a tool action was denied.",
            "artifact_not_authorized": "Task blocked: a tool tried to access a file not attached to this run.",
            "task_blocked": "The agent stopped because it could not complete the task.",
        }.get(reason, "The task was blocked.")
        if reason == "task_blocked" and run.task_state and run.task_state.blockers:
            message += " " + " ".join(run.task_state.blockers)[:1000]
        next_action = (
            "Review the result and effect receipts. This run has finished; "
            "start a new task with the necessary inputs or permissions if you want to continue."
        )
    elif state == "needs_attention" and run.status == "completed":
        message = {
            "tool_error": "Execution finished with tool errors; task success needs review.",
            "child_failed": "A specialist did not complete its work; task success needs review.",
            "outcome_unavailable": "This earlier run has no recorded task outcome; review its result.",
        }.get(reason, "Execution finished; the task needs review.")
        next_action = "Review the answer, files and effect receipts before relying on this result."
    return RunResult(
        outcome=state,
        outcome_reason=reason,
        run_id=run.id,
        session_id=run.session_id,
        status=run.status,
        answer=run.output.answer if run.output else None,
        value=run.output.value if run.output else None,
        files=run.artifacts,
        workspace=run.workspace,
        message=message,
        next_action=next_action,
        approvals=run.approvals,
        resources=resources,
        idempotency_key=idempotency_key,
        details=run,
    )
