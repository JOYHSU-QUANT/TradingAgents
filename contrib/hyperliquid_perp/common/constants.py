"""Shared constant vocabulary, dependency-free.

Extracted from ``config.py`` so ``live.config`` validates the same network
vocabulary without importing the loader module — that edge used to be the
reason ``config.load_config`` had to lazy-import ``live.config``.
"""

from __future__ import annotations

from decimal import Decimal

__all__ = [
    "ERROR_TYPES",
    "EXCHANGE_MIN_ORDER_NOTIONAL_USDC",
    "HOLDING_COST_HOURS",
    "LEGAL_NETWORKS",
    "MIN_VOLUME_PROFILE_WINDOW",
    "POC_LOWER_BAND",
    "POC_UPPER_BAND",
    "RANGE_MIDPOINT",
    "STALE_MARKET_DATA_ERROR",
    "THIN_VALUE_AREA_RATIO",
    "VALUE_AREA_FRACTION",
    "VOLUME_PROFILE_BUCKET_COUNT",
]

# The smallest legal ``market_data.volume_profile_window_candles``. Here, not in
# ``domains/perp/volume_profile.py``, for the reason ``indicator_vocab`` was
# split out of ``indicators``: the config loader must be able to enforce the
# rule without importing a compute module. That module is pure stdlib today, so
# importing it would cost nothing measurable — but the invariant being kept is
# "``load_config`` does not import compute modules", and bucketing volume across
# price levels is exactly the code someone later reaches for numpy in. The
# keyless ``live --config-check`` path would acquire it silently, and nothing
# would fail. ``volume_profile`` imports this name; so does the loader's
# ``market_data_config`` parser.
#
# That two-layer readership is also why this floor does not live on a
# ``MarketDataConfig`` field the way ``candle_lookback``'s default does: that
# default's readers (the fetch and the cross-field check) are all in the config
# layer, so the field IS its single declaration (PR #125). Moving this floor
# there would make the compute module import the config module to read it.
#
# See that module for WHY the floor is twelve rather than some other number.
MIN_VOLUME_PROFILE_WINDOW = 12

# The rest of the volume profile's vocabulary — the bucket resolution, the
# value-area convention, and the shape thresholds. Here for the same reason as
# the floor above, in the other direction: ``domains/perp/schema.py`` holds
# the ``VolumeProfile`` DTO, re-derives its ``shape`` and checks its counts
# and shares against these at construction, while ``volume_profile`` (the
# producer) imports ``schema`` — so the numbers must sit below BOTH.
# ``schema.derive_profile_shape`` reads the four thresholds; the producer
# imports the grid (as ``BUCKET_COUNT``) and ``VALUE_AREA_FRACTION``.

# Price-bucket resolution of the profile. The POC and both value-area edges
# are quantized to this grid, so it sets how precisely those levels can be
# stated: 24 buckets over the window's high-low range — fine enough that a
# value-area edge is not a coarse step, coarse enough that with a 30-candle
# window most buckets still collect volume from several bars.
VOLUME_PROFILE_BUCKET_COUNT = 24

# The share of window volume the value area holds — the profile convention.
VALUE_AREA_FRACTION = Decimal("0.70")

# --- Shape thresholds, all fractions of the window's own price range. -------
# The source article validates P/b against "close vs the day's 50% level";
# with no daily close on a perp, the latest candle close and the window's 50%
# level stand in. The rule combining them is ``schema.derive_profile_shape``.

# A value area at or above this share of the range is ``thin`` — "no price
# level held the activity", the elongated trend profile. It sits at the
# uniform-distribution mark on purpose: volume spread perfectly evenly puts
# VALUE_AREA_FRACTION of itself inside that same fraction of the range. Hence
# DERIVED from that constant rather than written as its own 0.70 — the
# sentence above is the whole justification, and it stays true only if the
# two move together.
THIN_VALUE_AREA_RATIO = float(VALUE_AREA_FRACTION)

# The POC bands leave a middle zone for D. Deliberately wider than the
# article's plain "upper half / lower half": a bare 0.5 split makes the label
# flip between cycles on a POC sitting one bucket either side of centre.
POC_UPPER_BAND = 0.60
POC_LOWER_BAND = 0.40

# The window's own midpoint, which the latest close must be the correct side
# of (strictly) before a POC skew is called P or b.
RANGE_MIDPOINT = 0.5

# Hyperliquid's minimum order value — an EXCHANGE fact, not a tuning choice.
# Two layers read it and neither may import the other's module for it: the live
# config gate (§5 rule 4: an effective notional cap below this means no order can
# ever be placed, so startup must fail rather than run a bot structurally unable
# to trade) and the paper engine's ``min_notional_usdc`` default (its TWAP
# clip floor, which simulates the same exchange rule). ``live/`` already imports
# ``paper/``, so the paper default reading it from ``live.config`` would have
# made the dependency two-way; it sits here instead, and ``live.config``
# re-exports it for its own callers (issue #102).
EXCHANGE_MIN_ORDER_NOTIONAL_USDC = Decimal("10")

# The legal network vocabulary, shared by config.py's ``network`` validation
# and live/config.py's ``live.network`` validation. Deliberately duplicated
# from sdk_client._BASE_URLS (not imported) to keep config loading free of the
# heavy SDK import that --context-only relies on being cheap.
LEGAL_NETWORKS = ("mainnet", "testnet")

# The §6.2 ``decision_attempts.error_type`` the freshness guard writes when a
# context is well-formed but its AGE is unusable. Lives here, at the bottom of
# the import graph, because three layers must agree on the exact word without
# importing each other: ``domains.perp.freshness`` produces it (and must stay
# free of both the exchange SDK and the persistence package, so it cannot
# import the vocabulary module), ``repository._vocab`` admits it at the write
# boundary, and the paper/live acceptance validators query on it (issue #50).
# A copy in any of the three would be a silent no-op the day it drifts: the
# reported stale-feed SUBSET would read zero forever, while the gating
# no-decision streak — which is class-blind — would not notice at all.
STALE_MARKET_DATA_ERROR = "stale_market_data"

# The complete §6.2 ``decision_attempts.error_type`` vocabulary — the retry
# classes plus the scheduler's restart-interrupted marker. Defined here rather
# than in ``repository._vocab`` for the reason the word above is: the guard
# that PRODUCES a class (``domains.perp.freshness.ContextRefusal``) validates
# it against this set at construction, and the persistence write boundary
# validates the same set at insert — one definition, two check sites, and the
# producer's site fires on a typo before a cycle is ever recorded instead of
# when the daemon tries to record its failure. ``_vocab`` re-exports it as the
# storage vocabulary; the repository is a consumer of this list, not its owner.
#
# ``malformed_response``: the venue ANSWERED and the answer was unusable (a
# misrouted read, wire-schema drift). Distinct from ``connection`` on purpose —
# a disconnect heals by itself and this does not, so collapsing the two made
# every per-class reading of the trail file a systematically wrong feed as one
# transient blip. The §3.1 ladder treats all of these alike (its delays index
# on attempt count), so a new member here changes records, never behaviour.
# ``stale_market_data``: see above — the venue answered with a well-formed
# context whose AGE is unusable. Added for the same reason as
# ``malformed_response`` and by the same argument: it does not heal on its own,
# so filing it as ``server_error`` made a fault that recurs every cycle until a
# human fixes the feed or the host clock read as one transient blip. The
# acceptance validators tell "this run cannot decide right now" from RUNBOOK
# §7's expected occasional ``api_failed`` by trailing CONSECUTIVENESS, not by
# this class; what the class earns is the specific wording and a reported
# subset (#50).
ERROR_TYPES = frozenset(
    {
        "timeout",
        "rate_limit",
        "connection",
        "malformed_response",
        STALE_MARKET_DATA_ERROR,
        "server_error",
        "interrupted",
    }
)

# The horizon the prompt's position section states holding cost over. Funding
# on Hyperliquid is charged hourly; 8h is one conventional funding "period",
# long enough for the figure to read against a fee. Lives here (not in
# ``marginal_cost``, which computes it) because ``schema`` re-derives the
# stored value against it at construction and cannot import the pricer.
HOLDING_COST_HOURS = 8
