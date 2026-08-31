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
- **mainnet_tiny**：主錢包**建議**入金約 167 USDC 以上。理由：名目上限
  `max_notional_usdc = 100`，而 `max_target_margin_pct = 60%`、`leverage = 1`，
  要讓 pct cap 觸及 100 USDC 名目需要 equity ≥ 100 / 0.6 ≈ 167 USDC。
  這是**建議值、不是機器門檻**：低於它 run 照樣啟動，只是
  `effective_notional_cap = min(equity × 0.6, 100)` 跟著變小（equity 50 → 上限 30）。
  真正會擋啟動的是 §5 規則 4——`effective_notional_cap` 低於交易所最小單
  （10 USDC）才具名 exit 1，換算約 equity < 16.7 USDC。

### 1.4 `OPENROUTER_API_KEY`

與 paper 相同（見 [RUNBOOK §1.3](./RUNBOOK.md)）：repo 根目錄 `.env`（存 UTF-8）或
使用者層級環境變數。live 迴圈的 4h AI cycle 一樣要它。

### 1.5 建 local config 的 `live:` 區塊

在 `contrib/hyperliquid_perp/configs/hyperliquid.local.yaml` 補上 `live:` 與明寫的 `risk:` 區塊（§4／§24）。
testnet_live 最小範例：

```yaml
wallet_address: "0xYOUR_MAINNET_READONLY_ADDR"   # 授權對象＝主錢包，live/paper 共用
network: mainnet          # 頂層：paper 讀行情用；live 用的是 live.network
# 兩道界限，取較嚴的那個——都以預設 refresh 30s／max_tick_gap 30s／
# schedule_cancel 120s 計：
#   硬性（擋啟動，exit 1）：< 15。kill_switch_timing_violation 自 2026-08-01 起
#     把「失敗那次自己燒掉的 timeout」與「retry 也要再等一個 tick」算進最壞情況
#     （30 + 30 + t + min(t,15) + 30 < 120），所以 network_timeout_s 的**預設
#     30 在 live 下並不合法**，啟動會被具名拒絕。
#   advisory（只出聲，不擋）：< 10。最長的一條無 refresh 鏈是一次下單的 3 筆
#     REST（§8.3 前置查詢→下單→重複 ack 查詢），中間不 refresh kill switch。
# （決策 cycle 的 4 筆行情讀取本來更長，2026-08-01 起已改為讀取之間各 refresh
# 一次——否則這裡要壓到 7.5 以下，而決策 cycle 沒有 within-cycle retry，
# 一筆行情讀取逾時就是 4 小時沒有決策。）
network_timeout_s: 8

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
    default_style: sliced_twap
    plan_duration_minutes: 60
    max_slippage_pct: 0.005
  # kill_switch / protection / websocket 子區塊照 §4 預設即可
```

先跑一次 config-only gate 檢查（不下單）——授權、caps、signed client 健檢一次跑完：

```bash
python -m contrib.hyperliquid_perp live --config contrib/hyperliquid_perp/configs/hyperliquid.local.yaml
# stdout 印 mode/network/agent_address/authorization_valid_until/account_equity/
# pct_cap_notional/effective_notional_cap；有任何 gate 失敗會一次列出所有原因後 exit 1
```

---

## 2. 建 live run 並跑 §19.1 startup recovery

> **建 run 前先想好 test 16 的前置**：若要做 smoke test 16（startup with existing
> position），那個小倉要在 **`--create` 之前**先開好，建 run 時用
> `--adopt-positions` 收編進 genesis（見 §3.2）。run 建立之後就**不能**再對這個錢包
> 手動下單。
>
> **那個小倉必須是「多單」（或不開倉）**：smoke 的探針全是多單形狀，平倉與觸發單
> 都是 reduce-only SELL；帳戶淨空倉時開倉的 BUY 只會縮小空單，之後每一張
> reduce-only SELL 都會被交易所拒絕。suite 會在探針區塊起點就以具名理由擋下
> （訊息含 `net SHORT`），不會讓你看到 7 筆假的「exchange refused」。

```bash
# 首次：--create 建 run（genesis＝交易所快照）並跑一次 §19.1 recovery（arm kill switch、
# reconcile、掃 stale bot-owned 單），印判定後退出（不進迴圈）。帳戶非空要 --adopt-positions。
python -m contrib.hyperliquid_perp live \
  --config contrib/hyperliquid_perp/configs/hyperliquid.local.yaml \
  --run-id live-BTC --db live_trading.db --create
```

exit 0＝recovery 判定通過（可進 cycles）；exit 4＝執行了但判定 unclean（run 進了
safe mode，見 §6）；exit 1＝硬失敗（config／arming／建立）。

> **genesis 之後，錢包只屬於 bot（§11／§12.3）**：live 帳本以交易所帳單為唯一基準，
> run 建立後任何**手動**成交（UI 下單、轉倉）都會變成本地無單可掛的
> `fill_unmapped`／`exchange_position_mismatch` case——`fill_unmapped` 無法用
> `--stamp-case` 了結（只能靠補記 fill，而手動單永遠沒有本地 order row），且驗收的
> integrity 計數是**累積制**：case 一旦記錄，該 run 的 `validate` 從此永遠 exit 5，
> 只能換新 run-id 重跑驗收（**換 run-id 一併把 §20.2 smoke gate 歸零**：18 項全回 `not_yet_run`，要整套 live-smoke 重跑，含 test 16／17 的操作者前置）。要動錢包，先把 run 收掉。

---

## 3. Smoke tests（§20.2）——進 cycles 的硬 gate

testnet_live **必須先全過 18 項 smoke test 才允許 `--loop` 進 cycles**（同一個
run-id）。smoke 對 testnet 真連線、真下小單（每筆約 11 USDC 名目、far-from-market、
reduce-only 或小額真倉，跑完自清）。

### 3.1 先離線驗一次 wiring（不下單）

```bash
python -m contrib.hyperliquid_perp live-smoke \
  --config contrib/hyperliquid_perp/configs/hyperliquid.local.yaml \
  --run-id live-BTC --db live_trading.db --dry-run
# 每項記 skipped、不下任何單；驗 config 與接線。exit 0＝wiring 檢查完成。
```

> `--dry-run` 不取 lease，因此**刻意不 migrate** store（同 `validate`／`export`／
> `--gate-status` 的唯讀政策——它不能在別的 daemon 腳底下升 schema）。所以剛拉了帶
> 新 migration 的 code 之後，這一步會先 exit 1 說 schema 版本不符。先讓一個**擁有這個
> store 的**指令升級它，再回來跑：`safe-mode --status --run-id <id> --db <db>` 是最輕的
> 一個（純診斷、不碰交易所、不 arm 錢包）——但它**不做**下面那個 sibling 檢查，跑之前
> 自己確認同一個 db 檔沒有別的 run 在跑；`paper` 與真跑的 `live-smoke`（不帶
> `--dry-run`）是**取得本 run 的 lease 之後**才升級，`live` 則是先以唯讀方式確認本 run
> 沒有活 lease、同錢包沒有活的 sibling 才升級（它的 lease 在 identity 檢查之後才取）。
> 三者都是：本 run 被別的 process 握著時退出、store 版本不動；而且只要**真的需要升級**，
> 同一個 db 檔裡**任何**其他 run 有活 lease（例如 §7.3 共用 `live_trading.db` 的另一個
> network 的 run、或同檔的 paper run）也會具名退出——先停掉那個 daemon 再升（issue #129）。
> store 已是最新版時不擋，sibling 還在跑照樣可以重啟。

### 3.2 為 restart 系列（測 15/16/17）備妥前置

三項 restart/startup 測試**各會**對 run 跑一次真正的 §19.1 recovery（arm kill
switch ＋ reconcile ＋ 掃 stale bot-owned 單）：

- **test 16（startup with existing position）**：那個小倉必須在 **`live --create`
  之前**就開好、並以 `--adopt-positions` 收編進 genesis（§2），而且**必須是多單**
  （見 §2 的說明框：淨空倉會讓整個探針區塊以 `net SHORT` 具名中止）。**不要**在 run 建立
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
  讓 recovery 判定 unclean，於是**測 15/16/17 全部**記 **failed**（三項都讀同一次
  recovery 判定，§20.2 gate 關起來）、
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
> （不然 scheduleCancel 會在套件跑到一半時觸發、把正在測的 resting probe 撤掉；
> suite 用的窗是 `max(schedule_cancel_seconds, 120s)`，不是寫死的 120s）；
> suite 收尾會**自動 disarm**（清掉 scheduleCancel），不在錢包
> 上留 armed 狀態（完全沒 arm 過的 run 不會去動它，以免誤清掉同錢包上其他 `live
> --loop` 的 arm）。萬一 disarm 失敗，會印一行醒目 `WARNING`——照它指示手動清掉，或
> 跑一次 `live --run-id ...`（不加 `--loop`）重新 arm＋sweep 後退出。**不要指望
> `live --loop`**：disarm 失敗通常伴隨 wire 失敗，那些測試會是 `failed`／`errored`、
> §20.2 gate 因此關著，`--loop` 會在 arm 之前就 exit 4。

### 3.3 跑 smoke（真連線）

```bash
# 全部 18 項：
python -m contrib.hyperliquid_perp live-smoke \
  --config contrib/hyperliquid_perp/configs/hyperliquid.local.yaml \
  --run-id live-BTC --db live_trading.db

# 只跑某幾項（key 見 --gate-status 或錯誤訊息列出的清單）：
python -m contrib.hyperliquid_perp live-smoke \
  --config contrib/hyperliquid_perp/configs/hyperliquid.local.yaml \
  --run-id live-BTC --db live_trading.db \
  --only stop_loss_create stop_loss_modify stop_loss_cancel
```

> `--only` 有一條配對規則：test 4（`slice_order_status`）查的是同一次執行裡
> test 3 送出的單，單選 test 4 會被入口直接拒絕（exit 1）——兩個一起選：
> `--only slice_order_submit slice_order_status`。**test 3 的判定幾乎總是
> `passed`**：無論成交、被交易所拒絕、還是根本沒成交，只要動作本身送到了撮合
> 引擎、沒有頂層例外，就算過（拒絕原因會夾在 `detail` 裡供診斷，不影響判定）。
> test 3 會記 **`failed`** 的是「動作根本沒能好好送出去」那一類：IOC 異常以
> `resting` 狀態回來（交易所語意違反，harness 自動撤單並判定失敗），或下單前
> 就過不了自身守門（例如 mark price 讀回非正值，無法算出探針尺寸）。這些情形
> 下 test 3 都沒能設好 test 4 要查的 handle，test 4 會記 **`error`**（harness／
> 選測連鎖，不是它自己查到的拒絕）。若 test 3 自己出的是 **`error`**（其他 harness 例外），suite 就停在
> test 3，test 4 **根本不會執行、也不寫 row**（維持 `not_yet_run`）——別去找一筆
> 不存在的 test 4 `error`。兩種情形都一樣：先修 test 3 的根因，再兩項一起重跑。

每項結果落 `live_smoke_tests`（append-only：修好再跑會覆蓋判定、保留歷史）。
紅的判定分兩型，triage 方式不同：

- **`failed`＝交易所拒絕**（config／市場狀態問題）——suite 記下後**繼續跑**下一項。
- **`error`＝harness bug**（程式自己炸了、帳戶狀態未知）——suite **停在這一項**，
  其後的測試不執行、它們既有的判定不動。先修 code 再回來。

無論哪型，修好原因後 `live-smoke --only <key>` 重跑該項即可（gate 以 latest-per-key
的真跑結果為準）。**唯一例外是 `slice_order_status`**：依上面的配對規則，它必須和
`slice_order_submit` 一起選，單獨重跑會被入口拒絕（exit 1）。

真跑才有的行為：

- **run 身分檢查**：套件會先比對 run 的 genesis 記錄（coin／`live.network`）與
  今天的 config，不符就具名拒絕（exit 1）——打錯 `--run-id` 指到同一個 db 裡的
  mainnet 驗收 run 時，pre-flight recovery 會拿 testnet 交易所去對那個 run 的帳、
  記下依 §5 累積制**永久**的 integrity case，這道檢查就是擋這個的。
- **run lock**：真跑（非 `--dry-run`／`--gate-status`）會先取 run 的 lease——同一
  run 正被 `live --loop` 跑著時會具名拒絕（exit 1）。先停掉 daemon 再跑 smoke。
  套件跑動中**每項測試前會 heartbeat** 這個 lease；萬一 lease 已被接管（本進程
  卡死超過 stale 門檻後被另一個 live 進程合法接手），套件立刻具名中止（exit 1）
  且**收尾完全不碰交易所**——不 disarm（kill switch 此時屬於接管者），**也不平掉
  staged 小多倉**（那口倉現在是接管者的部位，接管者的 §19.1 recovery 會收編它；
  平掉它等於平掉別人的倉）。改印一行 `WARNING: ... lease was taken over` 交接。
  已記錄的判定不受影響。
- **同一個 db 裡的其他 run**：lease 是綁 `run_id` 的，但 kill switch、
  `updateLeverage`、§19.3 掃單都是**整個帳戶**層級。所以若同一個 db 裡**同網路**的
  另一個 run 正被跑著（lease 還新鮮），smoke 會具名拒絕（exit 1）——否則它會扒掉
  那個 run 的 dead-man cover、撤掉它的掛單。**不同網路**的 run（§7.3 把 mainnet
  驗收 run 放在同一個 `live_trading.db`）是不同交易所、不同帳戶，**不算衝突**、
  不會被擋；同一個 db 裡的 **paper** run 也不算（它一張真單都不簽）。
  **所有 `live` 呼叫**（含不帶 `--loop` 的單發 §19.1 recovery）從 2026-07-31 起
  套用**同一道檢查**——它 arm/clear 的也是整帳戶的 scheduleCancel，§19.3 掃單
  認 bot-owned 時也不帶 `run_id`。
  ⚠️ **不要用「換一個 `--db`」繞過這條**：危害是**每個錢包**的，同網路的兩個 run
  共用同一個錢包，換 store 只是讓這道檢查看不見對方，危害原封不動。真正要隔離就得
  換錢包（換 `HYPERLIQUID_*_AGENT_KEY` 指向的帳戶）。
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
  面訊號。同一次執行內收尾會再試一次（部分成交時只補送真正的剩餘量，不會重送原始
  尺寸），但**重跑 suite 救不了**——`_staged_long` 是每個 runner 實例自己的，新的一次
  執行只會再開一口新的、平掉新的那口，原殘倉留著。所以：**到交易所確認並手動平掉，
  然後換一個新的 run-id 重跑驗收**——依 §2 的累積制，手動成交會記
  `fill_unmapped`，該 run 的 `validate` 從此永遠 exit 5。
  **換 run-id 也會把 §20.2 的 smoke gate 一併歸零**：`live_smoke_tests` 是 per-run-id 的，
  新 run-id 底下 18 項全是 `not_yet_run`，`live --loop` 會直接 exit 4。所以換完 run-id
  要**整套 live-smoke 重跑一次**（含 test 16／17 需要操作者事先備妥的前置狀態），
  不是只把殘倉平掉就好。
  （**唯一例外**：若殘倉的成因是 lease 被接管，收尾**刻意不平**，因為那口倉已經
  屬於接管者、由它的 §19.1 recovery 收編——此時不要手動平，先確認接管的那個
  進程是不是你要的，見上面的 run lock 條目。）
- **另外兩族收尾 `WARNING`**（與 staging 殘倉同一個 stderr 區塊，同樣是唯一的操作面
  訊號）：
  - `a probe position may still be OPEN` — 意外成交的探針倉沒能完全平掉（test 3 的
    far IOC、測 6/7 的切片）。處置同 staging 殘倉：手動平掉、換新 run-id。
  - `trigger probe(s) may still REST on the exchange` — 探針掛單撤不掉。**這族最重**：
    trigger 探針刻意不寫本地 orders row，而 §19.3 掃單對 `stop_loss`／`take_profit`
    是「驗證但不撤」，所以下一次 `live` 啟動會把它記成 `orphan_exchange_order`，依 §2
    的累積制，**該 run-id 的 `validate` 從此永遠 exit 5**。進 cycles 前務必到交易所
    確認並手動撤掉。
- **SIGTERM**：`live-smoke` 與 `live`／`paper` 一樣安裝了 SIGTERM handler（取得 lease
  之後），所以 `kill <pid>`／`systemctl stop`／`timeout` 包裝都會走與 Ctrl-C 相同的
  收尾（掃探針、平 staging 倉、disarm、印上面這些 WARNING、放掉 lease），exit 130。

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
> `4`（`not_yet_run` 會列出剩下的項）。真跑的 exit `0` 代表整個 §20.2 gate 開，不是「選到的那幾項過了」。
> （`--dry-run` 是另一回事：它不下單、每項記 skipped，照樣 exit 0 而 gate 仍關著——
> stdout 的 `smoke_gate_passed: no` 與 dry-run 橫幅是分辨依據。）

---

## 4. testnet_live cycles（§20.1）

smoke 全過後，同一 run 加 `--loop` 進 4h AI cycle ＋ ~10s tick 的 live 迴圈：

```bash
python -m contrib.hyperliquid_perp live \
  --config contrib/hyperliquid_perp/configs/hyperliquid.local.yaml \
  --run-id live-BTC --db live_trading.db --loop
```

- 若 smoke gate 未過，`--loop` 會具名 **exit 4**（`testnet_live cycles are gated on
  the §20.2 smoke suite ...`）——「還沒到 gate」，與 config／授權／環境類的 exit 1
  區分——先回 §3。gate 開著時會印最舊一筆通過結果的時間戳——
  smoke 通過**沒有時效**，程式或 config 大改後請自行重跑 `live-smoke`。
  **重跑前先確認這個 run 沒有持著空倉**：探針全是多單形狀，淨空倉時整個探針區塊
  會以 `net SHORT` 具名中止。出路只有兩條：**(a)** 重開 `live --loop` 等 AI 自己
  平掉倉位，或 **(b)** 在帳戶 flat／多倉的狀態下用**新的 `--run-id`** 重跑整套。
  **不要手動平倉**——手動成交沒有本 run 的 order row，會記 `fill_unmapped`，把
  這個 run 的 `validate` 永久釘在 exit 5（見 §2 的警告框），而且 unmapped fill
  不會套用到本地倉位，所以連這道守衛都解不開。
- 迴圈每 ~10s tick（在 30s kill-switch 預算內）：排空 WS queue → 刷 kill switch →
  reconciliation → SL/TP protection → 到期切片；4h AI decision 在背景 thread。
- Ctrl-C／SIGTERM 安全停止並跑 §18.2 shutdown sweep。
- **長駐建議**同 paper（[RUNBOOK §3](./RUNBOOK.md)）：掛在會自動重啟的監管下，
  working directory 設 repo 根目錄。監管（systemd 等）的重啟策略可依 exit code
  分流：**4**＝smoke gate 未開（重啟不會自己好，先去跑 `live-smoke`）、**1**＝
  config／憑證／環境錯誤——兩者都不該無腦無限重啟。**exit 1 有一個例外是暫時性的**：
  同錢包姊妹 run 還持著新鮮 lease 時的具名拒絕（訊息含 `ACCOUNT-wide`），等對方
  收工或 lease 過期後重跑就會好——但那代表有兩個 run 同時被啟動，該查的是啟動來源。
  另外 `live`／`live-smoke` 收到 SIGTERM 會走與 Ctrl-C 相同的收尾（exit 130）。注意 live 的無人看管空窗風險比
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
| `4` | 一致但未到 gate（cycles/orders 未滿、smoke 未跑**或 failed/errored**、**kill-switch daemon refresh 事件數 < 100**（live-smoke 寫的列不計，見下）、**`no_decision_streak` ≥ 3**——最近連續 ≥3 個 cycle 都沒出決策（不分成因；報告另印 `stale_feed_refusal_streak` 讓你分辨是不是 RUNBOOK §7 的 `freshness limit`），倉位只靠 SL/TP 撐著；下一個決策 cycle 自動歸零，run 停掉超過 2 個 cycle 後也不再套用） | 繼續跑；smoke 紅的修好後 `live-smoke --only <key>` 重跑（latest-per-key 覆蓋）；streak 亮了先看 `stale_feed_refusal_streak`：等於 streak 就查交易所 K 線 API（K 線視窗與年齡都只量交易所自己的時鐘，issue #124 起主機時鐘造不出這條拒跑；`timedatectl` 另由 log 的 skew WARNING 提醒），否則看 `decision_attempts.error_type` |
| `5` | integrity failure（dedupe error、orphan、position/replay mismatch、unprotected 秒數 > 0、refresh rate < 99%、**`kill_switch_fired_count` > 0**、**run 仍在 MANUAL safe mode**；mainnet_tiny 另含未解 reconciliation／daily-loss 破線） | 先調查再相信結果 |

報告在 `live_ready:` 之前另印 `prompt_regime:` 行——每組 `(prompt_version, context_shape,
format_fingerprint)` 的 cycle 數（只數 `completed`，與 `cycle_count` 同口徑；依首見順序）。
不影響 exit code：多於一行＝run 跨過 prompt 制度邊界；`n/a` 是該欄寫入時還不存在，
不是另一個制度。三個鍵的定義見 [RUNBOOK §4](./RUNBOOK.md)。

> smoke 的 failed／errored **不算** exit 5——它可補救（修好原因、`live-smoke --only
> <key>` 重跑，latest-per-key 覆蓋），歸 exit 4 的「未到 gate」。exit 5 保留給不可
> 補救的 integrity 條件（見下方累積制警告框）。
>
> **exit 5 裡有兩條是可補救的例外**，訊息本身也會這樣寫，別誤當成要換 run-id：
> - **MANUAL safe mode**（例如 §10.4 三連虧）：查清原因後 `safe-mode --release
>   --run-id <id>`，重跑 `validate` 即可。這是 latch，不是永久判定。
> - **daily-loss 仍 active**：§10.3 的 cap 由**乾淨的 reconciliation** 在跨過 UTC
>   日之後釋放，而那只在 daemon 跑的時候前進。停掉 daemon 之後才 validate 會永遠
>   看到它——重開 `live --run-id <id> --loop` 跑到一次 reconcile tick 再驗。
>
> **`kill_switch_fired_count` > 0 則是真的不可補救**：deadline 過期代表交易所已把
> 整個錢包的單（含 SL/TP）撤光，那段時間的倉位確實裸奔過，沒有任何後續狀態能讓它
> 變成沒發生。查清原因（跨過 deadline 的 API 中斷，或主機時鐘往前跳），然後**換新
> run-id** 重新累積驗收 cycles（**換 run-id 一併把 §20.2 smoke gate 歸零**：18 項全回 `not_yet_run`，要整套 live-smoke 重跑，含 test 16／17 的操作者前置）。

§20.3 驗收門檻（testnet_live）：`cycle_count ≥ 30`、`live_order_count ≥ 30`、
`exchange_fill_dedupe_error_count / orphan_exchange_order_count /
duplicate_fill_apply_count / local_exchange_position_mismatch_count /
account_replay_mismatch_count / unprotected_position_seconds` 全為 0
（`orphan_exchange_order_count` 數的是**案件列**不是訂單：交易所 orderStatus 一好一壞
抖動時，同一張單每抖一次就多一列，而同一張單還會因為不同的故障形狀落在不同的事實鍵上。
判準不受影響——一列就不過——但**要去交易所找幾張單，看旁邊那行
`orphan_exchange_order_distinct_count`**，它把事實鍵收斂回 cloid，數的是**相異訂單**）、
`kill_switch_refresh_success_rate ≥ 99%`（**樣本數 < 100 時不判定**，改記 exit 4 的
shortfall——30s 一次的節奏下約 50 分鐘就滿，遠早於 30 cycles，所以正常驗收 run 不會
卡在這裡；設這道下限是因為短 run 的覆蓋時間太短，一次 30s 中斷就吃掉整段可用率）。
**這道可用率以「時間」計，而且「沉默」也算 outage**：只要事件之間的空窗超過**當下實際
生效的** `schedule_cancel_seconds`，就代表那段期間沒有任何東西續約排程，交易所已在中途
把整個錢包的單（含 SL/TP）撤光——不管當時進程是卡住、被限流還是根本死了——一律以
**全長**計入 outage。
「當下實際生效」取自 `kill_switch_armed`／`kill_switch_refreshed` 兩種列當中
**有寫 `deadline=...s` 的**——兩種都算、不是只有 arm，但也只認這兩種（sweep 的
completed 列 detail 是整包 JSON，撞到字樣也不採信）；daemon 的 refresh 列不帶 detail、
不改變當下生效值；genesis
（`--create` 當時的 `live:` 區塊）只提供還沒 arm 之前的起始值。原因是 `config_json` 只在
`--create` 寫一次，而 resume 時改動 `live.kill_switch` 只印 WARNING 不擋——只讀 genesis
的話，一個 genesis 600、實際以 120 arm 的 run，會把每一段真實的 121–600s 失聯都算成
「有保護」，於是真的裸奔過的 run 報 `live_ready`。
**兩種都算是必要的**：`live-smoke` 以 `max(config, 120s)` 續約，所以在
`schedule_cancel_seconds` 設得比 120 小的 run 上，它的 floored 列——每個測試前的
refresh，加上 test 14 自己的 armed/refreshed 兩列——是「當下真的有更長保護」
的唯一證據（recovery 建的 manager 其 arm 列寫的是未 floor 的 config 值）；只聽 arm
會把 smoke 期間每一段 41–120s 的間隔憑空判成 outage。
另外，**`live-smoke` 期間寫進事件流的每一列都帶 `writer=live-smoke` 標記**——不只 suite
自己打的那幾筆（pre-flight refresh、test 14 的 arm/refresh/clear、離場 disarm、失敗的
refresh），**也包含 suite 為 pre-flight recovery 與 test 15-17 建的那個真 KillSwitchManager
寫的列**。後者才是關鍵：真實 testnet 一個 test 是數分鐘，refresh interval 30s，那個 manager
的 `tick()` 會到期並寫出 refresh 列；漏掉它們的話，這個排除規則在真實 run 上等於沒有生效
（離線測試的時鐘不前進，所以看不出來）。它們**計入 outage 秒數與當下 deadline**（是真的保護），
但**不計入 §20.3 的 100 筆樣本下限**（排除只作用在**樣本下限**與 `clean_shutdown` 的
daemon 判準這兩處；`kill_switch_fired_count`／`disarm_failed_count` **不分寫入者**，
所以 smoke 階段真的燒掉一次 dead man's switch 一樣會讓這個 run-id 報廢——見 §5 該列）——樣本下限問的是「這個 run 有沒有把 switch 操練到
足以判定可用率」，而連跑六輪 smoke 就能湊到 114 筆、100%、daemon 卻一秒都沒跑過
（每輪至少 19 筆＝18 個 test 各一次 pre-test refresh ＋ test 14 自己那次；pre-flight
recovery 寫的是 `kill_switch_armed`，本來就不計入樣本下限。說「至少」是因為真 testnet
上 pre-flight 與 test 15-17 建的那個真 `KillSwitchManager` 還會隨時間再寫幾筆——見下
一段——所以 114 是下限，這只讓「daemon 沒跑過也早就湊滿 100 筆」的論證更保守）。
（suite 自己打的那幾筆走的是 signed client 而不是 `KillSwitchManager`，所以沒有別人會
補那些列。）沒有這些列的話，整個 smoke 期間、以及跑完 smoke
到啟動 `live --loop` 之間那段由操作者決定長度的空窗，都會被算成 outage——一個完全乾淨的
120 小時 run 只要這段空窗超過約 73 分鐘就會被判 exit 5，而 §3 明明告訴你 smoke 的通過紀錄
不會過期。唯一豁免是空窗開頭為 **`kill_switch_disarmed`**：那一列寫在
`clear_scheduled_cancel()` 成功之後，是唯一能證明錢包層級 trigger 已被清掉的證據，
代表保護是**刻意**釋放的，所以計畫性的停機重啟不會被罰，而且那段時間**分子分母都不計**
（只扣分子會讓「停機一小時再開」變成稀釋工具，把不到 99% 的 run 洗成通過）。
注意**不是** `shutdown_cancel_orders_started`／`_completed`：那兩列夾住的是撤單 sweep，
跑在清 trigger **之前**，當下 switch 仍然是 armed 的。
**因此：被 SIGKILL／OOM 砍掉再重啟的 run，那段停機會直接反映在可用率上**，這是刻意的
——那段時間倉位確實裸奔過。另外兩個旗標都只**告知、不擋 gate**：`kill_switch_clean_shutdown: no`（**daemon** 的最後
一列不是乾淨收尾＝被砍掉而不是停下來；判準只看**未帶 `writer=live-smoke` 標記**的列，
所以乾淨停機之後再跑一次 live-smoke 不會把它翻成 no，而完全沒有 daemon 列的 run
印 `n/a (no daemon rows)`）與
`kill_switch_ended_in_outage: yes`（**這個 run** 結束時還有一段沒關起來的 outage——通常是
最後一列為失敗的 refresh，但**不只**：沉默超過當下 deadline 也會開一段，而 `fired`、
`disarm_failed`、撤單 sweep 那幾列都不會把它關起來，所以**表裡可能一列
`kill_switch_refresh_failed` 都沒有**）。兩者都**分不出「run 被砍掉」
與「run 還在跑、validate 剛好在這一刻讀」**——in-flight 的 run 其 log 本來就結束在
validate 讀到的位置。曾經讓後者記 shortfall 擋 gate，結果是：健康的 daemon 在 15 秒前抖了
一下就 exit 4，而**同一個 run 過 30 秒再驗**（實際曝險**更多**）反而通過——判決隨下指令的
時機漂移，正是這個量測要否定的東西。所以它們是給人看的訊號：看到
`ended_in_outage: yes` 就等 switch 恢復、log 有了收尾證據再驗一次；**可用率此時是下界**。
另外注意這兩個旗標的**範圍刻意不同**：`clean_shutdown` 只看 daemon 的列，
`ended_in_outage` 看整個 run 的列。它們問的不是同一件事——「daemon 是不是被砍掉的」
是關於 daemon 的事實，後來誰再寫列都改不了；而 `ended_in_outage` 問的是「可用率是不是
下界」，也就是尾端那段 outage 有沒有東西把它關起來，而 `kill_switch_armed`／`_refreshed`／
`_disarmed` 這三種列**不管是誰寫的**（包括後來 live-smoke 寫的）都關得起來、那段秒數也會
照實算進 outage。判準是**最後一列 `armed`／`refreshed`／`disarmed` 之後有沒有再開一段**，
不是「這次 smoke 有沒有 arm 過」——只要選到的測試裡有會下單的項目，smoke 的 pre-flight
recovery 就會先 arm；就算一項下單測試都沒挑，**test 14–17 自己也會 arm**（14 直接打、
15–17 各跑一次真 §19.1 recovery）。真正一列都不寫的只有 `--dry-run`，以及 `--only` 同時
避開下單測試與 14–17 的情況。所以「有 arm」幾乎恆成立、分不出東西。所以：daemon 死在 outage 裡、之後跑一次乾淨的 live-smoke →
`clean_shutdown: no` ＋ `ended_in_outage: no`（那次 arm 把 daemon 的 outage 關掉了）；
但同一次 smoke 若 arm 之後 refresh 失敗、離場 disarm 也失敗，那是**第二段** outage 且
沒人關它 → `ended_in_outage: yes`、`outage_episodes: 2`。裸奔秒數不管哪種情形都在
`kill_switch_outage_seconds` 裡。
`kill_switch_fired_count = 0`（dead man's switch 從未真的燒過）、run **不在 MANUAL
safe mode**、四項 smoke 布林（`restart_reconciliation_passed`
——注意這一項**沒有** `_test_` 中綴——以及 `emergency_close_test_passed`／
`startup_with_existing_position_test_passed`／`startup_with_stale_open_order_test_passed`，
來自 smoke 15/16/17/18）皆 true。

> **`cycle_count` 只認 `completed`**：模型輸出解不開的 cycle（`invalid_output`）
> **不計入** ≥30，另以 `invalid_output_count` 與一則 warning 呈現。paper 驗收器計
> 它（那裡量的是「排程有沒有在跑」），live 驗收器不計——這裡問的是「bot 能不能
> 交易」，而 §21.4 沒有 order count 可以背書；不然 30 個連續解不開、一單沒下的
> cycle 也會報 `live_ready`（paper-BTC 換模後就出現過 6/6 `invalid_output`）。
> 另注意 `phase2-target-v3` 起 prompt 的 schema 區塊是型別非法佔位符，模型整段照抄
> 會落在 `invalid_output`（先前照抄是**合法**的 `maintain_current`，被算成 cycle）。
> 這裡的 `invalid_output` 指 `decision_attempts.status`——每一種解不開都是它。同一個
> 字在 `ai_outputs.risk_reason` 那一欄另有窄義，照抄在那欄記的是
> `invalid_decision_mode`。RUNBOOK §5 兩種意思都用到:開頭講 ≥30 那道門的那句是
> `decision_attempts.status`（所以照抄的 cycle 也計入），後面做成因鑑別的那句才是
> `risk_reason`。
> 所以相對 v2 會看到 **`invalid_output_count` 上升、`cycle_count` 同步下降**——這是
> 既有照抄被顯性化，不是新缺陷，但 ≥30 那道門因此需要更多 cycle 才跨得過。要判斷
> 修法有沒有效看的是提案率，不是這兩個計數。

> **unprotected 秒數只算「沒有一張足以覆蓋的 SL」的時段**：§17.4 是
> modify-before-cancel，所以 wire gate 擋掉一次 **modify** 時舊的那張 SL 可能還掛在
> 交易所。判準是**覆蓋**不是存在（與 §12.3 `_has_valid_sl` 判準一致；那張 covering
> 單在記到事件上之前會**向交易所 orderStatus 正面確認**，讀不到一律不記——會走到
> blocked 這條分支就代表 kill switch 正下著，而過期的 deadline 早已讓交易所撤光整個
> 錢包、只留本地 row 原封不動，所以「只讀本地那一張」不是更嚴，是換了一個此刻剛好
> 失效的來源）：平倉方向
> 正確、且 `qty ≥ 目前倉位`，才算還有保護、才**不開窗**（事件上會帶那張單的
> order_id）。最常見的 blocked 其實是 **resize**——後面的切片成交了、舊 SL 只蓋得住
> 一部分——那**照樣開窗**，因為有一部分倉位真的沒有停損。真的一張都沒有的 blocked、
> 以及 `stop_loss_repair_exhausted`，同樣開窗、同樣是 exit 5。窗**只在真的結束時**
關（SL 回到書上、倉位平掉、或 protection 的失敗線降下）——§17.2 的 emergency close
只是「送出平倉單」，不算結束。另外**窗數本身也是 gate**：量到 0 秒但有窗（onset 與
close 落在同一個時鐘刻度）照樣 exit 5，不會讀成「從來沒有無護倉」。

報告中的 `warning:` 行不影響 exit，但寫結論前要看過。testnet 報告會警告：run 中
發生過 emergency close（§21.4「不得因 bot bug emergency close」無法機器判定，需
人工看 stop_loss_repair 證據）、**kill-switch disarm 失敗**（`kill_switch_disarm_failed_count`
——收工沒能清掉錢包層級的 scheduleCancel，兩個 profile 都會報）、daily-loss 破線紀錄、
還開著的人工 §12.3 case
（mainnet 是硬 gate，testnet 先提醒你在準備 mainnet 前解掉）、以及上面說的
`invalid_output` cycle 數。

> **integrity 計數是累積制**：dedupe error／orphan／position mismatch 等 failure
> 計數算的是「這個 run 歷史上記錄過的 case 數」，`--stamp-case` 只了結 §21.4 的
> unresolved gate，**不會**把計數歸零——一旦記錄過，該 run 的 `validate` 永遠
> exit 5。這是刻意的政策（帳本潔癖：驗收 run 必須全程乾淨）；中途出過 case 就換
> 新 run-id 重新累積 30 cycles（**換 run-id 一併把 §20.2 smoke gate 歸零**：18 項全回 `not_yet_run`，要整套 live-smoke 重跑，含 test 16／17 的操作者前置）。

---

## 6. 監控與 safe mode 處置

日常監控同 paper（[RUNBOOK §5](./RUNBOOK.md)）再加 live 專屬：

| 看什麼 | 在哪裡 | 正常 | 異常時 |
|---|---|---|---|
| kill switch 刷新 | stderr log／`kill_switch_events` | 每 30s 一次 `kill_switch_refreshed` | outage 有**兩種**開頭：一列 `kill_switch_refresh_failed`（同一次中斷只寫一列，不論重試幾次），**或是沉默超過當下 deadline**——進程被砍／卡死時它連失敗都寫不出來，所以**表裡可能一列 `refresh_failed` 都沒有卻仍記到 outage**（這種的長度從**前一列**算起，不是從 deadline 到期那一刻算起）。長度算到下一列 `kill_switch_armed`／`_refreshed`／`_disarmed` 為止（`fired`、`disarm_failed`、撤單 sweep 那幾列**不**結束 outage）。進 safe mode、擋新單，查網路。`validate` 的 refresh 可用率就是用這個時間長度算的，不是用列數 |
| reconciliation | `exchange_reconciliation_events` | 無 open case | 有 mismatch → safe mode（見下）。**`fill_malformed` 且 `exchange_value` 以 `envelope-` 開頭＝串流層故障**（訂閱錯錢包／channel schema 漂移），不是單筆 fill 壞掉——先修接線，別急著 stamp（見下方 envelope 專節） |
| protection | `protection_order_events` | 有部位時 SL 在書上 | `stop_loss_repair_exhausted` → unprotected，可能 emergency close。`*_repair_failed` 的 detail 若帶 `orderStatus recovery answered unusably:`，代表修復梯是被**交易所答非所問**耗盡的（誤路由），不是網路中斷——查的方向完全不同。**連續**答非所問還會另寫一列 `identity_fault_latched` 並把 run 升上 manual safe mode（見下方 `venue_identity_fault`） |
| 中途健檢 | `validate --run-id live-BTC --db live_trading.db` | exit 4（一致、未滿） | exit 5 → 停下來調查 |

**Safe mode**（§13）：進入來源有 WS 斷線 > 5min、kill switch 刷新失敗、
reconciliation mismatch、非 bot-owned 單、daily/consecutive loss、
`venue_identity_fault`（見下）。分兩型：

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

### `venue_identity_fault`（manual）

**意思**：交易所連續多次對 orderStatus 給出「讀不出來是我們這張單」的答案——回的是**別人**
的 cloid，或是這個 build 認不得的形狀。門檻與當下計數寫在 `protection_order_events`
那列 `identity_fault_latched` 的 `detail` 裡。**計數走的是整個 process 共用的一個 `VenueIdentityMonitor`，但計數本身每個 cloid 各自一條**（issue #80）：
protection 的兩個探測點、reconciliation 每張單的 orderStatus 查詢、以及 §18.2 shutdown 的
disarm 交叉檢查，走的是同一個 `VenueIdentityMonitor`——所以故障在哪個消費者身上被問到都算同一串，
`detail` 會寫出是哪一個站點跨過門檻的（計數**每個 cloid 一條**——venue 只誤路由其中一張單、
其他答得好時，那張單自己的連續次數照樣累積到門檻）。升級成 manual 的時點有三個：engine 每 tick 在
§17 sync 之後、`reconcile_and_apply` 每一輪對帳之後、CLI 在 shutdown sweep 之後——三處都會把升級
寫進 SQLite（`enter` 冪等，同一 episode 不重複寫列），shutdown 那次只是 process 結束前的最後一次
機會。下一次 `live --run-id` 開機會把 manual 狀態 hydrate 回來：照樣 arm、照樣對帳，但 verdict
不會過、不開新 cycle，直到 §13.6 人工解除——而不是每次 shutdown 都只留一行 log、每次都擋住 disarm。

站點名稱是**封閉詞彙**（`live/venue_identity.py`）：`ProbeSite` 是探測站點，寫在 `identity_fault_latched`
的 `latest from the …` 與 safe-mode detail 的 `latched at the …`；`EscalationHolder` 是升級方，寫在
`escalated by …`。`{role}`／`{trigger}` 是動態欄，分別只能是 `PROTECTIVE_ORDER_ROLES`／
`RECONCILIATION_TRIGGERS` 的成員。本表與 enum 同源（`test_venue_identity` 釘住集合相等，多一員或少一列都會紅）：

| 家族 | 字樣 |
|---|---|
| probe | `protection {role} no-op guard` |
| probe | `protection {role} recovery probe` |
| probe | `reconcile orphan-order tiebreaker` |
| probe | `reconcile absent-order settle` |
| probe | `kill-switch disarm cross-check` |
| holder | `§17 protection sync` |
| holder | `§12 reconciliation, {trigger}` |
| holder | `§18.2 shutdown disarm cross-check` |

**為什麼是 manual**：這種故障不會自癒。會把一次身分查詢誤路由的 venue，下一次照樣誤路由，
所以 recoverable（下一輪乾淨對帳就自動解除）等於把 run 放回原本那個無限迴圈：
no-op 守衛會對一張**其實還掛在簿上**的停損無限重修，而失蹤 ack 的復原探測可以把一整條
修復梯燒成 §17.2 緊急平倉——平掉的是本來健康且有保護的倉位。

**期間仍然安全**：SL/TP 與緊急平倉屬 `PROTECTIVE_ORDER_ROLES`，對 manual safe mode 這條
gate 線是豁免的（`order_gate.py` 有 import-time 保證），所以 latch 期間保護照常運作、
照常修復；被擋住的只有**加曝險**的新單。

**怎麼查**：

1. 先看 `identity_fault_latched` 那列的 `detail`——它會指出最後一次是哪個站點（protection 的
   哪個 role、reconcile、還是 kill-switch disarm；字樣見上表）、哪個 cloid、以及交易所實際回了什麼。
   **整包回應**在 `payloads/<run_id>/orderStatus-<cloid>-*.json`——裡面有對方的 oid 與本文，
   `str(exc)` 只說得出兩個 cloid。存檔規則（每個 cloid 存幾份、為什麼有上限）以
   `live/venue_identity.py` 的 `_note_unreadable` docstring 為準，這裡不複述。
   同一個故障在其他表也會露面：`exchange_reconciliation_events` 的 `order_missing_on_exchange`／
   `orphan_exchange_order` case detail 與 shutdown 那列 `shutdown_cancel_orders_completed` 的
   `failures` 寫 `orderStatus answered unusably (venue identity fault): …`（純網路中斷寫的是
   `orderStatus failed: …`，兩者查的方向相反）；protection 自己的 `*_repair_failed` 列沿用上表的
   `orderStatus recovery answered unusably:` 字樣。
2. 這是**交易所或接線**的問題，不是策略問題：核對 `live.wallet_address` 與 agent key
   是否同一個錢包（同 `envelope-wrong-user` 的核對方向）、SDK 是否被別處覆寫、
   以及 Hyperliquid 是否改了 orderStatus 的回應格式（後者會讓這個 build 的解析全面失效，
   `live-smoke` 在 testnet 會先撞到）。
3. 確認交易所已能正確回答我們的 cloid 之後才解除；解除用上面的 `--release --reason`。

**自己會退掉的部分**：只要 **latch 住的那個 cloid** 再被讀得懂一次答案，latch 就會落下（別的
cloid 讀得懂不算——串是按 cloid 記的），之後若再度連續
故障會重新 latch 並留下**新的一列**（所以兩次發作在紀錄上分得開）。但 latch 落下**不會**
自動解除 safe mode——§13.6 規定 manual 只能人工解除。

**⚠️ envelope-* 的 case 不適用上面那個範例。** `fill_malformed` 有一類 case 的
`exchange_value` 是 **`envelope-` 開頭的固定字串**，它記的不是「某一筆 fill 壞掉」，
而是**整條 WS 串流層級的故障**。stamp 掉它等於把唯一的訊號消音，而且**不可逆**：
這類 case 刻意用固定 key（否則錯線期間每則訊息都會生一列、每列都要人工 stamp），
所以同一個 run 一旦 stamp 過，之後即使故障還在，也**不會再寫第二列**。

| `exchange_value` | 意思 | 先做什麼 |
|---|---|---|
| `envelope-wrong-user` | `userFills` **這條 WS 串流**送來的是別的錢包的成交，本 run 自己的成交不再從 socket 進來（沒有任何一筆髒資料被寫入）。帳本不會就此漂掉——每一輪對帳的 REST backfill 走的是不帶 `user` 的合成 envelope，不受這個檢查影響，仍照常補記自己的 fills；真正的風險是**落在 backfill 視窗之外**的成交，以及 case 擋住 verdict 期間 run 進 manual safe mode 不再開新曝險。 | **先停 run**，核對 `live.wallet_address` 與 agent key 是否同一個錢包、SDK 訂閱是否被別處覆寫。修好重啟後 backfill 會把視窗內漏掉的補齊。**確認訂閱正確之後**才 stamp。 |
| `envelope-no-fills-list` | 該 channel 的 envelope 不再帶 `fills` 陣列——交易所 schema 漂移。 | 讀證據檔（**完整保留了第一則訊息**，形狀就是線索），確認新格式後改 parser，再 stamp。 |

證據檔在 `payloads/<run_id>/fill_parse_error-envelope-*.json`。`envelope-wrong-user`
那支**只留表頭**（channel＋對方位址），刻意不留對方的成交明細——所以事後無法從磁碟
反推「那些 fills 其實是不是我們的」，這是換取「一個故障一列可 stamp 的 case」的代價。

（升級注意：本版之前這兩類故障是按訊息內容 digest 記的，key 長 `unparsed-<digest>`。
既有 run 若已有那種列，本版第一次再遇到同一故障會另外寫一列新 key 的——多 stamp 一次，
沒有證據遺失。）

**「一個未癒事實一列」的 case 不只 envelope 那兩種。** 交易所倉位與訂單那邊也有幾個 case 的
`exchange_value` 是**不帶量值的固定 key**，語義與上面一樣——一個未癒的事實只寫一列
（訂單那三種另有一層：一段被了結之後又復發時會另開一列，見下面第 2–4 點）：

| `exchange_value` | 事實 | 現在多大／現在怎樣，去哪裡看 |
|---|---|---|
| `<幣別>\|unknown_coin` | 錢包持有本 run 不交易的幣種（manual safe mode） | 首列的 `detail`（第一次看到時的大小）、每 pass 的 warning log、每 pass 的 `reconciliation_diff` |
| `<幣別>\|sl_missing` | 這個實倉沒有足量的 reduce-only SL 覆蓋 | 同左三處，另加每 pass 的 `position_snapshots.position_size` |
| `equity_out_of_tolerance` | equity 超出容差 | 首列的 `detail`、每 pass 的 warning log、每 pass 的 `reconciliation_diff`（`position_snapshots` 那欄是倉位大小，與 equity 無關） |
| `<cloid>\|read_failed` | 這張單（本地 live、交易所沒列）的 orderStatus 讀不到 | **目前這一段**首列的 `detail`（該段第一次的**完整**例外訊息；早先已了結的段各有自己的列）、每 pass 的 warning log、每 pass 的 `reconciliation_diff`（那份 detail **截到 `_DIFF_STRING_MAX_CHARS`（目前 300 字元）**，交易所的錯誤內文可能被切掉尾巴） |
| `<cloid>\|local_terminal_read_failed` | 這張單（本地終態、交易所仍列 open）的 orderStatus 讀不到 | 同上一列；讀得到之後自動了結，再讀不到就是新的一段、新的一列 |
| `<cloid>\|local_terminal` | 上一列那張單的 orderStatus **答了**，而答案與本地終態列衝突（reopen／unknownOid 矛盾） | 該列的 `detail`、每 pass 的 `reconciliation_diff`（**reopen 與 unknownOid 這兩支沒有自己的 warning log**，run log 裡只有那一輪 `reconciliation … UNCLEAN` 的 `cases=N` 計數；grep cloid 找不到不代表 sweep 沒在看它） |

（「每 pass」是指有寫出 snapshot 的那些 pass。snapshot 腿是 fail-soft 的：clearinghouse 讀
失敗時整輪不寫 snapshot 列，沒有本地 ledger 列、缺 `crossMaintenanceMarginUsed`／
`positionValue`、或寫入失敗時跳過並留 warning——那些輪就只剩 log。特別注意這對
`read_failed` 那一列最常發生：會打斷 orderStatus 的 API 故障通常也讀不到 clearinghouse，
整段故障期間可能一列 diff 都沒有。）

四件事要記住：

1. **量值不在 key 裡，所以列不會隨倉位變動增生**——反過來說，`--status` 那一行看不到
   現在多大，要去看上表右欄。
2. **同一個未了結的事實不會寫第二列**——它每輪重見仍是那一列。已了結的列則要看處置是誰
   蓋的（2026-08-21 起，issue #65）：**你自己 `--stamp-case` 蓋的是終局**，同鍵之後的重見
   一律不再寫列（你的答案不該被每一輪再問一次）；**機器蓋的「暫定」處置**（下一點）之後，
   同鍵的新事實**會**另開一列並回到清單上。所以人工 stamp 過的那型復發時，只會出現在
   **該輪的 pass 判決與 safe mode**（例如 `position_sl_missing` 進 recoverable safe mode）
   與 warning log。**不要用「open case 清單是空的」推論「現在沒有這個問題」。**
3. **哪些是機器蓋的「暫定」處置**（就這四類、六個字串，其餘機器處置與你寫的字都是終局）：
   `settled_never_sent`、`settled_filled`／`settled_canceled`／`settled_rejected`、
   `resolved_read_succeeded`、`local_row_reopened`。終局的機器處置有三個：
   `resolved_fill_booked` 與 `local_row_backfilled`（fill 已入帳、本地 order 列已補寫，
   都不可能再不成立），以及 `backfilled`（它的列本來就不帶 key、從來不進去重）。
   暫定的意思是**「本輪把這件事了結了，但同一件事還可能再發生」**——再發生時**會另開一列、
   回到清單上**，例如該單被 §8.3 rule-5 重送或被 reopen 而復活之後又缺席，unknownOid 對上
   rule-10 證據那種（這一族裡最嚴重的一種）就會落在**裸 cloid** 那一列。
   （2026-08-21 之前這四個處置一旦蓋章就把 key 永久關閉，上述復發只進 pass 判決，既不進
   清單也不進 §21.4 計數——issue #65／#66。判「現在有沒有問題」仍以該輪的 pass 判決與
   safe mode 為準：清單是 backlog，不是即時健康指標。）
   **`--action` 不要用上面這九個字串**（六個暫定＋三個終局）：`--stamp-case` 會具名拒絕，
   改用你自己的話寫即可。兩半被拒的理由不同，錯誤訊息會說明是哪一種——**暫定那六個**是因為
   去重看的是字串本身、不看是誰寫的，拿它們當你的處置會把 key 重新打開；**終局那三個**
   是因為 `action_taken` 沒有「誰寫的」欄位，字串就是唯一的來源證據，人的證言不該與
   daemon 自己蓋的章長得一模一樣。
4. **兩個「讀不到」的 key，自動了結的條件不一樣**（issue #66）：
   - `<cloid>|local_terminal_read_failed`（本地終態、交易所仍列 open，那一輪 orderStatus
     讀失敗）——**之後任何一輪讀得到就蓋上 `resolved_read_succeeded`**，不管答案是終態、
     live 還是 unknownOid：這一列的事實就只是「問不到」。
   - `<cloid>|read_failed`（本地 live、交易所沒列，那一輪讀失敗）——**只有在本輪順便把那張
     單了結掉時才蓋**。所以有一種「該關卻沒關」：讀取失敗之後那張單只是**還掛在交易所上**
     （open_orders 慢了一拍），本 sweep 不蓋處置；若它接著被 §19.3 撤單、kill switch 或
     protection manager 收掉，就再也沒有哪一輪會了結它，那一列會**整個 run 停在未解**、把
     §21.4 計數壓在非零，而每一輪都報乾淨。看到這種孤兒列，人工 `--stamp-case` 掉即可。

   兩者的差別**不是**哪邊比較會生列——生一列要先讀失敗、讓 key 重開的那次蓋章要先讀成功，
   所以兩邊的**上限**都是「交易所讀取一好一壞抖動一次、生一列」，都不會每輪一列。實際上
   `read_failed` 連這個上限都到不了：它只在了結該單時才蓋章，所以純抖動一列都不會多生，要有
   一次重送或 reopen 才會。真正的差別是**還有沒有別的東西會關掉那一列**：`read_failed` 那張
   單留在 sweep 的游標裡，之後某一輪本來就會把它了結並順手蓋章，所以不必急（代價就是上面那種
   被別人收掉的孤兒列）；`local_terminal_read_failed` 最常見的那支答案（orderStatus 說終態）
   **不產生 case**，不在讀得到的當下蓋，就沒有任何後續會替它蓋——它抖動生的列可以接受，因為
   同一把 key 底下**只有最新那一列會是未了結的**（每一段都被結束它的那次讀取蓋掉），
   `--status` 與 §21.4 看到的仍是一個活的故障。

（升級注意：本版新增了 `<cloid>|local_terminal_read_failed`，把「讀不到」從
`<cloid>|local_terminal` 拆出來（先前這兩個事實共用一把 key，先到的那個佔住它）；更早之前
還改過 `ETH:2.5`→`ETH|unknown_coin`、`0.001`→`BTC|sl_missing`、裸 cloid→`<cloid>|read_failed`。
跨版沿用同一個 `run_id` resume 的 run，同一個未癒事實會再多一列新 key 的列（去重是精確比對，
舊列擋不住新 key）；舊列若還沒 stamp，就是兩列都要 stamp。沒有證據遺失。）

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

> **進 mainnet 前，先到交易所把該幣的槓桿設成 `cross 1x`。** 系統裡唯一會寫入
> 交易所端 sizing regime 的地方是 smoke test 2（`update_leverage`），而 smoke
> **只在 testnet 跑**（§21.3）——`live --create` 與 `live --loop` 都不會設定它，
> 啟動時也不比對。所以 mainnet 主錢包的 regime 就停在人工留下的值；若那是
> isolated 或 >1x，本地的保證金／清算價／§17 停損帶寬全部會以 `cross 1x` 為前提
> 計算而失準。這個錢包在 §1.3 本來就是你自己入金的，很可能也手動下過單。

### 7.3 跑法

用新的 run-id（例如 `mainnet-BTC`）避免與 testnet run 混帳：

```bash
export HYPERLIQUID_AGENT_KEY_MAINNET=0x...
python -m contrib.hyperliquid_perp live --config contrib/hyperliquid_perp/configs/hyperliquid.local.yaml \
  --run-id mainnet-BTC --db live_trading.db --create        # 建 + recovery
# mainnet_tiny 依賴 testnet 已過的 smoke（§21.3），--loop 不再擋同一 run 的 smoke gate
python -m contrib.hyperliquid_perp live --config contrib/hyperliquid_perp/configs/hyperliquid.local.yaml \
  --run-id mainnet-BTC --db live_trading.db --loop           # 跑 cycles
```

跑滿 ≥ 30 cycles 後：

```bash
python -m contrib.hyperliquid_perp validate --run-id mainnet-BTC --db live_trading.db
```

§21.4 驗收（mainnet_tiny）：`mainnet_tiny_cycles ≥ 30`、無 unprotected 部位、
無 orphan bot-owned 單、無 duplicate fill、無未解 reconciliation mismatch、
daily loss cap 未破、`kill_switch_refresh_success_rate ≥ 99%`（比 §21.4 條文嚴——
真錢 run 的 dead man's switch 必須持續在動，2026-07-27 拍板；同 §20.3，樣本數 < 100
時不判定而記 exit 4 的 shortfall）、`kill_switch_fired_count = 0`、run **不在 MANUAL
safe mode**（後兩項兩個 profile 共用同一條 gate——「機制在 testnet 證明過」不等於
「這個 run 上沒失效」，而一個等人工確認才能下單的 run 更不可能是 live-ready；前者
不可補救要換 run-id，後者 `safe-mode --release` 後重驗即可，見上方 exit 表）、
（人工確認）無因
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
| `effective_notional_cap ... below the exchange minimum`（exit 1） | 入金遠低於交易所最小單（約 equity < 16.7 USDC）；見 §1.3。想吃滿 100 USDC 名目上限另需 ≥ ~167 USDC。 |
| `--loop` 報 §20.2 smoke gate 未過（exit 4） | 先跑 `live-smoke`（§3），`--gate-status` 確認 yes 再 `--loop`。 |
| `live-smoke` 報 run lock 被持有（exit 1） | 同一 run 的 `live --loop`／`paper` 還在跑；先停掉（或等 lease 過期）再跑 smoke。 |
| `live-smoke`／`live` 報同 db 另一個 run 正在跑、提到 ACCOUNT-wide（exit 1） | 同網路的姊妹 run 還持著新鮮 lease；kill switch／`updateLeverage`／§19.3 掃單是整帳戶層級，會扒掉它的護欄。**停掉它，或等 lease 過期**。不同網路的 run、以及 paper run，不會觸發**這一條**（但會觸發下一條）。⚠️ 換 `--db` 不是解法——危害綁錢包不綁 store，換 store 只會讓檢查瞎掉。 |
| `this build needs to migrate the store, but run 'X' in it is being driven by pid N`（exit 1） | code 升級後帶了新 migration，而同一個 db 檔裡**任何**其他 run（不分網路、含 paper run）還有活 lease；migration 改的是整個檔，會在那個 daemon 腳底下換 schema。**停掉它（或等 lease 過期）再跑**，或先用它那個版本的 code。store 已是最新版時不會出現這條。 |
| `store schema is vN; this build needs vM`（exit 1） | code 升級後帶了新 migration，而 `validate`／`export`／`--gate-status`／`live-smoke --dry-run` 是純報表指令、**刻意不自動 migrate**（免得升級一個 daemon 正在用的 store）。先停掉 daemon，再跑擁有這個 store 的指令（paper store 用 `paper --run-id ...`，live store 用 `live --run-id ...`）讓它 migrate，然後重跑——`paper`／`live`／真跑的 `live-smoke`（不帶 `--dry-run`）都是確認沒人擁有這個 run（且同檔沒有其他活 lease）之後才 migrate，見 §3.1。最輕的升級指令是 `safe-mode --status`（純診斷、不碰交易所、不 arm 錢包），但它**不做**同檔 sibling 檢查，跑之前自己確認同一個 db 檔沒有別的 run 在跑。 |
| `store schema is vN but this build only knows vM`（拒絕開啟） | 這個 store 被**更新版**的 code migrate 過，現在用舊 binary 開它會用不認得的欄位寫穿它。跑回新版 code，或還原升級前的備份。 |
| `live-smoke` 報 pre-flight recovery 沒過（exit 4） | run 狀態不乾淨；`safe-mode --status` 查 open case、照 §6 處置後重跑。**注意 config 錯誤不會走這條**：kill-switch timing 違規在 `live-smoke` 與 `live` 一樣是啟動前的具名 **exit 1**（訊息直接指名 `schedule_cancel_seconds`／`refresh_interval_seconds`／`network_timeout_s` 三個 knob），所以 supervisor 依 1／4 分流仍然正確。 |
| `OPENROUTER_API_KEY is not set`（exit 1，只有 `--loop`） | `--loop` 要跑 4h AI cycle；依 §1.4 設好 key。不加 `--loop` 的 `live` 不需要 key。 |
| `live.allow_real_orders is false` | live-smoke／--loop 要真下單；設 `allow_real_orders: true` 並備妥 agent key，或 live-smoke 用 `--dry-run`。 |
| `validate` exit 5、replay unverifiable | store 帳本對不上；先查（別盲目重啟），必要時 `safe-mode --status`。 |
| run 反覆進 manual safe mode | 查 `safe-mode --status` 的 open cases；換 coin／改 run 定義是硬錯誤，用新 run-id。 |
| `answered with cloid ...` ／ `refusing to book another order's ack` ／ `carries coin/interval ... response does not match the request` ／ `userFills envelope carries user ...` | **身分回聲不符**：交易所（或中間的 proxy）拿別的單／別的商品／別的錢包的資料回答我們的請求，也可能是 client 指向了錯的錢包。全部 **fail-closed**——沒有任何一筆被記帳。訂單側走 §8.3 同 cloid 的 orderStatus 恢復（attempt 記 `failed`＝結果未知，不會換新 cloid 重送）；K 線／funding 側該 tick 的 market read 中止、下一根 4h 重來；fills 側留證據後 drain 繼續（見 §6 的 envelope 專節）。**缺少**回聲欄位和不符一樣擋——這是刻意的，venue 格式漂移應該由 testnet live-smoke 先撞到。 |

更多規格細節見 [phase3-spec](./phase3-spec.md)。
