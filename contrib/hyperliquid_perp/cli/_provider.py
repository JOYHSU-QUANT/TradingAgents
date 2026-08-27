"""Production seams: funding-rate history + the AI decision provider."""

from __future__ import annotations

import hashlib
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


# Version stamp for the ai_inputs.prompt_version column: bump when the injected
# context/format contract changes shape (the payload hash tracks content).
# v4 (2026-08-27): the context gains the ``Position:`` section — the account's
# own position and the round-trip cost of every legal move
# (``domains/perp/marginal_cost.py``). The format block is unchanged.
PROMPT_VERSION = "phase2-target-v4"


class _HistoryFundingSource:
    """Funding rates from the public fundingHistory endpoint (execution §6.5).

    Serves the engine's hourly settlements and the pending-event backfill
    (restart + every cycle boundary). Responses are cached briefly so a
    backfill loop over many pending hours does not re-fetch per event; the
    fetch window widens to cover however old the requested settlement is, so a
    long-pending event can always resolve. A missing hour returns ``None``
    (the caller records/keeps a ``pending`` event — never a fabricated rate).
    """

    _MIN_WINDOW_DAYS = 7
    _CACHE_TTL_SECONDS = 900
    # After this many consecutive fetch failures the log escalates to ERROR: a
    # chronic integration break (auth, endpoint drift) must read differently
    # from the ordinary "rate not published yet" warning it otherwise mimics —
    # events would pile up pending forever behind an easy-to-miss line.
    _FAILURE_ESCALATION_THRESHOLD = 3

    def __init__(self, market) -> None:
        self._market = market
        # coin -> (fetched_at_monotonic, window_days_fetched, {hour: rate})
        self._cache: dict[str, tuple[float, int, dict[datetime, object]]] = {}
        self._consecutive_failures: dict[str, int] = {}

    def rate_at(self, coin: str, funding_timestamp: datetime):
        hour = funding_timestamp.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
        age = datetime.now(timezone.utc) - hour
        needed_days = max(self._MIN_WINDOW_DAYS, age.days + 2)
        cached = self._cache.get(coin)
        if (
            cached is None
            or time.monotonic() - cached[0] > self._CACHE_TTL_SECONDS
            or cached[1] < needed_days
        ):
            try:
                points = self._market.get_funding_history(coin, needed_days)
            except Exception as exc:  # noqa: BLE001 — a rate fetch failure means "pending"
                failures = self._consecutive_failures.get(coin, 0) + 1
                self._consecutive_failures[coin] = failures
                log = (
                    logger.error
                    if failures >= self._FAILURE_ESCALATION_THRESHOLD
                    else logger.warning
                )
                log(
                    "funding history fetch failed for %s (%d consecutive): %s",
                    coin,
                    failures,
                    exc,
                )
                return None
            self._consecutive_failures.pop(coin, None)
            by_hour = {}
            for point in points:
                stamp = datetime.fromtimestamp(point.time / 1000, tz=timezone.utc)
                by_hour[stamp.replace(minute=0, second=0, microsecond=0)] = point.rate
            cached = (time.monotonic(), needed_days, by_hour)
            self._cache[coin] = cached
        return cached[2].get(hour)


class _EngineDecisionProvider:
    """Production :class:`~.paper.scheduler.DecisionProvider`: the TradingAgents engine.

    ``build_input`` fetches market data and persists the full payload JSON
    (phase2-data §5: SQLite keeps summary + path + hash); ``request_decision``
    drives the unmodified engine and parses the structured target. External
    failures are classified into the §6.2 retry vocabulary and raised as
    :class:`RetryableDecisionError`; contract violations are NOT errors — they
    come back as an invalid ``ParsedDecision`` (fail-closed downstream).

    NOT a pure function of its argument (PR 6 hazard): ``request_decision``
    reads ``_context_text`` / ``_format_text`` that the LAST ``build_input``
    call stashed on the instance, not fields of ``decision_input`` — and the
    one shared instance is handed to both the background worker thread and the
    main-thread driver. Today this is safe only because the driver's busy-gate
    serializes ``build_input() → submit()`` strictly. Any future re-send path
    (a within-cycle retry ladder, a replay harness) MUST re-run ``build_input``
    immediately before each ``request_decision`` — or first fold the prompt
    texts into the decision-input type — else it sends a STALE cycle's prompt
    while the audit trail records the fresh input.
    """

    # Class-level defaults so an instance built without __init__ (the tests use
    # object.__new__ to skip the engine import) still answers the attributes —
    # a missing hook must degrade to "no refresh" / "no position section",
    # never to AttributeError inside build_input, which the §6.2 classifier
    # would relabel as an API failure.
    _on_blocking_read = None
    _position_source = None

    def __init__(
        self,
        config: dict,
        *,
        risk_cfg,
        decision_cfg,
        payload_dir: Path,
        on_blocking_read=None,
        position_source=None,
    ) -> None:
        from ..engine_bridge import _build_engine_config

        self._config = config
        self._risk = risk_cfg
        self._decision = decision_cfg
        self._payload_dir = payload_dir
        # Live only: ``build_input`` runs on the single-threaded tick and makes
        # the longest unrefreshed REST chain in the system, so the live wiring
        # passes a kill-switch refresh here. ``None`` for paper and the one-shot
        # CLI paths, which hold no dead man's switch (2026-08-01 lifecycle review).
        self._on_blocking_read = on_blocking_read
        # ``() -> BookPosition | None`` — the run's books, read at build time
        # (``paper.position_facts.read_book_position``, bound by the paper and
        # live wirings). ``None`` leaves the context position-blind and the
        # prompt without its ``Position:`` section (prompt v4).
        self._position_source = position_source
        # The fill-cost parameters the position section prices with: the
        # ``paper_trading.execution`` block, parsed ONCE here (the startup
        # validator in engine_bridge._load_risk_decision already rejected a
        # malformed one). Parsed for every mode, live included — the live run
        # advertises the paper fill model's costs, the only fee model the
        # config carries, and the prompt says "assumptions" for that reason.
        from ..paper.config import PaperTradingConfig

        self._execution = PaperTradingConfig.from_dict(config.get("paper_trading")).execution
        self._engine_config, self._analysts = _build_engine_config(config)

    def _attach_position(self, ctx, *, max_pct: int):
        """The context with its ``Position:`` section, or unchanged if it has none.

        Priced by ``marginal_cost.build_position_context`` from the books the
        source reads, this cycle's mark/funding and ``self._execution``.
        ``max_pct`` is the EFFECTIVE grid ceiling the caller also hands the
        format block, so the cost table and the advertised ceiling cannot
        disagree. Omitted — with the pricer's own WARNING — when the books
        are unusable; an exception from the read itself propagates like the
        audit read that follows it (both hit the same store).
        """
        if self._position_source is None:
            return ctx
        from dataclasses import replace

        from ..domains.perp.marginal_cost import build_position_context

        book = self._position_source()
        if book is None:
            logger.warning("position section omitted: the run has no books yet")
            return ctx
        position = build_position_context(
            size=book.size,
            entry_price=book.entry_price,
            wallet_balance=book.wallet_balance,
            mark=ctx.mark_price,
            leverage=self._risk.leverage,
            funding_rate=ctx.funding_rate,
            grid_min=self._decision.ai_target_margin_min_pct,
            grid_max=max_pct,
            grid_step=self._decision.target_margin_step_pct,
            taker_fee_rate=self._execution.taker_fee_rate,
            slippage_bps=self._execution.fill_model.slippage_bps,
            last_fill_at=book.last_fill_at,
        )
        return ctx if position is None else replace(ctx, position=position)

    def build_input(self, *, coin: str, as_of: datetime):
        from ..domains.perp import risk_gate
        from ..domains.perp.prompt_context import context_shape, render_market_context
        from ..domains.perp.schema import interval_to_ms
        from ..domains.perp.target_decision import decision_format_instructions
        from ..engine_bridge import _build_context, _context_refusal
        from ..exchanges.hyperliquid.errors import ExchangeError, MalformedResponseError
        from ..paper.scheduler import DecisionInput, RetryableDecisionError

        try:
            ctx, _client = _build_context(
                self._config, coin, on_blocking_read=self._on_blocking_read
            )
        except MalformedResponseError as exc:
            # BEFORE the ExchangeError clause below — that is this error's BASE
            # class, so the order is what makes this branch reachable at all.
            #
            # The venue answered and the answer was unusable: a misrouted candle
            # or funding read, or wire-schema drift. Filed as "connection", a
            # feed that is systematically wrong shared its §6.2 class with an
            # ordinary network blip, so ``error_type`` — the machine-readable
            # half of the record, and the column ``decision_attempts.csv``
            # carries — said "one transient disconnect" about a fault that
            # recurs every cycle until a human fixes it. The §3.1 ladder is
            # class-blind (its delays index on attempt COUNT), so this changes
            # only what the durable trail says: still retried, still terminal as
            # api_failed, now honest about which fault it was (issue #47).
            raise RetryableDecisionError("malformed_response", str(exc)) from exc
        except ExchangeError as exc:
            raise RetryableDecisionError("connection", str(exc)) from exc
        # All four pre-LLM context guards (under-warm data, fully-dead
        # indicator set, missing/dead regime indicators atr_14/ema_20/ema_50,
        # a stale candle feed), shared with the one-shot path (see
        # engine_bridge._context_refusal) — a dead or absent regime indicator
        # would otherwise let every cycle trade on a fabricated-calm RANGING
        # regime, and a stalled feed would let it trade on the past.
        # Deliberate (reviewed): they ride the §3.1 ladder to an api_failed
        # cycle — the failure precedes the AI call, so nothing is spent. A
        # gappy feed heals by the next try/cycle; a too-young listing, a
        # broken indicator engine or a feed that stopped advancing produces a
        # recurring api_failed cycle every 4h until it warms up / is fixed.
        # The refusal carries its own §6.2 class: the three "cannot be reasoned
        # over" guards file as ``server_error``, the freshness ones as
        # ``stale_market_data`` — a fault that does not heal on its own must
        # not read like a transient blip in the durable trail. The acceptance
        # validators count consecutive cycles that reached NO decision, of
        # any class; this class earns the specific wording (issue #50).
        # ``as_of`` is the scheduler's own clock reading for THIS cycle, not a
        # second call to the wall clock — the daemon keeps one time base. The
        # guard uses it ONLY as the fallback measuring clock a live fetch never
        # needs; the host-vs-exchange skew comes from the context's own paired
        # host reading, taken adjacent to the exchange one.
        refusal = _context_refusal(ctx, coin, self._config, now=as_of)
        if refusal is not None:
            raise RetryableDecisionError(refusal.error_type, refusal.message)
        # After the guards: a refused context spends nothing, so it reads no
        # books either. The DecisionInput below carries THIS context, so the
        # ai_inputs row and the payload describe the prompt with its section.
        # One ceiling for both: the cost table's rows and the format block's
        # advertised maximum come from this same value.
        max_pct = risk_gate.effective_max_target_margin_pct(self._risk, self._decision)
        ctx = self._attach_position(ctx, max_pct=max_pct)
        context_text = render_market_context(ctx)
        # The prompt's structure, kept beside the version stamp (issue #97):
        # a section that appears or disappears on a config edit alone — no
        # deploy, nothing to bump — segments the data by itself. Payload
        # metadata, not prompt text: the model sees context_text/format_text
        # unchanged, so adding this key is not a PROMPT_VERSION bump.
        shape = context_shape(ctx)
        format_text = decision_format_instructions(self._decision, max_pct=max_pct)
        payload = {
            "coin": coin,
            "as_of": as_of.isoformat(),
            "prompt_version": PROMPT_VERSION,
            "context_shape": shape,
            "context_text": context_text,
            "format_instructions": format_text,
        }
        raw = json.dumps(payload, ensure_ascii=False, indent=2)
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        # Microsecond-stamped: each retry try builds its own payload, and two
        # tries landing in the same wall-clock second must not overwrite each
        # other (the earlier try's stored hash would falsely alias the later
        # file) — same per-try-distinctness rule as input_id/output_id.
        path = self._payload_dir / f"{coin}-{as_of.strftime('%Y%m%dT%H%M%S_%fZ')}.json"
        try:
            self._payload_dir.mkdir(parents=True, exist_ok=True)
            path.write_text(raw, encoding="utf-8")
        except OSError as exc:
            # An audit-artifact filesystem failure (disk full, permissions)
            # must not tear down the daemon and strand the live SL/TP monitor:
            # nothing is in the store yet and the AI has not been called, so
            # ride the §3.1 ladder like the sibling environmental failures —
            # worst case a recurring api_failed cycle whose error_message
            # names the cause, with the position held and protection alive.
            raise RetryableDecisionError("server_error", f"payload write failed: {exc}") from exc
        candle_end = ctx.as_of
        candle_start = candle_end - timedelta(milliseconds=interval_to_ms(ctx.candle_interval))
        self._context_text = context_text
        self._format_text = format_text
        return DecisionInput(
            context=ctx,
            candle_start=candle_start,
            candle_end=candle_end,
            input_payload_path=str(path),
            input_payload_hash=f"sha256:{digest}",
            prompt_version=PROMPT_VERSION,
            context_shape=shape,
            model=self._engine_config["deep_think_llm"],
        )

    def request_decision(self, decision_input):
        from ..domains.perp.target_decision import parse_target_decision
        from ..integration.trading_graph import build_graph
        from ..paper.scheduler import RetryableDecisionError

        graph = build_graph(
            perp_context_text=self._context_text,
            config=self._engine_config,
            selected_analysts=self._analysts,
            output_format_text=self._format_text,
        )
        coin = decision_input.context.coin
        # Drive the base engine off the cycle's own as_of, not wall-clock now:
        # a late/recovery cycle (process was down across schedule points) must
        # feed the base news/sentiment analysts the same time base as the perp
        # market context they reason alongside, and a single read can't straddle
        # a UTC midnight between the two.
        trade_date = decision_input.context.as_of.strftime("%Y-%m-%d")
        try:
            propagated = graph.propagate(coin, trade_date, asset_type="crypto")
        except Exception as exc:  # noqa: BLE001 — engine-run failures are external (§3.1)
            raise RetryableDecisionError(_classify_engine_error(exc), str(exc)) from exc
        if (
            not isinstance(propagated, (tuple, list))
            or len(propagated) < 2
            or not isinstance(propagated[0], dict)
        ):
            # A drifted return contract is indistinguishable from a broken
            # response — retryable server_error, and api_failed after 3 tries.
            raise RetryableDecisionError(
                "server_error",
                f"engine.propagate returned an unexpected shape ({type(propagated).__name__})",
            )
        return parse_target_decision(propagated[0].get("final_trade_decision"), self._decision)


def _classify_engine_error(exc: Exception) -> str:
    """Map an engine-run exception onto the §6.2 error-type vocabulary."""
    text = f"{type(exc).__name__}: {exc}".lower()
    if "timeout" in text or "timed out" in text:
        return "timeout"
    # No bare "429": those three digits match any larger number that contains
    # them — an oid, an epoch-ms timestamp, a price — so "run 1429 failed" filed
    # as a rate limit. The sibling classifier in exchanges/hyperliquid/
    # sdk_client.py deleted exactly this marker for exactly this reason and kept
    # the phrases; this copy was missed. A real rate limit still lands here
    # through the SDK's exception CLASS name (``RateLimitError`` → "ratelimit")
    # or the phrase itself (2026-08-01 round-18 concept scan). The two lists are
    # deliberately NOT identical and must not be merged: that one gates retry
    # and §17.2 escalation on VENUE errors and carries the SDK-ism "slow down",
    # while this one only labels an LLM-engine failure for the audit trail
    # (``decision_attempts.error_type``, a closed vocabulary the scheduler
    # retries identically whatever it says).
    if "rate limit" in text or "ratelimit" in text or "too many requests" in text:
        return "rate_limit"
    if "connection" in text or "connect" in text or "network" in text:
        return "connection"
    return "server_error"
