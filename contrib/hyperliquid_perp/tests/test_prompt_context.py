"""Tests for rendering a PerpMarketContext into prompt text.

This is the exact text ``--context-only`` prints, so the invariants under test
are: ``None`` renders as ``n/a`` (never ``NaN``), funding is shown in basis
points, the z-score keeps a sign, and the optional mid/premium lines drop out
when absent.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from contrib.hyperliquid_perp.domains.perp.prompt_context import render_market_context
from contrib.hyperliquid_perp.domains.perp.schema import MarketRegime, PerpMarketContext

_AS_OF = datetime(2024, 1, 2, 3, 4, 5, tzinfo=timezone.utc)


def _ctx(**overrides) -> PerpMarketContext:
    base = {
        "coin": "BTC",
        "as_of": _AS_OF,
        "candle_interval": "4h",
        "candle_count": 200,
        "mark_price": Decimal("60000.0"),
        "oracle_price": Decimal("60001.0"),
        "prev_day_price": Decimal("58000.0"),
        "mid_price": Decimal("60000.5"),
        "day_change_pct": 3.4482758620689653,
        "open_interest": Decimal("1234.5"),
        "day_ntl_volume": Decimal("987654.0"),
        "funding_rate": Decimal("0.0000125"),
        "funding_premium": Decimal("0.000002"),
        "funding_zscore_30d": 1.2345,
        "funding_window_days": 30,
        "funding_sample_count": 240,
        "indicators": {"rsi_14": 55.5, "ema_20": 60100.0, "macd": None},
        "market_regime": "trending",
    }
    base.update(overrides)
    return PerpMarketContext(**base)


def test_render_full_context_includes_all_sections():
    text = render_market_context(_ctx())
    assert "Coin: BTC (perpetual)" in text
    assert "As of: 2024-01-02T03:04:05+00:00 UTC" in text
    assert "Candles: 200 x 4h" in text
    for header in ("Price:", "Market:", "Funding:", "Indicators:"):
        assert header in text
    assert "Regime (computed): trending" in text
    assert "Regime note:" in text


@pytest.mark.parametrize("regime", list(MarketRegime))
def test_regime_note_covers_every_regime_without_directional_framing(regime):
    # The cost-awareness note is keyed to the computed regime (exhaustive over
    # MarketRegime) and must stay free of long/short directional framing —
    # asserted on the note line only, so unrelated context wording (e.g.
    # "longer window") can never trip this guard.
    text = render_market_context(_ctx(market_regime=regime.value))
    note = next(line for line in text.splitlines() if line.strip().startswith("Regime note:"))
    assert note.strip() != "Regime note:"
    assert "long" not in note
    assert "short" not in note


def test_render_funding_in_basis_points_and_signed_zscore():
    text = render_market_context(_ctx())
    # 0.0000125 * 1e4 = 0.125 bps.
    assert "Current rate: 0.1250 bps (per hour)" in text
    # z-score keeps an explicit sign and two decimals, with the sample count.
    assert "30d z-score: +1.23 (n=240)" in text


def test_render_day_change_and_indicator_formatting():
    text = render_market_context(_ctx())
    assert "24h change: 3.45%" in text
    # Known indicators get their label; values use 4 decimals.
    assert "RSI(14): 55.5000" in text
    assert "EMA(20): 60,100.0000" in text


def test_render_none_values_become_na_never_nan():
    text = render_market_context(
        _ctx(
            mid_price=None,
            day_change_pct=None,
            funding_premium=None,
            funding_zscore_30d=None,
            indicators={"macd": None},
        )
    )
    assert "NaN" not in text
    # Optional mid/premium lines are dropped entirely when absent.
    assert "Mid:" not in text
    assert "Premium:" not in text
    # n/a stand-ins for the missing scalars.
    assert "24h change: n/a%" in text
    assert "n/a (insufficient data)" in text
    assert "MACD: n/a" in text


def test_render_unknown_indicator_falls_back_to_raw_name():
    text = render_market_context(_ctx(indicators={"bollinger_99": 1.0}))
    assert "bollinger_99: 1.0000" in text


def test_market_regime_string_is_coerced_to_enum():
    # A plain string is accepted and coerced, so callers/fixtures stay terse.
    ctx = _ctx(market_regime="volatile")
    assert ctx.market_regime is MarketRegime.VOLATILE


def test_unknown_market_regime_raises_at_construction():
    # The point of the enum: an unknown regime fails here, not later at decision
    # time where it would burn an engine run before raising.
    with pytest.raises(ValueError, match="nonsense"):
        _ctx(market_regime="nonsense")
