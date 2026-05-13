"""Tests for the reference-class prior library."""

from research import default_priors
from research.priors import bayesian_blend


def test_incumbent_template_matches():
    lib = default_priors()
    est = lib.lookup("Politics", "Will the incumbent president be re-elected in 2028?")
    assert est is not None
    assert 0.5 <= est.prior_p <= 0.8


def test_no_match_returns_none():
    lib = default_priors()
    est = lib.lookup("Random", "Will it rain on Tuesday?")
    assert est is None


def test_recession_prior_low():
    lib = default_priors()
    est = lib.lookup("Economics", "Will the US enter a recession in 2026?")
    assert est is not None
    assert est.prior_p < 0.25


def test_bayesian_blend_pulls_toward_prior_when_weight_high():
    lib = default_priors()
    prior = lib.lookup("Politics", "Will the incumbent be re-elected in 2026?")
    assert prior is not None
    blended, w = bayesian_blend(prior, llm_estimate=0.30, llm_weight=2.0)
    # prior weight 8, llm weight 2 → blended pulls strongly toward prior_p (~0.65)
    assert blended > 0.50
    assert w == prior.weight


def test_bayesian_blend_no_prior_returns_llm():
    blended, w = bayesian_blend(None, llm_estimate=0.42, llm_weight=4.0)
    assert blended == 0.42
    assert w == 0.0
