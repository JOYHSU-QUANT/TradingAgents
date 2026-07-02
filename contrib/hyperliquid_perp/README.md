# hyperliquid_perp

把 TradingAgents loop 接上 [Hyperliquid](https://hyperliquid.gitbook.io/hyperliquid-docs)
永續合約交易所的 `contrib/` 模組。Agents 針對市場 context 推理並輸出**交易意圖**
（`PerpTradeDecision`），而不是直接下單；確定性的 risk gate 與執行層再把意圖轉成
實際的交易所動作。

> 狀態：Phase 1 可運行——市場 context + 未修改的引擎 + `PerpTradeDecision`
> audit log 已能端到端執行（見 [SETUP](./docs/SETUP.md)）；
> Phase 2（paper trading）設計已定稿——見 [phase2-spec](./docs/phase2-spec.md)——
> 尚未開始實作。

## 文件

完整設計在 [`docs/`](./docs/README.md)：

- [docs/README](./docs/README.md) — 總覽、架構、專案結構、roadmap。
- [SETUP](./docs/SETUP.md) — 安裝與執行 runbook（install、設定、執行、輸出、troubleshooting）。
- [DESIGN](./docs/DESIGN.md) — Hyperliquid API 與交易規則參考 + 決策契約（Phase 2 structured target 與 Phase 1 legacy）。
- [INTEGRATION](./docs/INTEGRATION.md) — 子類別 override 點、`PortfolioDecision → PerpTradeDecision` 映射與模型分工。
- [phase1-spec](./docs/phase1-spec.md) — Phase 1 decisions, config schema, secrets, setup & run, build order.
- [phase2-spec](./docs/phase2-spec.md) — Phase 2 目標、風控參數、排程、驗收標準與建置順序。
- [phase2-execution](./docs/phase2-execution.md) — Phase 2 執行與模擬設計（TWAP / flip、SL/TP、paper 成交模型、公式）。
- [phase2-data](./docs/phase2-data.md) — Phase 2 SQLite / CSV 資料 schema。

## 安全

`configs/*.local.yaml` 只保存公開 wallet address 與 network，且已被 gitignore——
永遠不要 commit 它。**Secrets（API keys、Phase 3 的 agent-wallet private key）
一律放環境變數，絕不放進任何 yaml。**
