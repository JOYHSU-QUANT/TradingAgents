"""Reads from an Ethereum node: blocks, pool prices, quotes and the base fee; and, on a local fork only, signed swaps.

Everything goes through one :class:`~.rpc.Rpc`, which retries a transport
failure and turns every other failure of the node or the transport into a
:class:`~.errors.ChainError`.
A read never answers with a guess: a node error, a reply of the wrong shape
or a block whose time does not fit raises.

Every read of chain state (a pool's price, a quote, a base fee) names the
block it reads at, so the same call against an archive node gives the same
answer later. A few reads start from the head instead: the latest block's
header, the pending block's (whose time is the node's clock), and the search
for the block at a time. The QuoterV2 calls are ``eth_call`` simulations.

Signing lives in :mod:`.fork`, :mod:`.transactions` and :mod:`.swaps`, and
only on a local anvil fork, as one of anvil's public dev accounts: no other
key can be handed in.

This is the one part of the package that imports ``web3``.
"""
