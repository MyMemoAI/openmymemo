//! The Python core behind the window: find the panel server for the memory folder, or start one.
//!
//! The app bundles `uv` and the package source (see scripts/stage_desktop.py) and runs
//! `openmymemo serve`, the same server the CLI and the AI apps' plugins run. One server per folder:
//! if a port already serves this folder (Claude Desktop's extension often got there first), the app
//! uses it, and starts its own when that one goes away. A port serving something else is skipped.

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
    ports: Range<u16>,
    child: Option<(Child, u16)>,
    /// The port that served this folder last, looked at first so the window's address stays put.
    last_port: Option<u16>,
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
        Core { vault, paths, ports, child: None, last_port: None }
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
    pub fn ensure(&mut self, status: &dyn Fn(&str, &str)) -> Result<(u16, PanelState), String> {
        let found = match self.child_port() {
            Some(port) => self.wait_ready(port, status),
            None => self.find_or_start(status),
        };
        if let Ok((port, _)) = &found {
            self.last_port = Some(*port);
        }
        found
    }

    fn find_or_start(&mut self, status: &dyn Fn(&str, &str)) -> Result<(u16, PanelState), String> {
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
            return Err(format!(
                "Ports {}–{} are all in use by other programs.",
                self.ports.start,
                self.ports.end - 1
            ));
        };
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
            // --init: a first launch creates the vault in a missing or empty folder (and refuses any other).
            .args(["serve", "--init", "--no-open", "--port", &port.to_string()])
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
        let paths = Paths { uv: none.join("uv"), project: none.clone(), support: none.clone(), cache: none.clone(), log: none.join("core.log") };
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
        assert!(err.contains("in use by other programs"), "{err}");
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

