# 待辦與待驗證

2026-11-16 整理。功能的現況以 `docs/editor-coverage.md` 與 README 為準；這裡只列還沒做、還沒驗證與待決定的事。
做完一項就從這裡移除，並更新對應的文件。

## 一、還沒做的 Tableau 功能

| 功能 | 現況 | 備註 |
| --- | --- | --- |
| 動畫 | 沒有 | 標記的轉場（Tableau 2020.1 起） |
| 表頭的框線、總計與合計的框線 | 沒有 | 儲存格與窗格的框線已有（.twb 的寫法與重疊時的順序是推定的，要與 Tableau 的畫面對過） |
| 活頁簿佈景主題（Default、Smooth 等） | 沒有 | 需要實際的 .twb 樣本；目前可以把一張工作表的格式套到其他工作表 |
| 自訂地理編碼、背景圖片 | 沒有 | |
| 擴充功能（Extension）物件 | 沒有 | 開啟 Tableau 檔時列入 unsupported |
| 預存程序當作資料表 | 沒有 | 開啟 Tableau 檔時列入 unsupported；先看盤點中有沒有報表用到 |
| Explain Data（資料解釋） | 沒有 | |
| Ask Data、指標（Metrics）、集合（Collections） | 沒有 | 伺服器上的功能 |
| 書籤（.tbm） | 部分 | 不解析 .tbm；以「匯入工作表…」從來源報表匯入，效果相同 |
| 編輯器的小缺口 | 部分 | `docs/editor-coverage.md` 備註欄中的：WMS／Mapbox 底圖、萃取的抽樣（要在來源資料庫取樣，等有引擎的環境再做）、列印時每頁重複標題與依窗格分頁、在檢視上直接拖曳標題排序、.xls 的具名範圍、「N 個 Null」指示器選單的「篩選資料」、描述預測 Summary 頁的 Initial／Change From Initial／Seasonal Effect／Contribution |

## 二、開啟 Tableau 檔時還不支援的寫法

解析時列入 `unsupported`，報表進人工佇列（完整的清單在 README「已知限制與待驗證」最後的「尚未支援」）：

- `SCRIPT_*`、`MODEL_*`、`RAWSQL_*`
- LOD 的維度本身含 LOD；Relationship 模型中同一個彙總用到多張資料表的欄位
- Data Blending：同一個彙總混用兩個資料源的欄位、LOD 中次要資料源的欄位、次要資料源的條件與 Top N 篩選
- 預測的其他結果類型（趨勢、精確度等）、取消彙總的叢集、標題中的 Table Calculation 值
- 故事點記下的其他狀態（排序、欄列架的變更、註解）
- 合併集合的 XML 寫法（公開報表中還沒看到）、尚未併入 .tdsx 的 Published Data Source、舊版的 .tde 萃取
- 要求檢視者輸入自己的資料庫帳號或以檢視者身分模擬的連線（要改用列層級權限重寫）

另有一類**不會**列入 `unsupported`：編輯器裡可以設定，但 .twb 中的寫法沒有樣本、解析器不認得，從 Tableau 開啟的
報表會是預設值——次要計算與表計算的自訂排序順序、堆疊標記的開關、篩選控制項的自訂（顯示「全部」、搜尋、
套用按鈕等）、頁面的顯示歷程、Story 的格式、分頁顏色。盤點時要留意報表有沒有用到；拿到樣本就能補上解析

## 三、還沒驗證的

這些比第一部分重要：目標是數字可以驗證與 Tableau 相同，而下面每一項都還沒有在公司的環境證實。

1. **資料庫實機**：實跑過的只有 PostgreSQL、MySQL、Spark 與 Trino（Trino 靠 CI 的 Docker，最近沒有跑）。Oracle、SQL Server、
   Snowflake、Redshift、BigQuery、Teradata、Hive、Impala、Db2、SAP HANA 都是轉回 DuckDB 模擬。公司用到的資料庫要
   實測：方言轉譯的 SQL、連線設定、Initial SQL
2. **公司環境**：Live 連線的設定檔（`tmig connections template`、`tmig connections check`）、登入（OIDC 或反向代理）、
   權限與排程的匯入都還沒在公司環境跑過
3. **Tableau Server 盤點**：由使用者以自己的 PAT 執行 `tools/tableau_server`（見 `tools/README.md`），再以
   `tmig census`、`tmig inventory` 看哪些報表符合起步條件、用到哪些還沒做的功能
4. **試點**：還沒有拿 1–2 份真實報表從頭到尾移轉並以 `tmig verify` 比對數字
5. **Windows**：2026-11-16 第一次在 Windows 11 上跑一鍵檢查（ruff、pyright、完整的 pytest）、`regen_ir.py`、
   `check_commit.py` 與前端的建置，修掉當時發現的四個問題（存檔時檔案正被讀取而換不上、兩個只差大小寫的前端
   檔名讓建置失敗、一項端到端測試依賴 Linux 字型的行高、示範報表的匯入紀錄過期）。前端在 Windows 上建置的結果
   與 Linux 上提交的 web/dist 相同。還沒驗證的：
   - 需要引擎的測試 110 項（PostgreSQL 63、Spark 37、MySQL 10）：那台電腦沒有引擎，略過了。MySQL 的臨時伺服器在
     Windows 上不啟動（`kernel/tests/engines.py`），要以 `TMIG_TEST_MYSQL` 指向現成的伺服器
   - 桌面版：Tauri 外殼、PyInstaller 打包的引擎與 NSIS 安裝檔由 CI 的 desktop 工作流程在 Windows 上建置與冒煙
     測試，而 GitHub Actions 目前沒有在跑，所以這陣子加的功能沒有在安裝好的桌面版上確認過
6. **與 Tableau 的畫面比對**：README「已知限制與待驗證」中標為「推定」的行為（版面、格式、預設值、圖片形狀的
   大小與著色等）還沒與 Tableau 的畫面對過；`tools/compare_screens` 可以用來比對

## 四、待決定

- **GitHub Actions**：目前停著（使用者決定先不處理）。恢復之前，Windows 上的測試與安裝檔要在本機手動跑
- **IR 的版本**：README 的開發規則寫「IR Schema 有任何欄位異動必須升 `ir_version`」，但 2026-10-28 定為 0.3 之後
  新增的選用欄位都留在 0.3（舊的 IR 檔讀得進來，新欄位取預設值）。要嘛之後照規則升版，要嘛把規則改成
  「不相容的異動才升版」
- **剩下的功能做哪些**：建議等盤點結果，依公司報表實際用到的排序，而不是照 Tableau 的功能表一項一項做
- **試點的報表**：盤點後挑選

## 五、建議的順序

1. 在 Windows 上補上引擎再跑一次一鍵檢查：裝 PostgreSQL（Spark 另需 Java 17 與 `uv sync --group spark`）後設
   `TMIG_REQUIRE_ENGINES=1`。沒有引擎的部分 2026-11-16 已經跑過（第三部分的 5）；公司的電腦若不是那一台，
   也在公司的電腦上跑一次
2. 盤點 Tableau Server（第三部分的 3），挑試點的報表
3. 設定公司資料庫的連線並實測（第三部分的 1、2）
4. 移轉試點的報表、比對數字，把不一致的地方修掉
5. 在 Windows 上建置並試用桌面版的安裝檔
6. 依盤點中實際用到的功能補第一、二部分
