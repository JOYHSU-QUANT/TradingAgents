"""Signing one transaction, sending it once, and waiting for its receipt.

:class:`TransactionSender` signs only as one of anvil's dev accounts
(:data:`~.fork.DEV_ACCOUNTS`), and refuses any other account it is handed:
this package has no way to sign with a key that could hold anything.

One transaction, from first to last:

1. Its gas is estimated on top of the latest block. A call that would
   revert raises :class:`~.errors.CallReverted`, and nothing is sent.
2. It is signed as an EIP-1559 transaction for the connection's chain ID,
   named explicitly, with the account's next nonce. The fee cap is twice
   the latest base fee plus the node's suggested priority fee.
3. It is sent once. When the send fails, the transaction's receipt is
   looked for, since the node may have taken it all the same; with one,
   the send goes on to it. Without one, a send the node refused raises
   what the node said, and nothing was sent; a send whose answer was lost,
   or cannot be read, may have reached the node, and raises
   :class:`~.errors.TransactionUnconfirmed`.
4. Its receipt is waited for. One that does not come in time, or cannot be
   read, raises :class:`~.errors.TransactionUnconfirmed`, and one that says
   it reverted :class:`~.errors.TransactionReverted`.

Everything that fails after the send raises a :class:`~.errors.SendError`:
the transaction may yet change the wallet.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from eth_account.signers.local import LocalAccount

from .errors import (
    ChainError,
    MalformedResponse,
    RpcRejected,
    RpcUnavailable,
    TransactionReverted,
    TransactionUnconfirmed,
)
from .fork import DEV_ACCOUNTS
from .rpc import Receipt, Rpc

__all__ = ["SendSettings", "TransactionSender"]


@dataclass(frozen=True)
class SendSettings:
    """How a transaction's gas limit is set, and how long its receipt is waited for.

    ``gas_margin_percent`` is added to the estimate for the gas limit, since
    the state a transaction meets can differ from the one it was estimated
    on. Gas the transaction does not use is not paid.
    """

    gas_margin_percent: int = 20
    receipt_timeout_seconds: float = 120.0
    poll_seconds: float = 1.0

    def __post_init__(self) -> None:
        margin = self.gas_margin_percent
        if isinstance(margin, bool) or not isinstance(margin, int) or margin < 0:
            raise ValueError(f"gas_margin_percent must be a non-negative integer, got {margin!r}")
        for name in ("receipt_timeout_seconds", "poll_seconds"):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, int | float)
                or not 0 < value < math.inf
            ):
                raise ValueError(f"{name} must be a finite number above zero, got {value!r}")


class TransactionSender:
    """Signs with ``account``, an anvil dev account, and sends through ``rpc``, one transaction at a time.

    For one thread: each send reads the account's next nonce, which a
    second sender on the same account would read as well.
    """

    def __init__(
        self,
        rpc: Rpc,
        account: LocalAccount,
        *,
        settings: SendSettings | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if account.address not in DEV_ACCOUNTS:
            raise ValueError(
                f"only an anvil dev account is signed with, and {account.address} is not one"
            )
        self._rpc = rpc
        self._account = account
        self._settings = settings if settings is not None else SendSettings()
        self._clock = clock
        self._sleep = sleep

    @property
    def address(self) -> str:
        """The address the transactions are sent from."""
        return str(self._account.address)

    def send(self, to: str, data: bytes, *, what: str, value: int = 0) -> Receipt:
        """Send a call of ``data`` to ``to`` with ``value`` wei, and return its receipt once mined.

        ``what`` names the transaction in errors. A receipt is returned only
        for a transaction that succeeded.
        """
        rpc = self._rpc
        call: dict[str, Any] = {
            "from": self.address,
            "to": to,
            "data": "0x" + data.hex(),
            "value": value,
        }
        estimate = rpc.estimate_gas(call)
        head = rpc.latest_header()
        if head.base_fee_wei is None:
            raise MalformedResponse(f"block {head.number} has no base fee to price {what} by")
        tip = rpc.max_priority_fee_wei()
        transaction = {
            "type": 2,
            # Named here: the validation that would fill it in is not run (Rpc).
            "chainId": rpc.chain_id,
            "nonce": rpc.transaction_count(self.address),
            "to": to,
            "value": value,
            "data": data,
            "gas": estimate + estimate * self._settings.gas_margin_percent // 100,
            "maxFeePerGas": 2 * head.base_fee_wei + tip,
            "maxPriorityFeePerGas": tip,
        }
        signed = self._account.sign_transaction(transaction)
        tx_hash = "0x" + bytes(signed.hash).hex()
        # Asked before the send, so that a failure of it is not taken for the send's.
        rpc.verify_chain()
        try:
            sent = rpc.send_raw_transaction(bytes(signed.raw_transaction))
        except (RpcRejected, RpcUnavailable, MalformedResponse) as exc:
            # The node may have taken the transaction all the same.
            unread: ChainError | None = None
            try:
                receipt = rpc.receipt(tx_hash)
            except ChainError as failed:
                receipt, unread = None, failed
            if receipt is None:
                if isinstance(exc, RpcRejected):
                    raise
                # The answer was lost or garbled, and the transaction may have arrived.
                looked = f"; its receipt could not be read either ({unread})" if unread else ""
                raise TransactionUnconfirmed(
                    f"{what} ({tx_hash}) may or may not have reached the node ({exc}){looked}",
                    tx_hashes=(tx_hash,),
                ) from exc
            sent = tx_hash
        if sent != tx_hash:
            raise TransactionUnconfirmed(
                f"{what} was signed as {tx_hash}, and the node took it as {sent}",
                tx_hashes=(tx_hash, sent),
            )
        receipt = self._wait(tx_hash, what)
        if not receipt.succeeded:
            raise TransactionReverted(
                f"{what} ({tx_hash}) was mined in block {receipt.block} and reverted",
                tx_hash=tx_hash,
                gas_cost_wei=receipt.gas_cost_wei,
            )
        return receipt

    def _wait(self, tx_hash: str, what: str) -> Receipt:
        deadline = self._clock() + self._settings.receipt_timeout_seconds
        while True:
            try:
                receipt = self._rpc.receipt(tx_hash)
            except ChainError as exc:
                raise TransactionUnconfirmed(
                    f"{what} ({tx_hash}) was sent, and its receipt could not be read ({exc})",
                    tx_hashes=(tx_hash,),
                ) from exc
            if receipt is not None:
                return receipt
            if self._clock() >= deadline:
                raise TransactionUnconfirmed(
                    f"{what} ({tx_hash}) was sent, and no receipt came in "
                    f"{self._settings.receipt_timeout_seconds} seconds",
                    tx_hashes=(tx_hash,),
                )
            self._sleep(self._settings.poll_seconds)
