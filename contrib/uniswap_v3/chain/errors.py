"""What a chain read raises instead of answering.

The classes are grouped by what a caller can do about each:

- :class:`TransientChainError`, try again later: :class:`RpcUnavailable`
  and :class:`BlockNotFound`.
- :class:`UnansweredRead`, this read has no answer and asking again will not
  change that: :class:`CallReverted`, :class:`InsufficientLiquidity`,
  :class:`MalformedResponse` and :class:`RpcRejected`.
- :class:`RpcConfigError`, no read will work until the setup is fixed.

The messages never hold the RPC URL: :class:`~.rpc.Rpc` scrubs it from the
text of whatever it caught before it builds one of these.
"""

from __future__ import annotations

__all__ = [
    "BlockNotFound",
    "CallReverted",
    "ChainError",
    "InsufficientLiquidity",
    "MalformedResponse",
    "RpcConfigError",
    "RpcRejected",
    "RpcUnavailable",
    "TransientChainError",
    "UnansweredRead",
]


class ChainError(Exception):
    """A chain read that produced no answer."""


class TransientChainError(ChainError):
    """The same read may answer later."""


class UnansweredRead(ChainError):
    """This read has no answer, now or later; another read may have one."""


class RpcConfigError(ChainError):
    """The endpoint cannot be used as set up.

    No URL or one that cannot be requested, the wrong chain, a refused key,
    or a node that does not keep the history asked for.
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
    """The node answered, and the answer was an error."""


class CallReverted(UnansweredRead):
    """The contract call reverted."""


class InsufficientLiquidity(UnansweredRead):
    """The pool ran out of liquidity before the whole input was swapped."""


class MalformedResponse(UnansweredRead):
    """The node's answer is not what the call returns, or its values cannot be right."""
