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
| `domains/perp/risk_gate.py` | ✅ | `max_target_margin_pct` clamp · step / `min_confidence` 檢查 · effective leverage（phase2-spec §2）。 |
| `domains/perp/config_coercion.py` | ✅ | 共用 YAML-coercion helpers（`config_overrides` · `decimal_from_yaml` · `int_from_yaml`），供 `RiskConfig` / `DecisionConfig` / `PaperTradingConfig` 三方使用。 |
| `domains/perp/margin.py` | ✅ | Hyperliquid maintenance-margin tier model（rate · continuity deduction · tier 選擇；phase2-execution §6.6.1）。 |
| `persistence/` | ✅ | SQLite source of truth · transaction · migrations · dedup 去重鍵 · typed repository（phase2-data §1／§5–§12）。 |
| `persistence/export.py` | ✅ | 每 cycle／shutdown／手動的 per-run 全量 CSV export · 欄位依 phase2-data §5–§12 · `.tmp` → atomic replace · 末尾 `manifest.json` 整組一致性標記 · `export_failed` 不回滾交易 state（phase2-data §1.1）。 |
| `paper/accounting.py` | ✅ | fills · fees（taker 0.045%）· funding exactly-once · account 公式 · accounting replay（phase2-execution §6）。 |
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
| schema v6（`persistence/schema.py`）＋ `persistence/cloid.py` ＋ `live/order_gate.py` ＋ `live/orders.py` ＋ `live/kill_switch.py` ＋ signed client 下單／取消／orderStatus／scheduleCancel ✅ | PR 2 | migration v6（§16 欄位＋七張 live 內部表＋`fills.exchange_fill_key` UNIQUE）、cloid 兩層推導與 cloid_registry（§8.2／§19.3）、§4.1 real order gate（建構時綁定 signed client）、§8.3 冪等送單（送單前 intent 落表、duplicate／unknown-outcome 一律先查 orderStatus、絕不盲目重送）、§18 dead man's switch（armed／refreshed／refresh_failed／shutdown cancel bot-owned／clean-sweep disarm（disarmed／disarm_failed），事件全落 `kill_switch_events`）（§7–§8、§16、§18）。 |
| WS／REST fill ingestion ＋ 去重 ＋ 帳務 | PR 3 | queue＋tick 消化、exchange fee／funding 單一基準（§11、§14–§15）。 |
| reconciliation ＋ safe mode | PR 4 | startup／heartbeat 對帳、safe mode 狀態機與 CLI 解除（§12–§13、§19）。 |
| `live/` 切片執行引擎 ＋ SL／TP protection | PR 5 | 自管切片 TWAP、reduce-only trigger orders、loss guards（§9–§10、§17）。 |
| smoke tests ＋ 驗收 | PR 6 | testnet smoke tests、live 驗收指標、live RUNBOOK（§20–§21）。 |
