"""Authenticated resource inspection, monotonic updates and durable budget waits."""

import copy
import time

from .db import OutboxRow, RunRow
from .general_db import GeneralOperationRow, GeneralRunRow
from .project_store import digest, fail
from .resources import RESOURCE_KEYS, ResourceBlocked, check, policy, shared, snapshot


class ResourceStore:
    async def resources(self, run_id):
        async with self.database.sessions.begin() as db:
            _, gr, root = await self.general_lock(db, run_id, active=False)
            if not policy(root.data):
                fail("resource_policy_not_enabled", 409)
            return snapshot(root, gr)

    async def update_resources(self, run_id, update, key):
        async with self.database.sessions.begin() as db:
            row, gr, root = await self.general_lock(db, run_id, active=False)
            if not policy(root.data) or (gr.parent_id and shared(root.data)):
                fail("root_or_fixed_child_resource_policy_required", 409)
            identity = run_id + ":resources:" + digest(key)
            fingerprint = digest(update.model_dump())
            old = await db.get(GeneralOperationRow, identity)
            if old:
                if old.fingerprint != fingerprint:
                    fail("resource_update_conflict", 409)
                return old.data["result"]
            root_row = await db.get(RunRow, root.run_id)
            if row.status in {"completed", "failed", "cancelled"} or root_row.status in {
                "completed",
                "failed",
                "cancelled",
            }:
                fail("run_terminal", 409)
            data = copy.deepcopy(root.data)
            state = data["resource_state"]
            if state["version"] != update.expected_version:
                fail("resource_version_conflict", 409)
            target_limits = gr.data["local_limits"] if gr.parent_id else data["policy"]["limits"]
            ceilings = data["policy"]["delegation"]["limits"] if gr.parent_id else state["ceilings"]
            for kind, value in update.limits.items():
                if type(value) is not int or value < target_limits[kind] or value > ceilings[kind]:
                    fail("resource_update_outside_ceiling", 422)
            if not any(value > target_limits[kind] for kind, value in update.limits.items()):
                fail("resource_update_requires_increase", 422)
            if gr.parent_id:
                gr.data = {
                    **gr.data,
                    "local_limits": {**target_limits, **update.limits},
                    "resource_sources": {
                        **gr.data.get("resource_sources", {}),
                        **{k: "authorized_update" for k in update.limits},
                    },
                }
            else:
                data["policy"]["limits"].update(update.limits)
            state["version"] += 1
            if not gr.parent_id:
                for kind in update.limits:
                    state["sources"][kind] = "authorized_update"
                    state.setdefault("task_limits", {})[kind] = update.limits[kind]
            root.data = data
            # Wake every run; child permissions and original model registrations stay pinned.
            for rid in [root.run_id, *root.data["children"]]:
                db.add(
                    OutboxRow(
                        id=f"resources:{root.run_id}:{state['version']}:{rid}",
                        run_id=rid,
                        kind="resources",
                        payload={"version": state["version"]},
                    )
                )
            result = snapshot(root, gr)
            db.add(
                GeneralOperationRow(
                    id=identity,
                    run_id=run_id,
                    fingerprint=fingerprint,
                    data={
                        "kind": "resource_update",
                        "sequence": 2000000 + state["version"],
                        "status": "complete",
                        "result": result,
                    },
                )
            )
            await self.emit(
                db, row, identity, "resources.updated", {"version": state["version"], "limits": update.limits}
            )
            return result

    async def resource_pause(self, run_id, block):
        async with self.database.sessions.begin() as db:
            _, gr, root = await self.general_lock(db, run_id, active=False)
            row = await db.get(RunRow, root.run_id)
            if row.status in {"completed", "failed", "cancelled"}:
                return {"terminal": True}
            p = policy(root.data)
            if not p or p["on_limit"] != "pause":
                return {"fail": True}
            data = copy.deepcopy(root.data)
            state = data["resource_state"]
            paused = state.get("pause")
            # All waiters follow the authoritative current tree pause. A stale
            # waiter or an unrelated limit increase must not clear a newer pause.
            if paused:
                block = paused["block"]
            target = await db.get(GeneralRunRow, block["run_id"])
            retry = False
            try:
                if block["resource"] == "active_seconds":
                    self.general_check_time(target, root)
                    retry = True
                elif block["resource"] in RESOURCE_KEYS:
                    check(
                        root,
                        target,
                        block["resource"],
                        block["required"],
                        block.get("finalization_reserve", 0),
                    )
                    retry = True
            except ResourceBlocked as exc:
                block = exc.snapshot
            if retry:
                if paused:
                    elapsed = time.time() - paused["started"]
                    approval = data.get("approval_started")
                    overlap = (
                        max(0, time.time() - max(approval, paused["started"])) if approval is not None else 0
                    )
                    data["paused_seconds"] = data.get("paused_seconds", 0) + elapsed - overlap
                    state["pause_seconds"] = state.get("pause_seconds", 0) + elapsed
                    state["pause"] = None
                    row.status = paused["prior_status"]
                    root.data = data
                    await self.emit(
                        db,
                        row,
                        "resource-resume:" + paused["identity"],
                        "resources.resumed",
                        {"version": state["version"]},
                    )
                return {"retry": True}
            if not paused:
                identity = digest({"version": state["version"], "block": block, "time": time.time()})
                paused = {
                    "identity": identity,
                    "started": time.time(),
                    "prior_status": row.status,
                    "block": block,
                }
                state["pause"] = paused
                root.data = data
                row.status = "paused_budget"
                await self.emit(db, row, "resource-pause:" + identity, "resources.paused", block)
            if paused["block"] != block:
                paused = {**paused, "block": block}
                state["pause"] = paused
                root.data = data
            remaining = (
                p["max_pause_seconds"] - state.get("pause_seconds", 0) - (time.time() - paused["started"])
            )
            return {"version": state["version"], "remaining": max(0, remaining), "reason": block["reason"]}
