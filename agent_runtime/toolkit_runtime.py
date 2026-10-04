"""Private activity model adapter. Public contracts contain no framework objects."""

import json

from pydantic_ai import Agent, CallDeferred, DeferredToolRequests, DeferredToolResults, Tool
from pydantic_ai.messages import (
    ModelMessagesTypeAdapter,
    ModelResponse,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.wrapper import WrapperModel
from temporalio import activity
from temporalio.exceptions import ApplicationError

from .model_adapter import build_model, request_context
from .runtime import get_store
from .schemas import Answer
from .tool_handlers import execute
from .tool_registry import ToolRegistry


async def defer(**kwargs):
    raise CallDeferred()


async def fake(messages, info):
    parts = [p for m in messages for p in m.parts]
    prompt = next((str(p.content) for p in reversed(parts) if isinstance(p, UserPromptPart)), "")
    if any(isinstance(p, ToolReturnPart) for p in parts):
        content = [p.content for p in parts if isinstance(p, ToolReturnPart)][-1]
        return ModelResponse(
            parts=[ToolCallPart(info.output_tools[0].name, {"answer": json.dumps(content)[:2000]})]
        )
    # Deterministic fixtures supply ordinary tool call arguments in task text.
    if prompt.startswith("toolkit:"):
        commands = json.loads(prompt[8:])
        return ModelResponse(
            parts=[
                ToolCallPart(c["tool"], c["arguments"], tool_call_id=c.get("id", f"call-{i}"))
                for i, c in enumerate(commands)
            ]
        )
    return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {"answer": "No matching test task"})])


class AccountedModel(WrapperModel):
    def __init__(self, model, run_id, output_limit, token_counter="utf8-v1"):
        super().__init__(model)
        self.run_id, self.output_limit = run_id, output_limit
        self.token_counter = token_counter

    async def request(self, messages, model_settings, model_request_parameters):
        encoded = ModelMessagesTypeAdapter.dump_json(messages)
        plan = await get_store().adaptive_settings(self.run_id, context_bytes=len(encoded))
        self.context_limit = plan["context_bytes"] if plan else 32000
        self.adaptive = bool(plan)
        if plan:
            self.output_limit = plan["output_tokens"]
            model_settings = {**(model_settings or {}), "max_tokens": self.output_limit}
        response = await self._request_once(messages, model_settings, model_request_parameters)
        if plan:
            truncated = response.finish_reason == "length"
            updated = await get_store().adaptive_settings(
                self.run_id, output_tokens=response.usage.output_tokens, truncated=truncated
            )
            if truncated:
                if updated["output_tokens"] > self.output_limit:
                    return await self.request(messages, model_settings, model_request_parameters)
                raise ValueError("output_limit")
        return response

    async def _request_once(self, messages, model_settings, model_request_parameters):
        encoded = ModelMessagesTypeAdapter.dump_json(messages)
        if len(encoded) > self.context_limit:
            raise ValueError("context_limit")
        # Byte count is conservative for UTF-8 tokenization; include complete tool/instruction overhead.
        # Count text/schema bytes actually supplied, excluding provider timestamps/usage/IDs
        # that are retained for replay but are not prompt content. Byte accounting is an
        # intentionally conservative tokenizer-independent ceiling for these text-only tools.
        parts = [
            {
                "instructions": getattr(m, "instructions", None),
                "parts": [
                    {
                        k: getattr(p, k)
                        for k in ("content", "args", "tool_name", "tool_call_id")
                        if hasattr(p, k)
                    }
                    for p in m.parts
                ],
            }
            for m in messages
        ]
        definitions = [
            {"name": t.name, "description": t.description, "schema": t.parameters_json_schema}
            for t in [*model_request_parameters.function_tools, *model_request_parameters.output_tools]
        ]
        tokens = (
            len(json.dumps([parts, definitions], ensure_ascii=False).encode())
            + self.output_limit
            + 256
            + 64 * len(messages)
        )
        if self.adaptive and self.token_counter != "utf8-v1":
            from .context import reserve_tokens

            tokens = reserve_tokens(
                json.dumps([parts, definitions], ensure_ascii=False).encode(),
                self.output_limit,
                self.token_counter,
            )
        await get_store().reserve_usage(self.run_id, "requests", tokens)
        feature = await get_store().toolkit(self.run_id)
        root = await get_store().get(feature["root_id"])
        token = request_context.set({"run_id": self.run_id, "root_id": root.id, "scenario": root.config.name})
        try:
            response = await self.wrapped.request(messages, model_settings, model_request_parameters)
        finally:
            request_context.reset(token)
        await get_store().reconcile_usage(self.run_id, tokens, response.usage.total_tokens)
        return response


@activity.defn
async def toolkit_step(data: dict):
    try:
        store = get_store()
        run = await store.load(data["run_id"])
        feature = await store.toolkit(data["run_id"])
        registration = await store.registration(run["registration_id"])
        model = FunctionModel(fake) if registration.adapter == "fake" else build_model(registration)
        tools = []
        for entry in feature["tools"].values():
            ToolRegistry.validate_snapshot(entry)
            tools.append(
                Tool.from_schema(
                    defer,
                    name=entry["alias"],
                    description=entry["description"],
                    json_schema=entry["arguments_schema"],
                )
            )
        config = run["config"]
        instruction = (
            config["instructions"] + "\nAvailable input artifact IDs: " + json.dumps(feature["artifact_ids"])
        )
        if feature["specialists"]:
            instruction += "\nSpecialists: " + json.dumps(
                {n: s["description"] for n, s in feature["specialists"].items()}
            )
        agent = Agent(
            AccountedModel(model, run["id"], config["max_tokens"], registration.token_counter),
            tools=tools,
            output_type=Answer | DeferredToolRequests,
            retries=1,
            instructions=instruction,
        )
        history = ModelMessagesTypeAdapter.validate_python(data.get("history") or run["history"])
        async with agent:
            result = await agent.run(
                None if data.get("results") is not None else run["input"],
                message_history=history,
                deferred_tool_results=DeferredToolResults(calls=data["results"])
                if data.get("results") is not None
                else None,
                model_settings={"max_tokens": config["max_tokens"], "timeout": 30},
            )
        serialized = ModelMessagesTypeAdapter.dump_python(result.all_messages(), mode="json")
        if len(json.dumps(serialized).encode()) > (
            feature.get("adaptive", {}).get("max_context_bytes", 32000) * 3 // 2
        ):
            raise ValueError("context_limit")
        if isinstance(result.output, DeferredToolRequests):
            return {
                "history": serialized,
                "calls": [
                    {"id": c.tool_call_id, "tool": c.tool_name, "arguments": c.args_as_dict()}
                    for c in result.output.calls
                ],
            }
        return {"output": result.output.model_dump(), "history": serialized}
    except Exception as exc:
        # Sanitize at the producer, before Temporal serializes an exception/cause.
        code = getattr(exc, "detail", "")
        if code not in {"budget_exhausted", "run_terminal"}:
            code = (
                str(exc)
                if isinstance(exc, ValueError) and str(exc) in {"context_limit", "output_limit"}
                else "model_execution_failed"
            )
        raise ApplicationError(code, non_retryable=code != "model_execution_failed") from None


@activity.defn
async def toolkit_tool(data: dict):
    try:
        if data.get("prepare_approval"):
            store = get_store()
            from .tool_registry import Note

            args = Note.model_validate(data["arguments"]).model_dump()
            key = data["id"] + ":approval"
            old = await store.operation_result(data["run_id"], key, args)
            if old is not None:
                return old
            await store.reserve_usage(data["run_id"], "tool_calls")
            return await store.save_operation(data["run_id"], key, {"prepared": True}, "record_note")
        return await execute(get_store(), data["run_id"], data["id"], data["tool"], data["arguments"])
    except Exception as exc:
        code = getattr(exc, "detail", "")
        allowed = {
            "budget_exhausted",
            "run_terminal",
            "artifact_not_authorized",
            "operation_arguments_changed",
            "sandbox_unavailable",
            "sandbox_timeout",
            "sandbox_execution_failed",
            "sandbox_output_limit",
            "sandbox_interrupted",
            "context_limit",
        }
        if not code and isinstance(exc, ValueError) and str(exc) in allowed:
            code = str(exc)
        code = code if code in allowed else "tool_execution_failed"
        await get_store().record_tool_failure(data["run_id"], data["id"], code)
        return {"error": code}


@activity.defn
async def toolkit_child(data: dict):
    from .tool_contracts import TaskSpec

    try:
        task = TaskSpec.model_validate(data["arguments"])
        store = get_store()
        # Exact replay of a committed intent does not charge a second delegation.
        existing = await store.operation_result(
            data["run_id"], data["id"], {"tool": "delegate", "arguments": task.model_dump()}
        )
        if existing:
            return existing["child_id"]
        await store.reserve_usage(data["run_id"], "tool_calls")
        cid = await store.create_child(data["run_id"], data["id"], task.model_dump())
        await store.save_operation(data["run_id"], data["id"], {"child_id": cid})
        return cid
    except Exception:
        raise ApplicationError("delegation_failed", non_retryable=True) from None


@activity.defn
async def toolkit_state(run_id: str):
    run = await get_store().get(run_id)
    return {"run": run.model_dump(mode="json"), "feature": await get_store().toolkit(run_id)}
