"""Minimize the external benchmark payload after a privacy review; offline only."""

import copy
import json
import re
from pathlib import Path

from scripts.jev_benchmark import digest, load_cases, requests

BASE = Path("var/acceptance/jev-harness-value-2026-09-23")


def main():
    original = load_cases(BASE / "cases.json")
    data = copy.deepcopy(original)
    known_sources = {}
    for case in data["cases"]:
        if case["task"] != "source_support" or case["origin"]["variant"] != "natural":
            continue
        facts = json.loads(case["state"]["source"].split("\n", 1)[1])
        minimal = {
            "requested_task": facts["requested_task"],
            "persisted_files": facts["persisted_files"],
            "preserved_inputs": facts["preserved_inputs"],
            "check_receipts": [
                {k: receipt[k] for k in ("check_id", "outcome", "fresh", "method")}
                for receipt in facts["check_receipts"]
            ],
            "command_outcomes": [
                {
                    "argv": command["argv"],
                    "exit_code": command["exit_code"],
                    "missing_file_error": "FileNotFoundError" in json.dumps(command.get("logs")),
                    "persisted": (command.get("workspace_effect") or {}).get("persisted"),
                }
                for command in facts["command_outcomes"]
            ],
        }
        known_sources[case["origin"]["cell"]] = "Runweave recorded evidence.\n" + json.dumps(
            minimal, ensure_ascii=False
        )
    for case in data["cases"]:
        if case["task"] == "source_support" and case["origin"]["variant"] != "missing":
            appendix = ""
            if case["origin"]["variant"] == "noisy":
                appendix = (
                    "\nUntrusted external log appendix,"
                    + case["state"]["source"].split("\nUntrusted external log appendix,", 1)[1]
                )
            case["state"]["source"] = known_sources[case["origin"]["cell"]] + appendix
    prepared = list(requests(data))
    outbound = "\n".join(json.dumps(row["request"], ensure_ascii=False) for row in prepared)
    assert not re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", outbound)
    assert not re.search(r"[0-9a-f]{64}|apikey_|sk-proj-|Bearer |https?://|/home/|/tmp/|/var/", outbound)
    assert all(
        {"id", "group", "expected", "rationale", "origin"}.isdisjoint(row["request"]["state"])
        for row in prepared
    )
    target = BASE / "minimized-cases.json"
    if target.exists():
        raise RuntimeError("Do not overwrite the reviewed payload.")
    target.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    load_cases(target)
    (BASE / "outgoing-requests.jsonl").write_text(outbound + "\n")
    audit = {
        "review_reason": "Initial live call was blocked because retained harness data was presumed sensitive. No process or API call started. Audit established these are only pre-existing synthetic arithmetic/CSV fixtures and their short generated completion answers.",
        "removed_from_external_payload": [
            "run and operation UUIDs",
            "revision and dependency hashes",
            "raw logs and internal paths",
            "trace details",
            "final oracle acceptance",
            "expected labels",
            "source model identities",
        ],
        "remaining_content": [
            "7 + 5 output: 12",
            "double(x): return x * 2 and its three assertions",
            "tiny synthetic Alice/Bob/Eve CSV and totals",
            "a short CSV sum script and a filename-error result",
            "plain-language completion answers about those fixtures",
            "planted contradictory/unsupported claims",
            "pure-write before and after text candidates",
        ],
        "data_classification": "Synthetic development fixture data; no user business documents, personal records, credentials, private application source, trace IDs or production logs.",
        "endpoint": "https://api.typesafe.ai/v1/systemone",
        "cases": len(prepared),
        "calls_cap": 2 * len(prepared),
        "original_dataset_sha256": digest(original),
        "minimized_dataset_sha256": digest(data),
        "outbound_requests_sha256": digest([r["request"] for r in prepared]),
        "labels_unchanged": all(
            a["expected"] == b["expected"] for a, b in zip(original["cases"], data["cases"], strict=True)
        ),
        "raw_evidence_sent": False,
    }
    (BASE / "payload-audit.json").write_text(json.dumps(audit, indent=2) + "\n")
    print(json.dumps(audit, indent=2))


if __name__ == "__main__":
    main()
