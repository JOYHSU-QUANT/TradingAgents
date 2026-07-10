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
(``validate`` only) the run is internally consistent but has not accumulated
the 30-cycle gate yet ("keep running cycles"), ``5`` (``validate`` only) the
run has integrity failures — orphans, snapshot or replay mismatches, or a
store so corrupt the checks themselves cannot run ("the store is broken;
investigate before trusting results"), ``130`` interrupted before a graceful
lane could take over (Ctrl-C in ``export``/``validate`` or during ``paper``
startup/reconciliation — SIGTERM likewise once ``paper`` has installed its
handler; once the loop runs, both signals take the shutdown-export lane
instead of ``130``). Legacy delegated invocations keep :mod:`.main`'s own
exit contract.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import CONFIG_LOAD_ERRORS, dotenv_diagnosis, load_config, load_dotenv_files
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
    # Before anything reads os.environ: the OPENROUTER_API_KEY startup checks
    # (fresh `paper` runs, healthy keyless-restart triage) run long before the
    # lazily-imported engine package would load the .env files itself.
    load_dotenv_files()
    argv = list(sys.argv[1:]) if argv is None else list(argv)
    if not argv or argv[0].startswith("-"):
        # Phase 1/2 compatibility path: identical flags, identical behaviour.
        # Legacy accepts no positionals, so flag-shaped/empty argv is lossless.
        from .main import main as legacy_main

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

    try:
        db = _open_existing_db(args.db)
        if db is None:
            return 1
        with db:
            report = validate_run(db, run_id=args.run_id)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except sqlite3.Error as exc:
        # The store cannot even be read (malformed file, I/O failure mid-scan):
        # the strongest possible "investigate the store" signal — the same
        # exit-5 verdict as a failing report, not a generic tool crash.
        print(f"error: store integrity failure — {exc}", file=sys.stderr)
        return 5
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


# Every behaviour-defining block the resume drift check compares — the single
# source shared by the genesis record and the comparison loop, so a new key
# can't be recorded but silently never drift-checked (or vice versa).
_DRIFT_COMPARED_KEYS = ("risk", "decision", "paper_trading", "engine", "market_data", "indicators")


def _run_config_subset(config: dict, coin: str) -> dict:
    """The behaviour-defining blocks recorded at run genesis (never network/wallet).

    ``engine`` (model/analysts), ``market_data`` (candle window feeding the
    context), and ``indicators`` (signal set + warm-up gate) are here because
    each redefines every subsequent decision at least as much as a
    risk-parameter tweak does; ``paper_trading`` is stored whole for the audit
    record even though only its ``execution`` sub-block is compared on resume
    (``account`` is genesis-only).
    """
    subset: dict = {key: config.get(key) for key in _DRIFT_COMPARED_KEYS}
    subset["coin"] = coin
    return subset


# Genesis records written before these keys joined the subset lack them;
# absence there means "unknown", not "was empty" — skip the comparison rather
# than false-flag every pre-upgrade run whose config carries the block today.
_DRIFT_KEYS_ADDED_LATER = frozenset({"engine", "market_data", "indicators"})


def _resume_effective(key: str, block: object) -> object:
    """Project a stored/current config block onto what actually applies on resume.

    ``paper_trading.account`` (initial balance, seed positions) is consumed
    only at run genesis — a resume-time edit there changes nothing, so it must
    not trip the "behaviour changes from here on" warning. Everything else
    applies as-is.
    """
    if key == "paper_trading" and isinstance(block, dict):
        return block.get("execution")
    return block


def _config_drift_report(
    stored_json: str | None, config: dict, coin: str
) -> tuple[str, str] | None:
    """Compare today's config against the run's genesis record.

    Returns ``("coin", msg)`` for a coin mismatch (hard error — a different
    instrument is a different run, not a resumption), ``("params", msg)`` for
    risk/decision/paper_trading(execution)/engine/market_data/indicators drift
    (warning — behaviour changes mid-run but the operator may intend it;
    genesis-only ``paper_trading.account`` edits are inert on resume and don't
    warn, and a genesis record predating a key in ``_DRIFT_KEYS_ADDED_LATER``
    skips that comparison rather than false-flagging), or ``None`` when
    nothing drifted or no record
    exists (a pre-drift-check store). A genesis record this process cannot
    parse also reports as ``("params", ...)``: the homogeneity check became
    impossible, which is breadcrumb-grade — never a startup abort (that would
    fire before the protection-only fork, leaving a live position unwatched).
    """
    if not stored_json:
        return None
    try:
        stored = json.loads(stored_json)
    except ValueError as exc:
        return ("params", f"could not verify config drift (corrupt stored config_json: {exc})")
    if not isinstance(stored, dict):
        return (
            "params",
            "could not verify config drift (corrupt stored config_json: not an object)",
        )
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
        key
        for key in _DRIFT_COMPARED_KEYS
        if not (key in _DRIFT_KEYS_ADDED_LATER and key not in stored)
        and _resume_effective(key, stored.get(key)) != _resume_effective(key, current.get(key))
    )
    if drifted:
        return (
            "params",
            f"config drift on resume: {', '.join(drifted)} differ from the values "
            "recorded at run creation — this run's behaviour changes from here on.",
        )
    return None


def _require_api_key() -> bool:
    """True when OPENROUTER_API_KEY is set; else print the abort message.

    Checked only on paths that will actually drive the AI engine — a fresh run
    (always, before the run row is written) and a healthy restart with nothing
    live to protect. A restart into protection-only mode never polls the AI,
    so it runs keyless — and a keyless healthy restart holding live work falls
    back to that same mode rather than exiting (the caller owns that fork:
    reconcile has already canceled the plans, so exiting would leave the
    position with nobody watching its SL/TP).
    """
    if os.environ.get("OPENROUTER_API_KEY"):
        return True
    print(
        "error: OPENROUTER_API_KEY is not set — the paper run drives the AI engine "
        "every 4h. Use --context-only (legacy CLI) for a keyless dev loop. "
        f"({dotenv_diagnosis('OPENROUTER_API_KEY')}.)",
        file=sys.stderr,
    )
    return False


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

    # Heavy/engine imports deferred so `export`/`validate` stay light (the
    # engine/scheduler stack is imported by _run_locked once the lease is in
    # hand).
    from .exchanges.hyperliquid.errors import ExchangeError
    from .exchanges.hyperliquid.market_data import HyperliquidMarketData
    from .exchanges.hyperliquid.sdk_client import HyperliquidClient
    from .main import _load_risk_decision, _resolve_coin
    from .paper.clock import WallClock
    from .paper.config import PaperTradingConfig
    from .paper.engine import AssetSpec
    from .paper.run_lock import RunLockError, acquire_run_lock, release_run_lock
    from .persistence import repository as repo

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
            from .main import EngineImportError
            from .paper import accounting
            from .paper.engine import PaperExecutionEngine
            from .paper.market_feed import PortSnapshotProvider
            from .paper.reconcile import ReconciliationError, reconcile_on_restart
            from .paper.scheduler import PaperScheduler
            from .persistence import repository as repo
            from .persistence.models import PositionState
            from .persistence.schema import SCHEMA_VERSION

            trading_halted = False
            # Built pre-flight on a fresh run (before the run row exists);
            # a restart builds it after reconciliation settles that it trades.
            provider = None

            def _build_provider():
                return _EngineDecisionProvider(
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
                run_row = repo.get_run(db.conn, run_id)
                assert run_row is not None  # is_restart established the row exists
                drift = _config_drift_report(run_row["config_json"], config, coin)
                if drift is None:
                    # Stamp clean resumes too, so a reverted config doesn't
                    # leave a stale "drift" as the last word in the store.
                    _stamp_breadcrumb(db, run_id, "config_drift", "ok", None)
                else:
                    kind, message = drift
                    if kind == "coin":
                        print(f"error: {message}", file=sys.stderr)
                        return 1
                    logger.warning("config drift on resume for %s: %s", run_id, message)
                    print(f"WARNING: {message}", file=sys.stderr)
                    _stamp_breadcrumb(db, run_id, "config_drift", "drift", message)
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
                    _stamp_breadcrumb(db, run_id, "replay", exc.replay_status, str(exc))
                    print(f"error: {exc}", file=sys.stderr)
                    return 1
                _stamp_breadcrumb(db, run_id, "replay", report.replay_status, report.replay_error)
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
                _retry_pending_funding(db, run_id, now=clock.now(), funding_source=funding_source)
                _post_cycle_export(db, run_id, export_dir)
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
            release_run_lock(db, run_id, pid=os.getpid(), now=clock.now())


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
    from .paper.reconcile import backfill_pending_funding
    from .paper.run_lock import heartbeat_run_lock
    from .paper.scheduler import CycleEvent

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
        clock.now() + timedelta(seconds=_HALTED_FUNDING_RETRY_SECONDS) if trading_halted else None
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
            _retry_pending_funding(db, run_id, now=now, funding_source=funding_source)
            next_funding_retry_at = now + timedelta(seconds=_HALTED_FUNDING_RETRY_SECONDS)
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
                    halt_reason = "replay"
                    # The cycle-terminal backfill above just ran; start the
                    # halted-mode funding-retry timer one full period out.
                    next_funding_retry_at = clock.now() + timedelta(
                        seconds=_HALTED_FUNDING_RETRY_SECONDS
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
            _retry_pending_funding(db, run_id, now=clock.now(), funding_source=funding_source)
            _post_cycle_export(db, run_id, export_dir)
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


def _stamp_breadcrumb(db, run_id: str, kind: str, status: str, error: str | None) -> None:
    """Durably stamp a ``scheduler_state`` breadcrumb trio (``last_<kind>_*``).

    Export failures, replay outcomes, and config drift on resume are
    warn-and-carry-on (the mid-run replay halt only lives in process memory) —
    without this record none would leave any trace once the process exits.
    ``kind`` is a code-owned literal ("export" / "replay" / "config_drift");
    the column vocabulary stays validated by ``upsert_scheduler_state``'s
    fixed keyword signature.
    """
    from .persistence import repository as repo

    stamp = datetime.now(timezone.utc)
    with db.transaction() as conn:
        repo.upsert_scheduler_state(
            conn,
            run_id,
            updated_at=stamp,
            **{
                f"last_{kind}_status": status,
                f"last_{kind}_error": error,
                f"last_{kind}_at": stamp,
            },
        )


_UNVERIFIED_MARKER = "REPLAY_UNVERIFIED.json"


def _mark_export_verification(
    export_dir: Path, run_id: str, replay_ok: bool, reason: str | None
) -> None:
    """Record, in-band next to the CSVs, whether the exported set passed replay.

    ``scheduler_state`` (which carries the ``last_replay_*`` breadcrumb) is NOT
    one of the exported tables, so a consumer reading the CSVs alone cannot tell
    a mismatch cycle's export from a healthy one. When replay did not verify we
    drop ``REPLAY_UNVERIFIED.json`` beside the CSVs; when it verifies we remove
    any stale marker a previous bad cycle left behind (the export dir is reused
    every cycle). Best-effort: a marker failure is logged, never raised — the
    loop must survive, and the ``last_replay_*`` breadcrumb remains authoritative.
    """
    marker = Path(export_dir) / _UNVERIFIED_MARKER
    try:
        if replay_ok:
            marker.unlink(missing_ok=True)
        else:
            marker.write_text(
                json.dumps(
                    {"run_id": run_id, "replay_verified": False, "reason": reason},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
    except OSError as exc:
        logger.error("failed to update replay-verification marker for %s: %s", run_id, exc)


def _post_cycle_export(db, run_id: str, export_dir: Path) -> bool:
    """Replay-verify then export (phase2-data §1.1); returns whether the books verified.

    An export failure never stops trading (spec: record ``export_failed`` and
    carry on) — but it IS durably recorded on ``scheduler_state``
    (``last_export_status`` / ``last_export_error`` / ``last_export_at``), so a
    post-mortem can tell how long the CSV view had been stale even when stderr
    was not captured. A replay mismatch or replay *failure* returns ``False``
    so the caller can stop opening new positions on unverifiable books; both
    outcomes (and healthy verifications) stamp the ``last_replay_*`` breadcrumb.
    """
    from .paper.reconcile import classify_replay
    from .persistence.export import ExportError, export_run

    # classify_replay contains a raising replay ("failed" — unverifiable books
    # are treated like inconsistent ones), so this lane cannot kill the loop.
    replay_status, replay_detail, _cause = classify_replay(db, run_id=run_id)
    replay_ok = replay_status == "ok"
    if replay_status == "mismatch":
        logger.error("accounting replay mismatch for %s: %s", run_id, replay_detail)
        print(
            f"WARNING: accounting replay mismatch for {run_id!r}: {replay_detail} — "
            "investigate before trusting this run's results.",
            file=sys.stderr,
        )
    elif replay_status == "failed":
        logger.error("accounting replay failed for %s: %s", run_id, replay_detail)
        print(f"WARNING: accounting replay failed: {replay_detail}", file=sys.stderr)
    _stamp_breadcrumb(db, run_id, "replay", replay_status, replay_detail)
    export_ok = False
    try:
        export_run(db, run_id=run_id, output_dir=export_dir)
        export_ok = True
        _stamp_breadcrumb(db, run_id, "export", "ok", None)
        print(f"exported CSVs to {export_dir}", file=sys.stderr)
    except ExportError as exc:
        # §1.1: record export_failed, keep the monitor and protections running.
        logger.error("export_failed for %s: %s", run_id, exc)
        print(f"WARNING: export_failed — {exc}", file=sys.stderr)
        _stamp_breadcrumb(db, run_id, "export", "failed", str(exc))
    # Mark the freshly written set as unverified when replay didn't pass, so a
    # consumer reading the CSVs alone isn't misled (see _mark_export_verification).
    # Only when a full set was actually (re)written — a failed export leaves the
    # previous set and its marker untouched.
    if export_ok:
        _mark_export_verification(export_dir, run_id, replay_ok, replay_detail)
    return replay_ok


# Protection-only mode never reaches the cycle-terminal funding retry (the
# scheduler is never polled), so the loop retries on this wall-clock cadence
# instead. Hour-grained to match funding's own settlement granularity; when
# nothing is pending the pass is a single cheap SQLite query.
_HALTED_FUNDING_RETRY_SECONDS = 3600


def _retry_pending_funding(db, run_id: str, *, now: datetime, funding_source) -> None:
    """Best-effort pending-funding retry for the protection and shutdown lanes.

    The cycle-terminal lane calls :func:`backfill_pending_funding` directly and
    stays fail-loud (a store-level error there must kill the loop — pinned by
    test). These lanes exist to keep SL/TP alive (the halted-mode timer) or to
    flush the most complete final CSVs the store allows (settle-exit and
    Ctrl-C/SIGTERM exports) — a raising retry must not take either down, so any
    failure is contained to an ERROR log and the hourly timer (or the export
    itself) carries on. ``record_funding`` posts exactly-once, so repeated
    passes are safe.
    """
    from .paper.reconcile import backfill_pending_funding

    try:
        backfill_pending_funding(db, run_id=run_id, now=now, funding_source=funding_source)
    except Exception:
        logger.exception("pending-funding retry failed for %s (best-effort lane)", run_id)


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
            # Not enough closed candles for real signal. Deliberate (reviewed):
            # this rides the §3.1 ladder as "server_error" → api_failed — the
            # closed §6.2 vocabulary has no data-availability label. A gappy
            # feed heals by the next try/cycle; a too-young listing produces a
            # recurring api_failed cycle every 4h until it warms up (no AI
            # spend — the failure precedes the call).
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
    if "rate limit" in text or "ratelimit" in text or "429" in text:
        return "rate_limit"
    if "connection" in text or "connect" in text or "network" in text:
        return "connection"
    return "server_error"
