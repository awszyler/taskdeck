from __future__ import annotations

from decimal import Decimal
from pathlib import Path  # noqa: TCH003

import yaml
from taskdeck_core.cost.pricing import Pricing


def test_load_defaults_has_known_models():
    p = Pricing.load()
    assert "anthropic/claude-sonnet-4-6" in p.models
    assert "openai/whisper-1" in p.models


def test_compute_tokens_sonnet():
    p = Pricing.load()
    # 1M input + 1M output = $3 + $15 = $18
    cost = p.compute_tokens("anthropic/claude-sonnet-4-6", 1_000_000, 1_000_000)
    assert cost == Decimal("18.000000")


def test_compute_tokens_small_amounts():
    p = Pricing.load()
    # 1000 input tokens, 500 output tokens for sonnet
    # (1000 * 3 / 1_000_000) + (500 * 15 / 1_000_000) = 0.003 + 0.0075 = 0.0105
    cost = p.compute_tokens("anthropic/claude-sonnet-4-6", 1000, 500)
    assert cost == Decimal("0.010500")


def test_compute_tokens_unknown_model_returns_none():
    p = Pricing.load()
    cost = p.compute_tokens("totally/unknown-model", 1000, 500)
    assert cost is None


def test_compute_audio_whisper():
    p = Pricing.load()
    # 60 seconds * 0.0001 per second = 0.006
    cost = p.compute_audio("openai/whisper-1", 60.0)
    assert cost == Decimal("0.006000")


def test_compute_audio_unknown_model_returns_none():
    p = Pricing.load()
    cost = p.compute_audio("unknown/model", 60.0)
    assert cost is None


def test_override_file_wins(tmp_path: Path):
    override = tmp_path / "prices.yaml"
    override.write_text(yaml.dump({
        "models": {
            "anthropic/claude-sonnet-4-6": {
                "input_per_million": 1.0,
                "output_per_million": 2.0,
            }
        }
    }))
    p = Pricing.load(override)
    cost = p.compute_tokens("anthropic/claude-sonnet-4-6", 1_000_000, 1_000_000)
    # Override: $1 + $2 = $3
    assert cost == Decimal("3.000000")
    # Non-overridden model still available
    assert p.compute_audio("openai/whisper-1", 10.0) is not None


def test_override_file_missing_falls_back_to_defaults(tmp_path: Path):
    nonexistent = tmp_path / "does_not_exist.yaml"
    p = Pricing.load(nonexistent)
    # Should load defaults without error
    assert "anthropic/claude-sonnet-4-6" in p.models


def test_override_path_none_loads_defaults():
    p = Pricing.load(None)
    assert "anthropic/claude-sonnet-4-6" in p.models
