"""Stateful, serialized Python jobs on a bounded conversation-owned computer."""

import copy
import hashlib
import json
import math
import os
import time

import httpx
from pydantic import Field

from . import e2b
from .e2b import JobReceipt, runner
from .e2b_artifacts import ARGUMENTS_SCHEMA as FILE_SCHEMA
from .e2b_artifacts import E2BArtifactConfig, E2BArtifactHandler, validate_job
from .extensions import DeferredToolResult

HANDLER = "e2b.session.python.v1"
ARGUMENTS_SCHEMA = copy.deepcopy(FILE_SCHEMA)
ARGUMENTS_SCHEMA["properties"]["computer"] = {
    "type": "string",
    "pattern": "^[a-z][a-z0-9_-]{0,39}$",
    "description": "Named computer within this conversation. A closed/expired name is never silently recreated.",
}
ARGUMENTS_SCHEMA["required"] = [*ARGUMENTS_SCHEMA["required"], "computer"]


class E2BSessionConfig(E2BArtifactConfig):
    session_seconds: int = Field(default=900, strict=True, ge=60, le=3600)
    idle_seconds: int = Field(default=120, strict=True, ge=15, le=300)
    max_sessions: int = Field(default=2, strict=True, ge=1, le=4)
    max_operations: int = Field(default=32, strict=True, ge=1, le=128)


class E2BSessionHandler(E2BArtifactHandler):
    version = 1
    config_type = E2BSessionConfig

    async def execute(self, call):
        config = self.config_type.model_validate(call.definition["config"])
        arguments = {k: v for k, v in call.arguments.items() if k != "computer"}
        validate_job(arguments)
        if call.state:
            return await self.advance(call)
        if call.computers is None or call.save_state is None:
            return {"error": "computer_sessions_unavailable"}
        if not os.environ.get(config.credential_env):
            return {"error": "e2b_not_configured"}
        remaining = config.sandbox_seconds if call.remaining_seconds is None else call.remaining_seconds
        seconds = math.floor(min(config.sandbox_seconds, remaining))
        if seconds < 1:
            return {"error": "e2b_time_limit"}
        error = await self.prepare(call)
        if error:
            return {"error": error}
        computer = await call.computers.claim(
            call.arguments["computer"], call.definition, config, provider="e2b"
        )
        if computer.get("error"):
            return {"error": computer["error"]}
        if computer.get("wait"):
            return DeferredToolResult(retry_after=2)
        state = {
            "computer_id": computer["id"],
            "phase": "creating" if computer["status"] == "creating" else "uploading",
            "sandbox_id": computer["sandbox_id"],
            "operation_digest": computer["operation_digest"],
            "deadline": min(computer["expires_at"], time.time() + seconds),
            "actions": [],
        }
        await call.save_state(state)
        if state["phase"] == "creating" and await call.computers.begin_create(computer["id"]):
            try:
                sandbox = await e2b.sandbox_class().create(
                    template=config.template,
                    timeout=max(1, math.floor(computer["expires_at"] - time.time())),
                    secure=True,
                    allow_internet_access=False,
                    metadata={"runweave_operation": state["operation_digest"]},
                    api_key=os.environ[config.credential_env],
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
                # A rejected create proves no machine was acquired, unlike transport failure.
                await call.computers.reject_create(computer["id"])
                state.update(phase="finished", error="e2b_create_rejected", cleanup_complete=True)
                await call.save_state(state)
                return self.result(state, config)
            await call.computers.acquired(computer["id"], sandbox.sandbox_id)
            state.update(phase="uploading", sandbox_id=sandbox.sandbox_id, actions=["sandbox.created"])
            await call.save_state(state)
        return DeferredToolResult()

    async def reconcile(self, call):
        return await self.execute(call)

    async def cleanup(self, call):
        if call.state.get("phase") == "finished":
            return self.result(call.state, self.config_type.model_validate(call.definition["config"]))
        if not call.state:
            # Admission can precede saving handler state; recover the durable holder by name.
            computer = await call.computers.owned(call.arguments["computer"])
            if computer is None:
                return {"error": "e2b_job_not_started", "cleanup_complete": True}
            state = {
                "computer_id": computer["id"],
                "sandbox_id": computer.get("sandbox_id"),
                "phase": "cleanup",
                "actions": [],
            }
        else:
            state = dict(call.state)
        if not await call.computers.close(state["computer_id"]):
            return DeferredToolResult()
        state.update(phase="finished", error=state.get("error", "e2b_job_cancelled"), cleanup_complete=True)
        await call.save_state(state)
        return self.result(state, self.config_type.model_validate(call.definition["config"]))

    async def advance_io(self, call, state, config, *, abandon):
        computer = await call.computers.current(state["computer_id"])
        if computer["status"] == "closed":
            state.update(phase="finished", error="computer_session_closed", cleanup_complete=True)
            await call.save_state(state)
            return self.result(state, config)
        if abandon or computer["status"] in {"closing", "unknown"} or time.time() >= state["deadline"]:
            state["error"] = (
                "e2b_job_cancelled"
                if abandon
                else "computer_session_unavailable"
                if computer["status"] in {"closing", "unknown"}
                else "e2b_time_limit"
            )
            state["phase"] = "cleanup"
        if state.get("error"):
            if not await call.computers.close(state["computer_id"]):
                return DeferredToolResult()
            state.update(phase="finished", cleanup_complete=True)
            await call.save_state(state)
            return self.result(state, config)
        if not state.get("sandbox_id"):
            identity = computer.get("sandbox_id") or await self.locate(
                state, {"api_key": os.environ[config.credential_env], "request_timeout": 10}
            )
            if not identity:
                return None
            await call.computers.acquired(state["computer_id"], identity)
            state.update(sandbox_id=identity, phase="uploading", actions=["sandbox.recovered"])
            await call.save_state(state)
        await self.recover_outputs(call, state)
        if state["phase"] == "cleanup":
            # Completed jobs release exclusive use, but keep the computer's admission slot.
            state["computer"] = await call.computers.ready(state["computer_id"], config.idle_seconds, state)
            state.update(phase="finished", session_retained=True)
            await call.save_state(state)
            return self.result(state, config)
        opts = {"api_key": os.environ[config.credential_env], "request_timeout": 10}
        try:
            sandbox = await e2b.sandbox_class().connect(
                state["sandbox_id"], timeout=max(1, math.floor(computer["expires_at"] - time.time())), **opts
            )
        except Exception as exc:
            from e2b import NotFoundException

            if not isinstance(exc, NotFoundException):
                raise
            state.update(phase="cleanup", error="computer_session_expired")
            await call.save_state(state)
            return DeferredToolResult()
        control = "/home/user/.runweave/" + hashlib.sha256(call.operation_id.encode()).hexdigest()
        if state["phase"] == "uploading":
            from .store import Problem

            await sandbox.files.make_dir(e2b.ROOT, request_timeout=5)
            await sandbox.files.make_dir(control, request_timeout=5)
            try:
                await self.upload_inputs(call, sandbox)
            except (Problem, ValueError, FileNotFoundError):
                state.update(phase="cleanup", error="e2b_input_invalid")
            else:
                program = runner(
                    config.command_seconds,
                    control_root=control,
                    outputs=call.arguments["outputs"],
                    inputs=call.arguments["artifacts"],
                )
                program = program.replace(
                    "1048576, 1048576", f"{config.max_output_bytes}, {config.max_output_bytes}"
                ).replace(
                    "268435456, 268435456", f"{config.memory_mb * 1048576}, {config.memory_mb * 1048576}"
                )
                await sandbox.files.write(control + "/code.py", call.arguments["code"], request_timeout=5)
                await sandbox.files.write(control + "/runner.py", program, request_timeout=5)
                state["actions"].append("files.uploaded")
                state["phase"] = "ready"
        elif state["phase"] == "ready":
            state["phase"] = "executing"
            await call.save_state(state)  # A lost dispatch response never reruns this command.
            seconds = min(config.command_seconds, max(1, math.floor(state["deadline"] - time.time())))
            try:
                await sandbox.commands.run(
                    "python -I " + control + "/runner.py", timeout=seconds + 5, request_timeout=seconds + 7
                )
            except Exception:
                return DeferredToolResult()
        elif state["phase"] == "executing":
            path = control + "/result.json"
            if not await sandbox.files.exists(path, request_timeout=5):
                return DeferredToolResult()
            try:
                state["receipt"] = JobReceipt.model_validate(
                    json.loads(await e2b.download(sandbox, path, 16384))
                ).model_dump()
            except (ValueError, UnicodeError):
                state.update(phase="cleanup", error="e2b_result_invalid")
            else:
                state["actions"].append("python.executed")
                state["phase"] = "collecting" if state["receipt"]["exit_code"] == 0 else "cleanup"
                if state["receipt"]["exit_code"] != 0:
                    state["error"] = "e2b_command_failed"
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
            raise ValueError("invalid_computer_phase")
        state["io_failures"] = 0
        await self.save(call, state, config)
        return DeferredToolResult()

    @staticmethod
    def validate(arguments):
        from jsonschema import Draft202012Validator

        Draft202012Validator(ARGUMENTS_SCHEMA).validate(arguments)
        validate_job({k: v for k, v in arguments.items() if k != "computer"})

    @staticmethod
    def result(state, config):
        return {
            "provider": "e2b",
            "computer_id": state["computer_id"],
            "actions": state["actions"],
            "outputs": state.get("outputs", {}),
            "receipt": state.get("receipt"),
            "session_retained": state.get("session_retained", False),
            **({"computer": state["computer"]} if state.get("computer") else {}),
            **({"cleanup_complete": True} if state.get("cleanup_complete") else {}),
            **({"error": state["error"]} if state.get("error") else {}),
        }
