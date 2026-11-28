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
deploy/windows/    伺服器版的 Windows 離線安裝檔：服務主程式（Rust）、預設的 server.toml、NSIS、建置腳本與冒煙測試
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
uv run python deploy/windows/build.py           # 伺服器版的 Windows 離線安裝檔（含冒煙測試；桌面版見 desktop/README.md）
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

## 目前的狀態（2026-11-25）

- Linux 上的完整測試（3573fd6）：2752 項通過、2 項略過（PostgreSQL、MySQL、Spark 實跑，略過的 2 項是沒有 Trino 與
  BigQuery 模擬器）
- Windows 上的完整測試（8622057，Windows 11、沒有資料庫引擎）：ruff、pyright 通過，pytest 3028 項通過、145 項略過
  （都是需要 PostgreSQL、MySQL、Spark、Trino、BigQuery 或認證管理員的，在 Windows 上還沒跑過）；前端可以建置。
  3573fd6 之後的 commit 只在 Windows 上測過，還沒在有引擎的 Linux 環境跑過
- 2026-11-21 使用體驗稽核找到的程式錯誤已修（01f6a4c 桌面版與核心、baf7d8b 伺服器端、ac93b7e 桌面版裡的確認、
  392a424 刪除可以復原、手機版面、畫圖的附註、框選與多行文字的列高）；修這幾批時看到的 10 項在 2026-11-22 修了
  （3b07205 第四批：編輯器跟上引擎的改變、觸控的工具提示、列高與欄寬、註解的排序、附註的名稱、簡報模式的 Esc；
  ca7583a 捲軸蓋住右邊的數字；6ec93f4 第五批：對話框裡的復原、釘住的工具提示中的工作表、簡報模式的 F7／Ctrl+H、
  篩選範圍的預設值、離散座標軸上的註解、標籤的位置與欄寬、錯誤訊息裡的內部代號）。還沒處理的，以及
  「與 Tableau 不同」的項目在 `docs/backlog.md`
- 2026-11-22 補 Tableau 缺口（第一批，232f545；408be66、a050f0c 是描述預測對話框的版面）：Relationship 跨資料表的計算、Data Blending 次要資料源的
  條件與 Top N 篩選和 LOD、標題中的表計算、預測結果與描述預測的摘要、Live 連線上的 RAWSQL、表頭與合計／總計的
  框線、.xls 的具名範圍、單張工作表分頁列印、在檢視上拖曳標題與圖例排序、「N 個 Null」的「篩選資料」。推定的部分
  與待驗證的在 README「已知限制與待驗證」與 `docs/backlog.md`；**待使用者確認**：標題中的量值在檢視中有多個值時
  Tableau 顯示「範圍」還是合計（`docs/backlog.md`「四、待決定」）
- 2026-11-23 補 Tableau 缺口（第二批，d349097、c6aa856）：LOD 維度中的 FIXED（世代分析）、叢集的類別變數與取消彙總、
  預測「自動」改用較細的細度（文件沒寫明的情況列為不支援）、標題中的 Measure Names／Values、故事點記下的排序與
  欄列架等狀態、合併集合（寫法取自公開報表）、背景圖片、依欄位的值排序時的圖例順序、拖曳 Measure Names 與分格標題、
  「清除排序」、列印所有頁面、整張的圖片與 PowerPoint、「更新中…」、標記的動畫（.twb 的動畫設定還沒解析，見
  `docs/backlog.md`）
- 2026-11-24 補 Tableau 缺口（第三批，d59cd74、2fcd576）：MODEL_QUANTILE／MODEL_PERCENTILE（線性迴歸）、預測選項的
  彙總依據、故事點加上的欄位與量值／Top N 篩選、顯示集合中的成員、分格與地圖上的背景圖片；以 GitHub 上的公開報表
  核對 .twb 的寫法（預測區間的名稱改正、框線確認）並解析動畫、表計算的自訂排序、篩選控制項的自訂、顯示歷程、Story
  的格式與分頁顏色；叢集對話框加入維度、「更新中…」的停止、折線與座標軸的動畫。Snow Sports 開啟後動畫是開
- GitHub Actions 目前沒有在跑（使用者決定先不處理）；Windows 上的測試與安裝檔的建置在本機做。這台電腦閒置時會進入
  新式待機：完整測試、建置等長時間的工作要先讓電腦保持喚醒（2026-11-23 有一次完整測試因待機跑了 14 小時並失敗）
- 桌面版安裝檔（2fcd576）：2026-11-24 在 Windows 本機重建，引擎的冒煙測試通過，放在 GitHub Release
  `desktop-v0.1.0-20261124`（預先發行版；安裝檔超過 100 MB、不進 git，見 `desktop/README.md`「發布安裝檔」）；在真正的視窗中驗證過補 Tableau
  缺口三批的項目與第四、第五批的項目（以 WebView2 的除錯埠與 Win32 操作，見 `docs/backlog.md`；觸控的工具提示只在
  瀏覽器的手機模擬上看過；F7、Ctrl+H 是從除錯埠送的按鍵，實體鍵盤會不會先被 WebView2 拿走沒有驗證；分頁列印
  沒有經過真正的列印對話框）。之前
  幾批在視窗裡驗證過版面、功能表、拉 dashboard、刪除後復原與確認用的訊息方塊。還沒有實際執行安裝檔，原生的選檔與
  存檔對話框也還沒驗證。建置的做法在 `desktop/README.md`；動到桌面版的流程後要在真正的視窗裡再驗證（瀏覽器裡的
  測試看不到外殼特有的行為：捲軸占的寬度就是在視窗裡才看到的）
- 離線安裝檔（8622057）：2026-11-25 建置伺服器版（`deploy/windows`：Windows Server 的服務，內含 Python、所有套件與
  資料庫驅動、Chromium）與桌面版（內含 WebView2 的離線安裝程式，引擎帶所有資料庫驅動），放在 GitHub Release
  `offline-v0.1.0-20261125`（預先發行版）。同一天在這台 Windows 11 上實際安裝驗證：伺服器版安裝成服務、查詢、重新啟動、
  升級與解除安裝都正常（驗證後已解除安裝）；桌面版更新了使用者在 `D:\TableauX\TmigDesktop` 的安裝並在視窗裡開報表。
  **還沒在 Windows Server 上安裝過**，驅動只驗證到載入。桌面版的離線安裝檔內附的 WebView2 離線安裝程式在公司內網的電腦上安裝失敗，
  2026-11-28 改成內附 WebView2 的固定版本（不必安裝；還沒在內網電腦上驗證，見 `desktop/README.md`）。見 README「伺服器的安裝（Windows）」與 `docs/backlog.md` 第三部分的 11
- **GitHub 上的 repo 是公開的**（2026-10-26 建立時就是公開，與上面安全規則的「維持 private」不同）。2026-11-25 告知
  使用者後，使用者決定照樣把離線安裝檔發布到公開的 Release；repo 的公開與否由使用者決定，不要自己改，也不要在
  回報中說它是 private
- `scripts/regen_ir.py`、`scripts/check_commit.py` 在 Linux 與 Windows 上都跑過
- 接下來的工作與待決定的事項見 `docs/backlog.md`
