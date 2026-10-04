"""Transactional adaptive settings. Adaptation never changes an authorization ceiling."""

import copy

from sqlalchemy import select

from .adaptive import AUTO_DEFAULTS, DEFAULT_CAPS, LEGACY_DEFAULTS, grow, initial_plan
from .db import ArtifactRow, RunRow, ToolkitRunRow
from .general_db import GeneralRunRow
from .schemas import AgentConfig


class AdaptiveStore:
    async def size_task(self, db, config, body, registration):
        values = config.model_dump()
        defaults = AUTO_DEFAULTS if config.adaptive else LEGACY_DEFAULTS
        for key in ("max_requests", "max_tool_calls", "timeout_seconds"):
            values[key] = values[key] or defaults[key]
        plan = None
        if config.adaptive:
            refs = [await db.get(ArtifactRow, aid) for aid in body.artifact_ids]
            plan = initial_plan(
                config,
                registration,
                input_bytes=len(body.input.encode()),
                file_bytes=sum(ref.size_bytes for ref in refs if ref),
                files=len(refs),
                criteria=len(body.task.criteria) if body.task else 0,
                checks=sum(len(c.checks) for c in body.task.criteria) if body.task else 0,
                workspace=bool(body.workspace),
            )
        values["max_tokens"] = plan["output_tokens"] if plan else (config.max_tokens or 1024)
        return AgentConfig.model_validate(values), plan

    async def pin_adaptive(self, db, row, plan):
        if not plan:
            return
        general = await db.get(GeneralRunRow, row.id)
        feature = await db.get(ToolkitRunRow, row.id)
        if general:
            data = copy.deepcopy(general.data)
            # Explicit task budgets keep their exact meaning. Default automatic caps are operator-owned.
            if data["policy"].get("resources"):
                state = data["resource_state"]
                automatic = data["operator"].get("adaptive_ceilings", DEFAULT_CAPS)
                for kind, limit in DEFAULT_CAPS.items():
                    if state["task_limits"][kind] is None:
                        bound = automatic.get(kind, limit)
                        if type(bound) is not int or bound < 1:
                            raise ValueError("invalid_adaptive_ceiling")
                        bound = min(bound, state["ceilings"][kind])
                        data["policy"]["limits"][kind] = bound
                        state["ceilings"][kind] = bound
                        if bound == automatic.get(kind, limit):
                            state["sources"][kind] = state["ceiling_sources"][kind] = (
                                "operator.adaptive_ceilings"
                            )
            caps = data["policy"]["limits"]
            plan["allowances"] = {k: min(v, caps[k]) for k, v in plan["allowances"].items()}
            data["adaptive"] = plan
            general.data = data
        else:
            b = feature.state["budget"]
            caps = dict(
                model_attempts=b["max_requests"],
                tool_attempts=b["max_tool_calls"],
                total_tokens=b["max_total_tokens"],
                active_seconds=row.config["timeout_seconds"],
                command_attempts=0,
            )
            plan["allowances"] = {k: min(v, caps[k]) for k, v in plan["allowances"].items()}
            feature.state = {**feature.state, "adaptive": plan}

    async def adaptive_settings(self, run_id, *, context_bytes=0, output_tokens=0, truncated=False):
        async with self.database.sessions.begin() as db:
            general = await db.scalar(select(GeneralRunRow.run_id).where(GeneralRunRow.run_id == run_id))
            if general:
                row, local, root = await self.general_lock(db, run_id, active=False)
                owner = root
                root_row = await db.get(RunRow, root.run_id)
                data = copy.deepcopy(root.data)
                plan = data.get("adaptive")
                caps = {k: data["policy"]["limits"][k] for k in DEFAULT_CAPS}
                budget = data["budget"]
                required = {
                    k: budget.get(k, 0) + 1 for k in ("model_attempts", "tool_attempts", "command_attempts")
                }
                required["active_seconds"] = (
                    max(0, int(caps["active_seconds"] - self.general_time_remaining(data))) + 30
                )
            else:
                root_row, root, row, local = await self.tree_lock(db, run_id, active=False)
                owner = local
                data = copy.deepcopy(local.state)
                plan = data.get("adaptive")
                budget = root.state["budget"]
                caps = dict(
                    model_attempts=budget["max_requests"],
                    tool_attempts=budget["max_tool_calls"],
                    total_tokens=budget["max_total_tokens"],
                    active_seconds=row.config["timeout_seconds"],
                    command_attempts=0,
                )
                required = dict(model_attempts=budget["requests"] + 1, tool_attempts=budget["tool_calls"] + 1)
            if not plan:
                return None
            if row.status in {"completed", "failed", "cancelled"} or root_row.status in {
                "completed",
                "failed",
                "cancelled",
            }:
                return plan
            required["total_tokens"] = (
                budget["reported_tokens"]
                + budget["reserved_tokens"]
                + context_bytes
                + plan["output_tokens"]
                + 1024
            )
            updated = grow(
                plan,
                required,
                caps,
                context_bytes=context_bytes,
                output_tokens=output_tokens,
                truncated=truncated,
            )
            if updated != plan:
                data["adaptive"] = updated
                if general:
                    owner.data = data
                else:
                    owner.state = data
                await self.emit(
                    db,
                    row,
                    f"adaptive:{run_id}:{updated['revision']}",
                    "resources.adapted",
                    {"adaptive": updated},
                )
            return updated
