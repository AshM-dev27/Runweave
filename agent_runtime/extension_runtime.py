"""Durable external-tool intents. I/O occurs outside database transactions, in activities."""

import asyncio
import copy
import json
import math
import time
from uuid import uuid4

from .activity_liveness import LEASE_SECONDS, enabled, leased_call
from .artifact_store import OperationArtifacts
from .computer_store import OperationComputers
from .extensions import DeferredToolResult, ToolCall, capacity_handler, deferred_handler
from .general_db import ExtensionSlotRow, GeneralAttemptRow, GeneralOperationRow, GeneralRecordRow
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
        capacity = capacity_handler(definition["handler"])
        handler = store.extensions.handler(definition)
        deferred = deferred_handler(definition)
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
            if definition["handler"] == "e2b.session.python.v1":
                # Replacement workers wait durably for the previous holder's lease,
                # rather than exhausting an activity retry before it expires.
                return {
                    "external_pending": True,
                    "retry_after": max(2, min(30, math.ceil(op.data["lease"] - time.time()))),
                }
            fail("extension_lease_pending", 409)
        limits = [
            entry["max_concurrency"]
            for entry in [definition, *store.extensions.tools.values()]
            if capacity_handler(entry["handler"]) == capacity and entry.get("max_concurrency") is not None
        ]
        limit = min(limits) if limits else None
        slot = await db.get(ExtensionSlotRow, operation_id)
        if limit is not None and slot is None and definition["handler"] != "e2b.session.python.v1":
            from sqlalchemy import func, select

            used = await db.scalar(
                select(func.count())
                .select_from(ExtensionSlotRow)
                .where(ExtensionSlotRow.handler == capacity, ExtensionSlotRow.active.is_(True))
            )
            if used >= limit:
                # No provider intent exists yet; admission/time checks still run on each poll.
                return {"external_pending": True, "retry_after": 5}
            db.add(ExtensionSlotRow(operation_id=operation_id, handler=capacity))
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
            state_writer(store, run_id, operation_id, owner, definition, fence=fence) if deferred else None,
            OperationArtifacts(store, run_id, operation_id, owner, fence),
            OperationComputers(store, run_id, operation_id, owner, fence),
        )
    result, unknown = None, False
    try:
        async with asyncio.timeout(TOOL_TIMEOUT_SECONDS):

            async def perform():
                if recovering and definition["effect"]["retry_safety"] == "reconcile":
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
                if (deferred or definition["handler"] == "artifacts.read.v1") and value.get("error"):
                    result["error"] = value["error"]
    except Exception:
        # A timeout/invalid response can happen after a remote write commits.
        # Do not leak exception bodies or turn an ambiguous effect into success.
        unknown = definition["effect"]["kind"] != "read"
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
                "feedback": "Do not repeat this operation. Its durable identity needs reconciliation.",
            }
        else:
            if definition["effect"]["kind"] != "read":
                result = {**result, "effect": True, "operation_id": operation_id}
            op.data = {**op.data, "status": "complete", "result": result, "lease": 0}
            await release_slot(db, operation_id)
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


def state_writer(store, run_id, operation_id, owner, definition, *, fence=None):
    async def save(state):
        state = store.extensions.redact(definition, state)
        if len(json.dumps(state, ensure_ascii=False).encode()) > 65536:
            fail("extension_state_limit", 413)
        async with store.database.sessions.begin() as db:
            row, _, root = await store.general_lock(db, run_id, active=False)
            op = await db.get(GeneralOperationRow, operation_id)
            if op.data.get("owner") != owner:
                fail("extension_lease_fenced", 409)
            if fence is not None and (
                root.data["fence"] != fence or row.status in {"completed", "cancelled", "failed"}
            ):
                fail("extension_lease_fenced", 409)
            if (
                definition["handler"] == "e2b.session.python.v1"
                and op.data.get("handler_state", {}).get("phase") == "finished"
            ):
                # Session release commits the finished receipt atomically. A stale
                # transient-error handler must never regress it after response loss.
                return
            if definition["handler"] == "e2b.session.python.v1" and state.get("phase") == "executing":
                from .computer_db import ComputerSessionRow
                from .computer_store import epoch

                computer = await db.get(ComputerSessionRow, state["computer_id"])
                if (
                    computer is None
                    or computer.status != "busy"
                    or computer.data.get("holder") != operation_id
                    or epoch(computer.expires_at) <= time.time()
                ):
                    fail("computer_session_unavailable", 409)
            op.data = {**op.data, "handler_state": copy.deepcopy(state)}

    return save


async def release_slot(db, operation_id):
    from sqlalchemy import select

    from .computer_db import ComputerSessionRow

    retained = await db.scalar(
        select(ComputerSessionRow.id).where(
            ComputerSessionRow.capacity_operation_id == operation_id, ComputerSessionRow.status != "closed"
        )
    )
    if retained:
        return
    slot = await db.get(ExtensionSlotRow, operation_id)
    if slot is not None:
        slot.active = False


async def settle_tool_attempts(db, op, result):
    for ordinal in range(1, op.data.get("external_attempts", 0) + 1):
        attempt = await db.get(GeneralAttemptRow, f"{op.id}:tool:{ordinal}")
        if attempt:
            attempt.data = {
                **attempt.data,
                "settled": True,
                "outcome": "failed" if result.get("error") else "complete",
            }


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
        if deferred_handler(definition) and op.data.get("started"):
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
                save_state=state_writer(store, run_id, operation_id, owner, definition),
                computers=OperationComputers(store, run_id, operation_id, owner, cleanup=True),
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
        await release_slot(db, operation_id)
        await settle_tool_attempts(db, op, result)
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
