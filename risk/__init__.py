"""
Deterministic risk-management primitives.

Replaces the previous LLM-driven Kelly computation. Pure Python so unit tests
can exercise it without API calls and so guardrails are mechanically enforced.
"""

from .guardrails import GuardrailReport, evaluate_guardrails
from .kelly import KellyResult, kelly_size

__all__ = ["kelly_size", "KellyResult", "evaluate_guardrails", "GuardrailReport"]
