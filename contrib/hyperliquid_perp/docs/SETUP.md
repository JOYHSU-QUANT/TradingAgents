# Setup & run（Phase 1）

實用 runbook：怎麼安裝、設定、執行。想知道**為什麼**這樣設計，見
[phase1-spec](./phase1-spec.md)；本頁只有「照做」的步驟。

所有指令都從 repo 根目錄 `TradingAgents/` 執行。

---

## 1. 需求

- Python 3.10+
- Hyperliquid 公開市場資料不需要 key；讀取帳戶／倉位需要**公開 wallet address**
  （唯讀，永遠不需要 private key）。
- `OPENROUTER_API_KEY` 只有跑完整引擎時才需要；`--context-only` 不需要。

## 2. 安裝

```bash
pip install -r requirements.txt
```

這會安裝核心引擎（`langchain-*` / `langgraph` 等）以及本模組自己的依賴：
`hyperliquid-python-sdk` 與 `PyYAML`。

驗證安裝：

```bash
python -m pytest contrib/hyperliquid_perp/tests/ -q
```

## 3. 設定

把 example 複製成 `*.local.yaml`（`.local.yaml` 已被 gitignore，**永遠不會進版控**）：

```bash
cp contrib/hyperliquid_perp/configs/hyperliquid.example.yaml \
   contrib/hyperliquid_perp/configs/hyperliquid.local.yaml
```

然後編輯 `hyperliquid.local.yaml`。主要欄位：

| 欄位 | 意義 |
|---|---|
| `network` | `mainnet`（Phase 1/2 唯讀 mainnet；testnet 保留給 Phase 3）。 |
| `network_timeout_s` | 每個 Hyperliquid request 的 HTTP timeout（秒），預設 `30`。卡住的讀取會大聲失敗，而不是讓整輪執行掛住。 |
| `wallet_address` | 唯讀 mainnet address，只用來讀倉位／margin。**留著 `0xYOUR...` 佔位符 = 視為未設定**，該輪執行會當作空倉帳戶。 |
| `coins` | Phase 1 單一標的，預設 `[BTC]`。 |
| `market_data` | `candle_interval`（4h；必須是 `1m`/`5m`/`15m`/`1h`/`4h`/`1d` 之一）／`candle_lookback`（200）／`funding_zscore_window_days`（30）。 |
| `indicators` | `rsi_14, ema_20, ema_50, atr_14, macd`，由 `context_builder` 計算。 |
| `engine` | `llm_provider: openrouter`、`deep_think_llm`、`quick_think_llm`、`selected_analysts: [market, social, news]`。 |
| `adapter` | rating→target 各 tier 數值、`deadband`、`no_direct_flip`、`allow_short`、`entry_band_pct`、`confidence`（逐 tier，皆在 `[0, 1]`）。 |

> 沒有任何 `*.local.yaml` 時，loader 會退回 `hyperliquid.example.yaml`，
> 所以 `--context-only` 不用複製任何東西就能跑。

### Secrets（decision #9：只放環境變數，絕不放任何 yaml）

| 變數 | 何時需要 |
|---|---|
| `OPENROUTER_API_KEY` | 跑完整引擎時。一把 key 涵蓋所有 OpenRouter 模型。 |
| `HYPERLIQUID_AGENT_KEY` | **只有 Phase 3**——Phase 1 不需要。 |

```bash
export OPENROUTER_API_KEY=sk-or-...
```

`*.local.yaml` 與 `.env` 都已被 gitignore。**Private key 絕不能放進任何 yaml
或任何會進版控的檔案。**

## 4. 執行

### A. 只建 context（不需要 key，開發迴圈用）

連 mainnet、讀資料、計算 indicators 與 funding z-score，然後印出
`PerpMarketContext`。不呼叫 LLM、零成本。

```bash
python -m contrib.hyperliquid_perp.main --context-only --coin BTC
```

若 `wallet_address` 是真實地址，也會印出目前倉位（或 `flat`）。

### B. 完整 Phase 1 一輪（需要 key）

建 context → 注入**未修改的** TradingAgents 引擎 → 引擎輸出 5-tier rating →
adapter 映射成 `PerpTradeDecision` → 寫 audit log → 印到 stdout。

```bash
export OPENROUTER_API_KEY=sk-or-...
python -m contrib.hyperliquid_perp.main --coin BTC
```

沒有 `OPENROUTER_API_KEY` 時會在**任何網路呼叫之前**乾淨地失敗，並提示你改用
`--context-only`。

常用 flags：

| Flag | 預設 | 意義 |
|---|---|---|
| `--coin BTC` | config `coins` 的第一個 | 要分析的標的。 |
| `--context-only` | 關 | 只建 context，跳過引擎。 |
| `--config PATH` | `*.local.yaml` → example | 改用其他 config YAML。 |

## 5. 輸出去哪裡

完整一輪會把 decision 寫成 JSON：

```
<results_dir>/perp_decisions/BTC_<YYYYmmdd_HHMMSS_fff>.json
```

`results_dir` 來自引擎的 `DEFAULT_CONFIG`（預設 `~/.tradingagents/logs`，
可用 `TRADINGAGENTS_RESULTS_DIR` 覆寫）。檔案內容（合成範例）：

```json
{
  "schema_version": 2,
  "coin": "BTC",
  "timestamp": "2026-06-27T17:45:00+00:00",
  "timestamp_ms": 1782668700000,
  "prompt_hash": "sha256:9f2a…",
  "models": {
    "provider": "openrouter",
    "deep": "anthropic/claude-sonnet-4-6",
    "quick": "deepseek/deepseek-chat"
  },
  "rating": "Buy",
  "rating_source": "explicit",
  "decision": {
    "intent": "open_long",
    "confidence": 0.8,
    "target_size_pct": 20.0,
    "entry_zone": { "low": "62685.0", "high": "63315.0" },
    "invalidation_price": "61800.0",
    "urgency": "low",
    "rationale": "…",
    "key_risks": ["…"],
    "market_regime": "trending",
    "funding_view": "neutral"
  }
}
```

`prompt_hash` 是引擎當時讀到的 perp context 文字的 sha256；配合 `models` 與
`timestamp`，任何一筆 decision 都能重建並做 post-mortem。`decision` 各欄位的
定義見 [DESIGN](./DESIGN.md)。

> Phase 1 到「寫 log + 印出 decision」為止。**不下任何單、不跑 RiskGate**——
> 那是 Phase 2+ 的事。

## 6. 測試

```bash
# 本模組全部測試（純函式——不需要 key、不連網）
python -m pytest contrib/hyperliquid_perp/tests/ -q

# Lint
python -m ruff check contrib/hyperliquid_perp/
```

## 7. Troubleshooting

| 症狀 | 解法 |
|---|---|
| `ModuleNotFoundError: langchain`（或類似） | 核心依賴沒裝 → `pip install -r requirements.txt`。 |
| `OPENROUTER_API_KEY is not set …` | 完整一輪需要 key；`export` 它，或改用 `--context-only`。 |
| 倉位永遠顯示 `flat`／讀不到帳戶 | `wallet_address` 還是 `0xYOUR...` 佔位符；填一個真實的唯讀地址。 |
| `config not found …` | 把 `hyperliquid.example.yaml` 複製成 `hyperliquid.local.yaml`。 |
| 想先便宜地跑一次驗收 | 暫時把 `engine.deep_think_llm` / `quick_think_llm` 指到便宜／免費的 OpenRouter 模型，確認 pipeline 會輸出 `PerpTradeDecision` 並寫 log，再切回來。 |
