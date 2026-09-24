"""``python -m contrib.replay`` — the offline exam's commands.

Four commands:

- ``score --db paper_trading.db --run-id paper-BTC-7`` — the scorecard
  (plan PR 1): read the run's decisions, mark each against the price that
  followed, print the summary, and with ``--out DIR`` write one CSV row per
  decision beside a copy of the summary. ``--research-db`` fills a missing
  cycle's later mark from the research store; ``--payload-root`` says where
  the run's payloads are when the store was copied away from its host, so
  the questions that already carry a ``.reports.json`` can be counted;
  ``--holdout`` scores the holdout segment too, and says so loudly; it
  needs ``--replay-db``, whose ledger records the look. With
  ``--replay-db replay.sqlite --variant NAME`` it scores a variant's
  replayed answers instead of the paper trader's own, one card per repeat,
  and compares them question by question with the paper trader's answers
  (or, with ``--against NAME``, with another variant's). With a replay
  store, the run is scored under the split pinned there (or, if none is
  pinned yet, as the run stands). A variant asked the direction probe gets
  one more section per probe (plan PR 2.1, :mod:`.probe_score`); a
  variant asked only the probe gets that section alone.
- ``replay --db paper_trading.db --run-id paper-BTC-6 --variant FILE`` —
  the past papers (plan PR 2): put every question of one segment (train
  unless ``--segment`` says otherwise) to the variant's model ``--repeats``
  times, gate each answer through the run's own gate, and store it in
  ``--replay-db``. Resumable: an answer already stored is never asked for
  again. The first replay of a run (or the first ``score --holdout`` look)
  pins its split in the store (a replay only once its payload checks
  have passed).
  ``--dry-run`` checks every payload and prints what would be asked,
  without building a client or writing anything. With ``--probe FILE``
  the same questions are put to the direction probe instead of asked for
  a decision (plan PR 2.1, :mod:`.probe`), under the same discipline.
- ``register --variant FILE`` — store a variant, or correct its
  ``model_cutoff``, without asking anything.
- ``pool --run-id A --run-id B ... --replay-db PATH --variant NAME --probe
  NAME`` — the direction probe pooled over several runs' validation
  segments (plan PR 2.2): the headline skill per horizon with a 90%
  block-bootstrap interval, and the plan section 5 bar. Reads only.

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
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from .compare import Table, compare, cutoff_scope
from .model import engine_model
from .paper_store import (
    Decisions,
    RunFacts,
    gate_config,
    load_decisions,
    load_research_closes,
    run_facts,
)
from .pool import BLOCK, DRAWS, RunScores, describe_pool
from .probe import PROBE_KEYS, PROBE_STEP_MS, Probe, ProbeAnswer, ProbeError, load_probe
from .probe_score import describe_probe, headline_scores
from .replay import (
    ProbeReport,
    ReplayError,
    ReplayReport,
    ask_all,
    ask_probes,
    inside,
    pending,
    prepare,
    select,
)
from .replay_store import HoldoutLook, ReplayStore, ReplayStoreError
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
from .variant import Variant, VariantError, load_variant

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
            "--replay-db, and not written for a variant with probe answers only) and "
            "<stem>-summary.txt here; the stem is the run id, or <run-id>-<variant> with "
            "--replay-db"
        ),
    )
    score.add_argument(
        "--holdout",
        action="store_true",
        help=(
            "score the holdout segment too. Off by default: the split is the research "
            "package's 60/20/20, holdout newest, and the holdout is what a variant is "
            "judged on ONCE (plan section 5): every look at it is a look spent, so it needs "
            "--replay-db, whose ledger records the look (the store is created there for a "
            "look at the paper trader's own answers)."
        ),
    )
    score.add_argument(
        "--replay-db",
        metavar="PATH",
        help=(
            "a replay.sqlite: the run is scored under the split pinned there (or, if none "
            "is pinned yet, as the run stands); with --variant, "
            "the answers that variant gave are scored instead of the paper trader's own, one "
            "card per repeat, and compared question by question with the paper trader's "
            "answers (or --against's)"
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
            "with --variant: also score the questions decided on or before the variant's "
            "model_cutoff day (with --against, the later of the two), whose later prices the "
            "model may have been trained on (plan section 6); left out by default, and a "
            "variant with no model_cutoff is refused without this flag"
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
        help=(
            "store at most N new answers or refusals this time (a call tried again counts once)"
        ),
    )
    replay.add_argument(
        "--retry-failed",
        action="store_true",
        help=(
            "ask again the questions the provider refused for their own sake on an earlier "
            "run of this command (they are otherwise recorded as unanswered and skipped)"
        ),
    )
    replay.add_argument(
        "--dry-run",
        action="store_true",
        help="check every payload and print what would be asked; no client, no writes",
    )
    replay.add_argument(
        "--probe",
        metavar="FILE",
        help=(
            "ask the direction probe in this YAML file instead of a decision (plan PR 2.1): "
            "one more completion per question and repeat for up / down / flat probabilities "
            "at 4h and 24h, stored beside the variant's answers and scored by score "
            "--replay-db; only on runs traded on 4h candles"
        ),
    )
    replay.set_defaults(func=_cmd_replay)

    pool = subparsers.add_parser(
        "pool",
        help="the direction probe pooled over several runs, with a block-bootstrap interval",
        description=(
            "Pool one variant's answers to one direction probe over the validation segments "
            "of several paper runs, each cut by the split pinned for it in the replay store, "
            "and print the headline Brier skill score per horizon with a 90 percent "
            "block-bootstrap interval, and whether the plan section 5 bar (4h, lower end "
            "above 0) is met. Reads only; the holdout is never read."
        ),
    )
    pool.add_argument("--db", default="paper_trading.db", help="the paper store (SQLite path)")
    pool.add_argument(
        "--run-id",
        action="append",
        required=True,
        help="a run to pool; give it once per run",
    )
    pool.add_argument("--replay-db", required=True, metavar="PATH", help="the replay store")
    pool.add_argument("--variant", required=True, metavar="NAME", help="the variant to score")
    pool.add_argument("--probe", required=True, metavar="NAME", help="the probe, by name")
    pool.add_argument(
        "--research-db",
        metavar="PATH",
        help="an existing autoresearch.sqlite whose closes fill a missing cycle's later mark",
    )
    pool.add_argument(
        "--include-pre-cutoff",
        action="store_true",
        help=(
            "also pool the questions decided on or before the variant's model_cutoff day; a "
            "variant with no model_cutoff is refused without this flag"
        ),
    )
    pool.add_argument(
        "--block",
        type=int,
        default=BLOCK,
        help=f"consecutive questions per bootstrap block, within a run (default: {BLOCK})",
    )
    pool.add_argument(
        "--draws", type=int, default=DRAWS, help=f"bootstrap draws (default: {DRAWS})"
    )
    pool.add_argument("--seed", type=int, default=0, help="the bootstrap's seed (default: 0)")
    pool.set_defaults(func=_cmd_pool)

    register = subparsers.add_parser(
        "register",
        help="store a variant, or correct its model_cutoff, without asking anything",
        description=(
            "Store the variant in the replay store (created if missing), or correct the "
            "model_cutoff of the variant already stored under its name. Asks no model."
        ),
    )
    register.add_argument("--variant", required=True, metavar="FILE", help="the variant YAML file")
    register.add_argument(
        "--replay-db",
        default="replay.sqlite",
        metavar="PATH",
        help="the replay store; created when it does not exist (default: replay.sqlite)",
    )
    register.set_defaults(func=_cmd_register)
    return parser


def _progress(line: str) -> None:
    print(line, file=sys.stderr)


def _fail(message: str) -> int:
    print(f"error: {message}", file=sys.stderr)
    return 1


@dataclass(frozen=True)
class _Run:
    """One paper run, read and cut: what both commands start from.

    ``gate`` is the run's genesis ``risk:`` / ``decision:`` pair, read only
    for the ``replay`` command (the scorecard runs without one). Once the
    run is put under a pinned split (:func:`_pinned`), ``pinned_at`` says
    when it was pinned and ``after_pin`` how many questions the run gained
    after the pinned end, which the exam leaves out.
    """

    facts: RunFacts
    decisions: Decisions
    split: Split
    gate: tuple[RiskConfig, DecisionConfig] | None = None
    pinned_at: str | None = None
    after_pin: int = 0

    def split_line(self, store: Path) -> str:
        """Where the split comes from, for a report that used a replay store."""
        if self.pinned_at is None:
            return (
                f"split: not pinned in {store} yet; the first replay of this run (or the first "
                "score --holdout) pins it"
            )
        return f"split: pinned {self.pinned_at} in {store}" + (
            f"; {self.after_pin} question(s) decided after its end are not part of this exam"
            if self.after_pin
            else ""
        )


def _pinned(run: _Run, split: Split, pinned_at: str) -> _Run:
    """``run`` under the pinned ``split``: the questions after its end left out, with their answers."""
    try:
        questions, after = inside(run.decisions.questions, split=split, step_ms=run.facts.step_ms)
    except ReplayError as exc:
        raise _Refused(str(exc)) from exc
    kept = {q.input_id for q in questions}
    decisions = replace(
        run.decisions,
        questions=questions,
        answers=[a for a in run.decisions.answers if a.input_id in kept],
    )
    return replace(run, decisions=decisions, split=split, pinned_at=pinned_at, after_pin=after)


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
    the command in the refusals. The split is cut over the run as it stands;
    a command with a replay store puts the run under the split pinned there.
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


def _holdout_questions(run: _Run) -> int:
    return sum(
        1
        for q in run.decisions.questions
        if segment_of(run.split, q.at_ms, run.facts.step_ms) is SegmentName.HOLDOUT
    )


# -- score ---------------------------------------------------------------------


@dataclass(frozen=True)
class _Replayed:
    """What ``score`` read from a replay store: the variants, their answers and failures, the looks.

    ``probes`` is every direction probe the scored variant was asked on the
    run, with its answers by repeat.
    """

    variant: Variant | None
    against: Variant | None
    answers: Mapping[int, Sequence[Answer]]
    failed: Mapping[int, Collection[str]]
    against_answers: Mapping[int, Sequence[Answer]] | None
    against_failed: Mapping[int, Collection[str]] | None
    looks: Sequence[HoldoutLook]
    probes: Sequence[tuple[Probe, Mapping[int, Sequence[ProbeAnswer]]]] = ()


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
        if args.holdout:
            return _fail(
                "--holdout needs --replay-db PATH: every look at the holdout is recorded in "
                "its ledger (plan section 5); the store is created there if it does not exist"
            )
    else:
        if not args.replay_db:
            return _fail("--replay-db needs a path, got ''")
        replay_path = Path(args.replay_db)
        if args.variant is None:
            stray = [
                flag
                for flag, given in (
                    ("--against", args.against is not None),
                    ("--include-pre-cutoff", args.include_pre_cutoff),
                )
                if given
            ]
            if stray:
                return _fail(f"{', '.join(stray)} only apply with --variant")
        elif args.against == args.variant:
            return _fail("--against names the variant being scored; compare it with another")
        # Only a look at the paper trader's own holdout may create the store:
        # it has a look to record. A reader never creates one behind a typo.
        creates = args.holdout and args.variant is None
        if not replay_path.is_file() and not creates:
            return _fail(f"--replay-db {args.replay_db!r} does not exist")
    run = _open_run(db_path, args.db, args.run_id, reports_root=reports_root)
    replayed: _Replayed | None = None
    if replay_path is not None:
        run, replayed = _read_replay_store(args, run, replay_path)
    facts, split = run.facts, run.split
    research: Mapping[int, float] = {}
    if research_path is not None:
        try:
            research = _research_closes(
                research_path, args.research_db, facts, split, holdout=args.holdout
            )
        except StoreError as exc:
            return _fail(str(exc))
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

    before: list[str] = []
    if replay_path is not None:
        before.append(run.split_line(replay_path))
    if replayed is not None and args.holdout:
        before.append(f"holdout looks recorded for this run before this one: {len(replayed.looks)}")
        before.extend(
            f"  {look.at} {look.who} {look.action} {look.variant_name} "
            f"({look.questions} question(s))"
            for look in replayed.looks
        )
    table: Table | None
    if replayed is not None and replayed.variant is not None:
        scope = cutoff_scope(
            run.decisions.questions,
            replayed.variant,
            replayed.against,
            include_pre_cutoff=args.include_pre_cutoff,
        )
        body: list[str] = []
        table = None
        if replayed.answers or replayed.failed:
            report = compare(
                questions=run.decisions.questions,
                card=card,
                variant=replayed.variant,
                answers=replayed.answers,
                paper_answers=run.decisions.answers,
                against=replayed.against,
                against_answers=replayed.against_answers,
                include_pre_cutoff=args.include_pre_cutoff,
                failed=replayed.failed,
                against_failed=replayed.against_failed,
            )
            body.extend(report.body)
            table = report.table
        if replayed.probes:
            # Every question the card keeps, none answered: the base rate
            # reads every train question, whoever was asked it.
            whole = card([])
            for probe, probe_answers in replayed.probes:
                body.extend(
                    describe_probe(
                        card=whole, probe=probe, answers=probe_answers, eligible=scope.eligible
                    )
                )
        lines = [header, *scope.preamble, *before, *body]
        stem = f"{facts.run_id}-{args.variant}"
    else:
        paper = card(run.decisions.answers)
        lines = [header, *before, *run.decisions.describe(), *paper.summary().describe(paper)]
        table = csv_table(paper)
        stem = facts.run_id
    for line in lines:
        print(line)
    if args.out is not None:
        return _write_out(Path(args.out), args.out, stem, lines, table)
    return 0


def _research_closes(
    path: Path, path_arg: str, facts: RunFacts, split: Split, *, holdout: bool
) -> Mapping[int, float]:
    """The research store's closes this run can pair, and a warning when there are none.

    Only the bars this run can pair: the lock at the I/O seam, so a locked
    run never reads a holdout bar off disk. The first question is decided at
    or after the train start, so no bar opening before it is within a
    half-bar tolerance of any later mark it wants. Raises ``StoreError``.
    """
    window = (split.train.start_ms, split.loadable_until(holdout=holdout))
    with ResearchStore(path) as store:
        closes = load_research_closes(
            store,
            coin=facts.coin,
            interval=facts.interval,
            since_ms=window[0],
            until_ms=window[1],
        )
    if not closes:
        print(
            f"warning: --research-db {path_arg} holds no {facts.coin} "
            f"{facts.interval} candles opening between {from_epoch_ms(window[0]):%Y-%m-%d %H:%M} "
            f"and {from_epoch_ms(window[1]):%Y-%m-%d %H:%M}; no missing cycle of run "
            f"{facts.run_id} can be filled from it",
            file=sys.stderr,
        )
    return closes


def _read_replay_store(
    args: argparse.Namespace, run: _Run, replay_path: Path
) -> tuple[_Run, _Replayed]:
    """The run under the store's pinned split, and what ``score`` reads from the store.

    With ``--holdout`` the split is pinned now if it was not (a look fixes
    the exam as much as a replay does), and the look is recorded before any
    holdout answer is scored: spent whether or not what follows succeeds.
    With ``--variant``, a variant (or ``--against``) with no ``model_cutoff``
    is refused unless ``--include-pre-cutoff`` says to score every question
    (decided 2026-09-24: the plan's one guard against scoring a model on
    prices it may have been trained on fails closed).
    """
    run_id = run.facts.run_id
    try:
        with ReplayStore(replay_path, create=args.holdout and args.variant is None) as store:
            variant = None if args.variant is None else store.variant(args.variant)
            against = None if args.against is None else store.variant(args.against)
            compared = [v for v in (variant, against) if v is not None]
            missing = [v.name for v in compared if v.model_cutoff is None]
            if missing and not args.include_pre_cutoff:
                raise _Refused(
                    f"{', '.join(repr(n) for n in missing)} record(s) no model_cutoff, so the "
                    "questions the model may have been trained on cannot be left out (plan "
                    "section 6): set model_cutoff in the variant file and run `register`, or "
                    "pass --include-pre-cutoff to score every question"
                )
            pinned = store.pinned_split(run_id)
            if pinned is None and args.holdout:
                pinned = store.pin_split(run_id, run.split, now=_now())
            if pinned is not None:
                run = _pinned(run, *pinned)
            looks = store.looks(run_id)
            if args.holdout:
                store.record_look(
                    action="score",
                    run_id=run_id,
                    variant_sha=None if variant is None else variant.sha,
                    questions=_holdout_questions(run),
                    who=_who(),
                    now=_now(),
                )
            answers = {} if variant is None else store.answers(variant.sha, run_id)
            failed = {} if variant is None else store.failed(variant.sha, run_id)
            against_answers = None if against is None else store.answers(against.sha, run_id)
            against_failed = None if against is None else store.failed(against.sha, run_id)
            probes = [] if variant is None else store.probe_answers(variant.sha, run_id)
    except ReplayStoreError as exc:
        raise _Refused(str(exc)) from exc
    decided = bool(answers or failed)
    if variant is not None and not decided and not probes:
        raise _Refused(
            f"variant {variant.name!r} has no answers for run {run_id!r} in {replay_path}"
        )
    if against is not None and not decided:
        raise _Refused(
            f"--against compares decisions, and variant {args.variant!r} was asked only the "
            f"direction probe on run {run_id!r}"
        )
    if against is not None and not against_answers and not against_failed:
        raise _Refused(
            f"variant {against.name!r} has no answers for run {run_id!r} to compare with"
        )
    return run, _Replayed(
        variant, against, answers, failed, against_answers, against_failed, looks, probes
    )


def _write_out(
    out: Path, out_arg: str, stem: str, lines: Sequence[str], table: Table | None
) -> int:
    """The summary, and the decisions CSV unless there are no decisions (a probe-only variant)."""
    decisions_csv = out / f"{stem}-decisions.csv"
    summary = out / f"{stem}-summary.txt"
    written = []
    try:
        out.mkdir(parents=True, exist_ok=True)
        if table is not None:
            with decisions_csv.open("w", encoding="utf-8", newline="") as fh:
                writer = csv.writer(fh)
                writer.writerow(table[0])
                writer.writerows(table[1])
            written.append(decisions_csv)
        summary.write_text("\n".join(lines) + "\n", encoding="utf-8")
        written.append(summary)
    except OSError as exc:
        return _fail(f"could not write under --out {out_arg!r}: {exc}")
    for path in written:
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
    if args.probe is not None and not args.probe:
        return _fail("--probe needs a file, got ''")
    try:
        variant = load_variant(Path(args.variant))
        probe = None if args.probe is None else load_probe(Path(args.probe))
    except (VariantError, ProbeError) as exc:
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
    if probe is not None and run.facts.step_ms != PROBE_STEP_MS:
        return _fail(
            f"run {args.run_id!r} was traded on {run.facts.interval} candles; the direction "
            "probe asks for 4h and 24h, the scorecard's horizons on a 4h run only"
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
    replay_path = Path(args.replay_db)
    if args.dry_run:
        return _dry_run(args, run, variant, probe, segment, payload_root, replay_path)
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
            if probe is not None:
                store.register_probe(probe, now=_now())
            # The split pinned for the run if there is one; otherwise the one
            # it would pin, which is pinned below only once every check has
            # passed: a replay refused on the way (no question in the segment,
            # a payload missing) must not freeze the exam.
            pinned = store.pinned_split(args.run_id)
            if pinned is not None:
                run = _pinned(run, *pinned)
            papers = select(
                run.decisions.questions,
                run.decisions.inputs,
                split=run.split,
                step_ms=run.facts.step_ms,
                segments={segment},
            )
            if not papers:
                return _fail(f"run {args.run_id!r} has no question in its {segment.value} segment")
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
            if pinned is None:
                stood, pinned_at = store.pin_split(args.run_id, run.split, now=_now())
                if stood != run.split:
                    # Another command pinned the run between the read above
                    # and here: the questions selected belong to a split that
                    # does not stand.
                    return _fail(
                        f"run {args.run_id!r} had its split pinned by another command while "
                        "this one ran; run it again to replay under that split"
                    )
                run = _pinned(run, stood, pinned_at)
            if args.retry_failed:
                cleared = (
                    store.clear_failures(variant.sha, args.run_id)
                    if probe is None
                    else store.clear_probe_refusals(variant.sha, probe.sha, args.run_id)
                )
                print(f"note: {cleared} refused question(s) will be asked again", file=sys.stderr)
            for line in _replay_header(run, variant, probe, segment, len(papers), args.repeats):
                print(line)
            print(run.split_line(replay_path))
            if probe is None:
                report: ReplayReport | ProbeReport = ask_all(
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
                    progress=_progress,
                )
            else:
                report = ask_probes(
                    prepared,
                    variant=variant,
                    probe=probe,
                    model=model,
                    store=store,
                    run_id=args.run_id,
                    repeats=args.repeats,
                    now=_now,
                    sleep=_sleep,
                    limit=args.limit,
                    progress=_progress,
                )
    except ReplayStoreError as exc:
        return _fail(str(exc))
    for line in report.describe():
        print(line)
    return 0


def _replay_header(
    run: _Run,
    variant: Variant,
    probe: Probe | None,
    segment: SegmentName,
    questions: int,
    repeats: int,
) -> list[str]:
    assert run.gate is not None
    risk, decision = run.gate
    lines = [
        f"replay: run {run.facts.run_id} ({run.facts.coin}, {run.facts.interval} cycle), "
        f"{segment.value} segment: {questions} question(s) x {repeats} repeat(s)",
        variant.describe(),
    ]
    if probe is not None:
        lines.append(
            f"{probe.describe()}: asked instead of a decision, with the variant's model; no gate"
        )
        return lines
    lines.append(
        f"gate: the run's genesis risk/decision blocks (leverage {risk.leverage}, max target "
        f"margin {risk.max_target_margin_pct}%, deadband {decision.rebalance_deadband_pct}%, "
        f"min_confidence {decision.min_confidence}, resize_min_confidence "
        f"{decision.resize_min_confidence})"
    )
    return lines


def _dry_run(
    args: argparse.Namespace,
    run: _Run,
    variant: Variant,
    probe: Probe | None,
    segment: SegmentName,
    payload_root: Path,
    replay_path: Path,
) -> int:
    """What ``replay`` would ask, under the split it would use; builds no client, writes nothing."""
    stored: set[tuple[str, int]] = set()
    refused: set[tuple[str, int]] = set()
    if replay_path.is_file():
        try:
            with ReplayStore(replay_path) as store:
                pinned = store.pinned_split(args.run_id)
                if pinned is not None:
                    run = _pinned(run, *pinned)
                if probe is not None:
                    stored, refused = store.probe_done(variant.sha, probe.sha, args.run_id)
                else:
                    stored = store.answered(variant.sha, args.run_id)
                    refused = {
                        (input_id, repeat)
                        for repeat, input_ids in store.failed(variant.sha, args.run_id).items()
                        for input_id in input_ids
                    }
        except ReplayStoreError as exc:
            return _fail(str(exc))
    papers = select(
        run.decisions.questions,
        run.decisions.inputs,
        split=run.split,
        step_ms=run.facts.step_ms,
        segments={segment},
    )
    if not papers:
        return _fail(f"run {args.run_id!r} has no question in its {segment.value} segment")
    assert run.gate is not None
    prepared = prepare(papers, payload_root=payload_root, risk=run.gate[0])
    skip = stored if args.retry_failed else stored | refused
    todo = pending(prepared, answered=skip, repeats=args.repeats)
    asks = len(todo) if args.limit is None else min(len(todo), args.limit)
    unchecked = sum(1 for paper in papers if paper.facts.payload_hash is None)
    for line in _replay_header(run, variant, probe, segment, len(papers), args.repeats):
        print(line)
    print(run.split_line(replay_path))
    if run.pinned_at is None:
        for line in run.split.describe():
            print(f"  {line}")
    print(
        f"payloads read: {len(prepared)}, each checked against its input row's digest where "
        "one was recorded"
        + (f" ({unchecked} recorded none and were read unchecked)" if unchecked else "")
    )
    wanted = {(item.input_id, repeat) for item in prepared for repeat in range(args.repeats)}
    if refused & wanted and not args.retry_failed:
        print(
            f"refused earlier and not asked again: {len(refused & wanted)} "
            "(--retry-failed asks them again)"
        )
    print(
        f"dry run: {len(wanted & stored)} answer(s) already stored, "
        f"{len(todo)} to ask; this command would ask for {asks} of them"
    )
    return 0


# -- pool --------------------------------------------------------------------------


def _cmd_pool(args: argparse.Namespace) -> int:
    """The direction probe pooled over several runs' validation segments (plan PR 2.2)."""
    db_path = Path(args.db)
    if not db_path.is_file():
        return _fail(f"database {args.db!r} does not exist")
    if args.block < 1:
        return _fail(f"--block must be at least 1, got {args.block}")
    if args.draws < 1:
        return _fail(f"--draws must be at least 1, got {args.draws}")
    repeated = sorted({r for r in args.run_id if args.run_id.count(r) > 1})
    if repeated:
        return _fail(f"--run-id names {', '.join(repeated)} more than once")
    replay_path = Path(args.replay_db)
    if not replay_path.is_file():
        # A reader never creates the store behind a typo.
        return _fail(f"--replay-db {args.replay_db!r} does not exist")
    research_path: Path | None = None
    if args.research_db is not None:
        research_path = Path(args.research_db)
        if not research_path.is_file():
            return _fail(f"--research-db {args.research_db!r} does not exist")
    runs = [
        _open_run(db_path, args.db, run_id, reports_root=None, count_reports=False, noun="pool")
        for run_id in args.run_id
    ]
    parts: list[RunScores] = []
    try:
        with ReplayStore(replay_path) as store:
            variant = store.variant(args.variant)
            if variant.model_cutoff is None and not args.include_pre_cutoff:
                raise _Refused(
                    f"{variant.name!r} records no model_cutoff, so the questions the model may "
                    "have been trained on cannot be left out (plan section 6): set model_cutoff "
                    "in the variant file and run `register`, or pass --include-pre-cutoff"
                )
            for run in runs:
                run_id = run.facts.run_id
                pinned = store.pinned_split(run_id)
                if pinned is None:
                    raise _Refused(
                        f"run {run_id!r} has no split pinned in {replay_path}: the pooled bar "
                        "reads each run's validation segment under its own pinned split, so "
                        "replay or probe the run through this store first"
                    )
                run = _pinned(run, *pinned)
                asked = {
                    probe.name: answers
                    for probe, answers in store.probe_answers(variant.sha, run_id)
                }
                if args.probe not in asked:
                    raise _Refused(
                        f"variant {variant.name!r} was not asked the probe {args.probe!r} on run "
                        f"{run_id!r} in {replay_path}"
                        + (f" (it was asked {', '.join(sorted(asked))})" if asked else "")
                    )
                research: Mapping[int, float] = {}
                if research_path is not None:
                    research = _research_closes(
                        research_path, args.research_db, run.facts, run.split, holdout=False
                    )
                card = score_run(
                    run.decisions.questions,
                    [],
                    step_ms=run.facts.step_ms,
                    costs=run.facts.costs,
                    research_closes=research,
                    split=run.split,
                )
                scope = cutoff_scope(
                    run.decisions.questions, variant, include_pre_cutoff=args.include_pre_cutoff
                )
                assert run.pinned_at is not None
                parts.append(
                    RunScores(
                        run_id=run_id,
                        pinned_at=run.pinned_at,
                        scores={
                            key: headline_scores(
                                card, asked[args.probe], scope.eligible, key, SegmentName.VALIDATION
                            )
                            for key in PROBE_KEYS
                        },
                        left_out=sum(
                            1
                            for row in card.rows
                            if row.segment is SegmentName.VALIDATION
                            and row.question.input_id not in scope.eligible
                        ),
                    )
                )
    except (ReplayStoreError, StoreError) as exc:
        return _fail(str(exc))
    print(f"direction probe {args.probe!r}, {variant.describe()}")
    for line in describe_pool(parts, block=args.block, draws=args.draws, seed=args.seed):
        print(line)
    return 0


# -- register ------------------------------------------------------------------


def _cmd_register(args: argparse.Namespace) -> int:
    if not args.replay_db:
        return _fail("--replay-db needs a path, got ''")
    try:
        variant = load_variant(Path(args.variant))
        with ReplayStore(Path(args.replay_db), create=True) as store:
            notes = store.register(variant, now=_now())
    except (VariantError, ReplayStoreError) as exc:
        return _fail(str(exc))
    print(f"registered {variant.describe()}")
    print(
        f"model_cutoff: {variant.model_cutoff}"
        if variant.model_cutoff is not None
        else "model_cutoff: none (score --replay-db refuses it without --include-pre-cutoff)"
    )
    for note in notes:
        print(f"note: {note}")
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
