"""Offline checks for the fresh, isolated reasoning campaign."""

import json
from pathlib import Path

from agent_runtime.registry import Registry
from scripts import showcase_accuracy as benchmark
from scripts.showcase_reasoning import MANIFEST, OUTPUT_CAP, arm, registrations


async def test_reasoning_campaign_enforces_wire_limits_and_restores_baseline():
    old_directory, old_manifest = benchmark.DIRECTORY, benchmark.MANIFEST
    with arm():
        assert benchmark.DIRECTORY != old_directory
        assert benchmark.MANIFEST["reasoning"] == "medium"
        await benchmark.check_guard({"fixture": "fixed"})
    assert benchmark.DIRECTORY == old_directory
    assert benchmark.MANIFEST == old_manifest


def test_reasoning_registration_is_isolated_and_preserves_other_settings():
    registry = Registry(json.loads(Path("config/models.json").read_text()))
    key = ("openai", MANIFEST["model"])
    original = registry.entries[key].model_dump()
    compared = registrations(registry)
    assert compared.entries[key].model_dump() == {
        **original,
        "reasoning_effort": "medium",
        "max_output_tokens": OUTPUT_CAP,
    }
    assert registry.entries[key].model_dump() == original
    assert {k: v for k, v in compared.entries.items() if k != key} == {
        k: v for k, v in registry.entries.items() if k != key
    }
    assert MANIFEST["limit"] == sum(MANIFEST["scenario_limits"].values()) == 30
    assert set(MANIFEST["scenario_caps"].values()) == {8192}
