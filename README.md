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
Data Blending 支援主要與次要資料源是萃取、檔案、跨資料庫的 join 或 Live 連線（次要資料源也可以是 Relationship 模型）的工作表，含引用次要資料源欄位的計算欄位、次要資料源的維度、篩選、排序、Table Calculation、參考線與總計（見「Data Blending」）。依檢視者的身分連線資料庫的報表（沒有嵌入密碼、Initial SQL 帶入登入者），移轉後資料庫端的權限
不會生效，盤點時列為不符合起步條件（見「依檢視者的身分連線」）。以 156 份公開報表重新盤點：放寬多個資料源後 12 份
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
各自的設定），標準大小時離散的列每一列 20 像素、放不下時在區塊內捲動；設定過的列高、欄寬與標題寬度，以及列與欄的
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
它的來源檔驗證（144 列、各年的總和與 Excel 相同）。文字檔的資料解譯器與具名範圍尚不支援；檔案的內容是報表封裝時的
快照（與 Tableau Server 上的 .twbx 相同）。

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

### 依檢視者的身分連線

平台一律以唯讀的服務帳號連線、不執行 Initial SQL。Tableau 上依檢視者的身分查詢資料庫、由資料庫端過濾資料的報表，
移轉後每個人會看到相同的資料，所以盤點時就列出（`tmig census`、`tmig inventory` 的「連線不依賴檢視者的身分」）：

- **不符合起步條件**（解析時也列入 unsupported）：Tableau Server 上要求檢視者輸入自己的資料庫帳號（伺服器盤點比對
  REST API 的「沒有嵌入密碼」，或 .twb 的 `workgroup-auth-mode='prompt'`）、以檢視者的身分模擬（impersonation）、
  Initial SQL 用到 `[TableauServerUser]`／`[TableauServerUserFull]`（例如 Oracle VPD 的 `DBMS_SESSION.SET_IDENTIFIER`、
  SQL Server 的 `EXECUTE AS USER`）。這類報表要改用列層級權限（使用者函式或使用者篩選）重寫後再移轉
- **需注意**：Windows 整合驗證與 Kerberos（伺服器可能以 Run As 服務帳號連線，也可能以 Kerberos 委派用檢視者的身分，
  請與 DBA 確認）、OAuth（伺服器上嵌入了登入資訊時不列）、其他 Initial SQL（平台不會執行，解析時列入 unsupported）、
  萃取來源的 Initial SQL（檢視時讀萃取檔不受影響，但更新萃取時不會執行）、伺服器上沒有嵌入密碼但對不到資料源的連線

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

尚不支援（查詢時列為不支援）：同一個彙總用到多張資料表的欄位（例如 `SUM(IF [客戶].[區域] = 'East' THEN [訂單].[金額] END)`）、
同時用到量值表與其他資料表（或兩個分支）欄位的篩選；關聯條件含計算（解析時列入 unsupported）。

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
  不必放次要資料源的欄位；dashboard 上的篩選控制項可以控制它（可選值來自次要資料源）。次要資料源的內容篩選、條件與
  Top N 篩選列為不支援
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

尚不支援：同一個彙總中同時用到主要與次要資料源的欄位、LOD 運算式中次要資料源的欄位、次要資料源的內容篩選、條件與
Top N 篩選、連結欄位不在檢視中卻設為作用中。連結欄位的 Null 視為相等。

### 跨資料源篩選

Tableau 10 起，篩選可以「套用到所有使用相關資料源的工作表」或選取的工作表：其他資料源的欄位的篩選，以資料源的關聯
（名稱相同或自訂的對應）篩選主要資料源中對應的欄位。.twb 在篩選中以 `<target field>` 記下對應的欄位，解析時直接改成
主要資料源該欄位的篩選（與 Tableau 相同，不篩選次要資料源）：

- 本地的跨資料源篩選（工作表的 `<filter>`，常帶 filter-group）與一般篩選相同，屬於同一篩選群組的工作表一起更新
- 全域的跨資料源篩選存在主要資料源的 `<shared-view>`，工作表的篩選架（`<slices>`）列出其他資料源的欄位；篩選群組與該資料源
  自己的全域篩選相同，任何一張工作表的篩選控制項都一起更新
- 同一欄位已有不設限的篩選時以跨資料源的篩選取代；dashboard 上其他資料源欄位的篩選控制項，控制工作表上對應的跨資料源篩選
- 沒有 `<target field>` 的其他資料源欄位篩選（次要資料源自己的篩選）、次要資料源的全域篩選尚不支援

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
  （與 Tableau 相同的大小寫與群組）；不在目錄中的帳號拒絕登入。沒有目錄時用標頭或 ID Token 中的群組
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
  - 使用者：新增（站台角色、網域、email）、修改站台角色與群組、刪除（擁有內容的人要先換擁有者；不能拿掉自己的管理員角色）
  - 群組：新增、改名、成員、刪除；報表公式的 `ISMEMBEROF()` 以群組名稱判斷，改名後用到舊名稱的列層級權限要一併修改
  - 修改紀錄：每次修改的時間、修改的人、動作與對象（`site-changes.jsonl`，不記錄修改前後的值）
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
└── scripts/                一鍵檢查、示範報表的建置與獨立計算
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
- **合併欄位**（「建立＞合併欄位…」）：勾選兩個以上的維度（依勾選的順序），值為各欄位的值以「, 」連接，名稱為
  「A & B (Combined)」（`DataField.combined` 記下組成的欄位；資料窗格以 🔗 標示）。從 Tableau 開啟的合併欄位相同
- **資料夾**（欄位選單的「資料夾＞建立資料夾…／加入資料夾／從資料夾移除」、資料窗格的「新資料夾…」，把欄位拖到
  資料夾的名稱上即移進去）：資料夾依名稱排序，可以改名、刪除（欄位回到最上層）；2020.2 以前分在維度、量值之下的
  `<folder role>` 與 2020.2 起的 `<folders-common>` 一併移轉（`Datasource.folders`）
- **日期屬性**（資料窗格的「日期屬性…」）：週的起始日與**會計年度**的起始月；日期欄位另有「預設屬性＞會計年度起始…」
  （沿用資料源或指定月份）
- **智慧顯示**（工具列的「智慧顯示」，Tableau 的 Show Me，`web/src/editor/showMe.ts`）：依檢視中的維度、日期、
  量值與地理欄位列出 24 種圖表（文字表、熱度圖、醒目提示表、符號地圖、填色地圖、圓餅圖、水平／堆疊／並排長條、
  矩形式樹狀結構圖、圓形檢視、並排圓形、連續與離散的線及區域圖、雙線、雙重組合、散佈圖、直方圖、盒鬚圖、甘特圖、
  子彈圖、填滿的泡泡圖），不能用的變淡並在提示中列出需要的欄位，建議的圖表加上外框；點選後重新排列欄列架與標記卡
  （篩選、頁面與排序不變，可以復原）。直方圖自動建立數值分組（建議的大小），盒鬚圖與子彈圖加上對應的參考線與分布
- **選取標記**：在檢視上點選標記（Ctrl 加選），選取列有「只保留」「排除」（一個維度時是這個維度的篩選，多個維度時是
  依值組合的「包含／排除」篩選）、「群組」（以選取的成員建立群組）與「建立集合」（多個維度時成員是值組合）
- **欄、列、篩選、標記卡**：拖放加入、移動（按住 Ctrl 為複製）、排列順序；標記卡的標記類型、色彩、大小、標籤、
  詳細資料、工具提示、形狀、角度、路徑；多個量值有各自的標記卡時有「全部」與各量值的分頁；Measure Values 卡
- **工具提示中的工作表**（標記卡的「工具提示…」，Tableau 工具提示編輯器的「插入＞工作表」）：加入、移除工作表，
  設定最大寬、高與篩選（所有欄位或檢視上選定的維度）；刪除工作表時一併從其他工作表的工具提示移除
- **膠囊選單**：篩選、排序（字母或依第一個量值，遞增、遞減、清除；「排序…」對話框另有資料來源順序、依任一欄位與
  彙總、手動排列成員）、日期的部分（離散）與截斷（連續）與精確日期、
  彙總方式、維度與量值互換、離散與連續、快速 Table Calculation（累計、差異、差異百分比、合計百分比、排名、百分位數、
  移動平均；檢視中有年時另有年初至今總計、年成長率，以及複合成長率）與清除、「編輯表計算…」（計算類型與選項：相對於
  上一個／下一個／第一個／最後一個、複合成長率、排名方式與順序、累計與移動計算的彙總與範圍；計算方向：表格、窗格、
  儲存格各方向與指定維度、計算層級、重新開始間隔）、數字格式（數字、貨幣、百分比、
  小數位數、K／M／B 單位、千分位）、顯示標題、編輯座標軸（標題、固定範圍、包含 0、反轉、同步）、雙軸（與 Tableau
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
  的預測區間；資料窗格加上 Forecast Indicator）、叢集（變數預設為檢視中的量值，叢集數自動或指定，叢集欄位放到
  色彩上，以 Cluster N 顯示）、彙總量值（關閉時量值不彙總，
  每一列資料是一個標記，完全相同的列也各是一個標記；只有維度時相同的組合只畫一個；與總計一起使用尚不支援）
- **註解**（分析的「註解…」或選取標記後的「💬 註解…」）：標記的註解跟著選取的標記，點與區域的註解以座標軸上的值
  定位；可以移除
- **工具列**：復原／重做（Ctrl+Z／Ctrl+Y）、交換欄與列、顯示大小、顯示欄總計與列總計、標題、格式（背景、
  文字顏色、字型與字級、列與欄的格線、座標軸線、刻度、欄位標籤、零線、分隔線、色帶）、工作表名稱、
  新增、複製、刪除工作表（dashboard 或 Story 用到的工作表不能刪）

### dashboard、Story 與動作的編輯

- **dashboard**（「編輯 dashboard」，網址 `/w/<報表>/edit-d/<dashboard>`）：畫面直接以檢視器繪製，點選畫面上的
  區塊或「版面配置」的樹狀清單選取；工作表拖到畫面上或連按兩下加入，物件有水平與垂直容器、文字、標題、圖片、
  網頁（內嵌外部網頁；網址可以留空白，由網址動作載入）、空白、導覽按鈕與匯出按鈕（按鈕的動作可以改成導覽、
  顯示／隱藏區塊或匯出 PDF、圖片、PowerPoint），工作表上的篩選、圖例、頁面與參數加入成控制項；並排加入時依序排列的容器重新分配空間，
  其他容器與 Tableau 拖放到區塊上相同把該區塊分成兩半；勾選「以浮動方式加入」時加在並排的版面之上，可以拖曳
  移動與調整大小。選取的區塊可以換工作表、顯示大小、顯示標題、控制項的樣式、文字、網址、按鈕的目標、
  容器的排列、固定寬度或高度、隱藏、背景、框線、外距與內距、前移與後移、移除；dashboard 的大小（固定、自動、
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
  右側即時預覽

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
  （或資料窗格的「編輯資料源…」）開啟目前資料源的編輯畫面
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
- **資料解譯器**（Excel 檔下的「使用資料解譯器」）：工作表上面有標題、說明的列時找出標題列（至少兩格有值、都是文字
  或整數，且下一個有值的列至少一半的欄有值；Tableau 的判斷方式沒有公開，這是推定的規則），之前的列與之後空白的列
  不算；切換時名稱相同的欄位保留
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
  `GET …/remote-tables`、`POST …/remote-columns`、`PUT …/custom-sql/<名稱>`；桌面版另有 `POST /api/desktop/inspect`、
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
- **匯出**：圖片（PNG）、資料、交叉資料表、PDF、PowerPoint，依權限顯示（下載圖片／PDF、下載摘要資料）：
  - 交叉資料表：選擇 dashboard 中的工作表，下載 Excel 或 CSV；內容與「資料」的表格相同（別名、數字與日期格式、
    小計與總計），Excel 中千分位、百分比、小數與括號的負數存成數字並套用相同的格式，可以直接計算
  - 資料：摘要（每個標記一列、未格式化的值）與完整資料（工作表的基礎資料列：資料源篩選、列層級權限、工作表的篩選，
    只含仍在檢視中的標記的資料列；要「檢視基礎資料」的權限），畫面上預覽前 1,000 列，下載 CSV（完整資料最多
    1,000,000 列）。CSV 為 UTF-8（含 BOM，Excel 直接開啟中文不會亂碼）
- **自訂檢視**（有報表庫時）：把目前的篩選、參數與選取存成自訂檢視，可以設為自己的預設檢視（開啟時套用）；
  有「分享自訂檢視」的權限時可以公開給看得到這份報表的人；同名時取代
- **留言**（有報表庫時）：每個 view 的留言，「檢視留言」看、「新增留言」留；留言的人、報表的擁有者與站台管理員可以刪除
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
  固定範圍、只固定起點或終點（數值或日期），範圍外的標記裁切；反轉；自動範圍不必包含 0；同步的雙軸共用刻度。
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
- 色帶（網底的列色帶、欄色帶）：隔 N 列（欄）畫上窗格的顏色，多層的列標題或列標題的寬度有設定時也畫上
  標題的顏色
- 分隔線（框線的列分隔線、欄分隔線）：窗格（`table-div`）與標題（`header-div`）分開設定，畫在前 N 層標題的
  成員改變處，顏色與線寬依設定。沒有寫時與 Tableau 相同都畫（淺灰、1 像素、最內層以外的每一層之間，只有一層
  時不畫）。多層的列標題中，線從改變的那一層所在的欄畫起，外層沒有改變的欄不切開；長條在分隔線與色帶上維持
  原本的粗細
- 線條：格線、座標軸線與刻度的顏色與 Tableau 的預設相同（格線 #f0f0f0、座標軸線與刻度 #f4f4f4，依畫面量測）；
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
  欄位的窗格）用於這個欄位的值
- 工作表標題的背景（`element='title'` 的 `background-color`）
- 多層的欄標題每一層一行（文字表在上方、其他圖在下方），外層的成員與 Tableau 相同合併成一格、寫在各組的中間；
  標準大小捲動時，欄標題、欄位標籤與座標軸固定在區塊的上下緣、列標題固定在左緣（檢視器以圖的複本裁出這些區域）
- 標題成員的垂直對齊（`vertical-align`）：外層的成員置中時寫在各組的中間（偶數列的組在中間兩列之間）、
  置底時寫在最後一列；文字方向（`text-orientation`，例如 -90 由下往上）直寫，欄寬為一行字
- 色帶的層級（`band-level`）：以前幾層標題的組為單位隔組上色；欄標題區域也畫分隔線（以刻度畫在各組之間）
- 字型（Format > Font）：沒有設定時與 Tableau 相同，刻度、標題成員、座標軸標題、欄位標籤、圖例與工具提示都是
  9pt（12 像素），工作表標題 15pt（20 像素）；刻度與標題成員為灰色（#555555），座標軸標題、欄位標籤與標題較深
  （#333333），欄位標籤半粗體（依 Logshark 報表文件中 Tableau 的畫面量測）。活頁簿的預設字型（`all`）與
  工作表、標題成員與刻度（`label`，可依欄位）、文字表（`cell`）、欄位標籤、圖例、標題的字型、字級、粗細與斜體
  照設定，標記標籤依標記卡的 `datalabel`；工作表標題另依標題文字本身的格式。Tableau 隨附的字型（Tableau Book、
  Tableau Semibold 等）在裝了 Tableau Desktop 的電腦上照用，沒有時依序改用 Benton Sans、Segoe UI、Arial
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
- **色彩上有多個欄位**：Tableau 以欄位值的組合著色；新平台以第一個欄位著色，其他的維度當作詳細資料（詳細程度與
  數字相同），Measure Names 略過（記在略過的元素）
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
- **樹狀圖、泡泡圖、文字雲**（欄列架上沒有欄位時的排列）：樹狀圖以 squarified 演算法依工作表在 dashboard 上的
  區塊寬高比排列（不在 dashboard 上時以 960×600），色彩是維度時先依顏色分區；泡泡圖以前緣鏈演算法排列、同色的
  泡泡排在一起；文字雲把字緊靠排成寬高約 1.6 的一團。標籤依預期的大小換行，放不下時不顯示；Tableau 的排列細節
  沒有公開，位置可能不同（面積、大小與數字相同），也沒有大小的圖例
- **沒有指定顏色的成員**：依畫面中的成員排序後依序取色盤的顏色（不隨工作表的排序改變）；Tableau 依欄位的所有
  成員排序，篩選掉部分成員時顏色可能不同（公開報表多半已在 .twb 中存下各成員的顏色，依存下的顏色）
- **地圖**：底圖由平台內建的公開地理資料畫出（陸地、水域、國界、州界與地名），沒有 Tableau 底圖的街道、地形、
  衛星影像與美國人口普查的資料圖層，「街道」「戶外」「衛星」樣式以相近的配色呈現；郵遞區號沒有區域形狀（平台的
  資料只有中心點），填色地圖以中心點的圓點呈現；美國以外沒有郡的資料。同名而範圍不足以區分的地名（例如沒有州的
  Springfield）列為未知，與 Tableau 的「不明確」相同。Tableau 地圖工具列的選取工具（矩形、套索、半徑）與
  自訂的 WMS、Mapbox 背景地圖尚不支援（以內建的底圖呈現）；密度圖依色彩分組、地圖上的趨勢線與參考區間尚未畫出。
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
  合在一起顯示。放 dashboard、Story 列為不支援，不存在的工作表（Tableau 不顯示）略過
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
  方向依公開資料推定）。外部分析服務（SCRIPT_*）、預測模型（MODEL_PERCENTILE、MODEL_QUANTILE）、
  直通資料庫的 SQL（RAWSQL_*）尚不支援；空間函式見「地圖」
- **Relationship 模型**：語意依 Tableau 文件（「How Analysis Works for Multi-table Data Sources」）與公開文章推定；
  維度表中沒有量值的成員何時列出、只用到其他資料表欄位的篩選對沒有關聯的列以 Null 判斷，這兩點待真實報表確認。
  維度是一對多資料表欄位的運算式（日期部分、計算欄位）時，Tableau 在原值還是運算式的值上去重不明，列為不支援
  （查詢附帶的檢查）；關聯的效能選項（基數、參照完整性）不影響結果，一律以「多對多、部分相符」的語意查詢。
  量值來自多張資料表時，FIXED LOD 的值在檢視層級彙總（例如 MAX）依篩選後各列出現的 LOD 維度組合計算
  （與單一資料表時相同）；維度表中沒有量值、另以維度查詢補上的成員，LOD 的值也是 Null，這點待真實報表確認
- **使用者篩選與使用者函式**：Tableau「建立使用者篩選」產生的集合 XML 依公開資料推定（交集 level-members 與
  各使用者條件的成員聯集），工作表上以空的類別篩選表示「在集合中」；比對名稱不分大小寫、USERDOMAIN 對本機帳號的值
  （推定為 `local`）待真實報表確認；USERATTRIBUTE 系列只在 Tableau Cloud 與嵌入時有值，新平台取自 SSO 的宣告
- **顯示格式**：Tableau「自動」格式的百分比位數依值而定（公開報表中同一份報表有 33.999% 也有 52.03%），新平台的
  百分比類 Table Calculation 一律兩位小數；沒有設定格式的日期以 ISO 格式（2020-01-02）顯示，Tableau 依地區設定
  （1/2/2020）。量值與日期欄位的標題（座標軸、表頭、圖例）新平台以膠囊的寫法顯示（AVG(Age)、YEAR(Order Date)），
  Tableau 依介面語言產生（英文為 Avg. Age、Year of Order Date）；公司 Tableau 的介面語言確認後再對齊
- **標題中的欄位值**：與 Tableau 相同以整張工作表計算：量值是篩選後全部資料的彙總（KPI 卡的標題放合計、
  下面畫每月走勢線的寫法因此成立），維度只有一個值時顯示它、多個值時顯示 *（與 ATTR 相同，依公開資料推定）；
  成員別名、數字格式照樣套用，Null 不顯示。標題中的 Table Calculation 值，以及與量值篩選並用時尚不支援
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
- **Drop Lines**：選取標記時才顯示的 Drop Lines 不顯示（記為略過）；一律顯示的列為不支援
- **甘特條**：從連續座標軸（日期或數值）上的位置開始，長度為「大小」的量值（日期軸以天為單位，可以是負數）；
  沒有大小時畫成細線。連續座標軸只能有一個且同一架上沒有其他維度（窗格中的甘特條以長條呈現）；標籤尚未畫出
- **多層圓餅**：疊在圓餅上（雙軸）、有角度與顏色的標記卡畫成另一層圓餅，各自依自己的詳細程度（較粗的另外查詢）
  與大小畫出，後面的標記卡畫在上面；沒有角度與顏色的標記卡畫成中間的洞。分面的多層圓餅只畫第一層
- **樣式**：篩選與參數控制項、dashboard 標題物件的字型、文字表中多個欄位各自的儲存格字型與文字顏色（只有
  一個欄位或自訂標籤文字中的欄位值會套用）、表頭與窗格的框線、欄的欄位標籤的背景（列的有畫）、依欄位設定的
  儲存格對齊、欄標題的垂直對齊、
  一致或各自的座標軸範圍不移轉（只影響外觀）。標記大小中文字標記的大小、依欄位決定大小時的範圍、連續座標軸（日期）上長條的寬度
  不移轉，沒有設定大小的圓餅推定為大小 1（公開報表中多數圓餅調大過）；折線寬度依兩個量測推定（預設約 2 像素、大小 14.55 約 30 像素）。沒有寫外距的區塊
  沒有外距、`display-field-labels` 與 `element='label'` 的 `display` 表示欄位標籤與離散欄位的標題、
  色帶的 `band-size` 是每段的列數、標準大小時離散欄的寬度（依內容估計）、`header-div` 是標題區域的分隔線
  （沒有寫顏色時與窗格相同）、沒有寫分隔線時預設畫在最內層以外的每一層，依公開報表推定（公開報表中最常見的
  寫法是把預設的分隔線關掉）、列標題預設靠左與文字表的數字預設靠右（依公開報表中 Tableau 存下的縮圖）、
  欄位標籤一律以半粗體顯示，依公開報表推定
- **檢視者身分的連線設定**：.twb 的 `workgroup-auth-mode`（公開範例中有 `prompt`、`as-is`）、`one-time-sql`
  （Initial SQL）與 REST API 連線的 `embedPassword`、`authenticationType` 依公開資料推定；以檢視者身分模擬
  （impersonation）的屬性值未確認，含 `imperson` 的值一律列為不符合，其他不認得的值列為需注意。
  伺服器上的連線以資料源名稱對應報表中的資料源，對不到時以連線類型對應

尚未支援（解析時列入 unsupported，報表進人工佇列）：
合併集合（XML 的寫法尚未在公開報表中看到）、預測的其他結果類型（趨勢、精確度等）與指定的彙總依據、工作表與 dashboard 的擴充功能（Extensions）、LOD 的維度本身含 LOD、SCRIPT_*／MODEL_*／RAWSQL_*、尚未併入 .tdsx 的 Published Data Source、
Relationship 模型中同一個彙總用到多張資料表的欄位、上表以外的資料庫的 Live 連線、
Live 連線的 Initial SQL、要求檢視者輸入自己的資料庫帳號或以檢視者身分模擬的連線、
Data Blending 中同一個彙總混用兩個資料源的欄位、LOD 中次要資料源的欄位、次要資料源的條件與 Top N 篩選、
標題中的 Table Calculation 值、
故事點記下的其他狀態（範圍篩選、排序、欄列架的變更、註解等）、
取消彙總的叢集與以維度分群、Teradata 的彙總中的 RANDOM()、
文字檔的資料解譯器、舊版的 .tde 萃取。

## 開發規則

- IR 是唯一的報表定義：解析器寫入，其他模組只讀，不回頭讀 .twb
- 遇到不支援的元素一律寫進 IR 的 `unsupported`，不得默默丟棄
- IR Schema 有任何欄位異動，必須升 `ir_version` 並重新產生 `kernel/schema/`
- SQL 與 Vega-Lite 規格的快照變更須人工審核：`TMIG_UPDATE_SNAPSHOTS=1 uv run pytest` 後檢查差異再提交
- 任務編號、依賴與驗證方式以 Spec 的任務清單為準
