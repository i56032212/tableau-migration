# web

F3、F4：React＋TypeScript 檢視器（Vite、vega-embed）。只負責排版與互動；查詢、篩選語意與圖表規格都由查詢服務提供。

建置結果 `web/dist` 有進版控，沒有 Node 的電腦也能直接 `uv run tmig serve` 使用；改了原始碼要重新建置後一起提交（CI 會檢查兩者一致）。

```bash
cd web
pnpm install
pnpm build        # 型別檢查＋建置到 dist/
pnpm dev          # 開發模式：http://127.0.0.1:5173，/api 與 /auth 轉給 tmig serve（8000 埠）
```

路由：

| 網址 | 內容 |
| --- | --- |
| `/` | 報表清單 |
| `/w/<報表>/d/<dashboard id>` | dashboard：區塊依 IR 的像素座標絕對定位，篩選與參數控制項、圖例 |
| `/w/<報表>/s/<工作表 id>` | 單張工作表（compare-screens 截圖用） |
| `?case=<篩選組合>` | 套用 cases.yaml 的篩選組合（與 Tableau 網址參數相同的語意） |

每張工作表右上角的「資料」可切換成資料表，直接核對數字。IR 中不支援的區塊顯示「未支援」佔位框，不會靜默略過。
