"""The report: the two legs' books over a span of UTC days, reconciled (carry plan §3.5).

Pure over what :mod:`.stores` read and what the perp run's handoff sidecars
say; nothing here opens a store or a clock. One :class:`BookDay` per UTC day
of the span, built by :func:`build_rows`, carries what each leg did that
day and what it was told to do; :func:`alerts` reads the rows for the
lights the plan asks for, and :func:`format_report` prints the days, the
ISO weeks and the span.

What a day is, on each leg. The perp run's day is the UTC day its
instants fall in: funding by settlement time, fees by fill time, the state
by the LAST cycle-end snapshot in the day, the cycles by their scheduled
instant. The spot run's day is its daily bar: the decision, the valuation
(the portfolio after that bar's fills, at that bar's close prices) and the
fills at the bar whose time IS the day's boundary. The two line up because
the handoff for a boundary is what both legs act on through that day: the
spot leg at the bar's fill block minutes after the boundary, the perp leg
at every cycle of the day (plan D4 and the file-target provider).

The lights (plan §2 D5 and D7, in these words):

- **delta** — the hedge's net exposure, read in the COIN, not in dollars:
  the spot token held at the day's bar plus the signed perp size at the
  day's end (a short is negative). A dollar reading would move with the
  price between the two instants on both legs alike and light for nothing;
  a quantity reading is the same number all day. It lights when the net is
  more than the threshold (10% of the larger leg, D7) on a day either leg
  holds anything — an exit that took on one leg only is the naked book the
  plan fears, whatever the day's intent was, and a perp long, which the
  hedge never asks for, adds to the spot instead of cancelling it.
- **held by D5** — a day a leg maintained for want of a usable document:
  the perp leg when no cycle applied a document and at least one found it
  unusable (a cycle that merely sat before the next boundary, ``pending``,
  is the normal schedule and does not count); the spot leg when its bar
  was decided with no target in the store. Such days in a row, as many as
  the threshold (3, D5), light the escalation on that day and on every day
  the streak goes on: a human steps in.
- **one-legged** — a day one leg acted on a document and the other had
  none to act on: the spot bar decided on a target while no perp cycle
  applied one, or the reverse.
- **different documents** — the spot target's digest (the handoff bytes the
  ``target`` command read) is not among the digests the perp sidecars of
  that day recorded: the two legs did not read the same file.
- **no sidecar** — perp cycles applied a document but no sidecar for the
  day is under the payload directory: the wrong directory, or lost files.
- **silent day** — neither leg acted on anything: no perp cycle applied a
  document or found one unusable (none ran, every one ended without a
  decision, every one only saw tomorrow's document, or the run is not a
  file target) and the spot bar was not decided, or was skipped as
  suspect. Both daemons down, a venue outage all day, a handoff written
  late for the next boundary only, the coordinator dead with the spot
  visit refusing at ``target`` — each reads as a quiet book otherwise. A
  day a leg maintained on an unusable document is D5's, not this light's.
- **sidecar not read** — a sidecar file under the payload directory that
  could not be read as one; said per file, under no day, since the day it
  belongs to is what could not be read.

Costs are each leg's own: the perp fills' fees and the spot swaps' cost at
the bar's prices (what went in less what came out — the pools' fees and
the slippage together, the spot package's own two cost lines summed
without its fee table) plus the gas, priced through the gas token when the
valuation carries its price. A cost the valuation cannot price is ``None``:
left out of the totals and said, never counted as nothing. Funding is the
perp ledger's posted income, positive when received. Net carry is funding
less the priced costs.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import Enum
from itertools import groupby
from pathlib import Path
from typing import Final, TypeVar

from .handoff import SPOT_TOKENS, Handoff, HandoffError, iso_utc, load_document, plain
from .history import floor_day, pct
from .signal import instant_ms
from .stores import (
    CycleKind,
    PerpCycle,
    PerpPosition,
    PerpSnapshot,
    SpotFill,
    SpotTarget,
    SpotValuation,
)
from .upstream import HANDOFF_SIDECAR_SUFFIX, MS_PER_DAY, RiskAction, from_epoch_ms

__all__ = [
    "DEFAULT_D5_DAYS",
    "DEFAULT_DELTA_ALERT_PCT",
    "GAS_TOKEN",
    "Alert",
    "AlertKind",
    "BookDay",
    "Sidecar",
    "SidecarProblem",
    "WeekRow",
    "alerts",
    "build_rows",
    "format_report",
    "read_sidecars",
    "weeks",
]

# The token whose price in the spot valuation prices one ETH of gas.
GAS_TOKEN: Final = "WETH"
# D7: the two legs differing by more than this share of the larger lights the delta.
DEFAULT_DELTA_ALERT_PCT: Final = 10
# D5: this many days held for want of a document in a row is the operator's.
DEFAULT_D5_DAYS: Final = 3
# The gate's words for an applied target that was not acted on.
_REFUSED: Final = frozenset({RiskAction.REJECTED, RiskAction.INVALID_FAIL_CLOSED})
# The spot engine's word for a bar it did not ask the strategy about (the
# bar was suspect): a target written for that bar was not acted on. Spelled
# here because the spot package may not be imported.
_SPOT_SKIPPED: Final = "skipped_suspect"

_ZERO: Final = Decimal(0)
_T = TypeVar("_T")


@dataclass(frozen=True)
class Sidecar:
    """One ``<payload>.handoff.json``: the document a perp cycle applied, and the bytes' digest."""

    path: Path
    cycle_at_ms: int
    digest: str
    handoff: Handoff


@dataclass(frozen=True)
class SidecarProblem:
    """A sidecar file the report could not read as one; said, not raised."""

    path: Path
    reason: str


def _hex_digest(value: object, path: Path) -> str:
    """The sidecar's ``sha256:<hex>`` as the bare hex the spot ``targets`` table holds."""
    if not isinstance(value, str) or not value.startswith("sha256:"):
        raise HandoffError(f"{path}: digest {value!r} is not sha256:<hex>")
    return value[len("sha256:") :]


def _sidecar(path: Path, *, coin: str) -> Sidecar:
    document = load_document(path)
    if not isinstance(document, dict) or not isinstance(document.get("handoff"), dict):
        raise HandoffError(f"{path}: not a handoff sidecar")
    handoff = Handoff.from_document(document["handoff"])
    if handoff.coin != coin:
        raise HandoffError(f"{path}: {handoff.coin}'s document, not {coin}'s")
    cycle_at = instant_ms(document.get("cycle_at"), f"{path}: cycle_at", error=HandoffError)
    return Sidecar(path, cycle_at, _hex_digest(document.get("digest"), path), handoff)


def read_sidecars(payloads: Path, *, coin: str) -> tuple[list[Sidecar], list[SidecarProblem]]:
    """Every handoff sidecar under ``payloads`` for ``coin``, oldest cycle first.

    A sidecar that cannot be read, is not a record of this kind, or carries
    another coin's document is a :class:`SidecarProblem`, so one bad file
    does not hide the rest; a missing directory is simply no sidecars.
    """
    sidecars: list[Sidecar] = []
    problems: list[SidecarProblem] = []
    if not payloads.is_dir():
        return sidecars, problems
    for path in sorted(payloads.glob(f"*{HANDOFF_SIDECAR_SUFFIX}")):
        try:
            sidecars.append(_sidecar(path, coin=coin))
        except HandoffError as exc:
            problems.append(SidecarProblem(path, str(exc)))
    sidecars.sort(key=lambda sidecar: sidecar.cycle_at_ms)
    return sidecars, problems


@dataclass(frozen=True)
class BookDay:
    """One UTC day of the hedged book.

    The perp leg is its first and last cycle-end snapshot of the day, the
    spot leg its bar's valuation; the readings the lights and the lines
    need are derived from those. A cost of ``None`` is one the day's
    valuation could not price. ``sidecars`` are the perp cycles' records of
    the day's document (as_of = this day).
    """

    boundary_ms: int
    token: str
    perp_funding: Decimal
    perp_funding_pending: int
    perp_fees: Decimal
    perp_first: PerpSnapshot | None
    perp_last: PerpSnapshot | None
    cycles: Mapping[CycleKind, int]
    refused: int
    sidecars: tuple[Sidecar, ...]
    spot_outcome: str | None
    spot_target: SpotTarget | None
    spot_valuation: SpotValuation | None
    spot_swap_cost: Decimal | None
    spot_gas_eth: Decimal
    spot_gas_cost: Decimal | None

    def __post_init__(self) -> None:
        if (self.perp_first is None) != (self.perp_last is None):
            raise ValueError("a day has both its first and last perp snapshot, or neither")
        if (self.spot_outcome is None) != (self.spot_valuation is None):
            raise ValueError("a decided spot bar carries its valuation, an undecided one none")
        if self.refused > self.applied:
            raise ValueError("the gate refuses applied cycles only")

    @property
    def boundary(self) -> date:
        return from_epoch_ms(self.boundary_ms).date()

    @property
    def perp_equity(self) -> Decimal | None:
        return None if self.perp_last is None else self.perp_last.equity

    @property
    def _perp_position(self) -> PerpPosition | None:
        return None if self.perp_last is None else self.perp_last.position

    @property
    def perp_size(self) -> Decimal:
        """The signed position at the day's end: negative short, zero flat or without a snapshot."""
        return _ZERO if self._perp_position is None else self._perp_position.size

    @property
    def perp_mark(self) -> Decimal | None:
        return None if self._perp_position is None else self._perp_position.mark

    @property
    def spot_equity(self) -> Decimal | None:
        return None if self.spot_valuation is None else self.spot_valuation.total

    @property
    def spot_qty(self) -> Decimal:
        """The token held at the day's bar; zero without a valuation."""
        if self.spot_valuation is None:
            return _ZERO
        return self.spot_valuation.balances.get(self.token, _ZERO)

    @property
    def spot_price(self) -> Decimal | None:
        return None if self.spot_valuation is None else self.spot_valuation.prices.get(self.token)

    @property
    def applied(self) -> int:
        return self.cycles.get(CycleKind.APPLIED, 0)

    @property
    def unusable(self) -> int:
        return self.cycles.get(CycleKind.UNUSABLE, 0)

    @property
    def intent_in(self) -> bool | None:
        """Whether the day's document said to be in: by the perp sidecar, else the spot target.

        ``None`` when neither leg recorded a document for the day.
        """
        if self.sidecars:
            return self.sidecars[-1].handoff.perp_side == "short"
        if self.spot_target is not None:
            return self.spot_target.weight > 0
        return None

    @property
    def delta(self) -> tuple[Decimal, Decimal | None]:
        """``(spot held + signed perp size, that as a share of the larger leg)``; no share with both legs empty."""
        larger = max(self.spot_qty, abs(self.perp_size))
        net = self.spot_qty + self.perp_size
        return net, (None if larger <= 0 else abs(net) / larger)

    @property
    def basis(self) -> Decimal | None:
        """The spot bar's close against the perp mark at the day's first cycle: ``spot / mark − 1``."""
        first = None if self.perp_first is None else self.perp_first.position
        mark = None if first is None else first.mark
        if self.spot_price is None or mark is None or mark <= 0:
            return None
        return self.spot_price / mark - 1

    @property
    def costs(self) -> Decimal:
        """The priced costs; an unpriced swap or gas counts as nothing here and is said in the lines."""
        return self.perp_fees + (self.spot_swap_cost or _ZERO) + (self.spot_gas_cost or _ZERO)

    @property
    def unpriced(self) -> bool:
        return self.spot_swap_cost is None or self.spot_gas_cost is None

    @property
    def net(self) -> Decimal:
        return self.perp_funding - self.costs

    @property
    def perp_held_by_d5(self) -> bool:
        return self.applied == 0 and self.unusable > 0

    @property
    def spot_held_by_d5(self) -> bool:
        """The strategy decided the bar with no target in the store; a skipped bar was not asked."""
        return self.spot_moved and self.spot_target is None

    @property
    def held_by_d5(self) -> bool:
        return self.perp_held_by_d5 or self.spot_held_by_d5

    @property
    def perp_read(self) -> bool:
        """Whether any perp cycle of the day applied a document or found one unusable.

        A ``pending`` cycle only saw tomorrow's document; a day of nothing
        but those (a handoff written late, for the next boundary only) is
        a day the leg acted on nothing.
        """
        return self.applied > 0 or self.unusable > 0

    @property
    def spot_moved(self) -> bool:
        """Whether the spot bar was decided by the strategy (a suspect bar skipped is not)."""
        return self.spot_outcome is not None and self.spot_outcome != _SPOT_SKIPPED

    @property
    def silent(self) -> bool:
        """Neither leg acted on anything: no perp cycle read a document, no spot bar was decided."""
        return not self.perp_read and not self.spot_moved

    @property
    def perp_digests(self) -> frozenset[str]:
        return frozenset(sidecar.digest for sidecar in self.sidecars)


def _group(items: Sequence[_T], key: Callable[[_T], int]) -> dict[int, list[_T]]:
    """``items`` by ``key``, in the order given."""
    grouped: dict[int, list[_T]] = {}
    for item in items:
        grouped.setdefault(key(item), []).append(item)
    return grouped


def _sum_by_day(items: Sequence[tuple[int, Decimal]]) -> dict[int, Decimal]:
    totals: dict[int, Decimal] = {}
    for at_ms, value in items:
        day = floor_day(at_ms)
        totals[day] = totals.get(day, _ZERO) + value
    return totals


def _priced_sum(costs: Sequence[Decimal | None]) -> Decimal | None:
    """The costs summed, or ``None`` when any of them is unpriced."""
    if any(cost is None for cost in costs):
        return None
    return sum((cost for cost in costs if cost is not None), _ZERO)


def _spot_value(
    amount: Decimal, token: str, valuation: SpotValuation, quote: str
) -> Decimal | None:
    if token == quote:
        return amount
    price = valuation.prices.get(token)
    return None if price is None else amount * price


def _swap_cost(fill: SpotFill, valuation: SpotValuation | None, quote: str) -> Decimal | None:
    """What went in less what came out at the bar's prices; ``None`` when a side is unpriced."""
    if valuation is None:
        return None
    value_in = _spot_value(fill.amount_in, fill.token_in, valuation, quote)
    value_out = _spot_value(fill.amount_out, fill.token_out, valuation, quote)
    return None if value_in is None or value_out is None else value_in - value_out


def _gas_cost(fill: SpotFill, valuation: SpotValuation | None) -> Decimal | None:
    price = None if valuation is None else valuation.prices.get(GAS_TOKEN)
    return None if price is None else fill.gas_eth * price


def build_rows(
    *,
    coin: str,
    quote: str,
    since_ms: int,
    until_ms: int,
    funding: Sequence[tuple[int, Decimal]],
    pending_funding: Sequence[int],
    fees: Sequence[tuple[int, Decimal]],
    snapshots: Sequence[PerpSnapshot],
    cycles: Sequence[PerpCycle],
    sidecars: Sequence[Sidecar],
    decisions: Sequence[tuple[int, str]],
    valuations: Sequence[SpotValuation],
    fills: Sequence[SpotFill],
    targets: Sequence[SpotTarget],
) -> list[BookDay]:
    """One row per UTC day from ``since_ms`` to ``until_ms`` inclusive (both day boundaries)."""
    if since_ms % MS_PER_DAY or until_ms % MS_PER_DAY:
        raise ValueError("the span's ends are UTC day boundaries")
    token = SPOT_TOKENS[coin]
    funding_by_day = _sum_by_day(funding)
    pending_by_day = Counter(floor_day(at_ms) for at_ms in pending_funding)
    fees_by_day = _sum_by_day(fees)
    snapshots_by_day = _group(snapshots, lambda snapshot: floor_day(snapshot.at_ms))
    cycles_by_day = _group(cycles, lambda cycle: floor_day(cycle.scheduled_ms))
    sidecars_by_day = _group(sidecars, lambda sidecar: floor_day(sidecar.handoff.as_of_ms))
    fills_at = _group(fills, lambda fill: fill.at_ms)
    outcome_at = dict(decisions)
    valuation_at = {valuation.at_ms: valuation for valuation in valuations}
    target_at = {target.at_ms: target for target in targets}

    rows = []
    for day in range(since_ms, until_ms + MS_PER_DAY, MS_PER_DAY):
        day_snapshots = snapshots_by_day.get(day, [])
        day_cycles = cycles_by_day.get(day, [])
        day_fills = fills_at.get(day, [])
        valuation = valuation_at.get(day)
        rows.append(
            BookDay(
                boundary_ms=day,
                token=token,
                perp_funding=funding_by_day.get(day, _ZERO),
                perp_funding_pending=pending_by_day.get(day, 0),
                perp_fees=fees_by_day.get(day, _ZERO),
                perp_first=day_snapshots[0] if day_snapshots else None,
                perp_last=day_snapshots[-1] if day_snapshots else None,
                cycles=Counter(cycle.kind for cycle in day_cycles),
                refused=sum(
                    1
                    for cycle in day_cycles
                    if cycle.kind is CycleKind.APPLIED and cycle.risk_action in _REFUSED
                ),
                sidecars=tuple(sidecars_by_day.get(day, [])),
                spot_outcome=outcome_at.get(day),
                spot_target=target_at.get(day),
                spot_valuation=valuation,
                spot_swap_cost=_priced_sum([_swap_cost(f, valuation, quote) for f in day_fills]),
                spot_gas_eth=sum((fill.gas_eth for fill in day_fills), _ZERO),
                spot_gas_cost=_priced_sum([_gas_cost(fill, valuation) for fill in day_fills]),
            )
        )
    return rows


class AlertKind(str, Enum):
    """The lights, as the lines spell them (module docstring)."""

    DELTA = "delta"
    ONE_LEGGED = "one-legged"
    DIFFERENT_DOCUMENTS = "different documents"
    NO_SIDECAR = "no sidecar"
    HELD_BY_D5 = "held by D5"
    SILENT_DAY = "silent day"
    SIDECAR_NOT_READ = "sidecar not read"


@dataclass(frozen=True)
class Alert:
    """One light, on one day."""

    boundary_ms: int
    kind: AlertKind
    text: str


def _short(digest: str) -> str:
    return digest[:12] + "…"


def alerts(rows: Sequence[BookDay], *, delta_alert_pct: int, d5_days: int) -> list[Alert]:
    """The lights the rows turn on, oldest first (module docstring)."""
    found: list[Alert] = []
    threshold = Decimal(delta_alert_pct) / 100
    streak = 0
    for row in rows:
        net, share = row.delta
        if share is not None and share > threshold:
            found.append(
                Alert(
                    row.boundary_ms,
                    AlertKind.DELTA,
                    f"net exposure {_qty(net)} of the coin ({pct(share)} of the larger leg: spot "
                    f"holds {_qty(row.spot_qty)}, perp holds {_qty(row.perp_size)}) "
                    f"exceeds {delta_alert_pct}% (D7)",
                )
            )
        spot_acted = row.spot_moved and row.spot_target is not None
        if spot_acted and row.applied == 0:
            found.append(
                Alert(
                    row.boundary_ms,
                    AlertKind.ONE_LEGGED,
                    "the spot bar decided on a target while no perp cycle applied the day's "
                    f"document ({_cycles(row.cycles)})",
                )
            )
        elif row.applied > 0 and not spot_acted:
            what = (
                "no decision"
                if row.spot_outcome is None
                else f"outcome {row.spot_outcome}"
                if row.spot_target is not None
                else "no target"
            )
            found.append(
                Alert(
                    row.boundary_ms,
                    AlertKind.ONE_LEGGED,
                    f"{row.applied} perp cycle(s) applied the day's document while the spot bar "
                    f"has {what}",
                )
            )
        digests = row.perp_digests
        if row.spot_target is not None and digests and row.spot_target.digest not in digests:
            found.append(
                Alert(
                    row.boundary_ms,
                    AlertKind.DIFFERENT_DOCUMENTS,
                    f"the spot target came from the handoff {_short(row.spot_target.digest)}, "
                    f"the perp sidecars recorded {', '.join(sorted(_short(d) for d in digests))}",
                )
            )
        if row.applied > 0 and not row.sidecars:
            found.append(
                Alert(
                    row.boundary_ms,
                    AlertKind.NO_SIDECAR,
                    f"{row.applied} perp cycle(s) applied a document but no sidecar for this "
                    f"day is under the payload directory",
                )
            )
        if row.silent:
            found.append(
                Alert(
                    row.boundary_ms,
                    AlertKind.SILENT_DAY,
                    f"neither leg acted: perp {_cycles(row.cycles)}, spot "
                    f"{row.spot_outcome or 'not decided'}",
                )
            )
        streak = streak + 1 if row.held_by_d5 else 0
        # Lit on the day the streak reaches the threshold and on every day it
        # goes on: the newest day's lines must show it while it lasts.
        if streak >= d5_days:
            legs = [
                leg
                for leg, held in (("perp", row.perp_held_by_d5), ("spot", row.spot_held_by_d5))
                if held
            ]
            found.append(
                Alert(
                    row.boundary_ms,
                    AlertKind.HELD_BY_D5,
                    f"{streak} days in a row a leg maintained for want of a usable document "
                    f"(today: {', '.join(legs)}); a human steps in (D5)",
                )
            )
    return found


@dataclass(frozen=True)
class WeekRow:
    """An ISO week's totals over the rows that fall in it.

    ``costs`` and ``net`` are the days' own summed (unpriced costs left
    out, counted in ``unpriced``); the three cost lines are shown apart,
    each ``None`` when any day of the week could not price it.
    """

    year: int
    week: int
    days: int
    days_in: int
    funding: Decimal
    perp_fees: Decimal
    spot_swap_cost: Decimal | None
    spot_gas_cost: Decimal | None
    costs: Decimal
    net: Decimal
    unpriced: int
    funding_pending: int
    alerts: int


def _iso_week(row: BookDay) -> tuple[int, int]:
    iso = row.boundary.isocalendar()
    return iso[0], iso[1]


def weeks(rows: Sequence[BookDay], found: Sequence[Alert]) -> list[WeekRow]:
    """The rows grouped by ISO week, oldest first."""
    alert_days = Counter(alert.boundary_ms for alert in found)
    out: list[WeekRow] = []
    for (year, week), group_rows in groupby(rows, key=_iso_week):
        group = list(group_rows)
        funding = sum((row.perp_funding for row in group), _ZERO)
        costs = sum((row.costs for row in group), _ZERO)
        out.append(
            WeekRow(
                year=year,
                week=week,
                days=len(group),
                days_in=sum(1 for row in group if row.intent_in),
                funding=funding,
                perp_fees=sum((row.perp_fees for row in group), _ZERO),
                spot_swap_cost=_priced_sum([row.spot_swap_cost for row in group]),
                spot_gas_cost=_priced_sum([row.spot_gas_cost for row in group]),
                costs=costs,
                net=funding - costs,
                unpriced=sum(1 for row in group if row.unpriced),
                funding_pending=sum(row.perp_funding_pending for row in group),
                alerts=sum(alert_days.get(row.boundary_ms, 0) for row in group),
            )
        )
    return out


# --- the operator's lines ----------------------------------------------------


def _money(value: Decimal | None) -> str:
    return "n/a" if value is None else f"{value:.4f}"


def _qty(value: Decimal) -> str:
    return f"{value:.4f}"


def _price(value: Decimal | None) -> str:
    return "n/a" if value is None else f"{value:.2f}"


def _signed_pct(value: Decimal | None) -> str:
    return "n/a" if value is None else f"{value * 100:+.2f}%"


def _cycles(cycles: Mapping[CycleKind, int]) -> str:
    total = sum(cycles.values())
    parts = [f"{kind.value} {count}" for kind, count in cycles.items() if count]
    return f"{total} cycle(s)" + (f": {', '.join(parts)}" if parts else "")


def _unpriced_note(count: int) -> str:
    return f" ({count} day(s) with a swap or gas not priced, left out)" if count else ""


_STANCE: Final = {True: "in ", False: "out", None: "?  "}


def _day_lines(row: BookDay, coin: str) -> list[str]:
    net, share = row.delta
    gas = (
        f"{_money(row.spot_gas_cost)} ({plain(row.spot_gas_eth)} ETH)" if row.spot_gas_eth else "0"
    )
    target = "none" if row.spot_target is None else plain(row.spot_target.weight)
    outcome = row.spot_outcome or "not decided"
    first_position = None if row.perp_first is None else row.perp_first.position
    first = (
        "n/a"
        if row.perp_first is None or first_position is None
        else f"{_price(first_position.mark)} at {iso_utc(row.perp_first.at_ms)[11:16]}Z"
    )
    pending = f" ({row.perp_funding_pending} pending)" if row.perp_funding_pending else ""
    refused = f", {row.refused} refused by the gate" if row.refused else ""
    return [
        (
            f"{row.boundary.isoformat()}  {_STANCE[row.intent_in]}  "
            f"funding {_money(row.perp_funding)}{pending}  perp fees {_money(row.perp_fees)}  "
            f"spot swaps {_money(row.spot_swap_cost)}  gas {gas}  net {_money(row.net)}"
        ),
        (
            f"    perp {_qty(row.perp_size)} {coin} @ {_price(row.perp_mark)}  "
            f"spot {_qty(row.spot_qty)} {row.token} @ {_price(row.spot_price)}  "
            f"delta {_qty(net)} ({pct(share)})  basis {_signed_pct(row.basis)} "
            f"(spot close vs perp mark {first})"
        ),
        (
            f"    perp {_cycles(row.cycles)}{refused}; spot {outcome}, target {target}; "
            f"equity perp {_money(row.perp_equity)} / spot {_money(row.spot_equity)}"
        ),
    ]


def format_report(
    rows: Sequence[BookDay],
    found: Sequence[Alert],
    problems: Sequence[SidecarProblem],
    *,
    coin: str,
    delta_alert_pct: int,
    d5_days: int,
) -> list[str]:
    """The report as the operator reads it: the span, each day with its lights, each week, the total."""
    token = SPOT_TOKENS[coin]
    if not rows:
        return [f"carry report: {coin}: no day in the span"]
    lines = [
        (
            f"carry report: {coin}/{token}, {len(rows)} day(s) from {rows[0].boundary.isoformat()} "
            f"to {rows[-1].boundary.isoformat()} (delta lights above {delta_alert_pct}%, "
            f"D5 lights at {d5_days} days)"
        )
    ]
    by_day = _group(found, lambda alert: alert.boundary_ms)
    for row in rows:
        lines.extend(_day_lines(row, coin))
        for alert in by_day.get(row.boundary_ms, []):
            lines.append(f"    ALERT {alert.kind.value}: {alert.text}")
    for problem in problems:
        lines.append(
            f"    ALERT {AlertKind.SIDECAR_NOT_READ.value}: {problem.path}: {problem.reason}"
        )
    lines.append("weeks (ISO):")
    for week in weeks(rows, found):
        pending = f" ({week.funding_pending} pending)" if week.funding_pending else ""
        lines.append(
            f"  {week.year}-W{week.week:02d}  {week.days} day(s), {week.days_in} in  "
            f"funding {_money(week.funding)}{pending}  costs {_money(week.costs)} (perp fees "
            f"{_money(week.perp_fees)}, spot swaps {_money(week.spot_swap_cost)}, gas "
            f"{_money(week.spot_gas_cost)}){_unpriced_note(week.unpriced)}  "
            f"net {_money(week.net)}  alerts {week.alerts}"
        )
    days_in = sum(1 for row in rows if row.intent_in)
    funding = sum((row.perp_funding for row in rows), _ZERO)
    costs = sum((row.costs for row in rows), _ZERO)
    unpriced = sum(1 for row in rows if row.unpriced)
    pending_count = sum(row.perp_funding_pending for row in rows)
    pending_note = (
        f" ({pending_count} settlement(s) still pending, not counted)" if pending_count else ""
    )
    lines.append(
        f"span: {days_in} of {len(rows)} day(s) in; funding {_money(funding)}{pending_note}, "
        f"costs {_money(costs)}{_unpriced_note(unpriced)}, net {_money(funding - costs)}; "
        f"{len(found) + len(problems)} alert(s), "
        f"{sum(1 for row in rows if row.held_by_d5)} day(s) held by D5"
    )
    return lines
