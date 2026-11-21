# CLAUDE.md

自建平台取代 Tableau Server 與 Tableau Desktop。目標是「無痛移轉」：移轉後的數字可以驗證與 Tableau 相同。
對照的版本是公司目前的 Tableau 2021.2.7。功能的說明在 `README.md`，編輯器的涵蓋範圍在
`docs/editor-coverage.md`，還沒做與還沒驗證的在 `docs/backlog.md`。

## 目標與範圍（使用者的決定）

- 最終的樣貌與 Tableau 相同：部署報表的伺服器，加上安裝在電腦上的桌面版編輯器（不是瀏覽器編輯器）
- 桌面版編輯器要還原 Tableau 的功能，以 2022 年以前的版本（2021.2.7）有的功能為主
- 先以 1–2 份真實報表做試點，從頭到尾移轉並比對數字
- 做完並驗證過再回報，過程中不必逐步確認；只有不可逆或改變範圍的決定才先問
- 對使用者的回報、文件、註解、介面文字與提交訊息都用繁體中文；回報中引用網路資料時附上來源

## 安全規則（一律遵守）

- repo 維持 private：裡面會放公司的真實報表與資料
- 憑證不進 IR、git、記錄檔與對話：連線設定只寫 `${環境變數}` 的參照；桌面版的密碼存在作業系統的認證管理員
- 資料庫一律用唯讀的服務帳號連線
- Tableau Server 的盤點（`tools/tableau_server`）由使用者用自己的 PAT 執行：不要求、不經手權杖
- 使用者的 email 只用來辨識身分，不送到任何外部服務
- 網路或代理伺服器擋下的請求不繞過，回報給使用者

## 專案結構

```text
kernel/src/tmig/   核心函式庫：ir（模型）、parser（.twb／.twbx → IR）、formula、planner、sqlgen（IR → SQL、方言）、
                   data（Hyper 匯入、DuckDB、Live 連線、Initial SQL）、render（Vega-Lite）、verify、census、cli.py
kernel/schema/     IR 的 JSON Schema（由程式產生，不手改）
service/           FastAPI 查詢服務 tmig_service：檢視器的 API、身分驗證、權限、發布、排程、桌面版的本機引擎與編輯 API
web/               React＋TypeScript 的檢視器與編輯器；建置結果 web/dist 進版控
desktop/           桌面版：Tauri 外殼（Rust）、本機引擎的打包（PyInstaller）與冒煙測試
tools/             連 Tableau Server 的工具（盤點、下載、抓預期結果、畫面比對）
fixtures/<報表>/    .twbx、golden IR、cases、mapping、預期結果、SQL 與規格快照
scripts/           check.py（一鍵檢查）、check_commit.py、regen_ir.py、示範報表的建置
```

流程：.twb／.twbx → IR（pydantic，`ir_version` 0.3）→ DuckDB SQL → 來源資料庫的方言（sqlglot）→ Vega-Lite →
檢視器／編輯器。核心函式庫不依賴服務與前端。規格文件「Tableau 移轉 規格」「桌面版編輯器規格」在 Claude Docs，
不在 repo 裡。

## 指令

```bash
uv sync                                         # 安裝 Python 3.12 與相依套件
uv run pytest -q kernel/tests/data/test_x.py    # 跑相關的測試（不要加 -n）
uv run ruff check --fix . && uv run ruff format . && uv run pyright   # lint、格式、型別
uv run python scripts/check.py                  # 一鍵檢查：ruff、pyright、完整的 pytest（約 25 分鐘）
uv run python scripts/check_commit.py --require-engines --log <檔案>   # 在另一個工作目錄測 HEAD
uv run python scripts/regen_ir.py               # IR 欄位異動後重新產生 Schema、範例 IR、golden IR
cd web && pnpm install --frozen-lockfile && pnpm build                # 建置檢視器（含 tsc 型別檢查）
uv run playwright install chromium              # 端到端測試用的瀏覽器
```

- 需要外部引擎的測試（PostgreSQL、MySQL、Spark 等，見 `kernel/tests/engines.py`）沒有引擎時略過；設
  `TMIG_REQUIRE_ENGINES=1`（或 `check_commit.py --require-engines`）時改為失敗。有引擎的環境一律要求
- SQL 與 Vega-Lite 規格的快照有變更時：`TMIG_UPDATE_SNAPSHOTS=1 uv run pytest` 後逐一檢查差異再提交

## 工作流程（每個功能）

1. 實作，同時寫測試：核心的單元測試、服務的 API 測試，介面的變更加 Playwright 端到端測試
   （`service/tests/*_viewer.py`）。資料庫的行為盡量在真的 PostgreSQL、MySQL 上跑
2. 動到 `web/src` 時重新建置並提交 `web/dist`（CI 會檢查兩者一致）
3. 動到 IR 的模型時執行 `scripts/regen_ir.py`，檢查 git diff 只有新欄位
4. ruff、格式、pyright 全部通過
5. 更新文件：`README.md`（功能說明；推定的行為寫進「已知限制與待驗證」）、`docs/editor-coverage.md`、
   `docs/backlog.md`
6. 提交後跑完整的測試（`check_commit.py` 可以在背景跑，主目錄繼續工作），**通過之後才推上 main**；
   沒通過的 commit 不推
7. 除錯用的暫時測試檔用完就刪，不提交

## 寫程式的規則

- IR 是唯一的報表定義：解析器寫入，其他模組只讀，不回頭讀 .twb
- 不支援的元素寫進 IR 的 `unsupported`（影響數字）或 `ignored`（不影響數字），不默默丟棄
- 只送出確定語意相同的 SQL：方言轉不了的函式列為不支援，不送出可能算錯的 SQL
- Tableau 的行為查不到文件、只能推定時照做，但在 README「已知限制與待驗證」註明是推定的
- 沒有實機驗證過的（資料庫、Windows 安裝檔、與 Tableau 的畫面比對）在文件與回報中照實寫，不寫成已驗證
- 其餘見 README 的「開發規則」

## 目前的狀態（2026-11-21）

- Linux 上的完整測試（3573fd6）：2752 項通過、2 項略過（PostgreSQL、MySQL、Spark 實跑，略過的 2 項是沒有 Trino 與
  BigQuery 模擬器）
- Windows 上的完整測試（24ebe58，Windows 11、沒有資料庫引擎）：ruff、pyright 通過，pytest 2675 項通過、114 項略過
  （其中 110 項需要 PostgreSQL、MySQL、Spark，在 Windows 上還沒跑過）；前端可以建置，結果與 Linux 上的相同。
  3573fd6 之後的 commit 只在 Windows 上測過，還沒在有引擎的 Linux 環境跑過
- GitHub Actions 目前沒有在跑（使用者決定先不處理）；Windows 上的測試與安裝檔的建置在本機做
- 桌面版安裝檔（24ebe58，含新的工作區排列與單一功能表列）：2026-11-21 在 Windows 本機重建，引擎的冒煙測試與外殼的
  啟動測試通過；同一天在真正的視窗中驗證過版面、功能表與拉 dashboard（以 WebView2 的除錯埠操作，見
  `docs/backlog.md`）。還沒有實際執行安裝檔，原生的選檔與存檔對話框也還沒驗證。建置的做法在 `desktop/README.md`
- `scripts/regen_ir.py`、`scripts/check_commit.py` 在 Linux 與 Windows 上都跑過
- 接下來的工作與待決定的事項見 `docs/backlog.md`
