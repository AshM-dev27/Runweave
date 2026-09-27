import json

import pytest
from test_general_semantic import complete, http_client, stub

from agent_runtime.context import ContextPolicies, MemoryContext, messages, reserve_tokens
from agent_runtime.general_completion import general_completion
from agent_runtime.general_contracts import GeneralPolicy
from agent_runtime.general_history import completed_turn
from agent_runtime.general_runtime import general_action, general_step
from agent_runtime.schemas import AgentConfig


def test_memory_retrieves_old_relevant_text_and_keeps_provenance():
    history = []
    history = completed_turn(history, "Use the amber routing key for the archive.", "Understood.")
    for i in range(10):
        history = completed_turn(history, f"Unrelated task {i}", "Done")
    selected = MemoryContext().history(history, "Which routing key is used for the archive?")
    first = next(item for item in selected["messages"] if item["id"] == "m0")
    assert "amber" in first["content"]
    assert first["sha256"] == messages(history)[0]["sha256"]
    assert selected["summary"]["method"] == "extractive"
    assert selected["summary"]["untrusted"]
    assert selected["omitted_messages"] > 0


def test_context_compaction_preserves_current_obligations():
    policy = MemoryContext()
    history = []
    for i in range(20):
        history = completed_turn(history, f"Prior request {i} " + "x" * 3000, "y" * 3000)
    context = {
        "input": "current request",
        "constraints": ["Never change user tests"],
        "criteria": {"c0": {"required": True}},
        "checks": {"k0": {"status": "pending"}},
        "grants": {"write_prefixes": ["one.py"]},
        "previous_turns": policy.history(history, "request"),
        "files": ["long-file-name-" + str(i) * 20 for i in range(200)],
    }
    compact = policy.compact(context, 5000)
    for key in ("input", "constraints", "criteria", "checks", "grants"):
        assert compact[key] == context[key]
    assert len(json.dumps(compact, separators=(",", ":")).encode()) < 5300
    assert compact["compaction"]["retrievable"]
    with pytest.raises(ValueError, match="unavailable"):
        ContextPolicies().get("memory-v1", 99)


def test_token_counter_keeps_byte_fallback_and_explicit_selection():
    payload = ("data and instructions " * 500).encode()
    assert reserve_tokens(payload, 1024) == len(payload) + 1024 + 512
    estimate = reserve_tokens(payload, 1024, "o200k-v1")
    assert 2048 < estimate < reserve_tokens(payload, 1024)
    with pytest.raises(ValueError):
        reserve_tokens(payload, 1024, "invented")


async def test_continuation_recovers_exact_history_beyond_adapter_cache(store, monkeypatch):
    from agent_runtime.db import RunRow, SessionRow
    from agent_runtime.general_actions import GeneralActions

    calls = stub(monkeypatch, lambda *_: complete())
    async with http_client(store) as client:
        agent = await client.create_agent(
            AgentConfig(
                name="memory", provider="fake", model="deterministic", tools=[], general=GeneralPolicy()
            )
        )
        first = await client.submit(agent.id, "semantic: remember original route AMBER-91")
        decision = await general_step({"run_id": first.id, "completion_loop": 2})
        await general_action(await general_completion({"run_id": first.id, **decision}))
        # Simulate bounded adapter cache; the exact owned turn must still be available.
        async with store.database.sessions.begin() as db:
            session = await db.get(SessionRow, first.session_id)
            session.history = completed_turn([], "latest only", "latest response")
        follow = await client.submit(
            agent.id, "semantic: what was the original route?", session_id=first.session_id
        )
        await general_step({"run_id": follow.id, "completion_loop": 2})
        assert any("AMBER-91" in item["content"] for item in calls[-1]["previous_turns"]["messages"])
        async with store.database.sessions.begin() as db:
            row, gr, root = await store.general_lock(db, follow.id)
            result = await GeneralActions.general_capability(
                store, db, row, gr, root, None, "session_history", {"message_id": first.id + ":input"}
            )
        assert result["message"]["content"] == "semantic: remember original route AMBER-91"
        # A known ID from another session must not leak its text.
        other = await client.submit(agent.id, "secret in a different session")
        async with store.database.sessions.begin() as db:
            r = await db.get(RunRow, other.id)
            r.status, r.output = "completed", {"answer": "private"}
        with pytest.raises(Exception):
            async with store.database.sessions.begin() as db:
                row, gr, root = await store.general_lock(db, follow.id)
                await store.general_capability(
                    db, row, gr, root, None, "session_history", {"message_id": other.id + ":input"}
                )


def test_legacy_registration_hash_survives_new_counter_field():
    import hashlib

    from agent_runtime.registry import Registration, load_registry

    legacy = (
        load_registry()
        .entries[("fake", "deterministic")]
        .model_dump(exclude={"token_counter", "reasoning_effort"})
    )
    expected = hashlib.sha256(json.dumps(legacy, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    restored = Registration.model_validate(legacy)
    assert restored.token_counter == "utf8-v1"
    assert restored.identity == expected


async def test_history_never_exposes_completed_toolkit_child(store):
    from agent_runtime.context import session_messages
    from agent_runtime.db import RunRow, ToolkitRunRow

    async with http_client(store) as client:
        agent = await client.create_agent(
            AgentConfig(name="memory", provider="fake", model="deterministic", tools=[])
        )
        parent = await client.submit(agent.id, "Parent request")
        child = await client.submit(agent.id, "Private child context")
        async with store.database.sessions.begin() as db:
            record = await db.get(RunRow, child.id)
            record.status, record.output = "completed", {"answer": "Private child result"}
            record.session_id = parent.session_id
            child_state = await db.get(ToolkitRunRow, child.id)
            child_state.root_id, child_state.parent_id = parent.id, parent.id
        async with store.database.sessions() as db:
            row = await db.get(RunRow, parent.id)
            assert await session_messages(db, row, message_id=child.id + ":input") == []
            assert await session_messages(db, row, query="Private") == []
