"""Public contracts, durable ownership and recovery with an unpaid remote computer."""

import copy
import json
import re
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from temporalio.exceptions import ApplicationError
from test_e2b import Cloud, finish
from test_general_semantic import http_client
from test_harness_extensions import action

from agent_runtime.computer_db import ComputerSessionRow
from agent_runtime.computer_runtime import close_computer
from agent_runtime.computer_store import OperationComputers
from agent_runtime.db import now
from agent_runtime.e2b import ROOT
from agent_runtime.extension_runtime import cleanup_extension
from agent_runtime.extensions import ExtensionRegistry
from agent_runtime.general_contracts import GeneralPolicy
from agent_runtime.general_db import GeneralOperationRow
from agent_runtime.general_runtime import general_action, general_step
from agent_runtime.runtime import configure_store
from agent_runtime.schemas import AgentConfig
from agent_runtime.store import Store


def registration(**config):
    manifest = json.loads(Path("config/extensions.json").read_text())
    definition = copy.deepcopy(next(t for t in manifest["tools"] if t["alias"] == "computer_python"))
    definition["config"].update(config)
    return definition


def job(computer="work"):
    return {
        "computer": computer,
        "code": "untrusted fake program",
        "artifacts": {},
        "outputs": {"counter.json": {"filename": "counter.json", "media_type": "application/json"}},
    }


class Remote(Cloud):
    def __init__(self, identity):
        super().__init__()
        self.sandbox_id = identity
        self.counter = 0

    async def make_dir(self, path, **kwargs):
        self.calls.append(("mkdir", path))

    async def run(self, cmd, **kwargs):
        self.calls.append(("run", cmd))
        self.counter += 1
        control = re.fullmatch(r"python -I (.+)/runner.py", cmd)[1]
        if not self.missing_receipt:
            self.content[control + "/result.json"] = json.dumps(
                {"exit_code": self.exit_code, "stdout": str(self.counter), "stderr": ""}
            ).encode()
        self.content[ROOT + "/counter.json"] = json.dumps({"counter": self.counter}).encode()
        if self.command_failure:
            raise TimeoutError("test-e2b-secret")
        return SimpleNamespace(exit_code=0)


class Computers:
    def __init__(self):
        self.machines = {}
        self.lose_create = False
        self.no_matches = False
        self.reject_create = False

    async def create(self, **kwargs):
        if self.reject_create:
            from e2b import AuthenticationException

            raise AuthenticationException("test-e2b-secret")
        machine = Remote("computer-" + str(len(self.machines)))
        machine.metadata = kwargs["metadata"]
        machine.calls.append(("create", kwargs))
        self.machines[machine.sandbox_id] = machine
        if self.lose_create:
            raise TimeoutError("test-e2b-secret")
        return machine

    async def connect(self, identity, **kwargs):
        return await self.machines[identity].connect(identity, **kwargs)

    async def kill(self, identity, **kwargs):
        return await self.machines[identity].kill(identity, **kwargs)

    def list(self, **kwargs):
        async def next_items():
            return (
                []
                if self.no_matches
                else [
                    m
                    for m in self.machines.values()
                    if m.metadata == kwargs["query"]["metadata"] and not m.killed
                ]
            )

        return SimpleNamespace(next_items=next_items, has_next=False)

    def count(self, name):
        return sum(machine.count(name) for machine in self.machines.values())


@pytest.fixture
def computers(monkeypatch):
    factory = Computers()

    async def download(machine, path, limit):
        return await machine.download(machine, path, limit)

    async def chunks(machine, path, limit):
        yield await download(machine, path, limit)

    async def upload(machine, path, source):
        machine.content[path] = source.read_bytes()

    monkeypatch.setenv("E2B_API_KEY", "test-e2b-secret")
    monkeypatch.setattr("agent_runtime.e2b.sandbox_class", lambda: factory)
    monkeypatch.setattr("agent_runtime.e2b.download", download)
    monkeypatch.setattr("agent_runtime.e2b_artifacts.download_chunks", chunks)
    monkeypatch.setattr("agent_runtime.e2b_artifacts.upload_file", upload)
    return factory


async def submit(client, *, session_id=None, computer="work", script=False):
    agent = await client.create_agent(
        AgentConfig(
            name="computer",
            provider="fake",
            model="deterministic",
            tools=["computer_python"],
            general=GeneralPolicy(),
        )
    )
    prompt = (
        "general:"
        + json.dumps(
            [{"action": {"kind": "invoke", "capability": "computer_python", "arguments": job(computer)}}]
        )
        if script
        else "continue computer work"
    )
    run = await client.submit(agent.id, prompt, session_id=session_id)
    return run, action(run.id, "computer_python", job(computer))


@pytest.mark.parametrize("invalid", ["media", "path"])
async def test_invalid_job_rejected_before_external_intent_or_admission(store, computers, invalid):
    async with http_client(store) as client:
        run, payload = await submit(client)
        arguments = payload["decision"]["action"]["arguments"]
        if invalid == "media":
            arguments["outputs"]["counter.json"]["media_type"] = "text/html"
        else:
            arguments["outputs"]["../escape.json"] = arguments["outputs"].pop("counter.json")
        result = await general_action(payload)
        assert result["error"] == "action_failed"
        assert computers.count("create") == computers.count("run") == 0
        assert (await client.extension_status())["items"][0]["active"] == 0
        async with store.database.sessions() as db:
            operation = await db.get(GeneralOperationRow, run.id + ":action:0")
            assert not operation.data.get("external") and not operation.data.get("started")


async def test_failed_command_cannot_be_reported_as_success_by_completion_assessment(store, computers):
    from agent_runtime.client_errors import ClientError

    async with http_client(store) as client:
        run, payload = await submit(client)
        await general_action(payload)
        next(iter(computers.machines.values())).exit_code = 1
        assert (await finish(payload))["error"] == "e2b_command_failed"
        assert (await client.computers(run.session_id))[0].status == "closed"
        completion = await general_step(run.id)
        assert (await general_action({"run_id": run.id, **completion}))["accepted"]
        await store.general_cleanup(run.id, provider_io=True)
        result = await client.result(run.id)
        assert result.status == "completed"
        assert (result.outcome, result.outcome_reason) == ("needs_attention", "tool_error")
        assert "tool errors" in result.message
        terminal = next(e for e in await store.events(run.id) if e.type == "run.completed")
        assert terminal.data["outcome"] == "needs_attention"
        with pytest.raises(ClientError) as denied:
            await client.evidence(run.id)
        assert denied.value.status_code == 409
        # Current receipts also protect historical runs without a stored issue map.
        assert (await store.get(run.id)).outcome == "needs_attention"


async def test_reuse_across_actions_and_later_tasks_keeps_one_machine(store, computers):
    async with http_client(store) as client:
        run, payload = await submit(client)
        first = await finish(payload)
        second = await finish(action(run.id, "computer_python", job(), step=1))
        assert first["output"]["session_retained"] and second["output"]["receipt"]["stdout"] == "2"
        await store.general_stop(run.id, "fixture_finished")
        later, payload = await submit(client, session_id=run.session_id)
        third = await finish(payload)
        assert third["output"]["receipt"]["stdout"] == "3"
        assert third["output"]["computer_id"] == first["output"]["computer_id"]
        assert computers.count("create") == 1 and computers.count("run") == 3 and computers.count("kill") == 0
        rows = await client.computers(run.session_id)
        assert len(rows) == 1 and rows[0].operation_count == 3 and rows[0].status == "ready"
        assert (await client.extension_status())["items"][0]["active"] == 1
        assert len((await client.get(later.id)).artifacts) == 1
        raw = (await client.http.get("/v1/computers/" + rows[0].id)).json()
        assert not any(
            key in json.dumps(raw) for key in ["sandbox_id", "test-e2b-secret", "definition", "holder"]
        )


async def test_same_name_in_other_conversation_cannot_reuse_machine(store, computers):
    async with http_client(store) as client:
        one, payload = await submit(client)
        first = await finish(payload)
        two, payload = await submit(client)
        second = await finish(payload)
        assert first["output"]["computer_id"] != second["output"]["computer_id"]
        assert first["output"]["receipt"]["stdout"] == second["output"]["receipt"]["stdout"] == "1"
        assert computers.count("create") == 2
        assert (await client.computers(one.session_id))[0].id != (await client.computers(two.session_id))[
            0
        ].id


async def test_cancelled_holder_blocks_later_task_until_cleanup_without_extra_capacity(store, computers):
    async with http_client(store) as client:
        one, first = await submit(client)
        assert (await general_action(first))["external_pending"]
        await client.cancel(one.id)
        _, second = await submit(client, session_id=one.session_id)
        for _ in range(3):
            assert (await general_action(second))["external_pending"]
        assert computers.count("create") == 1 and computers.count("run") == 0
        assert await cleanup_extension(store, one.id, one.id + ":action:0", provider_io=True)
        assert (await finish(second))["error"] == "computer_session_unavailable"
        assert computers.count("run") == 0
        assert (await client.extension_status())["items"][0]["active"] == 0


async def test_lost_dispatch_and_worker_replacement_reuse_unique_receipt(store, computers):
    async with http_client(store) as client:
        run, payload = await submit(client)
        await finish(payload)
        machine = next(iter(computers.machines.values()))
        machine.command_failure = True
        machine.missing_receipt = True
        second = action(run.id, "computer_python", job(), step=1)
        for _ in range(3):
            assert (await general_action(second))["external_pending"]
        assert machine.count("run") == 2
        assert (await general_action(second))[
            "external_pending"
        ]  # An earlier operation's receipt is ignored.
        machine.missing_receipt = False
        commands = [args for name, args in machine.calls if name == "run"]
        assert commands[0] != commands[1]
        path = commands[-1].removeprefix("python -I ").removesuffix("/runner.py") + "/result.json"
        machine.content[path] = b'{"exit_code":0,"stdout":"2","stderr":""}'
        replacement = Store(store.database, extensions=store.extensions, blobs=store.blobs)
        configure_store(replacement)
        try:
            assert (await finish(second))["output"]["receipt"]["stdout"] == "2"
            assert computers.count("create") == 1 and computers.count("run") == 2
        finally:
            configure_store(store)


async def test_lost_create_recovers_original_machine_without_creating_again(store, computers):
    computers.lose_create = True
    async with http_client(store) as client:
        _, payload = await submit(client)
        with pytest.raises(ApplicationError, match="extension_reconcile_pending"):
            await general_action(payload)
        result = await finish(payload)
        assert result["output"]["session_retained"]
        assert computers.count("create") == computers.count("run") == 1


async def test_unknown_acquisition_and_failed_cleanup_retain_capacity(store, computers):
    computers.lose_create = computers.no_matches = True
    async with http_client(store) as client:
        run, payload = await submit(client)
        with pytest.raises(ApplicationError, match="extension_reconcile_pending"):
            await general_action(payload)
        row = (await client.computers(run.session_id))[0]
        await client.close_computer(row.id)
        assert not await close_computer(store, row.id)
        assert (await client.computer(row.id)).status == "unknown"
        assert (await client.extension_status())["items"][0]["active"] == 1
        computers.no_matches = False
        assert await close_computer(store, row.id)
        assert (await client.extension_status())["items"][0]["active"] == 0


async def test_explicit_close_is_idempotent_and_closed_name_never_recreates(store, computers):
    async with http_client(store) as client:
        run, payload = await submit(client)
        result = await finish(payload)
        identity = result["output"]["computer_id"]
        assert (await client.close_computer(identity)).status == "closing"
        assert (await client.close_computer(identity)).status == "closing"
        assert await close_computer(store, identity)
        assert await close_computer(store, identity)
        assert (await client.close_computer(identity)).status == "closed"
        await store.general_stop(run.id, "fixture_finished")
        later, payload = await submit(client, session_id=run.session_id)
        assert (await finish(payload))["error"] == "computer_session_unavailable"
        assert computers.count("create") == computers.count("kill") == 1
        assert (await client.extension_status())["items"][0]["active"] == 0


async def test_cancelled_operation_closes_computer_and_cannot_execute(store, computers):
    async with http_client(store) as client:
        run, payload = await submit(client)
        assert (await general_action(payload))["external_pending"]
        await client.cancel(run.id)
        assert await cleanup_extension(store, run.id, run.id + ":action:0", provider_io=True)
        assert computers.count("run") == 0 and computers.count("kill") == 1
        assert (await client.computers(run.session_id))[0].status == "closed"


async def test_cleanup_cannot_kill_machine_reused_by_another_task(store, computers):
    async with http_client(store) as client:
        one, payload = await submit(client)
        await finish(payload)
        await store.general_stop(one.id, "fixture_finished")
        two, second = await submit(client, session_id=one.session_id)
        await general_action(second)
        async with store.database.sessions() as db:
            old = await db.get(GeneralOperationRow, one.id + ":action:0")
            access = OperationComputers(store, one.id, old.id, old.data["owner"], cleanup=True)
        from agent_runtime.store import Problem

        with pytest.raises(Problem):
            await access.close((await client.computers(one.session_id))[0].id)
        assert computers.count("kill") == 0


@pytest.mark.parametrize("limit", ["max_operations", "max_sessions"])
async def test_session_limits_are_operator_owned(store, computers, limit):
    store.extensions = ExtensionRegistry({"tools": [registration(**{limit: 1})]})
    async with http_client(store) as client:
        run, payload = await submit(client)
        await finish(payload)
        await store.general_stop(run.id, "fixture_finished")
        later, payload = await submit(
            client, session_id=run.session_id, computer="other" if limit == "max_sessions" else "work"
        )
        result = await finish(payload)
        assert result["error"] == (
            "computer_session_limit" if limit == "max_sessions" else "computer_operation_limit"
        )
        assert computers.count("create") == computers.count("run") == 1


async def test_expired_or_changed_policy_never_recreates_a_computer(store, computers):
    async with http_client(store) as client:
        run, payload = await submit(client)
        result = await finish(payload)
        await store.general_stop(run.id, "fixture_finished")
        store.extensions = ExtensionRegistry({"tools": [registration(memory_mb=1024)]})
        later, payload = await submit(client, session_id=run.session_id)
        assert (await finish(payload))["error"] == "computer_policy_changed"
        await store.general_stop(later.id, "fixture_finished")
        async with store.database.sessions.begin() as db:
            row = await db.get(ComputerSessionRow, result["output"]["computer_id"])
            row.expires_at = now() - timedelta(seconds=1)
        later, payload = await submit(client, session_id=run.session_id)
        assert (await finish(payload))["error"] == "computer_session_unavailable"
        assert computers.count("create") == 1


async def test_provider_rejection_releases_capacity_without_uncertain_cleanup(store, computers):
    computers.reject_create = True
    async with http_client(store) as client:
        run, payload = await submit(client)
        assert (await finish(payload))["error"] == "e2b_create_rejected"
        assert (await client.computers(run.session_id))[0].status == "closed"
        assert (await client.extension_status())["items"][0]["active"] == 0


async def test_missing_ungranted_input_is_rejected_before_session_admission(store, computers):
    async with http_client(store) as client:
        run, payload = await submit(client)
        payload["decision"]["action"]["arguments"]["artifacts"] = {"secret.bin": "missing"}
        assert (await finish(payload))["error"] == "artifact_not_authorized"
        assert await client.computers(run.session_id) == []
        assert computers.count("create") == 0


async def test_release_response_loss_recovers_finished_receipt_without_reacquiring(
    store, computers, monkeypatch
):
    original = OperationComputers.ready
    lost = False

    async def lose_response(self, *args):
        nonlocal lost
        result = await original(self, *args)
        if not lost:
            lost = True
            raise TimeoutError("lost after exclusive use released")
        return result

    monkeypatch.setattr(OperationComputers, "ready", lose_response)
    async with http_client(store) as client:
        _, payload = await submit(client)
        assert (await finish(payload))["output"]["session_retained"]
        assert computers.count("create") == computers.count("run") == 1
        assert (await client.extension_status())["items"][0]["active"] == 1


async def test_warm_computer_keeps_shared_capacity_and_can_be_reused_when_pool_full(store, computers):
    from test_e2b import registration as legacy_registration

    from examples.e2b_invoice import job as legacy_job

    store.extensions = ExtensionRegistry(
        {"tools": [registration(), {**legacy_registration(), "max_concurrency": 1}]}
    )
    async with http_client(store) as client:
        run, payload = await submit(client)
        first = await finish(payload)
        await store.general_stop(run.id, "fixture_finished")
        assert (await client.extension_status())["items"][0]["pending_cleanup"] == 0
        agent = await client.create_agent(
            AgentConfig(
                name="legacy",
                provider="fake",
                model="deterministic",
                tools=["e2b_python"],
                general=GeneralPolicy(),
            )
        )
        legacy = await client.submit(agent.id, "wait")
        pending = action(legacy.id, "e2b_python", legacy_job())
        assert (await general_action(pending))["external_pending"]
        later, payload = await submit(client, session_id=run.session_id)
        assert (await finish(payload))["output"]["receipt"]["stdout"] == "2"
        assert computers.count("create") == 1
        await client.close_computer(first["output"]["computer_id"])
        assert await close_computer(store, first["output"]["computer_id"])
        assert (await general_action(pending))["external_pending"]
        assert computers.count("create") == 2


async def test_unknown_computer_cleanup_accepts_explicit_audited_attestation(store, computers):
    computers.lose_create = computers.no_matches = True
    async with http_client(store) as client:
        run, payload = await submit(client)
        with pytest.raises(ApplicationError, match="extension_reconcile_pending"):
            await general_action(payload)
        identity = (await client.computers(run.session_id))[0].id
        await client.close_computer(identity)
        assert not await close_computer(store, identity)
        result = await client.attest_computer_cleanup(
            identity, "operator-evidence:termination-123", idempotency_key="attestation"
        )
        assert result.status == "closed"
        assert result == await client.attest_computer_cleanup(
            identity, "operator-evidence:termination-123", idempotency_key="attestation"
        )
        from agent_runtime.client import ClientError

        with pytest.raises(ClientError):
            await client.attest_computer_cleanup(
                identity, "different-evidence", idempotency_key="attestation"
            )
        assert (await client.extension_status())["items"][0]["active"] == 0
        assert computers.count("kill") == 0
        events = await store.events(run.id)
        assert sum(e.type == "computer.cleanup_attested" for e in events) == 1
        assert (
            next(e for e in events if e.type == "computer.cleanup_attested").data[
                "provider_cleanup_confirmed"
            ]
            is False
        )


async def test_ready_computer_cannot_be_attested_away(store, computers):
    from agent_runtime.client import ClientError

    async with http_client(store) as client:
        run, payload = await submit(client)
        await finish(payload)
        identity = (await client.computers(run.session_id))[0].id
        with pytest.raises(ClientError):
            await client.attest_computer_cleanup(identity, "unproved", idempotency_key="reject")
        assert (await client.extension_status())["items"][0]["active"] == 1


async def test_cancel_after_claim_before_handler_state_reclaims_reused_computer(
    store, computers, monkeypatch
):
    async with http_client(store) as client:
        run, payload = await submit(client)
        await finish(payload)
        original = OperationComputers.claim

        async def interrupt(self, *args, **kwargs):
            await original(self, *args, **kwargs)
            raise TimeoutError("lost claim response")

        monkeypatch.setattr(OperationComputers, "claim", interrupt)
        second = action(run.id, "computer_python", job(), step=1)
        with pytest.raises(ApplicationError, match="extension_reconcile_pending"):
            await general_action(second)
        await client.cancel(run.id)
        assert await cleanup_extension(store, run.id, run.id + ":action:1", provider_io=True)
        assert computers.count("run") == computers.count("kill") == 1


async def test_replacement_waits_for_old_lease_without_new_provider_io(store, computers):
    import time

    async with http_client(store) as client:
        run, payload = await submit(client)
        assert (await general_action(payload))["external_pending"]
        async with store.database.sessions.begin() as db:
            op = await db.get(GeneralOperationRow, run.id + ":action:0")
            op.data = {**op.data, "lease": time.time() + 60}
        before = computers.count("create"), computers.count("run")
        waiting = await general_action(payload)
        assert waiting["external_pending"] and 2 <= waiting["retry_after"] <= 30
        assert before == (computers.count("create"), computers.count("run"))


async def test_close_winning_before_dispatch_prevents_new_command(store, computers, monkeypatch):
    async with http_client(store) as client:
        run, payload = await submit(client)
        assert (await general_action(payload))["external_pending"]
        assert (await general_action(payload))["external_pending"]
        original = OperationComputers.current

        async def close_after_read(self, computer_id):
            state = await original(self, computer_id)
            await store.computer_close(computer_id)
            return state

        monkeypatch.setattr(OperationComputers, "current", close_after_read)
        await general_action(payload)
        assert computers.count("run") == 0
        await client.cancel(run.id)
        assert await cleanup_extension(store, run.id, run.id + ":action:0", provider_io=True)


async def test_finished_receipt_cleanup_does_not_kill_later_holder(store, computers):
    async with http_client(store) as client:
        first, payload = await submit(client)
        await finish(payload)
        await store.general_stop(first.id, "fixture_finished")
        second, next_payload = await submit(client, session_id=first.session_id)
        await general_action(next_payload)
        async with store.database.sessions.begin() as db:
            old = await db.get(GeneralOperationRow, first.id + ":action:0")
            old.data = {**old.data, "result": None, "status": "outcome_unknown"}
        assert await cleanup_extension(store, first.id, first.id + ":action:0", provider_io=True)
        assert computers.count("kill") == 0
        assert (await finish(next_payload))["output"]["receipt"]["stdout"] == "2"
