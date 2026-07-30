# Live trading RUNBOOK（Phase 3）

從零把 Phase 3 **live** run（testnet_live → mainnet_tiny）掛起來、跑過 smoke、
跑滿驗收的**操作手冊**。只有照做的步驟與日常操作；完整規格見
[phase3-spec](./phase3-spec.md)，paper 版流程見 [RUNBOOK](./RUNBOOK.md)。

> **這是真下單。** testnet 用測試幣、mainnet_tiny 用真錢（上限 100 USDC 名目、
> 1x cross、單一 symbol）。每一步的 gate 都是安全機制，不要繞過。

所有指令都從 repo 根目錄 `TradingAgents/` 執行。live run 的 SQLite store 預設是
`live_trading.db`（與 paper 的 `paper_trading.db` 分開）。

---

## 1. 一次性前置（第一次跑之前）

### 1.1 安裝與自我檢查

```bash
pip install -r requirements.txt
python -m pytest contrib/hyperliquid_perp/tests/ -q   # 全綠才繼續
```

### 1.2 Agent wallet 核准（§6 / §6.1）

live 下單用的是**主錢包授權的 agent key**，不是主錢包私鑰。

1. 到 Hyperliquid（testnet 先做 testnet）用主錢包核准一個 agent wallet，拿到它的
   private key。
2. 依網路 export 環境變數（**永不落 log／yaml／DB**）：
   - testnet：`HYPERLIQUID_AGENT_KEY_TESTNET=0x...`
   - mainnet：`HYPERLIQUID_AGENT_KEY_MAINNET=0x...`
3. 授權有效期有限——`live` 子命令啟動時會用 Info API 反查授權清單，過期或不在列
   就具名拒絕啟動（§6.1）；快到期會印警告，先重新核准再跑長 run。

### 1.3 領測試幣 / 入金

- **testnet**：到 Hyperliquid testnet faucet 給主錢包領測試 USDC。smoke 與
  testnet_live 都在 testnet 花測試幣。
- **mainnet_tiny**：主錢包入金**至少約 167 USDC**。理由：名目上限
  `max_notional_usdc = 100`，而 `max_target_margin_pct = 60%`、`leverage = 1`，
  要讓 pct cap 觸及 100 USDC 名目需要 equity ≥ 100 / 0.6 ≈ 167 USDC；低於這個數，
  `live` 啟動會因 `effective_notional_cap` 低於交易所最小單而具名 exit 1（§5 規則 4）。

### 1.4 `OPENROUTER_API_KEY`

與 paper 相同（見 [RUNBOOK §1.3](./RUNBOOK.md)）：repo 根目錄 `.env`（存 UTF-8）或
使用者層級環境變數。live 迴圈的 4h AI cycle 一樣要它。

### 1.5 建 local config 的 `live:` 區塊

在 `configs/hyperliquid.local.yaml` 補上 `live:` 與明寫的 `risk:` 區塊（§4／§24）。
testnet_live 最小範例：

```yaml
wallet_address: "0xYOUR_MAINNET_READONLY_ADDR"   # 授權對象＝主錢包，live/paper 共用
network: mainnet          # 頂層：paper 讀行情用；live 用的是 live.network
network_timeout_s: 10

risk:                     # live 子命令要求明寫（欄位層級），與 live.safety 交叉檢查
  leverage: 1
  margin_mode: cross
  max_target_margin_pct: 60

live:
  mode: testnet_live      # paper / testnet_live / mainnet_tiny（mainnet_live 拒絕）
  network: testnet
  allow_real_orders: true
  safety:
    allowed_symbols: [BTC]        # single_symbol_only：恰好一個
    max_notional_usdc: 100
    absolute_notional_ceiling: 500
    max_target_margin_pct: 60
    margin_mode: cross
    leverage: 1
  execution:
    execution_style: sliced_twap
    plan_duration_minutes: 60
    max_slippage_pct: 0.005
  # kill_switch / protection / websocket 子區塊照 §4 預設即可
```

先跑一次 config-only gate 檢查（不下單）——授權、caps、signed client 健檢一次跑完：

```bash
python -m contrib.hyperliquid_perp live --config configs/hyperliquid.local.yaml
# stdout 印 mode/network/agent_address/authorization_valid_until/account_equity/
# pct_cap_notional/effective_notional_cap；有任何 gate 失敗會一次列出所有原因後 exit 1
```

---

## 2. 建 live run 並跑 §19.1 startup recovery

> **建 run 前先想好 test 16 的前置**：若要做 smoke test 16（startup with existing
> position），那個小倉要在 **`--create` 之前**先開好，建 run 時用
> `--adopt-positions` 收編進 genesis（見 §3.2）。run 建立之後就**不能**再對這個錢包
> 手動下單。

```bash
# 首次：--create 建 run（genesis＝交易所快照）並跑一次 §19.1 recovery（arm kill switch、
# reconcile、掃 stale bot-owned 單），印判定後退出（不進迴圈）。帳戶非空要 --adopt-positions。
python -m contrib.hyperliquid_perp live \
  --config configs/hyperliquid.local.yaml \
  --run-id live-BTC --db live_trading.db --create
```

exit 0＝recovery 判定通過（可進 cycles）；exit 4＝執行了但判定 unclean（run 進了
safe mode，見 §6）；exit 1＝硬失敗（config／arming／建立）。

> **genesis 之後，錢包只屬於 bot（§11／§12.3）**：live 帳本以交易所帳單為唯一基準，
> run 建立後任何**手動**成交（UI 下單、轉倉）都會變成本地無單可掛的
> `fill_unmapped`／`exchange_position_mismatch` case——`fill_unmapped` 無法用
> `--stamp-case` 了結（只能靠補記 fill，而手動單永遠沒有本地 order row），且驗收的
> integrity 計數是**累積制**：case 一旦記錄，該 run 的 `validate` 從此永遠 exit 5，
> 只能換新 run-id 重跑驗收。要動錢包，先把 run 收掉。

---

## 3. Smoke tests（§20.2）——進 cycles 的硬 gate

testnet_live **必須先全過 18 項 smoke test 才允許 `--loop` 進 cycles**（同一個
run-id）。smoke 對 testnet 真連線、真下小單（每筆約 11 USDC 名目、far-from-market、
reduce-only 或小額真倉，跑完自清）。

### 3.1 先離線驗一次 wiring（不下單）

```bash
python -m contrib.hyperliquid_perp live-smoke \
  --config configs/hyperliquid.local.yaml \
  --run-id live-BTC --db live_trading.db --dry-run
# 每項記 skipped、不下任何單；驗 config 與接線。exit 0＝wiring 檢查完成。
```

### 3.2 為 restart 系列（測 15/16/17）備妥前置

三項 restart/startup 測試**各會**對 run 跑一次真正的 §19.1 recovery（arm kill
switch ＋ reconcile ＋ 掃 stale bot-owned 單）：

- **test 16（startup with existing position）**：那個小倉必須在 **`live --create`
  之前**就開好、並以 `--adopt-positions` 收編進 genesis（§2）。**不要**在 run 建立
  之後才手動開倉——post-genesis 的手動成交沒有本地 order row，會直接變成
  `fill_unmapped`＋`exchange_position_mismatch` case，recovery 從此永遠 unclean、
  該 run 的驗收永久 exit 5（見 §2 的警告框）。
- **test 17（startup with stale bot-owned order）**：這一項在 v1 **沒有可支援的
  前置備置方式**，預設會是 vacuous pass（沒有單可掃，recovery 自然「掃乾淨」）。
  §19.3 只撤 `entry`／`rebalance` 角色的殘單，而 v1 的每一片切片都是 IOC、永遠即時
  終態，不會留下掛單；`stop_loss`／`take_profit` **刻意永不被 §19.3 撤**（PR 4 沒有
  補掛路徑，撤掉不完美的 SL 等於拿保護換整潔），所以拿一張 SL 來充數不會觸發
  sweep。**也不要手動掛一張「看起來像 bot」的單**：bot-ownership 認的是本地
  `cloid_registry` 有沒有這一筆，不是命名前綴——手掛的單會被 sweep 直接跳過
  （測 17 一樣 vacuous），而且會被 reconciler 記成 `non_bot_owned_order`、
  讓 recovery 判定 unclean，於是測 17 記 **failed**（§20.2 gate 關起來）、
  run 還會進 **manual safe mode**，要人工 `safe-mode --release` 才出得來。
  無論如何 **不要**拿「上一輪 smoke 的殘留」充數——smoke 的 trigger 探針**刻意不寫
  本地 orders row**（§12.3 的 orphan lane 負責收編），殘留的探針會在 recovery 第一趟
  就記一筆 `orphan_exchange_order`，依 §2 的累積制，該 run 的 `validate` 從此永遠
  exit 5。這筆 case 不會讓 run 停下來（reconciliation 判定仍 clean、也不進 safe
  mode），要跑到 `validate` 才看得見，所以更要事前避開。
- **test 15（restart reconciliation）**：乾淨重跑 recovery 即可。

不方便一次備齊時，用 `--only` 分項跑（見下）。

> **pre-flight recovery（先讀這段再排順序）**：真跑且選到會下 probe 單的測試
> （3、5–13、18）時，suite 會在第一項測試前**先跑一次** §19.1 recovery——signed
> client 自己的 order gate 要求 recovery 通過＋kill switch armed 才放行任何單。
> 副作用：全套 18 項一次跑時，這個 pre-flight 會先把你為 test 17 備好的 stale 單
> 掃掉（test 17 之後照樣記 passed，但證明的只是「乾淨狀態下 recovery 乾淨」）。
> **要讓 test 16/17 真的驗到前置**，用 `--only` 單獨跑 restart 系列（不含下單測試
> 的選擇不觸發 pre-flight）：
> `--only restart_reconciliation startup_with_existing_position startup_with_stale_open_order`。
>
> **這兩項證明的是什麼**：test 16/17 只斷言 recovery 判定 `passed`，**不會**獨立驗證
> 前置情境真的存在——若沒照上面備妥（或前一次 recovery 已把狀態清乾淨），recovery 仍
> 會判乾淨、這兩項照樣記 passed。因此 §20.3 的
> `startup_with_existing_position_test_passed` /
> `startup_with_stale_open_order_test_passed` 證明的是「**在你備妥的前置下** recovery
> 乾淨」，而非「情境已被自動偵測」——請確實照上面備置後再跑。
>
> **kill switch**：pre-flight 或 restart／kill-switch 系列（測 14–17）的 recovery 會
> arm dead man's switch；觸發過 pre-flight 的真跑在**每項測試前會 refresh** 這個 arm
> （不然 120s 的 scheduleCancel 會在套件跑到一半時觸發、把正在測的 resting probe 撤掉）；
> suite 收尾會**自動 disarm**（清掉 scheduleCancel），不在錢包
> 上留 armed 狀態（完全沒 arm 過的 run 不會去動它，以免誤清掉同錢包上其他 `live
> --loop` 的 arm）。萬一 disarm 失敗，會印一行醒目 `WARNING`——照它指示手動清掉，或
> 直接跑 `live --loop`（會重新 arm 並持續 refresh）。

### 3.3 跑 smoke（真連線）

```bash
# 全部 18 項：
python -m contrib.hyperliquid_perp live-smoke \
  --config configs/hyperliquid.local.yaml \
  --run-id live-BTC --db live_trading.db

# 只跑某幾項（key 見 --gate-status 或錯誤訊息列出的清單）：
python -m contrib.hyperliquid_perp live-smoke \
  --config configs/hyperliquid.local.yaml \
  --run-id live-BTC --db live_trading.db \
  --only stop_loss_create stop_loss_modify stop_loss_cancel
```

> `--only` 有一條配對規則：test 4（`slice_order_status`）查的是同一次執行裡
> test 3 送出的單，單選 test 4 會被入口直接拒絕（exit 1）——兩個一起選：
> `--only slice_order_submit slice_order_status`。兩個都選了、但 test 3 在同一次
> 執行沒跑完（自己出 `error`，或 suite 在它之前就因 `error` 停下）時，test 4 會記
> **`error`**（harness／選測連鎖，不是交易所拒絕的 `failed`）——先修 test 3 的根因，
> 再兩項一起重跑。

每項結果落 `live_smoke_tests`（append-only：修好再跑會覆蓋判定、保留歷史）。
紅的判定分兩型，triage 方式不同：

- **`failed`＝交易所拒絕**（config／市場狀態問題）——suite 記下後**繼續跑**下一項。
- **`error`＝harness bug**（程式自己炸了、帳戶狀態未知）——suite **停在這一項**，
  其後的測試不執行、它們既有的判定不動。先修 code 再回來。

無論哪型，修好原因後 `live-smoke --only <key>` 重跑該項即可（gate 以 latest-per-key
的真跑結果為準）。

四個真跑才有的行為：

- **run 身分檢查**：套件會先比對 run 的 genesis 記錄（coin／`live.network`）與
  今天的 config，不符就具名拒絕（exit 1）——打錯 `--run-id` 指到同一個 db 裡的
  mainnet 驗收 run 時，pre-flight recovery 會拿 testnet 交易所去對那個 run 的帳、
  記下依 §5 累積制**永久**的 integrity case，這道檢查就是擋這個的。
- **run lock**：真跑（非 `--dry-run`／`--gate-status`）會先取 run 的 lease——同一
  run 正被 `live --loop` 跑著時會具名拒絕（exit 1）。先停掉 daemon 再跑 smoke。
  套件跑動中**每項測試前會 heartbeat** 這個 lease；萬一 lease 已被接管（本進程
  卡死超過 stale 門檻後被另一個 live 進程合法接手），套件立刻具名中止（exit 1）
  且**不做**收尾 disarm——kill switch 此時屬於接管者，已記錄的判定不受影響。
- **pre-flight recovery**：見 §3.2。pre-flight 沒過＝suite 直接中止（exit 4、
  不記任何判定）；先查 `safe-mode --status`／log、修好 run 狀態再重跑。
- **probe 成交入帳**：會成交的 probe——測 6/7/18 的開倉／平倉 IOC，**加上**
  trigger-probe 區塊（測 5、8–13）staged 的小多倉（第一個 trigger 測試前才 lazily
  開、最後一個之後 reduce-only 平掉、suite 收尾再 backstop 一次；每張 SL/TP probe
  因此都是真實護倉形狀，不是 flat 帳戶上的空掛單）——以 `smoke|<cloid>` 的 order
  row 記入 `orders`，其成交照一般 §14 流程入帳——它們是這個 run 的真實資金流，會
  出現在 fills／export／replay 裡（開平相抵、只留手續費與滑價）。另外 test 3 的
  far-price IOC 在薄的 testnet book 上偶爾會**意外成交**——suite 會自動把那筆平掉、
  並把異常記進該項的 detail（自清；判定照樣 passed），一樣算真實資金流。
  staged 小多倉萬一**平不掉**（交易所拒絕、只成交一部分、或成交了卻沒能記帳），
  收尾會印一行醒目 `WARNING: the trigger-block staging position may still be OPEN`
  ——它平在測試之間、沒有哪一項的 detail 承接得了，所以這行 stderr 就是唯一的操作
  面訊號：**照它指示到交易所確認並手動平掉**（或重跑一次 suite，收尾會再試一次）。
  部分成交時重試只補送真正的剩餘量，不會重送原始尺寸。

### 3.4 確認 gate（不下單、純讀 DB）

```bash
python -m contrib.hyperliquid_perp live-smoke \
  --run-id live-BTC --db live_trading.db --gate-status
# smoke_gate_passed: yes  → 可進 cycles（exit 0）
# smoke_gate_passed: no   → 印出 not_yet_run / failed / errored 三桶清單（exit 4）
```

> `--gate-status` 只適用 **testnet_live** run：指到 mainnet（或 genesis 沒記
> mode 的）run 會具名拒絕（exit 1）——mainnet run 的 smoke 依 §21.3 在獨立的
> testnet run 上證明，空的 smoke 表是設計、不是待辦。

| `live-smoke` exit | 意義 | 下一步 |
|---|---|---|
| `0` | **全 18 項** gate 開（每項最新真跑結果都 passed） | 可進 testnet_live cycles |
| `4` | 跑了（或讀了）但 gate 未滿足；含 pre-flight recovery 沒過、或跑到一半出 `error` 判定，suite 提前中止 | 看 not_yet_run／failed／errored（或 pre-flight 錯誤訊息），補跑或修 |
| `1` | config／env／網路／run-lock 具名錯誤 | 依訊息修 |

> `--only` 只跑子集時，即使選到的項全過，其餘未跑的項仍讓 gate 未開——所以 exit 仍是
> `4`（`not_yet_run` 會列出剩下的項）。exit `0` 一律代表整個 §20.2 gate 開，不是「選到的
> 那幾項過了」。

---

## 4. testnet_live cycles（§20.1）

smoke 全過後，同一 run 加 `--loop` 進 4h AI cycle ＋ 30s tick 的 live 迴圈：

```bash
python -m contrib.hyperliquid_perp live \
  --config configs/hyperliquid.local.yaml \
  --run-id live-BTC --db live_trading.db --loop
```

- 若 smoke gate 未過，`--loop` 會具名 **exit 4**（`testnet_live cycles are gated on
  the §20.2 smoke suite ...`）——「還沒到 gate」，與 config／授權／環境類的 exit 1
  區分——先回 §3。gate 開著時會印最舊一筆通過結果的時間戳——
  smoke 通過**沒有時效**，程式或 config 大改後請自行重跑 `live-smoke`。
- 迴圈每 ~10s tick（在 30s kill-switch 預算內）：排空 WS queue → 刷 kill switch →
  reconciliation → SL/TP protection → 到期切片；4h AI decision 在背景 thread。
- Ctrl-C／SIGTERM 安全停止並跑 §18.2 shutdown sweep。
- **長駐建議**同 paper（[RUNBOOK §3](./RUNBOOK.md)）：掛在會自動重啟的監管下，
  working directory 設 repo 根目錄。監管（systemd 等）的重啟策略可依 exit code
  分流：**4**＝smoke gate 未開（重啟不會自己好，先去跑 `live-smoke`）、**1**＝
  config／憑證／環境錯誤——兩者都不該無腦無限重啟。注意 live 的無人看管空窗風險比
  paper 高——真錢／真倉。

跑滿 **≥ 30 cycles**（§20.3）。

---

## 5. 驗收（§20.3）

```bash
python -m contrib.hyperliquid_perp validate --run-id live-BTC --db live_trading.db
```

live run 會自動走 §20.3／§21.4 報告（依 `live.mode`）。指標與 exit：

| `validate` exit | 意義 | 下一步 |
|---|---|---|
| `0` | `live_ready` — 全部驗收指標達標 | testnet_live 通過；可準備 mainnet_tiny |
| `4` | 一致但未到 gate（cycles/orders 未滿、smoke 未跑**或 failed/errored**） | 繼續跑；smoke 紅的修好後 `live-smoke --only <key>` 重跑（latest-per-key 覆蓋） |
| `5` | integrity failure（dedupe error、orphan、position/replay mismatch、unprotected 秒數 > 0、refresh rate < 99%；mainnet_tiny 另含未解 reconciliation／daily-loss 破線） | 先調查再相信結果 |

> smoke 的 failed／errored **不算** exit 5——它可補救（修好原因、`live-smoke --only
> <key>` 重跑，latest-per-key 覆蓋），歸 exit 4 的「未到 gate」。exit 5 保留給不可
> 補救的 integrity 條件（見下方累積制警告框）。

§20.3 驗收門檻（testnet_live）：`cycle_count ≥ 30`、`live_order_count ≥ 30`、
`exchange_fill_dedupe_error_count / orphan_exchange_order_count /
duplicate_fill_apply_count / local_exchange_position_mismatch_count /
account_replay_mismatch_count / unprotected_position_seconds` 全為 0、
`kill_switch_refresh_success_rate ≥ 99%`、四項 `*_test_passed`（restart / emergency
close / existing position / stale order，來自 smoke 15/16/17/18）皆 true。

> **`cycle_count` 只認 `completed`**：模型輸出解不開的 cycle（`invalid_output`）
> **不計入** ≥30，另以 `invalid_output_count` 與一則 warning 呈現。paper 驗收器計
> 它（那裡量的是「排程有沒有在跑」），live 驗收器不計——這裡問的是「bot 能不能
> 交易」，而 §21.4 沒有 order count 可以背書；不然 30 個連續解不開、一單沒下的
> cycle 也會報 `live_ready`（paper-BTC 換模後就出現過 6/6 `invalid_output`）。

> **unprotected 秒數只算「沒有一張足以覆蓋的 SL」的時段**：§17.4 是
> modify-before-cancel，所以 wire gate 擋掉一次 **modify** 時舊的那張 SL 可能還掛在
> 交易所。判準是**覆蓋**不是存在（與 §12.3 `_has_valid_sl` 判準一致，且更嚴——它
> 會把交易所側多張單的覆蓋量加總，這裡只看本地那一張）：平倉方向
> 正確、且 `qty ≥ 目前倉位`，才算還有保護、才**不開窗**（事件上會帶那張單的
> order_id）。最常見的 blocked 其實是 **resize**——後面的切片成交了、舊 SL 只蓋得住
> 一部分——那**照樣開窗**，因為有一部分倉位真的沒有停損。真的一張都沒有的 blocked、
> 以及 `stop_loss_repair_exhausted`，同樣開窗、同樣是 exit 5。

報告中的 `warning:` 行不影響 exit，但寫結論前要看過。testnet 報告會警告：run 中
發生過 emergency close（§21.4「不得因 bot bug emergency close」無法機器判定，需
人工看 stop_loss_repair 證據）、daily-loss 破線紀錄、還開著的人工 §12.3 case
（mainnet 是硬 gate，testnet 先提醒你在準備 mainnet 前解掉）、以及上面說的
`invalid_output` cycle 數。

> **integrity 計數是累積制**：dedupe error／orphan／position mismatch 等 failure
> 計數算的是「這個 run 歷史上記錄過的 case 數」，`--stamp-case` 只了結 §21.4 的
> unresolved gate，**不會**把計數歸零——一旦記錄過，該 run 的 `validate` 永遠
> exit 5。這是刻意的政策（帳本潔癖：驗收 run 必須全程乾淨）；中途出過 case 就換
> 新 run-id 重新累積 30 cycles。

---

## 6. 監控與 safe mode 處置

日常監控同 paper（[RUNBOOK §5](./RUNBOOK.md)）再加 live 專屬：

| 看什麼 | 在哪裡 | 正常 | 異常時 |
|---|---|---|---|
| kill switch 刷新 | stderr log／`kill_switch_events` | 每 30s 一次 `kill_switch_refreshed` | 連續 `kill_switch_refresh_failed` → 進 safe mode、擋新單，查網路 |
| reconciliation | `exchange_reconciliation_events` | 無 open case | 有 mismatch → safe mode（見下） |
| protection | `protection_order_events` | 有部位時 SL 在書上 | `stop_loss_repair_exhausted` → unprotected，可能 emergency close |
| 中途健檢 | `validate --run-id live-BTC --db live_trading.db` | exit 4（一致、未滿） | exit 5 → 停下來調查 |

**Safe mode**（§13）：進入來源有 WS 斷線 > 5min、kill switch 刷新失敗、
reconciliation mismatch、非 bot-owned 單、daily/consecutive loss。分兩型：

- **recoverable**（§13.4）：下一輪乾淨 reconciliation 自動解除；SL/TP 仍在看管。
- **manual**（§13.5）：需人工介入。查狀態與解除：

```bash
# 看目前 safe-mode 狀態、歷史、open reconciliation cases（exit 0＝無、4＝latched）
python -m contrib.hyperliquid_perp safe-mode --run-id live-BTC --db live_trading.db --status

# 人工核對後解除 manual safe mode（解除不等於恢復交易——還要過下一輪 reconciliation）
python -m contrib.hyperliquid_perp safe-mode --run-id live-BTC --db live_trading.db \
  --release --reason "已人工核對交易所倉位與本地一致"

# 對只能人工處置的 §12.3 case 標記處置（fill_unmapped 例外，要靠補記 fill）
python -m contrib.hyperliquid_perp safe-mode --run-id live-BTC --db live_trading.db \
  --stamp-case <event_id> --action "已確認為交易所延遲、無需動作"
```

---

## 7. mainnet_tiny（§21）——真錢，最嚴 gate

**只有 testnet smoke ＋ testnet_live 驗收都過了才進 mainnet_tiny。**

### 7.1 進入條件（§21.3）

```
Phase 2 paper 驗收通過
testnet smoke tests 全過
testnet_live_cycles >= 30（§20.3 驗收 exit 0）
emergency close / kill switch / SL 建立失敗路徑 / restart reconciliation 都測過
max_target_margin_pct = 60、max_notional_usdc = 100、leverage = 1、single_symbol_only
```

### 7.2 切 config 與 hard gate

把 `live:` 區塊改成：

```yaml
live:
  mode: mainnet_tiny
  network: mainnet
  allow_real_orders: true
  # safety 同上（max_notional_usdc: 100、leverage: 1、single_symbol_only、cross）
```

mainnet_tiny 有 **config-load 硬 gate**（§24）：`max_notional_usdc <= 100` 且
`max_target_margin_pct <= 60`，加全域 `leverage = 1`、`single_symbol_only = true`——
更緊可以，更鬆或設 `mainnet_live` 一律具名拒絕啟動。

### 7.3 跑法

用新的 run-id（例如 `mainnet-BTC`）避免與 testnet run 混帳：

```bash
export HYPERLIQUID_AGENT_KEY_MAINNET=0x...
python -m contrib.hyperliquid_perp live --config configs/hyperliquid.local.yaml \
  --run-id mainnet-BTC --db live_trading.db --create        # 建 + recovery
# mainnet_tiny 依賴 testnet 已過的 smoke（§21.3），--loop 不再擋同一 run 的 smoke gate
python -m contrib.hyperliquid_perp live --config configs/hyperliquid.local.yaml \
  --run-id mainnet-BTC --db live_trading.db --loop           # 跑 cycles
```

跑滿 ≥ 30 cycles 後：

```bash
python -m contrib.hyperliquid_perp validate --run-id mainnet-BTC --db live_trading.db
```

§21.4 驗收（mainnet_tiny）：`mainnet_tiny_cycles ≥ 30`、無 unprotected 部位、
無 orphan bot-owned 單、無 duplicate fill、無未解 reconciliation mismatch、
daily loss cap 未破、`kill_switch_refresh_success_rate ≥ 99%`（比 §21.4 條文嚴——
真錢 run 的 dead man's switch 必須持續在動，2026-07-27 拍板）、（人工確認）無因
bot bug 的 emergency close、手動 shutdown/restart 測過。最後兩項機器判不了：
報告對 mainnet run 固定印 `warning:` 提醒「manual shutdown/restart 為人工確認
項」，exit 0 不代表它自動成立——go-live 前自己打勾。

### 7.4 之後

mainnet_tiny 通過**不會**自動升級 mainnet_live、也不會自動放大資金——一切上限與
mode 切換都手動改 config（§22／§26）。

---

## 8. 快速故障排除

| 症狀 | 解法 |
|---|---|
| `HYPERLIQUID_AGENT_KEY_TESTNET/_MAINNET is not set` | 依 §1.2 export 對應網路的 agent key。 |
| `agent authorization failed` / 過期 | 重新核准 agent wallet（§1.2）。 |
| `effective_notional_cap ... below the exchange minimum`（exit 1） | 入金不足；見 §1.3（mainnet ≥ ~167 USDC）。 |
| `--loop` 報 §20.2 smoke gate 未過（exit 4） | 先跑 `live-smoke`（§3），`--gate-status` 確認 yes 再 `--loop`。 |
| `live-smoke` 報 run lock 被持有（exit 1） | 同一 run 的 `live --loop`／`paper` 還在跑；先停掉（或等 lease 過期）再跑 smoke。 |
| `live-smoke` 報 pre-flight recovery 沒過（exit 4） | run 狀態不乾淨；`safe-mode --status` 查 open case、照 §6 處置後重跑。 |
| `live.allow_real_orders is false` | live-smoke／--loop 要真下單；設 `allow_real_orders: true` 並備妥 agent key，或 live-smoke 用 `--dry-run`。 |
| `validate` exit 5、replay unverifiable | store 帳本對不上；先查（別盲目重啟），必要時 `safe-mode --status`。 |
| run 反覆進 manual safe mode | 查 `safe-mode --status` 的 open cases；換 coin／改 run 定義是硬錯誤，用新 run-id。 |

更多規格細節見 [phase3-spec](./phase3-spec.md)。
