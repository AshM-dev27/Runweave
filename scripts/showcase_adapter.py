"""Test-only local service adapters and scripted planner, never installed in production."""

import asyncio
import json
import os
from urllib.parse import urlsplit

import httpx

from agent_runtime.extensions import ToolCall
from agent_runtime.general_contracts import StepDecision


def local_url(value):
    parsed = urlsplit(value)
    if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or parsed.username or parsed.password:
        raise ValueError("The showcase only permits loopback HTTP services")
    return value.rstrip("/")


class LocalService:
    version = 1

    async def execute(self, call: ToolCall):
        base = local_url(call.definition["config"]["url"])
        alias = call.definition["alias"]
        async with httpx.AsyncClient(base_url=base, timeout=20, trust_env=False) as client:
            if alias == "crm_preview":
                from agent_runtime.artifacts import verify
                from agent_runtime.db import ArtifactRow
                from agent_runtime.general_db import GeneralRunRow
                from agent_runtime.runtime import get_store

                async with get_store().database.sessions() as db:
                    row = await db.get(GeneralRunRow, call.run_id)
                    if call.arguments["artifact_id"] not in row.data["artifact_ids"]:
                        raise ValueError("Artifact not attached to this run")
                    artifact = await db.get(ArtifactRow, call.arguments["artifact_id"])
                    content = verify(artifact).decode()
                response = await client.post("/crm/preview", json={"csv": content})
            elif alias == "order_lookup":
                response = await client.get("/orders/" + call.arguments["order_id"])
            else:
                path = "/crm/import" if alias == "crm_import" else "/refunds"
                response = await client.post(
                    path, json=call.arguments, headers={"Idempotency-Key": call.idempotency_key}
                )
            response.raise_for_status()
            return response.json()

    async def reconcile(self, call: ToolCall):
        base = local_url(call.definition["config"]["url"])
        async with httpx.AsyncClient(base_url=base, timeout=5, trust_env=False) as client:
            response = await client.get("/receipts/" + call.idempotency_key)
            if response.status_code == 404:
                return None
            response.raise_for_status()
            return response.json()


def manifest(base_url):
    from agent_runtime.browser_use import ARGUMENTS_SCHEMA

    tools = []
    schemas = {
        "crm_preview": {"artifact_id": {"type": "string"}},
        "crm_import": {"plan_id": {"type": "string", "pattern": "^[a-f0-9]{64}$"}},
        "order_lookup": {"order_id": {"type": "string", "pattern": "^TEST-[0-9]+$"}},
        "refund_order": {
            "order_id": {"type": "string", "pattern": "^TEST-[0-9]+$"},
            "amount_cents": {"type": "integer", "minimum": 1},
        },
    }
    for alias, properties in schemas.items():
        write = alias in ("crm_import", "refund_order")
        tools.append(
            {
                "alias": alias,
                "description": "Synthetic benchmark: " + alias,
                "handler": "scripts.showcase_adapter:LocalService",
                "version": 1,
                "arguments_schema": {
                    "type": "object",
                    "properties": properties,
                    "required": list(properties),
                    "additionalProperties": False,
                },
                "effect": {
                    "kind": "external-write" if write else "read",
                    "domain": "showcase",
                    "approval": "required" if write else "none",
                    "retry_safety": "reconcile" if write else "read",
                },
                "reconciliation": "lookup",
                "config": {"url": local_url(base_url)},
            }
        )
    tools.append(
        {
            "alias": "browser_task",
            "description": "Synthetic local Browser Use V4 comparison",
            "handler": "browser_use.v4",
            "version": 1,
            "arguments_schema": ARGUMENTS_SCHEMA,
            "effect": {
                "kind": "external-write",
                "domain": "browser",
                "approval": "required",
                "retry_safety": "reconcile",
            },
            "config": {
                "credential_env": "BROWSER_USE_API_KEY",
                "max_cost_usd": 1,
                "timeout_seconds": 120,
                "output_schema": {
                    "type": "object",
                    "required": ["selected", "offers", "sources", "quantity", "currency"],
                    "properties": {
                        "selected": {"type": "object"},
                        "offers": {"type": "array"},
                        "sources": {"type": "array", "items": {"type": "string"}},
                        "quantity": {"type": "integer"},
                        "currency": {"type": "string"},
                    },
                },
            },
        }
    )
    presentations = {
        "crm_import": {
            "title": "Import reviewed customers",
            "source_capability": "crm_preview",
            "bindings": {"plan_id": "plan_id"},
            "fields": [
                {"label": label, "source": "receipt", "path": ["counts", key]}
                for label, key in (
                    ("Rows reviewed", "input_rows"),
                    ("Customers to import", "accepted"),
                    ("Duplicates excluded", "duplicates"),
                    ("Invalid rows excluded", "invalid"),
                    ("Conflicting rows excluded", "conflicts"),
                )
            ],
        },
        "refund_order": {
            "title": "Approve customer refund",
            "source_capability": "order_lookup",
            "bindings": {"order_id": "order_id", "amount_cents": "amount_cents"},
            "fields": [
                {"label": "Order", "path": ["order_id"]},
                {"label": "Refund", "path": ["amount_cents"], "format": "money_minor", "currency": "USD"},
                {"label": "Eligibility", "source": "receipt", "path": ["reason"]},
            ],
        },
        "browser_task": {
            "title": "Approve hosted browser task",
            "fields": [
                {"label": "Task", "path": ["task"]},
                {"label": "Maximum cost (USD)", "source": "config", "path": ["max_cost_usd"]},
            ],
            "warnings": ["Approval covers the whole hosted task."],
        },
    }
    for tool in tools:
        if tool["alias"] in presentations:
            tool["approval_presentation"] = presentations[tool["alias"]]
    return {"version": 1, "tools": tools}


def scripted_decision(original, prompt, state, verifications):
    if not prompt.startswith("showcase:"):
        return original(prompt, state, verifications)
    case = json.loads(prompt.removeprefix("showcase:"))
    previous = state.get("last_result") or {}
    observed = previous.get("output", {})
    step = state["step"]

    def invoke(name, arguments):
        return StepDecision.model_validate(
            {"action": {"kind": "invoke", "capability": name, "arguments": arguments}}
        )

    def blocked(reason):
        return StepDecision.model_validate({"action": {"kind": "blocked", "reason": reason}})

    if previous.get("error"):
        return blocked("Tool did not settle successfully: " + previous["error"])
    if case["kind"] == "crm":
        if step == 0:
            return invoke("crm_preview", {"artifact_id": case["artifact_id"]})
        if step == 1:
            return invoke("crm_import", {"plan_id": observed["plan_id"]})
    elif case["kind"] == "refund":
        if step == 0:
            return invoke("order_lookup", {"order_id": case["order_id"]})
        if step == 1:
            if not observed["eligible"]:
                return blocked("Refund rejected: " + observed["reason"])
            return invoke(
                "refund_order", {"order_id": observed["order_id"], "amount_cents": observed["amount_cents"]}
            )
    elif case["kind"] == "supplier" and step == 0:
        return invoke("browser_task", {"task": case["task"]})
    decision = original("", state, verifications)
    # Report actual tool observations. Independent golden assertions live in the runner.
    decision.action.answer = json.dumps(observed, sort_keys=True)
    return decision


def browser_client(config=None):
    from browser_use_sdk.v4 import AsyncBrowserUse

    base = local_url(os.environ["SHOWCASE_URL"])
    if os.environ.get("BROWSER_USE_API_KEY") != "showcase-dummy-key":
        raise RuntimeError("Only a dummy browser credential is permitted")
    client = AsyncBrowserUse(api_key="showcase-dummy-key", base_url=base + "/api/v4", timeout=5)
    client._http._max_retries = 0
    return client


def main():
    import pydantic_ai.models

    import agent_runtime.browser_use as browser
    import agent_runtime.general_runtime as runtime
    from agent_runtime.worker import main as worker_main

    pydantic_ai.models.ALLOW_MODEL_REQUESTS = False
    local_url(os.environ["SHOWCASE_URL"])
    if os.environ.get("BROWSER_USE_API_KEY") != "showcase-dummy-key":
        raise RuntimeError("Only a dummy browser credential is permitted")
    browser.client_for = browser_client
    original = runtime.fake_decision
    runtime.fake_decision = lambda prompt, state, verifications: scripted_decision(
        original, prompt, state, verifications
    )
    asyncio.run(worker_main())


if __name__ == "__main__":
    main()
