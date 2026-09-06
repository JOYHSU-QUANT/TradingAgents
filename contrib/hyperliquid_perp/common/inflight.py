"""The in-flight decision — the ONE state machine both decision lanes drive (issue #181).

A decision cycle, once its attempt row exists, passes through the same
ordered states on the paper scheduler and on the live driver: the AI answers
(``parsed``); the §3.1 store SETTLES (``raw_stored`` — the response landed
durably, or was invalid and deliberately not stored); the engine gates it
once (``registration``, cached the moment it exists); or the cycle FAILS and
only its ``api_failed`` record is still owed (``pending_fail``). The rules
between those states are what make a crash or a locked store safe:

- gating is forbidden until the store settles — a crash in the store-retry
  window must fail closed on restart, never resume a cycle whose response was
  never durable (spec §3.1);
- once a registration is cached, only the PERSIST is retried, never the gate
  — a second ``start_plan`` would supersede the committed plan and register
  another;
- once a failure is armed, only its record is retried — never the AI, never
  the one-shot worker slot, never a gate.

Until this module the two lanes each hand-copied the fields and kept the
rules in sync by docstrings pointing at each other (``mirroring the live
driver's …``). The fields and the rules live HERE now, the rules as methods;
each lane subclasses to add what only it tracks (paper its persist-failure
streak, live its worker stall stamps) and keeps its own escalation policy —
paper lets the exception propagate as a daemon exit once the streak reaches
its bound, live raises typed errors into its tick guard's safe mode.

In ``common/`` because both lanes consume it and neither owns it (``paper``
does not import ``live``, and the layering guard keeps ``common`` at the
bottom of the import graph), so the decision and registration DTOs — which
live in ``domains`` and in each lane's engine — are type parameters rather
than imports. Beside the state sit the three other pieces the lanes used to
hand-copy: the per-try id scheme (``#in<n>`` / ``#out<n>``), the resume step
(re-parse the stored text, logging it in full first if the parse raises —
the row was its only durable copy), and the next-cycle anchor after a failed
cycle. The terminal write itself lives one layer up, in
``persistence.repository.record_api_failed``, which both lanes reach.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Generic, TypeVar

__all__ = [
    "NON_RETRYABLE_PREFIX",
    "InFlightDecision",
    "failed_cycle_next_at",
    "inflight_ids",
    "non_retryable_message",
    "parse_stored_response",
]

ParsedT = TypeVar("ParsedT")
RegistrationT = TypeVar("RegistrationT")

# The ``error_message`` prefix of an ``api_failed`` row written for a
# non-retryable error — a bug, or host trouble the provider does not classify
# — as opposed to a §6.2-classified API failure (which carries an
# ``error_type`` instead). The RUNBOOKs and the validators' tests read it as
# a contract (``startswith("non-retryable:")``), so it is spelled once.
NON_RETRYABLE_PREFIX = "non-retryable:"


def non_retryable_message(exc: BaseException) -> str:
    """The ``error_message`` for a cycle failed closed by ``exc`` (no §6.2 class).

    The repr, not ``str(exc)``: it is what tells a bug (``ValueError``,
    ``AssertionError``) from host trouble (``sqlite3.OperationalError`` past
    ``busy_timeout``, ``MemoryError``) in the RUNBOOK's triage row.
    """
    return f"{NON_RETRYABLE_PREFIX} {exc!r}"


def inflight_ids(attempt_id: str, try_no: int) -> tuple[str, str]:
    """``(input_id, output_id)`` of try ``try_no`` of ``attempt_id``.

    Per-try, so orders committed by a crashed try's ``start_plan`` reference
    the ``ai_outputs`` row the resumed gate eventually writes for THAT
    decision, never a later try's. Derived here for the fresh, the resumed and
    the failed lane alike, on both drivers, so none can drift.
    """
    if not attempt_id:
        raise ValueError("inflight_ids: attempt_id must be non-empty")
    if try_no < 1:
        raise ValueError(f"inflight_ids: try_no must be >= 1, got {try_no}")
    return f"{attempt_id}#in{try_no}", f"{attempt_id}#out{try_no}"


def failed_cycle_next_at(scheduled_at: datetime, now: datetime, interval: timedelta) -> datetime:
    """When the cycle after a FAILED one runs: ``scheduled_at + interval``, or later.

    A cycle that itself ran late (process outage across schedule points)
    anchors on the terminal instant instead: the literal ``scheduled_at +
    interval`` would land in the past and fire the next cycle immediately, so
    a long outage over a failing API would chain one full retry ladder per
    missed interval — spec §3's "missed intervals are never backfilled",
    extended to failed cycles (the completed path already anchors on
    completion). A retried terminal write applies the same rule at ITS
    instant.
    """
    next_at = scheduled_at + interval
    return next_at if next_at > now else now + interval


@dataclass
class InFlightDecision(Generic[ParsedT, RegistrationT]):
    """One attempt's post-row state, from its first try to its terminal record.

    Build through :meth:`for_try` (the ids are derived, never spelled). The
    lanes set ``parsed`` and ``raw_stored`` from their own collect and store
    steps (and :func:`parse_stored_response` sets both on resume); the
    transitions that carry a RULE — the gate, the registration, the failure
    verdict — go through the methods below, which refuse to run them twice or
    out of order.
    """

    attempt_id: str
    scheduled_at: datetime
    # The try this in-flight belongs to — the §3.1 ladder position on paper,
    # always 1 on live (no within-cycle ladder in v1).
    attempt_count: int
    input_id: str
    output_id: str
    # The AI's answer, once collected. Set before the in-flight is installed
    # on paper (the call is synchronous); set by the live driver when its
    # one-shot worker poll hands the result over — BEFORE the fallible store,
    # so "collected but not yet settled" is a real state there, and gating is
    # forbidden in it.
    parsed: ParsedT | None = None
    # Whether the §3.1 store is SETTLED: the response landed durably, or the
    # answer was invalid and deliberately not stored (nothing to resume, and
    # its preserved text is not guaranteed to re-parse to the same verdict).
    raw_stored: bool = False
    # The engine's start_plan outcome, cached the moment it exists (see
    # :meth:`cache_registration`).
    registration: RegistrationT | None = None
    # The cycle's failure verdict, armed BEFORE the api_failed record is
    # written (see :meth:`arm_fail`): ``(error_type, message)`` — a §6.2 class
    # or ``None`` for a non-retryable error.
    pending_fail: tuple[str | None, str] | None = None

    def __post_init__(self) -> None:
        # Mutable-state guard, same convention as the engines' plan state: a
        # malformed id here would key a decision_attempts/ai_outputs row with
        # no earlier failure point to diagnose it.
        for name in ("attempt_id", "input_id", "output_id"):
            if not getattr(self, name):
                raise ValueError(f"InFlightDecision.{name} must be non-empty")
        if self.attempt_count < 1:
            raise ValueError(
                f"InFlightDecision.attempt_count must be >= 1, got {self.attempt_count}"
            )
        if self.scheduled_at.tzinfo is None:
            raise ValueError("InFlightDecision.scheduled_at must be timezone-aware (UTC)")

    @classmethod
    def for_try(
        cls,
        attempt_id: str,
        scheduled_at: datetime,
        try_no: int,
        *,
        parsed: ParsedT | None = None,
    ):
        """The in-flight for try ``try_no``, its ids derived by :func:`inflight_ids`.

        Returns an instance of ``cls``, so each lane's subclass builds through
        the same door. The store is never settled here: the fresh lane lands
        it next, and the resumed lane settles it through
        :func:`parse_stored_response`.
        """
        input_id, output_id = inflight_ids(attempt_id, try_no)
        return cls(
            attempt_id=attempt_id,
            scheduled_at=scheduled_at,
            attempt_count=try_no,
            input_id=input_id,
            output_id=output_id,
            parsed=parsed,
        )

    # -- the ordering rules ---------------------------------------------------

    def require_gateable(self) -> ParsedT:
        """The decision the gate may run on: collected, store settled, no failure armed.

        Called by both lanes right before ``start_plan``. Each violation is a
        driver bug, not a market condition — an unstored decision gated here
        would, on a crash, be resumed from nothing or re-asked; a failed cycle
        gated here would trade on a verdict already recorded as no-decision.
        """
        if self.pending_fail is not None:
            raise AssertionError(
                f"in-flight {self.attempt_id}: the cycle has failed and only its api_failed "
                "record is owed — it must not be gated"
            )
        if self.parsed is None:
            raise AssertionError(
                f"in-flight {self.attempt_id}: no decision has been collected yet — nothing to gate"
            )
        if not self.raw_stored:
            raise AssertionError(
                f"in-flight {self.attempt_id}: the §3.1 store has not settled — gating an "
                "unstored decision would let a crash resume from nothing"
            )
        return self.parsed

    def cache_registration(self, registration: RegistrationT) -> None:
        """Cache ``start_plan``'s outcome the moment it exists — once.

        From here on the engine may have COMMITTED (and armed) a plan, so a
        persist failure must retry the PERSIST against THIS registration and
        never re-gate: a second ``start_plan`` would re-run the RiskGate and
        could register a second plan. A second cache is that re-gate.
        """
        if self.registration is not None:
            raise AssertionError(
                f"in-flight {self.attempt_id}: a registration is already cached — the gate "
                "must not run twice"
            )
        self.registration = registration

    def arm_fail(self, error_type: str | None, message: str) -> None:
        """Record the cycle's failure verdict BEFORE its ``api_failed`` write — once.

        If the write itself then fails (a double fault), the lane retries only
        the write on later polls; the verdict never changes, the AI is never
        re-asked, and a one-shot worker slot is never re-polled. Arming twice
        would mean a second verdict was reached for a cycle already decided.
        """
        if self.pending_fail is not None:
            raise AssertionError(
                f"in-flight {self.attempt_id}: a failure is already armed "
                f"({self.pending_fail[0]!r}) — a cycle fails once"
            )
        self.pending_fail = (error_type, message)


def parse_stored_response(
    inflight: InFlightDecision[ParsedT, RegistrationT],
    raw: str,
    parse: Callable[[str], ParsedT],
    *,
    log: logging.Logger,
) -> None:
    """Rebuild ``inflight.parsed`` from the §3.1 stored text; settle the store.

    The resume step both lanes run after a restart finds an attempt with a
    persisted ``pending_raw_response``: the parse is deterministic, so the
    result is exactly the decision the crashed process held, and the SOURCE
    is the store, so ``raw_stored`` is settled by definition. Never a second
    AI call (spec §3.1).

    ``parse`` is fail-closed by contract (malformed content returns an invalid
    decision), so a raise is a parser bug or a corrupted store — deterministic,
    and the caller fails the cycle closed rather than crash-looping every
    restart into the same parse. The terminal record then clears the text, so
    it is logged IN FULL here first, on the caller's ``log`` (the lane the
    operator greps), because ``ai_outputs`` never stores raw text and that
    row was its only durable copy.
    """
    try:
        parsed = parse(raw)
    except Exception:
        log.error(
            "decision attempt %s: stored response failed to parse and is being "
            "cleared; preserving it here for diagnosis: %r",
            inflight.attempt_id,
            raw,
        )
        raise
    inflight.parsed = parsed
    inflight.raw_stored = True
