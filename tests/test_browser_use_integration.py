import asyncio
import json

import pytest
from test_browser_use import cloud as cloud_fixture
from test_browser_use import registration
from test_general_integration import backend, invoke
from test_harness_integration import replay

from agent_runtime.extensions import ExtensionRegistry
from agent_runtime.general_contracts import GeneralPolicy
from agent_runtime.schemas import AgentConfig

cloud = cloud_fixture

pytestmark = pytest.mark.integration


async def launch(client):
    agent = await client.create_agent(
        AgentConfig(
            name="browser integration",
            provider="fake",
            model="deterministic",
            tools=["browser_task"],
            general=GeneralPolicy(),
        )
    )
    run = await client.submit(
        agent.id, "general:" + json.dumps([invoke("browser_task", task="Read example.com")])
    )
    for _ in range(200):
        state = await client.get(run.id)
        if state.status == "awaiting_approval":
            return state
        await asyncio.sleep(0.1)
    pytest.fail("Approval not reached")


async def test_real_workflow_polls_and_finishes_once(pg_store, monkeypatch, cloud):
    pg_store.extensions = ExtensionRegistry({"tools": [registration()]})
    async with backend(pg_store, monkeypatch) as (client, temporal):
        run = await launch(client)
        assert not cloud.calls
        await client.decide(run.id, run.approvals[0].id, True)
        result = await client.wait(run.id, timeout=30)
        assert result.status == "completed"
        assert len(cloud.creates) == 1
        assert any(method == "PATCH" for method, _, _ in cloud.calls)
        assert result.outcome == "succeeded"
        await temporal.get_workflow_handle("run:" + run.id).result()
        await replay(temporal, run.id)


async def test_real_cancellation_dispatches_cleanup_activity(pg_store, monkeypatch, cloud):
    cloud.status = "running"
    pg_store.extensions = ExtensionRegistry({"tools": [registration()]})
    async with backend(pg_store, monkeypatch) as (client, temporal):
        run = await launch(client)
        await client.decide(run.id, run.approvals[0].id, True)
        for _ in range(100):
            if cloud.creates:
                break
            await asyncio.sleep(0.1)
        assert len(cloud.creates) == 1
        await client.cancel(run.id)
        for _ in range(500):
            if (await pg_store.general(run.id))["cleanup_state"] == "complete":
                break
            await asyncio.sleep(0.1)
        else:
            pytest.fail("Browser cleanup did not complete")
        assert cloud.status == "cancelled"
        assert any(method == "PATCH" for method, _, _ in cloud.calls)
        assert len(cloud.creates) == 1
        assert (await client.get(run.id)).status == "cancelled"
        await temporal.get_workflow_handle("extension-cleanup:" + run.id).result()
