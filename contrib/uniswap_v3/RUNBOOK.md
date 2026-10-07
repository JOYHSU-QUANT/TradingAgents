# contrib/uniswap_v3 RUNBOOK

把兩個 paper run——對照 run（`trend_vol_weights`）與 AI run（`ai_gated_weights`）——部署到 Lightsail、
掛 systemd timer、每天顧一下、出事時怎麼處理的**操作手冊**。
套件本身的架構與指令說明見 [README.md](./README.md)。

伺服器上的指令都以 `trader` 身分、在 §0 的 checkout 根目錄用 bash 執行，Python 是 checkout 自己的 `.venv/bin/python`。
會讀鏈或問 judge 的指令前面都有 `python -m dotenv run --`，它從 checkout 根目錄的 `.env` 帶入 `ETH_RPC_URL` 與
`OPENROUTER_API_KEY`。本機（Windows、PowerShell）只負責 §1.1 的自我檢查、§1.2 的設定檔與 §1.3 的 store。
在本機用 Windows 工作排程器跑單一 run 的寫法在附錄 B。

---

## 0. 伺服器上的 checkout（固定版本）

與 Hyperliquid paper 同一台 Lightsail（主機與 SSH 見 `contrib/hyperliquid_perp/docs/RUNBOOK.md`），但**各走各的**：

| 東西 | 在哪 |
|---|---|
| checkout | `/home/trader/uniswap-paper`，detach 在一個固定的 commit（不是分支：pull 不會悄悄換版本） |
| Python | `/home/trader/uniswap-paper/.venv`（自己的 venv，不碰 hl-paper 的） |
| 秘密 | `/home/trader/uniswap-paper/.env`（`ETH_RPC_URL`、`OPENROUTER_API_KEY`；chmod 600） |
| 設定檔 | `/home/trader/uniswap-paper/contrib/uniswap_v3/configs/paper-trend.local.yaml`、`paper-ai.local.yaml`（§1.2） |
| store | `/home/trader/data/uniswap/paper.db`（repo 外，升級不碰） |
| log | `/home/trader/data/uniswap/paper-visits.log` |
| judge 的原文 | `/home/trader/data/uniswap/verdicts/<source>/<代幣>-<bar 時間>.json`（sidecar，§2.5）；上游引擎自己的 log 與 cache 在 `/home/trader/data/uniswap/tradingagents/` |
| 排程 | `/etc/systemd/system/uniswap-v3-paper.service`、`uniswap-v3-paper.timer`（系統 unit、`User=trader`，與 `hl-paper` 同型） |
| visit 的設定 | `/home/trader/uniswap-paper/contrib/uniswap_v3/schedule/paper-visit.local.sh`（§3.1） |

hl-paper 的部署（push `deploy/paper` 會 restart `hl-paper`）碰不到這個目錄；這裡的安裝腳本也不碰 hl-paper 的
checkout、venv、service 與 store。兩邊各有自己的 `.env`，OpenRouter 的 key 可以是同一把。
**不要對這個 checkout `git clean -fdx` 或重 clone**：`.env`、兩份 `*.local.yaml`、`paper-visit.local.sh` 與 `.venv` 都在裡面（gitignored），
store 與 log 則在 repo 外、不受影響。

### 0.1 安裝與升級：`lightsail-install.sh`

安裝與升級都是同一條：

```bash
sudo sh lightsail-install.sh <commit>
```

第一次裝的時候伺服器上還沒有 checkout，先把腳本送上去（本機 PowerShell；key 與主機同 hyperliquid 的 RUNBOOK）：

```powershell
scp -i ~/.ssh/<key> contrib/uniswap_v3/schedule/lightsail-install.sh ubuntu@<host>:/tmp/
ssh -i ~/.ssh/<key> ubuntu@<host> sudo sh /tmp/lightsail-install.sh <commit>
```

之後的升級直接用 checkout 裡的那份：`sudo sh ~trader/uniswap-paper/contrib/uniswap_v3/schedule/lightsail-install.sh <commit>`。
`<commit>` 是要跑的版本（develop 上的 merge commit），**寫 commit、不要寫分支名**。從 checkout 裡跑是安全的：整支腳本先讀完才執行，
升級改寫這個檔不影響正在跑的那次。clone 的來源預設是 hyperliquid checkout 的 origin；要指定就 `sudo REPO_URL=<url> sh ...`
（寫在 `sudo` 後面：sudo 會丟掉呼叫者的環境變數），腳本會印出它 clone 的 URL。

它做的事（可重複執行；第二次就是升級）：

1. 記下 timer 原本有沒有在跑，然後停掉它。正在跑的 visit 會先跑完：腳本看到 service 還 active 就把 timer 開回去（原本在跑的話）、
   結束碼 3，等它跑完再來一次。升級避開 visit 的時段（00:10–03:05 UTC）就不會撞到。
2. 以 `trader` 身分：沒有 checkout 就從 hyperliquid checkout 的 origin（伺服器上唯一有 deploy key 的遠端）clone 一份；
   fetch、`git checkout --detach <commit>`（印出原本在哪個 commit，回退用）；沒有 `.venv` 就建；
   `pip install -e ".[dev]" -r contrib/uniswap_v3/requirements.txt`；建 `/home/trader/data/uniswap`；`.env` 不在就寫空白範本（§1.4）、
   `paper-visit.local.sh` 不在就寫好伺服器路徑的那份（§3.1），`.env` 裡哪個 key 不在或沒有值每次都會警告；
   最後跑 `pytest -q -m "not smoke" contrib/uniswap_v3/tests`。
   **這一半任何一步失敗（fetch、pip、測試）就停在那裡**：checkout 可能已在 `<commit>`、timer 是停的（stderr 會說），
   回到上一版＝用印出來的那個 commit 再跑一次這條。
3. 以 root 身分：把 checkout 裡 `contrib/uniswap_v3/schedule/` 的兩個 unit 複製到 `/etc/systemd/system/`、`daemon-reload`，
   然後把 timer **還原成原本的狀態**：原本在跑就 enable＋start 並印下一次 visit 的時間；原本停著（暫停中、§7 修 reorg 中、
   第一次安裝，或上一次升級中途失敗把它留停了）就留著不開、印出 `sudo systemctl enable --now uniswap-v3-paper.timer`。
   升級不決定 visit 跑不跑：上一次失敗後重跑或回退，timer 要自己開回去。

要改 unit（例如把 timer 挪開 hl-paper 問 LLM 的時段）用 drop-in：`sudo systemctl edit uniswap-v3-paper.timer`，`[Timer]` 下先寫一行空的
`OnCalendar=` 清掉原本的再列新的時間；直接改 `/etc/systemd/system/` 裡的檔會被下一次升級蓋掉。

升級後看一下 log（§4）：若是 `failed: the run '...' was started under another config`，新版改了設定預設值，照 §6 開新 run。

---

## 1. 一次性前置

### 1.1 本機：相依與自我檢查

在本機先確認程式、節點與 judge 都通（這些 smoke 測試在伺服器上也能跑，見 §1.4）：

```powershell
pip install -e ".[dev]"                                             # 上游引擎與測試相依（verdict 與 tests/test_upstream_names.py 要）
pip install -r contrib/uniswap_v3/requirements.txt
python -m pytest -q -m "not smoke" contrib/uniswap_v3/tests        # 全綠才繼續
python -m dotenv run -- python -m pytest -q -m smoke contrib/uniswap_v3/tests/test_chain_smoke.py
python -m dotenv run -- python -m pytest -q -m smoke contrib/uniswap_v3/tests/test_agent_smoke.py -s
```

第二條 pytest 打真的節點：`.env` 要有 `ETH_RPC_URL=https://...`（完整 URL，不是只有 key），
而且要是 **archive** 節點（漏跑的 visit 補跑、回補歷史都要讀舊區塊）。
第三條真的問一次 judge（ETH-USD、今天；約 15–20 次 completion，花錢）：`.env` 要有 `OPENROUTER_API_KEY`，
沒有就 skip；它印出評等與耗時，是跑 AI 閘門策略前唯一一次「引擎＋閘道＋key 通不通」的檢查。

### 1.2 設定檔：兩份

兩個 run 各一份設定檔，從範例複製後改 `strategy`（gitignored，本機改好再送上伺服器）：

```powershell
Copy-Item contrib/uniswap_v3/configs/uniswap_v3.example.yaml contrib/uniswap_v3/configs/paper-trend.local.yaml
Copy-Item contrib/uniswap_v3/configs/uniswap_v3.example.yaml contrib/uniswap_v3/configs/paper-ai.local.yaml
```

- `paper-trend.local.yaml`（對照 run）：`strategy` 換成範例裡註解掉的 `trend_vol_weights` 區塊；**不打開 `verdicts`**
  （對照 run 不讀判斷，它的設定快照就是純規則 run 的）。
- `paper-ai.local.yaml`（AI run）：`strategy` 換成 `ai_gated_weights` 區塊，**`verdicts` 區塊打開**（沒有判斷來源的 run
  第一根就 `failed: the strategy refused the bar at …`，而且 run 列已經寫進 store，補上區塊後要換一個 run id）；
  它的 `rule` 要與對照 run 的 `trend_vol_weights` 參數**一字不差**，兩個 run 才比得起來。`verdict` 讀的也是這份。
- `agent` 區塊是 `verdict` 問的 judge（供應商、兩個模型、分析師、completion 上限），預設＝hyperliquid paper 在用的
  （deep `anthropic/claude-sonnet-4-6`、quick `deepseek/deepseek-chat`），**不改**；它不進設定快照。要換模型見附錄 A。
- 其餘兩份都照預設：一天一根 bar、成交在邊界後 25 塊（`execution.delay_blocks`）——排程時間是照這個值排的，
  改大到超過 45 塊（約 9 分鐘）就要把 timer 一起往後挪。

送上伺服器（兩份都送；ubuntu 進不了 trader 的家目錄，先放 `/tmp` 再 `install`）：

```powershell
scp -i ~/.ssh/<key> contrib/uniswap_v3/configs/paper-trend.local.yaml contrib/uniswap_v3/configs/paper-ai.local.yaml ubuntu@<host>:/tmp/
ssh -i ~/.ssh/<key> ubuntu@<host> sudo install -o trader -g trader -m 644 /tmp/paper-trend.local.yaml /tmp/paper-ai.local.yaml /home/trader/uniswap-paper/contrib/uniswap_v3/configs/
```

### 1.3 store：複製本機回補好的那份

策略看到的 view 是 store 裡到當根為止的全部 bar，`trend_vol_weights` 的視窗（`max(trend_window, vol_window + 1)` 根，
範例的 `trend_window` 設 50 就是 50 根）要有夠長的歷史才會進場。所以 **run 開始前歷史就要在 store 裡，之後不要在跑著的 run
後面再 backfill**（visit 自己會補新的 bar；補更早的歷史會讓 paper 當時看到的與事後回測看到的不一樣）。

本機已有回補好的 store 就複製它（用 SQLite 的 backup API，不要直接複製檔案：store 是 WAL 模式，最近寫的可能還在旁邊的 `-wal` 檔裡）：

```powershell
python -c "import sqlite3; sqlite3.connect('file:<本機 store>?mode=ro', uri=True).backup(sqlite3.connect('paper.db'))"
scp -i ~/.ssh/<key> paper.db ubuntu@<host>:/tmp/
ssh -i ~/.ssh/<key> ubuntu@<host> sudo install -o trader -g trader -m 600 /tmp/paper.db /home/trader/data/uniswap/paper.db
```

複製的那份可以帶著舊的 run（回測、本機的 paper）沒關係，新 run 用新的 id。沒有回補好的 store 時在伺服器上回補
（日線從 2022-01-01 到現在約 1,700 根、100 分鐘左右、要 archive 節點；中斷了重跑即可，已讀的跳過）：

```bash
cd ~/uniswap-paper
.venv/bin/python -m dotenv run -- .venv/bin/python -m contrib.uniswap_v3 backfill --config contrib/uniswap_v3/configs/paper-ai.local.yaml --db /home/trader/data/uniswap/paper.db --from 2022-01-01
```

`bars_per_year`、`trend_window`、`vol_window` 都以 bar 為單位：改了設定檔的 `interval_seconds`，或用 `--interval-seconds`
換 bar 長度，這三個要一起改，程式不會替你檢查。

### 1.4 伺服器：`.env` 與自我檢查

安裝腳本寫了空白的 `/home/trader/uniswap-paper/.env`，填進去（`sudo -u trader -H nano /home/trader/uniswap-paper/.env`）：

```
ETH_RPC_URL=https://...
OPENROUTER_API_KEY=sk-or-...
```

安裝腳本每次跑都會對空的 key 警告一次。然後以 `trader` 身分（`sudo -u trader -H bash`、`cd ~/uniswap-paper`）跑 §1.1 的兩條 smoke 測試，Python 換成 `.venv/bin/python`：

```bash
.venv/bin/python -m dotenv run -- .venv/bin/python -m pytest -q -m smoke contrib/uniswap_v3/tests/test_chain_smoke.py
.venv/bin/python -m dotenv run -- .venv/bin/python -m pytest -q -m smoke contrib/uniswap_v3/tests/test_agent_smoke.py -s
```

第二條是伺服器上第一次真的問 judge（ETH-USD）；WBTC 的第一次是 §2 開 run 那天的 `verdict`。

---

## 2. 開兩個 run（手動，一次）

排程的 visit 不帶起始餘額，所以 run 要先手動開；兩個 run 同一天、同一根 bar、同一筆起始資金開，之後才比得起來。
以 `trader` 身分、在 `~/uniswap-paper`，**在 00:10–04:00 UTC 之間**（judge 用伺服器的日期當 trade date，伺服器是 UTC；邊界過了
超過 `agent.ask_within_seconds`＝4 小時 `verdict` 就不問、那根沒判斷，§2.5）：

```bash
P=.venv/bin/python; C=contrib/uniswap_v3/configs; DB=/home/trader/data/uniswap/paper.db
$P -m dotenv run -- $P -m contrib.uniswap_v3 backfill --config $C/paper-ai.local.yaml --db $DB --from "$(date -u +%F)"
$P -m dotenv run -- $P -m contrib.uniswap_v3 verdict  --config $C/paper-ai.local.yaml --db $DB
$P -m dotenv run -- $P -m contrib.uniswap_v3 paper --config $C/paper-trend.local.yaml --db $DB --run-id paper-trend-1 --balance USDC=10000 --gas-eth 1
$P -m dotenv run -- $P -m contrib.uniswap_v3 paper --config $C/paper-ai.local.yaml    --db $DB --run-id paper-ai-1    --balance USDC=10000 --gas-eth 1
$P -m contrib.uniswap_v3 status --config $C/paper-trend.local.yaml --db $DB --run-id paper-trend-1
$P -m contrib.uniswap_v3 status --config $C/paper-ai.local.yaml    --db $DB --run-id paper-ai-1
sudo systemctl enable --now uniswap-v3-paper.timer   # 第一次安裝時腳本只裝 unit、不開 timer
systemctl list-timers uniswap-v3-paper.timer         # 下一次 visit 的時間
```

- 順序就是 visit 的順序（§3）：先把今天的 bar 讀進 store、問判斷，AI run 的第一根才有判斷可讀；對照 run 不讀判斷，先開後開都一樣。
- 新 run 從**最近一個已過的邊界**開始。在 00:00–00:05 UTC 之間開會拿到結束碼 3（成交區塊還沒出現），
  id 不會被佔用，晚點再跑同一條就好。
- `--balance` 每個代幣一個，沒寫的從 0 開始；`--gas-eth` 是另外一筆只拿來付 gas 的 ETH，不算進目標比例。
  **paper 的錢是虛擬的，gas 直接給足**：主網一筆 swap 的 gas 是幾美元起跳，gas 用完之後每次再平衡都會被拒
  （stderr 警告、結束碼仍 0），沒有東西會幫它補。只給 USDC 開就不會在第一根因歷史不夠而賣掉代幣（§1.3）。
- run id `paper-trend-1`／`paper-ai-1` 是 visit 腳本的預設；用別的 id 就改 §3.1 的設定。

排程**不帶** `--balance` 是故意的：`DB` 或 run id 打錯時 visit 會以結束碼 1 失敗
（`there is no run ..., and a new run needs opening balances`），而不是悄悄開一個新 run。

---

## 2.5 問判斷：`verdict`

跑 `ai_gated_weights` 的 run 要有判斷可讀。`verdict` 對設定交易的每個代幣（WETH→ETH-USD、WBTC→BTC-USD）
問一次 TradingAgents graph，以設定的 `verdicts.source` 寫進 store：

```bash
.venv/bin/python -m dotenv run -- .venv/bin/python -m contrib.uniswap_v3 verdict --config contrib/uniswap_v3/configs/paper-ai.local.yaml --db /home/trader/data/uniswap/paper.db
```

- 問的是**最近一個已過的邊界**那根 bar，而且只問這一根：judge 讀的新聞與價格到問的那天為止，隔天再問前一根會看到未來，
  所以 `--at` 指更早的邊界會被拒（結束碼 1），只有 `--fake-rating` 可以配舊邊界；漏問的那天就照「沒判斷」走（README 的 `ai_gated_weights`）。
  那根 bar 要先在 store 裡，而 `paper` 是「讀進來就決策」，所以 AI run 當天的順序是三步：
  `backfill --from <今天的邊界>`（只讀最新那根、不決策；它不在 §1.3「不要在跑著的 run 後面回補」的範圍，因為 run 還沒走到這根）
  → `verdict` → `paper`。bar 不在就 `verdict` 結束碼 3、稍後再跑。排程的 visit 把這三步（與對照 run 的 `paper`）串起來，
  前一條結束碼不是 0 就不跑下一條（§3）。
- **誠實視窗**：judge 讀的新聞與價格到被問的那一刻為止，而 AI run 的成交價是邊界後 25 塊（≈00:05 UTC）的，問得越晚 judge 比成交
  多看越多、對照 run 沒有這個優勢。所以邊界過了超過 `agent.ask_within_seconds`（預設 14400＝4 小時，蓋過三次 visit 的時段；最低 3600，
  第一次 visit 一定來得及）`verdict` 就不問還沒問的代幣：stderr `warning: the boundary ... passed N s ago, more than agent.ask_within_seconds`、
  結束碼 0、不寫列，那些代幣那根照「沒判斷」走；早先已寫進 store 的判斷照用、照印。正常 visit（00:10–00:35）的 5–30 分鐘後見之明是已知取捨。
  `--fake-rating` 不受視窗限制。
- 上游用**本機日期**當 trade date：伺服器是 UTC 沒事；在台灣的本機於 UTC 16:00 之後手動跑，上游會把「今天」算成明天、
  把這次當回測、即時資料源留白。要在本機手動跑就在台北時間 08:10–23:59 之間跑。
- 每個代幣約 15–20 次 completion；實測（2026-10-06，sonnet-4-6 經 OpenRouter）一個代幣約 11 分鐘，兩個代幣一次 visit 抓 20–25 分鐘。
  問過的（source、代幣、bar）**永不改寫**、重跑直接印 `already stored`；judge 中途沒答（閘道、額度、網路、分析師的資料源被限流或掛了）
  結束碼 3、已答的代幣保留、下次只問剩下的。
- 印出每個代幣：評等、模型、耗時、原文存在哪：`WETH (ETH-USD): Buy, model anthropic/claude-sonnet-4-6, 662 s, words in verdicts/tradingagents-rating-v1/WETH-20261006T000000Z.json`。
  sidecar 放在 **store 檔的同目錄**（伺服器上是 `/home/trader/data/uniswap/verdicts/<source>/`），裡面有最終決策全文、各分析師與辯論報告、
  給它看的現貨脈絡（最近收盤、1／7／30 根變動、20 根波動率）、模型與 judge 的設定、耗時；上游引擎自己的 log 與 cache 在同目錄的 `tradingagents/`。
  備份 store 時一起帶走。
- judge 回 `REVIEW`（答了但讀不出評等）照樣寫、stderr 警告一行、不再問；`ai_gated_weights` 把它當沒判斷。
- **演練**：`--fake-rating Buy` 不打模型、直接寫該評等（`model=fake`、沒有 sidecar）。它**只准用在沒有真判斷的 store**：
  store 裡該 source 已有非 fake 的列就結束碼 1——fake 列會永久擋掉那根 bar 真的判斷。排程演練請用另一個 db 與
  `verdicts.source` 不同的設定檔，不要碰 `paper.db`。
- 判斷是 store 的資料、不屬於任何 run：對照 run（`trend_vol_weights`）與 AI run（`ai_gated_weights`）讀同一份；
  對照 run 的設定沒打開 `verdicts`，它看不到、也用不到。

---

## 3. 排程（systemd timer）

`uniswap-v3-paper.timer` 每天 **00:10、01:10、02:10 UTC**（台灣時間 08:10、09:10、10:10）各跑一次 `uniswap-v3-paper.service`，
它以 `trader` 身分在 checkout 根目錄執行 `contrib/uniswap_v3/schedule/paper-visit.sh`，一次 visit 四步、**前一步結束碼 0 才跑下一步**，
visit 的結束碼＝第一個不是 0 的：

1. `paper` 對照 run（不讀判斷，所以 judge 掛了也擋不住它）；
2. `backfill --from <今天的邊界>`：把今天的 bar 讀進 store；
3. `verdict`：問判斷（問過的代幣直接跳過）；
4. `paper` AI run——只有判斷寫進來了才會跑到這一步，所以 AI run 不會在判斷之前就把當根決策掉。

- 第一次 visit 決策 00:00 收盤的那根 bar；後兩次發現已決策，結束碼 0、不寫決策（只會把先前 pending 的讀數核對成 final）。
  第一次若拿到結束碼 3（節點落後、成交區塊還沒到、judge 沒答、store 被鎖住），後兩次就是重試；
  一天最多試三次，三次都失敗的那根，隔天的 visit 會補決策（成交區塊照舊，與準時的一樣；AI run 補的那根多半沒有判斷——那天 `verdict`
  那步沒成功的話——走「沒判斷」政策）。
- 每次最多跑 **55 分鐘**（`TimeoutStartSec`）：兩個代幣的問答約 20–25 分鐘，超時多半是節點或閘道卡住，下一次 visit 會重來。
  還在跑的 visit 不會被下一次 timer 再開一次；`Persistent=true` 補跑主機關機時錯過的那次。
- `RUN_TREND`／`RUN_AI` 留空就跳過該段、log 寫一行；兩個都空是設定錯：log 一行、結束碼 1。
- 這台機器 2 GB 記憶體：judge 的 graph 與 hl-paper 的引擎各是一個 Python 行程，visit 時段兩者可能同時在跑。service 設了
  `OOMScoreAdjust=500`：記憶體不夠時 kernel 先殺 visit（下一次重來、已答的代幣保留），不是 hl-paper。`journalctl` 看到
  `oom-kill` 就用 §0.1 的 drop-in 把 timer 挪開 hl-paper 問 LLM 的時段。

### 3.1 visit 的設定

預設值寫在 `schedule/paper-visit.sh` 開頭，**不要改那個檔**（它被 git 追蹤）。要改的放進同目錄的
`schedule/paper-visit.local.sh`（gitignored，存在就會被讀；安裝腳本第一次會寫好伺服器的路徑），一行一個 `NAME="value"`：

```sh
DB="/home/trader/data/uniswap/paper.db"
LOG="/home/trader/data/uniswap/paper-visits.log"
PYTHON="/home/trader/uniswap-paper/.venv/bin/python"
```

| 變數 | 預設 | 要不要改 |
|---|---|---|
| `RUN_TREND` | `paper-trend-1` | 與 §2 開的對照 run 一致；留空就不跑對照 run |
| `RUN_AI` | `paper-ai-1` | 與 §2 開的 AI run 一致；留空就不跑 backfill／verdict／AI run |
| `CONFIG_TREND` | `contrib/uniswap_v3/configs/paper-trend.local.yaml` | 通常不用 |
| `CONFIG_AI` | `contrib/uniswap_v3/configs/paper-ai.local.yaml` | 通常不用（`verdict` 與 `backfill` 也讀這份） |
| `DB` | `contrib/uniswap_v3/data/paper.db` | 伺服器上改成 `/home/trader/data/uniswap/paper.db` |
| `LOG` | `contrib/uniswap_v3/data/paper-visits.log` | 伺服器上改成 `/home/trader/data/uniswap/paper-visits.log` |
| `PYTHON` | `python3` | 伺服器上改成 `.venv/bin/python` 的完整路徑 |

相對路徑以 checkout 根目錄為準（visit 在那裡跑）。這個檔會被 `.`（source）進腳本，**只放設定行、值加引號**
（有空白的路徑不加引號會讀不到）；寫了 `exit` 會讓 visit 不跑、也不留 log。log 每次 visit 的第一行會印出用的 run id、`DB` 與 `PYTHON`。

### 3.2 操作

```bash
systemctl list-timers uniswap-v3-paper.timer                 # 下一次／上一次 visit
systemctl status uniswap-v3-paper.service --no-pager         # 上一次 visit 的結束碼（status=0/SUCCESS、status=3、result 'timeout'）
journalctl -u uniswap-v3-paper.service -n 30 --no-pager      # systemd 看到的（visit 自己的輸出在 log 檔，§4）
sudo systemctl start uniswap-v3-paper.service                # 現在立刻跑一次 visit（不等 timer）
sudo systemctl stop uniswap-v3-paper.timer                   # 暫停排程（正在跑的 visit 不會被停）
sudo systemctl start uniswap-v3-paper.timer                  # 恢復
sudo systemctl disable --now uniswap-v3-paper.timer          # 移除排程
```

`systemctl status` 的結束碼：

| 值 | 意思 |
|---|---|
| `0` | 跑完了 |
| `1` | 要修，看 log（§5） |
| `3` | 稍後再試，當天後面的 visit 會重試 |
| `4` | visit 腳本進不了 checkout 目錄、寫不了 log，或 `PYTHON` 的路徑不存在，visit **沒有跑**；或 log 在 visit 中途變成寫不了：前面幾步跑了、後面沒跑（§5） |
| `127` | `PYTHON` 不是路徑而且 PATH 上找不到（§3.1） |
| `result 'timeout'` | 跑超過 55 分鐘被 systemd 停掉了（§5） |

---

## 4. 日常檢查

```bash
tail -n 40 /home/trader/data/uniswap/paper-visits.log
cd ~/uniswap-paper
.venv/bin/python -m contrib.uniswap_v3 status --config contrib/uniswap_v3/configs/paper-trend.local.yaml --db /home/trader/data/uniswap/paper.db --run-id paper-trend-1
.venv/bin/python -m contrib.uniswap_v3 status --config contrib/uniswap_v3/configs/paper-ai.local.yaml    --db /home/trader/data/uniswap/paper.db --run-id paper-ai-1
.venv/bin/python -m contrib.uniswap_v3 report --db /home/trader/data/uniswap/paper.db --run-id paper-trend-1
.venv/bin/python -m contrib.uniswap_v3 report --db /home/trader/data/uniswap/paper.db --run-id paper-ai-1
```

- log 每次 visit 有一行 `==== <UTC 時間> visit of "paper-trend-1" and "paper-ai-1" (db ..., python ...)`、四步各自的輸出、一行 `==== exit <碼>`。
  `verdict` 那步每個代幣一行評等與耗時（§2.5）。
- `status --run-id` 從 `run paper-ai-1: ...` 那行算起的第三行，說 run 跟不跟得上時鐘：`up to date` 是最近一個
  已過的邊界已決策；`behind: N boundary(ies) ...` 是有 N 根已過但還沒決策——對照 run 到 00:10、AI run 到 00:3x（等判斷）之間是 1、正常；
  **過了 03:05 UTC（第三次 visit 的時限）還是 behind**，就去 log 看那天的 visit 為什麼沒成功。
  run 還沒決策過任何一根時沒有這一行。
- 接著是持倉、價值、報酬（與 `report` 同一個算法：扣掉累計 gas）、最近幾筆決策，
  每筆附「邊界後多久決策的」——準時的應該是 `00:10:xx`（對照 run）與 `00:3x:xx`（AI run，等判斷）；`1d ...` 表示是隔天補決策的。
- 設定檔有 `verdicts` 區塊時（AI run），bar 列表後多一行 `verdicts from <source>: N of the latest M bar(s) have one for every token (...)`，
  是最近 M 根**成得了 bar 的**邊界裡每個代幣都有判斷的根數與各代幣各自的根數（列表裡 `incomplete` 的邊界不算，策略永遠不會決策它）；
  每筆決策另附 `verdicts: WBTC=none, WETH=Buy`：每個交易代幣當時 view 帶的判斷。`none`＝那根 bar 這個代幣沒有判斷，
  `REVIEW`＝判斷沒給出評等；兩者 `ai_gated_weights` 都走「沒判斷」的政策（覆蓋率行把 `REVIEW` 算成有判斷）。
  `changed`＝store 裡現在的判斷與決策看到的不同（事後補進、改了或刪了，與 §5 的 `now have other verdicts` warning 同一件事）。
  評等照 run 自己的設定快照查；`--config` 給別的 run 的設定時，上面的覆蓋率行查的是那份設定的 source（給對照 run 的設定就整行不印），
  所以 `status --run-id` 要給該 run 自己的設定檔。
  `ai_gated_weights` 的 band 沿用規則的：判斷把目標砍到持倉 band 以內（例如 4% 的持倉收到 Sell）要等別的代幣漂出 band
  才一起賣，`status` 會看到 `WETH=Sell` 旁邊還有持倉，是設計。對照 run 不印這些。
- `report` 對 AI run 多印一行 `verdicts from <source>: N decided bar(s) saw one on every traded token, M on some, K on none`
  （`REVIEW` 算看到，不進 M 與 K；它在 §5 的 `saw no rating` warning 與 `status` 的 `verdicts:` 裡看得到；suspect 而 skip 的 bar 不算）：
  K 與 M 只會從漏跑的日子與過了誠實視窗的日子累加；成績單（記錄滿 30 根後比較兩個 run 的 `report`）看這行知道 AI 實際有判斷可用的根數。
- `status` 與 `report` 不讀鏈，隨時可以跑；要避開的只有對同一個 store 寫入的事（§7 的刪列與回補、在 `paper.db` 上直接跑回測；§9 是在複本上跑）。

---

## 5. log 裡看到這些時

| 看到 | 意思 | 做什麼 |
|---|---|---|
| `==== exit 0` | visit 跑完了：決策了、這根已經決策過，或鏈在邊界沒有答案（前面會有 `warning: the chain had no answer`，見下面那一列） | 沒事；有 warning 就照那一列 |
| `exit 0` 前有 `warning: ... rebalance(s) were rejected` | 報價低於 `max_slippage` 容許的下限，或 gas 不夠；這根不再平衡 | 不重試，是設計；gas 不夠的話見 §6 |
| `warning: ... skipped as suspect` | bar 的收盤價與 TWAP 偏離太大（或被 reorg），不交易 | 沒事；連續很多根就看一下 `status` 的 flags |
| `warning: the chain had no answer at the boundary` | 池子在那個邊界讀不到 TWAP | 下一次 visit 會再問；run 走過去之後就永遠不決策它 |
| `warning: ... no longer on the final chain` | 先前存的讀數被 reorg | 見 §7 |
| `warning: ... now have other verdicts in the store than their decisions saw` | 某些已決策的 bar，store 裡的判斷後來變了（多半是決策時沒判斷、事後才補進） | 決策不改、照常跑；要讓判斷生效就開新的 run 重放 |
| `warning: N bar(s) decided now, ... saw no rating (no verdict, or a REVIEW) on some traded token` | AI run 這次決策的 bar 裡有代幣沒評等：judge 回了 `REVIEW`、補決策漏跑的日子（那些 bar 沒問過）、或那天 `verdict` 過了誠實視窗沒問；`ai_gated_weights` 對那個代幣不加新風險（suspect 而 skip 的 bar 不算在內） | 沒事；`status --run-id` 看是哪根哪個代幣。每天都出現就看 `verdict` 那步 |
| `try again later: ...` 接 `==== exit 3` | 節點落後、成交區塊還沒出現、或節點回錯誤 | 當天後面的 visit 會重試；**一整天三次都是 3** 就查節點（額度、URL、服務狀態） |
| `try again later: the store ... (database is locked)` 接 `==== exit 3` | 有別的程式開著同一個 store：同時在跑回測、用 DB 工具開著，或前一次 visit 被 systemd 停掉、它啟動的 python 還沒結束 | 關掉它（`pgrep -af contrib.uniswap_v3`）；當天後面的 visit 會重試 |
| `failed: there is no run 'paper-ai-1', and a new run needs opening balances` | run id 或 `DB` 打錯，或 run 還沒開 | 對照 §2、§3.1 |
| `failed: the run 'paper-ai-1' was started under another config` | 設定檔改了，或新版程式改了預設值 | run 只能在開它的設定下接續：見 §6 開新 run |
| `failed: ... the clock is behind` | 這台機器的時鐘早於 run 已走到的邊界 | 校時 |
| `failed: the strategy refused the bar at ... (ai_gated_weights reads verdicts, and the view carries none ...)` | 策略讀判斷，設定檔卻沒有 `verdicts` 區塊 | 設定補上 `verdicts`，用新的 run id 開 run（原 run 已開在沒有判斷的設定下） |
| `verdict`：`warning: the boundary ... passed N s ago, more than agent.ask_within_seconds (14400 s)` 接 `exit 0` | 邊界過了超過 4 小時才跑到 `verdict`（主機關機錯過 visit、過了 04:00 才開機補跑，或下午手動跑）；還沒問的代幣不問、那根沒判斷，已寫進的判斷照用 | 沒事，是設計（§2.5）；排程時段內就出現的話，看前面幾步為什麼拖那麼久 |
| `==== RUN_TREND and RUN_AI are both empty` 接 `==== exit 1` | `paper-visit.local.sh` 把兩個 run id 都留空 | 填回去（§3.1） |
| `verdict`：`try again later: the store has no bar at ...` 接 `exit 3` | 當天的 bar 還沒進 store（前一步的 `backfill` 沒讀到，多半是節點落後） | 當天後面的重試會補；順序見 §2.5 |
| `verdict`：`try again later: the judge did not answer on ETH-USD (...)` 接 `exit 3` | 原因可能會過：閘道回 402（額度）、408、429、5xx，連線／逾時類錯誤，或分析師的資料源被限流／掛了（括號裡是 `VendorRateLimitError`／`VendorUnavailableError` 之類）；已答的代幣已寫進 store | 當天後面的重試只問剩下的；**一整天都是 3** 就看括號裡的例外：閘道的就查 OpenRouter 額度與服務狀態，資料源的就查該資料源 |
| `verdict`：`failed: the judge cannot be built (ValueError: API key for provider 'openrouter' is not set. Please set ...)` | `.env` 沒有供應商的 key | 補 key（§1.4） |
| `verdict`：`failed: the judge failed on ... for good (...)` | 供應商回其他 4xx（模型名打錯、key 無效、請求格式不對）或引擎自己出錯（KeyError 之類），重試也一樣、還會先花掉分析師的呼叫 | 對照 `agent` 區塊的模型名與 key；是引擎的錯就看括號裡的例外 |
| `verdict`：`failed: the bar at ... is not the latest whose boundary has passed` | 想補問舊的 bar | 不補：舊 bar 的判斷會看到未來；那天照「沒判斷」走 |
| `verdict`：`failed: verdict writes under the source the config's verdicts section names, and the config has no verdicts section` | `CONFIG_AI` 指到沒打開 `verdicts` 的設定檔 | 對照 §1.2、§3.1 |
| `verdict`：`failed: the store holds verdicts of '...' given by [...]` | 在有真判斷的 store 上用了 `--fake-rating` | 演練換 scratch store（§2.5） |
| `verdict`：`warning: the judge gave no rating on WBTC (REVIEW)` | judge 答了但讀不出評等；已寫成 `REVIEW`、不再問 | 沒事；`ai_gated_weights` 當沒判斷處理。常發生就看 sidecar 的 `decision` |
| `failed: paper needs the packages in contrib/uniswap_v3/requirements.txt` | `PYTHON` 不是裝了相依的那個 venv | 改 §3.1 的 `PYTHON` |
| `==== there is no ".../python": fix PYTHON ...` 接 `==== exit 4` | §3.1 的 `PYTHON` 路徑打錯 | 改 `paper-visit.local.sh` |
| `python3: not found` 接 `==== exit 127` | `PYTHON` 不是路徑而且 PATH 上沒有 | 改 §3.1 的 `PYTHON` 成 `.venv/bin/python` 的完整路徑 |
| `Error: Invalid value: Invalid value for '-f' ".../.env" does not exist.` 接 `==== exit 2` | checkout 根目錄沒有 `.env` | 補上 `.env`（§1.4） |
| 其他 `usage: ...` 接 `==== exit 2` | visit 的指令被改壞 | 對照 git 版的 `paper-visit.sh`；設定只放在 `paper-visit.local.sh` |
| 有 `==== ... visit of` 卻沒有 `==== exit`，`systemctl status` 是 `result 'timeout'` | visit 跑超過 55 分鐘被 systemd 停掉；印到一半的輸出還在 | 多半是節點或閘道卡住；下一次 visit 會重來 |
| 有 `==== ... visit of` 與前幾步的輸出、沒有 `==== exit`，`systemctl status` 是 `status=4` | log 在 visit 中途變成寫不了（磁碟滿、權限被改）；寫得了的那幾步已經跑過，後面的沒跑 | 檢查磁碟與 `/home/trader/data/uniswap` 的權限；下一次 visit 會補 |
| 這次 visit 完全沒有留下任何一行，`systemctl status` 是 `status=4` | visit 腳本進不了 checkout 目錄，或 log 寫不了（目錄不是 trader 的、建不了） | 檢查 `/home/trader/data/uniswap` 的擁有者與權限；visit 沒有跑 |

---

## 6. 重設：開一個新的 run

這些情況要開新 run（舊的 run 留在 store 裡，`report` 仍可查）：設定檔改了、新版程式改了設定預設值、
想換起始餘額、gas 用完了。兩個 run 要比得起來，換設定就兩個一起換、一起重開。

1. `status --run-id <舊 id>` 抄下 `holdings after the bar at ...` 那一行的持倉。
2. 照 §2 用**新的 run id**、抄下的餘額（或你要的新餘額）開 run。新舊 run 之間沒有自動銜接。
3. 改 `schedule/paper-visit.local.sh` 的 `RUN_TREND`／`RUN_AI`。

要整個重來就換一個 store 檔（或刪掉 `paper.db`），從 §1.3 開始。判斷隨 store 走：換 store 就沒有舊的判斷了。

---

## 7. 資料缺漏怎麼處理

- **漏跑的 visit**：不用處理。下一次 visit 從 run 最後決策的下一根開始補讀、逐根決策，
  每根都在自己的成交區塊報價（要 archive 節點）。AI run 補的那幾根多半沒有判斷（judge 只問最新一根，而且過了視窗就不問），走「沒判斷」政策，
  log 會有 §5 的 `saw no rating` warning。
- **邊界沒答案**（TWAP revert）：store 不寫那根；visit 會在下次再問。run 已經走過它之後就不會回頭決策，
  `report` 與回測的 summary 會數到這種缺口。
- **reorg 的讀數**：visit 核對 pending 的讀數時發現 close block 不在最終鏈上，會把它標成 `reorged`
  （不刪、不重抓）。已經在它上面做的決策照舊；之後的新 run 會把它當可疑 bar 略過。要重抓：
  1. `sudo systemctl stop uniswap-v3-paper.timer`，並先備份 `paper.db`（§9 第一條的寫法）。
  2. 刪掉那幾列：
     ```bash
     .venv/bin/python -c "import sqlite3; c = sqlite3.connect('/home/trader/data/uniswap/paper.db'); print(c.execute('DELETE FROM bars WHERE finality = ?', ('reorged',)).rowcount); c.commit()"
     ```
  3. 對那段日期跑一次 §1.3 的 `backfill --from <那天> --to <那天>`，再 `sudo systemctl start uniswap-v3-paper.timer`。

  跑著的 run 對那根的決策不會變；之後對同一段跑回測會警告「decided earlier now read differently」。
  主網 merge 之後超過 25 塊深的 reorg 極少見。

---

## 8. 換 RPC 供應商

1. 改 `.env` 的 `ETH_RPC_URL`（或在設定檔的 `rpc.url_env` 改用另一個變數名稱）。
   `rpc` 不在 run 的設定快照裡，換了不用開新 run。
2. 新的節點要是 **archive**。用 §1.4 的 smoke 測試確認讀得到。

---

## 9. 驗證：paper 與事後回測一致

paper 決策過幾根之後，可以對同一段跑一次報價級回測，確認決策相同（約束：回測與紙上交易對同一段 bar
做出相同的決策）。在 store 的**複本**上跑，不動排程用的那份；避開 visit 時段（00:10–03:05 UTC）。對照 run 的例子
（AI run 一樣，設定檔與 run id 換掉；回測重放 store 裡記錄的判斷、不重問）：

```bash
cd ~/uniswap-paper
.venv/bin/python -c "import sqlite3; sqlite3.connect('file:/home/trader/data/uniswap/paper.db?mode=ro', uri=True).backup(sqlite3.connect('/home/trader/data/uniswap/check.db'))"
.venv/bin/python -m dotenv run -- .venv/bin/python -m contrib.uniswap_v3 backtest --config contrib/uniswap_v3/configs/paper-trend.local.yaml --db /home/trader/data/uniswap/check.db --run-id check-1 --fills quoter --from <run 的第一根> --to <最後決策的那根> --balance USDC=10000 --gas-eth 1
.venv/bin/python -c "import sqlite3; c = sqlite3.connect('/home/trader/data/uniswap/check.db'); print(c.execute('SELECT p.time, p.outcome, b.outcome FROM decisions p LEFT JOIN decisions b ON b.time = p.time AND b.run_id = ? WHERE p.run_id = ? AND (p.outcome IS NOT b.outcome OR p.target IS NOT b.target)', ('check-1', 'paper-trend-1')).fetchall())"
```

第一條讀不到 store 時會報錯（`unable to open database file`），那就不要往下跑。
起始餘額要與 paper run 開的時候相同。第三條印 `[]` 就是逐根一致（回測少決策的那根也會被列出來）；
兩個 run 的 `report` 裡報酬與成本也應該相同（成交區塊相同，所以報價相同）。用完可以刪掉 `check.db`。

---

## 10. 分叉沙盒：fork run（本機）

fork run 用 store 裡的 bar（同回測），但每根要交易的 bar 都在本機 anvil 分叉上真的簽名送出：先把分叉重設
（`anvil_reset`）到這根 bar 的成交區塊、把開發帳戶的 ETH 與代幣餘額灌成 run 的帳本，再送；送前送後都對帳。
用的是 anvil 的開發帳戶（公開的 test 助記詞），不碰任何真的錢包；分叉在 anvil 關掉時就消失。在本機（PowerShell）做。

### 10.1 跑一個 fork run

1. 裝好 Foundry（§10.3 第 1 條），`anvil --version` 確認。
2. store 裡要有那段 bar（§1.3 的 `backfill`；在跑著 paper 的 store 上跑 fork 也可以，但建議用複本，§1.3 的 PowerShell `backup` 寫法）。
3. 另開一個視窗起 anvil，分叉在哪一塊都可以（每根要交易的 bar 會自己重設到它的成交區塊；節點要 archive）：

   ```powershell
   python -m dotenv run -- powershell -Command 'anvil --fork-url $env:ETH_RPC_URL --fork-block-number <區塊> --port 8545 --silent'
   ```

   anvil 會把 `--fork-url` 印在自己的錯誤訊息裡，那就是含 API key 的 URL：這個視窗的輸出不要貼到別處。
4. 跑（新 run 要給起始餘額；`--fork-url` 不給就是 `http://127.0.0.1:8545`，主機必須寫字面的 `127.0.0.1` 或 `::1`）：

   ```powershell
   python -m contrib.uniswap_v3 fork --config contrib/uniswap_v3/configs/paper-trend.local.yaml --db contrib/uniswap_v3/data/check.db --run-id fork-1 --from <第一根> --to <最後一根> --balance USDC=10000 --gas-eth 1
   python -m contrib.uniswap_v3 report --db contrib/uniswap_v3/data/check.db --run-id fork-1
   ```

   簽名的帳戶與 swap 的 deadline 可以在設定檔加 `fork:` 區段（`account: 0`–`9`、`deadline_seconds`；範例檔裡是註解）。
   加了這個區段設定快照就會變，**跑著 paper 的設定檔不要加**，另複製一份給 fork 用。
5. 同一個指令再跑一次不會重送任何交易（決策過的 bar 不再決策）。結束碼同回測，另外：錢包與帳本不符、
   run 有未結 send、URL 不是本機的 anvil 分叉都是 1。

### 10.2 有未結 send 時

某根 bar 的 swap 送到一半中斷（送出失敗、交易 revert、送完對帳不符）時，`fork` 指令印 `failed: ...` 並列出：

- `open send at the bar ...`：哪根 bar、送了幾腿成交；`stopped by:` 是中斷的原因（含交易 hash）。
- 每一腿寫下的成交、bar 之前的帳本、「帳本套用這些腿再扣掉失敗那筆的 gas」應有的持有量、錢包現在的持有量，
  以及兩者一不一致。失敗那筆的 gas 不知道時不比 ETH。

這個 run 不會再往前走，重跑也只會再印一次同樣的對照（不送任何交易）。處理：

1. anvil 還開著、沒重設過的話，錢包的持有量就是鏈上真正的結果；用它（或你要的新餘額）照 §10.1 開**新的 run id**。
2. anvil 已經關掉或重設過，錢包的讀數就沒有意義了（印出來的會是重設後的狀態）；看寫下的腿自己判斷，一樣開新 run。

### 10.3 驗收測試

1. 裝 Foundry（要能分叉現在的主網；舊版 anvil 會用舊的硬分叉規則，gas 不準）：從
   [Foundry 的 GitHub releases](https://github.com/foundry-rs/foundry/releases) 下載 `foundry_<版本>_win32_amd64.zip`，
   核對同頁的 `.sha256`，解到 `%USERPROFILE%\.foundry\bin` 並放進 PATH 最前面。`anvil --version` 確認。
2. 跑（測試自己起 anvil、分叉在固定的歷史區塊，所以節點要 archive；跑完自己關掉）：

   ```powershell
   python -m dotenv run -- pytest -m smoke contrib/uniswap_v3/tests/test_chain_fork_smoke.py -s
   ```

   五個測試全過＝驗收成功。前三個是 `ChainExecutor` 本身：第二個是來回——每一筆 swap 後，錢包的輸入代幣、
   輸出代幣與 ETH 各自變動的量與成交的 `swap.amount_in`、`amount_out`、`gas_cost_eth` 一致；第三個確認報價低於
   底線的 swap 被拒、什麼都沒送。`-s` 會印每筆 swap 實際用的 gas 與 QuoterV2 估計的差
   （`execution.quote.gas_overhead_units` 代表的就是這個差）。
   後兩個是 fork run（先從節點回補兩根日線）：第四個確認錢包最後逐位元等於帳本、第一腿的成交量等於 QuoterV2 在
   同一個成交區塊的報價（與 paper 同一塊），重跑不送任何交易；第五個故意讓第二腿失敗（要求兩倍的最低成交量，
   報價達不到、什麼都不送），確認記成 `partial`、錢包仍等於帳本、重跑不送交易。
3. 沒有 `ETH_RPC_URL` 或 PATH 上沒有 `anvil` 時，測試會略過（skipped）而不是失敗。
4. 自己起 anvil、在程式裡用 `open_fork` 連的話，URL 的主機要寫字面的 `127.0.0.1`（或 `::1`）：
   `localhost` 這類名稱會被拒絕（hosts 檔可以把它改指到別處）；連線也不走環境變數設的 proxy。

---

## 附錄 A：換 judge 的模型

`agent` 的預設（deep `anthropic/claude-sonnet-4-6`、quick `deepseek/deepseek-chat`）＝hyperliquid paper 在用的，兩邊成績單才可比；
要換就**兩邊同一天一起換**（hyperliquid 換段的時候）。`agent` 不進設定快照、每筆判斷的列與 sidecar 都記著模型，
所以換模型**不用開新 run**；換的那天在這裡記一行當分段點，成績單分段看。換之前：

1. **確認 OpenRouter 的 slug**：對照 OpenRouter 的模型頁，名字打錯是 4xx、`verdict` 結束碼 1（§5 的 `for good`）。
2. **會「思考」的模型（Sonnet 5.5 之類）**：`agent.max_tokens` 是每次 completion 的上限、**含 thinking token**，8192 會被綁住、
   答案被截斷而讀不出評等（看起來是 `REVIEW` 變多、sidecar 的 `decision` 斷在半句）。要一起調高（例如 16384 起），
   目前沒有偵測「上限綁住」的機制，換完頭幾天看 sidecar。
3. **temperature**：這類模型對非預設的 `temperature` 回 400。上游的 `TRADINGAGENTS_*` 環境變數（temperature、辯論回合等）
   會覆蓋設定而且**不在 sidecar 裡**（重現性缺口），伺服器的 `.env` 不要設它們。
4. **先在 scratch store 上真問一次**：複製一份 store（§9 第一條）、設定檔 `verdicts.source` 換個名字（例如 `tradingagents-rating-v1-try`），
   跑 `verdict` 看評等讀不讀得出來、耗時多少（`--fake-rating` 不打模型，驗不到這些）。
5. 只有 `paper-ai.local.yaml` 的 `agent` 要改（`verdict` 只讀它）；對照 run 不受影響。

換過的紀錄：（還沒換過；2026-10-07 開 run 時維持 sonnet-4-6）

---

## 附錄 B：本機 Windows 工作排程器（單一 run）

在本機跑**一個**不讀判斷的 run（`fixed_weights`、`trend_vol_weights`）的寫法；`ai_gated_weights` 要 §3 的四步串接，
而且兩個代幣的問答就要 20–25 分鐘、加上 backfill 與 paper 就超過這裡 25 分鐘的時限，**不要在 Windows 上拿這個 task 跑它**
（paper 會在判斷寫進來之前就把當根決策掉）。指令都用 PowerShell、在排程 worktree 的根目錄執行。

### B.1 排程專用的 worktree

排程跑的是它所在目錄當下 checkout 的程式碼。放在平常開發、`git pull` 的主 checkout 裡，
之後任何一張 PR merge 後一 pull，跑著的 paper 就悄悄換了版本；若新版改了設定預設值，
下一次 visit 就是結束碼 1（`started under another config`）。所以排程只從一個**專用、固定版本**的
worktree 跑，要升級時才刻意升級。在主 checkout 的根目錄：

```powershell
git fetch origin
git worktree add --detach ..\TradingAgents-uniswap-paper origin/develop
Copy-Item .env ..\TradingAgents-uniswap-paper\.env
Set-Location ..\TradingAgents-uniswap-paper
```

store 與 log 會放在它的 `contrib\uniswap_v3\data\`（gitignored）。**升級**（要用新版程式時才做）：

```powershell
Disable-ScheduledTask -TaskName 'uniswap-v3-paper'
Stop-ScheduledTask -TaskName 'uniswap-v3-paper'      # 正在跑的 visit 也停掉，才不會換程式換到它腳下
git fetch origin
git checkout --detach origin/develop
python -m pytest -q -m "not smoke" contrib/uniswap_v3/tests
if ($LASTEXITCODE -ne 0) { throw 'tests failed: go back to the commit you came from' }
Enable-ScheduledTask -TaskName 'uniswap-v3-paper'
Start-ScheduledTask -TaskName 'uniswap-v3-paper'
```

**不要刪這個 worktree 或對它 `git clean -fdx`**：`data\` 裡是 store 與 log，而它是 gitignored——
`git worktree remove` **不會問、直接連它一起刪掉**。真的要移掉這個 worktree，先用 §1.3 的 PowerShell `backup` 寫法把
`paper.db` 備份到 worktree 外面（log 直接複製）。

升級後看一下 log：若是 `failed: the run '...' was started under another config`，新版改了設定預設值，照 §6 開新 run。

### B.2 開 run 與掛排程

設定檔一份（本機只跑一個 run）、store 回補到 `contrib\uniswap_v3\data\`（或照 §1.3 用 `backup` 複製一份現成的）：

```powershell
Copy-Item contrib/uniswap_v3/configs/uniswap_v3.example.yaml contrib/uniswap_v3/configs/paper.local.yaml
New-Item -ItemType Directory -Force contrib/uniswap_v3/data
python -m dotenv run -- python -m contrib.uniswap_v3 backfill --config contrib/uniswap_v3/configs/paper.local.yaml --db contrib/uniswap_v3/data/paper.db --from 2022-01-01
```

`paper.local.yaml` 的 `strategy` 換成 `trend_vol_weights`（example 的 `fixed_weights` 是佔位策略）。
run 照 §2 的 `paper ... --balance USDC=10000 --gas-eth 1` 開（Python 換成本機的、路徑換成 `contrib\uniswap_v3\data\`）。
記下這個 Python 的完整路徑（`(Get-Command python).Source`）：排程用的要是同一個 `python.exe`，repo 的 `.venv` 不一定裝了 web3。

`schedule/paper-visit.xml` 每天 **00:10、00:40、01:10 UTC**（台灣時間 08:10、08:40、09:10）各跑一次
`schedule/paper-visit.cmd`（只跑 `paper` 一步）：前一次還在跑就不開新的；每次最多跑 25 分鐘；電腦關機錯過的 visit，開機後會補跑一次。
預設值寫在 `paper-visit.cmd` 開頭，**不要改那個檔**；要改的放進同目錄的 `schedule\paper-visit.local.cmd`（gitignored），一行一個：

```bat
set "PYTHON=C:\Users\you\AppData\Local\Programs\Python\Python312\python.exe"
set "RUN_ID=paper-1"
```

| 變數 | 預設 | 要不要改 |
|---|---|---|
| `RUN_ID` | `paper-1` | 與開的 run 一致 |
| `CONFIG` | `contrib\uniswap_v3\configs\paper.local.yaml` | 通常不用 |
| `DB` | `contrib\uniswap_v3\data\paper.db` | 通常不用 |
| `LOG` | `contrib\uniswap_v3\data\paper-visits.log` | 通常不用 |
| `PYTHON` | `python`（排程用你的 PATH） | 建議改成完整路徑；要是 `python.exe`，不能是 `.cmd`／`.bat` |

相對路徑以 repo 根目錄為準（visit 在那裡跑）。這個檔**只放 `set` 行**：`setlocal` 會讓設定失效，`exit` 會讓 visit 不跑、也不留 log。
log 每次 visit 的第一行會印出用的 `DB` 與 `PYTHON`。註冊：

```powershell
$here = [System.Security.SecurityElement]::Escape((Get-Location).Path)
$xml = (Get-Content -Raw contrib\uniswap_v3\schedule\paper-visit.xml).Replace('C:\path\to\TradingAgents', $here)
Register-ScheduledTask -TaskName 'uniswap-v3-paper' -Xml $xml
Start-ScheduledTask -TaskName 'uniswap-v3-paper'          # 立刻跑一次試試
Get-ScheduledTaskInfo -TaskName 'uniswap-v3-paper'        # 等它跑完再看 LastTaskResult（見下）
Get-Content contrib\uniswap_v3\data\paper-visits.log -Tail 20
```

`LastTaskResult`：

| 值 | 意思 |
|---|---|
| `0` | 跑完了 |
| `1` | 要修，看 log（§5） |
| `3` | 稍後再試，當天後面的 visit 會重試 |
| `4` | visit 腳本進不了 repo 目錄、寫不了 log，或 `PYTHON` 的路徑不存在，visit **沒有跑**：`PYTHON` 打錯改 `paper-visit.local.cmd`；log 寫不了就檢查 `data\` 與 log 檔的權限（唯讀、被別的程式鎖住、目錄建不了） |
| `9009` | 找不到 `PYTHON`（`'python' is not recognized ...`，中文 Windows 是「不是內部或外部命令」）：改成完整路徑 |
| `267009` | 還在跑，等一下再查 |
| `267014` | 跑超過 25 分鐘被排程停掉了（log 有 `==== ... visit of` 卻沒有 `==== exit`） |
| `2147942402` | 找不到 visit 腳本：worktree 搬了或刪了，重新註冊 |

- 跑的時候會閃一下命令列視窗（task 是「只在使用者登入時執行」）。不想看到，可以在工作排程器 GUI
  把它改成「不論使用者登入與否均執行」。
- 停用／恢復／移除：`Disable-ScheduledTask`、`Enable-ScheduledTask`、
  `Unregister-ScheduledTask -TaskName 'uniswap-v3-paper' -Confirm:$false`。
- 日常檢查、log 的意思、重設、資料缺漏與 §9 的驗證都同 §4–§9，把路徑換成 `contrib\uniswap_v3\data\`、`systemctl stop` 換成
  `Disable-ScheduledTask`、`pgrep` 換成工作管理員裡找 `python.exe`、`paper-visit.local.sh` 換成 `.local.cmd`、`RUN_TREND`／`RUN_AI`
  換成 `RUN_ID`；visit 時段是 00:10–01:35 UTC，`status` 的「過了 01:35 還是 behind」才要看 log。
