"""One definition of the §5 / §7 audit-row assembly (phase2-data).

``ai_inputs`` (30 columns) and ``ai_outputs`` (24 columns) are written from two
call sites — the paper scheduler and the live decision driver — that previously
each carried a full copy of the field mapping ("mirror of PaperScheduler",
live/decision.py). A schema column added to one copy but not the other would
export silent NULLs and trip the mode-agnostic validate on a healthy run, so
the mapping lives here exactly once.

These are pure adapters: no policy of their own. The deliberate paper/live
differences stay visible at the call sites and arrive as parameters:

- how the account ledger is acquired (paper raises its own ``ValueError``
  pointing at ``accounting.initialize_run``; live uses
  ``repo.require_current_account_state``) — the caller passes ``ledger``;
- ``remaining_twap_qty`` (paper sums the active plans' remaining quantities;
  live reports None until fills are attributed to plans) — the caller passes
  the value;
- where ``mark_price`` / ``account_equity`` for the output row come from
  (paper ``PlanStartResult``, live ``PlanRegistration``) — the caller passes
  the values.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from decimal import Decimal, localcontext
from typing import TYPE_CHECKING

from ..domains.perp.margin import position_notional
from ..domains.perp.risk_gate import CurrentPositionState
from . import repository as repo
from .db import Database
from .models import DECIMAL_CONTEXT, PositionState

if TYPE_CHECKING:
    from ..domains.perp.risk_gate import RiskGateResult
    from ..domains.perp.target_decision import ParsedDecision
    from ..paper.accounting import AccountMetrics
    from ..paper.scheduler import DecisionInput
    from .models import AccountLedger

__all__ = ["write_ai_input", "write_ai_output"]


def write_ai_input(
    db: Database,
    *,
    now: datetime,
    input_id: str,
    attempt_id: str,
    decision_input: DecisionInput,
    mode: str,
    run_id: str,
    symbol: str,
    ledger: AccountLedger,
    position: PositionState,
    metrics: AccountMetrics,
    leverage: Decimal,
    max_target_margin_pct: int,
    liquidation_price: Decimal | None,
    active_twap: bool,
    remaining_twap_qty: Decimal | None,
) -> None:
    """Record what the AI is about to see: market context + account state (§5).

    Opens its own transaction — call it OUTSIDE any open ``db.transaction()``
    (``Database`` rejects nesting). The reads run in autocommit beforehand,
    exactly as the historical call sites did; the one transaction then writes
    the ``ai_inputs`` row and stamps ``input_id`` onto the attempt row, so a
    crash never leaves an attempt pointing at an input that was never
    persisted. ``liquidation_price`` is the engine-owned estimate, computed by
    the caller (the same value the engine trades on).
    """
    ctx = decision_input.context
    conn = db.conn
    protection = repo.get_position_protection(conn, run_id, symbol)
    stop_loss, take_profit = protection if protection is not None else (None, None)
    # The same imputation the gate sizes on, and the §6.1 canonical notional —
    # never re-derived inline (margin.py forbids drifting copies).
    state = CurrentPositionState.from_signed_size(
        position.size, mark=ctx.mark_price, equity=metrics.account_equity, leverage=leverage
    )
    with localcontext(DECIMAL_CONTEXT):
        notional = position_notional(position.size, ctx.mark_price)
    last_fill = repo.last_fill_time(conn, run_id)
    side = "flat" if position.is_flat else ("long" if position.is_long else "short")
    with db.transaction() as txn:
        repo.insert_ai_input(
            txn,
            input_id=input_id,
            timestamp=now,
            mode=mode,
            run_id=run_id,
            symbol=symbol,
            candle_start=decision_input.candle_start,
            candle_end=decision_input.candle_end,
            mark_price=ctx.mark_price,
            mid_price=ctx.mid_price,
            funding_rate=ctx.funding_rate,
            wallet_balance=ledger.wallet_balance,
            account_equity=metrics.account_equity,
            available_balance=metrics.available_balance,
            realized_pnl=ledger.realized_pnl,
            unrealized_pnl=metrics.unrealized_pnl,
            total_fees=ledger.total_fees,
            net_funding_pnl=ledger.net_funding_pnl,
            effective_leverage=metrics.effective_leverage,
            margin_ratio=metrics.margin_ratio,
            current_position_side=side,
            current_position_size=position.size,
            entry_price=position.entry_price,
            position_notional=notional,
            current_margin_pct=state.margin_pct,
            configured_leverage=leverage,
            estimated_liquidation_price=liquidation_price,
            stop_loss_price=stop_loss,
            take_profit_price=take_profit,
            active_twap=active_twap,
            remaining_twap_qty=remaining_twap_qty,
            last_fill_time=last_fill,
            max_target_margin_pct=Decimal(max_target_margin_pct),
            input_payload_path=decision_input.input_payload_path,
            input_payload_hash=decision_input.input_payload_hash,
            prompt_version=decision_input.prompt_version,
            model=decision_input.model,
        )
        repo.update_decision_attempt(txn, attempt_id, input_id=input_id, timestamp=now)


def write_ai_output(
    conn: sqlite3.Connection,
    *,
    now: datetime,
    output_id: str,
    input_id: str,
    decision_attempt_id: str,
    mode: str,
    run_id: str,
    symbol: str,
    gate: RiskGateResult,
    parsed: ParsedDecision,
    mark_price: Decimal | None,
    account_equity: Decimal | None,
) -> None:
    """One §7 ``ai_outputs`` row from the gate's outcome (its own sizing inputs).

    Must be called INSIDE the caller's open transaction (``conn`` is the
    transaction connection) — the sibling ``write_ai_input`` owns its own.
    ``mark_price`` / ``account_equity`` mirror their dataclass sources
    (``Decimal | None`` — present iff the gate ran; both callers assert the
    gate ran before calling).
    """
    decision = parsed.decision
    # §7: decision_reason must never be empty — a fail-closed decision may
    # carry no rationale, in which case the contract violation is the reason.
    reason = decision.rationale or parsed.invalid_reason or "(no rationale)"
    repo.insert_ai_output(
        conn,
        output_id=output_id,
        timestamp=now,
        mode=mode,
        run_id=run_id,
        input_id=input_id,
        decision_attempt_id=decision_attempt_id,
        symbol=symbol,
        decision_mode=gate.decision_mode.value,
        target_side=None if gate.target_side is None else gate.target_side.value,
        requested_target_margin_pct=gate.requested_target_margin_pct,
        approved_target_margin_pct=gate.approved_target_margin_pct,
        risk_action=gate.risk_action.value,
        risk_reason=gate.risk_reason,
        target_margin=gate.target_margin,
        configured_leverage=gate.configured_leverage,
        target_notional=gate.target_notional,
        target_signed_notional=gate.target_signed_notional,
        current_signed_notional=gate.current_signed_notional,
        delta_notional=gate.delta_notional,
        mark_price=mark_price,
        account_equity=account_equity,
        confidence=gate.confidence,
        decision_reason=reason,
        key_risks=json.dumps(list(decision.key_risks), ensure_ascii=False),
        order_created=gate.order_created,
        no_order_reason=gate.no_order_reason,
    )
