"""``python -m contrib.autoresearch`` — the research radar's commands.

Two of them are about the store:

- ``fetch --coin BTC --interval 4h --since 2023-01-01`` — walk that candle
  series and the coin's funding history into the store, then scan what landed
  for holes. ``--resume`` starts the funding walk just past the newest
  settlement already stored instead of at ``--since``.
- ``gaps --coin BTC --interval 4h`` — re-run that scan over what is already
  stored. No network, so it is the command to reach for when judging a store
  rather than filling one. Both scans also cover the daily backdrop, which
  every experiment reads whatever its interval.

Two are about the LANGUAGE a hypothesis is written in, and neither opens a
store or a socket:

- ``vocab`` — print the closed feature vocabulary, generated from the table
  the parser resolves against (plan §4).
- ``validate-spec --spec rule.json`` — parse one spec and read it back in
  words, or refuse it naming the path inside the document and the fix. It is
  the same parser the hypothesis loop will run, so a spec that passes here is
  one a trial can be spent on.

Five are the LEDGER (plan PR A4), and every one of them reads a store:

- ``experiment --name btc-4h --interval 4h`` — measure where the store's
  history is fit to measure on, cut train / validation / holdout, and write
  the conditions every trial in it will be measured under. The first one on a
  coin pins its holdout for good, so ``--dry-run`` prints the same split and
  writes no experiment.
- ``evaluate --experiment btc-4h --spec rule.json`` — score one rule on train
  and validation and file it as a trial. The holdout is not read, and a rule
  already filed is answered without its holdout even if it was promoted.
- ``promote --experiment btc-4h --trial 3`` — apply the gate, and only then
  measure that one trial's holdout. Once per trial; and every promotion on a
  coin is counted, because each is another look at the same window.
- ``report [--experiment btc-4h [--trial 3]]`` — read the ledger back. Reads
  nothing else, and loads no part of the feature stack.
- ``calibrate --experiment btc-4h`` — score the baselines (buy-and-hold,
  always-flat, a high-turnover noise rule) on the experiment's windows,
  filing nothing.

One is the SEARCH (plan PR B1), and it is the only command that talks to a
model:

- ``research --experiment btc-4h --provider anthropic --model ... [--max-trials 10]``
  — ask a model for one rule at a time, score each through the same parser and
  evaluator ``evaluate`` uses, and show the next round what the last one scored
  or why it was refused. The budget counts ANSWERS: a refused answer and a rule
  already tried each spend one. It never reads the holdout and never promotes.
  ``--dry-run`` prints the exact prompt and asks nothing of any model.

Exit codes, kept in step with the perp package's CLI so an operator's habits
carry across: ``0`` the command did what it says, ``1`` a named operator,
store, venue, spec, measurement or ledger failure (the sentence on stderr
says which), ``2`` argparse's own usage errors (a malformed argv, or an
``--interval`` outside the two this package studies), ``130`` interrupted.
Note what ``1`` does NOT mean here: a store with gaps in it is a successful
scan, reported and exited ``0``. Gaps are a fact about the venue's history,
not a failure of the command that found them — and the command that fills
them is ``fetch``, which an operator reads this report to decide about. A
REFUSED SPEC is the other way round: the document was the input, and a spec
this parser will not accept is one no trial should be spent on, so it exits
``1``. So is a trial the gate refuses to promote.
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from .constants import DAILY_INTERVAL, DEFAULT_MAX_TRIALS, STUDIED_INTERVALS
from .costs import (
    LIVE_LEVERAGE,
    LIVE_SLIPPAGE_BPS,
    LIVE_TAKER_FEE_RATE,
    VENUE_BASE_MAKER_FEE_RATE,
    CostModel,
    FillRole,
)
from .dsl import SpecError, StrategySpec, describe_spec, load_spec
from .fetch import (
    FUNDING_SERIES,
    StopReason,
    backfill_candles,
    backfill_funding,
    describe_stop,
    funding_resume_start,
    render_fetch,
)
from .gaps import render_report, scan_candles, scan_funding
from .ledger import (
    PENALTY_K,
    SHARPE_BASE,
    Experiment,
    Ledger,
    LedgerError,
    Penalty,
    SearchTrial,
    Trial,
    TrialStatus,
    Verdict,
)
from .metrics import describe_measurement
from .ports import HypothesistError
from .split import DEFAULT_TRAIN_SHARE, DEFAULT_VALIDATION_SHARE
from .store import (
    DB_FILENAME,
    ResearchStore,
    StoreError,
    canonical_coin,
    default_db_path,
)
from .upstream import ExchangeError, from_epoch_ms
from .vocabulary import describe_vocabulary

__all__ = ["main"]

# ``--interval``'s choices: the two intervals this package studies (see
# ``constants.STUDIED_INTERVALS`` for why it is two and not the venue's enum).
_INTERVALS = STUDIED_INTERVALS

# The one naive form ``--since`` accepts: a calendar date and nothing else.
_BARE_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _parse_since(text: str) -> datetime:
    """``--since`` as an aware UTC instant; ``ValueError`` naming the problem otherwise.

    A bare ``YYYY-MM-DD`` is midnight UTC — the spelling the plan's own
    example uses, and the one an operator actually types for a backfill
    start. A full datetime must carry an offset: this value becomes a window
    edge on the wire, and the rest of this package refuses host-local clocks
    at every boundary, so accepting one here (silently reading it in whatever
    zone the box happens to be set to) would be the one door left open.
    """
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(
            f"--since {text!r} is not a date or ISO-8601 instant "
            f"(e.g. 2023-01-01 or 2023-01-01T00:00:00+00:00)"
        ) from exc
    if parsed.tzinfo is not None:
        return parsed
    # A bare date is the only naive form accepted, and it is recognised by
    # MATCHING the bare-date shape rather than by ruling separators out one at
    # a time. Recognised by the TEXT, still: "2023-01-01T00:00:00" parses to
    # exactly the same midnight, but it is a datetime whose author left the
    # zone out, which is the thing being refused.
    #
    # Ruling separators out is what this got wrong. It excluded "T" and " ",
    # and ``fromisoformat`` also accepts a LOWERCASE "t" (3.11+), so
    # "2023-01-01t05:00:00" was neither refused nor read as 05:00 — it passed
    # the bare-date branch and was stamped midnight UTC, losing five hours
    # from a window the operator spelled out. A positive match has no such
    # list to keep in step with the parser's.
    if _BARE_DATE.fullmatch(text):
        return parsed.replace(tzinfo=timezone.utc)
    raise ValueError(
        f"--since {text!r} has no UTC offset; write a bare date (2023-01-01) for "
        f"midnight UTC, or spell the offset out (2023-01-01T00:00:00+00:00)"
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m contrib.autoresearch",
        description="AutoResearch history store: the research radar's own BTC data.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_db(sub: argparse.ArgumentParser) -> None:
        sub.add_argument(
            "--db", default=None, help=f"store path (default: <repo>/data/{DB_FILENAME})"
        )

    def add_common(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("--coin", default="BTC", help="perp coin symbol (default: BTC)")
        sub.add_argument(
            "--interval",
            default=_INTERVALS[0],
            choices=_INTERVALS,
            help=f"candle interval (default: {_INTERVALS[0]})",
        )
        add_db(sub)

    fetch_cmd = subparsers.add_parser(
        "fetch", help="walk venue history into the store, then scan it for holes"
    )
    add_common(fetch_cmd)
    fetch_cmd.add_argument(
        "--since",
        required=True,
        help="backfill start, as 2023-01-01 (midnight UTC) or a full ISO-8601 instant",
    )
    # One group: ``--resume`` is about the funding walk, so beside
    # ``--skip-funding`` it would be a flag that does nothing - refused as a
    # usage error rather than accepted and silently ignored.
    funding_walk = fetch_cmd.add_mutually_exclusive_group()
    funding_walk.add_argument(
        "--skip-funding",
        action="store_true",
        help=(
            "only walk the candle series. Funding is not interval-scoped, so a second "
            "fetch at another interval would re-walk it for nothing; this skips that pass."
        ),
    )
    funding_walk.add_argument(
        "--resume",
        action="store_true",
        help=(
            "start the funding walk just past the newest settlement already stored, not "
            "at --since. A walk from --since re-covers every window and so fills holes; a "
            "resume fills nothing behind it, but costs a request or two instead of one per "
            "twenty days since --since (about forty from the 4h depth wall, past where the "
            "venue starts throttling). Candles are always re-walked: five pages to the "
            "venue's depth wall, and the re-walk is what fills their holes."
        ),
    )

    gaps_cmd = subparsers.add_parser(
        "gaps", help="scan the stored series for holes (reads the store only, no network)"
    )
    add_common(gaps_cmd)

    # Neither of the next two takes --coin, --interval or --db, and the
    # absence is the statement: the vocabulary and the parser are properties
    # of this build, not of a store or of a market. A --db they ignored would
    # invite the reading that a spec is checked against the data it will be
    # measured on, which is the evaluator's job (plan PR A3) and not this
    # command's.
    subparsers.add_parser(
        "vocab", help="print the closed feature vocabulary a spec may refer to"
    )
    spec_cmd = subparsers.add_parser(
        "validate-spec", help="parse a strategy spec and read it back, or refuse it by name"
    )
    spec_cmd.add_argument("--spec", required=True, help="path to a JSON strategy spec")

    experiment_cmd = subparsers.add_parser(
        "experiment",
        help="cut train/validation/holdout over the store and write an experiment's conditions",
    )
    add_common(experiment_cmd)
    experiment_cmd.add_argument("--name", required=True, help="the experiment's name")
    experiment_cmd.add_argument(
        "--fill-role",
        default=FillRole.TAKER.value,
        choices=[role.value for role in FillRole],
        help="which fee a fill pays (default: taker, the paper run's)",
    )
    for flag, default, what in (
        ("--taker-fee-rate", LIVE_TAKER_FEE_RATE, "taker fee, a fraction of notional"),
        ("--maker-fee-rate", VENUE_BASE_MAKER_FEE_RATE, "maker fee, a fraction of notional"),
        ("--slippage-bps", LIVE_SLIPPAGE_BPS, "adverse slippage per fill, basis points"),
        ("--leverage", LIVE_LEVERAGE, "notional per unit of margin"),
        ("--train-share", DEFAULT_TRAIN_SHARE, "train's share of the measurable span"),
        ("--validation-share", DEFAULT_VALIDATION_SHARE, "validation's share of it"),
        ("--sharpe-base", SHARPE_BASE, "the promote threshold at one trial"),
        ("--penalty-k", PENALTY_K, "how much each ln(trials) raises it"),
    ):
        experiment_cmd.add_argument(
            flag, type=float, default=default, help=f"{what} (default: {default:g})"
        )
    experiment_cmd.add_argument(
        "--indicator-lookback",
        type=int,
        default=None,
        help="bars the indicator engine is shown at each bar (default: the live candle_lookback)",
    )
    experiment_cmd.add_argument("--notes", default="", help="free text stored with the experiment")
    experiment_cmd.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "print the split and whether it would pin the coin's holdout, and write no "
            "experiment (opening the store still brings its schema up to date)"
        ),
    )

    evaluate_cmd = subparsers.add_parser(
        "evaluate", help="score a spec on train and validation and file it as a trial"
    )
    evaluate_cmd.add_argument("--experiment", required=True)
    evaluate_cmd.add_argument("--spec", required=True, help="path to a JSON strategy spec")
    add_db(evaluate_cmd)

    promote_cmd = subparsers.add_parser(
        "promote", help="apply the gate, then measure one trial's holdout (once)"
    )
    promote_cmd.add_argument("--experiment", required=True)
    promote_cmd.add_argument("--trial", required=True, type=int, help="the trial's number")
    add_db(promote_cmd)

    report_cmd = subparsers.add_parser(
        "report", help="read the ledger back: experiments, trials, one trial in full"
    )
    report_cmd.add_argument("--experiment", default=None)
    report_cmd.add_argument("--trial", default=None, type=int, help="needs --experiment")
    add_db(report_cmd)

    calibrate_cmd = subparsers.add_parser(
        "calibrate", help="score the baselines on an experiment's windows, filing nothing"
    )
    calibrate_cmd.add_argument("--experiment", required=True)
    add_db(calibrate_cmd)

    signal_cmd = subparsers.add_parser(
        "signal", help="write the promoted rule's current qualitative signal for the live path"
    )
    # ``--coin`` but no ``--interval``: the bar cadence is the promoted
    # experiment's, not the operator's to pick here, and offering the flag
    # would invite an answer measured on bars the rule was never scored on.
    signal_cmd.add_argument("--coin", default="BTC", help="perp coin symbol (default: BTC)")
    signal_cmd.add_argument(
        "--out", required=True, help="path to write the handoff document to (JSON)"
    )
    signal_cmd.add_argument(
        "--allow-taker",
        action="store_true",
        help=(
            "publish even though the promoted rule was scored under taker fills "
            "(plan §7 wants it re-run under maker costs first)"
        ),
    )
    add_db(signal_cmd)

    research_cmd = subparsers.add_parser(
        "research", help="ask a model for rules, score each one, and file what it answered"
    )
    research_cmd.add_argument("--experiment", required=True)
    research_cmd.add_argument(
        "--max-trials",
        type=int,
        default=DEFAULT_MAX_TRIALS,
        help=(
            f"answers to spend this run (default: {DEFAULT_MAX_TRIALS}). A refused answer "
            f"and a rule already tried each spend one"
        ),
    )
    # No default provider or model, and the absence is deliberate: WHICH model
    # proposed a rule is part of what an experiment's results mean, and a
    # command quietly falling back to some configured default would file trials
    # from a model nobody chose. Refused by name in the command unless
    # --dry-run, which asks nothing of any model.
    research_cmd.add_argument("--provider", default=None, help="LLM provider (e.g. anthropic)")
    research_cmd.add_argument("--model", default=None, help="model name for that provider")
    research_cmd.add_argument("--base-url", default=None, help="override the provider endpoint")
    research_cmd.add_argument(
        "--temperature",
        type=float,
        default=None,
        help="sampling temperature, if this model takes one",
    )
    research_cmd.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "print the prompt this experiment would send and stop; no model is asked and "
            "nothing is filed"
        ),
    )
    add_db(research_cmd)
    return parser


def _store_path(args: argparse.Namespace) -> Path:
    return Path(args.db) if args.db else default_db_path()


def _coin(args: argparse.Namespace) -> str:
    """``--coin`` in the one spelling everything downstream uses.

    Canonicalised HERE as well as inside the store, because the two ends need
    it for different reasons and neither covers the other. The store's own
    canonicalisation is what stops one market being filed as two series; this
    one is what stops the request. The venue looks its coin up in a map keyed
    by the exact ticker, so ``--coin btc`` reached it as a bare ``KeyError:
    'btc'`` wearing an exchange-failure message — a shift key reported as the
    venue being broken.
    """
    return canonical_coin(args.coin)


def _print_reach(store: ResearchStore, *, coin: str, series: str) -> None:
    """Say where the last backfill of this series REACHED, and why it stopped there.

    The gap scan cannot answer this and never will: it anchors its grid on the
    first stamp it finds, so a series whose front was cut off by an interrupted
    backfill is internally consistent and reports no holes. "Complete" there
    means "what is here is a grid", not "this covers the span you asked for" —
    and the difference between a 4h series that ends early because the venue
    serves nothing older and one that ends early because someone interrupted it
    is invisible in the rows. It is in ``series_state``, so it is printed
    beside the scan rather than left for someone to query by hand.
    """
    state = store.series_state(coin=coin, series=series)
    if state is None:
        # "no fetch has recorded one", not "never fetched": a fetch whose
        # breadcrumb write failed has just run and landed rows, and saying it
        # never happened contradicts the line printed directly above.
        print("  reach: no fetch has recorded one in this store")
        return
    # A recorded name this build does not know is shown as it was stored, not
    # raised on. The column holds ``StopReason``'s member NAMES so the stored
    # value survives a change of wording, but a store written by a build with
    # an ending this one lacks would otherwise turn the whole scan into a
    # KeyError - a diagnostic command failing outright on the one store an
    # operator most needs to look at.
    stopped = state["stopped"]
    known = StopReason.__members__.get(stopped)
    print(
        f"  reach: stopped because {describe_stop(known) if known else stopped}"
        f" (asked from {from_epoch_ms(state['since_ms']).isoformat()},"
        f" venue clock {from_epoch_ms(state['venue_clock_ms']).isoformat()})"
    )


def _print_scans(store: ResearchStore, *, coin: str, interval: str, funding: bool) -> None:
    """Print the gap scan for what the caller just touched, candles first.

    Each scan is followed by the series' recorded reach, because the two
    answer different halves of "is this store fit to measure on": the scan
    says whether what is here is a grid, the reach says whether it is the span
    that was asked for.

    The daily backdrop is scanned beside whichever interval was asked for,
    because every experiment reads it: ``close_1d`` and ``sma_1d_*`` are daily
    features whatever the decision interval. A store holding a clean 4h series
    and no daily one is not fit to measure on, and a scan of the 4h series
    alone said "no gaps" about it. When the backdrop is absent the line says
    what lands it, since "no rows stored" under a series nobody asked about
    would read as noise.
    """
    backdrop = interval != DAILY_INTERVAL
    series = [(scan_candles(store, coin=coin, interval=interval), interval)]
    if backdrop:
        series.append((scan_candles(store, coin=coin, interval=DAILY_INTERVAL), DAILY_INTERVAL))
    if funding:
        series.append((scan_funding(store, coin=coin), FUNDING_SERIES))
    for report, name in series:
        for line in render_report(report):
            print(line)
        _print_reach(store, coin=coin, series=name)
        if backdrop and name == DAILY_INTERVAL and report.rows == 0:
            print(
                "  every experiment reads the daily backdrop (close_1d, sma_1d_*); "
                f"`fetch --coin {coin} --interval {DAILY_INTERVAL} --skip-funding` lands it"
            )


def _cmd_fetch(args: argparse.Namespace) -> int:
    since = _parse_since(args.since)
    # The reader is built — and the venue clock read — BEFORE the store is
    # opened, so a network or credential problem does not leave a freshly
    # created empty store behind on a path the operator mistyped.
    from .upstream import build_market_data

    market = build_market_data()
    # Read-before-fetch: every window below is cut at THIS reading, so a bar
    # that closes while the backfill runs is left for the next fetch instead
    # of being served with the OHLCV it had before it closed (issue #124).
    coin = _coin(args)
    end = market.get_exchange_time(coin)
    print(f"venue clock: {end.isoformat()}")
    with ResearchStore(_store_path(args)) as store:
        print(f"store: {store.path} (schema v{store.version})")
        results = [
            backfill_candles(
                market, store, coin=coin, interval=args.interval, since=since, end=end
            )
        ]
        if not args.skip_funding:
            start = _funding_start(store, coin=coin, since=since, end=end, resume=args.resume)
            if start is not None:
                results.append(
                    backfill_funding(market, store, coin=coin, since=start, end=end)
                )
        for result in results:
            print(render_fetch(result))
        _print_scans(store, coin=coin, interval=args.interval, funding=not args.skip_funding)
    return 0


def _funding_start(
    store: ResearchStore, *, coin: str, since: datetime, end: datetime, resume: bool
) -> datetime | None:
    """``--since``, or under ``--resume`` the start the walk's own rule picks - said either way.

    Said, because the recorded reach will name this start as what was asked
    from, and an operator reading "asked from" a date they never typed
    should have seen where it came from. The trade-off between the two
    starts is the flag's help text.

    ``None`` when there is nothing to walk: only a resume can get there (a
    ``--since`` at or past the clock is refused by the candle walk first),
    when the newest stored settlement is at or past the venue clock. Said
    here as one line, because the walk's own refusal would blame a
    ``--since`` the operator never typed, and the funding scan printed after
    it then describes what was already stored.
    """
    if not resume:
        return since
    start = funding_resume_start(store, coin=coin, since=since)
    if start >= end:
        print(
            "funding: the store already holds every settlement up to the venue clock; "
            "nothing to walk (the funding scan and reach below are not this run's)"
        )
        return None
    if start == since:
        print("funding: nothing newer than --since is stored; walking from --since")
    else:
        print(f"funding: resuming from {start.isoformat()}, just past the newest stored settlement")
    return start


def _cmd_gaps(args: argparse.Namespace) -> int:
    with ResearchStore(_store_path(args)) as store:
        print(f"store: {store.path} (schema v{store.version})")
        _print_scans(store, coin=_coin(args), interval=args.interval, funding=True)
    return 0


def _cmd_vocab(_args: argparse.Namespace) -> int:
    for line in describe_vocabulary():
        print(line)
    return 0


def _read_spec(path_text: str) -> StrategySpec:
    path = Path(path_text)
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        # Named, and named as a SPEC failure: the command's whole job is to
        # say whether this document is usable, and "it could not be read" is
        # one of the answers to that. Left to propagate it would be an OSError
        # traceback, which reads as a defect in this package rather than as
        # the mistyped path it is.
        raise SpecError(f"cannot read --spec {path}: {exc}") from exc
    return load_spec(text)


def _cmd_validate_spec(args: argparse.Namespace) -> int:
    for line in describe_spec(_read_spec(args.spec)):
        print(line)
    return 0


# -- the ledger commands -----------------------------------------------------


def _open_ledger(args: argparse.Namespace) -> tuple[ResearchStore, Ledger]:
    store = ResearchStore(_store_path(args))
    print(f"store: {store.path} (schema v{store.version})")
    return store, Ledger(store)


def _cmd_experiment(args: argparse.Namespace) -> int:
    # The feature stack is imported here, inside the command that computes:
    # ``report`` and the language commands must not pay for pandas.
    from .features import LIVE_CANDLE_LOOKBACK
    from .research import plan_experiment

    costs = CostModel(
        taker_fee_rate=args.taker_fee_rate,
        maker_fee_rate=args.maker_fee_rate,
        slippage_bps=args.slippage_bps,
        fill_role=FillRole(args.fill_role),
        leverage=args.leverage,
    )
    penalty = Penalty(sharpe_base=args.sharpe_base, k=args.penalty_k)
    lookback = LIVE_CANDLE_LOOKBACK if args.indicator_lookback is None else args.indicator_lookback
    store, ledger = _open_ledger(args)
    with store:
        pinned = ledger.holdout_pin(_coin(args))
        experiment = plan_experiment(
            ledger,
            experiment_id=args.name,
            coin=_coin(args),
            interval=args.interval,
            costs=costs,
            indicator_lookback=lookback,
            penalty=penalty,
            train_share=args.train_share,
            validation_share=args.validation_share,
            notes=args.notes,
        )
        if not args.dry_run:
            experiment = ledger.create_experiment(experiment)
        for line in experiment.describe():
            print(line)
        print(_shares(experiment))
        holdout = from_epoch_ms(experiment.split.holdout.start_ms).isoformat()
        if pinned is not None:
            pin = (
                "the start every experiment on this coin already withholds — the share flags "
                "only divide train from validation before it"
            )
        elif args.dry_run:
            pin = f"which this experiment would pin for every later one on {experiment.coin}"
        else:
            pin = f"now pinned for every later experiment on {experiment.coin}"
        print(f"holdout begins at {holdout}, {pin}")
        print(
            "train begins at the first bar every feature of the vocabulary has a value "
            "(with room for the deepest offset), so no legal spec is refused for warm-up"
        )
        if args.dry_run:
            print("dry run: no experiment was written, and no holdout was pinned")
    return 0


def _shares(experiment: Experiment) -> str:
    """The shares the split actually has, in bars — under a pin they are not the flags'."""
    windows = experiment.split.ordered
    total = windows[-1].end_ms - windows[0].start_ms
    return "actual shares of the span: " + ", ".join(
        f"{window.name.value} {(window.end_ms - window.start_ms) / total:.1%}" for window in windows
    )


def _describe_trial(experiment: Experiment, trial: Trial | SearchTrial) -> list[str]:
    """One trial as lines; a search view says its holdout is withheld, never "not promoted"."""
    head = f"trial #{trial.trial_id} of {experiment.experiment_id} ("
    if isinstance(trial, Trial):
        head += f"{trial.status.value}, measured {trial.created_at}"
        if trial.promoted_at:
            head += f", promoted {trial.promoted_at}"
        because = "not promoted"
    else:
        head += f"measured {trial.created_at}"
        because = "not shown to a search; `report` shows a promoted trial's"
    return [head + ")"] + describe_measurement(
        trial.spec,
        experiment.costs,
        experiment.split,
        experiment.indicator_lookback,
        trial.segments,
        withheld_because=because,
    )


def _describe_verdict(experiment: Experiment, verdict: Verdict) -> list[str]:
    lines = [experiment.penalty.describe(verdict.trials)]
    if verdict.eligible:
        lines.append("eligible: `promote` would measure its holdout")
    else:
        lines += [f"not eligible: {blocker}" for blocker in verdict.blockers]
    return lines


def _cmd_evaluate(args: argparse.Namespace) -> int:
    from .evaluator import describe_result
    from .research import measure

    spec = _read_spec(args.spec)
    store, ledger = _open_ledger(args)
    with store:
        experiment = ledger.experiment(args.experiment)
        measurement = measure(ledger, experiment, spec)
        if measurement.duplicate:
            print(
                f"this rule was already measured in {experiment.experiment_id} as trial "
                f"#{measurement.trial.trial_id}; nothing was measured or filed"
            )
            # The search view: resubmitting a rule is not a way to read its holdout.
            lines = _describe_trial(experiment, measurement.trial)
        else:
            lines = describe_result(measurement.result)
            lines.append(
                f"filed as trial #{measurement.trial.trial_id} of {experiment.experiment_id}"
            )
            if measurement.funding_holes:
                lines.append(
                    f"note: {measurement.funding_holes} hole(s) in the funding settlements this "
                    f"measurement reads, within the coverage the features allow"
                )
        for line in lines + _describe_verdict(experiment, measurement.verdict):
            print(line)
    return 0


def _cmd_promote(args: argparse.Namespace) -> int:
    from .research import promote

    store, ledger = _open_ledger(args)
    with store:
        experiment = ledger.experiment(args.experiment)
        trial = promote(ledger, experiment, args.trial)
        print(f"promoted trial #{trial.trial_id}; its holdout was measured once, below")
        for line in _describe_trial(experiment, trial):
            print(line)
        print(_looks(ledger, experiment.coin))
    return 0


def _looks(ledger: Ledger, coin: str) -> str:
    looks = ledger.holdout_looks(coin)
    return (
        f"{coin}'s holdout has been measured {looks} time(s), across every experiment on it; "
        f"each promotion is another look at the same window"
    )


def _rank(trial: Trial) -> tuple[bool, float, int]:
    """Ruined last, whatever its ratios; then validation net Sharpe, best first."""
    return (trial.validation.ruined, -trial.validation.net.sharpe, trial.trial_id)


def _cmd_report(args: argparse.Namespace) -> int:
    if args.trial is not None and args.experiment is None:
        raise ValueError("--trial names a trial inside an experiment; give --experiment too")
    store, ledger = _open_ledger(args)
    with store:
        if args.experiment is None:
            experiments = ledger.experiments()
            if not experiments:
                print("no experiments in this store — `experiment` creates one")
            for experiment in experiments:
                count, promoted = ledger.trial_counts(experiment.experiment_id)
                print(
                    f"{experiment.experiment_id}: {experiment.coin} {experiment.split.interval}, "
                    f"{count} trial(s), {promoted} promoted, created {experiment.created_at}"
                )
            return 0
        experiment = ledger.experiment(args.experiment)
        if args.trial is not None:
            trial = ledger.trial(experiment.experiment_id, args.trial)
            for line in _describe_trial(experiment, trial):
                print(line)
            return 0
        for line in experiment.describe():
            print(line)
        trials = ledger.trials(experiment.experiment_id)
        if not trials:
            print("no trials yet — `evaluate` files one")
            return 0
        print(experiment.penalty.describe(ledger.rules_tried(experiment.coin)))
        print(_looks(ledger, experiment.coin))
        _print_answers(ledger, experiment.experiment_id)
        families = Counter(trial.family for trial in trials)
        print("by family: " + ", ".join(f"{name} {n}" for name, n in sorted(families.items())))
        for trial in sorted(trials, key=_rank):
            validation = trial.validation
            line = (
                f"  #{trial.trial_id} {trial.family}: train sharpe {trial.train.net.sharpe:.2f}, "
                f"validation sharpe {validation.net.sharpe:.2f} net "
                f"{validation.net.total_return:+.2%} over {validation.trades} trades"
            )
            if validation.ruined:
                line += " — RUINED"
            if trial.status is TrialStatus.PROMOTED and trial.holdout is not None:
                line += (
                    f"; holdout sharpe {trial.holdout.net.sharpe:.2f} "
                    f"net {trial.holdout.net.total_return:+.2%} (promoted)"
                )
            print(line)
    return 0


# How many refused answers ``report`` shows. Fewer than the loop's own prompt
# carries: this is an operator glancing at what a model keeps getting wrong,
# not the feedback the model is steered by.
_REFUSALS_IN_REPORT = 3


def _print_answers(ledger: Ledger, experiment_id: str) -> None:
    """What a hypothesis loop has answered against this experiment, if anything.

    Printed by ``report`` because the trials list structurally cannot show it:
    an answer the parser refused never became a trial, so an experiment where a
    model produced forty malformed answers and two rules looks, in the trials
    list, exactly like one where it produced two rules and nothing else.
    """
    counts = ledger.proposal_counts(experiment_id)
    if not sum(counts.values()):
        return
    print(
        "answers from a hypothesis loop: "
        + ", ".join(f"{count} {outcome.value}" for outcome, count in counts.items())
    )
    for refusal in ledger.refusals(experiment_id, _REFUSALS_IN_REPORT):
        print(f"  refused: {refusal.refusal}")


def _cmd_research(args: argparse.Namespace) -> int:
    # Imported inside the command: the loop reaches the evaluator, so importing
    # it at module scope would put pandas behind ``vocab`` and ``report``.
    from .hypothesis import (
        INTERRUPTED,
        ChatHypothesist,
        build_system_prompt,
        build_user_prompt,
        search,
    )

    store, ledger = _open_ledger(args)
    with store:
        experiment = ledger.experiment(args.experiment)
        if args.dry_run:
            print(build_system_prompt())
            print()
            print(build_user_prompt(ledger, experiment))
            print()
            print("dry run: no model was asked, and nothing was filed")
            return 0
        missing = [name for name in ("provider", "model") if getattr(args, name) is None]
        if missing:
            raise ValueError(
                f"`research` needs {' and '.join('--' + name for name in missing)} \u2014 which "
                f"model proposed a rule is part of what the trial means, so there is no "
                f"default. Use --dry-run to see the prompt without asking a model."
            )
        extra = {} if args.temperature is None else {"temperature": args.temperature}
        hypothesist = ChatHypothesist.build(args.provider, args.model, args.base_url, **extra)
        report = search(
            ledger,
            experiment,
            hypothesist,
            model=f"{args.provider}/{args.model}",
            max_trials=args.max_trials,
            # Printed as each round lands rather than at the end: a ten-round
            # run is minutes of model calls, and an operator watching it should
            # not have to wait for the summary to learn the first answer was
            # refused for a reason they could have fixed.
            on_round=lambda completed: print(completed.describe()),
        )
        for line in report.describe():
            print(line)
        print(_looks(ledger, experiment.coin))
        if report.stopped == INTERRUPTED:
            # Re-raised rather than answered here: ``main``'s handler is the one
            # place that decides what an interrupt exits with, for every command.
            # The partial report above is the reason ``search`` caught it at all,
            # and catching it must not also reclassify a cancellation as a
            # failure - a script reading 130 as "the operator stopped this"
            # would otherwise page someone for a Ctrl-C that landed during the
            # model call rather than a millisecond earlier.
            raise KeyboardInterrupt
        if report.stopped is not None:
            # The rounds that filed are reported above and their rows are in the
            # store; the command still fails, because the run did not do what it
            # was asked to do.
            print(f"error: {report.stopped}", file=sys.stderr)
            return 1
    return 0


def _cmd_calibrate(args: argparse.Namespace) -> int:
    from .baselines import describe_calibration
    from .research import calibrate

    store, ledger = _open_ledger(args)
    with store:
        experiment = ledger.experiment(args.experiment)
        for line in experiment.describe():
            print(line)
        for line in describe_calibration(calibrate(ledger, experiment)):
            print(line)
        print("baselines are not trials: nothing was filed, and the promote threshold is unchanged")
    return 0


def _cmd_signal(args: argparse.Namespace) -> int:
    # Inside the command, like the other computing ones: this replays a rule
    # over the whole store and pays for the feature stack.
    from .signal import build_signal, describe_signal, write_signal

    store, ledger = _open_ledger(args)
    with store:
        signal, experiment, trial = build_signal(
            ledger, args.coin, allow_taker=args.allow_taker
        )
        target = write_signal(args.out, signal)
    for line in describe_signal(signal, experiment, trial):
        print(line)
    print(f"wrote {target}")
    return 0


_COMMANDS = {
    "fetch": _cmd_fetch,
    "gaps": _cmd_gaps,
    "vocab": _cmd_vocab,
    "validate-spec": _cmd_validate_spec,
    "experiment": _cmd_experiment,
    "evaluate": _cmd_evaluate,
    "promote": _cmd_promote,
    "report": _cmd_report,
    "calibrate": _cmd_calibrate,
    "research": _cmd_research,
    "signal": _cmd_signal,
}

# The two refusals that live beside the feature stack, named by module and
# class rather than imported: importing them here would make every command
# pay for pandas. A raised one means its module is already loaded, so looking
# it up in ``sys.modules`` finds exactly the class that was raised.
_MEASUREMENT_ERRORS = (
    ("contrib.autoresearch.evaluator", "EvaluationError"),
    ("contrib.autoresearch.features", "FeatureError"),
)


def _is_named_measurement_failure(exc: BaseException) -> bool:
    for module_name, class_name in _MEASUREMENT_ERRORS:
        module = sys.modules.get(module_name)
        if module is not None and isinstance(exc, getattr(module, class_name)):
            return True
    return False


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    args = _build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    try:
        return _COMMANDS[args.command](args)
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    except (
        StoreError,
        ExchangeError,
        HypothesistError,
        LedgerError,
        OSError,
        ValueError,
    ) as exc:
        # The families a well-formed invocation can still meet: this store
        # cannot be operated on, the venue failed, the model seam failed, the
        # ledger refused, a path could not be written (``signal --out`` on a
        # full or read-only filesystem, or naming a directory), or an argument
        # named a window, a spec or a split that is not one. Each
        # already carries a sentence written for an operator, so it is printed
        # as-is rather than wrapped.
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except RuntimeError as exc:
        # A history that cannot be measured on (``EvaluationError``) or a
        # feature the store cannot answer (``FeatureError``) is the same kind
        # of named refusal; any other RuntimeError is a defect and propagates.
        if not _is_named_measurement_failure(exc):
            raise
        print(f"error: {exc}", file=sys.stderr)
        return 1
