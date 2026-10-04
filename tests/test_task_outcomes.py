"""Task outcomes follow durable facts across denials, retries and old records."""

import httpx
import pytest

from agent_runtime.api import create_app
from agent_runtime.client import Client
from agent_runtime.client_results import outcome
from agent_runtime.db import ToolkitRunRow
from agent_runtime.schemas import AgentConfig, RunCreate
from agent_runtime.toolkit_runtime import toolkit_tool


async def submit(store, tools, **kwargs):
    agent = await store.agent(
        AgentConfig(name="Outcome test", provider="fake", model="deterministic", tools=tools, **kwargs)
    )
    return await store.submit(
        RunCreate(agent_id=agent.id, input="Perform the requested task"), "outcome-test"
    )


@pytest.mark.parametrize("approved", [False, True])
async def test_approval_outcome_matches_api_client_and_terminal_event(store, approved):
    run = await submit(store, ["record_note"])
    assert run.outcome == "in_progress"
    await store.awaiting(run.id, [{"id": "note-1", "tool": "record_note", "arguments": {"text": "test"}}])
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(store, "test")),
        base_url="http://test",
        headers={"Authorization": "Bearer test"},
    ) as http:
        client = Client(http_client=http)
        paused = await client.result(run.id)
        assert (paused.outcome, paused.outcome_reason) == ("needs_attention", "approval_required")
        decided = await client.decide(run.id, "note-1", approved)
        assert decided.outcome == "in_progress" and not decided.approvals
        # Model prose cannot override a recorded denial.
        await store.finish(run.id, "completed", {"answer": "Everything succeeded."})
        progress = []
        result = await client.result(run.id, on_progress=progress.append)
        expected = "succeeded" if approved else "blocked"
        assert result.status == "completed" and result.outcome == expected
        assert result.outcome_reason == (None if approved else "approval_denied")
        assert progress[-1].outcome == expected and progress[-1].message == result.message
        assert (await client.decide(run.id, "note-1", approved)).outcome == expected
        terminal = [e for e in await store.events(run.id) if e.type == "run.completed"]
        assert len(terminal) == 1 and terminal[0].data["outcome"] == expected
        assert (await client.get(run.id)).outcome == expected


async def test_unattached_file_is_blocked_despite_completed_model_answer(store):
    file = await store.upload(b"PRIVATE CONTENT\n", "text/plain", "private.txt", "private-file")
    run = await submit(store, ["document_read"])
    data = {"run_id": run.id, "id": "read-1", "tool": "document_read", "arguments": {"artifact_id": file.id}}
    for _ in range(2):
        assert await toolkit_tool(data) == {"error": "artifact_not_authorized"}
    await store.finish(run.id, "completed", {"answer": "Successfully read the file."})
    result = outcome(await store.get(run.id))
    assert result.outcome == "blocked" and result.outcome_reason == "artifact_not_authorized"
    assert "not attached" in result.message
    events = await store.events(run.id)
    assert len([e for e in events if e.type == "tool.failed"]) == 1
    assert not any("PRIVATE CONTENT" in str(e.data) for e in events)
    assert events[-1].data["outcome"] == "blocked"


async def test_retry_of_same_operation_clears_failure_and_late_error_cannot_override_success(
    store, monkeypatch
):
    file = await store.upload(b"Allowed content\n", "text/plain", "allowed.txt", "allowed-file")
    agent = await store.agent(
        AgentConfig(name="Recovered tool", provider="fake", model="deterministic", tools=["document_read"])
    )
    run = await store.submit(RunCreate(agent_id=agent.id, input="Read", artifact_ids=[file.id]), "recovered")
    data = {"run_id": run.id, "id": "read-1", "tool": "document_read", "arguments": {"artifact_id": file.id}}

    async def unavailable(*args, **kwargs):
        raise ValueError("sandbox_unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(store, "artifact", unavailable)
        assert await toolkit_tool(data) == {"error": "sandbox_unavailable"}
    result = await toolkit_tool(data)
    assert result["lines"][0]["text"] == "Allowed content"
    await store.record_tool_failure(run.id, "read-1", "sandbox_unavailable")
    assert not (await store.toolkit(run.id))["outcome_issues"]
    await store.finish(run.id, "completed", {"answer": "Read the content"})
    assert (await store.get(run.id)).outcome == "succeeded"
    await store.record_tool_failure(run.id, "late-unknown", "sandbox_unavailable")
    assert (await store.get(run.id)).outcome == "succeeded"


async def test_unresolved_error_requires_attention_and_historical_unknown_is_not_success(store):
    run = await submit(store, ["document_read"])
    await store.record_tool_failure(run.id, "read-1", "sandbox_unavailable")
    await store.finish(run.id, "completed", {"answer": "Done"})
    result = outcome(await store.get(run.id))
    assert (result.outcome, result.outcome_reason) == ("needs_attention", "tool_error")
    # Old toolkit runs did not retain these facts. Do not guess from answer wording.
    async with store.database.sessions.begin() as db:
        row = await db.get(ToolkitRunRow, run.id)
        row.state = {k: v for k, v in row.state.items() if k not in {"outcome_tracking", "outcome_issues"}}
    result = outcome(await store.get(run.id))
    assert (result.outcome, result.outcome_reason) == ("needs_attention", "outcome_unavailable")


async def test_failed_specialist_requires_parent_review(store):
    specialist = await store.agent(
        AgentConfig(name="Reader", provider="fake", model="deterministic", tools=["document_read"])
    )
    root = await submit(
        store,
        ["delegate"],
        subagents=[{"name": "reader", "agent_id": specialist.id, "description": "Read documents"}],
    )
    child = await store.create_child(
        root.id, "child-1", {"specialist": "reader", "instruction": "Read input", "artifact_ids": []}
    )
    await store.finish(child, "failed", error="execution_failed")
    await store.finish(root.id, "completed", {"answer": "Everything succeeded"})
    result = await store.get(root.id)
    assert (result.outcome, result.outcome_reason) == ("needs_attention", "child_failed")


@pytest.mark.parametrize(
    "status,expected",
    [
        ("paused_budget", "needs_attention"),
        ("failed", "failed"),
        ("cancelled", "cancelled"),
    ],
)
async def test_outcomes_on_client_talking_to_older_server(store, status, expected):
    run = await submit(store, ["add"])
    run = run.model_copy(update={"status": status, "outcome": None, "outcome_reason": None})
    assert outcome(run).outcome == expected


async def test_old_server_completion_is_not_guessed_from_prose(store):
    run = await submit(store, ["add"])
    await store.finish(run.id, "completed", {"answer": "Success"})
    run = (await store.get(run.id)).model_copy(update={"outcome": None, "outcome_reason": None})
    assert outcome(run).outcome == "needs_attention"


@pytest.mark.parametrize("denied", [True, False])
async def test_cli_reports_outcome_and_exit_code(store, monkeypatch, capsys, denied):
    from agent_runtime import cli

    run = await submit(store, ["record_note"])
    await store.awaiting(run.id, [{"id": "note", "tool": "record_note", "arguments": {"text": "test"}}])
    await store.decide(run.id, "note", not denied)
    await store.finish(run.id, "completed", {"answer": "Model answer"})
    monkeypatch.setenv("API_KEY", "test")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(store, "test")),
        base_url="http://test",
        headers={"Authorization": "Bearer test"},
    ) as http:
        monkeypatch.setattr(cli, "Client", lambda *args: Client(http_client=http))
        assert await cli.run(cli.parser().parse_args(["result", run.id])) == (2 if denied else 0)
        text = capsys.readouterr().out
        assert ("Outcome: blocked" if denied else "Outcome: succeeded") in text
        if denied:
            assert "Task blocked" in text and "Task succeeded" not in text


async def test_reusing_completed_call_id_for_changed_arguments_requires_review(store):
    a = await store.upload(b"A\n", "text/plain", "a.txt", "a")
    b = await store.upload(b"B\n", "text/plain", "b.txt", "b")
    agent = await store.agent(
        AgentConfig(name="Changed call", provider="fake", model="deterministic", tools=["document_read"])
    )
    run = await store.submit(
        RunCreate(agent_id=agent.id, input="Read", artifact_ids=[a.id, b.id]), "changed-call"
    )
    call = {"run_id": run.id, "id": "read-1", "tool": "document_read", "arguments": {"artifact_id": a.id}}
    assert "lines" in await toolkit_tool(call)
    assert await toolkit_tool({**call, "arguments": {"artifact_id": b.id}}) == {
        "error": "operation_arguments_changed"
    }
    await store.finish(run.id, "completed", {"answer": "Done"})
    result = await store.get(run.id)
    assert (result.outcome, result.outcome_reason) == ("needs_attention", "tool_error")
