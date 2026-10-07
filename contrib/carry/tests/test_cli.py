"""``python -m contrib.carry`` end to end: the handoff, the memory, the fetch, the refusals."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from contrib.carry import __main__ as entry
from contrib.carry.cli import GRACE_HOURS, default_boundary, main
from contrib.carry.handoff import read_handoff
from contrib.carry.signal import Action, Position, Side
from contrib.carry.upstream import ExchangeError, ResearchStore, StopReason, from_epoch_ms

from .conftest import (
    COIN,
    DAY0,
    MS_PER_HOUR,
    FakeMarket,
    alternating,
    day,
    hourly,
    hump_series,
    write_perp_store,
    write_research_store,
    write_spot_store,
)

TEN_MINUTES = 10 * 60 * 1000


def _clock(ms: int):
    return lambda: from_epoch_ms(ms)


@pytest.fixture
def research(tmp_path: Path) -> Path:
    return write_research_store(tmp_path / "autoresearch.sqlite", hump_series())


def _signal(research: Path, out: Path, *extra: str, coin: str = COIN) -> list[str]:
    return ["signal", "--coin", coin, "--out", str(out), "--research-db", str(research), *extra]


def _two_stores(
    tmp_path: Path,
    *,
    perp_equity: str,
    spot_equity: str = "10000",
    perp_at: str = "2026-02-10T20:00:00+00:00",
) -> list[str]:
    """The four store flags, over a perp and a spot store holding one equity row each."""
    perp = write_perp_store(tmp_path / "paper.db", [(perp_at, "carry-ETH-1", perp_equity)])
    spot = write_spot_store(
        tmp_path / "uniswap.db", [("paper-carry-1", day(40) // 1000, spot_equity)]
    )
    perp_flags = ["--perp-db", str(perp), "--perp-run-id", "carry-ETH-1"]
    spot_flags = ["--spot-db", str(spot), "--spot-run-id", "paper-carry-1"]
    return [*perp_flags, *spot_flags]


def test_the_module_entry_hands_argv_to_main():
    assert entry.main is main


# --- history ----------------------------------------------------------------


def test_history_prints_the_summary(research: Path, capsys):
    assert main(["history", "--coin", COIN, "--research-db", str(research)]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith("carry history: ETH, 59 boundaries from 2026-01-31T00:00:00+00:00")
    assert "entries 1, exits 1; longest hold 15 days" in out[2]


def test_history_rows_list_the_entries_and_exits(research: Path, capsys):
    assert main(["history", "--coin", COIN, "--research-db", str(research), "--rows"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[4].startswith("  2026-02-11T00:00:00+00:00 enter z ")
    assert out[5].startswith("  2026-02-26T00:00:00+00:00 exit  z ")
    assert len(out) == 6


def test_history_refuses_an_empty_store(tmp_path: Path, capsys):
    empty = write_research_store(tmp_path / "autoresearch.sqlite", [])
    assert main(["history", "--coin", COIN, "--research-db", str(empty)]) == 1
    err = capsys.readouterr().err
    assert "fetch it first" in err and f"--db {empty}" in err


def test_history_refuses_a_bad_parameter(research: Path, capsys):
    assert main(["history", "--coin", COIN, "--research-db", str(research), "--z-out", "2"]) == 1
    assert "z_out must be below z_in" in capsys.readouterr().err


def test_history_refuses_a_bad_instant(research: Path, capsys):
    argv = ["history", "--coin", COIN, "--research-db", str(research), "--since", "yesterday"]
    assert main(argv) == 1
    assert "--since: not an ISO-8601" in capsys.readouterr().err


# --- the boundary -----------------------------------------------------------


def test_the_default_boundary_is_the_next_midnight_or_within_the_grace_the_last_one():
    assert default_boundary(day(41) - TEN_MINUTES) == day(41)
    assert default_boundary(day(41)) == day(41)
    assert default_boundary(day(41) + 30 * 1000) == day(41)
    assert default_boundary(day(41) + GRACE_HOURS * MS_PER_HOUR - 1) == day(41)
    assert default_boundary(day(41) + GRACE_HOURS * MS_PER_HOUR) == day(42)
    assert default_boundary(day(42) - 1) == day(42)


def test_signal_run_just_after_midnight_decides_that_midnight(research: Path, tmp_path, capsys):
    out = tmp_path / "carry-eth.json"
    assert main(_signal(research, out, "--no-fetch"), now=_clock(day(41) + 5 * 60 * 1000)) == 0
    assert capsys.readouterr().out.splitlines()[0].endswith("as of 2026-02-11T00:00:00+00:00")
    assert read_handoff(out).as_of_ms == day(41)


def test_signal_warns_when_a_boundary_was_skipped(research: Path, tmp_path, capsys):
    out = tmp_path / "carry-eth.json"
    assert main(_signal(research, out, "--no-fetch"), now=_clock(day(41) - TEN_MINUTES)) == 0
    capsys.readouterr()
    assert main(_signal(research, out, "--no-fetch"), now=_clock(day(43) - TEN_MINUTES)) == 0
    err = capsys.readouterr().err
    assert "no handoff was written for 2026-02-12T00:00:00+00:00" in err
    assert "the venues ran on the one for 2026-02-11T00:00:00+00:00" in err
    assert read_handoff(out).as_of_ms == day(43)


# --- signal -----------------------------------------------------------------


def test_signal_writes_the_handoff_and_remembers_its_position(research: Path, tmp_path, capsys):
    out = tmp_path / "carry-eth.json"
    now = day(41) - TEN_MINUTES
    assert main(_signal(research, out, "--no-fetch"), now=_clock(now)) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "carry signal: ETH as of 2026-02-11T00:00:00+00:00"
    assert lines[1].startswith("  funding: 0.00004500/h at 2026-02-10T23:00:00+00:00 (")
    assert "last-day mean over 24 settlements" in lines[1]
    assert lines[2] == "  position: out -> in (enter); in since 2026-02-11T00:00:00+00:00"
    assert lines[3] == (
        "  targets: perp short 30% margin; spot WETH weight 0.3000 "
        "(perp equity unknown, spot equity unknown)"
    )
    handoff = read_handoff(out)
    assert handoff.action is Action.ENTER
    assert handoff.position == Position(Side.IN, day(41))
    assert handoff.written_at_ms == now
    assert handoff.reading is not None
    assert handoff.reading.z is not None and handoff.reading.z > 1.5

    # The next day reads the file it wrote: in, held since day 41, so it holds.
    assert main(_signal(research, out, "--no-fetch"), now=_clock(day(42) - TEN_MINUTES)) == 0
    assert "  position: in -> in (hold); in since 2026-02-11T00:00:00+00:00" in (
        capsys.readouterr().out
    )
    assert read_handoff(out).position == Position(Side.IN, day(41))

    # And the day after the hump has left the current settlement, it exits.
    assert main(_signal(research, out, "--no-fetch"), now=_clock(day(56) - TEN_MINUTES)) == 0
    exited = read_handoff(out)
    assert exited.action is Action.EXIT
    assert exited.position.side is Side.OUT
    assert exited.margin_pct == 0 and exited.spot_weight == 0


def test_signal_reruns_a_decided_boundary_without_deciding_again(tmp_path, capsys):
    # The first run sees the store without day 40's last two settlements and decides on
    # the 21:00 one; then those settlements land and the boundary is rerun.
    points = hump_series()
    early = [p for p in points if p.time < day(41) - 2 * MS_PER_HOUR]
    research = write_research_store(tmp_path / "autoresearch.sqlite", early)
    out = tmp_path / "carry-eth.json"
    argv = _signal(research, out, "--no-fetch", "--min-hold-days", "0")
    assert main(argv, now=_clock(day(41) - TEN_MINUTES)) == 0
    first = read_handoff(out)
    assert first.action is Action.ENTER
    assert first.reading is not None and first.reading.at_ms == day(41) - 3 * MS_PER_HOUR
    capsys.readouterr()
    write_research_store(research, points)
    # Rerun (after a crash past the write, or to refresh the sizing): the decision stands
    # with the reading it was made from (re-deciding from the position it left would read
    # the rule against its own outcome), the sizing is recomputed from the stores given
    # now, and different rule params are a warning, not a refusal.
    stores = _two_stores(tmp_path, perp_equity="20000")
    assert main([*argv, *stores, "--margin-pct", "40"], now=_clock(day(41) - 1)) == 0
    captured = capsys.readouterr()
    lines = captured.out.splitlines()
    assert lines[1].startswith("  funding: 0.00004500/h at 2026-02-10T21:00:00+00:00 (")
    assert lines[2].startswith("  current (not the decision's): 0.00004500/h at 2026-02-10T23:00")
    assert "(enter; this boundary was already decided, sizing refreshed)" in lines[3]
    assert "rerun with different rule parameters" in captured.err
    again = read_handoff(out)
    assert again.action is Action.ENTER and again.position == first.position
    assert again.reading == first.reading
    assert again.margin_pct == 40 and again.spot_weight == Decimal("0.8000")


def test_signal_refuses_a_boundary_older_than_its_memory(research: Path, tmp_path, capsys):
    out = tmp_path / "carry-eth.json"
    assert main(_signal(research, out, "--no-fetch"), now=_clock(day(41) - TEN_MINUTES)) == 0
    before = out.read_bytes()
    capsys.readouterr()
    argv = _signal(research, out, "--no-fetch", "--as-of", "2026-02-10")
    assert main(argv, now=_clock(day(41))) == 1
    assert "later than 2026-02-10T00:00:00+00:00; the venues may have acted" in (
        capsys.readouterr().err
    )
    assert out.read_bytes() == before


def test_signal_refuses_another_coins_handoff_at_out(research: Path, tmp_path, capsys):
    out = tmp_path / "carry.json"
    assert main(_signal(research, out, "--no-fetch"), now=_clock(day(41) - TEN_MINUTES)) == 0
    before = out.read_bytes()
    capsys.readouterr()
    argv = _signal(research, out, "--no-fetch", coin="BTC")
    assert main(argv, now=_clock(day(42) - TEN_MINUTES)) == 1
    assert "is ETH's handoff, not BTC's" in capsys.readouterr().err
    assert out.read_bytes() == before


def test_signal_sizes_the_spot_leg_from_the_two_stores(research: Path, tmp_path, capsys):
    out = tmp_path / "carry-eth.json"
    argv = _signal(research, out, "--no-fetch", *_two_stores(tmp_path, perp_equity="20000"))
    assert main(argv, now=_clock(day(41) - TEN_MINUTES)) == 0
    captured = capsys.readouterr()
    assert "spot WETH weight 0.6000 (perp equity 20000, spot equity 10000)" in captured.out
    assert captured.err == ""
    handoff = read_handoff(out)
    assert handoff.spot_weight == Decimal("0.6000")
    assert handoff.equity_perp is not None and handoff.equity_perp.value == Decimal("20000")
    assert handoff.equity_perp.at_ms == day(40) + 20 * MS_PER_HOUR


def test_signal_warns_when_an_equity_snapshot_is_old(research: Path, tmp_path, capsys):
    out = tmp_path / "carry-eth.json"
    stores = _two_stores(tmp_path, perp_equity="20000", perp_at="2026-02-01T00:00:00+00:00")
    assert main(_signal(research, out, "--no-fetch", *stores), now=_clock(day(41) - 1)) == 0
    err = capsys.readouterr().err
    assert "perp equity 20000 was written 240 hours ago" in err
    assert "is run 'carry-ETH-1' still running?" in err
    assert read_handoff(out).spot_weight == Decimal("0.6000")


def test_signal_refuses_a_run_without_an_equity_row(research: Path, tmp_path, capsys):
    out = tmp_path / "carry-eth.json"
    stores = _two_stores(tmp_path, perp_equity="20000")
    stores[3] = "carry-eth-1"  # the run id, mistyped
    assert main(_signal(research, out, "--no-fetch", *stores), now=_clock(day(41))) == 1
    assert "run 'carry-eth-1' has no equity row" in capsys.readouterr().err
    assert not out.exists()


def test_signal_warns_and_still_writes_when_a_leg_has_no_equity(research, tmp_path, capsys):
    out = tmp_path / "carry-eth.json"
    argv = _signal(research, out, "--no-fetch", *_two_stores(tmp_path, perp_equity="0"))
    assert main(argv, now=_clock(day(41) - TEN_MINUTES)) == 0
    captured = capsys.readouterr()
    assert "WARNING: spot weight is 0 while in: a leg has no equity" in captured.err
    handoff = read_handoff(out)
    assert handoff.position.side is Side.IN and handoff.spot_weight == 0
    assert handoff.equity_perp is not None and handoff.equity_perp.value == 0


def test_signal_names_a_rounding_zero_differently(research: Path, tmp_path, capsys):
    out = tmp_path / "carry-eth.json"
    stores = _two_stores(tmp_path, perp_equity="1", spot_equity="100000")
    assert main(_signal(research, out, "--no-fetch", *stores), now=_clock(day(41) - 1)) == 0
    assert "the perp notional rounds to nothing of the spot" in capsys.readouterr().err


def test_signal_fetches_the_window_through_the_venue(tmp_path: Path, capsys):
    research = write_research_store(tmp_path / "autoresearch.sqlite", [])
    market = FakeMarket(hump_series())
    out = tmp_path / "carry-eth.json"
    now = day(41) - TEN_MINUTES
    assert main(_signal(research, out), market_factory=lambda: market, now=_clock(now)) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[1].startswith("  fetched: ETH funding: ")
    assert "stopped because reached the requested end" in lines[1]
    assert market.calls and all(coin == COIN for coin, _, _ in market.calls)
    # The window plus two days of slack, up to the clock: nothing from the future.
    with ResearchStore(research) as store:
        stored = list(store.iter_funding(COIN))
    assert stored[0].time == day(41 - 32)
    assert stored[-1].time <= now
    assert read_handoff(out).action is Action.ENTER


def test_signal_refuses_a_walk_that_did_not_reach_the_end(research, tmp_path, capsys, monkeypatch):
    """A walk the request limit ended covers less than it was asked for, and that is no decision."""
    from types import SimpleNamespace

    from contrib.carry import cli

    def cut_short(market, store, *, coin, since, end):
        return SimpleNamespace(
            label=f"{coin} funding", pages=4, rows_written=96, rows_before=2160,
            rows_after=2160, rows_added=0, stopped=StopReason.PAGE_LIMIT,
        )  # fmt: skip

    monkeypatch.setattr(cli, "backfill_funding", cut_short)
    out = tmp_path / "carry-eth.json"
    argv = _signal(research, out)
    assert main(argv, market_factory=lambda: FakeMarket([]), now=_clock(day(41) - 1)) == 1
    captured = capsys.readouterr()
    assert "stopped because hit the request limit" in captured.out
    assert "did not reach the end of the window" in captured.err
    assert not out.exists()


def test_signal_refuses_a_stale_reading(tmp_path: Path, capsys):
    """A store that missed a day must not decide; the handoff is left for the readers to age."""
    gapped = [p for p in hump_series() if p.time < day(40) + 12 * MS_PER_HOUR]
    research = write_research_store(tmp_path / "autoresearch.sqlite", gapped)
    out = tmp_path / "carry-eth.json"
    assert main(_signal(research, out, "--no-fetch"), now=_clock(day(41) - 1)) == 1
    err = capsys.readouterr().err
    assert (
        "is 13.0 hours before 2026-02-11T00:00:00+00:00, more than --max-reading-age-hours 3" in err
    )
    assert not out.exists()
    argv = _signal(research, out, "--no-fetch", "--max-reading-age-hours", "24")
    assert main(argv, now=_clock(day(41) - 1)) == 0
    assert read_handoff(out).reading is not None


def test_signal_refuses_a_boundary_that_is_not_midnight(research: Path, tmp_path, capsys):
    out = tmp_path / "carry-eth.json"
    argv = _signal(research, out, "--no-fetch", "--as-of", "2026-02-11T04:00:00+00:00")
    assert main(argv, now=_clock(day(41))) == 1
    assert "is not a UTC day boundary" in capsys.readouterr().err
    assert not out.exists()


def test_signal_accepts_an_explicit_boundary_as_a_date_or_an_instant(research, tmp_path):
    out = tmp_path / "carry-eth.json"
    for spelled in ("2026-02-11", "2026-02-11T00:00:00Z", "2026-02-11T00:00:00+00:00"):
        argv = _signal(research, out, "--no-fetch", "--as-of", spelled)
        assert main(argv, now=_clock(day(41))) == 0
        assert read_handoff(out).as_of_ms == day(41)
        out.unlink()


def test_signal_refuses_a_naive_instant(research: Path, tmp_path, capsys):
    out = tmp_path / "carry-eth.json"
    argv = _signal(research, out, "--no-fetch", "--as-of", "2026-02-11T00:00:00")
    assert main(argv, now=_clock(day(41))) == 1
    assert "needs a timezone" in capsys.readouterr().err


def test_signal_refuses_when_nothing_settled_before_the_boundary(tmp_path: Path, capsys):
    later = write_research_store(tmp_path / "autoresearch.sqlite", hourly(day(50), alternating(2)))
    out = tmp_path / "carry-eth.json"
    assert main(_signal(later, out, "--no-fetch"), now=_clock(day(41) - TEN_MINUTES)) == 1
    assert "no ETH settlement before 2026-02-11T00:00:00+00:00" in capsys.readouterr().err
    assert not out.exists()


def test_signal_refuses_half_a_store_argument(research: Path, tmp_path, capsys):
    out = tmp_path / "carry-eth.json"
    argv = _signal(research, out, "--no-fetch", "--perp-db", "paper.db")
    assert main(argv, now=_clock(day(41))) == 1
    assert "--perp-db and --perp-run-id go together" in capsys.readouterr().err
    assert not out.exists()


def test_signal_refuses_an_out_directory_that_does_not_exist(research: Path, tmp_path, capsys):
    out = tmp_path / "nowhere" / "carry-eth.json"
    assert main(_signal(research, out, "--no-fetch"), now=_clock(day(41))) == 1
    assert "the directory does not exist" in capsys.readouterr().err


def test_a_missing_research_store_is_refused_rather_than_created(tmp_path: Path, capsys):
    missing = tmp_path / "typo" / "autoresearch.sqlite"
    out = tmp_path / "carry-eth.json"
    assert main(_signal(missing, out, "--no-fetch"), now=_clock(day(41))) == 1
    assert "no such store" in capsys.readouterr().err
    assert main(["history", "--coin", COIN, "--research-db", str(missing)]) == 1
    assert "no such store" in capsys.readouterr().err
    assert not missing.exists() and not missing.parent.exists()


def test_signal_refuses_a_spot_run_in_the_wrong_quote(research: Path, tmp_path, capsys):
    spot = write_spot_store(
        tmp_path / "uniswap.db", [("paper-carry-1", day(40) // 1000, "5")], quote="WETH"
    )
    out = tmp_path / "carry-eth.json"
    argv = _signal(
        research, out, "--no-fetch", "--spot-db", str(spot), "--spot-run-id", "paper-carry-1"
    )
    assert main(argv, now=_clock(day(41))) == 1
    assert "quoted in 'WETH', not a USD stable" in capsys.readouterr().err
    assert not out.exists()


def test_history_leaves_stale_boundaries_undecided(tmp_path: Path, capsys):
    gapped = [p for p in hump_series() if not (day(44) - 20 * MS_PER_HOUR < p.time < day(46))]
    research = write_research_store(tmp_path / "autoresearch.sqlite", gapped)
    assert main(["history", "--coin", COIN, "--research-db", str(research)]) == 0
    assert "with a reading older than 3h, left undecided as live would: 3" in (
        capsys.readouterr().out
    )
    argv = [
        "history",
        "--coin",
        COIN,
        "--research-db",
        str(research),
        "--max-reading-age-hours",
        "48",
    ]
    assert main(argv) == 0
    assert "left undecided as live would: 1" in capsys.readouterr().out


def test_signal_refuses_a_bad_reading_age(research: Path, tmp_path, capsys):
    out = tmp_path / "carry-eth.json"
    argv = _signal(research, out, "--no-fetch", "--max-reading-age-hours", "0")
    assert main(argv, now=_clock(day(41))) == 1
    assert "--max-reading-age-hours must be at least 1" in capsys.readouterr().err


def test_signal_refuses_a_store_it_cannot_read(research: Path, tmp_path, capsys):
    out = tmp_path / "carry-eth.json"
    nowhere = str(tmp_path / "nowhere.db")
    argv = _signal(research, out, "--no-fetch", "--spot-db", nowhere, "--spot-run-id", "r")
    assert main(argv, now=_clock(day(41))) == 1
    assert "spot store" in capsys.readouterr().err and not out.exists()


def test_signal_refuses_a_previous_handoff_it_cannot_read(research: Path, tmp_path, capsys):
    out = tmp_path / "carry-eth.json"
    out.write_text(json.dumps({"version": 7}), encoding="utf-8")
    assert main(_signal(research, out, "--no-fetch"), now=_clock(day(41))) == 1
    assert "version 7" in capsys.readouterr().err
    assert json.loads(out.read_text(encoding="utf-8")) == {"version": 7}


def test_signal_refuses_to_fetch_a_window_that_lies_in_the_future(research, tmp_path, capsys):
    out = tmp_path / "carry-eth.json"
    argv = _signal(research, out, "--as-of", "2030-01-01")
    assert main(argv, market_factory=lambda: FakeMarket([]), now=_clock(DAY0)) == 1
    assert "too far ahead to fetch" in capsys.readouterr().err


def test_signal_reports_a_venue_refusal_as_a_sentence(research: Path, tmp_path, capsys):
    class Refusing:
        def get_funding_history(self, coin, window_days, *, end):
            raise ExchangeError("429 throttled")

    out = tmp_path / "carry-eth.json"
    assert main(_signal(research, out), market_factory=Refusing, now=_clock(day(41))) == 1
    assert "throttled" in capsys.readouterr().err
    assert not out.exists()
