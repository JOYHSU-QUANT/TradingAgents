"""Typed view of the YAML ``paper_trading:`` block (phase2-execution §5.4).

Mirrors the ``risk:`` / ``decision:`` config pattern (``config_overrides`` +
per-field converters, absent/blank keys fall back to the field default declared
once on the dataclass). PR 2's accounting consumes ``taker_fee_rate`` and the
account block; ``min_notional_usdc`` / ``market_monitor`` / ``fill_model`` are
parsed here too so the whole §5.4 block round-trips as one typed unit for the
execution engine (PR 3).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from ..common.config_coercion import (
    config_overrides,
    decimal_from_yaml,
    int_from_yaml,
    str_from_yaml,
)
from ..common.constants import EXCHANGE_MIN_ORDER_NOTIONAL_USDC
from ..common.enum_guard import check_enum
from .twap import PLAN_LIFETIME_SECONDS, SLICE_INTERVAL_SECONDS

__all__ = [
    "FillModelConfig",
    "InitialPosition",
    "MarketMonitorConfig",
    "PaperAccountConfig",
    "PaperExecutionConfig",
    "PaperTradingConfig",
]


@dataclass(frozen=True)
class InitialPosition:
    """A seed position applied only when a new ``run_id`` is created (§5.4).

    ``size`` is signed (positive long, negative short); ``entry_price`` is the
    average entry the paper ledger opens with.
    """

    coin: str
    size: Decimal
    entry_price: Decimal

    def __post_init__(self) -> None:
        if not self.coin or not self.coin.strip():
            raise ValueError("initial position 'coin' must be a non-empty string")
        if self.size == 0:
            raise ValueError("initial position 'size' must be non-zero")
        if self.entry_price <= 0:
            raise ValueError(f"initial position 'entry_price' must be > 0, got {self.entry_price}")

    @classmethod
    def from_dict(cls, raw: object) -> InitialPosition:
        if not isinstance(raw, dict):
            raise ValueError(f"each initial position must be a mapping, got {raw!r}")
        allowed = {"coin", "size", "entry_price"}
        unknown = set(raw) - allowed
        if unknown:
            raise ValueError(
                f"unknown initial position key(s): {', '.join(map(repr, sorted(unknown)))}"
            )
        missing = allowed - set(raw)
        if missing:
            raise ValueError(
                f"initial position missing key(s): {', '.join(map(repr, sorted(missing)))}"
            )
        return cls(
            # str_from_yaml, not str(): the only downstream check is non-empty
            # (an open pattern), so YAML `coin: true` would otherwise seed a
            # position on the literal symbol "True".
            coin=str_from_yaml(raw["coin"]),
            size=decimal_from_yaml(raw["size"]),
            entry_price=decimal_from_yaml(raw["entry_price"]),
        )


def _parse_initial_positions(raw: object) -> tuple[InitialPosition, ...]:
    if not isinstance(raw, list):
        raise ValueError(f"initial_positions must be a list, got {raw!r}")
    positions = tuple(InitialPosition.from_dict(item) for item in raw)
    # One seed per coin: a duplicate would otherwise pass validation here and
    # only surface at run creation as an opaque run_seed_positions PK violation.
    seen: set[str] = set()
    for pos in positions:
        if pos.coin in seen:
            raise ValueError(f"duplicate initial position for coin {pos.coin!r}")
        seen.add(pos.coin)
    return positions


@dataclass(frozen=True)
class PaperAccountConfig:
    """``paper_trading.account`` — initial balance and seed positions (§5.4).

    Stricter than ``accounting.initialize_run`` on purpose: a non-positive
    opening balance in YAML is almost certainly an operator typo, so it is a
    config error here even though the accounting layer itself only warns (a
    stressed genesis is still expressible via adverse seed positions).
    """

    initial_balance_usdc: Decimal = Decimal("1000")
    initial_positions: tuple[InitialPosition, ...] = ()

    def __post_init__(self) -> None:
        if self.initial_balance_usdc <= 0:
            raise ValueError(f"initial_balance_usdc must be > 0, got {self.initial_balance_usdc}")

    @classmethod
    def from_dict(cls, cfg: dict | None) -> PaperAccountConfig:
        return cls(
            **config_overrides(
                cfg,
                {
                    "initial_balance_usdc": decimal_from_yaml,
                    "initial_positions": _parse_initial_positions,
                },
            )
        )


@dataclass(frozen=True)
class MarketMonitorConfig:
    """``paper_trading.execution.market_monitor`` — poll cadence and timeout (§5.4)."""

    interval_seconds: int = 30
    request_timeout_seconds: int = 5

    def __post_init__(self) -> None:
        if self.interval_seconds <= 0:
            raise ValueError(f"interval_seconds must be > 0, got {self.interval_seconds}")
        if self.interval_seconds > SLICE_INTERVAL_SECONDS:
            # The TWAP executor fills at most one slice per tick on a fixed 30s
            # slice grid with a hard 1h plan deadline — a monitor interval above
            # the slice interval cannot complete a full-size plan before the
            # deadline, silently under-filling the AI-approved target. Reject
            # loudly rather than tolerate broken execution.
            raise ValueError(
                f"interval_seconds must be <= {SLICE_INTERVAL_SECONDS} (the TWAP "
                f"slice cadence; larger values under-fill TWAP plans), got "
                f"{self.interval_seconds}"
            )
        if self.request_timeout_seconds <= 0:
            raise ValueError(
                f"request_timeout_seconds must be > 0, got {self.request_timeout_seconds}"
            )

    @classmethod
    def from_dict(cls, cfg: dict | None) -> MarketMonitorConfig:
        return cls(
            **config_overrides(
                cfg,
                {
                    "interval_seconds": int_from_yaml,
                    "request_timeout_seconds": int_from_yaml,
                },
            )
        )


# ``fill_model.style``: how a simulated slice reaches its fill (execution §5.2 /
# §5.2.1). ``taker`` is the Phase 2 model (mid ± slippage, taker fee, at once);
# ``maker`` posts at the touch and fills only when the mid trades THROUGH the
# posted price — the paper mirror of the live ``sliced_maker`` style.
PAPER_FILL_STYLES = frozenset({"taker", "maker"})


@dataclass(frozen=True)
class FillModelConfig:
    """``paper_trading.execution.fill_model`` — the simulated-fill model (§5.4).

    ``slippage_bps`` prices every taker fill (and the maker style's crossing
    fallback). The ``maker`` style (§5.2.1, 2026-09-16 maker path) adds: the
    maker fee, the half-spread assumed around the mid (the snapshot carries
    no book), how long a posted slice rests before it is re-posted at the new
    touch, and how many re-posts a slice gets before its remainder crosses as
    a taker. Conservative on purpose: a post fills only when the mid moves
    through it by a full tick, never on a touch.
    """

    slippage_bps: Decimal = Decimal("5")
    style: str = "taker"
    maker_fee_rate: Decimal = Decimal("0.00015")
    assumed_half_spread_bps: Decimal = Decimal("0.5")
    maker_rest_seconds: int = 30
    maker_max_requotes: int = 2

    def __post_init__(self) -> None:
        if self.slippage_bps < 0:
            raise ValueError(f"slippage_bps must be >= 0, got {self.slippage_bps}")
        check_enum(self.style, PAPER_FILL_STYLES, name="fill_model.style")
        if self.maker_fee_rate < 0:
            raise ValueError(f"maker_fee_rate must be >= 0, got {self.maker_fee_rate}")
        if self.assumed_half_spread_bps < 0:
            raise ValueError(
                f"assumed_half_spread_bps must be >= 0, got {self.assumed_half_spread_bps}"
            )
        if self.maker_rest_seconds <= 0:
            raise ValueError(f"maker_rest_seconds must be > 0, got {self.maker_rest_seconds}")
        if self.maker_max_requotes < 0:
            raise ValueError(f"maker_max_requotes must be >= 0, got {self.maker_max_requotes}")
        if self.style == "maker":
            # §5.2.1: every post rests its own clock, so a slice's longest stay
            # on the (simulated) book is (1 + requotes) * rest — it must fit the
            # plan lifetime (a sanity bound on one slice, not a completion
            # promise). The single-rest check is subsumed by the budget check and
            # kept only for its more specific message.
            if self.maker_rest_seconds > PLAN_LIFETIME_SECONDS:
                raise ValueError(
                    f"maker_rest_seconds must be <= {PLAN_LIFETIME_SECONDS} (the plan "
                    f"lifetime), got {self.maker_rest_seconds}"
                )
            budget = (1 + self.maker_max_requotes) * self.maker_rest_seconds
            if budget > PLAN_LIFETIME_SECONDS:
                raise ValueError(
                    "(1 + maker_max_requotes) * maker_rest_seconds must be <= "
                    f"{PLAN_LIFETIME_SECONDS} (the plan lifetime) so one slice's rest "
                    f"budget (first post + requotes) fits inside it, got {budget}"
                )

    @classmethod
    def from_dict(cls, cfg: dict | None) -> FillModelConfig:
        return cls(
            **config_overrides(
                cfg,
                {
                    "slippage_bps": decimal_from_yaml,
                    "style": str_from_yaml,
                    "maker_fee_rate": decimal_from_yaml,
                    "assumed_half_spread_bps": decimal_from_yaml,
                    "maker_rest_seconds": int_from_yaml,
                    "maker_max_requotes": int_from_yaml,
                },
            )
        )


@dataclass(frozen=True)
class PaperExecutionConfig:
    """``paper_trading.execution`` — fees, min notional, monitor, fill model (§5.4)."""

    taker_fee_rate: Decimal = Decimal("0.00045")
    # The exchange's own minimum order value (the same fact the live config gate
    # enforces), so the paper clip floor simulates the venue by default; the YAML
    # key still overrides it for a deliberately different simulation.
    min_notional_usdc: Decimal = EXCHANGE_MIN_ORDER_NOTIONAL_USDC
    market_monitor: MarketMonitorConfig = field(default_factory=MarketMonitorConfig)
    fill_model: FillModelConfig = field(default_factory=FillModelConfig)

    def __post_init__(self) -> None:
        if self.taker_fee_rate < 0:
            raise ValueError(f"taker_fee_rate must be >= 0, got {self.taker_fee_rate}")
        if self.min_notional_usdc <= 0:
            raise ValueError(f"min_notional_usdc must be > 0, got {self.min_notional_usdc}")

    @classmethod
    def from_dict(cls, cfg: dict | None) -> PaperExecutionConfig:
        return cls(
            **config_overrides(
                cfg,
                {
                    "taker_fee_rate": decimal_from_yaml,
                    "min_notional_usdc": decimal_from_yaml,
                    "market_monitor": MarketMonitorConfig.from_dict,
                    "fill_model": FillModelConfig.from_dict,
                },
            )
        )


@dataclass(frozen=True)
class PaperTradingConfig:
    """Typed ``paper_trading:`` block (account + execution)."""

    account: PaperAccountConfig = field(default_factory=PaperAccountConfig)
    execution: PaperExecutionConfig = field(default_factory=PaperExecutionConfig)

    @classmethod
    def from_dict(cls, cfg: dict | None) -> PaperTradingConfig:
        return cls(
            **config_overrides(
                cfg,
                {
                    "account": PaperAccountConfig.from_dict,
                    "execution": PaperExecutionConfig.from_dict,
                },
            )
        )
