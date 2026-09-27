"""Repeat the four foundation scenarios against the reviewed source, in a new ledger."""

import hashlib
import json
import os
from pathlib import Path

import pytest
import test_harness_live as foundation

from scripts.harness_review_budget import MANIFEST, policy


def frozen_sources():
    return {
        str(p): hashlib.sha256(p.read_bytes()).hexdigest()
        for pattern in (
            "agent_runtime/*.py",
            "config/*.json",
            "tests/test_harness*.py",
            "tests/test_general_semantic.py",
            "tests/test_general_integration.py",
            "tests/conftest.py",
            "scripts/*budget.py",
            "scripts/completion_loop_fixtures.py",
            "uv.lock",
        )
        for p in Path().glob(pattern)
    }


async def matrix(store, monkeypatch, directory, live):
    # Reuse the exact scenarios; never mutate the original manifest or retained ledger.
    with monkeypatch.context() as patch:
        patch.setattr(foundation, "MANIFEST", MANIFEST)
        patch.setattr(foundation, "policy", policy)
        patch.setattr(foundation, "frozen_sources", frozen_sources)
        return await foundation.matrix(store, patch, directory, live)


@pytest.mark.integration
async def test_review_acceptance_preflight(pg_store, monkeypatch, tmp_path):
    report = await matrix(pg_store, monkeypatch, tmp_path / "preflight", False)
    assert len(report["scenarios"]) == 4 and all(s["passed"] for s in report["scenarios"]), report
    target = os.environ.get("HARNESS_REVIEW_PREFLIGHT_OUTPUT")
    if target:
        with Path(target).open("x") as file:
            json.dump({"sources": frozen_sources(), "report": report}, file, indent=2)


@pytest.mark.integration
@pytest.mark.live
async def test_review_live_acceptance(pg_store, monkeypatch):
    from dotenv import load_dotenv

    load_dotenv(".env.local", override=False)
    assert os.environ.get("OPENAI_API_KEY"), "Existing authorized credential unavailable"
    directory = Path("var/acceptance") / MANIFEST["campaign"]
    preflight = json.loads((directory / "preflight.json").read_text())
    assert preflight["sources"] == frozen_sources(), "Preflight must match the current source"
    assert all(s["passed"] for s in preflight["report"]["scenarios"])
    report = await matrix(pg_store, monkeypatch, directory, True)
    assert len(report["scenarios"]) == 4 and all(s["passed"] for s in report["scenarios"]), (
        "See bounded results.json; terminal cases are not resubmitted"
    )
