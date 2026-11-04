"""Which subreddits a coin is searched in (#395)."""

from unittest.mock import patch

import pytest

from tradingagents.dataflows import reddit


@pytest.mark.unit
# A pair, never the bare coin: a bare ``BTC`` is a stock spelling to the
# symbol rules (#392), and the perp hands the graph ``BTC-USD`` since #396.
@pytest.mark.parametrize(
    "ticker, rooms",
    [
        ("BTC-USD", ("Bitcoin", "CryptoCurrency", "CryptoMarkets")),
        ("eth-usd", ("ethtrader", "CryptoCurrency", "CryptoMarkets")),
        ("SOL-USD", ("CryptoCurrency", "CryptoMarkets")),
    ],
)
def test_a_coin_is_searched_in_its_own_room_then_the_crypto_rooms(ticker, rooms):
    assert reddit.subreddits_for(ticker) == rooms


@pytest.mark.unit
def test_the_callers_knowledge_of_the_asset_outranks_the_symbol():
    # A coin the symbol rules do not know still gets the crypto rooms when
    # the caller says it is a coin; a spelling the rules read as a coin is
    # searched in the stock rooms when the caller says it is a stock.
    assert reddit.subreddits_for("HYPE-USD", crypto=True) == ("CryptoCurrency", "CryptoMarkets")
    assert reddit.subreddits_for("HYPE-USD") == reddit.DEFAULT_SUBREDDITS
    assert reddit.subreddits_for("BTC-USD", crypto=False) == reddit.DEFAULT_SUBREDDITS


@pytest.mark.unit
@pytest.mark.parametrize("ticker", ["NVDA", "AAPL", "7203.T"])
def test_a_stock_is_searched_in_the_stock_rooms_by_default(ticker):
    assert reddit.subreddits_for(ticker) == reddit.DEFAULT_SUBREDDITS


@pytest.mark.unit
@pytest.mark.parametrize(
    "ticker, term",
    [
        ("BTC-USD", "BTC"),
        ("hype-usd", "HYPE"),  # outside the symbol rules' base list, still a pair
        ("SUI-USDT", "SUI"),
        ("NVDA", "NVDA"),
        ("BRK-B", "BRK-B"),  # a dash that is not a quote
        ("7203.T", "7203.T"),
    ],
)
def test_the_search_term_is_the_coin_for_a_pair_and_the_symbol_otherwise(ticker, term):
    assert reddit.search_term(ticker) == term


@pytest.mark.unit
def test_an_unknown_coin_is_searched_by_its_base_in_the_rooms_the_caller_names():
    asked = []

    def _fetch(ticker, sub, limit, timeout, _retry=True):
        asked.append((ticker, sub))
        return []

    with patch.object(reddit, "_fetch_subreddit", side_effect=_fetch):
        reddit.fetch_reddit_posts(
            "HYPE-USD", subreddits=reddit.subreddits_for("HYPE-USD", crypto=True), inter_request_delay=0
        )
    assert asked == [("HYPE", "CryptoCurrency"), ("HYPE", "CryptoMarkets")]


@pytest.mark.unit
def test_the_fetcher_picks_the_rooms_from_the_symbol_when_none_are_named():
    asked = []

    def _fetch(ticker, sub, limit, timeout, _retry=True):
        asked.append((ticker, sub))
        return []

    with patch.object(reddit, "_fetch_subreddit", side_effect=_fetch):
        reddit.fetch_reddit_posts("BTC-USD", inter_request_delay=0)
        reddit.fetch_reddit_posts("NVDA", inter_request_delay=0)
    assert asked == [("BTC", sub) for sub in ("Bitcoin", "CryptoCurrency", "CryptoMarkets")] + [
        ("NVDA", sub) for sub in reddit.DEFAULT_SUBREDDITS
    ]


@pytest.mark.unit
def test_rooms_a_caller_names_are_used_as_given():
    asked = []
    with patch.object(
        reddit, "_fetch_subreddit", side_effect=lambda t, sub, *a, **k: asked.append(sub) or []
    ):
        reddit.fetch_reddit_posts("BTC-USD", subreddits=("Bitcoin",), inter_request_delay=0)
    assert asked == ["Bitcoin"]
