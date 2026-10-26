# tools

需要瀏覽器自動化（Playwright）的工具放這裡，核心函式庫不依賴它們。

| 目錄 | 任務 | 用途 |
| --- | --- | --- |
| `fetch_expected/` | E1 | 依 `cases.yaml` 從 Tableau 抓工作表的 CSV 與 PNG |
| `compare_screens/` | F5 | 產生 Tableau PNG 與新平台截圖的並列頁 |

這兩個工具都要在能連到 Tableau Server 的機器上由人執行，產出的檔案提交到 git 後，AI 工具直接使用。
