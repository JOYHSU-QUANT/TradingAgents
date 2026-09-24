"""The per-asset metadata an engine needs, and the precision steps behind it.

:class:`AssetSpec` serves both lanes. Its two derived steps come from
``szDecimals`` and nothing else, so their definitions live beside it, and
so does :func:`build_asset_spec`, the one way a running lane reads it off
the venue.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Protocol

from ..domains.perp.margin import MarginSchedule

__all__ = [
    "AssetMetaSource",
    "AssetSpec",
    "build_asset_spec",
    "price_tick_from_sz_decimals",
    "qty_step_from_sz_decimals",
]

# Perp price precision on Hyperliquid: a price may carry up to (6 - szDecimals)
# decimal places, so the tick is 10 ** -(6 - szDecimals).
_PERP_PRICE_MAX_DECIMALS = 6


def qty_step_from_sz_decimals(sz_decimals: int) -> Decimal:
    """The quantity step ``10 ** -szDecimals`` for an asset (never hardcoded)."""
    if sz_decimals < 0:
        raise ValueError(f"szDecimals must be >= 0, got {sz_decimals}")
    return Decimal(10) ** -sz_decimals


def price_tick_from_sz_decimals(sz_decimals: int) -> Decimal:
    """The perp price tick implied by an asset's ``szDecimals`` (never hardcoded)."""
    if sz_decimals < 0:
        raise ValueError(f"szDecimals must be >= 0, got {sz_decimals}")
    return Decimal(10) ** -(_PERP_PRICE_MAX_DECIMALS - sz_decimals)


@dataclass(frozen=True)
class AssetSpec:
    """The per-asset metadata the engine needs, never hardcoded (execution §1.2 / §6.6.1).

    ``sz_decimals`` fixes the quantity step and price tick; ``margin_schedule`` is
    the exchange's maintenance-margin table (from the ``meta`` response). The two
    derived steps are computed once at construction.
    """

    coin: str
    sz_decimals: int
    margin_schedule: MarginSchedule
    qty_step: Decimal = field(init=False)
    tick_size: Decimal = field(init=False)

    def __post_init__(self) -> None:
        if not self.coin or not self.coin.strip():
            raise ValueError("AssetSpec.coin must be a non-empty string")
        if self.margin_schedule.coin != self.coin:
            raise ValueError(
                f"AssetSpec.margin_schedule is for {self.margin_schedule.coin!r}, not {self.coin!r}"
            )
        object.__setattr__(self, "qty_step", qty_step_from_sz_decimals(self.sz_decimals))
        object.__setattr__(self, "tick_size", price_tick_from_sz_decimals(self.sz_decimals))


class AssetMetaSource(Protocol):
    """The one venue read :func:`build_asset_spec` needs (the ``meta`` request)."""

    def get_asset_meta(self, coin: str) -> tuple[int, MarginSchedule]:
        """``(szDecimals, MarginSchedule)`` for ``coin``."""
        ...


def build_asset_spec(market: AssetMetaSource, coin: str) -> AssetSpec:
    """The :class:`AssetSpec` for ``coin`` as the venue reports it.

    One ``meta`` read, then the constructor; a venue failure propagates as the
    reader raises it (the callers' ``ExchangeError`` lanes), and a mismatched
    schedule as the constructor's ``ValueError``.
    """
    sz_decimals, schedule = market.get_asset_meta(coin)
    return AssetSpec(coin=coin, sz_decimals=sz_decimals, margin_schedule=schedule)
