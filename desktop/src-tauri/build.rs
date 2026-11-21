// 自己的指令（選檔、選資料夾、存檔對話框，結束與關於）要在 app manifest 中宣告，才會產生 allow-pick-open-path 等權限；
// 主視窗載入的是本機引擎的頁面（遠端網址），沒有在 capabilities 中允許的指令一律被擋下。
const COMMANDS: &[&str] = &[
    "pick_open_path",
    "pick_data_path",
    "pick_folder_path",
    "pick_save_path",
    "quit_app",
    "about_text",
];

fn main() {
    tauri_build::try_build(
        tauri_build::Attributes::new()
            .app_manifest(tauri_build::AppManifest::new().commands(COMMANDS)),
    )
    .expect("tauri-build 失敗");
}
