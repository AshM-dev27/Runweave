"""Replay authored proposals through real API, PostgreSQL, and Temporal; no paid model."""

import json
import os
import time
from pathlib import Path

import pytest
from pydantic_ai.durable_exec.temporal import PydanticAIPlugin
from temporalio.worker import Replayer
from test_general_integration import backend
from test_general_semantic import stub

from agent_runtime.general_contracts import GeneralPolicy
from agent_runtime.general_workflow import GeneralWorkflow
from agent_runtime.schemas import AgentConfig
from scripts.jev_benchmark import digest, load_cases

pytestmark = pytest.mark.integration
DATASET = Path("tests/fixtures/jev-sample-cases-2026-09-23.json")


async def test_fresh_proposals_current_completion_pipeline(pg_store, monkeypatch, tmp_path):
    dataset = load_cases(DATASET)
    cases = {c["id"]: c for c in dataset["cases"] if c["task"] != "context_relevance"}
    counts = {}

    def next_action(context, _):
        cid = context["input"].split("\n", 1)[0].removeprefix("semantic: ")
        case = cases[cid]
        index = counts.get(cid, 0)
        counts[cid] = index + 1
        source = case["task"] == "source_support"
        if source and index == 0:
            return {
                "kind": "source",
                "path": "source.txt",
                "criterion": "c0",
                "quote": case["state"]["quote"],
            }
        return {
            "kind": "complete",
            "answer": case["state"]["claim" if source else "answer"],
            "assessments": [
                {
                    "criterion": "c0",
                    "disposition": "satisfied",
                    "assessment": "Scripted candidate asserts this requirement is satisfied.",
                }
            ],
        }

    stub(monkeypatch, next_action)
    output = Path(os.environ.get("JEV_SAMPLE_REPLAY_OUTPUT", str(tmp_path / "jev-sample-runtime.json")))
    if output.exists():
        raise AssertionError("Use a new evidence path; previous results cannot be overwritten.")
    report = {
        "dataset_sha256": digest(dataset),
        "mode": "real_ASGI_API_PostgreSQL_Temporal_scripted_FunctionModel",
        "paid_generative_calls": 0,
        "rows": [],
    }
    async with backend(pg_store, monkeypatch) as (client, temporal):
        agent = await client.create_agent(
            AgentConfig(
                name="jev-sample-replay",
                provider="fake",
                model="deterministic",
                tools=["workspace_read"],
                general=GeneralPolicy(),
            )
        )
        for case in cases.values():
            source = case["task"] == "source_support"
            workspace = (
                await client.workspace_create({"source.txt": case["state"]["source"].encode()})
                if source
                else None
            )
            criterion = (
                "Check this source-backed claim: " + case["state"]["claim"]
                if source
                else case["state"]["requirement"]
            )
            started = time.perf_counter()
            run = await client.submit(
                agent.id,
                "semantic: " + case["id"] + "\n" + case["sample_request"],
                workspace=workspace,
                task={
                    "outcome": case["sample_request"],
                    "criteria": [
                        {
                            "id": "sample",
                            "statement": criterion,
                            "evidence_policy": "source" if source else "assessment",
                        }
                    ],
                },
            )
            result = await client.wait(run.id, timeout=45)
            elapsed = (time.perf_counter() - started) * 1000
            assessment = (await client.task(run.id))["assessment"]
            receipts = (await client.verifications(run.id))["items"]
            expected_answer = case["state"]["claim" if source else "answer"]
            assert result.status == "completed", (case["id"], result.error)
            assert result.output.answer == expected_answer
            assert assessment["accepted"]
            assert len(receipts) == int(source)
            replayed = False
            if case["id"] in {"incident_source_1", "incident_coverage_1"}:
                history = await temporal.get_workflow_handle("run:" + run.id).fetch_history()
                await Replayer(workflows=[GeneralWorkflow], plugins=[PydanticAIPlugin()]).replay_workflow(
                    history
                )
                replayed = True
            report["rows"].append(
                {
                    "case_id": case["id"],
                    "run_id": run.id,
                    "status": result.status,
                    "accepted": assessment["accepted"],
                    "answer": result.output.answer,
                    "expected_label": case["expected"],
                    "source_receipts": len(receipts),
                    "elapsed_ms_including_client_poll": round(elapsed, 3),
                    "scripted_model_calls": counts[case["id"]],
                    "temporal_replay_passed": replayed,
                }
            )
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
            print(
                json.dumps({"sample": case["id"], "status": result.status, "expected": case["expected"]}),
                flush=True,
            )
    assert len(report["rows"]) == 36
