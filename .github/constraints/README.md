# CI 的 constraints 檔

CI 必過的 job 不再每次都裝「當下最新版」的相依套件，而是照這個資料夾裡 commit 進來的版本清單裝（issue #442）。上游發新版不會讓 `develop` 上的 PR 變紅；要換版本，得有一個 commit 去改清單。

這些檔**只有 CI 用**。本機的 venv、伺服器上的部署都不讀它們，`pyproject.toml` 的相依範圍也沒有因此改變。

## 這裡有什麼

| 檔案 | 是什麼 |
|---|---|
| `py3.10.txt`、`py3.11.txt`、`py3.12.txt`、`py3.13.txt` | 每個 Python 版本一份釘版清單（`套件==版本`），由 `compile.py` 產生，**不要手改** |
| `compile.py` | 重新產生四份清單 |
| `ci.in` | 只在 workflow 步驟裡指名安裝、沒有寫在任何 requirements 檔或 pyproject extra 裡的套件（目前只有 `httpx`） |
| `drift.py` | 列出「已安裝的版本」跟某一份清單不同的套件；「最新版」job 用它 |

每份清單是所有 job 會裝的東西的聯集：專案本身加上 `dev` 與 `discovery-tools` 兩個 extra、根目錄的 `requirements.txt`、`contrib/uniswap_v3/requirements.txt`、`ci.in`。一個 job 沒裝的套件，清單裡有它那一行也不會有任何效果，所以同一個 Python 版本的 job 共用一份。

每個 Python 版本各一份，是因為解出來的版本不一樣：2026-11-20 那次，3.10 解到 `langchain-openai 1.7.4`，另外三個版本解到 1.8.5。

## 哪個 job 用哪一份

| job（`ci.yml` 裡的名稱） | 用的清單 |
|---|---|
| `tests (py3.10)`…`tests (py3.13)` | 各自版本那一份 |
| `clean-install smoke`、`ruff (strict, full repo)`、`autoresearch + replay + carry + dashboard tests (py3.12)`、`discovery tests (py3.12)`、`uniswap_v3 tests (py3.12)` | `py3.12.txt` |
| `tests, latest deps (py3.10)`、`(py3.13)` | **不用**：照舊裝最新版，見下一節 |

用法是在每一條 `pip install` 後面加 `-c .github/constraints/py<版本>.txt`。

不受這些清單管的：

- `pip` 自己（每個 job 第一步的 `pip install --upgrade pip`）。
- `mypy (pyproject [tool.mypy] files)` 那個 job：它在 `ci.yml` 裡直接釘 `mypy==2.3.1`，跟 `dev` extra 解到的版本不同，是刻意的。
- dashboard 頁面的相依：`contrib/dashboard/requirements.txt` 自己逐一釘版本與 hash，用 `--require-hashes` 安裝。
- GitHub runner 的映像檔、Python 的修訂版號、`pip install -e` 建置時用的 setuptools。

## 「最新版」job 紅了怎麼辦

`tests, latest deps (py3.10)` 與 `(py3.13)` 不帶 constraints、裝當下最新版、跑 root 的測試。它們**不在 `develop` 的必過清單裡**，紅了不擋 merge；紅了的意思是「上游有東西變了，而且測試讀得到」。

1. 打開那個 job 的「List what was installed at another version than the constraints file pins」步驟（排在測試後面，測試失敗也會跑）：它列出跟清單不同的套件與兩邊的版本。
2. 對照失敗的測試，找出是哪個套件。
3. 修法二選一：改程式或測試讓新舊版都過（PR #436 是這樣修的）；或在 `pyproject.toml` 給那個套件加上限。
4. 修好後照下一節重新產生清單，一起進同一張 PR。

在這之前，必過的 job 仍然照清單裝舊版，其他 PR 不受影響。

用 `gh pr checks` 看 PR 時，這兩個 job 紅會讓指令回非零；只看必過的檢查用 `gh pr checks <PR> --required`。

## 怎麼更新

需要 [uv](https://docs.astral.sh/uv/)（`pip install uv` 即可）。在任何平台都能跑，因為指令指名要解的是 Linux，不是執行它的那台機器。

```
python .github/constraints/compile.py
```

它會把四份清單整份重解（每個套件都升到範圍內最新），然後：

1. 看 `git diff .github/constraints`：哪些套件動了。
2. `python -m pytest tests/test_ci_constraints.py -q`。
3. commit 四份清單、開 PR。那張 PR 的 CI 就是驗證：必過的 job 會照新清單安裝並跑完整套測試。

什麼時候要跑：

- 「最新版」job 紅了、修好之後（上一節）。
- **加了或改了相依**（`pyproject.toml`、`requirements.txt`、`contrib/uniswap_v3/requirements.txt`、`ci.in`）。沒跑的話 `tests/test_ci_constraints.py` 會指出哪一份清單缺哪個套件，或哪個釘的版本落在新宣告的範圍之外。
- 想讓 CI 跟上上游的新版本時。沒有排程，由人決定（2026-11-25 的決定）。

## 改 CI 時要注意

`tests/test_ci_constraints.py` 會讀 `ci.yml` 與這個資料夾，下面幾件事做錯它會失敗：

- 必過的 job 新增一條安裝指令卻沒帶 `-c`。真的不該帶的，要加進測試檔的 `UNCONSTRAINED` 並寫明原因。
- 必過的 job 從 `compile.py` 沒讀過的來源安裝（另一個 extra、另一個 requirements 檔、清單裡沒有的套件名稱）：`-c` 對清單沒列的套件沒有作用，那些套件會照舊裝最新版。要先把來源加進 `compile.py` 的 `EXTRAS`／`REQUIREMENTS` 或 `ci.in`，重新產生清單。
- 測試矩陣加減 Python 版本：`compile.py` 的 `PYTHON_VERSIONS` 與這裡的清單檔要一起改。
- 改 job 的 `name:`：`develop` 的分支保護是照名稱等檢查結果的，名稱改了、保護設定沒改，每張 PR 都會卡在等一個不會出現的檢查。要改就兩邊一起改，並更新測試檔的 `REQUIRED_NAMES`。

新增的 job 不會自動變成必過；要不要加進保護設定是另一個決定。

## 已知的限制

- 清單是替 Linux x86_64 解的。只有別的平台才會裝的套件（Windows 上的 `colorama`、`pywin32`、`tzdata`）不在裡面。
- 只釘版本、不釘 hash（2026-11-25 的決定）：這些檔的目的是讓 CI 不被上游發新版弄紅，不是供應鏈防護。
- 手改清單裡的某一行不會被測試發現（測試只核對檔頭記錄的指令、每行都是 `套件==版本`、宣告的相依都在）。手改出互相衝突的版本時，要等 CI 的 `pip` 或 `pip check` 才會報。
