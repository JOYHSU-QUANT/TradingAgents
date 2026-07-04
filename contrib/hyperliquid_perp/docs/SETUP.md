# Setup & run

實用 runbook：怎麼安裝、設定、執行。想知道**為什麼**這樣設計，見
[phase1-spec](./phase1-spec.md) 與 [phase2-spec](./phase2-spec.md)；本頁只有「照做」的步驟。

> 目前狀態：market context + 未修改的引擎 + **Phase 2 structured target 契約 +
> 確定性 RiskGate** 已能端到端執行（寫出 schema v3 的 target audit log）。實際下單
> （paper／live 執行）仍是 Phase 3+。

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
| `wallet_address` | 唯讀 mainnet address，只用來讀倉位／margin。**留著 `0xYOUR...` 佔位符 = 視為未設定**——完整引擎輪會在花費 LLM 前具名 exit 1（零淨值無從 sizing）；`--context-only` 照常可跑，只是不顯示倉位。 |
| `coins` | Phase 1 單一標的，預設 `[BTC]`。 |
| `market_data` | `candle_interval`（4h；必須是 `1m`/`5m`/`15m`/`1h`/`4h`/`1d` 之一）／`candle_lookback`（200）／`funding_zscore_window_days`（30）。 |
| `indicators` | `rsi_14, ema_20, ema_50, atr_14, macd`，由 `context_builder` 計算。 |
| `engine` | `llm_provider: openrouter`、`deep_think_llm`、`quick_think_llm`、`selected_analysts: [market, social, news]`。 |
| `risk` | Phase 2 RiskGate：`leverage`（Phase 2 = 1x）、`margin_mode`（cross-only）、`max_target_margin_pct`（clamp 上限，requested 61–100 → approved 60）。cap 低於 decision grid 上限（也就是會實際生效）時必須落在 grid 上，否則有效上限會被靜默收緊 → config load 時具名 exit 1；cap 等於或高於 grid 上限則一律合法——它永遠不生效（有效上限 = min(grid 上限, cap) 由 grid 綁住），只是後備上限，不要求 grid 對齊。若 cap snap 到 grid 後 ≤ 0（低於 grid 最小值，或 grid 由 0 起算但 cap 小於一個 step）→ 每個方向性 target 都會 clamp 成 0 被風控拒絕（REJECTED），同樣 load 時 exit 1，不靜默上線。 |
| `decision` | 決策契約 grid：`ai_target_margin_min_pct`／`ai_target_margin_max_pct`／`target_margin_step_pct`（合法 `requested_target_margin_pct` = {min, min+step, …, max}；off-grid 直接 fail closed，不四捨五入；`(max − min)` 必須為 `step` 的整數倍，否則廣告的 `max` 落在 grid 外、config load 時 exit 1）、`rebalance_deadband_pct`（同向 \|approved − current\| 小於此值 → 不下單；flip/flat 例外）、`min_confidence`（低於此值的 set_target 被風控拒絕 REJECTED——包含 flat 請求；confidence 永不縮放 sizing）。 |

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
`--context-only` 也會用與完整輪相同的檢查先驗證 `risk:`／`decision:` 區塊——
壞掉的 config 在這個免費 smoke 階段就具名 exit 1，不會等到付費輪才爆。

### B. 完整一輪（需要 key）

建 context → 注入**未修改的** TradingAgents 引擎 → 引擎輸出**結構化 JSON target**
→ `parse_target_decision` 解析＋驗證（任何無效輸出 fail-closed 成 `maintain_current`，
原始回應照留）→ 確定性 `RiskGate` 依帳戶淨值 sizing／clamp／deadband → 寫 audit log
（schema v3）→ 印到 stdout。

```bash
export OPENROUTER_API_KEY=sk-or-...
python -m contrib.hyperliquid_perp.main --coin BTC
```

在**跑引擎（花 LLM 成本）之前**就會先擋下這些情況，乾淨地 exit 1：

- 沒有 `OPENROUTER_API_KEY`（提示改用 `--context-only`）。
- 沒有設定 funded wallet／`account_value` 讀到 0——RiskGate 無法對零淨值 sizing，
  每個方向性 target 都會被風控拒絕（`no_account_equity`），所以直接擋下、不白花
  LLM 成本（要純診斷就用 `--context-only`）。
- context 暖身不足、指標全滅、或 `atr_14` 算不出來（regime 會退化成假的 RANGING，掩蓋波動市況）。
- `risk:`／`decision:` config 區塊格式錯誤——含未知／打錯的 key、打錯的頂層區塊名、
  或非 mapping 的區塊。這些都會具名 exit 1（不會靜默退回預設值，以免一個 typo 讓
  安全上限悄悄變回寬鬆的預設）。頂層已知 key 的值錯誤——`network` 不是
  `mainnet`/`testnet`、`network_timeout_s` 不是數字、`wallet_address` 不是字串——
  也在 config load 時具名 exit 1；留空白的 key（如 `market_data:` 後面沒內容）
  視同未設定、套用預設。`--config` 路徑不存在或 YAML 語法錯誤（如引號未閉合）
  也走同一個具名 exit 1，不會噴 raw traceback。
- 倉位讀取失敗（帳戶狀態未知，拒絕在其上下單）。

Exit codes：`0` = 成功（含健康的風控拒絕，例如 low confidence）；`1` = config／環境／
引擎錯誤（上述各項）；`2` = 未預期錯誤；`3` = 引擎輸出不符合 structured-target 契約
（該輪已 fail-closed 成 `maintain_current` 並寫入 audit 紀錄）——scheduler 對非零
exit code 告警即可同時抓到故障與 model drift。同一輪同時發生契約失敗與 audit 寫入
失敗時，exit `1` 優先於 `3`（基礎設施故障是更大聲的警報）；只對 exit 3 告警的
scheduler 要一併涵蓋 exit 1。

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
  "schema_version": 3,
  "coin": "BTC",
  "timestamp": "2026-06-27T17:45:00+00:00",
  "timestamp_ms": 1782668700000,
  "prompt_hash": "sha256:9f2a…",
  "models": {
    "provider": "openrouter",
    "deep": "anthropic/claude-sonnet-4-6",
    "quick": "deepseek/deepseek-chat"
  },
  "raw_response": "```json\n{ …引擎原始回應照留… }\n```",
  "parse": { "is_valid": true, "invalid_reason": null },
  "decision": {
    "decision_mode": "set_target",
    "target_side": "long",
    "requested_target_margin_pct": 35,
    "confidence": "0.78",
    "rationale": "…",
    "key_risks": ["…"]
  },
  "risk": {
    "decision_mode": "set_target",
    "target_side": "long",
    "requested_target_margin_pct": 35,
    "approved_target_margin_pct": 35,
    "risk_action": "approved",
    "risk_reason": null,
    "order_created": true,
    "no_order_reason": null,
    "target_margin": "350",
    "target_notional": "350",
    "target_signed_notional": "350",
    "current_signed_notional": "0",
    "delta_notional": "350",
    "configured_leverage": "1",
    "confidence": "0.78"
  },
  "mark_price": "65000",
  "account_equity": "1000"
}
```

`raw_response` 照留引擎的原始回應（DESIGN Part 2：無效輸出只記錄、絕不重建）；
`parse` 是解析判定；`decision` 是解析出的結構化 target；`risk` 是 RiskGate 的
sizing／clamp／下單判定（`target_*` 皆 mark 定價，`approved 35% × equity 1000 = 350`）；
`mark_price`／`account_equity` 是決策當下的 sizing 輸入——沒有它們，REJECTED／
maintain_current 紀錄（`target_*` 全為 null）無法事後重現成幣量。
`prompt_hash` 是本模組注入引擎的完整文本——perp context **加上** output-format
契約（內含當時的 margin grid 與 `min_confidence`）——的 sha256，所以改了
`decision:`／`risk:` config 的兩筆紀錄不可能帶同一個 hash；配合 `models` 與
`timestamp`，任何一筆 decision 都能重建並做 post-mortem。各欄位定義見
[DESIGN](./DESIGN.md) Part 2 與 [phase2-spec](./phase2-spec.md)。

> 這一輪跑到「parse → RiskGate → 寫 log + 印出 decision」為止；RiskGate 會 sizing／
> clamp／風控拒絕（REJECTED）／fail-close（契約違規），但**仍不下任何真實單**——
> 實際 paper／live 執行是 Phase 3+ 的事。

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
| `invalid config — unknown … key` / `unknown top-level config key` | config 有打錯的 key（如 `max_target_margin_pt`）或頂層區塊名（如 `riks:`）；strict 解析會擋下不讓它靜默退回預設值。對照 `hyperliquid.example.yaml` 的 key 名修正。 |
| 想先便宜地跑一次驗收 | 暫時把 `engine.deep_think_llm` / `quick_think_llm` 指到便宜／免費的 OpenRouter 模型，確認 pipeline 會 parse target → 跑 RiskGate → 寫 log（schema v3），再切回來。 |
