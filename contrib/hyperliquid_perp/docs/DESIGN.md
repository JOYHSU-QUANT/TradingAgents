# Design — API 與決策契約

本模組的資料契約：從 Hyperliquid 讀什麼（輸入）、產出什麼決策（輸出——Phase 2
structured target 與 Phase 1 legacy `PerpTradeDecision`）。決策如何從引擎產生，
見 [Integration](./INTEGRATION.md)。

---

# Part 1 — Hyperliquid API 與交易規則參考

本模組使用的 endpoints 與交易規則。它們在資料流中的位置見
[架構總覽](./README.md)。

## Info endpoint

`https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint`

公開市場資料不需要認證。帳戶讀取需要 **wallet address**（主帳戶地址——
*不是* agent / API-wallet 地址）。

| `type` | 用途 | 主要回傳欄位 | 認證 |
|---|---|---|---|
| `metaAndAssetCtxs` | 市場 snapshot：mark/index price、OI、funding、volume。Phase 1 最重要的一支呼叫——所有市場資料都來自這裡。 | `markPx`, `oraclePx`, `funding`, `openInterest`, `dayNtlVlm`, `prevDayPx`, `premium` | 公開 |
| `candleSnapshot` | OHLCV K 線歷史。Body：`req:{coin, interval:"4h", startTime, endTime}` | `o`, `h`, `l`, `c`, `v`, `t`, `T` | 公開 |
| `fundingHistory` | 歷史 funding rate。Body：`coin`、`startTime` | `fundingRate`, `premium`, `time` | 公開 |
| `predictedFundings` | 預測的下一期 funding rate，含各交易所的預測值可跨場所比較。 | `nextFunding`, `nextFundingTime` | 公開 |
| `clearinghouseState` | 帳戶狀態：margin、倉位、掛單。Body：`user`——**必須是 wallet address，不是 agent address**。 | `marginSummary`, `accountValue`, `withdrawable`, `assetPositions[].szi`, `entryPx`, `leverage`, `unrealizedPnl`, `liquidationPx`, `openOrders[]` | Wallet address |
| `userFillsByTime` | 今日成交，用來計算 realized PnL。Body：`user`、`startTime=今日 00:00 UTC`。 | `closedPnl`, `fee`, `side`, `sz`, `px`, `time` | Wallet address |
| `userFunding` | 今日 funding 收付。Body：`user`、`startTime=今日 00:00 UTC`；`delta` 要自己加總。 | `delta`, `time` | Wallet address |
| `orderStatus` | 查詢單一 order 的目前狀態（例如 limit 是否成交）。Body：`user`、`oid`。由 Phase 3 live 層用來輪詢 order 狀態（見 phase3-spec §7）。 | `status: open / filled / canceled / rejected` | Wallet address |

## Exchange endpoint

`https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/exchange-endpoint`

所有 actions 都需要簽名。（只有 Phase 3 用——Phase 1/2 不下單。）

| `action` | 用途 | 主要參數 | 簽名 |
|---|---|---|---|
| `order` | 下單：limit / market / TP / SL。TIF：`Alo`=post-only、`Ioc`=IOC、`Gtc`=GTC。 | `a`=asset index, `b`=isBuy, `p`=price, `s`=size, `r`=reduceOnly, `t.limit.tif` 或 `t.trigger` | 是 |
| `cancel` | 依 `oid` 取消 order。用於 timeout 後取消 limit；需要 asset index 與 oid。 | `a`=asset, `o`=oid | 是 |
| `cancelByCloid` | 依 client order id 取消。自行指定 `cloid` 時查找方便。 | `asset`, `cloid` | 是 |
| `scheduleCancel` | Dead man's switch：指定時間後自動取消所有掛單。`kill_switch.py` 依賴它；每輪刷新 deadline，process 掛掉時能自我保護。 | `time` = ms timestamp | 是 |
| `updateLeverage` | 更新槓桿倍數。開倉前確認槓桿與 `risk.yaml` 設定一致。 | `asset`, `isCross`, `leverage` | 是 |

---

## References

### Hyperliquid Trading

- Hyperliquid Docs — Order types
    
    [https://hyperliquid.gitbook.io/hyperliquid-docs/trading/order-types](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/order-types)
    
- Hyperliquid Docs — Take profit and stop loss orders
    
    [https://hyperliquid.gitbook.io/hyperliquid-docs/trading/take-profit-and-stop-loss-orders-tp-sl](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/take-profit-and-stop-loss-orders-tp-sl)
    
- Hyperliquid Docs — TP/SL FAQ
    
    [https://hyperliquid.gitbook.io/hyperliquid-docs/support/faq/trade-outcome-looks-incorrect/my-tp-sl-did-not-execute-correctly](https://hyperliquid.gitbook.io/hyperliquid-docs/support/faq/trade-outcome-looks-incorrect/my-tp-sl-did-not-execute-correctly)
    
- Hyperliquid Docs — Fees
    
    [https://hyperliquid.gitbook.io/hyperliquid-docs/trading/fees](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/fees)
    
- Hyperliquid Docs — Funding
    
    [https://hyperliquid.gitbook.io/hyperliquid-docs/trading/funding](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/funding)
    
- Hyperliquid Docs — Entry price and PnL
    
    [https://hyperliquid.gitbook.io/hyperliquid-docs/trading/entry-price-and-pnl](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/entry-price-and-pnl)
    

### Hyperliquid Margin / Liquidation

- Hyperliquid Docs — Contract specifications
    
    [https://hyperliquid.gitbook.io/hyperliquid-docs/trading/contract-specifications](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/contract-specifications)
    
- Hyperliquid Docs — Perpetual assets
    
    [https://hyperliquid.gitbook.io/hyperliquid-docs/trading/perpetual-assets](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/perpetual-assets)
    
- Hyperliquid Docs — Margining
    
    [https://hyperliquid.gitbook.io/hyperliquid-docs/trading/margining](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/margining)
    
- Hyperliquid Docs — Margin tiers
    
    [https://hyperliquid.gitbook.io/hyperliquid-docs/trading/margin-tiers](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/margin-tiers)
    
- Hyperliquid Docs — Liquidations
    
    [https://hyperliquid.gitbook.io/hyperliquid-docs/trading/liquidations](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/liquidations)
    
- Hyperliquid Docs — Robust price indices
    
    [https://hyperliquid.gitbook.io/hyperliquid-docs/trading/robust-price-indices](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/robust-price-indices)
    

### Hyperliquid API

- Hyperliquid Docs — Exchange endpoint
    
    [https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/exchange-endpoint](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/exchange-endpoint)
    
- Hyperliquid Docs — Info endpoint
    
    [https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint)
    
- Hyperliquid Docs — Info endpoint / Perpetuals
    
    [https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/perpetuals](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/perpetuals)
    
- Hyperliquid Docs — WebSocket subscriptions
    
    [https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/websocket/subscriptions](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/websocket/subscriptions)
    
- Hyperliquid Docs — Tick and lot size
    
    [https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/tick-and-lot-size](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/tick-and-lot-size)
    
- Hyperliquid Docs — Error responses
    
    [https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/error-responses](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/error-responses)
    
- Hyperliquid Docs — Rate limits and user limits
    
    [https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/rate-limits-and-user-limits](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/rate-limits-and-user-limits)
    

---

## Hyperliquid Order Types

### Order types 比較

| Order type | 定義 | 是否立刻送進 book | 是否會掛單 | 適用場景 | 策略注意事項 |
| --- | --- | --- | --- | --- | --- |
| **Market** | 以當前市場可成交價格立即成交 | 是 | 否 | 立刻進場、立刻平倉、緊急減倉 | 速度快但滑價不可控；實作上常用 aggressive `IOC limit` 模擬，避免無上限滑價 |
| **Limit** | 指定價格或更好價格成交 | 是 | 可能 | 掛買/掛賣、被動進場、MM quote | 行為取決於 TIF：`Alo`、`Ioc`、`Gtc` |
| **Stop Market** | 到達 trigger price 後，觸發 market order | 否，觸發後才送 | 否 | 止損、突破追單 | 觸發後成交價格不保證，行情急時可能滑價 |
| **Stop Limit** | 到達 trigger price 後，送出 limit order | 否，觸發後才送 | 可能 | 想止損但限制最差成交價 | 可能因 limit price 太保守而沒成交，風控上要小心 |
| **Take Market** | 到達 take-profit trigger 後，觸發 market order | 否，觸發後才送 | 否 | 止盈、快速落袋 | 成交確定性高，但可能有滑價 |
| **Take Limit** | 到達 take-profit trigger 後，送出 limit order | 否，觸發後才送 | 可能 | 止盈但想控制成交價 | 可能觸發後掛著沒成交，利潤可能回吐 |
| **Scale** | 在一段價格區間分批放多張 limit orders | 是 | 是 | 分批建倉、分批出場、網格式掛單 | 本質是多張 limit；要管理殘單、倉位累積、取消邏輯 |
| **TWAP** | 把大單拆成小單，按時間分批執行 | 依執行時段分批送 | 視實作 | 大單進出、降低衝擊成本 | 不適合需要瞬間成交；執行期間暴露意圖，需考慮被跟單/逆向風險 |

### Limit order 的 TIF 比較

> TIF = Time in Force，意思是：這張訂單送出去後，要用什麼「有效期限 / 成交規則」來處理。
> 

| TIF | 全名 | 定義 | 會吃單嗎 | 會掛單嗎 | 適用場景 | 常見坑 |
| --- | --- | --- | --- | --- | --- | --- |
| **Alo** | Add Liquidity Only / Post-only | 只允許加 liquidity；如果會立刻成交，就取消 | 否 | 是，但前提是沒有 cross book | Market making、掛 maker 單、避免 taker fee | 不是保證掛上去；價格太 aggressive 會直接被 cancel |
| **Ioc** | Immediate or Cancel | 立刻成交能成交的部分，剩下取消 | 是 | 否 | 類 market order、平倉、止損、unwind、緊急減倉 | 不保證全成交；limit price 太保守可能成交很少或 0 |
| **Gtc** | Good Til Cancel | 普通限價單；成交不了的部分留在 book 上 | 可能 | 是 | 普通 limit entry、等待價格回落/反彈、非緊急掛單 | 可能先 taker 成交一部分，剩下變 maker；也可能留下 stale order |

### Order flags / Constraints

| 屬性 / Flag | API 欄位 | 適用對象 | 定義 | 常見場景 |
| --- | --- | --- | --- | --- |
| **Post-only** | `t.limit.tif = "Alo"` | Limit order | 只允許掛 maker 單；如果會立刻吃單就取消 | MM quote、maker-only 掛單 |
| **Reduce-only** | `r = true` | 多數 order types | 只能減少既有倉位，不能增加倉位或反向開倉 | 平倉、止損、止盈、unwind |
| **Trigger** | `t.trigger` | Trigger order | 到 trigger price 才啟動訂單 | Stop loss、take profit |
| **Client order id** | `c` / `cloid` | 多數 order types | 自訂訂單 ID，方便追蹤、取消、對帳 | bot order tracking、retry idempotency |
| **Expires after** | `expiresAfter` | action-level | 超過指定時間才到達就拒絕執行 | MM quote、防 stale request |
| **Grouping** | `grouping` | trigger / TP-SL flow | 定義 TP/SL 是否跟 parent order 或 position 綁定 | bracket order、position TP/SL |
| **Vault address** | `vaultAddress` | action-level | 代表 vault / subaccount 下單 | 多帳戶、vault、subaccount trading |

---

## 多單實作比較 — TWAP vs. Scale vs. Batch Limit Orders

| 類型 | 拆單依據 | 子單怎麼產生 | 子單是主動成交還是被動掛單 | 價格怎麼決定 | 適用場景 |
| --- | --- | --- | --- | --- | --- |
| **TWAP** | 時間 | 系統每隔一段時間送子單 | 偏主動成交，market-like | 執行當下市場價格，通常有滑價限制 | 大單慢慢進出、降低瞬間衝擊 |
| **Scale** | 價格區間 | 系統在指定價格範圍內產生多張 limit 單 | 被動掛單 | 你設定 start / end price，系統分布價格 | 分批建倉、分批止盈、網格掛單 |
| **Batch limit orders** | 你自己定義 | 你一次送多張 limit orders | 看每張單的 TIF，可主動也可被動 | 每張單的 price / size / TIF 都由你決定 | bot 自訂多層 quote、批量下單、精細控制 |

### TWAP 屬性

| 參數 | 可不可以調 | 影響 |
| --- | --- | --- |
| **總數量 size** | 可以 | 決定總共要買/賣多少 |
| **duration** | 可以 | 決定 TWAP 跑多久 |
| **reduceOnly** | 可以 | 是否只減倉 |
| **randomize** | 通常可以 | 是否隨機化執行節奏/大小，避免太機械 |
| **送單頻率** | 原生 TWAP 不可直接調 | 固定約每 30 秒一筆 |

> **Phase 3 註記**：live 執行採**自管切片 TWAP**（自送 IOC 限價切片單，帶 0.5%
> 價格保護與 cloid），不用原生 twapOrder——SDK 未支援、API 無價格保護參數
> （子單滑價固定 3%）且無 cloid。詳見 [phase3-spec §9.5](./phase3-spec.md)。

---

## Order 限制

| 概念 | Hyperliquid 規則 | 對 executor 的意思 |
| --- | --- | --- |
| **tick size / price precision** | 價格最多 **5 個 significant figures**，且小數位數不能超過 `MAX_DECIMALS - szDecimals`；perps 的 `MAX_DECIMALS = 6`，spot 的 `MAX_DECIMALS = 8`。整數價格永遠允許。 | 下單前要把 `px` round / truncate 成合法價格。 |
| **lot size / size precision** | 數量 `sz` 最多只能有 `szDecimals` 位小數。 | 下單 size 要依照 asset 的 `szDecimals` 做 floor/truncate。 |
| **szDecimals** | 每個 asset 自己的 size 小數精度。例：`szDecimals = 4` 代表 size 最小 step 是 `0.0001`。 | 不能 hardcode；要從 metadata 查。 |
| **最小下單量** | 主要看 **notional value**，通常 order value 需要至少 **10 USDC**；例外是 reduce-only 精準平倉。 | 下單前檢查 `px * sz >= 10`，除非是 exact close / reduce-only 特例。 |

---

## Stop Loss / Take Profit Order 屬性

在 API 裡，trigger order 大概是這個概念：

```
{
"r":True,# reduceOnly，建議 TP/SL 都開
"t": {
"trigger": {
"isMarket":True,
"triggerPx":"3400",
"tpsl":"sl"
        }
    }
}
```

幾個欄位：

| 欄位 | 意思 |
| --- | --- |
| `triggerPx` | 觸發價格 |
| `tpsl = "sl"` | stop loss |
| `tpsl = "tp"` | take profit |
| `isMarket = true` | 觸發後送 market-like order |
| `isMarket = false` | 觸發後送 limit order |
| `r = true` | reduceOnly，只減倉，不反向開倉 |

---

## Fees

### Base Rate

Taker 0.045% Maker 0.015%

## Funding Rate

| 項目 | Hyperliquid 規則 / 說明 |
| --- | --- |
| **計費頻率** | 每小時結算一次 funding。 |
| **支付方向** | Funding rate > 0 時，long 付 short；Funding rate < 0 時，short 付 long。 |
| **基本計算** | `funding_pnl = -(position_size * mark_price) * funding_rate` |

---

# Part 2 — Decision schema & order flow

## Phase 2 schema — structured target contract

Phase 2 使用 structured target 作為唯一決策來源，不再使用 `Buy` / `Overweight` / `Hold` / `Underweight` / `Sell` rating，也不保留 `raw_rating` fallback。

```json
{
  "decision_mode": "set_target",
  "target_side": "long",
  "requested_target_margin_pct": 35,
  "confidence": 0.78,
  "rationale": "Trend and funding conditions support a long position.",
  "key_risks": ["Funding is rising", "Volatility remains elevated"]
}
```

| 欄位 | 合法值 | 說明 |
| --- | --- | --- |
| `decision_mode` | `set_target` / `maintain_current` | AI 是否建立新 target |
| `target_side` | `long` / `short` / `flat` / `null` | 最終目標方向 |
| `requested_target_margin_pct` | `0–100` / `null` | AI 建議的 account-equity margin allocation |
| `confidence` | `0–1`（`maintain_current` 可為 `null`） | AI 對決策的信心；`set_target` 必填，低於 `min_confidence` 時被風控拒絕成 `maintain_current`（`risk_action = rejected`）；同方向 resize 另須達到更高的 `resize_min_confidence`（`risk_reason = low_confidence_resize`，見 phase2-spec.md 2.4） |
| `rationale` | non-empty string | 決策理由 |
| `key_risks` | 1–3 項 | 主要風險（至少 1 項；空陣列 fail-closed） |

合法組合：

```text
set_target + long  + margin 1–100
set_target + short + margin 1–100
set_target + flat  + margin 0
maintain_current + target_side null + margin null
```

`maintain_current` 表示 AI 不建立新 target，系統維持目前 position quantity；它不是 `flat`，也不是持續 rebalance 到當下 margin percentage。已有 SL / TP 與硬性 RiskGate 仍繼續生效。

`flat` 是 AI 可主動輸出的正式 target：有倉位時使用 reduce-only 平倉，已空倉時為 no-op。

任何不符合 cross-field invariant 的輸出，包含 margin 負數或超過 `100`、`long/short + 0`、`flat + nonzero`、`maintain_current + target`，都視為 invalid decision：

```text
decision_mode = maintain_current
risk_action = invalid_fail_closed
order_created = false
```

不得從舊 rating、自由文字或前一輪 target 推測遺失欄位。舊 Phase 1 JSON audit 保持原樣，但 Phase 2 不讀取舊 rating 作為 execution fallback。

---

> **Phase 1 legacy**：以下 schema、examples 與 order flow 屬 Phase 1 的 rating 映射管線，Phase 2 起由上方 structured target contract 取代，保留供對照。
`PerpTradeDecision` describes **intent and direction** — never a concrete order.

> **Who produces it:** in Direction 2 the LLM agents do *not* emit this schema
> directly. The unmodified TradingAgents engine emits a `PortfolioDecision`
> (a 5-tier rating + thesis); the **decision adapter** in this module maps that —
> together with the live `PerpMarketContext` and `PerpPosition` — into the
> `PerpTradeDecision` below. See [Integration](./INTEGRATION.md) for the
> field-by-field mapping. The schema here is the adapter's **output contract**.

The perp context the engine reasons over is built by `context_builder.py`: every
numeric value is annotated (e.g. funding-rate basis points, z-scores) so the
model reads context rather than re-deriving it.

## Phase 1 schema — `PerpTradeDecision`（superseded）

| Field | Type | Description | Why it exists |
|---|---|---|---|
| `intent` | enum | `hold` / `open_long` / `open_short` / `reduce` / `close` | Core decision, one of five — no fuzzy answers allowed. |
| `confidence` | float 0–1 | Confidence in this decision. | RiskGate can force `confidence < 0.6 → hold`. |
| `target_size_pct` | float 0–100 | Target position as a % of account net value. | Gives direction + ratio; the actual size is computed by RiskGate + OrderPlanner. |
| `entry_zone` | object \| null | Suggested entry range `{low, high}` (decimal strings); `null` means "around market". | Reference for the OrderPlanner's limit price — not a hard command. |
| `invalidation_price` | string \| null | Breaking/crossing this level means the thesis is wrong → exit. Decimal string (exchange-native precision). | The logic behind the stop loss — not the exact SL trigger price. |
| `urgency` | enum | `low` / `medium` / `high` | Lets OrderPlanner choose limit vs market. |
| `rationale` | string | 2–4 sentences explaining the decision. | Required audit-log field for post-mortems. |
| `key_risks` | string[] | 1–3 main risks. | Gives the Risk Manager something concrete to review. |
| `market_regime` | enum | `trending` / `ranging` / `volatile` | Used by the Reflection agent to judge whether the strategy fit the regime. |
| `funding_view` | enum | `favorable` / `neutral` / `headwind` | Makes the funding judgement explicit rather than leaving it to interpret numbers. |

### Not in the decision — decided downstream

| Not a decision output | Decided by |
|---|---|
| Exact order size | RiskGate + OrderPlanner |
| Exact SL / TP price | RiskGate + OrderPlanner |
| Leverage multiplier | RiskGate |
| Final order params | OrderPlanner |

## Examples（Phase 1 legacy）

**Hold** — keep the current position, change nothing:

```json
{
  "intent": "hold",
  "confidence": 0.72,
  "target_size_pct": null,
  "entry_zone": null,
  "invalidation_price": "61800.0",
  "urgency": "low",
  "rationale": "Uptrend structure intact with price above EMA20 and EMA50. RSI at 64 is elevated but not overbought. However, funding z-score at +1.42 indicates moderately crowded long positioning. OI rising with price suggests leveraged buildup. Prefer to hold existing position rather than add exposure at current funding levels.",
  "key_risks": [
    "Funding rate trending higher — carry cost increasing for longs",
    "OI buildup could unwind quickly if price stalls near resistance at 64,500",
    "Daily realized PnL already -120 USDC, approaching loss threshold"
  ],
  "market_regime": "trending",
  "funding_view": "headwind"
}
```

**Open long** — `target_size_pct` is a target % of net value committed as margin; real size comes from RiskGate:

```json
{
  "intent": "open_long",
  "confidence": 0.81,
  "target_size_pct": 15,
  "entry_zone": { "low": "63000.0", "high": "63400.0" },
  "invalidation_price": "61800.0",
  "urgency": "low",
  "rationale": "Price broke above prior resistance at 62,800 with expanding volume. EMA structure bullish, RSI has room to run. Funding at z-score +0.4 is near neutral — no significant carry headwind. OI rising confirms new longs entering rather than short covering.",
  "key_risks": [
    "Resistance at 64,500 may cap near-term upside",
    "Macro risk: FOMC in 2 days could increase volatility"
  ],
  "market_regime": "trending",
  "funding_view": "neutral"
}
```

**Close** — `target_size_pct: 0` means fully flat:

```json
{
  "intent": "close",
  "confidence": 0.91,
  "target_size_pct": 0,
  "entry_zone": null,
  "invalidation_price": null,
  "urgency": "high",
  "rationale": "Price broke below key support at 61,800 — the defined invalidation level for this long position. Structure has shifted bearish with lower low confirmed. Funding turning negative suggests shorts gaining edge. Exit to protect remaining capital.",
  "key_risks": [
    "Slippage risk on close given expanding volatility"
  ],
  "market_regime": "volatile",
  "funding_view": "favorable"
}
```

## Order flow（Phase 1 legacy — Phase 2 執行流程見 phase2-execution.md）

How `RiskGate` and `OrderPlanner` turn each intent into deterministic action
(Phase 2+):

```
PerpTradeDecision (adapter output)
│
├─ intent = "hold"
│   └─ Do nothing. Store in local store, wait for the next round.
│
├─ intent = "open_long" / "open_short"
│   └─ RiskGate.check()
│       ├─ confidence < 0.6                 → reject, force hold
│       ├─ target_size_pct > max_notional   → clamp to limit
│       ├─ daily_loss_remaining <= 0        → reject
│       └─ passed → OrderPlanner
│           ├─ urgency = "low"   → limit order, TIF = Alo
│           ├─ urgency = "high"  → market order
│           └─ entry_zone set    → limit price = midpoint of the zone
│
├─ intent = "reduce"
│   └─ target_size_pct decides how much to cut
│       e.g. currently 15%, target = 8% → sell half
│
└─ intent = "close"
    ├─ urgency = "high" → market close
    └─ urgency = "low"  → limit close, wait for fill
```
