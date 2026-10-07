"""``python -m contrib.carry``: the ``signal`` and ``history`` commands.

``signal`` is the daily coordinator (carry plan §3.1): fetch the funding
settlements the window needs into the research store, read the rule at
the next UTC day boundary, carry the previous handoff's position forward,
size the spot leg from the two runs' equities, and write the handoff. It
writes nothing else — not the research store beyond the funding rows the
walk adds, and neither venue's store — and exits 1 without touching the
handoff when the rule cannot be read.

``history`` replays the rule over the funding the research store already
holds (:mod:`.history`) and prints the summary the parameters are judged
by. Put the rows there first with the research package's own walk:
``python -m contrib.autoresearch fetch --coin ETH --since 2024-01-01``.

Exit codes: 0 done, 1 refused (a message on stderr says why), 2 usage.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from .handoff import HandoffError, build, iso_utc, previous_handoff, write_handoff
from .history import floor_day, format_summary, pct, replay
from .signal import OUT, Action, Params, Side, SignalError, advance, decide, read
from .stores import StoreReadError, perp_equity, spot_equity
from .upstream import (
    MS_PER_DAY,
    ExchangeError,
    ResearchStore,
    StoreError,
    backfill_funding,
    epoch_ms,
    from_epoch_ms,
    load_market,
    render_fetch,
)

__all__ = ["main"]

COINS = tuple(sorted(("ETH", "BTC")))
# Two days of slack behind the window: the oldest day's samples must all be
# there, and a settlement the venue stamps a little early must not fall out.
_SLACK_DAYS = 2


class _Refused(Exception):
    """A command that stops with a sentence and exit code 1."""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _instant_ms(text: str, *, what: str) -> int:
    """``YYYY-MM-DD`` (midnight UTC) or an aware ISO-8601 instant, as epoch ms."""
    try:
        day = date.fromisoformat(text)
    except ValueError:
        day = None
    if day is not None:
        return epoch_ms(datetime(day.year, day.month, day.day, tzinfo=timezone.utc), what=what)
    spelled = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        value = datetime.fromisoformat(spelled)
    except ValueError:
        raise _Refused(f"{what}: not an ISO-8601 date or instant: {text!r}") from None
    if value.tzinfo is None:
        raise _Refused(f"{what}: an instant needs a timezone, got {text!r}")
    return epoch_ms(value, what=what)


def _add_param_args(sub: argparse.ArgumentParser) -> None:
    defaults = Params()
    group = sub.add_argument_group("the rule (carry plan §2 D6)")
    group.add_argument(
        "--window-days", type=int, default=defaults.window_days, help="z-score window"
    )
    group.add_argument("--z-in", type=float, default=defaults.z_in, help="enter at or above")
    group.add_argument("--z-out", type=float, default=defaults.z_out, help="exit at or below")
    group.add_argument(
        "--min-hold-days", type=int, default=defaults.min_hold_days, help="shortest stay once in"
    )
    group.add_argument(
        "--margin-pct", type=int, default=defaults.margin_pct, help="the perp leg's target margin"
    )


def _params(args: argparse.Namespace) -> Params:
    try:
        return Params(
            window_days=args.window_days,
            z_in=args.z_in,
            z_out=args.z_out,
            min_hold_days=args.min_hold_days,
            margin_pct=args.margin_pct,
        )
    except SignalError as exc:
        raise _Refused(str(exc)) from None


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m contrib.carry",
        description="The carry coordinator: one funding rule, one handoff document.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    signal = sub.add_parser(
        "signal",
        help="read the rule at the next UTC day boundary and write the handoff",
        description=(
            "Fetch the window's funding into the research store, read the rule, carry the "
            "previous handoff's position forward, and write the handoff for the next UTC day "
            "boundary. The handoff at --out is also the coordinator's memory of its position."
        ),
    )
    signal.add_argument("--coin", required=True, choices=COINS)
    signal.add_argument("--out", required=True, metavar="FILE", help="the handoff document")
    signal.add_argument(
        "--research-db", required=True, metavar="PATH", help="the research store (funding rows)"
    )
    signal.add_argument(
        "--as-of",
        metavar="INSTANT",
        help="the boundary to decide for (a UTC day boundary); default: the next one",
    )
    signal.add_argument(
        "--no-fetch",
        action="store_true",
        help="do not call the venue; read the rule from the funding the store already holds",
    )
    signal.add_argument("--perp-db", metavar="PATH", help="the perp leg's store, for its equity")
    signal.add_argument("--perp-run-id", metavar="RUN", help="the perp leg's run")
    signal.add_argument("--spot-db", metavar="PATH", help="the spot leg's store, for its equity")
    signal.add_argument("--spot-run-id", metavar="RUN", help="the spot leg's run")
    _add_param_args(signal)

    history = sub.add_parser(
        "history",
        help="replay the rule over the funding the research store holds",
        description=(
            "Drive the rule over every UTC day boundary the store's funding can be read at and "
            "print how often it was in, how often it traded, and what it collected."
        ),
    )
    history.add_argument("--coin", required=True, choices=COINS)
    history.add_argument(
        "--research-db", required=True, metavar="PATH", help="the research store (funding rows)"
    )
    history.add_argument("--since", metavar="INSTANT", help="first boundary at or after")
    history.add_argument("--until", metavar="INSTANT", help="last boundary at or before")
    history.add_argument(
        "--rows", action="store_true", help="also print every boundary that entered or exited"
    )
    _add_param_args(history)
    return parser


def _equity(
    reader: Callable[[Path, str], Decimal], db: str | None, run_id: str | None, *, leg: str
) -> Decimal | None:
    if (db is None) != (run_id is None):
        raise _Refused(f"--{leg}-db and --{leg}-run-id go together")
    if db is None or run_id is None:
        return None
    return reader(Path(db), run_id)


def _cmd_signal(
    args: argparse.Namespace, *, market_factory: Callable[[], Any], now: Callable[[], datetime]
) -> int:
    params = _params(args)
    coin: str = args.coin
    now_at = now()
    now_ms = epoch_ms(now_at, what="the clock")
    as_of_ms = (
        _instant_ms(args.as_of, what="--as-of") if args.as_of else floor_day(now_ms) + MS_PER_DAY
    )
    if as_of_ms % MS_PER_DAY:
        raise _Refused(f"--as-of {iso_utc(as_of_ms)} is not a UTC day boundary")
    out = Path(args.out)
    last = previous_handoff(out, coin=coin, as_of_ms=as_of_ms)
    since_ms = as_of_ms - (params.window_days + _SLACK_DAYS) * MS_PER_DAY
    print(f"carry signal: {coin} as of {iso_utc(as_of_ms)}")
    with ResearchStore(args.research_db) as store:
        if not args.no_fetch:
            if since_ms >= now_ms:
                raise _Refused(
                    f"--as-of {iso_utc(as_of_ms)} is too far ahead to fetch a window for; "
                    f"pass --no-fetch to read the store as it is"
                )
            try:
                fetched = backfill_funding(
                    market_factory(), store, coin=coin, since=from_epoch_ms(since_ms), end=now_at
                )
            except ValueError as exc:  # the walk's own window refusals
                raise _Refused(str(exc)) from None
            print(f"  fetched: {render_fetch(fetched)}")
        history = list(store.iter_funding(coin, since_ms=since_ms, until_ms=as_of_ms))
    reading = read(history, as_of_ms, params)
    if reading is None:
        raise _Refused(
            f"no {coin} settlement before {iso_utc(as_of_ms)} in {args.research_db}; "
            f"the handoff was not written"
        )
    before = OUT if last is None else last.position
    rerun = last is not None and last.as_of_ms == as_of_ms
    if last is not None and rerun:
        # The boundary was already decided and the venues may have read it: the
        # decision stands (deciding again from the position it left would read
        # the rule against its own outcome), and only the sizing is refreshed.
        action, position = last.action, last.position
    else:
        action = decide(reading, before, as_of_ms, params)
        position = advance(before, action, as_of_ms)
    equity_perp = _equity(perp_equity, args.perp_db, args.perp_run_id, leg="perp")
    equity_spot = _equity(spot_equity, args.spot_db, args.spot_run_id, leg="spot")
    handoff = build(
        coin=coin,
        as_of_ms=as_of_ms,
        written_at_ms=now_ms,
        action=action,
        position=position,
        params=params,
        reading=reading,
        equity_perp=equity_perp,
        equity_spot=equity_spot,
    )
    write_handoff(out, handoff)
    if position.side is Side.IN and handoff.spot_weight == 0:
        print(
            f"carry: WARNING: spot weight is 0 while in: a leg has no equity "
            f"(perp equity {equity_perp}, spot equity {equity_spot})",
            file=sys.stderr,
        )
    z = "n/a" if reading.z is None else f"{reading.z:.2f}"
    recent = (
        "n/a"
        if reading.recent_mean is None
        else f"{reading.recent_mean}/h ({pct(reading.recent_annualized)} annualized)"
    )
    print(
        f"  funding: {reading.current}/h at {iso_utc(reading.at_ms)} "
        f"({pct(reading.current_annualized)} annualized); z {z} over {reading.samples} "
        f"samples ({params.window_days}d); last {reading.recent_samples}h mean {recent}"
    )
    held = f"; in since {iso_utc(position.entered_at_ms)}" if position.entered_at_ms else ""
    if rerun:
        print(
            f"  position: {position.side.value} ({action.value}; this boundary was already "
            f"decided, sizing refreshed){held}"
        )
    else:
        print(f"  position: {before.side.value} -> {position.side.value} ({action.value}){held}")
    equity = (
        f"perp equity {equity_perp if equity_perp is not None else 'unknown'}, "
        f"spot equity {equity_spot if equity_spot is not None else 'unknown'}"
    )
    print(
        f"  targets: perp {handoff.perp_side} {handoff.margin_pct}% margin; "
        f"spot {handoff.spot_token} weight {handoff.spot_weight} ({equity})"
    )
    print(f"  wrote {out}")
    return 0


def _cmd_history(args: argparse.Namespace) -> int:
    params = _params(args)
    since_ms = _instant_ms(args.since, what="--since") if args.since else None
    until_ms = _instant_ms(args.until, what="--until") if args.until else None
    with ResearchStore(args.research_db) as store:
        points = list(store.iter_funding(args.coin))
    if not points:
        raise _Refused(
            f"no {args.coin} funding in {args.research_db}; fetch it first with "
            f"`python -m contrib.autoresearch fetch --coin {args.coin} --since <date>`"
        )
    rows, summary = replay(args.coin, points, params, since_ms=since_ms, until_ms=until_ms)
    for line in format_summary(summary, params):
        print(line)
    if args.rows:
        for row in rows:
            if row.action not in (Action.ENTER, Action.EXIT):
                continue
            z = "n/a" if row.reading is None or row.reading.z is None else f"{row.reading.z:.2f}"
            rate = "n/a" if row.reading is None else f"{row.reading.current}/h"
            print(f"  {iso_utc(row.boundary_ms)} {row.action.value:<5} z {z} funding {rate}")
    return 0


def main(
    argv: Sequence[str] | None = None,
    *,
    market_factory: Callable[[], Any] = load_market,
    now: Callable[[], datetime] = _utc_now,
) -> int:
    """Run one command; ``market_factory`` and ``now`` are the seams a test binds."""
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "signal":
            return _cmd_signal(args, market_factory=market_factory, now=now)
        if args.command == "history":
            return _cmd_history(args)
        raise AssertionError(args.command)
    except (_Refused, HandoffError, SignalError, StoreReadError, StoreError, ExchangeError) as exc:
        print(f"carry: {exc}", file=sys.stderr)
        return 1
