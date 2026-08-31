"""Helpers shared by the ``live`` and ``live-smoke`` entry points.

Extracted ahead of the live/smoke split on purpose: ``live``’s startup gate
reads the §20.2 smoke buckets and both entry points share the timing preflight
and the per-wallet lease guard, so ``live.py`` ↔ ``smoke.py`` would otherwise
import-cycle.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone

from ._drift import _norm_network

# This command's worst-case wall time between two kill-switch tick() calls is
# one reconciliation sweep (network reads, seconds) — 30s is generous and keeps
# the constructor invariant honest under the default 120s/30s switch config.
# ONE constant, read by both the pre-side-effect preflight check and the
# KillSwitchManager construction below, so the number the preflight proves is
# the number the constructor enforces.
_RECOVERY_MAX_TICK_GAP_SECONDS = 30.0


def _timing_preflight(live_cfg, client) -> int:
    """The §18.2 refresh-timing preflight: ``0`` to proceed, ``1`` to refuse.

    Shared by EVERY entry point that arms the dead man's switch, which is the
    whole point. `live` refused a violating config here with a named exit 1 that
    says which two knobs to move; `live-smoke` had no such check, so the same
    bad config reached ``KillSwitchManager.__init__``, raised, was contained as
    a ``SmokePreflightError`` and surfaced as exit 4. RUNBOOK §5 answers exit 4
    with "the run state is unclean — check `safe-mode --status`", which is the
    wrong investigation entirely, and §8 tells a supervisor to branch its
    restart policy on 1-vs-4, so a script mis-routes it too (2026-07-31).

    Refusing BEFORE any side effect (run row, run lock, wire action) is the
    point of a preflight; ``kill_switch_timing_violation``'s docstring owns the
    invariant itself.
    """
    from ..live.kill_switch import (
        kill_switch_timing_violation,
        network_timeout_warning,
        sl_repair_delay_warning,
    )

    violation = kill_switch_timing_violation(
        live_cfg.kill_switch, _RECOVERY_MAX_TICK_GAP_SECONDS, client.timeout
    )
    if violation is not None:
        print(
            f"error: {violation}. Raise live.kill_switch.schedule_cancel_seconds, "
            "lower live.kill_switch.refresh_interval_seconds, or lower "
            "network_timeout_s — the failed attempt's own timeout is part of the "
            "budget, so the 30s default does not fit a 120s scheduled cancel "
            "(RUNBOOK §1.5 uses 8).",
            file=sys.stderr,
        )
        return 1
    # Its advisory sister (decided 2026-07-22 "soft mitigation"): warn — do
    # not refuse — when the per-request REST timeout cannot keep the same
    # max_tick_gap promise. Beside the enforced check so the two halves of
    # the §18.2 timing story stay in one place.
    for advisory in (
        network_timeout_warning(client.timeout, _RECOVERY_MAX_TICK_GAP_SECONDS),
        sl_repair_delay_warning(
            float(live_cfg.protection.sl_repair_retry_delay_seconds),
            _RECOVERY_MAX_TICK_GAP_SECONDS,
        ),
    ):
        if advisory is not None:
            print(f"WARNING: {advisory}", file=sys.stderr)
    return 0


def _run_genesis_network(config_json: str | None) -> object | None:
    """``live.network`` from a run's genesis config, or None if unreadable.

    Same defensive shape as :func:`live.validation.execution_mode`: a corrupt or
    non-object ``config_json`` degrades to None rather than crashing a guard.

    NORMALISED through the same helper the drift report uses, because LiveConfig
    reads network case-insensitively: two runs whose genesis says "Testnet" and
    "testnet" are the same exchange and the same wallet. Comparing the raw
    strings made them look like different networks, sending the guard below
    fail-OPEN in the one direction its own docstring forbids — "the cost of a
    false pass is a stripped dead-man switch on a live wallet" (2026-07-31).
    """
    if not config_json:
        return None
    try:
        parsed = json.loads(config_json)
    except (ValueError, TypeError):
        return None
    live = parsed.get("live") if isinstance(parsed, dict) else None
    if isinstance(live, dict) and isinstance(live.get("network"), str):
        return _norm_network(live)
    return None


def _conflicting_run_lease(
    db, run_id: str, *, own_network: object | None = None
) -> tuple[str, int] | None:
    """``(run_id, pid)`` of a SAME-NETWORK sibling holding a FRESH lease, else None.

    The run lease is per-``run_id``; the kill switch, ``updateLeverage`` and the
    §19.3 sweep are per-WALLET. So "my run's lease is free" does not mean "no one
    else is on this wallet" (2026-07-30 concurrency review).

    But "same store" is NOT the same as "same wallet": RUNBOOK-live §7.3
    deliberately creates the mainnet_tiny run in the SAME ``live_trading.db`` as
    the testnet run, and those are different exchanges with different accounts —
    a testnet ``scheduleCancel`` cannot reach a mainnet order. Keying the refusal
    on the store alone told the operator to stop a real-money run in order to
    smoke-test testnet. It is keyed on the sibling's genesis ``live.network``
    instead (2026-07-31 exit check).

    Both networks are read from the STORE (each run's own genesis), not from the
    caller's session, so the guard is self-contained and cannot disagree with
    what the runs were actually created as.

    ``own_network`` overrides that read for the one caller that has no genesis to
    read yet: `live --create` must refuse BEFORE it writes the run row, or a
    refusal leaves a half-created run whose re-run is then rejected as "already
    exists" (2026-07-31 exit check). The sibling's network still comes from the
    store, so the comparison is never made against the caller's own idea of what
    the OTHER run is.

    Either side being UNREADABLE is treated as a conflict: a corrupt genesis is
    not evidence of safety, and the cost of a false refusal is one operator
    message, while the cost of a false pass is a stripped dead-man switch on a
    live wallet.
    """
    from ..common.instants import parse_instant
    from ..paper.run_lock import LOCK_STALE_SECONDS
    from ..persistence import repository as repo

    if own_network is None:
        own = repo.get_run(db.conn, run_id)
        own_network = None if own is None else _run_genesis_network(own["config_json"])
    now = datetime.now(timezone.utc)
    for row in repo.iter_other_run_leases(db.conn, run_id):
        age = (now - parse_instant(row["lock_heartbeat_at"])).total_seconds()
        if age >= LOCK_STALE_SECONDS:
            continue
        sibling = repo.get_run(db.conn, str(row["run_id"]))
        if sibling is not None and sibling["mode"] != "live":
            # A PAPER run. It signs nothing and holds no wallet, so it cannot be
            # harmed by an account-wide action and cannot take one. Checked
            # before the network comparison because a paper genesis has no
            # ``live`` block at all: it would read as an unreadable network and
            # be refused fail-closed, with a message about stripping a dead-man
            # cover it never had (2026-07-31).
            continue
        sibling_network = None if sibling is None else _run_genesis_network(sibling["config_json"])
        if (
            own_network is not None
            and sibling_network is not None
            and sibling_network != own_network
        ):
            # A different exchange entirely — its wallet cannot be touched by
            # anything this suite does.
            continue
        return str(row["run_id"]), int(row["lock_pid"])
    return None


def _smoke_gate_buckets(
    missing: tuple[str, ...],
    failed: tuple[str, ...],
    errored: tuple[str, ...],
) -> list[tuple[str, tuple[str, ...]]]:
    """``(label, keys)`` pairs for the §20.2 gate's non-passed buckets.

    The one list BOTH renderings draw from (``live-smoke``'s per-line report and
    the ``--loop`` refusal's one-liner), so a future bucket cannot appear on one
    operator surface and not the other.
    """
    return [("not_yet_run", missing), ("failed", failed), ("errored", errored)]


def _print_smoke_gate(
    passed: bool,
    missing: tuple[str, ...],
    failed: tuple[str, ...],
    errored: tuple[str, ...],
) -> None:
    """Print the §20.2 cycle-entry gate verdict (stdout: the machine contract).

    The three non-passed buckets are printed separately so an operator triaging
    a real-money go/no-go sees whether a test never ran, the exchange refused it,
    or the harness itself broke — without querying live_smoke_tests.
    """
    print(f"smoke_gate_passed: {'yes' if passed else 'no'}")
    for label, keys in _smoke_gate_buckets(missing, failed, errored):
        if keys:
            print(f"{label}: {', '.join(keys)}")
