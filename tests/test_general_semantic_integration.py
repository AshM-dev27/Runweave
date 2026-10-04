import pytest
from pydantic_ai.durable_exec.temporal import PydanticAIPlugin
from temporalio.worker import Replayer
from test_general_integration import backend
from test_general_semantic import complete, stub, submit

from agent_runtime.general_workflow import GeneralWorkflow

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("program,expected", [("assert 2 + 2 == 4", "completed"), ("assert False", "failed")])
async def test_private_automatic_command_and_replay(pg_store, monkeypatch, program, expected):
    calls = stub(
        monkeypatch,
        lambda context, i: (
            complete() if i == 0 else {"kind": "blocked", "reason": "Registered check failed."}
        ),
    )
    async with backend(pg_store, monkeypatch) as (client, temporal):
        workspace = await client.workspace_create(
            {"main.py": b"x = 1\n", "test_user.py": (program + "\n").encode()}
        )
        run = await submit(
            client,
            ["workspace_verify"],
            workspace,
            {
                "outcome": "Check project",
                "criteria": [
                    {
                        "id": "user",
                        "statement": "Caller check must pass",
                        "evidence_policy": "check",
                        "checks": [
                            {"id": "user.check", "kind": "command", "argv": ["python", "test_user.py"]}
                        ],
                    }
                ],
            },
        )
        result = await client.wait(run.id, timeout=60)
        assert result.status == expected, result.error
        evidence = (await client.verifications(run.id))["items"]
        assert len(evidence) == 1
        assert evidence[0]["outcome"] == ("pass" if expected == "completed" else "fail")
        assert evidence[0]["provenance"] == "user"
        assert len(calls) == (1 if expected == "completed" else 2)
        assert (await client.budget(run.id))["v3"]["counters"]["command_attempts"] == 1
        history = await temporal.get_workflow_handle("run:" + run.id).fetch_history()
        await Replayer(workflows=[GeneralWorkflow], plugins=[PydanticAIPlugin()]).replay_workflow(history)


async def test_private_write_auto_syntax_and_user_test_integrity(pg_store, monkeypatch):
    stub(
        monkeypatch,
        [
            {"kind": "write", "files": [{"path": "test_user.py", "content_base64": "cGFzcwo="}]},
            complete(),
            {"kind": "blocked", "reason": "Imported check integrity failed."},
        ],
    )
    async with backend(pg_store, monkeypatch) as (client, _):
        workspace = await client.workspace_create({"test_user.py": b"assert False\n"})
        run = await submit(
            client,
            ["workspace_write", "workspace_verify"],
            workspace,
            {
                "outcome": "Check project",
                "criteria": [
                    {
                        "id": "user",
                        "statement": "Caller test",
                        "evidence_policy": "check",
                        "checks": [{"id": "user.check", "kind": "command", "argv": ["pytest", "-q"]}],
                    }
                ],
            },
        )
        result = await client.wait(run.id, timeout=60)
        assert result.status == "failed"
        operations = (await client.operations(run.id))["items"]
        assert any(o["result"] and o["result"].get("error") == "input_check_modified" for o in operations)
        assert not (await client.task(run.id))["assessment"]["accepted"]


async def test_private_new_output_automatic_syntax(pg_store, monkeypatch):
    stub(
        monkeypatch,
        [
            {"kind": "write", "files": [{"path": "result.py", "content_base64": "dmFsdWUgPSAxMgo="}]},
            complete(),
        ],
    )
    async with backend(pg_store, monkeypatch) as (client, _):
        workspace = await client.workspace_create({})
        run = await submit(client, ["workspace_write", "workspace_verify"], workspace)
        result = await client.wait(run.id, timeout=60)
        assert result.status == "completed", result.error
        assert (
            await client.workspace_read(
                workspace["workspace_id"], result.workspace["revision_id"], "result.py"
            )
            == b"value = 12\n"
        )
        evidence = (await client.verifications(run.id))["items"]
        assert len(evidence) == 1 and evidence[0]["check_id"] == "runtime.syntax"


async def test_private_child_new_output_merge_and_allocation(pg_store, monkeypatch):
    async def action(context, _):
        last = context["last_result"] or {}
        if context["input"] == "semantic: child output":
            return (
                complete()
                if last
                else {"kind": "write", "files": [{"path": "new.txt", "content_base64": "bmV3"}]}
            )
        if not last:
            return {
                "kind": "assign",
                "assignments": [
                    {
                        "role": "writer",
                        "objective": "semantic: child output",
                        "acceptance": ["Create new output"],
                        "capabilities": ["workspace_write"],
                        "inputs": [],
                        "outputs": ["new.txt"],
                    }
                ],
            }
        if "children" in last:
            import asyncio

            await asyncio.sleep(1)  # Hold the parent reservation while its child starts.
            return {"kind": "join", "children": ["d0"]}
        if "join" in last:
            return {"kind": "merge", "child": "d0"}
        return complete()

    calls = stub(monkeypatch, action)
    from agent_runtime.general_contracts import GeneralPolicy

    async with backend(pg_store, monkeypatch) as (client, temporal):
        workspace = await client.workspace_create({"base.txt": b"base"})
        run = await submit(
            client,
            ["workspace_write", "workspace_verify"],
            workspace,
            policy=GeneralPolicy(delegation={"tools": ["workspace_write", "workspace_verify"]}),
        )
        result = await client.wait(run.id, timeout=90)
        assert result.status == "completed", (result.error, await client.operations(run.id))
        children = await client.children(run.id)
        assert len(children) == 1 and children[0].status == "completed"
        child_state = await pg_store.general(children[0].id)
        assert child_state["assignment"]["tools"] == ["workspace_write"]
        assert child_state["effective_capabilities"] == ["workspace_write", "workspace_verify"]
        assert any(
            v["check_id"] == "runtime.syntax" and v["outcome"] == "pass"
            for v in (await client.verifications(children[0].id))["items"]
        )
        assert (
            await client.workspace_read(workspace["workspace_id"], result.workspace["revision_id"], "new.txt")
            == b"new"
        )
        root_checks = (await client.verifications(run.id))["items"]
        assert root_checks and all(v["revision_id"] == result.workspace["revision_id"] for v in root_checks)
        assert any(c["children"] for c in calls)
        assert all("allocation" not in child for c in calls for child in c["children"].values())
        assert (await client.resources(children[0].id))["child_estimate"]["model_attempts"] == 4
        assert len(calls) == 6
        assert (await client.budget(run.id))["v3"]["counters"]["model_attempts"] == 6
        history = await temporal.get_workflow_handle("run:" + run.id).fetch_history()
        await Replayer(workflows=[GeneralWorkflow], plugins=[PydanticAIPlugin()]).replay_workflow(history)


@pytest.mark.parametrize("fault", ["broker", "worker", "cancel"])
async def test_private_automatic_check_faults(pg_store, monkeypatch, fault):
    import asyncio
    import os
    import sys
    from pathlib import Path

    from test_general_semantic import http_client

    from agent_runtime.project_store import digest

    monkeypatch.setenv("SANDBOX_BROKER_URL", "http://localhost:18091")
    monkeypatch.setenv("SANDBOX_BROKER_KEY", "v3-isolated-test-only")
    env = {
        "PATH": os.environ["PATH"],
        "PYTHONPATH": str(Path.cwd()),
        "DATABASE_URL": pg_store.test_url,
        "DATABASE_SCHEMA": pg_store.test_schema,
        "TASK_QUEUE": pg_store.test_schema,
        "SANDBOX_BROKER_URL": "http://localhost:18091",
        "SANDBOX_BROKER_KEY": "v3-isolated-test-only",
    }

    async def launch():
        return await asyncio.create_subprocess_exec(
            sys.executable,
            "tests/semantic_worker.py",
            env=env,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )

    worker = await launch()
    try:
        async with http_client(pg_store) as client:
            workspace = await client.workspace_create(
                {"test_user.py": b"import time\ntime.sleep(6)\nassert True\n"}
            )
            run = await submit(
                client,
                ["workspace_verify"],
                workspace,
                {
                    "outcome": "Verify registered test",
                    "criteria": [
                        {
                            "id": "user",
                            "statement": "Caller test passes",
                            "evidence_policy": "check",
                            "checks": [
                                {"id": "user.check", "kind": "command", "argv": ["python", "test_user.py"]}
                            ],
                        }
                    ],
                },
            )
            name = "agents-project-v3-" + digest((run.id + ":action:0:command:1").encode())
            for _ in range(200):
                p = await asyncio.create_subprocess_exec(
                    "sudo",
                    "-n",
                    "docker",
                    "inspect",
                    "--format",
                    "{{.State.Running}}",
                    name,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                out, _ = await p.communicate()
                if out.strip() == b"true":
                    break
                assert worker.returncode is None
                await asyncio.sleep(0.1)
            else:
                pytest.fail("Owned command container not observed")
            # Inspect the operation while it has intent but no receipt.
            pending = (await client.operations(run.id))["items"]
            assert any(o["status"] == "pending" and o["result"] is None for o in pending)
            if fault == "worker":
                worker.kill()
                await worker.wait()
                worker = await launch()
            elif fault == "broker":
                for args in [("kill", "--signal", "KILL"), ("start",)]:
                    p = await asyncio.create_subprocess_exec(
                        "sudo",
                        "-n",
                        "docker",
                        *args,
                        os.environ.get("TEST_PROJECT_BROKER_NAME", "agent-runtime-v3-test-broker"),
                        stdout=asyncio.subprocess.DEVNULL,
                    )
                    assert await p.wait() == 0
            else:
                await client.cancel(run.id)
            result = await client.wait(run.id, timeout=150)
            assert result.status == ("cancelled" if fault == "cancel" else "completed"), result.error
            assert result.cleanup_state == "complete"
            receipts = (await client.verifications(run.id))["items"]
            assert len(receipts) == (0 if fault == "cancel" else 1)
            assert (await client.budget(run.id))["v3"]["counters"]["command_attempts"] == (
                2 if fault == "broker" else 1
            )
    finally:
        if worker.returncode is None:
            worker.terminate()
            await worker.wait()


@pytest.mark.parametrize("semantic_v1", [False, True])
async def test_pre_correction_v3_history_replays(pg_store, semantic_v1):
    import asyncio

    from legacy_general_workflow import LegacyGeneralWorkflow
    from legacy_semantic_workflow import LegacySemanticWorkflow
    from temporalio.client import Client as TemporalClient
    from temporalio.worker import Worker
    from test_general_semantic import http_client

    from agent_runtime.dispatch import Dispatcher
    from agent_runtime.general_contracts import GeneralPolicy
    from agent_runtime.general_runtime import GENERAL_ACTIVITIES
    from agent_runtime.schemas import AgentConfig

    temporal = await TemporalClient.connect("localhost:7233", plugins=[PydanticAIPlugin()])
    queue = pg_store.test_schema
    async with Worker(
        temporal,
        task_queue=queue + "-v3",
        workflows=[LegacySemanticWorkflow if semantic_v1 else LegacyGeneralWorkflow],
        activities=GENERAL_ACTIVITIES,
    ):
        dispatch = asyncio.create_task(Dispatcher(pg_store, temporal, queue).run())
        try:
            async with http_client(pg_store) as client:
                agent = await client.create_agent(
                    AgentConfig(
                        name="legacy-v3",
                        provider="fake",
                        model="deterministic",
                        tools=[],
                        general=GeneralPolicy(),
                    )
                )
                run = await client.submit(agent.id, "Explain a tree")
                assert (await client.wait(run.id)).status == "completed"
                handle = temporal.get_workflow_handle("run:" + run.id)
                await handle.result()
                history = await handle.fetch_history()
                assert (
                    any(e.HasField("marker_recorded_event_attributes") for e in history.events) == semantic_v1
                )
                await Replayer(workflows=[GeneralWorkflow], plugins=[PydanticAIPlugin()]).replay_workflow(
                    history
                )
        finally:
            dispatch.cancel()
            await asyncio.gather(dispatch, return_exceptions=True)


@pytest.mark.parametrize("passes", [True, False])
async def test_final_reporting_reserve_runs_checks_without_model_request(pg_store, monkeypatch, passes):
    from agent_runtime.general_contracts import GeneralPolicy

    calls = stub(monkeypatch, [complete()])
    async with backend(pg_store, monkeypatch) as (client, _):
        workspace = await client.workspace_create({"out.txt": b"12"})
        run = await submit(
            client,
            ["workspace_verify"],
            workspace,
            {
                "outcome": "Verify",
                "criteria": [
                    {
                        "id": "user",
                        "statement": "Expected data",
                        "evidence_policy": "check",
                        "checks": [
                            {
                                "id": "bytes",
                                "kind": "bytes",
                                "path": "out.txt",
                                "expected": "MTI=" if passes else "MTM=",
                            }
                        ],
                    }
                ],
            },
            policy=GeneralPolicy(resources=None, limits={"model_attempts": 1}),
        )
        result = await client.wait(run.id, timeout=30)
        assert result.status == ("completed" if passes else "failed"), result.error
        assert len(calls) == 1
        assert len((await client.verifications(run.id))["items"]) == 1
        if not passes:
            assert result.stop_reason == "budget_exhausted"
