"""Source-frozen batch action-availability check, with at most three paid requests."""

import json
import os
from pathlib import Path

import pytest
from test_hardening_live import matrix, sources

from scripts.hardening_batch_budget import policy


@pytest.mark.integration
async def test_batch_choice_preflight(pg_store, monkeypatch, tmp_path):
    guard = policy()
    report = await matrix(pg_store, monkeypatch, tmp_path, guard.MANIFEST["model"], False, guard=guard)
    assert len(report["scenarios"]) == 1 and report["scenarios"][0]["passed"]
    if root := os.environ.get("HARDENING_BATCH_PREFLIGHT_OUTPUT"):
        target = Path(root) / guard.MANIFEST["campaign"]
        target.mkdir(parents=True, exist_ok=True)
        with (target / "preflight.json").open("x") as file:
            json.dump({"sources": sources(), "report": report}, file, indent=2)


@pytest.mark.integration
@pytest.mark.live
async def test_batch_choice_live(pg_store, monkeypatch):
    from dotenv import load_dotenv

    load_dotenv(".env.local", override=False)
    assert os.environ.get("OPENAI_API_KEY")
    guard = policy()
    target = Path("var/acceptance") / guard.MANIFEST["campaign"]
    preflight = json.loads((target / "preflight.json").read_text())
    assert preflight["sources"] == sources() and preflight["report"]["scenarios"][0]["passed"]
    report = await matrix(pg_store, monkeypatch, target, guard.MANIFEST["model"], True, guard=guard)
    assert len(report["scenarios"]) == 1 and report["scenarios"][0]["passed"], (
        "Terminal evidence retained; do not resubmit"
    )
