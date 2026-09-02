<p align="center">
  <img src="assets/TauricResearch.png" style="width: 60%; height: auto;">
</p>

<div align="center" style="line-height: 1;">
  <a href="https://arxiv.org/abs/2412.20138" target="_blank"><img alt="arXiv" src="https://img.shields.io/badge/arXiv-2412.20138-B31B1B?logo=arxiv"/></a>
  <a href="https://discord.com/invite/hk9PGKShPK" target="_blank"><img alt="Discord" src="https://img.shields.io/badge/Discord-TradingResearch-7289da?logo=discord&logoColor=white&color=7289da"/></a>
  <a href="./assets/wechat.png" target="_blank"><img alt="WeChat" src="https://img.shields.io/badge/WeChat-TauricResearch-brightgreen?logo=wechat&logoColor=white"/></a>
  <a href="https://x.com/TauricResearch" target="_blank"><img alt="X Follow" src="https://img.shields.io/badge/X-TauricResearch-white?logo=x&logoColor=white"/></a>
  <br>
  <a href="https://github.com/TauricResearch/" target="_blank"><img alt="Community" src="https://img.shields.io/badge/Join_GitHub_Community-TauricResearch-14C290?logo=discourse"/></a>
</div>

<div align="center">
  <!-- Keep these links. Translations will automatically update with the README. -->
  <a href="https://www.readme-i18n.com/TauricResearch/TradingAgents?lang=de">Deutsch</a> | 
  <a href="https://www.readme-i18n.com/TauricResearch/TradingAgents?lang=es">Español</a> | 
  <a href="https://www.readme-i18n.com/TauricResearch/TradingAgents?lang=fr">français</a> | 
  <a href="https://www.readme-i18n.com/TauricResearch/TradingAgents?lang=ja">日本語</a> | 
  <a href="https://www.readme-i18n.com/TauricResearch/TradingAgents?lang=ko">한국어</a> | 
  <a href="https://www.readme-i18n.com/TauricResearch/TradingAgents?lang=pt">Português</a> | 
  <a href="https://www.readme-i18n.com/TauricResearch/TradingAgents?lang=ru">Русский</a> | 
  <a href="https://www.readme-i18n.com/TauricResearch/TradingAgents?lang=zh">中文</a>
</div>

---

# TradingAgents: Multi-Agents LLM Financial Trading Framework

## News
- [2026-06] **TradingAgents v0.3.0** released with a verified data-access contract, an expanded provider registry (NVIDIA, Kimi, Groq, Mistral, Bedrock, and any OpenAI-compatible endpoint), FRED and Polymarket data vendors, a current-generation model catalog, and a CI gate. See [CHANGELOG.md](CHANGELOG.md) for the full list.
- [2026-05] **TradingAgents v0.2.5** released with the grounded Sentiment Analyst, GPT-5.5 etc. model coverage, Qwen/GLM/MiniMax dual-region support, `TRADINGAGENTS_*` env-var configurability with API-key auto-detection, remote Ollama support, non-US alpha benchmarks, and ticker path-traversal hardening.
- [2026-04] **TradingAgents v0.2.4** released with structured-output agents (Research Manager, Trader, Portfolio Manager), LangGraph checkpoint resume, persistent decision log, DeepSeek/Qwen/GLM/Azure provider support, Docker, and a Windows UTF-8 encoding fix.
- [2026-03] **TradingAgents v0.2.3** released with multi-language support, GPT-5.4 family models, unified model catalog, backtesting date fidelity, and proxy support.
- [2026-03] **TradingAgents v0.2.2** released with GPT-5.4/Gemini 3.1/Claude 4.6 model coverage, five-tier rating scale, OpenAI Responses API, Anthropic effort control, and cross-platform stability.
- [2026-02] **TradingAgents v0.2.0** released with multi-provider LLM support (GPT-5.x, Gemini 3.x, Claude 4.x, Grok 4.x) and improved system architecture.
- [2026-01] **Trading-R1** [Technical Report](https://arxiv.org/abs/2509.11420) released, with [Terminal](https://github.com/TauricResearch/Trading-R1) expected to land soon.

<div align="center">
<a href="https://www.star-history.com/#TauricResearch/TradingAgents&Date">
 <picture>
   <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/svg?repos=TauricResearch/TradingAgents&type=Date&theme=dark" />
   <source media="(prefers-color-scheme: light)" srcset="https://api.star-history.com/svg?repos=TauricResearch/TradingAgents&type=Date" />
   <img alt="TradingAgents Star History" src="https://api.star-history.com/svg?repos=TauricResearch/TradingAgents&type=Date" style="width: 80%; height: auto;" />
 </picture>
</a>
</div>

> 🎉 **TradingAgents** officially released! We have received numerous inquiries about the work, and we would like to express our thanks for the enthusiasm in our community.
>
> So we decided to fully open-source the framework. Looking forward to building impactful projects with you!

<div align="center">

🚀 [TradingAgents](#tradingagents-framework) | ⚡ [Installation & CLI](#installation-and-cli) | 🎬 [Demo](https://www.youtube.com/watch?v=90gr5lwjIho) | 📦 [Package Usage](#tradingagents-package) | 🤝 [Contributing](#contributing) | 📄 [Citation](#citation)

</div>

## TradingAgents Framework

TradingAgents is a multi-agent trading framework that mirrors the dynamics of real-world trading firms. By deploying specialized LLM-powered agents: from fundamental analysts, sentiment experts, and technical analysts, to trader, risk management team, the platform collaboratively evaluates market conditions and informs trading decisions. Moreover, these agents engage in dynamic discussions to pinpoint the optimal strategy.

<p align="center">
  <img src="assets/schema.png" style="width: 100%; height: auto;">
</p>

> TradingAgents framework is designed for research purposes. Trading performance may vary based on many factors, including the chosen backbone language models, model temperature, trading periods, the quality of data, and other non-deterministic factors. [It is not intended as financial, investment, or trading advice.](https://tauric.ai/disclaimer/)

Our framework decomposes complex trading tasks into specialized roles.

### Analyst Team
- Fundamentals Analyst: Evaluates company financials and performance metrics, identifying intrinsic values and potential red flags.
- Sentiment Analyst: Aggregates news headlines, StockTwits, and Reddit chatter into a single sentiment read to gauge short-term market mood.
- News Analyst: Monitors global news and macroeconomic indicators, interpreting the impact of events on market conditions.
- Technical Analyst: Utilizes technical indicators (like MACD and RSI) to detect trading patterns and forecast price movements.

<p align="center">
  <img src="assets/analyst.png" width="100%" style="display: inline-block; margin: 0 2%;">
</p>

### Researcher Team
- Comprises both bullish and bearish researchers who critically assess the insights provided by the Analyst Team. Through structured debates, they balance potential gains against inherent risks.

<p align="center">
  <img src="assets/researcher.png" width="70%" style="display: inline-block; margin: 0 2%;">
</p>

### Trader Agent
- Composes reports from the analysts and researchers to make informed trading decisions, determining the timing and magnitude of trades.

<p align="center">
  <img src="assets/trader.png" width="70%" style="display: inline-block; margin: 0 2%;">
</p>

### Risk Management and Portfolio Manager
- Continuously evaluates portfolio risk by assessing market volatility, liquidity, and other risk factors. The risk management team evaluates and adjusts trading strategies, providing assessment reports to the Portfolio Manager for final decision.
- The Portfolio Manager approves/rejects the transaction proposal. If approved, the order will be sent to the simulated exchange and executed.

<p align="center">
  <img src="assets/risk.png" width="70%" style="display: inline-block; margin: 0 2%;">
</p>

## Installation and CLI

### Installation

Clone TradingAgents:
```bash
git clone https://github.com/TauricResearch/TradingAgents.git
cd TradingAgents
```

Create a virtual environment in any of your favorite environment managers:
```bash
conda create -n tradingagents python=3.12
conda activate tradingagents
```

Install the package and its dependencies:
```bash
pip install .
```

### Docker

Alternatively, run with Docker:
```bash
cp .env.example .env  # add your API keys
docker compose run --rm tradingagents
```

For local models with Ollama:
```bash
docker compose --profile ollama run --rm tradingagents-ollama
```

### Required APIs

TradingAgents supports multiple LLM providers. Set the API key for your chosen provider:

```bash
export OPENAI_API_KEY=...          # OpenAI (GPT)
export GOOGLE_API_KEY=...          # Google (Gemini)
export ANTHROPIC_API_KEY=...       # Anthropic (Claude)
export XAI_API_KEY=...             # xAI (Grok)
export DEEPSEEK_API_KEY=...        # DeepSeek
export DASHSCOPE_API_KEY=...       # Qwen — International (dashscope-intl.aliyuncs.com)
export DASHSCOPE_CN_API_KEY=...    # Qwen — China (dashscope.aliyuncs.com)
export ZHIPU_API_KEY=...           # GLM via Z.AI (international)
export ZHIPU_CN_API_KEY=...        # GLM via BigModel (China, open.bigmodel.cn)
export MINIMAX_API_KEY=...         # MiniMax — Global (api.minimax.io)
export MINIMAX_CN_API_KEY=...      # MiniMax — China (api.minimaxi.com)
export OPENROUTER_API_KEY=...      # OpenRouter
export ALPHA_VANTAGE_API_KEY=...   # Alpha Vantage
```

For Azure OpenAI, copy `.env.enterprise.example` to `.env.enterprise` and fill in your credentials.

For AWS Bedrock, install the extra with `pip install ".[bedrock]"`, set `llm_provider: "bedrock"`, configure AWS credentials (environment variables, `~/.aws/credentials`, or an IAM role) and `AWS_DEFAULT_REGION`, and use a Bedrock model ID, e.g. `us.anthropic.claude-opus-4-8-v1:0`.

For local models, configure Ollama with `llm_provider: "ollama"`. The default endpoint is `http://localhost:11434/v1`; set `OLLAMA_BASE_URL` to point at a remote `ollama-serve`. Pull models with `ollama pull <name>`, and pick "Custom model ID" in the CLI for any model not listed by default.

For any other OpenAI-compatible server (vLLM, LM Studio, llama.cpp, or a custom relay), use `llm_provider: "openai_compatible"` and set the endpoint via `backend_url` (or `TRADINGAGENTS_LLM_BACKEND_URL`), e.g. `http://localhost:8000/v1` for vLLM or `http://localhost:1234/v1` for LM Studio. The model is whatever your server serves. No key is needed for local servers; set `OPENAI_COMPATIBLE_API_KEY` when the endpoint requires one.

Alternatively, copy `.env.example` to `.env` and fill in your keys:
```bash
cp .env.example .env
```

### CLI Usage

Launch the interactive CLI:
```bash
tradingagents          # installed command
python -m cli.main     # alternative: run directly from source
```
You will see a screen where you can select your desired tickers, analysis date, LLM provider, research depth, and more.

### Markets and tickers

TradingAgents works with any market Yahoo Finance covers, using the exchange-suffixed ticker. Company identity and the alpha benchmark resolve automatically per market.

- US: `AAPL`, `SPY`
- Hong Kong: `0700.HK` · Tokyo: `7203.T` · London: `AZN.L`
- India: `RELIANCE.NS`, `.BO` · Canada: `.TO` · Australia: `.AX`
- China A-shares: Shanghai `.SS`, Shenzhen `.SZ` (e.g. `600519.SS` for Kweichow Moutai)
- Crypto: `BTC-USD`, `ETH-USD`

<p align="center">
  <img src="assets/cli/cli_init.png" width="100%" style="display: inline-block; margin: 0 2%;">
</p>

An interface will appear showing results as they load, letting you track the agent's progress as it runs.

<p align="center">
  <img src="assets/cli/cli_news.png" width="100%" style="display: inline-block; margin: 0 2%;">
</p>

<p align="center">
  <img src="assets/cli/cli_transaction.png" width="100%" style="display: inline-block; margin: 0 2%;">
</p>

## TradingAgents Package

### Implementation Details

We built TradingAgents with LangGraph to ensure flexibility and modularity. The framework supports multiple LLM providers: OpenAI, Google, Anthropic, xAI, DeepSeek, Qwen (Alibaba DashScope, international and China endpoints), GLM (Zhipu), MiniMax (global + China), OpenRouter, Ollama for local models, and Azure OpenAI for enterprise.

### Python Usage

To use TradingAgents inside your code, you can import the `tradingagents` module and initialize a `TradingAgentsGraph()` object. The `.propagate()` function will return a decision. You can run `main.py`, here's also a quick example:

```python
from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.default_config import DEFAULT_CONFIG

ta = TradingAgentsGraph(debug=True, config=DEFAULT_CONFIG.copy())

# forward propagate
_, decision = ta.propagate("NVDA", "2026-01-15")
print(decision)
```

You can also adjust the default configuration to set your own choice of LLMs, debate rounds, etc.

```python
from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.default_config import DEFAULT_CONFIG

config = DEFAULT_CONFIG.copy()
config["llm_provider"] = "openai"        # e.g. openai, google, anthropic, deepseek, groq, ollama; openai_compatible covers any OpenAI-compatible endpoint (vLLM, LM Studio, llama.cpp, ...)
config["deep_think_llm"] = "gpt-5.5"     # Model for complex reasoning
config["quick_think_llm"] = "gpt-5.4-mini" # Model for quick tasks
config["max_debate_rounds"] = 2

ta = TradingAgentsGraph(debug=True, config=config)
_, decision = ta.propagate("NVDA", "2026-01-15")
print(decision)
```

See `tradingagents/default_config.py` for all configuration options.

Crypto assets (e.g. `BTC-USD`) additionally surface two news-analyst data
sources — BTC/ETH US spot-ETF daily flows (`crypto_etf_flows`, vendor chain
`"sosovalue,farside"`) and the Crypto Fear & Greed Index (alternative.me,
`crypto_sentiment`, keyless). ETF flows come from the SoSoValue OpenAPI (free
Demo key from the sosovalue.com developer dashboard; set `SOSOVALUE_API_KEY`).
Farside stays registered as a keyless fallback, but farside.co.uk has been
behind a Cloudflare JS challenge since 2026-07-27, so with the key unset the
category currently degrades to a stale cached Farside snapshot (up to 14 days
old) if one exists, and otherwise to the no-data sentinel. Both categories degrade to
that sentinel rather than aborting; the stock path is unchanged. A recognized
crypto risk asset without its own spot ETF (e.g. SOL) gets BTC flows as a
market-wide proxy; a stablecoin or unrecognized symbol gets a no-signal note.

Crypto assets also have one **market**-analyst data source available:
options-implied volatility from Deribit's public API (`options_data`, vendor
`deribit`, keyless). It reports the DVOL index with its 30-day min/max range and
its **365-day percentile** — separate windows on separate lines, each naming its
own sample count, because a percentile is a claim about the volatility regime and
a one-month lookback sits mid-range even at a multi-year extreme — plus the ATM
(50-delta) implied vol and the 25-delta call/put vols, each shown with the two
strikes it was interpolated between, and the risk reversal (RR25) computed from
them. The range and the percentile each need a window holding enough readings to
support them; below that the report states the shortfall rather than computing a
figure that would only describe how much data arrived. A point the chain cannot
bracket is reported `n/a` rather than extrapolated or guessed, and so is one whose
surrounding quotes are not a monotone smile. For the two **25Δ wings** those are
reported as the different facts they are — a thin book against the signature of a
collapsed or stale mark, since only the second says a listed quote should not be
trusted. Where a bracketing attempt was possible at all, the ATM point instead
names both causes together, because it is attempted on the call curve and then on
the put curve, so no single guard explains the miss; with neither curve carrying
two usable quotes it says that plainly and names no guard.

The chain figures come from **one expiry inside a bounded band around 30 days**
(`MAX_TENOR_DISTANCE_DAYS`, currently ±15, and never inside the 7-day pin-noise
floor). Normally that is the eligible expiry nearest 30 days; if it cannot be
used, the next eligible one is, labelled as such and with its tenor printed — one
step only. When neither of the two brackets both wings, the one carrying more of
the three smile points wins and a tie leaves the nearer expiry in place, so a
barren nearest expiry no longer hides a neighbour that had an ATM point and a wing
to show. Nothing outside the band is read at all: a risk reversal is not
comparable across tenors, so a thinned book yields **no skew** rather than a
96-day figure presented under a 30-day heading. Only contracts with open interest
enter the smile, since an unheld strike is where a stale or purely modelled mark
lives. The two halves fail independently, so a DVOL outage still leaves the skew
and vice versa; losing both degrades the category to the sentinel — as does losing
DVOL alone on a date, or for a proxied asset, where the chain is withheld by
design. Whenever no risk reversal is in the report — withheld by policy, a chain
that yielded no usable surface, or wings the chain does not supply — the closing
one-line summary states that and why, and such a chain also carries its own header
caveat like the withheld cases do. **Either half's absence is disclosed this way**,
and the closing line states the DVOL level itself rather than only its percentile,
so a feed whose window is too thin to rank is not silent in the same way an outage
is. That line also carries the chain degradations that change which quantity the
figures describe: a fallback expiry and a missing ATM point unconditionally, and —
only where a risk reversal is actually printed, since otherwise the line states no
wing vol to qualify — each 25Δ wing interpolated between strikes further apart
than 10% of the forward, naming both when both are that wide. All of this exists
because that italic line is what a downstream summariser keeps when it drops the
body, so an absence signalled only by a missing clause is exactly what does not
survive the hop. Counts and the latest reading are labelled **usable**, and the two
rejection classes — a non-positive reading, and a candle whose low is not positive or
whose open or close falls
outside its own high/low range — are each disclosed by count and by the newest date they
reached, so a window this module partially emptied is never described as a sparse
calendar window and a rejection newer than the reading on show cannot silently
contradict it. The second class is what catches a **reordered candle**, which the
length-and-finiteness shape check cannot see: permute the row and the day's low
reads as its close, on every row, indefinitely.
A third disclosure covers a shortfall neither the feed nor this module caused: when
Deribit answers with a **continuation cursor** the response is only its newest page,
so the report says the older part of the fetch was never delivered rather than
letting a window's own count be read as the index publishing sparsely. It names no
boundary date, because the readings this module kept are not the ones delivery
stopped at, and it says a count *may* be short rather than that the counts *are* —
a cursor only shortens a window whose candles exceed the page cap.

Past `MAX_DVOL_STALENESS_DAYS` (14) the DVOL half is **withheld rather than
caveated**. A level is only served while it is recent enough to describe the current
regime, on the same judgement the withheld historical chain rests on: a caveat has to
survive every downstream summarisation hop and the number it qualifies does not.
Before this bound the only ceiling was however far the fetch happened to reach, so a
feed stalled for weeks headlined a weeks-old print *and* ranked it, since the
365-day window still held enough readings to compute a percentile.

Chain rows are matched against the **currency that was requested**. The
`get_book_summary_by_currency` payload names its underlying nowhere but the
instrument names, so a misrouted or mis-served response would otherwise render one
currency's forward and smile under another's heading, with nothing in the report
contradicting it. The **Expiry used** line also states how many contracts Deribit
lists for the selected expiry, so a thin smile can be told apart from a chain this
module's own open-interest policy thinned.

This vendor reads chains for BTC and ETH, so other recognized crypto risk
assets get BTC's **DVOL level** as a market-wide crypto-vol proxy, on the same
rule as ETF flows; the skew is withheld for them, because a risk reversal measures
demand for downside in one specific underlying and does not transfer. No rendered
line claims Deribit itself lists nothing for those symbols — nothing at runtime
checks that.

The DVOL history is dated and filtered to `curr_date`, and its latest reading is
always printed with an as-of date, since that clause is the one downstream agents
quote on its own. The chain endpoint takes no date, so it is **withheld when
`curr_date` is earlier than today** and the report says so, because quoting the
present chain on a past date is future information. A `curr_date` up to
`MAX_FUTURE_DAYS` (1) later than the UTC clock — which callers deriving it from a
local clock east of UTC produce for the first hours of each day — is served with a
note explaining that the chain is not later than the analysis date: it either
predates that date outright or, when the UTC clock reaches the date while the
report is being built, falls inside it. The note says which. Further ahead than that
is a bad argument rather than a timezone, so the chain is withheld again. The chain
also re-reads the clock immediately before fetching, so a run whose DVOL half timed
out across UTC midnight cannot serve the next day's book for `curr_date`.

Text this vendor did not author — Deribit's error messages and the caller-supplied
`asset` — is flattened before it is interpolated:
whitespace collapsed, mid-line markdown markers removed, length capped (an unusable
`curr_date` is refused with the shared `INVALID_CURR_DATE` sentinel before any of
this runs, and that sentinel flattens its own echo). The report is assembled into an LLM
prompt, so a fragment carrying line breaks could otherwise open a forged heading or
a second `_Reading:_` line above the real one, and the forgery is what a downstream
summariser would quote. The flattening is applied where the fragment enters the message rather
than only where the report renders it, because a failure in an optional category
reaches the model through the router as `DATA_UNAVAILABLE: ... ({error})` too.

This vendor shipped **disabled** and was **cut over to `"deribit"` on
2026-08-12**; it is now the default and needs no opt-in. Being keyless, shipping
it on at merge time would have changed a running deployment's analyst input
surface — a new tool, a new prompt paragraph, a new report section — the moment
the code landed, with no server-side action to date the change from. The dated
cutover is that action, so a later review can attribute a behaviour change to it.

Two further news-analyst sources ride on the same SoSoValue key
(`SOSOVALUE_API_KEY`, the one already used for ETF flows). The calendar takes no
asset argument and is bound on the **stock path too** — CPI/NFP/PCE releases are
event risk for equities as much as for crypto; treasuries is a BTC-holdings feed
and stays crypto-only. Both are still gated on their own category being enabled:

- **US economic calendar** (`economic_calendar`, vendor `sosovalue`): scheduled
  releases from `curr_date` itself through the next two weeks with consensus
  forecasts (an event scheduled today is the most decision-relevant row the
  report carries, so the window includes it), and the trailing window's
  releases with actual-vs-forecast surprises, for a curated whitelist
  of high-signal events (CPI/Core CPI, Nonfarm Payrolls, Initial Jobless
  Claims, Core PCE, GDP, Retail Sales — exact live-verified names; the API has
  no importance field, so the whitelist is the importance filter). Calendar
  names outside the whitelist appear as name-only lines. Scheduled rows render
  forecast and previous but **never an actual**, and released figures appear
  only on or before `curr_date`, so a backtest date cannot see a future print.
  Actuals, forecasts and previous values are all the provider's *current*
  figures rather than point-in-time snapshots — macro actuals get revised, and
  the report says so rather than implying the printed actual is as-published. The feed
  carries **no Fed rate-decision events at all** — the report flags that
  coverage gap so an empty FOMC row is never read as a quiet Fed schedule. The
  report frames event risk as a regime/risk modifier, not a directional signal.
- **Corporate BTC treasuries** (`btc_treasuries`, vendor `sosovalue`): combined
  and top-5 holdings across the 15 largest tracked holders (the provider lists
  by holdings, and every contributor's as-of date is printed because no
  filing-age cut is applied; individual disclosures are filtered to
  `curr_date` but that top-15 cut is not — it is the listing as ranked when
  the snapshot was fetched, so a historical date sees today's universe rather
  than that date's, which the report states) and the window's disclosed
  changes — buys and
  disposals, with an implied US$/BTC where a cost was filed against a filed
  quantity, and holdings-only disclosures rendered as the implied change from
  the prior filing (those carry no cost or implied price: the change spans
  everything since that filing, so no single filed cost belongs to it).
  Disclosure dates lag
  the underlying transactions and some companies file only monthly or
  quarterly, which the report caveats. Treasuries hold BTC only, so **ETH and
  other recognized risk assets get the BTC data as a labelled market-wide
  demand proxy**; stablecoins and unrecognized symbols get a no-signal note.

Both **ship disabled** (`"economic_calendar": "none"`, `"btc_treasuries":
"none"`): the SoSoValue key already sits on a deployed box for ETF flows, so
landing these enabled would change a running deployment's analyst input surface
the moment the code deploys, with no server-side action to date the change from.
Flip them together, as one deliberate cutover, and record when — `options_data`
already had its own dated cutover (2026-08-12), so a separate date here keeps
the two input-surface changes attributable apart:

```python
config["data_vendors"]["economic_calendar"] = "sosovalue"
config["data_vendors"]["btc_treasuries"] = "sosovalue"
```

Any data category can be switched off by setting its vendor to `"none"`:

```python
config["data_vendors"]["crypto_etf_flows"] = "none"  # stop calling the ETF-flow vendors
```

An optional category then returns the no-data sentinel without opening a
connection, and the analyst stops binding that tool at all. This is the only way
to disable a keyless vendor, which has no API key to unset. Core data categories
reject `"none"` rather than silently running without prices or fundamentals.

The ETF-flow vendors' fetch throttle (a 6-hour cache TTL) relies on
`data_cache_dir` persisting between runs; pointing it at a temporary filesystem
makes every call re-fetch.

## Persistence and Recovery

TradingAgents persists two kinds of state across runs.

### Decision log

The decision log is always on. Each completed run appends its decision to `~/.tradingagents/memory/trading_memory.md`. On the next run for the same ticker, TradingAgents fetches the realised return (raw and alpha vs SPY), generates a one-paragraph reflection, and injects the most recent same-ticker decisions plus recent cross-ticker lessons into the Portfolio Manager prompt, so each analysis carries forward what worked and what didn't.

Override the path with `TRADINGAGENTS_MEMORY_LOG_PATH`.

### Checkpoint resume

Checkpoint resume is opt-in via `--checkpoint`. When enabled, LangGraph saves state after each node so a crashed or interrupted run resumes from the last successful step instead of starting over. On a resume run you will see `Resuming from step N for <TICKER> on <date>` in the logs; on a new run you will see `Starting fresh`. Checkpoints are cleared automatically on successful completion.

Per-ticker SQLite databases live at `~/.tradingagents/cache/checkpoints/<TICKER>.db` (override the base with `TRADINGAGENTS_CACHE_DIR`). Use `--clear-checkpoints` to reset all of them before a run.

```bash
tradingagents analyze --checkpoint           # enable for this run
tradingagents analyze --clear-checkpoints    # reset before running
```

```python
config = DEFAULT_CONFIG.copy()
config["checkpoint_enabled"] = True
ta = TradingAgentsGraph(config=config)
_, decision = ta.propagate("NVDA", "2026-01-15")
```

## Reproducibility

TradingAgents is LLM-driven, so two runs of the same ticker and date can differ. This is expected for a research tool built on language models, not a defect. The variation comes from a few distinct sources, and it helps to separate them.

Language model sampling is non-deterministic. Even at a fixed temperature, providers do not guarantee byte-identical output across calls, and reasoning models (the default GPT-5.x family, and any thinking-mode model) vary the most because their internal reasoning is itself sampled.

Live data moves. News, StockTwits, and Reddit return different content as time passes, so a run today sees different inputs than a run last week even for the same historical trade date. Pin the analysis date to hold the price and indicator window fixed, but the social and news sources still reflect "now".

To reduce variation you can lower the sampling temperature. Set `temperature` in your config (or `TRADINGAGENTS_TEMPERATURE` in `.env`); lower values make models that honor it more repeatable. The current curated models are reasoning-first and largely ignore temperature, so for tighter reproducibility use a non-reasoning model, which you can set explicitly via the Custom model ID option.

A completion-token cap works the same way: set `max_tokens` in your config (or `TRADINGAGENTS_MAX_TOKENS` in `.env`, positive integer). Unset leaves each provider at its own default, which is risky through gateway providers such as OpenRouter — some upstreams treat a missing cap as "the model's full context" and deterministically reject every call (issue #177). The cap includes reasoning/thinking tokens.

```python
config = DEFAULT_CONFIG.copy()
config["llm_provider"] = "openai"
config["temperature"] = 0.0
# Reasoning models ignore temperature. For tighter reproducibility, set a
# non-reasoning deep/quick model explicitly (e.g. via the Custom model ID option).
```

What does not vary anymore: the analyzed company identity is resolved deterministically from the ticker before any agent runs, and the market analyst grounds exact price and indicator claims in a verified data snapshot. Earlier reports of "different companies" or fabricated price levels across runs are addressed by these two mechanisms.

Backtest results are not guaranteed to match any published figure. Returns depend on the model, the temperature, the date range, data quality, and the sampling above. Treat the framework as a research scaffold for studying multi-agent analysis, not as a strategy with a fixed, replicable return.

## Contributing

Contributions are welcome: bug fixes, documentation, and feature ideas; past contributions are credited per release in [`CHANGELOG.md`](CHANGELOG.md).

## Citation

Please reference our work if you find *TradingAgents* provides you with some help :)

```
@misc{xiao2025tradingagentsmultiagentsllmfinancial,
      title={TradingAgents: Multi-Agents LLM Financial Trading Framework}, 
      author={Yijia Xiao and Edward Sun and Di Luo and Wei Wang},
      year={2025},
      eprint={2412.20138},
      archivePrefix={arXiv},
      primaryClass={q-fin.TR},
      url={https://arxiv.org/abs/2412.20138}, 
}
```
