"""The ``safe-mode`` subcommand: inspect / manually release a live run’s safe
mode, and the §12.3 reconciliation-case stamping lane (§13.6).
"""

from __future__ import annotations

import argparse
import sys

from ._common import _open_existing_db


def _cmd_safe_mode(argv: list[str]) -> int:
    """§13.6: the ONE manual-release interface (no config-flag release exists).

    ``--status`` (the default) prints the persisted current state and recent
    history — exit 0 when not in safe mode, 4 while one is latched, so a
    supervisor probe can branch without parsing stdout; ``--release --reason
    "<人工確認說明>"`` releases a MANUAL safe mode, writing the
    ``safe_mode_released`` audit event. Releasing does not resume trading:
    the run must still pass its next full reconciliation (§13.6 rule 3) —
    the released state only lifts the manual latch.
    """
    parser = argparse.ArgumentParser(
        prog="python -m contrib.hyperliquid_perp safe-mode",
        description="Inspect or manually release a live run's safe mode (§13.6).",
    )
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--db", default="live_trading.db", help="SQLite store path.")
    action = parser.add_mutually_exclusive_group()
    action.add_argument(
        "--status",
        action="store_true",
        help="Print the current safe-mode state, recent history and the open "
        "reconciliation cases (the default action). Exit 0 when not in safe "
        "mode, 4 while one is latched.",
    )
    action.add_argument(
        "--release",
        action="store_true",
        help="Release a MANUAL safe mode (writes the §13.6 audit event).",
    )
    action.add_argument(
        "--stamp-case",
        type=int,
        default=None,
        metavar="EVENT_ID",
        help="Record a human disposition on an open §12.3 reconciliation case "
        "(see --status for the ids). A fill_malformed case stops blocking the "
        "verdict once stamped; a fill_unmapped one is refused (it resolves by "
        "booking the fill, not by stamping). Requires --action.",
    )
    parser.add_argument(
        "--reason",
        default=None,
        help="Required with --release: the human confirmation recorded on the release event.",
    )
    parser.add_argument(
        "--action",
        default=None,
        help="Required with --stamp-case: what the human decided, recorded as "
        "the case's action_taken.",
    )
    parser.add_argument(
        "--released-by",
        default=None,
        help="Operator identity for the audit trail (default: the OS user name).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="With --status: how many recent history rows and open cases to "
        "print (default 10; 0 prints everything).",
    )
    args = parser.parse_args(argv)

    import getpass

    from ..live.safe_mode import SafeModeManager
    from ..persistence import repository as repo

    # migrate=True: safe-mode WRITES (--release / --stamp-case), and --status is
    # how an operator diagnoses a latched run. Refusing it on a store that needs
    # upgrading would disable the diagnostic tool at exactly the moment it is
    # needed — an upgrade that latches safe mode (2026-07-31 exit check).
    db = _open_existing_db(args.db, migrate=True)
    if db is None:
        return 1
    with db:
        run_row = repo.get_run(db.conn, args.run_id)
        if run_row is None:
            print(f"error: run {args.run_id!r} does not exist in {args.db}.", file=sys.stderr)
            return 1
        if run_row["mode"] != "live":
            # Same run-identity discipline as the live/paper resume guards: a
            # paper run has no safe-mode state, and letting the id through
            # would answer "safe_mode: none" (or a misleading release refusal)
            # instead of naming the actual mistake — a wrong --run-id / --db.
            print(
                f"error: run {args.run_id!r} in {args.db} is a "
                f"{run_row['mode']} run — safe mode is live-run state (§13.6). "
                "Fix --run-id / --db.",
                file=sys.stderr,
            )
            return 1
        # gate=None: this is the offline wiring — no live process, so the
        # persisted state IS the whole effect; the next startup hydrates it.
        manager = SafeModeManager(db=db, run_id=args.run_id, gate=None)
        state = manager.current()

        if args.stamp_case is not None:
            return _stamp_reconciliation_case(db, repo, args)

        if not args.release:
            if args.reason is not None or args.released_by is not None or args.action is not None:
                # Named rejection, not silence: these flags only mean something
                # on the release/stamp paths, and an operator who typed them
                # almost certainly meant one of those — dropping them without a
                # word would read as "release recorded".
                print(
                    "error: --reason/--released-by apply only with --release and "
                    "--action only with --stamp-case; the status action records nothing.",
                    file=sys.stderr,
                )
                return 1
            if args.limit is not None and args.limit < 0:
                print(
                    "error: --limit must be >= 0 (0 prints the full history).",
                    file=sys.stderr,
                )
                return 1
            if state is None:
                print("safe_mode: none")
            else:
                print(f"safe_mode: {state.safe_mode_type}")
                print(f"reason: {state.reason}")
                print(f"entered_at: {state.entered_at}")
            # Default 10 keeps the probe output lean; a long manual episode (one
            # reason_added row per distinct reason) can push the episode's
            # anchoring safe_mode_entered row out of a fixed tail, so the
            # operator can widen it (--limit 0 = all) without querying the DB
            # (decided 2026-07-17).
            limit = 10 if args.limit is None else args.limit
            history = repo.iter_safe_mode_events(db.conn, args.run_id)
            if history:
                rows = history if limit == 0 else history[-limit:]
                print("history (most recent last):")
                for row in rows:
                    who = f" by {row['released_by']}" if row["released_by"] else ""
                    print(
                        f"  {row['timestamp']}  {row['event_type']}"
                        f"{'' if row['safe_mode_type'] is None else ' ' + row['safe_mode_type']}"
                        f"{'' if row['reason'] is None else ': ' + row['reason']}{who}"
                    )
            # The §12.3 cases still open. Printed HERE because this is the only
            # place their event_ids surface: an open case can hold the verdict
            # unclean, and the safe-mode detail names a count, not ids.
            #
            # TWO resolution models, so two predicates — keying everything on
            # action_taken would list an unmapped sighting as stampable, and
            # stamping it changes NO verdict while hiding it from this very
            # listing (the operator's only enumeration of the backlog):
            #   - fill_unmapped  → open until the fill BOOKS (an anti-join that
            #     never reads action_taken); resolved by §8.3 re-ingest.
            #   - everything else → open until action_taken is stamped, by a
            #     human here or by the sweep for the dispositions it can
            #     establish (which is why a disposed-of row can be gone from
            #     this listing with nobody having touched it).
            unmapped_open = repo.iter_unresolved_fill_sightings(db.conn, args.run_id)
            unmapped_ids = {row["event_id"] for row in unmapped_open}
            open_cases = [
                row
                for row in repo.iter_exchange_reconciliation_events(db.conn, args.run_id)
                if (
                    row["event_id"] in unmapped_ids
                    if row["case_type"] == "fill_unmapped"
                    else row["action_taken"] is None
                )
            ]
            if open_cases:
                shown = open_cases if limit == 0 else open_cases[-limit:]
                print(
                    f"open reconciliation cases ({len(open_cases)}; stamp the "
                    'human-disposable ones with --stamp-case <event_id> --action "<disposition>"):'
                )
                for row in shown:
                    # Name the resolution path per row: an operator following a
                    # blanket "stamp these" instruction onto an unmapped sighting
                    # would believe they had cleared a block they had not.
                    how = (
                        " — resolved by booking the fill (§8.3 re-ingest), not by stamping"
                        if row["case_type"] == "fill_unmapped"
                        else ""
                    )
                    print(
                        f"  [{row['event_id']}] {row['timestamp']}  {row['case_type']}"
                        f"{'' if row['symbol'] is None else ' ' + row['symbol']}"
                        f"{'' if row['exchange_value'] is None else ' ' + row['exchange_value']}"
                        f"{how}"
                    )
            # Exit 4 while a safe mode is latched (0 = none): a supervisor
            # probe can branch without parsing stdout — the same multi-code
            # convention as validate's 4/5 and live's 0/4/1 (decided
            # 2026-07-17).
            return 0 if state is None else 4

        if args.limit is not None:
            # Same named-rejection discipline as --reason on the status path:
            # the flag only means something when history is printed.
            print(
                "error: --limit applies only with --status; the release action prints no history.",
                file=sys.stderr,
            )
            return 1
        if args.action is not None:
            print(
                "error: --action applies only with --stamp-case; a release records "
                "its human confirmation via --reason.",
                file=sys.stderr,
            )
            return 1
        if args.reason is None or not args.reason.strip():
            print(
                'error: --release requires --reason "<人工確認說明>" — the release '
                "event must record why a human decided the state is safe (§13.6 rule 2).",
                file=sys.stderr,
            )
            return 1
        # A RUNNING live process holds its gate flags in memory and only
        # hydrates them at startup — a release landing under it takes effect
        # in the store but not in that process until its next restart. Warn,
        # don't block: the §13.6 release is deliberately store-first.
        state_row = repo.get_scheduler_state(db.conn, args.run_id)
        if state_row is not None and state_row["lock_pid"] is not None:
            print(
                f"warning: run {args.run_id!r} has a run-lock held by pid "
                f"{state_row['lock_pid']} (heartbeat {state_row['lock_heartbeat_at']}) — "
                "a live process that is still running will not see this release "
                "until it restarts.",
                file=sys.stderr,
            )
        released_by = args.released_by or getpass.getuser()
        try:
            manager.release_manual(released_by=released_by, reason=args.reason)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print(f"manual safe mode released by {released_by}.")
        if state is not None:
            # Name what was cleared: the current-state trio carries the episode's
            # FIRST reason (entered_at anchors it) — additional reasons this
            # episode accrued are the safe_mode_reason_added rows in --status
            # history. Printing it lets the operator confirm the release matched
            # the fact they reviewed. (state was read just above; the CLI is
            # single-threaded, so nothing changed it before release_manual.)
            print(f"released episode: {state.reason} (entered {state.entered_at}).")
        print(
            "NOTE: trading does not resume yet — the run must pass its next full "
            "reconciliation before new orders are allowed (§13.6 rule 3)."
        )
        return 0


def _stamp_reconciliation_case(db, repo, args) -> int:
    """`safe-mode --stamp-case`: the §12.3 "人工核對後標記 action_taken" path.

    The tool that rule always implied and never had. Several case types can
    only ever be disposed of by a human — a ``fill_malformed`` sighting keyed
    by content digest can never be auto-resolved (nothing will ever join it to
    a booked fill), and money/fee-drift observations describe already-booked
    fills. Since an un-actioned case holds the verdict unclean, without this
    the exchange emitting ONE unparseable payload would wedge the run in safe
    mode permanently, with hand-written SQL against the live store as the only
    remedy.

    Stamping does not resume trading: the next reconciliation pass still has to
    prove the books (§13.6 rule 3's discipline, for the same reason).
    """
    if args.reason is not None or args.released_by is not None:
        print(
            "error: --reason/--released-by apply only with --release; a case "
            'disposition is recorded with --action "<disposition>".',
            file=sys.stderr,
        )
        return 1
    if args.limit is not None:
        print(
            "error: --limit applies only with --status; the stamp action prints no history.",
            file=sys.stderr,
        )
        return 1
    if args.action is None or not args.action.strip():
        print(
            'error: --stamp-case requires --action "<disposition>" — the audit row '
            "must record what the human decided about this case (§12.3).",
            file=sys.stderr,
        )
        return 1
    action = args.action.strip()
    if action in repo.MACHINE_DISPOSITIONS:
        # The sweep's OWN vocabulary, refused whole (issue #84). Named here
        # rather than left to the operator's word choice, because the RUNBOOK
        # publishes this vocabulary and invites imitation.
        #
        # The two halves are refused for different reasons, so the message says
        # which one this is rather than asserting the provisional argument over
        # a word it does not apply to:
        because = (
            # The once-per-fact guard reads the STRING, not who wrote it: these
            # words mean "the sweep disposed of an episode that can recur", so
            # one of them REOPENS the key for the next sighting. Right for the
            # daemon, wrong for a human — a case whose fault is still live (a
            # §8.3 rule-10 order never leaves the sweep's cursor) would answer
            # this stamp with a fresh unresolved row on every pass, holding
            # §21.4's count above zero for the rest of the run while this
            # command reported success each time.
            "which the once-per-fact guard treats as PROVISIONAL — stamping it "
            "would re-open this case on the next sighting instead of disposing "
            "of it"
            if action in repo.PROVISIONAL_DISPOSITIONS
            # The rest shut the key either way, so the dedupe is not the
            # argument — the audit row is. It records who decided what, and a
            # human stamp spelled exactly like the daemon's leaves a later
            # reader unable to tell an operator's attestation from an automatic
            # disposal.
            else "which the sweep writes automatically — a human's attestation "
            "must not be indistinguishable from the daemon's in the audit row"
        )
        print(
            f"error: --action {action!r} is one of the sweep's own "
            f"dispositions, {because}. Describe the disposition in your own words.",
            file=sys.stderr,
        )
        return 1
    # Scoped to THIS run: the id comes off a --status listing, and a typo'd
    # --run-id/--db must name that mistake rather than stamp another run's case.
    match = [
        row
        for row in repo.iter_exchange_reconciliation_events(db.conn, args.run_id)
        if row["event_id"] == args.stamp_case
    ]
    if not match:
        print(
            f"error: run {args.run_id!r} has no reconciliation case with event_id "
            f"{args.stamp_case} — see `safe-mode --status` for the open cases.",
            file=sys.stderr,
        )
        return 1
    (row,) = match
    if row["case_type"] == "fill_unmapped":
        # Stamping this would be a verdict NO-OP that also HIDES the row: an
        # unmapped sighting's block is an anti-join against the fills table
        # (repo.iter_unresolved_fill_sightings) which never reads action_taken,
        # so the pass stays unclean while the operator's only enumeration of the
        # backlog loses the row — the very wedge --stamp-case exists to prevent.
        print(
            f"error: case {args.stamp_case} is a fill_unmapped sighting — the "
            "exchange reported a fill the ledger still lacks, and it resolves by "
            "BOOKING that fill (§8.3 re-ingest / the next backfill), never by "
            "stamping: its block is an anti-join that does not read action_taken, "
            "so a stamp would clear no verdict and only hide the row from --status.",
            file=sys.stderr,
        )
        return 1
    if row["action_taken"] is not None:
        # Append-only in spirit: the first disposition is the audit record, and
        # silently overwriting it would erase what a human already attested.
        print(
            f"error: case {args.stamp_case} ({row['case_type']}) was already "
            f"disposed of as {row['action_taken']!r} — a recorded disposition is "
            "not overwritten.",
            file=sys.stderr,
        )
        return 1
    with db.transaction() as conn:
        # Re-tested INSIDE the write, not just above: this command takes no run
        # lease, so between that read and here the daemon's own reconciliation
        # pass can stamp the machine disposition — and a plain UPDATE would
        # erase what the system recorded it DID, with no error and no audit row
        # (2026-07-30 concurrency review).
        # The STRIPPED value, the same one the machine-vocabulary fence above
        # tested: deciding on one string and storing another would let
        # " backfilled " be refused while "backfilled " was not.
        stamped = repo.stamp_reconciliation_action_if_unset(conn, args.stamp_case, action)
    if not stamped:
        print(
            f"error: case {args.stamp_case} was disposed of by another writer "
            "while this command was running (the live daemon stamps cases its "
            "reconciliation pass resolves) — nothing was overwritten. Re-check "
            "`safe-mode --status` and stamp again only if it is still open.",
            file=sys.stderr,
        )
        return 1
    print(f"case {args.stamp_case} ({row['case_type']}) stamped: {args.action}")
    print(
        "NOTE: trading does not resume yet — the run must pass its next full "
        "reconciliation before new orders are allowed."
    )
    return 0
