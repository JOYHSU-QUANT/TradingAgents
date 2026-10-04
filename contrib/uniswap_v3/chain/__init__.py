"""Reads from an Ethereum node: blocks, pool prices, quotes and the base fee.

Everything goes through one :class:`~.rpc.Rpc`, which retries a transport
failure and turns every other failure of the node or the transport into a
:class:`~.errors.ChainError`.
A read never answers with a guess: a node error, a reply of the wrong shape
or a block whose time does not fit raises.

Every read of chain state (a pool's price, a quote, a base fee) names the
block it reads at, so the same call against an archive node gives the same
answer later. Two reads start from the latest block instead: its header, and
the search for the block at a time. Nothing here holds a key or signs; the
QuoterV2 calls are ``eth_call`` simulations.

This is the one part of the package that imports ``web3``.
"""
