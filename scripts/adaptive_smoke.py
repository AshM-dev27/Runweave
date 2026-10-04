"""One-shot adaptive sizing smoke with a fresh immutable physical-request ledger."""

import argparse
import asyncio
import hashlib
import json
import os
import sqlite3
import tempfile
from functools import partial
from pathlib import Path
from unittest.mock import patch

import httpx
from pydantic_ai.durable_exec.temporal import PydanticAIPlugin
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart, UserPromptPart
from pydantic_ai.usage import RequestUsage
from temporalio.client import WorkflowExecutionStatus
from temporalio.worker import Replayer

from agent_runtime.model_adapter import request_context
from agent_runtime.resources import RequestNotDispatched
from agent_runtime.schemas import AgentConfig
from agent_runtime.toolkit_workflow import ToolkitWorkflow
from scripts.contracts_smoke import api_client, isolated_store, sources
from scripts.harness_budget import policy as base_policy
from scripts.practical_benchmark import AccountedTransport, worker

DIRECTORY = Path("var/acceptance/adaptive-2026-10-04-v1")
CASES = ("small", "dossier", "long_answer")
MANIFEST = {
    "campaign": DIRECTORY.name,
    "version": 1,
    "limit": 12,
    "endpoint": "https://api.openai.com/v1/responses",
    "model": "gpt-5.6-luna",
    "reasoning": "none",
    "scenario_limits": dict.fromkeys(CASES, 4),
    "scenario_caps": {name: [1024, 2048, 4096] for name in CASES},
}


def reserve(value, context, cap, *, policy):
    policy.validate(value)
    scenario = context["scenario"]
    if scenario not in CASES or type(cap) is not int or cap not in policy.MANIFEST["scenario_caps"][scenario]:
        raise RuntimeError("Scenario outside campaign bounds")
    with sqlite3.connect(value, timeout=30) as db:
        db.execute("BEGIN IMMEDIATE")
        if db.execute("SELECT root_id FROM admissions WHERE scenario=?", (scenario,)).fetchone() != (
            context["root_id"],
        ):
            raise RuntimeError("Unadmitted root")
        if db.execute("SELECT 1 FROM terminals WHERE scenario=?", (scenario,)).fetchone():
            raise RuntimeError("Scenario terminal")
        if db.execute(
            "SELECT 1 FROM outcomes JOIN attempts ON attempts.id=outcomes.attempt_id WHERE scenario=? AND classification!='http_success'",
            (scenario,),
        ).fetchone():
            raise RuntimeError("Prior failed or ambiguous transport")
        if (
            db.execute("SELECT count(*) FROM attempts WHERE scenario=?", (scenario,)).fetchone()[0]
            >= policy.MANIFEST["scenario_limits"][scenario]
        ):
            raise RuntimeError("Scenario exhausted")
        return db.execute(
            "INSERT INTO attempts(scenario,run_id,root_id,operation_id,physical_attempt_id,output_cap) VALUES (?,?,?,?,?,?)",
            (
                scenario,
                context["run_id"],
                context["root_id"],
                context["operation_id"],
                context["attempt_id"],
                cap,
            ),
        ).lastrowid


def policy(frozen):
    guard = base_policy()
    guard.MANIFEST = {
        **MANIFEST,
        "source_sha256": hashlib.sha256(json.dumps(frozen, sort_keys=True).encode()).hexdigest(),
    }
    guard.TRIGGERS = {**guard.TRIGGERS, "hard_limit": guard.TRIGGERS["hard_limit"].replace(">=16", ">=12")}
    guard.reserve = partial(reserve, policy=guard)
    return guard


async def guard_preflight(frozen):
    with tempfile.TemporaryDirectory() as directory:
        guard, ledger = policy(frozen), Path(directory) / "requests.sqlite"
        guard.initialize(ledger)
        sent = []

        def respond(request):
            sent.append(request)
            return httpx.Response(200, json={})

        async with httpx.AsyncClient(
            transport=AccountedTransport(ledger, guard, httpx.MockTransport(respond))
        ) as client:
            for name in CASES:
                token = request_context.set({"scenario": name, "root_id": name, "run_id": name})
                body = {
                    "model": MANIFEST["model"],
                    "reasoning": {"effort": "none"},
                    "max_output_tokens": 1024,
                }
                try:
                    try:
                        await client.post(MANIFEST["endpoint"], json=body)
                    except RequestNotDispatched:
                        pass
                    else:
                        raise AssertionError("Unadmitted request sent")
                    guard.admit(ledger, name, name)
                    for changed in (
                        {"max_output_tokens": 1025},
                        {"max_output_tokens": 8192},
                        {"model": "other"},
                        {"reasoning": {"effort": "high"}},
                    ):
                        try:
                            await client.post(MANIFEST["endpoint"], json={**body, **changed})
                        except RequestNotDispatched:
                            pass
                        else:
                            raise AssertionError("Invalid request sent")
                    for cap in [1024, 2048, 4096, 1024]:
                        await client.post(MANIFEST["endpoint"], json={**body, "max_output_tokens": cap})
                    try:
                        await client.post(MANIFEST["endpoint"], json=body)
                    except RequestNotDispatched:
                        pass
                    else:
                        raise AssertionError("Scenario cap exceeded")
                    guard.finish(ledger, name, "passed")
                finally:
                    request_context.reset(token)
        assert len(sent) == guard.validate(ledger) == 12
        with sqlite3.connect(ledger) as db:
            try:
                db.execute("DELETE FROM attempts")
            except sqlite3.IntegrityError:
                pass
            else:
                raise AssertionError("Mutable ledger")


async def fake(messages, info):
    prompt = next(str(p.content) for m in messages for p in m.parts if isinstance(p, UserPromptPart))
    if prompt.startswith("long_answer"):
        if info.model_settings["max_tokens"] == 1024:
            return ModelResponse(
                parts=[TextPart("partial")],
                finish_reason="length",
                usage=RequestUsage(input_tokens=50, output_tokens=1000),
            )
        answer = json.dumps(list(range(1, 501)))
    elif prompt.startswith("dossier"):
        if not any(isinstance(p, ToolReturnPart) for m in messages for p in m.parts):
            aid = prompt.split("artifact ")[1].split()[0]
            return ModelResponse(parts=[ToolCallPart("document_read", {"artifact_id": aid, "max_lines": 3})])
        answer = "12%"
    else:
        answer = "12"
    return ModelResponse(
        parts=[ToolCallPart(info.output_tools[0].name, {"answer": answer})],
        usage=RequestUsage(input_tokens=50, output_tokens=100),
    )


async def run(live):
    os.umask(0o077)
    frozen = sources()
    guard, ledger = policy(frozen), DIRECTORY / "requests.sqlite"
    preflight = {"sources": frozen, "passed": list(CASES)}
    if live:
        if json.loads((DIRECTORY / "preflight.json").read_text()) != preflight:
            raise RuntimeError("Preflight/source mismatch")
        if ledger.exists() or ledger.with_suffix(".started").exists() or (DIRECTORY / "source.json").exists():
            raise RuntimeError("Campaign started; never resubmit or transfer allowance")
        from dotenv import load_dotenv

        load_dotenv(".env.local", override=False)
        if not os.environ.get("OPENAI_API_KEY"):
            raise RuntimeError("Authorized credential unavailable")
        with (DIRECTORY / "source.json").open("x") as file:
            json.dump(frozen, file, sort_keys=True, indent=2)
        guard.initialize(ledger)
    else:
        await guard_preflight(frozen)
    usage, results = [], []

    async def record(response):
        await response.aread()
        try:
            raw = response.json().get("usage", {})
        except ValueError:
            raw = {}
        item = {
            **(request_context.get() or {}),
            "status": response.status_code,
            "usage": {k: raw[k] for k in ("input_tokens", "output_tokens", "total_tokens") if k in raw},
        }
        usage.append(item)
        with (DIRECTORY / "usage.jsonl").open("a") as file:
            file.write(json.dumps(item) + "\n")
            file.flush()
            os.fsync(file.fileno())

    def transport(_):
        if sources() != frozen:
            raise RequestNotDispatched("evaluation_limit")
        return httpx.AsyncClient(
            transport=AccountedTransport(ledger, guard),
            trust_env=False,
            timeout=35,
            event_hooks={"response": [record]},
        )

    try:
        with (
            patch("agent_runtime.model_adapter.http_client_factory", transport)
            if live
            else patch("agent_runtime.toolkit_runtime.fake", fake)
        ):
            async with isolated_store() as (store, queue), api_client(store) as client:
                for name in CASES:
                    agent = await client.create_agent(
                        AgentConfig(
                            name=name,
                            provider="openai" if live else "fake",
                            model=MANIFEST["model"] if live else "deterministic",
                            tools=["document_read"],
                            max_requests=4,
                            instructions="Follow the user's exact requested answer format. Use document_read for supplied documents. Return the complete answer without abbreviations.",
                        )
                    )
                    files = []
                    if name == "dossier":
                        artifact = await client.upload(
                            b"Revenue growth: 12%\n" + b"Supporting archive entry.\n" * 3000,
                            filename="dossier.txt",
                        )
                        files = [artifact.id]
                        prompt = f"dossier: Read only the first three lines of artifact {artifact.id} using document_read. Return only the revenue growth percentage in your answer."
                    elif name == "long_answer":
                        prompt = "long_answer: In your final answer field, return a JSON array containing every integer from 1 through 500 in order. Do not abbreviate, omit numbers, add commentary, or use markdown."
                    else:
                        prompt = "small: Calculate five plus seven. Return only 12 in your final answer."
                    submitted = await client.submit(agent.id, prompt, artifact_ids=files)
                    initial = (await store.budget(submitted.id))["adaptive"]
                    assert initial["output_tokens"] == (2048 if name == "dossier" else 1024)
                    if live:
                        guard.admit(ledger, name, submitted.id)
                    result = {
                        "scenario": name,
                        "run_id": submitted.id,
                        "passed": False,
                        "initial_output_tokens": initial["output_tokens"],
                    }
                    async with worker(store, queue) as temporal:
                        try:
                            done = await client.result(submitted.id, timeout=100)
                            result.update(status=done.status, outcome=done.outcome)
                            assert done.outcome == "succeeded"
                            if name == "long_answer":
                                assert json.loads(done.answer) == list(range(1, 501))
                            else:
                                assert done.answer == ("12%" if name == "dossier" else "12")
                            budget = await store.budget(submitted.id)
                            result.update(
                                output_tokens=budget["adaptive"]["output_tokens"],
                                requests=budget["requests"],
                                tokens=budget["reported_tokens"],
                            )
                            if name == "long_answer":
                                assert budget["adaptive"]["output_tokens"] > 1024 and budget["requests"] >= 2
                            result["passed"] = True
                        except Exception as exc:
                            result.update(exception=type(exc).__name__)
                        finally:
                            try:
                                state = await client.get(submitted.id)
                                if state.status not in {"completed", "failed", "cancelled"}:
                                    await client.cancel(submitted.id)
                                done = await client.result(submitted.id, timeout=35)
                                handle = temporal.get_workflow_handle("run:" + submitted.id)
                                async with asyncio.timeout(25):
                                    while (await handle.describe()).status == WorkflowExecutionStatus.RUNNING:
                                        await asyncio.sleep(0.1)
                                await Replayer(
                                    workflows=[ToolkitWorkflow], plugins=[PydanticAIPlugin()]
                                ).replay_workflow(await handle.fetch_history())
                                result.update(
                                    cleanup=done.details.cleanup_state,
                                    workflow=(await handle.describe()).status.name,
                                    replay=True,
                                )
                            except Exception as exc:
                                store.contracts_cleanup_unresolved = True
                                result.update(
                                    passed=False, cleanup_error=type(exc).__name__, retained_schema=queue
                                )
                        results.append(result)
                        print(json.dumps(result), flush=True)
                        if live:
                            guard.finish(ledger, name, "passed" if result["passed"] else "failed")
    finally:
        if live:
            guard.reconcile_unknown(ledger)
            for name in CASES:
                guard.finish(ledger, name, "interrupted")
            print(json.dumps({"physical_requests": guard.validate(ledger), "usage": usage}), flush=True)
    assert frozen == sources()
    passed = len(results) == len(CASES) and all(r["passed"] for r in results)
    if not live and passed:
        DIRECTORY.mkdir(parents=True, exist_ok=True)
        with (DIRECTORY / "preflight.json").open("x") as file:
            json.dump(preflight, file, sort_keys=True, indent=2)
    return passed


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--preflight", action="store_true")
    modes.add_argument("--live", action="store_true")
    args = parser.parse_args()
    raise SystemExit(0 if asyncio.run(run(args.live)) else 1)
