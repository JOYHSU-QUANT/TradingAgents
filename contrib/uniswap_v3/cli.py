"""The command line: ``python -m contrib.uniswap_v3 <command>``.

- ``backfill`` reads the bars of a range from an archive node into the
  store. It can be repeated; what is stored is not read again.
- ``status`` prints what the store holds and its latest bars. It reads no
  chain.
- ``backtest`` replays the stored bars of a range through the engine as the
  run ``--run-id``, with fills from the offline model. It reads no chain. A
  new run takes its opening balances from ``--balance`` and ``--gas-eth``;
  the same command run again decides nothing twice.
- ``report`` prints a run's return, drawdown, turnover and costs, beside
  what leaving the opening balances untouched, or in the quote token, would
  have come to. It reads the store alone, and takes the run's config from
  the run.

``backfill`` takes the node's URL from the environment variable the config
names (``ETH_RPC_URL`` unless it names another); with the URL in a ``.env``
file, run it as ``python -m dotenv run -- python -m contrib.uniswap_v3 ...``.

Exit codes: 0 when the command ran to its end, 1 when it could not and
running it again unchanged will not help (the config, the store, the range,
the node's setup, or an answer of the node's that cannot be right), 3 when
the node could not be reached, is behind, or answered a read with an error,
and a later run may succeed. 2 is argparse's, for a command line it cannot
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
from typing import Final

from .chain.errors import ChainError, RpcRejected, TransientChainError
from .config import ConfigError, UniswapConfig, config_from_snapshot, load_config
from .constants import WRAPPED_NATIVE, pool_key
from .domain.bars import Finality
from .domain.decimal_context import parse_decimal, plain
from .domain.ledger import Ledger
from .domain.metrics import Curve, MetricsError, run_metrics
from .domain.records import Outcome
from .engine.backtest import BacktestRangeError, run_backtest
from .engine.step import EngineError
from .store.bar_source import load_bar
from .store.repository import Store, StoreError, open_store

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
    return datetime.fromtimestamp(time, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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

    status = add("status", "Print what the store holds and its latest bars.")
    status.add_argument(
        "--bars", type=_positive, default=5, help="how many of the latest bars to print"
    )

    backtest = add("backtest", "Replay the stored bars of a range through the engine. Offline.")
    backtest.add_argument("--run-id", type=_run_id, required=True, help="the run to start or carry on")
    backtest.add_argument(
        "--from",
        dest="start",
        type=_parse_time,
        required=True,
        help="the first bar boundary, in UTC: 2024-01-01 or 2024-01-01T12:00:00",
    )
    backtest.add_argument(
        "--to",
        dest="end",
        type=_parse_time,
        help="the range ends at the last boundary at or before this time "
        "(default: the store's latest bar)",
    )
    backtest.add_argument(
        "--balance",
        dest="balances",
        type=_balance,
        action="append",
        default=[],
        metavar="TOKEN=AMOUNT",
        help="an opening balance of a new run, in whole tokens: USDC=10000. Repeat it for "
        "each token held; a token left out starts at zero",
    )
    backtest.add_argument(
        "--gas-eth",
        type=_amount,
        metavar="AMOUNT",
        help="the ETH a new run sets aside for gas: 0.5",
    )
    backtest.add_argument(
        "--fills",
        choices=["model"],
        default="model",
        help="where fills come from: the offline model (the bar's close, less fees, "
        "execution.model.slippage and modelled gas)",
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


def _backfill(args: argparse.Namespace, out: Callable[[str], None], now: Callable[[], float]) -> int:
    # Imported here: ``status`` reads no chain and should not wait on web3.
    try:
        from .backfill import BackfillRangeError, backfill, check_range, plan_backfill
        from .chain.rpc import RpcSettings, connect
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
        settings = RpcSettings() if config.rpc_url_env is None else RpcSettings(config.rpc_url_env)
        rpc = connect(config.chain_id, settings=settings)
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
    if summary.confirmed or summary.reorged:
        out(
            f"checked pending readings against the final chain: {summary.confirmed} final, "
            f"{summary.reorged} reorged"
        )
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


def _status_lines(store: Store, config: UniswapConfig, bars: int) -> list[str]:
    interval = config.bars.interval_seconds
    lines = [f"chain {config.chain_id}, {interval}-second bars, prices in {config.quote.symbol}"]
    times: set[int] = set()
    for pool in config.pools:
        count, first, last = store.extent(config.chain_id, pool.address, interval)
        span = "" if first is None or last is None else f", {_iso(first)} to {_iso(last)}"
        lines.append(f"{pool_key(pool)}: {count} reading(s){span}")
        times.update(store.latest_times(config.chain_id, pool.address, interval, bars))
    for time in sorted(times)[-bars:]:
        stored = load_bar(store, config, time)
        if stored is None:
            lines.append(f"{_iso(time)}  incomplete: a configured pool has no reading")
            continue
        # Two decimal places for a price of 1 or more, six digits for a smaller one.
        prices = "  ".join(
            f"{symbol} {price:.2f}" if price >= 1 else f"{symbol} {price:.6g}"
            for symbol, price in sorted(stored.bar.prices.items())
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
    return lines


def _status(args: argparse.Namespace, out: Callable[[str], None]) -> int:
    config = _config(args)
    with open_store(args.db, create=False) as store:
        for line in _status_lines(store, config, args.bars):
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


def _backtest(args: argparse.Namespace, out: Callable[[str], None], now: Callable[[], float]) -> int:
    config = _config(args)
    opening = _opening(args, config)
    # A backtest's rows can be made again, so its commits need not wait for the disk.
    with open_store(args.db, create=False, durable=False) as store:
        summary = run_backtest(
            store,
            config,
            run_id=args.run_id,
            start=args.start,
            end=args.end,
            opening=opening,
            created_at=int(now()),
        )
    out(
        f"run {args.run_id}: {summary.boundaries} boundary(ies) from {_iso(summary.start)} to "
        f"{_iso(summary.end)}: {summary.decided} decided, {summary.already_decided} already "
        f"decided, {len(summary.missing)} without a bar"
    )
    out(_outcome_counts(summary.outcomes))
    warnings = []
    if summary.missing:
        warnings.append(
            f"{len(summary.missing)} boundary(ies) without a bar, from "
            f"{_iso(summary.missing[0])} to {_iso(summary.missing[-1])}; they were not decided. "
            f"One that is backfilled later is decided by a rerun only if the run has not gone "
            f"past it; otherwise the range needs a new run"
        )
    if summary.on_pending:
        warnings.append(
            f"{summary.on_pending} bar(s) were decided on readings that are not final yet; a "
            f"decision stands even if the chain later drops the block it was made on"
        )
    if summary.changed:
        warnings.append(
            f"{len(summary.changed)} bar(s) decided earlier now read differently in the store "
            f"(another close block, or suspect where they were not), from "
            f"{_iso(summary.changed[0])} to {_iso(summary.changed[-1])}; their decisions stand, "
            f"and a new run decides them on what the store holds now"
        )
    if summary.gas_rejected:
        warnings.append(
            f"{len(summary.gas_rejected)} rebalance(s) were rejected because the gas balance "
            f"did not cover them, the first at {_iso(summary.gas_rejected[0])}; nothing tops a "
            f"run's gas balance up"
        )
    for warning in warnings:
        print(f"warning: {warning}", file=sys.stderr)
    return EXIT_OK


def _fixed(value: Decimal, *, signed: bool = False) -> str:
    """``value`` to two decimal places, with its sign when ``signed``, and never a negative zero."""
    text = f"{value:+.2f}" if signed else f"{value:.2f}"
    if text.strip("+-0."):
        return text
    return ("+" if signed else "") + text.lstrip("+-")


def _curve_line(name: str, curve: Curve) -> str:
    change = _fixed(curve.total_return * 100, signed=True) + "%"
    fall = _fixed(curve.max_drawdown * 100) + "%"
    return f"{name:<18}{_fixed(curve.start):>14}{_fixed(curve.end):>14}{change:>11}{fall:>14}"


def _report(args: argparse.Namespace, out: Callable[[str], None]) -> int:
    # One view for all the reads: a backtest may be writing the run meanwhile.
    with open_store(args.db, create=False) as store, store.reading():
        run = store.run(args.run_id)
        if run is None:
            raise StoreError(f"there is no run {args.run_id!r} in the store")
        decisions = store.decisions(args.run_id)
        valuations = store.valuations(args.run_id)
        fills = store.fills(args.run_id)
    config = config_from_snapshot(run.config)
    # A suspect bar's prices are not to be trusted, so its valuation is not measured.
    suspect = {decision.time for decision in decisions if decision.suspect}
    measured = [valuation for valuation in valuations if valuation.time not in suspect]
    if decisions and not measured:
        raise MetricsError(
            f"every one of the run's {len(decisions)} decided bar(s) was suspect, so there "
            f"is none to measure it on"
        )
    quote = run.quote
    metrics = run_metrics(
        quote=quote,
        gas_token=WRAPPED_NATIVE[config.chain_id].symbol,
        opening=run.ledger,
        valuations=measured,
        fills=fills,
        fee_rates={pool.address: pool.fee_rate for pool in config.pools},
    )
    out(f"run {run.run_id}: {run.mode.value}, {run.strategy}, values in {quote}")
    out(
        f"{len(decisions)} bar(s) decided from {_iso(decisions[0].time)} to "
        f"{_iso(decisions[-1].time)}"
    )
    out("decisions: " + _outcome_counts(Counter(decision.outcome for decision in decisions)))
    for outcome in (Outcome.SKIPPED_SUSPECT, Outcome.REJECTED):
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
        if decision.seen is not None and decision.seen.finality is Finality.PENDING
    )
    if unsettled:
        out(
            f"{unsettled} bar(s) were decided on readings that were not final yet, and are "
            f"measured as they were decided"
        )
    days = Decimal(measured[-1].time - measured[0].time) / 86_400
    left_out = f"; {len(suspect)} suspect bar(s) left out" if suspect else ""
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
        if args.command == "report":
            return _report(args, out)
        return _status(args, out)
    except (TransientChainError, RpcRejected) as exc:
        # A node that answers a read with an error is as likely to be having
        # a bad moment as to be broken; the run stopped, and a later one asks.
        print(f"try again later: {_one_ascii_line(exc)}", file=sys.stderr)
        return EXIT_RETRY
    except (
        BacktestRangeError,
        ChainError,
        ConfigError,
        EngineError,
        MetricsError,
        StoreError,
    ) as exc:
        print(f"failed: {_one_ascii_line(exc)}", file=sys.stderr)
        return EXIT_FAILED
