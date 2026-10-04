"""Two-model paid checks, with frozen source and one admitted root per scenario."""

import asyncio
import hashlib
import json
import os
import sqlite3
from pathlib import Path

import pytest
import test_resources_live as foundation
from temporalio.exceptions import ApplicationError
from test_general_integration import backend
from test_general_semantic import complete, http_client, stub
from test_harness_extensions import Lookup, registration
from test_harness_integration import replay

from agent_runtime.extensions import ExtensionRegistry
from agent_runtime.general_contracts import GeneralPolicy
from agent_runtime.general_runtime import general_step
from agent_runtime.model_adapter import request_context
from agent_runtime.schemas import AgentConfig
from scripts.hardening_budget import MODELS, policy
from scripts.hardening_budget import client as guarded_client


def sources():
    return {
        str(p): hashlib.sha256(p.read_bytes()).hexdigest()
        for pattern in (
            "agent_runtime/*.py",
            "tests/*.py",
            "scripts/*.py",
            "config/*.json",
            "uv.lock",
            "pyproject.toml",
        )
        for p in Path().glob(pattern)
    }


async def matrix(store, monkeypatch, directory, model, live, *, guard=None):
    guard = guard or policy(model)
    before = sources()
    early = [name for name in guard.MANIFEST["scenario_limits"] if name in {"resume", "parallel"}]
    if early:
        with monkeypatch.context() as patch:
            patch.setattr(foundation, "MANIFEST", guard.MANIFEST)
            patch.setattr(foundation, "policy", lambda: guard)
            patch.setattr(foundation, "guarded_client", guarded_client)
            patch.setattr(foundation, "sources", sources)
            report = await foundation.matrix(store, patch, directory, live, early)
    else:
        directory.mkdir(parents=True, exist_ok=True)
        if live:
            with (directory / "source.json").open("x") as file:
                json.dump(before, file, indent=2)
            guard.initialize(directory / "requests.sqlite")
        report = {"campaign": guard.MANIFEST["campaign"], "live": live, "model": model, "scenarios": []}
    # Keep the earlier observations, including any failures, independently inspectable.
    ledger = directory / "requests.sqlite"
    report["scenarios"] = list(report["scenarios"])
    for name in [name for name in guard.MANIFEST["scenario_limits"] if name not in early]:
        with monkeypatch.context() as patch:
            usage = []

            async def record(response):
                await response.aread()
                context = request_context.get() or {}
                item = {
                    k: context.get(k) for k in ("scenario", "root_id", "run_id", "operation_id", "attempt_id")
                }
                item.update(status=response.status_code, usage=response.json().get("usage", {}))
                usage.append(item)
                with (directory / "usage.jsonl").open("a") as file:
                    file.write(json.dumps(item) + "\n")
                if name == "recovery" and response.status_code == 200:
                    # The provider completed; deliberately lose delivery to the runtime.
                    raise TimeoutError("simulated response loss after provider completion")

            def transport(_):
                value = guarded_client(ledger, guard)
                value.event_hooks["response"] = [record]
                return value

            if live:
                patch.setattr("agent_runtime.model_adapter.http_client_factory", transport)
            handler = Lookup()
            if name == "receipt":
                store.extensions = ExtensionRegistry(
                    {"tools": [registration()]}, handlers={"installed.lookup": handler}
                )
            tools = (
                ["workspace_verify"] if name == "batch" else ["customer_lookup"] if name == "receipt" else []
            )
            prompt = {
                "batch": "Run all registered checks together using verify check=all, then complete with a concise result.",
                "receipt": "Look up customer alice exactly once with customer_lookup, then tell me their plan.",
                "recovery": "Return the word ready.",
            }[name]
            task = None
            async with http_client(store) as api:
                workspace = (
                    await api.workspace_create({"a.txt": b"a", "b.txt": b"b"}) if name == "batch" else None
                )
                if workspace:
                    task = {
                        "outcome": "Verify both files",
                        "criteria": [
                            {
                                "id": "files",
                                "statement": "Both files contain the expected bytes",
                                "evidence_policy": "check",
                                "checks": [
                                    {"id": "a", "kind": "bytes", "path": "a.txt", "expected": "YQ=="},
                                    {"id": "b", "kind": "bytes", "path": "b.txt", "expected": "Yg=="},
                                ],
                            }
                        ],
                    }
                agent = await api.create_agent(
                    AgentConfig(
                        name=name,
                        provider="openai" if live else "fake",
                        model=model if live else "deterministic",
                        tools=tools,
                        max_tokens=1024,
                        general=GeneralPolicy(resources={"max_pause_seconds": 10}),
                    )
                )
                run = await api.submit(
                    agent.id, ("" if live else "semantic: ") + prompt, workspace=workspace, task=task
                )
            if live:
                guard.admit(ledger, name, run.id)
            if not live:
                if name == "recovery":

                    def lost(*_):
                        raise TimeoutError("simulated ambiguous request")

                    stub(patch, lost)
                else:
                    actions = (
                        [{"kind": "verify", "check": "all"}, complete()]
                        if workspace
                        else [
                            {
                                "kind": "invoke",
                                "capability": "customer_lookup",
                                "arguments": {"customer": "alice"},
                            },
                            {**complete(), "answer": "alice has the basic plan."},
                        ]
                    )
                    stub(patch, actions)
            item = {"scenario": name, "root_id": run.id, "passed": False}
            try:
                if name == "recovery":
                    # Exactly one activity invocation; no SDK/workflow retry of the paid request.
                    with pytest.raises(ApplicationError):
                        await general_step({"run_id": run.id, "completion_loop": 2, "batch_checks": True})
                    async with http_client(store) as api:
                        unresolved = (await api.recovery(run.id))["unresolved"]
                        assert len(unresolved) == 1
                        item["reserved_before"] = unresolved[0]["reserved_tokens"]
                        await api.cancel(run.id)
                        reported = usage[0]["usage"]["total_tokens"] if live else 123
                        receipt = await api.reconcile(
                            run.id,
                            {
                                "kind": "model_usage",
                                "target_id": unresolved[0]["target_id"],
                                "reported_tokens": reported,
                                "evidence_ref": "local-validation/provider-usage",
                            },
                            idempotency_key="recover",
                        )
                    async with backend(store, patch) as (api, temporal):
                        async with asyncio.timeout(30):
                            while (await api.reconciliation(run.id, receipt["id"]))["status"] != "complete":
                                await asyncio.sleep(0.05)
                        item["reconciliation"] = await api.reconciliation(run.id, receipt["id"])
                        item["usage_after"] = (await api.resources(run.id))["usage"]
                        assert item["usage_after"]["reserved_tokens"] == 0
                        assert item["usage_after"]["reported_tokens"] == reported
                        assert item["usage_after"]["model_attempts"] == 1
                        assert (await api.get(run.id)).status == "cancelled"
                        from pydantic_ai.durable_exec.temporal import PydanticAIPlugin
                        from temporalio.worker import Replayer

                        from agent_runtime.general_workflow import ReconciliationWorkflow

                        handle = temporal.get_workflow_handle(receipt["id"])
                        await handle.result()
                        await Replayer(
                            workflows=[ReconciliationWorkflow], plugins=[PydanticAIPlugin()]
                        ).replay_workflow(await handle.fetch_history())
                        item["replayed"] = True
                else:
                    async with backend(store, patch) as (api, temporal):
                        result = await api.wait(run.id, timeout=100)
                        item.update(
                            status=result.status,
                            error=result.error,
                            output=result.output.model_dump() if result.output else None,
                            resources=await api.resources(run.id),
                            operations=await api.operations(run.id),
                        )
                        assert result.status == "completed"
                        if name == "batch":
                            checks = (await api.verifications(run.id))["items"]
                            item["checks"] = checks
                            assert len(checks) == 2 and all(c["outcome"] == "pass" for c in checks)
                            assert any(
                                o["result"]
                                and o["result"].get("action", {}).get("arguments", {}).get("check_id")
                                == "$pending"
                                for o in item["operations"]["items"]
                            )
                        else:
                            item["tool_executions"] = len(handler.calls)
                            assert len(handler.calls) == 1 and "basic" in result.output.answer.lower()
                        await temporal.get_workflow_handle("run:" + run.id).result()
                        await replay(temporal, run.id)
                        item["replayed"] = True
                item["passed"] = True
            except Exception as exc:
                item["exception_type"] = type(exc).__name__
                async with http_client(store) as api:
                    await api.cancel(run.id)
            finally:
                report["scenarios"].append(item)
                if live:
                    guard.finish(ledger, name, "passed" if item["passed"] else "failed")
    report["source_unchanged"] = before == sources()
    if live:
        guard.reconcile_unknown(ledger)
        report["physical_requests"] = guard.validate(ledger)
        with sqlite3.connect(ledger) as db:
            report["outcomes"] = db.execute(
                "SELECT classification,count(*) FROM outcomes GROUP BY classification"
            ).fetchall()
    (directory / "matrix.json").write_text(json.dumps(report, indent=2) + "\n")
    assert report["source_unchanged"]
    return report


@pytest.mark.integration
@pytest.mark.parametrize("model", MODELS)
async def test_hardening_preflight(pg_store, monkeypatch, tmp_path, model):
    report = await matrix(pg_store, monkeypatch, tmp_path / model, model, False)
    assert len(report["scenarios"]) == 5 and all(s["passed"] for s in report["scenarios"]), report
    if root := os.environ.get("HARDENING_PREFLIGHT_OUTPUT"):
        target = Path(root) / policy(model).MANIFEST["campaign"]
        target.mkdir(parents=True, exist_ok=True)
        with (target / "preflight.json").open("x") as file:
            json.dump({"sources": sources(), "report": report}, file, indent=2)


@pytest.mark.integration
@pytest.mark.live
@pytest.mark.parametrize("model", MODELS)
async def test_hardening_live(pg_store, monkeypatch, model):
    from dotenv import load_dotenv

    load_dotenv(".env.local", override=False)
    assert os.environ.get("OPENAI_API_KEY")
    target = Path("var/acceptance") / policy(model).MANIFEST["campaign"]
    preflight = json.loads((target / "preflight.json").read_text())
    assert preflight["sources"] == sources()
    assert all(s["passed"] for s in preflight["report"]["scenarios"])
    report = await matrix(pg_store, monkeypatch, target, model, True)
    assert len(report["scenarios"]) == 5 and all(s["passed"] for s in report["scenarios"]), (
        "Terminal evidence retained; do not resubmit this campaign"
    )
