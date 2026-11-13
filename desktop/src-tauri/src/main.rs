//! 報表桌面版的外殼（D0）。
//!
//! - 啟動本機引擎（`tmig desktop serve`），從標準輸出讀第一行 JSON 取得埠號與權杖，
//!   主視窗再載入引擎提供的檢視器；外殼結束時關閉引擎（引擎也會在標準輸入關閉時自行結束）
//! - 檔案選單（開啟、儲存、另存新檔、關閉報表）與伺服器選單（登入、從伺服器開啟、發布、登出）
//!   呼叫檢視器的 `window.tmigDesktop`，邏輯只在檢視器與本機引擎中寫一次
//! - 選檔與存檔對話框由外殼提供（`pick_open_path`、`pick_data_path`、`pick_save_path`）
//! - 引擎的記錄寫到應用程式的記錄資料夾（engine.log）
//! - 只執行一份：工作區只有一個，第二份的引擎啟動時會清掉第一份開啟中的報表；
//!   再次啟動時把已開著的視窗帶到前面
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::fs::File;
use std::io::{BufRead, BufReader};
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::mpsc;
use std::sync::Mutex;
use std::time::Duration;

use serde::Deserialize;
use tauri::menu::{MenuBuilder, MenuItemBuilder, SubmenuBuilder};
use tauri::{AppHandle, Manager, RunEvent};
use tauri_plugin_dialog::{DialogExt, MessageDialogKind};

const TITLE: &str = "報表桌面版";
/// 第一次啟動時防毒軟體可能逐一掃描引擎的檔案，等久一點。
const ENGINE_TIMEOUT: Duration = Duration::from_secs(180);

/// 本機引擎的子行程。
struct Engine(Mutex<Option<Child>>);

#[derive(Deserialize)]
struct Ready {
    event: String,
    port: u16,
    token: String,
}

fn engine_command(app: &AppHandle) -> Result<Command, String> {
    // 開發時以環境變數指定引擎的指令，例如 TMIG_ENGINE="uv run --project .. tmig"
    if let Ok(custom) = std::env::var("TMIG_ENGINE") {
        let mut parts = custom.split_whitespace();
        let program = parts.next().ok_or("環境變數 TMIG_ENGINE 是空的")?;
        let mut command = Command::new(program);
        command.args(parts);
        return Ok(command);
    }
    let name = if cfg!(windows) { "tmig-engine.exe" } else { "tmig-engine" };
    let path = app
        .path()
        .resource_dir()
        .map_err(|e| e.to_string())?
        .join("engine")
        .join(name);
    if !path.is_file() {
        return Err(format!("找不到本機引擎：{}", path.display()));
    }
    Ok(Command::new(path))
}

fn start_engine(app: &AppHandle) -> Result<(Child, Ready), String> {
    let data = app.path().app_local_data_dir().map_err(|e| e.to_string())?;
    let logs = app.path().app_log_dir().map_err(|e| e.to_string())?;
    std::fs::create_dir_all(&logs).map_err(|e| e.to_string())?;
    let log_path = logs.join("engine.log");
    let log = File::create(&log_path).map_err(|e| format!("無法建立記錄檔：{e}"))?;
    let mut command = engine_command(app)?;
    command
        .args(["desktop", "serve", "--port", "0", "--clean", "--exit-with-parent"])
        .arg("--workspace")
        .arg(data.join("workspace"))
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::from(log));
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        // CREATE_NO_WINDOW：不顯示主控台視窗
        command.creation_flags(0x0800_0000);
    }
    let mut child = command
        .spawn()
        .map_err(|e| format!("無法啟動本機引擎：{e}"))?;
    let stdout = child.stdout.take().ok_or("無法讀取本機引擎的輸出")?;
    let (sender, receiver) = mpsc::channel();
    std::thread::spawn(move || {
        let mut lines = BufReader::new(stdout).lines();
        for line in lines.by_ref() {
            let Ok(line) = line else { break };
            if let Ok(ready) = serde_json::from_str::<Ready>(line.trim()) {
                if ready.event == "ready" {
                    let _ = sender.send(ready);
                    break;
                }
            }
        }
        // 讀到結束為止，引擎不會因為輸出沒人讀而停住
        for _ in lines {}
    });
    match receiver.recv_timeout(ENGINE_TIMEOUT) {
        Ok(ready) => Ok((child, ready)),
        Err(_) => {
            let _ = child.kill();
            Err(format!(
                "本機引擎沒有啟動。詳細原因請看記錄檔：\n{}",
                log_path.display()
            ))
        }
    }
}

fn stop_engine(app: &AppHandle) {
    let child = app.state::<Engine>().0.lock().ok().and_then(|mut slot| slot.take());
    if let Some(mut child) = child {
        let _ = child.kill();
        let _ = child.wait();
    }
}

fn launch(app: AppHandle) {
    match start_engine(&app) {
        Ok((child, ready)) => {
            if let Ok(mut slot) = app.state::<Engine>().0.lock() {
                *slot = Some(child);
            }
            let address = format!("http://127.0.0.1:{}/?token={}", ready.port, ready.token);
            let navigated = match (app.get_webview_window("main"), address.parse()) {
                (Some(window), Ok(url)) => window.navigate(url).is_ok(),
                _ => false,
            };
            if !navigated {
                fail(&app, "無法開啟檢視器".to_string());
            }
        }
        Err(message) => fail(&app, message),
    }
}

fn fail(app: &AppHandle, message: String) {
    app.dialog()
        .message(message)
        .title(TITLE)
        .kind(MessageDialogKind::Error)
        .blocking_show();
    stop_engine(app);
    app.exit(1);
}

fn build_menu(app: &AppHandle) -> tauri::Result<()> {
    let file = SubmenuBuilder::new(app, "檔案")
        .item(
            &MenuItemBuilder::with_id("new_workbook", "新增報表（連線到資料）…")
                .accelerator("CmdOrCtrl+N")
                .build(app)?,
        )
        .item(&MenuItemBuilder::with_id("connect_server", "新增報表（連線到伺服器）…").build(app)?)
        .item(&MenuItemBuilder::with_id("open", "開啟…").accelerator("CmdOrCtrl+O").build(app)?)
        .item(&MenuItemBuilder::with_id("save", "儲存").accelerator("CmdOrCtrl+S").build(app)?)
        .item(
            &MenuItemBuilder::with_id("save_as", "另存新檔…")
                .accelerator("CmdOrCtrl+Shift+S")
                .build(app)?,
        )
        .item(&MenuItemBuilder::with_id("close", "關閉報表").accelerator("CmdOrCtrl+W").build(app)?)
        .separator()
        .item(&MenuItemBuilder::with_id("quit", "結束").build(app)?)
        .build()?;
    let server = SubmenuBuilder::new(app, "伺服器")
        .item(&MenuItemBuilder::with_id("sign_in", "登入…").build(app)?)
        .item(&MenuItemBuilder::with_id("open_server", "從伺服器開啟…").build(app)?)
        .item(&MenuItemBuilder::with_id("publish", "發布到伺服器…").build(app)?)
        .item(&MenuItemBuilder::with_id("publish_datasource", "發布資料源…").build(app)?)
        .separator()
        .item(&MenuItemBuilder::with_id("sign_out", "登出").build(app)?)
        .build()?;
    let view = SubmenuBuilder::new(app, "檢視")
        .item(&MenuItemBuilder::with_id("home", "報表清單").build(app)?)
        .item(&MenuItemBuilder::with_id("reload", "重新整理").accelerator("F5").build(app)?)
        .build()?;
    let help = SubmenuBuilder::new(app, "說明")
        .item(&MenuItemBuilder::with_id("about", "關於報表桌面版").build(app)?)
        .build()?;
    let menu = MenuBuilder::new(app).items(&[&file, &server, &view, &help]).build()?;
    app.set_menu(menu)?;
    Ok(())
}

fn menu_action(app: &AppHandle, id: &str) {
    let script = match id {
        "new_workbook" => "window.tmigDesktop && window.tmigDesktop.newWorkbook()",
        "connect_server" => "window.tmigDesktop && window.tmigDesktop.connectServer()",
        "open" => "window.tmigDesktop && window.tmigDesktop.open()",
        "save" => "window.tmigDesktop && window.tmigDesktop.save()",
        "save_as" => "window.tmigDesktop && window.tmigDesktop.saveAs()",
        "close" => "window.tmigDesktop && window.tmigDesktop.close()",
        "sign_in" => "window.tmigDesktop && window.tmigDesktop.signIn()",
        "open_server" => "window.tmigDesktop && window.tmigDesktop.openFromServer()",
        "publish" => "window.tmigDesktop && window.tmigDesktop.publish()",
        "publish_datasource" => "window.tmigDesktop && window.tmigDesktop.publishDatasource()",
        "sign_out" => "window.tmigDesktop && window.tmigDesktop.signOut()",
        "home" => "window.location.assign('/')",
        "reload" => "window.location.reload()",
        "about" => {
            let version = app.package_info().version.to_string();
            app.dialog()
                .message(format!("{TITLE} {version}\n開啟 Tableau 的 .twbx／.twb 與報表套件 .tmigx"))
                .title(TITLE)
                .show(|_| {});
            return;
        }
        "quit" => {
            stop_engine(app);
            app.exit(0);
            return;
        }
        _ => return,
    };
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.eval(script);
    }
}

fn path_text(path: Option<tauri_plugin_dialog::FilePath>) -> Option<String> {
    path.and_then(|p| p.into_path().ok())
        .map(|p| p.to_string_lossy().into_owned())
}

/// 選擇要開啟的檔案（.twbx、.twb、.tmigx）；取消時回傳 null。
#[tauri::command]
async fn pick_open_path(app: AppHandle) -> Option<String> {
    tauri::async_runtime::spawn_blocking(move || {
        path_text(
            app.dialog()
                .file()
                .set_title("開啟")
                .add_filter("Tableau 活頁簿與報表套件", &["twbx", "twb", "tmigx"])
                .blocking_pick_file(),
        )
    })
    .await
    .ok()
    .flatten()
}

/// 選擇要連線的資料檔（Excel、文字檔、空間檔案）；取消時回傳 null。
#[tauri::command]
async fn pick_data_path(app: AppHandle) -> Option<String> {
    tauri::async_runtime::spawn_blocking(move || {
        path_text(
            app.dialog()
                .file()
                .set_title("連線到資料")
                .add_filter(
                    "資料檔",
                    &[
                        "xlsx", "xlsm", "xls", "csv", "tsv", "txt", "shp", "zip", "geojson", "json",
                        "kml", "kmz", "topojson",
                    ],
                )
                .add_filter("Excel", &["xlsx", "xlsm", "xls"])
                .add_filter("文字檔", &["csv", "tsv", "txt"])
                .add_filter("空間檔案", &["shp", "zip", "geojson", "json", "kml", "kmz", "topojson"])
                .blocking_pick_file(),
        )
    })
    .await
    .ok()
    .flatten()
}

/// 選擇存檔位置（預設為報表套件；匯出時另指定標題、副檔名與檔案類型的名稱）；取消時回傳 null。
#[tauri::command]
async fn pick_save_path(
    app: AppHandle,
    default_path: String,
    title: Option<String>,
    extension: Option<String>,
    label: Option<String>,
) -> Option<String> {
    tauri::async_runtime::spawn_blocking(move || {
        let default = PathBuf::from(default_path);
        let extension = extension.unwrap_or_else(|| "tmigx".to_string());
        let mut dialog = app
            .dialog()
            .file()
            .set_title(title.unwrap_or_else(|| "儲存報表套件".to_string()))
            .add_filter(label.unwrap_or_else(|| "報表套件".to_string()), &[extension.as_str()]);
        if let Some(folder) = default.parent().filter(|f| f.is_dir()) {
            dialog = dialog.set_directory(folder);
        }
        if let Some(name) = default.file_name().and_then(|n| n.to_str()) {
            dialog = dialog.set_file_name(name);
        }
        path_text(dialog.blocking_save_file())
    })
    .await
    .ok()
    .flatten()
}

fn bring_to_front(app: &AppHandle) {
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.unminimize();
        let _ = window.show();
        let _ = window.set_focus();
    }
}

fn main() {
    tauri::Builder::default()
        // 必須最先註冊：已經有一份在執行時，這一份不再啟動引擎就結束
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| bring_to_front(app)))
        .plugin(tauri_plugin_dialog::init())
        .manage(Engine(Mutex::new(None)))
        .invoke_handler(tauri::generate_handler![pick_open_path, pick_data_path, pick_save_path])
        .setup(|app| {
            let handle = app.handle().clone();
            build_menu(&handle)?;
            std::thread::spawn(move || launch(handle));
            Ok(())
        })
        .on_menu_event(|app, event| menu_action(app, event.id().as_ref()))
        .build(tauri::generate_context!())
        .expect("無法啟動報表桌面版")
        .run(|app, event| {
            if let RunEvent::Exit = event {
                stop_engine(app);
            }
        });
}
