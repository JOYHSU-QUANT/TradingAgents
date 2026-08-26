"""Tests for the construction-time invariants on the perp schema value objects."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from contrib.hyperliquid_perp.common.constants import (
    MIN_VOLUME_PROFILE_WINDOW,
    VOLUME_PROFILE_BUCKET_COUNT,
)
from contrib.hyperliquid_perp.domains.perp.schema import (
    AccountSnapshot,
    CandleInterval,
    FundingPoint,
    MarketSnapshot,
    PerpMarketContext,
    PerpPosition,
    ProfileShape,
    VolumeProfile,
    derive_day_change_pct,
    derive_profile_shape,
    interval_to_ms,
)

from .test_volume_profile import _shaped


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
        # Derived from the two prices above, the way context_builder does it,
        # because the DTO now cross-checks them. The first version of this
        # fixture said 1.5 — a rounded guess off by 0.19 points — and no test
        # noticed, which is the gap the guard closes.
        "day_change_pct": float((Decimal("60000") - Decimal("59000")) / Decimal("59000") * 100),
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


def test_interval_to_ms_known_intervals():
    assert interval_to_ms("4h") == 4 * 60 * 60_000
    assert interval_to_ms("1d") == 24 * 60 * 60_000
    assert interval_to_ms("1m") == 60_000


def test_interval_to_ms_unknown_raises_valueerror():
    # A typo like "4H" (wrong case) must raise a clear ValueError naming the
    # value rather than silently selecting a wrong interval.
    with pytest.raises(ValueError, match="4H"):
        interval_to_ms("4H")


def test_perp_market_context_host_reading_must_be_aware_and_paired():
    # Issue #94: the two rules PR #91 added beside the exchange-clock one. The
    # host reading is only ever subtracted from ``exchange_time``, so it must
    # be aware like its partner — and it must HAVE a partner: one without the
    # other is a half-built context that would read as "skew unknown" while
    # looking populated.
    exchange = datetime(2026, 6, 29, 12, 0, tzinfo=timezone.utc)
    paired = PerpMarketContext(
        **_context(), exchange_time=exchange, host_time_at_exchange_read=exchange
    )
    assert paired.host_time_at_exchange_read == exchange
    with pytest.raises(ValueError, match="host_time_at_exchange_read must be timezone-aware"):
        PerpMarketContext(
            **_context(),
            exchange_time=exchange,
            host_time_at_exchange_read=datetime(2026, 6, 29, 12, 0),
        )
    with pytest.raises(ValueError, match="host_time_at_exchange_read requires exchange_time"):
        PerpMarketContext(**_context(), host_time_at_exchange_read=exchange)


def test_perp_market_context_coerces_enum_interval_to_value():
    # A caller passing the CandleInterval *member* (not the "4h" string) must be stored
    # as the plain ".value" string — otherwise a (str, Enum) member renders as
    # "CandleInterval.H4" through an f-string under 3.12, corrupting the rendered prompt.
    ctx = PerpMarketContext(**_context(candle_interval=CandleInterval.H4))
    assert ctx.candle_interval == "4h"
    assert type(ctx.candle_interval) is str  # the plain string, not the enum member
    assert f"{ctx.candle_interval}" == "4h"  # render-safe


def test_perp_market_context_rejects_unknown_interval():
    # The message is interval_to_ms's — one check, one wording — and names the
    # offending value.
    with pytest.raises(ValueError, match="unsupported candle interval '7m'"):
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
        # These agree with the prices above — (105-100)/10 and (107-103)/10 —
        # because VolumeProfile cross-checks them. Overriding a fraction on its
        # own now fails construction, which is the guard doing its job.
        "poc_position": 0.5,
        "close_position": 0.5,
        "value_area_width_ratio": 0.4,
        "poc_volume_share": 0.2,
        "value_area_volume_share": 0.72,
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
        # The counts are pinned to the producer: it refuses a window below
        # MIN_VOLUME_PROFILE_WINDOW rather than narrowing it, and always
        # buckets on VOLUME_PROFILE_BUCKET_COUNT — so 11 candles or 23 buckets
        # never came from a walk. Issue #100: these used to admit 1-11 and
        # anything but 24.
        ({"candle_count": 0}, "candle_count must be >= 12"),
        ({"candle_count": MIN_VOLUME_PROFILE_WINDOW - 1}, "candle_count must be >= 12"),
        ({"bucket_count": 0}, "bucket_count must be 24"),
        ({"bucket_count": VOLUME_PROFILE_BUCKET_COUNT - 1}, "bucket_count must be 24"),
        ({"bucket_count": VOLUME_PROFILE_BUCKET_COUNT + 1}, "bucket_count must be 24"),
        # Shares of the window's VOLUME: a share of zero means the POC bucket
        # traded nothing, which contradicts it being the heaviest bucket.
        ({"poc_volume_share": 0.0}, "poc_volume_share"),
        ({"poc_volume_share": 1.5}, "poc_volume_share"),
        ({"value_area_volume_share": 0.0}, "value_area_volume_share"),
        ({"value_area_volume_share": 1.5}, "value_area_volume_share"),
        # The value area is grown outward FROM the POC bucket, so the POC's
        # share is one of the buckets the area holds and cannot exceed it.
        ({"poc_volume_share": 0.9}, "cannot exceed"),
        # Floors the walk guarantees (issue #100): the heaviest of 24 buckets
        # holds at least the average 1/24 (0.041666…), and the walk does not
        # stop before VALUE_AREA_FRACTION. 0.04 is in (0, 1] and below the VA
        # share, so only the floor rejects it; 0.69 likewise.
        ({"poc_volume_share": 0.04}, "below 1 / bucket_count"),
        ({"value_area_volume_share": 0.69}, "below VALUE_AREA_FRACTION"),
        # The letter is re-derived from the three fractions (issue #100): the
        # default fractions (width 0.4, POC mid-range) are a D, so a profile
        # calling itself P — the real hand-built case that rendered "POC (6%
        # up the range) / Shape: P — volume built up in the upper part" — is
        # refused, naming the letter the numbers give.
        ({"shape": ProfileShape.P}, "contradicts the fractions.*give D"),
        ({"shape": "thin"}, "contradicts the fractions.*give D"),
        # The fractions must agree with the prices they claim to come from.
        # Both of these are individually in-bounds and pass every other guard;
        # only the cross-check catches them, and without it the renderer would
        # print "POC: 105.00 (90% up the range)" for a POC sitting mid-range.
        ({"poc_position": 0.9}, "contradicts the values"),
        ({"value_area_width_ratio": 0.9}, "contradicts the values"),
    ],
)
def test_volume_profile_rejects_self_contradictory_values(overrides, match):
    # The point of the guards: a profile whose bounds contradict each other would
    # render as a confident, nonsensical price level in the prompt.
    with pytest.raises(ValueError, match=match):
        VolumeProfile(**_profile(**overrides))


def _lettered(letter: ProfileShape) -> dict:
    """Self-consistent kwargs whose numbers really ARE ``letter``.

    Taken from the production classifier (``_shaped``) rather than written by
    hand: hand geometry is a second copy of the rule ladder that stops being
    its letter the first time a threshold moves.
    """
    return asdict(_shaped(letter))


def test_volume_profile_coerces_a_plain_shape_string():
    # A plain string is accepted and coerced — on numbers that really ARE that
    # letter, since the shape is re-derived at construction.
    thin, b = _lettered(ProfileShape.THIN), _lettered(ProfileShape.B)
    assert VolumeProfile(**{**thin, "shape": "thin"}).shape is ProfileShape.THIN
    assert VolumeProfile(**{**b, "shape": "b"}).shape is ProfileShape.B


def test_volume_profile_rejects_an_unknown_shape():
    with pytest.raises(ValueError, match="nonsense"):
        VolumeProfile(**_profile(shape="nonsense"))


@pytest.mark.parametrize("letter", list(ProfileShape))
def test_volume_profile_shape_is_rederived_with_the_producers_rule(letter):
    # Issue #100: ``shape`` is fully determined by three stored fractions, so
    # the DTO checks the letter it was handed against derive_profile_shape —
    # the SAME function classify_shape labels with. Each letter's own numbers
    # build with that letter and are refused with every other, which pins
    # that the check is the rule (not a per-letter special case) and names
    # the letter the numbers give.
    kwargs = _lettered(letter)
    assert (
        derive_profile_shape(
            kwargs["value_area_width_ratio"], kwargs["poc_position"], kwargs["close_position"]
        )
        is letter
    )
    assert VolumeProfile(**kwargs).shape is letter
    for other in ProfileShape:
        if other is letter:
            continue
        with pytest.raises(ValueError, match=f"contradicts the fractions.*give {letter.value}"):
            VolumeProfile(**{**kwargs, "shape": other})


def test_derive_profile_shape_checks_thin_before_the_poc_bands():
    # Rule order: a smeared profile is ``thin`` even when its POC and close
    # would otherwise say P (or b). Dropping the rule to last would flip this
    # to P and still pass a "some letter came back" check.
    assert derive_profile_shape(0.8, 0.7, 0.7) is ProfileShape.THIN
    assert derive_profile_shape(0.4, 0.7, 0.7) is ProfileShape.P
    # And a skewed POC whose close does not confirm it is a D, not the letter
    # the POC alone would give.
    assert derive_profile_shape(0.4, 0.95, 0.29) is ProfileShape.D
    assert derive_profile_shape(0.4, 0.05, 0.71) is ProfileShape.D


def test_perp_market_context_day_change_must_agree_with_its_prices():
    # Issue #100-1: the same contradiction VolumeProfile's cross-checks keep
    # out. A context claiming a 40% move over prices that say ~1.7% passed
    # every bounds check and would render "24h change: 40.00%".
    with pytest.raises(ValueError, match="day_change_pct \\(40.0\\) contradicts the prices"):
        PerpMarketContext(**_context(day_change_pct=40.0))
    # The shared 1e-6 tolerance, relative to the value: a change recorded to
    # six decimal places builds; one off by a hundredth of a point does not.
    exact = _context()["day_change_pct"]
    PerpMarketContext(**_context(day_change_pct=round(exact, 6)))
    with pytest.raises(ValueError, match="contradicts the prices"):
        PerpMarketContext(**_context(day_change_pct=exact + 0.01))


def test_perp_market_context_day_change_is_the_producers_own_rule():
    # derive_day_change_pct is what context_builder fills the field with, so
    # the DTO checking against it can never refuse the producer's own output —
    # including at the size an absolute tolerance would: a dust prevDayPx
    # under a real mark (the exchange reports either; MarketSnapshot admits
    # both) is a ratio of ~1e10, where one double ulp alone is > 1e-6.
    mark, dust = Decimal("123456.789"), Decimal("0.0000123")
    change = derive_day_change_pct(mark, dust)
    assert change is not None and change > 1e11
    ctx = PerpMarketContext(**_context(mark_price=mark, prev_day_price=dust, day_change_pct=change))
    assert ctx.day_change_pct == change
    # And a Decimal handed in (the natural type for anything *_pct here) is
    # coerced and checked, not crashed on: the contradiction message, not a
    # TypeError from Decimal - float.
    exact = _context()["day_change_pct"]
    assert PerpMarketContext(**_context(day_change_pct=Decimal(str(exact)))).day_change_pct == exact
    with pytest.raises(ValueError, match="contradicts the prices"):
        PerpMarketContext(**_context(day_change_pct=Decimal("40")))


def test_perp_market_context_day_change_is_none_exactly_when_there_is_no_reference():
    # derive_day_change_pct's rule, enforced on the DTO: a zero prev_day_price
    # (freshly listed coin — MarketSnapshot allows it) means no reference, so
    # the change MUST be None; a positive one means a reference exists, so the
    # change must be present. Both directions of the disagreement are refused.
    ctx = PerpMarketContext(**_context(prev_day_price=Decimal("0"), day_change_pct=None))
    assert ctx.day_change_pct is None
    with pytest.raises(ValueError, match="day_change_pct \\(0.0\\) disagrees with prev_day_price"):
        PerpMarketContext(**_context(prev_day_price=Decimal("0"), day_change_pct=0.0))
    with pytest.raises(ValueError, match="day_change_pct \\(None\\) disagrees with prev_day_price"):
        PerpMarketContext(**_context(day_change_pct=None))
    # An unchanged price is a change of 0, not an absent one.
    same = PerpMarketContext(**_context(prev_day_price=Decimal("60000"), day_change_pct=0.0))
    assert same.day_change_pct == 0.0


def test_perp_market_context_rejects_negative_prev_day_price():
    # Mirrors MarketSnapshot's >= 0 guard (zero is legal: "no reference yet").
    with pytest.raises(ValueError, match="prev_day_price must be >= 0"):
        PerpMarketContext(**_context(prev_day_price=Decimal("-1"), day_change_pct=None))


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
