"""Clean domain types for the perp module.

Every value that originates from Hyperliquid is held as :class:`~decimal.Decimal`
(prices, sizes, funding) so we never lose precision the way ``float`` would.
Derived analytics that are inherently floating point (technical indicators,
z-scores) stay as ``float`` and use ``None`` — never ``NaN`` — to mean
"not enough data".

No Hyperliquid raw field names appear here; they live only in
:mod:`...exchanges.hyperliquid.mapper`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from types import MappingProxyType


class MarketRegime(str, Enum):
    """Computed by ``context_builder``, carried through for the Reflection agent.

    Held as an enum (not a free string) so an unknown regime fails at context
    construction — where it is cheap to spot — rather than deep in an engine run
    where it would burn an LLM call before raising.
    """

    TRENDING = "trending"
    RANGING = "ranging"
    VOLATILE = "volatile"


class CandleInterval(str, Enum):
    """The supported candle intervals — the single source of truth for the set.

    Mirrors ``market_data._INTERVAL_MS`` (which keys its lookup by these members)
    and is what :class:`PerpMarketContext` validates its ``candle_interval``
    against, so adding an interval is a one-line change here. A ``str`` mix-in
    keeps ``"4h" == CandleInterval.H4`` for callers that pass a plain string.
    """

    M1 = "1m"
    M5 = "5m"
    M15 = "15m"
    H1 = "1h"
    H4 = "4h"
    D1 = "1d"


# --------------------------------------------------------------------------
# Market data DTOs — what the exchange layer returns across the port boundary.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Candle:
    """A single OHLCV candle. Times are UTC epoch milliseconds."""

    open_time: int
    close_time: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal

    def __post_init__(self) -> None:
        # A candle that violates OHLC ordering, time ordering, or sign would silently
        # poison every downstream indicator (ATR, EMA, regime) and therefore the
        # decision. Enforce the invariant at the type so a malformed candle can never
        # reach the math; ``mapper.map_candles`` catches this per-candle to drop a
        # single bad bar without aborting the whole run.
        if self.open_time >= self.close_time:
            raise ValueError(
                f"Candle open_time ({self.open_time}) must be < close_time ({self.close_time})"
            )
        if not (self.low <= self.open <= self.high and self.low <= self.close <= self.high):
            raise ValueError(
                f"Candle OHLC ordering violated: low={self.low} open={self.open} "
                f"high={self.high} close={self.close} (need low <= open,close <= high)"
            )
        if self.volume < 0:
            raise ValueError(f"Candle volume must be >= 0, got {self.volume}")


@dataclass(frozen=True)
class MarketSnapshot:
    """A point-in-time perp market snapshot for one coin.

    ``funding`` is the current hourly funding rate as a fraction
    (e.g. ``Decimal("0.0000125")`` == 0.00125% / hour).
    """

    coin: str
    mark_price: Decimal
    oracle_price: Decimal
    prev_day_price: Decimal
    open_interest: Decimal
    day_ntl_volume: Decimal
    funding: Decimal
    mid_price: Decimal | None = None
    premium: Decimal | None = None

    def __post_init__(self) -> None:
        # A price must be strictly positive: a zero/negative mark/oracle is a divisor
        # downstream (classify_regime's atr/price, _day_change_pct, indicator math) and
        # is serialized verbatim into the prompt, so a bad price would poison decision
        # sizing rather than fail loudly. Open interest and volume are magnitudes (>= 0).
        # ``funding`` and ``premium`` are signed
        # (a negative funding rate is normal), so they carry no sign guard. Reject the
        # malformed feed here rather than let a bad price poison decision sizing.
        # ``coin`` keys the position lookup (``AccountSnapshot.position_for``) and the
        # audit filename. An empty/whitespace coin would silently miss an open position
        # (read as flat) and degrade the audit path — reject it at construction.
        if not self.coin or not self.coin.strip():
            raise ValueError("MarketSnapshot.coin must be a non-empty string")
        for name in ("mark_price", "oracle_price"):
            value = getattr(self, name)
            if value <= 0:
                raise ValueError(f"MarketSnapshot.{name} must be > 0, got {value}")
        if self.mid_price is not None and self.mid_price <= 0:
            raise ValueError(f"MarketSnapshot.mid_price must be > 0, got {self.mid_price}")
        # ``prev_day_price`` is a price but guarded ``>= 0`` (not ``> 0`` like mark/oracle)
        # on purpose: Hyperliquid returns ``prevDayPx = "0"`` for a freshly-listed coin
        # with no 24h-ago reference, and rejecting that would refuse an otherwise-valid
        # snapshot. ``0`` here means "no reference price"; the only consumer
        # (``context_builder._day_change_pct``) already special-cases ``prev_day == 0`` and
        # emits ``day_change_pct = None``, so a zero never reaches a division. Open interest
        # and volume are genuine magnitudes (>= 0).
        for name in ("prev_day_price", "open_interest", "day_ntl_volume"):
            value = getattr(self, name)
            if value < 0:
                raise ValueError(f"MarketSnapshot.{name} must be >= 0, got {value}")


@dataclass(frozen=True)
class FundingPoint:
    """One historical funding observation. ``time`` is UTC epoch milliseconds."""

    time: int
    rate: Decimal
    premium: Decimal | None = None

    def __post_init__(self) -> None:
        # ``time`` is a UTC epoch-ms timestamp. A non-positive value is a structurally
        # corrupt record that ``funding_zscore``'s window filter (``cutoff <= p.time <
        # as_of_ms``) would silently drop, biasing the z-score sample with no warning.
        # Reject it at construction, matching the boundary guards on Candle/MarketSnapshot.
        if self.time <= 0:
            raise ValueError(f"FundingPoint.time must be > 0 (UTC epoch ms), got {self.time}")


@dataclass(frozen=True)
class PerpPosition:
    """An open perp position. ``size`` is signed: positive = long, negative = short.

    A flat account is represented by ``None`` (no position), not a zero-size
    instance.
    """

    coin: str
    size: Decimal
    entry_price: Decimal
    unrealized_pnl: Decimal
    leverage: Decimal | None = None
    liquidation_price: Decimal | None = None
    margin_used: Decimal | None = None
    position_value: Decimal | None = None

    def __post_init__(self) -> None:
        # A flat account is ``None``, never a zero-size instance (see class docstring).
        # ``size == 0`` is a third state ``is_long``/``is_short`` both call ``False`` —
        # the invariant the type promises but can't express. ``_map_position`` already
        # returns ``None`` for flat; enforce it here so no other path can sneak one in.
        if not self.coin or not self.coin.strip():
            raise ValueError("PerpPosition.coin must be a non-empty string")
        if self.size == 0:
            raise ValueError("PerpPosition.size must be non-zero; use None for a flat account")
        # ``entry_price`` is a strictly positive reference the audit log / prompt
        # serialize verbatim; a zero/negative entry is a structurally corrupt position
        # record (mirrors the ``mark_price > 0`` guard on MarketSnapshot). Reject it at
        # construction rather than serialize a nonsensical entry that looks valid.
        if self.entry_price <= 0:
            raise ValueError(f"PerpPosition.entry_price must be > 0, got {self.entry_price}")
        # Optional margin/valuation fields the audit log and ``current_position_state``
        # margin math consume. The mapper's ``_opt_dec`` already drops absent/garbage
        # values to ``None`` ("not applicable" arrives as null), so a value that
        # survives to here must be well-formed: a non-positive leverage/liquidation
        # price or a negative margin/value is a structurally corrupt record, not an
        # absent field. Reject it at construction (mirrors ``entry_price > 0`` and the
        # MarketSnapshot/AccountSnapshot magnitude guards) rather than divide by it.
        if self.leverage is not None and self.leverage <= 0:
            raise ValueError(f"PerpPosition.leverage must be > 0, got {self.leverage}")
        if self.liquidation_price is not None and self.liquidation_price <= 0:
            raise ValueError(
                f"PerpPosition.liquidation_price must be > 0, got {self.liquidation_price}"
            )
        if self.margin_used is not None and self.margin_used < 0:
            raise ValueError(f"PerpPosition.margin_used must be >= 0, got {self.margin_used}")
        if self.position_value is not None and self.position_value < 0:
            raise ValueError(f"PerpPosition.position_value must be >= 0, got {self.position_value}")

    @property
    def is_long(self) -> bool:
        return self.size > 0

    @property
    def is_short(self) -> bool:
        return self.size < 0


@dataclass(frozen=True)
class AccountSnapshot:
    """Account-level margin state plus the open positions.

    ``cross_maintenance_margin_used`` and ``total_position_notional`` are the
    exchange's own account-level figures (``crossMaintenanceMarginUsed`` /
    ``marginSummary.totalNtlPos``), carried for the Phase 3 live snapshot
    writer — the reconciler records what the exchange reported rather than
    re-deriving a maintenance model. Optional: Phase 1/2 consumers never read
    them and older payload fixtures may omit the fields.
    """

    account_value: Decimal
    withdrawable: Decimal
    total_margin_used: Decimal
    positions: tuple[PerpPosition, ...] = ()
    cross_maintenance_margin_used: Decimal | None = None
    total_position_notional: Decimal | None = None

    def __post_init__(self) -> None:
        # ``account_value`` must be strictly positive: a zero/negative value makes
        # current_exposure_pct early-return 0% (its ``account_value <= 0`` guard),
        # silently masking a margin-called/liquidated account as flat — at which point
        # the rebalancer would believe there is no exposure and try to OPEN new
        # positions on an account with no margin. Reject it here at construction so the
        # corrupt state surfaces instead of being read as "nothing to do".
        if self.account_value <= 0:
            raise ValueError(f"AccountSnapshot.account_value must be > 0, got {self.account_value}")
        # The remaining money fields are magnitudes (>= 0).
        for name in ("withdrawable", "total_margin_used"):
            value = getattr(self, name)
            if value < 0:
                raise ValueError(f"AccountSnapshot.{name} must be >= 0, got {value}")
        for name in ("cross_maintenance_margin_used", "total_position_notional"):
            value = getattr(self, name)
            if value is not None and value < 0:
                raise ValueError(f"AccountSnapshot.{name} must be >= 0, got {value}")
        # ``position_for`` returns the first match, so a duplicate coin would silently
        # drop the second position — and the exchange should never report two open
        # positions for one coin. Reject it at construction so the ambiguity surfaces
        # here rather than as a wrong exposure deep in the rebalancer.
        coins = [pos.coin for pos in self.positions]
        if len(coins) != len(set(coins)):
            dupes = sorted({c for c in coins if coins.count(c) > 1})
            raise ValueError(f"AccountSnapshot has duplicate coin(s) in positions: {dupes}")

    def position_for(self, coin: str) -> PerpPosition | None:
        for pos in self.positions:
            if pos.coin == coin:
                return pos
        return None


# --------------------------------------------------------------------------
# Computed context — what context_builder produces for the engine / prompt.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class PerpMarketContext:
    """The market context the engine reasons over, built by context_builder.

    ``indicators`` maps the configured indicator name (e.g. ``"rsi_14"``) to its
    latest value, or ``None`` when there were not enough candles to compute it.
    ``funding_zscore_30d`` is ``None`` when the funding window has too few
    samples or zero variance (never ``NaN``).
    """

    coin: str
    as_of: datetime  # UTC; derived from the latest candle close time
    candle_interval: str
    candle_count: int

    mark_price: Decimal
    oracle_price: Decimal
    prev_day_price: Decimal
    mid_price: Decimal | None
    day_change_pct: float | None

    open_interest: Decimal
    day_ntl_volume: Decimal

    funding_rate: Decimal
    funding_premium: Decimal | None
    funding_zscore_30d: float | None
    funding_window_days: int
    funding_sample_count: int

    indicators: Mapping[str, float | None] = field(default_factory=dict)
    market_regime: MarketRegime = MarketRegime.RANGING

    def __post_init__(self) -> None:
        # Mirror the boundary invariants the source ``MarketSnapshot`` already enforces,
        # so a ``PerpMarketContext`` built by any path (tests, fixtures, Phase 2+) can't
        # carry a state the engine/audit layer would silently misread. ``context_builder``
        # always produces valid values; this just makes the frozen type self-guarding.
        if not self.coin or not self.coin.strip():
            raise ValueError("PerpMarketContext.coin must be a non-empty string")
        for name in ("mark_price", "oracle_price"):
            value = getattr(self, name)
            if value <= 0:
                raise ValueError(f"PerpMarketContext.{name} must be > 0, got {value}")
        if self.candle_count < 0:
            raise ValueError(
                f"PerpMarketContext.candle_count must be >= 0, got {self.candle_count}"
            )
        # ``funding_window_days`` is the look-back width for the funding z-score; a value
        # < 1 makes the window filter (``cutoff <= p.time < as_of_ms``) keep nothing and
        # silently degrade the z-score to ``None`` — indistinguishable from a genuine
        # data shortage. Reject a misconfigured window here rather than mask it downstream.
        if self.funding_window_days < 1:
            raise ValueError(
                f"PerpMarketContext.funding_window_days must be >= 1, got {self.funding_window_days}"
            )
        # ``funding_sample_count`` is a count of surviving funding points; negative is
        # structurally impossible (it comes from ``len(...)``), so guard it like candle_count.
        if self.funding_sample_count < 0:
            raise ValueError(
                f"PerpMarketContext.funding_sample_count must be >= 0, "
                f"got {self.funding_sample_count}"
            )
        # A naive ``as_of`` would serialize to an ISO string with no offset that looks
        # UTC on a UTC host but is wrong elsewhere (the audit log's _record_header
        # rejects naive timestamps for the same reason); require tz-awareness at
        # construction.
        if self.as_of.tzinfo is None:
            raise ValueError("PerpMarketContext.as_of must be timezone-aware (UTC)")
        # Validate ``candle_interval`` against the single source of truth
        # (:class:`CandleInterval`) so an unsupported value fails here at
        # construction — cheap to spot — rather than later inside ``interval_to_ms``.
        try:
            interval = CandleInterval(self.candle_interval)
        except ValueError:
            valid = [i.value for i in CandleInterval]
            raise ValueError(
                f"unsupported candle_interval {self.candle_interval!r}; choose from {valid}"
            ) from None
        # Store the plain ``.value`` string ("4h"), never the enum member: a caller
        # passing ``CandleInterval.H4`` itself would otherwise be stored as a
        # ``(str, Enum)`` member that renders as ``"CandleInterval.H4"`` (not ``"4h"``)
        # through an f-string under 3.12, corrupting the rendered prompt. Coercing to
        # ``.value`` makes the stored form render-safe whether a ``str`` or an enum
        # member was passed in.
        object.__setattr__(self, "candle_interval", interval.value)
        # ``frozen=True`` blocks reassignment but not in-place mutation of a plain
        # dict, so wrap ``indicators`` in a read-only proxy over a private copy —
        # the immutability the frozen flag advertises now actually holds.
        object.__setattr__(self, "indicators", MappingProxyType(dict(self.indicators)))
        # Coerce ``market_regime`` to the enum so a plain string (e.g. from a caller
        # or a recorded fixture) is accepted, while an unknown value raises a
        # ValueError here at construction rather than at decision time.
        object.__setattr__(self, "market_regime", MarketRegime(self.market_regime))
