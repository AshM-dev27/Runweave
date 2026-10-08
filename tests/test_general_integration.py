import asyncio
import base64
import json
import os
from contextlib import asynccontextmanager

import httpx
import pytest
from pydantic_ai.durable_exec.temporal import PydanticAIPlugin
from temporalio.client import Client as TemporalClient
from temporalio.worker import Worker

from agent_runtime.api import create_app
from agent_runtime.client import Client
from agent_runtime.dispatch import Dispatcher
from agent_runtime.general_contracts import GeneralPolicy
from agent_runtime.general_runtime import GENERAL_ACTIVITIES
from agent_runtime.general_workflow import (
    ComputerCleanupWorkflow,
    ExtensionCleanupWorkflow,
    GeneralWorkflow,
    ReconciliationWorkflow,
)
from agent_runtime.schemas import AgentConfig

pytestmark = pytest.mark.integration


@asynccontextmanager
async def backend(store, monkeypatch):
    monkeypatch.setenv("SANDBOX_BROKER_URL", "http://localhost:18091")
    monkeypatch.setenv("SANDBOX_BROKER_KEY", "v3-isolated-test-only")
    temporal = await TemporalClient.connect("localhost:7233", plugins=[PydanticAIPlugin()])
    queue = store.test_schema
    async with Worker(
        temporal,
        task_queue=queue + "-v3",
        workflows=[
            GeneralWorkflow,
            ReconciliationWorkflow,
            ExtensionCleanupWorkflow,
            ComputerCleanupWorkflow,
        ],
        activities=GENERAL_ACTIVITIES,
    ):
        dispatch = asyncio.create_task(Dispatcher(store, temporal, queue).run())
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=create_app(store, "test")),
                base_url="http://test",
                headers={"Authorization": "Bearer test"},
            ) as http:
                yield Client(http_client=http), temporal
        finally:
            dispatch.cancel()
            await asyncio.gather(dispatch, return_exceptions=True)


def invoke(name, **args):
    return {"action": {"kind": "invoke", "capability": name, "arguments": args}}


async def test_actual_project_command_verify_download(pg_store, monkeypatch):
    async with backend(pg_store, monkeypatch) as (client, temporal):
        workspace = await client.workspace_create({"main.py": b"x = 1\n"})
        config = AgentConfig(
            name="project",
            provider="fake",
            model="deterministic",
            tools=["workspace_command", "workspace_verify"],
            general=GeneralPolicy(),
        )
        agent = await client.create_agent(config)
        script = [
            invoke(
                "workspace_command",
                expected_revision="$HEAD",
                argv=[
                    "python",
                    "-c",
                    "from pathlib import Path; Path('main.py').write_text('x = 2\\n'); print('edited')",
                ],
                commit=True,
            ),
            invoke("workspace_verify", expected_revision="$HEAD", check_id="runtime.syntax"),
        ]
        run = await client.submit(agent.id, "general:" + json.dumps(script), workspace=workspace)
        result = await client.wait(run.id, timeout=45)
        assert result.status == "completed", result.model_dump()
        assert (
            await client.workspace_read(workspace["workspace_id"], result.workspace["revision_id"], "main.py")
            == b"x = 2\n"
        )
        verifications = (await client.verifications(run.id))["items"]
        assert len(verifications) == 1 and verifications[0]["outcome"] == "pass"
        assert verifications[0]["details"]["image_digest"].startswith("sha256:")
        assert await client.operation_output(run.id, run.id + ":action:0", "stdout") == b"edited\n"
        assert (await client.budget(run.id))["v3"]["counters"]["command_attempts"] == 2
        history = await temporal.get_workflow_handle("run:" + run.id).fetch_history()
        assert len(history.events) > 10


async def test_actual_dynamic_children(pg_store, monkeypatch):
    async with backend(pg_store, monkeypatch) as (client, temporal):
        workspace = await client.workspace_create({"base.txt": b"original"})
        config = AgentConfig(
            name="dynamic",
            provider="fake",
            model="deterministic",
            tools=["workspace_write", "workspace_verify"],
            general=GeneralPolicy(delegation={"tools": ["workspace_write", "workspace_verify"]}),
        )
        agent = await client.create_agent(config)
        assignments = []
        for name in ["left", "right"]:
            script = [
                invoke(
                    "workspace_write",
                    expected_revision="$HEAD",
                    writes=[
                        {
                            "path": name + ".txt",
                            "expected_sha256": None,
                            "content_base64": base64.b64encode(name.encode()).decode(),
                        }
                    ],
                ),
                invoke("workspace_verify", expected_revision="$HEAD", check_id="runtime.syntax"),
            ]
            assignments.append(
                {
                    "role": name,
                    "objective": "general:" + json.dumps(script),
                    "criteria": [{"id": "child", "statement": "Create assigned text"}],
                    "tools": config.tools,
                    "read_prefixes": [""],
                    "write_prefixes": [name + ".txt"],
                    "base_revision": workspace["revision_id"],
                }
            )
        script = [{"action": {"kind": "assign", "assignments": assignments}}]
        # Fake adapter fills join/merge identities from actual server-created children.
        script += [{"action": {"kind": "join", "child_ids": ["$CHILD0", "$CHILD1"]}}]
        for i in range(2):
            script.append(
                {
                    "action": {
                        "kind": "merge",
                        "child_id": "$CHILD" + str(i),
                        "base_revision": workspace["revision_id"],
                        "source_revision": "$CHILDHEAD" + str(i),
                        "expected_revision": "$HEAD",
                    }
                }
            )
        script.append(invoke("workspace_verify", expected_revision="$HEAD", check_id="runtime.syntax"))
        run = await client.submit(agent.id, "general:" + json.dumps(script), workspace=workspace)
        result = await client.wait(run.id, timeout=60)
        assert result.status == "completed", result.model_dump()
        children = await client.children(run.id)
        assert len(children) == 2 and all(c.status == "completed" for c in children)
        for child in children:
            desc = await temporal.get_workflow_handle("run:" + child.id).describe()
            assert desc.parent_id == "run:" + run.id
        for name in ["left", "right"]:
            assert (
                await client.workspace_read(
                    workspace["workspace_id"], result.workspace["revision_id"], name + ".txt"
                )
                == name.encode()
            )


@pytest.mark.parametrize(
    "program,expected",
    [
        ("import os; os.symlink('/etc/passwd','bad')", "sandbox_unsafe_result"),
        ("import os; os.link('base.txt','alias')", "sandbox_unsafe_result"),
        ("import time; time.sleep(5)", "sandbox_timeout"),
        (
            "from pathlib import Path; [Path('/work/outputs/'+str(i)).write_bytes(b'x'*262144) for i in range(5)]",
            "sandbox_unsafe_result",
        ),
        (
            "import resource; from pathlib import Path; assert resource.getrlimit(resource.RLIMIT_CPU)==(30,30); assert Path('/sys/fs/cgroup/pids.max').read_text().strip()=='64'; assert int(Path('/sys/fs/cgroup/memory.max').read_text())==268435456; assert 'CapEff:\t0000000000000000' in Path('/proc/self/status').read_text(); print('resource limits verified')",
            None,
        ),
        (
            "import os,socket; assert not any('KEY' in k for k in os.environ); assert not os.path.exists('/var/run/docker.sock'); s=socket.socket(); s.settimeout(.2); assert s.connect_ex(('1.1.1.1',443)) != 0; print('isolated')",
            None,
        ),
        (
            "from pathlib import Path; Path('base.txt').write_text('kept after failure'); raise SystemExit(7)",
            None,
        ),
    ],
)
async def test_actual_isolation_and_failure_receipts(pg_store, monkeypatch, program, expected):
    async with backend(pg_store, monkeypatch) as (client, _):
        workspace = await client.workspace_create({"base.txt": b"original"})
        agent = await client.create_agent(
            AgentConfig(
                name="isolation",
                provider="fake",
                model="deterministic",
                tools=["workspace_command", "workspace_verify"],
                general=GeneralPolicy(),
            )
        )
        script = [
            invoke(
                "workspace_command",
                expected_revision="$HEAD",
                argv=["python", "-c", program],
                wall_seconds=1,
                commit=True,
            ),
            invoke("workspace_verify", expected_revision="$HEAD", check_id="runtime.syntax"),
        ]
        run = await client.submit(agent.id, "general:" + json.dumps(script), workspace=workspace)
        result = await client.wait(run.id, timeout=35)
        operations = (await client.operations(run.id))["items"]
        receipt = next(o["result"] for o in operations if o["id"] == run.id + ":action:0")
        if expected:
            assert receipt["error"] == expected
            assert result.workspace["revision_id"] == workspace["revision_id"]
        else:
            assert receipt["exit_code"] in {0, 7}
            if receipt["exit_code"] == 7:
                assert (
                    await client.workspace_read(
                        workspace["workspace_id"], result.workspace["revision_id"], "base.txt"
                    )
                    == b"kept after failure"
                )


async def test_actual_broker_restart_reconstructs_project(pg_store, monkeypatch):
    from agent_runtime.project_store import digest

    broker_name = os.environ.get("TEST_PROJECT_BROKER_NAME", "agent-runtime-v3-test-broker")

    async with backend(pg_store, monkeypatch) as (client, _):
        workspace = await client.workspace_create({"base.txt": b"committed"})
        agent = await client.create_agent(
            AgentConfig(
                name="reconstruction",
                provider="fake",
                model="deterministic",
                tools=["workspace_command", "workspace_verify"],
                general=GeneralPolicy(),
            )
        )
        program = "import time; from pathlib import Path; assert Path('base.txt').read_text() == 'committed'; Path('base.txt').write_text('dirty'); time.sleep(4); Path('result.txt').write_text('reconstructed')"
        run = await client.submit(
            agent.id,
            "general:"
            + json.dumps(
                [
                    invoke(
                        "workspace_command",
                        expected_revision="$HEAD",
                        argv=["python", "-c", program],
                        commit=True,
                    ),
                    invoke("workspace_verify", expected_revision="$HEAD", check_id="runtime.syntax"),
                ]
            ),
            workspace=workspace,
        )
        name = "agents-project-v3-" + digest((run.id + ":action:0:command:1").encode())
        for _ in range(100):
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
            await asyncio.sleep(0.1)
        else:
            pytest.fail("Command container was not observed")
        p = await asyncio.create_subprocess_exec(
            "sudo",
            "-n",
            "docker",
            "kill",
            "--signal",
            "KILL",
            broker_name,
            stdout=asyncio.subprocess.DEVNULL,
        )
        assert await p.wait() == 0
        p = await asyncio.create_subprocess_exec(
            "sudo", "-n", "docker", "start", broker_name, stdout=asyncio.subprocess.DEVNULL
        )
        assert await p.wait() == 0
        result = await client.wait(run.id, timeout=100)
        assert result.status == "completed", result.error
        assert (
            await client.workspace_read(
                workspace["workspace_id"], result.workspace["revision_id"], "result.txt"
            )
            == b"reconstructed"
        )
        assert (await client.budget(run.id))["v3"]["counters"]["command_attempts"] == 3


async def test_cancel_command_fences_head_and_finishes_cleanup(pg_store, monkeypatch):
    from agent_runtime.project_store import digest

    async with backend(pg_store, monkeypatch) as (client, _):
        workspace = await client.workspace_create({"base.txt": b"original"})
        agent = await client.create_agent(
            AgentConfig(
                name="cancel",
                provider="fake",
                model="deterministic",
                tools=["workspace_command"],
                general=GeneralPolicy(),
            )
        )
        program = "import time; from pathlib import Path; Path('base.txt').write_text('late'); time.sleep(20)"
        run = await client.submit(
            agent.id,
            "general:"
            + json.dumps(
                [
                    invoke(
                        "workspace_command",
                        expected_revision="$HEAD",
                        argv=["python", "-c", program],
                        commit=True,
                    )
                ]
            ),
            workspace=workspace,
        )
        name = "agents-project-v3-" + digest((run.id + ":action:0:command:1").encode())
        for _ in range(100):
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
            await asyncio.sleep(0.1)
        await client.cancel(run.id)
        result = await client.wait(run.id, timeout=30)
        assert result.status == "cancelled" and result.cleanup_state == "complete"
        assert result.workspace["revision_id"] == workspace["revision_id"]
        events = [e async for e in client.watch(run.id)]
        assert any(e.type == "cleanup.completed" for e in events)


async def test_stale_check_rejected_then_fresh_check_accepted(pg_store, monkeypatch):
    from agent_runtime.project_store import digest

    async with backend(pg_store, monkeypatch) as (client, _):
        workspace = await client.workspace_create({"main.py": b"x = 1\n"})
        agent = await client.create_agent(
            AgentConfig(
                name="freshness",
                provider="fake",
                model="deterministic",
                tools=["workspace_patch", "workspace_write", "workspace_verify"],
                general=GeneralPolicy(),
            )
        )
        script = [
            invoke(
                "workspace_patch",
                expected_revision="$HEAD",
                patches=[
                    {
                        "path": "main.py",
                        "expected_sha256": digest(b"x = 1\n"),
                        "hunks": [{"old": "x = 1", "new": "x = 2"}],
                    }
                ],
            ),
            invoke("workspace_verify", expected_revision="$HEAD", check_id="runtime.syntax"),
            invoke(
                "workspace_write",
                expected_revision="$HEAD",
                writes=[
                    {
                        "path": "main.py",
                        "expected_sha256": digest(b"x = 2\n"),
                        "content_base64": base64.b64encode(b"x = 3\n").decode(),
                    }
                ],
            ),
            {"action": {"kind": "complete"}},
            invoke("workspace_verify", expected_revision="$HEAD", check_id="runtime.syntax"),
        ]
        run = await client.submit(agent.id, "general:" + json.dumps(script), workspace=workspace)
        result = await client.wait(run.id, timeout=40)
        assert result.status == "completed"
        checks = (await client.verifications(run.id))["items"]
        assert [c["fresh"] for c in checks] == [False, True]
        events = [e async for e in client.watch(run.id)]
        assert sum(e.type == "completion.rejected" for e in events) == 1
        assert (
            await client.workspace_read(workspace["workspace_id"], result.workspace["revision_id"], "main.py")
            == b"x = 3\n"
        )


async def test_dynamic_conflict_resolution_and_sparse_grants(pg_store, monkeypatch):
    from agent_runtime.project_store import digest

    async with backend(pg_store, monkeypatch) as (client, _):
        workspace = await client.workspace_create(
            {"shared.txt": b"base", "private.txt": b"synthetic private fixture"}
        )
        tools = ["workspace_read", "workspace_write", "workspace_verify"]
        cfg = AgentConfig(
            name="conflict",
            provider="fake",
            model="deterministic",
            tools=tools,
            general=GeneralPolicy(limits={"model_attempts": 24}, delegation={"tools": tools}),
        )
        agent = await client.create_agent(cfg)
        assignments = []
        for name in ["left", "right"]:
            child_script = [
                invoke("workspace_read", path="private.txt"),
                invoke(
                    "workspace_write",
                    expected_revision="$HEAD",
                    writes=[
                        {
                            "path": "shared.txt",
                            "expected_sha256": digest(b"base"),
                            "content_base64": base64.b64encode(name.encode()).decode(),
                        }
                    ],
                ),
                invoke("workspace_verify", expected_revision="$HEAD", check_id="runtime.syntax"),
            ]
            assignments.append(
                {
                    "role": name,
                    "objective": "general:" + json.dumps(child_script),
                    "criteria": [{"id": "edit", "statement": "Edit the assigned shared file"}],
                    "tools": tools,
                    "base_revision": workspace["revision_id"],
                    "read_prefixes": ["shared.txt"],
                    "write_prefixes": ["shared.txt"],
                }
            )
        script = [
            {"action": {"kind": "assign", "assignments": assignments}},
            {"action": {"kind": "join", "child_ids": ["$CHILD0", "$CHILD1"]}},
        ]
        for i in range(2):
            script.append(
                {
                    "action": {
                        "kind": "merge",
                        "child_id": "$CHILD" + str(i),
                        "base_revision": workspace["revision_id"],
                        "source_revision": "$CHILDHEAD" + str(i),
                        "expected_revision": "$HEAD",
                    }
                }
            )
        script += [
            invoke(
                "workspace_write",
                expected_revision="$HEAD",
                writes=[
                    {
                        "path": "shared.txt",
                        "expected_sha256": digest(b"left"),
                        "content_base64": base64.b64encode(b"resolved").decode(),
                    }
                ],
            ),
            invoke("workspace_verify", expected_revision="$HEAD", check_id="runtime.syntax"),
        ]
        run = await client.submit(agent.id, "general:" + json.dumps(script), workspace=workspace)
        result = await client.wait(run.id, timeout=60)
        assert result.status == "completed", result.error
        assert (
            await client.workspace_read(
                workspace["workspace_id"], result.workspace["revision_id"], "shared.txt"
            )
            == b"resolved"
        )
        operations = (await client.operations(run.id))["items"]
        assert any(o.get("result", {}).get("conflicts") == ["shared.txt"] for o in operations)
        children = await client.children(run.id)
        for child in children:
            assert child.effective_grants["read_prefixes"] == ["shared.txt"]
            manifest = await client.workspace(child.workspace["workspace_id"], child.workspace["revision_id"])
            assert [f["path"] for f in manifest["files"]] == ["shared.txt"]
            operations = (await client.operations(child.id))["items"]
            assert any(o.get("result", {}).get("error") == "File not found" for o in operations)
