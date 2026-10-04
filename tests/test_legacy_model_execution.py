import json

import pytest
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import FunctionModel

from agent_runtime.general_contracts import GeneralPolicy
from agent_runtime.general_db import GeneralOperationRow
from agent_runtime.general_runtime import general_action, general_step
from agent_runtime.registry import Registry
from agent_runtime.schemas import AgentConfig, RunCreate


@pytest.mark.parametrize("resource_controls", [False, True])
async def test_legacy_model_activity_completes_and_replays_without_semantic_binding(
    store, monkeypatch, resource_controls
):
    store.registry = Registry(
        [
            {
                "provider": "local",
                "model": "legacy-test",
                "adapter": "openai_chat",
                "upstream_model": "legacy-test",
                "endpoint": "http://backend.invalid/v1",
                "auth": "none",
                "credential_env": None,
                "tool_calling": True,
                "max_output_tokens": 2048,
                "total_tokens_limit": 16000,
            }
        ]
    )
    calls = []

    async def respond(messages, info):
        context = json.loads(messages[-1].parts[0].content)
        calls.append(context)
        assert "parallel_tool_calls" not in (info.model_settings or {})
        action = {
            "kind": "complete",
            "answer": "12",
            "assessment": {
                "proposal_id": "legacy-completion",
                "state_version": context["state"]["version"],
                "goal_version": context["goal"]["version"],
                "revision_id": context["state"]["head"],
                "criteria": [
                    {
                        "criterion_id": criterion["id"],
                        "disposition": "satisfied",
                        "assessment": "Computed the requested sum.",
                    }
                    for criterion in context["goal"]["criteria"]
                ],
            },
        }
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {"action": action})])

    monkeypatch.setattr("agent_runtime.general_runtime.build_model", lambda _: FunctionModel(respond))
    agent = await store.agent(
        AgentConfig(
            name="legacy-test",
            provider="local",
            model="legacy-test",
            tools=[],
            general=GeneralPolicy() if resource_controls else GeneralPolicy(resources=None),
        )
    )
    run = await store.submit(RunCreate(agent_id=agent.id, input="Compute 5 + 7"), "legacy-test")

    decision = await general_step(run.id)
    assert "semantic" not in decision
    assert decision == await general_step(run.id)
    assert len(calls) == 1
    async with store.database.sessions() as db:
        operation = await db.get(GeneralOperationRow, f"{run.id}:model:0")
        assert "binding" not in operation.data
        assert operation.data["attempts"] == 1
    result = await general_action({"run_id": run.id, **decision})
    assert result["accepted"]
    completed = await store.get(run.id)
    assert completed.status == "completed"
    assert completed.output.answer == "12"
