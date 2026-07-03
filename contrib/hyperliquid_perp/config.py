"""Load the Hyperliquid perp config from YAML.

Prefers ``configs/hyperliquid.local.yaml`` (gitignored — holds the public wallet
address + network) and falls back to the committed ``hyperliquid.example.yaml``
so ``--context-only`` works out of the box without any local setup.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

_CONFIG_DIR = Path(__file__).parent / "configs"
_LOCAL = _CONFIG_DIR / "hyperliquid.local.yaml"
_EXAMPLE = _CONFIG_DIR / "hyperliquid.example.yaml"

# Sentinel placeholder in the example file — treated as "no wallet configured".
_WALLET_PLACEHOLDER = "0xYOUR..."

# The complete set of recognised top-level config keys. Unknown keys are
# rejected (not ignored): a typo in a *block name* — e.g. ``riks:`` — would
# otherwise silently drop the whole block and fall back to defaults, and for the
# ``risk:`` block that means trading at the permissive default caps. Keys inside
# each block are validated by that block's own parser.
_ALLOWED_TOP_LEVEL_KEYS = frozenset(
    {
        "network",
        "network_timeout_s",
        "wallet_address",
        "coins",
        "market_data",
        "indicators",
        "engine",
        "risk",
        "decision",
    }
)


def config_path() -> Path:
    """The config file that :func:`load_config` will read."""
    return _LOCAL if _LOCAL.exists() else _EXAMPLE


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    """Parse the YAML config into a dict."""
    resolved = Path(path) if path else config_path()
    if not resolved.exists():
        raise FileNotFoundError(
            f"config not found at {resolved}. Copy {_EXAMPLE.name} to {_LOCAL.name} and fill it in."
        )
    with resolved.open(encoding="utf-8") as fh:
        config = yaml.safe_load(fh) or {}
    if not isinstance(config, dict):
        raise ValueError("config must be a YAML mapping at the top level")
    unknown = set(config) - _ALLOWED_TOP_LEVEL_KEYS
    if unknown:
        raise ValueError(
            f"unknown top-level config key(s): {', '.join(map(repr, sorted(unknown)))}. "
            f"Allowed: {', '.join(sorted(_ALLOWED_TOP_LEVEL_KEYS))}."
        )
    return config


def wallet_address(config: dict[str, Any]) -> str | None:
    """Return the configured wallet address, or ``None`` if unset/placeholder."""
    addr = (config.get("wallet_address") or "").strip()
    if not addr or addr == _WALLET_PLACEHOLDER:
        return None
    return addr
