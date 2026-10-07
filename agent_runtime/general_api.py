"""Authenticated v3 routes, mounted under the existing API's dependencies."""

from typing import Annotated

from fastapi import Header, Query, Request
from fastapi.responses import Response
from pydantic import ValidationError
from sqlalchemy import select

from .evidence import AcceptanceBundle, build_evidence_bundle
from .general_contracts import (
    CapabilityDescriptor,
    CapabilityPage,
    OperationPage,
    ProjectManifest,
    ReconciliationRequest,
    ResourceUpdate,
    TaskSnapshot,
    VerificationPage,
    WorkspaceAttachment,
    WorkspaceCreate,
)
from .general_db import GeneralOperationRow, ProjectBlobRow
from .project_store import digest, fail


def routes(app, store):
    from .computer_contracts import ComputerCleanupAttestation, ComputerSession

    @app.get("/v1/sessions/{session_id}/computers", response_model=list[ComputerSession])
    async def computers(session_id: str, cursor: str = "", limit: Annotated[int, Query(ge=1, le=100)] = 100):
        return await store.computer_sessions(session_id, cursor=cursor, limit=limit)

    @app.get("/v1/computers/{computer_id}", response_model=ComputerSession)
    async def computer(computer_id: str):
        return await store.computer(computer_id)

    @app.post("/v1/computers/{computer_id}/close", response_model=ComputerSession, status_code=202)
    async def close_computer(computer_id: str):
        return await store.computer_close(computer_id)

    @app.post("/v1/computers/{computer_id}/cleanup-attestation", response_model=ComputerSession)
    async def attest_computer(
        computer_id: str,
        body: ComputerCleanupAttestation,
        idempotency_key: Annotated[str, Header(min_length=1, max_length=128)],
    ):
        return await store.computer_attest_cleanup(computer_id, body.evidence_ref, idempotency_key)

    original_openapi = app.openapi

    def openapi():
        schema = original_openapi()
        definitions = WorkspaceCreate.model_json_schema(ref_template="#/components/schemas/{model}").get(
            "$defs", {}
        )
        schema.setdefault("components", {}).setdefault("schemas", {}).update(definitions)
        return schema

    app.openapi = openapi

    @app.get("/v1/extensions/status")
    async def extension_status():
        from .extension_health import extension_status

        return await extension_status(store)

    @app.get("/v1/capabilities", response_model=list[CapabilityDescriptor])
    async def installed_capabilities():
        from .general_catalog import CATALOG, descriptor

        entries = store.extensions.snapshots([*CATALOG, *store.extensions.tools])
        return [descriptor(entry) for entry in entries.values()]

    @app.get("/v1/runs/{run_id}/resources")
    async def resources(run_id: str):
        return await store.resources(run_id)

    @app.put("/v1/runs/{run_id}/resources")
    async def update_resources(
        run_id: str,
        body: ResourceUpdate,
        idempotency_key: Annotated[str, Header(min_length=1, max_length=128)],
    ):
        return await store.update_resources(run_id, body, idempotency_key)

    @app.get("/v1/runs/{run_id}/recovery")
    async def recovery(
        run_id: str, cursor: Annotated[int, Query(ge=0)] = 0, limit: Annotated[int, Query(ge=1, le=100)] = 100
    ):
        return await store.recovery(run_id, cursor, limit)

    @app.get("/v1/runs/{run_id}/reconciliations/{identity}")
    async def reconciliation(run_id: str, identity: str):
        return await store.reconciliation_get(run_id, identity)

    @app.post("/v1/runs/{run_id}/reconciliations", status_code=202)
    async def reconcile(
        run_id: str,
        body: ReconciliationRequest,
        idempotency_key: Annotated[str, Header(min_length=1, max_length=128)],
    ):
        return await store.reconciliation_submit(run_id, body, idempotency_key)

    @app.get("/v1/skills")
    async def installed_skills():
        return [
            {k: entry[k] for k in ("alias", "description", "version", "registration_id")}
            for entry in store.extensions.skills.values()
        ]

    @app.post(
        "/v1/workspaces",
        status_code=201,
        response_model=WorkspaceAttachment,
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": WorkspaceCreate.model_json_schema(
                            ref_template="#/components/schemas/{model}"
                        )
                    }
                },
            }
        },
    )
    async def create_workspace(
        request: Request, idempotency_key: Annotated[str, Header(min_length=1, max_length=128)]
    ):
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 6291456:
                fail("workspace_request_limit", 413)
        try:
            body = WorkspaceCreate.model_validate_json(raw)
        except ValidationError:
            fail("invalid_workspace_request")
        return await store.workspace_create(body, idempotency_key)

    @app.get("/v1/workspaces/{workspace_id}", response_model=ProjectManifest)
    async def workspace(workspace_id: str):
        return await store.workspace_get(workspace_id)

    @app.get("/v1/workspaces/{workspace_id}/revisions/{revision_id}", response_model=ProjectManifest)
    async def revision(workspace_id: str, revision_id: str):
        return await store.workspace_get(workspace_id, revision_id)

    def binary(data, media="application/octet-stream"):
        return Response(
            data,
            media_type=media,
            headers={
                "ETag": '"' + digest(data) + '"',
                "X-SHA256": digest(data),
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.get("/v1/workspaces/{workspace_id}/revisions/{revision_id}/files")
    async def file(workspace_id: str, revision_id: str, path: str):
        return binary(await store.workspace_get(workspace_id, revision_id, file=path))

    @app.get("/v1/workspaces/{workspace_id}/revisions/{revision_id}/download")
    async def download(workspace_id: str, revision_id: str):
        return binary(await store.workspace_get(workspace_id, revision_id, archive=True), "application/zip")

    @app.get("/v1/runs/{run_id}/evidence", response_model=AcceptanceBundle)
    async def evidence(run_id: str, response: Response):
        bundle = await build_evidence_bundle(store, run_id)
        response.headers["ETag"] = '"' + bundle.sha256 + '"'
        response.headers["Cache-Control"] = "no-store"
        return bundle

    @app.get("/v1/runs/{run_id}/task", response_model=TaskSnapshot)
    async def task(run_id: str):
        gr = await store.general(run_id)
        if gr is None:
            fail("General run not found", 404)
        return {"goal": gr["goal"], "state": gr["task_state"], "assessment": gr.get("completion_assessment")}

    @app.get("/v1/runs/{run_id}/capabilities", response_model=CapabilityPage)
    async def capabilities(
        run_id: str,
        query: Annotated[str, Query(max_length=200)] = "",
        cursor: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=16)] = 16,
    ):
        return await store.general_capabilities(run_id, query, cursor, limit)

    @app.get("/v1/runs/{run_id}/verifications", response_model=VerificationPage)
    async def verifications(
        run_id: str, cursor: Annotated[int, Query(ge=0)] = 0, limit: Annotated[int, Query(ge=1, le=16)] = 16
    ):
        return await store.general_records(run_id, "verification", cursor, limit)

    @app.get("/v1/runs/{run_id}/checkpoints")
    async def checkpoints(
        run_id: str, cursor: Annotated[int, Query(ge=0)] = 0, limit: Annotated[int, Query(ge=1, le=16)] = 16
    ):
        return await store.general_records(run_id, "checkpoint", cursor, limit)

    @app.get("/v1/runs/{run_id}/operations", response_model=OperationPage)
    async def operations(
        run_id: str, cursor: Annotated[int, Query(ge=0)] = 0, limit: Annotated[int, Query(ge=1, le=16)] = 16
    ):
        if not await store.general(run_id):
            fail("General run not found", 404)
        async with store.database.sessions() as db:
            rows = list(
                await db.scalars(
                    select(GeneralOperationRow)
                    .where(
                        GeneralOperationRow.run_id == run_id,
                        GeneralOperationRow.data["sequence"].as_integer() >= cursor,
                    )
                    .order_by(GeneralOperationRow.data["sequence"].as_integer())
                    .limit(limit + 1)
                )
            )
            return {
                "items": [
                    {
                        "id": r.id,
                        "status": r.data.get("status", "pending"),
                        "result": r.data.get("result"),
                        "diagnostics": {
                            k: r.data[k]
                            for k in (
                                "validation_feedback",
                                "output_calls",
                                "failure_type",
                                "validation_errors",
                                "response_shape",
                                "message_bytes",
                                "output_reservation",
                                "required_reservation",
                                "context_bytes",
                                "schema_bytes",
                            )
                            if k in r.data
                        },
                        "outputs": [
                            {"name": n, "sha256": v["sha256"], "size_bytes": v["size_bytes"]}
                            for n, v in r.data.get("outputs", {}).items()
                        ],
                    }
                    for r in rows[:limit]
                ],
                "next_cursor": rows[limit].data["sequence"] if len(rows) > limit else None,
            }

    @app.get("/v1/runs/{run_id}/operations/{operation_id}/outputs/{name:path}")
    async def output(run_id: str, operation_id: str, name: str):
        async with store.database.sessions() as db:
            op = await db.get(GeneralOperationRow, operation_id)
            if op is None or op.run_id != run_id or name not in op.data.get("outputs", {}):
                fail("Output not found", 404)
            item = op.data["outputs"][name]
            blob = await db.get(ProjectBlobRow, item["sha256"])
            if blob is None:
                fail("output_integrity", 409)
            data = blob.content
            if digest(data) != item["sha256"] or len(data) != item["size_bytes"]:
                fail("output_integrity", 409)
            return binary(data)
