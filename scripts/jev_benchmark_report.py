"""Summarize an existing Jev collection without issuing model requests."""

import argparse
import json
import math
import re
from collections import defaultdict
from pathlib import Path

from scripts.jev_benchmark import RUBRICS, digest, load_cases, percentile, ratio, score


def summarize_run(directory):
    directory = Path(directory)
    frozen = json.loads((directory / "manifest.json").read_text())
    dataset = load_cases(directory / "cases.json")
    if digest(dataset) != frozen["dataset_sha256"] or digest(RUBRICS) != frozen["rubric_sha256"]:
        raise ValueError("Frozen dataset or rubric differs from the current scorer")
    case_map = {c["id"]: c for c in dataset["cases"]}
    all_records, reports = [], []
    for repeat in range(1, frozen["repeats"] + 1):
        path = directory / f"repeat-{repeat}.jsonl"
        records = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
        all_records.append(records)
        reports.append(score(dataset, records, model=frozen["model"]))
    table = {}
    for task in RUBRICS:
        metrics = [r["by_task"][task] for r in reports]
        valid = sum(m["valid_responses"] for m in metrics)
        correct = sum(
            c["expected"] == report["outcomes"][c["id"]].get("answer", {}).get("choice")
            for c in dataset["cases"]
            if c["task"] == task
            for report in reports
        )
        latencies = [
            r["elapsed_ms"] for rows in all_records for r in rows if case_map[r["case_id"]]["task"] == task
        ]
        table[task] = {
            "unique_cases": metrics[0]["cases"],
            "expected_judgments": metrics[0]["cases"] * len(reports),
            "valid_judgments": valid,
            "correct_labels": correct,
            "label_accuracy": ratio(correct, valid),
            **{
                key: sum(m[key] for m in metrics)
                for key in ("accepted", "rejected", "review", "false_accepts", "false_rejects")
            },
            "p50_ms": percentile(latencies, 0.5),
            "p95_ms": percentile(latencies, 0.95),
        }
    changed_labels, changed_gates, errors = [], [], []
    for case in dataset["cases"]:
        outcomes = [report["outcomes"][case["id"]] for report in reports]
        labels = [o.get("answer", {}).get("choice") for o in outcomes]
        gates = [o["decision"] for o in outcomes]
        if len(set(labels)) > 1:
            changed_labels.append(case["id"])
        if len(set(gates)) > 1:
            changed_gates.append(case["id"])
        if any(label != case["expected"] for label in labels):
            errors.append(
                {
                    "case_id": case["id"],
                    "task": case["task"],
                    "expected": case["expected"],
                    "labels": labels,
                    "decisions": gates,
                    "probabilities": [o.get("answer", {}).get("probabilities") for o in outcomes],
                }
            )
    tokens = defaultdict(int)
    usage_missing = 0
    for rows in all_records:
        for row in rows:
            usage = row.get("response", {}).get("usage", {})
            if all(type(usage.get(k)) is int and usage[k] >= 0 for k in ("input_tokens", "output_tokens")):
                for key in ("input_tokens", "output_tokens"):
                    tokens[key] += usage[key]
            else:
                usage_missing += 1
    curves = {}
    for threshold in (0.5, 0.6, 0.7, 0.8, 0.9, 0.95, 0.99):
        points = [
            score(dataset, rows, model=frozen["model"], threshold=threshold)["overall"]
            for rows in all_records
        ]
        curves[str(threshold)] = {
            k: sum(p[k] for p in points)
            for k in ("accepted", "rejected", "review", "false_accepts", "false_rejects")
        }
    latencies = [r["elapsed_ms"] for rows in all_records for r in rows]
    return {
        "model": frozen["model"],
        "dataset_sha256": frozen["dataset_sha256"],
        "rubric_sha256": frozen["rubric_sha256"],
        "label_status": dataset["label_status"],
        "unique_cases": len(case_map),
        "independent_group_candidates": len({c["group"] for c in dataset["cases"]}),
        "repeats": len(reports),
        "attempts": sum(map(len, all_records)),
        "table": table,
        "label_flips": changed_labels,
        "decision_flips": changed_gates,
        "disagreements_with_provisional_labels": errors,
        "usage": dict(tokens),
        "attempts_without_reported_usage": usage_missing,
        "estimated_reported_usage_cost_usd": tokens["input_tokens"] * 0.042 / 1_000_000,
        "cost_note": "Published-rate estimate, not an invoice; unknown attempts may add cost.",
        "p50_ms": percentile(latencies, 0.5),
        "p95_ms": percentile(latencies, 0.95),
        "exploratory_threshold_curve": curves,
        "primary_threshold": 0.9,
        "per_repeat": [{k: r[k] for k in ("overall", "by_task")} for r in reports],
        "retrieval_comparison": retrieval_comparison(dataset, reports),
        "caution": "Authored seed cases; repeated judgments and paired variants are not independent samples.",
    }


def retrieval_comparison(dataset, reports):
    groups = defaultdict(list)
    for case in dataset["cases"]:
        if case["task"] == "context_relevance":
            groups[case["group"]].append(case)
    stop = set(
        "a an the to of in for is are was were has have had does do did with what which how it that".split()
    )

    def words(text):
        return set(re.findall(r"[^\W_]+", text.casefold())) - stop

    def lexical(case):
        q, p = words(case["state"]["query"]), words(case["state"]["passage"])
        return len(q & p) / math.sqrt(len(q) * len(p)) if q and p else 0

    def evaluate(score_fn, minimum):
        recall, no_answer = [], []
        for cases in groups.values():
            values = [score_fn(c) for c in cases]
            best = max(values)
            tied = [c for c, value in zip(cases, values, strict=True) if value == best]
            relevant = [c for c in cases if c["expected"] == "relevant"]
            if relevant:
                recall.append(
                    sum(c["expected"] == "relevant" for c in tied) / len(tied) if best >= minimum else 0
                )
            else:
                no_answer.append(best < minimum)
        return {
            "answerable_groups": len(recall),
            "expected_recall_at_1_with_fractional_ties": ratio(sum(recall), len(recall)),
            "no_answer_or_ambiguous_groups": len(no_answer),
            "correct_abstentions": sum(no_answer),
        }

    return {
        "note": "Three-candidate synthetic packs, one relevant passage at most; not a production RAG benchmark.",
        "lexical_cosine": evaluate(lexical, 1e-12),
        "jev_per_repeat": [
            evaluate(
                lambda c: (
                    report["outcomes"][c["id"]].get("answer", {}).get("probabilities", {}).get("relevant", 0)
                ),
                0.9,
            )
            for report in reports
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    print(json.dumps(summarize_run(args.directory), indent=2))


if __name__ == "__main__":
    main()
