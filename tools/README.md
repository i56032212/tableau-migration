# tools

需要連 Tableau Server 或瀏覽器自動化（Playwright）的工具放這裡，核心函式庫不依賴它們。

| 目錄 | 任務 | 用途 |
| --- | --- | --- |
| `tableau_server/` | S2 | 從 Tableau Server 盤點報表、排出起步候選、下載選定的報表 |
| `fetch_expected/` | E1 | 依 `cases.yaml` 從 Tableau 抓工作表的 CSV 與 PNG |
| `compare_screens/` | F5 | 產生 Tableau PNG 與新平台截圖的並列頁 |

第一次使用先安裝瀏覽器：

```bash
uv run playwright install chromium
```

## tableau_server（在能連到 Tableau Server 的電腦上執行）

只用 REST API、不需要管理員權限：看得到哪些報表就盤點哪些；下載需要報表的「下載／另存新檔」權限。

```bash
# 盤點：列出報表與檢視次數，下載結構（不含萃取）並逐份盤點 → inventory/inventory.md
uv run python tools/tableau_server/tableau_server.py inventory --server https://tableau.example.com --site sales --pat-name tmig

# 下載選定的報表（含萃取）到 fixtures/<報表>/，並產生起始的 cases.yaml
uv run python tools/tableau_server/tableau_server.py fetch --server https://tableau.example.com --site sales --pat-name tmig --workbook "銷售總覽"
```

- `--site`：瀏覽器網址 `#/site/<site>/...` 中的那一段；預設 site 不用給
- 登入（擇一）：`--pat-name` 個人存取權杖（在 Tableau Server 的「我的帳戶設定」建立；密鑰讀環境變數
  `TABLEAU_PAT_SECRET`，沒有就提示輸入）、`--user` 帳號密碼（`TABLEAU_PASSWORD` 或提示輸入）、
  `--browser-session` 沿用 fetch_expected `--login` 的瀏覽器登入（只能 SSO 登入時使用；實驗性）
- 工具不會把密鑰、密碼或登入權杖寫進任何檔案；憑證以作業系統的信任清單驗證，公司內部 CA 也適用（必要時 `--ca-bundle`）
- 連到 Published Data Source 的報表：`fetch` 一併下載資料源的 .tdsx（含萃取，需要資料源的下載權限）併入報表，
  原本的報表放在 `original/`、資料源在 `published/`；`inventory` 預設下載資料源的定義（不含萃取，放在 `datasources/`，
  沒有更新時沿用）併入後再盤點，`--no-published` 可關閉
- 用到使用者函式或使用者篩選（列層級權限）的報表：`fetch` 以登入帳號的使用者名稱、全名、網域與群組
  （Active Directory 群組寫成 `網域\名稱`）填好 cases.yaml 的 `user`；預期結果必須用同一個帳號抓取
- `directory --out directory.json`：匯出使用者與群組（查詢服務身分驗證的使用者目錄；需要站台管理員）
- `inventory` 的其他選項：`--project`（可重複）、`--limit N` 試跑、`--no-download` 只列清單、`--refresh` 全部重新下載
  （預設只重新下載有更新的報表）
- 輸出在 `inventory/`（不進版控）：`inventory.md`、`inventory.csv`（Excel 可直接開）、`features.csv`
  （各功能用到的報表數，決定之後先支援哪些功能）、`census/`（單份報表的細節）、`server.json`

## fetch_expected（在能連到 Tableau Server 的電腦上執行）

1. 確認 `fixtures/<報表>/cases.yaml` 的 `tableau_views_url` 與 `sheets` 的 `view`（工作表網址中的名稱）；用 `tableau_server.py fetch` 下載的報表已自動填好，手動填時從瀏覽器網址列複製 workbook 的網址即可（`#/site/...` 會自動轉換）
2. 第一次加 `--login`，在開啟的瀏覽器手動登入後回終端機按 Enter：

```bash
uv run python tools/fetch_expected/fetch_expected.py fixtures/<報表> --login
uv run python tools/fetch_expected/fetch_expected.py fixtures/<報表> --dry-run   # 只列出網址
```

cases.yaml 有 `user`（列層級權限）時，抓取前確認瀏覽器登入的帳號與 `user` 相同，不同就停止（換帳號加 `--login` 重新登入）。

寫入 `expected/<工作表 id>/<篩選組合>.csv`、`.png`、`expected/fetch-log.json`，並更新 `fetched_at`。一律用工作表網址（dashboard 網址的 .csv 只給字母序第一張工作表）；`.twbx` 與預期檔必須在同一次萃取更新之間取得。抓完提交到 git，之後的工作都不需要再連 Tableau。

## compare_screens

```bash
uv run tmig verify fixtures/<報表>                                  # 先跑計算比對（頁面會標示結果）
uv run python tools/compare_screens/compare_screens.py fixtures/<報表>
```

自動在背景啟動查詢服務，逐一截圖每組篩選下的每張工作表與 dashboard，輸出 `fixtures/<報表>/compare/index.html`（每組篩選一頁）。用瀏覽器開啟，逐頁確認沒有資訊落差後簽核。
