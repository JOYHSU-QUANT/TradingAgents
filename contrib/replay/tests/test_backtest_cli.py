"""``python -m contrib.replay backtest`` end to end: the paths, the bar, the store, the refusals.

Two fixtures: the scorecard's eleven-question table (``conftest.ROWS``) for
the paper trader and the controls, and the gate fixture (``papers.py``) for
a variant replayed by the echo model. Both get a research store whose bars
cover every question — the maker fill model needs the bar each decision
falls in — and whose funding runs hourly across the span.
"""

from __future__ import annotations

import csv
import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest

from contrib.hyperliquid_perp.domains.perp.schema import Candle, FundingPoint
from contrib.replay import cli
from contrib.replay.replay_store import SCHEMA_VERSION, ReplayStore
from contrib.replay.upstream import ResearchStore, from_epoch_ms, payload_dir

from .conftest import (
    ANCHOR_MS,
    COIN,
    ROWS,
    RUN_ID,
    STEP_MS,
    at_ms,
    downgrade_to_v3,
    run_config,
    write_paper_store,
)
from .papers import (
    FORMAT,
    PAPERS,
    POSITION_SECTION,
    RUN_ID as GATE_RUN_ID,
    Echo,
    EchoGraph,
    candle_end_ms,
    input_id as gate_input_id,
    question_line,
    replay_argv,
    write_gate_store,
    write_graph_variant,
    write_variant,
)

HOUR = 3_600_000
# The questions are decided one millisecond before each bar boundary
# (``conftest.at_ms``): the last instant of the bar closing there, which is
# the bar the split places them in and the bar a maker post is judged
# against; its close is the question's own mark, so the research closes
# agree with the store's marks at every question.
MARKS = {row.slot: Decimal(row.mark) for row in ROWS}
GATE_MARKS = {paper.slot: Decimal(paper.mark) for paper in PAPERS}


def write_research(path: Path, marks: dict[int, Decimal], *, slots: int = 16) -> Path:
    """Bars from one before the first question to past the last, hourly funding throughout."""
    candles = []
    last = next(iter(marks.values()))
    for slot in range(-1, slots):
        close = marks.get(slot + 1, last)
        last = close
        candles.append(
            Candle(
                open_time=ANCHOR_MS + slot * STEP_MS,
                close_time=ANCHOR_MS + (slot + 1) * STEP_MS - 1,
                open=close,
                high=close + 3,
                low=close - 3,
                close=close,
                volume=Decimal(1),
            )
        )
    first_hour = ANCHOR_MS - STEP_MS
    funding = [
        FundingPoint(time=first_hour + h * HOUR + 11, rate=Decimal("0.0001"))
        for h in range((slots + 1) * STEP_MS // HOUR)
    ]
    with ResearchStore(path) as store:
        store.upsert_candles(COIN, "4h", candles)
        store.upsert_funding(COIN, funding)
    return path


@pytest.fixture
def research(tmp_path: Path) -> Path:
    return write_research(tmp_path / "autoresearch.sqlite", MARKS)


@pytest.fixture
def store(tmp_path: Path) -> Path:
    return write_paper_store(
        tmp_path / "paper_trading.db", payload_root=tmp_path / "p", config=run_config(gate=True)
    )


def _backtest(store: Path, research: Path, *extra: str, run_id: str = RUN_ID) -> list[str]:
    return [
        "backtest",
        "--db",
        str(store),
        "--run-id",
        run_id,
        "--research-db",
        str(research),
        *extra,
    ]


def _section(lines: list[str], start: str) -> list[str]:
    index = lines.index(start)
    end = next((i for i in range(index + 1, len(lines)) if lines[i].startswith("-- ")), len(lines))
    return lines[index:end]


def test_the_paper_trader_and_the_controls_are_walked_and_the_bar_is_the_smoke_test(
    store, research, capsys
):
    assert cli.main(_backtest(store, research)) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0] == (
        f"backtest: run {RUN_ID} (BTC, 4h cycle; costs and interval from the run's recorded config)"
    )
    assert out[1].startswith("account: opens at 1000 USDC; taker fills at 0.00045 fee")
    for name in ("buy_hold", "flat", "research_bias", "paper"):
        section = _section(out, f"-- {name} --")
        assert section[1] == f"  run {RUN_ID}:"
        # Nine questions walked: train and validation, the holdout left out.
        assert section[2].startswith("    9 question(s) walked")
        assert any(line.startswith("  pooled: train sharpe ") for line in section)
    flat = _section(out, "-- flat --")
    assert "0 order(s), 0 fill(s)" in flat[2]
    assert "equity 1000 -> 1000.00 (+0.00%)" in flat[3]
    held = _section(out, "-- buy_hold --")
    assert "1 order(s), 1 fill(s)" in held[2]
    paper = _section(out, "-- paper --")
    # The gate approved a target at slots 0, 2, 5, 7 and 9; slot 7 was
    # inside PAPER's deadband, but the account is flat there (slot 5
    # closed it), so it is an order here.
    assert "5 order(s), 5 fill(s)" in paper[2]
    assert (
        "smoke test: no --variant, so the bar judges the paper trader against itself; "
        "checks (1) and (3) cannot pass and (4) cannot fail"
    ) in out
    bar = _section(out, "-- plan section 5 bar: paper on the validation segment --")
    assert bar[1].startswith("  (1) net sharpe above every control: paper ")
    assert bar[1].endswith("-> FAIL")
    assert "1 variant repeat(s) tried in the store" in bar[2]
    assert "paper only 0, McNemar p 1.000" in bar[3] and bar[3].endswith("-> FAIL")
    assert bar[4].endswith("-> pass")
    assert bar[5] == "  verdict: paper does not pass; the holdout stays locked"


def test_a_maker_run_posts_at_the_tick_and_reports_what_rested(tmp_path, research, capsys):
    maker = write_paper_store(
        tmp_path / "maker.db",
        payload_root=tmp_path / "p",
        config=run_config(style="maker", gate=True),
    )
    assert cli.main(_backtest(maker, research, "--tick", "1")) == 0
    out = capsys.readouterr().out.splitlines()
    assert "maker fills at 0.00015 fee, posted at mark -/+ 0.5 bps on a 1 tick" in out[1]
    paper = _section(out, "-- paper --")
    # The bars range +/-3 around each close and a post rests one tick off
    # the mark, so every post is reached within its bar: five orders, five
    # fills, nothing resting.
    assert "5 order(s), 5 fill(s), 0 post(s) left resting" in paper[2]
    # A coarser tick moves the post further from the mark and is printed.
    assert cli.main(_backtest(maker, research, "--tick", "5")) == 0
    out = capsys.readouterr().out.splitlines()
    assert "on a 5 tick" in out[1]
    # The run's own assumed half spread reaches the account from the genesis.
    wide = write_paper_store(
        tmp_path / "wide.db",
        payload_root=tmp_path / "p2",
        config=run_config(style="maker", half_spread_bps="20", gate=True),
    )
    assert cli.main(_backtest(wide, research)) == 0
    assert "posted at mark -/+ 20 bps" in capsys.readouterr().out.splitlines()[1]


def test_two_runs_pool_their_validation_returns_and_pin_their_splits(
    gate_store, gate_research, tmp_path, capsys
):
    other = "paper-GATE-2"
    write_gate_store(gate_store, run_id=other)
    assert cli.main(_gate_backtest(gate_store, gate_research)) == 0
    alone = capsys.readouterr().out.splitlines()
    assert cli.main(_gate_backtest(gate_store, gate_research, "--run-id", other)) == 0
    out = capsys.readouterr().out.splitlines()
    assert sum(1 for line in out if line.startswith("split: pinned ")) == 2
    assert out.count("paths walked on this run before this one: 4") == 1  # the first run, once
    assert out.count("paths walked on this run before this one: 0") == 1
    pooled_alone = next(line for line in _section(alone, "-- paper --") if "pooled:" in line)
    pooled_both = next(line for line in _section(out, "-- paper --") if "pooled:" in line)
    n_alone = int(pooled_alone.split("validation sharpe ")[1].split("(n ")[1].rstrip(")"))
    n_both = int(pooled_both.split("validation sharpe ")[1].split("(n ")[1].rstrip(")"))
    assert n_alone > 0 and n_both == 2 * n_alone
    with ReplayStore(gate_store.parent / "replay.sqlite") as replay:
        assert replay.pinned_split(GATE_RUN_ID) is not None
        assert replay.pinned_split(other) is not None
        assert len(replay.backtests(GATE_RUN_ID)) == 8 and len(replay.backtests(other)) == 4
        assert replay.looks(GATE_RUN_ID) == []  # no holdout, no look spent


def test_nothing_past_the_holdout_lock_is_read(store, tmp_path, capsys):
    research = write_research(tmp_path / "autoresearch.sqlite", MARKS)
    assert cli.main(_backtest(store, research)) == 0
    before = capsys.readouterr().out
    # Absurd prices and a wild funding rate in the holdout and after it: a
    # walk that stopped at the lock cannot see them.
    with ResearchStore(research) as rs:
        rs.upsert_candles(
            COIN,
            "4h",
            [
                Candle(
                    open_time=ANCHOR_MS + slot * STEP_MS,
                    close_time=ANCHOR_MS + (slot + 1) * STEP_MS - 1,
                    open=Decimal(5),
                    high=Decimal(1000),
                    low=Decimal(1),
                    close=Decimal(5),
                    volume=Decimal(1),
                )
                for slot in range(9, 16)
            ],
        )
        rs.upsert_funding(
            COIN, [FundingPoint(time=ANCHOR_MS + 9 * STEP_MS + 11, rate=Decimal("0.5"))]
        )
    assert cli.main(_backtest(store, research)) == 0
    assert capsys.readouterr().out == before


def test_funding_is_settled_from_the_research_store(store, research, capsys):
    assert cli.main(_backtest(store, research)) == 0
    out = capsys.readouterr().out.splitlines()
    held = _section(out, "-- buy_hold --")
    # Long at the cap from the first question at a positive rate: funding is paid.
    assert "funding -" in held[2]
    assert "missing" not in held[2]


def test_out_writes_one_row_per_walked_question_per_path(store, research, tmp_path, capsys):
    out_dir = tmp_path / "out"
    assert cli.main(_backtest(store, research, "--out", str(out_dir))) == 0
    captured = capsys.readouterr()
    table = out_dir / f"{RUN_ID}-backtest-decisions.csv"
    summary = out_dir / f"{RUN_ID}-backtest-summary.txt"
    with table.open(encoding="utf-8", newline="") as fh:
        rows = list(csv.reader(fh))
    assert rows[0][:4] == ["run_id", "driver", "repeat", "input_id"]
    assert rows[0][-1] == "return"
    assert len(rows) == 1 + 4 * 9
    assert {row[1] for row in rows[1:]} == {"buy_hold", "flat", "research_bias", "paper"}
    assert summary.read_text(encoding="utf-8") == captured.out
    assert f"wrote {table}" in captured.err


def test_holdout_walks_every_question_creates_the_store_and_records_the_look(
    store, research, tmp_path, capsys
):
    replay_db = tmp_path / "looks.sqlite"
    assert cli.main(_backtest(store, research, "--holdout", "--replay-db", str(replay_db))) == 0
    out = capsys.readouterr().out.splitlines()
    assert "HOLDOUT WALKED: this look is recorded in the ledger" in out
    paper = _section(out, "-- paper --")
    assert paper[2].startswith("    11 question(s) walked")
    pooled = next(line for line in paper if line.startswith("  pooled: "))
    assert "; holdout sharpe " in pooled
    with ReplayStore(replay_db) as replay:
        (look,) = replay.looks(RUN_ID)
        assert (look.action, look.variant_name, look.questions) == ("score", "paper", 2)
        walked = replay.backtests(RUN_ID)
    assert [w.driver for w in walked] == ["buy_hold", "flat", "research_bias", "paper"]
    assert all(w.variant_name is None and w.repeat is None for w in walked)


# -- with a variant ---------------------------------------------------------------------------


@pytest.fixture
def gate_store(tmp_path: Path) -> Path:
    return write_gate_store(tmp_path / "paper_trading.db")


@pytest.fixture
def gate_research(tmp_path: Path) -> Path:
    return write_research(tmp_path / "autoresearch.sqlite", GATE_MARKS, slots=12)


def _replay_db(store: Path) -> Path:
    """The replay store beside the paper store, created empty if the replay command has not."""
    path = store.parent / "replay.sqlite"
    if not path.exists():
        with ReplayStore(path, create=True):
            pass
    return path


def _gate_backtest(store: Path, research: Path, *extra: str) -> list[str]:
    return _backtest(
        store, research, "--replay-db", str(_replay_db(store)), *extra, run_id=GATE_RUN_ID
    )


def test_a_variants_repeats_are_walked_and_judged_and_every_path_is_stored(
    gate_store, gate_research, tmp_path, monkeypatch, capsys
):
    variant_file = write_variant(tmp_path, "v")
    monkeypatch.setattr(cli, "_build_model", lambda _variant: Echo())
    for segment in ("train", "validation"):
        argv = replay_argv(gate_store, variant_file, "--repeats", "2", "--segment", segment)
        assert cli.main(argv) == 0
    capsys.readouterr()
    assert cli.main(_gate_backtest(gate_store, gate_research, "--variant", "v")) == 0
    out = capsys.readouterr().out.splitlines()
    assert any(line.startswith("split: pinned ") for line in out)
    for repeat in (0, 1):
        section = _section(out, f"-- v repeat {repeat} --")
        assert section[2].startswith("    8 question(s) walked")  # train 6 + validation 2
        bar = _section(
            out, f"-- plan section 5 bar: v repeat {repeat} on the validation segment --"
        )
        assert "2 variant repeat(s) tried in the store" in bar[2]  # two repeats, one variant
        # The echo says what the paper trader said, so no paired question
        # is discordant and the fail-closed rates are equal.
        assert "McNemar p 1.000" in bar[3] and bar[3].endswith("-> FAIL")
        assert bar[4].endswith("-> pass")
    # The echo's path is the paper trader's path, order for order.
    paper = _section(out, "-- paper --")
    echo = _section(out, "-- v repeat 0 --")
    assert paper[2:4] == echo[2:4]
    with ReplayStore(_replay_db(gate_store)) as replay:
        walked = replay.backtests(GATE_RUN_ID)
        assert replay.trials() == 2
    assert [(w.driver, w.variant_name, w.repeat) for w in walked] == [
        ("buy_hold", None, None),
        ("flat", None, None),
        ("research_bias", None, None),
        ("paper", None, None),
        ("variant", "v", 0),
        ("variant", "v", 1),
    ]
    assert walked[3].final_equity == walked[4].final_equity


def test_holdout_with_a_variant_needs_the_bar_and_spends_no_look_otherwise(
    gate_store, gate_research, tmp_path, monkeypatch, capsys
):
    variant_file = write_variant(tmp_path, "v")
    monkeypatch.setattr(cli, "_build_model", lambda _variant: Echo())
    assert cli.main(replay_argv(gate_store, variant_file, "--repeats", "1")) == 0
    # Asked the holdout too (the replay's own ask look), so the bar is what refuses.
    holdout_argv = replay_argv(gate_store, variant_file, "--repeats", "1", "--segment", "holdout")
    assert cli.main([*holdout_argv, "--holdout"]) == 0
    capsys.readouterr()
    # The echo says what paper said: checks (1) and (3) cannot pass, so the
    # holdout stays locked and nothing is spent or stored by the backtest.
    assert cli.main(_gate_backtest(gate_store, gate_research, "--variant", "v", "--holdout")) == 1
    err = capsys.readouterr().err
    assert "v repeat 0: not every check of the plan section 5 bar passes" in err
    assert "no look is spent" in err
    with ReplayStore(_replay_db(gate_store)) as replay:
        assert [look.action for look in replay.looks(GATE_RUN_ID)] == ["ask"]  # the replay's
        assert replay.backtests(GATE_RUN_ID) == []
    # The paper trader's own holdout is not gated, and its look names paper.
    # The verdict printed is the one read with the holdout unread: its
    # figures are the figures of a run without --holdout.
    assert cli.main(_gate_backtest(gate_store, gate_research)) == 0
    locked = capsys.readouterr().out.splitlines()
    assert cli.main(_gate_backtest(gate_store, gate_research, "--holdout")) == 0
    out = capsys.readouterr().out.splitlines()
    assert "HOLDOUT WALKED: this look is recorded in the ledger" in out
    assert out[-1].endswith("the holdout above was walked on the paper trader's look")
    bar_locked = _section(locked, "-- plan section 5 bar: paper on the validation segment --")
    bar_walked = _section(out, "-- plan section 5 bar: paper on the validation segment --")
    assert bar_walked[1:5] == bar_locked[1:5]
    assert any(line.startswith("the bar below was read with the holdout unread") for line in out)
    with ReplayStore(_replay_db(gate_store)) as replay:
        look = replay.looks(GATE_RUN_ID)[-1]
        assert (look.action, look.variant_name) == ("score", "paper")


def test_check_four_reads_paper_over_the_questions_the_repeat_was_asked(
    gate_store, gate_research, tmp_path, monkeypatch, capsys
):
    variant_file = write_variant(tmp_path, "v")
    monkeypatch.setattr(cli, "_build_model", lambda _variant: Echo())
    assert cli.main(replay_argv(gate_store, variant_file, "--repeats", "1")) == 0
    # One of the two validation questions asked: check (4) reads paper over
    # that one question too, not over both.
    argv = replay_argv(gate_store, variant_file, "--repeats", "1", "--segment", "validation")
    assert cli.main([*argv, "--limit", "1"]) == 0
    capsys.readouterr()
    assert cli.main(_gate_backtest(gate_store, gate_research, "--variant", "v")) == 0
    out = capsys.readouterr().out.splitlines()
    bar = _section(out, "-- plan section 5 bar: v repeat 0 on the validation segment --")
    # The one question asked (slot 6, prose) is a fail-closed round for both.
    assert "v repeat 0 1/1 (100.0%) vs paper 1/1 (100.0%)" in bar[4]
    assert "n 1 (needs >= " not in bar[3]  # a fail-closed round has no reading to pair
    assert "n 0 (needs >= " in bar[3]


def test_a_refused_backtest_pins_nothing(gate_store, gate_research, tmp_path, monkeypatch, capsys):
    other = "paper-GATE-2"
    write_gate_store(gate_store, run_id=other)
    variant_file = write_variant(tmp_path, "v")
    monkeypatch.setattr(cli, "_build_model", lambda _variant: Echo())
    assert cli.main(replay_argv(gate_store, variant_file, "--repeats", "1")) == 0
    capsys.readouterr()
    # v was never asked on the second run: refused before anything is pinned or stored.
    argv = _gate_backtest(gate_store, gate_research, "--run-id", other, "--variant", "v")
    assert cli.main(argv) == 1
    assert f"has no answers for run {other!r}" in capsys.readouterr().err
    with ReplayStore(_replay_db(gate_store)) as replay:
        assert replay.pinned_split(other) is None
        assert replay.backtests(GATE_RUN_ID) == [] and replay.backtests(other) == []


def test_a_repeat_asked_on_one_run_is_judged_against_that_runs_controls(
    gate_store, gate_research, tmp_path, monkeypatch, capsys
):
    other = "paper-GATE-2"
    write_gate_store(gate_store, run_id=other)
    variant_file = write_variant(tmp_path, "v")
    monkeypatch.setattr(cli, "_build_model", lambda _variant: Echo())
    assert cli.main(replay_argv(gate_store, variant_file, "--repeats", "2")) == 0
    argv = replay_argv(gate_store, variant_file, "--repeats", "1")
    argv[argv.index(GATE_RUN_ID)] = other
    assert cli.main(argv) == 0
    capsys.readouterr()
    both_runs = _gate_backtest(gate_store, gate_research, "--run-id", other, "--variant", "v")
    assert cli.main(both_runs) == 0
    out = capsys.readouterr().out.splitlines()
    assert sum(1 for line in out if line.startswith(f"run {GATE_RUN_ID}: model_cutoff")) == 1
    assert sum(1 for line in out if line.startswith(f"run {other}: model_cutoff")) == 1
    both = _section(out, "-- v repeat 0 --")
    alone = _section(out, "-- v repeat 1 --")
    assert f"  run {other}:" in both and f"  run {other}:" not in alone
    pooled_alone = next(line for line in alone if "pooled:" in line)
    figure, n_alone = pooled_alone.split("validation sharpe ")[1].split(" (n ")
    bar = _section(out, "-- plan section 5 bar: v repeat 1 on the validation segment --")
    # The controls of repeat 1 are pooled over the one run it was asked on:
    # the echo's path IS paper's, so paper's pooled figure is the repeat's.
    assert f"paper {figure} (n {n_alone.rstrip(')')})" in bar[1]
    # Trials are (variant, repeat) pairs store-wide: repeats 0 and 1, whichever runs asked them.
    assert "2 variant repeat(s) tried in the store" in bar[2]


def test_holdout_needs_the_repeat_to_have_been_asked_the_holdout_and_prints_coverage(
    gate_store, gate_research, tmp_path, monkeypatch, capsys
):
    variant_file = write_variant(tmp_path, "v")
    monkeypatch.setattr(cli, "_build_model", lambda _variant: Echo())
    assert cli.main(replay_argv(gate_store, variant_file, "--repeats", "1")) == 0
    capsys.readouterr()
    # Asked the train segment only: the two validation questions were not
    # asked, and the report says so per repeat and run.
    assert cli.main(_gate_backtest(gate_store, gate_research, "--variant", "v")) == 0
    out = capsys.readouterr().out.splitlines()
    assert (
        f"  run {GATE_RUN_ID}: asked 6 of 8 question(s) in scope (answered 6, refused 0, "
        "not asked 2; a refused or not-asked question holds the book)"
    ) in out
    # The holdout gate reads the holdout coverage before the bar: a repeat
    # never asked the holdout questions is refused with nothing spent.
    assert cli.main(_gate_backtest(gate_store, gate_research, "--variant", "v", "--holdout")) == 1
    err = capsys.readouterr().err
    assert "repeat 0 was not asked 2 of the 2 holdout question(s)" in err
    assert "no look is spent" in err
    # One of the two asked is still not every one: the look is irreversible.
    holdout_argv = replay_argv(gate_store, variant_file, "--repeats", "1", "--segment", "holdout")
    assert cli.main([*holdout_argv, "--holdout", "--limit", "1"]) == 0
    capsys.readouterr()
    assert cli.main(_gate_backtest(gate_store, gate_research, "--variant", "v", "--holdout")) == 1
    assert "repeat 0 was not asked 1 of the 2 holdout question(s)" in capsys.readouterr().err
    with ReplayStore(_replay_db(gate_store)) as replay:
        assert [look.action for look in replay.looks(GATE_RUN_ID)] == ["ask"]  # the replay's only


def test_a_holdout_walk_that_fails_spends_no_look(tmp_path, capsys):
    # A maker run whose research store stops before the holdout: the holdout
    # walk refuses before any look is recorded, and the store holds no path.
    maker = write_paper_store(
        tmp_path / "maker.db",
        payload_root=tmp_path / "p",
        config=run_config(style="maker", gate=True),
    )
    short = write_research(tmp_path / "short.sqlite", MARKS, slots=8)  # no bar for slot 9's order
    replay_db = tmp_path / "looks.sqlite"
    assert cli.main(_backtest(maker, short, "--holdout", "--replay-db", str(replay_db))) == 1
    captured = capsys.readouterr()
    assert "no bar in the research store covers" in captured.err
    assert "recorded in the ledger" not in captured.err
    assert captured.out == ""
    with ReplayStore(replay_db) as replay:
        assert replay.looks(RUN_ID) == []
        assert replay.backtests(RUN_ID) == []


def test_a_v3_store_gains_the_backtest_tables_when_the_command_opens_it(
    gate_store, gate_research, tmp_path, monkeypatch, capsys
):
    variant_file = write_variant(tmp_path, "v")
    monkeypatch.setattr(cli, "_build_model", lambda _variant: Echo())
    assert cli.main(replay_argv(gate_store, variant_file, "--repeats", "1")) == 0
    replay_db = _replay_db(gate_store)
    downgrade_to_v3(replay_db)
    capsys.readouterr()
    assert cli.main(_gate_backtest(gate_store, gate_research, "--variant", "v")) == 0
    conn = sqlite3.connect(replay_db)
    try:
        assert conn.execute("PRAGMA user_version").fetchone() == (SCHEMA_VERSION,)
        assert conn.execute("SELECT COUNT(*) FROM backtests").fetchone() == (5,)
    finally:
        conn.close()


def test_a_variant_with_no_answers_or_no_cutoff_is_refused(
    gate_store, gate_research, tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(cli, "_build_model", lambda _variant: Echo())
    asked = write_variant(tmp_path, "v")
    assert cli.main(replay_argv(gate_store, asked, "--repeats", "1")) == 0
    other = tmp_path / "other"
    other.mkdir()
    unasked = write_variant(other, "w", cutoff=None)
    replay_db = str(_replay_db(gate_store))
    assert cli.main(["register", "--variant", str(unasked), "--replay-db", replay_db]) == 0
    capsys.readouterr()
    assert cli.main(_gate_backtest(gate_store, gate_research, "--variant", "w")) == 1
    assert "'w' records no model_cutoff" in capsys.readouterr().err
    argv = _gate_backtest(gate_store, gate_research, "--variant", "w", "--include-pre-cutoff")
    assert cli.main(argv) == 1
    assert f"variant 'w' has no answers for run {GATE_RUN_ID!r}" in capsys.readouterr().err
    assert cli.main(_gate_backtest(gate_store, gate_research, "--variant", "nope")) == 1
    assert "no variant named 'nope'" in capsys.readouterr().err


# -- refusals ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        (("--variant", "v"), "--variant only apply with --replay-db"),
        (("--include-pre-cutoff",), "--include-pre-cutoff only apply with --replay-db"),
        (("--holdout",), "--holdout needs --replay-db PATH"),
        (("--replay-db", "missing.sqlite"), "--replay-db 'missing.sqlite' does not exist"),
        (("--replay-db", ""), "--replay-db needs a path, got ''"),
        (("--tick", "0"), "--tick must be > 0, got '0'"),
        (("--tick", "abc"), "--tick is a price, got 'abc'"),
        (("--out", ""), "--out needs a directory, got ''"),
        (("--run-id", RUN_ID), f"--run-id names {RUN_ID} more than once"),
    ],
)
def test_a_flag_pointing_nowhere_is_a_named_exit_1(
    store, research, tmp_path, monkeypatch, capsys, extra, message
):
    monkeypatch.chdir(tmp_path)
    assert cli.main(_backtest(store, research, *extra)) == 1
    captured = capsys.readouterr()
    assert f"error: {message}" in captured.err
    assert captured.out == ""
    assert not (tmp_path / "missing.sqlite").exists()


def test_a_missing_research_store_is_refused_before_anything_is_read(store, tmp_path, capsys):
    missing = tmp_path / "none.sqlite"
    assert cli.main(_backtest(store, missing)) == 1
    assert f"--research-db {str(missing)!r} does not exist" in capsys.readouterr().err
    assert not missing.exists()


def test_a_maker_run_whose_bars_the_store_lacks_is_refused_by_name(tmp_path, capsys):
    maker = write_paper_store(
        tmp_path / "maker.db",
        payload_root=tmp_path / "p",
        config=run_config(style="maker", gate=True),
    )
    empty = tmp_path / "empty.sqlite"
    with ResearchStore(empty):
        pass
    assert cli.main(_backtest(maker, empty)) == 1
    err = capsys.readouterr().err
    assert "no bar in the research store covers" in err
    assert "whether the maker post fills cannot be judged" in err


# -- backtest --ask (plan PR 3b) ---------------------------------------------------


@pytest.fixture
def graph_store(tmp_path: Path) -> Path:
    return write_gate_store(tmp_path / "paper_trading.db", reports=True)


def _ask_argv(store: Path, research: Path, variant_file: Path, *extra: str) -> list[str]:
    return _gate_backtest(store, research, "--ask", str(variant_file), *extra)


def _install_echo_graph(monkeypatch) -> list[EchoGraph]:
    built: list[EchoGraph] = []

    def build(variant):
        graph = EchoGraph(variant)
        built.append(graph)
        return graph

    monkeypatch.setattr(cli, "_build_graph", build)
    return built


def test_ask_walks_the_graph_variant_from_its_own_book_and_reads_the_bar_on_its_answers(
    graph_store, gate_research, tmp_path, monkeypatch, capsys
):
    variant_file = write_graph_variant(tmp_path, "g")
    built = _install_echo_graph(monkeypatch)
    assert cli.main(_ask_argv(graph_store, gate_research, variant_file, "--repeats", "2")) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0] == (
        f"ask: run {GATE_RUN_ID} (BTC, 4h cycle), train + validation: 8 question(s) x 2 "
        "repeat(s), the lower half of the graph from each cycle's recorded reports"
    )
    assert out[1].endswith(
        "graph mode: trader and risk debators on openrouter/fixture/g-quick, the engine's own "
        "portfolio manager prompt, guardrail recorded"
    )
    assert "asked: 16 new answer(s); already stored, skipped: 0" in out
    assert (
        "tokens reported: portfolio manager 1600 in, 160 out; whole graph 8000 in, 800 out over "
        "80 completion(s)"
    ) in out
    # Then the bar, as for any variant: the echo says what paper said, so it walks paper's path.
    paper = _section(out, "-- paper --")
    echo = _section(out, "-- g repeat 0 --")
    assert paper[2:4] == echo[2:4]
    (graph,) = built
    # Question by question, the repeats inside.
    firsts = [a["context_text"].splitlines()[0] for a in graph.asked[:4]]
    assert firsts == [question_line(0), question_line(0), question_line(1), question_line(1)]
    first, third = graph.asked[0]["context_text"], graph.asked[2]["context_text"]
    # Slot 0 is asked from a flat 1000 USDC account; slot 1 from the long slot 0 opened
    # (30% at 1x: 3 BTC at the post, marked at 102).
    assert first.splitlines()[-2:] == [
        "Position:",
        "  flat, no open position (account equity 1,000.00 USDC)",
    ]
    assert "  Side: long, size 3 BTC, notional 306.00 USDC at mark" in third
    assert POSITION_SECTION not in third
    assert graph.asked[0]["format_instructions"] == FORMAT
    assert graph.asked[0]["reports"].investment_plan == "plan 00"
    with ReplayStore(_replay_db(graph_store)) as replay:
        stored = replay.variant("g")
        assert (stored.mode, stored.quick_model, stored.guardrail) == (
            "graph",
            "fixture/g-quick",
            "recorded",
        )
        answers = replay.answers(stored.sha, GATE_RUN_ID)
        assert {repeat: len(rows) for repeat, rows in answers.items()} == {0: 8, 1: 8}
        books = replay.books(stored.sha, GATE_RUN_ID)
        assert books[0][gate_input_id(0)].size == 0
        assert books[0][gate_input_id(0)].last_fill_ms is None
        assert books[0][gate_input_id(1)].size == Decimal(3)
        assert books[0][gate_input_id(1)].last_fill_ms == at_ms(0)
        assert books[1][gate_input_id(1)].graph_input_tokens == 500
        assert replay.trials() == 2


def test_ask_stops_at_the_limit_and_resumes_against_the_book_it_left(
    graph_store, gate_research, tmp_path, monkeypatch, capsys
):
    variant_file = write_graph_variant(tmp_path, "g")
    _install_echo_graph(monkeypatch)
    argv = _ask_argv(graph_store, gate_research, variant_file, "--repeats", "2")
    assert cli.main([*argv, "--limit", "3"]) == 0
    out = capsys.readouterr().out
    assert "asked: 3 new answer(s); already stored, skipped: 0" in out
    assert "stopped at --limit" in out and "-- paper --" not in out  # the bar waits
    # A dry run now says what is left.
    assert cli.main([*argv, "--dry-run"]) == 0
    assert (
        "dry run: 3 answer(s) already stored, 13 to ask; this command would ask for 13 of them"
    ) in capsys.readouterr().out
    assert cli.main(argv) == 0
    out = capsys.readouterr().out
    assert "asked: 13 new answer(s); already stored, skipped: 3" in out
    assert "-- g repeat 1 --" in out
    # Under another tick the maker posts fill elsewhere, so the stored books no longer
    # rebuild: refused by name, nothing asked.
    assert cli.main([*argv, "--tick", "5"]) == 1
    err = capsys.readouterr().err
    assert "gate-in-01 repeat 0: the book it was asked from" in err
    assert "is not the one this command rebuilds" in err


def test_an_answer_stored_after_an_unasked_question_is_refused(
    graph_store, gate_research, tmp_path, monkeypatch, capsys
):
    variant_file = write_graph_variant(tmp_path, "g")
    _install_echo_graph(monkeypatch)
    argv = _ask_argv(graph_store, gate_research, variant_file, "--repeats", "1")
    assert cli.main(argv) == 0
    capsys.readouterr()
    conn = sqlite3.connect(_replay_db(graph_store))
    conn.execute("DELETE FROM answers WHERE input_id = ?", (gate_input_id(0),))
    conn.execute("DELETE FROM graph_books WHERE input_id = ?", (gate_input_id(0),))
    conn.commit()
    conn.close()
    assert cli.main(argv) == 1
    assert (
        "gate-in-01 repeat 0: stored after gate-in-00, which the walk never asked"
    ) in capsys.readouterr().err


class _Refusing(Exception):
    status_code = 422


class _RefusingGraph(EchoGraph):
    """An echo graph whose provider refuses the questions in ``slots`` for their own sake."""

    def __init__(self, variant, *, slots) -> None:
        super().__init__(variant)
        self.slots = set(slots)

    def ask(self, **kwargs):
        if any(question_line(slot) in kwargs["context_text"] for slot in self.slots):
            # The trader and the debators ran before the manager was refused.
            self.lost_input_tokens += 300
            self.lost_output_tokens += 30
            raise _Refusing("the context was too long")
        return super().ask(**kwargs)


class _Stopping(Exception):
    status_code = 401


class _StoppingGraph(EchoGraph):
    def ask(self, **kwargs):
        self.lost_input_tokens += 300
        self.lost_output_tokens += 30
        raise _Stopping("bad key")


def test_a_question_the_provider_refuses_holds_the_book_and_is_recorded(
    graph_store, gate_research, tmp_path, monkeypatch, capsys
):
    variant_file = write_graph_variant(tmp_path, "g")
    monkeypatch.setattr(cli, "_build_graph", lambda variant: _RefusingGraph(variant, slots={0}))
    assert cli.main(_ask_argv(graph_store, gate_research, variant_file, "--repeats", "1")) == 0
    captured = capsys.readouterr()
    assert "questions the provider refused for their own sake: 1" in captured.out
    assert "whole graph 3800 in, 380 out over 35 completion(s)" in captured.out
    assert "of which on graph runs that failed or were refused: 300 in, 30 out" in captured.out
    assert (
        "gate-in-00 repeat 0: refused (422 _Refusing: the context was too long); recorded as "
        "unanswered, the book held"
    ) in captured.err
    with ReplayStore(_replay_db(graph_store)) as replay:
        sha = replay.variant("g").sha
        assert replay.failed(sha, GATE_RUN_ID) == {0: {gate_input_id(0)}}
        books = replay.books(sha, GATE_RUN_ID)[0]
        # The refused question's book carries what its failed run cost.
        assert books[gate_input_id(0)].graph_input_tokens == 300
        # Slot 1 was asked from a flat book: the refused slot 0 opened nothing.
        assert books[gate_input_id(1)].size == 0
        assert len(replay.answers(sha, GATE_RUN_ID)[0]) == 7


def test_a_failed_graph_call_is_tried_again_after_the_pause_and_its_cost_is_counted(
    graph_store, gate_research, tmp_path, monkeypatch, capsys
):
    variant_file = write_graph_variant(tmp_path, "g")
    pauses: list[float] = []
    monkeypatch.setattr(cli, "_sleep", pauses.append)
    monkeypatch.setattr(cli, "_build_graph", lambda variant: EchoGraph(variant, fail=1))
    assert cli.main(_ask_argv(graph_store, gate_research, variant_file, "--repeats", "1")) == 0
    assert pauses == [5.0]
    out = capsys.readouterr().out
    assert "asked: 8 new answer(s)" in out
    # The failed run's 40 / 4 tokens are paid for: in the graph total, said apart,
    # and on the book of the question they were spent on.
    assert "whole graph 4040 in, 404 out over 40 completion(s)" in out
    assert "of which on graph runs that failed or were refused: 40 in, 4 out" in out
    with ReplayStore(_replay_db(graph_store)) as replay:
        books = replay.books(replay.variant("g").sha, GATE_RUN_ID)[0]
        assert books[gate_input_id(0)].graph_input_tokens == 540
        assert books[gate_input_id(1)].graph_input_tokens == 500


def test_the_position_section_and_the_trade_date_are_as_of_the_rows_candle_end(
    graph_store, gate_research, tmp_path, monkeypatch, capsys
):
    variant_file = write_graph_variant(tmp_path, "g")
    built = _install_echo_graph(monkeypatch)
    argv = _ask_argv(graph_store, gate_research, variant_file, "--repeats", "1", "--limit", "2")
    assert cli.main(argv) == 0
    first, second = built[0].asked
    # The candle end the row recorded, not the scheduler's instant the payload stamps.
    assert first["as_of"] == from_epoch_ms(candle_end_ms(0))
    assert second["as_of"] == from_epoch_ms(candle_end_ms(1))
    # Slot 1's section is rendered from the slot 0 fill, aged against that as-of.
    fill_line = next(
        line for line in second["context_text"].splitlines() if line.startswith("  Last fill: ")
    )
    assert from_epoch_ms(at_ms(0)).isoformat() in fill_line
    assert fill_line.endswith("before the as-of time above)")


def test_preflight_refuses_before_anything_is_registered_or_pinned(
    graph_store, gate_research, tmp_path, monkeypatch, capsys
):
    variant_file = write_graph_variant(tmp_path, "g")
    _install_echo_graph(monkeypatch)
    # No funding settlement in the research store before the questions.
    conn = sqlite3.connect(gate_research)
    conn.execute("DELETE FROM funding")
    conn.commit()
    conn.close()
    argv = _ask_argv(graph_store, gate_research, variant_file, "--repeats", "1")
    assert cli.main([*argv, "--dry-run"]) == 1
    err = capsys.readouterr().err
    assert "gate-in-00: the research store has no funding settlement at or before" in err
    assert cli.main(argv) == 1
    assert "no funding settlement" in capsys.readouterr().err
    with ReplayStore(_replay_db(graph_store)) as replay:
        assert replay.sha_of("g") is None  # not registered
        assert replay.pinned_split(GATE_RUN_ID) is None  # not pinned
    # A row without a candle end cannot say what its section is as of.
    again = write_research(gate_research.parent / "again.sqlite", GATE_MARKS, slots=12)
    conn = sqlite3.connect(graph_store)
    conn.execute(
        "UPDATE ai_inputs SET candle_end = NULL WHERE input_id = ?", (gate_input_id(2),)
    )
    conn.commit()
    conn.close()
    assert cli.main([*_ask_argv(graph_store, again, variant_file), "--dry-run"]) == 1
    assert "gate-in-02: the input row records no candle_end" in capsys.readouterr().err


def test_an_answer_the_account_cannot_take_is_not_stored_and_the_walk_stops_by_name(
    graph_store, gate_research, tmp_path, monkeypatch, capsys
):
    from contrib.replay import walk
    from contrib.replay.account import AccountError, Walker

    variant_file = write_graph_variant(tmp_path, "g")
    _install_echo_graph(monkeypatch)
    # The account refuses the order the answer asks for (the books' own refusal,
    # a quantity the venue would not take); nothing was stored before it.
    original = Walker.advance

    def refusing(self, decision):
        if decision.target is not None:
            raise AccountError(f"{decision.input_id}: the order rounds to nothing")
        return original(self, decision)

    monkeypatch.setattr(walk.Walker, "advance", refusing)
    argv = _ask_argv(graph_store, gate_research, variant_file, "--repeats", "1")
    assert cli.main(argv) == 1
    err = capsys.readouterr().err
    assert "gate-in-00 repeat 0: the account cannot take the answer it was given" in err
    assert "out graph tokens) was paid for and not stored, so the next run asks it again" in err
    # The cost of that unstored answer is said on the way out.
    assert "tokens spent on graph runs that failed, since this command started: 500 in, 50 out" in err
    with ReplayStore(_replay_db(graph_store)) as replay:
        assert replay.answers(replay.variant("g").sha, GATE_RUN_ID) == {}


def test_repeats_the_store_holds_beyond_this_command_are_said(
    graph_store, gate_research, tmp_path, monkeypatch, capsys
):
    variant_file = write_graph_variant(tmp_path, "g")
    _install_echo_graph(monkeypatch)
    assert cli.main(_ask_argv(graph_store, gate_research, variant_file, "--repeats", "2")) == 0
    capsys.readouterr()
    assert cli.main(_ask_argv(graph_store, gate_research, variant_file, "--repeats", "1")) == 0
    captured = capsys.readouterr()
    assert "note: the store also holds repeat(s) [1] of 'g' on this run" in captured.err
    assert "-- g repeat 1 --" in captured.out  # the bar reads every stored repeat
    assert (
        "recorded model of the run's own answers: recorded-model (not the variant's"
    ) in captured.out


def test_ask_dry_run_builds_no_graph_and_writes_nothing(
    graph_store, gate_research, tmp_path, monkeypatch, capsys
):
    variant_file = write_graph_variant(tmp_path, "g")
    monkeypatch.setattr(
        cli, "_build_graph", lambda variant: pytest.fail("a dry run builds no graph")
    )
    replay_db = graph_store.parent / "replay.sqlite"
    argv = _backtest(
        graph_store,
        gate_research,
        "--replay-db",
        str(replay_db),
        "--ask",
        str(variant_file),
        "--dry-run",
        "--limit",
        "5",
        run_id=GATE_RUN_ID,
    )
    assert cli.main(argv) == 0
    out = capsys.readouterr().out
    assert "payloads and reports read: 8, each payload checked" in out
    assert (
        "dry run: 0 answer(s) already stored, 24 to ask; this command would ask for 5 of them"
    ) in out
    assert "split: not pinned in" in out
    assert not replay_db.exists()


@pytest.mark.parametrize(
    "extra, message",
    [
        (("--ask", "g.yaml", "--variant", "v"), "--ask names the variant to walk"),
        (("--ask", "g.yaml", "--holdout"), "the holdout is not asked in this build"),
        (("--repeats", "2"), "--repeats only apply with --ask"),
        (("--dry-run", "--limit", "1"), "--limit, --dry-run only apply with --ask"),
        (("--ask", "g.yaml", "--repeats", "0"), "--repeats must be at least 1, got 0"),
        (("--ask", "g.yaml", "--limit", "0"), "--limit must be at least 1, got 0"),
        (
            ("--ask", "g.yaml", "--rule", "r.yaml"),
            "--rule only applies to a variant whose guardrail is 'rule'",
        ),
        (
            ("--ask", "v.yaml"),
            "variant 'v' is a simple variant; backtest --ask walks graph variants",
        ),
    ],
)
def test_ask_flags_out_of_place_are_refused_by_name(
    graph_store, gate_research, tmp_path, monkeypatch, capsys, extra, message
):
    monkeypatch.chdir(tmp_path)
    write_graph_variant(tmp_path, "g")
    write_variant(tmp_path, "v")
    assert cli.main(_gate_backtest(graph_store, gate_research, *extra)) == 1
    assert message in capsys.readouterr().err


def test_ask_needs_the_replay_store_and_the_reports_sidecars(
    gate_store, gate_research, tmp_path, monkeypatch, capsys
):
    variant_file = write_graph_variant(tmp_path, "g")
    argv = _backtest(gate_store, gate_research, "--ask", str(variant_file), run_id=GATE_RUN_ID)
    assert cli.main(argv) == 1
    assert "--ask needs --replay-db PATH" in capsys.readouterr().err
    # The gate store without any sidecar: nothing to start the lower half from.
    _install_echo_graph(monkeypatch)
    assert cli.main(_ask_argv(gate_store, gate_research, variant_file)) == 1
    err = capsys.readouterr().err
    assert "has no question with a .reports.json beside its payload" in err
    with ReplayStore(_replay_db(gate_store)) as replay:
        assert replay.pinned_split(GATE_RUN_ID) is None  # refused before the pin


def test_a_question_whose_cycle_wrote_no_sidecar_is_held_not_asked_and_a_broken_one_refused(
    graph_store, gate_research, tmp_path, monkeypatch, capsys
):
    variant_file = write_graph_variant(tmp_path, "g")
    built = _install_echo_graph(monkeypatch)
    root = payload_dir(graph_store, GATE_RUN_ID)
    # Slot 0 (the long open) lost its sidecar: an engine-failed cycle. It is
    # walked as a hold, so slot 1 is asked from a flat book.
    (root / "BTC-gate-00.reports.json").unlink()
    argv = _ask_argv(graph_store, gate_research, variant_file, "--repeats", "1")
    assert cli.main([*argv, "--limit", "2"]) == 0
    out = capsys.readouterr().out
    assert (
        "questions without a .reports.json beside the payload: 1 of 8 (the engine failed "
        "those cycles; nothing is asked there and the account holds its book)"
    ) in out
    assert "asked: 2 new answer(s); already stored, skipped: 0" in out
    assert [a["context_text"].splitlines()[0] for a in built[0].asked] == [
        question_line(1),
        question_line(2),
    ]
    # Resumed, the hold is replayed too, and the walk completes: seven asked in all.
    assert cli.main(argv) == 0
    out = capsys.readouterr().out
    assert "asked: 5 new answer(s); already stored, skipped: 2" in out
    assert "-- g repeat 0 --" in out
    with ReplayStore(_replay_db(graph_store)) as replay:
        sha = replay.variant("g").sha
        assert len(replay.answers(sha, GATE_RUN_ID)[0]) == 7
        assert replay.books(sha, GATE_RUN_ID)[0][gate_input_id(1)].size == 0
    # A sidecar that exists but is not the daemon's shape is still refused by name.
    (root / "BTC-gate-02.reports.json").write_text("{}", encoding="utf-8")
    assert cli.main(argv) == 1
    assert "gate-in-02:" in capsys.readouterr().err


def test_replay_refuses_a_graph_variant(graph_store, tmp_path, monkeypatch, capsys):
    variant_file = write_graph_variant(tmp_path, "g")
    monkeypatch.setattr(cli, "_build_model", lambda _variant: Echo())
    assert cli.main(replay_argv(graph_store, variant_file)) == 1
    assert "variant 'g' is a graph variant (mode: graph): it is walked by backtest --ask" in (
        capsys.readouterr().err
    )


def test_a_walk_that_stops_on_a_provider_error_says_what_its_failed_runs_cost(
    graph_store, gate_research, tmp_path, monkeypatch, capsys
):
    variant_file = write_graph_variant(tmp_path, "g")
    monkeypatch.setattr(cli, "_build_graph", lambda variant: _StoppingGraph(variant))
    assert cli.main(_ask_argv(graph_store, gate_research, variant_file, "--repeats", "1")) == 1
    err = capsys.readouterr().err
    assert "gate-in-00 repeat 0: the provider refused the call with 401" in err
    assert "tokens spent on graph runs that failed, since this command started: 300 in, 30 out" in err
