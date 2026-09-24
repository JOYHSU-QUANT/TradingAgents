# replay

重放不是另一條交易路徑。

它不下單、不寫 `contrib/hyperliquid_perp/` 的 store、不動 `PROMPT_VERSION`。它只做一件事：
把 paper 交易員**已經記下**的每個決策拿出來對答案（成績單），再把當時的題目原樣重問
一顆別的腦袋（考古題）。上面那句話就是這個套件的 scope 判準：任何需要把它放寬的
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

## 成績單（PR 1）

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
  CSV 會標明來源；兩題相距半根以內（含）視為配對歧義、具名拒絕（實務上會撞到的情形：決策後
  一小時內重啟、reconcile 取消還活著的 plan 並立刻排下一次決策——那一題距上一題不到 2h，整個
  run 被拒，要先決定那兩題留哪一題）。鎖只套在選中的那個候選上：
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

> 這裡看的是 paper 交易員自己的實際決策，沒有 ledger：鎖住的是「人挑 variant 時別對著 holdout
> 挑」。variant 的答案看 holdout 時（`replay`／`score --replay-db`），ledger 會記「誰、何時、哪個
> variant」，見下面的考古題。

### 輸出

stdout 印摘要（一行一個事實）。第一行說成本與 interval 來自 run 的 genesis 還是預設值（genesis
缺哪個區塊會點名）；`regimes` 那一行按 `prompt_version/model/context_shape` 計數，一個 run-id 橫跨
兩個 prompt 段（RUNBOOK §4）時看得出來。`--out DIR` 另寫 `<run-id>-decisions.csv`（一列一決策，
時距欄位以 `_4h`／`_24h` 結尾）與 `<run-id>-summary.txt`。只支援 4h／1d 的 run（研究 split 的
兩種 interval），其他 interval 具名拒絕。有 `--payload-root`（或 store 旁邊
就有 daemon 的 `payloads/<run-id>/`）時，摘要多一行「幾題已有 `.reports.json`」——那是 PR 3
題庫完整度的計數。

## 考古題（PR 2＝重放）

```
python -m contrib.replay replay --db paper_trading.db --run-id paper-BTC-6 \
    --variant contrib/replay/variants/current-sonnet.yaml [--replay-db replay.sqlite] \
    [--repeats 3] [--segment train|validation|holdout] [--holdout] \
    [--payload-root DIR] [--limit N] [--dry-run]
python -m contrib.replay score --db paper_trading.db --run-id paper-BTC-6 \
    --replay-db replay.sqlite --variant current-sonnet [--against OTHER] [--include-pre-cutoff]
```

把 paper 交易員**當時被問的題目**原樣拿出來，換一顆腦袋重問一次。一題＝一次 completion，
**不跑分析師、不跑辯論**（plan §3-3）：

- system 訊息＝variant 指定的 system prompt；
- human 訊息＝payload 的 `context_text`＋`format_instructions`，用引擎自己的
  `inject_perp_context` 組起來（同一個標題；少了引擎放在上面的商品識別那一行，payload 沒存）。
  PM 當時看到的這一塊在 prompt 中段，後面還有評等表、計畫與辯論；這裡它就是整則訊息，所以
  format 區塊是模型最後讀到的。variant 的 `extra_context`（例如一條教訓）插在市場 context
  之後、format 區塊之前。

回來的文字走**同一個** `parse_target_decision`（帶這次 completion 自己的截斷判定），再用**該 run
genesis 記下的** `risk:`／`decision:` 區塊、從該列 `ai_inputs` 記錄的帳戶狀態（equity、倉位大小、
margin、leverage）重建 `CurrentPositionState`，走**同一個** `risk_gate.evaluate`。倉位不帶
（plan §3-4）：每題都從 paper 當時實際持有的倉位問。genesis 沒有 `risk`／`decision` 區塊的 run
具名拒絕（用預設值會拿 variant 去比一個那個 run 從沒有過的閘門）。

**已知且接受的兩個差異**：daemon 在答案回來後重讀一次即時 mark 才過閘門，重放用的是 input 列記
的 mark，所以剛好壓在 deadband 邊上的目標可能落到另一邊；閘門的倉位輸入從該列記的 size／margin／
leverage 重建，不是從帳本（store 已經不保留當時的帳本）。**簡單版的分數只能在 variant 之間比，
不能拿去跟 paper 的實際成績比**（形狀不同：一次 completion 不是它取代的那整張 graph）。

### variant（plan §3-5）

variant 是資料不是程式，一個 YAML（範例：`variants/current-sonnet.yaml`＝paper-BTC-6 genesis
記的模型、PM 自己的指示去掉它已經看不到的辯論）：

| 鍵 | 必填 | 意思 |
|---|---|---|
| `name` | 是 | 給人看的名字；在同一個 `replay.sqlite` 裡與 sha 一對一 |
| `model.provider`／`model.id` | 是 | 走 `tradingagents` 的 `create_llm_client`，不自己接 SDK |
| `system_prompt_path` | 是 | 相對於 YAML 檔 |
| `temperature` | 否 | 沒給＝provider 預設（跟 graph 一樣只在有設時才送） |
| `max_tokens` | 否 | 預設 8192＝paper daemon 的 completion cap；Gemini 自動換成 `max_output_tokens` |
| `extra_context` | 否 | 見上 |
| `model_cutoff` | 否 | 模型訓練截止日（YYYY-MM-DD），給 plan §6 用 |

未知的鍵具名拒絕（打錯的 `temprature` 不能被默默當成 provider 預設）。**sha＝會送到模型的東西**：
provider、model id、system prompt 的**文字**（不是路徑）、temperature、cap、extra context；改了
任何一個就是新 variant，要換名字（舊名字指向別的 sha 會被拒絕）。`model_cutoff` 不進 sha：它是
模型的事實、不是模型看到的東西，更正它不必丟掉已經付錢買到的答案（store 會更新並在 stderr 說）。

### replay.sqlite

自有的 store，三張表，**永遠不寫 paper store**：`variants`（sha 為鍵，name UNIQUE）、`answers`
（`(variant_sha, run_id, input_id, repeat)` 為鍵；存原文、parse 結果、成績單讀的每個 gate 欄位）、`ledger`
（每次看 holdout 一列：誰、何時、哪個 variant、`ask` 或 `score`）。版本在 `PRAGMA user_version`；
別人的 SQLite 檔在寫入任何東西之前就具名拒絕，新 build 寫過的 store 也拒絕。

### 重放的紀律

- **Split 與鎖**：同一個 `Split.by_shares`（60/20/20）切整個 run。預設只問 train；`--segment
  validation` 問 validation；問 holdout 要 `--segment holdout --holdout` **兩個都給**（缺一個具名
  拒絕），而且 ledger 那一列**在讀第一個 holdout payload 之前**就寫下。沒被選到的段落，payload
  **連打開都不打開**。
- **先驗再花錢**：先建 client（建不起來就在寫任何東西之前停下）；接著登記 variant、問 holdout
  時寫 ledger；然後所有題目的 payload 讀過、用 input 列記的 digest 比對（被改過的具名拒絕；
  input 列沒記 digest 的照讀不比）、閘門輸入全部重建成功，才開始問：第一次呼叫之前一毛不花。
  `--dry-run` 只做讀與驗並印出會存幾個答案（題數×repeat，扣掉已存的），不建 client、不寫任何
  東西（連 `replay.sqlite` 都不建；已存在的空檔會被具名拒絕，不會被建表）。
- **可續跑**：每個答案判完立刻寫入（各自一個 transaction）；已存的 `(題, repeat)` 永遠不再問。
  一次呼叫最多試 3 次（失敗後隔 5 秒、20 秒再試），第三次仍失敗就具名停下、已存的答案保留，
  同一個指令從停的地方接著跑。`--limit N` 限制這次最多存幾個新答案（重試的呼叫算一次）。
  usage collector 沒記到這次呼叫的答案（無從判斷是否截斷，照 daemon 的讀法當作沒截斷）另外計數印出。
- `--repeats N`（預設 3，plan §3-10）：每題每 variant 存 N 個答案。

### `score --replay-db`

同一個 `score_run`，把 paper 的答案換成 variant 的答案：**每個 repeat 一張卡**（只算那個 repeat
答過的題，沒問的題不會變成「沒答」），接著「跨 repeat 的中位數與區間」（每個時距一行：模型命中、執行命中、
每題平均執行損益——用平均不用總和，因為各 repeat 答的題數可能不同，plan §3-10），最後是**逐題配對比較**（模型讀法、McNemar 精確 p，plan §3-11）：預設跟
paper 交易員自己的答案比，`--against NAME` 改跟另一個 variant 的同一個 repeat 比。

- **cutoff（plan §6）**：variant 有 `model_cutoff` 時，決策時刻落在截止日當天或之前的題目**預設
  排除**（摘要開頭說排掉幾題），`--include-pre-cutoff` 才算進來；沒有 cutoff 就明說沒有分。
  有 `--against` 時取**兩個 variant 中較晚的**截止日：對手可能看過答案的題，配對的哪一邊都不算；
  沒填 cutoff 的那個 variant 另印一行說它沒被排除。
- `replay --dry-run` 不能用在 holdout：dry run 會讀它檢查的每個 payload，卻什麼都不寫（連 ledger
  都不寫），在 holdout 上就是一次沒記錄的偷看。
- `--holdout` 會先在 ledger 記一列 `score`，並列出這個 run 之前被看過幾次、誰看的。
- `--out DIR` 寫 `<run-id>-<variant>-decisions.csv`（多一個 `repeat` 欄）與 `-summary.txt`。

## 還沒有的

- 帶模擬帳戶的回測（PR 3）：從 `.reports.json` 起跑下半段 graph，倉位一路帶下去。
- plan §5 的驗收門檻（贏過四個對照組、`Penalty.threshold(n)`、配對 p < 0.05 且 ≥ 100 題、
  fail-closed 不高於現行）：成績單印出每一個原料，但門檻本身還沒寫成程式、也還沒拍板。

## 測試

```
pytest -q contrib/replay/tests
```

夾具是一張 11 題的手寫表（`tests/conftest.py::ROWS`）：一個缺席的 cycle、一題沒答案、
每種讀法各一列。`test_score.py` 的期望值全部寫成從那張表推得出來的算式，不是程式印出來的數字。
同一張表也透過 perp 的 repository 寫進真的 store，讓讀取器對著 daemon 的編碼方式測；
另有一個往返測試把真的 gate（`parse_target_decision` → `evaluate` → `write_ai_output`）寫出來的
四種答案讀回來，釘住夾具的手寫形狀與 gate 的真形狀不會分家。

考古題另有一個夾具（`tests/papers.py`）：一個 10 題的 run，**每個答案都是真的 gate 寫的**
（從 genesis 記的 run-6 閘門、各題自己的倉位），每題的 payload 檔都在、digest 記在 input 列上。
假模型 `Echo` 對每題說 paper 當時的模型說過的話，於是兩件事可以直接驗：重放的答案過閘門的結果
與記錄的**逐欄相同**（`test_replay.py`），以及 echo variant 的每張 repeat 卡與 paper 自己的成績單
**逐行相同**（`test_replay_score.py`）。模型那一層（`model.py`）用假的引擎介面測，不需要金鑰或網路。
