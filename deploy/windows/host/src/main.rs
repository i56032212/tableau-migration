//! 報表平台伺服器的 Windows 服務主程式（TmigServer 服務）。
//!
//! ```text
//! tmig-server-host.exe service [--config <server.toml>]   由服務控制管理員啟動（安裝程式登錄的指令）
//! tmig-server-host.exe console [--config <server.toml>]   在主控台執行，Ctrl+C 或標準輸入關閉時結束（測試、排除問題）
//! tmig-server-host.exe check   [--config <server.toml>]   檢查設定並列出會執行的指令
//! ```
//!
//! 設定檔預設為 `%ProgramData%\TmigServer\server.toml`；Python、檢視器與 Chromium 在這個程式所在的資料夾。

mod config;
mod logfile;
#[cfg(windows)]
mod supervisor;

use std::path::{Path, PathBuf};

pub const SERVICE_NAME: &str = "TmigServer";

enum Mode {
    Service,
    Console,
    Check,
}

struct Options {
    mode: Mode,
    config: PathBuf,
}

const USAGE: &str = "用法：tmig-server-host.exe (service|console|check) [--config <server.toml>]";

fn parse_options(args: &[String]) -> Result<Options, String> {
    let mode = match args.first().map(String::as_str) {
        Some("service") => Mode::Service,
        Some("console") => Mode::Console,
        Some("check") => Mode::Check,
        _ => return Err(USAGE.to_string()),
    };
    let mut config = None;
    let mut rest = args[1..].iter();
    while let Some(arg) = rest.next() {
        match arg.as_str() {
            "--config" => match rest.next() {
                Some(path) => config = Some(PathBuf::from(path)),
                None => return Err(USAGE.to_string()),
            },
            _ => return Err(format!("不認得的參數「{arg}」\n{USAGE}")),
        }
    }
    let config = match config {
        Some(path) => path,
        None => default_config_path()?,
    };
    Ok(Options { mode, config })
}

fn default_config_path() -> Result<PathBuf, String> {
    let program_data = std::env::var_os("ProgramData")
        .ok_or_else(|| "沒有環境變數 ProgramData；以 --config 指定設定檔".to_string())?;
    Ok(PathBuf::from(program_data)
        .join(SERVICE_NAME)
        .join("server.toml"))
}

fn install_dir() -> Result<PathBuf, String> {
    let exe = std::env::current_exe().map_err(|e| format!("找不到服務主程式的位置：{e}"))?;
    exe.parent()
        .map(Path::to_path_buf)
        .ok_or_else(|| "找不到服務主程式所在的資料夾".to_string())
}

fn load_config(path: &Path) -> Result<config::Config, String> {
    let text = std::fs::read_to_string(path)
        .map_err(|e| format!("無法讀取設定檔 {}：{e}", path.display()))?;
    let absolute = std::path::absolute(path).unwrap_or_else(|_| path.to_path_buf());
    let data_dir = absolute
        .parent()
        .map(Path::to_path_buf)
        .ok_or_else(|| "找不到設定檔所在的資料夾".to_string())?;
    config::parse(&text, &data_dir, &install_dir()?).map_err(|e| format!("{}：{e}", path.display()))
}

fn check(path: &Path) -> Result<(), String> {
    let config = load_config(path)?;
    println!("設定檔：{}", path.display());
    println!("資料夾：{}", config.data_dir.display());
    println!("安裝目錄：{}", config.install_dir.display());
    let python = config.python();
    if !python.is_file() {
        println!("警告：找不到 {}", python.display());
    }
    for process in &config.processes {
        println!(
            "程序 {}：{}",
            process.name,
            config::display_command(&python, &config.arguments(process))
        );
    }
    for (key, _) in &config.environment {
        // 值可能是密碼等敏感資料：只列名稱
        println!("環境變數：{key}");
    }
    Ok(())
}

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let options = match parse_options(&args) {
        Ok(options) => options,
        Err(message) => {
            eprintln!("{message}");
            std::process::exit(2);
        }
    };
    let result = match options.mode {
        Mode::Check => check(&options.config),
        #[cfg(windows)]
        Mode::Console => windows_main::console(&options.config),
        #[cfg(windows)]
        Mode::Service => windows_main::service(options.config),
        #[cfg(not(windows))]
        Mode::Console | Mode::Service => Err("服務主程式只能在 Windows 上執行".to_string()),
    };
    if let Err(message) = result {
        eprintln!("失敗：{message}");
        std::process::exit(1);
    }
}

#[cfg(windows)]
mod windows_main {
    use std::ffi::OsString;
    use std::path::{Path, PathBuf};
    use std::sync::atomic::{AtomicBool, Ordering};
    use std::sync::{mpsc, Arc, Mutex, OnceLock};
    use std::time::Duration;

    use windows_service::service::{
        ServiceControl, ServiceControlAccept, ServiceExitCode, ServiceState, ServiceStatus,
        ServiceType,
    };
    use windows_service::service_control_handler::{self, ServiceControlHandlerResult};
    use windows_service::{define_windows_service, service_dispatcher};
    use windows_sys::Win32::Foundation::TRUE;
    use windows_sys::Win32::System::Console::SetConsoleCtrlHandler;

    use crate::logfile::LogFile;
    use crate::supervisor::{Echo, Supervisor};
    use crate::{load_config, SERVICE_NAME};

    static CONSOLE_STOP: AtomicBool = AtomicBool::new(false);
    static SERVICE_CONFIG: OnceLock<PathBuf> = OnceLock::new();

    fn host_log_for(path: &Path) -> Arc<Mutex<LogFile>> {
        let dir = path
            .parent()
            .map(Path::to_path_buf)
            .unwrap_or_else(|| PathBuf::from("."));
        Arc::new(Mutex::new(LogFile::new(dir.join("logs").join("host.log"))))
    }

    unsafe extern "system" fn on_console_ctrl(_kind: u32) -> i32 {
        CONSOLE_STOP.store(true, Ordering::SeqCst);
        TRUE
    }

    pub fn console(path: &Path) -> Result<(), String> {
        let host_log = host_log_for(path);
        let config = load_config(path)?;
        // SAFETY：登錄 Ctrl+C、關閉主控台的處理常式（只設定旗標）
        unsafe {
            SetConsoleCtrlHandler(Some(on_console_ctrl), TRUE);
        }
        // 標準輸入關閉時也結束（由其他程式啟動時的停止方式，例如 deploy/windows/smoke.py）
        std::thread::spawn(|| {
            use std::io::Read;
            let mut buffer = [0u8; 256];
            let mut stdin = std::io::stdin();
            while let Ok(n) = stdin.read(&mut buffer) {
                if n == 0 {
                    break;
                }
            }
            CONSOLE_STOP.store(true, Ordering::SeqCst);
        });
        let echo: Echo = Some(Arc::new(|line: &str| println!("{line}")));
        host_log
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .line(&format!("在主控台啟動（設定檔 {}）", path.display()));
        println!("按 Ctrl+C 結束（或關閉標準輸入）");
        let supervisor = Supervisor::start(config, host_log, echo)?;
        while !CONSOLE_STOP.load(Ordering::SeqCst) {
            std::thread::sleep(Duration::from_millis(200));
        }
        println!("結束中…");
        supervisor.shutdown();
        Ok(())
    }

    define_windows_service!(ffi_service_main, service_main);

    pub fn service(path: PathBuf) -> Result<(), String> {
        let _ = SERVICE_CONFIG.set(path);
        service_dispatcher::start(SERVICE_NAME, ffi_service_main).map_err(|e| {
            format!("無法連到服務控制管理員（{e}）；這個指令由服務啟動，測試時改用 console")
        })
    }

    fn service_main(_arguments: Vec<OsString>) {
        let Some(path) = SERVICE_CONFIG.get().cloned() else {
            return;
        };
        let host_log = host_log_for(&path);
        if let Err(message) = run_service(&path, host_log.clone()) {
            host_log
                .lock()
                .unwrap_or_else(|e| e.into_inner())
                .line(&format!("服務停止：{message}"));
        }
    }

    fn status(state: ServiceState, exit: ServiceExitCode, wait: Duration) -> ServiceStatus {
        ServiceStatus {
            service_type: ServiceType::OWN_PROCESS,
            current_state: state,
            controls_accepted: if state == ServiceState::Running {
                ServiceControlAccept::STOP | ServiceControlAccept::SHUTDOWN
            } else {
                ServiceControlAccept::empty()
            },
            exit_code: exit,
            checkpoint: 0,
            wait_hint: wait,
            process_id: None,
        }
    }

    fn run_service(path: &Path, host_log: Arc<Mutex<LogFile>>) -> Result<(), String> {
        let (stop_tx, stop_rx) = mpsc::channel::<()>();
        let handler = move |control| match control {
            ServiceControl::Stop | ServiceControl::Shutdown => {
                let _ = stop_tx.send(());
                ServiceControlHandlerResult::NoError
            }
            ServiceControl::Interrogate => ServiceControlHandlerResult::NoError,
            _ => ServiceControlHandlerResult::NotImplemented,
        };
        let handle = service_control_handler::register(SERVICE_NAME, handler)
            .map_err(|e| format!("無法登錄服務的控制處理常式：{e}"))?;
        let set = |state, exit, wait| {
            let _ = handle.set_service_status(status(state, exit, wait));
        };
        set(
            ServiceState::StartPending,
            ServiceExitCode::Win32(0),
            Duration::from_secs(30),
        );
        host_log
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .line(&format!("服務啟動（設定檔 {}）", path.display()));
        let started =
            load_config(path).and_then(|config| Supervisor::start(config, host_log.clone(), None));
        let supervisor = match started {
            Ok(supervisor) => supervisor,
            Err(message) => {
                set(
                    ServiceState::Stopped,
                    ServiceExitCode::ServiceSpecific(1),
                    Duration::ZERO,
                );
                return Err(message);
            }
        };
        set(
            ServiceState::Running,
            ServiceExitCode::Win32(0),
            Duration::ZERO,
        );
        let _ = stop_rx.recv();
        set(
            ServiceState::StopPending,
            ServiceExitCode::Win32(0),
            Duration::from_secs(30),
        );
        host_log
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .line("服務停止中");
        supervisor.shutdown();
        set(
            ServiceState::Stopped,
            ServiceExitCode::Win32(0),
            Duration::ZERO,
        );
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_modes_and_config() {
        let options = parse_options(&[
            "console".into(),
            "--config".into(),
            r"D:\a\server.toml".into(),
        ])
        .unwrap();
        assert!(matches!(options.mode, Mode::Console));
        assert_eq!(options.config, PathBuf::from(r"D:\a\server.toml"));
        assert!(parse_options(&[]).is_err());
        assert!(parse_options(&["start".into()]).is_err());
        assert!(parse_options(&["check".into(), "--config".into()]).is_err());
        assert!(parse_options(&["check".into(), "--port".into(), "1".into()]).is_err());
    }
}
