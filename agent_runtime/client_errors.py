"""Actionable client errors built only from allowlisted public information."""

from httpx import ResponseNotRead

from . import general_contracts, schemas


class ClientError(RuntimeError):
    """Safe to display; raw response bodies, values and transport errors stay private."""

    def __init__(self, message, *, code=None, status_code=None, fields=(), run_id=None, idempotency_key=None):
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.fields = tuple(fields)
        self.run_id = run_id
        self.idempotency_key = idempotency_key


# Keys must match complete server messages. Never interpolate arbitrary response text.
PUBLIC_ERRORS = {
    "Invalid API key": ("invalid_api_key", "Check API_KEY; use the application key, not a provider key."),
    "Agent not found": ("agent_not_found", "Check agent_id or configure an agent first."),
    "Run not found": ("run_not_found", "Check run_id; use the ID returned when the task was submitted."),
    "Session not found": ("session_not_found", "Check session_id or omit it to start a new conversation."),
    "Idempotency key reused with different input": (
        "idempotency_conflict",
        "This key belongs to a different request. Retry the original input, or use a new key for a new task.",
    ),
    "Session already has an active run": (
        "session_busy",
        "Wait for or cancel the active task before submitting another turn to this session.",
    ),
    "V3 inputs require a general agent": (
        "general_agent_required",
        "Configure the agent with general={} to use task contracts or workspaces.",
    ),
    "Unsupported provider/model combination": (
        "unsupported_model",
        "Choose a provider/model pair returned by client.models().",
    ),
    "Requested output tokens exceed registration limit": (
        "output_token_limit",
        "Lower max_tokens to the selected model's registered output limit.",
    ),
    "Approval decision is immutable": (
        "approval_decided",
        "This approval already has a decision. Retrieve the run to see its current state.",
    ),
    "Approval is not pending": (
        "approval_not_pending",
        "Retrieve the run and select an approval that is still pending.",
    ),
    "approval_requires_root": (
        "approval_requires_root",
        "Submit this decision using the root run ID and the returned approval ID.",
    ),
    "artifact_size_limit": ("artifact_size_limit", "Each uploaded file must be at most 262144 bytes."),
    "unsupported_media_type": (
        "unsupported_media_type",
        "Use UTF-8 text, Markdown, CSV, JSON, a text diff, or a bounded repository ZIP.",
    ),
    "invalid_filename": (
        "invalid_filename",
        "Use a filename of up to 100 ASCII letters, numbers, periods, underscores or hyphens.",
    ),
    "invalid_artifact_content": (
        "invalid_artifact_content",
        "Check the file encoding and content; text files must be valid UTF-8.",
    ),
}

SAFE_FIELDS = {"authorization", "idempotency-key", "content-type", "x-filename", "cursor", "limit"}
for module in (schemas, general_contracts):
    for value in vars(module).values():
        SAFE_FIELDS.update(getattr(value, "model_fields", {}))


def validation_fields(details):
    fields = []
    for item in details[:8]:
        if not isinstance(item, dict) or not isinstance(item.get("loc"), (list, tuple)):
            continue
        parts = []
        for part in item["loc"][:8]:
            if isinstance(part, str) and part in {"body", "query", "path", "header"}:
                continue
            if isinstance(part, str) and part in SAFE_FIELDS:
                parts.append(part)
            elif type(part) is int and 0 <= part <= 999:
                parts.append(str(part))
            else:
                parts.append("[field]")
                break
        if parts and (field := ".".join(parts)) not in fields:
            fields.append(field)
    return fields


def check(response):
    if not response.is_error:
        return
    status = response.status_code
    hints = {
        401: "Check API_KEY; use the application key, not a provider key.",
        403: "This action is not permitted by the configured policy.",
        404: "Check the resource ID.",
        409: "Check the idempotency key, session activity or prior approval decision.",
        413: "Reduce the request or file size to the documented limit.",
        422: "Check input fields and registered model limits.",
        429: "Wait for active runs to finish, then retry with the same idempotency key.",
    }
    code, hint, fields = f"http_{status}", hints.get(status, "Check service readiness."), []
    try:
        body = response.json() if len(response.content) <= 65536 else None
        detail = body.get("detail") if isinstance(body, dict) else None
        if isinstance(detail, str) and detail in PUBLIC_ERRORS:
            code, hint = PUBLIC_ERRORS[detail]
        elif status == 422 and isinstance(detail, list):
            fields = validation_fields(detail)
            code = "validation_error"
            if fields:
                hint = "Check these fields: " + ", ".join(fields) + ". Values must match the request schema."
    except (ValueError, TypeError, ResponseNotRead):
        pass
    raise ClientError(f"HTTP {status}. {hint}", code=code, status_code=status, fields=fields)
