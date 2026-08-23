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


def _check_derived_fraction(label: str, claimed: float, numerator: Decimal, span: Decimal) -> None:
    """Raise unless ``claimed`` really is ``numerator / span``.

    A DTO that stores BOTH a ratio and the values it was computed from can be
    handed the two disagreeing, and every bounds check still passes — the
    contradiction only shows up in whatever renders them side by side. This is
    the one shared statement of that invariant.

    Tolerance rather than equality. Two of the reasons turn out to cost
    nothing in practice: over 4000 fuzzed producer outputs spanning six price
    magnitudes, the float-vs-Decimal gap and the grid quantization (see
    ``volume_profile._bucket_edges``, which pins its outermost edges) came out
    at EXACTLY zero, every time. What the tolerance actually buys is the third
    reason — a DTO rebuilt from a ratio someone rounded on the way in. 1e-6
    admits anything recorded to six decimal places or better, and is orders of
    magnitude below any disagreement worth calling a contradiction.

    Only ``VolumeProfile`` calls this today. ``PerpMarketContext.day_change_pct``
    has the same shape — a float ratio stored beside the two Decimal prices it
    comes from — and is NOT guarded. That is a deliberate deferral, not an
    oversight: that DTO is constructed in far more places, so tightening it
    needs its own pass rather than riding along with the volume profile.
    """
    expected = float(numerator / span)
    if abs(claimed - expected) > 1e-6:
        raise ValueError(
            f"{label} ({claimed}) contradicts the values it is derived from — those give {expected}"
        )


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
        # against those prices at the bottom of this method. EVERYTHING ELSE
        # gets bounds only, and is listed here rather than summarized.
        #
        # ``close_position`` is the one nothing here could ever check: it is a
        # fraction of a ``latest_close`` this class does not store, and adding
        # the field to store it serves no consumer.
        #
        # The rest are checks that ARE definable and are deliberately not
        # written, which is a different thing from impossible:
        #   - ``shape`` is fully determined by ``value_area_width_ratio``,
        #     ``poc_position`` and ``close_position``, all stored right here, yet
        #     is never re-derived — so a hand-built profile can label itself
        #     ``P`` while its own numbers say ``D``.
        #   - ``candle_count`` admits 1-11, and ``bucket_count`` anything but 24;
        #     no walk produces either.
        #   - ``poc_volume_share`` cannot fall below ``1 / bucket_count`` (the
        #     heaviest bucket is at least the average), and the producer's walk
        #     always carries ``value_area_volume_share`` to VALUE_AREA_FRACTION.
        # Most of those need thresholds that live in ``volume_profile``, which
        # imports THIS module — so at module scope the dependency cannot run
        # back the other way without moving the constants into ``common/``.
        # They belong with the consumer that reads these fields, which does not
        # exist yet; none of them is load-bearing while the producer is the only
        # caller.
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
        if self.candle_count < 1:
            raise ValueError(f"VolumeProfile.candle_count must be >= 1, got {self.candle_count}")
        if self.bucket_count < 1:
            raise ValueError(f"VolumeProfile.bucket_count must be >= 1, got {self.bucket_count}")
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
        object.__setattr__(self, "shape", ProfileShape(self.shape))


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
    # The exchange's own clock, read during the same fetch that produced
    # ``as_of`` (UTC). The freshness guard measures candle age against THIS
    # when present, not the host clock: ``as_of`` is cut from a window the
    # host clock bounded, so a host that runs behind makes a stale window
    # look current (issue #51). ``None`` only for contexts that did not come
    # from a live ``_build_context`` fetch (fixtures, replays) — the guard
    # then falls back to the caller's clock, blind spot and all.
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
