"""``python -m contrib.autoresearch`` — the research store's two commands.

- ``fetch --coin BTC --interval 4h --since 2023-01-01`` — walk that candle
  series and the coin's funding history into the store, then scan what landed
  for holes.
- ``gaps --coin BTC --interval 4h`` — re-run that scan over what is already
  stored. No network, so it is the command to reach for when judging a store
  rather than filling one.

Exit codes, kept in step with the perp package's CLI so an operator's habits
carry across: ``0`` the command did what it says, ``1`` a named operator,
store or venue failure (the sentence on stderr says which), ``130``
interrupted. Note what ``1`` does NOT mean here: a store with gaps in it is a
successful scan, reported and exited ``0``. Gaps are a fact about the venue's
history, not a failure of the command that found them — and the command that
fills them is ``fetch``, which an operator reads this report to decide about.
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from .fetch import (
    FUNDING_SERIES,
    StopReason,
    backfill_candles,
    backfill_funding,
    render_fetch,
)
from .gaps import render_report, scan_candles, scan_funding
from .store import (
    DB_FILENAME,
    ResearchStore,
    StoreError,
    canonical_coin,
    default_db_path,
)
from .upstream import CandleInterval, ExchangeError, from_epoch_ms

__all__ = ["main"]

# The intervals this package studies, NOT the venue's whole vocabulary. Plan §1
# names 4h (the paper cycle) and 1d (the daily backdrop) and nothing else, and
# the venue's ~5000-bar depth limit is what makes that a correctness matter
# rather than taste: at 1h it reaches about 208 days and at 15m about 52, and
# such a series scans as having no holes and is far too short for the
# train/validation/holdout split to mean anything. Borrowing the whole enum
# would import a vocabulary this package deliberately does not have.
_INTERVALS = (CandleInterval.H4.value, CandleInterval.D1.value)

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

    def add_common(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("--coin", default="BTC", help="perp coin symbol (default: BTC)")
        sub.add_argument(
            "--interval",
            default=CandleInterval.H4.value,
            choices=_INTERVALS,
            help="candle interval (default: 4h)",
        )
        sub.add_argument(
            "--db", default=None, help=f"store path (default: <repo>/data/{DB_FILENAME})"
        )

    fetch_cmd = subparsers.add_parser(
        "fetch", help="walk venue history into the store, then scan it for holes"
    )
    add_common(fetch_cmd)
    fetch_cmd.add_argument(
        "--since",
        required=True,
        help="backfill start, as 2023-01-01 (midnight UTC) or a full ISO-8601 instant",
    )
    fetch_cmd.add_argument(
        "--skip-funding",
        action="store_true",
        help=(
            "only walk the candle series. Funding is not interval-scoped, so a second "
            "fetch at another interval would re-walk it for nothing; this skips that pass."
        ),
    )

    gaps_cmd = subparsers.add_parser(
        "gaps", help="scan the stored series for holes (reads the store only, no network)"
    )
    add_common(gaps_cmd)
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
        print("  reach: never fetched into this store")
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
        f"  reach: stopped because {known.value if known else stopped}"
        f" (asked from {from_epoch_ms(state['since_ms']).isoformat()},"
        f" venue clock {from_epoch_ms(state['venue_clock_ms']).isoformat()})"
    )


def _print_scans(store: ResearchStore, *, coin: str, interval: str, funding: bool) -> None:
    """Print the gap scan for what the caller just touched, candles first.

    Each scan is followed by the series' recorded reach, because the two
    answer different halves of "is this store fit to measure on": the scan
    says whether what is here is a grid, the reach says whether it is the span
    that was asked for.
    """
    series = [(scan_candles(store, coin=coin, interval=interval), interval)]
    if funding:
        series.append((scan_funding(store, coin=coin), FUNDING_SERIES))
    for report, name in series:
        for line in render_report(report):
            print(line)
        _print_reach(store, coin=coin, series=name)


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
            results.append(backfill_funding(market, store, coin=coin, since=since, end=end))
        for result in results:
            print(render_fetch(result))
        _print_scans(store, coin=coin, interval=args.interval, funding=not args.skip_funding)
    return 0


def _cmd_gaps(args: argparse.Namespace) -> int:
    with ResearchStore(_store_path(args)) as store:
        print(f"store: {store.path} (schema v{store.version})")
        _print_scans(store, coin=_coin(args), interval=args.interval, funding=True)
    return 0


_COMMANDS = {"fetch": _cmd_fetch, "gaps": _cmd_gaps}


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    args = _build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    try:
        return _COMMANDS[args.command](args)
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    except (StoreError, ExchangeError, ValueError) as exc:
        # The three families a well-formed invocation can still meet: this
        # store cannot be operated on, the venue failed, or an argument named
        # a window that is not one. Each already carries a sentence written
        # for an operator, so it is printed as-is rather than wrapped.
        print(f"error: {exc}", file=sys.stderr)
        return 1
