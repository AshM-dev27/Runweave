import hashlib
from uuid import uuid4

from sqlalchemy import func, select

from .adaptive_store import AdaptiveStore
from .computer_store import ComputerStore
from .config import settings
from .db import (
    AgentRow,
    EventRow,
    GateRow,
    NoteRow,
    OutboxRow,
    RegistrationRow,
    RunRow,
    SessionRow,
    ToolkitRunRow,
)
from .general_actions import GeneralActions
from .general_store import GeneralStore
from .registry import Registration, load_registry
from .schemas import Agent, AgentConfig, Event, Run, RunCreate, Session
from .tool_registry import ToolRegistry
from .toolkit_store import ToolkitStore

TERMINAL = {"completed", "failed", "cancelled"}


class Problem(Exception):
    def __init__(self, status: int, detail: str):
        self.status, self.detail = status, detail


class Store(ComputerStore, AdaptiveStore, GeneralActions, GeneralStore, ToolkitStore):
    def __init__(
        self,
        database,
        max_active=20,
        registry=None,
        approval_wait_seconds=None,
        *,
        extensions=None,
        context_policies=None,
        executors=None,
        blobs=None,
    ):
        self.database, self.max_active = database, max_active
        self.registry = registry or load_registry()
        self.tools = ToolRegistry()
        from .blob_storage import FileBlobStorage

        self.blobs = blobs or (
            FileBlobStorage(settings().artifact_storage_path) if settings().artifact_storage_path else None
        )
        self.artifact_max_bytes = settings().artifact_max_bytes
        self.artifact_storage_bytes = settings().artifact_storage_bytes
        self.artifact_run_bytes = settings().artifact_run_bytes
        from .context import ContextPolicies
        from .executors import ExecutionBackends
        from .extensions import ExtensionRegistry

        self.extensions = extensions or ExtensionRegistry()
        self.context_policies = context_policies or ContextPolicies()
        self.executors = executors or ExecutionBackends()
        self.approval_wait_seconds = (
            settings().approval_wait_seconds if approval_wait_seconds is None else approval_wait_seconds
        )

    def selection(self, config):
        try:
            registration = self.registry.select(config)
            if (
                not config.general
                and config.max_total_tokens
                and config.max_total_tokens > registration.total_tokens_limit
            ):
                raise ValueError("Token limit exceeds registration")
            return registration
        except ValueError as exc:
            raise Problem(422, str(exc)) from None

    async def registration(self, identity):
        async with self.database.sessions() as db:
            row = await db.get(RegistrationRow, identity) if identity else None
            if row is None:
                raise ValueError("Registration unavailable")
            registration = Registration.model_validate(row.config)
            if registration.identity != identity:
                raise ValueError("Registration identity mismatch")
            return registration

    async def agent(self, config: AgentConfig, agent_id=None):
        self.selection(config)
        async with self.database.sessions.begin() as db:
            await self.validate_tool_config(db, config, agent_id)
            row = await db.get(AgentRow, agent_id) if agent_id else None
            if agent_id and row is None:
                raise Problem(404, "Agent not found")
            if row is None:
                row = AgentRow(id=str(uuid4()), config=config.model_dump())
                db.add(row)
            else:
                row.config = config.model_dump()
            return Agent(id=row.id, config=config)

    async def get_agent(self, agent_id):
        async with self.database.sessions() as db:
            row = await db.get(AgentRow, agent_id)
            if row is None:
                raise Problem(404, "Agent not found")
            return Agent(id=row.id, config=row.config)

    async def session(self):
        async with self.database.sessions.begin() as db:
            row = SessionRow(id=str(uuid4()))
            db.add(row)
            await db.flush()
            return Session(id=row.id, created_at=row.created_at)

    @staticmethod
    async def public(db, row):
        from .general_db import GeneralOperationRow, GeneralRunRow
        from .task_outcomes import classify

        feature = await db.get(ToolkitRunRow, row.id)
        general = await db.get(GeneralRunRow, row.id)
        state = feature.state if feature else {}
        assessment = (general.data.get("completion_assessment") or {}) if general else {}
        values = {k: getattr(row, k) for k in Run.model_fields if hasattr(row, k)}
        # Decisions commit before the worker consumes its signal. Only pending
        # approvals are actionable during that durable handoff window.
        values["approvals"] = [a for a in row.approvals if a["id"] not in row.decisions]
        issues = list(state.get("outcome_issues", {}).values())
        if general and row.status == "completed":
            # A model's completion assessment is not reconciliation of a failed tool.
            # Read current durable receipts so historical runs and successful recovery
            # of the same operation get the same outcome as newly completed runs.
            run_ids = [row.id, *general.data.get("children", {})]
            issues.extend(
                await db.scalars(
                    select(GeneralOperationRow.data["result"]["error"].as_string()).where(
                        GeneralOperationRow.run_id.in_(run_ids),
                        GeneralOperationRow.data["decision"]["action"]["kind"].as_string() == "invoke",
                        GeneralOperationRow.data["result"]["error"].as_string().is_not(None),
                    )
                )
            )
            if len(run_ids) > 1 and await db.scalar(
                select(RunRow.id)
                .where(RunRow.id.in_(run_ids[1:]), RunRow.status.in_(["failed", "cancelled"]))
                .limit(1)
            ):
                issues.append("child_failed")
        values["outcome"], values["outcome_reason"] = classify(
            row.status,
            error=row.error,
            denied=any(value is False for value in row.decisions.values()) or state.get("denied", False),
            issues=issues,
            accepted=assessment.get("accepted") if general else None,
            tracked=bool(general)
            or state.get("execution_version", 2 if feature and feature.parent_id else 1) == 1
            or state.get("outcome_tracking") == 1,
            pending=bool(values["approvals"]),
        )
        return Run(**values)

    async def locked(self, db, run_id):
        row = await db.scalar(select(RunRow).where(RunRow.id == run_id).with_for_update())
        if row is None:
            raise Problem(404, "Run not found")
        return row

    async def emit(self, db, row, key, kind, data=None):
        exists = await db.scalar(select(EventRow.id).where(EventRow.run_id == row.id, EventRow.dedupe == key))
        if exists is not None:
            return
        if kind.startswith("run."):
            public = await self.public(db, row)
            data = {**(data or {}), "outcome": public.outcome, "outcome_reason": public.outcome_reason}
        db.add(EventRow(run_id=row.id, id=row.next_event, dedupe=key, type=kind, data=data or {}))
        row.next_event += 1
        await db.flush()

    async def submit(self, body: RunCreate, key: str):
        submission = body.model_dump()
        if submission.get("task") and submission["task"].get("result_contract") is None:
            # Preserve pre-contract request fingerprints when the opt-in field is absent.
            submission["task"].pop("result_contract", None)
        fingerprint = hashlib.sha256(
            body.model_dump_json(
                exclude={"general", "task", "workspace"}
                | ({"artifact_ids"} if not body.artifact_ids else set())
            ).encode()
        ).hexdigest()
        async with self.database.sessions.begin() as db:
            # One workspace: this short lock enforces global admission + idempotency atomically.
            await db.scalar(select(GateRow).where(GateRow.id == 1).with_for_update())
            existing = await db.scalar(select(RunRow).where(RunRow.key == key))
            if existing:
                if existing.config.get("general"):
                    from .project_store import digest

                    fingerprint = digest({"fingerprint_version": 3, **submission})
                elif body.task is not None or body.workspace is not None:
                    raise Problem(409, "Idempotency key reused with different input")
                if existing.fingerprint != fingerprint:
                    raise Problem(409, "Idempotency key reused with different input")
                return await self.get(existing.id)
            agent = await db.get(AgentRow, body.agent_id)
            if agent is None:
                raise Problem(404, "Agent not found")
            if agent.config.get("general"):
                from .project_store import digest

                fingerprint = digest({"fingerprint_version": 3, **submission})
            elif body.task is not None or body.workspace is not None:
                raise Problem(422, "V3 inputs require a general agent")
            registration = self.selection(AgentConfig.model_validate(agent.config))
            if await db.get(RegistrationRow, registration.identity) is None:
                db.add(RegistrationRow(id=registration.identity, config=registration.model_dump()))
                await db.flush()
            count = await db.scalar(
                select(func.count()).select_from(RunRow).where(RunRow.status.not_in(TERMINAL))
            )
            if count >= self.max_active:
                raise Problem(429, "Active run limit reached")
            if body.session_id:
                session = await db.scalar(
                    select(SessionRow).where(SessionRow.id == body.session_id).with_for_update()
                )
                if session is None:
                    raise Problem(404, "Session not found")
                active = await db.scalar(
                    select(RunRow.id).where(RunRow.session_id == session.id, RunRow.status.not_in(TERMINAL))
                )
                if active:
                    raise Problem(409, "Session already has an active run")
            else:
                session = SessionRow(id=str(uuid4()))
                db.add(session)
                await db.flush()
            requested = AgentConfig.model_validate(agent.config)
            effective, adaptive = await self.size_task(db, requested, body, registration)
            row = RunRow(
                id=str(uuid4()),
                session_id=session.id,
                agent_id=agent.id,
                key=key,
                fingerprint=fingerprint,
                config=effective.model_dump(),
                input=body.input,
                registration_id=registration.identity,
                approval_wait_seconds=self.approval_wait_seconds,
            )
            db.add(row)
            await db.flush()
            if agent.config.get("general"):
                await self.initialize_general(db, row, effective, body)
            else:
                await self.initialize_toolkit(db, row, effective, body.artifact_ids)
            await self.pin_adaptive(db, row, adaptive)
            await self.emit(db, row, "created", "run.queued")
            db.add(OutboxRow(id=f"start:{row.id}", run_id=row.id, kind="start"))
            return await self.public(db, row)

    async def get(self, run_id):
        async with self.database.sessions() as db:
            row = await db.get(RunRow, run_id)
            if row is None:
                raise Problem(404, "Run not found")
            public = await self.public(db, row)
        general = await self.general(run_id)
        if general:
            public.execution_version = 3
            public.artifact_ids = general["artifact_ids"]
            async with self.database.sessions() as db:
                public.artifacts = await self.artifact_refs_locked(db, run_id, outputs_only=True)
            public.root_run_id = general["root_id"]
            public.parent_run_id = general["parent_id"]
            public.depth = int(bool(general["parent_id"]))
            public.session_id = None if general["parent_id"] else public.session_id
            from .general_contracts import CompletionAssessment, TaskState

            public.task_state = TaskState.model_validate(general["task_state"])
            public.completion_assessment = (
                CompletionAssessment.model_validate(general["completion_assessment"])
                if general.get("completion_assessment")
                else None
            )
            public.workspace = general.get("workspace")
            public.stop_reason = general.get("stop_reason")
            public.cleanup_state = general["cleanup_state"]
            public.cleanup_detail = {
                "children": len(general["children"]),
                "state": general["cleanup_state"],
                "retry_code": "cleanup_pending" if general["cleanup_state"] == "pending" else None,
                "scope": "run-owned children and command attempts; committed projects retained",
            }
            public.specialist = general.get("role")
            public.assignment = general.get("assignment")
            public.effective_grants = {
                "tools": list(general["tools"]),
                "limits": general.get("local_limits", general["policy"]["limits"]),
                "read_prefixes": general.get("assignment", {}).get("read_prefixes", [""]),
                "write_prefixes": general.get("assignment", {}).get("write_prefixes", [""]),
            }
            return Run.model_validate(public.model_dump())
        feature = await self.toolkit(run_id)
        if feature:
            public.root_run_id = feature["root_id"]
            public.cleanup_state = feature.get("cleanup_state", "complete")
            if feature.get("execution_version") == 2 or feature["parent_id"]:
                b = await self.budget(run_id)
                public.usage = (
                    {
                        "requests": b["requests"],
                        "tool_calls": b["tool_calls"],
                        "reported_tokens": b["reported_tokens"],
                        "reserved_tokens": b["reserved_tokens"],
                    }
                    if not feature["parent_id"]
                    else feature.get("usage", {})
                )
            public.parent_run_id = feature["parent_id"]
            public.depth = int(bool(feature["parent_id"]))
            public.specialist = feature.get("specialist")
            public.artifact_ids = feature["artifact_ids"]
            from .tool_contracts import ArtifactRef

            public.artifacts = [ArtifactRef.model_validate(a) for a in feature["artifacts"]]
            from .tool_registry import digest

            public.tool_registration_ids = {k: digest(v) for k, v in feature["tools"].items()}
            if feature["parent_id"]:
                public.session_id = None
            if public.status in TERMINAL:
                from .tool_contracts import TaskResult

                public.task_result = TaskResult(
                    status=public.status,
                    summary=public.output.answer[:2000] if public.output else "",
                    evidence=feature["evidence"],
                    artifacts=feature["artifacts"],
                    error=public.error,
                    usage=public.usage,
                )
        return public

    async def events(self, run_id, cursor=0):
        async with self.database.sessions() as db:
            rows = (
                await db.scalars(
                    select(EventRow)
                    .where(EventRow.run_id == run_id, EventRow.id > cursor)
                    .order_by(EventRow.id)
                    .limit(100)
                )
            ).all()
            return [Event(**{k: getattr(r, k) for k in Event.model_fields}) for r in rows]

    async def load(self, run_id):
        async with self.database.sessions.begin() as db:
            feature = await db.get(ToolkitRunRow, run_id)
            if feature:
                root, _, row, feature = await self.tree_lock(db, run_id, active=False)
            else:
                row = await self.locked(db, run_id)
            session = await db.get(SessionRow, row.session_id)
            if row.status == "queued":
                row.status = "running"
                await self.emit(db, row, "running", "run.running")
                if feature and feature.parent_id:
                    await self.emit(
                        db, root, "child-started:" + run_id, "child.started", {"child_run_id": run_id}
                    )
            return {
                "id": row.id,
                "status": row.status,
                "config": row.config,
                "input": row.input,
                "history": []
                if (feature := await db.get(ToolkitRunRow, run_id)) and feature.parent_id
                else session.history,
                "registration_id": row.registration_id,
                "approval_wait_seconds": row.approval_wait_seconds,
                "session_id": row.session_id,
            }

    async def finish(self, run_id, status, output=None, history=None, usage=None, error=None):
        if await self.general(run_id):
            if status == "completed":
                raise Problem(409, "completion_requires_evidence_gate")
            await self.general_stop(run_id, error or "execution_stopped")
            return (await self.get(run_id)).status
        async with self.database.sessions.begin() as db:
            feature = await db.get(ToolkitRunRow, run_id)
            if feature:
                root, rf, row, feature = await self.tree_lock(db, run_id, active=False)
                if root.status in TERMINAL and root.id != run_id:
                    status, output, error = "cancelled", None, "root_closed"
            else:
                row = await self.locked(db, run_id)
            if row.status in TERMINAL:
                return row.status
            row.status, row.output, row.error, row.usage = status, output, error, usage or {}
            row.approvals = []
            feature = await db.get(ToolkitRunRow, run_id)
            if status == "completed" and not (feature and feature.parent_id):
                session = await db.scalar(
                    select(SessionRow).where(SessionRow.id == row.session_id).with_for_update()
                )
                session.history = history or []
            if feature and feature.parent_id and status != "completed":
                rf.state = {
                    **rf.state,
                    "outcome_issues": {
                        **rf.state.get("outcome_issues", {}),
                        "child:" + run_id: "child_failed",
                    },
                }
            await self.emit(db, row, "terminal", f"run.{status}", {"error": error} if error else {})
            if feature and feature.parent_id:
                rf.state = {
                    **rf.state,
                    "evidence": (rf.state["evidence"] + feature.state["evidence"])[:12],
                    "artifacts": (rf.state["artifacts"] + feature.state["artifacts"])[:8],
                }
                await self.emit(
                    db, root, "child-terminal:" + run_id, "child." + status, {"child_run_id": run_id}
                )
            if feature and not feature.parent_id and status != "completed":
                children = list(
                    await db.scalars(
                        select(RunRow)
                        .join(ToolkitRunRow, ToolkitRunRow.run_id == RunRow.id)
                        .where(ToolkitRunRow.parent_id == run_id, RunRow.status.not_in(TERMINAL))
                    )
                )
                for child in children:
                    child.status, child.approvals = "cancelled", []
                    await self.emit(db, child, "terminal", "run.cancelled")
                    db.add(OutboxRow(id=f"cancel:{child.id}", run_id=child.id, kind="cancel"))
            return status

    async def cancel(self, run_id):
        if await self.general(run_id):
            return await self.general_cancel(run_id)
        feature = await self.toolkit(run_id)
        if feature:
            run_id = feature["root_id"]
        async with self.database.sessions.begin() as db:
            if feature:
                row, _, _, _ = await self.tree_lock(db, run_id, active=False)
            else:
                row = await self.locked(db, run_id)
            targets = [row]
            if feature:
                targets += list(
                    await db.scalars(
                        select(RunRow)
                        .join(ToolkitRunRow, ToolkitRunRow.run_id == RunRow.id)
                        .where(ToolkitRunRow.parent_id == run_id)
                    )
                )
            for target in targets:
                if target.status not in TERMINAL:
                    target.status, target.approvals = "cancelled", []
                    await self.emit(db, target, "terminal", "run.cancelled")
                    db.add(OutboxRow(id=f"cancel:{target.id}", run_id=target.id, kind="cancel"))
            return await self.public(db, row)

    async def awaiting(self, run_id, approvals):
        async with self.database.sessions.begin() as db:
            feature = await db.get(ToolkitRunRow, run_id)
            if feature:
                _, _, row, feature = await self.tree_lock(db, run_id)
            else:
                row = await self.locked(db, run_id)
            if row.status in TERMINAL:
                return
            row.status, row.approvals = "awaiting_approval", approvals
            if feature and feature.parent_id:
                root = await self.locked(db, feature.root_id)
                root.status = "awaiting_approval"
                root.approvals = [
                    {**a, "id": run_id + ":" + a["id"], "origin_run_id": run_id} for a in approvals
                ]
                for approval in root.approvals:
                    await self.emit(
                        db, root, f"child-approval:{run_id}:{approval['id']}", "approval.required", approval
                    )
            if feature:
                feature.state = {
                    **feature.state,
                    "reviewed": {
                        **feature.state.get("reviewed", {}),
                        **{a["id"]: a["arguments"] for a in approvals},
                    },
                }
            for approval in approvals:
                await self.emit(db, row, f"approval:{approval['id']}", "approval.required", approval)

    async def decide(self, run_id, approval_id, approved):
        if await self.general(run_id):
            return await self.general_decide(run_id, approval_id, approved)
        async with self.database.sessions.begin() as db:
            row = await self.locked(db, run_id)
            feature = await db.get(ToolkitRunRow, run_id)
            if feature and feature.parent_id:
                raise Problem(409, "approval_requires_root")
            origin = next(
                (a.get("origin_run_id", run_id) for a in row.approvals if a["id"] == approval_id), run_id
            )
            origin_call_id = approval_id.removeprefix(origin + ":") if origin != run_id else approval_id
            old = row.decisions.get(approval_id)
            if old is not None:
                if old != approved:
                    raise Problem(409, "Approval decision is immutable")
                return await self.public(db, row)
            if row.status != "awaiting_approval" or approval_id not in {a["id"] for a in row.approvals}:
                raise Problem(409, "Approval is not pending")
            row.decisions = {**row.decisions, approval_id: approved}
            if feature and not approved:
                feature.state = {**feature.state, "denied": True}
            if origin != run_id:
                child = await self.locked(db, origin)
                child.decisions = {**child.decisions, origin_call_id: approved}
            await self.emit(
                db,
                row,
                f"decision:{approval_id}",
                "approval.decided",
                {"id": approval_id, "approved": approved},
            )
            db.add(
                OutboxRow(
                    id=f"decision:{run_id}:{approval_id}",
                    run_id=origin,
                    kind="decision",
                    payload={"id": origin_call_id, "approved": approved},
                )
            )
            return await self.public(db, row)

    async def resumed(self, run_id):
        async with self.database.sessions.begin() as db:
            feature = await db.get(ToolkitRunRow, run_id)
            if feature:
                _, _, row, feature = await self.tree_lock(db, run_id, active=False)
            else:
                row = await self.locked(db, run_id)
            if row.status not in TERMINAL:
                row.status, row.approvals = "running", []
                feature = await db.get(ToolkitRunRow, run_id)
                if feature and feature.parent_id:
                    root = await self.locked(db, feature.root_id)
                    if root.status not in TERMINAL:
                        root.status, root.approvals = "running", []

    async def tool_event(self, run_id, call_id, tool, phase):
        async with self.database.sessions.begin() as db:
            row = await self.locked(db, run_id)
            if row.status not in TERMINAL:
                await self.emit(
                    db, row, f"tool:{call_id}:{phase}", f"tool.{phase}", {"tool": tool, "call_id": call_id}
                )

    async def record_note(self, run_id, call_id, text):
        operation = f"{run_id}:{call_id}"
        async with self.database.sessions.begin() as db:
            feature = await db.get(ToolkitRunRow, run_id)
            if feature:
                root, rf, row, _ = await self.tree_lock(db, run_id)
                if rf.state.get("execution_version") == 2 and rf.state.get("denied"):
                    raise Problem(409, "tool_denied")
            else:
                row = await self.locked(db, run_id)
            existing = await db.get(NoteRow, operation)
            if existing:
                if existing.text != text:
                    raise Problem(409, "Operation arguments changed")
                return "Note recorded"
            if row.status in TERMINAL:
                raise Problem(409, "Run is terminal")
            if feature and feature.state.get("reviewed", {}).get(call_id) != {"text": text}:
                raise Problem(409, "Approval arguments changed")
            if row.decisions.get(call_id) is not True:
                raise Problem(409, "Tool approval required")
            db.add(NoteRow(operation_id=operation, run_id=run_id, text=text))
            # Effect and event commit together; retries return the same effect.
            await self.emit(
                db,
                row,
                f"note:{call_id}",
                "tool.effect_committed",
                {"call_id": call_id, "tool": "record_note"},
            )
            return "Note recorded"


def require_retry_safe_effect(*, idempotency_supported: bool, reconciler_available: bool):
    """External mutating connectors must implement one of these before registration."""
    if not (idempotency_supported or reconciler_available):
        raise ValueError("Non-idempotent external effects require reconciliation; execution refused")
