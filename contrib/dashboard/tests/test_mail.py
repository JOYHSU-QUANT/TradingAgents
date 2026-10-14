"""The letters: what they say, how they are sent, and that no amount, position or address gets in."""

from __future__ import annotations

import smtplib
from datetime import timedelta
from email.message import EmailMessage

import pytest

from contrib.dashboard.alerts import ActiveCondition, Pending, advance, evaluate, fresh_state
from contrib.dashboard.config import MailSettings
from contrib.dashboard.mail import (
    MailError,
    digest_letter,
    line_summary,
    send_mail,
    transitions_letter,
)
from contrib.dashboard.readers import CommandResult

from .support import (
    AGENT_KEY,
    CARRY_KEY,
    HL_KEY,
    NOW,
    WALLET,
    carry_document,
    decimal_texts,
    hl_instant,
    quiet_documents,
    read_lines_and_host,
    safe_mode_event,
)

SETTINGS = MailSettings(
    host="smtp.example.com",
    port=587,
    user="bot@example.com",
    sender="bot@example.com",
    recipients=("op@example.com", "two@example.com"),
    password="app-pass",
)


class FakeSMTP:
    """Records the STARTTLS, login and send a letter goes through."""

    instances: list[FakeSMTP] = []
    refused: dict[str, tuple[int, bytes]] = {}

    def __init__(self, host: str, port: int, timeout: float) -> None:
        self.host, self.port, self.timeout = host, port, timeout
        self.steps: list[str] = []
        self.sent: list[EmailMessage] = []
        self.login_args: tuple[str, str] | None = None
        FakeSMTP.instances.append(self)

    def __enter__(self):
        self.steps.append("enter")
        return self

    def __exit__(self, *exc):
        self.steps.append("exit")
        return False

    def ehlo(self):
        self.steps.append("ehlo")

    def starttls(self, context=None):
        self.steps.append("starttls" + (" tls" if context is not None else ""))

    def login(self, user, password):
        self.login_args = (user, password)
        self.steps.append("login")

    def send_message(self, message):
        self.sent.append(message)
        self.steps.append("send")
        return dict(self.refused)


class HalfRefusingSMTP(FakeSMTP):
    refused = {"two@example.com": (550, b"no such user")}


class RefusingSMTP(FakeSMTP):
    def login(self, user, password):
        raise smtplib.SMTPAuthenticationError(535, f"bad credentials for {password}".encode())


_reads_and_host = read_lines_and_host


def test_send_mail_goes_through_starttls_and_login_with_one_letter_to_every_recipient():
    FakeSMTP.instances.clear()
    send_mail(SETTINGS, subject="[t] hi", body="body\n", now=NOW, smtp_factory=FakeSMTP)
    [smtp] = FakeSMTP.instances
    assert (smtp.host, smtp.port) == ("smtp.example.com", 587)
    assert smtp.steps == ["enter", "ehlo", "starttls tls", "ehlo", "login", "send", "exit"]
    assert smtp.login_args == ("bot@example.com", "app-pass")
    [message] = smtp.sent
    assert message["Subject"] == "[t] hi"
    assert message["To"] == "op@example.com, two@example.com"
    assert message["From"] == "bot@example.com"
    assert message["Date"].startswith("Wed, 14 Oct 2026 05:00:00")
    assert message.get_content() == "body\n"
    assert send_mail(SETTINGS, subject="s", body="b", now=NOW, smtp_factory=FakeSMTP) == 0
    assert send_mail(SETTINGS, subject="s", body="b", now=NOW, smtp_factory=HalfRefusingSMTP) == 1


def test_a_refused_send_is_a_mail_error_without_the_password():
    with pytest.raises(MailError) as caught:
        send_mail(SETTINGS, subject="s", body="b", now=NOW, smtp_factory=RefusingSMTP)
    text = str(caught.value)
    assert text.startswith("SMTPAuthenticationError") and "app-pass" not in text and "***" in text


def test_the_transitions_letter_lists_each_pending_line_with_its_kind_and_instant():
    at = "2026-10-14T05:00:00+00:00"
    pending = (
        Pending(
            "raised",
            "hl-paper:cycle_overdue",
            "hl-paper",
            "cycle_overdue",
            "the next decision was due at 2026-10-14 04:29Z, 31 minutes ago",
            at,
        ),
        Pending(
            "cleared",
            "unit:carry-signal.service:failed",
            "carry-signal.service",
            "failed",
            "last run failed",
            at,
        ),
        Pending(
            "event",
            "hl-paper:event:safe_mode_entered@x",
            "hl-paper",
            "event:safe_mode_entered",
            "safe_mode_entered (recoverable) at 2026-10-14 04:57Z",
            at,
        ),
    )
    subject, body = transitions_letter("[trading]", pending)
    assert (
        transitions_letter("[t]", pending[2:])[0]
        == "[t] 1 event(s): hl-paper event:safe_mode_entered"
    )
    assert subject == "[trading] 1 raised, 1 cleared, 1 event(s): hl-paper cycle_overdue"
    lines = body.splitlines()
    assert lines[0] == "State changes on the paper traders:"
    assert lines[2] == (
        "RAISED  2026-10-14 05:00Z  hl-paper: the next decision was due at 2026-10-14 04:29Z, "
        "31 minutes ago"
    )
    assert lines[3] == "CLEARED 2026-10-14 05:00Z  carry-signal.service: last run failed"
    assert lines[4] == (
        "EVENT   2026-10-14 05:00Z  hl-paper: safe_mode_entered (recoverable) at 2026-10-14 04:57Z"
    )
    assert lines[-1].startswith("Only which line, what state and when")


def test_the_digest_summarises_each_line_the_host_and_the_alerts_holding(tmp_path):
    config, reads, host = _reads_and_host(tmp_path, quiet_documents())
    active = {
        "hl-paper:safe_mode": ActiveCondition(
            "hl-paper",
            "safe_mode",
            "safe mode manual since 2026-10-14 02:00Z",
            "2026-10-14T02:02:00+00:00",
        ),
    }
    subject, body = digest_letter(
        "[trading]", now=NOW, reads=reads, host=host, active=active, notes=("hl-paper: parked",)
    )
    assert subject == "[trading] daily digest 2026-10-14"
    lines = body.splitlines()
    assert lines[0] == "Daily digest for 2026-10-14 (UTC), written 2026-10-14 05:00Z."
    assert lines[2] == (
        "hl-paper (paper-BTC-8, hyperliquid): unit active; cycles 78 (in_progress 0, completed 70, "
        "api_failed 6, invalid_output 2); last decision 2026-10-14 04:00Z; next 2026-10-14 08:00Z; "
        "safe mode none"
    )
    assert lines[4] == (
        "uniswap-trend (paper-trend-1, uniswap): unit inactive; decided 6 bar(s) (traded 2, held 3, "
        "skipped 1, rejected 0, partial 0); clock up_to_date; last decided 2026-10-14 00:00Z"
    )
    assert lines[7] == (
        "carry (carry-ETH-1+paper-carry-1, carry): unit inactive; handoff stay_out for "
        "2026-10-14 00:00Z; 3 day(s) 2026-10-11 to 2026-10-13, 0 light(s)"
    )
    assert lines[9].startswith("host tokyo: disk ")
    assert lines[9].endswith("% used; 1269 MB of memory available")
    assert lines[10].startswith(
        "units: carry-signal.service inactive, hl-paper-carry-eth.service active"
    )
    assert lines[11].startswith("checkouts: ") and "at b17efda8" in lines[11]
    assert lines[13] == "Alerts holding (1):"
    assert lines[14] == (
        "  hl-paper safe_mode since 2026-10-14 02:02Z: safe mode manual since 2026-10-14 02:00Z"
    )
    assert lines[15] == "Note: hl-paper: parked"


def test_a_line_not_read_or_not_shaped_is_said_so_in_the_digest(tmp_path):
    docs = quiet_documents()
    docs[HL_KEY] = CommandResult(5, "", "boom")
    docs[CARRY_KEY] = carry_document(span={"days": 3})
    config, reads, host = _reads_and_host(tmp_path, docs)
    assert line_summary(reads[0], host) == (
        "hl-paper (paper-BTC-8, hyperliquid): unit active; not read (exit 5)"
    )
    assert line_summary(reads[5], host) == (
        "carry (carry-ETH-1+paper-carry-1, carry): unit inactive; not summarised (KeyError)"
    )


def test_no_amount_position_or_address_of_any_document_reaches_a_letter(tmp_path):
    """Every decimal text and every hex in the documents stays out of both letters."""
    docs = quiet_documents()
    hl = docs[HL_KEY]
    hl["safe_mode"] = {
        "type": "manual",
        "reason": f"wallet {WALLET} drained",
        "entered_at": hl_instant(NOW - timedelta(hours=1)),
    }
    hl["safe_mode_events"] = [
        safe_mode_event(NOW - timedelta(minutes=5), "safe_mode_entered", "manual")
    ]
    hl["scheduler"]["next_decision_at"] = hl_instant(NOW - timedelta(hours=1))
    hl["scheduler"]["export"] = {
        "status": "failed",
        "at": hl_instant(NOW - timedelta(hours=1)),
        "error": AGENT_KEY,
    }
    config, reads, host = _reads_and_host(tmp_path, docs)
    seen = {"hl-paper": hl_instant(NOW - timedelta(hours=1))}
    evaluation = evaluate(now=NOW, config=config, reads=reads, host=host, events_seen=seen)
    assert len(evaluation.conditions) >= 3 and len(evaluation.events) == 1
    state = advance(fresh_state(), evaluation, NOW)
    _, transitions = transitions_letter("[t]", state.pending)
    _, digest = digest_letter(
        "[t]", now=NOW, reads=reads, host=host, active=state.active, notes=evaluation.notes
    )
    secrets = decimal_texts(list(docs.values()))
    assert len(secrets) > 20  # the fixtures do carry amounts, prices and the two hex strings
    for letter in (transitions, digest):
        for value in secrets:
            assert value not in letter, (value, letter)
        for word in ("0x", "key", "secret", "password", "mnemonic", "USDC", "WETH"):
            assert word not in letter, (word, letter)


def test_a_header_or_credential_the_library_cannot_encode_is_a_mail_error():
    from dataclasses import replace

    accented = replace(SETTINGS, password="p\u00e4ss")

    class AsciiOnlySMTP(FakeSMTP):
        def login(self, user, password):
            password.encode("ascii")

    with pytest.raises(MailError, match="UnicodeEncodeError"):
        send_mail(accented, subject="s", body="b", now=NOW, smtp_factory=AsciiOnlySMTP)
    with pytest.raises(MailError, match="ValueError"):
        send_mail(SETTINGS, subject="bad\rheader", body="b", now=NOW, smtp_factory=FakeSMTP)
