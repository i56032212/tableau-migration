//! 啟動並監看 server.toml 中的程序：輸出寫進各自的記錄檔，意外結束時等一下再重新啟動（5 秒起、
//! 最多 60 秒；跑超過一分鐘才結束的從 5 秒重新算）。所有子程序（含它們再啟動的 hyperd、Chromium）都放在
//! 同一個 Job 物件中：停止服務時一次結束，服務主程式異常結束時也由系統一併結束，不留下孤兒程序。

use std::io::{BufRead, BufReader, Read};
use std::os::windows::io::AsRawHandle;
use std::os::windows::process::CommandExt;
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Condvar, Mutex};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

use windows_sys::Win32::Foundation::{CloseHandle, HANDLE};
use windows_sys::Win32::System::JobObjects::{
    AssignProcessToJobObject, CreateJobObjectW, JobObjectExtendedLimitInformation,
    SetInformationJobObject, TerminateJobObject, JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
};
use windows_sys::Win32::System::Threading::{CREATE_NEW_PROCESS_GROUP, CREATE_NO_WINDOW};

use crate::config::{display_command, Config, Process};
use crate::logfile::LogFile;

const FIRST_DELAY: Duration = Duration::from_secs(5);
const MAX_DELAY: Duration = Duration::from_secs(60);
const HEALTHY_RUN: Duration = Duration::from_secs(60);

/// 主控台模式時也把每行輸出印到主控台。
pub type Echo = Option<Arc<dyn Fn(&str) + Send + Sync>>;

struct Job(HANDLE);

// SAFETY：Job 物件的 handle 可以在任何執行緒使用
unsafe impl Send for Job {}
unsafe impl Sync for Job {}

impl Job {
    fn new() -> Result<Job, String> {
        // SAFETY：建立沒有名稱的 Job 物件；失敗時回傳 null
        let handle = unsafe { CreateJobObjectW(std::ptr::null(), std::ptr::null()) };
        if handle.is_null() {
            return Err(format!(
                "無法建立 Job 物件：{}",
                std::io::Error::last_os_error()
            ));
        }
        let job = Job(handle);
        // SAFETY：結構全部填 0 再設定旗標，大小與型別相符
        let ok = unsafe {
            let mut info: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = std::mem::zeroed();
            info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
            SetInformationJobObject(
                job.0,
                JobObjectExtendedLimitInformation,
                &info as *const _ as *const std::ffi::c_void,
                std::mem::size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
            )
        };
        if ok == 0 {
            return Err(format!(
                "無法設定 Job 物件：{}",
                std::io::Error::last_os_error()
            ));
        }
        Ok(job)
    }

    fn assign(&self, child: &Child) -> Result<(), String> {
        // SAFETY：child 的 handle 在 Child 存在時有效
        let ok = unsafe { AssignProcessToJobObject(self.0, child.as_raw_handle() as HANDLE) };
        if ok == 0 {
            return Err(format!("{}", std::io::Error::last_os_error()));
        }
        Ok(())
    }

    fn terminate(&self) {
        // SAFETY：結束 Job 中所有的程序
        unsafe {
            TerminateJobObject(self.0, 1);
        }
    }
}

impl Drop for Job {
    fn drop(&mut self) {
        // SAFETY：最後一個 handle 關閉時，系統結束 Job 中剩下的程序（KILL_ON_JOB_CLOSE）
        unsafe {
            CloseHandle(self.0);
        }
    }
}

/// 停止的訊號：設定後喚醒正在等待重新啟動的執行緒。
#[derive(Clone)]
struct Stop {
    flag: Arc<AtomicBool>,
    wake: Arc<(Mutex<()>, Condvar)>,
}

impl Stop {
    fn new() -> Stop {
        Stop {
            flag: Arc::new(AtomicBool::new(false)),
            wake: Arc::new((Mutex::new(()), Condvar::new())),
        }
    }

    fn is_set(&self) -> bool {
        self.flag.load(Ordering::SeqCst)
    }

    fn set(&self) {
        self.flag.store(true, Ordering::SeqCst);
        let (lock, condvar) = &*self.wake;
        let _guard = lock.lock().unwrap_or_else(|e| e.into_inner());
        condvar.notify_all();
    }

    /// 等 delay；中途停止時回傳 true。
    fn wait(&self, delay: Duration) -> bool {
        let deadline = Instant::now() + delay;
        let (lock, condvar) = &*self.wake;
        let mut guard = lock.lock().unwrap_or_else(|e| e.into_inner());
        while !self.is_set() {
            let now = Instant::now();
            if now >= deadline {
                break;
            }
            guard = condvar
                .wait_timeout(guard, deadline - now)
                .unwrap_or_else(|e| e.into_inner())
                .0;
        }
        self.is_set()
    }
}

pub struct Supervisor {
    job: Arc<Job>,
    stop: Stop,
    threads: Vec<JoinHandle<()>>,
    host_log: Arc<Mutex<LogFile>>,
    echo: Echo,
}

impl Supervisor {
    pub fn start(
        config: Config,
        host_log: Arc<Mutex<LogFile>>,
        echo: Echo,
    ) -> Result<Supervisor, String> {
        if !config.python().is_file() {
            return Err(format!(
                "找不到 {}（安裝不完整，請重新安裝）",
                config.python().display()
            ));
        }
        for dir in [config.logs_dir(), config.temp_dir()] {
            std::fs::create_dir_all(&dir)
                .map_err(|e| format!("無法建立資料夾 {}：{e}", dir.display()))?;
        }
        let job = Arc::new(Job::new()?);
        let stop = Stop::new();
        let config = Arc::new(config);
        let mut threads = Vec::new();
        for process in config.processes.clone() {
            let (job, stop, config, host_log, echo) = (
                job.clone(),
                stop.clone(),
                config.clone(),
                host_log.clone(),
                echo.clone(),
            );
            let name = format!("supervise-{}", process.name);
            let handle = thread::Builder::new()
                .name(name)
                .spawn(move || supervise(&config, &process, &job, &stop, &host_log, &echo))
                .map_err(|e| format!("無法啟動監看的執行緒：{e}"))?;
            threads.push(handle);
        }
        Ok(Supervisor {
            job,
            stop,
            threads,
            host_log,
            echo,
        })
    }

    /// 結束所有程序並等監看的執行緒結束。
    pub fn shutdown(self) {
        self.stop.set();
        self.job.terminate();
        for thread in self.threads {
            let _ = thread.join();
        }
        log(&self.host_log, &self.echo, "所有程序已結束");
    }
}

fn log(host_log: &Arc<Mutex<LogFile>>, echo: &Echo, text: &str) {
    host_log
        .lock()
        .unwrap_or_else(|e| e.into_inner())
        .line(text);
    if let Some(echo) = echo {
        echo(&format!("[host] {text}"));
    }
}

fn supervise(
    config: &Config,
    process: &Process,
    job: &Job,
    stop: &Stop,
    host_log: &Arc<Mutex<LogFile>>,
    echo: &Echo,
) {
    let output = Arc::new(Mutex::new(LogFile::new(
        config.logs_dir().join(format!("{}.log", process.name)),
    )));
    let program = config.python();
    let args = config.arguments(process);
    // 下一次意外結束時等多久：5 秒起，連續很快就結束時加倍（最多 60 秒）
    let mut delay = FIRST_DELAY;
    while !stop.is_set() {
        log(
            host_log,
            echo,
            &format!(
                "啟動 {}：{}",
                process.name,
                display_command(&program, &args)
            ),
        );
        let started = Instant::now();
        let mut command = Command::new(&program);
        command
            .args(&args)
            .current_dir(&config.data_dir)
            .envs(config.environment_for_children())
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            // 沒有主控台視窗；不收主控台的 Ctrl+C（由服務主程式結束）
            .creation_flags(CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP);
        match command.spawn() {
            Ok(mut child) => {
                if let Err(e) = job.assign(&child) {
                    log(
                        host_log,
                        echo,
                        &format!("警告：{} 無法加入 Job 物件：{e}", process.name),
                    );
                }
                if stop.is_set() {
                    let _ = child.kill();
                }
                let readers: Vec<_> = [
                    child
                        .stdout
                        .take()
                        .map(|s| Box::new(s) as Box<dyn Read + Send>),
                    child
                        .stderr
                        .take()
                        .map(|s| Box::new(s) as Box<dyn Read + Send>),
                ]
                .into_iter()
                .flatten()
                .map(|stream| {
                    let (output, echo, name) = (output.clone(), echo.clone(), process.name.clone());
                    thread::spawn(move || copy_lines(stream, &output, &echo, &name))
                })
                .collect();
                let status = child.wait();
                for reader in readers {
                    let _ = reader.join();
                }
                if stop.is_set() {
                    break;
                }
                let code = match status {
                    Ok(status) => status
                        .code()
                        .map(|c| format!("結束碼 {c}"))
                        .unwrap_or_else(|| "沒有結束碼".to_string()),
                    Err(e) => format!("無法取得結束狀態：{e}"),
                };
                if started.elapsed() >= HEALTHY_RUN {
                    delay = FIRST_DELAY;
                }
                log(
                    host_log,
                    echo,
                    &format!(
                        "{} 意外結束（{code}），{} 秒後重新啟動；原因見 {}",
                        process.name,
                        delay.as_secs(),
                        output
                            .lock()
                            .unwrap_or_else(|e| e.into_inner())
                            .path()
                            .display()
                    ),
                );
            }
            Err(e) => {
                log(
                    host_log,
                    echo,
                    &format!(
                        "無法啟動 {}：{e}；{} 秒後再試",
                        process.name,
                        delay.as_secs()
                    ),
                );
            }
        }
        let wait = delay;
        delay = (delay * 2).min(MAX_DELAY);
        if stop.wait(wait) {
            break;
        }
    }
}

fn copy_lines(stream: Box<dyn Read + Send>, output: &Arc<Mutex<LogFile>>, echo: &Echo, name: &str) {
    let mut reader = BufReader::new(stream);
    let mut buffer = Vec::new();
    loop {
        buffer.clear();
        match reader.read_until(b'\n', &mut buffer) {
            Ok(0) | Err(_) => break,
            Ok(_) => {
                let text = String::from_utf8_lossy(&buffer);
                output.lock().unwrap_or_else(|e| e.into_inner()).line(&text);
                if let Some(echo) = echo {
                    echo(&format!("[{name}] {}", text.trim_end()));
                }
            }
        }
    }
}
