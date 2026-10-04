"""Version-two orchestration: activities perform I/O; real child workflows own contexts."""

import asyncio
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy, WorkflowIDReusePolicy
from temporalio.exceptions import ApplicationError

with workflow.unsafe.imports_passed_through():
    from .activities import await_approval, finish_run, load_run, resume_run
    from .toolkit_runtime import toolkit_child, toolkit_state, toolkit_step, toolkit_tool


async def io(fn, data):
    return await workflow.execute_activity(
        fn,
        data,
        start_to_close_timeout=timedelta(seconds=45),
        schedule_to_close_timeout=timedelta(seconds=100),
        retry_policy=RetryPolicy(maximum_attempts=2),
    )


@workflow.defn
class ToolkitWorkflow:
    def __init__(self):
        self.decisions = {}
        self.timer = None
        self.paused_at = None
        self.remaining = 0
        self.started = 0
        self.approval_remaining = 0
        self.handles = []

    @workflow.signal
    async def decision(self, data: dict):
        self.decisions.setdefault(data["id"], data["approved"])

    @workflow.signal
    async def approval_clock(self, pause: bool):
        if self.timer is None:
            return
        if pause and self.paused_at is None:
            self.remaining -= workflow.time() - self.started
            self.paused_at = workflow.time()
            self.timer.reschedule(asyncio.get_running_loop().time() + max(0, self.approval_remaining))
        elif not pause and self.paused_at is not None:
            self.approval_remaining -= workflow.time() - self.paused_at
            self.paused_at = None
            self.started = workflow.time()
            self.timer.reschedule(asyncio.get_running_loop().time() + max(0, self.remaining))

    async def approval(self, run_id, root_id, call):
        prepared = await io(toolkit_tool, {"run_id": run_id, **call, "prepare_approval": True})
        if not prepared.get("prepared"):
            return False
        state = await io(toolkit_state, root_id)
        if state["feature"].get("denied"):
            return False
        await io(
            await_approval,
            {
                "run_id": run_id,
                "approvals": [{"id": call["id"], "tool": call["tool"], "arguments": call["arguments"]}],
            },
        )
        await self.approval_clock(True)
        if root_id != run_id:
            await workflow.get_external_workflow_handle("run:" + root_id).signal("approval_clock", True)
        deadline = workflow.time() + self.approval_remaining
        try:
            await workflow.wait_condition(
                lambda: call["id"] in self.decisions,
                timeout=timedelta(seconds=max(0, self.approval_remaining)),
            )
            if workflow.time() >= deadline:
                raise TimeoutError
        finally:
            await self.approval_clock(False)
            if root_id != run_id:
                await workflow.get_external_workflow_handle("run:" + root_id).signal("approval_clock", False)
        await io(resume_run, run_id)
        return self.decisions[call["id"]]

    async def child(self, data):
        cid = await io(toolkit_child, data)
        state = await io(toolkit_state, cid)
        if state["run"]["status"] not in {"completed", "failed", "cancelled"}:
            handle = await workflow.start_child_workflow(
                ToolkitWorkflow.run,
                cid,
                id="run:" + cid,
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                parent_close_policy=workflow.ParentClosePolicy.REQUEST_CANCEL,
            )
            self.handles.append(handle)
            await handle
        result = await io(toolkit_state, cid)
        task_result = result["run"]["task_result"]
        # Explicit bounded handoff view; complete result stays available on the child API.
        return {
            "child_run_id": cid,
            "status": task_result["status"],
            "summary_excerpt": task_result["summary"][:600],
            "summary_truncated": len(task_result["summary"]) > 600,
            "evidence": [{**e, "quote": e["quote"][:160]} for e in task_result["evidence"][:4]],
            "evidence_truncated": len(task_result["evidence"]) > 4,
            "artifacts": [
                {"id": a["id"], "sha256": a["sha256"], "filename": a["filename"]}
                for a in task_result["artifacts"]
            ],
            "error": task_result["error"],
        }

    @workflow.run
    async def run(self, run_id: str):
        started = workflow.time()
        try:
            data = await io(load_run, run_id)
            state = await io(toolkit_state, run_id)
            if data["status"] in {"completed", "failed", "cancelled"}:
                return
            config = data["config"]
            self.remaining = config["timeout_seconds"] - (workflow.time() - started)
            if self.remaining <= 0:
                raise TimeoutError
            self.approval_remaining = data["approval_wait_seconds"]
            self.started = workflow.time()
            async with asyncio.timeout(self.remaining) as self.timer:
                history, results = None, None
                for _ in range(config["max_tool_calls"] + 2):
                    step = await io(toolkit_step, {"run_id": run_id, "history": history, "results": results})
                    history = step["history"]
                    if "output" in step:
                        # Parent session keeps only this turn's final answer, no specialist/tool transcripts.
                        with workflow.unsafe.imports_passed_through():
                            from pydantic_ai.messages import (
                                ModelMessagesTypeAdapter,
                                ModelRequest,
                                ModelResponse,
                                TextPart,
                                UserPromptPart,
                            )
                        final_history = ModelMessagesTypeAdapter.dump_python(
                            [
                                ModelRequest(parts=[UserPromptPart(data["input"])]),
                                ModelResponse(parts=[TextPart(step["output"]["answer"])]),
                            ],
                            mode="json",
                        )
                        await io(
                            finish_run,
                            {
                                "run_id": run_id,
                                "status": "completed",
                                "output": step["output"],
                                "history": data["history"] + final_history,
                            },
                        )
                        return
                    calls = step["calls"]
                    results = {}
                    # Only delegate calls may overlap; approval/effect calls remain serialized.
                    delegates = [c for c in calls if c["tool"] == "delegate"]
                    if delegates and config.get("delegation_mode") == "parallel_read":
                        values = await asyncio.gather(
                            *(self.child({"run_id": run_id, **c}) for c in delegates)
                        )
                        results.update({c["id"]: v for c, v in zip(delegates, values)})
                    for call in calls:
                        if call["id"] in results:
                            continue
                        if call["tool"] == "delegate":
                            results[call["id"]] = await self.child({"run_id": run_id, **call})
                        elif call["tool"] == "record_note" and not await self.approval(
                            run_id, state["feature"]["root_id"], call
                        ):
                            results[call["id"]] = {"error": "tool_denied"}
                        else:
                            results[call["id"]] = await io(toolkit_tool, {"run_id": run_id, **call})
                raise ApplicationError("tool_budget_exhausted")
        except asyncio.CancelledError:
            for handle in self.handles:
                handle.cancel()
            await asyncio.shield(io(finish_run, {"run_id": run_id, "status": "cancelled"}))
            raise
        except Exception as exc:
            for handle in self.handles:
                handle.cancel()
            error = "run_timeout" if isinstance(exc, TimeoutError) else "execution_failed"
            cause = getattr(exc, "cause", None)
            if isinstance(cause, ApplicationError) and cause.message in {
                "budget_exhausted",
                "context_limit",
                "output_limit",
                "model_execution_failed",
                "delegation_failed",
            }:
                error = cause.message
            await io(finish_run, {"run_id": run_id, "status": "failed", "error": error})
