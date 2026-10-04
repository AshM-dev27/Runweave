"""Real PostgreSQL/Temporal recovery, batching, races and replay."""

import asyncio

import pytest
from pydantic_ai.durable_exec.temporal import PydanticAIPlugin
from temporalio.worker import Replayer
from test_general_integration import backend
from test_general_semantic import complete, http_client, stub, submit
from test_hardening import DeferredWriter, unknown_model, unknown_write
from test_harness_integration import replay

from agent_runtime.general_workflow import ReconciliationWorkflow
from agent_runtime.reconciliation import reconcile_operation

pytestmark = pytest.mark.integration


async def test_durable_recovery_delivery_and_replay(pg_store, monkeypatch):
    # Create the ambiguous request before worker startup, then recover through the outbox.
    async with http_client(pg_store) as client:
        run, target = await unknown_model(pg_store, monkeypatch, client)
        await client.cancel(run.id)
        request = {
            "kind": "model_usage",
            "target_id": target["target_id"],
            "reported_tokens": 123,
            "evidence_ref": "provider/receipt-123",
        }
        accepted = await client.reconcile(run.id, request, idempotency_key="recovery")
    async with backend(pg_store, monkeypatch) as (client, temporal):
        async with asyncio.timeout(20):
            while (await client.reconciliation(run.id, accepted["id"]))["status"] != "complete":
                await asyncio.sleep(0.05)
        handle = temporal.get_workflow_handle(accepted["id"])
        await handle.result()
        await Replayer(workflows=[ReconciliationWorkflow], plugins=[PydanticAIPlugin()]).replay_workflow(
            await handle.fetch_history()
        )
        assert (await client.resources(run.id))["usage"]["reported_tokens"] == 123
        assert (await client.get(run.id)).status == "cancelled"


async def test_concurrent_recovery_lease_prevents_duplicate_lookups(pg_store):
    started, release = asyncio.Event(), asyncio.Event()

    class Slow(DeferredWriter):
        async def reconcile(self, call):
            if self.available:
                started.set()
                await release.wait()
            return await super().reconcile(call)

    handler = Slow()
    async with http_client(pg_store) as client:
        run = await unknown_write(pg_store, client, handler)
        accepted = await client.reconcile(run.id, (await client.recovery(run.id))["unresolved"][0])
        handler.available = True
        payload = {"run_id": run.id, "id": accepted["id"]}
        before = len(handler.reconciliations)
        task = asyncio.create_task(reconcile_operation(payload))
        try:
            await asyncio.wait_for(started.wait(), 5)
            items = (await client.operations(run.id))["items"]
            assert next(o for o in items if o["id"] == accepted["id"])["status"] == "pending"
            from temporalio.exceptions import ApplicationError

            with pytest.raises(ApplicationError, match="reconciliation_lease_pending"):
                await reconcile_operation(payload)
        finally:
            release.set()
            result = await task
        assert result["result"]["outcome"] == "resolved"
        assert len(handler.reconciliations) == before + 1 and len(handler.calls) == 1


async def test_batch_verification_workflow_replay(pg_store, monkeypatch):
    calls = stub(monkeypatch, [{"kind": "verify", "check": "all"}, complete()])
    async with backend(pg_store, monkeypatch) as (client, temporal):
        workspace = await client.workspace_create({"a.txt": b"a", "b.txt": b"b"})
        task = {
            "outcome": "Verify files",
            "criteria": [
                {
                    "id": "files",
                    "statement": "Exact bytes",
                    "evidence_policy": "check",
                    "checks": [
                        {"id": "a", "kind": "bytes", "path": "a.txt", "expected": "YQ=="},
                        {"id": "b", "kind": "bytes", "path": "b.txt", "expected": "Yg=="},
                    ],
                }
            ],
        }
        run = await submit(client, ["workspace_verify"], workspace, task)
        done = await client.wait(run.id, timeout=30)
        assert done.status == "completed", done.error
        assert len(calls) == 2
        assert len((await client.verifications(run.id))["items"]) == 2
        await replay(temporal, run.id)
