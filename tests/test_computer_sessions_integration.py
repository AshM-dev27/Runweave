"""Real PostgreSQL/Temporal continuation, replacement and expiry with fake machines."""

import asyncio
import json
from datetime import timedelta

import pytest
from pydantic_ai.durable_exec.temporal import PydanticAIPlugin
from temporalio.worker import Replayer
from test_computer_sessions import computers as computers
from test_computer_sessions import submit
from test_general_integration import backend

from agent_runtime.computer_db import ComputerSessionRow
from agent_runtime.db import now
from agent_runtime.general_workflow import ComputerCleanupWorkflow, GeneralWorkflow

pytestmark = pytest.mark.integration


async def closed(client, identity):
    async with asyncio.timeout(30):
        while (await client.computer(identity)).status != "closed":
            await asyncio.sleep(0.2)


async def test_continue_same_computer_after_task_and_worker_replacement(pg_store, monkeypatch, computers):
    async with backend(pg_store, monkeypatch) as (client, temporal):
        first, _ = await submit(client, script=True)
        result = await client.result(first.id, timeout=45)
        assert result.outcome == "succeeded"
        identity = (await client.computers(result.session_id))[0].id
        status = await client.extension_status()
        assert status["items"][0]["active"] == 1 and status["items"][0]["pending_cleanup"] == 0
    async with backend(pg_store, monkeypatch) as (client, temporal):
        second, _ = await submit(client, session_id=first.session_id, script=True)
        result = await client.result(second.id, timeout=45)
        assert result.outcome == "succeeded" and len(result.files) == 1
        assert json.loads(await client.download(result.files[0].id)) == {"counter": 2}
        assert (await client.computers(first.session_id))[0].id == identity
        assert computers.count("create") == 1 and computers.count("run") == 2 and computers.count("kill") == 0
        for run_id in [first.id, second.id]:
            history = await temporal.get_workflow_handle("run:" + run_id).fetch_history()
            await Replayer(workflows=[GeneralWorkflow], plugins=[PydanticAIPlugin()]).replay_workflow(history)
        await client.close_computer(identity)
        await closed(client, identity)
        assert computers.count("kill") == 1
        assert (await client.extension_status())["items"][0]["active"] == 0
        handle = temporal.get_workflow_handle("computer-cleanup:" + identity)
        await handle.result()
        await Replayer(workflows=[ComputerCleanupWorkflow], plugins=[PydanticAIPlugin()]).replay_workflow(
            await handle.fetch_history()
        )


async def test_idle_expiry_is_rediscovered_after_worker_restart(pg_store, monkeypatch, computers):
    async with backend(pg_store, monkeypatch) as (client, _):
        first, _ = await submit(client, script=True)
        await client.result(first.id, timeout=45)
        identity = (await client.computers(first.session_id))[0].id
    async with pg_store.database.sessions.begin() as db:
        row = await db.get(ComputerSessionRow, identity)
        row.idle_expires_at = now() - timedelta(seconds=1)
    async with backend(pg_store, monkeypatch) as (client, _):
        await closed(client, identity)
        assert computers.count("kill") == computers.count("create") == 1
        assert (await client.extension_status())["items"][0]["active"] == 0


async def test_worker_replacement_collects_same_inflight_command_once(pg_store, monkeypatch, computers):
    original = computers.create

    async def create(**kwargs):
        machine = await original(**kwargs)
        machine.missing_receipt = True
        return machine

    monkeypatch.setattr(computers, "create", create)
    async with backend(pg_store, monkeypatch) as (client, _):
        first, _ = await submit(client, script=True)
        async with asyncio.timeout(30):
            while computers.count("run") == 0:
                await asyncio.sleep(0.1)
    machine = next(iter(computers.machines.values()))
    command = next(args for name, args in machine.calls if name == "run")
    path = command.removeprefix("python -I ").removesuffix("/runner.py") + "/result.json"
    machine.content[path] = b'{"exit_code":0,"stdout":"1","stderr":""}'
    async with backend(pg_store, monkeypatch) as (client, temporal):
        result = await client.result(first.id, timeout=45)
        assert result.outcome == "succeeded" and len(result.files) == 1
        assert computers.count("create") == computers.count("run") == 1
        identity = (await client.computers(first.session_id))[0].id
        await client.close_computer(identity)
        await closed(client, identity)
        history = await temporal.get_workflow_handle("run:" + first.id).fetch_history()
        await Replayer(workflows=[GeneralWorkflow], plugins=[PydanticAIPlugin()]).replay_workflow(history)


async def test_failed_tool_requires_attention_in_postgres_temporal_and_replay(
    pg_store, monkeypatch, computers
):
    original = computers.create

    async def failed(**kwargs):
        machine = await original(**kwargs)
        machine.exit_code = 1
        return machine

    monkeypatch.setattr(computers, "create", failed)
    async with backend(pg_store, monkeypatch) as (client, temporal):
        run, _ = await submit(client, script=True)
        result = await client.result(run.id, timeout=45)
        assert result.status == "completed"
        assert (result.outcome, result.outcome_reason) == ("needs_attention", "tool_error")
        assert not result.files
        assert (await client.computers(run.session_id))[0].status == "closed"
        assert (await client.extension_status())["items"][0]["active"] == 0
        history = await temporal.get_workflow_handle("run:" + run.id).fetch_history()
        await Replayer(workflows=[GeneralWorkflow], plugins=[PydanticAIPlugin()]).replay_workflow(history)
