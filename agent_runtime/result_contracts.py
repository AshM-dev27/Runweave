"""Bounded, deterministic final-answer contracts; no model, tool, or network I/O."""

import json
import math
from typing import Literal

from jsonschema import Draft202012Validator
from pydantic import BaseModel, ConfigDict, Field, model_validator

SCHEMA_BYTES = 4096
ANSWER_BYTES = 16000
SCHEMA_KEYWORDS = {
    "type",
    "const",
    "enum",
    "properties",
    "required",
    "additionalProperties",
    "items",
    "minItems",
    "maxItems",
    "minLength",
    "maxLength",
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "title",
    "description",
}


def bounded_json(value, *, max_depth=16, max_nodes=2048):
    """Reject non-JSON values, nonfinite numbers, and excessive nesting/size."""
    pending, count = [(value, 0)], 0
    while pending:
        item, depth = pending.pop()
        count += 1
        if count > max_nodes or depth > max_depth:
            raise ValueError("JSON complexity limit exceeded")
        if isinstance(item, dict):
            if any(not isinstance(k, str) for k in item):
                raise ValueError("JSON keys must be strings")
            for key in item:
                key.encode("utf-8")
            pending.extend((v, depth + 1) for v in item.values())
        elif isinstance(item, list):
            pending.extend((v, depth + 1) for v in item)
        elif isinstance(item, str):
            item.encode("utf-8")
        elif isinstance(item, float):
            if not math.isfinite(item):
                raise ValueError("Nonfinite JSON number")
        elif item is not None and type(item) not in {str, int, bool}:
            raise ValueError("Non-JSON value")


def validate_schema(schema):
    bounded_json(schema, max_depth=16, max_nodes=512)
    if len(json.dumps(schema, ensure_ascii=False, allow_nan=False).encode()) > SCHEMA_BYTES:
        raise ValueError("Result schema exceeds 4096 bytes")
    pending = [(schema, 0)]
    while pending:
        item, depth = pending.pop()
        if not isinstance(item, dict) or depth > 8 or set(item) - SCHEMA_KEYWORDS:
            raise ValueError("Unsupported result schema; use the bounded JSON Schema subset")
        properties = item.get("properties", {})
        if not isinstance(properties, dict):
            raise ValueError("Schema properties must be an object")
        pending.extend((v, depth + 1) for v in properties.values())
        for key in ("items", "additionalProperties"):
            if key in item and not isinstance(item[key], bool):
                pending.append((item[key], depth + 1))
        if "enum" in item and (not isinstance(item["enum"], list) or len(item["enum"]) > 64):
            raise ValueError("Schema enum exceeds 64 entries")
    try:
        Draft202012Validator.check_schema(schema)
    except Exception:
        raise ValueError("Invalid result JSON Schema") from None


class ResultContract(BaseModel):
    """Applies to the final answer string; absent contracts retain legacy acceptance."""

    model_config = ConfigDict(extra="forbid", strict=True)
    kind: Literal["exact", "json_schema"]
    exact: str | None = Field(default=None, max_length=4096)
    json_schema: dict | None = None

    @model_validator(mode="after")
    def valid(self):
        if self.kind == "exact":
            if self.exact is None or self.json_schema is not None or len(self.exact.encode()) > 4096:
                raise ValueError("Exact contract requires only exact text, at most 4096 UTF-8 bytes")
        elif self.exact is not None or self.json_schema is None:
            raise ValueError("JSON contract requires only json_schema")
        else:
            validate_schema(self.json_schema)
        return self


def parse_answer(answer):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = value
        return result

    def constant(_):
        raise ValueError("Nonfinite JSON number")

    value = json.loads(answer, object_pairs_hook=pairs, parse_constant=constant)
    bounded_json(value)
    return value


def validate_result_contract(contract, answer):
    """Return stable public gap codes without echoing candidate content or schema data."""
    if contract is None:
        return []
    try:
        contract = ResultContract.model_validate(contract)
    except (ValueError, TypeError, RecursionError, UnicodeError):
        return ["result_contract:invalid_contract"]
    try:
        if not isinstance(answer, str) or len(answer.encode()) > ANSWER_BYTES:
            return ["result_contract:answer_limit"]
        if contract.kind == "exact":
            return [] if answer == contract.exact else ["result_contract:exact_mismatch"]
        try:
            value = parse_answer(answer)
        except (ValueError, TypeError, RecursionError):
            return ["result_contract:invalid_json"]
        if not Draft202012Validator(contract.json_schema).is_valid(value):
            return ["result_contract:schema_mismatch"]
    except (ValueError, TypeError, RecursionError, UnicodeError):
        return ["result_contract:invalid_answer"]
    return []
