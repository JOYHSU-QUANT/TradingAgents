"""The command line: ``python -m contrib.uniswap_v3 <command>``.

- ``backfill`` reads the bars of a range from an archive node into the
  store. It can be repeated; what is stored is not read again.
- ``status`` prints what the store holds and its latest bars. With
  ``--run-id`` it goes on to print that run's holdings, value and return
  (measured as ``report`` measures it), and its latest decisions with when
  each was made; ``--json`` prints the same, with everything ``report``
  says, as one document (``status.py``). It reads no chain.
- ``backtest`` replays the stored bars of a range through the engine as the
  run ``--run-id``. With ``--fills model``, the default, fills come from
  the offline model and no chain is read. With ``--fills quoter`` each swap
  is quoted on the node at its bar's fill block, which takes an archive
  node. A new run takes its opening balances from ``--balance`` and
  ``--gas-eth``; the same command run again decides nothing twice.
- ``paper`` is one visit of the paper run ``--run-id``. It reads the bars
  the chain has closed since the run's latest decided one into the store,
  which it creates when there is none, and decides them with fills quoted
  at each bar's fill block. Run again before the next boundary, it says
  that the latest bar is already decided, and decides nothing. Catching
  up on bars whose visits were missed takes a node that still has the
  state of their blocks: an archive node, for more than a short while.
- ``fork`` replays the stored bars of a range as the fork run ``--run-id``,
  like ``backtest``, except that each bar's swaps are signed and sent on a
  local anvil fork (``--fork-url``, a loopback address; 127.0.0.1:8545
  unless given), reset to the bar's fill block and given the run's balances
  first. The wallet is checked against the run's ledger before and after
  each bar's swaps. A run whose step stopped while it was sending goes no
  further: the command prints what was written of the send beside what the
  wallet holds, and exits 1. The fork is started separately (RUNBOOK.md).
- ``report`` prints a run's return, drawdown, turnover and costs, beside
  what leaving the opening balances untouched, or in the quote token, would
  have come to, and, for a run that reads verdicts, how many decided bars
  saw a verdict on every traded token, on some and on none. It reads the
  store alone, and takes the run's config from the run.
- ``verdict`` asks the judge, the TradingAgents graph, for a verdict on
  every traded token at the latest bar whose boundary has passed, and
  writes each into the store's ``verdicts`` table under the config's
  ``verdicts.source``, with the judge's words in a sidecar beside the
  store. A verdict the store already holds is not asked for again. It
  reads no chain: the bar must be in the store already. The judge reads
  its data through the day it is asked on, so only the latest bar gets an
  honest verdict, and only within ``agent.ask_within_seconds`` of its
  boundary: later than that the tokens not yet judged are left unrated,
  with a warning and exit 0, while verdicts already stored stand; ``--at``
  names an older boundary only together with
  ``--fake-rating``, which records that rating without asking any judge,
  for a rehearsal on a store that holds no real verdicts.
- ``target`` reads the carry coordinator's handoff file (``--handoff``) and
  writes the spot weight it names into the store's ``targets`` table under
  the config's ``targets.source``, at the boundary the handoff is for. It
  reads no chain, and the bar need not be in the store: the row belongs to
  no run, and the ``external_weights`` strategy reads it when the bar is
  decided. A target the store already holds is not rewritten. The handoff
  applies while its boundary is less than a day behind the clock; one whose
  boundary is ahead, or a day or more behind, is not acted on.

``backfill``, ``paper`` and ``backtest --fills quoter`` take the node's URL
from the environment variable the config names (``ETH_RPC_URL`` unless it
names another); with the URL in a ``.env`` file, run them as
``python -m dotenv run -- python -m contrib.uniswap_v3 ...``. ``verdict``
takes the judge's API key from the environment variable its provider names
(``OPENROUTER_API_KEY`` for OpenRouter), the same way.

Exit codes: 0 when the command ran to its end, 1 when it could not and
running it again unchanged will not help (the config, the store, the range,
the node's setup, or an answer of the node's that cannot be right), 3 when
the node could not be reached, is behind, or answered a read with an error,
or another program held the store for longer than a command waits, and a
later run may succeed. 2 is argparse's, for a command line it cannot
read. A ``backfill`` that ran to its end exits 0 even when it left
boundaries without an answer: its last lines count them, and a warning on
stderr gives their count and the first and last of them. A ``backfill``
whose node has not reached a boundary that passed five minutes or more
ago exits 3: the node's head is behind. A ``backtest`` that ran to its end
exits 0 even when boundaries of its range had no bar: they are not decided,
its first line counts them, and a warning on stderr gives the first and
last of them. It warns on stderr as well, and still exits 0, when bars were
decided on readings that were not final yet, when a bar decided earlier now
reads differently in the store, and when rebalances were rejected for want
of gas. A range without a single bar exits 1, and so does carrying a run on
in a way that would leave a stored bar undecided behind it: from a later
``--from``, or over a boundary given its bar after the run had passed it.
A ``paper`` visit exits 0 once the latest bar is decided, by this visit or
an earlier one and whatever the decision, and also when the chain had no
answer at the bar's boundary. It warns on stderr, still exiting 0, of a
boundary without an answer, of a rebalance that was rejected, of a bar
skipped as suspect, of stored readings a check found to be off the
final chain and, for a run that reads verdicts, of bars decided with no
rating on some traded token. It exits 3, having decided nothing of the latest bar, when
the node's chain has not yet reached the boundary or the bar's fill block,
and when a quote reverted without a reason of a pool's: whoever schedules
the visit runs it again later. It exits 1 when its clock is behind the run.
A ``fork`` run exits as a ``backtest`` does, and also 1 when the fork is not
an anvil fork on a loopback address, when the wallet does not hold what the
run's ledger says, and when the run has an open send.
A ``verdict`` exits 0 once every traded token has a verdict at the bar, by
this visit or an earlier one, and also when the bar is suspect, which no run
decides, so no judge is asked, and when the boundary passed longer ago than
the judge's window, so the tokens not yet judged are left unrated, warned of
on stderr. It warns on stderr of a verdict that holds no
rating (``REVIEW``) as it is given. It exits 3, keeping the verdicts given so far, when the
store has no bar at the boundary yet, and when the judge did not answer for
a reason that may pass (the provider's rate limit, timeout, server error or
empty balance, a network error, a data vendor throttled or down): whoever schedules it
runs it again later. It exits 1, keeping the verdicts given so far as well,
when the config reads no verdicts, when a traded token has no ticker, when
the judge cannot be set up or built (the engine is not installed, its
provider's key is not in the environment) or failed for good (any other
refusal of the provider's, such as a model it does not serve, or an error
inside the engine), when the sidecar cannot be written, when ``--at`` names
an older bar than the latest without ``--fake-rating``, and when
``--fake-rating`` meets a store that holds real verdicts of the source.
A ``target`` exits 0 once the handoff's target is in the store, written by
this visit or found there (a stored weight that differs from the handoff's
is warned of on stderr, and stands). It exits 3, writing nothing, when the
handoff is not there yet, or is for a boundary the clock has not reached or
left a day or more ago: a later visit may find it in its window, or
replaced. It exits 1 when the config reads no targets or trades any token
but the handoff's, when the file cannot be read or is not the handoff this
reader speaks (not JSON, another version, a weight outside 0 to 1, a boundary
that is not a UTC day's), when its boundary lies more than a day ahead, and
when the store does not exist. (A config that reads targets and has bars
that are not a day long is refused by every command, as the config's own
rule.)
"""

from __future__ import annotations

import argparse
import sys
import time as _time
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Final

from .agent.errors import AgentError, BarNotStored, JudgeUnavailable
from .chain.errors import ChainError, RpcRejected, TransientChainError
from .config import ConfigError, UniswapConfig, config_from_snapshot, load_config
from .domain.decimal_context import fixed_text, parse_decimal, plain
from .domain.execution import ForkSettings
from .domain.ledger import Ledger
from .domain.metrics import Curve, MetricsError
from .domain.records import Outcome
from .domain.targets import Target, TargetRecord
from .domain.text import counts, one_ascii_line
from .domain.times import utc_text
from .domain.verdicts import RATINGS, Rating, bytes_digest
from .engine.backtest import BacktestRangeError, BacktestSummary, run_backtest
from .engine.executors import QuoteExecutor
from .engine.step import EngineError, UnsettledSend, holdings_text
from .handoff import (
    HandoffError,
    HandoffNotApplicable,
    check_applicable,
    read_handoff,
    target_from_document,
)
from .status import (
    decided_span,
    measured,
    outcome_counts,
    read_status,
    reason_code_counts,
    run_header,
    status_json,
    status_lines,
    stored_run,
    unsettled_count,
    verdicts_seen,
)
from .store.repository import Store, StoreBusy, StoreError, open_store

if TYPE_CHECKING:
    from .agent.graph import Judge
    from .agent.verdicts import Asked
    from .backfill import BackfillSummary
    from .chain.rpc import Rpc
    from .fork_run import Reconciliation

__all__ = ["EXIT_FAILED", "EXIT_OK", "EXIT_RETRY", "main"]

EXIT_OK: Final = 0
EXIT_FAILED: Final = 1
EXIT_RETRY: Final = 3

# How long after a boundary a node's head may still be short of it before
# the node counts as behind. Mainnet makes a block every twelve seconds.
_HEAD_LAG_SECONDS: Final = 300
# What a dry run plans against when there is no store yet: an empty one,
# in memory, so that it leaves no file behind.
_NO_STORE: Final = Path(":memory:")
_REPORT_SUMMARY: Final = "Print a run's return, drawdown, turnover and costs."


def _parse_time(text: str) -> int:
    """A UTC time as ``2024-01-01`` or ``2024-01-01T12:00:00``, in epoch seconds."""
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"{text!r} is not a date such as 2024-01-01 or a time such as 2024-01-01T12:00:00"
        ) from None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    if moment.microsecond:
        raise argparse.ArgumentTypeError(f"{text!r} must be in whole seconds")
    time = int(moment.timestamp())
    try:
        _iso(time)
    except (OSError, OverflowError, ValueError):
        # The platform cannot turn it back into a date to print.
        raise argparse.ArgumentTypeError(f"{text!r} is outside the times supported") from None
    return time


def _positive(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        value = 0
    if value < 1:
        raise argparse.ArgumentTypeError(f"{text!r} is not a positive integer")
    return value


def _run_id(text: str) -> str:
    if not text.strip():
        raise argparse.ArgumentTypeError("a run id is not empty")
    return text


def _amount(text: str) -> Decimal:
    try:
        amount = parse_decimal(text, "an amount")
    except ValueError:
        amount = None
    if amount is None or amount.is_signed():
        raise argparse.ArgumentTypeError(f"{text!r} is not an amount such as 10000 or 0.5")
    return amount


def _balance(text: str) -> tuple[str, Decimal]:
    symbol, separator, amount = text.partition("=")
    if not separator or not symbol.strip():
        raise argparse.ArgumentTypeError(f"{text!r} is not a balance such as USDC=10000")
    return symbol, _amount(amount)


_iso = utc_text
_fixed = fixed_text


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m contrib.uniswap_v3", description="Uniswap v3 spot execution."
    )
    commands = parser.add_subparsers(dest="command", required=True)

    def add(name: str, summary: str) -> argparse.ArgumentParser:
        command = commands.add_parser(name, help=summary, description=summary)
        command.add_argument("--config", type=Path, required=True, help="the YAML config file")
        command.add_argument("--db", type=Path, required=True, help="the SQLite store file")
        command.add_argument(
            "--interval-seconds",
            type=_positive,
            help="the bar length, in place of the config's bars.interval_seconds",
        )
        return command

    backfill = add("backfill", "Read the bars of a range from an archive node into the store.")
    backfill.add_argument(
        "--from",
        dest="start",
        type=_parse_time,
        required=True,
        help="the first bar boundary, in UTC: 2024-01-01 or 2024-01-01T12:00:00",
    )
    backfill.add_argument(
        "--to",
        dest="end",
        type=_parse_time,
        help="the range ends at the last boundary at or before this time (default: now)",
    )
    backfill.add_argument(
        "--dry-run", action="store_true", help="print what would be read, and read nothing"
    )

    status = add("status", "Print what the store holds and its latest bars, and a run's state.")
    status.add_argument(
        "--bars",
        type=_positive,
        default=5,
        help="how many of the latest bars, and of the run's latest decisions, to print",
    )
    status.add_argument(
        "--run-id",
        type=_run_id,
        help="also print this run's holdings, value, return and latest decisions",
    )
    status.add_argument(
        "--json",
        action="store_true",
        help="print the same as one JSON document (schema_version and source first)",
    )

    def add_run(command: argparse.ArgumentParser) -> None:
        command.add_argument(
            "--run-id", type=_run_id, required=True, help="the run to start or carry on"
        )
        command.add_argument(
            "--balance",
            dest="balances",
            type=_balance,
            action="append",
            default=[],
            metavar="TOKEN=AMOUNT",
            help="an opening balance of a new run, in whole tokens: USDC=10000. Repeat it "
            "for each token held; a token left out starts at zero. Given for a stored run, "
            "the balances must be the ones it was started with",
        )
        command.add_argument(
            "--gas-eth",
            type=_amount,
            metavar="AMOUNT",
            help="the ETH a new run sets aside for gas: 0.5. It goes with --balance",
        )

    def add_range(command: argparse.ArgumentParser) -> None:
        command.add_argument(
            "--from",
            dest="start",
            type=_parse_time,
            required=True,
            help="the first bar boundary, in UTC: 2024-01-01 or 2024-01-01T12:00:00",
        )
        command.add_argument(
            "--to",
            dest="end",
            type=_parse_time,
            help="the range ends at the last boundary at or before this time "
            "(default: the store's latest bar)",
        )

    backtest = add("backtest", "Replay the stored bars of a range through the engine.")
    add_run(backtest)
    add_range(backtest)
    backtest.add_argument(
        "--fills",
        choices=["model", "quoter"],
        default="model",
        help="where fills come from: the offline model (the bar's close, less fees, "
        "execution.model.slippage and modelled gas), or the quoter (what the pools would "
        "have returned at the bar's fill block, asked of an archive node)",
    )

    paper = add(
        "paper", "Read the bars the chain has closed since the run's last, and decide them."
    )
    add_run(paper)

    fork = add(
        "fork", "Replay the stored bars of a range, signing each bar's swaps on a local fork."
    )
    add_run(fork)
    add_range(fork)
    fork.add_argument(
        "--fork-url",
        help="the anvil fork's URL, on a loopback address (default: http://127.0.0.1:8545)",
    )

    verdict = add(
        "verdict", "Ask the judge for a verdict on every traded token at a bar, and record it."
    )
    verdict.add_argument(
        "--at",
        type=_parse_time,
        help="the bar boundary to judge, in UTC: 2024-01-01 or 2024-01-01T12:00:00 "
        "(default: the latest boundary that has passed, which is the only one a judge is "
        "asked about; an older one takes --fake-rating)",
    )
    verdict.add_argument(
        "--fake-rating",
        choices=[rating.value for rating in RATINGS],
        help="record this rating for every token without asking any judge, on a store that "
        "holds no real verdicts of the source",
    )

    target = add("target", "Record the carry handoff's spot target at the boundary it is for.")
    target.add_argument(
        "--handoff",
        type=Path,
        required=True,
        help="the handoff file the carry coordinator writes (contrib/carry signal --out)",
    )

    report = commands.add_parser(
        "report", help=_REPORT_SUMMARY, description=_REPORT_SUMMARY
    )
    report.add_argument("--db", type=Path, required=True, help="the SQLite store file")
    report.add_argument("--run-id", type=_run_id, required=True, help="the run to report on")
    return parser


def _config(args: argparse.Namespace) -> UniswapConfig:
    config = load_config(args.config)
    if args.interval_seconds is None:
        return config
    try:
        bars = replace(config.bars, interval_seconds=args.interval_seconds)
    except ValueError as exc:
        raise ConfigError(f"--interval-seconds: {exc}") from exc
    return replace(config, bars=bars)


def _connect(config: UniswapConfig, command: str) -> Rpc:
    """A connection to the config's node, for ``command``."""
    # Imported here: a command that reads no chain should not wait on web3.
    try:
        from .chain.rpc import RpcSettings, connect
    except ImportError as exc:
        raise ConfigError(
            f"{command} needs the packages in contrib/uniswap_v3/requirements.txt ({exc})"
        ) from exc
    settings = RpcSettings() if config.rpc_url_env is None else RpcSettings(config.rpc_url_env)
    return connect(config.chain_id, settings=settings)


def _pending_checked(read: BackfillSummary, out: Callable[[str], None]) -> None:
    """Say how the pending readings a backfill checked came out, when it checked any."""
    if read.confirmed or read.reorged:
        out(
            f"checked pending readings against the final chain: {read.confirmed} final, "
            f"{read.reorged} reorged"
        )


def _backfill(args: argparse.Namespace, out: Callable[[str], None], now: Callable[[], float]) -> int:
    # Imported here: ``status`` reads no chain and should not wait on web3.
    try:
        from .backfill import BackfillRangeError, backfill, check_range, plan_backfill
    except ImportError as exc:
        raise ConfigError(
            f"backfill needs the packages in contrib/uniswap_v3/requirements.txt ({exc})"
        ) from exc

    config = _config(args)
    # No boundary after this moment exists yet, whatever --to says.
    present = int(now())
    end = present if args.end is None else min(args.end, present)
    try:
        check_range(config, start=args.start, end=end)
    except BackfillRangeError as exc:
        print(f"failed: {exc}", file=sys.stderr)
        return EXIT_FAILED

    on_disk = not args.dry_run or args.db.is_file()
    with open_store(args.db if on_disk else _NO_STORE) as store:
        plan = plan_backfill(store, config, start=args.start, end=end)
        out(
            f"{plan.boundaries} bar(s) from {_iso(args.start)} to {_iso(end)}: "
            f"{len(plan.missing)} to read, about {plan.requests} request(s)"
        )
        if args.dry_run:
            return EXIT_OK
        rpc = _connect(config, "backfill")
        summary = backfill(
            rpc,
            store,
            config,
            start=args.start,
            end=end,
            report=lambda time, text: out(f"{_iso(time)}  {one_ascii_line(text)}"),
        )
    out(
        f"wrote {summary.written} bar(s); {summary.already_stored} already stored, "
        f"{len(summary.unanswered)} without an answer, {len(summary.not_reached)} not reached yet"
    )
    _pending_checked(summary, out)
    if summary.unanswered:
        print(
            f"warning: {len(summary.unanswered)} boundary(ies) without an answer, from "
            f"{_iso(summary.unanswered[0])} to {_iso(summary.unanswered[-1])}; a later run "
            f"asks again",
            file=sys.stderr,
        )
    overdue = [time for time in summary.not_reached if time <= present - _HEAD_LAG_SECONDS]
    if overdue:
        print(
            f"try again later: the node's head is behind; {len(overdue)} boundary(ies) from "
            f"{_iso(overdue[0])} have passed and are not on its chain yet",
            file=sys.stderr,
        )
        return EXIT_RETRY
    return EXIT_OK


def _status(args: argparse.Namespace, out: Callable[[str], None], now: Callable[[], float]) -> int:
    config = _config(args)
    # One view for all the reads: a paper visit may be writing meanwhile.
    with open_store(args.db, create=False) as store, store.reading():
        status = read_status(store, config, bars=args.bars, run_id=args.run_id, now=now)
    if args.json:
        out(status_json(status))
        return EXIT_OK
    for line in status_lines(status):
        out(line)
    return EXIT_OK


def _opening(args: argparse.Namespace, config: UniswapConfig) -> Ledger | None:
    """The opening balances the command line gives, or ``None`` when it gives none."""
    if not args.balances and args.gas_eth is None:
        return None
    if not args.balances or args.gas_eth is None:
        raise ConfigError("opening balances take both --balance and --gas-eth")
    balances = {token.symbol: Decimal(0) for token in config.tokens}
    given: set[str] = set()
    for symbol, amount in args.balances:
        if symbol not in balances:
            raise ConfigError(
                f"--balance names {symbol!r} which the config does not list; the config's "
                f"tokens are {sorted(balances)}"
            )
        if symbol in given:
            raise ConfigError(f"--balance names {symbol!r} more than once")
        given.add(symbol)
        balances[symbol] = amount
    try:
        return Ledger(balances=balances, gas_eth=args.gas_eth)
    except ValueError as exc:
        raise ConfigError(f"the opening balances cannot be held ({exc})") from exc


def _quoting(config: UniswapConfig, command: str) -> tuple[Rpc, QuoteExecutor]:
    """A connection to the config's node, and an executor that fills from its quotes."""
    try:
        from .chain.gas import ChainGasOracle
        from .chain.quoter import ChainQuoter
    except ImportError as exc:
        raise ConfigError(
            f"{command} needs the packages in contrib/uniswap_v3/requirements.txt ({exc})"
        ) from exc
    rpc = _connect(config, command)
    return rpc, QuoteExecutor(ChainQuoter(rpc), ChainGasOracle(rpc), config.execution)


def _backtest(args: argparse.Namespace, out: Callable[[str], None], now: Callable[[], float]) -> int:
    config = _config(args)
    opening = _opening(args, config)
    executor = None
    if args.fills == "quoter":
        _, executor = _quoting(config, "backtest --fills quoter")
    # A backtest's rows can be made again, so its commits need not wait for the disk.
    with open_store(args.db, create=False, durable=False) as store:
        summary = run_backtest(
            store,
            config,
            run_id=args.run_id,
            start=args.start,
            end=args.end,
            opening=opening,
            now=int(now()),
            executor=executor,
        )
    _replayed(args.run_id, summary, out)
    return EXIT_OK


def _replayed(
    run_id: str,
    summary: BacktestSummary,
    out: Callable[[str], None],
    *,
    paper: bool = False,
    signs: bool = False,
) -> None:
    """Print what a replay did, and warn on stderr of what a reader should know of it.

    A paper visit, ``paper``, is watched by its exit code and stderr alone, and so is
    warned of a rebalance the executor rejected, of a bar skipped as suspect, for
    a run that reads verdicts, of a bar decided with no rating on some traded token,
    and, for a run that reads targets, of a bar decided with no target on some traded
    token, which in a backtest are counted and no more. It is not warned of bars decided on readings
    that were not final: a visit made on time always decides its latest bar on one.
    A run whose swaps are signed, ``signs``, is warned of rejected rebalances as well,
    and of those left partial, which hold a wallet half rebalanced.
    """
    out(
        f"run {one_ascii_line(run_id)}: {summary.boundaries} boundary(ies) from {_iso(summary.start)} to "
        f"{_iso(summary.end)}: {summary.decided} decided, {summary.already_decided} already "
        f"decided, {len(summary.missing)} without a bar"
    )
    out(outcome_counts(summary.outcomes))
    warnings = []
    if summary.missing:
        later = (
            "A later visit asks the chain again for those after the run's latest decided "
            "bar; one the run has gone past stays undecided"
            if paper
            else "One that is backfilled later is decided by a rerun only if the run has not "
            "gone past it; otherwise the range needs a new run"
        )
        warnings.append(
            f"{len(summary.missing)} boundary(ies) without a bar, from "
            f"{_iso(summary.missing[0])} to {_iso(summary.missing[-1])}; they were not "
            f"decided. {later}"
        )
    if summary.on_pending and not paper:
        warnings.append(
            f"{summary.on_pending} bar(s) were decided on readings that are not final yet; a "
            f"decision stands even if the chain later drops the block it was made on"
        )
    if summary.changed:
        warnings.append(
            f"{len(summary.changed)} bar(s) decided earlier now read differently in the store "
            f"(another close block, or another verdict on whether they are suspect), from "
            f"{_iso(summary.changed[0])} to {_iso(summary.changed[-1])}; their decisions stand, "
            f"and a new run decides them on what the store holds now"
        )
    if summary.verdicts_changed:
        warnings.append(
            f"{len(summary.verdicts_changed)} bar(s) decided earlier now have other verdicts in "
            f"the store than their decisions saw, from {_iso(summary.verdicts_changed[0])} to "
            f"{_iso(summary.verdicts_changed[-1])}; their decisions stand, and a new run decides "
            f"them on what the store holds now"
        )
    if summary.unrated and paper:
        warnings.append(
            f"{len(summary.unrated)} bar(s) decided now, from {_iso(summary.unrated[0])} to "
            f"{_iso(summary.unrated[-1])}, saw no rating (no verdict, or a REVIEW) on some "
            f"traded token; status --run-id shows which"
        )
    if summary.untargeted and paper:
        warnings.append(
            f"{len(summary.untargeted)} bar(s) decided now, from {_iso(summary.untargeted[0])} "
            f"to {_iso(summary.untargeted[-1])}, had no target on some traded token; a strategy "
            f"that reads targets holds there (carry plan D5), and the target command writes one "
            f"from the handoff"
        )
    if summary.gas_rejected:
        warnings.append(
            f"{len(summary.gas_rejected)} rebalance(s) were rejected because the gas balance "
            f"did not cover them, the first at {_iso(summary.gas_rejected[0])}; nothing tops a "
            f"run's gas balance up"
        )
    partial = summary.outcomes.get(Outcome.PARTIAL, 0)
    if partial:
        warnings.append(
            f"{partial} rebalance(s) were left partial: a swap was refused after earlier ones "
            f"had filled on the chain, which stand; the next bar is decided from there"
        )
    if summary.executor_rejected and (paper or signs):
        warnings.append(
            f"{len(summary.executor_rejected)} rebalance(s) were rejected because a swap was "
            f"refused, the first at {_iso(summary.executor_rejected[0])}; the decision keeps "
            f"why, and a rejected rebalance is not tried again"
        )
    if summary.skipped and paper:
        warnings.append(
            f"{len(summary.skipped)} bar(s) were skipped as suspect, the first at "
            f"{_iso(summary.skipped[0])}; nothing was traded on them"
        )
    for warning in warnings:
        print(f"warning: {warning}", file=sys.stderr)


def _paper(args: argparse.Namespace, out: Callable[[str], None], now: Callable[[], float]) -> int:
    config = _config(args)
    opening = _opening(args, config)
    rpc, executor = _quoting(config, "paper")
    from .backfill import BackfillRangeError
    from .paper import run_paper

    try:
        with open_store(args.db) as store:
            summary = run_paper(
                rpc,
                store,
                config,
                executor,
                run_id=args.run_id,
                opening=opening,
                now=int(now()),
            )
    except BackfillRangeError as exc:
        # The clock puts the latest boundary where no bar can be read.
        print(f"failed: {one_ascii_line(exc)}", file=sys.stderr)
        return EXIT_FAILED
    read = summary.read
    out(
        f"read {read.written} bar(s); {read.already_stored} already stored, "
        f"{len(read.unanswered)} without an answer"
    )
    _pending_checked(read, out)
    if read.reorged:
        print(
            f"warning: {read.reorged} stored reading(s) are no longer on the final chain; a "
            f"bar decided on one keeps its decision, and a new run skips it as suspect",
            file=sys.stderr,
        )
    if summary.replayed is not None:
        _replayed(args.run_id, summary.replayed, out, paper=True)
    decision = summary.decision
    if decision is None:
        # A replay has already warned of every boundary it found without a bar.
        if summary.replayed is None:
            earlier = len(read.unanswered) - 1
            others = (
                f", nor at {earlier} boundary(ies) before it, from {_iso(read.unanswered[0])}"
                if earlier > 0
                else ""
            )
            print(
                f"warning: the chain had no answer at the boundary {_iso(summary.latest)}"
                f"{others}, so nothing has a bar and nothing was decided; a later visit asks "
                f"again, and decides a bar only if the run has not gone past it",
                file=sys.stderr,
            )
        return EXIT_OK
    made = summary.replayed is not None and summary.replayed.decided
    why = "" if decision.reason is None else f" ({one_ascii_line(decision.reason)})"
    out(
        f"the bar at {_iso(summary.latest)} {'is decided' if made else 'was already decided'}: "
        f"{decision.outcome.value}{why}"
    )
    return EXIT_OK


def _reconciliation_lines(found: Reconciliation | None) -> list[str]:
    """What was written of an open send beside what the wallet holds, as lines to print."""
    if found is None:
        return ["the run has no open send any more"]
    send = found.send
    lines = [
        f"open send at the bar {_iso(send.time)}, begun {_iso(send.started_at)}: "
        f"{len(send.legs)} leg(s) filled"
    ]
    if send.failure is not None:
        lines.append(f"stopped by: {one_ascii_line(send.failure)}")
    for leg in send.legs:
        lines.append(
            f"leg {leg.leg}: {plain(leg.amount_in)} {leg.token_in} for {plain(leg.amount_out)} "
            f"{leg.token_out} in block {leg.block}, gas {plain(leg.gas_cost_eth)} ETH"
        )
    gas = (
        f"the failed swap's gas, {plain(send.failed_gas_eth)} ETH, taken off"
        if found.gas_known
        else "the failed swap's gas not known, so ETH is not compared"
    )
    lines += [
        f"ledger before the bar: {holdings_text(found.before)}",
        f"written, with the legs applied and {gas}: {holdings_text(found.expected)}",
        f"the wallet holds now: {holdings_text(found.held)}",
        "the wallet agrees with what was written"
        if found.agrees
        else "the wallet does NOT agree with what was written",
        "the run goes no further: start a new run from what the wallet holds (RUNBOOK.md, "
        "fork runs)",
    ]
    return lines


def _fork(args: argparse.Namespace, out: Callable[[str], None], now: Callable[[], float]) -> int:
    config = _config(args)
    opening = _opening(args, config)
    try:
        from .chain.fork import DEFAULT_FORK_URL, open_fork
        from .chain.rpc import RpcSettings
        from .chain.swaps import ChainExecutor, SwapSettings
        from .chain.wallet import ForkWallet
    except ImportError as exc:
        raise ConfigError(
            f"fork needs the packages in contrib/uniswap_v3/requirements.txt ({exc})"
        ) from exc
    from .fork_run import reconcile_open_send, run_fork

    settings = config.fork if config.fork is not None else ForkSettings()
    # A fork reads every account and slot it meets from its upstream node: slower than a node.
    fork = open_fork(
        config.chain_id,
        url=args.fork_url or DEFAULT_FORK_URL,
        settings=RpcSettings(timeout_seconds=60),
    )
    executor = ChainExecutor(
        fork, account=settings.account, settings=SwapSettings(settings.deadline_seconds)
    )
    wallet = ForkWallet(executor, tokens=config.tokens, settings=config.execution)
    with open_store(args.db, create=False) as store:
        try:
            summary = run_fork(
                store,
                config,
                executor,
                wallet,
                run_id=args.run_id,
                start=args.start,
                end=args.end,
                opening=opening,
                now=int(now()),
                # Kept by a run started here, and read only for one.
                fork_block=fork.fork_block() if store.run(args.run_id) is None else None,
            )
        except UnsettledSend as exc:
            print(f"failed: {one_ascii_line(exc)}", file=sys.stderr)
            try:
                found = reconcile_open_send(store, config, wallet, args.run_id)
            except Exception as failed:
                # The run needs a person either way: not a reason to say "try again later".
                print(
                    f"the open send could not be set beside the wallet: "
                    f"{type(failed).__name__}: {one_ascii_line(failed)}",
                    file=sys.stderr,
                )
                return EXIT_FAILED
            for line in _reconciliation_lines(found):
                out(line)
            return EXIT_FAILED
    _replayed(args.run_id, summary, out, signs=True)
    return EXIT_OK


def _asked_line(asked: Asked) -> str:
    """One token's verdict as ``verdict`` prints it."""
    record = asked.record
    rating = record.verdict.rating.value
    if not asked.asked_now:
        return (
            f"{asked.symbol} ({asked.ticker}): already stored: {rating}, model "
            f"{one_ascii_line(record.model)}, asked {_iso(record.asked_at)}"
        )
    words = (
        "no words kept"
        if record.sidecar_path is None
        else f"words in {one_ascii_line(record.sidecar_path)}"
    )
    return (
        f"{asked.symbol} ({asked.ticker}): {rating}, model {one_ascii_line(record.model)}, "
        f"{asked.elapsed_seconds:.0f} s, {words}"
    )


def _verdict(args: argparse.Namespace, out: Callable[[str], None], now: Callable[[], float]) -> int:
    config = _config(args)
    interval = config.bars.interval_seconds
    present = int(now())
    latest = present - present % interval
    at = latest if args.at is None else args.at
    if at % interval:
        raise ConfigError(
            f"--at {_iso(at)} is not a bar boundary: a boundary is a multiple of {interval} "
            f"seconds since the epoch"
        )
    if at > latest:
        raise ConfigError(
            f"--at {_iso(at)} has not passed; the latest boundary that has is {_iso(latest)}"
        )
    from .agent.verdicts import ask_verdicts, trade_date_of, verdict_source

    source = verdict_source(config)
    home = args.db.resolve().parent
    judge: Judge
    if args.fake_rating is not None:
        from .agent.graph import FakeJudge

        judge = FakeJudge(Rating(args.fake_rating))
    else:
        from .agent.graph import TradingAgentsJudge

        judge = TradingAgentsJudge(config.agent, home / "tradingagents")

    def recorded(asked: Asked) -> None:
        # Told as each verdict lands, so that a later token's failure loses no line.
        out(_asked_line(asked))
        if asked.superseded:
            print(
                f"warning: another visit recorded {asked.symbol} while this one's judge was "
                f"thinking; this visit's answer is dropped, and the engine's log of the day "
                f"under tradingagents/logs is the dropped one's",
                file=sys.stderr,
            )
        if asked.asked_now and asked.record.verdict.rating.is_review:
            print(
                f"warning: the judge gave no rating on {asked.symbol} (REVIEW); the verdict is "
                f"kept as such, a strategy treats it as none, and it is not asked again",
                file=sys.stderr,
            )

    out(f"verdicts of {one_ascii_line(source)} at {_iso(at)} (trade date {trade_date_of(at)}):")
    with open_store(args.db, create=False) as store:
        summary = ask_verdicts(
            store, config, judge, time=at, home=home, now=present, report=recorded
        )
    if summary.suspect:
        out(f"the bar at {_iso(at)} is suspect, which no run decides; no judge was asked")
        return EXIT_OK
    if summary.late:
        print(
            f"warning: the boundary {_iso(at)} passed {present - at} s ago, more than "
            f"agent.ask_within_seconds ({config.agent.ask_within_seconds} s); the judge would "
            f"see that long past the fill, so it is not asked, and {', '.join(summary.late)} "
            f"left unrated",
            file=sys.stderr,
        )
    window = f", {len(summary.late)} past the judge's window" if summary.late else ""
    out(f"{len(summary.asked)} asked, {len(summary.already_stored)} already stored{window}")
    return EXIT_OK


def _runs_reading(store: Store, source: str, time: int) -> list[str]:
    """The runs that read the targets of ``source`` and have decided the bar at ``time``, sorted.

    Read off each run's config snapshot: a store is shared by runs that read
    no targets (the control and AI runs decide the same bars before the
    carry run's visit), and those are not told anything by a target.
    """
    return [
        run_id
        for run_id in store.runs_decided_at(time)
        if (reads := config_from_snapshot(stored_run(store, run_id).config).targets) is not None
        and reads.source == source
    ]


def _target(args: argparse.Namespace, out: Callable[[str], None], now: Callable[[], float]) -> int:
    config = _config(args)
    settings = config.targets
    if settings is None:
        raise ConfigError(
            "target writes under the source the config's targets section names, and the config "
            "has no targets section"
        )
    raw, doc = read_handoff(args.handoff)
    found = target_from_document(doc)
    # Refused HERE, before the irrevocable write: the strategy refuses a bar with a
    # target on some traded tokens and not others, the row is never rewritten, and
    # dropping the other token from the config is another snapshot the run cannot
    # go on under, so the bar could never be decided. (The config itself holds
    # that a run reading targets takes daily bars, so the handoff's day boundary
    # is a bar boundary.)
    if config.traded_symbols != {found.token}:
        raise ConfigError(
            f"the config trades {sorted(config.traded_symbols)} and the handoff sizes "
            f"{found.token!r} alone; a run that reads targets trades the handoff's token and "
            f"no other"
        )
    present = int(now())
    check_applicable(found, now=present)
    record = TargetRecord(
        target=Target(
            source=settings.source, symbol=found.token, time=found.as_of, weight=found.weight
        ),
        written_at=present,
        digest=bytes_digest(raw),
    )
    did = f", {one_ascii_line(found.action)}" if found.action else ""
    with open_store(args.db, create=False) as store:
        out(
            f"target of {one_ascii_line(settings.source)} at {_iso(found.as_of)} from "
            f"{one_ascii_line(args.handoff.name)} ({one_ascii_line(found.coin)}{did}):"
        )
        # The insert is the check: a row already there, by an earlier visit or one
        # running beside this, is refused by the store and read back.
        try:
            store.insert_target(record)
        except StoreError:
            held = store.target(settings.source, found.token, found.as_of)
            if held is None:
                raise
        else:
            out(f"{found.token}: {plain(found.weight)} written")
            # The one ordering the schedule rules out (target before paper): said
            # here, since the row changes nothing a decision already made. Advisory:
            # the row is in, so a look that fails is said and does not fail the write.
            try:
                decided = _runs_reading(store, settings.source, found.as_of)
            except (ConfigError, StoreError) as exc:
                print(
                    f"warning: the target is written, and whether a run reading "
                    f"{settings.source} had already decided the bar at {_iso(found.as_of)} "
                    f"could not be told ({one_ascii_line(exc)})",
                    file=sys.stderr,
                )
            else:
                if decided:
                    print(
                        f"warning: {len(decided)} run(s) reading {settings.source} have already "
                        f"decided the bar at {_iso(found.as_of)} "
                        f"({', '.join(map(one_ascii_line, decided))}); the target written now "
                        f"does not change those decisions",
                        file=sys.stderr,
                    )
            return EXIT_OK
    out(
        f"{found.token}: already stored: {plain(held.target.weight)}, written "
        f"{_iso(held.written_at)}"
    )
    if held.target.weight != found.weight:
        print(
            f"warning: the handoff says {plain(found.weight)} for {found.token} at "
            f"{_iso(found.as_of)}, and the store holds {plain(held.target.weight)}; a target is "
            f"never rewritten, and the run decides on what the store holds",
            file=sys.stderr,
        )
    return EXIT_OK


def _curve_line(name: str, curve: Curve) -> str:
    change = _fixed(curve.total_return * 100, signed=True) + "%"
    fall = _fixed(curve.max_drawdown * 100) + "%"
    return f"{name:<18}{_fixed(curve.start):>14}{_fixed(curve.end):>14}{change:>11}{fall:>14}"


def _report(args: argparse.Namespace, out: Callable[[str], None]) -> int:
    # One view for all the reads: a backtest may be writing the run meanwhile.
    with open_store(args.db, create=False) as store, store.reading():
        run = stored_run(store, args.run_id)
        decisions = store.decisions(args.run_id)
        valuations = store.valuations(args.run_id)
        fills = store.fills(args.run_id)
    config = config_from_snapshot(run.config)
    measured_on, metrics = measured(run, config, decisions, valuations, fills)
    quote = run.quote
    out(
        run_header(
            run_id=run.run_id,
            mode=run.mode.value,
            fork_block=run.fork_block,
            fills=run.fills.value,
            strategy=run.strategy,
            quote=run.quote,
        )
    )
    out(decided_span(len(decisions), _iso(decisions[0].time), _iso(decisions[-1].time)))
    out("decisions: " + outcome_counts(Counter(decision.outcome for decision in decisions)))
    for outcome, codes in reason_code_counts(decisions).items():
        if codes:
            out(f"{outcome}: {counts(codes)}")
    if config.verdicts is not None:
        seen = verdicts_seen(config.verdicts.source, len(config.traded_symbols), decisions)
        out(
            f"verdicts from {seen.source}: {seen.every} decided bar(s) saw one on every traded "
            f"token, {seen.some} on some, {seen.none} on none"
        )
    unsettled = unsettled_count(decisions)
    if unsettled:
        out(
            f"{unsettled} bar(s) were decided on readings that were not final yet, and are "
            f"measured as they were decided"
        )
    days = Decimal(measured_on[-1].time - measured_on[0].time) / 86_400
    suspect = len(valuations) - len(measured_on)
    left_out = f"; {suspect} suspect bar(s) left out" if suspect else ""
    out(f"measured on {metrics.bars} bar(s) over {_fixed(days)} day(s){left_out}")
    out(f"{'':<18}{'start':>14}{'end':>14}{'return':>11}{'max drawdown':>14}")
    out(_curve_line("strategy", metrics.strategy))
    out(_curve_line("opening balances", metrics.opening_held))
    out(_curve_line(f"all in {quote}", metrics.all_quote))
    out("the two comparisons pay no cost")
    out(
        f"{metrics.rebalances} rebalance(s), {metrics.swaps} swap(s); "
        f"{_fixed(metrics.traded_value)} {quote} sold, {_fixed(metrics.turnover)} times the "
        f"mean equity"
    )
    costs = metrics.costs
    out(
        f"costs: {_fixed(costs.total)} {quote} (pool fees {_fixed(costs.pool_fees)}, slippage "
        f"{_fixed(costs.slippage)}, gas {_fixed(costs.gas)} for {plain(costs.gas_eth)} ETH)"
    )
    return EXIT_OK


def main(
    argv: Sequence[str] | None = None,
    *,
    out: Callable[[str], None] = print,
    now: Callable[[], float] = _time.time,
) -> int:
    """Run one command and return its exit code."""
    args = _parser().parse_args(argv)
    try:
        if args.command == "backfill":
            return _backfill(args, out, now)
        if args.command == "backtest":
            return _backtest(args, out, now)
        if args.command == "paper":
            return _paper(args, out, now)
        if args.command == "fork":
            return _fork(args, out, now)
        if args.command == "report":
            return _report(args, out)
        if args.command == "verdict":
            return _verdict(args, out, now)
        if args.command == "target":
            return _target(args, out, now)
        return _status(args, out, now)
    except (
        TransientChainError,
        RpcRejected,
        StoreBusy,
        BarNotStored,
        JudgeUnavailable,
        HandoffNotApplicable,
    ) as exc:
        # A node that answers a read with an error is as likely to be having
        # a bad moment as to be broken, and a store another program holds is
        # let go of; the run stopped, and a later one asks. A bar not yet in
        # the store, a judge that did not answer for a reason that may pass,
        # and a handoff not yet written or not yet in its window, are the
        # same: a later visit asks.
        print(f"try again later: {one_ascii_line(exc)}", file=sys.stderr)
        return EXIT_RETRY
    except (
        AgentError,
        BacktestRangeError,
        ChainError,
        ConfigError,
        EngineError,
        HandoffError,
        MetricsError,
        OSError,
        StoreError,
    ) as exc:
        # OSError: a local file that could not be written or read, the sidecar
        # beside the store first of all; the chain reader wraps its own.
        print(f"failed: {one_ascii_line(exc)}", file=sys.stderr)
        return EXIT_FAILED
