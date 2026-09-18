"""The bar-level simulation: a spec, a history, a window in, a scored trial out.

Plan §3.6 fixes the one rule everything here follows — a decision is taken at
a bar's CLOSE and filled at the NEXT bar's open — and the shape of this module
is that rule made unavoidable rather than checked afterwards. The loop reads
features at ``t`` through columns the frame computed (a reference cannot
carry a future offset; the vocabulary refuses one at construction), and turns
what it read into a *pending* order that the loop fills when it reaches
``t + 1``. There is no code path that prices a decision at the close it was
taken on, so there is nothing for a test to catch — though
``tests/test_evaluator.py`` still measures it, by hand-computing one trade and
by evaluating a truncated history and demanding the same decisions.

What a spec MEANS at the seams the parser left open (plan §10.1) is decided
here, once, and stated in :func:`_decide`:

- **An empty ``exit`` is "hold"**: the position is held until an opposite
  entry reverses it or the window ends. Not "exit when the entry condition
  stops holding" — that would turn every breakout spec without an exit into
  one that leaves a bar after it entered, and the parser already bounds
  ``max_bars`` on the reasoning that "never exit" is a thing this evaluator
  treats as one.
- **An opposite entry reverses.** A held long that sees its short entry fire
  is closed and a short is opened at the same fill. It is the most-signalled
  reading and the one that makes an always-in rule writable at all.
- **Filters gate entries only.** A filter going false does not close a
  position; a rule that wants that writes it under ``exit``, where the
  read-back shows it.
- **A missing feature is a rule that does not fire.** ``None`` at a bar
  means "not available here" (warm-up, a settlement that has not happened),
  which is the reading :mod:`.features` documents for entries; applying a
  different one to exits would make an exit rule fail-closed on the same
  ``None`` an entry rule fails-open on. The bars where a consulted condition
  could not be evaluated are COUNTED (``bars_unevaluable``), so a rule that
  went silent for a month shows up as a number rather than as a hold.
- **Both entries firing at once is a conflict, held or flat.** Nothing is
  opened, nothing is reversed, and the bar is counted
  (``bars_conflicting``). The same signal state has to mean the same thing
  whatever the position is, or a rule reads as a reversal rule only while
  it happens to be in a trade.

Two things the window itself guarantees. The window is the measured span
for EVERY spec (plan §10.2) — exposure and hit rate are ratios over the same
denominator whichever hypothesis is scored — so a store that only partly
covers it, and a spec whose features have no value at its first bar, are
both refused by name rather than measured over less. And every window is an
island: its first bar is always flat, because the decision that would fill
there was the previous bar's to take and the window does not read it, and a
position still open at the window's last bar is flattened at that bar's
close. The window never reads a bar that belongs to the next one, which is
the property the holdout lock (:mod:`.split`) rests on; the price is that an
always-in rule pays a round trip at every window edge and its exposure is
``(bars − 1) / bars``, the same for every window and every spec.

Costs follow plan §3.7. Gross is the price move, mid to mid; net subtracts
the fee and slippage of every fill (charged on the mid notional, see
:meth:`CostModel.fill_cost`) and the funding of every settlement that landed
while a position was held — signed the way the paper ledger signs it, so a
long at a positive rate PAYS. Both are reported, because a strategy whose
gross is good and whose net is not is the single most common thing a
hypothesis loop produces, and a report that showed one number would be
scoring the other.

What is deliberately NOT here: a stop-loss or take-profit (the DSL refuses
them, and the reason is this fill model — an intrabar stop is where a
backtest most easily cheats), a liquidation engine (a run whose equity
reaches zero is marked ``ruined`` and stops, which is the honest reading of
a strategy that lost everything), and pyramiding (a same-side entry while
held is ignored).
"""

from __future__ import annotations

import math
import statistics
from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Final

from .constants import (
    CANDLE_STAMP_TOLERANCE_MS,
    DAILY_INTERVAL,
    FUNDING_INTERVAL_MS,
    FUNDING_STAMP_TOLERANCE_MS,
    MS_PER_DAY,
)
from .costs import CostModel, require_amount
from .dsl import Condition, Op, Side, SizingMode, StrategySpec
from .features import FeatureFrame, FeatureValue, SeriesBundle, window_is_covered
from .gaps import scan_bars, scan_stamps
from .metrics import RegimeBucket, SegmentMetrics, Tally, describe_measurement
from .split import Segment, Split, studied_interval
from .store import ResearchStore
from .upstream import (
    Candle,
    MarketRegime,
    VocabEnum,
    from_epoch_ms,
    interval_to_ms,
)
from .vocabulary import FeatureKind, FeatureRef, SpecError, require_number

__all__ = [
    "MS_PER_YEAR",
    "STARTING_EQUITY",
    "UNLABELLED",
    "EvaluationError",
    "ExitReason",
    "RegimeBucket",
    "ReplayedPosition",
    "SegmentResult",
    "SplitResult",
    "Tally",
    "Trade",
    "describe_result",
    "evaluate_segment",
    "evaluate_split",
    "load_bundle",
    "replay_position",
]

# The annualisation base. Stated as a constant so the report can print the
# factor it used rather than leave a reader to guess whether a "Sharpe of
# 1.4" was scaled by the bar count of a 365- or a 252-day year — a crypto
# venue trades every day, so it is 365.
MS_PER_YEAR: Final = 365 * MS_PER_DAY

# Every run starts here, so every money figure in a result is a FRACTION of
# starting equity and reads as a return without a conversion.
STARTING_EQUITY: Final = 1.0

# The label the regime bucket files a bar under when the regime is not
# available there (the indicator warm-up at the head of a short bundle).
UNLABELLED: Final = "unlabelled"

_REGIME_REF: Final = FeatureRef(FeatureKind.REGIME)


class EvaluationError(RuntimeError):
    """This window cannot be measured on this history, and the message says why.

    Distinct from a bad SPEC (``SpecError``, the document's fault) and from a
    feature the bundle cannot answer at all (``FeatureError``): this is the
    pairing of a legal spec with a legal bundle over a window the two do not
    cover — a hole in the bars, a warm-up not finished by the window's first
    bar, a funding series that does not reach it. The response is to fetch
    or to move the window, never to edit the spec.
    """


class ExitReason(VocabEnum, noun="exit reason"):
    """Why a trade closed. Carried on the trade so a report can say which rule spoke."""

    EXIT_RULE = "exit_rule"
    MAX_BARS = "max_bars"
    REVERSAL = "reversal"
    SEGMENT_END = "segment_end"
    RUIN = "ruin"


@dataclass(frozen=True)
class Trade:
    """One round trip, mid to mid, with its costs beside it.

    ``gross_pnl`` is ``signed size × (exit − entry)``: every bar's marking
    telescopes to that, whichever way the trade ended, so it is derived from
    the two prices rather than accumulated beside them — two records of one
    number were one bar's move apart the moment either was edited alone.
    """

    side: Side
    entry_index: int
    exit_index: int
    entry_price: float
    exit_price: float
    size: float
    notional: float  # at entry, mid
    fees: float
    slippage: float
    funding: float  # positive = paid
    exit_reason: ExitReason

    def __post_init__(self) -> None:
        # Its invariants live on the type, as the sibling records' do: the
        # loop is its only builder today, but ``signed_size`` reads the side
        # by identity, so a string side would price a winning long as a
        # losing short rather than refuse.
        object.__setattr__(self, "side", Side(self.side))
        object.__setattr__(self, "exit_reason", ExitReason(self.exit_reason))
        for name in ("entry_index", "exit_index"):
            index = getattr(self, name)
            # ``bars_held`` is index arithmetic: a float index is a fractional
            # holding period in the exposure figure rather than a refusal.
            if isinstance(index, bool) or not isinstance(index, int) or index < 0:
                raise ValueError(f"Trade.{name} must be a bar index (an int >= 0), got {index!r}")
        if self.exit_index < self.entry_index:
            raise ValueError(
                f"a trade exits at bar {self.exit_index}, before it entered at {self.entry_index}"
            )
        for name, positive in (
            ("entry_price", True),
            ("exit_price", True),
            ("size", True),
            ("notional", True),
            ("fees", False),
            ("slippage", False),
        ):
            require_amount(getattr(self, name), f"Trade.{name}", positive=positive)
        # Funding is signed (a short at a positive rate receives), so it goes
        # through the one numeric guard with no sign bound — a hand copy here
        # let ``True`` through, and then an int too large for a float.
        try:
            require_number(self.funding, "Trade.funding")
        except SpecError as exc:
            raise ValueError(
                f"Trade.funding must be a finite number, got {self.funding!r}"
            ) from exc

    @property
    def signed_size(self) -> float:
        return self.size if self.side is Side.LONG else -self.size

    @property
    def gross_pnl(self) -> float:
        return self.signed_size * (self.exit_price - self.entry_price)

    @property
    def net_pnl(self) -> float:
        return self.gross_pnl - self.fees - self.slippage - self.funding

    @property
    def bars_held(self) -> int:
        """Bars whose close the position was held through."""
        return (
            self.exit_index
            - self.entry_index
            + (1 if self.exit_reason in (ExitReason.SEGMENT_END, ExitReason.RUIN) else 0)
        )

    def __str__(self) -> str:
        return (
            f"{self.side.value} {self.bars_held} bars @ {self.entry_price:g} -> "
            f"{self.exit_price:g}: gross {self.gross_pnl:+.4f} net {self.net_pnl:+.4f} "
            f"({self.exit_reason.value})"
        )


@dataclass(frozen=True)
class SegmentResult:
    """Everything one window said about one spec under one cost model.

    ``ruined`` is the fact to read, not something to infer from the trades:
    the last trade's ``exit_reason`` is ``ruin`` only when a position was
    still held when the account emptied. The fill that empties it can be a
    pending close (``exit_rule``, ``max_bars``) or the window's own flatten
    (``segment_end``); a reversal that empties it still ends on ``ruin``,
    because the position it opened is held when the bar is booked. After a
    ruin the remaining bars are booked
    flat, so every statistic is still over the whole window — the same
    denominator as every other spec — and a Sharpe of an early ruin is diluted
    by the flat bars after it. Rank or filter on ``ruined`` before any ratio.
    """

    segment: Segment
    bars: int
    bars_per_year: float
    gross: Tally
    net: Tally
    trades: tuple[Trade, ...]
    exposure: float
    turnover: float
    fees_paid: float
    slippage_paid: float
    funding_paid: float
    bars_unevaluable: int
    bars_conflicting: int
    funding_settlements_missing: int
    regime_buckets: tuple[RegimeBucket, ...]
    ruined: bool
    gross_bar_returns: tuple[float, ...]
    net_bar_returns: tuple[float, ...]

    def describe(self) -> list[str]:
        """Rendered from the ledger's record of this result, so ``evaluate`` and ``report`` agree."""
        return SegmentMetrics.from_result(self).describe()


@dataclass(frozen=True)
class SplitResult:
    """A spec measured on the windows it was allowed to see."""

    spec: StrategySpec
    costs: CostModel
    split: Split
    indicator_lookback: int
    train: SegmentResult
    validation: SegmentResult
    holdout: SegmentResult | None

    @property
    def results(self) -> tuple[SegmentResult, ...]:
        measured = (self.train, self.validation)
        return measured if self.holdout is None else (*measured, self.holdout)


# -- the loop ----------------------------------------------------------------


@dataclass
class _Open:
    """The position currently held, and the costs it has run up so far."""

    side: Side
    entry_index: int
    entry_price: float
    size: float
    notional: float
    fees: float = 0.0
    slippage: float = 0.0
    funding: float = 0.0

    @property
    def signed_size(self) -> float:
        return self.size if self.side is Side.LONG else -self.size

    def closed(self, exit_index: int, exit_price: float, reason: ExitReason) -> Trade:
        return Trade(
            side=self.side,
            entry_index=self.entry_index,
            exit_index=exit_index,
            entry_price=self.entry_price,
            exit_price=exit_price,
            size=self.size,
            notional=self.notional,
            fees=self.fees,
            slippage=self.slippage,
            funding=self.funding,
            exit_reason=reason,
        )


@dataclass(frozen=True)
class _Pending:
    """What the decision at ``t`` asks the fill at ``t + 1`` to do."""

    close: ExitReason | None = None
    open_side: Side | None = None
    open_notional: float = 0.0


@dataclass
class _Bar:
    """The running totals for one bar of the loop."""

    gross: float = 0.0
    cost: float = 0.0


class _Reader:
    """One spec's conditions over one frame, with its columns resolved once.

    Resolved once because the frame's public verb re-runs its empty-column
    guard on every call — 4.8 µs against 0.12 µs for the tuple read behind
    it, measured — and the loop reads several operands per bar for thousands
    of bars, per spec. The guard can only say something new once per column,
    and :func:`_require_measurable` has already given it that chance.
    """

    def __init__(self, frame: FeatureFrame, spec: StrategySpec) -> None:
        self.spec = spec
        self.columns: dict[tuple[FeatureKind, int | None], tuple[FeatureValue, ...]] = {
            (ref.kind, ref.period): frame.series(ref) for ref in spec.features
        }
        self.unevaluable = False
        self.sizing_ref = spec.sizing.refs[0] if spec.sizing.refs else None

    def read(self, ref: FeatureRef, index: int) -> FeatureValue:
        """``ref``'s value for the decision at ``bars[index]``'s close — the offset applied here."""
        shifted = index - ref.offset
        return None if shifted < 0 else self.columns[(ref.kind, ref.period)][shifted]

    def holds(self, conditions: Sequence[Condition], index: int) -> bool:
        """ANDed, and an empty list is ``False`` — no rule, no signal.

        Callers that want "no filter means pass" say so themselves; making
        the empty case ``True`` here would make an empty ``entry.short`` fire
        at every bar.
        """
        if not conditions:
            return False
        # Every condition is read, not short-circuited: ``bars_unevaluable``
        # is a property of the rule and the history, and a lazy AND would
        # make it depend on the order the author wrote the clauses in.
        outcomes = [self._holds(condition, index) for condition in conditions]
        return all(outcomes)

    def passes(self, index: int) -> bool:
        """The filter reading: nothing declared is nothing gating."""
        return not self.spec.filters or self.holds(self.spec.filters, index)

    def _holds(self, condition: Condition, index: int) -> bool:
        left = self.read(condition.left, index)
        right = condition.right
        if isinstance(right, FeatureRef):
            right = self.read(right, index)
        if left is None or right is None:
            self.unevaluable = True
            return False
        if isinstance(right, MarketRegime):
            return (left is right) if condition.op is Op.EQ else (left is not right)
        return _COMPARE[condition.op](left, right)

    def notional(self, index: int, equity: float, costs: CostModel) -> float | None:
        """What a firing rule asks for at this bar, in quote currency; ``None`` if it cannot size."""
        sizing = self.spec.sizing
        if sizing.mode is SizingMode.FIXED_MARGIN_FRACTION:
            assert sizing.fraction is not None
            return sizing.fraction * costs.leverage * equity
        assert sizing.target_vol is not None and sizing.max_fraction is not None
        assert self.sizing_ref is not None
        realized = self.read(self.sizing_ref, index)
        if realized is None or realized <= 0:
            # A vol-targeted rule that cannot read the vol cannot say how much
            # it wants, which is the same fact as a condition it cannot
            # evaluate. A vol of exactly zero — ten identical closes, a stuck
            # feed — is that case too, not a licence to size to the cap.
            self.unevaluable = True
            return None
        cap = sizing.max_fraction * costs.leverage
        return min(sizing.target_vol / float(realized), cap) * equity

    def entry(self, index: int, equity: float, costs: CostModel) -> tuple[_Pending | None, bool]:
        """Which entry fires at this bar, sized; the bool says both did.

        The one place the "filters pass → entry holds → size it → a rule that
        cannot size does not fire" chain is written, so the flat decision and
        the reversal decision cannot read it differently.

        The filters and both entries are read whatever the filters said: the
        conflict and the unevaluable counts are properties of the signal and
        the history, and a filter that stopped the entries being read would
        be the lazy AND :meth:`holds` refuses, one level up — moving a clause
        from ``entry.long`` to ``filters`` would change the counts of a
        long-only rule it did not change.
        """
        passes = self.passes(index)
        long_fires = self.holds(self.spec.entry_long, index)
        short_fires = self.holds(self.spec.entry_short, index)
        both = long_fires and short_fires
        if not passes or both or not (long_fires or short_fires):
            return None, both
        notional = self.notional(index, equity, costs)
        if notional is None:
            return None, False
        side = Side.LONG if long_fires else Side.SHORT
        return _Pending(open_side=side, open_notional=notional), False


_COMPARE: Final = {
    Op.GT: lambda a, b: a > b,
    Op.GE: lambda a, b: a >= b,
    Op.LT: lambda a, b: a < b,
    Op.LE: lambda a, b: a <= b,
}


def _decide(
    reader: _Reader, index: int, held: _Open | None, equity: float, costs: CostModel
) -> tuple[_Pending | None, bool]:
    """The decision at ``bars[index]``'s close; the bool says long and short both fired.

    This is where the plan §10.1 readings live (see the module docstring):
    an empty exit holds, an opposite entry reverses, filters gate entries,
    ``None`` does not fire, and both entries at once is a conflict.
    """
    entry, conflict = reader.entry(index, equity, costs)
    if held is None:
        return entry, conflict
    close: ExitReason | None = None
    if reader.holds(reader.spec.exits(held.side), index):
        close = ExitReason.EXIT_RULE
    elif reader.spec.max_bars is not None and index - held.entry_index + 1 >= reader.spec.max_bars:
        close = ExitReason.MAX_BARS
    if entry is not None and entry.open_side is not held.side:
        return replace(entry, close=close or ExitReason.REVERSAL), conflict
    # A same-side entry while held is not pyramided; after an exit decision
    # it is not re-entered at the same bar either — the next bar decides.
    return (_Pending(close=close) if close is not None else None), conflict


def evaluate_segment(
    spec: StrategySpec,
    frame: FeatureFrame,
    segment: Segment,
    costs: CostModel,
    *,
    interval: str,
) -> SegmentResult:
    """Score ``spec`` over ``segment`` of ``frame``'s history under ``costs``.

    ``interval`` is the bars' cadence, stated by the caller (a split carries
    it) rather than inferred from the bars: it is what the window is checked
    for holes against, and inferring it from the first two bars would assume
    the regularity being checked.
    """
    step = interval_to_ms(studied_interval(interval))
    bars = frame.bundle.bars
    first, stop = segment.bar_range([bar.open_time for bar in bars])
    settlements = _Settlements(frame.bundle)
    _require_measurable(spec, frame, segment, first, stop, step, settlements)

    reader = _Reader(frame, spec)
    equity = STARTING_EQUITY
    held: _Open | None = None
    pending: _Pending | None = None
    previous_close = 0.0
    trades: list[Trade] = []
    gross_returns: list[float] = []
    net_returns: list[float] = []
    equity_path: list[float] = []
    unevaluable = conflicting = 0
    ruined = False

    for index in range(first, stop):
        bar = bars[index]
        open_price, close_price = float(bar.open), float(bar.close)
        running = _Bar()
        last = index == stop - 1

        # 1. Fill what the previous close decided, at THIS bar's open.
        if pending is not None:
            if pending.close is not None:
                assert held is not None
                running.gross += held.signed_size * (open_price - previous_close)
                trades.append(_close(held, index, open_price, pending.close, costs, running))
                held = None
            if pending.open_side is not None:
                assert held is None
                held = _open(
                    pending.open_side, pending.open_notional, index, open_price, costs, running
                )
            pending = None

        # 2. Mark the held position to this bar's close, and settle its funding.
        if held is not None:
            basis = open_price if held.entry_index == index else previous_close
            running.gross += held.signed_size * (close_price - basis)
            paid = settlements.due(bar, held.signed_size * close_price)
            held.funding += paid
            running.cost += paid

        # 3. Book the bar. Ruin is read off the EQUITY, not off whether a
        # position is still open at this point: the loss that empties the
        # account may have been realised by the fill at this bar's open (a
        # pending close, so nothing is held by now), and the window's last
        # bar flattens whatever is held either way. Whatever is still held is
        # closed at this close like any other fill — its costs are real —
        # and the window is over. The bars after it are booked flat; the
        # equity path is not padded, so the mean equity turnover is measured
        # against is the equity that traded.
        before = equity
        ruined = before + running.gross - running.cost <= 0
        if held is not None and (ruined or last):
            # The window's last bar flattens at its close so the window reads
            # nothing of the next one; a ruin on that bar is still a ruin.
            reason = ExitReason.RUIN if ruined else ExitReason.SEGMENT_END
            trades.append(_close(held, index, close_price, reason, costs, running))
            held = None
        equity += running.gross - running.cost
        # The flatten's own fill costs can be what empties the account; then
        # the trade reads ``segment_end`` (that is why it closed) and the run
        # still reads ruined (that is what it left).
        ruined = ruined or equity <= 0
        gross_returns.append(running.gross / before)
        net_returns.append((running.gross - running.cost) / before)
        equity_path.append(equity)
        previous_close = close_price
        if ruined:
            remaining = stop - index - 1
            gross_returns += [0.0] * remaining
            net_returns += [0.0] * remaining
            break

        # 4. Decide at this close, for the next open — never on the last bar.
        if not last:
            reader.unevaluable = False
            pending, both = _decide(reader, index, held, equity, costs)
            unevaluable += reader.unevaluable
            conflicting += both

    # Every position ends as a trade (the window flattens, ruin closes), so
    # what was held, filled and paid is read off the trades rather than kept
    # in accumulators beside them — one record of each number.
    count = stop - first
    bars_per_year = MS_PER_YEAR / step
    # Turnover is measured against the equity that TRADED: the ruin bar's
    # equity is at or below zero and is not a stake anything was filled on.
    traded = equity_path[:-1] if ruined else equity_path
    mean_equity = statistics.fmean(traded) if traded else 0.0
    filled = sum(t.notional + t.size * t.exit_price for t in trades)
    return SegmentResult(
        segment=segment,
        bars=count,
        bars_per_year=bars_per_year,
        gross=_tally(gross_returns, [t.gross_pnl for t in trades], bars_per_year),
        net=_tally(net_returns, [t.net_pnl for t in trades], bars_per_year),
        trades=tuple(trades),
        exposure=sum(t.bars_held for t in trades) / count,
        turnover=filled / mean_equity if mean_equity > 0 else 0.0,
        fees_paid=sum(t.fees for t in trades),
        slippage_paid=sum(t.slippage for t in trades),
        funding_paid=sum(t.funding for t in trades),
        bars_unevaluable=unevaluable,
        bars_conflicting=conflicting,
        funding_settlements_missing=settlements.missing(bars[first], bars[stop - 1]),
        regime_buckets=_regime_buckets(frame, first, stop, net_returns),
        ruined=ruined,
        gross_bar_returns=tuple(gross_returns),
        net_bar_returns=tuple(net_returns),
    )


def _open(
    side: Side, notional: float, index: int, price: float, costs: CostModel, running: _Bar
) -> _Open:
    fee, slip = costs.fill_cost(notional)
    running.cost += fee + slip
    return _Open(
        side=side,
        entry_index=index,
        entry_price=price,
        size=notional / price,
        notional=notional,
        fees=fee,
        slippage=slip,
    )


def _close(
    held: _Open, index: int, price: float, reason: ExitReason, costs: CostModel, running: _Bar
) -> Trade:
    notional = held.size * price
    fee, slip = costs.fill_cost(notional)
    held.fees += fee
    held.slippage += slip
    running.cost += fee + slip
    return held.closed(index, price, reason)


class _Settlements:
    """The funding settlements a held position pays, bar by bar.

    A settlement belongs to the bar whose ``(open, close]`` it falls in, on
    EXACT edges — the same rule :mod:`.features` reads settlements by (its
    rate is the last one stamped at or before the close; its ``funding_cum``
    sums ``(previous close, close]``) wherever a settlement posts after the
    hour, as the venue's do. The two differ only for a stamp EXACTLY on a
    venue bar's open, which sits a millisecond past the previous close:
    ``funding_cum`` counts it and this slice does not. One rule, because a
    spec that reads
    ``funding_cum_1`` and pays funding over the same bar must be looking at
    the same four settlements. It is also the physically right rule for the
    fill model: the venue stamps a settlement a few ms AFTER the hour, and a
    position filled at that hour's open exists when it posts, while one
    flattened at that hour's close does not. A first cut pushed both edges
    back by the posting jitter, which put the jitter-late settlement into
    the bar just closed — the opposite of what the feature module says, on
    every bar of the real store.

    Signed the way the paper ledger signs it: positive is PAID (a long at a
    positive rate pays), which is what the running cost adds.

    Coverage is counted by stamps, the way the feature module counts a
    window's occupancy — so a count alone lets an off-grid stamp, or a second
    one in the same hour, stand in for a missing hour, and :meth:`due` would
    charge its rate as that hour's carry while the report says nothing is
    missing. The store's primary key rules out an exact duplicate; the other
    two are refused by the scanner's own verdict, because
    ``_require_measurable`` puts the window's stamps (:meth:`within`) through
    the gap scan before it counts them.
    """

    def __init__(self, bundle: SeriesBundle) -> None:
        self.points = bundle.funding
        self.times = [point.time for point in self.points]

    def _slice(self, bar: Candle, slack: int = 0) -> tuple[int, int]:
        return (
            bisect_right(self.times, bar.open_time + slack),
            bisect_right(self.times, bar.close_time + slack),
        )

    def due(self, bar: Candle, signed_notional: float) -> float:
        lo, hi = self._slice(bar)
        return sum(signed_notional * float(self.points[i].rate) for i in range(lo, hi))

    def held(self, first_bar: Candle, last_bar: Candle) -> tuple[int, int]:
        """``(stored, expected)`` hourly settlements over the span of those bars.

        This asks whether the settlements EXIST, not which bar pays them, so
        its edges carry the venue's posting jitter: the settlement due at the
        span's last close posts a few ms after it, and on exact edges every
        real window would read as missing exactly one. The charging edges
        in :meth:`due` stay exact for the reason the class docstring gives.
        """
        lo, hi = self._span(first_bar, last_bar)
        # Rounded, because a venue close is one millisecond BEFORE the next
        # open (see ``constants``): floored, a span of N hours less 1 ms
        # expected N - 1 settlements, and a window with one genuinely absent
        # read as complete.
        span = last_bar.close_time - first_bar.open_time
        return hi - lo, round(span / FUNDING_INTERVAL_MS)

    def within(self, first_bar: Candle, last_bar: Candle) -> list[int]:
        """The settlement stamps over the span of those bars, on :meth:`held`'s jittered edges."""
        lo, hi = self._span(first_bar, last_bar)
        return self.times[lo:hi]

    def _span(self, first_bar: Candle, last_bar: Candle) -> tuple[int, int]:
        lo, _ = self._slice(first_bar, FUNDING_STAMP_TOLERANCE_MS)
        _, hi = self._slice(last_bar, FUNDING_STAMP_TOLERANCE_MS)
        return lo, hi

    def missing(self, first_bar: Candle, last_bar: Candle) -> int:
        """How many of the settlements the window's span should hold are not there."""
        stored, expected = self.held(first_bar, last_bar)
        return max(0, expected - stored)


def _tally(returns: Sequence[float], trade_pnls: Sequence[float], bars_per_year: float) -> Tally:
    """The four statistics over one return series. Zero, never NaN, when there is nothing to say.

    An always-flat strategy has a return series of zeros and no trades; plan
    §6.6 wants that reported as zeros so a report reads "did nothing" rather
    than failing to render. ``sharpe`` is 0 when the deviation is 0 for the
    same reason: a series that never moved has no risk-adjusted anything.

    So a Sharpe of 0 is not "did nothing". A series that lost the SAME
    non-zero amount at every bar also has no deviation and also reads 0 —
    read it beside ``total_return`` and the trade count. On real history the
    compounding equity makes identical bar returns all but impossible; kept
    as 0 rather than ±inf, which no ledger row can hold (decided 2026-09-14).
    """
    path = [STARTING_EQUITY]
    for r in returns:
        path.append(path[-1] * (1.0 + r))
    total = path[-1] / STARTING_EQUITY - 1.0
    deviation = statistics.stdev(returns) if len(returns) > 1 else 0.0
    sharpe = (
        0.0 if deviation == 0 else statistics.fmean(returns) / deviation * math.sqrt(bars_per_year)
    )
    peak, drawdown = path[0], 0.0
    for value in path[1:]:
        peak = max(peak, value)
        drawdown = max(drawdown, (peak - value) / peak if peak > 0 else 0.0)
    hit = sum(1 for pnl in trade_pnls if pnl > 0) / len(trade_pnls) if trade_pnls else 0.0
    return Tally(total_return=total, sharpe=sharpe, max_drawdown=drawdown, hit_rate=hit)


def _regime_buckets(
    frame: FeatureFrame, first: int, stop: int, net_returns: Sequence[float]
) -> tuple[RegimeBucket, ...]:
    """Net return per regime label over the window, in the label's declared order.

    A bar's return is filed under the regime at the PREVIOUS close — the one
    known when the position that earned it was chosen. The label at the
    bar's own close is computed over that bar's move, so a large down bar
    that flips the classifier would file its own loss under the bear it
    caused. The window's first bar reads the bar before it, which the
    bundle may hold (that bar is always flat, so it moves nothing).

    The regime column is asked for only when the bundle is long enough for
    the engine to have warmed up somewhere in it: a full indicator window,
    which the frame holds at or above the classifier's own warm-up. Shorter than that, every
    bar is ``None`` by warm-up and the frame would refuse the column as one
    it cannot answer — which is the right refusal for a SPEC feature and the
    wrong one for a report dimension; here those bars are simply unlabelled.

    Asking for the column is what makes a frame pay the indicator walk
    whether or not the spec reads an indicator (about 12 s for 5000 bars on
    this box, once per frame — an experiment shares one). A report dimension
    that appeared only for the specs that happened to warm it would not be a
    dimension; the cost is stated in the README's trade-offs instead.
    """
    if len(frame.bundle.bars) >= frame.indicator_lookback:
        labels = frame.series(_REGIME_REF)
    else:
        labels = (None,) * len(frame.bundle.bars)
    bars: Counter[str] = Counter()
    totals: defaultdict[str, float] = defaultdict(float)
    for index, net in zip(range(first, stop), net_returns, strict=True):
        label = labels[index - 1] if index > 0 else None
        key = UNLABELLED if label is None else label.value
        bars[key] += 1
        totals[key] += net
    order = [regime.value for regime in MarketRegime] + [UNLABELLED]
    return tuple(
        RegimeBucket(label=key, bars=bars[key], net_return=totals[key])
        for key in order
        if key in bars
    )


# -- fitness of the window ----------------------------------------------------


def _require_measurable(
    spec: StrategySpec,
    frame: FeatureFrame,
    segment: Segment,
    first: int,
    stop: int,
    step: int,
    settlements: _Settlements,
) -> None:
    """Refuse a window this history cannot honestly score, naming what is missing.

    Plan §3.4: never evaluate across a hole. And plan §10.2: the window is
    the measured span, so a store that covers only part of it — or a feature
    still warming up at its first bar — is a strategy scored over less than
    the span the report prints. Refused, with the bar it first has a value
    at, so the operator can move the window or fetch older history.
    """
    bars = frame.bundle.bars
    if stop - first < 2:
        raise EvaluationError(
            f"{segment} holds {stop - first} of this bundle's bars; a window needs at least "
            f"two — a decision, and the bar it fills in"
        )
    stamps = [bar.open_time for bar in bars[first:stop]]
    # An edge off the store's grid first, by name: ``by_shares`` snaps its
    # cuts, but a hand-built or ledger-read segment need not be snapped, and
    # such an edge would otherwise be reported below as history the store
    # lacks — a sentence that sends the operator to fetch what is there.
    off_grid = [
        f"{name} {from_epoch_ms(edge).isoformat()}"
        for name, edge in (("start", segment.start_ms), ("end", segment.end_ms))
        if (edge - bars[0].open_time) % step
    ]
    if off_grid:
        raise EvaluationError(
            f"{segment}: its {' and '.join(off_grid)} {'is' if len(off_grid) == 1 else 'are'} "
            f"not on the store's {step} ms grid, so no bar opens there — cut the split on "
            f"the grid."
        )
    missing_edges = []
    if stamps[0] != segment.start_ms:
        missing_edges.append(f"begins at {from_epoch_ms(stamps[0]).isoformat()}")
    if stamps[-1] + step != segment.end_ms:
        missing_edges.append(f"ends at {from_epoch_ms(stamps[-1] + step).isoformat()}")
    if missing_edges:
        raise EvaluationError(
            f"{segment}: the stored history {' and '.join(missing_edges)}, so the window "
            f"would be measured over fewer bars than it names. Fetch the missing span, or "
            f"cut the split to the history the store has."
        )
    # The gap scanner's verdict, not a second definition of a hole: ``gaps``
    # and this refusal have to agree about the same store - including the
    # one finding a stamp scan cannot make, a bar whose close disagrees with
    # the interval, which ``scan_bars`` adds over the same stamps.
    report = scan_bars(str(segment), step, CANDLE_STAMP_TOLERANCE_MS, bars[first:stop])
    if report.duplicate_ms or report.misaligned_ms:
        # Named before any hole: a stamp the scanner could not place on the
        # grid leaves its slot empty, so the same series also reads as having
        # a hole there, and the hole is the consequence rather than the fact.
        # And before a misshapen bar: an off-grid bar is misshapen too when it
        # came from another cadence, but no re-fetch repairs it, while the
        # shape refusal below promises one.
        raise EvaluationError(
            f"{segment} is not on the {step} ms grid ({len(report.duplicate_ms)} duplicate "
            f"slot(s), {len(report.misaligned_ms)} off-grid stamp(s)); a re-fetch does not "
            f"repair this — run `gaps` to see which stamps."
        )
    if report.misshapen:
        bar = report.misshapen[0]
        raise EvaluationError(
            f"{segment} holds {len(report.misshapen)} bar(s) whose close disagrees with the "
            f"{step} ms interval, the first opening at {from_epoch_ms(bar.open_ms).isoformat()} "
            f"and lasting {bar.close_ms - bar.open_ms} ms - not the venue's bar shape. Run "
            f"`gaps`; a re-fetch at this interval overwrites a bar another cadence wrote here."
        )
    if report.gaps:
        gap = report.gaps[0]
        raise EvaluationError(
            f"{segment} has a hole: {from_epoch_ms(gap.after_ms).isoformat()} is followed by "
            f"{from_epoch_ms(gap.before_ms).isoformat()}, {gap.missing} bars missing. Read as "
            f"a grid, a hole is one enormous return between two adjacent-looking bars — run "
            f"`gaps`, then `fetch` the window."
        )
    if not frame.bundle.funding:
        raise EvaluationError(
            "the cost model settles funding hourly and this bundle has no settlements — "
            "fetch without --skip-funding before measuring on it"
        )
    # Structure before count, as for the bars: counted alone, a stamp off the
    # hourly grid fills in for a missing hour and is charged as its carry.
    # Holes stay the count's business below — a settlement or two short is
    # reported, not refused.
    funding_scan = scan_stamps(
        f"{segment} funding",
        FUNDING_INTERVAL_MS,
        FUNDING_STAMP_TOLERANCE_MS,
        settlements.within(bars[first], bars[stop - 1]),
    )
    if funding_scan.duplicate_ms or funding_scan.misaligned_ms:
        raise EvaluationError(
            f"{segment}'s funding settlements are not on the hourly grid "
            f"({len(funding_scan.duplicate_ms)} duplicate slot(s), "
            f"{len(funding_scan.misaligned_ms)} off-grid stamp(s)); counted, such a stamp "
            f"would stand in for a missing hour and be charged as its carry. A re-fetch "
            f"does not repair this — run `gaps` to see which stamps."
        )
    stored, expected = settlements.held(bars[first], bars[stop - 1])
    if not window_is_covered(stored, expected):
        raise EvaluationError(
            f"{segment} should hold {expected} hourly funding settlements and the store has "
            f"{stored}; the carry of a position held through those hours would be "
            f"understated without looking wrong. Run `gaps`, then `fetch` the window."
        )
    for ref in spec.features:
        # The column once (its guard fires once), then a scan bounded by the
        # window: the message says where inside the window the feature wakes
        # up, and reads nothing past it.
        column = frame.series(ref)
        ready = next(
            (
                i
                for i in range(first, stop)
                if i - ref.offset >= 0 and column[i - ref.offset] is not None
            ),
            None,
        )
        if ready == first:
            continue
        when = (
            f"it first has one at {from_epoch_ms(bars[ready].open_time).isoformat()} "
            f"({ready - first} bars in)"
            if ready is not None
            else "and has none anywhere inside the window either"
        )
        raise EvaluationError(
            f"{ref} has no value at the first bar of {segment} "
            f"({from_epoch_ms(bars[first].open_time).isoformat()}); {when}. The window is "
            f"the measured span, so a rule that cannot fire at its start would be scored over "
            f"bars it never saw — start the window later, or fetch older history."
        )


# -- over a whole split ----------------------------------------------------------


def evaluate_split(
    spec: StrategySpec,
    frame: FeatureFrame,
    split: Split,
    costs: CostModel,
    *,
    holdout: bool = False,
) -> SplitResult:
    """Score ``spec`` on train and validation — and on the holdout only when told to.

    The keyword is the lock (plan §3.8, §6.5): a caller that has not
    promoted a trial does not pass it, and the result then carries no holdout
    figure to print, average, or rank on. PR A4's ledger owns WHO may pass it.
    ``Split.segments`` is what withholds the window; this unpacks whatever it
    handed over.
    """
    train, validation, *rest = [
        evaluate_segment(spec, frame, segment, costs, interval=split.interval)
        for segment in split.segments(holdout=holdout)
    ]
    return SplitResult(
        spec=spec,
        costs=costs,
        split=split,
        indicator_lookback=frame.indicator_lookback,
        train=train,
        validation=validation,
        holdout=rest[0] if rest else None,
    )


def load_bundle(
    store: ResearchStore, *, coin: str, interval: str, until_ms: int | None = None
) -> SeriesBundle:
    """The history a split is measured on, read from the store — and no further than ``until_ms``.

    ``until_ms`` is an ``open_time`` bound on the decision bars (what
    :meth:`Split.loadable_until` hands over). The daily backdrop and the
    settlements are read to the CLOSE of the last bar that bound admitted —
    taken from that bar, not derived from the bound, since the bound may sit
    anywhere between two opens — because that close is the instant its
    features are aligned at. A daily bar is kept only if its OWN close has
    been reached — filtered on that stamp, not derived from its open, since a
    venue close is a millisecond short of the next open — so a day that
    closes inside the next window is not held even though it opened inside
    this one. The settlements are read up to the one DUE at that close,
    which the venue posts a few ms after it, so the bound carries the posting
    jitter; without it every bounded bundle read as missing its last
    settlement. That settlement is a fact about the close just reached; the
    one after it is a fact about the next window, and is not read. The
    holdout lock is the reason.
    """
    key = studied_interval(interval)
    daily_key = DAILY_INTERVAL
    bars = list(store.iter_candles(coin, key, until_ms=until_ms))
    if not bars:
        raise EvaluationError(f"the store holds no {key} bars for {coin} in that span")
    last_close = bars[-1].close_time
    if key == daily_key:
        # The backdrop IS the decision series: a prefix of what was just
        # read, not a second scan of the same rows.
        daily = bars
    else:
        daily = list(
            store.iter_candles(coin, daily_key, until_ms=None if until_ms is None else last_close)
        )
    if until_ms is not None:
        daily = [bar for bar in daily if bar.close_time <= last_close]
    funding_until = None if until_ms is None else last_close + FUNDING_STAMP_TOLERANCE_MS
    return SeriesBundle(
        bars, daily=daily, funding=list(store.iter_funding(coin, until_ms=funding_until))
    )


# -- rendering -------------------------------------------------------------------


def describe_result(result: SplitResult) -> list[str]:
    """The result as lines to print — the same text ``report`` prints from the ledger."""
    return describe_measurement(
        result.spec,
        result.costs,
        result.split,
        result.indicator_lookback,
        [SegmentMetrics.from_result(segment) for segment in result.results],
    )


# -- where the decisions leave the rule ------------------------------------------


@dataclass(frozen=True)
class ReplayedPosition:
    """Which side a spec's decisions leave it on after the LAST bar of a replay.

    ``side`` is the side the rule HOLDS once the decision at
    ``last_close_time`` has been applied — the same decide-at-close /
    fill-at-next-open rule the scored loop obeys, read one bar further along.
    ``None`` is flat.

    Read it with the two flags, not on its own. ``last_bar_unevaluable`` says
    the final decision consulted a condition it could not evaluate (a ``None``
    feature), in which case ``side`` is one carried in from an EARLIER bar
    rather than one the rule just re-took. ``replayed_bars_unevaluable`` says
    the same happened somewhere in the replay, which for a rule with no exit
    is how a hole in the history freezes it on one side for a month. The
    scored loop counts such bars and carries on, because over a measured
    window they are a property of the rule worth reporting; a signal read off
    the newest bar wants the opposite, so
    :func:`~contrib.autoresearch.signal.build_signal` refuses on either.

    The counter is named for its SPAN. ``SegmentResult.bars_unevaluable``
    counts the same event over one scored window; this one counts it from the
    experiment's train start through the unscored tail. Two spans under one
    name is how a report ends up comparing two different measurements.
    """

    side: Side | None
    last_close_time: int
    replayed_bars_unevaluable: int
    last_bar_unevaluable: bool


def replay_position(
    spec: StrategySpec, frame: FeatureFrame, costs: CostModel, *, since_ms: int
) -> ReplayedPosition:
    """Replay ``spec``'s DECISIONS from ``since_ms`` to the frame's last bar.

    What this is for: a promoted rule's current side, for the one qualitative
    block the research radar hands to the live prompt (plan §7, C1). The
    scored loop cannot answer it. ``evaluate_segment`` flattens whatever is
    held at its window's last bar and deliberately takes no decision there —
    a window is an island, so the last bar's decision would fill at a bar
    belonging to the next window. A signal wants exactly that decision, and
    the bar it would fill at is the future.

    What it does NOT model, and the caller must not read into it: equity,
    fills, fees, funding, and therefore ruin. Only whether a side is HELD is
    tracked, which is enough because nothing in :func:`_decide` reads equity
    except the notional a firing rule asks for, and that changes the size of
    a position, never which side fires or whether it does. A ``vol_target``
    rule that cannot read its volatility still declines to fire, since that
    branch turns on the feature, not on the stake. So the equity handed to
    every bar is :data:`STARTING_EQUITY`, and the returned side is a
    statement about the rule's signal, not about an account that survived to
    obey it.

    The replay starts at ``since_ms`` because a rule's side is path
    dependent: an empty ``exit`` holds until a reversal, and ``max_bars``
    counts from the entry. Start it later than the rule's own history and a
    position opened before the start is invisible, so the first entry after
    it reads as an open rather than as a reversal. Callers pass the first bar
    the experiment ever considered measurable (its train window's start), not
    a recent tail.
    """
    bars = frame.bundle.bars
    first = bisect_left([bar.open_time for bar in bars], since_ms)
    stop = len(bars)
    if first >= stop:
        raise EvaluationError(
            f"the frame's {stop} bars all open before "
            f"{from_epoch_ms(since_ms).isoformat()}, so there is nothing to replay"
        )
    if bars[0].open_time > since_ms:
        # The other end of the same hazard, and the one nothing else catches:
        # a store whose history no longer REACHES the start silently begins
        # the replay late, and ``bisect_left`` reports that as index 0. A
        # truncated prefix leaves no hole, so the gap scan cannot see it
        # either. A rule with no exit that entered before the store now begins
        # would read as flat — the very mistake ``since_ms`` exists to
        # prevent, arrived at from the other side.
        raise EvaluationError(
            f"the replay must start at {from_epoch_ms(since_ms).isoformat()} but this store's "
            f"{len(bars)}-bar history begins at "
            f"{from_epoch_ms(bars[0].open_time).isoformat()}; a position opened before that is "
            f"invisible, so the side would be wrong rather than merely short — fetch the older "
            f"history back"
        )

    reader = _Reader(frame, spec)
    held: _Open | None = None
    pending: _Pending | None = None
    unevaluable = 0
    last_bar_unevaluable = False

    for index in range(first, stop):
        bar = bars[index]
        # 1. Fill what the previous close decided, at THIS bar's open — the
        #    positions only, none of the money. ``_open``/``_close`` are not
        #    used: they book fees and slippage into a running bar total this
        #    replay has no place to put, and the entry INDEX is the only
        #    field a later decision reads (``max_bars`` counts from it).
        if pending is not None:
            if pending.close is not None:
                held = None
            if pending.open_side is not None:
                open_price = float(bar.open)
                held = _Open(
                    side=pending.open_side,
                    entry_index=index,
                    entry_price=open_price,
                    size=pending.open_notional / open_price,
                    notional=pending.open_notional,
                )
            pending = None

        # 2. Decide at this close — INCLUDING the last bar, which is the
        #    whole point of this function.
        reader.unevaluable = False
        # The conflict count is deliberately dropped rather than carried: a
        # bar where both entries fire is a property of the RULE that the
        # scored windows already report, and a second counter over a different
        # span with the same name is how two measurements get compared as one.
        pending, _both = _decide(reader, index, held, STARTING_EQUITY, costs)
        unevaluable += reader.unevaluable
        last_bar_unevaluable = reader.unevaluable

    # The fill the last decision asks for, applied to the side alone. Closing
    # before opening, in that order, so a reversal — which carries both —
    # lands on the new side rather than on flat.
    side = held.side if held is not None else None
    if pending is not None:
        if pending.close is not None:
            side = None
        if pending.open_side is not None:
            side = pending.open_side

    return ReplayedPosition(
        side=side,
        last_close_time=bars[stop - 1].close_time,
        replayed_bars_unevaluable=unevaluable,
        last_bar_unevaluable=last_bar_unevaluable,
    )


# Import-time check, in the style of the sibling modules: every ordering
# operator the DSL can produce has a comparison here, or a rule would reach a
# trial as a ``KeyError``.
if set(_COMPARE) != set(Op) - {Op.EQ, Op.NE}:
    raise RuntimeError("every ordering operator needs a comparison")
