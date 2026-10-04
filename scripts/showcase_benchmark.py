"""Unpaid business showcases through real HTTP/Postgres/Temporal, with synthetic services.

Run: uv run python -m scripts.showcase_benchmark
Requires the local PostgreSQL and Temporal services. Never reads dotenv files.
"""

import argparse
import asyncio
import hashlib
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from temporalio.client import Client as TemporalClient
from temporalio.worker import Replayer

from agent_runtime.client import Client
from agent_runtime.db import Database
from agent_runtime.general_contracts import GeneralLimits, GeneralPolicy
from agent_runtime.general_workflow import GeneralWorkflow
from agent_runtime.schemas import AgentConfig
from scripts.showcase_adapter import manifest
from scripts.showcase_fixtures import create_fixtures, csv_text

ROOT = Path(__file__).resolve().parents[1]
DATABASE_URL = "postgresql+asyncpg://agents:local-development-only@127.0.0.1:5432/agents"
TERMINAL = {"completed", "failed", "cancelled"}


def port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def until(function, *, timeout=180, label="condition"):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = await function()
        if value:
            return value
        await asyncio.sleep(0.3)
    raise TimeoutError(label)


class Processes:
    def __init__(self, directory, environment):
        self.directory, self.environment, self.processes = directory, environment, []
        self.handles = []

    def start(self, name, module, *args):
        log = open(self.directory / f"{name}-{len(self.processes)}.log", "w")
        process = subprocess.Popen(
            [sys.executable, "-m", module, *args],
            cwd=ROOT,
            env=self.environment,
            stdout=log,
            stderr=log,
            start_new_session=True,
        )
        self.handles.append(log)
        self.processes.append(process)
        return process

    def stop(self):
        for process in reversed(self.processes):
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
        for process in reversed(self.processes):
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
        for handle in self.handles:
            handle.close()


async def main(output):
    output.mkdir(parents=True, exist_ok=False)
    fixture_dir = output / "fixtures"
    expected_counts = create_fixtures(fixture_dir)
    schema = "test_showcase_" + uuid4().hex
    queue = schema
    fixture_port, api_port = port(), port()
    fixture_url, api_url = f"http://127.0.0.1:{fixture_port}", f"http://127.0.0.1:{api_port}"
    benchmark_key = "showcase-" + uuid4().hex
    report = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "mode": "unpaid_simulation",
        "real": [
            "HTTP API",
            "PostgreSQL",
            "Temporal",
            "worker process restart",
            "approvals",
            "receipt reconciliation",
            "Browser Use SDK transport",
        ],
        "simulated": ["agent decisions", "CRM", "payments", "supplier browser extraction"],
        "limitations": [
            "Does not measure live model planning or extraction accuracy.",
            "Fixture service receipts are stronger than some real upstream integrations.",
            "Success is bounded to these synthetic cases; not production certification.",
        ],
        "provider_cost_usd": 0,
        "cases": [],
        "cleanup": {},
    }
    frozen = {
        str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
        for pattern in (
            "scripts/showcase_*.py",
            "tests/test_showcase_benchmark.py",
            "agent_runtime/*.py",
            "config/*.json",
        )
        for p in sorted(ROOT.glob(pattern))
    }
    database = Database(DATABASE_URL, schema)
    admin = create_async_engine(DATABASE_URL)
    temporal = await TemporalClient.connect("127.0.0.1:7233")
    run_ids = []
    case_started = {}
    recovery_ids = []
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="runweave-showcase-") as tmp:
        temp = Path(tmp)
        (temp / "extensions.json").write_text(json.dumps(manifest(fixture_url)))
        environment = {
            k: v
            for k, v in os.environ.items()
            if k
            not in {
                "OPENAI_API_KEY",
                "ANTHROPIC_API_KEY",
                "BROWSER_USE_API_KEY",
                "OTEL_EXPORTER_OTLP_ENDPOINT",
            }
        }
        environment.update(
            {
                "DATABASE_URL": DATABASE_URL,
                "DATABASE_SCHEMA": schema,
                "TASK_QUEUE": queue,
                "TEMPORAL_ADDRESS": "127.0.0.1:7233",
                "TEMPORAL_NAMESPACE": "default",
                "API_KEY": benchmark_key,
                "EXTENSION_REGISTRY_FILE": str(temp / "extensions.json"),
                "SHOWCASE_URL": fixture_url,
                "SHOWCASE_FIXTURES": str(fixture_dir.resolve()),
                "SHOWCASE_LEDGER": str(temp / "service.sqlite"),
                "BROWSER_USE_API_KEY": "showcase-dummy-key",
                "PYTHONUNBUFFERED": "1",
                "OTEL_EXPORTER_OTLP_ENDPOINT": "",
                "NO_PROXY": "127.0.0.1,localhost",
            }
        )
        processes = Processes(temp, environment)
        try:
            async with admin.begin() as conn:
                await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
            await database.create_test_schema()
            processes.start(
                "services",
                "uvicorn",
                "scripts.showcase_services:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(fixture_port),
                "--no-access-log",
            )
            processes.start(
                "api",
                "uvicorn",
                "agent_runtime.api:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(api_port),
                "--no-access-log",
            )
            worker = processes.start("worker", "scripts.showcase_adapter")
            async with (
                httpx.AsyncClient(base_url=fixture_url, trust_env=False) as upstream,
                Client(api_url, benchmark_key) as client,
            ):

                async def ready():
                    try:
                        response = await upstream.get("/health")
                        return response.status_code == 200 and bool(await client.models())
                    except Exception:
                        return False

                await until(ready, timeout=45, label="local API startup")
                agents = {}
                for kind, aliases in {
                    "crm": ["crm_preview", "crm_import"],
                    "refund": ["order_lookup", "refund_order"],
                    "supplier": ["browser_task"],
                }.items():
                    agents[kind] = await client.create_agent(
                        AgentConfig(
                            name="Unpaid showcase: " + kind,
                            provider="fake",
                            model="deterministic",
                            adaptive=False,
                            tools=aliases,
                            instructions="Synthetic scripted planner. Report only actual tool receipts.",
                            general=GeneralPolicy(
                                limits=GeneralLimits(
                                    model_attempts=8, tool_attempts=10, total_tokens=16000, active_seconds=300
                                )
                            ),
                            timeout_seconds=300,
                            max_requests=8,
                            max_tool_calls=10,
                            max_total_tokens=16000,
                        )
                    )
                artifact = await client.upload(
                    (fixture_dir / "customers.csv").read_bytes(), "text/csv", "customers.csv"
                )

                async def service_state():
                    response = await upstream.get("/state")
                    response.raise_for_status()
                    return response.json()

                async def submit(name, case, *, attachments=None):
                    case_started[name] = time.monotonic()
                    run = await client.submit(
                        agents[case["kind"]].id,
                        "showcase:" + json.dumps(case),
                        artifact_ids=attachments or [],
                        idempotency_key=name,
                        task={
                            "outcome": name,
                            "criteria": [{"id": "result", "statement": "Report observed tool outcome"}],
                        },
                    )
                    run_ids.append(run.id)
                    print(json.dumps({"event": "submitted", "case": name, "run_id": run.id}), flush=True)
                    return run

                async def approve(run):
                    paused = await client.wait(run.id, timeout=60)
                    assert paused.status == "awaiting_approval" and len(paused.approvals) == 1, (
                        paused.model_dump()
                    )
                    await client.decide(run.id, paused.approvals[0].id, True)
                    return paused.approvals[0].model_dump()

                async def record(name, run, details, *, succeeded=True):
                    final = await client.get(run.id)
                    if succeeded:
                        assert final.outcome == "succeeded", final.model_dump()
                    operations = await client.operations(run.id)
                    if succeeded:
                        evidence = (await client.evidence(run.id)).model_dump(mode="json")
                    else:
                        rejected = await client.http.get(f"/v1/runs/{run.id}/evidence")
                        assert rejected.status_code == 409
                        evidence = {"accepted_completion_export_refused": True, "detail": rejected.json()}
                    case = {
                        "name": name,
                        "passed": True,
                        "duration_seconds": round(time.monotonic() - case_started[name], 2),
                        "run_id": run.id,
                        "status": final.status,
                        "outcome": final.outcome,
                        "cleanup_state": final.cleanup_state,
                        "result": json.loads(final.output.answer)
                        if final.output and final.outcome == "succeeded"
                        else None,
                        "details": details,
                    }
                    report["cases"].append(case)
                    (output / (name + ".json")).write_text(
                        json.dumps(
                            {
                                "case": case,
                                "run": final.model_dump(mode="json"),
                                "operations": operations,
                                "evidence": evidence,
                            },
                            indent=2,
                        )
                        + "\n"
                    )
                    print(json.dumps({"event": "passed", **case}), flush=True)

                # CRM: real API artifact + approval, then SIGKILL after remote commit before its response.
                run = await submit(
                    "crm_restart", {"kind": "crm", "artifact_id": artifact.id}, attachments=[artifact.id]
                )
                paused = await client.wait(run.id, timeout=60)
                assert paused.status == "awaiting_approval", paused.model_dump()
                assert (await service_state()).get("crm_posts", 0) == 0
                preview = (await service_state())["plan:" + paused.approvals[0].arguments["plan_id"]]
                assert preview["counts"] == expected_counts
                shown = paused.approvals[0].preview
                assert shown and shown.source_operation_id == run.id + ":action:0"
                assert {f.label: f.value for f in shown.facts}["Customers to import"] == "160"
                assert not shown.warnings
                (output / "crm-approval.json").write_text(
                    json.dumps(paused.approvals[0].model_dump(), indent=2)
                )
                await client.decide(run.id, paused.approvals[0].id, True)

                async def committed():
                    return (await service_state()).get("crm_committed")

                await until(committed, timeout=30, label="CRM commit before crash")
                crash_at = time.monotonic()
                os.killpg(worker.pid, signal.SIGKILL)
                worker.wait(timeout=5)
                worker = processes.start("worker-restarted", "scripts.showcase_adapter")
                print(json.dumps({"event": "worker_killed_after_crm_commit", "run_id": run.id}), flush=True)
                final = await client.wait(run.id, timeout=180)
                upstream_state = await service_state()
                assert len(upstream_state["customers"]) == 160
                assert set(upstream_state["customers"]) == {
                    f"customer{i:03d}@example.test" for i in range(160)
                }
                assert upstream_state["crm_posts"] == 1 and upstream_state["receipt_lookups"] >= 1
                assert json.loads(final.output.answer)["imported"] == 160
                assert time.monotonic() - crash_at < 30, "Worker recovery exceeded target"
                (output / "customers-clean.csv").write_text(
                    csv_text(preview["accepted"], ["name", "email", "company", "country"])
                )
                (output / "customers-exceptions.csv").write_text(
                    csv_text(preview["exceptions"], ["row", "reason", "name", "email", "company", "country"])
                )
                await record(
                    "crm_restart",
                    run,
                    {
                        "counts": expected_counts,
                        "remote_posts": 1,
                        "duplicates_created": 0,
                        "recovery_seconds": round(time.monotonic() - crash_at, 2),
                        "no_write_before_approval": True,
                    },
                )

                run = await submit(
                    "crm_denied", {"kind": "crm", "artifact_id": artifact.id}, attachments=[artifact.id]
                )
                paused = await client.wait(run.id, timeout=60)
                assert paused.status == "awaiting_approval"
                await client.decide(run.id, paused.approvals[0].id, False)
                final = await client.wait(run.id, timeout=60)
                assert final.outcome != "succeeded" and (await service_state())["crm_posts"] == 1
                await record("crm_denied", run, {"additional_remote_writes": 0}, succeeded=False)

                run = await submit("refund_lost_response", {"kind": "refund", "order_id": "TEST-104"})
                paused = await client.wait(run.id, timeout=60)
                assert paused.status == "awaiting_approval"
                assert (await service_state()).get("refund_posts", 0) == 0
                assert paused.approvals[0].arguments == {"order_id": "TEST-104", "amount_cents": 4900}
                await client.decide(run.id, paused.approvals[0].id, True)
                final = await client.wait(run.id, timeout=90)
                assert json.loads(final.output.answer)["amount_cents"] == 4900
                assert (await service_state())["refund_posts"] == 1
                await record(
                    "refund_lost_response",
                    run,
                    {"refunded_usd": "49.00", "remote_posts": 1, "no_write_before_approval": True},
                )

                for name, order_id, reason in [
                    ("refund_ineligible", "TEST-105", "outside_refund_window"),
                    ("refund_already_done", "TEST-106", "already_refunded"),
                ]:
                    run = await submit(name, {"kind": "refund", "order_id": order_id})
                    final = await client.wait(run.id, timeout=60)
                    assert final.outcome != "succeeded" and not final.approvals
                    assert (await service_state())["refund_posts"] == 1
                    assert reason in json.dumps(final.model_dump(mode="json"))
                    await record(
                        name, run, {"reason": reason, "additional_remote_writes": 0}, succeeded=False
                    )

                run = await submit("refund_unknown_receipt", {"kind": "refund", "order_id": "TEST-107"})
                await approve(run)

                async def terminal():
                    current = await client.get(run.id)
                    return current if current.status in TERMINAL else None

                final = await until(terminal, timeout=90, label="unknown refund fails closed")
                recovery = await client.recovery(run.id)
                unknown = [r for r in recovery["unresolved"] if r["kind"] == "external_write"]
                assert len(unknown) == 1 and final.outcome != "succeeded"
                assert (await service_state())["refund_posts"] == 2
                response = await upstream.post("/reveal/" + unknown[0]["target_id"])
                response.raise_for_status()
                receipt = await client.reconcile(run.id, unknown[0], idempotency_key="recover-unknown-refund")

                recovery_ids.append(receipt["id"])

                async def resolved():
                    current = await client.reconciliation(run.id, receipt["id"])
                    return current if current["status"] == "complete" else None

                resolved_receipt = await until(resolved, timeout=90, label="operator lookup reconciliation")
                assert not (await client.recovery(run.id))["unresolved"]
                assert (await service_state())["refund_posts"] == 2
                await client.wait(run.id, timeout=90)
                await record(
                    "refund_unknown_receipt",
                    run,
                    {
                        "fail_closed": True,
                        "second_post_for_this_refund": False,
                        "reconciliation": resolved_receipt,
                    },
                    succeeded=False,
                )

                task = "Compare 50 units of exact part ABC-123 at the three synthetic supplier pages. Normalize packs, stock and shipping; do not buy."
                run = await submit("supplier_comparison", {"kind": "supplier", "task": task})
                await approve(run)
                final = await client.wait(run.id, timeout=90)
                answer = json.loads(final.output.answer)
                comparison = answer["data"]
                assert comparison["selected"]["supplier"] == "A"
                assert comparison["selected"]["total_cents"] == 22500
                assert comparison["selected"]["packs_needed"] == 5
                assert [o["reason"] for o in comparison["offers"]] == [
                    "qualified",
                    "insufficient_stock",
                    "shipping_unknown",
                    "wrong_variant",
                ]
                assert comparison["offers"][2]["total_cents"] is None
                assert answer["cleanup_complete"] and answer["schema_validated"]
                state = await service_state()
                assert state["browser_creates"] == 1 and state["browser_stops"] == 1
                (output / "supplier-comparison.json").write_text(json.dumps(comparison, indent=2) + "\n")
                await record(
                    "supplier_comparison",
                    run,
                    {
                        "total_myr": "225.00",
                        "selected_supplier": "A",
                        "owned_browsers_stopped": 1,
                        "external_creates": 1,
                    },
                )

                run = await submit("supplier_cancel", {"kind": "supplier", "task": task + " CANCEL_DEMO"})
                await approve(run)

                async def browser_started():
                    return (await service_state()).get("browser_creates") == 2

                await until(browser_started, timeout=30, label="browser created before cancel")
                await client.cancel(run.id)
                final = await client.wait(run.id, timeout=90)
                state = await service_state()
                assert final.status == "cancelled" and final.cleanup_state == "complete"
                assert time.monotonic() - case_started["supplier_cancel"] < 15, (
                    "Cancellation cleanup exceeded target"
                )
                assert (
                    state["browser_creates"] == 2
                    and state["browser_cancels"] == 1
                    and state["browser_stops"] == 2
                )
                assert all(value == "stopped" for key, value in state.items() if key.startswith("browser:"))
                await record(
                    "supplier_cancel",
                    run,
                    {"provider_cancelled": True, "owned_browsers_stopped": 1},
                    succeeded=False,
                )

                replayed = []
                for run_id in run_ids:
                    history = await temporal.get_workflow_handle("run:" + run_id).fetch_history()
                    await Replayer(workflows=[GeneralWorkflow]).replay_workflow(history)
                    replayed.append(run_id)
                report["temporal_replay_passed"] = replayed
                report["upstream_counters"] = {k: v for k, v in state.items() if isinstance(v, int)}
                report["passed"] = True
        except BaseException as exc:
            report["passed"] = False
            report["failure"] = {"type": type(exc).__name__, "message": str(exc)[:4000]}
            # All processes use synthetic credentials; these temporary logs are removed on exit.
            for log in sorted(temp.glob("*.log")):
                print(log.name + "\n" + log.read_text()[-7000:], file=sys.stderr)
            raise
        finally:
            processes.stop()
            cleanup_errors = []
            workflow_ids = [
                prefix + run_id for run_id in run_ids for prefix in ("run:", "extension-cleanup:")
            ] + recovery_ids
            for workflow_id in workflow_ids:
                try:
                    handle = temporal.get_workflow_handle(workflow_id)
                    if (await handle.describe()).status.name == "RUNNING":
                        await handle.terminate(reason="Synthetic benchmark teardown")
                except Exception as exc:
                    from temporalio.service import RPCError, RPCStatusCode

                    if not isinstance(exc, RPCError) or exc.status != RPCStatusCode.NOT_FOUND:
                        cleanup_errors.append({"workflow_id": workflow_id, "error": type(exc).__name__})
            report["cleanup"]["workflow_errors"] = cleanup_errors
            report["cleanup"]["processes_stopped"] = all(p.poll() is not None for p in processes.processes)
            await database.close()
            async with admin.begin() as conn:
                await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
            await admin.dispose()
            report["cleanup"]["isolated_schema_removed"] = True
            report["duration_seconds"] = round(time.monotonic() - started, 2)
            report["finished_at"] = datetime.now(timezone.utc).isoformat()
            report["source_sha256"] = frozen
            report["source_unchanged"] = all(
                hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == digest
                for name, digest in frozen.items()
            )
            if cleanup_errors or not report["source_unchanged"]:
                report["passed"] = False
            (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    assert report["passed"], report.get("cleanup")
    print(
        json.dumps(
            {
                "event": "complete",
                "report": str(output / "report.json"),
                "passed": report["passed"],
                "cases": len(report["cases"]),
            }
        ),
        flush=True,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("var/benchmarks")
        / ("showcases-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")),
    )
    args = parser.parse_args()
    asyncio.run(main(args.output.resolve()))
