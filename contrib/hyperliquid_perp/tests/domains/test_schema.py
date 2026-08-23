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
    ProfileShape,
    VolumeProfile,
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


def test_perp_position_rejects_zero_size():
    # size == 0 is a third state is_long/is_short both call False — a flat account
    # must be None, never a zero-size instance. Enforced at construction so no path
    # can sneak one past the mapper's None-for-flat mapping.
    with pytest.raises(ValueError, match="non-zero"):
        PerpPosition(**_position(size=Decimal("0")))


@pytest.mark.parametrize("field", ["leverage", "liquidation_price"])
@pytest.mark.parametrize("bad", [Decimal("0"), Decimal("-1")])
def test_perp_position_rejects_nonpositive_leverage_and_liquidation(field, bad):
    # leverage/liquidation_price are optional, but a value that survives the mapper's
    # _opt_dec is well-formed by definition — a non-positive one is a corrupt record,
    # not an absent field. Mirror the entry_price > 0 guard.
    with pytest.raises(ValueError, match=field):
        PerpPosition(**_position(**{field: bad}))


@pytest.mark.parametrize("field", ["margin_used", "position_value"])
def test_perp_position_rejects_negative_magnitude(field):
    # margin_used/position_value are magnitudes (>= 0); margin_used in particular
    # feeds current_position_state's margin_pct division. A negative value is corrupt.
    with pytest.raises(ValueError, match=field):
        PerpPosition(**_position(**{field: Decimal("-1")}))


def test_perp_position_allows_zero_magnitudes():
    # Zero margin_used/position_value is legal (>= 0), unlike the strict price guards.
    pos = PerpPosition(**_position(margin_used=Decimal("0"), position_value=Decimal("0")))
    assert pos.margin_used == Decimal("0") and pos.position_value == Decimal("0")


def test_account_snapshot_rejects_duplicate_coin():
    # position_for returns the first match, so a duplicate coin would silently drop
    # the second position and misreport exposure — the exchange never reports two
    # positions for one coin, so reject it at construction.
    dupes = (PerpPosition(**_position()), PerpPosition(**_position(size=Decimal("2"))))
    with pytest.raises(ValueError, match="duplicate coin"):
        AccountSnapshot(
            account_value=Decimal("1000"),
            withdrawable=Decimal("500"),
            total_margin_used=Decimal("500"),
            positions=dupes,
        )


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
    # host but is wrong elsewhere (the audit log rejects naive timestamps too).
    with pytest.raises(ValueError, match="timezone-aware"):
        PerpMarketContext(**_context(as_of=datetime(2026, 6, 29, 12, 0)))


def test_perp_market_context_exchange_time_defaults_absent_and_must_be_aware():
    # Issue #51: the exchange clock is optional (fixtures/replays carry none)
    # but, like as_of, never naive — the guard subtracts the two.
    assert PerpMarketContext(**_context()).exchange_time is None
    aware = datetime(2026, 6, 29, 12, 0, tzinfo=timezone.utc)
    assert PerpMarketContext(**_context(), exchange_time=aware).exchange_time == aware
    with pytest.raises(ValueError, match="exchange_time must be timezone-aware"):
        PerpMarketContext(**_context(), exchange_time=datetime(2026, 6, 29, 12, 0))


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


def _profile(**overrides) -> dict:
    base = {
        "shape": ProfileShape.D,
        "poc": Decimal("105"),
        "value_area_low": Decimal("103"),
        "value_area_high": Decimal("107"),
        "range_low": Decimal("100"),
        "range_high": Decimal("110"),
        "poc_position": 0.5,
        "close_position": 0.5,
        "value_area_width_ratio": 0.4,
        "candle_count": 30,
        "bucket_count": 24,
    }
    base.update(overrides)
    return base


def test_volume_profile_builds_from_consistent_values():
    profile = VolumeProfile(**_profile())
    assert profile.shape is ProfileShape.D
    assert profile.poc == Decimal("105")


@pytest.mark.parametrize(
    ("overrides", "match"),
    [
        ({"range_low": Decimal("0")}, "range_low must be > 0"),
        ({"range_high": Decimal("100")}, "must be > range_low"),
        ({"value_area_high": Decimal("103")}, "must be > .*value_area_low"),
        # A value area escaping the range, on either side.
        ({"value_area_low": Decimal("99")}, "must sit inside the range"),
        ({"value_area_high": Decimal("111")}, "must sit inside the range"),
        # The value area is grown outward FROM the POC bucket, so a POC outside
        # it means the walk and the POC disagree about which bucket won.
        ({"poc": Decimal("108")}, "must sit inside the value area"),
        ({"poc_position": 1.5}, "poc_position"),
        ({"close_position": -0.1}, "close_position"),
        ({"value_area_width_ratio": 0.0}, "value_area_width_ratio"),
        ({"value_area_width_ratio": 1.5}, "value_area_width_ratio"),
        ({"candle_count": 0}, "candle_count must be >= 1"),
        ({"bucket_count": 0}, "bucket_count must be >= 1"),
    ],
)
def test_volume_profile_rejects_self_contradictory_values(overrides, match):
    # The point of the guards: a profile whose bounds contradict each other would
    # render as a confident, nonsensical price level in the prompt.
    with pytest.raises(ValueError, match=match):
        VolumeProfile(**_profile(**overrides))


def test_volume_profile_coerces_a_plain_shape_string():
    assert VolumeProfile(**_profile(shape="thin")).shape is ProfileShape.THIN
    assert VolumeProfile(**_profile(shape="b")).shape is ProfileShape.B


def test_volume_profile_rejects_an_unknown_shape():
    with pytest.raises(ValueError, match="nonsense"):
        VolumeProfile(**_profile(shape="nonsense"))


def test_profile_shape_values_render_as_the_articles_letters():
    # (str, Enum) members render as "ProfileShape.P" through an f-string under
    # 3.12, so the renderer must print .value — pin what .value actually is.
    assert [s.value for s in ProfileShape] == ["D", "P", "b", "thin"]


def test_perp_market_context_volume_profile_defaults_to_absent():
    # Off by default: merging the feature must not change any existing prompt.
    assert PerpMarketContext(**_context()).volume_profile is None


def test_perp_market_context_carries_a_volume_profile_when_given_one():
    profile = VolumeProfile(**_profile())
    ctx = PerpMarketContext(**_context(), volume_profile=profile)
    assert ctx.volume_profile is profile
