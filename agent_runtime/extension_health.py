"""Authenticated operational counts; no provider I/O or customer content."""

from datetime import datetime, timezone

from sqlalchemy import func, or_, select

from .computer_db import ComputerSessionRow
from .db import RunRow
from .extensions import capacity_handler
from .general_db import ExtensionSlotRow, GeneralOperationRow


async def extension_status(store):
    async with store.database.sessions() as db:
        computers = list(
            await db.scalars(select(ComputerSessionRow).where(ComputerSessionRow.status != "closed"))
        )
        healthy = [r.capacity_operation_id for r in computers if r.status == "ready"]
        unknown_computers = [r.capacity_operation_id for r in computers if r.status == "unknown"]
        holders = {
            r.data.get("holder"): r.capacity_operation_id
            for r in computers
            if r.status in {"busy", "creating"}
        }
        if holders:
            active_holders = await db.scalars(
                select(GeneralOperationRow.id)
                .join(RunRow, RunRow.id == GeneralOperationRow.run_id)
                .where(
                    GeneralOperationRow.id.in_(holders),
                    RunRow.status.not_in(["completed", "failed", "cancelled"]),
                )
            )
            healthy.extend(holders[id] for id in active_holders)
        rows = (
            await db.execute(
                select(
                    ExtensionSlotRow.handler,
                    func.count(),
                    func.min(ExtensionSlotRow.created_at),
                    func.count().filter(
                        RunRow.status.in_(["completed", "failed", "cancelled"]),
                        ExtensionSlotRow.operation_id.not_in(healthy),
                    ),
                    func.count().filter(
                        or_(
                            GeneralOperationRow.data["status"].as_string() == "outcome_unknown",
                            ExtensionSlotRow.operation_id.in_(unknown_computers),
                        )
                    ),
                )
                .join(GeneralOperationRow, GeneralOperationRow.id == ExtensionSlotRow.operation_id)
                .join(RunRow, RunRow.id == GeneralOperationRow.run_id)
                .where(ExtensionSlotRow.active.is_(True))
                .group_by(ExtensionSlotRow.handler)
            )
        ).all()
    active = {row[0]: row[1:] for row in rows}
    limits = {}
    for definition in store.extensions.tools.values():
        if definition.get("max_concurrency") is not None:
            handler = capacity_handler(definition["handler"])
            limits[handler] = min(limits.get(handler, 64), definition["max_concurrency"])
    items = []
    for handler in sorted(set(limits) | set(active)):
        count, oldest, cleanup, unknown = active.get(handler, (0, None, 0, 0))
        age = (
            max(0, (datetime.now(timezone.utc) - oldest.replace(tzinfo=timezone.utc)).total_seconds())
            if oldest
            else 0
        )
        items.append(
            {
                "handler": handler,
                "limit": limits.get(handler),
                "active": count,
                "pending_cleanup": cleanup,
                "outcome_unknown": unknown,
                "oldest_seconds": int(age),
            }
        )
    states = {}
    for computer in computers:
        states[computer.status] = states.get(computer.status, 0) + 1
    return {"items": items, "computers": states}
