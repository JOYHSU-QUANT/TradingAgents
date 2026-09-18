"""The one thing this package ever hands the live path: a qualitative signal.

Plan §7 / PR C1. Everything else here stays offline — this module takes the
coin's most recently promoted rule, replays its DECISIONS up to the newest bar
the store holds, cuts its selection-window figures into ordinal bands, and
writes one small JSON document. The trading daemon reads that document with
the standard library alone and renders it as a single analyst-input section,
behind a config switch that defaults to off.

Three properties are the whole design, and each is enforced here rather than
at the reader, because the reader cannot check any of them:

- **Qualitative.** No Sharpe, no drawdown percentage, no equity and no
  threshold crosses the seam. The bands are cut HERE, from figures that stay
  in the ledger. A prompt anchors a model on the numbers it is shown, and
  these are numbers measured on a different history over a window the prompt
  only names.
- **Dated to a bar, not to a clock.** ``as_of_ms`` is the close of the bar the
  decision was taken at. The reader judges freshness against its OWN newest
  bar, so a producer host whose clock drifts cannot make a stale document look
  current.
- **Refuse rather than soften.** No promoted rule, a store with holes, a rule
  whose newest decision could not read its features — each is a named refusal
  and no document is written. A document that exists is one that was believed
  when it was written; whether it is still fresh is the reader's question.

The document's shape and vocabulary are NOT declared here. They belong to
``ResearchSignal``, borrowed through :mod:`.upstream` from the package that
reads it, so the two sides cannot drift apart while both test suites stay
green.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Final

from .constants import MS_PER_DAY
from .dsl import Side
from .ledger import Experiment, Ledger, Trial
from .metrics import SegmentMetrics
from .upstream import (
    MAX_SIGNAL_AGE_INTERVALS,
    ResearchBias,
    ResearchConfidence,
    ResearchDrawdown,
    ResearchSignal,
    from_epoch_ms,
)

__all__ = [
    "CONFIDENCE_EDGES",
    "DRAWDOWN_EDGES",
    "SignalError",
    "build_signal",
    "describe_signal",
    "write_signal",
]


class SignalError(ValueError):
    """This store cannot answer what the live path would be told.

    A ``ValueError``, like :class:`~.vocabulary.SpecError`, so the CLI's
    existing named-refusal lane prints it and exits 1 without a new branch.
    """


# Where the selection window's return-to-volatility ratio is cut into three.
#
# The lower edge is deliberately ABOVE the promote gate rather than at it. A
# rule only reaches this module by clearing ``sharpe_base + k * ln(n)``, which
# starts at 1.0 and only rises, so nothing promoted can land under 1.0 and a
# band boundary there would name a member that never occurs. "weak" therefore
# means "cleared the gate and not much more", which is the honest reading of a
# rule at 1.1 — and the gate rising with the number of rules tried is exactly
# why the bands are NOT pinned to it: a late rule that had to clear a higher
# bar should not read as more confident for having been tried later.
#
# The numbers themselves are a convention, not a measurement, and they sit
# here in one place so that is visible. They were not fitted to anything.
CONFIDENCE_EDGES: Final = (1.5, 2.5)

# Where the selection window's deepest peak-to-trough fall is cut into three,
# as a fraction of equity. Same status as the edges above: a convention. Ten
# and twenty-five percent are round numbers a reader can hold, picked so the
# middle band is the ordinary answer rather than a rare one.
DRAWDOWN_EDGES: Final = (0.10, 0.25)

_CONFIDENCE_BANDS: Final = (
    ResearchConfidence.WEAK,
    ResearchConfidence.MEDIUM,
    ResearchConfidence.STRONG,
)
_DRAWDOWN_BANDS: Final = (
    ResearchDrawdown.SHALLOW,
    ResearchDrawdown.MODERATE,
    ResearchDrawdown.DEEP,
)


def build_signal(ledger: Ledger, coin: str) -> tuple[ResearchSignal, Experiment, Trial]:
    """``coin``'s promoted rule, as the live path would be told about it.

    Returns the signal with the experiment and trial it came from, so a caller
    can report what was promoted and under which cost model without looking it
    up a second time and risking a different answer.

    The replay reads the store's WHOLE history, past the holdout and up to the
    newest bar. That is not a hole in the holdout lock: the lock exists so a
    window's SCORE is never chosen on bars a later window will judge it by,
    and nothing here scores anything — every figure a band is cut from was
    measured under the lock when the trial was filed. What the replay needs
    the tail for is the SIDE, which is path dependent and therefore cannot be
    read off a window that ended weeks ago.
    """
    # Imported inside the function, like the CLI's own compute commands:
    # reaching the feature stack costs half a second of pandas, and a caller
    # that only wanted the band edges should not pay it.
    from .evaluator import load_bundle, replay_position
    from .features import FeatureFrame
    from .research import require_clean_history

    promotion = ledger.latest_promotion(coin)
    if promotion is None:
        raise SignalError(
            f"no rule has been promoted on {coin} in this store, so there is nothing to tell "
            f"the live path — promote one first, or point --db at the store that has one"
        )
    experiment, trial = promotion
    holdout = trial.holdout
    if holdout is None:  # pragma: no cover - the trials table's CHECK forbids it
        raise SignalError(
            f"trial #{trial.trial_id} of {experiment.experiment_id} is promoted with no holdout "
            f"figures; this store's trials table is inconsistent"
        )

    interval = experiment.split.interval
    bundle = load_bundle(ledger.store, coin=experiment.coin, interval=interval)
    # The same scan a measurement runs, for a sharper reason here: a replayed
    # side is path dependent, so a missing bar does not merely shorten the
    # history — it can silently change which side the rule is on today.
    require_clean_history(bundle, interval)
    frame = FeatureFrame(bundle, indicator_lookback=experiment.indicator_lookback)
    replay = replay_position(
        trial.spec, frame, experiment.costs, since_ms=experiment.split.train.start_ms
    )
    if replay.last_bar_unevaluable:
        raise SignalError(
            f"the newest {interval} bar in this store "
            f"({from_epoch_ms(replay.last_close_time).isoformat()}) left trial "
            f"#{trial.trial_id}'s rule with a condition it could not evaluate, so the side it "
            f"holds there is one it held EARLIER rather than one it just re-took — fetch the "
            f"missing history (`fetch`, then `gaps`) and run this again"
        )

    return (
        ResearchSignal(
            coin=experiment.coin,
            interval=interval,
            as_of_ms=replay.last_close_time,
            strategy_id=f"{experiment.experiment_id}#{trial.trial_id}",
            bias=_bias(replay.side),
            confidence=_band(trial.validation.net.sharpe, CONFIDENCE_EDGES, _CONFIDENCE_BANDS),
            drawdown=_band(trial.validation.net.max_drawdown, DRAWDOWN_EDGES, _DRAWDOWN_BANDS),
            eval_window_days=_window_days(trial.validation, "validation"),
            holdout_window_days=_window_days(holdout, "holdout"),
            notes=_notes(trial.validation, holdout),
        ),
        experiment,
        trial,
    )


def write_signal(path: str | Path, signal: ResearchSignal) -> Path:
    """Write ``signal`` to ``path`` as JSON, atomically; return where it landed.

    Through a temporary file and :func:`os.replace`, which is atomic on both
    platforms this runs on. The reader is a trading daemon on its own
    schedule: it must never be able to open a half-written document, because
    that costs a cycle its research section for no reason at all.
    """
    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    # The pid is in the temporary name so two producers cannot overwrite each
    # other's half-finished file. They can still race on the rename, and that
    # is harmless: a rename either happened or did not, and both documents are
    # complete and valid.
    temporary = target.with_name(f"{target.name}.{os.getpid()}.tmp")
    body = json.dumps(signal.to_document(), indent=2, sort_keys=True, allow_nan=False)
    try:
        temporary.write_text(body + "\n", encoding="utf-8")
        os.replace(temporary, target)
    finally:
        # A failed write leaves no litter beside the document the daemon reads.
        temporary.unlink(missing_ok=True)
    return target


def describe_signal(signal: ResearchSignal, experiment: Experiment, trial: Trial) -> list[str]:
    """What the operator is shown — including the two things the document omits.

    The cost model and the promotion stamp are not in the document and are not
    meant to be: the prompt has no use for either. The operator has. Whether
    the promoted rules were scored under MAKER fills is the standing
    precondition for turning the live switch on at all (plan §7), and it is
    answerable only from the experiment, so this prints it every time rather
    than leaving an operator to go and look it up.
    """
    return [
        f"rule: {signal.strategy_id} (family {trial.family}, promoted {trial.promoted_at})",
        f"scored under: {experiment.costs.fill_role.value} fills, "
        f"leverage {experiment.costs.leverage:g}",
        f"bias: {signal.bias.value} (decided at the {signal.interval} bar closing "
        f"{from_epoch_ms(signal.as_of_ms).isoformat()})",
        f"bands: confidence {signal.confidence.value}, drawdown {signal.drawdown.value}, "
        f"from {signal.eval_window_days}d selection and {signal.holdout_window_days}d held back",
        f"notes: {signal.notes}",
        # The bound is the READER's, borrowed rather than restated, so this
        # sentence cannot go on advertising a cadence the daemon stopped
        # honouring. Printed every run, not once: going stale is silent at
        # this end — it is the reader that drops the prompt section — so
        # whoever runs this by hand is the person who would otherwise never
        # learn that a schedule is what keeps the section alive.
        f"schedule: the reader refuses a document older than {MAX_SIGNAL_AGE_INTERVALS} of its "
        f"own {signal.interval} bars, so run this at least that often",
    ]


def _bias(side: Side | None) -> ResearchBias:
    """The replayed side in the document's vocabulary. Flat is a reading, not a gap."""
    if side is None:
        return ResearchBias.NEUTRAL
    return ResearchBias.LONG if side is Side.LONG else ResearchBias.SHORT


def _band(value, edges, bands):
    """``value`` cut into ``bands`` at ``edges``; each edge belongs to the band ABOVE it.

    Half-open the same way every other threshold in this package is, so a
    figure sitting exactly on an edge lands in the same band whoever reads it.

    ``strict=False`` is the point of the shape, not a shrug at it: there is
    one more band than there are edges, and the last band is what anything
    past the last edge falls into. The check at the bottom of this module
    holds that shape, so a table someone extends with a fourth band and no
    third edge fails at import rather than by silently never reporting it.
    """
    for edge, band in zip(edges, bands, strict=False):
        if value < edge:
            return band
    return bands[-1]


def _window_days(metrics: SegmentMetrics, what: str) -> int:
    """A measured window's length in whole days, rounded to the nearest.

    Whole days because the prompt says ``90 days``, and because a window is a
    span an operator chose in days rather than a figure anyone measures
    against. A window that rounds below one day is refused rather than
    reported as zero or nudged up to one: the document would then be
    describing a selection window that is not one, and the caller should hear
    that from here rather than from the DTO's own bound.
    """
    span_ms = metrics.segment.end_ms - metrics.segment.start_ms
    days = round(span_ms / MS_PER_DAY)
    if days < 1:
        raise SignalError(
            f"this experiment's {what} window spans {span_ms / MS_PER_DAY:.2f} days, and the "
            f"handoff document reports windows in whole days; a window this short is not one "
            f"the live path should be told about"
        )
    return days


def _notes(validation: SegmentMetrics, holdout: SegmentMetrics) -> str:
    """The one sentence the prompt prints: how the rule did OUT of sample.

    The most useful qualification of a promoted rule, and the one a reader of
    the prompt cannot derive from the two bands — both of those are cut from
    the window the rule was SELECTED on, where it was chosen for looking good.
    The held-back window is the only figure here that nothing chose on, and it
    is measured exactly once, which is also said.

    Comparative, never numeric, for the same reason the bands are.
    """
    ratio = "at or above" if holdout.net.sharpe >= validation.net.sharpe else "below"
    total = "positive" if holdout.net.total_return > 0 else "not positive"
    return (
        f"held-back window, measured once: net return {total}, return-to-volatility {ratio} "
        f"the selection window's; nothing since has been measured"
    )


# Import-time check, in the style of the sibling modules: a band table is
# ascending edges plus exactly one more band than edges. Get either wrong and
# ``_band`` still answers — with a band that can never be reached, or by
# reporting the wrong one for a whole range — which is precisely the failure
# nothing downstream could notice, since every answer is a legal word.
for _edges, _bands in ((CONFIDENCE_EDGES, _CONFIDENCE_BANDS), (DRAWDOWN_EDGES, _DRAWDOWN_BANDS)):
    if len(_bands) != len(_edges) + 1 or list(_edges) != sorted(_edges):
        raise RuntimeError(
            f"a band table needs ascending edges and one more band than edges, got "
            f"{_edges} and {[band.value for band in _bands]}"
        )
