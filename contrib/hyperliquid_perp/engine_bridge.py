"""Shared engine composition — config/market/engine assembly for both entry points.

Extracted verbatim from ``main.py`` (2026-08-18): the symbols ``cli.py`` had
been lazy-importing from the legacy entry point — market-context assembly
(:func:`_build_context`), the pre-LLM context guards
(:func:`_context_refusal`), the engine-config overlay
(:func:`_build_engine_config` with :class:`EngineImportError`), risk/decision
config parsing (:func:`_load_risk_decision`) and coin resolution
(:func:`_resolve_coin`) — plus what the call graph drags along: their private
helpers, and the position reads (:func:`_load_position`) both of ``main.py``'s
paths share. ``main.py`` keeps only the Phase-1 CLI shell (arg parsing,
``run_context_only`` / ``run_engine``, ``main``), so neither entry point
reaches into the other for the plumbing both share.

Top level rather than ``integration/`` on purpose: ``integration/`` is the
LLM-graph wiring seam (trading_graph), while this module composes exchange
reads + config into engine inputs — filing it there would blur that boundary.

Patch-target note: names are looked up in THIS module's globals at call time,
and neither entry point holds a module-lifetime copy of them — ``main.py``
uses module-attribute access (``engine_bridge.X``); the ``cli`` package's
modules use function-local from-imports that re-fetch the attribute on every
call. A
TOP-LEVEL from-import would break that: it copies the binding once at import
time, giving the same seam a second patch surface where a stub applied to the
wrong one is silently invisible to the other side's callers. One lookup site
means one patch surface: a test stubbing a function here, or a collaborator of
one (``HyperliquidClient``, ``_warmup_threshold``, …), always patches
``engine_bridge`` itself.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable
from datetime import datetime, timezone
from decimal import Decimal

from .common.constants import DEFAULT_CANDLE_LOOKBACK
from .config import CONFIG_LOAD_ERRORS, DOTENV_READ_ERRORS, load_config
from .domains.perp import risk_gate
from .domains.perp.context_builder import build_market_context
from .domains.perp.freshness import (
    UNUSABLE_CONTEXT_ERROR,
    ContextRefusal,
    freshness_refusal,
)
from .domains.perp.indicator_vocab import (
    REGIME_INDICATORS,
    required_candles,
    supported_indicators,
)
from .domains.perp.schema import PerpMarketContext, PerpPosition
from .domains.perp.target_decision import DecisionConfig
from .exchanges.hyperliquid.account import HyperliquidAccount
from .exchanges.hyperliquid.errors import ExchangeError
from .exchanges.hyperliquid.market_data import HyperliquidMarketData
from .exchanges.hyperliquid.sdk_client import HyperliquidClient
from .paper.config import PaperTradingConfig

logger = logging.getLogger(__name__)

# The default set is "everything the engine supports" by construction — a
# hand-kept literal here would drift silently past the loader's vocabulary
# check (which only sees operator-written lists, never this default).
_DEFAULT_INDICATORS = supported_indicators()
_DEFAULT_ANALYSTS = ["market", "social", "news"]


def _warn_dual(log_msg: str, *args: object, stderr: str) -> None:
    """One warning, both channels: the structured log and stderr.

    An operator scraping only the log stream (or only capturing stderr) must
    still see the condition — the dual-channel warnings in this module and in
    ``main.py``'s entry shells all route through here so the two channels
    cannot silently drift apart. Single-channel warnings exist and are each a
    deliberate exception, not a missed migration: the ``on_blocking_read``
    failure in :func:`_build_context` is log-only (mid-read, no operator
    moment to interrupt), :func:`_resolve_coin`'s multi-coin notice is
    stderr-only (interactive CLI feedback, not an operational event), and the
    host-vs-exchange clock-skew notice in the freshness guard
    (:func:`~.domains.perp.freshness.freshness_refusal`) is log-only (it fires
    mid-guard on every path including the daemon's, where stderr has no
    reader; the refusal message an operator does see carries the same skew
    sentence when it matters).
    """
    logger.warning(log_msg, *args)
    print(stderr, file=sys.stderr)


def _resolve_coin(coin: str | None, config: dict) -> str:
    """The coin to trade: the CLI's ``--coin`` when given, else ``config["coins"][0]``.

    Takes the bare ``--coin`` value rather than the parsed ``argparse``
    namespace: this is the shared composition layer, and the two entry points
    (``main.main``, ``cli._cmd_paper``) only ever needed the one field — taking
    the namespace coupled this module to argparse for nothing (issue #53).
    """
    if coin:
        return coin.upper()
    coins = config.get("coins") or []
    if not coins:
        raise SystemExit("no coin given and config has no 'coins' list.")
    if len(coins) > 1:
        # A single decision runs against one coin; picking coins[0] silently would
        # hide that the rest of a multi-coin config is ignored. Surface it (pass
        # --coin to choose explicitly) rather than let the selection pass unseen.
        ignored = ", ".join(str(c).upper() for c in coins[1:])
        print(
            f"warning: {len(coins)} coins configured but no --coin given; trading "
            f"only {str(coins[0]).upper()} and ignoring {ignored}.",
            file=sys.stderr,
        )
    return str(coins[0]).upper()


def _indicator_names(config: dict) -> list[str]:
    """Configured indicator names, defaulting when the key is absent or ``null``.

    A bare ``indicators:`` in YAML parses to ``None`` (not a missing key), so a
    plain ``.get(..., default)`` would return ``None`` and crash the downstream
    iteration; an explicit empty list is honoured as "no indicators".
    """
    names = config.get("indicators")
    return list(names) if names is not None else list(_DEFAULT_INDICATORS)


def _warmup_threshold(config: dict) -> int:
    """Candles the configured indicators need before they read as real signal."""
    return required_candles(_indicator_names(config))


def _context_refusal(
    ctx: PerpMarketContext, coin: str, config: dict, *, now: datetime | None = None
) -> ContextRefusal | None:
    """Why this context must not be traded on, or ``None`` if usable.

    Single source of truth for the four pre-LLM context guards — warm-up,
    fully-dead indicator set, missing/dead regime indicators
    (atr_14/ema_20/ema_50), stale feed, in that order: an
    under-warmed context legitimately has all-None indicators, so the dead-set
    diagnosis only means "the indicator engine broke" once the warm-up bar is
    cleared. Shared by the one-shot path (print + exit 1), the daemon
    provider (retry ladder -> api_failed cycle), and
    ``--context-only`` (render + warn) so the entry points can't drift apart —
    the daemon missing guards the one-shot had is exactly the drift this
    helper exists to prevent.

    The three "cannot be reasoned over" guards are written out here; the
    staleness verdict is :func:`~.domains.perp.freshness.freshness_refusal`'s,
    and ``now`` — the HOST clock, whose only use is the fallback measuring
    clock for a context that carries no exchange clock — is passed straight
    through to it. Which clock the age is measured against, and what ``now``
    is NOT used for, is documented there.
    """
    # Refuse to reason over under-warmed data: if fewer candles came back than
    # the configured indicators need, every indicator is None and the regime is
    # a guess — refuse before spending an LLM call on a hollow context.
    needed = _warmup_threshold(config)
    if ctx.candle_count < needed:
        return ContextRefusal(
            UNUSABLE_CONTEXT_ERROR,
            f"only {ctx.candle_count} candles available for {coin}, but the "
            f"configured indicators need {needed}. Refusing to run the engine on "
            "under-warmed market data.",
        )
    # Past the warm-up gate every configured indicator has enough candles, so a
    # fully-None known-indicator set is not under-warm — it means the indicator
    # engine (stockstats) failed on every column (version drift, bad frame). That
    # set is indistinguishable from a warm-up dict downstream: the regime silently
    # defaults to RANGING. Refuse it before spending an LLM call on signals that
    # are all dead.
    known = set(supported_indicators())
    computed = [v for k, v in ctx.indicators.items() if k in known]
    if computed and all(v is None for v in computed):
        return ContextRefusal(
            UNUSABLE_CONTEXT_ERROR,
            f"every technical indicator failed to compute for {coin} despite "
            f"{ctx.candle_count} candles — the indicator engine (stockstats) is likely "
            "broken or incompatible. Refusing to run the engine on a fully-dead "
            "indicator set.",
        )
    # The regime trio (see REGIME_INDICATORS for why they are load-bearing):
    # refuse whether a name was dropped from the configured indicator set
    # entirely or computed to None (stockstats failing on that column past the
    # warm-up gate — dead columns slipping past the all-dead guard above);
    # either way, do not trade on a fabricated-calm regime.
    dead = [name for name in REGIME_INDICATORS if ctx.indicators.get(name) is None]
    if dead:
        verb = "is" if len(dead) == 1 else "are"
        return ContextRefusal(
            UNUSABLE_CONTEXT_ERROR,
            f"{', '.join(dead)} {verb} unavailable for {coin} (not in the "
            f"configured indicator set, or failed to compute despite "
            f"{ctx.candle_count} candles) — the regime would silently default "
            "to RANGING, hiding a volatile or trending market. Refusing to run "
            "the engine without usable regime indicators.",
        )
    # Freshness — last of the four on purpose: those three say "this context
    # cannot be reasoned over at all", this one says "it is well-formed but out
    # of date". Why a stale context passes the three above, which clock the
    # age is measured against, and what ``now`` is for: see the guard itself.
    return freshness_refusal(ctx, coin, now=now)


def _context_refusal_error(
    ctx: PerpMarketContext, coin: str, config: dict, *, now: datetime | None = None
) -> str | None:
    """:func:`_context_refusal`'s sentence alone, or ``None`` if usable.

    The view for the two callers that only report it: ``main.run_engine``
    prints it and exits 1, ``main.run_context_only`` (the ``--context-only``
    path) renders it as a warning and exits 4. Only the daemon provider, which
    writes the durable attempt row, needs the §6.2 class alongside it.
    """
    refusal = _context_refusal(ctx, coin, config, now=now)
    return None if refusal is None else refusal.message


def _load_risk_decision(config: dict) -> tuple[risk_gate.RiskConfig, DecisionConfig] | None:
    """Parse + cross-validate the ``risk:``/``decision:``/``paper_trading:`` blocks.

    Returns ``None`` if any is invalid. A malformed block — unknown/typo'd keys,
    bad values, or a max_target margin cap that snaps below the decision grid
    (which would clamp every directional target to 0 and risk-reject it) — is
    reported as a named config error and the caller exits 1. Shared by the engine
    path and ``--context-only`` so the free smoke run validates exactly what the
    paid run will consume. The ``paper_trading`` block is parsed here for its
    validation side effect (bad fee/balance/slippage fail fast) even though PR 2
    does not yet consume it — the paper engine (PR 3) reads the same block.
    """
    try:
        risk_cfg = risk_gate.RiskConfig.from_dict(config.get("risk"))
        decision_cfg = DecisionConfig.from_dict(config.get("decision"))
        risk_gate.validate_risk_decision_config(risk_cfg, decision_cfg)
        PaperTradingConfig.from_dict(config.get("paper_trading"))
    except ValueError as exc:
        print(
            f"error: invalid risk:/decision:/paper_trading: config — {exc}. "
            "Fix the YAML block and re-run.",
            file=sys.stderr,
        )
        return None
    return risk_cfg, decision_cfg


def _build_context(
    config: dict, coin: str, *, on_blocking_read: Callable[[], None] | None = None
) -> tuple[PerpMarketContext, HyperliquidClient]:
    """Fetch market data and assemble the :class:`PerpMarketContext` for ``coin``.

    ``on_blocking_read`` is called between the network reads below. It exists for
    ONE caller — the live loop, where this runs on the single-threaded tick and
    the five reads here (constructing the client fetches perp meta, then
    snapshot, candles, the exchange clock, funding) are the longest run of
    back-to-back REST calls in the system, each riding the full
    ``network_timeout_s``. Left unrefreshed, this chain would set
    ``kill_switch._MAX_UNREFRESHED_REST_CALLS`` to its own length — four when
    that constant was last argued, five since the exchange-clock read joined
    (issue #51) — which made the
    operator advisory demand a timeout under 7.5s — and a live decision cycle has
    NO within-cycle retry, so one market read that times out fail-closes the
    cycle and re-anchors to the next 4h boundary. Refreshing between the reads
    buys the kill-switch headroom back without paying for it with a possible
    4-hour decision blackout (2026-08-01 lifecycle review).

    ``None`` for every other caller (the one-shot CLI paths), where no dead man's
    switch is being held open.
    """
    md_cfg = config.get("market_data", {})
    interval = md_cfg.get("candle_interval") or "4h"
    # ``dict.get(key, default)`` only falls back when the key is absent; a present-
    # but-null value (YAML key left blank) returns None and crashes ``int(None)`` —
    # treat null like absent (matches network_timeout_s handling in from_config).
    raw_lookback = md_cfg.get("candle_lookback", DEFAULT_CANDLE_LOOKBACK)
    lookback = int(raw_lookback) if raw_lookback is not None else DEFAULT_CANDLE_LOOKBACK
    raw_window = md_cfg.get("funding_zscore_window_days", 30)
    window_days = int(raw_window) if raw_window is not None else 30
    # Volume profile is OFF unless an operator sets a window (see the module
    # docstring of domains/perp/volume_profile.py). Same null-is-absent rule as
    # the two above; ``load_config`` has already rejected a non-integer or
    # out-of-band value, so this int() cannot raise on a loaded config.
    raw_profile_window = md_cfg.get("volume_profile_window_candles", 0)
    profile_window = int(raw_profile_window) if raw_profile_window is not None else 0
    indicator_names = _indicator_names(config)

    def _between_reads() -> None:
        """Refresh whatever the caller is holding open, between blocking reads.

        Never lets a refresh failure abort the market read it was protecting —
        the live wiring passes ``refresh_across_blocking_work``, which already
        swallows, but this must hold for any caller.
        """
        if on_blocking_read is None:
            return
        try:
            on_blocking_read()
        except Exception:  # noqa: BLE001 — a refresh miss must not fail the cycle
            logger.warning("on_blocking_read hook failed during market data build", exc_info=True)

    # Constructing the client is itself a network read (Info() auto-fetches perp
    # meta), so the refresh goes AFTER it, not only between the three explicit
    # market calls.
    client = HyperliquidClient.from_config(config)
    market = HyperliquidMarketData(client)
    _between_reads()

    print(f"Fetching {coin} market data from {client.network} (read-only)...", file=sys.stderr)
    snapshot = market.get_market_snapshot(coin)
    _between_reads()
    candles = market.get_candles(coin, interval, lookback)
    _between_reads()
    # The exchange's clock, read right after the candles it will be measured
    # against (issue #51): the candle window above is bounded by the HOST
    # clock, so it alone cannot reveal a host that runs behind. Keyless
    # (public l2Book), so --context-only gets the same check as the daemon.
    # A read that fails or answers without a timestamp propagates like the
    # other reads — fail-closed, the guard cannot measure without it.
    exchange_time = market.get_exchange_time(coin)
    # Adjacent to the read above — that adjacency is the whole point. A host
    # reading taken anywhere else (the scheduler's ``as_of``, the guard's own
    # ``datetime.now()``) is separated from the exchange's by however long the
    # surrounding REST calls took, and the difference would report that elapsed
    # time as clock skew.
    host_time_at_exchange_read = datetime.now(tz=timezone.utc)
    _between_reads()
    funding = market.get_funding_history(coin, window_days)
    _between_reads()

    ctx = build_market_context(
        coin,
        snapshot,
        candles,
        funding,
        candle_interval=interval,
        funding_window_days=window_days,
        indicator_names=indicator_names,
        exchange_time=exchange_time,
        host_time_at_exchange_read=host_time_at_exchange_read,
        volume_profile_window=profile_window,
    )
    return ctx, client


def _load_position(
    client: HyperliquidClient, addr: str | None, coin: str
) -> tuple[PerpPosition | None, Decimal, bool]:
    """Read the current position + account value for wallet ``addr``.

    Returns ``(position, account_value, ok)``. ``ok`` is ``False`` only when a
    configured wallet was looked up but the exchange call failed — letting callers
    tell a genuine flat account (``position is None`` with ``ok=True``) apart from a
    failed lookup, so a transient error never gets reported as a misleading "flat".
    With no wallet (``addr`` empty) the account is cleanly flat (decision #8: first
    round has no prior state). Callers resolve ``addr`` via :func:`wallet_address`.
    """
    if not addr:
        return None, Decimal(0), True
    try:
        account = HyperliquidAccount(client).get_account_snapshot(addr)
    except ExchangeError as exc:
        # A request/malformed-feed failure — transient or infrastructure, not an
        # account state.
        _warn_dual(
            "Hyperliquid account request failed for %s: %s",
            coin,
            exc,
            stderr=f"(account lookup skipped — request failed: {exc})",
        )
        return None, Decimal(0), False
    except ValueError as exc:
        # A structurally-unusable snapshot the schema rejects at construction — e.g. a
        # zero/negative accountValue (a margin-called or fully-withdrawn wallet) or a
        # duplicate-coin payload. This is a real *account state*, not a network failure;
        # log it distinctly so an operator isn't sent chasing a phantom outage. Caught
        # here (rather than escaping to main's last-resort handler as exit 2 "unexpected
        # error") so it reports the same clean failed lookup (ok=False -> exit 1).
        _warn_dual(
            "account snapshot rejected for %s (margin-called / empty / invalid?): %s",
            coin,
            exc,
            stderr=f"(account lookup skipped — snapshot unusable: {exc})",
        )
        return None, Decimal(0), False
    return account.position_for(coin), account.account_value, True


class EngineImportError(RuntimeError):
    """Named, operator-fixable failure importing the tradingagents engine.

    Raised only by :func:`_build_engine_config`; callers map exactly this type
    to a named exit 1, so any other ``RuntimeError`` still surfaces as an
    unexpected-error exit 2 instead of hiding behind a reassuring message.
    """


def _build_engine_config(config: dict) -> tuple[dict, list[str]]:
    """Overlay the perp ``engine`` block onto the engine's DEFAULT_CONFIG.

    ``backend_url`` stays ``None`` so the OpenRouter client uses its own default
    endpoint (``https://openrouter.ai/api/v1``). ``structured_output`` defaults
    to ``False`` here (the engine default is on) — see RUNBOOK §7.
    """
    try:
        from tradingagents.default_config import DEFAULT_CONFIG
    except ImportError as exc:
        # Deferred so --context-only stays import-light, but if the engine package is
        # missing/moved this surfaces a clear cause instead of a generic top-level
        # "unexpected error". ``exc`` rides in the message because callers print
        # only str(): a broken transitive dependency (``No module named
        # 'langchain'``) would otherwise be invisible — the chained cause never
        # reaches a traceback-printing handler on this named-exit path.
        raise EngineImportError(
            f"tradingagents.default_config.DEFAULT_CONFIG is not importable "
            f"({exc}) — is the tradingagents package (and its dependencies) "
            "installed?"
        ) from exc
    except DOTENV_READ_ERRORS as exc:
        # This is the process's first tradingagents import, and the package
        # __init__ loads the repo .env files with no read guard (unlike
        # config.load_dotenv_files, which warned and continued moments
        # earlier) — so a corrupt file (e.g. saved as UTF-16 by a bare
        # PowerShell ``>>``) detonates here, not as an ImportError.
        raise EngineImportError(
            f"importing tradingagents failed, most likely while its package "
            f"init read a repo .env file: {exc} — is the file saved as UTF-8?"
        ) from exc

    eng_cfg = config.get("engine", {})
    engine_config = dict(DEFAULT_CONFIG)
    # ``or`` (not ``.get(k, default)``) so a present-but-null/blank YAML value falls
    # back to the default instead of silently passing None into the LLM client,
    # where it would fail deep inside the engine with a non-obvious traceback.
    engine_config["llm_provider"] = eng_cfg.get("llm_provider") or "openrouter"
    engine_config["deep_think_llm"] = (
        eng_cfg.get("deep_think_llm") or engine_config["deep_think_llm"]
    )
    engine_config["quick_think_llm"] = (
        eng_cfg.get("quick_think_llm") or engine_config["quick_think_llm"]
    )
    engine_config["backend_url"] = None
    # Perp runs default structured output OFF (the engine default is on): the
    # Phase 2 target JSON contract is injected as prompt text and can only
    # survive in the gated agents' free-text answers — a *successful*
    # structured call renders only the schema's own fields, silently dropping
    # the contract and fail-closing every cycle as invalid_output (this is how
    # the 2026-07-27 model swap broke paper-BTC). ``engine.structured_output:
    # true`` stays available as an explicit escape hatch, e.g. once the
    # contract is carried by the structured schema itself. The raw passthrough
    # is type-safe: load_config already rejected any non-bool value.
    raw_structured = eng_cfg.get("structured_output")
    engine_config["structured_output"] = raw_structured if raw_structured is not None else False
    if engine_config["structured_output"]:
        # The armed escape hatch must be loud: until the structured schema
        # carries the contract, this setting fail-closes every cycle, and the
        # only other trace is the *absence* of the gate-off INFO lines. One
        # sentence drives both channels so they (and the RUNBOOK §7 search
        # anchor) can't drift apart.
        msg = (
            "engine.structured_output: true — the Phase 2 target JSON does "
            "not survive the structured render; expect invalid_output every "
            "cycle unless the schema carries the contract"
        )
        _warn_dual(msg, stderr=f"warning: {msg}")
    # ``is not None`` (not ``or``) so an explicit empty list is preserved as a
    # deliberate "no analysts" choice rather than silently replaced by the default
    # — matches the _indicator_names pattern above. A blank YAML value (None) still
    # falls back to the default.
    raw_analysts = eng_cfg.get("selected_analysts")
    selected = list(raw_analysts if raw_analysts is not None else _DEFAULT_ANALYSTS)
    return engine_config, selected


def load_config_or_exit(path: str | None) -> dict | None:
    """Parse the YAML config; on failure print the named error and return None.

    Missing/unreadable path, YAML syntax error, or failed validation — all
    operator config mistakes, all the same named exit 1 (callers translate
    ``None`` into theirs), never a traceback. The one home for the message the
    four entry points (legacy main, ``live``, ``live-smoke``, ``paper``) used
    to carry as verbatim copies, so the wording cannot drift between them.
    """
    try:
        return load_config(path)
    except CONFIG_LOAD_ERRORS as exc:
        print(f"error: invalid config — {exc}. Fix the YAML and re-run.", file=sys.stderr)
        return None
