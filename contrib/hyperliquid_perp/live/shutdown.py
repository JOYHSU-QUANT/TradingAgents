"""The §18.2 shutdown of ``live --run-id``: what the exit reads, keeps and reports.

The CLI's ``finally`` runs, in order, :func:`read_exit_state`,
:func:`classify_shutdown`, its own WARNING line, then :func:`sweep_on_exit`;
after the summary lines it asks :func:`classify_exit` for the exit code. The
two ``classify_*`` functions are pure, so every combination of flags is
testable without a signed client. Every stderr line stays in ``cli/live.py``:
this module returns decisions and problem descriptions, never prints.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

from ..domains.perp.schema import PerpPosition
from ..exchanges.hyperliquid.mapper import map_account_snapshot
from .venue_identity import EscalationHolder, escalate_identity_fault

if TYPE_CHECKING:
    from .wiring import LiveSession

logger = logging.getLogger(__name__)

__all__ = [
    "ExitReason",
    "ExitState",
    "ShutdownFlags",
    "ShutdownVerdict",
    "classify_exit",
    "classify_shutdown",
    "read_exit_state",
    "sweep_on_exit",
]


@dataclass(frozen=True)
class ExitState:
    """What the process reads fresh on its way out, before the sweep.

    ``positions`` is None when the account could not be read (unknown ≠
    flat). A failed safe-mode read sets both ``safe_mode_active`` (fail toward
    keeping the SL/TP) and ``safe_mode_unknown``.
    """

    positions: Sequence[PerpPosition] | None
    safe_mode_active: bool
    safe_mode_unknown: bool


@dataclass(frozen=True)
class ShutdownFlags:
    """The facts the keep decision reads, gathered by the caller.

    ``loop_refused`` names a raise and adds no cause of its own: the caller
    sets it only on an ``EngineConfigError``, which arrives with
    ``loop_raised`` set or with the verdict still unpassed.
    """

    verdict_passed: bool
    loop_raised: bool
    loop_refused: bool
    protection_only: bool
    exit_state: ExitState


@dataclass(frozen=True)
class ShutdownVerdict:
    """What the sweep keeps, and why the exit is unclean.

    ``unclean_note`` is None on a clean exit. ``keep_protective`` is set when
    the exit is unclean and the position is live or unreadable.
    ``kept_on_unknown_safe_mode`` is set when that keep acted on a safe-mode
    read that failed.
    """

    unclean_note: str | None
    keep_protective: bool
    kept_on_unknown_safe_mode: bool


def classify_shutdown(flags: ShutdownFlags) -> ShutdownVerdict:
    """Decide whether the §18.2 sweep keeps the resting SL/TP (decided 2026-07-16, revised 2026-07-22).

    An exit is unclean when the startup verdict did not pass, safe mode is
    active (or unreadable) at exit, the loop raised, or it ran protection-only
    (issue #268: the next start meets the same refusal, so stripping the SL/TP
    on the operator's way to fixing the environment would leave the position
    naked). Over a live or unreadable position an unclean exit keeps the
    SL/TP: the repair machinery that could re-cover a stripped position is
    what an unclean verdict refuses to start. A clean exit, or a flat book,
    cancels them with every other bot order.
    """
    state = flags.exit_state
    unclean = (
        not flags.verdict_passed
        or state.safe_mode_active
        or flags.loop_raised
        or flags.protection_only
    )
    keep = unclean and (state.positions is None or bool(state.positions))
    return ShutdownVerdict(
        unclean_note=_unclean_note(flags) if unclean else None,
        keep_protective=keep,
        kept_on_unknown_safe_mode=state.safe_mode_unknown and keep,
    )


def _unclean_note(flags: ShutdownFlags) -> str:
    # First match wins. A FAILED safe-mode read is not "safe mode is active":
    # the `safe_mode:` line printed after the sweep may find none.
    if not flags.verdict_passed:
        return "the startup verdict did not pass"
    if flags.loop_refused:
        return "the engine could not be built (see the error above)"
    if flags.loop_raised:
        return "the live loop raised instead of returning"
    if flags.protection_only:
        return "the loop ran in protection-only mode"
    if flags.exit_state.safe_mode_unknown:
        return "the exit-time safe-mode state could NOT be read (unknown ≠ clean)"
    return "safe mode is active at exit"


class ExitReason(Enum):
    """Why ``live --run-id`` exits with the code it does; ``code`` is that code."""

    VERDICT_FAILED = ("verdict_failed", 4)
    SWEEP_UNCLEAN = ("sweep_unclean", 4)
    PROTECTION_ONLY_SETTLED = ("protection_only_settled", 1)
    PROTECTION_ONLY_STOPPED = ("protection_only_stopped", 4)
    LOOP_IN_SAFE_MODE = ("loop_in_safe_mode", 4)
    LOOP_KEPT_ON_UNKNOWN_SAFE_MODE = ("loop_kept_on_unknown_safe_mode", 4)
    LOOP_CLEAN = ("loop_clean", 0)
    ONE_SHOT_PASSED = ("one_shot_passed", 0)

    # The tag only keeps the values distinct: members sharing a code would
    # otherwise become aliases of one another.
    def __init__(self, _tag: str, code: int) -> None:
        self.code = code


def classify_exit(
    *,
    verdict_passed: bool,
    sweep_unclean: bool,
    loop: bool,
    protection_only_settled: bool | None,
    safe_mode_latched: bool,
    kept_on_unknown_safe_mode: bool,
) -> ExitReason:
    """Pick the exit after the sweep: 0 = all quiet, 4 = executed but unclean, 1 = settle-exit.

    First match wins: a failed verdict (4, not 1: a verdict is not a hard
    failure, decided 2026-07-16), then an unclean sweep (the wallet-wide
    trigger may still be armed, decided 2026-07-17), then on ``--loop`` a
    protection-only ending (``protection_only_settled`` is None when the loop
    was not protection-only). A settled one exits 1 like paper's settle-exit,
    so a supervisor restarts into the same named refusal; a stopped one exits
    4. Then safe mode latched at exit, then SL/TP kept behind a failed
    safe-mode read (a failed read over a flat book kept nothing, so the exit
    follows the safe-mode state).
    """
    if not verdict_passed:
        return ExitReason.VERDICT_FAILED
    if sweep_unclean:
        return ExitReason.SWEEP_UNCLEAN
    if not loop:
        return ExitReason.ONE_SHOT_PASSED
    if protection_only_settled is not None:
        return (
            ExitReason.PROTECTION_ONLY_SETTLED
            if protection_only_settled
            else ExitReason.PROTECTION_ONLY_STOPPED
        )
    if safe_mode_latched:
        return ExitReason.LOOP_IN_SAFE_MODE
    if kept_on_unknown_safe_mode:
        return ExitReason.LOOP_KEPT_ON_UNKNOWN_SAFE_MODE
    return ExitReason.LOOP_CLEAN


def read_exit_state(session: LiveSession, *, reconcile_first: bool) -> ExitState:
    """Read the position and safe mode fresh for the keep decision; never raises.

    Fresh, not the boot snapshot (decided 2026-07-17): minutes of arming,
    backfill and reconcile passes, or a whole ``--loop`` run, separate the two.
    With ``reconcile_first`` (a ``--loop`` run, which has placed orders since
    the boot verdict) one §12.2 reconciliation pass runs before both reads,
    because it can itself enter safe mode. Without it (the one-shot), the
    verdict pass that just ran is the §12.2 pass: nothing is placed after it.
    Each step logs its failure and the next one runs.
    """
    if reconcile_first:
        try:
            session.reconciler.reconcile_and_apply(
                "shutdown",
                safe_mode=session.safe_mode,
                ws_restored=True,
                kill_switch_active=not session.kill_switch.stop_new_orders,
            )
        except Exception:  # noqa: BLE001
            logger.exception("§12.2 pre-shutdown reconciliation failed (sweep proceeds)")
    positions: Sequence[PerpPosition] | None
    try:
        positions = map_account_snapshot(session.fetch_clearinghouse()).positions
    except Exception:  # noqa: BLE001 — the warning must not mask the verdict
        logger.exception("shutdown position re-read failed")
        positions = None
    try:
        active = session.safe_mode.active
    except Exception:  # noqa: BLE001
        logger.exception("shutdown safe-mode read failed")
        return ExitState(positions=positions, safe_mode_active=True, safe_mode_unknown=True)
    return ExitState(positions=positions, safe_mode_active=active, safe_mode_unknown=False)


def sweep_on_exit(session: LiveSession, *, keep_protective: bool) -> str | None:
    """Run the §18.2 shutdown sweep over an armed switch; the problem, or None if clean.

    Does nothing over an unarmed switch. Never raises: a raise inside the
    CLI's ``finally`` would discard the computed exit code. The sweep cancels
    the bot-owned open orders (the SL/TP too unless ``keep_protective``) and
    disarms only when clean. After it, this holder escalates a latched §13.5
    venue-identity fault into manual safe mode (issue #80), and a latch makes
    the exit unclean even over a clean sweep. This is the process's last
    write of that state, and the next boot hydrates it, so that boot's verdict
    cannot pass until a §13.6 release. Only over an armed switch: an unarmed
    one ran no disarm cross-check, and the engine escalated any latch from the
    loop on every tick.
    """
    kill_switch = session.kill_switch
    if not kill_switch.armed:
        return None
    problem: str | None = None
    try:
        kill_switch.shutdown(keep_protective=keep_protective)
    except Exception as exc:  # noqa: BLE001
        logger.exception("§18.2 shutdown sweep raised")
        problem = f"shutdown sweep raised: {exc}"
    else:
        if kill_switch.armed:
            # Bot orders may rest and the wallet-wide scheduleCancel WILL fire
            # at the deadline, taking out non-bot orders §25 says never to touch.
            problem = (
                "shutdown sweep left the kill switch armed — bot "
                "orders may still rest and the wallet-wide "
                "scheduleCancel will fire at the deadline"
            )
            logger.error("§18.2 %s", problem)
    try:
        if escalate_identity_fault(
            session.identity, session.safe_mode, holder=EscalationHolder.SHUTDOWN
        ):
            problem = (
                "venue identity fault latched — the exchange kept "
                "answering orderStatus about orders that are not "
                "ours; manual safe mode entered (see "
                "identity_fault_latched in protection_order_events "
                "and payloads/orderStatus-*.json)" + (f"; also: {problem}" if problem else "")
            )
    except Exception:  # noqa: BLE001
        logger.exception("could not persist the venue-identity escalation at shutdown")
    return problem
