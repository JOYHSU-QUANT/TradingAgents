"""``report --json``: the sleeve's report as one document.

The dashboard plan's reading model for this package. The report is already a
model — :func:`.report.build_rows` gives one :class:`~.report.BookDay` per
UTC day, :func:`.report.alerts` the lights, :func:`.report.weeks` the ISO
weeks, :func:`.report.span_totals` the span — and this module is its
document form: :func:`report_status` folds those into a :class:`ReportStatus`
(a frozen tree of dataclasses whose fields are what the report lines say),
and :func:`report_json` renders it field by field (``dataclasses.asdict`` is
the whitelist), ``Decimal`` as its exact text, days as ``2026-02-10``,
instants as this package spells them (``2026-02-10T00:20:00+00:00``: each
package's document keeps its own ISO-8601 UTC spelling, and a reader parses
them with ``datetime.fromisoformat``), ASCII throughout. The document opens
with ``schema_version`` and ``source``; the version is scoped to this
package's documents, and a change to a field's name, place or meaning bumps
:data:`SCHEMA_VERSION` (``tests/test_report.py`` pins the field paths).

The free text the document carries — a light's sentence, the reason a
sidecar could not be read — is masked for a ``0x``-prefixed 20- or 32-byte
hex and cut at :data:`TEXT_LIMIT`, as the other packages' documents are; a
sidecar problem names the file, not where it sits on the host.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from .handoff import SPOT_TOKENS, iso_utc
from .report import (
    Alert,
    BookDay,
    SidecarProblem,
    SpanTotals,
    WeekRow,
    alerts_by_day,
    span_totals,
    weeks,
)
from .stores import CycleKind

__all__ = [
    "SCHEMA_VERSION",
    "SOURCE",
    "TEXT_LIMIT",
    "AlertStatus",
    "DayStatus",
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


@dataclass(frozen=True)
class AlertStatus:
    """One light on a day: its kind (as the report spells it) and its sentence."""

    kind: str
    text: str


@dataclass(frozen=True)
class DayStatus:
    """One UTC day of the hedged book, as the report's three lines say it.

    ``intent_in`` is the day's stance (``True`` in, ``False`` out, ``None``
    unknown). ``perp_as_of`` / ``spot_as_of`` are set when the leg had no
    reading on the day and the last known one is carried, and name the
    instant it was read. ``cycles`` counts every cycle kind (``applied``,
    ``pending``, ``unusable``, ``no_decision``, ``engine``), zero when none.
    ``delta`` is the net exposure in the coin and ``delta_share`` its share
    of the larger leg (``None`` when neither leg holds anything).
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


@dataclass(frozen=True)
class ProblemStatus:
    """A sidecar the report could not read: its file name and why."""

    path: str
    reason: str


@dataclass(frozen=True)
class ReportStatus:
    """The report as a document: the days with their lights, the problems, the weeks, the span.

    ``weeks`` and ``span`` are the report's own :class:`~.report.WeekRow` and
    :class:`~.report.SpanTotals`, carried as they are.
    """

    schema_version: int
    source: str
    generated_at: str
    coin: str
    token: str
    delta_alert_pct: int
    d5_days: int
    days: tuple[DayStatus, ...]
    problems: tuple[ProblemStatus, ...]
    weeks: tuple[WeekRow, ...]
    span: SpanTotals


def _text(value: str) -> str:
    """Free text: masked for a quoted key or address, and cut at the limit."""
    text = _HEX_SECRET.sub(lambda found: f"{found.group()[:6]}...{found.group()[-4:]}", value)
    return text if len(text) <= TEXT_LIMIT else text[: TEXT_LIMIT - 3] + "..."


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


def report_status(
    rows: Sequence[BookDay],
    found: Sequence[Alert],
    problems: Sequence[SidecarProblem],
    *,
    coin: str,
    delta_alert_pct: int,
    d5_days: int,
    now: datetime,
) -> ReportStatus:
    """The report over ``rows`` as a document; ``now`` (aware) is its ``generated_at``."""
    if now.tzinfo is None:
        raise ValueError("the report clock must give an aware datetime")
    by_day = alerts_by_day(found)
    totals = span_totals(rows, found, problems)
    return ReportStatus(
        schema_version=SCHEMA_VERSION,
        source=SOURCE,
        generated_at=now.astimezone(timezone.utc).isoformat(),
        coin=coin,
        token=SPOT_TOKENS[coin],
        delta_alert_pct=delta_alert_pct,
        d5_days=d5_days,
        days=tuple(_day(row, by_day.get(row.boundary_ms, ())) for row in rows),
        problems=tuple(ProblemStatus(Path(p.path).name, _text(p.reason)) for p in problems),
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
