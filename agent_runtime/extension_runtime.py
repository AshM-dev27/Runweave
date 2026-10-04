"""Durable external-tool intents. I/O occurs outside database transactions, in activities."""

import asyncio
import copy
import time
from uuid import uuid4

from .activity_liveness import LEASE_SECONDS, enabled, leased_call
from .extensions import DeferredToolResult, ToolCall
from .general_db import GeneralAttemptRow, GeneralOperationRow, GeneralRecordRow
from .project_store import fail

TOOL_TIMEOUT_SECONDS = 45


async def execute_extension(store, run_id, operation_id):
    owner = uuid4().hex
    async with store.database.sessions.begin() as db:
        row, gr, root = await store.general_lock(db, run_id, active=False)
        op = await db.get(GeneralOperationRow, operation_id)
        if op is None or not op.data.get("external"):
            fail("extension_intent_missing", 409)
        if op.data.get("result") is not None:
            return op.data["result"]
        alias = op.data["decision"]["action"]["capability"]
        entry = gr.data["tools"][alias]
        definition = entry["extension"]
        handler = store.extensions.handler(definition)
        deferred = definition["handler"] == "browser_use.v4"
        if row.status in {"completed", "cancelled", "failed"}:
            fail("run_not_active", 409)
        if not (deferred and op.data.get("deferred")):
            await store.general_lock(db, run_id)
        if entry["effect"]["approval"] == "required" and (
            row.decisions.get(operation_id) is not True
            or op.data.get("scope") in root.data["denied"]
            or entry["effect"]["domain"] + ":*" in root.data["denied"]
        ):
            fail("effect_denied", 403)
        if op.data.get("lease", 0) > time.time():
            fail("extension_lease_pending", 409)
        recovering = bool(op.data.get("started"))
        attempts = op.data.get("external_attempts", 0)
        polling = deferred and op.data.get("deferred", False)
        if attempts >= 2 and not polling:
            fail("extension_recovery_exhausted", 409)
        if attempts and not polling:
            await store.general_charge_locked(db, gr, root, "tool_attempts")
            db.add(
                GeneralAttemptRow(
                    id=f"{operation_id}:tool:{attempts if polling else attempts + 1}",
                    operation_id=operation_id,
                    ordinal=attempts + 1,
                    data={"kind": "tool", "reserved": True},
                )
            )
        op.data = {
            **op.data,
            "started": True,
            "lease": time.time() + (LEASE_SECONDS if enabled() else 60),
            "owner": owner,
            "external_attempts": attempts if polling else attempts + 1,
            "status": "pending",
        }
        fence = root.data["fence"]
        call = ToolCall(
            run_id,
            operation_id,
            op.data["decision"]["action"]["arguments"],
            definition,
            copy.deepcopy(op.data.get("handler_state", {})),
            max(0, store.general_time_remaining(root.data)),
            state_writer(store, run_id, operation_id, owner) if deferred else None,
        )
    result, unknown = None, False
    try:
        async with asyncio.timeout(TOOL_TIMEOUT_SECONDS):

            async def perform():
                if recovering and definition["effect"]["kind"] == "external-write":
                    return await handler.reconcile(call)
                return await handler.execute(call)

            value = await leased_call(store, run_id, operation_id, owner, perform)
            if isinstance(value, DeferredToolResult):
                async with store.database.sessions.begin() as db:
                    await store.general_lock(db, run_id, active=False)
                    op = await db.get(GeneralOperationRow, operation_id)
                    if op.data.get("owner") != owner:
                        fail("extension_lease_fenced", 409)
                    op.data = {**op.data, "lease": 0, "deferred": True}
                return {"external_pending": True, "retry_after": value.retry_after}
            if value is None:
                unknown = True
            else:
                result = store.extensions.result(definition, value)
                if deferred and value.get("error"):
                    result["error"] = value["error"]
    except Exception:
        # A timeout/invalid response can happen after a remote write commits.
        # Do not leak exception bodies or turn an ambiguous effect into success.
        unknown = definition["effect"]["kind"] == "external-write"
        if not unknown:
            result = {"error": "extension_execution_failed"}
    async with store.database.sessions.begin() as db:
        row, gr, root = await store.general_lock(db, run_id, active=False)
        op = await db.get(GeneralOperationRow, operation_id)
        if op.data.get("owner") != owner:
            fail("extension_lease_fenced", 409)
        attempt = await db.get(
            GeneralAttemptRow, f"{operation_id}:tool:{attempts if polling else attempts + 1}"
        )
        if attempt:
            attempt.data = {
                **attempt.data,
                "settled": not unknown,
                "outcome": "outcome_unknown" if unknown else "failed" if result.get("error") else "complete",
            }
        cancelled = row.status in {"cancelled", "failed"} or root.data["fence"] != fence
        if unknown:
            op.data = {**op.data, "status": "outcome_unknown", "lease": 0, "deferred": False}
            result = {
                "error": "extension_outcome_unknown",
                "operation_id": operation_id,
                "feedback": "Do not repeat this write. Its durable operation needs reconciliation.",
            }
        else:
            if definition["effect"]["kind"] == "external-write":
                result = {**result, "effect": True, "operation_id": operation_id}
            op.data = {**op.data, "status": "complete", "result": result, "lease": 0}
        # An already committed external effect is retained even when cancellation wins.
        # Cancellation forbids subsequent actions; it cannot undo a remote commit.
        if unknown and attempts == 0 and not cancelled:
            return {**result, "reconcile": True}
        if not cancelled and not await db.get(GeneralRecordRow, operation_id + ":checkpoint"):
            await store.general_checkpoint(
                db, row, gr, operation_id, result, progress=not unknown and not result.get("error")
            )
        await store.emit(
            db,
            row,
            operation_id + (":unknown" if unknown else ":settled"),
            "tool.outcome_unknown" if unknown else "tool.completed",
            {"operation_id": operation_id, "registration_id": entry["registration_id"]},
        )
        return result


def state_writer(store, run_id, operation_id, owner):
    async def save(state):
        async with store.database.sessions.begin() as db:
            await store.general_lock(db, run_id, active=False)
            op = await db.get(GeneralOperationRow, operation_id)
            if op.data.get("owner") != owner:
                fail("extension_lease_fenced", 409)
            op.data = {**op.data, "handler_state": copy.deepcopy(state)}

    return save


async def cleanup_extension(store, run_id, operation_id, *, provider_io=False):
    """Classify expired terminal intents without reissuing a cancelled remote call."""
    async with store.database.sessions.begin() as db:
        row, gr, _ = await store.general_lock(db, run_id, active=False)
        op = await db.get(GeneralOperationRow, operation_id)
        if op.data.get("result") is not None:
            return True
        if row.status not in {"completed", "failed", "cancelled"} or op.data.get("lease", 0) > time.time():
            return False
        alias = op.data["decision"]["action"]["capability"]
        definition = gr.data["tools"][alias]["extension"]
        if definition["handler"] == "browser_use.v4" and op.data.get("started"):
            if not provider_io:
                return False
            owner = uuid4().hex
            op.data = {**op.data, "owner": owner, "lease": time.time() + 60}
            call = ToolCall(
                run_id,
                operation_id,
                op.data["decision"]["action"]["arguments"],
                definition,
                state=copy.deepcopy(op.data.get("handler_state", {})),
                save_state=state_writer(store, run_id, operation_id, owner),
            )
        else:
            call = None
        if call is None:
            return await classify_interrupted(store, db, row, op)
    result = None
    try:
        async with asyncio.timeout(TOOL_TIMEOUT_SECONDS):
            value = await store.extensions.handler(definition).cleanup(call)
            if value is not None and not isinstance(value, DeferredToolResult):
                result = store.extensions.result(definition, value)
                if value.get("error"):
                    result["error"] = value["error"]
    except Exception:
        pass
    async with store.database.sessions.begin() as db:
        row, _, _ = await store.general_lock(db, run_id, active=False)
        op = await db.get(GeneralOperationRow, operation_id)
        if op.data.get("owner") != owner:
            return False
        op.data = {**op.data, "lease": 0, "status": "complete" if result else "outcome_unknown"}
        if result is None:
            return False
        op.data = {**op.data, "result": {**result, "effect": True, "operation_id": operation_id}}
        for ordinal in range(1, op.data.get("external_attempts", 0) + 1):
            attempt = await db.get(GeneralAttemptRow, f"{operation_id}:tool:{ordinal}")
            if attempt:
                attempt.data = {
                    **attempt.data,
                    "settled": True,
                    "outcome": "failed" if result.get("error") else "complete",
                }
        await store.emit(
            db, row, operation_id + ":cleanup", "tool.interrupted", {"operation_id": operation_id}
        )
        return True


async def classify_interrupted(store, db, row, op):
    operation_id = op.id
    write = op.data.get("effect_policy", {}).get("kind") != "read"
    unknown = bool(op.data.get("started")) and write
    op.data = {
        **op.data,
        "lease": 0,
        "status": "outcome_unknown" if unknown else "complete",
        **(
            {}
            if unknown
            else {
                "result": {
                    "error": "extension_interrupted" if op.data.get("started") else "extension_not_started"
                }
            }
        ),
    }
    await store.emit(
        db,
        row,
        operation_id + ":cleanup",
        "tool.outcome_unknown" if unknown else "tool.interrupted",
        {"operation_id": operation_id, "registration_id": op.data.get("registration_id")},
    )
    return not unknown
