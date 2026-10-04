"""Offline enforcement of the controlled model comparison."""

import json
from pathlib import Path

import pytest

from agent_runtime.registry import Registry
from scripts import showcase_accuracy as benchmark
from scripts.showcase_model_comparison import MODELS, arm, manifest, registrations


@pytest.mark.parametrize("model", MODELS)
async def test_comparison_model_preserves_input_budget_and_has_independent_guard(model):
    registry = Registry(json.loads(Path("config/models.json").read_text()))
    original = registry.entries[("openai", "gpt-5.6-luna")].model_dump()
    compared = registrations(registry, model)
    entry = compared.entries[("openai", model)].model_dump()
    assert entry == {**original, "model": model, "upstream_model": model, "reasoning_effort": "none"}
    assert registry.entries[("openai", "gpt-5.6-luna")].model_dump() == original
    assert ("openai", model) not in registry.entries
    old_directory, old_manifest = benchmark.DIRECTORY, benchmark.MANIFEST
    with arm(model):
        assert benchmark.DIRECTORY != old_directory
        assert benchmark.MANIFEST["model"] == model
        await benchmark.check_guard({"fixture": "fixed"})
    assert benchmark.DIRECTORY == old_directory and benchmark.MANIFEST == old_manifest


def test_comparison_has_fixed_models_and_disjoint_allowances():
    with pytest.raises(ValueError, match="outside this comparison"):
        manifest("unapproved-model")
    arms = [manifest(model) for model in MODELS]
    assert len({m["campaign"] for m in arms}) == len(MODELS)
    assert sum(m["limit"] for m in arms) == 60
    for entry in arms:
        assert set(entry["scenario_limits"].values()) == {3}
        assert set(entry["scenario_caps"].values()) == {2048}
        assert entry["reasoning"] == "none"
