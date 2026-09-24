"""The scorecard: every recorded decision, marked against the price that followed.

Replay plan §3-7 fixes the definitions, and they are written here once:

- A decision is scored at two horizons, ONE bar and SIX bars on
  (:data:`HORIZONS` — 4h and 24h on the paper cadence). The later mark is
  the ``mark_price`` of the question decided ``k`` bars after this one —
  the nearest question within half a bar of ``at_ms + k × step``, matched
  by the decision INSTANT and not by row order, because cycles go missing
  (``api_failed``, a restart), and not by the closed bar's ``candle_end``,
  because the paper scheduler rolls (next cycle = last decision + 4h, never
  a clock boundary) while the mark is the price at the decision: pairing on
  the bar's stamp made a cycle that drifted across a boundary read as a
  missing cycle, and its "4h" mark a close minutes away (run 3, measured
  2026-09-23; decided the same day). A gap no question fills within the
  tolerance is read from the research store's candle closes instead, and
  the row says which it got. Two questions within one tolerance of each
  other (inclusive) cannot be paired unambiguously and the run is refused
  by name.
- Direction: the model's call is the ``target_side`` it asked for — on an
  approved or clamped ``set_target``, and equally on a REJECTED one, which
  the gate records as ``maintain_current`` with the refused side and margin
  preserved (``risk_gate._no_target_result``) — and the position's own side
  on a genuine ``maintain_current``; a fail-closed round has no call.
  ``long`` is a hit when the mark rose, ``short`` when it fell, and ``flat``
  — "it will not move" — when the absolute return stayed under the median
  absolute return, at that horizon, of every train and validation question
  with a later mark, answered or not (the band does not move when the
  holdout is opened; a horizon no question reaches has no band, and a flat
  call there is not judged).
- Net P&L, as a fraction of equity: ``exposure × return − cost``, where
  exposure is ``±margin_pct / 100 × leverage`` and the cost is the run's own
  fill model (:class:`~contrib.autoresearch.costs.CostModel`, plan §3-6)
  charged on the turnover from the position the decision was made from.
  No position is carried from one decision to the next: each is "what if
  this call were held k bars" (plan §3-4, the simple version).
- Two readings of every decision, so the summary can answer "the model
  called it / the rule let it through" as a 2×2: the MODEL's reading uses
  the requested margin and the model's side; the EXECUTED reading uses the
  approved margin when an order was created and the unchanged position
  otherwise (a rejection, a fail-closed round, a target inside the deadband).
- Confidence calibration (plan §3-8): the rows on which the model ASKED A
  TARGET (a ``set_target``, or a rejected one — the same bucket the clamp
  and rejection rates are over), ten buckets, the model's own hit rate per
  bucket.
- Three baselines beside the trader, over the same answered rows: always
  long at the row's margin cap, always flat, and the research radar's
  ``autoresearch_bias`` as if it were traded at the cap. Each carries its
  position from row to row and pays the turnover when its position changes
  (decided 2026-09-23).

The split (plan §3-9) is the research package's own, cut over the run's
span; a row in the holdout is scored only when the caller says so, and a
later mark past the loadable bound is treated as unavailable rather than
read — the same lock the evaluator keeps.

Everything here is a pure function of :class:`Question` / :class:`Answer`
records, so the past-papers command (plan PR 2) scores its answers through
the same code by building the same records; :func:`paired_hits` is the
comparison it will use (plan §3-11).
"""

from __future__ import annotations

import math
import statistics
from bisect import bisect_left
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Final, TypeVar

from .upstream import (
    MS_PER_DAY,
    CostModel,
    DecisionMode,
    RiskAction,
    SegmentName,
    SpecError,
    Split,
    TargetSide,
    from_epoch_ms,
    require_amount,
    require_number,
)

__all__ = [
    "BASELINES",
    "HORIZONS",
    "Answer",
    "Baseline",
    "CalibrationBucket",
    "HitStats",
    "HorizonSummary",
    "Outcome",
    "Paired",
    "PnlStats",
    "Question",
    "ScoreError",
    "Scorecard",
    "Scored",
    "Summary",
    "bar_open_ms",
    "build_split",
    "csv_table",
    "paired_hits",
    "score_run",
    "sign_test",
]

# Bars ahead a decision is marked at: one bar on, and six bars on.
HORIZONS: Final = (1, 6)

# The baselines, in report order.
BASELINES: Final = ("buy_hold", "flat", "research_bias")

# The buckets a decision is filed under: no call / the model asked / a maintain.
FAIL_CLOSED: Final = "fail_closed"
MODES: Final = (DecisionMode.SET_TARGET.value, DecisionMode.MAINTAIN_CURRENT.value, FAIL_CLOSED)

# Confidence is a fraction in [0, 1]; ten buckets, the top one closed.
_CALIBRATION_BUCKETS: Final = 10

_SIGN: Final = {TargetSide.LONG: 1, TargetSide.SHORT: -1, TargetSide.FLAT: 0}
_REFUSED: Final = (RiskAction.REJECTED, RiskAction.INVALID_FAIL_CLOSED)

_E = TypeVar("_E", DecisionMode, RiskAction, TargetSide)


class ScoreError(ValueError):
    """A record the scorecard cannot score, and the sentence says which field."""


def _number(value: object, what: str) -> float:
    """A finite number — the research package's one numeric guard, in this module's error.

    A ``Decimal`` is taken as its float first: the gate's own results carry
    ``Decimal`` margins and confidences, and the past-papers command (plan
    PR 2) builds these records from them in-process.
    """
    if isinstance(value, Decimal):
        value = float(value)
    try:
        return require_number(value, what)
    except SpecError as exc:
        raise ScoreError(str(exc)) from exc


def _amount(value: object, what: str, *, positive: bool = False) -> float:
    """A non-negative (or positive) finite number, ``Decimal`` taken as :func:`_number` takes it."""
    if isinstance(value, Decimal):
        value = float(value)
    try:
        return require_amount(value, what, positive=positive)
    except ValueError as exc:
        raise ScoreError(str(exc)) from exc


def _enum(value: object, kind: type[_E], what: str) -> _E:
    """``value`` as a member of ``kind`` (a member passes through), refused by field."""
    try:
        return kind(value)
    except ValueError as exc:
        raise ScoreError(f"{what}: {exc}") from exc


@dataclass(frozen=True)
class Question:
    """One ``ai_inputs`` row: the facts a decision was made from.

    ``at_ms`` is the DECISION instant as epoch ms (the row's ``timestamp``,
    when ``mark`` was read), the instant later marks are paired to and the
    split is placed by. Margins are the store's percent: ``max_margin_pct``
    is the cap the baselines trade at (``(0, 100]``, the risk config's own
    bound), while ``current_margin_pct`` is imputed from the
    books (``notional / leverage / equity``) and exceeds 100 on a losing
    position, so it is only held to be non-negative and to agree with the
    side (flat carries none, a sized side carries some). ``leverage`` is the
    configured leverage the exposure is scaled by. ``reports_present`` is
    ``None`` when nobody looked.
    """

    input_id: str
    at_ms: int
    mark: float
    current_side: TargetSide
    current_margin_pct: float
    leverage: float
    max_margin_pct: float
    research_bias: TargetSide | None = None
    prompt_version: str | None = None
    model: str | None = None
    context_shape: str | None = None
    reports_present: bool | None = None
    account_equity: float | None = None
    strategy_id: str | None = None
    attempt_id: str | None = None

    def __post_init__(self) -> None:
        me = self.input_id
        if isinstance(self.at_ms, bool) or not isinstance(self.at_ms, int):
            raise ScoreError(f"{me}: at_ms is epoch ms, got {self.at_ms!r}")
        set_ = object.__setattr__
        set_(self, "mark", _amount(self.mark, f"{me}: mark", positive=True))
        set_(self, "current_side", _enum(self.current_side, TargetSide, f"{me}: current_side"))
        if self.research_bias is not None:
            set_(self, "research_bias", _enum(self.research_bias, TargetSide, f"{me}: bias"))
        cap = _number(self.max_margin_pct, f"{me}: max_margin_pct")
        if not 0 < cap <= 100:
            raise ScoreError(f"{me}: max_margin_pct is a percent in (0, 100], got {cap!r}")
        set_(self, "max_margin_pct", cap)
        set_(self, "current_margin_pct", _amount(self.current_margin_pct, f"{me}: current_margin_pct"))
        set_(self, "leverage", _amount(self.leverage, f"{me}: leverage", positive=True))
        if (self.current_side is TargetSide.FLAT) != (self.current_margin_pct == 0):
            raise ScoreError(
                f"{me}: a {self.current_side.value} position with margin "
                f"{self.current_margin_pct!r} cannot be scored (flat carries none, a sized side "
                "carries some)"
            )
        if self.account_equity is not None:
            set_(self, "account_equity", _number(self.account_equity, f"{me}: equity"))

    @property
    def current_exposure(self) -> float:
        """Signed fraction of equity at risk: ``±margin / 100 × leverage``."""
        return _SIGN[self.current_side] * self.current_margin_pct / 100.0 * self.leverage


@dataclass(frozen=True)
class Answer:
    """One ``ai_outputs`` row: what the model said and what the gate did with it.

    The same record the past-papers command will build from its own store
    (plan PR 2), so a replayed answer and a recorded one score through one
    function. The shape is the gate's (``RiskGateResult.__post_init__`` and
    ``_no_target_result``), and the checks below mirror what it guarantees
    on the way into the store: a ``set_target`` is approved or clamped and
    fully sized, with the approved margin never above the requested one;
    every rejection and every fail-closed round is recorded as
    ``maintain_current`` with no approved margin and no order — a REJECTED
    one keeping the side and margin it refused, a genuine maintain carrying
    neither, a fail-closed one carrying whatever the parser salvaged (which
    is never read as a call). ``order_created`` is what separates an
    approved target from an executed one: an approved target inside the
    deadband changed nothing.
    """

    input_id: str
    decision_mode: DecisionMode
    target_side: TargetSide | None
    requested_margin_pct: float | None
    approved_margin_pct: float | None
    risk_action: RiskAction
    risk_reason: str | None
    confidence: float | None
    order_created: bool
    no_order_reason: str | None = None

    def __post_init__(self) -> None:
        me = self.input_id
        set_ = object.__setattr__
        set_(self, "decision_mode", _enum(self.decision_mode, DecisionMode, f"{me}: decision_mode"))
        set_(self, "risk_action", _enum(self.risk_action, RiskAction, f"{me}: risk_action"))
        if self.target_side is not None:
            set_(self, "target_side", _enum(self.target_side, TargetSide, f"{me}: target_side"))
        for name, bound in (
            ("requested_margin_pct", 100),
            ("approved_margin_pct", 100),
            ("confidence", 1),
        ):
            value = getattr(self, name)
            if value is None:
                continue
            value = _number(value, f"{me}: {name}")
            if not 0 <= value <= bound:
                raise ScoreError(f"{me}: {name} must be in [0, {bound}], got {value!r}")
            set_(self, name, value)
        requested, approved = self.requested_margin_pct, self.approved_margin_pct
        sized = self.target_side is not None and requested is not None
        if self.decision_mode is DecisionMode.SET_TARGET:
            if self.risk_action in _REFUSED:
                raise ScoreError(
                    f"{me}: a rejected/fail-closed decision is recorded as maintain_current, "
                    "never as set_target"
                )
            if not sized or approved is None:
                raise ScoreError(f"{me}: a set_target carries a side and both margins")
            assert requested is not None
            if approved > requested:
                raise ScoreError(f"{me}: approved margin can never exceed requested")
            if self.risk_action is RiskAction.CLAMPED and approved >= requested:
                raise ScoreError(f"{me}: a clamped target strictly reduces the margin")
            if self.risk_action is RiskAction.APPROVED and approved != requested:
                raise ScoreError(f"{me}: an approved target keeps approved == requested")
        else:
            if self.risk_action is RiskAction.CLAMPED:
                raise ScoreError(f"{me}: only a set_target can be clamped")
            if approved is not None:
                raise ScoreError(f"{me}: a maintain_current carries no approved margin")
            if self.order_created:
                raise ScoreError(f"{me}: a maintain_current never creates an order")
            if self.risk_action is RiskAction.REJECTED and not sized:
                raise ScoreError(f"{me}: a rejection keeps the side and margin it refused")
            if self.risk_action is RiskAction.APPROVED and sized:
                raise ScoreError(f"{me}: a maintain_current asks for no target")
        if self.order_created == (self.no_order_reason is not None):
            raise ScoreError(f"{me}: exactly one of order_created / no_order_reason is set")

    @property
    def fail_closed(self) -> bool:
        return self.risk_action is RiskAction.INVALID_FAIL_CLOSED

    @property
    def asked_target(self) -> bool:
        """Whether the model asked for a target: a sized ``set_target``, or a REJECTED one.

        The gate collapses a rejection to ``maintain_current`` and keeps the
        refused side beside it (pinned above), so the mode alone would file
        every rejection as a maintain.
        """
        return self.decision_mode is DecisionMode.SET_TARGET or (
            self.risk_action is RiskAction.REJECTED
        )

    @property
    def mode(self) -> str:
        """The bucket the summary files this answer under (one of :data:`MODES`)."""
        if self.fail_closed:
            return FAIL_CLOSED
        if self.asked_target:
            return DecisionMode.SET_TARGET.value
        return DecisionMode.MAINTAIN_CURRENT.value


@dataclass(frozen=True)
class Outcome:
    """What one horizon says about one decision. ``None`` where nothing can be said.

    ``ret`` is ``None`` when no later mark exists (the run ended, the mark
    found is past the loadable bound, or neither the store nor the research
    candles hold one within the tolerance). The model-side fields are
    ``None`` on a fail-closed row; on an unanswered row both readings are
    ``None`` — there was no decision to read, whatever the price did.
    """

    bars: int
    later_mark: float | None
    source: str | None
    ret: float | None
    ai_hit: bool | None
    executed_hit: bool | None
    ai_pnl: float | None
    executed_pnl: float | None


@dataclass(frozen=True)
class Scored:
    """One question, its answer (if any), and the reading at every horizon (keyed by bars)."""

    question: Question
    answer: Answer | None
    segment: SegmentName | None
    ai_side: TargetSide | None
    executed_side: TargetSide
    ai_exposure: float | None
    executed_exposure: float
    flip: bool
    outcomes: Mapping[int, Outcome]

    @property
    def answered(self) -> bool:
        return self.answer is not None

    @property
    def fail_closed(self) -> bool:
        return self.answer is not None and self.answer.fail_closed

    @property
    def clamped(self) -> bool:
        return self.answer is not None and self.answer.risk_action is RiskAction.CLAMPED

    @property
    def rejected(self) -> bool:
        return self.answer is not None and self.answer.risk_action is RiskAction.REJECTED

    def outcome(self, bars: int) -> Outcome:
        return self.outcomes[bars]


# -- the grid and the split ------------------------------------------------------


def bar_open_ms(at_ms: int, step_ms: int) -> int:
    """The open of the bar the instant falls in: ``at_ms`` floored to the grid.

    The one quantity a question is placed on a split by — the split's edges
    are bar opens — and the span :func:`build_split` cuts is built from.
    """
    return at_ms - at_ms % step_ms


def build_split(questions: Sequence[Question], *, interval: str, step_ms: int) -> Split:
    """The research split cut over the span the questions cover.

    From the open of the bar the first question falls in to the close of
    the bar the last one falls in, so every question is inside exactly one
    segment and the last one is inside the holdout's final bar rather than
    on its edge. Raises :class:`~contrib.autoresearch.split.SplitError` on a
    run too short to cut three segments from (fewer than four bars).
    """
    if not questions:
        raise ScoreError("no questions to split")
    opens = [bar_open_ms(q.at_ms, step_ms) for q in questions]
    return Split.by_shares(interval, start_ms=min(opens), end_ms=max(opens) + step_ms)


def _segment_of(split: Split | None, at_ms: int, step_ms: int) -> SegmentName | None:
    """The segment holding the question's bar, or ``None`` without a split.

    A row no segment claims is refused rather than scored: with a split
    present, an unplaced row would slip past the holdout lock.
    """
    if split is None:
        return None
    opened = bar_open_ms(at_ms, step_ms)
    for segment in split.ordered:
        if segment.start_ms <= opened < segment.end_ms:
            return segment.name
    raise ScoreError(
        f"no segment of the split holds the bar opening at {from_epoch_ms(opened).isoformat()}"
    )


# -- pairing ------------------------------------------------------------------------


@dataclass(frozen=True)
class _Series:
    """Stamped prices in time order, answering "the one nearest ``target``, within ``tolerance``"."""

    stamps: tuple[int, ...]
    prices: tuple[float, ...]

    @classmethod
    def of(cls, points: Mapping[int, float]) -> _Series:
        """From ``{stamp: price}``; every price a finite positive number, every stamp an int."""
        for stamp in points:
            if isinstance(stamp, bool) or not isinstance(stamp, int):
                raise ScoreError(f"a price stamp is epoch ms, got {stamp!r}")
        stamps = tuple(sorted(points))
        prices = tuple(_amount(points[s], f"price at {s}", positive=True) for s in stamps)
        return cls(stamps, prices)

    def nearest(self, target: int, *, tolerance: int) -> tuple[int, float] | None:
        """``(stamp, price)`` of the stamp nearest ``target`` within ``tolerance`` (inclusive)."""
        index = bisect_left(self.stamps, target)
        candidates = [
            i
            for i in (index - 1, index)
            if 0 <= i < len(self.stamps) and abs(self.stamps[i] - target) <= tolerance
        ]
        if not candidates:
            return None
        winner = min(candidates, key=lambda i: abs(self.stamps[i] - target))
        return self.stamps[winner], self.prices[winner]


# -- scoring -----------------------------------------------------------------


def _side_of(exposure: float) -> TargetSide:
    if exposure > 0:
        return TargetSide.LONG
    if exposure < 0:
        return TargetSide.SHORT
    return TargetSide.FLAT


def _hit(side: TargetSide, ret: float, flat_band: float | None) -> bool | None:
    """Whether a call was right; ``None`` for a flat call at a horizon with no band to judge it by."""
    if side is TargetSide.LONG:
        return ret > 0
    if side is TargetSide.SHORT:
        return ret < 0
    return None if flat_band is None else abs(ret) < flat_band


def _pnl(exposure: float, ret: float, turnover: float, costs: CostModel) -> float:
    fee, slippage = costs.fill_cost(turnover)
    return exposure * ret - fee - slippage


@dataclass(frozen=True)
class _Reading:
    """The two exposures a decision is read at, and the turnover each implies."""

    ai_side: TargetSide | None
    ai_exposure: float | None
    ai_turnover: float
    executed_exposure: float
    executed_turnover: float

    @property
    def executed_side(self) -> TargetSide:
        return _side_of(self.executed_exposure)


def _read(question: Question, answer: Answer | None) -> _Reading:
    current = question.current_exposure
    if answer is None or answer.fail_closed:
        return _Reading(None, None, 0.0, current, 0.0)
    if not answer.asked_target:
        return _Reading(question.current_side, current, 0.0, current, 0.0)
    assert answer.target_side is not None and answer.requested_margin_pct is not None
    sign = _SIGN[answer.target_side]
    requested = sign * answer.requested_margin_pct / 100.0 * question.leverage
    executed = current
    if answer.order_created:
        assert answer.approved_margin_pct is not None
        executed = sign * answer.approved_margin_pct / 100.0 * question.leverage
    return _Reading(
        answer.target_side, requested, abs(requested - current), executed, abs(executed - current)
    )


_Mark = tuple[float | None, str | None]


def _later_marks(
    questions: Sequence[Question],
    *,
    step_ms: int,
    tolerance_ms: int,
    store: _Series,
    research: _Series,
    until_ms: int | None,
) -> dict[str, dict[int, _Mark]]:
    """Per question and horizon, ``(later mark, source)``: the store's question first, else a close.

    The lock is applied ONCE, to the candidate chosen: a store question
    inside the tolerance is the mark whether or not the bound allows it,
    and when it does not the answer is "unavailable" — never the research
    close beside it. Falling through would score the same validation row
    on different prices locked and opened.
    """

    def lookup(target: int) -> _Mark:
        for series, source in ((store, "store"), (research, "research")):
            found = series.nearest(target, tolerance=tolerance_ms)
            if found is not None:
                stamp, mark = found
                if until_ms is not None and stamp > until_ms:
                    return None, None
                return mark, source
        return None, None

    return {
        q.input_id: {bars: lookup(q.at_ms + bars * step_ms) for bars in HORIZONS}
        for q in questions
    }


@dataclass(frozen=True)
class Scorecard:
    """Every scored row of one run, plus the terms it was scored under."""

    rows: tuple[Scored, ...]
    step_ms: int
    costs: CostModel
    flat_bands: Mapping[int, float | None]  # None: no locked row has a later mark at that horizon
    split: Split | None
    holdout_read: bool

    @property
    def bars_per_year(self) -> float:
        return 365 * MS_PER_DAY / self.step_ms

    def horizon_label(self, bars: int) -> str:
        hours = bars * self.step_ms / 3_600_000
        return f"{hours:g}h"

    def summary(self) -> Summary:
        return _summarise(self)


def score_run(
    questions: Iterable[Question],
    answers: Iterable[Answer],
    *,
    step_ms: int,
    costs: CostModel,
    research_closes: Mapping[int, float] | None = None,
    split: Split | None = None,
    holdout: bool = False,
    tolerance_ms: int | None = None,
) -> Scorecard:
    """Score every question of a run. The one entry point.

    ``research_closes`` maps a candle's ``close_time`` to its close, read
    from the research store and consulted only where no question sits
    within ``tolerance_ms`` (half a bar by default) of the instant a later
    mark is wanted at. With a ``split``, holdout rows are dropped unless
    ``holdout`` is set, and later marks past the loadable bound are
    unavailable either way — read them and the validation score of the last
    day leaks the holdout's first day.
    """
    if step_ms <= 0:
        raise ScoreError(f"step_ms must be > 0, got {step_ms!r}")
    tolerance = step_ms // 2 if tolerance_ms is None else tolerance_ms
    if not 0 <= tolerance < step_ms:
        raise ScoreError(f"tolerance_ms must be in [0, step_ms), got {tolerance_ms!r}")
    by_input: dict[str, Answer] = {}
    for given in answers:
        if given.input_id in by_input:
            raise ScoreError(f"{given.input_id}: answered twice")
        by_input[given.input_id] = given
    ordered = sorted(questions, key=lambda q: q.at_ms)
    seen: set[str] = set()
    for question in ordered:
        if question.input_id in seen:
            raise ScoreError(f"{question.input_id}: asked twice")
        seen.add(question.input_id)
    for earlier, later_q in zip(ordered, ordered[1:], strict=False):
        if later_q.at_ms - earlier.at_ms <= tolerance:
            raise ScoreError(
                f"{earlier.input_id} and {later_q.input_id}: two questions within "
                f"{tolerance / 3_600_000:g}h of each other cannot be paired unambiguously"
            )
    unmatched = set(by_input) - seen
    if unmatched:
        raise ScoreError(f"answers without a question: {sorted(unmatched)}")

    store = _Series.of({q.at_ms: q.mark for q in ordered})
    research = _Series.of(research_closes or {})
    segments = {q.input_id: _segment_of(split, q.at_ms, step_ms) for q in ordered}
    locked_rows = [q for q in ordered if segments[q.input_id] is not SegmentName.HOLDOUT]
    locked = _later_marks(
        locked_rows,
        step_ms=step_ms,
        tolerance_ms=tolerance,
        store=store,
        research=research,
        until_ms=None if split is None else split.loadable_until(),
    )
    # The band is a fact about the train and validation rows under the lock,
    # whether or not the holdout is being read: opening the holdout must not
    # move the bar every earlier flat call was judged against.
    flat_bands: dict[int, float | None] = {
        bars: statistics.median(moves) if moves else None
        for bars in HORIZONS
        for moves in [
            [
                abs(mark / q.mark - 1.0)
                for q in locked_rows
                for mark, _ in [locked[q.input_id][bars]]
                if mark is not None
            ]
        ]
    }
    if holdout:
        kept = ordered
        later = _later_marks(
            kept,
            step_ms=step_ms,
            tolerance_ms=tolerance,
            store=store,
            research=research,
            until_ms=None if split is None else split.loadable_until(holdout=True),
        )
    else:
        kept, later = locked_rows, locked

    rows: list[Scored] = []
    for question in kept:
        answer = by_input.get(question.input_id)
        reading = _read(question, answer)
        outcomes: dict[int, Outcome] = {}
        for bars in HORIZONS:
            mark, source = later[question.input_id][bars]
            if mark is None:
                outcomes[bars] = Outcome(bars, None, None, None, None, None, None, None)
                continue
            ret = mark / question.mark - 1.0
            band = flat_bands[bars]
            ai_hit = ai_pnl = None
            if reading.ai_side is not None and reading.ai_exposure is not None:
                ai_hit = _hit(reading.ai_side, ret, band)
                ai_pnl = _pnl(reading.ai_exposure, ret, reading.ai_turnover, costs)
            executed_hit = executed_pnl = None
            if answer is not None:
                executed_hit = _hit(reading.executed_side, ret, band)
                executed_pnl = _pnl(
                    reading.executed_exposure, ret, reading.executed_turnover, costs
                )
            outcomes[bars] = Outcome(
                bars, mark, source, ret, ai_hit, executed_hit, ai_pnl, executed_pnl
            )
        rows.append(
            Scored(
                question=question,
                answer=answer,
                segment=segments[question.input_id],
                ai_side=reading.ai_side,
                executed_side=reading.executed_side,
                ai_exposure=reading.ai_exposure,
                executed_exposure=reading.executed_exposure,
                flip=_SIGN[question.current_side] * _SIGN[reading.executed_side] < 0,
                outcomes=outcomes,
            )
        )
    return Scorecard(
        rows=tuple(rows),
        step_ms=step_ms,
        costs=costs,
        flat_bands=flat_bands,
        split=split,
        holdout_read=holdout,
    )


# -- summary -----------------------------------------------------------------


@dataclass(frozen=True)
class HitStats:
    n: int
    hits: int

    @property
    def rate(self) -> float | None:
        return None if self.n == 0 else self.hits / self.n

    def __str__(self) -> str:
        rate = "n/a" if self.rate is None else f"{self.rate:.1%}"
        return f"{rate} ({self.hits}/{self.n})"


@dataclass(frozen=True)
class PnlStats:
    """Net P&L over a series of decisions, each a fraction of equity.

    ``sharpe`` annualises the per-decision mean over its deviation by the
    number of such horizons in a year — ``bars_per_year / bars`` — and is 0
    with fewer than two values or no deviation, the research evaluator's
    convention. At the longer horizon consecutive decisions overlap, so the
    deviation is understated and the figure is a ranking aid, not a claim;
    ``overlapping`` says so in the printed line.
    """

    n: int
    total: float
    mean: float
    sharpe: float
    overlapping: bool = False

    def __str__(self) -> str:
        caveat = " (overlapping, ranking only)" if self.overlapping else ""
        if self.n < 2:
            caveat = " (n<2)" + caveat
        return (
            f"total {self.total:+.2%}, mean {self.mean:+.3%}/decision, "
            f"sharpe {self.sharpe:.2f}{caveat}"
        )


@dataclass(frozen=True)
class CalibrationBucket:
    bucket: int
    hits: HitStats

    @property
    def label(self) -> str:
        lo = self.bucket / _CALIBRATION_BUCKETS
        hi = (self.bucket + 1) / _CALIBRATION_BUCKETS
        closed = self.bucket == _CALIBRATION_BUCKETS - 1
        return f"[{lo:.1f}, {hi:.1f}{']' if closed else ')'}"


@dataclass(frozen=True)
class Baseline:
    name: str
    hits: HitStats
    pnl: PnlStats


@dataclass(frozen=True)
class HorizonSummary:
    bars: int
    label: str
    flat_band: float | None
    marks_from_research: int
    executed: HitStats
    ai: HitStats
    executed_by_mode: Mapping[str, HitStats]
    executed_pnl: PnlStats
    ai_pnl: PnlStats
    two_by_two: Mapping[str, int]
    calibration: tuple[CalibrationBucket, ...]
    baselines: tuple[Baseline, ...]


@dataclass(frozen=True)
class Summary:
    questions: int
    answered: int
    fail_closed: int
    fail_closed_by_reason: Mapping[str, int]
    clamped: int
    rejected: int
    flips: int
    set_target: int
    maintain_current: int
    segments: Mapping[str, int]
    regimes: Mapping[str, int]
    reports_present: int | None
    horizons: tuple[HorizonSummary, ...]

    @property
    def unanswered(self) -> int:
        return self.questions - self.answered

    def describe(self, card: Scorecard) -> list[str]:
        """The report, one fact per line."""
        reasons = ", ".join(f"{k} {v}" for k, v in sorted(self.fail_closed_by_reason.items()))
        regimes = ", ".join(f"{regime} {n}" for regime, n in self.regimes.items())
        lines = [
            f"decisions: {self.questions} questions, {self.answered} answered, "
            f"{self.unanswered} unanswered",
            # One run-id can span more than one prompt segment (RUNBOOK §4);
            # a card pooled over two regimes has to say so.
            f"regimes (prompt_version/model/context_shape): {regimes}",
            f"fail-closed: {_rate(self.fail_closed, self.answered)}"
            + (f" ({reasons})" if reasons else ""),
            f"asked a target: {self.set_target} (set_target, or rejected), clamped "
            f"{_rate(self.clamped, self.set_target)}, rejected "
            f"{_rate(self.rejected, self.set_target)}; maintained: {self.maintain_current}",
            f"flips: {_rate(self.flips, self.answered)}",
            # Spelled here rather than through ``CostModel.describe()``: its
            # tail ("funding settled hourly, leverage 1") describes the
            # evaluator's account, which the scorecard does not keep.
            f"costs: {card.costs.fill_role.value} fills at {card.costs.fee_rate:g} fee + "
            f"{card.costs.slippage_bps:g} bps slippage; exposure = margin x configured leverage",
        ]
        if card.split is not None:
            lines.extend(card.split.describe())
            counted = ", ".join(f"{name} {n}" for name, n in self.segments.items())
            lines.append(
                f"segments (questions): {counted}"
                + (" -- HOLDOUT READ" if card.holdout_read else " (holdout not read)")
            )
        if self.reports_present is not None:
            lines.append(
                f"questions with a .reports.json beside the payload: "
                f"{self.reports_present}/{self.questions}"
            )
        for horizon in self.horizons:
            lines.append(f"-- {horizon.label} ahead ({horizon.bars} bar(s)) --")
            band = "n/a" if horizon.flat_band is None else f"+/-{horizon.flat_band:.3%}"
            lines.append(
                f"  flat band {band}; answered rows marked from the research store: "
                f"{horizon.marks_from_research}"
            )
            lines.append(f"  executed hit {horizon.executed}; model hit {horizon.ai}")
            for mode, stats in horizon.executed_by_mode.items():
                lines.append(f"    executed by {mode}: {stats}")
            lines.append(f"  executed pnl: {horizon.executed_pnl}")
            lines.append(f"  model pnl:    {horizon.ai_pnl}")
            grid = horizon.two_by_two
            lines.append(
                f"  model vs rule: both hit {grid['both']}, model only {grid['ai_only']}, "
                f"rule only {grid['rule_only']}, neither {grid['neither']}"
            )
            for bucket in horizon.calibration:
                lines.append(f"  confidence {bucket.label}: {bucket.hits}")
            for baseline in horizon.baselines:
                lines.append(f"  baseline {baseline.name}: hit {baseline.hits}; {baseline.pnl}")
        return lines


def _rate(count: int, of: int) -> str:
    return f"{count}/{of}" + ("" if of == 0 else f" ({count / of:.1%})")


def _sharpe(values: Sequence[float], periods_per_year: float) -> float:
    if len(values) < 2:
        return 0.0
    deviation = statistics.stdev(values)
    if deviation == 0:
        return 0.0
    return statistics.fmean(values) / deviation * math.sqrt(periods_per_year)


def _pnl_stats(values: Sequence[float], card: Scorecard, bars: int) -> PnlStats:
    total = sum(values)
    mean = total / len(values) if values else 0.0
    return PnlStats(len(values), total, mean, _sharpe(values, card.bars_per_year / bars), bars > 1)


def _hit_stats(flags: Iterable[bool | None]) -> HitStats:
    known = [flag for flag in flags if flag is not None]
    return HitStats(len(known), sum(known))


def _baseline(
    name: str,
    rows: Sequence[Scored],
    bars: int,
    exposure_of: Callable[[Scored], float | None],
    card: Scorecard,
) -> Baseline:
    """One baseline traded through the answered rows in time order, paying its own turnover.

    A row the baseline has no view on (a ``None`` exposure — the radar's
    bias missing) is skipped, and the baseline's position stays where it
    was, so the next row's turnover is measured from there. A row with no
    later mark moves the position too, and the turnover it paid is carried
    to the next row that is scored rather than dropped.
    """
    hits: list[bool | None] = []
    pnls: list[float] = []
    held = owed = 0.0
    for row in rows:
        exposure = exposure_of(row)
        if exposure is None:
            continue
        ret = row.outcomes[bars].ret
        owed += abs(exposure - held)
        held = exposure
        if ret is None:
            continue
        hits.append(_hit(_side_of(exposure), ret, card.flat_bands[bars]))
        pnls.append(_pnl(exposure, ret, owed, card.costs))
        owed = 0.0
    return Baseline(name, _hit_stats(hits), _pnl_stats(pnls, card, bars))


def _cap(row: Scored) -> float:
    return row.question.max_margin_pct / 100.0 * row.question.leverage


def _bias_exposure(row: Scored) -> float | None:
    bias = row.question.research_bias
    return None if bias is None else _SIGN[bias] * _cap(row)


def _two_by_two(outcomes: Iterable[Outcome]) -> dict[str, int]:
    grid = {"both": 0, "ai_only": 0, "rule_only": 0, "neither": 0}
    for o in outcomes:
        if o.ai_hit is None or o.executed_hit is None:
            continue
        key = {
            (True, True): "both",
            (True, False): "ai_only",
            (False, True): "rule_only",
            (False, False): "neither",
        }[(o.ai_hit, o.executed_hit)]
        grid[key] += 1
    return grid


def _calibration(pairs: Sequence[tuple[Answer, Outcome]]) -> tuple[CalibrationBucket, ...]:
    buckets: dict[int, list[bool]] = {}
    for answer, outcome in pairs:
        if not answer.asked_target or answer.confidence is None or outcome.ai_hit is None:
            continue
        index = min(int(answer.confidence * _CALIBRATION_BUCKETS), _CALIBRATION_BUCKETS - 1)
        buckets.setdefault(index, []).append(outcome.ai_hit)
    return tuple(CalibrationBucket(index, _hit_stats(flags)) for index, flags in sorted(buckets.items()))


def _summarise(card: Scorecard) -> Summary:
    rows = card.rows
    answered = [(row, row.answer) for row in rows if row.answer is not None]
    modes = Counter(answer.mode for _, answer in answered)
    reasons = Counter(
        answer.risk_reason or "?" for _, answer in answered if answer.fail_closed
    )
    segments = Counter(row.segment.value for row in rows if row.segment is not None)
    regimes = Counter(
        "/".join(
            "?" if value is None else value
            for value in (q.prompt_version, q.model, q.context_shape)
        )
        for q in (row.question for row in rows)
    )
    checked = [row.question.reports_present for row in rows]
    reports = None if all(flag is None for flag in checked) else sum(bool(flag) for flag in checked)

    horizons = []
    answered_rows = [row for row, _ in answered]
    for bars in HORIZONS:
        outcomes = [(row, answer, row.outcomes[bars]) for row, answer in answered]
        horizons.append(
            HorizonSummary(
                bars=bars,
                label=card.horizon_label(bars),
                flat_band=card.flat_bands[bars],
                marks_from_research=sum(1 for _, _, o in outcomes if o.source == "research"),
                executed=_hit_stats(o.executed_hit for _, _, o in outcomes),
                ai=_hit_stats(o.ai_hit for _, _, o in outcomes),
                executed_by_mode={
                    mode: _hit_stats(o.executed_hit for _, a, o in outcomes if a.mode == mode)
                    for mode in MODES
                },
                executed_pnl=_pnl_stats(
                    [o.executed_pnl for _, _, o in outcomes if o.executed_pnl is not None], card, bars
                ),
                ai_pnl=_pnl_stats([o.ai_pnl for _, _, o in outcomes if o.ai_pnl is not None], card, bars),
                two_by_two=_two_by_two(o for _, _, o in outcomes),
                calibration=_calibration([(a, o) for _, a, o in outcomes]),
                baselines=(
                    _baseline("buy_hold", answered_rows, bars, _cap, card),
                    _baseline("flat", answered_rows, bars, lambda row: 0.0, card),
                    _baseline("research_bias", answered_rows, bars, _bias_exposure, card),
                ),
            )
        )
    return Summary(
        questions=len(rows),
        answered=len(answered),
        fail_closed=modes[FAIL_CLOSED],
        fail_closed_by_reason=dict(reasons),
        clamped=sum(1 for row, _ in answered if row.clamped),
        rejected=sum(1 for row, _ in answered if row.rejected),
        flips=sum(1 for row, _ in answered if row.flip),
        set_target=modes[DecisionMode.SET_TARGET.value],
        maintain_current=modes[DecisionMode.MAINTAIN_CURRENT.value],
        segments=dict(segments),
        regimes=dict(regimes),
        reports_present=reports,
        horizons=tuple(horizons),
    )


# -- paired comparison (plan §3-11) -------------------------------------------


def sign_test(wins: int, losses: int) -> float:
    """Two-sided exact binomial p-value on the discordant pairs; ties are not counted.

    ``p = 2 × P(X ≤ min(wins, losses))`` under ``X ~ Binomial(wins + losses, ½)``,
    capped at 1. With no discordant pair there is nothing to test, and the
    answer is 1 — "no evidence either way", not "significant".
    """
    for name, value in (("wins", wins), ("losses", losses)):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ScoreError(f"{name} is a count, got {value!r}")
    n = wins + losses
    if n == 0:
        return 1.0
    tail = sum(math.comb(n, k) for k in range(min(wins, losses) + 1)) / 2**n
    return min(1.0, 2 * tail)


@dataclass(frozen=True)
class Paired:
    """Two answer sets on the same questions: who hit where the other missed."""

    n: int
    a_only: int
    b_only: int
    p_value: float


def paired_hits(a: Sequence[bool | None], b: Sequence[bool | None]) -> Paired:
    """Compare two hit sequences question by question (McNemar's exact form).

    Positions where either side has no reading (``None``) are dropped from
    both, so a pair is always the same question judged twice.
    """
    if len(a) != len(b):
        raise ScoreError(f"paired sequences differ in length: {len(a)} vs {len(b)}")
    pairs = [(x, y) for x, y in zip(a, b, strict=True) if x is not None and y is not None]
    a_only = sum(1 for x, y in pairs if x and not y)
    b_only = sum(1 for x, y in pairs if y and not x)
    return Paired(len(pairs), a_only, b_only, sign_test(a_only, b_only))


# -- CSV -----------------------------------------------------------------------

_OUTCOME_COLUMNS: Final = (
    "later_mark",
    "later_mark_source",
    "return",
    "ai_hit",
    "executed_hit",
    "ai_pnl",
    "executed_pnl",
)


def csv_table(card: Scorecard) -> tuple[list[str], list[list[object]]]:
    """One row per decision, with the horizon columns spelled by their label."""
    header = [
        "input_id",
        "attempt_id",
        "at",
        "segment",
        "prompt_version",
        "model",
        "context_shape",
        "research_bias",
        "research_strategy_id",
        "reports_present",
        "mark",
        "account_equity",
        "current_side",
        "current_margin_pct",
        "leverage",
        "decision_mode",
        "target_side",
        "requested_margin_pct",
        "approved_margin_pct",
        "risk_action",
        "risk_reason",
        "confidence",
        "order_created",
        "no_order_reason",
        "ai_side",
        "executed_side",
        "ai_exposure",
        "executed_exposure",
        "flip",
    ]
    for bars in HORIZONS:
        label = card.horizon_label(bars)
        header.extend(f"{name}_{label}" for name in _OUTCOME_COLUMNS)
    rows: list[list[object]] = []
    for row in card.rows:
        q, a = row.question, row.answer
        values: list[object] = [
            q.input_id,
            q.attempt_id,
            from_epoch_ms(q.at_ms).isoformat(),
            None if row.segment is None else row.segment.value,
            q.prompt_version,
            q.model,
            q.context_shape,
            None if q.research_bias is None else q.research_bias.value,
            q.strategy_id,
            q.reports_present,
            q.mark,
            q.account_equity,
            q.current_side.value,
            q.current_margin_pct,
            q.leverage,
            None if a is None else a.decision_mode.value,
            None if a is None or a.target_side is None else a.target_side.value,
            None if a is None else a.requested_margin_pct,
            None if a is None else a.approved_margin_pct,
            None if a is None else a.risk_action.value,
            None if a is None else a.risk_reason,
            None if a is None else a.confidence,
            None if a is None else a.order_created,
            None if a is None else a.no_order_reason,
            None if row.ai_side is None else row.ai_side.value,
            row.executed_side.value,
            row.ai_exposure,
            row.executed_exposure,
            row.flip,
        ]
        for bars in HORIZONS:
            outcome = row.outcomes[bars]
            values.extend(
                [
                    outcome.later_mark,
                    outcome.source,
                    outcome.ret,
                    outcome.ai_hit,
                    outcome.executed_hit,
                    outcome.ai_pnl,
                    outcome.executed_pnl,
                ]
            )
        rows.append(values)
    return header, rows
