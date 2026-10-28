# fixtures

每份試點報表一個目錄，目錄名稱用小寫英數字與底線（例如 `sales_overview`）。

```text
fixtures/<報表>/
├── <報表>.twbx                  從 Tableau Server 下載（S2）
├── <報表>.census.md / .json     tmig census 的盤點結果（S3）
├── golden.ir.json               人工確認過的 IR（A6）：tmig parse --out 產生
├── cases.yaml                   篩選組合：預設值加 3 組（E1）
├── mapping.yaml                 CSV 欄名 → 查詢結果欄位鍵，人工確認後 confirmed: true（E2）
├── expected/<工作表>/<組合>.csv、.png   Tableau 預期結果（E1：tools/fetch_expected）
├── sql/<工作表>.sql              SQL 快照，變更須人工審核（D1）
├── specs/<工作表>.vl.json        Vega-Lite 規格快照（F1）
├── data/<資料源>.duckdb          tmig import-hyper 產生，不進版控；.import.json 是匯入自檢紀錄
├── report/calc.json             tmig verify 的差異報告（不進版控）
└── compare/                     compare-screens 的並列頁（不進版控）
```

同一份報表的 .twbx 與預期檔，必須在同一次萃取更新之間取得。單一 .hyper 超過 100 MB 時改用 Git LFS（見根目錄 `.gitattributes`）。

`superstore_demo/` 是示範報表：以 Tableau 範例資料 Superstore 手寫的仿真 workbook，預期結果由獨立的 Python 程式計算（不是 Tableau 產生，見 `superstore_demo/expected/README.md`）。

`superstore_features/` 是擴充階段的功能報表：同一份資料，逐項加入擴充功能（全域篩選、篩選群組等），
由 `scripts/build_features_fixture.py` 產生，預期結果由 `scripts/features_oracle.py` 獨立計算（同樣不是 Tableau 產生）。
