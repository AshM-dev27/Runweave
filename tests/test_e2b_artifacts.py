"""Real store and activity boundary, fake isolated computer; no paid calls or host code execution."""

import copy
import json
import time

import pytest
from sqlalchemy import select
from test_e2b import Cloud, finish
from test_e2b import registration as legacy_registration
from test_general_semantic import http_client
from test_harness_extensions import action

from agent_runtime.artifact_store import OperationArtifacts
from agent_runtime.blob_storage import FileBlobStorage, bytes_chunks
from agent_runtime.db import ArtifactRow
from agent_runtime.e2b import ROOT
from agent_runtime.e2b_artifacts import ARGUMENTS_SCHEMA
from agent_runtime.extension_runtime import cleanup_extension
from agent_runtime.extensions import ExtensionRegistry
from agent_runtime.general_contracts import GeneralPolicy
from agent_runtime.general_db import GeneralOperationRow
from agent_runtime.general_runtime import general_action
from agent_runtime.runtime import configure_store
from agent_runtime.schemas import AgentConfig
from agent_runtime.store import Problem, Store

PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 2000


def registration(**config):
    return {
        **legacy_registration(),
        "alias": "e2b_files",
        "handler": "e2b.python.v2",
        "version": 2,
        "arguments_schema": ARGUMENTS_SCHEMA,
        "config": {"credential_env": "E2B_API_KEY", **config},
    }


def job(artifact_id=None):
    return {
        "code": "# isolated test fixture",
        "artifacts": {"input.bin": artifact_id} if artifact_id else {},
        "outputs": {
            "report.png": {"filename": "report.png", "media_type": "image/png"},
            "summary.json": {"filename": "summary.json", "media_type": "application/json"},
        },
    }


@pytest.fixture
def file_cloud(monkeypatch):
    class FileCloud(Cloud):
        async def run(self, *args, **kwargs):
            result = await super().run(*args, **kwargs)
            self.content[ROOT + "/report.png"] = PNG
            return result

    cloud = FileCloud()

    async def upload(sandbox, path, source):
        sandbox.content[path] = source.read_bytes()
        sandbox.calls.append(("upload", path))

    async def download(sandbox, path, limit):
        content = await sandbox.download(sandbox, path, limit)
        async for chunk in bytes_chunks(content):
            yield chunk

    monkeypatch.setenv("E2B_API_KEY", "test-e2b-secret")
    monkeypatch.setattr("agent_runtime.e2b.sandbox_class", lambda: cloud)
    monkeypatch.setattr("agent_runtime.e2b.download", cloud.download)
    monkeypatch.setattr("agent_runtime.e2b_artifacts.download_chunks", download)
    monkeypatch.setattr("agent_runtime.e2b_artifacts.upload_file", upload)
    return cloud


async def setup(store, client, arguments=None, attached=()):
    store.extensions = ExtensionRegistry({"tools": [registration()]})
    agent = await client.create_agent(
        AgentConfig(
            name="files", provider="fake", model="deterministic", tools=["e2b_files"], general=GeneralPolicy()
        )
    )
    run = await client.submit(agent.id, "Process attached files", artifact_ids=list(attached))
    return run, action(run.id, "e2b_files", arguments or job())


async def test_artifact_input_and_binary_outputs_are_normal_run_files(store, file_cloud, tmp_path):
    store.blobs = FileBlobStorage(tmp_path / "blobs")
    input = await store.upload(bytes(range(256)) * 4000, "application/octet-stream", "input.bin", "input")
    async with http_client(store) as client:
        run, payload = await setup(store, client, job(input.id), [input.id])
        result = await finish(payload)
        outputs = result["output"]["outputs"]
        assert set(outputs) == {"report.png", "summary.json"}
        assert file_cloud.content[ROOT + "/input.bin"] == bytes(range(256)) * 4000
        public = await client.get(run.id)
        assert {file.id for file in public.artifacts} == {file["id"] for file in outputs.values()}
        assert len(await client.artifacts(run.id)) == 3
        assert await client.download(outputs["report.png"]["id"]) == PNG
        assert "text" not in outputs["report.png"] and len(json.dumps(result)) < 8192
        assert await general_action(payload) == result
        assert file_cloud.count("create") == file_cloud.count("run") == file_cloud.count("kill") == 1


async def test_unattached_artifact_rejected_before_provider_acquisition(store, file_cloud):
    input = await store.upload(b"secret", "application/octet-stream", "private.bin", "private")
    async with http_client(store) as client:
        run, payload = await setup(store, client, job(input.id))
        result = await finish(payload)
        assert result["error"] == "artifact_not_authorized"
        assert file_cloud.count("create") == 0
        assert (await client.extension_status())["items"][0]["active"] == 0
        assert not (await client.get(run.id)).artifacts


async def test_publication_response_loss_reuses_file_identity_without_reexecution(
    store, file_cloud, tmp_path, monkeypatch
):
    store.blobs = FileBlobStorage(tmp_path / "blobs")
    original = OperationArtifacts.publish
    published = []

    async def lose_response(self, *args, **kwargs):
        reference = await original(self, *args, **kwargs)
        published.append(reference.id)
        if len(published) == 1:
            raise TimeoutError("lost after artifact commit")
        return reference

    monkeypatch.setattr(OperationArtifacts, "publish", lose_response)
    async with http_client(store) as client:
        run, payload = await setup(store, client)
        result = await finish(payload)
        assert len(published) == len(set(published)) == 2
        assert file_cloud.count("run") == file_cloud.count("create") == 1
        assert len((await client.get(run.id)).artifacts) == 2
        events = await store.events(run.id)
        assert sum(event.type == "artifact.created" for event in events) == 2
        async with store.database.sessions() as db:
            rows = list(await db.scalars(select(ArtifactRow).where(ArtifactRow.producer_run_id == run.id)))
            assert len(rows) == 2 and all(row.content is None for row in rows)
            operation = await db.get(GeneralOperationRow, run.id + ":action:0")
            assert "test-e2b-secret" not in json.dumps(operation.data)
            assert "signature" not in json.dumps(operation.data)
        assert result["output"]["cleanup_complete"]


async def test_replacement_store_resumes_collection_and_keeps_published_files(store, file_cloud, tmp_path):
    store.blobs = FileBlobStorage(tmp_path / "blobs")
    async with http_client(store) as client:
        run, payload = await setup(store, client)
        for _ in range(4):
            assert (await general_action(payload))["external_pending"]
        async with store.database.sessions() as db:
            before = copy.deepcopy((await db.get(GeneralOperationRow, run.id + ":action:0")).data)
            assert before["handler_state"]["phase"] == "collecting"
        replacement = Store(
            store.database, extensions=store.extensions, blobs=FileBlobStorage(tmp_path / "blobs")
        )
        configure_store(replacement)
        try:
            result = await finish(payload)
            ref = result["output"]["outputs"]["report.png"]
            assert (await replacement.artifact(ref["id"]))[1] == PNG
            assert file_cloud.count("run") == file_cloud.count("create") == 1
        finally:
            configure_store(store)


@pytest.mark.parametrize("alias", ["e2b_python", "e2b_files"])
async def test_replacement_waits_for_live_lease_then_collects_without_redispatch(store, file_cloud, alias):
    from examples.e2b_invoice import job as text_job

    store.extensions = ExtensionRegistry({"tools": [legacy_registration(), registration()]})
    file_cloud.missing_receipt = True
    async with http_client(store) as client:
        agent = await client.create_agent(
            AgentConfig(
                name="lease-recovery", provider="fake", model="deterministic", tools=[alias], general={}
            )
        )
        run = await client.submit(agent.id, "Recover the original command")
        payload = action(run.id, alias, text_job() if alias == "e2b_python" else job())
        for _ in range(3):
            assert (await general_action(payload))["external_pending"]
        assert file_cloud.count("run") == 1
        async with store.database.sessions.begin() as db:
            op = await db.get(GeneralOperationRow, run.id + ":action:0")
            op.data = {**op.data, "lease": time.time() + 60}
            before = copy.deepcopy(op.data)
        calls = list(file_cloud.calls)
        for _ in range(3):
            waiting = await general_action(payload)
            assert waiting["external_pending"] and 2 <= waiting["retry_after"] <= 30
        assert file_cloud.calls == calls
        async with store.database.sessions.begin() as db:
            op = await db.get(GeneralOperationRow, run.id + ":action:0")
            assert op.data == before
            op.data = {**op.data, "lease": time.time() - 1}
        file_cloud.content[ROOT + "/result.json"] = b'{"exit_code":0,"stdout":"generated","stderr":""}'
        result = await finish(payload)
        assert result["output"]["cleanup_complete"] and len(result["output"]["outputs"]) == 2
        assert file_cloud.count("create") == file_cloud.count("run") == file_cloud.count("kill") == 1
        assert (await client.budget(run.id))["v3"]["counters"]["tool_attempts"] == 1


async def test_cancelled_operation_cannot_publish_a_new_file(store, file_cloud, tmp_path):
    store.blobs = FileBlobStorage(tmp_path / "blobs")
    async with http_client(store) as client:
        run, payload = await setup(store, client)
        assert (await general_action(payload))["external_pending"]
        async with store.database.sessions() as db:
            op = await db.get(GeneralOperationRow, run.id + ":action:0")
            access = OperationArtifacts(store, run.id, op.id, op.data["owner"], 0)
        await client.cancel(run.id)
        with pytest.raises(Problem):
            await access.publish("late", bytes_chunks(b"late"), "text/plain", "late.txt")
        assert not (await client.get(run.id)).artifacts
        assert not list((tmp_path / "blobs").rglob("*"))
        assert await cleanup_extension(store, run.id, run.id + ":action:0", provider_io=True)
        assert file_cloud.killed and file_cloud.count("run") == 0


async def test_partial_output_quota_failure_preserves_files_and_cleans_guest(store, file_cloud, tmp_path):
    store.blobs = FileBlobStorage(tmp_path / "blobs")
    store.artifact_run_bytes = len(PNG)
    async with http_client(store) as client:
        run, payload = await setup(store, client)
        result = await finish(payload)
        assert result["error"] == "e2b_output_invalid" and result["output"]["cleanup_complete"]
        assert len((await client.get(run.id)).artifacts) == 1
        assert len([path for path in (tmp_path / "blobs").rglob("*") if path.is_file()]) == 1
        assert file_cloud.killed


async def test_v1_and_v2_share_provider_capacity(store, file_cloud):
    store.extensions = ExtensionRegistry({"tools": [legacy_registration(), registration()]})
    async with http_client(store) as client:
        payloads = []
        for alias in ["e2b_python", "e2b_files"] * 3:
            agent = await client.create_agent(
                AgentConfig(
                    name=alias, provider="fake", model="deterministic", tools=[alias], general=GeneralPolicy()
                )
            )
            run = await client.submit(agent.id, "wait")
            from examples.e2b_invoice import job as legacy_job

            payload = action(run.id, alias, legacy_job() if alias == "e2b_python" else job())
            payloads.append(payload)
            assert (await general_action(payload))["external_pending"]
        assert file_cloud.count("create") == 4
        status = await client.extension_status()
        assert len(status["items"]) == 1 and status["items"][0]["active"] == status["items"][0]["limit"] == 4


async def test_artifact_read_is_bounded_run_scoped_and_binary_is_explicit(store, file_cloud):
    import json
    from pathlib import Path

    definitions = json.loads(Path("config/extensions.json").read_text())
    store.extensions = ExtensionRegistry(definitions)
    source = await store.upload(b"line\n" * 2000, "text/plain", "text.txt", "read")
    private = await store.upload(b"private", "text/plain", "private.txt", "private")
    binary = await store.upload(b"\x00\xff", "application/octet-stream", "data.bin", "binary")
    async with http_client(store) as client:
        agent = await client.create_agent(
            AgentConfig(
                name="read",
                provider="fake",
                model="deterministic",
                tools=["artifact_read"],
                general=GeneralPolicy(),
            )
        )
        run = await client.submit(agent.id, "read", artifact_ids=[source.id, binary.id])
        result = await general_action(
            action(run.id, "artifact_read", {"artifact_id": source.id, "offset": 5, "length": 100})
        )
        assert result["output"]["text"] == "line\n" * 20
        assert result["output"]["next_offset"] == 105 and result["output"]["truncated"]
        result = await general_action(action(run.id, "artifact_read", {"artifact_id": binary.id}, step=1))
        assert result["error"] == "text_artifact_required"
        result = await general_action(action(run.id, "artifact_read", {"artifact_id": private.id}, step=2))
        assert result["error"] == "artifact_not_authorized"


async def test_corrupt_input_blob_cleans_sandbox_without_executing_code(store, file_cloud, tmp_path):
    store.blobs = FileBlobStorage(tmp_path / "blobs")
    source = await store.upload(b"original", "application/octet-stream", "input.bin", "input")
    store.blobs.path(source.sha256).write_bytes(b"tampered")
    async with http_client(store) as client:
        _, payload = await setup(store, client, job(source.id), [source.id])
        result = await finish(payload)
        assert result["error"] == "e2b_input_invalid" and result["output"]["cleanup_complete"]
        assert file_cloud.count("create") == file_cloud.count("kill") == 1
        assert file_cloud.count("run") == 0


async def test_upload_uses_bounded_multipart_reads_and_only_an_ephemeral_signed_url(monkeypatch):
    import io
    from types import SimpleNamespace

    import httpx

    from agent_runtime.e2b_artifacts import upload_file

    class GuardedFile(io.BytesIO):
        def read(self, size=-1):
            assert 0 < size <= 65536
            return super().read(size)

    class Source:
        def open(self, _):
            return GuardedFile(bytes(range(256)) * 4000)

    received = []

    class Transport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            assert request.url.params["signature"] == "test-ephemeral"
            async for chunk in request.stream:
                assert len(chunk) <= 65536
                received.append(chunk)
            return httpx.Response(200)

    original = httpx.AsyncClient
    monkeypatch.setattr(
        "agent_runtime.e2b_artifacts.httpx.AsyncClient",
        lambda **kwargs: original(transport=Transport(), **kwargs),
    )
    sandbox = SimpleNamespace(
        upload_url=lambda path, **kwargs: "https://sandbox.test/files?signature=test-ephemeral"
    )
    await upload_file(sandbox, ROOT + "/input.bin", Source())
    assert bytes(range(256)) * 4000 in b"".join(received)
