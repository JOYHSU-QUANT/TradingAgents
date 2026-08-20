"""Subcommand CLI for the Hyperliquid perp module.

The subcommands (plus full Phase 1/2 backward compatibility):

- ``python -m contrib.hyperliquid_perp paper --coin BTC`` — the long-running
  paper run: restart reconciliation (execution §1.2), then the 30-second
  monitor/scheduler loop (rolling 4h decisions, TWAP execution, SL/TP,
  funding), with CSV export after every completed cycle and on shutdown.
- ``python -m contrib.hyperliquid_perp export --run-id <id> --output-dir <dir>``
  — manual full-dataset CSV export (phase2-data §1.1).
- ``python -m contrib.hyperliquid_perp validate --run-id <id>`` — the acceptance
  report and verdict. A PAPER run gets the phase2-spec §5 report; a LIVE run gets
  the phase3-spec §20.3 / §21.4 report (the run's stored mode selects which).
- ``python -m contrib.hyperliquid_perp live --config <yaml>`` — the Phase 3
  startup skeleton (phase3-spec PR 1): load the ``live:`` config gates, verify
  the agent-wallet authorization, print the effective notional caps, and exit
  without entering any trading loop (that is ``live --run-id --loop``, PR 5).
  Places no orders.
- ``python -m contrib.hyperliquid_perp live-smoke --run-id <id> --config <yaml>``
  — the phase3-spec §20.2 testnet smoke checklist (PR 6): drive each signed
  exchange action against testnet, record the verdicts, and report the §20.2
  cycle-entry gate. ``--gate-status`` reads the stored gate (no network);
  ``--dry-run`` validates config + wiring and places no orders.
- ``python -m contrib.hyperliquid_perp safe-mode --run-id <id> --status`` —
  live-run safe-mode inspection and the manual lane (``--release``,
  ``--stamp-case``) for §12.3/§13.5 operator actions.

Empty argv and flag-style invocations (first argument starting with ``-``) —
including the Phase 1 ``--context-only`` smoke run and the single-shot engine
run — are delegated verbatim to :mod:`.main`, whose behaviour is unchanged.
A bare first argument that is not a known subcommand is an error (exit 1):
the legacy CLI takes no positionals, so it can only be a subcommand typo.

Exit codes: ``0`` success (for ``validate``: Phase-3 ready), ``1`` named
operator/config/environment errors — including a protection-only ``paper`` run
that self-terminates after its position closes (final export written; the
books never re-verified, the API key was never supplied, or the engine
failed to import — stderr says which), ``2`` unexpected error, ``4``
not-yet-at-the-gate outcomes (``validate``: short of the 30-cycle gate or a
red/missing smoke test — curable by a ``live-smoke`` re-run;
``live-smoke``: the §20.2 gate is not satisfied, incl. a pre-flight abort;
``live --loop``: the §20.2 smoke gate is not open on this run;
``live`` without ``--loop``: recovery ran but judged unclean; ``safe-mode
--status``: a safe mode is latched, recoverable OR manual — 4 means "latched",
not "human action required"), ``5`` (``validate`` only) the
run has integrity failures — orphans, snapshot or replay mismatches, or a
store so corrupt the checks themselves cannot run ("the store is broken;
investigate before trusting results"), ``130`` interrupted before a graceful
lane could take over (Ctrl-C in ``export``/``validate``/``live``/``live-smoke``
or during ``paper`` startup/reconciliation — SIGTERM likewise once ``paper``,
``live`` or ``live-smoke`` has installed its handler; once the ``paper`` loop
runs, both signals take the shutdown-export lane instead of ``130``). For
``live-smoke`` the handler is installed with the run lease, so an interrupt
after that point still runs the suite's cleanup (sweep resting probes, close the
staged long, disarm the switch — in that order, because flattening first makes
the exchange auto-cancel the reduce-only probes) rather than stranding it.
Legacy delegated invocations keep
:mod:`.main`'s own exit contract.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

from ..config import dotenv_diagnosis, load_dotenv_files
from ..persistence.db import Database
from . import _provider, paper_export
from ._common import (
    _existing_run_row,
    _open_existing_db,
    _raise_keyboard_interrupt,
    _require_api_key,
    _require_live_run_mode,
)
from ._drift import (
    _HARD_DRIFT_KINDS,
    _config_drift_report,
    _norm_network,
    _run_config_subset,
)
from ._provider import (
    PROMPT_VERSION,
    _classify_engine_error,
    _EngineDecisionProvider,
    _HistoryFundingSource,
)
from .live import _cmd_live, _live_startup_recovery
from .live_loop import (
    _LIVE_TICK_SECONDS,
    _contain_as_recoverable_safe_mode,
    _day_baseline_from_exchange,
    _live_heartbeat,
    _run_live_loop,
    _still_owns_run,
)
from .live_shared import (
    _RECOVERY_MAX_TICK_GAP_SECONDS,
    _conflicting_run_lease,
    _print_smoke_gate,
    _run_genesis_network,
    _smoke_gate_buckets,
    _timing_preflight,
)
from .offline import _cmd_export, _cmd_validate, _validate_live
from .paper_export import (
    _UNVERIFIED_MARKER,
    _mark_export_verification,
    _post_cycle_export,
    _retry_pending_funding,
    _stamp_breadcrumb,
)
from .safe_mode import _cmd_safe_mode, _stamp_reconciliation_case
from .smoke import (
    _SMOKE_MIN_KILL_SWITCH_DEADLINE,
    _build_real_smoke_session,
    _build_smoke_session,
    _cmd_live_smoke,
    _smoke_startup_recovery,
)

logger = logging.getLogger(__name__)

_SUBCOMMANDS = ("paper", "export", "validate", "live", "live-smoke", "safe-mode")


def main(argv: list[str] | None = None) -> int:
    # Before anything reads os.environ: the OPENROUTER_API_KEY startup checks
    # (fresh `paper` runs, healthy keyless-restart triage) run long before the
    # lazily-imported engine package would load the .env files itself.
    load_dotenv_files()
    argv = list(sys.argv[1:]) if argv is None else list(argv)
    if not argv or argv[0].startswith("-"):
        # Phase 1/2 compatibility path: identical flags, identical behaviour.
        # Legacy accepts no positionals, so flag-shaped/empty argv is lossless.
        from ..main import main as legacy_main

        return legacy_main(argv)
    if argv[0] not in _SUBCOMMANDS:
        # A bare unknown word is almost certainly a subcommand typo; delegating
        # it would surface the legacy parser's usage (which never mentions the
        # subcommands) under exit code 2 ("unexpected error").
        print(
            f"error: unknown subcommand {argv[0]!r} (expected one of: "
            f"{', '.join(_SUBCOMMANDS)}; legacy flag invocations like "
            "--context-only pass through unchanged).",
            file=sys.stderr,
        )
        return 1
    command, rest = argv[0], argv[1:]
    try:
        if command == "export":
            return _cmd_export(rest)
        if command == "validate":
            return _cmd_validate(rest)
        if command == "live":
            return _cmd_live(rest)
        if command == "live-smoke":
            return _cmd_live_smoke(rest)
        if command == "safe-mode":
            return _cmd_safe_mode(rest)
        return _cmd_paper(rest)
    except KeyboardInterrupt:
        print("interrupted.", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001 — last-resort handler, mirrors main.main
        logger.exception("unexpected error in %s", command)
        print(f"fatal: unexpected error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


# --------------------------------------------------------------------------
# paper — the long-running run
# --------------------------------------------------------------------------


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

    # The one multi-day daemon in this module: its ERROR/WARNING diagnostics
    # (corrupt funding rows, snapshot write failures, funding-history
    # escalations) need timestamps and logger names for a post-mortem, and its
    # INFO trail (e.g. which plans a restart canceled) must not be dropped.
    # basicConfig no-ops when an embedding application already installed
    # handlers; the single-shot subcommands (export/validate/legacy) stay
    # unconfigured on purpose.
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

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

    import signal

    from ..engine_bridge import _load_risk_decision, _resolve_coin, load_config_or_exit

    # Heavy/engine imports deferred so `export`/`validate` stay light (the
    # engine/scheduler stack is imported by _run_locked once the lease is in
    # hand).
    from ..exchanges.hyperliquid.errors import ExchangeError
    from ..exchanges.hyperliquid.market_data import HyperliquidMarketData
    from ..exchanges.hyperliquid.sdk_client import HyperliquidClient
    from ..paper.clock import WallClock
    from ..paper.config import PaperTradingConfig
    from ..paper.engine import AssetSpec
    from ..paper.run_lock import RunLockError, acquire_run_lock, release_run_lock
    from ..persistence import repository as repo

    config = load_config_or_exit(args.config)
    if config is None:
        return 1
    coin = _resolve_coin(args, config)
    cfgs = _load_risk_decision(config)
    if cfgs is None:
        return 1
    risk_cfg, decision_cfg = cfgs
    # Cannot raise for operator input: _load_risk_decision above already parsed
    # this same block inside its named exit-1 lane (bad values return None).
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
    funding_source = _provider._HistoryFundingSource(market)

    with Database(db_path) as db:
        existing_run = repo.get_run(db.conn, run_id)
        is_restart = existing_run is not None
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
        if existing_run is not None and existing_run["mode"] != "paper":
            # Same identity discipline as the live resume (decided
            # 2026-07-17): a live run's genesis carries the same coin, so the
            # drift check alone would wave a typo'd --run-id/--db through —
            # and the paper daemon would then trade over a LIVE run's books.
            # Checked before the run lock touches the row.
            print(
                f"error: run {run_id!r} in {db_path} is a "
                f"{existing_run['mode']} run — the paper daemon would trade "
                "over its books. Fix --run-id / --db.",
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
            from ..engine_bridge import EngineImportError
            from ..paper import accounting
            from ..paper.engine import PaperExecutionEngine
            from ..paper.market_feed import PortSnapshotProvider
            from ..paper.reconcile import ReconciliationError, reconcile_on_restart
            from ..paper.scheduler import PaperScheduler
            from ..persistence import repository as repo
            from ..persistence.models import PositionState
            from ..persistence.schema import SCHEMA_VERSION

            trading_halted = False
            # Built pre-flight on a fresh run (before the run row exists);
            # a restart builds it after reconciliation settles that it trades.
            provider = None

            def _build_provider():
                return _provider._EngineDecisionProvider(
                    config,
                    risk_cfg=risk_cfg,
                    decision_cfg=decision_cfg,
                    payload_dir=db_path.resolve().parent / "payloads" / run_id,
                )

            if not is_restart:
                # Seeds are genesis-only and the engine manages exactly the
                # run coin: an off-coin seed would sit in the store all run —
                # excluded from the equity the AI sizes against, with no
                # SL/TP, liquidation watch, or funding — silently misstating
                # net worth. Config error, before anything is written.
                off_coin = sorted({p.coin for p in paper_cfg.account.initial_positions} - {coin})
                if off_coin:
                    print(
                        f"error: initial_positions seed coin(s) "
                        f"{', '.join(map(repr, off_coin))} do not match the run "
                        f"coin {coin!r} — a paper run manages exactly one coin. "
                        "Fix paper_trading.account.initial_positions or start a "
                        "separate run for that coin.",
                        file=sys.stderr,
                    )
                    return 1
                # A fresh run always drives the AI — demand the key before
                # writing the run row, so a keyless ``--create`` fails cleanly
                # instead of leaving a half-created run that the next attempt
                # then rejects as "already exists".
                if not _require_api_key():
                    return 1
                # The decision provider is the other operator-fixable
                # pre-flight: its construction triggers the process's first
                # tradingagents import, which detonates on a corrupt repo
                # .env (see _build_engine_config). Same ordering rule as the
                # key check — fail before the run row exists, or the retry
                # after fixing the file is rejected as "already exists".
                try:
                    provider = _build_provider()
                except EngineImportError as exc:
                    print(f"error: {exc}", file=sys.stderr)
                    return 1
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
                # ``existing_run`` was fetched once at the top of the block
                # (is_restart proved it non-None); no writer touches the runs
                # row between there and here.
                drift = _config_drift_report(existing_run["config_json"], config, coin)
                if drift is None:
                    # Stamp clean resumes too, so a reverted config doesn't
                    # leave a stale "drift" as the last word in the store.
                    paper_export._stamp_breadcrumb(db, run_id, "config_drift", "ok", None)
                else:
                    kind, message = drift
                    if kind in _HARD_DRIFT_KINDS:
                        print(f"error: {message}", file=sys.stderr)
                        return 1
                    logger.warning("config drift on resume for %s: %s", run_id, message)
                    print(f"WARNING: {message}", file=sys.stderr)
                    paper_export._stamp_breadcrumb(db, run_id, "config_drift", "drift", message)
                # The same single-coin invariant as the fresh-run seed guard,
                # enforced at the other daemon entry point: a store created by
                # direct initialize_run (or an older build) may hold off-coin
                # positions that replay verifies symmetrically yet every
                # equity/protection computation silently excludes. Checked
                # before reconcile writes anything.
                off_coin = sorted(
                    p.coin
                    for p in repo.get_all_current_positions(db.conn, run_id)
                    if p.coin != coin and not p.is_flat
                )
                if off_coin:
                    print(
                        f"error: run {run_id!r} holds open position(s) in "
                        f"{', '.join(map(repr, off_coin))} but this daemon "
                        f"manages only {coin!r} — they would be excluded from "
                        "equity and SL/TP protection. The paper daemon cannot "
                        "run this store (export/validate still work).",
                        file=sys.stderr,
                    )
                    return 1
                try:
                    report = reconcile_on_restart(
                        db, run_id=run_id, now=now, funding_source=funding_source
                    )
                except ReconciliationError as exc:
                    # Flat run over corrupt/unverifiable books: refuse to start
                    # — but leave the durable breadcrumb first ("mismatch" or
                    # "failed" per the refusal's lane), so the refusal is
                    # visible to a post-mortem even when stderr wasn't captured.
                    paper_export._stamp_breadcrumb(
                        db, run_id, "replay", exc.replay_status, str(exc)
                    )
                    print(f"error: {exc}", file=sys.stderr)
                    return 1
                paper_export._stamp_breadcrumb(
                    db, run_id, "replay", report.replay_status, report.replay_error
                )
                trading_halted = report.replay_mismatch
                if report.replay_mismatch:
                    logger.error("restart over unverifiable books: %s", report.replay_error)
                    print(
                        "ERROR: accounting replay could not verify this run's "
                        "books on restart — running in protection-only mode: "
                        "SL/TP protection and the market monitor stay live, NEW "
                        "decision cycles stay halted until the store verifies "
                        "again.",
                        file=sys.stderr,
                    )
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
            halt_reason = "replay" if trading_halted else None
            if not trading_halted and not os.environ.get("OPENROUTER_API_KEY"):
                # Only the restart lane reaches here keyless — a fresh run
                # demanded the key before writing the run row. A keyless
                # healthy restart over live work must NOT exit: reconcile
                # already canceled its plans, so exiting would leave the
                # position with nobody watching SL/TP — the exact harm
                # protection-only mode exists to prevent (a replay-mismatch
                # restart, with *less* trustworthy books, already gets that
                # protection). Flat, there is nothing to protect and the
                # plain abort stands.
                if engine.has_active_work():
                    trading_halted = True
                    halt_reason = "missing-key"
                    logger.error(
                        "OPENROUTER_API_KEY missing on restart of %s with a live "
                        "position — entering protection-only mode",
                        run_id,
                    )
                    print(
                        "ERROR: OPENROUTER_API_KEY is not set but this run holds "
                        "a live position — running in protection-only mode: SL/TP "
                        "protection and the market monitor stay live, NEW decision "
                        "cycles stay halted. Set the key and restart to resume "
                        f"trading. ({dotenv_diagnosis('OPENROUTER_API_KEY')}.)",
                        file=sys.stderr,
                    )
                else:
                    _require_api_key()  # prints the standard abort message
                    return 1
            if not trading_halted and provider is None:
                # Only the healthy restart lane reaches here without a provider
                # — the fresh lane built it pre-flight. Same protection-only
                # rule as the keyless restart above: an EngineImportError is
                # operator-fixable (see _build_engine_config for the causes),
                # so over live work exiting would leave the position with
                # nobody watching SL/TP; flat, the named exit 1 stands.
                try:
                    provider = _build_provider()
                except EngineImportError as exc:
                    if engine.has_active_work():
                        trading_halted = True
                        halt_reason = "import-error"
                        logger.error(
                            "tradingagents import failed on restart of %s with "
                            "a live position — entering protection-only mode: %s",
                            run_id,
                            exc,
                        )
                        print(
                            f"ERROR: {exc}\nThis run holds a live position — "
                            "running in protection-only mode: SL/TP protection "
                            "and the market monitor stay live, NEW decision "
                            "cycles stay halted. Fix the environment and "
                            "restart to resume trading.",
                            file=sys.stderr,
                        )
                    else:
                        print(f"error: {exc}", file=sys.stderr)
                        return 1
            if trading_halted:
                # Protection-only never polls the AI, and nothing ever un-halts
                # a protection-only process — the loop never touches the
                # scheduler. Skip building it (and the decision provider's deep
                # tradingagents import behind it): each would only add failure
                # modes to a startup whose one job is keeping SL/TP alive, the
                # same principle that lets this mode start keyless.
                scheduler = None
                stranded = repo.find_in_progress_attempt(db.conn, run_id)
                if stranded is not None:
                    # Purely informational: only a healthy restart's first
                    # poll may finish this attempt (§3.1 resumes the SAME
                    # attempt, without burning its retry budget) — terminalizing
                    # it here would destroy that resumable state and write a
                    # next_decision_at onto books this mode exists to distrust.
                    attempt_id = stranded["decision_attempt_id"]
                    logger.warning(
                        "decision attempt %s remains in_progress; protection-only "
                        "never polls the scheduler, so it stays open until the "
                        "next healthy restart resumes it",
                        attempt_id,
                    )
                    print(
                        f"note: decision attempt {attempt_id!r} from the previous "
                        "process remains in_progress — protection-only mode never "
                        "resumes it; the next healthy restart will.",
                        file=sys.stderr,
                    )
            else:
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
                return _paper_loop(
                    db,
                    run_id,
                    engine,
                    scheduler,
                    clock,
                    interval,
                    export_dir,
                    funding_source,
                    trading_halted=trading_halted,
                    halt_reason=halt_reason,
                )
            except KeyboardInterrupt:
                print("\nshutting down — final export...", file=sys.stderr)
                # Same last-resolution pass as the settle-exit lane: pending
                # funding posted now is funding the final CSVs won't be missing.
                paper_export._retry_pending_funding(
                    db, run_id, now=clock.now(), funding_source=funding_source
                )
                paper_export._post_cycle_export(db, run_id, export_dir)
                return 0
            except RunLockError as exc:
                # The lease was taken over while this process stalled: the
                # successor already reconciled away this process's plans and
                # owns the run now. Exit WITHOUT the shutdown export or any
                # other store write — each one would corrupt the successor's
                # view (the pid-guarded release below no-ops for the same
                # reason).
                logger.error("run lease lost: %s", exc)
                print(f"error: {exc}", file=sys.stderr)
                return 1

        try:
            return _run_locked()
        finally:
            # Same guard as the live path: a raise here would clobber
            # _run_locked()'s exit code into a generic exit 2.
            try:
                release_run_lock(db, run_id, pid=os.getpid(), now=clock.now())
            except Exception:  # noqa: BLE001
                logger.exception("run-lock release failed (the exit code above stands)")


def _paper_loop(
    db,
    run_id,
    engine,
    scheduler,
    clock,
    interval: int,
    export_dir: Path,
    funding_source,
    *,
    trading_halted: bool,
    halt_reason: str | None = None,
) -> int:
    """The production loop: tick (when the monitor is on) → poll → sleep.

    Tick before poll so a restart handles liquidation / gap SL / TP on the
    reconciled position *before* the immediate new cycle plans against it
    (execution §1.2 step 6 before step 8). Sleep is bounded by the monitor
    interval while any market work is live (execution §5.5) and stretches
    toward ``next_decision_at`` when everything is quiet, so an idle run
    issues no market-data requests.

    A replay mismatch stops NEW decisions (the books can no longer be trusted
    for sizing) but keeps the engine ticking — SL/TP protection and the market
    monitor must survive (spec §5 / data §1.1). ``trading_halted=True`` enters
    that mode from the first iteration (a restart over mismatched books with a
    live position). Because halted mode never polls the scheduler, it retries
    pending funding on its own wall-clock cadence
    (``_HALTED_FUNDING_RETRY_SECONDS``) instead of at cycle boundaries, and
    both exit lanes (settle-exit below, Ctrl-C/SIGTERM in the caller) give
    pending funding one last resolution pass before the closing export.

    Protection-only mode exists *for* that live position — once it is gone
    (SL/TP closed it and nothing else is live), halted trading means there is
    nothing left this process may ever do. Rather than idle as a zombie for
    days (holding the lease, with the closing fill never reaching the CSVs),
    it exports the final state and returns exit code 1 so a supervisor or
    operator gets the signal to investigate the store.

    Every iteration refreshes the single-instance lease, so the sleep is
    capped at 60s (well inside ``LOCK_STALE_SECONDS``). The cap bounds only
    the wake cadence, not the market-data request rate: ``engine.tick()`` is
    throttled to the configured monitor interval, keeping the two cadences
    decoupled (defensive — config already rejects intervals above the 30s TWAP
    slice cadence; early wakes touch only SQLite). A superseded
    lease raises
    :class:`~.paper.run_lock.RunLockError` out of the loop (see
    :func:`~.paper.run_lock.heartbeat_run_lock`).
    """
    from ..paper.reconcile import backfill_pending_funding
    from ..paper.run_lock import heartbeat_run_lock
    from ..paper.scheduler import CycleEvent

    pid = os.getpid()
    next_tick_at: datetime | None = None  # None → the first tick fires immediately
    # Halted mode never polls the scheduler, so it never reaches the
    # cycle-terminal funding retry below — without its own timer, pending
    # funding would stay unposted for the run's whole halted lifetime
    # (execution §6.5's 稍後補帳). Both entries into halted mode have just run
    # a backfill (restart reconciliation, or the cycle-terminal retry in the
    # iteration that halts mid-run), so the first in-loop retry waits a full
    # period.
    next_funding_retry_at: datetime | None = (
        clock.now() + timedelta(seconds=paper_export._HALTED_FUNDING_RETRY_SECONDS)
        if trading_halted
        else None
    )
    while True:
        now = clock.now()
        heartbeat_run_lock(db, run_id, pid=pid, now=now)
        # Tick cadence is the configured monitor interval, decoupled from the
        # 60s heartbeat wake cap below: were the interval ever to exceed the
        # cap, the loop would wake for the lease but skip the tick (and its
        # market-data fetch) until the interval elapsed. Config currently
        # rejects intervals above the 30s TWAP slice cadence, so this is a
        # defensive invariant, not an operator-facing mode.
        if engine.has_active_work() and (next_tick_at is None or now >= next_tick_at):
            engine.tick()
            next_tick_at = now + timedelta(seconds=interval)
        if trading_halted and next_funding_retry_at is not None and now >= next_funding_retry_at:
            paper_export._retry_pending_funding(db, run_id, now=now, funding_source=funding_source)
            next_funding_retry_at = now + timedelta(
                seconds=paper_export._HALTED_FUNDING_RETRY_SECONDS
            )
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
                if not paper_export._post_cycle_export(db, run_id, export_dir):
                    trading_halted = True
                    halt_reason = "replay"
                    # The cycle-terminal backfill above just ran; start the
                    # halted-mode funding-retry timer one full period out.
                    next_funding_retry_at = clock.now() + timedelta(
                        seconds=paper_export._HALTED_FUNDING_RETRY_SECONDS
                    )
                    # Mirror the restart lane's cancel sweep: the halting cycle
                    # may have just started a plan (zero slices consumed), and
                    # letting it fill — or a flip open its reverse leg — would
                    # build exposure on the very books that failed to verify,
                    # diverging from what a kill-and-restart would produce.
                    canceled = engine.cancel_active_plans()
                    logger.error(
                        "halting NEW decision cycles for %s: accounting replay "
                        "can no longer rebuild the books%s",
                        run_id,
                        " (in-flight execution plan canceled)" if canceled else "",
                    )
                    print(
                        "ERROR: accounting replay can no longer rebuild this run's "
                        "books — halting NEW decision cycles and canceling any "
                        "in-flight execution plan. The engine keeps ticking "
                        "(SL/TP protection and monitor stay live). "
                        "Investigate the store; a restart stays in this "
                        "protection-only mode until the books verify again.",
                        file=sys.stderr,
                    )
            if result.event is CycleEvent.API_FAILED:
                logger.warning(
                    "decision API failed %s times for %s — holding position until the next cycle",
                    result.attempt_count,
                    run_id,
                )
                print(
                    f"decision API failed {result.attempt_count} times — holding "
                    "position until the next cycle.",
                    file=sys.stderr,
                )
        # One post-tick/poll read serves both the exit check and the delay
        # branch — nothing between them mutates the engine's work state.
        active = engine.has_active_work()
        if trading_halted and not active:
            # Protection-only mode exists for a live position; with the
            # position closed (or none left) and new cycles halted, no future
            # iteration can ever do anything — a zombie would hold the lease
            # for days while the closing fill never reaches the CSVs. Export
            # the final state and exit loud so a supervisor/operator gets the
            # signal.
            logger.error(
                "protection-only run %s has nothing left to protect — "
                "exporting final state and exiting",
                run_id,
            )
            if halt_reason == "missing-key":
                print(
                    "protection-only mode has nothing left to protect (the "
                    "position is closed and new cycles stayed halted for the "
                    "missing OPENROUTER_API_KEY) — exporting the final state "
                    "and exiting. Set the key to resume this run.",
                    file=sys.stderr,
                )
            elif halt_reason == "import-error":
                print(
                    "protection-only mode has nothing left to protect (the "
                    "position is closed and new cycles stayed halted because "
                    "the tradingagents engine failed to import) — exporting "
                    "the final state and exiting. Fix the environment (see "
                    "the startup error) to resume this run.",
                    file=sys.stderr,
                )
            else:
                print(
                    "protection-only mode has nothing left to protect (the "
                    "position is closed and new cycles stay halted) — exporting "
                    "the final state and exiting. The books never re-verified; "
                    "investigate the store before starting a new run.",
                    file=sys.stderr,
                )
            # The final CSVs should be as complete as the store allows: give
            # any still-pending funding one last resolution pass first.
            paper_export._retry_pending_funding(
                db, run_id, now=clock.now(), funding_source=funding_source
            )
            paper_export._post_cycle_export(db, run_id, export_dir)
            return 1
        now = clock.now()
        due = scheduler.next_due_at() if not trading_halted else None
        # Halted-with-work and pending-retry (due is None while not halted)
        # both tick at the monitor interval; the halted-and-idle case exited
        # above, so ``due is None`` can no longer mean "idle heartbeat".
        delay = float(interval) if active or due is None else max(1.0, (due - now).total_seconds())
        # The 60s cap keeps the lease heartbeat fresh; waking early is a cheap
        # SQLite poll — engine.tick() above is throttled to the configured
        # interval, so an early wake never issues extra market-data requests.
        time.sleep(min(delay, 60.0))
