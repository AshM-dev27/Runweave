"""Transactional toolkit controls. Root locks serialize reservations and effect fences."""

import json
from uuid import NAMESPACE_URL, uuid4, uuid5

from sqlalchemy import select

from . import artifacts
from .artifact_store import ArtifactStore
from .db import AgentRow, ArtifactRow, GateRow, RegistrationRow, RunRow, ToolkitOperationRow, ToolkitRunRow
from .schemas import AgentConfig
from .tool_registry import digest


class ToolkitStore(ArtifactStore):
    async def validate_tool_config(self, db, config, agent_id=None):
        from .store import Problem

        if config.general:
            try:
                self.context_policies.get(config.general.context_policy)
                self.executors.get(config.general.workspace_policy)
                self.extensions.selected_skills(config.general.skills)
                return self.extensions.snapshots(config.tools), {}
            except ValueError:
                raise Problem(422, "Unknown capability, skill, or runtime policy") from None
        try:
            tools = self.tools.select(config.tools)
        except ValueError:
            raise Problem(422, "Unknown operator tool") from None
        snapshots = {}
        for spec in config.subagents:
            child = await db.get(AgentRow, spec.agent_id)
            if child is None or child.id == agent_id:
                raise Problem(422, "Invalid specialist")
            cc = AgentConfig.model_validate(child.config)
            if cc.subagents or "delegate" in cc.tools:
                raise Problem(422, "Nested delegation forbidden")
            selected = self.tools.select(cc.tools)
            if config.delegation_mode == "parallel_read" and any(
                t["effect"] == "approval_write" for t in selected.values()
            ):
                raise Problem(422, "Parallel specialists cannot require approval")
            reg = self.selection(cc)
            if await db.get(RegistrationRow, reg.identity) is None:
                db.add(RegistrationRow(id=reg.identity, config=reg.model_dump()))
                await db.flush()
            snapshots[spec.name] = {
                "agent_id": child.id,
                "config": cc.model_dump(),
                "tools": selected,
                "registration_id": reg.identity,
                "description": spec.description,
            }
        return tools, snapshots

    async def initialize_toolkit(self, db, row, config, artifact_ids):
        from .store import Problem

        tools, snapshots = await self.validate_tool_config(db, config, row.agent_id)
        if len(set(artifact_ids)) != len(artifact_ids):
            raise Problem(422, "Duplicate artifact IDs")
        for aid in artifact_ids:
            artifact = await db.get(ArtifactRow, aid)
            if artifact is None:
                raise Problem(404, "Artifact not found")
            artifacts.verify(artifact)
        reg = self.selection(config)
        if config.max_total_tokens and config.max_total_tokens > reg.total_tokens_limit:
            raise Problem(422, "Token limit exceeds registration")
        state = {
            "execution_version": 2
            if (config.adaptive and reg.adapter != "fake")
            or config.max_total_tokens is not None
            or artifact_ids
            or snapshots
            or any(t not in {"add", "record_note", "convert_temperature"} for t in config.tools)
            else 1,
            "tools": tools,
            "specialists": snapshots,
            "artifact_ids": artifact_ids,
            "artifacts": [],
            "children": {},
            "evidence": [],
            "denied": False,
            "outcome_tracking": 1,
            "outcome_issues": {},
            "sandbox_jobs": {},
            "cleanup_state": "complete",
            "budget": {
                "requests": 0,
                "tool_calls": 0,
                "reported_tokens": 0,
                "reserved_tokens": 0,
                "max_requests": config.max_requests,
                "max_tool_calls": config.max_tool_calls,
                "max_total_tokens": min(
                    reg.total_tokens_limit, config.max_total_tokens or reg.total_tokens_limit
                ),
            },
        }
        db.add(ToolkitRunRow(run_id=row.id, root_id=row.id, parent_id=None, state=state))

    async def toolkit(self, run_id):
        async with self.database.sessions() as db:
            row = await db.get(ToolkitRunRow, run_id)
            return None if row is None else {"root_id": row.root_id, "parent_id": row.parent_id, **row.state}

    async def tree_lock(self, db, run_id, *, active=True):
        from .store import TERMINAL, Problem

        # Global gate gives SQLite tests the same ordering and avoids cross-root artifact quota races.
        await db.scalar(select(GateRow).where(GateRow.id == 1).with_for_update())
        feature = await db.get(ToolkitRunRow, run_id)
        if feature is None:
            raise Problem(404, "Toolkit run not found")
        root = await self.locked(db, feature.root_id)
        root_feature = await db.get(ToolkitRunRow, feature.root_id)
        run = root if root.id == run_id else await self.locked(db, run_id)
        if active and (root.status in TERMINAL or run.status in TERMINAL):
            raise Problem(409, "run_terminal")
        return root, root_feature, run, feature

    async def reserve_usage(self, run_id, kind, tokens=0):
        from .store import Problem

        async with self.database.sessions.begin() as db:
            root, rf, run, feature = await self.tree_lock(db, run_id)
            state = dict(rf.state)
            budget = dict(state["budget"])
            local = dict(feature.state.get("usage", {"requests": 0, "tool_calls": 0}))
            if (
                budget[kind] >= budget["max_" + kind]
                or local[kind] >= run.config["max_" + kind]
                or budget["reported_tokens"] + budget["reserved_tokens"] + tokens > budget["max_total_tokens"]
                or local.get("reported_tokens", 0) + local.get("reserved_tokens", 0) + tokens
                > (run.config.get("max_total_tokens") or budget["max_total_tokens"])
            ):
                raise Problem(409, "budget_exhausted")
            budget[kind] += 1
            budget["reserved_tokens"] += tokens
            local[kind] += 1
            local["reserved_tokens"] = local.get("reserved_tokens", 0) + tokens
            state["budget"] = budget
            if feature.run_id == rf.run_id:
                state["usage"] = local
            else:
                feature.state = {**feature.state, "usage": local}
            rf.state = state
            await self.emit(
                db,
                root,
                "reserve:" + uuid4().hex,
                "budget.reserved",
                {"origin_run_id": run_id, "kind": kind, "tokens": tokens},
            )

    async def reconcile_usage(self, run_id, reserved, used):
        async with self.database.sessions.begin() as db:
            root, rf, _, feature = await self.tree_lock(db, run_id, active=False)
            local = dict(feature.state.get("usage", {}))
            local["reserved_tokens"] = local.get("reserved_tokens", 0) - reserved
            local["reported_tokens"] = local.get("reported_tokens", 0) + used
            feature.state = {**feature.state, "usage": local}
            b = dict(rf.state["budget"])
            b["reserved_tokens"] -= reserved
            b["reported_tokens"] += used
            rf.state = {**rf.state, "budget": b}
            await self.emit(
                db,
                root,
                "settle:" + uuid4().hex,
                "budget.reconciled",
                {"origin_run_id": run_id, "reported_tokens": used},
            )

    async def operation_result(self, run_id, call_id, arguments):
        from .store import Problem

        identity = f"{run_id}:{call_id}"
        async with self.database.sessions.begin() as db:
            await self.tree_lock(db, run_id)
            row = await db.get(ToolkitOperationRow, identity)
            fingerprint = digest(arguments)
            if row:
                if row.digest != fingerprint:
                    raise Problem(409, "operation_arguments_changed")
                return row.result
            db.add(ToolkitOperationRow(id=identity, run_id=run_id, digest=fingerprint, result=None))
        return None

    async def record_tool_failure(self, run_id, call_id, code):
        """Persist only sanitized facts; retries cannot overwrite a committed success."""
        async with self.database.sessions.begin() as db:
            root, root_feature, row, feature = await self.tree_lock(db, run_id, active=False)
            operation = await db.get(ToolkitOperationRow, f"{run_id}:{call_id}")
            if (
                root.status in {"completed", "failed", "cancelled"}
                or row.status in {"completed", "failed", "cancelled"}
                or (operation and operation.result is not None and code != "operation_arguments_changed")
            ):
                return
            for owner in (feature,) if feature.run_id == root_feature.run_id else (feature, root_feature):
                owner.state = {
                    **owner.state,
                    "outcome_issues": {**owner.state.get("outcome_issues", {}), f"{run_id}:{call_id}": code},
                }
            await self.emit(
                db,
                root,
                f"tool-error:{run_id}:{call_id}",
                "tool.failed",
                {"origin_run_id": run_id, "call_id": call_id, "error": code},
            )

    async def save_operation(self, run_id, call_id, result, tool="delegate"):
        from .store import Problem

        if len(json.dumps(result).encode()) > 16384:
            raise Problem(409, "tool_result_limit")
        async with self.database.sessions.begin() as db:
            root, root_feature, _, feature = await self.tree_lock(db, run_id)
            row = await db.get(ToolkitOperationRow, f"{run_id}:{call_id}")
            for owner in (feature,) if feature.run_id == root_feature.run_id else (feature, root_feature):
                issues = dict(owner.state.get("outcome_issues", {}))
                issues.pop(f"{run_id}:{call_id}", None)
                owner.state = {**owner.state, "outcome_issues": issues}
            if row.result is None:
                row.result = result
                evidence = result.get("evidence", []) if isinstance(result, dict) else []
                feature.state = {**feature.state, "evidence": (feature.state["evidence"] + evidence)[:12]}
                await self.emit(
                    db,
                    root,
                    f"tool:{run_id}:{call_id}",
                    "tool.completed",
                    {"origin_run_id": run_id, "call_id": call_id, "tool": tool},
                )
            return row.result

    async def create_child(self, run_id, call_id, task):
        from .store import Problem

        async with self.database.sessions.begin() as db:
            root, rf, run, feature = await self.tree_lock(db, run_id)
            if feature.parent_id:
                raise Problem(409, "delegation_depth")
            children = dict(rf.state["children"])
            fingerprint = digest(task)
            if call_id in children:
                if children[call_id]["digest"] != fingerprint:
                    raise Problem(409, "delegation_arguments_changed")
                return children[call_id]["id"]
            if len(children) >= run.config.get("max_children", 2):
                raise Problem(409, "delegation_limit")
            spec = feature.state["specialists"].get(task["specialist"])
            if (
                spec is None
                or len(task["artifact_ids"]) != len(set(task["artifact_ids"]))
                or not set(task["artifact_ids"]).issubset(
                    feature.state["artifact_ids"] + [a["id"] for a in feature.state["artifacts"]]
                )
            ):
                raise Problem(422, "invalid_delegation")
            cid = str(uuid5(NAMESPACE_URL, f"{root.id}:{call_id}"))
            from .schemas import RunCreate

            selected = AgentConfig.model_validate(spec["config"])
            child_config, child_plan = await self.size_task(
                db,
                selected,
                RunCreate(
                    agent_id=spec["agent_id"], input=task["instruction"], artifact_ids=task["artifact_ids"]
                ),
                await self.registration(spec["registration_id"]),
            )
            # Legacy NOT NULL columns retained; public/session behavior is detached for child rows.
            child = RunRow(
                id=cid,
                session_id=run.session_id,
                agent_id=spec["agent_id"],
                key="child:" + cid,
                fingerprint=fingerprint,
                config=child_config.model_dump(),
                input=task["instruction"],
                registration_id=spec["registration_id"],
                approval_wait_seconds=run.approval_wait_seconds,
            )
            db.add(child)
            await db.flush()
            db.add(
                ToolkitRunRow(
                    run_id=cid,
                    root_id=root.id,
                    parent_id=run_id,
                    state={
                        "tools": spec["tools"],
                        "specialists": {},
                        "artifact_ids": task["artifact_ids"],
                        "artifacts": [],
                        "evidence": [],
                        "specialist": task["specialist"],
                        **({"adaptive": child_plan} if child_plan else {}),
                        "outcome_tracking": 1,
                        "outcome_issues": {},
                    },
                )
            )
            children[call_id] = {"id": cid, "digest": fingerprint}
            rf.state = {**rf.state, "children": children, "cleanup_state": "pending"}
            await self.emit(db, child, "created", "run.queued")
            await self.emit(
                db,
                root,
                "child:" + cid,
                "child.queued",
                {"child_run_id": cid, "specialist": task["specialist"]},
            )
            return cid

    async def children(self, run_id):
        await self.get(run_id)
        async with self.database.sessions() as db:
            ids = list(
                await db.scalars(
                    select(ToolkitRunRow.run_id)
                    .where(ToolkitRunRow.parent_id == run_id)
                    .order_by(ToolkitRunRow.run_id)
                )
            )
        return [await self.get(i) for i in ids]

    async def budget(self, run_id):
        data = await self.toolkit(run_id)
        from .store import Problem

        if data is None:
            raise Problem(404, "Toolkit run not found")
        if data.get("execution_version") == 3:
            root = await self.general(data["root_id"])
            b, limits = root["budget"], root["policy"]["limits"]
            return {
                "execution_version": 3,
                "accounting_mode": "shared_ledger",
                "requests": b["model_attempts"],
                "tool_calls": b["tool_attempts"],
                "reported_tokens": b["reported_tokens"],
                "reserved_tokens": b["reserved_tokens"],
                "max_requests": limits["model_attempts"],
                "max_tool_calls": limits["tool_attempts"],
                "max_total_tokens": limits["total_tokens"],
                "v3": {"counters": b, "limits": limits},
                "adaptive": root.get("adaptive"),
            }
        root = await self.toolkit(data["root_id"])
        if root.get("execution_version", 1) == 2:
            return {
                **root["budget"],
                "execution_version": 2,
                "accounting_mode": "shared_ledger",
                "successful_usage": None,
                "adaptive": root.get("adaptive"),
            }
        async with self.database.sessions() as db:
            row = await db.get(RunRow, run_id)
            return {
                "execution_version": 1,
                "accounting_mode": "recorded_successful_usage",
                "requests": None,
                "tool_calls": None,
                "reported_tokens": None,
                "reserved_tokens": None,
                "max_requests": row.config["max_requests"],
                "max_tool_calls": row.config["max_tool_calls"],
                "max_total_tokens": None,
                "successful_usage": row.usage or {},
                "adaptive": data.get("adaptive"),
            }

    async def sandbox_intent(self, run_id, operation_id):
        import hashlib

        from .store import Problem

        identity = hashlib.sha256(operation_id.encode()).hexdigest()
        async with self.database.sessions.begin() as db:
            root, rf, _, _ = await self.tree_lock(db, run_id)
            import time

            jobs = dict(rf.state.get("sandbox_jobs", {}))
            if identity not in jobs:
                if len(jobs) >= 4:
                    raise Problem(409, "sandbox_job_limit")
                jobs[identity] = time.time() + 30
            rf.state = {**rf.state, "sandbox_jobs": jobs, "cleanup_state": "pending"}
        return jobs[identity]

    async def cleanup_complete(self, root_id, expected):
        async with self.database.sessions.begin() as db:
            root, rf, _, _ = await self.tree_lock(db, root_id, active=False)
            if any(rf.state.get(k) != expected.get(k) for k in ("children", "sandbox_jobs")):
                return
            if rf.state.get("cleanup_state") != "complete":
                rf.state = {**rf.state, "cleanup_state": "complete"}
                await self.emit(
                    db,
                    root,
                    "cleanup-complete:"
                    + str(len(rf.state.get("sandbox_jobs", [])))
                    + ":"
                    + str(len(rf.state.get("children", {}))),
                    "cleanup.completed",
                )
