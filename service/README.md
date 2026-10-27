# service

F2：FastAPI 查詢服務（套件 `tmig_service`）。只依賴核心函式庫 `tmig`；API 只接受篩選與參數狀態，從不接受 SQL（請求多出任何欄位一律回 422）。

```bash
uv run tmig serve                      # 讀 fixtures/，開在 http://127.0.0.1:8000
uv run tmig serve --port 9000 --fixtures 其他目錄
```

同一個網址也提供檢視器（`web/dist`）。缺少 DuckDB 檔時，第一次查詢會自動由 `.twbx` 匯入。

| 方法與路徑 | 用途 |
| --- | --- |
| `GET /api/workbooks` | fixtures 中的報表，含 dashboard 與工作表清單 |
| `GET /api/workbooks/{wb}/ir` | IR |
| `POST /api/workbooks/{wb}/worksheets/{ws}/render` | 依篩選與參數狀態查詢，回傳 Vega-Lite 規格（資料內嵌）、結果列數與資料表 |
| `POST /api/workbooks/{wb}/fields/{field}/domain` | 篩選器的可選值，或最小值與最大值 |
| `GET /api/workbooks/{wb}/cases` | cases.yaml 的篩選組合（已轉成篩選狀態），供檢視器切換 |

render 的請求只帶狀態，欄位鍵與查詢結果相同（`f_category`、`year(f_order_date)`）：

```json
{ "filters": { "f_category": { "mode": "include", "values": ["Furniture"] } }, "parameters": { "p_parameter_1": 2 } }
```

`{wb}` 是 fixtures 下的目錄名稱，`{ws}` 可用工作表 id 或名稱。
