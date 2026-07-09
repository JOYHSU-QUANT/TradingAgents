# Setup & run

實用 runbook：怎麼安裝、設定、執行。想知道**為什麼**這樣設計，見
[phase1-spec](./phase1-spec.md) 與 [phase2-spec](./phase2-spec.md)；本頁只有「照做」的步驟。

> 目前狀態：Phase 2 已完整可跑——market context + 未修改的引擎 + structured
> target 契約 + 確定性 RiskGate + paper 執行引擎 + 4h scheduler／CLI（§4C 的
> `paper`／`export`／`validate` 子命令）。paper run 下的是**本地模擬單**（SQLite
> paper orders/fills），不碰交易所下單 endpoint——live 執行是 Phase 3 的事。

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
| `paper_trading` | Phase 2 紙上帳戶＋撮合模型：`initial_balance_usdc`（開帳淨值，必須 > 0）、`initial_positions`（可選種倉）、`execution.fill_model.slippage_bps`／`execution.market_monitor.interval_seconds`／`request_timeout_seconds`、`taker_fee_rate` 等。整個區塊在 config load 時就驗證——未知／打錯的 key、非 mapping、或無效值（如非正的 `initial_balance_usdc`、負費率）都具名 exit 1，不靜默套預設。 |

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
`--context-only` 也會用與完整輪相同的檢查先驗證 `risk:`／`decision:`／`paper_trading:`
區塊——壞掉的 config 在這個免費 smoke 階段就具名 exit 1，不會等到付費輪才爆。

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
- context 暖身不足、指標全滅、或 `atr_14` 不可用（未列入 `indicators` 設定、或算不出來——
  兩種情況都會讓 regime 退化成假的 RANGING，掩蓋波動市況，所以一律擋下）。
- `risk:`／`decision:`／`paper_trading:` config 區塊格式錯誤——含未知／打錯的 key、
  打錯的頂層區塊名、或非 mapping 的區塊（`paper_trading:` 另含無效值，如非正的
  `initial_balance_usdc`、負的 `taker_fee_rate`／`slippage_bps`、非正的 monitor
  interval／timeout）。這些都會具名 exit 1（不會靜默退回預設值，以免一個 typo 讓
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

### C. Phase 2 長駐 paper run（需要 key）

PR 4 的 subcommand CLI（`python -m contrib.hyperliquid_perp <sub>`；空 argv 或
`-` 開頭的旗標式呼叫原樣走上面 A/B 的 legacy 路徑，行為不變；不認識的裸字
——多半是 subcommand 打錯——具名報錯 exit 1，不會落進 legacy 的 usage）：

```bash
# 首次啟動（--create 才允許建立新 store／新 run——防走錯目錄靜默分叉歷史；
# 反向也成立：run 已存在時帶 --create 會報錯，避免以為開新 run 其實在舊 run 續寫）：
python -m contrib.hyperliquid_perp paper --coin BTC --db paper_trading.db --create

# 之後重啟（不帶 --create；DB 或 run 不存在會具名報錯）。重啟時自動做
# execution §1.2 的 reconciliation（取消舊 plan、補帳 pending funding、replay 驗證、
# gap SL 檢查、立即開新 cycle），並比對 genesis config：換 coin 硬錯、
# risk/decision/paper_trading 漂移警告（同時落地 scheduler_state 的
# last_config_drift_* breadcrumb，事後可從 store 還原）。同一個 --run-id 續跑同一個 run。
# 單實例鎖：同一 run 已有活著的 process（scheduler_state 心跳 < 900s）時啟動報錯；
# 反向也守住——凍結後被接管的舊 process，下一次心跳會發現 lease 易主而具名退出。
python -m contrib.hyperliquid_perp paper --coin BTC --db paper_trading.db

# 手動全量 CSV export（八張 phase2-data §5–§12 CSV；.tmp → atomic replace；
# 最後寫 manifest.json 標記整組一致——讀取方先驗 manifest 再做跨檔 join）
python -m contrib.hyperliquid_perp export --run-id paper-BTC --output-dir exports/

# 驗收器：13 項 summary 指標 + 鏈路完整性 + 可進 Phase 3 判定
python -m contrib.hyperliquid_perp validate --run-id paper-BTC
```

`paper` 每個 cycle 完成（含 `invalid_output`／`api_failed`）會先跑 accounting
replay 再自動 export CSV；Ctrl-C（SIGINT）與 SIGTERM（systemd／docker／`kill`）
的正常 shutdown 都會做最後一次 export。自動 export 寫到
`<db 目錄>/exports/<run_id>/`（`--export-dir` 可改）。CSV 只是 SQLite 的 view——
export 失敗只記 `export_failed`（stderr + `scheduler_state.last_export_status/
error/at` 持久化），不影響交易 state 與 SL/TP。replay 驗證結果同樣持久化在
`scheduler_state.last_replay_status/error/at`；由於 `scheduler_state` 不在匯出的
八張 CSV 內，replay 未通過（mismatch／failure）時會在 CSV 旁另寫
`REPLAY_UNVERIFIED.json`，讓只讀 CSV 的下游知道該批未經驗證（下一個乾淨 cycle
自動清除）。replay mismatch 時進 protection-only 模式——停開新 decision cycle，但
engine 續 tick、SL/TP 與監控不中斷（重啟時 mismatch 且有倉位也是同一模式；空倉才
直接拒絕啟動）。protection-only restart 不呼叫 AI，可無 `OPENROUTER_API_KEY` 啟動；
新 run 在建立 run row 前即檢查 key，正常 resume 則在 reconcile 定案模式之後才檢查。
完整 AI payload JSON 存於 `<db 目錄>/payloads/<run_id>/`，`ai_inputs` 記其路徑與
sha256。
市場資料 warmup 不足（closed candle 數 < 門檻）會走 §3.1 retry ladder 後記
`api_failed`（error_type=server_error、無 AI 花費）——太年輕的標的在暖機完成前
每 4h 產生一筆，屬預期行為。

`validate` 的 exit code：`0` = 可進 Phase 3（`cycle_count >= 30` 且 orphan／
snapshot／replay mismatch 全為 0）；`4` = 資料一致但尚未達標（繼續累積 cycles）；
`5` = 有 integrity failures（orphan／mismatch——先調查再相信結果）；`1` = 操作
錯誤（db／run 不存在）。`cycle_count` 只計 `completed`／`invalid_output`（
`api_failed` 另計為 `api_failed_count`，不算進 30 輪門檻）。跑滿 30 cycles
（約 5 天）後用它檢查驗收條件。

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
> clamp／風控拒絕（REJECTED）／fail-close（契約違規），但**仍不下任何單**——paper
> 執行引擎已實作、尚未接上這條 runbook 流程（PR4 scheduler／CLI）；live 執行是
> Phase 3 的事。

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
