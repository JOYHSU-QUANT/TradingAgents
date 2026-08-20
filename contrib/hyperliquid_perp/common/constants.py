"""Shared constant vocabulary, dependency-free.

Extracted from ``config.py`` so ``live.config`` validates the same network
vocabulary without importing the loader module — that edge used to be the
reason ``config.load_config`` had to lazy-import ``live.config``.
"""

from __future__ import annotations

__all__ = ["LEGAL_NETWORKS"]

# The legal network vocabulary, shared by config.py's ``network`` validation
# and live/config.py's ``live.network`` validation. Deliberately duplicated
# from sdk_client._BASE_URLS (not imported) to keep config loading free of the
# heavy SDK import that --context-only relies on being cheap.
LEGAL_NETWORKS = ("mainnet", "testnet")
