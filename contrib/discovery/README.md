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
`oracle.py` 的餘裕判定與 full-pool reference、`search`／`oracle`／`calibrate` 指令、manifest v2。
**PR 4（評分器，已落地）**：`scoring.py`（三項分報指標、`scores.json`、跨世界的表）、`score`／`report` 指令、manifest v3（artifact 的
SHA-256）、`oracle.json` v2（blockers 改 enum）。**PR 5（LLM 臂，已落地）**：`bench.py` 在 Random／Grid 之後跑 manifest 指名的模型；定義見「LLM 臂（PR 5）」一節。**PR 6（實驗 B：research 段有沒有進決策，已落地）**不在這個套件：它是
`contrib/replay` 的四個 `research-*` variant 與 `effect` 指令（[replay README](../replay/README.md) 的「多一段文字有沒有推動決策」一節），
兩個實驗只共用結論、不共用 import。
**工具組 T1（訓練區間查詢的純函式，已落地）**：`research_tools.py`＝會用工具的模型臂在提案前可以做的三種查詢。這一步只有純函式與它們的
JSON 層。定義見「訓練區間查詢（工具組 T1）」一節。
**工具組 T2（MCP 伺服器，已落地）**：`tools_server.py`＝把那三個查詢包成一個走 MCP 的 stdio 伺服器，另加 `tools` 指令讓人手動試玩。
定義見「MCP 伺服器（工具組 T2）」一節。
**工具組 T3（會用工具的提問器，已落地）**：`toolarm.py`＝`ToolHypothesist`，模型在回答規則之前可以先問那個伺服器；每輪上限、trace
都在這裡。定義見「會用工具的提問器（工具組 T3）」一節。
**工具組 T4（接進 benchmark，已落地）**：manifest v4 的 `arms.llm_tools`、`search` 多跑會用工具的模型臂、`tool_use.json`（每輪查了幾次與 token 數）、
`report` 多印查詢次數與估算花費。定義見「工具組接進 benchmark（工具組 T4）」一節。
**盲猜組的用量（#433，已落地）**：`search` 跑 `arms.llm` 時把每個 replicate 的模型呼叫次數與 token 數記進 `model_use.json`，
`report` 在那一列印跟工具組同一句 token 行。定義見同一節的「盲猜組花了什麼」。
**工具組 T5（不用模型的對照組，已落地）**：manifest v5 的 `arms.train_pick`、`hypothesists.py` 的 `TrainPickHypothesist`（每輪從候選池抽幾條、
都在訓練區間試跑、交訓練分數最好的那一條）、`search` 在 Grid 之後跑它。定義見「不用模型的對照組（工具組 T5）」一節。
**工具組 T6（固定摘要、不給工具的模型臂，程式已落地、還沒用真的模型跑過）**：manifest v6 的 `arms.llm_summary`、`summaryarm.py` 的
`SummaryHypothesist`（每次回答前把一份固定的訓練區間摘要接在 system prompt 後面，模型一次作答）。跑不跑要等工具組試跑後決定（只看花費，規則見「判定規則（2026-12 開發批次）」一節）；
定義先寫死。見「固定摘要組（工具組 T6）」一節。
**判定規則（2026-12 開發批次；2026-11-26 寫死、還沒開跑）**：跑哪些世界與臂、v2 世界的 `beta`／`price.drift`、試跑與凍結、主要與次要判定、
模型側失敗怎麼處理。只有文件，沒有程式在執行它。見「判定規則（2026-12 開發批次）」一節。

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
- [`hypothesists.py`](./hypothesists.py)：`RandomHypothesist`／`GridHypothesist`，以及工具組 T5 的對照組 `TrainPickHypothesist`，實作 autoresearch 的 `Hypothesist` port、
  **不讀 prompt**；池走完 raise `CandidatesExhausted`（`HypothesistError` 的子類）並自標 `exhausted`。候選池本身由 manifest 寫出
  （`SearchSpace.candidates`，讀 manifest 時就枚舉、parser 拒絕就 manifest 拒絕）；Random／Grid 的定義見「三臂與判定」一節，對照組的見「不用模型的對照組（工具組 T5）」一節。
- [`bench.py`](./bench.py)：`run_world(run_dir)` 把一個落地世界按計畫 §3 的順序跑到底；`preview_oracle(run_dir)`＝搜尋前的
  oracle 讀數（只准 dev seed）；`calibration_table(run_dirs)`＝誤判率表；`Verdicts`／`ArmResult`＝`verdicts.json` 的紀錄。
  工具組的每個 replicate 在這裡各開一次提問器（`tool_arm()` 是開跑前的檢查），伺服器側失敗時停下整個 run。
- [`tooluse.py`](./tooluse.py)：工具組的搜尋花了什麼。`read_trace`／`trace_use` 把一份 trace 讀回來整理成每輪的次數與 token 數，
  `ToolUse`＝`tool_use.json` 的紀錄，`usage_lines()`＝`report` 多印的那兩行，`Prices`＝估算花費用的單價。定義見「工具組接進 benchmark（工具組 T4）」一節。
- [`modeluse.py`](./modeluse.py)：不帶工具的模型臂——盲猜組（`arms.llm`）或摘要組（`arms.llm_summary`）——的搜尋花了什麼。`MeteredModel`＝包在模型 client 外面的計數器（每個 replicate 一個），
  `ModelUse`＝`model_use.json` 的紀錄，`model_use_lines()`＝`report` 在那一列多印的行。token 那一句與單價用的是 `tooluse.py` 的同一段程式。
- [`summaryarm.py`](./summaryarm.py)：摘要組的提問器。`summary(view)`＝接在 system prompt 後面的那一整段（開頭一段說明＋每個指標一行），
  `SummaryHypothesist`＝帶著那一段去問模型的提問器，`SUMMARY_VERSION`＋`wording_digest()`＝那一段文字的版本與雜湊。定義見「固定摘要組（工具組 T6）」一節。
- [`records.py`](./records.py)：各份 JSON 紀錄（manifest、verdict 快照、`verdicts.json`、`oracle.json`、`scores.json`、`tool_use.json`、`model_use.json`）共用的嚴格讀取器
  （`Reader`，各模組帶自己的 error class；manifest 以外的紀錄錯誤共用 `RecordError` 基底、`Reader.version` 統一版本檢查，
  manifest 同時讀兩個版本所以用 `Reader.one_version_of`）；`from_dict` 管
  鍵集合與只有原始文件才能做的檢查（版本、寫出的 id／hash 對衍生值、parser），每個欄位自己的值一律在 `__post_init__`
  驗（與 autoresearch 的 `Penalty.from_dict` 同款）；重複的 JSON key 拒絕。
- [`worlds.py`](./worlds.py)：`generate(manifest)` 把一個 seed 變成一份 `WorldHistory`（4h bars、1d 聚合、hourly funding、
  每根 bar 帶的埋入方向；吃整份 manifest 是因為 `indicator_lookback` 也決定 conditional 世界的持倉 bar）；
  `SyntheticHistory` 把它包成 autoresearch 的 `HistoryMarketData` port。定義見下一節。
- [`landing.py`](./landing.py)：`land()` 走 autoresearch 自己的 `backfill_candles`／`backfill_funding` 把世界落進一顆
  research store（三條 walk 都要走到頭，否則拒絕）；`verify()` 跑 PR 2 的驗收清單並回 `Landing`；`split_of()`＝manifest
  的 `count` 根決策 bar 按 shares 切（`Split.by_shares`），`experiment_for()` 把 manifest 的條件綁成各臂 ledger 要建的
  `Experiment`；`world_dir()`／`land_world()`＝run 目錄的配置與分段落地（見「檔案配置」）。
- [`scoring.py`](./scoring.py)：評分（計畫 §4.6）。`positions`／`direction_agreement`／`structural_distance` 是三個純函式，
  `score_world(run_dir)` 寫 `scores.json`，`report_table(run_dirs)` 是跨世界的表；定義見「評分（PR 4）」一節。
- [`research_tools.py`](./research_tools.py)：`TrainView`（只讀到訓練區間最後一根 bar 的 frame，加上 experiment 的 split 與成本）、三個查詢
  `feature_stats`／`conditional_returns`／`backtest_train`、JSON 層 `call(view, tool, arguments)`。不寫任何東西，不 import
  `manifest.py`。定義見「訓練區間查詢（工具組 T1）」一節。
- [`tools_server.py`](./tools_server.py)：那三個查詢的 MCP 伺服器。`ServerParams`（啟動時收的五樣東西與它們的命令列寫法）、
  `load_view`／`warm`（讀到訓練區間結尾、先把每個指標算一次）、`tool_specs()`（模型看到的說明與 schema）、`build_server`／`serve`／`main`。
  只 import `research_tools` 與 `upstream`；MCP SDK 是選用安裝，只在建伺服器時才 import。定義見「MCP 伺服器（工具組 T2）」一節。
- [`toolarm.py`](./toolarm.py)：會用工具的提問器。`ToolHypothesist.open(...)`（握著伺服器子程序、trace 檔與 MCP 客戶端的事件迴圈，
  為期一組搜尋）、`Limits`（每輪上限）、`Pack`／`PACK_VERSION`（模型多讀到的整包文字與它的版本）、`addendum()`（接在 system prompt
  後面的那段說明）、`read_reply`／`tool_message`（不 import langchain 的讀與寫）。只 import `research_tools`／`tools_server`／`upstream`。
  定義見「會用工具的提問器（工具組 T3）」一節。
- [`cli.py`](./cli.py)：`python -m contrib.discovery generate --manifest <path> [--root <dir>]`、`oracle --run <dir>`、
  `search --run <dir>`、`calibrate [--root <dir>]`、`score --run <dir>`、
  `report [--root <dir>] [--unseal] [--price-in <美元> --price-out <美元>]`、`tools --run <dir>`。

## 世界生成器（generator v1）

一個世界由 manifest 決定（`world` 區塊，加上 conditional 世界才讀的 `indicator_lookback`）；`generator_version` 不是 `"1"`、
`price.drift` 不合這個 kind 的規定（v1 的兩種世界必須是 0：有價格漂移的世界是另一種世界，不是這種世界的參數；`conditional-v2` 必須宣告一個逆著埋入方向、絕對值小於 `beta` 的漂移，見下）、`bars.interval` 不是 4h、最後一筆 funding 戳記（加上
autoresearch funding walk 多問的 20 天）超出 venue 時間戳上限的 manifest 直接拒絕；價格路徑衝出 float 範圍也拒絕。

- **亂數流**：三條（`price`／`wicks`／`funding`），都從不透明 id 派生（`random.Random(f"{id}/{name}")`），**kind 不進種子**，
  所以同 seed 的 Null 與 Conditional 共用同一組抽樣、只差埋入的東西（效果；`conditional-v2` 再加上宣告的漂移）。每根 bar 的抽樣按 bar 順序消耗，短世界是長世界的
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
- **Conditional-v2**：Conditional-v1 再加一個**宣告在 manifest 的常數** `price.drift`：每一根 bar（暖身也算）的 log 報酬都
  加上它，方向逆著埋的那一側（埋在多方＝負值）。為什麼需要：v1 的效果只有一個方向，整條價格因此帶著
  `beta ×（規則持倉的 bar 比例）` 的漂移，「每一根都站在埋入那一側」不必讀規則就賺得到。2026-11-11 在 dev seeds 0–9 的
  easy 世界（`beta` 0.004、模型臂 dev 那一批）上直接量：一直做多的 validation 淨 Sharpe 平均 3.58，10 個世界裡 7 個過了
  3.70 的晉升門檻；模型臂在那一批沒有找回任何一條埋的規則。把 `price.drift` 定成 `−beta × 持倉比例` 後整體漂移歸零：
  持倉的 bar 帶 `beta + drift`，其餘的 bar 帶 `drift`。
  - **宣告而不是由路徑算出**：用整個世界量出來的常數會讓每一根 bar 依賴它之後的 bar，「短世界是長世界的 prefix」就不
    成立；代價是抵銷只對用來定它的那個比例精確。每個世界自己的比例留下多少由 `generate` 印出來：`unconditional drift`
    那一行＝`WorldHistory.unconditional_drift`，決策 bar 合起來的平均每根 log 漂移（單一窗口的比例各自不同）與它佔一根 bar
    波動的比例，**以多方為正**（一直做多每根賺到的量，一直做空相反）；v1 的世界也印，那就是它的捷徑有多大
    （＝`WorldHistory.planted_drift`）。
  - **定法**：先讀每個 dev seed 的 **conditional-v1** 世界的 `planted rule: holds …` 比例（還沒宣告漂移的 v2 manifest 會被拒絕，所以用 v1 讀：落在用完即丟的 root，或在 Python 裡讀 `generate(manifest).planted_drift`，它就是 `beta ×` 比例、以多方為正），對 dev seeds 取平均、乘上 `−beta`（用 `planted_drift` 的話＝取平均後變號）；v2 世界自己的比例因為規則改在帶漂移的路徑上判定，可能差幾根；持倉 bar 的淨效果變成
    `beta + drift`，所以 `beta` 的難度檔要跟著重新校準。規則仍在未加效果的路徑上判定，這裡是「帶著宣告漂移、沒有效果」
    的那一條。2026-11-12 校準出來的三檔數字在「判定規則（2026-12 開發批次）」一節。
  - **拒絕**：規則兩邊都進場（一個漂移擋不了兩邊；兩邊都埋只能是 v1 世界，它留著兩邊相抵後的淨漂移，`generate` 照樣印出來）、漂移是 0 或與埋入同向（什麼都沒抵銷）、絕對值不小於 `beta`
    （持倉的 bar 不剩效果）；路徑鋪好、知道這個 seed 自己的持倉比例之後，再拒絕「留下的漂移不比埋入效果原本給的少」
    的宣告（＝漂移達到或超過埋入效果自己那份的兩倍：捷徑換到另一邊）。
  - **它沒有做到的事**：其餘的 bar 不是 martingale，每一根都帶 `drift`。所以「剛好在規則不持倉的 bar 上反向持倉」總共賺到
    的和埋的規則一樣多，只是攤在好幾倍的曝險上。那是埋入規則的鏡像，要讀懂規則才站得上去，不是繞過規則的路；但它
    在世界裡，而且沒有 null 世界照得出它：v2 世界的 null twin 仍是同 seed 的 Null-v1（沒有東西埋而帶漂移＝價格自己
    有優勢，是另一種世界）。2026-11-11 本機試算（未進 repo 的腳本；10 個 easy seeds、`beta` 0.004 不變、每個世界用
    自己的持倉比例定漂移）的 validation 淨 Sharpe 平均：一直做多 0.39、一直做空 −0.17、「波動率低於閘門就做空」0.15、
    「funding z 為正就做空」−2.71，沒有一個世界過 3.70；埋的那條 5.19、9/10 過。
  - **版本**：`generator_version` 仍是 `"1"`。多這個 kind 沒有讓任何既有 manifest 產生不同的世界，v1 的兩種世界照舊
    不准有漂移；分辨有沒有宣告漂移看 kind，不看版本。v2 的世界在 `calibrate`／`report` 自成一列（kind 是分列的鍵）。
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
bytes，`search_space.pool_size` 與 `artifact_sha256` 已回填；digest 把這兩個置空算，所以目錄名與手寫的 manifest 一致）。落地先在同一個 root 下的 staging 目錄做（`.<name>.partial-*`），驗收通過、manifest 寫好才 rename 到定位；任何原因
中途結束都把 staging 整個移除，最終路徑永遠不會出現半成品。同一份 manifest 已落地就拒絕（exit 1）。stdout 印三條 walk 的
結果、量到的第一根可量測 bar、三段 split 與各自的 bar 數、conditional 世界還有埋的規則在決策 bar 上持倉的根數與比例
（規則在決策 bar 上從不持倉的 conditional 世界在生成時就拒絕）與 `unconditional drift` 那一行（見上面 Conditional-v2 一段）、池的大小（去重後／枚舉數）與 manifest 路徑；這些只給操作者看，
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
   不叫人刪活的 claim）；有紀錄＝這個世界搜過了，拒絕（一個世界只搜一次，要重搜就手動移除）；有一個刻意保留的暫存資料夾
   （`.search.kept-*`，工具組那一側停下時留的，見「工具組接進 benchmark（工具組 T4）」）＝拒絕，不管 `arms/` 還在不在。
   池與 `pool_size` 在讀 manifest 時就已核對（見「候選池」）。
2. 先核對 `world.sqlite` 的 SHA-256 等於 manifest 的 `artifact_sha256`（對不上拒絕：同 seed 的 null 雙胞胎 artifact 複製錯目錄，形狀檢查全部會過）；每臂把 `world.sqlite` 複製到自己的 store（`arms/random/replicate-<search seed>.sqlite`、`arms/grid/replicate-0.sqlite`），
   第一顆先跑 `verify()`（落地時的驗收再跑一次：artifact 真的是 manifest 描述的世界）；每臂以 `experiment_for()`＋
   `ledger.create_experiment()` 開自己的 experiment（id＝`random-<seed>`／`grid-0`）——同 coin、同 split／costs／window／
   penalty，`Verdicts` 的建構子守住這件事（也守住：必有 `grid-0`、各臂 distinct rules＝自己的 trials 數；走池的臂（Random／Grid／對照組）另外不超過
   `pool_size`、答案數不超過 `pool_size`；Random／Grid 的 `exhausted` 答案數＝`pool_size`（對照組每輪可以抽好幾條，它的數另計，見「不用模型的對照組（工具組 T5）」）；模型臂不得 `exhausted`）；grid 只有 replicate 0、`interrupted` 的快照不收，
   由 `ArmResult` 守。
3. 每臂跑 autoresearch 的 `search(ledger, experiment, hypothesist, max_trials=budget)`；跑完立刻 `snapshot_arm`（晉升前）→
   `final_verdict`；`selected` 非空就用 autoresearch 的 `promote()` 晉升那一筆（holdout 數字由它讀）。中斷＝整個 run 丟掉
   （replicate 不 resume，計畫 §9 (14)）、staging 移除、exit 130；seam 失敗記成 `seam_failed`、下一臂照跑。
4. 所有臂跑完才讀 oracle（計畫 §4.4）：在 grid 臂的 store 上算埋的規則的 train／validation／holdout（null 世界為 null）與
   full-pool reference（整池 validation net sharpe、最佳者、過 `threshold(pool_size)` 的個數），不落 ledger。
5. 全部先寫進 run 目錄下的 staging（`.search.partial-*`），最後先搬 `verdicts.json`／`oracle.json`，再把各臂 store 逐一搬進
   開頭就建好的 `arms/`（claim）；`manifest.json` 不動（`pool_size` 由 `generate` 回填）。紀錄搬好之前中途結束都把
   staging 與 claim 移除，目錄回到落地狀態——只有兩個例外：停在搬檔之間會多留已經搬進去的紀錄（`verdicts.json`，模型臂還有 `tool_use.json` 或 `model_use.json`；
   `oracle.json` 一定最後搬），guard 與 calibrate 都會點名拒絕；工具組那一側停下時 staging 與 claim 刻意保留（見「工具組接進 benchmark（工具組 T4）」）；
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
的事。`seam_failed` 的臂沒有跑完搜尋：不進分母，另計「n seam failure(s) not counted」印在同一行（它已 file 的判定也一併
擱置：被切短的搜尋不是預算買到的那次）。沒搜過的目錄跳過；`verdicts.json` 的 `world_id`／`pool_size`／臂集合／`manifest_digest`
對不上旁邊 manifest（id、`len(candidates)`、`arms`、目錄的 digest）就拒絕；`oracle.json` 也一起讀：world_id／digest 要跟
verdicts 一致、`planted` 有無要對得上世界的 kind、`pool.size` 要等於 `len(candidates)`、門檻（`budget`／`oracle_margin`／
`threshold`、池的 `threshold`）要等於 manifest 的 budget 與 penalty 算出的值；`verdicts.json` 裡各臂 experiment 的條件
（split／costs／window／penalty）要等於 `experiment_for(manifest)`、`budget_spent` 的答案數＝預算、任何臂（含 `seam_failed`）
的答案數不超過預算、走池的臂（Random／Grid／對照組）的每筆 trial 都是池裡的規則；`oracle.json` 埋的規則三段數字的視窗要等於 manifest 的 split
（Random／Grid 的 `exhausted` 答案數＝池大小是紀錄本身的閘門、每次讀都檢查；對照組的在這裡對照 manifest 的 `backtests` 核對）；同一個目錄被走到兩次（root 裡有連到它的連結）只算一次；壞掉的紀錄（含 manifest）拒絕句子帶檔案路徑；兩份紀錄沒有都在的目錄（只有 `arms/`＝搜尋進行中或沒跑完、
只有 `verdicts.json`＝停在兩次搬檔之間）拒絕而不是跳過，句子只說「等沒有搜尋在跑再手動移除」；不要求 `world.sqlite`（artifact 可裁掉省空間）。

**兩份紀錄**：`verdicts.json`＝`{verdicts_version, world_id, pool_size, manifest_digest, results: [{arm, replicate, snapshot,
verdict, holdout}]}`（`manifest_digest`＝跑的時候 run 目錄的 digest，把紀錄綁在它的條件上、不只綁 world id——同 seed 的 null
雙胞胎 id 相同、池相同，只差 digest），
`verdict` 寫在 `snapshot` 旁邊給人讀，讀回時重算 `final_verdict(snapshot)` 不相等就拒絕（紀錄永遠可由快照重判，計畫 §4.5）；
`holdout` 有值 ⇔ `selected` 非空；grid 只有 replicate 0、必有 `grid-0`；`interrupted` 的快照不收；各臂 distinct rules＝trials 數；走池的臂（Random／Grid／對照組）另外
不超過 `pool_size`、答案數不超過 `pool_size`；Random／Grid 的 `exhausted` 答案數＝`pool_size`（對照組每輪可以抽好幾條，它的數另計，見「不用模型的對照組（工具組 T5）」）；模型臂不得 `exhausted`。`oracle.json`＝
`{oracle_version, world_id, manifest_digest, planted: {train, validation, holdout, budget, threshold, oracle_margin, blockers[],
eligible, margin, passed} | null, pool: {size, validation_sharpes[], best, threshold, clearing}}`：`blockers` 是 autoresearch
閘門在 `threshold(max_trials)` 失敗的判斷式名稱（`GateBlocker`：`ruined`／`no_trades`／`no_gain`／`under_threshold`，按閘門順序，
空＝過），由旁邊的 validation 數字衍生，所以手改 blockers 寫不出「有餘裕」；閘門的句子數若跟這四個判斷式對不上（上游多了新判斷式），
`planted_figures` 拒絕；`blockers`／`eligible`／`margin`／`passed`／`best`／`size` 都是衍生值，讀回對不上就
拒絕；三段數字各自要掛在自己的視窗名下；`planted` 有值時一定帶 holdout（紀錄是搜尋之後寫的）；`clearing` 不能超過
validation sharpe ≥ threshold 的個數。

**2026-11-09 dev seeds 校準（計畫 §8 #1／#2）**：40 個 dev seed，`vol` 0.01、`funding.sigma` 1e-4，埋的規則＝`funding_zscore_14 < −1.5`＋`realized_vol_20 > 0.0095`＋`max_bars 6`（曝險約 17%），候選池 128 條（long／short × hold 6／12 × entry 八格 × gate 四格）。在記憶體裡算，不落 ledger、零 LLM 費用。純雜訊世界的誤判率：

| validation 根數 | 門檻 base／k | Random 預算 30 | 整池 128 條 |
|---|---|---|---|
| 400 | 1.0／0.25（autoresearch 舊預設） | 78% | 88% |
| 400 | 2.0／0.5 | 20% | 20% |
| 1000 | 1.0／0.25（autoresearch 舊預設） | 60% | 62% |
| 1000 | 2.0／0.25 | 5% | 15% |
| 1000 | 2.0／0.5 | 0/40 | 0/40 |

**定案**：`bars.count` 5000（validation 1000 根）、`penalty` 2.0／0.5、`max_trials` 30（`threshold(30)`＝3.70）、`oracle_margin` 0.25；β 三檔＝tight 0.0025（oracle 有餘裕 21/40）、easy 0.004（39/40）、trivial 0.008（40/40）。這組門檻 2026-11-10 起也是 autoresearch 的預設（模型臂 dev 跑完後使用者拍板）；門檻按 coin pin，所以既有的 BTC store 仍是舊值 1.0／0.25，不會被改寫（計畫 §7）。**θ_b＝0.8**（同一批 40 個 dev seed × 三檔 β）：埋的規則對自己一致率 1.00，池 128 條裡 θ=0.8 平均 1.82 條算「就是埋的那條」（本尊＋相鄰一格 gate），持有 12 根的版本（0.51）不算；0.8–0.85 之間找回數是平台。範例 manifest（400 根）只是單元測試的夾具，不是校準過的世界。

```
python -m contrib.discovery oracle --run data/discovery/SYN-EB3BC030-c5f56953
python -m contrib.discovery search --run data/discovery/SYN-EB3BC030-c5f56953
python -m contrib.discovery calibrate --root data/discovery
```

## LLM 臂（PR 5）

manifest 的 `arms.llm` 有填時，`search` 在 Random／Grid 之後多跑模型臂。它排在最後，因為它是唯一花錢的臂：前面的臂若跑不起來，一毛都還沒花。
每個 replicate 一顆 store（`arms/llm/replicate-<k>.sqlite`），經 autoresearch 自己的 `ChatHypothesist` 建模型（provider、model、temperature 取自 manifest，
API key 由 provider 從環境變數讀），用同一個 `search()`、同一份 split／成本／penalty／預算。

模型看得到 prompt 與失敗回饋，可以提池外的門檻與結構（計畫 §4.3），所以「trial 都在池裡」「答案數不超過池大小」只對走池的臂（Random／Grid，以及工具組 T5 的對照組）成立；預算照樣是
`max_trials`，被拒絕的答案也扣預算；`calibrate`／`report` 每列印答案數與不同規則數的中位數和最小值，一條規則都沒 file 的搜尋看得出來，不會被讀成乾淨的「零誤判」。模型臂沒有池可走完，`exhausted` 對它不成立。模型呼叫失敗會重試 6 次（429 與 5xx），seam 失敗記 `seam_failed`，照 calibrate 的規則不進分母、另計。**作業規則：讀表之前，把所有 seam 失敗的世界全部重跑**（手動移除 `arms/`、`verdicts.json`、`oracle.json`、`scores.json`；盲猜組的世界還有 `model_use.json`），不看結果一律重跑，三臂比的世界才對得齊、也不會挑結果（2026-12 的開發批次改成重跑一次、再失敗就把那個世界從成對比較拿掉，見「判定規則（2026-12 開發批次）」）；
中斷或收尾前的任何失敗，整個 run 丟掉、已花的費用也白費（replicate 不 resume，計畫 §9 (14)）。provider 打錯，以及 OpenRouter 等 OpenAI 相容 provider 的 API key 沒設，在 `generate` 與 `search` 開始時就具名拒絕；Anthropic 直連與 Bedrock 要到第一次呼叫才發現，那次搜尋會以 seam 失敗收場。客戶端若丟掉 temperature 也拒絕，否則紀錄描述的是沒有發生的條件。

**一個世界兩個目錄**（計畫 §8 #5）：Random／Grid 全量用 `arms.llm: null` 的 manifest；要三臂並欄的子集用另一份帶 `arms.llm` 的 manifest，**同 seed 重新落地**。
`arms.llm` 在 manifest 的身分裡，所以這是另一個目錄、另一個條件 digest，`calibrate`／`report` 會把它分成自己的列；裡面的 Random／Grid 重跑一次，只花 CPU。
這樣三臂並欄永遠只比同一批世界，「一個世界只搜一次」也不用改。`arms.llm.world_subset` 必須含本世界，否則讀 manifest 時就拒絕；同一批世界的 manifest 要寫**同一份** `world_subset`（它在條件 digest 裡，各寫各的會讓每個世界自成一列）。

呼叫次數＝子集世界數 × `replicates` × `max_trials`。

## 訓練區間查詢（工具組 T1）

2026-11-10 的模型臂跟著 validation 分數爬坡，連純雜訊世界都爬得上去。工具組要問的是：讓模型在提案前先查**訓練區間**的資料，找回率會不會上升、
雜訊世界的誤判會不會下降。T1 只做查詢本身，三個純函式。MCP 伺服器（T2，見下一節）包住它們；會用工具的 hypothesist（T3，見「會用工具的提問器」一節）經由伺服器呼叫它們；
不用模型的對照組（T5，見「不用模型的對照組（工具組 T5）」一節）直接呼叫 `backtest_train` 這個函式，不經伺服器。

**界線。** 三個查詢都只吃一個 `TrainView`。`TrainView.load(store, coin=, split=, costs=, indicator_lookback=)` 用
`until_ms = 訓練區間結尾 − 1` 讀 store，所以 validation 與 holdout 的 bar 不是被濾掉，是根本沒有讀進來。view 收的是 experiment 的**整個 split**，
訓練區間由它自己從 split 取出，呼叫方不能另外遞一段區間進來。它認得出「這是不是一個 split」，認不出「這是不是這個 experiment 的 split」，所以要傳的就是 experiment 自己的那一個。`TrainView` 建構時自己再檢查一次：最後一根訓練 bar 之後不能有 4h bar，
日線不能晚於那一根的收盤，funding 不能晚於那個收盤該付的那一筆，整段歷史要在格線上（跟一次量測對歷史的要求相同）；手動組出來的 view 也過同一關。
那個收盤該付的那一筆 funding，交易所會晚幾毫秒才貼出來；view 手上有它的時間戳（funding 覆蓋率要數它），但沒有任何查詢讀它的費率。
`load` 不吃 manifest，模組也不 import `manifest.py`，所以世界類型與埋入的規則沒有路徑進到任何輸出。查詢不寫任何東西：不落 trial，不計入
`rules_tried`。

| 查詢 | 輸入 | 輸出 |
|---|---|---|
| `feature_stats` | `{"feature": "<指標名稱>"}` | `feature`、`bars`（訓練 bar 數）、`bars_with_value`、`min`、`p05`／`p25`／`p50`／`p75`／`p95`、`max`；`regime` 改回 `counts`（每個 label 的根數，沒出現過的 label 是 0） |
| `conditional_returns` | `{"conditions": [...], "horizon": N, "side": "long"｜"short"}` | `bars`、`bars_holding`（條件成立的根數）、`bars_unevaluable`、`horizon`、`side`、`samples`、`mean_return`、`std_return`、`t_stat`、`baseline_samples`、`baseline_mean_return` |
| `backtest_train` | `{"rule": {...}}`，跟正式提案是同一個物件 | `bars`、`bars_unevaluable`、`trades`、`exposure`、`turnover`、`net_sharpe`、`net_return`、`ruined` |

**同一個解析器。** 規則走 `parse_spec`，條件走 `parse_conditions`，指標名稱走 `parse_feature_name`；條件在哪幾根成立問的是評估器自己的
`condition_bars`，訓練區間的回測就是 `evaluate_segment`。解析器會拒絕的規則、條件、指標名稱，查詢也拒絕，錯誤文字就是解析器那一句
（`ToolRefusal`）。每個參數都必填，多的鍵拒絕。`horizon` 的範圍就是語言對 `exit.max_bars` 的範圍（1–1000）。查詢只會比提案窄、不會比它寬：
條件的門檻可以是數字、另一個指標或 regime 標籤，不收 `{"param": ...}`（單獨一串條件外面沒有 `params` 區塊）。

**第一根讀不到值的指標，三個查詢都不讀（2026-11-13 拍板）。** 查詢用到的指標在訓練區間第一根沒有值時，`feature_stats` 與 `conditional_returns` 丟
`EvaluationError`，跟評估器不量測讀這個指標的規則（`backtest_train` 走的那一條）是同一條規則。不這樣做的話，查詢會把指標有值的那一段當成整個訓練區間
來回答（例如 30 根裡只有後 11 根有值）；整段都沒有值時則回「0 根成立」，看起來像沒有訊號，其實是問不到。評估器其他的拒絕理由裡，跟格線有關的
（缺 bar、時間戳不在格線上）view 建構時就自己檢查了；剩下的（funding 覆蓋不足、窗口只有一根）只跟算損益有關，只有 `backtest_train` 會遇到。落地的世界不會遇到（訓練區間從每個指標都有值的地方開始）。
中途才讀不到值的 bar（例如交易所漏了幾筆 funding，那一根的費率過期）不拒絕：`conditional_returns` 與 `backtest_train` 計入 `bars_unevaluable`（`backtest_train` 的那個數是計分迴圈自己的，不含它不做決定的最後一根），`feature_stats` 則是 `bars_with_value` 少於 `bars`。這類「store 答不出來」的錯誤（`FeatureError`、`EvaluationError`）
不是模型的錯，不包成 `ToolRefusal`，照樣往外丟；autoresearch 的那幾句錯誤訊息帶日曆時間，所以伺服器（T2）自己接住，不原樣回給模型（見下一節「一次呼叫怎麼結束」）。

**`conditional_returns` 的算法（2026-11-13 拍板）。** 一個樣本的報酬＝`close[t+N] / close[t] − 1`，`side` 是 short 時取負，不含成本。
樣本只取不重疊的：條件成立的 bar 由前往後取，彼此至少隔 N 根，而且往後 N 根都還在訓練區間內；碰到結尾的直接丟掉，不補。
`mean_return`、`std_return`（樣本標準差）、`t_stat`＝`mean / (std / √samples)`（對 0）都只用這一組樣本。原因：相鄰 bar 往後看 N 根的報酬幾乎整段重疊，
拿全部成立的 bar 直接算，t 值會偏高，模型信了就等於又在爬雜訊。樣本不到 2 個時 `std_return` 與 `t_stat` 是 `null`；樣本彼此相同（或只差在浮點捨入）時 `t_stat`
是 `null`。

`baseline_mean_return` 是同一個 horizon、同一個方向、**不看條件**的平均報酬：訓練區間從第一根起每 N 根取一個樣本，共 `baseline_samples` 個。
訓練區間如果整段偏漲或偏跌，任何條件都會沾到那個漂移，所以條件的平均要跟這個數字比，不是跟 0 比。

這組樣本不等於規則實際會做的交易：評估器在下一根開盤成交、出場那一根不再進場，所以「訊號出現就進場、抱 N 根」的規則每筆交易會再晚一根；
整條規則的成績要問 `backtest_train`。

`backtest_train` 的數字等於同一條規則當成 trial 量測時 train 段的數字（測試逐欄比對）。JSON 層把數字取 6 位有效數字；純函式回的紀錄不取整。
三個紀錄型別（`NumberStats`、`LabelCounts`、`ConditionalReturns`）在建構時檢查自己的數字對不對得上（例如樣本數不能多於成立根數），手動造的也過同一關。

## MCP 伺服器（工具組 T2）

T1 的三個查詢，包成一個走 MCP 的伺服器（`tools_server.py`）。會用工具的提問器（T3，`toolarm.py`）不直接呼叫函式，而是問這個伺服器。
伺服器是另一個程序，用自己的 stdin／stdout 講 MCP。

**為什麼是另一個程序。** 跑搜尋的那個程序手上有讀到 validation 結尾的資料。伺服器程序啟動時只收五樣東西（`ServerParams`）：資料檔路徑、
coin、experiment 的整個 split、成本、indicator lookback。它不讀 manifest，也不 import 會讀 manifest 的模組，所以世界類型與埋入的規則不在
那個程序裡；它用 `TrainView.load` 讀到訓練區間最後一根就停，所以 validation 與 holdout 的 bar 也不在那個程序裡。split 是整個傳的，所以伺服器知道
後兩段的時間點，但沒有那兩段的資料。經 SDK 的 stdio 客戶端啟動時（benchmark 會這樣啟動），子程序拿到的環境變數只有 SDK 放行的一小份清單
（`PATH` 之類），API 金鑰不會被帶進去；`tools` 指令啟動的子程序則沿用下指令那個人的環境。

**怎麼啟動。**

- benchmark（`search` 跑工具組時，見「工具組接進 benchmark（工具組 T4）」）：`ServerParams.for_experiment(資料檔, experiment).command()` 給出整條指令
  （`python -m contrib.discovery.tools_server --db=… --coin=… --split=<JSON> --costs=<JSON> --indicator-lookback=N`，每個參數都是一個
  `--旗標=值`，值以 `-` 開頭也不會被看成另一個旗標），要在 `IMPORT_ROOT`（repo 根目錄）底下啟動。coin、split、成本、lookback 從同一個
  experiment 取，不會拿到別組的。
- 手動試玩：`python -m contrib.discovery tools --run <run 目錄>`。這個指令讀 manifest 只為了取那五樣，然後把 `world.sqlite` 複製到
  暫存資料夾，對著複本啟動上面那個子程序，自己的 stdin／stdout 原樣交給它；它自己要說的話寫在 stderr。**只接受開發用 seed。**
  用複本的原因：打開 store 會把舊版程式寫的檔升級，而 run 目錄是靠 artifact 的 SHA-256 認檔的。
- 資料檔必須已經存在，而且不是空檔。路徑打錯時伺服器拒絕啟動，不會在那裡建一顆空的 store。
- 直接下 `python -m contrib.discovery.tools_server` 時，`--db` 要指向某一組自己的複本，不要指向 run 目錄裡的 `world.sqlite`（理由同上：
  打開會升級舊檔）。
- MCP SDK import 不進來（沒裝，或裝的是缺了伺服器要用的名字的舊版）時，伺服器與 `tools` 指令都在讀資料、複製檔案之前就報錯，附上原因與安裝方式，
  不會先花二十秒暖機。
- **怎麼停。** 關掉它的 stdin，或送中斷（Ctrl-C；Windows 上 Ctrl-Break 也行），exit code 130。SDK 用另一條執行緒讀 stdin，卡在讀取時
  中斷不了，所以伺服器程序自己接中斷並立刻結束（暖機中也一樣；只有剛啟動、還在 import 的那一兩秒例外，那時被中斷是 Python 自己的結束方式）；沒有東西要收尾，因為它不寫任何東西。這個處理只裝在
  「當成程序執行」的入口，別的程式在自己的程序裡呼叫 `main()`／`serve()` 時不會被裝上。`tools` 指令被中斷時同樣回 130，並清掉暫存的那份複本；
  它的行程如果被強制結束（不是中斷），複本不會被清掉，子程序會繼續跑到 stdin 關閉。

**啟動時先暖機。** 伺服器回應第一個請求之前，先把詞彙表每個指標算一次。不這樣做的話，第一次 `backtest_train` 要付整趟 indicator walk
（5000 根的世界約 12 秒），事後看耗時會以為是工具慢。代價是連線要等（本機 Windows、2026-11-16 各量一次）：5000 根的 dev 世界經 `tools` 指令
從啟動到連上 23 秒（其中 indicator walk 約 12 秒，其餘是 import、複製與讀檔），之後每個查詢 17–140 毫秒；測試用的 400 根歷史直接啟動伺服器，
到連上約 6 秒（多半是 import）。接伺服器的一方要把連線逾時設得比這個寬。

暖機只跳過一種情況（2026-11-16 拍板）：這顆 store **整條序列都沒有**（沒有日線，或沒有 funding）。用到那條序列的指標跳過、名字寫進 log，
伺服器照常啟動；之後需要那條序列的查詢回協定層錯誤，其他查詢不受影響（沒有 funding 時，所有 `backtest_train` 也都答不出來，因為持倉要結算 funding）。
判斷的依據是 store 裡有沒有資料，不是接住計算時丟出來的錯誤：序列明明在、卻還是算不出來，丟的是同一種錯誤，那不是缺資料，伺服器會拒絕啟動，
並說明是暖機算不出哪個指標。這包括指標引擎回空的，也包括序列在、但整段沒有任何一根有值（例如 funding 費率從頭到尾不變，z-score 算不出來）。
落地的世界三條序列都有、每個指標都算得出來。

**模型看到什麼（`tool_specs()`）。** 三個工具各有一段說明、輸入 schema、輸出 schema。

- 指標名稱是 enum，取自 autoresearch 的 `feature_names()`；運算子、方向、regime 標籤、offset 與 horizon 的上下界也都取自 autoresearch
  的表，不手抄。詞彙表加了指標，下一次列工具就看得到。
- `conditional_returns` 的條件有完整 schema，而且整段展開、不用 `$ref`：有些模型供應商的工具層不解析 `$ref`，模型就看不到條件長什麼樣，
  而我們這邊無從得知。代價是指標名稱清單在一份 schema 裡出現四次，每次呼叫模型多送約 570 token。`backtest_train` 的規則只宣告
  「是一個 JSON 物件」（2026-11-16 拍板）：把整個語法寫成 schema 等於手抄第二份語法，會跟解析器走偏；規則對不對由 `parse_spec` 判。
- 每段說明都有一個呼叫範例，以及每個輸出欄位的意思。
- **說明只講「這是什麼」「什麼時候用哪一個」，不講「該怎麼下結論」（2026-11-16 拍板）。** 第一版有兩句建議（「訓練分數不等於驗證分數」
  「平均報酬要跟 baseline 比、不是跟 0 比」），已拿掉：這些字只有工具組讀得到，留著的話，工具組在雜訊世界少誤判時，分不出是因為查得到資料，
  還是因為被提醒過。baseline 欄位只留定義。「查詢不計分、也不佔提案次數」這一句是事實陳述；每輪能查幾次由提問器（T3）自己說。
- **範例刻意避開 benchmark 會埋的東西。** 埋入的規則用 funding 指標進場、用 regime 或 realized vol 當門檻、用 `max_bars` 出場；範例只讀
  RSI、用 RSI 出場。autoresearch 的 prompt 本身沒有任何範例規則，所以範例是只有工具組看得到的字：往答案的反方向寫，最壞是讓工具組少找回一點，
  不會白送它答案。測試釘住這一條。
- t 值那一句（2026-11-16 拍板）：只說它是對 0 的單樣本 t 值、沒有針對查了幾個條件做校正、要連同 `samples` 與 `baseline_mean_return`
  一起看。不給 p 值，也不給「多大才算數」的門檻。
- 說明與 schema 有版本號 `TOOLS_VERSION`（目前 1），伺服器把它當成自己的版本回報。測試釘住整份內容的 SHA-256：改了字或詞彙表變了而沒有升
  版本號，測試就紅。真模型試跑後凍結（計畫 §5）。這個版本號只管伺服器說的話；提問器（T3）接在 prompt 後面的文字由它的 `PACK_VERSION`
  管，而且那個版本的雜湊把伺服器這邊的字也算進去（見下一節）。
- `tool_specs()` 每次都重新建一份：呼叫方改了手上的 schema 或範例，不會影響下一次列出來的內容。

**schema 是告示，不擋輸入（2026-11-16 拍板）。** 用的是 SDK 的低階 `Server`：它把 schema 列給客戶端看，但不替我們驗參數。參數原樣交給 T1 的
`call()`，所以錯誤訊息就是解析器那一句，多的鍵會被拒絕。高階的 `MCPServer` 實測（mcp 2.3.0）會回 pydantic 的句子、把多的鍵靜靜丟掉，
而且程式例外會變成模型看得到的工具錯誤，跟模型自己打錯分不開。

**一次呼叫怎麼結束。**

| 情況 | 客戶端收到什麼 |
|---|---|
| 查得到 | 工具結果：同一個 dict 給兩份，一份是文字（JSON），一份是 structured content |
| 模型的錯（`ToolRefusal`：指標名稱打錯、規則不合語法、多了鍵、工具名稱不存在） | 工具結果，標成錯誤，內容是解析器那一句 |
| store 答不出來（`EvaluationError`／`FeatureError`） | 協定層錯誤，固定一句 `the store cannot answer this look-up`；原句帶日期，只寫到伺服器的 stderr |
| 程式缺陷（其他任何例外） | 協定層錯誤，固定一句 `the look-up failed`；例外的原文與 traceback 只寫到伺服器的 stderr |

第四列由伺服器自己接，不交給 SDK：實測 mcp 2.3.0，客戶端如果用舊的握手方式（`initialize`）連線，SDK 會把例外的原文直接回給它，而原文可能帶日期。
測試用新舊兩種握手各跑一次。

提問器（T3）把協定層錯誤當成接縫失敗，不當成工具結果交給模型。伺服器啟動失敗時（資料檔不存在、split 對不上這顆 store），原因印在子程序的
stderr，客戶端收到的是「連線已關閉」的錯誤，不會卡住。開始服務之後才出的錯（例如客戶端不再讀取）不是「拒絕啟動」，照原樣丟出、不包裝成 exit 1 的句子。
接伺服器的一方要持續讀走子程序的 stderr，否則子程序寫滿管線後會卡住；提問器把它導到一個檔案（`server_log`）。

**安裝。** `pip install -e ".[discovery-tools]"`（`mcp>=2.3,<3`）。沒裝的環境照樣能跑這個套件的其他指令（`tools_server.py` 只在建伺服器時才
import SDK）；paper 伺服器與 dashboard 的環境都不裝。CI 另開一個 job（`discovery tests`）裝這個選用安裝並跑這個套件的整套測試：SDK 需要的
sse-starlette 要求比 dashboard 釘的版本更新的 starlette，兩者放不進同一個環境。測試在檔頭直接 import SDK，沒裝就是紅的，不會靜靜跳過。

## 會用工具的提問器（工具組 T3）

`toolarm.py` 的 `ToolHypothesist`：一個可以先查訓練區間、再回答規則的 hypothesist。`search()` 完全不用改——查詢都發生在一次 `propose()`
裡面，`propose()` 最後回的還是一段文字（一條規則），由迴圈照舊解析、量測、寫紀錄。`search` 在 manifest 指名 `arms.llm_tools` 時會用到它（見「工具組接進 benchmark（工具組 T4）」）。
到目前為止沒有花過任何 API 費用：測試全部用照劇本回答的假模型，伺服器則是真的。

**一輪怎麼走。**

1. autoresearch 的 system prompt 後面接上一段說明（見下），連同 user prompt 送給綁了三個工具的模型。
2. 模型每要求一次工具呼叫，提問器就去問伺服器（T2，另一個程序），把結果加上一行「本輪還剩幾次」回給模型。
3. 模型回一則不帶工具呼叫的訊息，那段文字就是這一輪的答案。

每一輪都從那兩段 prompt 重新開始，上一輪查過的東西不帶過來（計畫 §2.4）。

用法（`bench.py` 就是這樣接的；檔名是每個 replicate 的 store 換副檔名）：

```python
with ToolHypothesist.open(
    chat,                                             # autoresearch 的 ChatHypothesist
    ServerParams.for_experiment(db, experiment),      # 這一組自己的資料檔與 experiment
    trace=arm_dir / "replicate-0.trace.jsonl",
    server_log=arm_dir / "replicate-0.server.log",
    limits=Limits(lookups=5, backtests=2),            # manifest 的 arms.llm_tools 寫的那兩個數
) as hypothesist:
    report = search(ledger, experiment, hypothesist, model=hypothesist.label, max_trials=...)
```

`open()` 在 `with` 區塊期間握著三樣東西：伺服器子程序、trace 檔、跑 MCP 客戶端的背景事件迴圈（MCP 客戶端是 async，`propose()` 是同步）。
一組搜尋開一次。

**模型從哪裡來。** 跟 LLM 臂一樣是 autoresearch 的 `ChatHypothesist`；提問器用它手上那個 client 的 `bind_tools` 綁工具。這個模組不
import langchain（`tests/test_upstream.py` 的規則沒有動）：回覆用屬性讀（`content`／`tool_calls`／`invalid_tool_calls`／
`usage_metadata`），工具結果寫成一個普通的 dict 訊息（`{"role": "tool", ...}`），工具定義也是普通的 dict。這三種形狀 langchain 吃不吃，
由 `contrib/tests/test_toolarm_wire.py` 對著 langchain 本身驗（那個資料夾不屬於任何套件，所以不受這個套件「測試也不碰 langchain」的規則管）。

**模型多讀到什麼（`Pack`）。** 除了 autoresearch 的兩段 prompt，工具組的模型還會讀到三樣：

- 三個工具的名稱、說明、輸入 schema，取自伺服器的 `list_tools`（輸出 schema 不會送給模型）。
- 接在 system prompt 後面的一段說明（`addendum()`）。**只講事實、不給建議（2026-11-19 拍板）**：工具查得到的是訓練區間；驗證區間對模型與每個
  工具都仍然看不到，而規則旁邊列的驗證數字是在那裡量的；每輪最多幾次、額度用完之前每次都算；查到的東西不會留到下一輪；每次呼叫前先寫一行「要查什麼、為什麼」；
  最後照舊只回一條規則的 JSON。它同時處理了 system prompt 原本那句「在你看不到的歷史上評分」：有了工具之後，這句話只對一半。
  不寫「訓練分數不等於驗證分數」這類提醒，理由同 T2：只有工具組讀得到的提醒，會讓結果分不出是工具有用還是提醒有用。
- 提問器會對模型說的幾句固定的話（`SENTENCES`）：剩餘額度那一行、額度用完、試跑額度用完、呼叫送不出去、強制作答時的標題與要求、
  以及強制作答時把本輪查詢寫成文字用的兩個模板。另外四個數字也在雜湊裡：`forced_note_chars`（模型自己寫的說明最多回放幾個字元）與
  `argument_bounds`（呼叫的長度、巢狀層數、工具名稱長度三個上限）。
  測試會檢查模組裡每一個句子常數都在這張表裡，漏放就紅：不在表裡的句子，改了字雜湊不會動。

有兩樣模型讀得到、但不在這包裡：解析器拒絕時的那一句（那是 autoresearch 的，跟著它的程式碼走），以及各段文字怎麼排（之間的換行、強制作答時參數的 JSON 寫法、
失敗呼叫的 `status` 標記），後者由測試逐字比對。

這整包有一個版本號 `PACK_VERSION`（目前 1；2026-11-19 拍板用一個數字管整包），測試釘住整包的 SHA-256，所以伺服器的說明改一個字，這邊也會紅。
T4 的 manifest 只需要記這個數字與每輪上限。連線後先核對兩件事，對不上就拒絕開始：伺服器回報的名稱與版本等於這份程式碼的，以及它列出來的
三個工具（名稱、說明、輸入 schema）跟這份程式碼的 `tool_specs()` 完全相同。執行時核對的是工具這一部分；那段說明與固定句子本來就出自同一份程式碼，雜湊則由測試釘住，
所以只要測試是綠的，模型實際讀到的就是釘住的那一包。
真模型試跑後凍結（計畫 §5）。

**每輪上限（`Limits`；由提問器數，不由伺服器數）。** 起點是每輪 5 次呼叫、其中拿得到分數的 `backtest_train` 最多 2 次，試跑一個世界後定案。

| 情況 | 模型收到什麼 | 算不算一次 |
|---|---|---|
| 查得到 | 伺服器的 JSON，後面一行 `[left this round: N tool call(s), of which backtest_train: M]`（2026-11-19 拍板每次都附） | 算 |
| 模型的錯（指標打錯、規則不合語法、工具名稱不存在） | 解析器那一句＋剩餘額度，標成錯誤 | 算 |
| 呼叫送不出去（參數 client 解析不了、不是 JSON 物件、轉成 JSON 再讀回來不是同一個值、巢狀超過 32 層、寫成 JSON 超過 8,000 字元；工具名稱超過 64 字元） | 固定一句（「工具名稱或參數無法照原樣送出」）＋剩餘額度，標成錯誤；不送伺服器 | 算 |
| 帶呼叫的整則訊息放不回對話（訊息裡任何地方有半個 surrogate，或參數帶 NaN／無限大） | 這段對話到此為止，直接改成強制作答；這一則裡的呼叫都不執行 | — |
| 已經拿到 2 次分數之後的 `backtest_train` | 「試跑額度用完，這次沒有執行」＋剩餘額度；其他工具照常可用 | 算 |
| 第 6 次呼叫 | 「額度用完，這次沒有執行，請直接回答規則」 | 不算（已經滿了） |
| 被告知額度用完之後，又有一則訊息帶呼叫 | 這段對話到此為止，改成強制作答（見下）；這一則裡的呼叫不會得到回覆 | — |

三個「不執行」的檢查照這個順序問：總額度用完、呼叫送不出去、試跑額度用完。

**被解析器拒絕的 `backtest_train` 算一次呼叫，但不算一次試跑（2026-11-19 拍板）。** 試跑額度限制的是「看到幾次訓練分數」，語法寫錯的規則沒有
回任何分數；扣了的話，一次筆誤就少一半的訓練證據，而不用 LLM 的對照組（T5）永遠拿得到兩次乾淨的試跑。

「呼叫送不出去」與「放不回對話」這兩列也都是模型的錯、不是接縫失敗，搜尋照常繼續。（不帶呼叫的訊息就是答案，裡面有什麼都原樣交給搜尋迴圈。）

「送不出去」的三個上限是為了不讓線路、trace 與強制作答時的文字被模型寫的任何東西塞爆。長度上限 8,000 就是搜尋迴圈對一個回答的長度上限
（autoresearch 的 `MAX_RESPONSE_CHARS`，測試釘住兩者相等）：比它長的規則本來就交不出去，試跑它沒有意義；長度量的是寫進 trace 的那個寫法
（非 ASCII 字元會被寫成跳脫序列），不是模型打了幾個字。這三個數字在版本雜湊裡，因為超過與否會改變模型被告知的內容。

「放不回對話」那一列有兩個實測（2026-11-19）在後面：半個 surrogate 交給 MCP 客戶端會讓連線關閉；就算不送伺服器，那則回覆放回對話後，
下一次呼叫模型時供應商的 client 也編不出請求（用 repo 自己的 client 實測），搜尋會被當成「模型沒回應」而結束（這是 2026-11-19 當時的實測；#441 之後 engine 的 client 送出前會把這種字元換成 U+FFFD，request 送得出去，但這條規則沒有跟著改）。NaN 則會被 MCP 客戶端悄悄換成
`null`，而且有的供應商 client 拒絕編碼它。所以這種訊息不放回去：直接開一段不帶工具的新對話要答案。

一則訊息裡同時要求多次呼叫時，照順序一個一個數，每一個都會得到回覆（有的供應商不接受「有呼叫、沒有回覆」的對話）；例外是那段對話不再使用的時候：觸發強制作答的
那一則，以及伺服器失敗、搜尋就此結束的那一則。

**強制作答。** 重開一段對話、不綁工具：system prompt 是 autoresearch 原本那份（不接工具說明），user prompt 後面附上這一輪查過的東西
（純文字：伺服器回答過的每次呼叫與完整結果；模型在每一則帶呼叫的訊息裡寫的說明，每段最多 2000 字元；提問器自己擋掉的呼叫不列，
觸發強制作答的那一則訊息整則不列）與一句「請回答一條規則」。
這次回覆如果還帶工具呼叫，代表那個 client 仍然綁著工具，算接縫失敗，不會把空字串當成答案交出去。不沿用原本那段對話有兩個原因（2026-11-19 不連網檢查 langchain-core 1.6.3
組出來的請求，沒有真的送出去）：不綁工具的 client 會把「帶著工具呼叫的對話」原樣送出、不附工具定義，各家供應商收不收沒有保證；而「這一輪不准
呼叫工具」各家 client 的寫法不同（`tool_choice="none"` 在 OpenAI 相容的 client 是字面意思，在 Anthropic 的 client 會變成「強制呼叫一個叫
none 的工具」）。純文字的新對話兩種問題都沒有。

**失敗怎麼算（計畫 §2.3）。** 下面這些都是 `HypothesistError`：`search()` 停下來、不扣提案次數、不寫成 refusal。

- 模型那一側：供應商沒回應、回覆不是文字、工具呼叫沒有 id、client 解析成功的工具呼叫卻沒有工具名稱（client 解析失敗的那種沒有名稱是模型的錯，照「送不出去」處理）。提問器本身沒壞，下一次 `propose()` 可以照常再問（跟 LLM 臂一樣）。
- 伺服器那一側（`ToolServerError`）：啟動不了、連線中斷、查詢逾時（預設 60 秒）、伺服器回協定層錯誤（store 答不出來）。這條連線不會再回來，
  之後每一次 `propose()` 直接丟同一個錯誤，不會多花一次模型呼叫去發現。

啟動不了的原因只在子程序的 stderr，所以用子程序啟動伺服器時一定要給 `server_log`（把 stderr 導到那個檔案；不給就拒絕啟動）。
連線逾時預設 180 秒：伺服器在回應握手之前暖機，5000 根的世界實測暖機約 14 秒（`tools_server.py` 的模組說明）、經 `tools` 指令從啟動到連上 23 秒（上一節）。
這個逾時管的是握手的 `initialize`；在它之前，SDK 會先用新版握手試探一次，那一段固定 10 秒（mcp 2.3，我們調不動）。所以暖機超過 10 秒的
世界（幾千根 bar 的都是）實際上走舊版握手連上，而完全不回應的伺服器要等「10 秒＋連線逾時」才會被放棄。兩種握手伺服器都測過（T2）。

`ToolArmError` 是另一回事：沒裝 SDK、上限不合法、`server` 給的是路徑或字串、用子程序卻沒給 `server_log`、模型的 client 不支援工具、
trace 或 `server_log` 檔已存在（各是一組搜尋自己的，不覆寫；前一次的 log 是那次伺服器說明原因的唯一地方）、兩者指到同一個檔、
要寫入的資料夾不存在、伺服器的名稱／版本／列出的工具對不上。這些是還沒問模型任何東西就拒絕，
要呼叫方去修，不是接縫失敗。檔案與資料夾的檢查在啟動伺服器之前做，不會等暖機完才報錯。

**Trace（每行一個 JSON 物件，照發生順序；經 `search` 跑的時候檔名是 `replicate-<k>.trace.jsonl`）。**

| `kind` | 什麼時候寫 | 欄位 |
|---|---|---|
| `pack` | 開頭一次 | `trace_version`（trace 自己的格式版本，目前 1）、`pack_version`、`pack_digest`、`server`、`model`、`tools_version`、`limits`、`addendum`、`sentences`、`forced_note_chars`、`argument_bounds`、`tools`（實際綁給模型的說明與 schema） |
| `model` | 每次呼叫模型 | `round`、`seq`、`bound`（有沒有綁工具）、`text`（模型寫的字，最多 2000 字元）、`text_chars`、`tool_calls`、`input_tokens`、`output_tokens`、`elapsed_ms` |
| `tool` | 每次工具呼叫 | `round`、`seq`、`tool`、`arguments`、`sent`（有沒有送到伺服器）、`tool_error`（是不是模型的錯）、`limit`（`null`／`"lookups"`／`"backtests"`）、`result`（最多 4000 字元）、`result_chars`、`elapsed_ms` |
| `answer` | 每輪結束 | `round`、`seq`、`forced`（是不是強制作答）、`lookups`、`backtests`、`tool_errors`、`text`、`text_chars` |
| `failed` | 接縫失敗 | `round`、`seq`、`side`（`model`／`server`）、`error`（例外的類別名稱） |

每種 `kind` 有哪些欄位寫在模組裡（`TRACE_KEYS`），寫入時逐行核對；欄位或種類有變就升 `TRACE_VERSION`，之後讀舊檔的程式才分得出來。
`seq` 在一行真的寫進檔案之後才往前數，所以同一輪寫出來的行是 1、2、3 連號，不會因為中間某一步失敗而跳號。
`kind`、`limit`、`side` 的值各是一個列舉（`TraceKind`／`Limit`／`Seam`），T4 讀 trace 時用同一組。

讀 trace 時的四條規則：

- 一輪正常結束時最後一筆是 `answer`，接縫失敗時是 `failed`。兩者都沒有的輪次是被打斷的：中斷（Ctrl-C）、程序死掉，或接縫失敗以外的任何例外。
- 伺服器失敗的那一次查詢照樣有一筆 `tool`（`sent` 為 true、`result` 為 `null`），緊接著才是 `failed`，所以看得出搜尋停在哪個工具、什麼參數。
  `sent` 的意思是「交給伺服器了」，不保證伺服器收到；`failed` 的 `error` 是最底層那個例外的類別名稱。
- 模型的回覆讀不懂（不是文字、工具呼叫沒有 id）時沒有 `model` 那一筆，只有 `failed`（`error` 是 `HypothesistError`）；那一次呼叫的 token 數不在 trace 裡。
- 強制作答（`answer` 的 `forced` 為 true）有兩種來源，看前面的行分得出來：前面有一筆 `limit` 為 `"lookups"` 的 `tool`，是額度用完後還繼續呼叫；
  沒有的話，是那一則訊息放不回對話（它的 `model` 那一筆 `tool_calls` 大於 0，後面卻沒有對應的 `tool`）。

刻意不寫的三樣：兩段 prompt（user prompt 帶驗證區間的數字；需要時可以從 ledger 重建）、任何時間戳（只記耗時）、
失敗的原句（供應商的錯誤訊息可能引用請求內容；`search()` 的報告已經有那一句）。

「trace 不含驗證區間的數字」說的是提問器自己寫進去的東西：它只寫工具的回答（只讀訓練區間）與上面那些欄位，測試把訓練區間之後的資料全部改掉，
trace 除耗時外逐行相同。**模型自己寫的字照原樣留著**，而 user prompt 本來就列了試過的規則的驗證分數，所以模型如果在說明裡引用那些分數，
trace 的 `text` 欄就會有；那是模型本來就看得到的東西，不是多漏出去的。

**還沒做的。** 真模型一次都還沒碰過這些工具，第一次是 12 月的試跑（計畫 §5）；上限與這一節的文字到那時才定案。
接進 `bench.py` 的部分見「工具組接進 benchmark（工具組 T4）」一節。

**已知風險（沒連過真的供應商，只能等試跑驗證）。** 三種情況下，第二次呼叫模型可能被供應商以 400 拒絕，那會變成接縫失敗、搜尋在第一次查詢後就停：

- 模型開了 thinking／reasoning：repo 的 client 會把回覆的內容區塊壓成純文字，提問器把那則回覆放回對話時，thinking 區塊已經不在了；
  有些供應商要求帶工具呼叫的那一則回覆原樣送回。目前工具組用固定 temperature、不開 thinking，照理不會遇到。
- 模型的工具呼叫參數被截斷（不是合法 JSON）：提問器回它一句「工具名稱或參數無法照原樣送出」並計一次，但那則回覆放回對話時，client 會把壞掉的參數字串
  原樣送出，上游收不收沒有保證。

- 模型叫了一個名稱不合供應商規定的工具（例如帶空白或 `functions.` 前綴）：伺服器會照常拒絕、算模型的錯，但那則回覆放回對話時，供應商可能因為
  工具名稱不合格式而拒收。

試跑第一輪如果就停在 `failed`／`side: model`，先查這三項。另外一個同類的邊角沒有處理：
DeepSeek 的 client 會把 `reasoning_content` 帶回去，裡面有壞字元時一樣編不出請求。（供應商回的呼叫 id 帶壞字元的情況有處理：那則訊息一樣不放回去，改成強制作答。）

**已知的限制。**

- 「查到的東西不留到下一輪」只對這個模組成立。autoresearch 的 prompt 會列出被拒絕的回答（解析器那一句會引用原文），所以模型可以花掉一輪、
  故意交一個不合法的回答，把幾個字元帶到下一輪。這條通道是搜尋迴圈的，每一組都有；只是只有工具組手上有值得帶的東西。試跑時看有沒有這種行為。
- 沒開 thinking 時，盲猜組只回 JSON，工具組卻被要求在每次呼叫前寫一行，等於多了一點打草稿的空間。T5／T6 都沒有對照這一點，寫結果時要註明。
- 連線建立那一段（伺服器暖機中）在 Windows 上按 Ctrl-C 可能要等到握手結束才生效；查詢進行中的等待每 0.5 秒醒來一次，不受影響。

## 工具組接進 benchmark（工具組 T4）

manifest 的 `arms.llm_tools` 有填時，`search` 在 Random／Grid 之後跑「會用工具的模型臂」。它跟盲猜的模型臂（`arms.llm`）一樣排在最後，因為它花錢。
一份 manifest 最多指名一個模型臂（`llm`、`llm_tools`，或工具組 T6 的 `llm_summary`；填兩個以上會被拒絕）：工具組與盲猜組是同一個 seed 的兩個落地目錄，
跟「LLM 臂（PR 5）」一節的「一個世界兩個目錄」是同一條規則。

`arms.llm_tools` 的欄位＝`arms.llm` 的五個，再加三個：

| 欄位 | 意思 |
|---|---|
| `lookups` | 每輪（一次回答之前）最多幾次工具呼叫，至少 1 |
| `backtests` | 其中最多幾次拿得到訓練分數的 `backtest_train`，0 到 `lookups` |
| `pack_version` | 模型多讀到的那整包文字的版本，要等於這一版程式的 `toolarm.PACK_VERSION` |

三個都在 manifest 的身分裡：上限或版本不同，就是另一個目錄、表上另一列。

**每個 replicate 怎麼跑。** 跟模型臂一樣一顆 store（`arms/llm_tools/replicate-<k>.sqlite`）、同一個 `search()`、同一份 split／成本／penalty／預算。
差別在提問器：模型臂的在開跑前建一次，工具組的每個 replicate 各開一次。`bench` 先複製 artifact、在那顆 store 裡建好 experiment，
再用**那個 experiment** 的 coin／split／成本／indicator window 啟動查詢伺服器子程序（`ServerParams.for_experiment`），搜尋結束就關掉。
搜尋迴圈與伺服器會同時開著同一顆 store（伺服器只在啟動時讀到訓練區間結尾，之後不再碰檔案）；測試用真的子程序跑過這個情況。
store 旁邊多兩個檔：`replicate-<k>.trace.jsonl`（trace，格式見上一節）與 `replicate-<k>.server.log`（伺服器的 stderr，正常是空的）。

**找到什麼與花了什麼，分兩個檔。** `verdicts.json` 的格式沒有動，工具組的結果跟其他臂寫在一起、形狀相同。每輪查了幾次寫在另一個檔
`tool_use.json`（[`tooluse.py`](./tooluse.py)），每個 replicate 搜尋結束後從它的 trace 讀一次整理出來。示意（實際的檔鍵照字母排、
逐行縮排；這裡是測試裡那次搜尋的內容，模型是照劇本回答的假模型，兩個 replicate：第一個跑完兩輪，第二個第一次呼叫模型就沒回應；
下面 `report` 的兩行印的是同一次搜尋）：

```json
{
  "tool_use_version": 1,
  "world_id": "SYN-EB3BC030", "manifest_digest": "0123abcd",
  "model": "scripted/model", "pack_version": 1, "pack_digest": "<整包文字的 SHA-256>",
  "limits": {"lookups": 4, "backtests": 1},
  "searches": [
    {"replicate": 0, "seam_failed": false,
     "model_calls": 4, "unmetered_calls": 0, "input_tokens": 480000, "output_tokens": 120000,
     "rounds": [{"lookups": 2, "backtests": 1, "tool_errors": 0, "forced": false},
                {"lookups": 1, "backtests": 0, "tool_errors": 1, "forced": false}]},
    {"replicate": 1, "seam_failed": true,
     "model_calls": 0, "unmetered_calls": 0, "input_tokens": 0, "output_tokens": 0, "rounds": []}
  ]
}
```

- `rounds` 一筆是一輪有回答的搜尋：`lookups`＝計數的呼叫、`backtests`＝其中拿到訓練分數的、`tool_errors`＝其中是模型自己的錯的
  （一次呼叫不會同時是兩者）、`forced`＝這一輪是不是被強制作答。
- 每一輪的這四個數是提問器邊跑邊記的；整理時會拿同一輪的模型行與工具行重算一次，對不上就拒絕。所以這個檔寫的每個數字，trace 裡都逐行對得出來。
- `model`＝ledger 記的那個模型名字（`provider/model`），讀的時候會核對它等於 manifest 的 `arms.llm_tools` 指名的那一個。
- `seam_failed`＝這個 replicate 是不是因為模型沒回應而結束；結束前有回答的輪次與已經花掉的 token 照樣記。
- `unmetered_calls`＝供應商沒有回報完整用量的模型呼叫次數（兩個數缺一個就算；負數也當成沒回報）。有回報的那一半照樣加進 token 數。

沒有工具組的 manifest 不會有這個檔。`calibrate`／`report`／`score` 讀到工具組的目錄時會核對它，下面任何一項不對都具名拒絕：
該有而沒有、不該有而有、世界或 manifest digest 不對、replicate 對不上、上限或 `pack_version` 或模型跟 manifest 不同、輪數不等於 ledger
的答案數、「以 seam 失敗結束」跟 `verdicts.json` 說的不一致。`report` 另外核對同一列的每個世界讀到的是同一包文字（`pack_digest` 相同）；
同一個 `pack_version` 底下 digest 不同，代表是不同版本的程式跑的；這時 `report` 會具名拒絕、整張表不印，不會把兩者算進同一個比例。

**`report` 在工具組那一列多印兩行**（Random／Grid 的列不變；盲猜組的列見下面「盲猜組花了什麼」）：

```
  tool use over 2 round(s) of 1 finished search(es): calls a round median 1.5 (max 2), 0 round(s) asked for no tool; backtests 1; the model's mistakes 1 of 3 call(s); answered without tools 0/2 = 0.0%
  tokens reported by scripted/model over 2 search(es), failed seams included: 480,000 in, 120,000 out, in 4 model call(s) that came back (a floor: a retried call counts once, a reply that was not read not at all)
```

- 第一行只算**跑完的搜尋**的輪次，跟那一列的找回率同一個分母：每輪呼叫次數的中位數與最大值、沒有要求任何工具的輪數
  （被強制作答的輪次不算在這裡，不管它被計數了幾次：它要求過工具）、拿到訓練分數的試跑數、模型自己的錯佔幾次呼叫、強制作答的輪數與比例。
- **強制作答的輪次留在主指標裡**（2026-11-19 拍板）：那一輪交出來的規則照常量測、照常計入找回與誤判，這一行只是把比例另外印出來。
  比例多高要調上限或文字，12 月試跑後、正式開跑前定。
- 第二行是供應商回報的 token 數，寫明是哪個模型的；seam 失敗的搜尋也算進去，因為錢已經花了。**這個數是下限**：只算「有回來、讀得懂」的模型呼叫。
  client 自己重試掉的呼叫只算到最後成功的那一次，讀不懂的回覆完全不在裡面（trace 沒有那一筆）；沒回報完整用量的呼叫另外標出個數。
- 加 `--price-in <美元> --price-out <美元>`（每一百萬 token 的單價，兩個要一起給）就在第二行後面多印估算花費。
  單價不寫在 manifest、程式也不內建：它不是實驗條件，內建的表下次跑就過期了。估算用的就是上面那個下限，而且把每個輸入 token 都照原價算、
  沒有扣 prompt caching 的折扣，所以兩個方向都可能偏，只能當量級看。一組單價是一個模型的，而且會套用到每一個有 token 紀錄的列（工具組與盲猜組都是，
  每一列的 token 行都寫出是哪個模型，2026-11-22 拍板維持這樣）；同一個 root 底下有兩個模型時，兩列都會用這組單價算，要自己分開跑。沒有任何一次呼叫把輸入與輸出兩個數都回報時不印金額，改印「無法估算」（token 數照樣印有回報的那一半）。
  單價不是 0 以上的有限數是用法錯誤（exit 2，訊息會指名是哪個旗標）；給了單價但表上沒有任何一列有 token 紀錄，會在 stderr 說一句「沒有東西可以估」
  （root 底下一個搜過的世界都沒有時，只會印「no searched world」）。

**兩種失敗分開處理**（2026-11-19 拍板）：

| 哪一側 | 例子 | 怎麼算 |
|---|---|---|
| 模型 | 供應商斷線、重試 6 次仍失敗、回覆讀不懂 | 跟模型臂一樣：那個 replicate 記 `seam_failed`、不進分母，其他照跑。讀表前照舊把有 seam 失敗的世界整個重跑（除了模型臂那幾樣，還要移除 `tool_use.json`；盲猜組的世界則是 `model_use.json`）。2026-12 的開發批次重跑以一次為限，見「判定規則（2026-12 開發批次）」 |
| 查詢伺服器 | 起不來、中途死掉、查詢逾時、某個查詢 store 答不出來 | **`search` 停下來**，**exit 3**，不寫任何紀錄 |

**exit 3 是這種停法專用的**（2026-11-19 拍板）。其他具名拒絕（例如「這個世界搜過了」）是 exit 1。跑一批世界的腳本因此分得出來：
看到 3 就整批停下（下一個世界會遇到同一個伺服器），看到 1 可以跳過。停下之後對同一個目錄再下指令，拿到的是一般的拒絕（exit 1）。
所以**重跑同一個批次迴圈之前，要先處理停下的那個世界**：不然腳本會把它當成「搜過了」跳過去，接著在下一個世界遇到同一個伺服器、再花一次錢才停。

伺服器側停下來時，run 目錄會留著空的 `arms/` 與暫存資料夾，暫存資料夾會從 `.search.partial-…` 改名成 `.search.kept-…`。
**這個名字就是「刻意保留」的記號**：改名不用寫任何位元組，所以就算磁碟滿到連說明檔都寫不進去，之後的指令還是認得出來
（改名失敗時退而求其次，靠裡面的說明檔認）。錯誤訊息會寫出暫存資料夾的路徑；
trace（伺服器起來、提問器也開成功之後才有）與伺服器的 log 在它底下的 `arms/llm_tools/`，前面跑完的臂的 store 也在。暫存資料夾裡另有一個
`kept-for-inquiry.txt`：開頭就是那句錯誤訊息；失敗是這個程序接到的例外時（伺服器起不來、trace 讀不回來），下面附那個例外自己的 traceback
（不附它是在處理哪個例外時發生的：那可能是供應商的錯誤，句子裡可能引用送出去的內容，這個套件不把它寫到任何地方）。
之後對這個目錄再下 `search`、`calibrate`、`score`、`report` 都會被拒絕，拒絕的句子會指出這個資料夾是刻意留下來的、要先讀那個說明檔。
**要 `arms/` 與暫存資料夾兩樣都移掉才會放行**：只移掉 `arms/`、留著暫存資料夾，一樣被拒絕（否則這個世界會被當成沒搜過、直接重跑，
等於把證據跳過去）。`oracle --run` 與 `tools --run` 不看這些，照常可用。寫結果時需要「停了幾次」這個數，清理前先把暫存資料夾搬到別處，不要直接刪。

下面這些情況走同一條路，因為都是程式這一側的問題，而留下來的檔案就是證據：

- 搜尋跑完了，但它的 trace 讀不回來；或 trace 的輪數跟 ledger 的答案數對不上；或 trace 記的上限、`pack_version`、模型跟 manifest 不同；
  或同一個世界的兩個 replicate 讀到的不是同一包文字。
- 伺服器已經啟動之後，提問器才發現開不起來（伺服器的名稱或版本不是這一版的、列出的工具跟這一版不同）。
- 搜尋途中 trace 寫不進去（例如磁碟滿了）。

為什麼不照模型斷線的規則「不看結果重跑」：這種失敗多半是程式缺陷或資料問題。重跑到模型剛好沒問那個查詢為止，樣本就變成照模型行為挑的，
缺陷也被蓋掉。代價是同一個世界已經跑完的 replicate 的 API 費用作廢。

**開始前就拒絕的**（`generate` 與 `search` 都查，所以不會先花掉落地或 Random／Grid 的時間）：`pack_version` 不是這一版的、
沒裝 `discovery-tools` 選用安裝、模型建不起來或丟掉 temperature（同模型臂）、client 綁不上這一版的工具（這裡會真的綁一次，
不連網；只看有沒有 `bind_tools` 這個方法不夠，langchain 的基底類別一定有，不支援時是呼叫了才丟錯）。

**盲猜組花了什麼（#433）。** 盲猜組（`arms.llm`）沒有 trace：它經 autoresearch 的 `ChatHypothesist` 提問，那一層只回文字。所以 `search` 在每個
replicate 開始時，把那個模型 client 外面包一層計數器（[`modeluse.py`](./modeluse.py) 的 `MeteredModel`；`ChatHypothesist` 本身不改、被問的內容也不變），
整個世界搜完後把每個 replicate 的呼叫次數與 token 數寫進 `model_use.json`，放在 `verdicts.json` 旁邊。`verdicts.json` 的內容不因此改變；
沒有盲猜組的世界不寫這個檔。（工具組 T6 的摘要組也是每輪問一次、沒有 trace，用的是同一個計數器、寫同一個檔；下面講「盲猜組」的規則對它同樣成立。）

- **算法跟工具組是同一條規則**，所以兩邊的數字是同一種下限、可以並排：只算「有回來、而且是文字」的回覆（文字才會被當成一次回答）；client 自己重試掉的
  只算一次；不是文字的回覆完全不算；沒回報完整用量的呼叫另外標出個數、有回報的那一半照樣加進總數；模型中途不回應而結束的 replicate 也記（錢已經花了）。
  讀用量這一步本身出錯（回覆的用量欄位讀不出來）不會讓那次回答失敗，只是記成沒量到。大於 10^12 的數字不當成用量（工具組同一條）：
  那是供應商的錯，數字夠大會讓 `report` 算花費時當掉或讓紀錄寫不出來。
- **`report`** 在盲猜組那一列印跟工具組第二行同一句 token 行（同一段程式），`--price-in`／`--price-out` 一樣適用。它沒有第一行（沒有工具可查）。
- **讀回來時會對照 `verdicts.json`**：世界與 manifest 的 digest、模型名稱（`provider/model`）、replicate 一個不多一個不少、每個 replicate 計到的呼叫次數
  等於它的 answers 數、是否以 seam 失敗結束兩邊一致。對不上就拒絕並指名哪一項。manifest 沒有盲猜組（也沒有摘要組）卻有這個檔，也拒絕。
- **舊目錄照樣讀得進來**（2026-11-22 拍板：manifest 不升版）：在有這份紀錄之前搜的世界（2026-11-10 那批）沒有這個檔，讀取時當成「沒有紀錄」。
  `report` 會在那一列寫出有幾個 search 沒有紀錄（`tokens not recorded for N search(es) of this row: no model_use.json beside their verdicts (…)`），
  不會留白讓人以為沒花錢；同一列裡有的世界有紀錄、有的沒有時，token 只加總有紀錄的，並另外寫出沒有紀錄的個數。那一行不說原因：
  沒有檔可能是舊目錄、可能是下一條講的情況、也可能是被手動刪掉，光看目錄分不出來。
- **search 當下如果計數湊不成紀錄**（計數器什麼都沒算到、次數跟 `verdicts.json` 的 answers 對不上、少了某個 replicate；照程式的走法不會發生，
  發生就是這一側的 bug）：不停下 run、不丟掉已經付錢的搜尋——只是不寫這個檔，`search` 在各臂與 oracle 的行印完之後、列出紀錄路徑之前印
  `model use: not recorded — <原因>`（之後不會有 `model use: <路徑>` 那一行）、log 留一筆 WARNING，之後那個世界讀起來跟舊目錄一樣是「沒有紀錄」，目錄本身看不出差別。
  工具組遇到同類問題會停下並保留 trace，是因為 trace 是可以查的證據；盲猜組只有計數器，沒有東西可留。
  紀錄已經湊成、但搬進目錄那一步失敗（磁碟滿、檔案被鎖）則跟其他紀錄一樣，run 會停下。

**還沒做的。** 送不出去的呼叫在 trace 裡仍然分不出是哪一種原因（T3 留下的，試跑需要時再加欄位）。

**已知的代價。** 每個 replicate 各啟動一次查詢伺服器，每次都要重新暖機（5000 根的世界約 12 秒），而各 replicate 讀到的訓練區間其實相同。
這是照計畫「每組各建」做的，沒有改成一個世界共用一個伺服器；只多花時間，不多花 API 費用。

## 不用模型的對照組（工具組 T5）

工具組如果比盲猜組好，有兩種可能：模型會推理，或者「看得到訓練區間的分數」本身就有用。這一組是為了分開兩者而設的：它用工具組的
`backtest_train` 看規則的訓練分數，每輪最多看的次數等於工具組的上限，但完全不推理。

**在「看得到什麼、能提什麼」上，它跟工具組只對齊一件事：每輪試跑的上限。下面幾點都不一樣，所以這個比較是單向的。**
（兩組用同一個 `backtest_train`、同一個讀訓練區間的函式；放在同一份 manifest 時 split、成本、預算、penalty 也相同。
2026-12 的開發批次兩組不在同一份，這幾樣要另外比對，見「判定規則（2026-12 開發批次）」。）

- 對照組只能從候選池的名單裡挑。2026-11 搜過的那批 dev 世界（`worlds-20261110`）名單是 128 條，埋入的規則本身就在名單上（那一批 20 份 conditional manifest 都是）。
  manifest 本身不保證這件事：當時另一批沒搜過的試作世界埋的是兩邊都進場的規則，就不在名單上。12 月的世界要再核對一次。
  模型面對的是整個規則語言，另外還有兩種查詢，也讀得到驗證分數的回饋。
- **工具組贏過對照組**是強的證據：它贏過了一個「答案就在名單上」的機械挑選者。
- **工具組打平或輸給對照組**不能讀成「模型不會推理」：名單本身就可能解釋這個結果。
- 工具組的上限是「最多」，模型可以少用（實際用了幾次在 `tool_use.json`）；對照組每輪都用滿，只有池子快空的最後一輪例外。
- 對照組比 Random 多做到的部分，才是「只看訓練分數、不加判斷」買到的。

這個讀法、以及雜訊世界的誤判該拿誰當基準，寫這一節時還沒定；2026-11-26 寫進了「判定規則（2026-12 開發批次）」一節。
那一節把這個比較定成只報數字：12 月那一批的結果不寫上面「強的證據」那一句，「打平或輸不能讀成模型不會推理」照舊。

manifest 的 `arms.train_pick` 有填時，`search` 在 Grid 之後、任何模型臂之前跑它：

```json
"train_pick": {"search_seeds": [0, 1, 2], "backtests": 2}
```

- `search_seeds`：每個 seed 一個 replicate。跟 Random 一樣，replicate 的編號就是 seed；store 在 `arms/train_pick/replicate-<seed>.sqlite`，
  experiment 叫 `train_pick-<seed>`。
- `backtests`：每輪抽幾條，也就是每輪在訓練區間試跑幾次（至少 1）。它要等於工具組的試跑上限（`arms.llm_tools.backtests`）；
  同一份 manifest 兩個都填而數字不同，讀 manifest 時就拒絕（工具組的試跑上限是 0 時沒有對照組可配，拒絕句會直接這樣說）。

**每一輪做什麼**（`hypothesists.TrainPickHypothesist`）：

1. 從候選池**不放回**抽 `backtests` 條。抽的順序是一份排列，由 `random.Random(f"{world_id}/train_pick/{seed}")` 決定：做法跟 Random 臂相同、
   亂數流是它自己的，所以兩臂的順序各抽各的、互相獨立（2026-11-24 拍板；共用同一份順序會把兩臂的運氣綁在一起。獨立不等於保證不同：
   池很小時兩份順序可能剛好一樣）。
2. 每一條都用 `research_tools.backtest_train` 在訓練區間試跑。直接呼叫純函式：不經 MCP、不啟動伺服器、不問任何模型。view 是從這個
   replicate 自己的 store 讀的，只讀到訓練區間最後一根；讀它的就是工具組伺服器啟動時用的同一個函式（`tools_server.load_view`），
   所以兩組讀訓練區間的方式不會走岔。
3. 交訓練分數最好的那一條。**抽到的每一條都離開池子，沒被交出去的也是**（2026-11-24 拍板；這樣它看過的不同規則最多，
   是這一類對照組裡最強的，「工具組贏過它」的結論也就最保守）。
4. 最後一輪剩不到 `backtests` 條就試剩下的；一條都不剩，就跟其他走池的臂一樣以 `exhausted` 結束。

**怎麼排「最好」**（`hypothesists.train_rank`；2026-11-24 拍板，在看到任何工具組結果之前寫死）：

| 層 | 條件 | 層內怎麼比 |
|---|---|---|
| 1（最先） | 有交易、沒爆倉 | 訓練區間的淨 Sharpe 高的先。訓練賠錢的也在這一層：它有一個可比的分數 |
| 2 | 訓練區間零交易 | 不比。Sharpe 是 0，訓練區間對這條規則什麼都沒說；同一輪沒有任何一條有交易時才會交出去 |
| 3（最後） | 爆倉（`ruined`） | 不比。爆倉的比率不讀；autoresearch 排 trial 時也把爆倉的放最後 |

同分時交先抽到的那一條。訓練賠錢但有交易的規則，排在零交易的前面。

第 2 層說的是「訓練區間看不出東西」，不是「閘門一定不過」：promote 閘門讀的是**驗證區間**，訓練區間零交易的規則在驗證區間可能有交易、
也可能過門檻。（這一節第一版把閘門寫成第 2 層的理由，是錯的；2026-11-24 更正後使用者確認排序不變。）

**試跑不算提案。** 試跑不寫 ledger、不計入 `rules_tried`、不扣預算（跟工具組的 `backtest_train` 一樣）；每輪只有交出去的那一條會被正式量測。
所以預算 30、`backtests` 2 的一次搜尋看過 60 條規則的訓練分數，正式提案 30 條。

**它不留自己的紀錄檔。** 沒有 trace、沒有 `tool_use.json`；結果跟其他臂一起在 `verdicts.json`。每輪抽到哪幾條、各自的訓練分數，可以由 seed 與那顆
store 重算出來（`tests/test_bench_pick.py` 就是這樣核對的）。

**紀錄的閘門。** 它是走池的臂：每筆 trial 都要是池裡的規則、答案數不超過 `pool_size`。跟 Random／Grid 不同的一點：它每輪可以抽好幾條（`backtests` 大於 1 時），所以池子在
`ceil(pool_size / backtests)` 個答案之後就空了，不是 `pool_size` 個。這個數由讀取端對照 manifest 核對，因為 `verdicts.json` 本身不記每輪抽幾條。

**在表上。** `calibrate`／`report` 多一列 `train_pick`，排在 Grid 之後、模型臂之前；它沒問任何模型，所以沒有 token 行。
欄位跟 Random 那一列相同，只在答案數與規則數後面多一句 `each answer the best of up to N rule(s) scored on the training window`（N＝`backtests`）：
表上的 `distinct rules median 30 (min 30)` 只算正式提案的規則，這一句說明每個答案是從幾條裡挑出來的，兩列的 N 不同時也看得出來。其他臂的列沒有這一句。

**已知的代價。** 每個 replicate 開始前多讀一次訓練區間，第一次試跑要付整趟指標計算，之後每次試跑是毫秒級。在一個 5000 根、池 128 條、預算 30 的
dev 世界上量過一次（本機 Windows，2026-11-24）：讀訓練區間 0.1 秒、第一輪 13.6 秒、其餘 29 輪共 1.8 秒，一個 replicate 大約多 15 秒，只花 CPU。

**放在哪一份 manifest、跑幾個 seed**：2026-12 的開發批次定在沒有模型臂的那一份、每個世界 5 個 seed（「判定規則（2026-12 開發批次）」一節）。它不花 API 費用，可以放進 Random／Grid 全量的那一份（`arms.llm: null`），也可以跟工具組放同一份。
這兩樣都在條件 digest 裡，所以同一批世界要寫同一份。

## 固定摘要組（工具組 T6）

`summaryarm.py`＝工具組計畫 §3.7 的候選對照組。**它還沒有用真的模型跑過；跑不跑要等工具組試跑、知道實際花費之後才決定。**
這一步做的是把定義寫死（2026-11-24，在看到任何工具組結果之前），程式與測試全部用假模型，沒有花 API 費用。

**它要分開的兩件事。** 工具組跟盲猜組最主要差兩樣：模型**看得到**訓練區間的數字，而且**自己決定**要查什麼（其他的差別見下表）。這一組只給第一樣：
每次回答前，模型的 system prompt 後面多一份固定的訓練區間摘要，然後跟盲猜組一樣一次作答。沒有工具、沒有伺服器、沒有來回。

| 比較 | 讀到的是什麼 |
|---|---|
| 摘要組 − 盲猜組 | 「看得到每個指標在訓練區間的分布」值多少。兩組只差這一樣（但見下面「長度與位置」） |
| 工具組 − 摘要組 | 好幾樣混在一起、分不開，主要是四樣：(1) 自己選要查什麼；(2) 摘要裡沒有的兩種查詢（條件成立後的報酬、整條規則的訓練區間回測）；(3) 每次呼叫前被要求先寫一行字，沒開 thinking 時等於多了打草稿的空間，摘要組跟盲猜組一樣只回一條 JSON；(4) 工具說明裡有一句用途提示，摘要的開頭沒有（見下）。另外工具組讀到的字本來就不一樣：它有自己那段工具說明，每個查詢結果後面還附一行剩餘額度 |

**工具組可能輸給摘要組。** 對任何一個指標，兩組讀到的文字相同；但**量**不同：摘要組每一輪都看得到全部 42 行，工具組每輪的查詢有上限（起點是 5 次）、查到的不留到下一輪。
只要上限比詞彙表的 42 個少，摘要組看得到分布的指標就比工具組一輪查得到的多（上限寫在 manifest，試跑後才定案），所以「工具組 − 摘要組」可以是負的，那不代表工具沒用。
這裡說的是涵蓋幾個指標，不是資訊誰多誰少：工具組另外兩種查詢回的東西（例如某個條件在幾根 bar 上成立），摘要裡沒有。

**長度與位置可能對摘要組不利。** 摘要連開頭約 9,100 字元，接在 4,385 字元的 system prompt 後面、排在一份會越來越長的「已評分規則」之前；工具組查到的東西則是它自己問完之後緊接著出現的。
42 行裡有 20 行是價格水位。如果長度或位置會讓模型分心，這一組會**低估**「看得到資料」的價值。沒有做長度相同的對照（例如放別的世界的摘要）。

**各種結果容易被讀錯成什麼。** 下面是讀法上的提醒，「比較可能是」的部分是推測、沒有驗證過。2026-11-26 起這張表不再改：「判定規則（2026-12 開發批次）」一節規定工具組對摘要組只描述、不判定，讀法就照這張表。

| 結果 | 容易讀成 | 其實只能說 |
|---|---|---|
| 摘要組 ≈ 盲猜組 | 看資料沒用 | 這一份摘要、這個長度、放在這個位置，沒有幫助。可能是各指標自己的分布本來就看不出條件式的規則，也可能是長度、位置或少了那句用途提示拖累了它 |
| 摘要組 > 盲猜組 | 模型用資料找到了規律 | 看得到分布有幫助。比較可能是門檻設在指標到得了的地方、「從未成交」的輪次變少。雜訊世界裡，會成交的規則變多也可能讓誤判變多 |
| 工具組 > 摘要組 | 自己選要查什麼很重要 | 兩組之間**所有**差別加起來對工具組有利：那四樣、它另外讀到的字、它一輪看得到分布的指標比較少（上限小於 42 時）、摘要的長度與位置。分不出是哪一項，連每一項的方向都不確定（例如摘要太長本身就可能拖累摘要組）。比較可能是另外兩種查詢 |
| 工具組 ≈ 摘要組 | 工具沒有比直接給數字好 | 不能單獨下結論：所有差別的淨效果接近零，可能是每一項都沒影響，也可能是互相抵銷 |
| 工具組 < 摘要組 | 工具有害 | 所有差別的淨效果對摘要組有利。不代表工具組多出來的那幾樣沒用 |

**摘要放什麼**（2026-11-24 拍板）：詞彙表裡**每一個**指標各一行（目前 42 行），照詞彙表的順序，沒有別的。

- **不挑指標。** 挑哪些值得給，等於替模型做了一部分搜尋，而且挑的人知道 benchmark 埋的是哪一類規則。沒選的做法：拿掉 20 個價格水位的指標
  （省將近一半的字）、另外加幾個 horizon 的基準報酬。
- **每一行就是 `feature_stats` 查詢對那個指標的回答**，跟工具伺服器回給模型的文字逐字相同（`tests/test_tools_server.py` 經 MCP 核對全部指標）：
  `feature`、`bars`、`bars_with_value`、`min`、五個分位數、`max`；`regime` 是各 label 的根數。數字取 6 位有效數字。所以同一個指標，兩組讀到的是同一段文字；差別在看得到幾個（上面「工具組可能輸給摘要組」）。
- **沒有條件報酬、沒有任何一條規則的回測。** 那些是搜尋的產物，替模型算就是替它搜（計畫 §3.7）。

**接在哪裡**（2026-11-24 拍板）：autoresearch 的 system prompt 後面，跟工具組的工具說明同一個位置；不是 user prompt 後面（計畫 §3.7 第一版寫的是 user prompt）。
摘要每輪都一樣，所以放在不變的那一段；user prompt 的最後一句仍然是「只回一條規則」。autoresearch 的兩個 prompt 函式都沒有改。

**開頭那一段只講事實、不給建議**（沿用工具組 2026-11-19 的拍板）：這些數字是訓練區間的、驗證區間仍然看不到而且正式分數在那裡量、
每輪都一樣、每個欄位是什麼意思。除了 `feature` 以外，每個欄位的解釋用的是工具伺服器說明裡的同一句（`tools_server.stats_fields()`），所以兩組讀到的欄位定義是同一份；
`feature` 那一行是摘要組自己的（伺服器寫的是「the feature asked about」，摘要裡沒有人問）。
它也回應 system prompt 那句「history you cannot see」：看得到的是訓練區間的統計，不是驗證區間。

**工具說明裡有一句用途提示，摘要的開頭沒有**（2026-11-25 拍板：不加）。`feature_stats` 的工具說明寫著「Use it before you choose a threshold: a condition only fires if the feature actually
reaches the number you compare it with.」。摘要組沒被告知這些數字拿來做什麼，所以「摘要組 − 盲猜組」可能偏小、「工具組 − 摘要組」可能偏大。
不加的理由：加了之後「摘要組 − 盲猜組」就變成「數字＋提示」，分不出是哪一個的功勞。

模型讀到的樣子（數字是示意）：

```
TRAINING-BAR FIGURES you are given before you answer:
The prompt above says a rule is scored on history you cannot see. Part of that history is described below: the training bars. …
- The figures are the same every round: they describe the training bars, and nothing you have proposed.
- Your answer is still what the prompt asks for: ONE new rule as a single JSON object, and nothing else.
Each line below is one JSON object, with:
- feature: the feature the line describes.
- bars: training bars in the window.
…
Figures are rounded to 6 significant digits.
{"feature": "close", "bars": 3000, "bars_with_value": 3000, "min": …, "p05": …, "p25": …, "p50": …, "p75": …, "p95": …, "max": …}
…（每個指標一行）
{"feature": "regime", "bars": 3000, "bars_with_value": 3000, "counts": {"trending": 1438, "ranging": 1562, "volatile": 0}}
```

**界線。** 數字來自一份 `TrainView`（只載到訓練區間最後一根），從這個 replicate 自己的 store 讀出來；讀它的就是工具組伺服器啟動時、對照組也在用的同一個函式
（`tools_server.load_view`）。`tests/test_summaryarm.py` 把訓練區間之後的 bar、日線、funding 全部改掉，摘要逐字不變（對照組：改到訓練區間內就會變）。
有指標算不出來時不跳過、不留洞（少一行就是版本號相同的另一個實驗條件）：`search` 以具名拒絕結束，這時模型一次都還沒被問。

**版本。** `SUMMARY_VERSION`（目前 1）涵蓋這個模組決定的部分：開頭那一段、列哪些指標、一個回答怎麼寫成一行、system prompt 與摘要之間隔什麼。測試釘住它的雜湊（`wording_digest()`）：
改一個字、工具伺服器那幾句欄位解釋改了、詞彙表多了一個指標、一行的寫法變了，測試都會紅，要升版本號。**雜湊不涵蓋數字本身怎麼算**（分位數怎麼內插、怎麼取到那幾位、回答實際帶哪些鍵）：
那是 `feature_stats` 查詢的事，由它自己的測試與「每一行等於伺服器回的文字」那個測試守住；那裡一改，工具組讀到的也跟著變。
（開頭那一段**怎麼說**這些事——列了哪些欄位、說取幾位——寫在那一段裡，所以在雜湊裡。）manifest 記這個數；不是這一版的 manifest，`generate` 在落地之前、`search` 在任何一臂開始之前就拒絕。
**跟工具組不同的一點：這個雜湊不寫進任何紀錄**（這一組沒有 trace）。所以版本號相同、字卻不同的兩個 build 跑出來的世界，`report` 分不出來；守住這件事的只有測試裡釘的雜湊。
模型實際讀到的摘要可以由那個 replicate 的 store 重算，`tests/test_bench_summary.py` 就是這樣核對送出去的 prompt 的。

**試跑之後這一組的字還能怎麼動**（2026-11-25 拍板）。欄位解釋借的是工具伺服器的句子，而工具說明要等真模型試跑後才凍結。規則：試跑後如果凍結時改了那幾句，
摘要組的版本號跟著升、雜湊重釘，**而且這是看過工具組結果之後唯一允許的改動**；摘要自己寫的句子、列哪些指標、接在哪裡，都不准跟著改。
沒選的做法：現在就把那幾句抄一份進來（摘要組的字從此不動，但試跑後兩組對同一個欄位讀到的解釋可能不一樣）；現在就凍結 `feature_stats` 的說明（等於放棄用試跑檢查模型有沒有誤解欄位）。

manifest 的 `arms.llm_summary` 有填時，`search` 在最後跑它（它是會花錢的臂）：

```json
"llm_summary": {"provider": "openrouter", "model": "anthropic/claude-sonnet-5.5", "temperature": 0.0,
                "replicates": 1, "world_subset": ["SYN-EB3BC030"], "summary_version": 1}
```

前五個欄位跟 `arms.llm` 相同。`llm`、`llm_tools`、`llm_summary` 一份 manifest 最多填一個：三組的比較是同一個 seed 各落地一次（跟「LLM 臂（PR 5）」一節
「一個世界兩個目錄」是同一條規則，只是現在模型臂的目錄最多三個），所以表上的一列永遠是一個臂、一組條件。store 在 `arms/llm_summary/replicate-<k>.sqlite`，experiment 叫 `llm_summary-<k>`；那個資料夾裡只有 store，沒有 trace、沒有 log。

**花了什麼。** 跟盲猜組一樣每輪問模型一次，client 外面包的是同一個計數器，寫進同一個 `model_use.json`，`report` 在 `llm_summary` 那一列印同一句 token 行
（規則見「工具組接進 benchmark（工具組 T4）」的「盲猜組花了什麼」）。一份 manifest 只有一個模型臂，所以一個目錄裡的 `model_use.json` 只會是其中一組的。

**大小與代價**（一個 5000 根的 dev 世界、本機 Windows、2026-11-24 量一次）：42 行共約 7,800 字元，開頭那一段 1,311 字元；autoresearch 的 system prompt 是 4,385 字元。
所以 system prompt 從 4,385 字元變成約 13,500 字元，**約 3.1 倍**。第一輪的 user prompt 只有幾百字元，整個輸入也接近 3 倍；之後 user prompt 隨已評分的規則變長，
倍數往下掉，30 輪平均大概在 2 倍上下（這個平均是估的，沒有量）。全部是字元數，不是 token 數：帶很多數字的 JSON 換成 token 可能更不划算。供應商有沒有 prompt caching 也還沒確認。
每個 replicate 開始前要讀一次訓練區間、把整個詞彙表的指標算一遍，那個世界上是 24 秒，只花 CPU。

**在表上。** `calibrate`／`report` 多一列 `llm_summary`。表依序照世界類型、null 定義的版本、seed set、條件 digest 排，同一組條件裡才照臂排，所以它排在**同一份 manifest**的 Random／Grid／對照組後面。
它跟 `llm`、`llm_tools` 兩列之間沒有固定的先後：三個模型臂一定在不同的 manifest、digest 不同，前三項相同時誰先誰後看的是 digest 的字母順序。

**這一組要不要跑、跟其他組怎麼比、多大的差才算數**：寫這一節時還沒定，2026-11-26 定在下一節「判定規則（2026-12 開發批次）」。

## 判定規則（2026-12 開發批次）

**2026-11-26 寫死。** 這時候工具組與摘要組都還沒有用真的模型跑過任何一個世界；跑過的只有 2026-11-10 的盲猜組，在 v1 世界、oracle 有餘裕的 14 個世界裡找回 0 個。
下面每一條都是使用者拍板的（2026-11-26），放進 repo 是為了讓 commit 的時間說明它寫在結果之前。

這是**開發用 seed** 的批次：結果是「這個做法在開發世界上的表現」；測試用 seed 仍然封著，這一節不涵蓋它。

**「開跑」＝第一次在下面 30 個世界的任何一個上用模型臂跑 `search`。** 開跑之前這一節還會補五樣（見「開跑前要補進這一節的東西」），補的 commit 要早於開跑；除此之外這一節的文字不改（工具組的程式與說明在開跑前還能改，見「試跑與凍結」）。開跑之後不看著結果改，要改照「開跑之後要改東西」那一段。

### 跑什麼

| 項目 | 定案 |
|---|---|
| 世界 | dev seed 0–9 ×（`null-v1`、`conditional-v2` tight、`conditional-v2` easy）＝30 個。跟 2026-11-10 那批同一組 seed、同樣三類，只是有埋規則的世界從 v1 換成 v2 |
| 其餘設定 | 跟那一批相同：5000 根 4h bar、預算 30（`threshold(30)`＝3.70）、`penalty` 2.0／0.5、`oracle_margin` 0.25、`theta_behavior` 0.8；`vol`、`funding.sigma`、埋的規則與 128 條的候選池也相同（「三臂與判定（PR 3）」一節 2026-11-09 校準那一段） |
| 模型臂 | 盲猜組 `arms.llm`、工具組 `arms.llm_tools`、摘要組 `arms.llm_summary`（跑不跑見下）。一份 manifest 只能有一個模型臂，所以每個世界每一組各一份；同一批 seed、同一份 `world_subset`（那 10 個 seed 的 id）；`openrouter` 的 `anthropic/claude-sonnet-5.5`、temperature 0、每個世界 1 個 replicate |
| 不花錢的臂 | 每個世界另一份沒有模型臂的 manifest：Random 與對照組 `arms.train_pick` 各跑 `search_seeds` 0–4（每個世界 5 個 seed），加上 Grid。對照組的 `backtests` 等於工具組凍結後的 `arms.llm_tools.backtests`（凍結成 0 的話沒有對照組可配，對照組那幾行就不報）。**寫結果時引用的 Random／Grid／對照組是這一份的**；模型臂那幾份 manifest 裡的 Random／Grid 不引用 |

**v2 世界的 `beta`／`price.drift`**（2026-11-12 以 40 個 dev seed 校準；定法見「世界生成器」一節，持倉 bar 的淨效果 `beta + drift` 維持 v1 各檔的 0.0025／0.004／0.008）：

| 檔 | `beta` | `price.drift` | oracle 有餘裕 | 「一直做多」過門檻 | Grid 在有餘裕的世界找回 |
|---|---|---|---|---|---|
| tight | 0.003022 | −0.000522 | 22／40 | 0／40 | 17／22 |
| easy | 0.004836 | −0.000836 | 39／40 | 0／40 | 30／39 |
| trivial（這一批不跑） | 0.009671 | −0.001671 | 40／40 | 1／40 | 38／40 |

這 120 個世界沒有一個被 `generate` 拒絕。校準是在記憶體裡算的（Random／Grid 與 oracle 都不花 API 費用），腳本不在 repo 裡。v1 那批 easy 世界「一直做多」有 7／10 過門檻；上表是同一件事在 v2 的數字。

**幾份 manifest 要對得齊**（`report` 不檢查，開跑前另外逐鍵比對）：

- 同一個世界的三份模型臂 manifest，除了 `arms` 裡的模型臂那一塊以外逐鍵相同；`arms.random` 三份相同（照 11 月那批；Grid 在 manifest 裡沒有自己的鍵），都不放 `train_pick`（它可以跟模型臂並存，這一批選擇不放）。
- 模型臂那一塊裡，五個共用的欄位（`provider`、`model`、`temperature`、`replicates`、`world_subset`）三份相同；只有各組自己的欄位不同（工具組的 `lookups`、`backtests`、`pack_version`，摘要組的 `summary_version`）。
- 沒有模型臂的那一份，除了 `arms` 以外跟它們逐鍵相同；對照組的 `backtests` 等於工具組的。兩個數不在同一份 manifest，讀 manifest 時不會替你擋。
- 每個 `conditional-v2` 世界埋的規則在對照組的候選池裡：埋的規則的 `spec_hash` 等於池裡某一條的（見「不用模型的對照組」）。這 20 個世界只差 seed 與難度檔，所以結果是全有或全無；不在的話先停下，改到在為止才開跑。

### 試跑與凍結

工具組先用真的模型跑 1 個世界：dev seed 10 的 `conditional-v2` easy 世界，落在另一個 root、`world_subset` 只有它自己。它不在上面 30 個裡，所以不必另外排除。

- **只看用法，不看找回率**：參數錯誤、誤解欄位、額度花在同一個查詢、回答格式被拒的比例、被強制作答的輪次比例、有沒有故意交不合法的回答把字帶到下一輪、有沒有第一輪就停在模型側失敗。這些從 `tool_use.json` 與 trace 讀；回答被拒的次數不在這兩個檔裡，看 `search` 當場印的那一行（`… round(s) spent on …: … measured, … duplicate, … refused`）。不對試跑的 root 下 `score`／`report`（那一列同時印找回率）；`search` 自己也會印選到的 trial 編號與它的 holdout Sharpe，以及埋的規則的驗證／holdout 數字；這幾行都不拿來決定改什麼。
- **開跑之前，工具組的任何東西都可以改**（工具說明、每輪上限、程式的錯）。改完可以再試跑，但只能用同一個世界、照樣只看用法。每一次試跑的次數與花費都公布。
- 摘要組的模型讀到的字只准跟著改一樣：跟工具伺服器共用的那幾句欄位解釋（「固定摘要組」一節）。盲猜組的模型讀到的字不改。不改變任何一組模型讀到什麼的程式修正（三組共用的，或摘要組接線的錯）可以做，照樣公布。
- **凍結＝一個 commit。** 它的雜湊、`PACK_VERSION`、工具組的 `lookups`／`backtests`、`SUMMARY_VERSION` 寫進這一節之後才開跑。三個模型臂與不花錢的那一份都用這個 commit 的程式跑（之後只改文件、不改程式的 commit 不算改動）。
- **凍結之後，三組各在這個世界跑一次，只為了量花費。** 工具組之前試跑過幾次都不算，花費估計只用這一次。token 數直接讀那個世界的 `tool_use.json`／`model_use.json` 再乘單價。以 `budget_spent` 結束的那一次才算。這一步如果發現要修的錯（摘要組到這裡才第一次碰到真的模型），修完就是新的凍結 commit：三組各重跑一次，估計只用最後一次凍結之後的那一次，之前的照樣列進試跑的次數與花費。

### 花費與摘要組跑不跑

- **估法**：每一組的估計＝它凍結之後在試跑世界跑的那一次的花費 × 30 × 1.3。紀錄裡的 token 數是下限、花費只能當量級看（「工具組接進 benchmark」一節），1.3 是為了這個留的。單價開跑前查。
- **摘要組跑不跑只看花費**：三組的估計加起來，使用者給當月的上限。合計在上限內，三組一起跑；超過，就不跑摘要組。這個決定不看任何一組的找回率，在開跑前做、寫進這一節，開跑後不改。之後才補跑的摘要組不屬於這一批，次要判定不成立。
- **逐個世界交錯跑**：同一個世界的幾個模型臂接著跑完，再換下一個世界，並記下起訖日期（manifest 釘的是 provider 與 model，不是當天路由到哪一家）。有暫停或重跑落在別的日子就寫出來。
- **跑到一半快超過上限**：停在一個世界的幾組都跑完的地方，之後用停下時那個 commit 的程式續跑，公布中斷的日期。
- `search` 以 `seam_failed` 結束照樣 exit 0（帳號類的錯也一樣），所以跑這一批的迴圈要自己從輸出認出模型臂那一行的 `: ended seam_failed`，並把它前面的 `the run stopped early: …` 那一句存下來（後者在走池的臂把池走完時也會印，不能單靠它），下面的暫停規則才做得到。

### 開跑前要補進這一節的東西

1. 凍結的 commit 與上面那幾個版本號、工具組的每輪上限。
2. 主指標分母的世界名單（見下）。
3. 三組的花費估計、當月上限、摘要組跑不跑。
4. 模型的單價，以及每一次試跑的次數與花費。
5. 這一批的設定：每一種 manifest 的條件 digest，共 12 個（3 類世界 × 4 份；摘要組不跑就是 9 個），以及一份完整的 manifest 當範本（seed 0 的 tight 世界、工具組那一份）。digest 是 `landing.conditions_digest` 算的那個，也就是 `report` 每一列方括號裡的；除了 `world_seed`／`world.id` 與 `generate` 填的 `pool_size`／`artifact_sha256`，manifest 的每一個鍵都在裡面。其餘各份跟範本只差 `world_seed`（與 `world.id`、落地後的 `artifact_sha256`）、`world.kind`（雜訊世界的 `planted` 是 null、`price.drift` 是 0）、難度檔的 `difficulty`／`beta`／`price.drift` 與 `arms`。這一批的 manifest 檔本身不進 repo。

### 主指標：找回

「找回」就是 `scores.json` 的 `recovered`。

**分母**：`conditional-v2` 世界裡 oracle 有餘裕、而且沒有被「模型側失敗的世界」那一段的規則拿掉的世界。

- 「oracle 有餘裕」＝沒有模型臂那一份的 `oracle.json` 的 `planted.passed`。模型臂那幾份的應該相同，不同就停下來查。
- 名單在開跑前用 `oracle --run` 列出、寫進這一節（oracle 不花 API 費用）。
- tight 與 easy 合成一個分母。照 40 個 seed 的校準比例是 5.5＋9.75，大約 15 個；同一組 seed 在 v1 那一批是 14 個。不設分母的下限：兩組結果不同的世界不到 5 個時，p < 0.05 不可能出現。
- 兩檔另外分開列出，但分開列的只有 W、L 與每個世界的結果，不算 p、不各自下判定。

**主要判定：工具組對盲猜組。** 逐個世界成對。只看兩組結果不同的世界：工具組找回而盲猜組沒有的算 W，反過來的算 L。用精確符號檢定、單尾：在「兩組一樣好」之下，n＝W＋L 次裡至少 W 次偏向工具組的機率 p。

| 結果 | 寫成 |
|---|---|
| p < 0.05 | 工具組找回得比盲猜組多 |
| 沒到門檻，但 W > L | 方向一致、證據不足。W、L、p 寫在同一句，不能寫成「工具組較好」 |
| W ≤ L | 沒有看到改善。單尾檢定不對「工具組較差」下判定，只列 W、L |

p < 0.05 需要的 W：L＝0 時至少 5，L＝1 時至少 7，L＝2 時至少 9。（L＝0、W＝4 是 6.25%，不算。）不管落在哪一列，W、L、分母、兩組都找回與兩組都沒找回的世界數、每個世界兩組各自的結果都列出來。

**次要判定：摘要組對盲猜組**（有跑的話），同一個算法。這是第二個檢定，各自報 p、不做多重比較的校正，結果裡寫明一共做了兩個；摘要、PR 標題、case study 引用其中一個判定時，另一個的結果寫在同一句。摘要組沒跑的話寫「只做了一個檢定；摘要組因為花費沒跑」。

**最壞情況的那一行**：主要與次要判定各自再算一次，把被拿掉的世界放回去、失敗的那一組在那個世界記成沒找回（另一組用它實際的結果）。這一次落在表上不同的一列時，引用那個判定必須兩個結果一起寫。這一行把失敗算在失敗的那一組頭上，不是對「工具組較好」的保守界：盲猜組失敗而工具組找回的世界，放回去會多一個 W。

**只描述、不判定的比較**（不算 p、不寫誰贏）：

- **工具組對摘要組。** 報 W、L 與每個世界的結果。讀法照「固定摘要組」一節那張「各種結果容易被讀錯成什麼」的表，那張表 2026-11-26 起不再改：量到的是兩組之間所有差別的淨效果，往哪一邊都不能歸給其中某一項。
- **工具組對對照組（`train_pick`）。** 每個世界，工具組是 0 或 1，對照組與 Random 是 5 個 seed 裡找回的比例。報三個跨世界的平均差：「工具組 − 對照組」「工具組 − 盲猜組」「對照組 − Random」，三個都在主指標的分母上、用同一組世界算（工具組或盲猜組被拿掉的世界三個都不算）。只報數字。這個比較是單向的（理由在「不用模型的對照組」一節）：打平或較低不能讀成「模型不會推理」。

### 純雜訊世界：誤判

10 個 `null-v1` 世界。誤判＝在沒有規律的世界選出了規則（那次搜尋的 `selected` 不是空的；`report` 的「false calls」）。

- 模型臂每個世界 1 次搜尋，報誤判的世界數。Random 與對照組每個世界 5 個 seed，報誤判的搜尋數（50 次裡幾次）與 `report` 那一行「全部／部分／沒有 replicate 命中的世界數」。
- **不判定「有沒有下降」**：2026-11-10 盲猜組是 1／10，10 個世界分不出 1 個與 0 個。
- 事先只寫一條警戒線：**工具組或摘要組誤判的雜訊世界有 3 個以上，寫成「誤判偏多（k／10）」**，旁邊固定並列 12 月盲猜組在同一批世界的誤判數。照世界數算、不照比例：不管有幾個雜訊世界因為模型側失敗沒有結果（沒有結果的另外列出個數）。
- 亮了警戒線的那一組，它在找回上的結果要跟這一句寫在同一句或同一格；摘要、PR 標題、case study 引用主要或次要判定時都要帶。

Random／Grid／對照組是地板的參考（它們不讀 prompt，也爬不了驗證分數）。對照組如果明顯不同於 Random，當成「只看訓練分數挑」本身的發現來報，不拿來當模型臂的基準。

### 每一組都報、不進判定的數字

- 供應商回報的 token 數與估算花費（`report` 的 token 行）。成本跟表現寫在同一張表上。
- 從未成交的比例：訓練與驗證區間的交易數都是 0 的 trial 數 ÷ trial 數，由 `verdicts.json` 算。
- 沒有變成 trial 的回答比例（被拒絕的＋重複的）：（回答數 − trial 數）÷ 回答數，由 `verdicts.json` 算。被拒絕與重複要分開的話得讀各臂 store 的 ledger，所以這一批的 `arms/` 在結果寫完之前不裁掉。
- 工具組另外報被強制作答的輪次比例與每輪實際用了幾次查詢（`report` 的工具組那一行）。

`report` 不印中間那兩個比例，要另外算：每次搜尋各算一個，報各組的中位數與最小、最大值；只算跑完的搜尋（以 `seam_failed` 結束的不算），分母是 0 的也不算、另外列出個數。

### 模型側失敗的世界

模型側失敗＝某個 replicate 以 `seam_failed` 結束，而且錯誤不是帳號類的（見下面「帳號那一邊的問題」那一段）。紀錄分不出這兩種：金鑰失效或額度用完一樣記成 `seam_failed`，要看 `search` 當場印的 `the run stopped early: …` 那一句，所以每次失敗都把那一句存下來。

它包含兩類：供應商那一邊的（斷線、重試後仍失敗），以及模型自己的輸出造成的（回覆讀不懂、工具呼叫的參數被截斷而讓供應商回 400；見「會用工具的提問器」一節）。後一類在 temperature 0 之下重跑很可能再發生一次，所以才有上面「最壞情況的那一行」。

**分到哪一類只憑那一句錯誤訊息**（工具組再加 trace 的 `failed` 那一筆），在讀那個世界別組的結果之前分，依據跟著公布：

| 看到什麼 | 算哪一類 |
|---|---|
| 401／402／403，或訊息寫明金鑰無效、餘額或金鑰的額度用完 | 帳號類 |
| 連線錯誤、逾時、408／409／429、5xx；OpenRouter 回 400 而訊息列出的上游狀態碼全是 408／409／429／5xx | 供應商那一邊 |
| 其餘的 400、回覆讀不懂 | 模型自己的輸出 |

由上往下，先符合的那一列為準。分不出來的一律算模型自己的輸出。

1. 那一組在那個世界**整個重跑一次**，不看別組在那個世界的結果、一律重跑，排在 30 個世界都跑過第一遍之後。以重跑的結果為準，不管第一次已經印出什麼。以 `budget_spent` 結束的世界一律不重跑（「開跑之後要改東西」第一列的整批重跑除外）。
2. 重跑又是模型側失敗：那個世界從所有用到這一組的成對比較裡拿掉。別組之間的比較不受影響。雜訊世界也照這兩步，兩次都失敗的就是「沒有結果」。
3. 每一組公布：失敗幾次（照三類分開列：供應商那一邊、模型自己的輸出、帳號類；最後一類不算模型側失敗）、重跑幾次、拿掉哪些世界。工具組另有 trace 的 `failed` 那一筆可以對。
4. 重跑前要移開哪些檔，見「LLM 臂（PR 5）」與「工具組接進 benchmark」；摘要組的世界跟盲猜組一樣，多一個 `model_use.json`。把那些檔搬到別處、不刪，第 3 點的數字才查得到。
5. 中斷（手動停下、當機）不算模型側失敗，可以重跑，但次數照樣公布；不得因為看到中途的輸出而中斷。

**帳號那一邊的問題不算模型側失敗。** 錯誤是金鑰、額度、帳號類的：整批停下來，排除後那些世界重跑，不佔第 1 點那一次。

**同一組連續兩個世界都以 `seam_failed` 結束時也先停下來查**，看的是錯誤訊息、不是找回與否：

- 帳號類的，或供應商整段斷線（同一組跨兩個以上的世界）：排除或恢復後那些世界重跑，不佔第 1 點那一次。同一個世界裡不同組先後失敗不算整段斷線，各佔自己那一次。
- 模型自己的輸出造成的：照第 1、2 點，各自只有那一次重跑。
- 要改工具組才避得開的：照「開跑之後要改東西」第一列。

暫停了幾次、各停多久、各是哪一類，照實公布。

這一批用這幾條取代先前「讀表前把 seam 失敗的世界全部重跑」（重跑到成功為止）：工具組每輪呼叫模型的次數最多可以是盲猜組的數倍，沒有上限的重跑等於照模型的行為挑樣本，花費也沒有上限。

### 開跑之後要改東西

工具側停下（exit 3）不是模型側失敗，照舊停下來查。查完，或者跑完之後想改設定再跑，分三種：

| 情況 | 怎麼做 |
|---|---|
| 改到模型讀到的東西、評分方式，或任何一份 manifest 的鍵（工具說明、每輪上限、摘要、prompt、世界生成器、評分、模型、預算、門檻） | 三個模型臂的 30 個世界全部重跑，不花錢的那一份也重跑，並在這一節寫明改了什麼、為什麼。**這份判定規則預先寫下的結果只有第一批的**（見下） |
| 不改上面任何一樣的程式修正 | 只重跑停下的那個世界的那一組；還沒跑的世界用修好的 commit 跑。記下兩個 commit 與各自跑了哪些世界 |
| 查不出可以修的 | 那個世界照兩次失敗的規則，從用到那一組的成對比較裡拿掉並公布；最壞情況的那一行照樣把它放回去、那一組記成沒找回 |

算第一列還是第二列，先看這幾樣有沒有動：`PACK_VERSION`／`SUMMARY_VERSION` 或測試釘在它們旁邊的雜湊、autoresearch 的 prompt、查詢回答的算法、世界生成器、評分、任何一份 manifest。任何一樣動了就是第一列。都沒動時再問一題：同一個世界、模型回同樣的字，修正前後任何一組模型讀到的字、或任何一條規則量出來的數字（parser 收不收、evaluator 的分數、提問器怎麼排字與計數、client 送出前怎麼改寫）會不會不同？會，就是第一列。兩題都是「不會」才算第二列；分不出來算第一列。

**只有第一批是事先寫好規則的結果。** 第一批的結果完整公布；沒跑完就停的話，寫「第一批沒跑完（k 個世界）」、列出已跑完世界的 W、L，不算 p。之後每一批照主要判定那張「結果／寫成」表寫，一律標明「看過第一批之後改的」；摘要、PR 標題、case study 引用任何一批的判定時，第一批的結果寫在同一句、放在前面。

### 這一批不做

- 只有 `feature_stats` 的工具組（manifest 目前也表達不出來）。
- 長度相同的安慰劑摘要（放別的世界的摘要）。
- 測試用 seed。
- 多模型對照：那是另一批（不帶工具的盲猜版），判讀另外寫。

### 寫結果時要一起寫的限制

- 沒開 thinking 時盲猜組與摘要組只回一條 JSON，工具組每次呼叫前被要求先寫一行字，等於多了打草稿的空間；沒有對照組分開這一點。
- 工具組的說明、上限與程式是看過試跑那個世界之後定的；試跑了幾次要寫出來。
- 主指標的分母大約 15 個世界，只看得出大的差別：盲猜組一個都沒找回時，工具組要多找回 5 個才算數。
- 同一個 seed 的 tight 與 easy 世界共用同一個 id 與同一組雜訊抽樣，所以成對的世界數大於獨立的 seed 數（10）；符號檢定把它們當成各自獨立。
- 對照組的候選池本身含埋的規則；模型面對的是整個規則語言。
- 這些規則沒有程式在執行：成對的 W、L 與 p 是另外從各世界的 `scores.json` 算的。
- 這一節沒有列的分析（別的 θ 下的找回數、方向一致率、結構距離、holdout Sharpe、別的檢定或別的分母）都算事後探索：可以報，但每一個都標明「事後、不在判定規則裡」，不寫進摘要、PR 標題或 case study 的結論句，也不拿來改主要判定那張「結果／寫成」表的措辭。這一節寫明「不算 p」「不判定」的地方（分檔、只描述的比較、雜訊世界有沒有下降、沒跑完的第一批）不在此列：那些不算、也不報。

## 評分（PR 4）

`score --run <dir>`（`scoring.score_world`，計畫 §4.6）回答「選到的規則是不是埋的那條」，三項分開報、不合成一個數：

| 指標 | 定義 | 用途 |
|---|---|---|
| 結構距離 `structural_distance` | 每個兩邊都有的 side 上，把兩條規則的條件（`entry.<side>` ＋ `filters`，同子句放哪都算一條；feature-to-feature 比較正規化方向）貪婪配對，每對的相似度＝（period 與 offset 相同 + op 相同 + 門檻接近度）／3（feature kind 不同＝0；接近度＝`1 − |a−b|/max(|a|,|b|)`，夾在 0，標籤或 feature 相同＝1），除以較長的條件數；再對「任一規則進場的 side」取平均（只有一邊進場的 side 相似度記 0、仍計入平均），距離＝1 − 相似度。exit 不讀（持倉長短看持倉方向一致率）；只差門檻的兩條規則距離最多 1/3，小差異別過度解讀 | 只報不判 |
| 持倉方向一致率 `agreement` | 在 holdout 上逐 bar 比方向 ∈ {−1, 0, +1}，**只算至少一方有倉的 bar**；雙方全程空倉＝`undefined` | 主要判準 |
| holdout 表現 | selected 的 holdout net sharpe，對照埋的規則的 | 判準之一＋對照 |

**持倉的展開**（`positions`）從 holdout 的 `SegmentResult.trades` 來，每筆交易持有 `Trade.bars_held` 根：一般出場（exit 規則／`max_bars`／反手）在
exit bar 開盤成交、**exit bar 不算持有**；窗口末端平倉（`segment_end`）與 `ruin` 在收盤成交、**exit bar 算持有**；反手的 exit bar 歸新方向。不用
`replay_sides`（它回的是收盤決策後的方向，差一根，也不模擬破產與窗口末端平倉）。

**找回**（`recovered`）＝`agreement ≥ scoring.recovery.theta_behavior` **且**（`require_positive_holdout` 時）selected 的 holdout net
sharpe ＞ 0、holdout 沒有破產（破產的比率不讀，autoresearch 自己的規則）。沒選到規則、`undefined` 都不算找回、留在分母；`seam_failed` 的臂沒有跑完搜尋，跟 calibrate 一樣另計、不進分母。Null 世界沒有東西可找回，`recovered` 為 `null`，
selected 就是一次誤判（holdout 數字照報）。

**不憑空量測**：紀錄裡已經有每個 holdout 數字（selected 的由 `promote` 寫進 `verdicts.json`、埋的規則的由 oracle 寫進 `oracle.json`），評分重讀 holdout
只是為了拿 trades，重讀的數字跟紀錄不是逐欄相等就拒絕（store 不是寫出那份紀錄的 store）。讀取走 oracle 的路徑（不落 ledger）、在 Grid 臂的 store 上、一個 frame、同一條規則只算一次；
Null 世界與沒有任何臂選到規則的世界不開 store。`scores.json` 是紀錄與 store 的純函式，`score` 可以重跑、寫出同樣的 bytes。`manifest.scoring.version` 不是這版的 `"1"` 就拒絕（`generate` 也拒絕，否則會白花一次僅此一次的搜尋）。test seed 的 console 不印任何由 holdout 算出的東西（net sharpe、方向一致率、是否找回），`scores.json` 照寫。

`report [--root <dir>] [--unseal]`（`scoring.report_table`）走 root 下每個搜過的世界，按 calibrate 同一組鍵（kind、null 定義、seed set、條件 digest、臂）分列，主數字按**每次搜尋**算（與 calibrate 一致），並印「k 次搜尋、W 個世界」：
conditional 世界印找回率、oracle 有餘裕的世界比例（**按世界算**；計畫 §4.2：**oracle 沒過也留著**）、「oracle 有餘裕時的找回率」（按搜尋）、沒選到與 `undefined` 的數量、一致率中位數、結構距離平均、selected 與埋的規則的 holdout 平均（配對：兩邊都沒破產才算，印配對數；破產的比率不讀）、首次過門檻輪數中位數（診斷）；
Null 世界印「false calls」（誤判率）、誤判者的 holdout net sharpe 平均與首次過門檻輪數中位數。同一世界有多個 replicate 時（Random）多印一行「全部／部分／沒有 replicate 命中的世界數」，分開「世界難」與「這次運氣差」。**開發 seed 的列**多印一行「在各 θ 下的找回數」（0.5–0.9），θ_b 從這行定（`theta_behavior` 在 manifest 的 digest 裡，靠改它試值等於開新目錄、重搜）。
**測試 seed 的列預設封印**：只印搜尋與世界數、oracle 有餘裕的比例（validation）、選到規則或誤判的比例、首次過門檻輪數，由 holdout 算出的一律不印；`--unseal` 才印（計畫 §4.2：讀 test 結果是一個刻意的動作）；θ 那行即使 `--unseal` 也不印。`report` 不開任何 store（評過分的世界可以裁掉 stores）；搜過但沒評過分的世界點名拒絕；`scores.json` 的 world／digest／評分版本／恢復規則／埋的規則 holdout／臂集合要對得上旁邊的紀錄，各臂除了一致率與它的 bar 數（要開 store 才算得出來）以外的欄位要等於由紀錄重算的結果。

```
python -m contrib.discovery score --run data/discovery/SYN-EB3BC030-c5f56953
python -m contrib.discovery report --root data/discovery
```

`scores.json`＝`{scores_version, world_id, manifest_digest, scoring_version, planted_holdout_sharpe, recovery, results: [{arm, replicate, ended, selected,
structural_distance, agreement, bars_compared, holdout_sharpe, holdout_ruined, recovered}]}`（頂層另有 `planted_holdout_ruined`）；`recovered` 是衍生值，讀回對不上就拒絕。

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
| `generate` 寫出 canonical manifest、第二次拒絕、失敗不留半成品；`generate` 拒絕這一版沒有的 scorer；`search`／`oracle`／`calibrate`／`score`／`report` 的輸出與各錯誤家族的 exit code | `tests/test_cli.py` |
| 池＝templates × grids 按 `candidate_order`、去重後編號、每個候選寫法正規（gate 在 `filters`、`max_bars`、`POOL_SIZING`、family）、parser 拒絕就 manifest 拒絕；Random 是 seed＋world 決定的排列、不放回；Grid 按編號；池走完 raise `CandidatesExhausted`＋旗標；經真的 `search()` 每個候選只 file 一次、以 `exhausted` 結束 | `tests/test_hypothesists.py` |
| 不用模型的對照組：每輪試跑的條數不超過上限、最後一輪試剩下的；抽到的規則剛好是整個池、每條一次、每條只試跑一次（沒被交出去的也離開池子）；同世界同 seed 的抽法、分數、答案都相同，換 seed、換世界、Random 臂同 seed 都是別的順序；每輪交的是訓練分數最好的那一條（手寫歷史上 seed 0 的八輪逐輪釘住：兩條都有交易取淨 Sharpe 高的、訓練賠錢但有交易的贏過零交易的、兩條都零交易取先抽到的）；排序三層（有交易沒爆倉→零交易→爆倉），後兩層不讀 Sharpe，同分取先抽到的，只有一筆交易也算有交易；手建的 `Scored`／`PickRound` 不合法就拒絕（編號、不是訓練區間的數字、同一輪重複）；評分途中 store 答不出來時那一輪什麼都不算，再問一次會重抽同一輪；`rounds` 與 `backtests` 只能讀；把訓練區間之後的資料全部改掉，每輪抽的、分數、答案都不變——訓練區間結束在午夜、結束在午夜前一根（有一根日線跨過界線）各一次，池裡加了讀日線的規則（對照組：改到訓練區間內就會變）；`_train_view` 在搜尋的 handle 還開著時讀 view，前後 store 與它的 WAL 逐位元相同；經真的 `search()`：三條規則每輪兩條，兩輪抽完（2＋1）、第三次被問時以 `exhausted` 結束，ledger 只有交出去的兩條、`rules_tried` 是 2、trial 的訓練數字等於當初排序用的那一組；store 不含 split 指名的訓練區間時是具名拒絕（`BenchError`），不是 traceback | `tests/test_hypothesists.py` |
| 對照組經 `bench` 跑完一個世界（池 3 條、預算 3、每輪 2 條）：排在 Grid 之後、自己一顆 store、沒有 trace／`tool_use.json`／`model_use.json`；Random／Grid 用完預算，對照組在 2 個答案後池空（`exhausted`，答案數既不是池大小也不是預算）而紀錄照樣讀得回來；它的 trial 等於用同一個 seed 從它自己的 store 重算出來的選擇；`calibrate`／`score`／`report` 有它那一列、沒有 token 行，兩張表都在那一列說每個答案是從最多幾條裡挑的、其他列沒有這一句、這個數放到別的臂的列上或從它的列上拿掉都被拒絕；紀錄少了它、manifest 沒指名它、它的 trial 不在池裡、池空時的答案數不對、答案數超過池子抽得出來的輪數，都拒絕；manifest 同時指名對照組與模型臂時，執行順序是 Random → Grid → 對照組 → 模型臂（不跑搜尋，直接讀 `_arms()` 排出來的清單） | `tests/test_bench_pick.py` |
| 摘要組的那一段文字與提問器：整段文字（開頭說明＋指標清單）的雜湊釘在 `SUMMARY_VERSION` 上，改一句、少一個指標、換版本號、換掉伺服器那幾句、換一行的寫法、換 system prompt 與摘要之間的空行都會變；除了 `feature` 以外每個欄位的解釋是工具伺服器說明裡的同一句、一行帶的每個鍵都有解釋、沒有多的；摘要＝詞彙表每個指標各一行、照順序、每行等於 `feature_stats` 的回答（鍵的順序也是）、`bars` 都是訓練區間的根數；把訓練區間之後的 bar／日線／funding 全部改掉，摘要逐字不變（對照組：三條序列各自改到訓練區間內就會變），訓練區間結束在午夜前一根、有一根日線跨過界線時也一樣；摘要裡沒有世界類型／planted／holdout 字樣，數字那幾行沒有 validation；有指標算不出來時不跳過、錯誤照樣丟出；提問器把摘要接在 system prompt 後面（中間空一行）、user prompt 原樣交出、每輪同一份、回的是 adapter 的文字、模型沒回應是 adapter 的 `HypothesistError`、`label` 與 `model` 是 adapter 的；手建的提問器帶的不是這一版的那段文字（沒有開頭、開頭被改、少一行、多一行、順序不對、混進別的字、某一行的鍵不是 `feature_stats` 的那一組或順序不同、regime 的格式套在數字指標上或反過來、該放數字的地方放了字／布林／NaN／沒有小數點的整數／大到不是數的數、根數不是非負整數、regime 的 label 不是詞彙表那三個；以及解析起來合格但寫法不是查詢回答那個樣子的一行：同一個鍵寫兩次、沒有空格的緊湊寫法、多塞空白、跳脫字元、行首或行尾空白、行與行之間用 CRLF。寫到幾位數不檢查）或問的不是 `ChatHypothesist` 都拒絕；寫一行時遇到 NaN 直接丟錯 | `tests/test_summaryarm.py` |
| 摘要組經 `bench` 跑完一個世界（假模型，兩個 replicate：一個回答兩次、一個第一輪就沒回應）：排在 Random／Grid 之後、每個 replicate 一顆 store、`arms/llm_summary/` 底下沒有別的檔、沒有 `tool_use.json`；可答池外規則、seam 失敗另計、ledger 記模型自己的名字；**模型實際收到的 system prompt＝autoresearch 的 system prompt＋空一行＋從這一組的 store 重算出來的摘要**（兩個 replicate 的 store 是同一份 artifact 的複本，各自重算出來的摘要相同），三次呼叫都是、user prompt 以「只回一條規則」結尾、第二輪才列出已評分的規則、對話裡沒有世界類型；`model_use.json` 的內容與盲猜組同形、讀回相等、`calibrate`／`report` 有 `llm_summary` 那一列、`report` 那一列有 token 行、給單價才印花費；紀錄跟這一組自己的搜尋對不上（呼叫次數、seam、模型、replicate）都拒絕，沒有這個檔時照樣讀並在那一列寫出沒紀錄的個數；紀錄是摘要組的而 manifest 寫的是盲猜組／沒有模型臂／replicate 較少，都拒絕；`summary_version` 不是這一版時（比這一版新或舊都算）`summary_arm()`、`_arms()`、`generate` 都拒絕且什麼都沒落地，已經落地的世界由 `search` 在任何一臂開始前拒絕、目錄維持落地時的樣子，模型都沒被問；模型建不起來時拒絕句寫的是 `arms.llm_summary`；執行順序是 Random → Grid → 對照組 → 摘要組，每個 replicate 各有自己的計數器；把兩個 replicate 開在兩顆訓練區間內容不同的 store 上，各自拿到的是自己那顆的摘要；開提問器不問模型；三張「哪些是模型臂」的清單（manifest 的鍵、bench 的臂、建模型時認的紀錄類別）互相對得上；`model_use.json` 拿去對不是這類臂的搜尋會被拒絕；哪兩個臂走 `model_use.json`，以及指名摘要組／盲猜組／工具組／沒有模型臂的四種 manifest 各算不算；不是模型臂的東西拿去建模型＝具名拒絕；store 答不出某個指標（兩種錯誤）＝具名拒絕、模型沒被問 | `tests/test_bench_summary.py` |
| 一次真的 `search`：`arms/` 兩顆 store、`verdicts.json`／`oracle.json` 讀回相等、`manifest.json` 不動且目錄名不變、artifact 位元組不變；各臂同條件、各自 store 裡只晉升 selected；oracle 在各臂之後印、holdout 有值、整池 16 個；再搜拒絕；模型臂最後跑、各自一顆 store、可答池外規則、seam 失敗另計；落地後改過的 manifest（digest 對不上目錄名）拒絕；calibrate 表（一列＝一組條件〔條件 digest〕、seam 失敗不進分母、artifact 裁掉仍可算、同一目錄只算一次、world_id／pool_size／臂集合不符與
null 雙胞胎 digest 拒絕、紀錄對 manifest 的條件／預算／池／oracle 門檻與視窗）；搜尋進行中（只有 `arms/`）拒絕且點名 staging；
staging 開不起來時 claim 歸還；搬 store 失敗或中斷時保留 staging 並點名；紀錄閘門（verdict 對不上快照、selected 非首位、holdout 段位、`eligible`／`margin`／`passed`／段名／`clearing`、`best`／`size`、版本、grid replicate、trials ≤ pool_size、row 的計數）；holdout 旗標可關；dev-only／null 的 oracle 預覽拒絕；中斷＝目錄回到落地狀態；seam 失敗記錄後下一臂照跑 | `tests/test_bench.py` |
| 三項指標各自的金標（出場原因對持倉 bar、只算有倉的 bar、未定義、結構距離的算式）；`recovered` 的判定；紀錄的閘門；一次真的評分（讀回相等、重跑同 bytes、store 不動、store 對不上紀錄就拒絕）；表（失敗留在分母、seam 另計、不開 store、沒評分拒絕、紀錄對不上拒絕） | `tests/test_scoring.py` |
| artifact 的 SHA-256 對不上 manifest（雙胞胎的 `world.sqlite` 複製錯目錄）在複製前拒絕 | `tests/test_bench.py` |
| 訓練區間查詢：`load` 的讀取上限＝訓練區間結尾 − 1；訓練區間由 split 取出，傳一個「標成 train 的區間」會被拒絕；view 拒絕帶著之後任何一根 bar／日線／funding 的 frame，也拒絕中間有洞的歷史；把最後一根訓練 bar 收盤之後的 bar、日線、funding（含 view 手上那一筆邊界上的 funding 的費率）全部改掉，每個查詢的輸出逐位元不變（對照組：4h／日線／funding 各自改到訓練區間內一點點就會變）；訓練區間不落在午夜時，跨過界線的那一天不被讀入；輸出的鍵與字串不含世界類型／planted／validation／holdout 字樣；模組只 import `upstream`；查詢前後 arm 的 sqlite 位元組相同、ledger 沒有 trial；`backtest_train`＝`evaluate_unfiled` 的 train 段；解析器拒絕的輸入回解析器原句；指標在訓練區間第一根沒有值時三個查詢都拒絕；`feature_stats` 與 `conditional_returns` 的手算金標（分位數、不重疊樣本、t 值、基準報酬）；三個紀錄型別拒絕對不上的數字 | `tests/test_research_tools.py` |
| MCP 伺服器：記憶體內客戶端列得出三個工具、每個呼叫的結果等於純函式的 dict；模型的錯回解析器原句並標成錯誤；store 答不出來＝協定層錯誤、不帶原句；指標 enum／運算子／regime／上下界等於 autoresearch 的表；schema 裡沒有 `$ref`；每個 schema 對測試裡的呼叫不比解析器窄、對形狀錯誤不比它寬；每個回答（含數字是 null 的）符合輸出 schema；暖機只跳過 store 沒有的序列、之後只有那些指標的查詢回錯，序列在卻算不出來則拒絕啟動；沒裝 SDK 在讀資料與複製前就拒絕；空檔與不是 store 的檔拒絕；只有 view 自己的拒絕被說成參數的錯；程式缺陷在新舊兩種握手下都只回固定一句；說明裡沒有「該怎麼下結論」的句子；`tool_specs()` 每次重建、互不共用；送中斷能停掉正在服務的子程序（exit 130）；`tools` 指令的 exit code 等於子程序的；每段說明有跑得動的範例與每個輸出欄位；範例不讀 funding／regime／realized vol、不用 `max_bars`；說明與 schema 的雜湊釘在版本號上；啟動參數只有五樣且過得了命令列；資料檔不存在時拒絕且不建檔；`tools` 指令拒絕測試用 seed、對著複本啟動、命令列不含埋入規則的字、artifact 位元組不變；真的 stdio 子程序（直接啟動與經 `tools` 指令各一次）回答與 view 相同、store 位元組不變；啟動失敗＝連線關閉而非卡住；伺服器模組在全新直譯器裡只載入 `research_tools`／`tools_server`／`upstream`，CLI 不需要 SDK 就能 import；同 seed 的 conditional 世界與 null 雙胞胎，回答的鍵、型別、拒絕句完全相同 | `tests/test_tools_server.py` |
| 會用工具的提問器：假模型查三次後回答，`propose()` 回那段文字、每個工具結果＝伺服器的回答＋剩餘額度、trace 筆數對得上；經真的 `search()` 兩輪照常寫 ledger；每輪從兩段 prompt 重新開始；第 6 次呼叫與第 3 次拿得到分數的試跑不執行、回固定句子（後者照算一次）；被解析器拒絕的試跑算一次呼叫、不算一次試跑；三個「不執行」的檢查有固定順序；一則訊息多個呼叫逐一計數、每個都有回覆；模型不停呼叫時改用不綁工具的模型、開新對話、只附伺服器回答過的查詢與模型寫的說明（去頭尾空白、最多 2000 字元），沒查到東西就不附；強制作答的回覆還帶工具呼叫＝接縫失敗、不交出空答案；模型的錯（指標打錯、工具不存在）回解析器原句並標成錯誤、計數；送不出去的呼叫（參數解析不了、不是物件、巢狀過深、過長、鍵不是文字；工具名稱過長）不送伺服器、回固定句子、不結束搜尋，剛好在界線上的照送；放不回對話的訊息（半個 surrogate 在參數／名稱／呼叫 id／文字裡，或參數帶 NaN）直接改成強制作答、同一則裡的呼叫都不執行、搜尋照常繼續；假模型跟真的 client 一樣拒絕編不出請求的對話；呼叫長度上限等於搜尋迴圈對回答的長度上限；`pack` 與 `label` 只能讀；store 答不出來（之後不再花模型呼叫；經真的 `search()` 不扣提案次數、不寫 refusal）、子程序中途死掉、查詢逾時、握手不回應、啟動不了、回答形狀不對、列不出工具，都是 `ToolServerError`；模型沒回應是 `HypothesistError`、下一輪可再問；回覆讀不懂（五種）也是；中斷原樣穿過；trace 每行是五種形狀之一且等於模組的 `TRACE_KEYS`、不合形狀的行拒寫且不佔序號、開頭一筆是整包文字與 schema、模型文字與工具結果過長會截斷並記原長、沒有任何一段 prompt、沒有日期、沒有失敗原句、沒有 `\r`；伺服器失敗的那一次查詢照樣寫一筆；把訓練區間之後的資料全部改掉，trace 除耗時外逐行相同；整包文字的雜湊釘在 `PACK_VERSION` 上，模組裡每個句子常數都在 `SENTENCES`；那段說明含計畫要的事實句、不含建議用字、不含世界類型／coin／日期；手建的 `Pack` 與 `Pack.of` 受同樣的檢查、交出去的工具是複本、兩張表不可改；上限不合法、client 不支援工具、`server` 是路徑、子程序沒給 log、trace 或 log 已存在（伺服器不會被啟動）或兩者同檔或資料夾不存在、伺服器名稱／版本／工具清單對不上、沒裝 SDK，都在問模型之前拒絕；真的 stdio 子程序回答與 view 相同、store 位元組不變、啟動失敗的原因寫進指定的 log；等待伺服器的方式（分段等、任務自己的逾時照樣丟出）、例外群組的拆解、關閉失敗只記 warning；模組在全新直譯器裡只載入 `research_tools`／`toolarm`／`tools_server`／`upstream`、不需要 SDK | `tests/test_toolarm.py` |
| 提問器不 import langchain 所依賴的三種形狀，對著 langchain 本身驗：`AIMessage`（含解析失敗的呼叫與用量）讀得出來；tuple＋回覆物件＋`{"role": "tool"}` dict 組成的對話轉得成 langchain 的訊息（含錯誤標記）；工具定義已經是 langchain 會送出去的函式格式；langchain 自己的假 chat model 綁上工具後走得完一輪 | `contrib/tests/test_toolarm_wire.py` |
| 工具組的 trace 讀得回來：每輪的次數與 token 數等於 trace 寫的；模型中途沒回應的搜尋保留已回答的輪次與已花的 token；兩個數缺一個（或不是 0 以上的整數）的呼叫另外計數、有回報的那一半照樣加進總數；輪次重複、跳號、沒結束、結束後還有行、伺服器側失敗、計數不合理，都拒絕；trace 檔逐行對照提問器的欄位表（不是 JSON、不是物件、種類不認得、欄位多或少、開頭不是 pack、pack 出現兩次、版本不對、讀不到檔）；`tool_use.json` 讀寫來回相等、每個欄位的拒絕、每輪不超過上限；`report` 的兩行只算跑完的搜尋的輪次、token 算全部、給了單價才印花費；單價必須是非負的有限數 | `tests/test_tooluse.py` |
| 盲猜組的用量：計數器把回覆原樣交回、只算文字回覆、少報一個數的呼叫另外計數、丟例外的呼叫不算；經計數器提問的 `ChatHypothesist` 回的文字與拒絕跟沒包時一樣；`model_use.json` 讀寫來回相等、每個欄位的拒絕；token 行跟工具組同一句；沒有紀錄的 search 會寫出個數 | `tests/test_modeluse.py` |
| 盲猜組經 `bench` 跑完一個世界（假模型）：`model_use.json` 的內容、讀回來對照 `verdicts.json`（世界、manifest、模型、replicate、呼叫次數、seam）的每一種拒絕；沒有盲猜組的世界沒有這個檔、有就拒絕；舊目錄（沒有這個檔）照樣讀、`report` 寫出沒有紀錄的個數、同一列新舊混在一起時兩者都寫；計數對不上時不寫檔並告知 | `tests/test_bench.py` |
| 工具組經 `bench` 跑完一個世界（假模型、真的 stdio 伺服器子程序、搜尋迴圈同時開著同一顆 store）：排在 Random／Grid 之後、每個 replicate 有 store／trace／伺服器 log、landed artifact 不變、ledger 記模型自己的名字、模型讀到的上限是 manifest 寫的、對話裡沒有世界類型；`tool_use.json` 逐位元等於預期、`verdicts.json` 的鍵沒有變；`calibrate`／`report` 有工具組那一列、多印的兩行與花費只出現在那一列；`report` 的兩個單價要一起給；`search` 有這個檔才印它的路徑；`tool_use.json` 跟 manifest 或 `verdicts.json` 對不上的十二種情況每個讀取端都拒絕、該有而沒有與不該有而有也拒絕；同一個世界的 Random／Grid 結果與 oracle 跟沒有工具組的那次搜尋逐鍵相同（只差 experiment 的建立時間）；伺服器中途死掉、伺服器起不來、伺服器起來後提問器才開不起來、trace 讀不回來或跟 ledger／manifest／前一個 replicate 對不上、搜尋途中 trace 寫不進去，都讓 `search` 停下（`KeptForInquiry`，經指令是 exit 3）、不寫紀錄、留下空的 `arms/`（claim）與改名成 `.search.kept-*` 的暫存資料夾（這些測試裡伺服器 log 都在，trace 是提問器開成功才有）與說明檔（接得到例外時附那個例外自己的 traceback、不附它是在處理哪個例外時發生的），拒絕是從造成停下的那個例外 raise 出來的，之後的 `search`／`calibrate`／`score`／`report` 以一般的拒絕（exit 1）擋下並指向說明檔；只移掉 `arms/`、留著暫存資料夾一樣被擋（旁邊有整套紀錄也一樣），兩樣都移掉才回到落地時的樣子；說明檔寫不進去只記 warning、暫存照樣改名並被認出來；改名失敗時留在原名、靠說明檔認；標記途中被中斷，暫存資料夾與 claim 照樣留著；名字不是 `.search.kept-*` 又沒有說明檔的暫存資料夾（被強制結束的 run 留下的）不會被說成刻意保留；Random／Grid 自己的 `OSError` 照舊原樣丟出、不保留任何東西；trace 記的模型名稱用 autoresearch 真的 `build` 產生的寫法核對；`oracle.json` 最後才搬進去；工具組跑不起來時 `search` 在任何一臂開始之前就拒絕；`pack_version` 不對在 `generate` 與 `search` 開始前就拒絕、沒裝 SDK／client 沒有 `bind_tools`／模型建不起來都具名；v3 的 run 目錄名稱等於改動前的 digest、搜出來的結果與 v4 相同、沒有被改寫成別的版本 | `tests/test_bench_tools.py` |

## Manifest 格式（v6）

示意（空字串與 `{}` 是佔位，不是可載入的值）：

```json
{
  "manifest_version": 6,
  "world": {
    "id": "SYN-EB3BC030",
    "kind": "null-v1 | conditional-v1 | conditional-v2",
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
  "arms": {"random": {"search_seeds": [0, 1]}, "llm": null, "llm_tools": null, "train_pick": null, "llm_summary": null},
  "scoring": {"version": "1", "recovery": {"theta_behavior": 0.8, "require_positive_holdout": true}},
  "null_definition_version": "v1",
  "artifact_sha256": null
}
```

與計畫 §5 的差異：`seed_set` 放在 `world` 裡（id 是 seed_set＋world_seed 的函數，`World.id` 是衍生屬性，讀檔時寫的 id
跟算出來的不符就拒絕）；Grid 臂沒有自己的區塊（它只走 `search_space` 的 `candidate_order`）；`arms.llm` 可為 `null`
（Null 校準先只跑 Random＋Grid；填了的形式是 `{"provider", "model", "temperature", "replicates", "world_subset"}`，見「LLM 臂（PR 5）」）。`costs` 是 `CostModel.to_dict()`、`penalty` 是 `Penalty.to_dict()`，不另造型別。`planted` 在 null 世界
為 `null`；conditional 世界的 `planted.spec` 必須通過 autoresearch 的 parser，每個進場側都要有 exit（exit 規則或 `max_bars`），
`beta` 嚴格為正、方向＝「有利於埋的那一側的部位」（short 側的埋法＝負的價格漂移）。`origin_ms` 必須 ≥ 1 且落在 UTC 午夜（同時在 4h 與
1d 格線上）。`indicator_lookback` 的下限＝引擎的 `MIN_INDICATOR_LOOKBACK`。`pool_size` 手寫時留 `null`，`generate` 落地時
回填成這一版枚舉出的數；已填的值不等於枚舉數就拒絕。`candidate_order` 是 enum（v2 起；v1 的自由文字沒有指名 runner 走得出來的順序，所以版本號 bump、v1 檔拒絕），
目前只有 `templates_then_grid`（定義見「三臂與判定」）。`artifact_sha256`（v3 起）手寫時留 `null`，`generate` 落地時回填成關閉後的
`world.sqlite` 的 SHA-256，所有會複製 artifact 的指令在複製前核對：同 seed 的 null 雙胞胎與 conditional 世界只差價格路徑，形狀的檢查全部會過，
這是唯一分得出來的東西；`calibrate`／`report`／`score` 不讀 artifact，不要求它存在。

`arms.llm_tools`（v4 起）可為 `null`；填了的形式是 `arms.llm` 的五個欄位加 `lookups`、`backtests`、`pack_version`
（見「工具組接進 benchmark（工具組 T4）」）。`arms.llm_tools.world_subset` 一樣必須含本世界。

`arms.train_pick`（v5 起）可為 `null`；填了的形式是 `{"search_seeds", "backtests"}`（見「不用模型的對照組（工具組 T5）」）。它可以跟任何一個
模型臂並存，也可以單獨跟 Random／Grid 一起；跟 `llm_tools` 並存時 `backtests` 要等於工具組的 `backtests`。

`arms.llm_summary`（v6 起）可為 `null`；填了的形式是 `arms.llm` 的五個欄位加 `summary_version`（見「固定摘要組（工具組 T6）」），`world_subset` 一樣必須含本世界。
**三個模型臂（`llm`、`llm_tools`、`llm_summary`）一份 manifest 最多填一個**，填了兩個以上時拒絕句會列出填了哪幾個。

**v3、v4 與 v5 的 manifest 照樣讀。** 2026-11-10 那批世界是在 v3 底下搜的，之後的結果要拿來跟它們並排。run 目錄是用 manifest 文字的 digest 命名的，
旁邊的紀錄也寫著那個 digest，所以舊版不能被「當成新版讀」：讀進來的 manifest 記得自己是第幾版（`Manifest.manifest_version`），
寫回去時鍵跟那一版寫的一模一樣（v3 的 `arms` 底下沒有 `llm_tools`、`train_pick`、`llm_summary`，v4 沒有後兩個，v5 沒有 `llm_summary`），digest 因此不變，目錄名、`verdicts.json`、
表上的列都跟以前對得上（測試釘住實際的 digest：v3 兩份 manifest、v4 三份、v5 四份，每份各兩個）。v3 的 manifest 不能指名工具組，v3 與 v4 都不能指名對照組，v6 之前的都不能指名摘要組；每一版各用自己的
鍵集合讀，寫錯版本的鍵會被拒絕。其他版本一律拒絕。

**不同版本的世界不會併成同一列。** 版本號在 manifest 的身分裡（這正是舊目錄名不變的前提），所以就算其他條件完全相同，
`calibrate`／`report` 也會把 v3、v4、v5、v6 各分成一列、各算各的比例。要把新世界加進舊的那一列，新 manifest 就要寫那一列的版本號
（`generate` 照樣讀得進來，只是不能指名那一版還沒有的臂）。

v1 候選池的兩個限制（寫在這裡，因為它們定義了 full-pool reference 的假說類）：每個 template 的 exit 只有 `max_bars`（LLM 臂可提
exit 規則，池不行）；每個 template 都有 gate（池裡沒有「只有進場」的候選）。`arms.llm.world_subset` 必須含本世界，否則讀 manifest 時就拒絕。

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
  manifest.json              世界與 benchmark 參數（generate 寫，canonical，search_space.pool_size 與 artifact_sha256 已回填；之後不再改寫）
  world.sqlite               生成器落地的 candles／funding（generate 寫；唯讀 artifact，各臂複製它）
  arms/random/replicate-<search seed>.sqlite   每個 Random replicate 自己的 store（search 寫；k＝search seed）
  arms/grid/replicate-0.sqlite                 Grid 臂的 store（確定性，只有一個 replicate）
  arms/train_pick/replicate-<search seed>.sqlite   對照組每個 replicate 自己的 store（search 寫；k＝search seed；manifest 指名才有）
  arms/llm/replicate-<k>.sqlite                每個模型 replicate 自己的 store（search 寫；k 從 0 起）
  arms/llm_summary/replicate-<k>.sqlite        摘要組每個 replicate 自己的 store（search 寫；k 從 0 起；manifest 指名才有）
  arms/llm_tools/replicate-<k>.sqlite          工具組每個 replicate 自己的 store（search 寫；k 從 0 起）
  arms/llm_tools/replicate-<k>.trace.jsonl     那個 replicate 的 trace（模型呼叫、工具呼叫、回答）
  arms/llm_tools/replicate-<k>.server.log      那個 replicate 的查詢伺服器的 stderr（正常是空的）
  verdicts.json              每臂每 replicate 的快照、最終判定、selected 的 holdout（search 寫）
  tool_use.json              工具組每個 replicate 每輪查了幾次與 token 數（search 寫；沒有工具組就沒有這個檔）
  model_use.json             盲猜組或摘要組每個 replicate 的模型呼叫次數與 token 數（search 寫；兩組都沒有就沒有這個檔，#433 之前搜的世界也沒有，search 當下計數湊不成紀錄時也沒有）
  oracle.json                各臂跑完後才寫：埋的規則三段數字＋餘裕判定、full-pool reference（search 寫）
  scores.json                每臂每 replicate 的三項指標與找回判定（score 寫，可重跑）
```

目錄用 **manifest 的 digest**（`search_space.pool_size` 與 `artifact_sha256` 兩個回填欄位置空後的 `Manifest.identity()` 的 SHA-256 前 8 碼——`generate` 回填這兩個欄位
後目錄名與手寫的 manifest 一致；`search`／`oracle`／`calibrate` 都核對目錄名＝旁邊 manifest 的 digest，落地後手改過的
manifest 會被拒；`verdicts.json`／`oracle.json` 也寫入跑的時候的 digest，calibrate 一併核對，所以改 manifest 又把目錄改名
也繞不過）而不只用 world id 命名：id 只是 seed_set＋seed 的函數
（計畫 §1 #7），同 seed 的 null 雙胞胎、或校準時同 seed 三檔 β 的三份 manifest 共用一個 id，要能並存；目錄名不進任何
prompt，store 裡的 coin 名仍是 id。與計畫 §2 寫的 `<world-id>/` 不同，記在計畫 §9。預設 `<bench-root>`＝
`<repo>/data/discovery`（`.gitignore` 已排除）。

## 不做清單

- 不改 autoresearch 的 DSL（語言本身）、評估器的行為、ledger、penalty 公式。工具組 T1 在 autoresearch 加了兩個公開的讀取函式
  （`dsl.parse_conditions`、`evaluator.condition_bars`），都只是包住原本就有的私有實作，沒有改任何既有行為。
- 不 import `contrib.replay`、`contrib.hyperliquid_perp`，不碰 paper／live 的任何 store。
- 不做 Null-v2／regime shift／價格衍生特徵的 planted 規則（第二階段）。
- 不做「首次成功就停止」協議。
- 不為 benchmark 加 OR、算術、新 feature。
