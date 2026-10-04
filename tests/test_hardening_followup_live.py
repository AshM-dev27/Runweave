"""Fresh bounded checks of issues observed in the original paid hardening campaign."""

import json
import os
from pathlib import Path

import pytest
from test_hardening_live import matrix, sources

from scripts.hardening_budget import MODELS
from scripts.hardening_followup_budget import policy


@pytest.mark.integration
@pytest.mark.parametrize("model", MODELS)
async def test_hardening_followup_preflight(pg_store, monkeypatch, tmp_path, model):
    guard = policy(model)
    report = await matrix(pg_store, monkeypatch, tmp_path / model, model, False, guard=guard)
    assert len(report["scenarios"]) == len(guard.MANIFEST["scenario_limits"])
    assert all(s["passed"] for s in report["scenarios"]), report
    if root := os.environ.get("HARDENING_FOLLOWUP_PREFLIGHT_OUTPUT"):
        target = Path(root) / guard.MANIFEST["campaign"]
        target.mkdir(parents=True, exist_ok=True)
        with (target / "preflight.json").open("x") as file:
            json.dump({"sources": sources(), "report": report}, file, indent=2)


@pytest.mark.integration
@pytest.mark.live
@pytest.mark.parametrize("model", MODELS)
async def test_hardening_followup_live(pg_store, monkeypatch, model):
    from dotenv import load_dotenv

    load_dotenv(".env.local", override=False)
    assert os.environ.get("OPENAI_API_KEY")
    guard = policy(model)
    target = Path("var/acceptance") / guard.MANIFEST["campaign"]
    preflight = json.loads((target / "preflight.json").read_text())
    assert preflight["sources"] == sources()
    assert all(s["passed"] for s in preflight["report"]["scenarios"])
    report = await matrix(pg_store, monkeypatch, target, model, True, guard=guard)
    assert len(report["scenarios"]) == len(guard.MANIFEST["scenario_limits"])
    assert all(s["passed"] for s in report["scenarios"]), "Terminal evidence retained; do not resubmit"
