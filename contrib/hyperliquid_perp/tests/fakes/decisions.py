"""A market context and a valid set-target decision for the decision seam."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from contrib.hyperliquid_perp.domains.perp.schema import PerpMarketContext
from contrib.hyperliquid_perp.domains.perp.target_decision import (
    DecisionMode,
    ParsedDecision,
    TargetDecision,
    TargetSide,
)

from .market import MARK


def market_ctx(as_of: datetime) -> PerpMarketContext:
    return PerpMarketContext(
        coin="BTC",
        as_of=as_of,
        candle_interval="4h",
        candle_count=200,
        mark_price=MARK,
        oracle_price=MARK,
        prev_day_price=MARK,
        mid_price=MARK,
        day_change_pct=0.0,  # prev == mark: a reference exists, so 0, not None
        open_interest=Decimal(0),
        day_ntl_volume=Decimal(0),
        funding_rate=Decimal("0.0001"),
        funding_premium=None,
        funding_zscore_30d=None,
        funding_window_days=30,
        funding_sample_count=0,
    )


def set_target(side: str, margin: int, conf: str = "0.8") -> ParsedDecision:
    dec = TargetDecision(
        decision_mode=DecisionMode.SET_TARGET,
        target_side=TargetSide(side),
        requested_target_margin_pct=margin,
        confidence=Decimal(conf),
        rationale="test rationale",
        key_risks=("a risk",),
    )
    return ParsedDecision(decision=dec, is_valid=True, invalid_reason=None, raw_response="{}")
