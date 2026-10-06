# contrib/uniswap_v3

Uniswap v3 現貨的執行架構：策略只回答「目標比例是多少」，引擎負責把目標變成 swap。
同一個引擎換接線，就能跑歷史回測、紙上交易，之後是主網分叉沙盒與實盤。

這個套件**不決定策略**：策略只透過 `Strategy` port 進來。內建三個：

- `fixed_weights`：佔位策略（固定比例＋偏離帶），用途是驅動引擎與測試；它不讀價格走勢、不做預測。
- `trend_vol_weights`：規則策略。代幣收盤在均線之上才持有，持有比例照波動率目標配置，其餘留在計價代幣。
  規則與參數的定義在 `strategies/trend_vol_weights.py` 的 docstring。
- `ai_gated_weights`：AI 閘門策略。拿 `trend_vol_weights` 的目標當護欄，每個代幣再乘上該代幣在這根 bar 的判斷
  （store 的 `verdicts`，見下）評等對應的倍率（範例：Buy 1／Overweight 0.75／Hold 0.5／Underweight 0.25／Sell 0）。
  沒判斷或 `REVIEW` 時不加新風險：目標＝目前佔比（截到權重的四位精度）、上限是規則的權重，該賣的照賣；不在趨勢上的代幣一律 0，
  所以每個代幣的目標永遠 ≤ 規則的目標。band 沿用規則的：判斷把目標砍到持倉 band 以內（例如小持倉收到 Sell）要等下次
  再平衡才一起賣。設定檔要有 `verdicts` 區塊，沒有的話第一根就 `failed: the strategy refused the bar at …`。
  政策與參數的定義在 `strategies/ai_gated_weights.py` 的 docstring。

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
| fork | `fork` | 同 backtest | `chain/swaps.py` 的 `ChainExecutor`：每根要交易的 bar 先把本機 anvil 分叉重設到該 bar 的成交區塊、把錢包灌成帳本的餘額，再簽名送出 | 可用（RUNBOOK §10） |
| live | — | — | 主網 | 還沒做（另開計畫） |

`start_run` 拒絕 live run（真錢的交易還沒做），所以不會建出打不開的 run。

backtest 與 paper 沒有私鑰、不簽交易。會簽名的只有 `ChainExecutor`，而且只在分叉上：

- 只接受主機是字面 loopback IP（127.0.0.1、::1）的 URL——`localhost` 這類名稱不收（hosts 檔可以改指）；
  節點的 `anvil_nodeInfo` 還要寫著它從哪個 URL 分叉，未分叉的 anvil 不收。anvil 分叉沿用主網的 chain ID，
  所以不能靠 chain ID 分辨分叉與主網（`chain/fork.py`）。
- 只用 anvil 公開的 test 助記詞推導出的 10 個開發帳戶簽名；送交易的 `TransactionSender` 拿到其他帳戶就拒絕，
  程式沒有接受其他私鑰的入口。
- 每筆 swap 把 allowance 設成剛好的量（多的也調回來）；`amountOutMinimum` 就是 swap 的 `min_amount_out`
  （與其他 executor 同一條底線），送出前先在最新區塊報價，低於底線或池子沒答案就拒絕、什麼都不送；
  approve 或 swap 的 gas 估計說會 revert（還沒送出任何東西時）也是拒絕。deadline 取 pending
  區塊的時間（節點的時鐘），閒置的 anvil 最新區塊時間不會走。
- `Rejection`＝錢包沒動；有交易上鏈後才失敗丟 `SendError`（不是 `ChainError`，讀取端的處理接不到它）：
  只花了 gas 是 `SwapNotFilled`，swap 上鏈了但結果讀不出來是 `SwapOutcomeUnknown`，收據沒來或讀不到是 `TransactionUnconfirmed`。
- 分叉只認 `open_fork` 開的：自己手建的 `Fork` 不保證在本機，`ChainExecutor` 只會再確認它是 anvil 分叉。
- 成交來源 `chain` 只屬於 fork／live run，fork／live run 也只能用它；會簽名的 executor 一定帶著它的錢包
  （`ports.Wallet`）一起交給 `open_engine`，其他 executor 不帶。
- 錢包每根 bar 交易前後都和帳本對帳（每個代幣的 `balanceOf` 與 ETH 餘額，逐位元相等），不一致就停下（fail closed）。

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

第 4 步的「一起套用或都不做」只對虛擬成交（model、quoter）成立。會簽名的 executor（fork）上鏈的那一腿已經動了錢包，
所以改成：

1. 錢包準備好這根 bar（分叉重設到成交區塊、灌入帳本餘額），持有量必須等於帳本，否則什麼都不送就停。
2. 送第一筆之前先在 store 寫下這根 bar 的 **send**（`sends` 表），每一腿成交就寫一筆（`sent_legs` 表）。
3. 某一腿被拒（被拒＝那一腿什麼都沒送）就停在那裡：一腿都沒成交記 `rejected`，有成交的記 **`partial`**，
   已成交的腿照實套用，下一根由策略從那裡重新決定。
4. 送完再對帳一次：錢包必須等於「帳本套用已成交的腿」。decision 寫入時同時結清 send。

其他任何讓這一步中斷的狀況（送出失敗、對帳不符）都讓 send 保持未結、記下原因（有的話連交易 hash 與已花的 gas），
並丟 `UnsettledSend`。有未結 send 的 run 不再往前走，那根 bar 也不會重新決策——重跑不會重送交易。
`fork` 指令遇到時會把寫下的腿與錢包現在的持有量並列印出，交給人處理（RUNBOOK §10）。

### Ports（`ports.py`）

| Port | 做什麼 | 實作 |
|---|---|---|
| `Strategy` | `decide(view, portfolio) -> TargetWeights \| Hold`。**策略進入系統的唯一入口** | `strategies/fixed_weights.py`、`strategies/trend_vol_weights.py`、`strategies/ai_gated_weights.py` |
| `Executor` | `execute(swap, bar) -> Fill \| Rejection`，並宣告成交來源 | `engine/executors.py` 的 `ModelExecutor`、`QuoteExecutor`；`chain/swaps.py` 的 `ChainExecutor`（分叉上簽名） |
| `Wallet` | `prepare(bar, ledger)`、`holdings()`：會簽名的 executor 從哪個錢包交易，引擎拿它對帳 | `chain/wallet.py` 的 `ForkWallet` |
| `Journal` | run、decision、帳本、send 的讀寫 | `store/repository.py` 的 `Store` |
| `Quoter`、`GasOracle`、`BlockLocator` | 報價、base fee、時間→區塊 | `chain/` |

### bar 與成交時點（無前視）

- bar 邊界 T（預設每天 UTC 00:00）的**收盤**＝時間戳 ≥ T 的第一個區塊 B 的前一塊（B−1）結束時的池子狀態。
- 價格以 USDC 計：ETH/USD 取 USDC/WETH 0.05% 池，BTC/USD＝ETH/USD × WBTC/WETH 0.05% 池。
- 策略看到的 view＝store 裡到這根為止的**全部** bar，沒有之後的。
- **成交**一律取在 B＋`execution.delay_blocks`（預設 25 塊，約 5 分鐘）。這個區塊由 bar 決定、
  與 visit 什麼時候跑無關，所以晚到或補跑的 visit 與準時的成交在同一塊。fork run 也把分叉重設到這一塊再送，
  所以第一腿遇到的池子與 paper／報價回測的報價完全相同（之後的腿會受前一腿影響）。
- 路徑不尋路：池子在設定裡必須成一棵以計價代幣為根的樹，任兩個代幣之間的路徑唯一
  （USDC↔WBTC 是經 WETH 的兩跳、一筆 swap）。

### 目錄

```
contrib/uniswap_v3/
  cli.py, __main__.py   python -m contrib.uniswap_v3 <backfill|status|backtest|paper|fork|report>
  config.py             讀 YAML（凍結 dataclass）；run 會存一份設定快照
  constants.py          以 chain ID 分表的代幣、池子、QuoterV2 與 SwapRouter02 地址
  ports.py              上表的 Protocol
  domain/               純邏輯：價格換算、bar、路徑、帳本、紀錄、績效（只 import 標準函式庫）
  strategies/           registry、佔位策略 fixed_weights、規則策略 trend_vol_weights、AI 閘門策略 ai_gated_weights、
                        三者共用的 rebalance（參數集檢查、band、權重位數、再平衡觸發）
  engine/               step、回測迴圈 replay、兩個 executor
  chain/                web3 讀取：區塊、池子價格與 TWAP、QuoterV2、base fee；
                        分叉防線與開發帳戶（fork.py）、簽名送出（transactions.py）、ChainExecutor（swaps.py）、
                        fork run 的錢包（wallet.py）
  store/                SQLite schema（含版本號與 migration）與讀寫；bar 與判斷（verdict）的載入
  backfill.py           把一段 bar 從 archive 節點讀進 store
  paper.py              paper 的一次 visit
  fork_run.py           fork run，以及未結 send 與錢包的對照
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
會簽名的 run 另寫 `sends`（每根開始送的 bar 一列，記下中斷的原因與已知花掉的 gas）與 `sent_legs`
（每一腿成交就寫），fork run 在 `runs.fork_block` 記下建立當時分叉所在的區塊（schema v5；status／report 的標頭會印，
但成交不在那一塊——每根要交易的 bar 都重設到自己的成交區塊，成交記錄的 `block` 才是實際的區塊）。
`verdicts` 是外部判斷：某個 source 對某代幣在某根 bar 的五級評等（`Buy`／`Overweight`／`Hold`／`Underweight`／`Sell`，
讀不出評等時是 `REVIEW`）＋模型、prompt 版本、問的時間、原文 digest 與 sidecar 位置；同 `bars` 不屬於任何 run，
寫入後不改。設定檔有 `verdicts.source` 的 run，策略拿到的 view 帶該 source 對交易代幣（計價代幣除外）的判斷（只到被決策的那根為止），
每筆 decision 在 `verdict_digests` 記下當時看到的判斷 digest——`{}` 是「有讀判斷但那天沒有」，NULL 是「這個 run 不讀判斷」
（schema v6）。重放時已決策的 bar 若 store 裡的判斷與記下的 digest 不同，像 bar 讀數變了一樣只計數、警告，決策不改。
讀判斷的策略是 `ai_gated_weights`；目前沒有指令會寫判斷，寫入端（問 TradingAgents）是下一張 PR。
舊版的 store 會在任何指令第一次打開時自動升級。

---

## 指令

都從 repo 根目錄執行。會讀鏈的指令（`backfill`、`paper`、`backtest --fills quoter`）要節點 URL，
用 `python -m dotenv run --` 從 `.env` 帶進來。

| 指令 | 做什麼 | 讀鏈 |
|---|---|---|
| `backfill --config C --db D --from 2022-01-01 [--to …] [--dry-run]` | 把一段 bar 讀進 store；可重複執行，已有的不重讀 | 是（archive） |
| `status --config C --db D [--bars N] [--run-id R]` | store 的範圍與最近 N 根 bar；設定檔有 `verdicts` 區塊時另印最近 N 根有判斷的覆蓋率；加 `--run-id` 再印該 run 的持倉、價值、報酬與最近 N 筆決策（含決策時間，讀判斷的 run 附每筆看到每個交易代幣的評等：`WBTC=none, WETH=Buy`，store 裡的判斷與決策看到的不同——事後補進、改了或刪了——印 `changed`）；paper run 另印跟不跟得上時鐘 | 否 |
| `backtest --config C --db D --run-id R --from … [--to …] [--fills model\|quoter] [--balance USDC=10000 … --gas-eth 0.5]` | 用 store 的 bar 跑回測；新 run 要給起始餘額 | 只有 `--fills quoter` |
| `paper --config C --db D --run-id R [--balance … --gas-eth …]` | paper 的一次 visit：補讀上次之後的 bar 並逐根決策 | 是 |
| `fork --config C --db D --run-id R --from … [--to …] [--fork-url http://127.0.0.1:8545] [--balance … --gas-eth …]` | 用 store 的 bar 跑 fork run：每根要交易的 bar 在本機 anvil 分叉上簽名送出，前後對帳；有未結 send 時印出對照、結束碼 1 | 只讀分叉（分叉向 archive 節點取狀態） |
| `report --db D --run-id R` | 報酬、最大回撤、周轉、成本拆解，並列「起始持倉不動」與「全放 USDC」兩個對照組 | 否 |

結束碼：

| 碼 | 意思 | 排程該怎麼做 |
|---|---|---|
| 0 | 跑完了（含「這根已經決策過」、成交被拒、邊界沒答案；後兩者 stderr 有警告） | 不用動 |
| 1 | 跑不下去，原樣重跑也不會好：設定、store、範圍、節點設定、時鐘落後於 run；fork 的錢包與帳本不符、run 有未結 send | 看 log、修好 |
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
2. 給它一個 `from_params(params)` 工廠，自己檢查參數（參考 `FixedWeights.from_params`；
   `strategies/rebalance.py` 有現成的 `require_params`、`require_band`、`require_decimal`（有界 Decimal），
   權重要截到四位、餘數給計價代幣就用 `floor_weight`／`weights_with_quote`，要「偏離超過 band 才再平衡」
   就用 `rebalance_or_hold`，別自己再寫一份）。
3. 在 `strategies/registry.py` 的 `_FACTORIES` 登記名字。
4. 設定檔寫 `strategy: {name: <名字>, params: {...}}`。

策略拋 `ValueError`（不能決策）或回答不合格，run 會以 `failed: …` 停在那根 bar（不能決策時是
`the strategy refused the bar at …`）、不記 decision，修好後重跑會從那根接著決策；其他例外原樣冒出。

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
- fork run 每根要交易的 bar 都重設分叉、從帳本灌錢包：鏈上狀態不跨 bar 延續，帳本才是真相、分叉只負責
  每根 bar 的成交與對帳。重設會讓 anvil 重新向 archive 節點取狀態，每根要交易的 bar 多花幾秒到幾十秒。
- fork 錢包的代幣餘額是直接寫進代幣 storage 的：只認 Solidity `mapping(address => uint256)` 放在前 64 個 slot 的代幣
  （USDC、WETH、WBTC 都是），找不到就拒絕。
- fork 錢包的 ETH 在簽名前就檢查（gas 上限 × 最高費率，比實際花費嚴）：不夠就是那一腿被拒、什麼都沒送，記成
  `reason_code=gas` 的 `rejected`（前面有腿成交就是 `partial`），與回測的 gas 不足同一個碼與警告。只有 approve
  已上鏈、swap 才付不起 gas 時會停在未結 send。
- 未結 send 沒有自動結清的指令：照 RUNBOOK §10 看對照、用錢包的持有量開新 run。
- fork 的成交含 approve 的 gas（每筆約 46k–55k），報價成交不含，所以 fork 的 gas 成本會比 paper 高。
- 報價成交的 gas 加成（`execution.quote.gas_overhead_units`，預設 50,000）：分叉上實測 swap 交易本身比 QuoterV2 的
  估計多 18k–58k gas，另外每次 approve 約 46k–55k，預設值沒改。
