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
        return yaml.safe_load(fh) or {}


def wallet_address(config: dict[str, Any]) -> str | None:
    """Return the configured wallet address, or ``None`` if unset/placeholder."""
    addr = (config.get("wallet_address") or "").strip()
    if not addr or addr == _WALLET_PLACEHOLDER:
        return None
    return addr
