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
| 26 | **（v3 新增）架構原則** | live 執行引擎為平行 `live/` 套件（paper engine 零改動）；WebSocket 事件經 thread-safe queue 由既有 30s tick 迴圈消化；live 帳務以記錄的交易所事件為單一基準（見 §2.1、§11.4、§15） |
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

系統只有在以下條件全部成立時，才允許送出 exchange order：

```
allow_real_orders = true
mode in {testnet_live, mainnet_tiny, mainnet_live}
agent key exists（且通過 §6 的啟動授權驗證）
startup reconciliation passed
kill switch active
symbol is allowed
risk gate approved target
current account / position state is reconciled
no unresolved protection failure
no active slice plan
no manual_safe_mode
```

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
    （`_EXCHANGE_TO_LOCAL_STATUS`，已涵蓋 Hyperliquid 文件列出的全部 29 個字）。
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
   重跑 RiskGate 才開 open leg；兩腿共用同一個 1 小時 envelope 與切片預算。

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
   `residual_qty`（與 paper 語意一致）。
3. 每張切片單有獨立 cloid（§8.2），retry（網路錯誤等）重用同一 cloid。

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

### 10.3 Daily Loss Cap（v3 新增行為定義）

`max_daily_loss_pct = 2` 的衡量方式：

1. 每日 UTC 00:00 記錄當日起始 `account_equity`（取交易所 reconciled 值）。
2. 任一時點 `(day_start_equity - current_equity) / day_start_equity > 2%`
   （**含未實現盈虧**）即觸發。
3. 觸發 → 進 **recoverable safe mode**，持倉與 SL/TP 保護照舊，
   停止新 entry / rebalance；次日 UTC 00:00 自動解除（仍須通過 §13.4 恢復條件）。
4. 觸發事件記入 `safe_mode_events`。

### 10.4 Consecutive Loss Cap（v3 新增行為定義）

`max_consecutive_loss_count = 3` 的計次定義：

1. 「一次 loss」= 一段持倉**完全平掉**（倉位歸零：正常 close、flip 的 close leg、
   SL 觸發皆算）時，該段的 realized PnL（含 fee / funding 分攤）< 0。
2. 任一段結算為獲利 → 計數歸零。
3. 連續達 3 次 → 進 **manual safe mode**（連虧暗示策略性問題，
   須人工確認後以 §13.6 介面解除）。

### 10.5 Max Open Orders（v3 新增行為定義）

1. 建立任何新 live order 前，先計數交易所端 bot-owned open orders
   （含 resting SL / TP trigger orders）。
2. 達 `max_open_orders = 5` → 拒絕建立新單、記錄
   `no_order_reason = max_open_orders`，並觸發一次 reconciliation
   （正常單一 symbol 運行不應接近此上限，接近即是異常訊號）。

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
5. 重連後必須用 REST 補查斷線期間可能錯過的 fills / orders / positions。
6. WebSocket 斷線期間禁止 new entry / add / rebalance。
7. WebSocket disconnected 超過 5 分鐘時，進入 safe mode。

### 11.3 WebSocket Failure Handling

```
ws_disconnected
→ start reconnect loop
→ disable new entry / rebalance
→ REST polling continues

ws_disconnected > 5 minutes
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

優先使用 exchange-provided unique fill id（`tid`）。若交易所沒有穩定 fill id，
使用 composite key：

```
exchange_fill_key = symbol + oid + fill_time + side + price + size
```

### 14.3 Fill Application Rules

1. 同一筆 exchange fill 最多只能套用一次。
2. Fill 寫入 SQLite、fee / PnL 過帳、position update、account update
   必須在同一個 SQLite transaction 內完成。
3. 若 transaction commit 前 crash，該 fill 不算套用完成。
4. 若 transaction 已 commit，重啟後不得再次套用。
5. REST 補查與 WebSocket 回報不得造成重複計帳。

## 15. Fee and Funding Reconciliation

Live mode 中，fee、funding 與 realized PnL 以交易所資料為準。

**（v3 新增）帳務單一基準**：live run 的帳本只有一套——記錄下來的交易所事件
（fill 含 exchange fee / closedPnl、funding、accounting adjustment events）。
accounting replay 從這些事件重建 position / account，必須與 materialized state
一致。Phase 2 的本地 fee / funding 模型**僅供 paper mode 使用**；
「real fee / funding comparison」（範圍項目 5）做成離線報表
（模型估計 vs 交易所實際），不影響帳務。

### 15.1 Fee Rules

1. 若 fill payload 直接提供 fee，立即入帳。
2. 若 fee 暫時缺失，先標記 fee_status = pending。
3. Pending fee 不得永久留空，需由 reconciliation job 回補。
4. 回補後重新計算 realized PnL / account state。
5. Fee correction 必須產生 accounting adjustment event，不得靜默覆蓋。

### 15.2 Funding Rules

1. Funding event 以 (run_id, symbol, funding_timestamp) 去重。
2. Funding 缺失時標記 funding_status = pending。
3. REST reconciliation 需補齊 missed funding。
4. Funding correction 必須產生 accounting adjustment event。

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

（`emergency_close_on_shutdown` 的行為（reduce-only emergency close，§8.1/§17）
隨 PR 5 的 protection manager 進場；在那之前 config 建構期拒絕 `true`——
未實作的行為不得被 config 靜默接受，v4 註記。）

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

### 18.2 Required Behavior

1. Process 啟動後，完成 exchange client 初始化時，必須立刻 schedule cancel。
   `arm()` 只在啟動期呼叫一次：重複 arm 是接線錯誤（具名 RuntimeError），且 arm
   必須尊重 sticky `stop_new_orders`（`kill_switch_active = not stop_new_orders`，
   與 refresh 同一條式子）——否則第二次 arm 會把刷新失敗關上的 §4.1 gate 靠運氣
   重開，而不是走 §13.4 reconciliation（v7 新增，2026-07-13）。
2. Live loop 運行期間，必須依 `refresh_interval_seconds` 刷新 schedule cancel deadline。
   `refresh_due()` 的 interval 語意是「至少這麼頻繁」而非「不得早於」：當呼叫方以與
   refresh_interval 相同的週期 tick（預期的 30s／30s 接線），tick 必然比它比較的那個
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

   **`max_tick_gap_seconds` 的語意要照字面讀：兩次 tick() 之間的最壞牆鐘時間，不是
   sleep 間隔**（v7 新增，2026-07-13）。既有 `_paper_loop`（cli.py）的一輪是
   `engine.tick()` → `scheduler.poll()` → `sleep(min(delay, 60))` **同步**執行，而
   `poll()` 會跑完整的多 agent AI 決策——**數分鐘**，不是數秒。若 PR 5 傳入 sleep 上限
   （60s）卻讓一輪 block 三分鐘，這道檢查會放行，然後在決策途中被交易所掃單。因此
   **§18.2 對 PR 5 的硬性要求：kill switch 必須在決策 cycle *內部*（或由獨立的
   refresher）刷新，不能只在 loop 頂端刷新**；傳進來的必須是真實的最壞 tick 間隔。
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
   「乾淨」的認定：registry 命中（確定 bot-owned）但 open orders payload 缺 coin
   無法下 cancel 的訂單計入 failures（「我們的但動不了」≠「不是我們的」），擋 disarm；
   從未 arm 過的 shutdown 沒有東西可解除——不呼叫 unset、不寫 disarmed 事件
   （v5 新增，2026-07-13）。形狀不明的 open orders 條目（非 dict）＝所有權不明，
   計入 failures 擋 disarm 且不得中斷 sweep；open_orders 回傳非 list 視同枚舉失敗
   （v6 新增，2026-07-13）。
7. 正常 shutdown 預設不強制平倉。
8. 持倉繼續依靠既有 SL protection。

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
| bot-owned SL / TP | validate quantity and trigger |
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
| **PR 3** | WebSocket user fills ingestion（queue + tick 消化）＋ REST fill backfill ＋ fill 去重與 accounting transaction ＋ fee / funding pending 與 adjustment events | §11、§14、§15 |
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
