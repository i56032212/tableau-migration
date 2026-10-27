# 預期結果（示範用）

這裡的 CSV **不是 Tableau 產生的**。示範報表沒有 Tableau Server 可以抓，所以由
`scripts/demo_oracle.py` 以純 Python 獨立計算：逐張工作表照 Tableau 的運算順序手寫，
不呼叫 tmig 的規劃器、SQL 或 DuckDB。

它和 tmig 依據同一份對 Tableau 語意的理解，所以只能抓出實作錯誤，抓不出語意誤解。
真實報表一律用 `tools/fetch_expected` 從 Tableau Server 抓取，取代這種做法。

重新產生：

```bash
uv run python scripts/demo_oracle.py
```
