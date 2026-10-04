"""PostgreSQL/Temporal recovery of deterministic final-answer rejection."""

import pytest
from temporalio import activity
from temporalio.exceptions import ApplicationError
from test_general_integration import backend
from test_general_semantic import complete, stub, submit
from test_harness_integration import replay

from agent_runtime.general_runtime import GENERAL_ACTIVITIES, general_action

pytestmark = pytest.mark.integration


async def test_json_rejection_survives_lost_activity_result_and_repairs(pg_store, monkeypatch):
    action_attempts = []
    lost_result = False

    @activity.defn(name="general_action")
    async def unreliable_action(payload):
        nonlocal lost_result
        action_attempts.append(payload["step"])
        result = await general_action(payload)
        if not lost_result and "result_contract:schema_mismatch" in result.get("remaining_gaps", []):
            lost_result = True
            raise ApplicationError("simulated activity response loss after durable rejection")
        return result

    monkeypatch.setattr(
        "test_general_integration.GENERAL_ACTIVITIES",
        [unreliable_action if handler is general_action else handler for handler in GENERAL_ACTIVITIES],
    )
    contract = {
        "kind": "json_schema",
        "json_schema": {
            "type": "object",
            "properties": {"answer": {"type": "integer", "const": 12}},
            "required": ["answer"],
            "additionalProperties": False,
        },
    }

    def respond(context, index):
        assert context["result_contract"]["kind"] == "json_schema"
        if index == 0:
            return {**complete(), "answer": '{"answer": "12"}'}
        assert index == 1
        assert "result_contract:schema_mismatch" in context["last_result"]["remaining_gaps"]
        return {**complete(), "answer": '{"answer": 12}'}

    calls = stub(monkeypatch, respond)
    async with backend(pg_store, monkeypatch) as (client, temporal):
        run = await submit(
            client,
            task={
                "outcome": "Return the requested JSON answer",
                "criteria": [{"id": "sum", "statement": "Correctly calculate five plus seven"}],
                "result_contract": contract,
            },
        )
        result = await client.wait(run.id, timeout=45)
        assert result.status == "completed", result.error
        assert result.output.answer == '{"answer": 12}'
        assert lost_result and action_attempts == [0, 0, 1]
        assert len(calls) == 2
        events = await pg_store.events(run.id)
        assert len([event for event in events if event.type == "completion.rejected"]) == 1
        assert len([event for event in events if event.type == "run.completed"]) == 1
        assert len((await client.checkpoints(run.id))["items"]) == 2
        assert (await client.budget(run.id))["v3"]["counters"]["model_attempts"] == 2
        task = await client.task(run.id)
        assert task["assessment"]["accepted"]
        assert task["goal"]["result_contract"]["json_schema"] == contract["json_schema"]
        bundle = await client.evidence(run.id)
        assert bundle.payload.output.answer == result.output.answer
        assert bundle.payload.goal.result_contract.json_schema == contract["json_schema"]
        await temporal.get_workflow_handle("run:" + run.id).result()
        await replay(temporal, run.id)
        assert len(calls) == 2
