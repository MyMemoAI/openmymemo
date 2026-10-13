//! The Python core behind the window: find the panel server for the memory folder, or start one.
//!
//! The app bundles `uv` and the package source (see scripts/stage_desktop.py). Before it starts a server it
//! installs the package with uv into its own environment in Application Support, not editable, so the
//! environment keeps working when the app is moved or replaced (the launcher AI apps run uses it directly,
//! launcher.rs), then runs `openmymemo serve` from there, the same server the CLI and the AI apps' plugins
//! run. One server per folder: if a port already serves this folder (Claude Desktop's extension often got
//! there first), the app uses it, and starts its own when that one goes away. A port serving something
//! else is skipped.

use std::ffi::OsString;
use std::fs::{self, File, OpenOptions};
use std::io::{Read, Seek, SeekFrom};
use std::ops::Range;
use std::os::unix::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicI32, Ordering};
use std::time::{Duration, Instant};

use serde::Deserialize;

/// The ports the app looks at for this folder's server, and starts its own on: 8787, the panel's usual
/// port, and the next nine.
pub const PORTS: Range<u16> = 8787..8797;
/// The first launch downloads Python and the dependencies; give it time.
const START_TIMEOUT: Duration = Duration::from_secs(600);
/// How long an install may take before the setup page explains the wait (one that only checks takes ~10 ms).
const EXPLAIN_AFTER: Duration = Duration::from_millis(1500);

/// The uv arguments that pick the app's environment, for the app's install and the launcher's: the locked
/// versions, no development tools, and the package copied in rather than linked to the bundle.
pub const UV_ARGS: [&str; 4] = ["--frozen", "--no-dev", "--no-editable", "--quiet"];

/// The environment uv runs with, for the app's install and the launcher's: everything it installs stays in
/// the app's own folders, and Python is uv's own, never the system's.
pub fn uv_env(p: &Paths) -> Vec<(&'static str, OsString)> {
    vec![
        ("UV_PROJECT_ENVIRONMENT", p.venv().into()),
        ("UV_PYTHON_INSTALL_DIR", p.support.join("python").into()),
        ("UV_PYTHON_PREFERENCE", "only-managed".into()),
        ("UV_PYTHON", "3.12".into()),
        ("UV_CACHE_DIR", p.cache.clone().into()),
    ]
}

/// Why there is no server, for the setup page.
#[derive(Debug, Clone, PartialEq)]
pub struct Problem {
    pub title: String,
    pub detail: String,
    /// No internet connection during the install: the page offers Retry, not another folder.
    pub offline: bool,
}

impl Problem {
    fn new(title: &str, detail: impl Into<String>) -> Self {
        Problem { title: title.into(), detail: detail.into(), offline: false }
    }
}

impl From<String> for Problem {
    fn from(detail: String) -> Self {
        Problem::new("OpenMyMemo couldn't start", detail)
    }
}

/// What uv says when it can't reach the network (no connection, DNS, a proxy or firewall in the way).
pub fn is_network_error(output: &str) -> bool {
    let output = output.to_lowercase();
    [
        "error sending request",
        "dns error",
        "failed to lookup address",
        "network is unreachable",
        "connection refused",
        "tcp connect error",
        "connection reset",
        "operation timed out",
        "failed to fetch",
        "failed to download",
        "network connectivity is disabled",
    ]
    .iter()
    .any(|marker| output.contains(marker))
}

/// What `/api/state` says; only the parts the shell needs.
#[derive(Debug, Clone, Deserialize)]
pub struct PanelState {
    pub vault: String,
    pub counts: Counts,
}

#[derive(Debug, Clone, Default, PartialEq, Deserialize)]
pub struct Counts {
    pub pending: u64,
    pub unreviewed: u64,
}

#[derive(Debug, Clone, Deserialize)]
pub struct Proposal {
    pub id: String,
    pub connector_name: String,
    pub op: String,
    pub content: Option<String>,
    pub final_content: Option<String>,
}

pub enum Probe {
    /// Nothing listens on the port.
    Free,
    /// The port serves this memory folder.
    Ours(PanelState),
    /// Something else: another folder, another program, or a server still starting.
    Other,
}

fn agent() -> ureq::Agent {
    ureq::AgentBuilder::new()
        .timeout_connect(Duration::from_millis(500))
        .timeout(Duration::from_secs(5))
        .build()
}

/// GET a panel API path with the header the panel's guard asks for (Host is set from the URL).
pub fn api<T: serde::de::DeserializeOwned>(port: u16, path: &str) -> Option<T> {
    agent()
        .get(&format!("http://127.0.0.1:{port}{path}"))
        .set("X-OpenMyMemo", "1")
        .call()
        .ok()?
        .into_json()
        .ok()
}

fn same_folder(a: &Path, b: &Path) -> bool {
    match (fs::canonicalize(a), fs::canonicalize(b)) {
        (Ok(a), Ok(b)) => a == b,
        _ => a == b,
    }
}

pub fn probe(port: u16, vault: &Path) -> Probe {
    let addr = std::net::SocketAddr::from(([127, 0, 0, 1], port));
    if std::net::TcpStream::connect_timeout(&addr, Duration::from_millis(300)).is_err() {
        return Probe::Free;
    }
    match api::<PanelState>(port, "/api/state") {
        Some(state) if same_folder(Path::new(&state.vault), vault) => Probe::Ours(state),
        _ => Probe::Other,
    }
}

pub struct Paths {
    /// The bundled `uv` binary.
    pub uv: PathBuf,
    /// The bundled package source (pyproject.toml, uv.lock, src/).
    pub project: PathBuf,
    /// ~/Library/Application Support/<id>: the virtual environment and Python live here.
    pub support: PathBuf,
    /// ~/Library/Caches/<id>: uv's download cache.
    pub cache: PathBuf,
    /// ~/Library/Logs/<id>/core.log: the server's output.
    pub log: PathBuf,
    /// support/bin/openmymemo, the command AI apps run (launcher.rs); the server gets it as OPENMYMEMO_COMMAND.
    pub launcher: PathBuf,
}

impl Paths {
    pub fn venv(&self) -> PathBuf {
        self.support.join("venv")
    }
}

pub struct Core {
    pub vault: PathBuf,
    paths: Paths,
    ports: Range<u16>,
    child: Option<(Child, u16)>,
    /// The port that served this folder last, looked at first so the window's address stays put.
    last_port: Option<u16>,
    /// The environment was brought up to date with this app's package in this session.
    installed: bool,
}

/// The process group of the server this app started, so the exit handler can stop it.
static CHILD_PGID: AtomicI32 = AtomicI32::new(0);

/// Stop the server this app started, if any (it runs in its own process group: uv and Python).
pub fn stop_child() {
    let pgid = CHILD_PGID.swap(0, Ordering::SeqCst);
    if pgid > 0 {
        unsafe {
            libc::kill(-pgid, libc::SIGTERM);
        }
    }
}

impl Core {
    pub fn new(vault: PathBuf, paths: Paths, ports: Range<u16>) -> Self {
        Core { vault, paths, ports, child: None, last_port: None, installed: false }
    }

    /// The memory folder changed: forget the old folder's server.
    pub fn set_vault(&mut self, vault: PathBuf) {
        self.stop();
        self.vault = vault;
        self.last_port = None;
    }

    /// The port that serves this folder, starting a server if none does. `status` reports progress.
    ///
    /// One server per folder: every candidate port is asked before a server is started, so a server for
    /// this folder on a higher port is found even when a lower one is free.
    pub fn ensure(&mut self, status: &dyn Fn(&str, &str)) -> Result<(u16, PanelState), Problem> {
        let found = match self.child_port() {
            Some(port) => self.wait_ready(port, status),
            None => self.find_or_start(status),
        };
        if let Ok((port, _)) = &found {
            self.last_port = Some(*port);
        }
        found
    }

    fn find_or_start(&mut self, status: &dyn Fn(&str, &str)) -> Result<(u16, PanelState), Problem> {
        let last = self.last_port.filter(|p| self.ports.contains(p));
        let mut free = None;
        for port in last.into_iter().chain(self.ports.clone().filter(|p| Some(*p) != last)) {
            match probe(port, &self.vault) {
                Probe::Ours(state) => return Ok((port, state)),
                Probe::Free => free = free.or(Some(port)),
                Probe::Other => {}
            }
        }
        let Some(port) = free else {
            return Err(Problem::from(format!(
                "Ports {}–{} are all in use by other programs.",
                self.ports.start,
                self.ports.end - 1
            )));
        };
        if !self.installed {
            self.install(status)?;
            self.installed = true;
            // The first install can take a minute: look again, another app may have started a server since.
            return self.find_or_start(status);
        }
        self.spawn(port)?;
        self.wait_ready(port, status)
    }

    /// The port of the server we started, if it is still running.
    fn child_port(&mut self) -> Option<u16> {
        let (child, port) = self.child.as_mut()?;
        if matches!(child.try_wait(), Ok(None)) {
            return Some(*port);
        }
        self.child = None;
        CHILD_PGID.store(0, Ordering::SeqCst);
        None
    }

    fn prepare_dirs(&self) -> Result<File, String> {
        let p = &self.paths;
        for dir in [&p.support, &p.cache, p.log.parent().unwrap_or(Path::new("/tmp"))] {
            fs::create_dir_all(dir).map_err(|e| format!("Can't create {}: {e}", dir.display()))?;
        }
        OpenOptions::new().create(true).append(true).open(&p.log).map_err(|e| e.to_string())
    }

    /// Bring the environment up to date with the bundled package: `uv sync`, which only checks when nothing
    /// changed, and otherwise downloads Python and the dependencies (the first start) or reinstalls the
    /// package (a new version, or the app moved). After a move without a network, uv can't look up the
    /// package's build tool, so it tries once more from its cache alone.
    fn install(&mut self, status: &dyn Fn(&str, &str)) -> Result<(), Problem> {
        let p = &self.paths;
        let log = self.prepare_dirs()?;
        let started = Instant::now();
        let mut offline = false;
        loop {
            let from = log.metadata().map(|m| m.len()).unwrap_or(0);
            let mut cmd = Command::new(&p.uv);
            cmd.args(["sync", "--project"]).arg(&p.project).args(UV_ARGS);
            if offline {
                cmd.arg("--offline");
            }
            let mut child = cmd
                .envs(uv_env(p))
                .env_remove("VIRTUAL_ENV")
                .stdin(Stdio::null())
                .stdout(log.try_clone().map_err(|e| e.to_string())?)
                .stderr(log.try_clone().map_err(|e| e.to_string())?)
                .process_group(0)
                .spawn()
                .map_err(|e| format!("Can't start {}: {e}", p.uv.display()))?;
            CHILD_PGID.store(child.id() as i32, Ordering::SeqCst);
            let done = loop {
                match child.try_wait() {
                    Ok(Some(code)) => break code,
                    Ok(None) => {}
                    Err(e) => {
                        stop_child();
                        let _ = child.kill();
                        return Err(Problem::new("OpenMyMemo couldn't set up", e.to_string()));
                    }
                }
                if started.elapsed() > START_TIMEOUT {
                    stop_child();
                    let _ = child.wait();
                    return Err(Problem::new("OpenMyMemo couldn't set up", "The download took too long. Check your internet connection and try again."));
                }
                // Said on every tick, so a setup page that loads late still shows it.
                if started.elapsed() > EXPLAIN_AFTER {
                    let (title, detail) = setup_message(p);
                    status(title, detail);
                }
                std::thread::sleep(Duration::from_millis(250));
            };
            CHILD_PGID.store(0, Ordering::SeqCst);
            if done.success() {
                return Ok(());
            }
            let output = read_from(&p.log, from);
            if is_network_error(&output) {
                if !offline && p.venv().join("bin/python").exists() {
                    offline = true;
                    continue;
                }
                return Err(Problem {
                    title: "No internet connection".into(),
                    detail: "The first start downloads Python and the libraries OpenMyMemo runs on (about 25 MB). Connect to the internet, then click Retry.".into(),
                    offline: true,
                });
            }
            return Err(Problem::new("OpenMyMemo couldn't set up", self.log_tail()));
        }
    }

    fn spawn(&mut self, port: u16) -> Result<(), String> {
        let p = &self.paths;
        let log = self.prepare_dirs()?;
        let child = Command::new(p.venv().join("bin/openmymemo"))
            .arg("--vault")
            .arg(&self.vault)
            // --init: a first launch creates the vault in a missing or empty folder (and refuses any other).
            .args(["serve", "--init", "--no-open", "--port", &port.to_string()])
            // The command the panel writes into AI apps' MCP configs (snippets.command_for).
            .env("OPENMYMEMO_COMMAND", &p.launcher)
            .env("PYTHONUNBUFFERED", "1")
            .env_remove("PYTHONHOME")
            .env_remove("PYTHONPATH")
            .env_remove("VIRTUAL_ENV")
            .stdin(Stdio::null())
            .stdout(log.try_clone().map_err(|e| e.to_string())?)
            .stderr(log)
            .process_group(0)
            .spawn()
            .map_err(|e| format!("Can't start the local server: {e}"))?;
        CHILD_PGID.store(child.id() as i32, Ordering::SeqCst);
        self.child = Some((child, port));
        Ok(())
    }

    fn wait_ready(&mut self, port: u16, status: &dyn Fn(&str, &str)) -> Result<(u16, PanelState), Problem> {
        let started = Instant::now();
        loop {
            match probe(port, &self.vault) {
                Probe::Ours(state) => return Ok((port, state)),
                Probe::Other | Probe::Free => {}
            }
            if self.child_port().is_none() {
                return Err(format!("The local server stopped. {}", self.log_tail()).into());
            }
            if started.elapsed() > Duration::from_secs(4) {
                status("Starting…", "Opening your memory folder.");
            }
            if started.elapsed() > Duration::from_secs(60) {
                self.stop();
                return Err(format!("The local server didn't start. {}", self.log_tail()).into());
            }
            std::thread::sleep(Duration::from_millis(300));
        }
    }

    pub fn stop(&mut self) {
        stop_child();
        if let Some((mut child, _)) = self.child.take() {
            let deadline = Instant::now() + Duration::from_secs(3);
            while Instant::now() < deadline {
                if !matches!(child.try_wait(), Ok(None)) {
                    return;
                }
                std::thread::sleep(Duration::from_millis(100));
            }
            let _ = child.kill();
        }
    }

    /// The last line of the server's log, for error messages.
    fn log_tail(&self) -> String {
        let Ok(mut f) = File::open(&self.paths.log) else { return String::new() };
        let len = f.metadata().map(|m| m.len()).unwrap_or(0);
        let _ = f.seek(SeekFrom::Start(len.saturating_sub(2000)));
        let mut text = String::new();
        let _ = f.read_to_string(&mut text);
        let last = text.lines().rev().find(|l| !l.trim().is_empty()).unwrap_or("").trim();
        if last.is_empty() {
            format!("Details: {}", self.paths.log.display())
        } else {
            format!("{last} (details: {})", self.paths.log.display())
        }
    }
}

/// The log written since `from`, at most its last 8 KB.
fn read_from(log: &Path, from: u64) -> String {
    let Ok(mut f) = File::open(log) else { return String::new() };
    let len = f.metadata().map(|m| m.len()).unwrap_or(0);
    let _ = f.seek(SeekFrom::Start(from.max(len.saturating_sub(8192))));
    let mut text = String::new();
    let _ = f.read_to_string(&mut text);
    text
}

/// What the setup page says while uv works: a first start downloads Python and then the libraries; with
/// Python and the package already there, it is an update (a new version, or the app moved).
fn setup_message(p: &Paths) -> (&'static str, &'static str) {
    const FIRST: &str = "The first start downloads Python and the libraries OpenMyMemo runs on (about 25 MB) into your Library folder. It usually takes under a minute and happens only once.";
    if p.venv().join("bin/openmymemo").exists() {
        return ("Updating OpenMyMemo…", "Getting this version ready. It takes a few seconds.");
    }
    let has_python = fs::read_dir(p.support.join("python"))
        .map(|entries| entries.flatten().any(|e| e.file_name().to_string_lossy().starts_with("cpython-3.12")))
        .unwrap_or(false);
    if has_python {
        ("Setting up OpenMyMemo (2 of 2)…", FIRST)
    } else {
        ("Downloading Python (1 of 2)…", FIRST)
    }
}

/// Whether `path` can be the memory folder: an OpenMyMemo vault, or a folder that is missing or holds only
/// hidden entries (the server creates the vault there). The same rule as `Vault.open_or_create`.
pub fn check_folder(path: &Path) -> Result<(), String> {
    if path.join(".openmymemo/topics.yaml").is_file() && path.join("memories").is_dir() {
        return Ok(());
    }
    if !path.exists() {
        return Ok(());
    }
    let entries = fs::read_dir(path).map_err(|e| format!("Can't open {}: {e}", path.display()))?;
    for entry in entries {
        let entry = entry.map_err(|e| e.to_string())?;
        if !entry.file_name().to_string_lossy().starts_with('.') {
            return Err(format!(
                "{} isn't empty and isn't an OpenMyMemo memory folder. Choose an empty folder or an existing memory folder.",
                path.display()
            ));
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;
    use std::net::TcpListener;
    use std::sync::Mutex;

    fn temp_dir(name: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!("openmymemo-desktop-{}-{name}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        dir
    }

    /// A stand-in panel server that says it serves `vault`.
    fn fake_server(vault: &Path) -> u16 {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let body = serde_json::json!({ "vault": vault, "counts": { "pending": 0, "unreviewed": 0 } }).to_string();
        std::thread::spawn(move || {
            for stream in listener.incoming().flatten() {
                let mut stream = stream;
                let mut buf = [0u8; 2048];
                let _ = stream.set_read_timeout(Some(Duration::from_millis(500)));
                if stream.read(&mut buf).unwrap_or(0) == 0 {
                    continue; // a probe's bare connect
                }
                let _ = write!(
                    stream,
                    "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
                    body.len()
                );
            }
        });
        port
    }

    fn core(vault: &Path, ports: Range<u16>) -> Core {
        let none = PathBuf::from("/nonexistent/openmymemo-test");
        let paths = Paths {
            uv: none.join("uv"),
            project: none.clone(),
            support: none.clone(),
            cache: none.clone(),
            log: none.join("core.log"),
            launcher: none.join("bin/openmymemo"),
        };
        Core::new(vault.to_path_buf(), paths, ports)
    }

    #[test]
    fn finds_this_folders_server_above_a_free_port_instead_of_starting_one() {
        let vault = temp_dir("found");
        let port = fake_server(&vault);
        let ports = port - 4..port + 1;
        assert!(ports.clone().any(|p| p < port && matches!(probe(p, &vault), Probe::Free)), "needs a free lower port");
        // Starting a server would fail here (no uv), so Ok means nothing was started.
        let (found, _) = core(&vault, ports).ensure(&|_, _| {}).expect("the existing server");
        assert_eq!(found, port);
    }

    #[test]
    fn skips_a_server_for_another_folder() {
        let ours = temp_dir("ours");
        let other = temp_dir("other");
        let theirs = fake_server(&other);
        assert!(matches!(probe(theirs, &ours), Probe::Other));
        let err = core(&ours, theirs..theirs + 1).ensure(&|_, _| {}).unwrap_err();
        assert!(err.detail.contains("in use by other programs"), "{err:?}");
    }

    /// Paths in a temporary folder with a stand-in uv that runs `body` (its arguments are appended to calls.log).
    fn fake_install(name: &str, body: &str) -> (Core, PathBuf) {
        use std::os::unix::fs::PermissionsExt;
        let root = temp_dir(name);
        let uv = root.join("uv");
        fs::write(&uv, format!("#!/bin/sh\necho \"$*\" >> '{}/calls.log'\n{body}\n", root.display())).unwrap();
        fs::set_permissions(&uv, fs::Permissions::from_mode(0o755)).unwrap();
        let paths = Paths {
            uv,
            project: root.join("core"),
            support: root.join("support"),
            cache: root.join("cache"),
            log: root.join("logs/core.log"),
            launcher: root.join("support/bin/openmymemo"),
        };
        (Core::new(root.join("vault"), paths, 0..0), root)
    }

    const OFFLINE: &str = "error: Failed to fetch: `https://pypi.org/simple/hatchling/`\n  Caused by: error sending request for url (https://pypi.org/simple/hatchling/)\n  Caused by: dns error: failed to lookup address information: nodename nor servname provided, or not known";

    #[test]
    fn recognises_uvs_network_errors() {
        assert!(is_network_error(OFFLINE));
        assert!(is_network_error("tcp connect error\n╰─▶ Connection refused (os error 61)"));
        assert!(is_network_error("error: Failed to download `cpython-3.12.13-macos-aarch64-none`"));
        assert!(!is_network_error("error: No `project` table found in: `/x/pyproject.toml`"));
    }

    #[test]
    fn a_first_start_without_a_network_asks_for_one() {
        let (mut core, root) = fake_install("offline", &format!("echo '{OFFLINE}' >&2; exit 2"));
        let said = Mutex::new(Vec::new());
        let err = core.install(&|title, _| said.lock().unwrap().push(title.to_string())).unwrap_err();
        assert!(err.offline && err.title == "No internet connection", "{err:?}");
        // No Python yet, so no second try from the cache.
        let calls = fs::read_to_string(root.join("calls.log")).unwrap();
        assert_eq!(calls.lines().count(), 1, "{calls}");
        assert!(calls.contains("sync --project") && calls.contains("--no-editable"), "{calls}");
        assert!(said.lock().unwrap().is_empty(), "a quick failure needs no explanation");
    }

    #[test]
    fn after_a_move_without_a_network_it_installs_from_the_cache() {
        let (mut core, root) = fake_install(
            "moved",
            &format!(r#"case "$*" in *--offline*) exit 0 ;; esac; echo '{OFFLINE}' >&2; exit 2"#),
        );
        fs::create_dir_all(root.join("support/venv/bin")).unwrap();
        fs::write(root.join("support/venv/bin/python"), "").unwrap();
        core.install(&|_, _| {}).expect("installed from the cache");
        let calls = fs::read_to_string(root.join("calls.log")).unwrap();
        assert_eq!(calls.lines().count(), 2, "{calls}");
        assert!(calls.lines().nth(1).unwrap().ends_with("--offline"), "{calls}");
    }

    #[test]
    fn a_long_install_is_explained_and_other_failures_show_the_log() {
        let (mut core, _) = fake_install("slow", "sleep 2; echo 'error: something else broke' >&2; exit 1");
        let said = Mutex::new(Vec::new());
        let err = core.install(&|title, _| said.lock().unwrap().push(title.to_string())).unwrap_err();
        assert!(!err.offline && err.detail.contains("something else broke"), "{err:?}");
        assert_eq!(said.lock().unwrap().first().map(String::as_str), Some("Downloading Python (1 of 2)…"));
    }

    #[test]
    fn accepts_a_vault_or_a_missing_or_empty_folder_and_refuses_anything_else() {
        let root = temp_dir("folders");
        assert!(check_folder(&root.join("absent")).is_ok());
        let empty = root.join("empty");
        fs::create_dir_all(&empty).unwrap();
        fs::write(empty.join(".DS_Store"), "").unwrap();
        assert!(check_folder(&empty).is_ok());
        let vault = root.join("vault");
        fs::create_dir_all(vault.join(".openmymemo")).unwrap();
        fs::create_dir_all(vault.join("memories")).unwrap();
        fs::write(vault.join(".openmymemo/topics.yaml"), "").unwrap();
        assert!(check_folder(&vault).is_ok());
        let busy = root.join("busy");
        fs::create_dir_all(&busy).unwrap();
        fs::write(busy.join("notes.txt"), "mine").unwrap();
        assert!(check_folder(&busy).unwrap_err().contains("isn't empty"));
    }
}

