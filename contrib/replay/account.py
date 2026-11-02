"""A simulated account that carries a position from one decision to the next (replay plan PR 3a).

The scorecard (:mod:`.score`) reads every decision on its own: "what if this
call were held one bar". A backtest is the other thing the mother plan §1
asks for — the whole path, one account, every decision applied to the
position the previous ones left behind, so that the figure at the end is a
run's P&L and not a sum of hypotheticals. This module is that account, and
nothing else: it asks no model, reads no store (its inputs are records) and
never touches the paper books.

The arithmetic is the paper lane's own, borrowed through :mod:`.upstream`:

- a target is sized the way the gate sizes it (``risk_gate.evaluate``):
  target margin = equity × approved margin % / 100, notional = margin ×
  the question's configured leverage, and the order is the DIFFERENCE
  between that signed notional and the position held, at the question's
  mark. The gate's order-necessity rules apply to the account's OWN
  book: a same-side ask less than the run's ``rebalance_deadband_pct``
  away from the margin held places no order (flips and closes always
  do), and a flat ask closes the whole position;
- a taker run fills at once at the mark moved by the run's slippage
  (``fill_model.fill_price``) and pays the taker fee; a maker run posts at
  the mark less (buy) or plus (sell) the run's assumed half spread,
  rounded to the tick on the passive side (``maker_post_price``), and the
  post FILLS when the bar the decision falls in traded through it by a
  tick — its low for a buy, its high for a sell, the paper engine's own
  ``maker_would_fill`` with that extreme standing in for the mid (decided
  2026-11-02) — at the post price, for the maker fee and no slippage. A
  post that does not fill rests: it is re-priced at the next question's
  mark and tried again there, until it fills, a new target replaces it,
  or the re-pricing finds it no longer needed (inside the deadband, or
  exactly held), where it is dropped like any ask that places nothing;
- the fill's effect on the position and the wallet is
  ``accounting.compute_fill_effect`` (weighted entry, realised P&L on a
  reduce, the fee out of the wallet);
- funding is settled hourly from the research store's series, the ledger's
  own ``funding_pnl`` (income positive) on the position's notional at the
  last bar closed at or before the settlement (at the previous question's
  mark for a settlement before any bar has closed). A settlement the store
  lacks while a position is held is COUNTED and reported, never taken as
  zero.

No stop-loss and no take-profit (decided 2026-11-02; the mother plan keeps
the safety gates out of the score), and the position still open at the
last question is marked to that question's mark, not flattened. Every
figure is ``Decimal`` under the books' own context until it becomes a
return; the returns are floats, as the scorecard's statistics are.

A decision's RETURN is the move of the account's equity from this
question's mark to the next one's: ``equity_{i+1} / equity_i - 1``, with
``equity_i`` read before the question's own order, so a fill's fee and the
funding paid up to the next question land on the decision that caused
them. The last question has none; the path's final equity is read after
its order. An account whose equity reaches zero is ruined: the path stops
there and says so.
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal, InvalidOperation, localcontext
from typing import Final

from .upstream import (
    DECIMAL_CONTEXT,
    FUNDING_INTERVAL_MS,
    CostModel,
    FillRole,
    PositionState,
    SegmentName,
    Side,
    TargetSide,
    compute_fill_effect,
    fill_price,
    from_epoch_ms,
    funding_pnl,
    maker_post_price,
    maker_would_fill,
    unrealized_pnl,
)

__all__ = [
    "STEP_COLUMNS",
    "AccountError",
    "Bar",
    "Decision",
    "Funding",
    "Path",
    "Series",
    "Step",
    "Target",
    "Terms",
    "simulate",
]

_SIGN: Final = {
    TargetSide.LONG: Decimal(1),
    TargetSide.SHORT: Decimal(-1),
    TargetSide.FLAT: Decimal(0),
}
_HUNDRED: Final = Decimal(100)


class AccountError(ValueError):
    """A path the account cannot simulate, and the sentence names the question and why."""


@dataclass(frozen=True)
class Bar:
    """One bar of the research store, the prices the fill model and the funding read."""

    open_time: int
    close_time: int
    high: Decimal
    low: Decimal
    close: Decimal

    def __post_init__(self) -> None:
        if self.open_time >= self.close_time:
            raise AccountError(
                f"a bar opens before it closes, got {self.open_time} >= {self.close_time}"
            )
        if not 0 < self.low <= self.close <= self.high:
            raise AccountError(
                f"bar closing {self.close_time}: low {self.low} <= close {self.close} <= high "
                f"{self.high} does not hold"
            )


@dataclass(frozen=True)
class Funding:
    """One hourly funding settlement: its stamp and the rate (positive: longs pay)."""

    time: int
    rate: Decimal


@dataclass(frozen=True)
class Target:
    """What a decision asked the book to become: a side and an approved margin percent.

    ``flat`` carries margin 0 and nothing else; a sided target carries a
    positive margin. The grid is integral in the store, but the account takes
    any non-negative ``Decimal`` so a control can trade at the run's cap.
    """

    side: TargetSide
    margin_pct: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(self, "side", TargetSide(self.side))
        object.__setattr__(self, "margin_pct", Decimal(self.margin_pct))
        if self.margin_pct < 0:
            raise AccountError(f"a target margin is a magnitude, got {self.margin_pct}")
        if (self.side is TargetSide.FLAT) != (self.margin_pct == 0):
            raise AccountError(
                f"a {self.side.value} target with margin {self.margin_pct}: flat carries none, "
                "a side carries some"
            )

    @property
    def sign(self) -> Decimal:
        return _SIGN[self.side]


@dataclass(frozen=True)
class Decision:
    """One question as the account needs it: when, at what mark, at what leverage, and the ask.

    ``target`` is ``None`` when the decision changed nothing (a maintain, a
    rejection, a fail-closed round, an unanswered question): the account
    holds what it holds, and a post still resting is tried again.
    """

    input_id: str
    at_ms: int
    segment: SegmentName | None
    mark: Decimal
    leverage: Decimal
    target: Target | None

    def __post_init__(self) -> None:
        me = self.input_id
        if isinstance(self.at_ms, bool) or not isinstance(self.at_ms, int):
            raise AccountError(f"{me}: at_ms is epoch ms, got {self.at_ms!r}")
        for name in ("mark", "leverage"):
            if not isinstance(getattr(self, name), Decimal):
                raise AccountError(f"{me}: {name} is a Decimal, got {getattr(self, name)!r}")
        if self.mark <= 0:
            raise AccountError(f"{me}: mark must be > 0, got {self.mark}")
        if self.leverage <= 0:
            raise AccountError(f"{me}: leverage must be > 0, got {self.leverage}")
        if self.segment is not None:
            object.__setattr__(self, "segment", SegmentName(self.segment))


@dataclass(frozen=True)
class Terms:
    """The account's fixed terms: the run's fill model, the maker post, the opening balance.

    ``costs`` is the run's own (:class:`~contrib.autoresearch.costs.CostModel`,
    read off the genesis as the scorecard reads it); ``half_spread_bps`` the
    run's ``assumed_half_spread_bps``; ``tick`` the venue's price tick, which
    the paper store does not record (it comes from the asset's metadata), so
    the caller says it. ``initial_balance`` is the run's
    ``initial_balance_usdc``; ``deadband_pct`` the run's
    ``rebalance_deadband_pct``, the same-side resize the account leaves
    alone (0: none). ``fee_rate`` and ``slippage_bps`` are the cost
    model's figures as the books' ``Decimal``, fixed once here.
    """

    coin: str
    costs: CostModel
    half_spread_bps: Decimal
    tick: Decimal
    initial_balance: Decimal
    deadband_pct: Decimal = Decimal(0)
    fee_rate: Decimal = field(init=False)
    slippage_bps: Decimal = field(init=False)

    def __post_init__(self) -> None:
        for name in ("half_spread_bps", "tick", "initial_balance", "deadband_pct"):
            if not isinstance(getattr(self, name), Decimal):
                raise AccountError(f"{name} is a Decimal, got {getattr(self, name)!r}")
        if self.tick <= 0:
            raise AccountError(f"tick must be > 0, got {self.tick}")
        if self.half_spread_bps < 0:
            raise AccountError(f"half_spread_bps must be >= 0, got {self.half_spread_bps}")
        if self.initial_balance <= 0:
            raise AccountError(f"initial_balance must be > 0, got {self.initial_balance}")
        if self.deadband_pct < 0:
            raise AccountError(f"deadband_pct must be >= 0, got {self.deadband_pct}")
        object.__setattr__(self, "fee_rate", Decimal(str(self.costs.fee_rate)))
        object.__setattr__(self, "slippage_bps", Decimal(str(self.costs.slippage_bps)))

    def describe(self) -> str:
        """One line: everything a step's price and fee depend on."""
        role = self.costs.fill_role.value
        fill = (
            f"posted at mark -/+ {self.half_spread_bps} bps on a {self.tick} tick, filled when the "
            "bar trades through the post by a tick"
            if self.costs.fill_role is FillRole.MAKER
            else f"filled at mark -/+ {self.costs.slippage_bps:g} bps slippage"
        )
        return (
            f"account: opens at {self.initial_balance} USDC; {role} fills at "
            f"{self.costs.fee_rate:g} fee, {fill}; a same-side ask within {self.deadband_pct}% of "
            "the margin held places no order; funding settled hourly from the research "
            "store; no stop-loss, no take-profit; the last position is marked, not closed"
        )


# The columns of :meth:`Step.values`, in order: what a step is when written out.
STEP_COLUMNS: Final = (
    "input_id",
    "at",
    "segment",
    "mark",
    "target_side",
    "target_margin_pct",
    "carried",
    "filled",
    "fill_price",
    "fee",
    "funding",
    "funding_missing",
    "size",
    "entry",
    "wallet",
    "equity",
    "return",
)


@dataclass(frozen=True)
class Step:
    """The account at one question: what it held, what it was asked, what it did.

    ``equity`` is read at the question's mark BEFORE its order, with the
    funding paid since the previous question already in the wallet.
    ``filled`` is ``None`` when no order was placed (nothing asked, or the
    ask already held), ``True`` for a fill at ``fill_price`` paying ``fee``,
    ``False`` for a maker post the bar did not reach, left resting at
    ``fill_price`` (the post's price). ``carried`` says the ask was a post
    left over from an earlier question; whether it placed an order again is
    ``filled``, as for any ask. ``funding`` is what the settlements
    since the previous question moved the wallet by (income positive),
    ``funding_missing`` how many of them the store lacked while a position
    was held. ``size``, ``entry`` and ``wallet`` are the position and the
    wallet AFTER the step. ``ret`` is this decision's return (module
    docstring), ``None`` on the last step.
    """

    input_id: str
    at_ms: int
    segment: SegmentName | None
    mark: Decimal
    target: Target | None
    carried: bool
    filled: bool | None
    fill_price: Decimal | None
    fee: Decimal
    funding: Decimal
    funding_missing: int
    size: Decimal
    entry: Decimal | None
    wallet: Decimal
    equity: Decimal
    ret: float | None = None

    def __post_init__(self) -> None:
        me = self.input_id
        if (self.filled is None) != (self.fill_price is None):
            raise AccountError(f"{me}: an order has a price and no order has none")
        if self.filled is not True and self.fee != 0:
            raise AccountError(f"{me}: only a fill pays a fee, got {self.fee}")
        if self.carried and self.target is None:
            raise AccountError(f"{me}: a carried post is a target")
        if (self.size == 0) != (self.entry is None):
            raise AccountError(f"{me}: a flat position has no entry and a sized one has")

    @property
    def closing_equity(self) -> Decimal:
        """The equity AFTER the step's order, marked at the step's mark."""
        if self.size == 0:
            return self.wallet
        assert self.entry is not None
        return self.wallet + unrealized_pnl(self.size, self.mark, self.entry)

    def values(self) -> tuple[object, ...]:
        """The step as :data:`STEP_COLUMNS` primitives: ISO stamp, enum values, Decimals, bools."""
        return (
            self.input_id,
            from_epoch_ms(self.at_ms).isoformat(),
            None if self.segment is None else self.segment.value,
            self.mark,
            None if self.target is None else self.target.side.value,
            None if self.target is None else self.target.margin_pct,
            self.carried,
            self.filled,
            self.fill_price,
            self.fee,
            self.funding,
            self.funding_missing,
            self.size,
            self.entry,
            self.wallet,
            self.equity,
            self.ret,
        )


@dataclass(frozen=True)
class Path:
    """One account's whole path over a run's questions, under one set of terms.

    ``ruined`` is a fact of the steps: the walk stops at the question that
    finds the equity at or below zero, so that step is last.
    """

    terms: Terms
    steps: tuple[Step, ...]

    @property
    def ruined(self) -> bool:
        return bool(self.steps) and self.steps[-1].equity <= 0

    @property
    def final_equity(self) -> Decimal:
        """The equity after the last question's order, at its mark."""
        return self.steps[-1].closing_equity if self.steps else self.terms.initial_balance

    @property
    def total_return(self) -> float:
        return float(self.final_equity / self.terms.initial_balance) - 1.0

    @property
    def fees(self) -> Decimal:
        return sum((s.fee for s in self.steps), Decimal(0))

    @property
    def funding(self) -> Decimal:
        return sum((s.funding for s in self.steps), Decimal(0))

    @property
    def funding_missing(self) -> int:
        return sum(s.funding_missing for s in self.steps)

    @property
    def orders(self) -> int:
        """Orders placed: a post retried at a later question is the same order, not another."""
        return sum(1 for s in self.steps if s.filled is not None and not s.carried)

    @property
    def fills(self) -> int:
        return sum(1 for s in self.steps if s.filled)

    @property
    def unfilled(self) -> int:
        """Posts left resting at the end of a question."""
        return sum(1 for s in self.steps if s.filled is False)

    def returns(
        self, segment: SegmentName | None = None, *, only: Collection[str] | None = None
    ) -> list[float]:
        """The decisions' returns, in time order; of one segment, or of the questions ``only`` names."""
        return [
            s.ret
            for s in self.steps
            if s.ret is not None
            and (segment is None or s.segment is segment)
            and (only is None or s.input_id in only)
        ]

    def describe(self) -> list[str]:
        """The path in two lines: what the account did, and where it ended."""
        missing = (
            f"; {self.funding_missing} funding settlement(s) missing from the research store "
            "while a position was held (NOT taken as zero)"
            if self.funding_missing
            else ""
        )
        ruin = " -- RUINED: the path stopped where equity reached zero" if self.ruined else ""
        return [
            f"  {len(self.steps)} question(s) walked, {self.orders} order(s), {self.fills} "
            f"fill(s), {self.unfilled} post(s) left resting; fees {self.fees:.2f}, funding "
            f"{self.funding:+.2f}{missing}",
            f"  equity {self.terms.initial_balance} -> {self.final_equity:.2f} "
            f"({self.total_return:+.2%}){ruin}",
        ]


@dataclass(frozen=True)
class Series:
    """A run's bars and settlements in time order, checked once and walked by every path.

    The caller that walks several drivers over one run builds it once
    (:meth:`of` sorts; the constructor checks) and hands it to each
    :func:`simulate`.
    """

    bars: tuple[Bar, ...]
    funding: tuple[Funding, ...]

    def __post_init__(self) -> None:
        for earlier, later in zip(self.bars, self.bars[1:], strict=False):
            if later.open_time < earlier.close_time:
                raise AccountError(
                    f"bars opening {earlier.open_time} and {later.open_time} overlap or are "
                    "out of order"
                )
        for first, second in zip(self.funding, self.funding[1:], strict=False):
            if second.time <= first.time:
                raise AccountError(
                    f"funding settlements stamped {first.time} and {second.time} repeat or are "
                    "out of order"
                )

    @classmethod
    def of(cls, bars: Iterable[Bar], funding: Iterable[Funding]) -> Series:
        return cls(
            tuple(sorted(bars, key=lambda b: b.open_time)),
            tuple(sorted(funding, key=lambda f: f.time)),
        )

    @property
    def gaps(self) -> int:
        """How many times a bar does not open where the previous one closed: holes in the store."""
        return sum(
            1
            for earlier, later in zip(self.bars, self.bars[1:], strict=False)
            if later.open_time != earlier.close_time + 1
        )

    def bar_at(self, at_ms: int) -> Bar | None:
        """The bar holding ``at_ms`` (``open <= at <= close``), or ``None`` when none does.

        The same bar the split places the question in (``score.bar_open_ms``
        floors the instant to the grid), so the bar a post is judged against
        is always inside the question's own segment and never past the
        holdout lock. A gap in the store is ``None``, never a later bar: a
        post judged against a bar that opens after the decision would read
        the future. The paper scheduler rolls (next cycle = last decision +
        one bar), so a decision lands somewhere inside a bar; the fixture's
        "one millisecond before the boundary" sits on the closing bar's last
        instant and is judged against that bar.
        """
        index = bisect_right(self.bars, at_ms, key=lambda b: b.open_time) - 1
        if index >= 0 and self.bars[index].close_time >= at_ms:
            return self.bars[index]
        return None

    def close_before(self, at_ms: int) -> Decimal | None:
        """The close of the last bar closed at or before ``at_ms``."""
        index = bisect_right(self.bars, at_ms, key=lambda b: b.close_time) - 1
        return self.bars[index].close if index >= 0 else None

    def settlements(self, after_ms: int, until_ms: int) -> Sequence[Funding]:
        """The settlements stamped in ``(after, until]``."""
        lo = bisect_right(self.funding, after_ms, key=lambda f: f.time)
        hi = bisect_right(self.funding, until_ms, key=lambda f: f.time)
        return self.funding[lo:hi]


def simulate(decisions: Sequence[Decision], *, terms: Terms, series: Series) -> Path:
    """Walk the account through ``decisions`` in time order. The one entry point.

    ``series`` holds the run's bars from the research store (the fill model
    reads the one each decision falls in; the funding values the position at
    the last close before each settlement) and its hourly settlements. Both
    may stop where the holdout lock stops them: a question whose bar is
    missing is refused by name on a maker run (a post cannot be judged
    without the bar), and a settlement missing while a position is held is
    counted on the step, never zeroed.
    """
    ordered = sorted(decisions, key=lambda d: d.at_ms)
    seen: set[str] = set()
    for decision in ordered:
        if decision.input_id in seen:
            raise AccountError(f"{decision.input_id}: decided twice")
        seen.add(decision.input_id)
    for earlier, later in zip(ordered, ordered[1:], strict=False):
        if later.at_ms == earlier.at_ms:
            raise AccountError(
                f"{earlier.input_id} and {later.input_id}: decided at the same instant"
            )
    steps: list[Step] = []
    position = PositionState(coin=terms.coin, size=Decimal(0), entry_price=None)
    wallet = terms.initial_balance
    resting: Target | None = None
    with localcontext(DECIMAL_CONTEXT):
        for index, decision in enumerate(ordered):
            funding_paid = Decimal(0)
            missing = 0
            if index:
                previous = ordered[index - 1]
                funding_paid, missing = _settle(
                    series, position, previous.at_ms, decision.at_ms, previous.mark
                )
                wallet += funding_paid
            equity = wallet + _unrealized(position, decision.mark)
            target: Target | None = None
            carried = False
            filled: bool | None = None
            price: Decimal | None = None
            fee = Decimal(0)
            ruined = equity <= 0
            if not ruined:
                carried = decision.target is None and resting is not None
                target = resting if decision.target is None else decision.target
                resting = None
                if target is not None:
                    try:
                        order = _order(target, decision, position, equity, terms)
                        if order is not None:
                            side, qty = order
                            filled, price = _fill(terms, series, decision, side)
                            if filled:
                                effect = compute_fill_effect(
                                    position,
                                    side=side,
                                    qty=qty,
                                    price=price,
                                    fee_rate=terms.fee_rate,
                                )
                                position = effect.position
                                wallet += effect.wallet_delta
                                fee = effect.fee
                            else:
                                resting = target
                    except AccountError:
                        raise
                    except (ValueError, InvalidOperation) as exc:
                        # The fill model's and the books' own refusals (a post
                        # rounded to nothing on a coarse tick, a zero quantity),
                        # named by the question that hit them.
                        raise AccountError(f"{decision.input_id}: {exc}") from exc
            steps.append(
                Step(
                    input_id=decision.input_id,
                    at_ms=decision.at_ms,
                    segment=decision.segment,
                    mark=decision.mark,
                    target=target,
                    carried=carried,
                    filled=filled,
                    fill_price=price,
                    fee=fee,
                    funding=funding_paid,
                    funding_missing=missing,
                    size=position.size,
                    entry=position.entry_price,
                    wallet=wallet,
                    equity=equity,
                )
            )
            if ruined:
                break
    # Each decision's return is the equity move to the next question; the
    # ruin step, if any, is last and has none.
    finished = [
        step
        if index + 1 == len(steps)
        else replace(step, ret=float(steps[index + 1].equity / step.equity) - 1.0)
        for index, step in enumerate(steps)
    ]
    return Path(terms=terms, steps=tuple(finished))


def _unrealized(position: PositionState, mark: Decimal) -> Decimal:
    if position.size == 0:
        return Decimal(0)
    assert position.entry_price is not None
    return unrealized_pnl(position.size, mark, position.entry_price)


def _settle(
    series: Series, position: PositionState, after_ms: int, until_ms: int, mark: Decimal
) -> tuple[Decimal, int]:
    """Funding the held position earned or paid in ``(after, until]``, and the settlements missing."""
    if position.size == 0:
        return Decimal(0), 0
    paid = Decimal(0)
    due = series.settlements(after_ms, until_ms)
    for point in due:
        price = series.close_before(point.time)
        paid += funding_pnl(position.size * (mark if price is None else price), point.rate)
    # The hours that turn inside ``(after, until]``: the venue stamps each
    # settlement a few milliseconds after its hour, so a boundary at
    # ``after`` itself belongs to the previous slice.
    expected = until_ms // FUNDING_INTERVAL_MS - after_ms // FUNDING_INTERVAL_MS
    return paid, max(0, expected - len(due))


def _order(
    target: Target, decision: Decision, position: PositionState, equity: Decimal, terms: Terms
) -> tuple[Side, Decimal] | None:
    """The order that takes the position to ``target``: ``(side, qty)``, or ``None`` for none.

    The gate's order-necessity rules (``risk_gate.evaluate`` step 7) on the
    account's own book: a flat ask over a flat book and an exact ask place
    nothing; a same-side ask less than the run's deadband away from the
    margin held places nothing; a flat ask closes the whole position so no
    dust is left behind by the division.
    """
    if target.side is TargetSide.FLAT:
        if position.size == 0:
            return None
        return (Side.SELL if position.size > 0 else Side.BUY), abs(position.size)
    held = position.size * decision.mark
    if held != 0 and (held > 0) == (target.sign > 0):
        margin_held = abs(held) / decision.leverage / equity * _HUNDRED
        if abs(target.margin_pct - margin_held) < terms.deadband_pct:
            return None
    wanted = target.sign * equity * target.margin_pct / _HUNDRED * decision.leverage
    delta = wanted - held
    if delta == 0:
        return None
    return (Side.BUY if delta > 0 else Side.SELL), abs(delta) / decision.mark


def _fill(terms: Terms, series: Series, decision: Decision, side: Side) -> tuple[bool, Decimal]:
    """Whether the order fills at this question, and at what price (module docstring)."""
    if terms.costs.fill_role is FillRole.TAKER:
        return True, fill_price(decision.mark, side, terms.slippage_bps)
    post = maker_post_price(decision.mark, side, terms.half_spread_bps, terms.tick)
    bar = series.bar_at(decision.at_ms)
    if bar is None:
        raise AccountError(
            f"{decision.input_id}: no bar in the research store covers "
            f"{from_epoch_ms(decision.at_ms):%Y-%m-%d %H:%M} UTC, so whether the maker post "
            "fills cannot be judged; fetch the store so that its bars cover the run"
        )
    extreme = bar.low if side is Side.BUY else bar.high
    return maker_would_fill(extreme, side, post, terms.tick), post
