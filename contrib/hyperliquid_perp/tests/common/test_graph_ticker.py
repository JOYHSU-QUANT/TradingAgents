"""``graph_ticker``: the spelling the graph is handed (#392)."""

from __future__ import annotations

import pytest

from contrib.hyperliquid_perp.common.graph_ticker import graph_ticker


@pytest.mark.parametrize(
    ("coin", "spelled"),
    [
        ("BTC", "BTC-USD"),
        (" eth ", "ETH-USD"),
        ("kPEPE", "KPEPE-USD"),  # no table: a coin the symbol rules never heard of still spells
    ],
)
def test_spells_a_bare_coin_the_way_yahoo_quotes_crypto(coin, spelled):
    assert graph_ticker(coin) == spelled


@pytest.mark.parametrize("coin", ["  ", "BTC-USD", "BTC/USD", "BTC USD"])
def test_anything_but_a_bare_coin_is_refused_rather_than_suffixed(coin):
    # ``BTC-USD`` would otherwise become ``BTC-USD-USD``.
    with pytest.raises(ValueError, match="bare coin"):
        graph_ticker(coin)
