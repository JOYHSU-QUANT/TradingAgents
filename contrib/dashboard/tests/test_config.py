"""The YAML and the environment: what is read, what is refused, and that no value is echoed."""

from __future__ import annotations

from datetime import time
from pathlib import Path

import pytest

from contrib.dashboard.config import (
    CarryLine,
    ConfigError,
    HyperliquidLine,
    MailSettings,
    Thresholds,
    UniswapLine,
    load_config,
    load_settings,
    read_env_file,
)

EXAMPLE = Path(__file__).resolve().parents[1] / "configs" / "exporter.example.yaml"

MINIMAL = """
status_dir: status
lines:
  - kind: hyperliquid
    name: hl
    python: python3
    cwd: /home/trader/TradingAgents
    unit: hl-paper.service
    db: /home/trader/data/paper_trading.db
    run_id: paper-BTC-8
"""


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "exporter.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_the_example_config_loads_with_the_six_lines_of_the_host():
    config = load_config(EXAMPLE)
    assert [type(line) for line in config.lines] == [
        HyperliquidLine,
        HyperliquidLine,
        UniswapLine,
        UniswapLine,
        UniswapLine,
        CarryLine,
    ]
    assert config.units() == (
        "carry-signal.service",
        "hl-paper-carry-eth.service",
        "hl-paper.service",
        "uniswap-v3-paper.service",
    )
    assert config.checkouts() == (
        Path("/home/trader/TradingAgents"),
        Path("/home/trader/uniswap-paper"),
    )
    assert config.digest_utc == time(3, 30)
    assert config.thresholds == Thresholds()
    assert config.lines[0].segment_marker == Path("/home/trader/data/SEGMENT_SWITCH_READY")
    assert config.lines[2].argv()[-1] == "--json"


def test_each_kind_builds_its_package_command_line():
    config = load_config(EXAMPLE)
    hl, _, uni, _, _, carry = config.lines
    assert hl.argv()[1:4] == ["-m", "contrib.hyperliquid_perp", "status"]
    assert hl.argv()[hl.argv().index("--run-id") + 1] == "paper-BTC-8"
    assert "--recent" in hl.argv()
    assert uni.argv()[1:4] == ["-m", "contrib.uniswap_v3", "status"]
    assert "--config" in uni.argv() and "--bars" in uni.argv()
    assert carry.argv()[1:4] == ["-m", "contrib.carry", "report"]
    assert carry.argv()[carry.argv().index("--handoff") + 1].endswith("handoff-ETH.json")
    assert "--payloads-dir" not in carry.argv()
    assert carry.argv()[carry.argv().index("--d5-days") + 1] == "3" and carry.d5_days == 3
    from contrib.dashboard.readers import run_label

    assert run_label(carry) == "carry-ETH-1+paper-carry-1" and run_label(hl) == "paper-BTC-8"


def test_a_relative_path_is_taken_from_the_yaml_directory_and_a_bare_program_from_path(tmp_path):
    config = load_config(_write(tmp_path, MINIMAL))
    assert config.status_dir == tmp_path / "status"
    assert config.lines[0].python == Path("python3")
    assert config.lines[0].db == Path("/home/trader/data/paper_trading.db")


@pytest.mark.parametrize(
    ("edit", "words"),
    [
        (lambda t: t.replace("    run_id: paper-BTC-8\n", ""), "missing run_id"),
        (lambda t: t + "    extra: 1\n", "unknown key\\(s\\) extra"),
        (lambda t: t.replace("kind: hyperliquid", "kind: binance"), "kind must be one of"),
        (lambda t: t.replace("name: hl", "name: host"), "is a file the exporter writes"),
        (lambda t: t.replace("name: hl", "name: ../hl"), "must be a file name"),
        (lambda t: t + "thresholds:\n  cycle_overdue_minutes: true\n", "integer of at least 1"),
        (lambda t: t + "thresholds:\n  export_stale_hours: 0\n", "positive number"),
        (lambda t: t + "thresholds:\n  handoff_due_utc: 7am\n", "HH:MM"),
        (lambda t: t + "thresholds:\n  banana: 1\n", "unknown key\\(s\\) banana"),
        (lambda t: t + "thresholds:\n  disk_used_pct: 850\n", "percentage, 1 to 100"),
        (lambda t: t.replace("name: hl", "name: hl-paper.service"), "cannot share a name"),
        (lambda t: t + "digest_utc: 25:00\n", "HH:MM"),
        (lambda t: t + 'subject_prefix: "a\\rb"\n', "one line of text"),
        (lambda t: t + "x: 1\n", "unknown key\\(s\\) x"),
    ],
)
def test_a_setting_that_cannot_be_used_is_refused_by_name(tmp_path, edit, words):
    with pytest.raises(ConfigError, match=words):
        load_config(_write(tmp_path, edit(MINIMAL)))


def test_two_lines_of_one_name_are_refused(tmp_path):
    text = MINIMAL + MINIMAL.split("lines:\n", 1)[1]
    with pytest.raises(ConfigError, match="name hl more than once"):
        load_config(_write(tmp_path, text))


def test_a_file_that_is_not_yaml_or_not_a_mapping_is_refused(tmp_path):
    with pytest.raises(ConfigError, match="not valid YAML"):
        load_config(_write(tmp_path, "status_dir: [unclosed"))
    with pytest.raises(ConfigError, match="must be a mapping"):
        load_config(_write(tmp_path, "- just\n- a list\n"))
    with pytest.raises(ConfigError, match="cannot be read"):
        load_config(tmp_path / "missing.yaml")
    with pytest.raises(ConfigError, match="non-empty list"):
        load_config(_write(tmp_path, "status_dir: s\nlines: []\n"))


# --- the environment -----------------------------------------------------------------


def test_the_env_file_gives_only_the_dashboard_names(tmp_path):
    env = tmp_path / ".env"
    env.write_text(
        "# the daemons' key\n"
        "OPENROUTER_API_KEY=sk-or-not-ours\n"
        "export DASHBOARD_SMTP_HOST=smtp.gmail.com\n"
        'DASHBOARD_SMTP_USER="bot@example.com"\n'
        "DASHBOARD_SMTP_PASSWORD='app pass word' \n"
        "DASHBOARD_MAIL_TO=a@example.com, b@example.com # two of them\n"
        "DASHBOARD_MAIL_FROM = bot@example.com\n"
        "not an assignment\n"
        "DASHBOARD-BAD=1\n",
        encoding="utf-8",
    )
    assert read_env_file(env) == {
        "DASHBOARD_SMTP_HOST": "smtp.gmail.com",
        "DASHBOARD_SMTP_USER": "bot@example.com",
        "DASHBOARD_SMTP_PASSWORD": "app pass word",
        "DASHBOARD_MAIL_TO": "a@example.com, b@example.com",
        "DASHBOARD_MAIL_FROM": "bot@example.com",
    }
    settings = load_settings(environ={}, env_file=env)
    assert settings.mail == MailSettings(
        host="smtp.gmail.com",
        port=587,
        user="bot@example.com",
        sender="bot@example.com",
        recipients=("a@example.com", "b@example.com"),
        password="app pass word",
    )
    assert settings.healthchecks_url is None
    assert "app pass word" not in repr(settings)


def test_the_process_environment_wins_over_the_file_and_a_missing_file_is_no_account(tmp_path):
    env = tmp_path / ".env"
    env.write_text("DASHBOARD_SMTP_HOST=from-file\n", encoding="utf-8")
    environ = {
        "DASHBOARD_SMTP_HOST": "from-env",
        "DASHBOARD_SMTP_USER": "u",
        "DASHBOARD_SMTP_PASSWORD": "p",
        "DASHBOARD_MAIL_FROM": "f@example.com",
        "DASHBOARD_MAIL_TO": "t@example.com",
        "DASHBOARD_SMTP_PORT": "2525",
        "DASHBOARD_HEALTHCHECKS_URL": "https://hc-ping.com/uuid",
    }
    settings = load_settings(environ=environ, env_file=env)
    assert settings.mail is not None
    assert (settings.mail.host, settings.mail.port) == ("from-env", 2525)
    assert settings.healthchecks_url == "https://hc-ping.com/uuid"
    off = load_settings(environ={}, env_file=tmp_path / "absent.env")
    assert off.mail is None and off.healthchecks_url is None


def test_a_half_account_is_refused_naming_the_missing_names_not_the_values():
    environ = {"DASHBOARD_SMTP_HOST": "smtp.example.com", "DASHBOARD_SMTP_PASSWORD": "s3cret"}
    with pytest.raises(ConfigError) as caught:
        load_settings(environ=environ, env_file=None)
    text = str(caught.value)
    assert "DASHBOARD_SMTP_USER" in text and "DASHBOARD_MAIL_TO" in text
    assert "s3cret" not in text and "smtp.example.com" not in text


@pytest.mark.parametrize(
    ("extra", "words"),
    [
        ({"DASHBOARD_SMTP_PORT": "70000"}, "port number"),
        ({"DASHBOARD_MAIL_TO": "nobody"}, "DASHBOARD_MAIL_TO must be"),
        ({"DASHBOARD_HEALTHCHECKS_URL": "http://hc-ping.com/x"}, "https://"),
    ],
)
def test_a_port_an_address_and_the_url_are_checked(extra, words):
    environ = {
        "DASHBOARD_SMTP_HOST": "h",
        "DASHBOARD_SMTP_USER": "u",
        "DASHBOARD_SMTP_PASSWORD": "p",
        "DASHBOARD_MAIL_FROM": "f@example.com",
        "DASHBOARD_MAIL_TO": "t@example.com",
        **extra,
    }
    with pytest.raises(ConfigError, match=words):
        load_settings(environ=environ, env_file=None)


def test_an_env_file_that_is_not_utf8_is_refused_by_name(tmp_path):
    env = tmp_path / ".env"
    env.write_bytes("DASHBOARD_SMTP_HOST=h\n".encode("utf-16"))
    with pytest.raises(ConfigError, match="not UTF-8"):
        load_settings(environ={}, env_file=env)


# --- the review round ---------------------------------------------------------------------


def test_an_unquoted_clock_read_as_a_sexagesimal_integer_is_accepted(tmp_path):
    config = load_config(
        _write(tmp_path, MINIMAL + "digest_utc: 210\nthresholds:\n  handoff_due_utc: 30\n")
    )
    assert config.digest_utc == time(3, 30)
    assert config.thresholds.handoff_due_utc == time(0, 30)
    assert load_config(_write(tmp_path, MINIMAL + "digest_utc: 3:30\n")).digest_utc == time(3, 30)
    with pytest.raises(ConfigError, match="HH:MM"):
        load_config(_write(tmp_path, MINIMAL + "digest_utc: 1440\n"))


def test_the_healthchecks_url_loses_its_trailing_slash_so_fail_can_be_appended():
    environ = {"DASHBOARD_HEALTHCHECKS_URL": "https://hc-ping.com/uuid/"}
    assert (
        load_settings(environ=environ, env_file=None).healthchecks_url == "https://hc-ping.com/uuid"
    )


def test_an_env_file_that_cannot_be_read_is_refused_by_name(tmp_path):
    with pytest.raises(ConfigError, match="cannot be read"):
        read_env_file(tmp_path)


def test_every_threshold_has_its_parser_and_no_parser_is_without_a_threshold():
    from dataclasses import fields

    from contrib.dashboard.config import _THRESHOLD_PARSERS

    assert set(_THRESHOLD_PARSERS) == {spec.name for spec in fields(Thresholds)}
