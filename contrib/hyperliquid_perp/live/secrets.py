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

__all__ = [
    "AGENT_KEY_ENV_VARS",
    "agent_key_env_var",
    "load_agent_key",
]

AGENT_KEY_ENV_VARS = {
    "testnet": "HYPERLIQUID_AGENT_KEY_TESTNET",
    "mainnet": "HYPERLIQUID_AGENT_KEY_MAINNET",
}


def agent_key_env_var(network: str) -> str:
    """The env var name holding the agent key for ``network`` (for messages)."""
    try:
        return AGENT_KEY_ENV_VARS[network]
    except KeyError:
        raise ValueError(
            f"network must be one of {sorted(AGENT_KEY_ENV_VARS)}, got {network!r}"
        ) from None


def load_agent_key(network: str) -> str | None:
    """The agent private key for ``network``, or ``None`` when unset/blank.

    A present-but-blank var (``HYPERLIQUID_AGENT_KEY_TESTNET=`` in ``.env``)
    reads as missing, exactly like the OPENROUTER_API_KEY checks treat it —
    so §6 rule 6 (missing key forces ``allow_real_orders`` to false) fires on
    the empty-assignment case too instead of passing "" to key derivation.
    """
    value = os.environ.get(agent_key_env_var(network), "").strip()
    return value or None
