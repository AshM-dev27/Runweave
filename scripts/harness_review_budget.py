"""Fresh review campaign; same bounds as the retained foundation evaluation."""

from scripts.harness_budget import MANIFEST as ORIGINAL
from scripts.harness_budget import policy as original_policy

MANIFEST = {**ORIGINAL, "campaign": "harness-review-2026-09-24-v1"}


def policy():
    value = original_policy()
    value.MANIFEST = MANIFEST
    return value
