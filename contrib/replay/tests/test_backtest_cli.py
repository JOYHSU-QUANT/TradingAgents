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
from contrib.replay.upstream import ResearchStore

from .conftest import (
    ANCHOR_MS,
    COIN,
    ROWS,
    RUN_ID,
    STEP_MS,
    downgrade_to_v3,
    run_config,
    write_paper_store,
)
from .papers import (
    PAPERS,
    RUN_ID as GATE_RUN_ID,
    Echo,
    replay_argv,
    write_gate_store,
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
        tmp_path / "paper_trading.db", payload_root=tmp_path / "p", config=run_config()
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
    # The fixture's answers created an order at slots 0, 2, 5 and 9 (the
    # approved target inside the deadband at slot 7 created none).
    assert "4 order(s), 4 fill(s)" in paper[2]
    assert (
        "smoke test: no --variant, so the bar judges the paper trader against itself; "
        "checks (1) and (3) cannot pass and (4) cannot fail"
    ) in out
    bar = _section(out, "-- plan section 5 bar: paper on the validation segment --")
    assert bar[1].startswith("  (1) net sharpe above every control: paper ")
    assert bar[1].endswith("-> FAIL")
    assert "1 variant(s) tried in the store" in bar[2]
    assert "paper only 0, McNemar p 1.000" in bar[3] and bar[3].endswith("-> FAIL")
    assert bar[4].endswith("-> pass")
    assert bar[5] == "  verdict: paper does not pass; the holdout stays locked"


def test_a_maker_run_posts_at_the_tick_and_reports_what_rested(tmp_path, research, capsys):
    maker = write_paper_store(
        tmp_path / "maker.db", payload_root=tmp_path / "p", config=run_config(style="maker")
    )
    assert cli.main(_backtest(maker, research, "--tick", "1")) == 0
    out = capsys.readouterr().out.splitlines()
    assert "maker fills at 0.00015 fee, posted at mark -/+ 0.5 bps on a 1 tick" in out[1]
    paper = _section(out, "-- paper --")
    assert "post(s) left resting" in paper[2]
    assert "fill(s)" in paper[2]


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
        assert "2 variant(s) tried in the store" in bar[2]  # two repeats, one variant
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
        tmp_path / "maker.db", payload_root=tmp_path / "p", config=run_config(style="maker")
    )
    empty = tmp_path / "empty.sqlite"
    with ResearchStore(empty):
        pass
    assert cli.main(_backtest(maker, empty)) == 1
    err = capsys.readouterr().err
    assert "no bar in the research store covers" in err
    assert "whether the maker post fills cannot be judged" in err
