# contrib/uniswap_v3 RUNBOOK

把 paper run 掛到 Windows 工作排程器、每天顧一下、出事時怎麼處理的**操作手冊**。
套件本身的架構與指令說明見 [README.md](./README.md)。

所有指令都用 PowerShell、在 §0 建的**排程 worktree 的根目錄**執行。會讀鏈的指令前面都有
`python -m dotenv run --`，它從根目錄的 `.env` 帶入 `ETH_RPC_URL`。

---

## 0. 排程專用的 worktree

排程跑的是它所在目錄當下 checkout 的程式碼。放在平常開發、`git pull` 的主 checkout 裡，
之後任何一張 PR merge 後一 pull，跑著的 paper 就悄悄換了版本；若新版改了設定預設值，
下一次 visit 就是結束碼 1（`started under another config`）。所以排程只從一個**專用、固定版本**的
worktree 跑，要升級時才刻意升級。

在主 checkout 的根目錄：

```powershell
git fetch origin
git worktree add --detach ..\TradingAgents-uniswap-paper origin/develop
Copy-Item .env ..\TradingAgents-uniswap-paper\.env
Set-Location ..\TradingAgents-uniswap-paper
```

之後本手冊的指令都在 `..\TradingAgents-uniswap-paper` 執行。store 與 log 會放在它的
`contrib\uniswap_v3\data\`（gitignored）。

**升級**（要用新版程式時才做）：

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

升級後看一下 log（§4）：若是 `failed: the run '...' was started under another config`，新版改了設定預設值，
照 §6 開新 run。

**不要刪這個 worktree 或對它 `git clean -fdx`**：`data\` 裡是 store 與 log，而它是 gitignored——
`git worktree remove` **不會問、直接連它一起刪掉**。真的要移掉這個 worktree，先用 §9 第一條的寫法把
`paper.db` 備份到 worktree 外面（log 直接複製）。

---

## 1. 一次性前置

### 1.1 相依與自我檢查

```powershell
pip install -r contrib/uniswap_v3/requirements.txt
python -m pytest -q -m "not smoke" contrib/uniswap_v3/tests        # 全綠才繼續
python -m dotenv run -- python -m pytest -q -m smoke contrib/uniswap_v3/tests/test_chain_smoke.py
```

第二條 pytest 打真的節點：`.env` 要有 `ETH_RPC_URL=https://...`（完整 URL，不是只有 key），
而且要是 **archive** 節點（漏跑的 visit 補跑、回補歷史都要讀舊區塊）。

記下這個 Python 的完整路徑（`(Get-Command python).Source`）：排程用的要是同一個 `python.exe`。
repo 的 `.venv` 不一定裝了 web3。

### 1.2 設定檔

```powershell
Copy-Item contrib/uniswap_v3/configs/uniswap_v3.example.yaml contrib/uniswap_v3/configs/paper.local.yaml
```

要改的只有 `strategy`（目前只有佔位策略 `fixed_weights`）。其餘照預設：一天一根 bar、
成交在邊界後 25 塊（`execution.delay_blocks`）——排程時間是照這個值排的，
改大到超過 45 塊（約 9 分鐘）就要把排程一起往後挪。

### 1.3 先把歷史讀進 store（第一次 visit 之前）

```powershell
New-Item -ItemType Directory -Force contrib/uniswap_v3/data
python -m dotenv run -- python -m contrib.uniswap_v3 backfill --config contrib/uniswap_v3/configs/paper.local.yaml --db contrib/uniswap_v3/data/paper.db --from 2022-01-01
```

日線從 2022-01-01 到現在約 1,700 根、100 分鐘左右；中斷了重跑即可（已讀的跳過）。
手上已有回補好的 store 檔，也可以複製過來當 `contrib/uniswap_v3/data/paper.db`
（用 §9 第一條的 `backup` 寫法複製，不要直接複製檔案：store 是 WAL 模式，最近寫的可能還在旁邊的 `-wal` 檔裡）。

**為什麼要先做**：策略看到的 view 是 store 裡到當根為止的全部 bar。run 開始後才補更早的歷史，
paper 當時看到的與事後回測看到的就不一樣了。所以：**歷史在開 run 之前補完，之後不要在
跑著的 run 後面再 backfill**（visit 自己會補新的 bar）。

---

## 2. 開一個新的 run（手動，一次）

排程的 visit 不帶起始餘額，所以 run 要先手動開：

```powershell
python -m dotenv run -- python -m contrib.uniswap_v3 paper --config contrib/uniswap_v3/configs/paper.local.yaml --db contrib/uniswap_v3/data/paper.db --run-id paper-1 --balance USDC=10000 --gas-eth 1
```

- 新 run 從**最近一個已過的邊界**開始。在 00:00–00:05 UTC 之間開會拿到結束碼 3（成交區塊還沒出現），
  `paper-1` 這個 id 不會被佔用，晚點再跑同一條就好。
- `--balance` 每個代幣一個，沒寫的從 0 開始；`--gas-eth` 是另外一筆只拿來付 gas 的 ETH，
  不算進目標比例。**paper 的錢是虛擬的，gas 直接給足**：主網一筆 swap 的 gas 是幾美元起跳，
  gas 用完之後每次再平衡都會被拒（stderr 警告、結束碼仍 0），沒有東西會幫它補。
- 開完用 `status --run-id paper-1` 看一下（見 §4）。

排程**不帶** `--balance` 是故意的：`--db` 或 `--run-id` 打錯時 visit 會以結束碼 1 失敗
（`there is no run ..., and a new run needs opening balances`），而不是悄悄開一個新 run。

---

## 3. 掛排程（Windows 工作排程器）

`schedule/paper-visit.xml` 每天 **00:10、00:40、01:10 UTC**（台灣時間 08:10、08:40、09:10）
各跑一次 `schedule/paper-visit.cmd`：

- 第一次決策 00:00 收盤的那根 bar；後兩次發現已決策，結束碼 0、不寫決策（只會把先前 pending 的讀數核對成 final）。
  第一次若拿到結束碼 3（節點落後、成交區塊還沒到、節點回錯誤、store 被別的程式鎖住），後兩次就是重試。
  一天最多試三次；三次都失敗的那根，隔天的 visit 會補決策（成交區塊照舊，與準時的一樣）。
- 前一次還在跑就不開新的（避免兩個 visit 搶同一個 store）；每次最多跑 25 分鐘。
- 電腦關機錯過的 visit，開機後會補跑一次。

### 3.1 visit 的設定

預設值寫在 `schedule/paper-visit.cmd` 開頭，**不要改那個檔**（它被 git 追蹤）。要改的放進同目錄的
`schedule\paper-visit.local.cmd`（gitignored，存在就會被讀），一行一個，例如：

```bat
set "PYTHON=C:\Users\you\AppData\Local\Programs\Python\Python312\python.exe"
set "RUN_ID=paper-1"
```

| 變數 | 預設 | 要不要改 |
|---|---|---|
| `RUN_ID` | `paper-1` | 與 §2 開的 run 一致 |
| `CONFIG` | `contrib\uniswap_v3\configs\paper.local.yaml` | 通常不用 |
| `DB` | `contrib\uniswap_v3\data\paper.db` | 通常不用 |
| `LOG` | `contrib\uniswap_v3\data\paper-visits.log` | 通常不用 |
| `PYTHON` | `python`（排程用你的 PATH） | 建議改成 §1.1 記下的完整路徑；要是 `python.exe`，不能是 `.cmd`／`.bat` |

相對路徑以 repo 根目錄為準（visit 在那裡跑）。這個檔**只放 `set` 行**：`setlocal` 會讓設定失效，
`exit` 會讓 visit 不跑、也不留 log。log 每次 visit 的第一行會印出用的 `DB` 與 `PYTHON`。

### 3.2 註冊

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
| `4` | visit 腳本進不了 repo 目錄、寫不了 log，或 `PYTHON` 的路徑不存在，visit **沒有跑**（§5） |
| `9009` | 找不到 `PYTHON`（§3.1） |
| `267009` | 還在跑，等一下再查 |
| `267014` | 跑超過 25 分鐘被排程停掉了（§5） |
| `2147942402` | 找不到 visit 腳本：worktree 搬了或刪了，重新註冊 |

- 跑的時候會閃一下命令列視窗（task 是「只在使用者登入時執行」）。不想看到，可以在工作排程器 GUI
  把它改成「不論使用者登入與否均執行」。
- 停用／恢復／移除：`Disable-ScheduledTask`、`Enable-ScheduledTask`、
  `Unregister-ScheduledTask -TaskName 'uniswap-v3-paper' -Confirm:$false`。

---

## 4. 日常檢查

```powershell
Get-Content contrib\uniswap_v3\data\paper-visits.log -Tail 30
python -m contrib.uniswap_v3 status --config contrib/uniswap_v3/configs/paper.local.yaml --db contrib/uniswap_v3/data/paper.db --run-id paper-1
python -m contrib.uniswap_v3 report --db contrib/uniswap_v3/data/paper.db --run-id paper-1
```

- log 每次 visit 有一行 `==== <本地時間> visit of "paper-1" (db ..., python ...)`、指令的輸出、一行 `==== exit <碼>`。
- `status --run-id` 從 `run paper-1: ...` 那行算起的第三行，說 run 跟不跟得上時鐘：`up to date` 是最近一個
  已過的邊界已決策；`behind: N boundary(ies) ...` 是有 N 根已過但還沒決策——00:00 到 00:10 之間是 1、正常；
  **過了 01:35（第三次 visit 的時限）還是 behind**，就去 log 看那天的 visit 為什麼沒成功。
  run 還沒決策過任何一根時沒有這一行。
- 接著是持倉、價值、報酬（與 `report` 同一個算法：扣掉累計 gas）、最近幾筆決策，
  每筆附「邊界後多久決策的」——準時的應該是 `00:10:xx`；`1d ...` 表示是隔天補決策的。
- 這兩個指令不讀鏈，隨時可以跑。

---

## 5. log 裡看到這些時

| 看到 | 意思 | 做什麼 |
|---|---|---|
| `==== exit 0` | visit 跑完了：決策了、這根已經決策過，或鏈在邊界沒有答案（前面會有 `warning: the chain had no answer`，見下面那一列） | 沒事；有 warning 就照那一列 |
| `exit 0` 前有 `warning: ... rebalance(s) were rejected` | 報價低於 `max_slippage` 容許的下限，或 gas 不夠；這根不再平衡 | 不重試，是設計；gas 不夠的話見 §6 |
| `warning: ... skipped as suspect` | bar 的收盤價與 TWAP 偏離太大（或被 reorg），不交易 | 沒事；連續很多根就看一下 `status` 的 flags |
| `warning: the chain had no answer at the boundary` | 池子在那個邊界讀不到 TWAP | 下一次 visit 會再問；run 走過去之後就永遠不決策它 |
| `warning: ... no longer on the final chain` | 先前存的讀數被 reorg | 見 §7 |
| `try again later: ...` 接 `==== exit 3` | 節點落後、成交區塊還沒出現、或節點回錯誤 | 當天後面的 visit 會重試；**一整天三次都是 3** 就查節點（額度、URL、服務狀態） |
| `try again later: the store ... (database is locked)` 接 `==== exit 3` | 有別的程式開著同一個 store：同時在跑回測、用 DB 瀏覽器開著，或前一次 visit 被排程停掉、它啟動的 python 還沒結束 | 關掉它（工作管理員裡找 `python.exe`）；當天後面的 visit 會重試 |
| `failed: there is no run 'paper-1', and a new run needs opening balances` | `RUN_ID` 或 `DB` 打錯，或 run 還沒開 | 對照 §2、§3.1 |
| `failed: the run 'paper-1' was started under another config` | 設定檔改了，或新版程式改了預設值 | run 只能在開它的設定下接續：見 §6 開新 run |
| `failed: ... the clock is behind` | 這台機器的時鐘早於 run 已走到的邊界 | 校時 |
| `failed: paper needs the packages in contrib/uniswap_v3/requirements.txt` | 排程用的 Python 不是裝了相依的那個 | 改 §3.1 的 `PYTHON` |
| `'python' is not recognized ...`（中文 Windows 是「不是內部或外部命令」）接 `==== exit 9009` | 排程找不到 `PYTHON` | 改 §3.1 的 `PYTHON` 成完整路徑 |
| `==== there is no "...python.exe": fix PYTHON ...` 接 `==== exit 4` | §3.1 的 `PYTHON` 路徑打錯 | 改 `paper-visit.local.cmd` |
| `Error: Invalid value: Invalid value for '-f' "...\.env" does not exist.` 接 `==== exit 2` | repo 根目錄沒有 `.env` | 補上 `.env`（§0） |
| 其他 `usage: ...` 接 `==== exit 2` | visit 的指令被改壞 | 對照 git 版的 `paper-visit.cmd`；設定只放在 `paper-visit.local.cmd` |
| 有 `==== ... visit of` 卻沒有 `==== exit` | visit 跑超過 25 分鐘被排程停掉（`LastTaskResult` 267014）；印到一半的輸出還在 | 多半是節點卡住；下一次 visit 會重來 |
| 這次 visit 完全沒有留下任何一行，`LastTaskResult` 是 4 | visit 腳本進不了 repo 目錄，或 log 寫不了（唯讀、被別的程式鎖住、目錄建不了） | 檢查 `data\` 與 log 檔的權限；visit 沒有跑 |

---

## 6. 重設：開一個新的 run

這些情況要開新 run（舊的 run 留在 store 裡，`report` 仍可查）：設定檔改了、新版程式改了設定預設值、
想換起始餘額、gas 用完了。

1. `status --run-id <舊 id>` 抄下 `holdings after the bar at ...` 那一行的持倉。
2. 照 §2 用**新的 run id**、抄下的餘額（或你要的新餘額）開 run。新舊 run 之間沒有自動銜接。
3. 改 `schedule\paper-visit.local.cmd` 的 `RUN_ID`。

要整個重來就換一個 store 檔（或刪掉 `data\paper.db`），從 §1.3 開始。

---

## 7. 資料缺漏怎麼處理

- **漏跑的 visit**：不用處理。下一次 visit 從 run 最後決策的下一根開始補讀、逐根決策，
  每根都在自己的成交區塊報價（要 archive 節點）。
- **邊界沒答案**（TWAP revert）：store 不寫那根；visit 會在下次再問。run 已經走過它之後就不會回頭決策，
  `report` 與回測的 summary 會數到這種缺口。
- **reorg 的讀數**：visit 核對 pending 的讀數時發現 close block 不在最終鏈上，會把它標成 `reorged`
  （不刪、不重抓）。已經在它上面做的決策照舊；之後的新 run 會把它當可疑 bar 略過。要重抓：
  1. `Disable-ScheduledTask -TaskName 'uniswap-v3-paper'`，並先備份 `data\paper.db`（§9 第一條的寫法）。
  2. 刪掉那幾列：
     ```powershell
     python -c "import sqlite3; c = sqlite3.connect('contrib/uniswap_v3/data/paper.db'); print(c.execute('DELETE FROM bars WHERE finality = ?', ('reorged',)).rowcount); c.commit()"
     ```
  3. 對那段日期跑一次 §1.3 的 `backfill --from <那天> --to <那天>`，再 `Enable-ScheduledTask`。

  跑著的 run 對那根的決策不會變；之後對同一段跑回測會警告「decided earlier now read differently」。
  主網 merge 之後超過 25 塊深的 reorg 極少見。

---

## 8. 換 RPC 供應商

1. 改 `.env` 的 `ETH_RPC_URL`（或在設定檔的 `rpc.url_env` 改用另一個變數名稱）。
   `rpc` 不在 run 的設定快照裡，換了不用開新 run。
2. 新的節點要是 **archive**。用 §1.1 的 smoke 測試確認讀得到。

---

## 9. 驗證：paper 與事後回測一致

paper 決策過幾根之後，可以對同一段跑一次報價級回測，確認決策相同（約束：回測與紙上交易對同一段 bar
做出相同的決策）。在 store 的**複本**上跑，不動排程用的那份；避開 visit 時段（00:10–01:35 UTC）：

```powershell
python -c "import sqlite3; sqlite3.connect('file:contrib/uniswap_v3/data/paper.db?mode=ro', uri=True).backup(sqlite3.connect('contrib/uniswap_v3/data/check.db'))"
python -m dotenv run -- python -m contrib.uniswap_v3 backtest --config contrib/uniswap_v3/configs/paper.local.yaml --db contrib/uniswap_v3/data/check.db --run-id check-1 --fills quoter --from <run 的第一根> --to <最後決策的那根> --balance USDC=10000 --gas-eth 1
python -c "import sqlite3; c = sqlite3.connect('contrib/uniswap_v3/data/check.db'); print(c.execute('SELECT p.time, p.outcome, b.outcome FROM decisions p LEFT JOIN decisions b ON b.time = p.time AND b.run_id = ? WHERE p.run_id = ? AND (p.outcome IS NOT b.outcome OR p.target IS NOT b.target)', ('check-1', 'paper-1')).fetchall())"
```

第一條讀不到 store 時會報錯（`unable to open database file`），那就不要往下跑。
起始餘額要與 paper run 開的時候相同。第三條印 `[]` 就是逐根一致（回測少決策的那根也會被列出來）；
兩個 run 的 `report` 裡報酬與成本也應該相同（成交區塊相同，所以報價相同）。用完可以刪掉 `check.db`。

---

## 10. 分叉沙盒：fork run

fork run 用 store 裡的 bar（同回測），但每根要交易的 bar 都在本機 anvil 分叉上真的簽名送出：先把分叉重設
（`anvil_reset`）到這根 bar 的成交區塊、把開發帳戶的 ETH 與代幣餘額灌成 run 的帳本，再送；送前送後都對帳。
用的是 anvil 的開發帳戶（公開的 test 助記詞），不碰任何真的錢包；分叉在 anvil 關掉時就消失。

### 10.1 跑一個 fork run

1. 裝好 Foundry（§10.3 第 1 條），`anvil --version` 確認。
2. store 裡要有那段 bar（§1.3 的 `backfill`；在跑著 paper 的 store 上跑 fork 也可以，但建議用複本，§9 第一條的寫法）。
3. 另開一個視窗起 anvil，分叉在哪一塊都可以（每根要交易的 bar 會自己重設到它的成交區塊；節點要 archive）：

   ```powershell
   python -m dotenv run -- powershell -Command 'anvil --fork-url $env:ETH_RPC_URL --fork-block-number <區塊> --port 8545 --silent'
   ```

   anvil 會把 `--fork-url` 印在自己的錯誤訊息裡，那就是含 API key 的 URL：這個視窗的輸出不要貼到別處。
4. 跑（新 run 要給起始餘額；`--fork-url` 不給就是 `http://127.0.0.1:8545`，主機必須寫字面的 `127.0.0.1` 或 `::1`）：

   ```powershell
   python -m contrib.uniswap_v3 fork --config contrib/uniswap_v3/configs/paper.local.yaml --db contrib/uniswap_v3/data/check.db --run-id fork-1 --from <第一根> --to <最後一根> --balance USDC=10000 --gas-eth 1
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

## 附錄：Linux（systemd timer）

在個人 Linux 主機上跑的對應寫法（使用者層級的 unit；`loginctl enable-linger` 讓它登出後也跑）。
`WorkingDirectory` 指向 §0 那樣的專用 worktree：

```ini
# ~/.config/systemd/user/uniswap-v3-paper.service
[Unit]
Description=contrib/uniswap_v3 paper visit

[Service]
Type=oneshot
WorkingDirectory=%h/TradingAgents-uniswap-paper
Environment=PYTHONUNBUFFERED=1
ExecStart=/bin/sh -c 'python -m dotenv run -- python -m contrib.uniswap_v3 paper --config contrib/uniswap_v3/configs/paper.local.yaml --db contrib/uniswap_v3/data/paper.db --run-id paper-1 >> contrib/uniswap_v3/data/paper-visits.log 2>&1'
TimeoutStartSec=25min
```

```ini
# ~/.config/systemd/user/uniswap-v3-paper.timer
[Timer]
OnCalendar=*-*-* 00:10:00 UTC
OnCalendar=*-*-* 00:40:00 UTC
OnCalendar=*-*-* 01:10:00 UTC
Persistent=true

[Install]
WantedBy=timers.target
```

`systemctl --user enable --now uniswap-v3-paper.timer`。還在跑的 oneshot 不會被再啟動一次，
`Persistent=true` 補跑關機時錯過的那次，與 Windows 版的設定對應。
