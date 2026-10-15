"""One tick of the exporter: read, judge, mail, write, ping.

In this order, so that nothing that can fail later loses what was found:

1. load the state (a corrupt state file refuses the tick before anything is read);
2. read every line's document (:mod:`.readers`) and the host (:mod:`.host`);
3. evaluate the rules (:mod:`.alerts`) and advance the state — the raised,
   cleared and event letter lines are pending now — and **save it**;
4. mail: the pending letter lines as one letter, and the digest when the
   day's hour has come and today's is not sent yet; a send that fails is
   logged and the letter stays pending; save the state again (with no mail
   account the letter lines are dropped: nothing will ever send them). The
   mail goes before the documents are written: of the files a tick writes,
   only the state file (step 3) stands between a finding and its letter;
5. write the documents: each line's as its package printed it
   (``<line>.json``, kept from the last good read when this one failed),
   ``host.json`` and ``alerts.json`` (the conditions holding, the notes,
   the recent events, the letters still pending);
6. carry the histories on (:mod:`.history`): each line's and the host's
   series under ``history/``, harvested from the documents just read; a
   history file this build cannot read is left as it is and named in
   ``exporter.json``;
7. ping the dead-man URL — plain when the tick is whole, ``/fail`` when a
   letter could not be sent (so the watchdog knows the mail path is down);
8. write ``exporter.json``: how each read went, how the mail and the ping
   went, which histories were not kept, how long the tick took.

Everything under the status directory is written by :mod:`.files` — atomic,
mode 0640 — for the dashboard account to read and nothing else. ``check``
(:func:`read_and_judge`) runs steps 1 to 3 without saving, writing or mailing.
"""

from __future__ import annotations

import http.client
import time
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .alerts import Evaluation, State, advance, evaluate, load_state, save_state
from .config import ExporterConfig, HistorySettings, MailSettings, Settings
from .files import dump_json, ensure_private_dir, write_private
from .history import (
    HISTORY_DIR,
    HOST_KIND,
    HarvestError,
    HistoryError,
    advance as advance_history,
    dump as dump_history,
    harvest,
    history_file,
    load_history,
)
from .host import HostFacts, read_host
from .mail import (
    MailError,
    SmtpFactory,
    digest_letter,
    send_mail,
    smtp_connection,
    transitions_letter,
)
from .readers import LineRead, Runner, read_line, run_command
from .times import clock_reached, iso, utc_day

__all__ = [
    "STATE_FILE",
    "HostReader",
    "TickReport",
    "failure_lines",
    "history_lines",
    "read_and_judge",
    "run_once",
    "summary_line",
]

STATE_FILE = "alerts-state.json"
_PING_TIMEOUT = 10.0
# The seams a test binds: how the host is read, how a URL is opened.
HostReader = Callable[..., HostFacts]
_Urlopen = Callable[..., Any]


@dataclass(frozen=True)
class TickReport:
    """What one tick did, for the command line to print."""

    generated_at: str
    reads: tuple[LineRead, ...]
    evaluation: Evaluation
    raised: tuple[str, ...]
    cleared: tuple[str, ...]
    mail_enabled: bool
    mailed: int
    digest_sent: bool
    mail_error: str | None
    pending: int
    ping: str | None
    ping_error: str | None
    written: tuple[str, ...]
    # The histories not kept this tick: ``(name, why)``; the host's under ``host``.
    history_errors: tuple[tuple[str, str], ...]
    duration_ms: int


def _alerts_document(now: datetime, state: State, evaluation: Evaluation) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "source": "dashboard",
        "generated_at": iso(now),
        "active": [{"key": key, **asdict(item)} for key, item in sorted(state.active.items())],
        "notes": list(evaluation.notes),
        "recent_events": [asdict(event) for event in state.recent_events],
        "pending_letters": len(state.pending),
    }


def _exporter_document(now: datetime, report: TickReport) -> dict[str, Any]:
    history_errors = dict(report.history_errors)
    return {
        "schema_version": 1,
        "source": "dashboard",
        "generated_at": iso(now),
        "duration_ms": report.duration_ms,
        "lines": [
            {
                "name": read.name,
                "kind": read.kind,
                "run": read.run_label,
                "unit": read.unit,
                "file": f"{read.name}.json",
                "ok": read.ok,
                "exit_code": read.exit_code,
                "failure": read.failure,
                "stderr_line": read.stderr_line,
                "duration_ms": read.duration_ms,
                "document_generated_at": (
                    read.document.get("generated_at") if read.document is not None else None
                ),
                "history_error": history_errors.get(read.name),
            }
            for read in report.reads
        ],
        "host_history_error": history_errors.get(HOST_KIND),
        "mail": {
            "enabled": report.mail_enabled,
            "sent": report.mailed,
            "digest_sent": report.digest_sent,
            "pending": report.pending,
            "error": report.mail_error,
        },
        "deadman": {
            "enabled": report.ping is not None,
            "ping": report.ping,
            "error": report.ping_error,
        },
    }


def _keep_history(
    status_dir: Path,
    name: str,
    *,
    kind: str,
    run: str,
    document: Mapping[str, Any],
    now: datetime,
    max_points: int,
    min_interval: timedelta,
) -> tuple[bool, str | None]:
    """Carry ``history/<name>.json`` on by ``document``: ``(written, why not kept)``.

    Not written when nothing changed (most ticks add no point): the file
    then still says when its series last changed. A file this build cannot
    read is left untouched (an operator moves it aside, as with the state
    file); a document the harvest cannot read leaves the history as it was;
    a file that cannot be written is reported.
    """
    path = status_dir / history_file(name)
    try:
        history = load_history(path)
    except HistoryError as exc:
        return False, f"{path.name} is not a history this build reads ({exc}); move it aside"
    try:
        harvested = harvest(kind, document)
    except HarvestError as exc:
        return False, f"the document is not shaped as this build harvests it ({exc})"
    advanced = advance_history(
        history,
        line=name,
        kind=kind,
        run=run,
        harvested=harvested,
        now=now,
        max_points=max_points,
        min_interval=min_interval,
    )
    if history is not None and replace(advanced, generated_at=history.generated_at) == history:
        return False, None
    try:
        write_private(path, dump_json(dump_history(advanced), compact=True))
    except OSError as exc:
        return False, f"{path.name} could not be written ({type(exc).__name__})"
    return True, None


def _keep_histories(
    status_dir: Path,
    reads: Sequence[LineRead],
    host: HostFacts,
    now: datetime,
    settings: HistorySettings,
) -> tuple[list[str], list[tuple[str, str]]]:
    """Step 6: every readable line's history and the host's; ``(files written, (name, why) not kept)``.

    A history that cannot be kept — the directory cannot be made, a file
    cannot be read or written — is reported, never raised: the tick's ping
    and ``exporter.json`` still follow.
    """
    written: list[str] = []
    errors: list[tuple[str, str]] = []
    try:
        ensure_private_dir(status_dir / HISTORY_DIR)
    except OSError as exc:
        why = f"{HISTORY_DIR}/ could not be made ({type(exc).__name__})"
        return [], [(read.name, why) for read in reads if read.ok] + [(HOST_KIND, why)]

    def keep(name: str, kind: str, run: str, document: Mapping[str, Any], gap: timedelta) -> None:
        was_written, error = _keep_history(
            status_dir,
            name,
            kind=kind,
            run=run,
            document=document,
            now=now,
            max_points=settings.max_points,
            min_interval=gap,
        )
        if was_written:
            written.append(history_file(name))
        if error is not None:
            errors.append((name, error))

    for read in reads:
        if read.document is not None:
            keep(read.name, read.kind, read.run_label, read.document, timedelta(0))
    # The host's document is host.json's shape: the harvest reads the same keys.
    host_gap = timedelta(minutes=settings.host_sample_minutes)
    keep(HOST_KIND, HOST_KIND, host.hostname, asdict(host), host_gap)
    return written, errors


def _ping(url: str, *, urlopen: _Urlopen) -> str | None:
    """``None`` when the GET went through, else why not."""
    try:
        with urlopen(url, timeout=_PING_TIMEOUT) as response:
            response.read()
    except (OSError, ValueError, http.client.HTTPException) as exc:
        return f"{type(exc).__name__}: {getattr(exc, 'reason', '') or exc}"
    return None


def _judge(
    config: ExporterConfig,
    state: State,
    *,
    now: datetime,
    runner: Runner,
    host_reader: HostReader,
) -> tuple[tuple[LineRead, ...], HostFacts, Evaluation]:
    """Steps 2 and 3 of a tick, against ``state``: read every line and the host, then the rules."""
    if now.tzinfo is None:
        raise ValueError("the tick's clock must give an aware datetime")
    reads = tuple(
        read_line(line, runner=runner, timeout=config.command_timeout_seconds)
        for line in config.lines
    )
    host = host_reader(config, now=now, runner=runner)
    evaluation = evaluate(
        now=now,
        config=config,
        reads=reads,
        host=host,
        events_seen=state.events_seen,
        held=state.active,
    )
    return reads, host, evaluation


def _send(
    mail: MailSettings, subject: str, body: str, now: datetime, smtp_factory: SmtpFactory
) -> str | None:
    """Send one letter; ``None`` when every recipient took it, else what went wrong.

    A send refused whole raises :class:`MailError`; a recipient refused
    while the others took the letter is reported, not retried (the letter
    reached the rest).
    """
    refused = send_mail(mail, subject=subject, body=body, now=now, smtp_factory=smtp_factory)
    return f"{refused} recipient(s) refused" if refused else None


def run_once(
    config: ExporterConfig,
    settings: Settings,
    *,
    now: datetime,
    runner: Runner = run_command,
    smtp_factory: SmtpFactory = smtp_connection,
    urlopen: _Urlopen = urllib.request.urlopen,
    host_reader: HostReader = read_host,
) -> TickReport:
    """One tick, as the module docstring orders it. ``StateError`` when the state file cannot be read."""
    started = time.monotonic()
    status_dir = config.status_dir
    status_dir.mkdir(parents=True, exist_ok=True)
    state_path = status_dir / STATE_FILE
    state = load_state(state_path)

    reads, host, evaluation = _judge(config, state, now=now, runner=runner, host_reader=host_reader)
    was_pending = len(state.pending)
    state = advance(state, evaluation, now)
    save_state(state_path, state)
    new = state.pending[was_pending:]
    raised = tuple(item.key for item in new if item.kind == "raised")
    cleared = tuple(item.key for item in new if item.kind == "cleared")

    mailed = 0
    digest_sent = False
    mail_error: str | None = None
    if settings.mail is not None:
        if state.pending:
            subject, body = transitions_letter(config.subject_prefix, state.pending)
            try:
                mail_error = _send(settings.mail, subject, body, now, smtp_factory)
                mailed = len(state.pending)
                state = replace(state, pending=())
            except MailError as exc:
                mail_error = str(exc)
        today = utc_day(now)
        if state.last_digest_day != today and clock_reached(now, config.digest_utc):
            subject, body = digest_letter(
                config.subject_prefix,
                now=now,
                reads=reads,
                host=host,
                active=state.active,
                notes=evaluation.notes,
            )
            try:
                problem = _send(settings.mail, subject, body, now, smtp_factory)
                digest_sent = True
                state = replace(state, last_digest_day=today)
            except MailError as exc:
                problem = str(exc)
            if problem is not None:
                mail_error = problem if mail_error is None else f"{mail_error}; digest: {problem}"
        save_state(state_path, state)
    elif state.pending:
        # No account to send them through: the letter lines are not kept for one.
        state = replace(state, pending=())
        save_state(state_path, state)

    written: list[str] = []
    for read in reads:
        if read.raw is not None:
            write_private(status_dir / f"{read.name}.json", read.raw)
            written.append(f"{read.name}.json")
    write_private(status_dir / "host.json", dump_json(host))
    written.append("host.json")
    write_private(status_dir / "alerts.json", dump_json(_alerts_document(now, state, evaluation)))
    written.append("alerts.json")
    kept, history_errors = _keep_histories(status_dir, reads, host, now, config.history)
    written.extend(kept)

    ping: str | None = None
    ping_error: str | None = None
    if settings.healthchecks_url is not None:
        ping = "fail" if (mail_error is not None or state.pending) else "ok"
        url = settings.healthchecks_url + ("/fail" if ping == "fail" else "")
        ping_error = _ping(url, urlopen=urlopen)

    report = TickReport(
        generated_at=iso(now),
        reads=reads,
        evaluation=evaluation,
        raised=raised,
        cleared=cleared,
        mail_enabled=settings.mail is not None,
        mailed=mailed,
        digest_sent=digest_sent,
        mail_error=mail_error,
        pending=len(state.pending),
        ping=ping,
        ping_error=ping_error,
        written=tuple(written) + ("exporter.json",),
        history_errors=tuple(history_errors),
        duration_ms=int((time.monotonic() - started) * 1000),
    )
    write_private(status_dir / "exporter.json", dump_json(_exporter_document(now, report)))
    return report


def read_and_judge(
    config: ExporterConfig,
    *,
    now: datetime,
    runner: Runner = run_command,
    host_reader: HostReader = read_host,
) -> tuple[tuple[LineRead, ...], HostFacts, Evaluation]:
    """Read and evaluate as a tick would, writing and mailing nothing (``check``).

    The rules see the saved state when there is one, so the events and the
    carried conditions a tick would mail are the ones shown.
    """
    state = load_state(config.status_dir / STATE_FILE)
    return _judge(config, state, now=now, runner=runner, host_reader=host_reader)


def summary_line(report: TickReport) -> str:
    """The one line ``export`` prints."""
    ok = sum(1 for read in report.reads if read.ok)
    mail = "off" if not report.mail_enabled else f"sent {report.mailed}, pending {report.pending}"
    if report.mail_error is not None:
        mail += f" ({report.mail_error[:60]})"
    ping = (
        "off" if report.ping is None else report.ping + (" (failed)" if report.ping_error else "")
    )
    history = (
        f"; {len(report.history_errors)} history file(s) not kept" if report.history_errors else ""
    )
    return (
        f"{report.generated_at}: read {ok}/{len(report.reads)} line(s); "
        f"{len(report.evaluation.conditions)} condition(s) hold, {len(report.raised)} raised, "
        f"{len(report.cleared)} cleared, {len(report.evaluation.events)} event(s); mail {mail}; "
        f"digest {'sent' if report.digest_sent else 'no'}; ping {ping}{history}"
    )


def failure_lines(reads: Sequence[LineRead]) -> list[str]:
    """One line per line that was not read, for stderr."""
    return [
        f"{read.name}: {read.failure}" + (f" -- {read.stderr_line}" if read.stderr_line else "")
        for read in reads
        if not read.ok
    ]


def history_lines(report: TickReport) -> list[str]:
    """One line per history not kept this tick, for stderr."""
    return [f"{name}: {why}" for name, why in report.history_errors]
