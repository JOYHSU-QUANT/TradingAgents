# carry

協調者不下單。

它不簽任何交易、不寫 `contrib/hyperliquid_perp/` 的 store、也不寫 `contrib/uniswap_v3/` 的 store。它只做一件事：
讀 Hyperliquid 的 funding 歷史，套**一條規則**，寫**一份交接檔**，說兩條腿在下一個 UTC 日線邊界該持有什麼；
兩邊的引擎各自讀那份檔、在各自的閘門下決定要不要動。上面那句話就是這個套件的 scope 判準：任何需要把它放寬的
改動都是 scope creep，不是更大的功能。

完整設計在 local-only 的 `.claude/carry-sleeve-plan-2026-10-07.md`；七題拍板在 memory `carry-sleeve-plan-2026-10-07`。

## 策略一句話

空 perp、多現貨，只做 funding 為正的一邊（現貨沒有融券，funding 轉負只出場、不反向）。收益來源是每小時結算的
資金費率，不是方向預測，所以幾週就能量出摩擦後是否為正。

規則（`signal.py`，計畫 §2 D6）：

| 步驟 | 內容 |
|---|---|
| 讀 | 邊界之前**最後一筆**結算當「現值」；它對前 `window_days` 天的 z-score（借 perp 套件自己的 `funding_zscore`，同一個定義）；最近 24 筆的平均 |
| out → in | `z ≥ z_in` 且現值 > 0 |
| in → out | `z ≤ z_out` 或最近 24 筆平均 ≤ 0；但要持有滿 `min_hold_days` 才看 |
| 其他 | 維持現狀。沒有讀數或算不出 z（樣本不足、視窗零變異）一律不動 |

| 參數 | 預設 | 意思 |
|---|---|---|
| `--window-days` | 30 | z-score 視窗 |
| `--z-in` | 1.5 | 進場門檻 |
| `--z-out` | 0.5 | 出場門檻（必須低於進場） |
| `--min-hold-days` | 3 | 進場後最短持有 |
| `--margin-pct` | 30 | perp 腿目標 margin（1x） |

## 為什麼是一個新套件

訊號是**一個數字、算一次**，兩條腿必須在**同一個邊界讀到同一個數字**。各自在兩邊套件裡算一份遲早漂移，而且
uniswap 那邊根本看不到 funding（計畫 §2 D1）。

借用照 `contrib/replay` 的作法，全列在 `upstream.py`：`hyperliquid_perp` 提供 `FundingPoint`、`funding_zscore`、
instants、原子寫檔、SQLite URI 拼法（`Path.as_uri` 碰到 UNC 與相對路徑都錯）、venue 錯誤型別；`autoresearch` 提供
research store 與它的 funding 分頁回補（`backfill_funding`，venue 端點一次最多 500 筆，往前走才不漏）、同一行回補報告、
結算週期常數，以及它那個「唯一的數字守衛」`require_number`（bool、超大整數、非有限值一律拒絕）。venue reader 只在 `load_market()` 裡 lazy import，`history` 與所有測試不載 SDK。
邊是單向的：`contrib/` 下**沒有任何套件**可以 import `contrib.carry`；本套件**不得**提到 `contrib.uniswap_v3` 與
`contrib.replay`，連測試都不行——`tests/test_upstream.py` 直接讀兩邊 source 守著。

對 uniswap 的 store 只有一句 `sqlite3` 唯讀查詢（`stores.py`，URI `mode=ro`），perp 的 store 同樣處理。

## 指令

```
python -m contrib.carry signal --coin ETH --out <handoff.json> --research-db <autoresearch.sqlite> \
    [--as-of 2026-10-08] [--no-fetch] \
    [--perp-db <paper_trading.db> --perp-run-id carry-ETH-1] \
    [--spot-db <uniswap.sqlite> --spot-run-id paper-carry-1]
```

每天 23:50 UTC 跑一次（排程在計畫 PR 4）：把視窗需要的 funding 回補進 research store（`--no-fetch` 就只讀 store）、
在**下一個** UTC 日線邊界讀規則、從 `--out` 現有的那份檔讀上一次的 position（這份檔也是協調者自己的記憶）、
從兩邊 store 讀 equity 算現貨 weight，然後原子寫檔。讀不出規則（邊界前沒有任何結算）就 exit 1、**不碰**交接檔；
現有的交接檔讀不懂、是別的幣的、或邊界比它晚，也都是 exit 1（讀不懂不會當成 out 再進一次；每個幣要有自己的 `--out`）。
**同一個邊界重跑不重做決定**：動作與 position 照上一份，只重算現貨 weight——重做會讓規則對著自己的結果讀。
`--perp-run-id`／`--spot-run-id` 在 store 裡沒有 equity 列就拒絕（打錯 run id 跟剛開的 run 長得一樣，不能靜默退回等額）；
真的剛開、還沒有快照的 run，那一次把 store 旗標省掉就是「等額」。任一邊 equity ≤ 0（帳戶已爆或現貨空了）時 weight 寫 0
並 WARNING，檔照寫——那一天正是另一條腿最需要知道的一天。

```
python -m contrib.carry history --coin ETH --research-db <autoresearch.sqlite> [--since 2024-01-01] [--until ...] [--rows]
```

把規則跑過 store 裡每一個讀得到完整視窗的日線邊界，印出：在場天數比例、進出次數、最長持有、在場期間收到的 funding
（每小時費率加總，佔名目的比例；空 perp 收正 funding）與年化。不是回測——沒有成交、手續費、現貨腿、gas；
那是 PR 4 的 report 對真實 run 算的事。先用 autoresearch 自己的 walk 把歷史放進 store：

```
python -m contrib.autoresearch fetch --coin ETH --since 2024-01-01
```

## 交接檔

一份 JSON，`handoff.py` 是它唯一的寫方與讀方；PR 2（perp 的檔案目標 provider）與 PR 3（uniswap 的 `target` 指令）
照這份 schema 讀，不 import 本套件。所有欄位都必填：

| 欄位 | 內容 |
|---|---|
| `version` | 1；讀方拒絕其他值 |
| `coin` | perp 幣（`ETH`）；`spot.token` 是對應的現貨代幣（`WETH`），明寫、讀方不用自己對應 |
| `as_of_ms` / `as_of` | 目標所屬的邊界，epoch 毫秒 / 同一瞬間的 ISO 字串（後者只給人看，不讀回） |
| `written_at_ms` / `written_at` | 寫檔時間；讀方據此判斷陳舊 |
| `action` | `enter` / `hold` / `exit` / `stay_out` |
| `position` | `{side: in|out, entered_at_ms}`，**動作之後**的 position |
| `perp` | `{side: short|flat, margin_pct}`；out 時 `flat` / 0 |
| `spot` | `{token, weight}`；weight 由 margin 與兩邊 equity 導出（perp 名目 ÷ 現貨 equity，上限 1、四位小數；任一邊 ≤ 0 → 0；out 時 0），寫出來給讀方省事，讀方會對照 `equity` 驗它 |
| `signal` | 做決定時的讀數，或 `null` |
| `equity` | 算 weight 用的兩邊 equity，沒給 store 就 `null`（此時兩邊視為等額，weight ＝ margin_pct）；≤ 0 照寫，weight 為 0 |

Decimal 一律字串，z 是 float。

## 陳舊政策（讀方的義務，計畫 §2 D5）

交接檔缺檔、陳舊、版本不對、幣不對：**兩腿都維持現狀並 WARNING，不 flat**。一腿 flat、另一腿還在，就是裸部位。
本套件只負責寫；這條規則由 PR 2 與 PR 3 各自實作，PR 4 的 report 亮燈。

## 不在這裡的東西

- perp 側讀交接檔的 `FileTargetDecisionProvider`（PR 2）。
- uniswap 側的 `targets` 表、`target` 指令與 `external_weights` 策略（PR 3）。
- `report`（兩邊 store 對帳：funding 收入、手續費、gas、淨 delta）、Lightsail 排程與 RUNBOOK（PR 4）。
- BTC 的第二個 run（`SPOT_TOKENS` 已經認得 `WBTC`，其餘待 ETH 跑過）。
