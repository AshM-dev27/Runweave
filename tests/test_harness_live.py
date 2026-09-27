"""One paid matrix behind --live; preflight uses the identical backend with fake models."""

import base64
import hashlib
import json
import os
import sqlite3
from pathlib import Path

import pytest
from test_general_integration import backend
from test_general_semantic import complete, http_client, stub
from test_harness_integration import replay
from test_harness_review import reviewer

from agent_runtime.general_contracts import GeneralPolicy
from agent_runtime.general_runtime import general_action, general_step
from agent_runtime.schemas import AgentConfig
from scripts.harness_budget import MANIFEST, policy
from scripts.harness_budget import client as guarded_client


def frozen_sources():
    return {
        str(p): hashlib.sha256(p.read_bytes()).hexdigest()
        for pattern in (
            "agent_runtime/*.py",
            "config/*.json",
            "tests/test_harness*.py",
            "scripts/harness_budget.py",
            "uv.lock",
        )
        for p in Path().glob(pattern)
    }


async def matrix(store, monkeypatch, directory, live):
    guard = policy()
    ledger_path = directory / "requests.sqlite"
    before = frozen_sources()
    directory.mkdir(parents=True, exist_ok=True)
    if live:
        started = directory / "source.json"
        with started.open("x") as file:
            json.dump(before, file, indent=2)
        guard.initialize(ledger_path)
        monkeypatch.setattr(
            "agent_runtime.model_adapter.http_client_factory", lambda _: guarded_client(ledger_path, guard)
        )
    results = []
    try:
        for name, answer, expected in (
            ("review_pass", "Rollback is unavailable after step 5.", "pass"),
            ("review_repair", "Rollback remains available after step 5.", "repair"),
        ):
            with monkeypatch.context() as patch:
                done = {**complete(), "answer": answer}
                stub(
                    patch,
                    [
                        {
                            "kind": "source",
                            "path": "policy.txt",
                            "criterion": "c0",
                            "quote": "Rollback is supported before step 5.",
                        },
                        done,
                        {
                            "kind": "blocked",
                            "reason": "Review rejected the intentionally incorrect candidate.",
                        },
                    ],
                )
                if not live:
                    reviewer(patch, expected)
                async with http_client(store) as client:
                    workspace = await client.workspace_create(
                        {
                            "policy.txt": b"Rollback is supported before step 5. Step 5 deletes the legacy columns. After step 5, rollback is impossible."
                        }
                    )
                    config = AgentConfig(
                        name=name,
                        provider="fake",
                        model="deterministic",
                        tools=["workspace_read"],
                        general=GeneralPolicy(review={"provider": "openai", "model": MANIFEST["model"]}),
                    )
                    agent = await client.create_agent(config)
                    run = await client.submit(
                        agent.id,
                        "semantic: Is rollback possible after step 5?",
                        workspace=workspace,
                        task={
                            "outcome": "Answer whether rollback is possible after step 5.",
                            "criteria": [
                                {
                                    "id": "support",
                                    "statement": "The answer must correctly state whether rollback is possible after step 5, based on policy.txt.",
                                    "evidence_policy": "source",
                                }
                            ],
                        },
                    )
                if live:
                    guard.admit(ledger_path, name, run.id)
                async with backend(store, patch) as (client, temporal):
                    finished = await client.wait(run.id, timeout=100)
                    events = await store.events(run.id)
                    verdicts = [e.data["verdict"] for e in events if e.type == "completion.reviewed"]
                    passed = verdicts == [expected] and finished.status == (
                        "completed" if expected == "pass" else "failed"
                    )
                    await replay(temporal, run.id)
                    results.append(
                        {
                            "scenario": name,
                            "passed": passed,
                            "status": finished.status,
                            "verdicts": verdicts,
                            "budget": await client.budget(run.id),
                            "operations": await client.operations(run.id),
                            "replayed": True,
                        }
                    )
                if live:
                    guard.finish(ledger_path, name, "passed" if passed else "failed")
        # Seed completed turns with fake models; only the follow-up is live.
        async with http_client(store) as client:
            seed = await client.create_agent(
                AgentConfig(
                    name="seed", provider="fake", model="deterministic", tools=[], general=GeneralPolicy()
                )
            )
            session_id = None
            for prompt in [
                "Start a project",
                "Remember routing key AMBER-91 for the archive.",
                *[f"Unrelated update {i}" for i in range(6)],
            ]:
                turn = await client.submit(seed.id, prompt, session_id=session_id)
                session_id = turn.session_id
                step = await general_step(turn.id)
                await general_action({"run_id": turn.id, **step})
                from agent_runtime.db import OutboxRow

                async with store.database.sessions.begin() as db:
                    (await db.get(OutboxRow, "start:" + turn.id)).delivered = True
            agent = await client.create_agent(
                AgentConfig(
                    name="context",
                    provider="openai" if live else "fake",
                    model=MANIFEST["model"] if live else "deterministic",
                    tools=[],
                    max_tokens=1024,
                    general=GeneralPolicy(limits={"model_attempts": 4}),
                )
            )
            run = await client.submit(
                agent.id,
                ("" if live else "semantic: ")
                + "What routing key did I ask you to remember for the archive? Return just that exact key.",
                session_id=session_id,
            )
        if live:
            guard.admit(ledger_path, "context", run.id)
        with monkeypatch.context() as patch:
            if not live:

                def recall(context, _):
                    assert "AMBER-91" in json.dumps(context["previous_turns"])
                    return {**complete(), "answer": "AMBER-91"}

                stub(patch, recall)
            async with backend(store, patch) as (client, temporal):
                finished = await client.wait(run.id, timeout=100)
                passed = finished.status == "completed" and finished.output.answer.strip() == "AMBER-91"
                await replay(temporal, run.id)
                results.append(
                    {
                        "scenario": "context",
                        "passed": passed,
                        "status": finished.status,
                        "budget": await client.budget(run.id),
                        "operations": await client.operations(run.id),
                        "replayed": True,
                    }
                )
        if live:
            guard.finish(ledger_path, "context", "passed" if passed else "failed")
        from scripts.completion_loop_fixtures import fixture

        f = fixture("parallel")
        async with http_client(store) as client:
            agent = await client.create_agent(
                AgentConfig(
                    name="parallel",
                    provider="openai" if live else "fake",
                    model=MANIFEST["model"] if live else "deterministic",
                    tools=f["tools"],
                    max_tokens=1024,
                    general=GeneralPolicy(limits={"model_attempts": 12}, delegation={"tools": f["tools"]}),
                )
            )
            workspace = await client.workspace_create({})
            run = await client.submit(
                agent.id, ("" if live else "semantic: ") + f["prompt"], workspace=workspace, task=f["task"]
            )
        if live:
            guard.admit(ledger_path, "parallel", run.id)
        with monkeypatch.context() as patch:
            if not live:
                counts = {}

                def next_action(context, _):
                    prompt = context["input"]
                    i = counts.get(prompt, 0)
                    counts[prompt] = i + 1
                    if prompt.startswith("semantic: child"):
                        filename, value = (
                            ("left.txt", b"12\n") if "left" in prompt else ("right.txt", b"20\n")
                        )
                        return (
                            complete()
                            if i
                            else {
                                "kind": "write",
                                "files": [
                                    {"path": filename, "content_base64": base64.b64encode(value).decode()}
                                ],
                            }
                        )
                    sequence = [
                        {
                            "kind": "assign",
                            "assignments": [
                                {
                                    "role": side,
                                    "objective": "semantic: child " + side,
                                    "acceptance": ["Write requested file"],
                                    "capabilities": f["tools"],
                                    "outputs": [side + ".txt"],
                                }
                                for side in ("left", "right")
                            ],
                        },
                        {"kind": "join", "children": ["d0", "d1"]},
                        {"kind": "merge", "child": "d0"},
                        {"kind": "merge", "child": "d1"},
                        {"kind": "complete", "answer": "Both outputs verified."},
                    ]
                    return sequence[i]

                stub(patch, next_action)
            async with backend(store, patch) as (client, temporal):
                finished = await client.wait(run.id, timeout=180)
                actual = {}
                if finished.workspace:
                    for name in f["expected"]:
                        try:
                            actual[name] = (
                                await client.workspace_read(
                                    workspace["workspace_id"], finished.workspace["revision_id"], name
                                )
                            ).decode()
                        except Exception:
                            actual[name] = None
                children = await client.children(run.id)
                passed = (
                    finished.status == "completed"
                    and actual == {p: b.decode() for p, b in f["expected"].items()}
                    and len(children) == 2
                    and all(c.status == "completed" for c in children)
                )
                await replay(temporal, run.id)
                results.append(
                    {
                        "scenario": "parallel",
                        "passed": passed,
                        "status": finished.status,
                        "outputs": actual,
                        "children": [{"status": c.status, "error": c.error} for c in children],
                        "budget": await client.budget(run.id),
                        "operations": await client.operations(run.id),
                        "replayed": True,
                    }
                )
        if live:
            guard.finish(ledger_path, "parallel", "passed" if passed else "failed")
    finally:
        report = {
            "live": live,
            "model": MANIFEST["model"] if live else "FunctionModel",
            "source_unchanged": before == frozen_sources(),
            "scenarios": results,
        }
        if live:
            guard.reconcile_unknown(ledger_path)
            report["physical_requests"] = guard.validate(ledger_path)
            with sqlite3.connect(ledger_path) as db:
                report["outcomes"] = db.execute(
                    "SELECT classification,count(*) FROM outcomes GROUP BY classification"
                ).fetchall()
        (directory / "results.json").write_text(json.dumps(report, indent=2))
    assert report["source_unchanged"]
    return report


@pytest.mark.integration
async def test_harness_acceptance_preflight(pg_store, monkeypatch, tmp_path):
    report = await matrix(pg_store, monkeypatch, tmp_path / "preflight", False)
    assert len(report["scenarios"]) == 4 and all(s["passed"] for s in report["scenarios"]), report
    # An explicit local path lets the paid run verify the exact tested source snapshot.
    target = os.environ.get("HARNESS_PREFLIGHT_OUTPUT")
    if target:
        Path(target).write_text(json.dumps({"sources": frozen_sources(), "report": report}, indent=2))


@pytest.mark.integration
@pytest.mark.live
async def test_harness_live_acceptance(pg_store, monkeypatch):
    from dotenv import load_dotenv

    load_dotenv(".env.local", override=False)
    assert os.environ.get("OPENAI_API_KEY"), "Existing authorized credential unavailable"
    directory = Path("var/acceptance") / MANIFEST["campaign"]
    preflight = json.loads((directory / "preflight.json").read_text())
    assert preflight["sources"] == frozen_sources(), "Rerun unpaid preflight for the current source"
    assert all(s["passed"] for s in preflight["report"]["scenarios"])
    report = await matrix(pg_store, monkeypatch, directory, True)
    assert len(report["scenarios"]) == 4 and all(s["passed"] for s in report["scenarios"]), (
        "See bounded results.json; terminal cases are not resubmitted"
    )
