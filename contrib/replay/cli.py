"""``python -m contrib.replay`` — the offline exam's commands.

Two commands:

- ``score --db paper_trading.db --run-id paper-BTC-7`` — the scorecard
  (plan PR 1): read the run's decisions, mark each against the price that
  followed, print the summary, and with ``--out DIR`` write one CSV row per
  decision beside a copy of the summary. ``--research-db`` fills a missing
  cycle's later mark from the research store; ``--payload-root`` says where
  the run's payloads are when the store was copied away from its host, so
  the questions that already carry a ``.reports.json`` can be counted;
  ``--holdout`` scores the holdout segment too, and says so loudly. With
  ``--replay-db replay.sqlite --variant NAME`` it scores a variant's
  replayed answers instead of the paper trader's own, one card per repeat,
  and compares them question by question with the paper trader's answers
  (or, with ``--against NAME``, with another variant's).
- ``replay --db paper_trading.db --run-id paper-BTC-6 --variant FILE`` —
  the past papers (plan PR 2): put every question of one segment (train
  unless ``--segment`` says otherwise) to the variant's model ``--repeats``
  times, gate each answer through the run's own gate, and store it in
  ``--replay-db``. Resumable: an answer already stored is never asked for
  again. ``--dry-run`` checks every payload and prints what would be asked,
  without building a client or writing anything.

Exit codes, kept in step with the two neighbouring packages' CLIs: ``0`` the
command did what it says, ``1`` a named operator, store, config, split,
scoring or replay failure (the sentence on stderr says which), ``2``
argparse's own usage errors, ``130`` interrupted.
"""

from __future__ import annotations

import argparse
import csv
import getpass
import sqlite3
import sys
import time
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .compare import Table, compare
from .model import engine_model
from .paper_store import (
    Decisions,
    RunFacts,
    gate_config,
    load_decisions,
    load_research_closes,
    run_facts,
)
from .replay import ReplayError, ask_all, pending, prepare, select
from .replay_store import ReplayStore, ReplayStoreError
from .score import (
    Answer,
    Scorecard,
    ScoreError,
    build_split,
    csv_table,
    score_run,
    segment_of,
)
from .upstream import (
    STUDIED_INTERVALS,
    Database,
    DecisionConfig,
    ResearchStore,
    RiskConfig,
    SchemaVersionError,
    SegmentName,
    Split,
    SplitError,
    StoreError,
    from_epoch_ms,
    payload_dir,
)
from .variant import VariantError, load_variant

__all__ = ["main"]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


# The clock, the pause between retries, and the model factory the ``replay``
# command uses; module attributes so the tests can replace them.
_now: Callable[[], datetime] = _utc_now
_sleep: Callable[[float], None] = time.sleep
_build_model = engine_model


class _Refused(Exception):
    """A named refusal on the way to a command's work: exit 1 with this sentence."""


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m contrib.replay",
        description=(
            "The paper trader's offline exam: score its recorded decisions, and replay them."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    score = subparsers.add_parser(
        "score",
        help="mark one paper run's decisions against the price that followed",
        description=(
            "Read one paper run's decision attempts (each cycle's final input and its "
            "output), mark every decision one bar and six bars on, and print "
            "the scorecard: hit rates by mode, "
            "net P&L under the run's own fill model, fail-closed / clamp / flip rates, "
            "confidence calibration, and three baselines. Never writes to the store."
        ),
    )
    score.add_argument("--db", default="paper_trading.db", help="the paper store (SQLite path)")
    score.add_argument("--run-id", required=True)
    score.add_argument(
        "--research-db",
        metavar="PATH",
        help=(
            "an existing autoresearch.sqlite whose candle closes fill the later mark of a "
            "cycle the run has no row for (api_failed, a restart); the CSV says which "
            "marks came from it"
        ),
    )
    score.add_argument(
        "--payload-root",
        metavar="DIR",
        help=(
            "the run's payload directory, for a store copied away from its host; each "
            "question is then checked for a .reports.json beside its payload by file "
            "name. Without it the daemon's own layout beside the store is used when it "
            "exists, and the count is skipped otherwise."
        ),
    )
    score.add_argument(
        "--out",
        metavar="DIR",
        help=(
            "write <stem>-decisions.csv (one row per decision; per decision and repeat with "
            "--replay-db) and <stem>-summary.txt here; the stem is the run id, or "
            "<run-id>-<variant> with --replay-db"
        ),
    )
    score.add_argument(
        "--holdout",
        action="store_true",
        help=(
            "score the holdout segment too. Off by default: the split is the research "
            "package's 60/20/20, holdout newest, and the holdout is what a variant is "
            "judged on ONCE (plan section 5): every look at it is a look spent."
        ),
    )
    score.add_argument(
        "--replay-db",
        metavar="PATH",
        help=(
            "an existing replay.sqlite: score the answers --variant gave there instead of "
            "the paper trader's own, one card per repeat, and compare them question by "
            "question with the paper trader's answers (or --against's)"
        ),
    )
    score.add_argument("--variant", metavar="NAME", help="with --replay-db: the variant to score")
    score.add_argument(
        "--against",
        metavar="NAME",
        help="with --replay-db: compare with this variant instead of the paper trader",
    )
    score.add_argument(
        "--include-pre-cutoff",
        action="store_true",
        help=(
            "with --replay-db: also score the questions decided on or before the variant's "
            "model_cutoff day, whose later prices the model may have been trained on "
            "(plan section 6); left out by default"
        ),
    )
    score.set_defaults(func=_cmd_score)

    replay = subparsers.add_parser(
        "replay",
        help="put one run's recorded questions to a variant's model, and gate its answers",
        description=(
            "Ask the variant's model each question of one segment of a paper run (the "
            "recorded context and format block, one completion, no analysts or debate), "
            "gate every answer through the run's own gate, and store it in the replay "
            "store. Never writes to the paper store; resumes where it stopped."
        ),
    )
    replay.add_argument("--db", default="paper_trading.db", help="the paper store (SQLite path)")
    replay.add_argument("--run-id", required=True)
    replay.add_argument("--variant", required=True, metavar="FILE", help="the variant YAML file")
    replay.add_argument(
        "--replay-db",
        default="replay.sqlite",
        metavar="PATH",
        help="the replay store; created when it does not exist (default: replay.sqlite)",
    )
    replay.add_argument(
        "--repeats",
        type=int,
        default=3,
        help="answers per question (default: 3, plan section 3-10)",
    )
    replay.add_argument(
        "--segment",
        choices=[name.value for name in SegmentName],
        default=SegmentName.TRAIN.value,
        help="which segment of the run's split to ask (default: train)",
    )
    replay.add_argument(
        "--holdout",
        action="store_true",
        help=(
            "required with --segment holdout, and allowed only with it: asking the holdout "
            "spends its one look, and the ledger records who spent it"
        ),
    )
    replay.add_argument(
        "--payload-root",
        metavar="DIR",
        help=(
            "the run's payload directory; defaults to the daemon's layout beside --db "
            "(payloads/<run-id>/)"
        ),
    )
    replay.add_argument(
        "--limit",
        type=int,
        metavar="N",
        help="store at most N new answers this time (a call tried again counts once)"
    )
    replay.add_argument(
        "--dry-run",
        action="store_true",
        help="check every payload and print what would be asked; no client, no writes",
    )
    replay.set_defaults(func=_cmd_replay)
    return parser


def _fail(message: str) -> int:
    print(f"error: {message}", file=sys.stderr)
    return 1


@dataclass(frozen=True)
class _Run:
    """One paper run, read and cut: what both commands start from.

    ``gate`` is the run's genesis ``risk:`` / ``decision:`` pair, read only
    for the ``replay`` command (the scorecard runs without one).
    """

    facts: RunFacts
    decisions: Decisions
    split: Split
    gate: tuple[RiskConfig, DecisionConfig] | None = None


def _open_run(
    db_path: Path,
    db_arg: str,
    run_id: str,
    *,
    reports_root: Path | None,
    count_reports: bool = True,
    with_gate: bool = False,
    noun: str = "scorecard",
) -> _Run:
    """Read the run's decisions and cut its split; every refusal is a :class:`_Refused`.

    One open of the store for everything a command reads from it.
    ``count_reports`` off skips the ``.reports.json`` lookups a command
    will not print; ``with_gate`` also reads the run's gate. ``noun`` names
    the command in the refusals.
    """
    verb = "scores" if noun == "scorecard" else "reads"
    try:
        db = Database(db_path, migrate=False)
    except SchemaVersionError as exc:
        raise _Refused(str(exc)) from exc
    with db:
        facts = run_facts(db, run_id)
        if facts is None:
            raise _Refused(f"run {run_id!r} not found in {db_arg}")
        if facts.mode != "paper":
            raise _Refused(
                f"run {run_id!r} is a {facts.mode} run; the {noun} reads paper runs "
                "(their fill model is what the costs are taken from)"
            )
        if reports_root is None and count_reports:
            beside = payload_dir(db_path, run_id)
            reports_root = beside if beside.is_dir() else None
        decisions = load_decisions(db, run_id, reports_root=reports_root)
        gate = gate_config(db, run_id) if with_gate else None
    if not decisions.questions:
        raise _Refused(f"run {run_id!r} has no decision attempt with an input row to score")
    if facts.interval not in STUDIED_INTERVALS:
        # Said before the split is cut: the split would refuse the interval
        # too, but in a sentence about the run being too short.
        raise _Refused(
            f"run {run_id!r} was traded on {facts.interval} candles; the {noun} {verb} "
            f"runs on {' / '.join(STUDIED_INTERVALS)} candles (the research split's intervals)"
        )
    try:
        split = build_split(decisions.questions, interval=facts.interval, step_ms=facts.step_ms)
    except SplitError as exc:
        raise _Refused(
            f"run {run_id!r} is too short to cut into train / validation / holdout "
            f"({exc}); the {noun} needs at least four {facts.interval} bars"
        ) from exc
    return _Run(facts, decisions, split, gate)


def _who() -> str:
    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001 - getuser raises whatever the platform lookup raises
        return "unknown"


# -- score ---------------------------------------------------------------------


def _cmd_score(args: argparse.Namespace) -> int:
    db_path = Path(args.db)
    if not db_path.is_file():
        return _fail(f"database {args.db!r} does not exist")
    reports_root: Path | None = None
    if args.payload_root is not None:
        if not args.payload_root:
            return _fail("--payload-root needs a directory, got ''")
        reports_root = Path(args.payload_root)
        if not reports_root.is_dir():
            return _fail(f"--payload-root {args.payload_root!r} is not a directory")
    if args.out is not None and not args.out:
        # ``Path("")`` is the working directory; an unset shell variable
        # must not quietly write there. Refused up front, like the other
        # flags, not after the card has been printed.
        return _fail("--out needs a directory, got ''")
    research_path: Path | None = None
    if args.research_db is not None:
        research_path = Path(args.research_db)
        if not research_path.is_file():
            # Checked before the store is opened: opening a path that does not
            # exist would CREATE an empty research store there.
            return _fail(f"--research-db {args.research_db!r} does not exist")
    replay_path: Path | None = None
    if args.replay_db is None:
        stray = [
            flag
            for flag, given in (
                ("--variant", args.variant is not None),
                ("--against", args.against is not None),
                ("--include-pre-cutoff", args.include_pre_cutoff),
            )
            if given
        ]
        if stray:
            return _fail(f"{', '.join(stray)} only apply with --replay-db")
    else:
        replay_path = Path(args.replay_db)
        if not replay_path.is_file():
            # The replay store would create an empty file at a mistyped path.
            return _fail(f"--replay-db {args.replay_db!r} does not exist")
        if not args.variant:
            return _fail("--replay-db needs --variant NAME: the variant whose answers to score")
        if args.against == args.variant:
            return _fail("--against names the variant being scored; compare it with another")
    run = _open_run(db_path, args.db, args.run_id, reports_root=reports_root)
    facts, split = run.facts, run.split
    research: Mapping[int, float] = {}
    if research_path is not None:
        try:
            with ResearchStore(research_path) as store:
                # Only the bars this run can pair: the lock at the I/O seam,
                # so a locked run never reads a holdout bar off disk. The
                # first question is decided at or after the train start, so
                # no bar opening before it is within a half-bar tolerance of
                # any later mark it wants.
                window = (split.train.start_ms, split.loadable_until(holdout=args.holdout))
                research = load_research_closes(
                    store,
                    coin=facts.coin,
                    interval=facts.interval,
                    since_ms=window[0],
                    until_ms=window[1],
                )
        except StoreError as exc:
            return _fail(str(exc))
        if not research:
            print(
                f"warning: --research-db {args.research_db} holds no {facts.coin} "
                f"{facts.interval} candles opening between {from_epoch_ms(window[0]):%Y-%m-%d %H:%M} "
                f"and {from_epoch_ms(window[1]):%Y-%m-%d %H:%M}; no missing cycle can be filled "
                "from it",
                file=sys.stderr,
            )
    header = (
        f"scorecard: run {facts.run_id} ({facts.coin}, {facts.interval} cycle; costs and "
        f"interval from {facts.describe_source()})"
    )

    def card(answers: Sequence[Answer], only: Collection[str] | None = None) -> Scorecard:
        return score_run(
            run.decisions.questions,
            answers,
            step_ms=facts.step_ms,
            costs=facts.costs,
            research_closes=research,
            split=split,
            holdout=args.holdout,
            only=only,
        )

    if replay_path is not None:
        lines, table = _score_replay(args, run, replay_path, header, card)
        stem = f"{facts.run_id}-{args.variant}"
    else:
        paper = card(run.decisions.answers)
        lines = [header, *run.decisions.describe(), *paper.summary().describe(paper)]
        table = csv_table(paper)
        stem = facts.run_id
    for line in lines:
        print(line)
    if args.out is not None:
        return _write_out(Path(args.out), args.out, stem, lines, table)
    return 0


def _score_replay(
    args: argparse.Namespace,
    run: _Run,
    replay_path: Path,
    header: str,
    card: Callable[[Sequence[Answer], Collection[str]], Scorecard],
) -> tuple[list[str], Table]:
    """Read one variant's answers (and ``--against``'s) and report them through :func:`compare`.

    With ``--holdout`` the look is recorded before any holdout answer is
    scored, and the earlier looks at this run are listed.
    """
    run_id = run.facts.run_id
    questions = run.decisions.questions
    try:
        with ReplayStore(replay_path) as store:
            variant = store.variant(args.variant)
            against = None if args.against is None else store.variant(args.against)
            looks = store.looks(run_id)
            if args.holdout:
                # Written before a holdout answer is scored: the look is
                # spent whether or not what follows succeeds.
                store.record_look(
                    action="score",
                    run_id=run_id,
                    variant_sha=variant.sha,
                    questions=sum(
                        1
                        for q in questions
                        if segment_of(run.split, q.at_ms, run.facts.step_ms)
                        is SegmentName.HOLDOUT
                    ),
                    who=_who(),
                    now=_now(),
                )
            answers = store.answers(variant.sha, run_id)
            against_answers = None if against is None else store.answers(against.sha, run_id)
    except ReplayStoreError as exc:
        raise _Refused(str(exc)) from exc
    if not answers:
        raise _Refused(
            f"variant {variant.name!r} has no answers for run {run_id!r} in {replay_path}"
        )
    if against is not None and not against_answers:
        raise _Refused(
            f"variant {against.name!r} has no answers for run {run_id!r} to compare with"
        )
    report = compare(
        questions=questions,
        card=card,
        variant=variant,
        answers=answers,
        paper_answers=run.decisions.answers,
        against=against,
        against_answers=against_answers,
        include_pre_cutoff=args.include_pre_cutoff,
    )
    seen: list[str] = []
    if args.holdout:
        seen.append(f"holdout looks recorded for this run before this one: {len(looks)}")
        seen.extend(
            f"  {look.at} {look.who} {look.action} {look.variant_name} "
            f"({look.questions} question(s))"
            for look in looks
        )
    return [header, *report.preamble, *seen, *report.body], report.table


def _write_out(out: Path, out_arg: str, stem: str, lines: Sequence[str], table: Table) -> int:
    decisions_csv = out / f"{stem}-decisions.csv"
    summary = out / f"{stem}-summary.txt"
    try:
        out.mkdir(parents=True, exist_ok=True)
        with decisions_csv.open("w", encoding="utf-8", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(table[0])
            writer.writerows(table[1])
        summary.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError as exc:
        return _fail(f"could not write under --out {out_arg!r}: {exc}")
    for path in (decisions_csv, summary):
        print(f"wrote {path}", file=sys.stderr)
    return 0


# -- replay --------------------------------------------------------------------


def _cmd_replay(args: argparse.Namespace) -> int:
    db_path = Path(args.db)
    if not db_path.is_file():
        return _fail(f"database {args.db!r} does not exist")
    if args.repeats < 1:
        return _fail(f"--repeats must be at least 1, got {args.repeats}")
    if args.limit is not None and args.limit < 1:
        return _fail(f"--limit must be at least 1, got {args.limit}")
    segment = SegmentName(args.segment)
    if (segment is SegmentName.HOLDOUT) != args.holdout:
        return _fail(
            "--segment holdout needs --holdout, and --holdout goes only with --segment "
            "holdout: asking the holdout spends its one look (plan section 5)"
        )
    if args.holdout and args.dry_run:
        # A dry run reads every payload it checks, and writes nothing, not
        # even a ledger row: on the holdout it would be an unrecorded look.
        return _fail("--dry-run reads the payloads it checks; it does not run on the holdout")
    if args.payload_root is not None and not args.payload_root:
        return _fail("--payload-root needs a directory, got ''")
    if not args.replay_db:
        return _fail("--replay-db needs a path, got ''")
    try:
        variant = load_variant(Path(args.variant))
    except VariantError as exc:
        return _fail(str(exc))
    run = _open_run(
        db_path,
        args.db,
        args.run_id,
        reports_root=None,
        count_reports=False,
        with_gate=True,
        noun="replay",
    )
    assert run.gate is not None
    risk, decision = run.gate
    payload_root = (
        Path(args.payload_root) if args.payload_root else payload_dir(db_path, args.run_id)
    )
    if not payload_root.is_dir():
        return _fail(
            f"no payload directory at {str(payload_root)!r}; pass --payload-root DIR with the "
            f"run's payloads (the daemon writes them to payloads/{args.run_id}/ beside its store)"
        )
    papers = select(
        run.decisions.questions,
        run.decisions.inputs,
        split=run.split,
        step_ms=run.facts.step_ms,
        segments={segment},
    )
    if not papers:
        return _fail(f"run {args.run_id!r} has no question in its {segment.value} segment")
    header = [
        f"replay: run {run.facts.run_id} ({run.facts.coin}, {run.facts.interval} cycle), "
        f"{segment.value} segment: {len(papers)} question(s) x {args.repeats} repeat(s)",
        variant.describe(),
        f"gate: the run's genesis risk/decision blocks (leverage {risk.leverage}, max target "
        f"margin {risk.max_target_margin_pct}%, deadband {decision.rebalance_deadband_pct}%, "
        f"min_confidence {decision.min_confidence}, resize_min_confidence "
        f"{decision.resize_min_confidence})",
    ]
    replay_path = Path(args.replay_db)
    if args.dry_run:
        prepared = prepare(papers, payload_root=payload_root, risk=risk)
        stored: set[tuple[str, int]] = set()
        if replay_path.is_file():
            try:
                with ReplayStore(replay_path) as store:
                    stored = store.answered(variant.sha, args.run_id)
            except ReplayStoreError as exc:
                return _fail(str(exc))
        todo = pending(prepared, answered=stored, repeats=args.repeats)
        asks = len(todo) if args.limit is None else min(len(todo), args.limit)
        unchecked = sum(1 for paper in papers if paper.facts.payload_hash is None)
        for line in header:
            print(line)
        print(
            f"payloads read: {len(prepared)}, each checked against its input row's digest where "
            "one was recorded"
            + (f" ({unchecked} recorded none and were read unchecked)" if unchecked else "")
        )
        print(
            f"dry run: {len(prepared) * args.repeats - len(todo)} answer(s) already stored, "
            f"{len(todo)} to ask; this command would ask for {asks} of them"
        )
        return 0
    try:
        # Built before the replay store is written or any payload is read: a
        # client that cannot be built must not have spent a holdout look.
        model = _build_model(variant)
    except (ImportError, ValueError) as exc:
        return _fail(f"the {variant.provider}/{variant.model} client could not be built: {exc}")
    try:
        with ReplayStore(replay_path, create=True) as store:
            for note in store.register(variant, now=_now()):
                print(f"note: {note}", file=sys.stderr)
            if segment is SegmentName.HOLDOUT:
                # Before the first holdout payload is opened (plan section 3-9).
                store.record_look(
                    action="ask",
                    run_id=args.run_id,
                    variant_sha=variant.sha,
                    questions=len(papers),
                    who=_who(),
                    now=_now(),
                )
            prepared = prepare(papers, payload_root=payload_root, risk=risk)
            for line in header:
                print(line)
            report = ask_all(
                prepared,
                variant=variant,
                model=model,
                store=store,
                run_id=args.run_id,
                repeats=args.repeats,
                risk=risk,
                decision=decision,
                now=_now,
                sleep=_sleep,
                limit=args.limit,
                progress=lambda line: print(line, file=sys.stderr),
            )
    except ReplayStoreError as exc:
        return _fail(str(exc))
    for line in report.describe():
        print(line)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        return 130
    except (_Refused, ScoreError, ReplayError) as exc:
        return _fail(str(exc))
    except sqlite3.Error as exc:
        return _fail(f"store read failed: {exc}")
