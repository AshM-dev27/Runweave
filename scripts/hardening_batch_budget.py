"""One final structural batch-choice check; earlier campaigns remain terminal."""

from scripts.hardening_followup_budget import policy as previous


def policy():
    guard = previous("gpt-5.6-luna")
    guard.MANIFEST = {
        **guard.MANIFEST,
        "campaign": "hardening-batch-choice-2026-09-27-gpt-5.6-luna-v1",
    }
    return guard
