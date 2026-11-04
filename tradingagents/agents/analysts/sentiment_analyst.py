"""Sentiment analyst — multi-source sentiment analysis for a target ticker.

Previously named ``social_media_analyst``. Renamed and redesigned because
the old version had a prompt that demanded social-media analysis but the
only tool available was Yahoo Finance news — which led LLMs to fabricate
Reddit/X/StockTwits content under prompt pressure (verified live).

The redesigned agent pre-fetches complementary data sources before the
LLM is invoked and injects them into the prompt as structured blocks:

  1. News headlines     — Yahoo Finance (institutional framing)
  2. StockTwits messages — retail-trader posts indexed by cashtag, with
                           user-labeled Bullish/Bearish sentiment tags
                           (stocks only: a crypto asset skips it, #395)
  3. Reddit posts        — r/wallstreetbets, r/stocks, r/investing for a
                           stock; the coin's own room (r/Bitcoin, r/ethtrader)
                           plus r/CryptoCurrency and r/CryptoMarkets for a
                           crypto asset

The agent does not use tool-calling; the data is in the prompt from
turn 0. Output uses the structured-output pattern (json_schema for
OpenAI/xAI, response_schema for Gemini, tool-use for Anthropic), falling
back to free-text generation for providers that lack native support, so
the sentiment header (band + score + confidence) is deterministic across
runs and providers instead of free-form per-model prose.

See: https://github.com/TauricResearch/TradingAgents/issues/557
See: https://github.com/TauricResearch/TradingAgents/issues/796
"""

from datetime import datetime, timedelta

from langchain_core.messages import AIMessage
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder

from tradingagents.agents.schemas import SentimentReport, render_sentiment_report
from tradingagents.agents.utils.agent_utils import (
    get_instrument_context_from_state,
    get_language_instruction,
    get_news,
)
from tradingagents.agents.utils.structured import (
    NO_EXTERNAL_TOOLS,
    bind_structured,
    invoke_structured_or_freetext,
)
from tradingagents.dataflows.reddit import fetch_reddit_posts, subreddits_for
from tradingagents.dataflows.stocktwits import fetch_stocktwits_messages


def _seven_days_back(trade_date: str) -> str:
    return (datetime.strptime(trade_date, "%Y-%m-%d") - timedelta(days=7)).strftime("%Y-%m-%d")


def create_sentiment_analyst(llm):
    """Create a sentiment analyst node for the trading graph.

    Pre-fetches news + StockTwits + Reddit data, injects them into the
    prompt as structured blocks, and produces a deterministic sentiment
    report via structured output (with a free-text fallback for providers
    that do not support it). For a crypto asset StockTwits is not fetched
    and the prompt has two sources, not three (#395): its public API
    refuses datacenter addresses (the paper host read a Cloudflare 403 on
    227 of 227 cycles), the prompt called it the key real-time signal, and
    the report then leaned on a placeholder for six weeks; the crypto
    subreddits carry that retail conversation instead.
    """
    # Ungated: no injected output contract to preserve here — see
    # bind_structured()'s docstring.
    structured_llm = bind_structured(llm, SentimentReport, "Sentiment Analyst", config_gated=False)

    def sentiment_analyst_node(state):
        ticker = state["company_of_interest"]
        end_date = state["trade_date"]
        start_date = _seven_days_back(end_date)
        instrument_context = get_instrument_context_from_state(state)
        # One decision for both the StockTwits skip and the Reddit rooms: the
        # asset type the graph was told, not a re-derivation from the symbol
        # (a coin outside the symbol rules' base list — HYPE-USD, SUI-USD —
        # would otherwise lose StockTwits AND be searched in the stock rooms);
        # the chooser then adds the coin's own room when it has one.
        is_crypto = state.get("asset_type", "stock") == "crypto"
        subreddits = subreddits_for(ticker, crypto=is_crypto)

        # Pre-fetch the sources: three for a stock, two for a crypto asset
        # (no StockTwits, #395). StockTwits and Reddit degrade to a
        # placeholder string on any failure, so the LLM always sees something
        # from them. News goes through route_to_vendor; news_data is optional
        # since #393, so an exhausted chain never raises here: it ends in
        # NO_DATA_AVAILABLE or the ``DATA_UNAVAILABLE: optional news_data
        # ...`` sentinel naming the vendor, and the report says the section
        # is absent.
        news_block = get_news.func(ticker, start_date, end_date)
        # Pass the analysis window so a historical run trims social posts to it
        # instead of leaking today's chatter into a backtest (#1220).
        # Not asked for a coin (#395): the request would only be refused from
        # the deployment's address, and a section that is always a placeholder
        # is worse than no section — the prompt below says two sources then.
        stocktwits_block = (
            None
            if is_crypto
            else fetch_stocktwits_messages(
                ticker, limit=30, start_date=start_date, end_date=end_date, curr_date=end_date
            )
        )
        reddit_block = fetch_reddit_posts(
            ticker, subreddits=subreddits, start_date=start_date, end_date=end_date
        )

        system_message = _build_system_message(
            ticker=ticker,
            start_date=start_date,
            end_date=end_date,
            news_block=news_block,
            stocktwits_block=stocktwits_block,
            reddit_block=reddit_block,
            subreddits=subreddits,
        )

        prompt = ChatPromptTemplate.from_messages(
            [
                (
                    "system",
                    "You are a helpful AI assistant, collaborating with other assistants."
                    " If you or any other assistant has the FINAL TRANSACTION PROPOSAL: **BUY/HOLD/SELL** or deliverable,"
                    " prefix your response with FINAL TRANSACTION PROPOSAL: **BUY/HOLD/SELL** so the team knows to stop."
                    # No tool-calling here: the data is pre-fetched into the
                    # prompt, so tool-range wording would only invite a
                    # hallucinated tool call (#1130).
                    " Today's date is {current_date}; treat it as 'now' for all analysis. {instrument_context}"
                    " " + NO_EXTERNAL_TOOLS +
                    "\n{system_message}",
                ),
                MessagesPlaceholder(variable_name="messages"),
            ]
        )

        prompt = prompt.partial(system_message=system_message)
        prompt = prompt.partial(current_date=end_date)
        prompt = prompt.partial(instrument_context=instrument_context)

        # Format the template into a concrete message list so the structured
        # and free-text paths receive the same input. No bind_tools — the
        # data is already in the prompt.
        formatted_messages = prompt.format_messages(messages=state["messages"])

        report_text = invoke_structured_or_freetext(
            structured_llm,
            llm,
            formatted_messages,
            render_sentiment_report,
            "Sentiment Analyst",
        )

        return {
            "messages": [AIMessage(content=report_text)],
            "sentiment_report": report_text,
        }

    return sentiment_analyst_node


# The Reddit heading names the rooms searched and what each is like, so the
# analyst weighs a post by where it was said; one line per subreddit the two
# default sets know, the room's name alone for any other a caller passes.
_SUBREDDIT_CHARACTER = {
    "wallstreetbets": "is often contrarian/exuberant",
    "stocks": "more measured",
    "investing": "longer-term",
    "cryptocurrency": "broad and noisy, the retail crowd across coins",
    "bitcoin": "is Bitcoin's own room (BTC holders and maximalists)",
    "ethtrader": "is Ether's own room (ETH holders and traders)",
    "cryptomarkets": "trading-minded, shorter horizon, across coins",
}


def _reddit_heading(subreddits: tuple[str, ...], *, engagement: bool) -> str:
    rooms = ", ".join(f"r/{sub}" for sub in subreddits)
    # Subreddit names are case-insensitive to Reddit; so is the lookup.
    character = "; ".join(
        f"r/{sub} {_SUBREDDIT_CHARACTER[sub.casefold()]}"
        for sub in subreddits
        if sub.casefold() in _SUBREDDIT_CHARACTER
    )
    note = f" Subreddit character matters ({character})." if character else ""
    # The engagement sentence is the stock prompt's as it always was; the
    # crypto prompt leaves it out, because its retail rule says the feed
    # carries no counts and one prompt must not say both.
    signal = " Engagement signal via upvote score and comment count." if engagement else ""
    return f"### Reddit posts — {rooms} (past 7 days)\nCommunity discussion.{signal}{note}"


def _build_system_message(
    *,
    ticker: str,
    start_date: str,
    end_date: str,
    news_block: str,
    stocktwits_block: str | None,
    reddit_block: str,
    subreddits: tuple[str, ...],
) -> str:
    """Assemble the sentiment-analyst system message with structured data blocks.

    ``stocktwits_block`` is ``None`` for a crypto asset (#395): the section and
    every instruction that reads it are left out, and the prompt says two
    sources, so the model is not told to weigh a signal it was never given.
    """
    with_stocktwits = stocktwits_block is not None
    count = "three" if with_stocktwits else "two"
    stocktwits_section = (
        f"""### StockTwits messages — retail-trader social platform indexed by cashtag
Fast-moving signal. Each message carries a user-labeled sentiment tag (Bullish / Bearish / no-label) plus the message body.

<start_of_stocktwits>
{stocktwits_block}
<end_of_stocktwits>

"""
        if with_stocktwits
        else ""
    )
    retail_rule = (
        "1. **Read the StockTwits Bullish/Bearish ratio as a leading retail-sentiment signal.** A 70/30 bullish/bearish split is moderately bullish; ≥90/10 may indicate over-extension and contrarian risk; 50/50 is uncertainty. Sample size matters — base rates on the actual message count, not percentages alone."
        if with_stocktwits
        else "1. **Read the Reddit posts as the retail-sentiment signal.** The balance of bullish and bearish posts, read from their titles and body excerpts, is the leading retail read here (the feed carries no upvote or comment counts, so do not infer engagement); a room that is uniformly euphoric may indicate over-extension and contrarian risk. Sample size matters — base rates on the actual post count, not impressions."
    )
    divergence_rule = (
        "If news framing is bearish but StockTwits is overwhelmingly bullish, that mismatch is itself a signal"
        if with_stocktwits
        else "If news framing is bearish but the subreddits are overwhelmingly bullish, that mismatch is itself a signal"
    )
    event_example = (
        'A news headline ("Nvidia announces $500M Corning deal")'
        if with_stocktwits
        else 'A news headline ("spot ETFs record their largest daily inflow")'
    )
    opinion_example = (
        'a StockTwits post ("buying NVDA, this is going to moon")'
        if with_stocktwits
        else 'a Reddit post ("buying the dip, this is going to moon")'
    )
    limits_rule = (
        'If StockTwits returned only a handful of messages, or one or more sources returned an "<unavailable>" placeholder'
        if with_stocktwits
        else 'If the subreddits returned only a handful of posts, or one or more sources returned an "<unavailable>" placeholder'
    )
    catalysts = (
        "news of upcoming earnings, product launches, competitive threats, macro headlines, etc"
        if with_stocktwits
        else "ETF flows, exchange or regulatory headlines, protocol upgrades and hacks, liquidation cascades, macro and rates headlines, etc"
    )
    weighting_rule = (
        "3. **Weight Reddit posts by engagement.** A 400-upvote / 200-comment thread reflects community attention; a 3-upvote post is noise. Read the body excerpts for context — the title alone often misleads."
        if with_stocktwits
        else "3. **Weight Reddit posts by substance.** Read the body excerpts for context — the title alone often misleads; a post that argues from something checkable (a flow, a filing, an on-chain fact) outweighs a bare opinion."
    )
    return f"""You are a financial market sentiment analyst. Your task is to produce a comprehensive sentiment report for {ticker} covering the period from {start_date} to {end_date}, drawing on {count} complementary data sources that have already been collected for you.

## Data sources (pre-fetched, in this prompt)

### News headlines — Yahoo Finance, past 7 days
Institutional framing. Fact-driven, slower-moving signal.

<start_of_news>
{news_block}
<end_of_news>

{stocktwits_section}{_reddit_heading(subreddits, engagement=with_stocktwits)}

<start_of_reddit>
{reddit_block}
<end_of_reddit>

## How to analyze this data (best practices)

{retail_rule}

2. **Look for cross-source divergences.** {divergence_rule} — it can mean retail is leaning into a thesis the news flow hasn't caught up to (or vice versa, that retail is chasing while institutions are cautious).

{weighting_rule}

4. **Distinguish opinion from event.** {event_example} is an event; {opinion_example} is opinion. Both are inputs but should be weighted differently in your conclusions.

5. **Identify recurring narrative themes.** What topic keeps coming up across sources? That's the dominant narrative driving current sentiment.

6. **Be honest about data limits.** {limits_rule}, the sentiment read is less robust — flag this explicitly in the `confidence` field and the narrative. If the sources are silent on a given subreddit, say so.

7. **Identify catalysts and risks** that emerge across sources — {catalysts}.

8. **Past sentiment is not predictive.** Frame your conclusions as signal for the trader to weigh alongside fundamentals and technicals, not as a price call.

## Output fields

Fill the following fields:

- **overall_band**: Exactly one of Bullish / Mildly Bullish / Neutral / Mixed / Mildly Bearish / Bearish. Use Mixed when sources point in clearly different directions; Neutral only when all sources are genuinely silent.
- **overall_score**: A number from 0 (maximally bearish) to 10 (maximally bullish); 5 is neutral. Keep it consistent with overall_band.
- **confidence**: low / medium / high, based on data quality and sample size.
- **narrative**: Full source-by-source breakdown, divergences, dominant narrative themes, catalysts and risks, and a markdown summary table of key sentiment signals (direction, source, supporting evidence).

{get_language_instruction()}"""


# ---------------------------------------------------------------------------
# Backwards-compatibility shim
# ---------------------------------------------------------------------------
def create_social_media_analyst(llm):
    """Deprecated alias for :func:`create_sentiment_analyst`.

    Kept so existing code that imports ``create_social_media_analyst``
    continues to work.

    .. deprecated::
        Import :func:`create_sentiment_analyst` directly instead.
    """
    import warnings

    warnings.warn(
        "create_social_media_analyst is deprecated and will be removed in a "
        "future version. Use create_sentiment_analyst instead.",
        DeprecationWarning,
        stacklevel=2,
    )
    return create_sentiment_analyst(llm)
