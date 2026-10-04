"""Secure project-local setup. Never sources shell code or prints secrets."""

import asyncio
import os
import secrets
import socket
import stat
import subprocess
from pathlib import Path

from dotenv import dotenv_values

from agent_runtime.client import Client, ClientError

ROOT = Path(__file__).resolve().parents[1]


def prepare_auth():
    path = ROOT / ".env.local"
    if path.is_symlink() or not path.is_file():
        raise RuntimeError("An existing regular ignored .env.local is required.")
    if (
        subprocess.run(
            ["git", "ls-files", "--error-unmatch", ".env.local"],
            cwd=ROOT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).returncode
        == 0
    ):
        raise RuntimeError("Refusing a tracked secret file.")
    if subprocess.run(["git", "check-ignore", "-q", ".env.local"], cwd=ROOT).returncode:
        raise RuntimeError(".env.local must be ignored.")
    fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
    with os.fdopen(fd, "r+") as file:
        import fcntl

        fcntl.flock(file, fcntl.LOCK_EX)
        if not stat.S_ISREG(os.fstat(file.fileno()).st_mode):
            raise RuntimeError("Secret file must be regular.")
        values = dotenv_values(stream=file, interpolate=False)
        key = values.get("API_KEY")
        if not key or key == "replace-with-a-long-random-local-api-key":
            key = secrets.token_urlsafe(48)
            file.seek(0, 2)
            file.write(f"\nAPI_KEY={key}\n")
            file.flush()
            os.fsync(file.fileno())
        os.fchmod(file.fileno(), 0o600)
    return key


def prepare_api_auth(key):
    path = ROOT / ".env.api.local"
    if path.is_symlink():
        raise RuntimeError("API credential file must be regular")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as file:
        file.write("API_KEY=" + key + "\n")
        os.fchmod(file.fileno(), 0o600)


def prepare_sandbox_auth():
    path = ROOT / ".env.sandbox.local"
    if path.is_symlink():
        raise RuntimeError("Sandbox credential file must be regular")
    if not path.exists():
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as file:
            file.write("SANDBOX_BROKER_KEY=" + secrets.token_urlsafe(48) + "\n")
    if not path.is_file() or not dotenv_values(path, interpolate=False).get("SANDBOX_BROKER_KEY"):
        raise RuntimeError("Sandbox credential unavailable")


async def ready(key):
    async with Client(api_key=key) as client:
        async with asyncio.timeout(90):
            while True:
                try:
                    result = await client.readiness()
                    if result["ready"]:
                        print(
                            "Ready: http://localhost:18000 (database ready; Temporal poller observed; provider/MCP access not probed)"
                        )
                        return
                except ClientError:
                    pass
                await asyncio.sleep(1)


def main():
    try:
        key = prepare_auth()
        prepare_sandbox_auth()
        prepare_api_auth(key)
        env_file = str((ROOT / ".env.local").absolute())
        launch_env = {**os.environ, "API_PORT": "18000", "AGENTS_ENV_FILE": env_file}
        docker = ["docker"]
        if subprocess.run(docker + ["info"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode:
            docker = ["sudo", "-n", "env", "API_PORT=18000", f"AGENTS_ENV_FILE={env_file}", "docker"]
        # An occupied port is acceptable only if this project's API already owns it.
        with socket.socket() as sock:
            occupied = sock.connect_ex(("127.0.0.1", 18000)) == 0
        if occupied:
            ports = subprocess.run(
                docker + ["compose", "port", "api", "8000"],
                cwd=ROOT,
                env=launch_env,
                capture_output=True,
                text=True,
            )
            if ports.returncode or ports.stdout.strip() != "127.0.0.1:18000":
                raise RuntimeError("Port 18000 is occupied by another service.")
        subprocess.run(docker + ["compose", "build", "sandbox-image"], cwd=ROOT, env=launch_env, check=True)
        subprocess.run(
            docker + ["compose", "--profile", "app", "up", "-d", "--build"],
            cwd=ROOT,
            env=launch_env,
            check=True,
        )
        asyncio.run(ready(key))
        return 0
    except Exception:
        print(
            "Local startup failed. Check Docker access, ignored regular .env.local, port 18000 and service readiness. Secret details suppressed."
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
