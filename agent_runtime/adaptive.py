"""Deterministic request sizing and observed-demand adjustments inside pinned grants."""

import copy

DEFAULT_CAPS = dict(
    model_attempts=64, tool_attempts=256, command_attempts=64, total_tokens=128000, active_seconds=1800
)
LEGACY_DEFAULTS = dict(max_requests=6, max_tool_calls=6, max_tokens=1024, timeout_seconds=120)
AUTO_DEFAULTS = dict(max_requests=24, max_tool_calls=96, timeout_seconds=600)


def initial_plan(
    config, registration, *, input_bytes, file_bytes=0, files=0, criteria=0, checks=0, workspace=False
):
    signals = dict(
        input_bytes=input_bytes,
        file_bytes=file_bytes,
        files=files,
        criteria=criteria,
        checks=checks,
        workspace=workspace,
    )
    # Size and explicit task structure estimate initial demand; execution supplies later evidence.
    weight = (
        input_bytes
        + min(file_bytes, 262144) // 4
        + files * 1024
        + checks * 2048
        + criteria * 256
        + int(workspace) * 4096
    )
    scale = 1 if weight < 8192 else 2 if weight < 32768 else 4
    maximum = config.max_tokens or registration.max_output_tokens
    output = min(maximum, 1024 * min(scale, 2))
    return {
        "version": 1,
        "revision": 1,
        "signals": signals,
        "output_tokens": output,
        "max_output_tokens": maximum,
        "context_bytes": min(registration.context_bytes_limit, 24576 * scale),
        "max_context_bytes": registration.context_bytes_limit,
        "allowances": {
            "model_attempts": 4 * scale,
            "tool_attempts": 8 * scale,
            "command_attempts": 2 * scale,
            "total_tokens": 8192 * scale,
            "active_seconds": 120 * scale,
        },
    }


def grow(plan, required, ceilings, *, context_bytes=0, output_tokens=0, truncated=False):
    updated = copy.deepcopy(plan)
    for kind, limit in ceilings.items():
        if kind not in updated["allowances"]:
            continue
        current = min(updated["allowances"][kind], limit)
        demand = min(required.get(kind, 0), limit)
        updated["allowances"][kind] = min(
            limit, max(current, demand, current * 2 if demand > current else current)
        )
    if context_bytes > updated["context_bytes"]:
        updated["context_bytes"] = min(
            updated["max_context_bytes"], max(context_bytes, updated["context_bytes"] * 2)
        )
    if truncated or output_tokens >= updated["output_tokens"] * 0.8:
        updated["output_tokens"] = min(updated["max_output_tokens"], updated["output_tokens"] * 2)
    if updated != plan:
        updated["revision"] += 1
    return updated
