"""StockTwits fetch degrades (never raises) on transport errors, including the
http.client chunked-transfer exceptions that are not OSErrors (#1024); missing
fields render as explicit markers and a mismatched symbol echo degrades (#31)."""

from __future__ import annotations

import http.client
import json
from unittest.mock import patch
from urllib.error import HTTPError

import pytest

from tradingagents.dataflows import stocktwits


def _resp(read_fn):
    """A minimal context-manager response whose read() runs ``read_fn``."""

    class _Resp:
        def __enter__(self_inner):
            return self_inner

        def __exit__(self_inner, *a):
            return False

        def read(self_inner):
            return read_fn()

    return _Resp()


def _raise(exc):
    def _r():
        raise exc

    return _resp(_r)


def _json_resp(payload):
    return _resp(lambda: json.dumps(payload).encode("utf-8"))


def _message(**overrides):
    msg = {
        "created_at": "2026-05-20T14:30:00Z",
        "user": {"username": "trader1"},
        "entities": {"sentiment": {"basic": "Bullish"}},
        "body": "to the moon",
    }
    msg.update(overrides)
    return msg


@pytest.mark.unit
class StockTwitsResilienceTests:
    @pytest.mark.parametrize(
        "exc",
        [
            http.client.IncompleteRead(b""),
            HTTPError("url", 503, "down", {}, None),
            TimeoutError("slow"),
        ],
    )
    def test_transport_errors_return_placeholder(self, exc):
        with patch.object(stocktwits, "urlopen", return_value=_raise(exc)):
            out = stocktwits.fetch_stocktwits_messages("NVDA")
        assert "unavailable" in out.lower()
        assert out.startswith("<stocktwits unavailable")


@pytest.mark.unit
class TestPlaceholderMarkers:
    """Missing message fields render as explicit markers, not values that
    look like a real timestamp or a user named "?" (#31)."""

    def test_missing_user_and_timestamp_get_markers(self):
        payload = {"messages": [_message(created_at=None, user=None)]}
        with patch.object(stocktwits, "urlopen", return_value=_json_resp(payload)):
            out = stocktwits.fetch_stocktwits_messages("NVDA")
        assert "[unknown user]" in out
        assert "[time unknown]" in out
        assert "@?" not in out

    def test_real_fields_render_normally(self):
        payload = {"messages": [_message()]}
        with patch.object(stocktwits, "urlopen", return_value=_json_resp(payload)):
            out = stocktwits.fetch_stocktwits_messages("NVDA")
        assert "@trader1" in out
        assert "2026-05-20T14:30:00Z" in out
        assert "[unknown user]" not in out


@pytest.mark.unit
class TestSymbolEcho:
    """A response that names a different symbol than requested must degrade
    with disclosure, not render another instrument's sentiment (#36)."""

    def test_mismatched_symbol_degrades_with_disclosure(self):
        payload = {"symbol": {"symbol": "TSLA"}, "messages": [_message()]}
        with patch.object(stocktwits, "urlopen", return_value=_json_resp(payload)):
            out = stocktwits.fetch_stocktwits_messages("NVDA")
        assert out.startswith("<stocktwits unavailable")
        assert "symbol mismatch" in out
        assert "NVDA" in out and "TSLA" in out
        assert "to the moon" not in out  # no content from the wrong symbol

    def test_matching_symbol_is_case_insensitive(self):
        payload = {"symbol": {"symbol": "NVDA"}, "messages": [_message()]}
        with patch.object(stocktwits, "urlopen", return_value=_json_resp(payload)):
            out = stocktwits.fetch_stocktwits_messages("nvda")
        assert "to the moon" in out
        assert "symbol mismatch" not in out

    def test_absent_symbol_envelope_does_not_false_alarm(self):
        payload = {"messages": [_message()]}
        with patch.object(stocktwits, "urlopen", return_value=_json_resp(payload)):
            out = stocktwits.fetch_stocktwits_messages("NVDA")
        assert "to the moon" in out
        assert "symbol mismatch" not in out
