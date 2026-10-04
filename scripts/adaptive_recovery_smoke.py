"""Fresh three-request follow-up: 750 integers force output growth beyond 1,024 tokens."""

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
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.usage import RequestUsage
from temporalio.client import WorkflowExecutionStatus
from temporalio.worker import Replayer

from agent_runtime.model_adapter import request_context
from agent_runtime.resources import RequestNotDispatched
from agent_runtime.schemas import AgentConfig
from agent_runtime.toolkit_workflow import ToolkitWorkflow
from scripts.adaptive_smoke import reserve
from scripts.contracts_smoke import api_client, isolated_store, sources
from scripts.harness_budget import policy as base_policy
from scripts.practical_benchmark import AccountedTransport, worker

DIRECTORY = Path("var/acceptance/adaptive-recovery-2026-10-04-v1")
NAME = "long_answer"
MANIFEST = {
    "campaign": DIRECTORY.name,
    "version": 1,
    "limit": 3,
    "endpoint": "https://api.openai.com/v1/responses",
    "model": "gpt-5.6-luna",
    "reasoning": "none",
    "scenario_limits": {NAME: 3},
    "scenario_caps": {NAME: [1024, 2048, 4096]},
}


def policy(frozen):
    guard = base_policy()
    guard.MANIFEST = {
        **MANIFEST,
        "source_sha256": hashlib.sha256(json.dumps(frozen, sort_keys=True).encode()).hexdigest(),
    }
    guard.TRIGGERS = {**guard.TRIGGERS, "hard_limit": guard.TRIGGERS["hard_limit"].replace(">=16", ">=3")}
    guard.reserve = partial(reserve, policy=guard)
    return guard


async def guard_preflight(frozen):
    with tempfile.TemporaryDirectory() as directory:
        guard, ledger = policy(frozen), Path(directory) / "requests.sqlite"
        guard.initialize(ledger)
        guard.admit(ledger, NAME, "root")
        context = {
            "scenario": NAME,
            "root_id": "root",
            "run_id": "root",
            "operation_id": "model",
            "attempt_id": "attempt",
        }
        for cap in (0, 1025, 8192, None):
            try:
                guard.reserve(ledger, context, cap)
            except RuntimeError:
                pass
            else:
                raise AssertionError("Invalid cap admitted")
        for cap in (1024, 2048, 4096):
            identity = guard.reserve(ledger, context, cap)
            with sqlite3.connect(ledger) as db:
                db.execute("INSERT INTO outcomes VALUES (?,?)", (identity, "http_success"))
        try:
            guard.reserve(ledger, context, 1024)
        except RuntimeError:
            pass
        else:
            raise AssertionError("Physical cap exceeded")
        assert guard.validate(ledger) == 3
        guard.finish(ledger, NAME, "passed")


async def fake(messages, info):
    if info.model_settings["max_tokens"] == 1024:
        return ModelResponse(
            parts=[TextPart("partial")],
            finish_reason="length",
            usage=RequestUsage(input_tokens=50, output_tokens=1000),
        )
    return ModelResponse(
        parts=[ToolCallPart(info.output_tools[0].name, {"answer": json.dumps(list(range(1, 751)))})],
        usage=RequestUsage(input_tokens=50, output_tokens=1520),
    )


async def run(live):
    os.umask(0o077)
    frozen = sources()
    guard, ledger = policy(frozen), DIRECTORY / "requests.sqlite"
    preflight = {"sources": frozen, "passed": [NAME]}
    if live:
        if json.loads((DIRECTORY / "preflight.json").read_text()) != preflight:
            raise RuntimeError("Preflight/source mismatch")
        if ledger.exists() or ledger.with_suffix(".started").exists() or (DIRECTORY / "source.json").exists():
            raise RuntimeError("Campaign already started; never resubmit")
        from dotenv import load_dotenv

        load_dotenv(".env.local", override=False)
        if not os.environ.get("OPENAI_API_KEY"):
            raise RuntimeError("Authorized credential unavailable")
        with (DIRECTORY / "source.json").open("x") as file:
            json.dump(frozen, file, sort_keys=True, indent=2)
        guard.initialize(ledger)
    else:
        await guard_preflight(frozen)
    usage = []

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
        if frozen != sources():
            raise RequestNotDispatched("evaluation_limit")
        return httpx.AsyncClient(
            transport=AccountedTransport(ledger, guard),
            trust_env=False,
            timeout=35,
            event_hooks={"response": [record]},
        )

    result = {"scenario": NAME, "passed": False}
    try:
        with (
            patch("agent_runtime.model_adapter.http_client_factory", transport)
            if live
            else patch("agent_runtime.toolkit_runtime.fake", fake)
        ):
            async with isolated_store() as (store, queue), api_client(store) as client:
                agent = await client.create_agent(
                    AgentConfig(
                        name=NAME,
                        provider="openai" if live else "fake",
                        model=MANIFEST["model"] if live else "deterministic",
                        tools=["document_read"],
                        max_requests=3,
                        instructions="Return the exact requested complete answer. Never abbreviate or omit entries.",
                    )
                )
                run = await client.submit(
                    agent.id,
                    "In your final answer field, return a JSON array containing every integer from 1 through 750 in order. Do not abbreviate, omit numbers, add commentary, or use markdown.",
                )
                assert (await store.budget(run.id))["adaptive"]["output_tokens"] == 1024
                if live:
                    guard.admit(ledger, NAME, run.id)
                async with worker(store, queue) as temporal:
                    try:
                        done = await client.result(run.id, timeout=100)
                        assert done.outcome == "succeeded"
                        assert json.loads(done.answer) == list(range(1, 751))
                        budget = await store.budget(run.id)
                        assert budget["requests"] >= 2 and budget["adaptive"]["output_tokens"] >= 2048
                        assert budget["reserved_tokens"] == 0
                        result.update(
                            passed=True,
                            run_id=run.id,
                            requests=budget["requests"],
                            tokens=budget["reported_tokens"],
                            output_tokens=budget["adaptive"]["output_tokens"],
                        )
                    except Exception as exc:
                        result.update(exception=type(exc).__name__)
                    finally:
                        try:
                            state = await client.get(run.id)
                            if state.status not in {"completed", "failed", "cancelled"}:
                                await client.cancel(run.id)
                            done = await client.result(run.id, timeout=35)
                            handle = temporal.get_workflow_handle("run:" + run.id)
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
                    print(json.dumps(result), flush=True)
    finally:
        if live:
            guard.reconcile_unknown(ledger)
            guard.finish(ledger, NAME, "passed" if result["passed"] else "failed")
            print(json.dumps({"physical_requests": guard.validate(ledger), "usage": usage}), flush=True)
    assert frozen == sources()
    if not live and result["passed"]:
        DIRECTORY.mkdir(parents=True, exist_ok=True)
        with (DIRECTORY / "preflight.json").open("x") as file:
            json.dump(preflight, file, sort_keys=True, indent=2)
    return result["passed"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--preflight", action="store_true")
    modes.add_argument("--live", action="store_true")
    args = parser.parse_args()
    raise SystemExit(0 if asyncio.run(run(args.live)) else 1)
