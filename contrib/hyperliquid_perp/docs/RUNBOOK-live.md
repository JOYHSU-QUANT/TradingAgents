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

```bash
# 首次：--create 建 run（genesis＝交易所快照）並跑一次 §19.1 recovery（arm kill switch、
# reconcile、掃 stale bot-owned 單），印判定後退出（不進迴圈）。帳戶非空要 --adopt-positions。
python -m contrib.hyperliquid_perp live \
  --config configs/hyperliquid.local.yaml \
  --run-id live-BTC --db live_trading.db --create
```

exit 0＝recovery 判定通過（可進 cycles）；exit 4＝執行了但判定 unclean（run 進了
safe mode，見 §6）；exit 1＝硬失敗（config／arming／建立）。

---

## 3. Smoke tests（§20.2）——進 cycles 的硬 gate

testnet_live **必須先全過 18 項 smoke test 才允許 `--loop` 進 cycles**（同一個
run-id）。smoke 對 testnet 真連線、真下小單（每筆約 11 USDC 名目、far-from-market
或 reduce-only，跑完自清）。

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

- **test 16（startup with existing position）**：先在 testnet 用主錢包開一個小倉，
  再跑 smoke——recovery 須乾淨 reconcile 這個既有倉。
- **test 17（startup with stale bot-owned order）**：先讓 run 留一張 bot-owned 掛單
  （例如上一輪 smoke 的殘留），再跑 smoke——recovery 須把它掃掉。
- **test 15（restart reconciliation）**：乾淨重跑 recovery 即可。

不方便一次備齊時，用 `--only` 分項跑（見下）。

> **這兩項證明的是什麼**：test 16/17 只斷言 recovery 判定 `passed`，**不會**獨立驗證
> 前置情境真的存在——若沒照上面備妥（或前一次 recovery 已把狀態清乾淨），recovery 仍
> 會判乾淨、這兩項照樣記 passed。因此 §20.3 的
> `startup_with_existing_position_test_passed` /
> `startup_with_stale_open_order_test_passed` 證明的是「**在你備妥的前置下** recovery
> 乾淨」，而非「情境已被自動偵測」——請確實照上面備置後再跑。
>
> **kill switch**：restart 系列的 recovery 會 arm dead man's switch；整套 suite 跑完
> 會**自動 disarm**（清掉 scheduleCancel），不會在錢包上留 armed 狀態。下次 `live
> --loop` 會重新 arm 並持續 refresh。

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

每項結果落 `live_smoke_tests`（append-only：修好再跑會覆蓋判定、保留歷史）。
失敗的項修好後重跑該項即可。

### 3.4 確認 gate（不下單、純讀 DB）

```bash
python -m contrib.hyperliquid_perp live-smoke \
  --run-id live-BTC --db live_trading.db --gate-status
# smoke_gate_passed: yes  → 可進 cycles（exit 0）
# smoke_gate_passed: no   → 印出 not_yet_run / failed 清單（exit 4）
```

| `live-smoke` exit | 意義 | 下一步 |
|---|---|---|
| `0` | **全 18 項** gate 開（每項最新真跑結果都 passed） | 可進 testnet_live cycles |
| `4` | 跑了（或讀了）但 gate 未滿足 | 看 not_yet_run／failed，補跑或修 |
| `1` | config／env／網路具名錯誤 | 依訊息修 |

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

- 若 smoke gate 未過，`--loop` 會具名 exit 1（`testnet_live cycles are gated on the
  §20.2 smoke suite ...`）——先回 §3。
- 迴圈每 ~10s tick（在 30s kill-switch 預算內）：排空 WS queue → 刷 kill switch →
  reconciliation → SL/TP protection → 到期切片；4h AI decision 在背景 thread。
- Ctrl-C／SIGTERM 安全停止並跑 §18.2 shutdown sweep。
- **長駐建議**同 paper（[RUNBOOK §3](./RUNBOOK.md)）：掛在會自動重啟的監管下，
  working directory 設 repo 根目錄。注意 live 的無人看管空窗風險比 paper 高——真錢／
  真倉。

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
| `4` | 一致但未到 gate（cycles/orders 未滿、smoke 未跑） | 繼續跑／補 smoke |
| `5` | integrity failure（dedupe error、orphan、position/replay mismatch、unprotected 秒數 > 0、refresh rate < 99%、smoke 失敗；mainnet_tiny 另含未解 reconciliation／daily-loss 破線） | 先調查再相信結果 |

§20.3 驗收門檻（testnet_live）：`cycle_count ≥ 30`、`live_order_count ≥ 30`、
`exchange_fill_dedupe_error_count / orphan_exchange_order_count /
duplicate_fill_apply_count / local_exchange_position_mismatch_count /
account_replay_mismatch_count / unprotected_position_seconds` 全為 0、
`kill_switch_refresh_success_rate ≥ 99%`、四項 `*_test_passed`（restart / emergency
close / existing position / stale order，來自 smoke 15/16/17/18）皆 true。

報告中的 `warning:` 行（例如 run 中發生過 emergency close——§21.4「不得因 bot bug
emergency close」無法機器判定，需人工看 stop_loss_repair 證據）不影響 exit，但寫
結論前要看過。

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
daily loss cap 未破、（人工確認）無因 bot bug 的 emergency close、手動 shutdown/restart
測過。

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
| `--loop` 報 §20.2 smoke gate 未過 | 先跑 `live-smoke`（§3），`--gate-status` 確認 yes 再 `--loop`。 |
| `live.allow_real_orders is false` | live-smoke／--loop 要真下單；設 `allow_real_orders: true` 並備妥 agent key，或 live-smoke 用 `--dry-run`。 |
| `validate` exit 5、replay unverifiable | store 帳本對不上；先查（別盲目重啟），必要時 `safe-mode --status`。 |
| run 反覆進 manual safe mode | 查 `safe-mode --status` 的 open cases；換 coin／改 run 定義是硬錯誤，用新 run-id。 |

更多規格細節見 [phase3-spec](./phase3-spec.md)。
