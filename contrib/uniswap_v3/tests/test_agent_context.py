"""The tickers the judge is asked about, and the spot context it is handed."""

from __future__ import annotations

import math
from decimal import Decimal

import pytest

from contrib.uniswap_v3.agent.context import BARS_NEEDED, CHANGE_SPANS, VOL_WINDOW, spot_context
from contrib.uniswap_v3.agent.tickers import TICKERS, tickers_for
from contrib.uniswap_v3.config import ConfigError

D = Decimal
# 2024-01-01 00:00:00 UTC.
_AT = 1_704_067_200


def _context(closes, **overrides):
    arguments = {
        "symbol": "WETH",
        "ticker": "ETH-USD",
        "quote": "USDC",
        "traded": ["WBTC", "WETH"],
        "time": _AT,
        "interval_seconds": 86_400,
    }
    return spot_context([D(close) for close in closes], **{**arguments, **overrides})


def test_the_tickers_are_the_assets_the_wrapped_tokens_stand_for():
    assert dict(TICKERS) == {"WETH": "ETH-USD", "WBTC": "BTC-USD"}
    assert tickers_for({"WETH", "WBTC"}) == {"WBTC": "BTC-USD", "WETH": "ETH-USD"}
    assert tickers_for(["WBTC"]) == {"WBTC": "BTC-USD"}


def test_a_token_without_a_ticker_refuses_the_whole_set():
    with pytest.raises(ConfigError, match=r"no ticker is known for \['DAI', 'USDC'\]"):
        tickers_for(["WETH", "USDC", "DAI"])
    with pytest.raises(ConfigError, match=r"the judge can be asked about \['WBTC', 'WETH'\]"):
        tickers_for(["DAI"])


def test_the_context_names_the_market_the_bar_the_close_and_the_role():
    text = _context(["2000"])
    assert "ETH-USD is traded as WETH against USDC" in text
    assert "A bar closes every 86400 seconds" in text
    assert "the bar being judged closed at 2024-01-01T00:00:00Z" in text
    assert "- Close: 2000.00 USDC." in text
    assert "a spot rebalance between USDC, WBTC and WETH that trades once per bar" in text
    assert (
        "The rating sets how much of a rule-capped position in WETH is held, the most on Buy "
        "and the least on Sell." in text
    )
    assert "Rate ETH-USD on its own; the other tokens are rated separately." in text
    assert text.endswith(
        "End the decision with one line that reads `Rating: <rating>`, the rating one of "
        "Buy, Overweight, Hold, Underweight or Sell, and nothing after it."
    )
    # The context gives no answer format, no multipliers (each run's own) and nothing of the
    # rule's state: the rating is read from the graph's own decision.
    assert "format" not in text.lower() and "JSON" not in text
    assert "zero" not in text and "trend" not in text and "0.75" not in text
    assert "a spot rebalance between USDC and WETH that trades" in _context(["1"], traded=["WETH"])


def test_the_spans_and_the_window_are_what_the_context_measures_over():
    assert CHANGE_SPANS == (1, 7, 30) and VOL_WINDOW == 20 and BARS_NEEDED == 31


def test_the_changes_are_measured_over_the_spans_and_the_volatility_over_the_window():
    # Rising 1% a bar: every log return is the same, so the volatility measured is zero.
    closes = [D("1000") * D("1.01") ** step for step in range(BARS_NEEDED)]
    text = _context(closes, traded=["WETH"])
    assert "over 1 bar(s): +1.00%; over 7 bar(s): +7.21%; over 30 bar(s): +34.78%" in text
    assert "over the last 20 bar(s): 0.0% annualised (sample" in text


def test_alternating_returns_give_the_sample_deviation_annualised_by_the_bar_length():
    closes = [D("100") if step % 2 == 0 else D("110") for step in range(VOL_WINDOW + 1)]
    # Twenty returns of +ln(1.1) and -ln(1.1), with mean zero.
    expected = math.log(1.1) * math.sqrt(VOL_WINDOW / (VOL_WINDOW - 1)) * math.sqrt(365) * 100
    assert f"over the last 20 bar(s): {expected:.1f}% annualised" in _context(closes)
    hourly = math.log(1.1) * math.sqrt(VOL_WINDOW / (VOL_WINDOW - 1)) * math.sqrt(365 * 24) * 100
    assert f"{hourly:.1f}% annualised" in _context(closes, interval_seconds=3_600)


def test_too_few_closes_leave_a_span_or_the_volatility_unmeasured():
    text = _context(["100", "99"])
    assert "over 1 bar(s): -1.00%" in text
    assert "over 7 bar(s): not measured (no bar 7 bar(s) back)" in text
    assert "over 30 bar(s): not measured (no bar 30 bar(s) back)" in text
    assert "over the last 20 bar(s): not measured (1 of 20 returns measured)" in text
    assert "over 1 bar(s): not measured (no bar 1 bar(s) back)" in _context(["100"])
    assert "(0 of 20 returns measured)" in _context(["100"])


def test_a_gap_in_the_closes_is_told_as_a_gap():
    # Twenty-one boundaries, the eighth from the end missing: no change over 7 bars, and
    # the two returns that would cross the gap left out of the volatility.
    closes: list[Decimal | None] = [
        D("100") if step % 2 == 0 else D("110") for step in range(VOL_WINDOW + 1)
    ]
    closes[-8] = None
    text = spot_context(
        closes,
        symbol="WETH",
        ticker="ETH-USD",
        quote="USDC",
        traded=["WBTC", "WETH"],
        time=_AT,
        interval_seconds=86_400,
    )
    assert "over 1 bar(s): -9.09%" in text
    assert "over 7 bar(s): not measured (no bar 7 bar(s) back)" in text
    # Eighteen returns of +ln(1.1) and -ln(1.1), nine of each, with mean zero.
    expected = math.log(1.1) * math.sqrt(18 / 17) * math.sqrt(365) * 100
    assert f"{expected:.1f}% annualised (18 of 20 returns measured)" in text
    # The bar being judged is there, or there is nothing to judge.
    closes[-1] = None
    with pytest.raises(ValueError, match="at least the close"):
        spot_context(
            closes,
            symbol="WETH",
            ticker="ETH-USD",
            quote="USDC",
            traded=["WETH"],
            time=_AT,
            interval_seconds=86_400,
        )


def test_a_small_price_is_given_six_digits_and_no_change_is_not_signed_negative():
    assert "- Close: 0.0123457 USDC." in _context(["0.0123456789"])
    assert "over 1 bar(s): +0.00%" in _context(["100", "100"])
    assert "over 1 bar(s): +0.00%" in _context(["100", "99.9999"])


def test_closes_that_are_not_prices_are_refused():
    with pytest.raises(ValueError, match="at least the close"):
        _context([])
    with pytest.raises(ValueError, match="positive price"):
        _context(["100", "0"])
    with pytest.raises(ValueError, match="at least one traded token"):
        _context(["100"], traded=[])
