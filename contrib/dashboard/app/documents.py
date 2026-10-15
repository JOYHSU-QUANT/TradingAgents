"""The status directory as the page shows it: the exporter's documents read into view models.

Pure: this module reads the JSON files the exporter wrote and builds frozen
dataclasses of strings for the templates to print. It imports no web
library — and nothing of the exporter's that pulls PyYAML in, which the
page's venv does not ship — so the whole model is tested where the exporter
is (the trader's venv has no FastAPI); :mod:`.web` only renders what it
returns.

What the page knows comes from ``exporter.json``: its ``lines[]`` is the
index of the line documents (``file`` names each), and a reader goes by it,
never by globbing the directory (decided 2026-10-14: a line taken out of
the YAML leaves its old file behind, harmlessly). ``host.json`` gives the
units' states, ``alerts.json`` the conditions holding; a line's card shows
the conditions about it (its own and its unit's: the exporter names a unit
condition's ``line`` after the unit).

Every card carries the instant its document was generated and is marked
**stale** when that is older than the threshold the page was started with;
a card is **bad** when a condition holds about it, when its line could not
be read, or when its document is not shaped as this build reads it — a
document of another shape never takes the page down, it takes its card to
a sentence naming the exception's class (never its text, which could quote
the document); :func:`_safe` holds that for every card alike. Every string
shown passes :func:`contrib.dashboard.text.mask_hex` and is cut: the
packages mask their own free text, and the page masks it again.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from functools import partial
from pathlib import Path
from typing import Any, Final, Literal
from urllib.parse import quote

from ..text import cut, mask_hex
from ..times import minutes, parse_when, show_when

__all__ = [
    "ALERTS_FILE",
    "BAD",
    "EXPORTER_FILE",
    "HOST_FILE",
    "OK",
    "TEXT_LIMIT",
    "WARN",
    "Card",
    "Loaded",
    "Page",
    "Row",
    "Sparkline",
    "Table",
    "ago",
    "load_line",
    "load_page",
    "read_document",
    "sparkline",
]

EXPORTER_FILE = "exporter.json"
HOST_FILE = "host.json"
ALERTS_FILE = "alerts.json"
# The longest a free text runs on the page (a decision's reason, an error).
TEXT_LIMIT = 300
# How many recent events the overview lists.
RECENT_EVENTS_SHOWN = 10
# The tones a card or a row takes; the stylesheet colours them. A card is
# OK or BAD; a row may be WARN too, or plain.
OK: Final = "ok"
WARN: Final = "warn"
BAD: Final = "bad"
CardTone = Literal["ok", "bad"]
RowTone = Literal["", "warn", "bad"]
# The sparkline's box, in the SVG's own units (the stylesheet scales it);
# the template prints the model's ``viewbox`` so the two cannot disagree.
_SPARK_WIDTH = 240
_SPARK_HEIGHT = 48
_SPARK_PAD = 2.0
_NONE = "—"
_log = logging.getLogger(__name__)

# --- the view models ---------------------------------------------------------------


@dataclass(frozen=True)
class Row:
    """One ``label: value`` line of a card; ``tone`` colours it when set."""

    label: str
    value: str
    tone: RowTone = ""


@dataclass(frozen=True)
class Table:
    """A titled table; every cell is text already masked and cut."""

    title: str
    headers: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]

    def __post_init__(self) -> None:
        if any(len(row) != len(self.headers) for row in self.rows):
            raise ValueError(f"every row of {self.title!r} has one cell per header")


@dataclass(frozen=True)
class Sparkline:
    """A polyline of the last values; ``viewbox`` is the box ``points`` are in."""

    points: str
    label: str
    first: str
    last: str
    count: int
    viewbox: str = field(init=False, default=f"0 0 {_SPARK_WIDTH} {_SPARK_HEIGHT}")


@dataclass(frozen=True)
class Card:
    """One card of the page: a line, the host, the alerts, the exporter itself."""

    name: str
    title: str
    kind: str
    tone: CardTone
    stale: bool
    status: str
    generated_at: str | None
    rows: tuple[Row, ...] = ()
    sparkline: Sparkline | None = None
    tables: tuple[Table, ...] = ()
    link: str | None = None


@dataclass(frozen=True)
class Page:
    """The overview."""

    now: str
    banner: str | None
    exporter: Card
    alerts: Card
    lines: tuple[Card, ...]
    host: Card


@dataclass(frozen=True)
class Loaded:
    """One JSON file of the status directory: its document, or why there is none."""

    file: str
    document: Mapping[str, Any] | None
    error: str | None

    def __post_init__(self) -> None:
        if (self.document is None) != (self.error is not None):
            raise ValueError("a loaded file has its document, or the reason it has none")


# --- reading -----------------------------------------------------------------------


def read_document(path: Path) -> Loaded:
    """The JSON object at ``path``; ``error`` says why there is none (never the path)."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return Loaded(path.name, None, "not written yet")
    except OSError as exc:
        return Loaded(path.name, None, f"not readable ({type(exc).__name__})")
    except ValueError:  # UnicodeDecodeError is one; so is a null byte in the name
        return Loaded(path.name, None, "not UTF-8")
    try:
        document = json.loads(text)
    except ValueError:
        return Loaded(path.name, None, "not JSON")
    if not isinstance(document, Mapping):
        return Loaded(path.name, None, "not a JSON object")
    return Loaded(path.name, document, None)


def _is_bare_json_name(file: object) -> bool:
    """Whether ``file`` is a bare ``<name>.json``, the only kind of name the index may give."""
    return (
        isinstance(file, str)
        and file.endswith(".json")
        and len(file) > len(".json")
        and file == Path(file).name
    )


# --- text --------------------------------------------------------------------------


def _s(value: object) -> str:
    """Any document value as page text: masked, cut; ``None`` a dash, a bool yes or no."""
    if value is None:
        return _NONE
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return cut(mask_hex(value), TEXT_LIMIT) or _NONE
    if isinstance(value, Mapping):
        return ", ".join(f"{_s(key)} {_s(item)}" for key, item in value.items()) or _NONE
    if isinstance(value, Sequence):
        return ", ".join(_s(item) for item in value) or _NONE
    return cut(mask_hex(str(value)), TEXT_LIMIT)


def _mapping(value: object) -> Mapping[str, Any]:
    """``value`` as a mapping; an empty one for ``None`` (a document's optional block)."""
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return value
    raise TypeError(f"a block of the document is a {type(value).__name__}")


def _items(value: object) -> list[Mapping[str, Any]]:
    """``value`` as a list of mappings; empty for ``None``."""
    if value is None:
        return []
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise TypeError(f"a list of the document is a {type(value).__name__}")
    return [_mapping(item) for item in value]


def _texts(value: object) -> list[str]:
    """``value`` as a list of texts (a document's notes); empty for ``None``."""
    if value is None:
        return []
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise TypeError(f"a list of the document is a {type(value).__name__}")
    return [_s(item) for item in value]


def _decimal(value: object) -> Decimal | None:
    """The finite amount a document value spells, or ``None``."""
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return number if number.is_finite() else None


def _span_text(seconds: float) -> str:
    if seconds < 60:
        return "under a minute"
    whole_minutes = int(seconds // 60)
    if whole_minutes < 60:
        return f"{whole_minutes} min"
    hours = whole_minutes // 60
    if hours < 48:
        return f"{hours} h {whole_minutes % 60} min"
    return f"{hours // 24} d"


def ago(span: timedelta) -> str:
    """``3 min ago``, ``in 2 h 10 min``, ``under a minute ago``."""
    seconds = span.total_seconds()
    if seconds < 0:
        return f"in {_span_text(-seconds)}"
    return f"{_span_text(seconds)} ago"


def _instant(value: object) -> datetime | None:
    """The instant a document spells, or ``None`` when it is not one."""
    if not isinstance(value, str):
        return None
    try:
        return parse_when(value)
    except (ValueError, OverflowError):
        return None


def _shown(moment: datetime, now: datetime) -> str:
    return f"{show_when(moment)} ({ago(now - moment)})"


def _when(value: object, now: datetime) -> str:
    """An instant as ``2026-10-14 05:00Z (3 min ago)``; any other value as text."""
    moment = _instant(value)
    return _s(value) if moment is None else _shown(moment, now)


def sparkline(amounts: Sequence[object], label: str) -> Sparkline | None:
    """A polyline of ``amounts``, oldest first; ``None`` with fewer than two that are numbers."""
    numbers: list[float] = []
    shown: list[object] = []
    for amount in amounts:
        number = _decimal(amount)
        if number is not None:
            numbers.append(float(number))
            shown.append(amount)
    if len(numbers) < 2:
        return None
    low, high = min(numbers), max(numbers)
    rise = (high - low) or 1.0
    step = (_SPARK_WIDTH - 2 * _SPARK_PAD) / (len(numbers) - 1)
    inner = _SPARK_HEIGHT - 2 * _SPARK_PAD
    points = " ".join(
        f"{_SPARK_PAD + i * step:.1f},{_SPARK_PAD + inner * (1 - (n - low) / rise):.1f}"
        for i, n in enumerate(numbers)
    )
    return Sparkline(points, label, _s(shown[0]), _s(shown[-1]), len(numbers))


# --- the conditions, from alerts.json -----------------------------------------------


@dataclass(frozen=True)
class _Active:
    key: str
    line: str
    rule: str
    text: str
    since: str


def _active(alerts: Mapping[str, Any] | None) -> list[_Active]:
    return [
        _Active(
            _s(item.get("key")),
            _s(item.get("line")),
            _s(item.get("rule")),
            _s(item.get("text")),
            _s(item.get("since")),
        )
        for item in _items((alerts or {}).get("active"))
    ]


def _about(active: Sequence[_Active], line: str, unit: str | None) -> list[_Active]:
    """The conditions about a line: its own and its unit's (a unit condition's ``line`` is the unit)."""
    return [
        item for item in active if item.line == line or (unit is not None and item.line == unit)
    ]


def _condition_rows(found: Sequence[_Active], now: datetime) -> list[Row]:
    return [
        Row("alert", f"{item.rule} since {_when(item.since, now)}: {item.text}", BAD)
        for item in found
    ]


def _status_sentence(found: Sequence[_Active], quiet: str) -> str:
    if not found:
        return quiet
    keys = ", ".join(item.key for item in found)
    return f"{len(found)} condition(s) hold: {keys}"


# --- the freshness and the cards every document shares -------------------------------


def _freshness(generated_at: object, now: datetime, stale: timedelta) -> tuple[bool, str | None]:
    """``(is stale, shown)``: a document with no instant is stale too."""
    moment = _instant(generated_at)
    if moment is None:
        return True, None if generated_at is None else _s(generated_at)
    return now - moment > stale, _shown(moment, now)


def _unread_card(name: str, status: str, rows: Sequence[Row] = ()) -> Card:
    """The card of a document there is nothing to show of: no instant, stale, bad."""
    return Card(
        name=name,
        title=name,
        kind=name,
        tone=BAD,
        stale=True,
        status=status,
        generated_at=None,
        rows=tuple(rows),
    )


# What a document of another shape raises while a card is built.
_SHAPE_ERRORS = (
    KeyError,
    TypeError,
    ValueError,
    AttributeError,
    IndexError,
    OverflowError,
    InvalidOperation,
)


def _safe(name: str, file: str, build: Callable[[], Card]) -> Card:
    """``build()``, or the card that says the document is not shaped as this build reads it."""
    try:
        return build()
    except _SHAPE_ERRORS as exc:
        return _unread_card(
            name, f"{file} not shaped as this build reads it ({type(exc).__name__})"
        )


# --- the host ----------------------------------------------------------------------


def _unit_text(unit: Mapping[str, Any] | None, now: datetime) -> tuple[str, RowTone]:
    """``(text, tone)`` of a unit as ``host.json`` shows it; ``None`` when the host did not list it.

    ``failed`` is systemd's ``ActiveState`` (the alert rule keys on it too);
    ``Result`` says why (``exit-code``, ``timeout``...) and is shown as such.
    """
    if unit is None:
        return "not in host.json", WARN
    if unit.get("error"):
        return f"not shown ({_s(unit.get('error'))})", BAD
    if unit.get("load_state") == "not-found":
        return "not installed", BAD
    active = _s(unit.get("active_state"))
    sub = _s(unit.get("sub_state"))
    text = f"{active} ({sub})"
    tone: RowTone
    if unit.get("type") == "oneshot":
        text += f", last run {_s(unit.get('result'))}"
        tone = BAD if active == "failed" else ""
        since = unit.get("exec_main_exit")
    else:
        tone = "" if active == "active" else BAD
        since = unit.get("exec_main_start")
    if since:
        text += f", since {_s(since)}"
    restarts = unit.get("n_restarts")
    if isinstance(restarts, int) and restarts:
        text += f", {restarts} restart(s)"
    return text, tone


def _unit_of(units: Sequence[Mapping[str, Any]], name: str | None) -> Mapping[str, Any] | None:
    if name is None:
        return None
    return next((unit for unit in units if unit.get("unit") == name), None)


def _host_card(loaded: Loaded, active: Sequence[_Active], now: datetime, stale: timedelta) -> Card:
    found = _about(active, "host", None)
    if loaded.document is None:
        return _unread_card("host", f"{loaded.file} {loaded.error}", _condition_rows(found, now))
    doc = loaded.document
    is_stale, shown = _freshness(doc.get("generated_at"), now, stale)
    rows = _condition_rows(found, now)
    disk = _mapping(doc.get("disk"))
    if disk:
        used = disk.get("used_pct")
        pct = f"{used:.0f}%" if isinstance(used, (int, float)) else _s(used)
        free = disk.get("free_bytes")
        free_text = f", {free / 2**30:.1f} GiB free" if isinstance(free, (int, float)) else ""
        rows.append(Row("disk", f"{pct} used on {_s(disk.get('path'))}{free_text}"))
    else:
        rows.append(Row("disk", "not read", WARN))
    memory = _mapping(doc.get("memory"))
    if memory:
        available = memory.get("available_kb")
        total = memory.get("total_kb")
        if isinstance(available, int) and isinstance(total, int):
            rows.append(Row("memory", f"{available // 1024} MB available of {total // 1024} MB"))
        else:
            rows.append(Row("memory", _s(memory)))
    else:
        rows.append(Row("memory", "not read", WARN))
    load = doc.get("load_average")
    rows.append(Row("load", _s(load)) if load else Row("load", "not read", WARN))
    units = _items(doc.get("units"))
    unit_rows = tuple(
        (
            _s(unit.get("unit")),
            _unit_text(unit, now)[0],
            _s(unit.get("type")),
            _s(unit.get("load_state")),
        )
        for unit in units
    )
    checkouts = tuple(
        (_s(item.get("path")), _s(item.get("commit")) if item.get("commit") else "not a checkout")
        for item in _items(doc.get("checkouts"))
    )
    journal_rows = tuple(
        (_s(unit), _s(count) if count is not None else "not readable")
        for unit, count in _mapping(doc.get("journal_errors_24h")).items()
    )
    return Card(
        name="host",
        title=f"host {_s(doc.get('hostname'))}",
        kind="host",
        tone=BAD if found else OK,
        stale=is_stale,
        status=_status_sentence(found, "quiet"),
        generated_at=shown,
        rows=tuple(rows),
        tables=(
            Table("units", ("unit", "state", "type", "loaded"), unit_rows),
            Table("checkouts", ("path", "commit"), checkouts),
            Table("journal errors, last 24 h", ("unit", "lines"), journal_rows),
        ),
    )


# --- the alerts and the exporter ----------------------------------------------------


def _alerts_card(
    loaded: Loaded, active: Sequence[_Active], now: datetime, stale: timedelta
) -> Card:
    if loaded.document is None:
        return _unread_card("alerts", f"{loaded.file} {loaded.error}")
    doc = loaded.document
    is_stale, shown = _freshness(doc.get("generated_at"), now, stale)
    rows = [Row("note", note, WARN) for note in _texts(doc.get("notes"))]
    pending = doc.get("pending_letters")
    if isinstance(pending, int) and pending:
        # The exporter pings /fail while a letter is pending: the mail path is down.
        rows.append(Row("letters pending", str(pending), BAD))
    holding = tuple((item.key, _when(item.since, now), item.text) for item in active)
    # alerts.json keeps its tail oldest first; the page shows the newest, newest first.
    events = _items(doc.get("recent_events"))[-RECENT_EVENTS_SHOWN:][::-1]
    recent = tuple(
        (
            _when(event.get("at"), now),
            _s(event.get("line")),
            _s(event.get("kind")),
            _s(event.get("text")),
        )
        for event in events
    )
    red = [row.label for row in rows if row.tone == BAD]
    status = _status_sentence(active, "nothing holds")
    if red:
        status += f"; {', '.join(red)}"
    return Card(
        name="alerts",
        title="alerts",
        kind="alerts",
        tone=BAD if active or red else OK,
        stale=is_stale,
        status=status,
        generated_at=shown,
        rows=tuple(rows),
        tables=(
            Table("conditions holding", ("key", "since", "what"), holding),
            Table("recent events (newest first)", ("at", "line", "kind", "what"), recent),
        ),
    )


def _exporter_card(loaded: Loaded, now: datetime, stale: timedelta) -> Card:
    if loaded.document is None:
        return _unread_card("exporter", f"{loaded.file} {loaded.error}")
    doc = loaded.document
    is_stale, shown = _freshness(doc.get("generated_at"), now, stale)
    lines = _items(doc.get("lines"))
    read = sum(1 for line in lines if line.get("ok") is True)
    rows = [Row("lines read", f"{read} of {len(lines)}", "" if read == len(lines) else BAD)]
    duration = doc.get("duration_ms")
    rows.append(Row("tick took", f"{duration} ms" if isinstance(duration, int) else _s(duration)))
    mail = _mapping(doc.get("mail"))
    if mail.get("enabled") is True:
        text = f"sent {_s(mail.get('sent'))}, pending {_s(mail.get('pending'))}"
        if mail.get("digest_sent") is True:
            text += ", digest sent this tick"
        error = mail.get("error")
        rows.append(Row("mail", text + (f"; {_s(error)}" if error else ""), BAD if error else ""))
    else:
        rows.append(Row("mail", "off", WARN))
    deadman = _mapping(doc.get("deadman"))
    if deadman.get("enabled") is True:
        ping = _s(deadman.get("ping"))
        error = deadman.get("error")
        tone: RowTone = BAD if error or ping != "ok" else ""
        rows.append(Row("dead-man ping", ping + (f"; {_s(error)}" if error else ""), tone))
    else:
        rows.append(Row("dead-man ping", "off", WARN))
    red = [row.label for row in rows if row.tone == BAD]
    if shown is None:
        status = f"{EXPORTER_FILE} carries no generated_at this build reads"
    elif is_stale:
        status = "the last tick is older than the page's threshold"
    elif red:
        status = f"ticking, with a failure: {', '.join(red)}"
    else:
        status = "ticking"
    return Card(
        name="exporter",
        title="exporter",
        kind="exporter",
        tone=BAD if is_stale or red else OK,
        stale=is_stale,
        status=status,
        generated_at=shown,
        rows=tuple(rows),
    )


# --- the lines ---------------------------------------------------------------------

_Built = tuple[list[Row], Sparkline | None, list[Table]]
_Builder = Callable[[Mapping[str, Any], datetime, bool], _Built]


def _breadcrumb(value: object, now: datetime) -> tuple[str, RowTone]:
    crumb = _mapping(value)
    status = crumb.get("status")
    if status is None:
        return "never", ""
    text = f"{_s(status)} {_when(crumb.get('at'), now)}"
    if crumb.get("error"):
        text += f": {_s(crumb.get('error'))}"
    return text, BAD if status == "failed" else ""


def _verdict_text(verdict: Mapping[str, Any]) -> str:
    side = verdict.get("target_side")
    text = _s(verdict.get("decision_mode"))
    if side:
        text += f" {_s(side)} {_s(verdict.get('approved_target_margin_pct'))}%"
    text += f", {_s(verdict.get('risk_action'))}"
    if verdict.get("risk_reason"):
        text += f" ({_s(verdict.get('risk_reason'))})"
    if verdict.get("confidence") is not None:
        text += f", confidence {_s(verdict.get('confidence'))}"
    if verdict.get("order_created") is False and verdict.get("no_order_reason"):
        text += f", no order: {_s(verdict.get('no_order_reason'))}"
    return text


def _cycle_status(cycle: Mapping[str, Any]) -> str:
    text = _s(cycle.get("status"))
    if cycle.get("error_type"):
        text += f": {_s(cycle.get('error_type'))} {_s(cycle.get('error_message'))}"
    return text


def _cycle_row(cycle: Mapping[str, Any], now: datetime) -> tuple[str, ...]:
    verdict = _mapping(cycle.get("verdict"))
    return (
        _when(cycle.get("scheduled_at"), now),
        _cycle_status(cycle),
        _s(cycle.get("attempt_count")),
        _verdict_text(verdict) if verdict else _NONE,
        _s(verdict.get("decision_reason")),
    )


def _hyperliquid(doc: Mapping[str, Any], now: datetime, detail: bool) -> _Built:
    rows: list[Row] = [
        Row(
            "run",
            f"{_s(doc.get('mode'))}, since {_when(doc.get('created_at'), now)}, "
            f"opened with {_s(doc.get('initial_balance_usdc'))} USDC",
        )
    ]
    safe = _mapping(doc.get("safe_mode"))
    if safe:
        rows.append(
            Row(
                "safe mode",
                f"{_s(safe.get('type'))} since {_when(safe.get('entered_at'), now)}: "
                f"{_s(safe.get('reason'))}",
                BAD,
            )
        )
    scheduler = _mapping(doc.get("scheduler"))
    if scheduler:
        rows.append(Row("last decision", _when(scheduler.get("last_decision_at"), now)))
        rows.append(Row("next decision", _when(scheduler.get("next_decision_at"), now)))
        if scheduler.get("in_progress_attempt_id"):
            rows.append(
                Row("in progress", f"attempt {_s(scheduler.get('in_progress_attempt_id'))}", WARN)
            )
        rows.append(Row("lease heartbeat", _when(scheduler.get("lease_heartbeat_at"), now)))
        for name in ("export", "replay", "config_drift"):
            text, tone = _breadcrumb(scheduler.get(name), now)
            rows.append(Row(name.replace("_", " "), text, tone))
    else:
        rows.append(Row("scheduler", "no row yet", WARN))
    cycles = _mapping(doc.get("cycles"))
    by_status = _mapping(cycles.get("by_status"))
    total = _s(cycles.get("total"))
    rows.append(Row("cycles", f"{total} ({_s(by_status)})" if by_status else total))
    recent = _items(cycles.get("recent"))
    if recent:
        last = recent[0]
        rows.append(
            Row(
                "last cycle",
                f"{_cycle_status(last)} {_when(last.get('timestamp'), now)}",
                "" if last.get("status") in ("completed", "in_progress") else BAD,
            )
        )
        verdict = _mapping(last.get("verdict"))
        if verdict:
            rows.append(Row("last verdict", _verdict_text(verdict)))
    account = _mapping(doc.get("account"))
    snapshot = _mapping(account.get("snapshot"))
    if snapshot:
        rows.append(
            Row(
                "equity",
                f"{_s(snapshot.get('account_equity'))} ({_s(snapshot.get('available_balance'))} "
                f"available) at {_when(snapshot.get('timestamp'), now)}",
            )
        )
        rows.append(
            Row(
                "pnl",
                f"unrealised {_s(snapshot.get('unrealized_pnl'))}, "
                f"total {_s(snapshot.get('total_pnl'))}",
            )
        )
        rows.append(
            Row(
                "exposure",
                f"notional {_s(snapshot.get('total_position_notional'))}, "
                f"leverage {_s(snapshot.get('effective_leverage'))}, "
                f"margin ratio {_s(snapshot.get('margin_ratio'))}",
            )
        )
    else:
        rows.append(Row("equity", "no snapshot yet", WARN))
    ledger = _mapping(account.get("ledger"))
    if ledger:
        rows.append(
            Row(
                "ledger",
                f"wallet {_s(ledger.get('wallet_balance'))}, "
                f"realised {_s(ledger.get('realized_pnl'))}, "
                f"fees {_s(ledger.get('total_fees'))}, "
                f"funding {_s(ledger.get('net_funding_pnl'))}",
            )
        )
    # The store keeps a closed position's row at size 0: that is flat too.
    positions = [p for p in _items(doc.get("positions")) if _decimal(p.get("size")) != 0]
    for position in positions:
        rows.append(
            Row(
                f"position {_s(position.get('symbol'))}",
                f"{_s(position.get('size'))} @ {_s(position.get('entry_price'))}, "
                f"stop {_s(position.get('stop_loss_price'))}, "
                f"take profit {_s(position.get('take_profit_price'))}, "
                f"liquidation {_s(position.get('exchange_liquidation_price'))}",
            )
        )
    if not positions:
        rows.append(Row("position", "flat"))
    models = _items(doc.get("models"))
    if models:
        rows.append(
            Row(
                "models",
                "; ".join(
                    f"{_s(m.get('model'))} {_s(m.get('prompt_version'))}: "
                    f"{_s(m.get('prompts'))} prompt(s), last {_when(m.get('last_at'), now)}"
                    for m in models
                ),
            )
        )
    snapshots = _items(account.get("recent_snapshots"))
    # The perp document's tails are newest first; a curve reads oldest first.
    spark = sparkline(
        [s.get("account_equity") for s in reversed(snapshots)], "equity, recent snapshots"
    )
    if not detail:
        return rows, spark, []
    tables = [
        Table(
            "recent cycles (newest first)",
            ("scheduled", "status", "attempts", "verdict", "reason"),
            tuple(_cycle_row(c, now) for c in recent),
        ),
        Table(
            "recent fills (newest first)",
            ("at", "side", "qty", "price", "fee", "realised", "liquidity", "reason"),
            tuple(
                (
                    _when(f.get("timestamp"), now),
                    f"{_s(f.get('side'))} {_s(f.get('symbol'))}",
                    _s(f.get("fill_qty")),
                    _s(f.get("fill_price")),
                    _s(f.get("fee")),
                    _s(f.get("realized_pnl_delta")),
                    _s(f.get("liquidity_type")),
                    _s(f.get("fill_reason")),
                )
                for f in _items(doc.get("recent_fills"))
            ),
        ),
        Table(
            "recent account snapshots (newest first)",
            ("at", "equity", "available", "unrealised", "total pnl", "notional"),
            tuple(
                (
                    _when(s.get("timestamp"), now),
                    _s(s.get("account_equity")),
                    _s(s.get("available_balance")),
                    _s(s.get("unrealized_pnl")),
                    _s(s.get("total_pnl")),
                    _s(s.get("total_position_notional")),
                )
                for s in snapshots
            ),
        ),
        Table(
            "kill-switch events (newest first)",
            ("at", "event", "detail"),
            tuple(
                (
                    _when(e.get("timestamp"), now),
                    _s(e.get("event_type")),
                    _s(e.get("detail"))
                    + (f"; {_s(e.get('error_message'))}" if e.get("error_message") else ""),
                )
                for e in _items(doc.get("kill_switch_events"))
            ),
        ),
        Table(
            "safe-mode events (newest first)",
            ("at", "event", "type", "reason", "released by", "detail"),
            tuple(
                (
                    _when(e.get("timestamp"), now),
                    _s(e.get("event_type")),
                    _s(e.get("safe_mode_type")),
                    _s(e.get("reason")),
                    _s(e.get("released_by")),
                    _s(e.get("detail")),
                )
                for e in _items(doc.get("safe_mode_events"))
            ),
        ),
    ]
    return rows, spark, tables


def _curve_text(curve: Mapping[str, Any]) -> str:
    return (
        f"return {_s(curve.get('total_return'))}, max drawdown {_s(curve.get('max_drawdown'))} "
        f"({_s(curve.get('start'))} to {_s(curve.get('end'))})"
    )


_CURVES = (
    ("strategy", "strategy"),
    ("opening_held", "vs opening held"),
    ("all_quote", "vs all quote"),
)


def _uniswap(doc: Mapping[str, Any], now: datetime, detail: bool) -> _Built:
    run = _mapping(doc.get("run"))
    if not run:
        return [Row("run", "the document carries no run", BAD)], None, []
    rows: list[Row] = [
        Row(
            "run",
            f"{_s(run.get('mode'))} {_s(run.get('strategy'))}, fills {_s(run.get('fills'))}, "
            f"in {_s(run.get('quote'))}, since {_when(run.get('created_at'), now)}",
        )
    ]
    clock = _mapping(run.get("clock"))
    if clock:
        state = _s(clock.get("state"))
        text = (
            f"{state}: last decided {_when(clock.get('last_decided'), now)}, "
            f"latest boundary {_when(clock.get('latest_boundary'), now)}"
        )
        undecided = clock.get("undecided")
        if isinstance(undecided, int) and undecided:
            text += f", {undecided} bar(s) undecided"
        rows.append(Row("clock", text, "" if state == "up_to_date" else WARN))
    else:
        rows.append(Row("clock", "no bar decided yet", WARN))
    outcomes = _mapping(run.get("outcomes"))
    unsettled = run.get("unsettled")
    rows.append(
        Row(
            "decided",
            f"{_s(run.get('decided'))} bar(s)"
            + (f" ({_s(outcomes)})" if outcomes else "")
            + (f", {_s(unsettled)} unsettled" if unsettled else ""),
        )
    )
    if run.get("value") is not None:
        rows.append(Row("value", f"{_s(run.get('value'))} at {_when(run.get('holdings_at'), now)}"))
    else:
        rows.append(Row("value", "not valued yet"))
    holdings = _mapping(run.get("holdings"))
    if holdings:
        rows.append(
            Row(
                "holdings",
                f"{_s(holdings.get('balances'))}; gas {_s(holdings.get('gas_eth'))} ETH",
            )
        )
    metrics = _mapping(run.get("metrics"))
    if metrics:
        for key, label in _CURVES:
            rows.append(Row(label, _curve_text(_mapping(metrics.get(key)))))
        costs = _mapping(metrics.get("costs"))
        rows.append(
            Row(
                "activity",
                f"{_s(metrics.get('rebalances'))} rebalance(s), {_s(metrics.get('swaps'))} "
                f"swap(s), turnover {_s(metrics.get('turnover'))}, costs {_s(costs.get('total'))} "
                f"over {_s(metrics.get('bars'))} bar(s)",
            )
        )
    elif run.get("metrics_error"):
        rows.append(Row("metrics", _s(run.get("metrics_error")), WARN))
    seen = _mapping(run.get("verdicts_seen"))
    if seen:
        rows.append(
            Row(
                "verdicts seen",
                f"{_s(seen.get('source'))}: every {_s(seen.get('every'))}, "
                f"some {_s(seen.get('some'))}, none {_s(seen.get('none'))}",
            )
        )
    valuations = _items(run.get("recent_valuations"))
    spark = sparkline([v.get("total_value") for v in valuations], "value, recent bars")
    if not detail:
        return rows, spark, []
    reasons = _mapping(run.get("reason_codes"))
    store = _mapping(doc.get("store"))
    pools = "; ".join(
        f"{_s(p.get('pool'))}: {_s(p.get('readings'))} reading(s), "
        f"{_s(p.get('first'))} to {_s(p.get('last'))}"
        for p in _items(store.get("pools"))
    )
    bars = "; ".join(
        f"{_s(b.get('time'))} {'complete' if b.get('complete') else 'open'} {_s(b.get('prices'))}"
        + (f" ({_s(b.get('flags'))})" if b.get("flags") else "")
        for b in _items(store.get("latest_bars"))
    )
    coverage = "; ".join(
        f"{_s(c.get('what'))} from {_s(c.get('source'))}: {_s(c.get('complete'))} of {_s(c.get('of'))}"
        for c in _items(store.get("coverage"))
    )
    tables = [
        Table(
            "recent decisions (oldest first)",
            ("bar", "outcome", "decided", "delay", "reason code", "reason", "verdicts", "targets"),
            tuple(
                (
                    _when(d.get("time"), now),
                    _s(d.get("outcome")),
                    _when(d.get("decided_at"), now),
                    f"{_s(d.get('delay_seconds'))} s"
                    if d.get("delay_seconds") is not None
                    else _NONE,
                    _s(d.get("reason_code")),
                    _s(d.get("reason")),
                    _s(d.get("verdicts")),
                    _s(d.get("targets_now")),
                )
                for d in _items(run.get("recent_decisions"))
            ),
        ),
        Table(
            "reason codes",
            ("outcome", "code", "count"),
            tuple(
                (_s(outcome), _s(code), _s(count))
                for outcome, codes in reasons.items()
                for code, count in _mapping(codes).items()
            ),
        ),
        Table(
            "store",
            ("what", "value"),
            (
                ("chain", _s(store.get("chain_id"))),
                ("interval", f"{_s(store.get('interval_seconds'))} s"),
                ("quote", _s(store.get("quote"))),
                ("pools", pools or _NONE),
                ("latest bars", bars or _NONE),
                ("coverage", coverage or _NONE),
            ),
        ),
    ]
    return rows, spark, tables


def _lights(day: Mapping[str, Any]) -> str:
    return "; ".join(_s(light.get("kind")) for light in _items(day.get("alerts"))) or "none"


def _carry(doc: Mapping[str, Any], now: datetime, detail: bool) -> _Built:
    rows: list[Row] = [
        Row(
            "legs",
            f"perp {_s(doc.get('perp_run_id'))} ({_s(doc.get('coin'))}), "
            f"spot {_s(doc.get('spot_run_id'))} ({_s(doc.get('token'))})",
        )
    ]
    handoff = _mapping(doc.get("handoff"))
    if handoff:
        text = (
            f"{_s(handoff.get('action'))}: perp {_s(handoff.get('perp_side'))} "
            f"{_s(handoff.get('margin_pct'))}%, spot {_s(handoff.get('spot_token'))} weight "
            f"{_s(handoff.get('spot_weight'))}; for {_when(handoff.get('as_of'), now)}, "
            f"written {_when(handoff.get('written_at'), now)}"
        )
        if handoff.get("z") is not None:
            text += f"; z {_s(handoff.get('z'))}, funding {_s(handoff.get('funding_hourly'))}/h"
        rows.append(Row("handoff", text))
    else:
        rows.append(Row("handoff", f"unreadable: {_s(doc.get('handoff_error'))}", BAD))
    span = _mapping(doc.get("span"))
    rows.append(
        Row(
            "span",
            f"{_s(doc.get('since'))} to {_s(doc.get('until'))}: {_s(span.get('days'))} day(s), "
            f"{_s(span.get('days_in'))} in; net {_s(span.get('net'))} "
            f"(funding {_s(span.get('funding'))}, costs {_s(span.get('costs'))}, "
            f"{_s(span.get('unpriced'))} unpriced); {_s(span.get('alerts'))} light(s), "
            f"{_s(span.get('held_by_d5'))} day(s) held by D5",
            WARN if span.get("alerts") else "",
        )
    )
    days = _items(doc.get("days"))
    if days:
        last = days[-1]
        rows.append(
            Row(
                "newest day",
                f"{_s(last.get('boundary'))}: in {_s(last.get('intent_in'))}, "
                f"net {_s(last.get('net'))}, spot {_s(last.get('spot_outcome'))} "
                f"(target {_s(last.get('spot_target'))})",
            )
        )
        rows.append(
            Row(
                "book",
                f"perp {_s(last.get('perp_size'))} @ {_s(last.get('perp_mark'))} "
                f"(equity {_s(last.get('perp_equity'))}); spot {_s(last.get('spot_qty'))} @ "
                f"{_s(last.get('spot_price'))} (equity {_s(last.get('spot_equity'))})",
            )
        )
        rows.append(
            Row(
                "delta",
                f"{_s(last.get('delta'))} ({_s(last.get('delta_share'))} of the book), "
                f"basis {_s(last.get('basis'))}",
            )
        )
        rows.append(
            Row("cycles", f"{_s(_mapping(last.get('cycles')))}; {_s(last.get('refused'))} refused")
        )
        rows.append(Row("lights", _lights(last), BAD if last.get("alerts") else ""))
    else:
        rows.append(Row("days", "none in the span yet", WARN))
    problems = _items(doc.get("problems"))
    if problems:
        rows.append(Row("problems", f"{len(problems)} sidecar(s) unreadable", WARN))
    cumulative: list[Decimal] = []
    total = Decimal(0)
    for day in days:
        net = _decimal(day.get("net"))
        if net is not None:
            total += net
            cumulative.append(total)
    spark = sparkline(cumulative, "net, cumulative over the span")
    if not detail:
        return rows, spark, []
    tables = [
        Table(
            "days (oldest first)",
            (
                "day",
                "in",
                "funding",
                "fees",
                "swap cost",
                "gas cost",
                "net",
                "delta share",
                "spot",
                "lights",
            ),
            tuple(
                (
                    _s(d.get("boundary")),
                    _s(d.get("intent_in")),
                    _s(d.get("perp_funding"))
                    + (
                        f" (+{_s(d.get('perp_funding_pending'))} pending)"
                        if d.get("perp_funding_pending")
                        else ""
                    ),
                    _s(d.get("perp_fees")),
                    _s(d.get("spot_swap_cost")),
                    _s(d.get("spot_gas_cost")),
                    _s(d.get("net")) + (" (unpriced)" if d.get("unpriced") else ""),
                    _s(d.get("delta_share")),
                    _s(d.get("spot_outcome")),
                    _lights(d),
                )
                for d in days
            ),
        ),
        Table(
            "weeks",
            ("week", "days", "in", "funding", "fees", "costs", "net", "lights"),
            tuple(
                (
                    f"{_s(w.get('year'))}-W{_s(w.get('week'))}",
                    _s(w.get("days")),
                    _s(w.get("days_in")),
                    _s(w.get("funding")),
                    _s(w.get("perp_fees")),
                    _s(w.get("costs")),
                    _s(w.get("net"))
                    + (f" ({_s(w.get('unpriced'))} unpriced)" if w.get("unpriced") else ""),
                    _s(w.get("alerts")),
                )
                for w in _items(doc.get("weeks"))
            ),
        ),
        Table(
            "sidecar problems",
            ("path", "reason"),
            tuple((_s(p.get("path")), _s(p.get("reason"))) for p in problems),
        ),
    ]
    return rows, spark, tables


# The line kinds the exporter's config knows (``config._KINDS``; the test pins the two).
_BUILDERS: Mapping[str, _Builder] = {
    "hyperliquid": _hyperliquid,
    "uniswap": _uniswap,
    "carry": _carry,
}


def _line_card(
    entry: Mapping[str, Any],
    loaded: Loaded | None,
    units: Sequence[Mapping[str, Any]],
    active: Sequence[_Active],
    now: datetime,
    stale: timedelta,
    *,
    detail: bool,
) -> Card:
    name = _s(entry.get("name"))
    kind = _s(entry.get("kind"))
    unit = entry.get("unit") if isinstance(entry.get("unit"), str) else None
    found = _about(active, name, unit)
    if unit is None:
        rows = [Row("unit", "no unit")]
    else:
        unit_text, unit_tone = _unit_text(_unit_of(units, unit), now)
        rows = [Row("unit", f"{_s(unit)}: {unit_text}", unit_tone)]
    rows.extend(_condition_rows(found, now))
    # Why the card is bad, beyond the conditions: each sentence is one reason.
    sentences: list[str] = []
    if entry.get("ok") is not True:
        hint = entry.get("stderr_line")
        sentences.append(
            f"not read: {_s(entry.get('failure'))}" + (f" — {_s(hint)}" if hint else "")
        )
    if loaded is None:
        sentences.append("the index names no file for this line")
    elif loaded.document is None:
        sentences.append(f"{loaded.file} {loaded.error}")
    spark: Sparkline | None = None
    tables: tuple[Table, ...] = ()
    is_stale, shown = True, None
    if loaded is not None and loaded.document is not None:
        generated_at = loaded.document.get("generated_at")
        is_stale, shown = _freshness(generated_at, now, stale)
        if generated_at is None:
            sentences.append("the document carries no generated_at")
        elif _instant(generated_at) is None:
            sentences.append("the document's generated_at is not an instant")
        builder = _BUILDERS.get(kind)
        if builder is None:
            sentences.append(f"a line of kind {kind} is not one this build shows")
        else:
            try:
                built_rows, spark, built_tables = builder(loaded.document, now, detail)
            except _SHAPE_ERRORS as exc:
                sentences.append(
                    f"document not shaped as this build reads it ({type(exc).__name__})"
                )
            else:
                rows.extend(built_rows)
                tables = tuple(built_tables)
    # A red row without a condition behind it (a failed cycle, a carry light the
    # exporter has no rule for, a unit down while alerts.json is unreadable) still
    # reddens the card: the colour never says less than the rows do.
    red = [row.label for row in rows if row.tone == BAD and row.label != "alert"]
    if red and not found and not sentences:
        sentences.append(f"quiet by the rules, but {len(red)} row(s) red: {', '.join(red)}")
    if sentences and found:
        sentences.insert(0, _status_sentence(found, ""))
    return Card(
        name=name,
        title=f"{name} · {_s(entry.get('run'))}",
        kind=kind,
        tone=BAD if found or sentences else OK,
        stale=is_stale,
        status="; ".join(sentences) if sentences else _status_sentence(found, "quiet"),
        generated_at=shown,
        rows=tuple(rows),
        sparkline=spark,
        tables=tables,
        link=None if detail else f"/line/{quote(name, safe='')}",
    )


def _index(exporter: Mapping[str, Any] | None) -> list[Mapping[str, Any]]:
    """The ``lines[]`` of ``exporter.json`` whose ``name`` is text (the rest cannot be shown)."""
    if exporter is None:
        return []
    entries = _items(exporter.get("lines"))
    return [entry for entry in entries if isinstance(entry.get("name"), str) and entry["name"]]


def _load_line_file(status_dir: Path, entry: Mapping[str, Any]) -> Loaded | None:
    """The line's document by the index's ``file``; ``None`` when the index gives no usable name."""
    file = entry.get("file")
    if not _is_bare_json_name(file):
        return None
    return read_document(status_dir / str(file))


@dataclass(frozen=True)
class _Context:
    """What every card is judged against, read once per page."""

    exporter: Loaded
    host: Loaded
    alerts: Loaded
    entries: tuple[Mapping[str, Any], ...]
    units: tuple[Mapping[str, Any], ...]
    active: tuple[_Active, ...]


def _not_shaped(loaded: Loaded, exc: BaseException) -> Loaded:
    return Loaded(loaded.file, None, f"not shaped as this build reads it ({type(exc).__name__})")


# The last complaint logged per shared file, so a file that stays broken is
# one journal line, not one per reload.
_complained: dict[str, str] = {}


def _complain(loaded: Loaded) -> None:
    """One journal line when a shared document cannot be read or indexed (deduplicated per file)."""
    if loaded.error is None:
        _complained.pop(loaded.file, None)
    elif _complained.get(loaded.file) != loaded.error:
        _complained[loaded.file] = loaded.error
        _log.warning("%s %s", loaded.file, loaded.error)


def _context(status_dir: Path) -> _Context:
    """The three shared documents; one not shaped as this build reads it counts as unread."""
    exporter = read_document(status_dir / EXPORTER_FILE)
    host = read_document(status_dir / HOST_FILE)
    alerts = read_document(status_dir / ALERTS_FILE)
    try:
        entries = tuple(_index(exporter.document))
    except _SHAPE_ERRORS as exc:
        exporter, entries = _not_shaped(exporter, exc), ()
    try:
        units = tuple(_items(_mapping(host.document).get("units"))) if host.document else ()
    except _SHAPE_ERRORS as exc:
        host, units = _not_shaped(host, exc), ()
    try:
        active = tuple(_active(alerts.document))
    except _SHAPE_ERRORS as exc:
        alerts, active = _not_shaped(alerts, exc), ()
    for loaded in (exporter, host, alerts):
        _complain(loaded)
    return _Context(exporter, host, alerts, entries, units, active)


def _line_cards(
    context: _Context,
    status_dir: Path,
    now: datetime,
    stale: timedelta,
    *,
    detail: bool,
    only: str | None = None,
) -> tuple[Card, ...]:
    """The cards of the indexed lines (one, ``only``), each guarded like the shared cards."""

    def build(entry: Mapping[str, Any]) -> Card:
        return _line_card(
            entry,
            _load_line_file(status_dir, entry),
            context.units,
            context.active,
            now,
            stale,
            detail=detail,
        )

    return tuple(
        _safe(_s(entry["name"]), _s(entry.get("file")), partial(build, entry))
        for entry in context.entries
        if only is None or entry["name"] == only
    )


def load_page(status_dir: Path, *, now: datetime, stale: timedelta) -> Page:
    """The overview of ``status_dir`` at ``now``; a document older than ``stale`` marks its card."""
    context = _context(status_dir)
    exporter_card = _safe(
        "exporter", EXPORTER_FILE, lambda: _exporter_card(context.exporter, now, stale)
    )
    banner: str | None = None
    if context.exporter.document is None:
        banner = f"No exporter tick to show: exporter.json {context.exporter.error}."
    elif exporter_card.generated_at is None:
        banner = f"{exporter_card.status}: every card may be out of date."
    elif exporter_card.stale:
        banner = (
            f"The exporter's last tick was {exporter_card.generated_at}, "
            f"older than {minutes(stale)} min: every card may be out of date."
        )
    return Page(
        now=show_when(now),
        banner=banner,
        exporter=exporter_card,
        alerts=_safe(
            "alerts", ALERTS_FILE, lambda: _alerts_card(context.alerts, context.active, now, stale)
        ),
        lines=_line_cards(context, status_dir, now, stale, detail=False),
        host=_safe("host", HOST_FILE, lambda: _host_card(context.host, context.active, now, stale)),
    )


def load_line(status_dir: Path, name: str, *, now: datetime, stale: timedelta) -> Card | None:
    """One line's card with its tables; ``None`` when the index knows no line ``name``."""
    cards = _line_cards(_context(status_dir), status_dir, now, stale, detail=True, only=name)
    return cards[0] if cards else None
