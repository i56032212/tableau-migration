# fixtures

每份試點報表一個目錄，目錄名稱用小寫英數字與底線（例如 `sales_overview`）。

```text
fixtures/<報表>/
├── <報表>.twbx                從 Tableau Server 下載（S2）
├── <報表>.census.md / .json   tmig census 的盤點結果（S3）
├── golden.ir.json             人工確認過的 IR（A6）
├── cases.yaml                 篩選組合：預設值加 3 組（E1）
├── mapping.yaml               CSV 欄名 → IR 欄位 id（E2）
└── expected/<工作表>/<case>.csv、.png   Tableau 預期結果（E1）
```

同一份報表的 .twbx 與預期檔，必須在同一次萃取更新之間取得。單一 .hyper 超過 100 MB 時改用 Git LFS（見根目錄 `.gitattributes`）。
