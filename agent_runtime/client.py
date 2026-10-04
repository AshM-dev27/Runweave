"""Small asynchronous client using only the versioned public HTTP contracts."""

import asyncio
import hashlib
import inspect
import math
from pathlib import Path
from uuid import uuid4

import httpx

from .client_errors import ClientError, check
from .client_results import MESSAGES, RunProgress, RunResult, outcome
from .schemas import Agent, AgentConfig, Event, Run, RunCreate, Session

TERMINAL = {"completed", "failed", "cancelled"}


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
                try:
                    check(response)
                except ClientError as exc:
                    exc.idempotency_key = kwargs.get("headers", {}).get("Idempotency-Key")
                    raise
                return response.json()
            except httpx.TransportError:
                if not retry or attempt == 2:
                    raise ClientError(
                        "Connection failed; check URL and readiness. Retry submissions with the same idempotency key.",
                        idempotency_key=kwargs.get("headers", {}).get("Idempotency-Key"),
                    ) from None
                await asyncio.sleep(0.1 * (attempt + 1))

    async def evidence(self, run_id):
        from .evidence import AcceptanceBundle, verify_evidence_bundle

        raw = await self.request("GET", f"/v1/runs/{run_id}/evidence", retry=True)
        if not verify_evidence_bundle(raw).valid:
            raise ClientError("Evidence integrity or acceptance verification failed.")
        return AcceptanceBundle.model_validate(raw)

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

    async def run(
        self,
        agent_id,
        input,
        *,
        files=None,
        session_id=None,
        artifact_ids=None,
        task=None,
        workspace=None,
        idempotency_key=None,
        timeout=150,
        on_progress=None,
    ) -> RunResult:
        """Upload explicit files, submit once, and return a result or an actionable pause.

        timeout bounds waiting after submission, not server execution. A timeout never
        cancels the task. Retry this same request with its key, or use result(run_id).
        """
        key = idempotency_key if idempotency_key is not None else uuid4().hex
        run_id = None
        try:
            self._validate_wait(timeout, on_progress)
            if (
                not isinstance(key, str)
                or not 1 <= len(key) <= 128
                or not key.isascii()
                or any(ord(c) < 33 or ord(c) > 126 for c in key)
            ):
                raise ClientError(
                    "Use an idempotency key of 1–128 printable ASCII characters without spaces."
                )
            from pydantic import ValidationError

            from .client_errors import validation_fields

            try:
                body = RunCreate(
                    agent_id=agent_id,
                    input=input,
                    session_id=session_id,
                    artifact_ids=artifact_ids or [],
                    task=task,
                    workspace=workspace,
                )
            except ValidationError as exc:
                fields = validation_fields(exc.errors(include_url=False, include_input=False))
                raise ClientError(
                    "Check task input fields: " + ", ".join(fields) + ".",
                    code="validation_error",
                    fields=fields,
                ) from None
            prepared = self._prepare_files(files)
            if len(prepared) + len(body.artifact_ids) > 8:
                raise ClientError("Attach at most eight files and existing artifacts per task.")
            attached = list(body.artifact_ids)
            for index, (filename, media, content) in enumerate(prepared):
                await self._progress(
                    on_progress,
                    RunProgress(
                        stage="uploading",
                        message=f"Uploading file {index + 1} of {len(prepared)}.",
                        idempotency_key=key,
                    ),
                )
                # Stable per-request slots detect changed files instead of silently creating a new upload.
                upload_key = "run-file-" + hashlib.sha256(f"{key}:{index}".encode()).hexdigest()
                ref = await self.upload(content, media, filename, idempotency_key=upload_key)
                attached.append(ref.id)
            submitted = await self.submit(
                agent_id,
                input,
                session_id=session_id,
                artifact_ids=attached,
                task=task,
                workspace=workspace,
                idempotency_key=key,
            )
            run_id = submitted.id
            await self._progress(
                on_progress,
                RunProgress(
                    stage="submitted",
                    message="Task submitted; this run ID can be used to resume waiting.",
                    run_id=run_id,
                    idempotency_key=key,
                ),
            )
            return await self.result(run_id, timeout=timeout, on_progress=on_progress, idempotency_key=key)
        except ClientError as exc:
            exc.idempotency_key = key
            exc.run_id = run_id or exc.run_id
            raise

    async def result(self, run_id, *, timeout=150, on_progress=None, idempotency_key=None) -> RunResult:
        """Wait for an existing task without submitting, approving or increasing limits."""
        self._validate_wait(timeout, on_progress)
        try:
            run = await self.wait(run_id, timeout=timeout, on_progress=on_progress)
            resources = await self.resources(run_id) if run.status == "paused_budget" else None
            return outcome(run, resources, idempotency_key)
        except ClientError as exc:
            exc.run_id = run_id
            exc.idempotency_key = idempotency_key
            raise

    @staticmethod
    def _validate_wait(timeout, callback):
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            raise ClientError("timeout must be a positive, finite number of seconds.")
        if callback is not None and not callable(callback):
            raise ClientError("on_progress must be a callable accepting one RunProgress value.")

    @staticmethod
    async def _progress(callback, progress):
        if callback is None:
            return
        try:
            returned = callback(progress)
            if inspect.isawaitable(returned):
                await returned
        except Exception:
            raise ClientError(
                "Progress callback failed. Use run_id to resume waiting if submission already succeeded.",
                code="progress_callback_failed",
                run_id=progress.run_id,
                idempotency_key=progress.idempotency_key,
            ) from None

    @staticmethod
    def _prepare_files(files):
        import os
        import stat

        from .artifacts import MAX_ARTIFACT, validate
        from .client_errors import PUBLIC_ERRORS

        if files is None:
            return []
        if not isinstance(files, (list, tuple)) or len(files) > 8:
            raise ClientError("files must be a list of at most eight local file paths.")
        media_types = {
            ".txt": "text/plain",
            ".md": "text/markdown",
            ".csv": "text/csv",
            ".json": "application/json",
            ".zip": "application/zip",
            ".diff": "text/x-diff",
            ".patch": "text/x-diff",
        }
        prepared = []
        for item in files:
            try:
                path = Path(item)
                if path.suffix.lower() not in media_types:
                    raise ClientError(
                        "Unsupported file type. Use .txt, .md, .csv, .json, .zip, .diff or .patch."
                    )
                if path.is_symlink():
                    raise ClientError("Upload a regular file, not a symbolic link.")
                # Open nonblocking so a device or FIFO cannot hang the client before validation.
                fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
                with os.fdopen(fd, "rb") as file:
                    if not stat.S_ISREG(os.fstat(file.fileno()).st_mode):
                        raise ClientError("Upload a regular file.")
                    content = file.read(MAX_ARTIFACT + 1)
                media = media_types[path.suffix.lower()]
                validate(content, media, path.name)
                prepared.append((path.name, media, content))
            except (OSError, TypeError):
                raise ClientError(
                    "Cannot read an attached file. Check that each path is a readable regular file."
                ) from None
            except ValueError as exc:
                code, message = PUBLIC_ERRORS.get(
                    str(exc), ("invalid_file", "Check the attached file's content and format.")
                )
                raise ClientError(message, code=code) from None
        return prepared

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

    async def recovery(self, run_id, **params):
        return await self.request("GET", f"/v1/runs/{run_id}/recovery", params=params, retry=True)

    async def reconcile(self, run_id, request, *, idempotency_key=None):
        return await self.request(
            "POST",
            f"/v1/runs/{run_id}/reconciliations",
            retry=True,
            headers={"Idempotency-Key": idempotency_key or uuid4().hex},
            json=request,
        )

    async def reconciliation(self, run_id, identity):
        return await self.request("GET", f"/v1/runs/{run_id}/reconciliations/{identity}", retry=True)

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

    async def wait(
        self, run_id, *, timeout=150, stop_at_approval=True, stop_at_budget=True, on_progress=None
    ):
        self._validate_wait(timeout, on_progress)
        previous_stage = None
        try:
            async with asyncio.timeout(timeout):
                while True:
                    run = await self.get(run_id)
                    stage = (
                        "cleaning_up"
                        if run.status in TERMINAL and run.cleanup_state != "complete"
                        else run.status
                    )
                    if run.status == "awaiting_approval" and not run.approvals:
                        stage = "resuming"
                    if stage != previous_stage:
                        await self._progress(
                            on_progress,
                            RunProgress(
                                stage=stage,
                                message=outcome(run).message if stage in TERMINAL else MESSAGES[stage],
                                run_id=run_id,
                                outcome=run.outcome,
                                outcome_reason=run.outcome_reason,
                            ),
                        )
                        previous_stage = stage
                    budget_stop = stop_at_budget and run.status == "paused_budget"
                    if budget_stop:
                        state = await self.resources(run_id)
                        # Reservations held by an in-flight request settle without operator action.
                        budget_stop = (state.get("pause") or {}).get("block", {}).get("reason") != "capacity"
                    if (
                        (run.status in TERMINAL and run.cleanup_state == "complete")
                        or (stop_at_approval and run.status == "awaiting_approval" and run.approvals)
                        or budget_stop
                    ):
                        return run
                    await asyncio.sleep(0.2)
        except TimeoutError:
            raise ClientError(
                f"Wait timed out. Resume with client.result({run_id!r}); execution may still be active.",
                code="wait_timeout",
                run_id=run_id,
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
