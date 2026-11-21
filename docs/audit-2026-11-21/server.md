# 伺服器端稽核報告（檢視器、內容管理、權限、排程、訂閱、站台管理）

> 這份報告是 2026-11-21 使用體驗稽核的原始紀錄，由稽核用的子代理實際操作產品後寫成，只稽核、沒有改程式。
> 這台電腦沒有 Tableau，「Tableau 的做法」依對 Tableau 2021.2 的了解，沒有並排比對。
> 證據（截圖、下載的檔案、收到的信）沒有進版控，在本機 repo 資料夾下的 `audit-evidence/server/`；
> 文中的 `shots/…` 等檔名都在那個資料夾。彙整與後續處理見 `docs/backlog.md`。

稽核日期：2026-11-21。對照基準：Tableau Server 2021.2 的使用經驗。只稽核、沒有修改 repo 的任何檔案（`git status` 只有原本就未追蹤的 .twbx）。

## 怎麼測的

- 伺服器：`tmig_service.app.create_app`（`web/dist` 現成的建置），dev 模式登入、7 位使用者（amy 站台管理員、carol Creator、erin Explorer、bob／alice／dave Viewer、uma 未授權）、3 個群組、4 個 Project（預設、Sales、Sales／華北、Finance 鎖定）、報表庫與使用紀錄。啟動程式：`serve.py`。
- 報表：`superstore_features`（31 張工作表、12 個 dashboard、1 個 Story，當作從 Tableau 移轉的報表）、`secure`（示範報表加列層級權限，名稱「區域銷售」）、由桌面版引擎實際發布的示範報表（`engine.py`、`e01.py`）。
- 操作：Playwright（Chromium 無頭、1440×900；手機 390×844），每一步都經由真正的畫面。指令稿 `a0*.py`～`i0*.py`，截圖在 `shots/`，下載的檔案在 `downloads/`，收到的信在 `mail/`。
- 郵件：測試用的假 SMTP（`smtp_sink.py`，做法同 `service/tests/test_subscriptions.py`），排程器以 `tmig schedule tick --store … --mail mail.toml` 執行一次。
- 另外開過兩個伺服器：工作階段 14 秒的 dev 模式（測逾時）、proxy 模式（以 `X-Forwarded-User` 標頭模擬 SSO 閘道）。
- 為了測單選、滑桿、套用按鈕等功能報表裡沒有的控制項，在 scratch 的 fixtures 複本上改了第一個 dashboard 的控制項類型（`mkcontrols.py`，報表「控制項測試」）。這幾列在表中註明「改過的複本」。
- 對 Tableau 行為的描述以我對 Tableau Server 2021.2 的了解為準；不確定的地方在該列註明。查過文件的兩處：
  分享修改過的檢視會產生帶狀態的連結（https://help.tableau.com/current/pro/desktop/en-us/shareworkbooks.htm ）、
  網址參數篩選（https://help.tableau.com/current/pro/desktop/en-us/embed_structure.htm ）。兩頁都是目前版本的文件，不是 2021.2 專屬的版本。
- 載入時間是本機 DuckDB、約 1 萬列的萃取，不代表公司的資料量。

證據的位置（以下只寫檔名）：
`C:/Users/User/AppData/Local/Temp/claude/ab7db37d4fb4f2e8/040c1e1a-3ae3-4f01-98e4-1972b6443e73/scratchpad/audit_server/` 之下的 `shots/`、`downloads/`、`mail/`。

## 逐項結果

共 127 列：相同 37、可用但不同 51、缺少 25、壞掉 8、無法實測 6。嚴重度：高 13、中 34、低 37（相同與無法實測不標）。

| 編號 | 角色 | 情境（Tableau Server 的做法） | 實測結果（我們的做法與發生的事） | 判定 | 嚴重度 | 證據 |
| --- | --- | --- | --- | --- | --- | --- |
| A1 | Viewer | 登入頁：帳號密碼或 SSO | dev 模式是「開發模式：請選擇要以哪個使用者檢視」的下拉選單；proxy 模式沒有登入頁（由 SSO 閘道帶入帳號）。平台沒有本機帳號密碼的登入 | 可用但不同 | 低 | A01_signin.png、A10_proxy_bob.png |
| A2 | Viewer | 密碼打錯時的訊息 | 平台沒有密碼登入（模式只有 none／dev／proxy／oidc）；OIDC 需要外部的身分提供者，沒有可用的假服務 | 無法實測 | — | `auth.py` 的模式 |
| A3 | Viewer | SSO 已登入、但站台沒有這個帳號：告知帳號沒有權限 | proxy 模式以不在目錄中的帳號 zed 進入：畫面寫「沒有登入者：請經由公司的登入入口開啟這個網站」；API 其實回「帳號 zed 不在使用者目錄中（Tableau Server 的使用者）」。訊息讓人以為沒登入 | 壞掉 | 中 | A10_proxy_unknown_user.png |
| A4 | Viewer | 登出（右上角的使用者選單） | dev 模式有「登出」，回到選使用者的畫面，按上一頁也回不到內容。proxy 模式右上角只有姓名，沒有登出也沒有使用者選單 | 可用但不同 | 低 | A10_proxy_bob.png |
| A5 | Viewer | 未登入開深層連結，登入後回到原本的網址 | 選了使用者後留在原本的網址並開啟該 view | 相同 | — | A02_deeplink_unauth.png、A03_after_login_deeplink.png |
| A6 | Viewer | 首頁（Home）與 Explore：卡片、縮圖、最愛、最近 | 首頁是一張平面清單：每份報表一個方塊，裡面是所有 dashboard、Story、工作表的文字連結（功能報表一塊就有 44 個連結）；上面有「我的最愛」「最近檢視」兩排文字連結 | 可用但不同 | 高 | A04_home_bob.png、A20_home_bob_favorites.png |
| A7 | Viewer | 依 Project、巢狀 Project 瀏覽內容 | 檢視者的畫面完全沒有 Project（只有站台管理看得到樹）；`/api/workbooks` 有 `project_name`，首頁沒有顯示 | 缺少 | 高 | F01_home_carol_after_publish.png |
| A8 | Viewer | 清單／格狀切換、縮圖 | 沒有縮圖，沒有格狀檢視 | 缺少 | 中 | A04_home_bob.png |
| A9 | Viewer | 排序（名稱、擁有者、修改時間、檢視次數） | 沒有排序，也看不到擁有者與修改時間 | 缺少 | 中 | A04_home_bob.png |
| A10 | Viewer | 搜尋：名稱、擁有者等，有結果頁 | 輸入時即時過濾報表、dashboard、工作表的名稱；找不到時「找不到「…」。」。不能依擁有者（搜尋 carol 找不到）、Project、資料源；按 Enter 沒有結果頁 | 可用但不同 | 中 | A06_search_profit.png |
| A11 | Creator | 發布時取的名稱出現在內容清單與檢視的標題 | 發布成「銷售示範」「華北週報」「季度銷售」的三份報表，在首頁全部叫「superstore_demo」（.twbx 裡的名稱），分不出來；管理員把報表改名後首頁照舊；搜尋「銷售示範」得到「找不到」；檢視器工具列也寫 superstore_demo。只有站台管理、版本對話框、資料源頁用發布的名稱 | 壞掉 | 高 | F01_home_carol_after_publish.png、E07_published_report_view.png |
| A12 | Viewer | 我的最愛：清單上按星號，Favorites 頁可以移除 | view 的工具列有「☆ 我的最愛」，首頁列出。首頁上不能移除（要打開那個 view 再按一次）；不能把整份報表加入 | 可用但不同 | 低 | A20_home_bob_favorites.png |
| A13 | Viewer | 最近檢視 | 首頁列出最近看過的 view（文字連結，沒有縮圖） | 相同 | — | A20_home_bob_favorites.png |
| A14 | Viewer | 導覽路徑：Explore／Project／Workbook／View，每層可點 | 只有「報表 / 報表名稱」兩層，報表名稱不是連結，沒有 Project | 可用但不同 | 低 | A07_workbook_page_bob.png |
| A15 | Viewer | workbook 頁：各 view 的縮圖、資料源、詳細資料、說明、標籤、擁有者、修改時間 | 沒有 workbook 頁：`/w/<報表>` 直接開第一個 dashboard。看不到擁有者、修改時間、說明、標籤 | 缺少 | 高 | A07_workbook_page_bob.png |
| A16 | Viewer | 瀏覽器分頁的標題是 view 的名稱 | 每一頁的標題都是「Tableau 移轉檢視器」，開多個分頁分不出來 | 可用但不同 | 低 | `e02.py` 的輸出（document.title） |
| A17 | Viewer | 未授權的帳號看不到內容 | uma 登入後首頁「沒有可以檢視的報表；請向報表的擁有者申請權限。」 | 相同 | — | A05_home_uma.png |
| B1 | Viewer | dashboard 的載入時間 | 12 個 dashboard 各 0.2–0.3 秒；切入到 1,849 列的長條圖 2.5 秒（3.9 萬個 SVG 節點）。本機小資料，不代表正式環境 | 相同 | — | `b01.py`、`b12.py` 的輸出 |
| B2 | Viewer | 多選下拉：選單浮在 dashboard 上面，完整顯示、可捲動 | 選單被篩選區塊的高度裁切。「篩選條件」dashboard 的 Sub-Category（17 個值加搜尋框，內容高 409 像素）只露出 37 像素，要在這一小條裡捲動；「功能總覽」的三個下拉各 4–5 個選項只露出 3 列 | 壞掉 | 高 | B27_dropdown_clipped_zoom.png、B28_dropdown_Segment.png |
| B3 | Viewer | 多選下拉：點別的地方就收起來 | 點 dashboard 的空白處不收；焦點離開後按 Esc 也不收；只能再按一次下拉鈕 | 壞掉 | 中 | B03_control_hover.png |
| B4 | Viewer | 多選下拉：（全部）、搜尋 | 有「（全部）」，成員多時有搜尋框，勾選後立即重新查詢 | 相同 | — | B01_multidropdown_open.png |
| B5 | Viewer | 單選下拉 | 原生的下拉選單，可用（改過的複本） | 相同 | — | B60_controls.png |
| B6 | Viewer | 單選清單：一排圓鈕 | 畫成下拉選單，與單選下拉沒有差別（改過的複本） | 可用但不同 | 低 | B60_controls.png |
| B7 | Viewer | 數值範圍：雙把手的滑桿 | 沒有滑桿，是兩個數字輸入框「最小～最大」；下限顯示成 0.44399999（改過的複本） | 可用但不同 | 中 | B60_controls.png、B64_controls_zoom.png |
| B8 | Viewer | 相對日期篩選：檢視者可以改成最近 N 天／月／年 | 唯讀的文字「最近 3 個月」，滑鼠提示「此篩選在 Tableau 中設定，檢視器尚不能修改」 | 缺少 | 高 | B25_relative_date_click.png |
| B9 | Viewer | 萬用字元／條件篩選：檢視者輸入文字 | 唯讀，而且直接顯示公式「條件：CONTAINS(LOWER([Customer Name]), LOWER('son'))」 | 缺少 | 中 | B00_dash_05.png |
| B10 | Viewer | 套用按鈕 | 有「套用／取消」：勾選後畫面不變，按套用才查詢。另有「排除」的勾選框（有顯示，沒有操作到）（改過的複本） | 相同 | — | `b11.py` 的輸出、B60_controls.png |
| B11 | Viewer | 篩選卡右上角的選單（只顯示相關值／全部值、包含／排除等） | 控制項沒有選單，檢視者不能自己切換 | 缺少 | 低 | B03_control_hover.png |
| B12 | Viewer | 「只顯示相關值」的效果 | 複本上設了 `relevant`，但沒有獨立驗證可選值是否真的縮減 | 無法實測 | — | — |
| B13 | Viewer | 日期範圍控制項 | 複本上加的日期範圍區塊沒有畫出來（多半是我改的版面問題），沒有測到 | 無法實測 | — | B60_controls.png |
| B14 | Viewer | 參數控制項 | 下拉選單，顯示別名（中區、東區…），0.17 秒重新查詢；參數動作（點標記改參數）有作用 | 相同 | — | B30_parameter.png、B31_parameter_changed.png |
| B15 | Viewer | 篩選動作：點標記篩選其他工作表，點空白處取消 | 0.19 秒，點空白處取消。按 Esc 只關掉工具提示、不取消選取（Tableau 按 Esc 取消選取） | 相同 | — | B20_filter_action_selected.png |
| B16 | Viewer | 醒目提示動作與醒目提示控制項 | 控制項可以輸入或選值，其他標記變淡 | 相同 | — | B23_highlighter_applied.png |
| B17 | Viewer | URL 動作：連結出現在工具提示裡（或選取時直接開啟） | 點標記後另外跳出一個小選單「Search Region／關閉」，連結正確代入值（`region=East&sales=678781.24`） | 可用但不同 | 低 | B20_filter_action_selected.png |
| B18 | Viewer | 導覽按鈕／前往工作表 | 按鈕切換到目標 dashboard，瀏覽器上一頁、下一頁正常 | 相同 | — | `b06.py` 的輸出 |
| B19 | Viewer | dashboard 的顯示／隱藏按鈕、網頁物件 | 按鈕顯示隱藏的篩選區塊；網頁物件（iframe）載入 | 相同 | — | B32_home_toggle_shown.png |
| B20 | Viewer | dashboard 上的「匯出 PDF」按鈕：下載 PDF | 直接呼叫瀏覽器的列印（沒有對話框、沒有檔案） | 可用但不同 | 低 | B33_home_export_pdf_btn.png |
| B21 | Viewer | 工具提示的內容與格式 | 立即出現，工具提示中的工作表也有。日期欄位寫成「MONTH(Order Date)　2024-11」（Tableau 是「Month of Order Date: November 2024」）；Measure Names 的折線同時列出 Sales、Profit、Measure Names、Measure Values | 可用但不同 | 中 | B42_line_tooltip.png、B08_lasso_done.png |
| B22 | Viewer | 工具提示的命令按鈕：Keep Only、Exclude、View Data | 有「✓ 只保留」「✕ 排除」（可以復原）；沒有 View Data 的按鈕 | 可用但不同 | 中 | B05_mark_selected.png |
| B23 | Viewer | 點選標記、Ctrl＋點選加選 | 點選後其他標記變淡，Ctrl 加選有作用 | 相同 | — | B05_mark_selected.png、B06_ctrl_click.png |
| B24 | Viewer | 拖曳矩形框選多個標記（任何圖表） | 一般圖表不能框選（程式裡只有地圖有選取工具）。拖曳時沒有選取框，反而把座標軸的刻度與標題反白成選取的文字 | 缺少 | 高 | B07_lasso_dragging.png |
| B25 | Viewer | 多選時的摘要（幾個項目、量值的合計） | 只寫「2 個標記」加上最後點的那個標記的工具提示，沒有合計 | 可用但不同 | 中 | B06_ctrl_click.png |
| B26 | Viewer | 點圖例的項目：醒目提示該項目的標記 | 點了沒有反應（折線圖的 Measure Names、長條圖的集合圖例都試過，標記的不透明度沒有變） | 缺少 | 中 | B41_legend_click.png、B44_legend_click2.png |
| B27 | Viewer | 滑到欄位標題或座標軸出現排序圖示，點了排序 | 沒有排序圖示，滑過標題與軸標題游標與畫面都沒有變化 | 缺少 | 高 | B47_field_label_hover.png、B48_axis_title_hover.png |
| B28 | Viewer | 階層的切入：滑到標題出現 ＋／－ | 工作表右上角有文字連結「⊞ Sub-Category」，按了切入、變成「⊟」可收合；不在標題上 | 可用但不同 | 低 | B21_drill_down.png |
| B29 | Viewer | view 上方的工作表分頁 | 一個 44 項的下拉選單（「dashboard：」「Story：」「工作表：」開頭），不是分頁；不管報表有沒有設定「以分頁顯示工作表」都一樣 | 可用但不同 | 中 | A07_workbook_page_bob.png |
| B30 | Viewer | Story：故事點的導覽 | 導覽列、上一個／下一個可用。鍵盤方向鍵不換故事點；重新整理後回到第一點 | 相同 | — | B50_story.png、B51_story_point2.png |
| B31 | Viewer | 全螢幕 | 「簡報模式」進入全螢幕，另外可以前後切換（共 44 張）；方向鍵不換頁，Esc 結束 | 可用但不同 | 低 | B52_presentation.png |
| B32 | Viewer | 捲動很長的表格／圖 | 在工作表的區塊內捲動，標題列與座標軸固定。外層的 Category、Sub-Category 標籤只寫在每組第一列，捲過去之後那兩欄是空白（Tableau 會讓外層標籤留在看得到的範圍；這一點我依記憶，沒有查證）；刻度「90,000100,000」重疊 | 可用但不同 | 中 | B71_long_sheet_scrolled.png |
| B33 | Viewer | 沒有資料時：空白的檢視 | 0 列的工作表仍畫出座標軸與一個「0.000000」的刻度 | 可用但不同 | 低 | B26_no_data.png、B00_dash_02.png |
| B34 | Viewer | 查詢失敗時的訊息 | 「查詢失敗：500 boom」；斷線時「查詢失敗：Failed to fetch」（瀏覽器的英文訊息）。沒有重試的按鈕 | 可用但不同 | 中 | B55_render_error.png、B56_render_offline.png |
| B35 | Viewer | 載入中的指示 | 文字「載入中…」。載入期間工作表的標題顯示內部代號（ws_sales_by_segment、ws_profit_by_ship_mode），查到資料才換成名稱 | 壞掉 | 低 | B57_loading_state.png |
| B36 | Viewer | 鍵盤操作 | 工具列與篩選控制項可以用 Tab 到達；圖表裡的標記不能用鍵盤到達（Tableau 2021.2 的鍵盤操作到什麼程度我不確定） | 可用但不同 | 低 | `b08.py` 的輸出 |
| B37 | Viewer | dashboard 上只有作者放的東西 | 每張工作表的右上角多了「N 列」與「資料」連結（按了把圖換成表格）。Tableau 的 dashboard 沒有；訂閱寄出的圖片與 PDF 也把它們印出來 | 可用但不同 | 中 | A07_workbook_page_bob.png、C20_data_link.png |
| B38 | Viewer | 工具列只有檢視者用得到的功能 | 從 Tableau 移轉的報表，工具列多一排「預設／default／segment_consumer／region_west／ship_first_class」（驗證用的篩選組合），所有檢視者都看得到；發布的報表也有一顆意義不明的「預設」 | 可用但不同 | 中 | A07_workbook_page_bob.png |
| B39 | Viewer | 找不到 view、報表、網址時 | view 不存在：「找不到這個 dashboard、工作表或 Story」；報表不存在：「載入失敗：404 找不到報表「no_such_workbook」」；亂打的路徑直接顯示首頁，沒有 404 頁 | 可用但不同 | 低 | B53_bad_view.png、B54_bad_workbook.png |
| C1 | Viewer | 復原、重做、還原 | 都可用（篩選、只保留／排除都能復原） | 相同 | — | `c01.py` 的輸出、B09_after_exclude.png |
| C2 | Viewer | 重新整理 | 重新查詢（0.14 秒），畫面沒有任何回饋 | 相同 | — | `c01.py` 的輸出 |
| C3 | Viewer | 暫停自動更新 | 沒有 | 缺少 | 低 | A07_workbook_page_bob.png |
| C4 | Viewer | 下載圖片 | PNG，整個 dashboard 含篩選控制項，不含「N 列 資料」 | 相同 | — | downloads/C02_功能總覽.png |
| C5 | Viewer | 下載資料（摘要） | 對話框選工作表、預覽、下載 CSV（UTF-8 含 BOM）。畫面上是 909,370，CSV 裡是 909369.9110000005（浮點數的尾數） | 可用但不同 | 低 | C05_view_data_dialog.png、downloads/C05_Sales by Segment.csv |
| C6 | Explorer | 檢視資料（完整資料）：顯示所有欄位、顯示別名的選項 | 要「檢視基礎資料」的權限（bob 沒有這個分頁，erin 有）；預覽前 1,000 列、CSV 是全部（6,326 列，只含畫面上篩選後的 Segment 與 Region）。沒有「顯示所有欄位」「顯示別名」的選項 | 可用但不同 | 低 | C21_full_data.png |
| C7 | Explorer | 選了標記再看資料：只列出選取的標記 | 選了 1 個標記，「檢視資料」仍列出全部 2 列 | 可用但不同 | 中 | `b11.py` 的輸出 |
| C8 | Viewer | 下載交叉資料表 | 選工作表、Excel 或 CSV；Excel 裡數字存成數字 | 相同 | — | C04_crosstab_dialog.png、downloads/C04_Sales by Segment.xlsx |
| C9 | Viewer | 下載 PDF：對話框（範圍、縮放、紙張、方向）後直接下載檔案 | 沒有直接下載：先開「頁面設定」，再叫瀏覽器的列印對話框，要自己選「另存為 PDF」。不能選「這份報表的其他工作表」。印出來的內容含「N 列 資料」 | 可用但不同 | 中 | C10_pdf_page_setup.png、downloads/C10_print_emulated.pdf（以 Chromium 的列印模擬） |
| C10 | Viewer | 下載 PowerPoint | .pptx，一張投影片、一張圖。沒有標題投影片，也不能選多個 view（Tableau 有；依記憶） | 可用但不同 | 低 | downloads/C03_功能總覽.pptx |
| C11 | Explorer | 下載 workbook | 發布的報表可以在「版本」下載每一版（.tmigx）；從 Tableau 移轉的報表沒有下載 | 可用但不同 | 低 | E08_versions_dialog.png |
| C12 | Viewer | 分享：複製連結（帶目前的篩選）、內嵌碼、分享給使用者 | 沒有分享的按鈕。改了篩選後網址不變，重新整理就回到原始的檢視，沒有辦法把目前的畫面傳給別人（除了存成公開的自訂檢視，而它也沒有自己的網址） | 缺少 | 高 | `c01.py` 的輸出 |
| C13 | Viewer | 網址參數篩選：`?Region=West` | 不支援：`?Region=West&Segment=Consumer` 開啟後篩選沒有變，檢視器只認 `?case=<名稱>` | 缺少 | 高 | C01_url_params.png |
| C14 | Viewer | 自訂檢視：儲存、設為預設、管理、公開；每個自訂檢視有自己的網址 | 儲存、設為預設、公開、刪除可用，重新開啟時套用預設。沒有各自的網址；不能改名；剛儲存時工具列不顯示名稱，重新整理才出現 | 可用但不同 | 中 | C13_custom_views_list.png |
| C15 | Viewer | 訂閱自己：選排程、主旨、圖片或 PDF | 內容（這個 view／整份報表）、主旨、排程（管理員建立的）、格式、訊息、「view 沒有資料時不寄送」 | 相同 | — | G10_subscribe_dialog_bob.png |
| C16 | Creator | 替別人訂閱：搜尋使用者或群組 | 擁有者看到一排使用者的勾選框（沒有 email 或不能檢視的人不列）。不能選群組、沒有搜尋，使用者多時不好用 | 可用但不同 | 中 | G13_subscribe_dialog_owner.png |
| C17 | Viewer | 資料警示：選數值軸後建立 | 從工具列建立：選工作表、量值（顯示目前的範圍）、條件、門檻、頻率。做法不同但可用 | 可用但不同 | 低 | C16_alert_dialog.png |
| C18 | Viewer | 留言：側邊窗格、@提及會通知 | 可以新增、刪除；是蓋住畫面的對話框。「@carol」只是文字、不通知；網址不是連結 | 可用但不同 | 中 | C15_comment_posted.png |
| C19 | 全部 | 時間以使用者的時區顯示 | 留言的時間（04:53，當地是 12:53）與資料源清單的「萃取更新」是 UTC、沒有標示；版本、執行紀錄、我的訂閱是當地時間；修改紀錄標了「時間（UTC）」 | 壞掉 | 中 | C15_comment_posted.png、E11_datasources.png、E08_versions_dialog.png |
| D1 | Creator | 在伺服器上編輯（Web Edit） | 伺服器上沒有編輯：Creator 的工具列沒有編輯鈕，`/w/…/edit/…` 的網址在伺服器上只顯示檢視 | 缺少 | 中 | D01_server_edit_path.png |
| D2 | Creator | 從 view 交給桌面版編輯 | 檢視器沒有「在桌面版開啟」。桌面版有「從伺服器開啟…」（這次沒有操作） | 缺少 | 中 | E07_published_report_view.png |
| E1 | Creator | 桌面版登入伺服器 | 在瀏覽器的核准頁按「允許」後桌面版顯示「已登入」；首頁的「桌面版的登入」列出這台電腦，可以撤銷 | 可用但不同 | 低 | E02_approval.png、E12_desktop_tokens.png |
| E2 | Creator | 從桌面版發布 workbook | 選 Project、報表名稱、版本說明，0.7 秒「已發布「季度銷售」第 1 版到「Sales / 華北」」 | 相同 | — | E03_publish_dialog.png、E04_publish_result.png |
| E3 | Creator | 發布對話框的其他選項：說明、標籤、要發布的工作表、以分頁顯示、權限、資料源的驗證（嵌入密碼） | 都沒有 | 缺少 | 中 | E03_publish_dialog.png |
| E4 | Creator | 覆蓋已發布的 workbook | 再發布時預設「覆蓋「季度銷售」（…這份是從第 1 版開始的）」，成為第 2 版 | 相同 | — | E05_republish_dialog.png |
| E5 | Creator | 版本紀錄、還原、下載 | 「版本」列出每一版（時間、發布者、說明、大小），可以比較兩版、回復（成為第 3 版）、下載。不能刪除單一版本、不能預覽舊版 | 相同 | — | E08_versions_dialog.png、E10_after_restore.png |
| E6 | Creator | 擁有者從內容的動作選單改名、移動、刪除、設權限 | 擁有者沒有任何畫面可以管理自己的內容：這些功能都在「站台管理」，carol 開 `/admin` 只看到「站台管理只有站台管理員可以使用。」 | 缺少 | 高 | `g02.py` 的輸出 |
| E7 | Admin | 管理員改名、移動、換擁有者、刪除 | 都可用；刪除前說明會移到垃圾桶、管理員可以救回 | 相同 | — | F08_report_edit_dialog.png、E20_delete_report.png |
| E8 | Creator | 標籤、說明 | 沒有（只有每一版的版本說明） | 缺少 | 低 | E03_publish_dialog.png |
| E9 | Creator | 設定「以分頁顯示工作表」 | 伺服器上不能設定 | 缺少 | 低 | F08_report_edit_dialog.png |
| E10 | Creator | 發布時的嵌入憑證提示 | 設計上資料庫帳號在伺服器的連線設定檔；沒有資料庫可以測 | 無法實測 | — | — |
| E11 | Creator | 發布資料源、讓 workbook 連到它 | 桌面版發布「訂單」，並顯示「報表已改用發布的資料源」；伺服器的「資料源」頁列出 | 相同 | — | E06_publish_ds_dialog.png、E11_datasources.png |
| E12 | Creator | 資料源的頁面：詳細資料、連到它的 workbook、認證、更新排程 | 只有一張清單（名稱、Project、擁有者、連線、萃取更新、用到的報表）與「下載」「刪除」；名稱不是連結，沒有詳細頁、沒有認證（Certified）、沒有說明 | 可用但不同 | 中 | E21_datasources_admin.png |
| E13 | Admin | 資料源的萃取更新成功 | 沒有來源資料庫：立即執行的結果是失敗「資料源「Orders」的萃取來源無法解析」，成功的情形沒有測到 | 無法實測 | — | G20_tasks_after_run.png |
| F1 | Admin | Project 的權限對話框：範本、逐項允許／拒絕 | 有範本（檢視、探索、發布、管理、拒絕）與逐項的下拉，儲存後立即生效 | 相同 | — | F03_project_permissions.png |
| F2 | Admin | 有效權限：看某個使用者最後的權限與原因 | 沒有 | 缺少 | 中 | F04_workbook_permissions.png |
| F3 | Admin | workbook 層級的規則，使用者的規則優先於群組 | 對 bob 拒絕「下載圖片／PDF」：bob 的「匯出」立刻只剩資料與交叉資料表 | 相同 | — | F06_workbook_perm_deny_bob.png |
| F4 | Admin | 鎖定的 Project：內容的權限不能個別改 | 報表的權限對話框只寫「這個 Project 鎖定權限：報表的權限要在 Project 上設定」 | 相同 | — | F07_locked_report_permissions.png |
| F5 | Admin | view 層級的權限 | 沒有「不以分頁顯示」的報表可以測 | 無法實測 | — | — |
| F6 | Creator | 站台角色：Explorer、Viewer 不能發布 | 以 API 發布被拒：「站台角色 Explorer 不能發布」「站台角色 Viewer 不能發布」；carol 發布到 Finance：「沒有發布到這個 Project 的權限」 | 相同 | — | 指令的輸出 |
| F7 | Viewer | 沒有權限時看到什麼 | 清單裡不出現。直接開網址：「載入失敗：404 找不到報表「report_3」」（Tableau 是說明沒有權限的頁面） | 可用但不同 | 低 | F09_no_permission_deeplink.png |
| F8 | Viewer | 列層級權限：不同的人看到不同的資料 | 同一張工作表：alice 2 列、bob 2 列、dave（Managers）6 列、amy（不符合條件的管理員）0 列，與公式一致；訂閱寄給 bob 的圖也只有 West | 相同 | — | F11_rls_alice.png、F11_rls_bob.png、F11_rls_dave.png |
| G1 | Admin | 建立排程 | 每小時／每天／每週／每月、優先順序、平行或依序；時區打錯：「422 不認得的時區 '台北'（請用 IANA 名稱，例如 Asia/Taipei）」 | 相同 | — | G01_add_schedule.png、G03_schedules.png |
| G2 | Admin | 萃取更新工作、立即執行 | 新增（報表或資料源、排程、完整或增量）、立即執行排入佇列 | 相同 | — | G04_add_extract.png、G05_tasks.png |
| G3 | Creator | 擁有者替自己的內容排萃取更新 | 只有站台管理員能排（功能在站台管理裡） | 缺少 | 中 | `g02.py` 的輸出 |
| G4 | Admin | 失敗的處理與通知 | 工作頁顯示「失敗、連續失敗 1 次」，寄信給擁有者。信裡是程式的例外名稱與 ISO 格式的 UTC 時間（「錯誤：RefreshError: …」「時間：2026-11-21T05:03:31.159656+00:00」）；擁有者在首頁與資料源頁看不到失敗 | 可用但不同 | 中 | mail/002.eml、G22_datasources_owner.png |
| G5 | Viewer | 訂閱的信 | 以訂閱者的身分產生。信只有 view 名稱的連結與一張圖；沒有「管理我的訂閱／取消訂閱」的連結；沒有 Date 標頭（假 SMTP 收到的原文） | 可用但不同 | 低 | mail/003.eml、mail/005_…png |
| G6 | Viewer | 訂閱的 PDF：縮放到一頁 | A4 直向的 PDF 右邊約五分之一被裁掉（dashboard 寬 1000 像素、頁寬 794 像素，沒有縮放），下半頁是網頁的灰色底，並印出「N 列 資料」「⊞ DAY(Order Date)」等介面文字 | 壞掉 | 高 | mail/003_區域銷售.pdf |
| G7 | Viewer | 資料警示的信 | 條件成立時寄出，列出符合的標記並附圖。報表名稱寫成資料夾代號「secure」，不是「區域銷售」 | 可用但不同 | 低 | mail/001.eml |
| G8 | Admin | 工作狀態頁 | 「執行紀錄」：開始、結束、工作、觸發（立即執行的人）、結果、說明，可以依工作篩選。警示顯示成「資料警示 a4a9647b801cc」 | 相同 | — | G21_runs.png |
| G9 | Admin | 背景程式的狀態 | 排程器沒有執行時，頁面直接顯示命令列「uv run tmig schedule serve --store <報表庫> --mail mail.toml」；同時又回「已排入佇列，排程器會在幾秒內檢查」 | 可用但不同 | 低 | H01_admin_3_排程.png、`g02.py` 的輸出 |
| G10 | Viewer | 我的訂閱 | 修改主旨、排程、格式，暫停，取消訂閱。報表顯示成「secure／銷售總覽」（內部代號）；同一個 view 可以重複訂閱 | 可用但不同 | 低 | G12_my_subscriptions.png、G23_edit_subscription.png |
| H1 | Admin | 新增使用者 | 對話框：使用者名稱、名稱、email、網域、站台角色。重複時「422 使用者 bob 已經存在」；email「not-an-email」被接受 | 可用但不同 | 低 | H10_add_user.png |
| H2 | Admin | 修改使用者的顯示名稱、email | 建立之後只能改站台角色與群組 | 缺少 | 中 | H01_admin_1_使用者.png |
| H3 | Admin | 匯入使用者（CSV）、與 AD 同步 | 畫面上沒有（只有命令列的 `tmig site import`） | 缺少 | 中 | H01_admin_1_使用者.png |
| H4 | Admin | 設定站台角色、停用（未授權） | 列上的下拉選單，立即生效；不能改自己的角色 | 相同 | — | H01_admin_1_使用者.png |
| H5 | Admin | 刪除擁有內容的使用者 | 擋下並列出內容：「carol 還擁有內容，先換擁有者：Project「華北」、報表「superstore_features」」 | 相同 | — | H12_delete_owner.png |
| H6 | Admin | 群組：新增、成員、改名、刪除 | 都可用；改名時提醒 ISMEMBEROF 的影響 | 相同 | — | H13_group_members.png |
| H7 | Admin | 使用者清單：排序、最後登入時間、多選 | 可以搜尋名稱與 email；不能排序、不能依群組找、沒有最後登入時間、不能多選 | 可用但不同 | 低 | H01_admin_1_使用者.png |
| H8 | Admin | 站台設定、授權數 | 沒有 | 缺少 | 低 | H01_admin_0_內容與權限.png |
| H9 | Admin | 狀態與使用統計（管理員檢視） | 沒有畫面；只有 `/api/usage` 的 JSON 與命令列 | 缺少 | 中 | `g03.py` 的輸出 |
| H10 | Admin | 稽核記錄 | 「修改紀錄」：時間、修改的人、動作、對象（不記修改前後的值） | 可用但不同 | 低 | H14_changes.png |
| H11 | Admin | 內容清單：搜尋、篩選、多選 | 站台管理的「內容與權限」是一棵樹，沒有搜尋、篩選、多選 | 可用但不同 | 低 | F02_admin_content.png |
| I1 | 全部 | 介面用語一致、中文化 | dashboard、Project、view、Story、Project Leader、email、Creator／Explorer／Viewer 沒有翻譯，與「報表」「工作表」「資料源」混用（Tableau 的繁體中文介面是儀表板、專案、檢視、故事、工作簿） | 可用但不同 | 中 | A07_workbook_page_bob.png、F03_project_permissions.png |
| I2 | 全部 | 不出現開發用語 | 產品名稱「Tableau 移轉檢視器」、下拉裡的「（fixtures）」、命令列、HTTP 狀態碼（「422 …」「載入失敗：404 …」「查詢失敗：500 …」）、內部代號（secure、ws_…、a4a9…） | 可用但不同 | 中 | G04_add_extract.png、B54_bad_workbook.png |
| I3 | 全部 | 空狀態有說明 | 訂閱、自訂檢視、留言、排程、工作、執行紀錄的空狀態都有一句說明 | 相同 | — | C11_custom_views_empty.png |
| I4 | 全部 | 深層連結、上一頁、下一頁 | 正常 | 相同 | — | `b06.py` 的輸出 |
| I5 | Viewer | 工作階段逾時：提示重新登入 | 逾時後一動篩選，整頁直接變成登入畫面，沒有說明；重新登入後回到同一頁，但改過的篩選消失 | 可用但不同 | 低 | I10_session_expired_filter.png |
| I6 | Viewer | 手機（390×844） | 工具列占 287 像素（螢幕的三分之一）；沒有手機版面的 dashboard 被壓成 358×509，長條圖只剩角落、看不到內容；點標記沒有工具提示 | 壞掉 | 高 | I02_mobile_dashboard.png |
| I7 | Viewer | 1024 寬的視窗 | 工具列折成多列（98 像素），dashboard 縮成 992×588，可用 | 可用但不同 | 低 | I20_1024_dashboard.png |
| I8 | Viewer | 主控台沒有錯誤 | Viewer 每開一個 view 都有一個 403（`/datasources`）與一個 404（`/api/reports/<id>`）的請求 | 可用但不同 | 低 | 各指令稿的輸出 |

## 最該先處理的 15 項

1. **發布的名稱不見了（A11）**：發布成不同名稱的報表在首頁與檢視器都叫 .twbx 裡的名稱，三份「superstore_demo」分不出來、也搜尋不到。Tableau 一律用發布時的名稱。證據：F01_home_carol_after_publish.png、E07_published_report_view.png。
2. **首頁不能瀏覽內容（A6–A9、A15）**：沒有 Project、縮圖、排序、擁有者與修改時間，也沒有 workbook 頁；報表一多就找不到。Tableau 是依 Project 瀏覽的 Explore 頁加 workbook 頁。證據：A04_home_bob.png、A07_workbook_page_bob.png。
3. **多選下拉被區塊裁切、點外面不收（B2、B3）**：每天最常用的篩選，17 個選項只露出 37 像素。Tableau 的選單浮在 dashboard 上面、點外面就收。證據：B27_dropdown_clipped_zoom.png、B03_control_hover.png。
4. **不能拖曳框選（B24）**：拖曳只會反白座標軸的文字。Tableau 在任何圖表上拖曳就是框選。證據：B07_lasso_dragging.png。
5. **不能分享目前的畫面、網址參數不能篩選（C12、C13）**：沒有分享鈕，`?Region=West` 沒有作用；既有的信件與入口網站裡帶參數的連結移轉後都失效。Tableau 的分享連結帶目前的狀態，網址參數可以篩選。證據：C01_url_params.png。
6. **相對日期與條件篩選唯讀（B8、B9）**：檢視者不能改「最近 3 個月」，條件篩選還直接顯示公式。Tableau 的檢視者可以改。證據：B25_relative_date_click.png。
7. **沒有排序圖示（B27）**：不能在標題或座標軸上排序。Tableau 滑過去就有排序圖示。證據：B47_field_label_hover.png。
8. **擁有者不能管理自己的內容（E6、G3）**：改名、移動、刪除、權限、萃取更新排程都要站台管理員做。Tableau 的擁有者從內容的動作選單自己處理。證據：`g02.py` 的輸出（carol 開 `/admin`）。
9. **訂閱的 PDF 被裁切（G6、B37）**：A4 直向右邊五分之一不見，還印出「N 列 資料」等介面文字。Tableau 會縮放到頁面，只有檢視的內容。證據：mail/003_區域銷售.pdf。
10. **手機上不能看（I6）**：工具列占三分之一個螢幕，dashboard 被壓到看不見。Tableau 用手機版面或可縮放的完整版面。證據：I02_mobile_dashboard.png。
11. **選取相關的功能不全（B26、B25、B22、C7）**：圖例不能醒目提示、多選沒有合計、工具提示沒有 View Data、檢視資料不依選取。Tableau 都有。證據：B41_legend_click.png、B06_ctrl_click.png。
12. **檢視畫面上多出來的東西（B29、B38、B37）**：分頁是 44 項的下拉、工具列一排驗證用的「篩選組合」、每張工作表的「N 列 資料」。Tableau 是上方的分頁，畫面上只有作者放的內容。證據：A07_workbook_page_bob.png。
13. **PDF 要經過瀏覽器的列印（C9、B20）**：不能直接下載、不能一次匯出多張工作表。Tableau 的下載對話框直接給 PDF 檔。證據：C10_pdf_page_setup.png。
14. **時間、名稱與用語不一致（C19、I1、I2、B35、A3）**：UTC 與當地時間混用且不標示；內部代號、HTTP 狀態碼、命令列出現在畫面與信裡；dashboard／Project／view 不翻譯；SSO 帳號不在站台時說「沒有登入者」。Tableau 一律是使用者的時區與名稱。證據：C15_comment_posted.png、B57_loading_state.png、A10_proxy_unknown_user.png。
15. **管理功能的缺口（F2、H9、H3、H2）**：沒有有效權限的檢視、沒有使用統計的畫面、不能匯入使用者、不能改使用者的名稱與 email。Tableau 都在站台管理裡。證據：F04_workbook_permissions.png、H01_admin_1_使用者.png。

## 我認為是程式錯誤的項目（附重現步驟）

1. A11：以 carol 從桌面版（或 `POST /api/reports/publish?name=季度銷售&project=p-north`）發布示範的 .twbx → 開伺服器首頁 → 方塊的名稱是「superstore_demo」；在站台管理把報表改名 → 首頁不變；首頁搜尋發布的名稱 → 「找不到」。原因：`/api/workbooks` 回的是 `ir.workbook.name`（`service/src/tmig_service/app.py` 第 790 行）。
2. B2：以 bob 開 `/w/superstore_features/d/db_eef81f0f` → 按 Sub-Category 的下拉 → 選單只有 37 像素高（`.options` 的內容高 409、可見 35；上層的 `.zone-box` 是 `overflow: hidden`）。
3. B3：同一個下拉打開後，點 dashboard 的空白處 → 選單還在；再按 Esc → 還在。
4. B24：開 `/w/superstore_features/d/db_60eea131` → 在 Sales by Segment 的圖上按住左鍵拖曳 → 座標軸的刻度與「Sales」被反白，沒有選到標記。
5. G6：bob 訂閱「區域銷售」的 dashboard、格式選「圖片與 PDF」（紙張方向沒有改）→ 管理員按「立即寄送」→ 排程器執行 → 信裡的 PDF 是 A4 直向，右邊被裁掉。
6. B35：把 `/render` 的回應延遲 3 秒（或查詢本來就慢）→ 開 dashboard → 工作表的標題顯示 ws_sales_by_segment。
7. C19：在任一 view 留言 → 留言的時間比當地時間早 8 小時（UTC），沒有標示；`/datasources` 的「萃取更新」欄也是；同一個產品的「版本」對話框是當地時間。
8. A3：proxy 模式，帶 `X-Forwarded-User: zed`（不在目錄中）開首頁 → 「沒有登入者：請經由公司的登入入口開啟這個網站」。
9. G7、G10：訂閱或警示 `fixtures/secure` 的報表 → 「我的訂閱」與警示信裡的報表名稱是「secure」，不是「區域銷售」。
10. I6：以 390×844 開 `/w/superstore_features/d/db_60eea131` → dashboard 壓成 358×509，圖表看不到。
11. 較小的：C5 摘要 CSV 的 909369.9110000005；H1 接受不合格式的 email；C14 剛儲存自訂檢視時工具列不顯示名稱；B32 的刻度「90,000100,000」重疊；B33 沒有資料時的「0.000000」。

## 沒有實測到的項目與原因

- 密碼登入與密碼錯誤的訊息：平台沒有密碼登入。OIDC 登入：需要外部的身分提供者，沒有測。
- 成功的萃取更新（資料庫來源）、嵌入憑證、Live 連線的報表：這台電腦沒有資料庫引擎；只測到失敗的路徑與通知信。
- view 層級的權限（不以分頁顯示工作表的報表）：沒有這樣的報表。
- 「只顯示相關值」的效果、日期範圍控制項：見 B12、B13。
- 常駐的排程器（`tmig schedule serve`）與到期時自動執行、連續失敗 5 次後暫停、暫停的通知信：只用 `tick` 執行了一次「立即執行」的工作。
- 桌面版的「從伺服器開啟」、發布衝突（別人先發布了新版本）、上傳 .tdsx 發布資料源：沒有操作。
- 真的瀏覽器列印對話框：無頭瀏覽器沒有對話框；以攔截 `window.print` 確認有呼叫，並以 Chromium 的列印輸出模擬結果。
- 真實的郵件軟體裡信件的樣子：只看了假 SMTP 收到的原文、內嵌的圖與 PDF。
- 大量資料下的載入時間、多人同時使用、長時間的工作階段：沒有測。
- 與 Tableau Server 2021.2 並排比對：這台電腦沒有 Tableau；對照以我對 Tableau 的了解與上面兩頁文件為準。B32（外層標籤的捲動）、B36（鍵盤操作）、C10（PowerPoint 的標題投影片）這三處對 Tableau 的描述我不確定。
- 桌面版的編輯器：由另一位稽核，不在這份報告裡。
