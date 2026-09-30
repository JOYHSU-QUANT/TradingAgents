"""The §12 sweep's orders leg: the open-orders listing against the local rows.

One leg of :meth:`~.reconcile.LiveReconciler.run`, a function over a
:class:`~.reconcile_types.SweepContext`: :func:`reconcile_orders` walks the
exchange's open-orders view (§12.3 rows 2–3, §19.3 bot-ownership) and then
the locally-live rows the view did not list (§12.3 row 1), putting each
disputed cloid to ``orderStatus`` through the shared §13.5 identity monitor.
Called inside ``run()``'s guarded lane: it appends to ``cases`` / ``errors``
and returns the leg's verdict. The three fact-key functions below are this
leg's once-per-fact keys, written and looked up only here.

Wire vocabulary read here: the frontendOpenOrders listing fields (``oid``,
``coin``, ``cloid``, ``side``, ``origSz``/``sz``, ``limitPx``, ``reduceOnly``,
``tif``) — see the division of labour in the mapper's module docstring and in
the WIRE VOCABULARY note at the top of ``reconcile.py``.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime

from ..exchanges.hyperliquid.mapper import HL_SIDE_TO_LOCAL, optional_decimal, require_decimal
from ..persistence import repository as repo
from .orders import ORDER_TYPE_FOR_TIF, local_status_for_exchange_status
from .reconcile_types import (
    ORPHAN_BACKFILLED_DISPOSITION,
    READ_SUCCEEDED_DISPOSITION,
    ReconciliationCase,
    SweepContext,
)
from .venue_identity import ProbeSite, describe_order_status_failure

__all__ = ["reconcile_orders"]

logger = logging.getLogger(__name__)


def _read_failure_fact_key(cloid: str) -> str:
    """The once-per-fact key for "orderStatus could not be read for this cloid".

    A DIFFERENT fact from the order's real disposition, so a different key (the
    same reasoning, and the same ``|`` suffix shape, as its mirror on the reopen
    side — ``_local_terminal_read_failure_fact_key``). This key and the bare
    cloid both land under case_type ``order_missing_on_exchange``, and while
    they WERE one key the first sighting to arrive owned it — a later genuine absence
    of that cloid was swallowed by the dedupe and never recorded, which is
    exactly the pairing a venue misroute produces every tick.

    ONE function so the write site and the disposition lookup cannot drift: a
    key written one way and looked up another would leave the row open forever
    with no error anywhere.
    """
    return f"{cloid}|read_failed"


def _local_terminal_fact_key(cloid: str) -> str:
    """The reopen tiebreaker's once-per-fact key: what orderStatus ANSWERED.

    "The exchange lists this cloid open while our row is terminal" — a fact of
    its own, so a later re-settle of the same cloid must not dedupe against a
    plain-orphan sighting, and vice versa.

    Shared by the two outcomes that are readings of one situation: the reopen
    (answer LIVE, the terminal row was wrong) and the unknownOid contradiction
    (the venue's two views disagree). NOT by the read failure, which has its
    own key below — see there.
    """
    return f"{cloid}|local_terminal"


def _local_terminal_read_failure_fact_key(cloid: str) -> str:
    """The once-per-fact key for "orderStatus could not be read" HERE.

    The reopen tiebreaker's read failure is a different fact from what a
    successful read then says, and it splits off for exactly the reasons
    ``_read_failure_fact_key`` splits off the absent-order tiebreaker's — the
    two directions are mirror images and now have mirror-image keys:

    * A recorded read failure would otherwise swallow the unknownOid
      contradiction that a later pass finds under the same key: the venue
      contradicting itself is the graver fault and it would reach no durable
      row at all.
    * And the disposal would run the other way: "a later read answered" is what
      disproves the read failure, but stamping that onto a CONTRADICTION row
      would assert a read that never failed, over a detail saying it answered
      unknownOid.

    ONE function, same as its sibling: this key is looked up as well as
    written, and a lookup spelled differently from the write would leave the
    row open forever with no error anywhere.
    """
    return f"{cloid}|local_terminal_read_failed"


def reconcile_orders(
    ctx: SweepContext,
    open_orders: list | None,
    cases: list[ReconciliationCase],
    errors: list[str],
    now: datetime,
) -> bool:
    """§12.3 order rows + §19.3 bot-ownership: the listing, then the local rows it omitted."""
    if open_orders is None:
        return False
    ok = True
    conn = ctx.db.conn
    exchange_open_cloids: set[str] = set()

    for order in open_orders:
        # §18.2: this loop's reopen check asks orderStatus per order, and the
        # exchange decides how many orders there are — refresh every
        # iteration so the wall of round-trips is never one unbroken gap.
        #
        # At the TOP, unlike the absent-order loop below, which refreshes
        # only for rows it is about to probe. Several branches here can reach
        # the wire and they do not share one guard, so tracking them
        # individually would be the kind of bookkeeping that silently misses
        # the branch added next year. A refresh on an iteration that turns
        # out to do no I/O costs a clock read (tick() reaches the wire only
        # when a refresh is due), which is the cheaper mistake.
        ctx.refresh_deadline()
        if not isinstance(order, dict):
            errors.append(f"malformed open_orders entry ({type(order).__name__})")
            ok = False
            continue
        oid = str(order.get("oid", "?"))
        coin = order.get("coin")
        cloid = order.get("cloid")
        registry = repo.get_cloid_by_hex(conn, cloid) if isinstance(cloid, str) else None
        if registry is None or not isinstance(cloid, str):
            # §12.3 row 3 / §19.3: no cloid, or a cloid our registry never
            # issued → non-bot-owned → manual safe mode; never manage it.
            ok = False
            cases.append(
                ReconciliationCase(
                    case_type="non_bot_owned_order",
                    symbol=coin if isinstance(coin, str) else None,
                    local_value=None,
                    exchange_value=f"oid={oid}",
                    detail=(
                        f"exchange open order oid={oid} cloid={cloid!r} has no "
                        "cloid_registry mapping — not bot-owned (§19.3); manual "
                        "safe mode, the bot never manages it (§25)"
                    ),
                    manual=True,
                )
            )
            continue
        exchange_open_cloids.add(cloid)
        local = repo.get_order_by_cloid_hex(conn, cloid)
        if local is None:
            # §12.3 row 2: bot-owned on the exchange, absent locally →
            # back-fill the local row from what the exchange reported
            # (fail-safe direction: once the row exists, every later sweep
            # — kill-switch shutdown included — sees and manages it).
            # The stamp is chosen AFTER ``insert_order`` commits, so
            # __post_init__ is too late to guard this write; it is checked
            # at import instead — see ``ORPHAN_BACKFILLED_DISPOSITION``.
            resolved = _backfill_orphan_order(ctx, order, registry, now)
            cases.append(
                ReconciliationCase(
                    case_type="orphan_exchange_order",
                    symbol=registry["symbol"],
                    local_value=None,
                    exchange_value=cloid,
                    detail=f"exchange open order oid={oid} had no local orders row",
                    action_taken=ORPHAN_BACKFILLED_DISPOSITION if resolved else None,
                    resolved=resolved,
                )
            )
            if not resolved:
                ok = False
        elif local["status"] not in repo.LIVE_ORDER_STATUSES:
            # The exchange's open-orders view lists the order but the local
            # row is terminal. TWO very different stories fit this shape:
            # a past pass settled the row off a wrong unknownOid answer
            # (the send had landed — the row must reopen, §12.1), or the
            # open-orders view is merely BEHIND a cancel this very startup
            # just landed (reopening would resurrect a phantom-open row
            # for an order the run itself retired). orderStatus is the
            # tiebreaker, same as the mirror direction's non-case reading:
            # two eventually-consistent reads disagreeing is not a
            # local/exchange conflict.
            reopened, case = _maybe_reopen_terminal_order(ctx, order, registry, local, now)
            if case is not None:
                cases.append(case)
            if not reopened:
                ok = False

    # §12.3 row 1: locally-live orders the exchange's open-orders view did
    # not list. Ask orderStatus directly; never resend (§8.3).
    for row in repo.iter_open_live_orders(conn):
        cloid = row["cloid_hex"]
        if cloid in exchange_open_cloids:
            continue
        # §18.2: one orderStatus round-trip per absent row, and this cursor
        # deliberately spans runs — so the count is bounded by the store's
        # history, not by anything this run configured. Refresh before each.
        ctx.refresh_deadline()
        settled, case = _settle_absent_order(ctx, row, now)
        if case is not None:
            cases.append(case)
            if case.action_taken is not None:
                # ONLY when this pass DISPOSED of the order, which is what
                # takes it out of THIS cursor. The other outcomes leave it
                # here, in a cursor that keeps probing it (while the
                # exchange does not list it — a pass that finds it listed
                # skips this loop for it), so a later pass gets to settle it
                # and stamp then. Waiting costs one row that stays open a
                # while; stamping on any answered read would cost a fresh
                # row per unreadable→readable flap of the venue, for a fault
                # the sweep has not finished with. The sibling caller has no
                # later pass to wait for and takes the other trade — see
                # _clear_read_failure_case for the pair.
                #
                # Disposal does not make recurrence impossible — a §8.3
                # rule-5 resend re-stamps the same order_id 'submitted'
                # (live/orders.py) and _maybe_reopen_terminal_order revives a
                # terminal row — it makes it need a deliberate new send or a
                # contradicting exchange answer first. That is the bound
                # this key relies on: on THIS side, rows come back per
                # revive rather than per flap.
                _clear_read_failure_case(
                    ctx,
                    cloid,
                    case_type="order_missing_on_exchange",
                    fact_key=_read_failure_fact_key(cloid),
                )
        if not settled:
            ok = False
    return ok


def _maybe_reopen_terminal_order(
    ctx: SweepContext, order: dict, registry: sqlite3.Row, local: sqlite3.Row, now: datetime
) -> tuple[bool, ReconciliationCase | None]:
    """Reopen a terminal local row ONLY when orderStatus proves it live.

    Returns ``(ok, case)``. orderStatus is the authority (§8.3): a LIVE
    answer proves the terminal row was wrong — reopen it (§12.1, the
    exchange wins). A TERMINAL answer proves the open-orders view is
    merely behind (typically a cancel this very startup just landed) —
    no case, nothing touched. Anything else (unknownOid contradicting the
    listing, a failed read) is an unproven conflict: recorded, unclean,
    never guessed.
    """
    cloid = local["cloid_hex"]
    oid = str(order.get("oid", "?"))
    try:
        parsed = ctx.identity.probe(cloid, site=ProbeSite.RECONCILE_ORPHAN_TIEBREAKER)
    except Exception as exc:  # noqa: BLE001 — a failed read is a verdict
        # Log-at-origin, same as _settle_absent_order's sibling tiebreaker:
        # the case detail is an in-memory value (and its row write is
        # fail-soft), so without this line a transient API error here
        # would leave no trace of WHY this order failed to reconcile.
        # The clause tells the two families apart for triage; the fact
        # key and the disposition are the same for both — an unreadable
        # ANSWER is still "we could not ask this cloid" as far as the
        # case ledger is concerned, and its bound lives in the monitor,
        # not in extra rows.
        why = describe_order_status_failure(exc)
        logger.warning("tiebreaker for cloid %s: %s", cloid, why)
        return False, ReconciliationCase(
            case_type="orphan_exchange_order",
            symbol=registry["symbol"],
            local_value=f"{local['order_id']}:{local['status']}",
            exchange_value=_local_terminal_read_failure_fact_key(cloid),
            detail=(
                f"exchange lists oid={oid} open but the local row is terminal "
                f"({local['status']}) and {why}"
            ),
        )
    # The read ANSWERED, whatever it answered — so the fact "we could not
    # ask this cloid" is disproved, for all three outcomes below. Disposing
    # of it here rather than per branch is the whole reason that fact has a
    # key of its own: the unknownOid branch below records a DIFFERENT fault
    # (the venue contradicting itself), and stamping "the read succeeded"
    # onto that row would say something that never happened (issue #66).
    _clear_read_failure_case(
        ctx,
        cloid,
        case_type="orphan_exchange_order",
        fact_key=_local_terminal_read_failure_fact_key(cloid),
    )
    if parsed is not None:
        exchange_order_id = parsed.exchange_order_id
        raw_status = parsed.status
        local_status = local_status_for_exchange_status(raw_status)
        if local_status in repo.LIVE_ORDER_STATUSES:
            # Confirmed live: the terminal row was wrong (the usual story:
            # a past pass settled it off a wrong unknownOid answer). The
            # row reopens, making the order visible again to
            # iter_open_live_orders and the shutdown cross-check. Persist the
            # MAPPED local status, never a literal "open": only "open" passes the
            # guard today, but hardcoding it is the same idiom that let the
            # protection manager misrecord a filled/canceled recovery as live.
            #
            # Built BEFORE the write, though it is returned after: its
            # __post_init__ is what validates the disposition, and an
            # unclassified one must not leave the order reopened in SQLite
            # with no audit row saying why (issue #84).
            case = ReconciliationCase(
                case_type="orphan_exchange_order",
                symbol=registry["symbol"],
                local_value=f"{local['order_id']}:{local['status']}",
                # Distinct fact key: a later re-settle of the same cloid
                # must not dedupe against a plain-orphan sighting.
                exchange_value=_local_terminal_fact_key(cloid),
                detail=(
                    f"exchange lists oid={oid} open, orderStatus confirms "
                    f"{raw_status!r}, but the local row was terminal "
                    f"({local['status']}) — reopened per §12.1"
                ),
                action_taken="local_row_reopened",
                resolved=True,
            )
            with ctx.db.transaction() as tx:
                repo.update_order(
                    tx,
                    local["order_id"],
                    status=local_status,
                    status_reason="reopened_from_exchange_reconciliation",
                    exchange_order_id=exchange_order_id,
                    exchange_status=local_status,
                    exchange_raw_status=raw_status,
                    updated_at=now,
                )
            return True, case
        # orderStatus says terminal too: the open-orders view is behind
        # (a cancel this startup just landed is the common cause). Two
        # eventually-consistent reads disagreeing for a moment is not a
        # local/exchange conflict — same reading as the mirror direction.
        # No case, therefore no ``LiveReconciler._record_cases`` restamp: the
        # read failure this pass disproved was disposed of above, where every
        # answered read is treated alike (issue #66 — before that, this
        # outcome, which is the COMMON one, left the read-failure row open
        # forever).
        return True, None
    # unknownOid while open_orders LISTS the order: contradictory exchange
    # answers — unproven either way, never guessed.
    return False, ReconciliationCase(
        case_type="orphan_exchange_order",
        symbol=registry["symbol"],
        local_value=f"{local['order_id']}:{local['status']}",
        exchange_value=_local_terminal_fact_key(cloid),
        detail=(
            f"exchange lists oid={oid} open but orderStatus answers unknownOid "
            f"and the local row is terminal ({local['status']}) — contradictory "
            "exchange answers, left for the next pass"
        ),
    )


def _orphan_order_type(ctx: SweepContext, order: dict, registry: sqlite3.Row) -> str:
    """The ``orders.type`` word for an orphan: role for triggers, venue tif otherwise.

    The listing entry carries the tif when the venue includes it; the
    documented ``orderStatus`` shape always does, so an entry without one
    is probed (through the shared identity monitor, §13.5). Raises when
    the word is missing or is not one this system places — the caller's
    back-fill then fails as it does for any unusable field, leaving the
    orphan an open mismatch rather than a mislabeled row.
    """
    role_type = repo.ROLE_TO_ORDER_TYPE.get(registry["order_role"])
    if role_type is not None:
        return role_type
    tif = order.get("tif")
    if not isinstance(tif, str):
        try:
            reading = ctx.identity.probe(
                registry["cloid_hex"], site=ProbeSite.RECONCILE_ORPHAN_TYPE
            )
        except Exception as exc:  # noqa: BLE001 — named like the sibling probes, then re-raised
            raise ValueError(
                f"cannot derive orders.type for orphan cloid {registry['cloid_hex']}: "
                f"the listing carries no tif and {describe_order_status_failure(exc)}"
            ) from exc
        tif = None if reading is None else reading.tif
    order_type = ORDER_TYPE_FOR_TIF.get(tif) if isinstance(tif, str) else None
    if order_type is None:
        raise ValueError(
            f"cannot derive orders.type for orphan cloid {registry['cloid_hex']}: "
            f"tif {tif!r} is not a wire type this system places"
        )
    return order_type


def _backfill_orphan_order(
    ctx: SweepContext, order: dict, registry: sqlite3.Row, now: datetime
) -> bool:
    """Insert the missing local row for a bot-owned exchange order."""
    try:
        side_raw = order.get("side")
        side = HL_SIDE_TO_LOCAL.get(side_raw) if isinstance(side_raw, str) else None
        if side is None:
            raise ValueError(f"open_orders entry side {side_raw!r} not recognised")
        # `is None` fallback, not dict.get's default: a PRESENT-but-null
        # origSz must still fall through to sz (get's default only covers
        # the absent-key case, and a required field that is None raises).
        raw_orig = order.get("origSz")
        if raw_orig is None:
            raw_orig = order.get("sz")
        # The mapper's guards, not a local Decimal(str(...)): "NaN"/"inf"
        # parse WITHOUT error, and a non-finite qty would land in the
        # orders row this back-fill inserts — read back later by the
        # protection manager's coverage compare (``live/protection.py``
        # reads this column at both its resting protection-order checks —
        # the SL-coverage one and the role-agnostic _establish one).
        # Required sizes fail loud (issue #81); limitPx keeps its optional
        # contract (a market order legitimately has none), so an unusable
        # one degrades to None rather than failing the back-fill —
        # refusing the row would leave a real exchange order with no local
        # row at all, and so outside the §18.2 disarm cross-check.
        #
        # ``field`` names the key the value CAME from, not the column it
        # fills: after the fallback above, a bad ``sz`` must not be
        # reported as a bad ``origSz``.
        raw_sz = order.get("sz")
        qty = require_decimal(
            raw_orig, field="origSz" if order.get("origSz") is not None else "sz"
        )
        remaining = require_decimal(raw_sz, field="sz")
        price = optional_decimal(order.get("limitPx"), field="limitPx")
        if price is None and order.get("limitPx") not in (None, ""):
            # The mapper logs the drop, but from inside a generic parser:
            # no cloid, no oid, no coin. This back-fill goes on to write a
            # RESOLVED case row, so without this line "the venue served a
            # corrupt price for a resting order" reduces to a context-free
            # WARNING inside an otherwise clean pass.
            #
            # ``""`` is excluded on purpose, not overlooked: the mapper's
            # optional contract treats blank as ABSENT (it does not log
            # either), and a market order with no price is the ordinary
            # case, not a corruption. Only a value that was PRESENT and
            # could not be used is worth an operator's attention.
            logger.warning(
                "orphan back-fill for cloid %s (oid %s): limitPx %r is unusable — "
                "the local row will be written with no price",
                registry["cloid_hex"],
                order.get("oid"),
                order.get("limitPx"),
            )
        # The registry role is the bot's own durable record of what it
        # placed: a trigger role names its type outright. A slice role does
        # NOT — the registry carries no tif, and since the maker path
        # (2026-09-16) a resting slice is an ``Alo`` at least as easily as
        # an ``Ioc`` (more easily: an IOC never rests long enough to be
        # orphaned). The venue's own ``tif`` word decides; a word this
        # system never places is refused, not defaulted — a wrong type
        # here is a permanent audit-row mislabel.
        order_type = _orphan_order_type(ctx, order, registry)
        with ctx.db.transaction() as conn:
            repo.insert_order(
                conn,
                # Deterministic and collision-free: one orphan row per cloid
                # (idx_orders_cloid_hex would reject a second anyway).
                order_id=f"orphan|{registry['cloid_hex']}",
                mode="live",
                run_id=registry["run_id"],
                symbol=registry["symbol"],
                order_role=registry["order_role"],
                side=side,
                order_type=order_type,
                qty=qty,
                filled_qty=qty - remaining,
                remaining_qty=remaining,
                status="open",
                status_reason="backfilled_from_exchange_reconciliation",
                price=price,
                reduce_only=bool(order.get("reduceOnly", False)),
                cloid_logical=registry["cloid_logical"],
                cloid_hex=registry["cloid_hex"],
                exchange_order_id=str(order.get("oid")),
                exchange_status="open",
                # The raw-status column stores the exchange's STATUS word
                # (every other writer stores "open"/"filled"/…); the source
                # here is presence in the open-orders listing, so "open" —
                # the earlier draft stored the orderType word ("Limit"),
                # which polluted the status vocabulary.
                exchange_raw_status="open",
                is_bot_owned=True,
                timestamp=now,
            )
        return True
    except Exception as exc:  # noqa: BLE001 — an unfillable orphan stays a mismatch
        logger.warning(
            "could not back-fill orphan order for cloid %s: %s", registry["cloid_hex"], exc
        )
        return False


def _settle_absent_order(
    ctx: SweepContext, row: sqlite3.Row, now: datetime
) -> tuple[bool, ReconciliationCase | None]:
    """One locally-live order absent from open_orders, put to orderStatus.

    Returns ``(settled, case)``. The §8.3 rule-10 evidence split decides
    the unknownOid answer: durable proof the exchange took the cloid makes
    "I don't know it" a MISMATCH (never a licence to resend); no proof
    means the send never landed, and the row is settled as rejected.
    """
    cloid = row["cloid_hex"]
    order_id = row["order_id"]
    try:
        parsed = ctx.identity.probe(cloid, site=ProbeSite.RECONCILE_ABSENT_SETTLE)
    except Exception as exc:  # noqa: BLE001
        # See the sibling in _maybe_reopen_terminal_order: same fact key
        # and disposition for both failure families, the clause alone
        # tells them apart, and the bound on the unreadable one lives in
        # the monitor the read went through.
        why = describe_order_status_failure(exc)
        logger.warning("cloid %s: %s", cloid, why)
        return False, ReconciliationCase(
            case_type="order_missing_on_exchange",
            symbol=row["symbol"],
            local_value=order_id,
            exchange_value=_read_failure_fact_key(cloid),
            detail=f"absent from open_orders and {why}",
        )
    if parsed is None:
        if repo.has_exchange_known_cloid(ctx.db.conn, cloid_hex=cloid):
            return False, ReconciliationCase(
                case_type="order_missing_on_exchange",
                symbol=row["symbol"],
                local_value=order_id,
                exchange_value=cloid,
                detail=(
                    "orderStatus answers unknownOid but durable local evidence "
                    "says the exchange took this cloid (§8.3 rule 10) — "
                    "unresolvable here, never resent"
                ),
            )
        # No proof the exchange ever saw it: the send never landed.
        # Case first, write second — see the sibling below.
        case = ReconciliationCase(
            case_type="order_missing_on_exchange",
            symbol=row["symbol"],
            local_value=order_id,
            exchange_value=cloid,
            detail="unknownOid with no §8.3 rule-10 evidence — settled as rejected",
            action_taken="settled_never_sent",
            resolved=True,
        )
        with ctx.db.transaction() as conn:
            repo.update_order(
                conn,
                order_id,
                status="rejected",
                status_reason="send_never_reached_exchange",
                updated_at=now,
            )
        return True, case
    exchange_order_id = parsed.exchange_order_id
    raw_status = parsed.status
    local_status = local_status_for_exchange_status(raw_status)
    if local_status in repo.LIVE_ORDER_STATUSES:
        # Still live per orderStatus; open_orders was merely behind. Not a
        # §12.3 case — two eventually-consistent exchange reads disagreeing
        # for a moment is not a local/exchange conflict.
        return True, None
    # Built before the write for the same reason as the reopen mirror and
    # the never-sent branch above: __post_init__ is what validates the
    # disposition, and settling the order in SQLite before validating it
    # would leave the row changed with no audit row explaining why (issue
    # #84). It matters most here, where the disposition is DERIVED
    # (``settled_{local_status}``) and so is the one word in this module no
    # reader can check by eye.
    case = ReconciliationCase(
        case_type="order_missing_on_exchange",
        symbol=row["symbol"],
        local_value=order_id,
        exchange_value=cloid,
        detail=f"settled from orderStatus: {raw_status}",
        action_taken=f"settled_{local_status}",
        resolved=True,
    )
    with ctx.db.transaction() as conn:
        repo.update_order(
            conn,
            order_id,
            status=local_status,
            exchange_order_id=exchange_order_id,
            exchange_status=local_status,
            exchange_raw_status=raw_status,
            updated_at=now,
        )
    return True, case


def _clear_read_failure_case(
    ctx: SweepContext, cloid: str, *, case_type: str, fact_key: str
) -> None:
    """Dispose of a past unreadable-orderStatus row that a later read disproved.

    ``LiveReconciler._record_cases``'s restamp reaches only rows under the SAME
    fact key, and each tiebreaker's read failure deliberately has one of its
    own (see ``_read_failure_fact_key`` and
    ``_local_terminal_read_failure_fact_key``) — so nothing else would ever
    close these rows. Left open, one transient API error would hold the §21.4
    ``unresolved_reconciliation_mismatch`` count above zero for the rest of
    the run, stampable only by hand, for a fact a later pass disproved.

    Both callers pass their own ``case_type``/``fact_key`` pair; the two
    keys land under DIFFERENT case types (the absent-order tiebreaker files
    ``order_missing_on_exchange``, the reopen tiebreaker
    ``orphan_exchange_order``), which is why neither is derived here.

    What "disproved" means differs by caller, and the difference is
    deliberate rather than an oversight in either:

    * The reopen tiebreaker stamps on the successful read itself, whatever
      it answered. It can therefore be re-observed without any revive: the
      local row stays terminal and the exchange goes on listing it, so an
      orderStatus that alternates unreadable/readable mints a row per flap.
      Accepted, because only the NEWEST of them is ever unresolved — every
      superseded one was closed by the read that ended it, so `safe-mode
      --status` and the §21.4 count still show one live fault, and the
      exit-5 ``orphan_exchange_order_count`` gate already fails at one row.
      What is bought is the common case: "orderStatus says terminal too"
      produces no case at all, so nothing else would ever close this row
      (issue #66).
    * The absent-order tiebreaker stamps only once the pass SETTLED the
      order, which is what takes it out of a cursor that would otherwise
      re-observe it every pass (see there). Two successful-read outcomes
      therefore leave its row open: unknownOid against §8.3 rule-10 evidence
      (that order already needs a human, so the extra row sits on a §12.3
      case someone is reading anyway), and "still live per orderStatus" —
      where the order may then be retired by a writer that is not this sweep
      (a §19.3 cancel, the kill switch, the protection manager), and the row
      holds §21.4's unresolved count above zero with nothing else reporting
      a problem. Accepted because this side does not NEED the wider rule:
      the order stays in a cursor that keeps probing it, so a later pass can
      settle it and stamp then — what orphans the row is another writer
      retiring the order first. The reopen side has no such second chance
      (its common outcome makes no case at all), which is the asymmetry.
      Not frequency: stamped on any answered read, BOTH sides would mint a
      row per unreadable→readable flap and neither per pass, since minting
      needs a failed read and the stamp before it a successful one.

    Fail-soft, like the liquidation mirror (``LiveReconciler._mirror_liquidation_price``):
    this is the audit trail's disposition, not a verdict input — a store that
    refuses the stamp must not fail the orders leg. The cost of losing it is
    one stale open row, the same shape the callers' guards deliberately leave
    behind elsewhere.
    """
    try:
        existing = repo.get_exchange_reconciliation_case(
            ctx.db.conn,
            ctx.run_id,
            case_type=case_type,
            exchange_value=fact_key,
        )
        # Pre-checked outside the write unit deliberately: a successful read
        # is the common case and almost never has a row to close, and
        # opening a BEGIN IMMEDIATE per absent order to discover that would
        # be the expensive way to do nothing.
        if existing is None or existing["action_taken"] is not None:
            return
        with ctx.db.transaction() as tx:
            # Which is exactly why the write has to be the if-unset one rather than
            # ``LiveReconciler._record_cases``'s set_reconciliation_action: the check
            # above is a separate step, so an operator (or a `--stamp-case` racing
            # this pass) may have disposed of the row in between, and THEIR
            # disposition is the one a human will look for (2026-07-30 concurrency
            # review).
            repo.stamp_reconciliation_action_if_unset(
                tx, existing["event_id"], READ_SUCCEEDED_DISPOSITION
            )
    except Exception as exc:  # noqa: BLE001 — audit disposition, see above
        logger.warning(
            "could not stamp the resolved orderStatus read failure for cloid %s "
            "(fact %s; %s: %s); the row stays open for a later pass or a human",
            cloid,
            fact_key,
            type(exc).__name__,
            exc,
        )
