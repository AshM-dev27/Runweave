import copy
import json

import pytest
from jsonschema import Draft202012Validator

from agent_runtime.result_contracts import validate_schema
from scripts.showcase_accuracy import check_guard, summarize
from scripts.showcase_accuracy_cases import CASES, canonical, output_schema, parse_answer, score


def test_all_golden_answers_obey_value_free_schemas():
    assert len(CASES) == 10
    for case in CASES.values():
        schema = output_schema(case["family"])
        validate_schema(schema)
        assert '"const"' not in json.dumps(schema)
        Draft202012Validator(schema).validate(case["expected"])
        result = score(case["expected"], copy.deepcopy(case["expected"]))
        assert result["exact_match"] and result["accuracy"] == 1


def test_grader_detects_wrong_money_and_hallucinated_shipping():
    gold = CASES["supplier_pack_prices"]["expected"]
    bad = copy.deepcopy(gold)
    bad["total_cents"] = 225
    bad["offers"][2]["total_cents"] = 20000
    result = score(gold, bad)
    assert not result["exact_match"] and result["correct"] == result["total"] - 2
    assert result["mismatched_paths"] == ["/offers/2/total_cents", "/total_cents"]


def test_grader_penalizes_extra_rows_missing_fields_and_wrong_types():
    gold = CASES["refund_basic"]["expected"]
    for mutate in (
        lambda v: v["orders"].append(v["orders"][0]),
        lambda v: v["orders"][0].pop("reason"),
        lambda v: v["orders"][1].update(amount_cents=False),
    ):
        bad = copy.deepcopy(gold)
        mutate(bad)
        assert not score(gold, bad)["exact_match"]
    assert score(gold, None)["correct"] == 0


def test_crm_ordering_is_ignored_but_record_contents_are_not():
    gold = CASES["crm_messy"]["expected"]
    reordered = copy.deepcopy(gold)
    reordered["accepted"].reverse()
    reordered["excluded"].reverse()
    assert canonical(gold) == canonical(reordered)
    assert score(gold, reordered)["exact_match"]
    reordered["accepted"][0]["company"] = "Invented"
    assert not score(gold, reordered)["exact_match"]


@pytest.mark.parametrize("raw", ['{"amount":1,"amount":2}', '{"amount":NaN}', "```json\n{}\n```"])
def test_output_parser_rejects_ambiguous_or_non_json_answers(raw):
    with pytest.raises(ValueError):
        parse_answer(raw)


def test_summary_separates_runtime_success_from_output_accuracy():
    results = [
        {"family": "crm", "passed": False, "outcome": "succeeded", "score": score({"a": 1}, {"a": 2})},
        {"family": "refund", "passed": True, "outcome": "succeeded", "score": score({"a": 1}, {"a": 1})},
    ]
    result = summarize(results)
    assert result["exact_passes"] == 1 and result["false_acceptances"] == 1
    assert result["families"]["crm"]["field_accuracy"] == 0


async def test_fresh_paid_campaign_cap_is_enforced_without_network():
    await check_guard({"fixture": "fixed"})
