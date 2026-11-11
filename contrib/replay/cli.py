"""``python -m contrib.replay`` — the offline exam's commands.

Seven commands:

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
  segments (plan PR 2.2): the headline and up-vs-down skills per horizon,
  against the base rate and against the model's own prior, with 90%
  block-bootstrap intervals, and the plan section 5 bar. Reads only.
- ``effect --run-id A [--run-id B ...] --replay-db PATH --variant NAME
  [--variant NAME ...] --against NAME`` — whether one more section of text
  moves the decision (discovery plan section 4.7, :mod:`.effect`): each
  variant's stored answers against ``--against``'s on the same questions,
  pooled over the runs: how many answers mention the section, which way the
  model's reading and the executed one moved, how that compares with the
  spread between two repeats of one variant, and a verdict against the
  bar that module states. Reads only; scores no
  price, so no ``model_cutoff`` is needed, and never reads the holdout.
- ``guardrail --run-id A [--run-id B ...] --research-db PATH`` — the trend
  guardrail's shadow mode (:mod:`.guardrail`): for every finished question
  of each run, the side the guardrail rule held at that instant, the side
  the paper book held once the decision was applied, and what the guardrail
  would have done about it. Scores nothing and reads no later price; with
  ``--out DIR`` it writes one CSV row per question beside the summary.
- ``backtest --run-id A [--run-id B ...] --research-db PATH`` — the
  simulated account (replay plan PR 3a, :mod:`.account`): every finished
  question of each run walked in order by one account that carries its
  position, under the run's own fill model (a maker post judged against
  the bar it falls in, ``--tick`` the venue's price tick) and the research
  store's hourly funding, for the paper trader's own answers and the three
  price controls (:mod:`.gate`), and with ``--replay-db ... --variant NAME``
  for every repeat of that variant's replayed answers too; then the mother
  plan's section 5 bar on the validation segment, pooled across the runs
  (a repeat's controls pooled over the runs it was asked on, the Sharpe
  over the questions past the model cutoff). With a replay store the run is
  pinned if it was not, as a replay pins it.
  Without a variant the bar judges the paper trader against itself — the
  program's smoke test (checks 1 and 3 cannot pass). Each walked path is
  written to the replay store when one is given. ``--holdout`` walks the
  holdout too and spends its look, as ``score --holdout`` does -- for a
  variant only once every repeat passes the bar on the validation
  segment and was asked the holdout questions (plan section 5).

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
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .account import (
    STEP_COLUMNS,
    AccountError,
    Path as AccountPath,
    Series,
    Terms,
    simulate,
)
from .compare import Card, Scope, Table, cards_by_repeat, compare, cutoff_scope
from .effect import MENTION, Arm, already_carried, arm_run, describe_effect
from .gate import (
    CONTROLS,
    Readings,
    Targets,
    Verdict,
    answer_targets,
    control_targets,
    decisions as account_decisions,
    describe_paths,
    judge,
)
from .guardrail import describe_shadow, shadow, shadow_table
from .lower_graph import LowerGraph, LowerGraphError
from .model import engine_model
from .paper_store import (
    Decisions,
    RunFacts,
    gate_config,
    load_decisions,
    load_research_bars,
    load_research_closes,
    load_research_funding,
    run_facts,
)
from .pool import BLOCK, DRAWS, JUDGED_KEY, RunScores, describe_pool
from .probe import PROBE_KEYS, PROBE_STEP_MS, Probe, ProbeAnswer, ProbeError, load_probe
from .probe_score import (
    PRIOR_MIN_FORECASTS,
    describe_probe,
    ensemble,
    headline_scores,
    own_prior,
)
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
from .replay_store import (
    VARIANT_DRIVER,
    BacktestRecord,
    HoldoutLook,
    ReplayStore,
    ReplayStoreError,
)
from .score import (
    Answer,
    Question,
    Scorecard,
    ScoreError,
    bars_per_year,
    build_split,
    csv_table,
    score_run,
    segment_of,
)
from .upstream import (
    DEFAULT_RULE,
    RULE_INTERVAL,
    STUDIED_INTERVALS,
    Database,
    DecisionConfig,
    GuardrailError,
    ResearchStore,
    RiskConfig,
    RuleTimeline,
    SchemaVersionError,
    SegmentName,
    Split,
    SplitError,
    StoreError,
    build_timeline,
    from_epoch_ms,
    load_rule,
    payload_dir,
)
from .variant import GRAPH_MODE, GUARDRAIL_RULE, Variant, VariantError, load_variant, short_sha
from .walk import Exam, ask_walks, load_all_reports, pending_walks, preflight

__all__ = ["main"]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


# The clock, the pause between retries, and the model factory the ``replay``
# command uses; module attributes so the tests can replace them.
_now: Callable[[], datetime] = _utc_now
_sleep: Callable[[float], None] = time.sleep
_build_model = engine_model
_build_graph = LowerGraph


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
            "and print the headline and up-vs-down Brier skill scores per horizon, against "
            "the train base rate and against the model's own train prior, each with a 90 "
            "percent interval from a circular block bootstrap within each run, and whether the "
            "plan section 5 bar (4h, both skills against the own prior, each lower end above "
            "0, blocks of 6, each skill's questions in at least 5 of them) is met. A run with "
            "no 4h train base rate, or "
            f"fewer than {PRIOR_MIN_FORECASTS} valid 4h train forecasts to form the prior from, "
            "is refused. Reads "
            "only; the holdout is never read."
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
        help=(
            f"consecutive questions per bootstrap block, within a run (default: {BLOCK}); the "
            "plan section 5 bar is read only at the default"
        ),
    )
    pool.add_argument(
        "--draws", type=int, default=DRAWS, help=f"bootstrap draws (default: {DRAWS})"
    )
    pool.add_argument("--seed", type=int, default=0, help="the bootstrap's seed (default: 0)")
    pool.set_defaults(func=_cmd_pool)

    effect = subparsers.add_parser(
        "effect",
        help="whether a variant's extra context moved its answers, against another variant's",
        description=(
            "Compare the stored answers of one or more variants with those of --against on "
            "the same questions of one or more paper runs, each cut by the split pinned for it "
            "in the replay store: how many answers mention the section, which way the model's "
            "reading and the executed one moved on each question both answered, the mean shift "
            "per question with a 90 percent interval from a circular block bootstrap within "
            "each run, how far apart two repeats of one variant are, and a verdict: the extra "
            "context moves the decision when that interval does not hold 0 and the shift is "
            "larger than the spread between two repeats of either variant; none is given when "
            "under 80 percent of the question-repeats asked carry a readable claim from both. "
            "The variants are "
            "meant to differ in their extra_context alone; the report names whatever else "
            "differs, and then gives no verdict. Simple variants only. Reads only: no later price is scored and the "
            "holdout is never read."
        ),
    )
    effect.add_argument("--db", default="paper_trading.db", help="the paper store (SQLite path)")
    effect.add_argument(
        "--run-id",
        action="append",
        required=True,
        help="a run to read; give it once per run",
    )
    effect.add_argument("--replay-db", required=True, metavar="PATH", help="the replay store")
    effect.add_argument(
        "--variant",
        action="append",
        required=True,
        metavar="NAME",
        help="a variant to compare with --against; give it once per variant",
    )
    effect.add_argument(
        "--against",
        required=True,
        metavar="NAME",
        help=(
            "the variant the others are compared with: the one without the section, or the "
            "one whose section says the opposite"
        ),
    )
    effect.add_argument(
        "--mention",
        action="append",
        metavar="TEXT",
        help=(
            "a phrase that counts an answer as mentioning the section, whatever its case; "
            f"give it once per phrase (default: {', '.join(MENTION)})"
        ),
    )
    effect.add_argument(
        "--block",
        type=int,
        default=BLOCK,
        help=(
            f"consecutive questions per bootstrap block, within a run (default: {BLOCK}); the "
            "verdict is read only at the default"
        ),
    )
    effect.add_argument(
        "--draws", type=int, default=DRAWS, help=f"bootstrap draws (default: {DRAWS})"
    )
    effect.add_argument("--seed", type=int, default=0, help="the bootstrap's seed (default: 0)")
    effect.add_argument(
        "--out",
        metavar="DIR",
        help=(
            "write effect-<variant>-against-<against>-decisions.csv (one row per question and "
            "repeat both answered) and -summary.txt here, per variant"
        ),
    )
    effect.set_defaults(func=_cmd_effect)

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

    guardrail = subparsers.add_parser(
        "guardrail",
        help="what the trend guardrail would have done to a paper run's recorded decisions",
        description=(
            "Read the decision attempts of one or more paper runs and, for each question, the "
            "side the guardrail rule held at that instant (replayed on the research store's "
            f"{RULE_INTERVAL} candles), and print what the guardrail would have done: pass, block "
            "an order's target to flat, or close a position already held. The guardrail is "
            "wired into the paper trader behind risk.trend_guardrail (off by default); this "
            "command re-reads recorded decisions offline, with or without it. Scores nothing and "
            "reads no later price, so every finished question is read. Never writes to the paper "
            "store."
        ),
    )
    guardrail.add_argument(
        "--db", default="paper_trading.db", help="the paper store (SQLite path)"
    )
    guardrail.add_argument(
        "--run-id",
        action="append",
        required=True,
        help="a run to read; give it once per run",
    )
    guardrail.add_argument(
        "--research-db",
        required=True,
        metavar="PATH",
        help=(
            "an existing autoresearch.sqlite whose candles the rule is replayed on; a position "
            "held at a question its history does not reach reads rule_unknown, and a run it "
            "reaches nowhere is refused"
        ),
    )
    guardrail.add_argument(
        "--rule",
        metavar="FILE",
        help=(
            "the guardrail rule, a strategy spec JSON file (default: the research package's "
            "committed rule); whatever cadence it was written for, it is replayed on "
            f"{RULE_INTERVAL} bars"
        ),
    )
    guardrail.add_argument(
        "--out",
        metavar="DIR",
        help=(
            "write <run-id>-guardrail-decisions.csv (one row per question) and "
            "<run-id>-guardrail-summary.txt here"
        ),
    )
    guardrail.set_defaults(func=_cmd_guardrail)

    backtest = subparsers.add_parser(
        "backtest",
        help="walk one account through each run's decisions, and read the plan section 5 bar",
        description=(
            "Walk every finished question of each paper run, in order, with one simulated "
            "account that carries its position (replay plan PR 3a): the paper trader's own "
            "answers and three price controls (buy-and-hold at the cap, flat, the research "
            "radar's bias at the cap), and with --replay-db and --variant every repeat of that "
            "variant's replayed answers. Fills follow the run's own fill model (a maker post "
            "is judged against the bar the decision falls in, on the research store's candles), "
            "funding is settled hourly from the research store, there is no stop-loss and no "
            "take-profit. Then the mother plan's section 5 bar on the validation segment, "
            "pooled across the runs; without a variant the paper trader is judged against "
            "itself, which is the program's smoke test. Never writes to the paper store; writes "
            "each walked path to the replay store when one is given."
        ),
    )
    backtest.add_argument("--db", default="paper_trading.db", help="the paper store (SQLite path)")
    backtest.add_argument(
        "--run-id",
        action="append",
        required=True,
        help="a run to walk; give it once per run (all on the same candle interval)",
    )
    backtest.add_argument(
        "--research-db",
        required=True,
        metavar="PATH",
        help=(
            "an existing autoresearch.sqlite: its candles judge the maker posts and fill a "
            "missing cycle's later mark, its funding series settles the account hourly"
        ),
    )
    backtest.add_argument(
        "--tick",
        default="1",
        metavar="PRICE",
        help=(
            "the venue's price tick for the run's coin, which the paper store does not record "
            "(default: 1, BTC's); a maker post rests on the passive side of the assumed "
            "spread, rounded to the tick, and fills when the bar trades through it by a tick"
        ),
    )
    backtest.add_argument(
        "--replay-db",
        metavar="PATH",
        help=(
            "a replay.sqlite: each run is walked under the split pinned there, every walked "
            "path is written to it, and --variant names a variant whose replayed answers are "
            "walked too; an existing store older than this build's schema gains the backtest "
            "tables"
        ),
    )
    backtest.add_argument("--variant", metavar="NAME", help="with --replay-db: the variant to walk")
    backtest.add_argument(
        "--ask",
        metavar="FILE",
        help=(
            "a graph variant's YAML file (mode: graph): walk its train and validation "
            "questions in order, asking the lower half of the graph (trader, risk debate, "
            "portfolio manager) from each cycle's recorded reports with the simulated "
            "account's own position in the context, store every answer in --replay-db, then "
            "read the bar on them as for --variant; resumes where it stopped (plan PR 3b)"
        ),
    )
    backtest.add_argument(
        "--repeats",
        type=int,
        metavar="N",
        help="with --ask: walks per question (default: 3, plan section 3-10)",
    )
    backtest.add_argument(
        "--limit",
        type=int,
        metavar="N",
        help=(
            "with --ask: store at most N new answers or refusals this time; the bar is read "
            "once every walk is complete"
        ),
    )
    backtest.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "with --ask: check every payload and reports sidecar and print what would be "
            "asked; no client, no writes"
        ),
    )
    backtest.add_argument(
        "--rule",
        metavar="FILE",
        help=(
            "with --ask on a variant whose guardrail is 'rule': the rule spec to replay over "
            "--research-db (default: the research package's own rule)"
        ),
    )
    backtest.add_argument(
        "--include-pre-cutoff",
        action="store_true",
        help=(
            "with --variant: also count the validation questions decided on or before the "
            "variant's model_cutoff day in the bar's pooled comparisons (plan section 6); "
            "left out by default, and a variant with no model_cutoff is refused without it"
        ),
    )
    backtest.add_argument(
        "--holdout",
        action="store_true",
        help=(
            "walk the holdout segment too and print its figures; a look at the holdout, "
            "recorded in the replay store's ledger (so it needs --replay-db; the store is "
            "created there for a look at the paper trader's own answers). With --variant, "
            "refused by name unless every repeat passes the plan section 5 bar on the "
            "validation segment and was asked the holdout questions; nothing is spent then"
        ),
    )
    backtest.add_argument(
        "--out",
        metavar="DIR",
        help=(
            "write <stem>-backtest-decisions.csv (one row per walked question per path) and "
            "<stem>-backtest-summary.txt here; the stem is the run ids joined by '+', then "
            "-<variant> with one"
        ),
    )
    backtest.set_defaults(func=_cmd_backtest)
    return parser


def _progress(line: str) -> None:
    print(line, file=sys.stderr)


def _fail(message: str) -> int:
    print(f"error: {message}", file=sys.stderr)
    return 1


def _repeated_run_ids(run_ids: Sequence[str]) -> None:
    """Refuse a ``--run-id`` given twice, by name."""
    repeated = sorted({r for r in run_ids if run_ids.count(r) > 1})
    if repeated:
        raise _Refused(f"--run-id names {', '.join(repeated)} more than once")


def _replay_db_flags(
    args: argparse.Namespace,
    *,
    without_store: Sequence[tuple[str, bool]],
    without_variant: Sequence[tuple[str, bool]],
    creates: bool = False,
) -> Path | None:
    """The validated ``--replay-db`` path (``None`` when none was given); every refusal is a :class:`_Refused`.

    ``without_store`` names the flags that only apply with ``--replay-db``
    and ``without_variant`` those that only apply with ``--variant``, each as
    ``(flag, given)``. Only a look at the paper trader's own holdout may
    create the store (it has a look to record); any other reader is refused
    rather than left an empty store behind a typo.
    """
    if args.replay_db is None:
        stray = [flag for flag, given in without_store if given]
        if stray:
            raise _Refused(f"{', '.join(stray)} only apply with --replay-db")
        if args.holdout:
            raise _Refused(
                "--holdout needs --replay-db PATH: every look at the holdout is recorded in "
                "its ledger (plan section 5); the store is created there if it does not exist"
            )
        return None
    if not args.replay_db:
        raise _Refused("--replay-db needs a path, got ''")
    if args.variant is None:
        stray = [flag for flag, given in without_variant if given]
        if stray:
            raise _Refused(f"{', '.join(stray)} only apply with --variant")
    path = Path(args.replay_db)
    creates = creates or (args.holdout and args.variant is None)
    if not path.is_file() and not creates:
        raise _Refused(f"--replay-db {args.replay_db!r} does not exist")
    return path


@dataclass(frozen=True)
class _Run:
    """One paper run, read and cut: what both commands start from.

    ``gate`` is the run's genesis ``risk:`` / ``decision:`` pair, read for
    the ``replay`` and ``backtest`` commands (the scorecard runs without
    one). Once the
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
    if args.replay_db and args.variant is not None and args.against == args.variant:
        return _fail("--against names the variant being scored; compare it with another")
    replay_path = _replay_db_flags(
        args,
        without_store=(
            ("--variant", args.variant is not None),
            ("--against", args.against is not None),
            ("--include-pre-cutoff", args.include_pre_cutoff),
        ),
        without_variant=(
            ("--against", args.against is not None),
            ("--include-pre-cutoff", args.include_pre_cutoff),
        ),
    )
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
    if variant.mode == GRAPH_MODE:
        return _fail(
            f"variant {variant.name!r} is a graph variant (mode: graph): it is walked by "
            "backtest --ask, question by question from its own simulated book; replay asks "
            "simple variants"
        )
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
                # Another command may have pinned the run between the read
                # above and here: the questions selected would then belong to
                # a split that does not stand.
                run = _pin(store, run, "replay")
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
    lines.append(_gate_line(run))
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
    _repeated_run_ids(args.run_id)
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
                scores = {
                    key: headline_scores(
                        card, asked[args.probe], scope.eligible, key, SegmentName.VALIDATION
                    )
                    for key in PROBE_KEYS
                }
                if scores[JUDGED_KEY] is None:
                    # Decided 2026-09-24: which runs count is the operator's
                    # call, not something the pool settles by leaving one out.
                    raise _Refused(
                        f"run {run_id!r} has no {JUDGED_KEY} train base rate (no train question "
                        "has an outcome at that horizon), so its questions have nothing to be "
                        "held against; leave it out of --run-id"
                    )
                merged = ensemble(asked[args.probe])
                found_prior = own_prior(card, merged, scope.eligible, JUDGED_KEY)
                counted = 0 if found_prior is None else found_prior[1]
                if counted < PRIOR_MIN_FORECASTS:
                    # The bar reads each run against the model's own prior
                    # (plan section 5, revised 2026-09-29), from its train answers.
                    everyone = {row.question.input_id for row in card.rows}
                    every = own_prior(card, merged, everyone, JUDGED_KEY)
                    cut = (0 if every is None else every[1]) - counted
                    if counted + cut >= PRIOR_MIN_FORECASTS:
                        remedy = (
                            f"{cut} more were left out at the model cutoff: pass "
                            "--include-pre-cutoff to count them, or leave the run out of --run-id"
                        )
                    else:
                        remedy = (
                            f"{cut} more were left out at the model cutoff, still short of it; "
                            if cut
                            else ""
                        ) + "probe the run's train segment, or leave it out of --run-id"
                    raise _Refused(
                        f"run {run_id!r} has {counted} valid {JUDGED_KEY} train forecast(s) from "
                        f"{variant.name!r} for the probe {args.probe!r}, fewer than the "
                        f"{PRIOR_MIN_FORECASTS} the model's own prior is formed from; {remedy}"
                    )
                parts.append(
                    RunScores(
                        run_id=run_id,
                        pinned_at=run.pinned_at,
                        scores=scores,
                    )
                )
    except (ReplayStoreError, StoreError) as exc:
        return _fail(str(exc))
    print(f"direction probe {args.probe!r}, {variant.describe()}")
    for line in describe_pool(parts, block=args.block, draws=args.draws, seed=args.seed):
        print(line)
    return 0


# -- effect --------------------------------------------------------------------


def _locked_card(run: _Run) -> Card:
    """Scores answers on ``run`` under its split with the holdout locked; no research closes."""

    def card(answers: Sequence[Answer], only: Collection[str]) -> Scorecard:
        return score_run(
            run.decisions.questions,
            answers,
            step_ms=run.facts.step_ms,
            costs=run.facts.costs,
            split=run.split,
            only=only,
        )

    return card


def _arm(
    store: ReplayStore, variant: Variant, runs: Sequence[_Run], phrases: Sequence[str]
) -> Arm:
    """``variant``'s stored answers on every run, read as the scorecard reads them.

    Each repeat is one of :func:`~.compare.cards_by_repeat`'s cards (the
    questions it answered or had refused), under the run's pinned split and
    without the holdout: the lock every other report on the run reads under.
    A run the variant was never asked on is refused by name, and so is one
    it was asked only the holdout of: every answer there is behind the lock,
    and a report of nothing would read as a variant that said nothing.
    """
    parts = []
    for run in runs:
        run_id = run.facts.run_id
        answers = store.answers(variant.sha, run_id)
        failed = store.failed(variant.sha, run_id)
        if not answers and not failed:
            raise _Refused(
                f"variant {variant.name!r} has no answers for run {run_id!r} in {store.path}"
            )
        cards = cards_by_repeat(_locked_card(run), answers, failed)
        part = arm_run(
            run_id,
            cards,
            store.responses(variant.sha, run_id),
            phrases,
            refusals=sum(len(refused) for refused in failed.values()),
        )
        if not part.refused and not part.repeats:
            raise _Refused(
                f"variant {variant.name!r} was asked only holdout questions of run {run_id!r}, "
                "which effect never reads: ask it the train and validation segments"
            )
        parts.append(part)
    return Arm(variant, parts, phrases)


def _read_questions(run: _Run) -> list[Question]:
    """The questions of ``run`` the effect report reads: every one outside the holdout."""
    return [
        question
        for question in run.decisions.questions
        if segment_of(run.split, question.at_ms, run.facts.step_ms) is not SegmentName.HOLDOUT
    ]


def _cmd_effect(args: argparse.Namespace) -> int:
    """Each ``--variant``'s stored answers against ``--against``'s (discovery plan section 4.7)."""
    db_path = Path(args.db)
    if not db_path.is_file():
        return _fail(f"database {args.db!r} does not exist")
    if args.block < 1:
        return _fail(f"--block must be at least 1, got {args.block}")
    if args.draws < 1:
        return _fail(f"--draws must be at least 1, got {args.draws}")
    if args.out is not None and not args.out:
        return _fail("--out needs a directory, got ''")
    _repeated_run_ids(args.run_id)
    names = [*args.variant, args.against]
    repeated = sorted({name for name in names if names.count(name) > 1})
    if repeated:
        return _fail(
            f"--variant and --against name {', '.join(repeated)} more than once; a variant is "
            "compared with another"
        )
    phrases = list(MENTION) if args.mention is None else args.mention
    blank = [phrase for phrase in phrases if not phrase.strip()]
    if blank:
        # A blank phrase is in every answer: every arm would read 100%.
        return _fail(f"--mention needs a phrase, got {blank[0]!r}")
    replay_path = Path(args.replay_db)
    if not replay_path.is_file():
        # A reader never creates the store behind a typo.
        return _fail(f"--replay-db {args.replay_db!r} does not exist")
    runs = [
        _open_run(
            db_path, args.db, run_id, reports_root=None, count_reports=False, noun="effect report"
        )
        for run_id in args.run_id
    ]
    try:
        with ReplayStore(replay_path) as store:
            variants = [store.variant(name) for name in names]
            graph = [variant.name for variant in variants if variant.mode == GRAPH_MODE]
            if graph:
                raise _Refused(
                    f"{', '.join(repr(name) for name in graph)}: a graph variant is asked each "
                    "question from its own simulated book, so two of them do not answer the same "
                    "question; effect pairs simple variants"
                )
            pinned_runs = []
            for run in runs:
                pinned = store.pinned_split(run.facts.run_id)
                if pinned is None:
                    raise _Refused(
                        f"run {run.facts.run_id!r} has no split pinned in {replay_path}: replay "
                        "it through this store first"
                    )
                pinned_runs.append(_pinned(run, *pinned))
            *treatments, control = (
                _arm(store, variant, pinned_runs, phrases) for variant in variants
            )
    except ReplayStoreError as exc:
        return _fail(str(exc))
    notes = (already_carried(run.facts.run_id, _read_questions(run)) for run in pinned_runs)
    read = [
        "runs: "
        + ", ".join(f"{run.facts.run_id} (split pinned {run.pinned_at})" for run in pinned_runs),
        *(note for note in notes if note is not None),
    ]
    status = 0
    for index, treatment in enumerate(treatments):
        report = describe_effect(
            treatment, control, block=args.block, draws=args.draws, seed=args.seed
        )
        lines = [*report.preamble, *read, *report.body]
        if index:
            print()
        for line in lines:
            print(line)
        if args.out is not None:
            stem = f"effect-{treatment.name}-against-{control.name}"
            status = max(status, _write_out(Path(args.out), args.out, stem, lines, report.table))
    return status


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


# -- guardrail -----------------------------------------------------------------


def _guardrail_run(db_path: Path, db_arg: str, run_id: str) -> tuple[RunFacts, Decisions]:
    """One paper run's terms and decisions for the guardrail report; every refusal is a :class:`_Refused`.

    Not :func:`_open_run`: that one cuts the research split, which this
    report does not read (it scores nothing), and so refuses a run too short
    to cut.
    """
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
                f"run {run_id!r} is a {facts.mode} run; the guardrail report reads paper runs"
            )
        decisions = load_decisions(db, run_id)
    if not decisions.questions:
        raise _Refused(f"run {run_id!r} has no decision attempt with an input row to read")
    if facts.interval != RULE_INTERVAL:
        raise _Refused(
            f"run {run_id!r} was traded on {facts.interval} candles; the guardrail rule is "
            f"written in {RULE_INTERVAL} bars and is read against {RULE_INTERVAL} runs only"
        )
    return facts, decisions


def _rule_timelines(
    research_path: Path, rule_arg: str | None, runs: Sequence[RunFacts]
) -> dict[str, RuleTimeline]:
    """The guardrail rule's timeline per coin: one replay per coin, since every run on it reads the same history."""
    rule = load_rule(DEFAULT_RULE if rule_arg is None else rule_arg)
    timelines: dict[str, RuleTimeline] = {}
    with ResearchStore(research_path) as store:
        for facts in runs:
            if facts.coin not in timelines:
                timelines[facts.coin] = build_timeline(store, rule, coin=facts.coin)
    return timelines


def _pin(store: ReplayStore, run: _Run, verb: str) -> _Run:
    """Pin the run's split in the store, or refuse if another command pinned a different one meanwhile."""
    run_id = run.facts.run_id
    stood, pinned_at = store.pin_split(run_id, run.split, now=_now())
    if stood != run.split:
        raise _Refused(
            f"run {run_id!r} had its split pinned by another command while this one ran; run "
            f"it again to {verb} under that split"
        )
    return _pinned(run, stood, pinned_at)


def _cmd_guardrail(args: argparse.Namespace) -> int:
    """What the trend guardrail would have done to each run's recorded decisions."""
    db_path = Path(args.db)
    if not db_path.is_file():
        return _fail(f"database {args.db!r} does not exist")
    research_path = Path(args.research_db)
    if not research_path.is_file():
        # Checked before the store is opened: opening a path that does not
        # exist would CREATE an empty research store there.
        return _fail(f"--research-db {args.research_db!r} does not exist")
    if args.out is not None and not args.out:
        return _fail("--out needs a directory, got ''")
    _repeated_run_ids(args.run_id)
    runs = [_guardrail_run(db_path, args.db, run_id) for run_id in args.run_id]
    # One replay per coin: the timeline is the store's whole history, so every
    # run on that coin reads the same one.
    try:
        timelines = _rule_timelines(research_path, args.rule, [facts for facts, _ in runs])
    except (GuardrailError, StoreError) as exc:
        return _fail(str(exc))
    reports: list[tuple[str, list[str], Table]] = []
    for facts, decisions in runs:
        timeline = timelines[facts.coin]
        rows = shadow(decisions.questions, decisions.answers, timeline)
        if all(row.rule_side is None for row in rows):
            # Refused like a store that cannot answer at all, and before any
            # run is printed: a report that judged nothing is not a report,
            # and a caller reading the exit status must not take it for one.
            decided = timeline.close_times[timeline.first_decided :]
            return _fail(
                f"run {facts.run_id!r}: the research store has no rule side for any of its "
                f"{len(rows)} question(s), decided "
                f"{from_epoch_ms(rows[0].question.at_ms):%Y-%m-%d %H:%M} to "
                f"{from_epoch_ms(rows[-1].question.at_ms):%Y-%m-%d %H:%M} UTC, while the rule's "
                f"history there closes {from_epoch_ms(decided[0]):%Y-%m-%d %H:%M} to "
                f"{from_epoch_ms(decided[-1]):%Y-%m-%d %H:%M}; fetch the store so that its "
                "history covers the run, or leave the run out of --run-id"
            )
        taken = timeline.first_taken_ms
        early = sum(
            row.rule_decided_ms is not None and (taken is None or row.rule_decided_ms < taken)
            for row in rows
        )
        lines = [
            f"guardrail shadow: run {facts.run_id} ({facts.coin}, {facts.interval} cycle; "
            f"interval from {facts.describe_source()})",
            *timeline.describe(),
        ]
        if early:
            before = (
                "while the rule took no side anywhere in this store"
                if taken is None
                else "before the rule first took a side in this store"
            )
            lines.append(
                f"{early} question(s) were read {before}: a flat rule there may be a position it "
                "opened before the store begins"
            )
        # The cycles that are not questions: a position the guardrail refuses
        # may have been held through them, unread.
        lines += [*decisions.describe_left_out(), *describe_shadow(rows)]
        reports.append((facts.run_id, lines, shadow_table(rows)))
    status = 0
    for index, (run_id, lines, table) in enumerate(reports):
        if index:
            print()
        for line in lines:
            print(line)
        if args.out is not None:
            written = _write_out(Path(args.out), args.out, f"{run_id}-guardrail", lines, table)
            status = max(status, written)
    return status


# -- backtest (plan PR 3a) -----------------------------------------------------


@dataclass(frozen=True)
class _Coverage:
    """How much of a run's walked questions in scope one repeat was asked."""

    answered: int
    refused: int
    not_asked: int

    @property
    def in_scope(self) -> int:
        return self.answered + self.refused + self.not_asked

    def describe(self) -> str:
        return (
            f"asked {self.answered + self.refused} of {self.in_scope} question(s) in scope "
            f"(answered {self.answered}, refused {self.refused}, not asked {self.not_asked}"
            + ("; a refused or not-asked question holds the book)" if self.refused or self.not_asked else ")")
        )


@dataclass(frozen=True)
class _Walked:
    """One run's paths, and what the bar reads off them.

    ``controls`` are the four controls' paths, ``candidate`` the variant's
    by repeat; ``paper`` is the paper trader's validation rows reduced for
    the smoke test (every question), ``paper_scoped`` the same rows cut, per
    repeat, to the questions that repeat was asked within the cutoff scope
    (plan §6) so a check compares one set of questions on both sides, and
    ``candidate_readings`` the variant's by repeat; ``coverage`` says, per
    repeat, how many walked questions in scope it was asked. ``scope`` is
    the variant's cutoff scope over the run's questions, ``None`` without a
    variant. ``gaps`` counts the holes in the research store's bars inside
    the walked window; ``walked_before`` the paths the store already held
    for the run (``None`` without a store).
    """

    run: _Run
    controls: dict[str, AccountPath]
    candidate: dict[int, AccountPath]
    paper: Readings
    paper_scoped: dict[int, Readings]
    candidate_readings: dict[int, Readings]
    coverage: dict[int, _Coverage]
    scope: Scope | None
    gaps: int
    walked_before: int | None


@dataclass(frozen=True)
class _Prepared:
    """One run as the store left it: under the split that stands for it, with the variant's answers."""

    run: _Run
    answers: Mapping[int, Sequence[Answer]]
    failed: Mapping[int, Collection[str]]
    walked_before: int | None


def _cmd_backtest(args: argparse.Namespace) -> int:
    db_path = Path(args.db)
    if not db_path.is_file():
        return _fail(f"database {args.db!r} does not exist")
    research_path = Path(args.research_db)
    if not research_path.is_file():
        # Checked before the store is opened: opening a path that does not
        # exist would CREATE an empty research store there.
        return _fail(f"--research-db {args.research_db!r} does not exist")
    _repeated_run_ids(args.run_id)
    if args.ask is None:
        stray = [
            flag
            for flag, given in (
                ("--repeats", args.repeats is not None),
                ("--limit", args.limit is not None),
                ("--dry-run", args.dry_run),
                ("--rule", args.rule is not None),
            )
            if given
        ]
        if stray:
            return _fail(f"{', '.join(stray)} only apply with --ask")
    else:
        if args.variant is not None:
            return _fail(
                "--ask names the variant to walk; --variant reads answers already stored, so "
                "the two do not go together"
            )
        if args.holdout:
            return _fail(
                "--ask walks the train and validation segments only; the holdout is not asked "
                "in this build (plan section 3-9), so --holdout does not go with it"
            )
        if args.replay_db is None:
            return _fail(
                "--ask needs --replay-db PATH: every answer the graph gives is stored there "
                "(the store is created if it does not exist)"
            )
        if args.repeats is None:
            args.repeats = 3
        if args.repeats < 1:
            return _fail(f"--repeats must be at least 1, got {args.repeats}")
        if args.limit is not None and args.limit < 1:
            return _fail(f"--limit must be at least 1, got {args.limit}")
    try:
        tick = Decimal(args.tick)
    except InvalidOperation:
        return _fail(f"--tick is a price, got {args.tick!r}")
    if not tick.is_finite() or tick <= 0:
        return _fail(f"--tick must be > 0, got {args.tick!r}")
    if args.out is not None and not args.out:
        return _fail("--out needs a directory, got ''")
    replay_path = _replay_db_flags(
        args,
        without_store=(
            ("--variant", args.variant is not None),
            ("--ask", args.ask is not None),
            ("--include-pre-cutoff", args.include_pre_cutoff),
        ),
        without_variant=(
            () if args.ask is not None else (("--include-pre-cutoff", args.include_pre_cutoff),)
        ),
        creates=args.ask is not None,
    )
    # The gate's blocks are read for the deadband the account applies to
    # its own book, as ``replay`` reads them for the gate it re-runs.
    runs = [
        _open_run(
            db_path,
            args.db,
            run_id,
            reports_root=None,
            count_reports=False,
            with_gate=True,
            noun="backtest",
        )
        for run_id in args.run_id
    ]
    intervals = sorted({run.facts.interval for run in runs})
    if len(intervals) > 1:
        return _fail(
            f"--run-id names runs on {' and '.join(intervals)} candles; the pooled bar reads "
            "runs on one interval"
        )
    if args.ask is not None:
        assert replay_path is not None  # --ask needs --replay-db (checked above)
        ended = _ask(args, runs, db_path, replay_path, research_path, tick)
        if ended is not None:
            return ended
    variant: Variant | None = None
    trials = 1
    store: ReplayStore | None = None
    looks_recorded = False
    try:
        if replay_path is not None:
            store = ReplayStore(replay_path, create=True)
            if args.variant is not None:
                variant = store.variant(args.variant)
                if variant.model_cutoff is None and not args.include_pre_cutoff:
                    raise _Refused(
                        f"{variant.name!r} records no model_cutoff, so the validation questions "
                        "the model may have been trained on cannot be left out of the bar (plan "
                        "section 6): set model_cutoff in the variant file and run `register`, "
                        "or pass --include-pre-cutoff"
                    )
            trials = max(store.trials(), 1)
        # Every refusal that reads nothing of the holdout comes first, for
        # every run, so a refusal on the second run cannot find the first
        # run's look already spent or its split already pinned.
        prepared: list[_Prepared] = []
        unpinned: list[str] = []
        for run in runs:
            answers: Mapping[int, Sequence[Answer]] = {}
            failed: Mapping[int, Collection[str]] = {}
            before: int | None = None
            if store is not None:
                run_id = run.facts.run_id
                pinned = store.pinned_split(run_id)
                if pinned is not None:
                    run = _pinned(run, *pinned)
                else:
                    unpinned.append(run_id)
                if variant is not None:
                    answers = store.answers(variant.sha, run_id)
                    failed = store.failed(variant.sha, run_id)
                    if not answers and not failed:
                        raise _Refused(
                            f"variant {variant.name!r} has no answers for run {run_id!r} in "
                            f"{replay_path}"
                        )
                    if args.holdout:
                        _require_holdout_asked(
                            run, variant, answers, failed, include_pre_cutoff=args.include_pre_cutoff
                        )
                before = len(store.backtests(run_id))
            prepared.append(_Prepared(run, answers, failed, before))

        def walk_all(items: Sequence[_Prepared], holdout: bool) -> list[_Walked]:
            return [
                replace(
                    _walk(
                        item.run,
                        research_path,
                        args.research_db,
                        tick,
                        variant,
                        item.answers,
                        item.failed,
                        include_pre_cutoff=args.include_pre_cutoff,
                        holdout=holdout,
                    ),
                    walked_before=item.walked_before,
                )
                for item in items
            ]

        # The bar is read on the validation segment with the holdout
        # unread, whatever else is walked: the verdict of record is the one
        # that could authorise a look, never one that has seen the holdout.
        locked = walk_all(prepared, holdout=False)
        verdicts = _verdicts(locked, variant, trials)
        if args.holdout and variant is not None:
            # Plan section 5: all four, or no look at the holdout. A
            # candidate that does not pass is refused by name and spends
            # nothing -- no look, no pin, no stored path.
            failing = [verdict.candidate for verdict in verdicts if not verdict.eligible]
            if failing:
                raise _Refused(
                    f"{', '.join(failing)}: not every check of the plan section 5 bar passes on "
                    "the validation segment, so the holdout stays locked and no look is spent; "
                    "run without --holdout to read the checks"
                )
        # With --holdout the holdout is walked into memory first, so a
        # refusal the walk raises (a bar the store lacks) spends nothing and
        # pins nothing; the look is recorded before anything of it is shown
        # or stored.
        fresh = walk_all(prepared, holdout=True) if args.holdout else locked
        if store is not None:
            # A walk written to the store fixes the exam as a replay does:
            # the run is pinned once every check and every walk has passed,
            # so a refusal on the way never freezes a run that is still
            # trading.
            pinned_runs: list[_Prepared] = []
            for item in prepared:
                run = item.run
                run_id = run.facts.run_id
                if run_id in unpinned:
                    run = _pin(store, run, "walk")
                pinned_runs.append(replace(item, run=run))
            prepared = pinned_runs
        # The walk, under the runs as they now stand (pinned).
        walked = [replace(w, run=item.run) for w, item in zip(fresh, prepared, strict=True)]
        if args.holdout:
            assert store is not None  # --holdout needs the store
            for item in prepared:
                store.record_look(
                    action="score",
                    run_id=item.run.facts.run_id,
                    variant_sha=None if variant is None else variant.sha,
                    questions=_holdout_questions(item.run),
                    who=_who(),
                    now=_now(),
                )
                looks_recorded = True
        lines = _backtest_report(walked, verdicts, variant, replay_path, args)
        for line in lines:
            print(line)
        if store is not None:
            _record_paths(store, walked, variant, holdout=args.holdout)
    except (ReplayStoreError, StoreError, AccountError, _Refused) as exc:
        spent = " (the holdout look(s) above are recorded in the ledger)" if looks_recorded else ""
        return _fail(f"{exc}{spent}")
    finally:
        if store is not None:
            store.close()
    if args.out is not None:
        stem = "+".join(args.run_id) + ("" if variant is None else f"-{variant.name}")
        return _write_out(
            Path(args.out), args.out, f"{stem}-backtest", lines, _backtest_table(walked)
        )
    return 0


def _gate_line(run: _Run) -> str:
    """The run's gate in one line: the thresholds every replayed or asked answer meets."""
    assert run.gate is not None
    risk, decision = run.gate
    return (
        f"gate: the run's genesis risk/decision blocks (leverage {risk.leverage}, max target "
        f"margin {risk.max_target_margin_pct}%, deadband {decision.rebalance_deadband_pct}%, "
        f"min_confidence {decision.min_confidence}, resize_min_confidence "
        f"{decision.resize_min_confidence})"
    )


def _ask(
    args: argparse.Namespace,
    runs: Sequence[_Run],
    db_path: Path,
    replay_path: Path,
    research_path: Path,
    tick: Decimal,
) -> int | None:
    """``--ask``: walk a graph variant over each run's train and validation questions, asking as it goes.

    Returns an exit status when the command ends here (a dry run, a walk
    stopped by ``--limit``, a refusal), or ``None`` once every walk is
    complete, with ``args.variant`` set so the bar is read off the stored
    answers as for any variant.
    """
    try:
        variant = load_variant(Path(args.ask))
    except VariantError as exc:
        return _fail(str(exc))
    if variant.mode != GRAPH_MODE:
        return _fail(
            f"variant {variant.name!r} is a {variant.mode} variant; backtest --ask walks graph "
            "variants (mode: graph), and replay asks the simple ones"
        )
    if variant.model_cutoff is None and not args.include_pre_cutoff:
        return _fail(
            f"{variant.name!r} records no model_cutoff, so the validation questions the model "
            "may have been trained on cannot be left out of the bar read after the walk (plan "
            "section 6): set model_cutoff in the variant file, or pass --include-pre-cutoff"
        )
    if args.rule is not None and variant.guardrail != GUARDRAIL_RULE:
        return _fail(
            f"--rule only applies to a variant whose guardrail is {GUARDRAIL_RULE!r}; "
            f"{variant.name!r} reads the guardrail as {variant.guardrail!r}"
        )
    timelines: dict[str, RuleTimeline] = {}
    if variant.guardrail == GUARDRAIL_RULE:
        for run in runs:
            if run.facts.interval != RULE_INTERVAL:
                return _fail(
                    f"run {run.facts.run_id!r} was traded on {run.facts.interval} candles; "
                    f"the guardrail rule is written in {RULE_INTERVAL} bars and is read "
                    f"against {RULE_INTERVAL} runs only"
                )
        try:
            timelines = _rule_timelines(research_path, args.rule, [run.facts for run in runs])
        except (GuardrailError, StoreError) as exc:
            return _fail(str(exc))
    graph: LowerGraph | None = None
    if not args.dry_run:
        try:
            # Built before the store is written or any payload is read: a
            # client that cannot be built must not have registered or pinned.
            graph = _build_graph(variant)
        except (ImportError, ValueError, LowerGraphError) as exc:
            return _fail(f"the lower graph for {variant.name!r} could not be built: {exc}")
    store: ReplayStore | None = None
    try:
        if not args.dry_run or replay_path.is_file():
            store = ReplayStore(replay_path, create=not args.dry_run)
            # What ``register`` would refuse, said now and without writing; the
            # variant itself is registered once every run has passed preflight.
            standing = store.sha_of(variant.name)
            if standing is not None and standing != variant.sha:
                raise _Refused(
                    f"variant name {variant.name!r} already stands for "
                    f"{short_sha(standing)} in {replay_path}, and this file is "
                    f"{variant.short_sha}; a changed variant needs a new name"
                )
        exams: list[tuple[_Run, Exam, bool]] = []
        for run in runs:
            run_id = run.facts.run_id
            pinned = None if store is None else store.pinned_split(run_id)
            if pinned is not None:
                run = _pinned(run, *pinned)
            payload_root = payload_dir(db_path, run_id)
            if not payload_root.is_dir():
                raise _Refused(
                    f"no payload directory at {str(payload_root)!r}; backtest --ask reads the "
                    "run's payloads and their .reports.json where the daemon writes them "
                    "(payloads/<run-id>/ beside the paper store)"
                )
            assert run.gate is not None  # opened with the gate
            risk, decision = run.gate
            papers = select(
                run.decisions.questions,
                run.decisions.inputs,
                split=run.split,
                step_ms=run.facts.step_ms,
                segments={SegmentName.TRAIN, SegmentName.VALIDATION},
            )
            if not papers:
                raise _Refused(f"run {run_id!r} has no question in its train or validation segment")
            prepared = prepare(papers, payload_root=payload_root, risk=risk)
            reports = load_all_reports(prepared, payload_root)
            if not reports:
                raise _Refused(
                    f"run {run_id!r} has no question with a .reports.json beside its payload in "
                    "its train or validation segment, so the lower half of the graph has "
                    "nothing to start from"
                )
            exam = Exam(
                run_id=run_id,
                terms=_terms(run, tick),
                series=_load_series(run, research_path, args.research_db, holdout=False),
                risk=risk,
                decision=decision,
                prepared=prepared,
                reports=reports,
            )
            # What the walk would refuse at a question, refused now: a dry run
            # promises what the live run does, and nothing is paid for or
            # pinned before every question has passed.
            preflight(exam)
            exams.append((run, exam, pinned is None))
        if store is not None and not args.dry_run:
            for note in store.register(variant, now=_now()):
                print(f"note: {note}", file=sys.stderr)
        remaining = args.limit
        for run, exam, unpinned in exams:
            run_id = run.facts.run_id
            timeline = timelines.get(run.facts.coin)
            print(
                f"ask: run {run_id} ({run.facts.coin}, {run.facts.interval} cycle), train + "
                f"validation: {len(exam.prepared)} question(s) x {args.repeats} repeat(s), the "
                "lower half of the graph from each cycle's recorded reports"
            )
            print(variant.describe())
            recorded = sorted({q.model for q in run.decisions.questions if q.model})
            mismatch = "" if variant.model in recorded else " (not the variant's portfolio manager model)"
            print(f"recorded model of the run's own answers: {', '.join(recorded) or 'unknown'}{mismatch}")
            if variant.guardrail == GUARDRAIL_RULE:
                print(f"guardrail rule: {DEFAULT_RULE if args.rule is None else args.rule}")
            print(_gate_line(run))
            unasked = len(exam.prepared) - exam.askable
            if unasked:
                print(
                    f"questions without a .reports.json beside the payload: {unasked} of "
                    f"{len(exam.prepared)} (the engine failed those cycles; nothing is asked "
                    "there and the account holds its book)"
                )
            if store is not None:
                held = sorted(
                    set(store.answers(variant.sha, run_id)) | set(store.failed(variant.sha, run_id))
                )
                beyond = [repeat for repeat in held if repeat >= args.repeats]
                if beyond:
                    print(
                        f"note: the store also holds repeat(s) {beyond} of {variant.name!r} on this "
                        f"run; this command walks repeats 0-{args.repeats - 1} and the bar reads "
                        "every stored repeat",
                        file=sys.stderr,
                    )
            if args.dry_run:
                stored, todo = pending_walks(exam, variant, store, args.repeats, timeline)
                asks = todo if remaining is None else min(todo, remaining)
                print(run.split_line(replay_path))
                print(
                    f"payloads and reports read: {len(exam.prepared)}, each payload checked "
                    "against its input row's digest where one was recorded"
                )
                print(
                    f"dry run: {stored} answer(s) already stored, {todo} to ask; this command "
                    f"would ask for {asks} of them"
                )
                if remaining is not None:
                    remaining = max(0, remaining - asks)
                continue
            assert store is not None and graph is not None
            if unpinned:
                # Pinned once every payload and sidecar has been read and
                # checked, as the replay pins: a refused command freezes nothing.
                run = _pin(store, run, "walk")
            print(run.split_line(replay_path))
            report = ask_walks(
                exam,
                graph=graph,
                store=store,
                repeats=args.repeats,
                now=_now,
                sleep=_sleep,
                timeline=timeline,
                limit=remaining,
                progress=_progress,
            )
            for line in report.describe():
                print(line)
            if report.stopped_at_limit:
                print(
                    "the bar is read once every walk is complete; run the same command again "
                    "to continue"
                )
                return 0
            if remaining is not None:
                remaining -= report.asked + report.refused
    except (ReplayError, LowerGraphError, ReplayStoreError, StoreError, AccountError, _Refused) as exc:
        _say_lost(graph)
        return _fail(str(exc))
    except KeyboardInterrupt:
        # The interrupted run's completions are paid for too; said before the
        # usual exit, or they are said nowhere.
        _say_lost(graph)
        raise
    finally:
        if store is not None:
            store.close()
    if args.dry_run:
        return 0
    args.variant = variant.name
    return None


def _say_lost(graph: LowerGraph | None) -> None:
    """What the graph runs that produced no stored answer cost, when the walk stops before its report."""
    if graph is not None and (graph.lost_input_tokens or graph.lost_output_tokens):
        print(
            f"tokens spent on graph runs that failed, since this command started: "
            f"{graph.lost_input_tokens} in, {graph.lost_output_tokens} out (paid for)",
            file=sys.stderr,
        )


def _require_holdout_asked(
    run: _Run,
    variant: Variant,
    answers: Mapping[int, Sequence[Answer]],
    failed: Mapping[int, Collection[str]],
    *,
    include_pre_cutoff: bool,
) -> None:
    """Refuse a holdout walk of a repeat not asked every holdout question in the cutoff scope.

    Walked unasked, the repeat would hold whatever it carried out of
    validation through those questions and the figures would read as its
    holdout path; the look is spent once, so partial coverage is refused
    before it, not reported after.
    """
    eligible = cutoff_scope(
        run.decisions.questions, variant, include_pre_cutoff=include_pre_cutoff
    ).eligible
    holdout = {
        q.input_id
        for q in run.decisions.questions
        if q.input_id in eligible
        and segment_of(run.split, q.at_ms, run.facts.step_ms) is SegmentName.HOLDOUT
    }
    if not holdout:
        raise _Refused(
            f"run {run.facts.run_id!r} has no holdout question in the cutoff scope of variant "
            f"{variant.name!r}, so there is nothing of the holdout to walk; no look is spent"
        )
    for repeat in sorted(set(answers) | set(failed)):
        asked = {a.input_id for a in answers.get(repeat, ())} | set(failed.get(repeat, ()))
        missing = holdout - asked
        if missing:
            raise _Refused(
                f"variant {variant.name!r} repeat {repeat} was not asked {len(missing)} of the "
                f"{len(holdout)} holdout question(s) of run {run.facts.run_id!r} in scope (replay "
                "it with --segment holdout --holdout first); no look is spent"
            )


def _load_series(run: _Run, research_path: Path, research_arg: str, *, holdout: bool) -> Series:
    """The run's bars and funding from the research store, read under the holdout lock."""
    facts, split = run.facts, run.split
    until = split.loadable_until(holdout=holdout)
    try:
        with ResearchStore(research_path) as research:
            return Series.of(
                load_research_bars(
                    research,
                    coin=facts.coin,
                    interval=facts.interval,
                    since_ms=split.train.start_ms,
                    until_ms=until,
                ),
                load_research_funding(
                    research, coin=facts.coin, since_ms=split.train.start_ms, until_ms=until
                ),
            )
    except (ValueError, InvalidOperation) as exc:
        # A corrupt row the store's own DTOs refuse, or a series the
        # account refuses (overlapping bars): named, not a traceback.
        raise _Refused(f"--research-db {research_arg}: {exc}") from exc


def _terms(run: _Run, tick: Decimal) -> Terms:
    """The account's terms for the run: its fill model, the tick, its opening balance, its deadband."""
    assert run.gate is not None  # opened with the gate
    facts = run.facts
    return Terms(
        coin=facts.coin,
        costs=facts.costs,
        half_spread_bps=facts.half_spread_bps,
        tick=tick,
        initial_balance=facts.initial_balance,
        deadband_pct=Decimal(str(run.gate[1].rebalance_deadband_pct)),
    )


def _walk(
    run: _Run,
    research_path: Path,
    research_arg: str,
    tick: Decimal,
    variant: Variant | None,
    answers: Mapping[int, Sequence[Answer]],
    failed: Mapping[int, Collection[str]],
    *,
    include_pre_cutoff: bool,
    holdout: bool,
) -> _Walked:
    """One run: its bars and funding read once under the lock, every driver walked, the bar's inputs reduced."""
    facts, split = run.facts, run.split
    assert run.gate is not None  # opened with the gate
    questions = run.decisions.questions
    until = split.loadable_until(holdout=holdout)
    series = _load_series(run, research_path, research_arg, holdout=holdout)
    # The scorecard's later marks come from the same bars, keyed as
    # ``load_research_closes`` keys them, so the store is read once.
    closes = {bar.close_time: float(bar.close) for bar in series.bars}
    if not closes:
        print(
            f"warning: --research-db {research_arg} holds no {facts.coin} {facts.interval} "
            f"candles opening between {from_epoch_ms(split.train.start_ms):%Y-%m-%d %H:%M} and "
            f"{from_epoch_ms(until):%Y-%m-%d %H:%M}; no missing cycle of run {facts.run_id} "
            "can be filled from it, no maker post can be judged, and funding is valued at the "
            "questions' marks",
            file=sys.stderr,
        )
    terms = _terms(run, tick)

    def walk(targets: Targets) -> AccountPath:
        return simulate(
            account_decisions(
                questions,
                run.decisions.inputs,
                targets,
                split=split,
                step_ms=facts.step_ms,
                holdout=holdout,
            ),
            terms=terms,
            series=series,
        )

    def readings(given: Sequence[Answer], only: Collection[str] | None = None) -> Readings:
        return Readings.of(
            score_run(
                questions,
                given,
                step_ms=facts.step_ms,
                costs=facts.costs,
                research_closes=closes,
                split=split,
                holdout=holdout,
                only=only,
            )
        )

    paper_answers = {a.input_id: a for a in run.decisions.answers}
    controls = {
        name: walk(
            answer_targets(questions, paper_answers)
            if name == "paper"
            else control_targets(name, questions)
        )
        for name in CONTROLS
    }
    candidate: dict[int, AccountPath] = {}
    candidate_readings: dict[int, Readings] = {}
    paper_scoped: dict[int, Readings] = {}
    coverage: dict[int, _Coverage] = {}
    scope: Scope | None = None
    if variant is not None:
        scope = cutoff_scope(questions, variant, include_pre_cutoff=include_pre_cutoff)
        walked_ids = {
            q.input_id
            for q in questions
            if holdout or segment_of(split, q.at_ms, facts.step_ms) is not SegmentName.HOLDOUT
        }
        in_scope = walked_ids & scope.eligible
        for repeat in sorted(set(answers) | set(failed)):
            theirs = {a.input_id: a for a in answers.get(repeat, ())}
            refused = set(failed.get(repeat, ()))
            candidate[repeat] = walk(answer_targets(questions, theirs))
            # The questions this repeat was asked (answered or refused)
            # inside the cutoff scope: both sides of checks (3) and (4) are
            # read over exactly these.
            asked = (set(theirs) | refused) & scope.eligible
            candidate_readings[repeat] = readings(list(theirs.values()), asked)
            paper_scoped[repeat] = readings(run.decisions.answers, asked)
            coverage[repeat] = _Coverage(
                answered=len(set(theirs) & in_scope),
                refused=len(refused & in_scope),
                not_asked=len(in_scope - set(theirs) - refused),
            )
    return _Walked(
        run=run,
        controls=controls,
        candidate=candidate,
        paper=readings(run.decisions.answers),
        paper_scoped=paper_scoped,
        candidate_readings=candidate_readings,
        coverage=coverage,
        scope=scope,
        gaps=series.gaps,
        walked_before=None,
    )


def _record_paths(
    store: ReplayStore, walked: Sequence[_Walked], variant: Variant | None, *, holdout: bool
) -> None:
    """Every path of this command into the store, in one transaction."""
    now = _now()
    walks: list[tuple[BacktestRecord, AccountPath]] = []
    for paths in walked:
        run_id = paths.run.facts.run_id
        walks.extend(
            (BacktestRecord(now, run_id, name, None, None, holdout), path)
            for name, path in paths.controls.items()
        )
        for repeat, path in paths.candidate.items():
            assert variant is not None
            walks.append(
                (BacktestRecord(now, run_id, VARIANT_DRIVER, variant.sha, repeat, holdout), path)
            )
    store.write_backtests(walks)


def _verdicts(walked: Sequence[_Walked], variant: Variant | None, trials: int) -> list[Verdict]:
    """The bar on each of the variant's repeats (or on the paper trader alone, the smoke test).

    Read off a walk that stopped at the validation segment's end: the
    verdict of record never sees the holdout. A repeat is judged over the
    runs it was asked on, the controls and the paper readings pooled over
    the same runs, so both sides of every check cover one set of questions.
    """
    per_year = bars_per_year(walked[0].run.facts.step_ms)
    if variant is None:
        papers = [w.paper for w in walked]
        return [
            judge(
                candidate="paper",
                candidate_paths=[w.controls["paper"] for w in walked],
                control_paths={name: [w.controls[name] for w in walked] for name in CONTROLS},
                trials=trials,
                candidate_readings=papers,
                paper_readings=papers,
                bars_per_year=per_year,
            )
        ]
    verdicts: list[Verdict] = []
    for repeat in sorted({repeat for w in walked for repeat in w.candidate}):
        present = [w for w in walked if repeat in w.candidate]
        eligible: frozenset[str] = frozenset()
        for w in present:
            assert w.scope is not None
            eligible |= w.scope.eligible
        verdicts.append(
            judge(
                candidate=f"{variant.name} repeat {repeat}",
                candidate_paths=[w.candidate[repeat] for w in present],
                control_paths={
                    control: [w.controls[control] for w in present] for control in CONTROLS
                },
                trials=trials,
                candidate_readings=[w.candidate_readings[repeat] for w in present],
                paper_readings=[w.paper_scoped[repeat] for w in present],
                bars_per_year=per_year,
                eligible=eligible,
            )
        )
    return verdicts


def _backtest_report(
    walked: Sequence[_Walked],
    verdicts: Sequence[Verdict],
    variant: Variant | None,
    replay_path: Path | None,
    args: argparse.Namespace,
) -> list[str]:
    lines: list[str] = []
    for w in walked:
        run, facts = w.run, w.run.facts
        lines.append(
            f"backtest: run {facts.run_id} ({facts.coin}, {facts.interval} cycle; costs and "
            f"interval from {facts.describe_source()})"
        )
        lines.append(w.controls["paper"].terms.describe())
        if replay_path is not None:
            lines.append(run.split_line(replay_path))
            lines.append(f"paths walked on this run before this one: {w.walked_before}")
        lines.extend(run.split.describe())
        if w.gaps:
            lines.append(
                f"research store: {w.gaps} gap(s) in the {facts.interval} bars inside the walked "
                "window; a funding settlement across a gap is valued at the last close before it"
            )
        if args.holdout:
            lines.append("HOLDOUT WALKED: this look is recorded in the ledger")
        lines.extend(run.decisions.describe_left_out())
    per_year = bars_per_year(walked[0].run.facts.step_ms)
    for name in CONTROLS:
        lines.extend(
            describe_paths(
                name,
                {w.run.facts.run_id: w.controls[name] for w in walked},
                bars_per_year=per_year,
                holdout=args.holdout,
            )
        )
    if variant is None:
        lines.append(
            "smoke test: no --variant, so the bar judges the paper trader against itself; "
            "checks (1) and (3) cannot pass and (4) cannot fail"
        )
    else:
        lines.append(variant.describe())
        for w in walked:
            # One cutoff line per run: the bar pools them all, and a reader
            # re-deriving its n needs each run's count.
            assert w.scope is not None
            prefix = f"run {w.run.facts.run_id}: " if len(walked) > 1 else ""
            lines.extend(prefix + line for line in w.scope.preamble[1:])
        for repeat in sorted({repeat for w in walked for repeat in w.candidate}):
            present = [w for w in walked if repeat in w.candidate]
            lines.extend(
                describe_paths(
                    f"{variant.name} repeat {repeat}",
                    {w.run.facts.run_id: w.candidate[repeat] for w in present},
                    bars_per_year=per_year,
                    holdout=args.holdout,
                )
            )
            lines.extend(
                f"  run {w.run.facts.run_id}: {w.coverage[repeat].describe()}" for w in present
            )
    if args.holdout:
        lines.append(
            "the bar below was read with the holdout unread (the verdict that could spend a "
            "look); the paths above include the holdout"
        )
    for verdict in verdicts:
        lines.extend(verdict.describe(holdout_walked=args.holdout))
    return lines


def _backtest_table(walked: Sequence[_Walked]) -> Table:
    rows: list[list[object]] = []
    for paths in walked:
        run_id = paths.run.facts.run_id
        drivers: list[tuple[str, int | None, AccountPath]] = [
            (name, None, path) for name, path in paths.controls.items()
        ]
        drivers.extend(
            (VARIANT_DRIVER, repeat, path) for repeat, path in paths.candidate.items()
        )
        for driver, repeat, path in drivers:
            rows.extend([run_id, driver, repeat, *step.values()] for step in path.steps)
    return ["run_id", "driver", "repeat", *STEP_COLUMNS], rows


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
