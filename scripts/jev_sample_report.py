"""Compare recorded Jev judgments with actual scripted completion replays; offline."""

import argparse
import json
from collections import Counter
from pathlib import Path

from scripts.jev_benchmark import ACCEPT, UNCERTAIN, digest, load_cases, percentile, score
from scripts.jev_benchmark_report import summarize_run


def build_report(directory, replay_path):
    directory = Path(directory)
    dataset = load_cases(directory / "cases.json")
    replay = json.loads(Path(replay_path).read_text())
    summary = summarize_run(directory)
    terminal = json.loads((directory / "terminal.json").read_text())
    assert terminal["status"] == "completed"
    assert replay["dataset_sha256"] == digest(dataset)
    cases = {c["id"]: c for c in dataset["cases"]}
    runtime = {row["case_id"]: row for row in replay["rows"]}
    completion_cases = [c for c in cases.values() if c["task"] != "context_relevance"]
    assert set(runtime) == {c["id"] for c in completion_cases}
    for case in completion_cases:
        row = runtime[case["id"]]
        assert row["status"] == "completed" and row["accepted"]
        assert row["answer"] == case["state"].get("claim", case["state"].get("answer"))
    records = [
        [json.loads(line) for line in (directory / f"repeat-{repeat}.jsonl").read_text().splitlines()]
        for repeat in range(1, summary["repeats"] + 1)
    ]
    reports = [score(dataset, rows, model=summary["model"], threshold=0.9) for rows in records]
    comparisons = []
    for repeat, report in enumerate(reports, 1):
        outcomes = report["outcomes"]
        accepted = [c for c in completion_cases if outcomes[c["id"]]["decision"] == "accept"]
        rejected = [c for c in completion_cases if outcomes[c["id"]]["decision"] == "reject"]
        review = [c for c in completion_cases if outcomes[c["id"]]["decision"] == "review"]
        nonpassing = [c for c in completion_cases if c["expected"] != ACCEPT[c["task"]]]
        positives = [c for c in completion_cases if c["expected"] == ACCEPT[c["task"]]]
        comparisons.append(
            {
                "repeat": repeat,
                "proposals": len(completion_cases),
                "passing_proposals": len(positives),
                "nonpassing_proposals": len(nonpassing),
                "current_runtime_accepts": sum(r["accepted"] for r in runtime.values()),
                "current_runtime_nonpassing_accepts": sum(runtime[c["id"]]["accepted"] for c in nonpassing),
                "with_jev_accept": len(accepted),
                "with_jev_reject": len(rejected),
                "with_jev_review": len(review),
                "with_jev_nonpassing_accepts": sum(c["expected"] != ACCEPT[c["task"]] for c in accepted),
                "with_jev_passing_rejects": sum(c["expected"] == ACCEPT[c["task"]] for c in rejected),
                "with_jev_passing_reviews": sum(c["expected"] == ACCEPT[c["task"]] for c in review),
                "uncertain_labels_sent_to_review": sum(c["expected"] == UNCERTAIN[c["task"]] for c in review),
                "automatic_judgments": len(accepted) + len(rejected),
                "uncertain_labels_incorrectly_rejected": sum(
                    c["expected"] == UNCERTAIN[c["task"]] for c in rejected
                ),
            }
        )
    per_case = []
    for case in cases.values():
        outcomes = [report["outcomes"][case["id"]] for report in reports]
        row = {
            "case_id": case["id"],
            "sample_request": case["sample_request"],
            "task": case["task"],
            "expected": case["expected"],
            "state": case["state"],
            "rationale": case["rationale"],
            "current_runtime": runtime.get(case["id"]),
            "jev": [
                {
                    "label": o.get("answer", {}).get("choice"),
                    "decision": o["decision"],
                    "probabilities": o.get("answer", {}).get("probabilities"),
                    "error": o.get("error"),
                }
                for o in outcomes
            ],
        }
        per_case.append(row)
    times = [row["elapsed_ms_including_client_poll"] for row in runtime.values()]
    families = {}
    for family in dataset["families"]:
        selected = [c for c in cases.values() if c["id"].startswith(family["id"] + "_")]
        by_repeat = []
        for report in reports:
            outcomes = report["outcomes"]
            by_repeat.append(
                {
                    "correct": sum(
                        outcomes[c["id"]].get("answer", {}).get("choice") == c["expected"] for c in selected
                    ),
                    "cases": len(selected),
                    "decisions": dict(Counter(outcomes[c["id"]]["decision"] for c in selected)),
                }
            )
        families[family["id"]] = {"request": family["request"], "per_repeat": by_repeat}
    automatic = [
        (case, report["outcomes"][case["id"]])
        for report in reports
        for case in cases.values()
        if report["outcomes"][case["id"]]["decision"] != "review"
    ]
    automatic_correct = sum(o["answer"]["choice"] == c["expected"] for c, o in automatic)
    valid = sum(r["overall"]["valid_responses"] for r in reports)
    summary.update(
        {
            "date": "2026-09-23",
            "quality_summary": {
                "valid_responses": valid,
                "service_or_schema_failures": summary["attempts"] - valid,
                "correct_labels": sum(t["correct_labels"] for t in summary["table"].values()),
                "automatic_judgments": len(automatic),
                "automatic_correct_labels": automatic_correct,
                "automatic_wrong_labels": len(automatic) - automatic_correct,
                "automatic_label_accuracy": automatic_correct / len(automatic),
                "automatic_coverage": len(automatic) / (len(cases) * len(reports)),
                "review_judgments": len(cases) * len(reports) - len(automatic),
            },
            "terminal": terminal,
            "runtime_replay": replay,
            "completion_comparison_per_repeat": comparisons,
            "families": families,
            "cases": per_case,
            "case_families": 6,
            "additional_retrieval_packs": 2,
            "group_note": "The 14 group IDs are indexing keys, not independent families. Six source/task families share variants and retrieval packs; two additional retrieval packs test absent and ambiguous answers.",
            "runtime_latency_ms_including_client_poll": {
                "p50": percentile(times, 0.5),
                "p95": percentile(times, 0.95),
                "note": "Scripted generator, ASGI in-process HTTP transport, real PostgreSQL and Temporal; includes client polling. Not live generative-agent latency or a service SLO.",
            },
            "estimated_cost_per_1000_judgments_usd_same_input_mix": summary[
                "estimated_reported_usage_cost_usd"
            ]
            * 1000
            / summary["attempts"],
            "observed_serial_requests_per_second": summary["attempts"] / terminal["elapsed_seconds"],
            "inference_design": "One question per fresh HTTP connection, serial, two shuffled repeats, no retry; primary probability threshold 0.90 fixed before calls. All insufficient-evidence/context labels go to review.",
            "comparison_limit": "Current runtime replay is executed; adding the recorded Jev gate is an offline counterfactual. No deployed integration, repair loop, human review outcome, real generative baseline, concurrency or batched-question test was measured.",
        }
    )
    return summary


def markdown(report):
    lines = [
        "# Fresh Jev sample benchmark — 23 September 2026",
        "",
        "Executed 36 authored completion proposals through the real Runweave API, PostgreSQL, and Temporal using a scripted FunctionModel. Evaluated 60 new judgments with live Jev twice (120 requests): source support, requirement coverage, and context relevance. No runtime integration or deployment was performed.",
        "",
        "The six sample requests concern a checkout incident, migration rollback, fictional subscription pricing, CSV validation, Malay backup policy, and feature rollout. Each contains deliberately correct, incorrect, and incomplete candidate content. Two extra retrieval packs cover missing and ambiguous answers.",
        "",
        "## Accuracy, decisions, and speed",
        "",
        "Labels were assistant-authored and frozen before inference. Agreement below is against those provisional labels, not independently established production accuracy. Paired variants and repeated calls are correlated.",
        "",
        "| Judgment | Distinct cases | Correct labels over two passes | Agreement | Accept / reject / review | False accepts | p50 / p95 HTTP latency |",
        "| --- | ---: | ---: | ---: | --- | ---: | --- |",
    ]
    for task, row in report["table"].items():
        if not row["unique_cases"]:
            continue
        lines.append(
            f"| {task.replace('_', ' ')} | {row['unique_cases']} | {row['correct_labels']}/{row['valid_judgments']} | {row['label_accuracy']:.1%} | {row['accepted']} / {row['rejected']} / {row['review']} | {row['false_accepts']} | {row['p50_ms']:.0f} / {row['p95_ms']:.0f} ms |"
        )
    lines += [
        "",
        f"Across all calls: **{report['p50_ms']:.0f} ms p50**, **{report['p95_ms']:.0f} ms p95**. Collection took {report['terminal']['elapsed_seconds']:.1f} seconds, or {report['observed_serial_requests_per_second']:.2f} requests/second in this serial client. This is not a throughput limit or pooled-connection measurement.",
        "",
        f"Reported tokens: {report['usage']['input_tokens']:,} input and {report['usage']['output_tokens']:,} output. Estimated charge: **US${report['estimated_reported_usage_cost_usd']:.6f}** for all {report['attempts']} calls; **US${report['estimated_cost_per_1000_judgments_usd_same_input_mix']:.4f} per 1,000 judgments** at this input mix. This is a usage-based estimate, not a billing receipt, using [published pricing](https://docs.typesafe.ai/models) checked on 23 September: $0.042/million input tokens; outputs free.",
        "",
        f"Label changes between repeats: {len(report['label_flips'])}/60. Threshold-decision changes: {len(report['decision_flips'])}/60. Requests without reported usage: {report['attempts_without_reported_usage']}.",
        "",
        f"Schema-valid responses: {report['quality_summary']['valid_responses']}/{report['attempts']}; service/schema failures: {report['quality_summary']['service_or_schema_failures']}. Overall label agreement: {report['quality_summary']['correct_labels']}/{report['attempts']}. The gate makes {report['quality_summary']['automatic_judgments']}/{report['attempts']} automatic judgments and defers {report['quality_summary']['review_judgments']}. Four automatic judgments have the wrong class: two distinct incomplete captures are rejected in both passes. Zero false acceptance therefore does not mean every automated disposition is correct.",
        "",
        "## What changes at the completion boundary",
        "",
        "The current completion pipeline accepted every scripted proposal because each source quote existed or the assessment text was present. This exercises the real pipeline, unlike the earlier structural proxy. It does not estimate how frequently a real agent produces these errors. Jev was evaluated afterward; the following gate is an offline counterfactual, not a live integrated run.",
        "",
        "| Result on 36 completion proposals | Current runtime | Jev gate, pass 1 | Jev gate, pass 2 |",
        "| --- | ---: | ---: | ---: |",
    ]
    a, b = report["completion_comparison_per_repeat"]
    for label, current, key in (
        ("Accepted", a["current_runtime_accepts"], "with_jev_accept"),
        ("Rejected for repair", 0, "with_jev_reject"),
        ("Deferred for review / more evidence", 0, "with_jev_review"),
        (
            "Nonpassing proposals accepted",
            a["current_runtime_nonpassing_accepts"],
            "with_jev_nonpassing_accepts",
        ),
        ("Passing proposals rejected", 0, "with_jev_passing_rejects"),
        ("Passing proposals deferred", 0, "with_jev_passing_reviews"),
        (
            "Incomplete captures incorrectly rejected instead of deferred",
            0,
            "uncertain_labels_incorrectly_rejected",
        ),
    ):
        lines.append(f"| {label} | {current} | {a[key]} | {b[key]} |")
    lines += [
        "",
        f"There are {a['passing_proposals']} passing and {a['nonpassing_proposals']} nonpassing proposals; nonpassing includes contradicted/uncovered content and missing evidence. The primary threshold is 0.90 on the selected class probability; explicit uncertainty classes always defer. A deferred case is not a corrected answer. Human review and repair costs are not included.",
        "",
        "## Concrete samples",
        "",
        "| Request / candidate | Expected | Jev labels, passes 1 / 2 | Gate, passes 1 / 2 |",
        "| --- | --- | --- | --- |",
    ]
    example_ids = (
        "incident_source_1",
        "migration_source_1",
        "pricing_source_1",
        "csv_source_2",
        "backup_coverage_0",
        "rollout_coverage_1",
    )
    for case in report["cases"]:
        if case["case_id"] in example_ids:
            text = case["state"].get("claim", case["state"].get("answer", ""))
            labels = " / ".join(x["label"] or "error" for x in case["jev"])
            gates = " / ".join(x["decision"] for x in case["jev"])
            lines.append(f"| {text} | {case['expected']} | {labels} | {gates} |")
    lines += [
        "",
        "## Context selection",
        "",
        "Eight three-candidate packs: six contain one relevant passage; one has no answer; one lacks a resolvable query identity. The fixed baseline uses lexical cosine with fractional ties. The distractors deliberately include lexical overlap, so the lexical baseline is disadvantaged by construction. These tiny authored packs do not compare Jev with BM25, embeddings, or a production reranker.",
        "",
    ]
    retrieval = report["retrieval_comparison"]
    for name, value in [
        ("Lexical cosine", retrieval["lexical_cosine"]),
        *[(f"Jev pass {i}", r) for i, r in enumerate(retrieval["jev_per_repeat"], 1)],
    ]:
        lines.append(
            f"- {name}: recall@1 {value['expected_recall_at_1_with_fractional_ties']:.1%} on {value['answerable_groups']} answerable packs; correct abstentions {value['correct_abstentions']}/{value['no_answer_or_ambiguous_groups']}."
        )
    lines += [
        "",
        "## Errors and uncertainty",
        "",
        "The material failure is incomplete-input handling. All six truncated captures were labeled not_covered rather than insufficient_context. Four were deferred because their probability was below 0.90; migration_coverage_2 (0.93) and csv_coverage_2 (0.97) were incorrectly sent to repair in both passes. The current false_rejects metric counts only known passing cases and does not include these uncertainty-routing errors; they are reported separately above.",
        "",
        "A likely input-quality issue affects pricing_context_0: the candidate refers to Q-19 but omits Arbor, while the model receives no mapping from Q-19 to Arbor. The expected label used the authored document provenance, which the request did not supply. Preserve the frozen label and count the miss, but do not attribute it solely to Jev. A future evaluation should supply explicit source identity and compare both input conditions.",
        "",
        "Recommendation: prioritize source-support review. Before requirement review, code should check known capture completeness and fetch missing content or defer. Context ranking needs document identity and scope attached to candidates. These are follow-up design changes, not changes tested in this run.",
        "",
    ]
    if report["disagreements_with_provisional_labels"]:
        for item in report["disagreements_with_provisional_labels"]:
            lines.append(
                f"- `{item['case_id']}`: expected `{item['expected']}`; observed {item['labels']}; gate {item['decisions']}. Inspect the JSON evidence before attributing this to model error rather than annotation ambiguity."
            )
    else:
        lines.append(
            "No top-label disagreements in these samples. This does not establish a low production error rate: labels are provisional, the samples are small, and the examples are deliberately constructed."
        )
    lines += [
        "",
        "## Reproduction and boundaries",
        "",
        f"Pinned model: `{report['model']}`. Dataset hash: `{report['dataset_sha256']}`. Rubric hash: `{report['rubric_sha256']}`. Prompts and threshold match the earlier benchmark; samples are new. Group IDs are indexing keys: the six request families share variants and passages and must stay together in future dataset splits.",
        "",
        "The replay used an isolated temporary PostgreSQL schema and Temporal task queue, with in-process ASGI HTTP transport and a scripted model returning the exact prepared answers. Two representative completed Temporal histories replayed successfully. It bypasses paid generation but runs the actual private model adapter, evidence recording, completion assessment, workflow, and public API. Current runtime latency includes polling and is not comparable with a live model's end-to-end speed.",
        "",
        "Source and assessment policies intentionally provide scoped evidence guarantees. These results justify evaluating an optional semantic reviewer; they do not demonstrate that existing command/check receipts, idempotency, permissions, or budgets should be replaced by a model.",
        "",
        "Live collection is terminal. No additional calls or retries are implied by these commands:",
        "",
        "```bash",
        ".venv/bin/python -m scripts.jev_sample_report var/acceptance/jev-samples-2026-09-23-v1 var/acceptance/jev-samples-runtime-2026-09-23.json",
        "```",
        "",
        "- Dataset: [fresh cases](../tests/fixtures/jev-sample-cases-2026-09-23.json)",
        "- Machine-readable report: [results JSON](jev-sample-results-2026-09-23.json)",
        "- Runtime replay: [integration harness](../tests/test_jev_sample_runtime.py)",
        "- Local evidence: `var/acceptance/jev-samples-2026-09-23-v1/` and `var/acceptance/jev-samples-runtime-2026-09-23.json` (gitignored).",
        "- Preparation caught a typed-client accessor error before the first replay evidence row was written; it was corrected without changing samples, labels, rubrics, or runtime code. No live Jev request was retried.",
        "",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("replay", type=Path)
    args = parser.parse_args()
    report = build_report(args.directory, args.replay)
    Path("docs/jev-sample-results-2026-09-23.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n"
    )
    Path("docs/jev-sample-benchmark-2026-09-23.md").write_text(markdown(report))
    print(
        json.dumps(
            {
                k: report[k]
                for k in (
                    "table",
                    "completion_comparison_per_repeat",
                    "usage",
                    "estimated_reported_usage_cost_usd",
                    "p50_ms",
                    "p95_ms",
                    "label_flips",
                    "decision_flips",
                    "disagreements_with_provisional_labels",
                    "retrieval_comparison",
                )
            },
            indent=2,
        )
    )
