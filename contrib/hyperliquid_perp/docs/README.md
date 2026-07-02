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
        schema.py · context_builder.py · prompt_context.py · decision.py
        │
        ▼  injected as instrument-context text (no core change)
┌──────────────────────────────────────────────────────────────────────┐
│  TradingAgents engine — UNCHANGED upstream code                        │
│  HyperliquidTradingGraph(TradingAgentsGraph)  ← contrib subclass       │
│   · overrides resolve_instrument_context() → injects perp context      │
│   · runs analysts → researchers → trader → portfolio manager           │
│   · emits PortfolioDecision: rating (Buy/Overweight/Hold/Under/Sell)    │
└──────────────────────────────────────────────────────────────────────┘
        │  final_state + signal (the 5-tier rating)
        ▼
[Ph 1/2] Decision adapter                                       (contrib)
        PortfolioDecision + PerpMarketContext + PerpPosition
        → PerpTradeDecision (intent, target_size_pct, funding_view, …)
        decision_log.py logs prompt hash · model · full decision JSON
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
        ├── risk/
        ├── execution/
        ├── audit/
        ├── notifications/
        ├── configs/
        │   ├── hyperliquid.example.yaml
        │   ├── risk.example.yaml
        │   ├── hyperliquid.local.yaml   🔒 gitignored
        │   └── risk.local.yaml          🔒 gitignored
        ├── docs/
        ├── ports.py
        ├── main.py
        ├── .gitignore
        └── README.md
```

`🔒 gitignored` 檔案只保存公開 wallet address 與 network。**Secrets（API keys、
Phase 3 的 agent-wallet private key）一律放環境變數，絕不放進任何 yaml。**
見 [phase1-spec](./phase1-spec.md#secrets--keys)。

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
| `domains/perp/decision.py` | ✅ | `PerpTradeDecision` schema——意圖，不是 order。 |
| `integration/trading_graph.py` | ✅ | `HyperliquidTradingGraph(TradingAgentsGraph)`——override `resolve_instrument_context()`，零核心修改。 |
| `integration/decision_adapter.py` | ✅ | `PortfolioDecision` → `PerpTradeDecision` rating 映射。**Phase 2 起退役刪除**，由 `domains/perp/target_decision.py` ＋ `domains/perp/risk_gate.py`（structured target 契約 ＋ RiskGate）取代。 |
| `audit/decision_log.py` | ✅ | prompt hash · model · 完整 decision JSON · timestamp。 |
| `configs/hyperliquid.example.yaml` | ✅ | 格式範例。 |
| `configs/hyperliquid.local.yaml` | 🔒 | network + wallet address（公開資訊）。 |

### Phase 2 — paper trading 驗證

設計定稿於 [phase2-spec](./phase2-spec.md)（建置順序見其 §6）；下表檔名為暫定，實作時以 spec 為準。

| 檔案 | 狀態 | 說明 |
|---|---|---|
| decision contract 遷移 | planned | structured target schema · fail-closed 驗證 · prompt 改版（DESIGN Part 2）。 |
| `risk/risk_gate.py` | planned | `max_target_margin_pct` clamp · step / `min_confidence` 檢查 · effective leverage（phase2-spec §2）。 |
| `execution/paper_engine.py` | planned | `paper_market` · TWAP / flip plan · SL/TP lifecycle · 30s market monitor（phase2-execution §1–5）。 |
| `accounting/ledger.py` | planned | fills · fees（taker 0.045%）· funding exactly-once · margin / 清算價模型（phase2-execution §6）。 |
| `persistence/db.py` | planned | SQLite source of truth · 八張 CSV atomic export（phase2-data）。 |
| `scheduler.py` | planned | 4h rolling cycle · 3-attempt retry · 重啟 reconciliation（phase2-spec §3）。 |

### Phase 3 — live execution

| 檔案 | 狀態 | 說明 |
|---|---|---|
| `exchanges/hyperliquid/execution.py` | ✅ | SDK Exchange → submit / cancel。 |
| `exchanges/hyperliquid/websocket.py` | ✅ | SDK WebSocket wrapper / callbacks。 |
| `execution/live_executor.py` | ✅ | 包裝 `HyperliquidExecution`。 |
| `execution/reconciliation.py` | ✅ | HL 實際狀態 vs 本地 `PerpState` 對帳。 |
| `notifications/telegram.py` | ✅ | 成交、清算警報、每日 PnL 通知。 |
