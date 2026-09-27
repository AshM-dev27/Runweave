import json

import pytest
from pydantic_ai.durable_exec.temporal import PydanticAIPlugin
from temporalio.worker import Replayer
from test_general_integration import backend
from test_general_semantic import complete, stub, submit
from test_harness_extensions import Lookup, Writer, registration

from agent_runtime.extensions import ExtensionRegistry
from agent_runtime.general_contracts import GeneralPolicy
from agent_runtime.general_workflow import GeneralWorkflow

pytestmark = pytest.mark.integration


async def replay(temporal, run_id):
    history = await temporal.get_workflow_handle("run:" + run_id).fetch_history()
    await Replayer(workflows=[GeneralWorkflow], plugins=[PydanticAIPlugin()]).replay_workflow(history)


async def test_review_repair_then_acceptance_and_replay(pg_store, monkeypatch):
    from pydantic_ai.messages import ModelResponse, ToolCallPart
    from pydantic_ai.models.function import FunctionModel

    actions = [complete(), {**complete(), "answer": "The repaired answer"}]
    stub(monkeypatch, actions)
    reviews = []

    def judge(messages, info):
        value = json.loads(messages[-1].parts[0].content)
        reviews.append(value)
        verdict = "repair" if len(reviews) == 1 else "pass"
        return ModelResponse(
            parts=[
                ToolCallPart(
                    info.output_tools[0].name,
                    {
                        "judgments": [
                            {
                                "criterion_id": c["id"],
                                "verdict": verdict,
                                "confidence": 0.99,
                                "reason": "Review fixture",
                            }
                            for c in value["criteria"]
                        ]
                    },
                )
            ]
        )

    monkeypatch.setattr("agent_runtime.completion_review.build_review_model", lambda _: FunctionModel(judge))
    async with backend(pg_store, monkeypatch) as (client, temporal):
        run = await submit(client, policy=GeneralPolicy(review={}))
        result = await client.wait(run.id, timeout=50)
        assert result.status == "completed", result.error
        assert result.output.answer == "The repaired answer"
        assert len(reviews) == 2
        events = await pg_store.events(run.id)
        assert [e.data["verdict"] for e in events if e.type == "completion.reviewed"] == ["repair", "pass"]
        assert (await client.budget(run.id))["v3"]["counters"]["model_attempts"] == 4
        await replay(temporal, run.id)


async def test_extension_write_approval_reconciliation_and_replay(pg_store, monkeypatch):
    handler = Writer()
    pg_store.extensions = ExtensionRegistry(
        {"tools": [registration(True)]}, handlers={"installed.lookup": handler}
    )
    calls = stub(
        monkeypatch,
        [{"kind": "invoke", "capability": "customer_lookup", "arguments": {"customer": "one"}}, complete()],
    )
    async with backend(pg_store, monkeypatch) as (client, temporal):
        run = await submit(client, ["customer_lookup"])
        pending = await client.wait(run.id, timeout=30)
        assert pending.status == "awaiting_approval" and not handler.calls
        await client.approve(run.id, pending.approvals[0].id)
        result = await client.wait(run.id, timeout=50)
        assert result.status == "completed", result.error
        assert len(handler.effects) == len(handler.calls) == len(handler.reconciliations) == 1
        assert len(calls) == 2
        assert (await client.budget(run.id))["v3"]["counters"]["tool_attempts"] == 2
        await replay(temporal, run.id)


async def test_real_http_mcp_discovery_execution_and_replay(pg_store, monkeypatch):
    from fastmcp import Client as MCPClient

    async with MCPClient("http://localhost:8001/mcp") as mcp:
        tools = await mcp.list_tools()
    remote = next(t for t in tools if t.name == "convert_temperature")
    definition = {
        **registration(),
        "alias": "temperature",
        "handler": "mcp.http",
        "arguments_schema": remote.input_schema,
        "config": {"url": "http://localhost:8001/mcp", "tool": remote.name},
    }
    pg_store.extensions = ExtensionRegistry({"tools": [definition]})
    calls = stub(
        monkeypatch,
        [{"kind": "invoke", "capability": "temperature", "arguments": {"celsius": 100}}, complete()],
    )
    async with backend(pg_store, monkeypatch) as (client, temporal):
        run = await submit(client, ["temperature"])
        result = await client.wait(run.id, timeout=50)
        assert result.status == "completed", result.error
        assert calls[-1]["last_result"]["output"]["data"] == 212.0
        await replay(temporal, run.id)


async def test_cancel_inflight_external_tool_preserves_committed_receipt(pg_store, monkeypatch):
    import asyncio

    from test_harness_extensions import action

    from agent_runtime.general_runtime import general_action

    started, release = asyncio.Event(), asyncio.Event()

    class SlowRead(Lookup):
        async def execute(self, call):
            started.set()
            await release.wait()
            return await super().execute(call)

    handler = SlowRead()
    pg_store.extensions = ExtensionRegistry(
        {"tools": [registration()]}, handlers={"installed.lookup": handler}
    )
    async with backend(pg_store, monkeypatch) as (client, _):
        # The actual workflow is not dispatched for this direct activity cancellation race.
        from agent_runtime.schemas import AgentConfig, RunCreate

        saved = await pg_store.agent(
            AgentConfig(
                name="race",
                provider="fake",
                model="deterministic",
                tools=["customer_lookup"],
                general=GeneralPolicy(),
            )
        )
        run = await pg_store.submit(RunCreate(agent_id=saved.id, input="race"), "race")
        from agent_runtime.db import OutboxRow

        async with pg_store.database.sessions.begin() as db:
            (await db.get(OutboxRow, "start:" + run.id)).delivered = True
        pending = asyncio.create_task(general_action(action(run.id)))
        await asyncio.wait_for(started.wait(), 10)
        await client.cancel(run.id)
        release.set()
        await pending
        assert await pg_store.general_cleanup(run.id)
        final = await client.get(run.id)
        assert final.status == "cancelled" and final.cleanup_state == "complete"
        assert len(handler.calls) == 1


async def test_retained_parallel_duplicate_writes_recover_with_merge_state(pg_store, monkeypatch):
    """Replay the observed duplicate writes, then use durable merge guidance to finish."""
    import base64

    from agent_runtime.schemas import AgentConfig
    from scripts.completion_loop_fixtures import fixture

    f = fixture("parallel")
    counts, parent_contexts = {}, []

    def respond(context, _):
        prompt = context["input"]
        i = counts.get(prompt, 0)
        counts[prompt] = i + 1
        if prompt.startswith("semantic: child"):
            side = "left" if "left" in prompt else "right"
            return (
                complete()
                if i
                else {
                    "kind": "write",
                    "files": [
                        {
                            "path": side + ".txt",
                            "content_base64": base64.b64encode(f["expected"][side + ".txt"]).decode(),
                        }
                    ],
                }
            )
        parent_contexts.append(context)
        duplicate = {
            "kind": "write",
            "files": [
                {"path": p, "content_base64": base64.b64encode(b).decode()} for p, b in f["expected"].items()
            ],
        }
        if i == 0:
            return {
                "kind": "assign",
                "assignments": [
                    {
                        "role": s,
                        "objective": "semantic: child " + s,
                        "acceptance": ["Write exact requested output"],
                        "capabilities": f["tools"],
                        "outputs": [s + ".txt"],
                    }
                    for s in ("left", "right")
                ],
            }
        if i in {1, 4}:
            return duplicate
        if i == 2:
            return {"kind": "join", "children": ["d0", "d1"]}
        if i == 3:
            return {"kind": "merge", "child": "d0"}
        if i == 5:
            assert context["children"]["d0"]["merged"]
            assert not context["children"]["d1"]["merged"]
            assert "do not repeat" in context["last_result"]["feedback"]
            assert context["delegation_state"]["next_action"] == {"kind": "merge", "child": "d1"}
            return context["delegation_state"]["next_action"]
        assert all(c["merged"] for c in context["children"].values())
        assert "delegation_state" not in context
        return {"kind": "complete", "answer": "Both child outputs integrated and checked."}

    stub(monkeypatch, respond)
    async with backend(pg_store, monkeypatch) as (client, temporal):
        agent = await client.create_agent(
            AgentConfig(
                name="retained-parallel",
                provider="fake",
                model="deterministic",
                tools=f["tools"],
                max_tokens=1024,
                general=GeneralPolicy(delegation={"tools": f["tools"]}),
            )
        )
        workspace = await client.workspace_create({})
        run = await client.submit(agent.id, "semantic: " + f["prompt"], workspace=workspace, task=f["task"])
        result = await client.wait(run.id, timeout=90)
        assert result.status == "completed", result.error
        assert len(parent_contexts) == 7
        for path, value in f["expected"].items():
            assert (
                await client.workspace_read(workspace["workspace_id"], result.workspace["revision_id"], path)
                == value
            )
        assert (await client.task(run.id))["assessment"]["accepted"]
        await replay(temporal, run.id)
