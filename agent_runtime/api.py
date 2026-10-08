import asyncio
import hmac
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .config import settings
from .db import Database
from .schemas import Agent, AgentConfig, Decision, Run, RunCreate, Session
from .store import TERMINAL, Problem, Store


def create_app(store: Store | None = None, api_key: str | None = None):
    owned = store is None
    store = store or Store(
        Database(settings().database_url, settings().database_schema), settings().max_active_runs
    )
    expected = api_key if api_key is not None else settings().api_key.get_secret_value()

    @asynccontextmanager
    async def lifespan(app):
        if not expected:
            raise RuntimeError("API_KEY must be configured")
        yield
        if owned:
            await store.database.close()

    bearer = HTTPBearer(
        scheme_name="RunweaveAPIKey",
        auto_error=False,
        description="Enter the application API_KEY. Provider credentials stay on the worker.",
    )

    async def authenticate(
        request: Request, credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]
    ):
        authorization = request.headers.get("authorization")
        supplied = (authorization or "").removeprefix("Bearer ")
        if (
            not expected
            or not authorization
            or not authorization.startswith("Bearer ")
            or not hmac.compare_digest(supplied.encode(), expected.encode())
        ):
            raise Problem(401, "Invalid API key")

    app = FastAPI(
        title="Runweave API",
        description=(
            "Configure an agent once, then give it a task and receive the result. "
            "Use **Authorize** with your application API key. Start with **Setup → Configure an agent once**, "
            "then **Tasks → Run a task**. The Python client also provides `await client.run(...)` "
            "for uploads, submission and waiting in one call. "
            "Approvals and resource increases remain explicit. Files, controls and inspection are available when needed. "
            "This is a single-workspace development build; general production reliability is not established."
        ),
        version="1.0.0",
        lifespan=lifespan,
        dependencies=[Depends(authenticate)],
    )

    @app.exception_handler(Problem)
    async def problem_handler(request, exc):
        return JSONResponse(
            status_code=exc.status,
            content={"detail": exc.detail},
            headers={"WWW-Authenticate": "Bearer"} if exc.status == 401 else {},
        )

    from .artifacts import MEDIA
    from .tool_contracts import ArtifactRef, RunBudget, ToolDescriptor

    @app.get("/v1/tools", response_model=list[ToolDescriptor])
    async def tools():
        return [store.tools.descriptor(t) for t in store.tools.entries.values()]

    @app.get("/v1/tools/{alias}", response_model=ToolDescriptor)
    async def tool(alias: str):
        if alias not in store.tools.entries:
            raise Problem(404, "Tool not found")
        return store.tools.descriptor(store.tools.entries[alias])

    @app.post(
        "/v1/artifacts",
        response_model=ArtifactRef,
        status_code=201,
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    media: {"schema": {"type": "string", "format": "binary"}} for media in sorted(MEDIA)
                },
            }
        },
    )
    async def upload(
        request: Request,
        idempotency_key: Annotated[str, Header(min_length=1, max_length=128)],
        content_type: Annotated[str, Header()],
        x_filename: Annotated[str, Header()] = "input.txt",
    ):
        return await store.upload_stream(
            request.stream(), content_type.split(";")[0].strip(), x_filename, "upload:" + idempotency_key
        )

    @app.get("/v1/artifacts", response_model=list[ArtifactRef])
    async def artifacts():
        return await store.artifact_list()

    @app.get("/v1/artifacts/{artifact_id}", response_model=ArtifactRef)
    async def artifact(artifact_id: str):
        return (await store.artifact(artifact_id))[0]

    @app.get("/v1/artifacts/{artifact_id}/content")
    async def content(artifact_id: str):
        ref = await store.artifact_ref(artifact_id)
        # Verify the complete source before sending headers. A missing/corrupt blob
        # is a clean HTTP error, never a successful download with unverified bytes.
        async for _ in store.artifact_chunks(artifact_id):
            pass
        return StreamingResponse(
            store.artifact_chunks(artifact_id),
            media_type=ref.media_type,
            headers={
                "ETag": '"' + ref.sha256 + '"',
                "Content-Disposition": 'attachment; filename="' + ref.filename + '"',
                "X-Content-Type-Options": "nosniff",
                "Content-Length": str(ref.size_bytes),
            },
        )

    @app.get("/v1/runs/{run_id}/children", response_model=list[Run])
    async def children(run_id: str):
        return await store.children(run_id)

    @app.get("/v1/runs/{run_id}/budget", response_model=RunBudget, response_model_exclude_unset=True)
    async def budget(run_id: str):
        if not await store.toolkit(run_id):
            raise Problem(404, "Toolkit run not found")
        return await store.budget(run_id)

    @app.get("/v1/runs/{run_id}/artifacts", response_model=list[ArtifactRef])
    async def run_artifacts(run_id: str):
        if not await store.toolkit(run_id):
            raise Problem(404, "Toolkit run not found")
        return await store.artifact_list(run_id)

    @app.get("/v1/runs/{run_id}/effects")
    async def effects(run_id: str):
        from sqlalchemy import select

        from .db import ArtifactRow, NoteRow, ToolkitRunRow
        from .tool_registry import digest

        general = await store.general(run_id)
        if general:
            from .general_db import GeneralOperationRow

            ids = [run_id] + list(general["children"])
            async with store.database.sessions() as db:
                rows = list(
                    await db.scalars(select(GeneralOperationRow).where(GeneralOperationRow.run_id.in_(ids)))
                )
                return [
                    {
                        "operation_id": r.id,
                        "origin_run_id": r.run_id,
                        "result": r.data.get("result"),
                        "classification": r.data["status"],
                        "argument_digest": r.data.get("argument_digest"),
                        "registration_id": r.data.get("registration_id"),
                        "effect_policy": r.data.get("effect_policy"),
                    }
                    for r in rows
                    if (r.data.get("result") or {}).get("effect")
                    or (r.data.get("external") and r.data.get("status") == "outcome_unknown")
                ]
        ids = [run_id] + [c.id for c in await store.children(run_id)]
        async with store.database.sessions() as db:
            notes = list(await db.scalars(select(NoteRow).where(NoteRow.run_id.in_(ids))))
            outputs = list(await db.scalars(select(ArtifactRow).where(ArtifactRow.producer_run_id.in_(ids))))
            note_receipts = []
            for note in notes:
                feature = await db.get(ToolkitRunRow, note.run_id)
                entry = feature.state["tools"].get("record_note") if feature else None
                note_receipts.append(
                    {
                        "operation_id": note.operation_id,
                        "origin_run_id": note.run_id,
                        "tool": "record_note",
                        "argument_digest": digest({"text": note.text}),
                        "tool_registration_id": digest(entry) if entry else None,
                    }
                )
            return note_receipts + [
                {"artifact_id": a.id, "origin_run_id": a.producer_run_id, "sha256": a.sha256} for a in outputs
            ]

    @app.get("/v1/models")
    async def models():
        return [
            {
                "provider": r.provider,
                "model": r.model,
                "tool_calling": r.tool_calling,
                "max_output_tokens": r.max_output_tokens,
            }
            for r in store.registry.entries.values()
        ]

    @app.get("/v1/readiness")
    async def readiness():
        from sqlalchemy import select
        from temporalio.api.enums.v1 import TaskQueueType
        from temporalio.api.taskqueue.v1 import TaskQueue
        from temporalio.api.workflowservice.v1 import DescribeTaskQueueRequest
        from temporalio.client import Client

        from .db import GateRow

        checks = {
            "database": "unavailable",
            "temporal": "unavailable",
            "worker": "unverified",
            "provider": "unverified",
            "mcp": "unverified",
            "sandbox": "unverified",
        }
        try:
            async with asyncio.timeout(3):
                async with store.database.sessions() as db:
                    if await db.scalar(select(GateRow.id).where(GateRow.id == 1)) == 1:
                        checks["database"] = "ready"
        except Exception:
            pass
        try:
            async with asyncio.timeout(3):
                temporal = await Client.connect(
                    settings().temporal_address, namespace=settings().temporal_namespace
                )
                result = await temporal.workflow_service.describe_task_queue(
                    DescribeTaskQueueRequest(
                        namespace=settings().temporal_namespace,
                        task_queue=TaskQueue(name=settings().task_queue),
                        task_queue_type=TaskQueueType.TASK_QUEUE_TYPE_WORKFLOW,
                    )
                )
                checks["temporal"] = "reachable"
                checks["worker"] = "poller_observed" if result.pollers else "no_poller_observed"
        except Exception:
            pass
        return {
            "ready": checks["database"] == "ready"
            and checks["temporal"] == "reachable"
            and checks["worker"] == "poller_observed",
            "checks": checks,
            "note": "Poller observation is not an execution or provider-access probe.",
        }

    @app.post("/v1/agents", response_model=Agent, status_code=201)
    async def create_agent(config: AgentConfig):
        return await store.agent(config)

    @app.get("/v1/agents/{agent_id}", response_model=Agent)
    async def get_agent(agent_id: str):
        return await store.get_agent(agent_id)

    @app.put("/v1/agents/{agent_id}", response_model=Agent)
    async def update_agent(agent_id: str, config: AgentConfig):
        return await store.agent(config, agent_id)

    @app.post("/v1/sessions", response_model=Session, status_code=201)
    async def create_session():
        return await store.session()

    @app.post("/v1/runs", response_model=Run, status_code=202)
    async def create_run(
        body: RunCreate, idempotency_key: Annotated[str, Header(min_length=1, max_length=128)]
    ):
        submitted = await store.submit(body, idempotency_key)
        return await store.get(submitted.id)

    @app.get("/v1/runs/{run_id}", response_model=Run)
    async def get_run(run_id: str):
        return await store.get(run_id)

    @app.post("/v1/runs/{run_id}/cancel", response_model=Run)
    async def cancel_run(run_id: str):
        cancelled = await store.cancel(run_id)
        return await store.get(cancelled.id)

    @app.post("/v1/runs/{run_id}/approvals/{approval_id}", response_model=Run)
    async def decide(run_id: str, approval_id: str, decision: Decision):
        decided = await store.decide(run_id, approval_id, decision.approved)
        return await store.get(decided.id)

    @app.get("/v1/runs/{run_id}/events")
    async def stream(
        run_id: str,
        request: Request,
        cursor: Annotated[int, Query(ge=0)] = 0,
        last_event_id: Annotated[str | None, Header()] = None,
    ):
        if last_event_id is not None:
            try:
                cursor = int(last_event_id)
                if cursor < 0:
                    raise ValueError
            except ValueError:
                raise Problem(400, "Invalid Last-Event-ID") from None
        await store.get(run_id)

        async def generate():
            nonlocal cursor
            while not await request.is_disconnected():
                # Read status before events to avoid missing a terminal event committed between reads.
                run = await store.get(run_id)
                events = await store.events(run_id, cursor)
                for event in events:
                    yield f"id: {event.id}\nevent: {event.type}\ndata: {event.model_dump_json()}\n\n"
                    cursor = event.id
                if run.status in TERMINAL and run.cleanup_state == "complete" and len(events) < 100:
                    return
                if not events:
                    yield ": keepalive\n\n"
                    await asyncio.sleep(0.5)

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    from .general_api import routes

    routes(app, store)
    from .api_docs import configure_docs

    configure_docs(app)
    return app


app = create_app()
