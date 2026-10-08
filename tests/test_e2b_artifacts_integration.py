"""PostgreSQL/Temporal orchestration and replay with a fake remote computer."""

import asyncio
import json
import time

import pytest
from pydantic_ai.durable_exec.temporal import PydanticAIPlugin
from temporalio.worker import Replayer
from test_e2b_artifacts import PNG, job, registration
from test_e2b_artifacts import file_cloud as file_cloud
from test_general_integration import backend

from agent_runtime.blob_storage import FileBlobStorage
from agent_runtime.e2b import ROOT
from agent_runtime.extensions import ExtensionRegistry
from agent_runtime.general_contracts import GeneralPolicy
from agent_runtime.general_db import GeneralOperationRow
from agent_runtime.general_workflow import GeneralWorkflow
from agent_runtime.schemas import AgentConfig

pytestmark = pytest.mark.integration


async def submit(client, source):
    agent = await client.create_agent(
        AgentConfig(
            name="file-flow",
            provider="fake",
            model="deterministic",
            tools=["e2b_files"],
            general=GeneralPolicy(),
        )
    )
    script = [{"action": {"kind": "invoke", "capability": "e2b_files", "arguments": job(source.id)}}]
    return await client.submit(agent.id, "general:" + json.dumps(script), artifact_ids=[source.id])


async def test_files_return_in_result_survive_worker_replacement_and_replay(
    pg_store, monkeypatch, file_cloud, tmp_path
):
    pg_store.blobs = FileBlobStorage(tmp_path / "blobs")
    pg_store.extensions = ExtensionRegistry({"tools": [registration()]})
    source = await pg_store.upload(bytes(range(256)) * 4000, "application/octet-stream", "input.bin", "input")
    file_cloud.missing_receipt = True
    async with backend(pg_store, monkeypatch) as (client, _):
        run = await submit(client, source)
        async with asyncio.timeout(30):
            while file_cloud.count("run") == 0:
                await asyncio.sleep(0.1)
    file_cloud.content[ROOT + "/result.json"] = b'{"exit_code":0,"stdout":"generated","stderr":""}'
    # A crashed worker can leave a live lease beyond Temporal's short retry window.
    # Force that window rather than relying on the timing of worker shutdown.
    async with pg_store.database.sessions.begin() as db:
        op = await db.get(GeneralOperationRow, run.id + ":action:0")
        op.data = {**op.data, "lease": time.time() + 12}
    # Reopen the same blob directory as a replacement process would.
    pg_store.blobs = FileBlobStorage(tmp_path / "blobs")
    async with backend(pg_store, monkeypatch) as (client, temporal):
        result = await client.result(run.id, timeout=45)
        assert result.outcome == "succeeded" and len(result.files) == 2, result.model_dump()
        image = next(file for file in result.files if file.filename == "report.png")
        destination = tmp_path / "download.png"
        await client.download_file(image.id, destination)
        assert destination.read_bytes() == PNG
        assert file_cloud.count("run") == file_cloud.count("create") == file_cloud.count("kill") == 1
        assert (await client.extension_status())["items"][0]["active"] == 0
        history = await temporal.get_workflow_handle("run:" + run.id).fetch_history()
        await Replayer(workflows=[GeneralWorkflow], plugins=[PydanticAIPlugin()]).replay_workflow(history)


async def test_published_output_can_feed_another_file_operation(pg_store, monkeypatch, file_cloud, tmp_path):
    pg_store.blobs = FileBlobStorage(tmp_path / "blobs")
    pg_store.extensions = ExtensionRegistry({"tools": [registration()]})
    async with backend(pg_store, monkeypatch) as (client, _):
        source = await client.upload(b"first", "application/octet-stream", "source.bin")
        run = await submit(client, source)
        first = await client.result(run.id, timeout=45)
        image = next(file for file in first.files if file.filename == "report.png")
        second_run = await submit(client, image)
        second = await client.result(second_run.id, timeout=45)
        assert first.outcome == second.outcome == "succeeded"
        assert file_cloud.content[ROOT + "/input.bin"] == PNG
        assert len(second.files) == 2


async def test_single_client_call_uploads_files_model_sees_refs_and_reads_report(
    pg_store, monkeypatch, file_cloud, tmp_path
):
    from pathlib import Path

    from test_general_semantic import complete, stub

    pg_store.blobs = FileBlobStorage(tmp_path / "blobs")
    pg_store.extensions = ExtensionRegistry(json.loads(Path("config/extensions.json").read_text()))

    def decide(context, step):
        files = {ref["filename"]: ref for ref in context["artifacts"]}
        if step == 0:
            assert set(files) == {"source.bin"}
            return {"kind": "invoke", "capability": "e2b_files", "arguments": job(files["source.bin"]["id"])}
        if step == 1:
            assert set(files) == {"source.bin", "report.png", "summary.json"}
            return {
                "kind": "invoke",
                "capability": "artifact_read",
                "arguments": {"artifact_id": files["summary.json"]["id"]},
            }
        assert json.loads(context["last_result"]["output"]["text"])["net_total"] == "130.00"
        return complete()

    contexts = stub(monkeypatch, decide)
    source = tmp_path / "source.bin"
    source.write_bytes(bytes(range(256)) * 4000)
    async with backend(pg_store, monkeypatch) as (client, temporal):
        agent = await client.create_agent(
            AgentConfig(
                name="one-call",
                provider="fake",
                model="deterministic",
                tools=["e2b_files", "artifact_read"],
                general=GeneralPolicy(),
            )
        )
        result = await client.run(
            agent.id, "semantic: process attached data and inspect the report", files=[source], timeout=60
        )
        assert result.outcome == "succeeded" and len(result.files) == 2
        assert len(contexts) == 3 and all(len(json.dumps(context)) < 16000 for context in contexts)
        assert file_cloud.content[ROOT + "/input.bin"] == source.read_bytes()
        assert file_cloud.count("create") == file_cloud.count("run") == file_cloud.count("kill") == 1
        history = await temporal.get_workflow_handle("run:" + result.run_id).fetch_history()
        await Replayer(workflows=[GeneralWorkflow], plugins=[PydanticAIPlugin()]).replay_workflow(history)
