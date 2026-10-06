"""The command line: ``python -m contrib.uniswap_v3 <command>``.

- ``backfill`` reads the bars of a range from an archive node into the
  store. It can be repeated; what is stored is not read again.
- ``status`` prints what the store holds and its latest bars. With
  ``--run-id`` it goes on to print that run's holdings, value and return
  (measured as ``report`` measures it), and its latest decisions with when
  each was made. It reads no chain.
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
  have come to. It reads the store alone, and takes the run's config from
  the run.
- ``verdict`` asks the judge, the TradingAgents graph, for a verdict on
  every traded token at the latest bar whose boundary has passed, and
  writes each into the store's ``verdicts`` table under the config's
  ``verdicts.source``, with the judge's words in a sidecar beside the
  store. A verdict the store already holds is not asked for again. It
  reads no chain: the bar must be in the store already. The judge reads
  its data through the day it is asked on, so only the latest bar gets an
  honest verdict; ``--at`` names an older boundary only together with
  ``--fake-rating``, which records that rating without asking any judge,
  for a rehearsal on a store that holds no real verdicts.

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
skipped as suspect, and of stored readings a check found to be off the
final chain. It exits 3, having decided nothing of the latest bar, when
the node's chain has not yet reached the boundary or the bar's fill block,
and when a quote reverted without a reason of a pool's: whoever schedules
the visit runs it again later. It exits 1 when its clock is behind the run.
A ``fork`` run exits as a ``backtest`` does, and also 1 when the fork is not
an anvil fork on a loopback address, when the wallet does not hold what the
run's ledger says, and when the run has an open send.
A ``verdict`` exits 0 once every traded token has a verdict at the bar, by
this visit or an earlier one, and also when the bar is suspect, which no run
decides, so no judge is asked. It warns on stderr of a verdict that holds no
rating (``REVIEW``). It exits 3, keeping the verdicts given so far, when the
store has no bar at the boundary yet, and when the judge did not answer:
whoever schedules it runs it again later. It exits 1 when the config reads
no verdicts, when a traded token has no ticker, when the judge cannot be
built (its provider's key is not in the environment) or its provider
refuses the question for good (a model it does not serve), when ``--at``
names an older bar than the latest without ``--fake-rating``, and when
``--fake-rating`` meets a store that holds real verdicts of the source.
"""

from __future__ import annotations

import argparse
import sys
import time as _time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Final

from .agent.errors import AgentError, BarNotStored, JudgeUnavailable
from .chain.errors import ChainError, RpcRejected, TransientChainError
from .config import ConfigError, UniswapConfig, config_from_snapshot, load_config
from .constants import WRAPPED_NATIVE, pool_key
from .domain.bars import Finality
from .domain.decimal_context import fixed_text, parse_decimal, plain, price_text
from .domain.execution import ForkSettings
from .domain.ledger import Ledger
from .domain.metrics import Curve, MetricsError, RunMetrics, measurable, run_metrics
from .domain.records import Decision, FillRecord, Outcome, RunRecord, Valuation
from .domain.times import utc_text
from .domain.types import RunMode
from .domain.verdicts import RATINGS, Rating
from .engine.backtest import BacktestRangeError, BacktestSummary, run_backtest
from .engine.executors import QuoteExecutor
from .engine.step import EngineError, UnsettledSend, holdings_text
from .store.bar_source import load_bar
from .store.repository import Store, StoreBusy, StoreError, open_store
from .store.verdict_source import load_verdicts

if TYPE_CHECKING:
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


def _iso(time: int) -> str:
    return utc_text(time)


def _one_ascii_line(text: object) -> str:
    """``text`` as one printable line.

    A line that quotes what a node said need not be ASCII, which a console
    may be unable to print; a line break in it would read as a second line,
    and a control character could do anything to a terminal.
    """
    line = " ".join(str(text).split())
    printable = "".join(char if char.isprintable() else "?" for char in line)
    return printable.encode("ascii", "backslashreplace").decode()


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
            report=lambda time, text: out(f"{_iso(time)}  {_one_ascii_line(text)}"),
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


def _coverage_lines(store: Store, config: UniswapConfig, times: Sequence[int]) -> list[str]:
    """How many of the bars at ``times`` have a verdict from the config's source for every token, and how many each token has.

    ``times`` are the boundaries that make a bar: one that lacks a pool's
    reading is never decided, so a verdict missing there is nothing missing.
    """
    settings = config.verdicts
    if settings is None:
        return []
    per_token = dict.fromkeys(sorted(config.traded_symbols), 0)
    complete = 0
    for time in times:
        # Already cut to the traded tokens, so every one is a token counted here.
        said = load_verdicts(store, config, time)
        for symbol in said:
            per_token[symbol] += 1
        complete += len(said) == len(per_token)
    counts = ", ".join(f"{symbol} {count}" for symbol, count in per_token.items())
    return [
        f"verdicts from {settings.source}: {complete} of the latest {len(times)} bar(s) have "
        f"one for every token ({counts})"
    ]


def _status_lines(store: Store, config: UniswapConfig, bars: int) -> list[str]:
    interval = config.bars.interval_seconds
    lines = [f"chain {config.chain_id}, {interval}-second bars, prices in {config.quote.symbol}"]
    times: set[int] = set()
    for pool in config.pools:
        count, first, last = store.extent(config.chain_id, pool.address, interval)
        span = "" if first is None or last is None else f", {_iso(first)} to {_iso(last)}"
        lines.append(f"{pool_key(pool)}: {count} reading(s){span}")
        times.update(store.latest_times(config.chain_id, pool.address, interval, bars))
    with_bar: list[int] = []
    for time in sorted(times)[-bars:]:
        stored = load_bar(store, config, time)
        if stored is None:
            lines.append(f"{_iso(time)}  incomplete: a configured pool has no reading")
            continue
        with_bar.append(time)
        prices = "  ".join(
            f"{symbol} {price_text(price)}" for symbol, price in sorted(stored.bar.prices.items())
        )
        flags = [
            f"{pool_key(pool)}:{flag.value}"
            for pool, found in zip(config.pools, stored.flags, strict=True)
            for flag in sorted(found, key=lambda flag: flag.value)
        ]
        lines.append(
            f"{_iso(time)}  block {stored.bar.close_block}  {prices}  {stored.finality.value}  "
            f"{'suspect' if stored.bar.suspect else 'ok'}  flags: {', '.join(flags) or 'none'}"
        )
    return lines + _coverage_lines(store, config, with_bar)


def _after(seconds: int) -> str:
    """A span of seconds as ``[-][<days>d ]HH:MM:SS``."""
    sign = "-" if seconds < 0 else ""
    days, rest = divmod(abs(seconds), 86_400)
    hours, rest = divmod(rest, 3_600)
    minutes, rest = divmod(rest, 60)
    clock = f"{hours:02}:{minutes:02}:{rest:02}"
    return f"{sign}{days}d {clock}" if days else f"{sign}{clock}"


def _measured(
    run: RunRecord,
    config: UniswapConfig,
    decisions: Sequence[Decision],
    valuations: Sequence[Valuation],
    fills: Sequence[FillRecord],
) -> tuple[list[Valuation], RunMetrics]:
    """The valuations a run is measured on, and the run, started under ``config``, measured on them.

    As ``report`` measures it.
    """
    measured = measurable(decisions, valuations)
    metrics = run_metrics(
        quote=run.quote,
        gas_token=WRAPPED_NATIVE[config.chain_id].symbol,
        opening=run.ledger,
        valuations=measured,
        fills=fills,
        fee_rates={pool.address: pool.fee_rate for pool in config.pools},
    )
    return measured, metrics


def _stored_run(store: Store, run_id: str) -> RunRecord:
    run = store.run(run_id)
    if run is None:
        raise StoreError(f"there is no run {run_id!r} in the store")
    return run


def _run_header(run: RunRecord) -> str:
    forked = (
        ""
        if run.fork_block is None
        else f" (the fork was at block {run.fork_block} when it started)"
    )
    return (
        f"run {_one_ascii_line(run.run_id)}: {run.mode.value}{forked}, fills from the "
        f"{run.fills.value}, {_one_ascii_line(run.strategy)}, values in {run.quote}"
    )


def _decided_span(decisions: Sequence[Decision]) -> str:
    return (
        f"{len(decisions)} bar(s) decided from {_iso(decisions[0].time)} to "
        f"{_iso(decisions[-1].time)}"
    )


def _saw(store: Store, config: UniswapConfig, decision: Decision) -> str:
    """What the decision saw of each traded token's verdict, as ``<token>=<rating>``; nothing for a run that reads none.

    The rating is read back from the store at the decision's bar and matched
    by the digest the decision kept.
    A token the decision saw no verdict on reads ``none``, and one whose
    verdict the store now holds differently from what the decision saw, or
    did not hold then, or does not hold now, reads ``changed``: the same
    difference a replay warns of.
    """
    if decision.verdicts is None:
        return ""
    if config.verdicts is None:
        raise StoreError(
            f"the decision at {decision.time} saw verdicts, and the run's config reads none"
        )
    now = load_verdicts(store, config, decision.time)
    seen = []
    for symbol in sorted(config.traded_symbols):
        kept, held = decision.verdicts.get(symbol), now.get(symbol)
        if kept is None and held is None:
            seen.append(f"{symbol}=none")
        elif kept is not None and held is not None and held.digest == kept:
            seen.append(f"{symbol}={held.rating.value}")
        else:
            seen.append(f"{symbol}=changed")
    return f"  verdicts: {', '.join(seen)}"


def _why(decision: Decision) -> str:
    """A rejected or a skipped decision's reason, as `` (<reason>)``; nothing for another."""
    return "" if decision.reason is None else f" ({_one_ascii_line(decision.reason)})"


def _behind(config: UniswapConfig, last: int, now: int) -> str:
    """How far a paper run whose latest decided bar is at ``last`` is behind the clock at ``now``."""
    interval = config.bars.interval_seconds
    latest = now - now % interval
    if latest < last:
        return (
            f"the clock is behind the run: it is at {_iso(now)}, and the run has decided the "
            f"bar at {_iso(last)}"
        )
    if latest == last:
        return "up to date: the latest boundary that has passed is decided"
    return (
        f"behind: {(latest - last) // interval} boundary(ies) after the last decided bar have "
        f"passed undecided, the latest at {_iso(latest)}; a visit decides them, and the visit "
        f"log says what stopped one"
    )


def _value_line(
    run: RunRecord,
    config: UniswapConfig,
    decisions: Sequence[Decision],
    valuations: Sequence[Valuation],
    fills: Sequence[FillRecord],
) -> str:
    """The run's latest value, and its return as ``report`` measures it, or why it has none."""
    value = f"value {_fixed(valuations[-1].total_value)} {run.quote}"
    try:
        measured, metrics = _measured(run, config, decisions, valuations, fills)
    except MetricsError as exc:
        return f"{value}; return not measured ({_one_ascii_line(exc)})"
    change = _fixed(metrics.strategy.total_return * 100, signed=True)
    return (
        f"{value}; return {change}% from {_iso(measured[0].time)} to "
        f"{_iso(measured[-1].time)}, after gas, as report measures it"
    )


def _run_status_lines(store: Store, run_id: str, latest: int, now: int) -> list[str]:
    """A run's state at ``now``: its holdings, value and return, and its ``latest`` decisions.

    A paper run is also said to be up to date with the clock, or behind it,
    and its decisions to have been made so long after their boundaries.
    """
    run = _stored_run(store, run_id)
    decisions = store.decisions(run_id)
    lines = [f"{_run_header(run)}, started {_iso(run.created_at)}"]
    if not decisions:
        lines.append(f"no bar decided yet; opening balances {holdings_text(run.ledger)}")
        return lines
    valuations = store.valuations(run_id)
    if not valuations or valuations[-1].time != decisions[-1].time:
        raise StoreError(f"the decision of run {run_id!r} at {decisions[-1].time} has no valuation")
    config = config_from_snapshot(run.config)
    paper = run.mode is RunMode.PAPER
    lines.append(
        f"{_decided_span(decisions)}: "
        + _outcome_counts(Counter(decision.outcome for decision in decisions))
    )
    if paper:
        lines.append(_behind(config, decisions[-1].time, now))
    last = valuations[-1]
    lines.append(f"holdings after the bar at {_iso(last.time)}: {holdings_text(last.ledger)}")
    lines.append(_value_line(run, config, decisions, valuations, store.fills(run_id)))
    lines.append(f"latest {min(latest, len(decisions))} decision(s):")
    for decision in decisions[-latest:]:
        if decision.decided_at is None:
            when = "decided at a time that was not kept"
        elif paper:
            # How late the visit came. A backtest decides its bars long after, by design.
            when = (
                f"decided {_iso(decision.decided_at)}, "
                f"{_after(decision.decided_at - decision.time)} after the boundary"
            )
        else:
            when = f"decided {_iso(decision.decided_at)}"
        lines.append(
            f"{_iso(decision.time)}  {decision.outcome.value}  {when}"
            f"{_saw(store, config, decision)}"
            f"{_why(decision)}"
        )
    return lines


def _status(args: argparse.Namespace, out: Callable[[str], None], now: Callable[[], float]) -> int:
    config = _config(args)
    # One view for all the reads: a paper visit may be writing meanwhile.
    with open_store(args.db, create=False) as store, store.reading():
        lines = _status_lines(store, config, args.bars)
        if args.run_id is not None:
            lines += _run_status_lines(store, args.run_id, args.bars, int(now()))
    for line in lines:
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


def _counts(counts: Mapping[str, int]) -> str:
    return ", ".join(f"{name} {count}" for name, count in sorted(counts.items()))


def _outcome_counts(counts: Mapping[Outcome, int]) -> str:
    """Every outcome with its count, those that did not occur at zero."""
    return _counts({outcome.value: counts.get(outcome, 0) for outcome in Outcome})


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
    warned of a rebalance the executor rejected and of a bar skipped as suspect, which
    in a backtest are counted and no more. It is not warned of bars decided on readings
    that were not final: a visit made on time always decides its latest bar on one.
    A run whose swaps are signed, ``signs``, is warned of rejected rebalances as well,
    and of those left partial, which hold a wallet half rebalanced.
    """
    out(
        f"run {_one_ascii_line(run_id)}: {summary.boundaries} boundary(ies) from {_iso(summary.start)} to "
        f"{_iso(summary.end)}: {summary.decided} decided, {summary.already_decided} already "
        f"decided, {len(summary.missing)} without a bar"
    )
    out(_outcome_counts(summary.outcomes))
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
        print(f"failed: {_one_ascii_line(exc)}", file=sys.stderr)
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
    out(
        f"the bar at {_iso(summary.latest)} {'is decided' if made else 'was already decided'}: "
        f"{decision.outcome.value}{_why(decision)}"
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
        lines.append(f"stopped by: {_one_ascii_line(send.failure)}")
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
            print(f"failed: {_one_ascii_line(exc)}", file=sys.stderr)
            try:
                found = reconcile_open_send(store, config, wallet, args.run_id)
            except Exception as failed:
                # The run needs a person either way: not a reason to say "try again later".
                print(
                    f"the open send could not be set beside the wallet: "
                    f"{type(failed).__name__}: {_one_ascii_line(failed)}",
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
            f"{_one_ascii_line(record.model)}, asked {_iso(record.asked_at)}"
        )
    words = (
        "no words kept"
        if record.sidecar_path is None
        else f"words in {_one_ascii_line(record.sidecar_path)}"
    )
    return (
        f"{asked.symbol} ({asked.ticker}): {rating}, model {_one_ascii_line(record.model)}, "
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
    if args.fake_rating is not None:
        from .agent.graph import FakeJudge

        judge = FakeJudge(Rating(args.fake_rating))
    else:
        from .agent.graph import TradingAgentsJudge

        judge = TradingAgentsJudge(config.agent, home / "tradingagents")
    out(f"verdicts of {_one_ascii_line(source)} at {_iso(at)} (trade date {trade_date_of(at)}):")
    with open_store(args.db, create=False) as store:
        summary = ask_verdicts(
            store,
            config,
            judge,
            time=at,
            home=home,
            now=present,
            report=lambda asked: out(_asked_line(asked)),
        )
    if summary.suspect:
        out(f"the bar at {_iso(at)} is suspect, which no run decides; no judge was asked")
        return EXIT_OK
    out(f"{len(summary.asked)} asked, {len(summary.already_stored)} already stored")
    review = [asked.symbol for asked in summary.asked if asked.record.verdict.rating.is_review]
    if review:
        print(
            f"warning: the judge gave no rating on {', '.join(review)} (REVIEW); the verdict "
            f"is kept as such, a strategy treats it as none, and it is not asked again",
            file=sys.stderr,
        )
    return EXIT_OK


_fixed = fixed_text


def _curve_line(name: str, curve: Curve) -> str:
    change = _fixed(curve.total_return * 100, signed=True) + "%"
    fall = _fixed(curve.max_drawdown * 100) + "%"
    return f"{name:<18}{_fixed(curve.start):>14}{_fixed(curve.end):>14}{change:>11}{fall:>14}"


def _report(args: argparse.Namespace, out: Callable[[str], None]) -> int:
    # One view for all the reads: a backtest may be writing the run meanwhile.
    with open_store(args.db, create=False) as store, store.reading():
        run = _stored_run(store, args.run_id)
        decisions = store.decisions(args.run_id)
        valuations = store.valuations(args.run_id)
        fills = store.fills(args.run_id)
    measured, metrics = _measured(
        run, config_from_snapshot(run.config), decisions, valuations, fills
    )
    quote = run.quote
    out(_run_header(run))
    out(_decided_span(decisions))
    out("decisions: " + _outcome_counts(Counter(decision.outcome for decision in decisions)))
    for outcome in (Outcome.SKIPPED_SUSPECT, Outcome.REJECTED, Outcome.PARTIAL):
        codes = Counter(
            decision.reason_code.value
            for decision in decisions
            if decision.outcome is outcome and decision.reason_code is not None
        )
        if codes:
            out(f"{outcome.value}: {_counts(codes)}")
    # The report reads no bars, so it cannot tell whether such a reading held.
    unsettled = sum(
        1
        for decision in decisions
        if decision.seen is not None
        and decision.seen.finality is Finality.PENDING
        and not decision.suspect
    )
    if unsettled:
        out(
            f"{unsettled} bar(s) were decided on readings that were not final yet, and are "
            f"measured as they were decided"
        )
    days = Decimal(measured[-1].time - measured[0].time) / 86_400
    suspect = len(valuations) - len(measured)
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
        return _status(args, out, now)
    except (TransientChainError, RpcRejected, StoreBusy, BarNotStored, JudgeUnavailable) as exc:
        # A node that answers a read with an error is as likely to be having
        # a bad moment as to be broken, and a store another program holds is
        # let go of; the run stopped, and a later one asks.
        print(f"try again later: {_one_ascii_line(exc)}", file=sys.stderr)
        return EXIT_RETRY
    except (
        AgentError,
        BacktestRangeError,
        ChainError,
        ConfigError,
        EngineError,
        MetricsError,
        StoreError,
    ) as exc:
        print(f"failed: {_one_ascii_line(exc)}", file=sys.stderr)
        return EXIT_FAILED
