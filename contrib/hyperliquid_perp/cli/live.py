"""The ``live`` subcommand: Phase 3 startup gates + authorization (PR 1) and
the §19.1 startup recovery (PR 4), continuing into the live trading loop
under ``--loop`` (PR 5).
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from ..common import store_layout
from ._common import (
    _migrate_owned_store,
    _open_run_or_exit,
    _raise_keyboard_interrupt,
    _require_agent_key,
    _require_api_key,
    announce_protection_only_settled,
)
from ._drift import _HARD_DRIFT_KINDS, _config_drift_report, _norm_network, _run_config_subset
from .live_loop import _run_live_loop, _still_owns_run
from .live_shared import (
    _RECOVERY_MAX_TICK_GAP_SECONDS,
    _conflicting_run_lease,
    _smoke_gate_buckets,
    _timing_preflight,
)

if TYPE_CHECKING:
    from ..live.config import LiveGateRefusal
    from ..live.shutdown import ExitReason

logger = logging.getLogger(__name__)


def _gate_refusal_wording(refusal: LiveGateRefusal) -> str:
    """This command's operator wording for each rung of ``load_live_gates`` it can hit.

    ``live`` passes no ``modes`` (every live mode is its business), so the
    ``MODE_NOT_ACCEPTED`` rung never fires here and has no wording.
    """
    from ..live.config import LiveGateStage as Stage

    wording = {
        Stage.NO_LIVE_BLOCK: (
            "config has no live: block — the live subcommand needs one "
            "(phase3-spec §4). Add it to the YAML and re-run."
        ),
        Stage.PAPER_MODE: (
            "live.mode is 'paper' — use the paper subcommand for paper "
            "runs; the live subcommand needs testnet_live or mainnet_tiny."
        ),
    }
    return wording[refusal.stage]


def _exit_line(reason: ExitReason) -> str | None:
    """The line ``live --run-id`` prints last for each exit, or None for none.

    The None reasons already printed theirs inside the ``finally``: an
    unclean sweep's ``error: §18.2 shutdown unclean`` and a protection-only
    ending's own line. Every reason has an entry, so a new one without
    wording fails loudly.
    """
    from ..live.shutdown import ExitReason

    wording: dict[ExitReason, str | None] = {
        ExitReason.SWEEP_UNCLEAN: None,
        ExitReason.PROTECTION_ONLY_SETTLED: None,
        ExitReason.PROTECTION_ONLY_STOPPED: None,
        ExitReason.VERDICT_FAILED: (
            "startup recovery did NOT pass — the run is in safe mode; see the "
            "reconciliation events / safe-mode state above (§19.1 step 15)."
        ),
        ExitReason.LOOP_IN_SAFE_MODE: (
            "live loop exited IN SAFE MODE — see safe_mode above; "
            "resolve it (manual release if required) before resuming."
        ),
        ExitReason.LOOP_KEPT_ON_UNKNOWN_SAFE_MODE: (
            "live loop exited with protective orders kept behind "
            "a FAILED shutdown safe-mode read (unknown ≠ clean) — "
            "inspect the run store before resuming."
        ),
        ExitReason.LOOP_CLEAN: (
            "live loop exited — §18.2 shutdown sweep done; re-run with --loop to resume this run."
        ),
        ExitReason.ONE_SHOT_PASSED: (
            "startup recovery passed — a live loop can start from this state (re-run with --loop)."
        ),
    }
    return wording[reason]


def _cmd_live(argv: list[str]) -> int:
    """Load the ``live:`` gates, verify agent authorization, print caps, exit.

    The Phase 3 startup sequence: everything here must pass before the live
    loop may run, and every failure is a named exit 1. Without --run-id the
    command is config-only and can never place an order; with --run-id it runs
    the §19.1 startup recovery, and --loop then continues into the PR 5 live
    trading loop (the one lane that trades). Config/env problems fail fast
    (nothing else is checkable without them); the network-dependent gates all
    run and report every failure in one pass.
    """
    parser = argparse.ArgumentParser(
        prog="python -m contrib.hyperliquid_perp live",
        description=(
            "Phase 3 live startup: validate the config gates + agent "
            "authorization and print effective caps; with --run-id, run the "
            "full §19.1 startup recovery (arm kill switch, reconcile, cancel "
            "stale bot-owned orders) and report the verdict; add --loop to "
            "continue into the live trading loop."
        ),
    )
    parser.add_argument("--config", default=None, help="Config YAML path.")
    parser.add_argument(
        "--db",
        default="live_trading.db",
        help="SQLite store path for the live run (only used with --run-id).",
    )
    parser.add_argument(
        "--run-id",
        default=None,
        help=(
            "Run the full §19.1 startup recovery against this run. Omitted: "
            "config-check mode (the PR 1 gates), which never signs anything."
        ),
    )
    parser.add_argument(
        "--create",
        action="store_true",
        help=(
            "Create the run (genesis from the live exchange snapshot) instead "
            "of resuming an existing one — same explicit-identity rule as the "
            "paper subcommand."
        ),
    )
    parser.add_argument(
        "--adopt-positions",
        action="store_true",
        help=(
            "Allow --create to seed an EXISTING exchange position into the new "
            "run's genesis. Without it, --create refuses a non-flat account — "
            "a typo'd --run-id must not silently adopt a live position into a "
            "fresh ledger."
        ),
    )
    parser.add_argument(
        "--loop",
        action="store_true",
        help=(
            "After the §19.1 startup recovery passes, run the PR 5 live trading "
            "loop (~10s tick, inside the 30s kill-switch budget: WS drain → "
            "kill-switch refresh → reconciliation → SL/TP protection → due "
            "slices, with the 4h AI decision cycle off the tick thread). "
            "Ctrl-C / SIGTERM stops it and runs the §18.2 shutdown sweep. "
            "Without it, --run-id is the one-shot recovery check."
        ),
    )
    args = parser.parse_args(argv)

    if args.run_id is None and (args.create or args.adopt_positions or args.loop):
        # Named rejection, not silent-ignore: without --run-id this command is
        # config-check mode — it creates and seeds nothing and runs no loop — so
        # --create / --adopt-positions / --loop have no effect. An operator who
        # passed them almost certainly meant the §19.1 recovery (and, for --loop,
        # the live trading loop) and would otherwise read the "gates OK" exit 0
        # as "run started". Same discipline as the resume and safe-mode guards.
        print(
            "error: --create / --adopt-positions / --loop require --run-id — without "
            "it this command only checks the config gates. Pass --run-id to run the "
            "§19.1 startup recovery (add --loop to continue into the live trading loop).",
            file=sys.stderr,
        )
        return 1

    # Same rationale as ``paper``: startup diagnostics need timestamps; the
    # basicConfig no-ops when an embedding application already configured one.
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    from ..config import wallet_address
    from ..engine_bridge import load_config_or_exit
    from ..exchanges.hyperliquid.account import HyperliquidAccount
    from ..exchanges.hyperliquid.errors import ExchangeError
    from ..exchanges.hyperliquid.sdk_client import HyperliquidClient
    from ..live.authorization import (
        EXPIRY_WARNING_HORIZON,
        AgentAuthorizationError,
        verify_agent_authorization,
    )
    from ..live.config import (
        EXCHANGE_MIN_ORDER_NOTIONAL_USDC,
        LiveGateRefusal,
        compute_notional_caps,
        load_live_gates,
    )
    from ..live.secrets import load_agent_key
    from ..live.wiring import build_signed_client

    config = load_config_or_exit(args.config)
    if config is None:
        return 1
    # The config ladder both live-mode commands climb (``live.config``); each
    # refusal's wording is this command's own.
    try:
        gates = load_live_gates(config)
    except LiveGateRefusal as refusal:
        print(f"error: {_gate_refusal_wording(refusal)}", file=sys.stderr)
        return 1
    raw_live, live_cfg = gates.raw_live, gates.live_cfg

    # PR 5 (decided 2026-07-22): --loop consumes the risk:/decision: grid the
    # same way the paper engine does — validate those blocks HERE, where a typo
    # is a named exit-1 config error, never after a passing recovery where the
    # loop would be silently skipped and exit 0 would read as a clean run to a
    # supervisor. (_cmd_paper makes the same up-front check.)
    loop_cfgs = None
    if args.loop:
        from ..engine_bridge import _load_risk_decision

        loop_cfgs = _load_risk_decision(config)
        if loop_cfgs is None:
            return 1
        # The AI key, checked here for the same reason the config blocks above
        # are: the loop drives a 4h AI cycle, and without a key EVERY cycle
        # records api_failed — which never counts toward the §20.3 >=30-cycle
        # gate. A real-money run could otherwise burn days producing nothing
        # gateable, with no named error anywhere. _cmd_paper has always checked
        # this; the live path did not (added 2026-07-30). After the config
        # validation, so a typo in risk:/decision: still reports as the config
        # error it is rather than being masked by a missing key.
        if not _require_api_key():
            return 1

    # A top-level ``network:`` that disagrees with ``live.network`` is legal —
    # the same file can drive paper reads on mainnet while live drills on
    # testnet — but it is also how a stale key silently points somewhere
    # unexpected, so say which one the live run uses.
    # load_config validates the top-level key case-insensitively but stores it
    # raw — normalise before comparing or `network: TestNet` would warn
    # spuriously against an equal live.network.
    top_network = config.get("network")
    if isinstance(top_network, str) and top_network.strip().lower() != live_cfg.network:
        print(
            f"warning: live run uses live.network {live_cfg.network!r} and "
            f"ignores the top-level network: {top_network!r} (only the paper "
            "subcommand reads that key; live does still inherit the top-level "
            "network_timeout_s and wallet_address).",
            file=sys.stderr,
        )

    addr = wallet_address(config)
    if not addr:
        print(
            "error: wallet_address is not configured — the live subcommand needs "
            "the main wallet address for agent authorization and account reads.",
            file=sys.stderr,
        )
        return 1

    if live_cfg.require_agent_wallet:
        # §6 rule 6 rides this check too: allow_real_orders: true implies
        # require_agent_wallet: true (a LiveConfig construction invariant), so
        # "real orders asked for, no key" always lands here — a named hard
        # fail, never a silent downgrade into an order-less run.
        detail = (
            "live.allow_real_orders is true (§6 rule 6: a missing key can "
            "never mean orders still on)"
            if live_cfg.allow_real_orders
            else "live.require_agent_wallet is true"
        )
        agent_key = _require_agent_key(
            live_cfg.network,
            demanded_by=detail,
            remedy=(
                f"export the {live_cfg.network} agent key, or set "
                "require_agent_wallet: false (with allow_real_orders: false) for "
                "a keyless gate check"
            ),
        )
        if agent_key is None:
            return 1
    else:
        # A keyless gate check is allowed to run keyless: no refusal, and the
        # authorization step below is skipped when this is None.
        agent_key = load_agent_key(live_cfg.network)

    try:
        # Live runs are pinned to ``live.network``, not the top-level Phase 1/2
        # ``network:`` key — the override keeps ``network_timeout_s`` resolution
        # in its one seam instead of re-implementing it here.
        client = HyperliquidClient.from_config(config, network=live_cfg.network)
    except ExchangeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    # The three network-dependent gates below (authorization, account read +
    # caps, signed health check) are independent given a constructed client —
    # run them ALL and report every failure in one pass, so an operator with
    # two broken things fixes both before the next run instead of discovering
    # them one re-run at a time.
    failures: list[str] = []

    auth = None
    if agent_key is not None:
        # §6.1: run the authorization check whenever a key is present — even
        # with real orders off, a bad key/approval is operator-actionable now.
        try:
            auth = verify_agent_authorization(client.info, wallet_address=addr, agent_key=agent_key)
        except (AgentAuthorizationError, ExchangeError) as exc:
            failures.append(f"agent authorization failed — {exc}")
        else:
            print(
                f"agent {auth.agent_address} authorized for {addr} until "
                f"{auth.valid_until.isoformat()}",
                file=sys.stderr,
            )
            if auth.expires_within(EXPIRY_WARNING_HORIZON):
                print(
                    f"warning: agent authorization expires at "
                    f"{auth.valid_until.isoformat()} — less than "
                    f"{EXPIRY_WARNING_HORIZON.days} days away; re-approve the "
                    "agent before a long run (§6.1).",
                    file=sys.stderr,
                )
        # Prove the signed transport end-to-end (construction + a read on the
        # live network) so a bad SDK/network surfaces now, not on the first
        # real order a --loop run places. The bound gate is fresh-from-config, i.e.
        # fail-closed: no runtime condition is proven in this config-only
        # command, so the client could not place an order even if asked.
        try:
            _gate, signed = build_signed_client(
                live_cfg,
                agent_key=agent_key,
                wallet_address=addr,
                timeout=client.timeout,
                agent_authorized=False,
            )
            signed.health_check()
        except ExchangeError as exc:
            failures.append(f"signed client health check failed — {exc}")
        else:
            print(f"signed client healthy: {signed!r}", file=sys.stderr)

    snapshot = None
    caps = None
    try:
        snapshot = HyperliquidAccount(client).get_account_snapshot(addr)
    except ExchangeError as exc:
        failures.append(f"account read failed — {exc}")
    except ValueError as exc:
        failures.append(f"account snapshot unusable (margin-called / empty / invalid?) — {exc}")
    else:
        caps = compute_notional_caps(snapshot.account_value, live_cfg.safety)
        # §5 rule 3: compute AND record both caps at startup.
        logger.info(
            "startup caps for %s: account_equity=%s pct_cap_notional=%s effective_notional_cap=%s",
            live_cfg.mode.value,
            snapshot.account_value,
            caps.pct_cap_notional,
            caps.effective_notional_cap,
        )
        if caps.below_exchange_minimum:
            failures.append(
                f"effective_notional_cap ({caps.effective_notional_cap} USDC) "
                f"is below the exchange minimum order value "
                f"({EXCHANGE_MIN_ORDER_NOTIONAL_USDC} USDC) — the run could never "
                "place an order (§5 rule 4). Fund the account or raise the caps."
            )

    if failures:
        for failure in failures:
            print(f"error: {failure}", file=sys.stderr)
        return 1

    print(f"mode: {live_cfg.mode.value}")
    print(f"network: {live_cfg.network}")
    print(f"allow_real_orders: {'true' if live_cfg.allow_real_orders else 'false'}")
    if auth is not None:
        # Part of the machine-readable contract: a deploy preflight wrapping
        # this gate check can capture the expiry without scraping stderr.
        print(f"agent_address: {auth.agent_address}")
        print(f"authorization_valid_until: {auth.valid_until.isoformat()}")
    print(f"account_equity: {snapshot.account_value} USDC")
    print(f"pct_cap_notional: {caps.pct_cap_notional} USDC")
    print(f"effective_notional_cap: {caps.effective_notional_cap} USDC")
    if args.run_id is None:
        print(
            "live startup gates OK — exiting (config-check mode; pass --run-id "
            "to run the full §19.1 startup recovery).",
            file=sys.stderr,
        )
        return 0
    return _live_startup_recovery(
        args,
        config=config,
        raw_live=raw_live,
        live_cfg=live_cfg,
        client=client,
        wallet=addr,
        agent_key=agent_key,
        snapshot=snapshot,
        loop_cfgs=loop_cfgs,
    )


def _live_startup_recovery(
    args,
    *,
    config: dict,
    raw_live: dict,
    live_cfg,
    client,
    wallet: str,
    agent_key: str | None,
    snapshot,
    loop_cfgs,
) -> int:
    """The §19.1 startup recovery tail of ``live --run-id`` (steps 5–16).

    Steps 1–4 (config gates, §6.1 authorization, exchange client, account
    read) were proven by the caller; this builds the PR 2–4 components — a
    runtime-flagged gate, the signed client, kill switch, safe-mode machine
    and reconciler — creates or resumes the live run, and hands off to
    :func:`~.live.startup.run_startup_recovery`. Without --loop the command is
    one-shot: it reports the verdict, runs the §18.2 shutdown sweep, and
    exits; with --loop a passing verdict hands off to :func:`_run_live_loop`
    (which keeps the kill switch refreshed) before the same sweep runs on the
    way out. Exit codes: 0 = the §19.1 step-16 verdict allows a new AI cycle;
    4 = recovery executed but the verdict is unclean (the run is in safe
    mode), or a --loop run was stopped while in protection-only mode (issue
    #268: the engine could not be built over a live position, so the loop
    ran tick-only — see :func:`_run_live_loop`); 1 = hard failure
    (config/arming/creation errors, the engine not buildable over a FLAT
    book, or a protection-only loop that ended itself once its position
    closed — that settle-exit is 1 even with safe mode latched: the cause the
    operator must fix comes first, and the ``safe_mode:`` line above it
    still reports the latch). Either protection-only ending prints its own
    line naming the cause BEFORE the exit-code dispatch, so an unclean
    §18.2 sweep (``shutdown_problem``, always 4: the wallet-wide trigger may
    still be armed) can outrank the code but never hide the cause.
    """
    import signal
    from decimal import Decimal

    from ..engine_bridge import EngineConfigError
    from ..exchanges.hyperliquid.sdk_client import call_sdk
    from ..live.shutdown import (
        ShutdownFlags,
        ShutdownVerdict,
        classify_exit,
        classify_shutdown,
        read_exit_state,
        sweep_on_exit,
    )
    from ..live.wiring import build_live_session, build_signed_client
    from ..persistence import repository as repo
    from ..runtime.genesis import write_genesis
    from ..runtime.run_lock import (
        RunLockError,
        acquire_run_lock,
        peek_run_lock,
        release_run_lock,
    )

    if agent_key is None:
        print(
            "error: the §19.1 startup recovery signs exchange actions (kill "
            "switch, stale-order cancels) — it needs the agent key. Run without "
            "--run-id for a keyless gate check.",
            file=sys.stderr,
        )
        return 1
    if not live_cfg.allow_real_orders:
        print(
            "error: live.allow_real_orders is false — the §19.1 startup recovery "
            "arms the kill switch and cancels stale bot-owned orders, which are "
            "signed exchange actions. Enable it, or run without --run-id for a "
            "gate check that never signs anything.",
            file=sys.stderr,
        )
        return 1

    if _timing_preflight(live_cfg, client) != 0:
        return 1

    run_id: str = args.run_id
    coin = live_cfg.safety.allowed_symbols[0]
    db_path = Path(args.db)
    payload_dir = store_layout.payload_dir(db_path, run_id)
    now = datetime.now(timezone.utc)

    # The runtime gate: config pins the wire conditions; §6.1 passed above.
    gate, signed = build_signed_client(
        live_cfg,
        agent_key=agent_key,
        wallet_address=wallet,
        timeout=client.timeout,
        agent_authorized=True,
    )

    def fetch_clearinghouse():
        return call_sdk(client.info.user_state, wallet)

    # Same guard the paper daemon applies, and it matters more here: Database()
    # creates AND migrates a store, so a wrong CWD or typo'd --db would leave an
    # empty live store behind before failing on "run does not exist" — and with
    # --create it would silently open a SECOND live ledger over the same real
    # wallet, each blind to the other's orders and books.
    if not Path(db_path).exists() and not args.create:
        print(
            f"error: database {str(db_path)!r} does not exist. Pass --create to "
            "start a new store, or point --db at the existing one.",
            file=sys.stderr,
        )
        return 1

    # Opened as-is (issue #129 — see runtime.run_identity.open_run). Unlike
    # paper, this command cannot take its lease before the upgrade: the drift and
    # off-coin checks between here and the lock read tables later migrations
    # have altered, and --create writes the run row before the lock. So the
    # refusals that need only what ``schema.LEASE_READABLE_SINCE`` declares
    # readable before the upgrade (the ``runs`` row and the lease columns) run
    # first — run existence, wallet-sibling lease, run mode, this run's own
    # lease (read-only) — and the store is migrated once they all pass. The
    # definitive lease is still taken below; a process starting concurrently
    # loses there, having written nothing the migration cannot share. The
    # peek has no pid to exempt: it holds no lease of its own, which is the
    # reason ``run_lock.peek_run_lock`` gives for refusing ANY fresh holder —
    # this process included, if a lease still carries a pid the OS recycled to
    # us after a hard kill.
    opened = _open_run_or_exit(db_path, run_id, create=args.create)
    if opened is None:
        return 1
    with opened.db as db:
        existing_run = opened.existing_run
        is_restart = opened.is_restart
        # BEFORE --create writes the run row and before any wire action: a
        # refusal taken later left a half-created run behind, and the operator's
        # corrected re-run was then rejected as "already exists" (2026-07-31
        # exit check). own_network comes from this session's config because a
        # not-yet-created run has no genesis to read; the SIBLING's network is
        # still read from the store.
        #
        # The same per-WALLET hazard `live-smoke` refuses, on the path that runs
        # with REAL money. This command arms and clears the account-wide
        # scheduleCancel and runs the §19.3 stale-order sweep, whose bot-ownership
        # lookup (get_cloid_by_hex) carries no run_id — so a sibling live run on
        # this wallet has its resting orders cancelled by our sweep, and whichever
        # of us shuts down cleanly first strips the other's dead-man cover. The
        # run lease cannot see this: it is per-run_id, and both runs hold their
        # own quite happily. Guarding only the testnet suite and not this was the
        # most asymmetric gap of the 2026-07-31 review.
        conflict = _conflicting_run_lease(db, run_id, own_network=_norm_network(raw_live))
        if conflict is not None:
            other_run, other_pid = conflict
            print(
                f"error: run {other_run!r} in {args.db} is being driven by pid "
                f"{other_pid} right now, on this same network — the same wallet. "
                "This command's kill-switch arm/clear and §19.3 stale-order sweep are "
                "ACCOUNT-wide, not run-scoped, so the two runs would cancel each "
                "other's resting orders and strip each other's dead-man cover. Stop "
                "that process, or wait for its lease to go stale. Moving either run "
                "to a different --db does NOT help: the hazard is per-WALLET, so a "
                "separate store only hides them from this check.",
                file=sys.stderr,
            )
            return 1
        foreign_mode = opened.foreign_mode("live")
        if foreign_mode is not None:
            # Resume validates the run's IDENTITY before any side effect (the
            # lock, arming the wallet-wide kill switch, reconciliation writes)
            # — the same discipline as the paper daemon's resume (decided
            # 2026-07-17). A typo'd --run-id/--db pointing at a paper run
            # would otherwise arm the kill switch over a paper ledger and
            # write live snapshots into it. The mode is a v1 column, so this
            # runs before the migration too: a typo must not upgrade a paper
            # store on its way to being refused.
            print(
                f"error: run {run_id!r} in {db_path} is a {foreign_mode} "
                "run — resuming it here would arm the kill switch and "
                f"reconcile a {foreign_mode} ledger against the live "
                "exchange. Fix --run-id / --db.",
                file=sys.stderr,
            )
            return 1
        try:
            peek_run_lock(db, run_id, now=now)
        except RunLockError as exc:
            # Own pid appended so the RUNBOOK's pid-recycling row can be
            # matched from the message alone: the process that printed it
            # has exited by the time anyone reads it.
            print(f"error: {exc} (this process is pid {os.getpid()})", file=sys.stderr)
            return 1
        if _migrate_owned_store(db, run_id=run_id, now=now):
            return 1
        if existing_run is not None:
            # A coin edit under an existing run would re-enter manual safe
            # mode every pass with nothing naming the true cause — refused
            # here, still before any side effect.
            drift = _config_drift_report(existing_run["config_json"], config, coin)
            if drift is not None:
                kind, message = drift
                if kind in _HARD_DRIFT_KINDS:
                    print(f"error: {message}", file=sys.stderr)
                    return 1
                logger.warning("config drift on live resume for %s: %s", run_id, message)
                print(f"WARNING: {message}", file=sys.stderr)
            # The STORE's own off-coin exposure, mirroring the paper daemon's
            # resume guard. The --create branch below checks the same thing from
            # the exchange snapshot, but resume never did: a live store carrying a
            # non-flat off-coin current_positions row (an older build, a
            # hand-seeded genesis, a coin edit that passed as soft "params" drift)
            # would resume and trade with that exposure invisible to every equity
            # and SL/TP computation — exactly the harm the paper message names.
            store_off_coin = sorted(
                p.coin
                for p in repo.get_all_current_positions(db.conn, run_id)
                if p.coin != coin and not p.is_flat
            )
            if store_off_coin:
                print(
                    f"error: run {run_id!r} holds open position(s) in "
                    f"{', '.join(map(repr, store_off_coin))} but this run trades "
                    f"only {coin!r} — they would be excluded from equity and "
                    "SL/TP protection, and the reconciler flags them as unknown "
                    "exchange positions (manual safe mode) on every pass. "
                    "Resolve them before resuming (export/validate still work).",
                    file=sys.stderr,
                )
                return 1
            if args.adopt_positions:
                # Named rejection, not silence: the flag seeds a NEW run's
                # genesis and has no meaning on resume — an operator who
                # passed it may believe the current exchange position was
                # adopted into the resumed books (it was not; a mismatch
                # surfaces as a reconciliation case instead).
                print(
                    "error: --adopt-positions seeds a new run's genesis and "
                    "requires --create — resuming an existing run never "
                    "re-seeds its books.",
                    file=sys.stderr,
                )
                return 1
        else:
            # Same convention as the paper path's initial_positions guard: a
            # run manages exactly one coin. An off-coin position CAN'T be
            # adopted meaningfully — the reconciler classifies every non-run
            # coin position as §13.5 "unknown exchange position" (manual safe
            # mode, re-entered every pass), so --adopt-positions over it would
            # create a run that is unreleasable from the first sweep (decided
            # 2026-07-17). Named rejection before anything is written.
            off_coin = sorted({p.coin for p in snapshot.positions} - {coin})
            if off_coin:
                print(
                    f"error: the account holds position(s) in "
                    f"{', '.join(map(repr, off_coin))} but this run trades only "
                    f"{coin!r} — a live run manages exactly one coin, and the "
                    "reconciler would flag any other coin's position as an "
                    "unknown exchange position (manual safe mode) on every "
                    "pass. Close or move those positions, or run them under a "
                    "separate wallet.",
                    file=sys.stderr,
                )
                return 1
            if snapshot.positions and not args.adopt_positions:
                # Creating a live-money ledger over a non-flat account must be
                # explicit: a typo'd --run-id plus --create would otherwise
                # silently adopt a live position into a fresh ledger with zero
                # history explaining it (decided 2026-07-16).
                held = ", ".join(f"{p.coin} {p.size}" for p in snapshot.positions)
                print(
                    f"error: the account already holds a position ({held}) — pass "
                    "--adopt-positions to seed it into the new run's genesis, or "
                    "resume the run that owns it.",
                    file=sys.stderr,
                )
                return 1
            # Live genesis = the exchange snapshot, verbatim: the opening
            # ledger balance is equity net of unrealized PnL (wallet form) and
            # any existing position is seeded as-is, so the first equity
            # reconciliation compares like against like. Historical fills that
            # PREDATE this run belong to other runs' orders (or none) and are
            # routed to unmapped/cross-run audit by the PR 3 processor — they
            # can never double-book onto this genesis.
            unrealized = sum((p.unrealized_pnl for p in snapshot.positions), Decimal(0))
            subset = _run_config_subset(config, coin)
            subset["live"] = raw_live
            write_genesis(
                db,
                run_id=run_id,
                mode="live",
                initial_balance_usdc=snapshot.account_value - unrealized,
                seeds=snapshot.positions,
                config_subset=subset,
                created_at=now,
            )
            print(f"created live run {run_id!r} in {db_path}", file=sys.stderr)

        # §20.2 gate: testnet_live cycles may not start until the smoke suite has
        # passed on THIS run. (mainnet_tiny relies on the testnet smoke pass per
        # §21.3 — a different run/network — so this same-run gate is testnet-only;
        # the one-shot recovery check, without --loop, never trades and so is not
        # gated.) Checked here, before arming: a fresh --create run has no smoke
        # results, so --loop on it is refused with the create → smoke → loop path.
        from ..live.config import ExecutionMode

        if args.loop and live_cfg.mode is ExecutionMode.TESTNET_LIVE:
            from ..live.smoke import smoke_gate_report

            gate_ok, gate_missing, gate_failed, gate_errored = smoke_gate_report(db.conn, run_id)
            if not gate_ok:
                if not is_restart:
                    print(
                        "error: --loop on a freshly-created testnet_live run needs the "
                        "§20.2 smoke suite first. Create the run, run "
                        f"`live-smoke --run-id {run_id}`, then re-run with --loop.",
                        file=sys.stderr,
                    )
                else:
                    parts = [
                        f"{label.replace('_', ' ')}: {', '.join(keys)}"
                        for label, keys in _smoke_gate_buckets(
                            gate_missing, gate_failed, gate_errored
                        )
                        if keys
                    ]
                    print(
                        "error: testnet_live cycles are gated on the §20.2 smoke suite "
                        f"(all must pass) — {'; '.join(parts)}. Run "
                        f"`live-smoke --run-id {run_id}` and re-run with --loop.",
                        file=sys.stderr,
                    )
                # Exit 4, not 1: "the gate is not open" is the same
                # not-yet-at-the-gate fact `live-smoke` itself reports as 4 (the
                # module exit contract lists it there), and a supervisor must be
                # able to tell "gate closed — human action needed" from a
                # config/auth failure's exit 1 (decision 2026-07-29).
                return 4
            # Gate open: say how stale the proof is. Passes never expire (a hard
            # max-age is a policy call deliberately not made here, 2026-07-27),
            # so an operator returning after weeks should at least SEE the age
            # and re-run live-smoke after significant code/config changes.
            latest_smoke = repo.latest_smoke_test_results(db.conn, run_id)
            if latest_smoke:
                oldest_iso = min(row["executed_at"] for row in latest_smoke.values())
                print(
                    f"§20.2 smoke gate open — oldest passing result recorded {oldest_iso}; "
                    "passes never expire, so re-run live-smoke after significant "
                    "code/config changes.",
                    file=sys.stderr,
                )

        try:
            acquire_run_lock(db, run_id, pid=os.getpid(), now=now)
        except RunLockError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        signal.signal(signal.SIGTERM, _raise_keyboard_interrupt)
        try:
            # The preflight above already proved the timing invariant with the
            # SAME constant and the SAME timeout, so the switch's constructor
            # cannot raise on timing.
            session = build_live_session(
                signed=signed,
                gate=gate,
                db=db,
                run_id=run_id,
                coin=coin,
                live_cfg=live_cfg,
                fetch_clearinghouse=fetch_clearinghouse,
                payload_dir=payload_dir,
                max_tick_gap_seconds=_RECOVERY_MAX_TICK_GAP_SECONDS,
            )
            shutdown_problem: str | None = None
            superseded = False
            # False until the §19.1 verdict PASSES — a recovery that raised
            # counts as unclean too (live.shutdown.classify_shutdown).
            verdict_passed = False
            # The ``finally``'s keep decision; stays None when a lost lease
            # skipped the sweep.
            shutdown_verdict: ShutdownVerdict | None = None
            # What a --loop run reports on the way out: None for an ordinary
            # stop, a ProtectionOnlyExit when the loop ran without a decision
            # provider (issue #268) — read after the sweep to pick the exit line.
            loop_exit = None
            # True while the loop is in flight, so a raise out of it leaves
            # it True. The boot verdict says the loop may start; it says
            # nothing about how the loop ended, and a raise out of it (any of
            # its construction steps, a store read, an import) reaches the
            # generic handler below with that passing verdict on record — the
            # sweep in the ``finally`` must not read "verdict passed" as
            # "ended cleanly" and strip SL/TP over a live position (issue
            # #268 review).
            loop_raised = False
            # The one raise out of the loop that is NAMED and handled (a flat
            # book's EngineConfigError): the sweep's note must point at the
            # error already printed, not call it an unaccounted-for raise.
            loop_refused = False
            try:
                result = session.run_startup_recovery()
                verdict_passed = result.passed
                # PR 5: with --loop, a passing recovery hands off to the live
                # trading loop; it returns on Ctrl-C / SIGTERM, and the §18.2
                # shutdown sweep in the ``finally`` below then disarms the switch.
                if args.loop and result.passed:
                    loop_raised = True
                    loop_exit = _run_live_loop(
                        cfgs=loop_cfgs,
                        db=db,
                        run_id=run_id,
                        coin=coin,
                        config=config,
                        live_cfg=live_cfg,
                        client=client,
                        signed=signed,
                        gate=gate,
                        kill_switch=session.kill_switch,
                        safe_mode=session.safe_mode,
                        reconciler=session.reconciler,
                        processor=session.processor,
                        payload_dir=payload_dir,
                        fetch_clearinghouse=fetch_clearinghouse,
                        identity=session.identity,
                    )
                    loop_raised = False
            except RunLockError as exc:
                # §18.2 lease takeover (raised out of the loop's heartbeat): a
                # successor process owns the run now — its store, its resting
                # orders (the SL/TP included) and the wallet's dead-man's
                # switch. ANY exchange action or store write from this process
                # sabotages the successor: the §18.2 sweep in the ``finally``
                # below would cancel the successor's live protection orders and
                # leave ITS position naked. Exit with nothing but the
                # pid-guarded lock release (a no-op once the successor holds
                # the lease) — the same contract as the paper loop's
                # RunLockError exit.
                superseded = True
                logger.error("run lease lost: %s", exc)
                print(f"error: {exc}", file=sys.stderr)
                return 1
            except EngineConfigError as exc:
                # The loop's decision provider could not be built and the book
                # is FLAT (over a live position the loop contains this itself
                # — issue #268): an operator-fixable environment fault — a
                # failed engine import, a rejected env knob — named as such,
                # the paper lane's exit 1, not the generic "startup recovery
                # failed" below. The ``finally`` sweep runs over the flat
                # book; nothing there needs guarding.
                loop_refused = True
                logger.error("the engine could not be built: %s", exc)
                print(f"error: {exc}", file=sys.stderr)
                return 1
            except Exception as exc:  # noqa: BLE001 — arming is the one hard-error step
                # Full traceback to the log (this is the signed live path);
                # the message alone would leave a failure here undiagnosable.
                logger.exception("startup recovery failed")
                print(f"error: startup recovery failed — {exc}", file=sys.stderr)
                return 1
            finally:
                if loop_exit is not None:
                    # Protection-only (issue #268): the cause the operator
                    # must fix reaches stderr FIRST — here, ahead of the
                    # sweep's own WARNINGs and the unclean-sweep line this
                    # ``finally`` may print, and ahead of the exit-code
                    # dispatch after it, none of which may hide it.
                    if loop_exit.settled:
                        announce_protection_only_settled(loop_exit.cause, then="exiting")
                    else:
                        print(
                            "live loop exited from protection-only mode — §18.2 "
                            "shutdown sweep done; NEW decision cycles never ran "
                            f"because the engine could not be built: {loop_exit.cause}. "
                            "Fix the environment and re-run with --loop to resume "
                            "this run.",
                            file=sys.stderr,
                        )
                # Re-ASKED, not merely remembered: ``superseded`` is only True
                # when a heartbeat raised, and the Ctrl-C / SIGTERM lane reaches
                # here without one (see _still_owns_run).
                if superseded or not _still_owns_run(
                    db, run_id, pid=os.getpid(), now=datetime.now(timezone.utc)
                ):
                    # Lease lost: the successor owns every resting order and
                    # the dead-man's switch — skip the position re-read and the
                    # §18.2 sweep ENTIRELY; only the caller's pid-guarded lock
                    # release runs (and no-ops).
                    logger.info("lease takeover — §18.2 shutdown sweep skipped")
                else:
                    # The fresh reads the keep decision needs (a --loop run
                    # reconciles first — see live.shutdown.read_exit_state),
                    # then the decision itself.
                    exit_state = read_exit_state(session, reconcile_first=args.loop)
                    shutdown_verdict = classify_shutdown(
                        ShutdownFlags(
                            verdict_passed=verdict_passed,
                            loop_raised=loop_raised,
                            loop_refused=loop_refused,
                            protection_only=loop_exit is not None,
                            exit_state=exit_state,
                        )
                    )
                    keep_protective = shutdown_verdict.keep_protective
                    positions = exit_state.positions
                    if positions is None:
                        if keep_protective:
                            print(
                                "WARNING: positions could NOT be re-read at shutdown "
                                f"(unknown ≠ flat) and {shutdown_verdict.unclean_note} "
                                "— the §18.2 shutdown sweep leaves the bot's resting "
                                "SL/TP STANDING (reduce-only) and cancels other bot "
                                "orders. Re-run `live --run-id ...` (--loop only once the §20.2 smoke gate is open), or intervene manually.",
                                file=sys.stderr,
                            )
                        else:
                            # Truthful wording: "could not look" is not "holds" —
                            # but the operator action is the same (unknown ≠ flat).
                            print(
                                "WARNING: positions could NOT be re-read at shutdown "
                                "and this command's §18.2 shutdown sweep cancels "
                                "bot-owned protection orders — any live position is "
                                "UNPROTECTED after exit until a --loop run or "
                                "manual action re-covers it.",
                                file=sys.stderr,
                            )
                    elif positions:
                        held = ", ".join(f"{p.coin} {p.size}" for p in positions)
                        if keep_protective:
                            print(
                                f"WARNING: the account holds a live position ({held}) "
                                f"and {shutdown_verdict.unclean_note} — the §18.2 "
                                "shutdown sweep leaves the bot's resting SL/TP "
                                "STANDING (reduce-only) and cancels other bot orders. "
                                "Re-run `live --run-id ...` (--loop only once the §20.2 smoke gate is open), or intervene manually.",
                                file=sys.stderr,
                            )
                        else:
                            print(
                                f"WARNING: the account holds a live position ({held}) "
                                "and this command's §18.2 shutdown sweep "
                                "cancels bot-owned protection orders — the position "
                                "is UNPROTECTED after exit until a --loop run "
                                "or manual action re-covers it.",
                                file=sys.stderr,
                            )
                    shutdown_problem = sweep_on_exit(session, keep_protective=keep_protective)
                    if shutdown_problem is not None:
                        # Surfaced HERE, inside the ``finally``: when the body
                        # above raised (the except path already returned 1),
                        # the summary prints below never run — and "the
                        # wallet-wide trigger is still armed" is the one fact
                        # that must never exit silently, on any path.
                        print(
                            f"error: §18.2 shutdown unclean — {shutdown_problem}",
                            file=sys.stderr,
                        )
                        if keep_protective and session.kill_switch.armed:
                            # The calm "left STANDING" warning above and the
                            # armed trigger are the SAME orders' fate — say
                            # so, or the operator reads two disconnected
                            # facts and misses that the kept SL/TP die at
                            # the scheduleCancel deadline.
                            print(
                                "NOTE: the SL/TP described above as kept "
                                "STANDING are NOT safe while the wallet-wide "
                                "scheduleCancel stays armed — it cancels them "
                                "too at its deadline. Re-run `live --run-id ...`: its "
                                "clean shutdown sweep disarms the switch on "
                                "exit. The recovery itself ARMS the switch, and "
                                "a --loop run is refused while the §20.2 gate is "
                                "shut — or clear the trigger manually.",
                                file=sys.stderr,
                            )

            print(f"startup_reconciliation_passed: {'true' if result.passed else 'false'}")
            print(f"canceled_stale_orders: {len(result.canceled_stale)}")
            print(f"kept_orders: {len(result.kept_orders)}")
            state = session.safe_mode.current()
            print(f"safe_mode: {'none' if state is None else state.safe_mode_type}")
            if state is not None:
                print(f"safe_mode_reason: {state.reason}")
            if result.sweep_failures:
                for failure in result.sweep_failures:
                    print(f"error: stale-order sweep — {failure}", file=sys.stderr)
            exit_reason = classify_exit(
                verdict_passed=result.passed,
                sweep_unclean=shutdown_problem is not None,
                loop=args.loop,
                protection_only_settled=None if loop_exit is None else loop_exit.settled,
                safe_mode_latched=state is not None,
                kept_on_unknown_safe_mode=(
                    shutdown_verdict is not None and shutdown_verdict.kept_on_unknown_safe_mode
                ),
            )
            line = _exit_line(exit_reason)
            if line is not None:
                print(line, file=sys.stderr)
            return exit_reason.code
        finally:
            # Guarded: the release opens its own transaction (BEGIN IMMEDIATE),
            # which can raise on a busy store — and a raise in this ``finally``
            # would clobber the 0/4/1 verdict the body just computed,
            # surfacing as a generic exit 2. A lock row left behind is
            # diagnosable and reapable; a clobbered verdict is not.
            try:
                release_run_lock(db, run_id, pid=os.getpid(), now=datetime.now(timezone.utc))
            except Exception:  # noqa: BLE001
                logger.exception("run-lock release failed (the verdict above stands)")
