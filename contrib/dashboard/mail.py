"""Mail: one account over SMTP with STARTTLS, and the two letters the exporter writes.

The *transitions letter* goes out on a tick that has pending letter lines:
what was raised, what cleared, what event was seen — one line each, the
line's name, the state and when. The *digest* goes out once a UTC day, at
the configured hour: one line per line of the host (counts, states and
instants from its document), the host's numbers, the alerts that hold.
Neither letter carries an amount, a position or an address (plan §4); a
letter is built from the rules' sentences and :func:`line_summary` only,
and ``tests/test_mail.py`` holds that no number with a decimal point in a
document reaches a letter.

A send that fails raises :class:`MailError` with the error's class and its
text — the account's password replaced should a server echo it — and the
exporter keeps the letter pending for the next tick.
"""

from __future__ import annotations

import smtplib
import ssl
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from email.message import EmailMessage
from email.utils import format_datetime
from typing import Any

from .alerts import ActiveCondition, Pending
from .config import MailSettings
from .host import HostFacts
from .readers import LineRead
from .times import show_text, show_when, utc_day

__all__ = [
    "MailError",
    "SmtpFactory",
    "digest_letter",
    "line_summary",
    "send_mail",
    "smtp_connection",
    "transitions_letter",
]

SmtpFactory = Callable[[str, int, float], smtplib.SMTP]
_SEND_TIMEOUT = 30.0
_FOOTER = (
    "Only which line, what state and when are written here; the documents in the status "
    "directory on the host hold the rest."
)


class MailError(RuntimeError):
    """The letter was not sent."""


def smtp_connection(host: str, port: int, timeout: float) -> smtplib.SMTP:
    """The :data:`SmtpFactory` used outside tests."""
    return smtplib.SMTP(host, port, timeout=timeout)


def _letter(settings: MailSettings, subject: str, body: str, now: datetime) -> EmailMessage:
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = settings.sender
    message["To"] = ", ".join(settings.recipients)
    message["Date"] = format_datetime(now)
    message.set_content(body)
    return message


def send_mail(
    settings: MailSettings,
    *,
    subject: str,
    body: str,
    now: datetime,
    smtp_factory: SmtpFactory = smtp_connection,
    timeout: float = _SEND_TIMEOUT,
) -> int:
    """Send one plain-text letter through the account, STARTTLS first.

    Returns how many recipients the server refused (``0``: every one took
    it); a send refused whole raises :class:`MailError`, as does a header or a
    credential the mail library cannot encode (a ``ValueError`` from it).
    """
    try:
        message = _letter(settings, subject, body, now)
        with smtp_factory(settings.host, settings.port, timeout) as smtp:
            smtp.ehlo()
            smtp.starttls(context=ssl.create_default_context())
            smtp.ehlo()
            smtp.login(settings.user, settings.password)
            refused = smtp.send_message(message)
    except UnicodeEncodeError as exc:
        # The library spells out the character it could not encode: a
        # character of the account name or the password. Not repeated.
        raise MailError("UnicodeEncodeError: the account name or password is not ASCII") from exc
    except (smtplib.SMTPException, OSError, ValueError) as exc:
        text = str(exc).replace(settings.password, "***") if settings.password else str(exc)
        raise MailError(f"{type(exc).__name__}: {text}") from exc
    return len(refused)


# --- the letters -------------------------------------------------------------------


def _when(text: str | None) -> str:
    """An instant for a letter; ``none`` for a missing one, the text itself if it is not an instant."""
    if text is None:
        return "none"
    try:
        return show_text(text)
    except ValueError:
        return text


def transitions_letter(prefix: str, pending: Sequence[Pending]) -> tuple[str, str]:
    """``(subject, body)`` for the pending letter lines."""
    counts = [
        (kind, sum(1 for item in pending if item.kind == kind))
        for kind in ("raised", "cleared", "event")
    ]
    parts = [f"{n} {kind}" + ("(s)" if kind == "event" else "") for kind, n in counts if n]
    first = pending[0]
    subject = f"{prefix} {', '.join(parts)}: {first.line} {first.rule}"
    lines = ["State changes on the paper traders:", ""]
    for item in pending:
        lines.append(f"{item.kind.upper():<7} {_when(item.at)}  {item.line}: {item.text}")
    lines += ["", _FOOTER]
    return subject, "\n".join(lines) + "\n"


def _hyperliquid_tail(doc: Mapping[str, Any]) -> str:
    cycles = doc["cycles"]
    by_status = ", ".join(f"{status} {count}" for status, count in cycles["by_status"].items())
    sched = doc["scheduler"] or {}
    safe = doc["safe_mode"]
    return (
        f"cycles {cycles['total']} ({by_status}); last decision "
        f"{_when(sched.get('last_decision_at'))}; next {_when(sched.get('next_decision_at'))}; "
        f"safe mode {safe['type'] if safe else 'none'}"
    )


def _uniswap_tail(doc: Mapping[str, Any]) -> str:
    run = doc["run"]
    if run is None:
        return "no run in the document"
    outcomes = ", ".join(f"{outcome} {count}" for outcome, count in run["outcomes"].items())
    clock = run["clock"]
    return (
        f"decided {run['decided']} bar(s) ({outcomes}); clock "
        f"{clock['state'] if clock else 'no bar decided'}; last decided {_when(run['last_decided'])}"
    )


def _carry_tail(doc: Mapping[str, Any]) -> str:
    handoff = doc["handoff"]
    if handoff is not None:
        handoff_text = f"handoff {handoff['action']} for {_when(handoff['as_of'])}"
    elif doc["handoff_error"] is not None:
        handoff_text = "handoff unreadable"
    else:
        handoff_text = "handoff not read"
    span = doc["span"]
    return (
        f"{handoff_text}; {span['days']} day(s) {doc['since']} to {doc['until']}, "
        f"{span['alerts']} light(s)"
    )


_TAILS: dict[str, Callable[[Mapping[str, Any]], str]] = {
    "hyperliquid": _hyperliquid_tail,
    "uniswap": _uniswap_tail,
    "carry": _carry_tail,
}


def line_summary(read: LineRead, host: HostFacts) -> str:
    """One line of the digest for one line of the host: states, counts and instants only."""
    unit = host.unit(read.unit)
    unit_state = unit.active_state if unit is not None and unit.active_state else "unknown"
    head = f"{read.name} ({read.run_label}, {read.kind}): unit {unit_state}"
    if read.document is None:
        return f"{head}; not read ({read.failure})"
    tail = _TAILS[read.kind]
    try:
        return f"{head}; {tail(read.document)}"
    except (KeyError, TypeError, AttributeError) as exc:
        return f"{head}; not summarised ({type(exc).__name__})"


def _host_summary(host: HostFacts) -> list[str]:
    disk = f"disk {host.disk.used_pct:.0f}% used" if host.disk is not None else "disk not read"
    memory = (
        f"{host.memory.available_kb // 1024} MB of memory available"
        if host.memory is not None
        else "memory not read"
    )
    commits = ", ".join(f"{c.path} at {c.commit or '?'}" for c in host.checkouts)
    return [
        f"host {host.hostname}: {disk}; {memory}",
        f"units: {host.units_text()}",
        f"checkouts: {commits}",
    ]


def digest_letter(
    prefix: str,
    *,
    now: datetime,
    reads: Sequence[LineRead],
    host: HostFacts,
    active: Mapping[str, ActiveCondition],
    notes: Sequence[str],
) -> tuple[str, str]:
    """``(subject, body)`` for the daily digest."""
    day = utc_day(now)
    subject = f"{prefix} daily digest {day}"
    lines = [f"Daily digest for {day} (UTC), written {show_when(now)}.", ""]
    lines += [line_summary(read, host) for read in reads]
    lines.append("")
    lines += _host_summary(host)
    lines.append("")
    if active:
        lines.append(f"Alerts holding ({len(active)}):")
        for _key, item in sorted(active.items()):
            lines.append(f"  {item.line} {item.rule} since {_when(item.since)}: {item.text}")
    else:
        lines.append("Alerts holding: none.")
    for note in notes:
        lines.append(f"Note: {note}")
    lines += ["", _FOOTER]
    return subject, "\n".join(lines) + "\n"
