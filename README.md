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

擴充階段以功能報表 `fixtures/superstore_features`（25 張工作表、10 個 dashboard）逐項加入真實報表常用的功能：
全域篩選與篩選群組、自動尺寸的 dashboard、dashboard 上的圖片、按鈕與網頁、Measure Names／Values、同一架多個量值與雙軸、圓餅、
排除選取的標記、篩選動作（點選標記篩選其他工作表）、網址、參數與切換動作、群組與數值分組、
相對日期、條件與萬用字元篩選、在一般選取中取 Top N、差異與差異百分比、移動計算、參考線、甜甜圈與各標記卡不同的詳細程度。
25 張工作表 × 4 組篩選共 100 項計算比對全部通過，
檢視器的篩選群組、自動尺寸、各種動作、群組與篩選條件都有端到端測試。以 156 份公開的 .twb／.twbx 盤點，符合起步條件的 75 份中，
解析後沒有不支援元素、可以直接開始的從擴充前的 1 份增加到 19 份。

第三階段（來源資料庫、權限、排程）進行中。Live 連線已能查詢：工作表的 SQL 中不含 Table Calculation 的部分
轉成來源資料庫的方言下推執行，結果取回 DuckDB 再算 Table Calculation（兩段式執行）。兩份報表的萃取資料載入
PostgreSQL 與 Spark（Databricks 的引擎）、資料源改成 Live 連線後，116 項計算比對全部通過，每個公式函式也都實跑比對；
Oracle 尚無實機，以轉回 DuckDB 模擬的方式檢查（見「Live 連線」）。多表的資料源支援 join 樹、自訂 SQL 與
Relationship 模型（見「Relationship 模型」）；公開報表盤點中符合起步條件的從 75 份增加到 90 份。
萃取型的報表可以由萃取層從來源資料庫更新（完整或增量、原子替換），並與 Tableau 的萃取比對（見「萃取層」）。

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
   .tdsx（含萃取）併入報表（見「Published Data Source」）
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

## 指令

| 指令 | 用途 |
| --- | --- |
| `tmig census <twbx>... [--out-dir 目錄]` | 功能盤點與起步條件檢查（S3） |
| `tmig inventory <資料夾>... [--out-dir 目錄]` | 批次盤點：總表、功能使用統計與起步候選（S2） |
| `tmig parse <twbx> [--out IR 檔] [--lenient]` | 解析成 IR，列出不支援的元素（A2–A6） |
| `tmig import-hyper <twbx> [--out-dir 目錄]` | 萃取匯入 DuckDB 並自檢（C1） |
| `tmig sql <IR> <工作表> [--case 名稱]` | 產生的 SQL（D1–D4） |
| `tmig query <IR> <工作表> [--case 名稱] [--out CSV]` | 執行查詢，欄名為欄位鍵；缺 DuckDB 檔時自動匯入（C2） |
| `tmig cases suggest <fixture 目錄> [--write]` | 依資料建議 3 組篩選組合（E1） |
| `tmig mapping suggest <fixture 目錄>` | 預期 CSV 欄名對照建議（E2） |
| `tmig verify <fixture 目錄>` | 計算比對，寫出固定格式的差異報告（E3） |
| `tmig connections template <報表或資料夾>... [--out 檔案]` | Live 連線的設定檔範本，憑證寫成環境變數（N4） |
| `tmig connections check <報表或資料夾>... [--config 檔案] [--no-connect]` | 檢查 Live 連線的設定並測試連線（N4） |
| `tmig extract refresh <報表> [--incremental] [--out-dir 目錄]` | 從來源資料庫更新萃取的 DuckDB 檔：完整或增量、原子替換（N7） |
| `tmig extract compare <.twbx> [--data-dir 目錄]` | 新萃取與 Tableau 的萃取比對列數、總和、最小值、最大值（N7） |
| `tmig published merge <報表> [--published 目錄] [--out .twbx]` | 把 Published Data Source 的 .tdsx 併入報表（N8） |
| `tmig serve [--fixtures 目錄] [--port 8000]` | 查詢服務與檢視器（F2–F4） |
| `tmig ir validate <IR 檔>`、`tmig ir schema --out <檔案>` | 驗證 IR、匯出 JSON Schema（A1） |

## 專案結構

```text
tableau-migration/
├── kernel/src/tmig/        Python 核心函式庫
│   ├── ir/                 IR v0.3 的 Pydantic 模型、參照檢查、JSON Schema
│   ├── parser/             .twbx 解析器
│   ├── census/             功能盤點與起步條件檢查
│   ├── formula/            公式解析、語法樹、型別與層級判定
│   ├── planner/            篩選與參數狀態
│   ├── sqlgen/             公式與工作表 → DuckDB SQL（依運算順序分段）、方言轉譯
│   ├── data/               Hyper 匯入、DuckDB 唯讀執行器、Live 連線（兩段式執行、連線設定）
│   ├── render/             Vega-Lite 規格與數字格式
│   ├── verify/             cases.yaml、欄位對照、正規化、比對、差異報告
│   └── cli.py              tmig 命令列
├── kernel/schema/          IR 的 JSON Schema（由程式產生）
├── service/                FastAPI 查詢服務（tmig_service）
├── web/                    React＋TypeScript 檢視器（建置結果在 web/dist）
├── tools/                  Playwright 工具：fetch_expected、compare_screens
├── fixtures/<報表>/         .twbx、golden IR、cases、mapping、預期結果、SQL 與規格快照
└── scripts/                一鍵檢查、示範報表的建置與獨立計算
```

核心函式庫不依賴服務、前端與瀏覽器自動化；外圍只透過核心的公開介面工作。

## 已知限制與待驗證

下列 Tableau 語意依文件與公開範例 .twb 推定，示範報表的獨立計算也採用同樣的理解，
所以只有用真實報表、以 Tableau 匯出的結果比對，才能確認：

- **Table Calculation 的計算方向**：`ordering-type='Columns'` 視為 Table (down)、`'Rows'` 視為 Table (across)，
  標記卡上的維度列入定址；未寫計算方向時依預設 Table (across)，IR 標記為推定（`inferred`）
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
- **Live 連線**：與 Tableau 直接連來源資料庫時相同，字串的排序與比較依資料庫的定序、TODAY() 與 NOW() 依資料庫的時區
  （Databricks 的工作階段時區設為 UTC）、STR() 轉換小數的格式依資料庫；Oracle 的空字串即 Null、ADD_MONTHS 在月底的行為照 Oracle。
  Oracle 沒有布林型別，結果是布林值的計算（布林維度、ISNULL、CONTAINS 的結果等）與 FINDNTH 在 Oracle 尚不支援；
  PostgreSQL 的 DATEPARSE 遇到符合格式但不存在的日期（例如 13 月）會報錯而不是 Null
- **函式**：REGEXP_EXTRACT 沒有符合時為 Null；正規表示式以 RE2 執行，用到前後查看、反向參照的樣式列為不支援；
  DATENAME 的月份與星期為英文名稱（Tableau 依工作簿的地區設定，中文環境可能不同）；ISDATE 只認得 ISO 格式的日期
- **Relationship 模型**：語意依 Tableau 文件（「How Analysis Works for Multi-table Data Sources」）與公開文章推定；
  維度表中沒有量值的成員何時列出、只用到其他資料表欄位的篩選對沒有關聯的列以 Null 判斷，這兩點待真實報表確認。
  維度是一對多資料表欄位的運算式（日期部分、計算欄位）時，Tableau 在原值還是運算式的值上去重不明，列為不支援
  （查詢附帶的檢查）；關聯的效能選項（基數、參照完整性）不影響結果，一律以「多對多、部分相符」的語意查詢

尚未支援（解析時列入 unsupported，報表進人工佇列）：多層圓餅（內圈有外圈沒有的維度）、
集合與集合動作、成員別名、趨勢線、地圖、小計與總計、巢狀 LOD、百分位數與百分比等其他快速 Table Calculation、
使用者函式（USERNAME、ISMEMBEROF 等）與其他尚未實作的函式、尚未併入 .tdsx 的 Published Data Source、
Relationship 模型中量值來自多張資料表的工作表、SQL Server、MySQL、Snowflake 等其他資料庫的 Live 連線。

## 開發規則

- IR 是唯一的報表定義：解析器寫入，其他模組只讀，不回頭讀 .twb
- 遇到不支援的元素一律寫進 IR 的 `unsupported`，不得默默丟棄
- IR Schema 有任何欄位異動，必須升 `ir_version` 並重新產生 `kernel/schema/`
- SQL 與 Vega-Lite 規格的快照變更須人工審核：`TMIG_UPDATE_SNAPSHOTS=1 uv run pytest` 後檢查差異再提交
- 任務編號、依賴與驗證方式以 Spec 的任務清單為準
