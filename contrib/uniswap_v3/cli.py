"""The command line: ``python -m contrib.uniswap_v3 <command>``.

- ``backfill`` reads the bars of a range from an archive node into the
  store. It can be repeated; what is stored is not read again.
- ``status`` prints what the store holds and its latest bars. It reads no
  chain.

``backfill`` takes the node's URL from the environment variable the config
names (``ETH_RPC_URL`` unless it names another); with the URL in a ``.env``
file, run it as ``python -m dotenv run -- python -m contrib.uniswap_v3 ...``.

Exit codes: 0 when the command ran to its end, 1 when it could not and
running it again unchanged will not help (the config, the store, the range,
the node's setup, or an answer of the node's that cannot be right), 3 when
the node could not be reached, is behind, or answered a read with an error,
and a later run may succeed. 2 is argparse's, for a command line it cannot
read. A ``backfill`` that ran to its end exits 0 even when it left
boundaries without an answer or not reached yet: its last lines count them,
and a warning on stderr names the ones without an answer.
"""

from __future__ import annotations

import argparse
import sys
import time as _time
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Final

from .chain.errors import ChainError, RpcRejected, TransientChainError
from .config import ConfigError, UniswapConfig, load_config
from .constants import pool_key
from .store.bar_source import load_bar
from .store.repository import Store, StoreError, open_store

__all__ = ["EXIT_FAILED", "EXIT_OK", "EXIT_RETRY", "main"]

EXIT_OK: Final = 0
EXIT_FAILED: Final = 1
EXIT_RETRY: Final = 3

# What a dry run plans against when there is no store yet: an empty one,
# in memory, so that it leaves no file behind.
_NO_STORE: Final = Path(":memory:")


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


def _iso(time: int) -> str:
    return datetime.fromtimestamp(time, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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
    from .backfill import BackfillRangeError, backfill, check_range, plan_backfill
    from .chain.rpc import RpcSettings, connect

    config = _config(args)
    end = int(now()) if args.end is None else args.end
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
            # What a node said is quoted in the text; it need not be ASCII,
            # and a console may not be able to print it.
            report=lambda time, text: out(
                f"{_iso(time)}  {text.encode('ascii', 'backslashreplace').decode()}"
            ),
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
        return _status(args, out)
    except (TransientChainError, RpcRejected) as exc:
        # A node that answers a read with an error is as likely to be having
        # a bad moment as to be broken; the run stopped, and a later one asks.
        print(f"try again later: {exc}", file=sys.stderr)
        return EXIT_RETRY
    except (ChainError, ConfigError, StoreError) as exc:
        print(f"failed: {exc}", file=sys.stderr)
        return EXIT_FAILED
