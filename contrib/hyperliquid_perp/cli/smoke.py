"""The ``live-smoke`` subcommand: the §20.2 testnet smoke checklist (PR 6)."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from ..persistence.db import SchemaVersionError, apply_migrations
from ._common import (
    _existing_run_row,
    _open_existing_db,
    _raise_keyboard_interrupt,
    _require_live_run_mode,
)
from ._drift import _HARD_DRIFT_KINDS, _config_drift_report
from .live_shared import (
    _RECOVERY_MAX_TICK_GAP_SECONDS,
    _conflicting_run_lease,
    _print_smoke_gate,
    _timing_preflight,
)

# The narrowest dead-man cover the smoke suite will run under, whatever the
# config says. The suite refreshes once per TEST rather than on a fixed tick,
# and a test is an unbounded place/poll/cancel round-trip, so the config's
# daemon-shaped invariant does not bound it. 120s is what the suite guaranteed
# before the value was wired from config at all (2026-07-31).
_SMOKE_MIN_KILL_SWITCH_DEADLINE = timedelta(seconds=120)


def _cmd_live_smoke(argv: list[str]) -> int:
    """Run the §20.2 testnet smoke checklist and report the cycle-entry gate.

    Each of the 18 tests drives a real signed exchange action against testnet and
    records its verdict in ``live_smoke_tests``; the gate (§20.2: all pass) then
    lets ``live --loop`` start the testnet_live cycles. ``--gate-status`` reports
    the stored gate without touching the network; ``--dry-run`` validates the
    config and wiring and records every selected test ``skipped`` (places no
    orders — the offline check the unit tests exercise). A real run takes the
    run's lease first (refused with exit 1 while ``live``/``paper`` holds it) and,
    when the selection places probe orders, runs one passing §19.1 pre-flight
    recovery before the first test. Exit: 0 = the FULL §20.2 gate is open (every
    one of the 18 tests' latest real result is ``passed``) — so ``--only`` on a
    subset still exits 4 until the whole suite has passed — EXCEPT ``--dry-run``,
    which exits 0 once the wiring check completes (its gate is never open); 4 =
    ran (or read) but the gate is not satisfied, including a pre-flight recovery
    failure that aborted the suite before any test; 1 = a named config / env /
    network / lease error.

    The restart tests (15–17) EACH drive a real §19.1 startup recovery (three
    arms and three reconcile passes over the run, not one);
    the operator stages their preconditions (an existing position / a stale
    bot-owned order) on testnet before running the suite (docs/RUNBOOK-live.md).
    """
    parser = argparse.ArgumentParser(
        prog="python -m contrib.hyperliquid_perp live-smoke",
        description=(
            "Run the phase3-spec §20.2 testnet smoke checklist against a live run, "
            "record each verdict, and report the cycle-entry gate."
        ),
    )
    parser.add_argument("--config", default=None, help="Config YAML path.")
    parser.add_argument("--db", default="live_trading.db", help="SQLite store path for the run.")
    parser.add_argument(
        "--run-id", required=True, help="The live run to record smoke results under."
    )
    parser.add_argument(
        "--only",
        nargs="+",
        default=None,
        metavar="TEST_KEY",
        help="Run only these smoke-test keys (default: all 18, in canonical order).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate config + wiring and record every selected test skipped; place NO orders.",
    )
    parser.add_argument(
        "--gate-status",
        action="store_true",
        help="Print the §20.2 gate for the run from the store and exit (no network, no orders).",
    )
    args = parser.parse_args(argv)

    from ..live.smoke import (
        SmokePreflightError,
        SmokeTestRunner,
        smoke_gate_report,
        validate_only_keys,
    )

    # Mutual exclusion BEFORE key validation: under --gate-status the --only
    # keys are never going to be used, so answering a typo in them names the
    # wrong mistake and sends the operator off correcting a key instead of
    # dropping the flag.
    if args.gate_status and (args.dry_run or args.only is not None):
        print(
            "error: --gate-status only reads the stored gate — drop --dry-run/--only.",
            file=sys.stderr,
        )
        return 1
    only: list[str] | None = None
    if args.only is not None:
        try:
            only = list(validate_only_keys(args.only))
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # --gate-status: a pure store read — no config, no network, no orders.
    if args.gate_status:
        db = _open_existing_db(args.db)
        if db is None:
            return 1
        with db:
            run_row = _existing_run_row(db.conn, args.run_id, args.db)
            if run_row is None:
                return 1
            if not _require_live_run_mode(
                run_row, args.run_id, args.db, extra="smoke results are live-run state"
            ):
                return 1
            # The §20.2 gate is testnet-only state: a mainnet_tiny run's
            # live_smoke_tests is empty BY DESIGN (§21.3 — smoke is proven on
            # the separate testnet run), so reporting its raw buckets would
            # print "not_yet_run: <all 18>" + exit 4 and read as "go smoke-test
            # mainnet" — the exact misreading `validate` renders as "n/a
            # (§21.3)". Refuse, mirroring the real-run testnet-only guard
            # (decision 2026-07-28).
            from ..live.validation import execution_mode

            genesis_mode = execution_mode(run_row["config_json"])
            if genesis_mode != "testnet_live":
                print(
                    f"error: run {args.run_id!r} has genesis live.mode "
                    f"{genesis_mode!r} — the §20.2 smoke gate applies only to "
                    "testnet_live runs (a mainnet run relies on the smoke "
                    "proven on the separate testnet run, §21.3); there is no "
                    "smoke gate to report here.",
                    file=sys.stderr,
                )
                return 1
            passed, missing, failed, errored = smoke_gate_report(db.conn, args.run_id)
            _print_smoke_gate(passed, missing, failed, errored)
            return 0 if passed else 4

    # This command OWNS the db handle: opened here, closed in the finally no
    # matter how the build or the run fails (a raise, not just an int return) —
    # the try/finally makes the cleanup structural, not dependent on every
    # _build_* failure path returning an int (silent-failure review, 2026-07-27).
    # Schema policy splits on --dry-run, not on the command:
    #
    # * a dry run takes NO lease (see below) and touches only this store to check
    #   wiring, which makes it a reporting command by the same test `validate` and
    #   `--gate-status` are judged by. Migrating there was the sharpest remaining
    #   version of the hazard the reporting/owning split exists to close: the one
    #   command advertised as the safe offline check was the one that silently
    #   upgraded a store a running daemon owned.
    # * a real run does own the store — but it cannot take the lease until the
    #   store is open, so migrating AT open still upgraded the schema underneath a
    #   sibling daemon and only then reached the conflict check that refuses. It
    #   defers instead and migrates below, once it actually owns the run.
    db = _open_existing_db(args.db, defer_migration=not args.dry_run)
    if db is None:
        return 1
    preflight_error: str | None = None
    import signal

    from ..paper.run_lock import RunLockError, acquire_run_lock, release_run_lock

    try:
        session = _build_smoke_session(args, db)
        if isinstance(session, int):
            return session
        lock_pid: int | None = None
        if not args.dry_run:
            # The suite places real orders and runs §19.1 recoveries — the same
            # actions the run lease exists to keep single-owner. A concurrent
            # `live --loop` on this run would race the probe orders, the
            # recovery's stale-order sweep, and the account-wide kill switch;
            # refuse instead (a dry run touches only this store, no lease needed).
            # The lease is per-run_id; the damage this suite can do is per-WALLET.
            # A sibling run in the same store shares the wallet, so its lease
            # would be acquired happily while this suite arms/clears the
            # ACCOUNT-WIDE kill switch, writes the account's leverage, and runs a
            # §19.3 sweep whose bot-ownership lookup is not run-scoped — i.e. it
            # cancels the sibling's resting orders. Refuse before any of that
            # (2026-07-30 concurrency review).
            conflict = _conflicting_run_lease(db, args.run_id)
            if conflict is not None:
                other_run, other_pid = conflict
                print(
                    f"error: run {other_run!r} in {args.db} is being driven by pid "
                    f"{other_pid} right now, on this same network. "
                    "The smoke suite's kill-switch arm/clear, updateLeverage and §19.3 "
                    "stale-order sweep are ACCOUNT-wide, not run-scoped, so running it "
                    "now would strip that run's dead-man cover and cancel its resting "
                    "orders. Stop that process, or wait for its lease to go stale. "
                    "Moving either run to a different --db does NOT help: the hazard "
                    "is per-WALLET and same-network runs share the wallet, so a "
                    "separate store only hides them from this check. A run on the "
                    "OTHER network is a different exchange and does not conflict.",
                    file=sys.stderr,
                )
                return 1
            try:
                acquire_run_lock(db, args.run_id, pid=os.getpid(), now=datetime.now(timezone.utc))
            except RunLockError as exc:
                print(
                    f"error: {exc} — the smoke suite places real orders and runs "
                    "recoveries on this run; stop that process (or wait for its "
                    "lease to expire) first.",
                    file=sys.stderr,
                )
                return 1
            lock_pid = os.getpid()
            # The same handler `live` (cli/live.py) and `paper` (cli/paper.py) install right after
            # their own lock. Without it, `kill <pid>` — systemd's and docker's
            # default, and what a `timeout` wrapper sends — kills this process
            # outright: runner.run()'s finally never runs, so the staged long is
            # left open, the probes are left resting, the account-wide kill
            # switch is left armed, the two operator WARNINGs below are never
            # printed, and the lease is left held for LOCK_STALE_SECONDS. This
            # command needs it MORE than its two siblings, not less: they have a
            # next tick and the §18.2 shutdown sweep behind them, while this
            # finally is the only cleanup that exists (2026-07-31).
            signal.signal(signal.SIGTERM, _raise_keyboard_interrupt)
        try:
            if lock_pid is not None:
                # The lease is ours: NOW the schema upgrade is safe, because no
                # sibling can be mid-write against the old one. Deferred from
                # open (see _open_existing_db) so a refusal above cannot leave a
                # migrated store behind as its only lasting effect.
                #
                # Deferring also moved the "migrated by a NEWER build" refusal
                # here, so it has to be caught: uncaught it reached main()'s
                # last-resort handler as exit 2 ("fatal: unexpected error"),
                # losing the named exit 1 the RUNBOOK documents and a supervisor
                # branches on — the very failure the sibling commit was fixing.
                # Inside the lease-releasing try, not before it. A `return 1`
                # taken above the block that owns `finally: release_run_lock`
                # left the lease stamped with this now-dead pid for the full
                # LOCK_STALE_SECONDS, so the operator's corrected re-run was
                # refused for 15 minutes by a message naming a process that no
                # longer exists (2026-07-31 exit check).
                try:
                    apply_migrations(db.conn)
                except SchemaVersionError as exc:
                    print(f"error: {exc}", file=sys.stderr)
                    return 1
            runner = SmokeTestRunner(session)
            try:
                try:
                    runner.run(only=only)
                except SmokePreflightError as exc:
                    # No test executed, no verdict recorded; the exit disarm has
                    # already run inside runner.run()'s finally.
                    preflight_error = str(exc)
                except RunLockError as exc:
                    # The per-test heartbeat found this process superseded: a
                    # successor legitimately took over the stale lease and now owns
                    # the run AND the wallet's kill switch (the runner suppressed
                    # its exit disarm for exactly that reason). Completed verdicts
                    # are durable; stop by name.
                    print(
                        f"error: {exc} — the run lease was superseded mid-suite; the "
                        "suite stopped and left the account-wide kill switch to the "
                        "new owner. Re-run live-smoke once this run has a single owner.",
                        file=sys.stderr,
                    )
                    return 1
                passed, missing, failed, errored = smoke_gate_report(db.conn, args.run_id)
            finally:
                # In a ``finally`` on purpose: the disarm runs inside
                # runner.run()'s own finally on EVERY exit path, so its failure
                # flag must be surfaced on every path too — an unexpected
                # mid-suite exception (a wire/store error escaping to main()'s
                # generic handler) is exactly the situation most likely to
                # co-occur with a failed disarm, and the operator must not lose
                # this warning under that stack trace (silent-failure review,
                # 2026-07-29).
                if runner.kill_switch_disarm_failed:
                    print(
                        "WARNING: the end-of-suite kill-switch disarm FAILED — the "
                        "wallet may still hold an armed scheduleCancel that will "
                        "cancel every resting order (including any SL/TP) at its "
                        "deadline. Verify and clear it manually, or run `live "
                        "--run-id ...` without --loop (it re-arms, sweeps, then "
                        "disarms on a clean exit). --loop is NOT the remedy here: a "
                        "suite whose disarm failed usually has red tests, so the "
                        "§20.2 gate is shut and --loop exits 4 before arming.",
                        file=sys.stderr,
                    )
                if runner.staged_long_residual is not None:
                    # The trigger block's staged long closes BETWEEN tests, so a
                    # failed close has no step row to land in. Say it here or a
                    # real funded position is left on the wire with only a log
                    # line to show for it (review round 2026-07-29).
                    print(
                        "WARNING: the trigger-block staging position may still be "
                        f"OPEN — {runner.staged_long_residual}. Close it manually, then use a "
                        "NEW run-id for acceptance: a post-genesis manual fill has "
                        "no local order row, so it books fill_unmapped and pins "
                        "this run's validate at exit 5. Re-running the suite does "
                        "NOT flatten this residual — a fresh runner only closes the "
                        "staging long it opened itself. Note the new run-id starts "
                        "with an EMPTY §20.2 gate: re-run the full live-smoke suite "
                        "under it (including the operator-staged preconditions for "
                        "tests 16/17) or `live --loop` exits 4 with all 18 not_yet_run.",
                        file=sys.stderr,
                    )
                if runner.position_residuals:
                    # Accidental fills (a far IOC the book crossed anyway, a
                    # slice that filled before its sibling aborted) whose
                    # cleanup did not fully succeed. The staging long has had a
                    # warning since 2026-07-29; these are the same thing —
                    # a real funded position — and used to be visible only in
                    # the detail column of a PASSED row.
                    for note in runner.position_residuals:
                        print(
                            f"WARNING: a probe position may still be OPEN — {note}. "
                            "Close it manually, then use a NEW run-id for acceptance: "
                            "a manual fill has no local order row, so it books "
                            "fill_unmapped and pins this run's validate at exit 5. "
                            "The new run-id starts with an EMPTY §20.2 gate — re-run "
                            "the full live-smoke suite under it, or `live --loop` "
                            "exits 4 with all 18 tests not_yet_run.",
                            file=sys.stderr,
                        )
                if runner.probe_residual is not None:
                    # Same reasoning as the staging long, different artefact: a
                    # trigger probe books no orders row, so the ONLY signal that
                    # one was stranded used to be the detail column of a GREEN
                    # row. It is not cosmetic — see the runner's note for why it
                    # ends in a permanent exit 5 (2026-07-31 lifecycle review).
                    print(
                        f"WARNING: {runner.probe_residual}.",
                        file=sys.stderr,
                    )
        finally:
            if lock_pid is not None:
                release_run_lock(db, args.run_id, pid=lock_pid, now=datetime.now(timezone.utc))
    finally:
        db.close()
    _print_smoke_gate(passed, missing, failed, errored)
    if preflight_error is not None:
        print(f"error: {preflight_error}", file=sys.stderr)
        return 4
    if args.dry_run:
        # A dry run places nothing, so the gate can never pass — that is the
        # point (a wiring check, not a cycle-entry proof). Exit 0 to signal the
        # wiring check itself completed; the operator reads "smoke_gate_passed:
        # no" and knows a real run is still required.
        print("dry-run complete — no orders placed; run without --dry-run for the real gate.")
        return 0
    return 0 if passed else 4


def _build_smoke_session(args, db):
    """Build the :class:`~.live.smoke.SmokeContext` for a real or dry run.

    ``db`` is the caller-owned, already-open store (the caller closes it), so
    every failure path here just returns an ``int`` exit code. A dry run stops
    after config validation (it needs no network): the context carries no signed
    client (``signed=None``) and market seams the runner never calls, so
    ``--dry-run`` works fully offline.
    """
    from decimal import Decimal

    from ..domains.perp.risk_gate import RiskConfig
    from ..engine_bridge import load_config_or_exit
    from ..live.config import ExecutionMode, LiveConfig, validate_live_risk_consistency
    from ..live.smoke import SmokeContext
    from ..paper.clock import WallClock

    config = load_config_or_exit(args.config)
    if config is None:
        return 1
    raw_live = config.get("live")
    if raw_live is None:
        print(
            "error: config has no live: block — live-smoke needs one (phase3-spec §4).",
            file=sys.stderr,
        )
        return 1
    try:
        live_cfg = LiveConfig.from_dict(raw_live)
    except ValueError as exc:
        print(f"error: invalid live: config — {exc}. Fix the YAML and re-run.", file=sys.stderr)
        return 1
    if live_cfg.mode is ExecutionMode.PAPER:
        print("error: live.mode is 'paper' — the smoke suite is a live-mode tool.", file=sys.stderr)
        return 1
    if live_cfg.mode is not ExecutionMode.TESTNET_LIVE:
        # The §20.2 smoke suite is a TESTNET pre-flight: it opens/closes real
        # positions and rests/cancels real SL/TP triggers. mainnet_tiny relies on
        # the smoke proven on the SEPARATE testnet run (§21.3) and is never
        # smoke-tested on mainnet — refuse here (both --dry-run and real) so a
        # mis-pointed config can never drive a real order onto mainnet.
        print(
            f"error: live-smoke runs only against a testnet_live run — live.mode is "
            f"'{live_cfg.mode.value}'. mainnet_tiny relies on the smoke suite proven on "
            "the separate testnet run (§21.3); it is never smoke-tested on mainnet.",
            file=sys.stderr,
        )
        return 1
    raw_risk = config.get("risk")
    if raw_risk is None:
        print(
            "error: config has no risk: block — required for the live gate (§24).", file=sys.stderr
        )
        return 1
    try:
        risk_cfg = RiskConfig.from_dict(raw_risk)
        validate_live_risk_consistency(live_cfg, risk_cfg, raw_risk)
    except ValueError as exc:
        print(f"error: invalid risk:/live: config — {exc}.", file=sys.stderr)
        return 1

    coin = live_cfg.safety.allowed_symbols[0]
    clock = WallClock()
    # The db is owned and closed by the caller (_cmd_live_smoke's try/finally),
    # so every error path here just returns an int — no close needed.
    not_found_hint = f" — create it first with `live --run-id {args.run_id} --create`."
    run_row = _existing_run_row(db.conn, args.run_id, args.db, not_found_hint=not_found_hint)
    if run_row is None:
        return 1
    if not _require_live_run_mode(run_row, args.run_id, args.db):
        return 1
    # Same identity discipline as `live` resume (decision 2026-07-28): the suite
    # runs a real §19.1 recovery and books probe orders ON this run, so a typo'd
    # --run-id pointing at another live run in the same store — the §21.4
    # mainnet acceptance run lives in the same default db — would reconcile the
    # TESTNET exchange against that run's ledger and file integrity cases that
    # the §5 cumulative policy makes permanent. coin / live.network drift is a
    # hard refusal (the mainnet run trips the network check); parameter drift
    # warns, as on resume.
    drift = _config_drift_report(run_row["config_json"], config, coin)
    if drift is not None:
        kind, message = drift
        if kind in _HARD_DRIFT_KINDS:
            print(f"error: {message}", file=sys.stderr)
            return 1
        print(f"WARNING: {message}", file=sys.stderr)

    if args.dry_run:
        # No network: signed stays None and the seams below are never called.
        def _unavailable() -> Decimal:
            raise RuntimeError("dry-run places no orders; mark_price is not fetched")

        return SmokeContext(
            signed=None,
            db=db,
            run_id=args.run_id,
            coin=coin,
            network=live_cfg.network,
            payload_dir=Path(args.db).resolve().parent / "payloads" / args.run_id,
            owner_prefix=live_cfg.order_owner_prefix,
            mark_price=_unavailable,
            qty_step=Decimal(1),
            tick_size=Decimal(1),
            now=clock.now,
            dry_run=True,
            run_recovery=None,
        )

    return _build_real_smoke_session(
        args, config=config, live_cfg=live_cfg, coin=coin, clock=clock, db=db
    )


def _build_real_smoke_session(args, *, config, live_cfg, coin, clock, db):
    """The network half of :func:`_build_smoke_session` (real, order-placing).

    Verifies the §6.1 agent authorization, builds the runtime-armed signed
    client, reads the asset meta and mark, and wires the ``run_recovery`` seam
    to one real §19.1 startup recovery over the run. Returns the context or an
    ``int`` exit code.
    """

    from ..config import wallet_address
    from ..exchanges.hyperliquid.errors import ExchangeError
    from ..exchanges.hyperliquid.market_data import HyperliquidMarketData
    from ..exchanges.hyperliquid.sdk_client import HyperliquidClient
    from ..exchanges.hyperliquid.signed_client import HyperliquidSignedClient
    from ..live.authorization import AgentAuthorizationError, verify_agent_authorization
    from ..live.order_gate import RealOrderGate
    from ..live.secrets import agent_key_env_var, load_agent_key
    from ..live.smoke import SmokeContext
    from ..paper.engine import AssetSpec

    if not live_cfg.allow_real_orders:
        print(
            "error: live.allow_real_orders is false — the smoke suite places real "
            "signed orders on testnet. Enable it (with the agent key), or use "
            "--dry-run for an offline wiring check.",
            file=sys.stderr,
        )
        return 1
    addr = wallet_address(config)
    if not addr:
        print(
            "error: wallet_address is not configured (needed for the smoke suite).", file=sys.stderr
        )
        return 1
    agent_key = load_agent_key(live_cfg.network)
    if agent_key is None:
        env_var = agent_key_env_var(live_cfg.network)
        print(
            f"error: {env_var} is not set — the smoke suite signs real testnet orders. "
            f"Export the {live_cfg.network} agent key, or use --dry-run.",
            file=sys.stderr,
        )
        return 1
    try:
        client = HyperliquidClient.from_config(config, network=live_cfg.network)
    except ExchangeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    try:
        verify_agent_authorization(client.info, wallet_address=addr, agent_key=agent_key)
    except (AgentAuthorizationError, ExchangeError) as exc:
        print(f"error: agent authorization failed — {exc}", file=sys.stderr)
        return 1

    # The SAME preflight `live` runs, so one bad config gets one exit code and
    # one remedy. Without it the violation surfaced from KillSwitchManager's
    # constructor as a contained SmokePreflightError → exit 4, which RUNBOOK §5
    # answers with "check the run state" — the wrong investigation, and a
    # supervisor branching on 1-vs-4 mis-routes it too. Needs `client` for the
    # timeout advisory, so it sits here rather than with the config checks.
    if _timing_preflight(live_cfg, client) != 0:
        return 1

    gate = RealOrderGate.from_config(live_cfg)
    gate.agent_authorized = True
    signed = HyperliquidSignedClient(
        live_cfg.network, agent_key, wallet_address=addr, gate=gate, timeout=client.timeout
    )
    try:
        signed.health_check()
        market = HyperliquidMarketData(client)
        sz_decimals, schedule = market.get_asset_meta(coin)
    except ExchangeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    asset = AssetSpec(coin=coin, sz_decimals=sz_decimals, margin_schedule=schedule)

    def _mark() -> Decimal:
        return market.get_market_snapshot(coin).mark_price

    payload_dir = Path(args.db).resolve().parent / "payloads" / args.run_id

    def _run_recovery():
        return _smoke_startup_recovery(
            db=db,
            run_id=args.run_id,
            coin=coin,
            live_cfg=live_cfg,
            signed=signed,
            gate=gate,
            client=client,
            wallet=addr,
            payload_dir=payload_dir,
        )

    def _heartbeat() -> None:
        # Keeps the lease _cmd_live_smoke acquired fresh across the suite;
        # raises RunLockError once superseded (the runner aborts on it).
        from ..paper.run_lock import heartbeat_run_lock

        heartbeat_run_lock(db, args.run_id, pid=os.getpid(), now=datetime.now(timezone.utc))

    return SmokeContext(
        signed=signed,
        db=db,
        run_id=args.run_id,
        coin=coin,
        network=live_cfg.network,
        payload_dir=payload_dir,
        owner_prefix=live_cfg.order_owner_prefix,
        mark_price=_mark,
        qty_step=asset.qty_step,
        tick_size=asset.tick_size,
        now=clock.now,
        dry_run=False,
        # The regime smoke test 2 writes is the RUN'S OWN (§4 live.safety) —
        # the suite must never leave the account in a leverage/margin mode the
        # config did not declare (decision 2026-07-29).
        leverage=int(live_cfg.safety.leverage),
        is_cross=live_cfg.safety.margin_mode.value == "cross",
        # The run's OWN §18 deadline, not the dataclass default: the pre-flight
        # recovery arms the switch from live.kill_switch.schedule_cancel_seconds,
        # and the suite's per-test refresh then overwrites that deadline. Leaving
        # it at 120s silently NARROWED a longer configured cover to 120s for the
        # whole suite — so a test making a few round-trips on a slow network
        # could let the switch fire and cancel the resting probe, exactly the
        # failure the refresh exists to prevent (2026-07-30 concurrency review).
        # ...but a FLOOR, not a plain hand-over. The config invariant only
        # requires schedule_cancel > 5s and >= 2x refresh_interval, both judged
        # against `live`'s 30s tick model — while the suite refreshes once per
        # TEST, and a test is a place/poll/cancel round-trip with no upper bound.
        # So a config that is perfectly legal for the daemon (say 40s/5s) would
        # hand the suite a 40s cover, narrower than the 120s it had before the
        # value was wired through at all, and the switch could fire mid-test and
        # cancel the resting probe — recorded as "the exchange refused", which
        # sends the operator to check config and market state rather than the
        # clock. max() keeps 3ae0087's intent (a LONGER cover is honoured) while
        # never going below what the suite used to guarantee (2026-07-31).
        kill_switch_deadline=max(
            timedelta(seconds=live_cfg.kill_switch.schedule_cancel_seconds),
            _SMOKE_MIN_KILL_SWITCH_DEADLINE,
        ),
        run_recovery=_run_recovery,
        heartbeat=_heartbeat,
    )


def _smoke_startup_recovery(
    *, db, run_id, coin, live_cfg, signed, gate, client, wallet, payload_dir
):
    """One real §19.1 startup recovery for the restart smoke tests (15–17).

    Builds the same recovery components ``live --run-id`` does and returns the
    :class:`~.live.startup.StartupResult`. It arms the kill switch and reconciles
    the run against the exchange — exactly the restart-reconciliation the smoke
    tests assert is clean given the operator-staged preconditions.
    """
    from ..exchanges.hyperliquid.sdk_client import call_sdk
    from ..live.fill_backfill import FillBackfiller
    from ..live.fills import LiveFillProcessor
    from ..live.kill_switch import KillSwitchManager, refresh_across_blocking_work
    from ..live.reconcile import LiveReconciler
    from ..live.safe_mode import SafeModeManager
    from ..live.startup import run_startup_recovery

    def fetch_clearinghouse():
        return call_sdk(client.info.user_state, wallet)

    kill_switch = KillSwitchManager(
        client=signed,
        gate=gate,
        db=db,
        run_id=run_id,
        config=live_cfg.kill_switch,
        max_tick_gap_seconds=_RECOVERY_MAX_TICK_GAP_SECONDS,
        network_timeout_s=signed.timeout,
        payload_dir=payload_dir,
        # This manager belongs to a live-smoke run: its arm and its tick-driven
        # refreshes happen inside the suite, so they are cover but not evidence
        # that the DAEMON exercised the switch — the marker drops these rows
        # from the §20.3 sample floor AND from the clean-shutdown daemon
        # verdict alike.
        suite_authored=True,
    )
    safe_mode = SafeModeManager(db=db, run_id=run_id, gate=gate)
    processor = LiveFillProcessor(
        db=db,
        run_id=run_id,
        payload_dir=payload_dir,
        wallet_address=signed.wallet_address,
    )

    # §18.2: the same wiring as the live loop's, and for the same reason — this
    # path ARMS the switch (it is handed to run_startup_recovery below) under the
    # same _RECOVERY_MAX_TICK_GAP_SECONDS, so its sweep can lapse the deadline
    # and cancel the wallet mid-recovery exactly as the live one can. Wiring only
    # the live lane would have left the smoke restart tests (15–17) running the
    # unrefreshed version of the very sweep they exist to exercise
    # (2026-07-31 deadline review).
    def _refresh_across_sweep() -> None:
        refresh_across_blocking_work(kill_switch, what="reconciliation")

    backfiller = FillBackfiller(
        fetch=signed.user_fills_by_time,
        processor=processor,
        refresh_kill_switch=_refresh_across_sweep,
    )
    reconciler = LiveReconciler(
        db=db,
        run_id=run_id,
        coin=coin,
        fetch_open_orders=signed.open_orders,
        fetch_clearinghouse=fetch_clearinghouse,
        query_order_by_cloid=signed.query_order_by_cloid,
        fetch_fills=signed.user_fills_by_time,
        backfiller=backfiller,
        payload_dir=payload_dir,
        refresh_kill_switch=_refresh_across_sweep,
    )
    return run_startup_recovery(
        db=db,
        run_id=run_id,
        client=signed,
        fetch_clearinghouse=fetch_clearinghouse,
        gate=gate,
        kill_switch=kill_switch,
        reconciler=reconciler,
        safe_mode=safe_mode,
        payload_dir=payload_dir,
    )
