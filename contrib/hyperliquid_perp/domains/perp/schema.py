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

from ...common.constants import (
    HOLDING_COST_HOURS,
    MIN_VOLUME_PROFILE_WINDOW,
    POC_LOWER_BAND,
    POC_UPPER_BAND,
    RANGE_MIDPOINT,
    THIN_VALUE_AREA_RATIO,
    VALUE_AREA_FRACTION,
    VOLUME_PROFILE_BUCKET_COUNT,
)


class MarketRegime(str, Enum):
    """Computed by ``context_builder``, carried through for the Reflection agent.

    Held as an enum (not a free string) so an unknown regime fails at context
    construction — where it is cheap to spot — rather than deep in an engine run
    where it would burn an LLM call before raising.
    """

    TRENDING = "trending"
    RANGING = "ranging"
    VOLATILE = "volatile"


class ProfileShape(str, Enum):
    """The volume-profile shape :func:`..perp.volume_profile.classify_shape` assigns.

    Held as an enum for the same reason as :class:`MarketRegime`: an unknown
    shape fails at construction rather than deep in a render. The values are
    the source article's letters (``"D"``/``"P"``/``"b"``), so ``.value`` is
    what the prompt must print — never the member itself, which an f-string
    renders as ``"ProfileShape.P"``.
    """

    D = "D"
    P = "P"
    B = "b"
    THIN = "thin"


class PositionSide(str, Enum):
    """Which way an OPEN position points, as the prompt's position section says it.

    A flat account is ``None`` on :class:`PositionContext`, never a third
    member: the section renders the two sides and the flat case differently,
    and an enum member for "flat" would let a hand-built context carry a side
    with a zero size.

    Its two values duplicate ``target_decision.TargetSide``'s LONG/SHORT, and
    that is a known cost rather than an oversight: importing ``TargetSide``
    here would put ``target_decision`` inside ``config.py``'s load-time import
    closure, which ``tests/common/test_layering.py`` pins to a short
    allowlist (this module is on it; that one is not). The two enums are
    never compared to each other — the gate's ``CurrentPositionState`` and
    this DTO are built separately from the same signed size — so ``==`` /
    ``is`` between them is not written anywhere; if a bridge is ever needed,
    convert by ``.value``.
    """

    LONG = "long"
    SHORT = "short"


class CandleInterval(str, Enum):
    """The supported candle intervals — the single source of truth for the set.

    :data:`_INTERVAL_MS` below keys its lookup by these members, and
    :class:`PerpMarketContext` validates its ``candle_interval`` against them,
    so adding an interval is a two-line change here. A ``str`` mix-in keeps
    ``"4h" == CandleInterval.H4`` for callers that pass a plain string.
    """

    M1 = "1m"
    M5 = "5m"
    M15 = "15m"
    H1 = "1h"
    H4 = "4h"
    D1 = "1d"


# How many milliseconds each supported candle interval spans. Lives beside the
# enum it is keyed by, not in the exchange adapter that first needed it: the
# freshness guard (:mod:`.freshness`) measures candle age in intervals and must
# stay importable on the keyless ``--context-only`` path, which the adapter's
# SDK import is not.
_INTERVAL_MS = {
    CandleInterval.M1: 60_000,
    CandleInterval.M5: 5 * 60_000,
    CandleInterval.M15: 15 * 60_000,
    CandleInterval.H1: 60 * 60_000,
    CandleInterval.H4: 4 * 60 * 60_000,
    CandleInterval.D1: 24 * 60 * 60_000,
}


def parse_interval(interval: str | CandleInterval) -> CandleInterval:
    """``interval`` as its :class:`CandleInterval` member; ``ValueError`` naming it if unsupported.

    The one place the string is resolved: :func:`interval_to_ms` and
    :class:`PerpMarketContext`'s constructor both go through it, so the check
    and its message live once (issue #122). A member passes through unchanged.
    """
    # ``CandleInterval(interval)`` raises ValueError on an unknown value (e.g. a
    # mis-cased "4H"); translate it into the same clear message the caller expects.
    try:
        return CandleInterval(interval)
    except ValueError:
        raise ValueError(
            f"unsupported candle interval {interval!r}; "
            f"choose from {[i.value for i in CandleInterval]}"
        ) from None


def interval_to_ms(interval: str) -> int:
    """``interval`` as milliseconds; ``ValueError`` naming the value if unsupported."""
    return _INTERVAL_MS[parse_interval(interval)]


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
        # Strictly positive, like MarketSnapshot's prices: a zero/negative close is
        # classify_regime's silent price <= 0 -> RANGING branch, and a zero bar
        # anywhere poisons the EMA/ATR series feeding it. The ordering check above
        # makes ``low`` the minimum, so one comparison covers all four prices.
        if self.low <= 0:
            raise ValueError(
                f"Candle prices must be > 0, got low={self.low} (open={self.open} "
                f"high={self.high} close={self.close})"
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
        # downstream (classify_regime's atr/price, derive_day_change_pct, indicator math) and
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
        # (:func:`derive_day_change_pct`) already special-cases ``prev_day == 0`` and
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


# The one tolerance every "stored value vs. what it derives from" check in this
# module uses — the fractions and volume shares on ``VolumeProfile`` and
# ``day_change_pct`` on ``PerpMarketContext``. Tolerance rather than equality,
# and ONE tolerance rather than one per field: what it admits is a DTO rebuilt
# from a value someone rounded on the way in, and a fixture or recorded row
# rounds every float the same way, so the class's admission policy must not
# differ field by field. 1e-6 admits anything recorded to six decimal places
# or better and is orders of magnitude below any disagreement worth calling a
# contradiction. The float-vs-Decimal gap it also covers is real but tiny —
# over 4000 fuzzed producer outputs spanning six price magnitudes the range
# fractions came out EXACTLY equal every time — except where a value itself is
# huge, which is why :func:`_check_derived` applies it RELATIVE to the expected
# value (for a fraction in ``[0, 1]`` that is the plain 1e-6).
_DERIVED_TOLERANCE = 1e-6


def _check_derived(label: str, claimed: float, expected: float, source: str) -> None:
    """Raise unless ``claimed`` agrees with ``expected`` to the shared tolerance.

    A DTO that stores BOTH a value and the values it was computed from can be
    handed the two disagreeing, and every bounds check still passes — the
    contradiction only shows up in whatever renders them side by side. This is
    the one shared statement of that invariant; ``source`` names, for the
    message, what ``expected`` was derived from.
    """
    if abs(claimed - expected) > _DERIVED_TOLERANCE * max(1.0, abs(expected)):
        raise ValueError(f"{label} ({claimed}) contradicts {source} — that gives {expected}")


def _check_derived_fraction(label: str, claimed: float, numerator: Decimal, span: Decimal) -> None:
    """``claimed`` must be ``numerator / span`` — a fraction of a price range."""
    _check_derived(label, claimed, float(numerator / span), "the values it is derived from")


def derive_day_change_pct(mark: Decimal, prev_day: Decimal) -> float | None:
    """The 24h change, in percent, or ``None`` when there is no reference.

    THE rule for ``PerpMarketContext.day_change_pct``: ``context_builder``
    calls it to fill the field, and the DTO calls it again at construction to
    check what it was handed — one definition, as :func:`derive_profile_shape`
    is for the profile's letter. ``prev_day == 0`` is Hyperliquid's "no 24h
    reference yet" for a freshly listed coin (``MarketSnapshot`` admits it on
    purpose), and is the ONLY input that yields ``None``.
    """
    if prev_day == 0:
        return None
    return float((mark - prev_day) / prev_day * 100)


def derive_profile_shape(
    value_area_width_ratio: float, poc_position: float, close_position: float
) -> ProfileShape:
    """The volume profile's letter, from the three fractions that decide it.

    This is THE shape rule: :func:`.volume_profile.classify_shape` calls it to
    label a freshly built profile, and :class:`VolumeProfile` calls it again
    at construction to check the label it was handed — one definition, so the
    producer and the DTO cannot disagree about what a ``P`` is. It lives here
    rather than in ``volume_profile`` because that module imports this one.

    Rules, checked in this order (thresholds in ``common.constants``):

    1. ``thin`` — the value area spans at least ``THIN_VALUE_AREA_RATIO`` of
       the range. First, because a smeared profile's POC tells you nothing.
    2. ``P`` — POC at or above ``POC_UPPER_BAND`` of the range AND the latest
       close above the window midpoint.
    3. ``b`` — the mirror: POC at or below ``POC_LOWER_BAND`` AND the close
       below the midpoint.
    4. ``D`` — everything else, which includes a skewed POC whose close did
       not confirm it (``classify_shape``'s docstring says what that means).
    """
    if value_area_width_ratio >= THIN_VALUE_AREA_RATIO:
        return ProfileShape.THIN
    if poc_position >= POC_UPPER_BAND and close_position > RANGE_MIDPOINT:
        return ProfileShape.P
    if poc_position <= POC_LOWER_BAND and close_position < RANGE_MIDPOINT:
        return ProfileShape.B
    return ProfileShape.D


@dataclass(frozen=True)
class VolumeProfile:
    """Where traded volume sat in the price range over a rolling candle window.

    Built by :mod:`.volume_profile` (see that module for the algorithm and for
    every threshold behind :attr:`shape`). Carried on
    :class:`PerpMarketContext` as an OPTIONAL analyst input: ``None`` means the
    section is omitted from the prompt entirely — there is no half-populated
    form, and no ``NaN`` ever reaches the renderer.

    Prices are :class:`~decimal.Decimal` like every other price in this module.
    The three ``*_position`` / ``*_ratio`` fields are derived fractions of the
    window's own price RANGE, in ``[0, 1]``, and are ``float`` because they are
    ratios the prompt prints as percentages — never money.

    The two ``*_volume_share`` fields are fractions of the window's VOLUME, not
    of its price range: how much of the traded volume the POC bucket alone held,
    and how much the whole value area held. They say how DOMINANT the levels
    are, which the geometry only sometimes implies — the rendered block usually
    differs between a heavy POC and a marginal one (the value-area walk stops
    on different buckets), but not always: when the walk stops on the SAME
    buckets, two profiles whose POC held 79% and 99.9% of the window render
    identically. Pinned by
    ``test_two_profiles_can_render_identically_yet_hold_very_different_volume``. ``poc_volume_share`` also has a floor the geometry hides —
    the heaviest bucket is at least the average, so it can never be below
    ``1 / bucket_count``.

    Carried but deliberately NOT rendered: the prompt block's wording is fixed
    by the rulings on this PR, and these exist so the frozen DTO already carries
    them when a gate or sizing consumer needs to ask "is this level real?".

    ``candle_count`` is the number of candles ACTUALLY folded in (which equals
    the configured window — :func:`.volume_profile.build_profile` refuses a
    short window rather than quietly narrowing it), and ``bucket_count`` is the
    price-bucket resolution the POC / value-area edges are quantized to. Both
    are carried so the rendered text can state the basis instead of implying a
    precision the coarse candle approximation does not have.
    """

    shape: ProfileShape
    poc: Decimal
    value_area_low: Decimal
    value_area_high: Decimal
    range_low: Decimal
    range_high: Decimal
    poc_position: float
    close_position: float
    value_area_width_ratio: float
    poc_volume_share: float
    value_area_volume_share: float
    candle_count: int
    bucket_count: int

    def __post_init__(self) -> None:
        # Self-guarding like the other frozen DTOs here: the producer always
        # emits consistent values, and these checks make any OTHER path (a
        # fixture, a future caller) unable to hand the renderer a profile whose
        # bounds contradict each other — which would print as a confident,
        # nonsensical support/resistance level in the prompt.
        #
        # Field by field, because a summary sentence about this has been wrong
        # twice: the prices below get mutual containment and ordering;
        # ``poc_position`` and ``value_area_width_ratio`` get cross-checked
        # against those prices; ``shape`` is re-derived from the three
        # fractions that decide it; the two counts are pinned to the producer's
        # floor and grid; the two volume shares get the floors the walk
        # guarantees. All of it is below, in that order.
        #
        # ``close_position`` is the one nothing here can check beyond bounds:
        # it is a fraction of a ``latest_close`` this class does not store, and
        # adding the field to store it serves no consumer.
        if self.range_low <= 0:
            raise ValueError(f"VolumeProfile.range_low must be > 0, got {self.range_low}")
        if self.range_high <= self.range_low:
            raise ValueError(
                f"VolumeProfile.range_high ({self.range_high}) must be > range_low "
                f"({self.range_low}) — a zero-width window has no profile"
            )
        if self.value_area_high <= self.value_area_low:
            raise ValueError(
                f"VolumeProfile.value_area_high ({self.value_area_high}) must be > "
                f"value_area_low ({self.value_area_low})"
            )
        if self.value_area_low < self.range_low or self.value_area_high > self.range_high:
            raise ValueError(
                f"VolumeProfile value area [{self.value_area_low}, {self.value_area_high}] "
                f"must sit inside the range [{self.range_low}, {self.range_high}]"
            )
        # The value area is grown OUTWARD from the POC bucket, so a POC outside
        # it means the walk and the POC disagree about which bucket won.
        if not (self.value_area_low <= self.poc <= self.value_area_high):
            raise ValueError(
                f"VolumeProfile.poc ({self.poc}) must sit inside the value area "
                f"[{self.value_area_low}, {self.value_area_high}]"
            )
        for name in ("poc_position", "close_position"):
            value = getattr(self, name)
            if not 0.0 <= value <= 1.0:
                raise ValueError(
                    f"VolumeProfile.{name} is a fraction of the window range and must be "
                    f"in [0, 1], got {value}"
                )
        if not 0.0 < self.value_area_width_ratio <= 1.0:
            raise ValueError(
                f"VolumeProfile.value_area_width_ratio must be in (0, 1], "
                f"got {self.value_area_width_ratio}"
            )
        # The producer refuses a window below the floor rather than narrowing
        # it, and always buckets on the one grid — so a count outside either
        # did not come from a walk. Pinned to the same constants the producer
        # reads (``common.constants``), not to literals that could drift.
        if self.candle_count < MIN_VOLUME_PROFILE_WINDOW:
            raise ValueError(
                f"VolumeProfile.candle_count must be >= {MIN_VOLUME_PROFILE_WINDOW} "
                f"(the producer's window floor), got {self.candle_count}"
            )
        if self.bucket_count != VOLUME_PROFILE_BUCKET_COUNT:
            raise ValueError(
                f"VolumeProfile.bucket_count must be {VOLUME_PROFILE_BUCKET_COUNT} "
                f"(the producer's grid), got {self.bucket_count}"
            )
        for name in ("poc_volume_share", "value_area_volume_share"):
            value = getattr(self, name)
            if not 0.0 < value <= 1.0:
                raise ValueError(
                    f"VolumeProfile.{name} is a share of the window's traded volume and "
                    f"must be in (0, 1], got {value}"
                )
        # The value area is grown outward FROM the POC bucket, so the POC bucket
        # is always one of the buckets the area holds — its share cannot exceed
        # the area's. A profile claiming otherwise came from a walk that lost
        # track of which buckets it had taken.
        if self.poc_volume_share > self.value_area_volume_share:
            raise ValueError(
                f"VolumeProfile.poc_volume_share ({self.poc_volume_share}) cannot exceed "
                f"value_area_volume_share ({self.value_area_volume_share}) — the POC "
                f"bucket sits inside the value area"
            )
        # Floors the walk guarantees, both with the module's one tolerance: the
        # POC is the heaviest of ``bucket_count`` buckets, so its share is at
        # least the average ``1 / bucket_count`` (a perfectly uniform window
        # sits exactly on it — issue #100's fuzz of the producer found the
        # minimum at 1.000000 x the floor); and the value-area walk exits only
        # once it holds at least VALUE_AREA_FRACTION of the volume (or every
        # bucket). A share below either says the walk lost track of a bucket.
        uniform_share = 1 / self.bucket_count
        if self.poc_volume_share < uniform_share - _DERIVED_TOLERANCE:
            raise ValueError(
                f"VolumeProfile.poc_volume_share ({self.poc_volume_share}) is below "
                f"1 / bucket_count ({uniform_share}) — the heaviest bucket cannot hold "
                f"less than the average"
            )
        value_area_floor = float(VALUE_AREA_FRACTION)
        if self.value_area_volume_share < value_area_floor - _DERIVED_TOLERANCE:
            raise ValueError(
                f"VolumeProfile.value_area_volume_share ({self.value_area_volume_share}) "
                f"is below VALUE_AREA_FRACTION ({value_area_floor}) — the walk never "
                f"stops before reaching it"
            )
        # Cross-check the derived fractions against the prices they came FROM.
        # Without this, every guard above constrains the two halves SEPARATELY:
        # a profile could satisfy all of them while claiming a ``poc_position``
        # of 0.01 for a ``poc`` sitting mid-range, and the renderer would print
        # "POC: 105.00 (1% up the range)" — one line contradicting itself. The
        # producer derives both from the same bucket index, so this can only
        # fire on a hand-built profile.
        span = self.range_high - self.range_low
        _check_derived_fraction(
            "VolumeProfile.poc_position", self.poc_position, self.poc - self.range_low, span
        )
        _check_derived_fraction(
            "VolumeProfile.value_area_width_ratio",
            self.value_area_width_ratio,
            self.value_area_high - self.value_area_low,
            span,
        )
        # Coerce like ``market_regime``: a plain string (fixture, recorded row)
        # is accepted, an unknown one raises here rather than at render time.
        shape = ProfileShape(self.shape)
        # Then re-derive it. The letter is fully determined by three fractions
        # stored right here, with the producer's own rule — so a profile that
        # calls itself ``P`` while its POC sits at 6% of the range (a real
        # hand-built case, and it rendered as one self-contradicting block)
        # cannot exist. Same tolerance question as the cross-checks above: the
        # thresholds are compared to the stored floats exactly, as the
        # producer compares them, so a fraction ON a threshold classifies the
        # same way on both sides.
        derived = derive_profile_shape(
            self.value_area_width_ratio, self.poc_position, self.close_position
        )
        if shape is not derived:
            raise ValueError(
                f"VolumeProfile.shape ({shape.value}) contradicts the fractions it is "
                f"derived from — value_area_width_ratio={self.value_area_width_ratio}, "
                f"poc_position={self.poc_position}, close_position={self.close_position} "
                f"give {derived.value}"
            )
        object.__setattr__(self, "shape", shape)


def derive_round_trip_rate(taker_fee_rate: Decimal, slippage_bps: Decimal) -> Decimal:
    """The cost of a round trip as a FRACTION of the notional traded.

    THE rule for :class:`MarginalCostRow`: :mod:`.marginal_cost` prices every
    row with it and the DTO re-derives each row against it at construction —
    one definition, as :func:`derive_day_change_pct` is for the 24h change.

    Two legs, each paying the taker fee AND the slippage, so
    ``2 * (taker_fee_rate + slippage_bps / 10_000)``. Slippage is counted on
    both legs because that is what the paper fill model charges
    (``paper.fill_model.fill_price`` moves every fill adversely by
    ``slippage_bps``, the opening one and the reversing one alike); a formula
    that counted it once would advertise a breakeven the books never see.
    Under the defaults (fee 0.045%, slippage 5 bps) that is 19 bps.
    """
    return 2 * (taker_fee_rate + slippage_bps / Decimal(10_000))


@dataclass(frozen=True)
class MarginalCostRow:
    """What moving the committed margin to ONE legal target would cost.

    ``trade_notional`` is the notional that changes hands to get there
    (``|target - current| / 100 * equity * leverage``), ``round_trip_cost``
    is that notional priced at :func:`derive_round_trip_rate` — the fee and
    slippage on this move PLUS the same again when it is later reversed, which
    is the shape of the churn the section exists to make visible. The
    breakeven the prompt prints beside each row (the favourable move, in
    basis points of the traded notional, that exactly pays for the round
    trip) is ``round_trip_cost / trade_notional`` — i.e. the rate itself,
    identical on every row — so it is not stored here; the renderer prints
    the rate.
    """

    target_margin_pct: int
    trade_notional: Decimal
    round_trip_cost: Decimal

    def __post_init__(self) -> None:
        # A row at the current margin trades nothing and has no breakeven —
        # the producer skips it; a hand-built one is a contradiction, not a
        # degenerate row.
        if self.trade_notional <= 0:
            raise ValueError(
                f"MarginalCostRow.trade_notional must be > 0, got {self.trade_notional} "
                f"(a target at the current margin has no row)"
            )
        if self.round_trip_cost < 0:
            raise ValueError(
                f"MarginalCostRow.round_trip_cost must be >= 0, got {self.round_trip_cost}"
            )
        # A percent of equity, like every grid bound upstream (DecisionConfig
        # holds 0 <= min < max <= 100): a row naming -40% or 500% is a legal
        # arithmetic row and an impossible target.
        if not 0 <= self.target_margin_pct <= 100:
            raise ValueError(
                f"MarginalCostRow.target_margin_pct must be in [0, 100], "
                f"got {self.target_margin_pct}"
            )


@dataclass(frozen=True)
class PositionContext:
    """The account's own position, as the prompt's position section shows it.

    Built by :mod:`.marginal_cost` from the local books (paper ledger / live
    store) and carried on :class:`PerpMarketContext` as an OPTIONAL section:
    ``None`` means the prompt omits it entirely — a context built without a
    position source (the one-shot CLI, fixtures) or one whose books were
    unusable (no ledger yet, non-positive equity). Same discipline as
    :class:`VolumeProfile`: no half-populated form.

    A FLAT account is ``side is None`` with every position-only field empty
    and no cost rows (the section then says so in one line). An OPEN one
    carries the facts — signed ``size``, ``entry_price``, ``unrealized_pnl`` at
    the context's mark, ``notional``, committed ``margin_pct`` of ``equity`` at
    the configured ``leverage`` — plus ``holding_cost_8h`` (funding on the
    notional per 8h, signed: positive means the position PAYS) and one
    :class:`MarginalCostRow` per displayed legal target.

    Facts only. Nothing here says which gate threshold a given target would
    face — that ranking stays out of the prompt (see
    ``target_decision.decision_format_instructions``).
    """

    side: PositionSide | None
    size: Decimal
    entry_price: Decimal | None
    unrealized_pnl: Decimal | None
    notional: Decimal
    margin_pct: Decimal | None
    equity: Decimal
    leverage: Decimal
    last_fill_at: datetime | None
    holding_cost_8h: Decimal | None
    taker_fee_rate: Decimal
    slippage_bps: Decimal
    cost_rows: tuple[MarginalCostRow, ...] = ()

    def __post_init__(self) -> None:
        if self.equity <= 0:
            raise ValueError(f"PositionContext.equity must be > 0, got {self.equity}")
        if self.leverage <= 0:
            raise ValueError(f"PositionContext.leverage must be > 0, got {self.leverage}")
        if self.taker_fee_rate < 0 or self.slippage_bps < 0:
            raise ValueError("PositionContext fee/slippage must be >= 0")
        if self.last_fill_at is not None and self.last_fill_at.tzinfo is None:
            raise ValueError("PositionContext.last_fill_at must be timezone-aware (UTC)")
        if self.side is None:
            # Flat: one shape, nothing position-only may be set.
            if self.size != 0:
                raise ValueError("a flat PositionContext (side None) must have size 0")
            if (
                self.entry_price is not None
                or self.unrealized_pnl is not None
                or self.margin_pct is not None
                or self.holding_cost_8h is not None
                or self.notional != 0
                or self.cost_rows
            ):
                raise ValueError(
                    "a flat PositionContext carries no entry, PnL, notional, margin, "
                    "holding cost or cost rows"
                )
            return
        side = PositionSide(self.side)
        object.__setattr__(self, "side", side)
        if side is PositionSide.LONG and self.size <= 0:
            raise ValueError("a long PositionContext must have size > 0")
        if side is PositionSide.SHORT and self.size >= 0:
            raise ValueError("a short PositionContext must have size < 0")
        if self.entry_price is None or self.entry_price <= 0:
            raise ValueError("an open PositionContext must carry entry_price > 0")
        if self.unrealized_pnl is None or self.margin_pct is None or self.holding_cost_8h is None:
            raise ValueError(
                "an open PositionContext must carry unrealized_pnl, margin_pct and holding_cost_8h"
            )
        if self.notional <= 0:
            raise ValueError(f"an open PositionContext must have notional > 0, got {self.notional}")
        if not self.cost_rows:
            raise ValueError("an open PositionContext must carry at least one cost row")
        # The committed margin is imputed from the notional at the configured
        # leverage (the gate's own ``CurrentPositionState.from_signed_size``
        # rule) — a margin% that disagrees with the notional two lines above
        # it would be the prompt contradicting itself.
        _check_derived(
            "PositionContext.margin_pct",
            float(self.margin_pct),
            float(self.notional / self.leverage / self.equity * 100),
            "notional / leverage / equity",
        )
        rate = derive_round_trip_rate(self.taker_fee_rate, self.slippage_bps)
        seen: set[int] = set()
        for row in self.cost_rows:
            if row.target_margin_pct in seen:
                raise ValueError(f"PositionContext has two cost rows for {row.target_margin_pct}%")
            seen.add(row.target_margin_pct)
            _check_derived(
                f"PositionContext cost row {row.target_margin_pct}% trade_notional",
                float(row.trade_notional),
                float(
                    abs(Decimal(row.target_margin_pct) - self.margin_pct)
                    / 100
                    * self.equity
                    * self.leverage
                ),
                "|target - margin_pct| / 100 * equity * leverage",
            )
            _check_derived(
                f"PositionContext cost row {row.target_margin_pct}% round_trip_cost",
                float(row.round_trip_cost),
                float(row.trade_notional * rate),
                "trade_notional * derive_round_trip_rate(fee, slippage)",
            )


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
    # The exchange's own clock, read by ``_build_context`` BEFORE the candle
    # fetch and handed to it as the window's end (UTC; issues #51, #124), so
    # ``as_of`` — the newest bar that window kept — is at or before this by
    # construction, whatever the host's clock reads. The freshness guard
    # measures candle age against THIS when present, not the host clock.
    # ``None`` only for contexts that did not come from a live
    # ``_build_context`` fetch (fixtures, replays) — the guard then falls
    # back to the caller's clock, and on that path a window cut by a host
    # running behind still looks current (the issue-#51 blind spot, which
    # only the exchange-clock path closes).
    exchange_time: datetime | None = None
    # This host's clock as read at the SAME INSTANT as ``exchange_time`` (UTC).
    # The two are only ever subtracted from each other, and that difference is
    # only meaningful if the readings are adjacent: the daemon's own clock
    # reading (the guard's ``now``) is taken before the fetch, so measuring
    # skew against THAT would fold three REST calls of elapsed time in as
    # apparent clock error — enough, on a slow network, to warn about NTP on
    # a correctly-synced host and to blame the clock for a stalled feed.
    # ``None`` whenever there is no paired reading; never populated without
    # ``exchange_time`` (enforced below), though the reverse IS legal — a
    # hand-built context may carry an exchange clock and no pairing, and the
    # guard then reports no skew rather than inventing one.
    host_time_at_exchange_read: datetime | None = None
    # Where volume sat in the price range over a rolling window of candles
    # (:mod:`.volume_profile`). Optional and OFF by default: it is populated
    # only when ``market_data.volume_profile_window_candles`` is configured
    # above zero, so merging the feature changes no existing prompt until an
    # operator turns it on. ``None`` means the prompt omits the section
    # entirely — never a half-filled block (see :class:`VolumeProfile`).
    volume_profile: VolumeProfile | None = None
    # The account's own position and what moving it would cost
    # (:mod:`.marginal_cost`; prompt ``phase2-target-v4``). Optional: attached
    # by the daemon provider from the run's books; ``None`` on the one-shot
    # CLI paths and whenever the books were unusable, and the prompt then
    # omits the section entirely (see :class:`PositionContext`).
    position: PositionContext | None = None

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
        # ``prev_day_price`` mirrors the snapshot's ``>= 0`` (zero = "no 24h
        # reference yet", a freshly listed coin) — and ``day_change_pct`` is a
        # function of it and ``mark_price``, both stored right here, so the
        # three are checked together against :func:`derive_day_change_pct`,
        # the producer's own rule. A context built any other way that breaks
        # it would print "24h change: 40%" over two prices that say 2%, with
        # every bounds check passing — the same contradiction the volume
        # profile's cross-checks exist for.
        if self.prev_day_price < 0:
            raise ValueError(
                f"PerpMarketContext.prev_day_price must be >= 0, got {self.prev_day_price}"
            )
        expected_change = derive_day_change_pct(self.mark_price, self.prev_day_price)
        if (expected_change is None) != (self.day_change_pct is None):
            raise ValueError(
                f"PerpMarketContext.day_change_pct ({self.day_change_pct}) disagrees with "
                f"prev_day_price ({self.prev_day_price}): the change is None exactly when "
                f"there is no reference price (prev_day_price == 0)"
            )
        if self.day_change_pct is not None and expected_change is not None:
            # Coerced first: a hand-built context naturally reaches for Decimal
            # for anything named *_pct, and Decimal - float is a TypeError, not
            # the contradiction message. The shared check's RELATIVE tolerance
            # is what makes it fit this field: the change is unbounded (a dust
            # ``prevDayPx`` under a real mark is a change of 1e11 percent), and
            # a value that size recorded to ten SIGNIFICANT digits — the usual
            # way a float is shortened — is off by whole units, which is far
            # outside 1e-6 absolute and well inside 1e-6 relative. The producer
            # itself is never at risk either way: it fills the field with the
            # same function this check re-derives from.
            claimed = float(self.day_change_pct)
            _check_derived(
                "PerpMarketContext.day_change_pct",
                claimed,
                expected_change,
                f"the prices it is derived from (mark {self.mark_price} over prev_day "
                f"{self.prev_day_price})",
            )
            object.__setattr__(self, "day_change_pct", claimed)
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
        # Same rule for the exchange clock: the guard subtracts the two, and a
        # naive/aware pair raises deep inside the freshness check instead of here.
        if self.exchange_time is not None and self.exchange_time.tzinfo is None:
            raise ValueError("PerpMarketContext.exchange_time must be timezone-aware (UTC)")
        if self.host_time_at_exchange_read is not None:
            if self.host_time_at_exchange_read.tzinfo is None:
                raise ValueError(
                    "PerpMarketContext.host_time_at_exchange_read must be timezone-aware (UTC)"
                )
            if self.exchange_time is None:
                # The field exists only to be subtracted from ``exchange_time``;
                # one without the other is a half-built context, not a degraded
                # one, and would read as "skew unknown" while looking populated.
                raise ValueError(
                    "PerpMarketContext.host_time_at_exchange_read requires exchange_time"
                )
        # Validate ``candle_interval`` against the single source of truth
        # (:class:`CandleInterval`) so an unsupported value fails here at
        # construction — cheap to spot — rather than later inside the guard.
        # The parse IS :func:`parse_interval`'s — the same one :func:`interval_to_ms`
        # is built on — so the check and its message live once, and one lookup
        # serves both the check and the coercion.
        interval = parse_interval(self.candle_interval)
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
        # The position's own guards hold its fields to each other; only the
        # two that depend on THIS context's mark can be checked here. A
        # notional or PnL priced at some other mark would print beside a
        # "Mark:" line that contradicts it.
        pos = self.position
        if pos is not None and pos.side is not None:
            # The §6.1/§6.3 formulas by name (margin.py's one-definition
            # rule). Function-local: ``schema`` sits in config.py's load-time
            # import closure, which tests/common/test_layering.py pins to a
            # short allowlist ``margin`` is not on — that guard walks
            # top-level statements only, and this branch runs only when a
            # position is attached, never on a config load.
            from .margin import funding_cost, position_notional, unrealized_pnl

            # Both non-None on an open position (PositionContext's guard).
            assert pos.unrealized_pnl is not None and pos.entry_price is not None
            _check_derived(
                "PerpMarketContext.position.notional",
                float(pos.notional),
                float(position_notional(pos.size, self.mark_price)),
                "size * mark_price",
            )
            _check_derived(
                "PerpMarketContext.position.unrealized_pnl",
                float(pos.unrealized_pnl),
                float(unrealized_pnl(pos.size, self.mark_price, pos.entry_price)),
                "size * (mark_price - entry_price)",
            )
            # The holding cost is funding on the SIGNED notional at this mark,
            # over the horizon the section states — checked against the
            # Funding: section's own rate so the two cannot disagree, and
            # through ``funding_cost`` so this check cannot be the copy of the
            # formula that drifts (issue #134; the books' income-signed
            # reading negates the same function).
            assert pos.holding_cost_8h is not None
            _check_derived(
                "PerpMarketContext.position.holding_cost_8h",
                float(pos.holding_cost_8h),
                float(
                    funding_cost(pos.size * self.mark_price, self.funding_rate) * HOLDING_COST_HOURS
                ),
                f"funding_rate * {HOLDING_COST_HOURS}h * size * mark_price",
            )
