# Phase 2 — 資料 Schema

SQLite persistence 與 CSV export 的正式 schema 參考。執行語意見
[phase2-execution](./phase2-execution.md)；排程、風控參數與驗收見 [phase2-spec](./phase2-spec.md)。

---

## 1. Persistence：SQLite 為唯一 source of truth

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

### 1.1 CSV export timing and atomicity

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

### 1.2 SQLite tables and existing CSV schemas

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

## 2. Paper / Live 共用 Schema

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

## 3. 資料關聯

### 3.1 Logical tables 與 CSV export 關聯

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

### 3.2 主要追蹤鏈路

```
AI input
  → AI output
  → Order
  → Fill
  → Funding event
  → Account / Position snapshot
```

---

## 4. CSV export 總覽

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

## 5. `ai_inputs.csv`

紀錄每次送進 AI / strategy 的輸入摘要，用來追蹤 AI 當時看到了什麼資料。

不建議把完整 prompt 或完整 market data 全部塞進 SQLite columns 或 CSV。完整內容可另外存成 JSON 檔，SQLite 保存摘要、路徑、hash 與 timestamp，CSV 匯出這些追蹤欄位。

### 5.1 紀錄時機

- 每次呼叫 AI / strategy 前記錄一次
- 若策略每 4H 跑一次，則每 4H 記錄一筆
- 此紀錄代表「AI 當時看到了什麼資料」

### 5.2 欄位

| 欄位 | 說明 |
| --- | --- |
| `timestamp` | AI input 建立時間 |
| `mode` | `paper` / `live` |
| `run_id` | Paper / live run id |
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

## 6. `decision_attempts.csv`

紀錄每個 scheduled cycle 的 AI decision attempt 與 retry 狀態，是 phase2-spec.md 3.1 節 retry 規則的正式 export。

### 6.1 紀錄時機

- 每個 scheduled cycle 建立 attempt 時記錄一筆 logical record
- 每次 retry、成功、或進入 terminal status（`api_failed` / `invalid_output`）時更新同一筆 record

### 6.2 欄位

| 欄位 | 說明 |
| --- | --- |
| `timestamp` | 最後一次狀態變化時間 |
| `mode` | `paper` / `live` |
| `run_id` | Paper / live run id |
| `decision_attempt_id` | Deterministic id，由 `run_id + scheduled_at` 產生 |
| `scheduled_at` | 本 cycle 預定執行時間 |
| `input_id` | 對應 `ai_inputs.csv`；尚未建立 input 時留空 |
| `output_id` | 成功產生 output 時對應 `ai_outputs.csv`；失敗留空 |
| `attempt_count` | 已執行的嘗試次數，最多 `3` |
| `first_attempt_at` | 第一次嘗試時間 |
| `last_attempt_at` | 最後一次嘗試時間 |
| `status` | `in_progress` / `completed` / `api_failed` / `invalid_output` |
| `error_type` | `timeout` / `rate_limit` / `connection` / `server_error` 等；成功可留空 |
| `error_message` | 最後一次錯誤摘要；成功可留空 |
| `next_decision_at` | 本 attempt 終結後排定的下一個 cycle 時間 |

---

## 7. `ai_outputs.csv`

紀錄 AI / strategy 每次輸出的交易意圖與目標倉位。

### 7.1 紀錄時機

- 每次 AI / strategy 產生新 output 時記錄
- 若策略每 4H 跑一次，則每 4H 記錄一筆
- 此紀錄代表「AI 想要的目標倉位」，不代表已下單或已成交

### 7.2 欄位

| 欄位 | 說明 |
| --- | --- |
| `timestamp` | AI output 時間 |
| `mode` | `paper` / `live` |
| `run_id` | Paper / live run id |
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
| `key_risks` | 1–3 項主要風險（至少 1 項）；CSV 可存 JSON array string |
| `order_created` | 本 output 是否產生 rebalance / flip order |
| `no_order_reason` | `maintain_current` / `within_deadband` / `invalid_fail_closed` / 其他；有 order 時留空 |

`maintain_current` 的 requested / approved target、target margin / notional 與 target side 皆為空，`delta_notional = 0`，並記錄 `order_created = false` 與 `no_order_reason = maintain_current`。`flat` 則有明確 target：`target_side = flat`、requested / approved margin = `0`、`target_signed_notional = 0`。

---

## 8. `orders.csv`

紀錄系統產生的 order，包括 rebalance、TWAP 子單、SL / TP 單。

### 8.1 紀錄時機

- order 建立時記錄
- order 狀態變化時記錄，例如部分成交、完全成交、取消、拒絕
- 每次狀態變化先寫入 SQLite order event；CSV 匯出時依既定 schema 呈現，不直接 append CSV 作為正式記錄

Paper trading 中，`active_from` 是 order 可以開始使用即時 market snapshot 模擬成交的最早時間。

### 8.2 欄位

| 欄位 | 說明 |
| --- | --- |
| `timestamp` | order 紀錄時間 |
| `mode` | `paper` / `live` |
| `run_id` | Paper / live run id |
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
| `status` | `pending_market_data` / `open` / `partially_filled` / `filled` / `canceled` / `rejected`；`pending_market_data` 為 paper 專用（見 phase2-execution.md 5.2） |
| `status_reason` | 拒絕或取消原因，例如 RiskGate 拒單、`canceled_restart`；正常狀態留空 |
| `reduce_only` | 是否為 reduce-only |
| `active_from` | paper trading 中 order 最早可成交時間 |

---

## 9. `fills.csv`

紀錄每一筆成交，是更新 position、fee、realized PnL 的主要來源。

### 9.1 紀錄時機

- 每次成交發生時記錄一筆

Paper trading：

- `paper_market` 在 order 生效且取得第一個有效 snapshot 後完整模擬成交
- TWAP 從 `active_from` 後依 30 秒 cadence 為每個已成交 slice 記錄一筆 fill

Live trading：

- 以交易所成交回報 / user fills 為準

### 9.2 欄位

| 欄位 | 說明 |
| --- | --- |
| `timestamp` | 成交時間 |
| `mode` | `paper` / `live` |
| `run_id` | Paper / live run id |
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

## 10. `funding_events.csv`

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

## 11. `account_snapshots.csv`

紀錄每個週期結束後的帳戶狀態，用來追蹤整體績效與風險。

### 11.1 紀錄時機

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

### 11.2 欄位

| 欄位 | 說明 |
| --- | --- |
| `timestamp` | snapshot 時間 |
| `mode` | `paper` / `live` |
| `run_id` | Paper / live run id |
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

## 12. `position_snapshots.csv`

紀錄每個 symbol 的倉位狀態，用來追蹤 entry、PnL、exposure、SL / TP。

### 12.1 紀錄時機

- 每個 cycle 結束時記錄
- 建議與 `account_snapshots.csv` 同步

若想精簡，只記錄：

1. 有持倉的 symbol
2. 當期有 decision / order / fill 的 symbol

### 12.2 欄位

| 欄位 | 說明 |
| --- | --- |
| `timestamp` | snapshot 時間 |
| `mode` | `paper` / `live` |
| `run_id` | Paper / live run id |
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
| `estimated_liquidation_price` | Paper mode 依 phase2-execution.md 6.6.1 求得的估算值；無正數清算點時可為 `null` |
| `exchange_liquidation_price` | Live mode 由 Hyperliquid API 取得；paper 為空 |
| `margin_tier_id` | 本次 maintenance-margin 計算使用的 tier / table id |
| `maintenance_margin_rate` | 本次適用的 maintenance margin rate |
| `maintenance_deduction` | 本次適用的 tier deduction |
| `liquidation_model_version` | Paper liquidation model 版本；live 可留空 |
| `stop_loss_price` | 目前有效 SL 價格 |
| `take_profit_price` | 目前有效 TP 價格 |

---

## 13. 紀錄頻率總結

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

