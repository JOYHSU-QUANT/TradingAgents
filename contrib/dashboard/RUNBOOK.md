# contrib/dashboard RUNBOOK

把狀態 exporter（dashboard 計畫 `.claude/dashboard-plan-2026-10-12.md` PR 3）掛到 Lightsail 上、第一次怎麼開、每天看什麼、
告警信來了怎麼辦的**操作手冊**。套件本身、設定與規則表見 [README.md](./README.md)。唯讀網頁是 PR 4，這張不裝。

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
| exporter 自己的狀態 | `/home/trader/status/alerts-state.json`（§5.3） |
| units | `/etc/systemd/system/dashboard-exporter.service`、`dashboard-exporter.timer`（§2；來源在 `contrib/dashboard/schedule/`） |
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
| `<hl>:event:kill_switch_cancel_triggered@...`、`emergency_close_triggered@...`、`safe_mode_entered@...` 等 | kill switch／safe mode 新事件（refresh 不算） | 看 journal 那個時間點 |
| `<uni>:behind` | 日界線過了 3.5 小時還沒 decide（三個 visit 時段都過了） | visit log；手動 `paper-visit.sh` 一次 |
| `<uni>:clock_ahead` | store 的 clock 跑在時鐘前面（不可能狀態） | 主機時鐘或 store 有問題，先 `validate` |
| `<uni>:no_decision` | run 開了 48 小時一根都沒 decide | run 開錯 id 或 visit 根本沒跑 |
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

改 `exporter.local.yaml`，下一個 tick 生效。拿掉一條線後它的 `<line>.json` 留在目錄裡不會被刪，自己 `rm`。
改了名字＝新的一條線（舊檔同上）。

### 5.5 升級

`deploy/paper` 的 push 會 reset hl checkout、跑 `contrib/dashboard/tests` 等測試，restart daemon 前後會停起這個 timer（有 enable 才停），
所以部署不會寄 down／cleared；手動 `systemctl stop hl-paper` 做維護則會各寄一行（重啟還會多一行 `kill_switch_armed` 事件），屬預期。timer 是 oneshot，每次用當時的 checkout，
**不用**重啟任何東西。uniswap checkout 另外用 `lightsail-install.sh <commit>` 升級（uniswap RUNBOOK §0）。

---

## 6. 資安對照（計畫 §4）

- 密碼只在 `.env`（600、owner trader）；exporter 只讀 `DASHBOARD_*` 行，refusal 只講名字不印值；`MailSettings` 的 repr 不含密碼。
- 寫出的 JSON＝各套件自己的白名單文件（`dataclasses.asdict`），exporter 不加任何 config 內容；`stderr_line` 有 0x＋40／64 hex 就遮罩。
- 信只有「哪條線、什麼狀態、幾點」（`tests/test_mail.py` 證明文件裡的任何小數字串與 hex 都不會進信）。
- 目錄 `2750`、檔 `0640`、unit `UMask=0027`：只有 trader 與 `dashboard` 群組讀得到。
- exporter unit 不加 `ProtectSystem=strict`：它跑的是各套件的指令，要讀 WAL store（讀者也要寫 `-shm`）；硬化留給 PR 4 的唯讀服務。
- 不開任何 port、不碰防火牆；dead-man 是**出去**的 HTTPS GET。
