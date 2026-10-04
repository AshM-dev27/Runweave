"""Outcomes and event payloads through real PostgreSQL/Temporal, without paid calls."""

import asyncio
import json

import httpx
import pytest
from pydantic_ai.durable_exec.temporal import PydanticAIPlugin
from temporalio.client import Client as TemporalClient
from temporalio.worker import Replayer, Worker

from agent_runtime.activities import ACTIVITIES
from agent_runtime.api import create_app
from agent_runtime.client import Client
from agent_runtime.dispatch import Dispatcher
from agent_runtime.schemas import AgentConfig
from agent_runtime.toolkit_runtime import toolkit_child, toolkit_state, toolkit_step, toolkit_tool
from agent_runtime.toolkit_workflow import ToolkitWorkflow

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("attached", [True, False])
async def test_file_scope_outcomes_are_durable_and_replayable(pg_store, attached):
    temporal = await TemporalClient.connect("localhost:7233", plugins=[PydanticAIPlugin()])
    async with Worker(
        temporal,
        task_queue=pg_store.test_schema,
        workflows=[ToolkitWorkflow],
        activities=ACTIVITIES + [toolkit_child, toolkit_state, toolkit_step, toolkit_tool],
    ):
        dispatcher = asyncio.create_task(Dispatcher(pg_store, temporal, pg_store.test_schema).run())
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=create_app(pg_store, "test")),
                base_url="http://test",
                headers={"Authorization": "Bearer test"},
            ) as http:
                client = Client(http_client=http)
                artifact = await client.upload(b"Authorized content\n", filename="input.txt")
                agent = await client.create_agent(
                    AgentConfig(
                        name="File scope", provider="fake", model="deterministic", tools=["document_read"]
                    )
                )
                prompt = "toolkit:" + json.dumps(
                    [{"tool": "document_read", "arguments": {"artifact_id": artifact.id}}]
                )
                submitted = await client.submit(
                    agent.id, prompt, artifact_ids=[artifact.id] if attached else []
                )
                try:
                    result = await client.result(submitted.id, timeout=40)
                    assert result.status == "completed"
                    assert result.outcome == ("succeeded" if attached else "blocked")
                    assert result.outcome_reason == (None if attached else "artifact_not_authorized")
                    events = [e async for e in client.watch(submitted.id)]
                    terminal = [e for e in events if e.type == "run.completed"]
                    assert len(terminal) == 1 and terminal[0].data["outcome"] == result.outcome
                    handle = temporal.get_workflow_handle("run:" + submitted.id)
                    await asyncio.wait_for(handle.result(), 20)
                    await Replayer(workflows=[ToolkitWorkflow], plugins=[PydanticAIPlugin()]).replay_workflow(
                        await handle.fetch_history()
                    )
                    assert result.details.cleanup_state == "complete"
                finally:
                    current = await client.get(submitted.id)
                    if current.status not in {"completed", "failed", "cancelled"}:
                        await client.cancel(submitted.id)
                    await client.result(submitted.id, timeout=30)
        finally:
            dispatcher.cancel()
            await asyncio.gather(dispatcher, return_exceptions=True)


@pytest.mark.parametrize("approved", [False, True])
async def test_v3_completion_cannot_override_approval_decision(pg_store, monkeypatch, approved):
    from test_general_integration import backend, invoke
    from test_harness_integration import replay

    from agent_runtime.client import ClientError
    from agent_runtime.general_contracts import GeneralPolicy

    async with backend(pg_store, monkeypatch) as (client, temporal):
        agent = await client.create_agent(
            AgentConfig(
                name="Durable v3 denial",
                provider="fake",
                model="deterministic",
                tools=["record_note"],
                general=GeneralPolicy(),
            )
        )
        run = await client.submit(
            agent.id, "general:" + json.dumps([invoke("record_note", text="Synthetic approval test")])
        )
        pending = await client.wait(run.id, timeout=30)
        assert pending.status == "awaiting_approval"
        await client.decide(run.id, pending.approvals[0].id, approved)
        final = await client.result(run.id, timeout=30)
        assert final.status == "completed"
        assert final.details.completion_assessment.accepted
        expected = "succeeded" if approved else "blocked"
        assert final.outcome == expected
        assert final.outcome_reason == (None if approved else "approval_denied")
        events = [event async for event in client.watch(run.id)]
        terminal = [event for event in events if event.type == "run.completed"]
        assert len(terminal) == 1 and terminal[0].data["outcome"] == expected
        effects = await client.effects(run.id)
        assert len(effects) == int(approved)
        if approved:
            await client.evidence(run.id)
        else:
            with pytest.raises(ClientError) as failure:
                await client.evidence(run.id)
            assert failure.value.status_code == 409
        await temporal.get_workflow_handle("run:" + run.id).result()
        await replay(temporal, run.id)
