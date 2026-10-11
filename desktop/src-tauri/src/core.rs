//! The Python core behind the window: find the panel server for the memory folder, or start one.
//!
//! The app bundles `uv` and the package source (see scripts/stage_desktop.py) and runs
//! `openmymemo serve`, the same server the CLI and the AI apps' plugins run. One server per folder:
//! if a port already serves this folder (Claude Desktop's extension often got there first), the app
//! uses it, and starts its own when that one goes away. A port serving something else is skipped.

use std::fs::{self, File, OpenOptions};
use std::io::{Read, Seek, SeekFrom};
use std::os::unix::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicI32, Ordering};
use std::time::{Duration, Instant};

use serde::Deserialize;

pub const BASE_PORT: u16 = 8787;
const PORTS: u16 = 10;
/// The first launch downloads Python and the dependencies; give it time.
const START_TIMEOUT: Duration = Duration::from_secs(600);

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
}

pub struct Core {
    pub vault: PathBuf,
    paths: Paths,
    child: Option<(Child, u16)>,
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
    pub fn new(vault: PathBuf, paths: Paths) -> Self {
        Core { vault, paths, child: None }
    }

    /// The port that serves this folder, starting a server if none does. `status` reports progress.
    pub fn ensure(&mut self, status: &dyn Fn(&str, &str)) -> Result<(u16, PanelState), String> {
        if let Some(port) = self.child_port() {
            return self.wait_ready(port, status);
        }
        for port in BASE_PORT..BASE_PORT + PORTS {
            match probe(port, &self.vault) {
                Probe::Ours(state) => return Ok((port, state)),
                Probe::Other => continue,
                Probe::Free => {
                    self.spawn(port)?;
                    return self.wait_ready(port, status);
                }
            }
        }
        Err(format!("Ports {BASE_PORT}–{} are all in use by other programs.", BASE_PORT + PORTS - 1))
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

    fn spawn(&mut self, port: u16) -> Result<(), String> {
        let p = &self.paths;
        for dir in [&p.support, &p.cache, p.log.parent().unwrap_or(Path::new("/tmp"))] {
            fs::create_dir_all(dir).map_err(|e| format!("Can't create {}: {e}", dir.display()))?;
        }
        let log = OpenOptions::new().create(true).append(true).open(&p.log).map_err(|e| e.to_string())?;
        let child = Command::new(&p.uv)
            .args(["run", "--project"])
            .arg(&p.project)
            .args(["--frozen", "--no-dev", "--quiet", "openmymemo", "--vault"])
            .arg(&self.vault)
            .args(["serve", "--no-open", "--port", &port.to_string()])
            // Everything the app installs stays in its own folders, and Python is uv's own, never the system's.
            .env("UV_PROJECT_ENVIRONMENT", p.support.join("venv"))
            .env("UV_PYTHON_INSTALL_DIR", p.support.join("python"))
            .env("UV_PYTHON_PREFERENCE", "only-managed")
            .env("UV_PYTHON", "3.12")
            .env("UV_CACHE_DIR", &p.cache)
            .env("PYTHONUNBUFFERED", "1")
            .stdin(Stdio::null())
            .stdout(log.try_clone().map_err(|e| e.to_string())?)
            .stderr(log)
            .process_group(0)
            .spawn()
            .map_err(|e| format!("Can't start {}: {e}", p.uv.display()))?;
        CHILD_PGID.store(child.id() as i32, Ordering::SeqCst);
        self.child = Some((child, port));
        Ok(())
    }

    fn wait_ready(&mut self, port: u16, status: &dyn Fn(&str, &str)) -> Result<(u16, PanelState), String> {
        let started = Instant::now();
        let mut said_setup = false;
        loop {
            match probe(port, &self.vault) {
                Probe::Ours(state) => return Ok((port, state)),
                Probe::Other | Probe::Free => {}
            }
            if self.child_port().is_none() {
                return Err(format!("The local server stopped. {}", self.log_tail()));
            }
            if !said_setup && started.elapsed() > Duration::from_secs(4) {
                said_setup = true;
                status(
                    "Setting up…",
                    "The first start downloads Python and what OpenMyMemo needs. It takes a minute or two.",
                );
            }
            if started.elapsed() > START_TIMEOUT {
                self.stop();
                return Err(format!("The local server didn't start. {}", self.log_tail()));
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
