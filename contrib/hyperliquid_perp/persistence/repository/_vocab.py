"""Enumerable storage vocabularies shared across the repository package's table modules."""

from __future__ import annotations

from ...common.constants import ERROR_TYPES
from ..cloid import LIVE_ORDER_ROLES

__all__ = [
    "ACCOUNTING_ADJUSTMENT_TYPES",
    "ERROR_TYPES",
    "EXCHANGE_KNOWN_ATTEMPT_STATUSES",
    "KILL_SWITCH_EVENT_TYPES",
    "LIVE_LIQUIDITY_ROLES",
    "LIVE_ORDER_STATUSES",
    "LIVE_PLAN_STATUSES",
    "LIVE_SMOKE_TEST_STATUSES",
    "MACHINE_DISPOSITIONS",
    "PROTECTION_ORDER_EVENT_TYPES",
    "PROVISIONAL_DISPOSITIONS",
    "RECONCILIATION_CASE_TYPES",
    "RECONCILIATION_TRIGGERS",
    "RESTING_ORDER_STATUSES",
    "ROLE_TO_ORDER_TYPE",
    "SAFE_MODE_EVENT_TYPES",
    "SAFE_MODE_TYPES",
    "TERMINAL_ATTEMPT_STATUSES",
]


# Enumerable storage values validated at the write boundary (fail loud on a typo
# rather than persisting it). ``Side`` carries the fill/order direction; these
# small sets cover the columns that don't warrant a full enum yet (their typed
# writers land in PR3).
_MODES = frozenset({"paper", "live"})
_LIQUIDITY_TYPES = frozenset({"maker", "taker", "simulated"})
# §14 live fills: the exchange marks each fill maker or taker (``crossed``); a
# simulated paper fill has no exchange liquidity role, so "simulated" is NOT a
# member here — a live fill must land on one of the two real roles.
LIVE_LIQUIDITY_ROLES = frozenset({"maker", "taker"})
# §15 accounting corrections: a backfilled fee, funding, or realized-PnL amount
# is an explicit adjustment event, never a silent overwrite of the recorded
# fill/funding row. Live replay FOLDS these deltas (accounting.replay), so the
# vocabulary is the fold's contract — a type not listed here has no defined
# ledger effect and is rejected at the write boundary.
ACCOUNTING_ADJUSTMENT_TYPES = frozenset({"fee", "funding", "realized_pnl"})
_FLIP_LEGS = frozenset({"open", "close"})
_FUNDING_STATUSES = frozenset({"pending", "posted"})
# phase2-data §10 funding provenance vocabulary.
_FUNDING_SOURCES = frozenset(
    {"live_public_data", "funding_history_backfill", "exchange_user_funding"}
)
# The §6.2 ``decision_attempts.error_type`` vocabulary is ``ERROR_TYPES``,
# imported above and re-exported here as the storage vocabulary. It is DEFINED
# in ``common.constants`` (with the member-by-member rationale) because the
# guard that produces a class — ``domains.perp.freshness``, which must not
# import this package — validates against the same set at construction; this
# module admits it at the write boundary. One list, two check sites.
# scheduler_state CSV-export breadcrumb states (schema v3).
_EXPORT_STATUSES = frozenset({"ok", "failed"})
# Replay breadcrumb vocabulary: "mismatch" = replay ran and contradicted the
# materialized state; "failed" = replay itself raised (books unverifiable).
_REPLAY_STATUSES = frozenset({"ok", "mismatch", "failed"})
# Config-drift-on-resume breadcrumb states (schema v5): "drift" = the resumed
# run's risk/decision/paper_trading blocks differ from the genesis record.
_CONFIG_DRIFT_STATUSES = frozenset({"ok", "drift"})
# live_order_attempts vocabulary (schema v6, phase3-spec §8.3): which exchange
# action the round-trip was, and where it ended. "submitted" is written BEFORE
# the network call — a row stuck there means the outcome is unknown and the
# duplicate-retry protocol must query the exchange before resending.
_LIVE_ATTEMPT_ACTIONS = frozenset({"place", "cancel", "cancel_by_cloid"})
_LIVE_ATTEMPT_STATUSES = frozenset({"submitted", "acknowledged", "rejected", "duplicate", "failed"})
# §18.5: the complete kill-switch event vocabulary. Public: PR 6's acceptance
# metrics read them. The PR 2 kill switch manager writes eight of the nine —
# kill_switch_cancel_triggered included, since PR 2 detects the case where the
# deadline lapsed and the exchange demonstrably fired the switch on us.
# emergency_kill_switch_triggered belongs to the §18.3 emergency-trigger state
# machine, which is NOT in PR 2 — no rows with that type exist yet.
KILL_SWITCH_EVENT_TYPES = frozenset(
    {
        "kill_switch_armed",
        "kill_switch_refreshed",
        "kill_switch_refresh_failed",
        "kill_switch_cancel_triggered",
        "kill_switch_disarmed",
        "kill_switch_disarm_failed",
        "emergency_kill_switch_triggered",
        "shutdown_cancel_orders_started",
        "shutdown_cancel_orders_completed",
    }
)


_ATTEMPT_STATUSES = frozenset({"in_progress", "completed", "api_failed", "invalid_output"})
# The live/terminal split of each status vocabulary, defined HERE next to the
# canonical sets so a future status addition updates both in one place — a
# hand-copied subset in a consumer (reconcile's cancel sweep, the validator's
# cycle counting) would silently miss the new member (check_enum can validate
# membership, never completeness).
TERMINAL_ATTEMPT_STATUSES: tuple[str, ...] = ("completed", "api_failed", "invalid_output")
LIVE_PLAN_STATUSES: tuple[str, ...] = ("active", "paused_market_data")
# "submitted" belongs here: a pre-ack live order is non-terminal — it may be
# resting on the exchange — so the restart cancel sweep and PR 4's
# reconciliation must see it, never skip it as already-settled.
#
# CAVEAT for PR 4/5: the PAPER restart sweep (paper/reconcile.reconcile_on_restart)
# marks everything in this tuple canceled locally with NO exchange call — sound
# for paper, where nothing ever rested. Pointing that sweep at a LIVE run would
# silently mark a genuinely resting order canceled on no evidence, the exact
# opposite of §8.3's query-before-assume protocol. Live reconciliation must ask
# the exchange; do not reuse the paper sweep as-is.
LIVE_ORDER_STATUSES: tuple[str, ...] = (
    "pending_market_data",
    "submitted",
    "open",
    "partially_filled",
)
# The narrower "could be ON THE BOOK right now" subset: LIVE_ORDER_STATUSES minus
# pending_market_data, which has by definition never been sent. Derived, not
# re-typed, so a new live status joins both sets at once. Two consumers ask the
# same question of different sources and must agree on the vocabulary:
# active_protection_order() asks SQLite, and protection._row_still_rests() asks
# the exchange's orderStatus when a fired kill switch has made the rows suspect.
RESTING_ORDER_STATUSES: tuple[str, ...] = tuple(
    status for status in LIVE_ORDER_STATUSES if status != "pending_market_data"
)
# §8.3 rule 10: the live-attempt statuses that are DURABLE PROOF the exchange
# received the order — the only local evidence that can contradict an
# "unknownOid" answer and forbid a resend. "duplicate" is proof at least as
# strong as "acknowledged": it is written ONLY when the exchange itself said
# the cloid already exists. The other three are not proof — "submitted" and
# "failed" mean the outcome is unknown, and "rejected" means the exchange
# answered but created no order; all three leave the cloid resendable once
# orderStatus confirms it is absent. Read this through
# has_exchange_known_cloid(), never by hand: an acknowledged CANCEL proves the
# exchange saw the cancel, not the place — and the attempt rows are only HALF the
# evidence (a recovered order's proof lives on orders.exchange_order_id).
EXCHANGE_KNOWN_ATTEMPT_STATUSES: tuple[str, ...] = ("acknowledged", "duplicate")
_NOT_EXCHANGE_KNOWN_ATTEMPT_STATUSES: tuple[str, ...] = ("submitted", "rejected", "failed")


# §8.1: the live roles are a superset of the four Phase 2 paper roles (close /
# emergency_close / cleanup_cancel are live-only vocabulary). One shared
# frozenset (defined next to the cloid derivation that also stamps roles) so
# the id layer and the write boundary can never accept different vocabularies.
_ORDER_ROLES = LIVE_ORDER_ROLES
# ``ioc_limit`` is the live wire type: every §9 slice (and §9.4 close /
# emergency close) is an IOC limit order; the four paper_* / trigger types are
# the Phase 2 simulation vocabulary.
_ORDER_TYPES = frozenset(
    {"paper_market", "paper_twap_slice", "stop_market", "take_market", "ioc_limit"}
)
# The trigger-role → order-type spelling, next to the vocabulary it draws from:
# writers that derive a type from a registry/protection role (the live orphan
# backfill; the paper engine's protection placement) must share one mapping or
# they drift and audit rows get mislabeled. Non-trigger roles take each
# writer's own wire-type default.
ROLE_TO_ORDER_TYPE = {"stop_loss": "stop_market", "take_profit": "take_market"}
# "submitted" is the live-only pre-ack state (phase3-spec §8.3): the order row
# is written before the network call and patched once the exchange answers.
_ORDER_STATUSES = frozenset(
    {
        "pending_market_data",
        "submitted",
        "open",
        "partially_filled",
        "filled",
        "canceled",
        "rejected",
    }
)
# The terminal half of the same vocabulary. Private (no consumer needs it): it
# exists so the partition guard below can prove LIVE_ORDER_STATUSES is not
# merely a valid subset but a COMPLETE one — a new non-terminal status that
# nobody added to LIVE_ORDER_STATUSES would otherwise be skipped as settled by
# the restart cancel sweep and PR 4's reconciliation.
_TERMINAL_ORDER_STATUSES: tuple[str, ...] = ("filled", "canceled", "rejected")
# §16.1 vocabulary contract: orders.exchange_status carries the NORMALIZED
# status family, exchange_raw_status the exchange's verbatim word. The families
# are exactly these four (§16.1 names them) — a strict subset of
# _ORDER_STATUSES, because the pre-ack local states (pending_market_data,
# submitted) and partially_filled describe OUR record, not any word the
# exchange answers with.
#
# Validated at the write boundary because the column is otherwise a free string
# that PR 4's reconciliation joins on: at the very call site that writes it, the
# ack path is also holding the RAW wire word (`resting` / `error`), and passing
# that into the normalized column would produce a plausible-looking row that
# silently never matches. The same-named parameter on update_live_order_attempt
# takes the opposite vocabulary (verbatim) — which is precisely how such a slip
# would happen.
_EXCHANGE_STATUS_FAMILIES = frozenset({"open", "filled", "canceled", "rejected"})
# Execution-plan lifecycle: ``active`` / ``paused_market_data`` are live; the rest
# are terminal (execution §1.1 / §1.3 / §4.1).
_PLAN_STATUSES = frozenset(
    {
        "active",
        "paused_market_data",
        "completed",
        "canceled",
        "canceled_restart",
        "expired",
        "failed",
        "flip_incomplete",
        "rejected",
        "residual",
    }
)

# A split with no declared complement can only be checked for membership: it must
# stay a subset of its vocabulary, so a renamed status cannot leave a stale member
# behind. Checked at import.
for _split, _whole, _name in (
    (TERMINAL_ATTEMPT_STATUSES, _ATTEMPT_STATUSES, "TERMINAL_ATTEMPT_STATUSES"),
    (LIVE_PLAN_STATUSES, _PLAN_STATUSES, "LIVE_PLAN_STATUSES"),
):
    _extra = set(_split) - _whole
    if _extra:
        raise ValueError(f"{_name} contains statuses outside its vocabulary: {sorted(_extra)}")
del _split, _whole, _name, _extra

# Subset alone cannot catch the failure mode the comment above describes: a status
# ADDED to a vocabulary is silently absent from every split (check_enum validates
# membership, never completeness). Where a vocabulary declares both halves, assert
# the PARTITION instead — strictly stronger than the subset check (an alien member
# shows up as "unknown"), and a newly added status fails at import until it is
# deliberately classified, rather than defaulting into the safe-looking half.
# §8.3's resend guard is the one that must never silently widen.
for _split, _rest, _whole, _name in (
    (
        EXCHANGE_KNOWN_ATTEMPT_STATUSES,
        _NOT_EXCHANGE_KNOWN_ATTEMPT_STATUSES,
        _LIVE_ATTEMPT_STATUSES,
        "the live-attempt statuses",
    ),
    (LIVE_ORDER_STATUSES, _TERMINAL_ORDER_STATUSES, _ORDER_STATUSES, "the order statuses"),
):
    _union = set(_split) | set(_rest)
    if _union != _whole:
        raise ValueError(
            f"{_name} are no longer partitioned by their splits — "
            f"unclassified: {sorted(_whole - _union)}, unknown: {sorted(_union - _whole)}"
        )
del _split, _rest, _whole, _name, _union


# §17 protection lifecycle audit vocabulary. Placement / in-place modify
# (§17.4 modify-before-cancel) / repair failures / the two escalations
# (§17.2 SL repair exhausted → emergency close; §17.3 TP failure → degraded
# protection) / clearing a zeroed position's residual (§17.1 rule 4) / the
# §13.5 venue-identity latch (consecutive orderStatus answers this build could
# not read as being about the cloid it asked for — see
# live.venue_identity.VenueIdentityMonitor; written once per episode, not per
# answer, by the monitor shared across protection, reconciliation and the kill
# switch since issue #80 — its ``symbol`` is the run's coin from every
# production wiring, or ``kill_switch._UNSCOPED_SYMBOL`` from a manager-private
# monitor, which only test wirings build).
#
# On that last member vs the 2026-08-17 decision that this vocabulary is CLOSED:
# that decision refused a PER-ATTEMPT ``*_recovery_unreadable`` event, because
# the recovery probe's caller already writes a ``*_repair_failed`` row and the
# cause belonged in it. It still holds. The latch is a different fact: the OTHER
# probe sites — protection's no-op guard, the reconciler's tiebreakers, the
# shutdown cross-check — write no protection row at all, so for them there is
# no existing row to fold into, and what is recorded here is one per EPISODE
# rather than one per answer. Fold-into-the-existing-row remains the rule for
# anything the repair ladder already reports.
PROTECTION_ORDER_EVENT_TYPES = frozenset(
    {
        "stop_loss_placed",
        "stop_loss_modified",
        "stop_loss_repair_failed",
        "stop_loss_repair_exhausted",
        "stop_loss_repair_blocked",
        "take_profit_placed",
        "take_profit_modified",
        "take_profit_repair_failed",
        "protection_cleared",
        "emergency_close_triggered",
        "degraded_protection_entered",
        "degraded_protection_cleared",
        "identity_fault_latched",
    }
)


# The §12.3 sighting cases PR 3's fill ingestion records (PR 4's reconciliation
# module extends this vocabulary with its own case marks). Each row is a durable,
# QUERYABLE record of an exchange fact the books do not carry — the evidence JSON
# file alone cannot be a backlog (the payload dir is write-only evidence, and a
# file older than every backfill window is reachable by nothing). Resolution is
# PER TYPE (§12.3): only ``fill_unmapped`` rows carry the §14.2 dedupe key in
# ``exchange_value`` and resolve by anti-join against ``fills.exchange_fill_key``;
# ``fill_malformed`` (a bare tid, a content digest, or an ``envelope-`` fact key
# for a fault that belongs to the STREAM rather than to one message) and the two drift types
# (``key|digest`` describing fills that ARE booked) can never match that column
# and resolve by human review via PR 4's ``action_taken``.
RECONCILIATION_CASE_TYPES = frozenset(
    {
        # PR 3 ingest-side sightings (once per fact, deduped on exchange_value).
        "fill_unmapped",
        "fill_malformed",
        "fill_money_drift",
        "fill_fee_drift",
        # PR 4 sweep cases — the §12.3 table, one type per row, in its order.
        "order_missing_on_exchange",
        "orphan_exchange_order",
        "non_bot_owned_order",
        "invalid_local_fill",
        "exchange_fill_missing_local",
        "exchange_position_mismatch",
        "local_position_phantom",
        "equity_mismatch",
        "position_sl_missing",
    }
)

# The machine dispositions the §12 sweep writes for facts that can COME BACK:
# each ends an episode in one order's life, and the sweep can find itself
# looking at that same fact again — after a §8.3 rule-5 resend re-stamps the
# same orders row 'submitted', after _maybe_reopen_terminal_order revives a
# terminal one, or (for the reopen tiebreaker's read failure) simply after the
# venue's next answer flips. So the fact they dispose of is quiet, not
# impossible, and the once-per-fact dedupe treats them as PROVISIONAL: a later
# sighting under the same key is a NEW occurrence and gets its own row (see
# insert_exchange_reconciliation_event).
#
# Membership is the load-bearing part, and the reason it is a closed set rather
# than "any machine stamp": a disposition NOT listed here keeps its key shut
# forever, which is right for two quite different things —
#   * A fact that cannot come back: ``local_row_backfilled`` (this package
#     contains no `DELETE FROM orders`) and ``resolved_fill_booked`` (nor
#     `DELETE FROM fills`). ``backfilled`` is outside for a third reason — its
#     sweep case carries no ``exchange_value``, so it never meets the dedupe.
#     All three are still MACHINE stamps; membership of THIS set is about how
#     the dedupe treats them, not about who wrote them. The set that answers
#     "did the sweep write this word" is ``MACHINE_DISPOSITIONS`` below.
#   * A human's ``--stamp-case`` text: the operator disposing of an
#     ``order_missing_on_exchange`` row whose order STAYS in the cursor (§8.3
#     rule 10: the exchange took the cloid and denies it) must not have their
#     stamp answered by a fresh row on every pass thereafter — that re-sighting
#     flood is what the dedupe exists to stop. Enforced at the operator's end
#     as well: ``safe-mode --stamp-case`` refuses an ``--action`` drawn from
#     this set, because the guard reads the STRING, not who wrote it.
#
# Adding a member is a claim about how OFTEN the fact can come back — and
# "only after a deliberate revive" is NOT true of every member below; see each
# one. What a member must be is a fact whose recurrence earns its own row. Only
# the NEWEST row under a key is ever unresolved (each recurrence is closed by
# whatever ends it before the next one opens), so the operator surfaces keep
# showing one live fault however often it comes back — what grows is the events
# table, by one row per recurrence.
PROVISIONAL_DISPOSITIONS = frozenset(
    {
        # _settle_absent_order: unknownOid with no §8.3 rule-10 evidence — the
        # send never landed, the local row goes 'rejected'. Back only after a
        # deliberate rule-5 resend.
        "settled_never_sent",
        # _clear_read_failure_case, for BOTH tiebreakers' read-failure keys.
        # Absent-order side: stamped only once the pass settled the order, so
        # back only after a revive. Reopen side: stamped on any answered read
        # while the local row stays terminal, so back on the venue's next flap
        # — the one member whose recurrence is not revive-bounded (the cost is
        # argued at that call site).
        "resolved_read_succeeded",
        # _maybe_reopen_terminal_order: the terminal local row was wrong and is
        # now live again. Back once some pass settles that row again — which
        # _settle_absent_order does on its own, so this member and
        # ``settled_{status}`` can revive each other with no resend involved.
        "local_row_reopened",
    }
    # _settle_absent_order's other disposal: orderStatus answered with a
    # terminal status and the local row was written to match. Derived from the
    # status vocabulary rather than spelled out, so a new terminal status
    # cannot land a stamp this set does not know about.
    | {f"settled_{status}" for status in _TERMINAL_ORDER_STATUSES}
)

# The sweep's own dispositions that shut their key FOR GOOD (or never meet the
# dedupe at all) — the complement of PROVISIONAL_DISPOSITIONS within the
# machine vocabulary. Spelled out here so the union below is a closed set: the
# three are the exhaustive "sweep wrote it, and no later sighting reopens the
# key" list.
_FINAL_DISPOSITIONS = frozenset(
    {
        # _reconcile_orders, orphan back-fill: the missing local row now exists.
        "local_row_backfilled",
        # _reconcile_fills: the stream fault's fill is booked.
        "resolved_fill_booked",
        # exchange_fill_missing_local: carries no exchange_value, so it never
        # reaches the dedupe in the first place (see the note above).
        "backfilled",
    }
)

# Every disposition the §12 sweep itself can write — the CLOSED vocabulary for
# ``action_taken`` values that are not a human's ``--stamp-case`` prose.
#
# Why closed, and why validated at construction (issue #84, following #65):
# PROVISIONAL_DISPOSITIONS decides by STRING COMPARISON whether a fact key
# reopens for its next sighting. A sixth machine disposition added — or an
# existing one renamed — without updating that set does not fail anywhere: the
# key simply stays shut forever, which is exactly the #65 defect returning for
# that one disposition, with no error and no log. The only way it surfaces is
# an operator noticing a recurrence that never reached ``safe-mode --status``,
# and #65's whole point was that this path is invisible.
#
# So ``ReconciliationCase.__post_init__`` checks membership here, and so does
# the sweep's import-time loop for the four stamps that check cannot stand in
# front of (three case-less writes, and the orphan back-fill whose case is
# built after its write — issue #104). A new disposition then fails LOUDLY at the
# moment it is constructed — an unclean leg, in the pass that introduced it —
# and whoever adds it must come here and decide which half it belongs in.
# ``safe-mode --stamp-case`` refuses this whole set for the mirror reason: the
# dedupe reads the string, not who wrote it, and an audit row must not leave a
# reader unable to tell a human's attestation from the daemon's.
MACHINE_DISPOSITIONS = PROVISIONAL_DISPOSITIONS | _FINAL_DISPOSITIONS

# Which code path observed the case: the PR 3 ingest sighting, or one of the
# §12.2 reconciliation timings PR 4's sweep runs at. Public: the reconciler
# names its trigger from this set and the acceptance metrics group by it.
RECONCILIATION_TRIGGERS = frozenset(
    {
        "live_fill_ingest",
        "startup",
        "pre_cycle",
        "post_cycle",
        "order_ack",
        "fill",
        "protection_change",
        "heartbeat",
        "shutdown",
        "mismatch",
    }
)
# Backwards-compatible private alias (PR 3 callers/tests referenced the old name).
_RECONCILIATION_TRIGGERS = RECONCILIATION_TRIGGERS


# §13.3: the two safe-mode types. Also validates the scheduler_state column.
SAFE_MODE_TYPES = frozenset({"recoverable", "manual"})

# §13.6 history vocabulary. ``safe_mode_entered`` records every entry,
# ``safe_mode_escalated`` a recoverable→manual upgrade while already inside,
# ``safe_mode_released`` every exit — CLI (§13.6 rule 2) and §13.4 auto-recovery
# alike, distinguished by ``released_by``. ``safe_mode_reason_added`` records a
# DISTINCT manual reason observed while manual safe mode is already latched
# (decided 2026-07-17): the current-state trio keeps the FIRST reason, but the
# operator's §13.6 triage surface must show every independent fact — a second
# reason absorbed silently would hide, say, an invalid local fill behind an
# already-reported non-bot order.
SAFE_MODE_EVENT_TYPES = frozenset(
    {
        "safe_mode_entered",
        "safe_mode_escalated",
        "safe_mode_released",
        "safe_mode_reason_added",
    }
)


# The four §20.2 outcomes a smoke step can land on. "skipped" is the dry-run /
# not-selected verdict (never satisfies the §20.2 cycle-entry gate); "error" is
# the harness failing to even run the step (distinct from a step that ran and
# "failed" its assertion) — the acceptance gate treats both non-"passed" the
# same, but the operator triages them differently.
LIVE_SMOKE_TEST_STATUSES = frozenset({"passed", "failed", "skipped", "error"})
