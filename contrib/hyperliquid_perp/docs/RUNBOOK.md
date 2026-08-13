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

已 export 的環境變數永遠優先於 `.env` 檔的值。repo 根目錄的 `.env.enterprise`
也會一併載入（上游引擎對等行為；優先序 exported env > `.env` > `.env.enterprise`）
——一般設定用不到，但失敗診斷訊息可能提到它。兩個檔都只在程序**啟動時讀一次**：
run 跑到一半編輯（補 key、換 key）對運行中的程序無效，重啟後才會讀到新值。

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

> **長駐建議**：這是前景程序。多日驗收 run 建議掛在**會自動重啟的監管**下
> （systemd `Restart=on-failure` + `RestartSec=60` + `StartLimitBurst`；Windows
> 用 Task Scheduler「工作失敗時重新啟動」或 NSSM）——重啟 reconciliation 本來
> 就設計成安全冪等，而無監管時半夜 crash 到被發現之間，持倉完全沒有 SL/TP 看管。
> 注意：protection-only 自我了結與 keyless 停止走的也是 exit 1，監管會把它拉
> 回來、再進 protection-only——反覆重啟不是修復，看到這個模式仍要照 §5 人工
> 調查。手動掛 tmux／screen 也可以，但要接受上述無人看管的空窗。無論哪種方式，
> launcher 的 working directory 必須是 repo 根目錄——`.env` 的搜尋從 CWD 往上走
> （systemd 設 `WorkingDirectory=`、Task Scheduler 設「開始位置」、`Start-Process`
> 設 `-WorkingDirectory`）。迴圈跑起來之後，Ctrl-C 與 SIGTERM 都是安全停止，
> 會先做最後一次 export 再退出；訊號若落在啟動／reconciliation 階段（迴圈開始
> 前），則以 exit 130 直接中止、不做 export。

## 4. 停止與重啟

**停止**：Ctrl-C（或 `kill <pid>`）。迴圈運行中的正常 shutdown 會補一次 pending
funding 重試並寫最後一批 CSV（訊號落在啟動／reconciliation 階段則 exit 130、
不寫最後 export——見 §3 同一注意事項）。

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

**策略調參換段**：改動策略參數（如 `rebalance_deadband_pct`、
`resize_min_confidence`）時，用 `--create` 開新 run-id（例如 `paper-BTC-2`）
而不是讓舊 run 帶著 drift 續跑——新段有自己的 genesis config，跨段指標比較
才乾淨。舊 run 的 DB 與 exports 原地保留，當作對照的 baseline。注意：drift
警告只比對 YAML——**code 內建預設值的變更**（例如新增的
`resize_min_confidence` 預設 0.7、prompt 文字改版）不會觸發警告，但一樣改變
行為，所以調參 code 的部署必須與新 run-id 同批上線，不要讓舊段跨過部署點。
prompt 的 context／format 契約改 shape 時，另要 bump `cli.py` 的
`PROMPT_VERSION`，讓 `ai_inputs.prompt_version` 在資料裡標出改版點。

**例外：只改 prompt 的 A/B 驗證**。上面那條規則是為了讓**績效指標**跨段可比。若這次
部署只改 prompt、且目的正是量測「這個 prompt 改動有沒有效」，那就刻意讓現有 run 跨過
部署點，改以 `ai_inputs.prompt_version` 切段——同一個 run 的市場條件與帳戶狀態連續，
比開新 run 更乾淨。前提是那個 run 的**策略價值已經是零**（否則等於汙染基線），且判讀
只看 prompt 敏感的指標（提案率、信心分布），不看權益曲線。`phase2-target-v3` 就是照
這條例外部署到 `paper-BTC` 的。

**空倉才換段（硬規則）**：舊 run 還有未平倉倉位時不要換段。換段後那個倉位
會永遠凍結在舊段 DB——沒人看管、沒有 SL/TP，舊段末端 equity 掛著一筆未實現
PnL，正是跨段對照要避免的汙染。等 cycle 自然回到空倉（或 AI 自己 flat 平倉）
再執行下面的順序，讓舊段以 realized PnL 乾淨收尾。

換段的實際順序——push `deploy/paper` 會觸發 workflow 自動 restart `hl-paper`
（`deploy/paper` 分支上的 `.github/workflows/deploy.yml`），而 run-id 在伺服器 systemd unit 的
`ExecStart` 裡，**先 push 就會讓舊 run 直接跨過部署點跑新 code**，所以必須：

1. 確認空倉後，SSH 上伺服器 `sudo systemctl stop hl-paper`，把 unit 的
   `ExecStart` 改成新段參數（`--run-id paper-BTC-2 --create`），
   `sudo systemctl daemon-reload`，**先不要啟動**。
2. 再 push `deploy/paper`——workflow 部署新 code 並 restart，服務直接以新
   run-id 起段。
3. 確認新段健康後，**立刻**把 unit 裡的 `--create` 拿掉再 `daemon-reload`：
   run 已存在時帶 `--create` 是硬錯誤，留著的話**任何**後續 restart——crash
   自動重啟、主機重開機、下一次 deploy——都會直接失敗（systemd `Restart=`
   還可能因此 crash-loop），不是只有下次 deploy 才危險。

deploy 自動 restart 的三個既有行為，換段與日常部署都要知道：push 落在 cycle
中間會中斷 in-flight 的 execution plan——安全但 off-schedule（見上方重啟
reconciliation 段）；若舊 process 是被硬殺而非 graceful shutdown，
single-instance lock lease 最長 15 分鐘才過期，新 process 會先起不來，deploy
的 15 秒健檢在這個窗口內可能誤報；服務處在 protection-only／keyless 狀態時，
deploy 的 restart 不是修復（§3 的警告同樣適用）——先照 §5 調查再部署。

## 5. 日常監控（每天看一眼）

| 看什麼 | 在哪裡 | 正常 | 異常時 |
|---|---|---|---|
| process 還活著、log 有新輸出 | stderr log（INFO、含時間戳） | 每 4h 一個 cycle 的 log；tick 只在有部位／活動掛單時發生且健康時不寫 log——空倉的安靜是正常的 | 見下方 protection-only |
| 最新 CSV | `<db 目錄>/exports/paper-BTC/` + `manifest.json` | 每 cycle 更新 | `export_failed` 只記錄不影響交易 state；連續失敗查磁碟/權限 |
| `REPLAY_UNVERIFIED.json` | 同上 export 目錄 | **不存在** | 存在 = 該批 CSV 未經 replay 驗證，先調查 store 再相信數字 |
| 中途健檢 | `python -m contrib.hyperliquid_perp validate --run-id paper-BTC` | exit 4（一致、cycles 未滿） | exit 5 = integrity failure，停下來調查 |

**Protection-only 模式**（log 會明講）：不開新 decision cycle，但 SL/TP 與監控
持續；倉位被 SL/TP 了結後程序會做最後 export 並以 exit 1 自動結束。三種成因、
對應動作不同：

- **replay mismatch**：帳本重放對不上——**先調查 store**（`validate` + log），
  不要盲目重啟。
- **健康 resume 缺 API key**：帳本是健康的，只是少了憑證——照 §1.3 設好
  `OPENROUTER_API_KEY` 後重啟即可恢復交易（CLI 的 stderr 訊息也是這麼指示），
  不需要先跑 `validate`。
- **健康 resume 但引擎 import 失敗**（常見成因：`.env` 被存成 UTF-16 等壞編
  碼）：帳本健康、倉位仍活著，只是引擎載不起來——照 stderr 印出的錯誤修好
  環境（例如把 `.env` 重存為 UTF-8）後重啟即可恢復交易，不需要先跑
  `validate`。同樣的失敗發生在**空倉** restart 或 fresh `--create` 時則直接
  具名 exit 1（沒有倉位需要看護）。

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
| 每 4h 一筆 `api_failed`，error_message 是 `every technical indicator failed` 或 `… is/are unavailable`（點名 `atr_14`／`ema_20`／`ema_50` 中死掉的那些） | indicator 引擎（stockstats）壞掉或 regime 指標算不出來——三者任一缺席 regime 都會被捏造成 RANGING，daemon 與 one-shot 同樣拒跑（不燒 LLM）。（非空的 `indicators:` 清單漏配三者任一現在直接在 config load 擋下；會走到這裡的 config 成因只剩刻意的 `indicators: []`。）修 stockstats 相容性後，`--context-only` 走同一套 guard：照樣渲染但印同一句 refusal 警告並 exit 4（健康 context 是 exit 0），可拿來免 key 驗證修好了沒。 |
| **每一個** cycle 都 `invalid_output`（fail-closed、零下單），且 log 裡不再出現 `structured-output invocation failed` fallback 警告 | 模型的 structured output 成功了，渲染輸出天生不含 Phase 2 target JSON → 解析必失敗。確認 `engine.structured_output` 沒被設成 `true`（perp 預設 false、強制 free-text 路徑）；若真的被設成 `true`，engine config 建構（provider 啟動）時會在 log＋stderr 雙通道發警告——直接搜 `engine.structured_output: true` 即可確認（兩個通道都含這段；`warning: ` 前綴只在 stderr 那份）。gate 生效的正向訊號是 paper/live log 每次 AI 呼叫三行 `structured output disabled by config; using free-text generation` INFO（Portfolio/Research Manager、Trader 各一；重試的 cycle 每次嘗試都會再印一組），看到它們就代表 free-text 路徑在跑。2026-07-27 paper-BTC 換模事故即此成因。**注意 `phase2-target-v3` 起有第二個成因與此症狀完全同形**（structured output 確實關著、三行 INFO 也都在，但每個 cycle 仍解不開）：schema 區塊改成型別非法佔位符後，模型整段照抄會 fail-closed。分辨方式是看 `ai_outputs.risk_reason`——照抄記 `invalid_decision_mode`，structured-output 事故記 `invalid_output`（連 JSON 區塊都找不到）。 |

更多症狀見 [SETUP §7](./SETUP.md#7-troubleshooting)。
