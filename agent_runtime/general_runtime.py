"""Private v3 model and execution activities. Workflow inputs remain owned DTOs."""

import asyncio
import copy
import json
import time
from uuid import uuid4

from pydantic import BaseModel, ValidationError
from pydantic_ai import Agent, ToolOutput
from pydantic_ai.messages import ModelMessagesTypeAdapter
from pydantic_ai.models.wrapper import WrapperModel
from temporalio import activity
from temporalio.exceptions import ApplicationError

from .general_completion import general_completion
from .general_contracts import Criterion, PlanStep, StepDecision
from .general_db import GeneralAttemptRow, GeneralOperationRow
from .model_adapter import build_model, request_context
from .project_store import digest, fail
from .resources import ResourceBlocked, check, not_dispatched, reserve_for, settle_model_attempt
from .resources import policy as resource_policy
from .runtime import get_store


class DecisionEnvelope(BaseModel):
    """Legacy private envelope retained for unpatched v3 histories."""

    action: dict
    goal_additions: list[Criterion] = []
    plan: list[PlanStep] | None = None


class GeneralModel(WrapperModel):
    def __init__(self, model, run_id, op_id, output, token_counter="utf8-v1"):
        super().__init__(model)
        self.run_id, self.op_id, self.output = run_id, op_id, output
        self.token_counter = token_counter
        self.reject_multiple = False
        self.semantic_type = None

    async def request(self, messages, model_settings, model_request_parameters):
        if self.reject_multiple:
            from dataclasses import replace

            from .general_semantic import compact_schema

            model_request_parameters = replace(
                model_request_parameters,
                output_tools=[
                    replace(
                        t,
                        parameters_json_schema=compact_schema(
                            t.parameters_json_schema, minimal=getattr(self, "semantic_version", 0) >= 3
                        ),
                    )
                    for t in model_request_parameters.output_tools
                ],
            )
        encoded = ModelMessagesTypeAdapter.dump_json(messages)
        definitions = [t.parameters_json_schema for t in model_request_parameters.output_tools]
        context_bytes = len(encoded) + len(json.dumps(definitions, separators=(",", ":")).encode())
        store = get_store()
        async with store.database.sessions.begin() as db:
            op = await db.get(GeneralOperationRow, self.op_id)
            op.data = {
                **op.data,
                "context_bytes": context_bytes,
                "schema_bytes": len(json.dumps(definitions, separators=(",", ":")).encode()),
                "message_bytes": len(encoded),
                "output_reservation": self.output,
                "required_reservation": context_bytes + self.output + 512,
            }
        if context_bytes > 24576:
            fail("context_limit", 409)
        from .context import reserve_tokens

        token_reservation = reserve_tokens(
            encoded + json.dumps(definitions, separators=(",", ":")).encode(), self.output, self.token_counter
        )
        async with store.database.sessions.begin() as db:
            op = await db.get(GeneralOperationRow, self.op_id)
            op.data = {
                **op.data,
                "required_reservation": token_reservation,
                "token_counter": self.token_counter,
            }
        attempt_id = self.op_id + ":" + str(uuid4())

        async def reserve_attempt():
            async with store.database.sessions.begin() as db:
                row, gr, root = await store.general_lock(db, self.run_id)
                op = await db.get(GeneralOperationRow, self.op_id)
                await store.general_charge_locked(db, gr, root, "model_attempts")
                data = copy.deepcopy(root.data)
                budget = data["budget"]
                if resource_policy(root.data):
                    check(
                        root,
                        gr,
                        "total_tokens",
                        token_reservation,
                        reserve_for(root.data, "total_tokens") if gr.parent_id else 0,
                    )
                if not resource_policy(root.data) and budget["reported_tokens"] + budget[
                    "reserved_tokens"
                ] + token_reservation > data["policy"]["limits"]["total_tokens"] - (
                    self.output + 2560 if gr.parent_id else 0
                ):
                    if (
                        op.data.get("binding")
                        and budget["reserved_tokens"]
                        and budget["reported_tokens"] + token_reservation
                        <= data["policy"]["limits"]["total_tokens"]
                        - (self.output + 2560 if gr.parent_id else 0)
                    ):
                        fail("model_capacity_pending", 409)
                    fail("budget_exhausted", 409)
                if gr.parent_id:
                    local = copy.deepcopy(gr.data)
                    if not resource_policy(root.data) and (
                        local.get("reported_tokens", 0) + local.get("reserved_tokens", 0) + token_reservation
                        > local["local_limits"]["total_tokens"]
                    ):
                        fail("budget_exhausted", 409)
                    local["reserved_tokens"] = local.get("reserved_tokens", 0) + token_reservation
                    gr.data = local
                budget["reserved_tokens"] += token_reservation
                root.data = data
                ordinal = op.data.get("attempts", 0) + 1
                op.data = {
                    **op.data,
                    "attempts": ordinal,
                    "context_bytes": context_bytes,
                    "schema_bytes": len(json.dumps(definitions, separators=(",", ":")).encode()),
                }
                db.add(
                    GeneralAttemptRow(
                        id=attempt_id,
                        operation_id=self.op_id,
                        ordinal=ordinal,
                        data={"reserved_tokens": token_reservation, "settled": False},
                    )
                )
                scenario = (
                    await db.get(__import__("agent_runtime.db", fromlist=["RunRow"]).RunRow, root.run_id)
                ).config["name"]
            return scenario, root.run_id

        deadline = time.monotonic() + 20
        while True:
            try:
                scenario, root_id = await reserve_attempt()
                break
            except Exception as exc:
                if getattr(exc, "detail", None) != "model_capacity_pending" or time.monotonic() >= deadline:
                    raise
                await asyncio.sleep(0.2)
        context = request_context.set(
            {
                "run_id": self.run_id,
                "root_id": root_id,
                "scenario": scenario,
                "operation_id": self.op_id,
                "attempt_id": attempt_id,
            }
        )
        try:
            response = await self.wrapped.request(messages, model_settings, model_request_parameters)
        except BaseException as exc:
            refused = not_dispatched(exc)
            await settle_model_attempt(store, self.run_id, attempt_id, refused=refused)
            if refused is not None:
                raise refused from None
            raise
        finally:
            request_context.reset(context)
        await settle_model_attempt(store, self.run_id, attempt_id, tokens=response.usage.total_tokens)

        # Retain only schema-known field names and types, never arbitrary provider keys.
        known_fields = set()

        def collect_fields(value):
            if isinstance(value, dict):
                known_fields.update(value.get("properties", {}))
                for child in value.values():
                    collect_fields(child)
            elif isinstance(value, list):
                for child in value:
                    collect_fields(child)

        collect_fields(definitions)

        # Retain only structural diagnostics, never provider text or raw exceptions.
        def shape(value, depth=0):
            if depth > 7:
                return type(value).__name__
            if isinstance(value, dict):
                return {
                    (k if k in known_fields else "unknown_field"): shape(v, depth + 1)
                    for k, v in list(value.items())[:24]
                }
            if isinstance(value, list):
                return [shape(v, depth + 1) for v in value[:3]]
            return type(value).__name__

        structures = []
        for part in response.parts:
            if hasattr(part, "args"):
                args = part.args
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except ValueError:
                        args = None
                structures.append(shape(args))
        async with store.database.sessions.begin() as db:
            op = await db.get(GeneralOperationRow, self.op_id)
            op.data = {**op.data, "response_shape": structures[:2]}
        if self.reject_multiple:
            names = {t.name for t in model_request_parameters.output_tools}
            calls = [p for p in response.parts if hasattr(p, "tool_name")]
            if len(calls) > 1:
                async with store.database.sessions.begin() as db:
                    op = await db.get(GeneralOperationRow, self.op_id)
                    op.data = {
                        **op.data,
                        "validation_feedback": "Multiple output actions rejected before effects. Return exactly one action; none of these actions executed.",
                        "output_calls": len(calls),
                    }
                fail("multiple_semantic_outputs", 409)
            errors = []
            if len(calls) != 1 or calls[0].tool_name not in names:
                errors = [{"type": "single_output_required", "loc": ["action"]}]
            else:
                try:
                    args = calls[0].args
                    if isinstance(args, str):
                        self.semantic_type.model_validate_json(args)
                    else:
                        self.semantic_type.model_validate(args)
                except ValidationError as exc:
                    errors = [
                        {
                            "type": e["type"],
                            "loc": [
                                part if isinstance(part, int) or part in known_fields else "schema_variant"
                                for part in e["loc"]
                            ],
                        }
                        for e in exc.errors(include_input=False, include_context=False)[:16]
                    ]
            if errors:
                feedback = (
                    "Invalid semantic action rejected before effects. Return exactly one action matching "
                    "the output schema. Structural errors: " + json.dumps(errors, separators=(",", ":"))
                )
                async with store.database.sessions.begin() as db:
                    op = await db.get(GeneralOperationRow, self.op_id)
                    op.data = {**op.data, "validation_errors": errors, "validation_feedback": feedback}
                fail("invalid_semantic_output", 409)
        return response


def fake_decision(prompt, state, verifications):
    """Test-only scripted decisions, using the same public action contracts."""
    task = state["task_state"]
    if prompt.startswith("general:"):
        script = json.loads(prompt[8:])
        if state["step"] < len(script):
            replacements = {"$HEAD": task["head"] or "", "$BRANCH": task["branch_id"] or ""}
            for i, (cid, child) in enumerate(state["children"].items()):
                replacements["$CHILDHEAD" + str(i)] = child.get("head", "")
                replacements["$CHILD" + str(i)] = cid

            def render(value):
                if isinstance(value, str):
                    return replacements.get(value, value)
                if isinstance(value, list):
                    return [render(v) for v in value]
                if isinstance(value, dict):
                    return {k: v if k == "objective" else render(v) for k, v in value.items()}
                return value

            decision = render(script[state["step"]])
            if decision.get("action", {}).get("kind") != "complete":
                return StepDecision.model_validate(decision)
    dispositions = [
        {
            "criterion_id": c["id"],
            "disposition": "satisfied",
            "assessment": "Scoped model assessment; no universal correctness claim.",
            "evidence_ids": [v["id"] for v in verifications if v["criterion_id"] == c["id"]],
        }
        for c in state["goal"]["criteria"]
    ]
    return StepDecision.model_validate(
        {
            "action": {
                "kind": "complete",
                "answer": "Completed the requested scoped task.",
                "assessment": {
                    "proposal_id": "proposal-" + str(state["step"]),
                    "state_version": task["version"],
                    "goal_version": state["goal"]["version"],
                    "revision_id": task["head"],
                    "criteria": dispositions,
                    "limitations": ["Model assessments are not verified facts."],
                },
            }
        }
    )


@activity.defn
async def general_step(run_id: str | dict):
    semantic = isinstance(run_id, dict)
    loop_version = run_id.get("completion_loop", 1) if semantic else 1
    if semantic:
        run_id = run_id["run_id"]
    store = get_store()
    semantic_assessments = []
    try:
        async with store.database.sessions.begin() as db:
            row, gr, root = await store.general_lock(db, run_id)
            if semantic:
                gr.data = {**gr.data, "completion_loop_version": loop_version}
            if gr.data["stagnation"] >= 5:
                fail("no_progress", 409)
            if root.data.get("active_started") is None:
                root.data = {**root.data, "active_started": time.time()}
            if gr.parent_id and gr.data.get("active_started") is None:
                gr.data = {
                    **gr.data,
                    "active_started": time.time(),
                    "pause_baseline": root.data.get("paused_seconds", 0),
                }
            report_only = (
                not resource_policy(root.data)
                and not gr.parent_id
                and bool(gr.data["tools"])
                and root.data["budget"]["model_attempts"]
                >= root.data["policy"]["limits"]["model_attempts"]
                - (2 if root.data["policy"].get("review") else 1)
            )
            state = copy.deepcopy(gr.data)
            prior = (
                await db.get(GeneralOperationRow, state["current_operation"])
                if state.get("current_operation")
                else None
            )
            last_action = (prior.data.get("decision") or {}).get("action") if prior else None
            if last_action and len(json.dumps(last_action).encode()) > 2048:
                last_action = {
                    "kind": last_action["kind"],
                    "capability": last_action.get("capability"),
                    "operation_id": prior.id,
                    "arguments_omitted": True,
                }
            remaining_budget = {
                "model_attempts": root.data["policy"]["limits"]["model_attempts"]
                - root.data["budget"]["model_attempts"],
                "tool_attempts": root.data["policy"]["limits"]["tool_attempts"]
                - root.data["budget"]["tool_attempts"],
                "includes_final_reporting_reserve": True,
            }
            state["report_only"] = report_only
            step = state["step"]
            op_id = f"{run_id}:model:{step}"
            old = await db.get(GeneralOperationRow, op_id)
            if old and old.data.get("rejected"):
                return {"step": step, "rejected": True}
            if old and old.data.get("result"):
                return {
                    "step": step,
                    "decision": old.data["result"],
                    **({"semantic": True} if old.data.get("semantic_result") else {}),
                }
            if old and old.data.get("lease", 0) > time.time():
                fail("model_lease_pending", 409)
            lease = str(uuid4())
            if old is None:
                old = GeneralOperationRow(
                    id=op_id, run_id=run_id, fingerprint=digest({"step": step}), data={"sequence": step * 2}
                )
                db.add(old)
            if semantic and old.data.get("resource_retry"):
                refreshed = {
                    k: v
                    for k, v in old.data.items()
                    if k not in {"binding", "semantic_context", "resource_retry"}
                }
                old.data = refreshed
            if semantic and not old.data.get("binding"):
                from .general_semantic import capture

                binding = await capture(
                    store, db, gr, root, op_id, version=3 if loop_version >= 2 else loop_version
                )
                old.data = {**old.data, "binding": binding}
            binding = old.data.get("binding")
            old.data = {**old.data, "lease": time.time() + 60, "owner": lease, "status": "pending"}
            if row.status == "queued":
                row.status = "running"
                await store.emit(db, row, "running", "run.running")
            config, prompt, registration_id = row.config, row.input, row.registration_id
            from .db import SessionRow

            session = await db.get(SessionRow, row.session_id)
            context_policy = store.context_policies.get(
                state["policy"].get("context_policy", "bounded-v3"), state.get("context_policy_version", 1)
            )
            history = session.history
            if not gr.parent_id and context_policy.name == "memory-v1":
                from .context import session_messages

                history = await session_messages(db, row, prompt)
            previous_turns = [] if gr.parent_id else context_policy.history(history, prompt)
        verifications = (await store.general_records(run_id, "verification", limit=128))["items"]
        for cid in state["children"]:
            child = await store.general(cid)
            state["children"][cid]["head"] = child["task_state"]["head"]
        registration = await store.registration(registration_id)
        if registration.adapter == "fake" and not (semantic and prompt.startswith("semantic:")):
            async with store.database.sessions.begin() as db:
                _, gr, root = await store.general_lock(db, run_id)
                await store.general_charge_locked(db, gr, root, "model_attempts")
                db.add(
                    GeneralAttemptRow(
                        id=op_id + ":fake",
                        operation_id=op_id,
                        ordinal=1,
                        data={"kind": "model", "fake": True, "settled": True, "reported_tokens": 0},
                    )
                )
            decision = fake_decision(prompt, state, verifications)
        elif semantic:
            from .general_semantic import compile_decision, instructions, project_context, wire_type

            context = context_policy.compact(
                project_context(binding, prompt, previous_turns, report_only), 14000
            )
            async with store.database.sessions.begin() as db:
                op = await db.get(GeneralOperationRow, op_id)
                if "semantic_context" not in op.data:
                    op.data = {**op.data, "semantic_context": context}
                context = op.data["semantic_context"]
            report_only = context["report_only"]
            encoded = json.dumps(context, separators=(",", ":"), ensure_ascii=False)
            if len(encoded.encode()) > 16000:
                fail("context_limit", 409)
            model = GeneralModel(
                build_model(registration), run_id, op_id, config["max_tokens"], registration.token_counter
            )
            model.reject_multiple = binding["version"] >= 2
            model.semantic_type = wire_type(binding)
            model.semantic_version = binding["version"]
            agent = Agent(
                model,
                output_type=(
                    ToolOutput(
                        model.semantic_type,
                        name="next_action",
                        description="Execute one action; return its observation. complete runs checks before acceptance.",
                    )
                    if binding["version"] >= 3
                    else model.semantic_type
                ),
                retries=0,
                instructions=config["instructions"] + instructions(binding["version"]),
            )
            async with agent:
                result = await agent.run(
                    encoded,
                    model_settings={
                        "max_tokens": config["max_tokens"],
                        "timeout": 30,
                        **({"parallel_tool_calls": False} if binding["version"] >= 3 else {}),
                    },
                )
            decision = compile_decision(result.output, binding)
            if result.output.action.kind == "complete":
                semantic_assessments = [a.model_dump() for a in result.output.action.assessments]
        else:
            # Full evidence remains queryable; the prompt carries bounded projections only.
            context = {
                "input": prompt,
                "report_only": report_only,
                "previous_turns": previous_turns,
                "goal": state["goal"],
                "state": state["task_state"],
                "last_action": last_action,
                "last_result": state["last_result"],
                "remaining_budget": remaining_budget,
                "capabilities": [
                    {k: state["tools"][a][k] for k in ("alias", "description", "arguments_schema", "effect")}
                    for a in state.get("loaded", list(state["tools"])[:4])
                ],
                "verification_ids": [
                    {k: v[k] for k in ("id", "criterion_id", "outcome", "revision_id", "fresh")}
                    for v in verifications[-12:]
                ],
                "children": state["children"],
                "delegation": state["policy"].get("delegation"),
            }
            encoded = json.dumps(context, separators=(",", ":"), ensure_ascii=False)
            if len(encoded.encode()) > 16000:
                fail("context_limit", 409)
            model = GeneralModel(
                build_model(registration), run_id, op_id, config["max_tokens"], registration.token_counter
            )
            agent = Agent(
                model,
                output_type=DecisionEnvelope,
                retries=0,
                instructions=config["instructions"]
                + """\nIf report_only is true, return complete or blocked; the final model attempt is reserved for reporting. Choose one action object: invoke{capability,arguments}, discover{query}, assign{assignments:[{role,objective,criteria,tools,base_revision,read_prefixes,write_prefixes}]}, join{child_ids}, merge{child_id,base_revision,source_revision,expected_revision}, blocked{reason}, or complete{answer,assessment:{proposal_id,state_version,goal_version,revision_id,criteria:[{criterion_id,disposition:satisfied|unsatisfied|inconclusive,assessment,evidence_ids}],limitations}}. Each action includes kind. Completion must cite current state/goal/head and genuine verification IDs. Never invent evidence. Discover capabilities when needed. workspace_write arguments: expected_revision,writes:[{path,expected_sha256:null or digest,content_base64 or delete:true}]. workspace_verify: expected_revision,check_id. workspace_read: path. workspace_command: expected_revision,argv,cwd,commit. All code runs offline Python/pytest. A changed project requires runtime.syntax verification. Existing required checks cannot be weakened. Model assessment supports assessment criteria only. Use JSON null (never the string null) for a missing revision/head. Copy state.version and state.goal_version exactly into completion. Do not add source evidence fields criterion_id or quote to an ordinary workspace_read; source fields are only for explicit source-policy criteria and exact quotes. Assignment criteria must be objects {id,statement}; limits is an optional object. Never create criterion/check IDs starting runtime.; runtime checks are added automatically after changes. Every assignment write_prefix must also be covered by its read_prefixes, including newly created files. For a single-file child, set both read_prefixes and write_prefixes to that file path. Keep optional plan and goal_additions omitted unless needed; be concise. last_action has already executed with last_result: do not repeat successful work. workspace_command never creates verification receipts; use workspace_verify for registered checks. Allocate the remaining attempts to required checks and a final completion. Use check_id runtime.syntax only with workspace_verify, never by manually running a syntax command. All action keys listed here are exact; put capability arguments only inside arguments. Example command: {"kind":"invoke","capability":"workspace_command","arguments":{"expected_revision":"COPY_STATE_HEAD","argv":["python","-c","print(1)"],"commit":false}}. Example completion: {"kind":"complete","answer":"12","assessment":{"proposal_id":"answer-1","state_version":1,"goal_version":1,"revision_id":null,"criteria":[{"criterion_id":"COPY_GOAL_CRITERION_ID","disposition":"satisfied","assessment":"Direct calculation","evidence_ids":[]}]}}.""",
            )
            async with agent:
                result = await agent.run(
                    encoded,
                    model_settings={
                        "max_tokens": config["max_tokens"],
                        "timeout": 30,
                        **({"parallel_tool_calls": False} if binding["version"] >= 3 else {}),
                    },
                )
            try:
                decision = StepDecision.model_validate(result.output.model_dump())
            except ValidationError as exc:
                async with store.database.sessions.begin() as db:
                    op = await db.get(GeneralOperationRow, op_id)
                    op.data = {
                        **op.data,
                        "validation_errors": [
                            {"type": e["type"], "loc": list(e["loc"])}
                            for e in exc.errors(include_input=False, include_context=False)[:16]
                        ],
                    }
                fail("invalid_model_decision", 409)
        if report_only and decision.action.kind not in {"complete", "blocked"}:
            decision = StepDecision.model_validate(
                {
                    "action": {
                        "kind": "blocked",
                        "reason": "Final reporting reserve reached; work remains incomplete.",
                    }
                }
            )
        encoded_decision = decision.model_dump()
        async with store.database.sessions.begin() as db:
            row, gr, _ = await store.general_lock(db, run_id, admission=False)
            op = await db.get(GeneralOperationRow, op_id)
            if op.data["owner"] != lease:
                fail("model_lease_fenced", 409)
            op.data = {
                **op.data,
                "result": encoded_decision,
                "status": "complete",
                "semantic_assessments": semantic_assessments,
                "semantic_result": semantic
                and not (registration.adapter == "fake" and not prompt.startswith("semantic:")),
            }
            await store.general_record(db, gr, "decision", op_id + ":decision", encoded_decision)
        return {
            "step": step,
            "decision": encoded_decision,
            **({"semantic": True} if op.data.get("semantic_result") else {}),
        }
    except Exception as exc:
        if "op_id" in locals():
            async with store.database.sessions.begin() as db:
                op = await db.get(GeneralOperationRow, op_id)
                if op:
                    op.data = {
                        **op.data,
                        "failure_type": type(exc).__name__,
                        "status": "failed",
                        "lease": 0,
                        **({"resource_retry": True} if isinstance(exc, ResourceBlocked) else {}),
                    }
        code = getattr(exc, "detail", "model_execution_failed")
        if code in {"multiple_semantic_outputs", "invalid_semantic_output"} and loop_version >= 2:
            async with store.database.sessions.begin() as db:
                row, gr, _ = await store.general_lock(db, run_id)
                op = await db.get(GeneralOperationRow, op_id)
                if not op.data.get("rejected"):
                    op.data = {**op.data, "rejected": True}
                    await store.general_checkpoint(
                        db, row, gr, op_id, {"error": code, "feedback": op.data["validation_feedback"]}
                    )
            return {"step": step, "rejected": True}

        if isinstance(exc, ResourceBlocked):
            raise ApplicationError(code, exc.snapshot, non_retryable=True) from None
        raise ApplicationError(
            code,
            non_retryable=code
            not in {"model_lease_pending", "model_execution_failed", "model_capacity_pending"},
        ) from None


@activity.defn
async def general_action(data: dict):
    store = get_store()
    try:
        if data["decision"]["action"]["kind"] == "complete":
            from .completion_review import ensure_review

            await ensure_review(store, data["run_id"], data["decision"]["action"])
        result = await store.general_operation(data["run_id"], data["step"], data["decision"])
        if result.get("external"):
            from .extension_runtime import execute_extension

            result = await execute_extension(store, data["run_id"], result["operation_id"])
            if result.get("reconcile"):
                fail("extension_reconcile_pending", 409)
        return result
    except Exception as exc:
        code = getattr(exc, "detail", "action_failed")
        if isinstance(exc, ResourceBlocked):
            raise ApplicationError(code, exc.snapshot, non_retryable=True) from None
        if code == "model_not_dispatched":
            raise ApplicationError(code, non_retryable=True) from None
        if code in {"extension_lease_pending", "review_lease_pending", "extension_reconcile_pending"}:
            raise ApplicationError(code) from None
        # A rejected decision is observable and consumes progress, not a workflow crash.
        async with store.database.sessions.begin() as db:
            row, gr, _ = await store.general_lock(db, data["run_id"], admission=False)
            op_id = f"{row.id}:action:{data['step']}"
            old = await db.get(GeneralOperationRow, op_id)
            if old and old.data.get("result"):
                return old.data["result"]
            if old and old.data.get("external") and old.data.get("started"):
                alias = old.data["decision"]["action"]["capability"]
                if gr.data["tools"][alias]["effect"]["kind"] == "external-write":
                    # A lost receipt after dispatch is not proof that the write failed.
                    # In particular, exhausting reconciliation must never clear the intent.
                    old.data = {**old.data, "status": "outcome_unknown", "lease": 0}
                    result = {"error": "extension_outcome_unknown", "operation_id": op_id}
                    from .general_db import GeneralRecordRow

                    if not await db.get(GeneralRecordRow, op_id + ":checkpoint"):
                        await store.general_checkpoint(db, row, gr, op_id, result)
                    return result
            result = {"error": code}
            if old is None:
                old = GeneralOperationRow(
                    id=op_id,
                    run_id=row.id,
                    fingerprint=digest(data["decision"]),
                    data={"sequence": data["step"] * 2 + 1},
                )
                db.add(old)
            old.data = {**old.data, "decision": data["decision"], "result": result, "status": "complete"}
            await store.general_checkpoint(db, row, gr, op_id, result)
            return result


@activity.defn
async def general_command(data: dict):
    store = get_store()
    state = await store.general(data["run_id"])
    executor = store.executors.get(state["policy"]["workspace_policy"], state.get("executor_version", 1))
    project_request = executor.request
    try:
        identity, payload = await store.general_command_payload(
            data["run_id"], data["operation_id"], with_identity=True
        )
        bundle = await project_request(identity, payload)
        if bundle.get("error") == "sandbox_interrupted":
            if await store.general_command_retry(data["run_id"], data["operation_id"], identity):
                await project_request(identity, acknowledge=True)
                identity, payload = await store.general_command_payload(
                    data["run_id"], data["operation_id"], with_identity=True
                )
                bundle = await project_request(identity, payload)
        result = await store.general_command_commit(data["run_id"], data["operation_id"], bundle, identity)
        for ordinal in range(1, int(identity.rsplit(":", 1)[-1]) + 1):
            await project_request(data["operation_id"] + ":command:" + str(ordinal), acknowledge=True)
        async with store.database.sessions.begin() as db:
            row, gr, _ = await store.general_lock(db, data["run_id"], active=False)
            op = await db.get(GeneralOperationRow, data["operation_id"])
            op.data = {**op.data, "acknowledged": True}
            gr.data = {**gr.data, "cleanup_state": "pending"}
            has_children = bool(gr.data["children"])
        if not has_children:
            await store.general_cleanup(data["run_id"])
        return result
    except Exception as exc:
        if isinstance(exc, ResourceBlocked):
            raise ApplicationError(exc.detail, exc.snapshot, non_retryable=True) from None
        raise ApplicationError(getattr(exc, "detail", "sandbox_unavailable")) from None


@activity.defn
async def general_state(run_id: str):
    run = await get_store().get(run_id)
    state = await get_store().general(run_id)
    root = await get_store().general(state["root_id"])
    if resource_policy(root) and run.status not in {"completed", "failed", "cancelled"}:
        try:
            async with get_store().database.sessions.begin() as db:
                await get_store().general_lock(db, run_id)
        except ResourceBlocked as exc:
            raise ApplicationError(exc.detail, exc.snapshot, non_retryable=True) from None
    return {
        "active_remaining": max(0, get_store().general_time_remaining(root)),
        "status": run.status,
        "policy": state["policy"],
        "approval_wait_seconds": max(0, get_store().approval_wait_seconds - root.get("approval_seconds", 0)),
        "parent_id": state["parent_id"],
    }


@activity.defn
async def general_stop(data: dict):
    await get_store().general_stop(data["run_id"], data["code"])


@activity.defn
async def general_resource_pause(data: dict):
    return await get_store().resource_pause(data["run_id"], data["block"])


GENERAL_ACTIVITIES = [
    general_resource_pause,
    general_step,
    general_action,
    general_command,
    general_state,
    general_stop,
    general_completion,
]
