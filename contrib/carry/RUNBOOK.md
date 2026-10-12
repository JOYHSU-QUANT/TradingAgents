# contrib/carry RUNBOOK

把 carry sleeve（Hyperliquid 空 perp 對 Uniswap 多現貨，計畫 `.claude/carry-sleeve-plan-2026-10-07.md`）的三件東西
掛到 Lightsail 上、第一天怎麼開、每天顧什麼、看到亮燈怎麼辦的**操作手冊**。套件本身與指令的說明見 [README.md](./README.md)；
兩條腿各自的 run 照各自的手冊：perp 是 [hyperliquid_perp/docs/RUNBOOK.md](../hyperliquid_perp/docs/RUNBOOK.md)，
現貨是 [uniswap_v3/RUNBOOK.md](../uniswap_v3/RUNBOOK.md)。

三件東西：

| 東西 | 是什麼 | 住哪個 checkout |
|---|---|---|
| 協調者 | `carry-signal.timer` 每天 23:50 UTC 跑 `schedule/carry-signal.sh`＝`python -m contrib.carry signal`，寫交接檔 | hl 的 `/home/trader/TradingAgents`（`deploy/paper`） |
| perp 腿 | `hl-paper-carry-eth.service`：第二個 perp paper daemon，`decision_source: file_target`，不問模型 | 同上 |
| 現貨腿 | uniswap 的第三個 run `paper-carry-1`（`external_weights`），由既有的 `uniswap-v3-paper.timer` 的 visit 帶著跑 | uniswap 的 `/home/trader/uniswap-paper`（固定 commit） |

伺服器上的指令以 `trader` 身分（`sudo -u trader -H bash`）在對應 checkout 根目錄用 bash 執行，Python 是該 checkout 的 `.venv/bin/python`。

---

## 0. 伺服器上的落點

| 東西 | 在哪 |
|---|---|
| 交接檔 | `/home/trader/data/carry/handoff-ETH.json`（協調者寫、兩腿讀；每個幣一個檔） |
| 協調者的 research store | `/home/trader/data/carry/autoresearch.sqlite`（carry 自己的，第一次 `signal` 會建；**不**共用雷達的 `/home/trader/research/data/autoresearch.sqlite`——那是另一個 checkout、另一個寫方） |
| 協調者的 log | `/home/trader/data/carry/carry-signal.log` |
| 協調者的設定 | `/home/trader/TradingAgents/contrib/carry/schedule/carry-signal.local.sh`（§2.3；gitignored） |
| perp 腿的 store | `/home/trader/data/carry/paper_trading.db`（自己的檔，不是 paper-BTC 的 `/home/trader/data/paper_trading.db`）；payload 與 sidecar 在 `/home/trader/data/carry/payloads/carry-ETH-1/`、export 在 `/home/trader/data/carry/exports/carry-ETH-1/` |
| perp 腿的設定 | `/home/trader/TradingAgents/contrib/hyperliquid_perp/configs/hyperliquid-carry-eth.local.yaml`（§2.1；gitignored；**不是** `hyperliquid.local.yaml`，那是 paper-BTC 的） |
| 現貨腿 | run `paper-carry-1`，與另外兩個 run 同一個 store `/home/trader/data/uniswap/paper.db`；設定 `/home/trader/uniswap-paper/contrib/uniswap_v3/configs/paper-carry.local.yaml`（§2.2） |
| units | `/etc/systemd/system/carry-signal.service`、`carry-signal.timer`、`hl-paper-carry-eth.service`（§3；來源在 `contrib/carry/schedule/`） |

`deploy/paper` 的 push 會 reset hl 的 checkout、跑 `contrib/hyperliquid_perp` 與 `contrib/carry` 的測試，然後 restart `hl-paper`
**與**（unit 是 enabled 時；§7 `disable` 掉的不會被拉回來）`hl-paper-carry-eth`；協調者的 timer 不用重啟（oneshot，每次跑當時的 checkout）。perp 的 cycle 是滾動的
4 小時、每個 daemon 各自錨在開段時間，push 前先確認兩個 run 都不在 cycle 中（§5）。uniswap 的 checkout 釘在固定 commit，
visit 腳本的 carry 鏈與 `target` 指令要用它 `lightsail-install.sh <commit>` 升級後才有（uniswap RUNBOOK §0.1）。

---

## 1. 一次性前置

### 1.1 本機：先看規則跑過歷史長什麼樣

```powershell
python -m contrib.autoresearch fetch --db .claude/carry-eth.sqlite --coin ETH --since 2024-01-01
python -m contrib.carry history --coin ETH --research-db .claude/carry-eth.sqlite --rows
```

在場天數比例、進出次數、在場期間的年化 funding；數字不像話就先改 D6 的參數再部署（`--z-in`／`--z-out`／`--min-hold-days`，
之後寫進 §2.3 的 `SIGNAL_ARGS`）。

### 1.2 `--margin-pct` 要落在 perp run 的格點上

perp 的 `decision:` 區塊（範例檔）：`ai_target_margin_min_pct: 0`、`ai_target_margin_max_pct: 100`、`target_margin_step_pct: 1`，
而且 `max_target_margin_pct: 60` 是閘門的上限。預設的 `--margin-pct 30` 落在格點上也在上限內；改任一邊就兩邊一起對：
off-grid 的 margin 會讓 perp 每個 cycle `margin_off_step_grid` fail closed（sidecar 照寫、不下單），現貨腿卻照買＝裸多單。

### 1.3 部署程式碼

兩個 checkout 都要到含本套件 `schedule/` 與 uniswap visit carry 鏈的 commit：

```powershell
git push origin origin/develop:refs/heads/deploy/paper      # hl checkout：workflow 跑測試＋restart hl-paper（§0）
gh run watch <id> --exit-status
scp -i ~/.ssh/<key> contrib/uniswap_v3/schedule/lightsail-install.sh ubuntu@<host>:/tmp/
ssh -i ~/.ssh/<key> ubuntu@<host> sudo sh /tmp/lightsail-install.sh <commit>   # uniswap checkout（它會停 timer 再放回）
```

---

## 2. 設定檔（本機寫好、scp 上去）

### 2.1 perp 腿：`hyperliquid-carry-eth.local.yaml`

從範例複製，只改這幾處（其餘照預設：1x cross、4h K 線、grid 0–100 step 1、`max_target_margin_pct` 60）：

```powershell
Copy-Item contrib/hyperliquid_perp/configs/hyperliquid.example.yaml contrib/hyperliquid_perp/configs/hyperliquid-carry-eth.local.yaml
```

```yaml
wallet_address: "0x..."          # 與 hyperliquid.local.yaml 同一個唯讀 mainnet 位址（讀倉位用；不是私鑰）
coins: [ETH]
decision_source:
  provider: file_target
  target_path: /home/trader/data/carry/handoff-ETH.json
paper_trading:
  account:
    initial_balance_usdc: 10000  # 與現貨腿等額（拍板 2026-10-12：兩邊 10000），weight ≈ margin_pct
```

- `engine:` 區塊留著沒關係：file_target 不問模型、不要 `OPENROUTER_API_KEY`。
- `market_data.autoresearch_signal` 不要打開（那是 paper-BTC 的雷達段，與 carry 無關）。
- `target_path` 的**目錄**在 daemon 建 provider 時必須存在（§2.4 先 `install -d`），檔本身可以還沒有。

### 2.2 現貨腿：`paper-carry.local.yaml`

從範例複製，改成只交易交接檔指名的那個代幣、讀 carry 的目標：

```powershell
Copy-Item contrib/uniswap_v3/configs/uniswap_v3.example.yaml contrib/uniswap_v3/configs/paper-carry.local.yaml
```

```yaml
tokens: [USDC, WETH]
pools:
  - USDC/WETH-500
strategy:
  name: external_weights
  params:
    band: "0.05"
targets:
  source: carry-eth-v1
```

- **不**打開 `verdicts`；`agent` 區塊不用動（carry run 不問 judge）。
- `tokens` 多一個代幣（例如留著 WBTC）`target` 與策略都會拒絕（uniswap README「`targets`」）。
- `targets` 進設定快照：run 開了之後不能再改 source，要改就換 run id。

### 2.3 協調者：`carry-signal.local.sh`

```sh
HANDOFF="/home/trader/data/carry/handoff-ETH.json"
RESEARCH_DB="/home/trader/data/carry/autoresearch.sqlite"
LOG="/home/trader/data/carry/carry-signal.log"
PYTHON="/home/trader/TradingAgents/.venv/bin/python"
SIGNAL_ARGS="--margin-pct 30"
# 兩個 run 都寫出第一筆 equity 之後再加（§4 第 5 步）：
# PERP_DB="/home/trader/data/carry/paper_trading.db"
# PERP_RUN_ID="carry-ETH-1"
# SPOT_DB="/home/trader/data/uniswap/paper.db"
# SPOT_RUN_ID="paper-carry-1"
```

一行一個 `NAME="value"`，值加引號；這個檔會被 `.`（source）進腳本，只放設定行。`PERP_RUN_ID`／`SPOT_RUN_ID` 空著時
兩腿視為等額（兩個 run 都用 10000 開，第一天就是等額）；設了但 store 裡還沒有那個 run 的列，`signal` 會拒絕（打錯 id 與剛開的 run 長得一樣）。

### 2.4 送上伺服器

```powershell
scp -i ~/.ssh/<key> contrib/hyperliquid_perp/configs/hyperliquid-carry-eth.local.yaml contrib/carry/schedule/carry-signal.local.sh contrib/uniswap_v3/configs/paper-carry.local.yaml ubuntu@<host>:/tmp/
ssh -i ~/.ssh/<key> ubuntu@<host> "sudo install -d -o trader -g trader -m 755 /home/trader/data/carry && sudo install -o trader -g trader -m 644 /tmp/hyperliquid-carry-eth.local.yaml /home/trader/TradingAgents/contrib/hyperliquid_perp/configs/ && sudo install -o trader -g trader -m 644 /tmp/carry-signal.local.sh /home/trader/TradingAgents/contrib/carry/schedule/ && sudo install -o trader -g trader -m 644 /tmp/paper-carry.local.yaml /home/trader/uniswap-paper/contrib/uniswap_v3/configs/"
```

（scp 的來源路徑用相對路徑，不要寫 `C:/...`，會被當主機名。）

---

## 3. 安裝 units 與 smoke

```bash
S=/home/trader/TradingAgents/contrib/carry/schedule
sudo install -m 644 $S/carry-signal.service $S/carry-signal.timer $S/hl-paper-carry-eth.service /etc/systemd/system/
sudo systemctl daemon-reload
systemd-analyze verify /etc/systemd/system/carry-signal.service /etc/systemd/system/carry-signal.timer /etc/systemd/system/hl-paper-carry-eth.service
```

`systemd-analyze verify` 要沒有輸出：`carry-signal.service` 是 `Type=oneshot` 加 `Restart=on-failure`（失敗 5 分鐘後重試一次，
`StartLimitBurst=2`＝一小時內最多兩次），這個組合要 systemd ≥ 244，Ubuntu 24.04 的 255 可以；它印出抱怨就把 `Restart=`
那兩行拿掉、改成手動重跑（§6）。

smoke（trader 身分、hl checkout）：

```bash
cd ~/TradingAgents
.venv/bin/python -m contrib.hyperliquid_perp.main --context-only --coin ETH --config contrib/hyperliquid_perp/configs/hyperliquid-carry-eth.local.yaml   # 設定通、連得上 mainnet；exit 0
.venv/bin/python -m pytest -q contrib/carry/tests                                        # 全綠（deploy workflow 也跑它）
```

uniswap 那邊 `lightsail-install.sh` 已經跑過套件測試；`paper-carry.local.yaml` 的格式用 `status` 看一次：

```bash
cd ~/uniswap-paper
.venv/bin/python -m contrib.uniswap_v3 status --config contrib/uniswap_v3/configs/paper-carry.local.yaml --db /home/trader/data/uniswap/paper.db
```

---

## 4. 第一天：開 run 的順序

順序有意義：perp daemon 讀的是交接檔，現貨 run 讀的是 `target` 寫進 store 的目標，兩者都要先有交接檔。

1. **先跑一次協調者**（任何時間；6 小時寬限內它會決定剛過的午夜，否則決定下一個午夜）：
   ```bash
   sudo systemctl start carry-signal.service
   tail -n 20 /home/trader/data/carry/carry-signal.log      # 要看到 "wrote /home/trader/data/carry/handoff-ETH.json" 與 "exit 0"
   ```
   exit 1 看 log 的那一句（README「什麼時候 exit 1」）；最常見是 `--max-reading-age-hours`：離下一個午夜還太遠、23:00 的結算還沒到，
   等 23:50 的 timer 就好。
2. **開 perp daemon**（照 hl-paper 的 SOP：第一次帶 `--create`，開好拔掉）：
   ```bash
   sudo sed -i 's|--db /home/trader/data/carry/paper_trading.db$|--db /home/trader/data/carry/paper_trading.db --create|' /etc/systemd/system/hl-paper-carry-eth.service
   sudo systemctl daemon-reload
   sudo systemctl enable --now hl-paper-carry-eth
   sudo journalctl -u hl-paper-carry-eth -n 40 --no-pager      # created paper run 'carry-ETH-1'；file target ... 那一行
   sudo sed -i 's/ --create$//' /etc/systemd/system/hl-paper-carry-eth.service && sudo systemctl daemon-reload   # 不重啟
   ```
   第一個 cycle 若在午夜前跑、讀到的是明天的檔，log 是 INFO `pending`、維持空手——正常；午夜後的 cycle 才會照檔開空單。
3. **隔天 00:10–04:00 UTC 開現貨 run**（uniswap RUNBOOK §2 的時間窗；先 `target` 再 `paper`，與 visit 鏈同序）：
   ```bash
   cd ~/uniswap-paper
   P=.venv/bin/python; C=contrib/uniswap_v3/configs/paper-carry.local.yaml; DB=/home/trader/data/uniswap/paper.db
   $P -m dotenv run -- $P -m contrib.uniswap_v3 target --config $C --db $DB --handoff /home/trader/data/carry/handoff-ETH.json   # exit 0；3＝交接檔還不是今天的，等協調者
   $P -m dotenv run -- $P -m contrib.uniswap_v3 paper  --config $C --db $DB --run-id paper-carry-1 --balance USDC=10000 --gas-eth 1
   $P -m contrib.uniswap_v3 status --config $C --db $DB --run-id paper-carry-1
   ```
   然後把 carry 鏈交給 visit：`/home/trader/uniswap-paper/contrib/uniswap_v3/schedule/paper-visit.local.sh` 加兩行
   ```sh
   RUN_CARRY="paper-carry-1"
   HANDOFF="/home/trader/data/carry/handoff-ETH.json"
   ```
   （visit 的 carry 鏈與 judge 鏈互不阻擋；`target` 結束碼 3 時 carry 鏈停在那步、當天後面的 visit 重試，uniswap RUNBOOK §3。）
4. **開 timer**：
   ```bash
   sudo systemctl enable --now carry-signal.timer
   systemctl list-timers carry-signal.timer          # 下一次 23:50 UTC
   ```
5. **兩個 run 都有第一筆 equity 後**（perp 第一個 cycle 完成、現貨第一根決策過），把 §2.3 註解掉的四行打開——之後現貨 weight
   就照兩邊真實 equity 算，不再是等額假設。

---

## 5. 日常檢查

```bash
systemctl list-timers carry-signal.timer uniswap-v3-paper.timer --no-pager
systemctl status carry-signal.service --no-pager               # 上一次：status=0/SUCCESS、1、4
tail -n 30 /home/trader/data/carry/carry-signal.log
sudo systemctl is-active hl-paper-carry-eth && sudo journalctl -u hl-paper-carry-eth -n 30 --no-pager   # WARNING "carry handoff unusable" 不該天天出現
cd ~/TradingAgents && .venv/bin/python -m contrib.hyperliquid_perp validate --db /home/trader/data/carry/paper_trading.db --run-id carry-ETH-1
cd ~/uniswap-paper && .venv/bin/python -m contrib.uniswap_v3 status --config contrib/uniswap_v3/configs/paper-carry.local.yaml --db /home/trader/data/uniswap/paper.db --run-id paper-carry-1
```

每週一次 `report`（唯讀，daemon 不用停；從 hl checkout 跑，它讀 uniswap 的 store 只用 sqlite 唯讀）：

```bash
cd ~/TradingAgents
.venv/bin/python -m contrib.carry report --coin ETH \
  --perp-db /home/trader/data/carry/paper_trading.db --perp-run-id carry-ETH-1 \
  --spot-db /home/trader/data/uniswap/paper.db --spot-run-id paper-carry-1
```

讀法在 README「指令」的 `report` 一節；`grep ALERT` 就是當週要看的事。push `deploy/paper` 前確認兩個 perp daemon 都不在 cycle 中
（各自的 `scheduler_state.current_attempt_id` 為 null）。

---

## 6. 看到這些時

| 看到 | 意思 | 做什麼 |
|---|---|---|
| `carry-signal` log `exit 1`，句子是 reading age | 23:00 的結算還沒進 store 或 timer 跑太早／太晚 | 等 service 的 5 分鐘重試；再失敗就在 6 小時寬限內手動 `sudo systemctl reset-failed carry-signal.service && sudo systemctl start carry-signal.service`（`StartLimitBurst=2` 對手動 start 也算，一小時內第三次要先 `reset-failed`） |
| `carry-signal` log `exit 4` | 設定錯：`PYTHON` 路徑不在、run id 與 store 沒成對、log 寫不了 | 修 `carry-signal.local.sh`，再手動 start；這一類不該等 service 重試 |
| `exit 1`，venue 失敗／回補沒走到底 | Hyperliquid 端點掛了 | 同上；兩腿維持現狀（D5），不用動 |
| `exit 1`，`is ... later than` | 交接檔的邊界比要決定的晚：時鐘或 `--as-of` 錯 | 不要刪檔；先弄清楚哪一邊的時間錯 |
| perp journal 每個 cycle `carry handoff unusable` | 協調者整天沒寫檔，或 `target_path` 指錯 | 看 `carry-signal.log`；路徑錯就改 yaml 再 restart（持倉重啟走 protection-only，hl RUNBOOK） |
| perp journal `pending` | 午夜前讀到明天的檔 | 正常，不用管 |
| uniswap visit log carry 鏈 `exit 3` | 交接檔還不是今天的 | 當天後面兩次 visit 會重試；三次都 3 就看協調者 |
| `report` `ALERT delta` | 兩腿數量差超過 10% | 看是哪一腿沒做到：perp 被閘門擋（`refused by the gate`；off-grid 的 margin 也算在這裡）、現貨 `no_trade`／`rejected`（gas 沒了？）；paper 階段**只看不補**（D7）。**大漲跌後隔天的燈多半是 deadband**：現貨 band 0.05（對 0.3 weight 約 17% 相對）與 perp 的 10 點 margin deadband 不同步，ETH 一天動 10–15% 時現貨重平衡、perp 不動；這種燈不算成績單的「delta 亮燈」，只數價格移動解釋不了的 |
| `ALERT silent day` | 那天 perp 沒有任何 cycle、現貨也沒決策 | 兩個 daemon 都停了，或協調者死了讓 visit 的 carry 鏈停在 `target`：`systemctl status` 兩邊都看 |
| `ALERT sidecar not read` | payload 目錄下有一個 `.handoff.json` 讀不出來 | 多半是 restart 切到寫一半；那個 cycle 讀了哪份檔已經查不到，看前後 cycle 的 |
| `ALERT held by D5` | 一腿連續三天沒有可用的交接檔 | 由人介入：先修協調者；要停就 §7 |
| `ALERT one-legged`／`different documents` | 兩腿看到的不是同一天／同一份檔 | 對 `carry-signal.log` 的寫檔時間與兩邊的 sidecar／`targets` 列；排程刻意不重跑成功的那天（README） |
| `ALERT no sidecar` | `--payloads-dir` 指錯或 payload 目錄被清 | 用 `<perp-db 目錄>/payloads/carry-ETH-1` |

成績單門檻（計畫 §3.5）：paper 跑滿 4 週後，`report` 的每週 `net` > 0 **且** `delta` 亮燈每週 ≤ 1 次，才談 live。
**只數滿 7 天的 ISO 週**（`days` 欄＝7）；週中開段的那個殘週帶著進場的 swap 成本與 gas，`net` 必然為負，當脈絡看不計分。
每日 `funding` 後面若印 `(N pending)`，是 venue 當時沒給費率、還沒入帳的結算：那天與合計都少算了它們，入帳後重跑數字會變。

---

## 7. 停掉

```bash
sudo systemctl disable --now carry-signal.timer
sudo systemctl disable --now hl-paper-carry-eth          # SIGTERM：最後一次 export 後退出
# uniswap：paper-visit.local.sh 把 RUN_CARRY 留空（或刪那行）；run 留在 store 裡，report 仍可查
```

兩腿若還有倉位，停掉 daemon 不會平倉：`signal` 不接受人工指定動作，要出場就把兩個門檻暫時都抬到現在的 z 之上
（`SIGNAL_ARGS="--z-in 99 --z-out 98 --margin-pct 30"`；`z_out` 必須低於 `z_in`），持有滿 `min_hold_days` 後下一次決定就是 exit；
等兩邊都 flat（`report` 的 delta 行兩腿都 0）再停、再把門檻改回來。
