"""Offline value decision from the completed Jev harness experiment."""

import json
import statistics
import xml.etree.ElementTree as ET
from pathlib import Path

from scripts.jev_benchmark import digest, load_cases, percentile, score
from scripts.jev_benchmark_report import summarize_run
from scripts.jev_harness_value import OUT, sha


def main():
    baseline = json.loads((OUT / "baseline.json").read_text())
    audit = json.loads((OUT / "payload-audit.json").read_text())
    protocol = json.loads((OUT / "protocol.json").read_text())
    data = load_cases(OUT / "live/cases.json")
    assert digest(data) == audit["minimized_dataset_sha256"]
    assert all(sha(p) == expected for p, expected in baseline["source_hashes"].items())
    terminal = json.loads((OUT / "live/terminal.json").read_text())
    assert terminal["status"] == "completed" and terminal["attempts"] == 174
    summary = summarize_run(OUT / "live")
    records = [
        [json.loads(s) for s in (OUT / f"live/repeat-{i}.jsonl").read_text().splitlines()] for i in (1, 2)
    ]
    reports = [
        score(data, r, model=summary["model"], threshold=protocol["primary_threshold"]) for r in records
    ]
    slices = {}
    for tag in ("natural", "contradiction", "unverified", "missing", "noisy", "captured_write"):
        selected = {c["id"] for c in data["cases"] if tag in c["tags"]}
        times = [row["elapsed_ms"] for rows in records for row in rows if row["case_id"] in selected]
        usages = [
            row["response"]["usage"]["input_tokens"]
            for rows in records
            for row in rows
            if row["case_id"] in selected
        ]
        metrics = [r["by_tag"][tag] for r in reports]
        slices[tag] = {
            "distinct_cases": len(selected),
            "responses": len(times),
            "per_repeat": [
                {
                    key: m[key]
                    for key in (
                        "label_accuracy_on_valid_responses",
                        "accepted",
                        "rejected",
                        "review",
                        "false_accepts",
                        "false_rejects",
                        "valid_responses",
                    )
                }
                for m in metrics
            ],
            "p50_ms": percentile(times, 0.5),
            "p95_ms": percentile(times, 0.95),
            "estimated_input_cost_usd": sum(usages) * 0.042 / 1_000_000,
        }
    cases = []
    for case in data["cases"]:
        cases.append(
            {
                **case,
                "jev": [
                    {
                        "decision": r["outcomes"][case["id"]]["decision"],
                        **r["outcomes"][case["id"]].get("answer", {}),
                    }
                    for r in reports
                ],
            }
        )
    tests = ET.parse(OUT / "deterministic-tests.xml").getroot()
    suites = list(tests.iter("testsuite"))
    test_summary = {
        key: sum(int(s.attrib.get(key, 0)) for s in suites)
        for key in ("tests", "failures", "errors", "skipped")
    }
    assert test_summary == {"tests": 26, "failures": 0, "errors": 0, "skipped": 0}
    passed = baseline["natural_successes"]
    would_accept = [m["accepted"] for m in slices["natural"]["per_repeat"]]
    result = {
        "decision": "Do not integrate Jev into the default Runweave harness on this evidence.",
        "adoption_rule": protocol["adoption_rule"],
        "reason": "No demonstrated incremental correction on natural successful outputs; four real failed runs are already rejected for executable-check/control issues. The reviewer introduces deferrals, while duplicate detection and known missing-evidence handling are deterministic.",
        "date": "2026-09-23",
        "model": summary["model"],
        "threshold": 0.9,
        "unique_cases": len(data["cases"]),
        "task_families": 5,
        "new_api_calls": terminal["attempts"],
        "natural_harness": {
            "runs": len(baseline["inventory"]),
            "accepted_by_existing_harness": passed,
            "failed_by_existing_harness": baseline["natural_failures"],
            "accepted_outputs_with_identified_semantic_errors": 0,
            "scope": "Existing 15 retained fixture runs; no new generative-agent runs and no estimate of production defect prevalence.",
            "hypothetical_unattended_acceptance_with_jev_per_repeat": would_accept,
            "additional_valid_outputs_deferred_per_repeat": [passed - x for x in would_accept],
            "newly_completed_failed_runs": "not measured; final-answer review does not execute missing checks or resolve capacity",
            "inventory": baseline["inventory"],
        },
        "slices": slices,
        "duplicate_baseline": {
            "writes": len(baseline["writes"]),
            "correct_predictions": baseline["write_predictions_correct"],
            "duplicates": baseline["exact_duplicate_writes"],
            "byte_changing": len(baseline["writes"]) - baseline["exact_duplicate_writes"],
            "median_us_per_write": statistics.median(baseline["baseline_ns_per_write_100_sweep_samples"])
            / 1000,
            "timing_scope": baseline["baseline_timing_scope"],
            "important_limit": "A no-op precheck can identify redundant effects after a model has proposed them. It does not itself save the model call already spent, ensure the next action makes progress, or fix the remaining failed runs.",
        },
        "speed_cost": {
            key: summary[key]
            for key in (
                "p50_ms",
                "p95_ms",
                "usage",
                "estimated_reported_usage_cost_usd",
                "attempts_without_reported_usage",
            )
        },
        "label_flips": summary["label_flips"],
        "decision_flips": summary["decision_flips"],
        "disagreements": summary["disagreements_with_provisional_labels"],
        "deterministic_recovery_tests": test_summary,
        "offline_scorer_and_precheck_tests": {"passed": 16},
        "frozen_dataset_sha256": digest(data),
        "source_files_unchanged": True,
        "payload_audit": audit,
        "terminal": terminal,
        "cases": cases,
        "limits": protocol["limitations"]
        + [
            "Semantic labels were assistant-adjudicated; five task families and their variants are correlated.",
            "The full-answer reviewer is one tested configuration. Better claim decomposition or fresh-evidence projection might reduce deferrals but was not tested.",
            "No lower threshold was selected after observing results.",
            "The byte-change oracle measures exact replacement, not permission or arbitrary semantic action usefulness.",
            "No production integration, live closed-loop repair, human-review cost, pooled-client or concurrent-load comparison was measured.",
        ],
    }
    output = Path("docs/jev-harness-value-results-2026-09-23.json")
    output.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    report = [
        "# Jev value decision for Runweave — 23 September 2026",
        "",
        "**Decision: do not add Jev to the default harness on the current evidence.** Its low inference price and ability to catch planted answer errors do not establish an improvement to the current coding harness. The measured configuration adds review friction to valid answers, and the observed loop failures have deterministic signals.",
        "",
        "## What was tested",
        "",
        "- Inspected the latest retained 15 real generative-agent fixture runs: 11 accepted, four failed. All required criteria used executable-check policies. These are historical runs; no original paid campaign was restarted.",
        "- Evaluated all 32 captured write proposals, including 13 exact duplicates, against pre-action persisted bytes. A SHA-256 comparison was validated independently against the recorded post-action changed flags.",
        "- Reviewed 11 unmodified successful agent answers against their actual tiny fixture files, preservation facts, and check/command outcomes. Expected source-support labels were adjudicated by this assistant before inference.",
        "- Added four variants per answer: one contradictory statement, one unverified performance claim, missing evidence, and unrelated/injected log text. Variants are capability stress tests, not naturally occurring production defects.",
        "- Ran 87 cases twice: **174 fresh Jev requests**, model `jev-1.13.0`, fixed selected-class probability threshold **0.90**, no automatic retries. Uncertain labels always defer.",
        "- Re-ran **26 focused deterministic recovery/Temporal replay tests** and **16 offline scorer/precheck tests**: all passed. No runtime code was changed.",
        "",
        "## Incremental value versus code",
        "",
        "| Boundary | Deterministic/current harness result | Jev result in each pass | Value decision |",
        "| --- | --- | --- | --- |",
        f"| Retained valid final answers | {passed}/11 accepted; file/check oracles pass | {would_accept[0]}/11 and {would_accept[1]}/11 accepted; five valid answers deferred each time | No new natural defect caught; adds review burden |",
        f"| Pure file replacements | {len(baseline['writes'])}/{len(baseline['writes'])} predictions match actual effects: 13 duplicates, 19 changes | 13 duplicates rejected, 16 changes accepted, three changes deferred | Prefer exact byte comparison |",
        "| Known missing evidence | An explicit incomplete flag can defer 11/11 without a model | 11/11 deferred in both passes | No semantic judgment needed to detect known absence |",
        "| Four failed real runs | Already rejected: repeated writes/no progress and reservation/check/child issues | No final successful answer exists to review | No demonstrated task-success improvement |",
        "",
        "The six-of-eleven result is an offline application of the Jev gate to retained successful outputs. It is not a new measured end-to-end success rate of 6/15. No human completed the reviews and no live repair loop was run. A deferred answer is not a corrected answer.",
        "",
        "The duplicate-write comparator uses a known current revision and exact expected content hashes. It must remain bound to current authoritative state. It is a benchmark helper, not a new runtime authorization rule. Finding a duplicate after proposal does not recover the already-spent generative call or prove that a feedback loop will complete successfully.",
        "",
        "## Detailed Jev results",
        "",
        "| Slice | Distinct cases | Label agreement, pass 1 / 2 | Accept / reject / review, pass 1 | Accept / reject / review, pass 2 |",
        "| --- | ---: | --- | --- | --- |",
    ]
    for tag, metrics in slices.items():
        a, b = metrics["per_repeat"]
        report.append(
            f"| {tag} | {metrics['distinct_cases']} | {a['label_accuracy_on_valid_responses']:.1%} / {b['label_accuracy_on_valid_responses']:.1%} | {a['accepted']} / {a['rejected']} / {a['review']} | {b['accepted']} / {b['rejected']} / {b['review']} |"
        )
    report += [
        "",
        "No planted contradictory or unsupported answer was accepted. Contradiction variants were rejected 11/11 in pass one and 10/11 in pass two, with the remaining case deferred. All added unverified performance claims were deferred. This demonstrates useful discrimination on planted faults, but does not establish how often such faults occur in Runweave or whether review improves total task outcomes.",
        "",
        "The five valid-answer deferrals persisted in both passes. One correct bug-fix answer also changed its top label from supported to contradicted at low probability. The gate prevented an automatic false rejection, but still requires someone or another model to review an already-correct result. Neither review labor nor downstream model cost is included in inference pricing.",
        "",
        "## Speed and cost",
        "",
        f"- Jev HTTP latency: **{summary['p50_ms']:.0f} ms median**, **{summary['p95_ms']:.0f} ms p95**. One question per request, serial fresh connections; includes network overhead.",
        f"- Inference estimate: **US${summary['estimated_reported_usage_cost_usd']:.6f}** for 174 calls; {summary['usage']['input_tokens']:,} input tokens and {summary['usage']['output_tokens']:,} output tokens. Uses [published pricing](https://docs.typesafe.ai/models) of $0.042/million input tokens and free outputs, checked on 23 September; not a billing receipt.",
        f"- Exact duplicate comparison: **{result['duplicate_baseline']['median_us_per_write']:.2f} microseconds per write** in a local CPU microbenchmark after state is available; zero model/API cost. This excludes database lookup and is not an end-to-end service timing.",
        "- All 174 requests returned valid responses. Label changes: four cases; gate changes: one planted-contradiction case. The test does not measure concurrent throughput or SDK connection pooling.",
        "",
        "## Adoption decision",
        "",
        "The predeclared rule required incremental benefit over deterministic checks on representative natural errors, preservation of valid completion, and an acceptable measured end-to-end tradeoff. It is not satisfied. Current evidence supports improving deterministic progress feedback, authoritative check handling, and reservation headroom first. Those runtime changes still require their own implementation and validation; this experiment did not apply them.",
        "",
        "Retain Jev as an unintegrated candidate for a future document/source-grounded workload where semantic errors actually occur and executable checks cannot resolve them. Before adoption, compare the unchanged harness, deterministic improvements alone, and deterministic improvements plus Jev on fresh real tasks with independently reviewed outcomes. Measure accepted incorrect results, completed correct tasks, unnecessary repairs/reviews, and full run time/cost. Do not count planted-error detection or low per-call pricing alone as an adoption win.",
        "",
        "Potential follow-ups such as splitting whole answers into individual claims, clearly separating fresh from historical receipts, or using a different threshold may improve this configuration. They were not tested and cannot be claimed as current benefits. Do not tune the threshold on these same cases and call that independent validation.",
        "",
        "## Evidence, privacy, and limitations",
        "",
        f"Frozen external dataset hash: `{digest(data)}`. Original evidence files and downloaded fixtures were verified byte-identical after testing. There are five original task families, not 87 independent tasks or 174 independent samples.",
        "",
        "Automatic approval review initially blocked a proposed transfer of retained trace-derived data. Before any call, every candidate payload was audited: its contents were only synthetic arithmetic/CSV fixtures and short generated answers. The payload was minimized to remove UUIDs, revision hashes, raw logs, internal paths, and source-model identities. The safer payload passed approval review. No raw trace or credential was sent in the evaluation state; credentials were supplied through the collector's hidden prompt.",
        "",
        "- [Machine-readable results and case-level judgments](jev-harness-value-results-2026-09-23.json)",
        "- [Dataset preparation](../scripts/jev_harness_value.py), [payload minimization](../scripts/jev_harness_payload.py), [offline report](../scripts/jev_harness_report.py)",
        "- Local raw evidence, frozen adoption protocol, payload audit, and JUnit report: `var/acceptance/jev-harness-value-2026-09-23/` (gitignored).",
        "- Recorded source of the real agent runs: [completion model findings](completion-models-findings.md).",
        "- The live collection is terminal. The report can be recomputed offline with `.venv/bin/python -m scripts.jev_harness_report`; this makes no paid calls.",
        "",
    ]
    Path("docs/jev-harness-value-decision-2026-09-23.md").write_text("\n".join(report))
    print(
        json.dumps(
            {
                "decision": result["decision"],
                "natural_harness": {k: v for k, v in result["natural_harness"].items() if k != "inventory"},
                "slices": slices,
                "speed_cost": result["speed_cost"],
                "tests": test_summary,
                "source_files_unchanged": True,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
