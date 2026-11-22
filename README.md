# Tableau 移轉平台

自建平台取代 Tableau Server。起步階段先把兩份報表從 .twbx 端到端移轉到新平台，並以自動化比對證明數字一致。

- 需求：[PRD](https://claude.ai/artifact/GyVE4S5wfjU6gQ5mCuCni1)
- 系統設計：[SDD](https://claude.ai/artifact/4FBduqgDhETHytMMagNowr)
- 本階段規格與任務清單：[起步 Spec](https://claude.ai/artifact/AuVmsCT9wWbRjZpnMeppVu)

## 現況

整條流程已打通：.twbx → IR → DuckDB → 依 Tableau 運算順序產生 SQL → 與預期結果比對 → 檢視器。
以示範報表 `fixtures/superstore_demo`（4 張工作表、1 個 dashboard，含資料源篩選、Top N、FIXED／INCLUDE LOD、
快速 Table Calculation、量值與 Table Calculation 篩選）驗證：

- 解析：unsupported 為空，與 golden IR 逐字相同
- Hyper 匯入：10194 列、自檢 69 項一致
- 計算比對：4 張工作表 × 4 組篩選，16 項全部通過
- 檢視器：區塊位置與 IR 相同；切換 4 組篩選後畫面上的數字與預期結果一致（Playwright 端到端測試）

擴充階段以功能報表 `fixtures/superstore_features`（31 張工作表、12 個 dashboard、1 個 Story）逐項加入真實報表常用的功能：
全域篩選與篩選群組、自動尺寸的 dashboard、裝置專屬版面（手機、平板）、註解、dashboard 上的圖片、按鈕與網頁、Measure Names／Values、同一架多個量值與雙軸、圓餅、
排除選取的標記、篩選動作（點選標記篩選其他工作表）、醒目提示動作與醒目提示控制項、網址、參數與切換動作、階層的切入、工具提示中的工作表、群組與數值分組、
相對日期、條件與萬用字元篩選、在一般選取中取 Top N、差異與差異百分比、移動計算、參考線、趨勢線、甜甜圈與各標記卡不同的詳細程度、
Story（故事點記下的篩選動作、篩選與選取）、集合（Top N、固定成員與條件集合）與集合動作、巢狀 LOD、
動態參數（開啟報表時取自欄位的目前值與允許值）、以參數切換日期單位（DATETRUNC／DATEADD／DATEDIFF／DATEPART／DATENAME 的日期部分為字串參數）、LOD 的維度為運算式
（例如 MONTH([Date])、[Resort]=[參數]）、參數直接放在標記卡上、dashboard 文字物件中的參數與格式（粗體、字級、顏色、對齊）。
31 張工作表 × 4 組篩選共 124 項計算比對全部通過，
檢視器的篩選群組、自動尺寸、各種動作、階層的切入、工具提示中的工作表、群組與篩選條件、Story、集合與集合動作都有端到端測試。以 156 份公開的 .twb／.twbx 盤點，符合起步條件的 112 份中，
解析後沒有不支援元素、可以直接開始的從擴充前的 1 份增加到 64 份（其餘多半是地圖）。

第三階段（來源資料庫、權限、排程）的功能都已完成，連線設定待公司環境實測（N4）。Live 連線已能查詢：工作表的 SQL 中不含 Table Calculation 的部分
轉成來源資料庫的方言下推執行，結果取回 DuckDB 再算 Table Calculation（兩段式執行）。兩份報表的萃取資料載入
PostgreSQL、Spark（Databricks 的引擎；Spark SQL 另走 HiveServer2 協定）、MySQL 與 Trino、資料源改成 Live 連線後，
124 項計算比對全部通過，每個公式函式也都實跑比對；Oracle、SQL Server、Snowflake、Redshift、BigQuery、Teradata、
Hive、Impala、Db2、SAP HANA 尚無實機，以轉回 DuckDB 模擬的方式檢查（見「Live 連線」）。多表的資料源支援 join 樹、自訂 SQL 與
Relationship 模型（見「Relationship 模型」）；公開報表盤點中符合起步條件的從 75 份增加到 90 份。
萃取型的報表可以由萃取層從來源資料庫更新（完整或增量、原子替換），並與 Tableau 的萃取比對（見「萃取層」）。
以使用者函式或使用者篩選限制可見資料的報表，以登入者的身分查詢，網址參數改不了權限篩選（見「列層級權限」）；
查詢服務與檢視器可以要求登入（反向代理標頭或 OIDC，開發模式可切換使用者；見「身分驗證」），並依 Tableau Server 匯出的
Project、workbook、view 權限開放（見「權限」）；匯入之後 Project、權限、使用者與群組在新平台上管理（見「站台管理」）。排程器依 Tableau Server 匯出的排程執行萃取更新，以訂閱者的身分產生
PDF、PNG 寄出訂閱（見「排程與訂閱」），排程、萃取更新與訂閱也在新平台上管理，使用者可以自己訂閱（見「排程管理」）、建立資料警示（見「資料警示」），Published Data Source 也發布到新平台、報表共用伺服器上的萃取（見「伺服器上的資料源」）；檢視器有復原、自訂檢視、下載交叉資料表與資料、留言與我的最愛（見「檢視者的工具」）；每次檢視記錄使用者、報表與時間（見「使用紀錄」）。
一份報表可以有多個資料源（各工作表用各自的資料源，網址參數套用到所有資料源中同名的欄位）；同一張工作表混用多個資料源的
Data Blending 支援主要與次要資料源是萃取、檔案、跨資料庫的 join 或 Live 連線（次要資料源也可以是 Relationship 模型）的工作表，含引用次要資料源欄位的計算欄位、次要資料源的維度、篩選、排序、Table Calculation、參考線與總計（見「Data Blending」）。依檢視者的身分連線資料庫的報表（沒有嵌入密碼、以檢視者的身分模擬），移轉後資料庫端的權限
不會生效，盤點時列為不符合起步條件；連線的 Initial SQL 平台照樣執行，`[TableauServerUser]` 代入平台登入的帳號
（見「Initial SQL」與「依檢視者的身分連線」）。以 156 份公開報表重新盤點：放寬多個資料源後 12 份
轉為符合起步條件；28 份要求檢視者輸入自己的資料庫帳號（Tableau Server Insights、TabMon 等連到 Tableau 資料庫的範例）
改列為不符合，3 份以 Windows 整合驗證連線列為需注意。

另以 GitHub 上 113 份公開的 .twb／.twbx（Logshark、TabMon、twbparser 的範例與個人作品集）逐份檢查：地圖以外的
95 份中 90 份解析後沒有不支援的元素，這 90 份共 814 張工作表都能查詢、畫出合法的 Vega-Lite 規格（其中 252 張
沒有資料，逐張核對過都是資料本身如此：Logshark 範例的萃取是空的或只有少量測試記錄、篩選的值不在資料中、動作篩選
在選取之前不顯示資料、相對於今天的日期篩選，以及 Top N 先於維度篩選的順序）。
這一輪補上的功能：變更資料類型、正規表示式的前後查看、RANDOM()、直接以 .hyper 為來源的 join 與 join 計算、
多個 .hyper 檔、萃取中排除的隱藏欄位、Live 連線快取以外的欄位、沒有用到的錯誤計算欄位、Measure Names 篩選的
「全部」與「排除」、Measure Values 的軸有自己的標記卡（KPI 的折線疊上圓點）、依欄位排序與 Top N、條件篩選中的
INCLUDE／EXCLUDE LOD（相對於排序或篩選的維度）、開啟報表時篩選到最新的值、叢集與預測，以及畫面上的排列：
欄列架上沒有欄位時的樹狀圖（方塊，或「自動」加上大小）、泡泡圖（圓形）與文字雲（文字），表格中的圓點與形狀，
標籤上的多個欄位各占一行，雙軸的兩張標記卡以不同的欄位著色時各自的色階，沒有指定顏色的成員依成員的順序取色
（與 Tableau 相同不隨工作表的排序改變）。以 Vega 實際繪製每一張圖檢查沒有運算式錯誤。其餘 5 份：4 份 TabMon
要求檢視者輸入自己的資料庫帳號（依設計列為不支援，見「依檢視者的身分連線」），1 份是 twbparser 手寫的測試檔
（不是 Tableau 存出的檔案）。

以 5 份附有作者 Tableau 截圖的公開報表（汽車銷售、HR 分析、股市分析、Superstore 銷售績效、HR 人資儀表板）實測：
萃取匯入自檢全部一致，截圖上看得到的數字（KPI、圖上標籤、明細表）都與 Tableau 相同；股市報表的截圖是把參數拖到
另一個日期後截的，代入同樣的參數後也相同。實測補上的顯示：「K／M」單位換算與正負數分段的數字格式、工作表上設定的
數字與日期格式、自訂的標籤文字（KPI 卡）與工作表標題（代入參數）、醒目提示表、固定列高可捲動的文字表、
欄列架上離散的量值（顯示成表格的欄）、連續日期的時間軸、分面的圓餅，以及總計（Grand Total）：與 Tableau 的
「自動」相同，在去掉該架維度的詳細程度重新彙總（平均是所有資料列的平均、相異計數是整體的相異數），HR 分析的
工作滿意度表的總計列、總計欄與右下角的 1,470 都與截圖相同（見「總計」）；成員別名、Measure Names 的別名與
參數的「顯示為」也已支援（只改變顯示，股市報表的 Apple.csv 顯示為 Apple；見「別名」），標題中的欄位值
（KPI 卡標題中的合計、篩選到的地區）以整張工作表計算（見「標題中的欄位值」）。156 份公開報表中
可直接開始的從 23 份增加到 37 份。巢狀 Table Calculation 也已支援（內層先算成欄位，巢狀計算各自的
計算方向），HR 人資報表原本無法查詢的 5 張工作表都能查詢。盤點也加上產生查詢時才知道的不支援
（例如總計中的 Table Calculation）。Story 也已支援：故事點顯示的 dashboard 或工作表，以及記下的篩選動作、
篩選與選取（見「已知限制與待驗證」），含 Story 的 3 份公開報表因此可直接開始，共 40 份。
集合（Set）也已支援：Top N、固定成員與條件集合轉成 TRUE／FALSE 的欄位，IN/OUT 顯示為 In／Out（或欄位實例上的
別名），可以放在架上、篩選架與計算欄位中；Top N 與條件集合與 Tableau 相同，以內容篩選後、維度篩選前的資料決定成員。
集合動作（點選標記改變集合的成員），以及篩選中連續勾選的成員（Tableau 記成一段 from–to）也已支援
（見「已知限制與待驗證」）。小計（Subtotal）也已支援：與總計相同在去掉之後維度的詳細程度重新彙總，
緊接在各組之後；交叉表的欄改為依欄維度自己的排序排列，第一列沒有的欄不再排到後面（見「總計」「欄標題的順序」）。
巢狀 LOD（LOD 的運算式中再用 LOD）也已支援：內層的 INCLUDE／EXCLUDE 相對於外層的維度，功能報表以
每區客戶的平均銷售額與每區最大一張訂單兩張工作表比對（見「巢狀 LOD」）。
色彩也已支援：資料源與工作表中指定給各值的顏色（含 Measure Names）、內建與自訂的連續色盤（反轉、分段、
範圍的起訖、發散的中心與 Use Full Color Range）、標記卡的顏色、不透明度與框線，沒有設定時與 Tableau 的自動色彩相同
（見「色彩」）。格式也已支援：多邊形、座標軸（隱藏、自訂標題、固定範圍、反轉、同步的雙軸）、格線與座標軸線、
離散欄位的標題與欄位標籤、工作表與 dashboard 的背景（可以透明）、文字的顏色，以及 dashboard 區塊的框線、外距、
內距與背景（有框線或背景的容器也畫出）；Snow Sports 報表的卡片框線、背景圖與地圖上的度假村區塊因此與 Tableau 相同
（見「格式」）。工作表的顯示大小也已支援：標準、符合寬度、符合高度與整個檢視（工作表本身與 dashboard 中各區塊
各自的設定），標準大小時離散的列每一列 20 像素（文字表與圓點表格的每一格有多行文字、或字大到一行放不下時，列高跟著加高到放得下）、放不下時在區塊內捲動；設定過的列高、欄寬與標題寬度，以及列與欄的
色帶與分隔線也照樣畫出（見「格式」）。列標題與 Tableau 相同每一層一欄（成員靠左、沒有刻度，外層的成員只寫在
各組的第一列、欄位標籤橫寫在各欄上方），Tableau 預設的分隔線（最內層以外的每一層之間，窗格與標題區域）也畫出；
文字表的數字靠右、文字靠左（或依「窗格」設定的對齊），欄依內容的寬度不拉寬，交叉表的版面因此與 Tableau 相同。
字型也已移轉：沒有設定時與 Tableau 相同為 9pt（標題 15pt），活頁簿、工作表、欄位與標記標籤的字型、字級與粗細照設定
（見「格式」）。資料源分頁也已加上：
與 Tableau Desktop 的「資料來源」分頁相同，看得到連線、資料表與關聯、資料源篩選、欄位（公式）與前 1,000 列的
資料，資料預覽與工作表相同套用資料源篩選與列層級權限（見「資料源分頁」）。

地圖也已支援（見「地圖」）：地理角色、Latitude／Longitude (generated)、符號地圖、填色地圖（國家、州／省、
美國的郡）、密度圖、標記圖層與雙軸地圖、背景地圖的樣式與圖層、「編輯位置」與存檔時固定的範圍、地圖上的參考線與
分格的地圖，以及空間資料：Shapefile、GeoJSON、KML、TopoJSON 等空間檔案（含 .prj 的投影座標）與萃取中的形狀，
MAKEPOINT、MAKELINE、DISTANCE、BUFFER、AREA 與 COLLECT 等空間函式（與 Tableau 的 Hyper 引擎逐一比對，數字的差距
在 1e-6 以內），以及空間 join（Intersects，例如 CSV 的地點以 MAKEPOINT 對到所在的行政區）。前面 113 份公開報表中的 18 份地圖報表（含 2 份附有作者截圖）全部解析通過、都能畫出；
Superstore 銷售績效的填色地圖與截圖相同（中部各州、Blue-Teal 的自動色盤與州名、銷售標籤）。

桌面版也能從資料檔建立報表（見「連線到資料與資料源的編輯」）：連線到 Excel、文字檔或空間檔案，在資料源的編輯畫面
以關聯、join、聯集加入其他資料表、樞紐、修改欄位的名稱、隱藏、角色與資料類型，再到工作表建立檢視；樞紐、聯集與
合併不相符的欄位也能從 Tableau 的報表移轉（見「Live 連線」之後的說明）。

示範與功能報表的預期結果由獨立的 Python 程式計算，**不是 Tableau 產生**；換上真實報表、以 Tableau 匯出的 CSV 比對，
才算完成 Spec 的驗收（見下方「已知限制與待驗證」）。

## 快速開始

需要 [uv](https://docs.astral.sh/uv/)。Windows 可用 `winget install --id=astral-sh.uv -e` 安裝。

```bash
uv sync                                      # 安裝 Python 3.12 與所有相依套件
uv run tmig verify fixtures/superstore_demo  # 示範報表的計算比對（第一次會自動匯入萃取）
uv run tmig serve                            # 開啟 http://127.0.0.1:8000 看 dashboard
```

檢視器的建置結果已在 `web/dist`，不需要 Node。一鍵檢查：

```bash
uv run python scripts/check.py       # ruff、pyright、pytest
uv run python scripts/check.py --fix # 先自動修正 lint 與格式
```

端到端測試需要 Playwright 的 Chromium（`uv run playwright install chromium`），沒有時會略過。

## 移轉一份真實報表

第 1、2、6 步要在能連到 Tableau Server 的電腦上執行（不需要管理員權限，登入方式見 [tools/README.md](tools/README.md)）。

1. **盤點並挑報表**：列出看得到的報表、下載結構（不含萃取）並逐份盤點，排出起步候選

   ```bash
   uv run python tools/tableau_server/tableau_server.py inventory --server https://<server> --site <site> --pat-name <權杖名稱>
   ```

   看 `inventory/inventory.md` 的 A 級、B 級候選，各挑一份有擁有者願意簽核的。手上已經有 .twbx 的話，
   也可以直接 `uv run tmig inventory <資料夾>`
2. **下載**：`tableau_server.py fetch`（同上的登入參數）`--workbook "<報表名稱>"`，下載含萃取的 .twbx 到
   `fixtures/<報表>/`，並產生起始的 `cases.yaml`。報表連到 Published Data Source 時，一併下載資料源的
   .tdsx（含萃取）併入報表（見「Published Data Source」）；報表用到使用者函式時，以登入帳號填好 `user`
   （預期結果要用同一個帳號抓取，見「列層級權限」）
3. **解析**：`uv run tmig parse fixtures/<報表>/<報表>.twbx --out fixtures/<報表>/golden.ir.json`
   - 結束碼 3 表示有不支援的元素，清單會印出來；unsupported 必須是空的才往下
   - 人工對照 Tableau Desktop 核對 IR（欄位、篩選、計算方向、區塊位置）後提交，之後測試會要求解析結果與它逐字相同
4. **匯入萃取**：`uv run tmig import-hyper fixtures/<報表>/<報表>.twbx`（自檢不一致會失敗）
5. **篩選組合**：`uv run tmig cases suggest fixtures/<報表> --write` 依 dashboard 的篩選與資料加入 3 組
   （常用值、日期邊界、參數或組合），再請擁有者確認是常用的組合
6. **抓預期結果**：見 [tools/README.md](tools/README.md)，抓完提交
7. **欄位對照**：`uv run tmig mapping suggest fixtures/<報表>`，人工確認後把 `confirmed` 改成 `true`
8. **計算比對**：`uv run tmig verify fixtures/<報表>`，差異報告在 `report/calc.json`；修到 fail 為 0
9. **目視與簽核**：`uv run tmig serve` 開 dashboard；`uv run python tools/compare_screens/compare_screens.py fixtures/<報表>` 產生並列頁給擁有者簽核
10. **上線設定**（需要站台管理員）：`tableau_server.py directory`、`permissions`、`schedules --timezone <時區>` 匯出
    使用者與群組、權限、排程與訂閱；依 `service/auth.example.toml`、`service/mail.example.toml` 寫好設定後，
    `uv run tmig serve --auth auth.toml` 提供檢視，`uv run tmig schedule serve schedules.json --mail mail.toml --auth auth.toml`
    執行萃取更新與訂閱（見「身分驗證」「權限」「排程與訂閱」）

除錯時可以單獨看某張工作表的 SQL 與結果：

```bash
uv run tmig sql fixtures/<報表>/golden.ir.json "工作表名稱" --case <篩選組合>
uv run tmig query fixtures/<報表>/golden.ir.json "工作表名稱" --case <篩選組合>
```

SQL 依 Tableau 的八段運算順序分成 CTE，並以註解標出每一段與下推界線。

## Live 連線

資料源是 Live 連線的報表（Oracle、SQL Server、MySQL、Snowflake、Databricks、Spark SQL、PostgreSQL、Redshift、Presto／Trino、Athena、BigQuery、Teradata、Hive、Impala、IBM Db2、SAP HANA），查詢時連到來源資料庫：

1. `uv run tmig connections template <報表或資料夾> --out connections.toml` 產生連線設定檔範本
2. 帳號、密碼、權杖在設定檔中一律寫成 `${環境變數}`（直接寫值會被拒絕），由部署環境或密碼管理服務提供；
   請使用唯讀的服務帳號。設定檔的位置由環境變數 `TMIG_CONNECTIONS` 指定，預設為目前目錄的 `connections.toml`
3. `uv run tmig connections check <報表或資料夾>` 檢查設定、環境變數，並以唯讀查詢測試連線
4. 之後 `tmig verify`、`tmig query`、`tmig serve` 遇到 Live 連線都會依設定檔連線

連線驅動依實際使用的資料庫安裝：`uv sync --extra oracle`、`--extra sqlserver`、`--extra mysql`、`--extra snowflake`、
`--extra databricks`、`--extra postgres`、`--extra redshift`、`--extra trino`（PrestoDB 為 `--extra presto`）、
`--extra athena`、`--extra bigquery`、`--extra teradata`、`--extra hive`（Spark SQL、Hive、Impala 共用，HiveServer2 協定）、
`--extra db2`、`--extra hana`。
IR 只記連線類型、主機、資料庫與資料表，不記任何憑證。

各資料庫的轉譯與驗證程度（`kernel/src/tmig/sqlgen/dialect.py`）：

| 資料庫 | 驗證 | 說明 |
| --- | --- | --- |
| PostgreSQL 16 以上 | 實跑 | |
| Databricks／Spark 3.4 以上 | 實跑（本機 Spark） | 工作階段的時區為 UTC |
| Spark SQL（Spark Thrift Server，例如 Amazon EMR、自建 Spark；Tableau 的 Spark SQL 連線） | 實跑（本機以 Spark Thrift Server 經 HiveServer2 協定） | 與 Databricks 相同的轉譯；主機是 Databricks 時改用 Databricks 的驅動。以 HiveServer2 協定連線：`auth_mechanism` 預設為 PLAIN（有密碼時為 LDAP），可改為 NOSASL、GSSAPI（Kerberos）、JWT；`http_path` 有值時以 HTTP 傳輸（例如經過 Knox）；工作階段的時區為 UTC |
| MySQL 8.0 以上（MariaDB、Amazon Aurora for MySQL、Google Cloud SQL 依 MySQL） | 實跑 | 工作階段為唯讀、UTC、英文的月份與星期名稱；布林值是 1／0，取回後轉回布林；沒有 MEDIAN、PERCENTILE、CORR、COVAR 與 FINDNTH |
| SQL Server 2017 以上、Azure SQL | 模擬（依官方文件） | 布林值以 BIT 表示；非英文的字串常值為 `N'…'`；星期不受 `@@DATEFIRST` 影響；沒有正規表示式、DATEPARSE、MEDIAN、PERCENTILE、CORR、COVAR 與 FINDNTH；SPLIT 的位置須為 ±1 到 ±10 的常值 |
| Snowflake | 模擬（依官方文件） | 工作階段的時區為 UTC；星期與週數以 ISO 的函式計算，不受 WEEK_START 影響 |
| Oracle 12.2 以上 | 模擬（依官方文件） | 沒有布林值（布林維度等列為不支援） |
| Teradata Vantage 16.20 以上 | 模擬（依官方文件） | 沿用與 Oracle 相容的函式；沒有布林值（布林維度等列為不支援）與陣列（FINDNTH、SPLIT 的負數位置）；工作階段的時區為 UTC、日期格式為 ANSIDATE；日期加減一律以 TIMESTAMP(6) 計算（時間單位一次最多加減 9999 天）；子查詢中的 LIMIT 改成 QUALIFY ROW_NUMBER()；REGEXP_EXTRACT 取群組時樣式須為常值；轉成字串為 `VARCHAR(4000) CHARACTER SET UNICODE` |
| Amazon Redshift | 模擬（依官方文件） | 工作階段的時區為 UTC；正規表示式以 PCRE 解讀，只能取第 1 個群組、取代字串不能參照群組；沒有陣列（FINDNTH）與 CORR、COVAR；文字轉數字、日期時先以正規表示式檢查（沒有 TRY_CAST） |
| Trino、Starburst（Tableau 的 Presto 連線）、PrestoDB | 實跑（CI 以 Docker 的 Trino 479；本機模擬） | 工作階段的時區為 UTC；時間以毫秒表示（PrestoDB、Athena 不支援 `TIMESTAMP(p)`，日期加減的最小單位也是毫秒）；MEDIAN、PERCENTILE 以排序後的陣列內插，與萃取時相同是精確值（Trino 只有近似的 approx_percentile）；伺服器是 PrestoDB 時設定檔用 `driver = "presto"` |
| Amazon Athena（engine 3） | 實跑（SQL 在 CI 的 Trino 執行；連線本身未實機驗證） | 與 Trino 相同的轉譯；查詢結果寫到 `s3_staging_dir` 或工作群組 `work_group` 設定的位置；AWS 憑證省略時用預設的來源（環境變數、設定檔 profile 或 IAM 角色） |
| Google BigQuery | 模擬（依官方文件）；執行器在 CI 以 BigQuery 模擬器實跑，連線本身未實機驗證 | 日期時間欄位一律以 DATETIME 計算（TIMESTAMP 欄位依 UTC 換算，與 Tableau 相同）；DATEDIFF 以 DATETIME_DIFF 計算邊界數；REGEXP_EXTRACT 的樣式與群組須為常值；MEDIAN、PERCENTILE 以排序後的 ARRAY_AGG 內插（精確值）；憑證為服務帳號金鑰檔的路徑或 Application Default Credentials，可用 `maximum_bytes_billed` 限制每次查詢的掃描量。BigQuery 模擬器（goccy/bigquery-emulator）執行公式與工作表的結果不可靠（一元負號、1970 年以前的日期等有錯，查詢多時記憶體持續增加），所以公式與工作表以模擬比對；模擬器找到的除法常值 Null、Null 陣列的 FINDNTH 已修正 |
| Apache Hive 2.3 以上（Tableau 的 Cloudera Hadoop、Hortonworks Hadoop Hive、MapR Hadoop Hive、Amazon EMR Hadoop Hive 連線） | 模擬（依官方文件） | 以 HiveServer2 協定連線（同 Spark SQL）；工作階段的時區為 UTC（Hive 3 起；Hive 2 維持伺服器的時區，伺服器的時區有日光節約時間時，跨越切換日的時間加減可能差一小時）；時間的加減以秒數計算，月份的加減從月初加起（月底的行為與萃取時相同）；Null 也相等的比較為 `<=>`；沒有陣列片段（FINDNTH）；子查詢只能在條件中（集合的成員、Top N 集合等列為不支援）；MEDIAN、PERCENTILE 以排序後的 COLLECT_LIST 內插（精確值）；DATEPARSE 精確到秒（Hive 2 的解析較寬鬆）；GROUP BY 不以位置分組 |
| IBM Db2 for Linux、UNIX and Windows 11.5 以上 | 模擬（依官方文件） | 沿用 Oracle 的規則：沒有布林值（布林維度、集合的成員等列為不支援）；字串以字元計算（`CODEUNITS32`，預設以位元組計算會切斷中文）；日期加減以 ADD_DAYS、ADD_SECONDS 等，月份從月初加起（月底的行為與萃取時相同）；天數差以 DAYS() 相減；DATEPARSE 以 TIMESTAMP_FORMAT（格式中的文字只能是分隔字元），文字轉數字、日期時先以正規表示式檢查（沒有 TRY_CAST）；沒有 FINDNTH；SPLIT 只支援正數位置；`ssl = true` 以 SSL 連線 |
| SAP HANA 2.0（SPS05 以上）、HANA Cloud | 模擬（依官方文件） | 查詢結果中的布林值寫成 CASE（比較不能直接當成值）；Null 也相等的比較寫成 = 或兩者皆 Null；ISO 週與年取自 ISOWEEK；正規表示式以 LIKE_REGEXPR、SUBSTRING_REGEXPR、REPLACE_REGEXPR（樣式須為常值）；DATEPARSE 以 TO_TIMESTAMP（格式中的文字只能是分隔字元）；沒有 COVAR、COVARP 與 FINDNTH；SPLIT 只支援正數位置；多租戶時以 `database` 指定租戶，HANA Cloud 預設以 TLS 連線 |
| Apache Impala 3.3 以上（Cloudera Hadoop 連到 Impala：連接埠 21050 或類型標示 Impala；Tableau 的 Impala 連線） | 模擬（依官方文件） | 與 Hive 相同的規則；時間的加減以 DAYS_ADD、MONTHS_ADD、MICROSECONDS_ADD 等函式；截斷以 TRUNC 與 DATE_TRUNC；正規表示式為 RE2；沒有陣列、精確的 MEDIAN、PERCENTILE 與 CORR、COVAR；SPLIT 的位置須為正整數常值；DATEPARSE 的格式不支援月份名稱、上下午與含英文字母的文字；LENGTH 計算位元組（非 ASCII 字元與 Tableau 不同）；工作階段的時區為 UTC（Impala 3.1 起） |

Excel、文字檔（CSV 等）沒有萃取時不是連到資料庫：匯入時依 Tableau 讀到的欄名與型別讀進 DuckDB，與萃取相同在本機查詢
（`kernel/src/tmig/data/file_import.py`）。支援 .xlsx、.xls 與文字檔的單一資料表、join 與 Relationship 模型；
文字檔依記下的分隔字元、字元集與地區設定解讀數字與日期，不符合型別的值成為 Null 並在匯入結果中列出。
**資料解譯器**（Excel 的連線 `cleaning='yes'`，範圍 `gridOrigin` 從第 2 列起時上面還有標題、說明的列）：在範圍中
依記下的欄名找標題列（空白的標題記成 F<第幾欄>，與 Tableau 相同），標題列之前的列與之後空白的列不算；以公開報表與
它的來源檔驗證（144 列、各年的總和與 Excel 相同）。文字檔也可以使用資料解譯器（.twb 的 `cleaning='yes'`；標題列
之前的說明列與之後空白的列不算）。**Excel 的具名範圍**（.xlsx、.xls）也列為資料表：資料在它所在工作表的那一塊範圍
（只列單一塊連續的範圍；.xls 的具名範圍尚不支援）。檔案的內容是報表封裝時的快照（與 Tableau Server 上的 .twbx
相同）。

樞紐、聯集與合併不相符的欄位（Live 連線與檔案都支援，在來源資料庫或 DuckDB 中以子查詢計算）：

- **樞紐**（`<relation type='pivot'>`，資料源分頁的「樞紐」）：選取的欄轉成列，名稱欄（Pivot Field Names）是欄名、
  值欄（Pivot Field Values）是它的值，轉成記下的型別；來源的每一列對每個欄位各一列，值是 Null 也保留（UNION ALL）。
  其他的欄位照樣帶著（欄位的 parent-name 是來源的資料表，樞紐的兩欄是樞紐）
- **聯集**（`<relation type='union'>`）：資料表上下相接，欄位依名稱對應，沒有這一欄的資料表為 Null，型別一律轉成
  記下的型別。另有來源的欄位：Table Name（資料表的名稱）、Sheet（Excel 的工作表）與 Path（文字檔在作者電腦上的
  路徑）。檔案依各資料表記下的欄位判斷有沒有這一欄；Live 連線沒有記下各資料表的欄位，視為每張都有聯集的所有欄位
  （有資料表缺欄時查詢失敗，不會默默算錯）
- **合併不相符的欄位**（聯集外面的 `<relation type='project'>`）：新欄位的公式（`IF NOT ISNULL([聯集].[A]) THEN …
  ELSEIF …`）與 join 計算相同轉成 SQL，取第一個不是 Null 的值
- **萬用字元聯集**（`<relation type='batch-union'>`，`kernel/src/tmig/parser/wildcard.py`）：依檔名的模式
  （`<database-filter>`，`*`、`?` 為萬用字元、不分大小寫）與 Excel 工作表名稱的模式（`<table-filter>`，空白為全部），
  在 .twbx 中連線的檔案所在的資料夾（`is-recursive` 時含子資料夾）找出符合的檔案，展開成一般的聯集（各檔案的欄位依
  它自己的標題列，另有 Path：檔案在作者電腦上的路徑、Sheet）。.twbx 中只有封裝時符合的檔案（快照）；
  `include-siblings` 的意義未在公開資料中說明，推定為同一層的檔案
- 都可以是 Relationship 模型的邏輯資料表、與其他資料表 join，資料源分頁列出它們的來源與說明。XML 依公開報表
  （2020.4 的樞紐與萬用字元聯集、2019.1 的聯集與合併的欄位）

**跨資料庫的 join**（Tableau 的 cross-database join：資料源有多個連線，例如 Excel 與 SQL Server、兩個不同的資料庫，
`kernel/src/tmig/data/federated.py`）：與 Tableau 相同，各連線的資料取到本機、在本機的引擎（DuckDB）中合併。檔案在匯入時
讀進資料源的 DuckDB 檔；資料庫的資料表與自訂 SQL（IR 中為 `FileTable.format = "live"`，帶著它的連線）在查詢時取回：資料表
只取查詢用到的欄（以 SQLGlot 分析 CTE 中真正用到的欄）、查詢沒有用到的資料表不取（例如 Relationship 模型中檢視沒有用到的
邏輯資料表），自訂 SQL 取整段的結果（參數代入目前值）；之後整段查詢與檔案的資料源相同在 DuckDB 執行。取回的資料在 TTL
之內重複使用（預設 60 秒，環境變數 `TMIG_FEDERATED_TTL`，0 為不重複使用；dashboard 的各張工作表要的欄都已取回時直接使用，
同時要同一張資料表時只取一次）。join、關聯、join 計算與關聯計算、樞紐都可以跨連線；聯集要在同一個連線（與 Tableau 相同）。
各資料庫的憑證依連線代號由連線設定檔提供（`tmig connections template` 列出，標示「跨資料庫的 join」），與 Live 連線相同。
只用到同一個資料庫的資料源（例如沒有用到的連線）是一般的 Live 連線，整段查詢下推。跨資料庫的 join 也可以當 Data Blending
的次要資料源（見「Data Blending」）。以臨時的 PostgreSQL 實跑（Excel＋PostgreSQL 的 LEFT JOIN、兩個 PostgreSQL 資料庫的
Relationship 模型與含參數的自訂 SQL、Data Blending 的次要資料源）比對。

**空間函式**（MAKEPOINT、MAKELINE、DISTANCE、BUFFER、AREA、INTERSECTS）在 Live 連線與萃取相同在 DuckDB 計算（球面的
計算與 Tableau 相同，見「地圖」）：用到空間函式的 CTE 與之後依賴它的 CTE 改在 DuckDB 執行，之前的部分（資料源的資料列與
資料源篩選之前）下推到來源資料庫、只取用到的欄；讀資料表的那一段就用到空間函式時（例如 join 計算中的 DISTANCE），取回
查詢讀的每張資料表（只取用到的欄）、以原本的名稱交給 DuckDB 執行整段查詢。資料庫的**空間欄位**（Tableau 2021.2 支援的
PostGIS 的 geometry、geography，SQL Server 的 geography，Oracle 的 SDO_GEOMETRY，Snowflake 的 GEOGRAPHY 與 Redshift 的
GEOMETRY；另有 MySQL、BigQuery、SAP HANA、Teradata、Db2）在來源資料庫轉成 WKT 文字（`ST_AsText`、`STAsText()`、
`SDO_UTIL.TO_WKTGEOMETRY`、`ST_ASWKT` 等；MySQL 8 的 SRID 4326 依 EPSG 的軸序是緯度在前，轉換時指定經度在前），
Relationship 模型的邏輯資料表中的空間欄位相同。桌面版連線到資料庫時，資料表的空間欄位（PostGIS 的 geometry、geography
等）列為空間欄位；自訂 SQL 選出的 PostGIS、Redshift 空間欄位依取回的值（含 SRID 的 EWKB）判斷。

**空間 join**（Intersects）的兩邊都是資料庫的空間欄位時，與 Tableau 的 Live 連線相同在資料庫計算：PostGIS、Redshift 的
`ST_Intersects`，SQL Server 的 `a.STIntersects(b) = 1`，Oracle 的 `SDO_GEOM.RELATE(a, 'ANYINTERACT', b, 0.005) = 'TRUE'`
（不需要空間索引），Snowflake、BigQuery 的 `ST_INTERSECTS`，SAP HANA、Teradata 的 `a.ST_Intersects(b) = 1`，Db2 的
`DB2GSE.ST_Intersects`，MySQL 的 `ST_Intersects`；整段查詢下推（geometry 依資料庫的平面計算，geography 依球面）。一邊是
join 計算（例如 MAKEPOINT([lat], [lon])）時，取回兩張資料表（空間欄位在資料庫轉成 WKT）在 DuckDB 計算。

以臨時的 PostgreSQL + PostGIS 3.4 與 MySQL 8 實跑比對：空間欄位的形狀（geometry、geography）、距離與面積、PostGIS 中下推的
空間 join（geometry 與 geography，inner 與 left）、join 計算 MAKEPOINT 與多邊形的空間 join、Relationship 模型的空間欄位、
PostGIS 的點與匯入的 GeoJSON 的跨資料庫空間 join、MySQL 的軸序與空間 join；另以 PostgreSQL 實跑距離、BUFFER 的面積、
點與線、資料源篩選與 join 計算中的距離。其他資料庫的轉換有轉譯快照。

「實跑」是公式的每個函式與兩份 fixture 的每張工作表 × 每組篩選都在該資料庫執行，與預期結果比對；「模擬」是轉譯後的
SQL 以 SQLGlot 轉回 DuckDB、該資料庫特有的函式依官方文件的語意模擬後執行同樣的比對，並有轉譯快照
（`fixtures/*/sql/<方言>/`）供審核，仍待實機驗證。與 Tableau 直接連該資料庫時相同，字串的比較與排序依資料庫的定序
（SQL Server、MySQL 預設不分大小寫），轉成字串的小數格式依資料庫。

### Initial SQL

連線的 Initial SQL（Tableau 的「初始 SQL」，.twb 連線的 `one-time-sql`）記在連線上（`LiveConnection.initial_sql`），
平台**連上來源資料庫時執行一次**，之後這條連線上的查詢都看得到它的結果（`kernel/src/tmig/data/initial_sql.py`、
`tmig.data.sources.open_executor`）：設定工作階段（`SET search_path`、`ALTER SESSION`）、建立暫存資料表給自訂 SQL
使用、把登入者帶給資料庫（Oracle VPD 的 `DBMS_SESSION.SET_IDENTIFIER`、SQL Server 的 `EXECUTE AS USER`）。
檢視、篩選控制項的可選值、動態參數、資料警示、發布前的檢查、`tmig query`、`tmig verify` 與更新萃取
（萃取的來源連線）都一樣。

- **參數**（與 Tableau 相同的五個，名稱不分大小寫）：`[TableauServerUser]` 登入者的帳號、`[TableauServerUserFull]`
  帶網域的帳號（`網域\帳號`，沒有網域時與帳號相同）、`[TableauApp]`（伺服器上是 `Tableau Server`、桌面版是
  `Tableau Desktop Professional`：沿用 Tableau 的名稱，依它判斷環境的 Initial SQL 照原本的方式執行）、
  `[TableauVersion]`（`2021.2`）、`[WorkbookName]` 報表的名稱。值依連線的方言跳脫（單引號重複一次；MySQL、BigQuery、
  Spark 等以反斜線跳脫的方言連反斜線一起），帳號含這些字元時不會跳出字串常值。沒有登入者時（排程的萃取更新、
  沒有設定身分驗證）帳號是空字串；`tmig verify`、`tmig query` 代入 cases.yaml 的 `user`（或 `--user`）
- **`<ServerOnly>…</ServerOnly>`**：只在伺服器上執行的部分，桌面版不執行（與 Tableau 相同）
- **每位登入者各自一條連線**：代入參數後內容不同的 Initial SQL 各自開一條連線（用到 `[TableauServerUser]` 時每位
  登入者一條），工作階段的設定不會互相影響；跨資料庫的 join 取回的資料與動態參數的快取也依它分開。同一個連線最多
  同時保留 16 條這樣的連線（環境變數 `TMIG_SESSION_CONNECTIONS`），超過時關掉最久沒用的
- **執行的方式**：依最外層的分號拆成一句一句送出（字串、識別字、註解與 PostgreSQL 的 `$…$` 中的分號不算）；
  SQL Server 整段是一個批次（變數只在同一個批次裡有效），Oracle、Db2、SAP HANA、Teradata 以 `BEGIN`、`DECLARE`
  開頭時是程序區塊，整段送出。平台的連線平常是唯讀的工作階段，執行 Initial SQL 時暫時解除（PostgreSQL、MySQL
  才能建立暫存資料表），之後恢復。執行失敗時連線關閉，查詢回報「連線 … 的 Initial SQL 執行失敗」與資料庫的訊息
- **停用**：環境變數 `TMIG_INITIAL_SQL=off` 時不送出任何 Initial SQL（Tableau Server 的
  `vizqlserver.initialsql.disabled`），報表照常開啟
- **在桌面版設定**：資料源分頁的「Initial SQL…」（見「連線到資料與資料源的編輯」）

平台不檢查 Initial SQL 的內容，原樣送給資料庫（與 Tableau 相同）：它是報表作者寫的 SQL，請用只有需要的權限的服務帳號
連線。以 PostgreSQL 16 與 MySQL 8 實跑測試（設定工作階段、建立暫存資料表、自訂 SQL 讀到 Initial SQL 記下的登入者、
之後的查詢仍是唯讀；`kernel/tests/data/test_initial_sql.py`）；其他資料庫沒有實機，正式使用前須在公司的資料庫實測。
BigQuery、Athena 的每次查詢各自獨立（沒有工作階段）：敘述會執行，但 `SET` 與暫存資料表不會留到之後的查詢。

### 依檢視者的身分連線

平台一律以部署環境提供的服務帳號連線。Tableau 上依檢視者的身分查詢資料庫、由資料庫端過濾資料的報表，
移轉後每個人會看到相同的資料，所以盤點時就列出（`tmig census`、`tmig inventory` 的「連線不依賴檢視者的身分」）：

- **不符合起步條件**（解析時也列入 unsupported）：Tableau Server 上要求檢視者輸入自己的資料庫帳號（伺服器盤點比對
  REST API 的「沒有嵌入密碼」，或 .twb 的 `workgroup-auth-mode='prompt'`）、以檢視者的身分模擬（impersonation）。
  這類報表要改用列層級權限（使用者函式或使用者篩選）重寫後再移轉
- **需注意**：Windows 整合驗證與 Kerberos（伺服器可能以 Run As 服務帳號連線，也可能以 Kerberos 委派用檢視者的身分，
  請與 DBA 確認）、OAuth（伺服器上嵌入了登入資訊時不列）、Initial SQL 用到 `[TableauServerUser]`／
  `[TableauServerUserFull]`（平台會執行、代入平台登入的帳號：請確認平台的帳號與資料庫端認得的身分相同；萃取來源的
  在排程更新時沒有登入者）、伺服器上沒有嵌入密碼但對不到資料源的連線。其他的 Initial SQL 平台照樣執行，不列

萃取型的資料源檢視時讀萃取檔，與檢視者的帳號無關，不列。伺服器盤點的連線設定是目前的狀態：.twb 記錄的是發佈時的設定，
之後在伺服器上嵌入了密碼的連線不列。確認資料庫端沒有依人不同的權限（例如所有檢視者本來就用同一個唯讀帳號）時，
在 Tableau 上改成嵌入密碼重新發佈、再重新下載與盤點，這個決定才會留下紀錄。

方言轉譯（`kernel/src/tmig/sqlgen/dialect.py`）只送出確定語意相同的 SQL：轉譯後的每個節點都要在該方言的允許清單中，
轉不了的函式列為不支援，不會送出可能算錯的 SQL。驗證方式：

- 實跑：PostgreSQL（16 以上）、Spark（3.4 以上，Databricks 的引擎；Spark SQL 另以本機的 Spark Thrift Server
  走 HiveServer2 協定）、MySQL、Trino（CI 以 Docker）：每個公式函式與兩份報表的每張工作表 × 每組篩選實跑比對
  （`kernel/tests/sqlgen/test_engines.py`；本機有 PostgreSQL、MySQL 時自動執行，Spark 需要 `uv sync --group spark`
  與 Java 17 以上）
- 模擬：Oracle、SQL Server、Snowflake、Redshift、BigQuery、Teradata、Hive、Impala、Db2、SAP HANA 尚無實機。轉譯後的 SQL 以 SQLGlot
  轉回 DuckDB、各自特有的函式依官方文件的語意模擬後執行同樣的比對，並保留轉譯快照（`fixtures/*/sql/<方言>/`）
  供審核；正式使用前須在公司的資料庫實測

## Relationship 模型

Tableau 2020.2 起多表資料源預設的 Relationship 模型（邏輯資料表以關聯連成一棵樹），Live 連線與萃取都支援
（萃取每張邏輯資料表一張 .hyper 資料表，匯入同一個 DuckDB 檔、各自自檢）。查詢依 Tableau 文件的語意
（`kernel/src/tmig/sqlgen/model.py`）：

- 只查檢視用到的資料表；量值在自己的資料表彙總，其他資料表依「關聯欄位＋用到的欄位」去重後連接，
  一對多時每一組維度值只算一次、不重複計算；沒有對應的量值列以 Null 呈現
- 檢視的維度都來自量值表以外的同一張（或同一個分支的）資料表時，維度表中沒有量值的成員也列出（量值為 Null）
- 只有維度時取內部連接（只列出存在的組合）
- 篩選只用到其他資料表的欄位時，量值表保留「有關聯、且關聯到的列中有符合者」，沒有關聯時以 Null 判斷；
  篩選仍在自己的運算階段套用（例如維度篩選在 FIXED LOD 之後）
- 在比檢視粗的層級彙總（FIXED／EXCLUDE LOD、Top N、依欄位排序、TOTAL）而其他資料表是一對多時，
  量值會重複計算：查詢附帶檢查，執行前確認關聯值都只對應一組欄位值（星狀模型的維度表都是如此），
  否則列為不支援，不給出與 Tableau 不同的數字；不受重複影響的彙總（MIN、MAX、COUNTD、ATTR）不需要檢查
- 量值來自多張資料表（例如一張表的 COUNTD 與另一張表的 FIXED LOD）：每張量值表各自連接其他資料表、
  套用篩選、在檢視的層級彙總，再依維度合併（完整外部連接）：任一張表有的維度值都列出，
  沒有該表的列時它的量值為 Null（COUNTD 與筆數也是 Null）；每個彙總只以所屬量值表的列計算，
  例如 FIXED LOD 只以 LOD 中量值所在的資料表計算、檢視的標記只來自檢視的彙總所屬的資料表
- 邏輯資料表內含 join（實體層，在邏輯資料表上按兩下加入的 join）：Live 連線、檔案與萃取都支援。欄位另記所屬的
  實體資料表（metadata-record 的 parent-name），查詢時邏輯資料表是 join 樹的子查詢（各欄以欄位 id 為欄名，
  各實體資料表的同名欄位不衝突）；join 在邏輯資料表之內先做，關聯再連接邏輯資料表（與 Tableau 相同，
  例如 inner join 去掉的維度成員不會因關聯而出現）。在 DuckDB、PostgreSQL、Spark 與模擬的 Oracle 上實跑比對
- 關聯條件是欄位之間的比較：等號或不等式（=、<>、<、<=、>、>=，以 AND 連接）。較新的版本以功能旗標的屬性
  （`_.fcp.InequalityRelationships.true...op`）寫出不等式、舊版讀到的是等號與一個計算，讀入時取 true 的版本；
  有不等式的關聯不以關聯欄位是否唯一判斷一對多，在比檢視粗的層級彙總時一律列為不支援
- **跨資料表的計算**（一個計算用到多張邏輯資料表的欄位，例如 `SUM([Qty] * [Price])`、
  `SUM(IF [客戶].[區域] = 'East' THEN [訂單].[金額] END)`）：與 Tableau 相同，計算的定義域是它的輸入的內部連接
  （https://help.tableau.com/v2021.2/pro/desktop/en-us/datasource_multitable_analysis_overview.htm 的疑難排解，
  2026-11-22 查閱）——用到的資料表（與連接它們的路徑上的資料表）以關聯內部連接，在連接後的每一列計算（不去重：
  一對多時每個組合各算一次），沒有對應的列不參與。這種彙總在內部連接後的列上彙總，與其他資料表的量值依維度合併
  （完整外部連接）；其他資料表照樣依關聯欄位去重後連接、篩選照樣以半連接套用，FIXED LOD 中的跨資料表彙總同樣如此
- 列層級的跨資料表計算（當維度、篩選或 LOD 的維度，例如 `IFNULL([Category], [Order ID])`）：用到的資料表都有
  對應的列時才計算，否則為 Null；量值表沒有對應的列照樣保留（在 Null）
- 篩選用到的欄位不全在連接結果中（含同時用到量值表與其他資料表、或兩個分支的欄位）：量值表的列保留「關聯到的列
  （沒有關聯時一列 Null）中有一組使條件成立者」，量值表與已連接的欄位取這一列的值

尚不支援（查詢時列為不支援）：含 LOD 運算式、又要以半連接套用（用到不在檢視中的其他資料表欄位）的篩選；關聯條件含
計算（解析時列入 unsupported）。

## Data Blending

同一張工作表用到多個資料源（`<datasources>` 中第一個是主要資料源，之後是次要資料源）時，與 Tableau 相同
（`kernel/src/tmig/sqlgen/blend.py`）：

- 主要資料源照常查詢（篩選、排序、LOD 都以主要資料源計算）；次要資料源以作用中的連結欄位分組、彙總次要資料源的量值，
  再依連結欄位 LEFT JOIN 到主要資料源的結果：主要資料源沒有的值不出現，次要資料源沒有的值為 Null；檢視中有連結欄位以外的
  維度時，次要資料源的值在這些維度上重複
- 連結欄位：.twb 的自訂關聯（`<datasource-relationships>`，例如主要資料源的 Id 對應次要資料源的 VersionId）優先，
  沒有自訂關聯時以名稱相同的維度自動連結；主要資料源的連結欄位在檢視中（欄列架或標記卡上的維度）時作用，
  日期欄位以檢視中的日期衍生連結（檢視是 YEAR(Order Date) 時次要資料源也以年分組）。檢視中沒有連結欄位時列為不支援
- 手動關閉的連結（`<join-lod-exclude-overrides>`）不用；手動設為作用中的連結（`<join-lod-include-overrides>`）在檢視中時
  與自動相同，不在檢視中時（要在更細的層級混合後再彙總）列為不支援
- 次要資料源自己的資料源篩選照樣套用；工作表上主要資料源的篩選不套用到次要資料源
- 桌面版編輯器：資料窗格可以切換瀏覽其他資料源，把其他資料源的欄位放到檢視、量值篩選或排序依據上即混合（沿用其他
  工作表的連結，否則以名稱相同的維度連結），不再用到後不再混合；空白的工作表放上的第一個欄位決定主要資料源。
  「取代主要資料源」以欄位名稱對應換掉主要資料源

做法（「外部彙總」）：與 Tableau 相同，各資料源分開查詢。只用到次要資料源欄位的彙總（次要資料源的量值、以 ATTR 彙總的
維度、計算欄位中的 `SUM([Targets].[Target])`）不從主要資料源的資料列計算：

- 次要資料源的查詢先在它自己的來源執行（萃取、檔案、跨資料庫的 join、Relationship 模型，或 Live 連線的資料庫），依連結
  欄位分組算出這些彙總；結果登記成資料表（`__s0`、`__s1`…）交給執行主要資料源查詢的 DuckDB。跨資料庫的 join 只取回
  用到的欄（TTL 之內重複使用）；Live 連線下推到它的資料庫（例如主要資料源在 PostgreSQL、次要資料源在 MySQL）
- 主要資料源的查詢每次彙總之後併入：彙總的層級含所有連結欄位時（檢視的層級）直接以連結欄位 LEFT JOIN；較粗的層級
  （依欄位排序、TOTAL、總計與小計）先取出該層級中出現的連結值，再把各連結值的結果再彙總
- 併入之後的各段（量值的運算、量值篩選、排序、Table Calculation、總計）與沒有混合時走同一套流程；主要資料源是 Live
  連線時，併入之前的部分下推到資料庫，之後的部分在 DuckDB 計算

以臨時的 PostgreSQL 與 MySQL 實跑：依地區連結、在未連結的維度上重複、以年連結、自訂關聯、計算欄位、次要資料源的維度、
篩選、排序、Table Calculation 與總計，在主要／次要資料源是 PostgreSQL、MySQL 或萃取的五種組合下，結果與兩個都是萃取時
相同；次要資料源是兩個 PostgreSQL 資料庫的 Relationship 模型、桌面版的 Live 連線混合 Excel 的目標也實跑比對。

支援的用法：

- **引用次要資料源欄位的計算欄位**（主要資料源的計算欄位，例如實績 ÷ 目標 `SUM([Sales]) / SUM([Targets].[Target])`）：與
  Tableau 相同，次要資料源的欄位要先彙總；彙總的部分在各自的資料源計算（主要資料源依檢視的層級，次要資料源依連結
  欄位的層級），其餘的運算（四則、IF、ZN 等）在合併的結果上計算，所以次要資料源沒有對應的列時
  `ZN(SUM([Targets].[Target]))` 是 0。計算欄位可以引用其他計算欄位（含另一個引用次要資料源的計算欄位）；檢視中不必
  另外放次要資料源的欄位。這類計算欄位可以放在**量值篩選、排序依據與參考線**上（只放在篩選或排序上、不在檢視中也
  可以），也可以再套用 Table Calculation（`RANK([達成率])`、`SUM([Targets].[Target]) / TOTAL(SUM([Targets].[Target]))`）。
  同一個彙總中同時用到兩個資料源的欄位（`SUM([Sales] * [Targets].[Target])`）列為不支援。編輯器的計算欄位以
  `[資料源的名稱].[欄位]` 引用
- **次要資料源的維度**放在欄列架或標記卡上時，與 Tableau 相同以 ATTR 彙總、不增加標記：連結欄位的一個值對到一個值時
  顯示它，對到多個值時顯示 `*`（文字欄位；日期、數字等其他型別為 Null），沒有對應的列時為 Null。編輯器放上次要
  資料源的維度時自動改成 ATTR
- **次要資料源的維度篩選**（篩選架上次要資料源的維度：值清單、範圍、相對日期）：篩選次要資料源的資料（量值只合計
  通過篩選的列）；主要資料源中連結欄位的值沒有對應到通過篩選的資料時，那些標記不顯示（常見的用法：以次要資料源的
  連結欄位篩選、排除 Null，只留下兩邊都有的值）。篩選包含 Null 時，次要資料源完全沒有對應資料的標記仍顯示。檢視中
  不必放次要資料源的欄位；dashboard 上的篩選控制項可以控制它（可選值來自次要資料源）。**次要資料源欄位的條件與
  Top N 篩選**在次要資料源的查詢中以次要資料源的資料計算（與維度篩選相同，限制顯示的標記）。次要資料源的內容篩選
  列為不支援：Tableau 的「加入內容」只出現在主要資料源欄位的篩選
- **次要資料源的計算欄位中的 LOD**（含 Table Calculation 計算欄位中的）在次要資料源的查詢中計算；依 Tableau 的說明
  （https://help.tableau.com/v2021.2/pro/desktop/en-us/multipleconnections_troubleshooting.htm ，2026-11-22 查閱），
  檢視同時有次要資料源的維度或維度篩選時 Tableau 顯示錯誤，本平台同樣不給出結果。主要資料源全域篩選（「套用到所有
  使用此資料源的工作表」）中的次要資料源欄位篩選照樣套用、屬於同一篩選群組
- **次要資料源的量值篩選**（篩選架上次要資料源彙總的量值，例如 SUM(Target) ≥ 100）：以混合後的值篩選標記，與主要
  資料源的量值篩選相同；沒有對應資料的標記是 Null，篩選包含 Null 時才留下。可以與次要資料源的維度篩選一起用（先篩選
  次要資料源的資料，再以混合後的值篩選）；量值只放在篩選上、不在檢視中也可以
- **依次要資料源的量值排序**：以混合後的值排序維度；排序的層級比連結欄位粗時（例如地區與年都連結、依地區排序），
  各連結值的結果再彙總。Tableau 對這種排序有限制（說明文件：可能無法依次要資料源的欄位排序），.twb 中有時照樣計算
- **次要資料源的 Table Calculation**：次要資料源的量值套用快速 Table Calculation（累計、占比、排名、差異、移動計算等），
  或次要資料源中定義的 Table Calculation 計算欄位（`RUNNING_SUM(SUM([Target]))`、含 TOTAL、引用其他計算欄位），都在
  混合後的結果上沿著主要資料源的維度計算，其中的彙總仍在次要資料源計算；沒有對應資料的標記是 Null（累計沿用前面的
  值）。次要資料源的計算欄位中彙總之上的運算（`RANK(ZN(SUM([Target])))` 的 ZN）也在次要資料源計算，所以沒有對應
  資料的標記仍是 Null；主要資料源的計算欄位（`RANK(ZN(SUM([Targets].[Target])))`）則在混合後計算，ZN 得到 0。
  可以再以 Table Calculation 的結果篩選
- **參考線**：次要資料源的量值或引用它的計算欄位在座標軸上時，參考線、參考區間與分布以混合後的標記值計算
- **總計與小計**：主要資料源的量值在總計的層級重新彙總（與沒有混合時相同）；次要資料源的量值在連結欄位的層級算出後，
  依總計範圍內出現的連結值再彙總（加總、計數、最小、最大、平均與 ATTR；檢視中有連結欄位以外的維度時不重複計算，
  只在次要資料源中的值不算）；引用次要資料源欄位的計算欄位以總計的各部分計算（總計的實績 ÷ 總計的目標）。與次要
  資料源的篩選一起使用時只合計顯示的標記。相異計數、中位數等非加總型的彙總（Tableau 在混合時同樣有限制）與次要
  資料源的彙總計算欄位，在比連結欄位粗的層級（總計、小計、依欄位排序、TOTAL）列為不支援

次要資料源的維度篩選、總計與較粗層級排序的算法，Tableau 的說明文件沒有寫明，依公開資料（Tableau 對非加總型彙總的限制、
以次要資料源的篩選排除 Null 的用法）推定，尚未以 Tableau 匯出的數字驗證；有實際的報表時請以 `tmig verify` 比對。

尚不支援：同一個彙總中同時用到主要與次要資料源的欄位、同一個 LOD 中同時用到主要與次要資料源的欄位、次要資料源的
內容篩選、連結欄位不在檢視中卻設為作用中。連結欄位的 Null 視為相等。

### 跨資料源篩選

Tableau 10 起，篩選可以「套用到所有使用相關資料源的工作表」或選取的工作表：其他資料源的欄位的篩選，以資料源的關聯
（名稱相同或自訂的對應）篩選主要資料源中對應的欄位。.twb 在篩選中以 `<target field>` 記下對應的欄位，解析時直接改成
主要資料源該欄位的篩選（與 Tableau 相同，不篩選次要資料源）：

- 本地的跨資料源篩選（工作表的 `<filter>`，常帶 filter-group）與一般篩選相同，屬於同一篩選群組的工作表一起更新
- 全域的跨資料源篩選存在主要資料源的 `<shared-view>`，工作表的篩選架（`<slices>`）列出其他資料源的欄位；篩選群組與該資料源
  自己的全域篩選相同，任何一張工作表的篩選控制項都一起更新
- 同一欄位已有不設限的篩選時以跨資料源的篩選取代；dashboard 上其他資料源欄位的篩選控制項，控制工作表上對應的跨資料源篩選
- 其他資料源自己的全域篩選出現在以它為次要資料源的工作表的篩選架上：Tableau 的全域篩選依工作表的主要資料源套用
  （https://help.tableau.com/v2021.2/pro/desktop/en-us/filtering_global.htm ，2026-11-22 查閱），無法確認 Tableau
  是否套用，列為不支援

## Published Data Source

報表連到 Published Data Source 時，.twb 只有指向 Tableau Server 的 sqlproxy 連線：資料源的連線、計算欄位的公式與
萃取都在伺服器上。下載資料源的 .tdsx 後併入報表，產生自含的 .twbx，之後與嵌入式資料源完全相同
（`kernel/src/tmig/parser/published.py`）：

- `tableau_server.py fetch` 自動處理：原本的報表在 `original/`、資料源在 `published/`、併入後的 `<報表>.twbx` 在 fixture 根目錄；
  `inventory` 預設也下載資料源的定義（不含萃取）併入後再盤點（`--no-published` 關閉）
- 手上已有 .tdsx 時：`uv run tmig published merge <報表> --published <目錄> --out <報表>.twbx`
- 併入的內容：資料源的連線、欄位與計算欄位（公式）、萃取與物件模型；報表本地的計算欄位、群組，以及欄位的
  顯示名稱、角色、隱藏與格式等覆寫都保留；報表本地另外萃取了資料源時沿用報表的萃取。IR 的 `Datasource.published`
  記下資料源在伺服器上的網址名稱（之後的權限與排程依此對應）

### 伺服器上的資料源

Tableau 下線後，Published Data Source 也發布到新平台（有報表庫時；`service/src/tmig_service/datasources.py`、
`datasource_api.py`）：

- 發布：站台管理的「內容與權限」或首頁的「資料源」頁面上傳 .tdsx（或 .tds），要 Project 的發布權限；同一個網址名稱
  （.tds 的 repository-location，或檔名）再發布為新的版本（要那個資料源的「覆蓋」）。每一版都保留，可以下載
- 報表連到這個資料源（`Datasource.published` 的網址名稱相同、萃取取自 .tdsx）時，查詢服務改用伺服器上的萃取：
  萃取更新一次，每份用到它的報表都看到新的資料；報表自己另外萃取的不改
- 權限與 Tableau 相同：檢視、連線、下載、覆蓋、刪除、設定權限（範本：檢視、探索、發布、管理）；看用到它的報表要
  「連線」的權限；擁有者、Project Leader 與站台管理員不受規則限制；第一次發布時 All Users 可以檢視與連線
- 資料源頁面列出看得到的資料源：連線、萃取最後更新的時間、用到它的報表、欄位（要「連線」）與每一版
- 萃取更新：排程管理的「新增萃取更新」可以選資料源，更新伺服器上的那一份（一次）；匯入的 Tableau 工作中對象是
  Published Data Source 的也一樣。更新成功後用到它的報表的資料警示跟著檢查
- 桌面版發布（與 Tableau Desktop 的「發布資料源」相同）：報表中的一個資料源存成資料源套件（.tmigds：資料源的定義、
  計算欄位用到的參數與萃取，不含任何帳號密碼）上傳。新的資料源由伺服器配給網址名稱（取名稱中的英數字，沒有時用
  資料源的 id，不與現有的重複）；同一個 Project 中已經有同名的資料源時先確認要不要覆蓋；已經連到伺服器上的資料源時
  預設覆蓋它（新的版本）。也可以在資料源頁面上傳 .tmigds

## 萃取層

萃取型的報表平常讀 DuckDB 檔（由 .twbx 內的 .hyper 匯入）。萃取中文字欄位的定序照樣保留：Tableau 的萃取多半是
大小寫不分的定序（`en_US_CI`、`en_IN_CI` 等），在 DuckDB 以 `NOCASE`（重音不分的 `_AI` 加 `NOACCENT`）定序
建立欄位，比較、篩選、分組與排序與 Tableau 相同不分大小寫（公開報表 Credit Card Complaints 以 'In Progress'
比對資料中的 'In progress'，Tableau 算出 329 筆，新平台相同）；依語系的定序（`en_US`、`de_DE` 等）以 DuckDB
的 ICU 語系定序建立（`en_us`；沒有這個地區時用語言，例如 `en_IN` → `en`），排序與 Tableau 相同依語系
（apple、Apple、banana，而不是 Apple、Banana、apple），認不得的語系才依位元組排序並在匯入時提醒。空間資料
（Hyper 的 GEOGRAPHY）以 WKT 文字匯入（與 Tableau 畫出的形狀相同，座標到小數第 7 位）。`tmig extract refresh` 依萃取各欄在來源的位置
（萃取 metadata-record 的 family 與 remote-alias）從來源資料庫重建（`kernel/src/tmig/data/extract_refresh.py`）：

1. 連線設定與 Live 連線相同；`tmig connections template` 也會列出萃取的來源（標示「萃取更新」）
2. `uv run tmig extract refresh <報表> [--out-dir data]`：完整更新；`--incremental` 只附加增量欄位
   （Tableau 的 increment-key）大於現有最大值的列，與 Tableau 的增量更新相同。寫入暫存檔後原子替換，
   查詢中的讀取不受影響，失敗時舊檔不變；萃取篩選照套，欄位型別與定序沿用現有檔案（.hyper 匯入時的）
3. `uv run tmig extract compare <.twbx> [--data-dir data]`：新萃取與 Tableau 的萃取（.twbx 內的 .hyper）逐表比對
   列數、每欄的非空值數，數值欄的總和、最小值、最大值，日期與文字欄的最小值、最大值（階段二）

計算比對（`tmig verify`）以 .twbx 內的快照為準；資料是從來源更新的時候，報告會註明，預期結果須在同一時間點
從 Tableau 抓取。不能更新的萃取：來源是檔案（Excel、CSV 等）、只取前 N 筆或抽樣、Relationship 模型中跨資料表的
萃取篩選；Tableau 的「彙總可見維度的資料」等設定無法從 .twb 得知，以資料比對檢查。

## 列層級權限

報表以使用者函式限制可見的資料時（`[Owner] = USERNAME()`、`ISMEMBEROF("Sales")` 的資料源篩選，或 Tableau 的
「建立使用者篩選」），新平台以登入者的身分產生 SQL（`kernel/src/tmig/planner/identity.py`）：

- 使用者函式（USERNAME、FULLNAME、USERDOMAIN、ISMEMBEROF、ISUSERNAME、ISFULLNAME、USERATTRIBUTE、
  USERATTRIBUTEINCLUDES）在 SQL 中代入登入者的常值；身分只從登入（查詢服務的身分驗證，P2）或 cases.yaml 的 `user` 取得，
  沒有身分時查詢失敗，不會當成看得到全部。ISMEMBEROF 與 Tableau 相同：名稱不分大小寫、`All Users` 一律成立、
  引數以網址編碼解讀（`USERS%2B` 代表 `USERS+`）；Active Directory 群組寫成 `網域\名稱`
- Tableau 的使用者篩選（`user:ui-builder='identity-set'` 的集合）轉成布林計算欄位：登入者看得到的成員為 TRUE
- 用到使用者函式的欄位（含間接參照）上的工作表篩選，不接受網址參數與篩選控制項的變更（Tableau 的網址參數可以改，
  是常見的漏洞）；報表本身為這個欄位放了篩選控制項（例如「只看我的客戶」開關）時才可以調整。
  篩選動作的選取與其他篩選一律以 AND 套用，只會縮小範圍；資料源篩選本來就不受影響
- 權限計算用到參數時與 Tableau 相同，使用者改參數就能改變可見範圍；`tmig census` 會在「列層級權限」列出提醒
- 篩選控制項的可選值也套用資料源篩選（看不到的成員不會出現在清單中）

驗證：預期結果是以抓取的帳號從 Tableau 匯出的，cases.yaml 的 `user` 要寫同一個帳號（使用者名稱、全名、網域、群組）。
`tools/tableau_server` 的 `fetch` 遇到這類報表時，自動以登入帳號的資料填好 `user`；`fetch_expected` 抓取前會確認瀏覽器登入的
帳號與 `user` 相同。`tmig sql`／`tmig query` 預設用 cases.yaml 的 `user`，也可以加 `--user 帳號 --group 群組` 試別的身分。

## 身分驗證

查詢服務以登入者的身分查詢（列層級權限），設定檔見 `service/auth.example.toml`
（`TMIG_AUTH=auth.toml` 或 `uv run tmig serve --auth auth.toml`；`service/src/tmig_service/auth.py`）：

| 模式 | 用途 |
| --- | --- |
| `none` | 不驗證（預設；本機檢視與示範）；用到使用者函式的報表無法查詢 |
| `dev` | 開發模式：檢視器先選使用者、頁首可切換；不可用於正式環境 |
| `proxy` | 公司的 SSO 閘道（反向代理）驗證後以標頭傳入帳號；只信任 `trusted_proxies` 的連線 |
| `oidc` | 查詢服務自己以 OpenID Connect 登入（授權碼＋PKCE），以簽章的 cookie 維持工作階段 |

- 使用者目錄：`uv run python tools/tableau_server/tableau_server.py directory --out directory.json` 從 Tableau Server
  匯出使用者與群組（需要站台管理員）。登入的帳號依名稱或 email 對應，`USERNAME()` 與 `ISMEMBEROF()` 以目錄為準
  （與 Tableau 相同的大小寫與群組）；不在目錄中的帳號拒絕登入。沒有目錄時用標頭或 ID Token 中的群組。
  proxy 模式下閘道已經驗證過身分、但帳號不在目錄中時，檢視器說明「這個帳號沒有使用這個網站的權限」並寫出帳號
  （`GET /api/me` 的 `denied`）；沒有帶帳號的請求才是「沒有登入者」
- 密鑰（`session_secret`、`client_secret`）只能寫成 `${環境變數}`；ID Token 只接受非對稱金鑰的簽章，
  驗證 issuer、audience、有效期限與 nonce；登入後只回到本站的相對路徑
- 設定之後報表的 API 都需要登入者；篩選器的可選值只提供報表上有篩選控制項的欄位（與 Tableau 相同，
  避免列舉其他欄位的值）。`GET /api/me` 回傳目前的登入者與模式
- `tools/compare_screens` 遇到有列層級權限的報表時，以開發模式、cases.yaml 的 `user` 身分截圖

## 權限

`uv run python tools/tableau_server/tableau_server.py permissions --out permissions.json` 從 Tableau Server 匯出 Project、
workbook、view 的權限（需要站台管理員），設定檔的 `[auth]` 加上 `permissions = "permissions.json"` 後，查詢服務依
Tableau 的權限評估規則開放（`kernel/src/tmig/access/permissions.py`、`service/src/tmig_service/access.py`）：

- 管理員看得到全部、未授權（Unlicensed）的帳號看不到；擁有者、所在 Project 與上層 Project 的擁有者與 Project Leader
  看得到；其餘依規則：本人的允許或拒絕優先，其次任一群組拒絕就拒絕、任一群組允許就允許，都沒有設定時拒絕
- Project 鎖定權限時用 Project 的 workbook 預設權限（上層鎖定也套用到巢狀的 Project）；workbook 不以分頁顯示工作表時，
  各 view 另有權限，dashboard 的權限涵蓋其中的工作表，Story 的權限涵蓋故事點顯示的 dashboard 與工作表
  （報表清單只列出有權限的 view 本身）
- 沒有檢視權限的報表不出現在清單中，報表的 API 一律回應 404；報表定義（IR）只給有「下載 workbook」權限的人完整版本，
  其他人拿到的版本不含連線、公式、資料源篩選與原始 XML；資料源分頁同樣要「下載 workbook」，其中的資料預覽
  另外要「檢視基礎資料」
- 報表依 cases.yaml 的 `tableau_views_url`（Tableau 上的網址名稱）或報表名稱對應到 Tableau 的 workbook；
  權限檔中找不到的報表只有管理員看得到

## 站台管理

查詢服務有報表庫（`tmig serve --store <報表庫>`）時，Project、權限、使用者與群組改在新平台上管理，Tableau 下線後
也能新增與修改（`service/src/tmig_service/site.py`、`admin.py`）：

- 第一次啟動時以 `[auth]` 的 `directory`、`permissions`（Tableau 匯出的檔案）建立報表庫中的 `site.json`，之後以站台管理
  修改、不再讀匯出的檔案。要重新從 Tableau 匯入：
  `uv run tmig site import --store <報表庫> --directory directory.json --permissions permissions.json --replace`；
  沒有 Tableau 時以 `uv run tmig site init --store <報表庫> --admin <帳號>` 建立只有「預設」Project 與一位站台管理員的站台，
  `uv run tmig site show --store <報表庫>` 顯示摘要
- 站台管理員在首頁的「站台管理」（`/admin`）：
  - 內容與權限：新增、改名、移動、刪除 Project（要先沒有報表與子 Project；新的 Project 沿用上層的權限）、Project 的擁有者、
    權限管理方式（由擁有者管理、鎖定）、Project 的權限與報表的預設權限；報表改名、移到其他 Project、換擁有者、設定權限、
    刪除（含每一版，移到報表庫的 `trash/`）；Tableau 匯入的 workbook 與各 view 的權限。權限可以套用範本
    （與 Tableau 相同的檢視、探索、發布、管理、拒絕）或逐項設定允許、拒絕
  - 使用者：新增（站台角色、網域、email）、修改站台角色與群組、刪除（擁有內容的人要先換擁有者；不能拿掉自己的管理員角色）。
    email 要像信箱（`名稱@網域`；不驗證信箱存不存在），不像的不接受：訂閱與警示的信要寄到這裡
  - 群組：新增、改名、成員、刪除；報表公式的 `ISMEMBEROF()` 以群組名稱判斷，改名後用到舊名稱的列層級權限要一併修改
  - 修改紀錄：每次修改的時間、修改的人、動作與對象（`site-changes.jsonl`，不記錄修改前後的值）
  - 畫面上的時間（修改紀錄、最後修改、版本、留言、資料源的發布與萃取更新）都以檢視者電腦的時區顯示；伺服器記的是 UTC
- 修改立即生效：下一個請求就依新的權限、站台角色與群組判斷；刪除的使用者不能再登入
- 與 Tableau 相同，報表的擁有者、所在 Project（與上層）的擁有者與 Project Leader，以及有「設定權限」的人可以設定報表的權限，
  有「刪除」的人可以刪除報表（`PATCH`、`DELETE /api/admin/reports/{id}`）；Project 鎖定權限時只能在 Project 上設定

## 排程與訂閱

`uv run python tools/tableau_server/tableau_server.py schedules --timezone Asia/Taipei --out schedules.json` 匯出排程、
萃取更新工作與訂閱（`kernel/src/tmig/schedule/`；需要站台管理員）：

- 排程：Tableau Server 的伺服器排程（每小時、每天、每週、每月；含間隔、星期、月底、每月第幾個星期幾），
  Tableau Cloud 寫在工作裡的排程也一併轉成排程；時間以 `--timezone`（Tableau Server 的時區）解讀，
  日光節約時間以當地時間為準。無法辨識的排程記下原因
- 萃取更新工作：目標是報表（嵌入的萃取）或 Published Data Source、完整或增量更新、連續失敗次數
- 訂閱：訂閱者、view 或整份報表、主旨、附圖片或 PDF、紙張方向與大小、暫停、view 沒有資料時是否寄送

排程器（`kernel/src/tmig/schedule/runner.py`）依排程檔執行萃取更新與訂閱：

```bash
uv run tmig schedule list schedules.json --fixtures fixtures    # 工作、下次執行時間、對應的報表與上次的結果
uv run tmig schedule serve schedules.json --fixtures fixtures   # 常駐執行
uv run tmig schedule tick schedules.json --fixtures fixtures    # 執行一次到期的工作（給作業系統的排程或 Celery beat 呼叫）
uv run tmig schedule run schedules.json --job extract:<id>      # 立即執行一個工作
```

- 萃取更新工作對應到報表：目標是報表時更新它的嵌入萃取；目標是 Published Data Source 時更新每份用到它的報表
  （N8 併入的萃取）。報表依 cases.yaml 的 `tableau_views_url` 或報表名稱對應；以連線設定檔連到來源資料庫（N7，原子替換）
- 對應不到報表、排程無法辨識或已暫停的工作不執行，`schedule list` 列出原因
- 下次執行時間記在狀態目錄的 `state.json`（預設為排程檔旁的 `schedule-state/`）；停機期間錯過的排程，啟動後補跑一次，
  再從現在起算下一次。一個工作失敗不影響其他工作；每次的結果（到期、開始、結束時間、成功或失敗與說明）逐行寫入
  `runs.jsonl`
- 同時執行（與 Tableau Server 的背景程式相同）：同時執行的工作數有上限（`--workers`；報表庫模式在「站台管理 →
  排程」設定，沒有設定時 2 個），排隊的工作依優先順序（數字小的先）開始；排程的「執行方式」為依序（Tableau 的
  Serial）時這個排程的工作一次執行一個。同一份報表的萃取更新不與讀取它的工作同時執行：同時到期時訂閱等萃取更新
  完成才產生（寄出的是新的資料），連到伺服器上的資料源的報表也等資料源的更新。常駐時執行中的工作不擋住檢查：
  長的萃取更新執行中，「立即執行」與其他到期的工作照常開始（不超過上限），站台管理顯示執行中與排隊的工作

訂閱（`service/src/tmig_service/subscriptions.py`）：排程器加上 `--mail mail.toml`（範本見 `service/mail.example.toml`）與
`--auth auth.toml`（使用者目錄與權限）後執行訂閱的工作：

- 以訂閱者的身分開啟檢視器（排程器在本機另外啟動一個只接受簽章工作階段的查詢服務），列層級權限與權限都照套；
  沒有權限、不在使用者目錄中或沒有 email 時這次訂閱失敗並記錄原因
- view 的訂閱為該 dashboard 或工作表，整份報表的訂閱為每個 dashboard（沒有時為每張工作表）；
  圖片內嵌在郵件內文、PDF（依紙張大小與方向）合併成一個附件，內文附每個 view 的連結
- 圖片與 PDF 只有 view 本身，不含檢視器的介面（工具列、每張工作表右上角的列數與「資料」切換、切入的連結）。
  PDF 用訂閱的紙張與方向（預設 A4 直向）、邊界 10 公釐，view 縮放到一頁之內（與檢視器「匯出＞PDF」的「符合一頁」
  相同的列印樣式；縮放的規則是推定的，見「已知限制與待驗證」），比紙寬的 dashboard 不會被裁掉；工作表的 view
  是整張（在區塊內捲動時看不到的列也在，縮到同一頁）
- 郵件中的報表名稱（預設的主旨、PDF 的檔名、警示與失敗通知的內文）是給人看的名稱：發布的報表是發布時填的名稱，
  移轉的報表是報表本身的名稱，不是目錄名稱或報表 id；每封信都有 Date 標頭（寄出的時間）
- view 沒有資料、而訂閱設定為不寄送時不寄（與 Tableau 的「view 為空時不傳送」相同）

### 排程管理

有報表庫時，排程、萃取更新工作與訂閱改在新平台上管理，Tableau 下線後也能新增與修改
（`service/src/tmig_service/schedules.py`、`schedule_api.py`）：

- 匯入：`uv run tmig schedule import schedules.json --store <報表庫>`（Tableau Server 匯出的排程檔；匯入的工作依報表的
  網址名稱或名稱對應到報表），之後在站台管理中修改；沒有 Tableau 的排程時在「站台管理 → 排程」設定時區後新增
- 排程器：`uv run tmig schedule serve --store <報表庫> --mail mail.toml`，每次檢查時重讀排程資料（修改不必重新啟動）、
  每 5 秒檢查一次「立即執行」；使用者與權限以站台資料為準，發布到伺服器的報表也能更新萃取與訂閱。
  狀態與執行紀錄在報表庫的 `schedule-state/`，排程器每次檢查時回報，站台管理據此顯示排程器是否在執行
- 站台管理員（「站台管理」的排程、工作、執行紀錄）：
  - 排程：新增、修改（名稱、萃取更新或訂閱、頻率：每小時／每天／每週／每月、優先順序、執行方式：平行或依序）、
    暫停、繼續、刪除（要先沒有工作）；同時執行的工作數上限（1–32，1 為一個接一個執行；排程器下一次檢查時就依新的上限）
  - 工作：萃取更新（完整或增量）與訂閱的上次與下次執行、連續失敗次數；新增萃取更新、立即執行、改排程、暫停、繼續、刪除
  - 執行紀錄：每次執行的時間、觸發（排程，或立即執行的人）、結果與說明
- 連續失敗（與 Tableau Server 相同）：萃取更新每次失敗寄信給報表的擁有者；萃取更新與訂閱連續失敗 5 次
  （`--suspend-after`）後暫停，訂閱暫停時寄信給訂閱者；暫停的工作可以立即執行確認問題排除，繼續時重新計算
- 訂閱（與 Tableau 的「訂閱」相同）：檢視器工具列的「訂閱」選擇這個 view 或整份報表、排程、圖片或 PDF（紙張大小、
  方向）、主旨、訊息與「view 沒有資料時不寄送」；訂閱者要能檢視這份報表並在站台中有 email，報表的擁有者、
  Project Leader 與站台管理員可以替別人訂閱。首頁的「我的訂閱」修改主旨、排程與格式，暫停、繼續或取消訂閱

### 資料警示

與 Tableau Server 的 data-driven alert 相同（有報表庫時；`service/src/tmig_service/alerts.py`、`alert_api.py`）：

- 建立：檢視器工具列的「警示」選擇 dashboard 中的工作表、量值（顯示目前的範圍）、條件（高於、高於或等於、低於、
  低於或等於、等於）、門檻與寄送頻率（資料更新時、最多每小時／每天／每週一次）；篩選與參數為建立時畫面上的。
  能檢視報表的人可以為自己建立，報表的擁有者、Project Leader 與站台管理員可以加入其他收件人（要能檢視報表、
  在站台中有 email）
- 檢查：排程器（`tmig schedule serve --store`）在報表的萃取更新成功後檢查用到它的警示，其餘每小時檢查一次
  （Live 連線）；每位收件人以自己的身分查詢（列層級權限照套），只有條件在自己看得到的資料中成立時才收到，
  郵件中只列出自己看得到的標記（附 view 的圖片）
- 連續 5 次無法檢查（查詢失敗）時暫停並寄信給建立的人；「我的訂閱」與站台管理的「工作」中可以立即檢查、暫停、
  繼續、刪除，收件人可以不再收到；檢查的結果記在執行紀錄

## 使用紀錄

查詢服務記錄每次檢視的使用者、報表與時間（`service/src/tmig_service/usage.py`），供報表管理與廢棄判定，不做完整稽核：

- 檢視器每開啟一個 dashboard、工作表或 Story 記一次（切換篩選、故事點不算新的檢視）；使用者取自登入者，
  沒有權限的檢視不記錄
- 逐行寫入使用紀錄目錄的 `<年-月>.jsonl`（`tmig serve --usage-dir`，或環境變數 `TMIG_USAGE_DIR`；
  預設為報表目錄旁的 `usage/`），只有時間、使用者、報表與 view，不記錄 IP
- `GET /api/usage?days=90`（有權限檔時限管理員）與 `uv run tmig usage summary usage --fixtures fixtures --csv usage.csv`：
  每份報表與 view 的檢視次數、不重複使用者數與最後一次檢視，期間內沒有檢視的報表也列出（廢棄候選）

## 指令

| 指令 | 用途 |
| --- | --- |
| `tmig census <twbx>... [--out-dir 目錄]` | 功能盤點與起步條件檢查（S3） |
| `tmig inventory <資料夾>... [--out-dir 目錄]` | 批次盤點：總表、功能使用統計與起步候選（S2） |
| `tmig parse <twbx> [--out IR 檔] [--lenient]` | 解析成 IR，列出不支援的元素（A2–A6） |
| `tmig import-hyper <twbx> [--out-dir 目錄]` | 萃取匯入 DuckDB 並自檢（C1） |
| `tmig sql <IR> <工作表> [--case 名稱] [--user 帳號 --group 群組]` | 產生的 SQL（D1–D4；身分見「列層級權限」） |
| `tmig query <IR> <工作表> [--case 名稱] [--user 帳號] [--out CSV]` | 執行查詢，欄名為欄位鍵；缺 DuckDB 檔時自動匯入（C2） |
| `tmig cases suggest <fixture 目錄> [--write]` | 依資料建議 3 組篩選組合（E1） |
| `tmig mapping suggest <fixture 目錄>` | 預期 CSV 欄名對照建議（E2） |
| `tmig verify <fixture 目錄>` | 計算比對，寫出固定格式的差異報告（E3） |
| `tmig connections template <報表或資料夾>... [--out 檔案]` | Live 連線的設定檔範本，憑證寫成環境變數（N4） |
| `tmig connections check <報表或資料夾>... [--config 檔案] [--no-connect]` | 檢查 Live 連線的設定並測試連線（N4） |
| `tmig extract refresh <報表> [--incremental] [--out-dir 目錄]` | 從來源資料庫更新萃取的 DuckDB 檔：完整或增量、原子替換（N7） |
| `tmig extract compare <.twbx> [--data-dir 目錄]` | 新萃取與 Tableau 的萃取比對列數、總和、最小值、最大值（N7） |
| `tmig published merge <報表> [--published 目錄] [--out .twbx]` | 把 Published Data Source 的 .tdsx 併入報表（N8） |
| `tmig schedule list\|tick\|run\|serve <排程檔> [--fixtures 目錄]` | 排程器：萃取更新與訂閱的工作、下次執行時間與執行紀錄（Q2、Q3：`--mail`、`--auth`） |
| `tmig schedule list\|tick\|run\|serve --store <報表庫> [--mail 設定檔]` | 排程器：報表庫中（站台管理的）排程；連續失敗暫停、寄信通知（見「排程管理」） |
| `tmig schedule import <排程檔> --store <報表庫> [--replace]` | 把 Tableau 匯出的排程檔匯入報表庫（見「排程管理」） |
| `tmig site import\|init\|show --store <報表庫>` | 站台資料：從 Tableau 匯出的檔案匯入、建立新的站台、顯示摘要（見「站台管理」） |
| `tmig usage summary <使用紀錄目錄> [--fixtures 目錄] [--days 90] [--csv 檔案]` | 每份報表的檢視次數、使用者數與最後一次檢視（R1） |
| `tmig serve [--fixtures 目錄] [--port 8000] [--auth 設定檔] [--usage-dir 目錄] [--store 目錄]` | 查詢服務與檢視器（F2–F4；身分驗證、使用紀錄、發布與版本見各節） |
| `tmig desktop serve [--workspace 目錄] [--port 0] [--clean] [--exit-with-parent]` | 桌面版的本機引擎：只接受本機、帶權杖的連線，由桌面版外殼啟動（D0，見「桌面版」） |
| `tmig ir validate <IR 檔>`、`tmig ir schema --out <檔案>` | 驗證 IR、匯出 JSON Schema（A1） |

## 專案結構

```text
tableau-migration/
├── kernel/src/tmig/        Python 核心函式庫
│   ├── ir/                 IR v0.3 的 Pydantic 模型、參照檢查、JSON Schema
│   ├── parser/             .twbx 解析器
│   ├── census/             功能盤點與起步條件檢查
│   ├── formula/            公式解析、語法樹、型別與層級判定
│   ├── planner/            篩選與參數狀態、登入者的身分與列層級權限
│   ├── access/             使用者目錄與權限（Tableau Server 的使用者、群組、Project／workbook／view 權限）
│   ├── schedule/           排程與訂閱的模型、下次執行時間、排程器
│   ├── catalog.py          報表目錄與 Tableau workbook、Published Data Source 的對應
│   ├── package.py          報表套件（.tmigx）：桌面版存檔的單一檔案
│   ├── sqlgen/             公式與工作表 → DuckDB SQL（依運算順序分段）、方言轉譯
│   ├── data/               Hyper 匯入、DuckDB 唯讀執行器、Live 連線（兩段式執行、連線設定）
│   ├── render/             Vega-Lite 規格與數字格式
│   ├── verify/             cases.yaml、欄位對照、正規化、比對、差異報告
│   └── cli.py              tmig 命令列
├── kernel/schema/          IR 的 JSON Schema（由程式產生）
├── service/                FastAPI 查詢服務（tmig_service：身分驗證、權限、訂閱、使用紀錄、報表庫與發布、桌面版）
├── web/                    React＋TypeScript 檢視器（建置結果在 web/dist）
├── desktop/                桌面版：Tauri 外殼、本機引擎的打包（PyInstaller）與冒煙測試
├── tools/                  連 Tableau Server 的工具：tableau_server（盤點、下載、匯出）、fetch_expected、compare_screens
├── fixtures/<報表>/         .twbx、golden IR、cases、mapping、預期結果、SQL 與規格快照
└── scripts/                一鍵檢查（check.py、check_commit.py）、重新產生 IR 的檔案（regen_ir.py）、示範報表的建置與獨立計算
```

核心函式庫不依賴服務、前端與瀏覽器自動化；外圍只透過核心的公開介面工作。

## 桌面版

桌面版（規格見 Claude Docs 的「桌面版編輯器規格」）是裝在作者電腦上的應用程式，功能目標是公司使用的
Tableau Desktop 2021.2。第一階段（D0）是骨架：開啟 Tableau 的 .twbx、.twb 或報表套件 .tmigx，以平台的引擎
檢視與查詢，另存成報表套件，登入伺服器、發布與從伺服器開啟。第二階段加入編輯器（見下方「工作表編輯器」）。

- **外殼**（`desktop/src-tauri`，Tauri 2）：啟動本機引擎、主視窗載入檢視器；檔案選單（開啟 Ctrl+O、儲存 Ctrl+S、
  另存新檔 Ctrl+Shift+S、關閉報表 Ctrl+W）與選檔、存檔對話框；伺服器選單（登入、從伺服器開啟、發布、發布資料源、
  登出）；只執行一份
- **本機引擎**（`tmig desktop serve`，見 `tmig_service.desktop`）：與伺服器同一份程式；只綁 127.0.0.1，
  每次啟動產生權杖，檢視器第一次帶 `?token=` 進來時換成 cookie，Host 標頭不是本機的一律拒絕；
  身分為作業系統的帳號（沒有群組）；開啟的檔案複製到工作區，原始檔不動
- **報表套件**（`.tmigx`，見 `tmig.package`）：zip，內含 manifest、IR、萃取的 DuckDB 檔與圖片；不含帳號密碼；
  開啟時檢查格式版本與成員路徑（不會解到工作區之外）
- **安裝檔**：CI 的 desktop 工作流程在 Windows 上以 PyInstaller 打包引擎、實跑冒煙測試（開啟 .twbx 由 Hyper 匯入、
  查詢每張工作表、存成報表套件），再以 Tauri 產生 NSIS 安裝檔（安裝在使用者資料夾，不需要系統管理員權限）

開發與打包的方式見 `desktop/README.md`。

### 編輯器的工作區排列

四種編輯畫面（工作表、dashboard、Story、資料源）共用同一個外框（`web/src/editor/Workspace.tsx`），排列與
Tableau Desktop 2021.2 相同，由上到下：

- **功能表列**：檔案、資料、工作表、dashboard、Story、分析、地圖、格式、伺服器、視窗；現有的命令放在 Tableau 中
  對應的功能表（例如「分析＞總計」「工作表＞清除」「格式＞複製格式」），沒有命令可放的功能表不顯示（dashboard 與
  Story 沒有「分析」「地圖」「格式」）。最右邊是使用者名稱與「完成」（回到檢視）。桌面版只有這一排功能表：外殼沒有
  原生選單（兩者並存時「檔案」「伺服器」會重複成兩排），「檔案＞結束」「說明＞關於」只有在外殼中才有；不在
  編輯器裡的頁面（報表清單、檢視）也有功能表列（檔案、伺服器、說明），檔案指令的快捷鍵（Ctrl+O、Ctrl+S、
  Ctrl+Shift+S、Ctrl+W）由頁面處理。刪除動作、連線、裝置版面與關閉還沒儲存的報表前的確認，
  在桌面版是原生的訊息方塊（確定／取消），在瀏覽器裡是瀏覽器的確認框；取消或方塊出不來時都不做。
  刪除工作表、dashboard、Story 不先問：刪除可以復原（見下面的分頁列）
- **工具列**：一列圖示按鈕（名稱在提示文字）：復原、重做｜儲存｜新增資料源｜新增工作表、複製、清除｜交換欄列、
  遞增排序、遞減排序｜顯示大小，最右邊是「智慧顯示」；不在 Tableau 工具列上的命令只在功能表。復原、重做
  （Ctrl+Z、Ctrl+Y）是這個分頁自己的變更，加上整份報表「刪除分頁」的紀錄（提示文字寫出下一步是什麼，例如
  「復原刪除工作表「工作表 3」」）；資料來源分頁的工具列也有，復原的是刪除的分頁。開著對話框、或焦點在可以
  輸入的地方（輸入框、下拉選單、標題與工具提示等格式文字的方塊）時，Ctrl+Z、Ctrl+Y 是那個方塊自己的復原，
  不動後面的工作表、dashboard、Story，也不放回刪除的分頁（工具列的按鈕照常）
- **主體**：
  - 工作表：左側的側欄有「資料」（資料源、搜尋框與選單、維度、量值、集合、參數）與「分析」（摘要、模型、自訂與
    分析的選項）兩個分頁；中間是頁面、篩選與**標記卡**（標記類型，三個一列的屬性按鈕：色彩、大小、標籤／詳細資料、
    工具提示，以及依標記類型出現的形狀、路徑、角度；把欄位拖曳到按鈕上，卡上的膠囊前面有所在屬性的圖示）；
    右邊是欄、列架與檢視
  - dashboard：左側的側欄有「dashboard」（裝置版面、大小、工作表、物件、並排／浮動、篩選與參數等控制項）與
    「版面配置」（選取項目的屬性、項目階層、格線）兩個分頁，其餘是畫面（沒有右邊的屬性窗格）
- **分頁列**：最左邊是「資料來源」，接著每張工作表、dashboard、Story 一個分頁（工作表在前，其次 dashboard、Story；
  IR 沒有記錄三者之間的順序），最後是新增工作表、dashboard、Story 的按鈕。按一下切換，連按兩下改名
  （還不是目前的分頁時先切過去再改名），按右鍵是這個分頁的命令（重新命名、複製、複製成交叉資料表、
  隱藏工作表、分頁顏色、刪除）。分頁很多、超出寬度時，目前的分頁捲進看得到的範圍（開啟時、切換與
  新增分頁之後），按看得到的分頁時分頁列不移動
- **刪除分頁與復原**（`web/src/editor/structure.ts`、`kernel` 的 `tmig.sheet_undo`）：刪除工作表、dashboard、Story
  （分頁的右鍵選單、Story 功能表的「刪除 Story」）不先問，刪除目前的分頁之後留在編輯器，開啟左邊的分頁（它是
  第一個時開啟右邊的）。刪除可以復原：右下角的訊息「已刪除工作表「…」」上有「復原」，工具列的復原與 Ctrl+Z 也可以。
  - 復原之後它原樣回來：同一個 id、名稱、定義、分頁列上原本的位置，刪除時連帶移除的東西也放回去（其他工作表
    工具提示裡的它；dashboard 的動作、目標工作表上指向動作的篩選、故事點記下的選取），編輯器開啟它。剛刪除就復原時
    整份報表的定義與刪除之前完全相同
  - 重做（工具列、Ctrl+Y）是再刪一次；復原之後只要再改過報表就不能重做
  - 先後順序：Ctrl+Z 先復原目前這個分頁自己的變更，沒有了才是最近一次刪除；刪除別的分頁時人還留在目前的分頁，
    那次刪除是最近的一步，所以先復原它。編輯的歷程仍然是每個分頁各自的（換分頁就重來），刪除的紀錄是整份報表的，
    換分頁還在；關閉報表或重新載入頁面之後就沒有了
  - 刪除之後又改過報表才復原：放回原本的位置（分頁變少時放在最後）；放回去會讓報表不正確時（例如把別的工作表
    改成了同一個名稱）說明原因、報表不變，排除之後可以再復原。還可以復原的期間，新增的東西不占用它的名稱與 id
    （「工作表 2」刪除後再新增的是「工作表 3」；別的工作表上新的篩選、複製出來的工作表也不會拿到它裡面的 id）
- **引擎連帶改到正在編輯的工作表時**（`web/src/editor/rebase.ts`）：引擎的一個操作會連帶改到別的工作表——
  刪除工作表時其他工作表的工具提示不再有它、刪除 dashboard 時它的動作在目標工作表上的篩選一併移除（復原刪除時
  放回去）、計算欄位的層級改變時用到它的膠囊調整彙總（彙總的公式改成列層級之後加上 SUM）、取代參照、欄位的
  預設屬性、把篩選套用到其他工作表。編輯器每次變更都把整張工作表存回，所以要知道引擎改過：
  - 工作表的讀取與存回帶著版本（`revision`，內容的指紋）。存回時帶上手上那一份的版本（`base`）；引擎上的定義
    已經不是那一版就不覆蓋（409，回應引擎上現在的定義），編輯器把變更重新套在它上面再存
  - 編輯器的其他請求改動了報表之後（刪除、復原或重做刪除分頁、計算欄位、群組、參數、匯入…），開著的工作表
    編輯器立刻確認手上的定義；引擎改過時畫面跟著變（例如膠囊變成 SUM(…)）
  - 歷程（復原、重做）的每一步也套上引擎的那些改變：編輯器沒有改過的地方換成引擎的，自己改過的地方留著；
    有 id 的項目（篩選、Table Calculation）依 id、工具提示中的工作表依它顯示的工作表對應；引擎換掉的欄位用法
    （膠囊）不論在哪個架上都跟著換。所以引擎改過之後，這張工作表自己的復原與重做照常可用，不會把引擎的改變
    蓋掉，也不會因為參照到已經刪除的工作表或動作而存不進去
  - dashboard 與 Story 的編輯器沒有這一層：沒有找到會在它們開著時連帶改到它們的操作
- **狀態列**：工作表是摘要資料的列數（標記數、列數 × 欄數還沒有），dashboard 是大小與縮放，Story 是故事點數與大小

圖示是自己畫的單色線條圖示（`web/src/editor/Icons.tsx`）。

### 工作表編輯器

桌面版的工作表上按「編輯工作表」進入編輯器（網址 `/w/<報表>/edit/<工作表>`），版面與操作與 Tableau Desktop 的工作表相同：

- **資料窗格**：資料源的維度、量值、集合、計算欄位（=）與參數，Measure Names／Values；可搜尋、顯示隱藏的欄位；
  拖曳到架上，連按兩下加入檢視（維度到列、量值到欄或列）；膠囊拖回資料窗格即移除。可以換掉工作表的資料源
  （欄位依內部名稱對應）
- **欄位選單**（欄位上按右鍵或 ▾，`tmig.field_edit`）：重新命名、複製（「<名稱> (copy)」，一般欄位成為公式為
  [欄位] 的計算欄位）、隱藏與取消隱藏、轉換為維度或量值、別名（維度成員的顯示名稱）、預設屬性（彙總、數字或日期的
  格式、註解，註解顯示在資料窗格的提示中）、地理角色、建立階層；「建立」計算欄位、**群組**（勾選成員組成一組、改名、
  取消群組、包含「其他」）、**集合**（一般：勾選成員或排除；條件：彙總的比較或公式；頂部：Top／Bottom N 依彙總或
  公式）、**數值分組**（大小為數字或參數，與 Tableau 相同建議大小：分組數 = 3 + log2(n) × log(n)）與參數；群組、
  數值分組與集合可以再編輯，計算欄位、群組、數值分組與集合可以刪除（工作表、其他欄位的公式、篩選、參數或動作用到時
  退回並說明用到的地方）。從 Tableau 開啟的報表中，群組（categorical-bin）與數值分組（bin）的定義與欄位的註解
  （`<desc>`）一併移轉，可以在編輯器中修改
- **臨時計算**（Tableau 直接在架上輸入的公式）：連按兩下欄、列架或標記卡「詳細資料」的空白處輸入公式，
  按 Enter 成為膠囊；膠囊選單的「在架上編輯…」以膠囊的公式（例如 `SUM([Sales])`）開始修改。公式只是一個欄位或
  欄位的彙總時直接用這個欄位，其他的建立臨時計算（`DataField.ad_hoc`：名稱就是公式、資料窗格不列出，公式相同的
  只建立一個）；「命名並加到資料窗格…」把它變成一般的計算欄位。沒有任何工作表用到的臨時計算在修改欄位前清掉，
  不會擋住欄位的刪除或變更
- **預設屬性的色彩、形狀、排序與總計使用**（`tmig.field_defaults`）：維度各成員的顏色與色盤、量值的連續色盤，
  各成員的形狀，字母順序或手動的排序，總計的計算方式（自動、加總、平均、最小值、最大值）。與 Tableau 相同，
  工作表沒有另外設定時使用：查詢與繪圖只看工作表，所以工作表存回時補上缺的設定，預設改變時把仍沿用舊預設的
  工作表換成新的。從 Tableau 開啟的報表，資料源中維度本身的色彩與形狀（`<style>` 的 `[none:欄位:nk]`）記為
  欄位的預設，編輯器新增的工作表與原有的工作表同色
- **變更資料類型**（欄位選單）：檔案的欄位以新的型別重新匯入；萃取與資料庫的欄位在來源中仍是原本的型別，與解析
  .twb 的 `datatype-customized` 相同，以隱藏的原始欄位加上轉換它的計算欄位表示（`STR`、`INT`、`FLOAT`、`DATE`、
  `DATETIME`，欄位的 id 不變），改回原本的型別時還原。工作表用到的欄位要先移除
- **分割、自訂分割**（「轉換＞分割…」）：字串依分隔符號分成「<名稱> - Split N」的計算欄位
  （`TRIM( SPLIT( [欄位], "分隔符號", N ) )`），可以取前面或最後的幾段；「自動分割」依資料中最常見的分隔符號
  分成所有的段（最多 10 個）
- **取代參照**：工作表、資料源篩選、動作、階層、參數與計算欄位的公式中用到這個欄位的地方改用另一個欄位，
  欄位本身保留；**描述**：角色、型別、來源欄位、公式、註解與按下「載入」後的值域
- **合併欄位**（「建立＞合併欄位…」）：勾選兩個以上的維度（依勾選的順序），值為各欄位的值以「, 」連接，名稱為
  「A & B (Combined)」（`DataField.combined` 記下組成的欄位；資料窗格以 🔗 標示）。從 Tableau 開啟的合併欄位相同
- **資料夾**（欄位選單的「資料夾＞建立資料夾…／加入資料夾／從資料夾移除」、資料窗格選單（搜尋框旁的 ▾）的「新資料夾…」，把欄位拖到
  資料夾的名稱上即移進去）：資料夾依名稱排序，可以改名、刪除（欄位回到最上層）；2020.2 以前分在維度、量值之下的
  `<folder role>` 與 2020.2 起的 `<folders-common>` 一併移轉（`Datasource.folders`）
- **日期屬性**（資料窗格選單或「資料」功能表的「日期屬性…」）：週的起始日與**會計年度**的起始月；日期欄位另有「預設屬性＞會計年度起始…」
  （沿用資料源或指定月份）
- **智慧顯示**（工具列的「智慧顯示」，Tableau 的 Show Me，`web/src/editor/showMeCharts.ts`）：依檢視中的維度、日期、
  量值與地理欄位列出 24 種圖表（文字表、熱度圖、醒目提示表、符號地圖、填色地圖、圓餅圖、水平／堆疊／並排長條、
  矩形式樹狀結構圖、圓形檢視、並排圓形、連續與離散的線及區域圖、雙線、雙重組合、散佈圖、直方圖、盒鬚圖、甘特圖、
  子彈圖、填滿的泡泡圖），不能用的變淡並在提示中列出需要的欄位，建議的圖表加上外框；點選後重新排列欄列架與標記卡
  （篩選、頁面與排序不變，可以復原）。直方圖自動建立數值分組（建議的大小），盒鬚圖與子彈圖加上對應的參考線與分布
- **選取標記**：在檢視上點選標記（Ctrl 加選），或按住左鍵拖曳出方框框選（方框碰到的標記都選取，按住 Ctrl／⌘／Shift
  加進原本的選取；地圖上拖曳是平移，用地圖工具列的選取工具）。選取列有「只保留」「排除」（一個維度時是這個維度的
  篩選，多個維度時是依值組合的「包含／排除」篩選）、「群組」（以選取的成員建立群組）與「建立集合」（多個維度時
  成員是值組合）。按 Esc 或點空白處取消選取。按在註解上拖曳是移動註解，不是框選
- **欄、列、篩選、標記卡**：拖放加入、移動（按住 Ctrl 為複製）、排列順序；標記卡的標記類型、色彩、大小、標籤、
  詳細資料、工具提示、形狀、角度、路徑；多個量值有各自的標記卡時有「全部」與各量值的分頁；Measure Values 卡。
  把 Measure Values 從資料窗格拖進檢視、檢視中還沒有 Measure Names 時，一併加上 Measure Names（各量值才不會
  疊在同一格）：放在標記卡上（文字表）時，列架上有標題而欄架上沒有就加到欄架，其餘加到列架；放在列（欄）架上
  當座標軸時加到另一架
- **工具提示中的工作表**（標記卡的「工具提示…」，Tableau 工具提示編輯器的「插入＞工作表」）：加入、移除工作表，
  設定最大寬、高與篩選（所有欄位或檢視上選定的維度）；刪除工作表時一併從其他工作表的工具提示移除
  （復原刪除時放回去）
- **工具提示的文字**（同一個對話框）：以格式文字編輯器輸入（粗體、斜體、底線、字級、顏色、對齊，「插入」檢視中的
  欄位與參數的值）；留空白時是預設的欄位清單。「顯示工具提示」關閉時滑過標記不顯示工具提示
- **標籤**（標記卡的「標籤…」）：顯示標記標籤（自動、顯示、不顯示）、標籤的文字（格式文字，留空白為標籤架上的
  欄位）、要標示的標記（全部、最小值／最大值及其範圍為窗格、儲存格、整個表格或每條線、線條的兩端、選取的、
  醒目提示的）、允許標籤與其他標記重疊
- **格式文字編輯器**（`web/src/editor/RichText.tsx`）：工作表的標題、工具提示、標籤、註解與 dashboard 的文字物件
  共用；.twb 的格式文字（`<formatted-text>` 的 `<run>`）原樣開啟、修改
- **膠囊選單**：篩選、排序（字母或依第一個量值，遞增、遞減、清除；「排序…」對話框另有資料來源順序、依任一欄位與
  彙總、手動排列成員；依欄位排序時可以勾選「巢狀」，在外層維度的每個值之內各自排序）、日期的部分（離散）與截斷
  （連續）與精確日期、
  彙總方式、維度與量值互換、離散與連續、快速 Table Calculation（累計、差異、差異百分比、合計百分比、排名、百分位數、
  移動平均；檢視中有年時另有年初至今總計、年成長率，以及複合成長率）與清除、「編輯表計算…」（計算類型與選項：相對於
  上一個／下一個／第一個／最後一個、複合成長率、排名方式與順序、累計與移動計算的彙總與範圍；計算方向：表格、窗格、
  儲存格各方向與指定維度、計算層級、重新開始間隔）、數字格式（數字、貨幣、百分比、
  小數位數、K／M／B 單位、千分位）、顯示標題、編輯座標軸（標題、固定範圍、包含 0、反轉、同步、線性／對數／
  對稱對數刻度、主要刻度的自動／固定間距與起點／不顯示）、雙軸（與 Tableau
  相同自動把 Measure Names 放到色彩）、小計
- **篩選對話框**：一般（勾選值、全部／無、排除、搜尋）、萬用字元（字串欄位：包含、開頭為、結尾為、完全符合，可以
  排除；公式與 Tableau 寫進 .twb 的相同，不分大小寫）、條件（依欄位或公式）、頂部（Top／Bottom N 依量值）；
  篩選膠囊的「套用到工作表…」把篩選套用到選取的工作表或使用此資料源的所有工作表（各工作表上是同一個篩選，
  改了內容一起改，檢視器中一起變更；改回僅此工作表時從其他工作表移除）；
  量值與連續欄位的範圍（預設為工作表詳細程度上各標記的範圍）、包含 Null；日期先選篩選方式（相對日期、日期範圍、
  年、季、月、星期、日、年月、個別日期），相對日期有上一期、本期、下一期、本期至今、最近 N 期、未來 N 期與基準日；
  篩選可加入至內容
- **計算欄位**：輸入時即時檢查公式，顯示型別、計算層級（列層級、彙總、LOD、Table Calculation）與角色；可修改、刪除
  （工作表還在用時不能刪）；改變層級時與 Tableau 相同調整使用它的膠囊（彙總的公式不再加彙總、Table Calculation
  的公式加上計算方向）
- **參數**：建立、修改（名稱、資料類型、目前值、允許的值為全部、清單或範圍；動態參數的「開啟報表時的值」與允許值
  「開啟報表時取自欄位」，選了欄位即以目前的資料填入）、刪除（使用中不能刪）
- **階層**：資料窗格中以維度建立階層（名稱、由上到下的欄位、上移下移），把維度拖到階層名稱上加入最下層，
  從階層移除欄位、刪除階層；一個欄位只能在一個階層中，只能放維度（刪除計算欄位或改成彙總時自動移出）。
  欄、列架上的階層膠囊與日期膠囊有 ⊞／⊟：向下切入在後面加入下一層、收合移除後面的下層，日期值（截斷）
  改成下一層的截斷（與 Tableau 相同）
- **頁面架**：維度拖到「頁面」後檢視只顯示目前的頁，頁面控制項可以上一頁、下一頁、選擇與自動播放；目前的頁與
  報表一起存檔（與 Tableau 相同，換了欄位從第一頁開始）。檢視器的工作表與 dashboard 的頁面控制項（.twb 的
  `currpage` 區塊）也能切換頁；頁面的歷程記錄（顯示歷程）不支援，只顯示目前的頁
- **分析**：參考線（平均、中位數、總和、總計、最小、最大、常數）、參考區間、分布（百分位數、標準差、分位數）、
  盒鬚圖、趨勢線（線性、對數、指數、冪、多項式，信賴區間、通過原點）、預測（預測選項：自動、正好或直到 N 個
  年／季／月／週／日／小時，忽略最後幾期、以 0 填補缺少的值，自動、不含季節性或自訂的趨勢與季節模型，90／95／99%
  的預測區間；資料窗格加上 Forecast Indicator，量值膠囊的「預測結果」：實際值與預測值、趨勢、精度、精度 %、品質、
  上層／較低預測區間、無）、叢集（變數預設為檢視中的量值，叢集數自動或指定，叢集欄位放到
  色彩上，以 Cluster N 顯示）、彙總量值（關閉時量值不彙總，
  每一列資料是一個標記，完全相同的列也各是一個標記；只有維度時相同的組合只畫一個；與總計一起使用尚不支援）
- **註解**（分析的「註解…」或選取標記後的「💬 註解…」）：標記的註解跟著選取的標記，點與區域的註解以座標軸上的值
  定位；座標在離散的座標軸（標題）上時是那個成員所在的格子——點在格子的中間，區域從第一格的起點到最後一格的
  終點、文字在這幾格的中間（多層標題的外層值占它底下相鄰的幾格；成員不在目前的資料中、或內層的值在好幾組裡
  都有時不畫，附註說明；欄標題另外排序時依畫面上的順序）。文字是格式文字，可以移除。排序過的離散座標軸上加上
  標記的註解，順序不變（註解的圖層帶著與主圖相同的排序）
- **混合關係**（資料窗格的「混合關係…」，檢視用到次要資料源時出現；Tableau 的「資料＞編輯混合關係」）：每個次要
  資料源的連結欄位（主要資料源的欄位 ↔ 次要資料源的欄位）可以新增、修改、移除
- **工具列**：復原／重做（Ctrl+Z／Ctrl+Y）、交換欄與列、顯示大小、顯示欄總計與列總計、「總計…」（欄總計移至
  頂端、列總計移至左側、色彩包含總計、各量值的「總計使用」：自動、總和、平均值、最小值、最大值）、標題、格式
  （背景、文字顏色、字型與字級、列與欄的格線、座標軸線、刻度、欄位標籤、零線、分隔線、色帶）、「複製格式」
  「貼上格式」（把一張工作表的格式設定套到另一張，欄位、篩選與資料不變）、工作表名稱、
  新增、複製、刪除工作表（dashboard 或 Story 用到的工作表不能刪）
- **百分比**（側欄「分析」分頁或「分析」功能表的「百分比」，Tableau 的「分析＞百分比」）：表、欄、列、窗格、窗格中的列、窗格中的欄、儲存格
- **下拉線**（側欄「分析」分頁或「分析」功能表的「下拉線」）：無、選取標記時、一律顯示
- **篩選的範圍分頁**（量值與連續欄位的篩選對話框）：值範圍、至少（只有下限）、至多（只有上限）與特殊值
  （Null 值、非 Null 值、所有值；`RangeFilter.only_null` 為只保留 Null）。新的篩選的最小值、最大值預設是資料的
  範圍（另一個查詢，對話框開啟之後才到）：只填進使用者還沒動過的方塊——已經輸入過、或正在裡面（點進去、全選了
  準備輸入）的方塊不填，打的字不會被蓋掉或接在預設值後面
- **篩選控制項的值與自訂**（篩選膠囊選單、dashboard 的區塊屬性，`ControlOptions`）：只顯示相關的值、內容中的
  所有值（可選值只套用內容篩選），顯示「全部」值、搜尋（成員超過 10 個時）、包含／排除的切換（排除時勾選的是要
  排除的值）與套用按鈕（勾選之後按「套用」才篩選）
- **顯示遺漏的值**（欄列架上日期與數值分組的膠囊選單，`Worksheet.show_missing`，`tmig.render.missing`）：在
  整個檢視的最小值到最大值之間補上沒有資料的成員（日期的部分與截斷依單位、數值分組依分組的大小），其他維度的
  每一種組合各補一列，放在依排序應該在的位置；標題與座標軸上看得到，標記是空的（折線在這裡斷開）。
  **包含在工具提示中**（膠囊選單，`Worksheet.tooltip_excluded`）：取消之後自動產生的工具提示不列出這個欄位
- **特殊值與表格版面**（「格式…」）：Null 值的文字（文字表與標籤上量值是 Null 時顯示的文字，
  `Worksheet.null_text`）、Null 值的標記（隱藏並斷開、隱藏並連接前後的點、顯示在 0，`null_marks`，Vega-Lite 的
  `invalid`）；顯示空的列／欄（`show_empty`）——這一架上離散維度在內容與維度篩選之後資料中有的成員組合另外
  查詢，檢視中沒有標記的（例如被量值篩選濾掉）也顯示標題，儲存格是空的
- **檢視資料**（「分析」功能表的「檢視資料…」）：與檢視器相同的視窗，摘要（每個標記一列）與完整資料（基礎資料列），
  可以下載 CSV
- **工具列的排序**（「↑ 排序」「↓ 排序」「清除排序」）：最內層的離散維度依另一架上的量值排序（同一架上前面還有
  維度時是巢狀排序），沒有量值時依名稱；「排序…」的手動清單可以拖曳成員改變順序
- **在檢視上拖曳標題或圖例排序**（`web/src/headerDrag.ts`，Tableau 的手動排序：把檢視或圖例上的標題拖到另一個
  位置）：按住離散的列標題、欄標題（多層標題的每一層；多層的欄標題合成一個標籤時是最內層）或類別圖例（色彩、
  形狀）的項目拖曳，放下的位置以一條粗黑線標示。放開後這個欄位的排序改成手動（與「排序…」的手動清單相同）：
  順序是畫面上的順序、拖曳的成員移到放下的位置，原本的排序（字母、依欄位、巢狀）被取代；多層標題的內層只在
  同一組裡移動，但手動的順序是整個欄位共用的，其他組跟著改。只是點一下不改排序；與其他編輯相同可以復原。
  捲動時固定在區塊邊緣的標題也拖得動。分格（facet）的標題與 Measure Names 不能拖曳
- **隱藏工作表與分頁顏色**（分頁右鍵選單的「隱藏工作表」「分頁顏色…」，`Worksheet.hidden`、`tab_color`）：隱藏的工作表只在
  dashboard 與 Story 中使用，檢視器的清單不列出（桌面版照樣列出、標示「（隱藏）」）；從 Tableau 開啟的報表依
  工作表視窗的 `hidden='true'`。**複製成交叉資料表**：新的工作表是這個檢視的文字表——欄列架上的維度留在原來的
  架上，標記卡上的維度加到列（詳細程度不變），量值只有一個時直接顯示、多個時以 Measure Names／Values 並列
- **堆疊標記**（側欄「分析」分頁或「分析」功能表的「堆疊標記」，`Worksheet.stack`）：自動（長條與區域依標記卡上的維度堆疊）、開啟（折線、
  圓點等也堆疊）、關閉（長條與區域從 0 畫起、互相重疊；標籤回到長條的尾端）
- **色彩的效果**（「色彩…」）：光暈（圓點、形狀與方塊外圍的白色細邊，`Marks.halo`）與折線上的標記
  （自動、全部、無，`Marks.markers`）；**色彩上的多個欄位**：按住 Shift 把維度放到色彩上，或在詳細資料的膠囊選單
  勾選「也用於色彩（組合著色）」，以各欄位值的組合著色（`Marks.color_more`）
- **標籤的文字方向與換行**（「標籤…」，`MarkLabels.direction`、`wrap`）：橫寫、由下往上、由上往下——直寫的標籤
  仍在原來的位置（標記的上、下、左、右），Vega 的對齊與位移相對於文字自己的方向，依旋轉換算；換行以 120 像素
  為寬度。長條的標籤依對齊放在起點（靠左／下）、中間（置中）或尾端（預設）
- **座標軸的次要刻度與各窗格獨立的範圍**（「編輯座標軸…」，`Axis.minor_spacing`、`independent`）：主要刻度是
  固定間距時可以加上間距更小、較短且沒有文字的次要刻度（格線只畫在主要刻度上）；欄列架上的離散維度把圖分成
  窗格時，列架上的量值在每一列的窗格各自決定範圍，欄架上的量值在每一欄
- **自訂的數字格式字串**（「格式…」與「預設屬性＞數字格式…」的「自訂（格式字串）」）：與 Tableau、Excel 的寫法相同
  （`#,##0.00`、`0.0%`、`"NT$"#,##0`），以分號分開正數、負數與零（`#,##0;(#,##0);"-"`、`▲0.0%;▼0.0%`）
- **次要計算**（「編輯表計算…」，主要計算是累計或移動計算時）：差異、差異百分比、百分比、合計百分比、排名、
  百分位數；可以有自己的計算方向（不勾選為與主要計算相同）
- **格式**（「格式」功能表的「格式…」）另有儲存格大小（列高、欄寬）、儲存格的水平對齊與標題的垂直對齊；
  「套用到所有工作表」把這張工作表的格式套到報表中其他的工作表（整份報表一致，Tableau 的「格式＞活頁簿」）
- **標籤的對齊**（「標籤…」）：水平（靠左、置中、靠右）與垂直（上、中、下），決定標籤在標記的哪一邊
- **說明**（「工作表」功能表的「說明…」，Tableau 的「工作表＞顯示說明」）：顯示在檢視的下方；文字以格式文字編輯器輸入
  （可以插入參數的值），留空白時是自動產生的說明
- **合併集合**（集合的選單「建立合併集合…」，或建立集合對話框的「合併」分頁）：同一個維度上兩個集合的所有成員、
  共同的成員、只在左側或只在右側的成員
- **線條類型**（標記類型為線時，標記卡上的「線條類型」）：直線、階梯、跳躍
- **個別成員的顏色**（「色彩…」）：離散的色彩可以為各成員指定顏色（其餘依色盤），「重設」改回色盤的顏色
- **形狀**（標記類型是形狀或形狀上有欄位時標記卡的「形狀…」，Tableau 的「編輯形狀」）：為各成員指定形狀（圓形、
  方形、加號、叉號、星號、菱形與四個方向的三角形），「自動」依預設調色盤的順序；形狀上沒有欄位時是所有標記的形狀。
  **自訂的圖片形狀**：選單中另有報表裡的形狀調色盤（Tableau 檔內嵌的，與在這裡加入的），「加入圖片…」把自己的圖片
  （PNG、JPEG、GIF、BMP、WebP、SVG，每張 4 MB 以內）加入一個調色盤（Tableau 的 `My Tableau Repository\Shapes\<調色盤>`），
  「指定調色盤」依成員的順序一次指定整個調色盤；資料窗格維度的「預設屬性＞形狀…」也可以用。圖片存在報表套件的
  `files/Shapes/<調色盤>/`，發布時一起上傳（見「自訂的圖片形狀」）
- **顯示篩選、顯示參數**：篩選膠囊的「顯示篩選」與資料窗格參數旁的 ○／◉ 把控制項放到工作表上（單獨檢視
  工作表時顯示在檢視的右側）；類別篩選的控制項可以設為「只顯示相關的值」
- **年初至今成長率**（快速表計算）：每年重新累計（年初至今總計），次要計算在年的層級與前一年同期比較
  （`SecondaryTableCalc.level`、`restart`：次要計算自己的計算層級與重新開始間隔）
- **表計算的自訂排序順序**（「編輯表計算…」指定維度時的「排序順序」，`TableCalc.sort`）：沿著計算方向的標記依
  一個彙總值（可以不在檢視中）由小到大或由大到小排列，同值時依維度在檢視中的順序
- **工具提示的選項**（「工具提示…」）：回應式或暫留時（`Worksheet.tooltip_mode`，.twb 的 `tooltip-mode`
  smooth／sticky）、包含命令按鈕（`tooltip_commands`，.twb 的 `<customized-tooltip show-buttons>`）、允許依類別選取
  （`tooltip_select`）。檢視器中點選標記後工具提示釘住：「只保留」「排除」以標記的維度值篩選這張工作表
  （`QueryState.marks`，依序套用；工作表上方的「已只保留／排除 ✕」清除），點工具提示中的維度值選取這個值的
  所有標記；按 Esc（同時取消選取）或點別處放開。各標記卡的自訂文字不同時，滑過的標記用它所在標記卡的文字。
  釘住的工具提示與滑過這個標記時的內容相同（欄位的名稱、格式化的值、自訂的文字與格式），由點選的標記直接
  算出——觸控的裝置上點標記之前沒有滑過，內容也一樣；標記沒有工具提示時只有命令按鈕
- **描述趨勢模型、描述預測、描述叢集**（側欄的「分析」分頁）：趨勢線的公式、觀測數、自由度、R 平方、標準誤與 P 值，以及
  各係數的值、標準誤、t 值與 P 值（Tableau 的 Individual trend lines；`spec.usermeta.trends` 的
  `coefficients`，X 的次方由高到低、截距在最後）；預測的選項與各序列的模型（趨勢、季節、平滑係數、品質與
  品質指標 RMSE、MAE、MASE、MAPE、AIC）與摘要（初始：第一個估計的期的估計值 ± 精度、相對於初始值的變化、
  季節影響的高值與低值、趨勢與季節的貢獻）、叢集的摘要
  （叢集數、點數、平方和、各叢集的中心與變異數分析；render 回應的 `describe`）。描述叢集的「存成群組」以目前的
  叢集建立群組欄位（檢視的詳細程度只有一個文字維度時）
- **拖曳註解**：標記與點的註解，文字可以直接在檢視上拖曳（改變文字相對於位置的偏移）
- **下拉線的值標籤**（側欄「分析」分頁的「下拉線的值標籤」，`DropLines.labels`）：下拉線碰到座標軸的地方寫上標記的值
- **摘要卡**（「工作表」功能表的「顯示摘要」，`Worksheet.summary_shown`，.twb 視窗的 `<card type='summary'>`）：檢視下方列出
  各量值的總和、平均、最小值、最大值與中位數；「更多」另外顯示選用的統計量（樣本標準差、第一與第三四分位數、
  偏度、超額峰度，Tableau 在卡片的選單中勾選；`web/src/summaryStats.ts`）；選取標記時只算選取的標記
- **儲存格、窗格、表頭、合計與總計的框線**（「格式…」的「框線」，`Worksheet.cell_border`、`pane_border`、
  `header_border`、`total_borders`，Tableau 的「格式＞框線」）：樣式（實線、虛線、點線）、顏色與線寬。儲存格的框線
  畫在每一列、每一欄之間；窗格的框線畫在各窗格之間（最內層以外的標題成員改變處，只有一層標題時整張表是一個窗格）；
  檢視的外框是窗格（沒有時為儲存格）的框線。表頭的框線畫在標題區域中各標題成員之間（外層的成員跨過的列不切開）；
  列、欄的「合計」「總計」的框線畫在合計（小計）、總計的列（欄）兩側，窗格與標題區域都畫。「複製格式」「清除格式」
  也包含框線
- **「N 個 Null」指示器**（`Worksheet.null_indicator`，Tableau 檢視右下角的指示器）：連續座標軸上的值是 Null 而
  沒有畫出的標記數（`spec.usermeta.nulls`）顯示在檢視的右下角。編輯器中點一下（或按右鍵）的選單與 Tableau 相同：
  「篩選資料」（座標軸的欄位中查詢結果有 Null 的，各加一個只留非 Null 值的篩選——篩選對話框「特殊值」的
  「非 Null 值」；量值是量值篩選，連續的日期篩選日期本身；篩選架上已有同一個欄位的範圍篩選時改成不含 Null。
  Null 的列從之後的計算中排除，可以復原）、「在預設位置顯示資料」（Null 值的標記改成顯示在 0）、「隱藏指示器」；
  「格式…」的特殊值也可以開關
- **畫圖的附註**（查詢服務 `/render` 回應的 `notes`、`warnings`，`tmig.render.notes`）：檢視的左下角有一個標示，
  點了列出各則（清單浮在畫面上，點別處或按 Esc 收起來；列印、PDF 與訂閱的圖片不印）。附註分兩種：
  - 作者要的東西沒有畫出來、算不出來，或畫得與設定的不同（`warnings`）——例如預測估計不出來的原因（資料不到
    兩期、資料不足以估計選定的模型）、趨勢線的資料不足、圓餅上的參考線與註解、還不支援的標記類型改以長條呈現。
    標示是「⚠ N 項未能呈現」，檢視器與編輯器都有：看報表的人知道圖上為什麼少了東西
  - 單純的說明：預測與叢集的計算方式與 Tableau 可能有些微差異、Measure Values 的文字表各量值分開排列。圖上沒有
    少東西，只有編輯器列出來（沒有上一種時標示是「ⓘ N 則附註」），檢視器不標示
  - 分法：`tmig.render.notes.INFORMATIONAL` 列出的是單純的說明，沒有列出的一律當成第一種——新加的附註忘了分類時
    會顯示出來，不會把「沒有畫出來」藏起來
  - 附註裡稱呼東西的方式與畫面上相同（`tmig.render.names`），不寫內部的代號：欄位是膠囊上的寫法（SUM(Profit)、
    YEAR(Order Date)，快速 Table Calculation 後面有「△」），參考線是「分析」分頁清單上的寫法（「平均：SUM(Sales)」
    「分布（百分位數）：SUM(Sales)」「盒鬚圖：SUM(Sales)」），註解引用它的文字（超過 30 個字截短，沒有文字時是
    「第 n 個註解」），標記類型與背景地圖的樣式是選單上的名稱（圓餅、衛星），自訂的預測模型寫趨勢與季節
    （「趨勢：加法、季節：無」）
  - 查詢錯誤的文字（檢視器上「未支援」的原因、編輯器的錯誤提示、`/render` 的附註「各標記卡無法另外查詢…」
    「顯示空的列／欄：查不到成員（…）」）也以畫面上的寫法稱呼：篩選是它的欄位（「Top N 篩選「Region」不能逐列
    套用」），量值與 Table Calculation 是膠囊上的寫法（「SUM(Sales) 的 Table Calculation 設定不存在」），公式是
    公式編輯器上的寫法（顯示名稱），標記類型是選單上的名稱（「標記類型「地圖」尚未支援」）。資料庫執行時的錯誤
    訊息（「SQL 執行失敗：…」）照原樣附上；「顯示空的列／欄」的附註遇到這種錯誤只寫「查詢執行失敗」
- **預設排序「依欄位」**（資料窗格「預設屬性＞排序…」的「欄位」，`FieldSort.by`）：這個維度沒有另外排序時，依同一個
  資料源中另一個欄位的彙總值排序（排序依據不必在檢視上）
- **建立使用者篩選**（資料窗格維度的「建立＞使用者篩選…」，Tableau 的「伺服器＞建立使用者篩選」）：列出使用者
  （登入的帳號）與群組，勾選各自看得到的成員，存成布林的計算欄位（與 .twb 的使用者篩選解析後的公式相同的形狀，
  `tmig.field_edit.create_user_filter`）；放到篩選架上選 True 即依登入者篩選
- **自動產生的說明**依 Tableau 自動說明的句子結構：「每個 <維度> 的 <量值>，依 … 細分」「<量值> 對 <量值>」
  「<日期> 的 <量值> 趨勢」，標記卡上的欄位與每個篩選各一句（保留或排除的成員、範圍）
- **分隔線的細部與清除格式**（「格式…」）：列、欄分隔線的顏色、線寬、樣式（實線、虛線、點線）、層級與標題的
  分隔線；「格式」功能表（或工具列「清除」）的「清除格式」把這張工作表的格式與數字格式改回預設（Tableau 的「工作表＞清除＞格式」）
- **線的樣式**（「格式…」，Tableau 的「格式＞線條」）：列、欄的格線與座標軸線各自的顏色、線寬與樣式（實線、
  虛線、點線；`ShelfStyle.grid_line`、`axis_line`，沒有設定時為預設）
- **頁面的顯示歷程**（頁面控制項下方的「顯示歷程」，`Worksheet.page_history`）：目前這一頁之外，也畫出之前
  各頁的標記（可以淡化）、軌跡（同一個標記依頁面的順序連成線）或兩者，可以只顯示之前幾頁。查詢時頁面欄位加到
  詳細資料、之前的幾頁一併取回，主圖、列數與選取只有目前這一頁
- **簡報模式**（檢視器工具列）：全螢幕、只留檢視，下方的列切換前後一張（dashboard、Story、沒有隱藏的工作表），
  按 Esc 結束。一次 Esc 只做一件事：有選取的標記時先離開簡報模式、選取留著，再按一次才取消選取；開著選單、
  清單或對話框時 Esc 先收起它們（沒有全螢幕的環境——桌面版的外殼、不允許全螢幕的瀏覽器——也一樣）。
  快捷鍵與 Tableau 相同是 F7 或 Ctrl+H：在檢視的頁面上按一次進入、再按一次離開；正在輸入文字、開著對話框或選單時
  不處理（那一下按鍵是它們的）。剛離開又馬上進入時照樣進入（上一次結束全螢幕的通知晚到，不算離開）。瀏覽器裡
  F7、Ctrl+H 也是瀏覽器自己的快捷鍵（見「已知限制與待驗證」）
- **地圖的標記圖層**（地圖卡的「＋ 標記圖層」，Tableau 2020.4）：新增一個圖層（`Worksheet.panes` 的一個窗格，
  量值是緯度），標記卡上方的分頁切換要編輯的圖層，可以上移、下移與移除圖層；每個圖層有自己的標記類型、色彩、
  大小、標籤與詳細資料
- **地圖的選取工具**（地圖工具列的矩形、半徑、套索）：在地圖上拖曳，範圍內的標記都選取（與點選多個標記相同：
  觸發選取的動作、摘要卡與「只保留／排除」）；再按一次工具回到平移。多個標記圖層的地圖上各圖層的標記都選得到
  （各自只有自己的維度，例如州與城市）；「只保留／排除」的標記沒有共同的維度時依各自的維度分組篩選、都要符合
  （只保留是各組的交集；Tableau 在這種情況的作法沒有公開資料，這是推定）。底圖的地名不擋住標記的點選
- **依色彩分組的密度圖**：色彩上有離散的維度時每個成員各自一張密度圖（由淺到這個成員的顏色），圖例照常顯示；
  **地圖上的參考區間**（經度或緯度上兩個值之間的色帶）與**趨勢線**（各圖層標記的緯度對經度配適，模型列在
  「描述趨勢模型」）
- **匯入工作表**（「檔案」功能表的「匯入工作表…」，`tmig.merge.import_from`、`POST /api/edit/<報表>/import`）：從工作區中
  開啟的另一份報表選取工作表匯入，或整份匯入（含 dashboard、Story 與動作）；相當於 Tableau 的「匯入活頁簿」、
  書籤與在活頁簿之間複製貼上工作表。用到的資料源（含 Data Blending 的次要資料源）、萃取與來源檔案、圖片一併
  帶過來；與目前報表相同的 id 加上編號、名稱相同的加上「 (2)」；參數名稱與定義都相同時共用，只是名稱相同時
  匯入的參數改名（公式中的 `[Parameters].[…]` 一併改掉）。只匯入部分工作表時，指向沒有匯入的動作與工具提示中的
  工作表的參照拿掉。Tableau 的書籤檔（.tbm）本身不解析：把來源的 .twb／.twbx 開啟後匯入

### 資料源篩選的編輯

資料來源分頁（`/w/<報表>/edit-ds/<資料源>`）的「資料源篩選」列出這個資料源的篩選（.twb 原有的也在其中），可以
新增、編輯、移除；對話框與工作表的篩選相同（一般、萬用字元、範圍、相對日期），沒有彙總的條件與頂部——資料源
篩選逐列套用到使用這個資料源的所有工作表與控制項的可選值，只能篩選維度（量值先轉成維度，或以列層級的計算欄位
篩選）。編輯既有的篩選時，可選值不套用它自己。
API：`GET`／`PUT /api/edit/{報表}/datasources/{資料源}/filters`（`?stage=extract` 為萃取篩選）。

### 即時與萃取（萃取設定）

資料來源分頁的「連線」（Tableau 右上角的「即時／萃取」，`tmig.extract_edit`）：

- **即時 → 萃取**：連線換成萃取（`ExtractConnection`，來源是原本的即時連線，每個實體欄位在來源的位置記在
  `ExtractRefresh.columns`），並立即由萃取層從來源取回資料（`refresh_extract`，寫入報表的 `data/<資料源>.duckdb`）；
  建立失敗（例如沒有連線設定）時維持即時連線
- **萃取 → 即時**：連線換回萃取的來源、欄位換成來源的欄名，之後查詢直接連來源資料庫；萃取篩選留著，改回萃取時再套用
- **萃取篩選**：與資料源篩選同一個對話框，可選值取自萃取的來源（萃取中只有通過萃取篩選的資料）；下次更新萃取時
  套用在來源的資料上
- **增量更新的欄位**（數字或日期的實體欄位）與「立即更新萃取」「增量更新」（Tableau 的「資料＞萃取＞重新整理」）；
  伺服器上的排程更新照舊
- **Relationship 模型**（多張邏輯資料表）：每張邏輯資料表各自一張萃取資料表（`Extract.<資料表 id>`，邏輯資料表中
  的 join 在萃取時合併），關聯照舊；改回即時連線後與原本的資料源相同
- **只取前 N 筆**（`ExtractRefresh.top_rows`）：更新時每張資料表只取來源的前 N 列
- **彙總可見維度的資料**（`ExtractRefresh.aggregate`，單一張萃取資料表時）：更新時依沒有隱藏的維度分組，
  預設彙總是總和、最小值、最大值的量值先彙總（其他預設彙總的量值再彙總會算錯，留在詳細資料中當作分組的欄位）；
  隱藏的欄位在萃取中是 Null，但沒有隱藏的計算欄位與集合用到的、資料源篩選的欄位仍保留。與增量更新不能並用

限制：.twbx 中來源不明的萃取（例如來源是檔案）不能改成即時，也不能在編輯器中更新；檔案型的資料源一律匯入，
沒有即時與萃取之分；Tableau 的「抽樣」沒有（只取前 N 筆依來源傳回的順序）；Relationship 模型的萃取不能彙總
（各資料表的詳細程度不同）。報表套件不含編輯器建立的萃取資料時，開啟後要再更新一次。API：`GET`／`PUT /api/edit/{報表}/datasources/{資料源}/extract`、`POST …/extract/mode`、
`POST …/extract/refresh`。

### dashboard、Story 與動作的編輯

- **dashboard**（「編輯 dashboard」，網址 `/w/<報表>/edit-d/<dashboard>`）：畫面直接以檢視器繪製，點選畫面上的
  區塊或「版面配置」的樹狀清單選取；工作表拖到畫面上或連按兩下加入，物件有水平與垂直容器、文字、標題、圖片
  （按「圖片」先開「編輯圖片物件」：選電腦上的圖片檔或報表中已有的圖片，按確定才加入、取消不加入；選的檔案
  存進報表的 `files/Image/`（`POST /api/edit/<報表>/images`），存檔、發布時跟著報表，同名而內容不同的另取
  檔名；屬性的「選擇圖片…」換圖片；找不到圖片的區塊檢視時是空的、編輯器裡寫出檔名）、
  網頁（內嵌外部網頁；網址可以留空白，由網址動作載入）、空白、導覽按鈕與匯出按鈕（按鈕的動作可以改成導覽、
  顯示／隱藏區塊或匯出 PDF、圖片、PowerPoint），工作表上的篩選、圖例、頁面與參數加入成控制項；並排加入時依序排列的容器重新分配空間，
  其他容器與 Tableau 拖放到區塊上相同把該區塊分成兩半；選「浮動」（並排／浮動的切換）時加在並排的版面之上，可以拖曳
  移動與調整大小。選取的區塊可以換工作表、顯示大小、顯示標題、控制項的樣式、文字、網址、按鈕的目標、
  容器的排列、固定寬度或高度、隱藏、背景、框線、外距與內距、前移與後移、移除；文字物件的「編輯文字…」以格式文字
  編輯器修改（可以插入參數的值）；水平或垂直容器的「平均分配內容」（沒有固定大小的子區塊等分，之後加入、移除
  區塊時維持等分）；篩選控制項的「只顯示相關的值」；工作表區塊的「顯示說明」；工作表的「當作篩選器使用」建立（或移除）以這張工作表為來源、篩選 dashboard 上
  其他工作表的篩選動作（與 Tableau 相同，動作名稱為自動產生）；dashboard 的大小（固定、自動、
  範圍與寬高）與背景；新增、複製、刪除 dashboard（Story、按鈕或動作用到的不能刪）。裝置版面：新增手機版面
  （預設版面上的物件由上到下排列，符合寬度）、平板或桌面版面（複製預設版面），切換到裝置版面編輯它自己的區塊與
  大小（與預設版面相同、固定、符合畫面、符合寬度），刪除裝置版面
- **動作**（dashboard 編輯器的「動作…」）：篩選、醒目提示、網址、變更參數、前往工作表與變更集合值，執行方式（選取、
  暫留、功能表）、只在這個 dashboard 或所有工作表、來源與目標工作表、清除選取時的處理；網址動作可以設定網址目標
  （新分頁，或載入 dashboard 上的網頁物件）與多個值的分隔字元；篩選動作以所有欄位或選定的
  欄位篩選（目標工作表上自動加上對應的多欄位篩選，目標改變或動作刪除時一併移除），醒目提示動作比對所有欄位或選定的
  欄位。dashboard 的物件中可以加入工作表的醒目提示控制項（欄列架與標記卡上的維度）
- **Story**（「編輯 Story」，網址 `/w/<報表>/edit-story/<Story>`）：故事點的新增、複製、排序、刪除、說明文字與
  顯示的 dashboard 或工作表（換掉時清除記下的篩選與選取），Story 的名稱、標題、導覽的樣式、箭頭與大小；
  右側即時預覽；「更新故事點」把預覽中目前的狀態（類別篩選的選取、參數值、
  篩選動作的選取與醒目顯示的標記）記到故事點，「另存為新的故事點」以目前的狀態新增一個；範圍類的篩選
  （數值滑桿、日期範圍）記下起訖（`StoryFilter.mode` 為 `range`，`min`、`max`）。**故事點上的文字物件**
  （「＋ 文字物件」，`StoryPoint.notes`）：格式文字的說明框，在預覽上拖曳移動、點一下編輯文字、位置與大小；
  **Story 的格式**（「格式…」，`Story.format`）：Story 的底色、標題的顏色、字級與對齊、導覽的底色與文字顏色、
  文字物件的底色與文字顏色，「清除格式」回到預設。這兩項只能在編輯器設定（.twb 的寫法沒有樣本，未解析）
- **dashboard 的格線**（dashboard 編輯器「版面配置」分頁的「顯示格線」與間距、「dashboard」功能表的「顯示格線」，Tableau 的「顯示格線」「格線選項」）：畫面上的
  對齊輔助線；顯示時浮動區塊的拖曳與調整大小貼齊格線（Tableau 的格線只是輔助線、不會貼齊）。設定記在瀏覽器，
  不寫進報表

編輯器與 Tableau Desktop 2021.2 各項功能的對照（有、部分、沒有）見 `docs/editor-coverage.md`。

每次變更由引擎檢查（欄位、參照、Table Calculation）後寫回開啟中的報表（`tmig.edit`、`/api/edit`），檢視立即
重新查詢；不正確的變更退回並顯示原因。改過還沒儲存的報表在關閉前會提醒，「儲存」把變更寫進報表套件。

### 連線到資料與資料源的編輯

與 Tableau Desktop 2020.2 起的「連線」與「資料來源」分頁相同（`kernel/src/tmig/authoring.py`、
`tmig_service.datasource_editor`、檢視器的 `web/src/editor/DatasourceEditor.tsx`）：

- **新增報表**（檔案選單的「新增報表（連線到資料）」Ctrl+N、首頁的「新增報表（連線到資料）…」）：選 Excel
  （.xlsx、.xlsm、.xls）、文字檔（.csv、.tsv、.txt、.tab）或空間檔案（Shapefile、GeoJSON、KML、TopoJSON、
  Esri File Geodatabase 的 .gdb.zip 或 .gdb 資料夾、MapInfo 的 .tab 或 .mif；第一行是 `!table` 的 .tab 是 MapInfo），
  檔案中有多張工作表或圖層時選一張；建立一個資料源與空白的「工作表 1」，開啟資料源的編輯畫面（網址
  `/w/<報表>/edit-ds/<資料源>`）。已開啟的報表以工具列的「新增資料源…」加入另一個資料源，「編輯資料源」
  （或編輯器下方的「資料來源」分頁、「資料」功能表與資料窗格選單的「編輯資料源…」）開啟目前資料源的編輯畫面
- **型別與角色**：Excel 依儲存格的型別（整數、小數、日期、日期時間、TRUE／FALSE 為布林，文字的儲存格即使寫的是
  數字也是字串）；文字檔自動判斷分隔字元（逗號、分號、Tab、直線）與字元集（UTF-8、Big5、Latin-1），依 en_US
  的寫法判斷數字與日期，只有 true／false 是布林值。數字為量值（預設總和），其他為維度；欄名是 Country、State、
  City、Postal Code、Latitude、Longitude 等時給地理角色（緯度、經度預設平均，其他地理欄位是維度）
- **連線到伺服器**（檔案選單與首頁的「新增報表（連線到伺服器）…」，已開啟的報表以工具列的「連線到伺服器…」
  加入資料源）：選資料庫的類型（PostgreSQL、SQL Server、MySQL、Oracle、Snowflake、Redshift、Databricks、
  Spark SQL、Presto／Trino、Athena、BigQuery、Teradata、Hive、Impala、Db2、SAP HANA），輸入伺服器、連接埠、
  資料庫（Oracle 為服務名稱、BigQuery 為專案）、結構描述與帳號登入；登入後列出資料表與檢視（可換結構描述、
  搜尋），選一張或輸入**自訂 SQL**（「檢查欄位」在資料庫執行，取前 200 列判斷型別）。資料留在資料庫（Live
  連線），查詢時才連線。**密碼不寫進報表與設定檔**：設定檔（設定資料夾的 `connections.toml`）只記
  `${TMIG_DB_<連線代號>}`，密碼只在本機引擎的記憶體中，勾選「記住密碼」時另存在作業系統的認證管理員
  （Windows 的 Credential Manager）、下次啟動讀回；也可以改用自己設定的環境變數。已建立的連線列在對話框上面，
  可以直接使用、重新登入或刪除。發布到伺服器後由伺服器的連線設定檔提供伺服器用的帳號（建議唯讀的服務帳號）
- **資料庫的資料源**：左邊列出資料庫中的資料表（加入的方式與檔案相同：關聯、join、聯集）與「新增自訂 SQL…」；
  自訂 SQL 可以「編輯 SQL…」，名稱相同的欄位保留（含名稱、角色與計算的參照）、新的欄加入、不在查詢中的欄位
  移除（有工作表或計算用到時不能移除）；樞紐在資料庫中以 UNION ALL 計算
- **加入資料表**（左邊連線中的資料表，或「其他檔案…」）：預設以**關聯**成為另一張邏輯資料表（條件預設為名稱
  相同的欄，可用 =、≠、<、≤、>、≥ 與多個條件）；也可以在邏輯資料表中 **join**（內部、左側、右側、完整外部）或
  **聯集**（欄位依名稱對應，另有 Table Name，Excel 另有 Sheet、文字檔另有 Path）。join 的類型與條件、關聯的
  條件可以修改；最後加入的 join、聯集的一張與邏輯資料表可以移除（只在這張資料表中的欄位一併移除）
- **Join 計算與關聯計算**：條件的任一邊選「建立 Join 計算…」或「建立關聯計算…」改成輸入公式（以欄位的顯示
  名稱書寫，例如 `LOWER([Region])`）。join 計算存成以 `[資料表].[欄位]` 參照實體欄位的公式（與 Tableau 的
  寫法相同），只能用到那一邊的資料表；關聯計算存成所屬邏輯資料表上隱藏的計算欄位（`DataField.relationship_calc`，
  公式以欄位的內部名稱參照，改名後仍有效），只能用到那一張邏輯資料表的欄位，不能彙總、用 LOD 或參數；不再用到時
  自動移除。從 Tableau 開啟的報表中，關聯條件寫成公式（`<expression op='DATETRUNC(&apos;month&apos;,[ORDER_TS])' />`，
  見 Tableau Server 處理過的報表）時同樣轉成關聯計算
- **加入連線**（左邊連線清單下的「加入連線：檔案…／資料庫…」）：檔案的資料源可以再連到資料庫（已建立的連線，或在
  對話框中新的連線、登入），資料庫的資料源可以再加上檔案或另一個資料庫：不同連線的資料表以關聯或 join 合併
  （**跨資料庫的 join**，資料源標示「跨資料庫」，邏輯資料表中的資料表標示所在的連線，見「Live 連線」）。資料庫的資料源
  第一次加入其他連線的資料表時，原本的資料表與自訂 SQL 改成跨資料庫的 join 中資料庫的資料表（欄從資料庫取得）；移除後
  只剩同一個資料庫時改回一般的 Live 連線。跨資料庫的 join 中資料庫的欄位也可以變更資料類型（取回後轉換）
- **樞紐**：欄位清單中勾選同一張資料表的欄，「樞紐所選的欄位」轉成 Pivot Field Names、Pivot Field Values；
  可以移除樞紐
- **Initial SQL**（資料源名稱旁的「Initial SQL…」，Tableau 的「資料＞初始 SQL」；有資料庫連線的資料源才有：Live
  連線、萃取的來源、跨資料庫的 join 中的各資料庫，有多個時先選連線）：輸入連上資料庫時先執行的 SQL，「插入參數…」
  放進 `[TableauServerUser]` 等五個參數或 `<ServerOnly>`；清空後確定是清除。確定後存檔並以新的內容連一次資料庫，
  執行失敗時留在對話框顯示資料庫的訊息（已經存檔：桌面版不執行 `<ServerOnly>`、沒有登入伺服器時帳號是空的，在桌面版
  失敗的不一定在伺服器上也失敗）。桌面版的 `[TableauServerUser]` 是登入伺服器的帳號，與 Tableau Desktop 相同。
  萃取的資料源在更新萃取時執行（見「Initial SQL」）
- **資料解譯器**（Excel 檔與文字檔下的「使用資料解譯器」）：工作表上面有標題、說明的列時找出標題列（至少兩格有值、都是文字
  或整數，且下一個有值的列至少一半的欄有值；Tableau 的判斷方式沒有公開，這是推定的規則），之前的列與之後空白的列
  不算；切換時名稱相同的欄位保留。Excel 的具名範圍（.xlsx、.xls）與工作表一起列在左邊，可以當作資料表加入
- **關聯的效能選項**（關聯條件下方的「效能選項」，`Relationship.left_unique`、`right_unique`、`left_complete`、
  `right_complete`）：兩邊的基數（多、一）與參考完整性（部分記錄相符、所有記錄相符），與 Tableau 相同的四個設定；
  .twb 的 `unique-key='true'` 解析成基數「一」。這裡只是記下設定：查詢一律以最保守的方式計算（多對多、部分記錄
  相符），設定與資料相符時數字與 Tableau 相同；設定與資料不符時 Tableau 會依設定簡化查詢而得到不同的數字，
  本平台不會
- **萬用字元聯集**（檔案下的「萬用字元聯集…」）：資料夾（作者電腦上的路徑）中檔名符合模式（預設把檔名中的數字換成
  `*`，例如 `Sales *.xlsx`）與工作表名稱符合模式的資料表接在資料表下面，另有 Path（檔案的路徑）與 Sheet；符合的檔案
  複製進報表（建立時的快照），目標的檔案使用資料解譯器時接上的檔案也使用
- **欄位**：改名、隱藏、角色（維度／量值）與資料類型（檔案的欄位以新的型別重新匯入，不符合的值成為 Null）；
  工作表、計算欄位、集合、階層或篩選用到的欄位不能移除、改角色或型別，退回並列出用到的地方。下面是資料的預覽，
  Relationship 模型點選邏輯資料表切換
- **檔案的複本**：連線的檔案複製進報表（`Data/<檔名>`，與 Tableau 封裝的 .twbx 相同），報表套件中存在
  `sources/` 之下；每次變更後以 DuckDB 重新匯入。再開啟報表套件、驗證（`tmig verify`）與發布都由這份複本匯入
- API（`/api/edit/<報表>/…`）：`POST inspect-file`、`POST datasources`（新增資料源：檔案，或 `connection` 與
  `table_path`／`sql`）、`GET datasources/<資料源>/model`、`POST …/tables`（加入資料表）、`DELETE …/tables/<資料表>`、
  `PUT …/joins/<資料表>`、`PUT …/relationships/<邏輯資料表>`（條件的一邊可以是 `{"formula": …}`）、
  `POST …/pivot`、`DELETE …/pivot/<名稱>`、`PUT …/fields/<欄位>`、`PUT datasources/<資料源>`（改名）、
  `GET …/remote-tables`、`POST …/remote-columns`、`PUT …/custom-sql/<名稱>`、`PUT …/initial-sql`
  （`{"sql", "connection"}`；模型的 `sources` 是可以設定的連線）；桌面版另有 `POST /api/desktop/inspect`、
  `POST /api/desktop/new`、`POST /api/desktop/new-live` 與連線的 `GET /api/desktop/connectors`、
  `GET|POST /api/desktop/connections`、`POST …/connections/<連線>/sign-in`、`DELETE …/connections/<連線>`、
  `GET …/connections/<連線>/tables`、`POST …/connections/<連線>/columns`（`tmig_service.desktop_connections`、
  `tmig.data.catalog`）
- 限制：萃取與已發布的資料源只能改資料源的名稱與欄位的名稱、隱藏、角色（連線與資料表沿用原本的定義）

## 發布與版本

伺服器（`tmig serve`）的報表庫（`--store`，或環境變數 `TMIG_STORE`；預設為報表目錄旁的 `store`）存放發布的報表：

- **每一版都保留**：每次發布是一個新版本，存成不再改動的報表套件；回復舊版也是發布一個內容相同的新版本。
  伺服器的檢視器在報表的「版本」中列出每一版，可以比較兩版（列出新增、刪除、修改的工作表、欄位等的名稱）、
  回復與下載。報表庫用的資料庫還沒決定前（規格待確認事項第 12 項）存在資料夾中（`tmig_service.store`）
- **名稱**：發布時填的名稱就是報表在伺服器上的名稱（與 Tableau Server 相同）——首頁的清單與搜尋、檢視器的標題列、
  我的最愛、最近檢視、使用統計、訂閱與警示都用它，同一個檔案發布成不同名稱的幾份報表分得出來；站台管理中改名後
  下一個請求就是新的名稱。報表套件裡的報表定義不變（下載的版本與發布的相同）
- **發布前檢查兩次**：桌面版先查、伺服器再查一次（`tmig_service.checks`）：結構與參照完整、每張工作表都能查詢、
  每個 Live 連線在伺服器上都有設定；不支援的寫法只列為提醒
- **衝突**：從伺服器開啟或發布後，桌面版記下版本；再發布時伺服器上已有別人發布的新版本，列出兩邊的差異，
  由作者決定覆蓋（合併兩邊的修改要等編輯功能）
- **權限**（沿用 Tableau）：發布到 Project 要 Publish（站台角色要能發布），覆蓋要 Overwrite，從伺服器下載要
  Download Workbook；第一次發布與 Tableau 上同名的 workbook 時要它的 Overwrite，之後沿用它的權限與擁有者；
  新報表的權限取自 Project 的 workbook 預設權限。沒有權限檔時登入的人都可以發布；沒有身分驗證時不能發布
- **發布資料源**（與 Tableau Desktop 的「發布資料源」相同）：工具列的「發布資料源…」、伺服器選單，或資料源分頁的
  「發布到伺服器…」選擇報表中的資料源、Project 與名稱（已經連到伺服器上的資料源時預設覆蓋它）。勾選「報表改用
  發布的資料源」（預設）時報表的資料源改連到伺服器上的資料源：之後發布這份報表，伺服器上的報表直接使用伺服器上
  的資料源的萃取（萃取更新一次，每份用到它的報表都看到新的資料）；桌面版仍以報表中的那一份資料查詢。
  即時連線的資料源只上傳定義，伺服器以自己的連線設定連到資料庫（帳號密碼不會上傳）
- **桌面版登入**（`tmig_service.desktop_auth`）：桌面版開啟系統瀏覽器到伺服器的核准頁，使用者以平常的方式登入
  （SSO 閘道、OIDC、開發模式）後按「允許」，伺服器導回桌面版的本機網址，桌面版以授權碼＋PKCE 換得權杖，
  存在 Windows 的認證管理員。權杖預設 30 天到期（`TMIG_DESKTOP_TOKEN_DAYS`），伺服器只存雜湊值；
  使用者可以在檢視器的「桌面版的登入」撤銷。proxy 模式下反向代理要放行帶 `Authorization: Bearer` 的請求

API 見 `tmig_service.publishing`（發布、版本、比較、回復、下載）；上傳的報表套件上限 2 GB（`TMIG_MAX_UPLOAD_MB`）。

## 檢視者的工具

伺服器的檢視器與 Tableau Server 相同，工具列上有（`tmig_service.downloads`、`viewer_api`、`viewer_store`）：

- **復原、重做、還原、重新整理**：篩選、參數、選取與動作的每一步都可以復原（最多 50 步）；還原回到原始的檢視
  （或網址的篩選組合）；重新整理以目前的狀態重新查詢
- **匯出**：圖片（PNG）、資料、交叉資料表、PDF、PowerPoint，依權限顯示（下載圖片／PDF、下載摘要資料）。
  桌面版先問存檔的位置，再由本機引擎寫出檔案（.png、.pptx、.pdf、.csv、.xlsx）；編輯器的「分析＞檢視資料…」
  也可以下載 CSV。寫不出去時在對話框（資料、交叉資料表）或右下角的訊息（圖片、PowerPoint）說明原因：
  - PDF（列印）：先開啟**頁面設定**（Tableau 的「檔案＞頁面設定」）：紙張（A4、A3、Letter、Legal、Tabloid）、
    方向、邊界、列印縮放（自動、符合一頁、符合頁寬、縮放比例，以及 Tableau 的「適合於」橫向 N 頁、縱向 M 頁），
    要不要印標題、說明與圖例，以及「標題和分頁」：「在每頁中重複標題和圖例」「在窗格邊界分頁」（兩者預設勾選）；
    按「列印」後以瀏覽器的列印輸出（可以另存 PDF）。設定記在瀏覽器，下次沿用；不經過頁面設定、直接以瀏覽器
    列印（Ctrl+P）時也照記下的設定。dashboard 上的匯出 PDF 按鈕套用記下的設定
  - **工作表分頁印**（`web/src/printPages.ts`，Tableau Desktop 的列印：比一頁大的檢視分成多頁）：單獨檢視的
    工作表印出整張——在區塊內捲動的工作表（標準大小放不下、符合高度或整個檢視時列高保持）也一樣，不只是區塊裡
    看得到的那一段。放不下一頁時分成多頁，頁的順序先往下、再往右；「在每頁中重複標題和圖例」時每一頁都有
    欄標題（含欄位標籤）、列標題、座標軸與圖例，「在窗格邊界分頁」時分頁落在列與列、欄與欄之間（不切過儲存格的
    文字與標記）。標題印在第一頁的最上面、說明接在最後一頁的圖下面（放不下時另起一頁）；工作表上的篩選與參數
    控制項不印（與 Tableau 列印單張工作表相同）。「自動」縮放：圖填滿區塊的方向（大小跟著視窗走）縮到紙張之內，
    固定大小的方向（標準大小的列高、欄寬）照原尺寸、分頁印；「符合一頁」「適合於」把整張工作表縮到指定的頁數
    之內。dashboard 與 Story 不分頁：整個縮放成一張
  - 交叉資料表：選擇 dashboard 中的工作表，下載 Excel 或 CSV；內容與「資料」的表格相同（別名、數字與日期格式、
    小計與總計），Excel 中千分位、百分比、小數與括號的負數存成數字並套用相同的格式，可以直接計算
  - 資料：摘要（每個標記一列、未格式化的值）與完整資料（工作表的基礎資料列：資料源篩選、列層級權限、工作表的篩選，
    只含仍在檢視中的標記的資料列；要「檢視基礎資料」的權限），畫面上預覽前 1,000 列，下載 CSV（完整資料最多
    1,000,000 列）。CSV 為 UTF-8（含 BOM，Excel 直接開啟中文不會亂碼）。摘要的浮點數不帶二進位浮點數加總留下的
    尾數（909369.9110000005 寫成 909369.911：取相差不到一兆分之一的十進位數字中位數最少的，前 12 位有效數字不變）
  - 列印與 PDF 只印檢視本身：工具列、每張工作表右上角的列數與「資料」切換、切入的連結與頁面的灰色底都不印
  - 在區塊內捲動的工作表（標準大小放不下的文字表；符合高度、整個檢視時區塊太矮而列高保持的文字表也一樣，兩者
    輸出的內容相同）：列印與 PDF 印出整張（分頁，見上面）；圖片與 PowerPoint 一律是最上面的一段（不管捲到
    哪裡），區塊外的列都不在輸出裡；顯示捲軸的瀏覽器裡捲軸也畫進圖片（桌面版的視窗裡沒有實測）。
    訂閱的圖片是剛開啟、沒有捲動的畫面（最上面的一段）；訂閱的 PDF 是整張工作表縮到一頁（與「符合一頁」相同）。
    見「已知限制與待驗證」
- **多選下拉的篩選控制項**：選單浮在 dashboard 上面（不受篩選區塊的大小限制，下方放不下時往上開），勾選時不收，
  點別的地方或按 Esc 收起來
- **框選**（Tableau 預設的矩形選取工具）：在圖上按住左鍵拖曳出方框，方框碰到的標記都選取——與點選多個標記相同
  （觸發選取的動作、「只保留／排除」）；按住 Ctrl／⌘／Shift 加進原本的選取，框到空白處取消選取。地圖上拖曳是平移，
  框選用地圖工具列的選取工具。圖表裡的文字（座標軸、標題）不會被拖曳反白。桌面版的工作表編輯器裡也一樣
- **按 Esc 取消選取**（Tableau 的「Clear the mark selection」）：點選、框選的選取都取消，篩選動作等跟著清除，釘住的
  工具提示放開。dashboard 上幾張工作表都有選取時，按一次取消最近選取的那一張，再按一次是前一張；正在輸入、
  開著下拉選單或對話框時，Esc 是它們的；簡報模式中 Esc 先離開簡報模式（選取留著）
- **手機大小的畫面**（畫面較短的一邊 500 像素以下，與選用裝置版面的判斷相同）：工具列收成一列——回報表清單、
  切換檢視與「⋯」（更多），其餘的工具（簡報模式、復原、匯出、自訂檢視、訂閱、登入者…）按「⋯」才展開；換到另一個
  檢視時收起來。dashboard 有手機版面時用手機版面；沒有時顯示預設版面：隨畫面調整大小的（自動，或沒有下限的
  範圍）不壓進手機的寬度，以作者設計的尺寸顯示，固定大小的照原尺寸，比畫面寬的部分在 dashboard 自己的區塊裡
  左右捲動（工具列留在原位）。單張工作表的篩選與參數控制項排在圖的下面；Story 的標題與故事點導覽留在畫面的
  寬度之內，故事點的內容比畫面寬時自己捲動。桌面與平板大小的畫面不變
- **自訂檢視**（有報表庫時）：把目前的篩選、參數與選取存成自訂檢視，可以設為自己的預設檢視（開啟時套用）；
  有「分享自訂檢視」的權限時可以公開給看得到這份報表的人；同名時取代。套用或剛儲存的自訂檢視，名稱顯示在工具列
- **留言**（有報表庫時）：每個 view 的留言，「檢視留言」看、「新增留言」留；留言的人、報表的擁有者與站台管理員可以刪除；
  時間以檢視者電腦的時區顯示
- **載入中**：查詢還沒回來時工作表的標題是工作表的名稱（自訂標題要代入查詢結果，先不顯示），不顯示內部的代號；
  沒有資料的工作表（例如篩選動作的目標還沒有選取）是空白的，不畫座標軸
- **我的最愛**（有報表庫時）：首頁列出我的最愛與最近檢視（依使用紀錄），也可以搜尋報表、dashboard 與工作表的名稱

## 地圖

Latitude／Longitude (generated) 在欄列架上（或緯度、經度角色的欄位分別在列與欄）的工作表畫成地圖：

- **地理角色**：國家／地區、州／省／直轄市／縣市、郡（美國）、城市、郵遞區號（美國）、緯度、經度（.twb 的
  `semantic-role`）；位置在查詢之後依檢視中最細的地理角色找出，較粗的角色（例如州之於同名的城市）與資料源的
  預設國家、州（「編輯位置」）作為範圍，「編輯位置」中指定的地名（例如 Florda → Florida）先套用；找不到或
  有多個可能的值不畫，右下角與 Tableau 相同顯示「N 個未知」，「篩選資料」（未知的位置）也照樣去掉
- **地名資料**：平台內建 Natural Earth、GeoNames、美國人口普查局等公開資料（約 15 MB，見 NOTICE.md）：258 個國家
  （含各語言與中文的國名、ISO 代碼）、4,597 個州／省（台灣的 22 個縣市以中文比對，臺／台通用；連江縣沒有區域形狀，以圓點呈現）、約 18 萬個
  城市、3,230 個美國的郡（含 FIPS 代碼）與 4 萬多個美國郵遞區號；比對時不分大小寫、全半形、重音與常見的縮寫
  （St.／Saint 等）。資料由 `tools/geodata/fetch.sh` 下載、`tools/geodata/build.py` 產生
- **標記**：自動（有 Geometry 時填色，否則圓點）、地圖（填色）、圓形、方形、形狀、文字、線與密度；填色地圖依量值
  著色且沒有設定色盤時與 Tableau 相同為 Blue-Teal。地圖上的標籤：區域在中央、點在右側，互相重疊的不顯示
- **密度圖**：各標記以高斯核疊加（大小決定核的半徑），依密度與 Tableau 的配色（藍色、多色等）與強度上色
- **標記圖層**（Tableau 2020.4 起）與雙軸地圖：每個圖層一張標記卡，依序由下往上畫；詳細程度較粗的圖層各自查詢。
  2021.2 起圖層的元素與屬性帶功能旗標的前綴（`_.fcp.Layers.true...`），讀入時一律換成一般的名稱；空間欄位的圖層
  以 geometry 編碼為形狀，沒有時依圖層的名稱（空間欄位的名稱），卡上其他的空間欄位不畫（記為略過）。
  較早的雙軸地圖在欄架上重複 Longitude (generated)，與列架上重複 Latitude 相同畫成兩個圖層
- **分格的地圖**：欄列架上有離散的維度時與 Tableau 相同每一格一張地圖（範圍相同），標籤的重疊依每一格的大小判斷
- **參考線**：緯度、經度座標軸上的參考線（平均、中位數、最小、最大、常數、參數等）畫成橫跨地圖的緯線、經線，
  以標記的位置計算；參考區間、分布與盒鬚圖尚未畫出
- **空間資料**（Tableau 的空間檔案與空間函式）：
  - 空間檔案（`ogrdirect` 連線，沒有萃取時匯入資料庫）：Esri Shapefile（.shp 與 .dbf、.prj、.cpg，或壓縮成
    .zip）、GeoJSON、KML／KMZ、TopoJSON、**Esri File Geodatabase**（.gdb 資料夾或壓縮的 .gdb.zip，資料表是
    feature class）與 **MapInfo**（.tab 與 .dat、.map、.id，或 .mif 與 .mid）；File Geodatabase、MapInfo 以 GDAL
    （pyogrio，與 Tableau 相同是 OGR：連線記有 `ogr-grid-shift-folder`）讀取，形狀的 Z、M 值略過、只有一個部分的
    MULTI* 寫成單一的形狀（與 Shapefile 相同），File Geodatabase 的日期欄位是日期時間。投影座標依 .prj（或檔案記下的
    座標系統）換成經緯度（橫麥卡托如 TWD97 二度分帶與 UTM、麥卡托與 Web Mercator、Lambert 正形圓錐如美國州平面座標、
    Albers 等積圓錐、Lambert 方位等積，公式依 EPSG Guidance Note 7-2，以其中的範例驗算；其他投影如荷蘭的斜軸立體、
    瑞士的斜軸麥卡托以 PROJ 計算）；多邊形的各環依包含關係分成外環與內環；屬性的文字依 .cpg（例如 Big5 的 950）
    解碼。萃取中的形狀（GEOGRAPHY）以 WKT 匯入
  - **座標基準面之間的轉換**：與 Tableau 相同「不論座標系統，一律轉換成 WGS84（EPSG:4326）」。WGS84 與 GRS80 橢球的
    基準面（TWD97、NAD83、ETRS89、GDA94、JGD2000 等，與 WGS84 相差約 1 公尺）直接以上面的公式換成經緯度；其他橢球的
    基準面（TWD67、OSGB 1936、Tokyo、NAD27、ED50 等）以 PROJ（pyproj）依 EPSG 資料庫的轉換參數計算（沒有下載的格網檔
    時以七參數等，與 GDAL 預設相同；OS 的範例點相差在 2 公尺之內）。EPSG 資料庫沒有 TWD67 到 WGS84 的參數，以臺灣
    常用的七參數補上（台北的 TWD67 與 TWD97 約相差 X + 828、Y − 207 公尺，與常用的換算相符）；MAKEPOINT(X, Y, SRID)
    同樣轉換
  - 形狀與 Hyper 讀入時相同處理（經緯度上的直線邊切分、外環逆時針），以 Hyper 實測逐字相同
  - 空間函式：MAKEPOINT(緯度, 經度)、MAKEPOINT(X, Y, SRID)（EPSG 代碼：TWD97、UTM、Web Mercator、日本與韓國的
    平面座標等以公式計算，其他代碼以 PROJ 計算）、MAKELINE、DISTANCE（WGS84 橢球的大地線，GeographicLib）、BUFFER（點的 120 邊形，與 Hyper
    相同）、AREA（大地線多邊形的面積）與彙總 COLLECT（同種形狀合成 MULTI*，不同種合成 GEOMETRYCOLLECTION）。
    與 Hyper 的 `tableau.geo_*` 函式逐一比對：DISTANCE、AREA 的相對差距在 1e-6 以內（Hyper 內部的座標精度約
    6e-8 度），MAKEPOINT、MAKELINE、COLLECT 的文字相同，BUFFER 的頂點相差在 3e-7 度以內
  - 空間 join（Tableau 2019.2 起的 Intersects，常與 join 計算的 MAKEPOINT 一起用）：兩邊的形狀相交即連接，
    邊界也算（點在邊上、形狀相接），落在內環（洞）中不算，以經緯度平面計算（Hyper 讀入時已切分長的邊）。
    沒有萃取時在 DuckDB 中執行：形狀與它的範圍在資料表的子查詢中先算好、以範圍的不等式縮小配對，點依另一邊的
    形狀分組一次算完（20 萬個點對 21 個縣市約 3 秒）；inner、left、right、full 都支援。Live 連線中兩邊都是資料庫的
    空間欄位時在資料庫計算（見「Live 連線」）。桌面版的資料源分頁：join 條件的比較選「相交」（兩邊要是空間欄位或
    傳回形狀的 join 計算，例如 MAKEPOINT([Lat], [Lon])；關聯不能用相交）
  - 地圖：標記卡上的 COLLECT 是每個標記的形狀，多邊形填色（與填色地圖相同的框線與 Blue-Teal 色盤）、線依顏色
    畫線（粗細依大小，兩點之間畫成大地線）、點畫成圓點（大小依欄位）；形狀依畫面的像素簡化，WKT 不放進規格；
    工具提示不列出形狀
  - 編輯器：資料窗格以「🗺」標出空間欄位；連按兩下或拖到標記卡上與 Tableau 相同建立地圖（COLLECT 為標記卡
    「空間」格的形狀，Latitude／Longitude (generated) 放到欄列架）
- **背景地圖**：淺色、一般、深色、街道、戶外、衛星的配色、淡化，以及陸地、水域、國界、國名、州界、州名、郡界、
  城市名稱等圖層的開關（.twb 的 `map-style`、`washout` 與 `map-layer`）；底圖的地名依縮放層級出現，與標記的
  標籤重疊的不顯示
- **縮放**：與 Tableau 相同自動縮放到所有標記的範圍（只算主要的陸塊，例如法國不含海外省）；平移、縮放後存檔的
  地圖畫出存檔的範圍，跨越 180 度經線的範圍（例如俄羅斯、阿拉斯加的島嶼）以該處為中心
- **平移與縮放**（`web/src/mapZoom.ts`）：與 Tableau 相同，滾輪以游標為中心縮放、拖曳平移、連按兩下放大（Shift 連按
  兩下縮小），左上角的地圖工具列有放大、縮小與「重設地圖」（回到依標記自動縮放）。投影的自動縮放（Vega 的 fit）換成
  同樣的縮放與位移（與 d3 的 fitSize 相同的計算，誤差在 1e-12 像素），再乘上平移與縮放，操作時立即重畫；停止操作後以
  畫面中的經緯度範圍重新繪製（查詢狀態的 `viewport`，底圖的細節、州界與地名依新的縮放層級）。拖曳後放開滑鼠不當成
  點選標記。檢視器中只在這個檢視（篩選改變後維持目前的範圍）；編輯器與 Tableau Desktop 相同存進工作表
  （`map.viewport`，可以復原）。分格的地圖尚不能平移、縮放
- **檢視器與匯出**：形狀與底圖以 TopoJSON 從 `/api/geo/<版本>/` 取得（不需登入、瀏覽器長期快取），畫面之外的
  形狀裁掉；匯出與訂閱的截圖等形狀下載、畫好才截取
- **編輯器**：資料窗格以地球圖示標出地理角色的欄位，「🌐」指定地理角色與編輯位置（預設國家、州與指定的地名）；
  連按兩下地理角色的欄位與 Tableau 相同建立地圖（國家、州、郡、郵遞區號為填色地圖），資料窗格也列出
  Latitude／Longitude (generated)；標記類型有「地圖」「密度」，地圖卡設定背景地圖、淡化、地圖圖層、篩選掉未知的
  位置與重設地圖

## 色彩

工作表的顏色與 Tableau 相同（`kernel/src/tmig/parser/colors.py`、`kernel/src/tmig/render/colors.py`）：

- 離散欄位：資料源或工作表中指定給各值的顏色（字串、數值、布林、Null；Measure Names 指定給量值的顏色依顯示名稱
  與別名對應）；沒有指定的值依色盤的順序（預設 Tableau 10，Tableau 10 以前的報表常見的 tableau-10 為舊的 10 色）
- 連續量值：沒有設定時，值都是正數（或都是負數）用 Blue，有正有負用 Orange-Blue Diverging（以 0 為中心）；
  內建色盤以 .twb 中的內部名稱對應（blue_10_0、red_green_diverging_10_0、tableau-map-temperatur 等 43 個），
  也支援自訂色盤、反轉、分段（等分成 N 段）、範圍的起訖與發散色盤的中心；發散色盤預設兩邊對稱，
  離中心較遠的一端用到最深的顏色，Use Full Color Range 時兩端都用到最深的顏色；總計預設不影響色階的範圍
- 標記卡：沒有顏色欄位時的顏色、不透明度與框線（面積的框線畫成上緣的折線）；折線與面積與 Tableau 的預設相同
  不畫出點（透明的點仍可停留顯示工具提示、點選）

### 自訂的圖片形狀

Tableau 的自訂形狀調色盤（`My Tableau Repository\Shapes\<調色盤>\<檔名>`，含 Tableau 內建的 Arrows、KPI、Gender、
Bug Tracking 等圖片調色盤）用到的圖片存在活頁簿裡（`<external><shapes><shape name='調色盤/檔名'>` 的 base64，依
Tableau 公布的 .twb 結構與公開報表）。解析後以圖片的路徑表示（`MarkShape.image`、`ShapeAssignment.image`，
`Shapes/<調色盤>/<檔名>`）：標記卡上固定的形狀（窗格樣式的 `<format attr='shape'>`，`Marks.mark_shape`）、依欄位指定給
各成員的形狀（`<encoding attr='shape'>` 的 `<map to='調色盤/檔名'>`）與資料源中維度的預設形狀
（`kernel/src/tmig/shapes.py`、`kernel/src/tmig/parser/colors.py`）。圖片與 dashboard 的圖片相同，檢視時才從報表的
`files/` 或原本的 .twbx／.twb 讀出（`GET /api/workbooks/<報表>/images/Shapes/…`），發布與存成報表套件時一起帶走。

- **呈現**：圖片畫在標記的位置（散佈圖、表格中的形狀、地圖的點），原本的點留著但透明，停留的工具提示、點選、
  醒目提示與動作不變；圖片的邊長是標記的直徑（依「大小」的設定或大小上的欄位）
- **顏色**：色彩上有欄位（或標記卡指定了顏色）時，圖片不透明的部分整個換成那個顏色（透明的部分不變，與 Tableau
  把圖片當作遮罩著色相同）；沒有時是圖片原本的顏色
- **圖例**：形狀的圖例顯示圖片；色彩與形狀是同一個欄位時合成一個圖例（圖片帶顏色）。dashboard 的形狀圖例相同
- **編輯**：見「工作表編輯器」的「形狀」。`GET /api/edit/<報表>/shapes` 列出報表中的調色盤，`POST` 加入圖片

活頁簿中沒有內嵌的圖片（檔案被改過）依預設的形狀呈現，解析時記為略過。

## 格式

工作表與 dashboard 的格式與 Tableau 相同（`kernel/src/tmig/parser/formats.py`、`kernel/src/tmig/render/vegalite.py`、
檢視器的 `web/src/layout.ts`）：

- 窗格（小倍數）：與量值的座標軸同一架的離散維度把圖分成窗格，例如列為 Region、SUM(Sales)，欄為連續的
  月份時每個地區一張折線圖由上往下排；欄的維度與量值同一架時左右排列，散佈圖可以兩個方向都分。每個窗格只畫
  自己的資料（趨勢線、窗格範圍的參考線也是），共用刻度，列的窗格只有最下面一張、欄的窗格只有最左邊一張畫刻度，
  窗格的標題寫在左側與上方；欄的窗格依欄維度自己的排序排列。窗格超過 200 個時以單一窗格呈現
- 多邊形：依路徑欄位的順序（沒有路徑時依資料的順序）連接各點並填滿，詳細資料的維度區分各個多邊形；
  標記卡的顏色、不透明度與框線照樣套用（多邊形上的標籤尚未畫出）
- 座標軸（「編輯座標軸」與「顯示標題」）：隱藏的座標軸與 Tableau 相同保留格線；自訂標題（空的不顯示標題）；
  固定範圍、只固定起點或終點（數值或日期），範圍外的標記裁切；時間軸只固定一端時，另一端仍是自動的、延伸到整齊的刻度（`nice_time`，與 Vega 的時間刻度相同的規則）；反轉；自動範圍不必包含 0；同步的雙軸共用刻度。
  Measure Values 的座標軸也適用
- 格線、座標軸線與刻度依列（縱軸）與欄（橫軸）分別設定；活頁簿的格式（Format > Workbook）是每張工作表的預設
- 離散欄位：不顯示標題的層不畫出（多層的標題只顯示其他層，位置仍依每一層區分）、欄位標籤可以關閉、
  標題成員的文字顏色
- 列標題：每一層一欄（拖曳調整過寬度的依設定，否則依成員與欄位標籤的內容估計、最寬 200 像素，較長的成員
  截斷），成員靠左（或依設定的 `text-align`：置中、靠右），外層的成員只寫在各組的第一列，欄位標籤橫寫在各欄
  上方（可以設定背景顏色）；離散欄位的標題（列與欄）與 Tableau 相同沒有刻度
- 文字表的儲存格：與 Tableau 相同數字靠右、文字靠左，對齊各欄的邊緣；Format > 窗格設定的對齊（寫在標記卡的
  `<pane>` 中）優先；只有一格、沒有標題（KPI）時沒有設定就置中。連續座標軸上的文字（例如依日期擺放的註記）
  置中在標記的位置。文字表的欄依內容的寬度，標準大小時不拉寬填滿區塊；只有列標題的文字表是一欄
- 工作表的背景（可以透明，畫在 dashboard 的背景圖上）與文字的顏色：座標軸、圖例、標題、標籤與文字表
  （工作表沒有設定時依活頁簿的預設）
- dashboard 的背景顏色；區塊的外距（框線之外）、框線、背景與內距（框線之內）。沒有寫外距的區塊沒有外距
  （Tableau 為並排的區塊寫出 4，浮動的區塊與容器通常不寫）；容器只在有框線或背景時畫出，畫在子區塊之下
- dashboard 上的工作表與 Tableau 相同不畫圖例（圖例是另外的區塊）；不顯示標題的工作表，列數與資料的切換
  只在滑過時顯示
- 顯示大小（工具列的「調整」，工作表視窗與 dashboard 視窗中各工作表的 `<viewpoint><zoom>`）：
  - 標準：離散的列每一列 20 像素（與 Tableau 相同，以 Logshark 報表的 Tableau 畫面量測），不拉長填滿區塊，
    放不下時在區塊內捲動；離散的欄依內容估計寬度，文字表固定這個寬度，其他圖在區塊放得下時填滿、放不下時
    固定寬度並左右捲動；連續的座標軸填滿區塊
  - 符合寬度、符合高度：該方向填滿區塊，另一個方向與標準相同；整個檢視：兩個方向都填滿
  - Format > Cell Size 或拖曳調整過的列高、欄寬（整張表或某個離散欄位）與連續座標軸的大小優先；
    列標題的寬度、欄標題與座標軸的高度固定為設定的大小，較長的成員截斷
  - 文字表與醒目提示表的每一格有多行文字時（標籤上有多個欄位、每個欄位一行；自訂的標籤文字中有換行；
    值本身有換行），列高加高到放得下：每行的行高是字級加 2 像素（各行格式不同時是該行字級的 1.2 倍），
    另外留半個字級的空白——預設的字（9pt）兩行 34 像素、三行 48 像素，只有一行時照舊 20 像素；整段文字
    以列的中線為中心（與列標題對齊）。設定的列高放得下時照設定，放不下時加大到放得下（寫進畫圖的附註，
    編輯器列得出來）。符合高度與整個檢視時列照舊填滿區塊（區塊太矮時見下面「符合高度、整個檢視時區塊太矮」）
  - 只有一行、字卻大到放不下預設列高的文字（一行占的高度——字級的 1.2 倍——超過 20 像素，13pt 起）：沒有設定
    列高時列跟著變高，算法與多行相同（一行的高度加上半個字級的空白：13pt 28 像素、14pt 30 像素、20pt
    42 像素）；9–12pt 的字照舊是 20 像素。設定了列高的照設定，不加大也不寫附註
  - 圓點與形狀的表格（每格一個標記）中的標籤：多行的（標籤上的多個欄位、標籤文字或值裡的換行）列高與文字表
    相同跟著變高，在標記左右或中央的標籤整段以標記的中線為中心；在標記上方或下方的標籤（一行或多行）整段留在
    自己的那一列裡（列高的一半放得下標記的半徑、4 像素的間距與整段文字：一行 46、兩行 74 像素），上方的標籤
    依實際的行數往上移、不蓋到標記。只有一行時設定了列高的照設定（與文字表的大字相同）。圖片形狀（大小相對於
    儲存格）的表格與直寫的標籤不處理列高
  - 離散的欄依內容估計寬度時，文字表每一格的文字依各行自己的字級估計、取最寬的一行（同一行內格式不同的各段
    相加）：各行格式不同的自訂文字不再是最小的 40 像素，大字的欄也夠寬。預設的字（9pt）與原本相同；醒目提示表
    每一格的文字也一樣。圓點與形狀的表格在欄架上有維度時，每一欄放得下標記與它的標籤（與只有列時的寬度相同：
    標記 10 像素、左右的空白與標籤的寬度），預設的字也因此比只依欄標題估計時寬（示範報表的 Sub-Category ×
    Region：62 → 88 像素）。欄標題的成員仍一律當成 12 像素的字估計
  - 沒有離散的列的檢視（KPI、只有欄的文字表）中，同一種格式的多行文字整段以檢視的中線為中心（原本第一行在
    中線、其餘往下排），與各行格式不同的文字一致
  - 符合高度、整個檢視時區塊太矮：列平分區塊的高度，矮到相鄰兩列的文字互相蓋住時（畫好之後量文字標記的
    外框：不同列、左右有交集的兩格上下重疊超過 1 像素，而且列已經比標準的列高小）不再往下壓——列高改用標準的
    大小，放不下的在區塊內上下捲動，欄標題固定，與標準大小時相同；區塊的高度改變時重新決定（變高到放得下就
    回到填滿）。文字沒有互相蓋住的照舊填滿，包括只是被壓得比標準的列高小一點的。只看文字表各格的文字
    （文字標記）：長條上的標記標籤重疊時照舊是隱藏重疊的標籤。檢視器、dashboard 的區塊與編輯器都一樣
  - 寬度填滿區塊而上下捲動時（符合寬度；整個檢視時列高保持）：捲軸占寬度的環境裡（Windows 上的瀏覽器、
    桌面版的視窗）捲軸在圖畫好之後才出現，圖照扣掉捲軸之後的寬度重排一次，最右邊的字不被捲軸蓋住
- 色帶（網底的列色帶、欄色帶）：隔 N 列（欄）畫上窗格的顏色，多層的列標題或列標題的寬度有設定時也畫上
  標題的顏色
- 分隔線（框線的列分隔線、欄分隔線）：窗格（`table-div`）與標題（`header-div`）分開設定，畫在前 N 層標題的
  成員改變處，顏色、線寬與樣式（`line-pattern-only`：實線、虛線、點線，窗格與標題各自設定）依設定。沒有寫時
  與 Tableau 相同都畫（淺灰、1 像素、最內層以外的每一層之間，只有一層時不畫）。多層的列標題中，線從改變的那一層所在的欄畫起，外層沒有改變的欄不切開；長條在分隔線與色帶上維持
  原本的粗細
- 框線：`element='cell'`（儲存格）與 `'pane'`（窗格）的 `border-style`（none、solid、dashed、dotted）、
  `border-width`、`border-color`；樣式是 none、沒有寫樣式、寬度 0 或透明時沒有框線。與分隔線一樣以座標軸的格線
  畫在各列（欄）的邊界，外框是檢視的邊框（Vega-Lite 的 `config.view`）
- 線條：格線、座標軸線與刻度的顏色與 Tableau 的預設相同（格線 #f0f0f0、座標軸線與刻度 #f4f4f4，依畫面量測）；
  格線（`element='gridline'`）與座標軸線（沒有 field 的 `element='axis'`）的 `stroke-color`、`stroke-size`、
  `line-pattern-only` 是它們的顏色、線寬與樣式，依 scope 區分列與欄；
  零線沒有設定時與 Tableau 相同，在範圍包含 0 的連續座標軸上畫 1 像素的灰色點線（#cbcbcb），可以關閉或改成
  實線、虛線與其他顏色（`element='zeroline'`）
- 標記標籤「與標記顏色相符」（`datalabel` 的 `color-mode='match'`）：依欄位著色時與標記同色
- 標記標籤（標記卡的「標籤」，`mark-labels-*`）：顯示標記標籤的開關（關閉時不畫；開啟但標籤上沒有欄位時
  與 Tableau 相同標示數值座標軸上的量值，圓餅為角度的量值、地圖為地名）；只標示最小值／最大值（範圍為窗格、
  儲存格、整個表格或每條線，可以只標其中一個）、線條的起點／終點、選取（醒目提示）的標記。互相重疊的標籤
  與 Tableau 相同只顯示先畫的（檢視器依畫好的位置判斷；允許重疊時全部顯示），堆疊長條各段內放不下的標籤
  不顯示（HR Dashboards 的 Departments 與 Tableau 的畫面相同）。位置依 Tableau：圓點、形狀與方塊在右側
  （對齊設為置中時在標記中央），折線的最大值在點的右上、最小值在右下。自訂標籤文字中各段的字級、粗細與
  顏色照樣套用，各行格式不同時（KPI 的小標題與大數字）分行畫出；依欄位設定的標記文字字型與顏色（Format >
  欄位的窗格）用於這個欄位的值。多行的標籤（標籤上的多個欄位、標籤文字或值裡的換行）整段放在單行時的位置：
  橫的長條、置中的長條、標記左右與折線點左右的標籤整段以定位點為中心；直的長條上端、折線與區域的點上方與
  最大值的標籤整段往上移（最後一行在單行時那一行的位置，不蓋到長條或點）；點的下方、最小值與堆疊長條各段
  內側的從定位點往下排。直寫的標籤不調整
- 工作表標題的背景（`element='title'` 的 `background-color`）
- 多層的欄標題每一層一行（文字表在上方、其他圖在下方），外層的成員與 Tableau 相同合併成一格、寫在各組的中間；
  標準大小捲動時，欄標題、欄位標籤與座標軸固定在區塊的上下緣、列標題固定在左緣（檢視器以圖的複本裁出這些區域）
- 標題成員的垂直對齊（`vertical-align`）：外層的成員置中時寫在各組的中間（偶數列的組在中間兩列之間）、
  置底時寫在最後一列；文字方向（`text-orientation`，例如 -90 由下往上）直寫，欄寬為一行字
- 色帶的層級（`band-level`）：1 是每一列（欄），每加 1 往外一層標題，等於標題的層數時以最外層標題的組為單位隔組上色；欄標題區域也畫分隔線（以刻度畫在各組之間）
- 字型（Format > Font）：沒有設定時與 Tableau 相同，刻度、標題成員、座標軸標題、欄位標籤、圖例與工具提示都是
  9pt（12 像素），工作表標題 15pt（20 像素）；刻度與標題成員為灰色（#555555），座標軸標題、欄位標籤與標題較深
  （#333333），欄位標籤半粗體（依 Logshark 報表文件中 Tableau 的畫面量測）。活頁簿的預設字型（`all`）與
  工作表、標題成員與刻度（`label`，可依欄位）、文字表（`cell`）、欄位標籤、圖例、標題的字型、字級、粗細與斜體
  照設定，標記標籤依標記卡的 `datalabel`；工作表標題另依標題文字本身的格式。Tableau 隨附的字型（Tableau Book、
  Tableau Semibold 等）在裝了 Tableau Desktop 的電腦上照用，沒有時依序改用 Benton Sans、Segoe UI、Arial；報表指定的其他字型（例如 Roboto）不在電腦上時也依這個順序，不用瀏覽器預設的 sans-serif
- 標記的大小（標記卡的「大小」滑桿，`<pane>` 中 `element='mark'` 的 `size`）：依 Logshark 報表文件中
  Tableau 的畫面量測——離散的列（欄）上的長條沒有設定時占列高（欄寬）的 75%（20 像素的列中 15 像素，放大的區塊
  中也是 75%），設定了大小 s 時為 s / 2（大小 1 為一半、2 填滿；四張圖的量測與這個比例相差 1% 以內）；
  圓點、形狀與方塊的直徑為 10 × s 像素（預設 s = 1）；折線寬 2 × s 像素（HR Dashboards 的啞鈴圖，大小
  14.55 的線量得約 30 像素），兩端與轉折是圓的。依欄位決定大小時不變。
  圓餅的直徑為區塊（分面的每一格）較短邊的 s / 2（大小 1.505 的圓餅量得約 76%），標籤寫在外側；甜甜圈的洞
  依洞的標記卡的大小（Snow Sports 的庫存甜甜圈因此與 Tableau 的粗細相同）
- 折線與 Tableau 相同不跨窗格：沿著最內層的離散維度連接，外層的離散維度（離散的年、月中的年）分開各條線；
  有路徑時依路徑連接，架上的維度都分開（啞鈴圖每一列一條線）

## 資料源分頁

報表的切換清單中「資料源：…」開啟資料源分頁（網址 `/w/<報表>/ds/<資料源>`；
`kernel/src/tmig/render/datasource.py`、`tmig.sqlgen.query.preview_sql`、檢視器的 `web/src/DatasourceView.tsx`）：

- 連線：萃取檔與資料表（萃取的來源資料庫與更新方式）、Live 連線的類型、伺服器、資料庫、結構描述與驗證方式；
  不含帳號與密碼（報表定義中本來就沒有）
- 資料表：單一資料表、join（類型與條件）、自訂 SQL 的原文；Relationship 模型的邏輯資料表與關聯的條件
- 資料源篩選與萃取篩選的說明
- 資料：前 100 或 1,000 列原始資料與列層級的計算欄位（含 LOD、彙總、Table Calculation 的計算欄位與隱藏的欄位
  不列出），萃取另外顯示篩選後的總列數；Relationship 模型一次預覽一張邏輯資料表（點選資料表切換）
- 欄位：名稱、類型（欄位、計算欄位、集合）、資料類型、角色、所在的資料表、實體欄位或公式（欄位與參數以顯示
  名稱呈現，與 Tableau 的公式編輯器相同）、預設彙總與格式
- 權限：資料源分頁與完整的報表定義相同要「下載 workbook」（ExportXml）；資料預覽另外要「檢視基礎資料」
  （ViewUnderlyingData），並以登入者的身分套用資料源篩選與列層級權限（看不到的列不會出現）。沒有權限時清單中
  不出現資料源

## 已知限制與待驗證

下列 Tableau 語意依文件與公開範例 .twb 推定，示範報表的獨立計算也採用同樣的理解，
所以只有用真實報表、以 Tableau 匯出的結果比對，才能確認：

- **Table Calculation 的計算方向**：`ordering-type='Columns'` 視為 Table (down)、`'Rows'` 視為 Table (across)、
  `Table`／`TableCol` 為表格（橫向再縱向／縱向再橫向）、`RowInPane`／`ColumnInPane` 為窗格（橫向／縱向）、
  `Pane`／`PaneCol` 為窗格（橫向再縱向／縱向再橫向）、`CellInPane` 為儲存格；標記卡上的維度是分割，
  計算方向的架上沒有維度時才定址它們（100% 堆疊長條的 Table (down)），圓餅（甜甜圈）標記卡上的計算則
  一併定址標記卡上的維度（已與公開報表的 Tableau 畫面比對：HR Dashboards 的 Departments 中 INDEX() 依部門
  編號、色彩上的狀態各自從 1 開始；HR Analytics 的甜甜圈各塊的百分比合計 100%）；
  窗格的方向只定址窗格內變化的維度（架上最後是離散維度時為它，最後是連續的座標軸時為結尾連續的維度），
  儲存格只定址標記卡上的維度；未寫計算方向時依預設 Table (across)，IR 標記為推定（`inferred`）。
  指定維度時，寫欄位名稱的定址這個欄位在檢視中的所有實例，寫欄位實例（例如 `[yr:Order Date:ok]`）的只定址
  這個實例；「計算層級」（`level-address`）把定址中更內層的維度改為分割（年增率：每一季各自與前一年比），
  「重新開始間隔」（`level-break`）把它與它之前的維度改為分割（年初至今）
- **其他快速 Table Calculation**：百分位數（`PctRank`）為 `RANK_PERCENTILE`，與 Tableau 文件的例子相同
  （6、9、9、14 遞增為 0、0.67、0.67、1，Null 不計入）；百分比（`PctValue`，Percent From）為
  `ZN(x) / LOOKUP(ZN(x), …)`（依差異百分比的寫法推定）；差異類的比較對象 `diff-options='Fixed'` 時，address 為
  `%first%`／`%last%`（或沒有寫）視為第一個／最後一個、其他值視為定址欄位的成員（公開報表中看到 2017、2019 等
  年份），寫法依公開資料推定；複合成長率（`Compounded`）為 `POWER(ZN(x) / 比較對象, ZN(1 / 相隔的標記數)) - 1`
  （第一個為 0）；比較對象也可以由參數決定（`Parameter`：相對位置或成員，查詢時代入參數目前的值）
- **巢狀 Table Calculation**：視窗函式不能巢狀，內層（被參照的 Table Calculation 計算欄位，或寫在另一個
  Table Calculation 函式參數中的函式）先算成欄位，外層再引用。被參照的計算欄位依欄位實例上
  `<table-calc field=…>` 的巢狀計算方向計算，沒有這項設定時與外層相同（依公開資料推定）；
  同一個公式中直接巢狀的函式依同一個計算方向。HR 人資報表「Education & Performance」的醒目標示
  （巢狀計算方向與外層不同）與 Tableau 截圖相同
- **LOD 的彙總**：彙總參數只含 LOD 時，在「檢視維度 ∪ LOD 維度」的組合上去重後彙總；含列層級欄位時逐列彙總
- **巢狀 LOD**：內層的 INCLUDE／EXCLUDE 相對於外層 LOD 的維度（不是檢視的維度）；外層的彙總中只含內層的值時，
  與檢視彙總 LOD 相同在「外層維度 ∪ 內層維度」的組合上去重後彙總。自己或外層有 FIXED 的在維度篩選前計算，
  其餘在維度篩選後計算；內層先併回每一列，外層再以併好的資料計算。內層 FIXED 的維度與外層無關時
  （例如 `{FIXED [Country] : MAX({FIXED [Order ID] : …})}`），每個外層成員取它出現過的內層成員，
  與公開文章描述的 Tableau 結果相同（依公開資料推定，待以真實報表確認）。LOD 的維度本身含 LOD 尚不支援
- **Top N**：同名次時取維度值較小者，數量剛好 N；內容 Top N 以其他內容篩選後的資料排名。
  Top N 篩選的篩選控制項與 Tableau 相同，選取的成員是排名前的選取（篩選對話框的「一般」頁），再取前 N 名
- **色彩上有多個欄位**：與 Tableau 相同以欄位值的組合著色（`Marks.color_more`；其他的維度同時是詳細資料，
  詳細程度與數字相同），圖例的成員是各欄位的值以「, 」連接；各組合的顏色依色盤的順序（.twb 中組合的顏色寫法
  沒有樣本，指定過顏色的報表顏色可能不同）。Measure Names 略過（記在略過的元素）
- **篩選到最新的值**（日期篩選的「開啟活頁簿時篩選到最新的日期值」，`<preset type='last-values'>`）：開啟報表時
  以欄位目前的最大值取代存檔時的選取（與動態參數相同，以資料源篩選與列層級權限後的資料計算，一分鐘內快取）
- **變更資料類型**（`datatype-customized`）：萃取與來源資料表中仍是原本的型別，查詢時以 Tableau 的型別轉換函式
  轉換（數字轉日期為 1899-12-30 起算的天數、字串轉數字無法轉換時為 Null）；以隱藏的原始欄位加上轉換的計算欄位表示
- **正規表示式的前後查看等**（RE2 不支援、ICU 支援的語法）：DuckDB 以 Python 的 re 計算（寫法與 ICU 相同）；
  Live 連線轉成 PostgreSQL、MySQL 8、Spark／Databricks、Trino／Athena、Hive、Redshift 的正規表示式，其他資料庫不支援
- **直接以 .hyper 為來源**、有 join、多個 .hyper 檔或 Relationship 模型的資料源（Logshark 的報表）：與 Excel、
  文字檔相同匯入成 Files.<資料表>，join 在本機執行；join 條件可以是計算（join 計算）
- **叢集**：k-means（最小–最大正規化、分割法的初始中心、Lloyd 演算法、自動時以 Calinski-Harabasz 指標的第一個
  局部最大值決定叢集數），依 Tableau 文件實作；Tableau 的編號規則與細節沒有公開，分群的結果可能不同
- **預測**：指數平滑（趨勢、季節各為無、加法或乘法，以 AIC 選模型；季節長度依月 12、季 4、週 13、日 7、時 24），
  忽略最後幾期、預測長度、信賴水準與預測區間依設定；Tableau 的初始值與最佳化細節沒有公開，估計值可能有些微差異
- **預測結果**（https://help.tableau.com/v2021.2/pro/desktop/en-us/forecast_field_results.htm ，2026-11-22 查閱）：
  .twb 的 `forecast-column-type` 只看過 `forecast-value`、`forecast-indicator`，其他結果類型的名稱（`forecast-trend`、
  `forecast-precision`、`forecast-quality`、`forecast-upper-bound` 等）是推定的，不認得的列為不支援；實際值的列在
  趨勢、精度等結果類型上是 Null（Tableau 的文件只說明預測的列）；乘法模型的預測區間以模擬計算，上下不對稱時精度
  取區間寬度的一半；精度 % 以百分比、品質以整數顯示，標題寫成「Sales（趨勢）」，都是推定的；量值選單沒有
  「指示器」（用資料窗格的 Forecast Indicator）。預測指定的彙總依據（`auto-forecast-agg='false'`）尚不支援
- **描述預測的摘要**（https://help.tableau.com/v2021.2/pro/desktop/en-us/forecast_describe.htm ，2026-11-22 查閱）：
  初始是第一個估計的期（含忽略的最後幾期）；季節影響取實際值與估計值合起來的最後一個季節長度；貢獻是估計的
  各期中去掉季節的部分與季節成分各自的變異數占比（Tableau 沒有公開算法）；「以百分比顯示」時的變化百分比沒有做；
  都還沒有與 Tableau 的數字對過
- **RAWSQL_***：只在 Live 連線、而且整段下推到來源資料庫的部分；參數不是欄位或常值時加上括號、`%n` 在 SQL 原文中
  一律代換（包括字串常值中）、整段 SQL 加上括號，是推定的；RAWSQL_BOOL 在 Oracle、SQL Server、Teradata、Db2、
  SAP HANA 不支援（SELECT 中沒有布林值）；參數是彙總、Relationship 模型中多張資料表的 RAWSQLAGG 不支援。
  PostgreSQL、MySQL 的實跑測試在沒有引擎的 Windows 上略過，還沒跑過
- **樹狀圖、泡泡圖、文字雲**（欄列架上沒有欄位時的排列）：樹狀圖以 squarified 演算法依工作表在 dashboard 上的
  區塊寬高比排列（不在 dashboard 上時以 960×600），色彩是維度時先依顏色分區；泡泡圖以前緣鏈演算法排列、同色的
  泡泡排在一起；文字雲把字緊靠排成寬高約 1.6 的一團。標籤依預期的大小換行，放不下時不顯示；Tableau 的排列細節
  沒有公開，位置可能不同（面積、大小與數字相同），也沒有大小的圖例
- **沒有指定顏色的成員**：依畫面中的成員排序後依序取色盤的顏色（不隨工作表的排序改變）；Tableau 依欄位的所有
  成員排序，篩選掉部分成員時顏色可能不同（公開報表多半已在 .twb 中存下各成員的顏色，依存下的顏色）
- **地圖**：底圖由平台內建的公開地理資料畫出（陸地、水域、國界、州界與地名），沒有 Tableau 底圖的街道、地形、
  衛星影像與美國人口普查的資料圖層，「街道」「戶外」「衛星」樣式以相近的配色呈現；郵遞區號沒有區域形狀（平台的
  資料只有中心點），填色地圖以中心點的圓點呈現；美國以外沒有郡的資料。同名而範圍不足以區分的地名（例如沒有州的
  Springfield）列為未知，與 Tableau 的「不明確」相同。自訂的 WMS、Mapbox 背景地圖尚不支援（以內建的底圖
  呈現）；依色彩分組的密度圖各成員各自計算密度再疊在一起（Tableau 的混色方式未公開，重疊處的顏色可能不同）；
  地圖上的趨勢線是緯度對經度的配適，沒有信賴區間的帶狀。
  標籤重疊的判斷依工作表在 dashboard 上的大小估計。空間資料：基準面之間的轉換沒有下載格網檔（例如英國的 OSTN15），
  以七參數等計算，精度約數公尺（Tableau 的設定未公開）；BUFFER 只支援點（Tableau 2021.2 的範圍）；File Geodatabase
  的曲線（圓弧等）尚未支援；資料庫的空間欄位以 PostGIS 與 MySQL 實機驗證，SQL Server、Oracle、Snowflake 等只有轉譯
  快照；自訂 SQL 中的空間欄位只有 PostGIS、Redshift 認得（取回的值是含 SRID 的 EWKB），其他資料庫成為文字。萃取與檔案的
  空間 join 的 Intersects 以經緯度平面
  判斷，跨越 180 度經線的形狀可能不同
- **TOTAL()**：以量值篩選後仍在檢視中的標記所對應的底層資料重新彙總
- **週起始日**：.twb 沒寫時視為星期日
- **預期 CSV 的格式**：Tableau 匯出的欄名與數值格式依版本與語系而異；欄名用 mapping.yaml 對照，
  帶格式的數值以顯示精度比對
- **篩選動作**：以目標工作表動作篩選中的欄位篩選，但只用每個選取的標記都有的欄位
  （一個都沒有時視同沒有選取）；Measure Names 不傳給目標。exclude 參數列出的工作表與來源工作表本身不受動作篩選
  （它們身上這個動作的篩選維持儲存時的狀態）。跨資料源的動作依 `<link expression>`（`tsl:` 網址，
  `[目標資料源].[欄位]~s0=<[來源欄位]~na>`）對應目標與來源的欄位；沒有對應或來源工作表用到多個資料源時列為不支援。
  網址參數另成一個篩選，不會取代篩選動作；計算比對的篩選組合不含選取（Tableau 的匯出無法指定選取）
- **醒目提示動作**（Tableau 的 `tsc:brush`）：選取、滑過或從選單執行時，目標工作表中欄位值相同的標記醒目、
  其餘淡化（不重新查詢、不影響數字），清除選取或滑鼠移開時取消。比對所有欄位（`special-fields='all'`）或選定的欄位
  （`field-captions`，欄位的顯示名稱以逗號分隔；不同資料源依顯示名稱對應），目標為 target 的 dashboard 上的工作表
  扣掉 exclude，來源工作表本身也是目標；工作表上沒有相同欄位時不受影響。XML 依公開資料推定；「日期與時間」等其他
  `special-fields` 列為不支援
- **醒目提示控制項**（dashboard 的 `highlighter` 區塊）：輸入時名稱包含輸入文字的值醒目（不分大小寫），也可以從清單
  選一個值；只影響它的工作表
- **階層的切入**（`<drill-paths>`）：檢視器的工作表標題旁列出可以切入的階層，「⊞ 下一層」在欄、列架上加入下一層、
  「⊟ 目前的層」收合（也可以收合存檔時已經顯示的下層），日期部分（YEAR）加入下一層的日期部分、日期值（截斷到月）
  改成下一層的截斷。查詢與存檔時就把那些欄位放在架上的工作表相同；切入只影響目前的畫面（含下載的資料），
  不改報表。下一層已經在欄、列架上時不重複加入；Tableau 檢視中欄、列標題上的 ⊞／⊟ 改放在工作表的標題列
- **工具提示中的工作表**（Viz in Tooltip，工具提示文字中的 `<Sheet name="…" maxwidth maxheight filter>`）：滑過標記時
  工具提示中顯示目標工作表（不超過最大寬、高，不顯示標題與圖例），以標記的所有維度（`<All Fields>`）或列出的欄位
  （`<Region>,<MONTH(Order Date)>`，須在來源工作表的檢視上）篩選，不同資料源依欄位名稱對應；目標工作表自己的篩選、
  參數照常套用。Tableau 在目標工作表上自動產生的工具提示篩選（`ui-vit-column`）在直接檢視時依存檔的狀態，在工具提示中
  改以滑過的標記篩選。只能看部分 view 的使用者也可以看可以看的工作表的工具提示中的工作表（常是隱藏的工作表）。
  工具提示的其他文字（自訂的說明、欄位的排版）不重現，顯示標記的欄位與值；有多張標記卡時，各卡的工具提示中的工作表
  合在一起顯示。放 dashboard、Story 列為不支援，不存在的工作表（Tableau 不顯示）略過。點選標記釘住的工具提示
  （有命令按鈕時）也有工具提示中的工作表，以點選的那個標記篩選——觸控的裝置上沒有滑過，看得到的就是這一個
  （Tableau 的說明：「Hovering over or selecting the mark reveals data from another sheet」，
  https://help.tableau.com/v2021.2/pro/desktop/en-us/viz_in_tooltip.htm ，2026-11-22 查閱）；框選多個標記時沒有
  單一的標記，釘住的工具提示不顯示工作表（Tableau 這時怎麼顯示沒有寫，推定）
- **動態參數**（Tableau 2020.1 起，參數的 `default-value-field`、`source-field`）：開啟報表時（伺服器上每次開啟檢視時，
  同一份資料與使用者的結果保留一分鐘）以目前的資料取值——目前值取自欄位（欄位只有一個值時，多個值時維持存檔時的值，
  與 Tableau 無法選擇預設值相同）、允許值的清單為欄位的不重複值（由小到大）、範圍為最小值到最大值。以資料源篩選、
  萃取篩選與列層級權限後的資料計算，與工作表的篩選無關；計算比對（tmig verify）與 tmig query 也一樣。查詢失敗時維持
  存檔時的值。取自彙總的計算欄位、找不到或型別不相容的欄位列為不支援
- **裝置專屬版面**（`<devicelayouts>`）：檢視時依畫面較短的一邊選用——500 像素以下為手機、501 到 800 為平板、
  超過 800 為桌面（與 Tableau 相同；網址的 `:device=phone|tablet|desktop` 可以指定），手機沒有自己的版面時用平板的，
  都沒有時用預設版面。符合寬度（`sizing-mode='vscroll'`）時寬度隨畫面、高度固定可以上下捲動，座標以一般手機
  375 像素寬換算；符合畫面以一般裝置的大小（手機 375×667、平板 768×1024）換算；沒有寫大小時與預設版面相同。
  Tableau 自動產生的手機版面（`auto-generated`）照 .twb 中的區塊顯示。編輯器的畫面一律顯示正在編輯的版面
- **註解**（`<annotations>`：標記、點、區域）：標記的註解畫在那個標記的位置（以註解記下的維度值找標記，文字代入
  欄位值），點的註解畫在座標軸的值上，文字偏移（`<body x y>`）時畫一條線連到文字；區域的註解畫成方框、文字置中。
  只影響圖表，不影響數字。只畫在第一列資料所在的窗格；多個量值、圓餅等圖表上尚未畫出，頁面架上的註解在每一頁都
  顯示（Tableau 只在那一頁），字型與方框的樣式用預設（以公開報表的區域註解驗證 XML 結構）
- **會計年度**：資料源（`<date-options fiscal-year-start='april'>`，已發布資料源的複本在 `<overridable-settings>`）
  或日期欄位（`<column fiscal-year-start='4'>`，0 為沿用資料源）的起始月不是一月時，與 Tableau 說明文件「會計日期」
  相同：年、季（部分與截斷）與週數依會計年度計算，月、日與週的截斷不變；會計年度以結束的年份命名（四月起算時
  2004 年 6 月 1 日是 FY 2005 的 Q1、2004 年 4 月 1 日是第 1 週），年的截斷是會計年度的第一天（五月起算時是
  2004 年 5 月 1 日），季的截斷是會計季的第一天（七月起算時是 2004 年 4 月 1 日）。年與季的標題加上 FY（「FY 2005」、
  季的截斷「FY 2005 Q1」，季的截斷的寫法依推定）。日期函式（DATEPART 等）、相對日期篩選與連續的日期軸仍依日曆年
  （說明文件只說明檢視中的日期層級）
- **網址動作與內嵌網頁**：網址中的欄位代入原始值（數字取 12 位有效數字）。開啟的位置與 Tableau 的「URL Target」
  相同（`<link>` 之下的 `<url-action-type>`、`<url-action-target>`，寫法取自 Tableau 公開的 .twb 結構描述）：
  預設（沒有寫或 `default-zone-or-browser`）時 dashboard 上有網頁物件就載入其中（第一個），沒有時在新分頁開啟；
  `browser` 一律在新分頁；`specific-zone` 載入指定的網頁物件（`url-action-target` 視為區塊 id，待真實報表確認；
  找不到時改回預設）。網頁物件可以沒有網址（空白，等動作載入）；選取、暫留與功能表三種執行方式都可以載入網頁
  物件（暫留時不開新分頁），切換 dashboard 後回到原本的網址。內嵌的網頁在沙箱中（只接受 http、https），
  對方的網站若禁止被內嵌（X-Frame-Options、CSP frame-ancestors）會是空白，這一點與 Tableau 相同。
  編輯器：dashboard 物件「網頁」的網址可以留空白；動作對話框的網址動作可以設定網址目標與多個值的分隔字元。
- **參數動作**：參數動作的 XML（source-field、target-parameter）依公開資料推定，選取多個標記時依彙總方式計算
- **集合**：Top N 與條件集合以內容篩選後、維度篩選前的資料決定成員（與 Top N 篩選相同，同額時取維度值較小者），
  所以網址參數與一般篩選不改變成員；用在資料源篩選、內容篩選或 FIXED LOD 中的 Top N、條件集合列為不支援。
  IN/OUT 依 In、Out 的順序排列（依公開資料推定）；Tableau 匯出資料時 IN/OUT 欄的欄名寫法未確認，
  計算比對的對照接受「In/Out of 集合」「IN/OUT(集合)」與集合名稱。Oracle 沒有布林值，Live 連線的 IN/OUT
  尚不支援下推
- **集合動作**：選取標記時以標記在集合各層級的值取代、加入或移除固定成員集合的成員，用到這個集合的工作表
  隨之改變（成員由檢視器送給查詢服務，只影響這次查詢；值的型別與格式都會檢查）。XML 依公開資料推定：
  取代、加入、移除寫在 add-or-remove-marks（功能旗標 GroupActionAddRemove），清除選取時的行為寫在
  selection-clear-set-option（show-all 推定為「加入所有值」，沒寫時保留成員）；目標是 Top N、條件集合，
  或來源工作表在另一個資料源的集合動作列為不支援
- **篩選中連續的成員**：在篩選清單中連續勾選時，Tableau 記成一段成員（range，from–to 含頭尾），新平台以值的
  大小比較（萃取的字串依 DuckDB 的二進位順序，Live 連線依資料庫的定序）；Tableau 依清單中成員的順序決定一段的
  範圍，大小寫混合或含符號的字串可能不同（依公開資料推定）。篩選控制項的勾選以同樣的方式顯示；改了篩選之後
  以勾選的值取代
- **篩選動作的舊參照**：動作刪掉重建後，工作表上留下參照舊動作的篩選（公開報表 Practice Activity Number 4），
  Tableau 照樣套用它，之後執行的動作寫入同一個欄位、取代它。新平台把它連到唯一會寫入這張工作表的篩選動作
  （目標 dashboard 上扣掉 exclude 與來源的工作表；沒有或有多個時維持靜態篩選），依 XML 結構推定
- **Story**：故事點顯示的狀態取自擷取時記下的差異（`<capturedDeltas>`）；擷取後又改過、沒有按「更新」時取改過的
  狀態（`<currentDeltas>`），推定開啟報表時看到的是它。篩選動作的篩選轉成以記下的標記執行動作（同一個動作在
  每張目標工作表各記一次，值相同）、一般篩選取代同一欄位的篩選（與網址參數相同）、選取轉成醒目顯示的標記。
  在故事點上的操作只影響目前的畫面，切換故事點即回到記下的狀態（Tableau 會保留改過的狀態並提供「還原」）。
  導覽樣式（說明文字、序號、圓點）與箭頭照 .twb；Story 的尺寸不強制，故事點的 dashboard 以自己的尺寸顯示。
  故事點記下的參數值（`<datasource name='Parameters'>`）取代目前的值，改過的 dashboard 文字物件
  （`<dashboard><zone>`）取代區塊原本的文字；記下的醒目提示只影響醒目提示，略過

- **群組與數值分組**：群組沒有「其他」時，未列入的成員維持原值；數值分組的值為所在區間的下限
  （FLOOR((值 − 起點) ÷ 大小) × 大小 ＋ 起點），與 Tableau 的區間邊界一致與否待真實報表確認

- **相對日期**：基準日為 cases.yaml 的 `today`，沒有時為抓取預期結果的日期（`fetched_at`），檢視器與計算比對相同；
  期間一律取完整的期間（例如最近 3 個月含本月全月），`include-future='false'` 時只到基準日
- **條件篩選與 Top N**：彙總條件（例如總銷售額大於 5000）與 Top N 一樣，以內容篩選後、其他維度篩選
  （含網址參數）之前的資料計算；先選取的成員（篩選對話框的「一般」頁）中再取 Top N
- **差異與差異百分比**：與 Tableau 轉成計算欄位時相同，`ZN(x) − LOOKUP(ZN(x), −1)`，百分比再除以
  `ABS(LOOKUP(ZN(x), −1))`；與第一個、最後一個或定址欄位的某個成員比較見上方「其他快速 Table Calculation」
- **移動計算**：`from`、`to` 視為相對於目前標記的位置（含兩端），例如 −2、0 為前兩個與目前的值，
  與 `WINDOW_AVG(x, −2, 0)` 相同；視窗超出分割範圍時只取現有的標記，「不足時為 Null」則為 Null。
  未勾選「目前的值」時 XML 的寫法待真實報表確認
- **dashboard 按鈕**：切換按鈕依 `<windows>` 的 simple-id 找目標（目標可以是 dashboard、工作表或 Story；Story 的
  視窗類別也是 dashboard，以名稱對應）；切換動作的目標也可以是 Story；顯示／隱藏按鈕的第一個外觀視為區塊顯示中、
  第二個為隱藏時（待真實報表確認）；匯出 PDF 以瀏覽器列印，匯出圖片與 PowerPoint 由檢視器把畫面上的 dashboard
  畫成 PNG（PowerPoint 是一張 16:9 的投影片放這張圖），桌面版以存檔對話框問位置；檢視器的工具列另有「匯出」
  （dashboard、工作表、Story 的圖片、PowerPoint 與 PDF）；網頁區塊只嵌入 http(s) 網址
- **各標記卡的詳細程度不同**（例如甜甜圈、疊在長條上依客群分色的折線）：資料表與計算比對以工作表上所有維度的
  組合計算（與只有一個標記卡時相同）；圖表中較粗的標記卡（例如甜甜圈中間的合計）各自以自己的維度查詢。
  較粗的標記卡與地圖各標記圖層的標記也可以選取（資料列的 `__row` 接在工作表的資料之後）：選取的標記只帶著
  那張標記卡有的維度，其他標記卡中這些維度的值相同的標記一起醒目顯示，沒有這些維度的標記淡化
  Tableau 匯出這類工作表的格式待真實報表確認；分面的甜甜圈（例如依性別的兩個甜甜圈）每一格各自畫出外側的
  標籤與中間的合計；角度上沒有欄位的圓餅各塊一樣大
- **參考線**：值在新平台以圖上標記的值計算（平均、總和、最小、最大、中位數、百分位數以線性內插、
  平均 ± 標準差的倍數）；窗格為欄列架上的離散維度，但各架最內層若是離散維度則不算（它切出的是儲存格）。
  合計（Total）只在量值為 SUM、COUNT、MIN、MAX 時畫出；同一條參考線有多個範圍時不顯示標籤。
  參考區間（兩條以 `paired-id` 配對的參考線）之間塗上底色；分布（百分位數、標準差、分位數、平均值與中位數的
  信賴區間、彙總值的百分比）的各值之間由內往外塗上由淺到深的灰色，`fill-above`／`fill-below` 延伸到窗格的邊緣，
  底色畫在標記之下、線畫在標記之上。預設的格式依 Logshark 報表文件中 Tableau 的截圖量測（參考線為 2 像素的
  灰色實線、標籤寫在窗格左側線的上方；分布不畫線，標準差 2 到 3 之間 #f6f6f6、以上 #dddddd），
  工作表上依參考線 id 設定的線條顏色、粗細、樣式、底色與標籤的數字格式照樣套用；參考區間的預設底色、
  分布的配色（`palette`）與中位數信賴區間的算法（以二項分配取對稱的順序統計量）依公開資料推定。
  盒鬚圖每個範圍以線性內插的四分位數畫出兩種灰色的盒子（中位數上下），鬚延伸到 1.5 倍四分位距內最遠的值
  （或最小、最大值），可以隱藏鬚以內的標記；盒子的顏色與寬度依公開資料推定。
  同一架有多個量值（各自的座標軸）或雙軸時，每條參考線畫在它的量值所在的座標軸上（例如雙軸的折線上的平均線），
  同一條線只畫一次；圓餅等沒有座標軸的圖表上的參考線尚未畫出
- **趨勢線**：與 Tableau 相同依窗格與顏色（「允許依色彩」）分組，以轉換後的最小平方法配適五種模型（線性、對數、
  指數、乘冪、2–8 次多項式；對數與乘冪不納入 X ≤ 0、指數與乘冪不納入 Y ≤ 0 的標記），可以通過原點、排除分組的因子；
  線畫在各組資料的範圍內、顏色與標記相同（或依格式設定），信賴區間為平均值的 95% 信賴區間（上下兩條虛線，
  指數模型沒有），停在線上顯示公式、R 平方與 P 值（F 檢定），數字以 numpy、scipy 核對。日期以 1899-12-30 起的
  天數計算（只影響對數、乘冪模型與公式中的係數，依公開資料推定），全部為負的值以指數模型配適時取 −Y（推定）；
  信賴區間的線型（虛線）、工具提示的寫法依公開資料推定。離散的座標軸上（例如離散的月份）、圓餅不畫；
  選取標記時重新計算的趨勢線（`enable-instant-analytics`）尚不支援
- **總計**：列架的總計（`<rows total='true'>`）是最下方的一列、欄架的總計是最右邊的一欄，兩者都有時加上右下角；
  與 Tableau 的「自動」相同，另外查詢去掉該架上離散維度的詳細程度（標記卡上的維度保留、篩選照樣套用、
  INCLUDE／EXCLUDE 相對於總計的詳細程度），不是把畫面上的值相加。依量值著色時總計不上色、不影響色階範圍，
  色彩設定「包含總計」時才上色；總計不能點選、不畫參考線。資料表的最後幾列是總計；計算比對只比工作表的標記，
  總計的數字以公開報表的截圖確認。總計的計算方式改成畫面上的值再彙總（Total Using：`visual-totals` 的 Sum、Avg、
  Min、Max）時，總計與小計為範圍內各標記的值再彙總；總計中的 Table Calculation 套用到總計的值（在去掉維度後的
  詳細程度計算，例如合計百分比的總計列為 100%，依 Tableau 文件推定）；與量值篩選並用時與 Tableau 相同只合計
  通過篩選的標記（以工作表結果中標記的維度組合限定底層資料），Table Calculation 篩選只隱藏標記、不影響總計。
  小計（`<subtotals>` 列出的維度）與總計相同，在去掉同一架上之後的維度的詳細程度重新彙總，標題為 Total，
  緊接在各組之後（兩層小計時較內層的在前）；列與欄的小計、總計交叉處也有值。同一架上最內層的維度與不在架上的維度
  沒有小計（Tableau 的設定留著，畫面上沒有）。資料表的順序與圖表相同。Retail Analysis 的 Sheet 7
  （Category／Sub-Category × Region）的小計與總計都與 Superstore 的已知合計相同；Tableau 中小計的寫法以這份與
  Retail Analysis practice 兩份公開報表確認
- **欄標題的順序**：列與欄都有離散維度時（交叉表），欄依欄維度自己的排序排列（查詢另外算出欄標題的名次），
  不受列的影響：第一列沒有的欄不會排到後面，與 Tableau 相同。只有欄（或欄架上有 Measure Names、離散的量值）時
  依資料的順序
  XML 的寫法（哪個架的 total 對應哪個方向）以 HR 分析報表的截圖確認
- **Live 連線**：與 Tableau 直接連來源資料庫時相同，字串的排序與比較依資料庫的定序、TODAY() 與 NOW() 依資料庫的時區
  （Databricks 的工作階段時區設為 UTC）、STR() 轉換小數的格式依資料庫；Oracle 的空字串即 Null、ADD_MONTHS 在月底的行為照 Oracle。
  Oracle 沒有布林型別，結果是布林值的計算（布林維度、ISNULL、CONTAINS 的結果等）與 FINDNTH 在 Oracle 尚不支援；
  PostgreSQL 的 DATEPARSE 遇到符合格式但不存在的日期（例如 13 月）會報錯而不是 Null
- **函式**：REGEXP_EXTRACT 沒有符合時為 Null；正規表示式以 RE2 執行，用到前後查看、反向參照的樣式列為不支援；
  DATENAME 的月份與星期為英文名稱（Tableau 依工作簿的地區設定，中文環境可能不同）；ISDATE 只認得 ISO 格式的日期。
  FLOAT、INT 把日期轉成數值，DATE、DATETIME 把數值轉成日期時，以 1899-12-30 為第 0 天、小數部分是一天中的時間
  （與 Excel 相同，依公開資料推定）；TabMon 等報表常見的「捨去到 N 分鐘」與兩個時間相減的寫法不受第 0 天影響。
  PREVIOUS_VALUE 以遞迴的 CTE 在每個分割中依計算方向的順序逐列計算（公式中的其他 Table Calculation 先算成欄位），
  只支援寫在工作表上的 Table Calculation 計算欄位中；視窗函式的起訖可以是運算式（例如 `FIRST() + 1`、參數），
  逐列決定範圍，起點在終點之後時為 Null；HEXBINX／HEXBINY 的六角形邊長為 1、頂點朝上（與 d3-hexbin 相同，
  方向依公開資料推定）。外部分析服務（SCRIPT_*）、預測模型（MODEL_PERCENTILE、MODEL_QUANTILE）尚不支援。
  直通資料庫的 SQL（RAWSQL_*、RAWSQLAGG_*）在 Live 連線上把 SQL 原文代入參數後原樣送到來源資料庫
  （https://help.tableau.com/v2021.2/pro/desktop/en-us/functions_functions_passthrough.htm ，2026-11-22 查閱）；
  萃取、檔案與 RAWSQL_SPATIAL 不支援。空間函式見「地圖」
- **Relationship 模型**：語意依 Tableau 文件（「How Analysis Works for Multi-table Data Sources」）與公開文章推定；
  維度表中沒有量值的成員何時列出、只用到其他資料表欄位的篩選對沒有關聯的列以 Null 判斷，這兩點待真實報表確認。
  維度是一對多資料表欄位的運算式（日期部分、計算欄位）時，Tableau 在原值還是運算式的值上去重不明，列為不支援
  （查詢附帶的檢查）；關聯的效能選項（基數、參照完整性）不影響結果，一律以「多對多、部分相符」的語意查詢。
  量值來自多張資料表時，FIXED LOD 的值在檢視層級彙總（例如 MAX）依篩選後各列出現的 LOD 維度組合計算
  （與單一資料表時相同）；維度表中沒有量值、另以維度查詢補上的成員，LOD 的值也是 Null，這點待真實報表確認。
  跨資料表的計算（推定的部分）：路徑上的資料表也以內部連接；列層級的跨資料表計算當維度時，量值表中不在定義域的列
  歸在 Null 的標記（Tableau 也可能不顯示）；篩選中以「計算為 Null」判斷沒有關聯的列；跨資料表的彙總一律另以維度表
  查詢補上沒有量值的成員
- **Data Blending 的篩選與 LOD（推定）**：次要資料源欄位的條件與 Top N 篩選以次要資料源的資料計算（Tableau 的
  說明沒有寫）；次要資料源中的 INCLUDE／EXCLUDE LOD 相對於作用中的連結欄位。次要資料源的內容篩選依 Tableau 社群的
  回答（2018-12-21）推定 Tableau 不提供
- **使用者篩選與使用者函式**：Tableau「建立使用者篩選」產生的集合 XML 依公開資料推定（交集 level-members 與
  各使用者條件的成員聯集），工作表上以空的類別篩選表示「在集合中」；比對名稱不分大小寫、USERDOMAIN 對本機帳號的值
  （推定為 `local`）待真實報表確認；USERATTRIBUTE 系列只在 Tableau Cloud 與嵌入時有值，新平台取自 SSO 的宣告
- **顯示格式**：Tableau「自動」格式的百分比位數依值而定（公開報表中同一份報表有 33.999% 也有 52.03%），新平台的
  百分比類 Table Calculation 一律兩位小數；沒有設定格式的日期以 ISO 格式（2020-01-02）顯示，Tableau 依地區設定
  （1/2/2020）。量值與日期欄位的標題（座標軸、表頭、圖例）新平台以膠囊的寫法顯示（AVG(Age)、YEAR(Order Date)），
  Tableau 依介面語言產生（英文為 Avg. Age、Year of Order Date）；公司 Tableau 的介面語言確認後再對齊
- **標題中的欄位值**：與 Tableau 相同以整張工作表計算：量值是篩選後全部資料的彙總（KPI 卡的標題放合計、
  下面畫每月走勢線的寫法因此成立），維度只有一個值時顯示它、多個值時顯示 *（與 ATTR 相同，依公開資料推定）；
  成員別名、數字格式照樣套用，Null 不顯示。標題中的 Table Calculation 取檢視中各標記的值，都相同時顯示它
  （例如 TOTAL、WINDOW_SUM），不同時列為不支援；與量值篩選、Table Calculation 篩選一起使用時，標題中的量值與
  不在檢視中的維度只計算通過篩選的標記；Measure Names、Measure Values 與產生的緯度、經度尚不支援。
  **待驗證**：Tableau 知識庫 001458099（Adding Totals to a Title，help.salesforce.com，2026-11-22 查閱）寫的是
  標題中的欄位值在檢視中有多個不同的值時顯示「範圍」；本平台把標題中的量值當作全部資料的合計（依公開報表推定），
  欄列架上有離散維度、量值在各標記不同時可能與 Tableau 不同，要以 Tableau 確認（範圍的寫法也還沒確認）
  （前者解析時、後者查詢時列為不支援）
- **別名**：成員別名只用於離散維度的原始成員（彙總、日期衍生、連續欄位沒有成員），顯示在座標軸、表頭、圖例、
  標籤、工具提示、資料表與篩選控制項；篩選、動作與排序仍用原始值（Tableau 依原始值排序，依公開資料推定）。
  Null 的別名（`%null%`）在標籤中照樣顯示。參數的「顯示為」用於參數控制項與標題；Measure Names 的別名用於
  Measure Names 的表頭與圖例。Tableau 匯出的資料中維度是別名還是原始值未確認，計算比對兩種都接受
  （別名換回原始值再比）；資料源的 `<aliases enabled>` 開關不處理（公開報表全部是 yes）
- **色彩**：內建色盤的顏色值依公開整理的資料（見 `kernel/src/tmig/render/palettes.py`），起訖與中心的顏色兩個來源一致，
  中間的顏色未以 Tableau 實機比對；沒有指定顏色的值依它在資料中出現的順序取色盤的顏色（Tableau 依欄位的排序）；
  合併欄位的顏色（多個欄位放在色彩上，`map-pri`）依預設色盤
- **合併欄位（Combined Field）**：`nest-group` 群組中 crossjoin 的各欄位組合成一個字串維度，成員與 Tableau 相同
  寫成「值, 值」（Null 寫成 Null）；排序依組合後的字串（Tableau 依各欄位的值，數字與日期的欄位可能不同）
- **百分位數彙總**：彙總選單的百分位數（5、10、25、50、75、90、95；`derivation='Percentile'` 與 `aggregation-param`）
  與 PERCENTILE 函式相同以線性內插計算；Tableau 匯出時的欄名未確認（計算比對接受 `PCT50(…)` 等寫法）
- **Measure Values 與其他量值**：同一架上的 Measure Values 與其他量值（各自一個軸或疊成雙軸），Measure Values 的軸
  的標記卡與共用的相同時等同共用的；其他量值的色彩若是 Measure Names，以它自己的名稱上色、與 Measure Values 共用圖例
- **Measure Values 自動帶出 Measure Names 的位置（部分推定）**：編輯器把 Measure Values 拖進檢視、檢視中沒有
  Measure Names 時自動加上（加在架的最後；地圖不加）。Measure Values 在標記卡上（文字表）時：列架上有標題
  （維度、離散的量值）而欄架上沒有，加到欄架；其餘（欄、列架上都有、都沒有，或只有欄架上有）加到列架。
  Measure Values 在列（欄）架上當座標軸時加到另一架。依據只有一個：Tableau 的說明文件〈Measure Values and
  Measure Names〉中，「各產品類別、各地區的利潤」的文字表再加一個量值時，寫的是「The Measure Names field is
  automatically added to the Rows shelf」
  （https://help.tableau.com/current/pro/desktop/en-us/datafields_understanddatawindow_meavalues.htm，
  2026-11-21 查閱）。文件沒有寫那張表的類別與地區各在哪個架上，這裡當作欄、列架上各有一個維度；文件說的也是
  「把量值拖進文字表」，不是直接拖 Measure Values。其餘各種情形（只有列架上有維度時加到欄架、都沒有時加到
  列架、在座標軸上時加到另一架）是依使用經驗推定的，這台電腦沒有 Tableau 可以實測
- **文字是 Measure Values、欄列架上沒有 Measure Names（與 Tableau 不同）**：每一格有每個量值各一個文字標記，
  畫在同一個位置。這裡不疊在一起：各量值各占一欄（列架上有標題而欄架上沒有時）或一列，依 Measure Values 卡的
  順序，不顯示標題（位置的規則與上一項相同，render 的 notes 註明）。推定 Tableau 會把數字疊在同一格（沒有
  實測）；數字相同，只有排列不同。Measure Names 只在色彩或詳細資料上時也一樣分開排列
- **雙軸上以 Measure Names 上色的趨勢線（推定）**：雙軸的兩個量值各有一條趨勢線，顏色與自己的量值相同、
  用自己那個軸的刻度，「描述趨勢模型」以量值的名稱區分；Measure Values 與另一個量值疊成雙軸時也一樣（另一個
  量值以每個標記一筆資料配適）。Tableau 的畫面沒有比對過
- **刪除工作表、dashboard、Story 與復原（部分推定）**：Tableau 的說明寫「unlimited undo and redo of almost all
  actions」「Tableau saves your undo and redo history across all worksheets until you exit」
  （https://help.tableau.com/v2021.2/pro/desktop/en-us/inspectdata_undo.htm ，2026-11-21 查閱），刪除工作表的說明
  （https://help.tableau.com/v2021.2/pro/desktop/en-us/environ_workbooksandsheets.htm ，同日查閱）沒有寫刪除能不能
  復原、刪除前有沒有確認。以下是推定的，這台電腦沒有 Tableau 可以實測：
  - 刪除分頁可以用 Ctrl+Z 復原、刪除前不先問（依使用經驗）；平台照這樣做，刪除之後另外有一則可以按「復原」的訊息
  - 刪除目前的分頁之後開啟左邊的分頁（沒有實測 Tableau 開啟的是左邊還是右邊）
  - 與 Tableau 不同的地方：Tableau 的復原是整份活頁簿一條線；平台編輯的歷程是每個分頁各自的（換分頁就重來），
    只有「刪除分頁」是整份報表的紀錄。所以換過分頁之後按 Ctrl+Z，復原的是最近一次刪除，中間在別的分頁做的變更
    不會跟著退回；新增、複製分頁不能用 Ctrl+Z 取消（要自己刪除，刪除可以復原）
  - 還可以復原的期間，新的分頁不重複使用刪除的分頁的名稱（「工作表 2」刪除後再新增的是「工作表 3」）：為了放得
    回去。Tableau 這時取什麼名稱沒有查證
  - 說明還寫「There must always be at least one worksheet in a workbook」：Tableau 不能刪除最後一張工作表；平台可以
    （刪掉之後開啟資料來源分頁，可以復原）
- **百分比**（`<percentages enabled='true' mode='table|column|row|pane|row-in-pane|column-in-pane|cell-in-pane'>`，
  `Worksheet.percentages`）：量值顯示成占範圍合計的比例，以百分比呈現（欄位名稱後註明「合計百分比」）。依 Tableau
  說明文件〈百分比選項〉：
  - 合計依各量值的彙總方式計算（文件的例子：最大值所在的標記是 100%）——與總計相同，在範圍的詳細程度重新彙總
    底層資料，範圍以外的維度（含標記卡上的）都去掉；總計與小計的列也是百分比
  - 取消彙總量值時，所有的值是總和的百分比（值 ÷ 範圍內各列的值的總和）
  - Measure Names 在欄架（或列架）的最內層時，窗格中的列（欄）百分比每個標記是 100%：窗格是去掉最內層之後的
    範圍，去掉的是 Measure Names，維度都留著
  - 「百分比選項等同加上一個合計百分比表計算」：已經有表計算的量值是「表計算的值 ÷ 範圍內各標記的值的總和」
    （等同次要計算的合計百分比）。文件沒有直接描述兩者並用的結果，這是依那一句推定的，尚未以實際的報表比對
  與量值篩選並用時合計只算通過篩選的標記；與 Table Calculation 篩選並用尚不支援。工作表的查詢（計算比對的對象）
  仍是原本的值，百分比在檢視器呈現時計算
- **總計的位置**（`<rows onTop='true'>`、`<cols onLeft='true'>`，`Worksheet.totals_top`、`totals_left`）：欄總計
  在上方、列總計在左側，小計也在各組之前
- **形狀的調色盤**（標記樣式的 `<encoding attr='shape'>`，`Marks.shape_assignments`）：指定給各成員的形狀優先，
  其餘的成員依 Tableau 預設形狀調色盤的順序（成員依值排序，與色彩相同）；實心調色盤（`:filled/…`）對應到同名的
  形狀，一律以實心畫出；其他調色盤是自訂的圖片形狀（見「自訂的圖片形狀」）。形狀的名稱依公開資料推定。
  圖片形狀的大小（在儲存格中時相對於儲存格，依 Tableau 的畫面量測，見下方「依 Tableau 的畫面修正」；在連續的座標軸上是 30 像素乘上標記卡的大小，是推定的）與著色的方式（有顏色時不透明的部分整個換色）依 Tableau 的說明推定，
  尚未與 Tableau 的畫面逐像素比對；分頁的歷程（Page history）中的舊標記以預設的形狀畫出；雙軸的兩張標記卡各有
  自己的大小範圍時圖片依第一張的範圍；TIFF、EMF 格式的圖片不支援（瀏覽器不能顯示），依預設的形狀呈現
- **說明**（Caption；`<layout-options><caption>` 的自訂文字，`Worksheet.caption`、`caption_shown`）：工作表視窗有
  `<card type='caption'>` 時單獨檢視會顯示說明，dashboard 的工作表區塊依各自的 `show-caption`（`Zone.show_caption`）。
  自訂的文字可以含參數的值（欄位的值不顯示，記為略過）；沒有自訂文字時是自動產生的說明——欄列架上的維度與量值、
  標記卡上的欄位與篩選的欄位組成的簡短敘述，不是 Tableau 的英文句子
- **次要計算**（`TableCalc.secondary`：`type`、`addressing`、`options`）：主要計算（累計或移動計算）的結果先依它
  自己的計算方向算成欄位，再套用次要計算（計算方向沒有指定時與主要計算相同）。合計百分比是「值 ÷ 範圍內各值的
  總和」，其餘與快速表計算相同的寫法；顯示格式依次要計算（百分比類以百分比顯示）。目前只能在編輯器中建立，
  .twb 的次要計算尚未解析；次要計算的類型與 Tableau 相同限於累計與移動計算之後（依 Tableau 文件推定）
- **巢狀排序**（`Sort.nested`）：依欄位排序的排序值在同一架上外層維度的每個值之內各自計算，各窗格的順序可以
  不同；不是巢狀時以這個維度所有資料的值排序，各窗格的順序相同。.twb 中從座標軸、工具列或欄位標籤排序的寫法
  （`<shelf-sorts>` 的 `<shelf-sort-v2>`，Tableau 2018.2 起）是巢狀排序；排序對話框的依欄位排序（`<computed-sort>`）
  不是（依 Tableau 文件推定）
- **集合控制項**（Tableau 2020.2 的「顯示集合」；dashboard 的區塊 `Zone.kind` 為 `set`，工作表上的控制項
  `SheetControl.kind` 為 `set`）：列出集合所在維度的值，勾選的是集合的成員；檢視器中改變後，使用這個集合的工作表
  （In／Out、依集合的計算、合併集合）跟著改變，與集合動作共用同一份狀態。單一維度的固定成員、Top N 與條件集合
  都可以（Top N 與條件集合的控制項改變「一般」分頁的選取，Top N 與條件照樣套用，與 Tableau 相同）；多個維度的
  固定成員集合列出成員的組合，可以勾選或取消（Tableau 沒有這個控制項，是本平台多出來的）。合併集合不能有控制項。
  目前只能在編輯器中加入（集合的選單「顯示集合」、dashboard 編輯器的「集合：…」），.twb 的集合控制項尚未解析
- **合併集合**（`FieldSet.rule` 的 `combined`：`left`、`right`、`op`）：以布林公式（例如 `[A] AND NOT [B]`）計算，
  來源集合的成員改變時（含集合動作）跟著改變；來源的集合還在用時不能刪除。目前只能在編輯器中建立，.twb 的合併集合
  依原本的方式解析
- **線條類型**（標記樣式的 `line-interpolation`，`Marks.line_type`）：階梯以 step 內插（量值在橫軸時先垂直），
  跳躍只畫每個點到下一個點位置的水平線（屬性值 `step`、`jump` 依公開資料推定）
- **工作表上的控制項**（工作表視窗的 `<card type='filter'|'parameter'>`，`Worksheet.controls`）：單獨檢視工作表時
  顯示在檢視的右側；工作表上沒有對應篩選的控制項不顯示（記為略過）。「只顯示相關的值」（`values='relevant'`，
  `SheetControl.relevant`、dashboard 區塊的 `Zone.relevant`）：可選值只列出套用這張工作表上其他篩選（內容與維度
  階段，含檢視器目前的選取與篩選動作）之後還有資料的值，其他篩選改變時重新取得；範圍類的控制項與次要資料源
  的欄位不適用。「內容中的所有值」等其他選項依預設列出資料庫中所有的值
- **下拉線**（Drop Lines；標記卡的 `<dropline x-axis y-axis drop-when haslabel>`，`Worksheet.drop_lines`）：從標記
  畫到連續座標軸的虛線（到橫軸是往下的垂直線，到縱軸是往左的水平線；離散的座標軸沒有）。一律顯示時是圖表的一層；
  選取標記時才顯示的，檢視器在選取（或醒目提示）標記後只畫那些標記的線。座標軸上的值標籤（`haslabel`）尚未畫出
- **標籤的垂直對齊**（標記卡樣式 `cell` 的 `vertical-align`，`Marks.label_valign`）：圓點、形狀與方塊的標籤在標記的
  上方或下方（水平對齊沒有指定時置中），折線與區域的標籤可以改到點的中間或下方；長條的標籤位置不變
- **甘特條**：從連續座標軸（日期或數值）上的位置開始，長度為「大小」的量值（日期軸以天為單位，可以是負數）；
  沒有大小時畫成細線。連續座標軸只能有一個且同一架上沒有其他維度（窗格中的甘特條以長條呈現）；標籤尚未畫出
- **多層圓餅**：疊在圓餅上（雙軸）、有角度與顏色的標記卡畫成另一層圓餅，各自依自己的詳細程度（較粗的另外查詢）
  與大小畫出，後面的標記卡畫在上面；沒有角度與顏色的標記卡畫成中間的洞。分面的多層圓餅只畫第一層
- **樣式**：篩選與參數控制項、dashboard 標題物件的字型、文字表中多個欄位各自的儲存格字型與文字顏色（只有
  一個欄位或自訂標籤文字中的欄位值會套用）、欄的欄位標籤的背景（列的有畫）、依欄位設定的
  儲存格對齊、欄標題的垂直對齊、
  一致或各自的座標軸範圍不移轉（只影響外觀）。標記大小中文字標記的大小、依欄位決定大小時的範圍、連續座標軸（日期）上長條的寬度
  不移轉，沒有設定大小的圓餅推定為大小 1（公開報表中多數圓餅調大過）；折線寬度依兩個量測推定（預設約 2 像素、大小 14.55 約 30 像素）。沒有寫外距的區塊
  沒有外距、`display-field-labels` 與 `element='label'` 的 `display` 表示欄位標籤與離散欄位的標題、
  色帶的 `band-size` 是每段的列數、標準大小時離散欄的寬度（依內容估計）、`header-div` 是標題區域的分隔線
  （沒有寫顏色時與窗格相同）、沒有寫分隔線時預設畫在最內層以外的每一層，依公開報表推定（公開報表中最常見的
  寫法是把預設的分隔線關掉）、列標題預設靠左與文字表的數字預設靠右（依公開報表中 Tableau 存下的縮圖）、
  欄位標籤一律以半粗體顯示，依公開報表推定
- **線的樣式**：零線、趨勢線與參考線的 `line-pattern-only` 有公開報表的樣本；格線、座標軸線與分隔線
  （`table-div`、`header-div`）用同一個屬性名稱是推定的（沒有樣本），標題的分隔線沒有寫樣式時視為實線（不沿用
  窗格的）也是推定的。虛線與點線的線段長度沒有與 Tableau 的畫面對過。儲存格與窗格的框線見下一項
- **摘要卡選用的統計量**：Tableau 的文件只說明標準差是樣本標準差（含修正）、四分位數是第 25 與第 75 百分位數，
  沒有列出公式。四分位數在排序後的值之間線性內插（與中位數取中間兩個值的平均一致）、偏度與超額峰度用樣本的
  公式（與 Excel 的 SKEW、KURT 相同），都是推定的，還沒有與 Tableau 的數字對過
- **描述趨勢模型的各係數與描述預測的品質指標**：係數的標準誤、t 值與 P 值是一般最小平方法的算法（Tableau 的
  文件只說明各欄的意義），以 numpy 獨立驗算；指數與乘冪模型的數字在取對數之後的空間。預測的 MAE、MASE、MAPE
  依 Tableau 文件的定義（MASE 與「下一期等於這一期」的單純預測相比，品質依 MASE 分為良好、尚可、不佳；MAPE 在
  資料有 0 時沒有定義），誤差取各期的一步預測誤差；Tableau 的公式在文件中是圖片，分母的細節（單純預測的誤差
  取哪一段資料）是推定的，而且預測模型本身的初始值與最佳化與 Tableau 可能有些微差異，數字還沒有與 Tableau 對過
- **dashboard 的版面（依 Snow Sports Rental Operations Overview 報表的測試，2026-11-18）**：
  - 固定大小（`fixed-size`）不含區塊的外距：這份報表 25 個固定大小的區塊，Tableau 存下的寬、高都等於固定大小
    加上兩側的外距，排版時依此計算（之前少了外距，後面的區塊會往上偏）
  - 文字物件的文字垂直置中、行高 1.2 倍、上下不另外留白：依這份報表的版面推定（導覽的文字若靠上會蓋到圖示，
    兩行的標題在 42 像素高的區塊中要放得下）；Tableau 的文字物件沒有垂直對齊的設定
  - 欄列架上都沒有欄位的形狀標記畫在檢視的中間（沒有標籤時；有標籤時以文字呈現標籤，圖片不畫）
  - 圖片形狀上方的多行標籤整段往上移；時間軸只固定一端時自動的那一端延伸到整齊的刻度（走勢圖因此不會貼到
    右緣）。Tableau 延伸的規則沒有文件，用的是 Vega 時間刻度的規則
  - 這份報表指定 Roboto 字型：檢視器隨附 Roboto（`web/src/fonts.css`，授權見 `NOTICE.md`），檢視的電腦上
    沒有安裝時也用同一個字型；畫圖之前先等字型載入（`web/src/fonts.ts`）。其他字型不在電腦上時仍改用後備字型
  - 同一行內格式不同的自訂文字（KPI 的「71.30」24pt 接著「CM」16pt）分成各段、各用自己的字級與顏色，
    共用一條基線（`usermeta.text_segments`）：各段水平的位置由檢視器在畫圖之前量出寬度決定
    （`web/src/textSegments.ts`）。只用在文字標記；圓點與形狀的標籤、工具提示仍是整行一種格式（字級取最大的）
  - 上面各項當時沒有 Tableau 的畫面可以對照，是依 .twb 的定義與版面的合理性推定的
- **依 Tableau 的畫面修正（Snow Sports 報表 Overview 的截圖，使用者 2026-11-19 提供，繁體中文環境）**：
  下面各項是對照一張截圖量出來的，只有這一份報表的依據，數值（倍率、比例）是量測值不是 Tableau 的定義
  - 文字標記的顏色：各段自己的顏色（`fontcolor`）最優先；活頁簿或工作表設定了預設的文字顏色時，沒有指定
    顏色的文字用它、不跟著標記卡的顏色（KPI 的數字因此是深藍色，不是標記卡的銅色）；都沒有時才是標記卡的
    顏色。色彩上有欄位時照舊依欄位著色。自訂文字各段的對齊（`fontalignment`）優先於儲存格的對齊
  - dashboard 的文字物件與標題沒有指定字型、顏色時用活頁簿格式的「全部」字型（`Dashboard.text_font`、
    `text_color`）；參數與篩選控制項的區塊關掉標題（`show-title='false'`）時不顯示標題
  - 參考區間的底色寫在兩條線的任一條都算；橫軸上的參考區間（兩條縱向的線配成的）的標籤預設在窗格的
    下緣、線的右側，顏色是工作表的文字顏色，並讀取標籤的對齊與字級（`ReferenceLine.label_align`、
    `label_valign`、`label_size`）。截圖中只有參考區間：單一條縱向參考線沒有畫面可以對照，照舊在上緣
  - 折線的粗細：標記卡的大小乘以 6 像素、最細 1 像素（大小 0.38 的折線量到約 2.3 像素）；沒有設定大小時
    仍是 2 像素
  - 圖片形狀在儲存格中（欄列都沒有連續的座標軸）時，大小相對於儲存格較短的一邊：大小 s 時為 s / 2、最多
    填滿，與圓餅相同（大小 0.78、寬 157 像素的儲存格中天氣圖示約 57 像素）；在連續的座標軸上仍是 30 像素
    乘上大小（沒有畫面可以對照）。標題隱藏的分面（每一列一個甜甜圈）各格相連、平分區塊，甜甜圈的大小因此
    與截圖相同（直徑約 53 像素）
  - 色帶的層級（`band-level`）：1 是每一列，等於標題的層數時以最外層標題的組為單位（五層標題、層級 5 時
    截圖是每筆交易一段）。中間的層級依此類推（每加 1 往外一層），沒有畫面可以對照
  - 縱軸刻度的數量是高度除以 50（Vega 的預設是 40，截圖中約 230 像素高的座標軸每 20K 一個刻度）；橫的
    日期軸是寬度除以 150，年初的刻度只寫年份；自動的時間軸（折線與區域）兩端各留資料範圍的 5%
  - 橫向長條尾端有標籤時，自動的座標軸多留標籤的寬度（以字數估計）加 15 像素，最長的長條的標籤不會被切掉
    （最大值進位到六位有效數字再算，規格才不會隨彙總的浮點數尾數變動）
  - 數字的自動格式：沒有設定格式的實數量值，這一欄有不是整數的值時整欄都寫兩位小數（截圖是 0.00 與
    14.10）；一千以上照舊不寫小數。Tableau 實際的規則沒有文件
  - 成員的預設順序是自然排序：沒有設定排序的文字維度，字串中的數字當成數值比較（`…-Cha-2` 在 `…-Cha-10`
    之前；`tmig.natural`）。依據是 Tableau 的說明——「資料源順序」在關聯式資料源通常是自然排序，「字母順序」
    才是逐字比較（[Sort Data in a Visualization](https://help.tableau.com/current/pro/desktop/en-us/sortgroup_sorting_computed_howto.htm)）——
    與截圖（同一天的交易編號依 0、2、4、6、8 排列，資料中有 0 到 40）。輸出的順序與沿著這種維度計算的
    Table Calculation 都跟著改；排序在 DuckDB 執行，不下推到來源資料庫。排序對話框選「字母順序」的照舊
    逐字比較。Live 連線時 Tableau 的順序依資料庫的定序，這裡一律是自然排序，沒有實機比對過
  - `TODAY()`、`NOW()` 在指定了基準日時用它（`QueryState.today`；檢視器用 `cases.yaml` 的 `today` 或抓取
    預期結果的日期，與相對日期篩選相同），沒有指定時是查詢時的今天。截圖是 11 月 18 日的資料：以它為
    基準日時，畫面上的數字（四個 KPI 與增減、尖峰季的三個總額、庫存、天氣、表格的前八列）與截圖相同
  - 日期的寫法依看報表的人的語言：檢視器送出的 `Accept-Language` 是繁體中文時，沒有設定格式的日期寫成
    `2026/11/18`、日期軸的年份寫成 `2025年`（`tmig.render.format.LOCALES`）。只做了繁體中文，其他語言
    維持 `2026-11-18`、`2025`；月、季的寫法與數字的格式還沒有跟著地區設定；匯出與訂閱的圖不經過瀏覽器，
    用的是與地區無關的寫法
  - 窗格的分隔線關掉、標題沒有另外設定（沒有 `header-div`）時，標題的分隔線也不畫；文字表的欄名只在左邊
    留白，112 像素寬的欄放得下 `Customer Segment`
  - **還與截圖不同、沒有修的**：下拉式控制項的外觀（框線顏色）；天氣的城市名稱比截圖高一點；尖峰季的色帶
    與上方的數字差幾個像素（座標軸標籤的寬度不同）；文字表量值欄的分隔線與列標題的差 2 像素
- **儲存格、窗格、表頭、合計與總計的框線**：.twb 中的寫法沒有樣本，是推定的——`cell`、`pane`、`header` 的
  `border-style`、`border-width`、`border-color`（沿用 dashboard 區塊框線的屬性名稱），合計、總計是帶
  `data-class='subtotal'`／`'total'` 與 `scope='rows'`／`'cols'` 的同樣屬性（沒有 scope 時兩個方向都套用）。
  同一個位置有不只一種線時分隔線優先，其次總計、合計、窗格，最後儲存格；外框用窗格（沒有時為儲存格）的框線，
  也是推定的（Tableau 的文件 https://help.tableau.com/v2021.2/pro/desktop/en-us/formatting_worksheet.htm 沒有說明
  重疊時的順序，2026-11-22 查閱）；線的位置與粗細還沒有與 Tableau 的畫面對過。地圖與分面的小倍數沒有框線
- **「N 個 Null」指示器**：只算查詢結果中連續座標軸的值是 Null 的列（顯示遺漏的值補上的空標記與總計不算）；
  Measure Values 的座標軸與地圖不計。Tableau 的說明（https://help.tableau.com/v2021.2/pro/desktop/en-us/datafields_specialvalues.htm ，
  2026-11-22 查閱）列出點指示器的選項「Filter Data」「Show Data at Default Position」（地圖另有「Edit Locations」、
  樹狀圖有「Use Absolute Values」，平台沒有），「Hide Indicator」在右鍵選單；「篩選資料」只寫以篩選排除 Null 值、
  之後的計算也不含它們，沒有寫加的是哪一種篩選。平台加的是只留非 Null 值的範圍篩選（篩選對話框「特殊值」的
  「非 Null 值」），只加在查詢結果有 Null 的座標軸欄位上，直接加入、不開對話框——是推定的。平台的選單左鍵、右鍵
  都列出三項。.twb 中特殊值的設定（標記怎麼處理、指示器是否隱藏）沒有樣本、不解析，從 Tableau 開啟的報表
  一律是預設（隱藏標記、顯示指示器）
- **在檢視上拖曳標題排序（推定的部分）**：Tableau 的排序說明
  （https://help.tableau.com/v2021.2/pro/desktop/en-us/sortgroup_sorting_computed_howto.htm ，2026-11-22 查閱）
  寫了選取檢視或圖例上的標題拖到正確的位置就是手動排序、放下的位置以粗黑線標示，以及在圖例上排序也改變標記的
  順序（圖例最下面的是最下面的標記）；沒有寫原本的排序怎麼處理、多層標題怎麼移動。平台：原本的排序換成手動，
  順序取畫面上的順序（同一個成員在多組裡出現時以第一次出現的位置為準），篩選掉、不在畫面上的手動成員接在後面；
  內層的標題只能在同一組裡移動，但手動的順序是整個欄位共用的。**圖例**：拖曳色彩圖例的項目改的是色彩欄位的排序；
  色彩欄位有手動排序或遞減的字母排序時，圖例依這個順序排列、堆疊的長條與區域依它堆疊，各成員的顏色不變。堆疊的
  方向與沒有排序時相同（圖例的第一項在基線那一端）；說明中「圖例最下面的是最下面的標記」指的是位置還是前後
  （重疊時誰在下面）無法確認，沒有改。依欄位的值排序時圖例仍依成員排列（與 Tableau 不同）；形狀圖例的順序沒有
  跟著排序
- **編輯器建立的使用者篩選**：是布林的計算欄位，不是 Tableau 的集合；建立之後要修改時編輯它的公式。使用者與
  群組的名稱由人輸入，不是從伺服器的名單挑選，名稱打錯時那個對象就看不到任何成員
- **檢視者身分的連線設定**：.twb 的 `workgroup-auth-mode`（公開範例中有 `prompt`、`as-is`）、`one-time-sql`
  （Initial SQL）與 REST API 連線的 `embedPassword`、`authenticationType` 依公開資料推定；以檢視者身分模擬
  （impersonation）的屬性值未確認，含 `imperson` 的值一律列為不符合，其他不認得的值列為需注意。
  伺服器上的連線以資料源名稱對應報表中的資料源，對不到時以連線類型對應
- **框選（部分推定）**：Tableau 的文件寫矩形選取是多數檢視預設的工具（「The Rectangular tool is the default tool
  in most views」）、按住 Ctrl／⌘ 加選個別的標記，只說它選取「within a rectangular shape」的標記
  （https://help.tableau.com/v2021.2/pro/desktop/en-us/inspectdata_pan_zoom.htm ，2026-11-21 查閱）。方框要包住
  還是碰到標記就算，文件沒有寫：平台是碰到就算（以畫好的標記的外框判斷，折線與區域逐點判斷；圓餅是每一塊的外框，
  方框碰到外框的空白處也算）。按住 Shift 也加選、框到空白處取消選取是推定的。按 Esc 取消選取依 Tableau 的
  快捷鍵表（「Clear the mark selection：Esc」，https://help.tableau.com/v2021.2/pro/desktop/en-us/shortcut.htm ，
  2026-11-21 查閱）；dashboard 上幾張工作表都有選取時，Tableau 的 Esc 取消哪幾張沒有寫——平台一次取消最近選取的
  那一張（推定）。桌面版編輯器裡按在註解上拖曳是移動註解、其他地方是框選：Tableau 的註解怎麼拖曳沒有查證
- **多行文字的列高（推定）**：文字表的每一格有多行文字時列高跟著加高（見「顯示大小」）。Tableau 的說明文件
  （Resize Tables and Cells，https://help.tableau.com/current/pro/desktop/en-us/formatting_resizetable.htm ，
  2026-11-21 查閱）只寫列高由「格式＞儲存格大小」或拖曳調整、同一個欄位的成員同高，沒有寫多行文字時的列高；
  加高後的像素（每行字級加 2 像素、另留半個字級）與整段文字垂直置中也還沒有與 Tableau 的畫面對過。作者設定的
  列高比文字矮時，這裡把列高加大到放得下、不讓上下列的數字互相蓋住；Tableau 會怎麼畫（截掉、不顯示或照樣
  重疊）沒有查證。還沒處理的：文字的大小依欄位而定時不加高
- **只有一行的大字的列高（推定）**：每一格只有一行、字卻放不下預設的 20 像素時，沒有設定列高的列跟著變高
  （見「顯示大小」）。Tableau 的文件沒有寫字級與列高的關係：Resize Tables and Cells
  （https://help.tableau.com/current/pro/desktop/en-us/formatting_resizetable.htm ，2026-11-22 查閱）只寫列高由
  「格式＞儲存格大小」或拖曳調整；Format Text
  （https://help.tableau.com/v2021.2/pro/desktop/en-us/formatting_fonts_beta.htm ，2026-11-22 查閱）寫標題換行時
  「If cells are not large enough to show more than one row of text, turning on wrapping will have no visible
  effect」、要自己把儲存格拉大——看起來 Tableau 的儲存格不會跟著內容變大。平台為了不讓上下列的字疊在一起而
  加高，很可能與 Tableau 不同（Tableau 是截掉、不顯示或照樣重疊沒有查證）；門檻（13pt 起）與像素是平台自己
  定的。12pt 的字外框 21 像素，在 20 像素的列中外框約重疊 1 像素，字本身不重疊。設定了列高而字比列高大時
  照設定，仍會重疊
- **圓點與形狀表格中標籤的列高與位置（推定）**：列高跟著多行的標籤變高、整段以標記的中線為中心；在標記上下方
  的標籤（只有一行也一樣）留在自己的列裡，沒有設定列高時列高加大（一行 46 像素）。Tableau 的文件沒有寫，沒有
  與畫面對過；Tableau 預設隱藏蓋到其他標記的標籤，也可能是不加大列高、改為隱藏標籤。設定了列高而只有一行的
  照設定，仍會蓋到上（下）一列的標記；圖片形狀（大小相對於儲存格）的表格與直寫的標籤沒有處理
- **長條、折線與散佈圖上多行標籤的位置（推定）**：整段以單行時的位置為準（置中的整段置中、在上方的最後一行在
  上方、在下方的從定位點往下排）。Tableau 的文件沒有寫多行標籤的位置，沒有與 Tableau 的畫面對過
- **文字表的欄寬（推定）**：欄寬是平台依文字估計的（每個字 0.55 個字高、全形字一個字高，加 16 像素），現在依
  各行自己的字級；字級不是 9pt 的文字表欄寬與修正前不同（大字變寬、小字變窄），沒有與 Tableau 的畫面對過
  （Snow Sports 的複本前後比對：只有「Weather (2)」的欄寬由 115 變成 116 像素，其餘 41 張工作表的畫圖規格
  相同）。醒目提示表的欄寬依格子裡的文字，欄架上有維度的圓點表格依標記與標籤（Snow Sports 的複本前後比對
  沒有工作表因此改變）。標題成員仍一律當成 12 像素估計：依標題的字型估計會改變 Snow Sports 的「Weather」
  「Weather (2)」的欄寬（115、116 → 148 像素），所以沒有做；標題用大字時欄標題會互相重疊
- **離散座標軸上的點與區域註解（推定）**：.twb 的註解只記連續座標軸上的值，離散座標軸上的座標只有 API 與編輯器
  做的註解會有；點畫在成員格子的中間、區域從第一格的起點到最後一格的終點是平台的畫法，Tableau 怎麼放沒有查證
- **查詢錯誤的訊息**：資料庫執行時的錯誤（「SQL 執行失敗：…」「來源資料庫執行失敗（…）：…」）照原樣附上，裡面
  可能有 SQL 的內部欄名；「內部錯誤：…」的訊息也照舊（其餘還有內部代號的訊息見 `docs/backlog.md`）
- **沒有列的檢視中多行文字的位置（推定）**：同一種格式的多行文字整段以檢視的中線為中心。Tableau 的文件只寫
  垂直對齊可以選上、中、下，沒有寫多行文字是整段置中；依據是各行格式不同的文字本來就整段置中。沒有 Tableau 的
  畫面可以對照（Snow Sports 的複本沒有工作表因此移動）
- **符合高度、整個檢視時區塊太矮（平台自己的做法，與 Tableau 可能不同）**：文字表的列被壓到相鄰兩列的文字互相
  蓋住時，平台不再壓：列高保持標準的大小，在區塊內上下捲動（見「顯示大小」）。Tableau 這時怎麼畫沒有查到文件：
  調整表格大小的說明（Resize Tables and Cells，https://help.tableau.com/current/pro/desktop/en-us/formatting_resizetable.htm ，
  2026-11-22 查閱）沒有提到顯示大小與放不下的情形；標記標籤的說明（Show, Hide, and Format Mark Labels，
  https://help.tableau.com/current/pro/desktop/en-us/annotations_marklabels_showhideworksheet.htm ，2026-11-22 查閱）
  寫「By default, all marks are labeled unless the labels would overlap」，文字表的文字是不是照這一條、重疊的不
  顯示，沒有明寫，這台電腦也沒有 Tableau 可以實測。平台選擇讓每一列都看得到（捲動），不隱藏任何一列的數字；
  所以這種情形下區塊多了捲軸、列沒有填滿，畫面與 Tableau 可能不同。「互相蓋住」以畫好的文字的外框判斷
  （上下重疊超過 1 像素），臨界點附近的列高差 1 像素就會在填滿與捲動之間切換
- **簡報模式中的 Esc（部分推定）**：Tableau 的快捷鍵表（https://help.tableau.com/v2021.2/pro/desktop/en-us/shortcut.htm ，
  2026-11-22 查閱）寫的是「Switch in and out of Presentation Mode：F7、Ctrl+H」與「Clear the mark selection：Esc」，
  工作區的說明（Reorganizing the Workspace，
  https://help.tableau.com/v2021.2/pro/desktop/en-us/environ_workspace_reorganize.htm ，2026-11-22 查閱）只寫工具列與
  簡報模式控制項上的「Exit Presentation Mode」，官方文件沒有寫 Esc 會離開簡報模式；寫「F7 或 Esc 離開」的是
  第三方的書（Packt《Learning Tableau 10》的 Presenting, printing, and exporting 一節，
  https://www.packtpub.com/en-us/product/learning-tableau-10-second-edition-11-9781786466358/chapter/sharing-your-data-story/presenting-printing-and-exporting ，
  2026-11-22 查閱，只看到搜尋結果的摘要）。平台照使用經驗：簡報模式中 Esc 離開簡報模式；同時有選取的標記時先
  離開、選取留著（哪一個優先 Tableau 沒有寫，推定）
- **簡報模式的 F7、Ctrl+H 在瀏覽器裡（沒有驗證）**：這兩個鍵也是 Chrome 自己的快捷鍵——Ctrl+H 開啟「歷程記錄」、
  F7 開啟游標瀏覽（https://support.google.com/chrome/answer/157179 ，2026-11-22 查閱）。檢視器把按鍵標成已處理
  （preventDefault），但瀏覽器會不會先拿走這一下按鍵，取決於 Chromium 保留給自己的快捷鍵：全螢幕時除了離開與
  切換全螢幕之外都交給網頁（`BrowserCommandController::IsReservedCommandOrKey`，
  https://github.com/chromium/chromium/blob/main/chrome/browser/ui/browser_command_controller.cc ，
  2026-11-22 查閱），所以簡報模式中按 F7、Ctrl+H 離開應該可以；還不是全螢幕時（進入）保留哪些鍵沒有查到清單。
  端到端測試送出的按鍵不經過瀏覽器自己的快捷鍵處理，量不到這一點；桌面版（WebView2）的視窗裡也還沒有實測
- **手機上沒有手機版面的 dashboard（推定）**：Tableau 裝置版面的說明
  （https://help.tableau.com/v2021.2/pro/desktop/en-us/dashboards_dsd_create.htm ，2026-11-21 查閱）只寫三種版面都有
  時使用者看不到預設版面（「After you publish a dashboard with all three layouts, users won't see the default
  dashboard layout」），沒有明寫沒有那個裝置的版面時顯示什麼——平台顯示預設版面（推定）；也沒有寫預設版面在手機上
  以什麼大小顯示。平台不把「自動」大小的 dashboard 壓進手機的寬度
  （壓進 358 像素時圖表只剩幾十像素寬），改以作者設計的尺寸顯示、左右捲動；固定大小的照原尺寸。Tableau 實際上
  是縮小整個 dashboard、照樣壓縮還是可以捲動，沒有比對過。檢視器的工具列在手機上收成一列是平台自己的設計
  （Tableau Server 的工具列不同）
- **畫圖的附註的分類**：哪些附註算「沒有畫出來、算不出來」、哪些只是說明，是平台自己的分法（Tableau 沒有對應的
  東西：Tableau 畫得出來）。附註與查詢錯誤裡的名稱已換成畫面上的寫法（資料庫執行時的錯誤訊息照原樣，見上面
  「查詢錯誤的訊息」）
- **列印與訂閱 PDF 的縮放（推定）**：Tableau Desktop 的列印說明寫縮放的「Automatic」是「Scales the view
  automatically based on the paper size」，並註明這些設定只影響列印、不影響匯出的 PDF
  （https://help.tableau.com/v2021.2/pro/desktop/en-us/printing.htm ）；Tableau Server 下載 PDF 的對話框有 Scaling
  （https://help.tableau.com/v2021.2/pro/desktop/en-us/export.htm ），訂閱的說明只寫選紙張與方向
  （https://help.tableau.com/v2021.2/pro/desktop/en-us/subscribe_user.htm ；三頁都是 2026-11-21 查閱），都沒有寫
  dashboard 比紙大時怎麼縮放。平台的訂閱 PDF 一律把 view 縮放到一頁之內（邊界 10 公釐；比紙小的 view 也會放大
  到頁寬或頁高；單張工作表是整張，包括在區塊內捲動時看不到的列，列多時字會很小），沒有與 Tableau 寄出的 PDF
  比對過。Tableau Server 下載 PDF 的 Scaling 選項依嵌入 API 的列舉是 Automatic、25%–400% 與「At most 1／2 pages
  wide／high」（https://help.tableau.com/current/api/embedding_api/en-us/reference/enums/PrintScaling.html ，
  2026-11-22 查閱；是現行版的文件），平台的訂閱沒有這些選項。訂閱的各種紙張（A3、A4、A5、Legal、Letter、Ledger、Tabloid）
  以 Chromium 實際產生過，頁面大小正確、都在一頁之內；`@page` 的 Tabloid 不是 CSS 認得的紙張名稱（瀏覽器會忽略
  紙張大小），改成寫寬高——檢視器「頁面設定」選 Tabloid 時原本也是這個問題
- **工作表的分頁列印（推定的部分）**：Tableau Desktop 的列印說明
  （https://help.tableau.com/v2021.2/pro/desktop/en-us/printing.htm ，中文版
  https://help.tableau.com/v2021.2/pro/desktop/zh-tw/printing.htm ，2026-11-22 查閱）寫了每張工作表依頁面設定印成
  一頁或多頁、「Repeat headers and legends on each page」在每一頁加上欄、列標題、「Break pages on pane boundaries」
  不在儲存格中間分頁，縮放有 Automatic（依紙張大小自動縮放）、Scale to（百分比）與 Fit to（橫向、縱向幾頁），
  以及這些設定只適用於單張工作表、不適用於 dashboard。下面是文件沒有寫、平台自己決定的：頁的順序（先往下、
  再往右；Tableau 的說明沒有寫，平台沒有「頁的順序」的選項）；「自動」怎麼縮（平台：填滿區塊的方向縮到紙張之內、
  固定大小的方向照原尺寸）；兩個勾選框的預設（平台：都勾選）；標題只印在第一頁、說明印在最後一頁；圖例當成
  圖右邊的一塊、每頁從最上面開始印；重複標題時列標題只印這一頁的列——多層列標題的外層成員只寫在各組的第一列，
  某一頁從組的中間開始時那一頁沒有外層的名稱（Tableau 怎麼印沒有查到）；「適合於」只縮小、不放大。
  頁面架的「列印所有頁面」沒有做（只印目前的頁）；dashboard 裡在區塊內捲動的工作表仍只印區塊裡看得到的那一段
  （dashboard 整個縮放成一張）。都沒有與 Tableau 印出來的紙本或 PDF 比對過。
  圖片、PowerPoint 與訂閱的圖片仍是區塊裡最上面看得到的那一段（Tableau 匯出圖片時捲動的檢視輸出到哪裡沒有
  查到文件，平台沒有改）；2026-11-22 之前列印與 PDF 也只印看得到的那一段，標準大小放不下的表與列高保持的表
  結果相同（以 17 列的文字表比對過）
- **同一個 dashboard 每次載入的截圖差幾個像素（不是平台的程式）**：以 Chromium 截圖比較時，同一個 dashboard 每次
  載入可能差幾十到幾百個像素（Snow Sports 的 Overview，1700×1100：Poly、Rented Units Chart、Snow Chart、Seasonal
  Rev 與 Rev KPI 幾張，每個色版差 1–6，都在反鋸齒的邊緣）。各工作表的查詢結果、字型、位置與大小、畫出來的 SVG
  每次都相同（Vega 的 clip 代號是整頁共用的流水號，依各圖畫好的先後不同，比較時要重新編號）；同一次載入、DOM 不變，
  只強迫重新點陣化也會變。原因是 Chromium 的部分點陣化（只重畫失效的矩形，各工作表畫好的先後每次不同）：加上
  `--disable-partial-raster` 時 5 次、8 次載入逐像素相同（2026-11-22，Windows 上 Playwright 的 Chromium）。逐像素
  比對截圖時加這個參數，或允許反鋸齒邊緣的小差異
- **沒有資料的檢視（推定）**：篩選之後沒有任何標記時畫成空白（沒有座標軸、表頭與格線），依對 Tableau 的了解，
  沒有查到文件、也沒有與 Tableau 的畫面比對。有「顯示空的列／欄」或「顯示遺漏的值」補出來的列時照常畫出
- **刻度文字的間隔（推定）**：連續座標軸上相鄰的刻度文字至少相隔 4 像素，不到時每隔一個省略（Vega 的作法）；
  Tableau 省略刻度文字的規則沒有文件，與 Tableau 的畫面比對時刻度的疏密可能不同
- **摘要資料的數字（推定）**：Tableau 的說明沒有寫摘要資料下載的數字格式
  （https://help.tableau.com/v2021.2/pro/desktop/en-us/export.htm ，2026-11-21 查閱）。平台寫未格式化的值，
  浮點數去掉加總留下的尾數（相對誤差一兆分之一以內）；計算比對（`tmig verify`）與檢視器用的仍是完整精度
- **發布的名稱**：伺服器上顯示的是發布時填的名稱，但連線的 Initial SQL 的 `[WorkbookName]` 仍代入報表檔裡的名稱
  （Tableau 代入的是伺服器上的名稱還是檔案的名稱沒有查證）

尚未支援（解析時列入 unsupported，報表進人工佇列）：
合併集合（XML 的寫法尚未在公開報表中看到）、預測指定的彙總依據、工作表與 dashboard 的擴充功能（Extensions）、LOD 的維度本身含 LOD、SCRIPT_*／MODEL_*、RAWSQL_SPATIAL 與萃取或檔案上的 RAWSQL_*、尚未併入 .tdsx 的 Published Data Source、
上表以外的資料庫的 Live 連線、
要求檢視者輸入自己的資料庫帳號或以檢視者身分模擬的連線、預存程序（Stored Procedure）當作資料表、
Data Blending 中同一個彙總混用兩個資料源的欄位、同一個 LOD 混用主要與次要資料源的欄位、次要資料源的內容篩選、
標題中的 Measure Names、Measure Values 與產生的緯度經度、
故事點記下的其他狀態（排序、欄列架的變更、註解等）、
取消彙總的叢集與以維度分群、Teradata 的彙總中的 RANDOM()、
舊版的 .tde 萃取。

## 開發規則

- IR 是唯一的報表定義：解析器寫入，其他模組只讀，不回頭讀 .twb
- 遇到不支援的元素一律寫進 IR 的 `unsupported`，不得默默丟棄
- IR Schema 有任何欄位異動，必須升 `ir_version` 並重新產生 `kernel/schema/`（`uv run python scripts/regen_ir.py`
  一併重新產生範例 IR 與各 fixture 的 golden IR）
- SQL 與 Vega-Lite 規格的快照變更須人工審核：`TMIG_UPDATE_SNAPSHOTS=1 uv run pytest` 後檢查差異再提交
- 任務編號、依賴與驗證方式以 Spec 的任務清單為準
- 完整的測試約 25 分鐘：提交後以 `uv run python scripts/check_commit.py` 在另一個工作目錄測那個 commit
  （主目錄可以繼續修改），通過之後才推上去
- 給 Claude Code 的工作規則在 `CLAUDE.md`；還沒做、還沒驗證與待決定的事在 `docs/backlog.md`
