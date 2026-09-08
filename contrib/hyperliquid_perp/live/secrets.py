"""Per-network agent-key loading (phase3-spec §6).

The private key is read from the environment only — never from YAML, SQLite,
CSV, logs, prompts, or raw payloads (§6 rules 1-2). The env var is split per
network (§6, v3) so both keys can coexist in ``.env`` and switching networks
can never accidentally reuse the other network's key. Nothing in this module
(or anywhere else) may ever log or persist the key value; functions here
return it to the caller and nothing more.
"""

from __future__ import annotations

import os

from ..common.constants import LEGAL_NETWORKS
from ..common.enum_guard import check_enum

__all__ = [
    "AGENT_KEY_ENV_VARS",
    "agent_key_env_var",
    "load_agent_key",
]

# Keyed by ``common.constants.LEGAL_NETWORKS`` (the owner; tests pin the key
# sets equal) — the lookup below refuses over it, then indexes here (issue #226).
AGENT_KEY_ENV_VARS = {
    "testnet": "HYPERLIQUID_AGENT_KEY_TESTNET",
    "mainnet": "HYPERLIQUID_AGENT_KEY_MAINNET",
}


def agent_key_env_var(network: str) -> str:
    """The env var name holding the agent key for ``network`` (for messages)."""
    check_enum(network, LEGAL_NETWORKS, name="network")
    return AGENT_KEY_ENV_VARS[network]


def load_agent_key(network: str) -> str | None:
    """The agent private key for ``network``, or ``None`` when unset/blank.

    A present-but-blank var (``HYPERLIQUID_AGENT_KEY_TESTNET=`` in ``.env``)
    reads as missing, exactly like the OPENROUTER_API_KEY checks treat it —
    so the §6 rule 6 gate (a missing key with ``allow_real_orders: true`` is a
    named startup failure) fires on the empty-assignment case too instead of
    passing "" to key derivation.
    """
    value = os.environ.get(agent_key_env_var(network), "").strip()
    return value or None
