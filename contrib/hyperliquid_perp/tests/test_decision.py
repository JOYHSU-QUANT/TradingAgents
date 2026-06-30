"""Tests for the PerpTradeDecision schema and its JSON serialization."""

from __future__ import annotations

from decimal import Decimal

import pytest

from contrib.hyperliquid_perp.domains.perp.decision import (
    EntryZone,
    FundingView,
    Intent,
    MarketRegime,
    PerpTradeDecision,
    Urgency,
)


def _decision(**overrides) -> PerpTradeDecision:
    base = {
        "intent": Intent.OPEN_LONG,
        "confidence": 0.8,
        "target_size_pct": 20.0,
        "entry_zone": EntryZone(low=Decimal("63000.0"), high=Decimal("63400.0")),
        "invalidation_price": Decimal("61800.0"),
        "urgency": Urgency.LOW,
        "rationale": "Uptrend intact.",
        "key_risks": ("Resistance overhead",),
        "market_regime": MarketRegime.TRENDING,
        "funding_view": FundingView.NEUTRAL,
    }
    base.update(overrides)
    return PerpTradeDecision(**base)


def test_entry_zone_rejects_inverted_range():
    # An inverted band (low > high) is a structurally invalid entry zone that downstream
    # order placement would read backwards; reject it at construction.
    with pytest.raises(ValueError, match="low.*<= high"):
        EntryZone(low=Decimal("63400.0"), high=Decimal("63000.0"))


@pytest.mark.parametrize("bad", [Decimal("0"), Decimal("-50")])
def test_entry_zone_rejects_nonpositive_low(bad):
    # Prices are strictly positive; a non-positive low (and the inverted negative-price
    # band it implies) is a corrupt parse order placement would read as a real entry.
    with pytest.raises(ValueError, match="low must be > 0"):
        EntryZone(low=bad, high=Decimal("63000.0"))


def test_decision_rejects_negative_target_size():
    # target_size_pct is an unsigned magnitude (direction lives in intent); a negative
    # value is a sign error that would mis-size the order downstream.
    with pytest.raises(ValueError, match="target_size_pct"):
        _decision(target_size_pct=-5.0)


@pytest.mark.parametrize("intent", [Intent.OPEN_LONG, Intent.OPEN_SHORT])
@pytest.mark.parametrize("bad_size", [0.0, None])
def test_decision_rejects_open_intent_without_positive_size(intent, bad_size):
    # Cross-field invariant: an OPEN intent must carry a positive size. An open with
    # target_size_pct of 0/None is a degenerate "open nothing" Phase 2 would have to
    # treat as a silent no-op; reject the nonsense decision at construction.
    with pytest.raises(ValueError, match="target_size_pct > 0"):
        _decision(intent=intent, target_size_pct=bad_size)


def test_decision_allows_hold_with_none_target():
    # A plain hold legitimately has target_size_pct=None — the OPEN guard must not fire.
    dec = _decision(intent=Intent.HOLD, target_size_pct=None, entry_zone=None)
    assert dec.intent is Intent.HOLD and dec.target_size_pct is None


def test_decision_allows_close_with_zero_target():
    # A close carries target_size_pct=0.0 (size down to flat) — not an OPEN, so allowed.
    dec = _decision(intent=Intent.CLOSE, target_size_pct=0.0, entry_zone=None)
    assert dec.intent is Intent.CLOSE and dec.target_size_pct == 0.0


def test_to_dict_flattens_enums_and_entry_zone():
    out = _decision().to_dict()
    assert out["intent"] == "open_long"
    assert out["urgency"] == "low"
    assert out["market_regime"] == "trending"
    assert out["funding_view"] == "neutral"
    # Prices serialize as strings to preserve full Decimal precision (a sub-cent coin
    # would lose digits through float()); downstream consumers parse them back.
    assert out["entry_zone"] == {"low": "63000.0", "high": "63400.0"}
    assert out["invalidation_price"] == "61800.0"
    assert out["key_risks"] == ["Resistance overhead"]  # tuple -> list


def test_to_dict_null_entry_zone_and_target():
    out = _decision(
        intent=Intent.HOLD,
        target_size_pct=None,
        entry_zone=None,
        invalidation_price=None,
    ).to_dict()
    assert out["entry_zone"] is None
    assert out["target_size_pct"] is None
    assert out["invalidation_price"] is None


def test_to_dict_is_json_serializable():
    import json

    # Round-trips cleanly — no Decimal/enum leaks that would break json.dump.
    text = json.dumps(_decision().to_dict())
    assert "open_long" in text


def test_to_dict_preserves_sub_cent_precision_as_string():
    # A sub-cent-priced coin would lose significant digits through float(); string
    # serialization round-trips to the exact same Decimal.
    tiny = Decimal("0.000012345678901234")
    out = _decision(
        entry_zone=EntryZone(low=tiny, high=tiny),
        invalidation_price=tiny,
    ).to_dict()
    assert out["entry_zone"]["low"] == str(tiny)
    assert Decimal(out["entry_zone"]["low"]) == tiny
    assert Decimal(out["invalidation_price"]) == tiny


@pytest.mark.parametrize("bad_confidence", [1.5, -0.1])
def test_confidence_out_of_range_raises(bad_confidence):
    # confidence is a 0-1 probability the RiskGate gates on; a misconfigured value
    # outside [0, 1] must fail at construction, not silently corrupt the gate.
    with pytest.raises(ValueError, match="confidence must be in"):
        _decision(confidence=bad_confidence)


def test_confidence_boundary_values_allowed():
    assert _decision(confidence=0.0).confidence == 0.0
    assert _decision(confidence=1.0).confidence == 1.0


def test_key_risks_list_is_coerced_to_tuple():
    # frozen=True blocks reassignment but not in-place mutation of a list value;
    # a caller passing a list must end up with an immutable tuple, honoring the
    # tuple[str, ...] annotation.
    d = _decision(key_risks=["a", "b"])
    assert d.key_risks == ("a", "b")
    assert isinstance(d.key_risks, tuple)
