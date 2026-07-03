"""Entry point for the Hyperliquid perp module.

Two paths:

- ``--context-only`` (Milestone A): connect to mainnet read-only, build and print
  the :class:`PerpMarketContext`. No API key, no wallet required.
- full run: build the context, drive the **unmodified** TradingAgents engine with
  that context (and the Phase 2 output-format contract) injected, parse the
  structured target JSON out of ``final_trade_decision``, run the deterministic
  RiskGate, write the audit record (raw response preserved), and print the
  outcome. Needs ``OPENROUTER_API_KEY``.

    python -m contrib.hyperliquid_perp.main --context-only --coin BTC
    python -m contrib.hyperliquid_perp.main --coin BTC
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from decimal import Decimal

from .audit.decision_log import log_target_decision
from .config import load_config, wallet_address
from .domains.perp import risk_gate
from .domains.perp.context_builder import build_market_context
from .domains.perp.indicators import required_candles, supported_indicators
from .domains.perp.prompt_context import render_market_context
from .domains.perp.schema import PerpMarketContext, PerpPosition
from .domains.perp.target_decision import (
    DecisionConfig,
    decision_format_instructions,
    parse_target_decision,
)
from .exchanges.hyperliquid.account import HyperliquidAccount
from .exchanges.hyperliquid.errors import ExchangeError
from .exchanges.hyperliquid.market_data import HyperliquidMarketData
from .exchanges.hyperliquid.sdk_client import HyperliquidClient
from .integration.trading_graph import build_graph, inject_perp_context

logger = logging.getLogger(__name__)

_DEFAULT_INDICATORS = ["rsi_14", "ema_20", "ema_50", "atr_14", "macd"]
_DEFAULT_ANALYSTS = ["market", "social", "news"]


def _warn_dual(log_msg: str, *args: object, stderr: str) -> None:
    """One warning, both channels: the structured log and stderr.

    An operator scraping only the log stream (or only capturing stderr) must
    still see the condition — every warning site in this file goes through
    here so the two channels cannot silently drift apart.
    """
    logger.warning(log_msg, *args)
    print(stderr, file=sys.stderr)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m contrib.hyperliquid_perp.main",
        description="Hyperliquid perp — Phase 2 (structured target contract + RiskGate).",
    )
    parser.add_argument(
        "--context-only",
        action="store_true",
        help="Build & print PerpMarketContext only; skip the engine (no API key needed).",
    )
    parser.add_argument(
        "--coin",
        default=None,
        help="Coin symbol, e.g. BTC. Defaults to the first coin in config.",
    )
    parser.add_argument(
        "--config",
        default=None,
        help="Path to a config YAML (defaults to hyperliquid.local.yaml, else example).",
    )
    return parser.parse_args(argv)


def _resolve_coin(args: argparse.Namespace, config: dict) -> str:
    if args.coin:
        return args.coin.upper()
    coins = config.get("coins") or []
    if not coins:
        raise SystemExit("no coin given and config has no 'coins' list.")
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
    """Candles the configured indicators need before they read as real signal.

    Single source of truth for the under-warmed-data check, shared by the engine
    path (hard abort) and ``--context-only`` (warn) so a future caller can't drift.
    """
    return required_candles(_indicator_names(config))


def _build_context(config: dict, coin: str) -> tuple[PerpMarketContext, HyperliquidClient]:
    """Fetch market data and assemble the :class:`PerpMarketContext` for ``coin``."""
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

    client = HyperliquidClient.from_config(config)
    market = HyperliquidMarketData(client)

    print(f"Fetching {coin} market data from {client.network} (read-only)...", file=sys.stderr)
    snapshot = market.get_market_snapshot(coin)
    candles = market.get_candles(coin, interval, lookback)
    funding = market.get_funding_history(coin, window_days)

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


def run_context_only(config: dict, coin: str) -> int:
    """Build and print the market context for ``coin``."""
    ctx, client = _build_context(config, coin)

    print("\n" + "=" * 64)
    print(f"PerpMarketContext - {coin}")
    print("=" * 64)
    print(render_market_context(ctx))
    print("=" * 64)

    # --context-only is a keyless diagnostic loop, so we render rather than abort —
    # but an under-warmed context has every indicator at None and a default-"ranging"
    # regime that *looks* like real data. Warn loudly so the degraded block above is
    # not mistaken for a live signal (run_engine hard-aborts on the same condition).
    needed = _warmup_threshold(config)
    if ctx.candle_count < needed:
        _warn_dual(
            "under-warmed context for %s: %d candles available, indicators need %d",
            coin,
            ctx.candle_count,
            needed,
            stderr=(
                f"warning: only {ctx.candle_count} candles available for {coin}, but the "
                f"configured indicators need {needed}. The indicators and regime above are "
                "under-warmed (degraded) — do not read them as live signal."
            ),
        )

    # Optional: if a real wallet is configured, also report the current position.
    # Only print when the lookup actually succeeded — a failed read is not a "flat".
    addr = wallet_address(config)
    position, _account_value, ok = _load_position(client, addr, coin)
    if addr and ok:
        if position is None:
            print(f"\nCurrent position: flat ({coin})")
        else:
            side = "long" if position.is_long else "short"
            print(
                f"\nCurrent position: {side} {abs(position.size)} {coin} "
                f"@ {position.entry_price} (uPnL {position.unrealized_pnl})"
            )
    return 0


def _build_engine_config(config: dict) -> tuple[dict, list[str]]:
    """Overlay the perp ``engine`` block onto the engine's DEFAULT_CONFIG.

    ``backend_url`` stays ``None`` so the OpenRouter client uses its own default
    endpoint (``https://openrouter.ai/api/v1``).
    """
    try:
        from tradingagents.default_config import DEFAULT_CONFIG
    except ImportError as exc:
        # Deferred so --context-only stays import-light, but if the engine package is
        # missing/moved this surfaces a clear cause instead of a generic top-level
        # "unexpected error".
        raise RuntimeError(
            "tradingagents.default_config.DEFAULT_CONFIG is not importable — "
            "is the tradingagents package installed?"
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
    # ``is not None`` (not ``or``) so an explicit empty list is preserved as a
    # deliberate "no analysts" choice rather than silently replaced by the default
    # — matches the _indicator_names pattern above. A blank YAML value (None) still
    # falls back to the default.
    raw_analysts = eng_cfg.get("selected_analysts")
    selected = list(raw_analysts if raw_analysts is not None else _DEFAULT_ANALYSTS)
    return engine_config, selected


def run_engine(config: dict, coin: str) -> int:
    """Full run: context -> engine -> structured target parse -> RiskGate -> log."""
    if not os.environ.get("OPENROUTER_API_KEY"):
        raise SystemExit(
            "OPENROUTER_API_KEY is not set — needed for the engine run. "
            "Use --context-only for a keyless dev loop."
        )

    ctx, client = _build_context(config, coin)
    # Refuse to reason over under-warmed data: if fewer candles came back than the
    # configured indicators need, every indicator is None and the regime is a guess
    # — abort here, before spending an LLM call on a hollow context.
    needed = _warmup_threshold(config)
    if ctx.candle_count < needed:
        print(
            f"error: only {ctx.candle_count} candles available for {coin}, but the "
            f"configured indicators need {needed}. Refusing to run the engine on "
            "under-warmed market data.",
            file=sys.stderr,
        )
        return 1
    # Past the warm-up gate every configured indicator has enough candles, so a
    # fully-None known-indicator set is not under-warm — it means the indicator
    # engine (stockstats) failed on every column (version drift, bad frame). That
    # set is indistinguishable from a warm-up dict downstream: the regime silently
    # defaults to RANGING. Refuse it the same way, before spending an LLM call on
    # signals that are all dead.
    _known = set(supported_indicators())
    _computed = [v for k, v in ctx.indicators.items() if k in _known]
    if _computed and all(v is None for v in _computed):
        print(
            f"error: every technical indicator failed to compute for {coin} despite "
            f"{ctx.candle_count} candles — the indicator engine (stockstats) is likely "
            "broken or incompatible. Refusing to run the engine on a fully-dead "
            "indicator set.",
            file=sys.stderr,
        )
        return 1
    # atr_14 is load-bearing beyond being "one missing number": classify_regime falls
    # back to RANGING (hiding a volatile market) when it is absent. Past the warm-up
    # gate there are enough candles to compute it, so a None atr_14 here means
    # stockstats failed on that column specifically — and a single dead indicator
    # slips past the all-dead guard above. Refuse the run rather than trade on a
    # fabricated-calm regime.
    if "atr_14" in ctx.indicators and ctx.indicators["atr_14"] is None:
        print(
            f"error: atr_14 failed to compute for {coin} despite {ctx.candle_count} "
            "candles — the regime would silently default to RANGING, hiding a "
            "volatile market. Refusing to run the engine without a usable ATR.",
            file=sys.stderr,
        )
        return 1
    ctx_text = render_market_context(ctx)
    # A configured-wallet lookup that fails leaves the real exposure unknown. Refuse
    # to rebalance against a guessed-flat account — and abort here, before the engine
    # run, so we don't spend an LLM call on state we can't trust.
    position, account_value, ok = _load_position(client, wallet_address(config), coin)
    if not ok:
        print(
            "error: position lookup failed — refusing to run the engine on unknown "
            "account state. Re-run once the exchange read recovers.",
            file=sys.stderr,
        )
        return 1

    # RiskGate sizes every target against net account value. A successful lookup
    # always yields account_value > 0 (a zero/negative snapshot is rejected at
    # construction and reported above as a failed lookup), so account_value == 0
    # here means no funded wallet is configured. Against zero equity every
    # directional target fail-closes to no_account_equity and no order can ever be
    # created — abort now, before the engine build and its LLM spend, rather than
    # pay for a decision the gate is guaranteed to reject. Use --context-only for a
    # keyless diagnostic run.
    if account_value <= 0:
        print(
            "error: no usable account equity (account_value = 0) — RiskGate cannot "
            "size any order, so every directional target would fail closed. Configure "
            "a funded wallet_address, or use --context-only for a keyless diagnostic "
            "run. Refusing to spend an LLM call on an unusable account state.",
            file=sys.stderr,
        )
        return 1

    # Parse the Phase 2 config blocks up front: a malformed risk:/decision: block
    # must abort here, before any LLM spend, not after the engine run. Caught as
    # a config error (exit 1, like the API-key and warm-up checks) rather than
    # falling through to main's exit-2 "unexpected error" bucket — an operator
    # typo is expected/actionable, not a bug.
    try:
        risk_cfg = risk_gate.RiskConfig.from_dict(config.get("risk"))
        decision_cfg = DecisionConfig.from_dict(config.get("decision"))
        # Cross-block check: each block is individually valid, but a max_target
        # margin cap that snaps below the decision grid would silently clamp every
        # directional target to 0 and fail closed. Reject that pairing loudly here.
        risk_gate.validate_risk_decision_config(risk_cfg, decision_cfg)
    except ValueError as exc:
        print(
            f"error: invalid risk:/decision: config — {exc}. Fix the YAML block and re-run.",
            file=sys.stderr,
        )
        return 1

    # Advertise the *effective* margin ceiling (grid max capped by the risk
    # allocation cap) so the model is never told a margin is legal that the
    # gate deterministically clamps — a clamped audit record then means the
    # risk gate genuinely intervened, not business as usual.
    output_format_text = decision_format_instructions(
        decision_cfg,
        max_pct=risk_gate.effective_max_target_margin_pct(risk_cfg, decision_cfg),
    )
    engine_config, selected_analysts = _build_engine_config(config)
    graph = build_graph(
        perp_context_text=ctx_text,
        config=engine_config,
        selected_analysts=selected_analysts,
        output_format_text=output_format_text,
    )

    trade_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    print(
        f"Running TradingAgents engine for {coin} on {trade_date} "
        f"(analysts: {', '.join(selected_analysts)})...",
        file=sys.stderr,
    )
    try:
        propagated = graph.propagate(coin, trade_date, asset_type="crypto")
    except Exception as exc:  # noqa: BLE001 — classify engine-side failures distinctly
        # ``propagate`` drives the unmodified engine (LLM calls, LangGraph state machine).
        # A failure here (provider rate-limit/timeout, an agent raising, a LangGraph error)
        # is an *engine run* failure, not a bug in this adapter — surface it with an
        # actionable message and exit 1 like every other external call, rather than letting
        # it fall through to main's last-resort handler as an opaque exit-2 "unexpected
        # error". The full traceback is still logged for a post-mortem.
        logger.exception("engine.propagate failed for %s", coin)
        print(
            f"error: engine run failed ({type(exc).__name__}: {exc}) — check the LLM "
            "provider status/credentials and retry. No decision was produced or logged.",
            file=sys.stderr,
        )
        return 1
    if not isinstance(propagated, (tuple, list)) or len(propagated) < 2:
        # ``propagate`` is the seam to the unmodified engine — the most likely place
        # for a version drift to change the return contract. A bad shape would
        # otherwise blow up as an opaque unpack ``ValueError`` in the last-resort
        # handler; name the seam instead.
        shape = len(propagated) if isinstance(propagated, (tuple, list)) else "n/a"
        print(
            f"error: engine.propagate returned an unexpected shape "
            f"({type(propagated).__name__}, len={shape}) — aborting before decision mapping",
            file=sys.stderr,
        )
        return 1
    final_state, _signal = propagated[0], propagated[1]
    if not isinstance(final_state, dict):
        # The parse seam reads ``final_state`` as a dict; a non-dict (e.g.
        # ``None`` from an engine crash) would otherwise surface as an opaque
        # ``AttributeError`` in the last-resort handler. Fail clean instead.
        print(
            f"error: engine returned a non-dict final_state ({type(final_state).__name__}) "
            "— aborting before decision mapping",
            file=sys.stderr,
        )
        return 1

    # Phase 2 contract: parse the structured target JSON out of the engine's
    # final_trade_decision. Any invalid output — no JSON, bad schema, illegal
    # cross-field combination — fails closed to maintain_current inside the
    # parse seam; the raw response is preserved for the audit record either way.
    parsed = parse_target_decision(final_state.get("final_trade_decision"), decision_cfg)
    if not parsed.is_valid:
        # Repeated contract failures are the model-drift signal alerting must
        # see: the cycle still completes fail-closed (gate + audit record
        # below), but the run exits 3 at the end so a naive scheduler can
        # alert on the exit code instead of log-scraping.
        _warn_dual(
            "engine output failed the structured-target contract for %s: %s",
            coin,
            parsed.invalid_reason,
            stderr=(
                f"warning: engine output failed the structured-target contract "
                f"({parsed.invalid_reason}) — failing closed to maintain_current."
            ),
        )

    # The deterministic RiskGate sizes and checks the target against the live
    # account state (mark-based valuation). A zero-equity account fail-closes
    # any directional target inside the gate (risk_reason=no_account_equity).
    current = risk_gate.current_position_state(position, account_value, ctx.mark_price)
    if current.leverage is not None and current.leverage != risk_cfg.leverage:
        # margin% only tracks notional when the position's real leverage matches
        # the configured risk.leverage (e.g. a manually opened 5x position under
        # ``leverage: 1``). The gate disables the rebalance deadband on this
        # mismatch so the order converges the true notional to the target — make
        # the condition visible to the operator here.
        _warn_dual(
            "position leverage %s differs from configured risk.leverage %s for %s — "
            "rebalance deadband disabled for this cycle so true exposure converges "
            "to the target",
            current.leverage,
            risk_cfg.leverage,
            coin,
            stderr=(
                f"warning: position leverage {current.leverage} != configured "
                f"risk.leverage {risk_cfg.leverage} — rebalance deadband disabled "
                "for this cycle."
            ),
        )
    result = risk_gate.evaluate(
        parsed,
        account_equity=account_value,
        current=current,
        risk=risk_cfg,
        decision_cfg=decision_cfg,
    )

    models = {
        "provider": engine_config["llm_provider"],
        "deep": engine_config["deep_think_llm"],
        "quick": engine_config["quick_think_llm"],
    }
    # Print the outcome *before* persisting it, so a later audit-write failure can
    # never make a decision the engine already produced vanish silently.
    print("\n" + "=" * 64)
    print(f"TargetDecision - {coin} (risk_action: {result.risk_action.value})")
    print("=" * 64)
    print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False))
    print("=" * 64)

    try:
        # ``prompt_hash`` covers everything this adapter injected — the market
        # context AND the output-format contract (which embeds the live margin
        # grid / min_confidence), assembled exactly as resolve_instrument_context
        # appends it — so two records with different decision:/risk: config can
        # never carry the same hash. The engine's own base instrument context is
        # engine-internal and not captured.
        _record, path = log_target_decision(
            coin=coin,
            parsed=parsed,
            risk_result=result,
            prompt=inject_perp_context("", ctx_text, output_format_text),
            models=models,
            results_dir=engine_config["results_dir"],
        )
    except (OSError, ValueError, TypeError, UnicodeError, OverflowError) as exc:
        # OSError: filesystem failure. ValueError/TypeError: a non-serializable
        # record or an unset results_dir (Path(None)). UnicodeError: a lone
        # surrogate in the engine response failing to encode on write.
        # OverflowError: a float('inf') in the record overflowing json.dump.
        # Either way the outcome was already printed above, so report the
        # persistence failure loudly rather than letting it surface as a generic
        # "fatal" exit.
        print(
            f"ERROR: audit log write failed — the decision above was NOT persisted: {exc}",
            file=sys.stderr,
        )
        return 1
    print(f"decision log written to {path}", file=sys.stderr)
    if not parsed.is_valid:
        # Exit codes: 0 = success (including healthy risk rejections), 1 =
        # config/env/engine errors, 2 = unexpected error, 3 = the model's output
        # failed the structured-target contract (cycle completed fail-closed).
        # The distinct code lets a naive scheduler alert on model drift.
        return 3
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        config = load_config(args.config)
    except ValueError as exc:
        print(f"error: invalid config — {exc}. Fix the YAML and re-run.", file=sys.stderr)
        return 1
    coin = _resolve_coin(args, config)

    try:
        if args.context_only:
            return run_context_only(config, coin)
        return run_engine(config, coin)
    except ExchangeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 — last-resort handler, never let one escape
        # Log the full traceback so an unexpected failure is diagnosable from any
        # configured log handler, then still print a one-line message to stderr and
        # exit cleanly with a distinct code rather than dumping a bare stack.
        logger.exception("unexpected error in main")
        print(f"fatal: unexpected error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
