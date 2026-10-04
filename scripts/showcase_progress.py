"""Fresh fixed-budget Luna comparison after receipt-based continuation guidance."""

import argparse
import asyncio
import hashlib
import json
from pathlib import Path
from unittest.mock import patch

from scripts import showcase_accuracy as benchmark
from scripts import showcase_reasoning as reasoning
from scripts.showcase_model_comparison import checked_baseline, model_summary

DIRECTORY = Path("var/benchmarks/luna-progress-2026-10-04-v1")
BASELINE = Path("var/benchmarks/luna-reasoning-2026-10-04-v1/results.json")
MANIFEST = {**reasoning.MANIFEST, "campaign": DIRECTORY.name}


def checked_reasoning_baseline():
    checked_baseline()
    report = json.loads(BASELINE.read_text())
    assert report["model"] == MANIFEST["model"] and report["reasoning"] == "medium"
    assert report["manifest"] == {**MANIFEST, "campaign": BASELINE.parent.name}
    frozen = json.loads(BASELINE.with_name("source.json").read_text())
    for name in (
        "scripts/showcase_accuracy.py",
        "scripts/showcase_accuracy_cases.py",
        "scripts/showcase_reasoning.py",
    ):
        if hashlib.sha256(Path(name).read_bytes()).hexdigest() != frozen[name]:
            raise ValueError("Benchmark harness, prompts or grading changed")
    return report


async def main(live):
    baseline = checked_reasoning_baseline()
    if any((DIRECTORY / name).exists() for name in ("source.json", "requests.sqlite", "requests.started")):
        raise RuntimeError("Campaign already started; do not reopen or transfer its allowance")
    with patch.object(reasoning, "DIRECTORY", DIRECTORY), patch.object(reasoning, "MANIFEST", MANIFEST):
        with reasoning.arm():
            await benchmark.matrix(live)
    if not live:
        print("Unpaid progress preflight passed; zero provider calls.", flush=True)
        return
    report = json.loads((DIRECTORY / "results.json").read_text())
    wire = [json.loads(line) for line in (DIRECTORY / "wire-metadata.jsonl").read_text().splitlines()]
    result = {
        "purpose": "Reference-only before/after continuation guidance; model defaults unchanged.",
        "baseline": model_summary(baseline),
        "updated_run": model_summary(report),
        "reasoning_tokens": sum(r["reasoning_tokens"] or 0 for r in wire),
        "wire_responses": len(wire),
        "wire_settings_match": all(
            r["request"]
            == {"model": MANIFEST["model"], "reasoning_effort": "medium", "max_output_tokens": 8192}
            for r in wire
        ),
        "source_unchanged": report["source_unchanged"],
        "baseline_sha256": hashlib.sha256(BASELINE.read_bytes()).hexdigest(),
        "limitations": [
            "Same ten synthetic cases, original task prompts, schemas, expected answers and grader. One execution per case; not an estimate of general accuracy.",
            "Same Luna medium reasoning, three requests per case, 8192 output tokens, 40000 total tokens and 180-second task deadline. Only runtime continuation context/instructions changed.",
            "No extra reviewer, domain-specific business checks, new tools, provider retries or expected answers were added to model input.",
            "Uses the public API through ASGI and real PostgreSQL/Temporal. No business writes or hosted browsers.",
            "Task timing excludes uploads, replay and cleanup. Historical baseline and sequential timings are descriptive.",
        ],
    }
    (DIRECTORY / "comparison.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({"event": "progress_comparison_complete", **result}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    flags = parser.add_mutually_exclusive_group(required=True)
    flags.add_argument("--preflight", action="store_true")
    flags.add_argument("--live", action="store_true")
    asyncio.run(main(parser.parse_args().live))
