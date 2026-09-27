"""Retest only the two failed actor paths after an observed-context correction."""

import json
import os
from pathlib import Path

import pytest
import test_resources_live as foundation

from scripts.resources_completion_budget import MANIFEST, policy


async def matrix(store, monkeypatch, directory, live):
    with monkeypatch.context() as patch:
        patch.setattr(foundation, "MANIFEST", MANIFEST)
        patch.setattr(foundation, "policy", policy)
        return await foundation.matrix(store, patch, directory, live)


@pytest.mark.integration
async def test_resource_completion_preflight(pg_store, monkeypatch, tmp_path):
    report = await matrix(pg_store, monkeypatch, tmp_path / "preflight", False)
    assert len(report["scenarios"]) == 2 and all(s["passed"] for s in report["scenarios"]), report
    target = os.environ.get("RESOURCE_COMPLETION_PREFLIGHT_OUTPUT")
    if target:
        with Path(target).open("x") as file:
            json.dump({"sources": foundation.sources(), "report": report}, file, indent=2)


@pytest.mark.integration
@pytest.mark.live
async def test_resource_completion_live(pg_store, monkeypatch):
    from dotenv import load_dotenv

    load_dotenv(".env.local", override=False)
    assert os.environ.get("OPENAI_API_KEY"), "Existing authorized credential unavailable"
    directory = Path("var/acceptance") / MANIFEST["campaign"]
    preflight = json.loads((directory / "preflight.json").read_text())
    assert preflight["sources"] == foundation.sources(), "Fresh preflight must match the source"
    assert all(s["passed"] for s in preflight["report"]["scenarios"])
    report = await matrix(pg_store, monkeypatch, directory, True)
    assert len(report["scenarios"]) == 2 and all(s["passed"] for s in report["scenarios"]), (
        "Terminal results retained; no retry root"
    )
