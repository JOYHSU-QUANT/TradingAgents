"""The dead man's switch's timing budget, as checkable messages (phase3-spec §18.2).

Kept apart from :class:`~.kill_switch.KillSwitchManager` because its readers
are not the manager: the CLI's live preflight runs the invariant and the two
advisories BEFORE a manager exists, and the sites that block the live loop
refresh through :func:`refresh_across_blocking_work` against anything that
can ``tick()``. The two budget constants live with the arithmetic that has
to know them.
"""

from __future__ import annotations

import logging
from typing import Protocol

from ..common.instants import Seconds, seconds_span
from .config import KillSwitchConfig

__all__ = [
    "FAILURE_BACKOFF_FRACTION",
    "MAX_UNREFRESHED_REST_CALLS",
    "kill_switch_timing_violation",
    "network_timeout_warning",
    "refresh_across_blocking_work",
    "sl_repair_delay_warning",
]

logger = logging.getLogger(__name__)


class _Refreshable(Protocol):
    """What :func:`refresh_across_blocking_work` needs of a switch: one ``tick()``."""

    def tick(self) -> None: ...


def kill_switch_timing_violation(
    config: KillSwitchConfig,
    max_tick_gap_seconds: Seconds,
    network_timeout_s: float | None = None,
) -> str | None:
    """The constructor's refresh-timing invariant as a checkable message.

    THE canonical account of the invariant (other sites point here). The worst
    case between two SUCCESSFUL refreshes walks the whole failure path, in the
    order the code actually executes it::

        refresh_interval          the deadline-to-due wait
      + max_tick_gap              the tick after it comes due lands a gap late
      + network_timeout_s         THAT attempt fails only after burning a timeout
      + min(timeout, backoff_cap) the failure then suppresses the retry
      + max_tick_gap              and the retry ALSO waits for a tick

    and that must stay strictly inside ``schedule_cancel``, or the dead man's
    switch fires during normal operation and cancels every order on the wallet.
    The config layer's own guard (``schedule_cancel >= 2 × refresh``) is only
    the special case of a caller ticking exactly at the interval: it cannot see
    the caller's tick gap at all.

    Two of those five terms were missing until 2026-08-01, and both were owed by
    the very commit that introduced the backoff: a failed attempt does not fail
    instantly (``_retry_not_before`` is computed from ``failed_at``, i.e. AFTER
    the request burned up to ``network_timeout_s``), and the backoff creates a
    SECOND wait-for-a-tick stage (due→tick, then backoff-expiry→tick). Omitting
    them let ``refresh=30 / max_tick_gap=30 / network_timeout=8 /
    schedule_cancel=80`` pass at a computed 75s while the real worst case is
    106s — the switch firing during normal operation, which is the single thing
    this invariant exists to prevent (2026-08-01 rules-vs-untouched-code review).

    ``network_timeout_s`` is the ONE term of the five that does not live in
    ``KillSwitchConfig`` (it is the top-level REST timeout), so it is passed in.
    When it is None the term is DROPPED rather than treated as unbounded: a
    stubbed client has no timeout to read, and refusing to construct on that
    would fail wiring this invariant is not about. The unbounded case belongs to
    :func:`network_timeout_warning`, which already fires on None. Even with the
    term dropped this check is strictly tighter than the version it replaces,
    which counted only ONE ``max_tick_gap``.

    Returns the violation text, or None when the timing is sound. ONE
    definition, used by the constructor (which raises on it) and by the CLI's
    live preflight — the preflight exists because the constructor runs only
    AFTER ``--create`` has written the run row and taken the run lock, so a
    violating config would otherwise surface as a late ValueError → generic
    exit 2 outside the documented 0/4/1 contract, with side effects already
    on disk (decided 2026-07-17).
    """
    # The gap is converged first, HERE rather than in the constructor, so the
    # preflight and the constructor read one number the same way (issue #224):
    # a bool, NaN or infinity would pass a bare ``<= 0`` and then satisfy or
    # fail the sum below for no reason the message could state — NaN in
    # particular sums to NaN, which compares under any deadline. Refused by
    # name as a violation message, which is what both callers read.
    try:
        max_tick_gap_seconds = seconds_span(
            "max_tick_gap_seconds", max_tick_gap_seconds
        ).total_seconds()
    except (TypeError, ValueError) as exc:
        return str(exc)
    # Every term above, summed in the same order. The failed attempt's own wall
    # time and the second tick wait are NOT refinements — they are the two
    # largest terms after the interval itself, and leaving them out is what made
    # the pre-2026-08-01 check accept configs that fire the switch.
    backoff_cap = config.refresh_interval_seconds * FAILURE_BACKOFF_FRACTION
    # The backoff is min(what the attempt cost, the cap) — see _in_failure_backoff.
    # With no timeout to read, the cap is the only bound we have.
    failed_attempt_cost = 0.0 if network_timeout_s is None else float(network_timeout_s)
    backoff = backoff_cap if network_timeout_s is None else min(failed_attempt_cost, backoff_cap)
    worst_case_gap = (
        config.refresh_interval_seconds
        + max_tick_gap_seconds
        + failed_attempt_cost
        + backoff
        + max_tick_gap_seconds
    )
    if worst_case_gap >= config.schedule_cancel_seconds:
        timeout_term = (
            "network timeout unknown"
            if network_timeout_s is None
            else f"failed-attempt network_timeout_s {failed_attempt_cost:g}s"
        )
        return (
            f"the kill switch cannot be refreshed in time: a refresh may land "
            f"{worst_case_gap:g}s apart (refresh_interval "
            f"{config.refresh_interval_seconds}s + max_tick_gap "
            f"{max_tick_gap_seconds:g}s + {timeout_term} + post-failure backoff "
            f"{backoff:g}s + a second max_tick_gap {max_tick_gap_seconds:g}s "
            f"before the retry), but the scheduled cancel fires after "
            f"{config.schedule_cancel_seconds}s — the dead man's switch would "
            "cancel every order on the wallet during normal operation"
        )
    return None


# How much of one refresh interval a FAILED attempt may suppress the retry for
# (see KillSwitchManager._in_failure_backoff). Named and shared because
# kill_switch_timing_violation has to budget for it: the backoff lengthens the
# worst-case gap between two successful refreshes, and an invariant that does not
# know about it is checking a slack the code no longer has.
FAILURE_BACKOFF_FRACTION = 0.5


# The longest run of BACK-TO-BACK REST calls a tick can make with no
# :func:`refresh_across_blocking_work` between them — the order-submission chain
# in ``orders.submit_limit``: the §8.3 pre-check recovery probe (which falls
# THROUGH when it cannot resolve the cloid, rather than returning), the place
# itself, and the duplicate-ack recovery probe. Each rides the full
# ``network_timeout_s``.
#
# Everything else refreshes ACROSS its blocking work and so contributes a run of
# ONE however many calls it makes: protection's repair ladder and its orderStatus
# confirmations, the reconcile legs and their per-order loops, BOTH page ladders
# (the fill backfill's and the reconcile fill cross-check's own inline one), the
# day-roll baseline read, and — since 2026-08-01 — the decision cycle's five
# market-data reads in ``engine_bridge._build_context`` (perp meta, snapshot,
# the exchange clock, candles, funding), which ``driver.pump()`` runs on
# this same thread.
#
# That last one is why this is 3 rather than 5. Those reads share
# ``network_timeout_s`` (``HyperliquidClient.from_config`` resolves from that key
# and Info() fetches perp meta at construction), so unrefreshed they were the
# longest chain and forced the advisory to demand <7.5s. But a live decision
# cycle has NO within-cycle retry: one market read that times out fail-closes the
# cycle and re-anchors to the next 4h boundary. Budgeting for that chain would
# have bought kill-switch headroom with a possible 4-hour decision blackout, so
# the chain was broken up instead.
#
# This number is a MAXIMUM OVER CHAINS, so every seam between two chains has to
# refresh or the truth becomes their SUM. Twice that was the bug:
#   - the first pass wired only the backfiller's ladder and left the
#     cross-check's identical one untouched, making the real maximum 20
#     (DEFAULT_MAX_PAGES) while this said 3;
#   - the second left ``engine.tick()`` and ``driver.pump()`` adjacent with
#     nothing between them, so the submit chain and the build_context chain ran
#     back to back for a real maximum of 7 (the build_context chain was four reads then) (2026-08-01 lifecycle review).
# Any new REST loop MUST refresh per iteration, and any new pair of blocking
# calls MUST refresh between them, or this number is a lie.
MAX_UNREFRESHED_REST_CALLS = 3


def network_timeout_warning(timeout: float | None, max_tick_gap_seconds: float) -> str | None:
    """The §18.2 residual-risk advisory as a checkable message (PR 5, decided
    2026-07-22 "soft mitigation").

    Sister of :func:`kill_switch_timing_violation`, advisory rather than
    enforced: nothing ties the per-request REST timeout to the caller's
    ``max_tick_gap_seconds`` promise, and REST calls riding their full timeout
    on a degraded (slow, not dead) network can stretch the wall gap past the
    promise — the dead man's switch then cancels the resting SL/TP while the
    process is still alive (protection re-covers on the next healthy tick, an
    unprotected window).

    Budgets ``MAX_UNREFRESHED_REST_CALLS``, not one. The single-call form
    called a 10s timeout sound against a 30s gap while one unrefreshed chain
    could spend 30s inside it — the arithmetic contradicted the very sentence
    this docstring opened with ("a tick makes several sequential REST calls") and
    under-reported the one number the operator can actually turn (2026-07-31
    deadline review).

    Returns the warning text when the timeout cannot keep the promise (or is
    unbounded), None when it fits. The hard construction-time invariant is
    deferred to the network-layer rework.
    """
    budget = max_tick_gap_seconds / MAX_UNREFRESHED_REST_CALLS
    if timeout is not None and timeout * MAX_UNREFRESHED_REST_CALLS < max_tick_gap_seconds:
        return None
    return (
        f"network_timeout_s ({timeout}) leaves no room under the kill switch's "
        f"max tick gap ({max_tick_gap_seconds:g}s): an order submission can make "
        f"{MAX_UNREFRESHED_REST_CALLS} back-to-back REST calls with no refresh "
        "between them, so on a degraded network that chain alone can push the "
        "§18.2 refresh past the exchange-side deadline and cancel the resting "
        f"SL/TP. Set network_timeout_s below {budget:g} in the config for headroom."
    )


def sl_repair_delay_warning(delay_seconds: float, max_tick_gap_seconds: float) -> str | None:
    """The SL-repair-ladder sibling of :func:`network_timeout_warning` (advisory).

    ``protection._maybe_delay`` sleeps the FULL configured
    ``sl_repair_retry_delay_seconds`` between repair attempts and only refreshes
    the kill switch AFTER the sleep — so a delay at or above the
    ``max_tick_gap_seconds`` promise stretches the refresh gap during an SL
    repair episode, the one window where the position has no valid stop. Push it
    past the scheduleCancel budget and the dead man's switch cancels every
    resting order mid-repair. Config only enforces ``> 0``; like its sister
    this is a warn-not-refuse preflight, with the hard construction-time
    invariant deferred to the network-layer rework.

    Budgeted against ONE SLOT of ``MAX_UNREFRESHED_REST_CALLS``, not the whole
    tick gap — the same three-way split ``network_timeout_warning`` uses and that
    ``protection._MAX_REPAIR_SLEEP_S`` clamps the backoff to. Comparing against
    the WHOLE gap left the band between one slot and the gap (10s..30s at the
    defaults) both unclamped — ``_maybe_delay`` never shortens a CONFIGURED delay
    — and unwarned, so a 25s delay burned two and a half slots of the very budget
    with nothing said (2026-08-01 round-13 exit check). The default (5s against a
    10s slot) still never warns.
    """
    budget = max_tick_gap_seconds / MAX_UNREFRESHED_REST_CALLS
    if delay_seconds < budget:
        return None
    return (
        f"live.protection.sl_repair_retry_delay_seconds ({delay_seconds:g}) is not "
        f"below its share of the kill switch's max tick gap "
        f"({max_tick_gap_seconds:g}s / {MAX_UNREFRESHED_REST_CALLS} = {budget:g}s) "
        "— the repair ladder sleeps that long between attempts while the "
        "position has no valid stop, and can push the §18.2 refresh past the "
        "exchange-side deadline, cancelling every resting order mid-repair. "
        f"Set it below {budget:g} for headroom."
    )


def refresh_across_blocking_work(kill_switch: _Refreshable | None, *, what: str) -> None:
    """Refresh the dead man's switch across a long blocking operation.

    THE helper for :meth:`KillSwitchManager.tick`'s stated contract — "the owner
    must call this at least once per ``max_tick_gap_seconds``, INCLUDING from
    inside a long decision cycle". The switch does not refresh itself, and the
    live loop is single-threaded, so every site that blocks it for anything
    approaching a network timeout has to call this or the exchange-side deadline
    lapses and cancels every resting order on the wallet while the process is
    alive and healthy — the §20.3 unprotected window opening at exactly the
    moment the network is least able to close it.

    CHEAP when a refresh is not due: :meth:`tick` reaches the wire only when
    ``refresh_due()`` says so, so calling this once per loop iteration costs a
    clock read plus the unconditional expired-deadline detection — and that
    detection is half the point, since a lapse discovered mid-sweep is a lapse
    the caller's own row-trust logic needs to know about.

    Guarded, and deliberately never re-raising: a refresh miss must not abort
    the work it was protecting. The caller is typically mid-repair or mid-sweep,
    where dying is strictly worse than a stale switch the next tick retries.
    Mirrors ``protection._maybe_delay``'s long-standing treatment, which this
    replaced.
    """
    if kill_switch is None:
        return
    try:
        kill_switch.tick()
    except Exception:  # noqa: BLE001 — a refresh miss must not abort its caller
        logger.warning("kill-switch refresh during %s failed", what, exc_info=True)
