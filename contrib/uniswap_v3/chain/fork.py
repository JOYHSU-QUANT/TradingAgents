"""The guard a signing wallet stands behind, and the only accounts it signs with.

An anvil fork of mainnet answers to mainnet's chain ID, so the chain ID
cannot tell a fork from mainnet. A fork is told apart by where it is and
what it is: :func:`open_fork` takes only a URL whose host is a literal
loopback address (not ``localhost``, which a hosts file can point
elsewhere), with a node behind it whose ``anvil_nodeInfo`` names the URL it
was forked from, and a :class:`Fork` is what it returns.

A wallet signs only with an account anvil makes from its public test
mnemonic (:func:`dev_account`). Their keys are published with anvil, so
they hold nothing on a real chain worth taking, and no other key can be
handed in: this package has no way to sign with a key of the user's.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final
from urllib.parse import urlsplit

from eth_account import Account
from eth_account.hdaccount import key_from_seed, seed_from_mnemonic
from eth_account.signers.local import LocalAccount

from .errors import MalformedResponse, NotAFork, RpcRejected
from .rpc import Rpc, RpcSettings, http_provider_at

__all__ = [
    "ANVIL_MNEMONIC",
    "DEFAULT_FORK_URL",
    "DEV_ACCOUNTS",
    "Fork",
    "dev_account",
    "open_fork",
    "require_anvil",
]

# Where ``anvil`` listens unless told otherwise.
DEFAULT_FORK_URL: Final = "http://127.0.0.1:8545"

# anvil's default mnemonic, printed by anvil itself on start. Public.
ANVIL_MNEMONIC: Final = "test test test test test test test test test test test junk"

# The accounts anvil funds from that mnemonic, at m/44'/60'/0'/0/<index>, as
# anvil prints them. A key derived here must give the address listed here.
DEV_ACCOUNTS: Final = (
    "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266",
    "0x70997970C51812dc3A010C7d01b50e0d17dc79C8",
    "0x3C44CdDdB6a900fa2b585dd299e03d12FA4293BC",
    "0x90F79bf6EB2c4f870365E785982E1f101E93b906",
    "0x15d34AAf54267DB7D7c367839AAf71A00a2C6A65",
    "0x9965507D1a55bcC2695C58ba16FB37d819B0A4dc",
    "0x976EA74026E726554dB657fA54763abd0C3a0aa9",
    "0x14dC79964da2C08b23698B3D3cc7Ca32193d9955",
    "0x23618e81E3f5cdF7f54C3d65f7FBc0aBf5B21E8f",
    "0xa0Ee7A142d267C1f36714E4a8F75612F20a79720",
)


def dev_account(index: int) -> LocalAccount:
    """The anvil dev account ``index`` (0 to 9), with its key, derived from the public mnemonic."""
    if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(DEV_ACCOUNTS):
        raise ValueError(
            f"a dev account is numbered 0 to {len(DEV_ACCOUNTS) - 1}, got {index!r}"
        )
    seed = seed_from_mnemonic(ANVIL_MNEMONIC, passphrase="")
    account: LocalAccount = Account.from_key(key_from_seed(seed, f"m/44'/60'/0'/0/{index}"))
    if account.address != DEV_ACCOUNTS[index]:
        raise ValueError(
            f"the key derived for dev account {index} is of {account.address}, "
            f"and anvil's is {DEV_ACCOUNTS[index]}"
        )
    return account


def _is_loopback(url: str) -> bool:
    """Whether ``url`` is an http(s) URL whose host is a literal loopback address."""
    try:
        parts = urlsplit(url)
        host, _ = parts.hostname, parts.port
    except ValueError:
        return False
    if parts.scheme not in ("http", "https") or not host:
        return False
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        # A name, ``localhost`` included: what it resolves to is not this code's to know.
        return False


def require_anvil(rpc: Rpc) -> None:
    """Refuse a node that is not an anvil fork: its ``anvil_nodeInfo`` must name a fork URL.

    A fresh anvil, not forked from anything, has no fork URL, and neither
    has a proxy that answers the method with something else. What the node
    answers is not quoted: the fork URL holds the upstream node's key.
    """
    try:
        info = rpc.node_info()
    except (RpcRejected, MalformedResponse) as exc:
        raise NotAFork(
            f"the node does not answer anvil_nodeInfo as anvil does, and only an anvil "
            f"fork is signed for ({type(exc).__name__})"
        ) from None
    fork = info.get("forkConfig")
    if not isinstance(fork, Mapping) or not isinstance(fork.get("forkUrl"), str) or not fork["forkUrl"]:
        raise NotAFork(
            "the node answers anvil_nodeInfo without the URL it was forked from: it is "
            "not a fork, and only an anvil fork is signed for"
        )


@dataclass(frozen=True)
class Fork:
    """A connection to a node :func:`open_fork` found to be a local anvil fork.

    Built by :func:`open_fork`. One built by hand is not known to be local:
    a signing wallet asks ``anvil_nodeInfo`` again, that it be an anvil
    fork, and nothing more.
    """

    rpc: Rpc


def open_fork(
    chain_id: int, *, url: str = DEFAULT_FORK_URL, settings: RpcSettings | None = None
) -> Fork:
    """Open the anvil fork at ``url``, a loopback address, of the chain ``chain_id``.

    A URL whose host is not a literal loopback address is refused before
    anything is asked of it, and the request goes there directly, past any
    proxy the environment names. The node must then report ``chain_id``,
    and name in ``anvil_nodeInfo`` the URL it was forked from.
    """
    if not isinstance(url, str) or not _is_loopback(url):
        raise NotAFork(
            "a fork is opened only at an http(s) URL on this machine, whose host is a "
            "loopback address (127.0.0.1 or ::1; not a name such as localhost)"
        )
    settings = settings if settings is not None else RpcSettings()
    rpc = Rpc(http_provider_at(url, settings=settings, direct=True), chain_id, settings=settings)
    rpc.verify_chain()
    require_anvil(rpc)
    return Fork(rpc)
