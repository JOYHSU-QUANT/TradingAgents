# replay

重放不是另一條交易路徑。

它不下單、不寫 `contrib/hyperliquid_perp/` 的 store、不動 `PROMPT_VERSION`。它只做一件事：
把 paper 交易員**已經記下**的每個決策拿出來對答案（成績單），之後再把當時的題目原樣重問
一顆別的腦袋（考古題，PR 2）。上面那句話就是這個套件的 scope 判準：任何需要把它放寬的
改動都是 scope creep，不是更大的功能。

完整設計在 local-only 的 `.claude/replay-plan-2026-09-23.md`；方向拍板在 memory
`hyperliquid-replay-scorecard-plan`。

## 為什麼是一個新套件

它是 `contrib/` 下**唯一**同時 import 兩個鄰居的套件：`hyperliquid_perp` 提供決策詞彙
（`DecisionMode`／`TargetSide`／`RiskAction`）、store 與 paper 的 fill model 參數；
`autoresearch` 提供 split（holdout 鎖）、`CostModel` 與研究 store。這條邊是單向的：
兩個鄰居都不得 import `contrib.replay`，`tests/test_upstream.py` 直接讀兩邊的 source 守著。
借了什麼一律列在 `upstream.py` 的 `BORROWED`，其他模組只從那裡 import。

C1 那條「`hyperliquid_perp` 讀 autoresearch 只走 JSON 文件、不 import」的否決是針對
**交易路徑**（prompt 段落）；一個永遠碰不到 prompt 的離線工具不是那條路徑。

## 現況（PR 1＝成績單）

```
python -m contrib.replay score --db paper_trading.db --run-id paper-BTC-7 \
    [--research-db data/autoresearch.sqlite] [--payload-root DIR] [--out DIR] [--holdout]
```

只讀 `runs`，以及每個 `decision_attempts` 列與它指到的最後一列 `ai_inputs`（題目）和
`ai_outputs`（答案），永遠不寫 paper store。**題目的單位是 decision attempt、不是 `ai_inputs` 列**：
一個 cycle 重試幾次就寫幾列 input（`#in1`／`#in2`／`#in3`），只有最後一列有答案；照列讀會把
前幾次當成同一格位的沒答題目，整個 run 被拒。attempt 在寫出任何 input 之前就失敗的 cycle
不是題目，摘要另外計數；複製 store 時還在進行中（`in_progress`）的 cycle 也不是，一樣另外計數。
最後仍 `api_failed` 的 cycle 是「沒答案的題」：只計數，不進執行面的命中與損益（2026-09-23 拍板：
成績單量的是決策的判斷力，帳戶層面的損益是 `/paper-review` 的事）。以 `migrate=False` 開 store
（report-only 指令不升級 daemon 可能持有的 store），所以落後 schema 的 store 會在開檔時被拒絕、
不會被誤讀。

### 定義（寫死在 `score.py`，改了就是改分數的意義）

- **兩個時距**：一根之後（4h）與六根之後（24h）。事後 mark＝**決策時刻** `+k×4h` 這個目標時點
  前後半根（±2h）內最近那一題的 `ai_inputs.mark_price`——按決策時刻（`ai_inputs.timestamp`）
  配對、不按列序，因為 cycle 會缺（在寫出 input 之前就 `api_failed`、停機）；也**不按 `candle_end`**，因為 paper
  排程是滾動的（下一次＝上次決策＋4h，不對齊整點）、mark 是決策當下的即時價，用已收盤 K 線的
  時戳配對會把跨過整點的 cycle 讀成缺口、把幾分鐘後的收盤價當成「4h 後」（run 3 實測；
  2026-09-23 拍板）。半根內沒有題的目標時點改讀研究 store 最近的 candle close（`--research-db`），
  CSV 會標明來源；兩題相距半根以內（含）視為配對歧義、具名拒絕。鎖只套在選中的那個候選上：
  store 裡半根內有題但它在 holdout，答案就是「沒有」，不會退而讀旁邊的研究收盤價（否則同一列
  鎖住與 `--holdout` 兩種跑法會用不同價格計分）。
- **方向**：模型的主張＝它要的 `target_side`——approved／clamped 的 `set_target` 如此，**被拒的也如此**
  （gate 把被拒記成 `maintain_current` 但保留被拒的方向與 margin，讀取器看的是保留下來的方向、不是
  `decision_mode`）；真正的 `maintain_current` 看當時倉位方向；fail-closed 那一輪沒有主張。
  `long` 命中＝mark 上漲，`short`＝下跌，`flat`（「不會動」）＝|報酬| **嚴格小於**該時距
  有事後 mark 的 train＋validation 題（含沒答案的）的中位絕對報酬；`--holdout` 打開時這個門檻
  不動；某個時距一題都搆不到時門檻是 n/a、flat 的主張在那個時距不判對錯。
- **淨損益**（權益的分數）：`exposure × 報酬 − 成本`，exposure＝`±margin_pct/100 × configured_leverage`，
  成本＝該 run 自己的 fill model（`runs.config_json` 裡 `paper_trading.execution`：taker 費率、
  `fill_model.style`、maker 費率、slippage）乘上「從決策當下的倉位換到目標倉位」的周轉量。
  **不帶倉位**：每一題都是「這個主張若持有 k 根會如何」，不是回測（plan §1／§3-4）。
- **兩種讀法，湊成 2×2**：**模型**讀法用 requested margin 與模型的方向；**執行**讀法用
  approved margin（且真的下了單）——被拒、fail-closed、落在 deadband 內沒下單的，都是「倉位不變」。
  摘要印 both／model only／rule only／neither，回答「AI 對、規則對、兩者都錯」。
- **「模型要了一個目標」（asked a target）**＝`set_target`，或被拒的那種（gate 記成 `maintain_current`
  但保留方向與 margin）。摘要的 `asked a target: N` 用這個定義，clamp 率與拒絕率以它為分母，
  信心校準也只算這些題（`confidence` 十等分，每桶模型讀法的命中率）。它與 CSV 的
  `decision_mode = set_target` 不同：後者不含被拒的。
- **對照組**：買進持有（該列的 `max_target_margin_pct` 上限）、永遠 flat、`autoresearch_bias`
  照上限交易；跨列帶倉位、部位改變時付周轉成本，**只跑在有答案的題上**（2026-09-23 拍板）。
  buy-hold 與 flat 的 n 與交易員相同；research_bias 跳過沒有 bias 的列，n 較小。
- **fail-closed 率**＝`risk_action = invalid_fail_closed`，依 `risk_reason` 分組（通常是
  `invalid_output`／`truncated_output`），分母是最終答案；同一個 cycle 內的重試另印一行
  「attempts retried N」、不併入分母（2026-09-23 拍板）。翻轉率＝執行方向由多轉空或反之。
- **Sharpe**：每題損益的平均／標準差，乘 √(一年有幾個這種時距)；少於兩題或無變異＝0
  （同評估器慣例）。§5 的門檻用 4h；24h 相鄰題目重疊、標準差被低估，照印但行尾標
  `(overlapping, ranking only)`（2026-09-23 拍板）。

### Split 與 holdout 鎖（plan §3-9）

用 `autoresearch.split.Split.by_shares`（60/20/20、holdout 最新）切整個 run 的跨度。
預設**不讀** holdout 段的列，而且任何時距的事後 mark 都不會越過 validation 的可讀上界
（否則 validation 最後一天的分數會偷看 holdout 第一天）。`--holdout` 才把它打開，摘要會印
`HOLDOUT READ`。太短切不出三段的 run 會被具名拒絕（exit 1）。

> PR 1 還沒有 ledger 可以記「誰、何時看了 holdout」——那張表隨 `replay.sqlite` 在 PR 2 來。
> 現在成績單看的是 paper 交易員自己的實際決策，鎖住的是「人挑 variant 時別對著 holdout 挑」。

### 輸出

stdout 印摘要（一行一個事實）。第一行說成本與 interval 來自 run 的 genesis 還是預設值（genesis
缺哪個區塊會點名）；`regimes` 那一行按 `prompt_version/model/context_shape` 計數，一個 run-id 橫跨
兩個 prompt 段（RUNBOOK §4）時看得出來。`--out DIR` 另寫 `<run-id>-decisions.csv`（一列一決策，
時距欄位以 `_4h`／`_24h` 結尾）與 `<run-id>-summary.txt`。只支援 4h／1d 的 run（研究 split 的
兩種 interval），其他 interval 具名拒絕。有 `--payload-root`（或 store 旁邊
就有 daemon 的 `payloads/<run-id>/`）時，摘要多一行「幾題已有 `.reports.json`」——那是 PR 3
題庫完整度的計數。

### 還沒有的

- `--replay-db`：讀 `replay.sqlite`、對 variant 做配對比較。配對檢定（`score.paired_hits`，
  McNemar 精確版）與 `Answer` 記錄型別已在，PR 2 只要把自己的答案建成同一種記錄。
- `pre_cutoff`／`post_cutoff` 標記（plan §6）：要有 variant 的 `model_cutoff` 才算得出來，PR 2。
- 帶模擬帳戶的回測（PR 3）。

## 測試

```
pytest -q contrib/replay/tests
```

夾具是一張 11 題的手寫表（`tests/conftest.py::ROWS`）：一個缺席的 cycle、一題沒答案、
每種讀法各一列。`test_score.py` 的期望值全部寫成從那張表推得出來的算式，不是程式印出來的數字。
同一張表也透過 perp 的 repository 寫進真的 store，讓讀取器對著 daemon 的編碼方式測；
另有一個往返測試把真的 gate（`parse_target_decision` → `evaluate` → `write_ai_output`）寫出來的
四種答案讀回來，釘住夾具的手寫形狀與 gate 的真形狀不會分家。
