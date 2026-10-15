"""Instants as the three packages spell them, read and shown one way here.

The perp store spells ``2026-10-14T05:00:00.123456+00:00`` (microseconds when
its clock had them), carry ``2026-10-14T05:00:00+00:00``, Uniswap
``2026-10-14T05:00:00Z`` — which Python 3.10's ``fromisoformat`` does not
read, so the ``Z`` is spelled out first. Every instant this package makes
(``since``, ``at``) is written as ``isoformat`` to the second, UTC; the
event marks (``events_seen``) keep the precision of the row they mark, so a
row is never newer than its own mark; every instant shown to an operator is
``2026-10-14 05:00Z``.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone

from contrib.core.instants import parse_instant

__all__ = [
    "clock_reached",
    "hours_text",
    "iso",
    "minutes",
    "parse_when",
    "show_text",
    "show_when",
    "span_text",
    "utc_day",
    "utc_now",
]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_when(text: str) -> datetime:
    """An ISO-8601 instant from any of the documents, as an aware UTC datetime.

    The floor reads it (:func:`contrib.core.instants.parse_instant`, which
    refuses a naive one) once the ``Z`` is spelled out.
    """
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    return parse_instant(text).astimezone(timezone.utc)


def iso(moment: datetime) -> str:
    """The instant as this package writes it: UTC, to the second."""
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def show_when(moment: datetime) -> str:
    """The instant as an operator reads it in a mail: ``2026-10-14 05:00Z``."""
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%MZ")


def show_text(text: str) -> str:
    """:func:`show_when` of an instant given as a document spells it."""
    return show_when(parse_when(text))


def utc_day(moment: datetime) -> str:
    """The UTC calendar day of the instant, ``2026-10-14``."""
    return moment.astimezone(timezone.utc).date().isoformat()


def clock_reached(now: datetime, due: time) -> bool:
    """Whether the UTC clock at ``now`` is at or past ``due``."""
    return now.astimezone(timezone.utc).time() >= due


def minutes(span: timedelta) -> int:
    return int(span.total_seconds() // 60)


def hours_text(span: timedelta) -> str:
    """A span in hours with one decimal, for a sentence (``5.3``)."""
    return f"{span.total_seconds() / 3600:.1f}"


def span_text(span: timedelta) -> str:
    """A span as the page reads it: ``under a minute``, ``3 min``, ``5 h 10 min``, ``3 d``."""
    seconds = abs(span.total_seconds())
    if seconds < 60:
        return "under a minute"
    whole_minutes = int(seconds // 60)
    if whole_minutes < 60:
        return f"{whole_minutes} min"
    hours = whole_minutes // 60
    if hours < 48:
        return f"{hours} h {whole_minutes % 60} min"
    return f"{hours // 24} d"
