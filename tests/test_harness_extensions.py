import json

import pytest
from temporalio.exceptions import ApplicationError
from test_general_semantic import http_client

from agent_runtime.extensions import ExtensionRegistry, MCPConfig, ToolCall
from agent_runtime.general_contracts import GeneralPolicy
from agent_runtime.general_db import GeneralOperationRow
from agent_runtime.general_runtime import general_action
from agent_runtime.schemas import AgentConfig


class Lookup:
    version = 1

    def __init__(self):
        self.calls = []

    async def execute(self, call):
        self.calls.append(call)
        return {"customer": call.arguments["customer"], "plan": "basic"}


class Writer(Lookup):
    def __init__(self):
        super().__init__()
        self.effects = {}
        self.reconciliations = []

    async def execute(self, call):
        self.calls.append(call)
        self.effects[call.idempotency_key] = {"recorded": call.arguments["customer"]}
        raise TimeoutError("SECRET REMOTE TOKEN")

    async def reconcile(self, call):
        self.reconciliations.append(call.idempotency_key)
        return self.effects.get(call.idempotency_key)


def registration(write=False):
    return {
        "alias": "customer_lookup",
        "handler": "installed.lookup",
        "version": 1,
        "description": "Look up a customer",
        "arguments_schema": {
            "type": "object",
            "properties": {"customer": {"type": "string"}},
            "required": ["customer"],
            "additionalProperties": False,
        },
        "effect": {
            "kind": "external-write" if write else "read",
            "domain": "customers",
            "approval": "required" if write else "none",
            "retry_safety": "reconcile" if write else "read",
        },
    }


async def create(client, tools=("customer_lookup",), policy=None):
    agent = await client.create_agent(
        AgentConfig(
            name="ext",
            provider="fake",
            model="deterministic",
            tools=list(tools),
            general=policy or GeneralPolicy(),
        )
    )
    return await client.submit(agent.id, "Look up a customer")


def action(run_id, alias="customer_lookup", args=None, step=0):
    return {
        "run_id": run_id,
        "step": step,
        "decision": {
            "action": {
                "kind": "invoke",
                "capability": alias,
                "arguments": {"customer": "one"} if args is None else args,
            }
        },
    }


async def test_installed_handler_snapshot_and_public_discovery(store):
    handler = Lookup()
    store.extensions = ExtensionRegistry({"tools": [registration()]}, handlers={"installed.lookup": handler})
    async with http_client(store) as client:
        installed = await client.installed_capabilities()
        assert any(t["alias"] == "customer_lookup" for t in installed)
        assert "installed.lookup" not in json.dumps(installed)
        run = await create(client)
        # Later operator changes do not change the definition of this run.
        store.extensions.tools["customer_lookup"]["description"] = "changed after submission"
        result = await general_action(action(run.id))
        assert result["output"]["plan"] == "basic"
        assert (await general_action(action(run.id))) == result
        assert len(handler.calls) == 1
        assert handler.calls[0].definition["description"] == "Look up a customer"
        assert (await client.budget(run.id))["v3"]["counters"]["tool_attempts"] == 1


async def test_ungranted_or_invalid_extension_never_executes(store):
    handler = Lookup()
    store.extensions = ExtensionRegistry({"tools": [registration()]}, handlers={"installed.lookup": handler})
    async with http_client(store) as client:
        run = await create(client, tools=[])
        assert (await general_action(action(run.id)))["error"] == "capability_not_authorized"
        selected = await create(client)
        assert (await general_action(action(selected.id, args={"customer": 3})))["error"] == "action_failed"
        assert not handler.calls


async def test_approved_ambiguous_write_reconciles_without_repeating_effect(store):
    handler = Writer()
    store.extensions = ExtensionRegistry(
        {"tools": [registration(True)]}, handlers={"installed.lookup": handler}
    )
    async with http_client(store) as client:
        run = await create(client)
        payload = action(run.id)
        pending = await general_action(payload)
        assert not handler.calls
        await client.decide(run.id, pending["approval"], True)
        with pytest.raises(ApplicationError, match="extension_reconcile_pending"):
            await general_action(payload)
        async with store.database.sessions() as db:
            op = await db.get(GeneralOperationRow, run.id + ":action:0")
            assert op.data["status"] == "outcome_unknown"
        result = await general_action(payload)
        assert result["effect"] and result["output"]["recorded"] == "one"
        assert len(handler.calls) == len(handler.effects) == len(handler.reconciliations) == 1
        assert await general_action(payload) == result
        assert "SECRET" not in json.dumps(await client.operations(run.id))


async def test_denial_persists_for_extension_domain(store):
    handler = Writer()
    store.extensions = ExtensionRegistry(
        {"tools": [registration(True)]}, handlers={"installed.lookup": handler}
    )
    async with http_client(store) as client:
        run = await create(client)
        pending = await general_action(action(run.id))
        await client.decide(run.id, pending["approval"], False)
        assert (await general_action(action(run.id)))["error"] == "effect_denied"
        assert (await general_action(action(run.id, step=1)))["error"] == "effect_denied"
        assert not handler.calls


async def test_skill_content_is_pinned_and_does_not_grant_tools(store, tmp_path):
    skill = tmp_path / "SKILL.md"
    skill.write_text("Use exact decimal arithmetic. Ask for refunds before reporting gross sales.")
    store.extensions = ExtensionRegistry(
        {
            "skills": [
                {"alias": "invoices", "version": 1, "description": "Invoice analysis", "path": str(skill)}
            ]
        }
    )
    async with http_client(store) as client:
        run = await create(client, tools=[], policy=GeneralPolicy(skills=["invoices"]))
        skill.write_text("Changed after submission")
        result = await general_action(action(run.id, "skill_read", {"alias": "invoices"}))
        assert "exact decimal" in result["skill"]["content"]
        assert result["permissions"] == "unchanged"
        assert set((await store.general(run.id))["tools"]) == {"skill_read"}
        assert (await general_action(action(run.id, "skill_read", {"alias": "unselected"}, 1)))[
            "error"
        ] == "skill_not_authorized"


def test_mcp_registration_requires_pinned_schema_and_retry_contract():
    definition = {
        **registration(True),
        "handler": "mcp.http",
        "config": {"url": "http://localhost:8001/mcp", "tool": "lookup"},
    }
    with pytest.raises(ValueError, match="idempotency"):
        ExtensionRegistry({"tools": [definition]})
    with pytest.raises(ValueError):
        MCPConfig(url="https://secret:password@example.com/mcp", tool="lookup")
    with pytest.raises(ValueError):
        MCPConfig(url="https://example.com/mcp?key=secret", tool="lookup")
    definition["config"]["idempotency_argument"] = "request_id"
    definition["arguments_schema"]["properties"]["request_id"] = {"type": "string"}
    definition["arguments_schema"]["required"].append("request_id")
    registry = ExtensionRegistry({"tools": [definition]})
    public = registry.snapshots([definition["alias"]])[definition["alias"]]
    assert "request_id" not in public["arguments_schema"]["properties"]
    assert "request_id" not in public["arguments_schema"]["required"]
    with pytest.raises(ValueError):
        registry.validate_arguments(public, {"customer": "one", "request_id": "forged"})


def test_credentials_redacted_before_persistence(monkeypatch):
    monkeypatch.setenv("TEST_MCP_TOKEN", "secret-value")
    definition = {**registration(), "config": {"credential_env": "TEST_MCP_TOKEN"}, "max_result_bytes": 1024}
    result = ExtensionRegistry.result(definition, {"echo": "token secret-value"})
    assert result["output"]["echo"] == "token [REDACTED]"
    assert "secret-value" not in json.dumps(result)


async def test_mcp_adapter_validates_remote_schema_and_calls_selected_tool(monkeypatch):
    from types import SimpleNamespace

    from agent_runtime.extensions import MCPHandler

    definition = {
        **registration(),
        "handler": "mcp.http",
        "config": {"url": "http://example.test/mcp", "tool": "lookup"},
    }
    calls = []

    class FakeMCP:
        def __init__(self, *args, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def list_tools(self):
            return [SimpleNamespace(name="lookup", input_schema=definition["arguments_schema"])]

        async def call_tool(self, name, args, **kw):
            calls.append((name, args))
            return SimpleNamespace(is_error=False, data={"plan": "basic"}, content=[])

    monkeypatch.setattr("fastmcp.Client", FakeMCP)
    result = await MCPHandler().execute(ToolCall("run", "op", {"customer": "one"}, definition))
    assert result["data"]["plan"] == "basic"
    assert calls == [("lookup", {"customer": "one"})]


async def test_crash_after_last_external_attempt_keeps_unknown_effect_blocking(store):
    handler = Writer()
    store.extensions = ExtensionRegistry(
        {"tools": [registration(True)]}, handlers={"installed.lookup": handler}
    )
    async with http_client(store) as client:
        run = await create(client)
        payload = action(run.id)
        pending = await general_action(payload)
        await client.decide(run.id, pending["approval"], True)
        with pytest.raises(ApplicationError, match="extension_reconcile_pending"):
            await general_action(payload)
        async with store.database.sessions.begin() as db:
            op = await db.get(GeneralOperationRow, run.id + ":action:0")
            op.data = {**op.data, "external_attempts": 2, "status": "pending", "lease": 0}
        result = await general_action(payload)
        assert result["error"] == "extension_outcome_unknown"
        assert not handler.reconciliations
        async with store.database.sessions() as db:
            op = await db.get(GeneralOperationRow, run.id + ":action:0")
            assert op.data["status"] == "outcome_unknown" and op.data.get("result") is None
        effects = await client.effects(run.id)
        assert any(e["classification"] == "outcome_unknown" for e in effects)
        # A new operation cannot work around the unresolved write.
        assert (await general_action(action(run.id, step=1)))["error"] == "unresolved_external_effect"
