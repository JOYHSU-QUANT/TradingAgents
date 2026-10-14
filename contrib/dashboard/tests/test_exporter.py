"""One tick end to end: the files, the letters, the state, the ping — and what they never contain."""

from __future__ import annotations

import json
import os
import re
from datetime import time, timedelta
from pathlib import Path

import pytest

from contrib.dashboard.alerts import StateError
from contrib.dashboard.config import Settings
from contrib.dashboard.exporter import STATE_FILE, read_and_judge, run_once, summary_line
from contrib.dashboard.readers import CommandResult

from .support import (
    HL_KEY,
    NOW,
    UNITS_QUIET,
    FakeRunner,
    build_config,
    hl_instant,
    host_reader_for,
    quiet_documents,
    unit_text,
)
from .test_mail import SETTINGS, FakeSMTP, HalfRefusingSMTP, RefusingSMTP

OFF = Settings(mail=None, healthchecks_url=None)
ON = Settings(mail=SETTINGS, healthchecks_url="https://hc-ping.com/abc")


class FakeResponse:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return b"OK"


class FakeUrlopen:
    def __init__(self, fail: bool = False) -> None:
        self.urls: list[str] = []
        self.fail = fail

    def __call__(self, url, timeout):
        self.urls.append(url)
        if self.fail:
            raise OSError("unreachable")
        return FakeResponse()


def _run(
    tmp_path,
    documents,
    settings=OFF,
    *,
    now=NOW,
    units=UNITS_QUIET,
    smtp=FakeSMTP,
    urlopen=None,
    config=None,
):
    config = config or build_config(tmp_path)
    runner = FakeRunner(documents, units=units)
    report = run_once(
        config,
        settings,
        now=now,
        runner=runner,
        smtp_factory=smtp,
        urlopen=urlopen or FakeUrlopen(),
        host_reader=host_reader_for(tmp_path),
    )
    return config, report, runner


def _json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _subjects():
    return [m["Subject"] for s in FakeSMTP.instances for m in s.sent]


def _digests():
    return [subject for subject in _subjects() if "daily digest" in subject]


def test_a_quiet_first_tick_writes_every_document_and_mails_only_the_digest(tmp_path):
    FakeSMTP.instances.clear()
    config, report, runner = _run(tmp_path, quiet_documents(), ON)
    status = config.status_dir
    assert sorted(p.name for p in status.iterdir()) == [
        "alerts-state.json",
        "alerts.json",
        "carry-perp.json",
        "carry.json",
        "exporter.json",
        "hl-paper.json",
        "host.json",
        "uniswap-ai.json",
        "uniswap-carry.json",
        "uniswap-trend.json",
    ]
    assert _json(status / "hl-paper.json") == quiet_documents()[HL_KEY]
    assert (status / "hl-paper.json").read_text(encoding="utf-8").endswith("}\n")
    host = _json(status / "host.json")
    assert host["source"] == "dashboard" and host["hostname"] == "tokyo"
    assert [u["unit"] for u in host["units"]] == list(config.units())
    alerts = _json(status / "alerts.json")
    assert alerts["active"] == [] and alerts["recent_events"] == []
    assert alerts["pending_letters"] == 0
    exporter = _json(status / "exporter.json")
    assert [line["ok"] for line in exporter["lines"]] == [True] * 6
    assert exporter["lines"][0]["document_generated_at"] == hl_instant(NOW)
    assert exporter["mail"] == {
        "enabled": True,
        "sent": 0,
        "digest_sent": True,
        "pending": 0,
        "error": None,
    }
    assert exporter["deadman"] == {"enabled": True, "ping": "ok", "error": None}
    assert isinstance(exporter["duration_ms"], int) and exporter["duration_ms"] >= 0
    state = _json(status / STATE_FILE)
    assert state["events_seen"] == {
        "hl-paper": "2026-10-14T04:50:00+00:00",
        "carry-perp": "2026-10-14T04:50:00+00:00",
    }
    assert state["last_digest_day"] == "2026-10-14"
    assert len(runner.line_calls()) == 6
    # 05:00Z is past the 03:30Z digest hour: the day's digest went out, and only it.
    assert _subjects() == ["[trading] daily digest 2026-10-14"]
    assert report.digest_sent and report.mailed == 0 and report.raised == ()
    assert summary_line(report) == (
        "2026-10-14T05:00:00+00:00: read 6/6 line(s); 0 condition(s) hold, 0 raised, 0 cleared, "
        "0 event(s); mail sent 0, pending 0; digest sent; ping ok"
    )


def test_the_digest_goes_out_once_a_day_at_its_hour(tmp_path):
    FakeSMTP.instances.clear()
    early = NOW.replace(hour=3, minute=28)
    _run(tmp_path, quiet_documents(), ON, now=early)
    assert FakeSMTP.instances == []
    _run(tmp_path, quiet_documents(), ON, now=early + timedelta(minutes=2))
    assert _subjects() == ["[trading] daily digest 2026-10-14"]
    _run(tmp_path, quiet_documents(), ON, now=early + timedelta(minutes=4))
    assert _digests() == ["[trading] daily digest 2026-10-14"]
    # The next day (the quiet documents are a day old by then: their raised
    # letter goes out too, which is not the digest).
    _run(tmp_path, quiet_documents(), ON, now=early + timedelta(days=1, minutes=2))
    assert _digests() == ["[trading] daily digest 2026-10-14", "[trading] daily digest 2026-10-15"]
    late_config = build_config(tmp_path, digest_utc=time(23, 0))
    _run(tmp_path, quiet_documents(), ON, now=early + timedelta(days=2), config=late_config)
    assert len(_digests()) == 2


def test_a_change_of_state_is_mailed_once_and_its_clearing_once(tmp_path):
    FakeSMTP.instances.clear()
    one = NOW.replace(hour=1)
    _run(tmp_path, quiet_documents(), ON, now=one)
    down = dict(UNITS_QUIET, **{"hl-paper.service": unit_text(active="inactive", sub="dead")})
    _, report, _ = _run(tmp_path, quiet_documents(), ON, now=one.replace(minute=2), units=down)
    assert report.raised == ("unit:hl-paper.service:down",) and report.mailed == 1
    _, report, _ = _run(tmp_path, quiet_documents(), ON, now=one.replace(minute=4), units=down)
    assert report.raised == () and report.mailed == 0 and len(report.evaluation.conditions) == 1
    _, report, _ = _run(tmp_path, quiet_documents(), ON, now=one.replace(minute=6))
    assert report.cleared == ("unit:hl-paper.service:down",) and report.mailed == 1
    assert _subjects() == [
        "[trading] 1 raised: hl-paper.service down",
        "[trading] 1 cleared: hl-paper.service down",
    ]
    body = FakeSMTP.instances[0].sent[0].get_content()
    assert (
        "RAISED  2026-10-14 01:02Z  hl-paper.service: hl-paper.service is inactive (dead; result "
        "success); last started Mon 2026-10-12 05:21:10 UTC"
    ) in body


def test_a_letter_that_cannot_be_sent_stays_pending_and_the_watchdog_hears_fail(tmp_path):
    FakeSMTP.instances.clear()
    one = NOW.replace(hour=1)
    down = dict(UNITS_QUIET, **{"hl-paper.service": unit_text(active="inactive", sub="dead")})
    pings = FakeUrlopen()
    config, report, _ = _run(
        tmp_path, quiet_documents(), ON, now=one, units=down, smtp=RefusingSMTP, urlopen=pings
    )
    assert report.mailed == 0 and report.pending == 1
    assert report.mail_error.startswith("SMTPAuthenticationError")
    assert "app-pass" not in report.mail_error
    assert report.ping == "fail" and pings.urls == ["https://hc-ping.com/abc/fail"]
    assert _json(config.status_dir / STATE_FILE)["pending"][0]["kind"] == "raised"
    assert _json(config.status_dir / "alerts.json")["pending_letters"] == 1
    assert _json(config.status_dir / "exporter.json")["mail"]["pending"] == 1
    assert "(send failed)" in summary_line(report)
    # The account is back: the same letter goes out, once, and the ping is plain.
    _, report, _ = _run(
        tmp_path, quiet_documents(), ON, now=one.replace(minute=2), units=down, urlopen=pings
    )
    assert report.mailed == 1 and report.pending == 0 and report.ping == "ok"
    assert pings.urls[-1] == "https://hc-ping.com/abc"
    # The refusing account was asked first (an instance that sent nothing), then this one.
    sent = [m for s in FakeSMTP.instances for m in s.sent]
    assert len(sent) == 1 and "RAISED  2026-10-14 01:00Z" in sent[0].get_content()


def test_a_ping_that_fails_is_reported_not_raised(tmp_path):
    pings = FakeUrlopen(fail=True)
    config, report, _ = _run(tmp_path, quiet_documents(), ON, urlopen=pings)
    assert report.ping == "ok" and report.ping_error == "OSError: unreachable"
    assert _json(config.status_dir / "exporter.json")["deadman"]["error"] == "OSError: unreachable"
    assert summary_line(report).endswith("ping ok (failed)")


def test_without_a_mail_account_the_letter_lines_are_dropped_and_nothing_is_pinged(tmp_path):
    down = dict(UNITS_QUIET, **{"hl-paper.service": unit_text(active="inactive", sub="dead")})
    pings = FakeUrlopen()
    config, report, _ = _run(tmp_path, quiet_documents(), OFF, units=down, urlopen=pings)
    assert report.raised == ("unit:hl-paper.service:down",) and not report.mail_enabled
    assert report.pending == 0 and report.ping is None and pings.urls == []
    assert _json(config.status_dir / STATE_FILE)["pending"] == []
    assert _json(config.status_dir / STATE_FILE)["last_digest_day"] is None
    assert summary_line(report).endswith("mail off; digest no; ping off")


def test_a_line_that_fails_keeps_its_last_good_document_on_disk(tmp_path):
    config, _, _ = _run(tmp_path, quiet_documents())
    before = (config.status_dir / "hl-paper.json").read_text(encoding="utf-8")
    docs = quiet_documents()
    docs[HL_KEY] = CommandResult(5, "", "error: malformed database\n")
    _, report, _ = _run(tmp_path, docs, now=NOW + timedelta(minutes=2))
    assert (config.status_dir / "hl-paper.json").read_text(encoding="utf-8") == before
    assert "hl-paper.json" not in report.written and "carry.json" in report.written
    line = _json(config.status_dir / "exporter.json")["lines"][0]
    assert (line["ok"], line["exit_code"], line["failure"], line["stderr_line"]) == (
        False,
        5,
        "exit 5",
        "error: malformed database",
    )
    assert report.raised == ("hl-paper:store_damaged",)


def test_a_state_file_this_build_cannot_read_refuses_the_tick_before_anything_is_read(tmp_path):
    config = build_config(tmp_path)
    config.status_dir.mkdir()
    (config.status_dir / STATE_FILE).write_text("{}", encoding="utf-8")
    runner = FakeRunner(quiet_documents())
    with pytest.raises(StateError):
        run_once(config, OFF, now=NOW, runner=runner, host_reader=host_reader_for(tmp_path))
    assert runner.calls == []


def test_read_and_judge_writes_nothing(tmp_path):
    config = build_config(tmp_path)
    runner = FakeRunner(quiet_documents())
    reads, host, evaluation = read_and_judge(
        config, now=NOW, runner=runner, host_reader=host_reader_for(tmp_path, meminfo_text="")
    )
    assert len(reads) == 6 and evaluation.conditions == () and host.memory is None
    assert not config.status_dir.exists()


def test_the_documents_name_no_secret_field_and_carry_no_hex_key(tmp_path):
    config, _, _ = _run(tmp_path, quiet_documents(), ON)
    forbidden = re.compile(r"key|secret|password|mnemonic", re.IGNORECASE)
    for name in ("host.json", "alerts.json", "exporter.json", STATE_FILE):
        document = _json(config.status_dir / name)
        keys = _keys_of(document)
        assert not [k for k in keys if forbidden.search(k)], (name, keys)
        text = (config.status_dir / name).read_text(encoding="utf-8")
        assert not re.search(r"0x[0-9a-fA-F]{64}", text), name
        assert text.isascii(), name


def _keys_of(value) -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for k, v in value.items():
            found.append(k)
            found += _keys_of(v)
    elif isinstance(value, list):
        for v in value:
            found += _keys_of(v)
    return found


@pytest.mark.skipif(os.name != "posix", reason="file modes are POSIX")
def test_every_file_written_is_mode_0640(tmp_path):
    config, _, _ = _run(tmp_path, quiet_documents())
    for path in config.status_dir.iterdir():
        assert (path.stat().st_mode & 0o777) == 0o640, path


def test_dump_json_refuses_what_is_not_a_document():
    from contrib.dashboard.files import dump_json

    assert dump_json({"a": 1}) == '{\n  "a": 1\n}\n'
    with pytest.raises(TypeError, match="carries no set"):
        dump_json({"a": {1, 2}})


def test_a_recipient_the_server_refused_is_a_mail_error_but_the_letter_is_not_resent(tmp_path):
    FakeSMTP.instances.clear()
    down = dict(UNITS_QUIET, **{"hl-paper.service": unit_text(active="inactive", sub="dead")})
    pings = FakeUrlopen()
    one = NOW.replace(hour=1)
    config, report, _ = _run(
        tmp_path, quiet_documents(), ON, now=one, units=down, smtp=HalfRefusingSMTP, urlopen=pings
    )
    assert report.mailed == 1 and report.pending == 0
    assert report.mail_error == "1 recipient(s) refused"
    assert report.ping == "fail" and pings.urls == ["https://hc-ping.com/abc/fail"]
    assert _json(config.status_dir / "exporter.json")["mail"]["error"] == "1 recipient(s) refused"
    _, report, _ = _run(tmp_path, quiet_documents(), ON, now=one.replace(minute=2), units=down)
    assert report.mailed == 0 and report.mail_error is None


def test_a_naive_clock_is_refused_before_anything_is_read(tmp_path):
    config = build_config(tmp_path)
    runner = FakeRunner(quiet_documents())
    with pytest.raises(ValueError, match="aware"):
        run_once(
            config,
            OFF,
            now=NOW.replace(tzinfo=None),
            runner=runner,
            host_reader=host_reader_for(tmp_path),
        )
    assert runner.calls == []
