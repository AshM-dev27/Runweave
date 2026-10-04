"""Operator-only registrations. Connection data never crosses the activity boundary."""

import hashlib
import json
import os
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .config import settings


class Registration(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    provider: str = Field(pattern=r"^[a-zA-Z0-9._-]+$", max_length=100)
    model: str = Field(pattern=r"^[a-zA-Z0-9._-]+$", max_length=100)
    adapter: Literal["fake", "openai_responses", "openai_chat", "anthropic"]
    upstream_model: str = Field(min_length=1, max_length=256)
    endpoint: str | None
    auth: Literal["none", "env"]
    credential_env: str | None
    tool_calling: bool
    max_output_tokens: int = Field(ge=128, le=1000000)
    total_tokens_limit: int = Field(ge=128, le=1000000)

    context_bytes_limit: int = Field(default=24576, ge=24576, le=1048576)

    token_counter: Literal["utf8-v1", "o200k-v1"] = "utf8-v1"

    reasoning_effort: Literal["none", "minimal", "low", "medium", "high"] | None = None

    @model_validator(mode="after")
    def connection(self):
        if self.reasoning_effort is not None and self.adapter not in {"openai_responses", "openai_chat"}:
            raise ValueError("Reasoning settings require an OpenAI adapter")
        if self.auth == "env":
            import re

            if not self.credential_env or not re.fullmatch(r"[A-Z_][A-Z0-9_]*", self.credential_env):
                raise ValueError("Authentication requires an environment-variable reference")
        elif self.credential_env is not None:
            raise ValueError("Unauthenticated registrations cannot reference credentials")
        if self.adapter == "fake":
            if self.endpoint is not None or self.auth != "none":
                raise ValueError("Fake registrations cannot configure connections")
        else:
            url = urlsplit(self.endpoint or "")
            if (
                url.scheme not in {"http", "https"}
                or not url.hostname
                or url.username
                or url.password
                or url.query
                or url.fragment
            ):
                raise ValueError("An explicit endpoint without credentials or query parameters is required")
            if self.adapter != "openai_chat" and self.auth != "env":
                raise ValueError("This adapter requires authentication")
        if self.max_output_tokens > self.total_tokens_limit:
            raise ValueError("Output limit exceeds total token budget")
        return self

    @property
    def identity(self):
        data = self.model_dump()
        # Preserve identities of retained registrations created before this optional field.
        if self.context_bytes_limit == 24576:
            data.pop("context_bytes_limit")
        if self.token_counter == "utf8-v1":
            data.pop("token_counter")
        if self.reasoning_effort is None:
            data.pop("reasoning_effort")
        encoded = json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()

    def available(self):
        return self.auth == "none" or bool(os.environ.get(self.credential_env))


class Registry:
    def __init__(self, entries):
        self.entries = {}
        for entry in entries:
            registration = Registration.model_validate(entry)
            key = (registration.provider, registration.model)
            if key in self.entries:
                raise ValueError("Duplicate provider/model alias")
            self.entries[key] = registration
        if not self.entries:
            raise ValueError("Registry cannot be empty")

    def select(self, config):
        registration = self.entries.get((config.provider, config.model))
        if registration is None:
            raise ValueError("Unsupported provider/model combination")
        if not registration.tool_calling:
            raise ValueError("Model does not support required tool calling")
        if config.max_tokens is not None and config.max_tokens > registration.max_output_tokens:
            raise ValueError("Requested output tokens exceed registration limit")
        return registration


def load_registry():
    try:
        path = settings().model_registry_file
        return Registry(json.loads(Path(path).read_text()))
    except Exception:
        raise RuntimeError("Invalid operator model registry") from None
