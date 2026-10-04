"""Fresh bounded outcome smoke; historical campaigns are never reopened."""

import argparse
import asyncio
import hashlib
import json
import os
import sqlite3
import tempfile
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

import httpx
from temporalio.client import WorkflowExecutionStatus

from agent_runtime.model_adapter import request_context
from agent_runtime.resources import RequestNotDispatched
from agent_runtime.schemas import AgentConfig
from scripts.contracts_smoke import api_client, isolated_store, sources
from scripts.harness_budget import policy as base_policy
from scripts.practical_benchmark import AccountedTransport, worker

DIRECTORY = Path("var/acceptance/outcomes-2026-10-04-v1")
CASES = {"allowed": "succeeded", "denied": "blocked"}
MANIFEST = {
    "campaign": DIRECTORY.name,
    "version": 1,
    "limit": 6,
    "endpoint": "https://api.openai.com/v1/responses",
    "model": "gpt-5.6-luna",
    "reasoning": "none",
    "scenario_limits": dict.fromkeys(CASES, 3),
    "scenario_caps": dict.fromkeys(CASES, 1024),
}


def policy(frozen):
    guard = base_policy()
    guard.MANIFEST = {
        **MANIFEST,
        "source_sha256": hashlib.sha256(json.dumps(frozen, sort_keys=True).encode()).hexdigest(),
    }
    guard.TRIGGERS = {**guard.TRIGGERS, "hard_limit": guard.TRIGGERS["hard_limit"].replace(">=16", ">=6")}
    return guard


async def guard_preflight(frozen):
    with tempfile.TemporaryDirectory() as directory:
        guard = policy(frozen)
        ledger = Path(directory) / "requests.sqlite"
        guard.initialize(ledger)
        sent = []

        def respond(request):
            sent.append(request)
            return httpx.Response(200, json={})

        async with httpx.AsyncClient(
            transport=AccountedTransport(ledger, guard, httpx.MockTransport(respond))
        ) as client:
            for name in CASES:
                guard.admit(ledger, name, name)
                token = request_context.set({"scenario": name, "root_id": name, "run_id": name})
                try:
                    body = {
                        "model": MANIFEST["model"],
                        "reasoning": {"effort": "none"},
                        "max_output_tokens": 1024,
                    }
                    for n in range(4):
                        try:
                            await client.post(MANIFEST["endpoint"], json=body)
                        except RequestNotDispatched:
                            assert n == 3
                        else:
                            assert n < 3
                    guard.finish(ledger, name, "passed")
                finally:
                    request_context.reset(token)
        assert len(sent) == guard.validate(ledger) == 6


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
        if frozen != sources():
            raise RequestNotDispatched("evaluation_limit")
        return httpx.AsyncClient(
            transport=AccountedTransport(ledger, guard),
            trust_env=False,
            timeout=35,
            event_hooks={"response": [record]},
        )

    try:
        with patch("agent_runtime.model_adapter.http_client_factory", transport) if live else nullcontext():
            async with isolated_store() as (store, queue), api_client(store) as client:
                for name, expected in CASES.items():
                    agent = await client.create_agent(
                        AgentConfig(
                            name=name,
                            provider="openai" if live else "fake",
                            model=MANIFEST["model"] if live else "deterministic",
                            tools=["document_read"],
                            instructions="Use document_read when asked to read a file. Report an access failure honestly; never claim to have read unavailable data.",
                            max_requests=3,
                            max_tokens=1024,
                            max_tool_calls=3,
                            max_total_tokens=16000,
                        )
                    )
                    artifact = await client.upload(
                        b"Quarterly revenue increased by 12%.\n", filename="quarter.txt"
                    )
                    prompt = (
                        f"Use document_read to read artifact {artifact.id}. State the revenue change if it can be read; otherwise report the access error."
                        if live
                        else "toolkit:"
                        + json.dumps([{"tool": "document_read", "arguments": {"artifact_id": artifact.id}}])
                    )
                    submitted = await client.submit(
                        agent.id, prompt, artifact_ids=[artifact.id] if name == "allowed" else []
                    )
                    if live:
                        guard.admit(ledger, name, submitted.id)
                    result = {"scenario": name, "run_id": submitted.id, "passed": False}
                    async with worker(store, queue) as temporal:
                        try:
                            done = await client.result(submitted.id, timeout=90)
                            result.update(
                                status=done.status,
                                outcome=done.outcome,
                                reason=done.outcome_reason,
                                answer=done.answer,
                            )
                            assert done.status == "completed" and done.outcome == expected
                            if name == "allowed":
                                assert "12%" in done.answer
                            else:
                                assert done.outcome_reason == "artifact_not_authorized"
                                assert "12%" not in done.answer
                            terminal = [
                                e for e in await store.events(done.run_id) if e.type == "run.completed"
                            ]
                            assert len(terminal) == 1 and terminal[0].data["outcome"] == expected
                            result["passed"] = True
                        except Exception as exc:
                            result.update(passed=False, exception=type(exc).__name__)
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
                                result.update(
                                    cleanup=done.details.cleanup_state,
                                    workflow=(await handle.describe()).status.name,
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
            with sqlite3.connect(ledger) as database:
                outcomes = database.execute(
                    "SELECT classification,count(*) FROM outcomes GROUP BY classification"
                ).fetchall()
            print(
                json.dumps(
                    {"physical_requests": guard.validate(ledger), "usage": usage, "outcomes": outcomes}
                ),
                flush=True,
            )
    assert frozen == sources()
    passed = len(results) == len(CASES) and all(result["passed"] for result in results)
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
