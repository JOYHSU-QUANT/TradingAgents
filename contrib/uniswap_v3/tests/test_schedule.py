"""The shipped Windows schedule: when it visits, and what each visit runs."""

from __future__ import annotations

import xml.etree.ElementTree as ElementTree
from datetime import datetime
from pathlib import Path

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
    assert command == r"C:\path\to\TradingAgents\contrib\uniswap_v3\schedule\paper-visit.cmd"


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
