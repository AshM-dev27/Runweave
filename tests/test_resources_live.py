"""Source-frozen resource smoke campaign; actual calls require explicit --live."""

import asyncio
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
from agent_runtime.model_adapter import request_context
from agent_runtime.schemas import AgentConfig
from scripts.harness_budget import client as guarded_client
from scripts.resources_budget import MANIFEST, policy


def sources():
    return {
        str(p): hashlib.sha256(p.read_bytes()).hexdigest()
        for pattern in (
            "agent_runtime/*.py",
            "config/*.json",
            "tests/test_resources*.py",
            "tests/test_general_integration.py",
            "tests/test_general_semantic.py",
            "tests/test_harness_integration.py",
            "tests/test_harness_review.py",
            "tests/conftest.py",
            "scripts/*budget.py",
            "scripts/completion_loop_fixtures.py",
            "uv.lock",
        )
        for p in Path().glob(pattern)
    }


async def matrix(store, monkeypatch, directory, live):
    before = sources()
    directory.mkdir(parents=True, exist_ok=True)
    guard, ledger = policy(), directory / "requests.sqlite"
    if live:
        with (directory / "source.json").open("x") as file:
            json.dump(before, file, indent=2)
        guard.initialize(ledger)

        async def usage(response):
            await response.aread()
            try:
                data = response.json()
            except ValueError:
                return
            context = request_context.get() or {}
            item = {k: context.get(k) for k in ("scenario", "root_id", "run_id", "operation_id")}
            item.update(status=response.status_code, usage=data.get("usage", {}))
            with (directory / "usage.jsonl").open("a") as file:
                file.write(json.dumps(item) + "\n")

        def transport(_):
            client = guarded_client(ledger, guard)
            client.event_hooks["response"] = [usage]
            return client

        monkeypatch.setattr("agent_runtime.model_adapter.http_client_factory", transport)
    results = []
    root_ids = []
    try:
        for name in MANIFEST["scenario_limits"]:
            with monkeypatch.context() as patch:
                review_case = name.startswith("review_")
                parallel = name == "parallel"
                if parallel:
                    from scripts.completion_loop_fixtures import fixture

                    f = fixture("parallel")
                    tools, task = f["tools"], f["task"]
                    prompt = (
                        f["prompt"]
                        + " After merging both outputs, propose complete; completion runs pending registered checks automatically."
                    )
                    general = GeneralPolicy(
                        resources={"max_pause_seconds": 60},
                        limits={"model_attempts": 16},
                        delegation={"tools": tools, "limits": {"model_attempts": 1}},
                    )
                else:
                    tools = [] if review_case else ["add"]
                    prompt = "Calculate 5 + 7. " + (
                        "Return 12."
                        if review_case
                        else "Call add once before completing, then answer exactly 12."
                    )
                    task = {
                        "outcome": "Compute 5 + 7",
                        "criteria": [{"id": "sum", "statement": "Answer correctly with the sum of 5 and 7."}],
                    }
                    general = GeneralPolicy(
                        resources={"max_pause_seconds": 60},
                        limits={"model_attempts": 1},
                        review={"provider": "openai", "model": MANIFEST["model"]} if review_case else None,
                    )
                use_fake_actor = not live or review_case
                async with http_client(store) as client:
                    config = AgentConfig(
                        name=name,
                        provider="fake" if use_fake_actor else "openai",
                        model="deterministic" if use_fake_actor else MANIFEST["model"],
                        tools=tools,
                        max_tokens=1024,
                        general=general,
                    )
                    agent = await client.create_agent(config)
                    workspace = await client.workspace_create({}) if parallel else None
                    run = await client.submit(
                        agent.id,
                        ("semantic: " if use_fake_actor else "") + prompt,
                        workspace=workspace,
                        task=task,
                    )
                root_ids.append(run.id)
                if live:
                    guard.admit(ledger, name, run.id)
                if review_case:
                    expected = "pass" if name == "review_pass" else "repair"
                    stub(
                        patch,
                        [
                            {**complete(), "answer": "12" if expected == "pass" else "13"},
                            {
                                "kind": "blocked",
                                "reason": "Stop after rejection of the deliberately wrong candidate.",
                            },
                        ],
                    )
                    if not live:
                        reviewer(patch, expected)
                elif not live and not parallel:
                    stub(
                        patch,
                        [{"kind": "invoke", "capability": "add", "arguments": {"a": 5, "b": 7}}, complete()],
                    )
                elif not live:
                    counts = {}

                    def next_action(context, _):
                        text = context["input"]
                        i = counts.get(text, 0)
                        counts[text] = i + 1
                        if text.startswith("semantic: child"):
                            side = "left" if "left" in text else "right"
                            return (
                                complete()
                                if i
                                else {
                                    "kind": "write",
                                    "files": [
                                        {
                                            "path": side + ".txt",
                                            "content_base64": base64.b64encode(
                                                f["expected"][side + ".txt"]
                                            ).decode(),
                                        }
                                    ],
                                }
                            )
                        return [
                            {
                                "kind": "assign",
                                "assignments": [
                                    {
                                        "role": side,
                                        "objective": "semantic: child " + side,
                                        "acceptance": ["Write requested file"],
                                        "capabilities": tools,
                                        "outputs": [side + ".txt"],
                                    }
                                    for side in ("left", "right")
                                ],
                            },
                            {"kind": "join", "children": ["d0", "d1"]},
                            {"kind": "merge", "child": "d0"},
                            {"kind": "merge", "child": "d1"},
                            {"kind": "complete", "answer": "Both outputs verified."},
                        ][i]

                    stub(patch, next_action)
                async with backend(store, patch) as (client, temporal):
                    item = {
                        "scenario": name,
                        "root_id": run.id,
                        "live_actor": live and not review_case,
                        "live_reviewer": live and review_case,
                        "passed": False,
                    }
                    try:
                        if not parallel:
                            paused = await client.wait(run.id, timeout=100)
                            item["paused"] = paused.status == "paused_budget"
                            state = await client.resources(run.id)
                            item["before_resume"] = state
                            if item["paused"]:
                                update = dict(
                                    expected_version=state["version"],
                                    limits={"model_attempts": 3},
                                    idempotency_key=name + "-resume",
                                )
                                first = await client.update_resources(run.id, **update)
                                assert first == await client.update_resources(run.id, **update)
                            finished = await client.wait(run.id, timeout=100, stop_at_budget=False)
                        else:
                            finished = await client.wait(run.id, timeout=240)
                        item.update(
                            status=finished.status,
                            error=finished.error,
                            budget=await client.budget(run.id),
                            resources=await client.resources(run.id),
                            operations=await client.operations(run.id),
                        )
                        if review_case:
                            item["verdicts"] = [
                                e.data["verdict"]
                                for e in await store.events(run.id)
                                if e.type == "completion.reviewed"
                            ]
                            item["passed"] = (
                                item["paused"]
                                and item["verdicts"] == [expected]
                                and finished.status == ("completed" if expected == "pass" else "failed")
                            )
                        elif not parallel:
                            item["passed"] = (
                                item["paused"]
                                and finished.status == "completed"
                                and finished.output.answer.strip() == "12"
                                and item["budget"]["tool_calls"] == 1
                            )
                        else:
                            actual = {}
                            for path in f["expected"]:
                                try:
                                    actual[path] = (
                                        await client.workspace_read(
                                            workspace["workspace_id"], finished.workspace["revision_id"], path
                                        )
                                    ).decode()
                                except Exception:
                                    actual[path] = None
                            children = await client.children(run.id)
                            item["outputs"] = actual
                            item["children"] = []
                            for c in children:
                                data = await store.general(c.id)
                                item["children"].append(
                                    {
                                        "id": c.id,
                                        "status": c.status,
                                        "error": c.error,
                                        "estimate": data["local_limits"]["model_attempts"],
                                        "actual_model_attempts": data.get("local_usage", {}).get(
                                            "model_attempts", 0
                                        ),
                                    }
                                )
                            item["passed"] = (
                                finished.status == "completed"
                                and actual == {k: v.decode() for k, v in f["expected"].items()}
                                and len(children) == 2
                                and all(
                                    c["status"] == "completed" and c["actual_model_attempts"] > c["estimate"]
                                    for c in item["children"]
                                )
                            )
                        if finished.status not in {"completed", "failed", "cancelled"}:
                            await client.cancel(run.id)
                        await asyncio.wait_for(temporal.get_workflow_handle("run:" + run.id).result(), 20)
                        await replay(temporal, run.id)
                        item["replayed"] = True
                    except Exception as exc:
                        item.update(passed=False, exception_type=type(exc).__name__)
                        # Leave a failed scenario terminal instead of trying another paid root.
                        await client.cancel(run.id)
                        try:
                            await asyncio.wait_for(temporal.get_workflow_handle("run:" + run.id).result(), 20)
                        except Exception:
                            pass
                    finally:
                        results.append(item)
                        if live:
                            guard.finish(ledger, name, "passed" if item["passed"] else "failed")
    finally:
        report = {
            "campaign": MANIFEST["campaign"],
            "live": live,
            "model": MANIFEST["model"] if live else "FunctionModel",
            "source_unchanged": before == sources(),
            "roots": root_ids,
            "scenarios": results,
        }
        if live:
            guard.reconcile_unknown(ledger)
            report["physical_requests"] = guard.validate(ledger)
            with sqlite3.connect(ledger) as db:
                report["outcomes"] = db.execute(
                    "SELECT classification,count(*) FROM outcomes GROUP BY classification"
                ).fetchall()
        (directory / "results.json").write_text(json.dumps(report, indent=2) + "\n")
    assert report["source_unchanged"]
    return report


@pytest.mark.integration
async def test_resource_campaign_preflight(pg_store, monkeypatch, tmp_path):
    report = await matrix(pg_store, monkeypatch, tmp_path / "preflight", False)
    assert len(report["scenarios"]) == 4 and all(s["passed"] for s in report["scenarios"]), report
    target = os.environ.get("RESOURCE_PREFLIGHT_OUTPUT")
    if target:
        with Path(target).open("x") as file:
            json.dump({"sources": sources(), "report": report}, file, indent=2)


@pytest.mark.integration
@pytest.mark.live
async def test_resource_live_campaign(pg_store, monkeypatch):
    from dotenv import load_dotenv

    load_dotenv(".env.local", override=False)
    assert os.environ.get("OPENAI_API_KEY"), "Existing authorized credential unavailable"
    directory = Path("var/acceptance") / MANIFEST["campaign"]
    preflight = json.loads((directory / "preflight.json").read_text())
    assert preflight["sources"] == sources(), "Fresh preflight must match the current source"
    assert all(s["passed"] for s in preflight["report"]["scenarios"]), "Unpaid preflight must pass"
    report = await matrix(pg_store, monkeypatch, directory, True)
    assert len(report["scenarios"]) == 4 and all(s["passed"] for s in report["scenarios"]), (
        "See terminal results; do not resubmit failed roots"
    )


def test_resource_campaign_guard_is_fixed(tmp_path):
    guard = policy()
    path = tmp_path / "guard.sqlite"
    guard.initialize(path)
    for name, limit in MANIFEST["scenario_limits"].items():
        guard.admit(path, name, name)
        for i in range(limit):
            # Separate allowed workers while retaining the fixed tree total.
            rid = name if name != "parallel" or i < 8 else name + "-child-" + str((i - 8) // 3)
            guard.reserve(
                path,
                dict(scenario=name, root_id=name, run_id=rid, operation_id=str(i), attempt_id=str(i)),
                1024,
            )
        with pytest.raises(RuntimeError):
            guard.reserve(
                path,
                dict(
                    scenario=name, root_id=name, run_id=name, operation_id="overflow", attempt_id="overflow"
                ),
                1024,
            )
        guard.finish(path, name, "passed")
        with pytest.raises(RuntimeError):
            guard.admit(path, name, "another-root")
    assert guard.validate(path) == 17
