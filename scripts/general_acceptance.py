"""Isolated v3 public HTTP/CLI acceptance. Live execution is separately guarded."""

import asyncio
import base64
import hashlib
import json
import os
import secrets
import socket
import sys
import time
from pathlib import Path
from uuid import uuid4

from dotenv import dotenv_values
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from temporalio.client import Client as TemporalClient

from agent_runtime.client import Client
from agent_runtime.general_contracts import GeneralPolicy
from agent_runtime.schemas import AgentConfig
from scripts.general_budget import MANIFEST, initialize, validate


def invoke(alias, **arguments):
    return {"action": {"kind": "invoke", "capability": alias, "arguments": arguments}}


def script(*decisions):
    return "general:" + json.dumps(decisions)


async def execute(args):
    resume_data = getattr(args, "resume_data_run", None)
    if resume_data and (args.scenario != "data-task" or args.campaign != "general-runtime-v3"):
        raise RuntimeError("Downloaded data recovery requires the data scenario")
    retry = getattr(args, "retry_scenario", None)
    if (args.scenario == "recovery-or-retry") != bool(retry):
        raise RuntimeError("Recovery requires an explicit retry scenario")
    budget = Path(args.budget_file)
    if args.live:
        if budget.absolute() != Path("var/acceptance/general-runtime-v3.sqlite").absolute():
            raise RuntimeError("V3 requires its fixed campaign path")
        gates = Path("docs/acceptance-general-runtime-v3-fake.json")
        if not gates.exists() or not json.loads(gates.read_text()).get("executions", [])[-1].get("passed"):
            raise RuntimeError("Fake HTTP acceptance gate is not passed")
        initialize(budget)
    env = {**os.environ, "PYDANTIC_AI_NO_BANNER": "1"}
    env.pop("OPENAI_API_KEY", None)
    env.pop("ANTHROPIC_API_KEY", None)
    schema = "general_" + uuid4().hex
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env.update(
        DATABASE_SCHEMA=schema,
        TASK_QUEUE=schema,
        API_KEY=secrets.token_urlsafe(32),
        ACCEPTANCE_CAMPAIGN="general-runtime-v3",
        ACCEPTANCE_BUDGET_FILE=str(budget.absolute()),
        APPROVAL_WAIT_SECONDS="120",
    )
    worker_env = {
        **env,
        "SANDBOX_BROKER_URL": "http://localhost:18091",
        "SANDBOX_BROKER_KEY": "v3-isolated-test-only",
    }
    if args.live:
        local = dotenv_values(".env.local", interpolate=False)
        worker_env["OPENAI_API_KEY"] = os.environ.get("OPENAI_API_KEY") or local.get("OPENAI_API_KEY") or ""
        if not worker_env["OPENAI_API_KEY"]:
            raise RuntimeError("Authorized worker credential unavailable")
    url = env.get("DATABASE_URL", "postgresql+asyncpg://agents:local-development-only@localhost:5432/agents")
    admin = create_async_engine(url)
    temporal = await TemporalClient.connect(env.get("TEMPORAL_ADDRESS", "localhost:7233"))
    output = Path(args.evidence)
    evidence = (
        json.loads(output.read_text())
        if output.exists()
        else {"campaign": "general-runtime-v3", "executions": []}
    )
    if evidence.get("campaign") != "general-runtime-v3":
        raise RuntimeError("Evidence belongs to another campaign")
    report = {
        "mode": "live" if args.live else "fake",
        "attempts_before": validate(budget) if args.live else 0,
        "scenarios": [],
        "passed": False,
    }
    evidence["executions"].append(report)
    processes, runs = [], []
    base = f"http://localhost:{port}"

    def save():
        report["attempts_after"] = validate(budget) if args.live else 0
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(evidence, indent=2) + "\n")

    async def process(*cmd, environment=env):
        p = await asyncio.create_subprocess_exec(
            sys.executable,
            *cmd,
            env=environment,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        processes.append(p)
        return p

    async def cli(*cmd):
        p = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "agent_runtime.cli",
            "--base-url",
            base,
            *cmd,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await p.communicate()
        if p.returncode:
            raise RuntimeError("CLI failed: " + cmd[0])
        return out.decode()

    def config(name, tools):
        allocation = "recovery-or-retry" if retry else name
        cap = MANIFEST["scenario_caps"].get(allocation, 1024)
        limit = MANIFEST["scenario_limits"].get(allocation, 12)
        return AgentConfig(
            name=allocation,
            provider="openai" if args.live else "fake",
            model="gpt-5.6-luna" if args.live else "deterministic",
            tools=tools,
            max_tokens=cap,
            instructions="Work autonomously using the authorized general actions. Finish the task with scoped evidence. Preserve user checks. Do not claim universal correctness.",
            general=GeneralPolicy(resources=None, limits={"model_attempts": limit}),
        )

    async def scenario(client, name, cfg, prompt, fake, files=None, task=None, expected=None):
        entry = {"name": name, "status": "failed", "attempts_before": validate(budget) if args.live else 0}
        allocation = "recovery-or-retry" if retry else name
        if resume_data:
            entry["imported_artifact_from_run"] = resume_data
            entry["verification_mode"] = "new_task_from_retained_artifact"
        entry["allocation"] = allocation
        if retry and args.live:
            prior = [
                s
                for execution in evidence["executions"][:-1]
                for s in execution["scenarios"]
                if s["name"] == name and s["status"] == "failed" and s.get("run_id")
            ]
            if not prior:
                raise RuntimeError("Recovery requires retained failed-run provenance")
            entry["retry_of_run_id"] = prior[-1]["run_id"]
        report["scenarios"].append(entry)
        save()
        started = time.monotonic()
        try:
            workspace = await client.workspace_create(files) if files is not None else None
            if args.live:
                import sqlite3

                with sqlite3.connect(budget) as db:
                    used = db.execute(
                        "SELECT count(*) FROM attempts WHERE scenario=?", (allocation,)
                    ).fetchone()[0]
                remaining = min(MANIFEST["scenario_limits"][allocation] - used, 24 - validate(budget))
                if remaining <= 0:
                    raise RuntimeError("Scenario capacity exhausted")
                cfg.general.limits.model_attempts = min(remaining, 1) if name == "simple" else remaining
            agent = await client.create_agent(cfg)
            actual_prompt = prompt if args.live else fake(workspace) if callable(fake) else fake
            run = await client.submit(agent.id, actual_prompt, workspace=workspace, task=task)
            runs.append(run.id)
            entry["run_id"] = run.id
            result = await client.wait(run.id, timeout=240, stop_at_approval=False)
            entry["run"] = result.model_dump(mode="json")
            entry["task"] = await client.task(run.id)
            entry["budget"] = await client.budget(run.id)
            entry["operations"] = await client.operations(run.id)
            entry["verifications"] = await client.verifications(run.id)
            entry["events"] = [e.model_dump(mode="json") async for e in client.watch(run.id)]
            children = await client.children(run.id)
            entry["children"] = [c.model_dump(mode="json") for c in children]
            entry["child_histories"] = []
            entry["child_operations"] = {}
            for child in children:
                entry["child_operations"][child.id] = await client.operations(child.id)
                handle = temporal.get_workflow_handle("run:" + child.id)
                desc = await handle.describe()
                history = await handle.fetch_history()
                entry["child_histories"].append(
                    {"id": child.id, "parent_id": desc.parent_id, "events": len(history.events)}
                )
            if expected:
                entry["downloads"] = []
                for file_name, content in expected.items():
                    actual = await client.workspace_read(
                        result.workspace["workspace_id"], result.workspace["revision_id"], file_name
                    )
                    target = (
                        output.parent / "general-downloads" / (run.id + "-" + file_name.replace("/", "_"))
                    )
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(actual)
                    entry["downloads"].append(
                        {
                            "path": file_name,
                            "sha256": hashlib.sha256(actual).hexdigest(),
                            "bytes": len(actual),
                            "matches": actual == content,
                        }
                    )
                    assert actual == content
                archive = await client.workspace_download(
                    result.workspace["workspace_id"], result.workspace["revision_id"]
                )
                entry["archive_sha256"] = hashlib.sha256(archive).hexdigest()
            assert result.status == "completed", result.error
            if name == "simple":
                assert (
                    entry["budget"]["requests"] == 1 and not children and entry["budget"]["tool_calls"] == 0
                )
            if name == "dynamic-specialists":
                assert all(c.status == "completed" for c in children)
                assert all(
                    any(
                        (o.get("result") or {}).get("changed")
                        for o in entry["child_operations"][c.id]["items"]
                    )
                    for c in children
                )
                assert any(
                    (o.get("result") or {}).get("conflicts") == [] and (o.get("result") or {}).get("changed")
                    for o in entry["operations"]["items"]
                )
                assert children and all(h["parent_id"] == "run:" + run.id for h in entry["child_histories"])
                assert any(v["method"] == "command" and v["fresh"] for v in entry["verifications"]["items"])
            entry["status"] = "passed"
        except Exception as exc:
            entry["error_type"] = type(exc).__name__
        finally:
            entry["elapsed_seconds"] = round(time.monotonic() - started, 3)
            entry["attempts_after"] = validate(budget) if args.live else 0
            save()
            print(
                json.dumps({k: entry[k] for k in ["name", "status", "attempts_before", "attempts_after"]}),
                flush=True,
            )
        return entry

    try:
        async with admin.begin() as conn:
            await conn.execute(text(f"CREATE SCHEMA {schema}"))
        migration = await process("-m", "alembic", "upgrade", "head")
        if await migration.wait():
            raise RuntimeError("Migration failed")
        drift = await process("-m", "alembic", "check")
        if await drift.wait():
            raise RuntimeError("Migration drift")
        await process("-m", "uvicorn", "agent_runtime.api:app", "--host", "127.0.0.1", "--port", str(port))
        worker = await process("-m", "scripts.acceptance_worker", environment=worker_env)
        async with Client(base, env["API_KEY"]) as client:
            for _ in range(100):
                try:
                    await client.models()
                    break
                except Exception:
                    await asyncio.sleep(0.1)

            def select(name):
                return name == retry if retry else args.scenario in {"all", name}

            if select("simple"):
                await scenario(
                    client,
                    "simple",
                    config("simple", []),
                    "Answer directly: what is 7 + 5?",
                    "Answer directly: what is 7 + 5?",
                )
            if select("project-revise"):
                files = {
                    "main.py": b"def double(x):\n    return x + 2\n",
                    "test_main.py": b"from main import double\ndef test_double():\n    assert double(4) == 8\n    assert double(-3) == -6\n",
                }
                good = b"def double(x):\n    return x * 2\n"
                task = {
                    "outcome": "Correct double(x); preserve input tests",
                    "criteria": [
                        {
                            "id": "behavior",
                            "statement": "Pass unchanged supplied pytest tests",
                            "evidence_policy": "check",
                            "checks": [{"id": "pytest", "kind": "command", "argv": ["pytest", "-q"]}],
                        }
                    ],
                }
                program = "from pathlib import Path; Path('main.py').write_text('def double(x):\\n    return x * 2\\n')"
                fake = script(
                    invoke(
                        "workspace_command",
                        expected_revision="$HEAD",
                        argv=["python", "-c", program],
                        commit=True,
                    ),
                    invoke("workspace_verify", expected_revision="$HEAD", check_id="pytest"),
                    invoke("workspace_verify", expected_revision="$HEAD", check_id="runtime.syntax"),
                )
                await scenario(
                    client,
                    "project-revise",
                    config(
                        "project-revise",
                        ["workspace_read", "workspace_write", "workspace_command", "workspace_verify"],
                    ),
                    "Fix main.py double(x) to multiply by 2. Preserve test_main.py. Request completion after the edit; completion automatically runs the registered caller and runtime checks. Keep main.py exactly: def double(x): newline four spaces return x * 2 newline.",
                    fake,
                    files,
                    task,
                    {"main.py": good},
                )
            if select("dynamic-specialists"):
                cfg = config(
                    "dynamic-specialists", ["workspace_write", "workspace_verify", "workspace_command"]
                )
                cfg.general.delegation = __import__(
                    "agent_runtime.general_contracts", fromlist=["DelegationPolicy"]
                ).DelegationPolicy(tools=cfg.tools, limits={"total_tokens": 12000})

                def delegated(workspace):
                    child_script = script(
                        invoke(
                            "workspace_write",
                            expected_revision="$HEAD",
                            writes=[
                                {
                                    "path": "part.txt",
                                    "expected_sha256": None,
                                    "content_base64": base64.b64encode(b"specialist result\n").decode(),
                                }
                            ],
                        ),
                        invoke("workspace_verify", expected_revision="$HEAD", check_id="runtime.syntax"),
                    )
                    return script(
                        {
                            "action": {
                                "kind": "assign",
                                "assignments": [
                                    {
                                        "role": "text contributor",
                                        "objective": child_script,
                                        "criteria": [{"id": "part", "statement": "Create assigned part.txt"}],
                                        "tools": cfg.tools,
                                        "base_revision": workspace["revision_id"],
                                        "read_prefixes": [""],
                                        "write_prefixes": ["part.txt"],
                                    }
                                ],
                            }
                        },
                        {"action": {"kind": "join", "child_ids": ["$CHILD0"]}},
                        {
                            "action": {
                                "kind": "merge",
                                "child_id": "$CHILD0",
                                "base_revision": workspace["revision_id"],
                                "source_revision": "$CHILDHEAD0",
                                "expected_revision": "$HEAD",
                            }
                        },
                        invoke("workspace_verify", expected_revision="$HEAD", check_id="runtime.syntax"),
                    )

                await scenario(
                    client,
                    "dynamic-specialists",
                    cfg,
                    "Dynamically assign one specialist to create part.txt containing exactly 'specialist result' plus newline. Request output scope only part.txt and capabilities workspace_write and workspace_verify, with an acceptance statement requiring those exact bytes. The server derives its grants and budgets. Join, merge its branch using its local selector, then complete. Completion automatically verifies both child and integrated parent heads. Do not write the file yourself.",
                    delegated,
                    {"base.txt": b"preserve\n"},
                    expected={"part.txt": b"specialist result\n"},
                )
            if select("data-task"):
                program = "import csv; from pathlib import Path; rows=list(csv.DictReader(Path('input.csv').open())); Path('result.csv').write_text('total\\n'+str(sum(int(r['amount']) for r in rows))+'\\n')"
                fake = script(
                    invoke(
                        "workspace_command",
                        expected_revision="$HEAD",
                        argv=["python", "-c", program],
                        commit=True,
                    ),
                    invoke("workspace_verify", expected_revision="$HEAD", check_id="runtime.syntax"),
                )
                data_files = {"input.csv": b"amount\n7\n-2\n9\n"}
                data_task = None
                data_prompt = "Use one Python command to read input.csv and create result.csv with header total and sum of integer amount values (newline terminated). Then request completion, which automatically runs the registered runtime checks. Input has amount values 7, -2, 9."
                if resume_data:
                    original = next(
                        (
                            s
                            for execution in evidence["executions"][:-1]
                            for s in execution["scenarios"]
                            if s.get("run_id") == resume_data and s["name"] == "data-task"
                        ),
                        None,
                    )
                    if not original:
                        raise RuntimeError("Retained data run not found")
                    receipt = next(
                        (
                            d
                            for d in original.get("downloads", [])
                            if d["path"] == "result.csv" and d["matches"]
                        ),
                        None,
                    )
                    recovered = (
                        output.parent / "general-downloads" / (resume_data + "-result.csv")
                    ).read_bytes()
                    if (
                        not receipt
                        or hashlib.sha256(recovered).hexdigest() != receipt["sha256"]
                        or len(recovered) != receipt["bytes"]
                    ):
                        raise RuntimeError("Recovered download integrity failed")
                    data_files["result.csv"] = recovered
                    data_task = {
                        "outcome": "Verify the retained output of the interrupted data task",
                        "criteria": [
                            {
                                "id": "output",
                                "statement": "Exact CSV total is 14",
                                "evidence_policy": "check",
                                "checks": [
                                    {
                                        "id": "output-bytes",
                                        "kind": "bytes",
                                        "path": "result.csv",
                                        "expected": base64.b64encode(b"total\n14\n").decode(),
                                    }
                                ],
                            }
                        ],
                    }
                    data_prompt = "The original data task already generated result.csv in an isolated command and its authenticated download is restored here. Input amount values are 7, -2, 9. Do not repeat generation. Request completion so the registered exact-byte check runs automatically. This is a new verification task, not a resumed workflow or fresh generation."
                    fake = script(
                        invoke("workspace_verify", expected_revision="$HEAD", check_id="output-bytes")
                    )
                await scenario(
                    client,
                    "data-task",
                    config("data-task", ["workspace_command", "workspace_verify"]),
                    data_prompt,
                    fake,
                    data_files,
                    task=data_task,
                    expected={"result.csv": b"total\n14\n"},
                )
            if not args.live and args.scenario in {"all", "controls"}:
                entry = {"name": "controls", "status": "failed"}
                report["scenarios"].append(entry)
                try:
                    cfg = config("controls", ["record_note"])
                    cfg_path = Path("/tmp") / (schema + "-config.json")
                    cfg_path.write_text(cfg.model_dump_json())
                    agent = json.loads((await cli("agent", "--config", str(cfg_path))).splitlines()[-1])
                    run = await client.submit(
                        agent["id"], script(invoke("record_note", text="Synthetic recovery note"))
                    )
                    runs.append(run.id)
                    assert (await client.wait(run.id)).status == "awaiting_approval"
                    worker.kill()
                    await worker.wait()
                    worker = await process("-m", "scripts.acceptance_worker", environment=worker_env)
                    waiting = await client.get(run.id)
                    await cli("deny", run.id, waiting.approvals[0].id)
                    result = await client.wait(run.id)
                    assert result.status == "completed" and await client.effects(run.id) == []
                    for command in [
                        "task",
                        "verifications",
                        "capabilities",
                        "operations",
                        "checkpoints",
                        "children",
                        "budget",
                        "effects",
                    ]:
                        await cli(command, run.id)
                    directory = Path("/tmp") / (schema + "-upload")
                    directory.mkdir()
                    (directory / "file.txt").write_bytes(b"CLI bytes\n")
                    ws = json.loads(
                        (
                            await cli("workspace-create", "--directory", str(directory), "--key", schema)
                        ).splitlines()[-1]
                    )
                    await cli("workspace", ws["workspace_id"], "--revision", ws["revision_id"])
                    await cli(
                        "workspace-read",
                        ws["workspace_id"],
                        "--revision",
                        ws["revision_id"],
                        "--file",
                        "file.txt",
                        str(directory / "read.bin"),
                    )
                    await cli(
                        "workspace-download",
                        ws["workspace_id"],
                        "--revision",
                        ws["revision_id"],
                        str(directory / "download.zip"),
                    )
                    assert (directory / "read.bin").read_bytes() == b"CLI bytes\n"
                    command_cfg = config("controls", ["workspace_command"])
                    cfg_path.write_text(command_cfg.model_dump_json())
                    command_agent = json.loads(
                        (await cli("agent", "--config", str(cfg_path))).splitlines()[-1]
                    )
                    task_path = directory / "task.json"
                    task_path.write_text(
                        json.dumps(
                            {
                                "outcome": "Print the declared line",
                                "criteria": [{"id": "line", "statement": "Print CLI output"}],
                            }
                        )
                    )
                    submitted = json.loads(
                        (
                            await cli(
                                "submit",
                                command_agent["id"],
                                script(
                                    invoke(
                                        "workspace_command",
                                        expected_revision="$HEAD",
                                        argv=["python", "-c", "print('CLI output')"],
                                    )
                                ),
                                "--task",
                                str(task_path),
                                "--workspace",
                                ws["workspace_id"],
                                "--revision",
                                ws["revision_id"],
                                "--key",
                                schema + "-cli-run",
                                "--wait",
                            )
                        ).splitlines()[-1]
                    )
                    runs.append(submitted["id"])
                    await cli(
                        "operation-output",
                        submitted["id"],
                        submitted["id"] + ":action:0",
                        "stdout",
                        str(directory / "stdout.bin"),
                    )
                    assert (directory / "stdout.bin").read_bytes() == b"CLI output\n"
                    continued = json.loads(
                        (
                            await cli(
                                "continue",
                                submitted["id"],
                                "Direct response",
                                "--task",
                                str(task_path),
                                "--workspace",
                                ws["workspace_id"],
                                "--revision",
                                ws["revision_id"],
                                "--key",
                                schema + "-cli-continue",
                                "--wait",
                            )
                        ).splitlines()[-1]
                    )
                    runs.append(continued["id"])
                    await cli("watch", continued["id"])
                    entry.update(
                        status="passed",
                        worker_sigkill_recovery=True,
                        cli_commands=19,
                        run_id=run.id,
                        cli_submit_id=submitted["id"],
                        cli_continue_id=continued["id"],
                    )

                except Exception as exc:
                    entry["error_type"] = type(exc).__name__
                save()
        report["passed"] = bool(report["scenarios"]) and all(
            e["status"] == "passed" for e in report["scenarios"]
        )
    finally:
        for rid in runs:
            try:
                await temporal.get_workflow_handle("run:" + rid).cancel()
            except Exception:
                pass
        for p in processes:
            if p.returncode is None:
                p.terminate()
                await p.wait()
        async with admin.begin() as conn:
            await conn.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
        await admin.dispose()
        report["isolated_schema_removed"] = True
        save()
    return 0 if report["passed"] else 1
