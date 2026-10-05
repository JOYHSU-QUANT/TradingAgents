"""A fork run's wallet: a dev account given the run's balances on a fork reset for each bar.

A fork run carries its balances in its ledger, from bar to bar, and the
fork is where each bar's swaps are signed and checked. For a bar that
trades, :meth:`ForkWallet.prepare`

1. resets the fork (``anvil_reset``) to the bar's fill block
   (:func:`~..domain.execution.fill_block`), the block a paper run and a
   quoted backtest price the same bar's swaps at, so that all three meet the
   same pools;
2. gives the wallet the ledger's balances: its ETH set to the gas balance,
   and each token's balance written into the token's storage.

A token's balance is found in its storage by trying the slots of a Solidity
``mapping(address => uint256)`` declared first, second and on: a value
written to the wallet's entry of the slot's mapping, and read back by
``balanceOf``, names the slot, and the word the slot held is put back. The
slot is then kept for the wallet's life. A token whose balances are not
found that way cannot be given to the wallet, and is refused.

:meth:`ForkWallet.holdings` reads what the wallet holds at the fork's
latest block, which the engine checks against the ledger before the bar's
swaps and after them.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

from web3 import Web3

from ..domain.execution import ExecutionSettings, fill_block
from ..domain.ledger import Ledger
from ..domain.types import Bar, Token, eth_from_wei, wei_from_eth
from .errors import MalformedResponse, RpcConfigError
from .swaps import ChainExecutor
from .units import from_raw, to_raw

__all__ = ["ForkWallet"]

# How many of a token's first storage slots are tried for its balances.
_SLOTS: Final = 64
# A balance no wallet holds by chance, written to find the slot: it reads back only
# from the slot that holds the balances. Below 2**255, which a token may keep a flag in.
_PROBE: Final = 0x5EED_0000_0000_0000_0000_0000_0000_0001


def _key(owner: str, slot: int) -> bytes:
    """Where ``owner``'s entry of the mapping declared at ``slot`` is stored."""
    return bytes(Web3.keccak(bytes(12) + bytes.fromhex(owner[2:]) + slot.to_bytes(32, "big")))


class ForkWallet:
    """The :class:`~..ports.Wallet` of ``executor``, whose dev account trades the ``tokens`` on its fork.

    ``settings`` place a bar's fill block, which the fork is reset to.
    """

    def __init__(
        self, executor: ChainExecutor, *, tokens: Sequence[Token], settings: ExecutionSettings
    ) -> None:
        if not isinstance(executor, ChainExecutor):
            # A chain executor is built only on a node it found to be an anvil fork.
            raise ValueError(f"a fork wallet is a chain executor's, got {executor!r}")
        self._rpc = executor.fork.rpc
        self._executor = executor
        self._tokens = tuple(tokens)
        self._settings = settings
        self._keys: dict[str, bytes] = {}

    @property
    def address(self) -> str:
        """The dev account the wallet is."""
        return self._executor.address

    def prepare(self, bar: Bar, ledger: Ledger) -> None:
        """Reset the fork to ``bar``'s fill block, and give the wallet the balances of ``ledger``."""
        rpc, block = self._rpc, fill_block(bar, self._settings)
        missing = sorted({token.symbol for token in self._tokens} - set(ledger.balances))
        if missing:
            raise ValueError(f"the ledger holds no balance of {missing}")
        rpc.reset_fork(block)
        head = rpc.latest_header().number
        if head != block:
            raise MalformedResponse(f"the fork was reset to block {block}, and is at block {head}")
        rpc.set_balance(self.address, wei_from_eth(ledger.gas_eth))
        for token in self._tokens:
            raw = to_raw(token, ledger.balances[token.symbol])
            rpc.set_storage(token.address, self._balance_key(token, head), raw)

    def holdings(self) -> Ledger:
        """What the wallet holds at the fork's latest block: the tokens, and its ETH."""
        head = self._rpc.latest_header().number
        return Ledger(
            balances={
                token.symbol: self._executor.balance(token, block=head) for token in self._tokens
            },
            gas_eth=eth_from_wei(self._rpc.balance(self.address, block=head)),
        )

    def _balance_key(self, token: Token, block: int) -> bytes:
        """Where the wallet's balance of ``token`` is stored, found once at ``block``."""
        if token.symbol in self._keys:
            return self._keys[token.symbol]
        rpc, owner = self._rpc, self.address
        for slot in range(_SLOTS):
            key = _key(owner, slot)
            held = rpc.storage_at(token.address, key, block=block)
            rpc.set_storage(token.address, key, _PROBE)
            try:
                read = self._executor.balance(token, block=block)
            finally:
                rpc.set_storage(token.address, key, held)
            if read == from_raw(token, _PROBE):
                self._keys[token.symbol] = key
                return key
        raise RpcConfigError(
            f"the balances of {token.symbol} are not in a mapping declared in its first "
            f"{_SLOTS} storage slots, so the wallet cannot be given any"
        )
