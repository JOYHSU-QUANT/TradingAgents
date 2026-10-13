"""The report: a day's row from the two legs' books, the lights, the weeks, and the command."""

from __future__ import annotations

import dataclasses
import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from contrib.carry import status as status_module
from contrib.carry.cli import main
from contrib.carry.handoff import iso_utc, read_handoff
from contrib.carry.report import (
    Alert,
    AlertKind,
    Sidecar,
    SidecarProblem,
    alerts,
    build_rows,
    format_report,
    read_sidecars,
    weeks,
)
from contrib.carry.status import AlertStatus, HandoffStatus, report_status
from contrib.carry.stores import (
    CycleKind,
    PerpCycle,
    PerpPosition,
    PerpSnapshot,
    SpotFill,
    SpotTarget,
    SpotValuation,
    StoreReadError,
    perp_cycles,
    perp_fees,
    perp_funding,
    perp_pending_funding,
    perp_snapshots,
    spot_run,
)
from contrib.carry.upstream import RiskAction, from_epoch_ms

from .conftest import (
    COIN,
    MS_PER_HOUR,
    day,
    handoff_document,
    write_perp_books,
    write_sidecar,
    write_spot_books,
)

A, B, C = "a" * 64, "b" * 64, "c" * 64
PERP_RUN, SPOT_RUN = "carry-ETH-1", "paper-carry-1"
MINUTE = 60_000


def _at(day_ms: int, hours: int, minutes: int = 0) -> str:
    return iso_utc(day_ms + hours * MS_PER_HOUR + minutes * MINUTE)


def _sidecar_for(payloads: Path, day_ms: int, *, stem: str, digest: str, hours: int) -> Sidecar:
    path = write_sidecar(
        payloads,
        stem,
        handoff_document(as_of_ms=day_ms),
        cycle_at=_at(day_ms, hours, 5),
        digest=digest,
    )
    sidecars, problems = read_sidecars(payloads, coin=COIN)
    assert problems == []
    return next(sidecar for sidecar in sidecars if sidecar.path == path)


def _snapshot(at_ms: int, equity: str, size: str | None, mark: str | None) -> PerpSnapshot:
    flat = size is None or mark is None
    position = None if flat else PerpPosition(Decimal(size), Decimal(mark))
    return PerpSnapshot(at_ms, Decimal(equity), position)


def _valuation(at_ms: int, balances: dict[str, str], prices: dict[str, str], total: str):
    return SpotValuation(
        at_ms,
        {k: Decimal(v) for k, v in balances.items()},
        {k: Decimal(v) for k, v in prices.items()},
        Decimal(total),
    )


_BOOKS = (
    "funding",
    "pending_funding",
    "fees",
    "snapshots",
    "cycles",
    "sidecars",
    "decisions",
    "valuations",
    "fills",
    "targets",
)


def _rows(*, since_ms: int = day(40), until_ms: int | None = None, coin: str = COIN, **books):
    """``build_rows`` over one day (40 unless given) with empty books but for ``books``."""
    empty: dict[str, object] = {name: [] for name in _BOOKS}
    return build_rows(
        coin=coin,
        quote="USDC",
        since_ms=since_ms,
        until_ms=since_ms if until_ms is None else until_ms,
        **{**empty, **books},
    )


# --- the sidecars -------------------------------------------------------------


def test_the_perp_queries_run_against_the_real_perp_schema(tmp_path: Path):
    # The fixtures here are a hand-copied subset of the perp schema; this is
    # the one place the report's queries meet the real one, so a column a perp
    # migration renames fails here and not on the host.
    from contrib.hyperliquid_perp.persistence.db import apply_migrations, connect

    path = tmp_path / "real.db"
    conn = connect(path)
    try:
        apply_migrations(conn)
    finally:
        conn.close()
    assert perp_cycles(path, PERP_RUN) == []
    assert perp_funding(path, PERP_RUN, COIN) == []
    assert perp_fees(path, PERP_RUN, COIN) == []
    assert perp_snapshots(path, PERP_RUN, COIN) == []


def test_read_sidecars_orders_by_cycle_and_reports_what_it_cannot_read(tmp_path: Path):
    payloads = tmp_path / "payloads"
    assert read_sidecars(payloads, coin=COIN) == ([], [])
    later = write_sidecar(
        payloads, "in2", handoff_document(as_of_ms=day(40)), cycle_at=_at(day(40), 4, 5), digest=A
    )
    earlier = write_sidecar(
        payloads, "in1", handoff_document(as_of_ms=day(40)), cycle_at=_at(day(40), 0, 5), digest=A
    )
    (payloads / "in3.handoff.json").write_text("{not json", encoding="utf-8")
    (payloads / "in4.handoff.json").write_text("{}", encoding="utf-8")
    other = handoff_document(as_of_ms=day(40))
    other["coin"], other["spot"] = "BTC", {**other["spot"], "token": "WBTC"}
    write_sidecar(payloads, "in5", other, cycle_at=_at(day(40), 8, 5), digest=B)
    bad_digest = json.loads((payloads / "in1.handoff.json").read_text(encoding="utf-8"))
    bad_digest["digest"] = A
    (payloads / "in6.handoff.json").write_text(json.dumps(bad_digest), encoding="utf-8")
    # The payload itself does not carry the suffix and is not looked at.
    (payloads / "in1.json").write_text("{}", encoding="utf-8")

    sidecars, problems = read_sidecars(payloads, coin=COIN)
    assert [sidecar.path for sidecar in sidecars] == [earlier, later]
    assert sidecars[0].digest == A and sidecars[0].handoff.as_of_ms == day(40)
    assert sidecars[0].cycle_at_ms == day(40) + 5 * MINUTE
    assert [(p.path.name, p.reason.split(": ")[-1]) for p in problems] == [
        ("in3.handoff.json", "line 1 column 2 (char 1)"),
        ("in4.handoff.json", "not a handoff sidecar"),
        ("in5.handoff.json", "BTC's document, not ETH's"),
        ("in6.handoff.json", f"digest {A!r} is not sha256:<hex>"),
    ]


# --- a day's row --------------------------------------------------------------


def _entry_day(tmp_path: Path):
    """Day 40: both legs entered; six cycles applied, one pending; the handoff's digest on both sides."""
    payloads = tmp_path / "payloads"
    sidecars = [
        _sidecar_for(payloads, day(40), stem="in1", digest=A, hours=0),
        _sidecar_for(payloads, day(40), stem="in2", digest=A, hours=4),
    ]
    cycles = [
        PerpCycle(day(40) + h * MS_PER_HOUR + 5 * MINUTE, CycleKind.APPLIED, RiskAction.APPROVED)
        for h in (0, 4, 8, 12, 16, 20)
    ] + [PerpCycle(day(40) + 23 * MS_PER_HOUR + 55 * MINUTE, CycleKind.PENDING, None)]
    return _rows(
        funding=[(day(40) + h * MS_PER_HOUR, Decimal("0.05")) for h in range(1, 24)],
        fees=[(day(40) + 10 * MINUTE, Decimal("0.45"))],
        snapshots=[
            _snapshot(day(40) + 20 * MINUTE, "10000", "-0.9", "3000"),
            _snapshot(day(40) + 23 * MS_PER_HOUR, "10001", "-0.9", "3010"),
        ],
        cycles=cycles,
        sidecars=sidecars,
        decisions=[(day(40), "filled")],
        valuations=[
            _valuation(day(40), {"USDC": "7300", "WETH": "0.9"}, {"WETH": "2995"}, "9995.5")
        ],
        fills=[SpotFill(day(40), "USDC", "WETH", Decimal(2700), Decimal("0.9"), Decimal("0.001"))],
        targets=[SpotTarget(day(40), Decimal("0.3"), A)],
    )


def test_a_days_row_reads_each_leg_in_its_own_books(tmp_path: Path):
    (row,) = _entry_day(tmp_path)
    assert row.boundary.isoformat() == "2026-02-10"
    assert row.perp_funding == Decimal("1.15") and row.perp_funding_pending == 0
    assert row.perp_fees == Decimal("0.45")
    assert (row.perp_size, row.perp_mark, row.perp_equity) == (
        Decimal("-0.9"),
        Decimal(3010),
        Decimal(10001),
    )
    assert row.perp_first == _snapshot(day(40) + 20 * MINUTE, "10000", "-0.9", "3000")
    assert dict(row.cycles) == {CycleKind.APPLIED: 6, CycleKind.PENDING: 1}
    assert (row.applied, row.unusable, row.refused) == (6, 0, 0)
    assert row.perp_digests == {A}
    assert (row.spot_outcome, row.spot_qty, row.spot_price) == (
        "filled",
        Decimal("0.9"),
        Decimal(2995),
    )
    assert row.spot_target is not None and row.spot_target.weight == Decimal("0.3")
    # 2700 USDC in, 0.9 WETH out at the bar's 2995: the pools' fees and the slippage together.
    assert row.spot_swap_cost == Decimal("4.5")
    assert (row.spot_gas_eth, row.spot_gas_cost) == (Decimal("0.001"), Decimal("2.995"))
    assert row.costs == Decimal("7.945") and row.net == Decimal("-6.795")
    assert row.intent_in is True
    assert row.delta == (Decimal("0"), Decimal("0"))
    assert row.basis is not None and f"{row.basis:.6f}" == "-0.001667"
    assert not row.held_by_d5
    assert alerts([row], delta_alert_pct=10, d5_days=3) == []


def test_a_refused_target_is_counted_apart_from_an_approved_one():
    (row,) = _rows(
        cycles=[
            PerpCycle(day(40), CycleKind.APPLIED, RiskAction.APPROVED),
            PerpCycle(day(40) + 4 * MS_PER_HOUR, CycleKind.APPLIED, RiskAction.REJECTED),
            PerpCycle(day(40) + 8 * MS_PER_HOUR, CycleKind.APPLIED, RiskAction.INVALID_FAIL_CLOSED),
            PerpCycle(day(40) + 12 * MS_PER_HOUR, CycleKind.UNUSABLE, None),
        ]
    )
    assert (row.applied, row.refused, row.unusable) == (3, 2, 1)


def test_an_empty_day_has_no_books_no_intent_and_lights_silent_day():
    (row,) = _rows()
    assert row.perp_size == 0 and row.spot_qty == 0 and row.perp_equity is None
    assert row.intent_in is None and row.delta == (Decimal(0), None) and row.basis is None
    assert row.spot_gas_cost == 0 and row.net == 0 and row.silent
    found = alerts([row], delta_alert_pct=10, d5_days=1)
    assert [alert.kind for alert in found] == [AlertKind.SILENT_DAY]
    # A day either leg acted on is not silent, whatever else it lights.
    (perp_only,) = _rows(cycles=[PerpCycle(day(40), CycleKind.UNUSABLE, None)])
    (spot_only,) = _rows(
        decisions=[(day(40), "hold")], valuations=[_valuation(day(40), {}, {}, "0")]
    )
    assert not perp_only.silent and not spot_only.silent
    # A trace that is not an action keeps the day silent: cycles that decided
    # nothing all day, a run that is not a file target, a bar skipped as suspect.
    (failed_all_day,) = _rows(
        cycles=[PerpCycle(day(40) + h * MS_PER_HOUR, CycleKind.NO_DECISION, None) for h in (0, 4)]
    )
    (engine_run,) = _rows(cycles=[PerpCycle(day(40), CycleKind.ENGINE, RiskAction.APPROVED)])
    (skipped,) = _rows(
        decisions=[(day(40), "skipped_suspect")], valuations=[_valuation(day(40), {}, {}, "0")]
    )
    (pending_all_day,) = _rows(
        cycles=[PerpCycle(day(40) + h * MS_PER_HOUR, CycleKind.PENDING, None) for h in (0, 4)]
    )
    for silent_row in (failed_all_day, engine_run, skipped, pending_all_day):
        assert silent_row.silent
    # A skipped bar was never put to the strategy: not a D5 hold, so a silent
    # day is never a D5 day as well.
    assert not skipped.spot_held_by_d5 and not skipped.held_by_d5
    (lit,) = alerts([failed_all_day], delta_alert_pct=10, d5_days=3)
    assert lit.kind is AlertKind.SILENT_DAY
    assert lit.text == "neither leg acted: perp 2 cycle(s): no_decision 2, spot not decided"


def test_a_book_day_refuses_impossible_states():
    (row,) = _rows()
    first = _snapshot(day(40), "1", None, None)
    with pytest.raises(ValueError, match="first and last perp snapshot"):
        dataclasses.replace(row, perp_first=first)
    with pytest.raises(ValueError, match="decided spot bar carries its valuation"):
        dataclasses.replace(row, spot_outcome="hold")
    with pytest.raises(ValueError, match="refuses applied cycles only"):
        dataclasses.replace(row, refused=1)


def test_the_span_must_be_day_boundaries():
    with pytest.raises(ValueError, match="UTC day boundaries"):
        _rows(since_ms=day(40) + 1, until_ms=day(40) + 1)


def test_gas_is_unpriced_without_the_gas_tokens_price():
    (row,) = _rows(
        coin="BTC",
        decisions=[(day(40), "filled")],
        valuations=[_valuation(day(40), {"WBTC": "0.05"}, {"WBTC": "60000"}, "3000")],
        fills=[SpotFill(day(40), "USDC", "WBTC", Decimal(3010), Decimal("0.05"), Decimal("0.002"))],
    )
    assert row.spot_qty == Decimal("0.05")
    assert row.spot_swap_cost == Decimal(10)
    assert (row.spot_gas_eth, row.spot_gas_cost) == (Decimal("0.002"), None)
    assert row.costs == Decimal(10) and row.unpriced
    lines = format_report([row], [], [], coin="BTC", delta_alert_pct=10, d5_days=3)
    assert "gas n/a (0.002 ETH)" in lines[1]
    assert "(1 day(s) with a swap or gas not priced, left out)" in lines[-1]


def test_a_swap_through_a_token_the_valuation_does_not_price_is_unpriced_not_free():
    # The WBTC leg of a two-hop route lands in a token the bar's prices lack:
    # the swap's cost is n/a and said, never a 0 that flatters the net.
    (row,) = _rows(
        coin="BTC",
        decisions=[(day(40), "filled")],
        valuations=[_valuation(day(40), {"WBTC": "0.05"}, {"WETH": "3000"}, "3000")],
        fills=[SpotFill(day(40), "USDC", "WBTC", Decimal(3010), Decimal("0.05"), Decimal("0.001"))],
    )
    assert row.spot_swap_cost is None and row.spot_gas_cost == Decimal(3)
    assert row.costs == Decimal(3) and row.unpriced
    lines = format_report([row], [], [], coin="BTC", delta_alert_pct=10, d5_days=3)
    assert "spot swaps n/a" in lines[1]
    assert weeks([row], [])[0].spot_swap_cost is None


# --- the lights -----------------------------------------------------------------


def _row(day_ms: int, payloads: Path | None = None, **changes):
    """A day whose perp is short 0.9 and whose spot holds 0.9, by default.

    Given ``payloads``, the day's one applied cycle has its sidecar there
    (digest ``A``); without, the day has no sidecar and says so.
    """
    books: dict[str, object] = {
        "snapshots": [_snapshot(day_ms + MS_PER_HOUR, "10000", "-0.9", "3000")],
        "cycles": [PerpCycle(day_ms + 5 * MINUTE, CycleKind.APPLIED, RiskAction.APPROVED)],
        "decisions": [(day_ms, "hold")],
        "valuations": [_valuation(day_ms, {"WETH": "0.9"}, {"WETH": "3000"}, "2700")],
        "targets": [SpotTarget(day_ms, Decimal("0.3"), A)],
    }
    if payloads is not None:
        stem = f"in{day_ms // MS_PER_HOUR}"
        books["sidecars"] = [_sidecar_for(payloads, day_ms, stem=stem, digest=A, hours=0)]
    (row,) = _rows(since_ms=day_ms, **{**books, **changes})
    return row


def _kinds(found: list[Alert]) -> list[tuple[str, str]]:
    return [
        (from_epoch_ms(alert.boundary_ms).date().isoformat(), alert.kind.value) for alert in found
    ]


def test_the_delta_lights_when_one_leg_is_short_of_the_other_whatever_the_intent(tmp_path: Path):
    payloads = tmp_path / "payloads"
    exited_spot = _row(
        day(42),
        payloads,
        valuations=[_valuation(day(42), {"USDC": "10050"}, {"WETH": "3000"}, "10050")],
        targets=[SpotTarget(day(42), Decimal("0"), A)],
    )
    found = alerts([exited_spot], delta_alert_pct=10, d5_days=3)
    assert _kinds(found) == [("2026-02-12", "delta")]
    assert "spot holds 0.0000, perp holds -0.9000" in found[0].text
    assert "100.00%" in found[0].text
    # Within the threshold: a 5% gap is not a light at 10%, and is one at 4%.
    within = _row(
        day(42),
        payloads,
        valuations=[_valuation(day(42), {"WETH": "0.855"}, {"WETH": "3000"}, "2565")],
    )
    assert alerts([within], delta_alert_pct=10, d5_days=3) == []
    assert _kinds(alerts([within], delta_alert_pct=4, d5_days=3)) == [("2026-02-12", "delta")]


def test_a_perp_long_adds_to_the_spot_instead_of_hedging_it_and_lights(tmp_path: Path):
    # The hedge never asks for a long; a signed read sees 1.9 of exposure where an
    # unsigned one would have called 0.9 against 1.0 a near-perfect hedge.
    long = _row(
        day(42),
        tmp_path / "payloads",
        snapshots=[_snapshot(day(42) + MS_PER_HOUR, "10000", "1.0", "3000")],
    )
    assert long.delta == (Decimal("1.9"), Decimal("1.9"))
    found = alerts([long], delta_alert_pct=10, d5_days=3)
    assert _kinds(found) == [("2026-02-12", "delta")]
    assert "spot holds 0.9000, perp holds 1.0000" in found[0].text
    # A naked short with no spot lights the same way.
    naked = _row(day(42), tmp_path / "payloads", valuations=[], decisions=[], targets=[])
    assert naked.delta == (Decimal("-0.9"), Decimal(1))


def test_a_one_legged_day_lights_from_either_side(tmp_path: Path):
    payloads = tmp_path / "payloads"
    perp_silent = _row(day(42), cycles=[PerpCycle(day(42) + 5 * MINUTE, CycleKind.UNUSABLE, None)])
    found = alerts([perp_silent], delta_alert_pct=10, d5_days=3)
    assert _kinds(found) == [("2026-02-12", "one-legged")]
    assert "no perp cycle applied" in found[0].text and "unusable 1" in found[0].text
    spot_silent = _row(day(42), payloads, targets=[])
    found = alerts([spot_silent], delta_alert_pct=10, d5_days=3)
    assert _kinds(found) == [("2026-02-12", "one-legged")]
    assert "spot bar has no target" in found[0].text
    undecided = _row(day(42), payloads, decisions=[], valuations=[], targets=[])
    found = alerts([undecided], delta_alert_pct=10, d5_days=3)
    one_legged = next(alert for alert in found if alert.kind is AlertKind.ONE_LEGGED)
    assert "spot bar has no decision" in one_legged.text
    # A suspect bar the strategy was never asked about did not act on its target.
    skipped = _row(day(42), payloads, decisions=[(day(42), "skipped_suspect")])
    found = alerts([skipped], delta_alert_pct=10, d5_days=3)
    assert _kinds(found) == [("2026-02-12", "one-legged")]
    assert "spot bar has outcome skipped_suspect" in found[0].text
    # Applied cycles with no sidecar under the payload directory are said too.
    no_sidecar = _row(day(42))
    assert _kinds(alerts([no_sidecar], delta_alert_pct=10, d5_days=3)) == [
        ("2026-02-12", "no sidecar")
    ]


def test_different_documents_light_when_the_spot_digest_is_not_among_the_perp_sidecars(
    tmp_path: Path,
):
    payloads = tmp_path / "payloads"
    sidecars = [_sidecar_for(payloads, day(41), stem="in1", digest=B, hours=0)]
    same = _row(day(41), sidecars=sidecars, targets=[SpotTarget(day(41), Decimal("0.3"), B)])
    assert alerts([same], delta_alert_pct=10, d5_days=3) == []
    other = _row(day(41), sidecars=sidecars, targets=[SpotTarget(day(41), Decimal("0.3"), A)])
    found = alerts([other], delta_alert_pct=10, d5_days=3)
    assert _kinds(found) == [("2026-02-11", "different documents")]
    assert "aaaaaaaaaaaa…" in found[0].text and "bbbbbbbbbbbb…" in found[0].text


def test_days_held_by_d5_light_the_escalation_only_in_a_row(tmp_path: Path):
    payloads = tmp_path / "payloads"
    unusable = [PerpCycle(day(42) + h * MS_PER_HOUR, CycleKind.UNUSABLE, None) for h in (0, 4)]
    fine = _row(day(41), sidecars=[_sidecar_for(payloads, day(41), stem="in1", digest=A, hours=0)])
    perp_held = _row(day(42), cycles=unusable, targets=[])  # the spot bar had no target either
    spot_held = _row(day(43), targets=[])
    assert (fine.held_by_d5, perp_held.held_by_d5, spot_held.held_by_d5) == (False, True, True)
    assert (perp_held.perp_held_by_d5, perp_held.spot_held_by_d5) == (True, True)
    assert (spot_held.perp_held_by_d5, spot_held.spot_held_by_d5) == (False, True)
    still_held = _row(day(44), targets=[])
    rows = [fine, perp_held, spot_held, still_held]

    def d5(found: list[Alert]) -> list[Alert]:
        return [alert for alert in found if alert.kind == "held by D5"]

    # Lit on the day the streak reaches the threshold AND on every day it goes on,
    # so the newest day's lines show it while it lasts.
    lit = d5(alerts(rows, delta_alert_pct=10, d5_days=2))
    assert _kinds(lit) == [("2026-02-13", "held by D5"), ("2026-02-14", "held by D5")]
    assert "(today: spot)" in lit[0].text and "2 days in a row" in lit[0].text
    assert "3 days in a row" in lit[1].text
    assert _kinds(d5(alerts(rows, delta_alert_pct=10, d5_days=3))) == [("2026-02-14", "held by D5")]
    # A fine day in between breaks the streak.
    assert d5(alerts([perp_held, fine, spot_held], delta_alert_pct=10, d5_days=2)) == []
    # A silent day in between (the daemon down inside the coordinator's outage)
    # carries it: the escalation must not restart from the outage within the outage.
    (silent_between,) = _rows(since_ms=day(43))
    carried = d5(alerts([perp_held, silent_between, still_held], delta_alert_pct=10, d5_days=2))
    assert _kinds(carried) == [("2026-02-14", "held by D5")]
    assert "2 days in a row" in carried[0].text
    # Once lit, a silent day keeps the light on and says it is carrying, not held.
    (silent_after,) = _rows(since_ms=day(45))
    kept = d5(alerts([perp_held, spot_held, silent_after], delta_alert_pct=10, d5_days=2))
    assert _kinds(kept) == [("2026-02-13", "held by D5"), ("2026-02-15", "held by D5")]
    assert "(today: silent, the streak carried)" in kept[1].text
    # A pending cycle alone is the schedule, not a fault.
    pending = _row(day(42), cycles=[PerpCycle(day(42), CycleKind.PENDING, None)], targets=[])
    assert pending.perp_held_by_d5 is False


def test_a_leg_without_a_reading_keeps_its_last_known_holding(tmp_path: Path):
    # Day 42: the coordinator failed, the spot visit refused at target, so the
    # bar was never decided and has no valuation; the spot ledger still holds
    # the 0.9 of day 41. The delta must not read the unobserved leg as empty.
    payloads = tmp_path / "payloads"
    rows = _rows(
        since_ms=day(41),
        until_ms=day(42),
        snapshots=[
            _snapshot(day(41) + MS_PER_HOUR, "10000", "-0.9", "3000"),
            _snapshot(day(42) + MS_PER_HOUR, "10000", "-0.9", "3000"),
        ],
        cycles=[
            PerpCycle(day(41) + 5 * MINUTE, CycleKind.APPLIED, RiskAction.APPROVED),
            PerpCycle(day(42) + 5 * MINUTE, CycleKind.UNUSABLE, None),
        ],
        sidecars=[_sidecar_for(payloads, day(41), stem="in1", digest=A, hours=0)],
        decisions=[(day(41), "hold")],
        valuations=[_valuation(day(41), {"WETH": "0.9"}, {"WETH": "3000"}, "2700")],
        targets=[SpotTarget(day(41), Decimal("0.3"), A)],
    )
    yesterday, today = rows
    assert not yesterday.spot_carried and not yesterday.perp_carried
    assert today.spot_carried and not today.perp_carried
    assert today.spot_qty == Decimal("0.9") and today.delta == (Decimal(0), Decimal(0))
    assert today.basis is None and yesterday.basis == Decimal(0)  # a carried close is no basis
    assert today.perp_held_by_d5 and not today.silent
    found = alerts(rows, delta_alert_pct=10, d5_days=1)
    assert _kinds(found) == [("2026-02-12", "held by D5")]
    lines = format_report(rows, found, [], coin=COIN, delta_alert_pct=10, d5_days=1)
    assert "spot 0.9000 WETH @ 3000.00 (as of 2026-02-11)  delta 0.0000 (0.00%)" in lines[5]
    assert "(as of" not in lines[2]
    # The perp side carries the same way, and a span that starts after the
    # last reading is seeded from it.
    (later,) = _rows(
        since_ms=day(43),
        snapshots=[_snapshot(day(42) + MS_PER_HOUR, "10000", "-0.9", "3000")],
        valuations=[_valuation(day(42), {"WETH": "0.9"}, {"WETH": "3000"}, "2700")],
        decisions=[(day(42), "hold")],
    )
    assert later.perp_carried and later.spot_carried and later.silent
    assert later.delta == (Decimal(0), Decimal(0)) and later.perp_equity == Decimal(10000)
    lines = format_report([later], [], [], coin=COIN, delta_alert_pct=10, d5_days=3)
    assert (
        "perp -0.9000 ETH @ 3000.00 (as of 2026-02-12)  spot 0.9000 WETH @ 3000.00 "
        "(as of 2026-02-12)"
    ) in lines[2]


# --- the weeks ------------------------------------------------------------------


def test_weeks_are_iso_weeks_with_the_days_in_and_the_alerts_counted():
    rows = [_row(day(n)) for n in range(43, 48)]  # Fri 13 Feb .. Tue 17 Feb 2026
    rows[1] = _row(day(44), targets=[SpotTarget(day(44), Decimal("0"), A)])  # Sat: out
    found = [Alert(day(46), "delta", "x"), Alert(day(46), "one-legged", "y")]
    by_week = weeks(rows, found)
    assert [(w.year, w.week, w.days, w.days_in, w.alerts) for w in by_week] == [
        (2026, 7, 3, 2, 0),  # Fri, Sat, Sun
        (2026, 8, 2, 2, 2),  # Mon, Tue
    ]
    assert by_week[0].net == 0 and by_week[0].spot_gas_cost == 0
    assert by_week[0].funding_pending == 0


# --- the command ----------------------------------------------------------------


def _books(
    tmp_path: Path, *, with_targets: bool = True, tokens=("USDC", "WETH")
) -> tuple[Path, Path, Path]:
    """Four days, 10–13 Feb 2026: enter, hold, the spot exits alone, both held by D5."""
    perp = write_perp_books(
        tmp_path / "paper_trading.db",
        PERP_RUN,
        cycles=[(_at(day(40), h, 5), "applied") for h in (0, 4, 8, 12, 16, 20)]
        + [(_at(day(40), 23, 55), "pending")]
        + [(_at(day(41), h, 5), "applied") for h in (0, 4, 8, 12, 16, 20)]
        + [(_at(day(41), 23, 55), "off_grid", "invalid_fail_closed")]
        + [(_at(day(42), h, 5), "unusable") for h in (0, 4, 8, 12, 16, 20)]
        + [(_at(day(43), h, 5), "unusable") for h in (0, 4, 8, 12)]
        + [(_at(day(43), 16, 5), "failed"), (_at(day(43), 20, 5), "in_progress")],
        funding=[(_at(day(40), h), "0.05") for h in range(1, 24)]
        + [(_at(day(41), h), "0.05") for h in range(24)]
        + [(_at(day(42), h), "0.04") for h in range(24)]
        + [(_at(day(43), h), "0.03") for h in range(24)],
        fills=[(_at(day(40), 0, 10), "0.45")],
        snapshots=[
            (_at(day(40), 0, 20), "10000", "-0.9", "3000"),
            (_at(day(40), 23), "10001", "-0.9", "3010"),
            (_at(day(41), 23), "10002", "-0.9", "3100"),
            (_at(day(42), 23), "10003", "-0.9", "3000"),
            (_at(day(43), 23), "10004", "-0.9", "2900"),
            # The stops closed the perp late on day 43: a snapshot with no position row.
            (_at(day(43), 23, 30), "10005", None, None),
        ],
    )
    spot = write_spot_books(
        tmp_path / "paper.db",
        SPOT_RUN,
        source="carry-eth-v1" if with_targets else None,
        tokens=tokens,
        decisions=[
            (day(40) // 1000, "filled"),
            (day(41) // 1000, "hold"),
            (day(42) // 1000, "filled"),
            (day(43) // 1000, "hold"),
        ],
        valuations=[
            (day(40) // 1000, {"USDC": "7300", "WETH": "0.9"}, {"WETH": "2995"}, "9995.5"),
            (day(41) // 1000, {"USDC": "7300", "WETH": "0.9"}, {"WETH": "3090"}, "10081"),
            (day(42) // 1000, {"USDC": "10050", "WETH": "0"}, {"WETH": "3000"}, "10050"),
            (day(43) // 1000, {"USDC": "10050", "WETH": "0"}, {"WETH": "2900"}, "10050"),
        ],
        fills=[
            (day(40) // 1000, 0, "USDC", "WETH", "2700", "0.9", "0.001"),
            (day(42) // 1000, 0, "WETH", "USDC", "0.9", "2690", "0.001"),
        ],
        targets=[
            (day(40) // 1000, "0.3", A),
            (day(41) // 1000, "0.3", B),
            (day(42) // 1000, "0", C),
        ],
    )
    payloads = tmp_path / "payloads" / PERP_RUN  # the perp package's layout beside the store
    for stem, as_of, hours, digest in (
        ("in1", day(40), 0, A),
        ("in2", day(40), 4, A),
        ("in8", day(41), 0, B),
    ):
        write_sidecar(
            payloads,
            stem,
            handoff_document(as_of_ms=as_of),
            cycle_at=_at(as_of, hours, 5),
            digest=digest,
        )
    return perp, spot, payloads


def _report(perp: Path, spot: Path, *extra: str, now_ms: int = day(44) + 3 * MS_PER_HOUR):
    argv = [
        "report",
        "--coin",
        COIN,
        "--perp-db",
        str(perp),
        "--perp-run-id",
        PERP_RUN,
        "--spot-db",
        str(spot),
        "--spot-run-id",
        SPOT_RUN,
        *extra,
    ]
    return argv, (lambda: from_epoch_ms(now_ms))


def test_the_stores_read_as_the_report_needs_them(tmp_path: Path):
    perp, spot, _ = _books(tmp_path)
    cycles = perp_cycles(perp, PERP_RUN)
    assert len(cycles) == 25  # the in-progress attempt is not a cycle yet
    # The off-grid margin: the contract refused it after the document applied.
    assert (cycles[13].kind, cycles[13].risk_action) == (
        CycleKind.APPLIED,
        RiskAction.INVALID_FAIL_CLOSED,
    )
    assert [c.kind for c in cycles[-3:]] == [
        CycleKind.UNUSABLE,
        CycleKind.UNUSABLE,
        CycleKind.NO_DECISION,
    ]
    assert cycles[0].risk_action is RiskAction.APPROVED and cycles[-1].risk_action is None
    assert sum(pnl for _, pnl in perp_funding(perp, PERP_RUN, COIN)) == Decimal("4.03")
    # The fixture always adds one settlement the venue had not posted yet.
    assert perp_pending_funding(perp, PERP_RUN, COIN) == [day(40) + 23 * MS_PER_HOUR]
    snapshots = perp_snapshots(perp, PERP_RUN, COIN)
    assert snapshots[0].position == PerpPosition(Decimal("-0.9"), Decimal(3000))
    # A flat snapshot has no position row.
    assert snapshots[-1].position is None
    # A gate word this package does not know refuses the read, not a silent "approved".
    with sqlite3.connect(perp) as conn:
        conn.execute("UPDATE ai_outputs SET risk_action = 'waved_through' WHERE output_id = 'out0'")
    with pytest.raises(StoreReadError, match="'waved_through' is not a gate action"):
        perp_cycles(perp, PERP_RUN)
    # So is an attempt status it does not know.
    with sqlite3.connect(perp) as conn:
        conn.execute("UPDATE ai_outputs SET risk_action = 'approved' WHERE output_id = 'out0'")
        conn.execute(
            "UPDATE decision_attempts SET status = 'parked' WHERE decision_attempt_id = 'attempt1'"
        )
    with pytest.raises(StoreReadError, match="'parked' is not an attempt status"):
        perp_cycles(perp, PERP_RUN)
    run = spot_run(spot, SPOT_RUN)
    assert (run.quote, run.tokens, run.target_source) == ("USDC", ("USDC", "WETH"), "carry-eth-v1")


def test_the_command_prints_the_days_the_lights_the_weeks_and_the_span(tmp_path: Path, capsys):
    perp, spot, _ = _books(tmp_path)
    argv, now = _report(perp, spot)
    assert main(argv, now=now) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0] == (
        "carry report: ETH/WETH, 4 day(s) from 2026-02-10 to 2026-02-13 "
        "(delta lights above 10%, D5 lights at 3 days)"
    )
    assert out[1] == (
        "2026-02-10  in   funding 1.1500 (1 pending)  perp fees 0.4500  spot swaps 4.5000  "
        "gas 2.9950 (0.001 ETH)  net -6.7950"
    )
    assert out[2] == (
        "    perp -0.9000 ETH @ 3010.00  spot 0.9000 WETH @ 2995.00  delta 0.0000 (0.00%)  "
        "basis -0.17% (spot close vs perp mark 3000.00 at 00:20Z)"
    )
    assert out[3] == (
        "    perp 7 cycle(s): applied 6, pending 1; spot filled, target 0.3; "
        "equity perp 10001.0000 / spot 9995.5000"
    )
    day_42 = out.index(
        "2026-02-12  out  funding 0.9600  perp fees 0.0000  spot swaps 10.0000  "
        "gas 3.0000 (0.001 ETH)  net -12.0400"
    )
    assert out[day_42 + 2].startswith("    perp 6 cycle(s): unusable 6; spot filled, target 0; ")
    assert out[day_42 + 3].startswith(
        "    ALERT delta: net exposure -0.9000 of the coin (100.00% of the larger leg"
    )
    assert out[day_42 + 4].startswith(
        "    ALERT one-legged: the spot bar decided on a target while no perp cycle applied"
    )
    day_43 = out.index(
        "2026-02-13  ?    funding 0.7200  perp fees 0.0000  spot swaps 0.0000  gas 0  net 0.7200"
    )
    # Both legs flat at the day's end (the perp's last snapshot has no position):
    # nothing to light, no share to print, the basis from the day's first mark.
    assert out[day_43 + 1] == (
        "    perp 0.0000 ETH @ n/a  spot 0.0000 WETH @ 2900.00  delta 0.0000 (n/a)  "
        "basis +0.00% (spot close vs perp mark 2900.00 at 23:00Z)"
    )
    # Four unusable, one failed; the attempt still in progress is not a cycle yet.
    assert out[day_43 + 2] == (
        "    perp 5 cycle(s): unusable 4, no_decision 1; spot hold, target none; "
        "equity perp 10005.0000 / spot 10050.0000"
    )
    assert not out[day_43 + 3].startswith("    ALERT")
    # Two days held in a row; the default asks three before the escalation lights.
    assert not any(line.startswith("    ALERT held by D5") for line in out)
    assert out[-2] == (
        "  2026-W07  4 day(s), 2 in  funding 4.0300 (1 pending)  costs 20.9450 (perp fees 0.4500, "
        "spot swaps 14.5000, gas 5.9950)  net -16.9150  alerts 2"
    )
    assert out[-1] == (
        "span: 2 of 4 day(s) in; funding 4.0300 (1 settlement(s) still pending, not counted), "
        "costs 20.9450, net -16.9150; 2 alert(s), 2 day(s) held by D5"
    )


def test_the_command_takes_a_span_and_the_d5_threshold(tmp_path: Path, capsys):
    perp, spot, _ = _books(tmp_path)
    argv, now = _report(
        perp, spot, "--since", "2026-02-12", "--until", "2026-02-13", "--d5-days", "2"
    )
    assert main(argv, now=now) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith("carry report: ETH/WETH, 2 day(s) from 2026-02-12 to 2026-02-13")
    assert any(line.startswith("    ALERT held by D5: 2 days in a row") for line in out)
    assert out[-1].endswith("2 day(s) held by D5")


def test_the_command_finds_the_sidecars_in_the_perp_layout_or_where_told(tmp_path: Path, capsys):
    perp, spot, payloads = _books(tmp_path)
    argv, now = _report(perp, spot, "--until", "2026-02-10")
    assert main(argv, now=now) == 0
    assert "ALERT no sidecar" not in capsys.readouterr().out
    elsewhere = str(tmp_path / "elsewhere")
    argv, now = _report(perp, spot, "--until", "2026-02-10", "--payloads-dir", elsewhere)
    assert main(argv, now=now) == 0
    out = capsys.readouterr().out
    assert "ALERT no sidecar: 6 perp cycle(s) applied a document but no sidecar" in out
    (payloads / "in9.handoff.json").write_text("{", encoding="utf-8")
    argv, now = _report(perp, spot, "--until", "2026-02-10")
    assert main(argv, now=now) == 0
    out = capsys.readouterr().out
    assert "    ALERT sidecar not read: " in out and "1 alert(s)" in out


@pytest.mark.parametrize(
    ("extra", "said"),
    [
        (
            ("--since", "2026-02-12T05:00:00Z"),
            "--since 2026-02-12T05:00:00+00:00 is not a UTC day boundary",
        ),
        (("--since", "2026-02-13", "--until", "2026-02-12"), "the span is empty"),
        (("--delta-alert-pct", "0"), "--delta-alert-pct must be at least 1"),
        (("--d5-days", "0"), "--d5-days must be at least 1"),
    ],
)
def test_the_command_refuses_a_bad_span_or_threshold(tmp_path: Path, capsys, extra, said):
    perp, spot, _ = _books(tmp_path)
    argv, now = _report(perp, spot, *extra)
    assert main(argv, now=now) == 1
    assert said in capsys.readouterr().err


def test_the_command_refuses_a_spot_run_that_is_not_the_carry_leg(tmp_path: Path, capsys):
    perp, spot, _ = _books(tmp_path, with_targets=False)
    argv, now = _report(perp, spot)
    assert main(argv, now=now) == 1
    assert "reads no targets" in capsys.readouterr().err
    (tmp_path / "btc").mkdir()
    perp, spot, _ = _books(tmp_path / "btc", tokens=("USDC", "WBTC"))
    argv, now = _report(perp, spot)
    assert main(argv, now=now) == 1
    assert "trades USDC, WBTC, not WETH" in capsys.readouterr().err


def test_the_command_refuses_two_runs_with_nothing_in_them(tmp_path: Path, capsys):
    perp = write_perp_books(tmp_path / "paper_trading.db", PERP_RUN)
    spot = write_spot_books(tmp_path / "paper.db", SPOT_RUN)
    argv, now = _report(perp, spot)
    assert main(argv, now=now) == 1
    assert "nothing to report" in capsys.readouterr().err


def test_the_command_refuses_a_missing_store(tmp_path: Path, capsys):
    perp = write_perp_books(tmp_path / "paper_trading.db", PERP_RUN)
    argv, now = _report(perp, tmp_path / "nowhere.db")
    assert main(argv, now=now) == 1
    assert "no such file" in capsys.readouterr().err


def test_format_report_says_so_for_an_empty_span():
    assert format_report([], [], [], coin=COIN, delta_alert_pct=10, d5_days=3) == [
        "carry report: ETH: no day in the span"
    ]


def test_the_command_says_when_no_day_has_finished_yet(tmp_path: Path, capsys):
    # Both runs opened today: with no span flags there is no finished day to
    # report, and the refusal says that rather than blaming flags never passed.
    perp, spot, _ = _books(tmp_path)
    argv, now = _report(perp, spot, now_ms=day(40) + 5 * MS_PER_HOUR)
    assert main(argv, now=now) == 1
    err = capsys.readouterr().err
    assert "nothing to report yet: the runs' first day is 2026-02-10" in err
    assert "--until" in err and "is before --since" not in err


def test_a_spot_config_snapshot_the_report_cannot_read_is_refused(tmp_path: Path, capsys):
    perp, spot, _ = _books(tmp_path)
    for config, said in (
        ("not json", "config is not JSON"),
        ('{"targets": {"source": "carry-eth-v1"}}', "config names no tokens"),
        ('{"targets": {"source": ""}, "tokens": ["USDC", "WETH"]}', "reads no targets"),
    ):
        with sqlite3.connect(spot) as conn:
            conn.execute("UPDATE runs SET config = ? WHERE run_id = ?", (config, SPOT_RUN))
        argv, now = _report(perp, spot)
        assert main(argv, now=now) == 1
        assert said in capsys.readouterr().err


# --- report --json ----------------------------------------------------------


def _nodes(value, prefix: str = ""):
    """Every ``(path, value)`` in a document, list items under ``path[]``."""
    if prefix:
        yield prefix, value
    if isinstance(value, dict):
        for key, child in value.items():
            yield from _nodes(child, f"{prefix}.{key}" if prefix else key)
    elif isinstance(value, list):
        for child in value:
            yield from _nodes(child, f"{prefix}[]")


def _paths(document) -> set[str]:
    return {path for path, _ in _nodes(document) if not path.endswith("[]")}


_DAY_FIELDS = (
    "boundary",
    "intent_in",
    "perp_funding",
    "perp_funding_pending",
    "perp_fees",
    "spot_swap_cost",
    "spot_gas_eth",
    "spot_gas_cost",
    "costs",
    "net",
    "unpriced",
    "perp_size",
    "perp_mark",
    "perp_as_of",
    "perp_equity",
    "spot_qty",
    "spot_price",
    "spot_as_of",
    "spot_equity",
    "delta",
    "delta_share",
    "basis",
    "cycles",
    "refused",
    "spot_outcome",
    "spot_target",
    "held_by_d5",
    "alerts",
)
_WEEK_FIELDS = (
    "year",
    "week",
    "days",
    "days_in",
    "funding",
    "funding_pending",
    "perp_fees",
    "spot_swap_cost",
    "spot_gas_cost",
    "costs",
    "net",
    "unpriced",
    "alerts",
)
_SPAN_FIELDS = (
    "days",
    "days_in",
    "funding",
    "funding_pending",
    "costs",
    "unpriced",
    "net",
    "alerts",
    "held_by_d5",
)
_HANDOFF_FIELDS = (
    "path",
    "as_of",
    "written_at",
    "action",
    "perp_side",
    "margin_pct",
    "spot_token",
    "spot_weight",
    "read_at",
    "funding_hourly",
    "z",
)
# The document's shape at status.SCHEMA_VERSION 1. A path added, removed or
# moved changes this set; change it together with the version.
_SCHEMA_1_PATHS = {
    "schema_version",
    "source",
    "generated_at",
    "coin",
    "token",
    "perp_run_id",
    "spot_run_id",
    "since",
    "until",
    "delta_alert_pct",
    "d5_days",
    "handoff",
    *(f"handoff.{field}" for field in _HANDOFF_FIELDS),
    "handoff_error",
    "days",
    *(f"days[].{field}" for field in _DAY_FIELDS),
    *(f"days[].cycles.{kind.value}" for kind in CycleKind),
    "days[].alerts[].kind",
    "days[].alerts[].text",
    "problems",
    "problems[].path",
    "problems[].reason",
    "weeks",
    *(f"weeks[].{field}" for field in _WEEK_FIELDS),
    "span",
    *(f"span.{field}" for field in _SPAN_FIELDS),
}


def _write_handoff(tmp_path: Path, document: object) -> Path:
    handoff = tmp_path / "handoff-ETH.json"
    handoff.write_text(json.dumps(document), encoding="utf-8")
    return handoff


def test_the_command_prints_the_report_as_one_document(tmp_path: Path, capsys):
    perp, spot, payloads = _books(tmp_path)
    (payloads / "bad.handoff.json").write_text("{", encoding="utf-8")
    handoff = _write_handoff(tmp_path, handoff_document(as_of_ms=day(43)))
    argv, now = _report(perp, spot, "--json", "--handoff", str(handoff))
    assert main(argv, now=now) == 0
    text = capsys.readouterr().out
    assert text.isascii()
    document = json.loads(text)
    assert list(document)[:3] == ["schema_version", "source", "generated_at"]
    assert (document["schema_version"], document["source"]) == (1, "carry")
    assert document["generated_at"] == "2026-02-14T03:00:00+00:00"
    assert (document["coin"], document["token"]) == ("ETH", "WETH")
    assert (document["perp_run_id"], document["spot_run_id"]) == (PERP_RUN, SPOT_RUN)
    assert (document["since"], document["until"]) == ("2026-02-10", "2026-02-13")
    assert (document["delta_alert_pct"], document["d5_days"]) == (10, 3)
    assert _paths(document) == _SCHEMA_1_PATHS
    assert not [path for path, value in _nodes(document) if isinstance(value, float)]
    secret = re.compile(r"key|secret|password|mnemonic|address", re.IGNORECASE)
    assert not [path for path in _paths(document) if secret.search(path.rsplit(".", 1)[-1])]

    # The coordinator's file: what the legs act on and the reading, not params or equity.
    assert document["handoff_error"] is None
    assert document["handoff"] == {
        "path": "handoff-ETH.json",
        "as_of": "2026-02-13T00:00:00+00:00",
        "written_at": "2026-02-12T23:50:00+00:00",
        "action": "enter",
        "perp_side": "short",
        "margin_pct": 30,
        "spot_token": "WETH",
        "spot_weight": str(read_handoff(handoff).spot_weight),
        "read_at": "2026-02-12T23:00:00+00:00",
        "funding_hourly": "0.00002",
        "z": "2.0",
    }

    first, _, third, fourth = document["days"]
    assert (first["boundary"], first["intent_in"], first["perp_funding"]) == (
        "2026-02-10",
        True,
        "1.15",
    )
    assert (first["perp_funding_pending"], first["perp_fees"], first["spot_swap_cost"]) == (
        1,
        "0.45",
        "4.5",
    )
    assert (first["spot_gas_eth"], first["net"], first["unpriced"]) == ("0.001", "-6.795", False)
    assert (first["perp_size"], first["perp_mark"], first["spot_qty"], first["spot_price"]) == (
        "-0.9",
        "3010",
        "0.9",
        "2995",
    )
    assert (first["delta"], first["cycles"]["applied"], first["cycles"]["pending"]) == (
        "0.0",
        6,
        1,
    )
    assert (first["spot_outcome"], first["spot_target"], first["held_by_d5"]) == (
        "filled",
        "0.3",
        False,
    )
    assert first["alerts"] == [] and first["perp_as_of"] is None and first["spot_as_of"] is None
    assert [alert["kind"] for alert in third["alerts"]] == ["delta", "one-legged"]
    assert third["alerts"][0]["text"].startswith("net exposure -0.9000 of the coin")
    assert third["held_by_d5"] is True
    # Both legs flat at the end of the last day: no share, the perp mark gone.
    assert (fourth["delta_share"], fourth["perp_mark"], fourth["intent_in"]) == (None, None, None)
    assert fourth["cycles"] == {
        "applied": 0,
        "pending": 0,
        "unusable": 4,
        "no_decision": 1,
        "engine": 0,
    }

    (problem,) = document["problems"]
    assert problem["path"] == "bad.handoff.json"  # the name, not where the host keeps it
    assert problem["reason"].startswith("bad.handoff.json: not JSON")
    assert str(tmp_path) not in problem["reason"]
    (week,) = document["weeks"]
    assert (week["year"], week["week"], week["days"], week["days_in"]) == (2026, 7, 4, 2)
    assert (week["funding"], week["funding_pending"], week["alerts"]) == ("4.03", 1, 2)
    span = document["span"]
    assert span == {
        "days": 4,
        "days_in": 2,
        "funding": "4.03",
        "funding_pending": 1,
        "costs": "20.945",
        "unpriced": 0,
        "net": "-16.915",
        "alerts": 3,
        "held_by_d5": 2,
    }


def test_the_document_and_the_lines_say_the_same_span(tmp_path: Path, capsys):
    perp, spot, _ = _books(tmp_path)
    argv, now = _report(perp, spot)
    assert main(argv, now=now) == 0
    last = capsys.readouterr().out.splitlines()[-1]
    assert main([*argv, "--json"], now=now) == 0
    span = json.loads(capsys.readouterr().out)["span"]
    assert last == (
        f"span: {span['days_in']} of {span['days']} day(s) in; funding "
        f"{Decimal(span['funding']):.4f} ({span['funding_pending']} settlement(s) still pending, "
        f"not counted), costs {Decimal(span['costs']):.4f}, net {Decimal(span['net']):.4f}; "
        f"{span['alerts']} alert(s), {span['held_by_d5']} day(s) held by D5"
    )


def _status(rows, found=(), problems=(), *, now=None, **changes):
    """``report_status`` over a span of day 40 by default, the clock at day 44."""
    fields = {
        "coin": "ETH",
        "perp_run_id": PERP_RUN,
        "spot_run_id": SPOT_RUN,
        "since_ms": day(40),
        "until_ms": day(40) if not rows else rows[-1].boundary_ms,
        "delta_alert_pct": 10,
        "d5_days": 3,
        "handoff": None,
        "now": from_epoch_ms(day(44)) if now is None else now,
    }
    return report_status(list(rows), list(found), list(problems), **{**fields, **changes})


def test_the_document_clock_is_aware_and_spelled_to_the_second_in_utc():
    with pytest.raises(ValueError, match="aware"):
        _status([], now=datetime(2026, 2, 14))
    taipei = timezone(timedelta(hours=8))
    status = _status([], now=datetime(2026, 2, 14, 11, 0, 0, 123456, tzinfo=taipei))
    assert status.generated_at == "2026-02-14T03:00:00+00:00"
    empty = _status([])
    assert (empty.days, empty.weeks, empty.span.days, empty.span.net) == ((), (), 0, Decimal(0))
    assert (empty.handoff, empty.handoff_error) == (None, None)


def test_the_documents_free_text_is_masked_and_cut(tmp_path: Path):
    # A sidecar that failed validation can echo a value, and so can the
    # handoff; a light names digests only, but goes through the same fold.
    wallet, key = "0x" + "12" * 20, "0x" + "ab" * 32
    problems = [
        SidecarProblem(tmp_path / "x.handoff.json", f"bad {wallet} and {key}"),
        SidecarProblem(tmp_path / "y.handoff.json", "z" * 1500),
    ]
    row = _row(day(40))
    found = [Alert(day(40), AlertKind.DELTA, f"net exposure seen by {wallet}")]
    handoff = _write_handoff(tmp_path, {"version": 1, "coin": wallet})
    status = _status([row], found, problems, handoff=handoff)
    first, second = status.problems
    assert (first.path, first.reason) == ("x.handoff.json", "bad 0x1212...1212 and 0xabab...abab")
    assert len(second.reason) == 1000 and second.reason.endswith("zzz...")
    assert status.days[0].alerts == (AlertStatus("delta", "net exposure seen by 0x1212...1212"),)
    assert status.handoff is None
    assert status.handoff_error == (
        "handoff-ETH.json: coin must be one of ['BTC', 'ETH'], got '0x1212...1212'"
    )
    assert status.span.alerts == 3  # the light and the two sidecars; the handoff is not a light
    # Masked before it is cut: a key straddling the cut leaves no hex behind.
    assert status_module._text("a" * 990 + key) == "a" * 990 + "0xabab...."
    straddling = status_module._text("a" * 940 + key + "z" * 100)
    assert len(straddling) == 1000 and "0xabab...abab" in straddling
    assert "ababab" not in straddling


def test_a_day_carries_every_field_of_its_row():
    # Three days with nothing alike: in, out, unknown; the perp read twice on
    # the first day, the spot bar closing above the first mark.
    rows = _rows(
        since_ms=day(40),
        until_ms=day(42),
        funding=[(day(40) + 2 * MS_PER_HOUR, Decimal("1.5"))],
        pending_funding=[day(40) + 3 * MS_PER_HOUR],
        fees=[(day(40) + MS_PER_HOUR, Decimal("0.25"))],
        snapshots=[
            _snapshot(day(40) + MS_PER_HOUR, "10000", "-0.9", "3000"),
            _snapshot(day(40) + 23 * MS_PER_HOUR, "10100", "-0.8", "3100"),
        ],
        cycles=[
            PerpCycle(day(40) + 5 * MINUTE, CycleKind.APPLIED, RiskAction.APPROVED),
            PerpCycle(day(40) + 4 * MS_PER_HOUR, CycleKind.APPLIED, RiskAction.REJECTED),
            PerpCycle(day(40) + 23 * MS_PER_HOUR, CycleKind.PENDING, None),
        ],
        decisions=[(day(40), "hold")],
        valuations=[_valuation(day(40), {"WETH": "0.9"}, {"WETH": "3030"}, "2727")],
        targets=[SpotTarget(day(40), Decimal("0.3"), A), SpotTarget(day(41), Decimal(0), B)],
    )
    status = _status(rows)
    first, second, third = status.days
    assert [d.intent_in for d in status.days] == [True, False, None]
    assert status.span.days_in == 1
    row = rows[0]
    assert dataclasses.asdict(first) == {
        "boundary": "2026-02-10",
        "intent_in": True,
        "perp_funding": Decimal("1.5"),
        "perp_funding_pending": 1,
        "perp_fees": Decimal("0.25"),
        "spot_swap_cost": row.spot_swap_cost,
        "spot_gas_eth": row.spot_gas_eth,
        "spot_gas_cost": row.spot_gas_cost,
        "costs": Decimal("0.25"),
        "net": Decimal("1.25"),
        "unpriced": row.spot_swap_cost is None or row.spot_gas_cost is None,
        "perp_size": Decimal("-0.8"),
        "perp_mark": Decimal("3100"),
        "perp_as_of": None,
        "perp_equity": Decimal("10100"),
        "spot_qty": Decimal("0.9"),
        "spot_price": Decimal("3030"),
        "spot_as_of": None,
        "spot_equity": Decimal("2727"),
        "delta": Decimal("0.1"),
        "delta_share": Decimal("0.1") / Decimal("0.9"),
        "basis": Decimal("0.01"),
        "cycles": {"applied": 2, "pending": 1, "unusable": 0, "no_decision": 0, "engine": 0},
        "refused": 1,
        "spot_outcome": "hold",
        "spot_target": Decimal("0.3"),
        "held_by_d5": False,
        "alerts": (),
    }
    # Days 41 and 42 carry day 40's readings, and say when they were read.
    assert (second.perp_as_of, second.spot_as_of) == (
        "2026-02-10T23:00:00+00:00",
        "2026-02-10T00:00:00+00:00",
    )
    assert (third.perp_equity, third.spot_equity) == (Decimal("10100"), Decimal("2727"))
    assert (third.perp_mark, third.spot_price, third.basis) == (Decimal("3100"), Decimal("3030"), None)
    assert third.spot_outcome is None and third.cycles["applied"] == 0


def test_the_command_without_a_handoff_carries_none_and_an_unreadable_one_says_why(
    tmp_path: Path, capsys
):
    perp, spot, _ = _books(tmp_path)
    argv, now = _report(perp, spot, "--json")
    assert main(argv, now=now) == 0
    document = json.loads(capsys.readouterr().out)
    assert (document["handoff"], document["handoff_error"]) == (None, None)
    handoff = tmp_path / "handoff-ETH.json"
    handoff.write_text("{", encoding="utf-8")
    assert main([*argv, "--handoff", str(handoff)], now=now) == 0
    document = json.loads(capsys.readouterr().out)
    assert document["handoff"] is None
    assert document["handoff_error"].startswith("handoff-ETH.json: not JSON")
    assert str(tmp_path) not in document["handoff_error"]
    # Without --json the lines have no place for it, so the flag is refused, not dropped.
    text_argv = [arg for arg in argv if arg != "--json"]
    assert main([*text_argv, "--handoff", str(handoff)], now=now) == 1
    assert "--handoff goes with --json" in capsys.readouterr().err


def test_a_stay_out_without_a_z_score_and_another_coins_file_are_told_apart(tmp_path: Path):
    # Too few samples: the coordinator stays out and the handoff says z is null.
    document = handoff_document(as_of_ms=day(43), side="flat")
    document["signal"] = {**document["signal"], "z": None, "samples": 12}
    status = _status([], handoff=_write_handoff(tmp_path, document))
    assert status.handoff_error is None and status.handoff is not None
    assert (status.handoff.action, status.handoff.perp_side, status.handoff.spot_weight) == (
        "stay_out",
        "flat",
        Decimal(0),
    )
    assert (status.handoff.read_at, status.handoff.funding_hourly, status.handoff.z) == (
        "2026-02-12T23:00:00+00:00",
        Decimal("0.00002"),
        None,
    )
    # BTC's file is not ETH's handoff, as the coordinator's own memory would refuse it.
    other = {**handoff_document(as_of_ms=day(43)), "coin": "BTC"}
    other["spot"] = {"token": "WBTC", "weight": other["spot"]["weight"]}
    status = _status([], handoff=_write_handoff(tmp_path, other))
    assert status.handoff is None
    assert status.handoff_error == "handoff-ETH.json is BTC's handoff, not ETH's"
    # An instant too large to spell, an equity too large to weigh, a file
    # nested too deep: "could not be read", not a crash.
    huge = {**handoff_document(as_of_ms=day(43)), "written_at_ms": 10**30}
    status = _status([], handoff=_write_handoff(tmp_path, huge))
    assert status.handoff is None and status.handoff_error is not None
    heavy = handoff_document(as_of_ms=day(43))
    heavy["equity"] = {
        "perp": "1E+999990",
        "perp_at_ms": day(42),
        "perp_at": iso_utc(day(42)),
        "spot": "1E-999990",
        "spot_at_ms": day(42),
        "spot_at": iso_utc(day(42)),
    }
    status = _status([], handoff=_write_handoff(tmp_path, heavy))
    assert status.handoff is None and status.handoff_error is not None
    deep = tmp_path / "handoff-ETH.json"
    deep.write_text("[" * 100_000, encoding="utf-8")
    status = _status([], handoff=deep)
    assert status.handoff is None and status.handoff_error is not None


def test_the_document_refuses_what_the_report_cannot_produce():
    row = _row(day(40))
    with pytest.raises(ValueError, match="since"):
        _status([row], since_ms=day(39))
    with pytest.raises(ValueError, match="after"):
        _status([], since_ms=day(41), until_ms=day(40))
    status = _status([row])
    day_status = status.days[0]
    with pytest.raises(ValueError, match="unpriced"):
        dataclasses.replace(day_status, unpriced=True)
    with pytest.raises(ValueError, match="refuses"):
        dataclasses.replace(day_status, refused=5)
    with pytest.raises(ValueError, match="kind"):
        dataclasses.replace(day_status, cycles={"applied": 1})
    with pytest.raises(ValueError, match="carried"):
        dataclasses.replace(day_status, perp_as_of="2026-02-09T23:00:00+00:00", perp_equity=None)
    with pytest.raises(ValueError, match="carried"):
        dataclasses.replace(day_status, spot_as_of="2026-02-09T00:00:00+00:00", spot_equity=None)
    with pytest.raises(ValueError, match="kind"):
        AlertStatus("delta!", "x")
    with pytest.raises(ValueError, match="counts"):
        dataclasses.replace(status, days=())


def test_the_handoff_section_refuses_what_the_coordinator_cannot_write(tmp_path: Path):
    status = _status([], handoff=_write_handoff(tmp_path, handoff_document(as_of_ms=day(43))))
    entry = status.handoff
    assert isinstance(entry, HandoffStatus) and entry.action == "enter"
    with pytest.raises(ValueError, match="not both"):
        dataclasses.replace(status, handoff_error="x")
    with pytest.raises(ValueError, match="whole"):
        dataclasses.replace(entry, funding_hourly=None)
    with pytest.raises(ValueError, match="z score"):
        dataclasses.replace(entry, read_at=None, funding_hourly=None)
    with pytest.raises(ValueError, match="entry carries"):
        dataclasses.replace(entry, read_at=None, funding_hourly=None, z=None)
    with pytest.raises(ValueError, match="action"):
        dataclasses.replace(entry, action="buy")
    with pytest.raises(ValueError, match="perp_side"):
        dataclasses.replace(entry, perp_side="long")
    # A hold decided without a reading is legitimate, and so is a stay-out.
    held = dataclasses.replace(entry, action="hold", read_at=None, funding_hourly=None, z=None)
    assert held.read_at is None
