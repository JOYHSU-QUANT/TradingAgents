"""What a chain read raises instead of answering.

The classes follow what a caller can do about each:

- :class:`RpcUnavailable` and :class:`BlockNotFound`: try again later.
- :class:`CallReverted`, :class:`MalformedResponse` and :class:`RpcRejected`:
  this read has no answer; another read may.
- :class:`RpcConfigError`: no read will work until the setup is fixed.

The messages never hold the RPC URL: :class:`~.rpc.Rpc` scrubs it from the
text of whatever it caught before it builds one of these.
"""

from __future__ import annotations

__all__ = [
    "BlockNotFound",
    "CallReverted",
    "ChainError",
    "MalformedResponse",
    "RpcConfigError",
    "RpcRejected",
    "RpcUnavailable",
]


class ChainError(Exception):
    """A chain read that produced no answer."""


class RpcConfigError(ChainError):
    """The endpoint cannot be used as configured: no URL, the wrong chain, or a refused key."""


class RpcUnavailable(ChainError):
    """The transport kept failing (connection, timeout, 429, 5xx) until the attempts ran out."""


class RpcRejected(ChainError):
    """The node answered, and the answer was an error."""


class BlockNotFound(ChainError):
    """The node has no such block, or none at or after the time asked for."""


class CallReverted(ChainError):
    """The contract call reverted."""


class MalformedResponse(ChainError):
    """The node's answer is not what the call returns, or its values cannot be right."""
