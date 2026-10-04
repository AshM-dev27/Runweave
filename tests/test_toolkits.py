import hashlib
import io
import json
import zipfile

import httpx
import pytest

from agent_runtime.api import create_app
from agent_runtime.schemas import AgentConfig, RunCreate
from agent_runtime.tool_contracts import SubagentSpec
from agent_runtime.tool_handlers import execute
from agent_runtime.toolkit_runtime import toolkit_step


def config(**kw):
    return AgentConfig(name="kit", provider="fake", model="deterministic", **kw)


async def test_public_artifacts_and_pinned_registry(store):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(store, "test")),
        base_url="http://test",
        headers={"Authorization": "Bearer test"},
    ) as c:
        tools = (await c.get("/v1/tools")).json()
        assert len(tools) == 12 and all(len(t["registration_id"]) == 64 for t in tools)
        upload = await c.post(
            "/v1/artifacts",
            content=b"notice 30 days\n",
            headers={"Idempotency-Key": "a", "Content-Type": "text/plain"},
        )
        assert upload.status_code == 201
        ref = upload.json()
        duplicate = await c.post(
            "/v1/artifacts",
            content=b"notice 30 days\n",
            headers={"Idempotency-Key": "a", "Content-Type": "text/plain"},
        )
        assert duplicate.json()["id"] == ref["id"]
        changed = await c.post(
            "/v1/artifacts",
            content=b"changed",
            headers={"Idempotency-Key": "a", "Content-Type": "text/plain"},
        )
        assert changed.status_code == 409
        downloaded = await c.get("/v1/artifacts/" + ref["id"] + "/content")
        assert hashlib.sha256(downloaded.content).hexdigest() == ref["sha256"]
        assert downloaded.headers["x-content-type-options"] == "nosniff"
        assert (
            await c.get("/v1/artifacts/" + ref["id"] + "/content", headers={"Authorization": "Bearer wrong"})
        ).status_code == 401
        assert (
            await c.post(
                "/v1/artifacts",
                content=b"x" * 262145,
                headers={"Idempotency-Key": "big", "Content-Type": "text/plain"},
            )
        ).status_code == 413
        assert (await c.post("/v1/agents", json=config(tools=["missing"]).model_dump())).status_code == 422
    agent = await store.agent(config(tools=["document_read"]))
    run = await store.submit(RunCreate(agent_id=agent.id, input="read", artifact_ids=[ref["id"]]), "run")
    snapshot = await store.toolkit(run.id)
    store.tools.entries.pop("document_read")
    assert snapshot["tools"]["document_read"]["version"] == 1
    result = await execute(store, run.id, "read", "document_read", {"artifact_id": ref["id"]})
    assert result["evidence"][0]["quote"] == "notice 30 days"
    other = await store.submit(RunCreate(agent_id=(await store.agent(config())).id, input="other"), "other")
    with pytest.raises(Exception):
        await store.artifact(ref["id"], other.id)


async def test_document_diff_and_operation_integrity(store):
    a = await store.upload(b"Notice: 30 days\n", "text/plain", "a.txt", "a")
    b = await store.upload(b"Notice: 60 days\n", "text/plain", "b.txt", "b")
    agent = await store.agent(config(tools=["document_compare"]))
    run = await store.submit(RunCreate(agent_id=agent.id, input="compare", artifact_ids=[a.id, b.id]), "diff")
    args = {"left_id": a.id, "right_id": b.id}
    first = await execute(store, run.id, "diff", "document_compare", args)
    again = await execute(store, run.id, "diff", "document_compare", args)
    assert first == again and len(await store.artifact_list(run.id)) == 3
    assert (await store.budget(run.id))["tool_calls"] == 1
    assert b"-Notice: 30 days" in (await store.artifact(first["artifact"]["id"]))[1]
    with pytest.raises(Exception):
        await execute(store, run.id, "diff", "document_compare", {"left_id": b.id, "right_id": a.id})


@pytest.mark.parametrize("name", ["../escape", "/absolute", "a/../../escape"])
async def test_archive_paths_rejected(store, name):
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as z:
        z.writestr(name, "hello")
    with pytest.raises(Exception):
        await store.upload(data.getvalue(), "application/zip", "repo.zip", "zip")


async def test_snapshot_delegation_and_lifetime_cap(store):
    specialist = await store.agent(config(tools=["document_read"]))
    parent = await store.agent(
        config(
            tools=["delegate"],
            subagents=[SubagentSpec(name="reader", agent_id=specialist.id, description="read")],
        )
    )
    run = await store.submit(RunCreate(agent_id=parent.id, input="delegate"), "parent")
    task = {"specialist": "reader", "instruction": "private task", "artifact_ids": []}
    child = await store.create_child(run.id, "call1", task)
    assert await store.create_child(run.id, "call1", task) == child
    await store.agent(config(tools=["add"]), specialist.id)
    assert (await store.get(child)).config.tools == ["document_read"]
    assert (await store.load(child))["history"] == []
    assert (await store.get(child)).session_id is None
    await store.create_child(run.id, "call2", task)
    with pytest.raises(Exception):
        await store.create_child(run.id, "call3", task)
    with pytest.raises(Exception):
        await store.create_child(child, "nested", task)
    with pytest.raises(Exception):
        await store.create_child(run.id, "call1", {**task, "instruction": "changed"})
    await store.cancel(child)
    assert (await store.get(run.id)).status == "cancelled"
    assert all(c.status == "cancelled" for c in await store.children(run.id))


async def test_fake_step_and_conservative_budget(store):
    a = await store.agent(config(tools=["document_read"], max_requests=1))
    run = await store.submit(
        RunCreate(
            agent_id=a.id,
            input="toolkit:"
            + json.dumps([{"tool": "document_read", "arguments": {"artifact_id": "missing"}}]),
        ),
        "model",
    )
    first = await toolkit_step({"run_id": run.id})
    assert first["calls"][0]["tool"] == "document_read"
    assert (await store.budget(run.id))["requests"] == 1
    with pytest.raises(Exception):
        await toolkit_step({"run_id": run.id})
    assert (await store.budget(run.id))["requests"] == 1


async def test_child_approval_exact_effect_once_and_root_denial(store):
    specialist = await store.agent(config(tools=["record_note"]))
    parent = await store.agent(
        config(
            tools=["delegate"],
            delegation_mode="sequential",
            subagents=[SubagentSpec(name="note", agent_id=specialist.id, description="note")],
        )
    )
    root = await store.submit(RunCreate(agent_id=parent.id, input="notes"), "notes")
    child = await store.create_child(
        root.id, "child", {"specialist": "note", "instruction": "note", "artifact_ids": []}
    )
    await store.awaiting(child, [{"id": "same", "tool": "record_note", "arguments": {"text": "approved"}}])
    pending = await store.get(root.id)
    assert pending.approvals[0].id == child + ":same"
    await store.decide(root.id, pending.approvals[0].id, True)
    await store.resumed(child)
    assert await store.record_note(child, "same", "approved") == "Note recorded"
    assert await store.record_note(child, "same", "approved") == "Note recorded"
    with pytest.raises(Exception):
        await store.record_note(child, "same", "changed")
    other = await store.create_child(
        root.id, "other", {"specialist": "note", "instruction": "note", "artifact_ids": []}
    )
    await store.awaiting(other, [{"id": "same", "tool": "record_note", "arguments": {"text": "denied"}}])
    pending = await store.get(root.id)
    assert pending.approvals[0].id == other + ":same"
    await store.decide(root.id, pending.approvals[0].id, False)
    with pytest.raises(Exception):
        await store.record_note(other, "same", "denied")


async def test_failed_model_attempts_remain_reserved(store):
    from pydantic_ai.messages import ModelRequest, UserPromptPart
    from pydantic_ai.models import ModelRequestParameters
    from pydantic_ai.models.function import FunctionModel

    from agent_runtime.toolkit_runtime import AccountedModel

    calls = []

    async def failure(messages, info):
        calls.append(1)
        raise RuntimeError("synthetic provider failure")

    agent = await store.agent(config(tools=["document_read"], max_requests=2))
    run = await store.submit(RunCreate(agent_id=agent.id, input="fail"), "failed-attempts")
    model = AccountedModel(FunctionModel(failure), run.id, 512)
    for _ in range(3):
        with pytest.raises(Exception):
            await model.request([ModelRequest(parts=[UserPromptPart("test")])], {}, ModelRequestParameters())
    budget = await store.budget(run.id)
    assert len(calls) == 2 and budget["requests"] == 2 and budget["reserved_tokens"] > 0


async def test_backend_absent_fails_closed(monkeypatch):
    from agent_runtime.sandbox import run_python

    monkeypatch.delenv("SANDBOX_BROKER_URL", raising=False)
    monkeypatch.delenv("SANDBOX_BROKER_KEY", raising=False)
    with pytest.raises(ValueError, match="sandbox_unavailable"):
        await run_python("no-backend", "open('/tmp/must-not-run','w').write('x')", b"")


@pytest.mark.parametrize("status", ["queued", "running", "completed"])
async def test_public_legacy_budget_reports_recorded_usage_not_physical_zeros(store, status):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(store, "test")),
        base_url="http://test",
        headers={"Authorization": "Bearer test"},
    ) as client:
        agent = (await client.post("/v1/agents", json=config(tools=["add"]).model_dump())).json()
        response = await client.post(
            "/v1/runs",
            json={"agent_id": agent["id"], "input": "add 2 and 3"},
            headers={"Idempotency-Key": status},
        )
        assert response.status_code == 202
        run_id = response.json()["id"]
        usage = {}
        if status != "queued":
            await store.load(run_id)
        if status == "completed":
            usage = {"requests": 2, "tool_calls": 1, "input_tokens": 17, "output_tokens": 9}
            await store.finish(run_id, "completed", output={"answer": "5", "value": 5}, usage=usage)
        public = (await client.get(f"/v1/runs/{run_id}")).json()
        response = await client.get(f"/v1/runs/{run_id}/budget")
        assert response.status_code == 200
        budget = response.json()
        assert public["status"] == status
        assert budget["execution_version"] == 1
        assert budget["accounting_mode"] == "recorded_successful_usage"
        assert budget["successful_usage"] == public["usage"] == usage
        for field in ("requests", "tool_calls", "reported_tokens", "reserved_tokens", "max_total_tokens"):
            assert budget[field] is None
        assert budget["max_requests"] == public["config"]["max_requests"]
        assert budget["max_tool_calls"] == public["config"]["max_tool_calls"]
        assert (await client.get("/v1/runs/missing/budget")).status_code == 404


async def test_public_v2_budget_uses_root_ledger_for_children(store):
    specialist = await store.agent(config(tools=["add"]))
    parent = await store.agent(
        config(
            tools=["delegate"],
            subagents=[SubagentSpec(name="math", agent_id=specialist.id, description="math")],
        )
    )
    root = await store.submit(RunCreate(agent_id=parent.id, input="delegate"), "budget-root")
    child = await store.create_child(
        root.id, "child", {"specialist": "math", "instruction": "add", "artifact_ids": []}
    )
    await store.reserve_usage(root.id, "requests", 1000)
    await store.reserve_usage(child, "requests", 500)
    await store.reserve_usage(child, "tool_calls")
    await store.reconcile_usage(root.id, 1000, 120)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(store, "test")),
        base_url="http://test",
        headers={"Authorization": "Bearer test"},
    ) as client:
        budgets = []
        for run_id in (root.id, child):
            response = await client.get(f"/v1/runs/{run_id}/budget")
            assert response.status_code == 200
            budgets.append(response.json())
        assert budgets[0] == budgets[1]
        assert budgets[0] == {
            "execution_version": 2,
            "accounting_mode": "shared_ledger",
            "requests": 2,
            "tool_calls": 1,
            "reported_tokens": 120,
            "reserved_tokens": 500,
            "max_requests": root.config.max_requests,
            "max_tool_calls": root.config.max_tool_calls,
            "max_total_tokens": (await store.toolkit(root.id))["budget"]["max_total_tokens"],
            "successful_usage": None,
            "adaptive": (await store.toolkit(root.id))["adaptive"],
        }


@pytest.mark.parametrize("configured", [False, True])
async def test_public_tool_availability_does_not_infer_external_health(store, monkeypatch, configured):
    for key in ("MCP_URL", "SANDBOX_BROKER_URL"):
        if configured:
            monkeypatch.setenv(key, "http://127.0.0.1:1/unreachable")
        else:
            monkeypatch.delenv(key, raising=False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(store, "test")),
        base_url="http://test",
        headers={"Authorization": "Bearer test"},
    ) as client:
        response = await client.get("/v1/tools")
        assert response.status_code == 200
        for descriptor in response.json():
            expected = (
                "unverified"
                if descriptor["alias"] in {"convert_temperature", "csv_analyze", "python_analyze"}
                else "available"
            )
            assert descriptor["availability"] == expected
            detail = await client.get("/v1/tools/" + descriptor["alias"])
            assert detail.status_code == 200 and detail.json() == descriptor
