"""Startup agent-authorization verification (phase3-spec §6.1).

Runs before the kill switch is armed: derive the agent address from the
private key, look up the main wallet's approved agents via the read-only Info
API (``extra_agents``), and require our agent to be listed and unexpired. Any
failure is a named error and the caller refuses to start — not safe mode,
because nothing is running yet. One check catches all three §6.1 mistakes:
the wrong network's key, a key authorized for a different account, and an
expired authorization (agent approvals last at most ~180 days).

The private key is consumed for address derivation only; it never appears in
any error message, log line, or return value.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from ..common.instants import from_epoch_ms
from ..exchanges.hyperliquid.sdk_client import account_from_agent_key, call_sdk

if TYPE_CHECKING:
    from hyperliquid.info import Info

__all__ = [
    "EXPIRY_WARNING_HORIZON",
    "AgentAuthorization",
    "AgentAuthorizationError",
    "derive_agent_address",
    "verify_agent_authorization",
]

# §6.1: an authorization that outlives startup but not the long-running loop
# turns into mid-run signing failures with real orders enabled — the exact
# class of operator-actionable problem the startup check exists to front-load.
# Below this remaining validity the caller warns (still passes; a legitimately
# short approval must not be refused).
EXPIRY_WARNING_HORIZON = timedelta(days=7)


class AgentAuthorizationError(Exception):
    """A named §6.1 verification failure — the operator must fix key/approval.

    Messages name the agent *address* (public) and the wallet, never the key.
    """


@dataclass(frozen=True)
class AgentAuthorization:
    """The verified result: which agent address is approved, and until when."""

    agent_address: str
    valid_until: datetime

    def expires_within(self, horizon: timedelta, *, now: datetime | None = None) -> bool:
        """True when the authorization expires within ``horizon`` of ``now``
        (callers warn — verification already guaranteed it is not expired)."""
        return self.valid_until - (now or datetime.now(timezone.utc)) < horizon


def derive_agent_address(agent_key: str) -> str:
    """Derive the agent's public address from its private key (§6.1 step 1).

    Delegates to the shared leak-safe key handling (``from None``, no input
    echo — see :func:`~..exchanges.hyperliquid.sdk_client.account_from_agent_key`),
    raising in this module's own vocabulary.
    """
    return account_from_agent_key(agent_key, error_cls=AgentAuthorizationError).address


def verify_agent_authorization(
    info: Info,
    *,
    wallet_address: str,
    agent_key: str,
    now: datetime | None = None,
) -> AgentAuthorization:
    """§6.1 steps 1-3: derive, look up ``extra_agents``, require listed + unexpired.

    ``info`` is any object with the SDK ``Info.extra_agents(user)`` method (a
    fake in tests). Network/SDK failures propagate as ``ExchangeError`` via
    :func:`call_sdk` (already named); everything this module itself detects is
    an :class:`AgentAuthorizationError`. Both refuse startup at the caller.
    """
    agent_address = derive_agent_address(agent_key)
    now = now or datetime.now(timezone.utc)
    raw = call_sdk(info.extra_agents, wallet_address)
    if not isinstance(raw, list):
        raise AgentAuthorizationError(
            f"extra_agents returned an unexpected shape ({type(raw).__name__}) "
            f"for wallet {wallet_address} — cannot verify agent authorization"
        )
    wanted = agent_address.lower()
    matches = [
        item
        for item in raw
        if isinstance(item, dict)
        and isinstance(item.get("address"), str)
        and item["address"].lower() == wanted
    ]
    if not matches:
        raise AgentAuthorizationError(
            f"agent {agent_address} is not in wallet {wallet_address}'s approved "
            "agent list — wrong network's key, a key for a different account, or "
            "the agent was never approved (§6.1)"
        )
    # Reject ambiguity rather than guess (the AccountSnapshot duplicate-coin
    # precedent): with two entries, picking either validUntil could silently
    # read a stale re-approval as the live one — or vice versa.
    if len(matches) > 1:
        raise AgentAuthorizationError(
            f"agent {agent_address} appears {len(matches)} times in wallet "
            f"{wallet_address}'s approved agent list — ambiguous authorization; "
            "remove the stale approval and re-run (§6.1)"
        )
    entry = matches[0]
    valid_until_ms = entry.get("validUntil")
    # The venue sends an integer; ``float`` is tolerated for a JSON decoder's
    # number type, not for fractional milliseconds, hence the ``int()``. The
    # decoder's ``NaN`` / ``Infinity`` pass the type check and fail that
    # ``int()``; a value past ``datetime``'s range fails ``from_epoch_ms`` —
    # all unreadable, all the same refusal, none a bare exception.
    try:
        if isinstance(valid_until_ms, bool) or not isinstance(valid_until_ms, (int, float)):
            raise TypeError("not a number")
        valid_until = from_epoch_ms(int(valid_until_ms))
    except (TypeError, ValueError, OverflowError) as exc:
        raise AgentAuthorizationError(
            f"agent {agent_address} is listed for wallet {wallet_address} but its "
            f"validUntil is unreadable ({valid_until_ms!r}) — cannot verify expiry"
        ) from exc
    if valid_until <= now:
        raise AgentAuthorizationError(
            f"agent {agent_address}'s authorization for wallet {wallet_address} "
            f"expired at {valid_until.isoformat()} — re-approve the agent "
            "(approvals last at most ~180 days)"
        )
    return AgentAuthorization(agent_address=agent_address, valid_until=valid_until)
