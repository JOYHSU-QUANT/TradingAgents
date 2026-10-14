"""What only the host knows: the units, the deployed commits, disk, memory, the journal.

Read as the trader user, without ``sudo``: ``systemctl show`` answers anyone
for a system unit, ``git rev-parse`` reads the checkouts the user owns,
``/proc/meminfo`` and ``statvfs`` need nothing. The journal does — the
error count is ``None`` when ``journalctl`` refuses, and the alert rules do
not read it; it is a number on the page. Every read that fails leaves its
field ``None`` (and, for a unit, its ``error`` set) rather than fail the
tick: a host fact missing is a fact about the host.

The unit facts are the properties the rules and the page use:
``Type`` (``oneshot`` for the visits and the coordinator, ``simple`` for the
daemons: a oneshot is judged by its last run, a daemon by being active),
``LoadState`` (``not-found`` for a unit not installed), ``ActiveState``,
``SubState``, ``Result``, ``NRestarts``, the main process's start and exit
instants (as systemd spells them, ``Mon 2026-10-13 23:50:12 UTC``) and exit
status.
"""

from __future__ import annotations

import os
import shutil
import socket
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .config import ExporterConfig
from .readers import Runner, masked_line
from .times import iso

__all__ = [
    "HOST_CWD",
    "Checkout",
    "DiskFacts",
    "HostFacts",
    "MemoryFacts",
    "UnitFacts",
    "read_commit",
    "read_disk",
    "read_host",
    "read_journal_errors",
    "read_memory",
    "read_unit",
]

_UNIT_PROPERTIES = (
    "Type",
    "LoadState",
    "ActiveState",
    "SubState",
    "Result",
    "NRestarts",
    "ExecMainStartTimestamp",
    "ExecMainExitTimestamp",
    "ExecMainStatus",
)
_HOST_COMMAND_TIMEOUT = 20.0
# Where systemctl and journalctl are run from: a directory that is always there,
# not a checkout a line names (one missing checkout must not blind every unit).
HOST_CWD = Path("/")
_JOURNAL_SINCE = "-24h"


@dataclass(frozen=True)
class UnitFacts:
    """One systemd unit as ``systemctl show`` reports it; ``error`` set (and the rest ``None``) when it could not."""

    unit: str
    type: str | None = None
    load_state: str | None = None
    active_state: str | None = None
    sub_state: str | None = None
    result: str | None = None
    n_restarts: int | None = None
    exec_main_start: str | None = None
    exec_main_exit: str | None = None
    exec_main_status: int | None = None
    error: str | None = None

    @property
    def running(self) -> bool:
        """Known to be active: read, and ``active`` (a daemon doing its job)."""
        return self.error is None and self.active_state == "active"


@dataclass(frozen=True)
class Checkout:
    """A checkout a line runs from and the commit it is at (``None`` when git could not say)."""

    path: str
    commit: str | None


@dataclass(frozen=True)
class DiskFacts:
    path: str
    total_bytes: int
    used_bytes: int
    free_bytes: int
    used_pct: float


@dataclass(frozen=True)
class MemoryFacts:
    total_kb: int
    available_kb: int


@dataclass(frozen=True)
class HostFacts:
    """The ``host.json`` document."""

    schema_version: int
    source: str
    generated_at: str
    hostname: str
    units: tuple[UnitFacts, ...]
    checkouts: tuple[Checkout, ...]
    disk: DiskFacts | None
    memory: MemoryFacts | None
    load_average: tuple[float, float, float] | None
    journal_errors_24h: dict[str, int | None]

    def unit(self, name: str) -> UnitFacts | None:
        for unit in self.units:
            if unit.unit == name:
                return unit
        return None

    def units_text(self) -> str:
        """Every unit and its state on one line, for an operator."""
        return ", ".join(f"{unit.unit} {unit.active_state or 'unreadable'}" for unit in self.units)


def _value(properties: dict[str, str], key: str) -> str | None:
    value = properties.get(key, "").strip()
    return value or None


def _int_value(properties: dict[str, str], key: str) -> int | None:
    value = _value(properties, key)
    return int(value) if value is not None and value.lstrip("-").isdigit() else None


def read_unit(unit: str, *, runner: Runner) -> UnitFacts:
    """``systemctl show`` for ``unit``."""
    argv = ["systemctl", "show", unit, "--property=" + ",".join(_UNIT_PROPERTIES)]
    result = runner(argv, HOST_CWD, _HOST_COMMAND_TIMEOUT)
    if result.exit_code != 0:
        why = masked_line(result.stderr) or (
            "timed out" if result.exit_code is None else f"exit {result.exit_code}"
        )
        return UnitFacts(unit, error=why)
    properties: dict[str, str] = {}
    for line in result.stdout.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            properties[key.strip()] = value
    return UnitFacts(
        unit=unit,
        type=_value(properties, "Type"),
        load_state=_value(properties, "LoadState"),
        active_state=_value(properties, "ActiveState"),
        sub_state=_value(properties, "SubState"),
        result=_value(properties, "Result"),
        n_restarts=_int_value(properties, "NRestarts"),
        exec_main_start=_value(properties, "ExecMainStartTimestamp"),
        exec_main_exit=_value(properties, "ExecMainExitTimestamp"),
        exec_main_status=_int_value(properties, "ExecMainStatus"),
    )


def read_commit(checkout: Path, *, runner: Runner) -> Checkout:
    """The short commit ``checkout`` is at."""
    argv = ["git", "-C", str(checkout), "rev-parse", "--short", "HEAD"]
    result = runner(argv, checkout, _HOST_COMMAND_TIMEOUT)
    commit = result.stdout.strip() if result.exit_code == 0 else ""
    return Checkout(str(checkout), commit or None)


def read_disk(path: Path) -> DiskFacts | None:
    try:
        usage = shutil.disk_usage(path)
    except OSError:
        return None
    used_pct = 100.0 * usage.used / usage.total if usage.total else 0.0
    return DiskFacts(str(path), usage.total, usage.used, usage.free, round(used_pct, 1))


def read_memory(meminfo: Path) -> MemoryFacts | None:
    """``MemTotal`` and ``MemAvailable`` from a ``/proc/meminfo``-shaped file."""
    try:
        text = meminfo.read_text(encoding="ascii", errors="replace")
    except OSError:
        return None
    found: dict[str, int] = {}
    for line in text.splitlines():
        key, sep, rest = line.partition(":")
        parts = rest.split()
        if sep and parts and parts[0].isdigit():
            found[key.strip()] = int(parts[0])
    if "MemTotal" not in found or "MemAvailable" not in found:
        return None
    return MemoryFacts(found["MemTotal"], found["MemAvailable"])


def read_journal_errors(unit: str, *, runner: Runner) -> int | None:
    """How many ``err``-or-worse lines ``unit`` wrote to the journal in the last day.

    ``None`` when the journal is not readable: ``journalctl`` failed, or
    answered nothing but a hint on stderr (the trader user outside the
    journal group).
    """
    # Not --quiet: it would also silence the hint that tells a user outside the
    # journal group from an empty journal.
    argv = ["journalctl", "-u", unit, "-p", "err", "--since", _JOURNAL_SINCE]
    argv += ["--no-pager", "-o", "cat"]
    result = runner(argv, HOST_CWD, _HOST_COMMAND_TIMEOUT)
    if result.exit_code != 0:
        return None
    if not result.stdout.strip() and result.stderr.strip():
        return None
    # journalctl's own lines ("-- No entries --") are not errors.
    return sum(
        1 for line in result.stdout.splitlines() if line.strip() and not line.startswith("-- ")
    )


def _load_average() -> tuple[float, float, float] | None:
    # Not on every platform (typeshed has it POSIX-only), and it can fail on one that has it.
    loadavg = getattr(os, "getloadavg", None)
    if loadavg is None:
        return None
    try:
        one, five, fifteen = loadavg()
    except OSError:
        return None
    return (round(one, 2), round(five, 2), round(fifteen, 2))


def read_host(
    config: ExporterConfig,
    *,
    now: datetime,
    runner: Runner,
    disk_path: Path = Path("/"),
    meminfo: Path = Path("/proc/meminfo"),
    hostname: Callable[[], str] = socket.gethostname,
) -> HostFacts:
    """Every host fact the config's lines call for, read now."""
    units = config.units()
    return HostFacts(
        schema_version=1,
        source="dashboard",
        generated_at=iso(now),
        hostname=hostname(),
        units=tuple(read_unit(unit, runner=runner) for unit in units),
        checkouts=tuple(read_commit(path, runner=runner) for path in config.checkouts()),
        disk=read_disk(disk_path),
        memory=read_memory(meminfo),
        load_average=_load_average(),
        journal_errors_24h={unit: read_journal_errors(unit, runner=runner) for unit in units},
    )
