"""Bounded E2B Python jobs with durable handles, conservative dispatch and cleanup."""

import hashlib
import json
import math
import os
import time
from pathlib import PurePosixPath
from typing import Literal

import httpx
from pydantic import Field, field_validator

from .extensions import DeferredToolResult
from .general_contracts import Contract

ROOT = "/home/user/runweave"
RESULT_PATH = ROOT + "/result.json"
MAX_FILE_BYTES = 4096
MAX_RESULT_BYTES = 32768
E2B_ARGUMENTS_SCHEMA = {
    "type": "object",
    "properties": {
        "code": {"type": "string", "minLength": 1, "maxLength": 8192},
        "files": {
            "type": "object",
            "maxProperties": 4,
            "additionalProperties": {"type": "string", "maxLength": 8192},
        },
        "outputs": {
            "type": "array",
            "minItems": 1,
            "maxItems": 2,
            "uniqueItems": True,
            "items": {"type": "string", "minLength": 1, "maxLength": 120},
        },
    },
    "required": ["code", "files", "outputs"],
    "additionalProperties": False,
}


class E2BConfig(Contract):
    credential_env: Literal["E2B_API_KEY"] = "E2B_API_KEY"
    template: str = Field(default="base", pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._/-]{0,99}$")
    sandbox_seconds: int = Field(default=120, strict=True, ge=30, le=300)
    command_seconds: int = Field(default=20, strict=True, ge=1, le=30)


class JobReceipt(Contract):
    exit_code: int = Field(strict=True, ge=-255, le=255)
    stdout: str = Field(max_length=2048)
    stderr: str = Field(max_length=2048)

    @field_validator("stdout", "stderr")
    @classmethod
    def bounded_utf8(cls, value):
        if len(value.encode("utf-8")) > 2048:
            raise ValueError("e2b_receipt_output_limit")
        return value


def safe_path(value):
    p = PurePosixPath(value)
    if (
        not value
        or not p.parts
        or len(value.encode()) > 120
        or p.is_absolute()
        or str(p) != value
        or any(part in {".", ".."} or part.startswith(".") for part in p.parts)
        or p.parts[0] in {"code.py", "runner.py", "result.json", "result.tmp", "stdout.txt", "stderr.txt"}
        or any(not char.isprintable() or char == "\\" for char in value)
    ):
        raise ValueError("invalid_e2b_path")
    return value


def validate_job(arguments):
    from jsonschema import Draft202012Validator

    Draft202012Validator(E2B_ARGUMENTS_SCHEMA).validate(arguments)
    for name in [*arguments["files"], *arguments["outputs"]]:
        safe_path(name)
    if sum(len(value.encode()) for value in arguments["files"].values()) > 16384:
        raise ValueError("e2b_input_limit")
    if len(arguments["code"].encode()) > 8192:
        raise ValueError("e2b_code_limit")
    names = set(arguments["files"]) | set(arguments["outputs"])
    if any("/".join(name.split("/")[:i]) in names for name in names for i in range(1, len(name.split("/")))):
        raise ValueError("e2b_path_collision")


def sandbox_class():
    from e2b import AsyncSandbox

    return AsyncSandbox


async def download(sandbox, path, limit):
    # The SDK's files.read buffers the entire response, even with format='stream'.
    # Use its public signed URL with a bounded HTTP stream. URLs never enter state or events.
    url = sandbox.download_url(path, use_signature_expiration=30)
    async with httpx.AsyncClient(timeout=10, trust_env=False, follow_redirects=False) as client:
        async with client.stream("GET", url) as response:
            response.raise_for_status()
            content = bytearray()
            async for chunk in response.aiter_bytes(chunk_size=1024):
                content.extend(chunk)
                if len(content) > limit:
                    raise ValueError("e2b_output_limit")
            return bytes(content)


async def download_chunks(sandbox, path, limit):
    """Stream guest bytes; provider download credentials stay in activity memory."""
    url = sandbox.download_url(path, use_signature_expiration=30)
    size = 0
    async with httpx.AsyncClient(timeout=10, trust_env=False, follow_redirects=False) as client:
        async with client.stream("GET", url) as response:
            response.raise_for_status()
            async for chunk in response.aiter_bytes(chunk_size=65536):
                size += len(chunk)
                if size > limit:
                    raise ValueError("e2b_output_limit")
                yield chunk


def runner(seconds, *, control_root=ROOT, outputs=(), inputs=()):
    # This operator-owned wrapper captures bounded observations. Its receipt, like all
    # sandbox output, is untrusted; business correctness must be checked independently.
    return f"""import json, os, resource, signal, subprocess, sys
from pathlib import Path
root = Path({ROOT!r})
control = Path({control_root!r})
for name in {tuple(name for name in outputs if name not in inputs)!r}:
    (root / name).unlink(missing_ok=True)
def limits():
    resource.setrlimit(resource.RLIMIT_FSIZE, (1048576, 1048576))
    resource.setrlimit(resource.RLIMIT_AS, (268435456, 268435456))
    resource.setrlimit(resource.RLIMIT_NPROC, (64, 64))
with (control / "stdout.txt").open("wb") as out, (control / "stderr.txt").open("wb") as err:
    process = subprocess.Popen([sys.executable, "-I", str(control / "code.py")], cwd=root,
                               stdout=out, stderr=err, start_new_session=True, preexec_fn=limits)
    try:
        exit_code = process.wait(timeout={seconds})
    except subprocess.TimeoutExpired:
        exit_code = 124
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()
def bounded(name):
    with (control / name).open("rb") as source:
        return source.read(2048).decode("utf-8", "replace").encode("utf-8")[:2048].decode("utf-8", "ignore")
receipt = {{"exit_code": exit_code, "stdout": bounded("stdout.txt"), "stderr": bounded("stderr.txt")}}
temporary = control / "result.tmp"
temporary.write_text(json.dumps(receipt, ensure_ascii=False), encoding="utf-8")
temporary.replace(control / "result.json")
"""


class E2BHandler:
    version = 1
    config_type = E2BConfig

    async def execute(self, call):
        self.validate(call.arguments)
        config = self.config_type.model_validate(call.definition["config"])
        if call.state or call.save_state is None:
            return None
        key = os.environ.get(config.credential_env)
        if not key:
            return {"error": "e2b_not_configured"}
        remaining = config.sandbox_seconds if call.remaining_seconds is None else call.remaining_seconds
        seconds = math.floor(min(config.sandbox_seconds, remaining))
        if seconds < 1:
            return {"error": "e2b_time_limit"}
        error = await self.prepare(call)
        if error:
            return {"error": error, "cleanup_complete": True}
        state = {
            "phase": "creating",
            "deadline": time.time() + seconds,
            "operation_digest": hashlib.sha256(call.operation_id.encode()).hexdigest(),
            "actions": [],
        }
        await call.save_state(state)  # Never blindly repeat a possibly dispatched create.
        try:
            sandbox = await sandbox_class().create(
                template=config.template,
                timeout=seconds,
                secure=True,
                allow_internet_access=False,
                metadata={"runweave_operation": state["operation_digest"]},
                api_key=key,
                request_timeout=10,
            )
        except Exception as exc:
            from e2b.exceptions import (
                AuthenticationException,
                InvalidArgumentException,
                NotFoundException,
                RateLimitException,
                TemplateException,
            )

            if not isinstance(
                exc,
                (
                    AuthenticationException,
                    InvalidArgumentException,
                    NotFoundException,
                    RateLimitException,
                    TemplateException,
                ),
            ):
                raise
            state.update(
                phase="finished", sandbox_id=None, error="e2b_create_rejected", cleanup_complete=True
            )
            await call.save_state(state)
            return self.result(state, config)
        state.update(phase="uploading", sandbox_id=sandbox.sandbox_id, actions=["sandbox.created"])
        await call.save_state(state)
        return DeferredToolResult()

    async def reconcile(self, call):
        return await self.advance(call)

    async def cleanup(self, call):
        if not call.state:
            # This handler always persists creation intent before provider I/O.
            return {
                "provider": "e2b",
                "error": "e2b_job_not_started",
                "cleanup_complete": True,
                "outputs": {},
                "actions": [],
            }
        return await self.advance(call, abandon=True)

    async def locate(self, state, opts):
        if state.get("sandbox_id"):
            return state["sandbox_id"]
        if not state.get("operation_digest"):
            return None
        paginator = sandbox_class().list(
            query={"metadata": {"runweave_operation": state["operation_digest"]}}, limit=2, **opts
        )
        matches = await paginator.next_items()
        # Metadata lookup is recovery evidence, not a provider uniqueness guarantee.
        if len(matches) != 1 or paginator.has_next:
            return None
        return matches[0].sandbox_id

    async def advance(self, call, *, abandon=False):
        self.validate(call.arguments)
        config = self.config_type.model_validate(call.definition["config"])
        if call.save_state is None or not call.state:
            return None
        state = dict(call.state)
        state["actions"] = list(state.get("actions", []))
        if state["phase"] == "finished":
            return self.result(state, config)
        try:
            return await self.advance_io(call, state, config, abandon=abandon)
        except Exception as exc:
            from e2b.exceptions import RateLimitException, SandboxException, TimeoutException

            transient = (
                isinstance(exc, (httpx.TransportError, TimeoutError, TimeoutException, RateLimitException))
                or type(exc) is SandboxException
                or isinstance(exc, httpx.HTTPStatusError)
                and exc.response.status_code in {429, 500, 502, 503, 504}
            )
            failures = state.get("io_failures", 0) + 1
            if not transient or failures > 8:
                raise
            state["io_failures"] = failures
            await self.save(call, state, config)
            return DeferredToolResult(retry_after=min(30, 2 ** min(failures, 5)))

    async def advance_io(self, call, state, config, *, abandon):
        opts = {"api_key": os.environ[config.credential_env], "request_timeout": 10}
        sandbox_id = await self.locate(state, opts)
        if not sandbox_id:
            return None
        if not state.get("sandbox_id"):
            state.update(sandbox_id=sandbox_id, phase="uploading", actions=["sandbox.recovered"])
            await self.save(call, state, config)
        await self.recover_outputs(call, state)
        if abandon:
            state["error"] = "e2b_job_cancelled"
        elif time.time() >= state["deadline"]:
            state["error"] = "e2b_time_limit"
        if state.get("error") or state["phase"] == "cleanup":
            state["phase"] = "cleanup"
            await self.save(call, state, config)  # Persist cancellation/cleanup before remote termination.
            try:
                await sandbox_class().kill(sandbox_id, **opts)
            except Exception as exc:
                from e2b import NotFoundException

                if not isinstance(exc, NotFoundException):
                    raise
            state.update(phase="finished", cleanup_complete=True)
            state["actions"].append("sandbox.killed")
            await self.save(call, state, config)
            return self.result(state, config)
        try:
            sandbox = await sandbox_class().connect(
                sandbox_id, timeout=max(1, math.ceil(state["deadline"] - time.time())), **opts
            )
        except Exception as exc:
            from e2b import NotFoundException

            if not isinstance(exc, NotFoundException):
                raise
            state.update(phase="finished", error="e2b_sandbox_expired", cleanup_complete=True)
            await self.save(call, state, config)
            return self.result(state, config)
        if state["phase"] == "uploading":
            try:
                await self.upload_inputs(call, sandbox)
            except (ValueError, FileNotFoundError):
                state.update(phase="cleanup", error="e2b_input_invalid")
                await self.save(call, state, config)
                return DeferredToolResult()
            await sandbox.files.write(ROOT + "/code.py", call.arguments["code"], request_timeout=5)
            await sandbox.files.write(
                ROOT + "/runner.py", self.runner(config.command_seconds, config), request_timeout=5
            )
            state["actions"].append("files.uploaded")
            state["phase"] = "ready"
        elif state["phase"] == "ready":
            state["phase"] = "executing"
            await self.save(call, state, config)  # A lost response must not dispatch Python again.
            seconds = min(config.command_seconds, max(1, math.floor(state["deadline"] - time.time())))
            try:
                await sandbox.commands.run(
                    "python -I " + ROOT + "/runner.py",
                    timeout=seconds + 5,
                    request_timeout=seconds + 7,
                )
            except Exception:
                # The dispatch may have reached the guest. Only inspect the receipt on
                # subsequent polls; never issue the command again under this operation.
                return DeferredToolResult()
        elif state["phase"] == "executing":
            if not await sandbox.files.exists(RESULT_PATH, request_timeout=5):
                return DeferredToolResult()
            try:
                receipt = JobReceipt.model_validate_json(
                    await download(sandbox, RESULT_PATH, MAX_RESULT_BYTES)
                )
            except ValueError:
                state.update(phase="cleanup", error="e2b_result_invalid")
                await self.save(call, state, config)
                return DeferredToolResult()
            state["receipt"] = receipt.model_dump()
            state["actions"].append("python.executed")
            if receipt.exit_code:
                state.update(phase="cleanup", error="e2b_command_failed")
            else:
                state["phase"] = "collecting"
        elif state["phase"] == "collecting":
            try:
                await self.collect(call, sandbox, state, config)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 429 or exc.response.status_code >= 500:
                    raise
                state["error"] = "e2b_output_invalid"
            except (ValueError, UnicodeError):
                state["error"] = "e2b_output_invalid"
            else:
                state["actions"].append("outputs.downloaded")
            state["phase"] = "cleanup"
        else:
            raise ValueError("invalid_e2b_phase")
        state["io_failures"] = 0
        await self.save(call, state, config)
        return DeferredToolResult()

    validate = staticmethod(validate_job)

    @staticmethod
    def runner(seconds, config=None):
        return runner(seconds)

    async def prepare(self, call):
        return None

    async def recover_outputs(self, call, state):
        return None

    async def upload_inputs(self, call, sandbox):
        for name, content in call.arguments["files"].items():
            await sandbox.files.write(ROOT + "/" + name, content, request_timeout=5)

    async def collect(self, call, sandbox, state, config):
        outputs = {}
        for name in call.arguments["outputs"]:
            content = await download(sandbox, ROOT + "/" + name, MAX_FILE_BYTES)
            outputs[name] = {
                "text": content.decode("utf-8"),
                "sha256": hashlib.sha256(content).hexdigest(),
                "size_bytes": len(content),
            }
        state["outputs"] = outputs

    async def save(self, call, state, config):
        # JSON escaping can expand otherwise valid UTF-8 files beyond the public result limit.
        # Reject those observations before persistence/cleanup instead of stranding a finished job.
        if (
            len(json.dumps(self.result(state, config), ensure_ascii=False).encode())
            > call.definition["max_result_bytes"]
        ):
            state.pop("outputs", None)
            state["error"] = "e2b_output_invalid"
            if (
                len(json.dumps(self.result(state, config), ensure_ascii=False).encode())
                > call.definition["max_result_bytes"]
            ):
                state.pop("receipt", None)
                state["error"] = "e2b_result_invalid"
            if state["phase"] != "finished":
                state["phase"] = "cleanup"
        await call.save_state(state)

    @staticmethod
    def result(state, config):
        return {
            "provider": "e2b",
            "template": config.template,
            "sandbox_id": state["sandbox_id"],
            "actions": state["actions"],
            "cleanup_complete": state.get("cleanup_complete", False),
            "outputs": state.get("outputs", {}),
            "receipt": state.get("receipt"),
            **({"error": state["error"]} if state.get("error") else {}),
        }
