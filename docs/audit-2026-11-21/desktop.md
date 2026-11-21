# 桌面版編輯器稽核：以 Tableau Desktop 2021.2 的使用體驗為基準

> 這份報告是 2026-11-21 使用體驗稽核的原始紀錄，由稽核用的子代理實際操作產品後寫成，只稽核、沒有改程式。
> 這台電腦沒有 Tableau，「Tableau 的做法」依對 Tableau 2021.2 的了解，沒有並排比對。
> 證據（截圖、下載的檔案、收到的信）沒有進版控，在本機 repo 資料夾下的 `audit-evidence/desktop/`；
> 文中的 `shots/…` 等檔名都在那個資料夾。彙整與後續處理見 `docs/backlog.md`。

- 日期：2026-11-21　版本：main（0666da4），`web/dist` 現成的建置，沒有重建、沒有改任何檔案
- 做法：以測試的方式在行程內啟動本機引擎（`create_desktop_app`），用 Playwright（無頭 Chromium，1440×900；窄視窗另測 1024×700）
  實際操作編輯器的介面。腳本在 `audit_desktop/s01.py`～`s39.py`，每一步的輸出在 `audit_desktop/out/`（s01～s04 的輸出當時沒有存檔），截圖在 `audit_desktop/shots/`
- 報表：`superstore_demo.twbx`（6 份複本）、`superstore_features.twbx`、以 Excel 新建的報表，以及 repo 根目錄那份未進版控的
  `Snow Sports Rental Operations Overview_v2024.3.twbx`（複製到暫存目錄後開啟，只讀；42 張工作表、2 個 dashboard）
- 限制（影響判讀）：
  - 這台電腦沒有 Tableau Desktop，「Tableau 的做法」是依我對 2021.2 的了解寫的，沒有並排操作；不確定的地方在該列註明「不確定」
  - 不是在 Tauri 外殼裡測的：原生的選檔／存檔對話框在這個環境是 `window.prompt` 輸入路徑，無法評價；拖曳時跟著游標的影像
    （drag ghost）無頭瀏覽器截不到
  - 範例資料很小（約一萬列），量到的等待時間不代表大資料
  - 判定只寫實際操作到的；只看到選單或對話框、沒有做完的，在「實測結果」寫明

## 一、逐項結果

判定：相同／可用但不同／缺少／壞掉／無法實測。嚴重度：阻斷／高／中／低（以 Tableau 使用者每天工作會不會被卡住來分）。

### A. 開始與資料

| 編號 | 情境（Tableau 的做法） | 實測結果（我們的做法與發生的事） | 判定 | 嚴重度 | 證據 |
| --- | --- | --- | --- | --- | --- |
| A1 | 開啟 .twbx：檔案＞開啟，直接進入可編輯的工作區 | 首頁「開啟檔案…」。真實的 2024.3 .twbx（5 MB）3.5 秒開啟，沒有錯誤訊息；但開啟後是「檢視模式」的 dashboard，要再按「編輯 dashboard」才能編輯（Tableau Desktop 沒有檢視／編輯兩種模式） | 可用但不同 | 中 | a12_open_real_twbx.png、a13_real_dashboard_editor.png |
| A2 | 真實報表的 42 張工作表在編輯器逐張開啟 | 42 張都畫得出來、沒有錯誤訊息與 JS 錯誤，最慢 1.67 秒；features 的 31 張工作表、12 個 dashboard、1 個 Story 也都開得起來（每張約 0.5 秒）。沒有與 Tableau 的畫面比對 | 相同 | 低 | out/s31.txt、out/s36.txt、a14_real_sheet_editor.png |
| A3 | 起始頁列出最近的活頁簿（縮圖、路徑） | 首頁是「報表清單」：每份報表列出 dashboard 與工作表的連結。同名的報表（我開了 6 份複本）完全分不出來，沒有顯示檔案路徑 | 可用但不同 | 低 | a01_home.png |
| A4 | 連線到 Excel：選檔 → 資料源頁 | 「新增報表（連線到資料）…」→ 輸入路徑（此環境是 prompt）→ 選工作表的對話框（列數、欄位與型別）→ 資料源頁，0.5 秒 | 可用但不同 | 低 | a04_new_pick_table.png、a05_datasource_page.png |
| A5 | 資料源頁：左邊連線與資料表、中間畫布（拖資料表上去、noodle 連線）、下方資料格線一直看得到 | 版面是由上往下的表單：連線、邏輯資料表卡片、欄位清單、連線、資料源篩選、最下面才是資料預覽（1440×900 要往下捲才看得到）。資料表不能拖曳（`draggable` 沒有設），要點資料表名稱開對話框 | 可用但不同 | 中 | a08_relationship_added.png、a06_drag_table.png |
| A6 | 關聯：把第二張表拖到畫布，自動配對欄位 | 點「People」→ 對話框選「關聯」，同名的 Region 自動配對，加入後顯示「Orders ⟷ People」與條件下拉。沒有 noodle 圖形 | 可用但不同 | 低 | a07_relationship_dialog.png、a08_relationship_added.png |
| A7 | Join：連按兩下邏輯資料表進實體層，拖表、點 Venn 圖選 join 類型 | 同一個對話框選「Join」→ 選 join 類型與條件 →「已加入資料表」；之後在卡片上以下拉改類型與條件。沒有 Venn 圖 | 可用但不同 | 低 | a09_join_dialog.png、a10_join_added.png |
| A8 | 欄位改名：在格線的欄標題連按兩下 | 在欄位清單的輸入框改（Sales → 銷售額），資料窗格跟著變 | 可用但不同 | 低 | out/s28.txt |
| A9 | 變更資料類型：點欄標題的型別圖示 | 欄位清單的下拉選單，0.33 秒 | 可用但不同 | 低 | out/s29.txt |
| A10 | 在資料格線的欄標題上按右鍵／下拉：改名、隱藏、建立計算欄位、分割、樞紐 | 預覽表格的欄標題按左鍵、右鍵都沒有反應 | 缺少 | 低 | out/s28.txt「RCLICK 預覽表格的欄標題: 沒有選單」 |
| A11 | 即時／萃取的切換（檔案也可以選） | 檔案型的資料源顯示「檔案型的資料源一律匯入」，沒有切換；資料庫的切換沒有資料庫可測 | 可用但不同 | 低 | a08_relationship_added.png |
| A12 | 前往工作表 | 「前往工作表」0.39 秒，資料窗格有新資料源的欄位，連按兩下欄位畫出檢視 | 相同 | 低 | a11_new_workbook_view.png |

### B. 建立檢視

| 編號 | 情境（Tableau 的做法） | 實測結果（我們的做法與發生的事） | 判定 | 嚴重度 | 證據 |
| --- | --- | --- | --- | --- | --- |
| B1 | 拖維度到列、量值到欄 | 可以；每次 0.42～0.44 秒；拖曳時整個架變色 | 相同 | 低 | b04_region_sales.png、f01_drag_feedback_shelf.png |
| B2 | 連按兩下欄位自動放到檢視 | 可以（0.35～0.40 秒）。第二個量值加到欄架成為第二個座標軸，日期加到「列」成 YEAR。Tableau 的規則我不確定是否相同（印象中日期放到欄） | 可用但不同 | 低 | b05_dblclick_profit.png、b06_dblclick_date.png |
| B3 | 把欄位拖到檢視畫布（畫布上有「將欄位拖放到這裡」的三個區域） | 拖到畫布沒有任何反應；放置目標只有各個架與標記卡 | 缺少 | 中 | out/s05.txt「drag Segment->canvas changed: False」 |
| B4 | 智慧顯示：建議的圖表、切換類型 | 面板列出 24 種、建議的有外框、不能用的變灰且滑過有說明；切換每次 0.35 秒（圓餅、樹狀圖、文字表、水平長條都試過）。圖示是通用的符號，不是圖表縮圖 | 相同 | 低 | b07_show_me.png、b08_showme_圓餅圖.png |
| B5 | 標記類型下拉 | 13 種（自動、長條、線、區域、方形、圓形、形狀、文字、圓餅、甘特條、多邊形、地圖、密度） | 相同 | 低 | out/s05.txt |
| B6 | 拖欄位到色彩、大小、標籤、詳細資料、工具提示 | 都可以，每次 0.40～0.44 秒 | 相同 | 低 | b09_marks_card.png |
| B7 | 點「色彩」：在按鈕旁彈出小面板（編輯色彩、不透明度、框線、光暈），改了立刻看得到 | 開的是蓋住檢視的對話框，按「確定」才套用；色盤下拉顯示的是內部代號（`tableau10_10_0`、`traffic_light_10_0`、`seattle_grays_10_0`…），沒有色票預覽 | 可用但不同 | 中 | b10_markbtn_色彩.png |
| B8 | 點「大小」：滑桿 | 標記卡內出現滑桿 | 相同 | 低 | b10_markbtn_大小.png |
| B9 | 點「工具提示」「標籤」：編輯器裡已經有預設的文字（欄位名：<值>）可以直接改 | 對話框的編輯區是空的（說明寫「留空白時列出檢視中各欄位的名稱與值」），要自己從「插入…」一個一個加；沒有字型選單 | 可用但不同 | 中 | b10_markbtn_工具提示.png、b10_markbtn_標籤.png |
| B10 | 膠囊的下拉選單（維度） | 按左鍵點膠囊就開選單（Tableau 左鍵是選取，選單在箭頭或右鍵）。有：篩選、排序、量值、顯示標題、包含在工具提示中、在架上編輯、移除。沒有：顯示篩選、顯示醒目提示工具、格式、編輯別名 | 可用但不同 | 中 | b01_pill_menu_dim.png |
| B11 | 膠囊的下拉選單（量值）：彙總、維度／量值、離散／連續、快速表計算、格式、編輯座標軸、雙軸 | 都有；另有「在架上編輯」 | 相同 | 低 | b02_pill_menu_measure.png |
| B12 | 日期膠囊：日期部分與截斷（選單上有範例值，例如「季 Q2」「月 五月」） | 有年、季、月、週、星期、日、時、分與各自的連續版、精確日期；沒有範例值，純日期欄位也列出時、分 | 相同 | 低 | b12_date_pill_menu.png |
| B13 | 日期膠囊的 ＋／－ 下鑽 | 膠囊左邊的 ⊞ 一次下鑽一層（年→季→月），0.34 秒；⊟ 收合。階層欄位（Category→Sub-Category）也可以。檢視標題上沒有 ＋／－ | 相同 | 低 | b13_date_drilled.png |
| B14 | 按右鍵拖曳欄位：放下時出現「放置欄位」選彙總或日期層級 | 沒有反應 | 缺少 | 低 | out/s06.txt「right-drag … dialogs 0」 |
| B15 | 離散日期（年）在欄、量值在列，標記「自動」→ 折線圖 | 畫成長條圖（連續的月份才是折線） | 可用但不同 | 高 | b11_date_line.png |
| B16 | 離散的欄標題在檢視上方，年／季／月由上到下，月份顯示名稱、季顯示 Q1 | 標題在檢視下方（月在上、年在下），月份是 1～12、季是 1～4，年的標題直排（轉 90 度）；欄位標籤在右下角被切掉（「YEAR(Order Date) \| QUA」）；連續日期的刻度是英文「Jan 2021」，軸標題是「MONTH(Order Date)」 | 可用但不同 | 高 | b13_date_drilled.png、b11_date_line.png、b47_forecast_only.png |
| B17 | 兩個量值在欄：列標題只出現一次 | 每個量值的窗格各重複一次列標題與直排的「Region」 | 可用但不同 | 中 | b05_dblclick_profit.png |
| B18 | 交換欄與列：工具列按鈕、Ctrl+W | 工具列可以（0.34 秒）。Ctrl+W 是「關閉報表」：跳出「「superstore_demo」有尚未儲存的變更，仍要關閉嗎？」，按確定就關掉了 | 可用但不同 | 中 | 對話框原文如左（當次輸出已被重跑覆蓋）；`web/src/desktop.ts` 的 `installShortcuts`：Ctrl+W＝關閉報表 |
| B19 | 工具列的遞增／遞減排序 | 可以，0.34 秒 | 相同 | 低 | b14_sorted.png |
| B20 | 滑過座標軸或欄位標籤出現排序圖示，點一下排序 | 滑過座標軸標題、欄位標籤都沒有任何圖示 | 缺少 | 中 | b15_hover_axis.png、b15_hover_header.png |
| B21 | 膠囊選單的排序（遞增、遞減、依欄位、排序…） | 選單有這些項目；沒有打開「排序…」對話框實際操作 | 相同 | 低 | b01_pill_menu_dim.png（選單的完整清單是 s03.py 的輸出，當時沒有存檔） |
| B22 | 拖欄位到篩選 → 對話框（一般、萬用字元、條件、頂部） | 0.26 秒開啟，四個分頁都有；勾選後套用正常 | 相同 | 低 | b20_filter_dialog_general.png、b20_filter_tab1.png、b20_filter_tab2.png |
| B23 | 「頂部」分頁的「依」列出欄位名稱 | 計算欄位顯示內部名稱：`[Calculation_1368249927221915648]`、`SUM([Calculation_2049003526124437504])`…（應該是「利潤率」等） | 壞掉 | 中 | b20_filter_tab3.png |
| B24 | 量值篩選：先問彙總方式，再用滑桿選範圍 | 直接以 SUM 開啟；範圍是兩個輸入框沒有滑桿；資料範圍顯示「-8182.708300000003 到 23090.30200000002」 | 可用但不同 | 低 | b23_measure_filter.png |
| B25 | 日期篩選：先選篩選方式（相對日期、日期範圍、年…） | 一個對話框，上面的下拉選方式（相對日期、日期範圍、年、季、月、星期、日、年月、個別日期）；只看了對話框，沒有套用相對日期 | 可用但不同 | 低 | b24_date_filter.png |
| B26 | 顯示篩選：篩選卡出現在檢視右邊，邊做邊操作 | 篩選膠囊選單的「顯示篩選」打勾了，但編輯器的畫面上沒有控制項；按「完成」離開編輯器後，檢視器才看得到 | 缺少 | 高 | b22_filter_control_editor.png、b26_viewer_after_done.png |
| B27 | 建立參數 | 對話框有名稱、資料類型、目前值、開啟報表時的值、全部／清單／範圍；建立後出現在資料窗格 | 相同 | 低 | b32_param_dialog.png |
| B28 | 顯示參數控制項、在編輯時改參數的值 | 參數上按右鍵沒有選單，只有 ○（顯示參數）與 ✎ 兩個小按鈕；提示寫「單獨檢視工作表時…顯示」，編輯器畫面上沒有控制項 | 缺少 | 高 | b25_param_control_editor.png |
| B29 | 計算欄位編輯器：非強制回應的視窗、輸入時自動完成、語法著色、右側函式清單與說明、可以把欄位拖進公式、有「套用」 | 蓋住畫面的對話框，純文字方塊：沒有自動完成、沒有著色、沒有函式清單、欄位拖不進去（對話框擋住）、沒有「套用」。即時檢查是好的（「計算有效：實數、彙總（量值）」「找不到欄位 [Profitt]」「不能混用彙總與非彙總的參數」），`//` 註解可以用。語法錯誤的訊息帶出內部名稱：「計算欄位 f_editor_probe 的公式無法解析」 | 可用但不同 | 高 | b28_calc_autocomplete.png、b29_calc_invalid.png、b30_calc_error_mixed.png |
| B30 | 群組、集合、數值分組的對話框 | 都開得起來，內容與 Tableau 相近；數值分組的說明顯示「最小 0.44399999999999995」。只建立了數值分組與（從選取）群組 | 相同 | 低 | b33_group_dialog.png、b34_set_dialog.png、b35_bin_dialog.png |
| B31 | 建立階層：在資料窗格把欄位拖到另一個欄位上 | 拖上去沒有反應；要用選單「建立階層…」的對話框 | 可用但不同 | 低 | b36_hierarchy_dialog.png、b39_datapane_after_drag_hierarchy.png |
| B32 | 資料窗格：搜尋、F2 改名 | 搜尋可以（輸入 prof 時參數沒有被過濾掉）；F2 沒有反應，改名要用選單開對話框 | 可用但不同 | 低 | b37_rename_field.png |
| B33 | 總計 | 分析＞總計可以（0.61 秒）；列的名稱是英文「Grand Total」。小計在膠囊選單有（兩個維度時），沒有實際套用 | 可用但不同 | 低 | b43_refline_view.png |
| B34 | 參考線 | 對話框（線、區間、分布、盒鬚圖；範圍、值、標籤）；加上平均線後畫出線與標籤 | 相同 | 低 | b42_refline_dialog.png、b43_refline_view.png |
| B35 | 分析窗格：把常數線、平均線、趨勢線等拖到檢視上 | 分析分頁是一排按鈕與選項，按了開對話框，不能拖曳 | 可用但不同 | 中 | b40_analytics_pane.png |
| B36 | 趨勢線 | 月份折線上加線性趨勢線可以 | 相同 | 低 | b47_forecast.png |
| B37 | 預測 | 只按「確定」時畫得出來（預測段與區間帶，顏色與實際值相同）；勾了「以 0 填補缺少的值」之後沒有預測、也沒有任何說明 | 壞掉 | 中 | b47_forecast_only.png、x08_forecast_fill_zero.png |
| B38 | 雙軸、同步座標軸 | 膠囊選單「雙軸」可以，Measure Names 自動放到色彩；同步在「編輯座標軸…」對話框的勾選（Tableau 在座標軸的右鍵） | 可用但不同 | 低 | b48_dual_axis_only.png、b49_axis_dialog.png |
| B39 | 雙軸的圖加趨勢線 | 整張圖變成「查詢失敗：Duplicate data set name: "__ref_trend_pathgroup"」；復原後恢復 | 壞掉 | 高 | x01_dual_axis_trend_error.png、b48_dual_axis.png |
| B40 | 把 Measure Values 拖到文字：自動把 Measure Names 放到欄，各量值一欄 | Measure Names 沒有自動加上去，8 個量值的數字疊在同一格，看不出內容；要自己再把 Measure Names 拖到欄 | 壞掉 | 高 | x03_measure_values_overlap.png |
| B41 | 快速表計算、編輯表計算（計算依據） | 合計百分比 0.61 秒，膠囊有 △；選單有「計算方向」與「編輯表計算…」對話框。YTD 類的項目在只有連續月份時是停用的（Tableau 是否啟用我不確定） | 相同 | 低 | b51_pct_of_total.png、b52_table_calc_dialog.png |
| B42 | 格式：左側的格式窗格（字型、對齊、色帶、框線、線條分頁），改了立刻看得到 | 一個很長的對話框把所有設定放在一起，按「確定」才套用；量值的數字格式是另一個對話框（自動、數字、貨幣、百分比、自訂）。只看了對話框，沒有套用後比對 | 可用但不同 | 中 | b56_format_dialog.png、b57_number_format.png |
| B43 | 連按兩下座標軸開「編輯座標軸」 | 連按兩下刻度、軸標題都沒有反應；要從膠囊選單的「編輯座標軸…」 | 缺少 | 中 | b16_axis_dialog.png（沒有對話框） |
| B44 | 連按兩下標題編輯 | 沒有反應；要從「工作表＞標題…」開格式文字的對話框 | 缺少 | 中 | b17_title_dialog.png（沒有對話框）、b59_title_dialog.png |
| B45 | 說明、摘要卡 | 「工作表＞顯示摘要」出現摘要卡；說明的對話框要勾「顯示說明」（沒有勾，所以沒看到說明） | 相同 | 低 | b60_caption_summary.png |
| B46 | 註解：在標記或空白處按右鍵＞註解 | 「分析＞註解…」的對話框（標記、點、區域）；只看了對話框 | 可用但不同 | 中 | b61_annotation_dialog.png |
| B47 | 選取標記：工具提示上有只保留、排除、群組、集合、檢視資料 | 檢視上方多出一列「已選取 N 個標記」與只保留、排除、註解、群組、建立集合（檢視被往下推）；沒有「檢視資料」。只保留 0.49 秒、可以復原；Ctrl+點選可以多選 | 可用但不同 | 中 | b18_mark_selected2.png |
| B48 | 選取標記後群組：立刻建立群組並換到檢視上 | 開「建立群組」對話框，按確定後只在資料窗格多一個「Sub-Category (group)」，檢視沒有變 | 可用但不同 | 中 | b69_group_from_selection.png |
| B49 | 滑過標記的工具提示 | 有（欄位名稱與值） | 相同 | 低 | b19_hover_tooltip.png |
| B50 | 復原／重做很多步 | 連續 10 步（拖放、篩選、交換、排序、標記類型）Ctrl+Z 全部正確還原，Ctrl+Y 全部重做；每步約 0.45 秒 | 相同 | 低 | out/s37.txt、b65_after_redo.png |
| B51 | 檢視資料 | 「分析＞檢視資料…」0.73 秒，有摘要與完整資料；是整張工作表的資料。裡面的「下載 CSV」失敗（見 E6） | 可用但不同 | 低 | b62_view_data.png |
| B52 | 圖例是獨立的卡：點選醒目提示、右鍵編輯色彩與排序、可以拖曳重排，永遠看得到 | 圖例是圖的一部分：檢視比較寬時被擠到畫面外（要橫向捲動才看得到）；點選、連按兩下、右鍵都沒有反應 | 缺少 | 中 | b66_legend_click.png |
| B53 | 狀態列：標記數、列×欄、SUM 的總和；選取時顯示選取的合計 | 只有「摘要資料 N 列」 | 可用但不同 | 低 | b18_mark_selected2.png |
| B54 | 移除膠囊：選取後按 Delete，或拖出架外任何地方 | Delete、Backspace 沒有反應；拖到畫布不會移除（拖回資料窗格才會）；選單的「移除」可以 | 缺少 | 中 | out/s09.txt |
| B55 | Ctrl+拖曳複製膠囊 | 可以（列架的 Category 複製到色彩） | 相同 | 低 | out/s09.txt |
| B56 | 在架上重排膠囊（有插入位置的標記） | 可以重排；拖曳時整個架變色，沒有標出會插在哪裡 | 可用但不同 | 低 | f03_reorder_indicator.png |
| B57 | 工作表分頁：右鍵選單 | 有重新命名、複製、複製成交叉資料表、隱藏工作表、分頁顏色、刪除 | 相同 | 低 | out/s19.txt |
| B58 | 連按兩下分頁改名 | 目前的分頁可以；連按兩下「不是目前的」分頁只會切過去，不會進入改名 | 可用但不同 | 低 | x04_dblclick_noncurrent_tab.png |
| B59 | 複製工作表：複本在原本的右邊 | 複本排在所有工作表的最後 | 可用但不同 | 低 | out/s20.txt |
| B60 | 拖曳分頁改變順序；dashboard 與工作表可以任意穿插 | 拖曳沒有反應（程式裡分頁沒有拖曳）；工作表一律排在 dashboard 前面 | 缺少 | 中 | b64_tab_drag.png、out/s20.txt |
| B61 | 刪除工作表可以復原 | 有確認；刪除後 Ctrl+Z 救不回來。刪除「目前開啟」的工作表會直接離開編輯器、跳到檢視器的 dashboard | 可用但不同 | 中 | x05_after_delete_current_sheet.png、out/s21.txt |
| B62 | 刪除 dashboard 用到的工作表：選項停用 | 可以按，之後出現訊息：「422 「Sub-Category Profit」還在 dashboard、Story 或動作中使用，不能刪除：dashboards[db_ee909b0c].zones[z10]：工作表「ws_sub_category_profit」不存在」 | 可用但不同 | 低 | out/s21.txt |
| B63 | 隱藏的工作表不出現在分頁列 | 隱藏後分頁還在，只是變灰。真實報表的 42 張隱藏工作表全部排在分頁列上，2 個 dashboard 在最右邊；開啟 dashboard 時目前的分頁在畫面外（x=4360，可見範圍到 1354），沒有自動捲過去，也沒有左右捲動的按鈕 | 可用但不同 | 高 | a13_real_dashboard_editor.png、b67_hidden_sheet_tab.png、out/s37.txt |

### C. Dashboard

| 編號 | 情境（Tableau 的做法） | 實測結果（我們的做法與發生的事） | 判定 | 嚴重度 | 證據 |
| --- | --- | --- | --- | --- | --- |
| C1 | 新增 dashboard：空白畫布有「將工作表拖放到這裡」 | 0.76 秒；畫布是空白的，沒有提示文字；分頁名稱是「儀表板 1」（介面其他地方都寫 dashboard） | 可用但不同 | 低 | c03_new_dashboard.png |
| C2 | 拖工作表進來：拖曳時灰色區塊預覽落點，放在區塊的左／右／上／下半邊就分割在那一邊 | 第一張可以。第二張我放在第一張的「右邊緣」，結果排在下面；第三張也是往下排。拖曳時整個區塊變色，沒有落點預覽。程式上是「加在那個區塊之後」，不看放在哪一邊；要左右並排得先加水平容器 | 缺少 | 高 | c05_drag_second_right.png、c08_three_sheets.png |
| C3 | 連按兩下工作表加入 | 可以 | 相同 | 低 | out/s23.txt |
| C4 | 並排區塊：拖邊界調整大小 | 邊界的游標沒有變化，拖了沒有反應；只能在「版面配置」勾「固定高度」輸入數字 | 缺少 | 高 | out/s24.txt「after dragging bottom border +80」沒有變、c13_zone_props.png |
| C5 | 並排區塊：拖上方的把手搬到別的位置 | 拖不動（只有浮動區塊可以拖） | 缺少 | 中 | out/s24.txt |
| C6 | 選取區塊：邊框上有把手、✕、前往工作表、當作篩選器、更多選項 ▾；右鍵有選單 | 選取後只多一個名稱標籤；右鍵沒有選單；區塊的屬性在左側「版面配置」分頁，停在「dashboard」分頁時點區塊看不到屬性 | 缺少 | 中 | c09_zone_selected.png、c13_zone_props.png |
| C7 | 移除項目：✕ 或 Delete | Delete 沒有反應；「版面配置」屬性裡的「移除」可以，可以復原 | 可用但不同 | 中 | out/s24.txt、out/s25.txt |
| C8 | 浮動：拖曳移動、調整大小 | 切到「浮動」後加入的物件可以拖曳移動，右下角有調整大小的把手 | 相同 | 低 | c11_floating_objects.png |
| C9 | 物件（文字、圖片、網頁、空白、按鈕、容器）：從左側拖到要放的位置；圖片與網頁會先問檔案或網址 | 是按鈕，按了加在預設位置（浮動時疊在左上角），不能拖。圖片不問檔案，直接加一個壞掉的圖（404 `…/files/image.png`）；網頁不問網址（預設 https://）。之後在屬性裡設定。水平／垂直容器只確認按鈕存在，沒有深入操作 | 可用但不同 | 中 | c11_floating_objects.png、out/s24.txt |
| C10 | 當作篩選器使用 | 屬性裡的勾選；勾了之後動作清單出現「篩選：Sales by Region（自動產生）」 | 可用但不同 | 低 | c13_zone_props.png、c14_actions_dialog.png |
| C11 | 動作對話框：篩選、醒目提示、網址、參數、集合、前往工作表 | 六種都可以新增；篩選動作的表單（執行方式、來源、目標、清除選取時、欄位）與 Tableau 相近。只看了表單，沒有逐種建立並執行 | 相同 | 低 | c14_actions_dialog.png、c15_action_form.png |
| C12 | 裝置版面 | 新增手機版面可以，區塊自動排成一欄，有使用哪個版面的規則說明 | 相同 | 低 | c16_phone_layout.png |
| C13 | 大小設定：固定（有常用尺寸的清單）、自動、範圍 | 固定、自動、範圍三種，寬高手動輸入；沒有常用尺寸的清單 | 可用但不同 | 低 | c01_dashboard_editor.png |
| C14 | 加入圖例、篩選、參數控制項：區塊的選單＞篩選／圖例／參數 | 左側「篩選、參數與圖例」先選工作表，再按「篩選：Region」等按鈕加入；三種都加得進去 | 可用但不同 | 低 | c17_controls_added.png |
| C15 | 版面配置的項目階層：可以拖曳重排 | 階層有列出、點了會選取；拖曳重排沒有反應 | 可用但不同 | 低 | c02_dashboard_layout_tab.png、out/s25.txt |

### D. Story

| 編號 | 情境（Tableau 的做法） | 實測結果（我們的做法與發生的事） | 判定 | 嚴重度 | 證據 |
| --- | --- | --- | --- | --- | --- |
| D1 | 新增 Story：空白，把工作表拖進來；直接在導覽方塊上打說明文字 | 0.79 秒；自動建立第一個故事點（顯示第一個 dashboard）；新增故事點用下拉選單選工作表，說明文字在左側的輸入框打；不能拖曳。分頁名稱是「故事 1」（功能表寫 Story） | 可用但不同 | 低 | d01_new_story.png、d03_story_captions.png |
| D2 | 導覽的樣式 | 說明文字、序號、圓點可以切換，可以復原 | 相同 | 低 | d02_story_dots.png |

### E. 檔案

| 編號 | 情境（Tableau 的做法） | 實測結果（我們的做法與發生的事） | 判定 | 嚴重度 | 證據 |
| --- | --- | --- | --- | --- | --- |
| E1 | 儲存（Ctrl+S） | Ctrl+S 問位置後存成 .tmigx，第二次直接覆蓋；有「已儲存：路徑」的訊息 | 相同 | 低 | out/s29.txt |
| E2 | 另存新檔：.twb 或 .twbx | 只有 .tmigx；輸入 `saved_sales_copy.twb` 會存成 `saved_sales_copy.twb.tmigx`。存出去的檔案 Tableau 打不開（文件已寫「不能存回 .twbx」） | 缺少 | 中 | out/s29.txt |
| E3 | 編輯時匯出圖片、資料、交叉資料表、列印（工作表與檔案功能表） | 編輯器的功能表沒有任何匯出或列印，Ctrl+P 沒有反應；要先按「完成」回檢視器，用工具列的「匯出…」 | 可用但不同 | 中 | out/s32.txt、e01_viewer_toolbar.png |
| E4 | 匯出圖片 | 檢視器的匯出＞圖片（PNG）：問位置後寫出檔案 | 相同 | 低 | out/s32.txt（sheet.png） |
| E5 | 匯出 PowerPoint | 寫出 .pptx（沒有打開檢查內容） | 相同 | 低 | out/s32.txt（sheet.pptx） |
| E6 | 匯出資料（CSV） | 失敗：`POST /api/desktop/export` 回 422「匯出的副檔名必須是 .png、.pptx、.pdf：sheet_data.csv」，畫面上沒有任何錯誤訊息，檔案沒有產生。編輯器「檢視資料」的「下載 CSV」也一樣 | 壞掉 | 高 | x06_export_csv_error.png、out/s33.txt |
| E7 | 匯出交叉資料表到 Excel | 失敗，同上：422「匯出的副檔名必須是 .png、.pptx、.pdf：sheet_crosstab.xlsx」，沒有錯誤訊息 | 壞掉 | 高 | x07_export_xlsx_error.png、out/s33.txt |
| E8 | 列印、列印成 PDF | 「PDF（列印）」開啟頁面設定（紙張、方向、邊界、縮放、顯示項目）；之後是瀏覽器的列印，無頭環境測不到 | 無法實測 | 中 | e02_export_pdf.png |

### F. 整體體驗

| 編號 | 情境（Tableau 的做法） | 實測結果（我們的做法與發生的事） | 判定 | 嚴重度 | 證據 |
| --- | --- | --- | --- | --- | --- |
| F1 | 右鍵選單：膠囊、架、標題、座標軸、標記、圖例、空白處、工作表標題、dashboard 的區塊 | 這些地方按右鍵都沒有選單（17 個位置都試過，含參數）。只有資料窗格的欄位與下方的分頁有右鍵選單。Tableau 使用者的編輯座標軸、格式、排序、隱藏、別名、只保留、註解、檢視資料幾乎都從右鍵進去 | 缺少 | 高 | f02_rclick_mark.png、out/s08.txt、out/s24.txt、out/s37.txt |
| F2 | 功能表列的內容 | 有檔案、資料、工作表、dashboard、Story、分析、地圖、格式、伺服器、視窗；沒有「說明」。各功能表的項目比 Tableau 少很多：例如「工作表」沒有匯出、複製、動作、工具提示、顯示卡片；「格式」只有一個「格式…」；「dashboard」在工作表上只有「新增 dashboard」；「分析」沒有圖例、篩選、參數、表格版面 | 可用但不同 | 中 | a03_menu_file.png；各功能表的完整清單是 s02.py 的輸出（當時沒有存檔，腳本可以重跑） |
| F3 | 拖放的回饋 | 架：整個架變色，沒有插入位置；dashboard：整個區塊變色，沒有落點預覽。跟著游標的影像無頭環境看不到 | 可用但不同 | 低 | f01_drag_feedback_shelf.png、f03_reorder_indicator.png、c05_drag_second_right.png |
| F4 | 工具列圖示的提示 | 每個按鈕都有 title（復原（Ctrl+Z）、交換欄與列…）。有兩個提示把「Tableau」寫進產品文字：「依檢視中的欄位建議圖表類型（Tableau 的 Show Me）」「依 量值由小到大排序「…」（Tableau 工具列的排序）」 | 可用但不同 | 低 | out/s09.txt、f05_toolbar_hover.png |
| F5 | 快捷鍵 | 有 Ctrl+Z／Y、Ctrl+S、Ctrl+Shift+S、Ctrl+O、Ctrl+W（關閉，與 Tableau 的交換衝突）。Delete、F2 沒有作用。Ctrl+M、F7、Ctrl+D 等沒有測；程式裡只有上述幾個 | 可用但不同 | 中 | out/s09.txt、out/s13.txt |
| F6 | 每個動作的等待時間 | 拖放 0.40～0.44 秒、連按兩下 0.35～0.40、智慧顯示 0.35、新增工作表 0.34、下鑽 0.34、交換 0.34、快速表計算 0.61、總計 0.61、復原 0.45、開啟工作表 0.5、新增 dashboard 0.76（含等待畫面穩定的 0.25～0.5 秒）。小資料下不會覺得卡 | 相同 | 低 | out/s05.txt～s39.txt 的 TIME 行（最早的 s04 沒有存檔） |
| F7 | 用語一致 | 「dashboard」與「儀表板 1」、「Story」與「故事 1」、「資料來源」（分頁）與「資料源」（其他地方）混用；英文殘留：Grand Total、Measure Names、Measure Values、Null、Jan 2021、MONTH(Order Date)；色盤代號；功能表的英文 dashboard、Story | 可用但不同 | 中 | c03_new_dashboard.png、d01_new_story.png、b43_refline_view.png、b10_markbtn_色彩.png |
| F8 | 空白狀態的提示 | 欄、列架有「將欄位拖曳到這裡」，空白工作表有一行提示；篩選、頁面、標記卡沒有；空白的 dashboard 沒有 | 可用但不同 | 低 | b03_new_sheet_empty.png、c03_new_dashboard.png |
| F9 | 錯誤訊息看得懂 | 公式的錯誤訊息清楚。其他的帶內部代號或原始錯誤：「查詢失敗：Duplicate data set name: "__ref_trend_pathgroup"」、「422 …dashboards[db_ee909b0c].zones[z10]：工作表「ws_sub_category_profit」不存在」、「計算欄位 f_editor_probe 的公式無法解析」；匯出失敗完全沒有訊息 | 可用但不同 | 中 | x01_dual_axis_trend_error.png、out/s21.txt、b29_calc_invalid.png |
| F10 | 視窗變窄（1024×700） | 工作表與 dashboard 編輯器沒有橫向捲軸，格式對話框的按鈕在畫面內，智慧顯示放得下。分頁很多時分頁列超出寬度（6032 像素放在 850 裡），沒有捲動按鈕 | 可用但不同 | 低 | f06_narrow_sheet.png、f08_narrow_format_dialog.png、f10_narrow_many_tabs.png |

## 二、最該先處理的 15 項

依「Tableau 使用者每天工作會被卡住的程度」排列。

1. **檢視與各個架上沒有右鍵選單（F1）**。Tableau 的編輯座標軸、格式、排序、隱藏、只保留、註解、檢視資料都從右鍵進去；我們要改到功能表列或膠囊選單找，很多入口根本不在使用者會找的地方。證據：f02_rclick_mark.png、out/s08.txt
2. **篩選與參數的控制項在編輯器裡不顯示（B26、B28）**。Tableau 邊做圖邊操作篩選卡與參數；我們要按「完成」離開編輯器才看得到，等於不能邊調邊看。證據：b22_filter_control_editor.png、b26_viewer_after_done.png、b25_param_control_editor.png
3. **Dashboard 的並排版面不能用拖的排（C2、C4、C5）**。Tableau 依放下的位置分割左右上下、拖邊界調大小、拖把手搬位置；我們一律往下排、邊界拖不動、並排的區塊搬不動。證據：c05_drag_second_right.png、c08_three_sheets.png、out/s24.txt
4. **Measure Values 拖到文字，數字全部疊在一起（B40）**。Tableau 自動把 Measure Names 放到欄；這是做交叉表最常見的第一步。證據：x03_measure_values_overlap.png
5. **匯出資料（CSV）與交叉資料表（Excel）在桌面版一定失敗，而且沒有訊息（E6、E7）**。Tableau 是工作表＞匯出；我們按了下載什麼都沒發生。證據：x06_export_csv_error.png、x07_export_xlsx_error.png、out/s33.txt
6. **雙軸加趨勢線整張圖查詢失敗（B39）**。Tableau 可以各軸各一條趨勢線。證據：x01_dual_axis_trend_error.png
7. **計算欄位編輯器只是文字方塊（B29）**。Tableau 有自動完成、函式清單與說明、語法著色、可以拖欄位進去、不擋住畫面；寫公式是分析師每天的工作。證據：b28_calc_autocomplete.png、b29_calc_invalid.png
8. **新做的圖長得跟 Tableau 不一樣（B15、B16、B17）**。離散日期預設是長條不是折線；離散的欄標題在下方、年直排、月份是數字；多個量值時列標題重複。使用者照同樣的步驟做，得到的圖第一眼就不同。證據：b11_date_line.png、b13_date_drilled.png、b05_dblclick_profit.png
9. **分頁列（B60、B63）**。隱藏的工作表全部留在分頁列上、工作表一律排在 dashboard 前面、不能拖曳排序、目前的分頁不會捲進畫面、沒有捲動按鈕；真實報表開起來是 42 個灰色分頁，dashboard 在看不到的最右邊。Tableau 只顯示沒有隱藏的分頁，順序可以拖。證據：a13_real_dashboard_editor.png、out/s37.txt
10. **連按兩下與滑過沒有反應（B20、B43、B44）**。Tableau 連按兩下座標軸開編輯座標軸、連按兩下標題編輯、滑過座標軸與標題有排序圖示；我們都沒有。證據：b15_hover_axis.png、b16_axis_dialog.png、b17_title_dialog.png
11. **圖例不是獨立的卡（B52）**。檢視寬一點圖例就被擠到畫面外，也不能點選醒目提示、右鍵改色彩。Tableau 的圖例固定在右側，可以互動。證據：b66_legend_click.png
12. **移除膠囊與快捷鍵（B54、B18、F5）**。Delete 不能移除、拖出架外不能移除（要拖回資料窗格）；Ctrl+W 在 Tableau 是交換欄列，在我們是關閉報表。證據：out/s09.txt、`web/src/desktop.ts` 的 `installShortcuts`
13. **色彩、工具提示、標籤、格式都是「按確定才套用」的對話框（B7、B9、B42）**。Tableau 是小面板或側邊窗格，改了立刻看得到；工具提示編輯器已有預設文字。我們的工具提示編輯器是空的，色盤是內部代號。證據：b10_markbtn_色彩.png、b10_markbtn_工具提示.png、b56_format_dialog.png
14. **檔案流程（A1、E2、E3、B61）**。開啟後先進檢視模式要再按編輯；只能存 .tmigx；匯出與列印要先離開編輯器；刪除工作表救不回來、刪除目前的工作表會被踢出編輯器。Tableau 開啟即編輯、另存 .twb／.twbx、刪除可以復原。證據：a12_open_real_twbx.png、out/s29.txt、x05_after_delete_current_sheet.png
15. **用語與內部代號外露（F7、F9、B23）**。dashboard／儀表板、Story／故事、資料來源／資料源混用，Grand Total、Jan 2021 等英文殘留；篩選「頂部」顯示 `[Calculation_1368…]`、錯誤訊息帶 `f_editor_probe`、`422`、`zones[z10]`。Tableau 的中文版用語一致，不會露出內部名稱。證據：b20_filter_tab3.png、c03_new_dashboard.png、b29_calc_invalid.png

## 三、我認為是程式錯誤的（附重現步驟）

1. **CSV／Excel 匯出失敗（E6、E7）**：開啟任何報表 → 檢視器工具列「匯出…」＞「資料…」＞「下載 CSV」（或「交叉資料表…」＞「下載」；或編輯器「分析＞檢視資料…＞下載 CSV」）→ 選位置 → 沒有檔案、沒有訊息。原因：`web/src/exporter.ts` 的 `saveFile` 把 .csv、.xlsx 也送到 `/api/desktop/export`，但 `service/src/tmig_service/desktop.py` 的 `EXPORT_SUFFIXES` 只有 `.png`、`.pptx`、`.pdf`，回 422；前端沒有顯示這個錯誤
2. **雙軸＋趨勢線（B39）**：新工作表 → Order Date 到欄、膠囊選「月（連續）」→ Sales、Profit 到列 → SUM(Profit) 膠囊「雙軸」→ 分析＞趨勢線…＞確定 →「查詢失敗：Duplicate data set name: "__ref_trend_pathgroup"」
3. **Measure Values 重疊（B40）**：新工作表 → Category 到列 → 資料窗格的 Measure Values 拖到「標籤」→ 8 個量值疊在同一格
4. **篩選「頂部」顯示內部名稱（B23）**：任一工作表 → Region 拖到篩選 →「頂部」分頁 →「依」的下拉有 `[Calculation_…]`
5. **預測加「以 0 填補缺少的值」後沒有預測（B37）**：新工作表 → 月（連續）× SUM(Sales) → 分析＞預測… → 勾「以 0 填補缺少的值」→ 確定 → 沒有預測段也沒有訊息（不勾就有）。只在 superstore_demo 上試過兩次，沒有查原因
6. **dashboard 的圖片物件（C9）**：dashboard 編輯器 → 物件「圖片」→ 立刻出現壞圖，網路請求 `GET /api/workbooks/<報表>/files/image.png` 回 404
7. **刪除目前的工作表（B61）**：在某張工作表上，分頁右鍵＞刪除＞確定 → 跳到檢視器的 dashboard（離開編輯器）；Ctrl+Z 不會還原刪除
8. **連按兩下非目前的分頁（B58）**：只切換分頁，不進入改名
9. **目前的分頁不捲進畫面（B63）**：開啟分頁很多的報表的 dashboard，分頁列停在最左邊

## 四、沒有實測到的項目與原因

- Tauri 外殼：原生的選檔與存檔對話框、原生功能表、安裝檔（這次是瀏覽器加本機引擎；此環境的選檔是 `window.prompt`）
- 與 Tableau Desktop 並排比對：這台電腦沒有 Tableau Desktop
- 列印與 PDF 的輸出結果（無頭瀏覽器不能列印）；PowerPoint 檔只確認有寫出，沒有打開
- 資料庫的 Live 連線、即時與萃取的切換、萃取篩選、Initial SQL（沒有資料庫引擎）
- 資料源頁的「隱藏欄位」：控制項存在，我的腳本因為同名欄位選到兩個而沒有按下去；聯集、樞紐、資料解譯器、自訂 SQL 沒有操作
- 形狀（標記類型與形狀調色盤）、頁面架、地圖、叢集、百分比、下拉線、盒鬚圖與參考區間：沒有操作
- 集合的使用（集合控制項、集合動作）、參數動作、網址動作的實際執行；動作對話框只看了表單
- 萬用字元、條件、頂部篩選的套用結果，相對日期與日期範圍，內容篩選，「套用到工作表」
- 排序對話框、小計的套用、數字格式套用後的結果、格式對話框各設定的效果、編輯座標軸各選項的效果
- 註解的建立與拖曳、說明（Caption）的顯示、工具提示中的工作表
- dashboard 的水平／垂直容器的排版、裝置版面的細部編輯、區塊的外距內距與背景
- Story 的文字物件與格式、把篩選狀態記到故事點
- 匯入工作表、取代資料源、Data Blending、發布到伺服器、從伺服器開啟
- 複製貼上（Ctrl+C／V）、Ctrl+M、F7、Ctrl+D 等其他快捷鍵
- 拖曳時跟著游標的影像（無頭環境看不到）
- 大資料量下的等待時間
