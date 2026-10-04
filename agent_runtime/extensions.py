"""Operator-installed capabilities and skills, snapshotted by content at submission.

No request can import code, choose an endpoint, or enlarge an execution grant.
"""

import importlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, Literal, Protocol
from urllib.parse import urlsplit

from jsonschema import Draft202012Validator
from pydantic import Field, model_validator

from .approval_preview import ApprovalPresentation
from .general_contracts import Contract, EffectPolicy
from .project_store import digest


class ToolDefinition(Contract):
    alias: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_.-]{0,79}$")
    description: str = Field(min_length=1, max_length=1000)
    handler: str = Field(min_length=1, max_length=200)
    version: int = Field(ge=1)
    arguments_schema: dict
    effect: EffectPolicy
    config: dict = Field(default_factory=dict)
    max_result_bytes: int = Field(default=16384, ge=256, le=32768)
    reconciliation: Literal["retry", "lookup"] = "retry"
    approval_presentation: ApprovalPresentation | None = None

    @model_validator(mode="after")
    def valid(self):
        def local_references(value):
            if isinstance(value, dict):
                if any(
                    key in value and not str(value[key]).startswith("#") for key in ("$ref", "$dynamicRef")
                ):
                    raise ValueError("Only local schema references are supported")
                for child in value.values():
                    local_references(child)
            elif isinstance(value, list):
                for child in value:
                    local_references(child)

        local_references(self.arguments_schema)
        Draft202012Validator.check_schema(self.arguments_schema)
        if self.arguments_schema.get("type") != "object":
            raise ValueError("Tool arguments must be an object")
        # These extension effects are never represented as local database transactions.
        if self.effect.kind not in {"read", "external-write"}:
            raise ValueError("Extensions support read or external-write effects")
        if self.effect.kind == "read" and self.effect.retry_safety != "read":
            raise ValueError("Read tools require read retry safety")
        if self.effect.kind == "external-write" and (
            self.effect.approval != "required" or self.effect.retry_safety != "reconcile"
        ):
            raise ValueError("External writes require approval and reconciliation")
        return self


class MCPConfig(Contract):
    url: str
    tool: str = Field(min_length=1, max_length=200)
    credential_env: str | None = Field(default=None, pattern=r"^[A-Z_][A-Z0-9_]*$")
    headers_env: dict[str, str] = Field(default_factory=dict)
    timeout_seconds: int = Field(default=20, ge=1, le=45)
    idempotency_argument: str | None = None

    @model_validator(mode="after")
    def valid(self):
        import re

        parsed = urlsplit(self.url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("MCP requires an explicit credential-free HTTP endpoint")
        for header, env in self.headers_env.items():
            if not re.fullmatch(r"[A-Za-z0-9-]+", header) or not re.fullmatch(r"[A-Z_][A-Z0-9_]*", env):
                raise ValueError("Headers must reference environment variables")
            if header.lower() in {"host", "content-length"}:
                raise ValueError("Forbidden transport header")
        if self.credential_env and any(k.lower() == "authorization" for k in self.headers_env):
            raise ValueError("Duplicate authorization source")
        return self


@dataclass(frozen=True)
class DeferredToolResult:
    retry_after: int = 2


@dataclass(frozen=True)
class ToolCall:
    run_id: str
    operation_id: str
    arguments: dict
    definition: dict
    state: dict = field(default_factory=dict)
    remaining_seconds: float | None = None
    save_state: Callable[[dict], Awaitable[None]] | None = None

    @property
    def idempotency_key(self):
        return self.operation_id


class ToolHandler(Protocol):
    version: int

    async def execute(self, call: ToolCall) -> dict: ...
    async def reconcile(self, call: ToolCall) -> dict | None: ...


class MCPHandler:
    version = 1

    async def execute(self, call):
        from fastmcp import Client
        from fastmcp.client.transports import StreamableHttpTransport

        config = MCPConfig.model_validate(call.definition["config"])
        headers = {k: os.environ[v] for k, v in config.headers_env.items()}
        if config.credential_env:
            headers["Authorization"] = "Bearer " + os.environ[config.credential_env]
        transport = StreamableHttpTransport(config.url, headers=headers)
        async with Client(transport, timeout=config.timeout_seconds) as client:
            available = await client.list_tools()
            remote = next((tool for tool in available if tool.name == config.tool), None)
            if remote is None or remote.input_schema != call.definition["arguments_schema"]:
                raise ValueError("mcp_schema_changed")
            arguments = dict(call.arguments)
            if config.idempotency_argument:
                arguments[config.idempotency_argument] = call.idempotency_key
            Draft202012Validator(call.definition["arguments_schema"]).validate(arguments)
            result = await client.call_tool(config.tool, arguments, timeout=config.timeout_seconds)
            if result.is_error:
                raise ValueError("mcp_tool_failed")
            # Text/structured data only; media are not silently converted into executable inputs.
            return {
                "data": result.data,
                "content": [
                    {"type": "text", "text": block.text} for block in result.content if block.type == "text"
                ],
            }

    async def reconcile(self, call):
        # Only admitted when the operator declares an upstream idempotency argument.
        # The upstream must return the original result for the identical operation key.
        if not MCPConfig.model_validate(call.definition["config"]).idempotency_argument:
            return None
        return await self.execute(call)


class SkillDefinition(Contract):
    alias: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_.-]{0,79}$")
    version: int = Field(ge=1)
    description: str = Field(min_length=1, max_length=512)
    path: str


class Manifest(Contract):
    version: Literal[1] = 1
    tools: list[ToolDefinition] = Field(default_factory=list, max_length=128)
    skills: list[SkillDefinition] = Field(default_factory=list, max_length=64)


def no_inline_secrets(config):
    if isinstance(config, dict):
        for key, value in config.items():
            if key.casefold() in {"api_key", "password", "token", "secret", "authorization", "headers"}:
                raise ValueError("Use credential environment references")
            no_inline_secrets(value)
    elif isinstance(config, list):
        for value in config:
            no_inline_secrets(value)


class ExtensionRegistry:
    def __init__(self, manifest=None, *, handlers=None, base_path=None):
        from .config import settings

        path = Path(settings().extension_registry_file)
        if manifest is None:
            manifest = json.loads(path.read_text()) if path.exists() else {}
            base_path = path.parent
        manifest = Manifest.model_validate(manifest)
        from .general_catalog import CATALOG

        if any(t.alias in CATALOG for t in manifest.tools):
            raise ValueError("Extensions cannot replace built-in capabilities")
        from .browser_use import ARGUMENTS_SCHEMA, BrowserUseConfig, BrowserUseHandler

        self.handlers = {"mcp.http": MCPHandler(), "browser_use.v4": BrowserUseHandler(), **(handlers or {})}
        self.tools, self.skills = {}, {}
        for definition in manifest.tools:
            if definition.alias in self.tools:
                raise ValueError("Duplicate extension alias")
            no_inline_secrets(definition.config)
            if definition.handler == "browser_use.v4":
                BrowserUseConfig.model_validate(definition.config)
                if (
                    definition.effect.kind != "external-write"
                    or definition.arguments_schema != ARGUMENTS_SCHEMA
                    or definition.config.get("credential_env") != "BROWSER_USE_API_KEY"
                ):
                    raise ValueError(
                        "Browser Use requires approved writes, the fixed task schema and credential reference"
                    )
            if definition.handler == "mcp.http":
                if definition.reconciliation == "lookup":
                    raise ValueError("Terminal MCP recovery requires a custom lookup-only handler")
                config = MCPConfig.model_validate(definition.config)
                if definition.effect.kind == "external-write" and not config.idempotency_argument:
                    raise ValueError("MCP writes require upstream idempotency")
                if (
                    config.idempotency_argument
                    and config.idempotency_argument not in definition.arguments_schema.get("properties", {})
                ):
                    raise ValueError("Idempotency argument must exist in the pinned schema")
            handler = self.handlers.get(definition.handler)
            if handler is None:
                # This is operator deployment configuration, never a caller-supplied import.
                module, separator, symbol = definition.handler.partition(":")
                if not separator:
                    raise ValueError("Unknown installed handler")
                handler = getattr(importlib.import_module(module), symbol)()
                self.handlers[definition.handler] = handler
            if handler.version != definition.version or not callable(getattr(handler, "execute", None)):
                raise ValueError("Handler version unavailable")
            if definition.effect.kind == "external-write" and not callable(
                getattr(handler, "reconcile", None)
            ):
                raise ValueError("External write handler requires reconciliation")
            entry = definition.model_dump()
            self.tools[definition.alias] = {**entry, "registration_id": digest(entry)}
        for definition in manifest.skills:
            if definition.alias in self.skills:
                raise ValueError("Duplicate skill alias")
            path = Path(definition.path)
            path = path if path.is_absolute() else Path(base_path or ".") / path
            content = path.read_text()
            if not content.strip() or len(content.encode()) > 4096:
                raise ValueError("Skill must contain 1–4096 bytes")
            entry = {
                "alias": definition.alias,
                "version": definition.version,
                "description": definition.description,
                "content": content,
            }
            self.skills[definition.alias] = {**entry, "registration_id": digest(entry)}

    def snapshots(self, aliases):
        from .general_catalog import CATALOG, snapshot

        if any(alias not in CATALOG and alias not in self.tools for alias in aliases):
            raise ValueError("Unknown v3 capability")
        result = snapshot([alias for alias in aliases if alias in CATALOG])
        for alias in aliases:
            if alias in self.tools:
                definition = self.tools[alias]
                public_schema = json.loads(json.dumps(definition["arguments_schema"]))
                if definition["handler"] == "mcp.http":
                    key = definition["config"].get("idempotency_argument")
                    if key:
                        public_schema.get("properties", {}).pop(key, None)
                        if key in public_schema.get("required", []):
                            public_schema["required"].remove(key)
                entry = {
                    "alias": alias,
                    "description": definition["description"],
                    "handler_version": 3,
                    "arguments_schema": public_schema,
                    "effect": definition["effect"],
                    "extension": definition,
                }
                result[alias] = {**entry, "registration_id": digest(entry)}
        return result

    def selected_skills(self, aliases):
        if len(set(aliases)) != len(aliases) or any(a not in self.skills for a in aliases):
            raise ValueError("Unknown or duplicate skill")
        return {a: self.skills[a] for a in aliases}

    def handler(self, definition):
        expected = digest({k: v for k, v in definition.items() if k != "registration_id"})
        handler = self.handlers.get(definition["handler"])
        if (
            expected != definition["registration_id"]
            or handler is None
            or handler.version != definition["version"]
        ):
            raise ValueError("extension_registration_unavailable")
        return handler

    @staticmethod
    def validate_arguments(entry, arguments):
        if list(Draft202012Validator(entry["arguments_schema"]).iter_errors(arguments)):
            raise ValueError("invalid_extension_arguments")

    @staticmethod
    def result(definition, value):
        if not isinstance(value, dict):
            raise ValueError("invalid_extension_result")
        encoded = json.dumps(value, allow_nan=False, ensure_ascii=False)
        refs = []
        config = definition["config"]
        if config.get("credential_env"):
            refs.append(config["credential_env"])
        refs.extend(config.get("headers_env", {}).values())
        for reference in refs:
            secret = os.environ.get(reference)
            if secret:
                encoded = encoded.replace(json.dumps(secret, ensure_ascii=False)[1:-1], "[REDACTED]")
        if len(encoded.encode()) > definition["max_result_bytes"]:
            raise ValueError("extension_result_limit")
        return {"output": json.loads(encoded), "untrusted": True}
