"""Fresh two-model comparison of the unchanged output-accuracy benchmark; reference only."""

import argparse
import asyncio
import hashlib
import json
import os
import statistics
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from unittest.mock import patch

from agent_runtime.registry import Registry
from scripts import showcase_accuracy as benchmark
from scripts.contracts_smoke import isolated_store, sources
from scripts.showcase_accuracy_cases import CASES, RULES, output_schema

DIRECTORY = Path("var/benchmarks/model-comparison-2026-10-04-v1")
BASELINE = Path("var/benchmarks/output-accuracy-2026-10-04-v1/results.json")
MODELS = ("gpt-5.6-terra", "gpt-5.6-sol")


def manifest(model):
    if model not in MODELS:
        raise ValueError("Model outside this comparison")
    return {
        **benchmark.MANIFEST,
        "campaign": DIRECTORY.name + "-" + model,
        "model": model,
        "reasoning": "none",
        "limit": 30,
        "scenario_limits": dict.fromkeys(CASES, 3),
        "scenario_caps": dict.fromkeys(CASES, 2048),
    }


def registrations(registry, model):
    settings = registry.entries[("openai", "gpt-5.6-luna")].model_dump()
    settings.update(model=model, upstream_model=model, reasoning_effort=manifest(model)["reasoning"])
    return Registry([r.model_dump() for r in registry.entries.values()] + [settings])


@contextmanager
def arm(model):
    @asynccontextmanager
    async def fixture_store():
        async with isolated_store() as (store, queue):
            store.registry = registrations(store.registry, model)
            yield store, queue

    with (
        patch.object(benchmark, "DIRECTORY", DIRECTORY / model),
        patch.object(benchmark, "MANIFEST", manifest(model)),
        patch.object(benchmark, "isolated_store", fixture_store),
    ):
        yield


def checked_baseline():
    value = json.loads(BASELINE.read_text())
    if value["model"] != "gpt-5.6-luna" or value["reasoning"] != "none":
        raise ValueError("Unexpected historical baseline")
    rows = {row["name"]: row for row in value["cases"]}
    if set(rows) != set(CASES):
        raise ValueError("Benchmark case set changed")
    for name, case in CASES.items():
        if any(rows[name][key] != case[key] for key in ("input", "expected")):
            raise ValueError("Inputs or expected answers differ from the baseline")
    frozen = json.loads(BASELINE.with_name("source.json").read_text())
    for path in ("scripts/showcase_accuracy.py", "scripts/showcase_accuracy_cases.py"):
        if hashlib.sha256(Path(path).read_bytes()).hexdigest() != frozen[path]:
            raise ValueError("Baseline prompts, schema or grading harness changed")
    return value


def snapshot():
    checked_baseline()
    return {
        "campaign": DIRECTORY.name,
        "combined_request_cap": 60,
        "models": {m: manifest(m) for m in MODELS},
        "sources": sources(),
        "baseline_sha256": hashlib.sha256(BASELINE.read_bytes()).hexdigest(),
        "dataset_sha256": hashlib.sha256(
            json.dumps(
                {"cases": CASES, "rules": RULES, "schemas": {f: output_schema(f) for f in RULES}},
                sort_keys=True,
            ).encode()
        ).hexdigest(),
    }


def model_summary(report):
    result = report["summary"]
    seconds = [case["seconds"] for case in report["cases"] if "seconds" in case]
    return {
        "model": report["model"],
        "reasoning": report["reasoning"],
        **result,
        "field_accuracy": result["correct_fields"] / result["total_fields"],
        "completed_results": sum(c.get("outcome") == "succeeded" for c in report["cases"]),
        "physical_requests": report.get("physical_requests"),
        "tokens": report.get("tokens"),
        "median_task_seconds": round(statistics.median(seconds), 3),
        "total_task_seconds": round(sum(seconds), 3),
    }


async def main(live):
    os.umask(0o077)
    frozen = snapshot()
    start = DIRECTORY / "comparison.started.json"
    if start.exists():
        raise RuntimeError("Comparison already started; do not restart or transfer its allowance")
    for model in MODELS:
        if any(
            (DIRECTORY / model / name).exists()
            for name in ("source.json", "requests.sqlite", "requests.started")
        ):
            raise RuntimeError("A model arm is already started; do not reopen it")
    if live:
        if json.loads((DIRECTORY / "preflight.json").read_text()) != frozen:
            raise RuntimeError("Comparison preflight/source mismatch")
        for model in MODELS:
            expected = {"sources": frozen["sources"], "cases": list(CASES), "manifest": manifest(model)}
            if json.loads((DIRECTORY / model / "preflight.json").read_text()) != expected:
                raise RuntimeError("Model preflight/source mismatch")
        with start.open("x") as file:
            file.write(json.dumps(frozen, indent=2) + "\n")
            file.flush()
            os.fsync(file.fileno())
    for model in MODELS:
        print(json.dumps({"event": "model_arm_started", "model": model, "live": live}), flush=True)
        with arm(model):
            await benchmark.matrix(live)
    if not live:
        DIRECTORY.mkdir(parents=True, exist_ok=True)
        (DIRECTORY / "preflight.json").write_text(json.dumps(frozen, indent=2) + "\n")
        print("Unpaid comparison preflight passed; zero provider calls.", flush=True)
        return
    reports = [checked_baseline()] + [
        json.loads((DIRECTORY / m / "results.json").read_text()) for m in MODELS
    ]
    result = {
        "purpose": "Reference-only model comparison; deployed model defaults unchanged.",
        "baseline": str(BASELINE),
        "model_results": [model_summary(r) for r in reports],
        "new_requests": sum(r["physical_requests"] for r in reports[1:]),
        "new_request_cap": 60,
        "source_unchanged": snapshot() == frozen,
        "limitations": [
            "One execution per model per case; ten synthetic cases, not a statistical accuracy estimate.",
            "Baseline is historical. Runtime denial reporting was fixed afterward; these read-only cases do not request approvals.",
            "Same prompts, source inputs, expected answers, schema and grader, three requests per case, 2048 output tokens per request, and none reasoning.",
            "No prompt, schema, tool or budget repair was applied to improve stronger-model scores.",
            "Strict scoring includes labels, array lengths and extra or missing paths; unfinished tasks score zero.",
            "Task times run from post-submission worker startup to the final result; they exclude uploads, replay and cleanup. Models ran sequentially; timing is descriptive.",
            "No real business writes or hosted browser tasks were executed.",
        ],
    }
    (DIRECTORY / "comparison.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({"event": "model_comparison_complete", **result}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    flags = parser.add_mutually_exclusive_group(required=True)
    flags.add_argument("--preflight", action="store_true")
    flags.add_argument("--live", action="store_true")
    asyncio.run(main(parser.parse_args().live))
