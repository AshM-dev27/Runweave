import json

import pytest
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import FunctionModel
from test_general_semantic import complete, http_client, stub, submit

from agent_runtime.general_completion import general_completion
from agent_runtime.general_contracts import GeneralPolicy
from agent_runtime.general_db import GeneralOperationRow
from agent_runtime.general_runtime import general_action, general_step


def reviewer(monkeypatch, verdict="pass", confidence=0.99, fail=False):
    calls = []

    def respond(messages, info):
        request = json.loads(messages[-1].parts[0].content)
        calls.append(request)
        if fail:
            raise RuntimeError("SECRET must never escape")
        return ModelResponse(
            parts=[
                ToolCallPart(
                    info.output_tools[0].name,
                    {
                        "judgments": [
                            {
                                "criterion_id": c["id"],
                                "verdict": verdict,
                                "confidence": confidence,
                                "reason": "Test review",
                            }
                            for c in request["criteria"]
                        ]
                    },
                )
            ]
        )

    monkeypatch.setattr(
        "agent_runtime.completion_review.build_review_model", lambda _: FunctionModel(respond)
    )
    return calls


@pytest.mark.parametrize(
    "verdict,confidence,error,accepted",
    [
        ("pass", 0.99, False, True),
        ("repair", 0.99, False, False),
        ("defer", 0.99, False, False),
        ("pass", 0.6, False, False),
        ("pass", 0.99, True, False),
    ],
)
async def test_optional_review_fails_closed_and_retry_reuses_receipt(
    store, monkeypatch, verdict, confidence, error, accepted
):
    stub(monkeypatch, [complete()])
    calls = reviewer(monkeypatch, verdict, confidence, error)
    async with http_client(store) as client:
        run = await submit(client, policy=GeneralPolicy(review={}))
        proposal = await general_step({"run_id": run.id, "completion_loop": 2})
        prepared = await general_completion({"run_id": run.id, **proposal})
        result = await general_action(prepared)
        assert result["accepted"] is accepted, result
        assert (await general_action(prepared)) == result
        assert len(calls) == 1
        operations = await client.operations(run.id)
        assert "SECRET" not in json.dumps(operations)
        assert (await client.budget(run.id))["v3"]["counters"]["model_attempts"] == 2
        reviews = [op for op in operations["items"] if ":review:" in op["id"]]
        assert len(reviews) == 1
        if error:
            assert reviews[0]["result"]["reason"] == "review_unavailable"


async def test_source_review_gets_full_source_not_only_candidate_quote(store, monkeypatch):
    stub(
        monkeypatch,
        [
            {"kind": "source", "path": "policy.txt", "criterion": "c0", "quote": "Rollback is supported"},
            complete(),
        ],
    )
    calls = reviewer(monkeypatch, "repair")
    async with http_client(store) as client:
        workspace = await client.workspace_create(
            {"policy.txt": b"Rollback is supported BEFORE migration. After step 5 it is impossible."}
        )
        run = await submit(
            client,
            ["workspace_read"],
            workspace,
            {
                "outcome": "Is rollback supported after step 5?",
                "criteria": [
                    {"id": "source", "statement": "Answer from policy", "evidence_policy": "source"}
                ],
            },
            GeneralPolicy(review={}),
        )
        source = await general_step({"run_id": run.id, "completion_loop": 2})
        await general_action({"run_id": run.id, **source})
        proposal = await general_step({"run_id": run.id, "completion_loop": 2})
        result = await general_action(await general_completion({"run_id": run.id, **proposal}))
        assert not result["accepted"]
        assert "After step 5 it is impossible" in calls[0]["sources"][0]["source"]
        assert calls[0]["sources"][0]["capture_complete"]


async def test_missing_deterministic_evidence_never_calls_reviewer(store, monkeypatch):
    stub(monkeypatch, [complete()])
    calls = reviewer(monkeypatch)
    async with http_client(store) as client:
        workspace = await client.workspace_create({"result.txt": b"wrong"})
        run = await submit(
            client,
            ["workspace_verify"],
            workspace,
            {
                "outcome": "Produce right",
                "criteria": [
                    {
                        "id": "bytes",
                        "statement": "Exact output",
                        "evidence_policy": "check",
                        "checks": [
                            {"id": "exact", "kind": "bytes", "path": "result.txt", "expected": "cmlnaHQ="}
                        ],
                    }
                ],
            },
            GeneralPolicy(review={}),
        )
        proposal = await general_step({"run_id": run.id, "completion_loop": 2})
        payload = {"run_id": run.id, **proposal}
        while True:
            prepared = await general_completion(payload)
            result = await general_action(prepared)
            if prepared["final"]:
                break
        assert not result["accepted"] and calls == []


async def test_pending_check_disposition_runs_check_but_uncertainty_stays_blocking(store, monkeypatch):
    actions = [
        dict(kind="complete", answer="ready", assessments=[dict(criterion="c0", disposition="pending")])
    ]
    stub(monkeypatch, actions)
    async with http_client(store) as client:
        workspace = await client.workspace_create({"result.txt": b"right"})
        run = await submit(
            client,
            ["workspace_verify"],
            workspace,
            {
                "outcome": "Produce right",
                "criteria": [
                    {
                        "id": "bytes",
                        "statement": "Exact output",
                        "evidence_policy": "check",
                        "checks": [
                            {"id": "exact", "kind": "bytes", "path": "result.txt", "expected": "cmlnaHQ="}
                        ],
                    }
                ],
            },
        )
        proposal = await general_step({"run_id": run.id, "completion_loop": 2})
        payload = {"run_id": run.id, **proposal}
        for _ in range(2):
            prepared = await general_completion(payload)
            result = await general_action(prepared)
        assert result["accepted"]
        async with store.database.sessions() as db:
            model = await db.get(GeneralOperationRow, run.id + ":model:0")
            assert model.data["semantic_assessments"][0]["disposition"] == "pending"


async def test_incomplete_source_defers_before_paid_review(store, monkeypatch):
    from agent_runtime.completion_review import ensure_review

    calls = reviewer(monkeypatch)
    stub(
        monkeypatch,
        [{"kind": "source", "path": "large.txt", "criterion": "c0", "quote": "real quote"}, complete()],
    )
    async with http_client(store) as client:
        workspace = await client.workspace_create({"large.txt": b"real quote\n" + b"context\n" * 1000})
        run = await submit(
            client,
            ["workspace_read"],
            workspace,
            {
                "outcome": "Answer from source",
                "criteria": [{"id": "c", "statement": "Supported", "evidence_policy": "source"}],
            },
            GeneralPolicy(review={}),
        )
        d = await general_step({"run_id": run.id, "completion_loop": 2})
        await general_action({"run_id": run.id, **d})
        d = await general_step({"run_id": run.id, "completion_loop": 2})
        prepared = await general_completion({"run_id": run.id, **d})
        await ensure_review(store, run.id, prepared["decision"]["action"])
        result = await general_action(prepared)
        assert not calls
        assert result["review"]["reason"] == "incomplete_review_context"


async def test_changed_answer_cannot_reuse_a_previous_passing_review(store, monkeypatch):
    from agent_runtime.completion_review import ensure_review

    stub(monkeypatch, [complete()])
    calls = reviewer(monkeypatch)
    async with http_client(store) as client:
        run = await submit(client, policy=GeneralPolicy(review={}))
        d = await general_step({"run_id": run.id, "completion_loop": 2})
        prepared = await general_completion({"run_id": run.id, **d})
        candidate = prepared["decision"]["action"]
        await ensure_review(store, run.id, candidate)
        assert len(calls) == 1
        candidate["answer"] = "A different unsupported answer"
        async with store.database.sessions.begin() as db:
            row, gr, _ = await store.general_lock(db, run.id)
            result = await store.general_complete(db, row, gr, candidate)
        assert not result["accepted"] and result["review"]["reason"] == "review_required"


async def test_ambiguous_multiple_reviewer_outputs_fail_closed(store, monkeypatch):
    def respond(messages, info):
        request = json.loads(messages[-1].parts[0].content)
        args = {
            "judgments": [
                dict(criterion_id=c["id"], verdict="pass", confidence=1, reason="Supported")
                for c in request["criteria"]
            ]
        }
        return ModelResponse(
            parts=[
                ToolCallPart(info.output_tools[0].name, args),
                ToolCallPart(info.output_tools[0].name, args),
            ]
        )

    monkeypatch.setattr(
        "agent_runtime.completion_review.build_review_model", lambda _: FunctionModel(respond)
    )
    stub(monkeypatch, [complete()])
    async with http_client(store) as client:
        run = await submit(client, policy=GeneralPolicy(review={}))
        proposal = await general_step({"run_id": run.id, "completion_loop": 2})
        result = await general_action(await general_completion({"run_id": run.id, **proposal}))
        assert not result["accepted"]
        assert result["review"]["reason"] == "review_unavailable"
