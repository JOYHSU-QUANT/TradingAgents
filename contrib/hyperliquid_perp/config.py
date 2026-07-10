"""Load the Hyperliquid perp config from YAML.

Prefers ``configs/hyperliquid.local.yaml`` (gitignored — holds the public wallet
address + network) and falls back to the committed ``hyperliquid.example.yaml``
so ``--context-only`` works out of the box without any local setup.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import yaml

_CONFIG_DIR = Path(__file__).parent / "configs"
_LOCAL = _CONFIG_DIR / "hyperliquid.local.yaml"
_EXAMPLE = _CONFIG_DIR / "hyperliquid.example.yaml"

# Sentinel placeholder in the example file — treated as "no wallet configured".
_WALLET_PLACEHOLDER = "0xYOUR..."

# Everything load_config can raise for an operator config mistake — a missing or
# unreadable path (OSError), a YAML syntax error, or a failed validation below.
# Callers turn any of these into a named exit, never a raw traceback; the list
# lives here so a parser swap updates it next to the code that raises.
CONFIG_LOAD_ERRORS = (ValueError, OSError, yaml.YAMLError)

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
        "paper_trading",
    }
)


def load_dotenv_files() -> None:
    """Load the project ``.env`` / ``.env.enterprise`` into ``os.environ``.

    Mirrors the loads in ``tradingagents/__init__`` so an ``OPENROUTER_API_KEY``
    kept in the repo-root ``.env`` satisfies this module's CLIs too. The engine
    package performs the same loads on import, but it is imported lazily — only
    once a cycle actually drives the AI — which is *after* the startup API-key
    checks here, so the CLI entry points must load the files themselves first.
    ``load_dotenv`` defaults to ``override=False`` (an exported variable always
    wins over the file). Degradations never raise — a missing python-dotenv
    means env-vars-only operation (same as upstream), and an unreadable file
    (e.g. saved as UTF-16 by a bare PowerShell ``>>`` redirection) warns on
    stderr and continues, so both CLI entry points keep their named-exit
    contract instead of dying on a raw traceback before any handler exists.
    """
    try:
        from dotenv import find_dotenv, load_dotenv
    except ImportError:
        return
    # Per-file try: a corrupt .env must degrade only itself, not suppress a
    # healthy .env.enterprise (the missing-file fallback re-load of .env keeps
    # its upstream behaviour, so the label goes generic when the path is "").
    for name in (".env", ".env.enterprise"):
        path = find_dotenv(name, usecwd=True)
        try:
            load_dotenv(path)
        except (OSError, UnicodeDecodeError) as exc:
            print(
                f"warning: could not read {path or 'a .env file'}: {exc} — "
                "continuing with the exported environment variables only. "
                "Is the file saved as UTF-8?",
                file=sys.stderr,
            )


def dotenv_diagnosis(var: str) -> str:
    """One line explaining why the ``.env`` files did not satisfy ``var``.

    Appended to the startup key-check failure messages, so the operator can
    tell apart "no .env found from this working directory", "found one but it
    does not set the key", and "python-dotenv unavailable" without any
    steady-state log noise on the healthy path.
    """
    try:
        from dotenv import dotenv_values, find_dotenv
    except ImportError:
        return "python-dotenv is not importable, so .env files were ignored"
    path = find_dotenv(usecwd=True)
    if not path:
        return f"no .env found walking up from {Path.cwd()}"
    try:
        values = dotenv_values(path)
    except (OSError, UnicodeDecodeError) as exc:
        return f"found {path} but could not read it ({exc})"
    if values.get(var):
        return f"{path} sets {var}, but an exported empty {var} takes precedence over .env files"
    return f"found {path} but it does not set {var}"


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
            f"Allowed: {', '.join(sorted(_ALLOWED_TOP_LEVEL_KEYS))}"
        )
    # A present-but-blank key (``market_data:`` with nothing after it — a normal
    # state when an operator comments out a block's contents) parses to None, not
    # a missing key, so every ``config.get(key, default)`` downstream would return
    # None instead of its default and crash on the first attribute access. Treat
    # blank exactly like absent — drop the key here so one rule covers every
    # top-level-key consumer, current and future. (Nulls *inside* a block, e.g.
    # ``candle_lookback:`` left blank, stay with each consumer's per-site default.)
    for key in [k for k, v in config.items() if v is None]:
        del config[key]
    # Validate the shape of container blocks up front. Without this a malformed
    # block (``market_data: 5``, ``coins: BTC``) survives key validation and then
    # blows up deep in the run — ``5.get(...)`` (AttributeError) or ``"BTC"[0]``
    # silently taking the first character — instead of a clean exit-1 here. The
    # ``risk:``/``decision:`` blocks are shape-checked by their own from_dict.
    for key in ("market_data", "engine", "paper_trading"):
        val = config.get(key)
        if val is not None and not isinstance(val, dict):
            raise ValueError(f"{key!r} must be a mapping, got {val!r}")
    coins = config.get("coins")
    if coins is not None and not isinstance(coins, list):
        raise ValueError(f"'coins' must be a list, got {coins!r}")
    # These three values are consumed by the Phase-1 client deep inside the run
    # (sdk_client from_config/__init__, wallet_address()); a bad value there
    # surfaces as an exit-2 traceback instead of a named config error. Validate
    # up front so an operator typo stays in the CONFIG_LOAD_ERRORS lane —
    # sdk_client's own ValueError remains the standalone defense. The legal
    # network set is duplicated here (not imported) to keep this module free of
    # the heavy SDK import that --context-only relies on being cheap.
    network = config.get("network")
    if network is not None and (
        not isinstance(network, str) or network.strip().lower() not in ("mainnet", "testnet")
    ):
        raise ValueError(f"'network' must be 'mainnet' or 'testnet', got {network!r}")
    timeout = config.get("network_timeout_s")
    if timeout is not None:
        try:
            float(timeout)
        except (TypeError, ValueError):
            raise ValueError(
                f"'network_timeout_s' must be a number (seconds), got {timeout!r}"
            ) from None
    addr = config.get("wallet_address")
    if addr is not None and not isinstance(addr, str):
        raise ValueError(f"'wallet_address' must be a string, got {addr!r}")
    return config


def wallet_address(config: dict[str, Any]) -> str | None:
    """Return the configured wallet address, or ``None`` if unset/placeholder."""
    addr = (config.get("wallet_address") or "").strip()
    if not addr or addr == _WALLET_PLACEHOLDER:
        return None
    return addr
