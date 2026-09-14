"""The verbs an operator runs: open an experiment, measure a rule, promote a trial, calibrate.

This is the one module that puts the evaluator and the ledger together, and it
exists so the CLI stays a thin argument parser — every rule about WHICH rows a
measurement may read lives here, beside the reads, rather than in a command.

The holdout lock, end to end (plan §3.8, §11). ``load_bundle`` reads the whole
store unless it is given a bound, and ``evaluate_split`` computes the holdout
only when told to — two defaults, each harmless alone. Every read in this
module passes ``Split.loadable_until(holdout=...)``, and ``holdout=True`` is
written in exactly one place: :func:`promote`, after the gate. A trial that
has not been promoted is measured on a bundle that does not HOLD the holdout
rows. The one read of the whole store is :func:`plan_experiment`, which needs
the span to cut and computes no figure for any window.

Before anything is measured, the history it is measured on is scanned
(:func:`require_clean_history`, plan §11). The evaluator already refuses a
hole inside a window; what it cannot see is the warm-up BEFORE the window —
``sma_200`` over a series with a hole in its two hundred bars covers more
calendar than its name says — and the daily backdrop.

Imports the feature stack (pandas, stockstats): only the commands that compute
something reach this module, and they import it inside the command.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from .baselines import baseline_specs
from .constants import (
    CANDLE_STAMP_TOLERANCE_MS,
    FUNDING_INTERVAL_MS,
    FUNDING_STAMP_TOLERANCE_MS,
    MS_PER_DAY,
)
from .costs import CostModel
from .dsl import StrategySpec, spec_hash
from .evaluator import EvaluationError, SplitResult, evaluate_split, load_bundle
from .features import FeatureFrame, SeriesBundle
from .gaps import GapReport, scan_stamps
from .ledger import (
    Experiment,
    Ledger,
    LedgerError,
    Penalty,
    SearchTrial,
    Trial,
    Verdict,
)
from .metrics import SegmentMetrics
from .split import (
    DEFAULT_TRAIN_SHARE,
    DEFAULT_VALIDATION_SHARE,
    Segment,
    SegmentName,
    Split,
    SplitError,
    studied_interval,
)
from .upstream import from_epoch_ms, interval_to_ms
from .vocabulary import (
    MAX_OFFSET_BARS,
    FeatureKind,
    FeatureRef,
    SeriesSource,
    feature_names,
    parse_feature_name,
    periods_for,
    spec_of,
)

__all__ = [
    "Measurement",
    "calibrate",
    "first_measurable_index",
    "measure",
    "open_experiment",
    "plan_experiment",
    "plan_split",
    "promote",
    "require_clean_history",
    "require_measurable_tail",
]

# How far before the first bar a feature at that bar can read, per series: the
# longest daily mean and the longest funding z-score window, in days. Derived
# from the vocabulary, so a longer period added there widens the scan here.
_DAILY_REACH_MS: Final = max(periods_for(FeatureKind.SMA_1D)) * MS_PER_DAY
_FUNDING_REACH_MS: Final = max(periods_for(FeatureKind.FUNDING_ZSCORE)) * MS_PER_DAY


@dataclass(frozen=True)
class Measurement:
    """What ``evaluate`` did with one spec.

    ``result`` is ``None`` for a duplicate: the rule was already measured in
    this experiment, ``trial`` is that earlier trial, and nothing was computed
    or written — the evaluator is deterministic, so a re-run is not a look.

    ``trial`` is the SEARCH view (:class:`~.ledger.SearchTrial`): resubmitting
    a promoted rule must not be a way for a hypothesis loop to read its
    holdout (plan §3.11).
    """

    trial: SearchTrial
    verdict: Verdict
    result: SplitResult | None
    funding_holes: int = 0

    @property
    def duplicate(self) -> bool:
        return self.result is None


# -- the history a measurement reads -----------------------------------------


def require_clean_history(bundle: SeriesBundle, interval: str) -> int:
    """Refuse a bundle whose warm-up or backdrop is not a grid; return the funding holes found.

    Scanned over the reach the features actually have: every decision bar the
    bundle holds (the warm-up is what ``sma_200`` and the indicator window
    read), the daily bars from the longest daily mean before the first bar,
    and the settlements from the longest z-score window before it. Older
    history is never read by any feature, and scanning it would refuse a store
    over a stamp nothing looks at.

    Bars and daily bars are refused on ANY finding: a hole there makes a
    windowed mean span more calendar than its name. Settlements are refused
    on a duplicate or off-grid stamp — counted, it stands in for a missing
    hour — and their HOLES are counted and returned rather than refused,
    because that is the coverage policy the features already apply
    (``features.MIN_WINDOW_COVERAGE``): the venue skips a settlement now and
    then, and a store refused for it would be unmeasurable.
    """
    key = studied_interval(interval)
    step = interval_to_ms(key)
    first_close = bundle.bars[0].close_time
    bars = scan_stamps(
        f"{key} bars", step, CANDLE_STAMP_TOLERANCE_MS, [bar.open_time for bar in bundle.bars]
    )
    daily = scan_stamps(
        "1d bars",
        MS_PER_DAY,
        CANDLE_STAMP_TOLERANCE_MS,
        [day.open_time for day in bundle.daily if day.close_time >= first_close - _DAILY_REACH_MS],
    )
    funding = scan_stamps(
        "funding settlements",
        FUNDING_INTERVAL_MS,
        FUNDING_STAMP_TOLERANCE_MS,
        [point.time for point in bundle.funding if point.time >= first_close - _FUNDING_REACH_MS],
    )
    for report, holes_allowed in ((bars, False), (daily, False), (funding, True)):
        if report.duplicate_ms or report.misaligned_ms or (report.gaps and not holes_allowed):
            raise EvaluationError(_unclean(report))
    return len(funding.gaps)


def _unclean(report: GapReport) -> str:
    stamps = [gap.after_ms for gap in report.gaps] + list(report.duplicate_ms + report.misaligned_ms)
    return (
        f"the {report.label} this measurement reads are not a grid: {len(report.gaps)} hole(s), "
        f"{len(report.duplicate_ms)} duplicate slot(s), {len(report.misaligned_ms)} off-grid "
        f"stamp(s), the earliest at {from_epoch_ms(min(stamps)).isoformat()}. A feature reading "
        f"across a hole covers more calendar than its name says — run `gaps`, then `fetch` "
        f"the span."
    )


def first_measurable_index(frame: FeatureFrame) -> int:
    """The first bar at which EVERY spec this language can write has every value it reads.

    Measured, not derived (plan §10.2, §11): the first bar that ends a run of
    ``MAX_OFFSET_BARS + 1`` consecutive bars on which every column of the
    vocabulary has a value. A reference may read up to that many bars back, so
    a spec is measurable at the train window's first bar only if every bar it
    could reach has a value — not merely the earliest one. A first cut took the
    first bar with every value and added the offset, which let a hole in
    between (a funding window losing coverage goes back to ``None``) refuse a
    lagged spec at train's first bar. So every trial is measured over the same
    bars, and a warm-up refusal never has to be mistaken for a result.

    A column the store cannot answer anywhere (no daily bars fetched, a funding
    series that ends before the bars begin) is the frame's own refusal, and
    names what to fetch.
    """
    columns = [column for _name, column in _vocabulary_columns(frame)]
    bars = len(frame.bundle.bars)
    run = 0
    for index in range(bars):
        run = run + 1 if all(column[index] is not None for column in columns) else 0
        if run > MAX_OFFSET_BARS:
            return index
    raise EvaluationError(
        f"no {MAX_OFFSET_BARS + 1} consecutive bars of this {bars}-bar store have every feature "
        f"of the vocabulary, so an experiment here would refuse some legal spec at its first "
        f"bar — fetch older history, or fill the hole `gaps` reports"
    )


def require_measurable_tail(frame: FeatureFrame) -> None:
    """Refuse a frame whose LAST bars lack a value of the vocabulary — the start rule, at the end.

    The span's end is where the bars end, and the tail of the span is the
    holdout. A daily or funding series fetched earlier than the decision bars
    stops short of them, and the features past its end read ``None`` — which
    mid-window is "does not trigger", not a refusal. So a rule that reads a
    daily mean would pass the gate and then sit silently flat through the end
    of its holdout, while a rule that reads none would not: the per-spec
    difference :func:`first_measurable_index` exists to rule out at the start
    (decided 2026-09-14). The last ``MAX_OFFSET_BARS + 1`` bars must have every
    value, the reach of the deepest offset, as at the start. Checked when an
    experiment is created and again at promote, on the holdout-bound frame.
    """
    bars = len(frame.bundle.bars)
    tail = range(max(0, bars - MAX_OFFSET_BARS - 1), bars)
    missing: dict[SeriesSource, list[str]] = {}
    for name, column in _vocabulary_columns(frame):
        if any(column[i] is None for i in tail):
            kind, _period = parse_feature_name(name)
            missing.setdefault(spec_of(kind).source, []).append(name)
    if missing:
        last = from_epoch_ms(frame.bundle.bars[-1].open_time).isoformat()
        count = sum(len(names) for names in missing.values())
        reasons = "; ".join(
            f"{', '.join(names)} ({_TAIL_REMEDY[source]})" for source, names in missing.items()
        )
        raise EvaluationError(
            f"{count} feature(s) of the vocabulary have no value on the last {len(tail)} bars "
            f"(to {last}): {reasons}. The end of the span is the holdout."
        )


# What to do about a feature with no value at the end, by the series it reads:
# a daily or settlement series stops short when it was fetched earlier than the
# decision bars; a bar feature has no such excuse, since its series IS the bars.
_TAIL_REMEDY: Final = {
    SeriesSource.DAILY: "read from the 1d bars — `fetch --interval 1d` up to the same end",
    SeriesSource.FUNDING: "read from the funding settlements — `fetch` them up to the same end",
    SeriesSource.BARS: (
        "read from the decision bars themselves, so no fetch supplies it — an indicator "
        "that stopped answering at the end; run `gaps` and look at the last bars"
    ),
}


def _vocabulary_columns(frame: FeatureFrame) -> list[tuple[str, tuple]]:
    return [(name, frame.series(FeatureRef(*parse_feature_name(name)))) for name in feature_names()]


def plan_split(
    interval: str,
    *,
    grid_origin_ms: int,
    train_start_ms: int,
    end_ms: int,
    pinned_holdout_ms: int | None,
    train_share: float = DEFAULT_TRAIN_SHARE,
    validation_share: float = DEFAULT_VALIDATION_SHARE,
) -> Split:
    """The split an experiment is created with: measured start, pinned or new holdout, shared cuts.

    The FIRST experiment on a coin cuts its holdout by share of the span and
    snaps that cut to the nearest UTC midnight — a day edge is on both studied
    grids, so a ``1d`` experiment created later can share the pin. Every later
    experiment starts its holdout at the pin (see :mod:`.ledger` for why
    exactly there). Train and validation then divide what is left before the
    holdout in the ratio of their shares.
    """
    key = studied_interval(interval)
    step = interval_to_ms(key)
    # ``by_shares`` owns the share and span checks and the snapping to bars;
    # its holdout cut is the starting point for the first experiment.
    shaped = Split.by_shares(
        key,
        start_ms=train_start_ms,
        end_ms=end_ms,
        train_share=train_share,
        validation_share=validation_share,
    )
    end_ms = shaped.holdout.end_ms
    if pinned_holdout_ms is None:
        holdout_ms = round(shaped.holdout.start_ms / MS_PER_DAY) * MS_PER_DAY
    else:
        holdout_ms = pinned_holdout_ms
    if (holdout_ms - grid_origin_ms) % step:
        raise SplitError(
            f"the holdout would begin at {from_epoch_ms(holdout_ms).isoformat()}, which is not on "
            f"this store's {key} grid — no bar opens there"
        )
    ratio = train_share / (train_share + validation_share)
    validation_ms = train_start_ms + step * round((holdout_ms - train_start_ms) * ratio / step)
    if not train_start_ms < validation_ms < holdout_ms < end_ms:
        raise SplitError(
            f"train from {from_epoch_ms(train_start_ms).isoformat()}, validation from "
            f"{from_epoch_ms(validation_ms).isoformat()}, holdout from "
            f"{from_epoch_ms(holdout_ms).isoformat()} to {from_epoch_ms(end_ms).isoformat()} "
            f"leaves a window with no bars — the store's measurable span is too short for "
            f"this split" + ("" if pinned_holdout_ms is None else ", given the pinned holdout")
        )
    return Split(
        interval=key,
        train=Segment(SegmentName.TRAIN, train_start_ms, validation_ms),
        validation=Segment(SegmentName.VALIDATION, validation_ms, holdout_ms),
        holdout=Segment(SegmentName.HOLDOUT, holdout_ms, end_ms),
    )


# -- the verbs ----------------------------------------------------------------


def plan_experiment(
    ledger: Ledger,
    *,
    experiment_id: str,
    coin: str,
    interval: str,
    costs: CostModel,
    indicator_lookback: int,
    penalty: Penalty,
    train_share: float = DEFAULT_TRAIN_SHARE,
    validation_share: float = DEFAULT_VALIDATION_SHARE,
    notes: str = "",
) -> Experiment:
    """Measure where the store's history is fit to measure on and cut the split; write nothing.

    Reads the whole store: the span to cut is the span the store holds. No
    figure for any window is computed here — the indicator walk runs so the
    warm-up and the tail can be MEASURED — so reading the holdout rows at this
    one moment lets nothing be chosen on them. What ``experiment --dry-run``
    prints; :func:`open_experiment` writes it.
    """
    key = studied_interval(interval)
    # Refused before the store is read, so a dry run refuses what the write
    # would; the write checks both again inside its transaction.
    ledger.require_unused_name(experiment_id)
    ledger.require_pinned_penalty(coin, penalty)
    bundle = load_bundle(ledger.store, coin=coin, interval=key)
    require_clean_history(bundle, key)
    frame = FeatureFrame(bundle, indicator_lookback=indicator_lookback)
    bars = bundle.bars
    first = first_measurable_index(frame)
    require_measurable_tail(frame)
    split = plan_split(
        key,
        grid_origin_ms=bars[0].open_time,
        train_start_ms=bars[first].open_time,
        end_ms=bars[-1].open_time + interval_to_ms(key),
        pinned_holdout_ms=ledger.holdout_pin(coin),
        train_share=train_share,
        validation_share=validation_share,
    )
    return Experiment(
        experiment_id=experiment_id,
        coin=coin,
        costs=costs,
        split=split,
        indicator_lookback=indicator_lookback,
        penalty=penalty,
        notes=notes,
    )


def open_experiment(ledger: Ledger, **conditions) -> Experiment:
    """:func:`plan_experiment`, then write it — the pin and the name are checked in the write."""
    return ledger.create_experiment(plan_experiment(ledger, **conditions))


def _frame(ledger: Ledger, experiment: Experiment, *, holdout: bool) -> tuple[FeatureFrame, int]:
    """The frame a measurement runs on, read no further than the lock allows, and its funding holes."""
    bundle = load_bundle(
        ledger.store,
        coin=experiment.coin,
        interval=experiment.split.interval,
        until_ms=experiment.split.loadable_until(holdout=holdout),
    )
    holes = require_clean_history(bundle, experiment.split.interval)
    return FeatureFrame(bundle, indicator_lookback=experiment.indicator_lookback), holes


def measure(ledger: Ledger, experiment: Experiment, spec: StrategySpec) -> Measurement:
    """Score ``spec`` on train and validation, file it as a trial, and say where it stands.

    A rule already in the experiment is not measured again (see
    :class:`Measurement`); the refusal of a duplicate row is the ledger's, so
    two runs racing on one rule still file it once.
    """
    # Before the evaluation, not only at the write: a value that is not the
    # stored experiment would otherwise pay for a measurement it cannot file.
    ledger.require_stored(experiment)
    existing = ledger.trial_by_hash(experiment.experiment_id, spec_hash(spec))
    if existing is not None:
        return Measurement(
            trial=existing.for_search(),
            verdict=ledger.verdict(experiment, existing),
            result=None,
        )
    frame, holes = _frame(ledger, experiment, holdout=False)
    result = evaluate_split(spec, frame, experiment.split, experiment.costs, holdout=False)
    trial = ledger.record_trial(
        experiment,
        spec,
        SegmentMetrics.from_result(result.train),
        SegmentMetrics.from_result(result.validation),
    )
    verdict = ledger.verdict(experiment, trial)
    return Measurement(
        trial=trial.for_search(), verdict=verdict, result=result, funding_holes=holes
    )


def promote(ledger: Ledger, experiment: Experiment, trial_id: int) -> Trial:
    """Apply the gate, and only then read and measure the holdout for this one trial.

    The train and validation figures are measured again on the holdout-bound
    bundle and must equal the ones filed. They are the same bars, and a bar's
    features never read past it, so the only way they differ is a store that
    was re-fetched or revised since the trial was measured — in which case
    the gate was passed on figures the store no longer gives, and it is
    refused by name rather than promoted.
    """
    ledger.require_stored(experiment)
    trial = ledger.trial(experiment.experiment_id, trial_id)
    verdict = ledger.verdict(experiment, trial)
    if not verdict.eligible:
        raise LedgerError(
            f"trial #{trial_id} of {experiment.experiment_id} cannot be promoted: "
            + "; ".join(verdict.blockers)
        )
    frame, _holes = _frame(ledger, experiment, holdout=True)
    # The tail was whole when the experiment was cut; a store revised since
    # could have lost it, and the holdout is measured on exactly those bars.
    require_measurable_tail(frame)
    result = evaluate_split(trial.spec, frame, experiment.split, experiment.costs, holdout=True)
    for filed, again in (
        (trial.train, SegmentMetrics.from_result(result.train)),
        (trial.validation, SegmentMetrics.from_result(result.validation)),
    ):
        if filed != again:
            raise LedgerError(
                f"the store no longer gives trial #{trial_id} the {filed.segment.name.value} "
                f"figures it was filed with (net sharpe {filed.net.sharpe:.4f} then, "
                f"{again.net.sharpe:.4f} now) — the history was re-fetched or revised since. "
                f"Its gate was passed on numbers this store cannot reproduce; measure the rule "
                f"in a new experiment."
            )
    if result.holdout is None:  # pragma: no cover - evaluate_split was told holdout=True
        raise LedgerError("the holdout was asked for and not measured")
    return ledger.promote(experiment, trial, SegmentMetrics.from_result(result.holdout))


def calibrate(
    ledger: Ledger, experiment: Experiment
) -> list[tuple[str, tuple[SegmentMetrics, ...]]]:
    """Measure every baseline on train and validation; record nothing (see :mod:`.baselines`)."""
    frame, _holes = _frame(ledger, experiment, holdout=False)
    rows = []
    for name, spec in baseline_specs():
        result = evaluate_split(spec, frame, experiment.split, experiment.costs, holdout=False)
        rows.append((name, tuple(SegmentMetrics.from_result(r) for r in result.results)))
    return rows
