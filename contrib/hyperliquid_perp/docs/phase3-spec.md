# Phase 3 spec — Live Execution Validation（v3）

> **狀態說明**：Phase 3 的目標是把 Phase 2 的 paper trading 架構延伸到 exchange-connected
> execution。本階段不以 profitability 為主要目標，而是驗證 live execution plumbing、安全邊界、
> reconciliation、protection orders 與 restart recovery 是否可靠。
>
> **版本沿革**：v2 = PDF 修訂版（cloid 兩層、absolute_notional_ceiling、§9.4 execution style
> scope）。**v3 = 本文件**，整合 2026-07-11 逐條確認的 9 項前置決策——最大的變更是
> **native TWAP 改為自管切片 TWAP**（SDK / API 查證事實見 §9.5），並補齊 v2 未定義的
> safety 參數語意、safe mode 解除介面、agent key 佈建與 6-PR build order。

Phase 3 既定範圍：

1. Exchange order placement（真實簽名下單）
2. **自管切片 TWAP live execution**（v3 修訂：原 native TWAP，見 §9.5）
3. User fills / WebSocket reconciliation
4. Live account / position reconciliation
5. Real fee / funding comparison
6. Stop Loss / Take Profit live protection
7. Kill switch / dead man's switch
8. Testnet live / mainnet tiny validation

Phase 3 的成功標準不是獲利，而是：

```
不下錯單
不漏 fill
不重複計帳
不留孤兒單
不裸奔
不在 state mismatch 時繼續開倉
重啟後能安全恢復
交易所狀態與 SQLite 狀態能對帳
```

## 0. Phase 3 Entry Criteria

進入 Phase 3 前，Phase 2 paper trading 至少需滿足：

```
cycle_count >= 30
orphan_fill_count = 0
snapshot_mismatch_count = 0
accounting_replay_mismatch_count = 0
沒有未處理的 exceptions
相同的已記錄 accounting events 能重建出相同的 positions、account state 與 PnL
```

若上述條件未滿足，不應進入 live execution implementation。
（實作對應：`paper/validation.py` 的 `phase3_ready` 判定，實際上還額外要求
`orphan_order_count = 0`，比本節更嚴——以程式為準。）

## 1. Confirmed Decisions

| # | 決策項目 | 決策 |
|---|---|---|
| 1 | Shadow live | 不做 shadow_live，也不做 preflight-only |
| 2 | Phase 3 路線 | testnet_live smoke tests → testnet_live 30 cycles → mainnet_tiny 30 cycles |
| 3 | Mainnet tiny execution | **（v3 修訂）自管切片 TWAP**：每 30 秒送一張帶 0.5% 價格保護的 IOC 限價切片單，每張有 cloid。native TWAP 因 SDK / API 限制列為 future work（事實與理由見 §9.5） |
| 4 | SL failure | SL create / modify 失敗後 retry 3 次；仍失敗則 reduce-only emergency close |
| 5 | TP failure | TP create / modify 失敗不平倉，但進 degraded protection / safe mode |
| 6 | Non-bot-owned orders / positions | 不管理；偵測到即進 manual safe mode |
| 7 | Mainnet tiny cap | leverage = 1、max_target_margin_pct = 60、max_notional_usdc = 100 |
| 8 | Cap validation | 啟動時必須計算 effective_notional_cap |
| 9 | Testnet acceptance | testnet_live >= 30 cycles |
| 10 | Mainnet tiny acceptance | mainnet_tiny >= 30 cycles |
| 11 | Testnet smoke tests | 必須全部通過後才進 testnet live cycles |
| 12 | Kill switch | schedule_cancel_seconds = 120、refresh_interval_seconds = 30、正常 shutdown 不自動平倉 |
| 13 | AI / API failure | 不沿用上一輪 target；本輪不建立新 order |
| 14 | WebSocket failure | disconnected > 5 minutes → safe mode |
| 15 | Safe mode recovery | 分級處理：輕微可自動恢復，嚴重需人工解除（解除介面見 §13.6） |
| 16 | Fee / funding missing | 允許 pending，之後 reconciliation job 回補 |
| 17 | Active TWAP overlap | active slice plan 未 terminal 時，不建立新 entry / rebalance plan |
| 18 | TWAP duration | **（v3 修訂）**切片 plan 總時長 60 minutes、slice 間隔 30 秒（沿用 paper `twap.py` 的切片模型） |
| 19 | TWAP slippage bound | **（v3 修訂）**每張切片單 = IOC 限價，價格保護 max_slippage_pct = 0.5%（相對送單當下 mid），見 §9.2 |
| 20 | Mainnet live | 保留為 future mode，但 Phase 3 第一版不啟用 |
| 21 | Capital scaling | Phase 3 不允許自動放大資金 |
| 22 | Cloid 表示法 | 系統維護 cloid_logical（人類可讀）與 cloid_hex（128-bit hex，送交易所）兩層；bot-owned 判斷用 SQLite lookup，不用 prefix 猜測（見 §8.2、§19.3）。v3 註記：因改用自管切片，**所有** live orders（含切片單）都有 cloid，無 native TWAP 母單例外 |
| 23 | Absolute notional ceiling | mainnet_tiny 階段 absolute_notional_ceiling = 500 USDC，config-load-time 檢查 max_notional_usdc 不得超過此值（見 §5 規則 5） |
| 24 | **（v3 新增）Agent key 佈建** | 分網路兩個環境變數 `HYPERLIQUID_AGENT_KEY_TESTNET` / `HYPERLIQUID_AGENT_KEY_MAINNET`；啟動時由 key 推導地址、以 Info API 驗證已被 wallet_address 授權且未過期，失敗拒絕啟動（見 §6） |
| 25 | **（v3 新增）Safety 參數語意** | max_daily_loss_pct：UTC 日切、含未實現，觸發進 recoverable safe mode 至次日；max_consecutive_loss_count：倉位歸零結算計次、達 3 進 manual safe mode；max_open_orders：達上限拒新單（見 §10.3–§10.5） |
| 26 | **（v3 新增）架構原則** | live 執行引擎為平行 `live/` 套件（paper engine 零改動）；WebSocket 事件經 thread-safe queue 由既有 tick 迴圈消化（PR 5 實作：live loop 為 10s tick，見 §11.4）；live 帳務以記錄的交易所事件為單一基準（見 §2.1、§11.4、§15） |
| 27 | **（v3 新增）Build order** | 6 個小 PR（見 §23） |

## 2. Phase 3 Goal

Phase 3 的核心目標是建立安全的 live execution 能力。

本階段主要包含：

1. Hyperliquid signed exchange client
2. Live order submission / cancel / status tracking
3. 自管切片 TWAP order execution（v3 修訂）
4. Client order id（cloid）與 idempotent retry
5. WebSocket user fills ingestion
6. Live fills deduplication
7. Live position / account reconciliation
8. Live Stop Loss / Take Profit protection
9. Kill switch / dead man's switch
10. Startup / restart recovery
11. Testnet live / mainnet tiny-capital 驗收

Phase 3 必須延續 Phase 2 的原則：

```
SQLite   = 本地 audit trail / runtime state / replay / export source of truth
Exchange = live orders / fills / positions / account state 的事實來源
```

若本地 state 與交易所 state 不一致，系統不得繼續開新倉，必須先 reconcile 或進入 safe mode。

### 2.1 架構原則（v3 新增）

1. Live 執行引擎放在 `contrib/hyperliquid_perp/live/`，與 `paper/` **平行**；
   已通過 Phase 2 驗收的 paper engine 零改動。
2. 兩邊共用：scheduler（4h 決策循環）、RiskGate、persistence、以及純函式模組
   （`paper/twap.py` 的切片數學、`paper/stops.py` 的 SL/TP 價格數學——皆無 I/O，直接 import）。
3. Live 特有的 reconciliation、safe mode 狀態機、protection order 生命週期、
   exchange client 都只存在於 `live/` 與 `exchanges/hyperliquid/`。

## 3. Execution Modes

Phase 3 引入以下模式：

```yaml
mode:
  paper         # Phase 2：本地模擬成交，不送單
  testnet_live  # Phase 3 第一個真下單模式
  mainnet_tiny  # mainnet 小本金驗證
  mainnet_live  # 正式 live；Phase 3 第一版不啟用
```

### 3.1 Mode Semantics

| Mode | Market data | Orders | Fills | Account / position |
|---|---|---|---|---|
| paper | public market data | local simulated orders | local simulated fills | local simulated account |
| testnet_live | testnet exchange data | real testnet orders | exchange fills | exchange account |
| mainnet_tiny | mainnet exchange data | real mainnet orders | exchange fills | exchange account |
| mainnet_live | mainnet exchange data | real mainnet orders | exchange fills | exchange account |

`network = mainnet` 不代表可以下單。是否允許送單必須由 `allow_real_orders` 明確控制。

## 4. Config Gates

Phase 3 必須明確區分「讀取交易所資料」與「允許真實下單」。

```yaml
live:
  mode: testnet_live      # 必填（PR 1 修訂）：無預設值
  network: testnet        # 必填（PR 1 修訂）：無預設值；依 mode 釘死（§3.1）
  allow_real_orders: false
  allow_manage_external_orders: false

  order_owner_prefix: "hta"     # 僅用於 cloid_logical 的可讀前綴，不用於 bot-owned 判定
  require_agent_wallet: true

  safety:
    single_symbol_only: true
    allowed_symbols: [BTC]
    leverage: 1
    margin_mode: cross
    max_target_margin_pct: 60
    max_notional_usdc: 100
    absolute_notional_ceiling: 500   # 見 §5 規則 5
    max_open_orders: 5               # 行為定義見 §10.5
    max_daily_loss_pct: 2            # 行為定義見 §10.3
    max_consecutive_loss_count: 3    # 行為定義見 §10.4

  execution:
    default_style: sliced_twap       # v3 修訂；僅適用於 entry / rebalance，見 §9.4
    max_slippage_pct: 0.005
    plan_duration_minutes: 60
    slice_interval_seconds: 30

  websocket:
    disconnect_safe_mode_after_seconds: 300

  protection:
    sl_repair_max_attempts: 3
    sl_repair_retry_delay_seconds: 5
    tp_failure_mode: degraded_protection

  kill_switch:
    enabled: true
    schedule_cancel_seconds: 120
    refresh_interval_seconds: 30
    on_refresh_failed: safe_mode
    on_shutdown: cancel_bot_owned_open_orders
    emergency_close_on_shutdown: false
```

### 4.1 Real Order Gate

系統只有在以下條件全部成立時，才允許**建立新的交易目標**（new target）：

```
allow_real_orders = true                             # wire-scoped
mode in {testnet_live, mainnet_tiny, mainnet_live}   # wire-scoped
agent key exists（且通過 §6 的啟動授權驗證）          # wire-scoped
startup reconciliation passed                        # wire-scoped
kill switch active                                   # wire-scoped
symbol is allowed                                    # wire-scoped
risk gate approved target                            # DECISION-scoped
current account / position state is reconciled       # SAFE-MODE-scoped
no unresolved protection failure                     # DECISION-scoped
no active slice plan                                 # DECISION-scoped
no manual_safe_mode                                  # SAFE-MODE-scoped
```

**兩種粒度（v7 修訂，2026-07-13）**：標記 DECISION-scoped 的三條，語意是「這個決策
cycle 可不可以開新目標」，**不是**「這一張單可不可以上線」。把它們套用在每一張單上
會自相矛盾——§9.3 明文規定 active slice plan 期間「不建立新的 entry / rebalance
plan」但「允許 SL repair、允許 emergency close」，而一個 plan 會跨數十個 decision
cycle 執行約一小時。若每張單都查這三條，引擎一旦設下 `active_slice_plan`，就會擋掉
**自己這個 plan 的每一張切片**，以及 §9.3 保證允許的 SL repair 與 emergency close
——正是 plan 執行期間最需要送出的那一張。引擎屆時只能「不設旗標」（條件淪為裝飾）或
繞過 submitter（繞過 gate），兩條都是錯的。同理 `risk_gate_approved`（per-cycle 的
核准無法涵蓋一小時的切片）與 `unresolved_protection_failure`（要修復它的 SL repair
本身必須送得出去）。

**SAFE-MODE-scoped 兩條（PR 5 review-loop 修訂，2026-07-20）**：`state_reconciled`
與 `manual_safe_mode` 對「加風險的單」是 wire 級的硬條件，但**保護／去風險單**
（`stop_loss` / `take_profit` / `emergency_close`，即 `PROTECTIVE_ORDER_ROLES`）
必須豁免：任何 safe mode 進場都會清掉 `state_reconciled`（§13），而 §13.1 明定
safe mode 期間「持續保護」、§17.2 的急平單正是 safe mode 下最需要送出的那一張。
若不豁免，§12.3 `position_sl_missing` 進 recoverable safe mode 後，修復它的 SL
place/modify 與急平單全被 gate 擋死，而能重升 `state_reconciled` 的乾淨
reconciliation pass 又要求場上已有有效 SL——死鎖，倉位裸奔到人工介入。豁免僅限這
兩條：基礎 wire 前置條件與 kill switch（dead man's switch）對保護單**仍然生效**。

因此 gate 提供三個入口，而條件表只有一份（`RealOrderGate._first_failed`，依上表順序
求值，回報第一條失敗的條件，入口之間不會漂移）：

- `check_new_target(symbol)` / `require_new_target` — **完整** §4.1 列表。PR 5 引擎在
  每個 decision cycle 建立 plan **之前**問一次。
- `check_order(symbol)` / `require_order` — 上表扣除 DECISION-scoped 三條。每一張
  **加風險**的單（entry / rebalance 切片）都必須通過（`LiveOrderSubmitter.
  submit_ioc_limit` 送出前查一次，signed client 綁定的 gate 在 wire 再查一次當
  backstop）。
- `check_protective_order(symbol)` / `require_protective_order` — 再扣除
  SAFE-MODE-scoped 兩條。保護／去風險單（SL / TP trigger、§17.2 急平 IOC）走這個
  入口；submitter 依 `order_role` 自動選路，trigger-order 方法固定走此入口。

若任一條件不成立，系統必須拒絕建立 live order，並記錄：

```
order_created = false
no_order_reason = live_order_gate_rejected
```

## 5. Mainnet Tiny Risk Cap

mainnet_tiny 使用：

```yaml
mainnet_tiny:
  leverage: 1
  max_target_margin_pct: 60
  max_notional_usdc: 100
  absolute_notional_ceiling: 500   # config-load-time 上限，見規則 5
  single_symbol_only: true
```

實際允許的名目倉位上限為：

```
pct_cap_notional       = account_equity × max_target_margin_pct / 100 × leverage
effective_notional_cap = min(pct_cap_notional, max_notional_usdc)
```

以預設值為例：

```
threshold_equity = max_notional_usdc / (max_target_margin_pct / 100 × leverage)
                 = 100 / 0.6
                 ≈ 166.67 USDC
```

規則：

1. 若 account_equity < 166.67 USDC，實際上限會低於 100 USDC。
2. 若 account_equity >= 166.67 USDC，實際上限會被 max_notional_usdc = 100 卡住。
3. 系統啟動時必須記錄 pct_cap_notional 與 effective_notional_cap。
4. 若 effective_notional_cap 低於交易所最小下單名目價值，啟動應 fail 或進入 safe mode。
5. 啟動時必須驗證 `max_notional_usdc` 本身是否落在合理範圍：

   ```
   config-load-time 檢查：
     max_notional_usdc <= absolute_notional_ceiling

   mainnet_tiny 階段：absolute_notional_ceiling = 500 USDC

   違反時：啟動 fail，不得自動 clamp 或忽略。
   ```

   另外 `max_notional_usdc < 10 USDC`（交易所最小下單額）在 config load
   直接具名拒絕（PR 1 修訂）：effective_notional_cap 恆 ≤ max_notional_usdc，
   低於最小下單額代表任何 equity 都無法下單，不必等到讀完帳戶才發現。
   （此規則檢查的對象是設定值 max_notional_usdc 本身，而不是計算出的
   effective_notional_cap——後者由規則 3 的公式決定，數學上不可能超過
   max_notional_usdc，不需要二次上限檢查。）

6. Phase 3 不允許系統自動調高 max_target_margin_pct 或 max_notional_usdc。

## 6. Secrets and Agent Wallet

Phase 3 需要 Hyperliquid Agent/API wallet private key。

**（v3 修訂）**環境變數分網路：

```
HYPERLIQUID_AGENT_KEY_TESTNET   # testnet agent wallet private key
HYPERLIQUID_AGENT_KEY_MAINNET   # mainnet agent wallet private key
```

程式依 `live.network` 自動選用對應變數；兩把 key 可同時存在 `.env`，
不會有「換網路忘了換 key」的人為風險。

規則：

1. Private key 只能從 env var 讀取。
2. Private key 不得出現在 yaml、SQLite、CSV、log、prompt 或 raw payload。
3. `.env` 與 `*.local.yaml` 必須 gitignored。
4. Main wallet private key 永遠不得提供給本系統。
5. Agent wallet 只能交易，不能提款。
6. 若對應網路的 agent key 缺失，系統不得以 allow_real_orders = true 運行：
   config 設了 `allow_real_orders: true` 而 key 缺失時，啟動必須以具名錯誤
   拒絕（與 paper + allow_real_orders 的矛盾同等對待，不做靜默降級）；
   keyless 的 gate 檢查一律以 `allow_real_orders: false` 明示。
   （PR 1 修訂：由字面「強制視為 false」改為硬失敗——設了 true 卻沒 key
   幾乎必是操作錯誤，應該大聲失敗而不是跑一個永遠下不了單的迴圈。）
7. **（PR 1 修訂）真單是雙旗宣告**：`allow_real_orders: true` 必須搭配
   `require_agent_wallet: true`，否則 config 建構即具名失敗——真單開關
   不得取決於環境變數是否恰好存在，而是兩個旗標的明確宣告（規則 6 的
   缺 key 拒絕因此一律經由 require_agent_wallet 檢查觸發）。

### 6.1 啟動授權驗證（v3 新增）

啟動時（arm kill switch 之前）必須執行：

1. 由 agent private key 推導出 agent address。
2. 以唯讀 Info API（`extra_agents`）查詢 `wallet_address` 的已授權 agent 清單。
3. agent address 必須在清單中且未過期（agent 授權有效期最長約 180 天）。
   同一 address 在清單中出現多次（例如 re-approve 留下 stale 條目）視為
   歧義，具名拒絕——不猜哪個 validUntil 是有效的（PR 1 修訂）。
4. 任一步失敗 → 具名錯誤、**拒絕啟動**（不進 safe mode，因為還沒開始跑）。
5. 剩餘效期低於 7 天時印出警告但放行（PR 1 修訂）：長駐迴圈跑到一半授權
   過期，會變成 real orders 開著時的簽名中途失敗——啟動時就該提醒續期；
   合法的短效授權仍不得被拒。

此驗證可一次抓到三種錯誤：拿錯網路的 key、key 授權給別的帳戶、授權過期。

## 7. Exchange Actions

Phase 3 會使用 Hyperliquid signed exchange endpoint，**全部透過官方
`hyperliquid-python-sdk` 的 `Exchange` class 現成方法**，不自製簽名：

| Action | SDK 方法 | 用途 |
|---|---|---|
| order | `Exchange.order` / `bulk_orders` | 下單：entry / rebalance 切片、close、SL、TP |
| cancel | `Exchange.cancel` | 依 exchange order id 取消 |
| cancelByCloid | `Exchange.cancel_by_cloid` | 依 client order id 取消 |
| modify | `Exchange.modify_order` / `bulk_modify_orders_new` | SL / TP modify-before-cancel（§17.4） |
| scheduleCancel | `Exchange.schedule_cancel` | dead man's switch |
| updateLeverage | `Exchange.update_leverage` | 開倉前確認 leverage 設定 |
| orderStatus | `Info.query_order_by_oid` / `query_order_by_cloid` | 查詢 order 狀態與 reconciliation |

Phase 3 第一版不支援：

| Feature | 狀態 |
|---|---|
| **native TWAP（twapOrder / twapCancel）** | **deferred（v3 修訂，理由見 §9.5）** |
| shadow live | removed |
| preflight-only | removed |
| multi-symbol portfolio execution | deferred |
| leverage > 1 | deferred |
| isolated margin | deferred |
| automatic management of non-bot-owned orders | deferred |
| complex grid / scale orders | deferred |
| confidence-aware sizing | deferred |
| autonomous capital scaling | out of scope |

## 8. Order Contract

### 8.1 Order Roles

所有 live orders 必須標記 `order_role`：

```
entry
rebalance
close
stop_loss
take_profit
emergency_close
cleanup_cancel
```

（Phase 2 已有 entry / rebalance / stop_loss / take_profit；close /
emergency_close / cleanup_cancel 為 live 新增詞彙。）

### 8.2 Client Order ID

系統維護兩層 id：

```
cloid_logical  — 人類可讀，用於內部追蹤、log、SQLite 查詢
cloid_hex      — 送往 Hyperliquid API 的實際欄位，128-bit hex
```

cloid_hex 由 cloid_logical 決定性推導（SHA-256 取前 16 bytes，格式化為
`0x` + 32 hex characters），同一個 cloid_logical 永遠映射到同一個 cloid_hex。

建議 cloid_logical 格式：

```
<prefix>_<run_id>_<symbol>_<output_id>_<plan_id>_<leg>_<slice_index>_<order_role>
```

範例：

```
cloid_logical = hta_live_20260708_BTC_out123_plan456_open_000_entry
cloid_hex     = 0xa1b2c3d4e5f60718293a4b5c6d7e8f90   # 16-byte hex，實際送出的欄位
```

v3 註記：因採自管切片（§9），**每一張**送到交易所的單（含切片單）都是一般
order、都有自己的 cloid——不存在 v2 native TWAP 母單「cloid_hex, if supported」
的例外情況。

### 8.3 Cloid Rules

1. 同一個 logical order retry 時，必須重用同一個 cloid_logical（因此也重用同一個 cloid_hex）。
2. 若 retry 時交易所回報 duplicate / already exists，系統不得直接重送新單。
3. Duplicate 時應先用 cloid_hex 查詢 orderStatus 或 open orders。
4. 若該 cloid_hex 對應的 order 已存在，應補寫或更新 SQLite order record
   （含 cloid_logical / cloid_hex 對照）。
5. 若該 cloid_hex 不存在，且確認前一次送單未成功，才允許重新送出。
6. 不得用不同 cloid 重複提交同一個 logical order。
7. 所有對交易所的查詢（orderStatus、cancelByCloid 等）一律使用 cloid_hex；
   cloid_logical 只在本地系統內部使用，不送給交易所。
8. Retry 同一個 logical order 時，參數（side / size / price / reduce_only）必須與
   前次送出完全一致（byte-identical）：恢復路徑（rule 4 的補寫）以呼叫方參數回填
   本地 order record、不與交易所回報逐欄比對，引擎不得在 retry 時用新價重算 qty——
   同一 cloid_logical ⇔ 同一組參數；要改參數就開新 logical order（v4 新增，2026-07-12）。
9. 任何 error ack 在判定 rejected 之前，必須先以 cloid_hex 查詢 orderStatus：
   duplicate 錯誤文案比對只是 fast-path（rule 2 的觸發器），orderStatus 才是
   rejected-vs-exists 的權威——rejected 判定授權呼叫方開新 logical order，若實際上
   訂單存在（文案改字造成 duplicate 漏判）就是雙倉（v5 新增，2026-07-13）。
10. rule 5 的「cloid_hex 不存在」以 orderStatus 回 unknownOid 為準——但若本地
    live_order_attempts 已有該 cloid 的 **acknowledged 或 duplicate** place attempt
    （交易所確定收過），unknownOid 是矛盾（retention 過期／Info 不一致），必須具名
    報錯拒絕重送，不得讀成「前次未成功」（v5 新增 2026-07-13；v7 補上 duplicate，
    2026-07-13）。
    「交易所確定收過」的證據**有兩處，兩處都要查**，統一由 `has_exchange_known_cloid()`
    判定（v7 擴充；原 `has_exchange_known_place_attempt` 只查第 1 處）：
    1. **attempt 列**：place attempt 落在 `repository.EXCHANGE_KNOWN_ATTEMPT_STATUSES`
       = {acknowledged, duplicate}（`action='place'` 過濾寫在 helper 內部：
       acknowledged 的 **cancel** attempt 只證明交易所收過 cancel、不證明收過 place）。
       duplicate 的證據力不低於 acknowledged——那是交易所自己說「這個 cloid 已存在」。
       只讀 acknowledged 會讓「timeout(failed) → 重試撞 duplicate → 再重試」這條路徑把
       unknownOid 讀成「確認不存在」而重送。
    2. **orders 列的 `exchange_order_id` 非 NULL**：交易所給過我們這個 cloid 的 oid。
       這是「成功的 §8.3 恢復」唯一留下的痕跡——恢復刻意**不回補 attempt 列**（orders
       列才是 PR4 對帳權威），而 pre-check 路徑的恢復根本不寫 attempt 列。少了這一臂，
       「timeout(attempt=failed) → 重試經 orderStatus 恢復到仍掛著的原單 → 更晚一次重試
       遇到 unknownOid」會被讀成「交易所從未收過」而**重送一張還活著的單**（出場檢查以
       真實類別重現，實際送出兩次）。只有交易所給的 oid 會寫進這欄（accepted ack 與
       orderStatus 恢復；rejected ack 刻意不寫，OrderAck 也禁止 error 狀態帶 oid），
       所以它是精確的收據，不會誤擋 rule 5 對「交易所真的沒收過」的合法重送。
    狀態集合有 import 期 partition guard：日後新增 attempt status 必須被明確歸類，
    不得預設落進安全的那一半。
11. 頂層 `{"status":"err"}` envelope（壞簽名、壞 payload、invalid nonce 等 action
    層失敗）不是 per-order error ack：訂單可能根本沒進撮合引擎，transport 層具名
    raise（`ExchangeRequestError`），attempt 記 'failed'（outcome unknown），依
    rule 1 同 cloid retry、由 pre-check 的 orderStatus 解決——transient 失敗不得
    消耗 cloid、不得在審計留下永久 rejected；rule 9 的「error ack」只指 `statuses`
    內的 per-order error（v6 新增，2026-07-13）。
12. **exchange status 一律只查 exact 表，表外的字一律不猜**（v7 新增，2026-07-13）：
    exchange status → local status 的對照，**唯一權威是「完整文件化詞彙 exact 表」**
    （`_EXCHANGE_TO_LOCAL_STATUS`，已涵蓋 Hyperliquid 文件列出的全部 30 個字）。
    表外的字＝交易所在這張表寫完之後新增的字；對這種字**任何方向的猜測都不安全**，
    所以一個都不猜——一律記為 `open` + warning，交給 PR 4 reconciliation 對帳：
    - 猜成 `rejected` 是最貴的：它會變成 `SubmitOutcomeKind.REJECTED`，依 rule 9
      **授權呼叫方開一張新的 logical order**。一個字面含 "reject" 但訂單其實還掛在
      場上的新狀態，就會替一個活著的部位再鑄一個 cloid（雙倉）。
    - 猜成**終態**（`canceled` / `filled`）則會靜默放棄一張可能還活著的單：像
      `cancelRequested`（取消在途、單還在場上）會被記成 canceled，之後真的成交時，
      fill 會落在一張本地已 canceled 的單上。
    兩者與 rule 10 的 resend guard 是同一個 bug class（用不完整的 status 判斷驅動
    安全判決）。exact 表既然已涵蓋全部文件化詞彙，heuristic 對「真正重要的字」毫無
    貢獻，只會在「最不該猜」的地方開火——因此 substring fallback 已整個移除（v6 曾
    保留 reject/cancel/filled 三臂，v7 全數刪除）。記為 `open` 是唯一保守的讀法：
    高估「還活著」可回復（單會繼續被看管、對帳會關掉它），高估「被拒絕」會鑄新單，
    高估「已結束」會丟掉活單。

## 9. Sliced TWAP Execution（v3 全章改寫）

Phase 3 的正常 entry / rebalance path 使用**自管切片 TWAP**：由 live 引擎
按 30 秒節奏送出一系列帶價格保護的 IOC 限價單。

```yaml
execution:
  default_style: sliced_twap
  plan_duration_minutes: 60
  slice_interval_seconds: 30
  max_slippage_pct: 0.005
```

### 9.1 切片模型

沿用 Phase 2 paper `twap.py` 的純函式切片數學（兩邊 import 同一份程式）：

1. `total_qty` 按 quantity step 向下取整；不足一張合法最小單 → plan `rejected`（residual）。
2. 合法切片數 = `min(floor(total_qty / min_order_qty), floor(duration / slice_interval), 120)`；
   `min_order_qty` 由交易所最小下單名目價值與當下 mid 推得。
3. 切片配量用整數 step allocation，總和恰等於 `total_qty`，
   捨去的零頭記為 `rounding_residual_qty`。
4. 1 張切片 → 單張立即單；2 張以上 → **首片於 t=0 立即送出**，其後每
   `slice_interval_seconds` 送一張（PR 1 定案：任何合法組合都至少送出一
   片；config 層仍拒絕 interval > duration——那幾乎必是秒/分單位打錯）。
   `plan_duration / slice_interval` 是**最大攤開範圍（envelope），不是承
   諾片數**：規則 2 的 `floor(total_qty / min_order_qty)` 上界讓每片自動
   ≥ 交易所最小下單額——最小 clip 額優先於 interval，片數不足時提前完成
   （PR 1 定案；mainnet_tiny 的 100 USDC cap 配預設 120 片即為此情形）。
5. Flip(反向)沿用 Phase 2 的 sequential 兩腿模型：close leg 完成且倉位歸零後，
   重跑 RiskGate 才開 open leg；兩腿共用同一個 plan envelope（`plan_duration_minutes`，
   預設 1 小時）與切片預算——預算下限為 2（每腿至少 1 片；PR 5 r10 修訂，2026-07-21）。

### 9.2 Slice Slippage Bound

每張切片單都是 **IOC 限價單**，送出前根據當下 mid_price 計算保護價格：

```
buy  limit = mid_price × (1 + max_slippage_pct)
sell limit = mid_price × (1 - max_slippage_pct)
```

預設：

```
max_slippage_pct = 0.5%
buy  limit = mid_price × 1.005
sell limit = mid_price × 0.995
```

規則：

1. 不得送出無價格保護的 market-like live order（`Exchange.market_open` 等
   convenience 方法一律不用；統一走帶限價的 `order`）。
2. IOC 未成交（或部分成交）的量**不重送同一張**：計入 plan 的落後量，
   由後續切片自然吸收；plan 到期仍未吃完 → terminal `expired`，殘量記
   `residual_qty`（paper 為真值；**live v1 寫 NULL＝unknown**——fill→plan
   歸屬隨 PR 6 WS/fill 路由，見 §16.1 與 phase2-data §6.1）。
3. 每張切片單有獨立 cloid（§8.2），retry（網路錯誤等）重用同一 cloid。
4. **每 tick 最多送一張切片；pre-send gate 拒絕不吃進度（PR 5 修訂，2026-07-21）**：
   30 秒 interval 是排程節奏，停滯後的 catch-up 也是一 tick 一張，不得在同一個
   價位 burst 整批 backlog。§4.1 wire gate 在送出**前**拒絕（safe mode /
   kill switch）時，該切片**從未上線**，rule 2 的「不重送」不適用——cursor 停在
   原地（事件 `slices_paused`），gate 重開後從同一張續送；反之，凡已觸及（或可能
   觸及）wire 的結果——ack、交易所拒絕、ambiguous 送單失敗（§8.3 冪等層以同
   cloid 查證定讞）——cursor 一律前進、不重送。plan deadline 照常束縛：被 gate
   擋到期的 plan 誠實 terminal `expired`（殘量同 rule 2——live v1 記 NULL），
   不得記成 completed。**deadline 是硬信封（PR 5 修訂，2026-07-22，使用者拍板）**：到期
   當下那個 tick（含斷線／safe-mode 暫停跨越 deadline 後恢復的第一個 tick）
   **先判到期、不送單**——切片的 size 與方向來自一個已超出整個 plan envelope 的
   決策，不得再上鏈；flip 預算收斂到 2 張時，那最後一張可能是半個倉位。同一原則涵蓋 **pre-wire 本地 store 故障（PR 5 修訂，2026-07-21）**：
   submitter 在 network call 之前的 §8.3 pre-check 讀取或 intent transaction 因
   transient SQLite 錯誤失敗（`LiveOrderPreSubmitError`）時，什麼都沒送、也沒留
   任何 evidence row——cursor 同樣停在原地（事件 `slice_held`）、下一 tick 以同
   cloid 重試；否則持續的本地 store 故障會讓整個 plan 一單未發卻記成 completed。
   §8.3 協議判決（refusing-to-resend 等——該 cloid 先前**已**上過線）與 contract
   violation（provenance／coherence）不在此列：保留原型別、照舊前進或 fail loud。
5. **執行參數來源（PR 5 修訂，2026-07-21）**：plan envelope 與切片節奏取自
   `live.execution.plan_duration_minutes` / `slice_interval_seconds`（預設 60 分
   / 30 秒；full-grid 切片預算 = duration/interval，上限 120）——config 驗證既有，
   引擎實際消費，不得驗證了卻靜默忽略（§18.1 原則）。

### 9.3 Active Plan Overlap

若存在 active slice plan 且尚未 terminal：

```
不建立新的 entry / rebalance plan
繼續 fill monitoring
繼續 reconciliation
允許 SL repair
允許 emergency close
新 AI output 可記錄，但不送新單
舊 plan terminal + reconciliation passed 後，下一輪才可正常交易
```

### 9.4 Execution Style Scope

`default_style: sliced_twap` 僅適用於 entry / rebalance 這兩種 order_role。

以下 order_role 一律使用立即成交（aggressive IOC，單張全量、仍帶價格保護），
不做切片：

```
close
stop_loss
take_profit
emergency_close
cleanup_cancel
```

理由：切片的設計目的是降低大單進出的市場衝擊，這與「立即降低風險」的目標
衝突。emergency_close 若攤成 60 分鐘，會讓系統在判定需要緊急平倉後，
仍暴露在市場風險下長達一小時。

> **v1 註記（2026-07-22 拍板）**：AI 決策的平倉目標（flat target）**不是**本節的
> `close` role——它走 reduce-only rebalance-to-zero 路徑（切片 TWAP，與 paper
> 同基準、平倉期間仍有 SL 保護），摩擦最低；殘量到期誠實 `expired`。`close`
> role 在 v1 沒有任何發射路徑，保留給未來的保護性平倉情境。本節的立即成交
> 規則實際生效的是 stop_loss / take_profit（trigger 單火線）與
> emergency_close（單張 aggressive IOC）。

### 9.5 為什麼不是 native TWAP（v3 決策依據，2026-07-11 查證）

v2 原定 native TWAP，v3 改為自管切片。查證到的事實：

1. **SDK 不支援**：`hyperliquid-python-sdk` 0.22.0 至最新 0.24.0 的 `Exchange`
   class 均無 `twapOrder` / `twapCancel`；只有唯讀的 `Info.user_twap_slice_fills`。
   要用 native TWAP 必須自製簽名 action。
2. **API 無價格保護**：官方 `twapOrder` payload 僅 `{a, b, s, r, m, t}`
   （asset / isBuy / size / reduceOnly / minutes / randomize），無 limit price
   參數；子單滑價為交易所固定的 **3%**（每 30 秒一子單、落後時子單放大至
   3 倍追進度）。v2 §9.2 要求的 0.5% bound 在 native TWAP 上做不到。
3. **無 cloid**：twapOrder 不支援 cloid，只回 `twapId`——§8.3 的冪等重試
   規則對母單無法適用，bot-owned 判定也要另建 twapId 對照。
4. **小額行為存疑**：mainnet_tiny 名目上限 100 USDC 攤 120 子單，每張遠低於
   交易所最小下單額。
5. **crash 殘留**：native TWAP 在交易所端獨立執行，process crash 後仍會
   繼續建倉，而本地已無人維護 SL——自管切片則跟著 process 一起停。

自管切片的代價（已接受）：切片程式碼自己維護（已有 paper 版）、訂單數較多
（每小時最多 120 張）。native TWAP 列為 future work，若未來單筆名目放大到
市場衝擊成為主要成本時再重新評估。

## 10. Risk Gate Extension

Phase 3 沿用 Phase 2 RiskGate，但 live mode 需要額外限制。

### 10.1 Live Risk Checks

Live order 建立前必須通過：

```
requested_target_margin_pct is valid
approved_target_margin_pct <= mode max_target_margin_pct
approved_notional <= max_notional_usdc, if configured
effective_leverage <= configured leverage cap
available margin is sufficient
symbol is allowed
no unresolved reconciliation mismatch
no active kill-switch failure
position is protected, if existing position != 0
no active slice plan
daily loss cap not breached（§10.3）
consecutive loss cap not breached（§10.4）
open order count below max_open_orders（§10.5）
```

**Live-only 拒絕必須留痕（PR 5 修訂，2026-07-22，使用者拍板）**：RiskGate 已核准
（ai_outputs 記 `order_created=1`）而被 live 檢查擋下的三類拒絕——§10.1 notional
cap、§10.5 open-order cap、§4.1 gate line——各插一筆 `rejected` execution_plans
row（`status_reason` 帶拒絕原因、以 `output_id` 連回決策、數量欄 NULL）＋一行
warning log；row 與 `no_legal_slice` 對稱（該路徑只留 row、無 warning——warning
是 live 拒絕路徑自己的）；§10.1 觸發代表 gate sizing 超過 live
上限，是最需要留痕的事件。flip open-leg 的每-tick 重試路徑不插 row（已有
`flip_open_pending` 事件逐 tick 可見，避免 row 洪水）。

### 10.2 AI / API Failure

若本輪 AI / market API / account API 失敗：

```
不得沿用上一輪 target
不得建立新 order
維持現有倉位與既有 SL / TP protection
下一輪照原 schedule 再試
```

正式規則：

```
Fail closed: stale AI output must not create live orders.
```

v1 live 補充（PR 5，2026-07-21）：live **不複製** paper（phase2-spec §3.1）的
within-cycle 三段 retry ladder——retryable 失敗當場記 `api_failed` fail-closed，
下一個 4h cycle 再試。process 於 cycle 途中中斷時，重啟由 loop 啟動先領養
stranded 的 `in_progress` attempt：已存有 raw response → 從 gate 續跑（絕不重問
AI）；AI 從未回應 → 記 `api_failed` 收場並 re-anchor 到下一個 cycle。plan 已
註冊後的 audit persist 失敗只重試 persist、絕不重跑 gate。決策已被 poll 收下、
但 §3.1 raw-response store 失敗時同樣只重試 store（2026-07-23）：poll 是
one-shot，此刻 fail-closed 會把已付費的決策作廢——store 落地前不得 gate，下一
pump 只補寫入，例外上拋 tick guard（recoverable safe mode）；shutdown salvage
對這個形狀同樣補一次 store。

### 10.3 Daily Loss Cap（v3 新增行為定義）

`max_daily_loss_pct = 2` 的衡量方式：

1. 每日 UTC 00:00 記錄當日起始 `account_equity`（取交易所 reconciled 值）。
2. 任一時點 `(day_start_equity - current_equity) / day_start_equity > 2%`
   （**含未實現盈虧**）即觸發。
3. 觸發 → 進 **recoverable safe mode**，持倉與 SL/TP 保護照舊，
   停止新 entry / rebalance；次日 UTC 00:00 自動解除（仍須通過 §13.4 恢復條件）。
4. 觸發事件記入 `safe_mode_events`。
5. **v1 基準來源（PR 5，2026-07-21，使用者拍板）**：day-roll 當下的 baseline 直接
   取交易所 clearinghouse `accountValue`（一日一次 REST；讀取失敗 fallback 本地
   reconciled 值並記 log——roll 不得卡 tick）；盤中 `current_equity` 用本地帳本
   （wallet_balance＋本地 entry 基準的未實現），偏差由 §12 的 equity 容差比對 bound。

### 10.4 Consecutive Loss Cap（v3 新增行為定義）

`max_consecutive_loss_count = 3` 的計次定義：

1. 「一次 loss」= 一段持倉**完全平掉**（倉位歸零：正常 close、flip 的 close leg、
   SL 觸發皆算）時，該段的 realized PnL（含 fee / funding 分攤）< 0。
2. 任一段結算為獲利 → 計數歸零。
3. 連續達 3 次 → 進 **manual safe mode**（連虧暗示策略性問題，
   須人工確認後以 §13.6 介面解除）。
4. 計次 anchor 為 `scheduler_state.last_settlement_wallet_balance`（schema v7，
   PR 5 修訂，2026-07-21）：每次 settlement 以 wallet balance 對前一 anchor 的
   差額為該段 realized PnL（wallet 已折入 fee / funding）。停機期間整段平倉
   （如 SL 於 offline 成交、由 startup recovery 補帳）者，loop 啟動時以
   「已 flat 且 wallet 偏離 anchor」補記一次 settlement（`settle_offline_flat`），
   不得把該段輸贏靜默併入下一段。
5. **已知量測窗口（接受並記錄，2026-07-21，使用者拍板）**：若收尾 fill 的 fee 落在
   pending-fee lane（fee 缺席／非 USDC，暫記 0 等 backfill 修正），該段以 fee-light
   的 wallet 計分——接近打平的段可能記為非虧（歸零計數），事後 fee 修正不回溯重計，
   誤差流入下一段 delta。量級為單筆 fee、僅於 pending lane 觸發，回溯改計數器的
   複雜度不成比例。

### 10.5 Max Open Orders（v3 新增行為定義）

1. 建立任何新 live order 前，先計數交易所端 bot-owned open orders
   （含 resting SL / TP trigger orders）。
2. 達 `max_open_orders = 5` → 拒絕建立新單、記錄
   `no_order_reason = max_open_orders`，並觸發一次 reconciliation
   （正常單一 symbol 運行不應接近此上限，接近即是異常訊號）。
3. **v1 計數點縮限（PR 5，2026-07-21，使用者拍板）**：計數僅在 plan admission
   （`start_plan`）執行一次。slice IOC 從不 resting、不占 open-order 名額；SL/TP
   是 protective——若掛單前也過 count 檢查，計數讀取失敗的 fail-closed（視為
   at-cap）會擋住停損掛單本身，與 §4.1 protective 豁免哲學矛盾。單一 symbol 下
   實際 resting 上限 ≈ SL＋TP 兩張，plan-admission 檢查已覆蓋風險。

## 11. WebSocket Streams

Phase 3 需要 WebSocket 長連線，用於接收即時 exchange events。

### 11.1 Required Streams

至少需要：

```
userFills
userAccount / clearinghouse state equivalent
open orders updates（orderUpdates）
```

### 11.2 WebSocket Rules

1. WebSocket 是即時事件來源，但不是唯一事實來源。
2. REST polling 必須作為補漏、重啟與 reconciliation 來源。
3. WebSocket event 寫入 SQLite 前必須去重。
4. WebSocket 斷線後必須重連。
5. 啟動與每次重連後都必須用 REST 補查可能錯過的 fills / orders / positions
   （`needs_backfill` 旗標）。**（v3 新增）backfill 契約**：
   - **視窗起點 = `min(now - trailing lookback, since)`，`since` = `stream.backfill_since()`
     原樣傳入**：stream 自持兩個義務——startup floor（開機時以 `set_startup_floor` 登記
     一次：帳上最新 fill 的時間、帳上無 fill 則用 run genesis）與重連的 gap anchor
     （斷線時刻）——回傳兩者中較早者，且**只**由同一個 epoch-gated 清除一起退休；例行
     heartbeat 什麼都不欠、`since` 為 None。**fold 放在 stream、不由呼叫方 dispatch**：
     「啟動用 floor、之後用 anchor」的分派有個靜默漏洞——首連成功後幾秒內斷線（長時間
     停機後網路不穩正是這個窗口）會錨出一個開機後的小 gap，呼叫方把非 None 的 anchor
     當成全部，蓋掉還沒補的 floor，那一整段停機 fills 從此沒有任何 pass 會撈。單靠固定
     trailing window 會在「斷線比視窗久」時（隔夜斷線、壞掉的部署、systemd 重啟迴圈）
     把那一段 fills 漏成**沒有任何路徑會撈到**，而且不報錯。**這個起點是只有 stream 的
     bookkeeping（開機登記＋斷線錨定）知道的事實**：fills 表本身答不出來——只要有任何
     一筆較新的 fill 入帳（重連時 HL 會推 `isSnapshot` 批次），`MAX(exchange_fill_time)`
     就會跳到現在，視窗縮回 lookback。
   - 回應**分頁**（`userFillsByTime` 單次上限 2000 筆）：整頁滿代表被截斷，必須續頁。
   - 一次 pass 若無法證明它覆蓋了整個視窗（頁數預算用盡、游標無法前進），必須回報
     `complete = False`：**gap 仍然開著，呼叫方不得清掉 `needs_backfill`**。
   - `complete = False` 的 pass **不推進 gap anchor、不持久化部分進度**——下一次 pass 從
     同一起點重走。這是刻意的 fail-loud：頁數預算（20 頁 × 2000 筆 ≈ 40k fills）對這隻
     bot 的成交量而言遠不可達，走到這裡代表狀態異常（起點錯了、時鐘錯了、或帳戶被
     別的東西狂刷成交），要人來看，而不是讓 anchor 悄悄前移去「收斂」一個不該存在的
     視窗。代價是超過預算的 gap 會**持續卡在 incomplete**（每 tick 一條 warning）；
     PR 4 的 safe-mode 狀態機必須把「backfill 長期 incomplete」納入停機判準。
   - `needs_backfill` 的清除以 **epoch** 為閘：先讀 epoch → 跑 backfill → 用讀到的 epoch
     清。期間若又發生重連（epoch 遞增），這次清除會被拒絕——那個新的 gap 不會被一個
     在它出現之前就關窗的 pass 誤判為已補完。
   - transport error 直接往上拋（旗標留著、下一 tick 重試）；非 list 的 REST payload 是
     malformed，不是「沒有 fills」。

   **（v12 新增，2026-07-15）backfill 義務必須耐重啟——持久化歸 PR 4**：上述兩個義務
   （startup floor 登記、gap anchor）與 fail-loud 的卡住態（`complete = False` 而
   `needs_backfill` 未清）在 PR 3 都只活在 process 記憶體。重啟後 floor 重新取
   「帳上最新 fill」，這個推導**只在前一個 process 沒有帶著未退休的義務死掉時才安全**。
   兩條靜默漏 fill 路徑：(a) 長於 trailing lookback 的斷線結束、重連的 `isSnapshot`
   批次已入帳、gap backfill pass 完成前 crash——重啟後 floor 跳到最新入帳 fill、視窗縮回
   lookback，斷線段的舊 fills 從此沒有任何 pass 會撈，§5 replay 也驗不出（replay 只能
   重折已記錄的事件）；(b) 頁預算耗盡的卡住態被 systemd 自動重啟抹掉，刻意的 fail-loud
   變成永久的靜默缺口。因此 **PR 4 接 daemon 接線時必須把義務持久化**：完成一次
   `complete = True` 的 pass 才推進一個 durable watermark（如 `scheduler_state` 上的
   「最後一次乾淨 backfill 覆蓋到 T」），startup floor 取 `min(watermark, 帳上最新 fill)`
   ——多撈的部分由去重鍵吸收；卡住態隨之自然耐重啟（watermark 不前進，重啟後視窗
   重新張開、warning 繼續響）。PR 3 刻意**不**先加欄位與 migration：讀寫點（tick 迴圈
   的 backfill 呼叫方）到 PR 4 才存在，比照「欄位等首個 writer 再加」原則。同族掃描
   確認其餘 in-memory 狀態（kill-switch latch、gate 旗標、engine halt/pause、scheduler
   pending）都有 fail-closed 後盾或持久化證據，唯一殘餘是 rule 7c 的 proof-of-life
   時鐘：「曾收過事件」的跨斷線歷史同樣只活在記憶體，重啟會讓 flap-never-deliver
   的 feed 讀回 0、永不 stale（a、b 兩時鐘對它也各自被短週期重置）——PR 4 持久化
   義務時應一併評估把最後事件時戳納入。
6. WebSocket 斷線期間禁止 new entry / add / rebalance。
7. **（v3 修訂）下列任一成立即視為 stale，進入 recoverable safe mode**：
   - a. socket 已關閉，且斷線超過 5 分鐘（`stale_after_seconds`，預設 300）；
   - b. socket **自稱仍連著**但靜默超過 2 分鐘（`silent_after_seconds`，預設 120）。
     half-open TCP（NAT／load balancer 的常態）不會回報 close，只量「斷線多久」的存活
     判斷會把一條死掉的 feed 永遠讀成健康，機器人就在一條收不到任何東西的 socket 上
     繼續開新倉。`webData2` 會持續推送帳戶狀態，所以一條真正活著的 socket 不會這麼久
     完全沒有事件。靜默的計時基準是「最後一則事件」與「連上的那一刻」**取較晚者**
     （沒基準的話一連上就沒動靜的 socket 永遠不會 stale；不取較晚者的話，上一條連線
     的舊事件會讓長斷線後的健康重連立刻被誤判）；
   - c. **曾收過事件、而距最後一則事件超過 5 分鐘**（同 `stale_after_seconds`），
     **不因重連重置**——這是 proof-of-life 時鐘。少了它，一條劣化成 flapping 的
     feed（每次都連得上、連上後從不送事件就又斷，週期短於上面兩個門檻）會把
     a、b 兩個 per-connection 時鐘無限重置、永遠不 stale。反向的含義是 **stale 具
     黏性**：超過門檻後光重連不算恢復，要等第一則事件（真恢復時 `webData2` 秒級
     就會推）才解除。

   **（v9 註記，2026-07-15）三個時鐘都是連線層級**：任何頻道的事件都算
   proof-of-life。`webData2` 持續推送，所以若單一訂閱（如 `userFills`）在 server 端
   單獨死掉（訂閱被拒、被 drop），stream 仍讀作健康——那段期間 fills 由 heartbeat
   REST backfill 兜底（rule 5；dedupe 保證不重不漏，缺口 bound 在一個 heartbeat
   間隔內）。頻道層級的 staleness **刻意不做**：`userFills` 只在有成交時才有事件，
   安靜市場會恆誤報 stale，而它沒有自己的「應該要有事件」基準。PR 5 接 SL/TP 時
   必須知道 position 在這個窗口內可能過時。

   **（v11 新增，2026-07-15）stale 的修復動作歸 PR 4 safe-mode 狀態機**：偵測（本節
   三時鐘）與修復是兩件事，而修復在 half-open 情境**不會自己發生**——半開 socket 的
   handle 仍在，`ensure_connected` 讀作已連線而 no-op；SDK 的 `on_close`（PR 5 接線）
   在正好這種情境也不會 fire；stale 又具黏性、要第一則事件才解除。所以 PR 4 的
   safe-mode 狀態機在讀到 `is_stale` 時，除了進 recoverable safe mode，**必須主動強制
   重連**（`supervisor.close()` ＋ `ensure_connected()`，現有 API 已足）。ws_stream
   維持「只報告、不行動」——重連策略（頻率、退避）是 safe-mode 的決策，不是
   bookkeeping 的。

### 11.3 WebSocket Failure Handling

```
ws_disconnected
→ start reconnect loop
→ disable new entry / rebalance
→ REST polling continues

ws_disconnected > 5 min  OR  ws_silent_while_connected > 2 min
  OR  ws_event_quiet > 5 min (proof-of-life，不因重連重置)
→ recoverable_safe_mode

ws_reconnected
→ REST backfill fills / orders / account state
→ reconciliation
→ if passed, may auto recover

ws_event_parse_failed
→ record raw payload + error
→ do not apply malformed event
```

### 11.4 併發模型（v3 新增）

SDK 的 WebSocket manager 是背景執行緒 callback 模型；本系統維持 Phase 2 的
單執行緒 30 秒 tick 迴圈：

1. WS callback **只做一件事**：把原始事件放進 thread-safe queue（不碰 SQLite、
   不做業務邏輯）。
2. 既有 tick 迴圈每輪先排空 queue（去重、寫 DB、更新狀態），再做其他工作。
3. SQLite 維持單一寫者，事件處理順序確定。
4. 代價：fill 入帳最多延遲一個 tick（≤ 30 秒），對 4h 決策週期無影響。
5. **（PR 5 修訂，2026-07-21）**live loop 的實際 tick 週期為 **10 秒**——遠小於
   kill-switch 的 `max_tick_gap_seconds = 30`，讓 §18.2 刷新在 tick 做實事
   （對帳網路讀取、SL repair）時仍不遲到；AI 決策跑背景 thread，不佔 tick。
   切片仍按 §9 的 30 秒節奏配速（tick 只是檢查點），fill 消化延遲上限相應
   ≤ 10 秒；paper 迴圈維持 30 秒。

## 12. Exchange Reconciliation

### 12.1 Truth Source

Live mode 中：

```
Exchange = 真實 orders / fills / positions / account state 的事實來源
SQLite   = 本地 audit trail / replay / bot state 的事實來源
```

SQLite 不得覆蓋交易所事實。若兩者衝突，系統必須進入 reconciliation 流程。

### 12.2 Reconciliation Timing

必須在以下時機執行 reconciliation：

1. Process startup
2. 每個 AI cycle 開始前
3. 每個 AI cycle 結束後
4. 每次 live order ack 後
5. 每次 live fill 後
6. 每次 SL / TP 建立或修改後
7. 每 5 分鐘 heartbeat check
8. Shutdown 前
9. 偵測到 WebSocket / REST mismatch 時

**v1 實作範圍（PR 5，2026-07-21，使用者拍板）**：live loop 實作 1（`startup`）、
5（`fill`）、6（protection sync 有建立／修改／取消任何 SL/TP 的 tick，
`protection_change`）、7（`heartbeat`）、8（§18.2 sweep 前 best-effort，
`shutdown`）與 9 的 §10.5 超標子集（`mismatch`）。2（cycle 前）、3（cycle 後）、
4（每次 order ack 後）延後至 PR 6 隨 WS 接線一併實作——REST backfill 模式下對每張
slice ack 都跑全量 reconcile，會使 plan 執行期間的 API 負載大約翻倍。

### 12.3 Reconciliation Cases

| Case | Required behavior |
|---|---|
| SQLite 有 order，但交易所查不到 | 標記 order_missing_on_exchange，不得盲目重送 |
| 交易所有 bot-owned open order，但 SQLite 沒記錄（bot-owned 由 cloid_hex 反查 SQLite 的 cloid_registry 判定，見 §19.3） | 補寫 SQLite 或 cancel，並標記 orphan_exchange_order |
| 交易所有 order，但其 cloid_hex 在 SQLite 查無對應（non-bot-owned） | 進入 manual safe mode；不自動管理 |
| SQLite 有 fill，但交易所查不到 | 標記 invalid_local_fill，不得套用 live accounting |
| 交易所有 fill，但 SQLite 沒記錄 | 補寫 exchange fill，再 replay accounting |
| 交易所有 position，但 SQLite 以為 flat | 標記 exchange_position_mismatch，停止新開倉 |
| SQLite 有 position，但交易所以為 flat | 以交易所為準，replay / correct local state |
| Exchange account equity 與 SQLite 差異超過 tolerance | 進入 safe mode |
| 交易所有 active position 但沒有 valid SL | 立即 repair；repair 失敗則 emergency close |

**（v10 新增、v11 修訂，2026-07-15）PR 3 的 ingest 端 sighting rows**：unmapped／
malformed／money-drift／fee-drift 四種 fill 觀察在第一次看到時即寫入
`exchange_reconciliation_events`（`case_type` = `fill_unmapped` / `fill_malformed` /
`fill_money_drift` / `fill_fee_drift`；once per fact——同一事實每輪 backfill 重見不
重複寫，`(run_id, case_type, exchange_value)` 為去重鍵）。理由：evidence 檔是
write-only 證據、log 會滾動，而 fill 一旦老出所有 backfill 窗口（trailing 6h、
floor/gap 義務皆清）就沒有任何路徑會再抓到它——DB row 是唯一可查詢的 backlog。
resolution 不改寫 case rows（它們是 log），且**「已解決」的定義是分型的**——
`exchange_value` 的內容形狀每型不同，一條共用的 anti-join 對其中三型永遠不會除帳：

- **`fill_unmapped`**：`exchange_value` 就是 §14.2 去重鍵。PR 4 的 discovery 以它對
  `fills.exchange_fill_key` 反查（anti-join）找出仍未入帳的 sighting，作為「交易所有
  fill 但 SQLite 沒記錄」那一列的已知起點清單（含 `detail` JSON 內的
  `exchange_fill_time`，給補抓窗口定位）；「已解決」= anti-join 不再命中（§8.3
  recovery 補上 mapping 後 re-ingest 入帳即自然除帳）。
- **`fill_malformed`**：`exchange_value` 是裸 tid 或 content digest（§11.3 的 malformed
  key），**永遠 join 不到** `fills.exchange_fill_key`。它代表「有一筆看不懂的 payload」
  而非「有一筆確定的 fill 沒入帳」；解決路徑是人工檢視證據檔（修 parser、或確認
  payload 本來就是垃圾），由 PR 4 的 sweep 以 `action_taken` 標記處置。
  **未 stamp `action_taken` 者擋住 verdict（2026-07-17 定案）**：交易所報過、SQLite
  沒入帳的錢就是「交易所有 fill、本地沒記錄」，只是無法 key。小額 malformed 若沒把
  倉位尺寸推歪、又藏在 equity 容差內，audit-only 會讓 run 判 clean 照常交易——故
  改為擋 clean、在 report 的 `errors` 具名，直到人工 stamp 才放行；裸 tid 的 sighting
  若事後被 §8.3 recovery 補入帳，sweep 仍自動標 `resolved_fill_booked` 除帳。
  人工標記的**工具**是 `safe-mode --stamp-case <event_id> --action "<處置說明>"`
  （event_id 由 `safe-mode --status` 的 open-cases 清單提供）：digest-keyed 的
  sighting 永遠 join 不到 fills、自動車道永遠救不了它，若無此指令，交易所丟一筆
  看不懂的 payload 就會讓 run 永久卡在 safe mode，只能對正式庫手寫 SQL。已記錄的
  處置不覆寫（第一筆就是稽核事實），且 stamp 不等於恢復交易——仍須過下一輪對帳。
  **`fill_unmapped` 具名拒絕 stamp**：它的「已解決」是對 `fills` 的 anti-join（見
  上），根本不讀 `action_taken`——stamp 它清不掉任何 verdict，卻會把該列從 open-cases
  清單抹掉（那是操作者唯一的 backlog 列舉面），等於製造出這個指令本來要防的死結。
  故 open-cases 清單對兩種 resolution model 用**兩個**判準：`fill_unmapped` 以
  anti-join 判定是否仍未解（無視 `action_taken`）、其餘以 `action_taken IS NULL`，
  且逐列標明真正的解決路徑。
- **`fill_money_drift` / `fill_fee_drift`**：`exchange_value` 是 `去重鍵|drift digest`，
  且描述的 fill **已經入帳**——它們根本不屬於「帳上沒有的錢」backlog，出現在
  anti-join 結果裡是誤列。它們是「同 tid 但內容矛盾」（money drift）或「別的 run 的
  fill 其 fee 有更正」（fee drift，見 §15.1 rule 8）的審計線索，解決路徑是人工核對後
  以 `action_taken` 標記。

## 13. Safe Mode

safe mode 不是整個程式停掉，而是系統還活著、繼續監控與保護倉位，
但暫停任何會增加風險的新交易。

### 13.1 Safe Mode Allows

```
讀 market / account / position
REST backfill fills / orders / funding
reconciliation
刷新 scheduleCancel
cancel bot-owned stale orders
修復 SL / TP
reduce-only close
emergency close
記錄 error / event / snapshot
```

### 13.2 Safe Mode Blocks

```
open long / open short
加倉
反向開倉
建立新的 slice plan（entry / rebalance）
根據 AI 新 decision 建新 order
沿用上一輪 target 補下單
```

### 13.3 Safe Mode Types

```
recoverable_safe_mode
manual_safe_mode
```

### 13.4 Recoverable Safe Mode

進入情境（可自動恢復）：

```
WebSocket 斷線超時（恢復並 backfill 後可解）
daily loss cap 觸發（§10.3，次日 UTC 00:00 可解）
kill switch refresh failed（refresh 恢復後可解）
market data 連續取不到（PR 5：連續 12 tick（約 2 分鐘）NO_MARKET_DATA →
  reason `no_market_data`；期間 protection sync／daily-loss／slice 全 hold
  （§10.2 fail-closed），每次 miss 記 `no_market_data` tick 事件保持可視）
```

恢復條件（全部滿足才解除）：

```
WebSocket restored, if required
REST backfill completed
open orders reconciled
fills reconciled
position reconciled
account state reconciled
existing position has valid SL
kill switch active
no unresolved mismatch
（daily-loss 觸發者另須：已過次日 UTC 00:00）
```

附註（2026-07-17）：上列條件要求 reconciler **全接線**——backfill／invalid-fill
交叉檢查 seam 缺席的 pass（report 記入 `legs_skipped`）證明不了「REST backfill
completed／fills reconciled」，即使其餘 legs 全 clean 也不得自動解除；半接線
（例如未綁 fill seams 的心跳 wiring）的 clean pass 只能維持、不能解除 latch。

同一立場適用於 "WebSocket restored, if required"：**沒有 WS 可恢復 ≠ WS 已恢復**。
PR 4 的 one-shot `live --run-id` wiring 完全沒有 WS stream，故一律 attest
`ws_restored=False`——否則前一個 process 因 `ws_disconnect` 進入並持久化的
recoverable latch，會被一個從未連過 WS 的 process 以「帳本乾淨」為由解除。該
latch 留給 PR 5 daemon（真正握著恢復後的 stream 時）解除。

**v1 loop 例外（2026-07-21，使用者拍板）**：PR 5 的 `live --loop` 尚未接真 WS
（§11 socket wiring 隨 PR 6），fill 來源本來就是 reconciler 的 REST backfill——
在這個 wiring 下「無 WS 可斷」不構成斷線條件，若照上段 attest `False`，
daily-loss 過午夜、`live_tick_error` 等所有 recoverable latch 在 v1 全部變成
只能手動解除。故 v1 loop 傳 `ws_restored=not ws.is_stale()`（從未連線的
stream 恆為未 stale ⇒ 實務上恆 `True`），接受 clean pass 自動解除各 recoverable
latch；PR 6 接上真 WS 後改回真實 stream 狀態，屆時上段的嚴格立場恢復適用。

「kill switch active」的 attestation 同樣必須**掙來**（2026-07-23）：live loop 的
reconcile pass 在 sticky latch up 時走 `release_safe_mode()`（§18.2 的唯一解鎖門，
帶 proving refresh；已觸發的 switch 會在該 refresh 內重新上鎖）並把其裁決傳給
`kill_switch_active`——裸讀 latch 沒有任何解除路徑，一次暫時性 refresh 失敗就會
讓 run 永久卡死（切片停、SL/TP 也掛不上：§4.1 kill-switch 線不在 protective 豁免
內），只能重啟 process。release 只在 reconcile cadence 嘗試、緊鄰結算 rows 的
pass，非 retry loop。

§19.3 stale-order sweep 的撤單失敗是 **verdict 輸入**（2026-07-17）：撤不掉的單
仍在交易所掛著，該 pass 不得讀成 clean。失敗以 `ReconciliationReport.sweep_failures`
欄位隨 pass 一起帶進 `run()`（即在 `_record` 落庫**之前**），故不會出現「clean pass
先自動解除 latch、事後才因 sweep 失敗重進」的 release→enter flap 與 `entered_at`
重錨；也不會發生「in-memory 判 unclean、落庫的 `reconciliation_status` 卻寫成 ok
且 diff 沒有任何原因」的稽核背離（事後才折進 `errors` 就會如此）。
`legs_skipped` 在 `clean` 之外，`sweep_failures` 在 `clean` 之內——前者是「這個
wiring 沒查」，後者是「查了而且失敗」。reconciliation legs 本身全 clean 時（
`ReconciliationReport.reconciliation_clean`），進入原因具名為
`stale_order_sweep_failed` 而非泛用 mismatch；複合失敗（case + errors-only 腿 +
sweep）則三個管道的原因**全部**寫進 safe-mode detail，不得互相蓋掉。

### 13.5 Manual Safe Mode

以下情況需要人工解除：

```
non-bot-owned order / position detected
unknown exchange position
repeated reconciliation mismatch
SL repair failed and triggered emergency close
consecutive loss cap 觸發（§10.4）
account equity / margin abnormal drop
authentication / permission error
```

Manual latch 掛住期間，AI 決策 cadence 本身**暫停**（PR 5 定案，2026-07-22）：
每個目標反正都會被 §4.1 `manual_safe_mode` 擋下，live decision driver 於
due tick 直接跳過整個 multi-agent LLM 呼叫（不建 attempt row、不燒 LLM 成本；
`next_decision_at` 不推進），§13.6 人工解除後下一個 tick 立即以新鮮輸入重新
決策。已在途（in-flight）的決策不受影響——照常收集、進 gate、留 §4.1
rejected row。recoverable safe mode 不暫停決策。

### 13.6 持久化與人工解除介面（v3 新增）

1. Safe mode **現態**存在 SQLite（`scheduler_state` 新增欄位：
   `safe_mode_type`、`safe_mode_reason`、`safe_mode_entered_at`）；
   **歷史**記入 `safe_mode_events` 表。重啟不會消除 safe mode——
   狀態在 DB，啟動 reconciliation 後照舊生效。
2. Manual safe mode 的解除用 CLI 子命令：

   ```
   python -m contrib.hyperliquid_perp safe-mode --run-id <id> --release --reason "<人工確認說明>"
   ```

   解除動作寫入一筆 `safe_mode_released` 事件（含 reason）留審計軌跡。
   同一子命令的 `--status`（預設動作）查詢現態與近期歷史，exit code
   0＝不在 safe mode、4＝latch 中（supervisor 探針可據此分支）；
   `--reason`／`--released-by` 只配 `--release`，status 模式下具名拒絕，
   非 live run 的 `--run-id` 亦具名報錯。
3. 解除後系統**不會**直接恢復交易：仍須通過下一輪完整 reconciliation
   （§13.4 恢復條件）才允許新單。
4. 不提供 config 旗標式解除（容易忘記改回、審計不乾淨）。

## 14. Fill Ingestion and Deduplication

Phase 2 的 fills 是本地模擬；Phase 3 的 live fills 必須以交易所為準。

### 14.1 Fill Sources

Live fill 可以來自：

1. WebSocket user fills
2. REST userFillsByTime
3. REST orderStatus

同一筆 fill 可能從多個來源出現，因此必須去重。

v3 註記：因採自管切片，所有 fills 都是一般 order fills（帶 oid + cloid），
不需要 native TWAP slice fills 的特例通道。

### 14.2 Live Fill Dedupe Key

`exchange_fill_key` = 交易所自帶的穩定 fill id：`tid|<tid>`。Hyperliquid 在
`userFills`（WS）與 `userFillsByTime`（REST）都必帶 `tid`（SDK 的 `Fill` 型別中
是必填 int），所以同一筆 fill 從任何來源都導出同一把 key（§14.3 rule 5）。

無 `tid` 的 fill 一律視為 **malformed**：記 raw payload、不入帳（§11.3），留給
reconciliation，不得改用 composite key。

**（v3 修訂）composite fallback `symbol + oid + fill_time + side + price + size`
刻意不實作**：

- 它會把「同一張單、同一毫秒、同 side／price／size 撞到兩筆 resting order」的
  兩筆**真實** fill 撞成同一把 key，第二筆被當 duplicate **靜默丟掉**——而 replay
  只重算已記錄的東西，偵測不到這種漏記；
- 任一來源若曾漏 `tid`，同一筆 fill 會導出兩把 key 而**重複計帳**。

兩個方向都不安全，所以一個都不猜。若未來的交易所真的沒有穩定 fill id，composite
才回到這裡討論，而且必須正面處理碰撞風險，不是繼承這段文字。

### 14.3 Fill Application Rules

1. 同一筆 exchange fill 最多只能套用一次。
2. Fill 寫入 SQLite、fee / PnL 過帳、position update、account update
   必須在同一個 SQLite transaction 內完成。
3. 若 transaction commit 前 crash，該 fill 不算套用完成。
4. 若 transaction 已 commit，重啟後不得再次套用。
5. REST 補查與 WebSocket 回報不得造成重複計帳。
6. **（v3 新增）套用與 replay 一律依交易所時間排序**，排序鍵為
   `(exchange_fill_time, exchange_fill_key)`——兩筆 fill 可能落在同一毫秒，故以
   dedupe key 破平手。「先到＝先發生」不成立：WS 與 REST backfill 是兩個競速來源，
   而 §12.3 的 unmapped fill 一旦重新 ingest，必然晚於較新的 fill 進來。
7. **（v3 新增）若一筆 fill 的排序鍵早於帳上最新的 fill（out-of-order），該 symbol
   的 position 必須從 run genesis（seed positions）依交易所時間重折一次**：size 與
   加權平均 entry price 不可交換，疊在較新的 fill 之上會永久錯誤，而 entry price 會
   流進 unrealized PnL、equity、清算價與 PR5 要掛的 SL/TP 價位。ledger（wallet /
   realized / total_fees）不需修復——它們是 per-fill delta 的總和，可交換。
8. **（v3 新增）materialized position 與 accounting replay 用同一個 genesis、同一個
   排序、同一套 per-fill 數學**，兩者一致是 by construction 而非巧合。若兩者會漂移，
   各自都仍是內部自洽的，§5 一致性檢查就無法判斷誰才是對的。

## 15. Fee and Funding Reconciliation

Live mode 中，fee、funding 與 realized PnL 以交易所資料為準。

**（v3 新增）帳務單一基準**：live run 的帳本只有一套——記錄下來的交易所事件
（fill 含 exchange fee / closedPnl、funding、accounting adjustment events）。
accounting replay 從這些事件重建 position / account，必須與 materialized state
一致。Phase 2 的本地 fee / funding 模型**僅供 paper mode 使用**；
「real fee / funding comparison」（範圍項目 5）做成離線報表
（模型估計 vs 交易所實際），不影響帳務。

### 15.1 Fee Rules

1. 若 fill payload 直接提供（USDC）fee，立即入帳。
2. 若 fee 暫時缺失、以非 USDC token 計價、**或 `feeToken` 欄位缺失（無法證明是
   USDC——該欄位文件上恆帶，缺失即 payload 漂移，寧可延後入 fee 也不冒記錯幣別的險）**，
   該 fill **仍照常入帳**，posted fee 記 0，
   `fills.exchange_fee` 保持 NULL 表示 **pending**（沒有獨立的 `fee_status` 欄位；
   pending 的定義就是 `exchange_fee IS NULL`，見 `iter_live_fills(pending_fee_only=True)`）。
3. Pending fee 不得永久留空，需由 reconciliation job 回補。**（v9 新增，2026-07-15）
   回補入口即要求幣別證明**：`backfill_fill_fee` 除金額外必收 `fee_token`，非
   `"USDC"` 一律拒絕——與 rule 2 的 ingest 端 fail-safe 對稱。fill 進 pending lane
   正是因為 fee 未能證明是 USDC；若解決入口不再要求同一份證明，「從 raw payload
   讀 `fee` 直接回補」這個最直覺的 job 實作會把非 USDC 金額記成 USDC——pending lane
   要防的就是這個。非 USDC fee 必須先估值，再以 USDC 金額（`fee_token="USDC"`）回補。
4. 回補**不得覆寫**已記錄的 fill（fill row 不可變）。它寫一筆
   `accounting_adjustment_events`（`old_value` = 帳上現行 fee、`new_value` = 新學到的
   fee），ledger 在**同一個 transaction** 內依 `new - old` 移動，live replay 折算同一組
   delta——所以 materialized 與 replay 不會漂移。
5. **（v3 修訂）更正是累積且有序的，不是一次性的**：
   - `adjustment_id` 帶 `seq`（該 (target, type) 已有幾筆更正：0、1、2…）；
   - 重複學到**相同**金額 = no-op，不寫事件（reconciliation job 可以每一輪安全呼叫）。
     **唯一例外：仍在 pending（`exchange_fee` NULL 且尚無 fee 更正）的 fill，首次回補
     即使金額恰等於 placeholder 0 也要寫事件**（ledger 移動 0）——那筆事件才是把 fill
     帶離 pending backlog 的東西，不寫的話 rule 3 永遠無法對「真實 fee 恰為 0」的 fill
     成立；
   - 學到**不同**金額（referral 折扣、事後 rebate、交易所自己更正 fee）寫下一筆更正，
     只 post 差額。若以 (target, type) 為唯一鍵拒絕第二筆，不只會漏掉那筆金額，還會讓
     不斷重試同一筆 fill 的 reconciliation job **永久卡死**在那裡。
6. **（v3 新增）exchange fee 可以是負的**（maker rebate，已在 mainnet 實測到）。
   `AccountLedger.total_fees` 因此**不再有 `>= 0` 約束**——真收到 rebate 時那個 guard 會讓
   ingester crash → rollback → 每次 backfill 重試再 crash，整條 ingestion 卡死。paper 模型的
   非負性仍由 paper 的 fill 邊界（`compute_fill_effect` / `FillEffect`）強制。
7. **（v3 新增）`closedPnl` 是 gross of fee**（未扣該筆 fee），已對 live mainnet `userFills`
   實測確認（六個幣、兩個方向、maker 與 taker，殘差恆為 0），故
   `wallet_delta = closedPnl - fee`。注意官方「Entry price and PnL」文件頁寫的
   `closed pnl = fee + side * (mark - entry) * size` 描述的是**前端顯示欄位**，不是這個
   API 欄位，不要照它「修正」公式。
8. **（v10 新增，2026-07-15）重投遞（redelivery）是 rule 5 的自動偵測管道**：heartbeat
   REST backfill 每輪重抓 trailing 窗口，已入帳的 fill 會被排程性重投遞，所以 ingest 的
   DUPLICATE 車道不只憑 key 丟棄，而是驗證內容（`_verify_redelivery`）：
   - **fee 與帳上不同** → 走 rule 5 的 `backfill_fill_fee` 自動 post 差額（交易所帳單是
     唯一基準）。比較基準是「入帳時記錄的 `exchange_fee`」，刻意**不是**折算後的
     effective fee——同一份 stale payload 每輪重來，不得把 rule 3 的人工估值回補
     flip-flop 回去；payload 金額真的變了才查更正鏈並補差額。pending fill 的重投遞
     若帶 USDC fee，即為首次回補（rule 5 的 pending 例外照常適用）。
   - **身份欄位（sz/px/side/coin/closedPnl/liquidity_role）不同** = 「同 tid、不同
     fill」，沒有可重記的車道：記證據檔＋`fill_money_drift` case row（§12.3），
     不套用；也不在身份漂移之上補 fee——連「這是哪筆 fill」都說不清的 payload，
     它宣稱的 fee 不可信。
   - **內容一致**（絕大多數）→ 無任何寫入。
   跨 run 的 duplicate 不動**任何** ledger（`backfill_fill_fee` 的 run 檢查會拒絕）；
   漂移仍記證據——身份漂移走 `fill_money_drift`，**（v11 新增）fee-only 差異走
   `fill_fee_drift` case row**（§12.3）：fee 車道對別的 run 無法 post，而 fee 又刻意
   不在身份比對集合裡，沒有這個 recorder 的話，交易所對已結束 run 的 fill 發出的
   fee 更正會無聲消失。比對語意鏡像同 run 車道的**淨**行為：先比 as-ingested（無新
   資訊即返回），再比該 run 折算後的 effective fee（那個 run 的更正鏈已載有此金額＝
   `ALREADY_POSTED` 的同義，不記已知為假的線索）；仍 pending 的 fill 不受 effective
   閘靜默（rule 5 的 pending 例外同義，fee 恰為 placeholder 0 也是新資訊）。

**（v11 新增，2026-07-15）per-fill fee 的讀者義務**：`fills.exchange_fee` 是**入帳當下**
的快照（pending = NULL，posted 0），rule 4/5 的更正**只**活在
`accounting_adjustment_events`，fill row 永不回寫。因此任何 per-fill fee 的消費者
（CSV export、§21 驗收指標、摩擦占比分析）**不得**直接讀 `fills.exchange_fee` 當生效
值——直接讀會對 pending fill 拿到 0、對已更正 fill 拿到過期值，per-fill 加總也不會等於
ledger 的 `total_fees`。生效 fee 的唯一定義是「as-ingested ＋ 折上全部 fee 更正」
（`_effective_fee` / `posted_exchange_fee` 那組 helper）；PR 4/PR 6 的 export 與指標
實作必須走共用 helper（或等價的 join），不得手刻第二份定義。

### 15.2 Funding Rules

1. Funding event 以 (run_id, symbol, funding_timestamp) 去重。
2. Funding 缺失時標記 funding_status = pending。
3. REST reconciliation 需補齊 missed funding。
4. Funding correction 必須產生 accounting adjustment event。
5. **（v9 新增，2026-07-15）兩扇門的分工——事實 vs 更正**：漏掉／遲到的 funding
   結算是「還沒記錄的事實」，一律走 rule 3 → `funding_events`（首次記錄，
   pending→posted 的 exactly-once 閘）；**只有已 posted 的 funding event 金額事後
   被證明錯誤**，才走 rule 4 → `adjustment_type='funding'` 的更正。同一筆結算絕不能
   兩扇門都走：live replay 對 funding_events 與 adjustment 兩者都 fold，重複記錄會讓
   wallet double-count，而且 §5 replay 檢查**抓不到**（materialized 與 replay 兩側
   同樣 double-fold，恆等式照樣成立）。
6. **（v9 新增，2026-07-15）adjustment 是 ledger-only**：任何型別的 accounting
   adjustment event（fee／funding／realized_pnl）只移動 ledger 四個總額（wallet／
   realized／fees／funding，經 `adjustment_ledger_delta`，replay 的 fold 也只在
   ledger 層），**永不觸碰 position row**。`realized_pnl` 型別目前沒有 writer
   （詞彙刻意保留給未來的更正場景）；它的 writer 進場時必須遵守本條，否則
   materialized position 與 replay position 會分歧。

## 16. Schema Additions

Phase 3 延伸 Phase 2 SQLite / CSV schema（migration v6+，僅 ADD COLUMN /
CREATE TABLE，沿用既有 `MIGRATIONS` 版本化機制）。

### 16.1 orders Additions

```
cloid_logical            # v3 註記：新增欄位；既有 client_order_id 保留閒置、標 deprecated
cloid_hex                # 實際送交易所的 128-bit hex 值
exchange_status
exchange_raw_status
submitted_at
acknowledged_at
canceled_at
cancel_reason
is_bot_owned             # 由 cloid_hex 反查 cloid_registry 決定，見 §19.3
raw_exchange_payload_path
```

（`exchange_order_id`、`order_role`、`reduce_only` Phase 2 schema 已有，不重複新增。）

詞彙契約（v5 新增，2026-07-13）：`exchange_status` 一律寫正規化家族詞（與
`orders.status` 同詞彙：open / filled / canceled / rejected），`exchange_raw_status`
一律寫交易所 verbatim 原字；ack 路徑與 §8.3 rule 4 恢復路徑都必須兩欄齊寫。
`cloid_hex` 有 UNIQUE index（一 cloid 一 orders row；NULL——所有 paper 舊列——互異）。
恢復路徑插入的 orders row `submitted_at` / `acknowledged_at` 保持 NULL：真實送出
時間未知，消費者必須把 NULL 讀成「未知」而非「未送出」。IOC ack 部分成交
（totalSz < 請求 size）時 `status` 寫 `partially_filled`，不得寫 `filled`；
成交數量真相仍由 PR 3 fill ingestion 擁有。

**（v11 新增，2026-07-15）live fill 的 plan/slice 歸因契約**：paper fill 的
`plan_id`／`slice_index` 由 engine 從記憶體內的 plan context 同步填入；live fill 走
非同步的 WS/REST ingest，唯一可用的 context 是 oid 反查到的 orders row——而 orders
目前沒有 plan/slice 欄位可抄（`flip_plan_id` 有、但 slice 沒有），所以 PR 3 的 live
fill 這兩欄暫為 NULL。**PR 5 動 schema 時必須把歸因欄位補上 orders**（`plan_id`／
`slice_index`，或等價的 slice→cloid 對照），讓 ingest 抄進 live fill——paper 與 live
的 fill row 形狀必須一致，per-slice TWAP 執行帳務（§9）與 export 分析才不用對 live
另走 order join 的第二套查法。這是刻意記在 PR 3 的前置契約：等 PR 5 的下單路徑
寫好才發現 fill 歸因斷鏈，補欄位就要回填資料而不是只加欄。

**（PR 5 定案修訂，2026-07-22）**：PR 5 實際**未**補歸因欄位——fill→plan 歸屬
與 `remaining_twap_qty`／`residual_qty` 真值一起延後到 PR 6 WS/fill 路由
（「誠實 NULL」決策，見 §9.2 rule 2 與 phase2-data §6.1）。上段預警的代價
已被接受：PR 6 補欄位時需回填 PR 5 期間的 live fills，或接受該區間兩欄為
NULL。

### 16.2 fills Additions

```
exchange_fill_key        # 去重鍵，UNIQUE
cloid_logical
cloid_hex
liquidity_role
exchange_fee
exchange_closed_pnl
exchange_fill_time
raw_exchange_payload_path
```

（`exchange_fill_id`、`exchange_order_id` Phase 2 schema 已有。）

### 16.3 account_snapshots Additions

```
exchange_account_value
exchange_withdrawable
exchange_margin_used
exchange_unrealized_pnl
exchange_raw_payload_path
reconciliation_status
reconciliation_diff
```

### 16.4 position_snapshots Additions

```
exchange_position_size
exchange_entry_price
exchange_liquidation_price
exchange_unrealized_pnl
exchange_margin_used
exchange_raw_payload_path
reconciliation_status
reconciliation_diff
```

### 16.5 New Internal Tables

```
exchange_reconciliation_events
kill_switch_events
live_order_attempts
protection_order_events
accounting_adjustment_events
safe_mode_events
cloid_registry            # cloid_logical ↔ cloid_hex 對照表，供 bot-owned 判斷使用
```

### 16.6 scheduler_state Additions（v3 新增）

```
safe_mode_type            # NULL / recoverable / manual
safe_mode_reason
safe_mode_entered_at
day_start_equity          # §10.3 daily loss cap 的當日基準
day_start_date            # UTC 日期
consecutive_loss_count    # §10.4
last_settlement_wallet_balance  # §10.4 segment 淨損益的錨（schema v7）
```

## 17. Stop Loss / Take Profit Protection

Phase 3 延續 Phase 2 的 protection invariant：

```
position_size == 0            → 不得有 active SL / TP
position_size != 0            → SL 必須涵蓋全部目前倉位
execution plan terminal 後    → TP 必須涵蓋全部目前倉位
```

### 17.1 Live Protection Rules

1. Live position without valid SL is not allowed.
2. 每次 position-changing fill 後，必須重新計算 SL。
3. 若 position 仍存在，必須建立或修改 reduce-only SL。
4. 若 position 歸零，必須取消殘留 SL / TP。
5. Slice plan / rebalance 執行期間，可暫停 TP，但不得移除 SL protection。
6. Execution plan terminal 後，必須建立或更新 TP。
7. SL / TP 數量必須與交易所實際 position size 對齊。
8. SL / TP 必須使用 reduce-only trigger order。
9. 第一版採 position-based SL / TP，不綁定單一 parent order。

附註——觸發後執行帶寬（2026-07-22，使用者拍板）：trigger 觸發後的執行單是
「限價護欄」（§9.2 rule 1 禁止無界 market）。TP 用 routine ±`max_slippage_pct`；
**SL 的帶寬取 `max(max_slippage_pct, 3%)`**——SL 只在劇烈行情下觸發，routine
0.5% 帶可能被跳空直接穿過（觸發後掛著不成交、倉位續虧，且下一次 sync 依
trigger/qty 未變仍回報 PROTECTED），3% 下限與 §9.4 急平單同族（aggressive、
仍有價格保護）。取捨：漏掉 TP 是機會成本，漏掉 SL 是無上限虧損。

### 17.2 SL Repair Policy

若 SL create / modify 失敗：

```
retry up to 3 times
retry delay = 5 seconds
still failed → reduce-only emergency close（aggressive IOC，見 §9.4，不做切片）
```

規則：

```
Live position without valid SL is not allowed.
SL repair failed after retries → emergency close.
```

附註（2026-07-21，使用者拍板）：「失敗」指**已上線**的失敗（交易所拒絕、
timeout、ack 遺失）。pre-send 的 §4.1 wire-gate 拒絕（protective 入口豁免
safe-mode 兩條後，實務上只剩 kill switch）**不燒修復預算**：整輪 ladder 都是
gate 拒絕時回報 `blocked`（事件 `stop_loss_repair_blocked`），不記 exhausted、
不升級急平——同一個 gate 也會擋急平單，升級只是徒勞的 close storm。失敗線
（`unresolved_protection_failure`）照設，擋新增風險單；下一 sync 重試，
§12.3 SL-missing 檢查是這段無保護窗口的兜底。ladder 途中的 delay 照跑並
tick kill switch（Q2 決策），故 gate 可在 ladder 中途重開。

### 17.3 TP Failure Policy

若 TP create / modify 失敗：

```
不平倉
進入 degraded protection
停止 new entry / rebalance
持續嘗試 repair 或等待人工確認
```

### 17.4 Modify-before-cancel Policy

Live mode 應優先使用 modify / batchModify 更新既有 protection order。

不得先 cancel 後 create，除非交易所不支援 modify 或 modify 明確失敗。

若必須 cancel + recreate，系統必須：

1. 進入 temporary protection update state。
2. 儘可能縮短無 protection 空窗。
3. 若 recreate 失敗，立即進入 safe mode。
4. 若 SL recreate 失敗，應 emergency close（見 §9.4）。

## 18. Kill Switch / Dead Man's Switch

Phase 3 必須同時支援：

1. Emergency Kill Switch：偵測異常後進入安全狀態
2. Dead Man's Switch：process 掛掉時，交易所自動取消掛單

### 18.1 Dead Man's Switch Config

```yaml
kill_switch:
  enabled: true
  schedule_cancel_seconds: 120
  refresh_interval_seconds: 30
  on_refresh_failed: safe_mode
  on_shutdown: cancel_bot_owned_open_orders
  emergency_close_on_shutdown: false
```

（`emergency_close_on_shutdown` 的行為（shutdown 時 reduce-only emergency
close）至今未實作——PR 5 的 protection manager 進場後改採 §18.2 rule 8 的
keep-protective shutdown，close-out 延後 PR 6；config 建構期拒絕 `true`——
未實作的行為不得被 config 靜默接受，v4 註記、2026-07-23 修訂。）

（`schedule_cancel_seconds` 建構期要求 > 5：Hyperliquid 拒絕觸發時間距今不足
5 秒的 scheduleCancel，≤ 5 的值永遠 arm 不起來，而 §18.2 rule 1 把 arm 失敗
定為硬錯誤——無法實作的 config 值在載入期拒絕，v5 新增，2026-07-13。）

（`schedule_cancel_seconds >= 2 × refresh_interval_seconds` 建構期強制（v7 新增，
2026-07-13）：舊規則只要求 `refresh < schedule_cancel`，但 manager 只在被 tick 時
刷新，實際節奏被**呼叫方的 tick 間隔**量化，**一次 skip 或慢一拍就把下次刷新推到
約 2× interval**。`refresh=119 / schedule_cancel=120` 這種舊規則接受的 config，第一個
≥119s 的 tick 會落在約 120s——dead man's switch 在**正常運行中**觸發，掃掉該錢包
全部掛單。要求 deadline 至少涵蓋兩個完整 interval，讓「漏掉一輪」是可存活的而非
致命的。預設值 120/30 是 4×。**這是必要條件、不是充分條件**——它看不到呼叫方的
tick 間隔，真正綁定的檢查是 §18.2 rule 2 的 `max_tick_gap_seconds` 建構期不變量。）

（**主機時鐘偏移在 arm() 檢查（v8 新增，2026-07-13）**：scheduleCancel 收的是**絕對**
deadline，而我們用**本地時鐘**算出它；`_detect_expired_deadline` 又以同一個本地時鐘量測
逾期——兩者自洽，於是主機時鐘漂移對 manager 完全隱形，卻默默改變交易所端真正的保護
窗。主機快 4 分鐘時，120s 的 dead man's switch 實際上變成 360s：process 死掉後掛單會
曝險 3 倍長的時間，而日誌與事件裡沒有任何跡象。`max_tick_gap` 不變量抓不到它——那條完全
在本地時間裡推理。因此 arm() 送出第一個 deadline 前，先用交易所回覆的時間戳
（`clearinghouseState.time`）比對本地時鐘，偏差 ≥ 5s 即拒絕啟動並要求修 NTP。只有「主機
偏快」這個方向是無聲的，故只有它需要防護：主機偏慢送出的 deadline 太近，交易所直接拒絕，
arm() 本來就會 fail loud。交易所未回時間戳時只警告不擋——時間戳拿不到應該降級這項**檢查**，
不該擋掉一個其他方面都健康的啟動。）

### 18.2 Required Behavior

1. Process 啟動後，完成 exchange client 初始化時，必須立刻 schedule cancel。
   `arm()` 只在啟動期呼叫一次：重複 arm 是接線錯誤（具名 RuntimeError），且 arm
   必須尊重 sticky `stop_new_orders`（`kill_switch_active = not stop_new_orders`，
   與 refresh 同一條式子）——否則第二次 arm 會把刷新失敗關上的 §4.1 gate 靠運氣
   重開，而不是走 §13.4 reconciliation（v7 新增，2026-07-13）。
2. Live loop 運行期間，必須依 `refresh_interval_seconds` 刷新 schedule cancel deadline。
   `refresh_due()` 的 interval 語意是「至少這麼頻繁」而非「不得早於」：當呼叫方以與
   refresh_interval 相同的週期 tick（例如 30s／30s 接線；PR 5 的 live loop 實際為
   10s tick／30s refresh，見 §11.4——slack 保護的正是「呼叫方剛好以 interval 為週期
   tick」的接線），tick 必然比它比較的那個
   排程時刻晚幾毫秒，若用嚴格 `elapsed >= interval` 判斷，這點抖動就會 skip 掉刷新、讓真實節奏
   悄悄砍半成「每兩輪一次」（只有事件時間戳的空隙看得出來）。因此保留一個遠大於
   抖動、又遠小於 interval 的 slack（0.5s）——只會讓刷新稍微提早，永遠不會推遲過
   deadline（v7 新增，2026-07-13）。
   **tick 間隔是 manager 的建構參數，不是註解裡的假設**（v7 新增，2026-07-13）：
   manager 只在 owner 呼叫 tick() 時刷新，所以真正決定「刷新最晚會多晚到」的是
   **呼叫方兩次 tick() 之間的最壞牆鐘時間**，不是設定的 interval。因此
   `KillSwitchManager.__init__` 收 `max_tick_gap_seconds` 並在建構期強制
   `refresh_interval_seconds + max_tick_gap_seconds < schedule_cancel_seconds`
   （兩次刷新之間的最壞間隔必須嚴格小於 deadline；剛好等於是 race 不是 margin）。
   §18.1 的 `schedule_cancel >= 2 × refresh_interval` config guard 只是「呼叫方剛好
   以 interval 為週期 tick」的特例，它根本看不到呼叫方——例如 `schedule_cancel=60 /
   refresh=30` 能通過 config guard，但配上 60 秒的 tick 間隔就會留下 90 秒空窗、讓
   dead man's switch 在正常運行中觸發並掃掉全錢包掛單。

   **更新（2026-08-01，PR 6 round-13）：`network_timeout_s` 已是硬不變量的一項。**
   `kill_switch_timing_violation` 現在編列五項——`refresh_interval` ＋ `max_tick_gap`
   ＋ 失敗那次自己燒掉的 `network_timeout_s` ＋ `min(timeout, backoff_cap)` ＋
   **第二個** `max_tick_gap`（backoff 之後的 retry 也要再等一個 tick）。實務後果：
   `network_timeout_s` 的預設 30 在 live 下不合法（啟動具名 exit 1），live 需 < 15，
   RUNBOOK-live §1.5 用 8。以下保留為歷史脈絡，說明它當初為何只是 advisory。

   **（歷史）已知殘餘風險：`network_timeout_s` 沒有被掃進這張時序帳**（PR 5 註記，
   2026-07-22 拍板「軟性緩解」）：當時的不變量只綁 `refresh_interval` 與
   `max_tick_gap`，但 tick 內每一筆 REST 呼叫真正的阻塞上限是頂層
   `network_timeout_s`（預設 30s），且一個 tick 會連續打多筆——網路降級（慢但
   沒斷）時，單 tick 牆鐘時間可以拖過 `max_tick_gap` 的建構期承諾，讓交易所端
   scheduleCancel 在程序還活著時觸發、掃掉含 SL/TP 的全錢包掛單（protection 於
   下個健康 tick 重建，中間是裸倉窗口）。v1 緩解：live loop 的 sleep 扣除本次
   tick 實耗（消除疊加放大），且啟動時若 `network_timeout_s × 3 >=
   max_tick_gap_seconds` **或未設（unbounded）**即印 stderr 警告
   （`network_timeout_warning`，與硬檢查 `kill_switch_timing_violation`
   併排、純函式可單測）。乘以 3 是因為最長的一條無 refresh 鏈是一次下單的 3 筆
   REST（`_MAX_UNREFRESHED_REST_CALLS`：§8.3 前置查詢→下單→重複 ack 查詢）。
   原本只編列 1 筆，於是 10s 逾時對 30s gap 被判為安全，實際上那條鏈剛好吃滿整個
   gap（2026-07-31 deadline review）。其餘阻塞路徑（protection 修復梯與其
   orderStatus 確認、reconcile 兩條 leg 與 per-order 迴圈、兩條分頁 ladder、
   日切 baseline 讀取、**決策 cycle 的 4 筆行情讀取**）已改為跨阻塞工作 refresh，
   故不再進入這筆預算。決策 cycle 那條之所以特別拆開（2026-08-01）：它本來是最長
   的一條（4 筆），照它編列會逼 `network_timeout_s` 壓到 7.5 以下，但 live 決策
   cycle **沒有 within-cycle retry**（「Live attempts are always try 1」），一筆行情
   讀取逾時就 fail-closed 並 re-anchor 到下一個 4h 邊界——等於拿「4 小時沒有決策」
   換 kill switch 餘裕，所以改成拆鏈而不是編列。
   **這個數字是「各鏈的最大值」，所以每一個接縫都必須 refresh，否則真值會變成
   各鏈的總和**——`engine.tick()` 與 `driver.pump()` 之間原本什麼都沒有，下單鏈與
   `_build_context` 鏈背靠背，真值一度是 7；接縫已補（2026-08-01 lifecycle review）。
   硬性建構期不變量與 live 專屬逾時延後網路層重做時一併處理。
   同族的 `sl_repair_retry_delay_seconds`（修復梯每次 sleep 完才 tick）也有
   姊妹 advisory（`sl_repair_delay_warning`，2026-07-22）。

   **兩個具名 fan-out 貢獻者的現況（PR 5 盤點，2026-07-22；2026-08-01 更新）：**
   (1) **決策 cycle 的 `build_input`**：`driver.pump()` 在 tick thread 上做
   `_build_context`——新建 SDK client（建構即抓 perp meta）＋ snapshot／candles／
   funding 三讀，共 4 筆連續 REST。**已解決**（2026-08-01）：`_build_context` 收一個
   optional `on_blocking_read` callback，四筆讀取之間各 refresh 一次（live 傳
   `refresh_across_blocking_work`，其餘一次性 CLI 路徑傳 None），所以這條鏈的無
   refresh 連續段是 1 筆。
   (2) **reconciler 單 pass 的呼叫數乘數**：open-orders＋clearinghouse＋fill
   backfill 分頁＋每張 terminal/absent 單的 `orderStatus` 查詢——**已解決**
   （2026-07-31）：兩條頂層 leg、兩個 per-order 迴圈與兩條分頁 ladder 全部改為
   跨阻塞工作 refresh，所以筆數再多，無 refresh 的連續段仍是 1 筆。

   **`max_tick_gap_seconds` 的語意要照字面讀：兩次 tick() 之間的最壞牆鐘時間，不是
   sleep 間隔**（v7 新增，2026-07-13）。既有 `_paper_loop`（cli.py）的一輪是
   `engine.tick()` → `scheduler.poll()` → `sleep(min(delay, 60))` **同步**執行，而
   `poll()` 會跑完整的多 agent AI 決策——**數分鐘**，不是數秒。若 PR 5 傳入 sleep 上限
   （60s）卻讓一輪 block 三分鐘，這道檢查會放行，然後在決策途中被交易所掃單。
   **PR 5 的實作用另一個方向滿足這個承諾（Option A，live/decision.py）：AI 決策整個移到
   背景 worker thread，live 迴圈的一輪永不 block 在決策上，因此 loop 頂端每輪 tick()
   的刷新就是真實 cadence——不存在、也不需要 decision-cycle 內部的 refresher**
   （2026-07-22 文字更正：原「必須在決策 cycle 內部刷新」是 PR 5 實作前的設計指引）；
   傳進來的仍必須是真實的最壞 tick 間隔（單筆 REST 呼叫拖滿 timeout 仍可拉長 gap——
   見上方 v1 軟性緩解與 `network_timeout_warning`）。
   **刷新（與 shutdown）必須先偵測「switch 是否已經觸發過了」**（v7 新增，
   2026-07-13）：`max_tick_gap_seconds` 是呼叫方在建構期做出的**承諾**，而這是唯一
   會去查核它有沒有兌現的地方。若距離上次成功排程已超過 `schedule_cancel_seconds`，
   交易所**已經**把該錢包的掛單全部取消了；此時若 refresh 只是若無其事地重新排程並
   重開 §4.1 gate，引擎就會對著一本它以為還在、其實已被清空的簿子繼續下單，而唯一的
   線索只有事件時間戳上的一個空隙。因此偵測到逾期時：latch `stop_new_orders`（與刷新
   失敗同一條 sticky 規則，只能由 §13.4 reconciliation 解除）、關閉 §4.1 gate、記
   `kill_switch_cancel_triggered` 事件（§18.5 詞彙既有成員；PR2 現在寫 9 之 8）、
   log ERROR。仍會重新排程（往後的保護要接上），但 latch 讓新單進不來；本地 order
   rows 在 reconciliation 之前是 stale 的（still 'open'）。
   **報一次、且以「排程紀元」為鍵**：latch 也會被刷新失敗拉起，而「連續刷新失敗」正是
   讓 deadline 逾期最可能的原因（API 斷線跨過 deadline），所以若以 latch 為去重鍵，
   最可能的真實觸發反而會靜默無聲。以 `_last_scheduled_at` 為鍵則每個逾期的 deadline
   恰好報一次。shutdown 也要做同一個偵測——它是呼叫方停止 tick 之後唯一還會跑的方法，
   而逾期後 `open_orders()` 會回空集合，sweep 會「乾淨」收場並 disarm，產生一份「什麼
   都不用取消的完美關機」假象。
3. 若刷新失敗，必須進入 safe mode。解除 safe mode 只有一個入口：
   `release_safe_mode()`（§13.4 呼叫），它必須**同時**清掉 sticky latch 與重開
   §4.1 gate——只清 latch 會讓 gate 一直關到下次成功刷新，只開 gate 會被下次
   refresh 依 latch 重算而還原。兩個狀態要一起動，所以只開一道門、不留兩個旋鈕。
   而且解除必須是**掙來的、不是宣告的**：能呼叫它的唯一狀態就是「上次刷新失敗」，
   此時交易所端 deadline 已經過期或即將觸發，直接重開 gate 等於放新單去對撞一個
   正要取消它們的 switch。因此實作是「落下 latch → 立刻 refresh」：刷新成功才重開
   §4.1（refresh 本來就依 latch 重算 gate），刷新失敗則自動重新上鎖、回到原狀並回傳
   False。附帶效果是 §18.5 只在真的送出 scheduleCancel 時才寫 `kill_switch_refreshed`
   ——PR 6 的 acceptance metrics 靠這個事件推算 deadline，不能有「沒刷新卻記了刷新」
   的假事件（v7 新增，2026-07-13）。
4. Process crash 時，交易所在 deadline 後自動取消**該錢包全部** open orders
   （scheduleCancel 是全錢包觸發，無法只限 bot-owned；crash backstop 接受此代價，
   v4 修訂措辭）。
5. 正常 shutdown 時，應取消 bot-owned open orders。shutdown 的第一步即關閉
   §4.1 gate（kill_switch_active 落下）——sweep 不得與新單競速；shutdown 開始後
   tick / refresh 一律拒絕（不論 disarm 成敗），邊界由 manager 自我封鎖、不依賴
   呼叫方自律（v5 新增，2026-07-13）；arm 同受此封鎖——重新 arm 會重開剛關閉的
   gate（v6 新增，2026-07-13）。shutdown **完成後**冪等：sweep 跑完並寫下 completed
   事件之後，再呼叫直接 return——signal handler 加 `finally` 兩路都會走到 shutdown
   是很現實的接線，重跑會對已取消的訂單再送一次 cancel、把交易所回的「unknown
   order」記成新的 failures，並在 §18.5 審計留下第二組 started/completed 事件；反過來
   raise 則會讓良性的重複呼叫在 teardown 期炸掉、蓋掉真正的關閉原因。
   但**中途失敗的 shutdown 必須可重試**：抑制條件是「已完成」而非「已開始」——
   started 事件的寫入刻意不設防（審計遺失必須 fail loud），DB 被鎖住就會在 manager
   已自我封鎖之後把 shutdown 炸掉；若以「已開始」抑制，那個 signal handler + finally
   的組合就會吞掉唯一一次真正取消我方掛單的呼叫，讓單子留在場上等全錢包 trigger 掃
   ——連帶掃掉 §19.3 明令不得碰的非 bot 訂單（v7 新增，2026-07-13）。
6. 正常 shutdown 的 cancel sweep 完全乾淨（open orders 枚舉成功且零 cancel 失敗）時，
   必須解除 scheduleCancel（unset），避免全錢包觸發掃掉 sweep 依 §19.3 刻意跳過的
   非 bot 訂單；sweep 有任何失敗則維持武裝，作為殘單的 backstop（v4 新增，2026-07-12）。
   **wire fact 一旦落地即 outrank 後續 sweep**（v7 新增，2026-07-13）：disarm 的判準是
   「這次 sweep 乾淨」**或**「先前某次 attempt 已實際送出 unset」（`clear_scheduled_cancel()`
   回來的當下就 latch，先於任何可能 raise 的動作）。理由：unset 成功、但它的 audit write
   撞上鎖住的 DB 而 unwind 時，重試若又以「新的一次 sweep」重新裁決，只要這次 open_orders
   枚舉失敗就會跳過 disarm 區塊、警告「left ARMED，觸發器將在 deadline 引爆」——而交易所
   其實早就忘了那個觸發器，這句話是假的。因此重試不得重送 unset、不得因新 sweep 不乾淨而
   謊報 still-armed；`kill_switch_disarmed` 的 detail 也要據實寫明是哪一次 attempt 掙來的
   （「clean shutdown sweep」vs「先前 attempt 已清除」），否則審計會出現兩列互相矛盾的紀錄。
   「乾淨」的認定：registry 命中（確定 bot-owned）但 open orders payload 缺 coin
   無法下 cancel 的訂單計入 failures（「我們的但動不了」≠「不是我們的」），擋 disarm；
   從未 arm 過的 shutdown 沒有東西可解除——不呼叫 unset、不寫 disarmed 事件。
   **「乾淨」必須雙邊認定，不得只信一次 `open_orders()` 讀取（v8 新增，2026-07-13）**：
   Info 是最終一致的視圖，空 list 無法與「剛送出的單還沒出現在視圖裡」區分——照舊寫法
   此時 failures 為空、sweep 判為乾淨，於是在一張活單之上永久解除全錢包 backstop。這正是
   §8.3 rule 10（`has_exchange_known_cloid`）拒絕相信的那種「交易所的沉默」，只是這次賭的是
   安全網本身。因此 disarm 前必須拿本地紀錄交叉檢查：凡 SQLite 仍判為非終態
   （`LIVE_ORDER_STATUSES`）且帶 cloid 的 live orders row，若 sweep 沒有處理過它，就逐一
   `orderStatus` 問交易所——確認終態才放行；仍活著、或問不到（含 unknownOid 但本地有收據
   證據＝§8.3 rule 10 的矛盾）一律計入 failures，維持武裝。反向也要成立：本地 'submitted'
   但交易所確認 unknownOid 且無收據證據者＝那次 send 根本沒送達，沒有東西需要保護，不得
   永久擋住 disarm
   （v5 新增，2026-07-13）。形狀不明的 open orders 條目（非 dict）＝所有權不明，
   計入 failures 擋 disarm 且不得中斷 sweep；open_orders 回傳非 list 視同枚舉失敗
   （v6 新增，2026-07-13）。
7. 正常 shutdown 預設不強制平倉。
8. 持倉繼續依靠既有 SL protection。
   **附註——unclean 出場的保留豁免（2026-07-22，使用者拍板；同日擴充）**：§19.1
   啟動裁決**未通過**（或 recovery 直接拋錯）、**或 --loop 出場當下 safe mode 仍
   active**（開機裁決在長跑後已過期——中途 latch 的 manual safe mode 會讓下次開機
   裁決拒絕啟動，正是保留要防的情境），而帳戶持倉（或倉位讀不到——unknown ≠
   flat）時，rule 5 的 cancel sweep **保留** resting SL / TP（reduce-only）不
   取消，只掃其餘 bot 單；保留單計入「已處理」不算 failure，且 rule 6 的 disarm
   照做——不 disarm 的話全錢包 scheduleCancel 會在 deadline 把保留的 SL / TP 一併
   掃掉，保留就毫無意義。保留豁免同時作用於 rule 6 的本地交叉檢查：kept-role 的
   本地非終態 row 若因 open_orders 快照過舊而缺席、但 `orderStatus` **正面確認**
   仍活著，計入保留、不擋 disarm；問不到（查詢拋錯或枚舉失敗）仍照舊擋 disarm
   （fail-safe 姿態不變）。動機：裁決失敗正是修復機制（safe mode 下 SL repair、
   §13.4 自動解除）無法啟動的時候，剝掉 reduce-only 保護等於拿 unclean verdict
   換一個裸倉。保留的 SL / TP 之後無人看管（reduce-only，只會減倉），直到
   `--loop` 重跑重新接管或人工處理。通過的裁決且出場時無 safe mode（含 --loop
   正常退出）維持原語意：全部取消＋stderr 警告；--loop 出場時 safe mode 仍
   active 者除保留 SL / TP 外，exit code 亦回 4（executed-but-unclean，不得對
   supervisor 報 0——與一次性路徑裁決發現 safe mode 時的 exit 4 同一慣例）。
   shutdown 時 safe-mode 讀取**失敗**（unknown ≠ clean）視同需保留：有倉（或倉位
   讀不到）即保留 SL / TP，且 --loop 因此保留了單者同樣 exit 4——事後較幸運的第二
   次讀取不得把 exit code 講回 0；讀取失敗但已確認 flat（無單被保留）者維持
   state-driven exit，不對 supervisor 誤報。警語文字須誠實區分「safe mode 確認
   active」與「讀取失敗（unknown）」兩種情況；disarm 被擋（trigger 仍 armed）而又有
   保留單時，必須明說保留的 SL / TP 也會在 scheduleCancel deadline 被掃掉。
9. **Lease 被接管的 process 不執行 shutdown sweep（PR 5 修訂，2026-07-21）**：
   `--loop` 的 lease heartbeat 拋出 `RunLockError`（此 pid 已被較新 process 取代）
   時，繼任 process 已擁有該 run 的 store、resting orders（含 SL/TP）與全錢包
   dead-man's switch——舊 process 的任何 exchange 動作或 store 寫入都是對繼任者的
   破壞（sweep 會撤掉**繼任者的**保護單、讓真倉裸奔）。因此以 exit 1 直接退出，
   只做 pid-guarded 的 lock release（繼任者持有 lease 時為 no-op）——與 paper loop
   的 RunLockError 出口同一契約。
10. **Shutdown 搶救未收割的 AI 決策（PR 5 修訂，2026-07-21，使用者拍板）**：
    Ctrl-C/SIGTERM 時 `worker.join(5s)` 之後再 poll 一次，若背景決策已完成、把
    raw response 寫入 `pending_raw_response`——重啟後 `resume_startup` 從 stored
    text 續 gate（§3.1，絕不重問 AI），不必把付費的 LLM call 燒掉並空等最多 4h。
    全程 contained：poll 或寫入失敗只記 log，shutdown 照常進行、該 cycle 重啟時
    照舊 fail closed。

### 18.3 Emergency Kill Switch Triggers

任一條件觸發時，系統必須進入 FAIL_SAFE_HOLD 或 safe mode：

```
連續向交易所下單失敗
API 回報認證錯誤
WebSocket / REST 皆無法取得必要 account state
網路連線中斷且重連失敗超過設定時間
偵測到未知的單邊持倉
偵測到非預期的保證金異常下降
偵測到 exchange/local position mismatch
偵測到 live position without valid SL
kill switch refresh failed
```

### 18.4 Emergency Actions

1. Cancel bot-owned open orders
2. Stop creating new entry / rebalance orders
3. Continue account / position monitoring
4. Continue SL / TP repair if possible
5. Optional: reduce-only emergency close（aggressive IOC，見 §9.4，不做切片）
6. Record kill_switch_event

### 18.5 Kill Switch Events

必須記錄：

```
kill_switch_armed
kill_switch_refreshed
kill_switch_refresh_failed
kill_switch_cancel_triggered
kill_switch_disarmed
kill_switch_disarm_failed
emergency_kill_switch_triggered
shutdown_cancel_orders_started
shutdown_cancel_orders_completed
```

（`kill_switch_disarmed` / `kill_switch_disarm_failed` 隨 §18.2 規則 6 的
clean-shutdown 解除行為新增，v4。）

## 19. Startup / Restart Recovery

Phase 3 啟動時不得直接開始新 decision 或新 order。

### 19.1 Startup Sequence

```
 1. Load config and validate live mode（含 max_notional_usdc <= absolute_notional_ceiling 檢查，見 §5 規則 5）
 2. Initialize SQLite
 3. Verify agent key authorization（§6.1，失敗拒絕啟動）
 4. Initialize exchange client
 5. Arm kill switch
 6. Read exchange open orders
 7. Read exchange account state
 8. Read exchange positions
 9. Read recent exchange fills
10. Read recent funding events
11. Reconcile SQLite state against exchange state
12. Cancel stale bot-owned orders
13. Check active position protection
14. Repair missing SL if needed
15. Enter safe mode if reconciliation fails
16. Only after reconciliation passes, allow new AI cycle
```

### 19.2 Existing Live Position on Startup

If exchange position exists on startup:

```
position has valid SL     → allow normal operation after reconciliation
position has no valid SL  → repair immediately
repair failed             → emergency close
```

### 19.3 Existing Open Orders on Startup

| Order type | Behavior |
|---|---|
| bot-owned stale entry / rebalance order | cancel |
| bot-owned stale close order | inspect before cancel |
| bot-owned SL / TP | validate quantity and trigger（sweep 逐單只驗結構——reduce-only／平倉方向／SL trigger；quantity 覆蓋由 reconciliation 的 SL leg 聚合驗證，分腿 SL 合計） |
| non-bot-owned order | manual safe mode |
| unknown order | manual safe mode |

Bot-owned order 判定方式：

交易所回傳的 order 資料中，cloid 欄位是 cloid_hex（雜湊值），不會保留人類
可讀的 prefix，因此不能用「檢查 cloid 開頭字串」來判斷是否為 bot-owned。改為：

1. 讀取交易所 order 的 cloid_hex
2. 用 cloid_hex 查詢 SQLite 的 cloid_registry 表
3. 查得到對應的 cloid_logical → 判定為 bot-owned
4. 查不到 → 判定為 non-bot-owned，進 manual safe mode

## 20. Testnet Live Mode

testnet_live 是 Phase 3 第一個真下單模式。

### 20.1 Testnet Live Requirements

```yaml
mode: testnet_live
network: testnet
allow_real_orders: true
leverage: 1
margin_mode: cross
single_symbol_only: true
execution_style: sliced_twap
plan_duration_minutes: 60
```

### 20.2 Testnet Smoke Tests

進入完整 testnet cycles 前，必須先通過：

```
 1. signed client initialization test（含 §6.1 agent 授權驗證）
 2. updateLeverage smoke test
 3. slice order submit test（IOC 限價 + cloid）
 4. slice order status check test（orderStatus by cloid）
 5. slice plan cancel test（撤銷未完成 plan）
 6. small entry / multi-slice fill test
 7. reduce-only close test
 8. SL create test
 9. SL modify test
10. SL cancel test
11. TP create test
12. TP modify test
13. TP cancel test
14. scheduleCancel arm / refresh test
15. restart reconciliation test
16. startup with existing position test
17. startup with stale bot-owned order test
```

所有 smoke tests 必須通過，才允許進入 testnet_live 30 cycles。

### 20.3 Testnet Live Acceptance

```
testnet_live_cycles >= 30
live_order_count >= 30
exchange_fill_dedupe_error_count = 0
orphan_exchange_order_count = 0
duplicate_fill_apply_count = 0
local_exchange_position_mismatch_count = 0
account_replay_mismatch_count = 0
unprotected_position_seconds = 0
kill_switch_refresh_success_rate >= 99%
restart_reconciliation_passed = true
emergency_close_test_passed = true
startup_with_existing_position_test_passed = true
startup_with_stale_open_order_test_passed = true
```

## 21. Mainnet Tiny Mode

mainnet_tiny 僅在 testnet smoke tests 與 testnet live 驗收通過後啟用。

### 21.1 Mainnet Tiny Requirements

```yaml
mode: mainnet_tiny
network: mainnet
allow_real_orders: true

risk:
  leverage: 1
  margin_mode: cross
  max_target_margin_pct: 60
  max_notional_usdc: 100
  absolute_notional_ceiling: 500
  single_symbol_only: true

execution:
  execution_style: sliced_twap
  plan_duration_minutes: 60
  max_slippage_pct: 0.005
```

### 21.2 Mainnet Tiny Restrictions

1. 只允許單一 symbol。
2. 不允許 leverage > 1。
3. 不允許管理 non-bot-owned orders。
4. 不允許多策略同時跑同一帳戶。
5. 不允許沒有 SL 的 position。
6. 不允許 reconciliation mismatch 時開新倉。
7. 不允許 kill switch inactive 時開新倉。
8. 不允許 unresolved fee / funding mismatch 長期存在。
9. 不允許 active slice plan 未 terminal 時建立新 entry / rebalance plan。
10. 不允許自動放大資金。

### 21.3 Mainnet Tiny Entry Criteria

Before enabling mainnet_tiny：

```
Phase 2 entry criteria passed
testnet smoke tests passed
testnet_live_cycles >= 30
emergency close tested
kill switch tested
SL creation failure path tested
restart reconciliation tested
max_target_margin_pct = 60
max_notional_usdc = 100
leverage = 1
single_symbol_only = true
```

### 21.4 Mainnet Tiny Acceptance

```
mainnet_tiny_cycles >= 30
no unprotected live position
no orphan bot-owned exchange order
no duplicate exchange fill applied
no unresolved reconciliation mismatch
no emergency close caused by bot bug
daily loss cap not breached
manual shutdown / restart tested
```

## 22. Mainnet Live Mode

mainnet_live 保留為 future mode，但 Phase 3 第一版不啟用。

規則：

```
mainnet_live is defined but not implemented in Phase 3.
mainnet_tiny passing acceptance does not automatically enable mainnet_live.
Capital scaling is manual only.
```

正式進入 mainnet_live 前，需另開 Phase 4 或獨立 spec。

## 23. Build Order（v3 修訂：6 個 PR）

| PR | 內容 | 對應章節 |
|---|---|---|
| **PR 1** | live config gates（mode / live: / allow_real_orders / absolute_notional_ceiling 檢查）＋ agent key 環境變數與啟動授權驗證 ＋ signed exchange client wrapper（尚不下單） | §3–§6 |
| **PR 2** | schema migration v6（新欄位 + 7 張新表 + scheduler_state 欄位）＋ cloid_logical / cloid_hex 推導與 cloid_registry ＋ order / cancel / cancelByCloid / orderStatus ＋ live order persistence ＋ scheduleCancel kill switch | §7、§8、§16、§18 |
| **PR 3** | WebSocket user fills ingestion（queue + tick 消化）＋ REST fill backfill（分頁、gap 起點由呼叫方給）＋ fill 去重（tid）與 accounting transaction（含 out-of-order 重折）＋ **fee** pending 與 adjustment events ＋ live accounting replay | §11、§14、§15.1 |
| **PR 4** | live account / position reconciliation（含 cloid_registry lookup）＋ startup reconciliation ＋ safe mode 狀態機與 CLI safe-mode 子命令 | §12、§13、§19 |
| **PR 5** | live SL / TP create / modify / cancel（protection manager）＋ 自管切片執行引擎（live/，含 flip 兩腿）＋ daily / consecutive loss guards | §9、§10、§17 |
| **PR 6** | testnet smoke tests ＋ live 驗收指標與 validate 擴充 ＋ live RUNBOOK | §20、§21 |

PR 全數合併後：Run testnet_live 30 cycles → enable mainnet_tiny behind hard
config gate → run mainnet_tiny 30 cycles。

## 24. Setup and Run

**（PR 1 修訂）**mode / network / allow_real_orders / symbol 一律由 YAML 的
`live:` 區塊提供（見 §4），CLI 不提供對應 flags——長駐（systemd）部署下
flag 無法跨重啟存續，而 `--allow-real-orders` 若寫死在 unit file 裡也就
失去「每次都要明確重打」的安全價值；改由嚴格驗證的 config 檔承擔這個
gate，與本模組其餘 config 的風格一致。

`live` 子命令的啟動 gate 檢查行為（PR 1 修訂）：

- config / env 問題 fail-fast（沒有它們其餘檢查無從談起）；client 建好之後
  的三個網路 gate（§6.1 授權、帳戶讀取＋§5 caps、signed client health
  check）**全部跑完、一次回報所有失敗**再 exit 1，operator 不必一次修一個。
- stdout 為機器可讀契約：`mode:` / `network:` / `allow_real_orders:` /
  `agent_address:` / `authorization_valid_until:`（keyless 時省略後兩項）/
  `account_equity:` / `pct_cap_notional:` / `effective_notional_cap:`；
  人讀訊息與警告一律走 stderr。
- 頂層 `network:` 與 `live.network` 不一致是合法的（同一份 config 讓 paper
  讀 mainnet 行情、live 在 testnet 演練），但會印 stderr 警告說明 live 用
  的是哪一個。
- **live 沿用頂層 `network_timeout_s` 與 `wallet_address`**（PR 1 定案）：
  `live:` 區塊只自帶 `network`；逾時與主錢包地址與 paper 共用同一頂層
  key（wallet 本來就必須同一顆——授權對象是主錢包；timeout 共用一個
  resolution seam）。注意：調整頂層 timeout 會同時影響 live 簽名路徑。
- **`live:` 區塊在 config load 即深度驗證**（PR 1 定案）：只要 config 裡
  有 `live:` 區塊，任何載入 config 的子命令（含 paper）都會在啟動時跑完
  整個 `LiveConfig` 驗證、要求 `risk:` 區塊明寫、並跑 risk↔live 交叉一致
  檢查——staged 的壞 live: 組合不得陪 paper 跑到切換 live 那一刻才爆。
- **明寫 `risk:` 區塊——到欄位層級**（PR 1 定案）：risk↔live 交叉檢查的
  前提是「兩塊都是操作者寫的」；config 有 `live:` 區塊而沒有 `risk:` 區塊
  → 具名 exit 1。且區塊存在還不夠：三個被交叉檢查的欄位（`leverage`、
  `margin_mode`、`max_target_margin_pct`）缺寫（或寫 null）會被 from_dict
  用「恰好等於 live.safety 預設」的預設值補上，讓交叉檢查空洞通過——所以
  這三個欄位也必須明寫，缺任一個 → 具名 exit 1，不拿預設值充數（純 paper
  config——沒有 live: 區塊——不受影響）。live 子命令另有同款 standalone
  檢查作為縱深防禦。

### 24.1 Testnet Live

```bash
export OPENROUTER_API_KEY=sk-or-...
export HYPERLIQUID_AGENT_KEY_TESTNET=...

# configs/hyperliquid.local.yaml 的 live: 區塊：
#   mode: testnet_live / network: testnet / allow_real_orders: true / safety.allowed_symbols: [BTC]
python -m contrib.hyperliquid_perp live --config configs/hyperliquid.local.yaml
```

### 24.2 Mainnet Tiny

```bash
export OPENROUTER_API_KEY=sk-or-...
export HYPERLIQUID_AGENT_KEY_MAINNET=...

# live: 區塊改為 mode: mainnet_tiny / network: mainnet
python -m contrib.hyperliquid_perp live --config configs/hyperliquid.local.yaml
```

mainnet_tiny 必須通過 hard config gate，否則拒絕啟動。（PR 1 修訂）此 gate
在 config load 由程式強制：`mode: mainnet_tiny` 時 `max_notional_usdc <= 100`
且 `max_target_margin_pct <= 60`（§21.1 的定義值；更緊可以，更鬆拒絕），
配合全域的 `leverage = 1` 與 `single_symbol_only = true` 硬檢查。

## 25. Out of Scope

Phase 3 第一版不處理：

1. Shadow live
2. Preflight-only
3. **Native TWAP（twapOrder / twapCancel）——v3 修訂，理由見 §9.5**
4. 多 symbol portfolio execution（PR 1 修訂：`single_symbol_only: false` 在
   config load 具名拒絕，與 leverage>1 / isolated 同等硬處理）
5. 多帳戶管理
6. 槓桿 > 1
7. Isolated margin
8. 自動管理 non-bot-owned orders
9. 複雜 grid / scale orders
10. Profitability optimization
11. Confidence-aware sizing
12. Strategy prompt optimization（含 analyst 即時 HL tool、prompt 原生 funding 序列）
13. Historical backtesting
14. Fully autonomous capital scaling
15. Mainnet live rollout

## 26. Summary

Phase 3 的重點不是「讓 AI 開始賺錢」，而是讓系統安全地接上真實交易所。

正式進入 mainnet tiny 之前，必須先完成：

```
Phase 2 paper validation passed
testnet smoke tests passed
testnet_live >= 30 cycles
sliced TWAP tested
kill switch tested
restart reconciliation tested
SL / TP protection tested
fill dedupe tested
accounting replay tested
```

只有在 testnet live 穩定通過後，才允許進入 mainnet tiny-capital 驗證。

mainnet_tiny 通過後，不會自動升級到 mainnet_live，也不會自動放大資金。
所有資金上限與 live mode 切換都必須由使用者手動修改 config。

---

*v3 修訂紀錄（2026-07-11）：(1) §9 全章改寫——native TWAP 改為自管切片 TWAP
（30s 切片、IOC ±0.5%、每張 cloid），查證依據見 §9.5，§1 決策 #3/#18/#19、
§7、§20.2、§25 同步；(2) §6 env var 拆分兩網路 ＋ §6.1 啟動授權驗證；
(3) §10.3–10.5 補 max_daily_loss_pct / max_consecutive_loss_count /
max_open_orders 行為定義；(4) §13.6 safe mode 持久化與 CLI 解除介面；
(5) §15 帳務單一基準明文；(6) §11.4 WS queue + tick 併發模型；(7) §2.1 平行
live/ 架構原則；(8) §16 註記 client_order_id 保留閒置、新增 §16.6
scheduler_state 欄位；(9) §23 build order 改為 6-PR。v2 修訂紀錄（承前）：
§9.4 execution style scope、§5 規則 5 absolute_notional_ceiling、§8.2 cloid
兩層與 §19.3 SQLite lookup。*
