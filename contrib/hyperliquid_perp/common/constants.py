"""Shared constant vocabulary, dependency-free.

Extracted from ``config.py`` so ``live.config`` validates the same network
vocabulary without importing the loader module — that edge used to be the
reason ``config.load_config`` had to lazy-import ``live.config``.
"""

from __future__ import annotations

__all__ = ["LEGAL_NETWORKS", "STALE_MARKET_DATA_ERROR"]

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
# #50). A copy in any of the three would be a silent no-op the day it drifts —
# the streak would simply always read zero.
STALE_MARKET_DATA_ERROR = "stale_market_data"
