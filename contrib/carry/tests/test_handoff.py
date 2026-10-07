"""The handoff document: building, sizing, the round trip, and what a reader refuses."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from contrib.carry.handoff import (
    HANDOFF_VERSION,
    Handoff,
    HandoffError,
    build,
    iso_utc,
    previous_handoff,
    read_handoff,
    spot_weight,
    write_handoff,
)
from contrib.carry.signal import OUT, Action, Params, Position, Reading, Side

from .conftest import MS_PER_HOUR, day

PARAMS = Params()
READING = Reading(
    at_ms=day(41) - MS_PER_HOUR,
    current=Decimal("0.00005"),
    z=1.8,
    samples=719,
    recent_mean=Decimal("0.000045"),
    recent_samples=24,
)


def _entered(**overrides) -> Handoff:
    kwargs: dict = {
        "coin": "ETH",
        "as_of_ms": day(41),
        "written_at_ms": day(41) - 10 * 60 * 1000,
        "action": Action.ENTER,
        "position": Position(Side.IN, day(41)),
        "params": PARAMS,
        "reading": READING,
        "equity_perp": Decimal("20000"),
        "equity_spot": Decimal("10000"),
    }
    kwargs.update(overrides)
    return build(**kwargs)


def test_build_while_in_sizes_the_spot_leg_to_the_perp_notional():
    handoff = _entered()
    assert handoff.perp_side == "short"
    assert handoff.margin_pct == 30
    assert handoff.spot_token == "WETH"
    # 30% of 20000 is 6000 of notional; over 10000 of spot equity that is 0.6.
    assert handoff.spot_weight == Decimal("0.6000")


def test_build_while_out_zeroes_both_legs():
    for action in (Action.EXIT, Action.STAY_OUT):
        handoff = _entered(action=action, position=OUT)
        assert handoff.perp_side == "flat"
        assert handoff.margin_pct == 0
        assert handoff.spot_weight == 0


def test_build_while_holding_keeps_the_entry_instant():
    handoff = _entered(action=Action.HOLD, position=Position(Side.IN, day(38)), as_of_ms=day(41))
    assert handoff.position.entered_at_ms == day(38)
    assert handoff.perp_side == "short"


@pytest.mark.parametrize(
    ("margin", "perp", "spot", "expected"),
    [
        (30, None, None, "0.3000"),
        (30, Decimal("20000"), None, "0.3000"),
        (30, None, Decimal("0"), "0.3000"),
        (30, Decimal("20000"), Decimal("10000"), "0.6000"),
        (30, Decimal("50000"), Decimal("10000"), "1.0000"),
        (30, Decimal("10000"), Decimal("30000"), "0.1000"),
        (33, Decimal("1"), Decimal("3"), "0.1100"),
        (30, Decimal("1"), Decimal("100000"), "0.0000"),
        (30, Decimal("0"), Decimal("10000"), "0.0000"),
        (30, Decimal("-250"), Decimal("10000"), "0.0000"),
        (30, Decimal("20000"), Decimal("0"), "0.0000"),
        (0, Decimal("20000"), Decimal("10000"), "0.0000"),
    ],
)
def test_spot_weight(margin, perp, spot, expected):
    assert spot_weight(margin, perp, spot) == Decimal(expected)


def test_a_dead_leg_still_gets_a_handoff_with_nothing_to_hedge():
    for perp, spot in ((Decimal("0"), Decimal("10000")), (Decimal("20000"), Decimal("-5"))):
        handoff = _entered(action=Action.HOLD, equity_perp=perp, equity_spot=spot)
        assert handoff.perp_side == "short" and handoff.spot_weight == 0
        assert (handoff.equity_perp, handoff.equity_spot) == (perp, spot)


def test_iso_utc_spells_the_boundary_to_the_second():
    assert iso_utc(day(41)) == "2026-02-11T00:00:00+00:00"


def test_the_document_round_trips_through_the_file(tmp_path: Path):
    handoff = _entered()
    path = tmp_path / "carry-eth.json"
    write_handoff(path, handoff)
    assert read_handoff(path) == handoff
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert doc["version"] == HANDOFF_VERSION
    assert doc["as_of"] == "2026-02-11T00:00:00+00:00"
    assert doc["perp"] == {"side": "short", "margin_pct": 30}
    assert doc["spot"] == {"token": "WETH", "weight": "0.6000"}
    assert doc["equity"] == {"perp": "20000", "spot": "10000"}
    assert doc["signal"]["funding_hourly"] == "0.00005"
    assert doc["signal"]["z"] == 1.8
    assert doc["position"] == {"side": "in", "entered_at_ms": day(41)}


def test_nulls_round_trip_too(tmp_path: Path):
    handoff = _entered(reading=None, equity_perp=None, equity_spot=None)
    path = tmp_path / "carry-eth.json"
    write_handoff(path, handoff)
    assert read_handoff(path) == handoff
    assert handoff.spot_weight == Decimal("0.3000")


# --- the memory -------------------------------------------------------------


def test_previous_handoff_is_none_without_a_file_and_the_file_with_one(tmp_path: Path):
    path = tmp_path / "carry-eth.json"
    assert previous_handoff(path, coin="ETH", as_of_ms=day(41)) is None
    write_handoff(path, _entered())
    assert previous_handoff(path, coin="ETH", as_of_ms=day(42)) == _entered()
    # The same boundary is the rerun the caller handles, so it is handed back too.
    assert previous_handoff(path, coin="ETH", as_of_ms=day(41)) == _entered()


def test_previous_handoff_refuses_another_coins_file_and_a_later_boundary(tmp_path: Path):
    path = tmp_path / "carry.json"
    write_handoff(path, _entered())
    with pytest.raises(HandoffError, match="is ETH's handoff, not BTC's"):
        previous_handoff(path, coin="BTC", as_of_ms=day(42))
    with pytest.raises(HandoffError, match="later than 2026-02-10T00:00:00"):
        previous_handoff(path, coin="ETH", as_of_ms=day(40))


def test_previous_handoff_refuses_a_file_it_cannot_read_rather_than_calling_it_none(
    tmp_path: Path,
):
    path = tmp_path / "carry-eth.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(HandoffError, match="not JSON"):
        previous_handoff(path, coin="ETH", as_of_ms=day(41))
    path.write_text(json.dumps({"version": 2}), encoding="utf-8")
    with pytest.raises(HandoffError, match="version 2"):
        previous_handoff(path, coin="ETH", as_of_ms=day(41))


# --- what a reader refuses --------------------------------------------------


def _document(**changes) -> dict:
    doc = _entered().to_document()
    for dotted, value in changes.items():
        head, _, tail = dotted.partition(".")
        if tail:
            doc[head][tail] = value
        else:
            doc[head] = value
    return doc


@pytest.mark.parametrize(
    ("changes", "fragment"),
    [
        ({"version": 2}, "version 2"),
        ({"version": True}, "version True"),
        ({"coin": "SOL"}, "coin must be one of"),
        ({"action": "buy"}, "unknown action"),
        ({"action": "stay_out"}, "contradicts position"),
        ({"position.side": "long"}, "unknown side"),
        ({"position.entered_at_ms": None}, "in with an entry instant"),
        ({"position.entered_at_ms": day(42)}, "is after as_of_ms"),
        ({"perp.side": "long"}, "perp.side 'long' contradicts"),
        ({"perp.margin_pct": 0}, "margin_pct while in must be between 1 and 100"),
        ({"perp.margin_pct": 101}, "margin_pct while in must be between 1 and 100"),
        ({"perp.margin_pct": -1}, "perp.margin_pct must be at least 0"),
        ({"spot.token": "WBTC"}, "is not ETH's WETH"),
        ({"spot.weight": "0.5"}, "disagrees with the equities"),
        ({"spot.weight": 0.6}, "decimal string"),
        ({"spot.weight": "NaN"}, "must be finite"),
        ({"signal.z": "high"}, "signal.z: expected a number"),
        ({"signal.funding_hourly": 5e-05}, "decimal string"),
        ({"equity.perp": "Infinity"}, "must be finite"),
        ({"as_of_ms": 0}, "as_of_ms must be at least 1"),
    ],
)
def test_a_reader_refuses_a_document_that_is_not_a_handoff(changes, fragment):
    with pytest.raises(HandoffError, match=fragment):
        Handoff.from_document(_document(**changes))


def test_a_reader_refuses_a_missing_field():
    doc = _entered().to_document()
    del doc["spot"]["weight"]
    with pytest.raises(HandoffError, match="spot is missing 'weight'"):
        Handoff.from_document(doc)
    with pytest.raises(HandoffError, match="must be an object"):
        Handoff.from_document([])


def test_an_out_document_must_carry_zero_targets():
    out = _entered(action=Action.EXIT, position=OUT).to_document()
    weighted = json.loads(json.dumps(out))
    weighted["spot"]["weight"] = "0.3"
    with pytest.raises(HandoffError, match="disagrees with the equities"):
        Handoff.from_document(weighted)
    margined = json.loads(json.dumps(out))
    margined["perp"]["margin_pct"] = 30
    with pytest.raises(HandoffError, match="margin_pct while out must be 0"):
        Handoff.from_document(margined)
    held = json.loads(json.dumps(out))
    held["action"] = "hold"
    with pytest.raises(HandoffError, match="action 'hold' contradicts position 'out'"):
        Handoff.from_document(held)
