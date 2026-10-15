# contrib/dashboard RUNBOOK

把狀態 exporter（dashboard 計畫 `.claude/dashboard-plan-2026-10-12.md` PR 3）掛到 Lightsail 上、第一次怎麼開、每天看什麼、
告警信來了怎麼辦的**操作手冊**。套件本身、設定與規則表見 [README.md](./README.md)。唯讀網頁（PR 4）的安裝在 §7。

exporter 是 `trader` 身分的 systemd timer，每 2 分鐘跑一次 `python -m contrib.dashboard export`：讀六條線各自的
`status --json`／`report --json`，加上主機狀態，寫到 `/home/trader/status/`，狀態轉換時寄 email，每天一封摘要。
**不碰任何交易路徑**：只跑各套件的唯讀指令、只寫自己的目錄。

伺服器上的指令以 `trader` 身分（`sudo -u trader -H bash`）在 hl checkout 根目錄 `/home/trader/TradingAgents` 執行，
Python 是該 checkout 的 `.venv/bin/python`；裝 unit 的步驟用 `ubuntu`＋`sudo`。

---

## 0. 伺服器上的落點

| 東西 | 在哪 |
|---|---|
| 設定 | `/home/trader/TradingAgents/contrib/dashboard/configs/exporter.local.yaml`（§1.2；gitignored；從 `exporter.example.yaml` 抄） |
| 信箱與 dead-man URL | `/home/trader/TradingAgents/.env` 裡的 `DASHBOARD_*` 行（§1.3；600；只讀這些名字） |
| 輸出目錄 | `/home/trader/status/`（owner `trader`、group `dashboard`、mode `2750`；檔案 `0640`） |
| 文件 | `hl-paper.json`、`carry-perp.json`、`uniswap-trend.json`、`uniswap-ai.json`、`uniswap-carry.json`、`carry.json`、`host.json`、`alerts.json`、`exporter.json` |
| 歷史曲線（PR 5） | `/home/trader/status/history/<line>.json`＋`history/host.json`（目錄 2750、檔 0640；每條線幾個序列：權益／價值／每日淨額、主機磁碟與記憶體；`exporter.local.yaml` 的 `history:` 管上限，預設每序列 4000 點、主機每 30 分鐘一點＝主機約 83 天、perp 一年以上、uniswap 與 carry 十年） |
| exporter 自己的狀態 | `/home/trader/status/alerts-state.json`（§5.3） |
| units | `/etc/systemd/system/dashboard-exporter.service`、`dashboard-exporter.timer`（§2；來源在 `contrib/dashboard/schedule/`） |
| 唯讀網頁 | `dashboard` 帳號；程式碼 `/home/dashboard/app`（`COMMIT` 檔寫著哪個 commit）、venv `/home/dashboard/venv`、unit `dashboard-web.service`、`http://127.0.0.1:8787/`，tailnet 內 `https://<機器名>.<tailnet>.ts.net/`（§7） |
| log | `journalctl -u dashboard-exporter.service`（每 tick 一行摘要；讀不到的線與寄信失敗在 stderr；寄不出去時 tick exit 3，`systemctl --failed` 看得到，下次寄成就恢復） |

---

## 1. 一次性前置

### 1.1 群組與目錄

```bash
sudo groupadd --system dashboard            # PR 4 的唯讀帳號會進這個 group
sudo install -d -o trader -g dashboard -m 2750 /home/trader/status
```

`2750`：trader 可寫、dashboard 群組可讀可進、其他人不可；setgid 讓 exporter 寫出的檔都掛 `dashboard` 群組。

### 1.2 設定檔

```bash
sudo -u trader -H bash
cd /home/trader/TradingAgents
cp contrib/dashboard/configs/exporter.example.yaml contrib/dashboard/configs/exporter.local.yaml
```

範例檔裡就是這台主機的六條線（兩個 perp daemon、三個 uniswap run、carry sleeve），只要對一次：

- `run_id`：hyperliquid 的 `paper-BTC-<N>` 要是**現在這段**的 run（換段時要改，§5.1）；carry 腿 `carry-ETH-1`；uniswap 三個 run id；
- uniswap 三個 `config`＝`/home/trader/uniswap-paper/contrib/uniswap_v3/configs/paper-*.local.yaml`；
- `python`／`cwd`：hl 的線用 hl checkout 的 venv，uniswap 的線用 uniswap checkout 的 venv（兩個 checkout 版本分開升級，各用自己的）；
- 門檻（`thresholds:`）與摘要時間（`digest_utc`）照 2026-10-14 拍板的預設，不用動。

先試跑、不寫不寄：

```bash
.venv/bin/python -m contrib.dashboard check --config contrib/dashboard/configs/exporter.local.yaml
```

六條線都要讀到（stderr 沒有 `not read:`），印出的 condition 應該只有你知道為什麼的那些。
uniswap 的線跑的是 uniswap checkout 的 `status`，**要 uniswap checkout 已升到有 `status --json` 的版本**（PR #358 之後）才讀得到。

### 1.3 信箱與 dead-man（`.env`）

在 `/home/trader/TradingAgents/.env`（已存在、600）**加**這幾行（值自己填，不要貼進對話）：

```
DASHBOARD_SMTP_HOST=smtp.gmail.com
DASHBOARD_SMTP_PORT=587
DASHBOARD_SMTP_USER=<寄件帳號>
DASHBOARD_SMTP_PASSWORD=<Gmail app password>
DASHBOARD_MAIL_FROM=<寄件帳號>
DASHBOARD_MAIL_TO=<收件人，逗號分隔>
DASHBOARD_HEALTHCHECKS_URL=<healthchecks.io 的 ping URL，https://；沒有就不要寫這行>
```

Gmail：帳號開兩步驟驗證後建 app password；587 STARTTLS（Lightsail 擋 25）。寄一封測試信：

```bash
.venv/bin/python -m contrib.dashboard test-mail --config contrib/dashboard/configs/exporter.local.yaml
```

沒收到就看 stderr 的 `dashboard: SMTP...`（密碼不會印出來）。healthchecks.io：免費版建一個 check，
period 2 分鐘、grace 10 分鐘，把 ping URL 填進 `.env`；exporter 每 tick ping 一次，信寄不出去時 ping `/fail`。
既有的 `health_check.sh` cron 與 `/home/trader/.healthchecks_url` 跟這個無關，可以留著。

---

## 2. 裝 unit

```bash
sudo cp /home/trader/TradingAgents/contrib/dashboard/schedule/dashboard-exporter.service /etc/systemd/system/
sudo cp /home/trader/TradingAgents/contrib/dashboard/schedule/dashboard-exporter.timer   /etc/systemd/system/
sudo systemd-analyze verify /etc/systemd/system/dashboard-exporter.service
sudo systemctl daemon-reload
sudo systemctl start dashboard-exporter.service            # 先手動跑一個 tick
sudo journalctl -u dashboard-exporter.service -n 20 --no-pager
ls -l /home/trader/status/                                 # 十個 json（含 alerts-state.json），0640，group dashboard
sudo systemctl enable --now dashboard-exporter.timer
systemctl list-timers dashboard-exporter.timer --no-pager
```

第一個 tick 會把「現在就成立」的條件當成 raised 寄一封信（例如沒裝的 unit），事件尾巴則只記下不寄。

---

## 3. 每天看什麼

- **信**：`[trading] N raised, ...` 是狀態轉換（raised／cleared／event 各一行：哪條線、什麼狀態、幾點）；
  `[trading] daily digest <日期>` 每天 03:30Z（台北 11:30）一封，每條線一行＋主機＋還在亮的告警。
  信裡**沒有**餘額、倉位、地址（計畫 §4）；要看數字去 §0 的 json 或 `/paper-status`。
- **沒收到摘要信**＝exporter 或主機或信箱有一個死了：先看 healthchecks（有接的話），再 `journalctl -u dashboard-exporter.service`。
- `exporter.json`：每條線 `ok`／`failure`／`stderr_line`／`duration_ms`，`mail`／`deadman` 的結果。
- `alerts.json`：現在成立的條件（`active[]` 有 `since`）、備註（例如換段 marker）、最近的事件。

---

## 4. 告警對照表

| 信裡的 key | 意思 | 怎麼辦 |
|---|---|---|
| `unit:hl-paper.service:down`／`unit:hl-paper-carry-eth.service:down` | perp daemon 不是 active | hyperliquid RUNBOOK §4（停止與重啟）、§7（故障排除）；若是刻意停機（換段）看本檔 §5.1 的 marker |
| `unit:uniswap-v3-paper.service:failed` | 當天 visit 最後一次失敗（三個時段都會重試） | uniswap RUNBOOK §5：看 `paper-visits.log` 的 `failed:` 行 |
| `unit:carry-signal.service:failed` | 協調者兩次都失敗（exit 4＝設定錯，不會重試） | carry RUNBOOK §6；兩腿今天照舊維持（D5） |
| `unit:<unit>:not_found` | unit 沒裝 | 查 local.yaml 的 `unit:` 拼字或裝 unit |
| `<hl>:cycle_overdue` | `next_decision_at` 過了 30 分鐘還沒下一個 cycle | daemon 卡住：journal 最後幾行；模型逾時會自己重試 |
| `<hl>:lease_stale` | lease 心跳 15 分鐘沒動但 unit 還 active | daemon 吊住：journal；必要時 restart（避開 cycle 中） |
| `<hl>:export_failed`／`export_stale` | export 失敗或 5 小時沒更新 | hyperliquid RUNBOOK §5（日常監控）；`REPLAY_UNVERIFIED` 看 `/paper-status` |
| `<hl>:safe_mode` | run 鎖在 safe mode | hyperliquid RUNBOOK §7（`recoverable` 自動解、`manual` 要人） |
| `<hl>:event:kill_switch_cancel_triggered@...`、`emergency_kill_switch_triggered@...`、`safe_mode_entered@...` 等 | kill switch／safe mode 新事件（refresh 不算） | 看 journal 那個時間點 |
| `<uni>:behind` | 日界線過了 3.5 小時還沒 decide（三個 visit 時段都過了） | visit log；手動 `paper-visit.sh` 一次 |
| `<uni>:clock_ahead` | store 的 clock 跑在時鐘前面（不可能狀態） | 主機時鐘或 store 有問題，先 `validate` |
| `<uni>:no_decision` | run 開了 48 小時一根都沒 decide | run 開錯 id 或 visit 根本沒跑 |
| `<uni>:no_run` | 文件裡沒有 run：`status` 沒帶 `--run-id` 才會這樣，exporter 一定會帶（run id 不在 store 是 `read_failed`） | 套件行為變了，回報 |
| `carry:handoff_stale` | 00:30Z 過了交接檔還是昨天的 | 協調者沒跑成：`carry-signal.log`；兩腿照舊維持（D5） |
| `carry:handoff_unreadable` | report 讀不到交接檔 | 路徑／權限；協調者第一次還沒跑完也會 |
| `carry:held_by_d5` | report 最新一天亮 `held by D5`（held 連續天數到了 carry 線的 `d5_days`，預設 3，exporter 以 `--d5-days` 傳給 report） | carry RUNBOOK §6：協調者連三天沒給可用交接檔 |
| `host:disk`／`host:memory` | 磁碟 > 85%／可用記憶體 < 200 MB | 清 `/home/trader/data/exports`、journal；兩個 perp daemon 各約 90 MB |
| `<hl>:store_damaged` | perp 的 `status` exit 5＝store 內容壞了（`validate` 的判決） | **紅燈**：hyperliquid RUNBOOK §7，先 `validate` |
| `<line>:read_failed` | 那條線的指令跑不起來（exit N／逾時／沒有 JSON／schema 不對） | `exporter.json` 的 `stderr_line`；uniswap 的 exit 1 不分「找不到 run」與「store 壞」，看 stderr |
| `<line>:rules_failed` | 文件形狀跟這版 exporter 讀的不同（套件改了欄位沒 bump 版本） | 升級 exporter（hl checkout）或回報 |

---

## 5. 常見狀況

### 5.1 換段（paper-BTC-N → N+1）

flat watcher 寫 `/home/trader/data/SEGMENT_SWITCH_READY` 時，exporter 會把 `hl-paper` 那條線的 unit 與進度規則**全部跳過**、
在 `alerts.json` 留一條 note，不會寄 `down`。換段完成、marker 移掉後，**把 `exporter.local.yaml` 的 `run_id` 改成新段**
（舊 run id 還讀得到，但看的是舊段）；timer 是 oneshot，下一個 tick 就生效，不用 restart。

### 5.2 重啟或重裝 exporter 不會重寄

狀態在 `alerts-state.json`：還成立的條件不會再 raised、事件尾巴只寄比記下的時間新的。
寄不出去的信留在 `pending`，下一個能寄的 tick 一起寄。

### 5.3 `alerts-state.json` 壞了

tick 會 exit 1、stderr 說 `is not a state file this build reads ... move it aside`。把它搬走（`mv alerts-state.json alerts-state.bad`），
下一個 tick 從頭來：現在成立的條件會再寄一次 raised、事件尾巴重新 seed，別的不會丟。

### 5.4 改門檻、改摘要時間、加減一條線

改 `exporter.local.yaml`，下一個 tick 生效。拿掉一條線後它的 `<line>.json` 與 `history/<line>.json` 留在目錄裡不會被刪，自己 `rm`。
改了名字＝新的一條線（舊檔同上）。換段（§5.1 改 `run_id`）之後那條線的歷史曲線**從頭開始**、舊段的點被覆寫（store 裡的 snapshot 都還在，但 `status` 只印最近 10 筆、頁面不會重畫舊段）；要留就先 `cp history/<line>.json history/<line>.<舊 run>.json`；
`history:` 的 `max_points` 調小，下一個 tick 就把最舊的點砍掉。

### 5.6 `history/<line>.json` 壞了

tick 照常跑（文件、信、ping 都不受影響），只是那條線的歷史**不再更新**：stderr 多一行 `history not kept: <line>: ... move it aside`、
`exporter.json` 該線的 `history_error` 有字、頁面的 exporter 卡轉紅寫 `history: not kept for <line>`。
把它搬走（`mv history/<line>.json history/<line>.bad`），下一個 tick 從文件的尾巴重新開始累積（之前累積的點就沒了）。
`history_error` 若寫 `the document is not shaped as this build harvests it (...)`＝套件文件的形狀變了（升級 exporter 或回報）；
寫 `the history could not be carried on (...)`＝這版 exporter 自己組出了讀不回來的歷史（括號裡是原因），屬 bug、回報；
寫 `history/ could not be made (...)` 或 `<line>.json could not be written (...)`＝目錄或檔案的權限、磁碟滿（括號裡是例外類別），不用搬任何檔，修權限或清磁碟（§1.1 的目錄要 trader 可寫）。

### 5.5 升級

`deploy/paper` 的 push 會 reset hl checkout、跑 `contrib/dashboard/tests` 等測試，restart daemon 前後會停起這個 timer（有 enable 才停），
所以部署不會寄 down／cleared；手動 `systemctl stop hl-paper` 做維護則會各寄一行，屬預期。timer 是 oneshot，每次用當時的 checkout，
**不用**重啟任何東西。uniswap checkout 另外用 `lightsail-install.sh <commit>` 升級（uniswap RUNBOOK §0）。

---

## 6. 資安對照（計畫 §4）

- 密碼只在 `.env`（600、owner trader）；exporter 只讀 `DASHBOARD_*` 行，refusal 只講名字不印值；`MailSettings` 的 repr 不含密碼。
- 寫出的 JSON＝各套件自己的白名單文件（`dataclasses.asdict`），exporter 不加任何 config 內容；`stderr_line` 有 0x＋40／64 hex 就遮罩。
- `history/` 的點只有時間戳與十進位數字（每種線一份固定的欄位白名單），其餘欄位是線名、kind、run id 與本套件自己的標籤，沒有任何來自文件的自由文字；`tests/test_history.py` 逐檔證明。
- 信只有「哪條線、什麼狀態、幾點」（`tests/test_mail.py` 證明文件裡的任何小數字串與 hex 都不會進信）。
- 目錄 `2750`、檔 `0640`、unit `UMask=0027`：只有 trader 與 `dashboard` 群組讀得到。
- exporter unit 不加 `ProtectSystem=strict`：它跑的是各套件的指令，要讀 WAL store（讀者也要寫 `-shm`）；硬化在唯讀網頁的 unit（§7.2、§7.7）。
- 不開任何 port、不碰防火牆；dead-man 是**出去**的 HTTPS GET。

---

## 7. 唯讀網頁（PR 4）

頁面只讀 §0 的 json、不開 store、不碰 `.env`；跑在獨立的 `dashboard` 帳號、綁 `127.0.0.1:8787`，
由 `tailscale serve` 在 tailnet 內轉出去（計畫 D1）；沒有任何寫入端點（D5）。套件說明見 README「The page」。

### 7.1 前提

- §1.1 的 `dashboard` 群組與 `/home/trader/status`（2750）已建好，exporter 至少跑過一個 tick（頁面會把沒寫過的檔標成 `not written yet`，不會壞，但沒東西看）；
- hl checkout（`/home/trader/TradingAgents`）已升到含 `contrib/dashboard/app` 的 commit（頁面的程式碼從這個 checkout 的 **committed tree** 抄出去）；
- 主機有 `python3 -m venv`（Ubuntu：`sudo apt install python3-venv`），且 `python3` 是 3.11 以上（24.04 是 3.12；pin 組是在 3.12 解析的，installer 對更舊的 python 直接拒絕，`PYTHON=` 可指到別的直譯器）。

### 7.2 裝（一支腳本，可重跑）

```bash
sudo /home/trader/TradingAgents/contrib/dashboard/schedule/install-web.sh
```

它只做四件事：建 `dashboard` 系統帳號（primary group `dashboard`、無 shell；home 是 root 的、帳號只能讀）；用 `git archive` 把 `contrib/__init__.py`、
`contrib/core`、`contrib/dashboard` 抄到 `/home/dashboard/app`（root 擁有、帳號只讀；沒有 `.env`、沒有 local.yaml、沒有 tests）並寫 `COMMIT`，
unit 檔則從同一個 committed tree 直接取到 root 的 `/var/lib/dashboard-web/new/`（保留原檔名，`systemd-analyze verify` 只認合法的 unit 名；root 驗證與安裝的檔案永遠不是帳號寫得到的）；
先停頁面、建 `/home/dashboard/venv` 並照 `contrib/dashboard/requirements.txt`（整組釘死，每個 wheel 的 sha256 都列在 pin 下；`pip install --require-hashes`：下載到的檔案 hash 不在清單裡就拒裝。pip 只驗它下載的檔案，所以 requirements.txt 跟 root 記在 `/var/lib/dashboard-web/requirements.sha256` 的上一次成功安裝不同時，venv 整個重建、每個 wheel 重新下載驗過；相同時 pip 是 no-op、不碰 index；比的是整個檔案的 sha256，改註解也算；record 只在 curl 探到頁面後才寫，回滾過的那次下一輪一定重建；要強制重建就刪掉那個 record 檔）裝；`systemd-analyze verify` 暫存的 unit、換樹、裝 `dashboard-web.service`、enable、start，
再用 curl 探到頁面真的回應才算裝好（`Type=simple` 一 fork 就算 active，不能信）。停頁面之後任何一步失敗都會把舊樹與舊 unit 檔換回去、把頁面重新啟動
（這一輪若重建了 venv，舊 venv 也會搬回來）。
不碰 trader 的任何檔案、不碰 exporter、不碰 daemon。

```bash
systemctl status dashboard-web --no-pager
curl -s http://127.0.0.1:8787/ | head -5          # 要看到 <!doctype html>
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://127.0.0.1:8787/   # 405
sudo journalctl -u dashboard-web -n 20 --no-pager
```

unit 的硬化（計畫 §4）：`ProtectHome=tmpfs`＋`BindReadOnlyPaths=` 只掛自己的 app／venv 與 `/home/trader/status`，
`ProtectSystem=strict`、`CapabilityBoundingSet=`（空）、`IPAddressAllow=localhost`、`SystemCallFilter=@system-service`。
`systemd-analyze verify` 若報 `Unknown key name`（例如 systemd < 247 不認 `ProtectProc`／`ProcSubset`），installer 會當失敗並回滾：
要刪那兩行得改 **repo 裡的 unit 檔**再部署（installer 每次都從 archive 的樹重抄 unit，直接改 `/etc/systemd/system` 下次就被蓋掉）；其餘不要放寬。

### 7.3 Tailscale（只做一次）

1. 裝：照官方 Linux 步驟（<https://tailscale.com/kb/1031/install-linux>，apt repo），然後 `sudo tailscale up`，用瀏覽器登入自己的 tailnet。
2. 轉出頁面（HTTPS、tailnet 內）：

   ```bash
   sudo tailscale serve --bg 8787
   tailscale serve status            # https://<機器名>.<tailnet>.ts.net/ -> http://127.0.0.1:8787
   ```

3. **禁用 Funnel**：永遠不要跑 `tailscale funnel`；`tailscale funnel status` 要是空的（Funnel 會開到公網）。
4. admin console：ACL 只允許自己的裝置連這台；device key expiry 保持開啟；**不開** Tailscale SSH（維持現有 ssh key）。
5. Lightsail 防火牆**不加任何 port**：Tailscale 是出去的連線（直連不成會走 DERP 中繼，仍可用）。

手機／電腦裝 Tailscale 登入同一個 tailnet，開 `https://<機器名>.<tailnet>.ts.net/`。

### 7.4 看什麼

- 卡片左邊顏色：綠＝quiet；紅＝有 condition、那條線讀不到、文件形狀不是這版讀的、**或卡上有紅色的列**（例如最後一個 cycle failed、carry 當天亮了 exporter 沒規則的燈）——
  紅列的理由會寫在卡片的狀態句；琥珀色的列＝資訊（歷史上的燈、mail 關著、clock 沒跟上），不影響卡片顏色；
  灰虛線＋灰標題＋`STALE`＝文件超過 6 分鐘（3 個 tick）沒更新（stale 的綠卡不再掛 `OK` 標籤；紅卡仍紅邊、`BAD` 標籤照掛）。
- 頁首紅色橫幅＝exporter 自己停了（`exporter.json` 太舊或沒有）：先看 §3、`systemctl list-timers dashboard-exporter.timer`。
- 每張卡都有 `generated ...` 一行（文件的 `generated_at`＋多久以前）；點線名進明細（cycles／fills／events；decisions／reason codes／store；days／weeks）。
- **明細頁的歷史曲線（PR 5）**：每條線的明細頁在表格前面有幾張圖——perp run 的權益與倉位名目；uniswap run 每根 bar 後的價值；carry 每日淨額（另一張累計）與兩腿權益——
  x 軸是時間（exporter 停過的那段就是兩點之間一條直線）、左邊標整條序列的最高與最低值（最多兩位小數）、下面標最早與最晚的時間；`history` 那列寫哪個 run、幾個序列、累積到幾點、最後一次有新點是什麼時候（`last changed`；沒新點的 tick 不重寫檔案）。
  點首頁 host 卡的標題進主機明細（`/host`）：磁碟使用率、可用記憶體、load 的曲線（每 30 分鐘一點）。
  第一個 tick 就把文件尾巴整條收進來（perp 最近 10 個 snapshot、uniswap 最近 5 根、carry 整段），所以各線一開始就有圖；只有主機從一點開始（`history` 列寫 `up to 1 point(s)`、沒有圖）。perp 的曲線每個 cycle 多一點（4 小時）、uniswap 與 carry 每天一點，**要幾天才看得出形狀**。
- 頁面每 60 秒自己重載；數字是各套件文件的原文（Decimal 字串），地址與 key 一律折成 `0x1234...abcd`。

### 7.5 升級

`deploy/paper` 的 push 在 daemon 重啟並確認 active 後，**若 `dashboard-web.service` 已 enable**，會自動再跑一次 `install-web.sh`
（失敗只在 Actions 上標 warning 並印出 unit 現在的狀態，不會讓部署失敗；installer 失敗會換回舊樹，頁面留在前一個 commit。**例外**：pip 因 hash 不符拒裝時 run 會紅、標 `::error`——daemon 那時已經重啟完成，不會被擋到，但這種失敗不該只是一行黃字；處置看 §7.6）。
daemon 的 active 檢查沒過時部署在那裡就停了、不會走到頁面這步，頁面留在舊 commit；手動升級＝再跑 §7.2 的那一行。`cat /home/dashboard/app/COMMIT` 看現在是哪個 commit。
exporter 文件形狀變了（套件 bump `schema_version`）而頁面沒跟上時，卡片會紅並寫 `document not shaped as this build reads it`——升級頁面即可。

**換依賴版本**：改 `requirements.txt` 裡的版本、在乾淨 venv 重新解析整組，再跑 `python contrib/dashboard/schedule/pin-hashes.py`
（stdlib，查 PyPI 的 JSON API，把該 release 每個 wheel 的 sha256 重寫到 pin 下；不收 sdist、不收 yanked）、兩者一起 commit。
hash 不能手改：`tests/test_requirements_hashes.py` 會比對檔案就是腳本寫出來的樣子，而 pip 在主機上會直接拒裝。

### 7.6 故障

| 症狀 | 看哪裡 |
|---|---|
| 瀏覽器連不上／502 | `systemctl status dashboard-web`；`tailscale serve status`；`curl http://127.0.0.1:8787/` 在主機上通不通 |
| 每張卡都 stale＋橫幅 | exporter 停了：§3、§5 |
| 卡片 `not written yet` | exporter 沒跑過，或 unit 的 `--status-dir` 不是 exporter 的 `status_dir` |
| 卡片 `not readable (PermissionError)` | 檔不是 0640／group `dashboard`：§1.1 的目錄要 2750（setgid），exporter unit 的 `UMask=0027` |
| `install-web.sh` 在 pip 那步失敗 | 主機沒網路或 PyPI 不通；會自動換回舊頁面（含舊 venv）；重跑即可（idempotent；半成品的 venv、或 requirements.txt 變了但沒裝完的 venv，下次都會重建） |
| pip 說 `THESE PACKAGES DO NOT MATCH THE HASHES` | 下載到的 wheel 不在 `requirements.txt` 的 hash 清單裡。先看 pip 印的檔名對 PyPI 該 release 的檔案列表：**檔名是清單裡沒有的新檔**（PyPI 允許事後補上新平台的 wheel；pin 換了版沒重產也長這樣）＝腳本過時，跑 §7.5 重產、commit；**檔名清單裡有、digest 卻不同**＝PyPI 不會換既有檔案，這是鏡像／線路／主機被動了手腳，不要重產，當事件查 |
| pip 說 `Hashes are required in --require-hashes mode` | 有 pin 沒帶 hash 進了 committed tree（`tests/test_requirements_hashes.py` 本應擋下）：跑 §7.5 的腳本重產、commit；這種失敗只是 warning、不會紅 |
| 第一次安裝就失敗（例如 curl 探不到） | 沒有舊頁面可回：unit 會被 disable 並移除、樹留在原地；看 stderr 的 `systemctl status`／`journalctl` 輸出，修了再重跑 |
| 卡片 `document not shaped ...` | 頁面版本落後套件：§7.5 |
| 明細頁每條線都 `history: history/<line>.json not readable by this build (not readable (PermissionError))` | `history/` 的檔案 group 不是 `dashboard`（第一版 exporter 用 trader 對目錄 chmod 2750、setgid 被核心剝掉）：`sudo chmod g+s /home/trader/status/history && sudo sh -c 'chgrp dashboard /home/trader/status/history/*.json'` 一次即可，之後寫出的檔跟著目錄的 group |
| 明細頁 `history: not written yet` | exporter 還沒升到會寫 `history/` 的版本，或第一個 tick 還沒跑；exporter 卡 `history: not kept for ...`＝§5.6 |

### 7.7 資安對照（計畫 §4，接 §6）

- `dashboard` 帳號只讀得到 `/home/trader/status`（經 group `dashboard`、檔 0640）；unit 的 mount namespace 裡 `/home` 是空的 tmpfs，只掛三個唯讀目錄，看不到 `.env`、`*.local.yaml`、`~/.ssh`、SQLite、payloads。
- 帳號寫得到的只有自己的 venv（pip 裝進去）；程式碼樹、home、unit 檔與回滾用的備份都是 root 的，root 不會安裝任何帳號寫得到的檔案；pip 裝進 venv 的每個 wheel 都要對上 `requirements.txt` 裡的 sha256（`--require-hashes`），而且 requirements.txt 一變 venv 就整個重建，venv 裡每個從 index 裝進來的檔案都驗過 hash（venv 自帶的 pip 來自系統 python 的 ensurepip、不經 index，不在清單裡）。
- 只綁 loopback（非 loopback 的 `--host` 直接拒絕、沒有放寬旗標）；對外只有 `tailscale serve`，Funnel 不開，Lightsail 防火牆不動。
- 無寫入端點：非 `GET`／`HEAD` 一律 405（在 routing 之前）；FastAPI 的 `/docs`、`/redoc`、`/openapi.json` 關閉（它們會從 CDN 載 script）。
- 回應帶 CSP（`default-src 'none'; style-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'`）、`nosniff`、`DENY`、`no-referrer`、`no-store`（樣式檔 `no-cache`）；頁面沒有任何 script，樣式檔在 repo。
- 文件裡的自由文字（AI 的 decision reason、錯誤訊息）autoescape 顯示為純文字，且再遮罩一次 `0x` hex、截 300 字；測試證明整個 wallet／agent key 不會進頁面。
- 依賴釘死在 `contrib/dashboard/requirements.txt`（版本＋每個 wheel 的 sha256，`schedule/pin-hashes.py` 產），只裝進 `dashboard` 帳號自己的 venv；不進根目錄 `requirements.txt`、不進 trader 的 venv。
