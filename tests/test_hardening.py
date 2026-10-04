"""Explicit budgets, useful receipts, batch checks and audited recovery contracts."""

import json

import pytest
from temporalio.exceptions import ApplicationError
from test_general_semantic import complete, http_client, stub, submit
from test_harness_extensions import Lookup, Writer, action, create, registration
from test_resources import charge

from agent_runtime.client import ClientError
from agent_runtime.extensions import ExtensionRegistry
from agent_runtime.general_completion import general_completion
from agent_runtime.general_contracts import GeneralPolicy
from agent_runtime.general_runtime import general_action, general_step
from agent_runtime.reconciliation import reconcile_operation
from agent_runtime.resources import LEGACY_DEFAULTS, ResourceBlocked


async def test_omitted_budgets_use_inspectable_ceiling_and_persist_nulls(store):
    async with http_client(store) as client:
        run = await submit(client)
        resources = await client.resources(run.id)
        assert resources["task_limits"] == dict.fromkeys(LEGACY_DEFAULTS)
        assert resources["limits"]["model_attempts"] == 64
        assert resources["sources"]["model_attempts"] == "operator.adaptive_ceilings"
        assert resources["sources"]["total_tokens"] == "model.registration"
        assert resources["allocation"] == "shared"
        for _ in range(13):
            await charge(store, run.id)
        assert (await client.resources(run.id))["usage"]["model_attempts"] == 13
        async with store.database.sessions.begin() as db:
            _, gr, root = await store.general_lock(db, run.id)
            from agent_runtime.resources import check

            with pytest.raises(ResourceBlocked) as error:
                check(root, gr, "model_attempts", 1024)
            assert error.value.snapshot["limit_type"] == "ceiling"
            assert error.value.snapshot["source"] == "operator.adaptive_ceilings"


async def test_explicit_budget_and_legacy_config_remain_distinct(store):
    async with http_client(store) as client:
        run = await submit(client, policy=GeneralPolicy(limits={"model_attempts": 1}))
        await charge(store, run.id)
        with pytest.raises(ResourceBlocked) as error:
            await charge(store, run.id)
        assert error.value.snapshot["limit_type"] == "budget"
        assert error.value.snapshot["source"] == "agent.limits"
        legacy = GeneralPolicy.model_validate({"resources": None})
        assert all(getattr(legacy.limits, k) == v for k, v in LEGACY_DEFAULTS.items())
        old = await submit(client, policy=legacy)
        assert (await store.general(old.id))["policy"]["resources"] is None
        for _ in range(12):
            await charge(store, old.id)
        from agent_runtime.store import Problem

        with pytest.raises(Problem, match="budget_exhausted"):
            await charge(store, old.id)


async def test_generic_tool_receipts_keep_inputs_and_are_bounded(store, monkeypatch):
    from agent_runtime.general_semantic import argument_observation

    store.extensions = ExtensionRegistry({"tools": [registration()]}, handlers={"installed.lookup": Lookup()})
    calls = stub(monkeypatch, [complete()])
    async with http_client(store) as client:
        run = await submit(client, ["customer_lookup"])
        await general_action(action(run.id, args={"customer": "different customer"}))
        await general_step({"run_id": run.id, "completion_loop": 2})
        receipt = calls[-1]["observations"]["actions"][0]
        assert receipt["action"]["arguments"] == {"customer": "different customer"}
        assert receipt["status"] == "complete"
        assert argument_observation({"large": "x" * 2000})["omitted"]
        assert len(json.dumps(argument_observation({"large": "x" * 2000}))) < 200


async def run_checks(run, first):
    while True:
        prepared = await general_completion({"run_id": run.id, **first})
        if prepared.get("verification_done"):
            return prepared
        result = await general_action(prepared)
        assert not result.get("command")
        if prepared["final"]:
            return result


@pytest.mark.parametrize("correct", [True, False])
async def test_batch_file_checks_use_one_model_call_and_do_not_approve_failures(store, monkeypatch, correct):
    calls = stub(monkeypatch, [{"kind": "verify", "check": "all"}, complete()])
    async with http_client(store) as client:
        workspace = await client.workspace_create({"left.txt": b"12\n", "right.txt": b"20\n"})
        task = {
            "outcome": "Check both files",
            "criteria": [
                {
                    "id": "files",
                    "statement": "Exact contents",
                    "evidence_policy": "check",
                    "checks": [
                        {"id": "left", "kind": "bytes", "path": "left.txt", "expected": "MTIK"},
                        {
                            "id": "right",
                            "kind": "bytes",
                            "path": "right.txt",
                            "expected": "MjAK" if correct else "YmFk",
                        },
                    ],
                }
            ],
        }
        run = await submit(client, ["workspace_verify"], workspace, task)
        first = await general_step({"run_id": run.id, "completion_loop": 2, "batch_checks": True})
        assert (await run_checks(run, first))["verification_done"]
        assert (await run_checks(run, first))["verification_done"]  # activity retry
        assert len(calls) == 1
        checks = (await client.verifications(run.id))["items"]
        assert len(checks) == 2 and sum(c["outcome"] == "pass" for c in checks) == (2 if correct else 1)
        second = await general_step({"run_id": run.id, "completion_loop": 2, "batch_checks": True})
        assert calls[-1]["last_result"]["action"] == {"kind": "verify", "check": "all"}
        assert calls[-1]["last_result"]["status"] == "complete"
        assert calls[-1]["last_result"]["outcome"] == ("pass" if correct else "fail")
        final = await run_checks(run, second)
        assert bool(final.get("accepted")) is correct
        assert len(calls) == 2
        assert len((await client.verifications(run.id))["items"]) == 2  # current receipts reused


async def unknown_model(store, monkeypatch, client):
    async def response(*_):
        raise TimeoutError("do not expose")

    stub(monkeypatch, response)
    run = await submit(client)
    with pytest.raises(ApplicationError):
        await general_step({"run_id": run.id, "completion_loop": 2})
    recovery = await client.recovery(run.id)
    return run, recovery["unresolved"][0]


async def test_model_recovery_auth_idempotency_and_exactly_once_accounting(store, monkeypatch):
    async with http_client(store) as client:
        run, target = await unknown_model(store, monkeypatch, client)
        request = {
            **{k: target[k] for k in ["target_id", "kind"]},
            "reported_tokens": 321,
            "evidence_ref": "receipt/test-123",
        }
        with pytest.raises(ClientError, match="409"):
            await client.reconcile(run.id, request)
        await client.cancel(run.id)
        endpoint = f"/v1/runs/{run.id}/reconciliations"
        assert (
            await client.http.post(
                endpoint, headers={"Authorization": "Bearer wrong", "Idempotency-Key": "x"}, json=request
            )
        ).status_code == 401
        assert (
            await client.http.post(
                endpoint, headers={"Idempotency-Key": "x"}, json={**request, "reported_tokens": True}
            )
        ).status_code == 422
        submitted = await client.reconcile(run.id, request, idempotency_key="same")
        assert submitted == await client.reconcile(run.id, request, idempotency_key="same")
        with pytest.raises(ClientError, match="409"):
            await client.reconcile(run.id, {**request, "reported_tokens": 322}, idempotency_key="same")
        with pytest.raises(ClientError, match="409"):
            await client.reconcile(run.id, request, idempotency_key="second")
        result = await reconcile_operation({"run_id": run.id, "id": submitted["id"]})
        assert result["result"]["source"] == "operator_attestation"
        assert result == await reconcile_operation({"run_id": run.id, "id": submitted["id"]})
        usage = (await client.resources(run.id))["usage"]
        assert (
            usage["reserved_tokens"] == 0 and usage["reported_tokens"] == 321 and usage["model_attempts"] == 1
        )
        assert (await client.get(run.id)).status == "cancelled"
        assert not (await client.recovery(run.id))["unresolved"]
        with pytest.raises(ClientError, match="409"):
            await client.reconcile(run.id, request, idempotency_key="third")
        events = [e for e in await store.events(run.id) if e.type == "reconciliation.completed"]
        assert len(events) == 1
        assert "do not expose" not in json.dumps(
            [e.model_dump(mode="json") for e in await store.events(run.id)]
        )


class DeferredWriter(Writer):
    available = False

    async def reconcile(self, call):
        self.reconciliations.append(call.idempotency_key)
        return self.effects.get(call.idempotency_key) if self.available else None


async def unknown_write(store, client, handler):
    store.extensions = ExtensionRegistry(
        {"tools": [{**registration(True), "reconciliation": "lookup"}]},
        handlers={"installed.lookup": handler},
    )
    run = await create(client)
    pending = await general_action(action(run.id))
    await client.decide(run.id, pending["approval"], True)
    with pytest.raises(ApplicationError, match="extension_reconcile_pending"):
        await general_action(action(run.id))
    assert (await general_action(action(run.id)))["error"] == "extension_outcome_unknown"
    await client.cancel(run.id)
    return run


async def test_external_recovery_uses_pinned_identity_never_executes_again(store):
    handler = DeferredWriter()
    async with http_client(store) as client:
        run = await unknown_write(store, client, handler)
        target = (await client.recovery(run.id))["unresolved"][0]
        first = await client.reconcile(run.id, target)
        unknown = await reconcile_operation({"run_id": run.id, "id": first["id"]})
        assert unknown["result"]["outcome"] == "unknown"
        assert (await client.get(run.id)).cleanup_state == "pending"
        handler.available = True
        second = await client.reconcile(run.id, target)
        result = await reconcile_operation({"run_id": run.id, "id": second["id"]})
        assert result["result"]["outcome"] == "resolved"
        assert len(handler.calls) == 1
        assert set(handler.reconciliations) == {target["target_id"]}
        assert await reconcile_operation({"run_id": run.id, "id": second["id"]}) == result
        assert (await client.get(run.id)).status == "cancelled"
        await store.general_cleanup(run.id)
        assert (await client.get(run.id)).cleanup_state == "complete"
        assert not (await client.recovery(run.id))["unresolved"]


@pytest.mark.parametrize("model", ["gpt-4.1-mini", "gpt-5.6-luna"])
@pytest.mark.parametrize("followup", [False, True])
async def test_paid_guard_pins_model_reasoning_and_stops_failed_transport(tmp_path, model, followup):
    import httpx

    from agent_runtime.model_adapter import request_context
    from agent_runtime.resources import RequestNotDispatched
    from scripts.hardening_budget import Transport, policy

    if followup:
        from scripts.hardening_followup_budget import policy

    guard = policy(model)
    scenario = next(iter(guard.MANIFEST["scenario_limits"]))
    ledger = tmp_path / "guard.sqlite"
    guard.initialize(ledger)
    guard.admit(ledger, scenario, "root")
    sent = []

    async def fake(request):
        sent.append(request)
        return httpx.Response(500, json={"error": "fixture"})

    context = request_context.set(
        dict(scenario=scenario, root_id="root", run_id="root", operation_id="operation", attempt_id="attempt")
    )
    body = {"model": model, "max_output_tokens": 1024}
    if guard.MANIFEST["reasoning"]:
        body["reasoning"] = {"effort": guard.MANIFEST["reasoning"]}
    try:
        async with httpx.AsyncClient(transport=Transport(ledger, guard, httpx.MockTransport(fake))) as client:
            with pytest.raises(RequestNotDispatched):
                await client.post(guard.MANIFEST["endpoint"], json={**body, "model": "unselected"})
            with pytest.raises(RequestNotDispatched):
                await client.post(guard.MANIFEST["endpoint"], json={**body, "max_output_tokens": 2048})
            assert guard.validate(ledger) == 0
            assert (await client.post(guard.MANIFEST["endpoint"], json=body)).status_code == 500
            with pytest.raises(RequestNotDispatched):
                await client.post(guard.MANIFEST["endpoint"], json=body)
        assert len(sent) == guard.validate(ledger) == 1
        guard.finish(ledger, scenario, "failed")
        with pytest.raises(RuntimeError):
            guard.admit(ledger, scenario, "replacement")
    finally:
        request_context.reset(context)


async def test_recovery_does_not_accept_another_runs_attempt_or_active_lease(store, monkeypatch):
    import time

    from agent_runtime.general_db import GeneralAttemptRow, GeneralOperationRow

    async with http_client(store) as client:
        run, target = await unknown_model(store, monkeypatch, client)
        other = await submit(client)
        await client.cancel(run.id)
        await client.cancel(other.id)
        body = {
            "kind": "model_usage",
            "target_id": target["target_id"],
            "reported_tokens": 10,
            "evidence_ref": "receipt/10",
        }
        with pytest.raises(ClientError, match="404"):
            await client.reconcile(other.id, body)
        async with store.database.sessions.begin() as db:
            attempt = await db.get(GeneralAttemptRow, target["target_id"])
            op = await db.get(GeneralOperationRow, attempt.operation_id)
            op.data = {**op.data, "lease": time.time() + 30}
        with pytest.raises(ClientError, match="409"):
            await client.reconcile(run.id, body)
        assert (await client.resources(run.id))["usage"]["reserved_tokens"] == target["reserved_tokens"]


async def test_closed_recovery_workflow_releases_only_the_recovery_lock(store, monkeypatch):
    from types import SimpleNamespace

    from temporalio.client import WorkflowExecutionStatus

    from agent_runtime.dispatch import Dispatcher

    async with http_client(store) as client:
        run, target = await unknown_model(store, monkeypatch, client)
        await client.cancel(run.id)
        body = {
            "kind": "model_usage",
            "target_id": target["target_id"],
            "reported_tokens": 10,
            "evidence_ref": "receipt/10",
        }
        first = await client.reconcile(run.id, body)

        async def describe():
            return SimpleNamespace(status=WorkflowExecutionStatus.TIMED_OUT)

        temporal = SimpleNamespace(get_workflow_handle=lambda _: SimpleNamespace(describe=describe))
        await Dispatcher(store, temporal, "unused").reconcile_operator_recovery()
        result = await client.reconciliation(run.id, first["id"])
        assert result["result"] == {"outcome": "unknown", "source": "recovery_interrupted"}
        assert (await client.resources(run.id))["usage"]["reserved_tokens"] == target["reserved_tokens"]
        second = await client.reconcile(run.id, body)
        assert second["id"] != first["id"]


async def test_no_pending_checks_are_not_offered_and_completion_rejects_state_change(store, monkeypatch):
    from agent_runtime.general_db import GeneralRunRow

    calls = stub(monkeypatch, [complete()])
    async with http_client(store) as client:
        run = await submit(client, ["workspace_verify"])
        step = await general_step({"run_id": run.id, "completion_loop": 2, "batch_checks": True})
        assert len(calls) == 1
        async with store.database.sessions.begin() as db:
            gr = await db.get(GeneralRunRow, run.id)
            gr.data = {
                **gr.data,
                "task_state": {**gr.data["task_state"], "version": gr.data["task_state"]["version"] + 1},
            }
        assert (await general_completion({"run_id": run.id, **step}))["stale"]
        assert (await client.get(run.id)).status != "completed"


async def test_terminal_recovery_rejects_handlers_that_may_retry_writes(store):
    from agent_runtime.extensions import ExtensionRegistry
    from agent_runtime.general_db import GeneralRunRow

    handler = DeferredWriter()
    async with http_client(store) as client:
        run = await unknown_write(store, client, handler)
        async with store.database.sessions.begin() as db:
            gr = await db.get(GeneralRunRow, run.id)
            import copy

            data = copy.deepcopy(gr.data)
            data["tools"]["customer_lookup"]["extension"]["reconciliation"] = "retry"
            gr.data = data
        with pytest.raises(ClientError, match="409"):
            await client.reconcile(run.id, (await client.recovery(run.id))["unresolved"][0])
        assert len(handler.calls) == 1
    definition = {
        **registration(True),
        "handler": "mcp.http",
        "reconciliation": "lookup",
        "config": {"url": "http://example.invalid/mcp", "tool": "write", "idempotency_argument": "key"},
    }
    with pytest.raises(ValueError, match="lookup-only"):
        ExtensionRegistry({"tools": [definition]})


async def test_small_registration_does_not_turn_unused_finalization_reserve_into_budget(store):
    entry = store.registry.entries["fake", "deterministic"]
    store.registry.entries["fake", "deterministic"] = entry.model_copy(
        update={"total_tokens_limit": 2048, "max_output_tokens": 1024}
    )
    async with http_client(store) as client:
        run = await submit(client)
        resources = await client.resources(run.id)
        assert resources["task_limits"]["total_tokens"] is None
        assert resources["limits"]["total_tokens"] == 2048
        assert resources["sources"]["total_tokens"] == "model.registration"


@pytest.mark.parametrize("value", [True, "12", 1.5])
def test_declared_compute_budgets_require_integer_values(value):
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        GeneralPolicy(limits={"model_attempts": value})


async def test_workspace_receipt_freshness_tracks_result_revision(store, monkeypatch):
    from agent_runtime.project_store import digest

    calls = stub(monkeypatch, [complete(), complete()])
    async with http_client(store) as client:
        workspace = await client.workspace_create({})
        run = await submit(client, ["workspace_write", "workspace_verify"], workspace)

        async def write(step, head, expected, content):
            return await general_action(
                {
                    "run_id": run.id,
                    "step": step,
                    "decision": {
                        "action": {
                            "kind": "invoke",
                            "capability": "workspace_write",
                            "arguments": {
                                "expected_revision": head,
                                "writes": [
                                    {"path": "x.txt", "expected_sha256": expected, "content_base64": content}
                                ],
                            },
                        }
                    },
                }
            )

        first = await write(0, workspace["revision_id"], None, "b25l")
        await general_step({"run_id": run.id, "completion_loop": 2, "batch_checks": True})
        assert calls[-1]["observations"]["actions"][0]["action"]["revision_current"] is True
        await write(1, first["revision_id"], digest(b"one"), "dHdv")
        await general_step({"run_id": run.id, "completion_loop": 2, "batch_checks": True})
        assert [o["action"]["revision_current"] for o in calls[-1]["observations"]["actions"]] == [
            False,
            True,
        ]


async def test_legacy_bindings_cannot_request_batch_verification(store):
    from agent_runtime.general_semantic import SemanticDecision, capture, compile_decision
    from agent_runtime.store import Problem

    async with http_client(store) as client:
        run = await submit(client, ["workspace_verify"])
        async with store.database.sessions.begin() as db:
            _, gr, root = await store.general_lock(db, run.id)
            binding = await capture(store, db, gr, root, "legacy", version=3)
        with pytest.raises(Problem, match="invalid_selector"):
            compile_decision(SemanticDecision(action={"kind": "verify", "check": "all"}), binding)


async def test_child_write_scope_is_explicit_before_starting_children(store, monkeypatch):
    from pydantic import ValidationError

    from agent_runtime.general_db import GeneralOperationRow
    from agent_runtime.general_semantic import SemanticDecision, compile_decision, wire_type
    from agent_runtime.store import Problem

    stub(monkeypatch, [complete()])
    async with http_client(store) as client:
        workspace = await client.workspace_create({})
        run = await submit(
            client,
            ["workspace_write"],
            workspace,
            policy=GeneralPolicy(delegation={"tools": ["workspace_write"]}),
        )
        await general_step({"run_id": run.id, "completion_loop": 2, "batch_checks": True})
        async with store.database.sessions() as db:
            model = await db.get(GeneralOperationRow, run.id + ":model:0")
            binding = model.data["binding"]
        action = {
            "kind": "assign",
            "assignments": [
                {
                    "role": "writer",
                    "objective": "Write a.txt",
                    "acceptance": ["a.txt contains a"],
                    "capabilities": ["workspace_write"],
                }
            ],
        }
        wire = wire_type(binding)
        with pytest.raises(ValidationError):
            wire.model_validate({"action": action})
        action["assignments"][0]["outputs"] = []
        with pytest.raises(Problem, match="child_output_scope_required"):
            compile_decision(SemanticDecision(action=action), binding)
        action["assignments"][0]["outputs"] = ["a.txt"]
        decision = compile_decision(wire.model_validate({"action": action}), binding)
        assert decision.action.assignments[0].write_prefixes == ["a.txt"]
        assert decision.action.assignments[0].read_prefixes == ["a.txt"]
        assert await client.children(run.id) == []
        # An empty scope remains valid for work without a workspace writer.
        action["assignments"][0].update(capabilities=[], outputs=[])
        compile_decision(wire.model_validate({"action": action}), binding)


async def test_verification_choices_return_after_workspace_changes(store, monkeypatch):
    from pydantic import ValidationError

    from agent_runtime.general_db import GeneralOperationRow
    from agent_runtime.general_semantic import wire_type

    calls = stub(
        monkeypatch,
        [
            {"kind": "verify", "check": "all"},
            {"kind": "write", "files": [{"path": "a.txt", "content_base64": "Yg=="}]},
            {"kind": "verify", "check": "all"},
        ],
    )
    async with http_client(store) as client:
        workspace = await client.workspace_create({"a.txt": b"a"})
        task = {
            "outcome": "Check a",
            "criteria": [
                {
                    "id": "file",
                    "statement": "a.txt is a",
                    "evidence_policy": "check",
                    "checks": [{"id": "a", "kind": "bytes", "path": "a.txt", "expected": "YQ=="}],
                }
            ],
        }
        run = await submit(client, ["workspace_verify", "workspace_write"], workspace, task)
        first = await general_step({"run_id": run.id, "completion_loop": 2, "batch_checks": True})
        await run_checks(run, first)
        write = await general_step({"run_id": run.id, "completion_loop": 2, "batch_checks": True})
        async with store.database.sessions() as db:
            operation = await db.get(GeneralOperationRow, run.id + ":model:" + str(write["step"]))
            wire = wire_type(operation.data["binding"])
            with pytest.raises(ValidationError):
                wire.model_validate({"action": {"kind": "verify", "check": "all"}})
        assert (await general_action({"run_id": run.id, **write}))["changed"]
        again = await general_step({"run_id": run.id, "completion_loop": 2, "batch_checks": True})
        assert again["decision"]["action"]["arguments"]["check_id"] == "$pending"
        assert all(c["status"] == "pending" for c in calls[-1]["checks"].values())
