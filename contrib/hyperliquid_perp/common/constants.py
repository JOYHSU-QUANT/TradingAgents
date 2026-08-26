"""Shared constant vocabulary, dependency-free.

Extracted from ``config.py`` so ``live.config`` validates the same network
vocabulary without importing the loader module — that edge used to be the
reason ``config.load_config`` had to lazy-import ``live.config``.
"""

from __future__ import annotations

__all__ = [
    "ERROR_TYPES",
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
