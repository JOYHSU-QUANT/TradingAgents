"""The prompt's position section: the account's own position, priced at the margin.

Pure (no I/O, no clock); every money figure is :class:`~decimal.Decimal`
under the shared ``DECIMAL_CONTEXT``. :func:`build_position_context` turns
what the local books know (signed size, entry, wallet balance, last fill) plus
the cycle's mark and funding into a :class:`~.schema.PositionContext`, which
:mod:`.prompt_context` renders as the ``Position:`` section.

Why it exists (2026-08-27 ``/paper-review`` of paper-BTC-2): the model resized
in >= 10-point jumps at the deadband's edge, reducing and then re-adding the
same exposure within days — churn the gate's thresholds shaped rather than
stopped. The context was position-blind, so the model had no basis for
weighing a resize against its cost. This section gives it two things and
nothing more: where it stands, and what each legal move costs as a round
trip, restated as the favourable price move (bps of the traded notional) that
would pay for it. MARGINAL cost only — never the run's accumulated fees,
which are sunk and would only invite recovering them (decided 2026-07-13).

Facts only. Which gate bar a given target would face (the open / flip / flat
exemptions from the resize confidence bar) stays out of the prompt; see
``target_decision.decision_format_instructions`` for why.

Fail-closed like :mod:`.volume_profile`: an account whose books cannot price
a move (no ledger yet, equity <= 0) yields ``None`` plus a WARNING and the
whole section is omitted — never a header over ``n/a`` rows.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, localcontext

from ...common.constants import HOLDING_COST_HOURS
from ...common.decimal_context import DECIMAL_CONTEXT
from .margin import account_equity, funding_cost, unrealized_pnl
from .risk_gate import CurrentPositionState
from .schema import MarginalCostRow, PositionContext, PositionSide, derive_round_trip_rate

__all__ = [
    "MAX_COST_ROWS",
    "BookPosition",
    "PositionInputs",
    "PositionPricing",
    "PositionSource",
    "build_position_context",
    "display_targets",
]

logger = logging.getLogger(__name__)

# The cost table is bounded: the paper grid is step 1 over 0..60 (61 legal
# targets), and 61 rows would bury the two facts the section carries. The cost
# is exactly linear in the distance moved, so a sampled table plus the
# per-point rate (rendered beside it) loses nothing — every legal target
# between two rows costs what its distance says. 13 rows is 0/5/.../60 on the
# paper grid: coarse enough to scan, fine enough that no legal target is more
# than 2 points from a printed row.
MAX_COST_ROWS = 13


@dataclass(frozen=True)
class BookPosition:
    """The books' position facts: signed size, entry, wallet balance, newest fill.

    Read from a run's store by ``paper.position_facts.read_book_position`` (the
    paper ledger and the live store keep the same tables) and priced here. It
    lives in ``domains/`` rather than beside its reader because the context
    builder — which must not import ``paper/`` — names it as an input.

    ``size == 0`` is flat (``entry_price`` then ``None``, as on
    ``persistence.models.PositionState``). ``wallet_balance`` is the ledger's
    — realized PnL, fees and funding already posted — so the pricer adds only
    unrealized PnL at its own mark to reach equity. ``last_fill_at`` is the
    run's newest fill of ANY kind (the same ``fills.timestamp`` maximum
    ``ai_inputs.last_fill_time`` records), not the position's opening time —
    the books do not keep one, and "when did this position last change" is the
    honest fact for a churn-aware prompt.
    """

    size: Decimal
    entry_price: Decimal | None
    wallet_balance: Decimal
    last_fill_at: datetime | None

    def __post_init__(self) -> None:
        # The same pairing ``persistence.models.PositionState`` enforces on the
        # row this is read from, restated on the type that leaves the store —
        # so a reader that ever builds one from somewhere else (a replay
        # harness, a fixture) cannot smuggle in the two states the pricer
        # would otherwise mishandle: a flat position carrying a stale entry
        # (silently dropped on the flat branch, no log, no symptom) or an open
        # one entered at zero or below (garbage equity that either surfaces
        # three modules later in ``PositionContext``, or comes out
        # non-positive and gets reported as insolvency, which it is not).
        if self.size == 0:
            if self.entry_price is not None:
                raise ValueError("a flat BookPosition (size 0) carries no entry_price")
        elif self.entry_price is None or self.entry_price <= 0:
            raise ValueError(
                f"an open BookPosition must carry entry_price > 0, got {self.entry_price}"
            )


@dataclass(frozen=True)
class PositionPricing:
    """What a move costs and which moves are legal — the config half of the section.

    ``leverage`` is the CONFIGURED ``risk.leverage`` (the same imputation the
    gate sizes on; the local books carry no per-position leverage).
    ``grid_max`` must already be the EFFECTIVE ceiling
    (``risk_gate.effective_max_target_margin_pct``) so no printed row names a
    margin the gate would clamp — the caller hands that same value to the
    format block, which is what keeps the cost table and the advertised
    ceiling from disagreeing.
    """

    leverage: Decimal
    grid_min: int
    grid_max: int
    grid_step: int
    taker_fee_rate: Decimal
    slippage_bps: Decimal

    def __post_init__(self) -> None:
        # Every field, each named in the message that rejects it: a bad config
        # value caught here still carries the name it came from, where the same
        # value found downstream surfaces as an arithmetic surprise in another
        # module. WHERE it fails is the whole merit — the bad PROMPT is already
        # unreachable, since an inverted grid leaves ``display_targets`` with no
        # points and ``PositionContext`` then refuses an open position carrying
        # no cost rows. But that refusal comes at pricing time, deep in a cycle,
        # naming a DTO rather than the config key; these come at construction,
        # which for the daemon is before it decides anything.
        # ``DecisionConfig``/``RiskConfig`` already constrain every one of these
        # for the one production caller, so this is defence in depth for any
        # other construction (a replay harness, a per-coin override).
        for name in ("taker_fee_rate", "slippage_bps"):
            if getattr(self, name) < 0:
                raise ValueError(f"PositionPricing.{name} must be >= 0, got {getattr(self, name)}")
        if self.leverage <= 0:
            raise ValueError(f"PositionPricing.leverage must be > 0, got {self.leverage}")
        if self.grid_step < 1:
            raise ValueError(f"PositionPricing.grid_step must be >= 1, got {self.grid_step}")
        # ``<=``, not ``<``: the effective ceiling is
        # ``min(ai_target_margin_max_pct, risk.max_target_margin_pct)``, which
        # a legal config (grid min 60, cap 60) can drive down onto the floor.
        if not 0 <= self.grid_min <= self.grid_max <= 100:
            raise ValueError(
                "PositionPricing needs 0 <= grid_min <= grid_max <= 100, got "
                f"grid_min={self.grid_min}, grid_max={self.grid_max}"
            )


@dataclass(frozen=True)
class PositionInputs:
    """Everything the position section needs that the market fetch does not supply.

    One optional bundle rather than two independent parameters: "the books say
    X, priced under these rules" is a single state, and a caller must not be
    able to supply half of it. ``None`` in
    :func:`.context_builder.build_market_context` is the position-blind
    context — no books wired (the one-shot CLI) or none seeded yet.
    """

    book: BookPosition
    pricing: PositionPricing


# How a wiring hands the daemon provider its books: bound over the run's store
# at construction, called once per cycle (the fresh-run provider is built
# before the ledger is seeded, so the read must be able to answer "no books
# yet"). Named so the seam is a declared type rather than a bare callable
# passed by keyword (issue #134).
PositionSource = Callable[[], BookPosition | None]


def display_targets(lo: int, hi: int, step: int, max_rows: int = MAX_COST_ROWS) -> list[int]:
    """The legal grid ``lo, lo+step, ..., hi`` — every point, or a bounded sample.

    Under ``max_rows`` points the whole grid is returned. Above it, every
    k-th point with the smallest ``k`` that fits, and ``hi`` appended if the
    stride skipped it — the ceiling is a legal target the model is told about
    elsewhere, so its row must exist. ``lo`` is always the first row (it is
    ``0`` on every grid in use: the flat row).
    """
    if step < 1:
        raise ValueError(f"step must be >= 1, got {step}")
    if max_rows < 2:
        raise ValueError(f"max_rows must be >= 2, got {max_rows}")
    points = list(range(lo, hi + 1, step))
    if len(points) <= max_rows:
        return points
    stride = math.ceil((len(points) - 1) / (max_rows - 1))
    sampled = points[::stride]
    if sampled[-1] != points[-1]:
        sampled.append(points[-1])
    return sampled


def build_position_context(
    inputs: PositionInputs, *, mark: Decimal, funding_rate: Decimal
) -> PositionContext | None:
    """Price the account's position and every displayed legal move, or ``None``.

    ``inputs`` is the books (:class:`BookPosition`) plus the rules a move is
    priced under (:class:`PositionPricing`); ``mark`` and ``funding_rate`` are
    THIS cycle's, from the same market snapshot the rest of the context is
    built from — which is why the section can never quote a notional or a PnL
    at a mark the ``Mark:`` line above it disagrees with.

    ``None`` — the section is omitted — when equity is not positive: a
    margin-called account has no basis to price a move against, and the gate
    already refuses every directional target on it (``no_account_equity``).
    """
    if mark <= 0:
        raise ValueError(f"mark must be > 0, got {mark}")
    book, pricing = inputs.book, inputs.pricing
    size, entry_price, leverage = book.size, book.entry_price, pricing.leverage
    with localcontext(DECIMAL_CONTEXT):
        # The §6.1 formulas by name, never re-derived inline (margin.py's
        # rule): a flat account has no unrealized PnL, so its equity IS the
        # wallet.
        if size == 0:
            unrealized = Decimal(0)
        else:
            if entry_price is None:
                raise ValueError("an open position needs an entry_price")
            unrealized = unrealized_pnl(size, mark, entry_price)
        equity = account_equity(book.wallet_balance, unrealized)
        if equity <= 0:
            logger.warning(
                "position section omitted: account equity %s is not positive at mark %s "
                "(wallet %s) — nothing to price a move against",
                equity,
                mark,
                book.wallet_balance,
            )
            return None
        if size == 0:
            return PositionContext(
                side=None,
                size=Decimal(0),
                entry_price=None,
                unrealized_pnl=None,
                notional=Decimal(0),
                margin_pct=None,
                equity=equity,
                leverage=leverage,
                last_fill_at=book.last_fill_at,
                holding_cost_8h=None,
                taker_fee_rate=pricing.taker_fee_rate,
                slippage_bps=pricing.slippage_bps,
            )
        # The gate's own imputation (notional at mark, margin at the
        # configured leverage) — the ONE derivation, so the margin% the model
        # reads is the margin% the deadband is measured against, and a change
        # to that rule reaches both at once.
        state = CurrentPositionState.from_signed_size(
            size, mark=mark, equity=equity, leverage=leverage
        )
        assert state.margin_pct is not None  # equity > 0 was checked above
        notional = abs(state.signed_notional)
        margin_pct = state.margin_pct
        # The §6.5 hourly formula by name, over the horizon this section
        # states. COST-signed: a long pays a positive rate, a short receives
        # it, so positive here = the position PAYS. The books state the same
        # hour income-signed (``paper.accounting.funding_pnl``); one formula,
        # one negation, so the two cannot drift apart (issue #134).
        holding = funding_cost(state.signed_notional, funding_rate) * HOLDING_COST_HOURS
        rate = derive_round_trip_rate(pricing.taker_fee_rate, pricing.slippage_bps)
        rows = []
        for target in display_targets(pricing.grid_min, pricing.grid_max, pricing.grid_step):
            trade_notional = abs(Decimal(target) - margin_pct) / 100 * equity * leverage
            if trade_notional == 0:
                continue  # already there: nothing to trade, no breakeven to state
            cost = trade_notional * rate
            rows.append(
                MarginalCostRow(
                    target_margin_pct=target,
                    trade_notional=trade_notional,
                    round_trip_cost=cost,
                )
            )
        return PositionContext(
            side=PositionSide.LONG if size > 0 else PositionSide.SHORT,
            size=size,
            entry_price=entry_price,
            unrealized_pnl=unrealized,
            notional=notional,
            margin_pct=margin_pct,
            equity=equity,
            leverage=leverage,
            last_fill_at=book.last_fill_at,
            holding_cost_8h=holding,
            taker_fee_rate=pricing.taker_fee_rate,
            slippage_bps=pricing.slippage_bps,
            cost_rows=tuple(rows),
        )
