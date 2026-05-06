"""Copy-trading subsystem (parallel to the thesis pipeline)."""
from .strategies import (
    PRESETS,
    CopyDecision,
    StrategyPreset,
    apply_preset,
    get_preset,
)

__all__ = [
    "PRESETS", "StrategyPreset", "CopyDecision", "apply_preset", "get_preset",
]
