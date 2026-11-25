# 報表桌面版（D0）

裝在作者電腦上的桌面應用程式：開啟 Tableau 的 .twbx／.twb 或平台的報表套件 .tmigx，以平台的引擎檢視與查詢，
另存成報表套件，登入伺服器、發布與從伺服器開啟。規格見 Claude Docs 的「桌面版編輯器規格」；
這是第一階段的骨架（D0），編輯在 D1 之後。

```
desktop/
├── src-tauri/        外殼（Tauri 2，Rust）：啟動本機引擎、主視窗、選檔與存檔對話框（沒有原生選單，功能表列在頁面裡）
├── ui/index.html     引擎就緒前的啟動畫面
└── engine/           本機引擎的打包：entry.py（進入點）、tmig-engine.spec（PyInstaller）、smoke.py（冒煙測試）
```

## 運作方式

1. 外殼啟動本機引擎 `tmig desktop serve --port 0 --clean --exit-with-parent --workspace <工作區>`
   （安裝後是 `resources/engine/tmig-engine.exe`），從標準輸出讀第一行 JSON：`{"event":"ready","port":…,"token":…}`
2. 主視窗開啟 `http://127.0.0.1:<埠號>/?token=<權杖>`；引擎把權杖換成 cookie，之後只接受帶權杖的本機請求
3. 檢視器在桌面版模式（`/api/me` 的 mode 為 desktop）多了「開啟檔案…」「儲存」「另存新檔…」「關閉」；
   功能表列在頁面裡（外殼沒有原生選單，與 Tableau Desktop 相同只有一排）；檔案指令的快捷鍵
   （Ctrl+O、Ctrl+S、Ctrl+Shift+S、Ctrl+W）也由檢視器處理，呼叫 `window.tmigDesktop`
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
5. 發布資料源…（或資料源分頁的「發布到伺服器…」）：選報表中的資料源、Project 與名稱，存成資料源套件（.tmigds：
   定義、用到的參數與萃取）上傳；已經連到伺服器上的資料源時預設覆蓋它。「報表改用發布的資料源」時，之後發布的
   報表在伺服器上直接使用伺服器上的資料源

連線用作業系統的憑證存放區（公司內部 CA 簽發的伺服器憑證不必另外設定）；代理伺服器依系統設定
（Windows 的網際網路選項，或 `HTTPS_PROXY` 等環境變數），本機的網址與例外清單（Windows 的「不使用 Proxy 伺服器的位址」、
`NO_PROXY`）中的伺服器直接連線。

開啟的檔案會複製到工作區（`%LOCALAPPDATA%\tw.tmig.desktop\workspace`），原始檔不會被鎖住或改動；
引擎的記錄寫在應用程式的記錄資料夾（`engine.log`）。資料庫的帳號密碼不會寫進報表套件。

## 安裝檔

CI 的 desktop 工作流程（`.github/workflows/desktop.yml`）在 Windows 上打包並產生 NSIS 安裝檔，
在該次執行的 Artifacts 下載 `TmigDesktop-windows-installer`。安裝在使用者自己的資料夾，不需要系統管理員權限；
需要 WebView2（Windows 10／11 通常已內建，沒有時安裝程式會下載）。

本機引擎帶著所有資料庫驅動（kernel 的 optional-dependencies：PostgreSQL、Oracle、SQL Server、MySQL、Databricks、
Snowflake、Redshift、Trino／Presto、Athena、BigQuery、Teradata、Hive／Impala、Db2、SAP HANA），冒煙測試逐一檢查；
PyInstaller 不會自動帶上的 Db2 clidriver 與 Teradata 的原生程式庫由 `tmig-engine.spec` 與執行期掛鉤
`rthook_ibm_db.py` 帶上。

**離線安裝檔**：給不能連網路的電腦。以 `src-tauri/tauri.offline.conf.json` 建置時，安裝檔內含 WebView2 執行階段的
離線安裝程式（多約 130 MB），沒有 WebView2 的電腦安裝時直接安裝它，不必下載：

```bash
cd desktop && pnpm tauri build --config src-tauri/tauri.offline.conf.json
```

產生的檔名與一般的相同（`TmigDesktop_<版本>_x64-setup.exe`），發布時改名成 `TmigDesktop_<版本>_x64-offline-setup.exe`。
建置時 Tauri 會從 Microsoft 下載 WebView2 的離線安裝程式。

### 在 Windows 本機建置安裝檔

GitHub Actions 沒有在跑時，可以在 Windows 電腦上自己建。需要 Rust（`winget install --id Rustlang.Rustup -e`）與
Visual Studio Build Tools 的 C++ 工作負載（`winget install --id Microsoft.VisualStudio.2022.BuildTools -e
--override "--passive --wait --norestart --add Microsoft.VisualStudio.Workload.VCTools --includeRecommended"`，
約 9 GB，安裝時要系統管理員確認；公司用途要留意 Visual Studio 的授權）。步驟與 CI 相同，在 repo 根目錄：

```bash
cd web && pnpm install --frozen-lockfile && pnpm build && cd ..      # 檢視器（web/dist 已是最新時可以略過）
# 打包用另一個環境（含所有資料庫驅動），不動開發用的 .venv（裝了驅動之後，測驅動沒有安裝時的測試會不一樣）
export UV_PROJECT_ENVIRONMENT=desktop/engine/build/venv UV_PYTHON_PREFERENCE=only-system
uv sync --frozen --no-dev --all-packages --all-extras --python 3.12
uv run --no-sync --with pyinstaller pyinstaller desktop/engine/tmig-engine.spec --distpath desktop/engine/dist --workpath desktop/engine/build/pyinstaller --noconfirm
unset UV_PROJECT_ENVIRONMENT UV_PYTHON_PREFERENCE
python desktop/engine/smoke.py desktop/engine/dist/tmig-engine/tmig-engine.exe fixtures/superstore_demo/superstore_demo.twbx
cd desktop && pnpm install --frozen-lockfile && pnpm tauri build     # 第一次編譯約 8 分鐘；離線安裝檔加 --config src-tauri/tauri.offline.conf.json
```

安裝檔在 `desktop/src-tauri/target/release/bundle/nsis/TmigDesktop_<版本>_x64-setup.exe`。建置的中間產物
（`desktop/src-tauri/target` 約 2 GB、`desktop/engine/dist`、`desktop/engine/build`）都不進版控，可以刪。安裝檔沒有程式碼簽章，
第一次執行時 Windows SmartScreen 會警告。不安裝也可以直接執行 `desktop/src-tauri/target/release/tmig-desktop.exe`
（引擎在同一個資料夾的 `engine/`）。

### 發布安裝檔

安裝檔超過 GitHub 單一檔案 100 MB 的上限，不進 git：建好之後放在這個 repo 的 GitHub Release（repo 目前是公開的，Release 誰都下載得到），tag 寫成
`desktop-v<版本>-<日期>`，指向建置時的 commit，說明寫上 SHA-256、內容的 commit、驗證過與還沒驗證的；還沒實際安裝
執行過的標成預先發行版（prerelease）。伺服器版的離線安裝檔（`deploy/windows/build.py`）也照同樣的方式放，
tag 寫成 `offline-v<版本>-<日期>` 時兩份放在同一個 Release：

```bash
gh release create desktop-v0.1.0-20261124 desktop/src-tauri/target/release/bundle/nsis/TmigDesktop_0.1.0_x64-setup.exe   --target <commit> --title "桌面版 0.1.0（2026-11-24）" --notes-file <說明> --prerelease
```

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

確認與訊息：外殼的對話框外掛（tauri-plugin-dialog）會把頁面的 `window.confirm`、`window.alert` 換成原生的訊息方塊，
而且 `window.confirm` 變成非同步的（回傳 Promise）。所以前端一律用 `web/src/confirm.ts` 的 `confirmBox`（等結果），
不直接寫 `if (!window.confirm(…))`——Promise 是真值，那樣寫在外殼中等於沒有問就做了；capabilities 要允許
`dialog:allow-message`，否則訊息方塊出不來（`confirmBox` 這時當作取消）。`service/tests/test_desktop_viewer.py` 以與外殼
相同的非同步 `window.confirm` 測試，並檢查前端沒有直接呼叫 `window.confirm`。

Linux 上建置外殼需要 WebKitGTK 等開發套件（`libwebkit2gtk-4.1-dev libgtk-3-dev librsvg2-dev
libayatana-appindicator3-dev`）。
