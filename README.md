# Tableau 移轉平台

自建平台取代 Tableau Server。起步階段先把兩份報表從 .twbx 端到端移轉到新平台，並以自動化比對證明數字一致。

- 需求：[PRD](https://claude.ai/artifact/GyVE4S5wfjU6gQ5mCuCni1)
- 系統設計：[SDD](https://claude.ai/artifact/4FBduqgDhETHytMMagNowr)
- 本階段規格與任務清單：[起步 Spec](https://claude.ai/artifact/AuVmsCT9wWbRjZpnMeppVu)

## 專案結構

```text
tableau-migration/
├── grammar/                TableauFormula.g4（B1）
├── kernel/                 Python 核心函式庫 tmig
│   ├── src/tmig/           ir、parser、census、formula、planner、sqlgen、data、render、verify、cli
│   ├── schema/             IR 的 JSON Schema（由程式產生）
│   └── tests/
├── service/                FastAPI 查詢服務（F2）
├── web/                    React＋TypeScript 檢視器（F3、F4）
├── tools/                  Playwright 工具：fetch_expected（E1）、compare_screens（F5）
├── fixtures/<報表>/         兩份試點報表的 .twbx、預期結果與 golden IR
└── scripts/check.py        一鍵檢查
```

核心函式庫不依賴服務、前端與瀏覽器自動化；外圍只透過核心的公開介面工作。

## 快速開始

需要 [uv](https://docs.astral.sh/uv/)。Windows 可用 `winget install --id=astral-sh.uv -e` 安裝。

```bash
uv sync                              # 安裝 Python 3.12 與所有相依套件
uv run python scripts/check.py       # 一鍵檢查：ruff、pyright、pytest
uv run python scripts/check.py --fix # 先自動修正 lint 與格式
```

第一次執行 pyright 會從 npm registry 下載 pyright 本體；公司網路若擋 npm，需要設定代理。

## 盤點試點報表（S3）

把兩份候選報表的 .twbx 放進 `fixtures/<報表>/`，再執行：

```bash
uv run tmig census fixtures/sales_overview/sales_overview.twbx --out-dir fixtures/sales_overview
```

產出 `<報表>.census.md`（給人看）與 `<報表>.census.json`（完整資料）。報告內容：

- 起步條件：單一資料源、資料在 .twbx 的萃取檔內、單表萃取、無 RLS、無 Blending、固定尺寸 dashboard 等八項，逐項標示通過、注意或不符合
- 分級：A（只有一般計算）、B（含 LOD 或 Table Calculation）、C（Blending、自訂 SQL、Story、地圖）
- 用到的函式與是否在 v0 函式集、每個計算欄位的公式、工作表的標記與篩選、dashboard 區塊、參數與動作

盤點只讀 .twb 的 XML，不讀萃取資料。報表選定後，把 census 列出、v0 尚未涵蓋的函式與功能補登為任務。

## 其他指令

```bash
uv run tmig ir validate <IR 檔>                               # 驗證 IR
uv run tmig ir schema --out kernel/schema/ir-0.2.schema.json   # 更新 JSON Schema
```

## 開發規則

- IR 是唯一的報表定義：解析器寫入，其他模組只讀，不回頭讀 .twb
- 遇到不支援的元素一律寫進 IR 的 `unsupported`，不得默默丟棄
- IR Schema 有任何欄位異動，必須升 `ir_version` 並重新產生 `kernel/schema/`
- 任務編號、依賴與驗證方式以 Spec 的任務清單為準；每個 commit 對應一個任務
