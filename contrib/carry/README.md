# carry

協調者不下單。

它不簽任何交易、不寫 `contrib/hyperliquid_perp/` 的 store、也不寫 `contrib/uniswap_v3/` 的 store。它只做一件事：
讀 Hyperliquid 的 funding 歷史，套**一條規則**，寫**一份交接檔**，說兩條腿在某個 UTC 日線邊界該持有什麼；
兩邊的引擎各自讀那份檔、在各自的閘門下決定要不要動。上面那句話就是這個套件的 scope 判準：任何需要把它放寬的
改動都是 scope creep，不是更大的功能。`report` 也只是讀：兩邊的 store 唯讀、perp 留下的 sidecar 唯讀，對帳後亮燈，
不補單（計畫 D7 第一階段「只報不改」）。

完整設計在 local-only 的 `.claude/carry-sleeve-plan-2026-10-07.md`；七題拍板在 memory `carry-sleeve-plan-2026-10-07`，
各張 PR 開工與 review 時再拍的題記在計畫檔 §7。部署與日常操作在 [RUNBOOK.md](./RUNBOOK.md)。

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
| `--margin-pct` | 30 | perp 腿目標 margin（1x）；**要落在 perp run 的 margin 格點上**（RUNBOOK §1.2 負責對齊），否則 perp 閘門每天都擋、現貨腿卻照買 |

## 為什麼是一個新套件

訊號是**一個數字、算一次**，兩條腿必須在**同一個邊界讀到同一個數字**。各自在兩邊套件裡算一份遲早漂移，而且
uniswap 那邊根本看不到 funding（計畫 §2 D1）。

借用照 `contrib/replay` 的作法，全列在 `upstream.py`：共用地板 `core` 提供 instants（含解 perp 快照時戳的
`parse_instant`）與原子寫檔；`hyperliquid_perp` 提供 `FundingPoint`、`funding_zscore`、SQLite URI 拼法（`Path.as_uri` 碰到 UNC 與相對路徑都錯）、
venue 錯誤型別，以及 `report` 要的三個 file-target 拼法（`ai_inputs.model` 的 `file-target`、兩個 rationale 前綴）與
payload 目錄的規則（sidecar 住在 payload 旁邊，規則是 perp 套件的、不另抄一份）；`autoresearch` 提供 research store 與它的
funding 分頁回補（`backfill_funding`，venue 端點一次最多 500 筆，往前走才不漏）、回補結束的原因（`StopReason`，沒走到底就不做決定）、
同一行回補報告、venue reader 的 `HistoryMarketData` 協定、結算週期常數，以及它那個「唯一的數字守衛」`require_number`
（bool、超大整數、非有限值一律拒絕）。venue reader 只在 `load_market()` 裡 lazy import，`history`、`report` 與用假 venue 的測試不載 SDK
（唯一的例外是 pin 住借用名稱的那個測試）。邊是單向的：`contrib/` 下**沒有任何套件**可以 import `contrib.carry`；本套件**不得 import**
`contrib.uniswap_v3` 與 `contrib.replay`，連測試都不行——`tests/test_upstream.py` 從 import 圖守著（散文裡提到名字不算）。

對兩邊 store 的讀取全在 `stores.py`，只有 `sqlite3` 唯讀查詢（URI `mode=ro`）：協調者讀兩邊 equity 各一句；`report` 多讀
perp 的 funding、fills、快照、decision cycle，與 uniswap 的 decisions、valuations、fills、targets。

## 指令

```
python -m contrib.carry signal --coin ETH --out <handoff.json> --research-db <autoresearch.sqlite> \
    [--as-of 2026-10-08] [--no-fetch] [--max-reading-age-hours 3] \
    [--perp-db <paper_trading.db> --perp-run-id carry-ETH-1] \
    [--spot-db <uniswap.sqlite> --spot-run-id paper-carry-1]
```

每天 23:50 UTC 跑一次（`schedule/carry-signal.timer`；失敗 5 分鐘後由 service 重試一次，RUNBOOK §3）：把視窗需要的 funding 回補進 research store
（`--no-fetch` 就只讀 store）、在要決定的 UTC 日線邊界讀規則、從 `--out` 現有的那份檔讀上一次的 position（這份檔也是
協調者自己的記憶）、從兩邊 store 讀 equity 算現貨 weight，然後原子寫檔（LF 換行，各平台位元組相同）。

**邊界怎麼取**：預設是下一個午夜；但若現在離上一個午夜不到 6 小時，就當成那個午夜的晚跑（排程延遲、重試）——
否則一次延遲會讓剛過的那天沒有交接檔、下一天又提早被凍結。上一份交接檔與這次邊界之間若空了整天，stderr 會點名
被跳過的邊界。

**什麼時候 exit 1、不碰交接檔**：邊界前沒有任何結算；最後一筆結算離邊界超過 `--max-reading-age-hours`（預設 3）——
對著幾天沒更新的 store 做決定比不做更糟；回補沒走到視窗盡頭（不論原因：請求上限、venue 沒資料、走不動）；venue 失敗（不退回 store 裡的舊資料，
由 service 的一次重試補；重試不是同邊界重跑，是正常決定）；現有的交接檔讀不懂、是別的幣的、或邊界比它晚
（讀不懂不會當成 out 再進一次；每個幣要有自己的 `--out`）；`--out` 的目錄不存在；`--no-fetch` 或 `history` 指到不存在的
research store（不會偷偷建一個空的）；`--as-of` 不是 UTC 午夜、沒有時區、或遠到視窗還在未來（後者只在會 fetch 時查；接受 `2026-10-08`、
`2026-10-08T00:00:00Z`、`...+00:00` 三種寫法）；只給 `--perp-db` 沒給 `--perp-run-id`（反之亦然）；現貨 run 的計價幣不是
美元穩定幣（USDC／USDT／DAI，否則 equity 單位對不上 perp）；`--perp-run-id`／`--spot-run-id` 在 store 裡沒有 equity 列
（打錯 run id 跟剛開的 run 長得一樣，不能靜默退回等額；真的剛開、還沒有快照的 run，那一次把該腿的兩個 store 旗標
一起省掉就是「等額」）。

**同一個邊界重跑不重做決定**：動作、position 與當時的 `signal` 照上一份，只重算兩腿的倉位大小（perp margin 跟著這次的 `params`、現貨 weight 跟著 equity）——重做會讓規則對著自己的
結果讀。重跑時規則參數與上一份不同會 WARNING，不拒絕（換 `--margin-pct` 重跑就是中途調倉的方式）；stdout 的 `funding:` 行印的是檔裡那份決策讀數，
新讀數若不同會另印一行 `current (not the decision's)`。排程刻意**不**在成功後再跑一次：重跑會換掉檔的位元組，兩腿若一前一後讀到不同版本，
`report` 會亮「different documents」。

**WARNING 不擋**：任一邊 equity ≤ 0（帳戶已爆或現貨空了）時 weight 寫 0、檔照寫——那一天正是另一條腿最需要知道的一天；
兩邊都為正但比例截到 0 也會說；equity 快照超過 48 小時沒更新（那個 run 大概停了）也會說。

```
python -m contrib.carry history --coin ETH --research-db <autoresearch.sqlite> [--since 2024-01-01] [--until ...] [--rows]
```

把規則跑過 store 裡每一個讀得到完整視窗、而且後面一整天都已結算的日線邊界（最後一筆結算離邊界超過
`--max-reading-age-hours` 的邊界比照 live 不做決定、只計數），印出：在場天數比例、進出次數、
最長持有、在場期間收到的 funding（每小時費率加總，佔名目的比例；空 perp 收正 funding）與年化。不是回測——沒有成交、
手續費、現貨腿、gas；那是 `report` 對真實 run 算的事。先用 autoresearch 自己的 walk 把歷史放進**同一個** store 檔：

```
python -m contrib.autoresearch fetch --db <autoresearch.sqlite> --coin ETH --since 2024-01-01
```

```
python -m contrib.carry report --coin ETH \
    --perp-db <paper_trading.db> --perp-run-id carry-ETH-1 \
    --spot-db <uniswap paper.db> --spot-run-id paper-carry-1 \
    [--payloads-dir <perp payloads/carry-ETH-1>] [--since 2026-10-20] [--until 2026-11-16] \
    [--delta-alert-pct 10] [--d5-days 3]
```

兩邊帳本的對帳（計畫 §3.5），每個 UTC 日一列、三行，之後每個 ISO 週一行合計、最後整段合計。唯讀，daemon 不用停；
store 可以是伺服器上的，也可以是快照下來的（perp 的 sidecar 目錄跟著 `--payloads-dir` 走，預設是 perp 套件的佈局
`<perp-db 目錄>/payloads/<perp-run-id>`）。預設從兩個 run 最早有東西的那天到昨天；`--since`／`--until` 要是 UTC 日邊界。

每一列：

| 項目 | 怎麼算 |
|---|---|
| perp 這一天 | 以 UTC 日切：funding 收入＝`funding_events` 裡 `posted` 的 `funding_pnl` 加總（帳本正負號，收到為正）；手續費＝`fills.fee` 加總；倉位與 equity 取當天**最後一個** cycle 快照；cycle 以排程時間歸日 |
| spot 這一天 | 就是那根日線 bar：決策、valuation（成交後、以收盤價計）、fills 都在 `time`＝邊界那根；swap 成本＝以 bar 收盤價算的「進去的價值 − 出來的價值」（池子費＋滑價，與 uniswap `report` 的兩項加總相同，不帶它的費率表）；gas＝`gas_cost_eth` × valuation 裡 WETH 的價（沒有 WETH 價就只印 ETH 數、不計入成本並在合計註明） |
| cycle 種類 | `applied`（讀到可用的交接檔、送進閘門；`refused by the gate` 另計——含 off-grid margin 的 fail closed，排程器記成 `invalid_output`、閘門字是 `invalid_fail_closed`，sidecar 照寫）、`pending`（午夜前讀到明天的檔，正常排程）、`unusable`（缺檔、陳舊、別的幣、讀不懂：兩腿維持，D5）、`no_decision`（cycle 沒有決策：市場資料、寫入失敗、沒有答案列）、`engine`（這個 run 不是 file-target）。閘門的字走 perp 的 `RiskAction` 列舉，不認識的字拒絕整份報表 |
| delta | **以幣計**，不以美元：現貨持有的代幣數 − perp 空單數量；兩個時點不同（現貨是 bar 收盤、perp 是當日最後快照），美元讀法會隨兩邊同向的價差亂亮。超過較大一腿的 `--delta-alert-pct`（預設 10%，D7）就亮，只要任一腿有東西就看，不看當天 intent——出場只做掉一腿就是計畫怕的裸部位。一腿當天沒有讀數（perp 沒快照、現貨那根沒決策）就沿用它最後一次已知的持有量，日列印 `(as of <日期>)`：沒看到不等於空手 |
| basis | 現貨 bar 收盤價對 perp 當天第一個快照的 mark（那個快照要有倉位，flat 的快照印 n/a；現貨那根沒決策、沿用前一天讀數的日子也印 n/a——舊收盤對今天的 mark 是價格移動不是基差）：`spot / mark − 1`，並印那個快照的時間；perp cycle 是滾動的 4 小時，第一個快照最多晚午夜 4 小時，所以這個數**含最多一個 cycle 的價格移動**，看趨勢不看單日 |
| in／out | 當天的 intent：perp sidecar 說 short 就是 in，沒有 sidecar 就看現貨 target 的 weight > 0；兩者都沒有印 `?` |
| net | funding − perp 手續費 − 現貨 swap 成本 − gas |

亮燈（印在那一天的列下面，`ALERT <kind>:`）：

| kind | 條件 |
|---|---|
| `delta` | 上表的 delta 超過門檻 |
| `held by D5` | 「被 D5 維持」的日子連續達 `--d5-days`（預設 3）：perp 當天沒有任何 `applied` 而有 `unusable`（只有 `pending` 不算），或現貨那根被策略決策了（`skipped_suspect` 不算）卻沒有 target 列。一腿真的對著文件行動的那天才中斷連續；兩腿都沒動的 `silent day` 不中斷也不累加 |
| `one-legged` | 現貨那根對著 target 決策了，perp 當天卻沒有任何 cycle applied；或反過來 perp applied 了、現貨沒有 target 或沒決策 |
| `different documents` | 現貨 `targets.digest`（`target` 指令讀到的交接檔位元組 sha256）不在 perp 當天 sidecar 記的 digest 裡：兩腿讀的不是同一份檔 |
| `no sidecar` | perp 有 applied cycle，payload 目錄下卻找不到當天的 sidecar：目錄指錯或檔案丟了 |
| `silent day` | 那天沒有任何 perp cycle 讀到可用或不可用的文件（沒跑、全部 `no_decision`、全部 `pending`＝只看到明天的檔、或不是 file-target run）、現貨也沒被策略決策（沒 visit 或 bar 可疑被跳過）：兩腿都沒動，否則看起來像安靜的一天。perp 對著不可用文件維持的日子歸 D5，不算這盞 |
| `sidecar not read` | 一個 `.handoff.json` 讀不出來（壞 JSON、別的幣、缺欄位）：逐檔列在日列之後、不歸哪一天，算進亮燈數 |

合計行印：在場天數、funding（與 venue 還沒入帳的 pending 結算數——那些沒算進去）、成本、net、亮燈數（含讀不出來的 sidecar）與被 D5 維持的天數。
`one-legged` 的現貨側只算真的對著 target 決策過的 bar：`skipped_suspect`（bar 可疑、策略沒被問）不算有行動。
結束碼：0 印完（亮燈不改結束碼，`grep ALERT`）；1 拒絕——現貨 run 的設定快照沒有 `targets` 區塊（不是 carry run）、交易的代幣不含交接檔那個、
兩個 run 都沒有任何決策、span 空、門檻 < 1、store 不在或不是那種 store。

## 交接檔

一份 JSON，`handoff.py` 是它在本套件這邊唯一的寫方與讀方；perp 的 `FileTargetDecisionProvider`（PR 2）與 uniswap 的
`target` 指令（PR 3）照這份 schema 讀，不 import 本套件。所有欄位都會寫；給人看的 ISO 字串（`as_of`、`written_at`、`signal.read_at`、`perp_at`、`spot_at`）
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
| `perp` | `{side: short|flat, margin_pct}`；margin ＝ `params.margin_pct`（in）或 0（out）。這是規則的 **intent**，perp 閘門可能給不到；差額由 `report` 對帳（計畫 D7） |
| `spot` | `{token, weight}`；weight 由 margin 與兩邊 equity 導出（perp 名目 ÷ 現貨 equity，上限 1、四位小數；**已知**的任一邊 ≤ 0 → 0；未知 → 等額；out 時 0），寫出來給讀方省事。**讀方只檢查 0 ≤ weight ≤ 1，不重算**；寫方讀回自己的檔時會對照 `equity` 驗它 |
| `signal` | 做決定時的讀數 `{read_at_ms, read_at, funding_hourly, z, samples, recent_mean_hourly, recent_samples}`（`read_at_ms` 一定在 `as_of_ms` 之前；`enter` 一定有），或 `null` |
| `equity` | 算 weight 用的兩邊 equity 與各自快照的時戳 `{perp, perp_at_ms, perp_at, spot, spot_at_ms, spot_at}`；沒給 store 的那邊整組 `null`（此時兩邊視為等額）；≤ 0 照寫，weight 為 0 |

Decimal 一律字串、純位置記法（`0.0000005`，不會是 `5E-7`）；z 與門檻是 float。

perp 讀到可用的檔時把它連同原始位元組的 sha256 寫成 `<payload>.handoff.json` sidecar（perp 套件的 `common.sidecar` 契約；
後綴與 file-target 的三個拼法一樣住在 perp 的 `common/constants.py`，`report` 經 `upstream.py` 借來，不 import provider 本身——那會把引擎拉進來）；
uniswap 的 `target` 指令把同一份位元組的 sha256 寫進 `targets.digest`。`report` 拿這兩個 digest 比，確認兩腿讀的是同一份檔。

## 陳舊政策（讀方的義務，計畫 §2 D5）

讀方**以 `as_of_ms` 計齡**：`as_of_ms ≤ now < as_of_ms + 1 天` 才照檔行動，否則視為陳舊——**兩腿都維持現狀並 WARNING，
不 flat**。一腿 flat、另一腿還在，就是裸部位。不用 `written_at` 計齡，因為協調者失敗一天留下的檔，寫檔時間看起來
夠新、目標卻是昨天的。缺檔、版本不對、幣不對、讀不懂，同樣當陳舊。本套件只負責寫；這條規則由 PR 2 與 PR 3
各自實作，`report` 亮燈（一腿被 D5 維持連續 `--d5-days` 天就是「由人介入」的那一刻）。

## 不在這裡的東西

- perp 側讀交接檔的 `FileTargetDecisionProvider`（`contrib/hyperliquid_perp/integration/file_target_provider.py`，PR 2）。
- uniswap 側的 `targets` 表、`target` 指令與 `external_weights` 策略（PR 3）。
- 自動補 delta（計畫 D7 (b)）：paper 跑滿四週、看過 delta 的分布再說。
- BTC 的第二個 run（`SPOT_TOKENS` 已經認得 `WBTC`，其餘待 ETH 跑過；`report` 的 gas 計價靠 valuation 裡的 WETH 價，
  只交易 WBTC 的 run 沒有它，gas 會印 ETH 數而不計入成本）。
- 本機快照腳本（`.claude/bin/pull-paper-snapshot.sh`）還沒有抓 carry 的 store 與 sidecar；要在本機跑 `report` 得自己 scp。
