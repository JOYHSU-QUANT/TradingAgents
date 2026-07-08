"""Subcommand CLI for the Hyperliquid perp module (PR 4).

Three subcommands plus full Phase 1/2 backward compatibility:

- ``python -m contrib.hyperliquid_perp paper --coin BTC`` — the long-running
  paper run: restart reconciliation (execution §1.2), then the 30-second
  monitor/scheduler loop (rolling 4h decisions, TWAP execution, SL/TP,
  funding), with CSV export after every completed cycle and on shutdown.
- ``python -m contrib.hyperliquid_perp export --run-id <id> --output-dir <dir>``
  — manual full-dataset CSV export (phase2-data §1.1).
- ``python -m contrib.hyperliquid_perp validate --run-id <id>`` — the spec §5
  acceptance report and Phase-3 verdict.

Anything else — including the Phase 1 ``--context-only`` smoke run and the
single-shot engine run — is delegated verbatim to :mod:`.main`, whose behaviour
is unchanged.

Exit codes: ``0`` success (for ``validate``: Phase-3 ready), ``1`` named
operator/config/environment errors, ``2`` unexpected error, ``4``
(``validate`` only) the run is internally consistent but has not accumulated
the 30-cycle gate yet ("keep running cycles"), ``5`` (``validate`` only) the
run has integrity failures — orphans, snapshot or replay mismatches ("the
store is broken; investigate before trusting results").
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import CONFIG_LOAD_ERRORS, load_config
from .persistence.db import Database

logger = logging.getLogger(__name__)

_SUBCOMMANDS = ("paper", "export", "validate")

# Version stamp for the ai_inputs.prompt_version column: bump when the injected
# context/format contract changes shape (the payload hash tracks content).
PROMPT_VERSION = "phase2-target-v1"


def _raise_keyboard_interrupt(signum, frame) -> None:
    """SIGTERM → the SIGINT path: systemd/docker/``kill`` stop with the default
    TERM signal, and phase2-data §1.1's shutdown export must fire for them
    exactly as it does for Ctrl-C."""
    raise KeyboardInterrupt


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:]) if argv is None else list(argv)
    if not argv or argv[0] not in _SUBCOMMANDS:
        # Phase 1/2 compatibility path: identical flags, identical behaviour.
        from .main import main as legacy_main

        return legacy_main(argv)
    command, rest = argv[0], argv[1:]
    try:
        if command == "export":
            return _cmd_export(rest)
        if command == "validate":
            return _cmd_validate(rest)
        return _cmd_paper(rest)
    except KeyboardInterrupt:
        print("interrupted.", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001 — last-resort handler, mirrors main.main
        logger.exception("unexpected error in %s", command)
        print(f"fatal: unexpected error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


# --------------------------------------------------------------------------
# export / validate — offline commands over an existing store
# --------------------------------------------------------------------------


def _open_existing_db(path: str) -> Database | None:
    """Open an existing store, or report why not (never create one implicitly).

    ``Database(path)`` would happily create an empty schema — and an offline
    command against a typo'd path would then "succeed" with zero rows.
    """
    if not Path(path).exists():
        print(f"error: database {path!r} does not exist.", file=sys.stderr)
        return None
    return Database(path)


def _cmd_export(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m contrib.hyperliquid_perp export",
        description="Export one run's full dataset as the eight phase2-data CSVs.",
    )
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--db", default="paper_trading.db", help="SQLite store path.")
    args = parser.parse_args(argv)

    from .persistence.export import ExportError, export_run

    db = _open_existing_db(args.db)
    if db is None:
        return 1
    with db:
        try:
            paths = export_run(db, run_id=args.run_id, output_dir=args.output_dir)
        except ExportError as exc:
            print(f"error: export_failed — {exc}", file=sys.stderr)
            return 1
    for path in paths:
        print(path)
    return 0


def _cmd_validate(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m contrib.hyperliquid_perp validate",
        description="Spec §5 acceptance report: summary metrics + Phase-3 verdict.",
    )
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--db", default="paper_trading.db", help="SQLite store path.")
    args = parser.parse_args(argv)

    from .paper.validation import validate_run

    db = _open_existing_db(args.db)
    if db is None:
        return 1
    with db:
        try:
            report = validate_run(db, run_id=args.run_id)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
    for line in report.summary_lines():
        print(line)
    if report.phase3_ready:
        return 0
    # Integrity failures and a merely-short run are different operator actions
    # (investigate vs keep running) — give them distinct codes.
    return 5 if report.failures else 4


# --------------------------------------------------------------------------
# paper — the long-running run
# --------------------------------------------------------------------------


def _run_config_subset(config: dict, coin: str) -> dict:
    """The behaviour-defining blocks recorded at run genesis (never network/wallet)."""
    return {
        "risk": config.get("risk"),
        "decision": config.get("decision"),
        "paper_trading": config.get("paper_trading"),
        "coin": coin,
    }


def _config_drift_report(
    stored_json: str | None, config: dict, coin: str
) -> tuple[str, str] | None:
    """Compare today's config against the run's genesis record.

    Returns ``("coin", msg)`` for a coin mismatch (hard error — a different
    instrument is a different run, not a resumption), ``("params", msg)`` for
    risk/decision/paper_trading drift (warning — behaviour changes mid-run but
    the operator may intend it), or ``None`` when nothing drifted or no record
    exists (a pre-drift-check store).
    """
    if not stored_json:
        return None
    stored = json.loads(stored_json)
    # Round-trip today's subset through JSON so both sides compare in the same
    # serialized shape (default=str stringifies any non-JSON scalar).
    current = json.loads(json.dumps(_run_config_subset(config, coin), default=str))
    if stored.get("coin") != current.get("coin"):
        return (
            "coin",
            f"run was created for coin {stored.get('coin')!r} but this resume "
            f"targets {coin!r} — refusing to continue a run on a different "
            "instrument (use a new --run-id).",
        )
    drifted = sorted(
        key for key in ("risk", "decision", "paper_trading") if stored.get(key) != current.get(key)
    )
    if drifted:
        return (
            "params",
            f"config drift on resume: {', '.join(drifted)} differ from the values "
            "recorded at run creation — this run's behaviour changes from here on.",
        )
    return None


def _cmd_paper(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m contrib.hyperliquid_perp paper",
        description="Long-running Phase 2 paper run: 4h AI cycles + 30s paper execution.",
    )
    parser.add_argument("--coin", default=None, help="Coin symbol, e.g. BTC.")
    parser.add_argument("--config", default=None, help="Config YAML path.")
    parser.add_argument("--db", default="paper_trading.db", help="SQLite store path.")
    parser.add_argument(
        "--run-id",
        default=None,
        help="Run id; defaults to paper-<COIN>. Reuse the same id to resume a run.",
    )
    parser.add_argument(
        "--export-dir",
        default=None,
        help="CSV export directory; defaults to <db dir>/exports/<run_id>.",
    )
    parser.add_argument(
        "--create",
        action="store_true",
        help=(
            "Allow creating a new store/run. Without it, a missing DB file or an "
            "unknown run_id is an error — so a wrong working directory or a typo'd "
            "path can never silently fork a run's history into a fresh database."
        ),
    )
    args = parser.parse_args(argv)

    # Checked before any network/key work: a wrong CWD or typo'd --db must be
    # an error, not a silently forked fresh store (and it needs no credentials
    # to diagnose).
    db_path = Path(args.db)
    if not db_path.exists() and not args.create:
        print(
            f"error: database {str(db_path)!r} does not exist. Pass --create to "
            "start a new store, or point --db at the existing one.",
            file=sys.stderr,
        )
        return 1

    import os
    import signal

    # Heavy/engine imports deferred so `export`/`validate` stay light (the
    # engine/scheduler stack is imported by _cmd_paper_locked once the lease
    # is in hand).
    from .exchanges.hyperliquid.errors import ExchangeError
    from .exchanges.hyperliquid.market_data import HyperliquidMarketData
    from .exchanges.hyperliquid.sdk_client import HyperliquidClient
    from .main import _load_risk_decision, _resolve_coin
    from .paper.clock import WallClock
    from .paper.config import PaperTradingConfig
    from .paper.engine import AssetSpec
    from .paper.run_lock import RunLockError, acquire_run_lock, release_run_lock
    from .persistence import repository as repo

    if not os.environ.get("OPENROUTER_API_KEY"):
        print(
            "error: OPENROUTER_API_KEY is not set — the paper run drives the AI engine "
            "every 4h. Use --context-only (legacy CLI) for a keyless dev loop.",
            file=sys.stderr,
        )
        return 1
    try:
        config = load_config(args.config)
    except CONFIG_LOAD_ERRORS as exc:
        print(f"error: invalid config — {exc}. Fix the YAML and re-run.", file=sys.stderr)
        return 1
    coin = _resolve_coin(args, config)
    cfgs = _load_risk_decision(config)
    if cfgs is None:
        return 1
    risk_cfg, decision_cfg = cfgs
    paper_cfg = PaperTradingConfig.from_dict(config.get("paper_trading"))

    clock = WallClock()
    try:
        client = HyperliquidClient.from_config(config)
        market = HyperliquidMarketData(client)
        sz_decimals, schedule = market.get_asset_meta(coin)
    except ExchangeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    asset = AssetSpec(coin=coin, sz_decimals=sz_decimals, margin_schedule=schedule)

    run_id = args.run_id or f"paper-{coin}"
    export_dir = (
        Path(args.export_dir) if args.export_dir else db_path.resolve().parent / "exports" / run_id
    )
    funding_source = _HistoryFundingSource(market)

    with Database(db_path) as db:
        is_restart = repo.get_run(db.conn, run_id) is not None
        now = clock.now()
        if not is_restart and not args.create:
            print(
                f"error: run {run_id!r} does not exist in {db_path}. Pass --create "
                "to start it, or fix --run-id / --db to resume the intended run.",
                file=sys.stderr,
            )
            return 1
        if is_restart and args.create:
            # The flag exists to make store identity explicit in BOTH
            # directions: silently resuming here would append to an old run
            # (old position, old ledger, old schedule) when the operator
            # plausibly meant a fresh acceptance run — contaminating every
            # §5 metric without a word.
            print(
                f"error: run {run_id!r} already exists in {db_path}. Drop --create "
                "to resume it, or pick a new --run-id for a fresh run.",
                file=sys.stderr,
            )
            return 1
        # One live process per run: taken BEFORE the destructive restart
        # reconciliation, which assumes the previous process is dead.
        try:
            acquire_run_lock(db, run_id, pid=os.getpid(), now=now)
        except RunLockError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        # From here the lease is ours; release it on every exit (the except
        # paths below return, a crash leaves it to go stale on its own).
        signal.signal(signal.SIGTERM, _raise_keyboard_interrupt)

        def _run_locked() -> int:
            """The lease-holding tail of ``paper``: create/reconcile, then the loop."""
            from .paper import accounting
            from .paper.engine import PaperExecutionEngine
            from .paper.market_feed import PortSnapshotProvider
            from .paper.reconcile import ReconciliationError, reconcile_on_restart
            from .paper.scheduler import PaperScheduler
            from .persistence import repository as repo
            from .persistence.models import PositionState
            from .persistence.schema import SCHEMA_VERSION

            if not is_restart:
                seeds = [
                    PositionState(coin=p.coin, size=p.size, entry_price=p.entry_price)
                    for p in paper_cfg.account.initial_positions
                ]
                accounting.initialize_run(
                    db,
                    run_id=run_id,
                    mode="paper",
                    initial_balance_usdc=paper_cfg.account.initial_balance_usdc,
                    schema_version=SCHEMA_VERSION,
                    initial_positions=seeds,
                    # Only the behaviour-defining blocks — never network/wallet keys.
                    config_json=json.dumps(
                        _run_config_subset(config, coin), ensure_ascii=False, default=str
                    ),
                    created_at=now,
                )
                print(f"created paper run {run_id!r} in {db_path}", file=sys.stderr)
            else:
                # Resuming an existing run under drifted config would silently
                # change behaviour mid-run while every metric treats it as one
                # homogeneous run: a coin mismatch is a hard error, parameter
                # drift a loud warning.
                run_row = repo.get_run(db.conn, run_id)
                assert run_row is not None  # is_restart established the row exists
                drift = _config_drift_report(run_row["config_json"], config, coin)
                if drift is not None:
                    kind, message = drift
                    if kind == "coin":
                        print(f"error: {message}", file=sys.stderr)
                        return 1
                    print(f"WARNING: {message}", file=sys.stderr)
                try:
                    report = reconcile_on_restart(
                        db, run_id=run_id, now=now, funding_source=funding_source
                    )
                except ReconciliationError as exc:
                    print(f"error: {exc}", file=sys.stderr)
                    return 1
                print(
                    f"restart reconciliation for {run_id!r}: "
                    f"{len(report.canceled_plan_ids)} plan(s) canceled, "
                    f"{report.funding_posted} funding event(s) backfilled"
                    + (", immediate cycle forced" if report.forced_immediate_cycle else ""),
                    file=sys.stderr,
                )

            engine = PaperExecutionEngine(
                db=db,
                run_id=run_id,
                asset=asset,
                clock=clock,
                provider=PortSnapshotProvider(market, clock),
                risk_config=risk_cfg,
                decision_config=decision_cfg,
                paper_config=paper_cfg,
                funding_source=funding_source,
            )
            if is_restart and engine.has_active_work():
                # §1.2 step 6: the restart was a blind window — the first tick must
                # treat a crossed SL as a gap stop, not a normal trigger-price fill.
                # Armed only when the blind window had something to watch (a position
                # or live protection): on a flat restart the forced immediate cycle
                # can open a NEW position via poll() before any tick ever runs, and
                # an unconsumed flag would mislabel that fresh position's first real
                # SL trigger as a restart gap fill.
                engine.flag_restart_gap()
            provider = _EngineDecisionProvider(
                config,
                risk_cfg=risk_cfg,
                decision_cfg=decision_cfg,
                payload_dir=db_path.resolve().parent / "payloads" / run_id,
            )
            scheduler = PaperScheduler(
                db=db,
                run_id=run_id,
                engine=engine,
                clock=clock,
                provider=provider,
                asset=asset,
                risk_config=risk_cfg,
                decision_config=decision_cfg,
            )
            interval = paper_cfg.execution.market_monitor.interval_seconds
            try:
                _paper_loop(
                    db, run_id, engine, scheduler, clock, interval, export_dir, funding_source
                )
            except KeyboardInterrupt:
                print("\nshutting down — final export...", file=sys.stderr)
                _post_cycle_export(db, run_id, export_dir)
                return 0
            return 0

        try:
            return _run_locked()
        finally:
            release_run_lock(db, run_id, pid=os.getpid(), now=clock.now())


def _paper_loop(
    db, run_id, engine, scheduler, clock, interval: int, export_dir: Path, funding_source
) -> None:
    """The production loop: tick (when the monitor is on) → poll → sleep.

    Tick before poll so a restart handles liquidation / gap SL / TP on the
    reconciled position *before* the immediate new cycle plans against it
    (execution §1.2 step 6 before step 8). Sleep is bounded by the monitor
    interval while any market work is live (execution §5.5) and stretches
    toward ``next_decision_at`` when everything is quiet, so an idle run
    issues no market-data requests.

    A cycle-boundary replay mismatch stops NEW decisions (the books can no
    longer be trusted for sizing) but keeps the engine ticking — SL/TP
    protection and the market monitor must survive (spec §5 / data §1.1).

    Every iteration refreshes the single-instance lease, so the sleep is
    capped at 60s (well inside ``LOCK_STALE_SECONDS``) — an idle iteration
    still touches only SQLite, never market data.
    """
    import os

    from .paper.reconcile import backfill_pending_funding
    from .paper.run_lock import heartbeat_run_lock
    from .paper.scheduler import CycleEvent

    pid = os.getpid()
    trading_halted = False
    while True:
        heartbeat_run_lock(db, run_id, pid=pid, now=clock.now())
        if engine.has_active_work():
            engine.tick()
        result = scheduler.poll() if not trading_halted else None
        if result is not None:
            print(
                f"[{clock.now().isoformat(timespec='seconds')}] cycle event: "
                f"{result.event.value} (attempt {result.decision_attempt_id})",
                file=sys.stderr,
            )
            if result.event.is_cycle_terminal:
                # Retry any backfillable pending funding at every cycle
                # boundary (execution §6.5's 稍後補帳 — not restart-only).
                backfill_pending_funding(
                    db, run_id=run_id, now=clock.now(), funding_source=funding_source
                )
                if not _post_cycle_export(db, run_id, export_dir):
                    trading_halted = True
                    print(
                        "ERROR: accounting replay can no longer rebuild this run's "
                        "books — halting NEW decision cycles. The engine keeps "
                        "ticking (SL/TP protection and monitor stay live). "
                        "Investigate the store, then restart to resume.",
                        file=sys.stderr,
                    )
            if result.event is CycleEvent.API_FAILED:
                print(
                    "decision API failed 3 times — holding position until the next cycle.",
                    file=sys.stderr,
                )
        now = clock.now()
        due = scheduler.next_due_at() if not trading_halted else None
        if engine.has_active_work() or (due is None and not trading_halted):
            delay = float(interval)
        elif due is None:  # halted and no market work: idle heartbeat
            delay = 600.0
        else:
            delay = max(1.0, min((due - now).total_seconds(), 600.0))
        # The 60s cap keeps the lease heartbeat fresh; waking early is a cheap
        # SQLite poll (nothing above issues market requests while idle).
        time.sleep(min(delay, 60.0))


def _post_cycle_export(db, run_id: str, export_dir: Path) -> bool:
    """Replay-verify then export (phase2-data §1.1); returns whether the books verified.

    An export failure never stops trading (spec: record ``export_failed`` and
    carry on) — but it IS durably recorded on ``scheduler_state``
    (``last_export_status`` / ``last_export_error`` / ``last_export_at``), so a
    post-mortem can tell how long the CSV view had been stale even when stderr
    was not captured. A replay mismatch or replay *failure* returns ``False``
    so the caller can stop opening new positions on unverifiable books.
    """
    from .paper import accounting
    from .persistence import repository as repo
    from .persistence.export import ExportError, export_run

    def _record(status: str, error: str | None) -> None:
        stamp = datetime.now(timezone.utc)
        with db.transaction() as conn:
            repo.upsert_scheduler_state(
                conn,
                run_id,
                last_export_status=status,
                last_export_error=error,
                last_export_at=stamp,
                updated_at=stamp,
            )

    replay_ok = True
    try:
        replayed = accounting.replay(db, run_id=run_id)
        if not replayed.is_consistent:
            replay_ok = False
            print(
                f"WARNING: accounting replay mismatch for {run_id!r}: "
                f"positions {list(replayed.position_mismatches)!r}, "
                f"account_matches={replayed.account_matches} — investigate before "
                "trusting this run's results.",
                file=sys.stderr,
            )
    except Exception as exc:  # noqa: BLE001 — reconciliation must not kill the loop
        replay_ok = False  # unverifiable books are treated like inconsistent ones
        print(f"WARNING: accounting replay failed: {exc}", file=sys.stderr)
    try:
        export_run(db, run_id=run_id, output_dir=export_dir)
        _record("ok", None)
        print(f"exported CSVs to {export_dir}", file=sys.stderr)
    except ExportError as exc:
        # §1.1: record export_failed, keep the monitor and protections running.
        logger.error("export_failed for %s: %s", run_id, exc)
        print(f"WARNING: export_failed — {exc}", file=sys.stderr)
        _record("failed", str(exc))
    return replay_ok


# --------------------------------------------------------------------------
# production seams: funding-rate history + the AI decision provider
# --------------------------------------------------------------------------


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

    def __init__(self, market) -> None:
        self._market = market
        # coin -> (fetched_at_monotonic, window_days_fetched, {hour: rate})
        self._cache: dict[str, tuple[float, int, dict[datetime, object]]] = {}

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
                logger.warning("funding history fetch failed for %s: %s", coin, exc)
                return None
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
    """

    def __init__(self, config: dict, *, risk_cfg, decision_cfg, payload_dir: Path) -> None:
        from .main import _build_engine_config

        self._config = config
        self._risk = risk_cfg
        self._decision = decision_cfg
        self._payload_dir = payload_dir
        self._engine_config, self._analysts = _build_engine_config(config)

    def build_input(self, *, coin: str, as_of: datetime):
        from .domains.perp import risk_gate
        from .domains.perp.prompt_context import render_market_context
        from .domains.perp.target_decision import decision_format_instructions
        from .exchanges.hyperliquid.errors import ExchangeError
        from .exchanges.hyperliquid.market_data import interval_to_ms
        from .main import _build_context, _warmup_threshold
        from .paper.scheduler import DecisionInput, RetryableDecisionError

        try:
            ctx, _client = _build_context(self._config, coin)
        except ExchangeError as exc:
            raise RetryableDecisionError("connection", str(exc)) from exc
        needed = _warmup_threshold(self._config)
        if ctx.candle_count < needed:
            # Not enough closed candles for real signal — transient for a young
            # listing / gappy feed, so let the §3.1 ladder retry then api_failed.
            raise RetryableDecisionError(
                "server_error",
                f"under-warmed market data: {ctx.candle_count} candles, need {needed}",
            )
        context_text = render_market_context(ctx)
        format_text = decision_format_instructions(
            self._decision,
            max_pct=risk_gate.effective_max_target_margin_pct(self._risk, self._decision),
        )
        payload = {
            "coin": coin,
            "as_of": as_of.isoformat(),
            "prompt_version": PROMPT_VERSION,
            "context_text": context_text,
            "format_instructions": format_text,
        }
        raw = json.dumps(payload, ensure_ascii=False, indent=2)
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        self._payload_dir.mkdir(parents=True, exist_ok=True)
        # Microsecond-stamped: each retry try builds its own payload, and two
        # tries landing in the same wall-clock second must not overwrite each
        # other (the earlier try's stored hash would falsely alias the later
        # file) — same per-try-distinctness rule as input_id/output_id.
        path = self._payload_dir / f"{coin}-{as_of.strftime('%Y%m%dT%H%M%S_%fZ')}.json"
        path.write_text(raw, encoding="utf-8")
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
            model=self._engine_config["deep_think_llm"],
        )

    def request_decision(self, decision_input):
        from .domains.perp.target_decision import parse_target_decision
        from .integration.trading_graph import build_graph
        from .paper.scheduler import RetryableDecisionError

        graph = build_graph(
            perp_context_text=self._context_text,
            config=self._engine_config,
            selected_analysts=self._analysts,
            output_format_text=self._format_text,
        )
        coin = decision_input.context.coin
        trade_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
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
    if "rate limit" in text or "ratelimit" in text or "429" in text:
        return "rate_limit"
    if "connection" in text or "connect" in text or "network" in text:
        return "connection"
    return "server_error"
