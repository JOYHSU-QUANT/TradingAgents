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
  resize_min_confidence: 0.7
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
  resize_min_confidence: 0.7
```

**刻度（`target_margin_step_pct`）**：`requested_target_margin_pct` 的合法值集合為 `{min, min+step, …, max}`，依預設值即整數 `0–100`。非整數步進、超出範圍或非數字 → invalid decision，fail-closed 成 `maintain_current`，記 `risk_action = invalid_fail_closed`。不做 silent rounding：四捨五入會讓 requested 與實際使用值對不上，並掩蓋 AI 輸出品質問題。

**Deadband（`rebalance_deadband_pct`）**：RiskGate 產生 approved target 後、建立 order 之前，若 `target_side` 與目前持倉方向相同且：

```text
abs(approved_target_margin_pct - current_margin_pct) < rebalance_deadband_pct
```

則不建立 order，記 `order_created = false`、`no_order_reason = within_deadband`。**Flip 與 flat 平倉不適用 deadband**——方向相反或要求歸零時，再小的差距都必須執行。Deadband 也只在倉位**實際槓桿**與 `risk.leverage` 一致（或交易所未回報）時適用：margin% 只有在槓桿一致時才代表名目曝險，已知不一致（例如手動開的倉）時 deadband 停用，收斂單不被 deadband 吞掉（仍受 resize 信心門檻約束，見下方互動註記）、把真實名目收斂到 target。本節是 phase2-data.md 的 `ai_outputs.csv` 一章 `no_order_reason = within_deadband` 的正式定義。

**信心門檻（`min_confidence`）**：`confidence` 僅作記錄與事後分析，**不參與 sizing**——`approved_target_margin_pct` 不得乘上 `confidence`。驗證規則：

```text
confidence 非數字或超出 0–1                        → invalid decision，fail-closed 成 maintain_current
decision_mode = set_target 且 confidence < 0.3     → 契約合法但被風控拒絕：maintain_current，
                                                     記 risk_action = rejected、risk_reason = low_confidence
                                                     （rejected 是正常風控運作；invalid_fail_closed
                                                     保留給契約違規，供 model-drift 告警使用）
set_target 且 target_side 與目前持倉方向相同        → 同方向 resize 專用門檻（resize_min_confidence）：
且該目標會實際產生訂單                               maintain_current，記 risk_action = rejected、
且 confidence < 0.7                                  risk_reason = low_confidence_resize。
                                                     只 gate「會下單」的 resize——deadband 內或
                                                     zero-delta 的重申不付費，維持原本判定
                                                     （approved；cap 拉回 deadband 時 clamped）+
                                                     within_deadband／zero_delta；權益歸零仍先記
                                                     no_account_equity（門檻檢查排在兩者之後）。
                                                     加倉與減倉**都適用**；flat→建倉、方向翻轉
                                                     （含 flip 第二腿——從 flat 出發）、明確 flat
                                                     平倉都只走 min_confidence。
                                                     載入時要求 resize_min_confidence >= min_confidence
                                                     （更低的 resize 門檻永遠不會生效——基本門檻先拒）。
```

**同方向 resize 門檻（`resize_min_confidence`）的理由**：2026-07 paper baseline
顯示，中等信心（0.6–0.68）的同方向倉位微調構成主要的手續費 churn——先減後加的
來回把費用付了兩次，而方向 edge 不足以賺回。開倉／翻轉／平倉是方向性決策，
維持原門檻；改變既有倉位大小則必須有更高的 conviction 才值得付 rebalance 成本。
**減倉刻意包含在內**（churn 的第一腿就是中等信心的減倉）：這造成「中等信心大幅
去險」被擋、但 flat 平倉只需 min_confidence 的不連續——是接受過的取捨，緊急
去險路徑（flat 平倉、SL/TP）永遠不受此門檻影響。

**與其他機制的交互**（皆為刻意行為，2026-07 review 定案）：

- **先 clamp、後查 resize 門檻**——「會不會下單」以 clamp 後的 approved 值計算。
  cap 把大請求拉回到 deadband 內時，記 `within_deadband`（不視為 churn 意圖、
  不觸發本門檻）；clamp 後仍會下單而信心不足時，REJECTED 蓋過 clamp——依既有
  REJECTED 契約，audit 只保留 requested、`approved_target_margin_pct = null`，
  cap-binding 統計因此不含這些列。
- **leverage-mismatch 收斂單同樣受此門檻約束**：實際槓桿與 `risk.leverage` 不一致
  而停用 deadband 的同方向收斂單也是「會下單的 resize」，信心不足一樣被拒。
  Phase 2 paper 不會產生這種倉位；Phase 3 的 adopt／手動倉位需注意——緊急收斂
  一律走 flat 平倉或 SL/TP。
- **cap 與 deadband 的遮蔽區（不可增倉區）**：保證金已落在
  `(有效上限 − deadband, 有效上限]` 的同方向倉位無法再加倉——任何更高的請求都會
  被 clamp 到有效上限、隨即落入 deadband，記 `within_deadband` 不下單。這是
  deadband 遮蔽 cap 的固有現象，但 paper 調參（deadband=10、cap=60）把遮蔽區
  放大到 `(50, 60]`：PnL 把倉位漂到這一區後，唯一的改倉出路是大幅減倉
  （≥ deadband 且過 resize 門檻）、翻向或 flat。prompt 廣告的有效上限在此區間內
  實際不可達，屬已知並接受的取捨。
- **「不付費不 gate」原則僅適用本門檻**：基本 `min_confidence` 門檻刻意無條件
  先檢（評估順序在前）——deadband 內的重申若 confidence 低於基本門檻仍記
  `rejected`／`low_confidence`。不要以本門檻的 order-necessity 原則「調和」
  基本門檻的檢查時機。
- **兩步繞道與量測盲點**：本門檻不阻止「flat 平倉（只需 min_confidence）→
  下一 cycle 同方向重開」的兩步改倉。position-blind 模型無法刻意繞道，但
  發生時付兩條全倉費用，且 churn 偵測若只配對相鄰 rebalance 的
  reduce→rebuild（/paper-review 的凍結定義），看不到這種形狀——平倉腿
  `target_side = flat`、重開腿 `order_role = entry`。檢討 gate 成效時
  churn=0 不能單獨當證據：摩擦佔比未同步下降時，先查 flat→同向重開配對。
- **Phase 3 live 路徑同樣適用**：本門檻是共用 RiskGate 的一部分（含 code 預設
  0.7，即使 YAML 未列出此 key），合回 `hyperliquid-adapter` 後 live 執行一體
  繼承——paper 段即其驗證段。

理由：LLM 的 confidence 未經校準，直接乘進 sizing 會把噪音放大成倉位；AI 的 conviction 應直接反映在 `requested_target_margin_pct`，prompt 必須明確要求兩者一致。`min_confidence` 門檻只負責擋下「高倉位、低信心」這類自相矛盾的輸出。是否引入 confidence-aware sizing，待 Phase 2 paper 累積的 confidence 與績效資料驗證其預測力後，於 Phase 3+ 再議。**分析 caveat**：prompt v2 起已向模型廣告 `min_confidence` 與 `resize_min_confidence` 的具體數值，模型因此有動機在想改倉位時報出跨過門檻的值——v2 段資料裡的 confidence 是 threshold-aware 的策略性變數，不再是未受激勵的純評估；預測力分析必須以廣告門檻為條件（例如分開檢視門檻上下的分佈與聚集），並注意與 v1 段（未廣告門檻）不可直接混併。

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
2. RiskGate — max margin clamp、step / `min_confidence`（同向 resize 另有 `resize_min_confidence`）檢查、`effective_leverage` 與 available margin 獨立檢查（本文件 §2）。
3. SQLite persistence 骨架 — tables、transaction 語意、去重鍵、accounting replay 驗證（phase2-data）。
4. Paper 帳務 — fills / fees / funding exactly-once 過帳、margin 與清算價模型（phase2-execution §6）。
5. 執行引擎 — `paper_market`、TWAP / flip plan、SL / TP lifecycle、market monitor（phase2-execution §1–5）。
6. Scheduler — 4h rolling cycle、retry、重啟 reconciliation（本文件 §3）。
7. CSV export 與驗收 run — §5 的 30 cycles 驗收。
