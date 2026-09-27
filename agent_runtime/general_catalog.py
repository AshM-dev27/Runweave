"""Retained v3 installed capability metadata; no runtime registration endpoint."""

from .general_contracts import CapabilityDescriptor, EffectPolicy
from .project_store import digest

CATALOG = {
    "session_history": (
        "Retrieve exact earlier messages in this session by message_id, query and offset",
        "read",
        "session",
        "none",
    ),
    "skill_read": (
        "Load an authorized versioned skill; skill instructions never grant tools or files",
        "read",
        "skills",
        "none",
    ),
    "workspace_read": ("Read bounded immutable project bytes", "read", "project", "none"),
    "workspace_search": ("Search literal project text", "read", "project", "none"),
    "workspace_write": (
        "Atomically write/delete files with expected hashes",
        "local-write",
        "project",
        "none",
    ),
    "workspace_patch": ("Apply exact unambiguous text replacements", "local-write", "project", "none"),
    "workspace_command": (
        "Run offline Python or pytest in an isolated project",
        "isolated-command",
        "project",
        "none",
    ),
    "workspace_verify": (
        "Run registered checks or supporting assertions",
        "isolated-command",
        "project",
        "none",
    ),
    "workspace_outputs": ("Inspect immutable operation outputs", "read", "project", "none"),
    "record_note": ("Record an application note after approval", "local-write", "notes", "required"),
    "record_set": (
        "Compare-and-swap an application record after approval",
        "local-write",
        "records",
        "required",
    ),
    "add": ("Add two numbers", "read", "arithmetic", "none"),
}


def snapshot(aliases):
    if any(a not in CATALOG for a in aliases):
        raise ValueError("Unknown v3 capability")
    result = {}
    for a in aliases:
        description, kind, domain, approval = CATALOG[a]
        data = {
            "alias": a,
            "description": description,
            "handler_version": 3,
            "arguments_schema": argument_schema(a),
            "effect": EffectPolicy(kind=kind, domain=domain, approval=approval).model_dump(),
        }
        result[a] = {**data, "registration_id": digest(data)}
    return result


def descriptor(entry):
    return CapabilityDescriptor(
        **{k: entry[k] for k in ("alias", "description", "effect", "registration_id")},
        arguments_schema=entry["arguments_schema"],
        availability="unverified"
        if entry.get("extension") or entry["effect"]["kind"] == "isolated-command"
        else "available",
    )


def argument_schema(alias):
    text = {"type": "string"}
    integer = {"type": "integer"}
    revision = {"expected_revision": text}
    shapes = {
        "session_history": {"message_id": text, "query": text, "offset": integer, "length": integer},
        "skill_read": {"alias": text},
        "workspace_read": {
            "path": text,
            "revision": text,
            "offset": integer,
            "length": integer,
            "criterion_id": text,
            "quote": text,
        },
        "workspace_search": {"query": text, "prefix": text, "cursor": integer},
        "workspace_write": {
            **revision,
            "writes": {
                "type": "array",
                "maxItems": 8,
                "items": {
                    "type": "object",
                    "properties": {
                        "path": text,
                        "expected_sha256": {"type": ["string", "null"]},
                        "content_base64": text,
                        "delete": {"type": "boolean"},
                    },
                    "required": ["path", "expected_sha256"],
                },
            },
        },
        "workspace_patch": {
            **revision,
            "patches": {
                "type": "array",
                "maxItems": 8,
                "items": {
                    "type": "object",
                    "properties": {
                        "path": text,
                        "expected_sha256": text,
                        "hunks": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {"old": text, "new": text},
                                "required": ["old", "new"],
                            },
                        },
                    },
                    "required": ["path", "expected_sha256", "hunks"],
                },
            },
        },
        "workspace_command": {
            **revision,
            "argv": {"type": "array", "maxItems": 32, "items": text},
            "cwd": text,
            "commit": {"type": "boolean"},
            "wall_seconds": integer,
        },
        "workspace_verify": {
            **revision,
            "check_id": text,
            "supporting_check": {"type": "object"},
            "criterion_id": text,
        },
        "workspace_outputs": {"operation_id": text, "name": text, "offset": integer, "length": integer},
        "record_note": {"text": text},
        "record_set": {"key": text, "value": text, "expected_version": integer},
        "add": {"a": {"type": "number"}, "b": {"type": "number"}},
    }
    return {"type": "object", "properties": shapes[alias], "additionalProperties": False}
