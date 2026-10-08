"""Isolated real HTTP/PostgreSQL/Temporal/E2B demo, with scripted model decisions.

Requires existing local PostgreSQL and Temporal. Uses its own schema, task queue,
API and worker processes; does not restart or reconfigure the application stack.
"""

import argparse
import asyncio
import os
import secrets
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from uuid import uuid4

import httpx
from dotenv import load_dotenv
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from agent_runtime.client import Client
from agent_runtime.db import Database
from agent_runtime.store import Store
from examples.e2b_invoice import run_demo

ROOT = Path(__file__).resolve().parents[1]


async def main(output, container_image=None):
    if not os.environ.get("E2B_API_KEY"):
        raise ValueError("E2B_API_KEY is required for this explicitly requested live compute demo")
    schema = "test_e2b_" + uuid4().hex
    database_url = os.environ.get(
        "E2B_DEMO_DATABASE_URL", "postgresql+asyncpg://agents:local-development-only@127.0.0.1:5432/agents"
    )
    database = Database(database_url, schema)
    store = Store(database)
    admin = create_async_engine(database_url)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    api_key = secrets.token_urlsafe(32)
    environment = {
        key: value
        for key, value in os.environ.items()
        if not any(word in key.upper() for word in ("KEY", "TOKEN", "SECRET", "PASSWORD"))
    }
    environment.update(
        DATABASE_URL=database_url,
        DATABASE_SCHEMA=schema,
        API_KEY=api_key,
        TASK_QUEUE=schema,
        EXTENSION_REGISTRY_FILE=str(ROOT / "config/extensions.json"),
    )
    processes, handles, containers = [], [], []
    docker = ["docker"]
    if (
        container_image
        and subprocess.run(docker + ["info"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode
    ):
        docker = ["sudo", "-n", "docker"]
    created, completed = False, False
    try:
        async with admin.begin() as connection:
            await connection.execute(text(f"CREATE SCHEMA {schema}"))
        created = True
        await store.database.create_test_schema()
        with tempfile.TemporaryDirectory(prefix="runweave-e2b-demo-") as temporary:
            try:
                for name, args, env in (
                    (
                        "api",
                        [
                            "uvicorn",
                            "agent_runtime.api:app",
                            "--host",
                            "127.0.0.1",
                            "--port",
                            str(port),
                            "--no-access-log",
                            "--log-level",
                            "warning",
                        ],
                        environment,
                    ),
                    (
                        "worker",
                        ["agent_runtime.worker"],
                        {**environment, "E2B_API_KEY": os.environ["E2B_API_KEY"]},
                    ),
                ):
                    handle = open(Path(temporary) / (name + ".log"), "w")
                    handles.append(handle)
                    env = dict(env)
                    if name == "worker":
                        env.pop("API_KEY", None)
                    command = [sys.executable, "-m", *args]
                    if container_image:
                        env["EXTENSION_REGISTRY_FILE"] = "config/extensions.json"
                        allowed = {
                            "DATABASE_URL",
                            "DATABASE_SCHEMA",
                            "API_KEY",
                            "TASK_QUEUE",
                            "EXTENSION_REGISTRY_FILE",
                            "E2B_API_KEY",
                            "TEMPORAL_ADDRESS",
                            "TEMPORAL_NAMESPACE",
                        }
                        env_path = Path(temporary) / (name + ".env")
                        fd = os.open(env_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                        with os.fdopen(fd, "w") as values:
                            for key, value in env.items():
                                if key in allowed:
                                    if "\n" in value or "\r" in value:
                                        raise ValueError("Invalid container environment value")
                                    values.write(key + "=" + value + "\n")
                        container_name = schema + "-" + name
                        containers.append(container_name)
                        command = [
                            *docker,
                            "run",
                            "--rm",
                            "--name",
                            container_name,
                            "--network",
                            "host",
                            "--user",
                            "10001:10001",
                            "--read-only",
                            "--cap-drop",
                            "ALL",
                            "--security-opt",
                            "no-new-privileges:true",
                            "--tmpfs",
                            "/tmp:rw,noexec,nosuid,size=64m",
                            "--env-file",
                            str(env_path),
                            container_image,
                            "/app/.venv/bin/python",
                            "-m",
                            *args,
                        ]
                    processes.append(
                        subprocess.Popen(
                            command,
                            cwd=ROOT,
                            env=env,
                            stdout=handle,
                            stderr=handle,
                            start_new_session=True,
                        )
                    )
                url = f"http://127.0.0.1:{port}"
                end = time.monotonic() + 30
                async with httpx.AsyncClient(timeout=1, trust_env=False) as http:
                    while True:
                        if any(process.poll() is not None for process in processes):
                            raise RuntimeError("Demo API or worker exited during startup")
                        try:
                            response = await http.get(
                                url + "/v1/capabilities", headers={"Authorization": "Bearer " + api_key}
                            )
                            if response.status_code == 200:
                                break
                        except httpx.TransportError:
                            pass
                        if time.monotonic() >= end:
                            raise TimeoutError("Demo API startup timed out")
                        await asyncio.sleep(0.2)
                async with Client(base_url=url, api_key=api_key) as client:
                    await run_demo(client, output)
                    assert all(item["active"] == 0 for item in (await client.extension_status())["items"])
                    if container_image:
                        print(
                            "Non-root, read-only API/worker containers verified; all provider slots released."
                        )
                completed = True
            finally:
                for process in reversed(processes):
                    if process.poll() is None:
                        os.killpg(process.pid, signal.SIGTERM)
                for process in reversed(processes):
                    try:
                        process.wait(timeout=8)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait(timeout=5)
                for handle in handles:
                    handle.close()
                for name in containers:
                    subprocess.run(
                        docker + ["rm", "-f", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                    )
    finally:
        await database.close()
        if created and completed:
            async with admin.begin() as connection:
                await connection.execute(text(f"DROP SCHEMA {schema} CASCADE"))
            print("Isolated database schema and demo processes cleaned up.")
        elif created:
            # Retain runtime evidence if dispatch or cleanup is uncertain; never erase it to hide failure.
            print("Incomplete demo state retained in schema:", schema)
        await admin.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, help="Private worker credential file; never printed")
    parser.add_argument("--output", type=Path, default=Path("output/e2b-demo"))
    parser.add_argument(
        "--container-image",
        help="Explicit local immutable image for non-root, read-only container verification",
    )
    args = parser.parse_args()
    if args.env_file:
        load_dotenv(args.env_file, override=False)
    try:
        asyncio.run(main(args.output, args.container_image))
    except Exception as error:
        # SDK exception bodies can include private URLs or authorization details.
        print("E2B demo failed:", type(error).__name__, file=sys.stderr)
        raise SystemExit(1) from None
