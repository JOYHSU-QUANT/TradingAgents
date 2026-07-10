# Paper trading RUNBOOK

從零開始把 Phase 2 paper run 掛起來、顧好、跑滿驗收的**操作手冊**——只有照做的
步驟與日常操作。完整的欄位說明、exit-code 契約與設計理由見 [SETUP](./SETUP.md)；
本頁重複的內容以 SETUP 為準。

所有指令都從 repo 根目錄 `TradingAgents/` 執行。

---

## 1. 一次性前置（第一次跑之前）

### 1.1 安裝與自我檢查

```bash
pip install -r requirements.txt
python -m pytest contrib/hyperliquid_perp/tests/ -q   # 全綠才繼續
```

### 1.2 建 local config

```bash
cp contrib/hyperliquid_perp/configs/hyperliquid.example.yaml \
   contrib/hyperliquid_perp/configs/hyperliquid.local.yaml
```

編輯 `hyperliquid.local.yaml`，至少改這一項：

- `wallet_address` — 你的**唯讀** mainnet 地址（只讀倉位/margin，永遠不需要
  private key）。留 `0xYOUR...` 佔位符 = 未設定，完整輪會在花 LLM 成本前 exit 1。

其餘欄位（1x cross、max margin 60%、紙上帳戶 1000 USDC、4h K 線）預設值可直接用。

> **省錢建議**：第一次試跑可把 `engine.deep_think_llm`／`quick_think_llm` 暫時
> 指到便宜或免費的 OpenRouter 模型，確認 pipeline 全通再切回正式模型。注意
> `engine:` 是 resume 時的 config-drift 比對項目——正式驗收的 30 cycles 請用
> 同一組模型跑完，中途換模型會記 drift warning 且聚合指標橫跨兩組參數。

### 1.3 設定 `OPENROUTER_API_KEY`

一把 key 涵蓋所有 OpenRouter 模型。三種方式擇一：

| 方式 | 指令／位置 | 適用 |
|---|---|---|
| repo 根目錄 `.env`（已 gitignore） | `OPENROUTER_API_KEY=sk-or-...` 一行，檔案存成 **UTF-8**（PowerShell 的 `>>` 預設寫 UTF-16，CLI 會警告後忽略該檔——用編輯器存檔，或 `Add-Content -Path .env -Value "OPENROUTER_API_KEY=sk-or-..." -Encoding utf8`） | **推薦**——長駐 run、重開機後仍有效，不依賴終端機。 |
| Windows 使用者層級環境變數 | `[Environment]::SetEnvironmentVariable("OPENROUTER_API_KEY", "sk-or-...", "User")` | 同樣持久；之後開的新終端機才看得到。 |
| 當前終端機 session | PowerShell：`$env:OPENROUTER_API_KEY = "sk-or-..."`；bash：`export OPENROUTER_API_KEY=sk-or-...` | 快速試跑；關窗即失效。 |

已 export 的環境變數永遠優先於 `.env` 檔的值。

## 2. 免費 smoke test（不花 LLM 成本）

```bash
python -m contrib.hyperliquid_perp.main --context-only --coin BTC
```

連 mainnet、算指標、印出 `PerpMarketContext`，同時用與完整輪相同的檢查驗證
config——壞 config 在這個免費階段就具名 exit 1。`wallet_address` 已設定的話會
一併印出目前倉位（或 `flat`）。

## 3. 啟動 paper run

```bash
# 首次啟動：--create 建立新 SQLite store + run（run 已存在時帶 --create 會報錯）
python -m contrib.hyperliquid_perp paper --coin BTC --db paper_trading.db --create
```

之後它會：

- 每 **4 小時**跑一個 decision cycle（context → AI → RiskGate → 紙上 TWAP 下單），
- 有活兒（持倉、進行中的 TWAP／flip、掛著的 SL/TP）時每 **30 秒** tick 一次監控
  （TWAP 吃單、SL/TP、市況新鮮度）；空倉閒置時不發任何市場請求，睡到下個 cycle，
- 每個 cycle 完成後自動 export 八張 CSV 到 `<db 目錄>/exports/paper-BTC/`
  （`--export-dir` 可改），
- 完整 AI payload JSON 存在 `<db 目錄>/payloads/paper-BTC/`。

單實例鎖：同一個 run 同時只允許一個 process，重複啟動會具名報錯。

> **長駐建議**：這是前景程序，掛幾天請放在不會被關掉的終端機
> （tmux／screen／`Start-Process`／systemd 都可以），且 launcher 的 working
> directory 必須是 repo 根目錄——`.env` 的搜尋從 CWD 往上走（systemd 設
> `WorkingDirectory=`、`Start-Process` 設 `-WorkingDirectory`）。Ctrl-C 與
> SIGTERM 都是安全停止，會先做最後一次 export 再退出。

## 4. 停止與重啟

**停止**：Ctrl-C（或 `kill <pid>`）。正常 shutdown 會補一次 pending funding
重試並寫最後一批 CSV。

**重啟**（同一個 run 續跑，**不帶** `--create`）：

```bash
python -m contrib.hyperliquid_perp paper --coin BTC --db paper_trading.db
```

重啟自動做 reconciliation：取消前一個 process 留下的 execution plan、補帳
pending funding、accounting replay 驗證、gap SL 檢查；若這次重啟真的取消了一個
未完成的 plan 會立即開新 cycle，否則沿用原本的排程時間（常見情況——乾淨停止、
沒有活 plan——最長可能等 4h 才有下一個 cycle）。換 coin
是硬錯誤；`risk:`／`decision:`／`engine:` 等與 genesis 不同會印 drift 警告並
記進 store。

## 5. 日常監控（每天看一眼）

| 看什麼 | 在哪裡 | 正常 | 異常時 |
|---|---|---|---|
| process 還活著、log 有新輸出 | stderr log（INFO、含時間戳） | 每 4h 一個 cycle 的 log；tick 只在有部位／活動掛單時發生且健康時不寫 log——空倉的安靜是正常的 | 見下方 protection-only |
| 最新 CSV | `<db 目錄>/exports/paper-BTC/` + `manifest.json` | 每 cycle 更新 | `export_failed` 只記錄不影響交易 state；連續失敗查磁碟/權限 |
| `REPLAY_UNVERIFIED.json` | 同上 export 目錄 | **不存在** | 存在 = 該批 CSV 未經 replay 驗證，先調查 store 再相信數字 |
| 中途健檢 | `python -m contrib.hyperliquid_perp validate --run-id paper-BTC` | exit 4（一致、cycles 未滿） | exit 5 = integrity failure，停下來調查 |

**Protection-only 模式**（log 會明講）：replay mismatch 或健康 resume 缺 API key
時進入——不開新 decision cycle，但 SL/TP 與監控持續。倉位被 SL/TP 了結後程序
會做最後 export 並以 exit 1 自動結束。此時**先調查 store**（`validate` + log），
不要盲目重啟。

`api_failed` cycle（網路／API 問題）是預期會偶發的，計入
`api_failed_count`、不進 30 輪門檻；連續大量出現才需要查網路。注意只有
LLM 呼叫**之前**的失敗（連線、warmup、payload 寫入）零 AI 花費——LLM 逾時／
限流類的 api_failed 每次嘗試（最多 3 次）都已產生費用。

## 6. 驗收（約 5 天後）

```bash
# 手動全量 export（可隨時跑，不影響 run）
python -m contrib.hyperliquid_perp export --run-id paper-BTC --output-dir exports/

# 驗收報告：13 項 summary 指標 + 鏈路完整性 + Phase 3 判定
python -m contrib.hyperliquid_perp validate --run-id paper-BTC
```

| `validate` exit | 意義 | 下一步 |
|---|---|---|
| `0` | `cycle_count >= 30` 且 orphan／snapshot／replay mismatch 全為 0 | **可進 Phase 3** |
| `4` | 資料一致但 cycles 未滿 30 | 繼續掛著跑 |
| `5` | integrity failure（orphan／mismatch／store 壞掉） | 先調查再相信任何結果 |
| `1` | 操作錯誤（db／run 不存在） | 檢查 `--db`／`--run-id` |

報告中的 `warning:` 行（超時 pending funding、config drift）不影響 exit code，
但寫結論前要看過。

## 7. 快速故障排除

| 症狀 | 解法 |
|---|---|
| `OPENROUTER_API_KEY is not set` | 依 §1.3 三種方式擇一設定；`.env` 記得放在 repo 根目錄。 |
| 啟動報 run 已有活著的 process | 單實例鎖。找到舊 process 停掉；若是殘留心跳，等 15 分鐘 lease 過期。 |
| `config not found` / `invalid config` | 對照 example 修 `hyperliquid.local.yaml`；strict 解析會擋未知 key。 |
| `--context-only` 不印倉位行／完整輪報 no usable account equity（exit 1） | `wallet_address` 還是佔位符；填真實唯讀地址。 |
| 太年輕的標的每 4h 一筆 `api_failed` | 市場資料 warmup 不足，暖機完成前屬預期行為。 |

更多症狀見 [SETUP §7](./SETUP.md#7-troubleshooting)。
