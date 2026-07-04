# Integration — 驅動 TradingAgents

本模組如何驅動**未修改的** TradingAgents 引擎（Direction 2：plugin、
`tradingagents/` 零修改），以及哪些模型跑哪些角色。

---

# Part 1 — 如何掛接

## 原則

`tradingagents/` 是黑盒依賴，我們永不修改它。兩個既有、可 override 的
extension points 負責把 perp 資料送*進去*、把引擎的決策讀*出來*：

1. **Context 進** —— 子類別化 `TradingAgentsGraph` 並 override
   `resolve_instrument_context()`，附加即時 perp snapshot。基底類別會把該字串
   注入 initial state，「觸及整個 graph」，因此每個 analyst、trader 與
   portfolio manager 都能對 funding / OI / position 推理。
2. **決策出** —— `propagate()` 回傳引擎的正常輸出：`final_state`。Phase 2 起
   `final_trade_decision` 內含 structured target JSON，由
   `parse_target_decision` 解析＋驗證（Phase 1 曾由 decision adapter 把 5-tier
   rating 映射為 `PerpTradeDecision`，已退役）。

從解析 seam 往下的一切（parse、RiskGate sizing／clamp、audit 記錄）都是確定性的。

## 依賴的引擎介面

來自 `tradingagents/graph/trading_graph.py` 與
`tradingagents/agents/schemas.py` —— 穩定的公開行為：

| Symbol | 角色 | 我們的用法 |
|---|---|---|
| `TradingAgentsGraph(selected_analysts, config, …)` | 建構子 | 子類別化。 |
| `.resolve_instrument_context(ticker, asset_type) -> str` | 建立注入每個 agent 的 per-instrument context 字串。 | **Override 點**——附加 perp snapshot。 |
| `._create_tool_nodes() -> dict[str, ToolNode]` | 註冊每個 analyst 可呼叫的 tools。 | **可選 override**——Phase 3 加即時 HL tool。 |
| `.propagate(company_name, trade_date, asset_type) -> (final_state, signal)` | 跑整個 graph。 | 以 `asset_type="crypto"` 呼叫。 |
| `final_state["final_trade_decision"]` | 渲染後的 `PortfolioDecision` markdown（`**Rating**: …`）。 | Adapter 輸入。 |
| `final_state["trader_investment_plan"]` | 渲染後的 `TraderProposal`（`action`、`entry_price`、`stop_loss`、`position_sizing`）。 | Adapter 輸入——價格水位。 |
| `PortfolioDecision` | `rating`（Buy/Overweight/Hold/Underweight/Sell）、`executive_summary`、`investment_thesis`、`price_target`、`time_horizon`。 | `intent`/`rationale` 的來源。 |
| `signal`（第二個回傳值） | `parse_rating(...)` → 5 tiers 之一。 | 便利用途；同一個 rating。 |

> 引擎本身不會原生輸出 perp 專屬欄位。Phase 2 由注入的 output-format 契約要求
> 引擎直接輸出 structured target JSON，再由確定性 RiskGate sizing／檢查
>（Phase 1 曾由 adapter 依 `PerpMarketContext` 確定性補上 `funding_view`／
> `target_size_pct`，已退役）。

## 執行流程

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
   ├─ parsed = parse_target_decision(final_state["final_trade_decision"], cfg)
   │     └─ structured target JSON（DESIGN Part 2）；invalid → fail-closed maintain_current
   │
   └─ risk_gate.evaluate(parsed, account_equity, current, …)   (domains/perp/risk_gate.py)
         └─ 核准/clamp/風控拒絕(REJECTED)/fail-close(契約違規)；PR 3 的執行引擎消費核准的 margin%，
            下單數量於 plan-build 以新鮮 snapshot 重算（execution §6.2），
            gate 的 notional 欄位屬 audit-only
```

> **Phase 2 note:** Phase 1 的 `DecisionAdapter(ctx, position).to_perp_decision(...)`
> rating 映射已退役，由上面的 structured target 解析 ＋ 確定性 RiskGate 取代。

## `PortfolioDecision → PerpTradeDecision` mapping

> **Phase 2 note:** 本節的 rating → target 映射是 Phase 1 管線。Phase 2 起引擎改為直接輸出
> structured target JSON（`decision_mode` / `target_side` / `requested_target_margin_pct`），
> 不再使用 5-tier rating 與下表；新契約見 [DESIGN](./DESIGN.md) Part 2 與
> [phase2-spec](./phase2-spec.md)。本節保留供 Phase 1 對照。

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

## 子類別示意（illustrative）

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
# Phase 2 的決策出口（domains/perp — 不在 integration/ 之下）：
from contrib.hyperliquid_perp.domains.perp import risk_gate
from contrib.hyperliquid_perp.domains.perp.target_decision import parse_target_decision

parsed = parse_target_decision(final_state["final_trade_decision"], decision_cfg)
result = risk_gate.evaluate(
    parsed, account_equity=equity, current=current, risk=risk_cfg, decision_cfg=decision_cfg
)
```

> `integration/trading_graph.py` 就是**全部的**整合面（Phase 1 的
> `decision_adapter.py` 已隨 Phase 2 契約遷移退役）。`tradingagents/` 之下
> 沒有任何檔案被動到，模組因此能持續跟上 upstream。

## 之後原生使用 funding（Phase 3）

在同一個子類別 override `_create_tool_nodes()`，把即時 Hyperliquid tool 加進
相關 analyst 的 tool node——仍然是純附加、零核心修改。在那之前，注入的 context
文字已足夠讓 agents 在散文中把 funding / OI 納入考量，精確的 perp 欄位由
adapter 確定性補齊。

---

# Part 2 — 角色與模型分工

> **引擎今天支援什麼：** `TradingAgentsGraph` 只建立**兩個** LLM clients——
> `deep_thinking_llm` 與 `quick_thinking_llm`——由 `config["llm_provider"]` +
> `deep_think_llm` / `quick_think_llm` 決定，並且只把這兩個交給所有 agents。
> **沒有 per-agent model slot**。Direction 2 之下我們不修改引擎，所以
> **Option B 是受支援的路徑**。Option A 是可能的*未來* upstream 變更，
> 本模組不依賴它。

## Option A —— 修改上游（future / upstream change）

> 本模組不使用。需要修改 `GraphSetup` 與每個 agent factory 以接受 per-agent
> model——這是對 `tradingagents/` 的核心修改，Direction 2 避免這件事。下表
> 仍然有用，作為*目標*：它說明哪些角色值得用 `deep` 模型。

| Agent 角色 | 職責 | 最佳選擇 | 替代 |
|---|---|---|---|
| News Analyst | 讀新聞、找市場敘事、摘要重點事件 | Gemini Flash | Grok / Qwen |
| Sentiment Analyst | 判斷市場情緒與社群輿論方向 | Grok | Gemini / Qwen |
| Technical Analyst | 讀 K 線、RSI、MACD 等指標 | DeepSeek V3 | Qwen / Gemini Flash |
| Fundamental Analyst | 讀 filings、財報電話會、基本面 | Gemini Pro | Claude Sonnet |
| Bull Researcher | 全力論證買進 | DeepSeek R1 | Qwen |
| Bear Researcher | 全力論證賣出／做空風險 | Claude Sonnet | DeepSeek R1 |
| Trader | 綜合所有分析成交易提案 | OpenAI GPT | Claude Sonnet |
| Risk Manager | 審查槓桿、倉位、stop loss 合規 | Claude Sonnet | OpenAI GPT |
| Portfolio Manager | 最終核准／否決交易提案 | OpenAI GPT | Claude Sonnet |
| Reflection / Memory | 決策 post-mortem、更新 memory | Claude Sonnet | MiniMax / DeepSeek |
| Market Data | 提供價格、K 線、原始技術指標 | Alpha Vantage | Hyperliquid API |

## Option B —— 兩個模型，deep + quick（受支援路徑）

哪些角色跑 `deep`、哪些跑 `quick`：

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

關鍵角色（Risk Manager / Trader / Portfolio Manager）跑 deep 模型，便宜的分析
跑 quick 模型——用零核心修改拿到 per-agent 分工表約八成的精神。
