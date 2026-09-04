"""Production seams: funding-rate history + the AI decision provider."""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # annotation-only: this module keeps its imports function-local
    from ..paper.position_facts import BookFacts, BookSource

logger = logging.getLogger(__name__)


# Version stamp for the ai_inputs.prompt_version column: bump whenever the
# injected context/format CONTRACT changes — its shape, or its wording — i.e.
# whenever a deploy crosses a measurement boundary (RUNBOOK §4; retired values
# are never reused, rollbacks included). The payload hash tracks content.
# History: the CHANGELOG entries for ``phase2-target-v*``. In short —
# v4 (2026-08-27): the context gains the ``Position:`` section; the format
# block is unchanged (v4's digest is v3's).
# v5 (2026-09-01): the FORMAT block no longer renders the three gate
# thresholds as numbers (marginal-cost plan PR-B); the context is unchanged.
PROMPT_VERSION = "phase2-target-v5"


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
        from ..common.instants import from_epoch_ms
        from ..exchanges.hyperliquid.errors import ExchangeError

        hour = funding_timestamp.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
        now = datetime.now(timezone.utc)
        age = now - hour
        needed_days = max(self._MIN_WINDOW_DAYS, age.days + 2)
        cached = self._cache.get(coin)
        if (
            cached is None
            or time.monotonic() - cached[0] > self._CACHE_TTL_SECONDS
            or cached[1] < needed_days
        ):
            try:
                # The HOST clock cuts this window, deliberately — the one
                # windowed read that does not take the exchange's (issue
                # #124). This looks up a PAST hour, and the only thing a host
                # clock offset can do to it is a miss: a host behind by S has
                # no points for the last S, so a fresh hour reads ``None``
                # and the event stays pending until a later poll — never a
                # wrong rate. Reading the exchange's clock here would add a
                # REST call per refresh to buy nothing the caller can use.
                points = self._market.get_funding_history(coin, needed_days, end=now)
            except ExchangeError as exc:
                # A VENUE failure means "pending" — the endpoint refused,
                # throttled, or answered malformed; the event waits for a
                # later poll. Only that family is caught: a ``TypeError``
                # from a call site that drifted from the reader's signature,
                # or a ``ValueError`` from a naive ``end``, is a programmer
                # error and propagates out of this lookup — counted as a
                # fetch failure it would log three WARNINGs and an ERROR that
                # read as an outage, while every settlement stayed pending
                # forever (issue #157). What the callers make of it is theirs:
                # the engine tick lets it end the run; the cycle-boundary
                # backfill contains it in a lane of its own, which says the
                # READER failed rather than blaming the stored row (issue
                # #193 — it used to land in the corrupt-row lane and send an
                # operator to SQLite to hunt a fault that is in the code).
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
                stamp = from_epoch_ms(point.time)
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
    # The last ``(prompt_version, context_shape, format_fingerprint)`` this
    # instance logged (issue #163); ``None`` until the first prompt is built.
    _logged_regime = None

    def __init__(
        self,
        config: dict,
        *,
        risk_cfg,
        decision_cfg,
        payload_dir: Path,
        on_blocking_read=None,
        position_source: BookSource,
    ) -> None:
        from ..domains.perp import risk_gate
        from ..domains.perp.marginal_cost import PositionPricing
        from ..engine_bridge import _build_engine_config

        self._config = config
        self._decision = decision_cfg
        self._payload_dir = payload_dir
        # Live only: ``build_input`` runs on the single-threaded tick and makes
        # the longest unrefreshed REST chain in the system, so the live wiring
        # passes a kill-switch refresh here. ``None`` for paper and the one-shot
        # CLI paths, which hold no dead man's switch (2026-08-01 lifecycle review).
        self._on_blocking_read = on_blocking_read
        # ``position_facts.BookSource`` — the run's books, read at build
        # time (``paper.position_facts.read_books``, bound by the paper
        # and live wirings). REQUIRED, with no default: both wirings pass one,
        # and a new one that forgot to would produce a silently position-blind
        # prompt (prompt v4's section simply absent, the ``|position`` token
        # missing from ``context_shape``) with nothing raising. The class-level
        # ``_position_source = None`` above still serves the tests that skip
        # ``__init__`` via ``object.__new__``. The books it returns reach the
        # builder as a declared parameter, so a harness that builds a context
        # directly can supply a position without this constructor (issue #134).
        self._position_source = position_source
        # The fill-cost parameters the position section prices with: the
        # ``paper_trading.execution`` block, parsed ONCE here (the startup
        # validator in engine_bridge._load_risk_decision already rejected a
        # malformed one). Parsed for every mode, live included — the live run
        # advertises the paper fill model's costs, the only fee model the
        # config carries, and the prompt says "assumptions" for that reason.
        # That the live lane reuses the PAPER assumptions is a decision, not
        # an oversight: made 2026-08-31 (PR #160), recorded 2026-09-03 (issue
        # #161) while paper is the only lane running. On live, slippage is a
        # measured quantity (the ``fills`` table), so a ``live:`` block should
        # eventually carry its own ``assumed_costs`` — and ``PositionPricing``
        # is the seam that change goes through: build it from that block
        # here, and nothing downstream needs to know which lane priced it.
        from ..paper.config import PaperTradingConfig

        execution = PaperTradingConfig.from_dict(config.get("paper_trading")).execution
        # ONE effective ceiling, resolved here rather than per cycle: it is a
        # pure function of two objects frozen at construction, and the cost
        # table and the format block must advertise the same number. As a
        # single attribute read twice that identity is structural; recomputed
        # at each call site it was only a comment. Building the pricing rules
        # here also moves their rejection (``PositionPricing.__post_init__``)
        # off the cycle path and onto provider construction — which is
        # pre-flight on a fresh run, and post-reconciliation on a restart
        # (see cli/paper.py), but either way before any decision is made.
        self._max_pct = risk_gate.effective_max_target_margin_pct(risk_cfg, decision_cfg)
        self._pricing = PositionPricing(
            leverage=risk_cfg.leverage,
            grid_min=decision_cfg.ai_target_margin_min_pct,
            grid_max=self._max_pct,
            grid_step=decision_cfg.target_margin_step_pct,
            taker_fee_rate=execution.taker_fee_rate,
            slippage_bps=execution.fill_model.slippage_bps,
        )
        self._engine_config, self._analysts = _build_engine_config(config)

    def _read_books(self) -> BookFacts | None:
        """This cycle's books — the ONE read behind the prompt and the audit row.

        ``None`` is the position-blind context: a run whose books do not exist
        yet, or — since the constructor made the source required — a test
        provider built through ``object.__new__``, which the class-level
        default serves. The one-shot CLI reaches a position-blind context by a
        different route entirely: it never builds this class, and writes
        ``position=None`` at the ``_build_context`` seam itself. Any OTHER
        exception (a store failure, a DTO guard tripped by a book state nothing
        expected) propagates: it is a bug, not a degraded state, and both
        drivers fail that cycle closed (``api_failed``, ``error_type`` empty)
        rather than the daemon (issue #134).
        """
        if self._position_source is None:
            return None
        books = self._position_source()
        if books is None:
            # Unreachable on the production wirings (initialize_run precedes
            # the first cycle); said in the log anyway, and the driver's audit
            # prologue will refuse the cycle on the same missing ledger.
            # Wording pinned by test (issue #161): "no books yet" must read
            # apart from ``marginal_cost.build_position_context``'s "is not
            # positive" — same prompt, same context_shape, no store column;
            # rationale in RUNBOOK §7.
            logger.warning(
                "position section omitted: the run has no books yet "
                "(the audit row will refuse this cycle on the same missing ledger)"
            )
        return books

    def _position_inputs(self, books: BookFacts | None):
        """The books plus the rules a move is priced under, or ``None``.

        Handed to ``_build_context`` so the ``Position:`` section is assembled
        by the builder, at the same mark and funding as the rest of the
        context (issue #134). The pricing half is ``self._pricing``, built
        once at construction — its ``grid_max`` is the same ``self._max_pct``
        the format block is rendered from, so the cost table and the
        advertised ceiling cannot disagree.
        """
        if books is None:
            return None
        from ..domains.perp.marginal_cost import PositionInputs

        return PositionInputs(book=books.position_facts, pricing=self._pricing)

    def build_input(self, *, coin: str, as_of: datetime):
        from ..domains.perp.context_guards import context_refusal
        from ..domains.perp.prompt_context import context_shape, render_market_context
        from ..domains.perp.schema import interval_to_ms
        from ..domains.perp.target_decision import (
            decision_format_instructions,
            format_fingerprint,
        )
        from ..engine_bridge import _build_context
        from ..exchanges.hyperliquid.errors import ExchangeError, MalformedResponseError
        from ..paper.scheduler import DecisionInput, RetryableDecisionError

        # Read BEFORE the fetch, so the builder can price the section at the
        # same snapshot the rest of the context is built from. That also puts
        # the read ahead of the four context guards below — deliberate: a
        # refused cycle now makes three local SQLite reads it used to skip.
        # They are cheap, they touch no network and spend nothing, and the
        # alternative (assembling the section after the guards) is exactly the
        # second construction path issue #134 removed. The same books ride the
        # DecisionInput to the driver's ai_inputs row (``books=`` below).
        books = self._read_books()
        position = self._position_inputs(books)
        try:
            ctx, _client = _build_context(
                self._config,
                coin,
                on_blocking_read=self._on_blocking_read,
                position=position,
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
        # context_guards.context_refusal) — a dead or absent regime indicator
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
        refusal = context_refusal(ctx, coin, self._config, now=as_of)
        if refusal is not None:
            raise RetryableDecisionError(refusal.error_type, refusal.message)
        # The DecisionInput below carries THIS context — the one the builder
        # already put the position section on — so the ai_inputs row and the
        # payload describe the prompt the model is actually shown.
        context_text = render_market_context(ctx)
        # The prompt's structure, kept beside the version stamp (issue #97):
        # a section that appears or disappears on a config edit alone — no
        # deploy, nothing to bump — segments the data by itself. Payload
        # metadata, not prompt text: the model sees context_text/format_text
        # unchanged, so adding this key is not a PROMPT_VERSION bump.
        shape = context_shape(ctx)
        format_text = decision_format_instructions(self._decision, max_pct=self._max_pct)
        # The third key (issue #129): the format block is rendered from the
        # live config, so its numbers move on a YAML edit that touches neither
        # of the other two — a content digest of the text the model is shown.
        fingerprint = format_fingerprint(format_text)
        payload = {
            "coin": coin,
            "as_of": as_of.isoformat(),
            "prompt_version": PROMPT_VERSION,
            "context_shape": shape,
            "format_fingerprint": fingerprint,
            "context_text": context_text,
            "format_instructions": format_text,
        }
        from ..common.digest import payload_digest

        # Bytes end to end: the digest is over exactly the bytes the file
        # holds, so a verifier that rehashes the file (the fingerprint
        # backfill, issue #163) agrees with the row on every platform — a
        # text-mode write would let the OS rewrite the newlines in between.
        raw = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        digest = payload_digest(raw)
        # Microsecond-stamped: each retry try builds its own payload, and two
        # tries landing in the same wall-clock second must not overwrite each
        # other (the earlier try's stored hash would falsely alias the later
        # file) — same per-try-distinctness rule as input_id/output_id.
        path = self._payload_dir / f"{coin}-{as_of.strftime('%Y%m%dT%H%M%S_%fZ')}.json"
        try:
            self._payload_dir.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
        except OSError as exc:
            # An audit-artifact filesystem failure (disk full, permissions)
            # must not tear down the daemon and strand the live SL/TP monitor:
            # nothing is in the store yet and the AI has not been called, so
            # ride the §3.1 ladder like the sibling environmental failures —
            # worst case a recurring api_failed cycle whose error_message
            # names the cause, with the position held and protection alive.
            raise RetryableDecisionError("server_error", f"payload write failed: {exc}") from exc
        # Say which bucket this prompt lands in (issue #163; the line and its
        # three surfaces: ``common.prompt_regime``): once, the first time this
        # instance builds one through, and again only when the triple flips
        # mid-run (a section appearing or disappearing) — silent in between.
        # AFTER the payload write, the last thing HERE that can fail: a cycle
        # that dies on the write leaves no ai_inputs row, and a line logged
        # for it would claim a bucket ``validate`` never counts while the
        # first cycle that does reach the store stayed silent. The driver's
        # own ai_inputs insert comes after this method returns and is not
        # covered (accepted 2026-09-03): a failure there is the non-retryable
        # bug lane — an ERROR traceback beside this line, api_failed with an
        # empty error_type — not a quiet cycle the line could be mistaken for.
        regime = (PROMPT_VERSION, shape, fingerprint)
        if regime != self._logged_regime:
            from ..common.prompt_regime import prompt_regime_line

            logger.info(prompt_regime_line(*regime))
            self._logged_regime = regime
        candle_end = ctx.as_of
        candle_start = candle_end - timedelta(milliseconds=interval_to_ms(ctx.candle_interval))
        self._context_text = context_text
        self._format_text = format_text
        return DecisionInput(
            context=ctx,
            candle_start=candle_start,
            candle_end=candle_end,
            input_payload_path=str(path),
            input_payload_hash=digest,
            prompt_version=PROMPT_VERSION,
            context_shape=shape,
            format_fingerprint=fingerprint,
            model=self._engine_config["deep_think_llm"],
            books=books,
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
