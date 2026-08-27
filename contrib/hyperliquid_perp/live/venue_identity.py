"""§13.5 venue-identity fault: ONE bounded fact for the fail-soft orderStatus consumers.

``live.orders.parse_order_status`` names its four consumers — a §8.3 resend
decision, a protection "is the stop still resting?" check, a kill-switch
disarm cross-check, a reconciliation settle — and every one of them asks the
same question of the same venue: *does it recognise our cloids?* A venue that
answers with another order's identity (or a shape this build cannot read)
does NOT heal on its own, so each FAIL-SOFT consumer's verdict repeats
forever: the no-op guard re-repairs a stop that is already resting, the
recovery probe burns a repair ladder into a §17.2 emergency close of a healthy
position, reconciliation re-records the same unresolved case every pass, and
every shutdown leaves the wallet-wide scheduleCancel armed over the SL/TP.
Those three consumers — protection, the reconciler, the kill switch — share
this module. The §8.3 resend pre-check (``orders._try_recover_existing``) is
deliberately NOT one of them: it has no fail-soft lane, its
``MalformedResponseError`` escapes ``submit()`` under its own type and aborts
the send, so nothing there repeats unbounded (issue #80's "not a defect"
verdict). The live-smoke suite's own orderStatus reads are the testnet gate's
assertions, not a run's verdicts, and stay direct as well.

PR #79 bounded that treadmill for protection alone, with a counter that lived
inside ``ProtectionManager`` — and its own comment argued the counter should
be shared ("the fault being counted is a property of the venue, not of which
question we happened to ask"). This module is that shared counter:

- :class:`VenueIdentityMonitor` wraps the ``query_order_by_cloid`` round-trip
  AND the parse, so it is the one frame that holds the whole response when
  the parse refuses it. It counts consecutive unreadable answers across every
  consumer, resets on any readable one, and is NEUTRAL on transport failures
  (a timeout is evidence of nothing — see :meth:`VenueIdentityMonitor.probe`).
- When it has a ``payload_dir`` it persists the refused payload (which the
  parser attaches to the error, one frame down) — the same evidence the
  order-ack and fill paths already keep when THEY refuse a payload (issue
  #80's forensic asymmetry: the operator triaging the MORE severe fault used
  to get less).
- The latch is derived from the streak and read by whoever holds the
  :class:`SafeModeManager` after a probe site ran: the engine's tick (after
  the §17 sync), ``LiveReconciler.reconcile_and_apply`` (after a pass) and
  the CLI's §18.2 shutdown ``finally`` (after the disarm cross-check) — via
  :func:`escalate_identity_fault`. The monitor itself never escalates: the
  reconciler and the kill switch have no safe-mode machine in hand, and
  giving the monitor one would put a durable state transition inside a
  shutdown path whose write ordering the CLI owns.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from ..exchanges.hyperliquid.errors import MalformedResponseError
from ..paper.clock import Clock, WallClock
from ..persistence import repository as repo
from ..persistence.db import Database
from .orders import parse_order_status
from .payloads import write_raw_payload
from .safe_mode import REASON_IDENTITY_FAULT, SafeModeManager

__all__ = [
    "UNREADABLE_PROBE_LATCH_THRESHOLD",
    "VenueIdentityMonitor",
    "describe_order_status_failure",
    "escalate_identity_fault",
]

logger = logging.getLogger(__name__)

# §17 / §13.5: how many CONSECUTIVE unreadable orderStatus answers ABOUT ONE
# CLOID latch the venue-identity fault. Counted per cloid (decided 2026-08-27,
# issue #80 round-1 review): the realistic fault misroutes one identity and
# answers the rest coherently, so a process-wide streak would flap 1→0 forever
# and the bound would never be reached for the one order that needs it.
# Every probe site fails CLOSED on an unusable answer,
# and that verdict's cost model assumes the fault heals next tick — "a false
# 'gone' costs one redundant re-place". A venue that answers with another
# order's identity does NOT heal: it misroutes every time, so the no-op guard
# re-repairs a stop that is already resting forever, and the recovery probe
# can burn a whole repair ladder into a §17.2 emergency close of a position
# that was healthy and protected all along.
#
# FIVE, chosen against the protection NO-OP GUARDS' worth of probes in one
# sync: at most three (the SL guard, the SL covering check on a gate-blocked
# repair, the TP guard — see protection._row_still_rests), of which at most
# TWO ask about the same cloid (the two SL checks), so the guards alone can
# never latch, whatever the venue does to them in a single sync. That is
# the case worth protecting: a guard reading "not resting" costs one redundant
# re-place of an order we want resting anyway, and the fail-closed verdict
# already handles it correctly.
#
# Under per-cloid counting no single sync latches at all — the ladder's
# recovery probes ask about the replacement order's cloid, not the resting
# row's — but a venue that keeps misrouting does not need many: the worst
# cloid's streak carries across syncs and the latch lands on the second
# (measured; pinned in test_protection's
# test_a_persistently_misrouting_venue_latches_within_a_few_syncs, alongside
# the guards-alone pin). Slower by one sync than the process-wide counter it
# replaces, in exchange for a bound that a coherently-answered sibling order
# can no longer reset away.
#
# The same number bounds the reconciler's per-order probes and the kill
# switch's shutdown cross-check (decided 2026-08-26, issue #80): a shutdown
# alone rarely asks about five orders, and that is fine — the streak it
# inherits from the loop's probes is what makes a persistent misroute latch
# BEFORE shutdown, and a fault that begins during shutdown still blocks the
# disarm (fail-safe, unchanged) with its cause named and its payload kept.
#
# Five is tiny against the unbounded treadmill it replaces — at the 30s
# max_tick_gap the latch lands within a minute or two of onset. (The sibling
# thresholds both use 3 — safe_mode._REPEATED_MISMATCH_THRESHOLD and the funding
# source's log escalation — but neither counts events that can repeat WITHIN one
# observation, which is why this one is not simply 3 as well.)
UNREADABLE_PROBE_LATCH_THRESHOLD = 5


def describe_order_status_failure(exc: BaseException) -> str:
    """The clause a consumer's audit text uses for a failed probe.

    The two failure families the probe sites catch under one ``except`` lead
    to opposite triage: a transport failure ("failed") heals and points at the
    network; an unreadable ANSWER ("answered unusably") does not heal and
    points at the venue or the wiring (see the RUNBOOK's
    ``venue_identity_fault`` entry). Before this helper the reconciler's case
    rows and the kill switch's unaccounted list said "failed" for both. The
    whole clause, not a fragment, so the wording lives in one place.
    """
    if isinstance(exc, MalformedResponseError):
        return f"orderStatus answered unusably (venue identity fault): {exc}"
    return f"orderStatus failed: {exc}"


class VenueIdentityMonitor:
    """The shared "can the venue identify our orders?" fact, with its bound.

    One instance per live process (built by the CLI beside the signed client
    and handed to protection, the reconciler and the kill switch). A consumer
    constructed without one builds a private monitor — that keeps every
    consumer bounded on its own, but only the SHARED instance lets a fault
    that alternates between consumers be seen as the one persistent fault it
    is, and only the CLI's instance carries the ``payload_dir``.
    """

    def __init__(
        self,
        *,
        query_order_by_cloid: Callable[[str], Any],
        db: Database,
        run_id: str,
        symbol: str,
        payload_dir: Path | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._query = query_order_by_cloid
        self._db = db
        self._run_id = run_id
        self._symbol = symbol
        self._payload_dir = payload_dir
        self._clock = clock or WallClock()
        # CONSECUTIVE unreadable orderStatus answers PER CLOID, across every
        # probe site (issue #80 round-1 review): the realistic fault misroutes
        # ONE of our cloids while answering the others coherently, and a single
        # process-wide streak would be reset by every coherent answer — the one
        # misrouted order's repair treadmill would then run unbounded forever,
        # which is the exact fault this bound exists for. A cloid's streak is
        # reset by a READABLE answer about THAT cloid (see probe), so
        # "consecutive" means consecutive about the order in question. The
        # latch is DERIVED from these numbers rather than stored beside them —
        # two fields kept in lockstep by hand is a desync waiting for the next
        # probe site to be added.
        self._streaks: dict[str, int] = {}
        # cloid -> the payload file already holding that cloid's refused answer
        # (see _note_unreadable for why evidence is bounded per cloid).
        self._evidence: dict[str, str] = {}
        # The site whose probe crossed the threshold (None until first latch;
        # kept at the most recent crossing) — read by escalate_identity_fault
        # so the safe-mode row can name where the fault was OBSERVED, not just
        # which holder happened to escalate it.
        self._latched_site: str | None = None

    @property
    def unreadable_streak(self) -> int:
        """The worst cloid's consecutive-unreadable count (0 when all clear)."""
        return max(self._streaks.values(), default=0)

    @property
    def latched_site(self) -> str | None:
        """The probe site whose answer crossed the threshold, if any yet."""
        return self._latched_site

    @property
    def latched(self) -> bool:
        """Whether ANY cloid's consecutive unreadable answers crossed the latch.

        The escalation signal (§13.5). A LATCH, not an edge: it stays up until
        a probe reads an answer about that cloid again, so an escalation whose
        durable write failed is retried by the next holder of the safe-mode
        machine rather than lost. Re-entering the same safe mode is idempotent,
        so a raised latch cannot spam the §13.6 history either.
        """
        return self.unreadable_streak >= UNREADABLE_PROBE_LATCH_THRESHOLD

    def probe(self, cloid_hex: str, *, site: str) -> tuple[str, str] | None:
        """``query_order_by_cloid`` + ``parse_order_status``, counted.

        Returns what the parser returns (``(exchange_oid, status)`` or ``None``
        for the documented ``unknownOid`` marker) and raises what it raises —
        the caller's verdict logic is untouched; this only observes. ``site``
        names the asking consumer for the log line and the latch row, so triage
        does not have to guess which question got the answer.

        Three outcomes, three different effects on the streak:

        - the READ raised (a timeout, a throttle — anything from the wire):
          NEUTRAL. It neither counts nor resets, and propagates unchanged.
          Counting it would let an ordinary outage latch a fault it is no
          evidence for; resetting on it would let one blip inside a persistent
          misroute restart the streak forever, so the bound would never be
          reached.
        - the venue ANSWERED and the answer could not be read as this cloid's
          (a misrouted identity, or a shape this build cannot parse):
          COUNTED, and the refused payload — which the parser attached to the
          error — is persisted. Both are counted together because both share
          the property the fail-closed verdicts do not model: they do not heal
          on their own.
        - the answer was READ, whatever it said — the ``unknownOid`` marker
          and a status word this build cannot classify included, since both
          are the venue answering coherently about the cloid we asked for,
          which is exactly what the latched fault says is not happening:
          RESET. Lowering the latch does not release the safe mode it caused
          (only §13.6 does, by design); it lets a LATER recurrence latch again
          and leave its own audit row.
        """
        payload = self._query(cloid_hex)
        try:
            parsed = parse_order_status(payload, expected_cloid_hex=cloid_hex)
        except MalformedResponseError as exc:
            self._note_unreadable(site=site, cloid_hex=cloid_hex, exc=exc, payload=payload)
            raise
        self._streaks.pop(cloid_hex, None)
        return parsed

    def _note_unreadable(
        self, *, site: str, cloid_hex: str, exc: MalformedResponseError, payload: Any
    ) -> None:
        """Count one unusable answer, keep its payload, latch at the threshold.

        The audit row is written ONCE per episode, at the crossing: a row per
        occurrence would bill an unbounded fault an unbounded number of rows,
        which is the very failure mode being fixed. The payload evidence is
        bounded too — ONE file per cloid for the life of the process, the
        first refused answer about it. Not one per occurrence: the §17 sync
        keeps probing every tick under the manual safe mode the latch raises
        (protective roles are gate-exempt), so a persistent fault would fill
        the payload_dir at hundreds of files an hour. Not one per episode
        either: the same cloid can flap between readable and unreadable for
        the life of a run, each flap ending one episode and starting the
        next, so a per-episode budget would re-arm on every flap. The
        distinct cloids a run asks about are bounded by its book; the
        first refusal is the evidence (a venue that misroutes a cloid
        misroutes it the same way next time).
        """
        now = self._clock.now()
        if self._payload_dir is not None and cloid_hex not in self._evidence:
            # write_raw_payload never raises (its rule 1) — this runs inside
            # the failure path of a probe whose caller promised not to crash.
            # A failed write leaves the cloid unrecorded, so the next refusal
            # tries again rather than silently giving up on the evidence.
            written = write_raw_payload(
                payload_dir=self._payload_dir,
                kind="orderStatus",
                key=cloid_hex,
                payload=payload,
                now=now,
            )
            if written is not None:
                self._evidence[cloid_hex] = written
        raw_path = self._evidence.get(cloid_hex)
        streak = self._streaks.get(cloid_hex, 0) + 1
        self._streaks[cloid_hex] = streak
        # EQUALITY, which is what makes the row once-per-episode (per cloid):
        # below the threshold there is nothing to report, above it the episode
        # is already reported. Sound only because a streak moves by exactly one.
        if streak != UNREADABLE_PROBE_LATCH_THRESHOLD:
            return
        self._latched_site = site
        logger.error(
            "orderStatus answered unusably %d times in a row about cloid %s (latest: %s — %s). "
            "A venue that cannot identify this order does not heal on its own, so this "
            "is escalated instead of retried indefinitely",
            streak,
            cloid_hex,
            site,
            exc,
        )
        # BEST-EFFORT, and last: the latch is already up (it is derived from
        # the counter incremented above), so the escalation cannot be lost
        # here. Every probe site runs this from inside an ``except`` handler
        # whose contract is that an unresolvable read must NOT crash its
        # caller — an unguarded transaction would break that promise on a
        # busy DB. Same ordering rule the §13.5 emergency-close escalation
        # follows: set the state first, record it after, and never let the
        # recording swallow the state.
        try:
            self._record_latch(site=site, cloid_hex=cloid_hex, exc=exc, raw_path=raw_path, now=now)
        except Exception:
            logger.exception(
                "could not record the venue-identity latch (%s, cloid %s); the latch "
                "itself stands and the next safe-mode holder still escalates",
                site,
                cloid_hex,
            )

    def _record_latch(
        self,
        *,
        site: str,
        cloid_hex: str,
        exc: MalformedResponseError,
        raw_path: str | None,
        now: datetime,
    ) -> None:
        # ``now`` is the instant of the crossing probe; ``raw_path`` is this
        # cloid's evidence file, which may predate it (see _note_unreadable).
        # The row must say when the file is MISSING despite a wired
        # payload_dir (disk full, permissions — likeliest during an incident):
        # a silently absent clause reads as "capture working, nothing kept",
        # and the log line that knew better may have rotated away by triage
        # time (2026-08-27 round-1 review).
        if raw_path:
            evidence = f"; payload {raw_path}"
        elif self._payload_dir is not None:
            evidence = "; payload capture FAILED — see the run log"
        else:
            evidence = ""  # no payload_dir wired: capture was never promised
        with self._db.transaction() as conn:
            repo.insert_protection_order_event(
                conn,
                run_id=self._run_id,
                event_type="identity_fault_latched",
                symbol=self._symbol,
                cloid_hex=cloid_hex,
                detail=(
                    f"{self._streaks.get(cloid_hex, 0)} consecutive unreadable orderStatus "
                    f"answers about this cloid (threshold {UNREADABLE_PROBE_LATCH_THRESHOLD}); "
                    f"latest from the {site}: {exc}{evidence}"
                ),
                timestamp=now,
            )


def escalate_identity_fault(
    monitor: VenueIdentityMonitor, safe_mode: SafeModeManager, *, site: str
) -> bool:
    """Enter manual safe mode if the shared latch is up; True when it was.

    Called by each holder of the safe-mode machine AFTER the probe sites it
    drives have run, and LAST in that holder's own bookkeeping: ``enter``
    writes SQLite and can raise on a busy DB, and a raise here must not skip
    the work the holder owes (an emergency close, a mismatch count, a
    shutdown verdict). Nothing is lost by running last — the latch stays up
    until a probe reads an answer again, so the next holder retries.
    Unconditional on the latch rather than on its rising edge: ``enter`` is
    idempotent for a repeated (severity, reason), so re-entering writes no
    second history row — while a first call whose write DIED is simply
    retried by the next holder.

    MANUAL, for the reason ``REASON_IDENTITY_FAULT`` documents: the fault does
    not heal, so a recoverable latch would auto-release straight back into the
    treadmill. Safe to latch mid-run because the protective roles are exempt
    from the manual gate line (order_gate.py); and worth calling even at
    shutdown because every holder's ``enter`` persists the same durable state
    the next boot's ``hydrate_gate`` restores — the shutdown call is merely the
    last chance before the process exits. A restored manual safe mode does not
    skip startup recovery: the boot still arms the switch and reconciles under
    it, but the verdict cannot pass and no cycle starts until §13.6 releases.
    """
    if not monitor.latched:
        return False
    # Both names, because they can differ and each answers a different triage
    # question: ``site`` is the holder that acted (whose lane the escalation
    # ran in), ``latched_site`` is where the fault was observed — without it,
    # a latch collected by the kill-switch cross-check would be filed under
    # whichever holder escalated first (2026-08-27 round-1 review).
    observed = monitor.latched_site or "unknown probe site"
    safe_mode.enter(
        "manual",
        REASON_IDENTITY_FAULT,
        detail=(
            "orderStatus answered unusably on consecutive §8.3 identity probes "
            f"(latched at the {observed}; escalated by {site})"
        ),
    )
    return True
