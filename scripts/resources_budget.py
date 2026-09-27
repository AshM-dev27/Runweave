"""Fresh bounded resource-policy validation; historical campaign allowances stay closed."""

from scripts.harness_budget import MANIFEST as BASE
from scripts.harness_budget import policy as base_policy

MANIFEST = {
    **BASE,
    "campaign": "resources-live-2026-09-26-v1",
    "limit": 17,
    "scenario_limits": {"resume": 3, "review_pass": 1, "review_repair": 1, "parallel": 12},
    "scenario_caps": {name: 1024 for name in ("resume", "review_pass", "review_repair", "parallel")},
    "parallel_root_limit": 8,
    "parallel_child_limit": 3,
}


def policy():
    value = base_policy()
    value.MANIFEST = MANIFEST
    value.TRIGGERS = {**value.TRIGGERS, "hard_limit": value.TRIGGERS["hard_limit"].replace(">=16", ">=17")}
    return value
