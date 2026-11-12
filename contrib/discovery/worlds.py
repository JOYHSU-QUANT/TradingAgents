"""A world: one seed turned into the history the research package reads as a market (plan §3 step 1, §4.1).

Generator version ``1`` makes exactly what a research store holds for one
coin — the 4h decision bars, the daily backdrop and the hourly funding
settlements — in the shape the research package's own scans refuse
anything else of: bars on a midnight-anchored grid, each closing one
millisecond before the next opens, with the daily bars aggregated from
the same path and the settlements posted a few milliseconds after every
hour. Nothing here reads a venue; the only market this package ever
touches is the one it writes.

**Null-v1** (``kind: null-v1``). The close is a martingale in price:
``log P[t] = log P[t-1] + drift + vol·Z[t] - vol²/2`` with ``Z`` i.i.d.
standard normal and ``vol`` per BAR (plan §4.1). The ``-vol²/2`` term is
what makes the conditional expectation of the next close the current one
at ``drift = 0``; without it a zero-mean log return carries a long-side
edge of half the variance, and the null world would not be null. Each bar
opens at the previous close (one continuous path, no gap for a rule to
earn from a close-to-open jump), and its high and low are the running
maximum and minimum of the Brownian path INSIDE the bar, drawn exactly
from the bridge between its open and close (the comment at ``_path``), so
a bar's range is correlated with its move as a real bar's is. ``drift``
is refused unless zero: a null world is the martingale, and a world with a
long-side edge in its price alone is another kind of world, not this one
with a knob turned. Funding is exogenous and i.i.d.:
``f[k] = sigma·W[k]``, ``W`` standard normal, drawn from its own stream —
conditional expectation zero at every bar, variance non-zero (a constant
zero would leave ``funding_zscore`` with nothing to standardise by; an
autoregressive series would be a rule to find in a world that must hold
none; plan §1 #12).

**Conditional-v1** (``kind: conditional-v1``). The same draws, plus one
thing: on every bar the planted rule would be HOLDING a position, the log
return carries ``beta`` in the direction of that position (a short-side
plant is ``-beta``). Which bars those are is not re-implemented here — it
is the research package's own reading of the rule, :func:`replay_sides`
over the same history, which decides at a bar's close and fills at the
next bar's open, holds through ``max_bars`` or the exit rule, and lets an
opposite entry reverse (plan §1 #11). So the effect lands on the bars from
the fill onward and never on the bar the decision was taken at, which is
exactly the span a position opened on the rule would earn over.

The rule is read on the PRE-EFFECT path: the null world of the same seed.
A price-derived gate (``realized_vol_20``, ``regime``) on the final path
can therefore differ from the gate the effect was keyed to, at the bars
where it sits exactly at its threshold — ``beta`` is small against ``vol``
and the funding entry is exogenous, so the difference is a margin, not a
displacement, and the oracle (plan §3 step 3) measures the FINAL path:
the figure ``beta`` is sized by on development seeds is what the rule
actually earns, not what was keyed in. A fixed-point pass would close the
margin and is not v1's definition; a change to this reading is a new
generator version, never a quiet edit.

**Conditional-v2** (``kind: conditional-v2``). Conditional-v1 with one
declared number more: ``price.drift``, a per-bar log drift AGAINST the
planted side, on every bar of the world, the warm-up included. A
conditional-v1 world is not a martingale once its planted bars are counted
in: a one-sided plant's effect has one sign, so the price as a whole
drifts by ``beta`` times the share of bars the rule holds, and a position
held on every bar on the planted side earns that without reading the rule
at all. The declared drift takes it away. Sized to ``-beta`` times that
share for a long-side plant (the opposite sign for a short-side one), it
leaves the price as a whole without a drift: the held bars carry
``beta + drift`` and every other bar ``drift``. It is DECLARED, a constant
of the manifest, and not computed from the path: a constant measured over
the whole world would
make every bar depend on the bars after it, and the prefix guarantee below
would be gone. So the offset is exact only for the share it was sized by,
and what one seed's own share leaves over is reported rather than hidden
(:attr:`WorldHistory.unconditional_drift`). The rule is read on the
pre-effect path as before — here the drifting walk without the effect.
Refused: a rule that enters both sides (one drift cannot stand against
both), a drift of zero or on the plant's own side (nothing is offset), a
drift as large as ``beta`` (the held bars would carry nothing), and — once
the path is laid and the seed's own share is known — a drift that leaves
the price drifting no less than the plant alone gave it, which is the
shortcut back on the other side (:attr:`WorldHistory.planted_drift`).

What the offset does not do is make the rest of the world a martingale.
Every bar the rule does not hold carries ``drift``, so a position AGAINST
the plant held on exactly those bars earns, in total, what the plant
earns, over several times the exposure. That is the plant's mirror, and it
takes reading the rule to hold it, so it is not a way around the rule; but
it is in the world, and no null world shows it: the null twin of a
conditional-v2 world is still the null-v1 martingale of its seed, because
a drift with nothing planted is an edge in the price alone.

**What the seed decides.** Three streams, each seeded from the world's
opaque id and a name (``price``, ``wicks``, ``funding``) — from the id,
which the kind never enters (:func:`~contrib.discovery.manifest.opaque_world_id`),
so the null world and the conditional world of one seed share every draw
and differ only by what is planted — the effect, and on a conditional-v2
world the drift declared against it — and a conditional world is read
against its own null twin. Each stream is consumed in bar order, one bar's
draws before the next bar's, so a shorter world of the same seed is a
PREFIX of a longer one: no bar's draw depends on a later bar's, which is
the generator's own no-look-ahead guarantee (plan PR 2's acceptance), held
by construction and tested as a prefix. The same seed gives the same
history on the same build — ``random.Random`` seeded by text is specified
to — and the floating arithmetic of ``math.exp`` is the platform's, so
bit-exactness is a claim about one machine; a world is landed once and
copied, never regenerated per arm.

Prices are written as the shortest decimal that reads back to the same
float (``repr``), not snapped to a tick: a tick is a venue's rule and this
world has no venue, and the evaluator reads every price as a float anyway.
The one place that matters is a bar's open, which is the previous close as
the same float and therefore the same decimal.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Final

from .manifest import Manifest, Planted, World, WorldKind
from .upstream import (
    DAILY_INTERVAL,
    FUNDING_INTERVAL_MS,
    FUNDING_PAGE_DAYS,
    MAX_EPOCH_MS,
    MS_PER_DAY,
    Candle,
    CostModel,
    FeatureError,
    FeatureFrame,
    FundingPoint,
    SeriesBundle,
    Side,
    bar_span_ms,
    epoch_ms,
    from_epoch_ms,
    interval_to_ms,
    replay_sides,
    studied_interval,
)

__all__ = [
    "GENERATOR_VERSION",
    "PATH_INTERVAL",
    "SETTLEMENT_LAG_MS",
    "SyntheticHistory",
    "WorldError",
    "WorldHistory",
    "generate",
]

# Bumped whenever a world of the same manifest would come out different: a
# change to a formula, a stream, the wick model, the lag. A manifest names
# the version it was generated under, and :func:`generate` refuses any other
# by name — a world from another generator is another world. Conditional-v2
# was added without a bump: a manifest names its kind, no earlier build read
# that one, and every manifest an earlier build made a world of still makes
# the same world.
GENERATOR_VERSION: Final = "1"

# The interval the path is laid on. The daily backdrop is aggregated from
# it, so a manifest asking for decision bars on another interval describes a
# world this generator does not make (``Bars.interval`` admits both studied
# intervals; the generator admits this one).
PATH_INTERVAL: Final = "4h"

# Where on the hour a settlement is stamped. The venue posts a few tens of
# milliseconds past the hour (2–99 ms measured; ``constants`` in the research
# package), and the research package reads settlements by that fact: a
# settlement stamped exactly ON a bar's open would belong to no bar under the
# evaluator's ``(open, close]`` rule, and one stamped exactly on the close
# would be read as that bar's rate where the venue's would not. A fixed
# fifty: inside the venue's measured range and well inside the scan's
# tolerance, off both exact edges. The venue jitters; this does not — the
# tolerance path is the research package's to exercise, not this world's.
SETTLEMENT_LAG_MS: Final = 50

# A synthetic market has no volume to speak of. Constant and positive, so
# the bar is a bar; nothing in the vocabulary reads it.
_VOLUME: Final = Decimal(1)


class WorldError(ValueError):
    """The world cannot be generated or served as asked, and the sentence says why.

    A ``ValueError`` as the manifest's refusals are: a parameter outside what
    this generator makes, or a request the synthetic venue has no answer to.
    """


def _require_this_generator(world: World) -> None:
    """Refuse a world of another generator version or on another path interval, by name."""
    if world.generator_version != GENERATOR_VERSION:
        raise WorldError(
            f"{world.id}: this build generates version {GENERATOR_VERSION!r}, the manifest "
            f"names {world.generator_version!r}; a world of another generator is another world"
        )
    if world.bars.interval != PATH_INTERVAL:
        raise WorldError(
            f"{world.id}: generator {GENERATOR_VERSION} lays the path on {PATH_INTERVAL} bars "
            f"and aggregates the {DAILY_INTERVAL} backdrop from it; a {world.bars.interval} "
            f"decision series is not a world it makes"
        )


def _require_this_drift(world: World) -> None:
    """Refuse a price drift this generator does not lay, by the world's kind.

    None on a null-v1 or a conditional-v1 world (the martingale, and the
    plant over it); on a conditional-v2 one, the declared offset of its
    plant.
    """
    if world.kind is WorldKind.CONDITIONAL_V2:
        _require_an_offset(world)
    elif world.price.drift != 0:
        raise WorldError(
            f"{world.id}: a {world.kind.value} world has no drift of its own; a price drift of "
            f"{world.price.drift!r} is a world with an edge in its price alone, which is "
            f"another kind (plan §4.1; {WorldKind.CONDITIONAL_V2.value} declares one against "
            f"its plant), not a parameter of this one"
        )


def _require_an_offset(world: World) -> None:
    """Refuse a conditional-v2 drift that is not against its plant, or offsets all of it."""
    planted = world.planted
    assert planted is not None  # ``World``'s pairing rule
    entered = [side for side in Side if planted.spec.entries(side)]
    if len(entered) != 1:
        raise WorldError(
            f"{world.id}: a {world.kind.value} world declares one price drift against the side "
            f"its rule enters, and this rule enters "
            f"{' and '.join(side.value for side in entered)}; one drift cannot stand against "
            f"both. A two-sided plant is a {WorldKind.CONDITIONAL_V1.value} world, which keeps "
            f"whatever drift its two sides leave between them"
        )
    drift, beta = world.price.drift, planted.beta
    against = -1.0 if entered[0] is Side.LONG else 1.0
    if not 0 < drift * against < beta:
        raise WorldError(
            f"{world.id}: a {world.kind.value} world declares a price drift against its "
            f"{entered[0].value}-side plant and smaller than the effect it offsets — strictly "
            f"between 0 and {against * beta!r}, sized to beta times the share of bars the rule "
            f"holds; got price.drift = {drift!r} with beta = {beta!r}"
        )


# -- the history ----------------------------------------------------------------------


@dataclass(frozen=True)
class WorldHistory:
    """One generated world: the three series a research store holds, and where the effect landed.

    ``bars`` is the whole path, warm-up first — ``world.bars.warmup_bars``
    bars before the first decision bar, then ``world.bars.count`` of those.
    ``daily`` holds the COMPLETE days of that path (a trailing partial day
    has not closed and is not a bar). ``funding`` holds one settlement per
    hour from the first bar's open through the one due at the last bar's
    close. ``planted_sides`` is index-aligned with ``bars`` and names, for
    each bar, the side whose effect the bar carried — ``None`` on every bar
    of a null world, and on every bar of a conditional one where the rule
    held nothing (a conditional-v2 world's declared drift is on every bar,
    and is not what this names).
    """

    manifest: Manifest
    bars: tuple[Candle, ...]
    daily: tuple[Candle, ...]
    funding: tuple[FundingPoint, ...]
    planted_sides: tuple[Side | None, ...]

    def __post_init__(self) -> None:
        world = self.world
        # The checks below read the path as this generator lays it (its
        # interval, its settlement grid), so a history of another generator
        # or interval is refused here too, not only at ``generate`` — and
        # with them a MANIFEST whose price drift its kind does not admit.
        # What is held is the declaration: the bars are not laid again
        # here to show they carry it, any more than for the seed or beta.
        _require_this_generator(world)
        _require_this_drift(world)
        expected = world.bars.warmup_bars + world.bars.count
        if len(self.bars) != expected:
            raise WorldError(
                f"{world.id}: {len(self.bars)} bars for a world of {expected} "
                f"({world.bars.warmup_bars} warm-up + {world.bars.count})"
            )
        if len(self.planted_sides) != len(self.bars):
            raise WorldError(
                f"{world.id}: {len(self.planted_sides)} planted sides for {len(self.bars)} bars"
            )
        # The three series are ONE world: the daily bars are the path's own
        # days and the settlements its own hourly grid, checked here rather
        # than trusted, so a history assembled by hand (a test double, a
        # later caller) cannot serve a backdrop the path did not make.
        if self.daily != _daily(self.bars):
            raise WorldError(f"{world.id}: the daily bars are not the path's complete days")
        stamps = [point.time for point in self.funding]
        if stamps != [
            world.origin_ms + k * FUNDING_INTERVAL_MS + SETTLEMENT_LAG_MS
            for k in range(len(self.bars) * _SETTLEMENTS_PER_BAR + 1)
        ]:
            raise WorldError(
                f"{world.id}: the settlements are not one per hour from the first open through "
                f"the one due at the last close"
            )
        if world.planted is None and any(side is not None for side in self.planted_sides):
            raise WorldError(f"{world.id}: a {world.kind.value} world carries no effect")
        if world.planted is not None and self.planted_exposure == 0:
            # The other pairing the manifest cannot see: a rule that holds
            # nothing on the DECISION bars plants nothing an arm could find,
            # and the world would be a null world filed as conditional —
            # every arm's "did not find it" counted against a rule that was
            # not there. The warm-up does not count: no window measures it.
            raise WorldError(
                f"{world.id}: the planted rule never holds a position on this seed's decision "
                f"bars, so nothing is planted; a {world.kind.value} world carries an effect "
                f"where it is measured — change the rule, its thresholds or the seed"
            )
        if world.kind is WorldKind.CONDITIONAL_V2 and abs(self.unconditional_drift) >= abs(
            self.planted_drift
        ):
            # The half of the offset's rule only the path can say. The drift
            # is sized by a share, and on this seed the rule holds another;
            # one that leaves as much as the plant gave (a drift of twice
            # the plant's own, or more) is no offset — the shortcut it was to take
            # away is back, on the other side.
            raise WorldError(
                f"{world.id}: the declared drift of {world.price.drift!r} leaves "
                f"{self.unconditional_drift:+.3g} per decision bar where the plant alone gave "
                f"this seed's price {self.planted_drift:+.3g} — no less than it found; a "
                f"{world.kind.value} drift is sized to beta times the share of decision bars "
                f"the rule holds ({self.planted_exposure / world.bars.count:.1%} here)"
            )

    @property
    def world(self) -> World:
        return self.manifest.world

    @property
    def planted_exposure(self) -> int:
        """How many DECISION bars carry the effect: the plant as an arm could meet it."""
        return sum(side is not None for side in self.planted_sides[self.world.bars.warmup_bars :])

    @property
    def planted_drift(self) -> float:
        """The planted effect as a mean per-bar log drift of the DECISION bars, signed towards long.

        ``beta`` times the share of decision bars held long, less the share
        held short: what the effect gives the price as a whole on this seed
        (the two sides of a two-sided plant net), and so what a
        conditional-v2 world's declared drift is there to offset.
        """
        world = self.world
        if world.planted is None:
            return 0.0
        held = self.planted_sides[world.bars.warmup_bars :]
        signed = sum(1 if side is Side.LONG else -1 for side in held if side is not None)
        return world.planted.beta * signed / len(held)

    @property
    def unconditional_drift(self) -> float:
        """The mean per-bar log drift of the DECISION bars, signed towards long: the declared drift and the effect.

        What a LONG position held on every decision bar earns per bar over
        the martingale without reading the rule, and a short one loses:
        nothing on a null world, :attr:`planted_drift` on a conditional-v1
        one, and on a conditional-v2 one whatever its declared offset leaves
        of that for this seed's own share. One mean over the decision bars
        together; a single window's share is its own.
        """
        return self.world.price.drift + self.planted_drift

    @property
    def clock_ms(self) -> int:
        """The venue's clock: the millisecond after the last settlement posted.

        Every window a backfill cuts is cut at this instant, so the whole
        path (every bar has closed) and every settlement, the one due at the
        last close included, are served. ``load_bundle`` reads settlements to
        that one — the venue posts it a few ms after the close — and a window
        ending on the last bar counts it; a clock set AT the last close
        would leave every real-looking window one settlement short.
        """
        return self.funding[-1].time + 1


# -- the port -----------------------------------------------------------------------------


@dataclass(frozen=True)
class SyntheticHistory:
    """A :class:`~contrib.autoresearch.ports.HistoryMarketData` over one generated world.

    Answers for the world's own coin and nothing else, with the windowing
    the venue reader has: candles CLOSED as of ``end`` (``close_time <=
    end``), the newest ``lookback`` of them, oldest first; settlements
    inside the ``window_days`` trailing ``end``, both edges inclusive. The
    clock is the world's (:attr:`WorldHistory.clock_ms`), so a backfill cut
    at it lands the whole world.

    A coin this world is not is a refusal, never an empty page: the walk
    reads an empty page as "the venue served nothing older" and records a
    short history that scans as a market which listed late. The refusal is
    a ``WorldError``, not the venue family the walk waits out, because a
    wrong coin here is the runner's defect, not an outage.
    """

    history: WorldHistory

    def _require_coin(self, coin: str) -> None:
        if coin != self.history.world.id:
            raise WorldError(
                f"this world is {self.history.world.id}; it has no history for {coin!r}"
            )

    def get_exchange_time(self, coin: str) -> datetime:
        self._require_coin(coin)
        return from_epoch_ms(self.history.clock_ms)

    def get_candles(
        self, coin: str, interval: str, lookback: int, *, end: datetime
    ) -> list[Candle]:
        self._require_coin(coin)
        # ``studied_interval`` admits exactly the two intervals the world
        # has, so the lookup cannot miss; a third studied interval upstream
        # would fail here as a ``KeyError`` — loudly, which is right.
        series = {PATH_INTERVAL: self.history.bars, DAILY_INTERVAL: self.history.daily}[
            studied_interval(interval)
        ]
        end_ms = epoch_ms(end, what=f"{coin} {interval} candle window end")
        closed = [bar for bar in series if bar.close_time <= end_ms]
        # The venue reader's own trim, including its reading of a zero
        # lookback as "no trim": the walk never asks for zero, and a port
        # that answered it differently would be a second contract.
        return closed[-lookback:] if lookback and len(closed) > lookback else closed

    def get_funding_history(
        self, coin: str, window_days: int, *, end: datetime
    ) -> list[FundingPoint]:
        self._require_coin(coin)
        end_ms = epoch_ms(end, what=f"{coin} funding window end")
        start_ms = end_ms - window_days * MS_PER_DAY
        return [point for point in self.history.funding if start_ms <= point.time <= end_ms]


# -- the draws ------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Draws:
    """Every random number a world consumes, drawn before any price is built.

    Drawn first and kept, so the pre-effect path and the final path of one
    world are built from the SAME numbers and differ by the effect alone.
    ``ups``/``downs`` are the wick draws: uniforms on ``(0, 1]``.
    """

    steps: tuple[float, ...]
    ups: tuple[float, ...]
    downs: tuple[float, ...]
    settlements: tuple[float, ...]


def _stream(world: World, name: str) -> random.Random:
    """The stream ``name`` of ``world``: seeded by the opaque id, which the kind never enters."""
    return random.Random(f"{world.id}/{name}")


_SETTLEMENTS_PER_BAR: Final = interval_to_ms(PATH_INTERVAL) // FUNDING_INTERVAL_MS


def _draw(world: World) -> _Draws:
    bars = world.bars.warmup_bars + world.bars.count
    price, wicks, funding = (_stream(world, name) for name in ("price", "wicks", "funding"))
    steps = tuple(price.gauss(0.0, 1.0) for _ in range(bars))
    # One bar's two wicks before the next bar's: a shorter world of the same
    # seed is then a prefix of a longer one (the module docstring).
    ups, downs = [], []
    # ``1 - random()`` is on ``(0, 1]``: the bridge draw takes a logarithm,
    # and ``random()`` itself can return exactly zero.
    for _ in range(bars):
        ups.append(1.0 - wicks.random())
        downs.append(1.0 - wicks.random())
    # One settlement per hour from the first open through the one due at
    # the last close: ``per_bar`` per bar, plus that last one.
    settlements = tuple(funding.gauss(0.0, 1.0) for _ in range(bars * _SETTLEMENTS_PER_BAR + 1))
    return _Draws(steps=steps, ups=tuple(ups), downs=tuple(downs), settlements=settlements)


# -- the series -----------------------------------------------------------------------------


def _decimal(value: float) -> Decimal:
    """``value`` as the shortest decimal that reads back to it (the module docstring)."""
    return Decimal(repr(value))


def _path(world: World, draws: _Draws, sides: tuple[Side | None, ...]) -> tuple[Candle, ...]:
    """The decision bars: the log-price walk with ``sides``' effect, wicks on each body."""
    step = interval_to_ms(PATH_INTERVAL)
    span = bar_span_ms(step)
    vol, drift = world.price.vol, world.price.drift
    beta = 0.0 if world.planted is None else world.planted.beta
    log_price = math.log(world.initial_price)
    opened = world.initial_price
    # The open is the previous close carried forward as the SAME decimal, so
    # "each bar opens at the last close" holds in the store by construction.
    open_decimal = _decimal(opened)
    bars: list[Candle] = []
    try:
        for index, (z, up, down) in enumerate(
            zip(draws.steps, draws.ups, draws.downs, strict=True)
        ):
            side = sides[index]
            effect = 0.0 if side is None else (beta if side is Side.LONG else -beta)
            move = drift + vol * z - vol * vol / 2 + effect
            log_price += move
            closed = math.exp(log_price)
            # The bar's high and low are the running maximum and minimum of
            # the Brownian path inside the bar, given its two ends: for a
            # bridge of variance ``vol²`` from 0 to ``move``, the overshoot
            # ``m`` above the start has ``P(M > m) = exp(-2m(m - move)/vol²)``
            # for ``m >= max(0, move)``, so ``m = (move + sqrt(move² -
            # 2·vol²·ln U))/2`` with ``U`` uniform on (0, 1]; the undershoot
            # is the same law on the reflected path. In exact arithmetic
            # ``m`` is at or above both ends by construction; the clamp
            # below covers the float's round trip.
            above = (move + math.sqrt(move * move - 2.0 * vol * vol * math.log(up))) / 2.0
            below = (-move + math.sqrt(move * move - 2.0 * vol * vol * math.log(down))) / 2.0
            # Clamped to the body as well: in exact arithmetic the overshoot
            # is at or above both ends, but ``exp(log(p))`` is a float's
            # round trip, and on a bar whose move and wick are both below an
            # ulp the close can land past the high by that ulp.
            high = max(opened, closed, opened * math.exp(above))
            low = min(opened, closed, opened * math.exp(-below))
            if low <= 0.0 or math.isinf(high):
                # Off the bottom (a price that rounds to zero) or the top
                # (an infinity ``exp`` returned rather than raised): either
                # is the overflow below, met one bar before the exception.
                raise OverflowError("the price left the range of a float")
            open_time = world.origin_ms + index * step
            close_decimal = _decimal(closed)
            bars.append(
                Candle(
                    open_time=open_time,
                    close_time=open_time + span,
                    open=open_decimal,
                    high=_decimal(high),
                    low=_decimal(low),
                    close=close_decimal,
                    volume=_VOLUME,
                )
            )
            opened, open_decimal = closed, close_decimal
    except OverflowError as exc:
        # ``math.exp`` past a float: a manifest whose vol or beta runs the
        # walk off the top. The manifest bounds none of them, so the walk
        # refuses by name rather than dying inside a library call.
        raise WorldError(
            f"{world.id}: the price walk left the range of a number at bar {len(bars)}; vol and "
            f"beta are per-bar log returns, and these run the price past what a number holds"
        ) from exc
    return tuple(bars)


def _daily(bars: tuple[Candle, ...]) -> tuple[Candle, ...]:
    """The complete days of ``bars``, each aggregated from its own bars.

    The world's origin is a UTC midnight and the path interval divides a
    day, so every day's bars are a contiguous run on the day's edges. A
    trailing partial day is not a bar: a daily bar is visible to a decision
    bar only once the day has closed, and this one has not.
    """
    per_day = MS_PER_DAY // interval_to_ms(PATH_INTERVAL)
    days: list[Candle] = []
    for first in range(0, len(bars) - per_day + 1, per_day):
        day = bars[first : first + per_day]
        days.append(
            Candle(
                open_time=day[0].open_time,
                close_time=day[0].open_time + bar_span_ms(MS_PER_DAY),
                open=day[0].open,
                high=max(bar.high for bar in day),
                low=min(bar.low for bar in day),
                close=day[-1].close,
                volume=sum((bar.volume for bar in day), Decimal(0)),
            )
        )
    return tuple(days)


def _funding(world: World, draws: _Draws) -> tuple[FundingPoint, ...]:
    sigma = world.funding.sigma
    return tuple(
        FundingPoint(
            time=world.origin_ms + k * FUNDING_INTERVAL_MS + SETTLEMENT_LAG_MS,
            rate=_decimal(sigma * w),
        )
        for k, w in enumerate(draws.settlements)
    )


def _planted_sides(
    planted: Planted,
    bars: tuple[Candle, ...],
    daily: tuple[Candle, ...],
    funding: tuple[FundingPoint, ...],
    *,
    indicator_lookback: int,
) -> tuple[Side | None, ...]:
    """The side the planted rule holds on each bar, read by the research package's own replay.

    ``replay_sides`` gives, per bar, the side held once the decision at that
    bar's close has been applied — the side held THROUGH THE NEXT bar. So
    the effect carried by bar ``i`` is the replay's answer at bar ``i - 1``,
    and the first bar carries none: no decision preceded it. The cost model
    is the default because the replay reads it for a position's SIZE only,
    never for whether a side fires (its own docstring says so), and a size
    is not a thing this generator keeps.

    Replayed from the FIRST bar, warm-up included: the effect is a property
    of the world, not of the windows later measured on it, so wherever the
    rule's features have a value the rule holds and the drift lands — as a
    rule that was true of a market would have been true before the day an
    experiment was cut. The backdrop an arm's long features read at the
    first decision bar therefore already carries the plant, exactly as the
    decision bars do.
    """
    try:
        frame = FeatureFrame(
            SeriesBundle(bars, daily, funding), indicator_lookback=indicator_lookback
        )
        replayed = replay_sides(planted.spec, frame, CostModel(), since_ms=bars[0].open_time)
    except FeatureError as exc:
        # The rule reads a feature this history cannot answer anywhere — a
        # world too short for the rule's own window. A generator parameter,
        # refused as one.
        raise WorldError(f"the planted rule cannot be read on this world: {exc}") from exc
    return (None, *replayed.sides[:-1])


# -- the verb --------------------------------------------------------------------------------


def generate(manifest: Manifest) -> WorldHistory:
    """The manifest's world from its seed: the pre-effect path, and the planted effect on top if it plants one.

    Takes the manifest rather than its ``world`` because one field outside
    that record shapes a conditional world: ``indicator_lookback``, the
    frame parameter the planted rule is read under — the same one every arm
    measures with — which reaches the result only through which bars the
    rule holds a position on. A null world never reads it. The manifest is
    the unit that fully determines a landed world (:mod:`.landing` keys the
    run directory by it).
    """
    world = manifest.world
    # The version and the path interval first — they say whether this is a
    # world of this generator at all — and only then this generator's own
    # rules, so a manifest of another version is told that, not a parameter.
    _require_this_generator(world)
    _require_this_drift(world)
    total = world.bars.warmup_bars + world.bars.count
    last_stamp = world.origin_ms + total * interval_to_ms(PATH_INTERVAL) + SETTLEMENT_LAG_MS
    # The venue's stamp type bounds every stamp above, and the last one this
    # world writes is the settlement due at its final close — but the
    # research package's funding walk asks its last page up to a page's
    # width PAST the clock (it does not clamp, on purpose), and that window
    # end is decoded too. Refused here, by name, rather than inside the
    # walk at the first instant past the edge.
    if last_stamp + FUNDING_PAGE_DAYS * MS_PER_DAY > MAX_EPOCH_MS:
        raise WorldError(
            f"{world.id}: {total} bars from {from_epoch_ms(world.origin_ms).isoformat()} end "
            f"past the last instant a venue stamp can hold (less the funding walk's "
            f"{FUNDING_PAGE_DAYS}-day page); fewer bars, or an earlier origin"
        )
    draws = _draw(world)
    quiet: tuple[Side | None, ...] = (None,) * len(draws.steps)
    bars = _path(world, draws, quiet)
    funding = _funding(world, draws)
    sides = quiet
    if world.planted is not None:
        # Read on the pre-effect path (the module docstring), then rebuilt
        # from the same draws with the effect on the bars the rule holds.
        sides = _planted_sides(
            world.planted,
            bars,
            _daily(bars),
            funding,
            indicator_lookback=manifest.indicator_lookback,
        )
        bars = _path(world, draws, sides)
    # ``kind`` is not consulted to lay the path: it and ``planted`` agree by
    # the manifest's own rule (``World`` refuses the other pairing), and
    # ``planted`` is the half that says what to do. Which drift the kind
    # admits was settled above.
    return WorldHistory(
        manifest=manifest, bars=bars, daily=_daily(bars), funding=funding, planted_sides=sides
    )
