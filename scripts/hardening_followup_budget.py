"""Targeted follow-up, independent of the terminal five-scenario campaigns."""

from scripts.hardening_budget import policy as original


def policy(model):
    guard = original(model)
    limits = {"resume": 3, "parallel": 12} if model == "gpt-4.1-mini" else {"batch": 3}
    total = sum(limits.values())
    guard.MANIFEST = {
        **guard.MANIFEST,
        "campaign": "hardening-followup-2026-09-27-" + model + "-v1",
        "limit": total,
        "scenario_limits": limits,
        "scenario_caps": dict.fromkeys(limits, 1024),
    }
    guard.TRIGGERS = {
        **guard.TRIGGERS,
        "hard_limit": guard.TRIGGERS["hard_limit"].replace(">=22", ">=" + str(total)),
    }
    return guard
