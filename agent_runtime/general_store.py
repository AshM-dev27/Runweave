"""V3 transactional state, evidence admission and durable operation identities."""

import copy
import json
import time
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import select

from .db import GateRow, OutboxRow, RunRow, ToolkitRunRow
from .general_catalog import descriptor
from .general_contracts import CompletionAssessment, Criterion, TaskGoal, TaskState
from .general_db import (
    GeneralOperationRow,
    GeneralRecordRow,
    GeneralRunRow,
    ProjectBranchRow,
    ProjectGrantRow,
    ProjectWorkspaceRow,
)
from .project_store import ProjectStore, covered, digest, fail, path
from .resource_store import ResourceStore


class GeneralStore(ResourceStore, ProjectStore):
    async def general(self, run_id):
        async with self.database.sessions() as db:
            row = await db.get(GeneralRunRow, run_id)
            return None if row is None else {"root_id": row.root_id, "parent_id": row.parent_id, **row.data}

    async def general_lock(self, db, run_id, active=True, admission=True):
        await db.scalar(select(GateRow).where(GateRow.id == 1).with_for_update())
        gr = await db.get(GeneralRunRow, run_id)
        if gr is None:
            fail("General run not found", 404)
        root = await self.locked(db, gr.root_id)
        row = root if run_id == root.id else await self.locked(db, run_id)
        if active and (
            root.status in {"completed", "failed", "cancelled"}
            or row.status in {"completed", "failed", "cancelled"}
        ):
            fail("run_terminal", 409)
        root_state = await db.get(GeneralRunRow, gr.root_id)
        from .resources import ResourceBlocked

        paused = root_state.data.get("resource_state", {}).get("pause")
        if active and admission and paused:
            raise ResourceBlocked(paused["block"])
        if active and (admission or not root_state.data["policy"].get("resources")):
            self.general_check_time(gr, root_state)
        return row, gr, root_state

    @staticmethod
    def general_paused_seconds(data):
        starts = [
            v
            for v in [
                data.get("approval_started"),
                (data.get("resource_state", {}).get("pause") or {}).get("started"),
            ]
            if v is not None
        ]
        return data.get("paused_seconds", 0) + (time.time() - min(starts) if starts else 0)

    @classmethod
    def general_time_remaining(cls, data):
        start = data.get("active_started")
        return data["policy"]["limits"]["active_seconds"] - (
            time.time() - start - cls.general_paused_seconds(data) if start is not None else 0
        )

    def general_check_time(self, gr, root):
        from .resources import ResourceBlocked, policy, shared

        scopes = [(root, self.general_time_remaining(root.data), False)]
        if gr.parent_id and not shared(root.data) and gr.data.get("active_started") is not None:
            remaining = gr.data["local_limits"]["active_seconds"] - (
                time.time()
                - gr.data["active_started"]
                - self.general_paused_seconds(root.data)
                + gr.data.get("pause_baseline", 0)
            )
            scopes.append((gr, remaining, True))
        for owner, remaining, local in scopes:
            if remaining > 0:
                continue
            if not policy(root.data):
                fail("run_timeout", 409)
            state = root.data["resource_state"]
            limit = (
                owner.data["local_limits"]["active_seconds"]
                if local
                else root.data["policy"]["limits"]["active_seconds"]
            )
            raise ResourceBlocked(
                {
                    "resource": "active_seconds",
                    "scope": "child" if local else "root",
                    "owner": owner.run_id,
                    "run_id": gr.run_id,
                    "source": "assignment.limits" if local else state["sources"]["active_seconds"],
                    "limit": limit,
                    "used": limit - remaining,
                    "reserved": 0,
                    "required": 0,
                    "finalization_reserve": 0,
                    "policy_version": state["version"],
                    "reason": "limit",
                }
            )

    async def general_record(self, db, gr, kind, identity, data):
        old = await db.get(GeneralRecordRow, identity)
        if old:
            if old.data != data:
                fail("record_conflict", 409)
            return
        encoded = json.dumps(data).encode()
        root = await db.get(GeneralRunRow, gr.root_id)
        state = copy.deepcopy(root.data)
        counts = state.setdefault("record_counts", {})
        count = counts.get(kind, 0)
        if len(encoded) > 32768 or count >= 128 or state.get("metadata_bytes", 0) + len(encoded) > 4194304:
            fail("context_limit", 409)
        counts[kind] = count + 1
        state["metadata_bytes"] = state.get("metadata_bytes", 0) + len(encoded)
        root.data = state
        # Sequence is globally increasing per kind, also unique in each run.
        db.add(GeneralRecordRow(id=identity, run_id=gr.run_id, kind=kind, sequence=count, data=data))
        await db.flush()

    async def initialize_general(self, db, row, config, body):
        from pathlib import Path

        operator = json.loads(Path("config/general.json").read_text())
        from .artifacts import verify
        from .db import ArtifactRow

        if len(set(body.artifact_ids)) != len(body.artifact_ids):
            fail("Duplicate artifact IDs")
        for aid in body.artifact_ids:
            artifact = await db.get(ArtifactRow, aid)
            if artifact is None:
                fail("Artifact not found", 404)
            verify(artifact)
        aliases = list(config.tools)
        from .db import SessionRow

        session = await db.get(SessionRow, row.session_id)
        if (
            config.general.context_policy == "memory-v1"
            and session.history
            and "session_history" not in aliases
        ):
            aliases.append("session_history")
        if config.general.skills and "skill_read" not in aliases:
            aliases.append("skill_read")
        tools = self.extensions.snapshots(aliases)
        skills = self.extensions.selected_skills(config.general.skills)
        context_policy = self.context_policies.get(config.general.context_policy)
        executor = self.executors.get(config.general.workspace_policy)
        review_registration = None
        if config.general.review:
            review = config.general.review
            review_config = config.model_copy(
                update={
                    "provider": review.provider or config.provider,
                    "model": review.model or config.model,
                    "max_tokens": review.max_tokens,
                }
            )
            registration = self.selection(review_config)
            from .db import RegistrationRow

            review_registration = registration.identity
            if await db.get(RegistrationRow, registration.identity) is None:
                db.add(RegistrationRow(id=registration.identity, config=registration.model_dump()))
                await db.flush()
        policy = config.general.model_dump()
        from .resources import LEGACY_CEILINGS, RESOURCE_KEYS

        ceilings = (
            operator.get("resource_ceilings", LEGACY_CEILINGS) if policy.get("resources") else LEGACY_CEILINGS
        )
        if any(type(ceilings.get(k)) is not int or ceilings[k] < 1 for k in RESOURCE_KEYS):
            fail("invalid_operator_resource_policy", 422)
        if any(policy["limits"][k] > ceilings[k] for k in RESOURCE_KEYS):
            fail("resource_limit_exceeds_operator", 422)
        if not policy.get("resources") and policy.get("delegation"):
            legacy_child = dict(
                model_attempts=4, tool_attempts=16, command_attempts=4, total_tokens=16000, active_seconds=240
            )
            if any(v > legacy_child[k] for k, v in policy["delegation"]["limits"].items()):
                fail("resource_limit_exceeds_operator", 422)
        if (
            policy.get("resources")
            and policy["resources"]["allocation"] == "fixed"
            and policy.get("delegation")
        ):
            if any(v > ceilings[k] for k, v in policy["delegation"]["limits"].items()):
                fail("child_limit_exceeds_operator", 422)
        if policy.get("resources") and any(
            v > ceilings[k] for k, v in policy["resources"]["finalization"].items()
        ):
            fail("finalization_reserve_exceeds_operator", 422)
        reg = self.selection(config)
        if policy["limits"]["total_tokens"] > reg.total_tokens_limit:
            if "total_tokens" in config.general.limits.model_fields_set:
                fail("Token limit exceeds registration")
            policy["limits"]["total_tokens"] = reg.total_tokens_limit
        if (
            not policy.get("resources")
            and (any(a.startswith("workspace_") for a in config.tools) or config.general.delegation)
            and policy["limits"]["total_tokens"] < config.max_tokens + 2560
        ):
            fail("insufficient_reporting_reserve", 422)
        if body.task:
            if any(
                c.id.startswith("runtime.") or any(check.id.startswith("runtime.") for check in c.checks)
                for c in body.task.criteria
            ):
                fail("reserved_criterion_identity", 422)
            goal = body.task.model_copy(deep=True)
            goal.version = 1
            for c in goal.criteria:
                c.origin = "user"
        else:
            goal = TaskGoal(
                outcome=body.input[:4000],
                criteria=[Criterion(id="outcome", statement=body.input[:512], origin="model")],
            )
        if len(goal.model_dump_json().encode()) > 8192:
            fail("context_limit", 422)
        state = TaskState(unresolved=[c.id for c in goal.criteria])
        gr = GeneralRunRow(
            run_id=row.id,
            root_id=row.id,
            parent_id=None,
            data={
                "policy": policy,
                **(
                    {
                        "resource_state": {
                            "version": 1,
                            "ceilings": {
                                **ceilings,
                                "total_tokens": min(ceilings["total_tokens"], reg.total_tokens_limit),
                            },
                            "sources": {
                                k: "agent.limits"
                                if k in config.general.limits.model_fields_set
                                else "default"
                                for k in RESOURCE_KEYS
                            },
                            "pause": None,
                        }
                    }
                    if policy.get("resources")
                    else {}
                ),
                "operator": operator,
                "context_policy_version": context_policy.version,
                "executor_version": executor.version,
                "review_registration_id": review_registration,
                "skills": skills,
                "tools": tools,
                "goal": goal.model_dump(),
                "task_state": state.model_dump(),
                "budget": {
                    "model_attempts": 0,
                    "tool_attempts": 0,
                    "command_attempts": 0,
                    "reported_tokens": 0,
                    "reserved_tokens": 0,
                    "revisions": 0,
                    "storage_bytes": 0,
                },
                "artifact_ids": body.artifact_ids,
                "children": {},
                "denied": [],
                "fence": 0,
                "cleanup_state": "complete",
                "step": 0,
                "stagnation": 0,
                "seen": [],
                "last_result": None,
            },
        )
        db.add(gr)
        db.add(
            ToolkitRunRow(
                run_id=row.id,
                root_id=row.id,
                parent_id=None,
                state={"execution_version": 3, "cleanup_state": "complete"},
            )
        )
        await db.flush()
        if body.workspace:
            await self.general_attach(db, gr, body.workspace.workspace_id, body.workspace.revision_id)
        await self.general_record(db, gr, "task", row.id + ":state:0", gr.data["task_state"])

    async def general_attach(self, db, gr, wid, revision, read=None, write=None):
        files = await self.project_files(db, wid, revision)
        from .project_store import validate_files

        validate_files(files, gr.data["policy"]["limits"])
        read, write = read if read is not None else [""], write if write is not None else [""]
        for prefix in read + write:
            path(prefix, prefix=True)
        files = {p: b for p, b in files.items() if covered(p, read)}
        state = copy.deepcopy(gr.data)
        root = await db.get(GeneralRunRow, gr.root_id)
        rs = copy.deepcopy(root.data)
        hashes = dict(rs.get("project_hashes", {}))
        hashes.update({digest(b): len(b) for b in files.values()})
        if sum(hashes.values()) > rs["policy"]["limits"]["storage_bytes"]:
            fail("project_storage_limit", 409)
        rs["budget"]["revisions"] += 1
        if rs["budget"]["revisions"] > rs["policy"]["limits"]["revisions"]:
            fail("project_revision_limit", 409)
        rs["project_hashes"] = hashes
        rs["budget"]["storage_bytes"] = sum(hashes.values())
        root.data = rs
        bid = str(uuid5(NAMESPACE_URL, gr.run_id + ":branch"))
        if gr.parent_id:
            revision = await self.project_commit(db, wid, files, [revision])
        db.add(ProjectBranchRow(id=bid, workspace_id=wid, run_id=gr.run_id, head=revision, version=1))
        await db.flush()
        db.add(
            ProjectGrantRow(
                run_id=gr.run_id,
                branch_id=bid,
                data={"read_prefixes": read, "write_prefixes": write, "revisions": [revision]},
            )
        )
        state = copy.deepcopy(gr.data)
        state["input_checks"] = {
            p: digest(b)
            for p, b in files.items()
            if p.rsplit("/", 1)[-1].startswith("test_") or p.endswith("_test.py")
        }
        state["workspace"] = {"workspace_id": wid, "revision_id": revision, "branch_id": bid}
        state["task_state"].update(branch_id=bid, head=revision)
        gr.data = state

    async def general_ensure_workspace(self, db, gr):
        if gr.data.get("workspace"):
            return
        wid = str(uuid5(NAMESPACE_URL, gr.run_id + ":workspace"))
        workspace = ProjectWorkspaceRow(
            id=wid, key="run:" + gr.run_id, fingerprint=digest({}), principal="app", initial_revision=""
        )
        db.add(workspace)
        await db.flush()
        workspace.initial_revision = await self.project_commit(db, wid, {}, [])
        await self.general_attach(db, gr, wid, workspace.initial_revision)

    async def general_records(self, run_id, kind, cursor=0, limit=16):
        if not await self.general(run_id):
            fail("General run not found", 404)
        async with self.database.sessions() as db:
            rows = list(
                await db.scalars(
                    select(GeneralRecordRow)
                    .where(
                        GeneralRecordRow.run_id == run_id,
                        GeneralRecordRow.kind == kind,
                        GeneralRecordRow.sequence >= cursor,
                    )
                    .order_by(GeneralRecordRow.sequence)
                    .limit(limit + 1)
                )
            )
            items = [{"id": r.id, **r.data} for r in rows[:limit]]
            if kind == "verification":
                gr = await db.get(GeneralRunRow, run_id)
                for item in items:
                    item["fresh"] = item["revision_id"] == gr.data["task_state"]["head"]
            return {"items": items, "next_cursor": rows[limit].sequence if len(rows) > limit else None}

    async def general_capabilities(self, run_id, query="", cursor=0, limit=16):
        gr = await self.general(run_id)
        if gr is None:
            fail("General run not found", 404)
        entries = [
            descriptor(e).model_dump()
            for a, e in sorted(gr["tools"].items())
            if query.casefold() in (a + e["description"]).casefold()
        ]
        return {
            "items": entries[cursor : cursor + limit],
            "next_cursor": cursor + limit if cursor + limit < len(entries) else None,
            "policy": gr["policy"],
            "operator": gr["operator"],
        }

    async def general_checkpoint(self, db, row, gr, operation_id, result, progress=False):
        state = copy.deepcopy(gr.data)
        task = state["task_state"]
        task["version"] += 1
        task["checkpoint_id"] = operation_id + ":checkpoint"
        state["step"] += 1
        context_result = copy.deepcopy(result)
        if "content_base64" in context_result:
            context_result.pop("content_base64")
            context_result["bytes_omitted_from_context"] = True
        if len(json.dumps(context_result).encode()) > 8192:
            context_result = {
                k: result[k]
                for k in ("error", "revision_id", "verification_ids", "outcome", "changed")
                if k in result
            }
            context_result.update(truncated=True, operation_id=operation_id)
        state["last_result"] = context_result
        signature = digest(result)
        if result.get("changed") and state.get("workspace"):
            branch, _ = await self.granted_branch(db, row.id)
            revision = await self.project_revision(db, branch.workspace_id, branch.head)
            relevant = [
                f
                for f in revision.data["files"]
                if not f["path"].rsplit("/", 1)[-1].startswith("test_") and not f["path"].endswith("_test.py")
            ]
            signature = "project:" + digest(relevant)
        elif result.get("verification_ids"):
            evidence = [await db.get(GeneralRecordRow, vid) for vid in result["verification_ids"]]
            signature = "evidence:" + digest(
                [
                    {
                        **{k: r.data.get(k) for k in ("criterion_id", "spec_hash", "outcome")},
                        "dependency_hash": r.data.get("details", {}).get(
                            "progress_dependency_hash", r.data.get("dependency_hash")
                        ),
                    }
                    for r in evidence
                    if r
                ]
            )
        useful = progress and signature not in state["seen"]
        state["stagnation"] = 0 if useful else state["stagnation"] + 1
        state["seen"] = [*state["seen"], signature][-128:]
        if state.get("workspace"):
            branch, _ = await self.granted_branch(db, row.id)
            task["head"] = branch.head
            state["workspace"]["revision_id"] = branch.head
        gr.data = state
        await self.general_record(
            db, gr, "checkpoint", task["checkpoint_id"], {"state": task, "result": context_result}
        )
        await self.emit(
            db,
            row,
            operation_id + ":checkpoint",
            "task.updated",
            {"state_version": task["version"], "checkpoint_id": task["checkpoint_id"]},
        )

    async def completion_gaps(self, db, row, gr, action):
        assessment = CompletionAssessment.model_validate(action["assessment"])
        state, goal = gr.data["task_state"], gr.data["goal"]
        gaps = []
        if (assessment.state_version, assessment.goal_version, assessment.revision_id) != (
            state["version"],
            goal["version"],
            state["head"],
        ):
            gaps.append("stale_proposal")
        from .general_receipts import current_checks

        authoritative = await current_checks(self, db, gr)
        dispositions = {d.criterion_id: d for d in assessment.criteria}
        if len(dispositions) != len(assessment.criteria):
            gaps.append("duplicate_criteria")
        for criterion in goal["criteria"]:
            if not criterion["required"]:
                continue
            d = dispositions.get(criterion["id"])
            if d is None or d.disposition != "satisfied":
                gaps.append("unsatisfied:" + criterion["id"])
                continue
            if criterion["evidence_policy"] == "assessment":
                if not d.assessment:
                    gaps.append("assessment_required:" + criterion["id"])
                continue
            evidence = []
            for vid in d.evidence_ids:
                record = await db.get(GeneralRecordRow, vid)
                if record is None or record.run_id != row.id or record.kind != "verification":
                    gaps.append("invalid_evidence:" + criterion["id"])
                    continue
                v = record.data
                if (
                    v["criterion_id"] == criterion["id"]
                    and v["outcome"] == "pass"
                    and v["goal_version"] == goal["version"]
                    and (v["method"] == "source" or v["revision_id"] == state["head"])
                ):
                    if (
                        criterion["evidence_policy"] == "source"
                        and v["method"] == "source"
                        or criterion["evidence_policy"] == "check"
                        and v["method"] != "source"
                        and v["provenance"] in {"user", "runtime"}
                    ):
                        if criterion["evidence_policy"] == "check":
                            latest = authoritative.get((criterion["id"], v["check_id"]))
                            if latest is None or latest["id"] != vid or latest["outcome"] != "pass":
                                continue
                        evidence.append(v)
            required_checks = {s["id"] for s in criterion["checks"]}
            if not evidence or not required_checks.issubset({v["check_id"] for v in evidence}):
                gaps.append("evidence_required:" + criterion["id"])
        for cid in gr.data["children"]:
            child = await db.get(RunRow, cid)
            if child.status not in {"completed", "failed", "cancelled"}:
                gaps.append("live_children")
        pending = list(
            await db.scalars(select(GeneralOperationRow).where(GeneralOperationRow.run_id == row.id))
        )
        if any(
            o.data.get("status") in {"pending", "outcome_unknown"}
            and o.id != f"{row.id}:action:{gr.data['step']}"
            and o.data.get("kind") != "completion_review"
            for o in pending
        ):
            gaps.append("unresolved_operations")
        if not gr.parent_id and gr.data["children"]:
            child_operations = await db.scalars(
                select(GeneralOperationRow).where(GeneralOperationRow.run_id.in_(list(gr.data["children"])))
            )
            if any(
                o.data.get("external") and o.data.get("status") in {"pending", "outcome_unknown"}
                for o in child_operations
            ):
                gaps.append("unresolved_child_effects")
        return assessment, gaps

    async def general_complete(self, db, row, gr, action):
        assessment, gaps = await self.completion_gaps(db, row, gr, action)
        state = gr.data["task_state"]
        if gr.data["policy"].get("review"):
            from .completion_review import review_input

            request, identity = await review_input(self, db, row, gr, action)
            record = await db.get(GeneralOperationRow, identity)
            verdict = record.data.get("result") if record else None
            assessment.review = verdict or {"verdict": "defer", "reason": "review_required"}
            if not verdict or verdict["verdict"] != "pass":
                gaps.append("semantic_review:" + (verdict["verdict"] if verdict else "required"))
        assessment.accepted = not gaps
        assessment.remaining_gaps = sorted(set(gaps))[:24]
        gr.data = {**gr.data, "completion_assessment": assessment.model_dump()}
        if not gaps:
            gr.data = {**gr.data, "task_state": {**gr.data["task_state"], "unresolved": []}}
            row.status = "completed"
            row.output = {"answer": action["answer"], "value": action.get("value")}
            if gr.parent_id is None:
                from .db import SessionRow
                from .general_history import completed_turn

                session = await db.get(SessionRow, row.session_id)
                session.history = completed_turn(
                    session.history,
                    row.input,
                    action["answer"],
                    bounded=gr.data["policy"].get("context_policy") == "memory-v1",
                )
            await self.emit(db, row, "terminal", "run.completed")
        else:
            await self.emit(
                db,
                row,
                "reject:" + str(state["version"]),
                "completion.rejected",
                {"gaps": assessment.remaining_gaps},
            )
        return assessment.model_dump()

    async def general_decide(self, run_id, approval_id, approved):
        async with self.database.sessions.begin() as db:
            row, gr, _ = await self.general_lock(db, run_id, active=False)
            if gr.parent_id:
                fail("approval_requires_root", 409)
            previous = row.decisions.get(approval_id)
            if previous is not None:
                if previous != approved:
                    fail("Approval decision is immutable", 409)
            else:
                pending = next((a for a in row.approvals if a["id"] == approval_id), None)
                if row.status not in {"awaiting_approval", "paused_budget"} or pending is None:
                    fail("Approval is not pending", 409)
                op = await db.get(GeneralOperationRow, approval_id)
                if op is None or op.run_id != pending["origin_run_id"]:
                    fail("Approval is not pending", 409)
                elapsed = time.time() - gr.data.get("approval_started", time.time())
                if gr.data.get("approval_seconds", 0) + elapsed >= row.approval_wait_seconds:
                    fail("approval_timeout", 409)
                paused = copy.deepcopy(gr.data.get("resource_state", {}).get("pause"))
                overlap = (
                    max(0, time.time() - max(paused["started"], gr.data["approval_started"])) if paused else 0
                )
                gr.data = {
                    **gr.data,
                    "approval_started": None,
                    "approval_seconds": gr.data.get("approval_seconds", 0) + elapsed,
                    "paused_seconds": gr.data.get("paused_seconds", 0) + elapsed - overlap,
                }
                origin = await self.locked(db, op.run_id)
                origin.decisions = {**origin.decisions, approval_id: approved}
                row.decisions = {**row.decisions, approval_id: approved}
                if not approved:
                    gr.data = {**gr.data, "denied": sorted(set(gr.data["denied"] + [op.data["scope"]]))}
                row.status, row.approvals = "running", []
                origin.status, origin.approvals = "running", []
                if paused:
                    paused["prior_status"] = "running"
                    gr.data = {**gr.data, "resource_state": {**gr.data["resource_state"], "pause": paused}}
                    row.status = "paused_budget"
                await self.emit(
                    db,
                    row,
                    approval_id + ":decision",
                    "approval.decided",
                    {"id": approval_id, "approved": approved},
                )
                db.add(
                    OutboxRow(
                        id="general-decision:" + approval_id,
                        run_id=origin.id,
                        kind="decision",
                        payload={"id": approval_id, "approved": approved},
                    )
                )
        return await self.get(run_id)

    async def general_cancel(self, run_id):
        async with self.database.sessions.begin() as db:
            _, gr, root = await self.general_lock(db, run_id, active=False)
            root_row = await db.get(RunRow, root.run_id)
            if root_row.status in {"completed", "failed", "cancelled"}:
                return await self.get(root.run_id)
            root.data = {**root.data, "fence": root.data["fence"] + 1, "cleanup_state": "pending"}
            for rid in [root.run_id, *root.data["children"]]:
                row = await self.locked(db, rid)
                if row.status not in {"completed", "failed", "cancelled"}:
                    row.status, row.approvals = "cancelled", []
                    await self.emit(db, row, "terminal", "run.cancelled")
                    db.add(OutboxRow(id="cancel:" + rid, run_id=rid, kind="cancel"))
        return await self.get(root.run_id)

    async def general_cleanup(self, run_id, *, allow_complete=True):
        """Acknowledge committed bundles only; pending jobs remain observable."""
        state = await self.general(run_id)
        executor = self.executors.get(state["policy"]["workspace_policy"], state.get("executor_version", 1))
        project_request = executor.request

        async with self.database.sessions() as db:
            operations = list(
                await db.scalars(select(GeneralOperationRow).where(GeneralOperationRow.run_id == run_id))
            )
        pending = False
        for op in operations:
            if op.data.get("external") and op.data.get("result") is None:
                from .extension_runtime import cleanup_extension

                pending |= not await cleanup_extension(self, run_id, op.id)
                continue
            if not op.data.get("command") or op.data.get("acknowledged"):
                continue
            try:
                identity = await self.general_command_identity(run_id, op.id)
                if op.data.get("result") is None:
                    run = await self.get(run_id)
                    if run.status in {"failed", "cancelled"}:
                        await project_request(identity, cancel=True)
                    else:
                        pending = True
                        continue
                    bundle = await project_request(
                        identity, await self.general_command_payload(run_id, op.id)
                    )
                    if bundle.get("error") == "sandbox_pending":
                        pending = True
                        continue
                    await self.general_command_commit(run_id, op.id, bundle, identity)
                for ordinal in range(1, op.data["command_attempt"] + 1):
                    await project_request(op.id + ":command:" + str(ordinal), acknowledge=True)
                async with self.database.sessions.begin() as db:
                    current = await db.get(GeneralOperationRow, op.id)
                    current.data = {**current.data, "acknowledged": True}
            except Exception:
                pending = True
        if pending or not allow_complete:
            return False
        async with self.database.sessions.begin() as db:
            row, gr, _ = await self.general_lock(db, run_id, active=False)
            # An action may have started while acknowledgments were in flight.
            current = await db.scalars(
                select(GeneralOperationRow).where(GeneralOperationRow.run_id == run_id)
            )
            if any(
                (op.data.get("external") and op.data.get("result") is None)
                or (op.data.get("command") and not op.data.get("acknowledged"))
                for op in current
            ):
                return False
            for child_id in gr.data["children"]:
                child = await db.get(GeneralRunRow, child_id)
                child_run = await db.get(RunRow, child_id)
                if child.data["cleanup_state"] != "complete" or child_run.status not in {
                    "completed",
                    "failed",
                    "cancelled",
                }:
                    return False
            if gr.data["cleanup_state"] != "complete":
                gr.data = {**gr.data, "cleanup_state": "complete"}
                await self.emit(db, row, "cleanup:" + str(gr.data["step"]), "cleanup.completed")
        return True

    async def general_command_payload(self, run_id, op_id, with_identity=False):
        import base64

        async with self.database.sessions() as db:
            op = await db.get(GeneralOperationRow, op_id)
            if op is None or op.run_id != run_id:
                fail("Operation not found", 404)
            _, _, files = await self.branch_files(db, run_id, op.data["input_revision"])
            payload = {
                **op.data["command"],
                "files": {p: base64.b64encode(b).decode() for p, b in sorted(files.items())},
            }
            return (
                (op_id + ":command:" + str(op.data["command_attempt"]), payload) if with_identity else payload
            )

    async def general_command_identity(self, run_id, op_id):
        async with self.database.sessions() as db:
            op = await db.get(GeneralOperationRow, op_id)
            if op is None or op.run_id != run_id:
                fail("Operation not found", 404)
            return op_id + ":command:" + str(op.data["command_attempt"])

    async def general_command_retry(self, run_id, op_id, identity):
        import time

        from .general_db import GeneralAttemptRow

        async with self.database.sessions.begin() as db:
            _, gr, root = await self.general_lock(db, run_id)
            op = await db.get(GeneralOperationRow, op_id)
            if identity != op_id + ":command:" + str(op.data["command_attempt"]):
                fail("attempt_superseded", 409)
            attempt = await db.get(GeneralAttemptRow, identity)
            attempt.data = {**attempt.data, "outcome": "interrupted"}
            if op.data["command_attempt"] >= 2 or op.data.get("result"):
                return False
            await self.general_charge_locked(db, gr, root, "tool_attempts")
            await self.general_charge_locked(db, gr, root, "command_attempts")
            ordinal = op.data["command_attempt"] + 1
            op.data = {
                **op.data,
                "command_attempt": ordinal,
                "command": {**op.data["command"], "deadline": time.time() + 110},
            }
            db.add(
                GeneralAttemptRow(
                    id=op_id + ":command:" + str(ordinal),
                    operation_id=op_id,
                    ordinal=ordinal,
                    data={"kind": "command", "reserved": True},
                )
            )
            return True
