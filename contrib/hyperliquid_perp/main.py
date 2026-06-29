"""Entry point for the Hyperliquid perp module.

Two paths:

- ``--context-only`` (Milestone A): connect to mainnet read-only, build and print
  the :class:`PerpMarketContext`. No API key, no wallet required.
- full run (Milestone B): build the context, drive the **unmodified** TradingAgents
  engine with that context injected, map the engine's rating into a
  :class:`PerpTradeDecision`, write the audit log, and print the decision. Needs
  ``OPENROUTER_API_KEY``.

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

from .audit.decision_log import log_decision
from .config import load_config, wallet_address
from .domains.perp.context_builder import build_market_context
from .domains.perp.decision import Intent
from .domains.perp.indicators import required_candles, supported_indicators
from .domains.perp.prompt_context import render_market_context
from .domains.perp.schema import PerpMarketContext, PerpPosition
from .exchanges.hyperliquid.account import HyperliquidAccount
from .exchanges.hyperliquid.errors import ExchangeError
from .exchanges.hyperliquid.market_data import HyperliquidMarketData
from .exchanges.hyperliquid.sdk_client import HyperliquidClient
from .integration.decision_adapter import DecisionAdapter, RatingSource
from .integration.trading_graph import build_graph

logger = logging.getLogger(__name__)

_DEFAULT_INDICATORS = ["rsi_14", "ema_20", "ema_50", "atr_14", "macd"]
_DEFAULT_ANALYSTS = ["market", "social", "news"]


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m contrib.hyperliquid_perp.main",
        description="Hyperliquid perp — Phase 1 (data + context + engine).",
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
        # Emit to the structured log as well as stderr: an operator capturing only
        # stdout (or scraping the log stream) would otherwise never see that the
        # position read failed and the run is proceeding without it.
        logger.warning("account lookup failed for %s: %s", coin, exc)
        print(f"(account lookup skipped: {exc})", file=sys.stderr)
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
        # Emit to the structured log as well as stderr (mirroring _load_position): an
        # operator scraping only the log stream would otherwise never see that the
        # rendered context is under-warmed and must not be read as live signal.
        logger.warning(
            "under-warmed context for %s: %d candles available, indicators need %d",
            coin,
            ctx.candle_count,
            needed,
        )
        print(
            f"warning: only {ctx.candle_count} candles available for {coin}, but the "
            f"configured indicators need {needed}. The indicators and regime above are "
            "under-warmed (degraded) — do not read them as live signal.",
            file=sys.stderr,
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
    from tradingagents.default_config import DEFAULT_CONFIG

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
    """Full Phase-1 run: context -> engine -> adapter -> decision -> log."""
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
    # defaults to RANGING and the ATR stop-loss is disabled. Refuse it the same way,
    # before spending an LLM call on signals that are all dead.
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

    engine_config, selected_analysts = _build_engine_config(config)
    graph = build_graph(
        perp_context_text=ctx_text,
        config=engine_config,
        selected_analysts=selected_analysts,
    )

    trade_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    print(
        f"Running TradingAgents engine for {coin} on {trade_date} "
        f"(analysts: {', '.join(selected_analysts)})...",
        file=sys.stderr,
    )
    propagated = graph.propagate(coin, trade_date, asset_type="crypto")
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
        # ``to_perp_decision`` indexes ``final_state`` as a dict; a non-dict (e.g.
        # ``None`` from an engine crash) would otherwise surface as an opaque
        # ``AttributeError`` in the last-resort handler. Fail clean instead.
        print(
            f"error: engine returned a non-dict final_state ({type(final_state).__name__}) "
            "— aborting before decision mapping",
            file=sys.stderr,
        )
        return 1

    adapter = DecisionAdapter(ctx, position, account_value, config.get("adapter"))
    # ``decide`` returns the rating + rating_source from the *same* parse that built
    # the decision, so the audit log is stamped from a single source of truth — no
    # second parse here with a separate sentinel that could drift. PARSE_FALLBACK
    # (non-empty output, no recognized rating) and DEFAULT (empty final_trade_decision,
    # e.g. the engine crashed before populating the key) are both non-decisions: abort
    # the round fail-closed rather than emit an actionable Hold — against a live
    # position a Hold would silently mask a would-be CLOSE, and persisting it would
    # record an engine failure as if it were a deliberate decision. Only EXPLICIT
    # proceeds to logging.
    decision, rating, rating_source = adapter.decide(final_state)
    if rating_source == RatingSource.PARSE_FALLBACK:
        print(
            "error: engine returned a non-empty final_trade_decision with no "
            "recognized rating — aborting the round rather than acting on a malformed "
            "response. No decision was produced or logged.",
            file=sys.stderr,
        )
        return 1
    if rating_source == RatingSource.DEFAULT:
        # An empty final_trade_decision is indistinguishable from an engine that
        # crashed/never populated the key — a real Hold emits the word "Hold". Abort
        # fail-closed (mirroring PARSE_FALLBACK) rather than persist a Hold that would
        # freeze a live position on what may be an engine failure.
        print(
            "error: engine returned an empty final_trade_decision — aborting the round "
            "rather than persisting a Hold that may mask an engine failure (a real Hold "
            "emits the word 'Hold'). No decision was produced or logged.",
            file=sys.stderr,
        )
        return 1
    # A zero/unknown account value (no wallet configured, or a genuinely empty account)
    # cannot size a position. Refuse to act on a non-Hold decision rather than emit an
    # OPEN/REDUCE/CLOSE computed against $0 of net value; a Hold is harmless and still
    # logged for the audit trail.
    if account_value <= 0 and decision.intent != Intent.HOLD:
        print(
            f"error: account value is {account_value} (no wallet configured or an empty "
            f"account) — refusing to act on a '{decision.intent.value}' decision sized "
            "against a zero account. No decision was logged.",
            file=sys.stderr,
        )
        return 1
    models = {
        "provider": engine_config["llm_provider"],
        "deep": engine_config["deep_think_llm"],
        "quick": engine_config["quick_think_llm"],
    }
    # Print the decision *before* persisting it, so a later audit-write failure can
    # never make a decision the engine already produced vanish silently.
    print("\n" + "=" * 64)
    print(f"PerpTradeDecision - {coin} (engine rating: {rating})")
    print("=" * 64)
    print(json.dumps(decision.to_dict(), indent=2, ensure_ascii=False))
    print("=" * 64)

    try:
        _record, path = log_decision(
            coin=coin,
            decision=decision,
            prompt=ctx_text,
            models=models,
            rating=rating,
            rating_source=rating_source,
            results_dir=engine_config["results_dir"],
        )
    except (OSError, ValueError, TypeError, UnicodeError, OverflowError) as exc:
        # OSError: filesystem failure. ValueError/TypeError: a non-serializable
        # record or an unset results_dir (Path(None)). UnicodeError: a lone
        # surrogate in the engine rationale failing to encode on write.
        # OverflowError: a float('inf') in the record overflowing json.dump.
        # Either way
        # the decision was already printed above, so report the persistence failure
        # loudly rather than letting it surface as a generic "fatal" exit.
        print(
            f"ERROR: audit log write failed — the decision above was NOT persisted: {exc}",
            file=sys.stderr,
        )
        return 1
    print(f"decision log written to {path}", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    config = load_config(args.config)
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
