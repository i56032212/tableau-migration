//! server.toml：服務要執行的程序與傳給它們的環境變數。
//!
//! ```toml
//! [environment]
//! TMIG_CONNECTIONS = '{data}\connections.toml'
//!
//! [[process]]
//! name = "server"
//! args = ["serve", "--host", "127.0.0.1", "--port", "8000", "--fixtures", '{data}\workbooks']
//! ```
//!
//! 每個程序執行 `<安裝目錄>\python\python.exe -m tmig.cli <args>`；`{data}` 代換成 server.toml 所在的資料夾，
//! `{install}` 代換成安裝目錄。這裡只處理設定本身（與作業系統無關），可以在任何平台上測試。

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use serde::Deserialize;

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct RawConfig {
    #[serde(default)]
    environment: BTreeMap<String, String>,
    #[serde(default)]
    process: Vec<RawProcess>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct RawProcess {
    name: String,
    #[serde(default = "enabled_by_default")]
    enabled: bool,
    args: Vec<String>,
}

fn enabled_by_default() -> bool {
    true
}

/// 一個要常駐執行的程序（tmig 的子指令）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Process {
    pub name: String,
    pub args: Vec<String>,
}

#[derive(Debug, Clone)]
pub struct Config {
    pub data_dir: PathBuf,
    pub install_dir: PathBuf,
    /// 設定檔中的環境變數（已代換 `{data}`、`{install}`），套用在預設的環境變數之後。
    pub environment: Vec<(String, String)>,
    /// 啟用的程序（enabled = false 的不在這裡）。
    pub processes: Vec<Process>,
}

impl Config {
    /// 安裝目錄中的 Python。
    pub fn python(&self) -> PathBuf {
        self.install_dir.join("python").join("python.exe")
    }

    pub fn logs_dir(&self) -> PathBuf {
        self.data_dir.join("logs")
    }

    pub fn temp_dir(&self) -> PathBuf {
        self.data_dir.join("temp")
    }

    /// 程序的參數：`-X utf8 -s -m tmig.cli <args>`（-s：不讀服務帳號自己的 site-packages）。
    pub fn arguments(&self, process: &Process) -> Vec<String> {
        let mut args: Vec<String> = ["-X", "utf8", "-s", "-m", "tmig.cli"]
            .iter()
            .map(|s| s.to_string())
            .collect();
        args.extend(process.args.iter().cloned());
        args
    }

    /// 傳給每個程序的環境變數：先是預設的，再是設定檔的（同名時以設定檔為準）。
    pub fn environment_for_children(&self) -> Vec<(String, String)> {
        let install = &self.install_dir;
        let mut env = vec![
            ("PYTHONUTF8".to_string(), "1".to_string()),
            ("PYTHONIOENCODING".to_string(), "utf-8".to_string()),
            ("PYTHONNOUSERSITE".to_string(), "1".to_string()),
            // 訂閱產生 PNG、PDF 用的 Chromium（安裝檔內附，不必連網路下載）
            (
                "PLAYWRIGHT_BROWSERS_PATH".to_string(),
                path_text(&install.join("ms-playwright")),
            ),
            (
                "TMIG_WEB_DIST".to_string(),
                path_text(&install.join("web").join("dist")),
            ),
            // 服務帳號的暫存資料夾不一定可以寫：一律用資料夾中的 temp
            ("TEMP".to_string(), path_text(&self.temp_dir())),
            ("TMP".to_string(), path_text(&self.temp_dir())),
        ];
        for (key, value) in &self.environment {
            env.retain(|(k, _)| !k.eq_ignore_ascii_case(key));
            env.push((key.clone(), value.clone()));
        }
        env
    }
}

fn path_text(path: &Path) -> String {
    path.to_string_lossy().into_owned()
}

fn substitute(value: &str, data_dir: &Path, install_dir: &Path) -> String {
    value
        .replace("{data}", &path_text(data_dir))
        .replace("{install}", &path_text(install_dir))
}

/// 程序的名稱也是記錄檔的檔名：只允許英文字母、數字、- 與 _。
fn valid_name(name: &str) -> bool {
    !name.is_empty()
        && name.len() <= 40
        && name
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || c == '-' || c == '_')
}

pub fn parse(text: &str, data_dir: &Path, install_dir: &Path) -> Result<Config, String> {
    let raw: RawConfig = toml::from_str(text).map_err(|e| format!("設定檔的格式不對：{e}"))?;
    let mut environment = Vec::new();
    for (key, value) in raw.environment {
        if key.is_empty() || key.contains('=') || key.contains('\0') {
            return Err(format!("環境變數的名稱不對：「{key}」"));
        }
        environment.push((key, substitute(&value, data_dir, install_dir)));
    }
    let mut seen: Vec<String> = Vec::new();
    let mut processes = Vec::new();
    for process in raw.process {
        if !valid_name(&process.name) {
            return Err(format!(
                "程序的名稱「{}」只能用英文字母、數字、- 與 _（也是記錄檔的檔名）",
                process.name
            ));
        }
        let lower = process.name.to_ascii_lowercase();
        if seen.contains(&lower) || lower == "host" {
            return Err(format!(
                "程序的名稱「{}」重複或保留給服務本身",
                process.name
            ));
        }
        seen.push(lower);
        if process.args.is_empty() {
            return Err(format!(
                "程序「{}」沒有 args（tmig 的子指令）",
                process.name
            ));
        }
        if process.enabled {
            processes.push(Process {
                name: process.name,
                args: process
                    .args
                    .iter()
                    .map(|a| substitute(a, data_dir, install_dir))
                    .collect(),
            });
        }
    }
    if processes.is_empty() {
        return Err("設定檔中沒有啟用的程序（[[process]]）".to_string());
    }
    Ok(Config {
        data_dir: data_dir.to_path_buf(),
        install_dir: install_dir.to_path_buf(),
        environment,
        processes,
    })
}

/// 記錄用的指令列：有空白的參數加上引號。
pub fn display_command(program: &Path, args: &[String]) -> String {
    let mut parts = vec![quote(&path_text(program))];
    parts.extend(args.iter().map(|a| quote(a)));
    parts.join(" ")
}

fn quote(text: &str) -> String {
    if text.is_empty() || text.contains(' ') || text.contains('\t') {
        format!("\"{text}\"")
    } else {
        text.to_string()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn dirs() -> (PathBuf, PathBuf) {
        (
            PathBuf::from(r"C:\ProgramData\TmigServer"),
            PathBuf::from(r"C:\Program Files\TmigServer"),
        )
    }

    #[test]
    fn substitutes_placeholders_and_skips_disabled() {
        let (data, install) = dirs();
        let text = r#"
            [environment]
            TMIG_CONNECTIONS = '{data}\connections.toml'

            [[process]]
            name = "server"
            args = ["serve", "--fixtures", '{data}\workbooks', "--web", '{install}\web\dist']

            [[process]]
            name = "scheduler"
            enabled = false
            args = ["schedule", "serve"]
        "#;
        let config = parse(text, &data, &install).unwrap();
        assert_eq!(config.processes.len(), 1);
        assert_eq!(
            config.processes[0].args,
            vec![
                "serve",
                "--fixtures",
                r"C:\ProgramData\TmigServer\workbooks",
                "--web",
                r"C:\Program Files\TmigServer\web\dist"
            ]
        );
        assert_eq!(
            config.environment,
            vec![(
                "TMIG_CONNECTIONS".to_string(),
                r"C:\ProgramData\TmigServer\connections.toml".to_string()
            )]
        );
        assert_eq!(
            config.arguments(&config.processes[0])[..5],
            ["-X", "utf8", "-s", "-m", "tmig.cli"]
        );
    }

    #[test]
    fn configured_environment_overrides_defaults() {
        let (data, install) = dirs();
        let text = "[environment]\ntmig_web_dist = 'D:\\viewer'\n[[process]]\nname = \"a\"\nargs = [\"serve\"]\n";
        let env = parse(text, &data, &install)
            .unwrap()
            .environment_for_children();
        let web: Vec<_> = env
            .iter()
            .filter(|(k, _)| k.eq_ignore_ascii_case("TMIG_WEB_DIST"))
            .collect();
        assert_eq!(
            web,
            vec![&("tmig_web_dist".to_string(), r"D:\viewer".to_string())]
        );
        assert!(env.iter().any(|(k, v)| k == "PLAYWRIGHT_BROWSERS_PATH"
            && v == r"C:\Program Files\TmigServer\ms-playwright"));
    }

    #[test]
    fn rejects_bad_configs() {
        let (data, install) = dirs();
        let cases = [
            ("[[process]]\nname = \"a b\"\nargs = [\"serve\"]\n", "只能用"),
            (
                "[[process]]\nname = \"a\"\nargs = [\"serve\"]\n[[process]]\nname = \"A\"\nargs = [\"serve\"]\n",
                "重複",
            ),
            ("[[process]]\nname = \"host\"\nargs = [\"serve\"]\n", "保留"),
            ("[[process]]\nname = \"a\"\nargs = []\n", "沒有 args"),
            ("[[process]]\nname = \"a\"\nenabled = false\nargs = [\"serve\"]\n", "沒有啟用"),
            ("[[process]]\nname = \"a\"\nargs = [\"serve\"]\ntypo = 1\n", "格式不對"),
            ("[environment]\n\"A=B\" = \"x\"\n[[process]]\nname = \"a\"\nargs = [\"serve\"]\n", "名稱不對"),
        ];
        for (text, expected) in cases {
            let error = parse(text, &data, &install).unwrap_err();
            assert!(error.contains(expected), "{text} → {error}");
        }
    }

    #[test]
    fn quotes_arguments_with_spaces() {
        let program = PathBuf::from(r"C:\Program Files\TmigServer\python\python.exe");
        assert_eq!(
            display_command(&program, &["serve".to_string(), "a b".to_string()]),
            r#""C:\Program Files\TmigServer\python\python.exe" serve "a b""#
        );
    }
}
