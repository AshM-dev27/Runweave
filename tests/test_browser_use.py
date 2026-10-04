import copy
import json
import time
from uuid import uuid4

import httpx
import pytest
from temporalio.exceptions import ApplicationError
from test_general_semantic import http_client
from test_harness_extensions import action, create

from agent_runtime.browser_use import ARGUMENTS_SCHEMA, BrowserUseConfig
from agent_runtime.extension_runtime import cleanup_extension
from agent_runtime.extensions import ExtensionRegistry
from agent_runtime.general_db import GeneralOperationRow, GeneralRunRow
from agent_runtime.general_runtime import general_action


def registration(**config):
    return {
        "alias": "browser_task",
        "handler": "browser_use.v4",
        "version": 1,
        "description": "Hosted browser task. Requires approval; costs at most $1 per task.",
        "arguments_schema": ARGUMENTS_SCHEMA,
        "effect": {
            "kind": "external-write",
            "domain": "browser",
            "approval": "required",
            "retry_safety": "reconcile",
        },
        "config": {"credential_env": "BROWSER_USE_API_KEY", **config},
    }


class Cloud:
    def __init__(self):
        self.run, self.session, self.workspace, self.browser = [str(uuid4()) for _ in range(4)]
        self.status = "completed"
        self.result = json.dumps({"summary": "Example Domain", "sources": ["https://example.com"]})
        self.cost = "0.05"
        self.calls = []
        self.create_failure = None
        self.stop_failure = False
        self.pages = 1

    def summary(self):
        return {
            "id": self.run,
            "status": self.status,
            "task": "test",
            "title": None,
            "model": "provider-default",
            "contextLimit": 128000,
            "result": self.result,
            "error": None,
            "sessionId": self.session,
            "workspaceId": self.workspace,
            "totalInputTokens": 10,
            "totalOutputTokens": 20,
            "totalCostUsd": self.cost,
            "createdAt": "2026-10-04T00:00:00Z",
            "updatedAt": "2026-10-04T00:00:01Z",
        }

    def request(self, request):
        assert request.headers["X-Browser-Use-API-Key"] == "test-browser-secret"
        path = request.url.path.removeprefix("/api/v4")
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.method, path, body))
        if request.method == "POST" and path == "/runs":
            if self.create_failure == "timeout":
                raise httpx.ReadTimeout("test-browser-secret", request=request)
            if self.create_failure == "throttle":
                return httpx.Response(429, json={"detail": "test-browser-secret"})
            return httpx.Response(
                200,
                json={
                    "id": self.run,
                    "status": "queued",
                    "model": "provider-default",
                    "sessionId": self.session,
                    "workspaceId": self.workspace,
                    "eventsUrl": "private",
                },
            )
        if path.endswith("/status"):
            return httpx.Response(200, json={"status": self.status})
        if path.endswith("/cancel"):
            self.status = "cancelled"
            return httpx.Response(200, json=self.summary())
        if path.endswith("/events"):
            after = int(request.url.params.get("after", 0))
            event = {
                "runId": self.run,
                "id": after + 1,
                "ts": "2026-10-04T00:00:00Z",
                "type": "browser.ready",
                "data": {
                    "browser_session_id": self.browser,
                    "live_view_url": "PRIVATE-LIVE-URL",
                    "secret": "test-browser-secret",
                },
            }
            return httpx.Response(
                200, json={"events": [event], "nextAfter": after + 1, "hasMore": after + 1 < self.pages}
            )
        if path.startswith("/browsers/"):
            assert request.method == "PATCH" and body == {"action": "stop"}
            if self.stop_failure:
                raise httpx.ReadTimeout("test-browser-secret", request=request)
            return httpx.Response(
                200,
                json={
                    "id": self.browser,
                    "status": "stopped",
                    "startedAt": "2026-10-04T00:00:00Z",
                    "timeoutAt": "2026-10-04T00:10:00Z",
                },
            )
        if path == "/runs/" + self.run:
            return httpx.Response(200, json=self.summary())
        raise AssertionError((request.method, path))

    @property
    def creates(self):
        return [c for c in self.calls if c[:2] == ("POST", "/runs")]


@pytest.fixture
def cloud(monkeypatch):
    cloud = Cloud()
    original = httpx.AsyncClient

    def mock_client(*args, **kwargs):
        if kwargs.get("base_url") == "https://api.browser-use.com/api/v4":
            kwargs["transport"] = httpx.MockTransport(cloud.request)
        return original(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", mock_client)
    monkeypatch.setenv("BROWSER_USE_API_KEY", "test-browser-secret")
    return cloud


async def approved(store, client, **config):
    store.extensions = ExtensionRegistry({"tools": [registration(**config)]})
    run = await create(client, tools=["browser_task"])
    payload = action(run.id, "browser_task", {"task": "Read example.com"})
    wait = await general_action(payload)
    assert "approval" in wait
    await client.decide(run.id, wait["approval"], True)
    return run, payload


async def finish(payload, attempts=8):
    for _ in range(attempts):
        result = await general_action(payload)
        if not result.get("external_pending"):
            return result
    pytest.fail("browser tool did not settle")


async def test_default_sdk_payload_and_durable_polling(store, cloud):
    async with http_client(store) as client:
        run, payload = await approved(store, client)
        assert not cloud.calls
        assert (await general_action(payload))["external_pending"]
        async with store.database.sessions() as db:
            op = await db.get(GeneralOperationRow, run.id + ":action:0")
            assert op.data["handler_state"]["provider_run_id"] == cloud.run
            assert op.data.get("result") is None
        cloud.status = "running"
        for _ in range(4):
            assert (await general_action(payload))["external_pending"]
        cloud.status = "completed"
        result = await finish(payload)
        assert result["output"]["data"]["summary"] == "Example Domain"
        assert result["output"]["schema_validated"] and result["output"]["cleanup_complete"]
        assert await general_action(payload) == result
        assert len(cloud.creates) == 1
        body = cloud.creates[0][2]
        assert body["maxCostUsd"] == 1
        assert "model" not in body and "modelParams" not in body and "sessionId" not in body
        assert any(method == "PATCH" for method, _, _ in cloud.calls)
        assert (await client.budget(run.id))["v3"]["counters"]["tool_attempts"] == 1
        async with store.database.sessions() as db:
            op = await db.get(GeneralOperationRow, run.id + ":action:0")
            assert "test-browser-secret" not in json.dumps(op.data)
            assert "PRIVATE-LIVE-URL" not in json.dumps(op.data)


async def test_explicit_model_and_lower_cap_preserved(store, cloud):
    async with http_client(store) as client:
        _, payload = await approved(store, client, model="gpt-6-astra", model_params={}, max_cost_usd=0.25)
        await finish(payload)
        body = cloud.creates[0][2]
        assert body["model"] == "gpt-6-astra" and body["modelParams"] == {} and body["maxCostUsd"] == 0.25


@pytest.mark.parametrize("failure", ["timeout", "throttle"])
async def test_ambiguous_create_never_retried(store, cloud, failure):
    cloud.create_failure = failure
    async with http_client(store) as client:
        run, payload = await approved(store, client)
        with pytest.raises(ApplicationError, match="extension_reconcile_pending"):
            await general_action(payload)
        assert (await general_action(payload))["error"] == "extension_outcome_unknown"
        assert len(cloud.creates) == 1
        await client.cancel(run.id)
        assert not await cleanup_extension(store, run.id, run.id + ":action:0", provider_io=True)
        assert len(cloud.creates) == 1
        assert "test-browser-secret" not in json.dumps(await client.operations(run.id))


@pytest.mark.parametrize(
    "value,error",
    [
        ("not json", "browser_result_invalid"),
        ('{"summary": "", "sources": []}', "browser_result_invalid"),
        ('{"summary": "ok"}', "browser_result_invalid"),
        ('{"summary": "test-browser-secret", "sources": []}', None),
    ],
)
async def test_validates_and_redacts_results_before_persisting(store, cloud, value, error):
    cloud.result = value
    async with http_client(store) as client:
        run, payload = await approved(store, client)
        result = await finish(payload)
        assert result.get("error") == error
        assert any(method == "PATCH" for method, _, _ in cloud.calls)
        async with store.database.sessions() as db:
            op = await db.get(GeneralOperationRow, run.id + ":action:0")
            assert "test-browser-secret" not in json.dumps(op.data)


@pytest.mark.parametrize("status", ["failed", "cancelled"])
async def test_noncompleted_provider_status_is_not_success(store, cloud, status):
    cloud.status = status
    async with http_client(store) as client:
        _, payload = await approved(store, client)
        assert (await finish(payload))["error"] == "browser_task_failed"
        assert any(method == "PATCH" for method, _, _ in cloud.calls)


async def test_cancellation_recovers_owned_browser_and_drains_events(store, cloud):
    cloud.status, cloud.pages = "running", 2
    async with http_client(store) as client:
        run, payload = await approved(store, client)
        await general_action(payload)
        await client.cancel(run.id)
        identity = run.id + ":action:0"
        assert not await cleanup_extension(store, run.id, identity)  # Dispatcher does no provider I/O.
        assert not await cleanup_extension(store, run.id, identity, provider_io=True)  # Cancel.
        assert not await cleanup_extension(store, run.id, identity, provider_io=True)  # Drain first page.
        assert await cleanup_extension(store, run.id, identity, provider_io=True)
        assert len(cloud.creates) == 1
        async with store.database.sessions() as db:
            op = await db.get(GeneralOperationRow, identity)
            assert op.data["result"]["error"] == "browser_task_cancelled"
            assert op.data["handler_state"]["phase"] == "cleaned"


async def test_root_budget_expiry_still_cancels_without_resource_pause(store, cloud):
    cloud.status = "running"
    async with http_client(store) as client:
        run, payload = await approved(store, client)
        await general_action(payload)
        async with store.database.sessions.begin() as db:
            op = await db.get(GeneralOperationRow, run.id + ":action:0")
            op.data = {**op.data, "handler_state": {**op.data["handler_state"], "deadline": time.time() - 1}}
            gr = await db.get(GeneralRunRow, run.id)
            gr.data = {**gr.data, "active_started": time.time() - 2000}
        result = await finish(payload)
        assert result["error"] == "browser_task_timeout"
        assert any(method == "PATCH" for method, _, _ in cloud.calls)


async def test_stop_failure_keeps_result_unaccepted_and_cleanup_retries(store, cloud):
    cloud.stop_failure = True
    async with http_client(store) as client:
        run, payload = await approved(store, client)
        await general_action(payload)
        assert (await general_action(payload))["error"] == "extension_outcome_unknown"
        await client.cancel(run.id)
        assert not await cleanup_extension(store, run.id, run.id + ":action:0", provider_io=True)
        cloud.stop_failure = False
        assert await cleanup_extension(store, run.id, run.id + ":action:0", provider_io=True)
        assert len(cloud.creates) == 1


async def test_crash_after_intent_does_not_create(store, cloud):
    async with http_client(store) as client:
        run, payload = await approved(store, client)
        # Simulate a worker lost after committing the intent but before saving a provider ID.
        await store.general_operation(run.id, 0, payload["decision"])
        async with store.database.sessions.begin() as db:
            op = await db.get(GeneralOperationRow, run.id + ":action:0")
            op.data = {**op.data, "started": True, "external_attempts": 1, "lease": 0}
        assert (await general_action(payload))["error"] == "extension_outcome_unknown"
        assert not cloud.calls


@pytest.mark.parametrize(
    "config",
    [
        {"max_cost_usd": 1.01},
        {"max_cost_usd": float("nan")},
        {"output_schema": {"$ref": "https://evil.invalid/schema"}},
        {"api_key": "secret"},
    ],
)
def test_unsafe_configuration_rejected(config):
    with pytest.raises(ValueError):
        BrowserUseConfig.model_validate(config)


def test_read_classification_rejected():
    definition = copy.deepcopy(registration())
    definition["effect"].update(kind="read", approval="none", retry_safety="read")
    with pytest.raises(ValueError):
        ExtensionRegistry({"tools": [definition]})


async def test_denied_browser_task_never_dispatches(store, cloud):
    store.extensions = ExtensionRegistry({"tools": [registration()]})
    async with http_client(store) as client:
        run = await create(client, tools=["browser_task"])
        payload = action(run.id, "browser_task", {"task": "Read example.com"})
        pending = await general_action(payload)
        await client.decide(run.id, pending["approval"], False)
        assert (await general_action(payload))["error"] == "effect_denied"
        assert not cloud.calls


async def test_missing_key_fails_without_dispatch(store, cloud, monkeypatch):
    monkeypatch.delenv("BROWSER_USE_API_KEY")
    async with http_client(store) as client:
        _, payload = await approved(store, client)
        assert (await general_action(payload))["error"] == "browser_use_not_configured"
        assert not cloud.calls


async def test_reported_over_cap_is_error_and_browser_stops(store, cloud):
    cloud.cost = "1.01"
    async with http_client(store) as client:
        _, payload = await approved(store, client)
        result = await finish(payload)
        assert result["error"] == "browser_cost_limit_exceeded"
        assert result["output"]["cost_usd"] == "1.01"
        assert any(method == "PATCH" for method, _, _ in cloud.calls)


async def test_malformed_summary_cannot_prevent_browser_shutdown(store, cloud, monkeypatch):
    original = cloud.summary

    def malformed():
        data = original()
        del data["contextLimit"]
        return data

    monkeypatch.setattr(cloud, "summary", malformed)
    async with http_client(store) as client:
        run, payload = await approved(store, client)
        await general_action(payload)
        assert (await general_action(payload))["error"] == "extension_outcome_unknown"
        async with store.database.sessions() as db:
            op = await db.get(GeneralOperationRow, run.id + ":action:0")
            assert op.data["handler_state"]["stopped_ids"] == [cloud.browser]
            assert op.data.get("result") is None
        assert len(cloud.creates) == 1


async def test_cleanup_wait_is_bounded_and_remains_unresolved(store, cloud):
    async with http_client(store) as client:
        run, payload = await approved(store, client)
        await general_action(payload)
        async with store.database.sessions.begin() as db:
            op = await db.get(GeneralOperationRow, run.id + ":action:0")
            op.data = {
                **op.data,
                "handler_state": {**op.data["handler_state"], "cleanup_deadline": time.time() - 1},
            }
        assert (await general_action(payload))["error"] == "extension_outcome_unknown"
        assert len(cloud.creates) == 1
        await client.cancel(run.id)
        assert await cleanup_extension(store, run.id, run.id + ":action:0", provider_io=True)
