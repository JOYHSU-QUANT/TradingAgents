"""Tests for the deterministic decision adapter (rating -> intent and fields).

These are pure-function tests: a synthetic PerpMarketContext + position drive the
rebalance-to-target logic with no engine run. The rating->intent table from
docs/INTEGRATION.md is the contract under test.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from contrib.hyperliquid_perp.domains.perp.decision import (
    FundingView,
    Intent,
    Urgency,
)
from contrib.hyperliquid_perp.domains.perp.schema import (
    AccountSnapshot,
    PerpMarketContext,
    PerpPosition,
)
from contrib.hyperliquid_perp.integration.decision_adapter import (
    AdapterConfig,
    DecisionAdapter,
    RatingSource,
    _bias_sign,
    _coerce_engine_text,
    _parse_price,
    confidence_for,
    current_exposure_pct,
    extract_fields,
    funding_view_for,
    rating_to_target,
    rebalance,
)

_ACCOUNT_VALUE = Decimal("1000")  # so a margin_used of 100 == 10% exposure


def _ctx(
    *, mark=60000.0, regime="trending", zscore=0.0, atr=300.0, interval="4h"
) -> PerpMarketContext:
    return PerpMarketContext(
        coin="BTC",
        as_of=datetime(2026, 6, 27, tzinfo=timezone.utc),
        candle_interval=interval,
        candle_count=200,
        mark_price=Decimal(str(mark)),
        oracle_price=Decimal(str(mark)),
        prev_day_price=Decimal(str(mark)),
        mid_price=None,
        day_change_pct=0.0,
        open_interest=Decimal("1000"),
        day_ntl_volume=Decimal("1000000"),
        funding_rate=Decimal("0.00001"),
        funding_premium=None,
        funding_zscore_30d=zscore,
        funding_window_days=30,
        funding_sample_count=200,
        indicators={"atr_14": atr, "ema_20": mark, "ema_50": mark},
        market_regime=regime,
    )


def _pos(*, pct, long=True, upnl="0") -> PerpPosition:
    """A position whose committed margin is ``pct`` % of _ACCOUNT_VALUE."""
    margin = _ACCOUNT_VALUE * Decimal(str(pct)) / 100
    size = margin / Decimal("60000")
    return PerpPosition(
        coin="BTC",
        size=size if long else -size,
        entry_price=Decimal("60000"),
        unrealized_pnl=Decimal(str(upnl)),
        margin_used=margin,
    )


def _adapter(position=None, *, ctx=None, adapter_config=None) -> DecisionAdapter:
    return DecisionAdapter(ctx or _ctx(), position, _ACCOUNT_VALUE, adapter_config)


# --------------------------------------------------------------------------
# rebalance() — the core decision table
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "current, target, expected_intent, expected_mag",
    [
        (0.0, 20.0, Intent.OPEN_LONG, 20.0),  # flat -> Buy
        (0.0, -20.0, Intent.OPEN_SHORT, 20.0),  # flat -> Sell (magnitude unsigned)
        (0.0, 0.0, Intent.HOLD, None),  # flat, target 0 -> hold
        (10.0, 20.0, Intent.OPEN_LONG, 20.0),  # long, size up toward target
        (10.0, 10.0, Intent.HOLD, None),  # long, within deadband
        (20.0, 10.0, Intent.REDUCE, 10.0),  # long, trim toward smaller target
        (20.0, 0.0, Intent.CLOSE, 0.0),  # long -> Underweight (flat)
        (10.0, -20.0, Intent.CLOSE, 0.0),  # long -> Sell, no_direct_flip closes first
        (-10.0, 20.0, Intent.CLOSE, 0.0),  # short -> Buy, no_direct_flip closes first
        # Short-side same-sign rows: these branches were previously only exercised
        # through build_decision(), so a regression in the negative-`t` path was
        # invisible to the unit table. The magnitude must stay unsigned (abs(t)).
        (-10.0, -20.0, Intent.OPEN_SHORT, 20.0),  # short, add toward a larger short
        (-10.0, -10.0, Intent.HOLD, None),  # short, within deadband
        (-20.0, -10.0, Intent.REDUCE, 10.0),  # short, trim toward -10 (abs, not -10)
    ],
)
def test_rebalance_table(current, target, expected_intent, expected_mag):
    intent, mag = rebalance(current, target, deadband=2.0, no_direct_flip=True)
    assert intent == expected_intent
    # The magnitude feeds downstream order sizing; assert it is the unsigned target,
    # never the signed `t` (a leaked sign would invert/negate the reduce-short path).
    assert mag == expected_mag


@pytest.mark.parametrize(
    "current, target, expected_intent",
    [
        (-10.0, -12.0, Intent.HOLD),  # short, diff == deadband exactly -> hold
        (-10.0, -12.01, Intent.OPEN_SHORT),  # short, just past the band -> add
        (-10.0, -8.0, Intent.HOLD),  # short, diff == deadband exactly on the trim side
        (-10.0, -7.99, Intent.REDUCE),  # short, just past the band -> trim
    ],
)
def test_rebalance_deadband_boundary_short_side(current, target, expected_intent):
    # The short side must mirror the long deadband exactly; an asymmetric > vs >=
    # would let shorts thrash where longs do not.
    intent, _mag = rebalance(current, target, deadband=2.0, no_direct_flip=True)
    assert intent == expected_intent


def test_rebalance_direct_flip_when_allowed():
    intent, mag = rebalance(10.0, -20.0, deadband=2.0, no_direct_flip=False)
    assert intent == Intent.OPEN_SHORT
    assert mag == 20.0


def test_rebalance_direct_flip_short_to_long_when_allowed():
    # The symmetric direction: a short flipping to long in one step must open long,
    # not mirror the wrong side. Only the long->short flip was previously asserted.
    intent, mag = rebalance(-10.0, 20.0, deadband=2.0, no_direct_flip=False)
    assert intent == Intent.OPEN_LONG
    assert mag == 20.0


@pytest.mark.parametrize(
    "current, target, expected_intent, expected_mag",
    [
        (10.0, 20.0, Intent.OPEN_LONG, 20.0),  # same-sign size-up
        (20.0, 10.0, Intent.REDUCE, 10.0),  # same-sign trim
    ],
)
def test_rebalance_same_sign_is_unaffected_by_no_direct_flip_flag(
    current, target, expected_intent, expected_mag
):
    # no_direct_flip only governs opposite-sign transitions (close-first vs flip);
    # a same-sign rebalance must behave identically whether the flag is on or off.
    off = rebalance(current, target, deadband=2.0, no_direct_flip=False)
    on = rebalance(current, target, deadband=2.0, no_direct_flip=True)
    assert off == on == (expected_intent, expected_mag)


def test_rebalance_close_target_is_zero_not_none():
    _intent, mag = rebalance(20.0, 0.0, deadband=2.0, no_direct_flip=True)
    assert mag == 0.0  # close => flat, an explicit 0 target


@pytest.mark.parametrize(
    "current, target, expected_intent",
    [
        (10.0, 12.0, Intent.HOLD),  # diff == deadband exactly -> hold (strict >)
        (10.0, 12.01, Intent.OPEN_LONG),  # just past the band -> add
        (10.0, 8.0, Intent.HOLD),  # diff == deadband exactly on the trim side
        (10.0, 7.99, Intent.REDUCE),  # just past the band -> trim
    ],
)
def test_rebalance_deadband_boundary(current, target, expected_intent):
    # The deadband is what stops thrashing; a > vs >= slip would flip these.
    intent, _mag = rebalance(current, target, deadband=2.0, no_direct_flip=True)
    assert intent == expected_intent


# --------------------------------------------------------------------------
# current_exposure_pct() — signed exposure (+long / -short)
# --------------------------------------------------------------------------


def test_current_exposure_long_is_positive():
    exposure = current_exposure_pct(_pos(pct=10, long=True), _ACCOUNT_VALUE)
    assert exposure == pytest.approx(10.0)


def test_current_exposure_short_is_negative():
    # A short must report negative exposure; an inverted sign would make the
    # rebalancer treat every short as a long.
    exposure = current_exposure_pct(_pos(pct=10, long=False), _ACCOUNT_VALUE)
    assert exposure == pytest.approx(-10.0)


def test_current_exposure_flat_or_unknown_is_zero():
    assert current_exposure_pct(None, _ACCOUNT_VALUE) == 0.0
    assert current_exposure_pct(_pos(pct=10), Decimal("0")) == 0.0


@pytest.mark.parametrize("margin_used", [None, Decimal("0")])
def test_current_exposure_unknown_margin_returns_none(margin_used, caplog):
    # Decision C: a sized position whose committed margin is missing or 0 has an
    # undeterminable exposure. A reported 0 would read as flat (-> pyramiding), so the
    # function returns None and the caller (build_decision) forces HOLD rather than
    # sizing against a guess.
    pos = PerpPosition(
        coin="BTC",
        size=Decimal("0.01"),
        entry_price=Decimal("60000"),
        unrealized_pnl=Decimal("0"),
        margin_used=margin_used,
    )
    with caplog.at_level(logging.WARNING):
        assert current_exposure_pct(pos, _ACCOUNT_VALUE) is None
    assert any("unreliable" in r.message for r in caplog.records)


def test_unknown_margin_forces_hold_through_build_decision(caplog):
    # End-to-end: a Buy on a sized position with no usable margin must not size up — it
    # is forced to HOLD with a reason recorded in key_risks for the audit trail.
    pos = PerpPosition(
        coin="BTC",
        size=Decimal("0.01"),
        entry_price=Decimal("60000"),
        unrealized_pnl=Decimal("0"),
        margin_used=None,
    )
    with caplog.at_level(logging.WARNING):
        decision = _adapter(pos).build_decision("Buy", {}, {})
    assert decision.intent == Intent.HOLD
    assert decision.target_size_pct is None
    assert any("forced to HOLD" in r for r in decision.key_risks)


# --------------------------------------------------------------------------
# rating_to_target() — tier-symmetric shorting (revised decision #11)
# --------------------------------------------------------------------------

_TARGETS = {"buy": 20.0, "overweight": 10.0, "underweight": -10.0, "sell": -20.0}


def test_underweight_is_mild_short_when_allowed():
    # Revised decision #11: Underweight mirrors Overweight on the short side.
    assert rating_to_target("Underweight", 10.0, _TARGETS, allow_short=True) == -10.0


def test_negative_tiers_de_risk_to_flat_when_shorting_off():
    # allow_short=False forces every negative tier (Sell and Underweight) to flat.
    assert rating_to_target("Underweight", 10.0, _TARGETS, allow_short=False) == 0.0
    assert rating_to_target("Sell", 10.0, _TARGETS, allow_short=False) == 0.0


def test_sell_is_full_short_when_allowed():
    assert rating_to_target("Sell", 10.0, _TARGETS, allow_short=True) == -20.0


def test_hold_keeps_current():
    assert rating_to_target("Hold", 7.5, _TARGETS, allow_short=True) == 7.5


def test_hold_keeps_existing_short_when_shorting_allowed():
    assert rating_to_target("Hold", -7.5, _TARGETS, allow_short=True) == -7.5


def test_hold_flattens_existing_short_when_shorting_off():
    # A short can be read in from the live wallet; Hold must not maintain it when
    # allow_short=False — it de-risks to flat.
    assert rating_to_target("Hold", -7.5, _TARGETS, allow_short=False) == 0.0


def test_hold_keeps_existing_long_when_shorting_off():
    # The guard only targets shorts; a long is untouched by allow_short.
    assert rating_to_target("Hold", 7.5, _TARGETS, allow_short=False) == 7.5


def test_unrecognized_rating_defaults_to_flat_and_warns(caplog):
    # An unknown tier de-risks to flat (0) — but that silently CLOSEs an open
    # position, so it must emit a WARNING rather than vanish.
    with caplog.at_level(logging.WARNING):
        assert rating_to_target("Gibberish", 12.0, _TARGETS, allow_short=True) == 0.0
    assert any("unrecognized rating" in r.message.lower() for r in caplog.records)


def test_explicit_zero_target_tier_does_not_warn(caplog):
    # A tier explicitly configured to 0.0 is a recognized target, not an unknown
    # rating — it must return 0 silently (no spurious warning).
    with caplog.at_level(logging.WARNING):
        assert rating_to_target("Hold2", 5.0, {"hold2": 0.0}, allow_short=True) == 0.0
    assert not any("unrecognized rating" in r.message.lower() for r in caplog.records)


# --------------------------------------------------------------------------
# Full rating -> PerpTradeDecision through build_decision()
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "rating, position, expected_intent",
    [
        ("Buy", None, Intent.OPEN_LONG),
        ("Sell", None, Intent.OPEN_SHORT),
        ("Overweight", None, Intent.OPEN_LONG),
        ("Underweight", None, Intent.OPEN_SHORT),  # flat + mild short -> open short
        ("Hold", None, Intent.HOLD),
        ("Buy", _pos(pct=10), Intent.OPEN_LONG),  # add toward 20
        ("Overweight", _pos(pct=10), Intent.HOLD),
        ("Underweight", _pos(pct=20), Intent.CLOSE),  # long -> mild short, no flip -> close
        ("Underweight", _pos(pct=15, long=False), Intent.REDUCE),  # short trimmed toward -10
        ("Sell", _pos(pct=10), Intent.CLOSE),  # long facing Sell, no flip
    ],
)
def test_rating_to_intent_with_position(rating, position, expected_intent):
    decision = _adapter(position).build_decision(rating, {}, {})
    assert decision.intent == expected_intent


def test_adapter_config_allow_short_false_blocks_sell_short():
    # The conservative live guard: allow_short=False must force a Sell from flat to
    # HOLD (target pinned to 0), never OPEN_SHORT.
    decision = _adapter(None, adapter_config={"allow_short": False}).build_decision("Sell", {}, {})
    assert decision.intent == Intent.HOLD


def test_adapter_config_target_size_override_applies():
    # A configured per-tier target overrides the default (buy 20 -> 5).
    adapter = _adapter(None, adapter_config={"target_size_pct": {"buy": 5.0}})
    decision = adapter.build_decision("Buy", {}, {})
    assert decision.intent == Intent.OPEN_LONG
    assert decision.target_size_pct == 5.0


def test_adapter_rejects_unknown_target_tier_key():
    # A typo'd tier would silently default that tier and drop the override -> raise.
    with pytest.raises(ValueError, match="buuy"):
        _adapter(None, adapter_config={"target_size_pct": {"buuy": 5.0}})


def test_adapter_rejects_unknown_confidence_tier_key():
    with pytest.raises(ValueError, match="huge"):
        _adapter(None, adapter_config={"confidence": {"huge": 0.99}})


def test_adapter_rejects_non_numeric_target_value():
    # A non-numeric override (e.g. a YAML string/null) must be rejected at the
    # config seam, not blow up later in arithmetic with a confusing traceback.
    with pytest.raises(ValueError, match="non-numeric"):
        _adapter(None, adapter_config={"target_size_pct": {"buy": "twenty"}})


def test_adapter_rejects_bool_tier_value():
    # bool is an int subclass but is never a valid tier value.
    with pytest.raises(ValueError, match="non-numeric"):
        _adapter(None, adapter_config={"confidence": {"full": True}})


def test_adapter_config_direct_construction_rejects_out_of_range_confidence():
    # The range check lives in __post_init__ (not only from_dict), so a *direct*
    # AdapterConfig(...) call — bypassing from_dict — can't hold an out-of-range tier
    # that would silently corrupt the RiskGate later.
    with pytest.raises(ValueError, match="out of range"):
        AdapterConfig(confidence={"full": 1.5, "partial": 0.6, "hold": 0.4})


def test_adapter_rejects_out_of_range_confidence():
    # confidence is a 0-1 probability the RiskGate gates on; a tier outside [0, 1]
    # must be rejected at the config seam, not silently corrupt that gate later.
    with pytest.raises(ValueError, match="out of range"):
        _adapter(None, adapter_config={"confidence": {"full": 1.5}})


def test_adapter_rejects_target_tier_over_100pct():
    # Decision F: a tier magnitude > 100% of net value is unreachable here (leverage is
    # the order planner's concern) and almost always a fat-fingered config; reject it at
    # the seam rather than instruct Phase 2 to size a multiple of net value.
    with pytest.raises(ValueError, match="exceed"):
        _adapter(None, adapter_config={"target_size_pct": {"buy": 500.0}})


def test_adapter_rejects_negative_target_tier_over_100pct():
    # The bound is on the magnitude, so a deep negative short tier is rejected too.
    with pytest.raises(ValueError, match="exceed"):
        _adapter(None, adapter_config={"target_size_pct": {"sell": -150.0}})


def test_build_decision_degraded_rating_source_forces_hold(caplog):
    # Decision E: build_decision is public; a non-EXPLICIT rating_source is a degraded
    # parse (engine failure), so it must force HOLD regardless of the rating word — a
    # direct caller (Phase 2/tests) without main.py's own rating_source gate must not act
    # on it. The hold-tier confidence makes the forced hold legible, not a confident Buy.
    with caplog.at_level(logging.WARNING):
        decision = _adapter(None).build_decision(
            "Buy", {}, {}, rating_source=RatingSource.PARSE_FALLBACK
        )
    assert decision.intent == Intent.HOLD
    assert decision.confidence == 0.4  # hold tier, not the Buy/full tier
    assert any("degraded" in r.message.lower() for r in caplog.records)


def test_build_decision_default_rating_source_is_explicit():
    # The default rating_source is EXPLICIT, so existing callers (and a clean rating) act
    # normally — a Buy from flat opens long.
    decision = _adapter(None).build_decision("Buy", {}, {})
    assert decision.intent == Intent.OPEN_LONG


def test_adapter_rejects_negative_deadband():
    # A negative deadband makes rebalance() treat every position as outside the band
    # -> perpetual OPEN; reject it at the config seam rather than mis-rebalance later.
    with pytest.raises(ValueError, match="rebalance_deadband_pct"):
        _adapter(None, adapter_config={"rebalance_deadband_pct": -1.0})


def test_adapter_rejects_negative_entry_band():
    # A negative entry band yields EntryZone(low > high), which only fails deep in a
    # decision; reject it up front at construction.
    with pytest.raises(ValueError, match="entry_band_pct"):
        _adapter(None, adapter_config={"entry_band_pct": -0.5})


def test_adapter_blank_numeric_config_keys_fall_back_to_defaults():
    # A YAML key left blank parses to None; ``float(None)`` would crash. A present-but-
    # null deadband/entry_band must fall back to the default, not raise.
    adapter = _adapter(
        None, adapter_config={"rebalance_deadband_pct": None, "entry_band_pct": None}
    )
    assert adapter.config.rebalance_deadband_pct > 0
    assert adapter.config.entry_band_pct > 0


def test_perp_position_rejects_zero_size():
    # A flat account is None, never a zero-size PerpPosition (is_long/is_short would
    # both be False — a third state the type forbids). Reject it at construction.
    with pytest.raises(ValueError, match="non-zero"):
        PerpPosition(
            coin="BTC",
            size=Decimal("0"),
            entry_price=Decimal("63000"),
            unrealized_pnl=Decimal("0"),
        )


def test_account_snapshot_rejects_duplicate_coin():
    # position_for returns the first match, so a duplicate coin would silently drop the
    # second position. The exchange never reports two positions for one coin; reject it
    # at construction rather than let the ambiguity become a wrong exposure downstream.
    pos_a = PerpPosition(
        coin="BTC", size=Decimal("0.1"), entry_price=Decimal("60000"), unrealized_pnl=Decimal("0")
    )
    pos_b = PerpPosition(
        coin="BTC", size=Decimal("-0.2"), entry_price=Decimal("61000"), unrealized_pnl=Decimal("0")
    )
    with pytest.raises(ValueError, match="duplicate coin"):
        AccountSnapshot(
            account_value=Decimal("1000"),
            withdrawable=Decimal("500"),
            total_margin_used=Decimal("500"),
            positions=(pos_a, pos_b),
        )


def test_perp_market_context_rejects_unknown_candle_interval():
    # candle_interval is validated against CandleInterval at construction; an
    # unsupported value (e.g. "9h") must raise here rather than later in interval_to_ms.
    with pytest.raises(ValueError, match="candle_interval"):
        _ctx(interval="9h")


def test_perp_market_context_accepts_string_interval_unchanged():
    # The field stays a plain, render-safe str (a str-enum member would render as
    # "CandleInterval.H4" in a prompt under 3.12). A valid string is kept verbatim.
    ctx = _ctx(interval="1h")
    assert ctx.candle_interval == "1h"
    assert type(ctx.candle_interval) is str


def test_adapter_blank_bool_config_keys_fall_back_to_true():
    # A YAML key present-but-blank parses to None; ``bool(None)`` is False, which would
    # silently invert these safety defaults (disable shorts / enable direct flips). A
    # blank key must fall back to the True default, exactly like an absent key.
    adapter = _adapter(None, adapter_config={"allow_short": None, "no_direct_flip": None})
    assert adapter.config.allow_short is True
    assert adapter.config.no_direct_flip is True


def test_confidence_for_unrecognized_rating_warns(caplog):
    conf = {"full": 0.8, "partial": 0.6, "hold": 0.4}
    with caplog.at_level("WARNING"):
        assert confidence_for("Gibberish", conf) == 0.4  # falls back to the hold tier
    assert any("unrecognized rating" in r.message for r in caplog.records)


def test_confidence_for_hold_does_not_warn(caplog):
    # ``Hold`` legitimately resolves to the hold tier; it must not log a false warning.
    conf = {"full": 0.8, "partial": 0.6, "hold": 0.4}
    with caplog.at_level("WARNING"):
        assert confidence_for("Hold", conf) == 0.4
    assert not caplog.records


def test_buy_from_flat_full_decision():
    fields = {"executive summary": "Breakout with volume; structure bullish."}
    trader = {"entry price": "63000.0", "stop loss": "61800.0"}
    decision = _adapter(None).build_decision("Buy", fields, trader)

    assert decision.intent == Intent.OPEN_LONG
    assert decision.target_size_pct == 20.0
    assert decision.confidence == 0.8
    assert decision.rationale.startswith("Breakout")
    # entry_zone is the trader entry +/- 0.5% (non-volatile regime).
    assert decision.entry_zone is not None
    assert float(decision.entry_zone.low) == pytest.approx(63000.0 * 0.995)
    assert float(decision.entry_zone.high) == pytest.approx(63000.0 * 1.005)
    assert decision.invalidation_price == Decimal("61800.0")  # trader stop wins
    # Decimal-pure end-to-end: == would pass even for a float (61800.0 == Decimal),
    # so pin the type to catch a float leak through _parse_price -> build_decision.
    assert isinstance(decision.invalidation_price, Decimal)
    assert isinstance(decision.entry_zone.low, Decimal)
    assert decision.key_risks  # never empty


def test_volatile_regime_forces_high_urgency_and_no_zone():
    ctx = _ctx(regime="volatile", atr=3000.0)
    trader = {"entry price": "63000.0"}
    decision = DecisionAdapter(ctx, None, _ACCOUNT_VALUE).build_decision("Buy", {}, trader)
    assert decision.urgency == Urgency.HIGH
    assert decision.entry_zone is None  # urgent -> take market, no limit band


def test_invalidation_falls_back_to_atr_when_no_stop():
    ctx = _ctx(mark=60000.0, atr=500.0)
    decision = DecisionAdapter(ctx, None, _ACCOUNT_VALUE).build_decision("Buy", {}, {})
    # open_long, no trader stop -> mark - 2*ATR.
    assert float(decision.invalidation_price) == pytest.approx(60000.0 - 2 * 500.0)


def test_invalidation_atr_long_stop_non_positive_falls_back_to_none(caplog):
    # ATR >= mark/2 drives the long ATR stop (mark - 2*ATR) to <= 0; a non-positive
    # stop must be dropped (None), never serialized into the decision / audit log.
    ctx = _ctx(mark=100.0, atr=60.0)  # mark - 2*atr = -20
    with caplog.at_level("WARNING"):
        decision = DecisionAdapter(ctx, None, _ACCOUNT_VALUE).build_decision("Buy", {}, {})
    assert decision.invalidation_price is None
    assert any("non-positive" in r.message for r in caplog.records)


def test_invalidation_atr_fallback_is_above_mark_for_short():
    # open_short with no trader stop -> mark + 2*ATR (a sign slip would put the
    # stop below entry, i.e. on the wrong side of the trade).
    ctx = _ctx(mark=60000.0, atr=500.0)
    decision = DecisionAdapter(ctx, None, _ACCOUNT_VALUE).build_decision("Sell", {}, {})
    assert decision.intent == Intent.OPEN_SHORT
    assert float(decision.invalidation_price) == pytest.approx(60000.0 + 2 * 500.0)


@pytest.mark.parametrize(
    "text, expected",
    [
        ("63,000.0", Decimal("63000.0")),
        ("$63,000", Decimal("63000")),
        ("~63000", Decimal("63000")),
        ("$ 63 000.5", Decimal("63000.5")),
        ("63000-63500", None),  # a range stays unparseable -> ATR fallback
        ("-63000", None),  # a sign-flipped price is not a real price -> absent
        ("0", None),  # zero is never a real entry/stop -> absent
        ("$0.00", None),  # zero after stripping currency -> absent
        ("Infinity", None),  # non-finite is dropped, never serialized as a price
        ("NaN", None),  # NaN is dropped rather than propagated into the decision
        ("63000 USDT", Decimal("63000")),  # trailing quote-currency ticker stripped
        ("63000usdc", Decimal("63000")),  # ticker is case-insensitive, no space
        ("$63,000.5 USD", Decimal("63000.5")),  # currency suffix on top of $/comma
        ("USD 63000", Decimal("63000")),  # leading ticker (prefix) stripped, not just suffix
        ("USD63,000.5", Decimal("63000.5")),  # leading ticker with no space
        ("", None),
        (None, None),
    ],
)
def test_parse_price_strips_currency_and_tilde(text, expected):
    result = _parse_price(text)
    assert result == expected
    # Prices are Decimal-pure in-memory; a float leak here would reintroduce the
    # binary-rounding the Decimal migration removed.
    if expected is not None:
        assert isinstance(result, Decimal)


def test_parse_price_misreads_european_decimal_by_design():
    # Documented English-locale-only behaviour (see _parse_price docstring): a comma is
    # always a thousands separator, so European "63.000,5" (== 63000.5) loses the comma
    # and the period is read as the decimal point -> 63.0005. Pin this so a future
    # "fix" that adds locale heuristics can't silently change the downstream contract.
    # (With a reference mark supplied the gross result is caught — see below.)
    assert _parse_price("63.000,5") == Decimal("63.0005")


def test_parse_price_reference_bound_rejects_gross_misparse():
    # The locale mis-parse above (63.000,5 -> 63.0005) is ~1000x below a 63000 mark;
    # with the mark supplied as reference it is rejected (falls back to the ATR
    # heuristic) rather than handed to Phase 2 as a stop near zero.
    assert _parse_price("63.000,5", reference_price=Decimal("63000")) is None


def test_parse_price_reference_bound_keeps_plausible_price():
    # A price within tolerance of the mark is kept unchanged (and Decimal-pure).
    result = _parse_price("$62,000", reference_price=Decimal("63000"))
    assert result == Decimal("62000") and isinstance(result, Decimal)


@pytest.mark.parametrize("text", ["31000", "95000"])  # ~51% below / above a 63000 mark
def test_parse_price_reference_bound_rejects_beyond_tolerance(text):
    assert _parse_price(text, reference_price=Decimal("63000")) is None


def test_parse_price_warning_names_the_field(caplog):
    # When both entry and stop are parsed in the same round, the warning must name which
    # field was dropped so the rejection is attributable in the log stream.
    import logging

    caplog.set_level(logging.WARNING)
    assert _parse_price("63.000,5", reference_price=Decimal("63000"), field="stop loss") is None
    assert "stop loss" in caplog.text


def test_entry_zone_degenerate_band_falls_back_to_market():
    # entry_band_pct >= 100 drives the zone low to <= 0. _entry_zone must fall back to a
    # market entry (entry_zone is None) rather than let the EntryZone.low > 0 invariant
    # raise uncaught out of build_decision (which would exit 2 with no audit log).
    ctx = _ctx()  # trending regime -> low urgency, so an entry zone is attempted
    adapter = DecisionAdapter(ctx, None, _ACCOUNT_VALUE, {"entry_band_pct": 150.0})
    decision = adapter.build_decision("Buy", {"entry price": "60000"}, {})
    assert decision.intent == Intent.OPEN_LONG  # decision still produced
    assert decision.entry_zone is None  # degenerate band -> market entry, no crash


# --------------------------------------------------------------------------
# confidence_for() and funding_view_for()
# --------------------------------------------------------------------------


def test_confidence_tiers():
    conf = {"full": 0.8, "partial": 0.6, "hold": 0.4}
    assert confidence_for("Buy", conf) == 0.8
    assert confidence_for("Sell", conf) == 0.8
    assert confidence_for("Overweight", conf) == 0.6
    assert confidence_for("Underweight", conf) == 0.6
    assert confidence_for("Hold", conf) == 0.4


@pytest.mark.parametrize(
    "zscore, bias, expected",
    [
        (1.42, 1, FundingView.HEADWIND),  # crowded long pays
        (0.40, 1, FundingView.NEUTRAL),  # inside the neutral band
        (0.50, 1, FundingView.HEADWIND),  # exactly on the band -> not neutral
        (0.50, -1, FundingView.FAVORABLE),  # exactly on the band, other side
        (-0.50, 1, FundingView.FAVORABLE),  # symmetric band: -0.5 long is favorable, not neutral
        (-0.50, -1, FundingView.HEADWIND),  # symmetric band: -0.5 short pays
        (-1.0, 1, FundingView.FAVORABLE),  # negative funding helps the long
        (1.0, -1, FundingView.FAVORABLE),  # short receives positive funding
        (None, 1, FundingView.NEUTRAL),  # no data
        (2.0, 0, FundingView.NEUTRAL),  # no directional bias
    ],
)
def test_funding_view(zscore, bias, expected):
    assert funding_view_for(zscore, bias) == expected


# --------------------------------------------------------------------------
# _coerce_engine_text()
# --------------------------------------------------------------------------


def test_coerce_engine_text_passes_through_str():
    assert _coerce_engine_text("Buy. Strong.", field="final_trade_decision") == "Buy. Strong."


def test_coerce_engine_text_none_is_empty():
    assert _coerce_engine_text(None, field="final_trade_decision") == ""


def test_coerce_engine_text_non_str_warns_and_empties(caplog):
    # A non-string (schema drift / upstream bug) must not be silently coerced to a
    # blank Hold — it should warn and surface as empty.
    with caplog.at_level(logging.WARNING):
        result = _coerce_engine_text(["unexpected", "list"], field="final_trade_decision")
    assert result == ""
    assert any("expected str" in r.message for r in caplog.records)


def test_long_with_crowded_funding_is_headwind_end_to_end():
    ctx = _ctx(zscore=1.42)
    decision = DecisionAdapter(ctx, None, _ACCOUNT_VALUE).build_decision("Buy", {}, {})
    assert decision.funding_view == FundingView.HEADWIND
    # The headwind risk string interpolates the signed z-score; a format bug here
    # would surface only in the audit log, so assert it explicitly.
    assert any("+1.42" in r for r in decision.key_risks)


def test_short_with_crowded_funding_is_headwind_end_to_end():
    # Mirror of the long case for the short side: a short pays when funding is negative,
    # so a negative z-score is a headwind for a held short. The risk string must
    # interpolate the *signed* (negative) z-score — a sign/format bug between the two
    # sides would otherwise only surface in the audit log.
    ctx = _ctx(zscore=-1.42)
    short = _pos(pct=10, long=False)
    decision = DecisionAdapter(ctx, short, _ACCOUNT_VALUE).build_decision("Hold", {}, {})
    assert decision.funding_view == FundingView.HEADWIND
    assert any("-1.42" in r for r in decision.key_risks)


def test_bias_sign_close_with_no_position_uses_target_sign():
    # A CLOSE/REDUCE with no position object (degenerate: a flat account pinned to a
    # signed target) has no live side to read, so the funding bias — and therefore the
    # audit log's funding_view — must come from the target's sign rather than default
    # to neutral and mislabel the exit.
    assert _bias_sign(Intent.CLOSE, None, -20.0) == -1
    assert _bias_sign(Intent.CLOSE, None, 20.0) == 1
    assert _bias_sign(Intent.CLOSE, None, 0.0) == 0


def test_underwater_risk_omitted_when_closing_that_position():
    # Underweight on a 20% long -> CLOSE. The losing position is being exited,
    # so it must not be listed as a forward-looking risk.
    losing_long = _pos(pct=20, upnl="-500")
    decision = _adapter(losing_long).build_decision("Underweight", {}, {})
    assert decision.intent == Intent.CLOSE
    assert not any("underwater" in r for r in decision.key_risks)
    assert decision.key_risks  # still never empty (falls back to standard risk)


def test_underwater_risk_kept_when_holding_that_position():
    # Overweight on a 10% long sits within the deadband -> HOLD; the underwater
    # position is being kept, so the risk should be reported.
    losing_long = _pos(pct=10, upnl="-500")
    decision = _adapter(losing_long).build_decision("Overweight", {}, {})
    assert decision.intent == Intent.HOLD
    assert any("underwater" in r for r in decision.key_risks)


def test_underwater_short_risk_kept_when_holding_that_position():
    # Hold on a 10% short -> HOLD (allow_short default True); the underwater short
    # is being kept, so the risk string must name the *short* side, not "long".
    losing_short = _pos(pct=10, long=False, upnl="-500")
    decision = _adapter(losing_short).build_decision("Hold", {}, {})
    assert decision.intent == Intent.HOLD
    assert any("short is underwater" in r for r in decision.key_risks)


def test_short_reduce_target_size_pct_is_unsigned_end_to_end():
    # Underweight on a 15% short trims toward -10 -> REDUCE. The decision's
    # target_size_pct must be the unsigned magnitude (10.0), never -10.0: a leaked
    # sign here would feed a negative size to the downstream order planner.
    decision = _adapter(_pos(pct=15, long=False)).build_decision("Underweight", {}, {})
    assert decision.intent == Intent.REDUCE
    assert decision.target_size_pct == 10.0


def test_to_perp_decision_warns_on_unparseable_engine_output(caplog):
    # A direct caller (not run_engine) must still be signalled when the engine
    # output carries no recognized rating: the adapter defaults to Hold but warns,
    # so the degraded decision can't masquerade as a deliberate hold.
    final_state = {
        "final_trade_decision": "I cannot help with that.",
        "trader_investment_plan": "",
    }
    with caplog.at_level(logging.WARNING):
        decision = _adapter(None).to_perp_decision(final_state)
    assert decision.intent == Intent.HOLD
    assert any("degraded" in r.message.lower() for r in caplog.records)


def test_to_perp_decision_warns_on_empty_trader_plan(caplog):
    # A blank trader_investment_plan means a crashed/empty upstream trader agent:
    # entry price and stop loss are lost. That silent degradation must warn so a
    # downstream consumer doesn't treat the ATR-fallback open as fully specified.
    final_state = {"final_trade_decision": "**Rating**: Buy", "trader_investment_plan": "  "}
    with caplog.at_level(logging.WARNING):
        _adapter(None).to_perp_decision(final_state)
    assert any("trader_investment_plan" in r.message for r in caplog.records)


def test_to_perp_decision_with_trader_plan_does_not_warn_about_it(caplog):
    final_state = {
        "final_trade_decision": "**Rating**: Buy",
        "trader_investment_plan": "**Entry Price**: 63000\n**Stop Loss**: 61800",
    }
    with caplog.at_level(logging.WARNING):
        _adapter(None).to_perp_decision(final_state)
    assert not any("trader_investment_plan" in r.message for r in caplog.records)


def test_short_reduce_funding_view_is_short_side_end_to_end():
    # Underweight on a 15% short -> REDUCE. The funding view must be computed from
    # the *short* side (bias -1): a crowded-long z-score is FAVORABLE for a short.
    # A bias-sign slip here would invert the funding judgement in the audit log.
    decision = _adapter(_pos(pct=15, long=False), ctx=_ctx(zscore=1.5)).build_decision(
        "Underweight", {}, {}
    )
    assert decision.intent == Intent.REDUCE
    assert decision.funding_view == FundingView.FAVORABLE


def test_to_perp_decision_explicit_rating_does_not_warn(caplog):
    # A clean, recognized rating must not trip the degraded-output warning.
    final_state = {"final_trade_decision": "**Rating**: Buy", "trader_investment_plan": ""}
    with caplog.at_level(logging.WARNING):
        decision = _adapter(None).to_perp_decision(final_state)
    assert decision.intent == Intent.OPEN_LONG
    assert not any("degraded" in r.message.lower() for r in caplog.records)


@pytest.mark.parametrize(
    "decision_md, expected_rating, expected_source",
    [
        ("**Rating**: Buy", "Buy", "explicit"),  # recognized tier
        ("I cannot help with that.", "Hold", "parse_fallback"),  # non-empty, no tier
        ("", "Hold", "default"),  # empty output
    ],
)
def test_decide_reports_rating_source(decision_md, expected_rating, expected_source):
    # decide() is the seam main.py uses to stamp the audit log; the three
    # rating_source values must be distinguishable so a parse failure ("default"/
    # "parse_fallback") is never recorded as a deliberate hold ("explicit").
    final_state = {"final_trade_decision": decision_md, "trader_investment_plan": ""}
    _decision, rating, rating_source = _adapter(None).decide(final_state)
    assert rating == expected_rating
    assert rating_source == expected_source
    # The source is the typed RatingSource enum (the single source of truth), even
    # though it compares/serialises equal to its plain-string value.
    assert isinstance(rating_source, RatingSource)


@pytest.mark.parametrize("bad_state", [None, [], "final_trade_decision", 42])
def test_decide_rejects_non_dict_final_state(bad_state):
    # final_state comes from the external engine; a None/non-dict (schema drift or an
    # agent crash) must fail loud at this seam with a clear domain error rather than a
    # bare AttributeError on .get() with no audit context.
    with pytest.raises(ValueError, match="final_state must be a dict"):
        _adapter(None).decide(bad_state)


def test_adapter_config_tier_maps_are_immutable():
    # frozen=True blocks reassignment but not in-place dict mutation; the tier maps are
    # wrapped in a read-only proxy so a stray ``config.target_size_pct["buy"] = 0.0``
    # cannot silently corrupt sizing for the rest of the run.
    config = _adapter(None).config
    with pytest.raises(TypeError):
        config.target_size_pct["buy"] = 0.0
    with pytest.raises(TypeError):
        config.confidence["full"] = 0.0


def test_volatile_regime_risk_string_present():
    # A Buy in a volatile regime must surface the volatile-regime risk to the
    # downstream Risk Manager; a silently-missing string hides the slippage warning.
    ctx = _ctx(regime="volatile", atr=3000.0)
    decision = DecisionAdapter(ctx, None, _ACCOUNT_VALUE).build_decision("Buy", {}, {})
    assert any("Volatile regime" in r for r in decision.key_risks)


# --------------------------------------------------------------------------
# extract_fields() — parsing the engine's rendered markdown
# --------------------------------------------------------------------------


def test_extract_fields_multiline():
    md = (
        "**Rating**: Buy\n\n"
        "**Executive Summary**: Enter on a pullback.\nScale in over two days.\n\n"
        "**Investment Thesis**: Momentum confirmed."
    )
    fields = extract_fields(md)
    assert fields["rating"] == "Buy"
    assert "Scale in over two days." in fields["executive summary"]
    assert fields["investment thesis"] == "Momentum confirmed."


def test_extract_fields_no_markers_returns_empty():
    # Prose with no ``**Field**:`` markers must return {} so build_decision falls
    # back to its "No rationale provided" sentinel. A regex that matched arbitrary
    # lines would bypass that safety net.
    assert extract_fields("Momentum is strong and volume is rising.") == {}
    assert extract_fields("") == {}


def test_extract_fields_duplicate_key_last_wins():
    # A repeated field name overwrites; the last value is the one used downstream,
    # so the audit rationale matches the final marker, not the first.
    fields = extract_fields("**Rating**: Buy\n**Rating**: Sell")
    assert fields["rating"] == "Sell"


def test_sell_from_flat_produces_entry_zone_on_short_side():
    # OPEN_SHORT shares the long branch's entry-band math but had no direct
    # coverage; a guard that skipped short entry zones would otherwise go unnoticed.
    trader = {"entry price": "63000.0"}
    decision = _adapter(None).build_decision("Sell", {}, trader)
    assert decision.intent == Intent.OPEN_SHORT
    assert decision.entry_zone is not None
    assert float(decision.entry_zone.low) == pytest.approx(63000.0 * 0.995)
    assert float(decision.entry_zone.high) == pytest.approx(63000.0 * 1.005)
    assert isinstance(decision.entry_zone.low, Decimal)


def test_short_atr_stop_dropped_when_atr_zero():
    # Mirror of the long-side guard: atr == 0 would place the short stop exactly at
    # mark (immediate stop-out), so the ATR fallback must yield no stop.
    ctx = _ctx(mark=60000.0, atr=0.0)
    decision = DecisionAdapter(ctx, None, _ACCOUNT_VALUE).build_decision("Sell", {}, {})
    assert decision.intent == Intent.OPEN_SHORT
    assert decision.invalidation_price is None
