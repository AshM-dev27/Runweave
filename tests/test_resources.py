import asyncio
import copy
import json
import time

import httpx
import pytest
import tiktoken
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import FunctionModel
from sqlalchemy import select
from temporalio.exceptions import ApplicationError
from test_general_semantic import complete, http_client, stub, submit

from agent_runtime.client import ClientError
from agent_runtime.general_contracts import GeneralPolicy
from agent_runtime.general_db import GeneralAttemptRow, GeneralOperationRow
from agent_runtime.general_runtime import general_step
from agent_runtime.resources import RequestNotDispatched, ResourceBlocked, check, settle_model_attempt
from agent_runtime.store import Problem


async def charge(store, rid, kind="model_attempts"):
    async with store.database.sessions.begin() as db:
        _, gr, root = await store.general_lock(db, rid)
        await store.general_charge_locked(db, gr, root, kind)


async def child_run(store, client, allocation="shared", resources=True):
    workspace = await client.workspace_create({"a.txt": b"a"})
    run = await submit(
        client,
        ["add"],
        workspace,
        policy=GeneralPolicy(
            resources={"allocation": allocation} if resources else None,
            delegation={"tools": ["add"]},
        ),
    )
    result = await store.general_operation(
        run.id,
        0,
        {
            "action": {
                "kind": "assign",
                "assignments": [
                    {
                        "role": "worker",
                        "objective": "semantic: compute",
                        "criteria": [{"id": "c0", "statement": "compute"}],
                        "tools": ["add"],
                        "base_revision": workspace["revision_id"],
                        "limits": {"model_attempts": 1},
                    }
                ],
            }
        },
    )
    return run.id, result["children"][0]


@pytest.mark.parametrize("allocation", ["shared", "fixed"])
async def test_children_share_actual_spending_and_fixed_limits_remain_explicit(store, allocation):
    async with http_client(store) as client:
        root, child = await child_run(store, client, allocation)
        await charge(store, child)
        if allocation == "shared":
            await charge(store, child)  # May exceed estimate, with room in the root pool.
            assert (await client.resources(root))["usage"]["model_attempts"] == 2
        else:
            with pytest.raises(ResourceBlocked) as blocked:
                await charge(store, child)
            assert blocked.value.snapshot["scope"] == "child"
            assert blocked.value.snapshot["source"] == "assignment.limits"
            await client.update_resources(child, expected_version=1, limits={"model_attempts": 2})
            await charge(store, child)
            assert (await client.resources(root))["version"] == 2


async def test_update_contract_auth_version_idempotency_and_ceiling(store):
    async with http_client(store) as client:
        run = await submit(client, policy=GeneralPolicy(resources={}))
        before = await client.resources(run.id)
        update = dict(expected_version=1, limits={"model_attempts": 30}, idempotency_key="raise-1")
        result = await client.update_resources(run.id, **update)
        assert result["version"] == 2 and result["limits"]["model_attempts"] == 30
        assert result == await client.update_resources(run.id, **update)
        assert before["sources"]["model_attempts"] == "agent.limits"
        assert result["sources"]["model_attempts"] == "authorized_update"
        for version, limits, status in [
            (1, {"model_attempts": 31}, 409),
            (2, {"model_attempts": 29}, 422),
            (2, {"model_attempts": 1025}, 422),
            (2, {"total_tokens": 16001}, 422),
            (2, {"model_attempts": True}, 422),
            (2, {"files": 300}, 422),
        ]:
            with pytest.raises(ClientError) as error:
                await client.update_resources(run.id, expected_version=version, limits=limits)
            assert f"HTTP {status}." in str(error.value)
        with pytest.raises(ClientError) as error:
            await client.update_resources(run.id, **{**update, "limits": {"model_attempts": 31}})
        assert "HTTP 409." in str(error.value)
        from agent_runtime.api import create_app

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(store, "test")), base_url="http://test"
        ) as unauthorized:
            assert (await unauthorized.get(f"/v1/runs/{run.id}/resources")).status_code == 401
            assert (
                await unauthorized.put(
                    f"/v1/runs/{run.id}/resources",
                    json={"expected_version": 2, "limits": {"model_attempts": 40}},
                )
            ).status_code == 401
        await store.general_stop(run.id, "test_done")
        assert result == await client.update_resources(run.id, **update)
        with pytest.raises(ClientError):
            await client.update_resources(run.id, expected_version=2, limits={"model_attempts": 31})


@pytest.mark.parametrize("kind", ["not_dispatched", "wrapped", "unknown", "spoofed"])
async def test_model_reservation_settlement_is_typed_and_exactly_once(store, monkeypatch, kind):
    async def reject(*_):
        if kind == "not_dispatched":
            raise RequestNotDispatched("evaluation_limit")
        if kind == "wrapped":
            raise RuntimeError("sdk wrapper") from RequestNotDispatched("evaluation_limit")
        raise TimeoutError("model_not_dispatched" if kind == "spoofed" else "timeout")

    monkeypatch.setattr("agent_runtime.general_runtime.build_model", lambda _: FunctionModel(reject))
    async with http_client(store) as client:
        run = await submit(client, policy=GeneralPolicy(resources={}))
        with pytest.raises(ApplicationError):
            await general_step({"run_id": run.id, "completion_loop": 2})
        async with store.database.sessions() as db:
            attempts = list(await db.scalars(select(GeneralAttemptRow)))
        assert len(attempts) == 1
        attempt = attempts[0]
        budget = (await client.resources(run.id))["usage"]
        if kind in {"not_dispatched", "wrapped"}:
            assert budget["reserved_tokens"] == budget["model_attempts"] == 0
            assert attempt.data["outcome"] == "not_dispatched" and attempt.data["settled"]
            await settle_model_attempt(store, run.id, attempt.id, refused=RequestNotDispatched())
            assert (await client.resources(run.id))["usage"] == budget
            stub(monkeypatch, [complete()])
            await general_step({"run_id": run.id, "completion_loop": 2})
            assert (await client.resources(run.id))["usage"]["model_attempts"] == 1
        else:
            assert budget["reserved_tokens"] > 0 and budget["model_attempts"] == 1
            assert attempt.data["outcome"] == "dispatch_unknown" and not attempt.data["settled"]


async def test_capacity_wait_does_not_spend_and_releases_on_settlement(store):
    async with http_client(store) as client:
        run = await submit(client, policy=GeneralPolicy(resources={}))
        async with store.database.sessions.begin() as db:
            _, gr, root = await store.general_lock(db, run.id)
            data = copy.deepcopy(root.data)
            data["budget"]["reserved_tokens"] = 15000
            root.data = data
        with pytest.raises(ResourceBlocked) as blocked:
            async with store.database.sessions.begin() as db:
                _, gr, root = await store.general_lock(db, run.id)
                check(root, gr, "total_tokens", 2000)
        assert blocked.value.snapshot["reason"] == "capacity"
        state = await store.resource_pause(run.id, blocked.value.snapshot)
        assert not state.get("retry")
        assert (await client.get(run.id)).status == "paused_budget"
        async with store.database.sessions.begin() as db:
            _, _, root = await store.general_lock(db, run.id, active=False)
            data = copy.deepcopy(root.data)
            data["budget"]["reserved_tokens"] = 0
            root.data = data
        assert (await store.resource_pause(run.id, blocked.value.snapshot))["retry"]
        assert (await client.resources(run.id))["pause"] is None
        assert (await client.resources(run.id))["usage"]["model_attempts"] == 0


async def test_inflight_model_result_survives_another_resource_pause(store, monkeypatch):
    started, finish = asyncio.Event(), asyncio.Event()

    async def response(*_):
        started.set()
        await finish.wait()
        return complete()

    calls = stub(monkeypatch, response)
    async with http_client(store) as client:
        run = await submit(client, policy=GeneralPolicy(resources={}))
        task = asyncio.create_task(general_step({"run_id": run.id, "completion_loop": 2}))
        await asyncio.wait_for(started.wait(), 10)
        block = {
            "resource": "model_attempts",
            "policy_version": 1,
            "run_id": run.id,
            "required": 100,
            "reason": "limit",
        }
        await store.resource_pause(run.id, block)
        finish.set()
        result = await task
        async with store.database.sessions() as db:
            op = await db.get(GeneralOperationRow, f"{run.id}:model:0")
            assert op.data["result"] == result["decision"]
        await client.update_resources(run.id, expected_version=1, limits={"model_attempts": 101})
        assert (await store.resource_pause(run.id, block))["retry"]
        assert await general_step({"run_id": run.id, "completion_loop": 2}) == result
        assert len(calls) == 1


async def test_time_limit_is_configured_and_pause_time_is_not_active_time(store):
    async with http_client(store) as client:
        run = await submit(client, policy=GeneralPolicy(resources={}, limits={"active_seconds": 10}))
        async with store.database.sessions.begin() as db:
            _, _, root = await store.general_lock(db, run.id)
            root.data = {**root.data, "active_started": time.time() - 11}
        with pytest.raises(ResourceBlocked) as blocked:
            await charge(store, run.id)
        assert blocked.value.snapshot["resource"] == "active_seconds"
        await store.resource_pause(run.id, blocked.value.snapshot)
        await client.update_resources(run.id, expected_version=1, limits={"active_seconds": 20})
        assert (await store.resource_pause(run.id, blocked.value.snapshot))["retry"]
        await charge(store, run.id)
        now = time.time()
        sample = {
            "policy": {"limits": {"active_seconds": 100}},
            "active_started": now - 40,
            "paused_seconds": 5,
            "approval_started": now - 10,
            "resource_state": {"pause": {"started": now - 20}},
        }
        assert store.general_time_remaining(sample) == pytest.approx(85, abs=0.01)


@pytest.mark.parametrize("scenario", ["direct", "workspace", "delegation", "child"])
async def test_resource_policy_has_no_extra_model_payload_tokens(store, monkeypatch, scenario):
    payloads = []

    async def respond(messages, info):
        payloads.append(
            (info.instructions or "")
            + json.dumps([p.content for m in messages for p in m.parts], separators=(",", ":"))
            + json.dumps([t.parameters_json_schema for t in info.output_tools], separators=(",", ":"))
        )
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {"action": complete()})])

    monkeypatch.setattr("agent_runtime.general_runtime.build_model", lambda _: FunctionModel(respond))
    async with http_client(store) as client:
        for modern in [False, True]:
            if scenario == "child":
                _, rid = await child_run(store, client, resources=modern)
            else:
                workspace = (
                    await client.workspace_create({"a.txt": b"a"}) if scenario == "workspace" else None
                )
                tools = ["workspace_read"] if workspace else ["add"] if scenario == "delegation" else []
                policy = GeneralPolicy(
                    resources={} if modern else None,
                    delegation={"tools": ["add"]} if scenario == "delegation" else None,
                )
                rid = (await submit(client, tools, workspace, policy=policy)).id
            await general_step({"run_id": rid, "completion_loop": 2})
    enc = tiktoken.get_encoding("o200k_base")
    counts = [len(enc.encode(p, disallowed_special=())) for p in payloads]
    print(json.dumps({"scenario": scenario, "legacy_tokens": counts[0], "shared_tokens": counts[1]}))
    assert counts[1] <= counts[0]
    for private in [
        "max_pause_seconds",
        "expected_version",
        "resource_ceilings",
        "on_limit",
        "resource_state",
    ]:
        assert private not in payloads[1]


async def test_shared_resources_do_not_expand_permissions(store):
    async with http_client(store) as client:
        root, child = await child_run(store, client)
        with pytest.raises(Problem) as error:
            await store.general_operation(
                child,
                0,
                {
                    "action": {
                        "kind": "invoke",
                        "capability": "record_set",
                        "arguments": {"key": "x", "value": "x", "expected_version": 0},
                    }
                },
            )
        assert error.value.status in {403, 404}
        with pytest.raises(ClientError):
            await client.update_resources(child, expected_version=1, limits={"model_attempts": 3})
        assert (await client.resources(root))["version"] == 1


async def test_explicit_resource_configuration_replaces_legacy_allocation_guesses(store):
    async with http_client(store) as client:
        run = await submit(
            client,
            ["workspace_read"],
            policy=GeneralPolicy(
                resources={"finalization": {"total_tokens": 0}},
                limits={"total_tokens": 1024, "model_attempts": 30},
            ),
        )
        snapshot = await client.resources(run.id)
        assert snapshot["limits"]["model_attempts"] == 30
        assert snapshot["finalization_reserve"]["total_tokens"] == 0
        workspace = await client.workspace_create({"x": b"x"})
        root = await submit(client, [], workspace, policy=GeneralPolicy(resources={}, delegation={}))
        result = await store.general_operation(
            root.id,
            0,
            {
                "action": {
                    "kind": "assign",
                    "assignments": [
                        {
                            "role": "worker",
                            "objective": "work",
                            "criteria": [{"id": "c", "statement": "work"}],
                            "tools": [],
                            "base_revision": workspace["revision_id"],
                            "limits": {"model_attempts": 100},
                        }
                    ],
                }
            },
        )
        child = result["children"][0]
        for _ in range(11):
            await charge(store, child)
        with pytest.raises(ResourceBlocked) as error:
            await charge(store, child)
        assert error.value.snapshot["scope"] == "root"
        assert error.value.snapshot["finalization_reserve"] == 1


async def test_completion_preparation_pauses_without_losing_proposal(store, monkeypatch):
    from agent_runtime.general_completion import general_completion
    from agent_runtime.general_runtime import general_action

    calls = stub(monkeypatch, [complete()])
    async with http_client(store) as client:
        run = await submit(client, policy=GeneralPolicy(resources={}))
        proposal = await general_step({"run_id": run.id, "completion_loop": 2})
        block = {
            "resource": "model_attempts",
            "policy_version": 1,
            "run_id": run.id,
            "required": 100,
            "reason": "limit",
        }
        await store.resource_pause(run.id, block)
        with pytest.raises(ApplicationError) as error:
            await general_completion({"run_id": run.id, **proposal})
        assert error.value.message == "resource_limit"
        await client.update_resources(run.id, expected_version=1, limits={"model_attempts": 101})
        await store.resource_pause(run.id, block)
        result = await general_completion({"run_id": run.id, **proposal})
        assert (await general_action(result))["accepted"]
        assert len(calls) == 1


async def test_approval_can_resolve_during_budget_pause_and_remains_authoritative(store):
    async with http_client(store) as client:
        run = await submit(client, ["record_set"], policy=GeneralPolicy(resources={}))
        decision = {
            "action": {
                "kind": "invoke",
                "capability": "record_set",
                "arguments": {"key": "x", "value": "authorized", "expected_version": 0},
            }
        }
        pending = await store.general_operation(run.id, 0, decision)
        block = {
            "resource": "model_attempts",
            "policy_version": 1,
            "run_id": run.id,
            "required": 100,
            "reason": "limit",
        }
        await store.resource_pause(run.id, block)
        await client.decide(run.id, pending["approval"], True)
        assert (await client.get(run.id)).status == "paused_budget"
        await client.update_resources(run.id, expected_version=1, limits={"model_attempts": 101})
        await store.resource_pause(run.id, block)
        result = await store.general_operation(run.id, 0, decision)
        assert result["effect"] == "committed"
        assert result == await store.general_operation(run.id, 0, decision)


async def test_unrelated_increase_and_stale_waiter_do_not_clear_current_pause(store):
    async with http_client(store) as client:
        run = await submit(client, policy=GeneralPolicy(resources={}, limits={"model_attempts": 1}))
        await charge(store, run.id)
        with pytest.raises(ResourceBlocked) as error:
            await charge(store, run.id)
        blocked = error.value.snapshot
        await store.resource_pause(run.id, blocked)
        await client.update_resources(run.id, expected_version=1, limits={"tool_attempts": 49})
        assert not (await store.resource_pause(run.id, blocked)).get("retry")
        state = await client.resources(run.id)
        assert state["pause"]["block"]["policy_version"] == 2
        stale = {**blocked, "resource": "tool_attempts"}
        assert not (await store.resource_pause(run.id, stale)).get("retry")
        await client.update_resources(run.id, expected_version=2, limits={"model_attempts": 2})
        assert (await store.resource_pause(run.id, blocked))["retry"]
        await charge(store, run.id)


async def test_completed_tool_inputs_and_results_support_completion_without_repeating(store, monkeypatch):
    from agent_runtime.general_completion import general_completion
    from agent_runtime.general_runtime import general_action

    def respond(context, _):
        completed = [
            o
            for o in context["observations"]["actions"]
            if o.get("status") == "complete"
            and o["action"].get("capability") == "add"
            and o["action"].get("arguments") == {"a": 5, "b": 7}
        ]
        if completed and context["last_result"] == {"value": 12}:
            return complete()
        return {"kind": "invoke", "capability": "add", "arguments": {"a": 5, "b": 7}}

    calls = stub(monkeypatch, respond)
    async with http_client(store) as client:
        run = await submit(client, ["add"], policy=GeneralPolicy(resources={}))
        first = await general_step({"run_id": run.id, "completion_loop": 2})
        await general_action({"run_id": run.id, **first})
        second = await general_step({"run_id": run.id, "completion_loop": 2})
        assert second["decision"]["action"]["kind"] == "complete"
        prepared = await general_completion({"run_id": run.id, **second})
        assert (await general_action(prepared))["accepted"]
        assert len(calls) == 2
        assert (await client.resources(run.id))["usage"]["tool_attempts"] == 1
