"""``report --json``: the sleeve's report as one document.

The dashboard plan's reading model for this package. The report is already a
model — :func:`.report.build_rows` gives one :class:`~.report.BookDay` per
UTC day, :func:`.report.alerts` the lights, :func:`.report.weeks` the ISO
weeks, :func:`.report.span_totals` the span — and this module is its
document form: :func:`report_status` folds those into a :class:`ReportStatus`
(a frozen tree of dataclasses whose fields are what the report lines say,
under an envelope that names the two runs it reconciles and the span it
covers, and, when asked, the coordinator's latest handoff), and
:func:`report_json` renders it field by field (``dataclasses.asdict`` is
the whitelist), ``Decimal`` as its exact text, days as ``2026-02-10``,
instants as this package spells them (``2026-02-10T00:20:00+00:00``, to the
second: each package's document keeps its own ISO-8601 UTC spelling, and a
reader parses them with ``datetime.fromisoformat``), ASCII throughout. The
document opens with ``schema_version`` and ``source``; the version is scoped
to this package's documents, and a change to a field's name, place or
meaning bumps :data:`SCHEMA_VERSION` (``tests/test_report.py`` pins the
field paths). ``days`` and ``weeks`` are oldest first, as the lines print
them; a reader takes ``[-1]`` as the latest.

The free text the document carries — a light's sentence, the reason a
sidecar or the handoff could not be read — is masked for a ``0x``-prefixed
20- or 32-byte hex and cut at :data:`TEXT_LIMIT`, as the other packages'
documents are; a sidecar problem and the handoff name their file, not where
it sits on the host. Of the handoff, the document copies out what the two
legs act on (the action, the perp side and margin, the spot token and
weight) and the reading it was decided from; not its ``params`` nor its
``equity`` block.

The model classes with a pairing to guard refuse it in ``__post_init__``
(a day unpriced with every cost priced, a handoff read and unreadable at
once), so a document built by hand cannot be half-filled.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from .handoff import SPOT_TOKENS, HandoffError, iso_utc, read_handoff
from .report import (
    Alert,
    AlertKind,
    BookDay,
    SidecarProblem,
    SpanTotals,
    WeekRow,
    alerts_by_day,
    span_totals,
    weeks,
)
from .signal import Action
from .stores import CycleKind

__all__ = [
    "SCHEMA_VERSION",
    "SOURCE",
    "TEXT_LIMIT",
    "AlertStatus",
    "DayStatus",
    "HandoffStatus",
    "ProblemStatus",
    "ReportStatus",
    "report_json",
    "report_status",
]

# The document's version, scoped to SOURCE.
SCHEMA_VERSION = 1
SOURCE = "carry"
# The longest a free-text value runs in the document.
TEXT_LIMIT = 1000
# A 32-byte hex (a key) or a 20-byte hex (an address) in free text, the longer
# shape first; folded to its first six and last four characters.
_HEX_SECRET = re.compile(r"0x(?:[0-9a-fA-F]{64}|[0-9a-fA-F]{40})")
_KINDS = frozenset(kind.value for kind in AlertKind)
_ACTIONS = frozenset(action.value for action in Action)
_SIDES = frozenset({"short", "flat"})
_CYCLE_KINDS = frozenset(kind.value for kind in CycleKind)


@dataclass(frozen=True)
class AlertStatus:
    """One light on a day: its kind and its sentence.

    ``kind`` is one of ``delta``, ``one-legged``, ``different documents``,
    ``no sidecar``, ``held by D5``, ``silent day``, ``sidecar not read``,
    as the report spells them.
    """

    kind: str
    text: str

    def __post_init__(self) -> None:
        if self.kind not in _KINDS:
            raise ValueError(f"kind must be one of {sorted(_KINDS)}, got {self.kind!r}")


@dataclass(frozen=True)
class DayStatus:
    """One UTC day of the hedged book, as the report's three lines say it.

    ``intent_in`` is the day's stance (``True`` in, ``False`` out, ``None``
    unknown). ``costs`` is the day's priced costs and ``net`` its funding
    less those; a swap or gas the day's valuation could not price is
    ``None``, left out of both and flagged by ``unpriced``. A leg's
    ``*_as_of`` is set when it had no reading on the day and the last known
    one is carried, and names the instant that was read; a leg never read
    has ``*_equity`` ``None`` (and a size or quantity of zero, which is
    also what flat reads). ``delta`` is the net exposure in the coin and
    ``delta_share`` its share of the larger leg as a fraction (``0.1`` is
    10%, against the document's ``delta_alert_pct`` in percent); ``None``
    when neither leg holds anything. ``perp_funding_pending`` counts the
    settlements not yet in ``perp_funding``. ``cycles`` counts every cycle
    kind (``applied``, ``pending``, ``unusable``, ``no_decision``,
    ``engine``), zero when none, and ``refused`` is how many of the applied
    ones the gate refused (a part of ``applied``, not another kind).
    ``spot_outcome`` is the spot bar's decision outcome as the spot store
    spells it, ``None`` for an undecided bar.
    """

    boundary: str
    intent_in: bool | None
    perp_funding: Decimal
    perp_funding_pending: int
    perp_fees: Decimal
    spot_swap_cost: Decimal | None
    spot_gas_eth: Decimal
    spot_gas_cost: Decimal | None
    costs: Decimal
    net: Decimal
    unpriced: bool
    perp_size: Decimal
    perp_mark: Decimal | None
    perp_as_of: str | None
    perp_equity: Decimal | None
    spot_qty: Decimal
    spot_price: Decimal | None
    spot_as_of: str | None
    spot_equity: Decimal | None
    delta: Decimal
    delta_share: Decimal | None
    basis: Decimal | None
    cycles: dict[str, int]
    refused: int
    spot_outcome: str | None
    spot_target: Decimal | None
    held_by_d5: bool
    alerts: tuple[AlertStatus, ...]

    def __post_init__(self) -> None:
        if self.unpriced != (self.spot_swap_cost is None or self.spot_gas_cost is None):
            raise ValueError(
                "a day is unpriced exactly when a swap or gas cost could not be priced"
            )
        if set(self.cycles) != _CYCLE_KINDS:
            raise ValueError(f"cycles count every kind, {sorted(_CYCLE_KINDS)}")
        if not 0 <= self.refused <= self.cycles[CycleKind.APPLIED.value]:
            raise ValueError("the gate refuses applied cycles only")
        for leg, as_of, equity in (
            ("perp", self.perp_as_of, self.perp_equity),
            ("spot", self.spot_as_of, self.spot_equity),
        ):
            if as_of is not None and equity is None:
                raise ValueError(f"a carried {leg} reading is a reading: its equity is known")


@dataclass(frozen=True)
class ProblemStatus:
    """A sidecar the report could not read: its file name and why."""

    path: str
    reason: str


@dataclass(frozen=True)
class HandoffStatus:
    """The coordinator's handoff as the document carries it: what the legs act on, and the reading.

    ``as_of`` is the boundary it decides and ``written_at`` when it was
    written. ``action`` is ``enter`` / ``hold`` / ``exit`` / ``stay_out``,
    ``perp_side`` ``short`` or ``flat`` with ``margin_pct`` (0 while out),
    and ``spot_weight`` the spot leg's target weight of ``spot_token``.
    The reading the handoff was decided from — ``read_at`` and the
    ``funding_hourly`` rate — is carried by every entry, and is ``None``
    for a handoff decided without one (a stay-out, or a hold while in);
    its ``z`` score (the handoff's float, as its shortest exact text) is
    ``None`` as well when the window could not give one (too few samples,
    or no deviation), which keeps the book where it is.
    """

    path: str
    as_of: str
    written_at: str
    action: str
    perp_side: str
    margin_pct: int
    spot_token: str
    spot_weight: Decimal
    read_at: str | None
    funding_hourly: Decimal | None
    z: Decimal | None

    def __post_init__(self) -> None:
        if self.action not in _ACTIONS:
            raise ValueError(f"action must be one of {sorted(_ACTIONS)}, got {self.action!r}")
        if self.perp_side not in _SIDES:
            raise ValueError(f"perp_side must be one of {sorted(_SIDES)}, got {self.perp_side!r}")
        if (self.read_at is None) != (self.funding_hourly is None):
            raise ValueError("a reading is carried whole, or not at all")
        if self.read_at is None and self.z is not None:
            raise ValueError("a z score comes from a reading")
        if self.action == Action.ENTER.value and self.read_at is None:
            raise ValueError("an entry carries the reading it was decided from")


@dataclass(frozen=True)
class ReportStatus:
    """The report as a document: the days with their lights, the problems, the weeks, the span.

    The envelope names the two runs (``perp_run_id``, ``spot_run_id``) and
    the span (``since`` to ``until``, UTC days, both ends in) the report
    covers; ``days`` holds one entry per day of it. ``handoff`` is the
    coordinator's latest handoff when the report was asked to read one and
    could, ``handoff_error`` why it could not, and both are ``None`` when it
    was not asked. ``weeks`` and ``span`` are the report's own
    :class:`~.report.WeekRow` and :class:`~.report.SpanTotals`, carried as
    they are; ``span.alerts`` counts the lights and the problems together,
    where a day's and a week's ``alerts`` count lights alone.
    """

    schema_version: int
    source: str
    generated_at: str
    coin: str
    token: str
    perp_run_id: str
    spot_run_id: str
    since: str
    until: str
    delta_alert_pct: int
    d5_days: int
    handoff: HandoffStatus | None
    handoff_error: str | None
    days: tuple[DayStatus, ...]
    problems: tuple[ProblemStatus, ...]
    weeks: tuple[WeekRow, ...]
    span: SpanTotals

    def __post_init__(self) -> None:
        if self.handoff is not None and self.handoff_error is not None:
            raise ValueError("the handoff was read, or could not be; not both")
        if self.span.days != len(self.days):
            raise ValueError("the span counts the days carried")
        if self.days and (self.days[0].boundary, self.days[-1].boundary) != (
            self.since,
            self.until,
        ):
            raise ValueError("the days run from since to until")
        if self.until < self.since:
            raise ValueError("the span ends on or after the day it starts")


def _text(value: str) -> str:
    """Free text: masked for a quoted key or address, and cut at the limit."""
    text = _HEX_SECRET.sub(lambda found: f"{found.group()[:6]}...{found.group()[-4:]}", value)
    return text if len(text) <= TEXT_LIMIT else text[: TEXT_LIMIT - 3] + "..."


def _named(reason: str, path: Path) -> str:
    """``reason`` with the file named, not placed: its host path folded to its name, then masked."""
    return _text(reason.replace(str(path), path.name))


def _day(row: BookDay, lights: Sequence[Alert]) -> DayStatus:
    net, share = row.delta
    perp_state, spot_state = row.perp_state, row.spot_state
    return DayStatus(
        boundary=row.boundary.isoformat(),
        intent_in=row.intent_in,
        perp_funding=row.perp_funding,
        perp_funding_pending=row.perp_funding_pending,
        perp_fees=row.perp_fees,
        spot_swap_cost=row.spot_swap_cost,
        spot_gas_eth=row.spot_gas_eth,
        spot_gas_cost=row.spot_gas_cost,
        costs=row.costs,
        net=row.net,
        unpriced=row.unpriced,
        perp_size=row.perp_size,
        perp_mark=row.perp_mark,
        perp_as_of=iso_utc(perp_state.at_ms) if row.perp_carried and perp_state else None,
        perp_equity=row.perp_equity,
        spot_qty=row.spot_qty,
        spot_price=row.spot_price,
        spot_as_of=iso_utc(spot_state.at_ms) if row.spot_carried and spot_state else None,
        spot_equity=row.spot_equity,
        delta=net,
        delta_share=share,
        basis=row.basis,
        cycles={kind.value: row.cycles.get(kind, 0) for kind in CycleKind},
        refused=row.refused,
        spot_outcome=row.spot_outcome,
        spot_target=None if row.spot_target is None else row.spot_target.weight,
        held_by_d5=row.held_by_d5,
        alerts=tuple(AlertStatus(alert.kind.value, _text(alert.text)) for alert in lights),
    )


def _handoff(path: Path, coin: str) -> tuple[HandoffStatus | None, str | None]:
    """``coin``'s handoff at ``path`` as the document carries it, or why it could not be read.

    Another coin's file is not read as this one's, as the coordinator's
    own memory refuses it. An instant too large to spell, an equity too
    large to weigh, or a file nested too deep to parse, is "could not be
    read" too, not a crash.
    """
    try:
        found = read_handoff(path)
        if found.coin != coin:
            raise HandoffError(f"{path} is {found.coin}'s handoff, not {coin}'s")
        reading = found.reading
        status = HandoffStatus(
            path=path.name,
            as_of=iso_utc(found.as_of_ms),
            written_at=iso_utc(found.written_at_ms),
            action=found.action.value,
            perp_side=found.perp_side,
            margin_pct=found.margin_pct,
            spot_token=found.spot_token,
            spot_weight=found.spot_weight,
            read_at=None if reading is None else iso_utc(reading.at_ms),
            funding_hourly=None if reading is None else reading.current,
            z=None if reading is None or reading.z is None else Decimal(repr(reading.z)),
        )
    except (HandoffError, ArithmeticError, RecursionError) as exc:
        return None, _named(str(exc), path)
    return status, None


def report_status(
    rows: Sequence[BookDay],
    found: Sequence[Alert],
    problems: Sequence[SidecarProblem],
    *,
    coin: str,
    perp_run_id: str,
    spot_run_id: str,
    since_ms: int,
    until_ms: int,
    delta_alert_pct: int,
    d5_days: int,
    handoff: Path | None,
    now: datetime,
) -> ReportStatus:
    """The report over ``rows`` (the days from ``since_ms`` to ``until_ms``) as a document.

    ``now`` (aware) is its ``generated_at``; ``handoff`` is the coordinator's
    handoff file to carry, or ``None`` to carry none.
    """
    if now.tzinfo is None:
        raise ValueError("the report clock must give an aware datetime")
    by_day = alerts_by_day(found)
    totals = span_totals(rows, found, problems)
    carried, error = (None, None) if handoff is None else _handoff(handoff, coin)
    return ReportStatus(
        schema_version=SCHEMA_VERSION,
        source=SOURCE,
        generated_at=now.astimezone(timezone.utc).isoformat(timespec="seconds"),
        coin=coin,
        token=SPOT_TOKENS[coin],
        perp_run_id=perp_run_id,
        spot_run_id=spot_run_id,
        since=iso_utc(since_ms)[:10],
        until=iso_utc(until_ms)[:10],
        delta_alert_pct=delta_alert_pct,
        d5_days=d5_days,
        handoff=carried,
        handoff_error=error,
        days=tuple(_day(row, by_day.get(row.boundary_ms, ())) for row in rows),
        problems=tuple(
            ProblemStatus(Path(p.path).name, _named(p.reason, Path(p.path))) for p in problems
        ),
        weeks=tuple(weeks(rows, found)),
        span=totals,
    )


def _json_default(value: object) -> str:
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(f"a report carries no {type(value).__name__} (got {value!r})")


def report_json(status: ReportStatus) -> str:
    """The report as an ASCII JSON document, field by field, ``Decimal`` as exact text."""
    return json.dumps(asdict(status), default=_json_default, indent=2)
