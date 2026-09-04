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
> 監管會拉回來的是 daemon **真的退出**的情況（store 壞掉、lease 被搶、export 的
> fail-loud 例外等）；decision cycle **前半**（讀帳本、抓市場、寫 `ai_inputs`、呼叫
> AI——即 AI 回答之前）的**程式缺陷**（非 Retryable 例外）不再讓 daemon 退出——該
> cycle 記成 `api_failed`（`error_type` 空、`error_message` 以 `non-retryable:` 開頭）、
> log 印 ERROR traceback、倉位與 SL/TP 照舊看管、下個 cycle 照排，與 live 車道對
> 這一段的語意一致（見 §7）。AI 回答**之後**依「當下已有哪些持久事實」分流
> （與 live 的 persist-retry 分流對齊）：**兩個 scheduler 自己的寫入**——回覆落地
> （`pending_raw_response`）與 `ai_outputs` 稽核寫入——失敗時**不退出**，決策（與已
> gate 的 plan 登記）留在記憶體，下一次 poll 只重試那筆寫入（絕不重問 AI、絕不重跑
> gate；每次重試失敗都印 ERROR traceback——operator 的 export／validate 短暫持有
> SQLite 鎖就是設想情境）；同一 cycle **連續 10 次** poll 都失敗才改讓例外傳播
> （daemon 退出交監管——撐過十輪 poll 的故障不是暫時性鎖，無上限重試會讓 run
> 靜默僵住；中間只要有一次寫入成功，連續計數就歸零）；重啟時 resume 到**存壞的
> 回覆**（re-parse 丟例外）則把該 cycle 記成 `api_failed` 並清掉該回覆（否則重啟
> 會無限 crash-loop 進同一個 parse；清除前會先把回覆全文印進 ERROR log 供事後診斷）。
> AI 回答之後**唯一完全不做容納**的是引擎 `start_plan` 內逃出的例外——引擎
> fail-stop 後拒絕所有後續呼叫（可能存在部分 commit 的 plan），留在 process 裡
> 等於倉位無人看管，退出重啟、由重啟 reconciliation 重建引擎才是復原路徑（這是
> 與 live 車道的刻意差異：live 有 recoverable safe mode，paper 沒有對應機制）。
> 除它以外仍會退出的還有：上面那個 persist 逃生上限、**終端 `api_failed` 記錄
> 本身的寫入失敗**（這一筆沒有重試車道——live 那側有 `pending_fail` 無上限重試，
> paper 尚無對應物）、cycle 邊界那幾筆在所有守衛之外的排程寫入（`_execute` 呼叫
> AI 前的 counter 預寫、新 cycle 的 attempt insert），以及 cycle 收尾 best-effort
> 快照丟出的非 DB 例外。
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
（prompt v5 起 `format_fingerprint` 也不再跟著門檻走，所以門檻的 code 預設值改動在
`ai_inputs`、drift 戳、`prompt_regime:` 三處**都看不到**——只剩 run-id 這一道。）
prompt 的 context／format 契約改形狀**或改措辭**時（v5 就是純措辭的改版），另要 bump
`cli/_provider.py` 的 `PROMPT_VERSION`，讓 `ai_inputs.prompt_version` 在資料裡標出改版點。
**凡是跨越量測邊界的部署都要 bump，回滾也算**：回滾到舊 prompt 不算「改 shape」，
但沿用已退役的舊值會讓 `GROUP BY prompt_version` 把 v3 之前與回滾之後併成同一桶，
正好污染要拿來比的基線。退役過的值一律不得重用（回滾就給**下一個從未用過的值**，
內容等不等於舊版無所謂；`v4` 已被 2026-08-27 的 `Position:` 段用掉、`v5` 已被 2026-09-01
的「格式段不印門檻數字」用掉，都不是回滾備用值）。另注意 `decision_format_instructions` 的文字與這個常數不在同一個
模組——`cli/_provider.py` 確實 import 了它，但 import 不會讓常數跟著文字動——所以有一個測試把
版本戳釘在渲染出來的區塊指紋上：改了 prompt 文字卻忘了改戳就會紅。

**例外：只改 prompt 的 A/B 驗證**。上面那條規則是為了讓**績效指標**跨段可比。若這次
部署只改 prompt、且目的正是量測「這個 prompt 改動有沒有效」，那就刻意讓現有 run 跨過
部署點，改以 `ai_inputs.prompt_version` 切段——同一個 run 的市場條件與帳戶狀態連續，
比開新 run 更乾淨。前提是那個 run 的**策略價值已經是零**（否則等於汙染基線），且判讀
只看 prompt 敏感的指標，不看權益曲線。`phase2-target-v3` 就打算照這條例外部署——跨過
部署點的會是最後成交停在 2026-07-16、此後零成交、策略價值歸零的 `paper-BTC`。

**這條例外附一個硬規則：量測窗內不得夾帶其他輸入面變更。** 切段鍵是
`prompt_version`，而它只在 context／format **契約改形狀或改措辭**時才 bump；輸入面的**內容**
變了（例如把某個 ships-OFF 的 vendor 翻成啟用）不改形狀、不會 bump，`input_payload_hash`
每個 cycle 本來就不同也切不了段——於是 after 段的前半後半是兩個不同的輸入制度，而資料
裡完全看不出來。所以跨過 v3 邊界之後、量測窗結束之前，`deploy/paper` **只准**帶 prompt
變更；若不得不夾帶（含 PR #21 macro／treasuries 那種需要第二個 cutover commit 才生效的
vendor），這一段量測作廢、bump 到下一個版本戳重來。`paper-BTC` 當初 159 個 cycle 橫跨五個
制度斷點而無法解讀，就是這條規則不存在的代價。

**第三種情況：改 YAML 就會改 context 形狀——由 `context_shape` 自動切段。** 上面兩條規則都
預設「改形狀 = 改 code = 會部署 = 有機會 bump `PROMPT_VERSION`」。有些 key 不需要部署就會
改 prompt 的**結構**：`market_data.volume_profile_window_candles` 從 `0` 調到 `>= 12`，
`render_market_context` 多出一整段 `Volume profile (...)`；改 `indicators` 清單，
`Indicators:` 底下多一列或少一列。沒有 commit、沒有 bump，釘在 `decision_format_instructions`
指紋上的那個測試也看不到。所以 schema v10 起 `ai_inputs` 多一欄 **`context_shape`**（同時寫進
payload JSON）：`domains/perp/prompt_context.context_shape` 把當次渲染的**段落結構**寫成一個
字串，例如 `price|market|funding|indicators(rsi_14,ema_20,ema_50,atr_14,macd)|volume_profile`。
`--context-only` 會在渲染結果後印一行 `prompt_regime: prompt_version=… context_shape=… format_fingerprint=…`
（三鍵同一行，文法與 daemon 啟動 log、`validate` 的 `prompt_regime:` 行完全相同——三處共用
`common/prompt_regime.py` 一個渲染函式，同一個字串可以 grep），改 YAML 後部署前就能看到會落在哪個
桶——但注意它印的 `context_shape` **少一段**：prompt v4 起 paper／live daemon 的列一律多帶 `|position`
（倉位段從本地帳本來，一次性 CLI 沒有帳本、維持 position-blind），比對時把這一段補上再比。
**daemon 自己也會說**（issue #163）：paper／live 第一個組出 prompt 並寫下 payload 的 cycle 會在 log 印
同一行 `prompt_regime: …`（INFO，`cli._provider`），之後**只在三鍵翻桶時再印一次、仍是 INFO**——volume
profile 段因歷史不夠被跳過、倉位段因權益 ≤ 0 被省略（見 §7）都算翻桶，多半是資料驅動、不是告警；一整段
run 沒翻過就只有啟動那一行。改 YAML 重啟後看 journald 這一行就知道落在哪個桶，不用跑 `validate`。
（這行印在 payload 寫入成功後、`ai_inputs` 列寫入前；後者失敗是 §7 那種非 Retryable bug，同時間會有
ERROR traceback，該 cycle 記 `api_failed`——所以看到這行但 `validate` 桶數少一個，先找那個 traceback。）
它只取結構——段落標題、指標列名、volume profile 段有沒有——**不取**標籤裡的數字
（`Candles: 200 x 4h`、`30d z-score`）也不取每 cycle 隨資料有無變動的 `Mid:`／`Premium:`
行，否則每個 cycle 自成一段。

context 那一半的切段鍵因此是兩個（**`prompt_version, context_shape`**；format 那一半的第三個鍵見下段）。`prompt_version` 仍是人工
指定、退役值不得重用的 code 改版戳；`context_shape` 則讓「多一段／少一段」（含 `indicators` 清單重排——列數不變但
prompt 不同）不管來自 code 還是 YAML 都自動落進資料裡。翻動這些 key 仍然是跨越量測邊界（A/B 量測窗內**禁止翻動**，
與夾帶 vendor 變更同罪）——差別是現在資料會自己標出來，不靠人記得。標籤裡的數字變了
（`candle_interval`、`funding_zscore_window_days`）屬於**內容**變更，`context_shape` 不動，
由下面的 config drift 警告承接。**format 那一半由第三個鍵承接**：`decision:` 的格線
（與 `risk.max_target_margin_pct` 壓出的有效上限）會渲染進 `decision_format_instructions` 的文字，
改 YAML 就換數字、不 bump 也不改 shape——schema v11 起 `ai_inputs` 多一欄 **`format_fingerprint`**
（`domains/perp/target_decision.format_fingerprint`：渲染文字的 SHA-256 前 16 個 hex，同時寫進
payload JSON），`--context-only` 那一行 `prompt_regime:` 也帶著它。它是**內容指紋不是 shape**：
那段文字任何改動都換值。**prompt v5 起三個 gate 門檻（`min_confidence`／`resize_min_confidence`／
`rebalance_deadband_pct`）不再渲染成數字**（只剩定性描述），所以改門檻 YAML **不會**換指紋——它
跟著模型讀到的文字走，模型讀不到門檻了；門檻改動仍是策略調參、照上面的規則開新 run-id——
resume 時的 `config_drift` 只是「最新一次比對」的戳（下次乾淨 resume 就蓋回 `ok`），不是逐列的鍵，
`prompt_regime:` 因此**不會**自動為門檻改動切段。所以完整切段鍵是三個：
**`GROUP BY prompt_version, context_shape, format_fingerprint`**；`validate` 會依三鍵印每組的
cycle 數（`prompt_regime:` 行，見 §6），一眼看出 run 有沒有跨段。翻動這些 key 仍是跨越量測
邊界、量測窗內禁止翻動——差別只是資料現在會自己標出來。另一條紀律：**改 `context_shape` 的字串文法**（排序、
改名、加段）等同改 prompt 契約，同一個 commit 要 bump `PROMPT_VERSION`，否則新舊文法的
字串會在同一欄裡互相撞桶。`volume_profile_window_candles` 預設 `0` 的理由不變：
merge 進來不動任何既有 prompt，分段點是「你改 config 那一刻」，由你選。

**相反方向——只加一行預設值不是分段點，drift 比對現在自己知道。** 把
`volume_profile_window_candles: 0` 這一行**加進**一個既有 run 的 config（例如照新版
`hyperliquid.example.yaml` 重抄一份），行為完全沒變。`cli/_drift.py` 對有 typed parser 的區塊
（`risk`／`decision`／`market_data`／`paper_trading.execution`）改比**解析後**的物件而不是原始
YAML（`_block_parsers`）：缺一個 key、`null`、空區塊、把每個預設值都寫出來，解析出來都是同一個
物件，所以這種 no-op 編輯不再印 WARNING、不再在 store 蓋 `config_drift = drift`——反過來從 YAML
刪掉一行預設值、或 `"30"` 改寫成 `30`，也一樣安靜。同一個 key 設成 `30`（真的開啟）仍照報
drift。任一側 parser 讀不了（例如 genesis 帶著已改名的舊 key）就退回原始比對，drift 不會被
例外吞掉。`engine`／`indicators` 沒有宣告的預設值，仍整塊原始比對，任何新 key 照報。

另注意 `context_shape` 描述的是**模型實際看到的 prompt**：視窗開著但該 cycle 的 profile 被
執行期跳過（歷史不夠、零寬度、零成交量，各有一行 WARNING），那個 cycle 會落在「沒有 volume
profile 段」的 shape——這是真的少了一段，不是假訊號；判讀時對照 WARNING 把它們併回去。
（伺服器上跑著的 run 不受影響：`local.yaml` 整檔優先且不進版控，不會自動拿到這個 key。）

判讀時**主判準是提案率**（`requested_target_margin_pct` 非 null 的佔比）。**但這一欄
會低估**：fail-closed 的 cycle 一律把它寫成 NULL，模型實際要求了什麼在 parse 接縫就被
丟掉了，所以「提了案但格式被擋掉」與「根本沒提案」在這一欄完全同形。量提案率時要把
**只可能跟在模型真的給了一個數字 margin 之後**的那六個 tag 的 cycle 加回來算成提案：
`margin_off_step_grid`／`margin_out_of_range`（值本身被拒）、
`flat_with_nonzero_margin`／`directional_side_with_zero_margin`／
`set_target_without_confidence`（三個都排在 `set_target_without_margin` 那道守衛之後，
null margin 到不了）、以及 `margin_quoted_number`（模型寫了數字但加了引號，例如
`"35"`——唯一一個發生在轉型**之前**的成員）。這六個要逐一列出，因為這個集合猜不出來。

**不要**用「margin 轉型成功之後發的 tag」當判準——轉型對 null 也算成功，所以
`invalid_key_risks` 與 `missing_rationale` 在完全沒提案時也到得了（實測：
`maintain_current` + `requested_target_margin_pct: null` + 空 `key_risks`）。也**不要**
直接加 `margin_not_numeric`／`confidence_not_numeric`：這兩個裝的是「根本不是數字」的
字串（照抄的佔位符、`maintain_current` 把 `"null"` 加引號、散文），證明不了任何提案。
`confidence_quoted_number` 同樣**不算**——margin 比 confidence 早轉型，而 null margin 是
**跳過**轉型不是通過，所以零提案時它照樣到得了。少了這個修正，已經恢復提案的模型會被讀成「還是不提案」，剛好在這次改動要
判生死的那個指標上；而 `raw_response` 不落地，事後補不回來。信心分布只能
當輔助，而且必須把 `confidence IS NULL` 當成一個看得見的桶一起畫：fail-closed 的 cycle
存的是 NULL，所以光是換 prompt 就會讓舊段照抄的信心尖峰整批消失——**模型行為完全沒變
也會出現這個變化**，把它讀成「信心散開了」就是被自己的部署騙了。

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
| 落在哪個 prompt 桶 | stderr log 的 `prompt_regime: prompt_version=… context_shape=… format_fingerprint=…`（INFO） | 啟動後第一個寫下 payload 的 cycle 印一行；之後整段 run 不再出現 | 中途又印一行 = 三鍵翻桶了（段落出現／消失，見 §4 與 §7 的 position 列）；對照 `validate` 的 `prompt_regime:` 行，判讀時分段讀 |

**Protection-only 模式**（log 會明講）：不開新 decision cycle，但 SL/TP 與監控
持續；倉位被 SL/TP 了結後程序會做最後 export 並以 exit 1 自動結束。三種成因、
對應動作不同：

- **replay mismatch**：帳本重放對不上——**先調查 store**（`validate` + log），
  不要盲目重啟。
- **健康 resume 缺 API key**：帳本是健康的，只是少了憑證——照 §1.3 設好
  `OPENROUTER_API_KEY` 後重啟即可恢復交易（CLI 的 stderr 訊息也是這麼指示），
  不需要先跑 `validate`。
- **健康 resume 但引擎建不起來**（`EngineConfigError`；成因有二：引擎 import
  失敗，常見是 `.env` 被存成 UTF-16 等壞編碼；或某個 config 值被拒絕，例如
  `TRADINGAGENTS_MAX_TOKENS=8k` 這種壞的 completion 上限）：帳本健康、倉位仍
  活著，只是引擎起不來——照 stderr 印出的錯誤修好環境（重存 `.env` 為 UTF-8、
  或改掉那個被指名的 config key）後重啟即可恢復交易，不需要先跑 `validate`。
  同樣的失敗發生在**空倉** restart 或 fresh `--create` 時則直接具名 exit 1
  （沒有倉位需要看護）。

`api_failed` cycle（網路／API 問題）是預期會偶發的，計入
`api_failed_count`、不進 30 輪門檻；連續大量出現才需要查網路——**先看
`error_type`**：空的那種不是網路，是非 Retryable 例外（通常是程式缺陷），照 §7 那列處理。注意只有
LLM 呼叫**之前**的失敗（連線、warmup、payload 寫入）零 AI 花費——LLM 逾時／
限流類的 api_failed 每次嘗試（最多 3 次）都已產生費用。

`invalid_output` cycle（模型輸出不合契約、fail-closed 成 `maintain_current`）
與 `api_failed` 不同：它**計入** 30 輪門檻，且每筆都會把
`ai_outputs.risk_action` 寫成 `invalid_fail_closed`——那是文件上的 model-drift
告警值（見 phase2-data.md）。`phase2-target-v3` 起這個值會成批出現屬預期
（模型照抄 schema 區塊由「合法」變成「不合法」），不是新故障；真正要看的是它
有沒有隨著時間下降、以及提案率有沒有上來。判斷成因看 `risk_reason`，但要知道它是**多對一**的：照抄記
`invalid_decision_mode`，然而任何落在兩個 enum 值以外的 mode（模型自己編的 `hold`、
大小寫變體）也記同一個值；`invalid_output` 則同時涵蓋「找不到 JSON 區塊」「回覆是空
字串或非字串」與「JSON 解不開」，**包含照抄時順手把兩個數值欄的引號拿掉**——區塊自己
的說明就要求那兩欄寫成裸數字，所以那是最可能的部分遵從形狀。因此
`invalid_decision_mode` 只能當照抄的**代理量**、不能當證據，`invalid_output` 也不等於
structured-output 事故。

## 6. 驗收（約 5 天後）

```bash
# 手動全量 export（可隨時跑，不影響 run）
python -m contrib.hyperliquid_perp export --run-id paper-BTC --output-dir exports/

# 驗收報告：13 項 summary 指標 + 鏈路完整性 + Phase 3 判定
python -m contrib.hyperliquid_perp validate --run-id paper-BTC
```

| `validate` exit | 意義 | 下一步 |
|---|---|---|
| `0` | `cycle_count >= 30` 且 orphan／snapshot／replay mismatch 全為 0 | **可進 Phase 3**——但 `cycle_count` 含 `invalid_output`（那裡量的是「排程有沒有在跑」），而 paper 報告沒有 live 那個 `invalid_output_count` 欄可以拆。下判斷前先看同一份報告的 `order_count`，必要時到 export 的 `decision_attempts.csv` 數 status 分佈：30 個解不開、一單沒下的 run 一樣會印 exit 0 |
| `4` | 資料一致但 cycles 未滿 30；**或 `no_decision_streak` ≥ 3**（最近連續 ≥3 個 cycle 都沒出決策——不分成因，報告會印 `shortfall:` 行說明，並另印 `stale_feed_refusal_streak` 讓你分辨是不是 K 線 feed／時鐘問題；下一個決策 cycle 自動歸零，run 停掉超過 2 個 cycle 後也不再套用） | 繼續掛著跑；stale streak 照 §7 `freshness limit` 列處置，其餘看 `decision_attempts.error_type` |
| `5` | integrity failure（orphan／mismatch／store 壞掉） | 先調查再相信任何結果 |
| `1` | 操作錯誤（db／run 不存在） | 檢查 `--db`／`--run-id` |

報告中的 `warning:` 行（超時 pending funding、config drift）不影響 exit code，
但寫結論前要看過。`prompt_regime:` 行（每組 `(prompt_version, context_shape,
format_fingerprint)` 的 cycle 數，依首見順序，只數計入 `cycle_count` 的 cycle）同樣不影響
exit code：**三鍵齊全的行多於一行**＝這個 run 跨過 prompt 制度邊界，跨段指標要分開讀（§4）；
`n/a` 是該欄在寫入時還不存在（v10 前無 shape、v11 前無 fingerprint），不是另一個制度——
一個 run 跨過 v11 部署點會多出一行 `n/a` 桶，那是同一個制度的舊列。行是**桶**不是**段**：
A→B→A 翻回去仍只印兩行。另有一條自我檢查：各桶總和應等於 `cycle_count`，不等時印
`warning:`（有已決策的 attempt 沒有 `ai_inputs` 列——手工修過的 store），這時分佈是部分的。

**把 v11 前的 `n/a` 桶補回去（issue #163）**：那些列的 payload JSON 存有當次的 `format_instructions`
全文，指紋可以離線重算——

```bash
# 先備份 DB；在寫 payload 的那台主機上跑（列上記的是絕對路徑）；daemon 在跑也可以
# （只寫 v11 前那些列的 NULL 格、單一短交易，daemon 不再碰它們；鎖等超過 5 秒會具名 exit 1，重跑即可）
python -m contrib.hyperliquid_perp export --run-id paper-BTC-3 --output-dir exports/ --backfill-format-fingerprint
```

回填在 export 之前跑，CSV 直接帶新值；stderr 印一行
`format_fingerprint backfill for 'paper-BTC-3': stamped=N pre_v10=N missing_payload=N unreadable=N unverified=N`。
規則：只寫 NULL 格（daemon 寫過的值永遠不會被重算蓋掉，第二次跑 `stamped=0`）；`pre_v10`＝連
`context_shape` 都沒有的列，**不填**（三鍵是一組，半組會變成 `validate` 上多出來的新桶）——**這些列永久留在
`n/a` 桶是接受的現況**（2026-09-03 拍板：不另做 shape 回填工具；paper-BTC-3 自 v10 起跑，只有已封存的
舊 run 有這種列）；payload 檔必須
存在、讀得到、**且** bytes 仍 hash 到該列的 `input_payload_hash`（被改過、截斷、從別處復原的檔不算證據）、
JSON 裡要有字串 `format_instructions`——不符的列保持 NULL 並計數，不猜。回填後 `validate` 對 format 段
沒變過的 run 只剩一行 `prompt_regime:`。它不是 migration（schema 步驟不做檔案 I/O、缺檔要容忍），對象是
**已升到 v11 的 store 裡 v11 前寫的列**——落後的 store 一樣被 `export` 拒開。

## 7. 快速故障排除

| 症狀 | 解法 |
|---|---|
| `OPENROUTER_API_KEY is not set` | 依 §1.3 三種方式擇一設定；`.env` 記得放在 repo 根目錄。 |
| 啟動報 run 已有活著的 process | 單實例鎖。找到舊 process 停掉；若是殘留心跳，等 15 分鐘 lease 過期。 |
| `config not found` / `invalid config` | 對照 example 修 `hyperliquid.local.yaml`；strict 解析會擋未知 key。 |
| `--context-only` 不印倉位行／完整輪報 no usable account equity（exit 1） | `wallet_address` 還是佔位符；填真實唯讀地址。 |
| 太年輕的標的每 4h 一筆 `api_failed` | 市場資料 warmup 不足，暖機完成前屬預期行為。 |
| `context_shape` 少了結尾的 `position` token（prompt 沒有 `Position:` 段），log 有 `position section omitted` | 兩個成因渲染出**完全相同**的 prompt 與 `context_shape`，store 裡沒有欄位分得出（issue #161 拍板：接受現況、不加欄），**只能靠 journald 這行 WARNING 的措辭分辨**——`… the run has no books yet`（`cli._provider`：帳本還沒 seed，生產接線上到不了，出現代表接線或 store 有問題）vs `… account equity … is not positive at mark …`（`domains/perp/marginal_cost`：權益 ≤ 0，該 cycle 省略倉位段是對的，gate 反正也會拒絕方向性目標）。兩句措辭各有測試釘住。 |
| 每 4h 一筆 `api_failed`，`error_type` **空**、`error_message` 以 `non-retryable:` 開頭 | 非 Retryable 例外——**通常是程式缺陷**（store 讀出來的狀態踩到 DTO 守衛、engine 回傳形狀壞掉……），但主機問題也會落到這一列，看 `error_message` 裡的 repr 分辨：`sqlite3.OperationalError`（store 被鎖超過 busy_timeout、檔案系統壞）、`MemoryError` 是主機不是 code（payload 寫檔的 `OSError` 例外——它已被歸類成 `server_error`，走 ladder）。log 同一時間有 ERROR traceback，那才是要修的東西。不會自癒、也**不會讓 daemon 退出**（systemd 的 `NRestarts` 不會動，別拿它當健康訊號）；不走 3 次 ladder，直接 terminal；連續 3 筆起 log 升級 ERROR、`validate` 印 `shortfall:`（exit 4）。修 code（或主機）後部署。live 車道自 Phase 3 起就是這個語意，paper 於 issue #134 對齊。 |
| 每 4h 一筆 `api_failed`，error_message 是 `every technical indicator failed` 或 `… is/are unavailable`（點名 `atr_14`／`ema_20`／`ema_50` 中死掉的那些） | indicator 引擎（stockstats）壞掉或 regime 指標算不出來——三者任一缺席 regime 都會被捏造成 RANGING，daemon 與 one-shot 同樣拒跑（不燒 LLM）。（非空的 `indicators:` 清單漏配三者任一現在直接在 config load 擋下；會走到這裡的 config 成因只剩刻意的 `indicators: []`。）修 stockstats 相容性後，`--context-only` 走同一套 guard：照樣渲染但印同一句 refusal 警告並 exit 4（健康 context 是 exit 0），可拿來免 key 驗證修好了沒。 |
| 每 4h 一筆 `api_failed`，error_message 是 `… freshness limit` | K 線 feed 停止推進。context 的 `as_of` 取自最後一根**收盤** K 線，健康 feed 不足一個 interval 舊；超過 `3 × candle_interval`（夾在下限與上限之間——下限＝`domains/perp/freshness.py` 的 `_MAX_CANDLE_AGE_FLOOR_MS`，目前 30 分鐘；上限＝三個決策 cycle，目前 12 小時；**但上限不會壓到一根健康 K 線以下**：`candle_interval: 1d` 的最新收盤 bar 在一天內會從 0 老化到 24h，所以 1d 的界限是「一根 bar ＋ 一個決策 cycle」＝28 小時——日線 feed 漏掉一根，最新收盤 bar 超過 28h 舊就拒跑（漏掉的 boundary 之後第一或第二個 cycle——相鄰 cycle 間隔超過 4h，寬限窗內最多只落得下一個）。目前出貨的 interval 沒有一個真的被 12 小時上限夾到（會被夾的是「大於一個 cycle、不超過三個」的 bar；4h 剛好是一個 cycle、走 `3 x 4h`，1d 走上面的加寬），所以 `… capped at` 這個由來標籤目前不會印出來。這些數字以程式碼的常數為準，訊息本身也會印出生效上限）就拒跑，不燒 LLM。訊息會印出該根 K 線的收盤時戳、實際年齡、生效上限與上限的由來（`3 x 4h`／`… raised to the 30m floor`／`one 1d bar plus one 4h decision cycle`）。**倉位不會被動到**：拒的只有 4h 一次的新決策，30 秒節奏的 monitor tick（清算／`gap_stop_fill`／SL・TP）照常跑——它讀的是 snapshot 的 mark price，與 K 線 endpoint 是不同資料路徑，K 線停了不代表保護瞎了。**成因只有一個＝feed 沒推進**：K 線視窗本身就是用**交易所自己的時鐘**截的（同一次抓取先讀 public `l2Book` 的 `time`，再以它當 K 線與 funding 視窗的上界，issue #124），年齡也是拿同一個讀數量的，所以主機時鐘偏移既推不動視窗、也進不了年齡——訊息會明寫 feed 沒推進，並附上本機與交易所時鐘的差距供參考（偏移 ≥1 分鐘另有 WARNING 提醒修 NTP，因為排程格線與所有紀錄時戳仍走主機時鐘；但那不是這個拒跑的成因）。查交易所 K 線 API 狀態。**這條拒跑信任交易所自己的 `l2Book` `time`**（2026-08-26 實測是伺服器時鐘、冷門幣也每次前進）：若那個時戳本身壞掉（倒退或停住），視窗會被它截短、症狀與 feed 停滯完全同形——守衛刻意沒有第二個參考時鐘，這是接受的殘餘風險。**不再是完全無聲的無限期狀態**：這類 cycle 的 `error_type` 記成 `stale_market_data`（不再與會自癒的環境失敗共用 `server_error`），連續第 3 筆起 log 從 WARNING 升成 ERROR，`validate` 也會把 `no_decision_streak ≥ 3` 列成 exit 4 的 `shortfall:`（該判準**不分 error class**——feed 停滯、l2Book 掛掉、連線問題一律計入，因為對操作員來說都是「連續 N 個 cycle 出不了決策」；報告另印 `stale_feed_refusal_streak` 供分辨。feed 恢復、下一個 cycle 出得了決策就自動歸零；run 停掉超過 2 個 cycle 後也不再套用，已封存的驗收 run 不會被永久判死）。`--context-only` 走同一套 guard：照樣渲染但印同一句警告並 exit 4。live run 的對應行為見 [RUNBOOK-live.md](./RUNBOOK-live.md)（tick 節奏不同，~10s）。 |
| error_message 是 `… AFTER the exchange's clock … did not come from a live market fetch` | 最新那根 K 線的收盤時戳落在**交易所時鐘之後**——不論多少。live 抓取做不出這種 context：`_build_context` **先**讀交易所時鐘、再以它當 `get_candles` 的視窗上界（`close_time <= end`，issue #124），所以 `as_of` 構造上不會晚於交易所時鐘；本機時鐘再快也只會拿到上一根**已收盤**的 bar（#93 那種「主機快 H、落在 bar 最後 H 內抓到未收盤 bar」的拒跑已不存在，`1m` 到 `1d` 一視同仁，也沒有 1 分鐘容忍值了）。會走到這列只剩：replay／手工 fixture，或 K 線與時鐘取自不同次抓取。若在生產 log 看到，查是不是餵了非 live 的輸入，不是查 NTP（訊息附的時鐘差距只是參考）。這種拒跑仍記成 `stale_market_data`、計入 no-decision streak。 |
| error_message 是 `… AFTER the current time` | 同上一列的反向分支，但走的是**沒有交易所時鐘**的後備路徑（fixture／replay；生產路徑一律帶交易所時鐘，所以正常不會看到這句）。意思是產生這兩個時戳的**兩次讀時鐘之間**本機時鐘跳了（休眠喚醒、NTP step、容器時鐘重新同步）。**方向不固定**：daemon 先讀時鐘再抓資料，觸發的是往**前**跳；one-shot 抓完才讀時鐘，觸發的是往**回**跳——所以訊息只說「跳了」不指方向。 |
| **每一個** cycle 都 `invalid_output`（fail-closed、零下單），且 log 裡不再出現 `structured-output invocation failed` fallback 警告 | 模型的 structured output 成功了，渲染輸出天生不含 Phase 2 target JSON → 解析必失敗。確認 `engine.structured_output` 沒被設成 `true`（perp 預設 false、強制 free-text 路徑）；若真的被設成 `true`，engine config 建構（provider 啟動）時會在 log＋stderr 雙通道發警告——直接搜 `engine.structured_output: true` 即可確認（兩個通道都含這段；`warning: ` 前綴只在 stderr 那份）。gate 生效的正向訊號是 paper/live log 每次 AI 呼叫三行 `structured output disabled by config; using free-text generation` INFO（Portfolio/Research Manager、Trader 各一；重試的 cycle 每次嘗試都會再印一組），看到它們就代表 free-text 路徑在跑。2026-07-27 paper-BTC 換模事故即此成因。**注意 `phase2-target-v3` 起有第二個成因與此症狀完全同形**（structured output 確實關著、三行 INFO 也都在，但每個 cycle 仍解不開）：schema 區塊改成型別非法佔位符後，模型整段照抄會 fail-closed。分辨方式是看 `ai_outputs.risk_reason`。**先注意這一格裡 `invalid_output` 出現兩次而意思不同**：症狀欄講的是 `decision_attempts.status`，兩種事故都是它；能分辨的是 `risk_reason` 這個同名但不同欄的值。照抄多半記 `invalid_decision_mode`，structured-output 事故記 `invalid_output`。但兩邊都不是唯一成因——照抄若照著區塊的指示把兩個數值欄的引號拿掉，也會記 `invalid_output`，與 structured-output 事故完全同形。此時改看 log：那三行 `structured output disabled by config` INFO 還在，就不是 structured-output 事故。 |
| log 出現 `escalating to the supervisor (daemon exit)`，daemon 隨即退出、由監管拉回 | AI 回答之後的某一筆 scheduler 寫入（§3.1 回覆落地或 `ai_outputs` 稽核寫入）**連續 10 次 poll 都失敗**——不是暫時性鎖（那個一兩輪就自癒），而是 SQLite 檔案級的問題：查誰長期握著寫鎖（別的 process、跑很久的 `export`／`validate`）、磁碟是否寫滿、DB 檔或所在目錄是否變成唯讀、WAL 是否卡住。log 裡同一 cycle 前面會有 9 筆帶 traceback 的 ERROR，看它們的例外類型定位。**重啟後的行為依失敗的是哪一筆而不同，兩者都是預期**：落地那筆失敗（回覆從未 durable）→ 該 attempt 走 §3.1 ladder **重問一次 AI**（在 3 次預算內；預算已用完則記 `api_failed`／`interrupted`）；稽核那筆失敗（回覆已 durable、plan 已 commit）→ 重啟 reconciliation 取消那個 plan，resume 後**在新價格重跑一次 gate**。也就是說「絕不重問 AI／絕不重跑 gate」只在 in-process 重試期間成立，逃生之後不成立。這條路徑不會留下 terminal row，所以 `validate` 的 no-decision streak（exit 4）看不到它——訊號是這行 ERROR 與監管的 restart count。 |
| log 出現 `funding backfill for … could not read the rate for …`，帶 traceback，該小時一直是 `pending` | **funding reader 壞了，不是 store 壞了**——這兩句刻意分開（issue #193）。`rate_at` 對「venue 失敗」是回 `None`（安靜地留 pending），所以會走到這行的只剩我方缺陷：呼叫端與 reader 簽章漂移（`TypeError`）、餵了 naive 時鐘（`ValueError`）之類。看 traceback 修 code，**不要去翻 SQLite**——那是另一句 `… (corrupt stored row; fix it in the store to resolve it)` 的意思。這條**不會中斷 backfill pass、也不會讓 daemon 退出**（該 pass 契約上不准 abort：重啟時 abort 會早於 protection-only fork，倉位就變成沒人看管的 crash-loop），事件留 pending、下個 cycle 邊界或重啟再試；修好之前那筆 funding P&L 不計入總額，也絕不捏造。 |
| log 出現 `funding backfill for … hit an unexpected failure on …`＋`no lane claimed this one`，帶 traceback | 同上一列的兜底：backfill 的三條具名 lane 都不認領的例外。這條在的理由就是「不准 abort」不能靠 handler 清單維持——issue #191 正是從清單縫裡穿過去的。**一定是缺陷**，看 traceback 修 code；事件留 pending、pass 照跑完、daemon 不退出。注意 `validate` 目前看不到這種卡住（它只認得出 timestamp 解不開那一種），所以連續幾個 pass 都出現同一句就要當真——`validate` 只會把它算進 6 小時後的泛用 stale pending。 |
| 反覆重啟，且每次 log 都是同一個 `start_plan` traceback | 確定性的引擎缺陷：`start_plan` 的例外刻意不容納（引擎 fail-stop 後拒絕所有呼叫），而已落地的回覆會讓重啟 resume 到同一個 gate → 再爆。監管會一直拉、一直爆，倉位期間由既有 SL/TP 看管但不會有新決策。**這不是自癒模式，照 §5 人工介入**：修掉 traceback 指的缺陷；急救時可停掉 run 後手動清該 attempt 的 `pending_raw_response`（該 cycle 記成失敗、下個 cycle 照排）。 |

更多症狀見 [SETUP §7](./SETUP.md#7-troubleshooting)。
