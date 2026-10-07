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
    plain,
    previous_handoff,
    read_handoff,
    spot_weight,
    write_handoff,
)
from contrib.carry.signal import OUT, Action, Params, Position, Reading, Side
from contrib.carry.stores import Equity

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
PERP = Equity(Decimal("20000"), day(40) + 20 * MS_PER_HOUR)
SPOT = Equity(Decimal("10000"), day(40))


def _entered(**overrides) -> Handoff:
    kwargs: dict = {
        "coin": "ETH",
        "as_of_ms": day(41),
        "written_at_ms": day(41) - 10 * 60 * 1000,
        "action": Action.ENTER,
        "position": Position(Side.IN, day(41)),
        "params": PARAMS,
        "reading": READING,
        "equity_perp": PERP,
        "equity_spot": SPOT,
    }
    kwargs.update(overrides)
    return build(**kwargs)


def _held(**overrides) -> Handoff:
    return _entered(**{"action": Action.HOLD, "position": Position(Side.IN, day(38)), **overrides})


def _out(**overrides) -> Handoff:
    return _entered(**{"action": Action.EXIT, "position": OUT, **overrides})


# --- building ---------------------------------------------------------------


def test_build_while_in_sizes_the_spot_leg_to_the_perp_notional():
    handoff = _entered()
    assert handoff.perp_side == "short"
    assert handoff.margin_pct == 30
    assert handoff.spot_token == "WETH"
    # 30% of 20000 is 6000 of notional; over 10000 of spot equity that is 0.6.
    assert handoff.spot_weight == Decimal("0.6000")


def test_build_while_out_zeroes_both_legs():
    for action in (Action.EXIT, Action.STAY_OUT):
        handoff = _out(action=action)
        assert handoff.perp_side == "flat"
        assert handoff.margin_pct == 0
        assert handoff.spot_weight == 0


def test_build_while_holding_keeps_the_entry_instant():
    handoff = _held()
    assert handoff.position.entered_at_ms == day(38)
    assert handoff.perp_side == "short"


def test_the_action_is_tied_to_the_entry_instant_and_the_reading():
    with pytest.raises(HandoffError, match="an entry is at the boundary"):
        _entered(position=Position(Side.IN, day(38)))
    with pytest.raises(HandoffError, match="a hold was entered before the boundary"):
        _held(position=Position(Side.IN, day(41)))
    with pytest.raises(HandoffError, match="an entry carries the reading"):
        _entered(reading=None)
    assert _held(reading=None).reading is None


def test_a_reading_is_from_before_the_boundary():
    late = Reading(at_ms=day(41), current=Decimal(1), z=None, samples=0,
                   recent_mean=None, recent_samples=0)  # fmt: skip
    with pytest.raises(HandoffError, match="is not before as_of_ms"):
        _held(reading=late)


@pytest.mark.parametrize(
    ("margin", "perp", "spot", "expected"),
    [
        (30, None, None, "0.3000"),
        (30, Decimal("20000"), None, "0.3000"),
        (30, None, Decimal("10000"), "0.3000"),
        (30, Decimal("20000"), Decimal("10000"), "0.6000"),
        (30, Decimal("50000"), Decimal("10000"), "1.0000"),
        (30, Decimal("10000"), Decimal("30000"), "0.1000"),
        (33, Decimal("1"), Decimal("3"), "0.1100"),
        (30, Decimal("1"), Decimal("100000"), "0.0000"),
        (30, Decimal("0"), Decimal("10000"), "0.0000"),
        (30, Decimal("-250"), Decimal("10000"), "0.0000"),
        (30, Decimal("20000"), Decimal("0"), "0.0000"),
        # A dead leg is a dead leg whether or not the other one was read.
        (30, Decimal("0"), None, "0.0000"),
        (30, Decimal("-5"), None, "0.0000"),
        (30, None, Decimal("0"), "0.0000"),
        (0, Decimal("20000"), Decimal("10000"), "0.0000"),
    ],
)
def test_spot_weight(margin, perp, spot, expected):
    assert spot_weight(margin, perp, spot) == Decimal(expected)


def test_a_dead_leg_still_gets_a_handoff_with_nothing_to_hedge():
    cases = (
        (Equity(Decimal("0"), day(40)), SPOT),
        (PERP, Equity(Decimal("-5"), day(40))),
        (Equity(Decimal("-250"), day(40)), None),
    )
    for perp, spot in cases:
        handoff = _held(equity_perp=perp, equity_spot=spot)
        assert handoff.perp_side == "short" and handoff.spot_weight == 0
        assert (handoff.equity_perp, handoff.equity_spot) == (perp, spot)


def test_iso_utc_and_plain_spell_the_document():
    assert iso_utc(day(41)) == "2026-02-11T00:00:00+00:00"
    assert plain(Decimal("5E-7")) == "0.0000005"
    assert plain(Decimal("0.6000")) == "0.6000"


# --- the round trip ---------------------------------------------------------


def test_the_document_round_trips_through_the_file(tmp_path: Path):
    handoff = _entered()
    path = tmp_path / "carry-eth.json"
    write_handoff(path, handoff)
    assert read_handoff(path) == handoff
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert doc["version"] == HANDOFF_VERSION
    assert doc["as_of"] == "2026-02-11T00:00:00+00:00"
    assert doc["params"] == {
        "window_days": 30, "z_in": 1.5, "z_out": 0.5, "min_hold_days": 3, "margin_pct": 30
    }  # fmt: skip
    assert doc["perp"] == {"side": "short", "margin_pct": 30}
    assert doc["spot"] == {"token": "WETH", "weight": "0.6000"}
    assert doc["equity"] == {
        "perp": "20000",
        "perp_at_ms": PERP.at_ms,
        "perp_at": "2026-02-10T20:00:00+00:00",
        "spot": "10000",
        "spot_at_ms": SPOT.at_ms,
        "spot_at": "2026-02-10T00:00:00+00:00",
    }
    assert doc["signal"]["funding_hourly"] == "0.00005"
    assert doc["signal"]["z"] == 1.8
    assert doc["position"] == {"side": "in", "entered_at_ms": day(41)}
    assert b"\r" not in path.read_bytes()


def test_nulls_round_trip_too(tmp_path: Path):
    handoff = _held(reading=None, equity_perp=None, equity_spot=None)
    path = tmp_path / "carry-eth.json"
    write_handoff(path, handoff)
    assert read_handoff(path) == handoff
    assert handoff.spot_weight == Decimal("0.3000")
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert doc["equity"]["perp"] is None and doc["equity"]["perp_at_ms"] is None


def test_tiny_rates_are_written_in_positional_notation(tmp_path: Path):
    tiny = Reading(at_ms=day(41) - MS_PER_HOUR, current=Decimal("5E-7"), z=1.8, samples=719,
                   recent_mean=Decimal("4E-7"), recent_samples=24)  # fmt: skip
    path = tmp_path / "carry-eth.json"
    write_handoff(path, _entered(reading=tiny))
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert doc["signal"]["funding_hourly"] == "0.0000005"
    assert doc["signal"]["recent_mean_hourly"] == "0.0000004"
    assert read_handoff(path).reading == tiny


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
        ({"version": 1.0}, "version 1.0"),
        ({"coin": "SOL"}, "coin must be one of"),
        ({"action": "buy"}, "unknown action"),
        ({"action": "stay_out"}, "contradicts position"),
        ({"action": "hold"}, "a hold was entered before the boundary"),
        ({"position.side": "long"}, "unknown side"),
        ({"position.entered_at_ms": None}, "in with an entry instant"),
        ({"position.entered_at_ms": day(42)}, "an entry is at the boundary"),
        ({"params.z_out": 2.0}, "params: z_out must be below z_in"),
        ({"params.margin_pct": 31}, "perp.margin_pct 30 disagrees"),
        ({"perp.side": "long"}, "perp.side 'long' contradicts"),
        ({"perp.margin_pct": 0}, "perp.margin_pct 0 disagrees"),
        ({"perp.margin_pct": 101}, "between 0 and 100"),
        ({"spot.token": "WBTC"}, "is not ETH's WETH"),
        ({"spot.weight": "0.5"}, "disagrees with the equities"),
        ({"spot.weight": 0.6}, "decimal string"),
        ({"spot.weight": "NaN"}, "must be finite"),
        ({"signal": None}, "an entry carries the reading"),
        ({"signal.z": "high"}, "signal.z: expected a number"),
        ({"signal.samples": 3}, "signal: a z-score needs at least"),
        ({"signal.read_at_ms": day(41)}, "is not before as_of_ms"),
        ({"signal.funding_hourly": 5e-05}, "decimal string"),
        ({"equity.perp": "Infinity"}, "must be finite"),
        ({"equity.perp_at_ms": None}, "both set or both null"),
        ({"equity.spot_at_ms": 0}, "equity.spot_at_ms must be at least 1"),
        ({"as_of_ms": 0}, "as_of_ms must be at least 1"),
    ],
)
def test_a_reader_refuses_a_document_that_is_not_a_handoff(changes, fragment):
    with pytest.raises(HandoffError, match=fragment):
        Handoff.from_document(_document(**changes))


def test_a_reader_ignores_keys_it_does_not_know():
    """Fields may be added under the same version; a reader must not choke on them."""
    doc = _document()
    doc["basis"] = {"annualized": "0.05"}
    doc["spot"]["note"] = "later"
    assert Handoff.from_document(doc) == _entered()


def test_a_reader_refuses_a_missing_field():
    doc = _entered().to_document()
    del doc["spot"]["weight"]
    with pytest.raises(HandoffError, match="spot is missing 'weight'"):
        Handoff.from_document(doc)
    del doc["params"]
    with pytest.raises(HandoffError, match="the handoff is missing 'params'"):
        Handoff.from_document(doc)
    with pytest.raises(HandoffError, match="must be an object"):
        Handoff.from_document([])


def test_an_out_document_must_carry_zero_targets():
    out = _out().to_document()
    weighted = json.loads(json.dumps(out))
    weighted["spot"]["weight"] = "0.3"
    with pytest.raises(HandoffError, match="spot.weight 0.3 must be 0 while out"):
        Handoff.from_document(weighted)
    margined = json.loads(json.dumps(out))
    margined["perp"]["margin_pct"] = 30
    with pytest.raises(HandoffError, match="perp.margin_pct 30 disagrees"):
        Handoff.from_document(margined)
    held = json.loads(json.dumps(out))
    held["action"] = "hold"
    with pytest.raises(HandoffError, match="action 'hold' contradicts position 'out'"):
        Handoff.from_document(held)
