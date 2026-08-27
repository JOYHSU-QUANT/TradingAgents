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
from datetime import datetime
from decimal import Decimal, localcontext

from ...common.decimal_context import DECIMAL_CONTEXT
from .margin import account_equity, unrealized_pnl
from .risk_gate import CurrentPositionState
from .schema import MarginalCostRow, PositionContext, PositionSide, derive_round_trip_rate

__all__ = ["MAX_COST_ROWS", "build_position_context", "display_targets"]

logger = logging.getLogger(__name__)

# Funding on Hyperliquid is charged hourly (``MarketSnapshot.funding`` is the
# per-hour rate); the section states holding cost per 8h — one conventional
# funding "period" — so the number is large enough to read against a fee.
_HOLDING_COST_HOURS = 8

# The cost table is bounded: the paper grid is step 1 over 0..60 (61 legal
# targets), and 61 rows would bury the two facts the section carries. The cost
# is exactly linear in the distance moved, so a sampled table plus the
# per-point rate (rendered beside it) loses nothing — every legal target
# between two rows costs what its distance says. 13 rows is 0/5/.../60 on the
# paper grid: coarse enough to scan, fine enough that no legal target is more
# than 2 points from a printed row.
MAX_COST_ROWS = 13


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
    *,
    size: Decimal,
    entry_price: Decimal | None,
    wallet_balance: Decimal,
    mark: Decimal,
    leverage: Decimal,
    funding_rate: Decimal,
    grid_min: int,
    grid_max: int,
    grid_step: int,
    taker_fee_rate: Decimal,
    slippage_bps: Decimal,
    last_fill_at: datetime | None,
) -> PositionContext | None:
    """Price the account's position and every displayed legal move, or ``None``.

    ``size`` is the signed position size from the books (``0`` = flat, and
    then ``entry_price`` is ignored); ``wallet_balance`` is the ledger's
    (realized PnL, fees and funding already posted — execution §6.1), so
    equity is ``wallet_balance + unrealized`` at ``mark``. ``leverage`` is
    the CONFIGURED ``risk.leverage`` — the same imputation the gate sizes on
    (``CurrentPositionState.from_signed_size``); the local books carry no
    per-position leverage. ``funding_rate`` is the current hourly rate;
    ``grid_*`` is the legal target grid with ``grid_max`` already the
    EFFECTIVE ceiling (``risk_gate.effective_max_target_margin_pct``), so no
    row names a margin the gate would clamp.

    ``None`` — the section is omitted — when equity is not positive: a
    margin-called account has no basis to price a move against, and the gate
    already refuses every directional target on it (``no_account_equity``).
    """
    if mark <= 0:
        raise ValueError(f"mark must be > 0, got {mark}")
    if leverage <= 0:
        raise ValueError(f"leverage must be > 0, got {leverage}")
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
        equity = account_equity(wallet_balance, unrealized)
        if equity <= 0:
            logger.warning(
                "position section omitted: account equity %s is not positive at mark %s "
                "(wallet %s) — nothing to price a move against",
                equity,
                mark,
                wallet_balance,
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
                last_fill_at=last_fill_at,
                holding_cost_8h=None,
                taker_fee_rate=taker_fee_rate,
                slippage_bps=slippage_bps,
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
        # Signed like the ledger's funding posting: a long pays a positive
        # rate, a short receives it. Positive here = the position PAYS.
        holding = funding_rate * _HOLDING_COST_HOURS * state.signed_notional
        rate = derive_round_trip_rate(taker_fee_rate, slippage_bps)
        rows = []
        for target in display_targets(grid_min, grid_max, grid_step):
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
            last_fill_at=last_fill_at,
            holding_cost_8h=holding,
            taker_fee_rate=taker_fee_rate,
            slippage_bps=slippage_bps,
            cost_rows=tuple(rows),
        )
