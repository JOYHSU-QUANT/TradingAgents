"""Tests for the construction-time invariants on the perp schema value objects."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from contrib.hyperliquid_perp.domains.perp.schema import (
    AccountSnapshot,
    CandleInterval,
    FundingPoint,
    MarketSnapshot,
    PerpMarketContext,
    PerpPosition,
)


def _market(**overrides) -> dict:
    """Valid MarketSnapshot kwargs; override one field to probe a single guard."""
    base = {
        "coin": "BTC",
        "mark_price": Decimal("60000"),
        "oracle_price": Decimal("60000"),
        "prev_day_price": Decimal("59000"),
        "open_interest": Decimal("1000"),
        "day_ntl_volume": Decimal("5000000"),
        "funding": Decimal("-0.0001"),  # negative funding is normal — must be allowed
    }
    base.update(overrides)
    return base


def test_market_snapshot_valid_construction_with_negative_funding():
    # A negative funding rate is a real market state, not an error — it must build.
    snap = MarketSnapshot(**_market())
    assert snap.funding == Decimal("-0.0001")


@pytest.mark.parametrize("field", ["mark_price", "oracle_price"])
@pytest.mark.parametrize("bad", [Decimal("0"), Decimal("-1")])
def test_market_snapshot_rejects_nonpositive_price(field, bad):
    # A zero/negative mark would make current_exposure_pct silently report 0%
    # exposure instead of failing — reject it at construction.
    with pytest.raises(ValueError, match=field):
        MarketSnapshot(**_market(**{field: bad}))


def test_market_snapshot_rejects_nonpositive_mid_price():
    with pytest.raises(ValueError, match="mid_price"):
        MarketSnapshot(**_market(mid_price=Decimal("0")))


@pytest.mark.parametrize("field", ["prev_day_price", "open_interest", "day_ntl_volume"])
def test_market_snapshot_rejects_negative_magnitude(field):
    with pytest.raises(ValueError, match=field):
        MarketSnapshot(**_market(**{field: Decimal("-1")}))


@pytest.mark.parametrize("field", ["prev_day_price", "open_interest", "day_ntl_volume"])
def test_market_snapshot_allows_zero_magnitude(field):
    # A newly listed coin legitimately reports 0 for prevDayPx / open interest /
    # volume on its first day — these are >= 0 magnitudes, not strictly-positive
    # prices, so zero must build (guards the >= vs > boundary against drift).
    snap = MarketSnapshot(**_market(**{field: Decimal("0")}))
    assert getattr(snap, field) == Decimal("0")


@pytest.mark.parametrize("field", ["account_value", "withdrawable", "total_margin_used"])
def test_account_snapshot_rejects_negative_margin(field):
    # A negative account_value would make current_exposure_pct early-return 0%,
    # masking a corrupt feed as a flat account — reject it at construction.
    base = {
        "account_value": Decimal("1000"),
        "withdrawable": Decimal("500"),
        "total_margin_used": Decimal("500"),
    }
    base[field] = Decimal("-1")
    with pytest.raises(ValueError, match=field):
        AccountSnapshot(**base)


def test_account_snapshot_allows_zero_margin_used():
    # A fully-unused account (no open positions) is valid: zero margin is allowed.
    snap = AccountSnapshot(
        account_value=Decimal("1000"),
        withdrawable=Decimal("1000"),
        total_margin_used=Decimal("0"),
    )
    assert snap.total_margin_used == Decimal("0")


@pytest.mark.parametrize("bad", [Decimal("0"), Decimal("-1")])
def test_account_snapshot_rejects_nonpositive_account_value(bad):
    # account_value must be strictly > 0: a zero value would make current_exposure_pct
    # early-return 0% and let the rebalancer try to OPEN positions on a margin-called
    # account. Reject it at construction (the >= 0 magnitude rule is for the other two).
    with pytest.raises(ValueError, match="account_value"):
        AccountSnapshot(
            account_value=bad,
            withdrawable=Decimal("0"),
            total_margin_used=Decimal("0"),
        )


def _position(**overrides) -> dict:
    """Valid PerpPosition kwargs; override one field to probe a single guard."""
    base = {
        "coin": "BTC",
        "size": Decimal("1"),
        "entry_price": Decimal("60000"),
        "unrealized_pnl": Decimal("0"),
    }
    base.update(overrides)
    return base


@pytest.mark.parametrize("bad", [Decimal("0"), Decimal("-1")])
def test_perp_position_rejects_nonpositive_entry_price(bad):
    # entry_price is a strictly positive reference serialized verbatim into the audit
    # log / prompt; a zero/negative entry is a structurally corrupt position record.
    with pytest.raises(ValueError, match="entry_price"):
        PerpPosition(**_position(entry_price=bad))


def test_perp_position_valid_short_with_negative_size_builds():
    # A short carries a negative size but a positive entry_price — must build.
    pos = PerpPosition(**_position(size=Decimal("-1")))
    assert pos.is_short and pos.entry_price == Decimal("60000")


@pytest.mark.parametrize("bad", ["", "   "])
def test_market_snapshot_rejects_empty_coin(bad):
    # An empty/whitespace coin keys position_for() and the audit filename; it would
    # silently miss an open position (read as flat) — reject it at construction.
    with pytest.raises(ValueError, match="coin"):
        MarketSnapshot(**_market(coin=bad))


@pytest.mark.parametrize("bad", ["", "   "])
def test_perp_position_rejects_empty_coin(bad):
    with pytest.raises(ValueError, match="coin"):
        PerpPosition(**_position(coin=bad))


def _context(**overrides) -> dict:
    """Valid PerpMarketContext kwargs; override one field to probe a single guard."""
    base = {
        "coin": "BTC",
        "as_of": datetime(2026, 6, 29, 12, 0, tzinfo=timezone.utc),
        "candle_interval": "4h",
        "candle_count": 100,
        "mark_price": Decimal("60000"),
        "oracle_price": Decimal("60000"),
        "prev_day_price": Decimal("59000"),
        "mid_price": Decimal("60000"),
        "day_change_pct": 1.5,
        "open_interest": Decimal("1000"),
        "day_ntl_volume": Decimal("5000000"),
        "funding_rate": Decimal("-0.0001"),
        "funding_premium": None,
        "funding_zscore_30d": None,
        "funding_window_days": 30,
        "funding_sample_count": 0,
    }
    base.update(overrides)
    return base


def test_perp_market_context_valid_construction():
    ctx = PerpMarketContext(**_context())
    assert ctx.coin == "BTC" and ctx.as_of.tzinfo is timezone.utc


@pytest.mark.parametrize("field", ["mark_price", "oracle_price"])
@pytest.mark.parametrize("bad", [Decimal("0"), Decimal("-1")])
def test_perp_market_context_rejects_nonpositive_price(field, bad):
    # Mirror the MarketSnapshot guard so a directly-built context can't carry a
    # zero/negative price that current_exposure_pct would silently read as 0% exposure.
    with pytest.raises(ValueError, match=field):
        PerpMarketContext(**_context(**{field: bad}))


def test_perp_market_context_rejects_negative_candle_count():
    with pytest.raises(ValueError, match="candle_count"):
        PerpMarketContext(**_context(candle_count=-1))


@pytest.mark.parametrize("bad", [0, -1])
def test_perp_market_context_rejects_subunit_funding_window(bad):
    # A funding_window_days < 1 makes the z-score window keep nothing and silently
    # degrade to None — indistinguishable from a real data shortage. Reject it.
    with pytest.raises(ValueError, match="funding_window_days"):
        PerpMarketContext(**_context(funding_window_days=bad))


def test_perp_market_context_rejects_negative_funding_sample_count():
    with pytest.raises(ValueError, match="funding_sample_count"):
        PerpMarketContext(**_context(funding_sample_count=-1))


@pytest.mark.parametrize("bad", ["", "   "])
def test_perp_market_context_rejects_empty_coin(bad):
    with pytest.raises(ValueError, match="coin"):
        PerpMarketContext(**_context(coin=bad))


def test_perp_market_context_rejects_naive_as_of():
    # A naive as_of serializes to an offset-less ISO string that looks UTC on a UTC
    # host but is wrong elsewhere (build_log_record rejects naive timestamps too).
    with pytest.raises(ValueError, match="timezone-aware"):
        PerpMarketContext(**_context(as_of=datetime(2026, 6, 29, 12, 0)))


def test_perp_market_context_coerces_enum_interval_to_value():
    # A caller passing the CandleInterval *member* (not the "4h" string) must be stored
    # as the plain ".value" string — otherwise a (str, Enum) member renders as
    # "CandleInterval.H4" through an f-string under 3.12, corrupting the rendered prompt.
    ctx = PerpMarketContext(**_context(candle_interval=CandleInterval.H4))
    assert ctx.candle_interval == "4h"
    assert type(ctx.candle_interval) is str  # the plain string, not the enum member
    assert f"{ctx.candle_interval}" == "4h"  # render-safe


def test_perp_market_context_rejects_unknown_interval():
    with pytest.raises(ValueError, match="unsupported candle_interval"):
        PerpMarketContext(**_context(candle_interval="7m"))


def test_funding_point_rejects_nonpositive_time():
    # time is a UTC epoch-ms timestamp; a non-positive value is a corrupt record that
    # funding_zscore's window filter would silently drop, biasing the sample.
    FundingPoint(time=1, rate=Decimal("0.0001"))  # smallest valid time builds
    for bad in (0, -1):
        with pytest.raises(ValueError, match="FundingPoint.time must be > 0"):
            FundingPoint(time=bad, rate=Decimal("0.0001"))
