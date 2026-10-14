"""Reading a line through its command: the document, or the reason there is none."""

from __future__ import annotations

import json
import sys

from contrib.dashboard.readers import (
    KNOWN_SCHEMA_VERSIONS,
    STDERR_LIMIT,
    CommandResult,
    masked_line,
    read_line,
    run_command,
)

from .support import (
    AGENT_KEY,
    HL_KEY,
    UNI_TREND_KEY,
    FakeRunner,
    build_config,
    hyperliquid_document,
)


def _hl_line(tmp_path):
    return build_config(tmp_path).lines[0]


def test_a_document_is_read_with_its_raw_text_and_the_command_the_line_builds(tmp_path):
    runner = FakeRunner({HL_KEY: hyperliquid_document()})
    read = read_line(_hl_line(tmp_path), runner=runner, timeout=60)
    assert read.ok and read.failure is None and read.exit_code == 0
    assert read.document["run_id"] == "paper-BTC-8"
    assert json.loads(read.raw) == read.document and read.raw.endswith("\n")
    assert read.kind == "hyperliquid" and read.run_label == "paper-BTC-8"
    argv, cwd, timeout = runner.calls[0]
    line = _hl_line(tmp_path)
    assert argv == line.argv() and cwd == line.cwd and timeout == 60


def test_a_non_zero_exit_is_the_failure_with_the_first_stderr_line_masked(tmp_path):
    stderr = f"error: the store refused {AGENT_KEY} badly\nsecond line\n"
    runner = FakeRunner({HL_KEY: CommandResult(5, "", stderr)})
    read = read_line(_hl_line(tmp_path), runner=runner, timeout=60)
    assert not read.ok and read.document is None and read.raw is None
    assert read.failure == "exit 5" and read.exit_code == 5
    assert read.stderr_line == f"error: the store refused {AGENT_KEY[:6]}...{AGENT_KEY[-4:]} badly"


def test_a_timeout_a_non_document_and_another_schema_are_named(tmp_path):
    line = _hl_line(tmp_path)
    timed_out = read_line(
        line, runner=FakeRunner({HL_KEY: CommandResult(None, "", "")}), timeout=120
    )
    assert timed_out.failure == "timed out after 120 s" and timed_out.exit_code is None
    prose = read_line(
        line, runner=FakeRunner({HL_KEY: CommandResult(0, "not json", "")}), timeout=1
    )
    assert prose.failure == "no JSON document on stdout"
    a_list = read_line(line, runner=FakeRunner({HL_KEY: CommandResult(0, "[1]", "")}), timeout=1)
    assert a_list.failure == "no JSON document on stdout"
    newer = read_line(
        line, runner=FakeRunner({HL_KEY: hyperliquid_document(schema_version=2)}), timeout=1
    )
    assert newer.failure == (
        "document is 'hyperliquid_perp' schema 2; this exporter reads 'hyperliquid_perp' schema 1"
    )
    other = read_line(
        line, runner=FakeRunner({HL_KEY: hyperliquid_document(source="uniswap_v3")}), timeout=1
    )
    assert other.failure.startswith("document is 'uniswap_v3' schema 1")


def test_a_uniswap_line_is_keyed_by_its_run_id_too(tmp_path):
    runner = FakeRunner({UNI_TREND_KEY: {"schema_version": 1, "source": "uniswap_v3", "run": None}})
    read = read_line(build_config(tmp_path).lines[2], runner=runner, timeout=1)
    assert read.ok and read.kind == "uniswap"


def test_masked_line_takes_the_first_non_empty_line_and_cuts_it():
    assert masked_line("") is None
    assert masked_line("\n\n  first  \nsecond") == "first"
    long = "x" * (STDERR_LIMIT + 10)
    assert len(masked_line(long)) == STDERR_LIMIT and masked_line(long).endswith("...")


def test_run_command_captures_a_real_process_and_names_a_missing_program(tmp_path):
    ok = run_command([sys.executable, "-c", "import sys; print('hi'); sys.exit(3)"], tmp_path, 60)
    assert (ok.exit_code, ok.stdout) == (3, "hi\n")
    gone = run_command([str(tmp_path / "no-such-python")], tmp_path, 60)
    assert gone.exit_code == 127 and gone.stdout == "" and gone.stderr


def test_run_command_gives_up_after_the_timeout(tmp_path):
    slow = run_command([sys.executable, "-c", "import time; time.sleep(5)"], tmp_path, 0.5)
    assert slow.exit_code is None


def test_the_known_schemas_are_the_three_sources():
    assert set(KNOWN_SCHEMA_VERSIONS) == {"hyperliquid_perp", "uniswap_v3", "carry"}
