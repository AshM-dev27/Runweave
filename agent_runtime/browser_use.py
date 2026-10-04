"""Optional hosted V4 browser agent; no local browser or SDK types in public contracts."""

import json
import os
import time
from decimal import Decimal
from uuid import UUID

from jsonschema import Draft202012Validator
from pydantic import Field, model_validator

from .extensions import DeferredToolResult
from .general_contracts import Contract
from .result_contracts import bounded_json, validate_schema

DEFAULT_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string", "minLength": 1, "maxLength": 8000},
        "sources": {"type": "array", "items": {"type": "string", "maxLength": 2048}, "maxItems": 20},
    },
    "required": ["summary", "sources"],
    "additionalProperties": False,
}
ARGUMENTS_SCHEMA = {
    "type": "object",
    "properties": {"task": {"type": "string", "minLength": 1, "maxLength": 8000}},
    "required": ["task"],
    "additionalProperties": False,
}
TERMINAL = {"completed", "failed", "cancelled"}


class BrowserUseConfig(Contract):
    credential_env: str = "BROWSER_USE_API_KEY"
    max_cost_usd: float = Field(default=1.0, gt=0, le=1, allow_inf_nan=False)
    timeout_seconds: int = Field(default=300, ge=10, le=600)
    model: str | None = Field(default=None, min_length=1, max_length=100)
    model_params: dict | None = None
    output_schema: dict = Field(default_factory=lambda: DEFAULT_OUTPUT_SCHEMA.copy())

    @model_validator(mode="after")
    def valid(self):
        if self.credential_env != "BROWSER_USE_API_KEY":
            raise ValueError("Browser Use requires the server-side BROWSER_USE_API_KEY")
        validate_schema(self.output_schema)
        if self.model_params is not None:
            bounded_json(self.model_params, max_nodes=128)
        return self


def client_for(config):
    from browser_use_sdk.v4 import AsyncBrowserUse

    client = AsyncBrowserUse(api_key=os.environ[config.credential_env], timeout=5)
    # SDK 3.11.3 exposes no public retry option. Pin and test this adapter seam:
    # even a throttled create is issued once; no implicit paid dispatch retries.
    client._http._max_retries = 0
    return client


class BrowserUseHandler:
    version = 1

    async def execute(self, call):
        config = BrowserUseConfig.model_validate(call.definition["config"])
        if call.state or call.save_state is None:
            return None  # An earlier create may have reached the provider.
        if not os.environ.get(config.credential_env):
            return {"error": "browser_use_not_configured"}
        if not call.arguments["task"].strip():
            return {"error": "browser_task_empty"}
        duration = min(config.timeout_seconds, call.remaining_seconds or config.timeout_seconds)
        state = {"phase": "creating", "deadline": time.time() + duration}
        await call.save_state(state)  # Durable intent precedes the non-idempotent POST.
        async with client_for(config) as client:
            options = {"max_cost_usd": config.max_cost_usd, "browser_settings": {"record": False}}
            if config.model is not None:
                options["model"] = config.model
            if config.model_params is not None:
                options["model_params"] = config.model_params
            task = (
                call.arguments["task"]
                + "\nReturn only JSON matching this schema: "
                + json.dumps(config.output_schema, separators=(",", ":"))
            )
            run = await client.runs.create(task, **options)
            state.update(
                phase="running",
                provider_run_id=str(run.id),
                session_id=str(run.session_id),
                browser_ids=[],
                stopped_ids=[],
                after=0,
            )
            await call.save_state(state)
        return DeferredToolResult()

    async def reconcile(self, call):
        return await self.advance(call)

    async def cleanup(self, call):
        return await self.advance(call, abandon=True)

    async def advance(self, call, *, abandon=False):
        state = dict(call.state)
        if not state.get("provider_run_id") or call.save_state is None:
            return None  # Lookup by task text is not proof of identity; never recreate.
        config = BrowserUseConfig.model_validate(call.definition["config"])
        cleanup_deadline = min(
            state.get("cleanup_deadline", float("inf")), state.get("cancel_deadline", float("inf"))
        )
        if not abandon and time.time() >= cleanup_deadline:
            raise TimeoutError("browser_cleanup_pending")
        async with client_for(config) as client:
            run_id = state["provider_run_id"]
            if not state.get("terminal_status"):
                status = (await client.runs.status(run_id)).status.value
                if status not in TERMINAL:
                    if abandon or time.time() >= state["deadline"]:
                        state["stop_reason"] = "browser_task_cancelled" if abandon else "browser_task_timeout"
                        state.setdefault("cancel_deadline", time.time() + 60)
                        await call.save_state(state)
                        await client.runs.cancel(run_id)
                    return DeferredToolResult()
                state["terminal_status"] = status
                state["cleanup_deadline"] = time.time() + 60
                await call.save_state(state)
            if not state.get("events_drained"):
                page = await client.runs.events(run_id, after=state["after"], limit=100)
                ids = set(state["browser_ids"])
                for event in page.events:
                    if str(event.run_id) != run_id:
                        raise ValueError("browser_event_identity_mismatch")
                    if event.type in {"browser.ready", "browser.reattached"}:
                        ids.add(str(UUID(event.data["browser_session_id"])))
                if len(ids) > 32:
                    raise ValueError("browser_count_limit")
                state.update(
                    browser_ids=sorted(ids),
                    after=page.next_after or state["after"],
                    events_drained=not page.has_more,
                )
                await call.save_state(state)  # Never persist live/CDP URLs or raw events.
                if page.has_more:
                    if not page.next_after or page.next_after <= call.state.get("after", 0):
                        raise ValueError("browser_event_cursor_invalid")
                    return DeferredToolResult()
            remaining = sorted(set(state["browser_ids"]) - set(state["stopped_ids"]))
            if remaining:
                browser = await client.browsers.stop(remaining[0])
                if str(browser.id) != remaining[0] or browser.status.value != "stopped":
                    return DeferredToolResult()
                state["stopped_ids"] = [*state["stopped_ids"], remaining[0]]
                await call.save_state(state)
                if len(remaining) > 1:
                    return DeferredToolResult()
            if not state.get("receipt"):
                run = await client.runs.get(run_id)
                if str(run.id) != run_id or str(run.session_id) != state["session_id"]:
                    raise ValueError("browser_receipt_identity_mismatch")
                if run.status.value not in TERMINAL:
                    return DeferredToolResult()
                state["receipt"] = validate_result(run, config, state.get("stop_reason"))
                # Redact secrets before any result is persisted, not just before public output.
                from .extensions import ExtensionRegistry

                try:
                    state["receipt"] = ExtensionRegistry.result(call.definition, state["receipt"])["output"]
                except ValueError:
                    state["receipt"] = {"provider_run_id": run_id, "error": "browser_result_invalid"}
                await call.save_state(state)
            state["phase"] = "cleaned"
            await call.save_state(state)
            return {**state["receipt"], "cleanup_complete": True}


def validate_result(run, config, stop_reason=None):
    """Provider completion is necessary, but does not establish semantic correctness."""
    receipt = {"provider_run_id": str(run.id), "status": run.status.value}
    try:
        cost = Decimal(run.total_cost_usd)
        if not cost.is_finite() or cost < 0:
            raise ValueError("invalid cost")
        receipt["cost_usd"] = str(cost)
        if cost > Decimal(str(config.max_cost_usd)):
            return {**receipt, "error": "browser_cost_limit_exceeded"}
        if stop_reason or run.status.value != "completed" or run.error:
            return {**receipt, "error": stop_reason or "browser_task_failed"}
        if not run.result or len(run.result.encode()) > 12000:
            raise ValueError("missing or oversized result")
        value = json.loads(run.result)
        bounded_json(value)
        if not Draft202012Validator(config.output_schema).is_valid(value):
            raise ValueError("invalid result")
        return {**receipt, "data": value, "schema_validated": True}
    except (ValueError, TypeError, ArithmeticError, RecursionError):
        return {**receipt, "error": "browser_result_invalid"}
