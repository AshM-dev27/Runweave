"""Adaptive recovery through real PostgreSQL and Temporal with no paid calls."""

import asyncio

import pytest
from pydantic_ai.durable_exec.temporal import PydanticAIPlugin
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.usage import RequestUsage
from temporalio.worker import Replayer
from test_adaptive import roomy
from test_general_integration import backend
from test_general_semantic import complete, submit

from agent_runtime.general_workflow import GeneralWorkflow
from agent_runtime.schemas import RunCreate

pytestmark = pytest.mark.integration


async def test_automatic_growth_is_durable_serialized_and_replayable(pg_store, monkeypatch):
    roomy(pg_store)
    caps = []

    async def respond(messages, info):
        caps.append(info.model_settings["max_tokens"])
        if len(caps) == 1:
            return ModelResponse(
                parts=[TextPart("partial")],
                finish_reason="length",
                usage=RequestUsage(input_tokens=30, output_tokens=1000),
            )
        return ModelResponse(
            parts=[ToolCallPart(info.output_tools[0].name, {"action": complete()})],
            usage=RequestUsage(input_tokens=30, output_tokens=40),
        )

    monkeypatch.setattr("agent_runtime.general_runtime.build_model", lambda _: FunctionModel(respond))
    async with backend(pg_store, monkeypatch) as (client, temporal):
        run = await submit(client)
        done = await client.result(run.id, timeout=40)
        assert done.outcome == "succeeded" and done.answer == "12"
        assert caps == [1024, 2048]
        handle = temporal.get_workflow_handle("run:" + run.id)
        await handle.result()
        await Replayer(workflows=[GeneralWorkflow], plugins=[PydanticAIPlugin()]).replay_workflow(
            await handle.fetch_history()
        )
        state = await client.resources(run.id)
        assert state["usage"]["model_attempts"] == 2 and state["usage"]["reported_tokens"] == 1100
        assert state["adaptive"]["output_tokens"] == 2048
        assert done.details.cleanup_state == "complete"

    # A queued run lets multiple activity observations contend without executing it.
    other = await pg_store.submit(
        RunCreate(agent_id=run.agent_id, input="observe"),
        "concurrent-adaptation",
    )
    plans = await asyncio.gather(
        *[pg_store.adaptive_settings(other.id, context_bytes=30000) for _ in range(6)]
    )
    assert all(p == plans[0] for p in plans)
    assert len([e for e in await pg_store.events(other.id) if e.type == "resources.adapted"]) == 1
    await pg_store.cancel(other.id)
