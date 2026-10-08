import copy
import json
import time
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select
from temporalio.exceptions import ApplicationError
from test_general_semantic import http_client
from test_harness_extensions import action, create

from agent_runtime.e2b import E2B_ARGUMENTS_SCHEMA, ROOT, E2BHandler, download
from agent_runtime.extension_runtime import cleanup_extension
from agent_runtime.extensions import ExtensionRegistry, ToolCall
from agent_runtime.general_db import GeneralAttemptRow, GeneralOperationRow
from agent_runtime.general_runtime import general_action
from agent_runtime.reconciliation import reconcile_operation
from examples.e2b_invoice import EXPECTED_SUMMARY, job, verify_output


def registration(**config):
    return {
        "alias": "e2b_python",
        "handler": "e2b.python.v1",
        "version": 1,
        "description": "Bounded isolated E2B Python test",
        "arguments_schema": E2B_ARGUMENTS_SCHEMA,
        "effect": {
            "kind": "isolated-command",
            "domain": "e2b",
            "approval": "none",
            "retry_safety": "reconcile",
        },
        "execution_mode": "deferred",
        "max_concurrency": 4,
        "config": {"credential_env": "E2B_API_KEY", **config},
        "max_result_bytes": 24576,
    }


class Cloud:
    def __init__(self):
        self.sandbox_id = "sandbox-test"
        self.files = self
        self.commands = self
        self.content = {}
        self.calls = []
        self.create_failure = False
        self.command_failure = False
        self.kill_failure = False
        self.missing_receipt = False
        self.oversized_output = False
        self.exit_code = 0
        self.matches = 1
        self.killed = False

    async def create(self, **kwargs):
        self.calls.append(("create", kwargs))
        if self.create_failure:
            raise TimeoutError("test-e2b-secret")
        return self

    async def connect(self, sandbox_id, **kwargs):
        assert sandbox_id == self.sandbox_id
        self.calls.append(("connect", kwargs))
        return self

    def list(self, **kwargs):
        self.calls.append(("list", kwargs))

        async def next_items():
            return [SimpleNamespace(sandbox_id=self.sandbox_id) for _ in range(self.matches)]

        return SimpleNamespace(next_items=next_items, has_next=False)

    async def kill(self, sandbox_id, **kwargs):
        assert sandbox_id == self.sandbox_id
        self.calls.append(("kill", kwargs))
        if self.kill_failure:
            raise TimeoutError("test-e2b-secret")
        self.killed = True
        return True

    async def write(self, path, content, **kwargs):
        self.calls.append(("write", path))
        self.content[path] = content.encode()

    async def exists(self, path, **kwargs):
        return path in self.content

    async def run(self, cmd, **kwargs):
        self.calls.append(("run", cmd))
        # Simulate provider outputs; never execute generated Python on the test host.
        if not self.missing_receipt:
            self.content[ROOT + "/result.json"] = json.dumps(
                {"exit_code": self.exit_code, "stdout": "generated", "stderr": ""}
            ).encode()
        self.content[ROOT + "/summary.json"] = json.dumps(EXPECTED_SUMMARY).encode()
        self.content[ROOT + "/cleaned.csv"] = (
            b"invoice_id,amount\nINV-001,100.00\nINV-002,50.00\nREF-001,-20.00\n"
            if not self.oversized_output
            else b"x" * 5000
        )
        if self.command_failure:
            raise TimeoutError("test-e2b-secret")
        return SimpleNamespace(exit_code=0)

    async def download(self, sandbox, path, limit):
        value = self.content[path]
        if len(value) > limit:
            raise ValueError("e2b_output_limit")
        return value

    def count(self, name):
        return sum(call[0] == name for call in self.calls)


@pytest.fixture
def cloud(monkeypatch):
    value = Cloud()
    monkeypatch.setenv("E2B_API_KEY", "test-e2b-secret")
    monkeypatch.setattr("agent_runtime.e2b.sandbox_class", lambda: value)
    monkeypatch.setattr("agent_runtime.e2b.download", value.download)
    return value


async def setup(store, client, arguments=None):
    store.extensions = ExtensionRegistry({"tools": [registration()]})
    run = await create(client, tools=["e2b_python"])
    return run, action(run.id, "e2b_python", job() if arguments is None else arguments)


async def finish(payload):
    for _ in range(12):
        result = await general_action(payload)
        if not result.get("external_pending"):
            return result
    pytest.fail("E2B operation did not settle")


async def test_durable_multiaction_job_and_repeat_receipt(store, cloud):
    async with http_client(store) as client:
        run, payload = await setup(store, client)
        result = await finish(payload)
        assert verify_output(result["output"]) == EXPECTED_SUMMARY
        assert result["output"]["actions"] == [
            "sandbox.created",
            "files.uploaded",
            "python.executed",
            "outputs.downloaded",
            "sandbox.killed",
        ]
        assert await general_action(payload) == result
        assert cloud.count("create") == cloud.count("run") == cloud.count("kill") == 1
        assert cloud.killed
        options = cloud.calls[0][1]
        assert options["allow_internet_access"] is False and options["secure"] is True
        assert options["timeout"] <= 120 and "envs" not in options
        assert (await client.budget(run.id))["v3"]["counters"]["tool_attempts"] == 1
        async with store.database.sessions() as db:
            operation = await db.get(GeneralOperationRow, run.id + ":action:0")
            assert operation.data["handler_state"]["phase"] == "finished"
            assert "test-e2b-secret" not in json.dumps(operation.data)


async def test_lost_create_response_recovers_by_metadata_without_recreation(store, cloud):
    cloud.create_failure = True
    async with http_client(store) as client:
        _, payload = await setup(store, client)
        with pytest.raises(ApplicationError, match="extension_reconcile_pending"):
            await general_action(payload)
        result = await finish(payload)
        assert verify_output(result["output"]) == EXPECTED_SUMMARY
        assert cloud.count("create") == cloud.count("run") == 1
        assert cloud.count("list") == 1
        assert result["output"]["actions"][0] == "sandbox.recovered"


@pytest.mark.parametrize("matches", [0, 2])
async def test_ambiguous_create_remains_unknown_without_blind_retry(store, cloud, matches):
    cloud.create_failure, cloud.matches = True, matches
    async with http_client(store) as client:
        run, payload = await setup(store, client)
        with pytest.raises(ApplicationError, match="extension_reconcile_pending"):
            await general_action(payload)
        assert (await general_action(payload))["error"] == "extension_outcome_unknown"
        assert cloud.count("create") == 1 and cloud.count("run") == 0
        await client.cancel(run.id)
        assert not await cleanup_extension(store, run.id, run.id + ":action:0", provider_io=True)
        assert cloud.count("create") == 1


async def test_lost_command_response_reads_receipt_without_reexecuting(store, cloud):
    cloud.command_failure = True
    async with http_client(store) as client:
        _, payload = await setup(store, client)
        assert (await general_action(payload))["external_pending"]
        assert (await general_action(payload))["external_pending"]
        assert (await general_action(payload))["external_pending"]
        assert verify_output((await finish(payload))["output"]) == EXPECTED_SUMMARY
        assert cloud.count("run") == 1 and cloud.count("create") == 1


async def test_finished_state_recovers_lost_result_without_provider_io(store, cloud, monkeypatch):
    async with http_client(store) as client:
        run, payload = await setup(store, client)
        result = await finish(payload)
        async with store.database.sessions.begin() as db:
            operation = await db.get(GeneralOperationRow, run.id + ":action:0")
            # Simulate a crash after cleanup state was saved, before the operation result committed.
            operation.data = {
                **operation.data,
                "result": None,
                "status": "pending",
                "handler_state": {**operation.data["handler_state"], "deadline": time.time() - 1},
            }
        calls = copy.deepcopy(cloud.calls)
        monkeypatch.delenv("E2B_API_KEY")
        assert await general_action(payload) == result
        assert cloud.calls == calls


async def test_no_remaining_time_never_dispatches(cloud):
    states = []

    async def save(state):
        states.append(state)

    call = ToolCall("run", "operation", job(), registration(), remaining_seconds=0, save_state=save)
    assert await E2BHandler().execute(call) == {"error": "e2b_time_limit"}
    assert not states and not cloud.calls


async def test_cancelled_job_only_cleans_up_and_never_launches_python(store, cloud):
    async with http_client(store) as client:
        run, payload = await setup(store, client)
        assert (await general_action(payload))["external_pending"]
        await client.cancel(run.id)
        assert await cleanup_extension(store, run.id, run.id + ":action:0", provider_io=True)
        assert cloud.count("kill") == 1 and cloud.count("run") == 0
        assert cloud.count("create") == 1


async def test_failed_cleanup_keeps_pending_until_kill_can_be_confirmed(store, cloud):
    async with http_client(store) as client:
        run, payload = await setup(store, client)
        await general_action(payload)
        await client.cancel(run.id)
        cloud.kill_failure = True
        assert not await cleanup_extension(store, run.id, run.id + ":action:0", provider_io=True)
        cloud.kill_failure = False
        assert await cleanup_extension(store, run.id, run.id + ":action:0", provider_io=True)
        assert cloud.count("create") == 1 and cloud.killed


@pytest.mark.parametrize("failure", ["exit", "output", "receipt"])
async def test_invalid_results_are_errors_with_cleanup(store, cloud, failure):
    cloud.exit_code = 1 if failure == "exit" else 0
    cloud.oversized_output = failure == "output"
    async with http_client(store) as client:
        _, payload = await setup(store, client)
        for _ in range(3):
            await general_action(payload)
        if failure == "receipt":
            cloud.content[ROOT + "/result.json"] = b"not JSON"
        result = await finish(payload)
        assert result["error"] in {"e2b_command_failed", "e2b_output_invalid", "e2b_result_invalid"}
        assert result["output"]["cleanup_complete"] and cloud.killed


@pytest.mark.parametrize(
    "path", ["../private", "/etc/passwd", "code.py", "result.tmp", "a/../b", ".runtime/x"]
)
async def test_invalid_file_scope_never_creates_a_sandbox(store, cloud, path):
    arguments = {**job(), "outputs": [path]}
    async with http_client(store) as client:
        _, payload = await setup(store, client, arguments)
        assert (await general_action(payload))["error"] == "action_failed"
        assert not cloud.calls


async def test_pending_command_expires_without_relaunching(store, cloud):
    cloud.missing_receipt = True
    async with http_client(store) as client:
        run, payload = await setup(store, client)
        for _ in range(6):
            assert (await general_action(payload))["external_pending"]
        assert cloud.count("run") == 1
        async with store.database.sessions.begin() as db:
            operation = await db.get(GeneralOperationRow, run.id + ":action:0")
            operation.data = {
                **operation.data,
                "handler_state": {**operation.data["handler_state"], "deadline": time.time() - 1},
            }
        result = await general_action(payload)
        assert result["error"] == "e2b_time_limit" and cloud.killed
        assert cloud.count("run") == cloud.count("create") == 1


async def test_missing_key_has_explicit_error_without_provider_calls(store, cloud, monkeypatch):
    monkeypatch.delenv("E2B_API_KEY")
    async with http_client(store) as client:
        _, payload = await setup(store, client)
        assert (await general_action(payload))["error"] == "e2b_not_configured"
        assert not cloud.calls


def test_registry_rejects_unbounded_or_misclassified_e2b():
    for key, value in [("command_seconds", 31), ("sandbox_seconds", 301), ("credential_env", "OTHER_KEY")]:
        with pytest.raises(ValueError):
            ExtensionRegistry({"tools": [registration(**{key: value})]})
    wrong = copy.deepcopy(registration())
    wrong["effect"] = {"kind": "read", "domain": "e2b", "approval": "none", "retry_safety": "read"}
    with pytest.raises(ValueError, match="E2B requires"):
        ExtensionRegistry({"tools": [wrong]})


async def test_signed_download_is_bounded_and_not_persisted(monkeypatch):
    original = httpx.AsyncClient
    private_url = "https://sandbox.test/file?signature=private-token"
    sandbox = SimpleNamespace(download_url=lambda *args, **kwargs: private_url)
    calls = []

    def request(req):
        calls.append(req)
        return httpx.Response(200, content=b"x" * 5000)

    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(request), **kwargs)
    )
    with pytest.raises(ValueError, match="e2b_output_limit"):
        await download(sandbox, "output", 4096)
    assert len(calls) == 1


async def test_transient_polling_and_kill_errors_do_not_relaunch(store, cloud, monkeypatch):
    original = cloud.connect
    failures = 2

    async def connect(*args, **kwargs):
        nonlocal failures
        if failures:
            failures -= 1
            raise httpx.ConnectError("test-e2b-secret")
        return await original(*args, **kwargs)

    monkeypatch.setattr(cloud, "connect", connect)
    async with http_client(store) as client:
        _, payload = await setup(store, client)
        for _ in range(7):
            assert (await general_action(payload))["external_pending"]
        cloud.kill_failure = True
        assert (await general_action(payload))["external_pending"]
        cloud.kill_failure = False
        result = await finish(payload)
        assert verify_output(result["output"]) == EXPECTED_SUMMARY
        assert cloud.count("create") == cloud.count("run") == 1


async def test_unknown_cleanup_holds_capacity_and_operator_retry_only_terminates(store, cloud):
    async with http_client(store) as client:
        first, second = [await create(client, tools=["e2b_python"]) for _ in range(2)]
        # Lowering the installed ceiling also constrains already-pinned registrations.
        store.extensions = ExtensionRegistry({"tools": [{**registration(), "max_concurrency": 1}]})
        a, b = [action(run.id, "e2b_python", job()) for run in (first, second)]
        assert (await general_action(a))["external_pending"]
        assert (await general_action(b))["external_pending"]
        assert cloud.count("create") == 1
        await client.cancel(first.id)
        cloud.kill_failure = True
        assert not await cleanup_extension(store, first.id, first.id + ":action:0", provider_io=True)
        status = (await client.extension_status())["items"][0]
        assert status["active"] == status["pending_cleanup"] == status["outcome_unknown"] == 1
        assert (await general_action(b))["external_pending"]
        target = (await client.recovery(first.id))["unresolved"][0]
        assert target["kind"] == "tool_cleanup"
        recovery = await client.reconcile(first.id, target, idempotency_key="cleanup")
        assert recovery == await client.reconcile(first.id, target, idempotency_key="cleanup")
        cloud.kill_failure = False
        result = await reconcile_operation({"run_id": first.id, "id": recovery["id"]})
        assert result["result"]["outcome"] == "resolved"
        assert result == await reconcile_operation({"run_id": first.id, "id": recovery["id"]})
        assert cloud.count("run") == 0
        assert (await client.get(first.id)).status == "cancelled"
        assert (await client.extension_status())["items"][0]["active"] == 0
        async with store.database.sessions() as db:
            attempt = await db.get(GeneralAttemptRow, first.id + ":action:0:tool:1")
            assert attempt.data["settled"] and attempt.data["outcome"] == "failed"
        assert verify_output((await finish(b))["output"]) == EXPECTED_SUMMARY
        assert cloud.count("create") == 2


async def test_cancellation_before_acquisition_intent_never_contacts_provider(store, cloud, monkeypatch):
    execute = E2BHandler.execute
    async with http_client(store) as client:
        run, payload = await setup(store, client)

        async def cancelled(handler, call):
            await client.cancel(run.id)
            return await execute(handler, call)

        monkeypatch.setattr(E2BHandler, "execute", cancelled)
        await general_action(payload)
        assert not cloud.calls
        assert await cleanup_extension(store, run.id, run.id + ":action:0", provider_io=True)
        assert (await client.extension_status())["items"][0]["active"] == 0


async def test_provider_rejection_releases_capacity_without_unknown_acquisition(store, cloud, monkeypatch):
    from e2b import AuthenticationException

    async def rejected(**kwargs):
        raise AuthenticationException("test-e2b-secret")

    monkeypatch.setattr(cloud, "create", rejected)
    async with http_client(store) as client:
        _, payload = await setup(store, client)
        result = await general_action(payload)
        assert result["error"] == "e2b_create_rejected"
        assert result["output"]["cleanup_complete"]
        assert (await client.extension_status())["items"][0]["active"] == 0


async def test_credentials_are_redacted_before_handler_state_persistence(store, cloud):
    async with http_client(store) as client:
        run, payload = await setup(store, client)
        for _ in range(3):
            await general_action(payload)
        cloud.content[ROOT + "/result.json"] = json.dumps(
            {"exit_code": 0, "stdout": "test-e2b-secret", "stderr": ""}
        ).encode()
        result = await finish(payload)
        assert result["output"]["receipt"]["stdout"] == "[REDACTED]"
        async with store.database.sessions() as db:
            op = await db.get(GeneralOperationRow, run.id + ":action:0")
            assert "test-e2b-secret" not in json.dumps(op.data)


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
async def test_untrusted_receipt_observations_enforce_utf8_byte_limits(store, cloud, stream):
    async with http_client(store) as client:
        _, payload = await setup(store, client)
        for _ in range(3):
            await general_action(payload)
        cloud.content[ROOT + "/result.json"] = json.dumps(
            {"exit_code": 0, "stdout": "", "stderr": "", stream: "🦉" * 600}, ensure_ascii=False
        ).encode()
        result = await finish(payload)
        assert result["error"] == "e2b_result_invalid" and result["output"]["cleanup_complete"]
        assert (await client.extension_status())["items"][0]["active"] == 0


async def test_json_expansion_cannot_strand_finished_sandbox_or_capacity(store, cloud):
    async with http_client(store) as client:
        _, payload = await setup(store, client)
        for _ in range(3):
            await general_action(payload)
        for name in ("cleaned.csv", "summary.json"):
            cloud.content[ROOT + "/" + name] = b"\0" * 4096
        result = await finish(payload)
        assert result["error"] == "e2b_output_invalid" and result["output"]["cleanup_complete"]
        assert (await client.extension_status())["items"][0]["active"] == 0


@pytest.mark.parametrize("files", [{"a": "x", "a/b": "y"}, {"cleaned.csv/a": "x"}])
async def test_path_collisions_fail_before_acquisition(store, cloud, files):
    async with http_client(store) as client:
        _, payload = await setup(store, client, {**job(), "files": files})
        assert (await general_action(payload))["error"] == "action_failed"
        assert not cloud.calls


async def test_missing_acquisition_requires_explicit_operator_cleanup_evidence(store, cloud):
    from agent_runtime.client import ClientError

    cloud.create_failure, cloud.matches = True, 0
    async with http_client(store) as client:
        run, payload = await setup(store, client)
        with pytest.raises(ApplicationError):
            await general_action(payload)
        assert (await general_action(payload))["error"] == "extension_outcome_unknown"
        await client.cancel(run.id)
        target = (await client.recovery(run.id))["unresolved"][0]
        with pytest.raises(ClientError, match="422"):
            await client.reconcile(run.id, {**target, "kind": "tool_cleanup_attestation"})
        request = {**target, "kind": "tool_cleanup_attestation", "evidence_ref": "provider-audit/ticket-123"}
        recovery = await client.reconcile(run.id, request)
        receipt = await reconcile_operation({"run_id": run.id, "id": recovery["id"]})
        assert receipt["result"]["source"] == "operator_cleanup_attestation"
        assert cloud.count("create") == 1 and cloud.count("run") == cloud.count("kill") == 0
        assert (await client.extension_status())["items"][0]["active"] == 0
        operation = (await client.operations(run.id))["items"][0]
        assert operation["result"]["error"] == "operator_cleanup_attested"
        assert not operation["result"]["output"]["provider_cleanup_confirmed"]
        assert (await client.get(run.id)).status == "cancelled"
        async with store.database.sessions() as db:
            attempts = list(
                await db.scalars(
                    select(GeneralAttemptRow).where(GeneralAttemptRow.operation_id == target["target_id"])
                )
            )
            assert attempts and all(a.data["settled"] and a.data["outcome"] == "failed" for a in attempts)
