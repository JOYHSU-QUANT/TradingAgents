"""``python -m contrib.dashboard``: ``export``, ``check``, ``test-mail``.

``export`` is one tick (:func:`.exporter.run_once`), what the timer runs;
``check`` reads and judges the same way and prints what holds, writing and
mailing nothing — for an operator's eyes after a change; ``test-mail`` sends
one letter through the account so the SMTP settings are proven before the
first alert needs them.

Exit codes: ``0`` done (an ``export`` whose lines failed to read is still
done: the failures are in ``exporter.json`` and, as alerts, in the mail);
``1`` a setting that cannot be used — the YAML, the ``.env`` names, a state
file this build cannot read, a ``test-mail`` without a mail account, whose
send failed or had a recipient refused; ``2`` usage; ``3`` an ``export`` that ran whole but could not
send a letter (or a recipient was refused), so the unit shows as failed
until a tick sends again.

The ``.env`` read for the ``DASHBOARD_*`` names is the repository root's
unless ``--env-file`` names another (one that must exist: a mistyped path
must not pass as "no mail"); the process environment wins over it.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from pathlib import Path

from .alerts import StateError
from .config import ConfigError, load_config, load_settings
from .exporter import failure_lines, read_and_judge, run_once, summary_line
from .mail import MailError, SmtpFactory, send_mail, smtp_connection
from .readers import Runner, run_command
from .times import show_when, utc_now

__all__ = ["main"]

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_ENV_FILE = _REPO_ROOT / ".env"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m contrib.dashboard",
        description=(
            "Export the paper traders' status documents and mail the operator on a change of state."
        ),
    )
    commands = parser.add_subparsers(dest="command", required=True)
    for name, text in (
        ("export", "one tick: read every line and the host, mail the changes, write the documents"),
        ("check", "read and judge as a tick would, print what holds; write and mail nothing"),
        ("test-mail", "send one letter through the configured account"),
    ):
        sub = commands.add_parser(name, help=text, description=text)
        sub.add_argument("--config", required=True, type=Path, help="the exporter's YAML file")
        sub.add_argument(
            "--env-file",
            type=Path,
            default=None,
            help=f"the .env to read the DASHBOARD_* names from (default: {_DEFAULT_ENV_FILE})",
        )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    now: Callable[[], datetime] = utc_now,
    runner: Runner = run_command,
    smtp_factory: SmtpFactory = smtp_connection,
    environ: Mapping[str, str] = os.environ,
) -> int:
    """Run one command; ``now``, ``runner``, ``smtp_factory`` and ``environ`` are the seams a test binds."""
    args = _build_parser().parse_args(argv)
    try:
        config = load_config(args.config)
        if args.env_file is not None and not args.env_file.is_file():
            raise ConfigError(f"{args.env_file}: no such .env file")
        settings = load_settings(environ=environ, env_file=args.env_file or _DEFAULT_ENV_FILE)
        moment = now()
        if args.command == "export":
            report = run_once(
                config, settings, now=moment, runner=runner, smtp_factory=smtp_factory
            )
            for line in failure_lines(report.reads):
                print(f"not read: {line}", file=sys.stderr)
            if report.mail_error is not None:
                print(f"mail: {report.mail_error}", file=sys.stderr)
            if report.ping_error is not None:
                print(f"ping: {report.ping_error}", file=sys.stderr)
            print(summary_line(report))
            return 3 if report.mail_error is not None else 0
        if args.command == "check":
            reads, host, evaluation = read_and_judge(config, now=moment, runner=runner)
            for line in failure_lines(reads):
                print(f"not read: {line}", file=sys.stderr)
            print(f"{show_when(moment)}: {len(evaluation.conditions)} condition(s) hold")
            for condition in evaluation.conditions:
                print(f"  {condition.key}: {condition.text}")
            for event in evaluation.events:
                print(f"  event {event.key}: {event.text}")
            for note in evaluation.notes:
                print(f"  note: {note}")
            print(f"  units: {host.units_text()}")
            return 0
        if settings.mail is None:
            print(
                "dashboard: no mail account: set DASHBOARD_SMTP_HOST and the names it takes "
                "in the .env (see README)",
                file=sys.stderr,
            )
            return 1
        refused = send_mail(
            settings.mail,
            subject=f"{config.subject_prefix} test letter",
            body=f"The exporter's mail account works; sent {show_when(moment)}.\n",
            now=moment,
            smtp_factory=smtp_factory,
        )
        if refused:
            print(f"dashboard: {refused} recipient(s) refused the test letter", file=sys.stderr)
            return 1
        print(f"sent a test letter to {', '.join(settings.mail.recipients)}")
        return 0
    except (ConfigError, StateError, MailError) as exc:
        print(f"dashboard: {exc}", file=sys.stderr)
        return 1
