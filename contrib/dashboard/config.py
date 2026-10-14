"""The exporter's settings: the lines, the thresholds, where it writes — and, from the environment, where it mails.

Two sources, kept apart on purpose. The YAML file (``configs/exporter.example.yaml``
is the template; the host's copy is a gitignored ``*.local.yaml``) holds what
is not secret: the status directory, the lines with the command each is read
by, the thresholds the alert rules compare against, the digest hour. The mail
account and the dead-man URL come from the environment — the repo-root
``.env`` the daemons already keep their keys in, read here for the
``DASHBOARD_*`` names only (:func:`read_env_file`), the process environment
winning over the file — so no YAML ever holds a password, and no document
this package writes can copy one out (:class:`MailSettings` keeps its
password out of its ``repr``).

Everything is a frozen dataclass. An unknown key, a missing one or a value of
the wrong shape is refused by name (:class:`ConfigError`), never defaulted;
a refusal names the key, never the value, because a value could be a secret
pasted into the wrong slot. A relative path in the YAML is taken from the
YAML file's own directory; a bare program name as a line's ``python`` is
left for ``PATH`` to find, as a shell would.

A *line* is one thing the host runs that has a document: a Hyperliquid run
(``python -m contrib.hyperliquid_perp status --json``), a Uniswap run
(``python -m contrib.uniswap_v3 status --json``) or a carry sleeve
(``python -m contrib.carry report --json``). Each names the python and the
checkout to run its command from (the Uniswap checkout is its own), the
systemd unit that drives it, and the stores and run ids its command takes.
A line's name is the file its document is written to under the status
directory, so it is a file name, and not one of the files the exporter
writes itself (:data:`RESERVED_NAMES`).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import time
from pathlib import Path
from typing import Any, ClassVar

import yaml

__all__ = [
    "CarryLine",
    "ConfigError",
    "ExporterConfig",
    "HyperliquidLine",
    "Line",
    "MailSettings",
    "Settings",
    "Thresholds",
    "UniswapLine",
    "load_config",
    "load_settings",
    "read_env_file",
]


class ConfigError(ValueError):
    """A setting that cannot be used, named by its key."""


# A line's name is also its file name under the status directory.
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
# The files the exporter writes beside the lines' documents.
RESERVED_NAMES = frozenset({"host", "alerts", "exporter", "alerts-state"})
# The environment names this package reads; nothing else in the .env is looked at.
ENV_PREFIX = "DASHBOARD_"
_CLOCK = re.compile(r"^\d\d:\d\d$")
_DEFAULT_SMTP_PORT = 587


@dataclass(frozen=True)
class Thresholds:
    """What the alert rules compare against (:mod:`.alerts`); the defaults are the plan's (2026-10-14)."""

    cycle_overdue_minutes: int = 30
    lease_stale_minutes: int = 15
    export_stale_hours: float = 5.0
    uniswap_behind_hours: float = 3.5
    uniswap_no_decision_hours: float = 48.0
    handoff_due_utc: time = time(0, 30)
    d5_days: int = 3
    disk_used_pct: int = 85
    memory_available_mb: int = 200


@dataclass(frozen=True)
class HyperliquidLine:
    """A Hyperliquid run: its daemon's unit, its store and run id.

    ``segment_marker`` is the flat watcher's ``SEGMENT_SWITCH_READY`` file
    when the line has one: while it is present the daemon is parked between
    segments on purpose, and the unit and progress rules are skipped.
    """

    kind: ClassVar[str] = "hyperliquid"
    source: ClassVar[str] = "hyperliquid_perp"

    name: str
    python: Path
    cwd: Path
    unit: str
    db: Path
    run_id: str
    segment_marker: Path | None = None
    recent: int = 10

    def parked(self) -> bool:
        """Whether the segment switch marker is present right now."""
        return self.segment_marker is not None and self.segment_marker.exists()

    def argv(self) -> list[str]:
        return [
            str(self.python),
            "-m",
            "contrib.hyperliquid_perp",
            "status",
            "--db",
            str(self.db),
            "--run-id",
            self.run_id,
            "--recent",
            str(self.recent),
            "--json",
        ]


@dataclass(frozen=True)
class UniswapLine:
    """A Uniswap paper run: the visit unit, the run's config file, the store and run id."""

    kind: ClassVar[str] = "uniswap"
    source: ClassVar[str] = "uniswap_v3"

    name: str
    python: Path
    cwd: Path
    unit: str
    config: Path
    db: Path
    run_id: str
    bars: int = 5

    def argv(self) -> list[str]:
        return [
            str(self.python),
            "-m",
            "contrib.uniswap_v3",
            "status",
            "--config",
            str(self.config),
            "--db",
            str(self.db),
            "--run-id",
            self.run_id,
            "--bars",
            str(self.bars),
            "--json",
        ]


@dataclass(frozen=True)
class CarryLine:
    """A carry sleeve: the coordinator's unit, the two legs' stores and run ids, the handoff file.

    The report covers the sleeve's whole history (its own default span): a
    ``--since`` before the runs' first day would read as silent days.
    """

    kind: ClassVar[str] = "carry"
    source: ClassVar[str] = "carry"

    name: str
    python: Path
    cwd: Path
    unit: str
    coin: str
    perp_db: Path
    perp_run_id: str
    spot_db: Path
    spot_run_id: str
    handoff: Path
    payloads_dir: Path | None = None

    def argv(self) -> list[str]:
        argv = [
            str(self.python),
            "-m",
            "contrib.carry",
            "report",
            "--coin",
            self.coin,
            "--perp-db",
            str(self.perp_db),
            "--perp-run-id",
            self.perp_run_id,
            "--spot-db",
            str(self.spot_db),
            "--spot-run-id",
            self.spot_run_id,
            "--json",
            "--handoff",
            str(self.handoff),
        ]
        if self.payloads_dir is not None:
            argv += ["--payloads-dir", str(self.payloads_dir)]
        return argv


Line = HyperliquidLine | UniswapLine | CarryLine
_KINDS: dict[str, type[Line]] = {
    HyperliquidLine.kind: HyperliquidLine,
    UniswapLine.kind: UniswapLine,
    CarryLine.kind: CarryLine,
}


@dataclass(frozen=True)
class ExporterConfig:
    """The YAML file as a whole."""

    status_dir: Path
    lines: tuple[Line, ...]
    thresholds: Thresholds = Thresholds()
    digest_utc: time = time(3, 30)
    command_timeout_seconds: int = 60
    subject_prefix: str = "[trading]"

    def units(self) -> tuple[str, ...]:
        """Every unit a line names, once each, sorted."""
        return tuple(sorted({line.unit for line in self.lines}))

    def checkouts(self) -> tuple[Path, ...]:
        """Every checkout a line runs its command from, once each, sorted."""
        return tuple(sorted({line.cwd for line in self.lines}))


# --- the YAML -------------------------------------------------------------------


def _mapping(value: object, where: str) -> dict[str, Any]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ConfigError(f"{where} must be a mapping with string keys")
    return value


def _keys(
    mapping: Mapping[str, Any], where: str, *, required: tuple[str, ...], optional: tuple[str, ...]
) -> None:
    missing = [key for key in required if key not in mapping]
    if missing:
        raise ConfigError(f"{where} is missing {', '.join(missing)}")
    unknown = sorted(set(mapping) - set(required) - set(optional))
    if unknown:
        raise ConfigError(f"{where} has unknown key(s) {', '.join(unknown)}")


def _text(mapping: Mapping[str, Any], key: str, where: str) -> str:
    value = mapping[key]
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{where}.{key} must be a non-empty string")
    return value.strip()


def _path(mapping: Mapping[str, Any], key: str, where: str, base: Path) -> Path:
    text = _text(mapping, key, where)
    path = Path(text)
    # A POSIX-absolute path is absolute wherever the YAML is read (the host is
    # Linux; a test may run elsewhere).
    return path if path.is_absolute() or text.startswith("/") else base / path


def _program(mapping: Mapping[str, Any], key: str, where: str, base: Path) -> Path:
    """Like :func:`_path`, except that a bare program name (no path separator) is left for PATH."""
    text = _text(mapping, key, where)
    if "/" not in text and "\\" not in text:
        return Path(text)
    return _path(mapping, key, where, base)


# The three value parsers share one shape, so a table can name them bare.
def _int(mapping: Mapping[str, Any], key: str, where: str, default: int) -> int:
    value = mapping.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ConfigError(f"{where}.{key} must be an integer of at least 1")
    return value


def _hours(mapping: Mapping[str, Any], key: str, where: str, default: float) -> float:
    value = mapping.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not value > 0:
        raise ConfigError(f"{where}.{key} must be a positive number")
    return float(value)


def _clock(mapping: Mapping[str, Any], key: str, where: str, default: time) -> time:
    value = mapping.get(key, default)
    if isinstance(value, time):
        return value
    if isinstance(value, int) and not isinstance(value, bool) and 0 <= value < 24 * 60:
        # YAML 1.1 reads an unquoted 03:30 as the sexagesimal integer 210.
        return time(value // 60, value % 60)
    if not isinstance(value, str) or not _CLOCK.match(value):
        raise ConfigError(f"{where}.{key} must be a UTC clock time as HH:MM")
    try:
        return time.fromisoformat(value)
    except ValueError as exc:
        raise ConfigError(f"{where}.{key} must be a UTC clock time as HH:MM") from exc


# One parser per threshold, by name: tests/test_config.py holds the table to the fields.
_THRESHOLD_PARSERS: dict[str, Callable[[Mapping[str, Any], str, str, Any], Any]] = {
    "cycle_overdue_minutes": _int,
    "lease_stale_minutes": _int,
    "export_stale_hours": _hours,
    "uniswap_behind_hours": _hours,
    "uniswap_no_decision_hours": _hours,
    "handoff_due_utc": _clock,
    "d5_days": _int,
    "disk_used_pct": _int,
    "memory_available_mb": _int,
}


def _thresholds(raw: object, where: str) -> Thresholds:
    mapping = _mapping(raw, where)
    _keys(mapping, where, required=(), optional=tuple(_THRESHOLD_PARSERS))
    return Thresholds(
        **{
            name: parse(mapping, name, where, getattr(Thresholds, name))
            for name, parse in _THRESHOLD_PARSERS.items()
        }
    )


def _common(mapping: Mapping[str, Any], where: str, base: Path) -> dict[str, Any]:
    name = _text(mapping, "name", where)
    if not _NAME.match(name):
        raise ConfigError(
            f"{where}.name must be a file name: letters, digits, '.', '_' or '-', "
            "starting with a letter or digit, at most 64 characters"
        )
    if name in RESERVED_NAMES:
        raise ConfigError(f"{where}.name {name!r} is a file the exporter writes itself")
    return {
        "name": name,
        "python": _program(mapping, "python", where, base),
        "cwd": _path(mapping, "cwd", where, base),
        "unit": _text(mapping, "unit", where),
    }


_COMMON_KEYS = ("kind", "name", "python", "cwd", "unit")


def _line(raw: object, where: str, base: Path) -> Line:
    mapping = _mapping(raw, where)
    if "kind" not in mapping:
        raise ConfigError(f"{where} is missing kind")
    kind = _text(mapping, "kind", where)
    if kind not in _KINDS:
        raise ConfigError(f"{where}.kind must be one of {', '.join(sorted(_KINDS))}")
    if kind == HyperliquidLine.kind:
        _keys(
            mapping,
            where,
            required=_COMMON_KEYS + ("db", "run_id"),
            optional=("segment_marker", "recent"),
        )
        marker = (
            _path(mapping, "segment_marker", where, base) if "segment_marker" in mapping else None
        )
        return HyperliquidLine(
            **_common(mapping, where, base),
            db=_path(mapping, "db", where, base),
            run_id=_text(mapping, "run_id", where),
            segment_marker=marker,
            recent=_int(mapping, "recent", where, HyperliquidLine.recent),
        )
    if kind == UniswapLine.kind:
        _keys(
            mapping, where, required=_COMMON_KEYS + ("config", "db", "run_id"), optional=("bars",)
        )
        return UniswapLine(
            **_common(mapping, where, base),
            config=_path(mapping, "config", where, base),
            db=_path(mapping, "db", where, base),
            run_id=_text(mapping, "run_id", where),
            bars=_int(mapping, "bars", where, UniswapLine.bars),
        )
    _keys(
        mapping,
        where,
        required=_COMMON_KEYS
        + ("coin", "perp_db", "perp_run_id", "spot_db", "spot_run_id", "handoff"),
        optional=("payloads_dir",),
    )
    payloads = _path(mapping, "payloads_dir", where, base) if "payloads_dir" in mapping else None
    return CarryLine(
        **_common(mapping, where, base),
        coin=_text(mapping, "coin", where),
        perp_db=_path(mapping, "perp_db", where, base),
        perp_run_id=_text(mapping, "perp_run_id", where),
        spot_db=_path(mapping, "spot_db", where, base),
        spot_run_id=_text(mapping, "spot_run_id", where),
        handoff=_path(mapping, "handoff", where, base),
        payloads_dir=payloads,
    )


def load_config(path: Path) -> ExporterConfig:
    """The YAML file at ``path`` as an :class:`ExporterConfig`; ``ConfigError`` for anything unusable."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"{path}: cannot be read ({exc.strerror or type(exc).__name__})") from exc
    except UnicodeDecodeError as exc:
        raise ConfigError(f"{path}: is not UTF-8 text") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: is not valid YAML ({exc})") from exc
    where = str(path)
    top = _mapping(raw, where)
    _keys(
        top,
        where,
        required=("status_dir", "lines"),
        optional=("thresholds", "digest_utc", "command_timeout_seconds", "subject_prefix"),
    )
    base = path.resolve().parent
    items = top["lines"]
    if not isinstance(items, list) or not items:
        raise ConfigError(f"{where}.lines must be a non-empty list")
    lines = tuple(_line(item, f"{where}.lines[{i}]", base) for i, item in enumerate(items))
    names = [line.name for line in lines]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise ConfigError(f"{where}.lines name {', '.join(duplicates)} more than once")
    prefix = top.get("subject_prefix", ExporterConfig.subject_prefix)
    if not isinstance(prefix, str) or not prefix.strip() or "\n" in prefix:
        raise ConfigError(f"{where}.subject_prefix must be one line of text")
    return ExporterConfig(
        status_dir=_path(top, "status_dir", where, base),
        lines=lines,
        thresholds=_thresholds(top.get("thresholds", {}), f"{where}.thresholds"),
        digest_utc=_clock(top, "digest_utc", where, ExporterConfig.digest_utc),
        command_timeout_seconds=_int(
            top, "command_timeout_seconds", where, ExporterConfig.command_timeout_seconds
        ),
        subject_prefix=prefix.strip(),
    )


# --- the environment ------------------------------------------------------------


@dataclass(frozen=True)
class MailSettings:
    """One SMTP account with STARTTLS (port 587 by default) and the recipients; the password is not in ``repr``."""

    host: str
    port: int
    user: str
    sender: str
    recipients: tuple[str, ...]
    password: str = field(repr=False)


@dataclass(frozen=True)
class Settings:
    """What the environment gives: the mail account (``None``: JSON only, no mail) and the dead-man URL (``None``: no ping)."""

    mail: MailSettings | None
    healthchecks_url: str | None


def read_env_file(path: Path) -> dict[str, str]:
    """The ``DASHBOARD_*`` assignments in a ``.env`` file; every other line is left unread.

    ``KEY=VALUE`` one per line, an optional ``export``, ``#`` comments,
    matching single or double quotes stripped, an unquoted value cut at
    `` #``. A line that is not an assignment of a ``DASHBOARD_*`` name is
    skipped, whatever it holds.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"{path}: cannot be read ({exc.strerror or type(exc).__name__})") from exc
    except UnicodeDecodeError as exc:
        raise ConfigError(f"{path}: is not UTF-8 text") from exc
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not key.startswith(ENV_PREFIX) or not key.isidentifier():
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        else:
            value = value.split(" #", 1)[0].rstrip()
        values[key] = value
    return values


def _addresses(text: str, name: str) -> tuple[str, ...]:
    found = tuple(part.strip() for part in text.split(",") if part.strip())
    if not found or any(
        "@" not in address or any(c.isspace() for c in address) for address in found
    ):
        raise ConfigError(f"{name} must be one or more mail addresses, comma-separated")
    return found


def load_settings(*, environ: Mapping[str, str], env_file: Path | None) -> Settings:
    """The mail account and dead-man URL from ``env_file`` (when it exists) and ``environ`` (which wins).

    Mail is on when ``DASHBOARD_SMTP_HOST`` is set, and then takes
    ``DASHBOARD_SMTP_USER``, ``DASHBOARD_SMTP_PASSWORD``, ``DASHBOARD_MAIL_FROM``
    and ``DASHBOARD_MAIL_TO`` (comma-separated), with ``DASHBOARD_SMTP_PORT``
    587 unless set. ``DASHBOARD_HEALTHCHECKS_URL`` must be ``https://``.
    """
    values: dict[str, str] = {}
    if env_file is not None and env_file.is_file():
        values.update(read_env_file(env_file))
    values.update({key: value for key, value in environ.items() if key.startswith(ENV_PREFIX)})
    mail = None
    host = values.get("DASHBOARD_SMTP_HOST", "").strip()
    if host:
        missing = [
            name
            for name in (
                "DASHBOARD_SMTP_USER",
                "DASHBOARD_SMTP_PASSWORD",
                "DASHBOARD_MAIL_FROM",
                "DASHBOARD_MAIL_TO",
            )
            if not values.get(name, "").strip()
        ]
        if missing:
            raise ConfigError(
                f"DASHBOARD_SMTP_HOST is set but {', '.join(missing)} not: the mail account takes "
                "all of them"
            )
        port_text = values.get("DASHBOARD_SMTP_PORT", str(_DEFAULT_SMTP_PORT)).strip()
        if not port_text.isdigit() or not 1 <= int(port_text) <= 65535:
            raise ConfigError("DASHBOARD_SMTP_PORT must be a port number")
        mail = MailSettings(
            host=host,
            port=int(port_text),
            user=values["DASHBOARD_SMTP_USER"].strip(),
            sender=_addresses(values["DASHBOARD_MAIL_FROM"], "DASHBOARD_MAIL_FROM")[0],
            recipients=_addresses(values["DASHBOARD_MAIL_TO"], "DASHBOARD_MAIL_TO"),
            password=values["DASHBOARD_SMTP_PASSWORD"],
        )
    # Without a trailing slash: the /fail ping is appended to it.
    url = values.get("DASHBOARD_HEALTHCHECKS_URL", "").strip().rstrip("/") or None
    if url is not None and (not url.startswith("https://") or any(c.isspace() for c in url)):
        raise ConfigError("DASHBOARD_HEALTHCHECKS_URL must be an https:// URL")
    return Settings(mail=mail, healthchecks_url=url)
