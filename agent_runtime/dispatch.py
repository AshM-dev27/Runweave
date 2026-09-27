"""Post-commit delivery and reconciliation; safe across process death at every await."""

import asyncio
from datetime import timedelta

from sqlalchemy import select
from temporalio.client import WorkflowExecutionStatus
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.service import RPCError, RPCStatusCode

from .db import OutboxRow, RunRow, ToolkitRunRow
from .store import TERMINAL


class Dispatcher:
    def __init__(self, store, client, task_queue):
        self.store, self.client, self.task_queue = store, client, task_queue

    async def once(self):
        async with self.store.database.sessions() as db:
            ids = list(
                await db.scalars(select(OutboxRow.id).where(OutboxRow.delivered.is_(False)).limit(100))
            )
        for item_id in ids:
            async with self.store.database.sessions.begin() as db:
                item = await db.scalar(
                    select(OutboxRow).where(OutboxRow.id == item_id).with_for_update(skip_locked=True)
                )
                if item is None or item.delivered:
                    continue
                handle = self.client.get_workflow_handle(f"run:{item.run_id}")
                if item.kind == "start":
                    run = await db.get(RunRow, item.run_id)
                    feature = await db.get(ToolkitRunRow, run.id)
                    version = feature.state.get("execution_version", 1) if feature else 1
                    if version not in {1, 2, 3}:
                        raise ValueError("Unknown execution version")
                    try:
                        await self.client.start_workflow(
                            {1: "RunWorkflow", 2: "ToolkitWorkflow", 3: "GeneralWorkflow"}[version],
                            item.run_id,
                            id=f"run:{item.run_id}",
                            task_queue=self.task_queue + "-v3" if version == 3 else self.task_queue,
                            execution_timeout=timedelta(
                                seconds=(
                                    (await self.store.general(run.id))
                                    .get("resource_state", {})
                                    .get("ceilings", {})
                                    .get("active_seconds", run.config["general"]["limits"]["active_seconds"])
                                    + (
                                        (run.config["general"].get("resources") or {}).get(
                                            "max_pause_seconds", 0
                                        )
                                    )
                                    if version == 3
                                    else run.config["timeout_seconds"]
                                )
                                + run.approval_wait_seconds
                                + 180
                            ),
                            id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                        )
                    except WorkflowAlreadyStartedError:
                        pass
                else:
                    try:
                        if item.kind == "cancel":
                            await handle.cancel()
                        else:
                            await handle.signal(
                                "resources" if item.kind == "resources" else "decision", item.payload
                            )
                    except RPCError as exc:
                        if exc.status != RPCStatusCode.NOT_FOUND:
                            raise
                        # Start may not yet have been delivered. Do not lose a command.
                        start = await db.get(OutboxRow, f"start:{item.run_id}")
                        if start is None or not start.delivered:
                            continue
                        status = await db.scalar(select(RunRow.status).where(RunRow.id == item.run_id))
                        if status not in TERMINAL:
                            continue
                item.delivered = True
        await self.reconcile()
        await self.reconcile_cleanup()
        await self.reconcile_general_cleanup()

    async def reconcile(self):
        # Covers workflow execution timeouts/failed workflow tasks and crashes during finalization.
        async with self.store.database.sessions() as db:
            ids = list(await db.scalars(select(RunRow.id).where(RunRow.status.not_in(TERMINAL))))
        for run_id in ids:
            try:
                desc = await self.client.get_workflow_handle(f"run:{run_id}").describe()
            except RPCError as exc:
                if exc.status == RPCStatusCode.NOT_FOUND:
                    continue
                raise
            if desc.status in {
                WorkflowExecutionStatus.FAILED,
                WorkflowExecutionStatus.TIMED_OUT,
                WorkflowExecutionStatus.TERMINATED,
                WorkflowExecutionStatus.CANCELED,
                WorkflowExecutionStatus.COMPLETED,
            }:
                status = "cancelled" if desc.status == WorkflowExecutionStatus.CANCELED else "failed"
                await self.store.finish(run_id, status, error="workflow_closed_without_result")

    async def reconcile_cleanup(self):
        from .sandbox import job_complete

        async with self.store.database.sessions() as db:
            features = list(await db.scalars(select(ToolkitRunRow).where(ToolkitRunRow.parent_id.is_(None))))
        for feature in features:
            if feature.state.get("cleanup_state") != "pending":
                continue
            ready = True
            for child in feature.state.get("children", {}).values():
                try:
                    desc = await self.client.get_workflow_handle("run:" + child["id"]).describe()
                    if desc.status == WorkflowExecutionStatus.RUNNING:
                        ready = False
                except RPCError:
                    ready = False
            for job, deadline in feature.state.get("sandbox_jobs", {}).items():
                if not await job_complete(job, deadline):
                    ready = False
            if ready:
                await self.store.cleanup_complete(feature.run_id, feature.state)

    async def reconcile_general_cleanup(self):
        from .general_db import GeneralRunRow

        async with self.store.database.sessions() as db:
            records = list(await db.scalars(select(GeneralRunRow)))
        for gr in records:
            if gr.data.get("cleanup_state") != "pending":
                continue
            children_done = True
            for cid in gr.data.get("children", {}):
                try:
                    desc = await self.client.get_workflow_handle("run:" + cid).describe()
                    child = await self.store.general(cid)
                    if desc.status == WorkflowExecutionStatus.RUNNING or child["cleanup_state"] != "complete":
                        children_done = False
                except RPCError:
                    children_done = False
            try:
                await self.store.general_cleanup(gr.run_id, allow_complete=children_done)
            except Exception:
                pass

    async def run(self):
        while True:
            try:
                await self.once()
            except Exception:
                # Leave delivery pending. No exception bodies (which can contain input) in logs.
                import logging

                logging.getLogger(__name__).warning("Dispatch unavailable; will retry")
            await asyncio.sleep(0.5)
