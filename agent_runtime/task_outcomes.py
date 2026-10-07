"""Owned task outcomes derived from durable execution facts, never answer text."""

from typing import Literal

TaskOutcome = Literal["in_progress", "succeeded", "blocked", "needs_attention", "failed", "cancelled"]
OutcomeReason = Literal[
    "approval_required",
    "resource_limit",
    "approval_denied",
    "artifact_not_authorized",
    "task_blocked",
    "tool_error",
    "child_failed",
    "outcome_unavailable",
    "execution_failed",
]


def classify(status, *, error=None, denied=False, issues=(), accepted=None, tracked=True, pending=True):
    if status == "awaiting_approval":
        return ("needs_attention", "approval_required") if pending else ("in_progress", None)
    if status == "paused_budget":
        return "needs_attention", "resource_limit"
    if status in {"queued", "running"}:
        return "in_progress", None
    if status == "cancelled":
        return "cancelled", None
    # A completion assessment cannot override the user's recorded refusal.
    if denied or error == "tool_denied":
        return "blocked", "approval_denied"
    if error == "artifact_not_authorized" or "artifact_not_authorized" in issues:
        return "blocked", "artifact_not_authorized"
    if error == "task_blocked":
        return "blocked", "task_blocked"
    if status == "failed":
        return "failed", "execution_failed"
    if "child_failed" in issues:
        return "needs_attention", "child_failed"
    if issues:
        return "needs_attention", "tool_error"
    if status == "completed" and accepted is True:
        return "succeeded", None
    if not tracked or accepted is False:
        return "needs_attention", "outcome_unavailable"
    return "succeeded", None
