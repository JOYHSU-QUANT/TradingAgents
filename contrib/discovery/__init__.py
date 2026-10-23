"""Discovery Lab — synthetic worlds with a known answer, to examine the research loop.

The discovery package is NOT a second research path and not a trading path:
it reads no real market, writes into no trading or research store the other
packages own, and changes nothing of the research package's language. It
builds synthetic candle and funding histories (worlds) with a rule planted
in them — or with nothing planted at all — and runs ``contrib.autoresearch``'s
own hypothesis loop over them through that package's two ports, so that
"did the loop find the planted rule" and "did it make one up in pure noise"
become measurable (discovery plan §0). That sentence is the package's scope
test: a change that needs it softened is out of scope, not a bigger feature.

What is examined is search efficiency and statistical reliability inside a
finite-structure, bounded hypothesis space — the research package's
declarative language as it is. It is not a test of open-ended scientific
discovery, and nothing here widens that language to make a benchmark easier
(plan §0.3, the scope statement in ``README.md``).

The package imports ``contrib.autoresearch`` and the shared floor
``contrib.core`` and nothing else under ``contrib/``: not the perp trader,
not replay, not carry. What it borrows is listed once, in
:mod:`~contrib.discovery.upstream`, and ``tests/test_upstream.py`` reads the
sources to hold both the funnel and the direction of the edge.
"""
