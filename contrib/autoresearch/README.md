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

**PR A1＝資料落地層、PR A2（本次）＝假說寫得出來的那套語言。** 已經有的東西：

- 自己的 store `autoresearch.sqlite`（schema v1：`candles`、`funding`、`series_state`）。
- `series_state`：每條序列一列，記上一跑 fetch **實際抽到哪、為什麼停在那**。gap 掃描回答不了這件事：它以第一個 stamp 當格線原點，所以前端被截掉的序列掃起來「完全沒洞」——跟交易所真的沒更舊資料長得一模一樣。
- `fetch` 指令：由新往舊分頁抓 candles、由舊往新分頁抓 funding history，全部 upsert。
- gap 檢查：把每個時間戳指派到最近的格位，分開回報**三種**發現——
  **洞**（中間有空格，重抓可補）、**重複格位**（兩筆落在同一格；一小時內兩筆 funding
  會把那小時的 carry 算兩次，而且 row 數看起來更健康）、**不在格線上**（重抓修不好，
  意思是交易所改了節奏，或兩種節奏被寫進同一條序列）。只回報，不修補。
- **封閉的 feature 詞彙表**（`vocabulary.py`）：16 個 kind、每個 kind 自己宣告它接受哪些
  period，所以整套語言列得完。`vocab` 指令印的那張表是**從 parser 查的同一張表生出來的**，
  不是手抄的——B1 要餵給 LLM 的詞彙表也走這一個函式。
- **宣告式 DSL 與它的 parser**（`dsl.py`）：假說是資料不是程式，沒有 `eval`／`exec`／任何
  自由運算式。未知欄位、未知 feature、未知 op、未來 offset 一律**具名拒絕**，句子裡帶著
  文件內的路徑（`spec.entry.long[0].right`）與該怎麼改。
- **逐 bar 的 feature 計算**（`features.py`）：每個值只由「截至該 bar 收盤」的資料算出來，
  而且是**建構上如此**——indicator 引擎拿到的永遠是 `bars[: t + 1]`。
- **三個 pin 測試**（`tests/test_pins.py`）：`compute_indicators`、`classify_regime`、
  `funding_zscore` 的簽名與固定輸入的固定輸出。

**還沒有的東西**（依計畫 §5 的順序）：bar 級模擬與成本模型（A3）、`experiments`／`trials`
ledger 與 baseline 校準（A4）、LLM 假說迴圈（B1）、context bridge（C1，排 run 6）。

`experiments`／`trials` 兩張表**故意還沒建**：等 A4 把寫它們、讀它們的程式一起帶進來。
現在先建好，只會多兩張沒有生產者也沒有消費者的表。

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

# 只掃 gap，不連網
python -m contrib.autoresearch gaps --coin BTC --interval 4h

# 一個假說能用哪些 feature（不開 store、不連網）
python -m contrib.autoresearch vocab

# 這份 spec 合不合法？合法的話它到底在說什麼？
python -m contrib.autoresearch validate-spec --spec rule.json
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
indicator 引擎拿到的 window 結束在這根 bar，日線只看 `close_time` 已經 ≤ 這根 4h bar 收盤
的那些，funding 同理。測試（`test_features.py`）把 bundle 截到第 t 根再算一次，要求兩邊
完全相同——**而且截兩次**：只截 bar 抓得到「往前索引」的錯，連日線與 funding 一起截才抓得到
「去讀一根當下看不到的日 K」的錯。旁邊還有一個守門測試，要求被檢查的那根 bar 上**每個**
feature 都有值，否則一整排 `None` 會跟自己完美相符。

window 的**起點**則是另一件事：它固定往回 200 根，也就是實盤每個 cycle 去 fetch 的根數
（`candle_lookback`）。同一根 bar 因此在這裡與那裡拿到同一個 EMA；順帶一提，這也讓成本從
平方變回線性——引擎每次呼叫都重建 frame，餵不斷變長的 prefix 到第 5000 根是 6.8 ms/bar，
固定 window 是大約 1.6 ms。

有幾個地方「忠實鏡射實盤」的意思是**不要**把上游的值直接傳出來：

- **warm-up 期間的 regime**：`classify_regime` 在指標缺席時回 `RANGING`，而實盤永遠不會把
  這個答案拿給任何人看——`context_guards` 會直接拒絕整個 cycle，就是為了不讓人對著一個
  捏造的「平靜」下單。忠實的鏡射是 `None`。
- **被打分的那一筆 funding rate 不算進它自己的比較窗**：實盤傳進去的是尚未結算的當期
  rate，本來就不在 history 裡；把值折進自己的平均與標準差，會讓真正的極端值讀起來沒那麼
  極端（上游自己的註解就是這樣寫的）。
- **過期的 funding rate 與過期的日線**：兩條序列都可能比 bar 早結束。把最後一筆一直往前
  帶，等於用一次觀測去定價好幾週的 carry，或讓一個日線趨勢濾網凍結在一個數字上、永遠開著
  或永遠關著。所以超過一個 interval（加上發布抖動）就是 `None`。`fetch --interval 4h` 與
  `--interval 1d` 是兩次呼叫，所以「4h 是新的、1d 落後幾個月」是只跑其中一次的預設結果。
- **窗口回推不到的 funding z-score**：`funding_zscore_30` 需要 30 天的 settlement。store
  只有兩天時，上游那個函式的下限是 24 筆（＝一天），所以它會給你一個數字，而 7／14／30 三
  個 feature 會是同一欄。這裡直接回 `None`。這一條是**刻意不鏡射實盤**：實盤每個 cycle 自己
  抓 30 天窗，而且會把 `n=` 印進 prompt 讓讀的人看見，研究這邊沒有那個管道。

## store 路徑與拒絕

預設在 repo root 的 `data/autoresearch.sqlite`，用 `--db` 改路徑。四種會被**具名拒絕**的情況：

1. 指到一個**不是**本套件 store 的 SQLite 檔（例如 paper 的 `paper_trading.db`）——不會有任何
   migration 跑在別人的檔案上，連對方的 journal mode 都不會被動到。
2. 指到一個**不是一般檔案**的路徑（例如目錄）。
3. **根本開不起來**的路徑（目錄建不出來、不是資料庫、權限不夠）——訊息會帶著 sqlite
   自己的診斷，而不是一堆 traceback。
4. 被**更新版本的 build** 寫過的 store（schema 版本比本 build 新）。

離開碼：`0` 成功、`1` 具名的操作／store／交易所／**spec** 失敗、`2` argparse 自己的用法
錯誤（例如 `--interval 1h`）、`130` 中斷。**store 有洞不算失敗**：掃到洞是成功掃描的結果，
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

## 測試

```bash
python -m pytest contrib/autoresearch/tests -q
```

自己一條基準數，不併進 `hyperliquid_perp` 的那條。測試不打網路：交易所那端一律走
[`ports.py`](./ports.py) 的 `HistoryMarketData`，由測試餵劇本化的假資料。
