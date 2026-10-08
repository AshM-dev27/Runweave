import asyncio
import json
from types import SimpleNamespace

import pytest
from pydantic_ai.durable_exec.temporal import PydanticAIPlugin
from temporalio.worker import Replayer
from test_e2b import Cloud, registration
from test_e2b import cloud as cloud
from test_general_integration import backend

from agent_runtime.extensions import ExtensionRegistry
from agent_runtime.general_contracts import GeneralPolicy
from agent_runtime.general_workflow import GeneralWorkflow
from agent_runtime.schemas import AgentConfig
from examples.e2b_invoice import run_demo

pytestmark = pytest.mark.integration


async def test_single_task_multiple_actions_and_replay(pg_store, monkeypatch, cloud, tmp_path):
    pg_store.extensions = ExtensionRegistry({"tools": [registration()]})
    async with backend(pg_store, monkeypatch) as (client, temporal):
        result, output = await run_demo(client, tmp_path / "outputs")
        assert result.outcome == "succeeded" and output["cleanup_complete"]
        assert cloud.count("create") == cloud.count("run") == cloud.count("kill") == 1
        assert (tmp_path / "outputs/summary.json").exists()
        history = await temporal.get_workflow_handle("run:" + result.run_id).fetch_history()
        await Replayer(workflows=[GeneralWorkflow], plugins=[PydanticAIPlugin()]).replay_workflow(history)


async def test_worker_replacement_resumes_existing_remote_job(pg_store, monkeypatch, cloud):
    from agent_runtime.e2b import ROOT
    from examples.e2b_invoice import job

    pg_store.extensions = ExtensionRegistry({"tools": [registration()]})
    cloud.missing_receipt = True
    async with backend(pg_store, monkeypatch) as (client, _):
        agent = await client.create_agent(
            AgentConfig(
                name="restart",
                provider="fake",
                model="deterministic",
                tools=["e2b_python"],
                general=GeneralPolicy(),
            )
        )
        run = await client.submit(
            agent.id,
            "general:"
            + json.dumps(
                [
                    {"action": {"kind": "invoke", "capability": "e2b_python", "arguments": job()}},
                ]
            ),
        )
        async with asyncio.timeout(30):
            while cloud.count("run") == 0:
                await asyncio.sleep(0.1)
        assert (await client.extension_status())["items"][0]["active"] == 1
    # Remote work finishes while no worker is polling. Its saved dispatch must not be repeated.
    cloud.content[ROOT + "/result.json"] = b'{"exit_code":0,"stdout":"generated","stderr":""}'
    async with backend(pg_store, monkeypatch) as (client, temporal):
        result = await client.result(run.id, timeout=45)
        assert result.outcome == "succeeded"
        assert cloud.count("create") == cloud.count("run") == cloud.count("kill") == 1
        assert (await client.extension_status())["items"][0]["active"] == 0
        history = await temporal.get_workflow_handle("run:" + run.id).fetch_history()
        await Replayer(workflows=[GeneralWorkflow], plugins=[PydanticAIPlugin()]).replay_workflow(history)


async def test_parallel_tasks_respect_durable_provider_limit(pg_store, monkeypatch, tmp_path):
    sandboxes = {}
    peak = 0

    async def create(**kwargs):
        nonlocal peak
        sandbox = Cloud()
        sandbox.sandbox_id = "sandbox-" + str(len(sandboxes))
        sandbox.metadata = kwargs["metadata"]
        sandboxes[sandbox.sandbox_id] = sandbox
        peak = max(peak, sum(not item.killed for item in sandboxes.values()))
        assert peak <= 4
        return sandbox

    async def connect(identity, **kwargs):
        return sandboxes[identity]

    async def kill(identity, **kwargs):
        return await sandboxes[identity].kill(identity, **kwargs)

    async def download(sandbox, path, limit):
        return await sandbox.download(sandbox, path, limit)

    monkeypatch.setenv("E2B_API_KEY", "test-e2b-secret")
    monkeypatch.setattr(
        "agent_runtime.e2b.sandbox_class", lambda: SimpleNamespace(create=create, connect=connect, kill=kill)
    )
    monkeypatch.setattr("agent_runtime.e2b.download", download)
    pg_store.extensions = ExtensionRegistry({"tools": [registration()]})
    async with backend(pg_store, monkeypatch) as (client, _):
        results = await asyncio.gather(*(run_demo(client, tmp_path / str(i)) for i in range(8)))
        assert all(result.outcome == "succeeded" for result, _ in results)
        assert len(sandboxes) == 8 and 2 <= peak <= 4
        assert all(sandbox.killed and sandbox.count("run") == 1 for sandbox in sandboxes.values())
        assert (await client.extension_status())["items"][0]["active"] == 0
