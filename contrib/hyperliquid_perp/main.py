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

The plumbing this shell and the subcommand CLI share — context build, the
pre-LLM guards, the engine-config overlay — lives in :mod:`.engine_bridge`;
this module keeps only what is legacy-entry-point specific.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone

# Module import + attribute access, NOT a from-import: a from-import copies
# each binding into this module, giving every shared seam a second patch
# surface — a test stubbing engine_bridge._build_context would silently miss
# the copy run_engine calls (and vice versa). Attribute access keeps ONE
# lookup site, so patches on the defining module are seen by every caller.
# (The same prescription the cli decomposition plan applies to cli.py.)
from . import engine_bridge
from .audit.decision_log import log_target_decision
from .config import dotenv_diagnosis, load_dotenv_files, wallet_address
from .domains.perp import risk_gate
from .domains.perp.prompt_context import render_market_context
from .domains.perp.target_decision import (
    decision_format_instructions,
    parse_target_decision,
)
from .exchanges.hyperliquid.errors import ExchangeError
from .integration.trading_graph import build_graph, inject_perp_context

logger = logging.getLogger(__name__)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m contrib.hyperliquid_perp.main",
        description="Hyperliquid perp — Phase 2 (structured target contract + RiskGate).",
    )
    parser.add_argument(
        "--context-only",
        action="store_true",
        help=(
            "Build & print PerpMarketContext only; skip the engine (no API key "
            "needed). Exits 4 when the rendered context is one the engine "
            "would refuse (degraded state), so a preflight can gate on it."
        ),
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


def run_context_only(config: dict, coin: str) -> int:
    """Build and print the market context for ``coin``.

    Exit codes follow the repo's probe convention (validate 0/1/4,
    ``safe-mode --status`` 0/4): 0 = healthy context, 1 = config error,
    4 = the command succeeded but the rendered context is one the engine
    would refuse — so a keyless deploy preflight can gate on the code
    instead of parsing stderr.
    """
    # The parsed configs aren't consumed here — the validation runs purely for
    # its named exit-1 side effect, before any network fetch (see the docstring
    # for why the smoke run validates at all).
    if engine_bridge._load_risk_decision(config) is None:
        return 1
    ctx, client = engine_bridge._build_context(config, coin)

    print("\n" + "=" * 64)
    print(f"PerpMarketContext - {coin}")
    print("=" * 64)
    print(render_market_context(ctx))
    print("=" * 64)

    # Keyless diagnostic loop: render rather than abort, but warn with the same
    # shared guard the trading paths refuse on — a refused context *looks* like
    # real data (a plausible default-"ranging" regime, or prices and indicators
    # that are internally consistent but describe a market hours or days old).
    refusal = engine_bridge._context_refusal_error(ctx, coin, config)
    if refusal is not None:
        engine_bridge._warn_dual(
            "degraded context: %s",
            refusal,
            stderr=(
                f"warning: {refusal} The context above is rendered for "
                "diagnosis only — do not read it as live signal."
            ),
        )

    # Optional: if a real wallet is configured, also report the current position.
    # Only print when the lookup actually succeeded — a failed read is not a "flat".
    addr = wallet_address(config)
    position, _account_value, ok = engine_bridge._load_position(client, addr, coin)
    if addr and ok:
        if position is None:
            print(f"\nCurrent position: flat ({coin})")
        else:
            side = "long" if position.is_long else "short"
            print(
                f"\nCurrent position: {side} {abs(position.size)} {coin} "
                f"@ {position.entry_price} (uPnL {position.unrealized_pnl})"
            )
    # After the full render + optional position block, so the diagnostic output
    # is never truncated by the degraded verdict.
    return 4 if refusal is not None else 0


def run_engine(config: dict, coin: str) -> int:
    """Full run: context -> engine -> structured target parse -> RiskGate -> log."""
    if not os.environ.get("OPENROUTER_API_KEY"):
        raise SystemExit(
            "OPENROUTER_API_KEY is not set — needed for the engine run. "
            "Use --context-only for a keyless dev loop. "
            f"({dotenv_diagnosis('OPENROUTER_API_KEY')}.)"
        )

    ctx, client = engine_bridge._build_context(config, coin)
    # All four pre-LLM context guards (warm-up, fully-dead indicator set,
    # missing/dead regime indicators, stale feed) live in
    # _context_refusal_error, shared with the daemon provider.
    refusal = engine_bridge._context_refusal_error(ctx, coin, config)
    if refusal is not None:
        print(f"error: {refusal}", file=sys.stderr)
        return 1
    ctx_text = render_market_context(ctx)
    # A configured-wallet lookup that fails leaves the real exposure unknown. Refuse
    # to rebalance against a guessed-flat account — and abort here, before the engine
    # run, so we don't spend an LLM call on state we can't trust.
    position, account_value, ok = engine_bridge._load_position(client, wallet_address(config), coin)
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
    # directional target is risk-rejected (no_account_equity) and no order can ever
    # be created — abort now, before the engine build and its LLM spend, rather than
    # pay for a decision the gate is guaranteed to reject. Use --context-only for a
    # keyless diagnostic run.
    if account_value <= 0:
        print(
            "error: no usable account equity (account_value = 0) — RiskGate cannot "
            "size any order, so every directional target would be risk-rejected "
            "(no_account_equity). Configure a funded wallet_address, or use "
            "--context-only for a keyless diagnostic run. Refusing to spend an LLM "
            "call on an unusable account state.",
            file=sys.stderr,
        )
        return 1

    # Named config error (exit 1, like the API-key and warm-up checks), not
    # main's exit-2 "unexpected error" bucket — an operator typo is expected,
    # not a bug; and it must abort here, before any LLM spend.
    cfgs = engine_bridge._load_risk_decision(config)
    if cfgs is None:
        return 1
    risk_cfg, decision_cfg = cfgs

    # Advertise the *effective* margin ceiling (grid max capped by the risk
    # allocation cap) so the model is never told a margin is legal that the
    # gate deterministically clamps — a clamped audit record then means the
    # risk gate genuinely intervened, not business as usual.
    output_format_text = decision_format_instructions(
        decision_cfg,
        max_pct=risk_gate.effective_max_target_margin_pct(risk_cfg, decision_cfg),
    )
    try:
        engine_config, selected_analysts = engine_bridge._build_engine_config(config)
    except engine_bridge.EngineImportError as exc:
        # Operator-fixable environment error — named exit 1, not main's exit-2
        # "unexpected error" bucket; see _build_engine_config for the causes.
        print(f"error: {exc}", file=sys.stderr)
        return 1
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
        engine_bridge._warn_dual(
            "engine output failed the structured-target contract for %s: %s",
            coin,
            parsed.invalid_reason,
            stderr=(
                f"warning: engine output failed the structured-target contract "
                f"({parsed.invalid_reason}) — failing closed to maintain_current."
            ),
        )

    # The deterministic RiskGate sizes and checks the target against the live
    # account state (mark-based valuation). A zero-equity account risk-rejects
    # any directional target inside the gate (risk_reason=no_account_equity).
    current = risk_gate.current_position_state(position, account_value, ctx.mark_price)
    if current.leverage is not None and current.leverage != risk_cfg.leverage:
        # margin% only tracks notional when the position's real leverage matches
        # the configured risk.leverage (e.g. a manually opened 5x position under
        # ``leverage: 1``). The gate disables the rebalance deadband on this
        # mismatch so any rebalance this cycle converges the true notional to the
        # target — make the condition visible to the operator here. (Warned on
        # every mismatched cycle, even when the decision ends up producing no
        # order — the mismatch itself is the operator-actionable state.)
        engine_bridge._warn_dual(
            "position leverage %s differs from configured risk.leverage %s for %s — "
            "rebalance deadband disabled for this cycle",
            current.leverage,
            risk_cfg.leverage,
            coin,
            stderr=(
                f"warning: position leverage {current.leverage} != configured "
                f"risk.leverage {risk_cfg.leverage} — rebalance deadband disabled "
                "for this cycle."
            ),
        )
    if current.side is not None and current.margin_pct is None:
        # A sized position with no usable margin_used (degraded account read):
        # the gate cannot evaluate the rebalance deadband, so a same-side
        # rebalance skips it this cycle (the zero-delta check and the resize
        # confidence bar still apply). Same operator-visibility
        # contract as the leverage-mismatch warning above — warned every cycle
        # the degraded read persists.
        engine_bridge._warn_dual(
            "position margin_used unusable for %s — rebalance deadband skipped this cycle",
            coin,
            stderr=(
                "warning: position margin_used is unusable — rebalance deadband "
                "skipped for this cycle."
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
            mark_price=ctx.mark_price,
            account_equity=account_value,
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
    # Before the OPENROUTER_API_KEY check in run_engine: the engine package
    # that would load the .env files itself is imported lazily, after that
    # check. Idempotent when already called by the subcommand CLI's main().
    load_dotenv_files()
    args = _parse_args(argv)
    config = engine_bridge.load_config_or_exit(args.config)
    if config is None:
        return 1
    coin = engine_bridge._resolve_coin(args, config)

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
