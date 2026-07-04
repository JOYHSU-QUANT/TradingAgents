# Phase 2 spec — paper trading validation

Phase 2 的可執行規格：目標、風控參數、cycle 排程、第一版取捨、驗收標準與建置順序。
執行與模擬設計見 [phase2-execution](./phase2-execution.md)，資料 schema 見
[phase2-data](./phase2-data.md)，決策契約見 [DESIGN](./DESIGN.md) Part 2。
Paper trading 預設參數（initial balance、taker fee、market monitor、fill_model）定義於
phase2-execution.md 5.4。

---

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

## 2. Risk & Margin：風控與保證金

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
  rebalance_deadband_pct: 1
  min_confidence: 0.3
```

### 2.1 Leverage

第一版使用：

```
leverage = 1
```

---

### 2.2 Margin Mode

第一版使用：

```
margin_mode = cross
```

---

### 2.3 最大目標 Margin Allocation

```
max_target_margin_pct = 60
```

RiskGate 需保證：

```
0 <= requested_target_margin_pct <= 100
approved_target_margin_pct = min(requested_target_margin_pct, max_target_margin_pct)
```

AI 可在 `0–100%` 內提出 requested target。若 requested target 介於 `61–100%`，RiskGate 必須 clamp 為 `60%`，並同時保留 requested / approved 數值與 `risk_action = clamped`。負數、超過 `100%`、非數字或與 `target_side` 不一致的輸出視為 invalid decision，fail-closed 成 `maintain_current`。

RiskGate 仍必須獨立檢查 `effective_leverage` 與 available margin，不能只依賴 margin allocation 上限；這兩個檢查同樣以 clamp 方式進一步收緊 `approved_target_margin_pct`（`risk_action = clamped`，`risk_reason` 分別為 `effective_leverage_cap`／`insufficient_available_margin`），因此上式的 `min(...)` 只是 allocation cap 這一關，最終 approved 是三個 cap 取最緊。在 Phase 2 預設 `account_equity = 1,000 USDC` 與 `leverage = 1` 下，`60%` 上限對應 `600 USDC` target margin 與 `600 USDC` target notional。

---

### 2.4 Target Margin 刻度、Rebalance Deadband 與信心門檻

```
decision:
  ai_target_margin_min_pct: 0
  ai_target_margin_max_pct: 100
  target_margin_step_pct: 1
  rebalance_deadband_pct: 1
  min_confidence: 0.3
```

**刻度（`target_margin_step_pct`）**：`requested_target_margin_pct` 的合法值集合為 `{min, min+step, …, max}`，依預設值即整數 `0–100`。非整數步進、超出範圍或非數字 → invalid decision，fail-closed 成 `maintain_current`，記 `risk_action = invalid_fail_closed`。不做 silent rounding：四捨五入會讓 requested 與實際使用值對不上，並掩蓋 AI 輸出品質問題。

**Deadband（`rebalance_deadband_pct`）**：RiskGate 產生 approved target 後、建立 order 之前，若 `target_side` 與目前持倉方向相同且：

```text
abs(approved_target_margin_pct - current_margin_pct) < rebalance_deadband_pct
```

則不建立 order，記 `order_created = false`、`no_order_reason = within_deadband`。**Flip 與 flat 平倉不適用 deadband**——方向相反或要求歸零時，再小的差距都必須執行。Deadband 也只在倉位**實際槓桿**與 `risk.leverage` 一致（或交易所未回報）時適用：margin% 只有在槓桿一致時才代表名目曝險，已知不一致（例如手動開的倉）時 deadband 停用，讓訂單執行、把真實名目收斂到 target。本節是 phase2-data.md 的 `ai_outputs.csv` 一章 `no_order_reason = within_deadband` 的正式定義。

**信心門檻（`min_confidence`）**：`confidence` 僅作記錄與事後分析，**不參與 sizing**——`approved_target_margin_pct` 不得乘上 `confidence`。驗證規則：

```text
confidence 非數字或超出 0–1                        → invalid decision，fail-closed 成 maintain_current
decision_mode = set_target 且 confidence < 0.3     → 契約合法但被風控拒絕：maintain_current，
                                                     記 risk_action = rejected、risk_reason = low_confidence
                                                     （rejected 是正常風控運作；invalid_fail_closed
                                                     保留給契約違規，供 model-drift 告警使用）
```

理由：LLM 的 confidence 未經校準，直接乘進 sizing 會把噪音放大成倉位；AI 的 conviction 應直接反映在 `requested_target_margin_pct`，prompt 必須明確要求兩者一致。本門檻只負責擋下「高倉位、低信心」這類自相矛盾的輸出。是否引入 confidence-aware sizing，待 Phase 2 paper 累積的 confidence 與績效資料驗證其預測力後，於 Phase 3+ 再議。

---

## 3. Cycle 時間

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

新 `run_id` 視為沒有 previous decision 並立即執行。相同 `run_id` 的一般 restart 必須延續 SQLite scheduler state；唯一會提前重設四小時計時的情況，是依 phase2-execution.md 1.2 節取消重啟時發現的 unfinished TWAP / flip plan，並成功完成新的 AI decision。

### 3.1 Decision API failure and retry

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

## 4. 第一版取捨

第一版暫時不獨立建立 `risk_events.csv`。

理由：

- Stop loss、take profit 可先透過 `orders.csv` 的 `order_role` 追蹤
- Rejected order 可先透過 `orders.csv` 的 `status = rejected` 追蹤
- 成交與 PnL 變化可透過 `fills.csv`、`account_snapshots.csv`、`position_snapshots.csv` 追蹤
- 若未來需要分析 RiskGate 拒單原因，再新增 `risk_events.csv`

---

## 5. 驗收標準

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
沒有未處理的 exceptions
相同的已記錄 accounting events 能重建出相同的 positions、account state 與 PnL
```

Phase 3 再開始處理：

- exchange order placement
- user fills / websocket reconciliation
- live account / position reconciliation
- real fee / funding comparison
- small-capital 或 shadow live mode

---

## 6. 建置順序

1. 決策契約遷移 — structured target schema、fail-closed 驗證、prompt 改版（DESIGN Part 2）；退役 Phase 1 的 rating 解析與 tier 設定。
2. RiskGate — max margin clamp、step / `min_confidence` 檢查、`effective_leverage` 與 available margin 獨立檢查（本文件 §2）。
3. SQLite persistence 骨架 — tables、transaction 語意、去重鍵、accounting replay 驗證（phase2-data）。
4. Paper 帳務 — fills / fees / funding exactly-once 過帳、margin 與清算價模型（phase2-execution §6）。
5. 執行引擎 — `paper_market`、TWAP / flip plan、SL / TP lifecycle、market monitor（phase2-execution §1–5）。
6. Scheduler — 4h rolling cycle、retry、重啟 reconciliation（本文件 §3）。
7. CSV export 與驗收 run — §5 的 30 cycles 驗收。
