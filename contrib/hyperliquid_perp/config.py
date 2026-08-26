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

from .common.config_coercion import bool_from_yaml
from .common.constants import LEGAL_NETWORKS
from .domains.perp.indicator_vocab import REGIME_INDICATORS, supported_indicators
from .domains.perp.market_data_config import MarketDataConfig

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

# The dotenv files the loader reads, in load order. dotenv_diagnosis walks the
# same tuple so its verdicts can never contradict what the loader just did —
# extend HERE (only) to add a file to both.
_DOTENV_FILE_NAMES = (".env", ".env.enterprise")

# Everything reading a found dotenv file can raise (e.g. saved as UTF-16 by a
# bare PowerShell ``>>``). Shared with engine_bridge._build_engine_config's import guard
# so the loader's warn-and-continue set and the guard's named-error set never
# fork.
DOTENV_READ_ERRORS = (OSError, UnicodeDecodeError)

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
        "live",
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
    # healthy .env.enterprise. A missing file makes find_dotenv return "" and
    # load_dotenv("") is a silent no-op (the same call upstream makes), so only
    # a found-but-unreadable file can reach the warning below.
    for name in _DOTENV_FILE_NAMES:
        # find_dotenv is inside the guard too: usecwd=True calls os.getcwd(),
        # which raises OSError if the working directory was deleted — the
        # degradation contract must cover the scan, not just the read.
        path = ""
        try:
            path = find_dotenv(name, usecwd=True)
            load_dotenv(path)
        except DOTENV_READ_ERRORS as exc:
            print(
                f"warning: could not read {path or name}: {exc} — "
                "continuing with the exported environment variables only. "
                "Is the file saved as UTF-8?",
                file=sys.stderr,
            )


def dotenv_diagnosis(var: str) -> str:
    """One line explaining why the ``.env`` files did not satisfy ``var``.

    Appended to the startup key-check failure messages, so the operator can
    tell apart "no dotenv file found from this working directory", "found one
    but it does not set the key", and "python-dotenv unavailable" without any
    steady-state log noise on the healthy path. Inspects the same two files, in
    the same order, as :func:`load_dotenv_files` — the diagnosis must never
    contradict what the loader just did (e.g. claim "no .env found" when a
    ``.env.enterprise`` was loaded moments earlier).
    """
    try:
        from dotenv import dotenv_values, find_dotenv
    except ImportError as exc:
        return f"python-dotenv is not importable ({exc}), so .env files were ignored"
    found: list[str] = []
    unreadable: str | None = None
    empty_in: str | None = None
    for name in _DOTENV_FILE_NAMES:
        try:
            path = find_dotenv(name, usecwd=True)
        except DOTENV_READ_ERRORS as exc:
            # os.getcwd() on a deleted working directory — same degradation
            # contract as the loader's scan.
            return f"could not scan for {name} ({exc})"
        if not path:
            continue
        found.append(path)
        try:
            values = dotenv_values(path)
        except DOTENV_READ_ERRORS as exc:
            if unreadable is None:
                unreadable = f"found {path} but could not read it ({exc})"
            continue
        if values.get(var):
            # The loader already ran in this process, so a readable file that
            # sets the var can only have lost to something that set it empty
            # first. An earlier file's blank assignment (``{var}=``) is loaded
            # into os.environ and blocks this one exactly like an exported
            # empty value would — blaming "an export" then sends the operator
            # to the wrong place, away from the fixable line in the repo.
            if empty_in is not None:
                return (
                    f"{path} sets {var}, but {empty_in} sets it to an empty "
                    f"value that loads first and blocks it — remove or fill "
                    f"in that line"
                )
            return (
                f"{path} sets {var}, but an exported empty {var} takes precedence over .env files"
            )
        if var in values and empty_in is None:
            empty_in = path
    if unreadable is not None:
        return unreadable
    if len(found) == 1:
        return f"found {found[0]} but it does not set {var}"
    if found:
        return f"found {' and '.join(found)} but neither sets {var}"
    return f"no {' or '.join(_DOTENV_FILE_NAMES)} found walking up from {Path.cwd()}"


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
    # ``candle_lookback:`` left blank, are each block parser's business —
    # ``config_overrides`` reads them as absent, so the dataclass field default
    # applies.)
    for key in [k for k, v in config.items() if v is None]:
        del config[key]
    # Validate the shape of container blocks up front. Without this a malformed
    # block (``market_data: 5``, ``coins: BTC``) survives key validation and then
    # blows up deep in the run — ``5.get(...)`` (AttributeError) or ``"BTC"[0]``
    # silently taking the first character — instead of a clean exit-1 here. The
    # ``risk:``/``decision:``/``live:``/``market_data:`` blocks are shape-
    # checked by their own from_dict too (``live:`` and ``market_data:``
    # additionally here, for the message naming the block — and for
    # ``live:``, so a scalar ``live: true`` fails at load even on paths that
    # never parse the block).
    for key in ("market_data", "engine", "paper_trading", "live"):
        val = config.get(key)
        if val is not None and not isinstance(val, dict):
            raise ValueError(f"{key!r} must be a mapping, got {val!r}")
    # The ``market_data:`` block is parsed on every load — unknown keys, wrong
    # types and out-of-band values all fail here, named. It is the block whose
    # mistakes are otherwise the quietest: a typo'd key fell back to its
    # default with no signal, and a bad value surfaced (if at all) as a bare
    # ValueError from inside the market fetch — which nothing on the paper
    # daemon's path catches (issue #96).
    # Only the parse's verdict is used; engine_bridge re-parses the same block
    # at fetch time, so the loaded dict stays the plain YAML the drift check
    # and the genesis snapshot compare.
    try:
        MarketDataConfig.from_dict(config.get("market_data"))
    except ValueError as exc:
        raise ValueError(f"invalid market_data: config — {exc}") from None
    # A scalar here would silently resolve to per-character values downstream
    # (``coins: BTC`` → "B"; ``indicators: rsi_14`` → six unknown names, which
    # zeroes the warm-up threshold and empties the all-dead-indicator guard).
    for key in ("coins", "indicators"):
        val = config.get(key)
        if val is not None and not isinstance(val, list):
            raise ValueError(f"{key!r} must be a list, got {val!r}")
    # List shape alone still lets a typo'd element through (``rsi14``): an
    # unknown name computes to a permanent None that every context guard skips
    # (it zeroes its share of the warm-up threshold, and the all-dead guard's
    # known-names filter excludes it). The vocabulary is closed, so reject
    # unknown names here; ``coins`` has no local vocabulary — a bogus symbol
    # fails loud at the first exchange call instead. List membership (not a
    # set) on purpose: unhashable junk (``indicators: [[rsi_14]]``) must land
    # in the error message, not raise a bare TypeError.
    inds = config.get("indicators")
    if inds:
        known = supported_indicators()
        unknown_inds = [name for name in inds if name not in known]
        if unknown_inds:
            raise ValueError(
                f"'indicators' contains unknown indicator name(s): "
                f"{', '.join(map(repr, unknown_inds))}. "
                f"Supported: {', '.join(known)}"
            )
        # A non-empty list missing any of the regime trio (see REGIME_INDICATORS
        # for why they are load-bearing) can never trade — the runtime guard
        # refuses every cycle — so fail here, not as an endless daemon retry
        # ladder. An explicit empty list keeps its documented "no indicators"
        # meaning (the ``if inds:`` above skips it).
        missing = [name for name in REGIME_INDICATORS if name not in inds]
        if missing:
            raise ValueError(
                f"'indicators' must include {', '.join(map(repr, missing))} — "
                f"the regime classifier needs a usable "
                f"{', '.join(REGIME_INDICATORS)}, and every engine cycle is "
                "refused without them"
            )
    # These three values are consumed by the Phase-1 client deep inside the run
    # (sdk_client from_config/__init__, wallet_address()); a bad value there
    # surfaces as an exit-2 traceback instead of a named config error. Validate
    # up front so an operator typo stays in the CONFIG_LOAD_ERRORS lane —
    # sdk_client's own ValueError remains the standalone defense. The legal
    # network set lives in common.constants (see its comment for why it is
    # not imported from sdk_client).
    network = config.get("network")
    if network is not None and (
        not isinstance(network, str) or network.strip().lower() not in LEGAL_NETWORKS
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
    # The engine: block's string keys stay lenient (``or`` fallbacks in
    # engine_bridge._build_engine_config); these are its two deliberate exceptions.
    eng = config.get("engine")
    if eng is not None:
        # Its one *bool* key: a quoted "false" would read truthy and silently
        # re-enable structured output (RUNBOOK §7). Validates only — no
        # write-back needed.
        if eng.get("structured_output") is not None:
            try:
                bool_from_yaml(eng["structured_output"])
            except ValueError as exc:
                raise ValueError(f"config key 'engine.structured_output': {exc}") from None
        # Its one *list* key: a bare string would be ``list()``-exploded
        # per-character by _build_engine_config into bogus analyst keys that
        # only detonate deep inside build_graph (in the daemon: an endless
        # retry ladder on a pure typo). Elements are not vocabulary-checked —
        # the analyst name set lives in the tradingagents package, which a
        # bare config load must not import.
        analysts = eng.get("selected_analysts")
        if analysts is not None and not isinstance(analysts, list):
            raise ValueError(f"'engine.selected_analysts' must be a list, got {analysts!r}")
    # A present live: block is deep-validated on EVERY load, not just by the
    # live subcommand: a staged-but-broken block (deploy workflow: edit config,
    # restart under systemd) would otherwise ride along with paper for days and
    # only fail at the moment of flipping to live — the highest-stakes moment.
    live_raw = config.get("live")
    if live_raw is not None:
        # Lazy import: live.config pulls in the risk-gate domain module, which
        # --context-only smoke runs should not pay for unless a live: block
        # exists. (Purely an import-cost choice — live.config imports nothing
        # from this module, so there is no cycle to avoid.)
        from .domains.perp.risk_gate import RiskConfig
        from .live.config import LiveConfig, validate_live_risk_consistency

        try:
            live_cfg = LiveConfig.from_dict(live_raw)
        except ValueError as exc:
            raise ValueError(f"invalid live: config — {exc}") from None
        # A staged live: block also pins its companions: risk: and its three
        # cross-checked fields must be operator-written, and the two blocks
        # must agree NOW, not at the flip-to-live moment (§24 — see
        # validate_live_risk_consistency for the vacuous-pass rationale).
        raw_risk = config.get("risk")
        if raw_risk is None:
            raise ValueError(
                "config has a live: block but no risk: block — live startup "
                "cross-checks risk: against live.safety, so a config staged "
                "for live must write risk: explicitly"
            )
        try:
            validate_live_risk_consistency(live_cfg, RiskConfig.from_dict(raw_risk), raw_risk)
        except ValueError as exc:
            raise ValueError(f"invalid risk:/live: config — {exc}") from None
    return config


def wallet_address(config: dict[str, Any]) -> str | None:
    """Return the configured wallet address, or ``None`` if unset/placeholder."""
    addr = (config.get("wallet_address") or "").strip()
    if not addr or addr == _WALLET_PLACEHOLDER:
        return None
    return addr
