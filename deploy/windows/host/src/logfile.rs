//! 記錄檔：每行加上本機時間；超過大小上限時改名成 .1（舊的往後推，最多留 KEEP 份）。

use std::fs::{self, File, OpenOptions};
use std::io::Write;
use std::path::{Path, PathBuf};

pub const MAX_BYTES: u64 = 10 * 1024 * 1024;
pub const KEEP: u32 = 5;

pub struct LogFile {
    path: PathBuf,
    max_bytes: u64,
    keep: u32,
    file: Option<File>,
    size: u64,
}

impl LogFile {
    pub fn new(path: PathBuf) -> Self {
        Self::with_limits(path, MAX_BYTES, KEEP)
    }

    pub fn with_limits(path: PathBuf, max_bytes: u64, keep: u32) -> Self {
        LogFile {
            path,
            max_bytes,
            keep,
            file: None,
            size: 0,
        }
    }

    pub fn path(&self) -> &Path {
        &self.path
    }

    /// 寫一行（自動加上時間與換行）。寫不進去時不中斷服務，只能放棄這一行。
    pub fn line(&mut self, text: &str) {
        let line = format!("{} {}\n", timestamp(), text.trim_end_matches(['\r', '\n']));
        if self.file.is_none() || self.size + line.len() as u64 > self.max_bytes {
            self.reopen(line.len() as u64);
        }
        if let Some(file) = self.file.as_mut() {
            if file.write_all(line.as_bytes()).is_ok() {
                let _ = file.flush();
                self.size += line.len() as u64;
            }
        }
    }

    fn reopen(&mut self, incoming: u64) {
        self.file = None;
        if let Some(parent) = self.path.parent() {
            let _ = fs::create_dir_all(parent);
        }
        let current = fs::metadata(&self.path).map(|m| m.len()).unwrap_or(0);
        if current > 0 && current + incoming > self.max_bytes {
            self.rotate();
        }
        match OpenOptions::new()
            .create(true)
            .append(true)
            .open(&self.path)
        {
            Ok(file) => {
                self.size = file.metadata().map(|m| m.len()).unwrap_or(0);
                self.file = Some(file);
            }
            Err(_) => self.size = 0,
        }
    }

    fn rotate(&self) {
        let numbered = |n: u32| {
            let mut name = self.path.as_os_str().to_owned();
            name.push(format!(".{n}"));
            PathBuf::from(name)
        };
        let _ = fs::remove_file(numbered(self.keep));
        for n in (1..self.keep).rev() {
            let _ = fs::rename(numbered(n), numbered(n + 1));
        }
        let _ = fs::rename(&self.path, numbered(1));
    }
}

#[cfg(windows)]
pub fn timestamp() -> String {
    use windows_sys::Win32::System::SystemInformation::GetLocalTime;
    // SAFETY：GetLocalTime 只寫入傳入的結構
    let t = unsafe {
        let mut t = std::mem::zeroed();
        GetLocalTime(&mut t);
        t
    };
    format!(
        "{:04}-{:02}-{:02} {:02}:{:02}:{:02}",
        t.wYear, t.wMonth, t.wDay, t.wHour, t.wMinute, t.wSecond
    )
}

#[cfg(not(windows))]
pub fn timestamp() -> String {
    let secs = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0);
    format!("@{secs}")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rotates_when_full_and_keeps_a_few() {
        let dir = std::env::temp_dir().join(format!("tmig-host-log-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        let path = dir.join("server.log");
        let mut log = LogFile::with_limits(path.clone(), 200, 2);
        for i in 0..40 {
            log.line(&format!("第 {i} 行"));
        }
        let size = |p: &Path| fs::metadata(p).map(|m| m.len()).unwrap_or(0);
        assert!(size(&path) <= 200);
        let first = PathBuf::from(format!("{}.1", path.display()));
        let second = PathBuf::from(format!("{}.2", path.display()));
        assert!(size(&first) > 0 && size(&first) <= 200);
        assert!(size(&second) > 0);
        assert!(!PathBuf::from(format!("{}.3", path.display())).exists());
        let latest = fs::read_to_string(&path).unwrap();
        assert!(latest.trim_end().ends_with("第 39 行"));
        let _ = fs::remove_dir_all(&dir);
    }
}
