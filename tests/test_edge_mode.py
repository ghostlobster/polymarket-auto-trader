"""Tests for the EDGE_MODE source resolution and external adapter toggles."""

from config import Settings
from tools.external import question_fingerprint, score_sentiment


def test_strict_mode_disables_all_externals():
    s = Settings(anthropic_api_key="t", edge_mode="strict")
    sources = s.resolve_sources()
    assert all(v is False for v in sources.values())


def test_full_mode_enables_all_externals():
    s = Settings(anthropic_api_key="t", edge_mode="full")
    sources = s.resolve_sources()
    assert all(v is True for v in sources.values())


def test_hybrid_defaults_known_set():
    s = Settings(anthropic_api_key="t", edge_mode="hybrid")
    sources = s.resolve_sources()
    assert sources["news"] is True
    assert sources["manifold"] is True
    assert sources["metaculus"] is True
    assert sources["polling"] is False
    assert sources["gdelt"] is False


def test_explicit_flag_overrides_strict_default():
    s = Settings(anthropic_api_key="t", edge_mode="strict", manifold_enabled=True)
    sources = s.resolve_sources()
    assert sources["manifold"] is True
    assert sources["news"] is False


def test_question_fingerprint_is_stable_and_slugged():
    fp = question_fingerprint("Will the incumbent president win re-election in 2028?")
    # Stopwords removed, lowercased, joined with hyphens
    assert "incumbent" in fp
    assert "president" in fp
    assert "election" in fp
    assert " " not in fp


def test_sentiment_score_handles_polarity():
    pos = score_sentiment(["Trump wins decisive victory"])
    neg = score_sentiment(["Market crash after Fed rate hike"])
    none = score_sentiment([])
    assert pos is not None and pos > 0
    assert neg is not None and neg < 0
    assert none is None


def test_sentiment_score_handles_negation():
    s = score_sentiment(["He did not win the election"])
    assert s is not None
    # "win" is positive, but negated → score should be ≤0
    assert s <= 0
