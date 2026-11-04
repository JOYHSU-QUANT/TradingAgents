# discovery

Discovery Lab 不是另一條研究路徑，也不是交易路徑。

它不讀真實市場、不寫別的套件擁有的任何 store（只寫自己每臂一顆的 sqlite）、不改 autoresearch 的 DSL。它做的事只有一件：**造有標準答案的合成世界，考既有
`contrib/autoresearch` 的研究迴圈找不找得到埋的規律、會不會在純雜訊世界硬掰出規則。** 考的是「AI 做研究會不會自欺」，
不是「AI 看盤準不準」（後者 `contrib/replay` 已在量）。上面那句話就是這個套件的 scope 判準：**任何需要把它放寬的改動，
都是 scope creep，不是更大的功能。**

完整設計在 local-only 的 `.claude/discovery-plan-2026-10-21.md`；拍板依據在 memory `discovery-lab-plan-2026-10-21`。

## Scope statement

**Objective** — Evaluate whether an existing LLM-driven research loop can efficiently identify and validate predictive
rules within a finite-structure, bounded hypothesis space.

**In scope** — synthetic candle／funding generation through the existing `HistoryMarketData` port; ground-truth hypotheses
expressible in the existing declarative DSL; null-world false-eligibility calibration; oracle／random／grid／LLM-guided
search comparison under matched budgets; structural and behavioral hypothesis recovery; out-of-sample generalization;
separate evaluation of research discovery and downstream decision influence.

**Out of scope** — open-ended hypothesis generation; arbitrary mathematical or executable hypotheses; discovery outside the
existing DSL vocabulary; evidence of general scientific reasoning; live trading or production strategy promotion; **any
change to the DSL to make the benchmark easier**.

**Interpretation** — success demonstrates effective hypothesis search and validation within the supported DSL. It does not
establish general autonomous scientific discovery capability. Track B（開放式假說）另起爐灶。

## 現況

**PR 1（骨架，已落地）**：套件、借用漏斗、manifest 格式、最終判定、oracle 的讀取原語、隔離規則的測試。
**PR 2（世界生成器，已落地）**：`worlds.py`（generator v1 與 `SyntheticHistory` port）、`landing.py`（經 autoresearch 的
backfill 落地＋驗收）、`generate` 指令。Random／Grid 臂與 oracle 的其餘部分（PR 3）、評分器（PR 4）、LLM 臂（PR 5）、
replay 消融（PR 6）都還沒有。

已經有的東西：

- [`upstream.py`](./upstream.py)：這個套件借的每個名字列在一處。只借 `contrib.autoresearch` 與 `contrib.core`；
  `tests/test_upstream.py` 用 AST 掃描釘住：其他模組只從 `upstream.py` import、src 與 tests 都不碰 perp／replay／carry／
  uniswap／dashboard／`tradingagents`／venue SDK、被借的兩個套件不回頭 import 這裡。
- [`manifest.py`](./manifest.py)：一個世界＋一次 benchmark 的全部參數（計畫 §5），frozen、`to_dict`／`from_dict` 逐層嚴格
  （鍵集合完全相等、每個值檢查、版本不對拒絕）。`opaque_world_id(seed_set, world_seed)` 產生不透明 id（`SYN-` 加 8 個大寫
  hex），**世界類型不是輸入**，所以 id 無法洩漏它；id 就是各臂 experiment 的 coin 名，也是 manifest 裡唯一會進 prompt 的字。
- [`verdict.py`](./verdict.py)：最終判定（計畫 §4.5）。`snapshot_arm` 在搜尋結束、晉升之前讀一次 ledger，把每筆 trial 的
  train／validation 數字、實際 `rules_tried`、penalty、結束原因存成快照（`ArmSnapshot.dumps()`；PR 3 起寫成 `verdicts.json`）；`final_verdict` 是純函式，
  用 autoresearch 自己的 `promotion_verdict` 對每筆 trial 以**最終**的規則數重判，「已 promoted」這個 blocker 因為 trial 被
  重建成 measured 而結構上不存在。晉升後的重建只讀快照，不重問 ledger。
- [`oracle.py`](./oracle.py)：`evaluate_unfiled`，oracle 走的讀取路徑：`evaluate_split` 不落 ledger（計畫 §1 #4／#5），
  回整個 `SplitResult`（評分器要讀 trades），holdout 那條跟 `promote` 一樣先過 `require_measurable_tail`。
  什麼時候可以讀 holdout 是 protocol（PR 3 的 runner），不是這個函式管的。
- [`records.py`](./records.py)：兩份 JSON 紀錄共用的嚴格讀取器（`Reader`，各模組帶自己的 error class）；`from_dict` 管
  鍵集合與只有原始文件才能做的檢查（版本、寫出的 id／hash 對衍生值、parser），每個欄位自己的值一律在 `__post_init__`
  驗（與 autoresearch 的 `Penalty.from_dict` 同款）；重複的 JSON key 拒絕。
- [`worlds.py`](./worlds.py)：`generate(manifest)` 把一個 seed 變成一份 `WorldHistory`（4h bars、1d 聚合、hourly funding、
  每根 bar 帶的埋入方向；吃整份 manifest 是因為 `indicator_lookback` 也決定 conditional 世界的持倉 bar）；
  `SyntheticHistory` 把它包成 autoresearch 的 `HistoryMarketData` port。定義見下一節。
- [`landing.py`](./landing.py)：`land()` 走 autoresearch 自己的 `backfill_candles`／`backfill_funding` 把世界落進一顆
  research store（三條 walk 都要走到頭，否則拒絕）；`verify()` 跑 PR 2 的驗收清單並回 `Landing`；`split_of()`＝manifest
  的 `count` 根決策 bar 按 shares 切（`Split.by_shares`），`experiment_for()` 把 manifest 的條件綁成各臂 ledger 要建的
  `Experiment`；`world_dir()`／`land_world()`＝run 目錄的配置與分段落地（見「檔案配置」）。
- [`cli.py`](./cli.py)：`python -m contrib.discovery generate --manifest <path> [--root <dir>]`。

## 世界生成器（generator v1）

一個世界由 manifest 的 `world` 區塊完全決定；`generator_version` 不是 `"1"` 的 manifest 直接拒絕。

- **亂數流**：三條（`price`／`wicks`／`funding`），都從不透明 id 派生（`random.Random(f"{id}/{name}")`），**kind 不進種子**，
  所以同 seed 的 Null 與 Conditional 共用同一組抽樣、只差埋入效果。每根 bar 的抽樣按 bar 順序消耗，短世界是長世界的
  prefix＝生成器內無前視。
- **Null-v1**：`log P[t] = log P[t-1] + drift + vol·Z[t] − vol²/2`，`Z` i.i.d. N(0,1)，`vol` 以一根 4h bar 為單位；`−vol²/2`
  讓 `drift = 0` 時下一根收盤的條件期望等於現價。每根 bar 的 open＝前一根 close（連續路徑、無跳空）；high／low＝實體兩端
  各加一段半常態 wick（`WICK_SCALE·vol·|N(0,1)|`，log 空間）；volume 常數 1。funding：`f[k] = sigma·W[k]`，`W` i.i.d.
  N(0,1)，每小時一筆、戳記在整點後 `SETTLEMENT_LAG_MS`（50 ms，與 venue 的貼出延遲同型），從第一根 open 到最後一根
  close 應結的那一筆（共 `4n+1` 筆）。1d 由同一條 4h 路徑聚合（只出完整日）。venue 時鐘＝最後一筆 funding 貼出後 1 ms。
- **Conditional-v1**：同一組抽樣，再在「埋的規則持倉中的 bar」的 log 報酬加上 `beta`（short 側＝`−beta`）。哪些 bar 持倉由
  autoresearch 自己的 `replay_sides` 在**未加效果的路徑**（同 seed 的 null 世界）上判定：收盤決策、下一根開盤成交、
  `max_bars`／exit 規則／反手；效果從成交那根起算，**決策當根不加**。價格衍生的 gate（`realized_vol_*`、`regime`）在最終
  路徑上可能在門檻邊緣差幾根，這是 v1 的定義（單趟、不做 fixed point）；oracle 量的是最終路徑，β 的校準以它為準。
- **價格的寫法**：float 路徑以 `repr` 轉 Decimal（最短可逆串），不貼 tick；同一個 float 同一個 Decimal，所以 open＝前一根
  close 在 store 裡也精確相等。
- **落地**：`land()` 用 `SyntheticHistory` 當 venue、以世界自己的時鐘切視窗，三條 walk 分別必須以 `reached_since`／
  `reached_since`／`reached_end` 結束。`verify()`＝`require_clean_history` → `FeatureFrame` → `first_measurable_index ≤
  warmup_bars`（否則句子裡告訴你要把 `warmup_bars` 提到幾） → `require_measurable_tail` → `Split.by_shares` 三段長度加總
  ＝`count`。split 從 manifest 切而不用 `plan_experiment`，因為後者會把 train 起點往前挪到第一根可量測的 bar。
- **暖身要多長**：由 `verify()` 量，不是推導；`sma_1d_200` 要有 ≥ 180 個日收盤（覆蓋率 0.9）才有值，所以 warm-up 至少
  約 185 天（≈1,110 根）；範例 manifest 用 200 天（1,200 根）。

```
python -m contrib.discovery generate --manifest dev-seed-0.json --root data/discovery
```

成功時 `<root>/<world id>-<manifest digest>/` 有 `world.sqlite`（唯讀 artifact，各臂複製它）與 `manifest.json`（canonical
bytes）。落地先在同一個 root 下的 staging 目錄做（`.<name>.partial-*`），驗收通過、manifest 寫好才 rename 到定位；任何原因
中途結束都把 staging 整個移除，最終路徑永遠不會出現半成品。同一份 manifest 已落地就拒絕（exit 1）。stdout 印三條 walk 的
結果、量到的第一根可量測 bar、三段 split 與各自的 bar 數；這些只給操作者看，不進任何 prompt。

## 測試釘住的隔離規則

| 規則 | 測試 |
|---|---|
| 只 import `autoresearch`＋`core`；src 與 tests 都不碰其他套件；反向邊不存在 | `tests/test_upstream.py`、`contrib/tests/test_package_graph.py` |
| Oracle 路徑前後 arm 的 sqlite 位元組相同（對照組：`measure()` 會改） | `tests/test_isolation.py` (a) |
| 同一個 coin 名、每臂一顆 sqlite：rules_tried／holdout pin／penalty pin 互不影響 | `tests/test_isolation.py` (b) |
| system＋user prompt 含 opaque id、不含 `COND`（精確）／null／planted／世界類型值；manifest 本身沒有任何路徑進 prompt | `tests/test_isolation.py` (c) |
| 最終判定與 promote 前的 `ledger.verdict` 一致；promote 後從快照重建不變；零 trial 回空集合 | `tests/test_verdict.py` |
| 同 seed bit-exact 重現；短世界是長世界的 prefix；funding 不讀價格；Null-v1 公式對得上亂數流；candle 形狀（close＝next open − 1 ms、OHLC、open＝前 close）；1d 由同一路徑聚合；funding 格線無洞無重複；效果＝null 雙胞胎＋β、從決策下一根起 | `tests/test_worlds.py` |
| store 的三條序列逐筆等於生成結果；`first_measurable_index ≤ warmup`；tail 通過；三段長度＝manifest；walk 沒走到頭拒絕 | `tests/test_landing.py` |
| `generate` 寫出 canonical manifest、第二次拒絕、失敗不留半成品 | `tests/test_cli.py` |

## Manifest 格式（v1）

示意（空字串與 `{}` 是佔位，不是可載入的值）：

```json
{
  "manifest_version": 1,
  "world": {
    "id": "SYN-EB3BC030",
    "kind": "null-v1 | conditional-v1",
    "seed_set": "dev | test", "world_seed": 0, "generator_version": "1",
    "origin_ms": 1682640000000, "initial_price": 30000.0,
    "bars": {"interval": "4h", "count": 400, "warmup_bars": 1200},
    "price": {"vol": 0.01, "drift": 0.0},
    "funding": {"sigma": 0.0001},
    "planted": {"spec": {}, "beta": 0.001, "difficulty": "tight | easy | trivial"}
  },
  "split": {"train_share": 0.6, "validation_share": 0.2},
  "indicator_lookback": 200,
  "costs": {}, "penalty": {"sharpe_base": 1.0, "k": 0.25},
  "budget": {"max_trials": 10, "oracle_margin": 0.25},
  "search_space": {
    "templates": [{"side": "long", "entry": ["funding_zscore_14", "<"], "gate": ["realized_vol_20", ">"], "exit": {"max_bars": 12}}],
    "threshold_grid": {"funding_zscore_14": [-2.0, -1.0], "realized_vol_20": [0.002, 0.004]},
    "candidate_order": "...", "pool_size": null
  },
  "arms": {"random": {"search_seeds": [0, 1]}, "llm": {"provider": "", "model": "", "temperature": 0.0, "replicates": 1, "world_subset": ["SYN-EB3BC030"]}},
  "scoring": {"version": "1", "recovery": {"theta_behavior": 0.8, "require_positive_holdout": true}},
  "null_definition_version": "v1"
}
```

與計畫 §5 的差異：`seed_set` 放在 `world` 裡（id 是 seed_set＋world_seed 的函數，`World.id` 是衍生屬性，讀檔時寫的 id
跟算出來的不符就拒絕）；Grid 臂沒有自己的區塊（它只走 `search_space` 的 `candidate_order`）；`arms.llm` 可為 `null`
（Null 校準先只跑 Random＋Grid）。`costs` 是 `CostModel.to_dict()`、`penalty` 是 `Penalty.to_dict()`，不另造型別。`planted` 在 null 世界
為 `null`；conditional 世界的 `planted.spec` 必須通過 autoresearch 的 parser，每個進場側都要有 exit（exit 規則或 `max_bars`），
`beta` 嚴格為正、方向＝「有利於埋的那一側的部位」（short 側的埋法＝負的價格漂移）。`origin_ms` 必須落在 UTC 午夜（同時在 4h 與
1d 格線上）。`indicator_lookback` 的下限＝引擎的 `MIN_INDICATOR_LOOKBACK`。`pool_size` 在枚舉前是 `null`，枚舉後 ≥ 1。

v1 候選池的兩個限制（寫在這裡，因為它們定義了 full-pool reference 的假說類）：每個 template 的 exit 只有 `max_bars`（LLM 臂可提
exit 規則，池不行）；每個 template 都有 gate（池裡沒有「只有進場」的候選）。`arms.llm.world_subset` 不檢查是否含本世界，PR 5 的
runner 遇到「有 llm 區塊但本世界不在子集」應拒絕。

## 最終判定（verdict.py）

- 時間點：搜尋結束、晉升之前。`snapshot_arm` 若發現任何 trial 已 promoted，拒絕。
- `ended` 四種：`budget_spent`（`report.stopped` 為 None）、`interrupted`、`exhausted`（seam error 且 hypothesist 的
  `exhausted` 旗標為真）、`seam_failed`（seam error 且旗標為假）。旗標為真但預算用盡＝矛盾，拒絕。
- `answers`＝ledger 的 proposals 數（拒絕與重複都算）；`distinct_rules`＝`rules_tried(coin)`；兩個數分開報。
- `final_eligible`＝以 `distinct_rules` 重判仍過門檻的 trial，按 validation net sharpe 降冪、trial_id 升冪排；`selected`＝
  第一個；零 trial 時為空、不呼叫 `threshold(0)`。
- `first_eligible_round`：診斷用（當時過門檻的第一輪），不是成功。

## 檔案配置（計畫 §2）

```
<bench-root>/<world-id>-<manifest-digest>/
  manifest.json              世界與 benchmark 參數（generate 寫，canonical）
  world.sqlite               生成器落地的 candles／funding（generate 寫；唯讀 artifact，各臂複製它）
  oracle.json                各臂跑完後才寫（PR 3）
  arms/{random,grid,llm}/replicate-<k>.sqlite   （PR 3／PR 5）
  verdicts.json              每臂每 replicate 的快照與最終判定（PR 3）
  scores.json                （PR 4）
```

目錄用 **manifest 的 digest**（`sha256(manifest.dumps())` 前 8 碼）而不只用 world id 命名：id 只是 seed_set＋seed 的函數
（計畫 §1 #7），同 seed 的 null 雙胞胎、或校準時同 seed 三檔 β 的三份 manifest 共用一個 id，要能並存；目錄名不進任何
prompt，store 裡的 coin 名仍是 id。與計畫 §2 寫的 `<world-id>/` 不同，記在計畫 §9。預設 `<bench-root>`＝
`<repo>/data/discovery`（`.gitignore` 已排除）。

## 不做清單

- 不改 autoresearch 的 DSL、評估器、ledger、penalty 公式。
- 不 import `contrib.replay`、`contrib.hyperliquid_perp`，不碰 paper／live 的任何 store。
- 不做 Null-v2／regime shift／價格衍生特徵的 planted 規則（第二階段）。
- 不做「首次成功就停止」協議。
- 不為 benchmark 加 OR、算術、新 feature。
