# Hyperliquid Perp Trading Agents — 設計文件

一個可選的 `contrib/` 模組，驅動**未修改的** TradingAgents 引擎對接
[Hyperliquid](https://hyperliquid.gitbook.io/hyperliquid-docs) 永續合約交易所。
TradingAgents 原樣運行並輸出決策（Phase 1 為 5-tier rating + thesis；Phase 2 起
為 structured target JSON）；本模組的 **adapter** 將其映射為 perp 交易意圖，再由
確定性的 risk gate + 執行層轉成實際的交易所動作。

> **整合立場（Direction 2 — plugin，零核心修改）：** `tradingagents/` 內的任何
> 檔案都不修改。Perp market context 透過*子類別化* `TradingAgentsGraph` 並
> override 既有 extension points 注入；perp 意圖在引擎跑完*之後*由 adapter 產生。

## 文件

**設計參考**（穩定——描述系統如何運作）：

| 文件 | 內容 |
|---|---|
| [DESIGN](./DESIGN.md) | 本模組的資料契約：Hyperliquid API 與交易規則參考（輸入），以及決策契約——Phase 2 structured target 與 Phase 1 legacy `PerpTradeDecision`（輸出）。 |
| [INTEGRATION](./INTEGRATION.md) | 如何驅動未修改的引擎（子類別 override 點、`PortfolioDecision → PerpTradeDecision` 映射）以及模型分工。 |
| [phase2-execution](./phase2-execution.md) | Phase 2 執行與模擬設計：TWAP / flip、SL / TP、paper 成交模型與模擬數值公式。 |
| [phase2-data](./phase2-data.md) | Phase 2 資料 schema：SQLite tables 與八張 CSV export 的欄位定義。 |

**規格**（可執行——描述要蓋什麼）：

| 文件 | 內容 |
|---|---|
| [phase1-spec](./phase1-spec.md) | Phase 1 決策記錄、config schema、secrets、setup & run、build order。 |
| [phase2-spec](./phase2-spec.md) | Phase 2 目標、風控參數、cycle 排程、第一版取捨、驗收標準、建置順序。 |
| [phase3-spec](./phase3-spec.md) | Phase 3 live execution（v3）：config gates、agent key、自管切片 TWAP、reconciliation、safe mode、kill switch、驗收標準、6-PR 建置順序。 |

**操作**（照做的步驟）：

| 文件 | 內容 |
|---|---|
| [SETUP](./SETUP.md) | 安裝、config 欄位、exit-code 契約、troubleshooting 的完整參考。 |
| [RUNBOOK](./RUNBOOK.md) | Paper trading 試跑操作手冊：前置 → smoke → 啟動／重啟 → 日常監控 → 驗收。 |

---

## 架構

Perp 資料經由子類別 override 向上流入**未修改的** TradingAgents 引擎；引擎的
決策向下流經 adapter、risk gate，進入執行層。每一層都標注它在哪個 phase 建置。
標 `contrib` 的方塊完全住在本模組內；引擎方塊是上游程式碼，我們不修改。

```
┌──────────────────────────────────────────────────────────────────────┐
│  Hyperliquid exchange                                                  │
│  REST + WebSocket — raw market data, fills, liquidation events         │
└──────────────────────────────────────────────────────────────────────┘
        │
[Ph 1]  Exchange adapter layer                                  (contrib)
        Wraps the HL SDK so nothing upstream sees HL directly.
        sdk_client.py · market_data.py · account.py · mapper.py · errors.py
        │
[Ph 1]  Perp domain layer                                       (contrib)
        HL raw response → clean schema. Builds PerpMarketContext + PerpPosition.
        schema.py · context_builder.py · prompt_context.py
        │
        ▼  injected as instrument-context text (no core change)
┌──────────────────────────────────────────────────────────────────────┐
│  TradingAgents engine — UNCHANGED upstream code                        │
│  HyperliquidTradingGraph(TradingAgentsGraph)  ← contrib subclass       │
│   · overrides resolve_instrument_context() → injects perp context      │
│   · runs analysts → researchers → trader → portfolio manager           │
│   · emits the structured TargetDecision JSON block (Ph 2 contract)     │
└──────────────────────────────────────────────────────────────────────┘
        │  final_state (raw response containing the decision JSON)
        ▼
[Ph 2]  Decision contract                                       (contrib)
        raw response → parse_target_decision (invalid output fails closed
        to maintain_current) → TargetDecision (side, margin %, confidence)
        decision_log.py logs prompt hash · model · raw response · verdict
        │
[Ph 1/2] RiskGate                                               (contrib)
        Deterministic hard gate: schema check → soft risk → hard limits →
        kill switch.
        │
        ├─ approved ─→ [Ph 2] Execution pipeline               (contrib)
        │              order_planner.py (decision → order, deterministic)
        │              paper_executor.py (fee 0.035% · slippage · SL/TP)
        │
        └─ rejected ─→ [Ph 1/2] Audit                          (contrib)
                       order_log.py (planned orders · fills · rejections ·
                       position before/after)
        │
[Ph 2/3] Reconciliation                                         (contrib)
        Before each decision: sync HL state → trusted PerpState.
        After execution: HL actual position vs planned order / fills.
        │
[Ph 3]  Phase-3 additions                                       (contrib)
        exchanges/hyperliquid/ (websocket, polling, submit/cancel)
        live_executor.py (API wallet, testnet/mainnet)
        telegram.py (fills · liquidation alerts · daily PnL)
```

**核心不變量：** `tradingagents/` 永不修改。唯一非確定性的部分是引擎——我們透過
子類別把 context 餵進去、把決策讀出來。從 adapter 往下的一切——決策映射、
sizing、SL/TP、槓桿、最終下單參數——都是確定性的，住在本模組的 adapter +
RiskGate + 執行層。

> **Phase 2 note:** 上圖與本段描述的 rating→intent 管線是 Phase 1 行為。Phase 2 起引擎改為
> 直接輸出 structured target JSON（見 [DESIGN](./DESIGN.md) Part 2），執行層為 TWAP paper
> engine + SQLite accounting（見 [phase2-execution](./phase2-execution.md) /
> [phase2-data](./phase2-data.md)），而非圖中的 `order_planner.py` / `paper_executor.py`；
> paper taker fee 為 0.045%（非圖中的 0.035%）。

---

## 專案結構

`tradingagents/` **不修改**——整個整合住在 `contrib/hyperliquid_perp/` 之下，
`integration/` 放橋接引擎的子類別與 adapter。

```
TradingAgents/
├── tradingagents/                       # UNCHANGED upstream engine
├── examples/
└── contrib/
    └── hyperliquid_perp/
        ├── exchanges/
        ├── domains/
        ├── integration/                 # bridge to the unmodified engine
        │   └── trading_graph.py         #   HyperliquidTradingGraph subclass
        ├── persistence/                 # Phase 2 SQLite source of truth
        ├── paper/                       # Phase 2 paper accounting + execution engine
        ├── live/                        # Phase 3 live execution（平行於 paper/，PR 1 起）
        ├── risk/
        ├── execution/
        ├── audit/
        ├── notifications/
        ├── configs/
        │   ├── hyperliquid.example.yaml   # network/wallet + risk:/decision:/paper_trading:/live: 區塊
        │   └── hyperliquid.local.yaml     🔒 gitignored
        ├── docs/
        ├── ports.py
        ├── main.py
        ├── .gitignore
        └── README.md
```

`🔒 gitignored` 檔案保存公開 wallet address、network，以及 Phase 3 的 `live:`
gate 區塊（mode / allow_real_orders / safety 等，見 phase3-spec §24）——都是
非機密設定。**Secrets（API keys、Phase 3 的 agent-wallet private key）一律放
環境變數，絕不放進任何 yaml。**見 [phase1-spec](./phase1-spec.md#secrets--keys)。

## 實作 phases

圖例：✅ 開源 · ⚠️ 框架開源、細節私有 · 🔒 gitignored。

### Phase 1 — 可運行、不下真單

| 檔案 | 狀態 | 說明 |
|---|---|---|
| `ports.py` | ✅ | `ExchangeMarketData` / `ExchangeAccount` 介面定義——最先寫這個。 |
| `exchanges/hyperliquid/sdk_client.py` | ✅ | 官方 SDK 初始化、testnet/mainnet 設定載入。 |
| `exchanges/hyperliquid/market_data.py` | ✅ | SDK Info → market snapshot。 |
| `exchanges/hyperliquid/account.py` | ✅ | SDK Info → account / position snapshot。 |
| `exchanges/hyperliquid/mapper.py` | ✅ | SDK 原始回應 → 內部 schema。 |
| `exchanges/hyperliquid/errors.py` | ✅ | SDK error → domain error。 |
| `domains/perp/schema.py` | ✅ | `PerpMarketContext` · `PerpPosition` · `AccountSnapshot`。 |
| `domains/perp/context_builder.py` | ✅ | `market_data + account → PerpMarketContext`；計算 indicators 與 funding z-score。 |
| `domains/perp/prompt_context.py` | ⚠️ | 結構開源；確切措辭私有（funding-rate 的表述方式是你的 alpha）。 |
| `domains/perp/decision.py` | ~~✅~~ 已刪除 | `PerpTradeDecision` schema（Phase 1 意圖決策）。**Phase 2 起退役刪除**——舊 audit 紀錄（schema_version 2）仍可讀，但寫入路徑由 `target_decision.py` 的 structured target 契約取代。 |
| `integration/trading_graph.py` | ✅ | `HyperliquidTradingGraph(TradingAgentsGraph)`——override `resolve_instrument_context()`，零核心修改。 |
| `integration/decision_adapter.py` | ~~✅~~ 已刪除 | `PortfolioDecision` → `PerpTradeDecision` rating 映射。**Phase 2 起退役刪除**，由 `domains/perp/target_decision.py` ＋ `domains/perp/risk_gate.py`（structured target 契約 ＋ RiskGate）取代。 |
| `audit/decision_log.py` | ✅ | prompt hash · model · 完整 decision JSON · timestamp。 |
| `configs/hyperliquid.example.yaml` | ✅ | 格式範例。 |
| `configs/hyperliquid.local.yaml` | 🔒 | network + wallet address（公開資訊）。 |

### Phase 2 — paper trading 驗證

設計定稿於 [phase2-spec](./phase2-spec.md)（建置順序見其 §6）；下表檔名為暫定，實作時以 spec 為準。

| 檔案 | 狀態 | 說明 |
|---|---|---|
| `domains/perp/target_decision.py` | ✅ | structured target schema · fail-closed 驗證 · prompt 改版（DESIGN Part 2）。 |
| `domains/perp/risk_gate.py` | ✅ | `max_target_margin_pct` clamp · step / `min_confidence` 檢查（同方向 resize 走更高的 `resize_min_confidence`） · effective leverage（phase2-spec §2）。 |
| `domains/perp/config_coercion.py` | ✅ | 共用 YAML-coercion helpers（`config_overrides` · `decimal_from_yaml` · `int_from_yaml`），供 `RiskConfig` / `DecisionConfig` / `PaperTradingConfig` 三方使用。 |
| `domains/perp/margin.py` | ✅ | Hyperliquid maintenance-margin tier model（rate · continuity deduction · tier 選擇；phase2-execution §6.6.1）。 |
| `persistence/` | ✅ | SQLite source of truth · transaction · migrations · dedup 去重鍵 · typed repository（phase2-data §1／§5–§12）。 |
| `persistence/export.py` | ✅ | 每 cycle／shutdown／手動的 per-run 全量 CSV export · 欄位依 phase2-data §5–§12 · `.tmp` → atomic replace · 末尾 `manifest.json` 整組一致性標記 · `export_failed` 不回滾交易 state（phase2-data §1.1）。 |
| `paper/accounting.py` | ✅ | fills · fees（taker 0.045%）· funding exactly-once · account 公式 · accounting replay（**依 run mode 分流**：paper 用模型 fee/realized；live 用交易所 closedPnl/fee、依交易所時間排序、並折算 accounting adjustments）· live fill effect（`compute_live_fill_effect` / `adjustment_ledger_delta`）（phase2-execution §6、phase3-spec §15）。 |
| `paper/liquidation.py` | ✅ | paper estimated liquidation price · margin tier bisection（phase2-execution §6.6.1）。 |
| `paper/config.py` | ✅ | typed `paper_trading:` block（phase2-execution §5.4）。 |
| `paper/engine.py`（+ `clock` / `fill_model` / `market_feed` / `stops` / `twap`） | ✅ | TWAP / flip plan · SL/TP lifecycle · paper 成交模擬 · market-data 新鮮度／pause／gap-stop · monitor tick 邏輯（外層 30s loop 屬 PR4 scheduler；phase2-execution §1–5）。 |
| `paper/scheduler.py` | ✅ | 4h rolling cycle · deterministic `decision_attempt_id` · 3-attempt retry（10s/30s，跨重啟延續）· `invalid_output` fail-closed · `ai_inputs`/`ai_outputs`/`decision_attempts` audit rows（phase2-spec §3／§3.1）。 |
| `paper/reconcile.py` | ✅ | 重啟 reconciliation 九步：canceled_restart + residual · pending funding 以 stored basis 補帳 · replay 不一致時 flat 拒絕啟動／非 flat 轉 protection-only（引擎續守 SL/TP、halt 新決策）· 取消到未完成 plan 時立即開新 cycle（phase2-execution §1.2）。 |
| `paper/validation.py` | ✅ | 驗收器：13 項 summary 指標 · orphan／snapshot／replay 鏈路檢查 · 可進 Phase 3 判定（phase2-spec §5）。 |
| `paper/run_lock.py` | ✅ | 單實例 lease（`scheduler_state` 的 pid + heartbeat）：同一 run 同時只允許一個 `paper` process，防重複啟動互相取消活單、雙倍 AI 花費。 |
| `cli.py` + `__main__.py` | ✅ | `python -m contrib.hyperliquid_perp paper / export / validate`；空 argv／旗標式呼叫原樣委派 legacy `main.py`（`--context-only` 不變），未知裸字具名報錯 exit 1；迴圈運行中 SIGTERM 與 Ctrl-C 同樣走收尾 export（啟動／reconciliation 階段收到則 exit 130、無收尾 export）。 |

### Phase 3 — live execution

規格：[phase3-spec](./phase3-spec.md)（v3）。架構原則：live 引擎為平行 `live/` 套件、
paper engine 零改動，共用 scheduler／RiskGate／persistence／純函式（phase3-spec §2.1）。

| 模組 | PR | 說明 |
|---|---|---|
| `live/config.py`＋`live/secrets.py`＋`live/authorization.py`＋`exchanges/hyperliquid/signed_client.py`＋CLI `live` 子命令 skeleton ✅ | PR 1 | typed `live:` 區塊（mode 必填、mainnet_live 拒絕、§5 ceiling 檢查、risk↔live 一致性、config load 即深度驗證、真單雙旗宣告 §6 規則 7、明寫 `risk:` 區塊到欄位層級——三個交叉檢查欄位缺寫即拒，config load 即驗）、分網路 agent key（缺 key＋`allow_real_orders: true` 拒絕啟動）、§6.1 啟動授權驗證、signed `Exchange` wrapper（PR 1 僅初始化＋健康檢查；下單方法於 PR 2 隨 §4.1 gate 一同進場）（phase3-spec §3–§6、§24）。 |
| schema v6（`persistence/schema.py`）＋ `persistence/cloid.py` ＋ `live/order_gate.py` ＋ `live/orders.py` ＋ `live/kill_switch.py` ＋ signed client 下單／取消／orderStatus／scheduleCancel ✅ | PR 2 | migration v6（§16 欄位＋七張 live 內部表＋`fills.exchange_fill_key` UNIQUE）、cloid 兩層推導與 cloid_registry（§8.2／§19.3）、§4.1 real order gate（建構時綁定 signed client；**三種粒度**（第三種為 PR 5 review-loop 增補）——`check_new_target` 是完整 §4.1 列表、引擎每個決策 cycle 問一次；`check_order` 是每張**加風險**單都要過的子集：§9.3 明文允許 active plan 期間做 SL repair／emergency close，故 risk_gate_approved／active_slice_plan／unresolved_protection_failure 三條屬決策層、不逐單套用；`check_protective_order` 再豁免 state_reconciled／manual_safe_mode 兩條 safe-mode 線，供保護／去風險單（stop_loss／take_profit／emergency_close）使用——§13.1 safe mode 期間持續保護，且修復 §12.3 SL-missing 的 SL 單本身必須送得出去，否則死鎖；kill switch 與基礎前置條件對保護單仍生效）、§8.3 冪等送單（送單前 intent 落表、拒絕／duplicate／unknown-outcome 一律先查 orderStatus、絕不盲目重送；rule-5 重送前把 orders row 蓋回 `submitted`——留著終態的 'rejected' 會讓 crash 後仍活著的單被當成已結案，進而被授權鑄新 cloid＝雙倉）、§18 dead man's switch（armed／refreshed／refresh_failed／cancel_triggered（deadline 已逾期＝交易所已把該錢包掃單）／shutdown cancel bot-owned／clean-sweep disarm（disarmed／disarm_failed），事件全落 `kill_switch_events`；呼叫方的最壞 tick 間隔以 `max_tick_gap_seconds` 於建構期強制；arm 前檢查主機與交易所的**時鐘偏移**（絕對 deadline 由本地時鐘算出，漂移會無聲改變真正的保護窗）；**disarm 需本地佐證**——不只信一次 `open_orders()`，SQLite 仍判為活的單要逐一 orderStatus 確認，枚舉失敗時只列名、不對死掉的端點重問）（§7–§8、§16、§18）。 |
| `live/ws_stream.py` ＋ `live/fills.py` ＋ `live/fill_backfill.py` ＋ 帳務／replay 擴充（`paper/accounting.py`、`persistence/`） ✅ | PR 3 | §11.4 併發模型（WS callback **只**把原始事件丟進 thread-safe queue，tick 迴圈才解析／寫 DB，SQLite 維持單一寫者）、§11.1 三條訂閱（userFills 由 PR 3 消化；orderUpdates／clearinghouse 排空後留給 PR 4）、斷線計時與 >5 分鐘 stale 旗標（§11.2 規則 7，safe mode 由 PR 4 接手）、tick 驅動重連（close 若與連線競合則丟棄該連線，不得把已死的 socket 標成健康）、每次（重）連線都要求 REST backfill（§11.2 規則 5）；**§14.2 去重鍵＝交易所 tid**（HL 兩個端點都必帶；spec 的 composite fallback 刻意不實作——它會把同一張單同毫秒、同 side／price／size 的兩筆真實 fill 撞成同一把 key 而**靜默丟掉一筆**，replay 也偵測不到，故無 tid 的 fill 一律當 malformed 記證據不入帳）、§14.3 exactly-once（fill row ＋ position ＋ ledger 同一 transaction；WS／REST／orderStatus 三來源只套用一次）；§15 帳務單一基準（realized＝交易所 closedPnl、fee＝交易所 fee，非 paper 模型）、fee pending 與**不可變＋fold** 的更正模型（回補**不覆寫**已記錄的 fill，而是寫一筆 `accounting_adjustment_events`，由 live replay 折算回來）、replay 依 run mode 分流並比對 materialized state（§11、§14–§15）。；**fill 依交易所時間 `(exchange_fill_time, exchange_fill_key)` 排序套用**（WS/REST 競速、unmapped 重新 ingest 都會亂序），亂序到達時該 symbol 的 position 從 genesis 重折一次，ledger 不需修復（三個總額都是 per-fill delta 的和，可交換）；**fee 更正累積且有序**（`adjustment_id` 帶 seq：相同金額 no-op、不同金額只 post 差額——以 (target,type) 為唯一鍵會讓 reconciliation job 永久卡在那筆 fill；唯一例外：pending fee **首次**解出恰為 0 也寫事件、ledger 移動 0，否則該 fill 永遠留在 pending backlog；**回補入口必收 `fee_token` 幣別證明**，非 USDC 拒絕——與 ingest 端 fail-safe 對稱，§15.1 規則 3）、fee pending 的判定涵蓋 fee 缺失、非 USDC、與 `feeToken` 欄位缺失（無法證明是 USDC＝payload 漂移，寧可延後入 fee 也不冒記錯幣別的險）；**REST backfill 的 gap 起點＝`stream.backfill_since()` 原樣傳入**（stream 自持 startup floor——開機 `set_startup_floor` 登記帳上最新 fill、無 fill 則 run genesis——與重連 gap anchor，回傳較早者、同一個 epoch-gated 清除一起退休，首連後立刻斷線的小 gap 才不會遮蔽還沒補的 floor）、分頁、`complete=False` 時不得清 `needs_backfill`，且清除以 epoch 為閘；**backfill 義務必須耐重啟（§11.2 rule 5 v12，實作歸 PR 4）**——義務（floor／anchor／卡住態）在 PR 3 只活在 process 記憶體，重啟後 floor 重取帳上最新 fill 的推導只在前一個 process 沒帶著未退休義務死掉時才安全（isSnapshot 已入帳、gap pass 完成前 crash，或 systemd 自動重啟抹掉 fail-loud 卡住態，都會變成永久靜默缺口），PR 4 接 daemon 接線時必須持久化 durable watermark、floor＝min(watermark, 帳上最新 fill)，PR 3 刻意不先加無 writer 的欄位；**stale 也涵蓋 half-open socket 與 flapping**（自稱連著但靜默 >2 分鐘；另有 proof-of-life 時鐘——曾有事件而距最後事件 >5 分鐘即 stale，**不因重連重置**，故 stale 具黏性、須事件才解除）；**重投遞驗證＝§15.1 rule 5 的自動偵測管道**（DUPLICATE 車道不只憑 key 丟棄：fee 與帳上「入帳時記錄值」不同→走 `backfill_fill_fee` 自動 post 差額（比較基準刻意不用 effective fee，免得 stale payload 把人工估值 flip-flop 回去）；身份欄位不同＝「同 tid 不同 fill」→記證據不套用、也不在其上補 fee；**跨 run duplicate 的 fee-only 差異→`fill_fee_drift` case row**——fee 車道對別的 run 無法 post、fee 又不在身份比對集合裡，沒有這個 recorder 交易所對已結束 run 的 fee 更正會無聲消失；比對鏡像同 run 車道淨行為（先 as-ingested、再折更正鏈的 effective fee，共用 `_fee_books_state` 單一定義），§15.1 rule 8 v11）；**unmapped／malformed／money-drift／fee-drift sighting 落 `exchange_reconciliation_events` case row**（once per fact、去重下沉在 repository 寫入邊界——evidence 檔是 write-only 證據，fill 老出所有 backfill 窗口後 DB row 是 PR 4 discovery 唯一可查詢的 backlog；**resolution 分型**：只有 fill_unmapped 以 anti-join 除帳，malformed／兩種 drift 的 key 形狀永遠 join 不到 fills、走人工核對＋`action_taken`，§12.3 v10/v11）。 |
| `live/reconcile.py` ＋ `live/safe_mode.py` ＋ `live/startup.py` ＋ `live/cancel.py` ＋ CLI `safe-mode` 子命令與 `live --run-id` 啟動恢復 ✅ | PR 4 | §12.3 全案對帳 sweep（交易所為事實來源：卡住的 'submitted' 送單以 orderStatus 定讞——rule-10 有收據證據則永不重送；orphan bot 單補寫本地 row；本地終態但交易所仍列 open 以 **orderStatus 當 tiebreaker** 才 reopen——open_orders 只是落後一拍的 cancel 不得復活 phantom row；phantom 本地倉位**永不**被 sweep 歸零，帳只由補入 exchange events 修正；equity 容差＝max(1 USDC, 1% equity) 暫定常數；invalid-local-fill 交叉檢查**分頁**、窗口證明不了覆蓋就 withhold verdict 而非發假 manual 判決）、結果落 `exchange_reconciliation_events`（once-per-fact 去重命中而本輪已解決者，處置以 `set_reconciliation_action` 蓋回既有列）＋ snapshots 的 reconciliation_status／diff（case rows 與 snapshot 寫入**分離 transaction**——snapshot 寫失敗不得回滾人最需要的 case 證據；`run()` 含記錄腿在內「records everything, raises nothing」；**snapshot 的本地視角欄位一律走 `paper.accounting` 的正典公式並 pin `DECIMAL_CONTEXT`**——`margin_ratio`＝equity/maint（非倒數）、`available_balance`＝equity−used_im（交易所原始 withdrawable 只進 `exchange_withdrawable` 欄）、`total_pnl` 帶齊 −fees ＋funding，因為 mode-agnostic 的 `validate` 會對**每一列** account_snapshots 重算這些 §6.1／§6.6 恆等式，手寫算式漂掉會讓健康的 live run 被稽核判成 corruption）；§13 safe mode 狀態機（recoverable／manual、manual 恆優先、升級保留原 `entered_at`、現態存 `scheduler_state` 三欄一體、歷史落 `safe_mode_events`（**同嚴重度**已 latch 期間出現**不同** reason 時以 `safe_mode_reason_added` 補記一筆——manual-during-manual 與 recoverable-during-recoverable 皆然，每 episode 每 reason 一次，現態欄維持第一個 reason；低嚴重度被高嚴重度吸收者不記）、重啟不消除；§13.4 自動恢復要求呼叫方**當輪**證明 clean pass＋WS restored＋kill switch healthy，且 reconciler 須**全接線**——None seam（backfiller／fetch_fills 未綁）記入 report 的 `legs_skipped`，缺腿的 clean pass 只能維持、不能解除 latch，manual 只走 §13.6 CLI `safe-mode --release --reason` 且解除後仍須過下一輪對帳；三連續 unclean pass 升級 manual）；§19.1 步驟 5–16 startup recovery（兩段對帳夾住 §19.3 分角色 stale-order 處置；**第一段看到的 manual 級證據即 latch**——外人的單／不明幣種倉位在兩段之間被撤掉也算數，§13.5 要人確認過才放行；**sweep 失敗餵進 verdict**（非事後另記）——撤不掉的單讓該 pass 不 clean，故對帳乾淨的 pass 不會先解除 latch 再重進而製造 release→enter flap 與 `entered_at` 重錨；**此 wiring 無 WS 故 `ws_restored=False`**——「沒有 WS 可恢復」不等於「WS 已恢復」，前一個 process 因 ws_disconnect 留下的 latch 不得被這個 one-shot 自動解除（與 §13.4 seam gate 同一「缺機制≠健康」立場）：entry/rebalance 撤、close 類**只在讀得到倉位且證明不再適用時**才撤（適用＝reduce-only＋平倉方向；**不比 size**——停機期間倉位被 ADL／手動縮小後的超額平倉單仍保留閉倉意圖，reduce-only 由交易所 clamp，撤掉唯一平倉單且 PR 4 無補掛路徑才是不可逆方向）——讀不到倉位＝unknown≠flat 一律保留、SL/TP 只驗結構（reduce-only＋平倉方向＋SL trigger）不撤——size 覆蓋是 reconciler SL leg 的聚合職權（分腿 SL／刻意部分的 TP 不逐單誤報）；kill switch 於各 leg 與每張撤單間 **tick**，避免恢復期間 deadline 逾期掃錢包；SL 覆蓋只計**平倉方向**的 reduce-only stop_loss）；CLI：`live --run-id` exit 0=pass 且 §18.2 shutdown sweep 乾淨／4=executed-but-unclean（verdict 不乾淨，**或** verdict pass 但 shutdown sweep 不乾淨——kill switch 仍 armed、錢包級 scheduleCancel 將於 deadline 觸發）／1=硬失敗；kill-switch timing（refresh＋此命令的 30s tick gap < schedule_cancel）於建 run row／取鎖**之前**preflight 驗證，違規具名拒絕 exit 1——config guard 看不見呼叫方 tick gap，留給建構子晚炸會落在 0/4/1 契約外且留下剛建的 run row；`--create` 對非 flat 帳戶需 `--adopt-positions` 且具名拒絕非本 run 幣種的倉位（off-coin 倉位 reconciler 每輪必標 manual unknown position，adopt 了也無法釋放——與 paper 路徑同一約定）；**resume 先驗 run 身份**（run mode 非 live 具名硬失敗、coin 不一致硬失敗、**`live.network` 不一致硬失敗**——testnet 建的 run 拿 mainnet config resume 會對 mainnet 錢包 arm kill switch 並拿 testnet 帳本對 mainnet 交易所，滿盤 mismatch 卻無一處指出真因，故與 coin 同列身份；`live:` 其餘欄位（safety caps／kill-switch 時序等）drift 與行為參數一同響亮警告，全在上鎖／arm 之前；`--adopt-positions` 對 resume 具名拒絕——它只播種新 run 的 genesis；`--create`／`--adopt-positions` 未帶 `--run-id` 亦具名拒絕——config-check 模式不建任何 run，靜默忽略會被讀成「已建立」；paper resume 同樣拒絕 live-mode run，且 paper genesis 不存 `live:` 故不受上述 live 檢查影響）；有倉位時響亮警告 §18.2 shutdown 會撤掉保護單（依 shutdown 前的 **fresh** 倉位讀取判定，讀失敗也警告——unknown≠flat）；`safe-mode --status` exit 0=無 safe mode／4=latch 中（supervisor 可據 exit code 分支；`--reason`/`--released-by` 不帶 `--release`、`--action` 不帶 `--stamp-case` 皆具名拒絕、非 live run 具名錯誤；歷史預設印最後 10 筆，`--limit N` 放寬、0=全部——長 manual episode 的 reason_added 列會把 episode 起點擠出固定尾巴；並列出**未解決的 §12.3 cases 與其 event_id**，且逐列標明真正的解決路徑）；`safe-mode --stamp-case <event_id> --action "<處置說明>"` 是 §12.3「人工核對後標記 `action_taken`」的**唯一工具**——digest-keyed 的 malformed sighting 永遠 join 不到 fills、自動車道救不了，未 stamp 又擋 verdict，沒有這個指令一筆看不懂的 payload 就會讓 run 永久卡死（只能手寫 SQL）；已記錄的處置不覆寫、stamp 後仍須過下一輪對帳才恢復交易；**`fill_unmapped` 具名拒絕 stamp**（它靠對 fills 的 anti-join 除帳、不讀 `action_taken`，stamp 它清不掉 verdict 卻會把它從清單抹掉——故 open-cases 清單對這型改用 anti-join 判準）；stamp 路徑以 run 範圍查詢定位 event（`set_reconciliation_action` 本身無 run 過濾，這是唯一屏障）；equity_mismatch 稽核列以**常數 fact key** 去重（同一未癒事實一列＋restamp，即時差額看首列 detail 與每輪 warning log）；**倉位類 case 的 fact key 帶幣別與 local→exchange 轉換**（`ETH:2.5`／`BTC:0.001->0.003`）——去重鍵是 (run_id, case_type, exchange_value) 不含 symbol 欄，裸 size 會讓 off-coin ETH 2.5 與同幣 2.5、或每一次 phantom（交易所側恆為 0）互撞而**靜默丟掉**第二筆稽核列；**未 stamp 的 `fill_malformed` 與 unmapped backlog 皆擋 clean 並在 `errors` 具名**（交易所報過、SQLite 沒入帳的錢要人 stamp `action_taken` 才放行；backfill `complete=False` 亦然）——這條 errors 通道正是 safe-mode detail 與持久化 `reconciliation_diff` 的來源，只寫 log 會讓 safe mode 亮起卻無任何操作面指出原因；startup backfill floor＝帳上最新 fill 或 run genesis（durable watermark 歸 PR 5 daemon 接線）（§12–§13、§16.5–16.6、§19）。 |
| `live/engine.py`（切片引擎）＋ `live/protection.py` ＋ `live/loss_guards.py` ＋ `live/decision.py` ＋ CLI `live --run-id --loop` daemon ✅ | PR 5 | §9 自管切片 TWAP（每 tick 最多送一片——30s 是排程節奏、停滯後 catch-up 也一 tick 一片不 burst；pre-send §4.1 gate 拒絕 **hold cursor**（事件 `slices_paused`）、gate 重開從同一片續送；已觸 wire 的交易所拒絕／ambiguous 失敗 cursor 前進不重送（§9.2 rule 2）；被 gate 擋到期的 plan 誠實 terminal `expired`（residual live v1 記 NULL＝unknown）、不記 completed；重啟時 active plan 一律 `expired`／`restart_abandoned`——切片配量無法精確恢復；flip sequential 兩腿、close leg 達 flat 後重跑 RiskGate 才開 open leg）、§17 SL/TP protection（reduce-only trigger orders、§17.4 modify-before-cancel、SL repair ladder——gate 拒絕回報 `blocked`（事件 `stop_loss_repair_blocked`）不燒修復預算、ladder delay 間 tick kill switch；§17.3 TP 失敗走 degraded protection；no-safe-SL → §9.4 aggressive IOC 急平、急平達 flat 後升級 manual safe mode）、§10 loss guards（§10.3 daily loss 含未實現→recoverable；§10.4 連虧以 settlement anchor（schema v7 `last_settlement_wallet_balance`）計段、offline 平倉段由 `settle_offline_flat` 於啟動補記→manual；§10.5 max open orders）、§18.2 Option A 併發（AI 決策跑背景 thread；live loop **10s tick**、kill switch 每 tick 刷新；decision driver fail-closed——retryable 當場 `api_failed`、不複製 paper 的 within-cycle ladder、下一 4h 再試；重啟領養 stranded `in_progress` attempt——已存 response 從 gate 續跑不重問 AI、否則 `api_failed` 收場；plan 已註冊後的 audit persist 失敗只重試 persist、絕不重 gate）、loop 全例外 fail-closed（tick／pump 例外一律收進 recoverable safe mode，loop 不得倒——teardown 會掃掉活倉的 SL/TP）；v1 缺口：真 WS 接線隨 PR 6（fill 靠 reconciler REST backfill；§13.4 v1 `ws_restored` 例外）（§9–§11、§17–§18）。 |
| smoke tests ＋ 驗收 | PR 6 | testnet smoke tests、live 驗收指標、live RUNBOOK（§20–§21）。 |
