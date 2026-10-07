"""The shipped schedules, Windows and Linux: when they visit, and what each visit runs."""

from __future__ import annotations

import shutil
import subprocess
import xml.etree.ElementTree as ElementTree
from datetime import datetime
from pathlib import Path

import pytest

from contrib.uniswap_v3.config import load_config

_PACKAGE = Path(__file__).resolve().parents[1]
_TASK = _PACKAGE / "schedule" / "paper-visit.xml"
_VISIT = _PACKAGE / "schedule" / "paper-visit.cmd"
_EXAMPLE = _PACKAGE / "configs" / "uniswap_v3.example.yaml"
_NS = {"task": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
# Mainnet makes a block every twelve seconds.
_BLOCK_SECONDS = 12


def _task() -> ElementTree.Element:
    return ElementTree.parse(_TASK).getroot()


def _visit_times() -> list[int]:
    """The seconds after 00:00 UTC each trigger fires at, in order."""
    times = []
    for trigger in _task().iterfind("task:Triggers/task:CalendarTrigger", _NS):
        start = trigger.findtext("task:StartBoundary", namespaces=_NS)
        assert start is not None and start.endswith("Z"), "a trigger's time is in UTC"
        moment = datetime.fromisoformat(start.replace("Z", "+00:00"))
        assert trigger.findtext("task:ScheduleByDay/task:DaysInterval", namespaces=_NS) == "1"
        times.append(moment.hour * 3600 + moment.minute * 60 + moment.second)
    return times


def _visit_lines(script: Path = _VISIT) -> list[str]:
    return script.read_text(encoding="ascii").splitlines()


def _at(prefix: str, script: Path = _VISIT) -> int:
    """The index of the visit script's first line that starts with ``prefix``, indented or not."""
    lines = _visit_lines(script)
    return next(index for index, line in enumerate(lines) if line.lstrip().startswith(prefix))


def _limit_seconds() -> int:
    limit = _task().findtext("task:Settings/task:ExecutionTimeLimit", namespaces=_NS)
    assert limit is not None and limit.startswith("PT") and limit.endswith("M")
    return int(limit[2:-1]) * 60


def test_the_task_visits_three_times_a_day_after_the_bar_fills():
    times = _visit_times()
    assert times == [600, 2400, 4200]
    # The first visit comes after the fill block of the bar that closed at 00:00.
    delay_blocks = load_config(_EXAMPLE).execution.delay_blocks
    assert times[0] > (delay_blocks + 1) * _BLOCK_SECONDS


def test_a_visit_is_stopped_before_the_next_is_due_and_never_runs_beside_another():
    times = _visit_times()
    gaps = [later - earlier for earlier, later in zip(times, times[1:], strict=False)]
    assert _limit_seconds() < min(gaps)
    settings = _task().find("task:Settings", _NS)
    assert settings.findtext("task:MultipleInstancesPolicy", namespaces=_NS) == "IgnoreNew"
    assert settings.findtext("task:StartWhenAvailable", namespaces=_NS) == "true"


def test_the_task_runs_the_visit_script_under_the_repository_placeholder():
    command = _task().findtext("task:Actions/task:Exec/task:Command", namespaces=_NS)
    # Quoted: a repository's path may hold a space.
    assert command == r'"C:\path\to\TradingAgents\contrib\uniswap_v3\schedule\paper-visit.cmd"'


def test_a_visit_runs_paper_with_no_opening_balances():
    commands = [
        line
        for line in _VISIT.read_text(encoding="ascii").splitlines()
        if line.strip() and not line.lstrip().lower().startswith(("rem", "@echo"))
    ]
    paper = [line for line in commands if "-m contrib.uniswap_v3 paper" in line]
    assert len(paper) == 1
    assert "--balance" not in paper[0] and "--gas-eth" not in paper[0]
    assert "-m dotenv run --" in paper[0]
    assert paper[0].endswith('>>"%LOG%" 2>&1')
    # The visit's exit code is the script's, for Task Scheduler's last run result.
    assert commands[-2:] == ['>>"%LOG%" echo ==== exit %CODE%', "exit /b %CODE%"]


def test_a_visit_that_cannot_reach_the_repository_or_its_log_exits_4_and_runs_nothing():
    lines = _visit_lines()
    cd, header, paper = _at("cd /d "), _at('>>"%LOG%" echo ==== %DATE%'), _at('"%PYTHON%" -m')
    assert lines[cd].endswith("|| exit /b 4") and lines[header].endswith("|| exit /b 4")
    assert cd < header < paper
    # The log is checked again right before the visit: it may have stopped taking writes.
    assert lines[paper - 1] == '>>"%LOG%" (call ) || exit /b 4'


def test_a_visit_takes_its_settings_from_a_local_file_and_prints_unbuffered():
    # The local file is read after the defaults it overrides, and before they are used.
    defaults = [_at(f'set "{name}=') for name in ("RUN_ID", "CONFIG", "DB", "LOG", "PYTHON")]
    assert max(defaults) < _at('if exist "%~dp0paper-visit.local.cmd" call ') < _at("cd /d ")
    assert 'set "PYTHONUNBUFFERED=1"' in _visit_lines()


def test_a_visit_whose_python_path_is_not_there_exits_4_before_running():
    check = _at('if not "%PYTHON:\\=%"=="%PYTHON%" if not exist "%PYTHON%" (')
    lines = _visit_lines()
    assert _at('>>"%LOG%" echo ==== %DATE%') < check < _at('"%PYTHON%" -m')
    assert lines[check + 3].strip() == "exit /b 4"


def test_a_visit_echoes_its_settings_in_quotes():
    # Unquoted, a path's ")" (Program Files (x86)) would end the block it is echoed in,
    # and its "&" would start another command.
    echoes = [line for line in _visit_lines() if " echo " in line]
    for name in ("RUN_ID", "DB", "PYTHON"):
        uses = [line for line in echoes if f"%{name}%" in line]
        assert uses and all(f'"%{name}%"' in line for line in uses), name


# The Linux schedule: the visit script a systemd timer runs, and the units.

_VISIT_SH = _PACKAGE / "schedule" / "paper-visit.sh"
_INSTALL_SH = _PACKAGE / "schedule" / "lightsail-install.sh"
_SERVICE = _PACKAGE / "schedule" / "uniswap-v3-paper.service"
_TIMER = _PACKAGE / "schedule" / "uniswap-v3-paper.timer"
_SH = shutil.which("sh")


def _unit(path: Path) -> dict[str, list[str]]:
    """A unit file's keys to their values, in order; a key given twice has two."""
    values: dict[str, list[str]] = {}
    for line in path.read_text(encoding="ascii").splitlines():
        if line.startswith("#") or not line.strip() or line.startswith("["):
            continue
        key, _, value = line.partition("=")
        values.setdefault(key, []).append(value)
    return values


def _timer_times() -> list[int]:
    """The seconds after 00:00 UTC each OnCalendar fires at, in order."""
    times = []
    for when in _unit(_TIMER)["OnCalendar"]:
        date, clock, zone = when.split()
        assert date == "*-*-*" and zone == "UTC", when
        hour, minute, second = (int(part) for part in clock.split(":"))
        times.append(hour * 3600 + minute * 60 + second)
    return times


def test_the_timer_visits_three_times_a_day_after_the_bar_fills_and_catches_up():
    times = _timer_times()
    assert times == [600, 4200, 7800]
    delay_blocks = load_config(_EXAMPLE).execution.delay_blocks
    assert times[0] > (delay_blocks + 1) * _BLOCK_SECONDS
    timer = _unit(_TIMER)
    assert timer["Persistent"] == ["true"]
    assert timer["Unit"] == ["uniswap-v3-paper.service"]
    assert timer["WantedBy"] == ["timers.target"]


def test_the_service_is_stopped_before_the_next_visit_is_due_and_runs_the_script_as_trader():
    service = _unit(_SERVICE)
    times = _timer_times()
    gaps = [later - earlier for earlier, later in zip(times, times[1:], strict=False)]
    limit = service["TimeoutStartSec"][0]
    assert limit.endswith("min") and int(limit[:-3]) * 60 < min(gaps)
    assert service["Type"] == ["oneshot"]
    assert service["User"] == ["trader"]
    assert service["WorkingDirectory"] == ["/home/trader/uniswap-paper"]
    # Short of memory, the kernel takes the visit, not the paper daemon beside it.
    assert service["OOMScoreAdjust"] == ["500"]
    assert service["ExecStart"] == [
        "/home/trader/uniswap-paper/contrib/uniswap_v3/schedule/paper-visit.sh"
    ]


def test_the_linux_visit_takes_its_settings_from_a_local_file_before_using_them():
    names = ("RUN_TREND", "RUN_AI", "CONFIG_TREND", "CONFIG_AI", "DB", "LOG", "PYTHON")
    defaults = [_at(f"{name}=", _VISIT_SH) for name in names]
    reads = 'if [ -f "$here/paper-visit.local.sh" ]; then . "$here/paper-visit.local.sh"'
    local = _at(reads, _VISIT_SH)
    root = _at('cd "$here/../../.."', _VISIT_SH)
    assert max(defaults) < local < root
    lines = _visit_lines(_VISIT_SH)
    assert lines[root].endswith("|| exit 4")
    assert "export PYTHONUNBUFFERED=1" in lines
    assert lines[_at('"$PYTHON" -m dotenv run --', _VISIT_SH)].endswith('>>"$LOG" 2>&1')


def test_the_installer_waits_for_a_running_visit_by_its_state_not_by_is_active():
    # A oneshot service that is running is "activating", which is-active exits 3 on.
    lines = [line.strip() for line in _visit_lines(_INSTALL_SH)]
    state = next(index for index, line in enumerate(lines) if "ActiveState" in line)
    assert lines[state].startswith('case "$(systemctl show -p ActiveState --value')
    assert lines[state + 1] == "active | activating)"
    assert "return 3" in lines[state + 1 : state + 6]
    # The timer's state is read with is-active (it is no oneshot); the service's never is.
    service = [line for line in lines if "is-active" in line and "$UNIT.service" in line]
    assert service == []


def test_the_installer_is_parsed_whole_before_it_runs():
    # The upgrade rewrites the checkout, the installer with it: the whole run is one
    # function call at the very end, read before any of it executes.
    lines = [line.strip() for line in _visit_lines(_INSTALL_SH) if line.strip()]
    assert lines[-2:] == ['main "$@"', "exit"]
    assert lines.index("main() {") > lines.index("as_trader() {")


def test_the_shell_scripts_parse():
    if _SH is None:
        pytest.skip("no sh on this machine")
    for script in (_VISIT_SH, _INSTALL_SH):
        subprocess.run([_SH, "-n", str(script)], check=True)


# A python that records what it is asked, one line per call, and exits as told
# for the command (the word after contrib.uniswap_v3).
_STUB = """#!/bin/sh
printf '%s\\n' "$*" >>"$STUB_LOG"
case "$*" in
    *" contrib.uniswap_v3 paper "*) exit "${EXIT_PAPER:-0}" ;;
    *" contrib.uniswap_v3 backfill "*) exit "${EXIT_BACKFILL:-0}" ;;
    *" contrib.uniswap_v3 verdict "*) exit "${EXIT_VERDICT:-0}" ;;
esac
exit 0
"""


def _visit(
    tmp_path: Path, monkeypatch, python: str | None = None, extra: str = "", **exits: int
) -> tuple[int, list[list[str]], list[str]]:
    """Run a copy of the Linux visit script, laid out as in the repository, with a stub python.

    The local settings name ``python`` (the stub, by default), the log and a
    store, and then ``extra``, more lines of the file. Returns the exit code,
    the words of each call the stub was asked in order, and the log's lines.
    """
    if _SH is None:
        pytest.skip("no sh on this machine")
    schedule = tmp_path / "contrib" / "uniswap_v3" / "schedule"
    schedule.mkdir(parents=True)
    script = schedule / "paper-visit.sh"
    script.write_bytes(_VISIT_SH.read_bytes())
    stub = tmp_path / "python"
    stub.write_text(_STUB, encoding="ascii")
    stub.chmod(0o755)
    # Quoted, as a value with a space in it has to be: the file is sourced.
    settings = f'PYTHON="{stub.as_posix() if python is None else python}"\n'
    settings += "LOG=visits.log\nDB=store.db\n" + extra
    (schedule / "paper-visit.local.sh").write_text(settings, encoding="ascii")
    asked = tmp_path / "asked.txt"
    monkeypatch.setenv("STUB_LOG", asked.as_posix())
    for name, code in exits.items():
        monkeypatch.setenv(name, str(code))
    done = subprocess.run([_SH, str(script)], check=False)
    calls = []
    if asked.is_file():
        calls = [line.split() for line in asked.read_text(encoding="ascii").splitlines()]
    log = (tmp_path / "visits.log").read_text(encoding="ascii").splitlines()
    return done.returncode, calls, log


def _commands(calls: list[list[str]]) -> list[str]:
    return [words[words.index("contrib.uniswap_v3") + 1] for words in calls]


def test_a_linux_visit_runs_the_control_run_then_backfill_verdict_and_the_ai_run(
    tmp_path, monkeypatch
):
    code, calls, log = _visit(tmp_path, monkeypatch)
    assert code == 0
    assert _commands(calls) == ["paper", "backfill", "verdict", "paper"]
    trend, backfill, verdict, ai = calls
    assert trend[trend.index("--run-id") + 1] == "paper-trend-1"
    assert trend[trend.index("--config") + 1].endswith("paper-trend.local.yaml")
    assert ai[ai.index("--run-id") + 1] == "paper-ai-1"
    for call in (backfill, verdict, ai):
        assert call[call.index("--config") + 1].endswith("paper-ai.local.yaml")
    assert backfill[backfill.index("--from") + 1].count("-") == 2
    for call in calls:
        assert "--balance" not in call and "--gas-eth" not in call
        assert call[:3] == ["-m", "dotenv", "run"] and "store.db" in call
    assert log[0].startswith("==== ") and 'visit of "paper-trend-1" and "paper-ai-1"' in log[0]
    assert '(db "store.db", python "' in log[0]
    assert log[-1] == "==== exit 0"


@pytest.mark.parametrize(
    ("exits", "code", "commands"),
    [
        # The control run was visited; the AI run was not, its verdicts not being there.
        ({"EXIT_VERDICT": 3}, 3, ["paper", "backfill", "verdict"]),
        ({"EXIT_BACKFILL": 3}, 3, ["paper", "backfill"]),
        ({"EXIT_PAPER": 1}, 1, ["paper"]),
    ],
)
def test_a_linux_visit_stops_at_the_first_step_that_fails_and_exits_as_it_did(
    tmp_path, monkeypatch, exits, code, commands
):
    exited, calls, log = _visit(tmp_path, monkeypatch, **exits)
    assert exited == code
    assert _commands(calls) == commands
    assert log[-1] == f"==== exit {code}"


@pytest.mark.parametrize(
    ("extra", "code", "commands", "said"),
    [
        ('RUN_TREND=""\n', 0, ["backfill", "verdict", "paper"], "RUN_TREND is empty: the control run is left out"),
        ('RUN_AI=""\n', 0, ["paper"], "RUN_AI is empty: backfill, verdict and the AI run are left out"),
        ('RUN_TREND=""\nRUN_AI=""\n', 1, [], "RUN_TREND and RUN_AI are both empty: nothing to visit; fix paper-visit.local.sh"),
    ],
)  # fmt: skip
def test_a_linux_visit_leaves_out_the_steps_of_an_empty_run_id_and_says_so(
    tmp_path, monkeypatch, extra, code, commands, said
):
    exited, calls, log = _visit(tmp_path, monkeypatch, extra=extra)
    assert exited == code
    assert _commands(calls) == commands
    assert f"==== {said}" in log and log[-1] == f"==== exit {code}"


def test_a_linux_visit_whose_python_path_is_not_there_exits_4_before_running(
    tmp_path, monkeypatch
):
    code, calls, log = _visit(tmp_path, monkeypatch, python="/nowhere/python")
    assert code == 4 and calls == []
    assert log[1] == '==== there is no "/nowhere/python": fix PYTHON in paper-visit.local.sh'
    assert log[2] == "==== exit 4"
