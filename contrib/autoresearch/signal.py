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

import contextlib
import json
import logging
import os
from pathlib import Path
from typing import Final

from .constants import MS_PER_DAY
from .costs import FillRole
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


logger = logging.getLogger(__name__)


class SignalError(ValueError):
    """This store cannot answer what the live path would be told.

    A ``ValueError``, like :class:`~.vocabulary.SpecError`, so the CLI's
    existing named-refusal lane prints it and exits 1 without a new branch.
    """


# Where the selection window's return-to-volatility ratio is cut into three.
#
# The lower edge sits ABOVE the promote gate rather than at it, so "weak"
# reads as "cleared the gate and not much more". The gate is
# ``sharpe_base + k * ln(n)``, and ``Penalty.sharpe_base`` takes its value
# from ``ledger.SHARPE_BASE`` — a DEFAULT, not a floor: ``experiment
# --sharpe-base`` can set it lower, and the lowest band then covers rules
# under it, which is the operator's choice and still reads correctly. What
# the edges must NOT do is track the gate, because the gate rises with the
# number of rules tried, and a late rule that had to clear a higher bar
# should not read as more confident for having been tried later.
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


def build_signal(
    ledger: Ledger, coin: str, *, allow_taker: bool = False
) -> tuple[ResearchSignal, Experiment, Trial]:
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

    if experiment.costs.fill_role is not FillRole.MAKER and not allow_taker:
        # Plan §7's standing precondition for turning the live switch on at
        # all: after run 5 moved the paper lane to maker fills, a rule scored
        # under taker costs was selected against a cost model the account no
        # longer pays. That precondition was prose in three documents and a
        # line of stdout, which is not a guard — and the mistake it prevents
        # is the LIKELY state on the day the switch is flipped, not a
        # hypothetical. Refusing here keeps it entirely inside this package:
        # nothing extra crosses the seam, and the escape hatch is explicit
        # rather than implied.
        raise SignalError(
            f"{experiment.experiment_id} scored its trials under "
            f"{experiment.costs.fill_role.value} fills, and the live lane trades maker "
            f"(plan §7: promoted rules are re-run under maker costs before the prompt switch "
            f"goes on) — open the experiment again with `--fill-role maker` and promote there, "
            f"or pass --allow-taker to publish this anyway"
        )

    interval = experiment.split.interval
    bundle = load_bundle(ledger.store, coin=experiment.coin, interval=interval)
    # The same scan a measurement runs, for a sharper reason here: a replayed
    # side is path dependent, so a missing bar does not merely shorten the
    # history — it can silently change which side the rule is on today. Bar
    # and daily holes are refused by the scan; funding holes it only COUNTS,
    # and the replay's own unevaluable count is REPORTED below rather than
    # refused on — the comment there says why that asymmetry is deliberate.
    #
    # Known cost, stated rather than fixed: the scan starts at the bundle's
    # first bar while the replay starts at the experiment's train start, so a
    # hole older than the experiment refuses this command over a span the
    # answer does not depend on. That is a loud false refusal naming the span,
    # which is the safer direction to be wrong in.
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
    if replay.replayed_bars_unevaluable:
        # REPORTED, not refused — and the difference was learned in review. A
        # rule that cannot be evaluated does not go flat, it FREEZES on
        # whatever side it held, so a long run of missing settlements under a
        # rule that exits on funding can publish a side the rule left weeks
        # ago, which the newest bar alone cannot show. That hazard is real.
        #
        # But a threshold of "any bar at all", over a span that is usually the
        # whole store, refuses a store this package defines as HEALTHY:
        # ``require_clean_history`` tolerates funding holes by name ("the
        # venue skips a settlement now and then, and a store refused for it
        # would be unmeasurable"), the promote gate has no blocker on
        # unevaluable bars, and the scored windows print them as a note. A
        # guard stricter than the promotion that produced the trial makes this
        # command unusable — and the remedy it would name is impossible, since
        # ``fetch`` cannot invent a settlement the venue never posted.
        #
        # So the operator is told, and the one refusal kept is the one that
        # matches what the document actually CLAIMS: that the side is the one
        # the rule holds after its LATEST bar's decision.
        logger.warning(
            "trial #%s's rule could not be evaluated on %d of the %d %s bars replayed since %s, "
            "where it held whatever side it was already on instead of deciding. A few are "
            "settlements the venue skipped; a long run is a gap worth filling (`fetch`, then "
            "`gaps`) before today's side is trusted",
            trial.trial_id,
            replay.replayed_bars_unevaluable,
            replay.replayed_bars,
            interval,
            from_epoch_ms(experiment.split.train.start_ms).isoformat(),
        )

    return (
        ResearchSignal(
            coin=experiment.coin,
            interval=interval,
            as_of_ms=replay.last_close_time,
            # ``<experiment>#<trial>``. It cannot overflow the reader's
            # ``MAX_RESEARCH_TEXT_CHARS`` bound on a rendered field: the
            # ledger's own ``_EXPERIMENT_ID`` pattern caps a name at 64
            # characters and a trial id is a small autoincrement integer, so
            # the whole string is far inside it. A check here would be a
            # branch nothing can reach; the bound is real, and it is the
            # DTO's.
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
    # EVERY step that can touch the filesystem is inside the one wrap,
    # expanding the path and making the parent included. The obvious version
    # of this — wrapping only the write and the rename — left the likeliest
    # operator mistake of all escaping as a bare traceback: ``--out`` under a
    # directory the cron user cannot create. The same commit that named this
    # refusal also took ``OSError`` back out of the CLI's refusal family, so
    # there was nothing behind it any more.
    #
    # Named HERE, the way ``cli._read_spec`` names a spec it cannot open,
    # rather than by putting ``OSError`` in that family: the family's rule is
    # that every member already carries a sentence written for an operator,
    # and a bare errno naming a temporary file does not. Worse, ``requests``'
    # exceptions ARE ``OSError``s, so a blanket catch would print a transport
    # defect under ``fetch`` or ``research`` as though it were a mistake.
    temporary = None
    try:
        # ``expanduser`` raises ``RuntimeError`` — not ``OSError`` — for
        # ``~someuser`` with no such user, which is an operator's typo.
        target = Path(path).expanduser().resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        # The pid is in the temporary name so two producers cannot overwrite
        # each other's half-finished file. They can still race on the rename,
        # and that is harmless: a rename either happened or did not, and both
        # documents are complete and valid.
        temporary = target.with_name(f"{target.name}.{os.getpid()}.tmp")
        body = json.dumps(signal.to_document(), indent=2, sort_keys=True, allow_nan=False)
        temporary.write_text(body + "\n", encoding="utf-8")
        os.replace(temporary, target)
    except (OSError, RuntimeError, ValueError) as exc:
        raise SignalError(
            f"could not write the handoff document to {_shown(path)} — check that --out names a "
            f"writable path whose parent this user may create: {exc}"
        ) from exc
    finally:
        if temporary is not None:
            # A failed write leaves no litter beside the document the daemon
            # reads. Suppressed rather than raised: this runs on the way out
            # of a failure, and an errno from the cleanup would replace the
            # sentence saying what actually went wrong.
            with contextlib.suppress(OSError):
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


def _shown(path: str | Path) -> str:
    """``path`` as the operator should see it: expanded and absolute if it can be.

    The consumer half of this seam resolves before printing for the same
    reason — a relative path started from two working directories is two
    files — and it is worth as much here, because the producer's cron and the
    daemon's unit are exactly the two processes that disagree. Falls back to
    the configured string when resolving is itself what failed.
    """
    try:
        return str(Path(path).expanduser().resolve())
    except (OSError, RuntimeError, ValueError):
        return repr(path)


def _bias(side: Side | None) -> ResearchBias:
    """The replayed side in the document's vocabulary. Flat is a reading, not a gap."""
    if side is None:
        return ResearchBias.FLAT
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
    # STRICTLY ascending. ``sorted()`` alone accepts EQUAL adjacent edges, and
    # a repeated edge produces exactly the failure this check exists to catch:
    # ``_band`` still answers, with the band between the two equal edges
    # unreachable — and every answer it gives is a legal word, so nothing
    # downstream could notice.
    if len(_bands) != len(_edges) + 1 or any(
        later <= earlier for earlier, later in zip(_edges, _edges[1:], strict=False)
    ):
        raise RuntimeError(
            f"a band table needs strictly ascending edges and one more band than edges, got "
            f"{_edges} and {[band.value for band in _bands]}"
        )
