"""Clarified completion-loop-v1 fixtures; historical fixtures remain untouched."""

from agent_runtime.general_contracts import GeneralPolicy
from agent_runtime.schemas import AgentConfig
from scripts.completion_loop_budget import MANIFEST
from scripts.lightweight_fixtures import actions
from scripts.lightweight_fixtures import fixture as original_fixture


def fixture(name):
    f = original_fixture(name)
    if name == "direct":
        f["prompt"] = (
            "Evaluate 7 + 5 and write sum.txt containing exactly 12 followed by one LF newline (12\\n), then complete. Work directly without children."
        )
    elif name == "csv":
        f["prompt"] += (
            " cleaned.csv must have headers name,amount; summary.csv must have headers valid_rows,rejected_rows,total in that order, one data row. Use comma separators and LF newlines including the final newline; no index column."
        )
    f["task"]["outcome"] = f["prompt"]
    f["task"]["constraints"] = (
        ["Preserve these files byte-for-byte: " + ", ".join(f["preserve"])] if f["preserve"] else []
    )
    return f


def config(name):
    f = fixture(name)
    policy = GeneralPolicy(resources=None, limits={"model_attempts": MANIFEST["scenario_limits"][name]})
    if name == "parallel":
        policy = GeneralPolicy(
            resources=None,
            limits={"model_attempts": 24},
            delegation={"tools": f["tools"], "limits": {"model_attempts": 4}},
        )
    return AgentConfig(
        name=name,
        provider="openai",
        model="gpt-5.6-luna",
        tools=f["tools"],
        max_tokens=MANIFEST["scenario_caps"][name],
        instructions="Complete the task using declared checks and constraints. Return one action per response.",
        general=policy,
    )


__all__ = ["fixture", "config", "actions"]
