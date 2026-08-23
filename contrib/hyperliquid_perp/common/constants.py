"""Shared constant vocabulary, dependency-free.

Extracted from ``config.py`` so ``live.config`` validates the same network
vocabulary without importing the loader module — that edge used to be the
reason ``config.load_config`` had to lazy-import ``live.config``.
"""

from __future__ import annotations

__all__ = [
    "DEFAULT_CANDLE_LOOKBACK",
    "LEGAL_NETWORKS",
    "MIN_VOLUME_PROFILE_WINDOW",
    "STALE_MARKET_DATA_ERROR",
]

# The smallest legal ``market_data.volume_profile_window_candles``. Here, not in
# ``domains/perp/volume_profile.py``, for the reason ``indicator_vocab`` was
# split out of ``indicators``: the config loader must be able to enforce the
# rule without importing a compute module. That module is pure stdlib today, so
# importing it would cost nothing measurable — but the invariant being kept is
# "``load_config`` does not import compute modules", and bucketing volume across
# price levels is exactly the code someone later reaches for numpy in. The
# keyless ``live --config-check`` path would acquire it silently, and nothing
# would fail. ``volume_profile`` imports this name; the loader imports it too.
#
# See that module for WHY the floor is twelve rather than some other number.
MIN_VOLUME_PROFILE_WINDOW = 12

# How many candles ``engine_bridge._build_context`` fetches when
# ``market_data.candle_lookback`` is absent or blank. Lives here, with the other
# values two layers must agree on, because it is now read from BOTH sides of the
# same rule: the fetch uses it, and ``config._validate_volume_profile_window``
# rejects a volume-profile window wider than it. A second copy would go stale
# silently — the cross-check would keep validating against the old number, and
# the only symptom is a prompt section that never appears.
DEFAULT_CANDLE_LOOKBACK = 200

# The legal network vocabulary, shared by config.py's ``network`` validation
# and live/config.py's ``live.network`` validation. Deliberately duplicated
# from sdk_client._BASE_URLS (not imported) to keep config loading free of the
# heavy SDK import that --context-only relies on being cheap.
LEGAL_NETWORKS = ("mainnet", "testnet")

# The §6.2 ``decision_attempts.error_type`` the freshness guard writes when a
# context is well-formed but its AGE is unusable. Lives here, at the bottom of
# the import graph, because three layers must agree on the exact word without
# importing each other: ``engine_bridge`` produces it (and drags the exchange
# SDK, so no validator may import it), ``repository._vocab`` admits it at the
# write boundary, and the paper/live acceptance validators query on it (issue
# #50). A copy in any of the three would be a silent no-op the day it drifts:
# the reported stale-feed SUBSET would read zero forever, while the gating
# no-decision streak — which is class-blind — would not notice at all.
STALE_MARKET_DATA_ERROR = "stale_market_data"
