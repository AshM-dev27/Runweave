"""Real services. Poll observable state with deadlines; no guessed startup sleeps."""

import asyncio
import json
import os
import sys
from contextlib import asynccontextmanager
from datetime import timedelta
from uuid import uuid4

import pytest
from pydantic_ai.durable_exec.temporal import PydanticAIPlugin
from sqlalchemy import func, select
from temporalio.client import Client
from temporalio.worker import Worker

from agent_runtime.activities import ACTIVITIES
from agent_runtime.db import NoteRow, OutboxRow
from agent_runtime.dispatch import Dispatcher
from agent_runtime.runtime import configure_store
from agent_runtime.schemas import AgentConfig, RunCreate
from agent_runtime.store import Problem
from agent_runtime.workflow import RunWorkflow

pytestmark = pytest.mark.integration


async def wait_status(store, run_id, statuses, seconds=45):
    async with asyncio.timeout(seconds):
        while True:
            run = await store.get(run_id)
            if run.status in statuses:
                return run
            if run.status in {"completed", "failed", "cancelled"}:
                raise AssertionError(f"Unexpected run result: {run.status}, {run.error}")
            await asyncio.sleep(0.05)


async def submit(store, prompt="add 2 3", tools=None, session_id=None, **kwargs):
    saved = await store.agent(
        AgentConfig(
            name="integration", provider="fake", model="deterministic", tools=tools or ["add"], **kwargs
        )
    )
    return await store.submit(RunCreate(agent_id=saved.id, input=prompt, session_id=session_id), uuid4().hex)


async def client():
    return await Client.connect(
        os.environ.get("TEMPORAL_ADDRESS", "localhost:7233"), plugins=[PydanticAIPlugin()]
    )


@asynccontextmanager
async def working(store, temporal=None):
    configure_store(store)
    temporal = temporal or await client()
    queue = f"test-{uuid4().hex}"
    dispatch = Dispatcher(store, temporal, queue)
    async with Worker(temporal, task_queue=queue, workflows=[RunWorkflow], activities=ACTIVITIES):
        task = asyncio.create_task(dispatch.run())
        try:
            yield temporal, dispatch
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_durable_flow_session_and_mcp(pg_store):
    async with working(pg_store):
        run = await submit(pg_store)
        result = await wait_status(pg_store, run.id, {"completed"})
        assert result.output.value == 5
        next_run = await submit(pg_store, "previous", session_id=run.session_id)
        continued = await wait_status(pg_store, next_run.id, {"completed"})
        assert continued.output.value == 5
        mcp = await submit(pg_store, "temperature:100", tools=["convert_temperature"])
        converted = await wait_status(pg_store, mcp.id, {"completed"})
        assert converted.output.value == 212
        events = await pg_store.events(mcp.id)
        assert any(e.type == "tool.completed" for e in events)


async def test_postgres_concurrent_submission_and_terminal_race(pg_store):
    agent = await pg_store.agent(AgentConfig(name="race", provider="fake", model="deterministic"))
    body = RunCreate(agent_id=agent.id, input="add 2 3")
    runs = await asyncio.gather(*(pg_store.submit(body, "same") for _ in range(12)))
    assert len({r.id for r in runs}) == 1
    run = runs[0]
    await asyncio.gather(
        pg_store.cancel(run.id), pg_store.finish(run.id, "completed", output={"answer": "5", "value": 5})
    )
    events = await pg_store.events(run.id)
    assert sum(e.type in {"run.completed", "run.cancelled"} for e in events) == 1
    session = await pg_store.session()
    body = RunCreate(agent_id=agent.id, session_id=session.id, input="add 1 2")
    attempts = await asyncio.gather(
        *(pg_store.submit(body, f"unique-{i}") for i in range(5)), return_exceptions=True
    )
    assert sum(not isinstance(r, Exception) for r in attempts) == 1
    assert all(isinstance(r, Problem) and r.status == 409 for r in attempts if isinstance(r, Exception))


async def test_dispatch_ack_loss_and_cancel_before_start(pg_store):
    temporal = await client()
    queue = f"test-{uuid4().hex}"
    dispatch = Dispatcher(pg_store, temporal, queue)
    run = await submit(pg_store)
    await dispatch.once()
    async with pg_store.database.sessions.begin() as db:
        item = await db.get(OutboxRow, f"start:{run.id}")
        item.delivered = False  # Start succeeded; process died before DB acknowledgement.
    await dispatch.once()
    await pg_store.cancel(run.id)
    await dispatch.once()
    async with Worker(temporal, task_queue=queue, workflows=[RunWorkflow], activities=ACTIVITIES):
        handle = temporal.get_workflow_handle(f"run:{run.id}")
        try:
            await asyncio.wait_for(handle.result(), 30)
        except Exception:
            pass
    assert (await pg_store.get(run.id)).status == "cancelled"
    assert not any(e.type == "tool.started" for e in await pg_store.events(run.id))


@pytest.mark.parametrize("approved", [False, True])
@pytest.mark.parametrize("time_skipping", [False, True])
async def test_approval_survives_hard_worker_restart(
    pg_store, tmp_path, time_environment, time_skipping, approved
):
    temporal = time_environment.client if time_skipping else await client()
    queue = f"restart-{uuid4().hex}"
    env = {
        **os.environ,
        "DATABASE_URL": pg_store.test_url,
        "DATABASE_SCHEMA": pg_store.test_schema,
        "TASK_QUEUE": queue,
        "TEMPORAL_ADDRESS": temporal.service_client.config.target_host,
    }
    # Remove provider credentials: this subprocess test must never make a paid call.
    env.pop("OPENAI_API_KEY", None)
    env.pop("ANTHROPIC_API_KEY", None)
    log = (tmp_path / "worker.log").open("w")

    async def start():
        command = [sys.executable, "-m", "agent_runtime.worker"]
        if time_skipping:
            # The Java test server cannot reset/expire a dead sticky queue after a
            # time jump. Disable caching only in this test worker, forcing full replay.
            command = [
                sys.executable,
                "-c",
                "import asyncio, functools; import agent_runtime.worker as w; w.Worker = functools.partial(w.Worker, max_cached_workflows=0); asyncio.run(w.main())",
            ]
        return await asyncio.create_subprocess_exec(*command, env=env, stdout=log, stderr=log)

    process = await start()
    try:
        run = await submit(pg_store, "note:durable approval", tools=["record_note"])
        pending = await wait_status(pg_store, run.id, {"awaiting_approval"})
        # PostgreSQL awaiting state commits before the activity acknowledgement and
        # timer command. Kill only once the long wait is durably in Temporal history.
        async with asyncio.timeout(20):
            while True:
                history = await temporal.get_workflow_handle(f"run:{run.id}").fetch_history()
                if any(
                    event.HasField("timer_started_event_attributes")
                    and event.timer_started_event_attributes.start_to_fire_timeout.seconds == 86400
                    for event in history.events
                ):
                    break
                await asyncio.sleep(0.05)
        process.kill()
        await process.wait()
        # Advance hours while the worker is dead; the active budget remains paused.
        if time_skipping:
            await time_environment.sleep(timedelta(hours=3))
        changed = [registration.model_dump() for registration in pg_store.registry.entries.values()]
        changed[0]["upstream_model"] = "operator-edited-after-submission"
        registry_file = tmp_path / "models.json"
        registry_file.write_text(json.dumps(changed))
        env["MODEL_REGISTRY_FILE"] = str(registry_file)
        approval_id = pending.approvals[0].id
        await pg_store.decide(run.id, approval_id, approved)
        process = await start()
        await Dispatcher(pg_store, temporal, queue).once()
        result = await wait_status(pg_store, run.id, {"completed"})
        assert (
            result.output.answer == "Note recorded" if approved else "denied" in result.output.answer.lower()
        )
        async with pg_store.database.sessions() as db:
            assert await db.scalar(
                select(func.count()).select_from(NoteRow).where(NoteRow.run_id == run.id)
            ) == int(approved)
        history = await temporal.get_workflow_handle(f"run:{run.id}").fetch_history()
        from temporalio.worker import Replayer

        await Replayer(workflows=[RunWorkflow], plugins=[PydanticAIPlugin()]).replay_workflow(history)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
        log.close()


async def two_round_response(messages, info):
    from pydantic_ai.messages import ModelResponse, ToolCallPart, ToolReturnPart

    returned = [
        part
        for message in messages
        for part in message.parts
        if isinstance(part, ToolReturnPart) and part.tool_name == "record_note"
    ]
    if len(returned) < 2:
        return ModelResponse(
            parts=[ToolCallPart("record_note", {"text": "round"}, tool_call_id=f"round-{len(returned)}")]
        )
    return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {"answer": "done"})])


async def wait_round(store, run_id, call_id):
    async with asyncio.timeout(30):
        while True:
            run = await store.get(run_id)
            # Decided approvals disappear before the worker advances the run status.
            if run.status == "awaiting_approval" and run.approvals and run.approvals[0].id == call_id:
                return run
            assert run.status not in {"completed", "failed", "cancelled"}, run.error
            await asyncio.sleep(0.05)


async def test_approval_rejection_and_cumulative_timeout(pg_store, time_environment, monkeypatch):
    from agent_runtime.runtime import fake_model

    async with working(pg_store, time_environment.client) as (temporal, dispatch):
        run = await submit(pg_store, "note:reject me", tools=["record_note"])
        pending = await wait_status(pg_store, run.id, {"awaiting_approval"})
        await pg_store.decide(run.id, pending.approvals[0].id, False)
        await wait_status(pg_store, run.id, {"completed"})
        monkeypatch.setattr(fake_model, "function", two_round_response)
        pg_store.approval_wait_seconds = 6 * 3600
        timed = await submit(pg_store, "two rounds", tools=["record_note"])
        pg_store.approval_wait_seconds = 86400  # A later operator edit cannot extend this run.
        await wait_round(pg_store, timed.id, "round-0")
        await time_environment.sleep(timedelta(hours=2))
        await pg_store.decide(timed.id, "round-0", True)
        await dispatch.once()
        await wait_round(pg_store, timed.id, "round-1")
        await time_environment.sleep(timedelta(hours=4, seconds=1))
        result = await wait_status(pg_store, timed.id, {"failed"})
        assert result.error == "approval_timeout"
        with pytest.raises(Problem):
            await pg_store.decide(timed.id, "round-1", True)
        # Even a late already-in-flight signal cannot execute the expired effect.
        handle = temporal.get_workflow_handle(f"run:{timed.id}")
        await handle.result()
        await dispatch.once()
        async with pg_store.database.sessions() as db:
            assert await db.scalar(select(func.count()).select_from(NoteRow)) == 1
        from temporalio.worker import Replayer

        await Replayer(workflows=[RunWorkflow], plugins=[PydanticAIPlugin()]).replay_workflow(
            await handle.fetch_history()
        )


@pytest.mark.parametrize("limit", ["active", "requests"])
async def test_cumulative_execution_and_usage_across_resumes(pg_store, time_environment, monkeypatch, limit):
    from agent_runtime.runtime import fake_model

    calls = []

    async def response(messages, info):
        calls.append(len(messages))
        if limit == "active":
            await asyncio.sleep(2)
        return await two_round_response(messages, info)

    monkeypatch.setattr(fake_model, "function", response)
    async with working(pg_store, time_environment.client) as (_, dispatch):
        run = await submit(
            pg_store,
            "two rounds",
            tools=["record_note"],
            timeout_seconds=5 if limit == "active" else 120,
            max_requests=2 if limit == "requests" else 6,
        )
        for call_id in ["round-0", "round-1"]:
            await wait_round(pg_store, run.id, call_id)
            await time_environment.sleep(timedelta(hours=2))
            await pg_store.decide(run.id, call_id, True)
            await dispatch.once()
        result = await wait_status(pg_store, run.id, {"failed"})
        assert result.error == ("run_timeout" if limit == "active" else "usage_limit")
        assert len(calls) == (3 if limit == "active" else 2)
        async with pg_store.database.sessions() as db:
            assert await db.scalar(select(func.count()).select_from(NoteRow)) == 2


async def test_effect_commit_then_activity_failure_retries_once(pg_store, monkeypatch):
    original = pg_store.record_note
    attempts = []

    async def interrupted(run_id, call_id, text):
        result = await original(run_id, call_id, text)
        attempts.append(call_id)
        if len(attempts) == 1:
            raise RuntimeError("Injected failure after effect commit before activity acknowledgement")
        return result

    monkeypatch.setattr(pg_store, "record_note", interrupted)
    async with working(pg_store):
        run = await submit(pg_store, "note:exactly once", tools=["record_note"])
        pending = await wait_status(pg_store, run.id, {"awaiting_approval"})
        await pg_store.decide(run.id, pending.approvals[0].id, True)
        await wait_status(pg_store, run.id, {"completed"})
        assert len(attempts) == 2 and len(set(attempts)) == 1
        async with pg_store.database.sessions() as db:
            assert await db.scalar(select(func.count()).select_from(NoteRow)) == 1


async def test_cancellation_approval_race_and_budget(pg_store):
    async with working(pg_store) as (temporal, _):
        run = await submit(pg_store, "note:cancel me", tools=["record_note"])
        pending = await wait_status(pg_store, run.id, {"awaiting_approval"})
        await asyncio.gather(
            pg_store.cancel(run.id),
            pg_store.decide(run.id, pending.approvals[0].id, True),
            return_exceptions=True,
        )
        assert (await pg_store.get(run.id)).status == "cancelled"
        from temporalio.client import WorkflowExecutionStatus

        async with asyncio.timeout(20):
            while (
                await temporal.get_workflow_handle(f"run:{run.id}").describe()
            ).status == WorkflowExecutionStatus.RUNNING:
                await asyncio.sleep(0.05)
        async with pg_store.database.sessions() as db:
            assert await db.scalar(select(func.count()).select_from(NoteRow)) == 0
        limited = await submit(pg_store, max_requests=1)
        result = await wait_status(pg_store, limited.id, {"failed"})
        assert result.error == "usage_limit"


async def test_dispatch_outage_and_closed_workflow_reconciliation(pg_store, monkeypatch):
    temporal = await client()
    dispatch = Dispatcher(pg_store, temporal, f"unstaffed-{uuid4().hex}")
    run = await submit(pg_store)
    original = temporal.start_workflow

    async def unavailable(*args, **kwargs):
        raise ConnectionError("Injected Temporal outage")

    monkeypatch.setattr(temporal, "start_workflow", unavailable)
    with pytest.raises(ConnectionError):
        await dispatch.once()
    async with pg_store.database.sessions() as db:
        assert not (await db.get(OutboxRow, f"start:{run.id}")).delivered
    monkeypatch.setattr(temporal, "start_workflow", original)
    await dispatch.once()
    await temporal.get_workflow_handle(f"run:{run.id}").terminate()
    await dispatch.reconcile()
    result = await pg_store.get(run.id)
    assert result.status == "failed"
    assert result.error == "workflow_closed_without_result"


async def test_resume_activity_retries_consume_active_budget(pg_store, time_environment, monkeypatch):
    attempts = []
    original = pg_store.resumed

    async def retry_resume(run_id):
        attempts.append(run_id)
        await asyncio.sleep(3)
        if len(attempts) == 1:
            raise RuntimeError("Injected resume failure")
        await original(run_id)

    monkeypatch.setattr(pg_store, "resumed", retry_resume)
    async with working(pg_store, time_environment.client) as (_, dispatch):
        run = await submit(pg_store, "note:retry budget", tools=["record_note"], timeout_seconds=5)
        pending = await wait_status(pg_store, run.id, {"awaiting_approval"})
        await time_environment.sleep(timedelta(hours=2))
        await pg_store.decide(run.id, pending.approvals[0].id, False)
        await dispatch.once()
        result = await wait_status(pg_store, run.id, {"failed"})
        assert result.error == "run_timeout"
        assert len(attempts) == 2
        async with pg_store.database.sessions() as db:
            assert await db.scalar(select(func.count()).select_from(NoteRow)) == 0


@pytest.mark.parametrize("mixed", [False, True])
async def test_run_scoped_denial_retries_readonly_fresh_turn_and_replay(pg_store, monkeypatch, mixed):
    from pydantic_ai.messages import ModelResponse, ToolCallPart, ToolReturnPart
    from temporalio.worker import Replayer

    from agent_runtime.runtime import fake_model

    async def response(messages, info):
        returned = [p for m in messages for p in m.parts if isinstance(p, ToolReturnPart)]
        notes = [p for p in returned if p.tool_name == "record_note"]
        if not notes:
            parts = [ToolCallPart("record_note", {"text": "original"}, tool_call_id="first")]
            if mixed:
                parts.append(ToolCallPart("record_note", {"text": "approved"}, tool_call_id="allowed"))
            return ModelResponse(parts=parts)
        initial = 2 if mixed else 1
        if len(notes) < initial + 2:
            index = len(notes) - initial
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "record_note",
                        {"text": "original" if index == 0 else "changed"},
                        tool_call_id=f"retry-{index}",
                    )
                ]
            )
        assert all("disabled for this run" in str(p.content) for p in notes[-2:])
        if not any(p.tool_name == "add" for p in returned):
            return ModelResponse(parts=[ToolCallPart("add", {"a": 2, "b": 3}, tool_call_id="read")])
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {"answer": "done", "value": 5})])

    monkeypatch.setattr(fake_model, "function", response)
    async with working(pg_store) as (temporal, _):
        run = await submit(pg_store, "retry denial", tools=["record_note", "add"], max_requests=10)
        pending = await wait_status(pg_store, run.id, {"awaiting_approval"})
        await pg_store.decide(run.id, "first", False)
        await pg_store.decide(run.id, "first", False)
        with pytest.raises(Problem):
            await pg_store.decide(run.id, "first", True)
        if mixed:
            await pg_store.decide(run.id, "allowed", True)
        result = await wait_status(pg_store, run.id, {"completed"})
        assert result.output.value == 5
        events = await pg_store.events(run.id)
        assert sum(e.type == "approval.required" for e in events) == len(pending.approvals)
        assert sum(e.type == "tool.effect_committed" for e in events) == int(mixed)
        async with pg_store.database.sessions() as db:
            assert await db.scalar(select(func.count()).select_from(NoteRow)) == int(mixed)
        handle = temporal.get_workflow_handle(f"run:{run.id}")
        await handle.result()
        await Replayer(workflows=[RunWorkflow], plugins=[PydanticAIPlugin()]).replay_workflow(
            await handle.fetch_history()
        )
        # A new explicit turn in the same session is a fresh run, with fresh policy state.
        from agent_runtime.runtime import fake_response

        monkeypatch.setattr(fake_model, "function", fake_response)
        fresh = await submit(pg_store, "note:fresh turn", tools=["record_note"], session_id=run.session_id)
        pending = await wait_status(pg_store, fresh.id, {"awaiting_approval"})
        await pg_store.decide(fresh.id, pending.approvals[0].id, True)
        await wait_status(pg_store, fresh.id, {"completed"})


async def test_persistent_denied_requests_remain_bounded(pg_store, monkeypatch):
    from agent_runtime.runtime import fake_model

    monkeypatch.setattr(fake_model, "function", two_round_response)
    async with working(pg_store):
        run = await submit(pg_store, "repeated denial", tools=["record_note"], max_requests=2)
        await wait_round(pg_store, run.id, "round-0")
        await pg_store.decide(run.id, "round-0", False)
        result = await wait_status(pg_store, run.id, {"failed"})
        assert result.error == "usage_limit"
        events = await pg_store.events(run.id)
        assert sum(e.type == "approval.required" for e in events) == 1
        assert not any(e.type.startswith("tool.") for e in events)
        async with pg_store.database.sessions() as db:
            assert await db.scalar(select(func.count()).select_from(NoteRow)) == 0


async def test_unmarked_history_replays_original_denial_semantics(pg_store, monkeypatch):
    from temporalio import workflow
    from temporalio.worker import Replayer

    from agent_runtime.runtime import fake_model

    monkeypatch.setattr(fake_model, "function", two_round_response)
    original = workflow.patched
    # Generate the original command sequence, with no new patch marker recorded.
    monkeypatch.setattr(
        workflow, "patched", lambda patch: False if patch == "run-scoped-note-denial-v1" else original(patch)
    )
    async with working(pg_store) as (temporal, _):
        run = await submit(pg_store, "legacy denial", tools=["record_note"])
        for call_id in ["round-0", "round-1"]:
            await wait_round(pg_store, run.id, call_id)
            await pg_store.decide(run.id, call_id, False)
        await wait_status(pg_store, run.id, {"completed"})
        handle = temporal.get_workflow_handle(f"run:{run.id}")
        await handle.result()
        history = await handle.fetch_history()
    monkeypatch.setattr(workflow, "patched", original)
    assert sum(e.type == "approval.required" for e in await pg_store.events(run.id)) == 2
    await Replayer(workflows=[RunWorkflow], plugins=[PydanticAIPlugin()]).replay_workflow(history)
