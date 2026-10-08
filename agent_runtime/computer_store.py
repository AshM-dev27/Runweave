"""Session ownership and exclusive use, serialized with workspace admission."""

import copy
import hashlib
import time
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import func, or_, select

from .computer_contracts import describe
from .computer_db import ComputerSessionRow
from .db import GateRow, RunRow, SessionRow, now
from .general_db import ExtensionSlotRow, GeneralOperationRow
from .project_store import fail


def epoch(value):
    return value.replace(tzinfo=timezone.utc).timestamp()


async def gate(db):
    await db.scalar(select(GateRow).where(GateRow.id == 1).with_for_update())


class ComputerStore:
    async def computer_sessions(self, session_id, *, cursor="", limit=100):
        async with self.database.sessions() as db:
            if await db.get(SessionRow, session_id) is None:
                fail("Session not found", 404)
            rows = await db.scalars(
                select(ComputerSessionRow)
                .where(ComputerSessionRow.session_id == session_id, ComputerSessionRow.id > cursor)
                .order_by(ComputerSessionRow.id)
                .limit(limit)
            )
            return [describe(row) for row in rows]

    async def computer(self, computer_id):
        async with self.database.sessions() as db:
            row = await db.get(ComputerSessionRow, computer_id)
            if row is None:
                fail("Computer not found", 404)
            return describe(row)

    async def computer_close(self, computer_id):
        async with self.database.sessions.begin() as db:
            await gate(db)
            row = await db.get(ComputerSessionRow, computer_id)
            if row is None:
                fail("Computer not found", 404)
            if row.status != "closed":
                row.status = "closing"
                row.data = {**row.data, "close_requested": True}
                run = await db.get(RunRow, row.created_by_run_id)
                await self.emit(
                    db,
                    run,
                    "computer:" + row.id + ":close",
                    "computer.close_requested",
                    {"computer_id": row.id},
                )
            return describe(row)

    async def computer_context_locked(self, db, run_id):
        run = await db.get(RunRow, run_id)
        rows = await db.scalars(
            select(ComputerSessionRow)
            .where(ComputerSessionRow.session_id == run.session_id, ComputerSessionRow.status != "closed")
            .order_by(ComputerSessionRow.created_at.desc())
            .limit(8)
        )
        return [describe(row).model_dump(mode="json") for row in rows]

    async def computer_attest_cleanup(self, computer_id, evidence_ref, key):
        async with self.database.sessions.begin() as db:
            await gate(db)
            row = await db.get(ComputerSessionRow, computer_id)
            if row is None:
                fail("Computer not found", 404)
            attestation = {"evidence_ref": evidence_ref, "key": key}
            if row.status == "closed":
                if row.data.get("cleanup_attestation") != attestation:
                    fail("computer_attestation_conflict", 409)
                return describe(row)
            if row.status not in {"unknown", "closing"} or row.data.get("cleanup_lease", 0) > time.time():
                fail("computer_cleanup_not_unknown", 409)
            holder = await db.get(GeneralOperationRow, row.data["holder"]) if row.data.get("holder") else None
            if holder and holder.data.get("lease", 0) > time.time():
                fail("computer_busy", 409)
            row.status = "closed"
            row.data = {
                **row.data,
                "holder": None,
                "cleanup_attestation": attestation,
                "provider_cleanup_confirmed": False,
            }
            slot = await db.get(ExtensionSlotRow, row.capacity_operation_id)
            slot.active = False
            run = await db.get(RunRow, row.created_by_run_id)
            await self.emit(
                db,
                run,
                "computer:" + row.id + ":attested",
                "computer.cleanup_attested",
                {"computer_id": row.id, "provider_cleanup_confirmed": False},
            )
            return describe(row)


class OperationComputers:
    def __init__(self, store, run_id, operation_id, owner, fence=None, *, cleanup=False):
        self.store, self.run_id, self.operation_id = store, run_id, operation_id
        self.owner, self.fence, self.cleanup = owner, fence, cleanup

    async def locked(self, db):
        run, gr, root = await self.store.general_lock(db, self.run_id, active=False)
        op = await db.get(GeneralOperationRow, self.operation_id)
        if op is None or op.data.get("owner") != self.owner:
            fail("computer_operation_fenced", 409)
        if not self.cleanup and (
            run.status in {"completed", "cancelled", "failed"} or root.data["fence"] != self.fence
        ):
            fail("computer_operation_fenced", 409)
        return run, gr, op

    async def claim(self, name, definition, config, *, provider):
        async with self.store.database.sessions.begin() as db:
            run, gr, _ = await self.locked(db)
            if gr.parent_id is not None:
                return {"error": "computer_delegation_not_supported"}
            row = await db.scalar(
                select(ComputerSessionRow).where(
                    ComputerSessionRow.session_id == run.session_id, ComputerSessionRow.name == name
                )
            )
            if row:
                if (
                    row.status in {"closing", "closed", "unknown"}
                    or epoch(row.expires_at) <= time.time()
                    or epoch(row.idle_expires_at) <= time.time()
                ):
                    return {"error": "computer_session_unavailable"}
                if row.data["definition"]["registration_id"] != definition["registration_id"]:
                    return {"error": "computer_policy_changed"}
                holder = row.data.get("holder")
                if holder and holder != self.operation_id:
                    return {"wait": True}
                if holder != self.operation_id and row.data["operation_count"] >= config.max_operations:
                    return {"error": "computer_operation_limit"}
                row.data = {
                    **row.data,
                    "holder": self.operation_id,
                    "operation_count": row.data["operation_count"] + int(holder != self.operation_id),
                }
                if row.status == "ready":
                    row.status = "busy"
                row.idle_expires_at = row.expires_at
                return self.snapshot(row)
            count = await db.scalar(
                select(func.count())
                .select_from(ComputerSessionRow)
                .where(ComputerSessionRow.session_id == run.session_id, ComputerSessionRow.status != "closed")
            )
            if count >= config.max_sessions:
                return {"error": "computer_session_limit"}
            from .extensions import capacity_handler

            capacity = capacity_handler(definition["handler"])
            limits = [
                d["max_concurrency"]
                for d in [definition, *self.store.extensions.tools.values()]
                if capacity_handler(d["handler"]) == capacity and d.get("max_concurrency") is not None
            ]
            used = await db.scalar(
                select(func.count())
                .select_from(ExtensionSlotRow)
                .where(ExtensionSlotRow.handler == capacity, ExtensionSlotRow.active.is_(True))
            )
            if used >= min(limits):
                return {"wait": True}
            timestamp = now()
            row = ComputerSessionRow(
                id=str(uuid4()),
                session_id=run.session_id,
                name=name,
                provider=provider,
                created_by_run_id=run.id,
                capacity_operation_id=self.operation_id,
                status="creating",
                created_at=timestamp,
                expires_at=timestamp + timedelta(seconds=config.session_seconds),
                idle_expires_at=timestamp + timedelta(seconds=config.session_seconds),
                data={
                    "definition": copy.deepcopy(definition),
                    "holder": self.operation_id,
                    "operation_count": 1,
                    "creation_dispatched": False,
                    "sandbox_id": None,
                    "operation_digest": hashlib.sha256(self.operation_id.encode()).hexdigest(),
                },
            )
            db.add(row)
            db.add(ExtensionSlotRow(operation_id=self.operation_id, handler=capacity, active=True))
            await self.store.emit(
                db,
                run,
                "computer:" + row.id + ":created",
                "computer.created",
                {"computer_id": row.id, "provider": row.provider},
            )
            return self.snapshot(row)

    @staticmethod
    def snapshot(row):
        return {
            **copy.deepcopy(row.data),
            "id": row.id,
            "status": row.status,
            "expires_at": epoch(row.expires_at),
        }

    async def access(self, db, computer_id, *, receipt=False):
        run, _, _ = await self.locked(db)
        row = await db.get(ComputerSessionRow, computer_id)
        if row is None or row.session_id != run.session_id or row.data.get("holder") != self.operation_id:
            fail("computer_not_authorized", 403)
        if not receipt and row.status not in {"creating", "busy"}:
            fail("computer_session_unavailable", 409)
        return row

    async def begin_create(self, computer_id):
        async with self.store.database.sessions.begin() as db:
            row = await self.access(db, computer_id)
            if row.data["creation_dispatched"]:
                return False
            row.data = {**row.data, "creation_dispatched": True}
            return True

    async def acquired(self, computer_id, sandbox_id):
        # A returned handle must survive cancellation so cleanup can find it.
        async with self.store.database.sessions.begin() as db:
            await gate(db)
            op = await db.get(GeneralOperationRow, self.operation_id)
            row = await db.get(ComputerSessionRow, computer_id)
            if (
                row is None
                or row.capacity_operation_id != self.operation_id
                or op.data.get("owner") != self.owner
            ):
                fail("computer_operation_fenced", 409)
            row.data = {**row.data, "sandbox_id": sandbox_id}
            if row.status == "creating":
                row.status = "busy"

    async def current(self, computer_id):
        async with self.store.database.sessions.begin() as db:
            await self.locked(db)
            row = await db.get(ComputerSessionRow, computer_id)
            run = await db.get(RunRow, self.run_id)
            if row is None or row.session_id != run.session_id:
                fail("computer_not_authorized", 403)
            if (
                row.status not in {"closed", "closing", "unknown"}
                and row.data.get("holder") != self.operation_id
            ):
                fail("computer_operation_fenced", 409)
            return self.snapshot(row)

    async def ready(self, computer_id, idle_seconds, state):
        async with self.store.database.sessions.begin() as db:
            row = await self.access(db, computer_id)
            if epoch(row.expires_at) <= time.time():
                fail("computer_session_expired", 409)
            row.status = "ready"
            row.idle_expires_at = datetime.fromtimestamp(
                min(epoch(row.expires_at), time.time() + idle_seconds), timezone.utc
            )
            row.data = {**row.data, "holder": None}
            public = describe(row).model_dump(mode="json")
            op = await db.get(GeneralOperationRow, self.operation_id)
            state = self.store.extensions.redact(row.data["definition"], state)
            # Releasing use and recording completion are atomic. A lost response
            # cannot make an old operation steal a later operation's ownership.
            op.data = {
                **op.data,
                "handler_state": {
                    **copy.deepcopy(state),
                    "computer": public,
                    "phase": "finished",
                    "session_retained": True,
                },
            }
            return public

    async def owned(self, name):
        async with self.store.database.sessions.begin() as db:
            run, _, _ = await self.locked(db)
            row = await db.scalar(
                select(ComputerSessionRow).where(
                    ComputerSessionRow.session_id == run.session_id,
                    ComputerSessionRow.name == name,
                    or_(
                        ComputerSessionRow.capacity_operation_id == self.operation_id,
                        ComputerSessionRow.data["holder"].as_string() == self.operation_id,
                    ),
                )
            )
            return self.snapshot(row) if row is not None else None

    async def reject_create(self, computer_id):
        async with self.store.database.sessions.begin() as db:
            await gate(db)
            row = await db.get(ComputerSessionRow, computer_id)
            op = await db.get(GeneralOperationRow, self.operation_id)
            if row.capacity_operation_id != self.operation_id or op.data.get("owner") != self.owner:
                fail("computer_operation_fenced", 409)
            row.data = {**row.data, "creation_dispatched": False, "holder": None}
            row.status = "closed"
            slot = await db.get(ExtensionSlotRow, self.operation_id)
            slot.active = False

    async def close(self, computer_id):
        from .computer_runtime import close_computer

        # Even fenced/cancelled operations may clean up their own machine, never another holder's.
        async with self.store.database.sessions.begin() as db:
            run, _, _ = await self.locked(db)
            row = await db.get(ComputerSessionRow, computer_id)
            if (
                row is None
                or row.session_id != run.session_id
                or row.data.get("holder") not in {None, self.operation_id}
            ):
                fail("computer_not_authorized", 403)
            if row.status != "closed":
                row.status = "closing"
                row.data = {**row.data, "close_requested": True}
        return await close_computer(self.store, computer_id, operation_id=self.operation_id)
