# Phase 2 — 執行與模擬設計

Phase 2 paper trading 的執行層設計參考：目標倉位調整（TWAP / flip）、Stop Loss / Take Profit、
paper 成交模擬與模擬數值公式。決策契約見 [DESIGN](./DESIGN.md) Part 2；風控參數、排程與驗收
見 [phase2-spec](./phase2-spec.md)；SQLite / CSV schema 見 [phase2-data](./phase2-data.md)。

---

## 1. 目標倉位調整

策略每 4 小時產生一次新預測與目標倉位。

當新目標倉位產生後，系統使用 TWAP 單在預測完成後約 1 小時內，逐步調整至目標倉位。

```
AI output → target position → TWAP rebalance orders → fills → position update
```

### 1.1 Phase 2 TWAP execution model

Phase 2 是 **forward paper trading**，不是歷史回測：

- 策略每四小時以已封閉的 `4h` K 線產生一次新 decision。
- Paper TWAP 在約一小時內逐步調整本地模擬倉位。
- Paper TWAP 依照 Hyperliquid 原生節奏，每 30 秒建立一個本地 execution slice（約每小時 120 個 slices）。
- 每個 slice 執行時，系統讀取當下公開的 `mid_price`，套用設定的 paper slippage 後記錄一筆 simulated fill。
- Paper orders 與 fills 只存在本地。系統不得呼叫交易所下單 endpoint，也不需要 private key。
- Phase 3 live execution 才送出一個原生 TWAP request；屆時由 Hyperliquid（而非本系統）排程 live 的 30 秒子單。

**Paper market data 新鮮度與中斷處理**

每個排定的 paper TWAP slice 都必須發出一次新的公開 market-data request。只有在 `5` 秒 request timeout 內、由同一份新 snapshot 同時提供有效的 `mid_price` 與 `mark_price` 時，該回應才有效。價格沒有變動不代表資料過期；新鮮度由「是否成功取得新回應」決定，而不是由「價格是否變動」決定。

每次 request 都要記錄 `requested_at`、`received_at`、latency 與結果。不得使用快取價格或上一次成功的 snapshot 偽造 fill。

```text
5 秒內取得新 snapshot                     → 模擬本次排定的 slice
timeout / request 錯誤 / mid 或 mark 無效 → 該 slice 留空不成交
連續 3 個 slices 失敗                     → plan status = paused_market_data
```

處於 `paused_market_data` 期間，系統仍每 30 秒探測一次。取得有效 snapshot 後，後續排定的 slices 可恢復執行。錯過的 slices 不補跑，也不得在恢復當下的價格一次爆量執行。原始 plan deadline 不延長；到一小時 deadline 時，所有未成交數量記為 `residual_qty`，plan 進入對應的 terminal state。

資料中斷期間，paper engine 無法可靠判斷 SL trigger。恢復後的第一個有效 snapshot，若 `mark_price` 已越過 active SL trigger，系統立即以當下 `mid_price` 加上設定的 adverse slippage 模擬 reduce-only stop fill，並記錄 `fill_reason = gap_stop_fill`；不得宣稱倉位是在更早的 SL trigger price 成交。

### 1.2 TWAP 最小下單量與 duration

建立 paper TWAP 時使用以下定義：

- `min_notional`：設定的單筆 order 最小名目價值；預設 `10` USDC（見 5.4 的 `min_notional_usdc`）。
- `mid`：建立 TWAP plan 時觀察到的當下公開 mid price。
- `ceil_to_step`：把數量向上湊整到該 asset 合法的 `szDecimals` step。
- `raw_total_qty`：target-position 調整所需、尚未湊整的數量；於 plan-build 依 6.2 節公式重算。
- `qty_step = 10 ^ (-szDecimals)`，`total_qty = floor_to_step(raw_total_qty)`。

```text
min_order_qty = ceil_to_step(min_notional / mid)
max_legal_slices = floor(total_qty / min_order_qty)
planned_slices = min(max_legal_slices, 120)

0 slices → reject / residual
1 slice  → paper_market（Phase 3 對應 Market / IOC）
2–120    → TWAP，每 30 秒一個 slice

duration = planned_slices × 30 秒
```

120-slice 上限使一個 paper TWAP 最長一小時。湊不出一個合法 slice 的數量，不得向上湊整成過大的倉位；它維持 rejected 或 residual。

`planned_slices >= 1` 時，以整數 quantity steps 分配數量，而不是直接對浮點數量做除法：

```text
total_steps = integer(total_qty / qty_step)
base_steps  = floor(total_steps / planned_slices)
extra_steps = total_steps % planned_slices

slice[0 : extra_steps]              = (base_steps + 1) × qty_step
slice[extra_steps : planned_slices] = base_steps × qty_step
```

這保證規劃出的 slice 數量總和正好等於 `total_qty`。例如 `total_qty = 1.03`、`qty_step = 0.01`、`planned_slices = 4` 會產生 `0.26, 0.26, 0.26, 0.25`。

`raw_total_qty` 轉成 `total_qty` 時去掉的數量記為 `rounding_residual_qty = raw_total_qty - total_qty`；不得向上湊整，否則可能超過核准的 target。

`min_notional` 只在建立 paper plan 時、用當下取得的新鮮 `mid` 評估一次。之後的價格變動不重新切分、也不使已規劃的 paper slices 失效。Phase 2 不會把這些 slices 送到交易所，因此不得在每個 simulated fill 重新套用交易所的 minimum-notional 驗證。Phase 3 live execution 則依交易所當下的驗證與實際成交結果為準。

每個 TWAP / flip plan 最晚必須在建立後一小時進入 terminal state。正常運作下，下一個 `4h` AI cycle 因此不會與上一個 execution plan 重疊。

若 process 啟動時發現未完成的 TWAP / flip plan，系統不恢復、不追趕、也不完成舊 target。重啟建立新的 decision boundary：

```text
1. 停止舊 plan 所有剩餘 slices
2. 將舊 plan 標記為 canceled_restart
3. 所有未執行數量記為 residual_qty
4. 從已 committed 的 SQLite fills 重建實際 position
5. 補帳到期的 funding events 並取得新鮮 market snapshot
6. 先處理 liquidation / emergency close / gap SL / TP
7. Reconcile position、account、SL / TP 與 scheduler state
8. 立即以重建後的當前狀態開始一次新的 AI cycle
9. next_decision_at = 新 decision_at + 4 小時
```

不論重啟發生在舊 plan deadline 之前或之後，本規則都適用。錯過的 slices 永不補跑、中斷 plan 的未來 slices 永不恢復、舊 target 永不用來推測新 target。若新的 AI / market API 嘗試失敗，系統依 phase2-spec.md 3.1 節處理，保持 reconciled 後的目前 position 受保護；不得重新啟用已取消的 plan。

系統不得讓舊 plan 的 slices 與較新 `output_id` 衍生的 orders 同時執行。

### 1.3 反向 target 與 sequential flip

當核准的 target side 與目前倉位方向相反時，系統建立一個含兩個依序執行 legs 的 flip plan：

```text
FlipPlan
├─ Close leg：reduce-only，目前倉位 -> flat
└─ Open leg： 非 reduce-only，flat -> 核准的反向 target
```

Close leg 完全成交、本地 position 確認為 flat 之前，open leg 不得開始。開始 open leg 之前，系統以最新的 market 與本地 account state 重跑一次 deterministic RiskGate 檢查，不再次呼叫 AI。

兩個 legs 共用同一個 `output_id` 與 `flip_plan_id`，合計 execution budget 最多 120 slices / 一小時。Slices 依兩個 legs 的數量比例分配，並須符合合法 size-step 與 minimum-notional 規則。

若 close leg 未完成、plan 到期、market data 無法取得、或 RiskGate 拒絕 open leg，系統不得開反向倉位。記錄 `flip_incomplete` 與原因，剩餘 target 留給下一個 `4h` decision 重新考慮。

---

## 2. Stop Loss / Take Profit

系統使用：

- Stop Loss：`SL Market Order reduce-only`
- Take Profit：`TP Market Order reduce-only`

SL / TP 都只用來減倉或平倉，不應增加反向倉位。

Protection orders 必須遵守以下 invariant：

```text
position_size == 0                → 不得有 active SL / TP
position_size != 0                → SL 數量必須涵蓋全部目前倉位
結算完成（plan terminal）的倉位   → TP 數量必須涵蓋全部目前倉位
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

### 3.5 Stop Loss 參數

```
sl_min_pct = 5%
sl_max_pct = 10%
sl_target_pct = (sl_min_pct + sl_max_pct) / 2
liq_buffer = 10%
```

---

### 3.6 Risk Gate

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

### 3.7 Stop Loss 價格公式

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

Phase 2 不會向交易所送出 Market / IOC。RiskGate、最小數量、可用 margin 與 rounding 都是在 order 進入成交流程前完成的驗證：未通過時建立一筆 `status = rejected` 的 order row 並記錄 `status_reason`；該 order 不進入成交模擬，也不得事後描述成「未成交」。

通過驗證後建立的 `paper_market`，在 `active_from` 後取得第一個有效即時 market snapshot 時，必須以完整 order quantity 產生一筆 simulated fill；Phase 2 不模擬部分成交。Phase 3 live execution 才將此意圖轉換成真實 Market / aggressive IOC，並採用交易所回報的實際成交結果。

預設：

```
slippage_bps = 5
```

`mid_price` 必須是執行當時取得的真實值。若暫時無法取得 `mid_price`，order 狀態設為 `pending_market_data`；這不代表交易所拒絕或未成交。系統不得以 `mark_price` 或舊價格偽造 fill，取得有效 snapshot 後才完整模擬成交。

#### Buy

```
fill_price = mid_price * (1 + slippage_bps / 10_000)
```

#### Sell

```
fill_price = mid_price * (1 - slippage_bps / 10_000)
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
    min_notional_usdc: 10

    market_monitor:
      interval_seconds: 30
      request_timeout_seconds: 5

    fill_model:
      slippage_bps: 5
```

`fill_model` 是所有 simulated fills 共用的成交參數（`paper_market`、TWAP slices、SL / TP 與 gap-stop fills），成交參考價一律為執行當時取得的 `mid_price`（見 5.2）。`min_notional_usdc` 即 1.2 節的 `min_notional`，對應交易所單筆 order 至少 `10 USDC` 的規則。

`initial_balance_usdc` 與 `initial_positions` 只在建立新的 paper `run_id` 時套用。一般程式重啟必須從已記錄的 accounting events / snapshots 恢復上次的本地 account 與 position state，不得重設為 1,000 USDC。

Phase 2 的 paper account 不讀取真實 Hyperliquid wallet balance 或 position，也不需要 private key。

Phase 2 不使用 candle high / low / volume 模擬 limit fill。Limit-order partial-fill 模型與 historical backtesting 延後至後續階段。

### 5.5 Phase 2 position-aware market monitor

Phase 2 的 SL / TP、liquidation、unrealized PnL 與 TWAP fills 都是本地模擬，因此只要存在任何 active position、active TWAP / flip plan、`paper_market`、SL 或 TP，paper engine 就必須啟動 market-monitor loop，每 `30` 秒取得一次新的 `mid_price` / `mark_price` snapshot，並依 5.3 節的固定事件順序處理。

```text
position != 0 or active plan/order exists → poll every 30 seconds
position == 0 and no active plan/order    → stop 30-second polling
```

若同一時間點也是 TWAP slice 的 scheduled time，monitor 與 slice 必須共用同一份有效 snapshot，不得為同一 logical tick 重複呼叫 API 或使用兩組不同價格。Request timeout、stale-data pause、恢復後的 `gap_stop_fill` 均沿用 1.1 節規則。

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
| `approved_target_margin_pct` | `min(requested_target_margin_pct, max_target_margin_pct)` | RiskGate 實際核准的 margin allocation 比例（此為 allocation cap；available margin 與 effective leverage 檢查可再往下 clamp，見 spec §2.3） |
| `target_margin` | `account_equity * approved_target_margin_pct / 100` | RiskGate 核准用於目標倉位的 margin |
| `target_notional` | `target_margin * configured_leverage` | 套用槓桿後的目標名目倉位 magnitude |
| `target_signed_notional` | `direction * target_notional` | long 為正、short 為負的目標名目倉位 |
| `current_signed_notional` | `position_size * mark_price` | 目前方向性倉位；long 為正，short 為負 |
| `delta_notional` | `target_signed_notional - current_signed_notional` | 目標倉位與目前倉位的差額 |

上表的 `target_margin` / `target_notional` / `target_signed_notional` / `delta_notional` 是 RiskGate 以**決策當下**的 `mark_price` 與 `account_equity` 算出的 snapshot，寫進 audit record 供事後重現，屬 **audit-only**——執行層不得沿用它們換算下單數量。4h 決策與 plan 建立（`active_from`）之間價格會漂移；`approved_target_margin_pct` 以**執行時價格**兌現，下單數量在 plan-build 時對新鮮 snapshot 重算：

```text
# plan-build（見 1.2 的 raw_total_qty）：以新鮮 snapshot 重算，不沿用決策時的 delta_notional
fresh_target_notional        = account_equity_fresh × approved_target_margin_pct / 100 × configured_leverage
fresh_target_signed_notional = direction × fresh_target_notional
raw_total_qty                = abs(fresh_target_signed_notional / mark_fresh − position_size)
```

`requested_target_margin_pct` 是帳戶淨值中 AI 建議分配為目標倉位 margin 的比例，而不是名目曝險比例。AI 可輸出 `0–100%`，但真正用於下單的數值必須是 RiskGate 產生的 `approved_target_margin_pct`。方向不寫在百分比的正負號中，由 `target_side` 提供。

```text
account_equity = 1,000 USDC
requested_target_margin_pct = 20
approved_target_margin_pct = 20
configured_leverage = 5

target_margin = 1,000 * 20 / 100 = 200 USDC
target_notional = 200 * 5 = 1,000 USDC
```

> 相容性說明：RiskGate 以 `position.margin_used / account_value` 計算目前 margin allocation，與本節的 margin-based 定義一致——但此等式只在倉位**實際槓桿**等於 `configured_leverage` 時才代表名目曝險。RiskGate 對已知的槓桿不匹配（例如手動開的倉）會停用 rebalance deadband（見 phase2-spec.md §2.4），讓訂單把真實名目收斂到 target。

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
| `market_buy_fill_price` | `mid_price * (1 + slippage_bps / 10_000)` | `paper_market` buy 的模擬成交價 |
| `market_sell_fill_price` | `mid_price * (1 - slippage_bps / 10_000)` | `paper_market` sell 的模擬成交價 |
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

