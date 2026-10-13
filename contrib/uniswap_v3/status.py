"""``status``: what the store holds and a run's state, as a model, lines and JSON.

The dashboard plan's reading model for this package. :func:`read_status`
reads the store — and, given a run, that run — into a :class:`Status`: a
frozen tree of dataclasses whose fields are what the ``status`` lines have
always said (the store's extent per pool, its latest bars with their flags,
the coverage of verdicts and targets; the run's header, how many bars it
decided and how they ended, whether a paper run is up to date with the
clock, its holdings and value, the return and costs as ``report`` measures
them, and its latest decisions with what each saw and what the store now
holds for it). :func:`status_lines` renders those lines from the model, and
:func:`status_json` the same model as one document, field by field
(``dataclasses.asdict`` is the whitelist), ``Decimal`` as its exact text,
instants as this package spells them (``2024-01-01T00:00:00Z``: each
package's document keeps its own ISO-8601 UTC spelling, and a reader parses
them with ``datetime.fromisoformat``), ASCII throughout. The document opens
with ``schema_version`` and ``source``: the version is scoped to this
package's documents, and a change to a field's name, place or meaning bumps
:data:`SCHEMA_VERSION` (``tests/test_cli.py`` pins the field paths).

Nothing from the run's config snapshot is copied out beyond the strategy's
name and the quote; a decision's reason (engine text) is masked for a
``0x``-prefixed 20- or 32-byte hex and cut at :data:`TEXT_LIMIT`.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from decimal import Decimal

from .config import UniswapConfig, config_from_snapshot
from .constants import WRAPPED_NATIVE, pool_key
from .domain.decimal_context import fixed_text, plain, price_text
from .domain.ledger import Ledger
from .domain.metrics import Curve, MetricsError, RunMetrics, measurable, run_metrics
from .domain.records import Decision, FillRecord, Outcome, RunRecord, Valuation
from .domain.text import counts, one_ascii_line
from .domain.times import after, utc_text
from .domain.types import RunMode
from .store.bar_source import load_bar
from .store.repository import Store, StoreError
from .store.target_source import load_targets
from .store.verdict_source import load_verdicts

__all__ = [
    "SCHEMA_VERSION",
    "SOURCE",
    "TEXT_LIMIT",
    "BarStatus",
    "Clock",
    "CostsStatus",
    "Coverage",
    "CurveStatus",
    "DecisionStatus",
    "Holdings",
    "Metrics",
    "PoolExtent",
    "RunStatus",
    "Status",
    "StoreStatus",
    "VerdictsSeen",
    "decided_span",
    "measured",
    "outcome_counts",
    "read_status",
    "run_header",
    "status_json",
    "status_lines",
    "stored_run",
    "verdicts_seen",
]

# The document's version, scoped to SOURCE.
SCHEMA_VERSION = 1
SOURCE = "uniswap_v3"
# The longest a decision's reason or a metrics error runs in the model.
TEXT_LIMIT = 1000
# A 32-byte hex (a key) or a 20-byte hex (an address) in free text, the longer
# shape first; folded to its first six and last four characters.
_HEX_SECRET = re.compile(r"0x(?:[0-9a-fA-F]{64}|[0-9a-fA-F]{40})")


# --- the model ----------------------------------------------------------------


@dataclass(frozen=True)
class PoolExtent:
    """How many readings a pool has, and the boundaries of the first and the last."""

    pool: str
    readings: int
    first: str | None
    last: str | None


@dataclass(frozen=True)
class BarStatus:
    """One of the latest boundaries: the bar there, or ``complete`` False when a pool lacks a reading.

    ``finality`` is ``pending`` or ``final``; ``flags`` are ``<pool>:<flag>``
    for every data-check flag on a pool's reading.
    """

    time: str
    complete: bool
    close_block: int | None
    prices: dict[str, Decimal]
    finality: str | None
    suspect: bool | None
    flags: tuple[str, ...]


@dataclass(frozen=True)
class Coverage:
    """Of the latest bars, how many have a verdict (or a target) from the config's source for every token."""

    what: str
    source: str
    complete: int
    of: int
    per_token: dict[str, int]


@dataclass(frozen=True)
class StoreStatus:
    """What the store holds under the config: its pools' extent, the latest bars, the coverage."""

    chain_id: int
    interval_seconds: int
    quote: str
    pools: tuple[PoolExtent, ...]
    latest_bars: tuple[BarStatus, ...]
    coverage: tuple[Coverage, ...]


@dataclass(frozen=True)
class Holdings:
    """A ledger: each token's balance, and the gas balance in ETH."""

    balances: dict[str, Decimal]
    gas_eth: Decimal


@dataclass(frozen=True)
class CurveStatus:
    """A value over the measured bars: start, end, the change as a fraction, the deepest fall."""

    start: Decimal
    end: Decimal
    total_return: Decimal
    max_drawdown: Decimal


@dataclass(frozen=True)
class CostsStatus:
    """What the run paid, in the quote: pool fees, slippage, gas (and the ETH it burned)."""

    pool_fees: Decimal
    slippage: Decimal
    gas: Decimal
    gas_eth: Decimal
    total: Decimal


@dataclass(frozen=True)
class Metrics:
    """The run as ``report`` measures it, over the bars that could be measured."""

    bars: int
    measured_from: str
    measured_to: str
    suspect_left_out: int
    strategy: CurveStatus
    opening_held: CurveStatus
    all_quote: CurveStatus
    rebalances: int
    swaps: int
    traded_value: Decimal
    turnover: Decimal
    costs: CostsStatus


@dataclass(frozen=True)
class Clock:
    """A paper run against the clock at ``now``.

    ``state`` is ``up_to_date`` (the latest boundary that has passed is
    decided), ``behind`` (``undecided`` boundaries after the last decided bar
    have passed, the latest at ``latest_boundary``) or ``ahead`` (the clock
    is before the last decided bar).
    """

    state: str
    now: str
    last_decided: str
    latest_boundary: str
    undecided: int


@dataclass(frozen=True)
class VerdictsSeen:
    """How many decided bars saw a verdict on every traded token, on some, on none (``REVIEW`` counts as seen)."""

    source: str
    every: int
    some: int
    none: int


@dataclass(frozen=True)
class DecisionStatus:
    """One decided bar: its outcome, when it was decided, what it saw, what the store now holds.

    ``outcome`` is one of ``skipped_suspect``, ``hold``, ``no_trade``,
    ``filled``, ``rejected``, ``partial``; ``reason_code`` and ``reason`` are
    carried by a rejected, a partial or a skipped decision. ``verdicts`` maps
    each traded token to the rating the decision saw (``none`` for no
    verdict, ``changed`` when the store now holds another), and
    ``targets_now`` to the weight the store holds NOW at the bar; both are
    ``None`` for a run whose config reads none.
    """

    time: str
    outcome: str
    decided_at: str | None
    delay_seconds: int | None
    reason_code: str | None
    reason: str | None
    verdicts: dict[str, str] | None
    targets_now: dict[str, Decimal | None] | None


@dataclass(frozen=True)
class RunStatus:
    """A run's state: what it is, what it decided, where it stands and what it holds.

    ``mode`` is ``backtest`` / ``paper`` / ``fork`` / ``live``; ``fills`` is
    where its fills come from (``model`` / ``quoter`` / ``chain``).
    ``clock`` is read for a paper run with a decided bar; ``holdings``,
    ``value`` and ``metrics`` need a decided bar too (``metrics_error`` says
    why ``metrics`` is ``None`` when a return could not be measured).
    ``recent_decisions`` are the latest few, oldest first as the lines print
    them.
    """

    run_id: str
    mode: str
    fills: str
    strategy: str
    quote: str
    created_at: str
    fork_block: int | None
    opening: Holdings
    decided: int
    first_decided: str | None
    last_decided: str | None
    outcomes: dict[str, int]
    clock: Clock | None
    holdings: Holdings | None
    holdings_at: str | None
    value: Decimal | None
    metrics: Metrics | None
    metrics_error: str | None
    verdicts_seen: VerdictsSeen | None
    recent_decisions: tuple[DecisionStatus, ...]


@dataclass(frozen=True)
class Status:
    """What ``status`` prints: the store under the config, and the run when one was named."""

    schema_version: int
    source: str
    generated_at: str
    store: StoreStatus
    run: RunStatus | None


# --- what status and report both say ------------------------------------------------


def outcome_counts(found: Mapping[Outcome, int]) -> str:
    """Every outcome with its count, those that did not occur at zero."""
    return counts({outcome.value: found.get(outcome, 0) for outcome in Outcome})


def stored_run(store: Store, run_id: str) -> RunRecord:
    run = store.run(run_id)
    if run is None:
        raise StoreError(f"there is no run {run_id!r} in the store")
    return run


def run_header(
    *, run_id: str, mode: str, fork_block: int | None, fills: str, strategy: str, quote: str
) -> str:
    """The one line that names a run, as ``status`` and ``report`` both print it."""
    forked = "" if fork_block is None else f" (the fork was at block {fork_block} when it started)"
    return (
        f"run {one_ascii_line(run_id)}: {mode}{forked}, fills from the {fills}, "
        f"{one_ascii_line(strategy)}, values in {quote}"
    )


def decided_span(decided: int, first: str, last: str) -> str:
    return f"{decided} bar(s) decided from {first} to {last}"


def measured(
    run: RunRecord,
    config: UniswapConfig,
    decisions: Sequence[Decision],
    valuations: Sequence[Valuation],
    fills: Sequence[FillRecord],
) -> tuple[list[Valuation], RunMetrics]:
    """The valuations a run is measured on, and the run, started under ``config``, measured on them.

    As ``report`` measures it.
    """
    found = measurable(decisions, valuations)
    metrics = run_metrics(
        quote=run.quote,
        gas_token=WRAPPED_NATIVE[config.chain_id].symbol,
        opening=run.ledger,
        valuations=found,
        fills=fills,
        fee_rates={pool.address: pool.fee_rate for pool in config.pools},
    )
    return found, metrics


def verdicts_seen(source: str, traded: int, decisions: Sequence[Decision]) -> VerdictsSeen:
    """How many decided bars saw a verdict on every one of the ``traded`` tokens, on some and on none.

    A ``REVIEW`` counts as seen, as ``status`` counts it: the judge was
    asked and answered. What a strategy made of each bar is its own policy
    (``ai_gated_weights`` takes no new risk on a token without a rating). A
    bar skipped as suspect is not counted: nothing was decided on it, and no
    judge is asked about it.
    """
    seen = [len(decision.verdicts or {}) for decision in decisions if not decision.suspect]
    every, none = seen.count(traded), seen.count(0)
    return VerdictsSeen(source=source, every=every, some=len(seen) - every - none, none=none)


# --- reading -----------------------------------------------------------------


def _text(value: str | None) -> str | None:
    """Free text: masked for a quoted key or address, and cut at the limit."""
    if value is None:
        return None
    text = _HEX_SECRET.sub(lambda found: f"{found.group()[:6]}...{found.group()[-4:]}", value)
    return text if len(text) <= TEXT_LIMIT else text[: TEXT_LIMIT - 3] + "..."


def _iso_or_none(time: int | None) -> str | None:
    return None if time is None else utc_text(time)


def _holdings(ledger: Ledger) -> Holdings:
    return Holdings(balances=dict(sorted(ledger.balances.items())), gas_eth=ledger.gas_eth)


def _read_store(store: Store, config: UniswapConfig, bars: int) -> StoreStatus:
    interval = config.bars.interval_seconds
    pools: list[PoolExtent] = []
    times: set[int] = set()
    for pool in config.pools:
        count, first, last = store.extent(config.chain_id, pool.address, interval)
        pools.append(PoolExtent(pool_key(pool), count, _iso_or_none(first), _iso_or_none(last)))
        times.update(store.latest_times(config.chain_id, pool.address, interval, bars))
    latest: list[BarStatus] = []
    with_bar: list[int] = []
    for time in sorted(times)[-bars:]:
        stored = load_bar(store, config, time)
        if stored is None:
            latest.append(BarStatus(utc_text(time), False, None, {}, None, None, ()))
            continue
        with_bar.append(time)
        flags = tuple(
            f"{pool_key(pool)}:{flag.value}"
            for pool, found in zip(config.pools, stored.flags, strict=True)
            for flag in sorted(found, key=lambda flag: flag.value)
        )
        latest.append(
            BarStatus(
                time=utc_text(time),
                complete=True,
                close_block=stored.bar.close_block,
                prices=dict(sorted(stored.bar.prices.items())),
                finality=stored.finality.value,
                suspect=stored.bar.suspect,
                flags=flags,
            )
        )
    coverage: list[Coverage] = []
    for settings, load, what in (
        (config.verdicts, load_verdicts, "verdicts"),
        (config.targets, load_targets, "targets"),
    ):
        if settings is None:
            continue
        per_token = dict.fromkeys(sorted(config.traded_symbols), 0)
        complete = 0
        for time in with_bar:
            # Already cut to the traded tokens, so every one is a token counted here.
            said = load(store, config, time)
            for symbol in said:
                per_token[symbol] += 1
            complete += len(said) == len(per_token)
        coverage.append(Coverage(what, settings.source, complete, len(with_bar), per_token))
    return StoreStatus(
        chain_id=config.chain_id,
        interval_seconds=interval,
        quote=config.quote.symbol,
        pools=tuple(pools),
        latest_bars=tuple(latest),
        coverage=tuple(coverage),
    )


def _saw(store: Store, config: UniswapConfig, decision: Decision) -> dict[str, str] | None:
    """What the decision saw of each traded token's verdict; ``None`` for a run that reads none.

    The rating is read back from the store at the decision's bar and matched
    by the digest the decision kept. A token the decision saw no verdict on
    reads ``none``, and one whose verdict the store now holds differently
    from what the decision saw, or did not hold then, or does not hold now,
    reads ``changed``: the same difference a replay warns of.
    """
    if decision.verdicts is None:
        return None
    if config.verdicts is None:
        raise StoreError(
            f"the decision at {decision.time} saw verdicts, and the run's config reads none"
        )
    now = load_verdicts(store, config, decision.time)
    seen: dict[str, str] = {}
    for symbol in sorted(config.traded_symbols):
        kept, held = decision.verdicts.get(symbol), now.get(symbol)
        if kept is None and held is None:
            seen[symbol] = "none"
        elif kept is not None and held is not None and held.digest == kept:
            seen[symbol] = held.rating.value
        else:
            seen[symbol] = "changed"
    return seen


def _aimed(
    store: Store, config: UniswapConfig, decision: Decision
) -> dict[str, Decimal | None] | None:
    """The targets the store holds NOW at the decision's bar; ``None`` for a run that reads none.

    "Now", not "saw": a decision keeps no target digest, so a target written
    after the bar was decided reads the same as one it followed.
    """
    if config.targets is None:
        return None
    held = load_targets(store, config, decision.time)
    return {
        symbol: (held[symbol].weight if symbol in held else None)
        for symbol in sorted(config.traded_symbols)
    }


def _clock(config: UniswapConfig, last: int, now: int) -> Clock:
    interval = config.bars.interval_seconds
    latest = now - now % interval
    if latest < last:
        state, undecided = "ahead", 0
    elif latest == last:
        state, undecided = "up_to_date", 0
    else:
        state, undecided = "behind", (latest - last) // interval
    return Clock(state, utc_text(now), utc_text(last), utc_text(latest), undecided)


def _curve(curve: Curve) -> CurveStatus:
    return CurveStatus(curve.start, curve.end, curve.total_return, curve.max_drawdown)


def _metrics(found: Sequence[Valuation], metrics: RunMetrics, suspect: int) -> Metrics:
    return Metrics(
        bars=metrics.bars,
        measured_from=utc_text(found[0].time),
        measured_to=utc_text(found[-1].time),
        suspect_left_out=suspect,
        strategy=_curve(metrics.strategy),
        opening_held=_curve(metrics.opening_held),
        all_quote=_curve(metrics.all_quote),
        rebalances=metrics.rebalances,
        swaps=metrics.swaps,
        traded_value=metrics.traded_value,
        turnover=metrics.turnover,
        costs=CostsStatus(
            pool_fees=metrics.costs.pool_fees,
            slippage=metrics.costs.slippage,
            gas=metrics.costs.gas,
            gas_eth=metrics.costs.gas_eth,
            total=metrics.costs.total,
        ),
    )


def _decision(store: Store, config: UniswapConfig, decision: Decision) -> DecisionStatus:
    return DecisionStatus(
        time=utc_text(decision.time),
        outcome=decision.outcome.value,
        decided_at=_iso_or_none(decision.decided_at),
        delay_seconds=None if decision.decided_at is None else decision.decided_at - decision.time,
        reason_code=None if decision.reason_code is None else decision.reason_code.value,
        reason=_text(decision.reason),
        verdicts=_saw(store, config, decision),
        targets_now=_aimed(store, config, decision),
    )


def _read_run(store: Store, run_id: str, latest: int, now: int) -> RunStatus:
    run = stored_run(store, run_id)
    decisions = store.decisions(run_id)
    found = Counter(decision.outcome for decision in decisions)
    clock: Clock | None = None
    holdings: Holdings | None = None
    holdings_at: str | None = None
    value: Decimal | None = None
    metrics: Metrics | None = None
    metrics_error: str | None = None
    seen: VerdictsSeen | None = None
    recent: tuple[DecisionStatus, ...] = ()
    if decisions:
        valuations = store.valuations(run_id)
        if not valuations or valuations[-1].time != decisions[-1].time:
            raise StoreError(
                f"the decision of run {run_id!r} at {decisions[-1].time} has no valuation"
            )
        # The run's own config: the sources it reads, not the ones the caller's config names.
        config = config_from_snapshot(run.config)
        last = valuations[-1]
        if run.mode is RunMode.PAPER:
            clock = _clock(config, decisions[-1].time, now)
        holdings, holdings_at, value = _holdings(last.ledger), utc_text(last.time), last.total_value
        try:
            measured_on, run_metrics_found = measured(
                run, config, decisions, valuations, store.fills(run_id)
            )
        except MetricsError as exc:
            metrics_error = _text(str(exc))
        else:
            metrics = _metrics(measured_on, run_metrics_found, len(valuations) - len(measured_on))
        if config.verdicts is not None:
            seen = verdicts_seen(config.verdicts.source, len(config.traded_symbols), decisions)
        recent = tuple(_decision(store, config, decision) for decision in decisions[-latest:])
    return RunStatus(
        run_id=run.run_id,
        mode=run.mode.value,
        fills=run.fills.value,
        strategy=run.strategy,
        quote=run.quote,
        created_at=utc_text(run.created_at),
        fork_block=run.fork_block,
        opening=_holdings(run.ledger),
        decided=len(decisions),
        first_decided=utc_text(decisions[0].time) if decisions else None,
        last_decided=utc_text(decisions[-1].time) if decisions else None,
        outcomes={outcome.value: found.get(outcome, 0) for outcome in Outcome},
        clock=clock,
        holdings=holdings,
        holdings_at=holdings_at,
        value=value,
        metrics=metrics,
        metrics_error=metrics_error,
        verdicts_seen=seen,
        recent_decisions=recent,
    )


def read_status(
    store: Store,
    config: UniswapConfig,
    *,
    bars: int,
    run_id: str | None,
    now: Callable[[], float],
) -> Status:
    """The store under ``config`` and, when ``run_id`` is given, that run, read in the caller's view.

    ``bars`` is how many of the latest bars, and of the run's latest
    decisions, to carry. Raises :class:`~.store.repository.StoreError` for a
    run the store does not hold.
    """
    moment = int(now())
    return Status(
        schema_version=SCHEMA_VERSION,
        source=SOURCE,
        generated_at=utc_text(moment),
        store=_read_store(store, config, bars),
        run=None if run_id is None else _read_run(store, run_id, bars, moment),
    )


# --- rendering -----------------------------------------------------------------


def _json_default(value: object) -> str:
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(f"a status carries no {type(value).__name__} (got {value!r})")


def status_json(status: Status) -> str:
    """The status as an ASCII JSON document, field by field, ``Decimal`` as exact text."""
    return json.dumps(asdict(status), default=_json_default, indent=2)


def _store_lines(store: StoreStatus) -> list[str]:
    lines = [
        f"chain {store.chain_id}, {store.interval_seconds}-second bars, prices in {store.quote}"
    ]
    for pool in store.pools:
        span = "" if pool.first is None or pool.last is None else f", {pool.first} to {pool.last}"
        lines.append(f"{pool.pool}: {pool.readings} reading(s){span}")
    for bar in store.latest_bars:
        if not bar.complete:
            lines.append(f"{bar.time}  incomplete: a configured pool has no reading")
            continue
        prices = "  ".join(f"{symbol} {price_text(price)}" for symbol, price in bar.prices.items())
        lines.append(
            f"{bar.time}  block {bar.close_block}  {prices}  {bar.finality}  "
            f"{'suspect' if bar.suspect else 'ok'}  flags: {', '.join(bar.flags) or 'none'}"
        )
    for coverage in store.coverage:
        lines.append(
            f"{coverage.what} from {coverage.source}: {coverage.complete} of the latest "
            f"{coverage.of} bar(s) have one for every token ({counts(coverage.per_token)})"
        )
    return lines


def _clock_line(clock: Clock) -> str:
    if clock.state == "ahead":
        return (
            f"the clock is behind the run: it is at {clock.now}, and the run has decided the "
            f"bar at {clock.last_decided}"
        )
    if clock.state == "up_to_date":
        return "up to date: the latest boundary that has passed is decided"
    return (
        f"behind: {clock.undecided} boundary(ies) after the last decided bar have passed "
        f"undecided, the latest at {clock.latest_boundary}; a visit decides them, and the visit "
        f"log says what stopped one"
    )


def _value_line(run: RunStatus) -> str:
    assert run.value is not None
    value = f"value {fixed_text(run.value)} {run.quote}"
    if run.metrics is None:
        return f"{value}; return not measured ({one_ascii_line(run.metrics_error)})"
    change = fixed_text(run.metrics.strategy.total_return * 100, signed=True)
    return (
        f"{value}; return {change}% from {run.metrics.measured_from} to "
        f"{run.metrics.measured_to}, after gas, as report measures it"
    )


def _decision_line(decision: DecisionStatus, *, paper: bool) -> str:
    if decision.decided_at is None or decision.delay_seconds is None:
        when = "decided at a time that was not kept"
    elif paper:
        # How late the visit came. A backtest decides its bars long after, by design.
        when = f"decided {decision.decided_at}, {after(decision.delay_seconds)} after the boundary"
    else:
        when = f"decided {decision.decided_at}"
    saw = (
        ""
        if decision.verdicts is None
        else "  verdicts: " + ", ".join(f"{s}={v}" for s, v in decision.verdicts.items())
    )
    aimed = (
        ""
        if decision.targets_now is None
        else "  targets now: "
        + ", ".join(
            f"{s}={'none' if w is None else plain(w)}" for s, w in decision.targets_now.items()
        )
    )
    why = "" if decision.reason is None else f" ({one_ascii_line(decision.reason)})"
    return f"{decision.time}  {decision.outcome}  {when}{saw}{aimed}{why}"


def _holdings_text(holdings: Holdings) -> str:
    """``holdings`` as one line, as ``engine.step.holdings_text`` prints a ledger."""
    amounts = ", ".join(f"{symbol} {plain(amount)}" for symbol, amount in holdings.balances.items())
    return f"{amounts}; gas ETH {plain(holdings.gas_eth)}"


def _run_lines(run: RunStatus) -> list[str]:
    header = run_header(
        run_id=run.run_id,
        mode=run.mode,
        fork_block=run.fork_block,
        fills=run.fills,
        strategy=run.strategy,
        quote=run.quote,
    )
    lines = [f"{header}, started {run.created_at}"]
    if not run.decided:
        lines.append(f"no bar decided yet; opening balances {_holdings_text(run.opening)}")
        return lines
    assert run.first_decided is not None and run.last_decided is not None
    lines.append(
        decided_span(run.decided, run.first_decided, run.last_decided) + ": " + counts(run.outcomes)
    )
    if run.clock is not None:
        lines.append(_clock_line(run.clock))
    assert run.holdings is not None  # a decided bar has its valuation
    lines.append(f"holdings after the bar at {run.holdings_at}: {_holdings_text(run.holdings)}")
    lines.append(_value_line(run))
    lines.append(f"latest {len(run.recent_decisions)} decision(s):")
    paper = run.mode == RunMode.PAPER.value
    lines.extend(_decision_line(decision, paper=paper) for decision in run.recent_decisions)
    return lines


def status_lines(status: Status) -> list[str]:
    """The status as ``status`` has always printed it: the store, then the run when one was named."""
    lines = _store_lines(status.store)
    if status.run is not None:
        lines += _run_lines(status.run)
    return lines
