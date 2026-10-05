# contrib/uniswap_v3 RUNBOOK

把 paper run 掛到 Windows 工作排程器、每天顧一下、出事時怎麼處理的**操作手冊**。
套件本身的架構與指令說明見 [README.md](./README.md)。

所有指令都在 **repo 根目錄**、用 PowerShell 執行。會讀鏈的指令前面都有
`python -m dotenv run --`，它從根目錄的 `.env` 帶入 `ETH_RPC_URL`。

排程跑的是**這個目錄當下 checkout 的程式碼**：掛排程的那個 checkout 保持在 `develop`，
功能開發用 worktree，不要在這裡切分支。

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

記下這個 Python 的完整路徑（`(Get-Command python).Source`）：排程用的要是同一個。
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
手上已有回補好的 store 檔，直接複製成 `contrib/uniswap_v3/data/paper.db` 也行。

**為什麼要先做**：策略看到的 view 是 store 裡到當根為止的全部 bar。run 開始後才補更早的歷史，
paper 當時看到的與事後回測看到的就不一樣了。所以：**歷史在開 run 之前補完，之後不要在
跑著的 run 後面再 backfill**（visit 自己會補新的 bar）。

`contrib/uniswap_v3/data/` 是 gitignored。

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
  第一次若拿到結束碼 3（節點落後、成交區塊還沒到、節點回錯誤），後兩次就是重試。
  一天最多試三次；三次都失敗的那根，隔天的 visit 會補決策（成交區塊照舊，與準時的一樣）。
- 前一次還在跑就不開新的（避免兩個 visit 搶同一個 store）；每次最多跑 25 分鐘。
- 電腦關機錯過的 visit，開機後會補跑一次。

### 3.1 設定 visit 腳本

`contrib/uniswap_v3/schedule/paper-visit.cmd` 開頭的 `set` 幾行：

| 變數 | 預設 | 要不要改 |
|---|---|---|
| `RUN_ID` | `paper-1` | 與 §2 開的 run 一致 |
| `CONFIG` | `configs\paper.local.yaml` | 通常不用 |
| `DB` | `data\paper.db` | 通常不用 |
| `LOG` | `data\paper-visits.log` | 通常不用 |
| `PYTHON` | `python`（排程用你的 PATH） | 建議改成 §1.1 記下的完整路徑 |

這個檔有被 git 追蹤：改了的話 `git status` 會看到，不要 commit 你的路徑。

### 3.2 註冊

```powershell
$xml = (Get-Content -Raw contrib\uniswap_v3\schedule\paper-visit.xml).Replace('C:\path\to\TradingAgents', (Get-Location).Path)
Register-ScheduledTask -TaskName 'uniswap-v3-paper' -Xml $xml
Start-ScheduledTask -TaskName 'uniswap-v3-paper'          # 立刻跑一次試試
Get-ScheduledTaskInfo -TaskName 'uniswap-v3-paper'        # LastTaskResult：0 好、1 要修、3 晚點會好
Get-Content contrib\uniswap_v3\data\paper-visits.log -Tail 20
```

- `LastTaskResult` 是 `2147942402`（0x80070002，找不到檔案）的話，是 `Command` 的路徑含空白沒被認出來：
  在工作排程器 GUI 的「動作」把程式路徑前後加上雙引號。
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

- log 每次 visit 有一行 `==== <本地時間> visit of paper-1`、指令的輸出、一行 `==== exit <碼>`。
- `status --run-id` 印持倉、價值、報酬（與 `report` 同一個算法：扣掉累計 gas）、最近幾筆決策，
  每筆附「邊界後多久決策的」——準時的應該是 `00:10:xx`；`1d ...` 表示是隔天補決策的。
- 這兩個指令不讀鏈，隨時可以跑。

---

## 5. log 裡看到這些時

| 看到 | 意思 | 做什麼 |
|---|---|---|
| `==== exit 0` | 決策了，或這根已經決策過 | 沒事 |
| `exit 0` 前有 `warning: ... rebalance(s) were rejected` | 報價低於 `max_slippage` 容許的下限，或 gas 不夠；這根不再平衡 | 不重試，是設計；gas 不夠的話見 §6 |
| `warning: ... skipped as suspect` | bar 的收盤價與 TWAP 偏離太大（或被 reorg），不交易 | 沒事；連續很多根就看一下 `status` 的 flags |
| `warning: the chain had no answer at the boundary` | 池子在那個邊界讀不到 TWAP | 下一次 visit 會再問；run 走過去之後就永遠不決策它 |
| `warning: ... no longer on the final chain` | 先前存的讀數被 reorg | 見 §7 |
| `try again later: ...` 接 `==== exit 3` | 節點落後、成交區塊還沒出現、或節點回錯誤 | 當天後面的 visit 會重試；**一整天三次都是 3** 就查節點（額度、URL、服務狀態） |
| `failed: there is no run 'paper-1', and a new run needs opening balances` | `RUN_ID` 或 `DB` 打錯，或 run 還沒開 | 對照 §2、§3.1 |
| `failed: the run 'paper-1' was started under another config` | 設定檔改了，或新版程式改了預設值 | run 只能在開它的設定下接續：見 §6 開新 run |
| `failed: ... the clock is behind` | 這台機器的時鐘早於 run 已走到的邊界 | 校時 |
| `failed: the store at ... cannot be used (database is locked)` | 有別的程式開著同一個 store（例如同時在跑回測、用 DB 瀏覽器開著，或前一次 visit 超過 25 分鐘被排程停掉、它啟動的 python 還沒結束） | 關掉它（工作管理員裡找 `python.exe`）；下一次 visit 會自己好 |
| `failed: paper needs the packages in contrib/uniswap_v3/requirements.txt` | 排程用的 Python 不是裝了相依的那個 | 改 §3.1 的 `PYTHON` |
| `Error: Invalid value for '-f' "...\.env" does not exist.` 接 `==== exit 2` | repo 根目錄沒有 `.env` | 補上 `.env`（§1.1） |
| 其他 `usage: ...` 接 `==== exit 2` | `paper-visit.cmd` 的指令被改壞 | 對照 git 版的 `paper-visit.cmd` |

---

## 6. 重設：開一個新的 run

這些情況要開新 run（舊的 run 留在 store 裡，`report` 仍可查）：設定檔改了、新版程式改了設定預設值、
想換起始餘額、gas 用完了。

1. `status --run-id <舊 id>` 抄下最後一行持倉（`holdings after the bar at ...`）。
2. 照 §2 用**新的 run id**、抄下的餘額（或你要的新餘額）開 run。新舊 run 之間沒有自動銜接。
3. 改 `paper-visit.cmd` 的 `RUN_ID`。

要整個重來就換一個 store 檔（或刪掉 `data\paper.db`），從 §1.3 開始。

---

## 7. 資料缺漏怎麼處理

- **漏跑的 visit**：不用處理。下一次 visit 從 run 最後決策的下一根開始補讀、逐根決策，
  每根都在自己的成交區塊報價（要 archive 節點）。
- **邊界沒答案**（TWAP revert）：store 不寫那根；visit 會在下次再問。run 已經走過它之後就不會回頭決策，
  `report` 與回測的 summary 會數到這種缺口。
- **reorg 的讀數**：visit 核對 pending 的讀數時發現 close block 不在最終鏈上，會把它標成 `reorged`
  （不刪、不重抓）。已經在它上面做的決策照舊；之後的新 run 會把它當可疑 bar 略過。要重抓：
  1. `Disable-ScheduledTask -TaskName 'uniswap-v3-paper'`，並先備份 `data\paper.db`。
  2. 刪掉那幾列：
     ```powershell
     python -c "import sqlite3; c = sqlite3.connect('contrib/uniswap_v3/data/paper.db'); print(c.execute(\"DELETE FROM bars WHERE finality = 'reorged'\").rowcount); c.commit()"
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
做出相同的決策）。在 visit 時段之外跑（避開 00:10–01:35 UTC）：

```powershell
python -m dotenv run -- python -m contrib.uniswap_v3 backtest --config contrib/uniswap_v3/configs/paper.local.yaml --db contrib/uniswap_v3/data/paper.db --run-id check-1 --fills quoter --from <run 的第一根> --to <最後決策的那根> --balance USDC=10000 --gas-eth 1
python -c "import sqlite3; c = sqlite3.connect('contrib/uniswap_v3/data/paper.db'); print(c.execute(\"SELECT p.time, p.outcome, b.outcome FROM decisions p JOIN decisions b ON b.time = p.time WHERE p.run_id = 'paper-1' AND b.run_id = 'check-1' AND (p.outcome <> b.outcome OR IFNULL(p.target, '') <> IFNULL(b.target, ''))\").fetchall())"
```

起始餘額要與 paper run 開的時候相同。第二條印 `[]` 就是逐根一致；兩個 run 的 `report` 裡報酬與成本也應該相同
（成交區塊相同，所以報價相同）。

---

## 附錄：Linux（systemd timer）

在個人 Linux 主機上跑的對應寫法（使用者層級的 unit；`loginctl enable-linger` 讓它登出後也跑）：

```ini
# ~/.config/systemd/user/uniswap-v3-paper.service
[Unit]
Description=contrib/uniswap_v3 paper visit

[Service]
Type=oneshot
WorkingDirectory=%h/TradingAgents
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
