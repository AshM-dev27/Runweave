"""Continuation receipts, bounded-call guidance and fresh-read behavior through the public API."""

import copy
from types import SimpleNamespace

import pytest
from test_general_semantic import complete, http_client, stub, submit
from test_harness_extensions import Lookup, action, registration

from agent_runtime.extensions import ExtensionRegistry
from agent_runtime.general_completion import general_completion
from agent_runtime.general_contracts import GeneralPolicy
from agent_runtime.general_db import GeneralOperationRow
from agent_runtime.general_progress import capture_progress
from agent_runtime.general_runtime import general_action, general_step
from agent_runtime.general_semantic import project_context


async def test_continuation_identifies_completed_reads_and_keeps_final_call(store, monkeypatch):
    handler = Lookup()
    store.extensions = ExtensionRegistry({"tools": [registration()]}, handlers={"installed.lookup": handler})
    calls = stub(
        monkeypatch,
        [
            {"kind": "invoke", "capability": "customer_lookup", "arguments": {"customer": "one"}},
            {"kind": "invoke", "capability": "customer_lookup", "arguments": {"customer": "one"}},
            complete(),
        ],
    )
    async with http_client(store) as client:
        run = await submit(client, ["customer_lookup"], policy=GeneralPolicy(limits={"model_attempts": 3}))
        for index in range(3):
            decision = await general_step({"run_id": run.id, "completion_loop": 2})
            payload = {"run_id": run.id, **decision}
            if index == 2:
                payload = await general_completion(payload)
            result = await general_action(payload)
            # A workflow retry consumes no extra tool effect or checkpoint.
            assert await general_action(payload) == result
        assert (await client.get(run.id)).outcome == "succeeded"
        assert len(handler.calls) == 2  # Fresh reads remain possible; no blanket result cache.
        assert [c["execution_progress"]["model_calls_after_this"] for c in calls] == [2, 1, 0]
        assert calls[1]["execution_progress"]["repeated_read_count"] == 1
        assert calls[2]["execution_progress"]["repeated_read_count"] == 2
        assert calls[2]["execution_progress"]["last_action"]["operation"] == "o1"
        assert calls[2]["execution_progress"]["read_result_in_last_result"]
        assert calls[2]["last_result"]["output"] == {"customer": "one", "plan": "basic"}
        assert calls[2]["last_result"]["untrusted"]
        assert not any(c["report_only"] for c in calls)  # Explicit resource policy keeps its semantics.
        async with store.database.sessions() as db:
            op = await db.get(GeneralOperationRow, run.id + ":model:2")
            old_binding = copy.deepcopy(op.data["binding"])
        old_binding.pop("execution_progress")
        assert "execution_progress" not in project_context(old_binding, "task", [], False)


async def test_changing_poll_result_is_not_mistaken_for_duplicate_read(store, monkeypatch):
    class Poll(Lookup):
        async def execute(self, call):
            self.calls.append(call)
            return {"status": "ready" if len(self.calls) == 2 else "pending"}

    handler = Poll()
    store.extensions = ExtensionRegistry({"tools": [registration()]}, handlers={"installed.lookup": handler})
    calls = stub(monkeypatch, [complete()])
    async with http_client(store) as client:
        run = await submit(client, ["customer_lookup"])
        await general_action(action(run.id))
        await general_action(action(run.id, step=1))
        await general_step({"run_id": run.id, "completion_loop": 2})
        assert len(handler.calls) == 2
        assert calls[0]["execution_progress"]["repeated_read_count"] == 1
        assert calls[0]["last_result"]["output"]["status"] == "ready"


@pytest.mark.parametrize("change", ["arguments", "registration", "result", "failed", "pending", "write"])
def test_read_streak_requires_matching_successful_read_receipts(change):
    result = {"output": {"value": 17}, "untrusted": True}
    entry = {"effect": {"kind": "read"}}
    gr = SimpleNamespace(parent_id=None, data={"step": 2, "tools": {"lookup": entry}, "last_result": result})
    root = SimpleNamespace(
        data={"policy": {"limits": {"model_attempts": 3}}, "budget": {"model_attempts": 2}}
    )
    record = {
        "decision": {"action": {"kind": "invoke", "capability": "lookup", "arguments": {"id": "one"}}},
        "registration_id": "v1",
        "status": "complete",
        "result": result,
    }
    first, last = copy.deepcopy(record), copy.deepcopy(record)
    if change == "arguments":
        first["decision"]["action"]["arguments"] = {"id": "two"}
    elif change == "registration":
        first["registration_id"] = "v0"
    elif change == "result":
        first["result"]["output"]["value"] = 16
    elif change in {"failed", "pending"}:
        last["status"] = change
        last["result"] = {"error": "unavailable"} if change == "failed" else None
    else:
        entry["effect"]["kind"] = "external-write"
    progress = capture_progress(
        gr, root, [SimpleNamespace(id="a", data=first), SimpleNamespace(id="b", data=last)]
    )
    if change in {"failed", "pending", "write"}:
        assert "repeated_read_count" not in progress
    else:
        assert progress["repeated_read_count"] == 1
    assert progress["model_calls_after_this"] == 0


@pytest.mark.parametrize(
    "projection",
    [
        {"truncated": True},
        {"text": "partial", "text_truncated": True},
        {"content_base64": "YQ=="},
        {"error": "invalid_semantic_output"},
    ],
)
def test_missing_or_partial_last_result_is_not_advertised_as_available(projection):
    result = {"output": {"value": 17}, "untrusted": True}
    gr = SimpleNamespace(
        parent_id=None,
        data={"step": 1, "tools": {"lookup": {"effect": {"kind": "read"}}}, "last_result": projection},
    )
    root = SimpleNamespace(
        data={"policy": {"limits": {"model_attempts": 3}}, "budget": {"model_attempts": 1}}
    )
    op = SimpleNamespace(
        id="a",
        data={
            "decision": {"action": {"kind": "invoke", "capability": "lookup", "arguments": {}}},
            "status": "complete",
            "result": result,
        },
    )
    assert not capture_progress(gr, root, [op])["read_result_in_last_result"]


@pytest.mark.parametrize("allocation,expected", [("shared", 6), ("fixed", 1)])
def test_child_budget_guidance_accounts_for_protected_and_local_capacity(allocation, expected):
    root = SimpleNamespace(
        data={
            "policy": {
                "limits": {"model_attempts": 10},
                "resources": {"allocation": allocation, "finalization": {"model_attempts": 1}},
            },
            "budget": {"model_attempts": 2},
        }
    )
    child = SimpleNamespace(
        parent_id="parent",
        data={"step": 0, "local_limits": {"model_attempts": 3}, "local_usage": {"model_attempts": 1}},
    )
    assert capture_progress(child, root, [])["model_calls_after_this"] == expected
