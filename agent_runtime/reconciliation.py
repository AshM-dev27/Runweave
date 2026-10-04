"""Audited operator recovery. Network reconciliation runs only in a Temporal activity."""

import asyncio
import copy
import time
from uuid import uuid4

from sqlalchemy import select
from temporalio import activity
from temporalio.exceptions import ApplicationError

from .db import OutboxRow, RunRow
from .extensions import ToolCall
from .general_db import GeneralAttemptRow, GeneralOperationRow
from .project_store import digest, fail

TERMINAL = {"completed", "failed", "cancelled"}


def receipt(op):
    return {
        "id": op.id,
        "target_id": op.data["request"]["target_id"],
        "kind": op.data["request"]["kind"],
        "status": op.data["status"],
        "result": op.data.get("result"),
    }


async def target(db, run_id, request):
    attempt = None
    if request["kind"] == "model_usage":
        attempt = await db.get(GeneralAttemptRow, request["target_id"])
        op = await db.get(GeneralOperationRow, attempt.operation_id) if attempt else None
    else:
        op = await db.get(GeneralOperationRow, request["target_id"])
    if op is None or op.run_id != run_id:
        fail("reconciliation_target_not_found", 404)
    return op, attempt


def eligible(op, attempt, request):
    if op.data.get("lease", 0) > time.time():
        fail("reconciliation_target_busy", 409)
    if request["kind"] == "model_usage":
        if attempt.data.get("settled") or attempt.data.get("outcome") != "dispatch_unknown":
            fail("reconciliation_target_resolved", 409)
    elif not (
        op.data.get("external")
        and op.data.get("started")
        and op.data.get("effect_policy", {}).get("kind") == "external-write"
        and op.data.get("status") == "outcome_unknown"
        and op.data.get("result") is None
    ):
        fail("reconciliation_target_not_unknown_write", 409)


class ReconciliationStore:
    async def recovery(self, run_id, cursor=0, limit=100):
        async with self.database.sessions.begin() as db:
            row, _, root = await self.general_lock(db, run_id, active=False)
            root_row = await db.get(RunRow, root.run_id)
            operations = list(
                await db.scalars(
                    select(GeneralOperationRow)
                    .where(GeneralOperationRow.run_id == run_id)
                    .order_by(GeneralOperationRow.id)
                )
            )
            attempts = list(
                await db.scalars(
                    select(GeneralAttemptRow)
                    .join(GeneralOperationRow)
                    .where(GeneralOperationRow.run_id == run_id)
                    .order_by(GeneralAttemptRow.id)
                )
            )
            unresolved = [
                {"target_id": a.id, "kind": "model_usage", "reserved_tokens": a.data["reserved_tokens"]}
                for a in attempts
                if not a.data.get("settled") and a.data.get("outcome") == "dispatch_unknown"
            ]
            unresolved += [
                {"target_id": o.id, "kind": "external_write"}
                for o in operations
                if o.data.get("external") and o.data.get("status") == "outcome_unknown"
            ]
            unresolved.sort(key=lambda item: item["target_id"])
            return {
                "terminal_tree": row.status in TERMINAL and root_row.status in TERMINAL,
                "unresolved": unresolved[cursor : cursor + limit],
                "next_cursor": cursor + limit if len(unresolved) > cursor + limit else None,
                "reconciliations": [receipt(o) for o in operations if o.data.get("kind") == "reconciliation"],
            }

    async def reconciliation_get(self, run_id, identity):
        async with self.database.sessions() as db:
            op = await db.get(GeneralOperationRow, identity)
            if op is None or op.run_id != run_id or op.data.get("kind") != "reconciliation":
                fail("reconciliation_not_found", 404)
            return receipt(op)

    async def reconciliation_submit(self, run_id, request, key):
        body = request.model_dump()
        identity = run_id + ":reconcile:" + digest(key)
        async with self.database.sessions.begin() as db:
            row, gr, root = await self.general_lock(db, run_id, active=False)
            old = await db.get(GeneralOperationRow, identity)
            if old:
                if old.fingerprint != digest(body):
                    fail("reconciliation_idempotency_conflict", 409)
                return receipt(old)
            root_row = await db.get(RunRow, root.run_id)
            if row.status not in TERMINAL or root_row.status not in TERMINAL:
                fail("reconciliation_requires_terminal_tree", 409)
            op, attempt = await target(db, run_id, body)
            eligible(op, attempt, body)
            if body["kind"] == "external_write":
                alias = op.data["decision"]["action"]["capability"]
                definition = gr.data["tools"][alias]["extension"]
                if definition.get("reconciliation") != "lookup":
                    fail("terminal_recovery_requires_lookup_handler", 409)
            holder = attempt if attempt is not None else op
            prior_id = holder.data.get("reconciliation_id")
            prior = await db.get(GeneralOperationRow, prior_id) if prior_id else None
            if prior and prior.data["status"] != "complete":
                fail("reconciliation_pending", 409)
            count = root.data.get("reconciliation_count", 0)
            if count >= 128:
                fail("operator_reconciliation_capacity", 429)
            root.data = {**root.data, "reconciliation_count": count + 1}
            holder.data = {**holder.data, "reconciliation_id": identity}
            operation = GeneralOperationRow(
                id=identity,
                run_id=run_id,
                fingerprint=digest(body),
                data={
                    "kind": "reconciliation",
                    "sequence": 3000000 + count,
                    "status": "pending",
                    "request": body,
                },
            )
            db.add(operation)
            db.add(
                OutboxRow(
                    id=identity,
                    run_id=run_id,
                    kind="reconciliation",
                    payload={"id": identity, "run_id": run_id},
                )
            )
            await self.emit(
                db,
                row,
                identity + ":requested",
                "reconciliation.requested",
                {"id": identity, "target_id": body["target_id"], "kind": body["kind"]},
            )
            return receipt(operation)


@activity.defn
async def reconcile_operation(data: dict):
    from .resources import settle_model_attempt_locked
    from .runtime import get_store

    store = get_store()
    owner = uuid4().hex
    async with store.database.sessions.begin() as db:
        row, gr, root = await store.general_lock(db, data["run_id"], active=False)
        recovery = await db.get(GeneralOperationRow, data["id"])
        if recovery is None or recovery.run_id != row.id or recovery.data.get("kind") != "reconciliation":
            raise ApplicationError("reconciliation_not_found", non_retryable=True)
        if recovery.data["status"] == "complete":
            return receipt(recovery)
        if recovery.data.get("lease", 0) > time.time():
            raise ApplicationError("reconciliation_lease_pending")
        request = recovery.data["request"]
        op, attempt = await target(db, row.id, request)
        holder = attempt if attempt is not None else op
        if holder.data.get("reconciliation_id") != recovery.id:
            raise ApplicationError("reconciliation_fenced", non_retryable=True)
        if request["kind"] == "model_usage":
            source = "existing_receipt" if attempt.data.get("settled") else "operator_attestation"
            if not attempt.data.get("settled"):
                settle_model_attempt_locked(gr, root, attempt, tokens=request["reported_tokens"])
                attempt.data = {
                    **attempt.data,
                    "outcome": "operator_reported_usage",
                    "evidence_ref": request["evidence_ref"],
                }
            result = {
                "outcome": "resolved",
                "source": source,
                "reported_tokens": attempt.data["reported_tokens"],
                "attempt_refunded": False,
            }
            await finish(store, db, row, recovery, result)
            return receipt(recovery)
        if op.data.get("result") is not None:
            await finish(store, db, row, recovery, {"outcome": "resolved", "source": "existing_receipt"})
            return receipt(recovery)
        recovery.data = {**recovery.data, "status": "pending", "lease": time.time() + 60, "owner": owner}
        alias = op.data["decision"]["action"]["capability"]
        definition = gr.data["tools"][alias]["extension"]
        call = ToolCall(row.id, op.id, copy.deepcopy(op.data["decision"]["action"]["arguments"]), definition)
    result = None
    try:
        async with asyncio.timeout(45):
            handler = store.extensions.handler(definition)
            value = await handler.reconcile(call)
            if value is not None:
                result = store.extensions.result(definition, value)
    except Exception:
        pass  # Exception bodies can contain credentials; ambiguity remains explicit.
    async with store.database.sessions.begin() as db:
        row, _, _ = await store.general_lock(db, data["run_id"], active=False)
        recovery = await db.get(GeneralOperationRow, data["id"])
        if recovery.data.get("owner") != owner:
            raise ApplicationError("reconciliation_fenced", non_retryable=True)
        op, _ = await target(db, row.id, request)
        if result is not None:
            op.data = {
                **op.data,
                "status": "complete",
                "lease": 0,
                "result": {**result, "effect": True, "operation_id": op.id},
            }
            await store.emit(db, row, recovery.id + ":effect", "tool.reconciled", {"operation_id": op.id})
        await finish(
            store,
            db,
            row,
            recovery,
            {"outcome": "resolved" if result is not None else "unknown", "source": "handler_reconciliation"},
        )
        answer = receipt(recovery)
    # Existing dispatcher cleanup also retries this if interrupted after settlement.
    await store.general_cleanup(data["run_id"], allow_complete=False)
    return answer


async def finish(store, db, row, recovery, result):
    recovery.data = {**recovery.data, "status": "complete", "lease": 0, "result": result}
    await store.emit(
        db, row, recovery.id + ":completed", "reconciliation.completed", {"id": recovery.id, **result}
    )
