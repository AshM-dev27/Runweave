"""Small asynchronous client using only the versioned public HTTP contracts."""

import asyncio
from uuid import uuid4

import httpx

from .schemas import Agent, AgentConfig, Event, Run, RunCreate, Session

TERMINAL = {"completed", "failed", "cancelled"}


class ClientError(RuntimeError):
    """Safe to display: response bodies and transport exceptions are never included."""

    def __init__(self, message, *, idempotency_key=None):
        super().__init__(message)
        self.idempotency_key = idempotency_key


def check(response):
    if response.is_error:
        hints = {
            401: "Check API_KEY.",
            404: "Check the resource ID.",
            409: "Check the idempotency key, session activity or prior approval decision.",
            422: "Check input fields and registered model limits.",
            429: "Wait for active runs to finish.",
        }
        raise ClientError(
            f"HTTP {response.status_code}. {hints.get(response.status_code, 'Check service readiness.')}"
        )


class Client:
    def __init__(self, base_url="http://localhost:18000", api_key="", *, http_client=None):
        self.owned = http_client is None
        self.http = http_client or httpx.AsyncClient(
            base_url=base_url, headers={"Authorization": f"Bearer {api_key}"}, timeout=30
        )

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        if self.owned:
            await self.http.aclose()

    async def request(self, method, path, *, retry=False, **kwargs):
        for attempt in range(3 if retry else 1):
            try:
                response = await self.http.request(method, path, **kwargs)
                check(response)
                return response.json()
            except httpx.TransportError:
                if not retry or attempt == 2:
                    raise ClientError(
                        "Connection failed; check URL and readiness. Retry submissions with the same idempotency key.",
                        idempotency_key=kwargs.get("headers", {}).get("Idempotency-Key"),
                    ) from None
                await asyncio.sleep(0.1 * (attempt + 1))

    async def task(self, run_id):
        return await self.request("GET", f"/v1/runs/{run_id}/task", retry=True)

    async def capabilities(self, run_id, **params):
        return await self.request("GET", f"/v1/runs/{run_id}/capabilities", params=params, retry=True)

    async def verifications(self, run_id, **params):
        return await self.request("GET", f"/v1/runs/{run_id}/verifications", params=params, retry=True)

    async def checkpoints(self, run_id, **params):
        return await self.request("GET", f"/v1/runs/{run_id}/checkpoints", params=params, retry=True)

    async def operations(self, run_id, **params):
        return await self.request("GET", f"/v1/runs/{run_id}/operations", params=params, retry=True)

    async def workspace_create(self, files=None, *, idempotency_key=None):
        import base64

        return await self.request(
            "POST",
            "/v1/workspaces",
            retry=True,
            headers={"Idempotency-Key": idempotency_key or uuid4().hex},
            json={
                "files": [
                    {"path": p, "content_base64": base64.b64encode(b).decode()}
                    for p, b in (files or {}).items()
                ]
            },
        )

    async def workspace(self, workspace_id, revision_id=None):
        suffix = f"/revisions/{revision_id}" if revision_id else ""
        return await self.request("GET", f"/v1/workspaces/{workspace_id}{suffix}", retry=True)

    async def verified_bytes(self, url, **params):
        import hashlib

        response = await self.http.get(url, params=params)
        check(response)
        data = response.content
        if response.headers.get("X-SHA256") != hashlib.sha256(data).hexdigest() or int(
            response.headers.get("Content-Length", -1)
        ) != len(data):
            raise ClientError("Download integrity check failed")
        return data

    async def workspace_read(self, workspace_id, revision_id, path):
        return await self.verified_bytes(
            f"/v1/workspaces/{workspace_id}/revisions/{revision_id}/files", path=path
        )

    async def workspace_download(self, workspace_id, revision_id):
        import hashlib
        import io
        import zipfile

        manifest = await self.workspace(workspace_id, revision_id)
        data = await self.verified_bytes(f"/v1/workspaces/{workspace_id}/revisions/{revision_id}/download")
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            if archive.namelist() != [f["path"] for f in manifest["files"]]:
                raise ClientError("Archive manifest mismatch")
            for f in manifest["files"]:
                content = archive.read(f["path"])
                if len(content) != f["size_bytes"] or hashlib.sha256(content).hexdigest() != f["sha256"]:
                    raise ClientError("Archive file integrity check failed")
        return data

    async def operation_output(self, run_id, operation_id, name):
        return await self.verified_bytes(f"/v1/runs/{run_id}/operations/{operation_id}/outputs/{name}")

    async def installed_capabilities(self):
        return await self.request("GET", "/v1/capabilities", retry=True)

    async def skills(self):
        return await self.request("GET", "/v1/skills", retry=True)

    async def tools(self):
        return await self.request("GET", "/v1/tools", retry=True)

    async def upload(
        self, content: bytes, media_type="text/plain", filename="input.txt", *, idempotency_key=None
    ):
        from .tool_contracts import ArtifactRef

        return ArtifactRef.model_validate(
            await self.request(
                "POST",
                "/v1/artifacts",
                retry=True,
                content=content,
                headers={
                    "Content-Type": media_type,
                    "X-Filename": filename,
                    "Idempotency-Key": idempotency_key or uuid4().hex,
                },
            )
        )

    async def download(self, artifact_id):
        import hashlib

        ref = await self.request("GET", f"/v1/artifacts/{artifact_id}", retry=True)
        try:
            response = await self.http.get(f"/v1/artifacts/{artifact_id}/content")
            check(response)
            data = response.content
            if len(data) != ref["size_bytes"] or hashlib.sha256(data).hexdigest() != ref["sha256"]:
                raise ClientError("Artifact integrity check failed")
            return data
        except httpx.TransportError:
            raise ClientError("Artifact download failed") from None

    async def artifacts(self, run_id=None):
        return await self.request(
            "GET", f"/v1/runs/{run_id}/artifacts" if run_id else "/v1/artifacts", retry=True
        )

    async def children(self, run_id):
        return [
            Run.model_validate(r)
            for r in await self.request("GET", f"/v1/runs/{run_id}/children", retry=True)
        ]

    async def budget(self, run_id):
        return await self.request("GET", f"/v1/runs/{run_id}/budget", retry=True)

    async def effects(self, run_id):
        return await self.request("GET", f"/v1/runs/{run_id}/effects", retry=True)

    async def models(self):
        return await self.request("GET", "/v1/models", retry=True)

    async def readiness(self):
        return await self.request("GET", "/v1/readiness", retry=True)

    async def create_agent(self, config: AgentConfig):
        return Agent.model_validate(await self.request("POST", "/v1/agents", json=config.model_dump()))

    async def update_agent(self, agent_id, config: AgentConfig):
        return Agent.model_validate(
            await self.request("PUT", f"/v1/agents/{agent_id}", json=config.model_dump())
        )

    async def create_session(self):
        return Session.model_validate(await self.request("POST", "/v1/sessions"))

    async def submit(
        self,
        agent_id,
        input,
        *,
        session_id=None,
        artifact_ids=None,
        idempotency_key=None,
        task=None,
        workspace=None,
    ):
        key = idempotency_key or uuid4().hex
        body = RunCreate(
            agent_id=agent_id,
            input=input,
            session_id=session_id,
            artifact_ids=artifact_ids or [],
            task=task,
            workspace=workspace,
        )
        return Run.model_validate(
            await self.request(
                "POST", "/v1/runs", retry=True, json=body.model_dump(), headers={"Idempotency-Key": key}
            )
        )

    async def continue_run(
        self, run_id, input, *, idempotency_key=None, artifact_ids=None, task=None, workspace=None
    ):
        previous = await self.get(run_id)
        return await self.submit(
            previous.agent_id,
            input,
            session_id=previous.session_id,
            artifact_ids=artifact_ids,
            task=task,
            workspace=workspace,
            idempotency_key=idempotency_key,
        )

    async def get(self, run_id):
        return Run.model_validate(await self.request("GET", f"/v1/runs/{run_id}", retry=True))

    async def decide(self, run_id, approval_id, approved):
        return Run.model_validate(
            await self.request(
                "POST", f"/v1/runs/{run_id}/approvals/{approval_id}", retry=True, json={"approved": approved}
            )
        )

    async def approve(self, run_id, approval_id):
        return await self.decide(run_id, approval_id, True)

    async def deny(self, run_id, approval_id):
        return await self.decide(run_id, approval_id, False)

    async def resources(self, run_id):
        return await self.request("GET", f"/v1/runs/{run_id}/resources", retry=True)

    async def update_resources(self, run_id, *, expected_version, limits, idempotency_key=None):
        return await self.request(
            "PUT",
            f"/v1/runs/{run_id}/resources",
            retry=True,
            headers={"Idempotency-Key": idempotency_key or uuid4().hex},
            json={"expected_version": expected_version, "limits": limits},
        )

    async def cancel(self, run_id):
        return Run.model_validate(await self.request("POST", f"/v1/runs/{run_id}/cancel", retry=True))

    async def wait(self, run_id, *, timeout=150, stop_at_approval=True, stop_at_budget=True):
        try:
            async with asyncio.timeout(timeout):
                while True:
                    run = await self.get(run_id)
                    if (
                        (run.status in TERMINAL and run.cleanup_state == "complete")
                        or (stop_at_approval and run.status == "awaiting_approval")
                        or (stop_at_budget and run.status == "paused_budget")
                    ):
                        return run
                    await asyncio.sleep(0.2)
        except TimeoutError:
            raise ClientError(
                f"Wait timed out. Retrieve or watch run {run_id}; execution may still be active."
            ) from None

    async def watch(self, run_id, *, cursor=0, timeout=180):
        """Resume from the last yielded persisted ID; bounded reconnects and wall time."""
        failures = 0
        reconnects = 0
        terminal_confirmed = False
        try:
            async with asyncio.timeout(timeout):
                while True:
                    try:
                        delivered = False
                        async with self.http.stream(
                            "GET", f"/v1/runs/{run_id}/events", headers={"Last-Event-ID": str(cursor)}
                        ) as response:
                            check(response)
                            async for line in response.aiter_lines():
                                if line.startswith("data: "):
                                    event = Event.model_validate_json(line[6:])
                                    if event.id > cursor:
                                        cursor = event.id
                                        delivered = True
                                        yield event
                                        if event.type in {
                                            "run.completed",
                                            "run.failed",
                                            "run.cancelled",
                                            "cleanup.completed",
                                        }:
                                            latest = await self.get(run_id)
                                            if (
                                                latest.cleanup_state == "complete"
                                                and latest.execution_version != 3
                                                and event.type != "cleanup.completed"
                                            ):
                                                return
                        latest = await self.get(run_id)
                        terminal = latest.status in TERMINAL and latest.cleanup_state == "complete"
                        # Completion alone cannot prove the cursor consumed the stream.
                        # An empty replay handles cursors already at/past terminal.
                        if terminal_confirmed and terminal and not delivered:
                            return
                        terminal_confirmed = terminal
                    except httpx.TransportError:
                        failures += 1
                        if failures >= 3:
                            raise ClientError(
                                f"Stream disconnected. Resume run {run_id} from cursor {cursor}."
                            ) from None
                    reconnects += 1
                    if reconnects >= 3:
                        raise ClientError(f"Stream incomplete. Resume run {run_id} from cursor {cursor}.")
                    await asyncio.sleep(0.2)
        except TimeoutError:
            raise ClientError(f"Watch timed out. Resume run {run_id} from cursor {cursor}.") from None
