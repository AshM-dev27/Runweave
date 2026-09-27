"""Independent follow-up after completed-action context correction; no allowance transfer."""

from scripts.resources_budget import MANIFEST as BASE
from scripts.resources_budget import policy as base_policy

MANIFEST = {
    **BASE,
    "campaign": "resources-completion-2026-09-26-v1",
    "limit": 15,
    "scenario_limits": {"resume": 3, "parallel": 12},
    "scenario_caps": {"resume": 1024, "parallel": 1024},
}


def policy():
    value = base_policy()
    value.MANIFEST = MANIFEST
    value.TRIGGERS = {**value.TRIGGERS, "hard_limit": value.TRIGGERS["hard_limit"].replace(">=17", ">=15")}
    return value
