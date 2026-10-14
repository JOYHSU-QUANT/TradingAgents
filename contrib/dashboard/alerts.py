"""The alert rules: what a tick says about each line, the host and the exporter — and the state kept between ticks.

A *condition* is a state that holds or does not — the unit is down, the next
decision is overdue, the disk is full — keyed so the same state on two ticks
is the same condition: the operator is mailed when one appears (raised) and
when it goes (cleared), never while it merely holds (plan D2). An *event* is
a point — a kill-switch or safe-mode row the store wrote — mailed once, when
first seen; the newest instant seen per line is kept in the state so a
restart of the exporter does not mail history again (the first tick seeds
it silently). The pending letters are kept in the state too, so a mail that
could not be sent is sent on the next tick, not lost.

Every sentence a condition or an event carries names states, counts and
instants only: no amount, no position, no address (plan §4: the mail passes
through a third party). The details stay in the documents on the host.

The rules, per line kind (the thresholds are :class:`~.config.Thresholds`):

- **Hyperliquid** (``status --json``): the daemon's unit down (a daemon is
  judged by being ``active``) — unless the line's segment switch marker is
  present: a daemon parked between segments is not an alert, and the
  progress rules are skipped too, the tick notes it instead; while the unit
  is known to be active: the next decision overdue, the lease heartbeat
  stale, the export breadcrumb ``failed`` or stale; in any case: safe mode
  latched. Events: every kill-switch row but the periodic refresh, every
  safe-mode row.
- **Uniswap** (``status --json``): the clock ``behind`` past the visit
  window, ``ahead``, or no bar decided long after the run was created; the
  visit unit's last run ``failed`` (a oneshot is judged by its last run).
- **Carry** (``report --json``): the handoff unreadable, or deciding a day
  before today once the coordinator is due; ``held by D5`` on the last N
  days; the coordinator's unit ``failed``.
- **Host**: disk used past the threshold, memory available below it.
- **Exporter**: a line whose document could not be read (the command
  failed, timed out, printed no document, or one of another schema) — its
  other conditions are carried as they were, unjudged, until it is read
  again; a line whose document the rules could not read (a field missing
  or of the wrong shape: a package changed its document without bumping
  its version).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field, fields, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, TypeVar

from .config import CarryLine, ExporterConfig, HyperliquidLine, Line, Thresholds, UniswapLine
from .files import dump_json, write_private
from .host import HostFacts, UnitFacts
from .readers import LineRead
from .times import (
    clock_reached,
    hours_text,
    iso,
    minutes,
    parse_when,
    show_text,
    show_when,
    utc_day,
)

__all__ = [
    "HELD_BY_D5",
    "KILL_SWITCH_REFRESH",
    "ActiveCondition",
    "Condition",
    "Evaluation",
    "Event",
    "Pending",
    "State",
    "StateError",
    "advance",
    "evaluate",
    "fresh_state",
    "load_state",
    "save_state",
]

# The kill-switch row the daemon writes on every refresh: not news.
KILL_SWITCH_REFRESH = "kill_switch_refreshed"
# The carry report's light for a day both legs held because the handoff was unusable.
HELD_BY_D5 = "held by D5"
STATE_VERSION = 1
# How many events ``alerts.json`` keeps for the page.
RECENT_EVENTS = 50
# The conditions a line does not keep through a failed read: they are about the read.
_NOT_CARRIED = frozenset({"read_failed", "rules_failed"})


@dataclass(frozen=True)
class Condition:
    """A state that holds now. ``key`` is ``<line>:<rule>``, ``unit:<unit>:<rule>`` or ``host:<rule>``."""

    key: str
    line: str
    rule: str
    text: str


@dataclass(frozen=True)
class Event:
    """A row the store wrote, first seen this tick; ``at`` as the package spelled the instant."""

    key: str
    line: str
    kind: str
    at: str
    text: str


@dataclass(frozen=True)
class Evaluation:
    """What a tick found: the conditions that hold (sorted by key), the new events, the notes, the newest event instant seen per line."""

    conditions: tuple[Condition, ...]
    events: tuple[Event, ...]
    events_seen: dict[str, str]
    notes: tuple[str, ...]


@dataclass(frozen=True)
class _Outcome:
    conditions: list[Condition]
    events: list[Event] = field(default_factory=list)
    events_seen: str | None = None
    notes: list[str] = field(default_factory=list)


def _condition(line: str, rule: str, text: str) -> Condition:
    return Condition(f"{line}:{rule}", line, rule, text)


def _adder(name: str, found: list[Condition]) -> Callable[[str, str], None]:
    """``add(rule, text)`` appending ``name``'s condition to ``found``."""

    def add(rule: str, text: str) -> None:
        found.append(_condition(name, rule, text))

    return add


# --- the rules ------------------------------------------------------------------


def _progress(
    name: str,
    now: datetime,
    created: datetime,
    sched: Mapping[str, Any] | None,
    th: Thresholds,
) -> list[Condition]:
    """The Hyperliquid daemon-is-alive-but-not-progressing rules."""
    found: list[Condition] = []
    add = _adder(name, found)
    age = now - created
    overdue = timedelta(minutes=th.cycle_overdue_minutes)
    lease = timedelta(minutes=th.lease_stale_minutes)
    export = timedelta(hours=th.export_stale_hours)
    if sched is None:
        if age > overdue:
            add(
                "cycle_overdue",
                f"no scheduler row {minutes(age)} minutes after the run was created at "
                f"{show_when(created)}",
            )
        return found
    next_at = sched["next_decision_at"]
    if next_at is not None:
        late = now - parse_when(next_at)
        if late > overdue:
            add(
                "cycle_overdue",
                f"the next decision was due at {show_text(next_at)}, {minutes(late)} minutes ago",
            )
    heartbeat = sched["lease_heartbeat_at"]
    if heartbeat is None:
        if age > lease:
            add(
                "lease_stale",
                f"no lease heartbeat {minutes(age)} minutes after the run was created",
            )
    elif (old := now - parse_when(heartbeat)) > lease:
        add(
            "lease_stale",
            f"the lease heartbeat is {minutes(old)} minutes old (last at {show_text(heartbeat)})",
        )
    crumb = sched["export"]
    if crumb["status"] == "failed":
        when = f" at {show_text(crumb['at'])}" if crumb["at"] is not None else ""
        add("export_failed", f"the last export failed{when}")
    elif crumb["at"] is None:
        if age > export:
            add("export_stale", f"no export {hours_text(age)} hours after the run was created")
    elif (old := now - parse_when(crumb["at"])) > export:
        add(
            "export_stale",
            f"the last export was {hours_text(old)} hours ago (at {show_text(crumb['at'])})",
        )
    return found


def _new_events(
    name: str, doc: Mapping[str, Any], seen: str | None
) -> tuple[list[Event], str | None]:
    """The kill-switch and safe-mode rows newer than ``seen``, oldest first, and the newest instant now seen."""
    rows: list[tuple[datetime, str, str, bool, str | None]] = []
    for row in doc["kill_switch_events"]:
        kind = row["event_type"]
        rows.append(
            (
                parse_when(row["timestamp"]),
                row["timestamp"],
                kind,
                kind != KILL_SWITCH_REFRESH,
                None,
            )
        )
    for row in doc["safe_mode_events"]:
        rows.append(
            (
                parse_when(row["timestamp"]),
                row["timestamp"],
                row["event_type"],
                True,
                row["safe_mode_type"],
            )
        )
    if not rows:
        return [], seen
    newest = max(row[0] for row in rows)
    if seen is None:
        # First sight of the line: the tail is history, not news. The mark
        # keeps the precision of the row (the perp store writes microseconds),
        # so the row is never newer than its own mark.
        return [], newest.isoformat()
    since = parse_when(seen)
    fresh = sorted((row for row in rows if row[0] > since and row[3]), key=lambda row: row[0])
    events = [
        Event(
            key=f"{name}:event:{kind}@{stamp}",
            line=name,
            kind=kind,
            at=stamp,
            text=f"{kind}" + (f" ({mode})" if mode else "") + f" at {show_when(when)}",
        )
        for when, stamp, kind, _, mode in fresh
    ]
    return events, max(newest, since).isoformat()


def _hyperliquid(
    line: HyperliquidLine,
    doc: Mapping[str, Any],
    unit: UnitFacts | None,
    now: datetime,
    th: Thresholds,
    seen: str | None,
    parked: bool,
) -> _Outcome:
    conditions: list[Condition] = []
    notes: list[str] = []
    if parked:
        notes.append(
            f"{line.name}: the segment switch marker is present; the unit and progress rules "
            "are skipped"
        )
    # The progress rules presume a daemon that is running: only a unit known
    # to be active is judged by them (an unreadable unit is its own condition).
    elif unit is not None and unit.running:
        conditions += _progress(line.name, now, parse_when(doc["created_at"]), doc["scheduler"], th)
    safe = doc["safe_mode"]
    if safe is not None:
        text = f"safe mode {safe['type']} since {show_text(safe['entered_at'])}"
        conditions.append(_condition(line.name, "safe_mode", text))
    events, newest = _new_events(line.name, doc, seen)
    return _Outcome(conditions, events, newest, notes)


def _uniswap(line: UniswapLine, doc: Mapping[str, Any], now: datetime, th: Thresholds) -> _Outcome:
    found: list[Condition] = []
    add = _adder(line.name, found)
    run = doc["run"]
    if run is None:
        add("no_run", "the document carries no run")
        return _Outcome(found)
    clock = run["clock"]
    if clock is None:
        created = parse_when(run["created_at"])
        age = now - created
        if age > timedelta(hours=th.uniswap_no_decision_hours):
            add(
                "no_decision",
                f"no bar decided {hours_text(age)} hours after the run was created at "
                f"{show_when(created)}",
            )
    elif clock["state"] == "behind":
        late = now - parse_when(clock["latest_boundary"])
        if late > timedelta(hours=th.uniswap_behind_hours):
            add(
                "behind",
                f"{clock['undecided']} boundary(ies) undecided; the latest, "
                f"{show_text(clock['latest_boundary'])}, passed {hours_text(late)} hours ago",
            )
    elif clock["state"] == "ahead":
        add(
            "clock_ahead",
            f"the clock is before the last decided bar ({show_text(clock['last_decided'])})",
        )
    return _Outcome(found)


def _carry(line: CarryLine, doc: Mapping[str, Any], now: datetime, th: Thresholds) -> _Outcome:
    found: list[Condition] = []
    add = _adder(line.name, found)
    if doc["handoff_error"] is not None:
        add("handoff_unreadable", "the coordinator's handoff could not be read")
    elif doc["handoff"] is not None:
        as_of = utc_day(parse_when(doc["handoff"]["as_of"]))
        today = utc_day(now)
        if as_of < today and clock_reached(now, th.handoff_due_utc):
            add(
                "handoff_stale",
                f"the handoff decides {as_of}; today is {today} and the coordinator was due "
                f"by {th.handoff_due_utc:%H:%M}Z",
            )
    tail = doc["days"][-th.d5_days :]
    if len(tail) == th.d5_days and all(
        any(light["kind"] == HELD_BY_D5 for light in day["alerts"]) for day in tail
    ):
        add(
            "held_by_d5",
            f"held by D5 on the last {th.d5_days} day(s), {tail[0]['boundary']} to "
            f"{tail[-1]['boundary']}",
        )
    return _Outcome(found)


def _guarded(
    line: Line,
    doc: Mapping[str, Any],
    unit: UnitFacts | None,
    now: datetime,
    th: Thresholds,
    seen: str | None,
    parked: bool,
) -> _Outcome:
    """The line's rules, or ``rules_failed`` when the document is not shaped as this build reads it."""
    try:
        if isinstance(line, HyperliquidLine):
            return _hyperliquid(line, doc, unit, now, th, seen, parked)
        if isinstance(line, UniswapLine):
            return _uniswap(line, doc, now, th)
        return _carry(line, doc, now, th)
    except (KeyError, TypeError, ValueError, AttributeError, IndexError) as exc:
        text = (
            f"the rules could not read the document ({type(exc).__name__}: a field is missing "
            "or not shaped as this build reads it)"
        )
        return _Outcome([_condition(line.name, "rules_failed", text)])


def _unit_condition(unit: UnitFacts) -> Condition | None:
    key = f"unit:{unit.unit}"
    if unit.error is not None:
        return Condition(
            f"{key}:unreadable", unit.unit, "unreadable", f"systemctl could not show {unit.unit}"
        )
    if unit.load_state == "not-found":
        return Condition(
            f"{key}:not_found", unit.unit, "not_found", f"{unit.unit} is not installed"
        )
    if unit.type == "oneshot":
        if unit.active_state == "failed":
            return Condition(
                f"{key}:failed",
                unit.unit,
                "failed",
                f"{unit.unit}'s last run failed ({unit.result}, exit status "
                f"{unit.exec_main_status}) at {unit.exec_main_exit}",
            )
        return None
    if not unit.running:
        return Condition(
            f"{key}:down",
            unit.unit,
            "down",
            f"{unit.unit} is {unit.active_state} ({unit.sub_state}; result {unit.result}); "
            f"last started {unit.exec_main_start or 'never'}",
        )
    return None


def _host_conditions(host: HostFacts, th: Thresholds) -> list[Condition]:
    found: list[Condition] = []
    if host.disk is not None and host.disk.used_pct > th.disk_used_pct:
        found.append(
            Condition(
                "host:disk",
                "host",
                "disk",
                f"disk {host.disk.used_pct:.0f}% used on {host.disk.path}",
            )
        )
    if host.memory is not None and host.memory.available_kb // 1024 < th.memory_available_mb:
        found.append(
            Condition(
                "host:memory",
                "host",
                "memory",
                f"{host.memory.available_kb // 1024} MB of memory available",
            )
        )
    return found


def _carried(name: str, held: Mapping[str, ActiveCondition]) -> list[Condition]:
    """The conditions ``name`` held last tick, carried unjudged while its document cannot be read."""
    return [
        Condition(key, item.line, item.rule, item.text)
        for key, item in held.items()
        if item.line == name and item.rule not in _NOT_CARRIED
    ]


def evaluate(
    *,
    now: datetime,
    config: ExporterConfig,
    reads: Sequence[LineRead],
    host: HostFacts,
    events_seen: Mapping[str, str],
    held: Mapping[str, ActiveCondition] | None = None,
) -> Evaluation:
    """Every rule over this tick's reads and host facts.

    ``held`` is the conditions the last tick left holding: a line whose
    document could not be read this tick keeps its own (a read that fails
    once must not clear a safe mode that still holds, nor raise it again
    with a new ``since`` when the read recovers); ``read_failed`` is added.
    """
    held = held or {}
    # A parked daemon is a fact of the config and the file system, judged
    # before any read: it silences the unit whether or not the read went.
    parked = {
        line.unit for line in config.lines if isinstance(line, HyperliquidLine) and line.parked()
    }
    conditions: list[Condition] = []
    events: list[Event] = []
    notes: list[str] = []
    seen = dict(events_seen)
    for read in reads:
        line = read.line
        if read.document is None:
            text = f"the document could not be read: {read.failure}"
            conditions.append(_condition(line.name, "read_failed", text))
            conditions += _carried(line.name, held)
            continue
        outcome = _guarded(
            line,
            read.document,
            host.unit(line.unit),
            now,
            config.thresholds,
            events_seen.get(line.name),
            line.unit in parked,
        )
        conditions += outcome.conditions
        events += outcome.events
        notes += outcome.notes
        if outcome.events_seen is not None:
            seen[line.name] = outcome.events_seen
    for unit in host.units:
        if unit.unit in parked:
            continue
        found = _unit_condition(unit)
        if found is not None:
            conditions.append(found)
    conditions += _host_conditions(host, config.thresholds)
    return Evaluation(
        conditions=tuple(sorted(conditions, key=lambda c: c.key)),
        events=tuple(events),
        events_seen=seen,
        notes=tuple(notes),
    )


# --- the state between ticks --------------------------------------------------------


class StateError(RuntimeError):
    """The state file is not one this build reads."""


@dataclass(frozen=True)
class ActiveCondition:
    line: str
    rule: str
    text: str
    since: str


@dataclass(frozen=True)
class Pending:
    """A letter line not yet mailed: ``kind`` is ``raised``, ``cleared`` or ``event``; ``rule`` what it is about."""

    kind: str
    key: str
    line: str
    rule: str
    text: str
    at: str


@dataclass(frozen=True)
class State:
    """What ``alerts-state.json`` holds."""

    active: dict[str, ActiveCondition]
    events_seen: dict[str, str]
    pending: tuple[Pending, ...]
    recent_events: tuple[Event, ...]
    last_digest_day: str | None


def fresh_state() -> State:
    return State({}, {}, (), (), None)


_Record = TypeVar("_Record", ActiveCondition, Pending, Event)


def _str(value: object, what: str) -> str:
    if not isinstance(value, str):
        raise StateError(f"{what} must be text")
    return value


def _record(cls: type[_Record], item: Mapping[str, Any], what: str) -> _Record:
    """``cls`` (all of whose fields are text) from its saved form."""
    return cls(*(_str(item[spec.name], f"{what}.{spec.name}") for spec in fields(cls)))


def load_state(path: Path) -> State:
    """The state at ``path``, or a fresh one when there is no file yet; ``StateError`` for a file this build cannot read."""
    if not path.exists():
        return fresh_state()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or raw.get("version") != STATE_VERSION:
            raise StateError(f"version is not {STATE_VERSION}")
        day = raw["last_digest_day"]
        if day is not None and not isinstance(day, str):
            raise StateError("last_digest_day must be text")
        return State(
            {
                _str(k, "active key"): _record(ActiveCondition, v, "active")
                for k, v in raw["active"].items()
            },
            {
                _str(k, "events_seen key"): _str(v, "events_seen value")
                for k, v in raw["events_seen"].items()
            },
            tuple(_record(Pending, p, "pending") for p in raw["pending"]),
            tuple(_record(Event, e, "event") for e in raw["recent_events"]),
            day,
        )
    except (OSError, ValueError, KeyError, TypeError, AttributeError, StateError) as exc:
        why = exc if isinstance(exc, StateError) else type(exc).__name__
        raise StateError(
            f"{path} is not a state file this build reads ({why}); move it aside to start afresh "
            "(the alerts that hold are raised again, the event tails are seeded, nothing else is "
            "lost)"
        ) from exc


def save_state(path: Path, state: State) -> None:
    document = {
        "version": STATE_VERSION,
        "active": {key: asdict(item) for key, item in sorted(state.active.items())},
        "events_seen": dict(sorted(state.events_seen.items())),
        "pending": [asdict(item) for item in state.pending],
        "recent_events": [asdict(item) for item in state.recent_events],
        "last_digest_day": state.last_digest_day,
    }
    write_private(path, dump_json(document))


def advance(state: State, evaluation: Evaluation, now: datetime) -> State:
    """The state after this tick: raised and cleared conditions and the new events queued as pending letters."""
    at = iso(now)
    active: dict[str, ActiveCondition] = {}
    pending = list(state.pending)
    for condition in evaluation.conditions:
        held = state.active.get(condition.key)
        if held is None:
            pending.append(
                Pending("raised", condition.key, condition.line, condition.rule, condition.text, at)
            )
            active[condition.key] = ActiveCondition(
                condition.line, condition.rule, condition.text, at
            )
        else:
            active[condition.key] = replace(held, text=condition.text)
    for key, held in state.active.items():
        if key not in active:
            pending.append(Pending("cleared", key, held.line, held.rule, held.text, at))
    for event in evaluation.events:
        pending.append(
            Pending("event", event.key, event.line, f"event:{event.kind}", event.text, at)
        )
    recent = (state.recent_events + evaluation.events)[-RECENT_EVENTS:]
    return State(
        active, dict(evaluation.events_seen), tuple(pending), recent, state.last_digest_day
    )
