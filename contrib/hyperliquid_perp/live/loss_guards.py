"""§10 live risk checks and loss guards (PR 5).

The Phase 2 RiskGate (:func:`domains.perp.risk_gate.evaluate`) still sizes every
target — these are the LIVE-only §10 additions layered around it, not a rewrite
of it (§2.1: the paper engine is untouched, the RiskGate is shared). Four checks:

- **§10.1 hard notional cap** — ``approved_notional <= live.safety.max_notional_usdc``
  is a separate ceiling from the AI gate's margin-allocation cap;
- **§10.3 daily loss cap** — a UTC-day drawdown breach enters RECOVERABLE safe
  mode (position and SL/TP kept, new entry/rebalance stopped), auto-releasing at
  the next UTC 00:00 through the §10.3 time gate in
  :meth:`SafeModeManager.try_auto_recover`;
- **§10.4 consecutive loss cap** — a position segment (flat → flat) whose net
  realized PnL is negative counts one loss; a profitable settlement resets the
  count; three in a row enters MANUAL safe mode (a human must confirm, §13.6);
- **§10.5 max open orders** — the count of bot-owned exchange open orders (resting
  SL/TP included) must stay below ``live.safety.max_open_orders`` before any new
  order.

The two loss breakers drive the §13 :class:`~.safe_mode.SafeModeManager`; the
notional and open-order checks are per-cycle refusals the engine consults before
building a plan. Durable state lives on ``scheduler_state`` (§16.6): the daily
baseline (``day_start_equity`` / ``day_start_date``), the consecutive-loss
counter (``consecutive_loss_count``) and its segment anchor
(``last_settlement_wallet_balance``, schema v7). All money math pins
``DECIMAL_CONTEXT``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, localcontext
from typing import Any

from ..domains.perp.margin import DECIMAL_CONTEXT
from ..persistence import repository as repo
from ..persistence.db import Database
from .config import LiveSafetyConfig
from .safe_mode import REASON_CONSECUTIVE_LOSS, REASON_DAILY_LOSS, SafeModeManager

__all__ = ["DailyLossResult", "LossGuards", "SettlementResult"]

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DailyLossResult:
    """The outcome of one §10.3 daily-loss evaluation."""

    baseline_equity: Decimal
    current_equity: Decimal
    drawdown_pct: Decimal  # (baseline - current) / baseline * 100; 0 when baseline <= 0
    breached: bool
    rolled: bool  # the UTC-day baseline rolled to a new day this call
    entered_safe_mode: bool  # this call newly entered (or re-entered a fresh) recoverable safe mode


@dataclass(frozen=True)
class SettlementResult:
    """The outcome of recording one §10.4 position settlement (flat transition)."""

    segment_pnl: Decimal  # wallet delta since the previous settlement anchor
    is_loss: bool
    consecutive_loss_count: int
    entered_manual: bool  # this call escalated to manual safe mode (the 3rd loss)
    anchored: bool  # this call only established the anchor (no prior baseline to diff)


class LossGuards:
    """One run's §10 loss-guard and live-risk state, persisted via ``scheduler_state``."""

    def __init__(
        self,
        *,
        db: Database,
        run_id: str,
        safety: LiveSafetyConfig,
        safe_mode: SafeModeManager,
        day_baseline_source: Callable[[], Decimal] | None = None,
    ) -> None:
        self._db = db
        self._run_id = run_id
        self._safety = safety
        self._safe_mode = safe_mode
        # §10.3 rule 1: the day baseline is the EXCHANGE's reconciled equity
        # (accountValue), fetched once at each UTC-day roll. Optional so tests
        # and non-live contexts fall back to the caller-passed local equity;
        # a fetch failure also falls back (logged) — the roll must never stall
        # the tick.
        self._day_baseline_source = day_baseline_source

    # -- §10.1 hard notional cap ---------------------------------------------

    def notional_exceeds_cap(self, approved_notional: Decimal) -> bool:
        """§10.1: an approved target notional above the live hard cap is refused.

        Distinct from the AI gate's margin-allocation cap — the two are layered
        (``validate_live_risk_consistency`` keeps ``live.safety`` no looser than
        ``risk:``), so this fires only if the gate ever sized past the live
        ceiling. ``max_notional_usdc`` is the notional, not the margin.
        """
        return approved_notional > self._safety.max_notional_usdc

    # -- §10.5 max open orders ------------------------------------------------

    def count_bot_open_orders(self, open_orders: Any) -> int:
        """Count exchange open orders this bot owns (§10.5; SL/TP included).

        Ownership is the §19.3 reverse lookup: an order is bot-owned iff its
        ``cloid`` maps to a ``cloid_registry`` row. Non-dict entries and orders
        without a registry-known cloid are simply not counted here — a non-bot
        order is a §12.3 reconciliation concern, not an open-order-budget one.
        """
        if not isinstance(open_orders, list):
            return 0
        count = 0
        for order in open_orders:
            if not isinstance(order, dict):
                continue
            cloid = order.get("cloid")
            if isinstance(cloid, str) and repo.get_cloid_by_hex(self._db.conn, cloid) is not None:
                count += 1
        return count

    def max_open_orders_reached(self, open_orders: Any) -> bool:
        """§10.5: is the bot-owned open-order count at or above the cap?"""
        return self.count_bot_open_orders(open_orders) >= self._safety.max_open_orders

    # -- §10.3 daily loss cap -------------------------------------------------

    def evaluate_daily_loss(self, *, account_equity: Decimal, now: datetime) -> DailyLossResult:
        """§10.3: roll the UTC-day baseline, then breach-check the drawdown.

        On the first evaluation of a new UTC day (or a run with no baseline yet)
        the baseline rolls to the current reconciled equity — this is the §10.3
        rule 1 "record the day's opening equity at UTC 00:00", captured at the
        first tick of the day (within one 30s tick of midnight). A drawdown past
        ``max_daily_loss_pct`` (unrealized included — the caller passes reconciled
        account equity) enters recoverable safe mode; the entry is idempotent, so
        calling this every tick while breached keeps the latch without spamming
        history. The safe mode auto-releases past the next UTC 00:00 (the baseline
        rolls and the drawdown resets) via the §10.3 gate in ``try_auto_recover``.
        """
        today = now.astimezone(timezone.utc).date().isoformat()
        row = repo.get_scheduler_state(self._db.conn, self._run_id)
        stored_date = row["day_start_date"] if row is not None else None
        stored_equity = row["day_start_equity"] if row is not None else None
        rolled = False
        if stored_date != today or stored_equity is None:
            baseline = account_equity
            if self._day_baseline_source is not None:
                # §10.3 rule 1: the opening equity is the exchange's own
                # reconciled accountValue, not the local ledger's view — the
                # local value lags pending fees/funding, and a skewed baseline
                # would mis-anchor the whole day's drawdown measurement.
                try:
                    baseline = self._day_baseline_source()
                except Exception:  # noqa: BLE001 — the roll must never stall the tick
                    logger.warning(
                        "day-roll baseline fetch from the exchange failed — "
                        "falling back to local equity %s",
                        account_equity,
                        exc_info=True,
                    )
                    baseline = account_equity
            with self._db.transaction() as conn:
                repo.upsert_scheduler_state(
                    conn,
                    self._run_id,
                    day_start_equity=baseline,
                    day_start_date=today,
                    updated_at=now,
                )
            rolled = True
        else:
            baseline = Decimal(stored_equity)

        with localcontext(DECIMAL_CONTEXT):
            if baseline > 0:
                drawdown_pct = (baseline - account_equity) / baseline * 100
            else:
                # A non-positive baseline cannot express a percentage drawdown;
                # a margin-called / empty account is already blocked elsewhere.
                drawdown_pct = Decimal(0)
        breached = baseline > 0 and drawdown_pct > self._safety.max_daily_loss_pct

        entered = False
        if breached:
            entered = self._safe_mode.enter(
                "recoverable",
                REASON_DAILY_LOSS,
                detail=(
                    f"daily drawdown {drawdown_pct:.4f}% > {self._safety.max_daily_loss_pct}% "
                    f"(day_start_equity={baseline}, equity={account_equity})"
                ),
            )
        return DailyLossResult(
            baseline_equity=baseline,
            current_equity=account_equity,
            drawdown_pct=drawdown_pct,
            breached=breached,
            rolled=rolled,
            entered_safe_mode=entered,
        )

    # -- §10.4 consecutive loss cap ------------------------------------------

    def ensure_settlement_anchor(self, wallet_balance: Decimal, *, now: datetime) -> None:
        """Seed the §10.4 segment anchor if a run has none yet (idempotent).

        Called once at startup so the FIRST settlement of a fresh run measures
        its segment against genesis rather than falling into the anchor-only
        no-op below. A run that already has an anchor (a prior settlement, or a
        prior call) is left untouched, so a restart never rewrites a live anchor.
        """
        row = repo.get_scheduler_state(self._db.conn, self._run_id)
        if row is not None and row["last_settlement_wallet_balance"] is not None:
            return
        with self._db.transaction() as conn:
            repo.upsert_scheduler_state(
                conn,
                self._run_id,
                last_settlement_wallet_balance=wallet_balance,
                updated_at=now,
            )

    def record_settlement(self, *, wallet_balance: Decimal, now: datetime) -> SettlementResult:
        """§10.4: record one position settlement (the position just hit flat).

        The segment's net realized PnL is the wallet-balance delta since the
        previous settlement anchor — ``wallet_balance`` already folds realized
        PnL, fees and funding (AccountLedger), and it does not move while flat,
        so the delta over a flat → flat segment IS that segment's net PnL. A
        negative segment increments the consecutive-loss count; a non-negative
        one resets it to zero. The third consecutive loss escalates to MANUAL
        safe mode (§10.4 rule 3). With no prior anchor (a legacy store upgraded
        to v7 without ``ensure_settlement_anchor``), this call only establishes
        the anchor and leaves the count untouched — it cannot invent a segment
        PnL it has no baseline for.

        Known §10.4 measurement window (accepted, documented in the spec): the
        local ledger books a pending fee (absent / non-USDC at ingest) as 0
        until the backfill posts the correction, so a segment scored while its
        closing fill's fee is still pending reads fee-light — a near-zero
        segment can score as a gain (resetting the streak) that the later
        correction would have made a small loss. The count is never re-scored;
        the correction leaks into the NEXT segment's delta. Bounded by one
        fill's fee and only on the rare pending-fee lane.
        """
        row = repo.get_scheduler_state(self._db.conn, self._run_id)
        stored_anchor = row["last_settlement_wallet_balance"] if row is not None else None
        stored_count = row["consecutive_loss_count"] if row is not None else None
        prior_count = int(stored_count) if stored_count is not None else 0

        if stored_anchor is None:
            # No baseline to diff against: anchor here, touch nothing else.
            with self._db.transaction() as conn:
                repo.upsert_scheduler_state(
                    conn,
                    self._run_id,
                    last_settlement_wallet_balance=wallet_balance,
                    updated_at=now,
                )
            logger.info(
                "consecutive-loss anchor established at wallet_balance=%s (no prior "
                "segment to score)",
                wallet_balance,
            )
            return SettlementResult(
                segment_pnl=Decimal(0),
                is_loss=False,
                consecutive_loss_count=prior_count,
                entered_manual=False,
                anchored=True,
            )

        with localcontext(DECIMAL_CONTEXT):
            segment_pnl = wallet_balance - Decimal(stored_anchor)
        is_loss = segment_pnl < 0
        count = prior_count + 1 if is_loss else 0
        with self._db.transaction() as conn:
            repo.upsert_scheduler_state(
                conn,
                self._run_id,
                last_settlement_wallet_balance=wallet_balance,
                consecutive_loss_count=count,
                updated_at=now,
            )
        logger.info(
            "settlement recorded: segment_pnl=%s (%s), consecutive_loss_count=%d",
            segment_pnl,
            "loss" if is_loss else "gain/flat",
            count,
        )
        entered_manual = False
        if count >= self._safety.max_consecutive_loss_count:
            entered_manual = self._safe_mode.enter(
                "manual",
                REASON_CONSECUTIVE_LOSS,
                detail=(
                    f"{count} consecutive losing settlements "
                    f"(>= {self._safety.max_consecutive_loss_count}); "
                    f"last segment_pnl={segment_pnl}"
                ),
            )
        return SettlementResult(
            segment_pnl=segment_pnl,
            is_loss=is_loss,
            consecutive_loss_count=count,
            entered_manual=entered_manual,
            anchored=False,
        )
