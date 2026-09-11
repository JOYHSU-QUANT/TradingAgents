# autoresearch

研究雷達不是另一套交易系統。

它不下單、不持倉、不碰 `contrib/hyperliquid_perp/` 的 SQLite 或 schema，也不越過
RiskGate。它做的事只有一件：把 BTC 的歷史資料落到**自己的** store，之後（PR A2–A4）
在上面跑一個確定性的回測評估器，替 LLM 提出的擇時假說打分。唯一會流回實盤路徑的東西，
是很久以後（計畫 §7、run 6）prompt 裡一段**定性**的研究訊號，而且藏在一個預設關閉的
config 開關後面。

上面那句話就是這個套件的 scope 判準：**任何需要把它放寬的改動，都是 scope creep，不是
更大的功能。**

完整設計在 local-only 的 `.claude/autoresearch-plan-2026-09-09.md`；方向拍板的依據在
memory `hyperliquid-autoresearch-mvp-direction`。

## 現況

**PR A1（本次）＝資料落地層。** 已經有的東西：

- 自己的 store `autoresearch.sqlite`（schema v1：`candles`、`funding`、`series_state`）。
- `series_state`：每條序列一列，記上一跑 fetch **實際抽到哪、為什麼停在那**。gap 掃描回答不了這件事：它以第一個 stamp 當格線原點，所以前端被截掉的序列掃起來「完全沒洞」——跟交易所真的沒更舊資料長得一模一樣。
- `fetch` 指令：由新往舊分頁抓 candles、由舊往新分頁抓 funding history，全部 upsert。
- gap 檢查：掃出序列上的洞（缺格）與不在格線上的時間戳，只回報、不修補。

**還沒有的東西**（依計畫 §5 的順序）：feature 詞彙表與 DSL parser（A2）、bar 級模擬與
成本模型（A3）、`experiments`／`trials` ledger 與 baseline 校準（A4）、LLM 假說迴圈
（B1）、context bridge（C1，排 run 6）。

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

還有一件：連續快速要求約 44 次就會吃到 429。fetch 遇到 throttle 會等（2s／5s／15s／30s，
共五次），等不到就具名失敗；已寫進去的頁不會不見，重跑即可。

store 預設在 repo root 的 `data/autoresearch.sqlite`，用 `--db` 改路徑。指到一個
**不是**本套件 store 的 SQLite 檔（例如 paper 的 `paper_trading.db`）會被具名拒絕——
不會有任何 migration 跑在別人的檔案上。

離開碼：`0` 成功、`1` 具名的操作／store／交易所失敗、`130` 中斷。**store 有洞不算失敗**：
掃到洞是成功掃描的結果，exit 0；要補洞請跑 `fetch`。

## 對 `hyperliquid_perp` 的關係

唯讀，而且只從一個地方借：[`upstream.py`](./upstream.py) 列出全部借用的名字
（`BORROWED`），本套件其他模組一律經過它，不自己 import 上游——
`tests/test_upstream.py` 讀原始碼檢查這件事。之後每個借來的符號還要各自加 pin 測試
（計畫 §3.2），這樣上游重構時，是這裡的測試先紅，而不是實驗結果悄悄變得不可比。

## 測試

```bash
python -m pytest contrib/autoresearch/tests -q
```

自己一條基準數，不併進 `hyperliquid_perp` 的那條。測試不打網路：交易所那端一律走
[`ports.py`](./ports.py) 的 `HistoryMarketData`，由測試餵劇本化的假資料。
