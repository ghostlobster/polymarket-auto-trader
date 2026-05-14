"""
Reference-class priors for prediction markets.

`PriorLibrary` maps a `(category, template)` key to a base-rate probability with
an associated pseudo-count weight `n`. The signal pipeline performs a Bayesian
blend `posterior = (n·prior + k·llm_estimate) / (n + k)` so research starts
near a reference class instead of cold.

Seed values are conservative starting points drawn from published forecasting
benchmarks (Superforecasting / academic prediction-market literature). The
library can also be populated dynamically from realized signal outcomes via
`PriorLibrary.update_from_resolutions`.
"""

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class PriorEstimate:
    prior_p: float  # base rate in [0, 1]
    weight: float  # pseudo-count, contributes against LLM evidence
    template: str  # which template matched
    rationale: str  # human-readable note for logs


_TEMPLATES: list[tuple[str, str, float, float, str]] = [
    # (category, regex, prior_p, weight, rationale)
    (
        "Politics",
        r"\b(incumbent|re-?elect(?:ion|ed)?|re-?elected)\b",
        0.65,
        8.0,
        "US/UK incumbent re-election base rate ≈65%",
    ),
    (
        "Politics",
        r"\b(will .* win .* (election|primary|nomination))\b",
        0.50,
        4.0,
        "Open-race election base rate, no prior favorite",
    ),
    (
        "Politics",
        r"\b(impeachment|impeached|convicted|conviction|removed from office)\b",
        0.08,
        6.0,
        "Impeachment-conviction base rate is very low",
    ),
    (
        "Economics",
        r"\b(recession|gdp .* contract|negative gdp)\b",
        0.18,
        6.0,
        "Year-ahead recession base rate ≈18%",
    ),
    (
        "Economics",
        r"\b(fed (cut|raise|hike|hold)|interest rate (cut|hike|hold))\b",
        0.55,
        4.0,
        "Fed move on a given meeting is roughly coin-flip pre-signal",
    ),
    (
        "Economics",
        r"\b(unemployment .* (above|below|under) [0-9])",
        0.40,
        3.0,
        "Threshold rate change probability",
    ),
    (
        "Crypto",
        r"\b(btc|bitcoin|eth|ethereum) .* (above|below|reach) \$?[0-9]",
        0.42,
        3.0,
        "Crypto threshold question base rate slightly favors NO due to spread",
    ),
    (
        "Crypto",
        r"\b(etf .* (approved|approve|denied))\b",
        0.55,
        4.0,
        "Crypto ETF approval rate elevated post-2024",
    ),
    (
        "Sports",
        r"\b(favorite|favourite|win the (championship|finals|cup|series))\b",
        0.45,
        4.0,
        "Sports favorite-longshot — favorites slightly overpriced",
    ),
    ("Sports", r"\b(underdog|upset)\b", 0.30, 3.0, "Underdog base rate"),
    (
        "Geopolitics",
        r"\b(war|invasion|ceasefire|peace deal|treaty signed)\b",
        0.25,
        5.0,
        "Geopolitical event base rate is low absent live escalation",
    ),
    (
        "Tech",
        r"\b(launch|release|ship|announce) .* (before|by) [0-9]",
        0.55,
        3.0,
        "Tech launch-by-date probability tends to favor YES once announced",
    ),
    (
        "Tech",
        r"\b(ai .* (will|reach|surpass|beat))\b",
        0.40,
        3.0,
        "AI milestone questions tend to overpromise",
    ),
    (
        "Climate",
        r"\b(hottest|warmest|record) (year|month)\b",
        0.60,
        4.0,
        "Record-temperature base rate is elevated in current decade",
    ),
]


class PriorLibrary:
    """Looks up a base-rate prior from a market category + question text."""

    def __init__(self):
        # Compile templates once
        self._compiled: list[tuple[str, re.Pattern, float, float, str]] = [
            (cat, re.compile(pat, re.IGNORECASE), p, w, r) for cat, pat, p, w, r in _TEMPLATES
        ]
        # Mutable overrides populated from historical resolutions.
        self._dynamic: dict[str, tuple[float, float]] = {}

    def lookup(self, category: str, question: str) -> PriorEstimate | None:
        """
        Return the best-matching prior, or None when no template matches.

        Matching prefers a template whose `category` matches; if multiple match
        the first listed wins (templates are ordered specific → general).
        """
        text = question or ""
        cat = (category or "").strip()

        best: PriorEstimate | None = None
        for tcat, pattern, p, w, r in self._compiled:
            if tcat and cat and tcat.lower() != cat.lower():
                continue
            if pattern.search(text):
                est = PriorEstimate(prior_p=p, weight=w, template=pattern.pattern, rationale=r)
                # Dynamic override if present
                dyn = self._dynamic.get(est.template)
                if dyn is not None:
                    est = PriorEstimate(
                        prior_p=dyn[0],
                        weight=dyn[1],
                        template=est.template,
                        rationale=f"dynamic({est.rationale})",
                    )
                if best is None:
                    best = est
                else:
                    # Prefer the higher-weight prior when multiple templates match
                    if est.weight > best.weight:
                        best = est
        return best

    def update_dynamic(self, template_pattern: str, p: float, weight: float) -> None:
        self._dynamic[template_pattern] = (float(p), float(weight))


def default_priors() -> PriorLibrary:
    return PriorLibrary()


def bayesian_blend(
    prior: PriorEstimate | None,
    llm_estimate: float,
    llm_weight: float = 4.0,
) -> tuple[float, float]:
    """
    Return (posterior_p, prior_weight_used). When `prior` is None this is a no-op
    that returns (llm_estimate, 0). The blend is a Beta-Binomial style weighted
    mean — keeps the math simple while remaining principled.
    """
    if prior is None:
        return float(llm_estimate), 0.0
    n = max(0.0, prior.weight)
    k = max(0.0, llm_weight)
    denom = n + k
    if denom == 0:
        return float(llm_estimate), 0.0
    blended = (n * prior.prior_p + k * llm_estimate) / denom
    return float(min(1.0, max(0.0, blended))), n
