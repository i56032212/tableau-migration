# tools

需要瀏覽器自動化（Playwright）的工具放這裡，核心函式庫不依賴它們。

| 目錄 | 任務 | 用途 |
| --- | --- | --- |
| `fetch_expected/` | E1 | 依 `cases.yaml` 從 Tableau 抓工作表的 CSV 與 PNG |
| `compare_screens/` | F5 | 產生 Tableau PNG 與新平台截圖的並列頁 |

第一次使用先安裝瀏覽器：

```bash
uv run playwright install chromium
```

## fetch_expected（在能連到 Tableau Server 的電腦上執行）

1. 在 `fixtures/<報表>/cases.yaml` 填 `tableau_views_url`（從瀏覽器網址列複製 workbook 的網址即可，`#/site/...` 會自動轉換）與 `sheets` 的 `view`（工作表網址中的名稱）
2. 第一次加 `--login`，在開啟的瀏覽器手動登入後回終端機按 Enter：

```bash
uv run python tools/fetch_expected/fetch_expected.py fixtures/<報表> --login
uv run python tools/fetch_expected/fetch_expected.py fixtures/<報表> --dry-run   # 只列出網址
```

寫入 `expected/<工作表 id>/<篩選組合>.csv`、`.png`、`expected/fetch-log.json`，並更新 `fetched_at`。一律用工作表網址（dashboard 網址的 .csv 只給字母序第一張工作表）；`.twbx` 與預期檔必須在同一次萃取更新之間取得。抓完提交到 git，之後的工作都不需要再連 Tableau。

## compare_screens

```bash
uv run tmig verify fixtures/<報表>                                  # 先跑計算比對（頁面會標示結果）
uv run python tools/compare_screens/compare_screens.py fixtures/<報表>
```

自動在背景啟動查詢服務，逐一截圖每組篩選下的每張工作表與 dashboard，輸出 `fixtures/<報表>/compare/index.html`（每組篩選一頁）。用瀏覽器開啟，逐頁確認沒有資訊落差後簽核。
