# Trading Agents - Ph 2

# References

## Hyperliquid Trading

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
    

## Hyperliquid Margin / Liquidation

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
    

## Hyperliquid API

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

# Hyperliquid Order Types

## Order types 比較

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

## Limit order 的 TIF 比較

> TIF = Time in Force，意思是：這張訂單送出去後，要用什麼「有效期限 / 成交規則」來處理。
> 

| TIF | 全名 | 定義 | 會吃單嗎 | 會掛單嗎 | 適用場景 | 常見坑 |
| --- | --- | --- | --- | --- | --- | --- |
| **Alo** | Add Liquidity Only / Post-only | 只允許加 liquidity；如果會立刻成交，就取消 | 否 | 是，但前提是沒有 cross book | Market making、掛 maker 單、避免 taker fee | 不是保證掛上去；價格太 aggressive 會直接被 cancel |
| **Ioc** | Immediate or Cancel | 立刻成交能成交的部分，剩下取消 | 是 | 否 | 類 market order、平倉、止損、unwind、緊急減倉 | 不保證全成交；limit price 太保守可能成交很少或 0 |
| **Gtc** | Good Til Cancel | 普通限價單；成交不了的部分留在 book 上 | 可能 | 是 | 普通 limit entry、等待價格回落/反彈、非緊急掛單 | 可能先 taker 成交一部分，剩下變 maker；也可能留下 stale order |

## Order flags / Constraints

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

# 多單實作比較 — TWAP vs. Scale vs. Batch Limit Orders

| 類型 | 拆單依據 | 子單怎麼產生 | 子單是主動成交還是被動掛單 | 價格怎麼決定 | 適用場景 |
| --- | --- | --- | --- | --- | --- |
| **TWAP** | 時間 | 系統每隔一段時間送子單 | 偏主動成交，market-like | 執行當下市場價格，通常有滑價限制 | 大單慢慢進出、降低瞬間衝擊 |
| **Scale** | 價格區間 | 系統在指定價格範圍內產生多張 limit 單 | 被動掛單 | 你設定 start / end price，系統分布價格 | 分批建倉、分批止盈、網格掛單 |
| **Batch limit orders** | 你自己定義 | 你一次送多張 limit orders | 看每張單的 TIF，可主動也可被動 | 每張單的 price / size / TIF 都由你決定 | bot 自訂多層 quote、批量下單、精細控制 |

## TWAP 屬性

| 參數 | 可不可以調 | 影響 |
| --- | --- | --- |
| **總數量 size** | 可以 | 決定總共要買/賣多少 |
| **duration** | 可以 | 決定 TWAP 跑多久 |
| **reduceOnly** | 可以 | 是否只減倉 |
| **randomize** | 通常可以 | 是否隨機化執行節奏/大小，避免太機械 |
| **送單頻率** | 原生 TWAP 不可直接調 | 固定約每 30 秒一筆 |

---

# Order 限制

| 概念 | Hyperliquid 規則 | 對 executor 的意思 |
| --- | --- | --- |
| **tick size / price precision** | 價格最多 **5 個 significant figures**，且小數位數不能超過 `MAX_DECIMALS - szDecimals`；perps 的 `MAX_DECIMALS = 6`，spot 的 `MAX_DECIMALS = 8`。整數價格永遠允許。 | 下單前要把 `px` round / truncate 成合法價格。 |
| **lot size / size precision** | 數量 `sz` 最多只能有 `szDecimals` 位小數。 | 下單 size 要依照 asset 的 `szDecimals` 做 floor/truncate。 |
| **szDecimals** | 每個 asset 自己的 size 小數精度。例：`szDecimals = 4` 代表 size 最小 step 是 `0.0001`。 | 不能 hardcode；要從 metadata 查。 |
| **最小下單量** | 主要看 **notional value**，通常 order value 需要至少 **10 USDC**；例外是 reduce-only 精準平倉。 | 下單前檢查 `px * sz >= 10`，除非是 exact close / reduce-only 特例。 |

---

# Stop Loss / Take Profit Order 屬性

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

# Fees

## Base Rate

Taker 0.045% Maker 0.015%

# Funding Rate

| 項目 | Hyperliquid 規則 / 說明 |
| --- | --- |
| **計費頻率** | 每小時結算一次 funding。 |
| **支付方向** | Funding rate > 0 時，long 付 short；Funding rate < 0 時，short 付 long。 |
| **基本計算** | `funding_pnl = -(position_size * mark_price) * funding_rate` |

---

# Phase 2 設計：Paper Trading、Order、Risk 與 Accounting

## 1. Phase 2 目標

Phase 2 的核心目標是建立一套可以支援 **paper trading**，並且未來可平滑延伸到 **live trading** 的交易執行與記錄架構。

本階段主要包含：

1. 下單邏輯設計
2. Stop Loss / Take Profit 管理
3. Paper trading 成交模擬
4. 帳戶、倉位、PnL 與 margin 模擬
5. Risk / Margin 限制
6. SQLite persistence 與 CSV export 設計

---

## 2. Order：下單邏輯

### 2.1 AI decision contract

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
| `confidence` | `0–1` | AI 對決策的信心 |
| `rationale` | non-empty string | 決策理由 |
| `key_risks` | 最多 3 項 | 主要風險 |

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

### 2.2 目標倉位調整

策略每 4 小時產生一次新預測與目標倉位。

當新目標倉位產生後，系統使用 TWAP 單在預測完成後約 1 小時內，逐步調整至目標倉位。

```
AI output → target position → TWAP rebalance orders → fills → position update
```

#### 2.2.1 Phase 2 TWAP execution model

Phase 2 is **forward paper trading**, not historical backtesting:

- The strategy produces a new decision from closed `4h` candles every four hours.
- A paper TWAP adjusts the local simulated position over approximately one hour.
- The paper TWAP follows Hyperliquid's native cadence and creates one local execution slice every 30 seconds (approximately 120 slices per hour).
- At each slice time, the system reads the current public `mid_price`, applies the configured paper slippage, and records a simulated fill.
- Paper orders and fills exist locally only. The system must not call an exchange order-placement endpoint and does not require a private key.
- Phase 3 live execution submits one native TWAP request; Hyperliquid, rather than this system, schedules the live 30-second suborders.

**Paper market-data freshness and outage handling**

At each scheduled paper TWAP slice, the system must issue a new public market-data request. A response is valid only when the same fresh snapshot provides valid `mid_price` and `mark_price` values within a `5`-second request timeout. An unchanged price is not stale by itself; freshness is determined by obtaining a new successful response, not by observing a price change.

For every request, record `requested_at`, `received_at`, latency, and the result. Cached prices and the previous successful snapshot must never be used to fabricate a fill.

```text
fresh snapshot received within 5 seconds → simulate the scheduled slice
timeout / request error / invalid mid or mark → leave that slice unfilled
3 consecutive failed slices              → plan status = paused_market_data
```

While `paused_market_data`, the system continues probing once every 30 seconds. When a valid snapshot returns, execution may resume for future scheduled slices. Missed slices are not backfilled or executed as a burst at the recovery price. The original plan deadline is not extended; at the one-hour deadline, any unfilled quantity is recorded as `residual_qty` and the plan enters its applicable terminal state.

During a data outage, the paper engine cannot reliably evaluate an SL trigger. On the first valid snapshot after recovery, if `mark_price` has already crossed the active SL trigger, the system immediately simulates the reduce-only stop fill using the current `mid_price` plus the configured adverse slippage. It records `fill_reason = gap_stop_fill`; it must not claim that the position filled at the earlier SL trigger price.

#### 2.2.2 TWAP minimum order size and duration

Use the following definitions when the paper TWAP is created:

- `min_notional` is the configured minimum notional value for one order.
- `mid` is the current public mid price observed when the TWAP plan is created.
- `ceil_to_step` rounds quantity upward to the asset's legal `szDecimals` step.
- `raw_total_qty` is the unrounded quantity required by the target-position adjustment.
- `qty_step = 10 ^ (-szDecimals)` and `total_qty = floor_to_step(raw_total_qty)`.

```text
min_order_qty = ceil_to_step(min_notional / mid)
max_legal_slices = floor(total_qty / min_order_qty)
planned_slices = min(max_legal_slices, 120)

0 slices → reject/residual
1 slice  → paper_market（Phase 3 對應 Market / IOC）
2–120    → TWAP, one slice every 30 seconds

duration = planned_slices × 30 seconds
```

The 120-slice cap limits a paper TWAP to at most one hour. A quantity that cannot form one legal slice must not be rounded up into an oversized position; it remains rejected or residual.

When `planned_slices >= 1`, allocate quantity in integer quantity steps rather than dividing floating-point quantities directly:

```text
total_steps = integer(total_qty / qty_step)
base_steps  = floor(total_steps / planned_slices)
extra_steps = total_steps % planned_slices

slice[0 : extra_steps]              = (base_steps + 1) × qty_step
slice[extra_steps : planned_slices] = base_steps × qty_step
```

This guarantees that the planned slice quantities sum exactly to `total_qty`. For example, `total_qty = 1.03`, `qty_step = 0.01`, and `planned_slices = 4` produces `0.26, 0.26, 0.26, 0.25`.

Any amount removed when converting `raw_total_qty` to `total_qty` is recorded as `rounding_residual_qty = raw_total_qty - total_qty`; it must not be rounded upward because doing so could exceed the approved target.

`min_notional` is evaluated once, using the fresh `mid` captured when the paper plan is created. Later price movement does not repartition or invalidate already planned paper slices. Phase 2 does not submit these slices to the exchange, so it must not reapply exchange minimum-notional validation at every simulated fill. Phase 3 live execution instead follows the exchange's current validation and actual execution results.

Every TWAP / flip plan must enter a terminal state no later than one hour after creation. Under normal operation, the next `4h` AI cycle therefore cannot overlap the previous execution plan.

If process startup finds an unfinished TWAP / flip plan, the system does not resume, catch up, or finish that old target. The restart creates a new decision boundary:

```text
1. stop all remaining slices from the old plan
2. mark the old plan as canceled_restart
3. record every unexecuted quantity as residual_qty
4. rebuild the actual position from committed SQLite fills
5. backfill due funding events and obtain a fresh market snapshot
6. process liquidation / emergency close / gap SL / TP first
7. reconcile position, account, SL / TP and scheduler state
8. immediately start one new AI cycle using the resulting current state
9. set next_decision_at = new decision_at + 4 hours
```

This rule applies whether restart occurs before or after the old plan deadline. Missed slices are never backfilled, future slices from the interrupted plan are never resumed, and the old target is never used to infer the new target. If the new AI / market API attempt fails, the system follows section 8.1.1 and keeps the reconciled current position protected; it must not reactivate the canceled plan.

The system must never execute slices from an old plan concurrently with orders derived from a newer `output_id`.

#### 2.2.3 Opposite-side target and sequential flip

When the approved target side is opposite to the current position, the system creates one flip plan with two sequential legs:

```text
FlipPlan
├─ Close leg: reduce-only, current position -> flat
└─ Open leg:  non-reduce-only, flat -> approved opposite-side target
```

The open leg must not start until the close leg has fully filled and the local position is confirmed flat. Immediately before starting the open leg, the system reruns deterministic RiskGate checks using the latest market and local account state. It does not call the AI again.

Both legs share the same `output_id` and `flip_plan_id`, and their combined execution budget is at most 120 slices / one hour. Slices are allocated between the two legs in proportion to their quantities, subject to legal size-step and minimum-notional rules.

If the close leg does not finish, the plan expires, market data is unavailable, or RiskGate rejects the open leg, the system must not open the opposite position. It records `flip_incomplete` with a reason and leaves the remaining target for the next `4h` decision to reconsider.

---

### 2.3 Stop Loss / Take Profit

系統使用：

- Stop Loss：`SL Market Order reduce-only`
- Take Profit：`TP Market Order reduce-only`

SL / TP 都只用來減倉或平倉，不應增加反向倉位。

Protection orders 必須遵守以下 invariant：

```text
position_size == 0  → no active SL / TP
position_size != 0  → SL quantity covers the full current position
settled position    → TP quantity covers the full current position
```

Phase 2 paper mode 直接更新本地 trigger-order state，並追加 order-version event。Phase 3 live mode 應優先使用 Hyperliquid `modify` / `batchModify` 更新現有 trigger order，不應先 cancel 後重建，以避免產生沒有保護單的空窗。

---

## 3. Stop Loss 設計

### 3.1 資料來源

Stop Loss 計算需要以下資料：

| 資料 | 來源 |
| --- | --- |
| `estimated_liquidation_price` | Paper account ledger + Hyperliquid margin tiers + 官方 liquidation formula |
| `exchange_liquidation_price` | Live `clearinghouseState.liquidationPx` |
| `entry_price` | Paper position ledger；live 來自 `clearinghouseState.entryPx` |

Paper trading 使用 `estimated_liquidation_price`，其目的是提供內部一致、可重算的保守風控參考，不宣稱與交易所所有 rounding / timing edge cases 完全一致。Live trading 一律使用 `exchange_liquidation_price`。

本章後續公式中的 `liquidation_price` 是 mode-aware alias：

```text
paper → estimated_liquidation_price
live  → exchange_liquidation_price
```

---

### 3.2 Stop Loss 下單時機

Stop Loss 在每次 position-changing fill 後都重新計算與 reconciliation：

1. 更新 position size、平均 entry price、margin 與 liquidation price。
2. 重新檢查安全 SL range 與 liquidation buffer。
3. 依本章公式計算最新 SL target price。
4. 若 position 仍存在，modify 現有 SL 的 trigger price 與 protected quantity；若 SL 不存在則建立。
5. 若 position 已為 `0`，取消 SL，且不建立新 SL。

即使 SL 價格變動很小，Phase 2 仍依最新平均 entry 與 liquidation state 計算本地 desired SL。價格與數量必須先套用 tick / lot-size rounding。

若無法在合法 range 內得到能於 liquidation 前觸發的 SL，不得建立或更新無效 SL；RiskGate 直接平倉。

---

### 3.3 Long Stop Loss 檢查條件

若目前是 long position，SL 應滿足：

```
entry * (1 - sl_max_pct) ≤ SL ≤ entry * (1 - sl_min_pct)
```

並且 SL 必須在 liquidation price 前觸發：

```
liquidation_price * (1 + liq_buffer) < SL price
```

此 range 用於驗證最新公式結果是否合法；依 3.2 節，每次 position-changing fill 後仍會重新計算並更新 SL price 與 quantity。

---

### 3.4 Short Stop Loss 檢查條件

若目前是 short position，SL 應滿足：

```
entry * (1 + sl_min_pct) ≤ SL ≤ entry * (1 + sl_max_pct)
```

並且 SL 必須在 liquidation price 前觸發：

```
SL price < liquidation_price * (1 - liq_buffer)
```

此 range 用於驗證最新公式結果是否合法；依 3.2 節，每次 position-changing fill 後仍會重新計算並更新 SL price 與 quantity。

---

### 3.5 Stop Loss 價格限制

Stop Loss 一定要在 liquidation price 之前觸發。

#### Long

```
liquidation_price < liquidation_price * (1 + liq_buffer) < SL price < entry
```

#### Short

```
entry < SL price < liquidation_price * (1 - liq_buffer) < liquidation_price
```

---

### 3.6 Stop Loss 參數

```
sl_min_pct = 5%
sl_max_pct = 10%
sl_target_pct = (sl_min_pct + sl_max_pct) / 2
```

---

### 3.7 Risk Gate

如果 liquidation price 與 entry price 太接近，導致 SL 已經無法安全放在合理區間內，則不再嘗試掛 SL，而是直接 market order 平倉。

#### Long

```
liquidation_price * (1 + liq_buffer) >= entry * (1 - sl_min_pct)
→ 立即 market order 平倉
```

#### Short

```
liquidation_price * (1 - liq_buffer) <= entry * (1 + sl_min_pct)
→ 立即 market order 平倉
```

---

### 3.8 Stop Loss 價格公式

#### Long

```
SL = max(
  entry * (1 - sl_target_pct),
  liquidation_price * (1 + liq_buffer)
)
```

#### Short

```
SL = min(
  entry * (1 + sl_target_pct),
  liquidation_price * (1 - liq_buffer)
)
```

如果 position 為 0，則不下 SL 單。

---

## 4. Take Profit 設計

### 4.1 Take Profit 下單時機

Take Profit 依 TWAP lifecycle 管理：

1. TWAP 啟動前，取消目前 active TP，但保留 SL。
2. TWAP 執行期間不建立 TP；每次 fill 只更新 SL。
3. TWAP 進入 terminal state 後，以最終 position size 與平均 entry price 建立或更新 TP。
4. Terminal state 包含 `completed`、`canceled`、`canceled_restart`、`expired`、`failed` 與 `flip_incomplete`；部分成交後非正常結束也必須 reconciliation。
5. 若最終 position 為 `0`，取消殘留 SL / TP，且不建立新 TP。

在 sequential flip 中，close leg 期間持續更新 SL；平倉完成時取消 SL；open leg 第一筆 fill 後立即建立 SL；整個 flip plan 結束後才建立 TP。

---

### 4.2 Take Profit 價格

```
tp_threshold = 20%
```

#### Long

```
TP = entry * (1 + tp_threshold)
```

#### Short

```
TP = entry * (1 - tp_threshold)
```

如果 position 為 0，則不下 TP 單。

---

## 5. Paper Trading：模擬成交

Paper trading 中，market data 來自外部，但帳戶狀態、倉位、PnL、margin、fills 都由系統自行模擬。

---

### 5.1 避免 Look-ahead Bias

主要的 Phase 2 模式是 forward paper trading。AI 只能使用已封閉的 `4h` K 線；decision 完成後才能建立 paper order，並設定 `active_from`。

```text
已封閉的 4h candle
  → AI decision
  → paper order 建立
  → active_from
  → active_from 之後第一個可用的即時 market snapshot
```

- `paper_market` 可在 `active_from` 後的第一個可用 snapshot 模擬成交。
- TWAP 的第一個 slice 在建立後 30 秒執行，後續每 30 秒執行一個 slice。
- 不得使用 order 建立前不可見的價格、high、low 或 volume 判斷成交。

Phase 2 不包含 historical candle replay 或 backtesting。

---

### 5.2 paper_market 成交模型

Phase 2 不會向交易所送出 Market / IOC。RiskGate、最小數量、可用 margin 與 rounding 都是在建立本地 order 前完成的驗證：未通過時不建立 paper order，而不是建立後再描述成「未成交」。

通過驗證後建立的 `paper_market`，在 `active_from` 後取得第一個有效即時 market snapshot 時，必須以完整 order quantity 產生一筆 simulated fill；Phase 2 不模擬部分成交。Phase 3 live execution 才將此意圖轉換成真實 Market / aggressive IOC，並採用交易所回報的實際成交結果。

預設：

```
slippage_bps = 5
ref_price = mid_price
```

`ref_price` 必須使用執行當時取得的真實 `mid_price`。若暫時無法取得 `mid_price`，order 狀態設為 `pending_market_data`；這不代表交易所拒絕或未成交。系統不得以 `mark_price` 或舊價格偽造 fill，取得有效 snapshot 後才完整模擬成交。

#### Buy

```
fill_price = ref_price * (1 + slippage_bps / 10_000)
```

#### Sell

```
fill_price = ref_price * (1 - slippage_bps / 10_000)
```

SL / TP 的觸發與成交價格使用不同基準：

```text
trigger 判斷     = mark_price
模擬成交參考價 = mid_price
最終模擬成交價 = mid_price ± slippage
```

---

### 5.3 同一 market snapshot 的事件優先順序

同一個 snapshot 可能同時滿足 funding timestamp、SL / TP trigger 與 TWAP slice 的執行時間。為確保 replay 與重啟後得到相同結果，paper engine 必須依下列固定順序處理：

```text
1. 更新本次 snapshot 的 mark_price / mid_price
2. 過帳截至本次 snapshot 已到期且尚未處理的 funding event
3. 檢查 liquidation / emergency-close condition
4. 檢查並執行 active SL
5. 檢查並執行 active TP
6. 若尚可繼續執行，才處理本次 TWAP slice / paper_market
7. 依所有 fills 更新 position、average entry、realized PnL、fees 與 margin
8. 重新計算 SL / TP lifecycle，最後寫入 account snapshot
```

Liquidation、emergency close、SL 或 TP 一旦將 position 平倉，系統必須立即取消同一 target / plan 的剩餘 TWAP slices，將 plan 設為 terminal，並記錄取消原因。同一個 snapshot 不得再執行會重新建立該 position 的 slice。下一次開倉只能來自後續新的 AI decision 與 `output_id`。

若風險出場只減少部分 position 而沒有歸零，保守起見仍終止原 TWAP plan；不得讓舊 target 在風險事件後繼續把部位加回。完成 position、SL / TP 與 account reconciliation 後，剩餘 target 留待下一個 `4h` decision 重新評估。

---

### 5.4 Paper Trading 預設設定

```
paper_trading:
  account:
    initial_balance_usdc: 1000
    initial_positions: []

  execution:
    taker_fee_rate: 0.00045

    market_monitor:
      interval_seconds: 30
      request_timeout_seconds: 5

    market:
      ref_price: mid_price
      slippage_bps: 5

    paper_market:
      ref_price: mid_price
      slippage_bps: 5

```

`initial_balance_usdc` 與 `initial_positions` 只在建立新的 paper `run_id` 時套用。一般程式重啟必須從已記錄的 accounting events / snapshots 恢復上次的本地 account 與 position state，不得重設為 1,000 USDC。

Phase 2 的 paper account 不讀取真實 Hyperliquid wallet balance 或 position，也不需要 private key。

Phase 2 不使用 candle high / low / volume 模擬 limit fill。Limit-order partial-fill 模型與 historical backtesting 延後至後續階段。

### 5.5 Phase 2 position-aware market monitor

Phase 2 的 SL / TP、liquidation、unrealized PnL 與 TWAP fills 都是本地模擬，因此只要存在任何 active position、active TWAP / flip plan、`paper_market`、SL 或 TP，paper engine 就必須啟動 market-monitor loop，每 `30` 秒取得一次新的 `mid_price` / `mark_price` snapshot，並依 5.3 節的固定事件順序處理。

```text
position != 0 or active plan/order exists → poll every 30 seconds
position == 0 and no active plan/order    → stop 30-second polling
```

若同一時間點也是 TWAP slice 的 scheduled time，monitor 與 slice 必須共用同一份有效 snapshot，不得為同一 logical tick 重複呼叫 API 或使用兩組不同價格。Request timeout、stale-data pause、恢復後的 `gap_stop_fill` 均沿用 2.2.1 節規則。

這個本地 trigger-monitoring loop 是 Phase 2 paper behavior。Phase 3 的 SL / TP 必須實際掛在 Hyperliquid，由交易所負責觸發；Phase 3 本地程式仍監控並 reconciliation exchange order / fill / position state，但不以本地 30 秒輪詢作為 live stop 能否執行的必要條件。

---

## 6. Paper Trading 模擬數值

### 6.1 帳戶與曝險

| 數值 | 公式 | 說明 |
| --- | --- | --- |
| `account_equity` | `wallet_balance + total_unrealized_pnl` | 帳戶當前淨值，用於計算目標倉位與風控限制 |
| `available_balance` | `account_equity - used_initial_margin` | 可用於開新倉的餘額 |
| `position_notional` | `abs(position_size * mark_price)` | 單一倉位的名目價值 |
| `total_position_notional` | `sum(position_notional_i)` | 所有倉位的總名目價值 |
| `current_exposure_pct` | `position_notional / account_equity * 100` | 單一倉位佔帳戶淨值的曝險比例 |
| `effective_leverage` | `total_position_notional / account_equity` | 帳戶實際槓桿 |

---

### 6.2 目標倉位與下單數量

| 數值 | 公式 | 說明 |
| --- | --- | --- |
| `requested_target_margin_pct` | AI decision 輸出，合法範圍 `0–100` | AI 要求的 unsigned margin allocation 比例 |
| `approved_target_margin_pct` | `min(requested_target_margin_pct, max_target_margin_pct)` | RiskGate 實際核准的 margin allocation 比例 |
| `target_margin` | `account_equity * approved_target_margin_pct / 100` | RiskGate 核准用於目標倉位的 margin |
| `target_notional` | `target_margin * configured_leverage` | 套用槓桿後的目標名目倉位 magnitude |
| `target_signed_notional` | `direction * target_notional` | long 為正、short 為負的目標名目倉位 |
| `current_signed_notional` | `position_size * mark_price` | 目前方向性倉位；long 為正，short 為負 |
| `delta_notional` | `target_signed_notional - current_signed_notional` | 目標倉位與目前倉位的差額 |
| `order_qty` | `abs(delta_notional) / ref_price` | 為達到目標倉位需要下單的數量 |

`requested_target_margin_pct` 是帳戶淨值中 AI 建議分配為目標倉位 margin 的比例，而不是名目曝險比例。AI 可輸出 `0–100%`，但真正用於下單的數值必須是 RiskGate 產生的 `approved_target_margin_pct`。方向不寫在百分比的正負號中，由 `target_side` 提供。

```text
account_equity = 1,000 USDC
requested_target_margin_pct = 20
approved_target_margin_pct = 20
configured_leverage = 5

target_margin = 1,000 * 20 / 100 = 200 USDC
target_notional = 200 * 5 = 1,000 USDC
```

> Compatibility note: 現行 Phase 1 adapter 已使用 `position.margin_used / account_value` 計算目前 signed margin allocation，與本節的 margin-based 定義一致。Phase 2 實作時仍需將舊欄位 `target_size_pct` 拆分為 `requested_target_margin_pct` 與 `approved_target_margin_pct`。

---

### 6.3 PnL 與平均成本

| 數值 | 公式 | 說明 |
| --- | --- | --- |
| `unrealized_pnl` | `position_size * (mark_price - entry_price)` | 未實現損益，`position_size` 正數代表 long，負數代表 short |
| `realized_pnl_delta` | Long: `(exit_price - entry_price) * closed_qty`Short: `(entry_price - exit_price) * closed_qty` | 減倉或平倉時產生的已實現損益 |
| `total_pnl` | `realized_pnl + unrealized_pnl - total_fees + net_funding_pnl` | 扣除手續費並納入 signed funding PnL 後的總損益 |
| `pnl_pct` | `total_pnl / initial_balance * 100` | 總損益率 |
| `new_entry_price` | `(old_abs_size * old_entry_price + add_qty * fill_price) / new_abs_size` | 同方向加倉後的新平均進場價；減倉時 `entry_price` 不變 |

---

### 6.4 成交模擬

| 數值 | 公式 | 說明 |
| --- | --- | --- |
| `market_buy_fill_price` | `ref_price * (1 + slippage_bps / 10_000)` | `paper_market` buy 的模擬成交價 |
| `market_sell_fill_price` | `ref_price * (1 - slippage_bps / 10_000)` | `paper_market` sell 的模擬成交價 |
| `fill_notional` | `fill_qty * fill_price` | 單次成交的名目價值 |

---

### 6.5 手續費與 Funding

| 數值 | 公式 | 說明 |
| --- | --- | --- |
| `fill_notional` | `abs(fill_qty * fill_price)` | 單次成交的絕對名目價值 |
| `fee` | `fill_notional * taker_fee_rate` | 單次成交手續費，永遠為非負成本 |
| `signed_position_notional` | `position_size * mark_price` | Long 為正，short 為負 |
| `funding_pnl` | `-signed_position_notional * funding_rate` | Signed 單期 funding PnL；收入為正，成本為負 |
| `net_funding_pnl` | `sum(funding_pnl)` | 所有已過帳 funding events 的 signed 累積值 |

Phase 2 的 TWAP slices、`paper_market`、SL、TP 與 emergency / gap-stop fills 都是主動成交模型，一律按 taker fill 計費。每筆 fill 過帳時同時執行：

```text
fill_notional = abs(fill_qty * fill_price)
fee = fill_notional * taker_fee_rate
wallet_balance = wallet_balance - fee
total_fees = total_fees + fee
```

Phase 2 使用設定檔中的固定費率，預設 `taker_fee_rate = 0.00045`（`0.045%`），以確保相同輸入可以重現相同結果；這是 paper simulation 參數，不宣稱等於任一帳戶當下的實際費率。每筆 fill 必須保存實際使用的 `fee_rate`，後續調整設定不得改寫既有 fills。

Phase 3 live trading 可由 Hyperliquid 帳戶 API 取得實際帳戶費率並覆寫設定值；若無法取得，不得把已成交交易記為零費用，應將 fee reconciliation 標記為 pending，待真實 fill / fee 資料回補。

Funding 每小時依 Hyperliquid 實際結算 rate 過帳一次。計算使用 funding timestamp 之前最後確認的 paper position 與當時 mark price：

```text
event_time < funding_timestamp  → 納入本期 position
event_time >= funding_timestamp → 不納入本期

wallet_balance = wallet_balance + funding_pnl
```

每筆 funding event 使用 `(run_id, symbol, funding_timestamp)` 作為唯一鍵，保證 retry / restart 時 exactly once。若結算 rate 暫時取得失敗，記錄 `funding_pending`，稍後由 funding history 補帳；不得使用 `0` 或舊 rate 伪造記錄。

---

### 6.6 Margin 與清算風險

| 數值 | 公式 | 說明 |
| --- | --- | --- |
| `initial_margin` | `position_notional / configured_leverage` | 開倉所需 initial margin |
| `used_initial_margin` | `sum(initial_margin_i)` | 所有倉位已使用的 initial margin |
| `maintenance_margin` | `position_notional * maintenance_margin_rate - maintenance_deduction` | 維持倉位所需的最低保證金 |
| `total_maintenance_margin` | `sum(maintenance_margin_i)` | 所有倉位的總 maintenance margin |
| `margin_ratio` | `account_equity / total_maintenance_margin` | 清算風險指標，越接近 `1` 越危險 |
| `cross_liquidation_condition` | `account_equity <= total_maintenance_margin` | Cross margin 下的清算條件 |
| `isolated_equity` | `isolated_margin + unrealized_pnl - accrued_fees + net_funding_pnl` | Isolated margin 下單一倉位的 equity |
| `isolated_liquidation_condition` | `isolated_equity <= maintenance_margin` | Isolated margin 下的清算條件 |

#### 6.6.1 Paper estimated liquidation price

Margin tier 必須從 Hyperliquid `meta` 回應的 margin table 取得，不得 hardcode BTC 或其他 asset 的 max leverage。

```text
maintenance_margin_rate = 1 / (2 * tier_max_leverage)

maintenance_margin(p) =
    position_notional(p) * maintenance_margin_rate(p)
    - maintenance_deduction(p)
```

`maintenance_deduction` 使用 Hyperliquid margin-table tier 的連續計算規則。Tier 必須依 candidate liquidation price `p` 下的 notional 選擇，因為價格變動可能跨越 tier boundary。

對要估算 liquidation price 的 symbol，建立 candidate-price 函數：

```text
position_notional(p) = abs(position_size) * p
unrealized_pnl(p) = position_size * (p - entry_price)

account_equity(p) =
    wallet_balance
    + candidate_position_unrealized_pnl(p)
    + other_positions_unrealized_pnl

total_maintenance_margin(p) =
    candidate_position_maintenance_margin(p)
    + other_positions_maintenance_margin

f(p) = account_equity(p) - total_maintenance_margin(p)
```

Fees、funding 與 realized PnL 若已過帳到 `wallet_balance`，不得在 `account_equity(p)` 中重複扣除或加回。

`estimated_liquidation_price` 是 adverse direction 上 `f(p) = 0` 的解：

- Long：由當前 mark price 向 `0` 搜尋。若所有正價格上皆 `f(p) > 0`，則沒有正數 liquidation price，記為 `null`。
- Short：由當前 mark price 向上擴張 bracket，直到 `f(p) <= 0`。
- 找到 bracket 後使用 deterministic bisection 求根，並在每個 candidate price 重新選擇 margin tier。
- 若當前 mark price 已滿足 `f(mark) <= 0`，視為已進入 liquidatable state，不再建立一般 SL，直接進入 liquidation / emergency-close flow。

為保守反映風險，結果套用 asset tick size 時：

```text
Long estimated liquidation price  → round up
Short estimated liquidation price → round down
```

若 `estimated_liquidation_price = null`，SL 仍使用 entry-based `sl_min_pct` / `sl_max_pct` range，但不套用 liquidation buffer，也不因為沒有正數 liquidation price 而拒絕 SL。

以下事件後必須重新計算：

- position-changing fill
- fee / funding posting
- mark-price update
- deposit / withdrawal（未來）
- margin-tier metadata / model-version change

Paper snapshot 必須同時記錄 `margin_tier_id`、`maintenance_margin_rate`、`maintenance_deduction` 與 `liquidation_model_version`，使計算可重現。

#### 6.6.2 Model validation

Paper model 至少必須測試 Long、Short、無正數 liquidation price、當前已可清算、跨 margin tier、fee / funding 變動、多倉位 cross margin 與 tick rounding。另需使用 recorded `clearinghouseState` fixtures 將估算值與 Hyperliquid `liquidationPx` 比對，並記錄容許誤差；未完成實際比對前，不得將 paper estimate 標示為 exchange-exact value。

---

## 7. Risk & Margin：風控與保證金

第一版採用保守設定：

```
risk:
  leverage: 1
  margin_mode: cross
  max_target_margin_pct: 60

decision:
  ai_target_margin_min_pct: 0
  ai_target_margin_max_pct: 100
  target_margin_step_pct: 1
```

### 7.1 Leverage

第一版使用：

```
leverage = 1
```

---

### 7.2 Margin Mode

第一版使用：

```
margin_mode = cross
```

---

### 7.3 最大目標 Margin Allocation

```
max_target_margin_pct = 60
```

RiskGate 需保證：

```
0 <= requested_target_margin_pct <= 100
approved_target_margin_pct = min(requested_target_margin_pct, max_target_margin_pct)
```

AI 可在 `0–100%` 內提出 requested target。若 requested target 介於 `61–100%`，RiskGate 必須 clamp 為 `60%`，並同時保留 requested / approved 數值與 `risk_action = clamped`。負數、超過 `100%`、非數字或與 `target_side` 不一致的輸出視為 invalid decision，fail-closed 成 `maintain_current`。

RiskGate 仍必須獨立檢查 `effective_leverage` 與 available margin，不能只依賴 margin allocation 上限。在 Phase 2 預設 `account_equity = 1,000 USDC` 與 `leverage = 1` 下，`60%` 上限對應 `600 USDC` target margin 與 `600 USDC` target notional。

---

## 8. Accounting：記帳設計

### 8.1 Cycle 時間

策略使用 rolling `4h` interval，不綁定固定 UTC candle boundary：

```text
new run, no previous decision → start immediately
next_decision_at             = last_decision_at + 4 hours
```

例如首次於 `10:15` 成功完成 decision，後續預定時間為 `14:15`、`18:15`、`22:15`。每次 decision 仍只能使用執行當下已封閉的 candles，不得讀取尚未封閉的 candle。

SQLite 必須保存 scheduler state，至少包含 `last_decision_at`、`next_decision_at`、`last_input_id` 與 `last_output_id`。程式重啟時：

```text
unfinished TWAP / flip plan exists → 取消舊 plan、reconcile，立即開始新 cycle
no unfinished plan and now < next_decision_at  → 等待原 next_decision_at
no unfinished plan and now >= next_decision_at → 立即執行一次，不補跑錯過的 intervals
```

延遲執行完成後，以實際 `decision_at + 4 hours` 建立新的 `next_decision_at`。例如原本應於 `14:15` 執行，但程式到 `16:00` 才恢復，則於 `16:00` 執行一次，下一次為 `20:00`；不補做 `14:15` 的歷史 decision。

新 `run_id` 視為沒有 previous decision 並立即執行。相同 `run_id` 的一般 restart 必須延續 SQLite scheduler state；唯一會提前重設四小時計時的情況，是依 2.2.2 節取消重啟時發現的 unfinished TWAP / flip plan，並成功完成新的 AI decision。

#### 8.1.1 Decision API failure and retry

每個 scheduled cycle 建立一個 deterministic `decision_attempt_id`（由 `run_id + scheduled_at` 產生）。市場資料 API 或 AI API 發生 timeout、rate limit、connection error 或 retryable server error 時，最多執行三次同一 logical attempt：

```text
attempt 1 failed → wait 10 seconds
attempt 2 failed → wait 30 seconds
attempt 3 failed → decision_status = api_failed
```

三次皆失敗時，不建立新 target 或 order，不得沿用上一次 AI output。系統維持目前 position，既有 SL / TP、funding 與 market-monitor loop 繼續運作，並保存 error type、message、attempt count 與 timestamps。下一個 scheduled cycle 設為本次 `scheduled_at + 4 hours`。

若 AI API 成功回應，但 schema、型別或 cross-field validation 無效，則不再次呼叫 AI；依 fail-closed 規則記為 `decision_status = invalid_output`、套用 `maintain_current`、不建立 order，並保存原始 response。此 cycle 視為已完成，下一次以實際 `decision_at + 4 hours` 排程。

所有 retry state 必須先寫入 SQLite。Process restart 後只能繼續尚未超過三次的同一 `decision_attempt_id`，不得因重啟把 attempt counter 歸零或產生另一個重複 AI decision。

---

### 8.2 Persistence：SQLite 為唯一 source of truth

Phase 2 將所有正式運行資料寫入本地 SQLite database（例如 `paper_trading.db`）。SQLite 是重啟恢復、去重、accounting replay 與狀態查詢的唯一 source of truth；CSV 不參與交易邏輯，也不得用來恢復 position 或 plan state。

同一個模擬成交所造成的變更必須在一個 SQLite transaction 內完成：

```text
BEGIN
  insert fill
  post fee / realized PnL
  update position and account state
  mark slice completed
  append related order / protection events
COMMIT
```

若在 `COMMIT` 前發生 crash，整筆 transaction 回滾；若已 `COMMIT`，重啟後不得再次套用。每個 TWAP slice 使用 deterministic unique key：

```text
slice_id = run_id + plan_id + flip_leg + slice_index
```

SQLite 必須對 `slice_id` 建立 unique constraint，使一個 slice 最多只能產生一筆有效 fill。Funding event 仍以 `(run_id, symbol, funding_timestamp)` 去重。啟動時從 SQLite 的 committed fills、fees、funding 與 plan events 重建並核對 position / account state，不相信前一次 process memory。

先前定義的所有 CSV 都保留，但定位為可由 SQLite 查詢結果重新產生的 export schema。刪除或人工修改 CSV 不得影響正式狀態。

完整 AI prompt、原始 API response 等大型內容可另外存為 JSON；SQLite 必須保存其路徑、content hash 與 timestamp，以維持可追溯性。

#### 8.2.1 CSV export timing and atomicity

CSV 不在每個 TWAP slice / fill 發生時直接寫入。系統在下列時機從 SQLite 匯出目前 `run_id` 的完整資料集：

1. 每個 AI cycle 完成並完成 accounting reconciliation 後自動匯出。
2. Process 正常 shutdown、最後一個 SQLite transaction 完成後自動匯出。
3. 使用者執行手動 export command 時。

建議的 CLI contract：

```text
python -m contrib.hyperliquid_perp export \
  --run-id <run_id> \
  --output-dir <directory>
```

每個 CSV 必須以 atomic replacement 產生：先在相同 output directory 寫入 `<name>.csv.tmp`，flush 並成功關閉後，再以 atomic replace 將它替換為 `<name>.csv`。不得讓讀取者看到只寫入一部分的正式 CSV。

異常 crash 時不要求匯出 CSV；重啟後仍以 SQLite 恢復，並可再次完整匯出。CSV export 失敗只記錄 `export_failed` 與錯誤，不得回滾已 committed 的交易/accounting state，也不得停止 market monitor 或 SL / TP protection。

Phase 2 預設每次輸出該 `run_id` 的全部 records，而非只輸出最近四小時。若資料量未來明顯增加，再額外提供 `from` / `to` 範圍匯出，但不得改變 SQLite source-of-truth 規則。

#### 8.2.2 SQLite tables and existing CSV schemas

原先定義的 CSV 欄位仍是正式 export contract，不因改用 SQLite 而刪除或更名。下列 SQLite logical tables 與 CSV exports 一對一對應：

| SQLite logical table | CSV export |
| --- | --- |
| `ai_inputs` | `ai_inputs.csv` |
| `decision_attempts` | `decision_attempts.csv` |
| `ai_outputs` | `ai_outputs.csv` |
| `orders` | `orders.csv` |
| `fills` | `fills.csv` |
| `funding_events` | `funding_events.csv` |
| `account_snapshots` | `account_snapshots.csv` |
| `position_snapshots` | `position_snapshots.csv` |

`decision_attempts.csv` 是因本階段新增 API retry / terminal-attempt tracking 而增加的唯一新 export dataset。`slice_id`、`plan_id`、`residual_qty`、`decision_attempt_id` 與 `canceled_restart` 等欄位或狀態，則是本文件後續決策對既有 schemas 的增補；不得移除其他原有欄位。

SQLite 另有只供 runtime 使用、預設不匯出 CSV 的 internal tables：

| Internal table | 用途 |
| --- | --- |
| `runs` | `run_id`、mode、初始資金、設定與 schema version |
| `scheduler_state` | `last_decision_at`、`next_decision_at` 與目前 attempt reference |
| `execution_plans` | TWAP / flip plan、deadline、slice allocation、remaining / residual quantity 與 terminal state |
| `current_positions` | 每個 symbol 的最新 position、average entry、margin 與 active protection references |
| `current_account_state` | 最新 wallet balance、equity、margin、fees、funding 與 PnL |
| `schema_migrations` | Database schema version 與 migration history |

`current_positions` 與 `current_account_state` 是交易 loop 的 materialized current state，必須和造成變化的 fill / fee / funding event 在同一個 SQLite transaction 更新。啟動時仍需以 committed events 與 snapshots 驗證或重建它們；`fills`、funding 與 order / plan events 是 accounting replay 的依據，CSV 不得成為另一份可寫入的 source of truth。

### 8.3 Paper / Live 共用 Schema

本系統使用同一套 SQLite logical schema 與 CSV export schema 支援 paper trading 與 live trading。

#### Paper Trading

- market data 來自外部
- fills 由系統模擬
- positions 由系統模擬
- PnL 由系統模擬
- margin 由系統模擬
- account state 由系統模擬

#### Live Trading

- orders 需與交易所 API / websocket 對帳
- fills 需以交易所成交回報為準
- positions 需以交易所倉位資料對帳
- account state 需以交易所帳戶資料對帳

所有正式 records 與 CSV exports 都保留 `mode` 欄位：

```
paper
live
```

---

## 9. 資料關聯

### 9.1 Logical tables 與 CSV export 關聯

```
ai_inputs.csv
    input_id

decision_attempts.csv
    decision_attempt_id
    input_id
    scheduled_at

ai_outputs.csv
    output_id
    input_id
    decision_attempt_id

orders.csv
    order_id
    output_id

fills.csv
    fill_id
    order_id

funding_events.csv
    funding_event_id
    run_id
    symbol
    funding_timestamp

account_snapshots.csv
    timestamp

position_snapshots.csv
    timestamp
    symbol
```

---

### 9.2 主要追蹤鏈路

```
AI input
  → AI output
  → Order
  → Fill
  → Funding event
  → Account / Position snapshot
```

---

## 10. CSV export 總覽

| CSV export | 用途 | SQLite record 產生時機 |
| --- | --- | --- |
| `ai_inputs.csv` | 紀錄每次送進 AI / strategy 的輸入摘要 | 每次呼叫 AI / strategy 前 |
| `decision_attempts.csv` | 紀錄 scheduled cycle、API retries、terminal status 與錯誤 | 每次 attempt 建立、重試或狀態變化時 |
| `ai_outputs.csv` | 紀錄 AI / strategy 輸出的交易意圖與目標倉位 | 每次產生 AI output 時 |
| `orders.csv` | 紀錄系統產生的 orders | order 建立或狀態變化時 |
| `fills.csv` | 紀錄每一筆成交 | 每次成交發生時 |
| `funding_events.csv` | 紀錄每小時 signed funding PnL 與 exactly-once 過帳狀態 | 每個 funding timestamp |
| `account_snapshots.csv` | 紀錄帳戶整體狀態 | 每個 cycle 結束時 |
| `position_snapshots.csv` | 紀錄每個 symbol 的倉位狀態 | 每個 cycle 結束時 |

---

## 11. `ai_inputs.csv`

紀錄每次送進 AI / strategy 的輸入摘要，用來追蹤 AI 當時看到了什麼資料。

不建議把完整 prompt 或完整 market data 全部塞進 SQLite columns 或 CSV。完整內容可另外存成 JSON 檔，SQLite 保存摘要、路徑、hash 與 timestamp，CSV 匯出這些追蹤欄位。

### 11.1 紀錄時機

- 每次呼叫 AI / strategy 前記錄一次
- 若策略每 4H 跑一次，則每 4H 記錄一筆
- 此紀錄代表「AI 當時看到了什麼資料」

### 11.2 欄位

| 欄位 | 說明 |
| --- | --- |
| `timestamp` | AI input 建立時間 |
| `mode` | `paper` / `live` |
| `input_id` | 本次 AI input id |
| `symbol` | 交易標的 |
| `candle_start` | 使用的最新 K 線開始時間 |
| `candle_end` | 使用的最新 K 線結束時間 |
| `mark_price` | 當時 mark price |
| `mid_price` | 當時 mid price，可無則留空 |
| `funding_rate` | 當時 funding rate |
| `wallet_balance` | Paper wallet balance |
| `account_equity` | 當時帳戶淨值 |
| `available_balance` | 當時可用餘額 |
| `realized_pnl` | 累積已實現損益 |
| `unrealized_pnl` | 當時未實現損益 |
| `total_fees` | 累積手續費 |
| `net_funding_pnl` | 累積 signed funding PnL |
| `effective_leverage` | 當時帳戶實際槓桿 |
| `margin_ratio` | 當時 margin ratio |
| `current_position_side` | `long` / `short` / `flat` |
| `current_position_size` | 當時 signed 倉位數量 |
| `entry_price` | 目前平均進場價；空倉為空 |
| `position_notional` | 當時倉位名目價值 |
| `current_margin_pct` | 目前倉位使用的 account-equity margin allocation |
| `configured_leverage` | 本 symbol 設定槓桿 |
| `estimated_liquidation_price` | Paper 估算清算價；可為空 |
| `stop_loss_price` | 目前 active SL；無則留空 |
| `take_profit_price` | 目前 active TP；無則留空 |
| `active_twap` | 是否有 active TWAP / flip plan |
| `remaining_twap_qty` | Active plan 剩餘數量；無 active plan 為空 |
| `last_fill_time` | 最後一筆 paper fill 時間；無 fill 為空 |
| `max_target_margin_pct` | 當時 RiskGate 上限；預設 `60` |
| `input_payload_path` | 完整 AI input JSON 檔路徑 |
| `prompt_version` | 使用的 prompt / strategy 版本 |
| `model` | 使用的 LLM model |

---

## 12. `ai_outputs.csv`

紀錄 AI / strategy 每次輸出的交易意圖與目標倉位。

### 12.1 紀錄時機

- 每次 AI / strategy 產生新 output 時記錄
- 若策略每 4H 跑一次，則每 4H 記錄一筆
- 此紀錄代表「AI 想要的目標倉位」，不代表已下單或已成交

### 12.2 欄位

| 欄位 | 說明 |
| --- | --- |
| `timestamp` | AI output 時間 |
| `mode` | `paper` / `live` |
| `input_id` | 對應 `ai_inputs.csv` 的 input id |
| `decision_attempt_id` | 對應同一 scheduled cycle 與 retry state |
| `output_id` | 本次 AI output id |
| `symbol` | 交易標的 |
| `decision_mode` | `set_target` / `maintain_current` |
| `target_side` | `long` / `short` / `flat` / 空 |
| `requested_target_margin_pct` | AI 要求的 account equity margin allocation 比例；合法範圍 `0–100` |
| `approved_target_margin_pct` | RiskGate 核准後用於下單的比例；目前上限 `60` |
| `risk_action` | `approved` / `clamped` / `invalid_fail_closed` |
| `risk_reason` | RiskGate 調整或拒絕的原因；未調整可留空 |
| `target_margin` | `account_equity * approved_target_margin_pct / 100` |
| `configured_leverage` | 本次目標倉位使用的槓桿 |
| `target_notional` | `target_margin * configured_leverage` |
| `target_signed_notional` | 套用 `target_side` 後的目標名目倉位；long 為正、short 為負、flat 為 `0` |
| `current_signed_notional` | 決策當下目前方向性倉位 |
| `delta_notional` | 目標與目前倉位差額 |
| `confidence` | AI 信心，範圍 `0–1` |
| `decision_reason` | AI 決策摘要，不得為空 |
| `key_risks` | 最多 3 項主要風險；CSV 可存 JSON array string |
| `order_created` | 本 output 是否產生 rebalance / flip order |
| `no_order_reason` | `maintain_current` / `within_deadband` / `invalid_fail_closed` / 其他；有 order 時留空 |

`maintain_current` 的 requested / approved target、target margin / notional 與 target side 皆為空，`delta_notional = 0`，並記錄 `order_created = false` 與 `no_order_reason = maintain_current`。`flat` 則有明確 target：`target_side = flat`、requested / approved margin = `0`、`target_signed_notional = 0`。

---

## 13. `orders.csv`

紀錄系統產生的 order，包括 rebalance、TWAP 子單、SL / TP 單。

### 13.1 紀錄時機

- order 建立時記錄
- order 狀態變化時記錄，例如部分成交、完全成交、取消、拒絕
- 每次狀態變化先寫入 SQLite order event；CSV 匯出時依既定 schema 呈現，不直接 append CSV 作為正式記錄

Paper trading 中，`active_from` 是 order 可以開始使用即時 market snapshot 模擬成交的最早時間。

### 13.2 欄位

| 欄位 | 說明 |
| --- | --- |
| `timestamp` | order 紀錄時間 |
| `mode` | `paper` / `live` |
| `order_id` | 系統內部 order id |
| `output_id` | 對應 `ai_outputs.csv` 的 output id；若非 AI 直接造成，可留空或填來源 output |
| `exchange_order_id` | 實盤交易所 order id；paper 可留空 |
| `client_order_id` | client order id / cloid |
| `parent_order_id` | TWAP 子單對應母單；沒有則留空 |
| `flip_plan_id` | 相反方向 target 的 sequential flip plan id；非翻倉可留空 |
| `flip_leg` | `close` / `open`；非翻倉可留空 |
| `symbol` | 交易標的 |
| `order_role` | `entry` / `rebalance` / `stop_loss` / `take_profit` |
| `side` | `buy` / `sell` |
| `type` | Phase 2 使用 `paper_market` / `paper_twap_slice` / `stop_market` / `take_market`；Phase 3 才使用 live `market` / `ioc` 等型別 |
| `price` | limit price；market 類型可留空 |
| `trigger_price` | SL / TP 觸發價；非 trigger order 可留空 |
| `qty` | 原始下單數量 |
| `filled_qty` | 已成交數量 |
| `remaining_qty` | 未成交數量 |
| `status` | `open` / `partially_filled` / `filled` / `canceled` / `rejected` |
| `reduce_only` | 是否為 reduce-only |
| `active_from` | paper trading 中 order 最早可成交時間 |

---

## 14. `fills.csv`

紀錄每一筆成交，是更新 position、fee、realized PnL 的主要來源。

### 14.1 紀錄時機

- 每次成交發生時記錄一筆

Paper trading：

- `paper_market` 在 order 生效且取得第一個有效 snapshot 後完整模擬成交
- TWAP 從 `active_from` 後依 30 秒 cadence 為每個已成交 slice 記錄一筆 fill

Live trading：

- 以交易所成交回報 / user fills 為準

### 14.2 欄位

| 欄位 | 說明 |
| --- | --- |
| `timestamp` | 成交時間 |
| `mode` | `paper` / `live` |
| `fill_id` | 系統內部 fill id |
| `order_id` | 對應系統內部 order id |
| `exchange_fill_id` | 實盤交易所 fill id；paper 可留空 |
| `exchange_order_id` | 對應交易所 order id；paper 可留空 |
| `symbol` | 交易標的 |
| `side` | `buy` / `sell` |
| `fill_qty` | 成交數量 |
| `fill_price` | 成交價格 |
| `fill_notional` | `fill_qty * fill_price` |
| `fee` | 本次成交手續費 |
| `fee_rate` | 使用的 fee rate |
| `realized_pnl_delta` | 本次成交造成的已實現損益變化 |
| `liquidity_type` | `maker` / `taker` / `simulated` |

---

## 14A. `funding_events.csv`

紀錄 paper / live funding 事件，是重建 wallet balance 與 `net_funding_pnl` 的主要來源。

| 欄位 | 說明 |
| --- | --- |
| `recorded_at` | 本地寫入時間 |
| `funding_timestamp` | Hyperliquid 本期 funding 結算時間 |
| `mode` | `paper` / `live` |
| `run_id` | Paper / live run id |
| `funding_event_id` | Deterministic event id，由 `run_id + symbol + funding_timestamp` 產生 |
| `symbol` | 交易標的 |
| `position_size` | 結算 timestamp 前最後確認的 signed position size |
| `mark_price` | 本期計算使用的 mark price |
| `signed_position_notional` | `position_size * mark_price` |
| `funding_rate` | Hyperliquid 本期實際 funding rate |
| `funding_pnl` | `-signed_position_notional * funding_rate` |
| `status` | `pending` / `posted` / `failed` |
| `source` | `live_public_data` / `funding_history_backfill` / `exchange_user_funding` |

`(run_id, symbol, funding_timestamp)` 必須唯一。`pending` 補帳為 `posted` 時更新同一個 logical event，並在該狀態轉換時將 `funding_pnl` 套用到 wallet balance 一次；後續 retry 不得重複過帳。

---

## 15. `account_snapshots.csv`

紀錄每個週期結束後的帳戶狀態，用來追蹤整體績效與風險。

### 15.1 紀錄時機

- 每個 paper trading / live trading cycle 結束時記錄一次
- 若策略每 4H 跑一次，則每 4H 記錄一次

建議在以下狀態都更新完成後記錄：

1. mark price
2. unrealized PnL
3. fills
4. fees
5. funding
6. positions
7. margin / liquidation risk

### 15.2 欄位

| 欄位 | 說明 |
| --- | --- |
| `timestamp` | snapshot 時間 |
| `mode` | `paper` / `live` |
| `wallet_balance` | 錢包餘額 |
| `account_equity` | 帳戶淨值 |
| `available_balance` | 可用餘額 |
| `realized_pnl` | 累積已實現損益 |
| `unrealized_pnl` | 總未實現損益 |
| `total_pnl` | 總損益 |
| `total_fees` | 累積手續費 |
| `net_funding_pnl` | 累積 signed funding PnL；收入為正，成本為負 |
| `total_position_notional` | 總名目曝險 |
| `effective_leverage` | 實際槓桿 |
| `used_initial_margin` | 已使用 initial margin |
| `total_maintenance_margin` | 總 maintenance margin |
| `margin_ratio` | `account_equity / total_maintenance_margin` |

---

## 16. `position_snapshots.csv`

紀錄每個 symbol 的倉位狀態，用來追蹤 entry、PnL、exposure、SL / TP。

### 16.1 紀錄時機

- 每個 cycle 結束時記錄
- 建議與 `account_snapshots.csv` 同步

若想精簡，只記錄：

1. 有持倉的 symbol
2. 當期有 decision / order / fill 的 symbol

### 16.2 欄位

| 欄位 | 說明 |
| --- | --- |
| `timestamp` | snapshot 時間 |
| `mode` | `paper` / `live` |
| `symbol` | 交易標的 |
| `position_size` | 倉位數量；long 為正，short 為負 |
| `side` | `long` / `short` / `flat` |
| `entry_price` | 平均進場價 |
| `mark_price` | 當前 mark price |
| `position_notional` | `abs(position_size * mark_price)` |
| `exposure_pct` | `position_notional / account_equity * 100` |
| `unrealized_pnl` | 未實現損益 |
| `realized_pnl` | 該 symbol 累積已實現損益 |
| `maintenance_margin` | 該倉位 maintenance margin |
| `estimated_liquidation_price` | Paper mode 依 6.6.1 求得的估算值；無正數清算點時可為 `null` |
| `exchange_liquidation_price` | Live mode 由 Hyperliquid API 取得；paper 為空 |
| `margin_tier_id` | 本次 maintenance-margin 計算使用的 tier / table id |
| `maintenance_margin_rate` | 本次適用的 maintenance margin rate |
| `maintenance_deduction` | 本次適用的 tier deduction |
| `liquidation_model_version` | Paper liquidation model 版本；live 可留空 |
| `stop_loss_price` | 目前有效 SL 價格 |
| `take_profit_price` | 目前有效 TP 價格 |

---

## 17. 紀錄頻率總結

| CSV | 紀錄時機 | 頻率 |
| --- | --- | --- |
| `ai_inputs.csv` | 呼叫 AI / strategy 前 | 通常每 4H 一次 |
| `ai_outputs.csv` | AI / strategy 產生 output 時 | 通常每 4H 一次 |
| `orders.csv` | order 建立或狀態變化時 | event-based |
| `fills.csv` | 成交發生時 | event-based |
| `funding_events.csv` | 每個 funding timestamp；失敗時後續補帳 | 每小時 / event-based |
| `account_snapshots.csv` | 每個 cycle 狀態更新完成後 | 通常每 4H 一次 |
| `position_snapshots.csv` | 每個 cycle 狀態更新完成後 | 通常每 4H 一次 |

---

## 18. 第一版取捨

第一版暫時不獨立建立 `risk_events.csv`。

理由：

- Stop loss、take profit 可先透過 `orders.csv` 的 `order_role` 追蹤
- Rejected order 可先透過 `orders.csv` 的 `status = rejected` 追蹤
- 成交與 PnL 變化可透過 `fills.csv`、`account_snapshots.csv`、`position_snapshots.csv` 追蹤
- 若未來需要分析 RiskGate 拒單原因，再新增 `risk_events.csv`

---

## Phase 2 驗收標準

Phase 2 = **AI decision → target position → paper orders → simulated fills → transactional SQLite state → optional CSV exports**。

不下真單。

不需要 private key。

不做 exchange-side reconciliation；這些留到 Phase 3+。

### 必跑輪數

最低驗收：

```
BTC 單一標的，4H interval，至少 30 cycles
```

約等於 5 天。

進 Phase 3 前建議：

```
BTC 單一標的，4H interval，至少 60 cycles
```

約等於 10 天，可以多觀察 funding、SL/TP、rebalance 行為。

### 必須通過

| 檢查項目 | 驗收條件 |
| --- | --- |
| Decision → Order | 每筆 paper order 都要有來源 `output_id`；SL/TP 這類系統單則需能對應到 active position |
| Order → Fill | 每筆 fill 都要有合法 `order_id`，不可有 orphan fill |
| Fill → Position | accounting replay 已記錄 fills 後算出的 position 要和 `position_snapshots.csv` 一致 |
| PnL | `realized_pnl`、`unrealized_pnl`、fee、funding、`total_pnl` 都要能重算 |
| Account state | `account_snapshots.csv` 要能由 position、mark price、fee、funding、margin 設定重算驗證 |
| Input boundary | AI 只能使用已封閉的 `4h` candles；order 只能使用 `active_from` 之後的即時 market snapshots |
| Risk limits | AI requested target 必須介於 `0–100%`；`approved_target_margin_pct` 不得超過 `max_target_margin_pct = 60`；`effective_leverage` 不得超過設定槓桿 |
| SL/TP | 有倉位時要有有效 reduce-only SL/TP；空倉時不可有 active SL/TP |
| Accounting replay | 使用相同的已記錄 fills、fees 與 funding events 重建時，positions、account state 與 PnL 結果一致 |

### 驗收輸出指標

驗收 run 結束後，至少輸出以下 summary：

| 指標 | 用途 |
| --- | --- |
| `cycle_count` | 完成幾個 cycle |
| `order_count` | 產生幾筆 paper orders |
| `fill_count` | 模擬成交幾筆 fills |
| `rejected_order_count` | RiskGate 拒絕幾筆 orders |
| `orphan_order_count` | 找不到來源的 orders 數 |
| `orphan_fill_count` | 找不到 order 的 fills 數 |
| `snapshot_mismatch_count` | position/account snapshot 重算不一致次數 |
| `accounting_replay_mismatch_count` | accounting/event replay 結果不一致次數 |
| `max_exposure_pct` | 測試期間最大曝險 |
| `max_effective_leverage` | 測試期間最大實際槓桿 |
| `total_pnl` | 扣除 fee / funding 後的總 PnL |
| `total_fees` | 累積模擬手續費 |
| `net_funding_pnl` | 累積 signed funding PnL |

### Phase 2 不要求

Phase 2 不要求策略賺錢。

以下不作為驗收條件：

```
total_pnl > 0
win_rate > 50%
```

Phase 2 只驗證 paper trading 系統是否：

- 內部一致
- 可追蹤
- 可重算
- 可重現

策略 profitability 留到後續再評估。

### 可以進 Phase 3 的條件

符合以下條件後，可以開始 Phase 3：

```
cycle_count >= 30
orphan_fill_count = 0
snapshot_mismatch_count = 0
accounting_replay_mismatch_count = 0
no unhandled exceptions
same recorded accounting events rebuild to same positions, account state, and PnL
```

Phase 3 再開始處理：

- exchange order placement
- user fills / websocket reconciliation
- live account / position reconciliation
- real fee / funding comparison
- small-capital 或 shadow live mode
