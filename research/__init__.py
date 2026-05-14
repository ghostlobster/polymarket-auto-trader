"""
Research-layer primitives — reference-class priors and the bias detector.

These modules are deterministic feature extractors that feed the LLM-based
SignalGenerator and the deterministic RiskManager. Keeping them pure Python
makes them unit-testable and lets the ensemble combine them with LLM outputs.
"""

from .bias_detector import BiasReport, detect_biases
from .priors import PriorEstimate, PriorLibrary, default_priors

__all__ = [
    "PriorLibrary",
    "PriorEstimate",
    "default_priors",
    "BiasReport",
    "detect_biases",
]
