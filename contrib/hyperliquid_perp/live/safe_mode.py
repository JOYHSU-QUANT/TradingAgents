"""The §13 safe-mode state machine: enter, escalate, persist, release.

Safe mode is not a stopped process — the run keeps monitoring, protecting and
reconciling (§13.1); what stops is anything that ADDS risk (§13.2). This module
owns the state transitions and their two records:

- **current state** on ``scheduler_state`` (§16.6: ``safe_mode_type`` /
  ``safe_mode_reason`` / ``safe_mode_entered_at``) — a restart does NOT clear
  safe mode (§13.6 rule 1); :meth:`SafeModeManager.hydrate_gate` restores the
  gate flags from the persisted state before the first order could pass;
- **history** in ``safe_mode_events`` (§13.6) — every entry, escalation and
  release is one auditable row, releases naming who released (``released_by``).

Enforcement is the §4.1 :class:`~.order_gate.RealOrderGate`, not ad-hoc checks:
entering ANY safe mode drops ``gate.state_reconciled`` (its §4.1 line blocks
every new order until a reconciliation pass re-proves it — exactly §13.4's
release condition), and a MANUAL entry additionally raises
``gate.manual_safe_mode``, which only the §13.6 CLI release lowers. §13.1's
allowed actions (cancel bot orders, kill-switch refresh, reconciliation reads)
ride :meth:`~.order_gate.RealOrderGate.check_exchange_action`, which reads
neither flag — the gate widths already encode the allow/block split.

Release is deliberately TWO different doors:

- recoverable → :meth:`SafeModeManager.try_auto_recover`, which demands the
  §13.4 condition set (a clean reconciliation pass, WS restored, kill switch
  healthy) proven by the CALLER this same tick — the manager never asserts
  recovery it did not see evidence for;
- manual → :meth:`SafeModeManager.release_manual`, the CLI's path (§13.6
  rule 2). Even then trading does not resume: the gate's ``state_reconciled``
  stays down until the NEXT full reconciliation passes (§13.6 rule 3).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from ..domains.perp.enum_guard import check_enum
from ..paper.clock import Clock, WallClock
from ..persistence import repository as repo
from ..persistence.db import Database
from .order_gate import RealOrderGate

__all__ = [
    "REASON_CONSECUTIVE_LOSS",
    "REASON_DAILY_LOSS",
    "REASON_INVALID_LOCAL_FILL",
    "REASON_KILL_SWITCH_REFRESH_FAILED",
    "REASON_NON_BOT_OWNED_ORDER",
    "REASON_RECONCILIATION_MISMATCH",
    "REASON_REPEATED_MISMATCH",
    "REASON_SL_MISSING",
    "REASON_STALE_ORDER_SWEEP_FAILED",
    "REASON_UNKNOWN_POSITION",
    "REASON_WS_DISCONNECT",
    "SafeModeManager",
    "SafeModeState",
]

logger = logging.getLogger(__name__)

# The PR 4 entry-reason vocabulary (§13.4/§13.5 sources wired in this PR).
# Free strings in storage — PR 5 adds daily_loss / consecutive_loss without a
# schema change — but named here so the wiring and the tests share one spelling.
REASON_WS_DISCONNECT = "ws_disconnect"  # §13.4: recoverable
REASON_KILL_SWITCH_REFRESH_FAILED = "kill_switch_refresh_failed"  # §13.4: recoverable
REASON_RECONCILIATION_MISMATCH = "reconciliation_mismatch"  # §12.3: recoverable
REASON_REPEATED_MISMATCH = "repeated_reconciliation_mismatch"  # §13.5: manual
REASON_NON_BOT_OWNED_ORDER = "non_bot_owned_order"  # §13.5: manual
REASON_INVALID_LOCAL_FILL = "invalid_local_fill"  # §12.3: manual (money state unknown)
REASON_UNKNOWN_POSITION = "unknown_exchange_position"  # §13.5: manual
REASON_SL_MISSING = "position_sl_missing"  # §12.3: recoverable (repair lands in PR 5)
# §19.3: recoverable. Covers every way the startup stale-order sweep can fail —
# a cancel that would not land AND a positions read it could not prove — so a
# reason-keyed query never misclassifies a read failure as a cancel failure.
REASON_STALE_ORDER_SWEEP_FAILED = "stale_order_sweep_failed"
# §10.3 daily loss cap: recoverable (position + SL/TP kept, new entry/rebalance
# stopped; auto-releases at the next UTC 00:00 baseline roll, still subject to
# §13.4). §10.4 consecutive loss cap: manual (3 losing settlements suggest a
# strategy problem — a human must confirm before trading resumes, §13.6).
REASON_DAILY_LOSS = "daily_loss"  # §10.3: recoverable
REASON_CONSECUTIVE_LOSS = "consecutive_loss"  # §10.4: manual

# §13.5 "repeated reconciliation mismatch": this many CONSECUTIVE unclean
# passes escalate a recoverable safe mode to manual. In-memory (a restart
# starts the count afresh — the restart runs its own startup reconciliation,
# which re-enters safe mode immediately if the mismatch persists).
_REPEATED_MISMATCH_THRESHOLD = 3


@dataclass(frozen=True)
class SafeModeState:
    """The persisted §16.6 current state, decoded."""

    safe_mode_type: str  # "recoverable" | "manual"
    reason: str
    entered_at: str  # ISO-8601 UTC, as stored

    def __post_init__(self) -> None:
        # Loud at construction (mirrors ReconciliationCase): both release
        # doors branch on ``is_manual``, so a corrupted stored safe_mode_type
        # (a direct SQL fix, a bad migration) would otherwise silently read as
        # "not manual" — try_auto_recover would auto-release a safe mode a
        # human was supposed to lift (§13.5) while release_manual refuses the
        # legitimate CLI release. The writers validate via check_enum; the
        # read side must be just as strict or the write-side guard proves
        # nothing about what the doors actually see.
        check_enum(self.safe_mode_type, repo.SAFE_MODE_TYPES, name="safe_mode_type")

    @property
    def is_manual(self) -> bool:
        return self.safe_mode_type == "manual"


class SafeModeManager:
    """One run's safe-mode transitions, persisted through ``scheduler_state``.

    Owns its transactions (like the kill-switch manager): every transition is
    one atomic write of the current-state trio plus its history event, so a
    crash can never record an entry the current state does not show (or vice
    versa). Severity is one-way — ``manual`` outranks ``recoverable``, so an
    entry can escalate but a recoverable trigger firing during a manual safe
    mode never downgrades it.
    """

    def __init__(
        self,
        *,
        db: Database,
        run_id: str,
        gate: RealOrderGate | None,
        clock: Clock | None = None,
    ) -> None:
        # ``gate=None`` is the OFFLINE wiring (the §13.6 CLI subcommand): no
        # live process, so there is no gate to flip — the persisted state is
        # the whole effect, and the next live startup hydrates from it. Every
        # in-process wiring must pass the real gate; None there would record
        # safe modes that never block anything.
        self._db = db
        self._run_id = run_id
        self._gate = gate
        self._clock = clock or WallClock()
        self._consecutive_mismatches = 0

    # -- reads ---------------------------------------------------------------

    def current(self) -> SafeModeState | None:
        """The persisted current state, or ``None`` when not in safe mode."""
        row = repo.get_scheduler_state(self._db.conn, self._run_id)
        if row is None or row["safe_mode_type"] is None:
            return None
        return SafeModeState(
            safe_mode_type=row["safe_mode_type"],
            reason=row["safe_mode_reason"],
            entered_at=row["safe_mode_entered_at"],
        )

    @property
    def active(self) -> bool:
        return self.current() is not None

    # -- transitions ----------------------------------------------------------

    def hydrate_gate(self) -> SafeModeState | None:
        """Restore the gate flags from the persisted state (§13.6 rule 1).

        Called once at startup, before anything could pass the gate: a restart
        must not forget a safe mode. ``state_reconciled`` needs no restore —
        it is fail-closed (starts ``False``) and only a fresh reconciliation
        pass raises it; ``manual_safe_mode`` is the flag with memory.
        """
        state = self.current()
        if self._gate is not None:
            self._gate.manual_safe_mode = state is not None and state.is_manual
        if state is not None:
            logger.warning(
                "safe mode restored from store: %s (%s, entered %s) — trading stays "
                "blocked until it is released and reconciliation passes",
                state.safe_mode_type,
                state.reason,
                state.entered_at,
            )
        return state

    def enter(self, safe_mode_type: str, reason: str, *, detail: str | None = None) -> bool:
        """Enter (or escalate to) safe mode; returns True when state changed.

        Idempotent under repetition: re-entering the level already held (a
        stale WS re-reported every tick, a mismatch re-observed every pass)
        neither spams the history nor resets ``entered_at`` — the §13.4
        "daily loss until next UTC midnight" style conditions key off the
        ORIGINAL entry instant. A recoverable trigger during a manual safe
        mode is absorbed (manual outranks it); a manual trigger during a
        recoverable one escalates, recorded as ``safe_mode_escalated``; a
        trigger with a NEW reason at the SAME severity already latched
        (manual-during-manual or recoverable-during-recoverable) keeps the
        state untouched but leaves a ``safe_mode_reason_added`` history row
        (once per reason per episode) so the §13.6 triage surface shows every
        independent fact.
        """
        check_enum(safe_mode_type, repo.SAFE_MODE_TYPES, name="safe_mode_type")
        current = self.current()
        now = self._clock.now()
        entered_at: datetime = now
        if current is not None:
            if current.is_manual or safe_mode_type == "recoverable":
                # Already at (or above) the requested severity — the state does
                # not change. But a DISTINCT reason surfacing at the SAME
                # severity already latched is a new independent fact the §13.6
                # triage surface must show (decided 2026-07-17, extended from
                # manual to recoverable the same day): record it as its own
                # history row, idempotent per episode keyed on the reason — the
                # same fact re-observed every pass gets one row, like the first
                # reason does. The current-state trio keeps the FIRST reason. A
                # LOWER severity absorbed under a higher one (a recoverable
                # trigger during a manual episode) is NOT a same-level fact and
                # stays silently absorbed — the manual episode is what triage
                # acts on.
                if (
                    safe_mode_type == current.safe_mode_type
                    and reason != current.reason
                    and not repo.has_safe_mode_reason_event(
                        self._db.conn,
                        self._run_id,
                        reason=reason,
                        since_iso=current.entered_at,
                    )
                ):
                    logger.error(
                        "additional %s safe-mode reason observed: %s%s (episode "
                        "entered %s for: %s)",
                        safe_mode_type,
                        reason,
                        f" ({detail})" if detail else "",
                        current.entered_at,
                        current.reason,
                    )
                    with self._db.transaction() as conn:
                        repo.insert_safe_mode_event(
                            conn,
                            run_id=self._run_id,
                            event_type="safe_mode_reason_added",
                            safe_mode_type=safe_mode_type,
                            reason=reason,
                            detail=detail,
                            timestamp=now,
                        )
                return False
            event_type = "safe_mode_escalated"
            # An escalation is a severity change on the SAME unhealthy episode:
            # the entry instant is preserved, so time-gated release conditions
            # (and "how long has this run been unhealthy") key off the original
            # entry, exactly as the docstring promises for re-entry.
            try:
                entered_at = datetime.fromisoformat(current.entered_at)
            except (TypeError, ValueError):
                # Our own writer always stores ISO-8601; an unparseable value is
                # store corruption — keep the machine functioning (the write
                # below re-normalizes) rather than crash the safety path.
                logger.warning(
                    "stored safe_mode_entered_at %r is unparseable; re-anchoring at now",
                    current.entered_at,
                )
        else:
            event_type = "safe_mode_entered"
        # Fail-safe ordering: the gate closes BEFORE the durable write — if the
        # write dies on a busy DB, the process is still blocked this tick and
        # the next trigger retries the record; the reverse order could leave a
        # recorded safe mode whose gate still admits orders until hydrate.
        if self._gate is not None:
            self._gate.state_reconciled = False
            if safe_mode_type == "manual":
                self._gate.manual_safe_mode = True
        logger.error(
            "entering %s safe mode: %s%s — new risk-adding orders are blocked (§13.2)",
            safe_mode_type,
            reason,
            f" ({detail})" if detail else "",
        )
        with self._db.transaction() as conn:
            repo.upsert_scheduler_state(
                conn,
                self._run_id,
                safe_mode_type=safe_mode_type,
                safe_mode_reason=reason,
                safe_mode_entered_at=entered_at,
                updated_at=now,
            )
            repo.insert_safe_mode_event(
                conn,
                run_id=self._run_id,
                event_type=event_type,
                safe_mode_type=safe_mode_type,
                reason=reason,
                detail=detail,
                timestamp=now,
            )
        return True

    def set_state_reconciled(self, proven: bool) -> None:
        """Flip the gate's §4.1 "state is reconciled" line from a pass verdict.

        The one writer of that flag outside :meth:`try_auto_recover`, kept on
        the manager so the gate wiring has a single owner: the reconciler
        reports its verdict here rather than reaching into the gate itself.
        A ``True`` never overrides safe mode — the manual flag (and, for
        recoverable, the §13.4 door) still decide whether trading resumes.
        The recoverable half of that promise is enforced HERE, not left to
        caller discipline: while a recoverable safe mode is latched this flag
        is the gate's ONLY blocking line, so a ``True`` is refused (only
        :meth:`try_auto_recover`'s release — the §13.4 door — raises it).
        """
        if self._gate is None:
            return
        if proven:
            current = self.current()
            if current is not None and not current.is_manual:
                logger.info(
                    "clean pass acknowledged, but recoverable safe mode (%s) is "
                    "still latched — the gate stays shut until every §13.4 "
                    "release condition is proven",
                    current.reason,
                )
                return
        self._gate.state_reconciled = proven

    def note_reconciliation_outcome(self, clean: bool) -> None:
        """Track §13.5's "repeated reconciliation mismatch" escalation.

        Call once per reconciliation pass. ``clean`` resets the streak; the
        third consecutive unclean pass escalates to manual safe mode — a
        mismatch that three passes could not heal is not going to heal itself,
        and §13.5 says a human decides from there.
        """
        if clean:
            self._consecutive_mismatches = 0
            return
        self._consecutive_mismatches += 1
        if self._consecutive_mismatches >= _REPEATED_MISMATCH_THRESHOLD:
            self.enter(
                "manual",
                REASON_REPEATED_MISMATCH,
                detail=f"{self._consecutive_mismatches} consecutive unclean reconciliation passes",
            )

    def try_auto_recover(
        self,
        *,
        reconciliation_clean: bool,
        ws_restored: bool,
        kill_switch_active: bool,
        fully_wired: bool,
    ) -> bool:
        """§13.4: release a RECOVERABLE safe mode when every condition holds.

        The caller proves the conditions THIS tick — a clean reconciliation
        pass it just ran (orders/fills/position/account reconciled, backfill
        complete, position protected, no unresolved mismatch), the WS stream
        restored, the kill switch healthy (its own sticky latch released
        through ``release_safe_mode()``, which earns it with a proving
        refresh), and the reconciler fully wired (``fully_wired`` = the pass
        carried no skipped legs; see ``ReconciliationReport.legs_skipped``).
        Every attestation is REQUIRED, no defaults, on this signature — so no
        call site, present or future, can auto-release without evidence.
        Manual safe mode never auto-recovers (§13.5); returns True when the
        release happened.

        The PR 5 daily-loss entry adds its "past next UTC midnight" condition
        when it lands — its trigger does not exist yet, so no reason gets a
        time gate here.
        """
        current = self.current()
        if current is None or current.is_manual:
            return False
        if not (reconciliation_clean and ws_restored and kill_switch_active and fully_wired):
            return False
        if current.reason == REASON_DAILY_LOSS and not self._past_daily_loss_window(current):
            # §10.3 rule 3: the daily-loss safe mode auto-releases only past the
            # NEXT UTC 00:00, when the loss guard's baseline rolls to the new
            # day's opening equity and the drawdown resets. Before then the
            # drawdown still holds, so a clean reconciliation pass alone must not
            # lift it. (A recovery that lands after midnight releases here; if
            # the NEW day is itself a loss day, the loss guard re-enters on its
            # own fresh baseline.)
            return False
        self._release(
            released_by="auto_recovery",
            detail=f"§13.4 conditions met (entered for: {current.reason})",
        )
        # Recovery IS the reconciliation proof — the caller just ran the clean
        # pass, so the gate's §4.1 "state is reconciled" line re-opens now.
        if self._gate is not None:
            self._gate.state_reconciled = True
        logger.info("recoverable safe mode released by auto-recovery (§13.4)")
        return True

    def _past_daily_loss_window(self, state: SafeModeState) -> bool:
        """§10.3 rule 3: has the UTC day advanced past a daily-loss entry?

        The daily-loss baseline rolls at the next UTC 00:00, so a daily-loss
        safe mode entered on day D may only auto-release once ``now`` is on a
        LATER UTC day than the entry. An unparseable stored ``entered_at`` is
        store corruption on the safety path — withhold the release (fail safe)
        rather than lift protection blind.
        """
        try:
            entered = datetime.fromisoformat(state.entered_at)
        except (TypeError, ValueError):
            logger.warning(
                "daily-loss safe_mode_entered_at %r is unparseable — withholding "
                "auto-release (§10.3)",
                state.entered_at,
            )
            return False
        now = self._clock.now()
        return now.astimezone(timezone.utc).date() > entered.astimezone(timezone.utc).date()

    def release_manual(self, *, released_by: str, reason: str) -> None:
        """§13.6 rule 2: the CLI's manual release, with its audit trail.

        ``released_by`` names the operator (or invoking identity), ``reason``
        their confirmation text; both land on the ``safe_mode_released`` row.
        Releasing does NOT resume trading (§13.6 rule 3): the gate's
        ``state_reconciled`` stays down until the next full reconciliation
        pass proves the books — only the manual latch lifts here. Refuses a
        recoverable safe mode (that is auto-recovery's door — releasing it by
        hand would skip the §13.4 evidence) and a run not in safe mode.
        """
        if not released_by or not released_by.strip():
            raise ValueError("released_by must be a non-empty operator identity")
        if not reason or not reason.strip():
            raise ValueError("a manual release requires a --reason (§13.6 rule 2)")
        current = self.current()
        if current is None:
            raise ValueError(f"run {self._run_id!r} is not in safe mode")
        if not current.is_manual:
            raise ValueError(
                "run is in RECOVERABLE safe mode — it releases itself via §13.4 "
                "auto-recovery once its conditions heal; the CLI releases only "
                "manual safe mode"
            )
        self._release(released_by=released_by, detail=reason)
        if self._gate is not None:
            self._gate.manual_safe_mode = False
            # state_reconciled deliberately NOT raised: §13.6 rule 3.
            self._gate.state_reconciled = False
        logger.warning(
            "manual safe mode released by %s — trading resumes only after the "
            "next reconciliation pass succeeds (§13.6 rule 3)",
            released_by,
        )

    def _release(self, *, released_by: str, detail: str | None) -> None:
        now = self._clock.now()
        with self._db.transaction() as conn:
            repo.upsert_scheduler_state(
                conn,
                self._run_id,
                safe_mode_type=None,
                safe_mode_reason=None,
                safe_mode_entered_at=None,
                updated_at=now,
            )
            repo.insert_safe_mode_event(
                conn,
                run_id=self._run_id,
                event_type="safe_mode_released",
                released_by=released_by,
                detail=detail,
                timestamp=now,
            )
