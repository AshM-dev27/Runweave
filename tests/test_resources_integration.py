import asyncio

import pytest
from test_general_integration import backend
from test_general_semantic import complete, stub, submit
from test_harness_integration import replay
from test_harness_review import reviewer
from test_resources import charge

from agent_runtime.general_contracts import GeneralPolicy
from agent_runtime.resources import ResourceBlocked

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("boundary", ["model", "tool", "review"])
async def test_durable_pause_resume_replay_without_repeating_work(pg_store, monkeypatch, boundary):
    actions = [{"kind": "invoke", "capability": "add", "arguments": {"a": i, "b": 2}} for i in range(3)]
    calls = stub(monkeypatch, [complete()] if boundary == "review" else actions + [complete()])
    reviews = reviewer(monkeypatch)
    resource = "tool_attempts" if boundary == "tool" else "model_attempts"
    policy = GeneralPolicy(
        resources={"max_pause_seconds": 60},
        limits={resource: 2 if boundary == "tool" else 1},
        review={} if boundary == "review" else None,
    )
    async with backend(pg_store, monkeypatch) as (client, temporal):
        run = await submit(client, ["add"], policy=policy)
        paused = await client.wait(run.id, timeout=30)
        assert paused.status == "paused_budget", paused.model_dump()
        state = await client.resources(run.id)
        assert state["pause"]["block"]["resource"] == resource
        count = len(calls)
        await asyncio.sleep(0.1)
        assert len(calls) == count
        update = dict(expected_version=1, limits={resource: 8}, idempotency_key="resume")
        assert await client.update_resources(run.id, **update) == await client.update_resources(
            run.id, **update
        )
        final = await client.wait(run.id, timeout=30, stop_at_budget=False)
        assert final.status == "completed", final.model_dump()
        assert len(calls) == (1 if boundary == "review" else 4)
        assert len(reviews) == (1 if boundary == "review" else 0)
        assert (await client.resources(run.id))["usage"]["tool_attempts"] == (
            0 if boundary == "review" else 3
        )
        await temporal.get_workflow_handle("run:" + run.id).result()
        await replay(temporal, run.id)


@pytest.mark.parametrize("mode", ["cancel", "timeout", "fail"])
async def test_budget_stop_modes_are_durable(pg_store, monkeypatch, mode):
    calls = stub(monkeypatch, [{"kind": "invoke", "capability": "add", "arguments": {"a": 1, "b": 2}}])
    async with backend(pg_store, monkeypatch) as (client, temporal):
        run = await submit(
            client,
            ["add"],
            policy=GeneralPolicy(
                limits={"model_attempts": 1},
                resources={
                    "max_pause_seconds": 1 if mode == "timeout" else 60,
                    "on_limit": "fail" if mode == "fail" else "pause",
                },
            ),
        )
        result = await client.wait(run.id, timeout=30, stop_at_budget=mode == "cancel")
        if mode == "cancel":
            assert result.status == "paused_budget"
            await client.cancel(run.id)
            assert (await client.get(run.id)).status == "cancelled"
        else:
            assert result.status == "failed", result.model_dump()
            assert result.error == ("resource_pause_timeout" if mode == "timeout" else "resource_limit")
            await temporal.get_workflow_handle("run:" + run.id).result()
            await replay(temporal, run.id)
        assert len(calls) == 1


async def test_concurrent_actual_charges_cannot_overdraw_root(pg_store):
    from test_general_semantic import http_client

    async with http_client(pg_store) as client:
        run = await submit(client, policy=GeneralPolicy(resources={}, limits={"model_attempts": 3}))
        results = await asyncio.gather(*(charge(pg_store, run.id) for _ in range(12)), return_exceptions=True)
        assert sum(r is None for r in results) == 3
        assert all(r is None or isinstance(r, ResourceBlocked) for r in results)
        assert (await client.resources(run.id))["usage"]["model_attempts"] == 3
