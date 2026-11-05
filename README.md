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

擴充階段以功能報表 `fixtures/superstore_features`（29 張工作表、11 個 dashboard、1 個 Story）逐項加入真實報表常用的功能：
全域篩選與篩選群組、自動尺寸的 dashboard、dashboard 上的圖片、按鈕與網頁、Measure Names／Values、同一架多個量值與雙軸、圓餅、
排除選取的標記、篩選動作（點選標記篩選其他工作表）、網址、參數與切換動作、群組與數值分組、
相對日期、條件與萬用字元篩選、在一般選取中取 Top N、差異與差異百分比、移動計算、參考線、甜甜圈與各標記卡不同的詳細程度、
Story（故事點記下的篩選動作、篩選與選取）、集合（Top N、固定成員與條件集合）與集合動作。
29 張工作表 × 4 組篩選共 116 項計算比對全部通過，
檢視器的篩選群組、自動尺寸、各種動作、群組與篩選條件、Story、集合與集合動作都有端到端測試。以 156 份公開的 .twb／.twbx 盤點，符合起步條件的 75 份中，
解析後沒有不支援元素、可以直接開始的從擴充前的 1 份增加到 19 份。

第三階段（來源資料庫、權限、排程）的功能都已完成，連線設定待公司環境實測（N4）。Live 連線已能查詢：工作表的 SQL 中不含 Table Calculation 的部分
轉成來源資料庫的方言下推執行，結果取回 DuckDB 再算 Table Calculation（兩段式執行）。兩份報表的萃取資料載入
PostgreSQL 與 Spark（Databricks 的引擎）、資料源改成 Live 連線後，116 項計算比對全部通過，每個公式函式也都實跑比對；
Oracle 尚無實機，以轉回 DuckDB 模擬的方式檢查（見「Live 連線」）。多表的資料源支援 join 樹、自訂 SQL 與
Relationship 模型（見「Relationship 模型」）；公開報表盤點中符合起步條件的從 75 份增加到 90 份。
萃取型的報表可以由萃取層從來源資料庫更新（完整或增量、原子替換），並與 Tableau 的萃取比對（見「萃取層」）。
以使用者函式或使用者篩選限制可見資料的報表，以登入者的身分查詢，網址參數改不了權限篩選（見「列層級權限」）；
查詢服務與檢視器可以要求登入（反向代理標頭或 OIDC，開發模式可切換使用者；見「身分驗證」），並依 Tableau Server 匯出的
Project、workbook、view 權限開放（見「權限」）。排程器依 Tableau Server 匯出的排程執行萃取更新，以訂閱者的身分產生
PDF、PNG 寄出訂閱（見「排程與訂閱」）；每次檢視記錄使用者、報表與時間（見「使用紀錄」）。
一份報表可以有多個資料源（各工作表用各自的資料源，網址參數套用到所有資料源中同名的欄位）；同一張工作表混用多個資料源的
Data Blending 尚不支援。依檢視者的身分連線資料庫的報表（沒有嵌入密碼、Initial SQL 帶入登入者），移轉後資料庫端的權限
不會生效，盤點時列為不符合起步條件（見「依檢視者的身分連線」）。以 156 份公開報表重新盤點：放寬多個資料源後 12 份
轉為符合起步條件；28 份要求檢視者輸入自己的資料庫帳號（Tableau Server Insights、TabMon 等連到 Tableau 資料庫的範例）
改列為不符合，3 份以 Windows 整合驗證連線列為需注意。

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
集合動作（點選標記改變集合的成員）也已支援（見「已知限制與待驗證」）。樣式（文字顏色、字型大小、自訂色盤）不在移轉範圍，
深色背景的報表上標題與文字會看不清楚（見「已知限制與待驗證」）。

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

資料源是 Live 連線的報表（Oracle、Databricks、PostgreSQL），查詢時連到來源資料庫：

1. `uv run tmig connections template <報表或資料夾> --out connections.toml` 產生連線設定檔範本
2. 帳號、密碼、權杖在設定檔中一律寫成 `${環境變數}`（直接寫值會被拒絕），由部署環境或密碼管理服務提供；
   請使用唯讀的服務帳號。設定檔的位置由環境變數 `TMIG_CONNECTIONS` 指定，預設為目前目錄的 `connections.toml`
3. `uv run tmig connections check <報表或資料夾>` 檢查設定、環境變數，並以唯讀查詢測試連線
4. 之後 `tmig verify`、`tmig query`、`tmig serve` 遇到 Live 連線都會依設定檔連線

連線驅動依實際使用的資料庫安裝：`uv sync --extra oracle`、`--extra databricks`、`--extra postgres`。
IR 只記連線類型、主機、資料庫與資料表，不記任何憑證。

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

- PostgreSQL（16 以上）、Spark（3.4 以上，Databricks 的引擎）：每個公式函式與兩份報表的每張工作表 × 每組篩選實跑比對
  （`kernel/tests/sqlgen/test_engines.py`；本機有 PostgreSQL 時自動執行，Spark 需要 `uv sync --group spark` 與 Java 17 以上）
- Oracle（12.2 以上）：尚無實機。轉譯後的 SQL 以 SQLGlot 轉回 DuckDB、Oracle 特有的函式依官方文件的語意模擬後執行比對，
  並保留轉譯快照（`fixtures/*/sql/oracle/`）供審核；正式使用前須在公司的 Oracle 實測

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
  否則列為不支援，不給出與 Tableau 不同的數字

尚不支援（查詢時列為不支援）：同一張工作表的量值來自多張資料表、同時用到量值表與其他資料表（或兩個分支）欄位的篩選；
Live 連線的邏輯資料表內含實體層的 join、關聯條件含計算（解析時列入 unsupported）。

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

## 萃取層

萃取型的報表平常讀 DuckDB 檔（由 .twbx 內的 .hyper 匯入）。`tmig extract refresh` 依萃取各欄在來源的位置
（萃取 metadata-record 的 family 與 remote-alias）從來源資料庫重建（`kernel/src/tmig/data/extract_refresh.py`）：

1. 連線設定與 Live 連線相同；`tmig connections template` 也會列出萃取的來源（標示「萃取更新」）
2. `uv run tmig extract refresh <報表> [--out-dir data]`：完整更新；`--incremental` 只附加增量欄位
   （Tableau 的 increment-key）大於現有最大值的列，與 Tableau 的增量更新相同。寫入暫存檔後原子替換，
   查詢中的讀取不受影響，失敗時舊檔不變；萃取篩選照套，欄位型別沿用現有檔案（.hyper 匯入時的型別）
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
  其他人拿到的版本不含連線、公式、資料源篩選與原始 XML
- 報表依 cases.yaml 的 `tableau_views_url`（Tableau 上的網址名稱）或報表名稱對應到 Tableau 的 workbook；
  權限檔中找不到的報表只有管理員看得到

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
  再從現在起算下一次。同時到期的工作依 Tableau 的優先順序（數字小的先）逐一執行，一個失敗不影響其他工作；
  每次的結果（到期、開始、結束時間、成功或失敗與說明）逐行寫入 `runs.jsonl`

訂閱（`service/src/tmig_service/subscriptions.py`）：排程器加上 `--mail mail.toml`（範本見 `service/mail.example.toml`）與
`--auth auth.toml`（使用者目錄與權限）後執行訂閱的工作：

- 以訂閱者的身分開啟檢視器（排程器在本機另外啟動一個只接受簽章工作階段的查詢服務），列層級權限與權限都照套；
  沒有權限、不在使用者目錄中或沒有 email 時這次訂閱失敗並記錄原因
- view 的訂閱為該 dashboard 或工作表，整份報表的訂閱為每個 dashboard（沒有時為每張工作表）；
  圖片內嵌在郵件內文、PDF（依紙張大小與方向）合併成一個附件，內文附每個 view 的連結
- view 沒有資料、而訂閱設定為不寄送時不寄（與 Tableau 的「view 為空時不傳送」相同）

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
| `tmig usage summary <使用紀錄目錄> [--fixtures 目錄] [--days 90] [--csv 檔案]` | 每份報表的檢視次數、使用者數與最後一次檢視（R1） |
| `tmig serve [--fixtures 目錄] [--port 8000] [--auth 設定檔] [--usage-dir 目錄]` | 查詢服務與檢視器（F2–F4；身分驗證與使用紀錄見各節） |
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
│   ├── sqlgen/             公式與工作表 → DuckDB SQL（依運算順序分段）、方言轉譯
│   ├── data/               Hyper 匯入、DuckDB 唯讀執行器、Live 連線（兩段式執行、連線設定）
│   ├── render/             Vega-Lite 規格與數字格式
│   ├── verify/             cases.yaml、欄位對照、正規化、比對、差異報告
│   └── cli.py              tmig 命令列
├── kernel/schema/          IR 的 JSON Schema（由程式產生）
├── service/                FastAPI 查詢服務（tmig_service：身分驗證、權限、訂閱、使用紀錄）
├── web/                    React＋TypeScript 檢視器（建置結果在 web/dist）
├── tools/                  連 Tableau Server 的工具：tableau_server（盤點、下載、匯出）、fetch_expected、compare_screens
├── fixtures/<報表>/         .twbx、golden IR、cases、mapping、預期結果、SQL 與規格快照
└── scripts/                一鍵檢查、示範報表的建置與獨立計算
```

核心函式庫不依賴服務、前端與瀏覽器自動化；外圍只透過核心的公開介面工作。

## 已知限制與待驗證

下列 Tableau 語意依文件與公開範例 .twb 推定，示範報表的獨立計算也採用同樣的理解，
所以只有用真實報表、以 Tableau 匯出的結果比對，才能確認：

- **Table Calculation 的計算方向**：`ordering-type='Columns'` 視為 Table (down)、`'Rows'` 視為 Table (across)，
  標記卡上的維度列入定址；未寫計算方向時依預設 Table (across)，IR 標記為推定（`inferred`）
- **巢狀 Table Calculation**：視窗函式不能巢狀，內層（被參照的 Table Calculation 計算欄位，或寫在另一個
  Table Calculation 函式參數中的函式）先算成欄位，外層再引用。被參照的計算欄位依欄位實例上
  `<table-calc field=…>` 的巢狀計算方向計算，沒有這項設定時與外層相同（依公開資料推定）；
  同一個公式中直接巢狀的函式依同一個計算方向。HR 人資報表「Education & Performance」的醒目標示
  （巢狀計算方向與外層不同）與 Tableau 截圖相同
- **LOD 的彙總**：彙總參數只含 LOD 時，在「檢視維度 ∪ LOD 維度」的組合上去重後彙總；含列層級欄位時逐列彙總
- **Top N**：同名次時取維度值較小者，數量剛好 N；內容 Top N 以其他內容篩選後的資料排名
- **TOTAL()**：以量值篩選後仍在檢視中的標記所對應的底層資料重新彙總
- **週起始日**：.twb 沒寫時視為星期日
- **預期 CSV 的格式**：Tableau 匯出的欄名與數值格式依版本與語系而異；欄名用 mapping.yaml 對照，
  帶格式的數值以顯示精度比對
- **篩選動作**：以目標工作表動作篩選中的欄位篩選，但只用每個選取的標記都有的欄位
  （一個都沒有時視同沒有選取）；Measure Names 不傳給目標；跨資料源的動作列為不支援。
  網址參數另成一個篩選，不會取代篩選動作；計算比對的篩選組合不含選取（Tableau 的匯出無法指定選取）
- **網址與參數動作**：網址中的欄位代入原始值（數字取 12 位有效數字），一律在新分頁開啟；
  參數動作的 XML（source-field、target-parameter）依公開資料推定，選取多個標記時依彙總方式計算
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
- **篩選動作的舊參照**：動作刪掉重建後，工作表上留下參照舊動作的篩選（公開報表 Practice Activity Number 4），
  Tableau 照樣套用它，之後執行的動作寫入同一個欄位、取代它。新平台把它連到唯一會寫入這張工作表的篩選動作
  （目標 dashboard 上扣掉 exclude 與來源的工作表；沒有或有多個時維持靜態篩選），依 XML 結構推定
- **Story**：故事點顯示的狀態取自擷取時記下的差異（`<capturedDeltas>`）；擷取後又改過、沒有按「更新」時取改過的
  狀態（`<currentDeltas>`），推定開啟報表時看到的是它。篩選動作的篩選轉成以記下的標記執行動作（同一個動作在
  每張目標工作表各記一次，值相同）、一般篩選取代同一欄位的篩選（與網址參數相同）、選取轉成醒目顯示的標記。
  在故事點上的操作只影響目前的畫面，切換故事點即回到記下的狀態（Tableau 會保留改過的狀態並提供「還原」）。
  導覽樣式（說明文字、序號、圓點）與箭頭照 .twb；Story 的尺寸不強制，故事點的 dashboard 以自己的尺寸顯示

- **群組與數值分組**：群組沒有「其他」時，未列入的成員維持原值；數值分組的值為所在區間的下限
  （FLOOR((值 − 起點) ÷ 大小) × 大小 ＋ 起點），與 Tableau 的區間邊界一致與否待真實報表確認

- **相對日期**：基準日為 cases.yaml 的 `today`，沒有時為抓取預期結果的日期（`fetched_at`），檢視器與計算比對相同；
  期間一律取完整的期間（例如最近 3 個月含本月全月），`include-future='false'` 時只到基準日
- **條件篩選與 Top N**：彙總條件（例如總銷售額大於 5000）與 Top N 一樣，以內容篩選後、其他維度篩選
  （含網址參數）之前的資料計算；先選取的成員（篩選對話框的「一般」頁）中再取 Top N
- **差異與差異百分比**：與 Tableau 轉成計算欄位時相同，`ZN(x) − LOOKUP(ZN(x), −1)`，百分比再除以
  `ABS(LOOKUP(ZN(x), −1))`；與第一個、最後一個比較尚不支援
- **移動計算**：`from`、`to` 視為相對於目前標記的位置（含兩端），例如 −2、0 為前兩個與目前的值，
  與 `WINDOW_AVG(x, −2, 0)` 相同；視窗超出分割範圍時只取現有的標記，「不足時為 Null」則為 Null。
  未勾選「目前的值」時 XML 的寫法待真實報表確認
- **dashboard 按鈕**：切換按鈕依 `<windows>` 的 simple-id 找目標；顯示／隱藏按鈕的第一個外觀視為區塊顯示中、
  第二個為隱藏時（待真實報表確認）；匯出 PDF 以瀏覽器列印，匯出圖片與 PowerPoint 不支援（按鈕停用，不影響數字）；
  網頁區塊只嵌入 http(s) 網址
- **各標記卡的詳細程度不同**（例如甜甜圈、疊在長條上依客群分色的折線）：資料表與計算比對以工作表上所有維度的
  組合計算（與只有一個標記卡時相同）；圖表中較粗的標記卡（例如甜甜圈中間的合計）各自以自己的維度查詢。
  Tableau 匯出這類工作表的格式待真實報表確認；甜甜圈中間的洞大小固定，分面的甜甜圈中間的標籤尚未畫出
- **參考線**：值在新平台以圖上標記的值計算（平均、總和、最小、最大、中位數、百分位數以線性內插、
  平均 ± 標準差的倍數）；窗格為欄列架上的離散維度，但各架最內層若是離散維度則不算（它切出的是儲存格）。
  合計（Total）只在量值為 SUM、COUNT、MIN、MAX 時畫出；參考區間只畫兩端的線、不畫底色，
  分布的百分比、分位數、信賴區間與盒鬚圖不畫；同一條參考線有多個範圍時不顯示標籤
- **總計**：列架的總計（`<rows total='true'>`）是最下方的一列、欄架的總計是最右邊的一欄，兩者都有時加上右下角；
  與 Tableau 的「自動」相同，另外查詢去掉該架上離散維度的詳細程度（標記卡上的維度保留、篩選照樣套用、
  INCLUDE／EXCLUDE 相對於總計的詳細程度），不是把畫面上的值相加。依量值著色時總計不上色、不影響色階範圍，
  色彩設定「包含總計」時才上色；總計不能點選、不畫參考線。資料表的最後幾列是總計；計算比對只比工作表的標記，
  總計的數字以公開報表的截圖確認。總計的計算方式改成畫面上的值再彙總（Total Using）、總計中的 Table Calculation、
  與量值篩選並用時算不出與 Tableau 相同的數字，前者在解析時、後兩者在查詢時列為不支援（盤點也會列出）；小計尚不支援。
  XML 的寫法（哪個架的 total 對應哪個方向）以 HR 分析報表的截圖確認
- **Live 連線**：與 Tableau 直接連來源資料庫時相同，字串的排序與比較依資料庫的定序、TODAY() 與 NOW() 依資料庫的時區
  （Databricks 的工作階段時區設為 UTC）、STR() 轉換小數的格式依資料庫；Oracle 的空字串即 Null、ADD_MONTHS 在月底的行為照 Oracle。
  Oracle 沒有布林型別，結果是布林值的計算（布林維度、ISNULL、CONTAINS 的結果等）與 FINDNTH 在 Oracle 尚不支援；
  PostgreSQL 的 DATEPARSE 遇到符合格式但不存在的日期（例如 13 月）會報錯而不是 Null
- **函式**：REGEXP_EXTRACT 沒有符合時為 Null；正規表示式以 RE2 執行，用到前後查看、反向參照的樣式列為不支援；
  DATENAME 的月份與星期為英文名稱（Tableau 依工作簿的地區設定，中文環境可能不同）；ISDATE 只認得 ISO 格式的日期。
  FLOAT、INT 把日期轉成數值，DATE、DATETIME 把數值轉成日期時，以 1899-12-30 為第 0 天、小數部分是一天中的時間
  （與 Excel 相同，依公開資料推定）；TabMon 等報表常見的「捨去到 N 分鐘」與兩個時間相減的寫法不受第 0 天影響
- **Relationship 模型**：語意依 Tableau 文件（「How Analysis Works for Multi-table Data Sources」）與公開文章推定；
  維度表中沒有量值的成員何時列出、只用到其他資料表欄位的篩選對沒有關聯的列以 Null 判斷，這兩點待真實報表確認。
  維度是一對多資料表欄位的運算式（日期部分、計算欄位）時，Tableau 在原值還是運算式的值上去重不明，列為不支援
  （查詢附帶的檢查）；關聯的效能選項（基數、參照完整性）不影響結果，一律以「多對多、部分相符」的語意查詢
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
- **樣式**：文字的顏色、字型與大小，工作表與區塊的背景色、自訂色盤都不移轉（只影響外觀）；深色背景圖片上的
  標題、文字區塊與篩選控制項名稱因此看不清楚。多個列標題合併成一欄顯示（以「｜」分隔）
- **檢視者身分的連線設定**：.twb 的 `workgroup-auth-mode`（公開範例中有 `prompt`、`as-is`）、`one-time-sql`
  （Initial SQL）與 REST API 連線的 `embedPassword`、`authenticationType` 依公開資料推定；以檢視者身分模擬
  （impersonation）的屬性值未確認，含 `imperson` 的值一律列為不符合，其他不認得的值列為需注意。
  伺服器上的連線以資料源名稱對應報表中的資料源，對不到時以連線類型對應

尚未支援（解析時列入 unsupported，報表進人工佇列）：多層圓餅（內圈有外圈沒有的維度）、
合併集合與合併欄位（Combined Field）、趨勢線、地圖、小計、巢狀 LOD、百分位數與百分比等其他快速
Table Calculation、尚未實作的函式、尚未併入 .tdsx 的 Published Data Source、
Relationship 模型中量值來自多張資料表的工作表、SQL Server、MySQL、Snowflake 等其他資料庫的 Live 連線、
Live 連線的 Initial SQL、要求檢視者輸入自己的資料庫帳號或以檢視者身分模擬的連線、Data Blending、
總計的 Total Using、總計中的 Table Calculation 或與量值篩選並用、標題中的 Table Calculation 值、
故事點記下的其他狀態（參數、範圍篩選、排序等）、導覽到 Story 的按鈕。

## 開發規則

- IR 是唯一的報表定義：解析器寫入，其他模組只讀，不回頭讀 .twb
- 遇到不支援的元素一律寫進 IR 的 `unsupported`，不得默默丟棄
- IR Schema 有任何欄位異動，必須升 `ir_version` 並重新產生 `kernel/schema/`
- SQL 與 Vega-Lite 規格的快照變更須人工審核：`TMIG_UPDATE_SNAPSHOTS=1 uv run pytest` 後檢查差異再提交
- 任務編號、依賴與驗證方式以 Spec 的任務清單為準
