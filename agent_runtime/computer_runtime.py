"""Cleanup runs in activities; provider I/O never occurs in workflow or API code."""

import os
import time
from uuid import uuid4

from temporalio import activity

from .computer_db import ComputerSessionRow
from .computer_store import gate
from .db import RunRow
from .general_db import ExtensionSlotRow, GeneralOperationRow


class E2BComputerBackend:
    async def close(self, state):
        from .e2b import sandbox_class

        definition = state["definition"]
        if not state.get("creation_dispatched"):
            return True  # Persisted admission but no create dispatch.
        opts = {
            "api_key": os.environ[definition["config"].get("credential_env", "E2B_API_KEY")],
            "request_timeout": 10,
        }
        identity = state.get("sandbox_id")
        if not identity:
            paginator = sandbox_class().list(
                query={"metadata": {"runweave_operation": state["operation_digest"]}}, limit=2, **opts
            )
            matches = await paginator.next_items()
            if len(matches) != 1 or paginator.has_next:
                return False  # Absence/ambiguity is never proof of termination.
            identity = matches[0].sandbox_id
        try:
            await sandbox_class().kill(identity, **opts)
        except Exception as exc:
            from e2b import NotFoundException

            if not isinstance(exc, NotFoundException):
                raise
        return True


def computer_backend(provider):
    # Private adapter boundary. Adding a provider must define matching lifecycle/recovery semantics.
    if provider == "e2b":
        return E2BComputerBackend()
    raise ValueError("computer_provider_unavailable")


async def close_computer(store, computer_id, *, operation_id=None):
    owner = uuid4().hex
    async with store.database.sessions.begin() as db:
        await gate(db)
        row = await db.get(ComputerSessionRow, computer_id)
        if row is None or row.status == "closed":
            return True
        if row.data.get("cleanup_lease", 0) > time.time():
            return False
        holder = row.data.get("holder")
        if holder and holder != operation_id:
            op = await db.get(GeneralOperationRow, holder)
            if op and op.data.get("lease", 0) > time.time():
                return False
        row.status = "closing"
        row.data = {
            **row.data,
            "cleanup_owner": owner,
            "cleanup_lease": time.time() + 60,
            "close_requested": True,
        }
        state, provider = dict(row.data), row.provider
    complete = False
    try:
        complete = await computer_backend(provider).close(state)
    except Exception:
        pass  # Never persist provider exception text or credentials.
    async with store.database.sessions.begin() as db:
        await gate(db)
        row = await db.get(ComputerSessionRow, computer_id)
        if row.data.get("cleanup_owner") != owner:
            return False
        row.data = {**row.data, "cleanup_lease": 0}
        if not complete:
            row.status = "unknown"
            return False
        row.status = "closed"
        row.data = {**row.data, "holder": None}
        slot = await db.get(ExtensionSlotRow, row.capacity_operation_id)
        if slot:
            slot.active = False
        run = await db.get(RunRow, row.created_by_run_id)
        await store.emit(
            db, run, "computer:" + row.id + ":closed", "computer.closed", {"computer_id": row.id}
        )
        return True


@activity.defn
async def cleanup_computer(computer_id: str):
    from .runtime import get_store

    return await close_computer(get_store(), computer_id)
