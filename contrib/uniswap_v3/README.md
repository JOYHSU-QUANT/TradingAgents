# contrib/uniswap_v3

Uniswap v3 現貨的執行架構：策略只回答「目標比例是多少」，引擎負責把目標變成 swap。
同一個引擎換接線，就能跑歷史回測、紙上交易，之後是主網分叉沙盒與實盤。

這個套件**不決定策略**。內建的只有一個佔位策略 `fixed_weights`（固定比例＋偏離帶），
用途是驅動引擎與測試；它不讀價格走勢、不做預測。

它與 `contrib/hyperliquid_perp`、`contrib/autoresearch`、`contrib/replay` 完全隔離：
互不 import（`tests/test_isolation.py` 釘住），store 是自己的 SQLite 檔，也不碰
`deploy/paper` 的部署。

操作步驟（開 run、掛排程、出事怎麼辦）見 [RUNBOOK.md](./RUNBOOK.md)。

---

## 模式

| 模式 | 指令 | bar 從哪來 | 成交 | 現況 |
|---|---|---|---|---|
| backtest | `backtest --fills model` | store 裡回補好的歷史 bar | 離線模型：收盤價扣池子費率、`execution.model.slippage`，gas＝固定單位 × 該 bar 的 base fee | 可用 |
| backtest | `backtest --fills quoter` | 同上 | 每根 bar 的成交區塊上對 QuoterV2 做歷史 `eth_call`（要 archive 節點） | 可用 |
| paper | `paper` | 每次 visit 從鏈上讀新收盤的 bar 寫進 store | 同 `--fills quoter`，不送交易 | 可用 |
| fork | — | — | `chain/swaps.py` 的 `ChainExecutor`：在本機 anvil 主網分叉上簽名送出 | executor 與防線已做、還沒接進引擎（下一步）；分叉上的來回驗收見 RUNBOOK §10 |
| live | — | — | 主網 | 還沒做（另開計畫） |

`start_run` 目前拒絕 fork 與 live run（兩者都從鏈上成交，而引擎還不接會簽名的 executor），所以不會建出打不開的 run。

backtest 與 paper 沒有私鑰、不簽交易。會簽名的只有 `ChainExecutor`，而且只在分叉上：

- 只接受主機是字面 loopback IP（127.0.0.1、::1）的 URL——`localhost` 這類名稱不收（hosts 檔可以改指）；
  節點的 `anvil_nodeInfo` 還要寫著它從哪個 URL 分叉，未分叉的 anvil 不收。anvil 分叉沿用主網的 chain ID，
  所以不能靠 chain ID 分辨分叉與主網（`chain/fork.py`）。
- 只用 anvil 公開的 test 助記詞推導出的 10 個開發帳戶簽名；送交易的 `TransactionSender` 拿到其他帳戶就拒絕，
  程式沒有接受其他私鑰的入口。
- 每筆 swap 把 allowance 設成剛好的量（多的也調回來）；`amountOutMinimum` 就是 swap 的 `min_amount_out`
  （與其他 executor 同一條底線），送出前先在最新區塊報價，低於底線就拒絕、什麼都不送。deadline 取 pending
  區塊的時間（節點的時鐘），閒置的 anvil 最新區塊時間不會走。
- `Rejection`＝錢包沒動；有交易上鏈後才失敗丟 `SendError`（不是 `ChainError`，讀取端的處理接不到它）：
  只花了 gas 是 `SwapNotFilled`，swap 上鏈了但結果讀不出來是 `SwapOutcomeUnknown`，收據沒來是 `TransactionUnconfirmed`。
- 成交來源 `chain` 只屬於 fork／live run，fork／live run 也只能用它；在引擎能記下「第一腿上鏈、後腿失敗」之前，
  `open_engine` 拒絕任何會簽名的 executor。

---

## 架構

### 一個引擎、不同接線

`engine/step.py` 的 `Engine.step` 是唯一的決策與成交流程，每種模式都走它；模式只決定
注入哪個 executor、bar 從哪來。回測與紙上交易對同一段 bar 做出相同的決策
（`tests/test_paper.py` 釘住：paper 與事後的 `backtest --fills quoter` 逐筆相同）。

一根 bar 的 step：

1. 這個 run 已經決策過這根 → 回報「已跑過」，什麼都不寫（重跑無害）。
2. bar 可疑（收盤價與 TWAP 偏離超過門檻、讀數被 reorg、各池 close block 不一致）→
   記成 `skipped_suspect` 與原因，不問策略、不成交。
3. 策略回 `Hold` → 只記估值。
4. 策略回目標比例 → 排出 swap，每一腿交給 executor；**全部成交且 gas 餘額夠才一起套用**，
   否則整筆不做、記成 `rejected` 與原因，下一根由策略重新決定。

每根決策過的 bar 都有一筆 decision 與一筆估值，同一個交易寫入。

### Ports（`ports.py`）

| Port | 做什麼 | 實作 |
|---|---|---|
| `Strategy` | `decide(view, portfolio) -> TargetWeights \| Hold`。**策略進入系統的唯一入口** | `strategies/fixed_weights.py` |
| `Executor` | `execute(swap, bar) -> Fill \| Rejection`，並宣告成交來源 | `engine/executors.py` 的 `ModelExecutor`、`QuoteExecutor`；`chain/swaps.py` 的 `ChainExecutor`（分叉上簽名，還沒接進引擎） |
| `Journal` | run、decision、帳本的讀寫 | `store/repository.py` 的 `Store` |
| `Quoter`、`GasOracle`、`BlockLocator` | 報價、base fee、時間→區塊 | `chain/` |

### bar 與成交時點（無前視）

- bar 邊界 T（預設每天 UTC 00:00）的**收盤**＝時間戳 ≥ T 的第一個區塊 B 的前一塊（B−1）結束時的池子狀態。
- 價格以 USDC 計：ETH/USD 取 USDC/WETH 0.05% 池，BTC/USD＝ETH/USD × WBTC/WETH 0.05% 池。
- 策略看到的 view＝store 裡到這根為止的**全部** bar，沒有之後的。
- **成交**一律取在 B＋`execution.delay_blocks`（預設 25 塊，約 5 分鐘）。這個區塊由 bar 決定、
  與 visit 什麼時候跑無關，所以晚到或補跑的 visit 與準時的成交在同一塊。
- 路徑不尋路：池子在設定裡必須成一棵以計價代幣為根的樹，任兩個代幣之間的路徑唯一
  （USDC↔WBTC 是經 WETH 的兩跳、一筆 swap）。

### 目錄

```
contrib/uniswap_v3/
  cli.py, __main__.py   python -m contrib.uniswap_v3 <backfill|status|backtest|paper|report>
  config.py             讀 YAML（凍結 dataclass）；run 會存一份設定快照
  constants.py          以 chain ID 分表的代幣、池子、QuoterV2 與 SwapRouter02 地址
  ports.py              上表的 Protocol
  domain/               純邏輯：價格換算、bar、路徑、帳本、紀錄、績效（只 import 標準函式庫）
  strategies/           registry 與佔位策略 fixed_weights
  engine/               step、回測迴圈 replay、兩個 executor
  chain/                web3 讀取：區塊、池子價格與 TWAP、QuoterV2、base fee；
                        分叉防線與開發帳戶（fork.py）、簽名送出（transactions.py）、ChainExecutor（swaps.py）
  store/                SQLite schema（含版本號與 migration）與讀寫
  backfill.py           把一段 bar 從 archive 節點讀進 store
  paper.py              paper 的一次 visit
  schedule/             Windows 工作排程器的 task 與 visit 腳本（本機設定放 *.local.cmd）
  data/                 （gitignored）排程的 store 與 log
  configs/              設定範例
  tests/                全部用 fake 或錄好的回應；打真節點的只有標成 smoke 的
```

### Store

一個 SQLite 檔（`--db`），用 `application_id` 標記，別的程式建的檔會被拒絕。
`bars` 是市場資料、不屬於任何 run；`runs`、`decisions`、`fills`、`valuations` 是每個 run 寫的，
四種模式同一組表，所以 `status`／`report` 對任何 run 都能用。decision 以（run、bar 邊界）為鍵，
同一根 bar 不會決策兩次；它也記下決策當下 bar 的 close block hash 與 finality，以及
`decided_at`（決策那次呼叫的時間；schema v4 之前的列是空的）。
舊版的 store 會在任何指令第一次打開時自動升級。

---

## 指令

都從 repo 根目錄執行。會讀鏈的指令（`backfill`、`paper`、`backtest --fills quoter`）要節點 URL，
用 `python -m dotenv run --` 從 `.env` 帶進來。

| 指令 | 做什麼 | 讀鏈 |
|---|---|---|
| `backfill --config C --db D --from 2022-01-01 [--to …] [--dry-run]` | 把一段 bar 讀進 store；可重複執行，已有的不重讀 | 是（archive） |
| `status --config C --db D [--bars N] [--run-id R]` | store 的範圍與最近 N 根 bar；加 `--run-id` 再印該 run 的持倉、價值、報酬與最近 N 筆決策（含決策時間）；paper run 另印跟不跟得上時鐘 | 否 |
| `backtest --config C --db D --run-id R --from … [--to …] [--fills model\|quoter] [--balance USDC=10000 … --gas-eth 0.5]` | 用 store 的 bar 跑回測；新 run 要給起始餘額 | 只有 `--fills quoter` |
| `paper --config C --db D --run-id R [--balance … --gas-eth …]` | paper 的一次 visit：補讀上次之後的 bar 並逐根決策 | 是 |
| `report --db D --run-id R` | 報酬、最大回撤、周轉、成本拆解，並列「起始持倉不動」與「全放 USDC」兩個對照組 | 否 |

結束碼：

| 碼 | 意思 | 排程該怎麼做 |
|---|---|---|
| 0 | 跑完了（含「這根已經決策過」、成交被拒、邊界沒答案；後兩者 stderr 有警告） | 不用動 |
| 1 | 跑不下去，原樣重跑也不會好：設定、store、範圍、節點設定、時鐘落後於 run | 看 log、修好 |
| 2 | 命令列打錯（argparse） | 修排程的指令 |
| 3 | 稍後再跑可能就好：節點連不上、落後（還沒到邊界或成交區塊）、回了錯誤，或 store 被別的程式鎖住 | 稍後再跑（排程一天三次就是為了這個） |
| 4 | 只有排程的 visit 腳本會給：進不了 repo 目錄、寫不了 log，或 `PYTHON` 的路徑不存在，visit 沒有跑 | 看 RUNBOOK §5 |

---

## 設定與環境變數

複製 `configs/uniswap_v3.example.yaml` 成同目錄的 `*.local.yaml`（gitignored）再改；
每個鍵的意思寫在範例檔的註解裡。代幣與池子只能用 `constants.py` 裡的名字，設定檔不能給地址；
不認得的鍵會被拒絕。數字一律加引號（YAML 的浮點數在讀進來前就已經掉位數）。

| 環境變數 | 用途 |
|---|---|
| `ETH_RPC_URL` | 節點 URL（含 API key，程式不會印出來）。設定的 `rpc.url_env` 可以改成別的變數名稱 |

回補、`backtest --fills quoter`、以及漏跑過幾天的 paper visit 都要 **archive** 節點。

相依：`pip install -r contrib/uniswap_v3/requirements.txt`（PyYAML、web3；刻意不放進 repo 根目錄的
`requirements.txt`，那份是 Hyperliquid paper 伺服器在裝的）。

---

## 加一個策略

1. 寫一個類別，實作 `ports.Strategy` 的 `decide(view, portfolio)`：回 `TargetWeights`
   （涵蓋設定的全部代幣、非負、總和恰為 1）或 `Hold`。
   答案只能取決於這兩個參數——不讀時鐘、不用亂數、不保留上次呼叫的狀態、不讀外部資料——
   這樣同樣的 bar 在回測、paper、分叉上才會做出同樣的決策。
2. 給它一個 `from_params(params)` 工廠，自己檢查參數（參考 `FixedWeights.from_params`）。
3. 在 `strategies/registry.py` 的 `_FACTORIES` 登記名字。
4. 設定檔寫 `strategy: {name: <名字>, params: {...}}`。

策略拋例外或回答不合格，run 會停在那根 bar、不記 decision，修好後重跑會從那根接著決策。

---

## 已知限制

- 只有 Ethereum mainnet、USDC／WETH／WBTC 兩個池子。
- bar 預設一天；`--interval-seconds` 可以更短，但回測每根重建 view 是 O(n²)，小時線以下要先改。
- `EARLIEST_BAR_TIME` 是 2022-01-01（之前 WBTC/WETH 池的 30 分鐘 TWAP 讀不到）；加第三個池子前要改成每池各自的下限。
- 一個 run 只往前走：漏掉的 bar 之後被補回，原 run 不會回頭決策；設定一改、成交來源一換，都要開新 run
  （起始餘額要手動帶，沒有新舊 run 的銜接）。
- 被拒的再平衡不重試，下一根由策略重新決定。
- 各腿各自報價，不計彼此對同一個池子的衝擊（成交略樂觀）。
- paper 的成交區塊在 visit 當下還不是 final；之後 reorg 的話同一區塊號的報價可能不同，目前不偵測。
- reorg 的 bar 讀數沒有重抓的指令（處理方式見 RUNBOOK）。
- 主網 gas 對小資金很重：每筆 swap 是幾美元起跳。
- `ChainExecutor` 還沒接進引擎：多腿再平衡在鏈上不是原子的，第一腿上鏈、後腿失敗時引擎還記不下來，
  所以 `open_engine` 拒絕它；`fork` 指令與鏈上餘額對帳也還沒做。
- 報價成交的 gas 加成（`execution.quote.gas_overhead_units`，預設 50,000）：分叉上實測 swap 交易本身比 QuoterV2 的
  估計多 18k–58k gas，另外每次 approve 約 46k–55k，預設值沒改。
