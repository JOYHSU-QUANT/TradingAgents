"""Volume profile — where traded volume sat in the price range, and its shape.

Adds the one thing the RSI/EMA/ATR/MACD set cannot express: the *distribution*
of volume across price. Two levels come out of it — the POC (the price bucket
that traded the most) and the value area (the band around the POC holding
:data:`VALUE_AREA_FRACTION` of the window's volume) — plus a coarse one-letter
shape label taken from the source article's D / P / b / thin taxonomy.

Same discipline as :func:`..context_builder.classify_regime`: pure,
deterministic, and fail-closed — every degenerate input returns ``None`` for
the WHOLE profile rather than a partly-filled one, so nothing half-computed can
reach a prompt.

Three things this module is deliberately NOT:

- **Not tick data.** Each candle contributes its volume spread *evenly* across
  its own high-low range, because that is all an OHLCV bar knows. A bar that
  actually traded 90% of its volume at its low is recorded as flat across the
  bar. This is a coarse approximation, and the renderer says so in the prompt.
- **Not a trading session.** The window is the last N candles, rolling — perps
  never close, so there is no daily session to profile. The article's "today's
  profile" reads here as "the last N candles". At the project's ``4h`` candles,
  N=30 spans ~5 days, i.e. a composite profile, not a day profile.
- **Not a gate.** Nothing here feeds sizing, the risk gate, or any refusal. It
  is an analyst *input* only; its value to decision quality is unverified and
  is meant to be measured on a paper run.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal, localcontext

from ...common.constants import MIN_VOLUME_PROFILE_WINDOW as MIN_WINDOW_CANDLES
from ...common.decimal_context import DECIMAL_CONTEXT
from .schema import Candle, ProfileShape, VolumeProfile

logger = logging.getLogger(__name__)

__all__ = [
    "BUCKET_COUNT",
    "DEFAULT_WINDOW_CANDLES",
    "MIN_WINDOW_CANDLES",
    "VALUE_AREA_FRACTION",
    "PriceDistribution",
    "build_profile",
    "classify_shape",
    "compute_volume_profile",
]

# The window width an operator gets by writing
# ``market_data.volume_profile_window_candles: 30``. Not applied as a default
# anywhere — the *code* default is 0 (feature off); this is the documented
# starting value for the config file and the tests.
DEFAULT_WINDOW_CANDLES = 30

# ``MIN_WINDOW_CANDLES`` is the smallest window allowed to produce a profile at
# all. It is DEFINED in ``common.constants`` (imported above under this module's
# own name) so ``config.load_config`` can enforce the floor without importing
# this compute module — see that constant for the layering rationale.
#
# Why twelve: it is a floor, NOT a sufficiency guarantee (same caveat as
# ``indicator_vocab._MIN_CANDLES``) — a single dominant bar can still own the
# POC at twelve candles. It exists to rule out the degenerate end. At the
# project's 4h candles a literal "rolling 24h" window is six bars, where the POC
# is essentially the midpoint of whichever bar traded most and the shape label
# would be noise.

# Price-bucket resolution of the profile. POC and both value-area edges are
# quantized to this grid, so it sets how precisely those levels can be stated:
# 24 buckets over the window's high-low range. Chosen to be fine enough that a
# value-area edge is not a coarse step, and coarse enough that with a
# 30-candle window most buckets still collect volume from several bars.
BUCKET_COUNT = 24

# The share of window volume the value area holds — the profile convention.
VALUE_AREA_FRACTION = Decimal("0.70")

# --- Shape thresholds, all fractions of the window's own price range. -------
#
# The source article validates P/b against "close vs the day's 50% level"; with
# no daily close on a perp, the latest candle close and the window's 50% level
# stand in (design decision 1 in the plan).
#
# ``_THIN_VA_RATIO`` is at the uniform-distribution mark on purpose: volume
# spread perfectly evenly puts VALUE_AREA_FRACTION of it inside that same
# fraction of the range, so a value area at or above this is "no price level
# held the activity" — the elongated trend profile. Checked FIRST because a
# smeared profile's POC location carries no information to classify on.
#
# DERIVED from VALUE_AREA_FRACTION rather than repeating its value: the
# sentence above is the whole justification for the number, and it stays true
# only if the two move together. Written as its own 0.70 they would silently
# come apart the first time the value-area convention was changed, leaving a
# threshold whose comment described a mark it no longer sat on.
_THIN_VA_RATIO = float(VALUE_AREA_FRACTION)

# The POC bands leave a middle zone for D. Deliberately wider than the
# article's plain "upper half / lower half": a bare 0.5 split makes the label
# flip between cycles on a POC sitting one bucket either side of centre.
_POC_UPPER = 0.60
_POC_LOWER = 0.40

# The window's own midpoint, which the latest close must be the correct side of
# before a POC skew is called P or b.
_MID = 0.5


@dataclass(frozen=True)
class PriceDistribution:
    """The raw bucketed distribution — :func:`build_profile`'s output.

    ``bucket_volumes`` is lowest-price-first: index 0 covers
    ``[range_low, range_low + width)`` and the last index closes at
    ``range_high``. Intermediate on purpose — :class:`VolumeProfile` is what
    the context carries; this type exists so the bucketing can be tested
    independently of the shape rules.
    """

    range_low: Decimal
    range_high: Decimal
    bucket_volumes: tuple[Decimal, ...]
    candle_count: int

    def __post_init__(self) -> None:
        if self.range_low <= 0:
            raise ValueError(f"PriceDistribution.range_low must be > 0, got {self.range_low}")
        if self.range_high <= self.range_low:
            raise ValueError(
                f"PriceDistribution.range_high ({self.range_high}) must be > range_low "
                f"({self.range_low})"
            )
        if not self.bucket_volumes:
            raise ValueError("PriceDistribution.bucket_volumes must not be empty")
        if any(v < 0 for v in self.bucket_volumes):
            raise ValueError("PriceDistribution.bucket_volumes must all be >= 0")
        # An all-zero distribution has no POC to find; ``build_profile`` already
        # refuses one, so reaching here means a hand-built instance.
        if sum(self.bucket_volumes) <= 0:
            raise ValueError("PriceDistribution.bucket_volumes must sum to > 0")
        if self.candle_count < 1:
            raise ValueError(
                f"PriceDistribution.candle_count must be >= 1, got {self.candle_count}"
            )


def _bucket_width(dist_low: Decimal, dist_high: Decimal, count: int) -> Decimal:
    """The nominal width of one price bucket.

    One definition, three callers (:func:`_bucket_edges`, :func:`build_profile`
    for the zero-range-candle index, :func:`classify_shape` for the POC price).
    Recomputed per call rather than threaded through as a parameter: the whole
    call chain runs once per engine cycle over ~30 candles, so the arithmetic is
    free, and a ``width`` passed in could disagree with the ``dist_low`` /
    ``dist_high`` it is supposed to divide — which is precisely the kind of
    silent inconsistency :func:`_bucket_edges` exists to rule out.
    """
    return (dist_high - dist_low) / count


def _bucket_edges(
    dist_low: Decimal, dist_high: Decimal, index: int, count: int
) -> tuple[Decimal, Decimal]:
    """Half-open edges of bucket ``index``, with the outermost edges pinned.

    ``width * count`` does not reliably reproduce ``dist_high - dist_low``: at
    28 significant digits it can land a sub-ulp either side of it. WHETHER it
    does is governed by how many significant digits ``dist_low`` and ``width``
    demand between them — the residual has to still be representable once the
    two are added — so it is a property of the numbers' MAGNITUDES, not of the
    coin's tick grid.

    Sweeps of 50k synthetic windows each, comparing ``dist_low + width * 24``
    to ``dist_high`` under this module's own 28-digit context:

    - Hold the grid at BTC's whole dollars, ``range_low`` in $30k-$120k, and
      vary only how WIDE the window is: a realistic 30-candle high-low
      ($100-$12k) misses on 0% of windows, an implausible $1k-$100k spread on
      ~8%, an absurd $10k-$500k one on ~37%. One grid throughout — only the
      width moved. The middle figure is the sensitive one and the price band
      is why it has to be stated: the same $1k-$100k spread drops to ~1.5%
      with ``range_low`` in $60k-$120k and to 0% in $100k-$200k. The absurd
      case is the steady one, staying in the mid-30s across all three bands.
    - Hold the window's width at 3-8% of each sample's own price — so the
      RELATIVE window is fixed — and vary the grid: 1, 0.1, 0.01, 0.001 and
      1e-8 ticks all come out at 0%, the sub-cent coin included. Put that same
      sub-cent grid on an absurdly wide window instead and it misses on ~39%.
      The tick grid is not the variable; the width is.

    So no window this project can currently produce reaches it. Pinning is
    still worth its zero cost, because the overshoot half is a crash: when the
    value area reaches the TOP bucket, an unpinned top edge becomes
    ``value_area_high``, and a ``value_area_high`` above ``range_high`` is
    exactly what :class:`VolumeProfile`'s guard rejects — the cycle would die
    on a ValueError raised out of a pure function. Taking the outer edges from
    the range itself makes them correct by definition rather than by rounding
    luck.

    The sweeps above are a one-off; what stays checkable is
    ``test_a_whole_dollar_range_overshoots_once_it_is_large_enough``, which
    pins one concrete whole-dollar pair that overshoots — the counterexample to
    reading any of this as "fine ticks overshoot, coarse ticks do not".

    (The undershoot half leaves a ~1e-27-wide sliver of price above the last
    bucket. Pinning closes that too, but the volume involved is ~1e-28 of one
    candle's — housekeeping, not a defect.)

    Neither pin makes the bucketing bit-exact: ``volume * (overlap / span)``
    rounds per candle per bucket, so the bucket total tracks the input total to
    about a 1e-24 relative error rather than to the last digit.
    """
    width = _bucket_width(dist_low, dist_high, count)
    low = dist_low if index == 0 else dist_low + width * index
    high = dist_high if index == count - 1 else dist_low + width * (index + 1)
    return low, high


def build_profile(candles: Sequence[Candle], window: int) -> PriceDistribution | None:
    """Bucket the last ``window`` candles' volume across their price range.

    Returns ``None`` — never a partial distribution — when the profile cannot
    be built: the feature is off (``window <= 0``), the window is below
    :data:`MIN_WINDOW_CANDLES`, there is less history than the window asks for,
    the window's price range has zero width, or it traded no volume at all.

    A short window is REFUSED rather than silently narrowed to whatever history
    exists: a profile labelled "30 candles" that was really cut from 8 would
    read as the wider, steadier structure it is not.
    """
    if window <= 0:
        return None  # feature disabled — the documented off switch, not a fault
    if window < MIN_WINDOW_CANDLES:
        # Config-shaped mistake. ``config.load_config`` rejects it up front, so
        # a caller reaching here built its window some other way; the section
        # vanishing from the prompt is otherwise invisible.
        logger.warning(
            "volume-profile window of %d candle(s) is below the %d-candle minimum; "
            "skipping the volume profile for this cycle",
            window,
            MIN_WINDOW_CANDLES,
        )
        return None
    if len(candles) < window:
        logger.warning(
            "volume-profile window asks for %d candles but only %d are available; "
            "skipping the volume profile for this cycle (raise "
            "market_data.candle_lookback or lower the window)",
            window,
            len(candles),
        )
        return None

    recent = tuple(candles[-window:])
    range_low = min(c.low for c in recent)
    range_high = max(c.high for c in recent)
    if range_high <= range_low:
        # Every candle printed the same single price across the whole window.
        logger.warning(
            "volume-profile window has zero price width (all %d candles at %s); "
            "skipping the volume profile for this cycle",
            len(recent),
            range_low,
        )
        return None
    if sum((c.volume for c in recent), Decimal(0)) <= 0:
        logger.warning(
            "volume-profile window traded zero volume across %d candles; "
            "skipping the volume profile for this cycle",
            len(recent),
        )
        return None

    buckets = [Decimal(0)] * BUCKET_COUNT
    with localcontext(DECIMAL_CONTEXT):
        width = _bucket_width(range_low, range_high, BUCKET_COUNT)
        for candle in recent:
            if candle.volume == 0:
                continue
            if candle.high == candle.low:
                # A zero-range bar has nothing to spread across; drop it whole
                # into the bucket containing its single price. ``int()``
                # truncates and the price sits inside the range, so the clamp
                # only catches a bar printing exactly ``range_high``.
                index = int((candle.low - range_low) / width)
                buckets[min(max(index, 0), BUCKET_COUNT - 1)] += candle.volume
                continue
            span = candle.high - candle.low
            for i in range(BUCKET_COUNT):
                low, high = _bucket_edges(range_low, range_high, i, BUCKET_COUNT)
                overlap = min(candle.high, high) - max(candle.low, low)
                if overlap > 0:
                    buckets[i] += candle.volume * (overlap / span)

    return PriceDistribution(
        range_low=range_low,
        range_high=range_high,
        bucket_volumes=tuple(buckets),
        candle_count=len(recent),
    )


def classify_shape(profile: PriceDistribution, latest_close: Decimal) -> VolumeProfile:
    """Find the POC and value area, and label the shape.

    The value area grows OUTWARD from the POC bucket, one neighbour at a time,
    always taking the heavier of the two adjacent buckets (ties go upward),
    until it holds :data:`VALUE_AREA_FRACTION` of the window's volume. The POC
    is the midpoint of the heaviest bucket; ties there go to the LOWEST such
    bucket, so the result is reproducible on a symmetric distribution instead
    of depending on iteration luck.

    The two tie-breaks therefore point in OPPOSITE directions — POC ties resolve
    downward, value-area ties upward. Neither direction means anything: a tie
    says the buckets are indistinguishable on volume, so there is no better
    answer to pick. Each is pinned only to make the output reproducible, and
    the two were chosen independently rather than to a shared convention —
    both arrived in this module's first commit; neither was ever a repair. Do
    not read a signal into either one.

    The upward value-area tie-break has a visible consequence worth stating:
    between equal neighbours the walk always climbs, so it can cross a run of
    near-empty buckets before turning back. A wide value area is therefore
    evidence about the WALK, not proof that the volume needed the range — see
    the ``thin`` note in ``prompt_context``, which says so in the prompt.

    This single-bucket greedy walk is also not the Market Profile literature's
    rule, which compares the SUM OF THE NEXT TWO rows on each side. The two
    disagree when an isolated spike sits next to a light bucket, and because the
    value area's width feeds the ``thin`` test, a disagreement can change the
    letter, not just the edges. The simpler walk is deliberate; the difference is
    recorded so nobody reads "value area" here as the literature's quantity.

    Alongside the geometry, the result carries how much VOLUME those levels
    hold (``poc_volume_share`` / ``value_area_volume_share``). Nothing here
    classifies on them — they exist because the ``*_position`` fields alone
    cannot distinguish a dominant POC from a marginal one.

    Shape rules, checked in this order:

    1. ``thin`` — the value area spans at least :data:`_THIN_VA_RATIO` of the
       range. First, because a smeared profile's POC tells you nothing.
    2. ``P`` — POC at or above :data:`_POC_UPPER` of the range AND
       ``latest_close`` above the window midpoint.
    3. ``b`` — the mirror: POC at or below :data:`_POC_LOWER` AND
       ``latest_close`` below the midpoint.
    4. ``D`` — everything else.

    Note what ``D`` therefore means here: "no one-sided structure was
    confirmed", which covers both a genuinely centred profile AND a skewed POC
    whose close did not confirm it. Distribution *symmetry* is not tested — POC
    centrality is the only proxy — so no caller may read ``D`` as "the profile
    is symmetric".

    Raises ``ValueError`` if ``latest_close`` sits outside ``profile``'s range.
    :func:`compute_volume_profile` cannot trigger that (it takes both from the
    same candles, and a candle's close always lies within its own high-low), so
    it only catches a caller pairing a close with a foreign distribution.
    """
    if not (profile.range_low <= latest_close <= profile.range_high):
        raise ValueError(
            f"latest_close ({latest_close}) is outside the profile's range "
            f"[{profile.range_low}, {profile.range_high}] — the close and the "
            f"distribution must come from the same candle window"
        )

    volumes = profile.bucket_volumes
    count = len(volumes)
    # ``.index(max(...))`` returns the FIRST maximum, i.e. the lowest-priced
    # bucket among ties. See the docstring: this is the reproducibility choice.
    poc_index = volumes.index(max(volumes))

    with localcontext(DECIMAL_CONTEXT):
        # ``PriceDistribution`` guarantees a positive total, so both shares
        # below divide by a non-zero number.
        total = sum(volumes, Decimal(0))
        target = total * VALUE_AREA_FRACTION
        low_index = high_index = poc_index
        held = volumes[poc_index]
        while held < target and (low_index > 0 or high_index < count - 1):
            below = volumes[low_index - 1] if low_index > 0 else None
            above = volumes[high_index + 1] if high_index < count - 1 else None
            if above is not None and (below is None or above >= below):
                high_index += 1
                held += volumes[high_index]
            else:
                low_index -= 1
                held += volumes[low_index]

        value_area_low, _ = _bucket_edges(profile.range_low, profile.range_high, low_index, count)
        _, value_area_high = _bucket_edges(profile.range_low, profile.range_high, high_index, count)
        width = _bucket_width(profile.range_low, profile.range_high, count)
        poc = profile.range_low + width * (Decimal(poc_index) + Decimal("0.5"))
        span = profile.range_high - profile.range_low
        close_fraction = float((latest_close - profile.range_low) / span)
        # How much volume the levels actually hold, which the geometry above
        # cannot express: a POC owning 30% of the window and one owning 4% sit
        # at the same place on the range and produce identical *_position
        # fields. ``value_area_volume_share`` is the share the walk really
        # reached, not VALUE_AREA_FRACTION — it stops on the first bucket that
        # crosses the target, so it lands at or above it, never exactly on it
        # except by coincidence.
        poc_volume_share = float(volumes[poc_index] / total)
        value_area_volume_share = float(held / total)

    # Derived from the bucket indices, not from the Decimal prices: the POC is
    # a bucket midpoint and the value-area edges are bucket edges, so integer
    # arithmetic gives these fractions exactly and cannot drift out of [0, 1]
    # on a rounding ulp — which the schema guards would then reject.
    poc_position = (2 * poc_index + 1) / (2 * count)
    value_area_width_ratio = (high_index - low_index + 1) / count
    # The Decimal division above is exact enough in practice, but a close
    # sitting exactly on ``range_high`` can round a hair past 1.0; the schema
    # rejects that, so pin the fraction to its mathematical bounds.
    close_position = min(max(close_fraction, 0.0), 1.0)

    if value_area_width_ratio >= _THIN_VA_RATIO:
        shape = ProfileShape.THIN
    elif poc_position >= _POC_UPPER and close_position > _MID:
        shape = ProfileShape.P
    elif poc_position <= _POC_LOWER and close_position < _MID:
        shape = ProfileShape.B
    else:
        shape = ProfileShape.D

    return VolumeProfile(
        shape=shape,
        poc=poc,
        value_area_low=value_area_low,
        value_area_high=value_area_high,
        range_low=profile.range_low,
        range_high=profile.range_high,
        poc_position=poc_position,
        close_position=close_position,
        value_area_width_ratio=value_area_width_ratio,
        poc_volume_share=poc_volume_share,
        value_area_volume_share=value_area_volume_share,
        candle_count=profile.candle_count,
        bucket_count=count,
    )


def compute_volume_profile(candles: Sequence[Candle], window: int) -> VolumeProfile | None:
    """Build and classify in one call, or ``None`` if the profile is unavailable.

    This is what ``context_builder`` uses. ``window <= 0`` disables the feature
    and returns ``None`` without a log line — that is the configured off state,
    not a degradation.
    """
    distribution = build_profile(candles, window)
    if distribution is None:
        return None
    # Safe indexing: ``build_profile`` only returns a distribution when it held
    # at least ``MIN_WINDOW_CANDLES`` candles, so ``candles`` is non-empty, and
    # its last candle is the last one in the window it profiled.
    return classify_shape(distribution, candles[-1].close)
