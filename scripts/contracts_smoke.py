"""Fresh bounded result-contract smoke; preflight is unpaid, live requires a source freeze."""

import argparse
import asyncio
import hashlib
import json
import os
import sqlite3
import tempfile
from contextlib import asynccontextmanager, contextmanager, nullcontext
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import httpx
from pydantic_ai.durable_exec.temporal import PydanticAIPlugin
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import FunctionModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from temporalio.client import Client as TemporalClient
from temporalio.client import WorkflowExecutionStatus
from temporalio.service import RPCError, RPCStatusCode
from temporalio.worker import Replayer, Worker

from agent_runtime.api import create_app
from agent_runtime.client import Client
from agent_runtime.db import Database
from agent_runtime.dispatch import Dispatcher
from agent_runtime.evidence import verify_evidence_bundle
from agent_runtime.general_contracts import GeneralPolicy
from agent_runtime.general_runtime import GENERAL_ACTIVITIES
from agent_runtime.general_workflow import GeneralWorkflow, ReconciliationWorkflow
from agent_runtime.model_adapter import request_context
from agent_runtime.resources import RequestNotDispatched
from agent_runtime.runtime import configure_store
from agent_runtime.schemas import AgentConfig
from agent_runtime.store import Store
from scripts.hardening_budget import Transport
from scripts.harness_budget import policy as base_policy

CAMPAIGN = "contracts-2026-10-03-v1"
DIRECTORY = Path("var/acceptance") / CAMPAIGN
MANIFEST = {
    "campaign": CAMPAIGN,
    "version": 1,
    "limit": 6,
    "endpoint": "https://api.openai.com/v1/responses",
    "model": "gpt-5.6-luna",
    "reasoning": "none",
    "scenario_limits": {"exact": 3, "json": 3},
    "scenario_caps": {"exact": 1024, "json": 1024},
}


def sources():
    return {
        str(path): hashlib.sha256(path.read_bytes()).hexdigest()
        for pattern in (
            "agent_runtime/*.py",
            "sandbox/*.py",
            "scripts/*.py",
            "tests/*.py",
            "config/*.json",
            "uv.lock",
            "pyproject.toml",
        )
        for path in sorted(Path().glob(pattern))
    }


def policy(frozen):
    guard = base_policy()
    guard.MANIFEST = {
        **MANIFEST,
        "source_sha256": hashlib.sha256(json.dumps(frozen, sort_keys=True).encode()).hexdigest(),
    }
    guard.TRIGGERS = {
        **guard.TRIGGERS,
        "hard_limit": guard.TRIGGERS["hard_limit"].replace(">=16", ">=6"),
    }
    return guard


def scenario(name):
    if name == "exact":
        contract, answer = {"kind": "exact", "exact": "12"}, "12"
        instruction = "Return exactly 12 with no other text in the final answer."
    else:
        contract, answer = (
            {
                "kind": "json_schema",
                "json_schema": {
                    "type": "object",
                    "properties": {"answer": {"type": "integer", "const": 12}},
                    "required": ["answer"],
                    "additionalProperties": False,
                },
            },
            '{"answer":12}',
        )
        instruction = (
            'Return a JSON object with the sole property "answer" set to the integer 12; no markdown.'
        )
    return {
        "prompt": "Calculate five plus seven. " + instruction,
        "answer": answer,
        "task": {
            "outcome": "Return the sum in the required format",
            "criteria": [{"id": "sum", "statement": "The answer correctly represents five plus seven"}],
            "result_contract": contract,
        },
    }


@asynccontextmanager
async def isolated_store():
    url = os.environ.get(
        "TEST_DATABASE_URL", "postgresql+asyncpg://agents:local-development-only@localhost:5432/agents"
    )
    schema = "test_contracts_" + uuid4().hex
    admin = create_async_engine(url)
    async with admin.begin() as connection:
        await connection.execute(text(f"CREATE SCHEMA {schema}"))
    database = Database(url, schema)
    try:
        await database.create_test_schema()
        store = Store(database)
        configure_store(store)
        yield store, schema
    finally:
        await database.close()
        if not getattr(locals().get("store"), "contracts_cleanup_unresolved", False):
            async with admin.begin() as connection:
                await connection.execute(text(f"DROP SCHEMA {schema} CASCADE"))
        await admin.dispose()


@asynccontextmanager
async def api_client(store):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(store, "test")),
        base_url="http://test",
        headers={"Authorization": "Bearer test"},
    ) as http:
        yield Client(http_client=http)


@asynccontextmanager
async def worker(store, queue):
    temporal = await TemporalClient.connect("localhost:7233", plugins=[PydanticAIPlugin()])
    async with Worker(
        temporal,
        task_queue=queue + "-v3",
        workflows=[GeneralWorkflow, ReconciliationWorkflow],
        activities=GENERAL_ACTIVITIES,
    ):
        dispatcher = asyncio.create_task(Dispatcher(store, temporal, queue).run())
        try:
            yield temporal
        finally:
            dispatcher.cancel()
            await asyncio.gather(dispatcher, return_exceptions=True)


async def settle_workflow(temporal, run_id, queue):
    """Settle this fixture's workflow while its dispatcher is still available."""
    handle = temporal.get_workflow_handle("run:" + run_id)
    description = None
    try:
        async with asyncio.timeout(10):
            while True:
                try:
                    description = await handle.describe()
                except RPCError as exc:
                    if exc.status != RPCStatusCode.NOT_FOUND:
                        raise
                else:
                    if description.task_queue != queue + "-v3" or description.id != "run:" + run_id:
                        raise RuntimeError("Refusing cleanup of a workflow outside this fixture")
                    if description.status != WorkflowExecutionStatus.RUNNING:
                        return {"workflow_status": description.status.name, "workflow_terminated": False}
                await asyncio.sleep(0.05)
    except TimeoutError:
        if description is None:
            raise RuntimeError("Workflow startup could not be reconciled before fixture cleanup") from None
    # Only a verified fixture-owned workflow may be terminated after cancellation stalls.
    await handle.terminate("Isolated result-contract smoke cleanup")
    description = await handle.describe()
    if description.status == WorkflowExecutionStatus.RUNNING:
        raise RuntimeError("Workflow still running after fixture cleanup")
    return {"workflow_status": description.status.name, "workflow_terminated": True}


async def guard_preflight(frozen):
    """Exercise physical-request limits through a mock transport, without credentials."""
    guard = policy(frozen)
    with tempfile.TemporaryDirectory(prefix="contracts-guard-") as directory:
        ledger = Path(directory) / "requests.sqlite"
        guard.initialize(ledger)
        inner = httpx.MockTransport(lambda _: httpx.Response(200, json={"id": "unpaid-fixture"}))
        async with httpx.AsyncClient(transport=Transport(ledger, guard, inner=inner)) as client:
            for name in MANIFEST["scenario_limits"]:
                guard.admit(ledger, name, name)
                for index in range(4):
                    token = request_context.set(
                        {
                            "scenario": name,
                            "run_id": name,
                            "root_id": name,
                            "operation_id": str(index),
                            "attempt_id": name + str(index),
                        }
                    )
                    try:
                        try:
                            await client.post(
                                MANIFEST["endpoint"],
                                json={
                                    "model": MANIFEST["model"],
                                    "reasoning": {"effort": "none"},
                                    "max_output_tokens": 1024,
                                },
                            )
                        except RequestNotDispatched:
                            assert index == 3
                        else:
                            assert index < 3
                    finally:
                        request_context.reset(token)
                guard.finish(ledger, name, "passed")
        assert guard.validate(ledger) == 6


@contextmanager
def account_terminal_campaign(guard, ledger, live):
    try:
        yield
    finally:
        if live:
            guard.reconcile_unknown(ledger)
            for name in MANIFEST["scenario_limits"]:
                # Existing terminal outcomes are immutable; fill only interrupted scenarios.
                guard.finish(ledger, name, "interrupted")


async def run(live=False, freeze=False):
    frozen = sources()
    guard, ledger = policy(frozen), DIRECTORY / "requests.sqlite"
    usage = []
    if live:
        preflight = json.loads((DIRECTORY / "preflight.json").read_text())
        if preflight != {"sources": frozen, "passed": ["exact", "json"]}:
            raise RuntimeError("Current source lacks a matching passed preflight")
        if (DIRECTORY / "source.json").exists() or ledger.exists() or ledger.with_suffix(".started").exists():
            raise RuntimeError("Campaign already started; never resubmit or move its allowance")
        from dotenv import load_dotenv

        load_dotenv(".env.local", override=False)
        if not os.environ.get("OPENAI_API_KEY"):
            raise RuntimeError("Authorized credential is unavailable")
        with (DIRECTORY / "source.json").open("x") as file:
            json.dump(frozen, file, sort_keys=True, indent=2)
        guard.initialize(ledger)
    else:
        await guard_preflight(frozen)

    async def record(response):
        await response.aread()
        context = request_context.get() or {}
        try:
            reported = response.json().get("usage", {})
        except ValueError:
            reported = {}
        item = {
            key: context.get(key) for key in ("scenario", "root_id", "run_id", "operation_id", "attempt_id")
        }
        item.update(
            status=response.status_code,
            usage={
                key: reported[key]
                for key in ("input_tokens", "output_tokens", "total_tokens")
                if key in reported
            },
        )
        usage.append(item)
        fd = os.open(DIRECTORY / "usage.jsonl", os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "a") as file:
            file.write(json.dumps(item) + "\n")
            file.flush()
            os.fsync(file.fileno())

    def transport(_):
        if frozen != sources():
            raise RequestNotDispatched("evaluation_limit")
        return httpx.AsyncClient(
            transport=Transport(ledger, guard),
            trust_env=False,
            timeout=35,
            event_hooks={"response": [record]},
        )

    results = []
    with (
        account_terminal_campaign(guard, ledger, live),
        patch("agent_runtime.model_adapter.http_client_factory", transport) if live else nullcontext(),
    ):
        async with isolated_store() as (store, queue):
            async with api_client(store) as client:
                for name in MANIFEST["scenario_limits"]:
                    fixture = scenario(name)
                    calls = []

                    async def respond(messages, info):
                        context = json.loads(messages[-1].parts[0].content)
                        calls.append(context)
                        answer = "incorrect" if len(calls) == 1 else fixture["answer"]
                        return ModelResponse(
                            parts=[
                                ToolCallPart(
                                    info.output_tools[0].name,
                                    {
                                        "action": {
                                            "kind": "complete",
                                            "answer": answer,
                                            "assessments": [
                                                {
                                                    "criterion": "c0",
                                                    "disposition": "satisfied",
                                                    "assessment": "Computed five plus seven",
                                                }
                                            ],
                                        }
                                    },
                                )
                            ]
                        )

                    with (
                        patch("agent_runtime.general_runtime.build_model", lambda _: FunctionModel(respond))
                        if not live
                        else nullcontext()
                    ):
                        agent = await client.create_agent(
                            AgentConfig(
                                name=name,
                                provider="openai" if live else "fake",
                                model=MANIFEST["model"] if live else "deterministic",
                                tools=[],
                                max_tokens=1024,
                                general=GeneralPolicy(
                                    limits={"model_attempts": 3}, resources={"on_limit": "fail"}
                                ),
                            )
                        )
                        submitted = await client.submit(
                            agent.id, ("" if live else "semantic: ") + fixture["prompt"], task=fixture["task"]
                        )
                        if live:
                            guard.admit(ledger, name, submitted.id)
                        result = {"scenario": name, "run_id": submitted.id, "passed": False}
                        async with worker(store, queue) as temporal:
                            try:
                                done = await client.wait(submitted.id, timeout=90)
                                result.update(status=done.status, error=done.error)
                                assert done.status == "completed"
                                handle = temporal.get_workflow_handle("run:" + done.id)
                                await handle.result()
                                bundle = await client.evidence(done.id)
                                verified = verify_evidence_bundle(bundle)
                                assert verified.valid and verified.integrity_verified, verified.errors
                                assert not verified.origin_authenticated and not verified.commands_executed
                                assert (
                                    "answer:" + fixture["task"]["result_contract"]["kind"]
                                    in verified.deterministic_checks
                                )
                                assert (await client.task(done.id))["assessment"]["accepted"]
                                await Replayer(
                                    workflows=[GeneralWorkflow], plugins=[PydanticAIPlugin()]
                                ).replay_workflow(await handle.fetch_history())
                                result.update(
                                    passed=True,
                                    evidence_sha256=bundle.sha256,
                                    answer=done.output.answer,
                                    requests=(await client.budget(done.id))["v3"]["counters"][
                                        "model_attempts"
                                    ],
                                )
                                if live:
                                    with (DIRECTORY / (name + ".evidence.json")).open("x") as file:
                                        file.write(bundle.model_dump_json(indent=2))
                                else:
                                    assert len(calls) == 2
                            except Exception as exc:
                                result.update(passed=False, exception_type=type(exc).__name__)
                                try:
                                    await client.cancel(submitted.id)
                                except Exception as cleanup_error:
                                    result["cancellation_error"] = type(cleanup_error).__name__
                            finally:
                                try:
                                    workflow_state = await settle_workflow(temporal, submitted.id, queue)
                                    result.update(workflow_state)
                                    if workflow_state["workflow_terminated"]:
                                        result["passed"] = False
                                    await store.general_cleanup(submitted.id)
                                    result["cleanup_state"] = (await client.get(submitted.id)).cleanup_state
                                except Exception as cleanup_error:
                                    store.contracts_cleanup_unresolved = True
                                    result.update(
                                        passed=False,
                                        cleanup_error=type(cleanup_error).__name__,
                                        retained_schema=queue,
                                    )
                                results.append(result)
                                if live:
                                    guard.finish(ledger, name, "passed" if result["passed"] else "failed")
    output = {"live": live, "scenarios": results, "source_unchanged": frozen == sources(), "usage": usage}
    if live:
        guard.reconcile_unknown(ledger)
        output["physical_requests"] = guard.validate(ledger)
        with sqlite3.connect(ledger) as db:
            output["outcomes"] = db.execute(
                "SELECT classification,count(*) FROM outcomes GROUP BY classification"
            ).fetchall()
    passed = output["source_unchanged"] and all(result["passed"] for result in results)
    if freeze:
        if not passed:
            raise RuntimeError("Cannot freeze a failed preflight")
        DIRECTORY.mkdir(parents=True, exist_ok=True)
        with (DIRECTORY / "preflight.json").open("x") as file:
            json.dump({"sources": frozen, "passed": ["exact", "json"]}, file, sort_keys=True, indent=2)
    print(json.dumps(output, indent=2))
    if not passed:
        raise RuntimeError("Smoke failed; preserve terminal accounting and do not resubmit")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument(
        "--preflight", action="store_true", help="Run fake model/service checks without paid requests"
    )
    modes.add_argument("--live", action="store_true", help="Execute the fresh frozen campaign once")
    parser.add_argument(
        "--freeze", action="store_true", help="Save the passed fake preflight and source hashes"
    )
    args = parser.parse_args()
    if args.freeze and not args.preflight:
        parser.error("--freeze requires --preflight")
    asyncio.run(run(live=args.live, freeze=args.freeze))
