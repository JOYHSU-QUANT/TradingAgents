# autoresearch

研究雷達不是另一套交易系統。

它不下單、不持倉、不碰 `contrib/hyperliquid_perp/` 的 SQLite 或 schema，也不越過
RiskGate。它做的事只有一件：把 BTC 的歷史資料落到**自己的** store，之後（PR A3–A4）
在上面跑一個確定性的回測評估器，替 LLM 提出的擇時假說打分。唯一會流回實盤路徑的東西，
是很久以後（計畫 §7、run 6）prompt 裡一段**定性**的研究訊號，而且藏在一個預設關閉的
config 開關後面。

上面那句話就是這個套件的 scope 判準：**任何需要把它放寬的改動，都是 scope creep，不是
更大的功能。**

完整設計在 local-only 的 `.claude/autoresearch-plan-2026-09-09.md`；方向拍板的依據在
memory `hyperliquid-autoresearch-mvp-direction`。

## 現況

**PR A1＝資料落地層、PR A2＝假說寫得出來的那套語言、PR A3＝替假說打分的評估器、
PR A4＝把打分變成實驗紀錄：ledger、trial penalty、五個指令、baseline 校準（Phase A 完成點）、
PR B1（本次）＝讓模型自己提假說的那個迴圈：`research` 指令、答案預算、失敗回饋。
B1 是計畫的 Phase B 完成點。**
已經有的東西：

- 自己的 store `autoresearch.sqlite`（schema v3：歷史的 `candles`、`funding`、`series_state`，
  ledger 的 `experiments`、`trials`，加上搜尋的 `proposals`）。
- `series_state`：每條序列一列，記上一跑 fetch **實際抽到哪、為什麼停在那**。gap 掃描回答不了這件事：它以第一個 stamp 當格線原點，所以前端被截掉的序列掃起來「完全沒洞」——跟交易所真的沒更舊資料長得一模一樣。
- `fetch` 指令：由新往舊分頁抓 candles、由舊往新分頁抓 funding history，全部 upsert。
  `--resume` 讓 funding 那趟從已存的最新一筆之後開始走，而不是從 `--since` 重走。
- gap 檢查：把每個時間戳指派到最近的格位，分開回報**四種**發現——
  **洞**（中間有空格，重抓可補）、**重複格位**（兩筆落在同一格；一小時內兩筆 funding
  會把那小時的 carry 算兩次，而且 row 數看起來更健康）、**不在格線上**（重抓修不好，
  意思是交易所改了節奏，或兩種節奏被寫進同一條序列）、**形狀不對**（只有 bar 有：
  `close_time` 不在 `open_time + interval − 1 ms`；一根日 K 被寫進 4h 序列時 open
  正好落在 4h 格位上，前三種發現看不出來）。只回報，不修補。
- **封閉的 feature 詞彙表**（`vocabulary.py`）：16 個 kind、每個 kind 自己宣告它接受哪些
  period，所以整套語言列得完。`vocab` 指令印的那張表是**從 parser 查的同一張表生出來的**，
  不是手抄的——B1 要餵給 LLM 的詞彙表也走這一個函式。
- **宣告式 DSL 與它的 parser**（`dsl.py`）：假說是資料不是程式，沒有 `eval`／`exec`／任何
  自由運算式。未知欄位、未知 feature、未知 op、未來 offset 一律**具名拒絕**，句子裡帶著
  文件內的路徑（`spec.entry.long[0].right`）與該怎麼改。
- **逐 bar 的 feature 計算**（`features.py`）：每個值只由「截至該 bar 收盤」的資料算出來，
  而且是**建構上如此**——indicator 引擎拿到的 window 永遠**結束在這根 bar**，起點則往回
  `indicator_lookback` 根（預設 200＝實盤每 cycle fetch 的根數；A3 起是 frame 的參數，
  見下面「無前視是建構保證」）。
- **三個 pin 測試**（`tests/test_pins.py`）：`compute_indicators`、`classify_regime`、
  `funding_zscore` 的簽名與固定輸入的固定輸出。
- **成本模型**（`costs.py`）：taker／maker 費率、slippage、槓桿，一個 frozen 值；預設就是
  paper run 的三個數字（0.045%、5 bps、槓桿 1），pin 在 `test_pins.py`。
- **固定切分＋holdout 鎖**（`split.py`）：train／validation／holdout 按日曆切、holdout 最新；
  沒有明說 `holdout=True` 就拿不到那段，連 rows 都不從 store 讀。
- **bar 級評估器**（`evaluator.py`）：t 收盤決策、t+1 開盤成交、gross／net 兩組指標、
  regime 分桶；空的 `exit`、反向 entry、filter、`None` 四題的語意在這裡定（見下面）。

- **實驗 ledger**（`ledger.py`、`research.py`）：一個 experiment 寫死一次成本、切分、
  indicator window 與 penalty；每條**不同的規則**是一個 trial；promote 門檻隨 trial 數的
  `ln(n)` 上升；holdout 只有 promote 過的 trial 看得到，而且一次。見下面「ledger」段。
- **baseline 校準**（`baselines.py`、`tests/test_calibration.py`）：buy-and-hold、always-flat、
  高換手雜訊三個寫成本套件語言的文件，加上只能在測試裡抽的 seeded random entries（計畫 §6.6）。
- **假說迴圈**（`hypothesis.py`、`ports.Hypothesist`）：`research` 指令一次跟模型要一條規則，
  用同一個 parser 讀、同一個評估器打分，每個答案都記進 `proposals`——被拒絕的也記。
  見下面「假說迴圈」段。

**還沒有的東西**（依計畫 §5 的順序）：context bridge（C1，排 run 6）。

## 用法

`--interval` 只接受 `4h` 與 `1d`——計畫 §1 只點名這兩條，而下面那個 5000 根深度上限
讓這件事從「口味」變成「正確性」：`1h` 只到約 208 天、`15m` 約 52 天，這種序列掃起來
沒洞、看起來完全合法，實際短到 60/20/20 切分根本沒意義。

也沒有 `--network`：row 的主鍵是 `(coin, interval, open_time)`，不帶交易所，所以 testnet
跟 mainnet 的同一根 K 線 **就是同一列**，互相覆蓋而事後查不出來。要 testnet 的話正確
做法是把網路放進 store 的身分，而不是加一個只會把兩個市場掺在一起的旗標。

```bash
# 抓 BTC 4h candles ＋ funding history（從 2023-01-01 起），抓完順便掃 gap
python -m contrib.autoresearch fetch --coin BTC --interval 4h --since 2023-01-01

# 日線那條序列；funding 不分 interval，第二趟就別再抓一次
python -m contrib.autoresearch fetch --coin BTC --interval 1d --since 2023-01-01 --skip-funding

# 例行補資料：funding 從已存的最新一筆之後接著走（一兩個 request），candles 照舊重走
python -m contrib.autoresearch fetch --coin BTC --interval 4h --since 2023-01-01 --resume

# 只掃 gap，不連網；日線 backdrop 也一併掃（每個 experiment 都讀它）
python -m contrib.autoresearch gaps --coin BTC --interval 4h

# 一個假說能用哪些 feature（不開 store、不連網）
python -m contrib.autoresearch vocab

# 這份 spec 合不合法？合法的話它到底在說什麼？
python -m contrib.autoresearch validate-spec --spec rule.json

# 開一個 experiment：量出 train 從哪根開始、切 train／validation／holdout、寫死成本
# （4h、1d、funding 三條都要先 fetch 過）
python -m contrib.autoresearch experiment --name btc-4h --interval 4h

# 先看會切成什麼樣子：印切分與實際占比，什麼都不寫（第一個 experiment 會永久 pin holdout）
python -m contrib.autoresearch experiment --name btc-4h --interval 4h --dry-run

# 替一條規則打分（train＋validation），記成一個 trial；holdout 一列都不讀
python -m contrib.autoresearch evaluate --experiment btc-4h --spec rule.json

# 過了門檻才量 holdout，一個 trial 一次
python -m contrib.autoresearch promote --experiment btc-4h --trial 3

# 讀 ledger：不重算、不載入 pandas
python -m contrib.autoresearch report
python -m contrib.autoresearch report --experiment btc-4h --trial 3

# 三個 baseline 在這個 experiment 的窗口上拿幾分（不記成 trial）
python -m contrib.autoresearch calibrate --experiment btc-4h

# 讓模型自己提假說。預設一跑 10 個答案——被拒絕的、重複的都各算一個
python -m contrib.autoresearch research --experiment btc-4h \
    --provider anthropic --model claude-sonnet-5

# 先看模型會拿到什麼：印出整份 prompt，不問任何模型、什麼都不記
python -m contrib.autoresearch research --experiment btc-4h --dry-run
```

### 交易所只給得起這麼多歷史（2026-09-11 實測）

`candleSnapshot` 的 5000 根上限**不只是單次回應的上限，而是歷史深度的上限**，
這直接決定 A3／A4 能拿到多長的視窗：

| 序列 | 實測拿到 | 停在哪 | 停的原因 |
|---|---|---|---|
| BTC `4h` | 4999 根 | 2024-05-30 | **撞到交易所深度上限**（約 833 天） |
| BTC `1d` | 2214 根 | 2020-08-19 | 交易所真的沒更舊的了（完整歷史） |

所以 `--since 2023-01-01` 對 `4h` 是拿不到的；計畫 §3.4 寫的「起點＝Hyperliquid BTC perp
上市」只有 `1d` 這條序列做得到。fetch 會老實說「停在 the venue served no older
data」，不會靜默地裝作抓完了。

還有一件：連續快速要求約 44 次就會吃到 429。fetch 遇到 throttle 會等**四次**
（2s→5s→15s→30s），加上最後一次不再等的嘗試，共 **五次嘗試**；都等不到就具名
失敗。只有 throttle 會等，其他交易所錯誤一次也不重試。已寫進去的頁不會不見，重跑即可。

### 交易所的 `close_time` 比下一根的 `open_time` 早 1 ms（2026-09-12 實測）

4h 全部 4999 列 `close_time − open_time = 14399999`，1d 全部 2215 列 `= 86399999`。任何把
close 跟 open 或跟 interval 混著算的算式都得說清楚用的是哪一個：評估器算「這段該有幾筆
settlement」用 round 不用 floor，日線 backdrop 用日 K **自己的** close 過濾而不是從 open 推。
gap 掃描拿這個形狀當檢查（`constants.CANDLE_CLOSE_BEFORE_NEXT_OPEN_MS`）：`close_time`
不等於 `open_time + interval − 1` 的 bar 是第四種發現 **misshapen**，experiment 的暖機
檢查也照樣拒絕。測試夾具 `bars()`／`candles()` 從此就是這個形狀——原本是
`close = open + step`，碰邊界的測試各自手工減 1 ms，而整點 `.000` 的 settlement 正好
落在下一根的 open 上（見下面「已知取捨」）。

### funding 偶爾晚好幾分鐘才落（2026-09-14 實測）

settlement 平常晚整點 2–99 ms 落，但 2024-03-01 起的 22,254 筆裡有兩筆晚了幾分鐘：
2025-07-19 10:14:47、2025-07-27 12:01:50，各自是那一小時唯一的一筆（整點那格是空的）。
格線容忍原本是 5 秒，這兩筆因此算「不在格線上」，而含有不在格線 settlement 的窗口會被拒絕
——真實 store 上**任何 experiment 都開不起來**，重抓也修不好。現在容忍是 **20 分鐘**：這兩筆
回到自己那一小時，`gaps` 對這段歷史只剩一個真正缺的洞（2024-08-15）。小時中間的 stamp
（例如另一種節奏混進來）仍然是不在格線，已經有 settlement 的小時再晚來一筆仍然是重複格位。

### 掃描輸出跟 `reach:` 那一行

兩個指令在每條序列的 gap 報告下面，都會多印一行 `reach:`——上一跑 fetch **實際抽到哪、
為什麼停在那**。這一行是 gap 掃描答不了的那半：

```
BTC 4h candles: 4999 rows, 2024-05-30... .. 2026-09-10... - no gaps
  reach: stopped because the venue served no older data (asked from ..., venue clock ...)
```

最要緊的是區分這兩句：`the venue served no older data`（交易所真的沒更舊的，這就是完整資料）
跟 `the walk did not finish`（被交易所錯誤或 Ctrl-C 打斷，**前端缺一塊**）。兩者的 gap 報告
會長得一模一樣，因為掃描以第一個 stamp 當格線原點。另外還有 `reached the requested
start`／`reached the requested end`（正常跑完）、`hit the request limit`、
`the venue stopped moving the window`。從沒記錄過的 store 則是 `no fetch has recorded one in this store`。

所以 `gaps` 不只是「看有沒有洞」，它也是回答「上次回補是不是被打斷」的那個指令。

兩個指令也都在被要求的 interval 旁邊**一併掃日線 backdrop**：每個 experiment 都讀
`close_1d`／`sma_1d_*`，所以一個只有乾淨 4h、沒有 1d 的 store 對 A2 之後是半殘的，
而單掃 4h 只會說「no gaps」。1d 沒有 rows 時會多印一行說該用哪個指令補。

### 假說是資料，不是程式

一份 spec 是一個 JSON 文件，`dsl.py` 嚴格驗證它。沒有 `eval`、沒有 `exec`、沒有任何自由
運算式——**能寫出來的東西小到沒有洞可鑽**，這是評估器敢被最佳化的前提（計畫 §3.5）。

```json
{
  "family": "breakout",
  "entry": {"long": [{"left": "close", "op": ">", "right": "donchian_high_20"}]},
  "exit":  {"long": [{"left": "rsi_14", "op": "<", "right": {"param": "cool_off"}}], "max_bars": 30},
  "filters": [{"left": "regime", "op": "==", "right": "trending"}],
  "sizing": {"mode": "fixed_margin_fraction", "fraction": 0.25},
  "params": {"cool_off": 45}
}
```

上面這份文件 `dsl.py` 的 docstring 裡有同一份，而且**測試會把它拿去 parse**——照抄一份
範例的人第一件事就是丟給 `validate-spec`，而它的第一版正好違反了下面第 4 條。

五種「文法對、意思是空的」會被拒絕，每一種都是為了不讓一次 trial 白花：

1. **未來 offset**。offset 往回數，`-1` 會被拒絕，句子直接說那根 bar 在決策當下還沒收盤
   （計畫 §3.6a）。lag 語法本身是存在的（`close` 比 `close[1]` 是真的規則），被拒絕的是
   方向。
2. **跨單位比較**。`close > rsi_14` 文法完全正確而且毫無意義：五位數的價格對上限 100 的
   震盪指標，等於常數 true 穿了一件像樣子的衣服。同一條規則也擋掉 `close > atr_14`
   （價位 vs 距離）與 `funding_cum_24 > funding_rate`（總和 vs 單筆，實測 276/276 根成立）
   ——單位表的第一版把這兩組各放成同一個單位，等於把自己要擋的東西放了回來。
3. **對浮點數做等於**。`ema_20 == 30000` 永遠不會成立，而「永遠不觸發」會被當成「試過了」
   計分。等號只給 regime 用。
4. **沒有人讀的宣告**。`params` 裡沒被任何條件引用的鍵，以及計畫原本寫的 `features` 清單，
   都會被拒絕：**feature 集合就是條件裡出現的那些**，另寫一份只會有機會不一致。
5. **跨不過去的門檻**。`rsi_14 > 150`、`close > -1`、`ret_6 > -2` 全被拒絕。這條只擋
   **不可能**的門檻，不擋「尺度寫錯」的：`rsi_14 < 0.3`（把 0..100 當成 0..1）落在界內，
   它會過——擋那個要靠合理性區間，而一個會拒絕「罕見但真實」門檻的區間，比它省下的那次
   trial 更貴。

`family` 有三個是**可檢查的主張**（`regime_filter` 必須真的讀 regime、`funding_filter`
必須讀 funding、`vol_targeting` 必須用 vol 來 sizing），另外兩個（`breakout`、
`mean_reversion`）只是作者的意圖，這裡不量測意圖。**A4 的 per-family trial penalty
不能只看這個欄位**：改個標籤是免費的。

### 無前視是建構保證

每個 feature 的值只由「截至該 bar 收盤」的資料算出來，因為**沒有一條路徑寫得出 t+1**：
indicator 引擎拿到的 window **結束在這根 bar**（起點見下一段），日線只看 `close_time`
已經 ≤ 這根 4h bar 收盤的那些，funding 同理。測試（`test_features.py`）把 bundle 截到第 t 根再算一次，要求兩邊
完全相同——**而且截兩次**：只截 bar 抓得到「往前索引」的錯，連日線與 funding 一起截才抓得到
「去讀一根當下看不到的日 K」的錯。旁邊還有一個守門測試，要求被檢查的那根 bar 上**每個**
feature 都有值，否則一整排 `None` 會跟自己完美相符。

window 的**起點**則是另一件事：它往回 `indicator_lookback` 根，**預設** 200＝實盤每個 cycle
去 fetch 的根數（`candle_lookback`），同一根 bar 因此在這裡與那裡拿到同一個 EMA——而這句話
只在預設下成立：實盤的值是 gitignored 的 `local.yaml` 可以改的，所以 A3 把它做成 frame 的
參數、報表印出用的是哪個（計畫 §10.3）。**不滿一個完整 window 之前，每個 indicator 與
regime 都是 `None`**（2026-09-14 拍板）：短 window 算出來的 EMA 不是實盤那個數，而且日後
回補更舊的歷史會讓同一個 split 量出來的數字悄悄改變；現在它們跟其他 feature 一樣暖機、
在窗口第一根被具名拒絕。順帶一提，固定 window 也讓成本從
平方變回線性——引擎每次呼叫都重建 frame，餵不斷變長的 prefix 到第 5000 根本機實測
12 ms/bar，固定 window 是大約 2.5 ms。

有幾個地方「忠實鏡射實盤」的意思是**不要**把上游的值直接傳出來：

- **warm-up 期間的 regime**：`classify_regime` 在指標缺席時回 `RANGING`，而實盤永遠不會把
  這個答案拿給任何人看——`context_guards` 會直接拒絕整個 cycle，就是為了不讓人對著一個
  捏造的「平靜」下單。忠實的鏡射是 `None`。
- **被打分的那一筆 funding rate 不算進它自己的比較窗**：實盤傳進去的是尚未結算的當期
  rate，本來就不在 history 裡；把值折進自己的平均與標準差，會讓真正的極端值讀起來沒那麼
  極端（上游自己的註解就是這樣寫的）。
- **過期的 funding rate 與過期的日線**：兩條序列都可能比 bar 早結束。把最後一筆一直往前
  帶，等於用一次觀測去定價好幾週的 carry，或讓一個日線趨勢濾網凍結在一個數字上、永遠開著
  或永遠關著。兩邊的界限不一樣，而且不一樣是有原因的：funding 是**超過**一個 interval
  （settlement 蓋在整點之後，所以一筆「整整一個 interval 舊」的 rate 在完整序列上是正常的），
  日線是**到達**一天就算過期（K 線的時間戳是精確的，所以完整序列上最舊只會到 20 小時，
  剛好 24 小時代表該收的那根沒來）。`fetch --interval 4h` 與 `--interval 1d` 是兩次呼叫，
  所以「4h 是新的、1d 落後幾個月」是只跑其中一次的預設結果。
- **窗口回推不到的 funding z-score**：`funding_zscore_30` 需要 30 天的 settlement。store
  只有兩天時，上游那個函式的下限是 24 筆（＝一天），所以它會給你一個數字，而 7／14／30 三
  個 feature 會是同一欄。這裡直接回 `None`。這一條是**刻意不鏡射實盤**：實盤每個 cycle 自己
  抓 30 天窗，而且會把 `n=` 印進 prompt 讓讀的人看見，研究這邊沒有那個管道。

### 評估器：t 收盤決策、t+1 開盤成交

`evaluator.py` 是計畫 §3.6 那條規則做成無法繞過的形狀：迴圈在 bar t 用 `value_at` 讀
feature（它收不到未來 offset），讀到的東西變成一張 *pending* 單，迴圈走到 t+1 才用那根的
**開盤價**成交。沒有任何路徑會用決策當根的收盤價成交，所以測試抓不到東西——但
`test_evaluator.py` 還是量了：一筆 long 逐項手算（成交價、fee、slippage、逐小時 funding），
以及對整段歷史與截到 t 的歷史各跑一次、要求 t 之前的每個決定完全相同（計畫 §3.6b）。

A2 的 parser 留了四個語意缺口（計畫 §10.1），這裡一次定死：

| 缺口 | 定案 | 為什麼 |
|---|---|---|
| 空的 `exit`、也沒 `max_bars` | **抱到反向 entry 反手，或抱到窗口結束** | 「entry 不再成立就平」會讓每個沒寫 exit 的 breakout 進場下一根就出場；parser 給 `max_bars` 設上限時已經假設「never exit」是評估器認得的東西 |
| 持倉中反向 entry 成立 | **反手**（同一次成交平掉再開反向） | 最有訊號的讀法，也是 always-in 規則寫得出來的唯一讀法 |
| `filters` | **只擋進場**（含反手），變 false 不平倉；擋住的 bar 上 entry 條件照讀、`bars_conflicting`／`bars_unevaluable` 照計 | 想「regime 翻了就平」的規則寫在 `exit`，read-back 看得到；read-back 現在印 `enter only while:`。計數是訊號的性質不是閘門的（2026-09-14 拍板），否則把一條 clause 從 `entry.long` 搬到 `filters` 會改變長單策略沒變的計數 |
| 持倉中 exit／filter 的 feature 是 `None` | **不觸發，續抱，並計數**（`bars_unevaluable`） | `features.py` 對 entry 的讀法就是「None＝這裡不觸發」；exit 反過來 fail-closed 等於同一個值兩種讀法。計數是為了讓「一條規則安靜了一個月」變成數字而不是一次 hold |

其他形狀：同向 entry 持倉中不加碼；持倉的 `exit`／`max_bars` 觸發時同向 entry 仍成立，
**exit 優先**——下一根開盤平倉、那根空手、再下一根才重新進場（多付一趟來回；反向 entry
則同一次成交反手、不空手，2026-09-14 拍板保留）；long／short 同根同時成立不進場、計數
（`bars_conflicting`）；**每個窗口各自獨立**——第一根必然空手（能在那裡成交的決策是前一根
收盤的事，窗口不讀它），最後一根收盤**強制平倉**（付成本），所以一個窗口永遠不讀下一個
窗口的 bar——holdout 鎖就靠這一點；代價是 always-in 規則每個段界付一趟來回、exposure 是
`(bars − 1) / bars`，對每段每個 spec 都一樣（2026-09-14 拍板保留）；`vol_target` 只在進場時
定名目、持倉中不重新調整；權益歸零記 `ruined`、停止（看的是權益不是有沒有持倉：出場成交
那根或末根強平把權益打到零也算），不做 liquidation 引擎；DSL 本來就沒有停損停利。

**窗口就是量測範圍，對每個 spec 一樣**（計畫 §10.2）：exposure／hit rate 的分母對每個
假說都相同。warm-up 不是從 spec 推出來的（那得混 bar、day、`MAX_OFFSET_BARS`、上游
`required_candles` 四種單位），而是**量出來的**：某個 feature 在窗口第一根沒有值，整個窗口
被具名拒絕，句子裡帶著它第幾根才有值——要嘛把窗口往後移，要嘛抓更舊的歷史。

**成本**（計畫 §3.7）：gross＝mid 到 mid 的價差；net 再扣每次成交的 fee＋slippage（算在 mid
名目上，跟實盤「fee 算在滑價後的價格」差 fee×slippage，預設下是 0.00045×0.0005＝名目的千萬分之 2.25）與持倉
期間每個**逐小時** settlement 的 funding（正負號同 paper ledger：long 在正費率**付**）。
兩組都印，因為「gross 好看、net 不好看」是假說迴圈最常生出來的東西。`indicator_lookback`
現在是 frame 的參數（預設仍是實盤的 200），報表會印出用的是哪個（計畫 §10.3）。

**指標**（計畫 §3.9）：total return、Sharpe（bar 報酬年化，報表明寫 `sqrt(2190 bars/year)`）、
max drawdown、hit rate 各算 gross／net 一組；exposure、turnover（名目成交／平均權益）、
fees／slippage／funding 各自的總額、每個 regime 的 net return 分桶（一根的報酬歸到**前一根**收盤的 regime＝決定持倉當下已知
的那個；歸到自己收盤的 regime，會讓造成翻轉的那根大跌把虧損記進它剛造成的 bear，
2026-09-14 拍板）。always-flat 各指標是
**0 不是 NaN**（計畫 §6.6）。標準差為 0 時 Sharpe 一律報 0，所以 **Sharpe 0 不等於沒交易**：
每根都虧同樣金額的序列也是 0，要跟 total return 與交易數一起讀（2026-09-14 拍板，不報 ±inf）。
`ruined` 為真的窗口先濾掉再看任何比率。

**會被具名拒絕的窗口**（`EvaluationError`）：bar 有洞（計畫 §3.4：一個洞讀成格線就是兩根
相鄰 bar 之間一次巨大報酬）、少於兩根、funding 覆蓋不到九成（成本模型逐小時結算，缺 settlement
會低估 carry 而看起來完全正常；缺一兩筆則只回報 `funding_settlements_missing`）、
spec 的 feature 在第一根沒有值、窗口邊界不在 store 的格線上（手寫或 ledger 讀回的
segment 才會；`by_shares` 會貼格線）。這幾種都是「換窗口或補資料」的事，不是改 spec 的事。

### ledger：一個 experiment、很多 trial、一段 holdout（A4）

評估器回答「這條規則在這個窗口拿幾分」；ledger 回答**搜尋**規則時才冒出來的問題。

- **experiment 寫死一次**：成本（`CostModel.to_dict`）、切分（`Split.to_dict`）、
  `indicator_lookback`、penalty 參數是 `experiments` 的一列，這個 experiment 裡每個 trial 都在
  同一組條件下量。計畫 §3.3 列的 `family` 欄**沒放**（§10.6：改標籤免費，family 是 trial 的
  報表維度）；`indicator_lookback` 與 `coin` 是計畫沒列、這裡加的——兩個只差 lookback 的
  experiment 否則會寫出一模一樣的列。
- **train 從哪根開始是量的**：store 上**整張詞彙表**每一欄第一次同時有值的那根，再往後
  `MAX_OFFSET_BARS`（24）根。任何合法 spec 在 train 第一根都量得到，所以暖機拒絕永遠不會
  被當成結果記進 ledger（計畫 §11）。這需要 4h、1d、funding 三條都抓過：`sma_1d_200` 要
  約 200 天日線，`funding_zscore_30` 要 30 天 funding。
- **每個 coin 只有一段 holdout**：第一個 experiment 按比例切，再貼到最近的 UTC 午夜（4h 與 1d
  兩種格線都有這個點）；之後每個 experiment 的 holdout **一定從同一刻開始**，只能往後長。
  計畫 §11 原本寫「不得更早」，這裡改成**完全相同**：更晚的起點會把舊 holdout 放進新的
  validation，更早的起點會把舊 trial 挑選時看過的 validation 放進新的 holdout——兩邊都讓
  holdout 不再是「沒有被挑選過的歷史」。
- **門檻＝`sharpe_base + k·ln(n)`**（預設 1.0、0.25），`n` 是**這個 coin 所有 experiment**
  在 **promote 當下**試過的**不同規則**數（按 `spec_hash` 去重，`Ledger.rules_tried`）。不按
  family 算（改標籤就能歸零）、不按 experiment 算（holdout 每個 coin 一個 pin，store 沒變時換個
  名字開新 experiment 切出來是同一組窗口，n 卻會歸零；2026-09-14 拍板），也不用 trial 自己的序號
  （第 1 個 trial 在試了 500 條之後才 promote，它是從 501 條裡挑出來的）。同一條規則換成本在另一個
  experiment 重量，在那裡是一個 trial，但仍是一條規則、`n` 不加。**`sharpe_base`／`k` 也按 coin
  pin**：第一個 experiment 定下之後，同 coin 的 experiment 帶不同的 penalty 會被具名拒絕（`--dry-run`
  也會），否則 `--penalty-k 0` 就能繞過跨 experiment 的 `n`。
- **同一條規則只是一個 trial**：`spec_hash` 看規則不看文件——clause 順序、重複 clause、param
  名字、family 標籤、feature 對 feature 的比較寫在哪一邊、`30` 或 `30.0` 都不影響；門檻、op、
  offset、side、`max_bars`、sizing 會。重複的規則 `evaluate` 直接回報舊 trial，不重算、`n`
  不加（評估器是確定性的，重跑不是多看一眼）。
- **promote 門檻全部列出、不只第一個**：沒 promote 過、validation 沒 ruined、至少一筆交易、
  net 報酬 > 0、net Sharpe ≥ 門檻。Sharpe 旁邊一定讀報酬與交易數（Sharpe 0 不等於沒交易）。
  **沒有** train→validation 的退化門檻：總量指標不跨窗口比，`report` 並排印兩個 Sharpe。
- **promote 會把 train／validation 再量一次**，要跟記下來的一模一樣才去量 holdout。同一批
  bar、feature 不往後讀，所以唯一會不同的情況是 store 事後被重抓或修正——那等於門檻是用現在
  重現不出來的數字過的，具名拒絕。
- **holdout 鎖從頭到尾**：`evaluate` 讀的 bundle 只到 validation 最後一根
  （`loadable_until(holdout=False)`）；`holdout=True` 整個套件只寫在 `research.promote` 過了
  門檻之後那一處。唯一讀整個 store 的是 `experiment`——它要知道 span 才能切，而且不算任何窗口
  的分數。資料表本身也守著：`holdout_metrics_json` 有值 ⇔ `status = promoted`。
- **量之前先掃歷史**（計畫 §11）：4h bar（含窗口前的暖機）與 `sma_1d_200` 讀得到的日線，有任何
  洞／重複／不在格線就拒絕——評估器自己的窗口檢查看不到暖機。funding 的重複與不在格線拒絕，
  **洞只計數**（跟 feature 的 90% 覆蓋政策一致，交易所偶爾真的少一筆）。
- **span 的尾端也是量的**：最後 `MAX_OFFSET_BARS + 1` 根只要有任何一欄詞彙沒有值（1d 或
  funding 比 4h 早抓、停在決策 bar 之前）就拒絕，建立 experiment 時一次、promote 時在含 holdout
  的 frame 上再一次。窗口中段的「沒有值」是不觸發、不拒絕，而尾端正是 holdout——讀日線的規則
  會悄悄空手度過 holdout，不讀的不會。
- **搜尋看得到什麼**：`Ledger.search_trials` 與 `evaluate` 回報重複規則時給的是
  `SearchTrial`——只有 train／validation，**沒有** holdout 欄位（計畫 §3.11）。重送一條已
  promote 的規則不會印出它的 holdout；`report` 是操作者的視圖，照印。promote 本身是 CLI 動作。
- **holdout 被看過幾次會印出來**：`promote` 與 `report` 印「這個 coin 的 holdout 已被量過
  k 次（跨 experiment）」。只計數、不設上限；每 promote 一次就是多看一眼同一段窗口。
- **`experiment --dry-run`**：印切分、實際各窗口占比、會不會建立 pin，不寫 experiment
  （開 store 仍會把 schema 升到最新）。coin 已有 pin 時 share 旗標只決定 train:validation，
  「actual shares」那行會照實印出。
- **baseline 不是 trial**：`calibrate` 什麼都不記，門檻不動。
- **`report` 只讀 ledger**，不重算、不載入 pandas（`test_upstream.py` 的 subprocess 測試守著）；
  它印的量測文字和 `evaluate` 當下印的是同一個函式（`metrics.describe_measurement`）產生的。

### 假說迴圈：模型提、評估器打分、失敗回饋（B1）

`research` 是這個套件唯一會跟模型講話的指令。一輪的形狀是：把詞彙表與文法交給模型 → 模型回
一段文字 → 用**跟手寫 spec 同一個 parser** 讀 → 讀得出來就走 `research.measure` 打分、記成
trial → 不管結果是什麼，都記成一列 `proposals`。

- **預算算「答案」，不算「trial」**（計畫 §3.11、§10.7）。`--max-trials` 預設 10；被 parser
  拒絕的答案算一個，提出一條**已經量過的規則**也算一個。只算成功規則的預算不會停——模型一直
  回同一段壞 JSON 就永遠跑不完——而且多重比較本來要收費的就是「看了幾次」。
- **例外是 seam 失敗**。`HypothesistError`（連不上、401、回傳的不是文字）不是一個答案：不花
  預算、直接中止這一跑。否則一把壞掉的 key 可以安靜把預算燒完，還回報一場沒發生過的搜尋。
- **`proposals` 表（schema v3）**：`trials` 裝的是**規則**，這張裝的是**嘗試**。被拒絕的答案
  從來不是規則，記成 trial 會替沒人量過的東西把 promote 門檻墊高。它存原文，因為對一個被拒絕
  的答案來說，store 裡沒有別的地方留著模型到底寫了什麼。
- **prompt 裡沒有任何日期**，窗口一律用「幾根 bar」講。這是 holdout 鎖自己關不掉的那個洞：
  模型對 BTC 有自己的記憶，prompt 只要說出 validation 的日期，就等於邀請它拿場外知識去 fit
  那一段——而 penalty 收的是「試了幾條規則」的費，根本 price 不到這件事。三段窗口又是連著的，
  講 validation 的結束就等於講 holdout 的開始。
- **模型看到的是搜尋視圖**。摘要一律從 `SearchTrial` 來（那個型別沒有 holdout 欄位），迴圈
  只呼叫 `research.measure`、**不呼叫 `research.promote`**；`tests/test_hypothesis.py` 是用
  import graph 斷言這件事，不是用字串搜尋——這個模組的 docstring 自己就一直在講 promote。
- **fence 只剝「整段包起來」的那一種**。` ```json … ``` ` 是 chat 格式包上去的殼，剝掉；
  prose 夾著 JSON、兩段 fence、沒收尾的 fence，一律原樣交給 parser 拒絕——再往下就是在猜
  作者指的是哪一段文字了。
- **失敗回饋**：下一輪的 prompt 會帶上這個 experiment 最近幾條被拒絕的句子，以及已經量過的
  規則。所以「`ema_9` 不存在」只會被學一次，不是每跑一次學一次。**指標只印前 12 條
  （validation sharpe 由好到壞），但「試過哪些規則」是列完的**——重複提案要花掉一個 round，
  只印前 12 條等於拿 prompt 自己的遺漏去罰模型。
- **每個答案都記下是哪個模型講的**（`proposals.model`）。`research` 不替你猜 provider／model，
  正是同一個理由：一個 store 被兩個模型搜過之後，append-only 的 ledger 沒有第二次機會說清楚
  哪條是誰提的。
- **trial 和它的來源寫在同一個 transaction 裡**。本來是兩段：中間失敗會留下一個算進
  `rules_tried`（正確，它真的被量過）、卻沒有任何一列說它從哪來的 trial，而 `report` 的答案數
  就對不上 trial 數。重複與被拒絕的答案沒有 trial 可搭，各自單獨寫。
- **seam 失敗會回傳「跑到哪」的部分報告**：第 7 輪斷線不會把前 6 輪的摘要一起丟掉，指令照樣
  exit 1。rounds 本來就是落地的，所以重跑就等於接續。
- **Ctrl-C 也留得住部分報告，但 exit code 還是 130**。攔 `KeyboardInterrupt` 是為了把已經
  記下的 round 印出來，不是為了把「使用者自己停掉」改判成「這次跑失敗」——同一個 Ctrl-C 落在
  模型呼叫裡跟落在別的地方，退出碼必須一樣，否則拿 130 判斷「人為取消」的腳本會為了一次
  Ctrl-C 叫人起床。理由寫在 `INTERRUPTED` 常數旁邊。

## store 路徑與拒絕

預設在 repo root 的 `data/autoresearch.sqlite`，用 `--db` 改路徑。四種會被**具名拒絕**的情況：

1. 指到一個**不是**本套件 store 的 SQLite 檔（例如 paper 的 `paper_trading.db`）——不會有任何
   migration 跑在別人的檔案上，連對方的 journal mode 都不會被動到。
2. 指到一個**不是一般檔案**的路徑（例如目錄）。
3. **根本開不起來**的路徑（目錄建不出來、不是資料庫、權限不夠）——訊息會帶著 sqlite
   自己的診斷，而不是一堆 traceback。
4. 被**更新版本的 build** 寫過的 store（schema 版本比本 build 新）。

離開碼：`0` 成功、`1` 具名的操作／store／交易所／**spec**／量測／ledger 失敗（被 promote
門檻擋下的 trial 也是）、`2` argparse 自己的用法錯誤（例如 `--interval 1h`）、`130` 中斷。**store 有洞不算失敗**：掃到洞是成功掃描的結果，
exit 0；要補洞請跑 `fetch`。**被拒絕的 spec 則相反**：文件本身就是輸入，parser 不收的 spec
是不值得花一次 trial 的 spec，所以 exit 1。

## 對 `hyperliquid_perp` 的關係

唯讀，而且只從一個地方借：[`upstream.py`](./upstream.py) 列出全部借用的名字
（`BORROWED`），本套件其他模組一律經過它，不自己 import 上游——
`tests/test_upstream.py` 讀原始碼檢查這件事。

計畫 §3.2 要的 pin 測試在 [`tests/test_pins.py`](./tests/test_pins.py)：
`compute_indicators`、`classify_regime`、`funding_zscore` 三個各釘**簽名**（本套件是按位置
傳參數的）與**固定輸入的固定輸出**。這裡紅掉不自動等於上游有 bug，它的意思是「改動前後
的研究數字不是同一個量測」，該做的是把實驗重跑一次，而不是把期望值改到綠。

同一個檔還釘了兩個**數字**：indicator 引擎每根 bar 看到的 window 長度（＝實盤 fetch 的
`candle_lookback`，200），與 vol-target sizing 的預設上限（＝ RiskGate 的
`max_target_margin_pct`，60%）。兩個都是「實盤現在是這樣」的假設，錯了不會報錯，只會讓
研究數字悄悄不再是 trader 看到的那個。

這三個 analytics 是**延後 import** 的（`domains/perp/indicators` 會拉進 pandas 與
stockstats，本機實測 511 ms，而整層 store 只要 57 ms），交易所 reader 也是（會拉進
Hyperliquid SDK）。所以 `gaps`／`vocab`／`validate-spec` 三個指令一個 import 成本都不付，
而 `fetch` 只付 reader 那一份——它本來就要連線。這件事有測試守著：
`test_upstream.py` 開一個 subprocess 跑 `vocab`，回頭看 `sys.modules` 裡有沒有 pandas。
（沒有它時，在 `cli.py` 加一行 `from .features import ...` 可以讓 `gaps` 開始付 pandas 的
錢，而全套測試依然全綠。）

## 已知取捨（記著就好，不開 issue）

- **indicator 一次算四個名字**（計畫 §10.5）：評估器知道整個 experiment 的 feature 聯集，
  可以只叫 frame 算需要的；沒做，因為一個 frame 服務整個 experiment，聯集遲早全要。
  順帶：regime 分桶讓**每個** spec 都付這趟 indicator walk（5000 根本機實測約 12 秒，
  每個 frame 一次），就算 spec 一個 indicator 都沒讀；而 engine 真的壞掉時，那個
  `FeatureError` 會從一個沒讀 indicator 的 spec 冒出來。一個只對「碰巧算過」的 spec
  出現的 report 維度不是維度，所以照付。
- **gross 的分母是 net 權益路徑**：gross bar return＝該根價差損益／當時實際持有的權益，
  再複利。所以 `gross.total_return` 不等於各筆 `gross_pnl` 的總和；兩組指標同分母，
  差的只有成本項，這才是「gross 好看、net 不好看」要比的東西。
- **`_notional` 的 `None` 分支實務上到不了**：`realized_vol_N` 只在 warm-up 是 `None`，而
  warm-up 在窗口第一根就被拒絕；留著是型別上的完整，不是行為。
- **短於 `indicator_lookback` 根（預設 200）的 bundle，regime 分桶全記 `unlabelled`**：那是
  report 的維度不是 spec 的 feature，對它套「整欄 None 就拒絕」會讓每個小測試都得先餵一整個
  window。
- **`spec_hash` 懂文法、不懂評估器語意**：只做多的 spec 把一條 clause 從 `entry.long` 搬到
  `filters`，交易一模一樣，hash 卻不同。要正規化這個得用評估器的語意，留成「第二個 trial」。
- **每個 coin 只有一段 holdout，而且只往後長**：validation 永遠拿不到新的 bar。要換一段
  holdout 是另開一個 store 的事，這裡不提供輪替。
- **`experiment` 會讀到 holdout 的 rows**（量 span 與暖機要用），不算任何窗口的分數；鎖擋的是
  trial 的量測。
- **`SearchTrial` 不藏 promote 狀態，但 B1 的 prompt 藏**（B1 拍板）：gate 的 blocker 仍然會
  說「已經 promote 過」，型別本身沒改；模型拿到的那份摘要則完全不提 promote——「這條過了」
  不是提下一條假說需要的事實，而要讓型別說出這件事就得替它加一個欄位，那個型別存在的理由
  就是沒有東西可洩。
- **prompt 不給日期是有代價的**：模型不知道自己在哪一段歷史上，所以「這幾年比較像什麼行情」
  這類先驗完全用不上。故意付的，理由見上面「假說迴圈」段。
- **`--max-trials` 是每次呼叫自己的計數，不跨 run 累計**：`proposals` 記得住每一次嘗試，但
  預算不是從那裡扣的。要限制總量是排程的事，不是這個旗標的事。
- **`ChatHypothesist` 捕捉整個 `Exception` 家族**：後面是半打 provider SDK 疊在 httpx 上，
  沒有共同基底類別可以點名，而每一種在這裡的意思都一樣——沒有答案回來。`BaseException`
  沒捕，所以 Ctrl-C 照樣停得下來。
- **模型重提同一條規則，要花掉一個 round 才知道它重複**：`spec_hash` 撞到就不重算（正確，
  評估器是確定性的），回給它的是上一次的數字。prompt 現在會把試過的規則列完，所以這是模型
  自己的重複，不是 prompt 沒講。
- **schema v3 是單向門**：v2 的 store 升上來資料完整（實測 candles／funding／experiments／
  trials 全保留、`integrity_check` ok），但**只要用新 build 開過一次，舊 checkout 就完全打不開
  它**——連唯讀的 `report` 都會被 `schema_version` 的守衛擋下。跟 PR #239 不同的是，備份不是
  為了保資料，是為了保住「退回舊 build」這個選項。要在兩個 build 之間來回，先複製一份 store。
- **「被中斷」是拿字串常數 `INTERRUPTED` 認出來的**：CLI 靠它決定回 130 還是 1。理論上一個
  自訂的 `Hypothesist` 丟出訊息剛好等於 `interrupted` 的 `HypothesistError` 會被誤判成取消；
  本套件唯一的實作 `ChatHypothesist` 一定會加上 label 前綴，所以實際踩不到。改成「訊息＋旗標」
  兩個欄位反而會長出「兩個欄位可能互相矛盾」的問題——那正是 `Round.__post_init__` 在防的東西
  ——所以維持一個欄位。
- **CLI 認 `EvaluationError`／`FeatureError` 是查 `sys.modules`**：直接 import 會讓每個指令付
  pandas 的錢；它們被 raise 出來，就代表定義它們的模組已經載入。
- **`--interval 1d` 的 experiment 上 `close_1d` 退化成 `close`**（bars 與 daily 是同一批
  rows，`load_bundle` 直接拿 bars 當 backdrop，不讀第二次）；parser 看不到 interval 所以擋不了
  （承 A2 §10.8）。
- **`fetch` 沒拆成 `fetch-candles`／`fetch-funding`**（#256 的可選配套）：`--skip-funding`
  留著，`--resume` 也只管 funding——candles 到深度牆只要 5 頁，而且重走才補得了洞。
- **整點 `.000` 的 settlement 落在兩根 venue bar 的縫**：`_Settlements.due` 用
  `(open, close]` 收費，feature 用 `(前一根 close, close]` 加總，兩者只在 stamp 恰好等於
  open（＝前一根 close ＋ 1 ms）時不同。實測 531 筆全部晚 2–99 ms，evaluator 的 docstring
  有記；`test_evaluator` 的 `_funding` 夾具不造這種 stamp，`conftest.funding_points` 仍是整點
  （feature 測試拿它釘窗口邊界，gaps 測試釘的是整點格線）。

## 測試

```bash
python -m pytest contrib/autoresearch/tests -q
```

自己一條基準數，不併進 `hyperliquid_perp` 的那條。測試不打網路：交易所那端一律走
[`ports.py`](./ports.py) 的 `HistoryMarketData`，由測試餵劇本化的假資料。

CI 也跑這一套（`.github/workflows/ci.yml` 的 `autoresearch` job）：pin 測試會 import 上游的
venue reader，所以除了 dev extras 還要裝 `requirements.txt`（Hyperliquid SDK）。
`hyperliquid_perp` 自己的測試仍然只在部署時於伺服器上跑，這裡沒有改。
