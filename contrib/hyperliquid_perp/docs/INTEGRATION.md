# Integration — driving TradingAgents

How this module drives the **unmodified** TradingAgents engine (Direction 2:
plugin, zero changes to `tradingagents/`), and which models run which roles.

---

# Part 1 — How it attaches

## Principle

`tradingagents/` is a black-box dependency. We never edit it. Two existing,
overridable extension points carry perp data *in* and the engine's rating *out*:

1. **Context in** — subclass `TradingAgentsGraph` and override
   `resolve_instrument_context()` to append the live perp snapshot. The base
   class injects that string into the initial state, where it "reaches the whole
   graph", so every analyst, the trader, and the portfolio manager can reason
   about funding / OI / position.
2. **Rating out** — `propagate()` returns the engine's normal output: a
   `final_state` whose `PortfolioDecision` carries a 5-tier rating. The
   **decision adapter** maps that into a `PerpTradeDecision`.

Everything from the adapter onward (rating→intent, sizing, SL/TP, leverage,
order params) is deterministic.

## Engine surface we depend on

From `tradingagents/graph/trading_graph.py` and
`tradingagents/agents/schemas.py` — stable public behaviour:

| Symbol | Role | We use it as |
|---|---|---|
| `TradingAgentsGraph(selected_analysts, config, …)` | Constructor | Subclassed. |
| `.resolve_instrument_context(ticker, asset_type) -> str` | Builds the per-instrument context string injected into every agent. | **Override point** — append perp snapshot. |
| `._create_tool_nodes() -> dict[str, ToolNode]` | Registers the tools each analyst can call. | **Optional override** — add a live HL tool for Phase 3. |
| `.propagate(company_name, trade_date, asset_type) -> (final_state, signal)` | Runs the graph. | Called with `asset_type="crypto"`. |
| `final_state["final_trade_decision"]` | Rendered `PortfolioDecision` markdown (`**Rating**: …`). | Adapter input. |
| `final_state["trader_investment_plan"]` | Rendered `TraderProposal` (`action`, `entry_price`, `stop_loss`, `position_sizing`). | Adapter input — price levels. |
| `PortfolioDecision` | `rating` (Buy/Overweight/Hold/Underweight/Sell), `executive_summary`, `investment_thesis`, `price_target`, `time_horizon`. | Source of `intent`/`rationale`. |
| `signal` (2nd return value) | `parse_rating(...)` → one of the 5 tiers. | Convenience; same rating. |

> The engine reasons in **prose + a 5-tier rating**; it does not natively emit
> perp fields like `funding_view` or `target_size_pct`. The adapter supplies
> those deterministically from the `PerpMarketContext` this module fetched.

## Run flow

```
contrib main.py
   │
   ├─ build PerpMarketContext + PerpPosition         (domains/perp)
   │
   ├─ graph = HyperliquidTradingGraph(config, perp_context=ctx)
   │     └─ override resolve_instrument_context() injects ctx text
   │
   ├─ final_state, signal = graph.propagate("BTC", date, asset_type="crypto")
   │     └─ UNCHANGED engine: analysts → researchers → trader → PM
   │        → PortfolioDecision (rating + thesis)
   │
   ├─ decision = DecisionAdapter(ctx, position).to_perp_decision(final_state)
   │     └─ PerpTradeDecision (intent, target_size_pct, funding_view, …)
   │
   └─ RiskGate.check(decision) → OrderPlanner → executor       (Phase 2+)
```

## `PortfolioDecision → PerpTradeDecision` mapping

The adapter does **not** read the rating in isolation: it diffs the engine's
*desired direction* against the *current* `PerpPosition` to pick an intent. The
engine already sees the account/position (it is in the injected context), so the
rating is account-aware; the adapter then makes the precise open/close/reduce
call deterministically.

### Rating → signed target exposure `T`

Each rating maps to a **bounded target** position (% of net value **committed as
margin**, signed: +long / −short). The numbers live in `adapter.target_size_pct`
config. Because margin can't exceed equity, a target is naturally ≤ 100% and a
tier like `buy=20` permits a leveraged position whose margin is 20% of equity —
it does **not** cap gross notional at 20% (which would forbid leverage).

| `PortfolioDecision.rating` | Target exposure `T` |
|---|---|
| Buy | +full (e.g. +20%) |
| Overweight | +partial (e.g. +10%) |
| Hold | keep current (`T = C`) — but de-risk an existing short to flat when `allow_short: false` |
| Underweight | −partial short (e.g. −10%) — mirrors Overweight; `allow_short: true` |
| Sell | −full short (e.g. −20%) — `allow_short: true` |

> **Shorting is tier-symmetric (revised decision #11):** the bearish tiers mirror
> the bullish ones — `Sell` is a full short (−20%) and `Underweight` a mild one
> (−10%). Both are gated by `allow_short`: when it is `false`, every negative
> target is forced to flat (`T = 0`). With `no_direct_flip`, a long position
> facing a bearish tier closes to flat first and re-enters short the next round.

### Rebalance to target → `intent`

> **Phase 1 policy: rebalance to a bounded per-tier target (not pyramiding).**
> The adapter moves the position *toward* `T` — it can size **up** if the rating
> is more bullish than the current position, or trim down — but never exceeds the
> tier's target, so it cannot stack unbounded. A `deadband` avoids churn, and
> `no_direct_flip` means an opposite-sign target closes first rather than
> flipping in one step.

Let `C` = current signed exposure — the position's committed margin as a % of net
value (`margin_used / account_value`, from `PerpPosition`), `d` = deadband:

| Condition | `intent` |
|---|---|
| `C = 0`, `T > 0` | `open_long` (to \|T\|) |
| `C = 0`, `T < 0` | `open_short` (to \|T\|) |
| same sign, `\|T\| > \|C\| + d` | `open_long` / `open_short` — **add toward T** |
| same sign, `\|T\| < \|C\| − d` | `reduce` |
| same sign, within deadband | `hold` |
| opposite sign (`no_direct_flip`) | `close` first (re-enter next round) |
| `T = 0`, `C ≠ 0` | `close` |

> Example: you are long 10% and the rating is **Buy** (target +20%) → `T > C` →
> `open_long`, sizing up toward 20%. If the rating were **Overweight**
> (target +10% ≈ current) → `hold`. Granularity is limited to the 5 rating tiers.

### Field-by-field

| `PerpTradeDecision` field | Source |
|---|---|
| `intent` | Rating + current position (tables above). |
| `confidence` | Rating tier strength (Buy/Sell = high, Overweight/Underweight = medium, Hold = low). |
| `target_size_pct` | Rating conviction × `risk.local.yaml` max — a *target*; RiskGate computes the real size. |
| `entry_zone` | `TraderProposal.entry_price` ± a configured band; `null` when `urgency=high`. |
| `invalidation_price` | `TraderProposal.stop_loss`, else a deterministic level from `PerpMarketContext`. |
| `urgency` | From the perp `market_regime` (volatile → high) and distance from `entry_price`. |
| `rationale` | `PortfolioDecision.executive_summary` (+ trimmed `investment_thesis`). |
| `key_risks` | Extracted from `investment_thesis` / the risk-debate state in `final_state`. |
| `market_regime` | Computed by `context_builder.py`, not the LLM. |
| `funding_view` | Deterministic from the funding z-score in `PerpMarketContext`. |

## Subclass sketch (illustrative)

```python
# contrib/hyperliquid_perp/integration/trading_graph.py
from tradingagents.graph.trading_graph import TradingAgentsGraph


class HyperliquidTradingGraph(TradingAgentsGraph):
    """Drive the unmodified engine with Hyperliquid perp context injected."""

    def __init__(self, *args, perp_context_text: str = "", **kwargs):
        self._perp_context_text = perp_context_text
        super().__init__(*args, **kwargs)

    def resolve_instrument_context(self, ticker: str, asset_type: str = "stock") -> str:
        base = super().resolve_instrument_context(ticker, asset_type)
        if not self._perp_context_text:
            return base
        return f"{base}\n\n## Perpetual market context\n{self._perp_context_text}"
```

```python
# contrib/hyperliquid_perp/integration/decision_adapter.py
from tradingagents.agents.utils.rating import parse_rating


class DecisionAdapter:
    def __init__(self, perp_context, position):
        self.ctx = perp_context
        self.position = position

    def to_perp_decision(self, final_state) -> "PerpTradeDecision":
        rating = parse_rating(final_state["final_trade_decision"])
        # rating + self.position → intent; fill the rest from self.ctx and
        # final_state["trader_investment_plan"] per the mapping table above.
        ...
```

> These two files are the **entire** integration surface. No file under
> `tradingagents/` is touched, so the module stays current with upstream.

## Natively using funding later (Phase 3)

Override `_create_tool_nodes()` in the same subclass to add a live Hyperliquid
tool to the relevant analyst's tool node — still additive, still zero core edits.
Until then, the injected context text is enough for the agents to factor funding
/ OI into their prose, and the adapter fills the precise perp fields
deterministically.

---

# Part 2 — Roles & model assignment

> **What the engine supports today:** `TradingAgentsGraph` builds exactly **two**
> LLM clients — `deep_thinking_llm` and `quick_thinking_llm` — from
> `config["llm_provider"]` + `deep_think_llm` / `quick_think_llm`, and hands only
> those two to every agent. There is **no per-agent model slot**. Under
> Direction 2 we do not edit the engine, so **Option B is the supported path**.
> Option A is a possible *future* upstream change, not something this module
> relies on.

## Option A — patch the source (future / upstream change)

> Not used by this module. Requires editing `GraphSetup` and each agent factory
> to accept a per-agent model — a core change to `tradingagents/`, which
> Direction 2 avoids. The table below is still useful as a *target*: it says
> which roles deserve the `deep` model.

| Agent role | Responsibility | Best fit | Alternatives |
|---|---|---|---|
| News Analyst | Read news, find market narratives, summarize key events | Gemini Flash | Grok / Qwen |
| Sentiment Analyst | Judge market sentiment and social-media direction | Grok | Gemini / Qwen |
| Technical Analyst | Read candles, RSI, MACD and other indicators | DeepSeek V3 | Qwen / Gemini Flash |
| Fundamental Analyst | Read filings, earnings calls, fundamentals | Gemini Pro | Claude Sonnet |
| Bull Researcher | Argue hard for buying | DeepSeek R1 | Qwen |
| Bear Researcher | Argue hard for selling/shorting risk | Claude Sonnet | DeepSeek R1 |
| Trader | Synthesize all analysis into a trade proposal | OpenAI GPT | Claude Sonnet |
| Risk Manager | Review leverage, position, stop loss for compliance | Claude Sonnet | OpenAI GPT |
| Portfolio Manager | Final approve/reject of the trade proposal | OpenAI GPT | Claude Sonnet |
| Reflection / Memory | Post-mortem decisions, update memory | Claude Sonnet | MiniMax / DeepSeek |
| Market Data | Provide price, candles, raw technical indicators | Alpha Vantage | Hyperliquid API |

## Option B — two models, deep + quick (supported path)

Which roles run on `deep` vs `quick`:

| `deep_think_llm` | `quick_think_llm` |
|---|---|
| Trader | News Analyst |
| Risk Manager | Technical Analyst |
| Bull / Bear Researcher | Sentiment Analyst |
| Portfolio Manager | Reflection |

```python
config["llm_provider"]    = "openrouter"
config["deep_think_llm"]  = "anthropic/claude-sonnet-4-6"
config["quick_think_llm"] = "deepseek/deepseek-chat"
```

The critical roles (Risk Manager / Trader / Portfolio Manager) run on the deep
model while cheap analysis runs on the quick model — ~80% of the spirit of the
per-agent table, with zero core changes.
