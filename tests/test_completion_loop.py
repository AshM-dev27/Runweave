import json

import pytest
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import FunctionModel
from test_general_semantic import complete, http_client, stub, submit

from agent_runtime.general_completion import general_completion
from agent_runtime.general_db import GeneralOperationRow
from agent_runtime.general_history import completed_turn, context
from agent_runtime.general_runtime import general_action, general_step


def test_role_history_and_explicit_truncation():
    history = []
    for _ in range(3):
        history = completed_turn(history, "user" * 300, "assistant")
    value = context(history)
    assert value["omitted_messages"] == 2
    assert [m["role"] for m in value["messages"]] == ["user", "assistant"] * 2
    assert value["messages"][0]["truncated"]


@pytest.mark.asyncio
async def test_multiple_outputs_rejected_before_effects_and_repair(store, monkeypatch):
    observations = []

    async def respond(messages, info):
        observations.append(json.loads(messages[-1].parts[0].content))
        actions = (
            [{"kind": "invoke", "capability": "add", "arguments": {"a": 2, "b": 3}}, complete()]
            if len(observations) == 1
            else [complete()]
        )
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {"action": a}) for a in actions])

    monkeypatch.setattr("agent_runtime.general_runtime.build_model", lambda _: FunctionModel(respond))
    async with http_client(store) as client:
        run = await submit(client, ["add"])
        rejected = await general_step({"run_id": run.id, "completion_loop": 2})
        assert rejected["rejected"]
        operations = (await client.operations(run.id))["items"]
        assert len(operations) == 1  # No action was executed.
        assert operations[0]["diagnostics"]["output_calls"] == 2
        assert "exactly one action" in operations[0]["diagnostics"]["validation_feedback"]
        d = await general_step({"run_id": run.id, "completion_loop": 2})
        assert observations[-1]["last_result"]["error"] == "multiple_semantic_outputs"
        assert (await general_action(await general_completion({"run_id": run.id, **d})))["accepted"]
        assert (await client.budget(run.id))["v3"]["counters"]["model_attempts"] == 2


@pytest.mark.asyncio
async def test_failed_multiple_checks_repair_and_authoritative_resolution(store, monkeypatch):
    calls = stub(
        monkeypatch,
        [complete(), {"kind": "write", "files": [{"path": "a.txt", "content_base64": "MTIK"}]}, complete()],
    )
    async with http_client(store) as client:
        ws = await client.workspace_create({"a.txt": b"wrong", "b.txt": b"20\n"})
        task = {
            "outcome": "Two outputs",
            "constraints": ["Preserve b.txt"],
            "assumptions": ["Offline"],
            "criteria": [
                {
                    "id": "outputs",
                    "statement": "Both exact",
                    "evidence_policy": "check",
                    "checks": [
                        {"id": "a", "kind": "bytes", "path": "a.txt", "expected": "MTIK"},
                        {"id": "b", "kind": "bytes", "path": "b.txt", "expected": "MjAK"},
                    ],
                }
            ],
        }
        run = await submit(client, ["workspace_write", "workspace_verify"], ws, task)
        d = await general_step({"run_id": run.id, "completion_loop": 2})
        payload = {"run_id": run.id, **d}
        assert calls[0]["checks"]["k0"]["expected"] == "MTIK"
        assert calls[0]["criteria"]["c0"]["checks"] == ["k0", "k1"]
        assert calls[0]["constraints"] == ["Preserve b.txt"]
        for _ in range(2):
            await general_action(await general_completion(payload))
        final = await general_completion(payload)
        assert not (await general_action(final))["accepted"]
        d = await general_step({"run_id": run.id, "completion_loop": 2})
        assert any(v["outcome"] == "fail" for v in calls[-1]["verification_state"])
        await general_action({"run_id": run.id, **d})
        # Byte tests stay offline: remove the automatically appended syntax criterion here only.
        # Real sandbox syntax and recovery are covered in the integration test below.
        from agent_runtime.general_db import GeneralRunRow

        async with store.database.sessions.begin() as db:
            gr = await db.get(GeneralRunRow, run.id)
            gr.data = {**gr.data, "goal": {**gr.data["goal"], "criteria": [gr.data["goal"]["criteria"][0]]}}
        d = await general_step({"run_id": run.id, "completion_loop": 2})
        payload = {"run_id": run.id, **d}
        async with store.database.sessions.begin() as db:
            op = await db.get(GeneralOperationRow, f"{run.id}:model:{d['step']}")
            op.data = {**op.data, "binding": {**op.data["binding"], "evidence": [{"id": "fabricated"}]}}
        for _ in range(2):
            check = await general_completion(payload)
            assert not check["final"]
            await general_action(check)
        final = await general_completion(payload)
        ids = final["decision"]["action"]["assessment"]["criteria"][0]["evidence_ids"]
        assert len(ids) == 2 and "fabricated" not in ids
        assert (await general_action(final))["accepted"]


@pytest.mark.asyncio
async def test_child_inherits_constraints_and_config_with_scoped_history(store, monkeypatch):
    from agent_runtime.db import RunRow
    from agent_runtime.general_contracts import GeneralPolicy
    from agent_runtime.general_db import GeneralRunRow

    calls = stub(
        monkeypatch,
        [
            {
                "kind": "assign",
                "assignments": [
                    {
                        "role": "writer",
                        "objective": "semantic: child",
                        "acceptance": ["Write result"],
                        "capabilities": ["workspace_write"],
                        "outputs": ["out.txt"],
                    }
                ],
            },
            complete(),
        ],
    )
    async with http_client(store) as client:
        ws = await client.workspace_create({"private.txt": b"parent-only"})
        run = await submit(
            client,
            ["workspace_write", "workspace_verify"],
            ws,
            {
                "outcome": "Delegate",
                "constraints": ["Keep output concise"],
                "assumptions": ["No network"],
                "criteria": [{"id": "root", "statement": "Output"}],
            },
            GeneralPolicy(delegation={"tools": ["workspace_write", "workspace_verify"]}),
        )
        d = await general_step({"run_id": run.id, "completion_loop": 2})
        result = await general_action({"run_id": run.id, **d})
        cid = result["children"][0]
        async with store.database.sessions() as db:
            parent, child = await db.get(RunRow, run.id), await db.get(RunRow, cid)
            cg = await db.get(GeneralRunRow, cid)
            assert child.config["instructions"] == parent.config["instructions"]
            assert cg.data["goal"]["constraints"] == ["Keep output concise"]
            assert cg.data["goal"]["assumptions"] == ["No network"]
        await general_step({"run_id": cid, "completion_loop": 2})
        assert calls[-1]["previous_turns"] == []
        assert calls[-1]["observations"]["actions"] == []
        assert calls[-1]["files"] == []
        assert calls[-1]["delegation"] is None


@pytest.mark.asyncio
async def test_explicit_uncertainty_is_not_overridden(store, monkeypatch):
    a = complete()
    a["assessments"][0]["disposition"] = "inconclusive"
    stub(monkeypatch, [a])
    async with http_client(store) as client:
        ws = await client.workspace_create({"a": b"12"})
        run = await submit(
            client,
            ["workspace_verify"],
            ws,
            {
                "outcome": "Check",
                "criteria": [
                    {
                        "id": "c",
                        "statement": "Bytes",
                        "evidence_policy": "check",
                        "checks": [{"id": "b", "kind": "bytes", "path": "a", "expected": "MTI="}],
                    }
                ],
            },
        )
        d = await general_step({"run_id": run.id, "completion_loop": 2})
        payload = {"run_id": run.id, **d}
        await general_action(await general_completion(payload))
        final = await general_completion(payload)
        assert final["decision"]["action"]["assessment"]["criteria"][0]["disposition"] == "inconclusive"
        assert not (await general_action(final))["accepted"]


@pytest.mark.asyncio
async def test_actual_capacity_failure_visible_before_model_io(store, monkeypatch):
    from temporalio.exceptions import ApplicationError

    from agent_runtime.general_contracts import GeneralPolicy

    calls = stub(monkeypatch, [complete()])
    async with http_client(store) as client:
        run = await submit(client, policy=GeneralPolicy(resources=None, limits={"total_tokens": 1024}))
        with pytest.raises(ApplicationError, match="budget_exhausted"):
            await general_step({"run_id": run.id, "completion_loop": 2})
        assert calls == []
        op = (await client.operations(run.id))["items"][0]
        assert op["status"] == "failed"
        assert op["diagnostics"]["required_reservation"] > 1024
        assert op["diagnostics"]["message_bytes"] > 0
        assert op["diagnostics"]["schema_bytes"] > 0
        assert not (await client.task(run.id))["assessment"]


@pytest.mark.asyncio
async def test_retry_uses_captured_reporting_mode(store, monkeypatch):
    from temporalio.exceptions import ApplicationError

    calls = []

    def action(context, i):
        calls.append(context)
        if i == 0:
            raise RuntimeError("synthetic unavailable")
        return {"kind": "discover", "query": "add"}

    stub(monkeypatch, action)
    async with http_client(store) as client:
        run = await submit(client, ["add"])
        with pytest.raises(ApplicationError):
            await general_step({"run_id": run.id, "completion_loop": 2})
        async with store.database.sessions.begin() as db:
            _, _, root = await store.general_lock(db, run.id)
            root.data = {**root.data, "budget": {**root.data["budget"], "model_attempts": 11}}
        decision = await general_step({"run_id": run.id, "completion_loop": 2})
        assert calls[0] == calls[1]
        assert not calls[1]["report_only"]
        assert decision["decision"]["action"]["kind"] == "discover"


def test_read_bytes_do_not_expand_observation_history():
    from agent_runtime.general_semantic import observation

    value = observation(
        {"path": "large.txt", "content_base64": "YQ==" * 4000, "text": "preview", "text_truncated": True}
    )
    assert "content_base64" not in value
    assert value["bytes_omitted_from_context"] is True
    assert value["text"] == "preview"


def test_history_keeps_failure_and_successful_repair():
    from types import SimpleNamespace

    from agent_runtime.general_semantic import observed_operations

    results = [
        {"exit_code": 1},
        {"changed": True},
        {"outcome": "pass"},
        {"outcome": "pass"},
        {"outcome": "pass"},
        {"accepted": False},
    ]
    operations = [
        SimpleNamespace(id=str(i), data={"result": result, "sequence": i}) for i, result in enumerate(results)
    ]
    assert [o.id for o in observed_operations(operations)] == ["0", "1", "4", "5"]


@pytest.mark.parametrize(
    "invalid",
    [
        {"action": {"kind": "write", "files": "secret-sentinel"}},
        "{broken secret-sentinel",
        {"action": {"kind": "secret-sentinel"}},
    ],
)
async def test_invalid_sdk_output_repairs_to_accepted_completion(store, monkeypatch, invalid):
    observations = []

    async def respond(messages, info):
        observations.append(json.loads(messages[-1].parts[0].content))
        output = (
            invalid
            if len(observations) == 1
            else {"action": {"kind": "invoke", "capability": "add", "arguments": {"a": 5, "b": 7}}}
            if len(observations) == 2
            else {"action": complete()}
        )
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, output)])

    monkeypatch.setattr("agent_runtime.general_runtime.build_model", lambda _: FunctionModel(respond))
    async with http_client(store) as client:
        run = await submit(client, ["workspace_write", "add"])
        rejected = await general_step({"run_id": run.id, "completion_loop": 2})
        assert rejected["rejected"]
        operations = (await client.operations(run.id))["items"]
        assert len(operations) == 1
        assert "secret-sentinel" not in json.dumps(operations)
        d = await general_step({"run_id": run.id, "completion_loop": 2})
        assert observations[-1]["last_result"]["error"] == "invalid_semantic_output"
        assert "Structural errors" in observations[-1]["last_result"]["feedback"]
        result = await general_action({"run_id": run.id, **d})
        assert not result.get("error")
        d = await general_step({"run_id": run.id, "completion_loop": 2})
        assert (await general_action(await general_completion({"run_id": run.id, **d})))["accepted"]
        assert (await client.budget(run.id))["v3"]["counters"]["model_attempts"] == 3


async def test_repeated_malformed_sdk_output_bounded(store, monkeypatch):
    from temporalio.exceptions import ApplicationError

    stub(monkeypatch, lambda *_: {"kind": "invalid"})
    async with http_client(store) as client:
        run = await submit(client)
        for _ in range(5):
            assert (await general_step({"run_id": run.id, "completion_loop": 2}))["rejected"]
        with pytest.raises(ApplicationError, match="no_progress"):
            await general_step({"run_id": run.id, "completion_loop": 2})
        assert (await client.budget(run.id))["v3"]["counters"]["model_attempts"] == 5
        assert len((await client.operations(run.id))["items"]) == 5


async def test_child_verification_denied_before_any_children(store, monkeypatch):
    from agent_runtime.general_contracts import GeneralPolicy

    stub(
        monkeypatch,
        [
            {
                "kind": "assign",
                "assignments": [
                    {
                        "role": "reader",
                        "objective": "Read",
                        "acceptance": ["Read"],
                        "capabilities": [],
                        "inputs": [],
                        "outputs": [],
                    },
                    {
                        "role": "writer",
                        "objective": "Write",
                        "acceptance": ["Write"],
                        "capabilities": ["workspace_write"],
                        "inputs": [],
                        "outputs": ["new.txt"],
                    },
                ],
            }
        ],
    )
    async with http_client(store) as client:
        ws = await client.workspace_create({"base.txt": b"base"})
        run = await submit(
            client,
            ["workspace_write", "workspace_verify"],
            ws,
            policy=GeneralPolicy(delegation={"tools": ["workspace_write"]}),
        )
        d = await general_step({"run_id": run.id, "completion_loop": 2})
        result = await general_action({"run_id": run.id, **d})
        assert "child_verification_required" in result["error"]
        assert await client.children(run.id) == []
        assert (await client.get(run.id)).workspace["revision_id"] == ws["revision_id"]


@pytest.mark.parametrize("error_kind", ["transport", "provider", "unexpected"])
async def test_provider_failures_are_not_semantic_repair(store, monkeypatch, error_kind):
    from pydantic_ai.exceptions import ModelHTTPError
    from temporalio.exceptions import ApplicationError

    async def respond(messages, info):
        if error_kind == "provider":
            raise ModelHTTPError(401, "stub", "secret-sentinel")
        if error_kind == "transport":
            raise ConnectionError("secret-sentinel")
        raise RuntimeError("secret-sentinel")

    monkeypatch.setattr("agent_runtime.general_runtime.build_model", lambda _: FunctionModel(respond))
    async with http_client(store) as client:
        run = await submit(client)
        with pytest.raises(ApplicationError, match="model_execution_failed"):
            await general_step({"run_id": run.id, "completion_loop": 2})
        state = await store.general(run.id)
        assert state["step"] == 0 and state["last_result"] is None
        operations = (await client.operations(run.id))["items"]
        assert len(operations) == 1
        assert "secret-sentinel" not in json.dumps(operations)


@pytest.mark.parametrize("response_kind", ["text", "unknown", "mixed"])
async def test_nonconforming_sdk_calls_have_no_effects(store, monkeypatch, response_kind):
    from pydantic_ai.messages import TextPart

    async def respond(messages, info):
        parts = (
            [TextPart("secret-sentinel")]
            if response_kind == "text"
            else [ToolCallPart("secret-sentinel", {"action": complete()})]
        )
        if response_kind == "mixed":
            parts.append(ToolCallPart(info.output_tools[0].name, {"action": complete()}))
        return ModelResponse(parts=parts)

    monkeypatch.setattr("agent_runtime.general_runtime.build_model", lambda _: FunctionModel(respond))
    async with http_client(store) as client:
        run = await submit(client)
        assert (await general_step({"run_id": run.id, "completion_loop": 2}))["rejected"]
        operations = (await client.operations(run.id))["items"]
        assert len(operations) == 1
        assert "secret-sentinel" not in json.dumps(operations)
        assert (await client.budget(run.id))["v3"]["counters"]["model_attempts"] == 1
