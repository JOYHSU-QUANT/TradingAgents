"""The ``live`` subcommand: Phase 3 startup gates + authorization (PR 1) and
the §19.1 startup recovery (PR 4), continuing into the live trading loop
under ``--loop`` (PR 5).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from ._common import (
    _migrate_owned_store,
    _open_owned_store,
    _raise_keyboard_interrupt,
    _require_agent_key,
    _require_api_key,
)
from ._drift import _HARD_DRIFT_KINDS, _config_drift_report, _norm_network, _run_config_subset
from .live_loop import _run_live_loop, _still_owns_run
from .live_shared import (
    _RECOVERY_MAX_TICK_GAP_SECONDS,
    _conflicting_run_lease,
    _smoke_gate_buckets,
    _timing_preflight,
)

logger = logging.getLogger(__name__)


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
    from ..domains.perp.risk_gate import RiskConfig
    from ..engine_bridge import load_config_or_exit
    from ..exchanges.hyperliquid.account import HyperliquidAccount
    from ..exchanges.hyperliquid.errors import ExchangeError
    from ..exchanges.hyperliquid.sdk_client import HyperliquidClient
    from ..exchanges.hyperliquid.signed_client import HyperliquidSignedClient
    from ..live.authorization import (
        EXPIRY_WARNING_HORIZON,
        AgentAuthorizationError,
        verify_agent_authorization,
    )
    from ..live.config import (
        EXCHANGE_MIN_ORDER_NOTIONAL_USDC,
        ExecutionMode,
        LiveConfig,
        compute_notional_caps,
        validate_live_risk_consistency,
    )
    from ..live.secrets import load_agent_key

    config = load_config_or_exit(args.config)
    if config is None:
        return 1
    raw_live = config.get("live")
    if raw_live is None:
        print(
            "error: config has no live: block — the live subcommand needs one "
            "(phase3-spec §4). Add it to the YAML and re-run.",
            file=sys.stderr,
        )
        return 1
    try:
        live_cfg = LiveConfig.from_dict(raw_live)
    except ValueError as exc:
        print(f"error: invalid live: config — {exc}. Fix the YAML and re-run.", file=sys.stderr)
        return 1
    if live_cfg.mode is ExecutionMode.PAPER:
        print(
            "error: live.mode is 'paper' — use the paper subcommand for paper "
            "runs; the live subcommand needs testnet_live or mainnet_tiny.",
            file=sys.stderr,
        )
        return 1
    # The AI gate (risk:) and the live hard caps (live.safety:) must agree on
    # the sizing regime — PR 5's loop wires them together, so a divergent
    # pair is a config mistake at load time, not a runtime surprise. The block
    # and its cross-checked fields must be operator-written (§24 — see
    # validate_live_risk_consistency for the vacuous-pass rationale).
    raw_risk = config.get("risk")
    if raw_risk is None:
        print(
            "error: config has no risk: block — the live subcommand refuses to "
            "run the AI gate on implicit defaults; write the block explicitly "
            "so the risk:/live.safety cross-check compares operator intent.",
            file=sys.stderr,
        )
        return 1
    try:
        risk_cfg = RiskConfig.from_dict(raw_risk)
        validate_live_risk_consistency(live_cfg, risk_cfg, raw_risk)
    except ValueError as exc:
        print(
            f"error: invalid risk:/live: config — {exc}. Fix the YAML and re-run.", file=sys.stderr
        )
        return 1

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
            from ..live.order_gate import RealOrderGate

            signed = HyperliquidSignedClient(
                live_cfg.network,
                agent_key,
                wallet_address=addr,
                gate=RealOrderGate.from_config(live_cfg),
                timeout=client.timeout,
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
    mode); 1 = hard failure (config/arming/creation errors).
    """
    import signal
    from decimal import Decimal

    from ..exchanges.hyperliquid.mapper import map_account_snapshot
    from ..exchanges.hyperliquid.sdk_client import call_sdk
    from ..exchanges.hyperliquid.signed_client import HyperliquidSignedClient
    from ..live.fill_backfill import FillBackfiller
    from ..live.fills import LiveFillProcessor
    from ..live.kill_switch import KillSwitchManager, refresh_across_blocking_work
    from ..live.order_gate import RealOrderGate
    from ..live.reconcile import LiveReconciler
    from ..live.safe_mode import SafeModeManager
    from ..live.startup import run_startup_recovery
    from ..live.venue_identity import (
        EscalationHolder,
        VenueIdentityMonitor,
        escalate_identity_fault,
    )
    from ..paper import accounting
    from ..paper.run_lock import (
        RunLockError,
        acquire_run_lock,
        peek_run_lock,
        release_run_lock,
    )
    from ..persistence import repository as repo
    from ..persistence.models import PositionState
    from ..persistence.schema import SCHEMA_VERSION

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
    payload_dir = db_path.resolve().parent / "payloads" / run_id
    now = datetime.now(timezone.utc)

    # The runtime gate: config pins the wire conditions; §6.1 passed above.
    gate = RealOrderGate.from_config(live_cfg)
    gate.agent_authorized = True
    signed = HyperliquidSignedClient(
        live_cfg.network,
        agent_key,
        wallet_address=wallet,
        gate=gate,
        timeout=client.timeout,
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

    # Opened as-is (issue #129 — see _open_owned_store). Unlike paper, this
    # command cannot take its lease before the upgrade: the drift and
    # off-coin checks between here and the lock read tables later migrations
    # have altered, and --create writes the run row before the lock. So the
    # refusals that need only the v1 ``runs`` row and the v3 lease columns run
    # first — run existence, wallet-sibling lease, run mode, this run's own
    # lease (read-only) — and the store is migrated once they all pass. The
    # definitive lease is still taken below; a process starting concurrently
    # loses there, having written nothing the migration cannot share. The
    # peek exempts no pid, not even this one: a lease stamped with a pid the
    # OS recycled to us after a hard kill refuses here for LOCK_STALE_SECONDS,
    # where acquire alone would have silently re-taken it.
    db = _open_owned_store(db_path)
    if db is None:
        return 1
    with db:
        existing_run = repo.get_run(db.conn, run_id)
        is_restart = existing_run is not None
        if not is_restart and not args.create:
            print(
                f"error: run {run_id!r} does not exist in {db_path}. Pass --create "
                "to start it, or fix --run-id / --db to resume the intended run.",
                file=sys.stderr,
            )
            return 1
        if is_restart and args.create:
            print(
                f"error: run {run_id!r} already exists in {db_path}. Drop --create "
                "to resume it, or pick a new --run-id for a fresh run.",
                file=sys.stderr,
            )
            return 1
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
        if existing_run is not None and existing_run["mode"] != "live":
            # Resume validates the run's IDENTITY before any side effect (the
            # lock, arming the wallet-wide kill switch, reconciliation writes)
            # — the same discipline as the paper daemon's resume (decided
            # 2026-07-17). A typo'd --run-id/--db pointing at a paper run
            # would otherwise arm the kill switch over a paper ledger and
            # write live snapshots into it. The mode is a v1 column, so this
            # runs before the migration too: a typo must not upgrade a paper
            # store on its way to being refused.
            print(
                f"error: run {run_id!r} in {db_path} is a {existing_run['mode']} "
                "run — resuming it here would arm the kill switch and "
                f"reconcile a {existing_run['mode']} ledger against the live "
                "exchange. Fix --run-id / --db.",
                file=sys.stderr,
            )
            return 1
        try:
            peek_run_lock(db, run_id, now=now)
        except RunLockError as exc:
            print(f"error: {exc}", file=sys.stderr)
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
            seeds = [
                PositionState(coin=p.coin, size=p.size, entry_price=p.entry_price)
                for p in snapshot.positions
            ]
            subset = _run_config_subset(config, coin)
            subset["live"] = raw_live
            accounting.initialize_run(
                db,
                run_id=run_id,
                mode="live",
                initial_balance_usdc=snapshot.account_value - unrealized,
                schema_version=SCHEMA_VERSION,
                initial_positions=seeds,
                config_json=json.dumps(subset, ensure_ascii=False, default=str),
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
            # §13.5 (issue #80): ONE venue-identity monitor for the whole
            # process. The kill switch's disarm cross-check, the reconciler's
            # per-order probes and (under --loop) the §17 protection manager
            # all read orderStatus through it, so consecutive answers the
            # venue cannot make about OUR cloids are one streak wherever they
            # were asked — and its payload_dir keeps every refused answer.
            identity = VenueIdentityMonitor(
                query_order_by_cloid=signed.query_order_by_cloid,
                db=db,
                run_id=run_id,
                symbol=coin,
                payload_dir=payload_dir,
            )
            kill_switch = KillSwitchManager(
                client=signed,
                gate=gate,
                db=db,
                run_id=run_id,
                config=live_cfg.kill_switch,
                # The preflight above already proved this invariant with the
                # SAME constant and the SAME timeout, so this constructor cannot
                # raise on timing. Both are passed explicitly: the timeout used to
                # be probed off the client with getattr, which silently dropped
                # the term on every production manager.
                max_tick_gap_seconds=_RECOVERY_MAX_TICK_GAP_SECONDS,
                network_timeout_s=signed.timeout,
                payload_dir=payload_dir,
                identity=identity,
            )
            safe_mode = SafeModeManager(db=db, run_id=run_id, gate=gate)
            processor = LiveFillProcessor(
                db=db,
                run_id=run_id,
                payload_dir=payload_dir,
                wallet_address=signed.wallet_address,
            )

            # §18.2: both of these block the single-threaded tick for far longer
            # than one round-trip (a paged backfill, a per-order orderStatus
            # sweep), and the tick's own refresh happens before either runs — so
            # they refresh across their own work (2026-07-31 deadline review).
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
                fetch_fills=signed.user_fills_by_time,
                backfiller=backfiller,
                payload_dir=payload_dir,
                refresh_kill_switch=_refresh_across_sweep,
                identity=identity,  # owns the orderStatus seam (§13.5)
            )
            shutdown_problem: str | None = None
            superseded = False
            # False until the §19.1 verdict PASSES — a recovery that raised
            # counts as unclean too. The ``finally`` sweep below keys its
            # keep-the-SL/TP decision off this OR'd with the EXIT-TIME safe
            # mode (both decided 2026-07-22): over a --loop run the boot
            # verdict goes stale, and safe mode latched mid-loop means the
            # next boot's verdict will refuse to start — exactly the
            # "no repair machinery" case the keep exists for.
            verdict_passed = False
            # True when the shutdown-time safe-mode read RAISED: the keep
            # decision then acted on unknown ≠ clean, and the exit code below
            # must not let a luckier later read report "all quiet" over
            # deliberately-kept orders.
            exit_safe_mode_unknown = False
            try:
                result = run_startup_recovery(
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
                verdict_passed = result.passed
                # PR 5: with --loop, a passing recovery hands off to the live
                # trading loop; it returns on Ctrl-C / SIGTERM, and the §18.2
                # shutdown sweep in the ``finally`` below then disarms the switch.
                if args.loop and result.passed:
                    _run_live_loop(
                        cfgs=loop_cfgs,
                        db=db,
                        run_id=run_id,
                        coin=coin,
                        config=config,
                        live_cfg=live_cfg,
                        client=client,
                        signed=signed,
                        gate=gate,
                        kill_switch=kill_switch,
                        safe_mode=safe_mode,
                        reconciler=reconciler,
                        processor=processor,
                        payload_dir=payload_dir,
                        fetch_clearinghouse=fetch_clearinghouse,
                        identity=identity,
                    )
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
            except Exception as exc:  # noqa: BLE001 — arming is the one hard-error step
                # Full traceback to the log (this is the signed live path);
                # the message alone would leave a failure here undiagnosable.
                logger.exception("startup recovery failed")
                print(f"error: startup recovery failed — {exc}", file=sys.stderr)
                return 1
            finally:
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
                    # §12.2 rule 8 (the --loop path): the loop has been placing
                    # orders since the startup verdict pass — reconcile once
                    # more so the sweep below works from fresh exchange state.
                    # Runs FIRST, before the position/safe-mode reads below:
                    # this pass can itself ENTER safe mode (an unclean final
                    # reconcile), and a keep decision computed from the
                    # pre-reconcile snapshot would strip SL/TP at exactly the
                    # moment the problem was found. Best-effort: a failure must
                    # not block the sweep.
                    if args.loop:
                        try:
                            reconciler.reconcile_and_apply(
                                "shutdown",
                                safe_mode=safe_mode,
                                ws_restored=True,
                                kill_switch_active=not kill_switch.stop_new_orders,
                            )
                        except Exception:  # noqa: BLE001
                            logger.exception(
                                "§12.2 pre-shutdown reconciliation failed (sweep proceeds)"
                            )
                    # Decided 2026-07-16, revised 2026-07-22: on a PASSING
                    # verdict the §18.2 semantics stand (shutdown cancels ALL
                    # bot-owned orders, the §19.3-kept SL/TP included), but
                    # never silently over a live position. On an UNCLEAN exit
                    # (verdict failed, recovery raised, or safe mode active at
                    # exit — the boot verdict goes stale over a loop) the sweep
                    # now KEEPS
                    # the resting SL/TP over a live — or unreadable, and
                    # unreadable ≠ flat is the startup sweep's own rule —
                    # position: stripping reduce-only protection trades an
                    # unclean verdict for a naked position, and the repair
                    # machinery that could re-cover it is exactly what an
                    # unclean verdict refuses to start. The position that
                    # decides both the warning and the keep is read FRESH here
                    # (decided 2026-07-17): the boot snapshot can be minutes
                    # stale after arming/backfill/two reconcile passes, and a
                    # position acquired mid-recovery would otherwise lose its
                    # protection to the sweep below without a word.
                    # None = could not read (unknown ≠ flat), the same sentinel
                    # convention as ``_safe_fetch_open_orders``.
                    fresh_positions: list | None
                    try:
                        fresh_positions = map_account_snapshot(fetch_clearinghouse()).positions
                    except Exception:  # noqa: BLE001 — the warning must not mask the verdict
                        logger.exception("shutdown position re-read failed")
                        fresh_positions = None
                    # Exit-time safe mode is read FRESH for the same reason the
                    # position is: the boot verdict is stale after a loop. A
                    # failed read is unknown ≠ clean — fail toward keeping.
                    try:
                        exit_safe_mode = safe_mode.active
                    except Exception:  # noqa: BLE001
                        logger.exception("shutdown safe-mode read failed")
                        exit_safe_mode = True
                        exit_safe_mode_unknown = True
                    keep_protective = (not verdict_passed or exit_safe_mode) and (
                        fresh_positions is None or bool(fresh_positions)
                    )
                    # Three distinct causes, three truthful notes: a FAILED
                    # read is not "safe mode is active" — claiming so would
                    # contradict the fresh `safe_mode:` line printed later
                    # when the second read succeeds and finds none.
                    if not verdict_passed:
                        unclean_note = "the startup verdict did not pass"
                    elif exit_safe_mode_unknown:
                        unclean_note = (
                            "the exit-time safe-mode state could NOT be read (unknown ≠ clean)"
                        )
                    else:
                        unclean_note = "safe mode is active at exit"
                    if fresh_positions is None:
                        if keep_protective:
                            print(
                                "WARNING: positions could NOT be re-read at shutdown "
                                f"(unknown ≠ flat) and {unclean_note} "
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
                    elif fresh_positions:
                        held = ", ".join(f"{p.coin} {p.size}" for p in fresh_positions)
                        if keep_protective:
                            print(
                                f"WARNING: the account holds a live position ({held}) "
                                f"and {unclean_note} — the §18.2 "
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
                    # Leave nothing resting behind a dead man's switch nobody
                    # will refresh — except the deliberately-kept SL/TP when
                    # ``keep_protective`` is set (reduce-only; the disarm below
                    # is what lets them outlive the wallet-wide trigger). The
                    # §18.2 shutdown sweep cancels the rest of the bot-owned
                    # open orders and disarms only on a clean sweep.
                    # (The §12.2 "before shutdown" reconciliation: for the
                    # one-shot command it is the verdict pass that just ran — no
                    # orders are placed after it; the --loop path runs the fresh
                    # pass above.)
                    if kill_switch.armed:
                        # Guarded because a raise inside this ``finally`` would
                        # DISCARD the computed verdict (nothing prints, the
                        # documented 0/4/1 contract becomes a generic exit 2) —
                        # shutdown()'s audit writes are fail-loud by design, so a
                        # busy DB here is a realistic raise, not an edge case.
                        try:
                            kill_switch.shutdown(keep_protective=keep_protective)
                        except Exception as exc:  # noqa: BLE001
                            logger.exception("§18.2 shutdown sweep raised")
                            shutdown_problem = f"shutdown sweep raised: {exc}"
                        else:
                            if kill_switch.armed:
                                # shutdown() disarms only on a clean sweep: still
                                # armed means bot orders may rest and the
                                # wallet-wide scheduleCancel WILL fire at the
                                # deadline — taking out non-bot orders §25 says
                                # never to touch. Loud, and never exit 0.
                                shutdown_problem = (
                                    "shutdown sweep left the kill switch armed — bot "
                                    "orders may still rest and the wallet-wide "
                                    "scheduleCancel will fire at the deadline"
                                )
                                logger.error("§18.2 %s", shutdown_problem)
                        # §13.5 (issue #80): the sweep's disarm cross-check just
                        # probed orderStatus through the shared monitor, and
                        # this is the LAST holder of the safe-mode machine to
                        # run — so it reads the latch (inside the armed branch
                        # on purpose: an unarmed switch ran no cross-check, and
                        # a latch from the loop was escalated by the engine
                        # every tick). Every holder's enter persists the same
                        # durable state; this call is the last chance before
                        # the process exits, and that state is what the next
                        # boot hydrates — its verdict then cannot pass until
                        # §13.6 releases, instead of every shutdown blocking
                        # its disarm with only a log line. Fed into
                        # ``shutdown_problem`` so the one-shot lane exits 4 the
                        # way it does for an armed switch — a run that just
                        # latched manual safe mode must never hand exit 0 to
                        # its supervisor. Best-effort like every other write in
                        # this finally: a busy DB must not discard the verdict.
                        try:
                            if escalate_identity_fault(
                                identity, safe_mode, holder=EscalationHolder.SHUTDOWN
                            ):
                                shutdown_problem = (
                                    "venue identity fault latched — the exchange kept "
                                    "answering orderStatus about orders that are not "
                                    "ours; manual safe mode entered (see "
                                    "identity_fault_latched in protection_order_events "
                                    "and payloads/orderStatus-*.json)"
                                    + (f"; also: {shutdown_problem}" if shutdown_problem else "")
                                )
                        except Exception:  # noqa: BLE001
                            logger.exception(
                                "could not persist the venue-identity escalation at shutdown"
                            )
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
                            if keep_protective and kill_switch.armed:
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
            state = safe_mode.current()
            print(f"safe_mode: {'none' if state is None else state.safe_mode_type}")
            if state is not None:
                print(f"safe_mode_reason: {state.reason}")
            if result.sweep_failures:
                for failure in result.sweep_failures:
                    print(f"error: stale-order sweep — {failure}", file=sys.stderr)
            if result.passed:
                if shutdown_problem is not None:
                    # Decided 2026-07-17: exit 0 means "all quiet" to a
                    # supervisor — a passing verdict with an unclean shutdown
                    # sweep is NOT that; it folds into the same
                    # executed-but-unclean code 4 the verdict path uses (the
                    # "§18.2 shutdown unclean" line above carries the detail).
                    return 4
                if args.loop:
                    if state is not None or (exit_safe_mode_unknown and keep_protective):
                        # Sibling of the keep decision (2026-07-22): the boot
                        # verdict is stale after a loop, and a run that latched
                        # safe mode mid-loop must not hand exit 0 ("all quiet")
                        # to its supervisor. Same executed-but-unclean code 4
                        # the one-shot path returns when ITS verdict finds safe
                        # mode active. A FAILED shutdown safe-mode read that
                        # actually KEPT orders counts too — the sweep just
                        # acted on unknown ≠ clean, and a luckier later read
                        # must not talk the exit code back down to 0 over
                        # deliberately-kept orders. (Unknown read over a
                        # confirmed-flat book kept nothing and changed nothing:
                        # exit stays state-driven, no supervisor false alarm.)
                        print(
                            "live loop exited IN SAFE MODE — see safe_mode above; "
                            "resolve it (manual release if required) before resuming."
                            if state is not None
                            else "live loop exited with protective orders kept behind "
                            "a FAILED shutdown safe-mode read (unknown ≠ clean) — "
                            "inspect the run store before resuming.",
                            file=sys.stderr,
                        )
                        return 4
                    print(
                        "live loop exited — §18.2 shutdown sweep done; re-run "
                        "with --loop to resume this run.",
                        file=sys.stderr,
                    )
                else:
                    print(
                        "startup recovery passed — a live loop can start from "
                        "this state (re-run with --loop).",
                        file=sys.stderr,
                    )
                return 0
            print(
                "startup recovery did NOT pass — the run is in safe mode; see the "
                "reconciliation events / safe-mode state above (§19.1 step 15).",
                file=sys.stderr,
            )
            # Exit 4, not 1: "executed fine, verdict unclean" is an operator
            # signal distinct from a hard failure — same multi-code convention
            # as validate's 4/5 (decided 2026-07-16).
            return 4
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
