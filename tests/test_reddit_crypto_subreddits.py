"""Which subreddits a coin is searched in (#395)."""

from unittest.mock import patch

import pytest

from tradingagents.dataflows import reddit


@pytest.mark.unit
# A pair, never the bare coin: a bare ``BTC`` is a stock spelling to the
# symbol rules (#392), and the perp hands the graph ``BTC-USD`` since #396.
@pytest.mark.parametrize("ticker", ["BTC-USD", "eth-usd", "SOL-USD"])
def test_a_coin_is_searched_in_the_crypto_rooms_by_default(ticker):
    assert reddit.subreddits_for(ticker) == reddit.CRYPTO_SUBREDDITS


@pytest.mark.unit
@pytest.mark.parametrize("ticker", ["NVDA", "AAPL", "7203.T"])
def test_a_stock_is_searched_in_the_stock_rooms_by_default(ticker):
    assert reddit.subreddits_for(ticker) == reddit.DEFAULT_SUBREDDITS


@pytest.mark.unit
def test_the_fetcher_picks_the_rooms_from_the_symbol_when_none_are_named():
    asked = []

    def _fetch(ticker, sub, limit, timeout, _retry=True):
        asked.append((ticker, sub))
        return []

    with patch.object(reddit, "_fetch_subreddit", side_effect=_fetch):
        reddit.fetch_reddit_posts("BTC-USD", inter_request_delay=0)
        reddit.fetch_reddit_posts("NVDA", inter_request_delay=0)
    assert asked == [("BTC", sub) for sub in reddit.CRYPTO_SUBREDDITS] + [
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
