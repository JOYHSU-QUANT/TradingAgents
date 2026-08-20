"""Shared engine composition — config/market/engine assembly for both entry points.

Extracted verbatim from ``main.py`` (2026-08-18): the symbols ``cli.py`` had
been lazy-importing from the legacy entry point — market-context assembly
(:func:`_build_context`), the pre-LLM context guards
(:func:`_context_refusal_error`), the engine-config overlay
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

import argparse
import logging
import sys
from collections.abc import Callable
from datetime import datetime, timezone
from decimal import Decimal

from .config import CONFIG_LOAD_ERRORS, DOTENV_READ_ERRORS, load_config
from .domains.perp import risk_gate
from .domains.perp.context_builder import build_market_context
from .domains.perp.indicator_vocab import (
    REGIME_INDICATORS,
    required_candles,
    supported_indicators,
)
from .domains.perp.schema import PerpMarketContext, PerpPosition
from .domains.perp.target_decision import DecisionConfig
from .exchanges.hyperliquid.account import HyperliquidAccount
from .exchanges.hyperliquid.errors import ExchangeError
from .exchanges.hyperliquid.market_data import HyperliquidMarketData, interval_to_ms
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
    moment to interrupt), and :func:`_resolve_coin`'s multi-coin notice is
    stderr-only (interactive CLI feedback, not an operational event).
    """
    logger.warning(log_msg, *args)
    print(stderr, file=sys.stderr)


def _resolve_coin(args: argparse.Namespace, config: dict) -> str:
    if args.coin:
        return args.coin.upper()
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


def _format_duration_ms(ms: int) -> str:
    """``ms`` as ``"45m 0s"`` / ``"14h 12m 30s"`` / ``"153d 4h"``, for refusal text.

    Seconds are carried because the refusal message prints an age and the limit
    it exceeded side by side: a minute-resolution format renders every age in
    the first MINUTE past the limit as the limit itself, and the message then
    reads "X is past the X limit". Seconds narrow that window to the first
    second rather than removing it — sub-second precision would cost more
    legibility than the residue is worth.

    The day form starts at two days, not one, purely for readability: an outage
    of thirty-odd hours reads better as ``"30h 0m 0s"`` than as ``"1d 6h"``,
    and the collision the seconds exist for cannot happen in either band (the
    limit is capped at three decision cycles, far below a day).
    """
    seconds, minutes = ms // 1000 % 60, ms // 60_000 % 60
    hours, days = ms // 3_600_000, ms // 86_400_000
    if ms < 3_600_000:
        return f"{minutes}m {seconds}s"
    if days < 2:
        return f"{hours}h {minutes}m {seconds}s"
    return f"{days}d {hours % 24}h"


def _utc_stamp(moment: datetime) -> str:
    """``moment`` as a UTC ISO stamp; the tz is normalized, never assumed."""
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# How old the newest candle may be, counted in candle intervals.
# ``get_candles`` drops the still-forming bar, so a healthy feed's newest CLOSED
# candle is under one interval old, and each bar the exchange fails to publish
# adds another interval: three tolerates two consecutive missing bars and
# refuses at the third. Not a config knob — a correctness bound on "is this
# still the current market?", not a tuning parameter.
_MAX_CANDLE_AGE_INTERVALS = 3
# ...but the interval is operator-configurable (1m through 1d) while the decision
# cycle is fixed, so the bar width alone would make this guard mean wildly
# different things: 3 x 1d would let 18 cycles trade through a three-day outage,
# 3 x 1m would refuse a cycle over three minutes of feed jitter. Clamp both ends
# in terms of the DECISION cadence the guard actually protects: at most three
# cycles of stale data, and never so tight that ordinary jitter refuses a cycle.
# The ceiling states three DECISION cycles, but is written out rather than
# derived from the scheduler's CYCLE_INTERVAL: importing paper.scheduler here
# would pull the whole paper engine into the keyless --context-only path (25ms
# measured) to read one timedelta. That leaves the value duplicated, so a
# drift-lock test asserts the two against each other — a changed cycle length
# fails a test instead of silently leaving this bound, and the operator-facing
# "3 x the 4h decision cycle" text, lying. (Extracting CYCLE_INTERVAL into a
# dependency-free module the way indicator_vocab holds REGIME_INDICATORS would
# remove the duplication outright; it belongs with the scheduler refactor, not
# here.)
_MAX_CANDLE_AGE_CEILING_MS = 12 * 60 * 60_000
_MAX_CANDLE_AGE_FLOOR_MS = 30 * 60_000
_CYCLE_LABEL = "4h"


def _candle_age_limit(interval_ms: int, interval: str) -> tuple[int, str]:
    """``(limit_ms, how it was derived)`` for this candle interval.

    The derivation travels with the number so a refusal message never states a
    bound whose origin the operator cannot see — the clamp is invisible in the
    number alone, and "12h" reads very differently as "3 x 4h" than as "3 x 1d,
    capped".
    """
    base = _MAX_CANDLE_AGE_INTERVALS * interval_ms
    if base > _MAX_CANDLE_AGE_CEILING_MS:
        return _MAX_CANDLE_AGE_CEILING_MS, (
            f"{_MAX_CANDLE_AGE_INTERVALS} x {interval} capped at "
            f"{_MAX_CANDLE_AGE_INTERVALS} x the {_CYCLE_LABEL} decision cycle"
        )
    if base < _MAX_CANDLE_AGE_FLOOR_MS:
        return _MAX_CANDLE_AGE_FLOOR_MS, (
            f"{_MAX_CANDLE_AGE_INTERVALS} x {interval} raised to the 30m floor"
        )
    return base, f"{_MAX_CANDLE_AGE_INTERVALS} x {interval}"


def _context_refusal_error(
    ctx: PerpMarketContext, coin: str, config: dict, *, now: datetime | None = None
) -> str | None:
    """Why this context must not be traded on, or ``None`` if usable.

    Single source of truth for the four pre-LLM context guards — warm-up,
    fully-dead indicator set, missing/dead regime indicators
    (atr_14/ema_20/ema_50), stale feed, in that order: an
    under-warmed context legitimately has all-None indicators, so the dead-set
    diagnosis only means "the indicator engine broke" once the warm-up bar is
    cleared. Shared by the one-shot path (print + exit 1), the daemon
    provider (retry-ladder ``server_error`` -> api_failed cycle), and
    ``--context-only`` (render + warn) so the entry points can't drift apart —
    the daemon missing guards the one-shot had is exactly the drift this
    helper exists to prevent.

    ``now`` is the wall clock the staleness guard measures against. The daemon
    passes the reading its own clock gave at the start of THIS attempt (NOT the
    scheduled slot, which for a retried or recovered cycle is an older and quite
    different value; each retry re-reads the clock); the one-shot callers let it
    default to
    :func:`datetime.now`. Every caller in the system builds ``ctx``
    from a live REST read (``_build_context`` is ``build_market_context``'s only
    production caller), so there is no historical-replay mode whose lagging
    ``as_of`` this would misjudge — the parameter exists to make the clock
    injectable, not to model a second time base.
    """
    # Refuse to reason over under-warmed data: if fewer candles came back than
    # the configured indicators need, every indicator is None and the regime is
    # a guess — refuse before spending an LLM call on a hollow context.
    needed = _warmup_threshold(config)
    if ctx.candle_count < needed:
        return (
            f"only {ctx.candle_count} candles available for {coin}, but the "
            f"configured indicators need {needed}. Refusing to run the engine on "
            "under-warmed market data."
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
        return (
            f"every technical indicator failed to compute for {coin} despite "
            f"{ctx.candle_count} candles — the indicator engine (stockstats) is likely "
            "broken or incompatible. Refusing to run the engine on a fully-dead "
            "indicator set."
        )
    # The regime trio (see REGIME_INDICATORS for why they are load-bearing):
    # refuse whether a name was dropped from the configured indicator set
    # entirely or computed to None (stockstats failing on that column past the
    # warm-up gate — dead columns slipping past the all-dead guard above);
    # either way, do not trade on a fabricated-calm regime.
    dead = [name for name in REGIME_INDICATORS if ctx.indicators.get(name) is None]
    if dead:
        verb = "is" if len(dead) == 1 else "are"
        return (
            f"{', '.join(dead)} {verb} unavailable for {coin} (not in the "
            f"configured indicator set, or failed to compute despite "
            f"{ctx.candle_count} candles) — the regime would silently default "
            "to RANGING, hiding a volatile or trending market. Refusing to run "
            "the engine without usable regime indicators."
        )
    # Freshness. ``ctx.as_of`` is the newest candle's close (context_builder) and
    # nothing upstream compares it to a clock: a feed that stalled — or a
    # snapshot replayed from an earlier run — yields a context whose indicators
    # all compute cleanly and whose regime reads healthy, so the three guards
    # above pass it. It merely describes the past. That same ``as_of`` becomes
    # the engine's ``trade_date`` (cli), so a stale feed also silently moves the
    # analysts' whole research window to an earlier day.
    #
    # Last of the four on purpose: those three say "this context cannot be
    # reasoned over at all", this one says "it is well-formed but out of date".
    # It is also vacuous with zero candles by construction (context_builder
    # falls back to a wall-clock ``as_of``, age zero) — the warm-up guard owns
    # that case.
    moment = now if now is not None else datetime.now(tz=timezone.utc)
    try:
        interval_ms = interval_to_ms(ctx.candle_interval)
    except ValueError as exc:
        # Unreachable via _build_context (get_candles resolves the same interval
        # before a single candle exists, so an unusable one raises there first).
        # A context that gets here anyway carries an interval nothing can
        # measure — refuse rather than skip the age check.
        return (
            f"cannot establish the age of {coin}'s market data — {exc}. Refusing "
            "to run the engine on a context whose freshness cannot be checked."
        )
    age_ms = int((moment - ctx.as_of).total_seconds() * 1000)
    limit_ms, limit_basis = _candle_age_limit(interval_ms, ctx.candle_interval)
    # The same bound, applied to a candle closing far in the FUTURE. Be precise
    # about what this can and cannot catch, because the obvious reading is
    # wrong: it does NOT detect a host clock that is simply set behind.
    # ``get_candles`` derives its window end from this SAME clock and keeps only
    # ``close_time <= end``, so a uniformly-slow clock truncates the candles by
    # the same amount it truncates ``moment`` — the age comes out ordinary and
    # neither branch fires. (Closing that gap needs a clock we do not own; the
    # signed client's ``exchange_time`` is the candidate, but ``--context-only``
    # is keyless and has no signed client — see the follow-up issue.)
    #
    # What it DOES catch is the clock JUMPING between the two readings that
    # produced these timestamps — ``moment`` and ``get_candles``' own window end
    # — from a host resuming from suspend, an NTP step, a container clock
    # resyncing; and a ``ctx`` that never came from a live fetch. Do not name a
    # direction: the two readings happen in opposite orders on the two paths
    # (the daemon reads its clock first and fetches after; the one-shot callers
    # let ``now`` default here, AFTER the fetch), so the same branch means a
    # forward jump on one and a backward jump on the other. What is common to
    # both is that the readings disagree, which is all the refusal needs to say.
    #
    # Small negatives are legitimate on the daemon path and must NOT trip it:
    # its clock reading precedes the market reads, so a boundary closing in
    # between lands slightly ahead of it. (The one-shot callers read after the
    # fetch and expect no negative at all.) Sharing the bound above keeps that
    # slack comfortably wide at every interval — the gap spans two REST calls,
    # so its 30m floor is ~30x the default network_timeout_s — instead of
    # inventing a second threshold to re-derive whenever that timeout changes.
    # (config validates network_timeout_s as a number but sets no upper bound,
    # so a deployment choosing minutes-long timeouts would need this revisited.)
    if age_ms < -limit_ms:
        return (
            f"the newest {coin} candle closes at {_utc_stamp(ctx.as_of)}, which is "
            f"{_format_duration_ms(-age_ms)} AFTER the current time "
            f"({_utc_stamp(moment)}) — more than the {_format_duration_ms(limit_ms)} "
            f"tolerance ({limit_basis}). The candle window is taken from this same "
            "clock, so a gap this size means it jumped between the two readings "
            "(suspend/resume, an NTP step, a container clock resync), or this "
            "context did not come from a live market fetch. Either way the two "
            "timestamps cannot be compared. Refusing to run the engine on a "
            "context whose age cannot be established."
        )
    if age_ms > limit_ms:
        return (
            f"the newest {coin} candle closed at {_utc_stamp(ctx.as_of)}, "
            f"{_format_duration_ms(age_ms)} before now ({_utc_stamp(moment)}) — "
            f"past the {_format_duration_ms(limit_ms)} freshness limit "
            f"({limit_basis}). Either the market data feed stopped advancing or "
            "this host's clock is ahead — the two are indistinguishable from "
            "here, and both mean this context describes a market other than the "
            "current one. Refusing to run the engine on market data this old."
        )
    return None


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
    the four reads here (constructing the client fetches perp meta, then
    snapshot, candles, funding) are the longest run of back-to-back REST calls in
    the system, each riding the full ``network_timeout_s``. Left unrefreshed that
    chain set ``kill_switch._MAX_UNREFRESHED_REST_CALLS`` to 4, which made the
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
    raw_lookback = md_cfg.get("candle_lookback", 200)
    lookback = int(raw_lookback) if raw_lookback is not None else 200
    raw_window = md_cfg.get("funding_zscore_window_days", 30)
    window_days = int(raw_window) if raw_window is not None else 30
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
