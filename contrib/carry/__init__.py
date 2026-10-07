"""Carry — the coordinator of one hedged book: short the perp, hold the spot.

The carry package is NOT a trading path of its own and NOT a decision maker
for either venue: it places no orders, signs nothing, and writes into
neither ``contrib.hyperliquid_perp``'s store nor ``contrib.uniswap_v3``'s.
It reads Hyperliquid's funding history, applies one rule (the z-score of the
latest settlement against the trailing window, and a state machine around
it), and writes ONE handoff document naming the target each leg should hold
at the next daily boundary. Each venue's own engine reads that document and
decides, under its own gates, whether to act. That sentence is the
package's scope test: a change that needs it softened is out of scope, not
a bigger feature.

Why a package of its own (carry plan §2 D1): the signal is one number
computed once, and the two legs must read the SAME number at the SAME
boundary — a rule copied into each venue's package would drift, and the
spot package cannot see funding at all. The borrow is one-way and funnelled
through :mod:`.upstream`: ``contrib.hyperliquid_perp`` for the funding
types, the z-score and the instants, ``contrib.autoresearch`` for the
funding walk and the research store that keeps the history. The spot
package is never imported — its isolation test forbids it — so what this
package knows of it is one table read with ``sqlite3`` and nothing else.
"""
