"""The host facts: units, commits, disk, memory, journal — each ``None`` when it cannot be read."""

from __future__ import annotations

from contrib.dashboard.host import (
    HostFacts,
    read_commit,
    read_disk,
    read_host,
    read_journal_errors,
    read_memory,
    read_unit,
)
from contrib.dashboard.readers import CommandResult

from .support import MEMINFO, NOW, FakeRunner, build_config, quiet_documents, unit_text


def test_a_unit_is_read_property_by_property():
    runner = FakeRunner({}, units={"hl-paper.service": unit_text(restarts=2, status=1)})
    unit = read_unit("hl-paper.service", runner=runner)
    assert (unit.type, unit.load_state, unit.active_state, unit.sub_state) == (
        "simple",
        "loaded",
        "active",
        "running",
    )
    assert (unit.result, unit.n_restarts, unit.exec_main_status) == ("success", 2, 1)
    assert unit.exec_main_start == "Mon 2026-10-12 05:21:10 UTC"
    assert unit.exec_main_exit is None and unit.error is None
    argv = runner.calls[0][0]
    assert argv[:3] == ["systemctl", "show", "hl-paper.service"]
    assert argv[3].startswith("--property=Type,LoadState,ActiveState")


def test_a_unit_not_installed_and_a_systemctl_that_fails_are_told_apart():
    missing = read_unit("ghost.service", runner=FakeRunner({}, units={}))
    assert missing.load_state == "not-found" and missing.error is None and missing.type is None

    def refusing(argv, cwd, timeout):
        return CommandResult(1, "", "Failed to connect to bus: No such file or directory\n")

    failed = read_unit("hl-paper.service", runner=refusing)
    assert failed.error == "Failed to connect to bus: No such file or directory"
    assert failed.active_state is None

    def timing_out(argv, cwd, timeout):
        return CommandResult(None, "", "")

    assert read_unit("x.service", runner=timing_out).error == "timed out"


def test_the_commit_is_read_or_none(tmp_path):
    assert read_commit(tmp_path, runner=FakeRunner({})).commit == "b17efda8"
    assert read_commit(tmp_path, runner=FakeRunner({}, commit=None)).commit is None


def test_memory_is_read_from_a_meminfo_shaped_file(tmp_path):
    meminfo = tmp_path / "meminfo"
    meminfo.write_text(MEMINFO, encoding="ascii")
    facts = read_memory(meminfo)
    assert (facts.total_kb, facts.available_kb) == (2003876, 1300000)
    assert read_memory(tmp_path / "absent") is None
    meminfo.write_text("MemTotal: 1 kB\n", encoding="ascii")
    assert read_memory(meminfo) is None


def test_disk_is_read_for_an_existing_path_and_none_for_a_missing_one(tmp_path):
    facts = read_disk(tmp_path)
    assert facts is not None and facts.total_bytes > 0 and 0 <= facts.used_pct <= 100
    assert read_disk(tmp_path / "absent" / "deeper") is None


def test_journal_errors_are_counted_or_none_when_refused():
    runner = FakeRunner({}, journal={"hl-paper.service": 3, "other.service": None})
    assert read_journal_errors("hl-paper.service", runner=runner) == 3
    assert read_journal_errors("other.service", runner=runner) is None
    argv = runner.calls[0][0]
    assert argv[:5] == ["journalctl", "-u", "hl-paper.service", "-p", "err"]
    assert "-q" not in argv

    def no_entries(argv, cwd, timeout):
        return CommandResult(0, "-- No entries --\n", "")

    assert read_journal_errors("hl-paper.service", runner=no_entries) == 0


def test_read_host_assembles_every_unit_and_checkout_the_config_names(tmp_path):
    config = build_config(tmp_path)
    meminfo = tmp_path / "meminfo"
    meminfo.write_text(MEMINFO, encoding="ascii")
    runner = FakeRunner(quiet_documents(), journal={"hl-paper.service": 1})
    host = read_host(
        config,
        now=NOW,
        runner=runner,
        disk_path=tmp_path,
        meminfo=meminfo,
        hostname=lambda: "tokyo",
    )
    assert isinstance(host, HostFacts) and host.hostname == "tokyo"
    assert host.generated_at == "2026-10-14T05:00:00+00:00"
    assert tuple(unit.unit for unit in host.units) == config.units()
    assert host.unit("hl-paper.service").active_state == "active"
    assert host.unit("uniswap-v3-paper.service").type == "oneshot"
    assert host.unit("absent.service") is None
    assert [c.path for c in host.checkouts] == [str(p) for p in config.checkouts()]
    assert host.journal_errors_24h == {
        "carry-signal.service": None,
        "hl-paper-carry-eth.service": None,
        "hl-paper.service": 1,
        "uniswap-v3-paper.service": None,
    }
    assert host.memory.available_kb == 1300000 and host.disk is not None
    assert not runner.line_calls()


def test_a_journal_that_answers_nothing_but_a_hint_is_not_a_count_of_zero():
    def outsider(argv, cwd, timeout):
        return CommandResult(
            0, "", "Hint: You are currently not seeing messages from other users\n"
        )

    assert read_journal_errors("hl-paper.service", runner=outsider) is None

    def quiet(argv, cwd, timeout):
        return CommandResult(0, "", "")

    assert read_journal_errors("hl-paper.service", runner=quiet) == 0


def test_host_commands_run_from_a_directory_that_is_always_there_not_a_checkout(tmp_path):
    from contrib.dashboard.host import HOST_CWD

    config = build_config(tmp_path)
    runner = FakeRunner(quiet_documents())
    read_host(config, now=NOW, runner=runner, disk_path=tmp_path, meminfo=tmp_path / "none")
    host_calls = [
        (argv[0], cwd) for argv, cwd, _ in runner.calls if argv[0] in ("systemctl", "journalctl")
    ]
    assert host_calls and all(cwd == HOST_CWD for _, cwd in host_calls)
    assert HOST_CWD not in config.checkouts()
