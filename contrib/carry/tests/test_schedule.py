"""The shipped schedule: the coordinator's timer and retry, the perp leg's unit, what the script runs."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from contrib.carry.cli import GRACE_HOURS
from contrib.carry.history import DEFAULT_MAX_READING_AGE_HOURS

_PACKAGE = Path(__file__).resolve().parents[1]
_SCHEDULE = _PACKAGE / "schedule"
_SCRIPT = _SCHEDULE / "carry-signal.sh"
_SERVICE = _SCHEDULE / "carry-signal.service"
_TIMER = _SCHEDULE / "carry-signal.timer"
_PERP_SERVICE = _SCHEDULE / "hl-paper-carry-eth.service"
_DEPLOY = _PACKAGE.parents[1] / ".github" / "workflows" / "deploy.yml"
_CHECKOUT = "/home/trader/TradingAgents"
_SH = shutil.which("sh")
# The venue settles funding on the hour; the handoff is written after the
# day's last settlement and before the boundary.
_LAST_SETTLEMENT = 23 * 3600


def _unit(path: Path) -> dict[str, list[str]]:
    """A unit file's keys to their values, in order; a key given twice has two."""
    values: dict[str, list[str]] = {}
    for line in path.read_text(encoding="ascii").splitlines():
        if line.startswith("#") or not line.strip() or line.startswith("["):
            continue
        key, _, value = line.partition("=")
        values.setdefault(key, []).append(value)
    return values


def _seconds(value: str) -> int:
    """A systemd span of one unit (``5min``, ``8min``, ``1h``, ``60``) in seconds."""
    for suffix, scale in (("min", 60), ("h", 3600), ("s", 1)):
        if value.endswith(suffix):
            return int(value[: -len(suffix)]) * scale
    return int(value)


def _timer_time() -> int:
    (when,) = _unit(_TIMER)["OnCalendar"]
    date, clock, zone = when.split()
    assert date == "*-*-*" and zone == "UTC", when
    hour, minute, second = (int(part) for part in clock.split(":"))
    return hour * 3600 + minute * 60 + second


def _lines(script: Path) -> list[str]:
    return script.read_text(encoding="ascii").splitlines()


def _at(prefix: str, script: Path = _SCRIPT) -> int:
    """The index of the script's first line that starts with ``prefix``, indented or not."""
    return next(i for i, line in enumerate(_lines(script)) if line.lstrip().startswith(prefix))


def test_the_timer_runs_once_a_day_after_the_last_settlement_and_before_the_boundary():
    at = _timer_time()
    assert _LAST_SETTLEMENT < at < 24 * 3600
    # The reading the run decides from is the 23:00 settlement, within the age limit.
    assert 24 * 3600 - _LAST_SETTLEMENT <= DEFAULT_MAX_READING_AGE_HOURS * 3600
    timer = _unit(_TIMER)
    assert timer["Persistent"] == ["true"]
    assert timer["Unit"] == ["carry-signal.service"]
    assert timer["WantedBy"] == ["timers.target"]


def test_the_service_retries_once_within_the_grace_and_runs_the_script_from_the_hl_checkout():
    service = _unit(_SERVICE)
    assert service["Type"] == ["oneshot"]
    assert service["User"] == ["trader"]
    assert service["WorkingDirectory"] == [_CHECKOUT]
    assert service["ExecStart"] == [f"{_CHECKOUT}/contrib/carry/schedule/carry-signal.sh"]
    assert service["Restart"] == ["on-failure"]
    # One retry (two starts in the interval), five minutes on: the plan's 23:55.
    assert service["StartLimitBurst"] == ["2"]
    assert _seconds(service["StartLimitIntervalSec"][0]) >= 2 * _seconds(service["RestartSec"][0])
    # The retry, even after a run that hit its timeout, still decides the same
    # boundary: a run within the grace after a midnight is a late run for it.
    worst = (24 * 3600 - _timer_time()) + _seconds(service["TimeoutStartSec"][0])
    worst += _seconds(service["RestartSec"][0])
    assert worst < GRACE_HOURS * 3600
    assert service["OOMScoreAdjust"] == ["500"]


def test_the_perp_leg_is_its_own_daemon_with_its_own_config_store_and_run():
    service = _unit(_PERP_SERVICE)
    (exec_start,) = service["ExecStart"]
    words = exec_start.split()
    assert words[0] == f"{_CHECKOUT}/.venv/bin/python"
    assert words[1:4] == ["-m", "contrib.hyperliquid_perp", "paper"]
    assert words[words.index("--coin") + 1] == "ETH"
    config = words[words.index("--config") + 1]
    assert config.endswith("hyperliquid-carry-eth.local.yaml")
    assert "hyperliquid.local.yaml" not in config  # the paper-BTC run's config is not touched
    assert words[words.index("--run-id") + 1] == "carry-ETH-1"
    assert words[words.index("--db") + 1] == "/home/trader/data/carry/paper_trading.db"
    assert "--create" not in words  # added by hand for the first start only (RUNBOOK)
    assert service["User"] == ["trader"]
    assert service["WorkingDirectory"] == [_CHECKOUT]
    assert service["Restart"] == ["on-failure"]
    assert service["WantedBy"] == ["multi-user.target"]


def test_the_deploy_workflow_tests_the_package_and_restarts_the_perp_leg_while_enabled():
    text = _DEPLOY.read_text(encoding="utf-8")
    assert "contrib/carry/tests/" in text
    lines = [line.strip() for line in text.splitlines()]
    # Enabled, not merely installed: a daemon the operator disabled (RUNBOOK 7)
    # must not come back with the next deploy.
    guard = "if systemctl is-enabled --quiet hl-paper-carry-eth 2>/dev/null; then"
    restart = lines.index("sudo systemctl restart hl-paper-carry-eth")
    assert lines[restart - 1] == guard and lines[restart + 1] == "fi"
    assert lines.index("sudo systemctl restart hl-paper") < restart
    # The coordinator's units are not restarted: a oneshot runs the checkout as it is.
    assert "carry-signal" not in text


def test_the_script_takes_its_settings_from_a_local_file_before_using_them():
    names = (
        "COIN",
        "HANDOFF",
        "RESEARCH_DB",
        "LOG",
        "PERP_DB",
        "PERP_RUN_ID",
        "SPOT_DB",
        "SPOT_RUN_ID",
        "SIGNAL_ARGS",
        "PYTHON",
    )
    defaults = [_at(f"{name}=") for name in names]
    local = _at('if [ -f "$here/carry-signal.local.sh" ]; then . "$here/carry-signal.local.sh"')
    root = _at('cd "$here/../../.."')
    assert max(defaults) < local < root
    lines = _lines(_SCRIPT)
    assert lines[root].endswith("|| exit 4")
    assert "export PYTHONUNBUFFERED=1" in lines
    assert lines[_at('"$PYTHON" -m contrib.carry')].endswith('>>"$LOG" 2>&1')


def test_the_shell_script_parses():
    if _SH is None:
        pytest.skip("no sh on this machine")
    subprocess.run([_SH, "-n", str(_SCRIPT)], check=True)


# A python that records what it is asked, one line per call, and exits as told.
_STUB = """#!/bin/sh
printf '%s\\n' "$*" >>"$STUB_LOG"
exit "${EXIT_SIGNAL:-0}"
"""


def _run(
    tmp_path: Path, monkeypatch, python: str | None = None, extra: str = "", exit_code: int = 0
) -> tuple[int, list[list[str]], list[str]]:
    """Run a copy of the script, laid out as in the repository, with a stub python.

    The local settings name ``python`` (the stub, by default) and the log,
    and then ``extra``, more lines of the file. Returns the exit code, the
    words of each call the stub was asked, and the log's lines.
    """
    if _SH is None:
        pytest.skip("no sh on this machine")
    schedule = tmp_path / "contrib" / "carry" / "schedule"
    schedule.mkdir(parents=True)
    script = schedule / "carry-signal.sh"
    script.write_bytes(_SCRIPT.read_bytes())
    stub = tmp_path / "python"
    stub.write_text(_STUB, encoding="ascii")
    stub.chmod(0o755)
    settings = f'PYTHON="{stub.as_posix() if python is None else python}"\n'
    settings += "LOG=signal.log\n" + extra
    (schedule / "carry-signal.local.sh").write_text(settings, encoding="ascii")
    asked = tmp_path / "asked.txt"
    monkeypatch.setenv("STUB_LOG", asked.as_posix())
    monkeypatch.setenv("EXIT_SIGNAL", str(exit_code))
    done = subprocess.run([_SH, str(script)], check=False)
    calls = []
    if asked.is_file():
        calls = [line.split() for line in asked.read_text(encoding="ascii").splitlines()]
    log = (tmp_path / "signal.log").read_text(encoding="ascii").splitlines()
    return done.returncode, calls, log


def test_a_run_is_one_signal_command_sized_as_equal_capital_until_the_runs_are_named(
    tmp_path, monkeypatch
):
    code, calls, log = _run(tmp_path, monkeypatch)
    assert code == 0
    (call,) = calls
    assert call[:3] == ["-m", "contrib.carry", "signal"]
    assert call[call.index("--coin") + 1] == "ETH"
    assert call[call.index("--out") + 1] == "contrib/carry/data/handoff-ETH.json"
    assert call[call.index("--research-db") + 1] == "contrib/carry/data/autoresearch.sqlite"
    assert not {"--perp-db", "--perp-run-id", "--spot-db", "--spot-run-id"} & set(call)
    assert log[0].startswith("==== ")
    assert 'carry signal ETH (handoff "contrib/carry/data/handoff-ETH.json"' in log[0]
    equal = "the legs are sized as equal capital"
    assert f"==== PERP_RUN_ID is empty: the perp equity is not read; {equal}" in log
    assert f"==== SPOT_RUN_ID is empty: the spot equity is not read; {equal}" in log
    assert log[-1] == "==== exit 0"


def test_a_run_with_the_runs_named_reads_both_stores_and_passes_the_rules_flags(
    tmp_path, monkeypatch
):
    extra = (
        'PERP_DB="/data/carry/paper_trading.db"\nPERP_RUN_ID="carry-ETH-1"\n'
        'SPOT_DB="/data/uniswap/paper.db"\nSPOT_RUN_ID="paper-carry-1"\n'
        'SIGNAL_ARGS="--margin-pct 30 --z-in 1.5"\n'
    )
    code, calls, log = _run(tmp_path, monkeypatch, extra=extra)
    assert code == 0
    (call,) = calls
    assert call[call.index("--perp-db") + 1] == "/data/carry/paper_trading.db"
    assert call[call.index("--perp-run-id") + 1] == "carry-ETH-1"
    assert call[call.index("--spot-db") + 1] == "/data/uniswap/paper.db"
    assert call[call.index("--spot-run-id") + 1] == "paper-carry-1"
    assert call[-4:] == ["--margin-pct", "30", "--z-in", "1.5"]
    assert not any("is empty" in line for line in log)


def test_the_commands_exit_code_is_the_runs(tmp_path, monkeypatch):
    code, calls, log = _run(tmp_path, monkeypatch, exit_code=1)
    assert code == 1 and len(calls) == 1
    assert log[-1] == "==== exit 1"


def test_a_python_path_that_is_not_there_exits_4_before_running(tmp_path, monkeypatch):
    code, calls, log = _run(tmp_path, monkeypatch, python="/nowhere/python")
    assert code == 4 and calls == []
    assert log[1] == '==== there is no "/nowhere/python": fix PYTHON in carry-signal.local.sh'
    assert log[2] == "==== exit 4"


def test_a_log_that_cannot_be_written_exits_4_and_runs_nothing(tmp_path, monkeypatch):
    if _SH is None:
        pytest.skip("no sh on this machine")
    if os.name == "nt" or getattr(os, "geteuid", lambda: 1)() == 0:
        pytest.skip("the owner, or root, writes a file with the write bit off")
    schedule = tmp_path / "contrib" / "carry" / "schedule"
    schedule.mkdir(parents=True)
    script = schedule / "carry-signal.sh"
    script.write_bytes(_SCRIPT.read_bytes())
    log = tmp_path / "signal.log"
    log.write_text("", encoding="ascii")
    (schedule / "carry-signal.local.sh").write_text("LOG=signal.log\n", encoding="ascii")
    asked = tmp_path / "asked.txt"
    monkeypatch.setenv("STUB_LOG", asked.as_posix())
    log.chmod(0o444)
    try:
        done = subprocess.run([_SH, str(script)], check=False)
    finally:
        log.chmod(0o644)
    assert done.returncode == 4
    assert log.read_text(encoding="ascii") == "" and not asked.exists()


@pytest.mark.parametrize(
    ("extra", "said"),
    [
        ('PERP_RUN_ID="carry-ETH-1"\n', "PERP_DB and PERP_RUN_ID go together, one is empty"),
        (
            'PERP_DB="/data/carry/paper_trading.db"\n',
            "PERP_DB and PERP_RUN_ID go together, one is empty",
        ),
        ('SPOT_RUN_ID="paper-carry-1"\n', "SPOT_DB and SPOT_RUN_ID go together, one is empty"),
    ],
)
def test_a_run_id_without_its_store_is_a_settings_error_said_before_running(
    tmp_path, monkeypatch, extra, said
):
    # Half of a pair would reach the command as --perp-db "" and fail at 23:50
    # as "no such file"; the script names the setting instead and runs nothing.
    code, calls, log = _run(tmp_path, monkeypatch, extra=extra)
    assert code == 4 and calls == []
    assert f"==== {said}: fix carry-signal.local.sh" in log
    assert log[-1] == "==== exit 4"
