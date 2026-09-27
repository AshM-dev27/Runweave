"""Concurrent activity retries and cancellation against isolated PostgreSQL state."""

import asyncio
import json

import pytest
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import FunctionModel
from temporalio.exceptions import ApplicationError
from test_general_semantic import complete, http_client, stub, submit
from test_harness_extensions import Lookup, action, create, registration

from agent_runtime.completion_review import ensure_review
from agent_runtime.extensions import ExtensionRegistry
from agent_runtime.general_completion import general_completion
from agent_runtime.general_contracts import GeneralPolicy
from agent_runtime.general_runtime import general_action, general_step
from agent_runtime.store import Problem

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("write", [False, True])
async def test_concurrent_tool_retry_executes_once(pg_store, write):
    started, release = asyncio.Event(), asyncio.Event()

    class Slow(Lookup):
        async def execute(self, call):
            self.calls.append(call)
            started.set()
            await release.wait()
            return {"recorded": call.arguments["customer"]}

        async def reconcile(self, call):
            pytest.fail("A concurrent retry must wait for the original lease")

    handler = Slow()
    pg_store.extensions = ExtensionRegistry(
        {"tools": [registration(write)]}, handlers={"installed.lookup": handler}
    )
    async with http_client(pg_store) as client:
        run = await create(client)
        payload = action(run.id)
        if write:
            pending = await general_action(payload)
            await client.decide(run.id, pending["approval"], True)
        task = asyncio.create_task(general_action(payload))
        try:
            await asyncio.wait_for(started.wait(), 3)
            with pytest.raises(ApplicationError, match="extension_lease_pending"):
                await general_action(payload)
        finally:
            release.set()
            result = await task
        assert await general_action(payload) == result
        assert len(handler.calls) == 1
        assert (await client.budget(run.id))["v3"]["counters"]["tool_attempts"] == 1
        assert len([e for e in await pg_store.events(run.id) if e.type == "tool.completed"]) == 1


@pytest.mark.parametrize("cancel", [False, True])
async def test_review_lease_and_cancellation_fence(pg_store, monkeypatch, cancel):
    started, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def judge(messages, info):
        request = json.loads(messages[-1].parts[0].content)
        calls.append(request)
        started.set()
        await release.wait()
        return ModelResponse(
            parts=[
                ToolCallPart(
                    info.output_tools[0].name,
                    {
                        "judgments": [
                            {
                                "criterion_id": c["id"],
                                "verdict": "pass",
                                "confidence": 0.99,
                                "reason": "fixture",
                            }
                            for c in request["criteria"]
                        ]
                    },
                )
            ]
        )

    monkeypatch.setattr("agent_runtime.completion_review.build_review_model", lambda _: FunctionModel(judge))
    stub(monkeypatch, [complete()])
    async with http_client(pg_store) as client:
        run = await submit(client, policy=GeneralPolicy(review={}))
        step = await general_step({"run_id": run.id, "completion_loop": 2})
        prepared = await general_completion({"run_id": run.id, **step})
        task = asyncio.create_task(ensure_review(pg_store, run.id, prepared["decision"]["action"]))
        try:
            await asyncio.wait_for(started.wait(), 3)
            with pytest.raises(Problem, match="review_lease_pending"):
                await ensure_review(pg_store, run.id, prepared["decision"]["action"])
            if cancel:
                await client.cancel(run.id)
        finally:
            release.set()
            await task
        if cancel:
            final = await client.get(run.id)
            assert final.status == "cancelled" and final.output is None
            records = (await client.operations(run.id))["items"]
            review = next(o for o in records if ":review:" in o["id"])
            assert review["result"]["reason"] == "stale_review"
        else:
            assert (await general_action(prepared))["accepted"]
        assert len(calls) == 1
