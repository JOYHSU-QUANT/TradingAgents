"""What a chain read raises instead of answering, and what a send raises once it may have changed the wallet.

A :class:`ChainError` is a read with no answer, grouped by what a caller can
do about it:

- :class:`TransientChainError`, try again later: :class:`RpcUnavailable`
  and :class:`BlockNotFound`.
- :class:`UnansweredRead`, this read has no answer: :class:`CallReverted`,
  :class:`InsufficientLiquidity`, :class:`MalformedResponse` and
  :class:`RpcRejected`. Asking again will not change the first three; the
  last may be the node's bad moment. :class:`InsufficientFunds` is one too:
  a transaction the wallet's ETH could not pay the gas of, read off its
  balance before anything was signed.
- :class:`RpcConfigError`, no read will work until the setup is fixed.
  :class:`NotAFork` is one: a node that is not a local anvil fork.

A :class:`SendError` is not a :class:`ChainError`, so no handler of reads
catches it: a transaction was sent, or may have been, and did not end as
asked. The wallet may have changed, and the caller cannot take it for
"nothing happened". :class:`TransactionReverted`,
:class:`TransactionUnconfirmed`, :class:`SwapNotFilled` (only gas was
spent) and :class:`SwapOutcomeUnknown` (a swap was mined and what it did
cannot be read).

The messages never hold the node's URL, nor the URL a fork was made from:
:class:`~.rpc.Rpc` scrubs them, and anything shaped like a URL, from the
text of whatever it caught before it builds one of these.
"""

from __future__ import annotations

from decimal import Decimal

__all__ = [
    "BlockNotFound",
    "CallReverted",
    "ChainError",
    "InsufficientFunds",
    "InsufficientLiquidity",
    "MalformedResponse",
    "NotAFork",
    "RpcConfigError",
    "RpcRejected",
    "RpcUnavailable",
    "SendError",
    "SwapNotFilled",
    "SwapOutcomeUnknown",
    "TransactionReverted",
    "TransactionUnconfirmed",
    "TransientChainError",
    "UnansweredRead",
]


class ChainError(Exception):
    """A chain read that produced no answer."""


class TransientChainError(ChainError):
    """The same read may answer later."""


class UnansweredRead(ChainError):
    """This read has no answer; another read may have one.

    Asking the same thing again is pointless for every subclass but
    :class:`RpcRejected`.
    """


class RpcConfigError(ChainError):
    """The endpoint cannot be used as set up.

    No URL or one that cannot be requested, the wrong chain, a refused key,
    a node that does not keep the history asked for, a chain this package
    knows no quoter or router for, or (:class:`NotAFork`) a node that is not
    an anvil fork where only a fork is signed for.
    """


class RpcUnavailable(TransientChainError):
    """The transport kept failing, or the node kept rate-limiting, until the attempts ran out."""


class BlockNotFound(TransientChainError):
    """The node does not have the block yet, or the chain has not reached the time asked for.

    A read of a block long past, from a node that has dropped it, raises
    this as well: only a read that knows which blocks are final (the block
    search, and :func:`~.blocks.reading_block`) can tell the two apart, and
    it raises :class:`RpcConfigError` for the second.
    """


class RpcRejected(UnansweredRead):
    """The node answered, and the answer was an error.

    Which kind is not known: a request the node will never accept, or a
    node having a bad moment. A caller must not take it for an answer about
    the chain; the command line stops and exits as it does for a transient
    failure, leaving the retry to whoever scheduled it.
    """


class CallReverted(UnansweredRead):
    """The contract call reverted."""


class InsufficientLiquidity(UnansweredRead):
    """The pool ran out of liquidity before the whole input was swapped."""


class InsufficientFunds(UnansweredRead):
    """The wallet's ETH does not cover what a transaction may pay for gas, so it was not sent."""


class MalformedResponse(UnansweredRead):
    """The node's answer is not what the call returns, or its values cannot be right."""


class NotAFork(RpcConfigError):
    """The node is not a local anvil fork, and only a fork is signed for."""


class SendError(Exception):
    """A transaction was sent, or may have been, and did not end as asked.

    ``tx_hashes`` are the transactions concerned, oldest first. Unlike the
    reads above, this is not "nothing happened": what was mined changed the
    wallet, if only by its gas.
    """

    def __init__(self, message: str, *, tx_hashes: tuple[str, ...] = ()) -> None:
        super().__init__(message)
        self.tx_hashes = tuple(tx_hashes)


class TransactionReverted(SendError):
    """The transaction was mined and reverted: its gas was spent and nothing else changed.

    ``gas_cost_wei`` is what its gas cost.
    """

    def __init__(self, message: str, *, tx_hash: str, gas_cost_wei: int) -> None:
        super().__init__(message, tx_hashes=(tx_hash,))
        self.gas_cost_wei = gas_cost_wei


class TransactionUnconfirmed(SendError):
    """A transaction may or may not have reached the chain: it may be mined later, or never.

    No receipt came in time, or none could be read, or the node took the
    transaction under another hash than the one signed. ``gas_cost_eth`` is
    the gas of the transactions before it that were mined (an approval
    before a swap), which the wallet has paid; ``None`` when there were none.
    """

    def __init__(
        self,
        message: str,
        *,
        tx_hashes: tuple[str, ...] = (),
        gas_cost_eth: Decimal | None = None,
    ) -> None:
        super().__init__(message, tx_hashes=tx_hashes)
        self.gas_cost_eth = gas_cost_eth


class _MinedSwapError(SendError):
    """A swap's error once one of its transactions was mined: ``gas_cost_eth`` is what they all cost."""

    def __init__(self, message: str, *, tx_hashes: tuple[str, ...], gas_cost_eth: Decimal) -> None:
        super().__init__(message, tx_hashes=tx_hashes)
        self.gas_cost_eth = gas_cost_eth


class SwapNotFilled(_MinedSwapError):
    """A swap did not fill, after a transaction for it was mined: only gas was spent.

    An approval that reverted, a swap that reverted, or a swap not sent
    after its approval was mined. No token moved, beyond what the approval
    allows. The wallet has paid ``gas_cost_eth``.
    """


class SwapOutcomeUnknown(_MinedSwapError):
    """A swap was mined and succeeded, and what it did cannot be read off its receipt.

    Tokens may have moved, so the wallet has to be read to know where it
    stands. The wallet has paid ``gas_cost_eth``.
    """
