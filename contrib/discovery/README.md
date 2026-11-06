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
backfill 落地＋驗收）、`generate` 指令。
**PR 3（Random／Grid 臂＋oracle 判定，已落地）**：`hypothesists.py`（候選池與兩臂）、`bench.py`（一個世界跑到判定）、
`oracle.py` 的餘裕判定與 full-pool reference、`search`／`oracle`／`calibrate` 指令、manifest v2。評分器（PR 4）、LLM 臂（PR 5）、
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
- [`oracle.py`](./oracle.py)：`frame_for`／`evaluate_unfiled`，oracle 走的讀取路徑：`evaluate_split` 不落 ledger（計畫 §1
  #4／#5），回整個 `SplitResult`（評分器要讀 trades），holdout 那條跟 `promote` 一樣先過 `require_measurable_tail`；兩個讀數
  都可以吃一個已建好的 `frame`（一次 indicator walk 兩邊用）；`planted_figures()`＝埋的規則當成「預算用盡時的 trial」過
  autoresearch 的閘門（`verdict.gate_at`：`threshold(max_trials)`、餘裕 ≥ `oracle_margin` 才算 oracle 有餘裕，計畫 §4.4）；
  `pool_reference()`＝整池每個候選的 validation net sharpe（一個 frame 算完，不計預算、不落 ledger）；`Oracle`＝`oracle.json`
  的紀錄。什麼時候可以讀 holdout 是 protocol（`bench.py`），不是這些函式管的。
- [`hypothesists.py`](./hypothesists.py)：`RandomHypothesist`／`GridHypothesist` 實作 autoresearch 的 `Hypothesist` port、
  **不讀 prompt**；池走完 raise `CandidatesExhausted`（`HypothesistError` 的子類）並自標 `exhausted`。候選池本身由 manifest 寫出
  （`SearchSpace.candidates`，讀 manifest 時就枚舉、parser 拒絕就 manifest 拒絕）；定義見「三臂與判定」一節。
- [`bench.py`](./bench.py)：`run_world(run_dir)` 把一個落地世界按計畫 §3 的順序跑到底；`preview_oracle(run_dir)`＝搜尋前的
  oracle 讀數（只准 dev seed）；`calibration_table(run_dirs)`＝誤判率表；`Verdicts`／`ArmResult`＝`verdicts.json` 的紀錄。
- [`records.py`](./records.py)：各份 JSON 紀錄（manifest、verdict 快照、`verdicts.json`、`oracle.json`）共用的嚴格讀取器
  （`Reader`，各模組帶自己的 error class；三個紀錄錯誤共用 `RecordError` 基底、`Reader.version` 統一版本檢查）；`from_dict` 管
  鍵集合與只有原始文件才能做的檢查（版本、寫出的 id／hash 對衍生值、parser），每個欄位自己的值一律在 `__post_init__`
  驗（與 autoresearch 的 `Penalty.from_dict` 同款）；重複的 JSON key 拒絕。
- [`worlds.py`](./worlds.py)：`generate(manifest)` 把一個 seed 變成一份 `WorldHistory`（4h bars、1d 聚合、hourly funding、
  每根 bar 帶的埋入方向；吃整份 manifest 是因為 `indicator_lookback` 也決定 conditional 世界的持倉 bar）；
  `SyntheticHistory` 把它包成 autoresearch 的 `HistoryMarketData` port。定義見下一節。
- [`landing.py`](./landing.py)：`land()` 走 autoresearch 自己的 `backfill_candles`／`backfill_funding` 把世界落進一顆
  research store（三條 walk 都要走到頭，否則拒絕）；`verify()` 跑 PR 2 的驗收清單並回 `Landing`；`split_of()`＝manifest
  的 `count` 根決策 bar 按 shares 切（`Split.by_shares`），`experiment_for()` 把 manifest 的條件綁成各臂 ledger 要建的
  `Experiment`；`world_dir()`／`land_world()`＝run 目錄的配置與分段落地（見「檔案配置」）。
- [`cli.py`](./cli.py)：`python -m contrib.discovery generate --manifest <path> [--root <dir>]`、`oracle --run <dir>`、
  `search --run <dir>`、`calibrate [--root <dir>]`。

## 世界生成器（generator v1）

一個世界由 manifest 決定（`world` 區塊，加上 conditional 世界才讀的 `indicator_lookback`）；`generator_version` 不是 `"1"`、
`price.drift` 不是 0（有價格漂移的世界是另一種世界，不是這種世界的參數）、`bars.interval` 不是 4h、最後一筆 funding 戳記（加上
autoresearch funding walk 多問的 20 天）超出 venue 時間戳上限的 manifest 直接拒絕；價格路徑衝出 float 範圍也拒絕。帶 `arms.llm`
的 manifest 也在 `generate` 就拒絕（這一版跑不了那個臂，落地只是把拒絕延到 `search`；LLM 臂是 PR 5）。

- **亂數流**：三條（`price`／`wicks`／`funding`），都從不透明 id 派生（`random.Random(f"{id}/{name}")`），**kind 不進種子**，
  所以同 seed 的 Null 與 Conditional 共用同一組抽樣、只差埋入效果。每根 bar 的抽樣按 bar 順序消耗，短世界是長世界的
  prefix＝生成器內無前視。
- **Null-v1**：`log P[t] = log P[t-1] + vol·Z[t] − vol²/2`，`Z` i.i.d. N(0,1)，`vol` 以一根 4h bar 為單位；`−vol²/2`
  讓下一根收盤的條件期望等於現價（martingale）。每根 bar 的 open＝前一根 close（連續路徑、無跳空）；high／low＝bar 內
  Brownian 路徑的最高／最低，給定兩端精確抽樣（bridge 的 overshoot：`m = (move + sqrt(move² − 2·vol²·ln U))/2`，`U` 均勻於
  (0,1]，下方同法反射），所以 range 與 bar 的漲跌相關、像真實 bar；volume 常數 1。funding：`f[k] = sigma·W[k]`，`W` i.i.d.
  N(0,1)，每小時一筆、戳記在整點後 `SETTLEMENT_LAG_MS`（50 ms，與 venue 的貼出延遲同型），從第一根 open 到最後一根
  close 應結的那一筆（共 `4n+1` 筆，n＝暖身＋決策 bar 總數）。1d 由同一條 4h 路徑聚合（只出完整日）。venue 時鐘＝最後一筆 funding 貼出後 1 ms。
- **Conditional-v1**：同一組抽樣，再在「埋的規則持倉中的 bar」的 log 報酬加上 `beta`（short 側＝`−beta`）。哪些 bar 持倉由
  autoresearch 自己的 `replay_sides` 在**未加效果的路徑**（同 seed 的 null 世界）上判定：收盤決策、下一根開盤成交、
  `max_bars`／exit 規則／反手；效果從成交那根起算，**決策當根不加**。價格衍生的 gate（`realized_vol_*`、`regime`）在最終
  路徑上可能在門檻邊緣差幾根，這是 v1 的定義（單趟、不做 fixed point）；oracle 量的是最終路徑，β 的校準以它為準。
- **價格的寫法**：float 路徑以 `repr` 轉 Decimal（最短可逆串），不貼 tick；同一個 float 同一個 Decimal，所以 open＝前一根
  close 在 store 裡也精確相等。
- **落地**：`land()` 用 `SyntheticHistory` 當 venue、以世界自己的時鐘切視窗，三條 walk 分別必須以 `reached_since`／
  `reached_since`／`reached_end` 結束，而且 store 的列數要等於世界的列數（走到頭但掉頁也拒絕）。`verify()`＝store 的
  bar 數與 origin 對得上 manifest → `require_clean_history`（autoresearch 只數 funding 的洞，這個世界沒有洞，所以數到
  就拒絕）→ `FeatureFrame` → `first_measurable_index ≤ warmup_bars`（否則句子裡告訴你要把 `warmup_bars` 提到幾）→
  `require_measurable_tail` → `Split.by_shares` 切三段（構造上加總＝`count`）。split 從 manifest 切而不用 `plan_experiment`，因為後者會把 train 起點往前挪到第一根可量測的 bar。
- **暖身要多長**：由 `verify()` 量，不是推導；`sma_1d_200` 要有 ≥ 180 個日收盤（覆蓋率 0.9）才有值，所以 warm-up 至少
  約 185 天（≈1,110 根）；範例 manifest 用 200 天（1,200 根）。

```
python -m contrib.discovery generate --manifest dev-seed-0.json --root data/discovery
```

成功時 `<root>/<world id>-<manifest digest>/` 有 `world.sqlite`（唯讀 artifact，各臂複製它）與 `manifest.json`（canonical
bytes，`search_space.pool_size` 已回填；digest 把它置空算，所以目錄名與手寫的 manifest 一致）。落地先在同一個 root 下的 staging 目錄做（`.<name>.partial-*`），驗收通過、manifest 寫好才 rename 到定位；任何原因
中途結束都把 staging 整個移除，最終路徑永遠不會出現半成品。同一份 manifest 已落地就拒絕（exit 1）。stdout 印三條 walk 的
結果、量到的第一根可量測 bar、三段 split 與各自的 bar 數、conditional 世界還有埋的規則在決策 bar 上持倉的根數與比例
（規則在決策 bar 上從不持倉的 conditional 世界在生成時就拒絕）、池的大小（去重後／枚舉數）與 manifest 路徑；這些只給操作者看，
不進任何 prompt。

## 三臂與判定（PR 3）

**候選池**（`manifest.SearchSpace.candidates`，讀 manifest 時寫出；計畫 §4.3）：`search_space.templates` 逐一 × 各自 entry／gate
特徵的 `threshold_grid`。
每個候選寫成語言裡的一條規則：entry 子句在 `entry.<side>`、gate 子句一律在 `filters`（計畫 §1 #10 的正規位置）、`exit.max_bars`＝
template 的 hold、sizing 固定（`POOL_SIZING`＝`fixed_margin_fraction 0.5`，Sharpe 不隨固定比例變；改它＝新的池）、family＝
特徵掙來的標籤（讀 funding 序列→`funding_filter`、讀 regime→`regime_filter`、其餘 `breakout`；`spec_hash` 對 family 盲）。每個
候選都過 autoresearch 自己的 parser，被拒就是 manifest 被拒（句子點名 template 與 parser 的理由；`generate` 在建世界前就先枚舉
一次，所以池寫不出來的 manifest 什麼都不會落地）。`spec_hash` 相同的候選只留
第一個，去重後按 `candidate_order` 從 0 編號，`pool_size`＝去重後的數。`candidate_order` 只有一種：`templates_then_grid`＝
template 依 manifest 順序，template 內 entry 門檻升冪（外層）× gate 門檻升冪（內層）；標籤格線照列出的順序。哪條格線用標籤由
詞彙表的 unit 決定：categorical 的特徵（目前只有 `regime`）格線是標籤、其餘是數字，讀 manifest 時就擋（數值特徵給字串會被
parser 讀成 feature 名、regime 給數字會被 parser 拒絕，兩種都在格線這層先說清楚）。v1 池的兩個限制：exit 只有 `max_bars`、每個候選都有 gate。

**兩臂**：都實作 autoresearch 的 `Hypothesist` port、都**不讀 prompt**（餵給它們的已試規則與失敗回饋被忽略——Random 是零資訊
基準、Grid 是固定順序基準，LLM 臂贏過它們的部分才是「讀回饋」買到的）。`RandomHypothesist(pool, world_id=…, search_seed=…)`
的排列由 `random.Random(f"{world_id}/random/{seed}")` 抽（同一個 search seed 在不同世界不是同一個排列；kind 不進 id），
不放回；`GridHypothesist(pool)` 按編號走。池走完就 raise `CandidatesExhausted`（`HypothesistError` 的子類：`search()` 不扣預算、
停下、回報告）並把自己的 `exhausted` 旗標設 True，`verdict.classify_ending` 靠它把 `exhausted` 與 `seam_failed` 分開。

**`search --run <dir>`**（`bench.run_world`，計畫 §3 步驟 2–5，順序就是重點）：
1. 讀落地的 `manifest.json`（必須是這個目錄自己的：目錄名＝manifest 的 digest，落地後手改過 kind／penalty／budget 的
   manifest 會指到別的目錄名，拒絕）；只有 `arms/`＝另一個搜尋正在跑或沒跑完，拒絕（句子只說「等沒有搜尋在跑再手動移除」，
   不叫人刪活的 claim）；有紀錄＝這個世界搜過了，拒絕（一個世界只搜一次，要重搜就手動移除）；`arms.llm` 有填＝這一版跑不了 manifest 指名的臂，拒絕（`generate` 也拒絕，所以這種目錄只會
   是別的版本落的；要重跑就從 `llm: null` 的 manifest 重新落地——就地改 manifest 會改掉 digest、對不上目錄名；LLM 臂是 PR 5）。
   池與 `pool_size` 在讀 manifest 時就已核對（見「候選池」）。
2. 每臂把 `world.sqlite` 複製到自己的 store（`arms/random/replicate-<search seed>.sqlite`、`arms/grid/replicate-0.sqlite`），
   第一顆先跑 `verify()`（落地時的驗收再跑一次：artifact 真的是 manifest 描述的世界）；每臂以 `experiment_for()`＋
   `ledger.create_experiment()` 開自己的 experiment（id＝`random-<seed>`／`grid-0`）——同 coin、同 split／costs／window／
   penalty，`Verdicts` 的建構子守住這件事（也守住：必有 `grid-0`、各臂 distinct rules＝自己的 trials 數且不超過
   `pool_size`、答案數不超過 `pool_size`、`exhausted` 的答案數＝`pool_size`）；grid 只有 replicate 0、`interrupted` 的快照不收，
   由 `ArmResult` 守。
3. 每臂跑 autoresearch 的 `search(ledger, experiment, hypothesist, max_trials=budget)`；跑完立刻 `snapshot_arm`（晉升前）→
   `final_verdict`；`selected` 非空就用 autoresearch 的 `promote()` 晉升那一筆（holdout 數字由它讀）。中斷＝整個 run 丟掉
   （replicate 不 resume，計畫 §9 (14)）、staging 移除、exit 130；seam 失敗記成 `seam_failed`、下一臂照跑。
4. 所有臂跑完才讀 oracle（計畫 §4.4）：在 grid 臂的 store 上算埋的規則的 train／validation／holdout（null 世界為 null）與
   full-pool reference（整池 validation net sharpe、最佳者、過 `threshold(pool_size)` 的個數），不落 ledger。
5. 全部先寫進 run 目錄下的 staging（`.search.partial-*`），最後先搬 `verdicts.json`／`oracle.json`，再把各臂 store 逐一搬進
   開頭就建好的 `arms/`（claim）；`manifest.json` 不動（`pool_size` 由 `generate` 回填）。紀錄搬好之前任何原因中途結束都把
   staging 與 claim 移除，目錄回到落地狀態——只有停在兩次搬檔之間會多留一個 `verdicts.json`，guard 與 calibrate 都會點名拒絕；
   紀錄搬好之後搬 store 中途結束，**staging 保留**、不丟資料：搬檔失敗（例如 Windows 防毒鎖檔）時拒絕句子點名哪些臂已進
   `arms/`、其餘 store 在哪個 staging 路徑，手動搬完再刪 staging；剛好 Ctrl-C 時只印「interrupted」、staging 一樣保留。
   收尾看的是目錄本身（`oracle.json` 在不在、staging 裡還有沒有 store），不是程式跑到哪一行，所以中斷落在兩行之間也一樣。
   之後再 `search` 時 guard 會把保留的 staging 列出來；只有 `arms/` 的目錄（搜尋進行中、或被直接殺掉）拒絕句子也會列出
   staging，免得只刪 `arms/` 留下整份 store 副本。test seed 的世界不把 holdout 數字印到 console（`verdicts.json`／`oracle.json` 照寫；計畫 §4.2
   測試世界的 holdout 不得拿來調任何東西）。

**`oracle --run <dir>`**（`bench.preview_oracle`）：搜尋前先看埋的規則的 train／validation 對 `threshold(max_trials)` 的餘裕，
在拋棄式副本上算、不留檔。**只准 dev seed**：test seed 的 oracle 數字由 `search` 在各臂之後寫進 `oracle.json`（計畫 §4.2／§4.4）；
null 世界沒有東西可看。holdout 在任何 seed 上都不在這裡讀。

**`calibrate [--root <dir>]`**（`bench.calibration_table`）：走 root 下每個搜過的 run 目錄，按（world kind、
`null_definition_version`、seed set、**條件 digest**、arm）列：搜尋次數、`final_eligible` 非空的次數、比例。條件 digest＝把
`world_seed`（與由它衍生的 id）置空後的 manifest digest（`landing.conditions_digest`），印在每一列的方括號裡：同一組條件的
多個 seed 合成一列，β 檔、penalty、預算、bars、seed set 任何一個不同就是另一列——同一個 root 放幾組校準都不會被合成一個比率。null 世界那行叫「false calls」（報表措辭
照計畫 §4.1：Null-v1 定義下的誤判率）；conditional 世界那行叫「non-empty verdicts」——找到的規則是不是埋的那條是 PR 4 評分器
的事。`seam_failed` 的臂沒有搜尋過：不進分母，另計「n seam failure(s) not counted」印在同一行（它已 file 的判定也一併
擱置：被切短的搜尋不是預算買到的那次）。沒搜過的目錄跳過；`verdicts.json` 的 `world_id`／`pool_size`／臂集合／`manifest_digest`
對不上旁邊 manifest（id、`len(candidates)`、`arms`、目錄的 digest）就拒絕；`oracle.json` 也一起讀：world_id／digest 要跟
verdicts 一致、`planted` 有無要對得上世界的 kind、`pool.size` 要等於 `len(candidates)`、門檻（`budget`／`oracle_margin`／
`threshold`、池的 `threshold`）要等於 manifest 的 budget 與 penalty 算出的值；`verdicts.json` 裡各臂 experiment 的條件
（split／costs／window／penalty）要等於 `experiment_for(manifest)`、`budget_spent` 的答案數＝預算、任何臂（含 `seam_failed`）
的答案數不超過預算、每筆 trial 都是池裡的規則；`oracle.json` 埋的規則三段數字的視窗要等於 manifest 的 split
（`exhausted` 的答案數＝池大小是紀錄本身的閘門、每次讀都檢查）；同一個目錄被走到兩次（root 裡有連到它的連結）只算一次；壞掉的紀錄（含 manifest）拒絕句子帶檔案路徑；兩份紀錄沒有都在的目錄（只有 `arms/`＝搜尋進行中或沒跑完、
只有 `verdicts.json`＝停在兩次搬檔之間）拒絕而不是跳過，句子只說「等沒有搜尋在跑再手動移除」；帶 `llm` 的 manifest 整個 calibrate
拒絕；不要求 `world.sqlite`（artifact 可裁掉省空間）。

**兩份紀錄**：`verdicts.json`＝`{verdicts_version, world_id, pool_size, manifest_digest, results: [{arm, replicate, snapshot,
verdict, holdout}]}`（`manifest_digest`＝跑的時候 run 目錄的 digest，把紀錄綁在它的條件上、不只綁 world id——同 seed 的 null
雙胞胎 id 相同、池相同，只差 digest），
`verdict` 寫在 `snapshot` 旁邊給人讀，讀回時重算 `final_verdict(snapshot)` 不相等就拒絕（紀錄永遠可由快照重判，計畫 §4.5）；
`holdout` 有值 ⇔ `selected` 非空；grid 只有 replicate 0、必有 `grid-0`；`interrupted` 的快照不收；各臂 distinct rules＝trials 數且
不超過 `pool_size`、答案數不超過 `pool_size`、`exhausted` 的答案數＝`pool_size`。`oracle.json`＝
`{oracle_version, world_id, manifest_digest, planted: {train, validation, holdout, budget, threshold, oracle_margin, blockers[],
eligible, margin, passed} | null, pool: {size, validation_sharpes[], best, threshold, clearing}}`：`blockers` 是 autoresearch
閘門在 `threshold(max_trials)` 的句子（空＝過），而且句數要等於旁邊 validation 數字失敗的判斷式數（ruined／零交易／淨報酬不正／
sharpe 低於門檻），所以手改 blockers 寫不出「有餘裕」；`eligible`／`margin`／`passed`／`best`／`size` 是衍生值，讀回對不上就
拒絕；三段數字各自要掛在自己的視窗名下；`planted` 有值時一定帶 holdout（紀錄是搜尋之後寫的）；`clearing` 不能超過
validation sharpe ≥ threshold 的個數。

**2026-11-06 範例世界（dev seed 0、400 根決策 bar）的煙霧讀數，給校準參考**：埋的規則 train net sharpe 5.31、validation −4.36
→ oracle 無餘裕；同 seed 的 Null 世界整池 16 個候選有 8 個過 `threshold(16)`＝1.69、最佳 validation sharpe 9.3，Random／Grid
預算 3 都「找到」＝誤判率 100%。80 根 4h 的 validation 視窗太短、年化 Sharpe 的變異太大：dev seeds 校準（計畫 §8 #1／#2）
要先拉長 `bars.count` 或抬 penalty，範例 manifest 只是單元測試的夾具，不是校準過的世界。

```
python -m contrib.discovery oracle --run data/discovery/SYN-EB3BC030-c5f56953
python -m contrib.discovery search --run data/discovery/SYN-EB3BC030-c5f56953
python -m contrib.discovery calibrate --root data/discovery
```

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
| `generate` 寫出 canonical manifest、第二次拒絕、失敗不留半成品；`search`／`oracle`／`calibrate` 的輸出與各錯誤家族的 exit code | `tests/test_cli.py` |
| 池＝templates × grids 按 `candidate_order`、去重後編號、每個候選寫法正規（gate 在 `filters`、`max_bars`、`POOL_SIZING`、family）、parser 拒絕就 manifest 拒絕；Random 是 seed＋world 決定的排列、不放回；Grid 按編號；池走完 raise `CandidatesExhausted`＋旗標；經真的 `search()` 每個候選只 file 一次、以 `exhausted` 結束 | `tests/test_hypothesists.py` |
| 一次真的 `search`：`arms/` 兩顆 store、`verdicts.json`／`oracle.json` 讀回相等、`manifest.json` 不動且目錄名不變、artifact 位元組不變；各臂同條件、各自 store 裡只晉升 selected；oracle 在各臂之後印、holdout 有值、整池 16 個；再搜拒絕；帶 `llm` 的 manifest 拒絕；落地後改過的 manifest（digest 對不上目錄名）拒絕；calibrate 表（一列＝一組條件〔條件 digest〕、seam 失敗不進分母、artifact 裁掉仍可算、同一目錄只算一次、world_id／pool_size／臂集合不符與
null 雙胞胎 digest 拒絕、紀錄對 manifest 的條件／預算／池／oracle 門檻與視窗、帶 `llm` 拒絕）；搜尋進行中（只有 `arms/`）拒絕且點名 staging；
staging 開不起來時 claim 歸還；搬 store 失敗或中斷時保留 staging 並點名；紀錄閘門（verdict 對不上快照、selected 非首位、holdout 段位、`eligible`／`margin`／`passed`／段名／`clearing`、`best`／`size`、版本、grid replicate、trials ≤ pool_size、row 的計數）；holdout 旗標可關；dev-only／null 的 oracle 預覽拒絕；中斷＝目錄回到落地狀態；seam 失敗記錄後下一臂照跑 | `tests/test_bench.py` |

## Manifest 格式（v2）

示意（空字串與 `{}` 是佔位，不是可載入的值）：

```json
{
  "manifest_version": 2,
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
    "candidate_order": "templates_then_grid", "pool_size": null
  },
  "arms": {"random": {"search_seeds": [0, 1]}, "llm": null},
  "scoring": {"version": "1", "recovery": {"theta_behavior": 0.8, "require_positive_holdout": true}},
  "null_definition_version": "v1"
}
```

與計畫 §5 的差異：`seed_set` 放在 `world` 裡（id 是 seed_set＋world_seed 的函數，`World.id` 是衍生屬性，讀檔時寫的 id
跟算出來的不符就拒絕）；Grid 臂沒有自己的區塊（它只走 `search_space` 的 `candidate_order`）；`arms.llm` 可為 `null`
（Null 校準先只跑 Random＋Grid；**這一版的 `search` 要 `llm: null`**，填了的形式 `{"provider", "model", "temperature",
"replicates", "world_subset"}` 是 PR 5 的 LLM 臂讀的，現在填了會被 `search` 拒絕）。`costs` 是 `CostModel.to_dict()`、`penalty` 是 `Penalty.to_dict()`，不另造型別。`planted` 在 null 世界
為 `null`；conditional 世界的 `planted.spec` 必須通過 autoresearch 的 parser，每個進場側都要有 exit（exit 規則或 `max_bars`），
`beta` 嚴格為正、方向＝「有利於埋的那一側的部位」（short 側的埋法＝負的價格漂移）。`origin_ms` 必須 ≥ 1 且落在 UTC 午夜（同時在 4h 與
1d 格線上）。`indicator_lookback` 的下限＝引擎的 `MIN_INDICATOR_LOOKBACK`。`pool_size` 手寫時留 `null`，`generate` 落地時
回填成這一版枚舉出的數；已填的值不等於枚舉數就拒絕。`candidate_order` 是 enum（v2 起；v1 的自由文字沒有指名 runner 走得出來的順序，所以版本號 bump、v1 檔拒絕），
目前只有 `templates_then_grid`（定義見「三臂與判定」）。

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
  manifest.json              世界與 benchmark 參數（generate 寫，canonical，search_space.pool_size 已回填；之後不再改寫）
  world.sqlite               生成器落地的 candles／funding（generate 寫；唯讀 artifact，各臂複製它）
  arms/random/replicate-<search seed>.sqlite   每個 Random replicate 自己的 store（search 寫；k＝search seed）
  arms/grid/replicate-0.sqlite                 Grid 臂的 store（確定性，只有一個 replicate）
  arms/llm/replicate-<k>.sqlite                （PR 5）
  verdicts.json              每臂每 replicate 的快照、最終判定、selected 的 holdout（search 寫）
  oracle.json                各臂跑完後才寫：埋的規則三段數字＋餘裕判定、full-pool reference（search 寫）
  scores.json                （PR 4）
```

目錄用 **manifest 的 digest**（`search_space.pool_size` 置空後的 `sha256(manifest.dumps())` 前 8 碼——`generate` 回填池大小
後目錄名與手寫的 manifest 一致；`search`／`oracle`／`calibrate` 都核對目錄名＝旁邊 manifest 的 digest，落地後手改過的
manifest 會被拒；`verdicts.json`／`oracle.json` 也寫入跑的時候的 digest，calibrate 一併核對，所以改 manifest 又把目錄改名
也繞不過）而不只用 world id 命名：id 只是 seed_set＋seed 的函數
（計畫 §1 #7），同 seed 的 null 雙胞胎、或校準時同 seed 三檔 β 的三份 manifest 共用一個 id，要能並存；目錄名不進任何
prompt，store 裡的 coin 名仍是 id。與計畫 §2 寫的 `<world-id>/` 不同，記在計畫 §9。預設 `<bench-root>`＝
`<repo>/data/discovery`（`.gitignore` 已排除）。

## 不做清單

- 不改 autoresearch 的 DSL、評估器、ledger、penalty 公式。
- 不 import `contrib.replay`、`contrib.hyperliquid_perp`，不碰 paper／live 的任何 store。
- 不做 Null-v2／regime shift／價格衍生特徵的 planted 規則（第二階段）。
- 不做「首次成功就停止」協議。
- 不為 benchmark 加 OR、算術、新 feature。
