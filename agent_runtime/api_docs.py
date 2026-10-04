"""Progressive documentation for the existing public API; no execution behavior."""

from fastapi.routing import APIRoute

TAGS = [
    {"name": "Tasks", "description": "Everyday use: submit a task, get its result, or follow progress."},
    {"name": "Setup", "description": "Configure a reusable agent and discover installed models and tools."},
    {
        "name": "Files",
        "description": "Attach inputs and retrieve results. Workspace operations are optional.",
    },
    {"name": "Controls", "description": "Explicit approvals, cancellation and resource decisions."},
    {"name": "Inspection", "description": "Optional evidence, diagnostics and operator recovery."},
]

DESCRIPTIONS = {
    ("POST", "/v1/runs"): (
        "Tasks",
        "Run a task",
        "Submit input to an existing agent. Returns a durable run ID immediately. "
        "Reuse Idempotency-Key with identical input to safely retry. Get the run to retrieve its result, "
        "or use the Python client's run() method to upload files and wait in one call. "
        "Approval and budget pauses require an explicit caller decision.",
    ),
    ("GET", "/v1/runs/{run_id}"): (
        "Tasks",
        "Get progress or result",
        "Read status for execution and outcome for task success. completed, failed and cancelled are terminal; "
        "awaiting_approval and paused_budget describe decisions the caller must handle. "
        "A completed run may be blocked or need attention: inspect outcome and outcome_reason before using its answer. "
        "cleanup_state reports whether cleanup has finished.",
    ),
    ("GET", "/v1/runs/{run_id}/events"): (
        "Tasks",
        "Follow task progress",
        "Stream durable lifecycle and tool events using SSE. Reconnect with Last-Event-ID to resume. "
        "These are progress events, not model token deltas. Disconnecting does not cancel the task.",
    ),
    ("POST", "/v1/agents"): (
        "Setup",
        "Configure an agent once",
        "Choose an installed provider/model, instructions and authorized tools. Set general={} for the "
        "general task runtime. Keep and reuse the returned agent ID. fake/deterministic runs scripted "
        "test tasks only; choose a registered live model for natural-language tasks.",
    ),
    ("GET", "/v1/agents/{agent_id}"): (
        "Setup",
        "Read agent configuration",
        "Retrieve a reusable agent by its ID.",
    ),
    ("PUT", "/v1/agents/{agent_id}"): (
        "Setup",
        "Update agent configuration",
        "Update configuration for future tasks. Existing runs keep their pinned configuration.",
    ),
    ("POST", "/v1/sessions"): (
        "Setup",
        "Start a conversation",
        "Optionally create a session in advance. Task submission creates one when session_id is omitted.",
    ),
    ("GET", "/v1/models"): (
        "Setup",
        "Discover installed models",
        "List registered provider/model pairs and limits. Listing a model does not probe provider connectivity.",
    ),
    ("GET", "/v1/readiness"): (
        "Setup",
        "Check service readiness",
        "Check the database, Temporal connection and worker poller. This does not make a model call or probe tools.",
    ),
    ("POST", "/v1/runs/{run_id}/cancel"): (
        "Controls",
        "Cancel a task",
        "Request cancellation of the task tree. Already committed effects remain; follow cleanup_state until complete.",
    ),
    ("POST", "/v1/runs/{run_id}/approvals/{approval_id}"): (
        "Controls",
        "Approve or deny a proposed action",
        "Review the exact tool and arguments returned in approvals before deciding. Use the root run for child approvals. Decisions are immutable.",
    ),
    ("GET", "/v1/runs/{run_id}/resources"): (
        "Controls",
        "Understand a resource pause",
        "Inspect usage, remaining capacity and the reason for a pause. Task budgets and operator/model ceilings are distinct.",
    ),
    ("PUT", "/v1/runs/{run_id}/resources"): (
        "Controls",
        "Explicitly increase a task budget",
        "Supply expected_version, approved limit increases and an Idempotency-Key. Increases cannot bypass pinned operator or model ceilings.",
    ),
    ("GET", "/v1/runs/{run_id}/evidence"): (
        "Inspection",
        "Export accepted evidence",
        "Download a bounded evidence bundle for a completed, accepted general run. The offline verifier checks internal consistency and deterministic assertions. Bundles are unsigned.",
    ),
}


def configure_docs(app):
    app.openapi_tags = TAGS
    for route in app.routes:
        if not isinstance(route, APIRoute) or not route.path.startswith("/v1/"):
            continue
        entry = next(
            (
                DESCRIPTIONS[(method, route.path)]
                for method in route.methods
                if (method, route.path) in DESCRIPTIONS
            ),
            None,
        )
        if entry:
            tag, route.summary, route.description = entry
        else:
            if (
                route.path.startswith(("/v1/artifacts", "/v1/workspaces"))
                or route.path.endswith("/artifacts")
                or "/outputs/" in route.path
            ):
                tag = "Files"
            elif route.path.startswith(("/v1/tools", "/v1/capabilities", "/v1/skills")):
                tag = "Setup"
            else:
                tag = "Inspection"
            route.summary = route.name.replace("_", " ").capitalize()
            route.description = {
                "Files": "Explicitly attach or retrieve files through the authenticated workspace API. Existing grants and file limits apply.",
                "Setup": "Discover installed capabilities. Discovery does not grant permission or confirm external service availability.",
                "Inspection": "Inspect a task's durable state or perform explicit operator recovery. Ordinary task submission does not require this operation.",
            }[tag]
        route.tags = [tag]
        examples = {
            "/v1/agents": {
                "name": "demo",
                "provider": "fake",
                "model": "deterministic",
                "tools": [],
                "general": {},
            },
            "/v1/runs": {
                "agent_id": "REPLACE_WITH_YOUR_AGENT_ID",
                "input": "Run the scripted completion demonstration.",
            },
        }
        if "POST" in route.methods and route.path in examples:
            route.openapi_extra = {
                **(route.openapi_extra or {}),
                "requestBody": {
                    "content": {
                        "application/json": {
                            "examples": {
                                "quickstart": {
                                    "summary": "Minimal scripted quickstart",
                                    "value": examples[route.path],
                                }
                            }
                        }
                    }
                },
            }
