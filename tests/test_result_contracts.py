"""Caller-owned result requirements stay authoritative across repair and retries."""

import copy
import json

import pytest
from pydantic import ValidationError
from test_general_semantic import complete, http_client, stub, submit

from agent_runtime.db import RunRow
from agent_runtime.general_completion import general_completion
from agent_runtime.general_runtime import general_action, general_step
from agent_runtime.project_store import digest
from agent_runtime.result_contracts import ResultContract, validate_result_contract
from agent_runtime.schemas import AgentConfig, RunCreate

JSON_CONTRACT = {
    "kind": "json_schema",
    "json_schema": {
        "type": "object",
        "properties": {"total": {"type": "integer", "minimum": 0}},
        "required": ["total"],
        "additionalProperties": False,
    },
}


def task(contract=None):
    value = {
        "outcome": "Return the total",
        "criteria": [{"id": "total", "statement": "Correct total"}],
    }
    if contract is not None:
        value["result_contract"] = contract
    return value


@pytest.mark.parametrize("answer", ['{"total":12}', ' { "total" : 0 } '])
def test_json_result_accepts_only_schema_conforming_values(answer):
    assert validate_result_contract(JSON_CONTRACT, answer) == []


@pytest.mark.parametrize(
    "answer,code",
    [
        ('{"total":"12"}', "schema_mismatch"),
        ('{"total":true}', "schema_mismatch"),
        ('{"total":-1}', "schema_mismatch"),
        ('{"total":12,"extra":1}', "schema_mismatch"),
        ("{}", "schema_mismatch"),
        ('{"total":12,"total":13}', "invalid_json"),
        ('{"total":NaN}', "invalid_json"),
        ('{"total":Infinity}', "invalid_json"),
        ('{"total":1e999}', "invalid_json"),
        ('```json\n{"total":12}\n```', "invalid_json"),
        ('{"total":12} trailing', "invalid_json"),
        ("[" * 30 + "0" + "]" * 30, "invalid_json"),
        ("x" * 16001, "answer_limit"),
    ],
)
def test_result_contract_rejects_ambiguous_or_invalid_answers(answer, code):
    assert validate_result_contract(JSON_CONTRACT, answer) == ["result_contract:" + code]


def test_exact_contract_never_normalizes_or_extracts_an_answer():
    contract = {"kind": "exact", "exact": "é"}
    assert validate_result_contract(contract, "é") == []
    for candidate in ("é\n", " é", "É", "e\u0301", '"é"'):
        assert validate_result_contract(contract, candidate) == ["result_contract:exact_mismatch"]
    assert validate_result_contract({"kind": "exact", "exact": ""}, "") == []


@pytest.mark.parametrize(
    "schema",
    [
        {"$ref": "https://example.invalid/schema"},
        {"$ref": "#"},
        {"type": "string", "pattern": "(a+)+"},
        {"type": "string", "format": "email"},
        {"allOf": [{"type": "string"}]},
        {"properties": {"a": {"$ref": "file:///etc/passwd"}}},
        {"description": "x" * 4097},
        {"type": "invalid"},
        {"minimum": float("inf")},
    ],
)
def test_schema_rejects_unsupported_or_unbounded_features(schema):
    with pytest.raises(ValidationError):
        ResultContract(kind="json_schema", json_schema=schema)


async def test_json_contract_rejection_repair_and_durable_acceptance(store, monkeypatch):
    proposals = [complete(), complete()]
    proposals[0]["answer"] = '{"total":"12"}'
    proposals[1]["answer"] = '{"total":12}'
    calls = stub(monkeypatch, proposals)
    async with http_client(store) as client:
        run = await submit(client, task=task(JSON_CONTRACT))
        first = await general_step({"run_id": run.id, "completion_loop": 2})
        payload = await general_completion({"run_id": run.id, **first})
        rejected = await general_action(payload)
        assert not rejected["accepted"]
        assert "result_contract:schema_mismatch" in rejected["remaining_gaps"]
        assert await general_action(payload) == rejected
        assert (await client.get(run.id)).status != "completed"
        assert calls[0]["result_contract"]["json_schema"] == JSON_CONTRACT["json_schema"]
        second = await general_step({"run_id": run.id, "completion_loop": 2})
        assert (await general_action(await general_completion({"run_id": run.id, **second})))["accepted"]
        finished = await client.get(run.id)
        assert finished.output.answer == '{"total":12}'
        assert finished.completion_assessment.accepted
        events = await store.events(run.id)
        assert sum(e.type == "completion.rejected" for e in events) == 1
        assert sum(e.type == "run.completed" for e in events) == 1


async def test_exact_contract_is_enforced_even_without_semantic_schema(store):
    async with http_client(store) as client:
        run = await submit(client, task=task({"kind": "exact", "exact": "12"}))
        async with store.database.sessions.begin() as db:
            row, gr, _ = await store.general_lock(db, run.id)
            state = gr.data["task_state"]
            action = {
                "answer": "The answer is 12.",
                "assessment": {
                    "proposal_id": "forged",
                    "state_version": state["version"],
                    "goal_version": state["goal_version"],
                    "revision_id": state["head"],
                    "criteria": [
                        {"criterion_id": "total", "disposition": "satisfied", "assessment": "Correct"}
                    ],
                },
            }
            rejected = await store.general_complete(db, row, gr, action)
            assert not rejected["accepted"]
            assert rejected["remaining_gaps"] == ["result_contract:exact_mismatch"]
            assert row.status != "completed" and row.output is None


async def test_invalid_contract_does_not_dispatch_reviewer(store, monkeypatch):
    from agent_runtime.completion_review import ensure_review
    from agent_runtime.general_contracts import CompletionReviewPolicy, GeneralPolicy

    def forbidden(*args):
        pytest.fail("Deterministically invalid answers must never spend review calls")

    monkeypatch.setattr("agent_runtime.completion_review.build_review_model", forbidden)
    async with http_client(store) as client:
        run = await submit(
            client,
            task=task({"kind": "exact", "exact": "12"}),
            policy=GeneralPolicy(review=CompletionReviewPolicy()),
        )
        state = (await store.general(run.id))["task_state"]
        await ensure_review(
            store,
            run.id,
            {
                "answer": "wrong",
                "assessment": {
                    "proposal_id": "invalid",
                    "state_version": state["version"],
                    "goal_version": state["goal_version"],
                    "revision_id": state["head"],
                    "criteria": [
                        {"criterion_id": "total", "disposition": "satisfied", "assessment": "Claim"}
                    ],
                },
            },
        )
        assert (await client.operations(run.id))["items"] == []


async def test_submission_fingerprint_keeps_old_retries_and_binds_new_contract(store):
    from agent_runtime.general_contracts import GeneralPolicy
    from agent_runtime.store import Problem

    agent = await store.agent(
        AgentConfig(name="contract", provider="fake", model="deterministic", general=GeneralPolicy())
    )
    body = RunCreate(agent_id=agent.id, input="Calculate", task=task())
    original = await store.submit(body, "old")
    old_payload = body.model_dump()
    old_payload["task"].pop("result_contract")
    old_fingerprint = digest({"fingerprint_version": 3, **old_payload})
    async with store.database.sessions.begin() as db:
        row = await db.get(RunRow, original.id)
        assert row.fingerprint == old_fingerprint
        row.fingerprint = old_fingerprint  # Represents a retained pre-contract submission.
    assert (await store.submit(body, "old")).id == original.id
    changed = body.model_copy(
        update={
            "task": body.task.model_copy(update={"result_contract": ResultContract(kind="exact", exact="12")})
        }
    )
    with pytest.raises(Problem) as error:
        await store.submit(changed, "old")
    assert error.value.status == 409
    fresh = await store.submit(changed, "new")
    assert (await store.submit(changed, "new")).id == fresh.id
    changed.task.result_contract.exact = "13"
    with pytest.raises(Problem):
        await store.submit(changed, "new")


async def test_contract_api_validation_and_schema_independence(store):
    async with http_client(store) as client:
        agent = await client.create_agent(
            AgentConfig(
                name="contract",
                provider="fake",
                model="deterministic",
                general={},
            )
        )
        for contract in (
            {"kind": "json_schema", "json_schema": {"$ref": "https://example.invalid"}},
            {"kind": "exact"},
            {"kind": "exact", "exact": "12", "json_schema": {}},
        ):
            response = await client.http.post(
                "/v1/runs",
                json={"agent_id": agent.id, "input": "Task", "task": task(contract)},
                headers={"Idempotency-Key": "invalid"},
            )
            assert response.status_code == 422
        schema = (await client.http.get("/openapi.json")).json()
        assert "ResultContract" in schema["components"]["schemas"]
        assert "pydantic_ai" not in json.dumps(schema) and "temporalio" not in json.dumps(schema)


async def test_exact_schema_is_pinned_and_preserved_by_compaction(store, monkeypatch):
    from agent_runtime.context import MemoryContext
    from agent_runtime.general_db import GeneralOperationRow
    from agent_runtime.general_semantic import project_context, wire_type

    stub(monkeypatch, [complete()])
    async with http_client(store) as client:
        run = await submit(client, task=task({"kind": "exact", "exact": "12"}))
        decision = await general_step({"run_id": run.id, "completion_loop": 2})
        async with store.database.sessions() as db:
            op = await db.get(GeneralOperationRow, f"{run.id}:model:0")
            binding = copy.deepcopy(op.data["binding"])
        schema = wire_type(binding).model_json_schema()
        assert schema["$defs"]["Complete"]["properties"]["answer"]["const"] == "12"
        assert "assessments" in schema["$defs"]["Complete"]["required"]
        projected = project_context(binding, "task", [], False)
        compacted = MemoryContext().compact(projected, 1)
        assert compacted["result_contract"]["exact"] == "12"
        assert (await general_action(await general_completion({"run_id": run.id, **decision})))["accepted"]


def test_json_contract_rejects_unpaired_unicode_surrogates():
    contract = {"kind": "json_schema", "json_schema": {"type": "string"}}
    assert validate_result_contract(contract, json.dumps(chr(0xD800))) == ["result_contract:invalid_json"]
