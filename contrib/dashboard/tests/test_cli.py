"""``python -m contrib.dashboard``: the three commands and the exit codes."""

from __future__ import annotations

import json
from pathlib import Path

from contrib.dashboard.cli import main
from contrib.dashboard.readers import CommandResult

from .support import HL_KEY, NOW, UNITS_QUIET, FakeRunner, quiet_documents, unit_text
from .test_mail import FakeSMTP, RefusingSMTP

ENVIRON_ON = {
    "DASHBOARD_SMTP_HOST": "smtp.example.com",
    "DASHBOARD_SMTP_USER": "bot@example.com",
    "DASHBOARD_SMTP_PASSWORD": "app-pass",
    "DASHBOARD_MAIL_FROM": "bot@example.com",
    "DASHBOARD_MAIL_TO": "op@example.com",
}


def _config(tmp_path: Path) -> Path:
    text = f"""
status_dir: status
lines:
  - kind: hyperliquid
    name: hl-paper
    python: /usr/bin/python3
    cwd: {tmp_path.as_posix()}/hl
    unit: hl-paper.service
    db: data/paper_trading.db
    run_id: paper-BTC-8
"""
    path = tmp_path / "exporter.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def _args(command: str, tmp_path: Path) -> list[str]:
    return [command, "--config", str(_config(tmp_path)), "--env-file", str(tmp_path / "none.env")]


def test_export_prints_one_summary_line_and_exits_0(tmp_path, capsys):
    runner = FakeRunner(quiet_documents())
    code = main(_args("export", tmp_path), now=lambda: NOW, runner=runner, environ={})
    out, err = capsys.readouterr()
    assert code == 0 and err == ""
    assert out.startswith("2026-10-14T05:00:00+00:00: read 1/1 line(s); 0 condition(s) hold")
    assert (tmp_path / "status" / "hl-paper.json").exists()
    exporter = json.loads((tmp_path / "status" / "exporter.json").read_text(encoding="utf-8"))
    assert exporter["mail"]["enabled"] is False


def test_check_prints_what_holds_and_writes_nothing(tmp_path, capsys):
    units = dict(UNITS_QUIET, **{"hl-paper.service": unit_text(active="inactive", sub="dead")})
    runner = FakeRunner(quiet_documents(), units=units)
    code = main(_args("check", tmp_path), now=lambda: NOW, runner=runner, environ={})
    out, err = capsys.readouterr()
    assert code == 0 and err == ""
    assert out.splitlines()[0] == "2026-10-14 05:00Z: 1 condition(s) hold"
    assert out.splitlines()[1].startswith(
        "  unit:hl-paper.service:down: hl-paper.service is inactive"
    )
    assert out.splitlines()[-1] == "  units: hl-paper.service inactive"
    assert not (tmp_path / "status").exists()


def test_a_line_not_read_is_on_stderr_and_still_exit_0(tmp_path, capsys):
    runner = FakeRunner({HL_KEY: CommandResult(1, "", "error: database does not exist.\n")})
    code = main(_args("export", tmp_path), now=lambda: NOW, runner=runner, environ={})
    out, err = capsys.readouterr()
    assert code == 0
    assert err == "not read: hl-paper: exit 1 -- error: database does not exist.\n"
    assert "1 condition(s) hold, 1 raised" in out


def test_test_mail_sends_one_letter_or_says_there_is_no_account(tmp_path, capsys):
    FakeSMTP.instances.clear()
    args = _args("test-mail", tmp_path)
    assert main(args, now=lambda: NOW, environ={}) == 1
    assert "no mail account" in capsys.readouterr().err
    assert main(args, now=lambda: NOW, smtp_factory=FakeSMTP, environ=ENVIRON_ON) == 0
    assert capsys.readouterr().out == "sent a test letter to op@example.com\n"
    [smtp] = FakeSMTP.instances
    assert smtp.sent[0]["Subject"] == "[trading] test letter"


def test_a_bad_setting_is_exit_1_with_the_reason(tmp_path, capsys):
    path = tmp_path / "exporter.yaml"
    path.write_text("status_dir: s\nlines: []\n", encoding="utf-8")
    args = ["export", "--config", str(path), "--env-file", str(tmp_path / "none.env")]
    assert main(args, environ={}) == 1
    assert "lines must be a non-empty list" in capsys.readouterr().err
    assert main(_args("export", tmp_path), environ={"DASHBOARD_SMTP_HOST": "h"}) == 1
    assert "DASHBOARD_SMTP_USER" in capsys.readouterr().err


def test_export_exits_3_when_a_letter_could_not_be_sent(tmp_path, capsys):
    units = dict(UNITS_QUIET, **{"hl-paper.service": unit_text(active="inactive", sub="dead")})
    runner = FakeRunner(quiet_documents(), units=units)
    code = main(
        _args("export", tmp_path),
        now=lambda: NOW,
        runner=runner,
        smtp_factory=RefusingSMTP,
        environ=ENVIRON_ON,
    )
    out, err = capsys.readouterr()
    assert code == 3
    assert err.startswith("mail: SMTPAuthenticationError")
    assert "mail sent 0, pending 1 (send failed)" in out
