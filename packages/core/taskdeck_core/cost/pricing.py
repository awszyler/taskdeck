from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Self

import yaml


@dataclass(frozen=True)
class ModelPricing:
    input_per_million: Decimal | None = None
    output_per_million: Decimal | None = None
    audio_per_second: Decimal | None = None


@dataclass
class Pricing:
    models: dict[str, ModelPricing]

    @classmethod
    def load(cls, override_path: Path | None = None) -> Self:
        defaults_path = Path(__file__).parent / "default_prices.yaml"
        models = cls._parse(defaults_path)
        if override_path and override_path.exists():
            override = cls._parse(override_path)
            models.update(override)
        return cls(models=models)

    @staticmethod
    def _parse(path: Path) -> dict[str, ModelPricing]:
        data = yaml.safe_load(path.read_text()) or {}
        out: dict[str, ModelPricing] = {}
        for name, info in (data.get("models") or {}).items():
            out[name] = ModelPricing(
                input_per_million=Decimal(str(info["input_per_million"])) if "input_per_million" in info else None,
                output_per_million=Decimal(str(info["output_per_million"])) if "output_per_million" in info else None,
                audio_per_second=Decimal(str(info["audio_per_second"])) if "audio_per_second" in info else None,
            )
        return out

    def compute_tokens(
        self, model: str, tokens_in: int, tokens_out: int
    ) -> Decimal | None:
        p = self.models.get(model)
        if p is None or p.input_per_million is None or p.output_per_million is None:
            return None
        cost = (
            Decimal(tokens_in) * p.input_per_million / Decimal(1_000_000)
            + Decimal(tokens_out) * p.output_per_million / Decimal(1_000_000)
        )
        return cost.quantize(Decimal("0.000001"))

    def compute_audio(self, model: str, seconds: float) -> Decimal | None:
        p = self.models.get(model)
        if p is None or p.audio_per_second is None:
            return None
        return (Decimal(str(seconds)) * p.audio_per_second).quantize(Decimal("0.000001"))
