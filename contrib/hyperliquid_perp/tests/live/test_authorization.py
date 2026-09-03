"""Tests for the §6.1 startup agent-authorization check (fake Info, no network).

One check catches three operator mistakes — wrong network's key, key approved
for a different account, expired approval — so each has its own named-failure
test, plus the secret-hygiene invariant: the private key never appears in any
error message.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from contrib.hyperliquid_perp.exchanges.hyperliquid.errors import ExchangeRequestError
from contrib.hyperliquid_perp.live.authorization import (
    EXPIRY_WARNING_HORIZON,
    AgentAuthorization,
    AgentAuthorizationError,
    derive_agent_address,
    verify_agent_authorization,
)

# A synthetic test-only private key (never funded anywhere).
_KEY = "0x" + "11" * 32
_WALLET = "0x" + "aa" * 20
_NOW = datetime(2026, 7, 11, tzinfo=timezone.utc)


class _FakeInfo:
    """Stands in for the SDK ``Info``: returns a canned extra_agents payload."""

    def __init__(self, response):
        self._response = response
        self.queried: list[str] = []

    def extra_agents(self, user: str):
        self.queried.append(user)
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


def _entry(address: str, *, valid_until: datetime = _NOW + timedelta(days=90)) -> dict:
    return {
        "name": "agent",
        "address": address,
        "validUntil": int(valid_until.timestamp() * 1000),
    }


def test_authorized_agent_passes():
    agent = derive_agent_address(_KEY)
    info = _FakeInfo([_entry("0x" + "bb" * 20), _entry(agent)])
    auth = verify_agent_authorization(info, wallet_address=_WALLET, agent_key=_KEY, now=_NOW)
    assert auth.agent_address == agent
    assert auth.valid_until == _NOW + timedelta(days=90)
    assert info.queried == [_WALLET]


def test_address_match_is_case_insensitive():
    # The exchange may return lowercase addresses while eth_account derives
    # checksummed mixed case — the comparison must not care.
    agent = derive_agent_address(_KEY)
    info = _FakeInfo([_entry(agent.lower())])
    auth = verify_agent_authorization(info, wallet_address=_WALLET, agent_key=_KEY, now=_NOW)
    assert auth.agent_address == agent


def test_duplicate_agent_entries_are_rejected():
    # Two entries for the same address (e.g. a re-approval leaving a stale
    # one) make the verdict ambiguous — reject rather than guess which
    # validUntil is live (the AccountSnapshot duplicate-coin precedent).
    agent = derive_agent_address(_KEY)
    info = _FakeInfo(
        [
            _entry(agent.lower(), valid_until=_NOW - timedelta(days=1)),
            _entry(agent, valid_until=_NOW + timedelta(days=90)),
        ]
    )
    with pytest.raises(AgentAuthorizationError, match="ambiguous"):
        verify_agent_authorization(info, wallet_address=_WALLET, agent_key=_KEY, now=_NOW)


def test_expires_within_boundaries():
    auth = AgentAuthorization(
        agent_address="0x" + "cc" * 20, valid_until=_NOW + EXPIRY_WARNING_HORIZON
    )
    # Exactly the horizon away is NOT "within" (strict <)…
    assert not auth.expires_within(EXPIRY_WARNING_HORIZON, now=_NOW)
    # …one second inside it is.
    assert auth.expires_within(EXPIRY_WARNING_HORIZON, now=_NOW + timedelta(seconds=1))


def test_agent_not_in_list_is_named_failure():
    info = _FakeInfo([_entry("0x" + "bb" * 20)])
    with pytest.raises(AgentAuthorizationError, match="not in wallet"):
        verify_agent_authorization(info, wallet_address=_WALLET, agent_key=_KEY, now=_NOW)


def test_empty_agent_list_is_named_failure():
    info = _FakeInfo([])
    with pytest.raises(AgentAuthorizationError, match="not in wallet"):
        verify_agent_authorization(info, wallet_address=_WALLET, agent_key=_KEY, now=_NOW)


def test_expired_authorization_is_named_failure():
    agent = derive_agent_address(_KEY)
    info = _FakeInfo([_entry(agent, valid_until=_NOW - timedelta(seconds=1))])
    with pytest.raises(AgentAuthorizationError, match="expired"):
        verify_agent_authorization(info, wallet_address=_WALLET, agent_key=_KEY, now=_NOW)


def test_expiry_boundary_exactly_now_is_expired():
    # validUntil == now must read as expired, not as a still-valid approval.
    agent = derive_agent_address(_KEY)
    info = _FakeInfo([_entry(agent, valid_until=_NOW)])
    with pytest.raises(AgentAuthorizationError, match="expired"):
        verify_agent_authorization(info, wallet_address=_WALLET, agent_key=_KEY, now=_NOW)


@pytest.mark.parametrize(
    "valid_until",
    [
        "soon",  # not a number
        True,  # a bool is an int to isinstance, never a stamp
        float("nan"),  # the JSON decoder's NaN passes the number check
        float("inf"),  # ...and so does Infinity
        10**20,  # an integer past datetime's range
    ],
)
def test_unreadable_valid_until_is_named_failure(valid_until):
    # Every unreadable shape is the SAME named refusal — never a bare
    # ValueError / OverflowError out of the int() or the epoch decode.
    agent = derive_agent_address(_KEY)
    info = _FakeInfo([{"name": "agent", "address": agent, "validUntil": valid_until}])
    with pytest.raises(AgentAuthorizationError, match="validUntil is unreadable"):
        verify_agent_authorization(info, wallet_address=_WALLET, agent_key=_KEY, now=_NOW)


def test_non_list_response_is_named_failure():
    info = _FakeInfo({"error": "rate limited"})
    with pytest.raises(AgentAuthorizationError, match="unexpected shape"):
        verify_agent_authorization(info, wallet_address=_WALLET, agent_key=_KEY, now=_NOW)


def test_info_failure_propagates_as_exchange_error():
    # Network/SDK failures ride the existing call_sdk translation so the
    # caller's `except ExchangeError` lane handles them.
    info = _FakeInfo(ConnectionError("boom"))
    with pytest.raises(ExchangeRequestError):
        verify_agent_authorization(info, wallet_address=_WALLET, agent_key=_KEY, now=_NOW)


def test_malformed_key_is_named_and_never_echoed():
    bad_key = "0xdeadbeef"  # too short to be a 32-byte key
    with pytest.raises(AgentAuthorizationError) as excinfo:
        derive_agent_address(bad_key)
    message = str(excinfo.value)
    assert "malformed" in message
    # §6 rule 2: the key value must never leak into the error text, and the
    # named error must not chain the original (whose message may quote it).
    assert bad_key not in message
    assert excinfo.value.__cause__ is None


def test_derive_agent_address_is_deterministic():
    assert derive_agent_address(_KEY) == derive_agent_address(_KEY)
    assert derive_agent_address(_KEY).startswith("0x")
