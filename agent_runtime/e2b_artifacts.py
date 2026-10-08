"""Versioned E2B file execution. Only immutable references enter observations/history."""

import httpx
from pydantic import Field

from .artifacts import validate_metadata
from .e2b import ROOT, E2BConfig, E2BHandler, download_chunks, runner, safe_path

ARGUMENTS_SCHEMA = {
    "type": "object",
    "properties": {
        "code": {"type": "string", "minLength": 1, "maxLength": 8192},
        "artifacts": {
            "type": "object",
            "maxProperties": 8,
            "description": "Map guest relative paths to artifact IDs attached to this run or produced by it.",
            "additionalProperties": {"type": "string", "minLength": 1, "maxLength": 36},
        },
        "outputs": {
            "type": "object",
            "minProperties": 1,
            "maxProperties": 8,
            "description": "Map guest relative paths to downloadable output metadata. Bytes are stored outside tool receipts.",
            "additionalProperties": {
                "type": "object",
                "properties": {
                    "filename": {"type": "string", "minLength": 1, "maxLength": 100},
                    "media_type": {"type": "string", "minLength": 1, "maxLength": 80},
                },
                "required": ["filename", "media_type"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["code", "artifacts", "outputs"],
    "additionalProperties": False,
}


class E2BArtifactConfig(E2BConfig):
    max_input_bytes: int = Field(default=64 * 1024 * 1024, strict=True, ge=16384, le=256 * 1024 * 1024)
    max_output_bytes: int = Field(default=16 * 1024 * 1024, strict=True, ge=4096, le=256 * 1024 * 1024)
    memory_mb: int = Field(default=512, strict=True, ge=128, le=2048)


def validate_job(arguments):
    from jsonschema import Draft202012Validator

    Draft202012Validator(ARGUMENTS_SCHEMA).validate(arguments)
    if len(arguments["code"].encode()) > 8192:
        raise ValueError("e2b_code_limit")
    names = set(arguments["artifacts"]) | set(arguments["outputs"])
    for name in names:
        safe_path(name)
    if any("/".join(name.split("/")[:i]) in names for name in names for i in range(1, len(name.split("/")))):
        raise ValueError("e2b_path_collision")
    for output in arguments["outputs"].values():
        validate_metadata(output["media_type"], output["filename"])


class E2BArtifactHandler(E2BHandler):
    version = 2
    config_type = E2BArtifactConfig
    validate = staticmethod(validate_job)

    async def prepare(self, call):
        from .store import Problem

        if call.artifacts is None:
            return "e2b_artifacts_unavailable"
        config = self.config_type.model_validate(call.definition["config"])
        try:
            references = [await call.artifacts.resolve(id) for id in call.arguments["artifacts"].values()]
        except Problem:
            return "artifact_not_authorized"
        if sum(ref.size_bytes for ref in references) > config.max_input_bytes:
            return "e2b_input_limit"
        return None

    async def upload_inputs(self, call, sandbox):
        from .store import Problem

        for name, artifact_id in call.arguments["artifacts"].items():
            try:
                async with call.artifacts.file(artifact_id) as (_, path):
                    await upload_file(sandbox, ROOT + "/" + name, path)
            except Problem:
                raise ValueError("e2b_input_invalid") from None

    async def recover_outputs(self, call, state):
        if state["phase"] != "collecting" or call.artifacts is None:
            return
        state["outputs"] = dict(state.get("outputs", {}))
        for name in call.arguments["outputs"]:
            if name not in state["outputs"]:
                reference = await call.artifacts.published(name)
                if reference is not None:
                    state["outputs"][name] = reference.model_dump(mode="json")
        if set(state["outputs"]) == set(call.arguments["outputs"]):
            state["phase"] = "cleanup"
            if "outputs.downloaded" not in state["actions"]:
                state["actions"].append("outputs.downloaded")

    @staticmethod
    def runner(seconds, config=None):
        config = config or E2BArtifactConfig()
        return (
            runner(seconds)
            .replace("1048576, 1048576", f"{config.max_output_bytes}, {config.max_output_bytes}")
            .replace("268435456, 268435456", f"{config.memory_mb * 1048576}, {config.memory_mb * 1048576}")
        )

    async def collect(self, call, sandbox, state, config):
        from .store import Problem

        if call.artifacts is None:
            raise ValueError("e2b_artifacts_unavailable")
        config = self.config_type.model_validate(call.definition["config"])
        state["outputs"] = dict(state.get("outputs", {}))
        for name, output in call.arguments["outputs"].items():
            if name in state["outputs"]:
                continue
            try:
                reference = await call.artifacts.publish(
                    name,
                    download_chunks(sandbox, ROOT + "/" + name, config.max_output_bytes),
                    output["media_type"],
                    output["filename"],
                )
            except Problem:
                raise ValueError("e2b_artifact_publish_failed") from None
            state["outputs"][name] = reference.model_dump(mode="json")
            # A lost publication response reuses an operation/path idempotency key.
            # Already published files survive guest expiry and worker restarts.
            await self.save(call, state, config)


async def upload_file(sandbox, guest_path, path):
    # SDK 2.3.0 files.write(IO) calls read() without a size; use its public signed
    # upload URL with httpx's bounded multipart file stream instead.
    url = sandbox.upload_url(guest_path, use_signature_expiration=60)
    async with httpx.AsyncClient(timeout=30, trust_env=False, follow_redirects=False) as client:
        with path.open("rb") as source:
            async with client.stream(
                "POST", url, files={"file": ("input", source, "application/octet-stream")}
            ) as response:
                response.raise_for_status()
