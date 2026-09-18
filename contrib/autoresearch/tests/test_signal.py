"""The handoff document: what it says, what it refuses to say, and who reads it.

The one place the research radar touches the live path (plan §7 / PR C1), so
the tests here are about the SEAM rather than about the arithmetic behind it:
that a document this package writes is one the trading package's own reader
accepts, that every band is cut where this module says it is, that nothing
numeric crosses, and that each way of not having an answer is a refusal rather
than a softer document.

The synthetic history and the experiment helper come from ``test_research``:
it owns the one store this suite measures on (400 4h bars, their daily
backdrop and hourly funding), and a second generator here would be a second
history to keep in step for no gain.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import re
import shutil

import pytest

from contrib.autoresearch import evaluator as evaluator_module
from contrib.autoresearch.cli import main
from contrib.autoresearch.costs import CostModel, FillRole
from contrib.autoresearch.dsl import Side, parse_spec
from contrib.autoresearch.evaluator import load_bundle
from contrib.autoresearch.ledger import _EXPERIMENT_ID, Ledger
from contrib.autoresearch.metrics import Tally
from contrib.autoresearch.research import measure, promote, require_clean_history
from contrib.autoresearch.signal import (
    CONFIDENCE_EDGES,
    DRAWDOWN_EDGES,
    SignalError,
    build_signal,
    describe_signal,
    write_signal,
)
from contrib.autoresearch.store import ResearchStore
from contrib.autoresearch.upstream import (
    MAX_SIGNAL_AGE_INTERVALS,
    ResearchBias,
    ResearchConfidence,
    ResearchDrawdown,
    ResearchSignal,
)

# The reader, from the package that will actually read the document. This is
# the contract test's other half: what is written here has to satisfy the
# module the daemon calls, not a local restatement of its rules.
from contrib.hyperliquid_perp.domains.perp.research_signal import load_research_signal
from contrib.hyperliquid_perp.domains.perp.schema import MAX_RESEARCH_TEXT_CHARS

from .test_research import _BUY, _SIZING, _fill, _open

# A rule that READS FUNDING, so a gap in the settlements leaves bars the rule
# cannot be asked about. ``_BUY`` reads only the close and could never show it.
_FUNDING_RULE = {
    "family": "breakout",
    "entry": {"long": [{"left": "funding_rate", "op": ">", "right": -1}]},
    "sizing": _SIZING,
}

_MS_PER_4H = 4 * 60 * 60_000
_MS_PER_DAY = 24 * 60 * 60_000


@pytest.fixture(scope="module")
def promoted_store(tmp_path_factory):
    """A store with one promoted rule on BTC, built once and copied per test."""
    path = tmp_path_factory.mktemp("promoted") / "autoresearch.sqlite"
    with ResearchStore(path) as store:
        ledger = Ledger(store)
        _fill(store)
        # Maker fills: plan §7 makes that the precondition for publishing at
        # all and ``build_signal`` enforces it, so the ordinary fixture is a
        # maker experiment and the taker case gets its own test.
        experiment = _open(ledger, costs=CostModel(fill_role=FillRole.MAKER))
        measurement = measure(ledger, experiment, parse_spec(_BUY))
        assert measurement.verdict.eligible, measurement.verdict.blockers
        promote(ledger, experiment, measurement.trial.trial_id)
    return path


@pytest.fixture
def ledger(promoted_store, tmp_path):
    copy = tmp_path / "autoresearch.sqlite"
    shutil.copy(promoted_store, copy)
    with ResearchStore(copy) as store:
        yield Ledger(store)


@pytest.fixture
def bare(tmp_path):
    """A store with the same history and nothing promoted on it."""
    with ResearchStore(tmp_path / "bare.sqlite") as store:
        _fill(store)
        yield Ledger(store)


def _with_validation(ledger, monkeypatch, *, sharpe=None, drawdown=None):
    """Re-file the promoted trial's validation tally, leaving the store alone.

    The bands are the one thing here that is a pure function of two filed
    numbers, and driving them from real history would mean fitting a rule to
    land on each edge — which would test the fitting, not the cut.
    """
    experiment, trial = ledger.latest_promotion("BTC")
    net = trial.validation.net
    tally = Tally(
        total_return=net.total_return,
        sharpe=net.sharpe if sharpe is None else sharpe,
        max_drawdown=net.max_drawdown if drawdown is None else drawdown,
        hit_rate=net.hit_rate,
    )
    edited = dataclasses.replace(trial, validation=dataclasses.replace(trial.validation, net=tally))
    monkeypatch.setattr(ledger, "latest_promotion", lambda coin: (experiment, edited))


# -- what the document says -------------------------------------------------


def test_the_signal_is_dated_to_the_newest_bar_the_store_holds(ledger):
    signal, experiment, trial = build_signal(ledger, "BTC")
    newest = list(ledger.store.iter_candles("BTC", "4h"))[-1]
    assert signal.as_of_ms == newest.close_time
    assert signal.coin == "BTC"
    assert signal.interval == "4h"
    assert signal.strategy_id == f"{experiment.experiment_id}#{trial.trial_id}"
    # Past the holdout on purpose: the side is path dependent, so it is read
    # off the newest bar and not off the window the bands came from.
    assert newest.open_time >= experiment.split.holdout.start_ms


def test_the_windows_are_the_experiments_own_in_whole_days(ledger):
    signal, experiment, _trial = build_signal(ledger, "BTC")
    for days, segment in (
        (signal.eval_window_days, experiment.split.validation),
        (signal.holdout_window_days, experiment.split.holdout),
    ):
        assert days == round((segment.end_ms - segment.start_ms) / _MS_PER_DAY)


def test_nothing_the_ledger_measured_reaches_the_document(ledger):
    # The scope test of the whole seam. The document may carry the windows —
    # a span an operator chose — and nothing else that was measured: no
    # Sharpe, no drawdown, no return, no hit rate, no trade count.
    signal, _experiment, trial = build_signal(ledger, "BTC")
    document = signal.to_document()
    # Checked as the set of numeric FIELDS rather than by hunting each figure
    # in the text: a measured value that happened to be 1 would be "found" in
    # the version number, and a test that passes for that reason would go on
    # passing after someone added a field carrying a real one.
    numeric = {
        key
        for key, value in document.items()
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    }
    assert numeric == {"document_version", "as_of_ms", "eval_window_days", "holdout_window_days"}
    text = json.dumps(document)
    for measured in (
        trial.validation.net.sharpe,
        trial.validation.net.max_drawdown,
        trial.validation.net.total_return,
        trial.validation.net.hit_rate,
    ):
        assert repr(measured) not in text


def test_the_notes_compare_the_held_back_window_to_the_selection_one(ledger):
    signal, _experiment, trial = build_signal(ledger, "BTC")
    holdout, validation = trial.holdout, trial.validation
    assert signal.notes.startswith("held-back window, measured once:")
    assert ("net return positive" in signal.notes) is (holdout.net.total_return > 0)
    ratio = "at or above" if holdout.net.sharpe >= validation.net.sharpe else "below"
    assert f"return-to-volatility {ratio} the selection window's" in signal.notes


def test_the_bias_is_the_side_the_replay_ended_on_from_the_experiments_first_bar(
    ledger, monkeypatch
):
    seen = {}
    original = evaluator_module.replay_position

    def spy(spec, frame, costs, *, since_ms):
        replayed = original(spec, frame, costs, since_ms=since_ms)
        seen.update(replayed=replayed, since_ms=since_ms)
        return replayed

    monkeypatch.setattr(evaluator_module, "replay_position", spy)
    signal, experiment, _trial = build_signal(ledger, "BTC")
    # Replayed from the experiment's own first measurable bar, not from a
    # recent tail: an open position older than the tail would be invisible.
    assert seen["since_ms"] == experiment.split.train.start_ms
    assert signal.bias is {
        Side.LONG: ResearchBias.LONG,
        Side.SHORT: ResearchBias.SHORT,
        None: ResearchBias.FLAT,
    }[seen["replayed"].side]


@pytest.mark.parametrize(
    ("sharpe", "band"),
    [
        (CONFIDENCE_EDGES[0] - 0.01, ResearchConfidence.WEAK),
        (CONFIDENCE_EDGES[0], ResearchConfidence.MEDIUM),
        (CONFIDENCE_EDGES[1] - 0.01, ResearchConfidence.MEDIUM),
        (CONFIDENCE_EDGES[1], ResearchConfidence.STRONG),
    ],
)
def test_the_confidence_band_is_cut_at_the_documented_edges(ledger, monkeypatch, sharpe, band):
    _with_validation(ledger, monkeypatch, sharpe=sharpe)
    assert build_signal(ledger, "BTC")[0].confidence is band


@pytest.mark.parametrize(
    ("drawdown", "band"),
    [
        (DRAWDOWN_EDGES[0] - 0.001, ResearchDrawdown.SHALLOW),
        (DRAWDOWN_EDGES[0], ResearchDrawdown.MODERATE),
        (DRAWDOWN_EDGES[1] - 0.001, ResearchDrawdown.MODERATE),
        (DRAWDOWN_EDGES[1], ResearchDrawdown.DEEP),
    ],
)
def test_the_drawdown_band_is_cut_at_the_documented_edges(ledger, monkeypatch, drawdown, band):
    _with_validation(ledger, monkeypatch, drawdown=drawdown)
    assert build_signal(ledger, "BTC")[0].drawdown is band


def test_the_latest_promotion_is_the_one_that_speaks(ledger):
    # A coin can carry several promotions and no column marks one current, so
    # the rule is pinned here against two real rows rather than left to the
    # SQL's ORDER BY to imply.
    experiment = ledger.experiment("btc-4h")
    other = measure(
        ledger,
        experiment,
        parse_spec(
            {
                "family": "breakout",
                "entry": {"long": [{"left": "close", "op": ">", "right": 1}]},
                "sizing": {"mode": "fixed_margin_fraction", "fraction": 0.5},
            }
        ),
    )
    promote(ledger, experiment, other.trial.trial_id)
    _signal, _experiment, trial = build_signal(ledger, "BTC")
    assert trial.trial_id == other.trial.trial_id
    assert ledger.holdout_looks("BTC") == 2


# -- what it refuses --------------------------------------------------------


def test_a_rule_scored_under_taker_fills_is_not_published_without_being_asked(tmp_path):
    # Plan §7's standing precondition, as a guard rather than as prose: run 5
    # moved the paper lane to maker fills, so a rule selected against taker
    # costs was chosen under a cost model the account no longer pays. The
    # escape hatch is explicit rather than implied.
    with ResearchStore(tmp_path / "taker.sqlite") as store:
        ledger = Ledger(store)
        _fill(store)
        experiment = _open(ledger, costs=CostModel(fill_role=FillRole.TAKER))
        measurement = measure(ledger, experiment, parse_spec(_BUY))
        promote(ledger, experiment, measurement.trial.trial_id)
        with pytest.raises(SignalError, match="scored its trials under taker fills"):
            build_signal(ledger, "BTC")
        signal, _experiment, _trial = build_signal(ledger, "BTC", allow_taker=True)
        assert signal.bias in set(ResearchBias)


def test_a_funding_hole_this_package_tolerates_is_reported_rather_than_refused(tmp_path, caplog):
    # The guard this replaced refused on a SINGLE unevaluable bar, which
    # ``require_clean_history`` calls healthy by name ("the venue skips a
    # settlement now and then") and which the promote gate does not blame a
    # trial for. That made the command unusable on a store the package had
    # already promoted from, with a remedy — fetch the missing settlement —
    # that cannot be performed. Driven from a REAL gapped store here, which is
    # what would have caught it the first time.
    with ResearchStore(tmp_path / "gapped.sqlite") as store:
        ledger = Ledger(store)
        _fill(store)
        experiment = _open(ledger, costs=CostModel(fill_role=FillRole.MAKER))
        measurement = measure(ledger, experiment, parse_spec(_FUNDING_RULE))
        assert measurement.verdict.eligible, measurement.verdict.blockers
        promote(ledger, experiment, measurement.trial.trial_id)
        # A day of settlements removed from inside the replay span, AFTER the
        # promotion, so the trial's own filed figures are untouched.
        start = experiment.split.validation.start_ms
        store.conn.execute(
            "DELETE FROM funding WHERE coin = 'BTC' AND time BETWEEN ? AND ?",
            (start, start + _MS_PER_DAY),
        )
        # The store is still one this package will measure on...
        assert require_clean_history(load_bundle(store, coin="BTC", interval="4h"), "4h") > 0
        # ...so the command publishes, and says what it saw.
        with caplog.at_level(logging.WARNING):
            signal, _experiment, _trial = build_signal(ledger, "BTC")
    assert signal.bias in set(ResearchBias)
    # The whole point of the message is the RATIO — "6 bars" and "6 of 4,000"
    # ask the reader for different judgements — so both numbers are pinned,
    # and in the right order. Without this, swapping them leaves the suite
    # green while the operator reads the denominator as the count.
    reported = re.search(r"evaluated on (\d+) of the (\d+) 4h bars", caplog.text)
    assert reported, caplog.text
    unevaluable, replayed = (int(group) for group in reported.groups())
    assert 0 < unevaluable < replayed


def test_a_rule_id_cannot_overflow_the_bound_the_reader_enforces(ledger):
    # There is no producer-side length check because none can be reached: the
    # ledger caps an experiment name and a trial id is a small integer, so
    # ``<experiment>#<trial>`` is far inside the reader's bound on a rendered
    # field. Pinned as arithmetic against the CAP ITSELF rather than against a
    # copy of the number: a copy would keep this green while the real cap
    # moved past the bound, which is the failure it claims to catch.
    experiment, trial = ledger.latest_promotion("BTC")
    longest_name = "e" * _EXPERIMENT_ID.match("e" * 4096).end()
    assert len(f"{longest_name}#{trial.trial_id}") < MAX_RESEARCH_TEXT_CHARS
    signal, _experiment, _trial = build_signal(ledger, "BTC")
    assert signal.strategy_id == f"{experiment.experiment_id}#{trial.trial_id}"


def test_a_store_with_no_promotion_has_nothing_to_tell_the_live_path(bare):
    with pytest.raises(SignalError, match="no rule has been promoted on BTC"):
        build_signal(bare, "BTC")


def test_a_coin_this_store_never_studied_is_refused_by_name(ledger):
    with pytest.raises(SignalError, match="no rule has been promoted on ETH"):
        build_signal(ledger, "ETH")


def test_a_newest_bar_the_rule_could_not_be_asked_about_is_refused(ledger, monkeypatch):
    # The fail-closed case the replay's flag exists for: the side would be one
    # the rule took earlier, not one it just re-took, and publishing it would
    # tell the live path a rule re-affirmed a position it was never asked
    # about.
    original = evaluator_module.replay_position

    def unevaluable(spec, frame, costs, *, since_ms):
        return dataclasses.replace(
            original(spec, frame, costs, since_ms=since_ms), last_bar_unevaluable=True
        )

    monkeypatch.setattr(evaluator_module, "replay_position", unevaluable)
    with pytest.raises(SignalError, match="could not evaluate"):
        build_signal(ledger, "BTC")


def test_a_store_with_a_hole_in_it_is_refused_before_any_side_is_read(ledger):
    # ``require_clean_history``'s refusal, reached through this command: a
    # replayed side is path dependent, so a missing bar is not merely a
    # shorter history, it is possibly a different answer.
    newest = list(ledger.store.iter_candles("BTC", "4h"))[-1].open_time
    ledger.store.conn.execute(
        "DELETE FROM candles WHERE coin = 'BTC' AND interval = '4h' AND open_time = ?",
        (newest - 10 * _MS_PER_4H,),
    )
    with pytest.raises(RuntimeError, match="4h"):
        build_signal(ledger, "BTC")


# -- the seam: written here, read there -------------------------------------


def test_the_written_document_is_one_the_trading_package_accepts(ledger, tmp_path):
    # The contract test. Both halves run: this package writes the document and
    # the module the daemon calls reads it back, with no local restatement of
    # the reader's rules in between.
    signal, _experiment, _trial = build_signal(ledger, "BTC")
    target = write_signal(tmp_path / "signal.json", signal)
    assert (
        load_research_signal(
            str(target), coin="BTC", as_of_ms=signal.as_of_ms, candle_interval_ms=_MS_PER_4H
        )
        == signal
    )


def test_a_document_older_than_the_readers_bound_is_dropped_by_the_reader(ledger, tmp_path, caplog):
    signal, _experiment, _trial = build_signal(ledger, "BTC")
    target = write_signal(tmp_path / "signal.json", signal)
    bound = MAX_SIGNAL_AGE_INTERVALS * _MS_PER_4H
    common = {"coin": "BTC", "candle_interval_ms": _MS_PER_4H}
    assert load_research_signal(str(target), as_of_ms=signal.as_of_ms + bound, **common) == signal
    with caplog.at_level("WARNING"):
        stale = load_research_signal(str(target), as_of_ms=signal.as_of_ms + bound + 1, **common)
    assert stale is None
    assert "past the" in caplog.text


def test_the_document_is_written_atomically_and_leaves_nothing_behind(ledger, tmp_path):
    signal, _experiment, _trial = build_signal(ledger, "BTC")
    target = write_signal(tmp_path / "nested" / "signal.json", signal)
    assert [path.name for path in sorted(target.parent.iterdir())] == ["signal.json"]
    assert ResearchSignal.from_document(json.loads(target.read_text(encoding="utf-8"))) == signal


def test_an_out_path_whose_parent_cannot_be_made_is_refused_by_name(ledger, tmp_path, capsys):
    # The likeliest operator mistake this command has — `--out` under a
    # directory the cron user cannot create — and for one round it escaped as
    # a bare traceback, because the wrap covered only the write and the
    # rename while the CLI's refusal family no longer covered OSError. Driven
    # through main(), which is where the exit-code contract lives.
    blocker = tmp_path / "notadir"
    blocker.write_text("i am a file", encoding="utf-8")
    signal, _experiment, _trial = build_signal(ledger, "BTC")
    with pytest.raises(SignalError, match="could not write the handoff document to"):
        write_signal(blocker / "under" / "signal.json", signal)
    # An argument that is not a path at all gets its OWN sentence: the advice
    # about writability would be about a filesystem this never reached.
    with pytest.raises(SignalError, match="is not a path this command can resolve"):
        write_signal("out" + chr(0) + ".json", signal)
    assert main(
        ["signal", "--db", str(ledger.store.path), "--out", str(blocker / "under" / "s.json")]
    ) == 1
    assert "could not write the handoff document to" in capsys.readouterr().err


def test_a_failed_write_leaves_no_temporary_beside_the_document(ledger, tmp_path, monkeypatch):
    signal, _experiment, _trial = build_signal(ledger, "BTC")

    def refuse(*_args):
        raise OSError("the rename failed")

    monkeypatch.setattr("contrib.autoresearch.signal.os.replace", refuse)
    # Named here rather than left as a bare errno: the CLI's refusal family
    # promises every member carries a sentence written for an operator, and an
    # errno naming a temporary file does not. (Putting ``OSError`` in that
    # family instead would have swallowed ``requests``' exceptions, which ARE
    # ``OSError``s, under ``fetch`` and ``research``.)
    with pytest.raises(SignalError, match=r"could not write the handoff document to .*--out"):
        write_signal(tmp_path / "signal.json", signal)
    assert not (tmp_path / "signal.json").exists()
    assert [path.name for path in tmp_path.iterdir() if path.name.endswith(".tmp")] == []


# -- the command ------------------------------------------------------------


def test_the_command_writes_the_document_and_names_the_cost_model(promoted_store, tmp_path, capsys):
    copy = tmp_path / "autoresearch.sqlite"
    shutil.copy(promoted_store, copy)
    out = tmp_path / "signal.json"
    assert main(["signal", "--coin", "BTC", "--db", str(copy), "--out", str(out)]) == 0
    printed = capsys.readouterr().out
    # The standing precondition for turning the live switch on is that the
    # promoted rules were scored under MAKER fills, and only the experiment
    # knows; the command says it every run rather than leaving it to be
    # looked up.
    assert "scored under: maker fills" in printed
    assert str(out) in printed
    assert ResearchSignal.from_document(json.loads(out.read_text(encoding="utf-8")))


def test_the_command_refuses_a_store_with_nothing_promoted(tmp_path, capsys):
    path = tmp_path / "autoresearch.sqlite"
    with ResearchStore(path) as store:
        _fill(store)
    assert main(["signal", "--db", str(path), "--out", str(tmp_path / "x.json")]) == 1
    assert "no rule has been promoted on BTC" in capsys.readouterr().err
    assert not (tmp_path / "x.json").exists()


def test_the_schedule_line_takes_its_bound_from_the_reader(ledger):
    # One definition of the staleness bound: the sentence an operator reads
    # here is built from the constant the daemon enforces, so it cannot go on
    # advertising a cadence the reader stopped honouring.
    signal, experiment, trial = build_signal(ledger, "BTC")
    lines = describe_signal(signal, experiment, trial)
    schedule = next(line for line in lines if line.startswith("schedule:"))
    assert f"older than {MAX_SIGNAL_AGE_INTERVALS} of its own 4h bars" in schedule
