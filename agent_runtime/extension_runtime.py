"""Durable external-tool intents. I/O occurs outside database transactions, in activities."""

import asyncio
import time
from uuid import uuid4

from .extensions import ToolCall
from .general_db import GeneralAttemptRow, GeneralOperationRow, GeneralRecordRow
from .project_store import fail

TOOL_TIMEOUT_SECONDS = 45


async def execute_extension(store, run_id, operation_id):
    owner = uuid4().hex
    async with store.database.sessions.begin() as db:
        row, gr, root = await store.general_lock(db, run_id)
        op = await db.get(GeneralOperationRow, operation_id)
        if op is None or not op.data.get("external"):
            fail("extension_intent_missing", 409)
        if op.data.get("result") is not None:
            return op.data["result"]
        alias = op.data["decision"]["action"]["capability"]
        entry = gr.data["tools"][alias]
        definition = entry["extension"]
        handler = store.extensions.handler(definition)
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
        if attempts >= 2:
            fail("extension_recovery_exhausted", 409)
        if attempts:
            await store.general_charge_locked(db, gr, root, "tool_attempts")
            db.add(
                GeneralAttemptRow(
                    id=f"{operation_id}:tool:{attempts + 1}",
                    operation_id=operation_id,
                    ordinal=attempts + 1,
                    data={"kind": "tool", "reserved": True},
                )
            )
        op.data = {
            **op.data,
            "started": True,
            "lease": time.time() + 60,
            "owner": owner,
            "external_attempts": attempts + 1,
            "status": "pending",
        }
        fence = root.data["fence"]
        call = ToolCall(run_id, operation_id, op.data["decision"]["action"]["arguments"], definition)
    result, unknown = None, False
    try:
        async with asyncio.timeout(TOOL_TIMEOUT_SECONDS):
            if recovering and definition["effect"]["kind"] == "external-write":
                value = await handler.reconcile(call)
                if value is None:
                    unknown = True
                else:
                    result = store.extensions.result(definition, value)
            else:
                result = store.extensions.result(definition, await handler.execute(call))
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
        attempt = await db.get(GeneralAttemptRow, f"{operation_id}:tool:{attempts + 1}")
        if attempt:
            attempt.data = {
                **attempt.data,
                "settled": not unknown,
                "outcome": "outcome_unknown" if unknown else "failed" if result.get("error") else "complete",
            }
        cancelled = row.status in {"cancelled", "failed"} or root.data["fence"] != fence
        if unknown:
            op.data = {**op.data, "status": "outcome_unknown", "lease": 0}
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


async def cleanup_extension(store, run_id, operation_id):
    """Classify expired terminal intents without reissuing a cancelled remote call."""
    async with store.database.sessions.begin() as db:
        row, _, _ = await store.general_lock(db, run_id, active=False)
        op = await db.get(GeneralOperationRow, operation_id)
        if op.data.get("result") is not None:
            return True
        if row.status not in {"completed", "failed", "cancelled"} or op.data.get("lease", 0) > time.time():
            return False
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
                        "error": "extension_interrupted"
                        if op.data.get("started")
                        else "extension_not_started"
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
