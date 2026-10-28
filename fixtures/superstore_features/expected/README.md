# 預期結果（不是 Tableau 產生）

這個目錄的 CSV 由 `scripts/features_oracle.py` 以純 Python 獨立計算，只用來驗證實作沒有出錯；
它與 tmig 依據同一份對 Tableau 語意的理解，抓不出語意誤解。真實報表的預期結果一律由
`tools/fetch_expected` 從 Tableau Server 抓取。
