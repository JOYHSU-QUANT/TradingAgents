"""Shared engine composition — config/market/engine assembly for both entry points.

Extracted verbatim from ``main.py`` (2026-08-18): the symbols ``cli.py`` had
been lazy-importing from the legacy entry point — market-context assembly
(:func:`_build_context`), the engine-config overlay
(:func:`_build_engine_config` with :class:`EngineConfigError` and its
:class:`EngineImportError` subclass), risk/decision
config parsing (:func:`_load_risk_decision`) and coin resolution
(:func:`_resolve_coin`) — plus what the call graph drags along: their private
helpers, and the position reads (:func:`_load_position`) both of ``main.py``'s
paths share. ``main.py`` keeps only the Phase-1 CLI shell (arg parsing,
``run_context_only`` / ``run_engine``, ``main``), so neither entry point
reaches into the other for the plumbing both share. The pre-LLM context
guards came along too and have since moved down to
:mod:`.domains.perp.context_guards` (issue #122) — they are domain logic, and
the entry points call them there, not through this module.

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
one (``HyperliquidClient``, ``HyperliquidMarketData``, …), always patches
``engine_bridge`` itself. The same rule is why the context guards, which
moved to ``context_guards``, are NOT re-exported from here: they are patched
on their own module, and a second binding here would be the second surface.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable
from datetime import datetime, timezone
from decimal import Decimal

from .common.config_coercion import int_from_yaml
from .config import CONFIG_LOAD_ERRORS, DOTENV_READ_ERRORS, load_config
from .domains.perp import risk_gate
from .domains.perp.context_builder import build_market_context
from .domains.perp.indicator_vocab import indicator_names
from .domains.perp.marginal_cost import PositionInputs
from .domains.perp.market_data_config import MarketDataConfig
from .domains.perp.schema import PerpMarketContext, PerpPosition
from .domains.perp.target_decision import DecisionConfig
from .exchanges.hyperliquid.account import HyperliquidAccount
from .exchanges.hyperliquid.errors import ExchangeError
from .exchanges.hyperliquid.market_data import HyperliquidMarketData
from .exchanges.hyperliquid.sdk_client import HyperliquidClient
from .paper.config import PaperTradingConfig

logger = logging.getLogger(__name__)

_DEFAULT_ANALYSTS = ("market", "social", "news")

# Perp default completion cap. Perp runs are never uncapped — a missing cap
# through a gateway is a deterministic 400 on some upstreams (#177). Chosen
# against the non-thinking deep-think model this ships with; the cap counts
# reasoning tokens too, so a thinking model needs an explicit raise. Whether
# it binds IS measured (issue #182, ``cli/_provider``): every engine run logs
# its per-call output tokens against this cap and writes a ``.usage.json``
# beside the input payload; a bound cap on the decision call is recorded as
# ``truncated_output`` (not a plain ``invalid_output``), and a bound cap on
# an analyst call logs a WARNING naming the node. The effective value is
# logged at build time so the number that actually applied is recoverable.
_DEFAULT_MAX_COMPLETION_TOKENS = 8192


def _resolve_completion_cap(yaml_value: object, env_value: object) -> tuple[int, str]:
    """Resolve the effective completion cap, and name where it came from.

    ``load_config`` validates the YAML key, but ``TRADINGAGENTS_MAX_TOKENS``
    reaches here unchecked — env overrides are coerced against the type of the
    engine default, which is ``None``, so any string rides through. Left to
    the graph, a junk value raises inside ``build_graph`` on every cycle:
    outside the retry classification, so the daemon logs an unclassified
    ``api_failed``, keeps running, and holds the position on SL/TP alone —
    the #177 stall shape, relocated from the vendor to the config. Checking
    here fails the daemon at startup instead, in the same operator-fixable
    lane as a bad YAML value.
    """
    # ``is None``/blank, not falsiness: an explicit 0 must reach the rejection
    # below (this cap deliberately has no "off" sentinel — off IS the bug),
    # not fall silently through to the next source.
    for value, source in (
        (yaml_value, "engine.max_completion_tokens"),
        (env_value, "TRADINGAGENTS_MAX_TOKENS"),
    ):
        if value is None or value == "":
            continue
        try:
            cap = int_from_yaml(value)
        except ValueError as exc:
            raise EngineConfigError(f"config key '{source}': {exc}") from None
        if cap <= 0:
            raise EngineConfigError(
                f"config key '{source}': expected a positive integer, got {value!r}"
            )
        return cap, source
    return _DEFAULT_MAX_COMPLETION_TOKENS, "perp default"


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
    config: dict,
    coin: str,
    *,
    on_blocking_read: Callable[[], None] | None = None,
    position: PositionInputs | None,
) -> tuple[PerpMarketContext, HyperliquidClient]:
    """Fetch market data and assemble the :class:`PerpMarketContext` for ``coin``.

    ``on_blocking_read`` is called between the network reads below. It exists for
    ONE caller — the live loop, where this runs on the single-threaded tick and
    the five reads here (constructing the client fetches perp meta, then
    snapshot, the exchange clock, candles, funding) are the longest run of
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

    ``position`` is handed straight to the builder, which prices the prompt's
    ``Position:`` section from it (issue #134). REQUIRED, with no default, for
    the same reason the builder requires it — and it matters MORE here: this,
    not the builder, is the composition seam callers actually reach for, so a
    default would put the builder's guard permanently out of reach. Forgetting
    it would cost a silently position-blind prompt and a ``context_shape``
    quietly missing its ``position`` token, with nothing raising. The one-shot
    CLI writes ``position=None`` and means it: it has no local books, and
    reads the venue's own position separately to print UNDER the rendered
    context, deliberately outside the prompt (see
    ``target_decision.decision_format_instructions`` for why that lane stays
    blind).
    """
    # The same parse ``load_config`` already ran on this block, so on a loaded
    # config it cannot raise; absent or blank keys take the field defaults.
    market_data = MarketDataConfig.from_dict(config.get("market_data"))
    indicators = indicator_names(config)

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
    # The exchange's clock, read BEFORE the two windows it bounds (issues #51,
    # #124): both fetches below cut their window at this value, so a host
    # clock that runs behind truncates nothing and one that runs ahead admits
    # no bar the exchange has not closed. Before, not after — a bar that
    # closes during the candle fetch is then simply outside the window,
    # whereas a clock read after the fetch would pass that bar's close_time
    # while the response carried its OHLCV as captured before the close. The
    # freshness guard measures the candles' age against this same reading.
    # Keyless (public l2Book), so --context-only gets the same clock as the
    # daemon. A read that fails or answers without a timestamp propagates like
    # the other reads — fail-closed, nothing below can cut a window without it.
    exchange_time = market.get_exchange_time(coin)
    # Adjacent to the read above — that adjacency is the whole point. A host
    # reading taken anywhere else (the scheduler's ``as_of``, the guard's own
    # ``datetime.now()``) is separated from the exchange's by however long the
    # surrounding REST calls took, and the difference would report that elapsed
    # time as clock skew.
    host_time_at_exchange_read = datetime.now(tz=timezone.utc)
    _between_reads()
    candles = market.get_candles(
        coin, market_data.candle_interval, market_data.candle_lookback, end=exchange_time
    )
    _between_reads()
    funding = market.get_funding_history(
        coin, market_data.funding_zscore_window_days, end=exchange_time
    )
    _between_reads()

    ctx = build_market_context(
        coin,
        snapshot,
        candles,
        funding,
        market_data=market_data,
        indicator_names=indicators,
        exchange_time=exchange_time,
        position=position,
        host_time_at_exchange_read=host_time_at_exchange_read,
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


class EngineConfigError(RuntimeError):
    """Named, operator-fixable failure building the engine's config.

    The base of the setup errors the CLI lanes treat as "the operator can fix
    this and restart": every one of them means the engine could not be brought
    up from the environment as it stands, and none of them is an adapter bug.
    Catching the base (rather than each subclass) is what keeps a new cause
    from silently falling through to main's exit-2 "unexpected error" bucket —
    over a live position that bucket kills the process, taking SL/TP
    protection with it.
    """


class EngineImportError(EngineConfigError):
    """Named, operator-fixable failure importing the tradingagents engine.

    Raised only by :func:`_build_engine_config`. Callers catch the
    :class:`EngineConfigError` base, not this type — a new sibling cause needs
    no new handler — and any ``RuntimeError`` outside that base still surfaces
    as an unexpected-error exit 2 instead of hiding behind a reassuring
    message.
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
    # Cap precedence: YAML > TRADINGAGENTS_MAX_TOKENS (already overlaid onto
    # DEFAULT_CONFIG, like every sibling env knob) > perp default. ``or``
    # matches the string keys above: present-but-null falls through, never to
    # an uncapped request.
    # The key's ABSENCE means the engine predates the cap (a stale
    # site-packages ``tradingagents`` shadowing the checkout — PR #109), and
    # that engine's _get_provider_kwargs would forward nothing: the request
    # goes out uncapped and 400s, which is #177 verbatim. Refusing by name
    # beats both a bare KeyError (exit 2 "unexpected error") and, worse,
    # logging a cap that was never sent.
    if "max_tokens" not in engine_config:
        raise EngineConfigError(
            "the imported tradingagents engine has no 'max_tokens' config key, "
            "so no completion cap can be applied — a run would go out uncapped "
            "(issue #177). Is a stale tradingagents shadowing this checkout?"
        )
    engine_config["max_tokens"], cap_source = _resolve_completion_cap(
        eng_cfg.get("max_completion_tokens"), engine_config["max_tokens"]
    )
    # The effective cap is not derivable from any one file (YAML can shadow an
    # env var set on the host, and both can be absent), and a cap that binds
    # is invisible downstream — it presents as invalid_output, not an error.
    # One line at startup so "which number actually applied, and from where"
    # is answerable from the log.
    logger.info(
        "engine completion cap: %d tokens (from %s)",
        engine_config["max_tokens"],
        cap_source,
    )
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
    # — matches ``indicator_vocab.indicator_names``. A blank YAML value (None) still
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
