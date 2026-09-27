"""Boundary and recovery scenarios added during the full harness review."""

import copy
import json
from datetime import timedelta
from uuid import uuid4

import pytest
from test_general_semantic import complete, http_client, stub, submit
from test_harness_extensions import Lookup, Writer, action, create, registration
from test_harness_review import reviewer

from agent_runtime.completion_review import ensure_review
from agent_runtime.context import MemoryContext, session_messages
from agent_runtime.db import RunRow
from agent_runtime.executors import ExecutionBackends
from agent_runtime.extensions import ExtensionRegistry
from agent_runtime.general_completion import general_completion
from agent_runtime.general_contracts import GeneralPolicy
from agent_runtime.general_db import GeneralOperationRow
from agent_runtime.general_history import completed_turn
from agent_runtime.general_runtime import general_action, general_step
from agent_runtime.schemas import AgentConfig


async def test_review_binds_numeric_output_as_well_as_answer(store, monkeypatch):
    stub(monkeypatch, [complete()])
    calls = reviewer(monkeypatch)
    async with http_client(store) as client:
        run = await submit(client, policy=GeneralPolicy(review={}))
        step = await general_step({"run_id": run.id, "completion_loop": 2})
        prepared = await general_completion({"run_id": run.id, **step})
        candidate = prepared["decision"]["action"]
        candidate["value"] = 12
        await ensure_review(store, run.id, candidate)
        changed = copy.deepcopy(candidate)
        changed["value"] = -999
        async with store.database.sessions.begin() as db:
            row, gr, _ = await store.general_lock(db, run.id)
            result = await store.general_complete(db, row, gr, changed)
        assert not result["accepted"]
        assert result["review"]["reason"] == "review_required"
        assert calls[0]["value"] == 12


async def test_review_ignores_uncited_large_source_from_earlier_work(store, monkeypatch):
    calls = reviewer(monkeypatch)
    stub(
        monkeypatch,
        [
            {"kind": "source", "path": "long.txt", "criterion": "c0", "quote": "supported"},
            {"kind": "source", "path": "short.txt", "criterion": "c0", "quote": "supported"},
            complete(),
        ],
    )
    async with http_client(store) as client:
        workspace = await client.workspace_create(
            {"long.txt": b"supported\n" + b"x" * 7000, "short.txt": b"supported answer"}
        )
        run = await submit(
            client,
            ["workspace_read"],
            workspace,
            {
                "outcome": "Answer from short.txt",
                "criteria": [{"id": "c", "statement": "Supported answer", "evidence_policy": "source"}],
            },
            GeneralPolicy(review={}),
        )
        for _ in range(2):
            step = await general_step({"run_id": run.id, "completion_loop": 2})
            await general_action({"run_id": run.id, **step})
        step = await general_step({"run_id": run.id, "completion_loop": 2})
        result = await general_action(await general_completion({"run_id": run.id, **step}))
        assert result["accepted"], result
        assert [s["path"] for s in calls[0]["sources"]] == ["short.txt"]


async def test_unicode_context_is_bounded_in_utf8_not_ascii_escapes(store, monkeypatch):
    calls = stub(monkeypatch, [complete()])
    async with http_client(store) as client:
        agent = await client.create_agent(
            AgentConfig(
                name="unicode", provider="fake", model="deterministic", tools=[], general=GeneralPolicy()
            )
        )
        run = await client.submit(
            agent.id,
            "semantic: " + "项目" * 1350,
            task={
                "outcome": "Summarize this text",
                "criteria": [{"id": "summary", "statement": "A concise summary"}],
            },
        )
        step = await general_step({"run_id": run.id, "completion_loop": 2})
        assert calls and not step.get("rejected")


def test_bounded_history_fits_multibyte_turns_without_losing_latest_pair():
    history = []
    for i in range(8):
        history = completed_turn(history, str(i) + "文" * 16000, "答" * 16000, bounded=True)
    assert len(json.dumps(history, ensure_ascii=False).encode()) < 250000
    assert history[-2]["parts"][0]["content"].startswith("7")
    assert history[-1]["parts"][0]["content"] == "答" * 16000


def test_compaction_accepts_optional_empty_observations():
    value = MemoryContext().compact(
        {"input": "small", "observations": None, "loaded_skills": {"large": "x" * 3000}}, 300
    )
    assert value["input"] == "small" and not value["loaded_skills"]


async def test_history_ranks_old_specific_match_above_recent_generic_matches(store):
    async with http_client(store) as client:
        run = await submit(client)
        async with store.database.sessions.begin() as db:
            seed = await db.get(RunRow, run.id)
            for i in range(40):
                rid = str(uuid4())
                db.add(
                    RunRow(
                        id=rid,
                        session_id=seed.session_id,
                        agent_id=seed.agent_id,
                        key=rid,
                        fingerprint=rid,
                        config=seed.config,
                        registration_id=seed.registration_id,
                        input="The routing key for the archive is AMBER-91"
                        if i == 2
                        else "Unrelated archive update",
                        status="completed",
                        output={"answer": "Recorded"},
                        created_at=seed.created_at + timedelta(seconds=i),
                    )
                )
        async with store.database.sessions() as db:
            row = await db.get(RunRow, run.id)
            items = await session_messages(db, row, "What routing key did I use for the archive?")
        assert any("AMBER-91" in item["content"] for item in items)


async def test_unknown_external_effect_does_not_prevent_sandbox_acknowledgment(store):
    class Backend:
        name, version = "cleanup-test", 1

        def __init__(self):
            self.acks = []

        async def request(self, identity, payload=None, *, acknowledge=False, cancel=False):
            assert acknowledge and not cancel
            self.acks.append(identity)
            return {}

    backend = Backend()
    store.executors = ExecutionBackends([backend])
    store.extensions = ExtensionRegistry(
        {"tools": [registration(True)]}, handlers={"installed.lookup": Writer()}
    )
    async with http_client(store) as client:
        run = await create(client, policy=GeneralPolicy(workspace_policy=backend.name))
        async with store.database.sessions.begin() as db:
            row, gr, _ = await store.general_lock(db, run.id)
            gr.data = {**gr.data, "cleanup_state": "pending"}
            db.add(
                GeneralOperationRow(
                    id=run.id + ":action:0",
                    run_id=run.id,
                    fingerprint="unknown",
                    data={
                        "external": True,
                        "started": True,
                        "status": "outcome_unknown",
                        "lease": 0,
                        "decision": action(run.id)["decision"],
                        "effect_policy": registration(True)["effect"],
                    },
                )
            )
            await db.flush()
            db.add(
                GeneralOperationRow(
                    id=run.id + ":action:1",
                    run_id=run.id,
                    fingerprint="sandbox",
                    data={
                        "status": "complete",
                        "command": {"argv": ["python", "-c", "pass"]},
                        "command_attempt": 1,
                        "result": {"exit_code": 0},
                    },
                )
            )
        assert not await store.general_cleanup(run.id)
        assert backend.acks == [run.id + ":action:1:command:1"]
        assert (await client.get(run.id)).cleanup_state == "pending"


@pytest.mark.parametrize("write", [False, True])
async def test_cancelled_lost_extension_receipt_is_classified_without_reexecution(store, write):
    import asyncio

    started = asyncio.Event()

    class Interrupted(Lookup):
        async def execute(self, call):
            self.calls.append(call)
            started.set()
            await asyncio.Event().wait()

        async def reconcile(self, call):
            pytest.fail("Cancellation must not reissue a remote mutation")

    handler = Interrupted()
    store.extensions = ExtensionRegistry(
        {"tools": [registration(write)]}, handlers={"installed.lookup": handler}
    )
    async with http_client(store) as client:
        run = await create(client)
        payload = action(run.id)
        if write:
            approval = await general_action(payload)
            await client.decide(run.id, approval["approval"], True)
        pending = asyncio.create_task(general_action(payload))
        await asyncio.wait_for(started.wait(), 3)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        await client.cancel(run.id)
        async with store.database.sessions.begin() as db:
            op = await db.get(GeneralOperationRow, run.id + ":action:0")
            op.data = {**op.data, "lease": 0}
        assert await store.general_cleanup(run.id) is (not write)
        async with store.database.sessions() as db:
            op = await db.get(GeneralOperationRow, run.id + ":action:0")
            assert op.data["status"] == ("outcome_unknown" if write else "complete")
        assert len(handler.calls) == 1
        assert len(await client.effects(run.id)) == int(write)


async def test_optional_criteria_still_review_the_requested_outcome(store, monkeypatch):
    calls = reviewer(monkeypatch)
    stub(monkeypatch, [complete()])
    async with http_client(store) as client:
        run = await submit(
            client,
            task={
                "outcome": "Compute the answer",
                "criteria": [{"id": "optional", "statement": "Extra detail", "required": False}],
            },
            policy=GeneralPolicy(review={}),
        )
        step = await general_step({"run_id": run.id, "completion_loop": 2})
        result = await general_action(await general_completion({"run_id": run.id, **step}))
        assert result["accepted"], result
        assert calls[0]["criteria"] == [
            {
                "id": "runtime.review.outcome",
                "statement": "Compute the answer",
                "evidence_policy": "assessment",
            }
        ]


@pytest.mark.parametrize("write", [False, True])
async def test_extension_timeout_has_bounded_and_truthful_outcome(store, monkeypatch, write):
    import asyncio

    from temporalio.exceptions import ApplicationError

    monkeypatch.setattr("agent_runtime.extension_runtime.TOOL_TIMEOUT_SECONDS", 0.02)

    class Slow(Writer):
        async def execute(self, call):
            self.calls.append(call)
            self.effects[call.idempotency_key] = {"recorded": "one"}
            await asyncio.Event().wait()

    handler = Slow()
    store.extensions = ExtensionRegistry(
        {"tools": [registration(write)]}, handlers={"installed.lookup": handler}
    )
    async with http_client(store) as client:
        run = await create(client)
        payload = action(run.id)
        if write:
            pending = await general_action(payload)
            await client.decide(run.id, pending["approval"], True)
            with pytest.raises(ApplicationError, match="extension_reconcile_pending"):
                await asyncio.wait_for(general_action(payload), 3)
            result = await general_action(payload)
            assert result["effect"] and len(handler.reconciliations) == 1
        else:
            result = await asyncio.wait_for(general_action(payload), 3)
            assert result["error"] == "extension_execution_failed"
        assert len(handler.calls) == 1


@pytest.mark.parametrize("mode", ["schema_changed", "tool_removed"])
async def test_mcp_contract_drift_prevents_remote_call(monkeypatch, mode):
    from types import SimpleNamespace

    from agent_runtime.extensions import MCPHandler, ToolCall

    definition = {
        **registration(),
        "handler": "mcp.http",
        "config": {"url": "http://fixture.test/mcp", "tool": "lookup"},
    }

    class Drifted:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def list_tools(self):
            return (
                []
                if mode == "tool_removed"
                else [SimpleNamespace(name="lookup", input_schema={"type": "object"})]
            )

        async def call_tool(self, *args, **kwargs):
            pytest.fail("A changed remote schema must not execute")

    monkeypatch.setattr("fastmcp.Client", Drifted)
    with pytest.raises(ValueError, match="mcp_schema_changed"):
        await MCPHandler().execute(ToolCall("run", "operation", {"customer": "one"}, definition))


@pytest.mark.parametrize("write", [False, True])
@pytest.mark.parametrize("value", [["wrong type"], {"number": float("nan")}, {"large": "x" * 1024}])
async def test_invalid_extension_result_never_makes_unknown_write_successful(store, write, value):
    from temporalio.exceptions import ApplicationError

    class Invalid(Writer):
        async def execute(self, call):
            self.calls.append(call)
            return value

        async def reconcile(self, call):
            self.reconciliations.append(call.idempotency_key)
            return value

    handler = Invalid()
    definition = {**registration(write), "max_result_bytes": 256}
    store.extensions = ExtensionRegistry({"tools": [definition]}, handlers={"installed.lookup": handler})
    async with http_client(store) as client:
        run = await create(client)
        payload = action(run.id)
        if write:
            pending = await general_action(payload)
            await client.decide(run.id, pending["approval"], True)
            with pytest.raises(ApplicationError, match="extension_reconcile_pending"):
                await general_action(payload)
        result = await general_action(payload)
        assert result["error"] == ("extension_outcome_unknown" if write else "extension_execution_failed")
        assert not result.get("effect") and len(handler.calls) == 1


async def test_cleanup_rechecks_new_intents_created_during_acknowledgment(store):
    class RacingBackend:
        name, version = "racing-cleanup", 1

        async def request(self, identity, payload=None, *, acknowledge=False, cancel=False):
            assert acknowledge
            async with store.database.sessions.begin() as db:
                db.add(
                    GeneralOperationRow(
                        id=run.id + ":action:1",
                        run_id=run.id,
                        fingerprint="new",
                        data={"status": "pending", "external": True},
                    )
                )
            return {}

    store.executors = ExecutionBackends([RacingBackend()])
    async with http_client(store) as client:
        run = await submit(client, policy=GeneralPolicy(workspace_policy="racing-cleanup"))
        async with store.database.sessions.begin() as db:
            _, gr, _ = await store.general_lock(db, run.id)
            gr.data = {**gr.data, "cleanup_state": "pending"}
            db.add(
                GeneralOperationRow(
                    id=run.id + ":action:0",
                    run_id=run.id,
                    fingerprint="done",
                    data={
                        "status": "complete",
                        "command": {"argv": ["python", "-c", "pass"]},
                        "command_attempt": 1,
                        "result": {"exit_code": 0},
                    },
                )
            )
        assert not await store.general_cleanup(run.id)
        assert (await client.get(run.id)).cleanup_state == "pending"
