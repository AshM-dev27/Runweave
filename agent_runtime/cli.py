"""Run with uv run python -m agent_runtime.cli --help."""

import argparse
import asyncio
import json
import os
from pathlib import Path
from uuid import uuid4

from .client import Client, ClientError
from .schemas import AgentConfig, Run


def parser():
    p = argparse.ArgumentParser(
        description=(
            "Submit tasks, instructions and context to the Independent Agents API for iterative planning, "
            "authorized tools, optional scoped subagents, verification and results. Development build."
        ),
        epilog="Defaults use scripted fake tasks, not general language autonomy. No model calls at startup.",
    )
    p.add_argument("--base-url", default="http://localhost:18000")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("models")
    sub.add_parser("tools")
    sub.add_parser("catalog")
    sub.add_parser("skills")
    sub.add_parser("artifacts")
    up = sub.add_parser("upload")
    up.add_argument("path")
    up.add_argument("--media-type", default="text/plain")
    up.add_argument("--key")
    down = sub.add_parser("download")
    down.add_argument("id")
    down.add_argument("path")
    sub.add_parser("readiness")
    sub.add_parser("session")
    a = sub.add_parser("agent")
    a.add_argument(
        "--config",
        help="Complete AgentConfig JSON: instructions, tools, general policy or legacy specialists",
    )
    a.add_argument("--name", default="local agent")
    a.add_argument("--provider", default="fake")
    a.add_argument("--model", default="deterministic")
    a.add_argument("--tools", nargs="+", default=["add"])
    a.add_argument("--max-tokens", type=int, default=512)
    for name in ["submit", "continue"]:
        s = sub.add_parser(name)
        s.add_argument("id", help="Agent ID for submit; previous run ID for continue")
        s.add_argument("input")
        s.add_argument("--task", help="TaskGoal JSON file with caller criteria and constraints")
        s.add_argument("--workspace", help="Explicit workspace ID to attach to this run")
        s.add_argument("--revision", help="Workspace revision ID to attach")
        s.add_argument("--artifact", action="append", default=[])
        s.add_argument("--key", help="Reuse this key when retrying the same submission")
        s.add_argument("--wait", action="store_true")
    for name in [
        "get",
        "wait",
        "watch",
        "cancel",
        "approve",
        "deny",
        "children",
        "budget",
        "effects",
        "task",
        "verifications",
        "capabilities",
        "operations",
        "checkpoints",
    ]:
        s = sub.add_parser(name)
        s.add_argument("id", help="Run ID")
        if name in {"approve", "deny"}:
            s.add_argument("approval_id")
        if name == "watch":
            s.add_argument("--cursor", type=int, default=0)
        if name in {"capabilities", "verifications", "operations", "checkpoints"}:
            s.add_argument("--cursor", type=int, default=0)
            s.add_argument("--limit", type=int, default=16)
        if name == "capabilities":
            s.add_argument("--query", default="")
    wc = sub.add_parser("workspace-create")
    wc.add_argument("--directory")
    wc.add_argument("--key")
    for name in ["workspace", "workspace-read", "workspace-download"]:
        w = sub.add_parser(name)
        w.add_argument("id")
        w.add_argument("--revision", required=name != "workspace")
        if name == "workspace-read":
            w.add_argument("--file", required=True)
        if name != "workspace":
            w.add_argument("path")
    output = sub.add_parser("operation-output")
    output.add_argument("id")
    output.add_argument("operation")
    output.add_argument("name")
    output.add_argument("path")
    return p


def read_directory(directory):
    import stat

    from .project_store import validate_files

    root = Path(directory)
    if any(p.is_symlink() for p in [root.absolute(), *root.absolute().parents]) or not root.is_dir():
        raise ClientError("Upload directory must be a regular directory")
    files = {}
    for base, dirs, names, directory_fd in os.fwalk(root, follow_symlinks=False):
        for name in dirs:
            if not stat.S_ISDIR(os.stat(name, dir_fd=directory_fd, follow_symlinks=False).st_mode):
                raise ClientError("Upload links are forbidden")
        for name in names:
            source = Path(base) / name
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
            try:
                st = os.fstat(fd)
                if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_size > 262144:
                    raise ClientError("Upload requires bounded regular files without links")
                files[source.relative_to(root).as_posix()] = os.read(fd, 262145)
            finally:
                os.close(fd)
    try:
        validate_files(files)
    except Exception:
        raise ClientError("Upload paths or bounds are invalid") from None
    return files


def display(value):
    print(value.model_dump_json() if hasattr(value, "model_dump_json") else json.dumps(value))


async def run(args):
    if not os.environ.get("API_KEY"):
        raise ClientError(
            "API_KEY is missing. Use uv run --env-file .env.local python -m agent_runtime.cli …"
        )
    async with Client(args.base_url, os.environ["API_KEY"]) as client:
        cmd = args.command
        if cmd in {"models", "readiness", "tools", "artifacts"}:
            result = await getattr(client, cmd)()
        elif cmd == "catalog":
            result = await client.installed_capabilities()
        elif cmd == "skills":
            result = await client.skills()
        elif cmd == "workspace-create":
            result = await client.workspace_create(
                read_directory(args.directory) if args.directory else {}, idempotency_key=args.key
            )
        elif cmd == "workspace":
            result = await client.workspace(args.id, args.revision)
        elif cmd in {"workspace-read", "workspace-download", "operation-output"}:
            if cmd == "workspace-read":
                data = await client.workspace_read(args.id, args.revision, args.file)
            elif cmd == "workspace-download":
                data = await client.workspace_download(args.id, args.revision)
            else:
                data = await client.operation_output(args.id, args.operation, args.name)
            with open(args.path, "xb") as output:
                output.write(data)
            result = {"path": args.path, "bytes": len(data)}
        elif cmd == "upload":
            result = await client.upload(
                Path(args.path).read_bytes(), args.media_type, Path(args.path).name, idempotency_key=args.key
            )
        elif cmd == "download":
            data = await client.download(args.id)
            with open(args.path, "xb") as file:
                file.write(data)
            result = {"bytes": len(data), "path": args.path}
        elif cmd == "agent":
            result = await client.create_agent(
                AgentConfig.model_validate_json(Path(args.config).read_text())
                if args.config
                else AgentConfig(
                    name=args.name,
                    provider=args.provider,
                    model=args.model,
                    tools=args.tools,
                    max_tokens=args.max_tokens,
                )
            )
        elif cmd == "session":
            result = await client.create_session()
        elif cmd in {"submit", "continue"}:
            key = args.key or uuid4().hex
            print(json.dumps({"idempotency_key": key}), flush=True)
            method = client.submit if cmd == "submit" else client.continue_run
            if bool(args.workspace) != bool(args.revision):
                raise ClientError("Workspace and revision must be supplied together")
            result = await method(
                args.id,
                args.input,
                idempotency_key=key,
                artifact_ids=args.artifact,
                task=json.loads(Path(args.task).read_text()) if args.task else None,
                workspace={"workspace_id": args.workspace, "revision_id": args.revision}
                if args.workspace
                else None,
            )
            display(result)
            if args.wait:
                result = await client.wait(result.id)
        elif cmd in {"approve", "deny"}:
            result = await client.decide(args.id, args.approval_id, cmd == "approve")
        elif cmd == "watch":
            async for event in client.watch(args.id, cursor=args.cursor):
                display(event)
            result = await client.get(args.id)
        elif cmd in {"capabilities", "verifications", "operations", "checkpoints"}:
            params = {"cursor": args.cursor, "limit": args.limit}
            if cmd == "capabilities":
                params["query"] = args.query
            result = await getattr(client, cmd)(args.id, **params)
        else:
            result = await getattr(client, cmd)(args.id)
        display(result)
        if isinstance(result, Run):
            if cmd in {"approve", "deny"}:
                print(f"Decision recorded. Use get/wait {result.id} to follow workflow progress.")
            elif result.status == "awaiting_approval":
                print("Approval required: review approvals and run approve/deny RUN_ID APPROVAL_ID.")
            return 1 if result.status in {"failed", "cancelled"} else 0
        return 1 if cmd == "readiness" and not result["ready"] else 0


def main():
    try:
        return asyncio.run(run(parser().parse_args()))
    except (ClientError, ValueError) as exc:
        # Validation errors may echo user input; only display our own safe errors.
        print(str(exc) if isinstance(exc, ClientError) else "Invalid input; check --help.")
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
