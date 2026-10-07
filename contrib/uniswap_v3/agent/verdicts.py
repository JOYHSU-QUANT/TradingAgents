"""One visit's asking: a verdict on every traded token at one bar, recorded as each comes.

:func:`ask_verdicts` asks the judge about each token the config trades, in
symbol order, at the bar whose boundary is ``time``, and writes each
verdict to the store and its sidecar as soon as it is given. A token whose
verdict the store already holds is not asked again: a judge whose answers
cannot be reproduced is asked once per bar, and a verdict is never
rewritten. A judge that fails on a later token leaves the earlier verdicts
stored and raises :class:`~.errors.JudgeUnavailable` or
:class:`~.errors.AgentError`; the next visit asks only what is missing.

The bar must be in the store (:class:`~.errors.BarNotStored` otherwise):
its close is what the judge is shown, and a bar the store never gets is one
the run never decides. A suspect bar is not decided either, so no judge is
asked about it. A judge that is not point in time
(:attr:`~.graph.Judge.point_in_time`) is asked about the latest bar whose
boundary has passed and no other: a bar judged later would be judged on
what came after it.

The judge's words are kept for every token it is asked about, whatever
the rating, ``REVIEW`` included: that the judge was asked and gave no
rating is itself a verdict, which a strategy treats as none.

Two visits asking at once are not guarded against beyond a second look at
the store just before each verdict is written: the one that writes second
is refused by the store, and may have replaced the other's sidecar in the
window between that look and its own write, a few milliseconds against the
minutes a judge takes. The schedule runs one visit at a time.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from ..config import ConfigError, UniswapConfig
from ..domain.times import DATE_ONLY, utc_text
from ..domain.verdicts import VerdictRecord
from ..store.bar_source import StoredBar, load_bar
from ..store.repository import Store
from .context import BARS_NEEDED, spot_context
from .errors import AgentError, BarNotStored
from .graph import FAKE_MODEL, Judge
from .record import sidecar_path, sidecar_record, verdict_record, write_sidecar
from .tickers import tickers_for

__all__ = ["AskSummary", "Asked", "ask_verdicts", "trade_date_of", "verdict_source"]


@dataclass(frozen=True)
class Asked:
    """One token's verdict at the bar: given now, or found stored."""

    symbol: str
    ticker: str
    record: VerdictRecord
    # Whether this visit asked for it; ``False`` for one the store already held.
    asked_now: bool
    elapsed_seconds: float = 0.0
    # Whether this visit asked, and found the token recorded by another visit
    # by the time its judge answered: the record is the other's, and this
    # visit's answer was dropped.
    superseded: bool = False

    def __post_init__(self) -> None:
        if self.record.verdict.symbol != self.symbol:
            raise ValueError(
                f"the record is of {self.record.verdict.symbol}, and the token is {self.symbol}"
            )
        if not self.asked_now and self.elapsed_seconds and not self.superseded:
            raise ValueError("a verdict found stored took this visit no time")
        if self.superseded and self.asked_now:
            raise ValueError("a superseded answer is not the verdict this visit recorded")


@dataclass(frozen=True)
class AskSummary:
    """What a visit did at the bar ``time``, in symbol order.

    ``suspect`` says the bar is suspect, so nothing was asked and ``verdicts``
    is empty; ``late`` says the boundary passed longer ago than the judge's
    ``ask_within_seconds``, so nothing was asked either.
    """

    time: int
    source: str
    verdicts: tuple[Asked, ...] = ()
    suspect: bool = False
    late: bool = False

    def __post_init__(self) -> None:
        if (self.suspect or self.late) and self.verdicts:
            raise ValueError("a suspect or late bar has no verdicts asked or found")

    @property
    def asked(self) -> tuple[Asked, ...]:
        """The verdicts this visit asked for."""
        return tuple(each for each in self.verdicts if each.asked_now)

    @property
    def already_stored(self) -> tuple[Asked, ...]:
        """The verdicts the store already held."""
        return tuple(each for each in self.verdicts if not each.asked_now)


def trade_date_of(time: int) -> str:
    """The UTC date of the boundary ``time``, as the engine takes a trade date."""
    return utc_text(time, DATE_ONLY)


def verdict_source(config: UniswapConfig) -> str:
    """The source the config's verdicts are written under; a config that reads none has none to write."""
    if config.verdicts is None:
        raise ConfigError(
            "verdict writes under the source the config's verdicts section names, and the "
            "config has no verdicts section"
        )
    return config.verdicts.source


def _closes(
    store: Store, config: UniswapConfig, latest: StoredBar
) -> Mapping[str, list[Decimal | None]]:
    """Each traded token's close at each of the last :data:`BARS_NEEDED` boundaries, ``latest`` last.

    One entry per boundary, oldest first; ``None`` where the store has no
    bar, or a suspect one, so that a gap stays a gap in what the judge is
    told.
    """
    interval = config.bars.interval_seconds
    closes: dict[str, list[Decimal | None]] = {symbol: [] for symbol in config.traded_symbols}
    for steps in range(BARS_NEEDED - 1, -1, -1):
        boundary = latest.bar.time - steps * interval
        if steps == 0:
            stored: StoredBar | None = latest
        else:
            stored = None if boundary < 0 else load_bar(store, config, boundary)
        for symbol, series in closes.items():
            series.append(
                None if stored is None or stored.bar.suspect else stored.bar.prices[symbol]
            )
    return closes


def ask_verdicts(
    store: Store,
    config: UniswapConfig,
    judge: Judge,
    *,
    time: int,
    home: Path,
    now: int,
    report: Callable[[Asked], None] = lambda asked: None,
) -> AskSummary:
    """Ask ``judge`` about every traded token at the bar ``time`` and record what it says.

    ``home`` is the store's directory, where the sidecars go. ``now`` is
    when the asking is done, kept as each verdict's ``asked_at``, and what
    the latest boundary is reckoned from. ``report`` is handed each token's
    :class:`Asked` as soon as it is recorded.

    The config names the source the verdicts are written under
    (:func:`verdict_source`), and every traded token must have a ticker
    (:func:`~.tickers.tickers_for`). A rehearsal judge is refused when the
    store already holds a verdict of the source from any model but the
    fake: a fake verdict blocks the real one at its bar for good, so a
    rehearsal is for a store that holds no real verdicts.
    """
    source = verdict_source(config)
    tickers = tickers_for(config.traded_symbols)
    trade_date = trade_date_of(time)
    if judge.rehearsal:
        real = [model for model in store.verdict_models(source) if model != FAKE_MODEL]
        if real:
            raise ConfigError(
                f"the store holds verdicts of {source!r} given by {real}, and a fake verdict "
                f"would stand in the way of a real one at its bar; rehearse on a store that "
                f"holds no real verdicts"
            )
    latest = now - now % config.bars.interval_seconds
    if time < latest and not judge.point_in_time:
        raise ConfigError(
            f"the bar at {trade_date} ({utc_text(time)}) is not the latest whose boundary has "
            f"passed ({utc_text(latest)}); the judge is asked about the latest bar alone, "
            f"since its data runs to the day it is asked on, and an older bar takes "
            f"--fake-rating"
        )
    if not judge.point_in_time and now - time > config.agent.ask_within_seconds:
        # The judge reads through the moment it is asked: this late it would see
        # hours past the fill the run trades at.
        return AskSummary(time=time, source=source, late=True)
    stored = load_bar(store, config, time)
    if stored is None:
        raise BarNotStored(
            f"the store has no bar at {trade_date} ({utc_text(time)}) to show the judge; "
            f"backfill or a paper visit reads it"
        )
    if stored.bar.suspect:
        return AskSummary(time=time, source=source, suspect=True)
    held = {record.verdict.symbol: record for record in store.verdicts_at(source, time)}
    closes: Mapping[str, list[Decimal | None]] | None = None
    done: list[Asked] = []
    for symbol, ticker in tickers.items():
        if symbol in held:
            done.append(Asked(symbol=symbol, ticker=ticker, record=held[symbol], asked_now=False))
            report(done[-1])
            continue
        if closes is None:
            closes = _closes(store, config, stored)
        context = spot_context(
            closes[symbol],
            symbol=symbol,
            ticker=ticker,
            quote=config.quote.symbol,
            traded=config.traded_symbols,
            time=time,
            interval_seconds=config.bars.interval_seconds,
        )
        try:
            answer = judge.ask(ticker, trade_date, context)
        except AgentError as exc:
            raise type(exc)(
                f"{exc}; {len(done)} verdict(s) at {trade_date} stay recorded, and a later "
                f"visit asks about the rest"
            ) from exc
        # Another visit may have recorded this token while the judge was thinking.
        meanwhile = store.verdict(source, symbol, time)
        if meanwhile is not None:
            done.append(
                Asked(
                    symbol=symbol,
                    ticker=ticker,
                    record=meanwhile,
                    asked_now=False,
                    elapsed_seconds=answer.elapsed_seconds,
                    superseded=True,
                )
            )
            report(done[-1])
            continue
        relative = digest = None
        if answer.reports is not None:
            relative = sidecar_path(source, symbol, time)
            try:
                digest = write_sidecar(
                    home,
                    relative,
                    sidecar_record(
                        answer,
                        source=source,
                        symbol=symbol,
                        ticker=ticker,
                        time=time,
                        trade_date=trade_date,
                        context=context,
                        model=judge.model,
                        settings=judge.settings,
                        asked_at=now,
                    ),
                )
            except OSError as exc:
                raise AgentError(
                    f"the sidecar for {symbol} at {trade_date} could not be written at "
                    f"{relative} ({type(exc).__name__}: {exc}); the judge's answer "
                    f"({answer.rating.value}) is not recorded, {len(done)} verdict(s) at "
                    f"{trade_date} stay recorded, and a later visit asks again"
                ) from exc
        record = verdict_record(
            answer,
            source=source,
            symbol=symbol,
            time=time,
            model=judge.model,
            asked_at=now,
            sidecar_path=relative,
            sidecar_digest=digest,
        )
        store.insert_verdict(record)
        done.append(
            Asked(
                symbol=symbol,
                ticker=ticker,
                record=record,
                asked_now=True,
                elapsed_seconds=answer.elapsed_seconds,
            )
        )
        report(done[-1])
    return AskSummary(time=time, source=source, verdicts=tuple(done))
