//! The Python core behind the window: find the panel server for the memory folder, or start one.
//!
//! The app bundles `uv` and the package source (see scripts/stage_desktop.py). On every start, before it
//! looks for a server, it makes sure its build of the package is installed, not editable, in an environment
//! of its own under Application Support: `venvs/<version>-<stamp>`, one per build, written by `uv sync` and
//! marked ready (`.openmymemo-ready`) last, then made current by pointing the `venv` link at it. A running
//! server keeps the environment it started from (its paths are the real folder's), so an update never
//! changes the files under a running AI app; it gets the new version when that app restarts. The launcher
//! AI apps run (launcher.rs) runs the current environment, which keeps working when the app moves.
//!
//! The server is `openmymemo serve`, the same one the CLI and the AI apps' plugins run. One server per
//! folder: if a port already serves this folder (Claude Desktop's extension often got there first) from a
//! core at least as new as the app, the app uses it, and starts its own when that one goes away. A port
//! serving something else is skipped, and so is another app's older core serving this folder: its panel
//! may predate the Mac layout, which the window needs to be dragged and to keep clear of its buttons. The
//! app's own server is used whatever its version, since it runs from one of the app's own environments
//! (an older one when an update couldn't be installed), and every one of those has the Mac layout.

use std::ffi::OsString;
use std::fs::{self, File, OpenOptions};
use std::io::{Read, Seek, SeekFrom};
use std::ops::Range;
#[cfg(unix)]
use std::os::fd::AsRawFd;
#[cfg(unix)]
use std::os::unix::process::CommandExt;
#[cfg(windows)]
use std::os::windows::fs::OpenOptionsExt;
#[cfg(windows)]
use std::os::windows::process::CommandExt;
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
/// the app's own folders, and Python is uv's own, never the system's. UV_PROJECT_ENVIRONMENT, the build's
/// own folder under venvs/, is added by each caller.
pub fn uv_env(p: &Paths) -> Vec<(&'static str, OsString)> {
    vec![
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
    /// The core's version. Every release sends it; a server without one counts as too old.
    pub version: Option<String>,
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

/// The app's version, which its bundled core shares (tests/test_packaging.py keeps them in sync).
const VERSION: &str = env!("CARGO_PKG_VERSION");

/// `[major, minor, patch]` of a version such as "0.2.5" or "0.2.6.dev0"; None if it doesn't start with a number.
fn release(version: &str) -> Option<[u64; 3]> {
    let mut out = [0; 3];
    for (i, part) in version.split('.').take(3).enumerate() {
        let digits: String = part.chars().take_while(char::is_ascii_digit).collect();
        match digits.parse() {
            Ok(n) => out[i] = n,
            Err(_) if i > 0 => break,
            Err(_) => return None,
        }
        if digits.len() < part.len() {
            break; // "6rc1": the rest is a pre-release
        }
    }
    Some(out)
}

/// Whether the app can use a server another app started for its folder: its core is at least as new as the app,
/// so the panel has the Mac layout (window dragging, room for the window's buttons) and the shell commands this
/// build expects. An AI app's plugin that hasn't been updated may serve a panel from before the Mac layout.
fn new_enough(version: Option<&str>) -> bool {
    matches!((version.and_then(release), release(VERSION)), (Some(theirs), Some(ours)) if theirs >= ours)
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

/// POST JSON to a panel API path; true when the server accepted it.
pub fn post(port: u16, path: &str, body: serde_json::Value) -> bool {
    agent()
        .post(&format!("http://127.0.0.1:{port}{path}"))
        .set("X-OpenMyMemo", "1")
        .send_json(body)
        .is_ok()
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
    /// ~/Library/Application Support/<id> (on Windows %LOCALAPPDATA%\<id>): the environments and Python live here.
    pub support: PathBuf,
    /// ~/Library/Caches/<id> (on Windows support\cache): uv's download cache.
    pub cache: PathBuf,
    /// ~/Library/Logs/<id>/core.log: the server's output.
    pub log: PathBuf,
    /// support/bin/openmymemo (openmymemo.exe on Windows), the command AI apps run (launcher.rs, winlauncher.rs);
    /// the server gets it as OPENMYMEMO_COMMAND.
    pub launcher: PathBuf,
}

/// Written last into an environment that uv finished, holding the package version.
pub const READY: &str = ".openmymemo-ready";

/// On Windows, the name of the current environment under venvs/: a file instead of the `venv` link, because
/// making a symbolic link there takes Developer Mode or an administrator.
#[cfg(windows)]
pub const CURRENT: &str = "current-env";

/// A program in an environment: bin/<name> on macOS, Scripts\<name>.exe on Windows.
pub fn env_bin(dir: &Path, name: &str) -> PathBuf {
    if cfg!(windows) {
        dir.join("Scripts").join(format!("{name}.exe"))
    } else {
        dir.join("bin").join(name)
    }
}

impl Paths {
    /// The bundled build's own environment folder: its version and pyproject.toml's modification time,
    /// which stage_desktop.py sets when it stages a build (the launcher computes the same name).
    pub fn build_env(&self) -> Result<(String, PathBuf), String> {
        let pyproject = self.project.join("pyproject.toml");
        let text = fs::read_to_string(&pyproject).map_err(|e| format!("Can't read {}: {e}", pyproject.display()))?;
        let version = text
            .lines()
            .find_map(|l| l.strip_prefix("version = \"").and_then(|v| v.split('"').next()))
            .unwrap_or("0")
            .to_string();
        let stamp = fs::metadata(&pyproject)
            .and_then(|m| m.modified())
            .map(|t| t.duration_since(std::time::UNIX_EPOCH).map(|d| d.as_secs()).unwrap_or(0))
            .map_err(|e| e.to_string())?;
        Ok((version.clone(), self.support.join("venvs").join(format!("{version}-{stamp}"))))
    }

    /// The current environment, if it is complete.
    pub fn current(&self) -> Option<PathBuf> {
        let dir = current_dir(&self.support)?;
        is_ready(&dir).then_some(dir)
    }
}

/// An environment uv finished: the marker is written after the install succeeded.
pub fn is_ready(dir: &Path) -> bool {
    dir.join(READY).is_file() && env_bin(dir, "python").exists() && env_bin(dir, "openmymemo").is_file()
}

/// The folder the current environment's link points to.
#[cfg(unix)]
fn current_dir(support: &Path) -> Option<PathBuf> {
    fs::canonicalize(support.join("venv")).ok()
}

/// The folder named in `current-env`, only ever a folder directly under venvs/.
#[cfg(windows)]
fn current_dir(support: &Path) -> Option<PathBuf> {
    let name = fs::read_to_string(support.join(CURRENT)).ok()?;
    let name = name.trim();
    if name.is_empty() || name.contains(['/', '\\', ':']) || name.starts_with('.') {
        return None;
    }
    fs::canonicalize(support.join("venvs").join(name)).ok()
}

/// Make `dir` (under support/venvs) the current environment: a new link renamed over `venv`, so the
/// launcher sees the old one or the new one. Returns the environment that was current. A `venv` folder
/// from before environments were per build is moved into venvs/ first.
#[cfg(unix)]
fn make_current(support: &Path, dir: &Path) -> std::io::Result<Option<PathBuf>> {
    let link = support.join("venv");
    let previous = match fs::symlink_metadata(&link) {
        Ok(m) if m.file_type().is_symlink() => fs::canonicalize(&link).ok(),
        Ok(m) if m.is_dir() => {
            let secs = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_secs()).unwrap_or(0);
            let legacy = support.join("venvs").join(format!("legacy-{secs}"));
            fs::rename(&link, &legacy)?;
            Some(legacy)
        }
        Ok(_) => {
            fs::remove_file(&link)?;
            None
        }
        Err(_) => None,
    };
    let name = dir.file_name().ok_or_else(|| std::io::Error::other("no environment name"))?;
    let tmp = support.join(format!(".venv.{}.tmp", std::process::id()));
    let _ = fs::remove_file(&tmp);
    std::os::unix::fs::symlink(Path::new("venvs").join(name), &tmp)?;
    fs::rename(&tmp, &link)?;
    Ok(previous)
}

/// Make `dir` current on Windows: its name written to a temporary file renamed over `current-env`.
#[cfg(windows)]
fn make_current(support: &Path, dir: &Path) -> std::io::Result<Option<PathBuf>> {
    let previous = current_dir(support);
    let name = dir.file_name().and_then(|n| n.to_str()).ok_or_else(|| std::io::Error::other("no environment name"))?;
    let tmp = support.join(format!(".{CURRENT}.{}.tmp", std::process::id()));
    fs::write(&tmp, format!("{name}\n"))?;
    fs::rename(&tmp, support.join(CURRENT))?;
    Ok(previous)
}

/// The file every `openmymemo` process started from an environment under venvs/ holds a shared lock on
/// (`flock`, cli.py's `_hold_environment`) for as long as it runs; the lock goes away with the process.
/// On Windows the process keeps the file open instead.
pub const IN_USE: &str = ".openmymemo-in-use";

/// Whether no process runs from `dir`. If so, the returned file (when there is one) holds the lock
/// exclusively, so a process starting meanwhile doesn't take it, until it is dropped.
#[cfg(unix)]
fn unused(dir: &Path) -> Option<Option<File>> {
    let Ok(file) = File::open(dir.join(IN_USE)) else { return Some(None) };
    let free = unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) } == 0;
    free.then_some(Some(file))
}

/// On Windows: the file opens without sharing reads or writes only while no process has it open, and then
/// keeps others from opening it until it is dropped. Deleting is shared, so the folder can be removed with it.
#[cfg(windows)]
fn unused(dir: &Path) -> Option<Option<File>> {
    const FILE_SHARE_DELETE: u32 = 0x4;
    match OpenOptions::new().read(true).share_mode(FILE_SHARE_DELETE).open(dir.join(IN_USE)) {
        Ok(file) => Some(Some(file)),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Some(None),
        Err(_) => None,
    }
}

/// Windows: no console window for a program the app starts, and its own process group.
#[cfg(windows)]
const CREATE_NO_WINDOW: u32 = 0x0800_0000;
#[cfg(windows)]
const CREATE_NEW_PROCESS_GROUP: u32 = 0x0000_0200;

/// Start `cmd` in its own process group, so stop_child can stop it with what it starts (uv and Python).
fn own_group(cmd: &mut Command) -> &mut Command {
    #[cfg(unix)]
    return cmd.process_group(0);
    #[cfg(windows)]
    return cmd.creation_flags(CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP);
}

/// Remove environments other than `keep` (the current one and the one before it) that no process runs
/// from: a server an AI app kept alive across updates still reads its files (the panel's, for one). One
/// that isn't ready is left for a day, in case a launcher is installing into it right now.
fn collect_garbage(venvs: &Path, keep: &[&Path]) {
    let Ok(entries) = fs::read_dir(venvs) else { return };
    let day_ago = std::time::SystemTime::now() - Duration::from_secs(24 * 3600);
    for entry in entries.flatten() {
        // Only real folders the app made: a symlink placed here is skipped, so its target is never deleted.
        if !entry.file_type().map_or(false, |t| t.is_dir()) {
            continue;
        }
        let dir = entry.path();
        if keep.iter().any(|k| fs::canonicalize(k).map_or(false, |k| fs::canonicalize(&dir).map_or(false, |d| d == k))) {
            continue;
        }
        let old = entry.metadata().and_then(|m| m.modified()).map_or(false, |t| t < day_ago);
        if !(is_ready(&dir) || old) {
            continue;
        }
        if let Some(_lock) = unused(&dir) {
            let _ = fs::remove_dir_all(&dir);
        }
    }
}

pub struct Core {
    pub vault: PathBuf,
    paths: Paths,
    ports: Range<u16>,
    child: Option<(Child, u16)>,
    /// The port that served this folder last, looked at first so the window's address stays put.
    last_port: Option<u16>,
    /// The environment the app starts its server from, once prepared.
    ready: Option<PathBuf>,
    /// Whether this session has tried to prepare its build's environment.
    prepared: bool,
}

/// The process group of the server this app started, so the exit handler can stop it.
static CHILD_PGID: AtomicI32 = AtomicI32::new(0);

/// Stop the server this app started, if any (it runs in its own process group: uv and Python).
pub fn stop_child() {
    let pgid = CHILD_PGID.swap(0, Ordering::SeqCst);
    if pgid > 0 {
        #[cfg(unix)]
        unsafe {
            libc::kill(-pgid, libc::SIGTERM);
        }
        // Windows has no signal for another process group: end the process and everything it started.
        #[cfg(windows)]
        {
            let system = std::env::var_os("SystemRoot").map(PathBuf::from).unwrap_or_else(|| PathBuf::from(r"C:\Windows"));
            let _ = Command::new(system.join(r"System32\taskkill.exe"))
                .args(["/T", "/F", "/PID", &pgid.to_string()])
                .stdin(Stdio::null())
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .creation_flags(CREATE_NO_WINDOW)
                .status();
        }
    }
}

impl Core {
    pub fn new(vault: PathBuf, paths: Paths, ports: Range<u16>) -> Self {
        Core { vault, paths, ports, child: None, last_port: None, ready: None, prepared: false }
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
        // Once per start, before looking for a server: another app's server for this folder doesn't
        // update this app's environment. If it fails, a server that is already running still opens.
        if !self.prepared {
            self.prepared = true;
            let _ = self.prepare(status);
        }
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
                Probe::Ours(state) if new_enough(state.version.as_deref()) => return Ok((port, state)),
                Probe::Free => free = free.or(Some(port)),
                // An older core serving this folder: the app starts its own beside it.
                Probe::Ours(_) | Probe::Other => {}
            }
        }
        let Some(port) = free else {
            return Err(Problem::from(format!(
                "Ports {}–{} are all in use by other programs.",
                self.ports.start,
                self.ports.end - 1
            )));
        };
        if self.ready.is_none() {
            self.prepare(status)?;
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

    /// Make this build's environment the current one, installing it if it isn't ready (the first start, a
    /// new version or build). Old environments nothing runs from are removed, keeping the one before. If
    /// the install fails (no network, say) but a complete environment exists, the server starts from that one.
    fn prepare(&mut self, status: &dyn Fn(&str, &str)) -> Result<(), Problem> {
        let (version, dir) = self.paths.build_env()?;
        if !is_ready(&dir) {
            if let Err(problem) = self.install(&dir, status) {
                let Some(current) = self.paths.current() else { return Err(problem) };
                eprintln!("OpenMyMemo: {}: {}; starting from {}", problem.title, problem.detail, current.display());
                self.ready = Some(current);
                return Ok(());
            }
            let marker = dir.join(format!("{READY}.tmp"));
            fs::write(&marker, format!("{version}\n"))
                .and_then(|_| fs::rename(&marker, dir.join(READY)))
                .map_err(|e| format!("Can't mark {} ready: {e}", dir.display()))?;
        }
        if self.paths.current().as_deref() != fs::canonicalize(&dir).ok().as_deref() {
            let previous = make_current(&self.paths.support, &dir).map_err(|e| format!("Can't switch to {}: {e}", dir.display()))?;
            collect_garbage(&self.paths.support.join("venvs"), &[&dir, previous.as_deref().unwrap_or(&dir)]);
        }
        self.ready = Some(dir);
        Ok(())
    }

    /// Install the bundled package into `dir` with `uv sync`: the first start downloads Python and the
    /// dependencies; a new build mostly links them from uv's cache. Without a network, when Python is
    /// already there, it tries once more from the cache alone.
    fn install(&mut self, dir: &Path, status: &dyn Fn(&str, &str)) -> Result<(), Problem> {
        let p = &self.paths;
        let log = self.prepare_dirs()?;
        fs::create_dir_all(p.support.join("venvs")).map_err(|e| e.to_string())?;
        let started = Instant::now();
        let mut offline = false;
        loop {
            let from = log.metadata().map(|m| m.len()).unwrap_or(0);
            let mut cmd = Command::new(&p.uv);
            cmd.args(["sync", "--project"]).arg(&p.project).args(UV_ARGS);
            if offline {
                cmd.arg("--offline");
            }
            let mut child = own_group(&mut cmd)
                .envs(uv_env(p))
                .env("UV_PROJECT_ENVIRONMENT", dir)
                .env_remove("VIRTUAL_ENV")
                .stdin(Stdio::null())
                .stdout(log.try_clone().map_err(|e| e.to_string())?)
                .stderr(log.try_clone().map_err(|e| e.to_string())?)
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
                if !offline && has_python(p) {
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
        let env = self.ready.as_ref().ok_or("OpenMyMemo isn't installed yet.")?;
        // From the environment's real folder, so the server keeps its version when `venv` moves on.
        let mut cmd = Command::new(env_bin(env, "openmymemo"));
        let child = own_group(&mut cmd)
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
            .spawn()
            .map_err(|e| format!("Can't start the local server: {e}"))?;
        CHILD_PGID.store(child.id() as i32, Ordering::SeqCst);
        self.child = Some((child, port));
        Ok(())
    }

    /// Wait for the server the app started. Whatever its version: it runs the environment `prepare` chose.
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
fn has_python(p: &Paths) -> bool {
    fs::read_dir(p.support.join("python"))
        .map(|entries| entries.flatten().any(|e| e.file_name().to_string_lossy().starts_with("cpython-3.12")))
        .unwrap_or(false)
}

fn setup_message(p: &Paths) -> (&'static str, &'static str) {
    #[cfg(not(windows))]
    const FIRST: &str = "The first start downloads Python and the libraries OpenMyMemo runs on (about 25 MB) into your Library folder. It usually takes under a minute and happens only once.";
    #[cfg(windows)]
    const FIRST: &str = "The first start downloads Python and the libraries OpenMyMemo runs on (about 25 MB) into your AppData folder. It usually takes under a minute and happens only once.";
    if p.current().is_some() {
        return ("Updating OpenMyMemo…", "Getting this version ready. It takes a few seconds.");
    }
    if has_python(p) {
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

// The tests start stand-ins for uv and the server written as shell scripts.
#[cfg(all(test, unix))]
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

    /// A stand-in panel server that says it serves `vault` from a core at `version`.
    fn fake_server(vault: &Path, version: &str) -> u16 {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let body = serde_json::json!({ "vault": vault, "version": version, "counts": { "pending": 0, "unreviewed": 0 } })
            .to_string();
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
        let port = fake_server(&vault, VERSION);
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
        let theirs = fake_server(&other, VERSION);
        assert!(matches!(probe(theirs, &ours), Probe::Other));
        let err = core(&ours, theirs..theirs + 1).ensure(&|_, _| {}).unwrap_err();
        assert!(err.detail.contains("in use by other programs"), "{err:?}");
    }

    #[test]
    fn skips_an_older_core_serving_this_folder() {
        // An AI app's plugin from before the Mac layout: its panel can't be dragged and sits under the window's buttons.
        let vault = temp_dir("older");
        let older = fake_server(&vault, "0.2.2");
        // Starting a server would fail here (no uv), and there is no other port: Err means it wasn't used.
        let err = core(&vault, older..older + 1).ensure(&|_, _| {}).unwrap_err();
        assert!(err.detail.contains("in use by other programs"), "{err:?}");
        // A newer one is used.
        let newer = fake_server(&vault, "99.0.0.dev0");
        assert_eq!(core(&vault, newer..newer + 1).ensure(&|_, _| {}).expect("the newer server").0, newer);
    }

    #[test]
    fn compares_versions_by_release() {
        assert_eq!(release("0.2.5"), Some([0, 2, 5]));
        assert_eq!(release("0.2.6.dev0"), Some([0, 2, 6]));
        assert_eq!(release("1.0rc1"), Some([1, 0, 0]));
        assert_eq!(release("0.10"), Some([0, 10, 0]));
        assert_eq!(release("dev"), None);
        assert!(new_enough(Some(VERSION)));
        assert!(new_enough(Some("99.0.0")));
        assert!(!new_enough(Some("0.2.2")));
        assert!(!new_enough(Some("unknown")));
        assert!(!new_enough(None));
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

    /// A stand-in uv that "installs" into $UV_PROJECT_ENVIRONMENT, and a bundled project at `version`.
    fn fake_project(name: &str, version: &str, uv_body: &str) -> (Core, PathBuf) {
        let (core, root) = fake_install(
            name,
            &format!(
                "echo \"env=$UV_PROJECT_ENVIRONMENT\" >> '{}/calls.log'\n{uv_body}\nmkdir -p \"$UV_PROJECT_ENVIRONMENT/bin\" && touch \"$UV_PROJECT_ENVIRONMENT/bin/python\" \"$UV_PROJECT_ENVIRONMENT/bin/openmymemo\" && chmod +x \"$UV_PROJECT_ENVIRONMENT/bin/python\" \"$UV_PROJECT_ENVIRONMENT/bin/openmymemo\"",
                temp_dir_path(name).display()
            ),
        );
        fs::create_dir_all(root.join("core")).unwrap();
        fs::write(root.join("core/pyproject.toml"), format!("[project]\nname = \"openmymemo\"\nversion = \"{version}\"\n")).unwrap();
        fs::create_dir_all(root.join("vault")).unwrap();
        (core, root)
    }

    fn temp_dir_path(name: &str) -> PathBuf {
        std::env::temp_dir().join(format!("openmymemo-desktop-{}-{name}", std::process::id()))
    }

    #[test]
    fn an_update_is_installed_into_a_new_environment_even_while_a_server_runs() {
        let (mut core, root) = fake_project("update", "9.1.0", "");
        // The folder's server (an AI app's MCP process, say) runs from the previous version's environment.
        let old = root.join("support/venvs/9.0.0-1");
        fs::create_dir_all(old.join("bin")).unwrap();
        fs::write(old.join(".openmymemo-ready"), "9.0.0\n").unwrap();
        std::os::unix::fs::symlink("venvs/9.0.0-1", root.join("support/venv")).unwrap();
        let port = fake_server(&root.join("vault"), VERSION);
        core.ports = port..port + 1;

        let (found, _) = core.ensure(&|_, _| {}).expect("the running server");
        assert_eq!(found, port);
        let stamp = fs::metadata(root.join("core/pyproject.toml")).unwrap().modified().unwrap()
            .duration_since(std::time::UNIX_EPOCH).unwrap().as_secs();
        let new = root.join(format!("support/venvs/9.1.0-{stamp}"));
        let calls = fs::read_to_string(root.join("calls.log")).unwrap_or_default();
        assert!(calls.contains(&format!("env={}", new.display())), "{calls}");
        assert_eq!(fs::read_link(root.join("support/venv")).unwrap(), Path::new("venvs").join(new.file_name().unwrap()));
        assert_eq!(fs::read_to_string(new.join(".openmymemo-ready")).unwrap().trim(), "9.1.0");
        assert!(old.join(".openmymemo-ready").exists(), "the previous environment stays for what still runs from it");

        // The next start finds it ready and doesn't run uv.
        let (mut again, _) = (Core::new(root.join("vault"), core.paths, port..port + 1), ());
        again.ensure(&|_, _| {}).unwrap();
        assert_eq!(fs::read_to_string(root.join("calls.log")).unwrap(), calls);
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
        let (mut core, root) = fake_project("offline", "9.1.0", &format!("echo '{OFFLINE}' >&2; exit 2"));
        let err = core.prepare(&|_, _| {}).unwrap_err();
        assert!(err.offline && err.title == "No internet connection", "{err:?}");
        // No Python yet, so no second try from the cache.
        let calls = syncs(&root);
        assert_eq!(calls.len(), 1, "{calls:?}");
        assert!(calls[0].contains("sync --project") && calls[0].contains("--no-editable"), "{calls:?}");
        assert!(core.ready.is_none() && fs::symlink_metadata(root.join("support/venv")).is_err());
    }

    /// The uv calls a test's stand-in uv received.
    fn syncs(root: &Path) -> Vec<String> {
        let calls = fs::read_to_string(root.join("calls.log")).unwrap_or_default();
        calls.lines().filter(|l| l.starts_with("sync")).map(String::from).collect()
    }

    fn ready_env(root: &Path, name: &str) -> PathBuf {
        let dir = root.join("support/venvs").join(name);
        fs::create_dir_all(dir.join("bin")).unwrap();
        fs::write(dir.join("bin/python"), "").unwrap();
        fs::write(dir.join("bin/openmymemo"), "").unwrap();
        fs::write(dir.join(READY), "9.0.0\n").unwrap();
        dir
    }

    #[test]
    fn a_new_build_without_a_network_installs_from_the_cache() {
        let (mut core, root) = fake_project(
            "cache",
            "9.1.0",
            &format!(r#"case "$*" in *--offline*) ;; *) echo '{OFFLINE}' >&2; exit 2 ;; esac"#),
        );
        fs::create_dir_all(root.join("support/python/cpython-3.12.13-macos-aarch64-none")).unwrap();
        core.prepare(&|_, _| {}).expect("installed from the cache");
        let calls = syncs(&root);
        assert_eq!(calls.len(), 2, "{calls:?}");
        assert!(calls[1].ends_with("--offline"), "{calls:?}");
        assert!(is_ready(core.ready.as_ref().unwrap()));
    }

    #[test]
    fn when_an_update_cant_be_installed_the_current_environment_still_starts() {
        let (mut core, root) = fake_project("stuck", "9.1.0", &format!("echo '{OFFLINE}' >&2; exit 2"));
        let current = ready_env(&root, "9.0.0-1");
        std::os::unix::fs::symlink("venvs/9.0.0-1", root.join("support/venv")).unwrap();
        core.prepare(&|_, _| {}).expect("the environment there is");
        assert_eq!(core.ready, Some(fs::canonicalize(&current).unwrap()));
        assert_eq!(fs::read_link(root.join("support/venv")).unwrap(), Path::new("venvs/9.0.0-1"));
    }

    #[test]
    fn the_server_started_from_an_older_environment_is_used() {
        // The update couldn't be installed, so the server starts from the environment before it, older than the app.
        use std::os::unix::fs::PermissionsExt;
        let (mut core, root) = fake_project("stuck-serve", "9.1.0", &format!("echo '{OFFLINE}' >&2; exit 2"));
        let current = ready_env(&root, "9.0.0-1");
        std::os::unix::fs::symlink("venvs/9.0.0-1", root.join("support/venv")).unwrap();
        // Its `openmymemo --vault <vault> serve --init --no-open --port <port>` answers /api/state as a 0.0.1 core.
        let server = current.join("bin/openmymemo");
        fs::write(&server, format!("#!/bin/sh\nexec python3 -c '{OLD_CORE}' \"$2\" \"$7\"\n")).unwrap();
        fs::set_permissions(&server, fs::Permissions::from_mode(0o755)).unwrap();
        let port = TcpListener::bind("127.0.0.1:0").unwrap().local_addr().unwrap().port();
        core.ports = port..port + 1;

        let found = core.ensure(&|_, _| {});
        core.stop();
        let (found, state) = found.expect("the app's own server");
        assert_eq!(found, port);
        assert!(!new_enough(state.version.as_deref()), "{state:?}");
    }

    /// A stand-in core: serves /api/state for the folder in argv[1] on the port in argv[2], with version 0.0.1.
    const OLD_CORE: &str = r#"
import http.server, json, sys
body = json.dumps({"vault": sys.argv[1], "version": "0.0.1", "counts": {"pending": 0, "unreviewed": 0}}).encode()
class State(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *args):
        pass
http.server.HTTPServer(("127.0.0.1", int(sys.argv[2])), State).serve_forever()
"#;

    #[test]
    fn an_interrupted_install_is_finished_and_only_then_made_current() {
        // Quit during the setup: uv had written some of the environment, not the marker.
        let (mut core, root) = fake_project("interrupted", "9.1.0", "");
        let (_, dir) = core.paths.build_env().unwrap();
        fs::create_dir_all(dir.join("bin")).unwrap();
        fs::write(dir.join("bin/openmymemo"), "").unwrap();
        assert!(!is_ready(&dir) && core.paths.current().is_none());
        core.prepare(&|_, _| {}).unwrap();
        assert_eq!(syncs(&root).len(), 1);
        assert_eq!(core.paths.current(), Some(fs::canonicalize(&dir).unwrap()));
    }

    #[test]
    fn keeps_the_current_and_the_previous_environment_and_moves_an_old_folder_in() {
        let (mut core, root) = fake_project("garbage", "9.1.0", "");
        let older = ready_env(&root, "8.0.0-1");
        let stale = root.join("support/venvs/8.5.0-1"); // never finished, from long ago
        fs::create_dir_all(&stale).unwrap();
        let recent = root.join("support/venvs/9.0.5-1"); // a launcher may be installing into it
        fs::create_dir_all(&recent).unwrap();
        let day_old = std::time::SystemTime::now() - Duration::from_secs(2 * 24 * 3600);
        File::options().write(true).open(&stale).or_else(|_| File::open(&stale)).unwrap().set_modified(day_old).unwrap();
        // The environment from before they were per build: a plain folder named venv.
        fs::create_dir_all(root.join("support/venv/bin")).unwrap();
        fs::write(root.join("support/venv/bin/openmymemo"), "").unwrap();

        core.prepare(&|_, _| {}).unwrap();
        let mut left: Vec<String> = fs::read_dir(root.join("support/venvs")).unwrap().flatten()
            .map(|e| e.file_name().to_string_lossy().into_owned()).collect();
        left.sort();
        let current = core.ready.as_ref().unwrap().file_name().unwrap().to_string_lossy().into_owned();
        assert!(!older.exists() && !stale.exists() && recent.exists(), "{left:?}");
        assert_eq!(left.len(), 3, "{left:?}");
        assert!(left.iter().any(|n| n.starts_with("legacy-")) && left.contains(&current), "{left:?}");
        assert!(fs::symlink_metadata(root.join("support/venv")).unwrap().file_type().is_symlink());
    }

    #[test]
    fn keeps_an_older_environment_a_server_still_runs_from() {
        let (mut core, root) = fake_project("in-use", "9.1.0", "");
        let busy = ready_env(&root, "8.0.0-1");
        let idle = ready_env(&root, "8.5.0-1");
        fs::write(idle.join(IN_USE), "").unwrap(); // its server has exited
        let previous = ready_env(&root, "9.0.0-1");
        std::os::unix::fs::symlink("venvs/9.0.0-1", root.join("support/venv")).unwrap();
        // A server started two updates ago holds its environment's lock, as cli.py does.
        let held = File::options().read(true).write(true).create(true).truncate(false).open(busy.join(IN_USE)).unwrap();
        assert_eq!(unsafe { libc::flock(held.as_raw_fd(), libc::LOCK_SH | libc::LOCK_NB) }, 0);

        core.prepare(&|_, _| {}).unwrap();
        assert!(busy.join(READY).exists(), "the environment a server runs from stays");
        assert!(!idle.exists() && previous.exists());

        // Once that server is gone, the next update's clean-up removes it. Tests run in parallel and spawn
        // processes: one forked while `held` is open shares its lock until its exec closes the descriptor, so
        // clean up again for a moment, as the next update would.
        drop(held);
        let deadline = Instant::now() + Duration::from_secs(5);
        while busy.exists() && Instant::now() < deadline {
            collect_garbage(&root.join("support/venvs"), &[core.ready.as_deref().unwrap(), &previous]);
            if busy.exists() {
                std::thread::sleep(Duration::from_millis(20));
            }
        }
        assert!(!busy.exists());
    }

    #[test]
    fn never_follows_a_symlink_out_of_the_environments_folder() {
        let (_core, root) = fake_project("symlink", "9.1.0", "");
        let outside = ready_env(&root, "../../elsewhere"); // a ready-looking folder outside venvs/
        std::os::unix::fs::symlink(&outside, root.join("support/venvs/7.0.0-1")).unwrap();
        collect_garbage(&root.join("support/venvs"), &[]);
        assert!(outside.join(READY).exists(), "the symlink's target is left alone");
    }

    #[test]
    fn a_long_install_is_explained_and_other_failures_show_the_log() {
        let (mut core, _) = fake_project("slow", "9.1.0", "sleep 2; echo 'error: something else broke' >&2; exit 1");
        let said = Mutex::new(Vec::new());
        let err = core.prepare(&|title, _| said.lock().unwrap().push(title.to_string())).unwrap_err();
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


#[cfg(all(test, windows))]
mod windows_tests {
    use super::*;

    fn support(name: &str) -> PathBuf {
        let root = std::env::temp_dir().join(format!("openmymemo-desktop-{}-{name}", std::process::id()));
        let _ = fs::remove_dir_all(&root);
        fs::create_dir_all(root.join("venvs")).unwrap();
        root
    }

    fn ready_env(support: &Path, name: &str) -> PathBuf {
        let dir = support.join("venvs").join(name);
        fs::create_dir_all(dir.join("Scripts")).unwrap();
        for program in ["python", "openmymemo"] {
            fs::write(env_bin(&dir, program), "").unwrap();
        }
        fs::write(dir.join(READY), "1.0.0\n").unwrap();
        dir
    }

    #[test]
    fn the_current_environment_is_named_in_a_file() {
        let support = support("current");
        let first = ready_env(&support, "1.0.0-1");
        let second = ready_env(&support, "1.0.1-2");
        assert_eq!(current_dir(&support), None);
        assert_eq!(make_current(&support, &first).unwrap(), None);
        assert_eq!(current_dir(&support), fs::canonicalize(&first).ok());
        assert_eq!(make_current(&support, &second).unwrap(), fs::canonicalize(&first).ok());
        assert_eq!(fs::read_to_string(support.join(CURRENT)).unwrap(), "1.0.1-2\n");
        let names: Vec<_> = fs::read_dir(&support).unwrap().flatten().map(|e| e.file_name()).collect();
        assert_eq!(names.len(), 2, "no temporary file left: {names:?}");
        for outside in ["..", "..\\elsewhere", "C:\\Windows", ".hidden", ""] {
            fs::write(support.join(CURRENT), outside).unwrap();
            assert_eq!(current_dir(&support), None, "{outside:?}");
        }
    }

    #[test]
    fn an_environment_a_process_holds_open_is_kept() {
        let support = support("in-use");
        let old = ready_env(&support, "1.0.0-1");
        let keep = ready_env(&support, "1.0.1-2");
        fs::write(old.join(IN_USE), "").unwrap();
        let held = File::open(old.join(IN_USE)).unwrap(); // as cli.py's _hold_environment does on Windows
        collect_garbage(&support.join("venvs"), &[&keep]);
        assert!(old.exists(), "in use");
        drop(held);
        collect_garbage(&support.join("venvs"), &[&keep]);
        assert!(!old.exists() && keep.exists());
    }
}
