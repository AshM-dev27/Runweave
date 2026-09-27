import copy
import json
import math

import pytest

from scripts.jev_benchmark import (
    ACCEPT,
    DEFAULT_MODEL,
    RUBRICS,
    disposition,
    load_cases,
    requests,
    score,
)


def response_record(case, label=None, probability=1.0):
    label = label or case["expected"]
    labels = list(RUBRICS[case["task"]]["criteria"])
    return {
        "case_id": case["id"],
        "request_sha256": next(requests({"cases": [case]}))["request_sha256"],
        "elapsed_ms": 100,
        "response": {
            "model": DEFAULT_MODEL,
            "answers": {
                "verdict": {
                    "type": "choice",
                    "choice": label,
                    "confidence": 0.95,
                    "probabilities": {
                        k: probability if k == label else (1 - probability) / (len(labels) - 1)
                        for k in labels
                    },
                }
            },
            "usage": {"input_tokens": 100, "output_tokens": 10},
        },
    }


def test_export_has_no_labels_and_is_bound_to_rubric_and_state():
    dataset = load_cases()
    exported = list(requests(dataset))
    assert len(exported) == len(dataset["cases"]) == 80
    body = exported[0]["request"]
    assert set(body) == {"model", "state", "questions"}
    assert "expected" not in body["state"] and "rationale" not in body["state"]
    changed = copy.deepcopy(dataset)
    changed["cases"][0]["state"]["claim"] += " Changed claim."
    assert next(requests(changed))["request_sha256"] != exported[0]["request_sha256"]
    with pytest.raises(ValueError, match="Pin"):
        list(requests(dataset, "jev-latest"))


def test_missing_quotes_stop_before_inference(tmp_path):
    dataset = load_cases()
    dataset["cases"][0]["state"]["quote"] = "fabricated quotation"
    path = tmp_path / "cases.json"
    path.write_text(json.dumps(dataset))
    with pytest.raises(ValueError, match="missing exact quote"):
        load_cases(path)


def test_fake_responses_exercise_scoring_not_model_quality():
    dataset = load_cases()
    report = score(dataset, [response_record(c) for c in dataset["cases"]])
    metrics = report["overall"]
    assert metrics["label_accuracy_on_valid_responses"] == 1
    assert metrics["false_accepts"] == 0
    assert metrics["multiclass_brier_sum"] == 0
    assert metrics["review"] > 0  # Insufficient evidence remains review even at confidence 1.


def test_errors_and_missing_responses_are_review_not_correct_labels():
    dataset = load_cases()
    record = response_record(dataset["cases"][0])
    record.pop("response")
    record["error_code"] = "timeout"
    report = score(dataset, [record])
    assert report["overall"]["valid_responses"] == 0
    assert report["overall"]["review"] == 80
    assert report["overall"]["label_accuracy_on_valid_responses"] is None
    assert report["overall"]["error_rate_among_accepted_cases"] is None


def test_false_accept_and_false_reject_denominators():
    dataset = load_cases()
    good = next(c for c in dataset["cases"] if c["task"] == "source_support" and c["expected"] == "supported")
    bad = next(
        c for c in dataset["cases"] if c["task"] == "source_support" and c["expected"] == "contradicted"
    )
    subset = {**dataset, "cases": [good, bad]}
    report = score(subset, [response_record(good, "contradicted"), response_record(bad, "supported")])
    assert report["overall"]["false_accept_rate_among_nonpassing_cases"] == 1
    assert report["overall"]["false_reject_rate_among_passing_cases"] == 1
    assert report["overall"]["error_rate_among_accepted_cases"] == 1


@pytest.mark.parametrize("invalid", [math.nan, math.inf, -0.1, 1.1, True])
def test_invalid_probabilities_never_become_acceptance(invalid):
    dataset = load_cases()
    case = dataset["cases"][0]
    record = response_record(case)
    record["response"]["answers"]["verdict"]["probabilities"][ACCEPT[case["task"]]] = invalid
    report = score(dataset, [record])
    assert report["outcomes"][case["id"]]["error"] == "invalid_response"
    assert report["overall"]["accepted"] == 0


def test_no_duplicate_or_unbound_results():
    dataset = load_cases()
    record = response_record(dataset["cases"][0])
    with pytest.raises(ValueError, match="duplicate"):
        score(dataset, [record, record])
    record["request_sha256"] = "wrong"
    with pytest.raises(ValueError, match="bound"):
        score(dataset, [record])


def test_low_probability_review_and_wrong_model():
    dataset = load_cases()
    record = response_record(dataset["cases"][0], probability=0.6)
    assert disposition(record["response"]["answers"]["verdict"], "source_support", 0.9) == "review"
    record["response"]["model"] = "different-model"
    assert score(dataset, [record])["overall"]["valid_responses"] == 0


def test_report_aggregates_repeats_without_claiming_independent_cases(tmp_path):
    from scripts.jev_benchmark import manifest
    from scripts.jev_benchmark_report import summarize_run

    dataset = load_cases()
    (tmp_path / "cases.json").write_text(json.dumps(dataset))
    (tmp_path / "manifest.json").write_text(json.dumps({**manifest(dataset, DEFAULT_MODEL), "repeats": 2}))
    for repeat in (1, 2):
        records = [response_record(c) for c in dataset["cases"]]
        (tmp_path / f"repeat-{repeat}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in records))
    report = summarize_run(tmp_path)
    assert report["unique_cases"] == 80
    assert report["attempts"] == 160
    assert report["label_flips"] == []
    assert report["usage"]["input_tokens"] == 16000
    assert report["estimated_reported_usage_cost_usd"] == pytest.approx(0.000672)
    assert (
        report["retrieval_comparison"]["jev_per_repeat"][0]["expected_recall_at_1_with_fractional_ties"] == 1
    )
    assert report["retrieval_comparison"]["jev_per_repeat"][0]["correct_abstentions"] == 2


def test_collector_uses_fake_transport_and_refuses_reusing_campaign(tmp_path, monkeypatch):
    import io
    import sys

    from scripts import jev_benchmark_live

    dataset = load_cases()
    dataset["cases"] = dataset["cases"][:2]
    cases_path = tmp_path / "seed.json"
    cases_path.write_text(json.dumps(dataset))
    out = tmp_path / "run"
    monkeypatch.setenv("TYPESAFE_API_KEY", "fake-test-key")
    captured = []

    class FakeOpener:
        def open(self, request, timeout):
            assert timeout == 15
            assert request.headers["Authorization"] == "Bearer fake-test-key"
            captured.append(json.loads(request.data))
            return io.StringIO(json.dumps(response_record(dataset["cases"][0])["response"]))

    monkeypatch.setattr(jev_benchmark_live, "build_opener", lambda *_: FakeOpener())
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "collector",
            "--live",
            "--out",
            str(out),
            "--cases",
            str(cases_path),
            "--repeats",
            "1",
            "--max-requests",
            "2",
        ],
    )
    jev_benchmark_live.main()
    assert len(captured) == 2
    assert json.loads((out / "terminal.json").read_text())["attempts"] == 2
    assert all("fake-test-key" not in p.read_text() for p in out.iterdir())
    with pytest.raises(FileExistsError):
        jev_benchmark_live.main()
    assert len(captured) == 2
