"""The spelling of a perp coin that the trading-agents graph is handed.

The perp package names its instrument by Hyperliquid's bare coin (``BTC``):
the store's ``symbol`` column, the payload, the context text's ``Coin:``
line and every exchange call use that word, and they keep using it. The
graph is the one consumer that must not: its analysts carry the word they
are given to yfinance-backed tools unchanged, and on Yahoo Finance a bare
``BTC`` is not bitcoin but the NYSE-listed Grayscale Bitcoin Mini Trust —
``normalize_symbol`` leaves a plain upper-case word alone because it is a
legal equity ticker (issue #392). Handed ``BTC``, the instrument identity
came back as the ETF's name on every cycle, the market analyst's verified
snapshot as the ETF's ~US$37 daily closes on a third of the recorded cycles
across runs 7–9, and the ticker news as the ETF's near-empty feed.

So the graph is handed Yahoo's crypto spelling, ``<COIN>-USD``. Every
routed crypto tool already accepts it (``classify_crypto_asset`` reads the
base off a pair, ``normalize_symbol`` returns it unchanged), and the uniswap
package has passed ``ETH-USD`` / ``BTC-USD`` from the start. Yahoo quotes
every crypto pair in USD; the fork's symbol rules recognise USDT/USDC too,
but the graph is handed the one spelling Yahoo serves.

Shared with ``contrib.replay`` (through its ``upstream`` seam) so the
re-run lower graph and the daemon put the same word in
``company_of_interest`` — the parity the replay plan's §5 measures. A pure
string function with no imports, so that seam stays importable without
loading the engine.
"""

from __future__ import annotations

__all__ = ["graph_ticker"]


def graph_ticker(coin: str) -> str:
    """Yahoo's crypto spelling of a Hyperliquid coin: ``BTC`` -> ``BTC-USD``.

    Purely syntactic — no table, no network — so a coin the fork's symbol
    rules have not heard of (``HYPE``) still gets the spelling Yahoo would
    serve it under. Takes a BARE coin as the exchange names it (letters and
    digits): a blank, or a word carrying a dash, slash or space, is already
    some venue's pair spelling and is refused rather than suffixed into a
    symbol no vendor resolves (``BTC/USD-USD``). A coin that is a legal word
    but no coin (a config typo) is the exchange's to refuse, as it is for
    every other perp-side use of ``coins:``.
    """
    spelled = coin.strip().upper()
    if not (spelled.isascii() and spelled.isalnum()):
        raise ValueError(f"graph_ticker: {coin!r} is not a bare coin (letters and digits only)")
    return f"{spelled}-USD"
