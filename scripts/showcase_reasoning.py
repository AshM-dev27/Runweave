"""Fresh Luna medium-reasoning benchmark; historical campaigns remain terminal."""

import argparse
import asyncio
import json
import os
import sqlite3
import tempfile
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from unittest.mock import patch

import httpx

from agent_runtime.model_adapter import request_context
from agent_runtime.registry import Registry
from agent_runtime.resources import RequestNotDispatched
from agent_runtime.schemas import AgentConfig
from scripts import showcase_accuracy as benchmark
from scripts.contracts_smoke import isolated_store
from scripts.hardening_budget import Transport
from scripts.showcase_model_comparison import checked_baseline, model_summary

DIRECTORY = Path("var/benchmarks/luna-reasoning-2026-10-04-v1")
OUTPUT_CAP = 8192
MANIFEST = {
    **benchmark.MANIFEST,
    "campaign": DIRECTORY.name,
    "reasoning": "medium",
    "scenario_caps": dict.fromkeys(benchmark.CASES, OUTPUT_CAP),
}


def registrations(registry):
    entries = []
    for key, registration in registry.entries.items():
        entry = registration.model_dump()
        if key == ("openai", MANIFEST["model"]):
            entry.update(reasoning_effort="medium", max_output_tokens=OUTPUT_CAP)
        entries.append(entry)
    return Registry(entries)


def agent_config(**kwargs):
    if kwargs["provider"] == "openai":
        kwargs["max_tokens"] = OUTPUT_CAP
    return AgentConfig(**kwargs)


class CaptureTransport(Transport):
    async def handle_async_request(self, request):
        body = json.loads(await request.aread())
        response = await super().handle_async_request(request)
        await response.aread()
        try:
            raw = response.json()
        except ValueError:
            raw = {}
        usage = raw.get("usage") or {}
        record = {
            **(request_context.get() or {}),
            "request": {
                "model": body.get("model"),
                "reasoning_effort": (body.get("reasoning") or {}).get("effort"),
                "max_output_tokens": body.get("max_output_tokens"),
            },
            "http_status": response.status_code,
            "response_status": raw.get("status"),
            "incomplete_reason": (raw.get("incomplete_details") or {}).get("reason"),
            "input_tokens": usage.get("input_tokens"),
            "output_tokens": usage.get("output_tokens"),
            "reasoning_tokens": (usage.get("output_tokens_details") or {}).get("reasoning_tokens"),
            "cached_input_tokens": (usage.get("input_tokens_details") or {}).get("cached_tokens"),
        }
        with (DIRECTORY / "wire-metadata.jsonl").open("a") as file:
            file.write(json.dumps(record) + "\n")
            file.flush()
            os.fsync(file.fileno())
        return response


async def check_guard(frozen):
    """Prove reasoning, output and physical-call caps without provider calls."""
    guard = benchmark.policy(frozen)
    with tempfile.TemporaryDirectory(prefix="luna-reasoning-guard-") as tmp:
        ledger = Path(tmp) / "requests.sqlite"
        guard.initialize(ledger)
        sent = []

        def reply(request):
            sent.append(request)
            return httpx.Response(200, json={})

        async with httpx.AsyncClient(
            transport=Transport(ledger, guard, httpx.MockTransport(reply))
        ) as client:
            for name in benchmark.CASES:
                guard.admit(ledger, name, name)
                valid = {
                    "model": MANIFEST["model"],
                    "reasoning": {"effort": "medium"},
                    "max_output_tokens": OUTPUT_CAP,
                }
                probes = [
                    ({**valid, "model": "gpt-5.6-sol"}, False),
                    ({**valid, "reasoning": {"effort": "none"}}, False),
                    ({**valid, "max_output_tokens": OUTPUT_CAP + 1}, False),
                    (valid, True),
                    (valid, True),
                    (valid, True),
                    (valid, False),
                ]
                for index, (body, allowed) in enumerate(probes):
                    token = request_context.set(
                        {
                            "scenario": name,
                            "root_id": name,
                            "run_id": name,
                            "operation_id": str(index),
                            "attempt_id": str(index),
                        }
                    )
                    try:
                        try:
                            await client.post(MANIFEST["endpoint"], json=body)
                        except RequestNotDispatched:
                            assert not allowed, "Valid request rejected"
                        else:
                            assert allowed, "Disallowed request reached transport"
                    finally:
                        request_context.reset(token)
                guard.finish(ledger, name, "preflight")
        assert len(sent) == guard.validate(ledger) == MANIFEST["limit"] == 30
        with sqlite3.connect(ledger) as db:
            try:
                db.execute("DELETE FROM attempts")
            except sqlite3.IntegrityError:
                pass
            else:
                raise AssertionError("Ledger was mutable")


@contextmanager
def arm():
    @asynccontextmanager
    async def fixture_store():
        async with isolated_store() as (store, queue):
            store.registry = registrations(store.registry)
            yield store, queue

    with (
        patch.object(benchmark, "DIRECTORY", DIRECTORY),
        patch.object(benchmark, "MANIFEST", MANIFEST),
        patch.object(benchmark, "isolated_store", fixture_store),
        patch.object(benchmark, "AgentConfig", agent_config),
        patch.object(benchmark, "check_guard", check_guard),
        patch.object(benchmark, "Transport", CaptureTransport),
    ):
        yield


async def main(live):
    baseline = checked_baseline()
    if any((DIRECTORY / name).exists() for name in ("source.json", "requests.sqlite", "requests.started")):
        raise RuntimeError("Campaign already started; do not reopen or transfer its allowance")
    with arm():
        await benchmark.matrix(live)
    if not live:
        print("Unpaid reasoning preflight passed; zero provider calls.", flush=True)
        return
    report = json.loads((DIRECTORY / "results.json").read_text())
    wire = [json.loads(line) for line in (DIRECTORY / "wire-metadata.jsonl").read_text().splitlines()]
    summary = {
        "purpose": "Reference-only Luna medium reasoning rerun; deployed defaults unchanged.",
        "baseline": model_summary(baseline),
        "reasoning_run": model_summary(report),
        "reasoning_tokens": sum(row["reasoning_tokens"] or 0 for row in wire),
        "cached_input_tokens": sum(row["cached_input_tokens"] or 0 for row in wire),
        "wire_responses": len(wire),
        "wire_settings_match": all(
            row["request"]
            == {
                "model": MANIFEST["model"],
                "reasoning_effort": "medium",
                "max_output_tokens": OUTPUT_CAP,
            }
            for row in wire
        ),
        "source_unchanged": report["source_unchanged"],
        "limitations": [
            "Ten synthetic cases, one execution per case; not a general accuracy estimate.",
            "Historical Luna baseline used none reasoning and 2048 output tokens; this run uses medium and 8192. Improvement cannot be attributed solely to reasoning effort.",
            "Same cases, prompts, schemas, grader, three-request cap, 40000 total-token budget and 180-second task deadline. No workflow review or business-rule checker was added.",
            "Output usage includes internal reasoning. Task timings exclude uploads, replay and cleanup; runs were sequential.",
            "No real business writes or hosted browser tasks were executed. Public API exercised through ASGI with real PostgreSQL and Temporal.",
        ],
    }
    (DIRECTORY / "comparison.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps({"event": "reasoning_comparison_complete", **summary}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    flags = parser.add_mutually_exclusive_group(required=True)
    flags.add_argument("--preflight", action="store_true")
    flags.add_argument("--live", action="store_true")
    asyncio.run(main(parser.parse_args().live))
