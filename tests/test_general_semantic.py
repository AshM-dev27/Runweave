import inspect
import json
from contextlib import asynccontextmanager

import httpx
import pytest
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import FunctionModel

from agent_runtime.api import create_app
from agent_runtime.client import Client
from agent_runtime.general_completion import general_completion
from agent_runtime.general_contracts import GeneralPolicy
from agent_runtime.general_db import GeneralOperationRow
from agent_runtime.general_runtime import general_action, general_step
from agent_runtime.general_semantic import SemanticDecision, compile_decision
from agent_runtime.schemas import AgentConfig


@asynccontextmanager
async def http_client(store):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(store, "test")),
        base_url="http://test",
        headers={"Authorization": "Bearer test"},
    ) as http:
        yield Client(http_client=http)


def stub(monkeypatch, actions):
    calls = []

    async def respond(messages, info):
        context = json.loads(messages[-1].parts[0].content)
        calls.append(context)
        action = actions(context, len(calls) - 1) if callable(actions) else actions[len(calls) - 1]
        if inspect.isawaitable(action):
            action = await action
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {"action": action})])

    monkeypatch.setattr("agent_runtime.general_runtime.build_model", lambda _: FunctionModel(respond))
    return calls


def complete():
    return {
        "kind": "complete",
        "answer": "12",
        "assessments": [{"criterion": "c0", "disposition": "satisfied", "assessment": "Direct calculation."}],
    }


async def submit(client, tools=(), workspace=None, task=None, policy=None):
    agent = await client.create_agent(
        AgentConfig(
            name="semantic-test",
            provider="fake",
            model="deterministic",
            tools=list(tools),
            general=policy or GeneralPolicy(),
        )
    )
    return await client.submit(
        agent.id, "semantic: compute requested outcome", workspace=workspace, task=task
    )


@pytest.mark.asyncio
async def test_private_adapter_completion_and_reuse(store, monkeypatch, record_property):
    calls = stub(monkeypatch, [complete()])
    async with http_client(store) as client:
        run = await submit(client)
        decision = await general_step({"run_id": run.id, "completion_loop": 2})
        assert decision == await general_step({"run_id": run.id, "completion_loop": 2})
        prepared = await general_completion({"run_id": run.id, **decision})
        assert prepared["final"]
        assert (await general_action(prepared))["accepted"]
        assert (await client.get(run.id)).status == "completed"
        assert len(calls) == 1
        async with store.database.sessions() as db:
            op = await db.get(GeneralOperationRow, f"{run.id}:model:0")
            assert op.data["binding"]["version"] == 3
            assert op.data["context_bytes"] <= 24576
            assert op.data["schema_bytes"] < 12000
            record_property("direct_schema_bytes", op.data["schema_bytes"])
            record_property("direct_context_bytes", op.data["context_bytes"])


@pytest.mark.asyncio
async def test_private_read_source_and_forged_selector(store, monkeypatch):
    actions = [
        {"kind": "read", "path": "source.txt"},
        {"kind": "source", "path": "source.txt", "criterion": "c0", "quote": "verified quote"},
        complete(),
    ]
    stub(monkeypatch, actions)
    async with http_client(store) as client:
        workspace = await client.workspace_create({"source.txt": b"verified quote"})
        run = await submit(
            client,
            ["workspace_read"],
            workspace,
            {
                "outcome": "Read source",
                "criteria": [{"id": "source", "statement": "Cite source", "evidence_policy": "source"}],
            },
        )
        for i in range(3):
            d = await general_step({"run_id": run.id, "completion_loop": 2})
            payload = {"run_id": run.id, **d}
            if i == 2:
                payload = await general_completion(payload)
            result = await general_action(payload)
            if i == 0:
                assert not result.get("verification_ids")
            if i == 1:
                assert result["verification_ids"]
        assert result["accepted"]
        async with store.database.sessions() as db:
            op = await db.get(GeneralOperationRow, f"{run.id}:model:0")
            completion_op = await db.get(GeneralOperationRow, f"{run.id}:model:2")
            repeated = dict(completion_op.data["binding"])
            repeated["evidence"] = repeated["evidence"] * 30
            compiled = compile_decision(SemanticDecision.model_validate({"action": complete()}), repeated)
            assert compiled.action.assessment.criteria[0].evidence_ids == []
            with pytest.raises(Exception, match="invalid_selector"):
                compile_decision(
                    SemanticDecision.model_validate(
                        {
                            "action": {
                                "kind": "source",
                                "path": "source.txt",
                                "criterion": "c99",
                                "quote": "verified quote",
                            }
                        }
                    ),
                    op.data["binding"],
                )


@pytest.mark.asyncio
async def test_capture_retry_preserves_head_and_private_rejection(store, monkeypatch):
    stub(monkeypatch, [{"kind": "write", "files": [{"path": "new.txt", "content_base64": "bmV3"}]}])
    async with http_client(store) as client:
        workspace = await client.workspace_create({"base.txt": b"base"})
        run = await submit(client, ["workspace_write"], workspace)
        original = await general_step({"run_id": run.id, "completion_loop": 2})
        # An explicit authorized action changes the state after capture.
        await general_action(
            {"run_id": run.id, "step": 0, "decision": {"action": {"kind": "discover", "query": ""}}}
        )
        rejected = await general_action({"run_id": run.id, **original})
        # Existing operation identity cannot be rebound to another decision.
        assert rejected.get("capabilities") is not None
        async with store.database.sessions() as db:
            op = await db.get(GeneralOperationRow, f"{run.id}:model:0")
            assert op.data["binding"]["state"]["version"] == 1
            assert op.data["binding"]["state"]["head"] == workspace["revision_id"]
        operations = await client.operations(run.id)
        assert all(o["status"] in {"complete", "pending", "failed"} for o in operations["items"])


@pytest.mark.asyncio
async def test_semantic_assignment_scope_budget_and_replay(store, monkeypatch):
    action = {
        "kind": "assign",
        "assignments": [
            {
                "role": "writer",
                "objective": "Write result",
                "acceptance": ["New output exists"],
                "capabilities": ["workspace_write"],
                "inputs": ["input.txt"],
                "outputs": ["output.txt"],
            }
        ],
    }
    stub(monkeypatch, [action])
    async with http_client(store) as client:
        workspace = await client.workspace_create({"input.txt": b"input"})
        run = await submit(
            client,
            ["workspace_write", "workspace_verify"],
            workspace,
            policy=GeneralPolicy(delegation={"tools": ["workspace_write", "workspace_verify"]}),
        )
        decision = await general_step({"run_id": run.id, "completion_loop": 2})
        assignment = decision["decision"]["action"]["assignments"][0]
        assert assignment["read_prefixes"] == ["input.txt", "output.txt"]
        assert assignment["write_prefixes"] == ["output.txt"]
        assert assignment["criteria"][0]["origin"] == "model"
        result = await general_action({"run_id": run.id, **decision})
        assert result == await general_action({"run_id": run.id, **decision})
        assert len(await client.children(run.id)) == 1
        async with store.database.sessions() as db:
            op = await db.get(GeneralOperationRow, f"{run.id}:model:0")
            binding = op.data["binding"]
            for bad in ["not_installed", "record_note"]:
                action["assignments"][0]["capabilities"] = [bad]
                with pytest.raises(Exception, match="child_grant_denied"):
                    compile_decision(SemanticDecision.model_validate({"action": action}), binding)
            action["assignments"][0]["capabilities"] = ["workspace_write"]
            binding["grants"]["write_prefixes"] = ["elsewhere"]
            with pytest.raises(Exception, match="child_grant_denied"):
                compile_decision(SemanticDecision.model_validate({"action": action}), binding)


@pytest.mark.asyncio
@pytest.mark.parametrize("expected,accepted", [("MTI=", True), ("MTM=", False)])
async def test_automatic_registered_bytes_check(store, monkeypatch, expected, accepted):
    stub(monkeypatch, [complete()])
    async with http_client(store) as client:
        workspace = await client.workspace_create({"out.txt": b"12"})
        run = await submit(
            client,
            ["workspace_verify"],
            workspace,
            {
                "outcome": "Verify result",
                "criteria": [
                    {
                        "id": "result",
                        "statement": "Expected bytes",
                        "evidence_policy": "check",
                        "checks": [{"id": "bytes", "kind": "bytes", "path": "out.txt", "expected": expected}],
                    }
                ],
            },
        )
        decision = await general_step({"run_id": run.id, "completion_loop": 2})
        payload = {"run_id": run.id, **decision}
        check = await general_completion(payload)
        assert not check["final"]
        assert check == await general_completion(payload)
        await general_action(check)
        final = await general_completion(payload)
        assert final["final"]
        result = await general_action(final)
        assert result["accepted"] == accepted
        assert len((await client.verifications(run.id))["items"]) == 1


@pytest.mark.asyncio
async def test_private_context_capture_provider_failure_retry_and_safe_inspection(store, monkeypatch):
    from temporalio.exceptions import ApplicationError

    observed = []

    def response(context, i):
        observed.append(context)
        if i == 0:
            raise RuntimeError("DO_NOT_EXPOSE_PROVIDER_SECRET")
        return complete()

    stub(monkeypatch, response)
    async with http_client(store) as client:
        run = await submit(client)
        with pytest.raises(ApplicationError):
            await general_step({"run_id": run.id, "completion_loop": 2})
        operations = await client.operations(run.id)
        assert operations["items"][0]["status"] == "failed"
        assert "DO_NOT_EXPOSE_PROVIDER_SECRET" not in json.dumps(operations)
        result = await general_step({"run_id": run.id, "completion_loop": 2})
        assert observed[0] == observed[1]
        prepared = await general_completion({"run_id": run.id, **result})
        assert (await general_action(prepared))["accepted"]
        assert (await general_action(prepared))["accepted"]


@pytest.mark.asyncio
async def test_captured_head_cannot_be_repaired_and_phase_invalidates(store, monkeypatch):
    stub(monkeypatch, [complete()])
    async with http_client(store) as client:
        workspace = await client.workspace_create({"a.txt": b"old"})
        run = await submit(client, ["workspace_verify"], workspace)
        decision = await general_step({"run_id": run.id, "completion_loop": 2})
        # Concurrent authorized workspace mutation, deliberately before its checkpoint.
        async with store.database.sessions.begin() as db:
            await store.general_lock(db, run.id)
            await store.branch_commit(db, run.id, {"a.txt": b"changed"}, workspace["revision_id"])
        assert (await general_completion({"run_id": run.id, **decision})) == {"stale": True}
        result = await general_action({"run_id": run.id, **decision})
        assert result["error"] == "stale_decision"
        assert (await client.get(run.id)).status != "completed"


@pytest.mark.parametrize(
    "payload",
    [
        {"kind": "source", "criterion": "c0", "path": "a", "quote": ""},
        {"kind": "read", "path": "a", "criterion_id": "caller"},
        {
            "kind": "assign",
            "assignments": [
                {
                    "role": "x",
                    "objective": "x",
                    "acceptance": [{"id": "runtime.fake", "statement": "x"}],
                    "capabilities": [],
                }
            ],
        },
    ],
)
def test_private_contract_rejects_authority_injection(payload):
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        SemanticDecision.model_validate({"action": payload})


@pytest.mark.asyncio
async def test_child_allocations_preserve_shared_reserves(store, monkeypatch):
    from agent_runtime.general_semantic import capture

    work = {
        "role": "worker",
        "objective": "semantic: work",
        "acceptance": ["Output"],
        "capabilities": ["workspace_write"],
        "outputs": ["a.txt"],
    }
    stub(monkeypatch, [{"kind": "assign", "assignments": [work, {**work, "outputs": ["b.txt"]}]}])
    async with http_client(store) as client:
        workspace = await client.workspace_create({})
        policy = GeneralPolicy(
            resources=None,
            delegation={"tools": ["workspace_write", "workspace_verify"]},
            limits={"model_attempts": 6, "tool_attempts": 8, "command_attempts": 6},
        )
        run = await submit(client, ["workspace_write", "workspace_verify"], workspace, policy=policy)
        decision = await general_step({"run_id": run.id, "completion_loop": 2})
        assignments = decision["decision"]["action"]["assignments"]
        assert sum(a["limits"]["model_attempts"] for a in assignments) == 4
        assert sum(a["limits"]["tool_attempts"] for a in assignments) == 6
        assert sum(a["limits"]["command_attempts"] for a in assignments) == 4
        await general_action({"run_id": run.id, **decision})
        async with store.database.sessions.begin() as db:
            _, gr, root = await store.general_lock(db, run.id)
            binding = await capture(store, db, gr, root, "new-context")
            assert binding["child_available"]["model_attempts"] == 0
            assert binding["child_available"]["tool_attempts"] == 0
            assert binding["child_available"]["command_attempts"] == 0
