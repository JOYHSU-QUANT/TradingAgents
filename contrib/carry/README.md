# carry

協調者不下單。

它不簽任何交易、不寫 `contrib/hyperliquid_perp/` 的 store、也不寫 `contrib/uniswap_v3/` 的 store。它只做一件事：
讀 Hyperliquid 的 funding 歷史，套**一條規則**，寫**一份交接檔**，說兩條腿在某個 UTC 日線邊界該持有什麼；
兩邊的引擎各自讀那份檔、在各自的閘門下決定要不要動。上面那句話就是這個套件的 scope 判準：任何需要把它放寬的
改動都是 scope creep，不是更大的功能。

完整設計在 local-only 的 `.claude/carry-sleeve-plan-2026-10-07.md`；七題拍板在 memory `carry-sleeve-plan-2026-10-07`，
PR 1 review 時再拍的四題記在計畫檔 §7。

## 策略一句話

空 perp、多現貨，只做 funding 為正的一邊（現貨沒有融券，funding 轉負只出場、不反向）。收益來源是每小時結算的
資金費率，不是方向預測，所以幾週就能量出摩擦後是否為正。

規則（`signal.py`，計畫 §2 D6）：

| 步驟 | 內容 |
|---|---|
| 讀 | 邊界之前**最後一筆**結算當「現值」；它對前 `window_days` 天的 z-score（借 perp 套件自己的 `funding_zscore`，同一個定義）；最近 24 小時內的結算（含現值，至少 12 筆）的平均 |
| out → in | `z ≥ z_in` 且現值 > 0 |
| in → out | `z ≤ z_out` **或**最近一天平均 ≤ 0，兩條腿各自看；但要持有滿 `min_hold_days` 才看 |
| 其他 | 維持現狀。沒有讀數就不動；算不出 z（樣本不足、視窗零變異）時不能進場、z 那條腿也不能出場，但「最近一天平均 ≤ 0」那條腿照樣能出 |

| 參數 | 預設 | 意思 |
|---|---|---|
| `--window-days` | 30 | z-score 視窗 |
| `--z-in` | 1.5 | 進場門檻 |
| `--z-out` | 0.5 | 出場門檻（必須低於進場） |
| `--min-hold-days` | 3 | 進場後最短持有 |
| `--margin-pct` | 30 | perp 腿目標 margin（1x）；**要落在 perp run 的 margin 格點上**（PR 4 的 RUNBOOK 負責對齊），否則 perp 閘門每天都擋、現貨腿卻照買 |

## 為什麼是一個新套件

訊號是**一個數字、算一次**，兩條腿必須在**同一個邊界讀到同一個數字**。各自在兩邊套件裡算一份遲早漂移，而且
uniswap 那邊根本看不到 funding（計畫 §2 D1）。

借用照 `contrib/replay` 的作法，全列在 `upstream.py`：`hyperliquid_perp` 提供 `FundingPoint`、`funding_zscore`、
instants（含解 perp 快照時戳的 `parse_instant`）、原子寫檔、SQLite URI 拼法（`Path.as_uri` 碰到 UNC 與相對路徑都錯）、
venue 錯誤型別；`autoresearch` 提供 research store 與它的 funding 分頁回補（`backfill_funding`，venue 端點一次最多
500 筆，往前走才不漏）、回補結束的原因（`StopReason`，沒走到底就不做決定）、同一行回補報告、venue reader 的
`HistoryMarketData` 協定、結算週期常數，以及它那個「唯一的數字守衛」`require_number`（bool、超大整數、非有限值一律拒絕）。
venue reader 只在 `load_market()` 裡 lazy import，`history` 與用假 venue 的測試不載 SDK（唯一的例外是 pin 住借用名稱的
那個測試）。邊是單向的：`contrib/` 下**沒有任何套件**可以 import `contrib.carry`；本套件**不得 import**
`contrib.uniswap_v3` 與 `contrib.replay`，連測試都不行——`tests/test_upstream.py` 從 import 圖守著（散文裡提到名字不算）。

對 uniswap 的 store 只有一句 `sqlite3` 唯讀查詢（`stores.py`，URI `mode=ro`），perp 的 store 同樣處理。

## 指令

```
python -m contrib.carry signal --coin ETH --out <handoff.json> --research-db <autoresearch.sqlite> \
    [--as-of 2026-10-08] [--no-fetch] [--max-reading-age-hours 3] \
    [--perp-db <paper_trading.db> --perp-run-id carry-ETH-1] \
    [--spot-db <uniswap.sqlite> --spot-run-id paper-carry-1]
```

每天 23:50 UTC 跑一次（排程在計畫 PR 4，並在 23:55 排一次重試）：把視窗需要的 funding 回補進 research store
（`--no-fetch` 就只讀 store）、在要決定的 UTC 日線邊界讀規則、從 `--out` 現有的那份檔讀上一次的 position（這份檔也是
協調者自己的記憶）、從兩邊 store 讀 equity 算現貨 weight，然後原子寫檔（LF 換行，各平台位元組相同）。

**邊界怎麼取**：預設是下一個午夜；但若現在離上一個午夜不到 6 小時，就當成那個午夜的晚跑（排程延遲、重試）——
否則一次延遲會讓剛過的那天沒有交接檔、下一天又提早被凍結。上一份交接檔與這次邊界之間若空了整天，stderr 會點名
被跳過的邊界。

**什麼時候 exit 1、不碰交接檔**：邊界前沒有任何結算；最後一筆結算離邊界超過 `--max-reading-age-hours`（預設 3）——
對著幾天沒更新的 store 做決定比不做更糟；回補沒走到視窗盡頭（不論原因：請求上限、venue 沒資料、走不動）；venue 失敗（不退回 store 裡的舊資料，
由 PR 4 的 23:55 重試補；重試不是同邊界重跑，是正常決定）；現有的交接檔讀不懂、是別的幣的、或邊界比它晚
（讀不懂不會當成 out 再進一次；每個幣要有自己的 `--out`）；`--out` 的目錄不存在；`--no-fetch` 或 `history` 指到不存在的
research store（不會偷偷建一個空的）；`--as-of` 不是 UTC 午夜、沒有時區、或遠到視窗還在未來（接受 `2026-10-08`、
`2026-10-08T00:00:00Z`、`...+00:00` 三種寫法）；只給 `--perp-db` 沒給 `--perp-run-id`（反之亦然）；現貨 run 的計價幣不是
美元穩定幣（USDC／USDT／DAI，否則 equity 單位對不上 perp）；`--perp-run-id`／`--spot-run-id` 在 store 裡沒有 equity 列
（打錯 run id 跟剛開的 run 長得一樣，不能靜默退回等額；真的剛開、還沒有快照的 run，那一次把該腿的兩個 store 旗標
一起省掉就是「等額」）。

**同一個邊界重跑不重做決定**：動作、position 與當時的 `signal` 照上一份，只重算兩腿的倉位大小（perp margin 跟著這次的 `params`、現貨 weight 跟著 equity）——重做會讓規則對著自己的
結果讀。重跑時規則參數與上一份不同會 WARNING，不拒絕（換 `--margin-pct` 重跑就是中途調倉的方式）；stdout 的 `funding:` 行印的是檔裡那份決策讀數，
新讀數若不同會另印一行 `current (not the decision's)`。

**WARNING 不擋**：任一邊 equity ≤ 0（帳戶已爆或現貨空了）時 weight 寫 0、檔照寫——那一天正是另一條腿最需要知道的一天；
兩邊都為正但比例截到 0 也會說；equity 快照超過 48 小時沒更新（那個 run 大概停了）也會說。

```
python -m contrib.carry history --coin ETH --research-db <autoresearch.sqlite> [--since 2024-01-01] [--until ...] [--rows]
```

把規則跑過 store 裡每一個讀得到完整視窗、而且後面一整天都已結算的日線邊界（最後一筆結算離邊界超過
`--max-reading-age-hours` 的邊界比照 live 不做決定、只計數），印出：在場天數比例、進出次數、
最長持有、在場期間收到的 funding（每小時費率加總，佔名目的比例；空 perp 收正 funding）與年化。不是回測——沒有成交、
手續費、現貨腿、gas；那是 PR 4 的 report 對真實 run 算的事。先用 autoresearch 自己的 walk 把歷史放進**同一個** store 檔：

```
python -m contrib.autoresearch fetch --db <autoresearch.sqlite> --coin ETH --since 2024-01-01
```

## 交接檔

一份 JSON，`handoff.py` 是它在本套件這邊唯一的寫方與讀方；PR 2（perp 的檔案目標 provider）與 PR 3（uniswap 的
`target` 指令）照這份 schema 讀，不 import 本套件。所有欄位都會寫；給人看的 ISO 字串（`as_of`、`written_at`、`signal.read_at`、`perp_at`、`spot_at`）
由 `*_ms` 導出、不讀回：

| 欄位 | 內容 |
|---|---|
| `version` | 1（整數）；讀方拒絕其他值。**同版本可以加欄位**，讀方要忽略不認識的 key；只有既有欄位變義才 bump，而且讀方先部署、寫方後部署 |
| `coin` | perp 幣（`ETH`）；`spot.token` 是對應的現貨代幣（`WETH`），明寫、讀方不用自己對應 |
| `as_of_ms` / `as_of` | 目標所屬的邊界，epoch 毫秒 / 同一瞬間的 ISO 字串。**陳舊判準的錨點**（見下節） |
| `written_at_ms` / `written_at` | 寫檔時間，**只是資訊**，不是陳舊判準 |
| `action` | `enter` / `hold` / `exit` / `stay_out` |
| `position` | `{side: in|out, entered_at_ms}`，**動作之後**的 position；`enter` 的 `entered_at_ms` 就是 `as_of_ms`，`hold` 的一定在它之前 |
| `params` | 寫這份檔時的規則參數 `{window_days, z_in, z_out, min_hold_days, margin_pct}`；同邊界重跑時是重跑那次的（倉位大小跟著它），決策當時的參數在被取代的前一份檔裡 |
| `perp` | `{side: short|flat, margin_pct}`；margin ＝ `params.margin_pct`（in）或 0（out）。這是規則的 **intent**，perp 閘門可能給不到；差額由 PR 4 的 report 對帳（計畫 D7） |
| `spot` | `{token, weight}`；weight 由 margin 與兩邊 equity 導出（perp 名目 ÷ 現貨 equity，上限 1、四位小數；**已知**的任一邊 ≤ 0 → 0；未知 → 等額；out 時 0），寫出來給讀方省事。**讀方只檢查 0 ≤ weight ≤ 1，不重算**；寫方讀回自己的檔時會對照 `equity` 驗它 |
| `signal` | 做決定時的讀數 `{read_at_ms, read_at, funding_hourly, z, samples, recent_mean_hourly, recent_samples}`（`read_at_ms` 一定在 `as_of_ms` 之前；`enter` 一定有），或 `null` |
| `equity` | 算 weight 用的兩邊 equity 與各自快照的時戳 `{perp, perp_at_ms, perp_at, spot, spot_at_ms, spot_at}`；沒給 store 的那邊整組 `null`（此時兩邊視為等額）；≤ 0 照寫，weight 為 0 |

Decimal 一律字串、純位置記法（`0.0000005`，不會是 `5E-7`）；z 與門檻是 float。

## 陳舊政策（讀方的義務，計畫 §2 D5）

讀方**以 `as_of_ms` 計齡**：`as_of_ms ≤ now < as_of_ms + 1 天` 才照檔行動，否則視為陳舊——**兩腿都維持現狀並 WARNING，
不 flat**。一腿 flat、另一腿還在，就是裸部位。不用 `written_at` 計齡，因為協調者失敗一天留下的檔，寫檔時間看起來
夠新、目標卻是昨天的。缺檔、版本不對、幣不對、讀不懂，同樣當陳舊。本套件只負責寫；這條規則由 PR 2 與 PR 3
各自實作，PR 4 的 report 亮燈。

## 不在這裡的東西

- perp 側讀交接檔的 `FileTargetDecisionProvider`（PR 2）。
- uniswap 側的 `targets` 表、`target` 指令與 `external_weights` 策略（PR 3）。
- `report`（兩邊 store 對帳：funding 收入、手續費、gas、淨 delta）、Lightsail 排程（23:50 加 23:55 重試）與 RUNBOOK（PR 4）。
- BTC 的第二個 run（`SPOT_TOKENS` 已經認得 `WBTC`，其餘待 ETH 跑過）。
