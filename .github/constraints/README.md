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

每份清單是所有 job 會裝的東西的聯集：專案本身加上 `dev` 與 `discovery-tools` 兩個 extra、根目錄的 `requirements.txt`、`contrib/uniswap_v3/requirements.txt`、`ci.in`。同一個 Python 版本的 job 共用一份（2026-11-25 的決定；issue #442 原本寫的是分開考慮）。

共用有一個副作用：清單是把所有 job 的相依放在一起解的，所以**某個 job 沒裝的套件，會決定它有裝的套件釘在哪一版**。目前有三處，每個 job 單獨解的話會拿到比較新的版本（見「已知的限制」）。

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

它們只裝 `.[dev]`、只跑 root 的 `tests/`。只影響 contrib 測試的上游變動（`web3`、`mcp`、`hyperliquid-python-sdk` 這類）在這裡看不到，要等重新產生清單的那張 PR，必過的 job 才會變紅。

它們跟其他 job 一樣在每張 PR、以及 push 到 `main`／`develop` 時跑，沒有另外排程：排程的 workflow 只會從預設分支（`main`）的檔案觸發，而且 GitHub 會停掉 fork 上不活躍的排程。

1. 打開那個 job 的「List what was installed at another version than the constraints file pins」步驟（排在測試後面，測試失敗也會跑）：它列出跟清單不同的套件與兩邊的版本。`websockets` 每次都會在裡面（見「已知的限制」），不是上游剛變的。如果這一步說專案沒有安裝，代表安裝步驟本身就失敗了，要看安裝步驟的輸出。
2. 對照失敗的測試，找出是哪個套件。
3. 修法二選一：改程式或測試讓新舊版都過（PR #436 是這樣修的）；或在 `pyproject.toml` 給那個套件加上限。
4. 修好後照下一節重新產生清單，一起進同一張 PR。

在這之前，必過的 job 仍然照清單裝舊版，其他 PR 不受影響。

用 `gh pr checks` 看 PR 時，這兩個 job 紅會讓指令回非零；只看必過的檢查用 `gh pr checks <PR> --required`。

## 怎麼更新

需要 [uv](https://docs.astral.sh/uv/)（`pip install uv` 即可）。在任何平台都能跑，因為指令指名要解的是 Linux，不是執行它的那台機器。

| 指令 | 做什麼 |
|---|---|
| `python .github/constraints/compile.py` | 清單裡已有的版本只要還符合相依宣告就**不動**，只補上新加的相依、改掉不再符合範圍的版本 |
| `python .github/constraints/compile.py --upgrade-package 套件名` | 只把指名的套件升到範圍內最新（可以給多次）；它的相依需要跟著動的也會動 |
| `python .github/constraints/compile.py --upgrade` | 整份重解，每個套件都升到範圍內最新 |

預設不升級是 2026-11-25 的決定：加一個相依的 PR 不應該順便吃進上游當天所有的新版。因此檔頭記錄的那條指令不是清單的全部來歷，要看 git 歷史。要壓住某個套件不讓它升，只能在宣告相依的地方加上限：`pyproject.toml`，或那兩個 requirements 檔（那會連帶影響 CI 以外的安裝）。

`--upgrade-package` 的套件名打錯時 uv 不會報錯，清單也不會變：跑完 `git diff` 是空的，代表名稱不對，或那個套件已經是最新。

`--upgrade` 也會動到 `ruff`：新版多一條規則時，那張 PR 的 lint job 會變紅，在那張 PR 裡修。本機的 ruff 版本不受這些清單管。

這些清單是用 uv 0.11.18 產生的。別的 uv 版本可能解出不同的結果或寫出不同的檔頭（測試會核對檔頭）。

跑完之後：

1. 看 `git diff .github/constraints`：哪些套件動了。
2. `python -m pytest tests/test_ci_constraints.py -q`。
3. commit 四份清單、開 PR。那張 PR 的 CI 就是驗證：必過的 job 會照新清單安裝並跑完整套測試。

什麼時候要跑：

- 「最新版」job 紅了、修好之後（上一節）：用 `--upgrade-package` 或 `--upgrade`。
- **加了或改了相依**（`pyproject.toml`、`requirements.txt`、`contrib/uniswap_v3/requirements.txt`、`ci.in`）：不帶參數。沒跑的話 `tests/test_ci_constraints.py` 會指出哪一份清單缺哪個套件，或哪個釘的版本落在新宣告的範圍之外。**替既有的相依加 extra**（例如 `redis` 改成 `redis[hiredis]`）也算：extra 帶進來的套件不在清單裡就會照舊裝最新版，測試會要求重新產生後把那個 extra 登記進測試檔的 `EXTRAS_RESOLVED`。
- 上游同步改了 `pyproject.toml` 的相依範圍之後：不帶參數。
- 想讓 CI 跟上上游的新版本時：`--upgrade`。沒有排程，由人決定（2026-11-25 的決定）。

## 改 CI 時要注意

`tests/test_ci_constraints.py` 會讀 `ci.yml` 與這個資料夾，下面幾件事做錯它會失敗：

- 必過的 job 新增一條安裝指令卻沒帶 `-c`。真的不該帶的，要加進測試檔的 `UNCONSTRAINED` 並寫明原因。
- 必過的 job 從 `compile.py` 沒讀過的來源安裝（另一個 extra、另一個 requirements 檔、清單裡沒有的套件名稱）：`-c` 對清單沒列的套件沒有作用，那些套件會照舊裝最新版。要先把來源加進 `compile.py` 的 `EXTRAS`／`REQUIREMENTS` 或 `ci.in`，重新產生清單。
- 測試矩陣加減 Python 版本：`compile.py` 的 `PYTHON_VERSIONS` 與這裡的清單檔要一起改。**分支保護也要改**：`tests (py3.x)` 是照版本展開的名稱，拿掉一個版本而沒改保護設定，每張 PR 都會卡住。
- 改 job 的 `name:`：`develop` 的分支保護是照名稱等檢查結果的，名稱改了、保護設定沒改，每張 PR 都會卡在等一個不會出現的檢查。要改就兩邊一起改，並更新測試檔的 `REQUIRED_NAMES`。
- 新增任何 job：測試檔列出了 `ci.yml` 的全部 job，多一個就會失敗，要把它加進 `REQUIRED_NAMES`（會被當成必過、每條安裝指令都要帶 `-c`）。
- 讓必過的檢查「沒跑也算過」或「永遠不回報」的改法：在 job 或步驟加 `if:`、`continue-on-error`、`needs:`、`container:`，改 `runs-on`，改檔案開頭 `jobs:` 之上的任何設定（例如替觸發條件加 `paths-ignore`、加全域 `env:`；加註解不算），用 `PIP_CONSTRAINT` 環境變數，用 `actions/checkout` 與 `actions/setup-python` 以外的 action（這兩個換版本不受影響）。測試擋的是列在這裡的這幾種，不是所有可能。
- 用 `uv`、`uvx`、`pipx`、`pipenv`、`poetry`、`conda`、`mamba`、`tox`、`nox`、`hatch`、`pdm`、`pre-commit` 當指令：這些工具大多自己建環境、不讀 `-c`，測試一律把它們當成沒帶清單（即使 `uv pip install -c` 其實會讀）。真的要用，得先改測試檔。
- 在帶 `-c` 的安裝行加 `-e`、`-r`、`-c` 以外的選項（`-q`、`--no-cache-dir`、`--pre` 等）：測試只讀得懂那三個，其餘一律失敗，要先在測試檔的 `_sources` 寫明新選項的意思。

新增的 job 不會自動變成必過；要不要加進保護設定是另一個決定。

## 已知的限制

- **共用清單壓住的版本**（2026-11-25 產生清單時）。這幾個套件在所列的 job 裡，單獨解會拿到比較新的版本：

  | 套件 | 清單釘的 | 被誰壓住 | 影響的 job | 單獨解會拿到 |
  |---|---|---|---|---|
  | `websockets` | 15.0.1 | uniswap 的 `web3<8`（它要求 `websockets<16`） | 四個 `tests`、smoke、autoresearch、discovery | 17.4（3.10 是 16.1.1） |
  | `python-dateutil` | 2.9.0.post0（連帶多裝 `six`、不裝 `tzdata`） | `dev` extra 的 `langchain-aws` 帶進來的 `botocore`（它要求 `<3`） | smoke | 4.0.0 |
  | `eth-account`、`eth-keyfile`、`eth-utils` | 0.13.7／0.8.1／5.3.1 | perp 的 `hyperliquid-python-sdk`（它要求 `eth-account<0.14`） | uniswap | 0.14.0／0.10.0／6.0.0（另外多裝 `py_ecc`） |

  後果有兩個。第一，「最新版」job 的差異清單每次都會列出 `websockets`。第二，`clean-install smoke` 測的是「照清單的版本做 bare install」，不是「今天 `pip install .` 會拿到的版本」；後者現在沒有任何 job 在測（「最新版」job 裝的是 `.[dev]`）。專案直接 import `dateutil` 卻沒有在 `pyproject.toml` 宣告它，目前靠 pandas 帶進來。
- 清單是替 Linux x86_64 解的。只有 Windows 才會裝的套件（`colorama`、`pywin32`）不在裡面。
- uv 是替每個 Python 版本的 `.0` 修訂版解的，runner 上是比較新的修訂版。相依的條件寫到修訂版號時兩邊會不同：`py3.11.txt` 多釘了一個 runner 不會裝的 `async-timeout`（無害）；反過來，如果有套件只在較新的修訂版才需要某個相依，那個相依會不在清單裡、照舊裝最新版，而且沒有東西會報。
- 只釘版本、不釘 hash（2026-11-25 的決定）：這些檔的目的是讓 CI 不被上游發新版弄紅，不是供應鏈防護。
- 手改清單裡的某一行不會被測試發現（測試只核對檔頭記錄的指令、每行都是 `套件==版本`、宣告的相依都在）。手改出互相衝突的版本時，要等 CI 的 `pip` 或 `pip check` 才會報。
