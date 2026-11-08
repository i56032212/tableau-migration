# 報表桌面版（D0）

裝在作者電腦上的桌面應用程式：開啟 Tableau 的 .twbx／.twb 或平台的報表套件 .tmigx，以平台的引擎檢視與查詢，
另存成報表套件，登入伺服器、發布與從伺服器開啟。規格見 Claude Docs 的「桌面版編輯器規格」；
這是第一階段的骨架（D0），編輯在 D1 之後。

```
desktop/
├── src-tauri/        外殼（Tauri 2，Rust）：啟動本機引擎、主視窗、檔案選單、選檔與存檔對話框
├── ui/index.html     引擎就緒前的啟動畫面
└── engine/           本機引擎的打包：entry.py（進入點）、tmig-engine.spec（PyInstaller）、smoke.py（冒煙測試）
```

## 運作方式

1. 外殼啟動本機引擎 `tmig desktop serve --port 0 --clean --exit-with-parent --workspace <工作區>`
   （安裝後是 `resources/engine/tmig-engine.exe`），從標準輸出讀第一行 JSON：`{"event":"ready","port":…,"token":…}`
2. 主視窗開啟 `http://127.0.0.1:<埠號>/?token=<權杖>`；引擎把權杖換成 cookie，之後只接受帶權杖的本機請求
3. 檢視器在桌面版模式（`/api/me` 的 mode 為 desktop）多了「開啟檔案…」「儲存」「另存新檔…」「關閉」；
   外殼的檔案選單（Ctrl+O、Ctrl+S、Ctrl+Shift+S、Ctrl+W）呼叫檢視器的 `window.tmigDesktop`
4. 外殼結束時關閉引擎；外殼異常結束時標準輸入關閉，引擎也會自行結束
5. 只執行一份：工作區只有一個，再次啟動時把已開著的視窗帶到前面，不會啟動第二個引擎

## 伺服器（登入、發布、從伺服器開啟）

1. 伺服器 → 登入…（或首頁的「登入伺服器」）：輸入伺服器網址；桌面版開啟系統瀏覽器到伺服器的核准頁，
   以平常的方式登入伺服器後按「允許」，瀏覽器導回本機引擎（`/desktop-auth/callback`），引擎換得權杖
2. 權杖存在 Windows 的認證管理員（`tmig-desktop:<伺服器網址>`），設定檔 `desktop-server.json` 只記網址與帳號；
   伺服器 → 登出 時在伺服器撤銷並刪除
3. 發布到伺服器…：選 Project 與名稱（或覆蓋從伺服器開啟、發布過的那一份），先在這台電腦檢查每張工作表，
   存成報表套件上傳，伺服器再檢查一次；伺服器上已有別人發布的新版本時列出差異，由作者決定覆蓋
4. 從伺服器開啟…：列出有權限下載的報表，下載後開啟並記下版本（之後發布以這一版為基準）

連線用作業系統的憑證存放區（公司內部 CA 簽發的伺服器憑證不必另外設定）；代理伺服器依系統設定
（Windows 的網際網路選項，或 `HTTPS_PROXY` 等環境變數），本機的網址與例外清單（Windows 的「不使用 Proxy 伺服器的位址」、
`NO_PROXY`）中的伺服器直接連線。

開啟的檔案會複製到工作區（`%LOCALAPPDATA%\tw.tmig.desktop\workspace`），原始檔不會被鎖住或改動；
引擎的記錄寫在應用程式的記錄資料夾（`engine.log`）。資料庫的帳號密碼不會寫進報表套件。

## 安裝檔

CI 的 desktop 工作流程（`.github/workflows/desktop.yml`）在 Windows 上打包並產生 NSIS 安裝檔，
在該次執行的 Artifacts 下載 `TmigDesktop-windows-installer`。安裝在使用者自己的資料夾，不需要系統管理員權限；
需要 WebView2（Windows 10／11 通常已內建，沒有時安裝程式會下載）。

## 開發

```bash
# 只開發檢視器與引擎：瀏覽器中沒有外殼，選檔改成輸入完整路徑
uv run tmig desktop serve --workspace /tmp/tmig-ws --port 8765   # 印出權杖後開 http://127.0.0.1:8765/?token=<權杖>

# 外殼：以環境變數指定引擎的指令（不必先打包）
cd desktop && pnpm install
TMIG_ENGINE="uv run --project .. tmig" pnpm tauri dev

# 打包引擎與冒煙測試（Linux、Windows 都可以）
uv run --with pyinstaller pyinstaller desktop/engine/tmig-engine.spec \
    --distpath desktop/engine/dist --workpath desktop/engine/build --noconfirm
python desktop/engine/smoke.py desktop/engine/dist/tmig-engine/tmig-engine fixtures/superstore_demo/superstore_demo.twbx
```

在 Linux 上開發時，權杖存在設定資料夾的 `credentials.json`（權限 0600）；CI 在 Windows 上另外測試認證管理員。

外殼自己的指令（`pick_open_path`、`pick_save_path`）要在 `src-tauri/build.rs` 宣告、在
`src-tauri/capabilities/default.json` 允許：主視窗載入的是引擎的頁面（遠端網址），沒有允許的指令一律被擋下
（錯誤訊息為「not allowed」），新增指令時兩處都要加。打包後的引擎以 UTF-8 模式執行（`tmig-engine.spec`）。

Linux 上建置外殼需要 WebKitGTK 等開發套件（`libwebkit2gtk-4.1-dev libgtk-3-dev librsvg2-dev
libayatana-appindicator3-dev`）。
