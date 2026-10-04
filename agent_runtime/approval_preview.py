"""Bounded operator-defined approval facts, projected from exact arguments and stored receipts."""

import json
from decimal import Decimal
from typing import Literal

from pydantic import Field, model_validator

from .general_contracts import Contract


class ApprovalFact(Contract):
    label: str = Field(min_length=1, max_length=100)
    value: str = Field(max_length=600)


class ApprovalPreview(Contract):
    title: str = Field(min_length=1, max_length=160)
    facts: list[ApprovalFact] = Field(default_factory=list, max_length=12)
    warnings: list[str] = Field(default_factory=list, max_length=4)
    source_operation_id: str | None = None


class PreviewField(Contract):
    label: str = Field(min_length=1, max_length=100)
    source: Literal["arguments", "receipt", "config"] = "arguments"
    path: list[str] = Field(min_length=1, max_length=6)
    format: Literal["text", "money_minor"] = "text"
    currency: str = Field(default="USD", pattern=r"^[A-Z]{3}$")


class ApprovalPresentation(Contract):
    title: str = Field(min_length=1, max_length=160)
    fields: list[PreviewField] = Field(default_factory=list, max_length=12)
    source_capability: str | None = None
    bindings: dict[str, str] = Field(default_factory=dict, max_length=4)
    warnings: list[str] = Field(default_factory=list, max_length=3)

    @model_validator(mode="after")
    def bound_receipt(self):
        if any(f.source == "receipt" for f in self.fields) and not (self.source_capability and self.bindings):
            raise ValueError("Receipt previews require a source capability and exact argument bindings")
        if any(not text or len(text) > 400 for text in self.warnings):
            raise ValueError("Approval warning must contain 1–400 characters")
        return self


def path_value(value, path):
    for key in path:
        if not isinstance(value, dict) or key not in value:
            raise ValueError("Approval field unavailable")
        value = value[key]
    return value


def display_value(value, field):
    if field.format == "money_minor":
        if type(value) is not int:
            raise ValueError("Money must use integer minor units")
        value = f"{field.currency} {Decimal(value) / 100:.2f}"
    elif not isinstance(value, (str, int, float, bool)) and value is not None:
        raise ValueError("Approval facts must be scalar")
    elif not isinstance(value, str):
        value = json.dumps(value, allow_nan=False)
    # Keep control sequences and multi-line upstream text out of terminal presentation.
    value = " ".join(value.split())
    value = "".join(c for c in value if c.isprintable())
    return value[:599] + "…" if len(value) > 600 else value


async def build_preview(db, state, entry, arguments):
    from .extensions import ExtensionRegistry
    from .general_db import GeneralOperationRow

    definition = entry.get("extension", {})
    raw = definition.get("approval_presentation")
    if not raw:
        return None
    spec = ApprovalPresentation.model_validate(raw)
    receipt, source = None, None
    if spec.source_capability:
        checkpoint = state["task_state"].get("checkpoint_id", "") or ""
        identity = checkpoint.removesuffix(":checkpoint")
        operation = await db.get(GeneralOperationRow, identity) if identity else None
        if operation:
            action = operation.data.get("decision", {}).get("action", {})
            result = operation.data.get("result") or {}
            candidate = result.get("output")
            if (
                action.get("capability") == spec.source_capability
                and not result.get("error")
                and isinstance(candidate, dict)
                and all(
                    key in arguments and path in candidate and arguments[key] == candidate[path]
                    for key, path in spec.bindings.items()
                )
            ):
                receipt, source = candidate, identity
    facts, warnings = [], list(spec.warnings)
    for field in spec.fields:
        try:
            value = path_value(
                {"arguments": arguments, "receipt": receipt, "config": definition.get("config", {})}[
                    field.source
                ],
                field.path,
            )
            # Apply the same referenced-credential redaction as ordinary tool observations.
            value = ExtensionRegistry.result(definition, {"value": value})["output"]["value"]
            facts.append(ApprovalFact(label=field.label, value=display_value(value, field)))
        except (ValueError, TypeError):
            warning = "Some details could not be matched to this action. Review the exact arguments."
            if warning not in warnings:
                warnings.append(warning)
    return ApprovalPreview(
        title=spec.title, facts=facts, warnings=warnings, source_operation_id=source
    ).model_dump()
