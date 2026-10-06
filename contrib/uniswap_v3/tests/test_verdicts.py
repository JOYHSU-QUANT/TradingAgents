"""Verdicts: the rating vocabulary, the digest, and the values that refuse what contradicts itself."""

from __future__ import annotations

import hashlib
from dataclasses import replace

import pytest

from contrib.uniswap_v3.domain.verdicts import (
    RATINGS,
    Rating,
    VerdictRecord,
    VerdictSettings,
    text_digest,
)
from contrib.uniswap_v3.tests.fakes.engine import DAY, FIRST_DAY
from contrib.uniswap_v3.tests.fakes.verdicts import SOURCE, record, verdict

_DIGEST = "ab" * 32


def test_the_ratings_are_the_five_tiers_the_graph_answers_in_and_review():
    # The strings are the TradingAgents graph's own (tradingagents/agents/utils/rating.py),
    # which the agent layer reads them from; a drift would show there, not here.
    assert [rating.value for rating in RATINGS] == [
        "Buy",
        "Overweight",
        "Hold",
        "Underweight",
        "Sell",
    ]
    assert Rating.REVIEW.value == "REVIEW" and Rating.REVIEW not in RATINGS
    assert set(Rating) == {*RATINGS, Rating.REVIEW}
    assert Rating.REVIEW.is_review and not any(rating.is_review for rating in RATINGS)
    assert Rating("Buy") is Rating.BUY


def test_a_digest_is_the_sha256_of_the_text_as_utf8():
    for text in ("Rating: Buy", "", "評等：Overweight"):
        assert text_digest(text) == hashlib.sha256(text.encode("utf-8")).hexdigest()
    assert len(text_digest("x")) == 64 and text_digest("x") != text_digest("y")
    with pytest.raises(ValueError, match="taken of a string"):
        text_digest(b"Rating: Buy")  # type: ignore[arg-type]


def test_a_verdict_names_its_source_token_bar_and_rating():
    said = verdict("WBTC", 2, Rating.SELL)
    assert (said.source, said.symbol, said.time, said.rating) == (
        SOURCE,
        "WBTC",
        FIRST_DAY + 2 * DAY,
        Rating.SELL,
    )
    assert said.digest == text_digest(f"WBTC at {said.time}: Sell")
    assert verdict("WBTC", 2, Rating.SELL, words="the same words") == replace(
        said, digest=text_digest("the same words")
    )


@pytest.mark.parametrize(
    ("changes", "match"),
    [
        ({"source": ""}, "source must be letters"),
        ({"source": "a judge"}, "source must be letters"),
        ({"source": "-judge"}, "starting with a letter or a digit"),
        ({"source": 1}, "source must be letters"),
        ({"symbol": " "}, "symbol must be a non-empty string"),
        ({"time": -1}, "time must be a non-negative integer"),
        ({"time": True}, "time must be a non-negative integer"),
        ({"rating": "Buy"}, "rating must be a Rating"),
        ({"digest": "AB" * 32}, "64 lowercase hex digits"),
        ({"digest": "ab" * 31}, "64 lowercase hex digits"),
        ({"digest": None}, "64 lowercase hex digits"),
    ],
)
def test_a_malformed_verdict_is_refused(changes, match):
    with pytest.raises(ValueError, match=match):
        replace(verdict(), **changes)


def test_a_record_names_a_sidecar_with_its_digest_or_not_at_all():
    plain = record()
    assert (plain.sidecar_path, plain.sidecar_digest) == (None, None)
    assert (plain.model, plain.prompt_version, plain.asked_at) == (
        "synthetic",
        "test-1",
        FIRST_DAY + 600,
    )
    kept = record(sidecar_path="verdicts/2024-01-01-WETH.json", sidecar_digest=_DIGEST)
    assert kept.sidecar_digest == _DIGEST
    for changes in ({"sidecar_path": "verdicts/x.json"}, {"sidecar_digest": _DIGEST}):
        with pytest.raises(ValueError, match="or not at all"):
            record(**changes)


@pytest.mark.parametrize(
    ("changes", "match"),
    [
        ({"verdict": "Buy"}, "verdict must be a Verdict"),
        ({"model": ""}, "model must be a non-empty string"),
        ({"prompt_version": " "}, "prompt_version must be a non-empty string"),
        ({"asked_at": -1}, "asked_at must be a non-negative integer"),
        ({"sidecar_path": "", "sidecar_digest": _DIGEST}, "sidecar_path must be a non-empty"),
        ({"sidecar_path": "x.json", "sidecar_digest": "zz" * 32}, "sidecar_digest must be"),
    ],
)
def test_a_malformed_record_is_refused(changes, match):
    with pytest.raises(ValueError, match=match):
        record(**changes)


def test_verdict_settings_name_one_source():
    assert VerdictSettings("tradingagents-rating-v1").source == "tradingagents-rating-v1"
    assert VerdictSettings("a.b_c-1").source == "a.b_c-1"
    for source in ("", "two words", "_x", None):
        with pytest.raises(ValueError, match="source must be letters"):
            VerdictSettings(source)  # type: ignore[arg-type]
    assert isinstance(record(), VerdictRecord)
