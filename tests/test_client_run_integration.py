"""Convenience flow through real PostgreSQL/Temporal, with fake models only."""

import asyncio

import pytest
from temporalio.client import WorkflowFailureError
from temporalio.exceptions import CancelledError
from test_general_integration import backend

from agent_runtime.general_contracts import GeneralPolicy
from agent_runtime.schemas import AgentConfig

pytestmark = pytest.mark.integration


async def test_simple_run_upload_contract_and_budget_pause(pg_store, monkeypatch, tmp_path):
    file = tmp_path / "input.txt"
    file.write_text("attached input\n")
    seen = []
    async with backend(pg_store, monkeypatch) as (client, temporal):
        agent = await client.create_agent(
            AgentConfig(
                name="Convenience integration",
                provider="fake",
                model="deterministic",
                tools=[],
                general=GeneralPolicy(limits={"model_attempts": 2}),
            )
        )
        task = {
            "outcome": "Return the scripted completion",
            "criteria": [{"id": "answer", "statement": "Return the required text"}],
            "result_contract": {"kind": "exact", "exact": "Completed the requested scoped task."},
        }
        result = await client.run(
            agent.id,
            "Run the scripted demonstration.",
            files=[file],
            task=task,
            on_progress=seen.append,
            timeout=40,
        )
        assert result.status == "completed" and result.answer == task["result_contract"]["exact"]
        assert result.outcome == "succeeded"
        assert len(result.details.artifact_ids) == 1 and any(p.stage == "submitted" for p in seen)
        assert (await client.evidence(result.run_id)).payload.output.answer == result.answer
        await temporal.get_workflow_handle("run:" + result.run_id).result()
        task["result_contract"]["exact"] = "unreachable scripted answer"
        paused = await client.run(agent.id, "Run the scripted demonstration.", task=task, timeout=40)
        try:
            assert paused.status == "paused_budget" and paused.resources["pause"]
            assert paused.outcome == "needs_attention" and paused.outcome_reason == "resource_limit"
            assert paused.next_action and not any(
                e.type == "resources.updated" for e in await pg_store.events(paused.run_id)
            )
        finally:
            await client.cancel(paused.run_id)
            cancelled = await client.result(paused.run_id, timeout=40)
            assert cancelled.status == "cancelled" and cancelled.details.cleanup_state == "complete"
            async with asyncio.timeout(15):
                try:
                    await temporal.get_workflow_handle("run:" + paused.run_id).result()
                except WorkflowFailureError as exc:
                    assert isinstance(exc.cause, CancelledError)


@pytest.mark.parametrize("approved", [True, False])
async def test_approval_resume_through_real_worker(pg_store, approved):
    import httpx
    from test_integration import working

    from agent_runtime.api import create_app
    from agent_runtime.client import Client

    async with working(pg_store) as (temporal, _):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(pg_store, "test")),
            base_url="http://test",
            headers={"Authorization": "Bearer test"},
        ) as http:
            client = Client(http_client=http)
            agent = await client.create_agent(
                AgentConfig(
                    name="Simple approval", provider="fake", model="deterministic", tools=["record_note"]
                )
            )
            paused = await client.run(agent.id, "note:integration confirmation", timeout=40)
            try:
                assert paused.status == "awaiting_approval"
                assert paused.outcome == "needs_attention"
                await client.decide(paused.run_id, paused.approvals[0].id, approved)
                result = await client.result(paused.run_id, timeout=40)
                assert result.status == "completed"
                assert result.outcome == ("succeeded" if approved else "blocked")
                assert result.answer == ("Note recorded" if approved else "The tool call was denied.")
                if not approved:
                    assert not await client.effects(result.run_id)
            finally:
                state = await client.get(paused.run_id)
                if state.status not in {"completed", "failed", "cancelled"}:
                    await client.cancel(paused.run_id)
                await client.result(paused.run_id, timeout=40)
                async with asyncio.timeout(15):
                    try:
                        await temporal.get_workflow_handle("run:" + paused.run_id).result()
                    except WorkflowFailureError as exc:
                        assert isinstance(exc.cause, CancelledError)
