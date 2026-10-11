//! OpenMyMemo for Mac: a window with the panel, a menu-bar item, and the Python core behind both.
//!
//! The window first shows the bundled setup page (../splash), then the panel from the local server at
//! `http://127.0.0.1:<port>/?desktop=1`, which switches the panel to its Mac layout. A monitor thread
//! keeps the server running, follows its port, puts the number of changes to review next to the
//! menu-bar icon, and notifies when an AI app changes something or something starts waiting.
//! Closing the window keeps the app in the menu bar; it quits from the menu.

mod core;

use std::collections::HashSet;
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, AtomicU16, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use serde::{Deserialize, Serialize};
use tauri::image::Image;
use tauri::menu::{Menu, MenuBuilder, MenuItem, MenuItemBuilder, SubmenuBuilder};
use tauri::tray::{TrayIcon, TrayIconBuilder};
use tauri::webview::DownloadEvent;
use tauri::{AppHandle, LogicalPosition, Manager, RunEvent, TitleBarStyle, Url, WebviewUrl, WebviewWindow, WindowEvent};
use tauri_plugin_autostart::{MacosLauncher, ManagerExt as _};
use tauri_plugin_dialog::DialogExt;
use tauri_plugin_notification::NotificationExt;

use crate::core::{Core, Counts, Paths, Proposal};

const HIDDEN_ARG: &str = "--hidden";

/// The app's own settings, in ~/Library/Application Support/<id>/settings.json.
#[derive(Clone, Serialize, Deserialize)]
struct Settings {
    vault: PathBuf,
}

struct Shared {
    settings: Mutex<Settings>,
    settings_path: PathBuf,
    /// The port the window's panel comes from; 0 while the setup page shows.
    port: AtomicU16,
    /// Set when the memory folder changed: the monitor restarts the server on the new folder.
    restart: AtomicBool,
    /// Where the bundled setup page loads from (tauri://localhost, or Tauri's own server in development).
    splash_url: Mutex<Option<Url>>,
    tray: Mutex<Option<TrayIcon>>,
    review_item: Mutex<Option<MenuItem<tauri::Wry>>>,
}

fn default_vault() -> PathBuf {
    dirs_home().join("OpenMyMemo")
}

fn dirs_home() -> PathBuf {
    std::env::var_os("HOME").map(PathBuf::from).unwrap_or_else(|| PathBuf::from("/"))
}

/// OPENMYMEMO_HOME (as for the CLI) overrides the saved folder, for development.
fn load_settings(path: &PathBuf) -> Settings {
    if let Some(home) = std::env::var_os("OPENMYMEMO_HOME").filter(|v| !v.is_empty()) {
        return Settings { vault: PathBuf::from(home) };
    }
    std::fs::read_to_string(path)
        .ok()
        .and_then(|s| serde_json::from_str(&s).ok())
        .unwrap_or(Settings { vault: default_vault() })
}

fn main_window(app: &AppHandle) -> Option<WebviewWindow> {
    app.get_webview_window("main")
}

fn show_window(app: &AppHandle) {
    if let Some(w) = main_window(app) {
        let _ = w.unminimize();
        let _ = w.show();
        let _ = w.set_focus();
    }
}

/// Run one of the panel's commands (desktop.ts): the panel routes "new-memory" and "find" itself.
fn panel_command(app: &AppHandle, cmd: &str) {
    show_window(app);
    if let Some(w) = main_window(app) {
        let _ = w.eval(format!(
            "window.dispatchEvent(new CustomEvent('openmymemo:command', {{ detail: {cmd:?} }}))"
        ));
    }
}

fn open_panel_at(app: &AppHandle, hash: &str) {
    let shared = app.state::<Arc<Shared>>();
    let port = shared.port.load(Ordering::SeqCst);
    show_window(app);
    if port != 0 {
        if let (Some(w), Ok(url)) = (main_window(app), Url::parse(&format!("http://127.0.0.1:{port}/?desktop=1{hash}"))) {
            let _ = w.navigate(url);
        }
    }
}

fn splash_status(app: &AppHandle, status: &str, detail: &str, error: bool) {
    if let Some(w) = main_window(app) {
        let _ = w.eval(format!(
            "window.__setStatus && window.__setStatus({status:?}, {detail:?}, {error})"
        ));
    }
}

// ---- commands the panel calls (desktop.ts `shell`) ------------------------------------------------

#[tauri::command]
fn reveal_path(path: String) -> Result<(), String> {
    let path = PathBuf::from(path);
    let mut cmd = std::process::Command::new("/usr/bin/open");
    if path.is_dir() {
        cmd.arg(&path);
    } else {
        cmd.arg("-R").arg(&path);
    }
    cmd.status().map(|_| ()).map_err(|e| e.to_string())
}

#[derive(Serialize)]
struct AppSettings {
    vault: PathBuf,
    autostart: bool,
    version: String,
}

#[tauri::command]
fn app_settings(app: AppHandle, shared: tauri::State<Arc<Shared>>) -> AppSettings {
    AppSettings {
        vault: shared.settings.lock().unwrap().vault.clone(),
        autostart: app.autolaunch().is_enabled().unwrap_or(false),
        version: app.package_info().version.to_string(),
    }
}

#[tauri::command]
fn set_autostart(app: AppHandle, enabled: bool) -> Result<(), String> {
    let launcher = app.autolaunch();
    if enabled { launcher.enable() } else { launcher.disable() }.map_err(|e| e.to_string())
}

#[tauri::command]
async fn choose_vault(app: AppHandle) -> Result<(), String> {
    let shared = app.state::<Arc<Shared>>().inner().clone();
    let current = shared.settings.lock().unwrap().vault.clone();
    let picked = app
        .dialog()
        .file()
        .set_title("Choose your memory folder")
        .set_directory(current.parent().unwrap_or(&current))
        .blocking_pick_folder();
    let Some(picked) = picked else { return Ok(()) };
    let vault = picked.into_path().map_err(|e| e.to_string())?;
    if vault == current {
        return Ok(());
    }
    let settings = Settings { vault };
    std::fs::create_dir_all(shared.settings_path.parent().unwrap()).map_err(|e| e.to_string())?;
    std::fs::write(&shared.settings_path, serde_json::to_string_pretty(&settings).unwrap()).map_err(|e| e.to_string())?;
    *shared.settings.lock().unwrap() = settings;
    shared.restart.store(true, Ordering::SeqCst);
    Ok(())
}

// ---- the monitor: keep the server up, follow it, badge and notify ----------------------------------

fn to_review_label(counts: &Counts) -> String {
    match counts.pending + counts.unreviewed {
        0 => "Nothing to review".into(),
        1 => "1 change to review".into(),
        n => format!("{n} changes to review"),
    }
}

/// "Claude Code", "Claude Code and Cursor", "A, B and C".
fn list_names(names: &[String]) -> String {
    match names.len() {
        0 => String::new(),
        1 => names[0].clone(),
        n => format!("{} and {}", names[..n - 1].join(", "), names[n - 1]),
    }
}

/// One notification for what arrived since the last look: applied changes, then what waits.
fn notify_new(app: &AppHandle, port: u16, seen: &mut HashSet<String>, first: bool) {
    let applied: Vec<Proposal> = core::api(port, "/api/proposals?unreviewed=true&limit=100").unwrap_or_default();
    let waiting: Vec<Proposal> = core::api(port, "/api/proposals?status=pending&limit=100").unwrap_or_default();
    let new_applied: Vec<&Proposal> = applied.iter().filter(|p| !seen.contains(&p.id)).collect();
    let new_waiting: Vec<&Proposal> = waiting.iter().filter(|p| !seen.contains(&p.id)).collect();
    seen.extend(applied.iter().chain(waiting.iter()).map(|p| p.id.clone()));
    if first || (new_applied.is_empty() && new_waiting.is_empty()) {
        return;
    }
    let mut names: Vec<String> = Vec::new();
    for p in new_applied.iter().chain(new_waiting.iter()) {
        if !names.contains(&p.connector_name) {
            names.push(p.connector_name.clone());
        }
    }
    let who = list_names(&names);
    let body = match (new_applied.len(), new_waiting.len()) {
        (1, 0) => {
            let p = new_applied[0];
            let verb = match p.op.as_str() { "add" => "remembered", "archive" => "archived", _ => "changed" };
            let text = p.final_content.as_deref().or(p.content.as_deref()).unwrap_or("");
            format!("{who} {verb}: “{text}”")
        }
        (0, 1) => format!("{who} wants to change a memory. It waits for you."),
        (a, 0) => format!("{who} made {a} changes to your memory."),
        (0, w) => format!("{who} proposed {w} changes. They wait for you."),
        (a, w) => format!("{who} made {a} changes; {w} more wait for you."),
    };
    let _ = app.notification().builder().title("OpenMyMemo").body(body).show();
}

fn update_badge(app: &AppHandle, counts: &Counts) {
    let shared = app.state::<Arc<Shared>>();
    let n = counts.pending + counts.unreviewed;
    let tray = shared.tray.lock().unwrap().clone();
    if let Some(tray) = tray {
        let _ = tray.set_title(if n > 0 { Some(n.to_string()) } else { None });
        let _ = tray.set_tooltip(Some(format!("OpenMyMemo · {}", to_review_label(counts))));
    }
    let item = shared.review_item.lock().unwrap().clone();
    if let Some(item) = item {
        let _ = item.set_text(to_review_label(counts));
        let _ = item.set_enabled(n > 0);
    }
}

fn monitor(app: AppHandle, paths: Paths) {
    let shared = app.state::<Arc<Shared>>().inner().clone();
    let vault = shared.settings.lock().unwrap().vault.clone();
    let mut core = Core::new(vault, paths);
    let mut last_counts: Option<Counts> = None;
    let mut seen: HashSet<String> = HashSet::new();
    loop {
        if shared.restart.swap(false, Ordering::SeqCst) {
            core.stop();
            core.vault = shared.settings.lock().unwrap().vault.clone();
            shared.port.store(0, Ordering::SeqCst);
            last_counts = None;
            seen.clear();
            let splash = shared.splash_url.lock().unwrap().clone();
            if let (Some(w), Some(url)) = (main_window(&app), splash) {
                let _ = w.navigate(url);
            }
            std::thread::sleep(Duration::from_millis(300));
        }

        let on_splash = shared.port.load(Ordering::SeqCst) == 0;
        let status_app = app.clone();
        let status = move |s: &str, d: &str| splash_status(&status_app, s, d, false);
        match core.ensure(&status) {
            Ok((port, state)) => {
                if shared.port.swap(port, Ordering::SeqCst) != port {
                    if let (Some(w), Ok(url)) = (main_window(&app), Url::parse(&format!("http://127.0.0.1:{port}/?desktop=1"))) {
                        let _ = w.navigate(url);
                    }
                }
                if last_counts.as_ref() != Some(&state.counts) {
                    let grew = last_counts.as_ref().map_or(true, |c| {
                        state.counts.pending > c.pending || state.counts.unreviewed > c.unreviewed
                    });
                    if grew {
                        notify_new(&app, port, &mut seen, last_counts.is_none());
                    }
                    update_badge(&app, &state.counts);
                    last_counts = Some(state.counts);
                }
            }
            Err(e) => {
                if on_splash {
                    splash_status(&app, "OpenMyMemo couldn't start", &e, true);
                }
            }
        }

        for _ in 0..16 {
            if shared.restart.load(Ordering::SeqCst) {
                break;
            }
            std::thread::sleep(Duration::from_millis(250));
        }
    }
}

// ---- menus ------------------------------------------------------------------------------------------

fn app_menu(app: &AppHandle) -> tauri::Result<Menu<tauri::Wry>> {
    let item = |id: &str, label: &str, accel: &str| {
        MenuItemBuilder::with_id(id, label).accelerator(accel).build(app)
    };
    let app_sub = SubmenuBuilder::new(app, "OpenMyMemo")
        .about(None)
        .separator()
        .item(&item("settings", "Settings…", "CmdOrCtrl+,")?)
        .separator()
        .services()
        .separator()
        .hide()
        .hide_others()
        .show_all()
        .separator()
        .quit()
        .build()?;
    let file = SubmenuBuilder::new(app, "File")
        .item(&item("new-memory", "New Memory", "CmdOrCtrl+N")?)
        .separator()
        .close_window()
        .build()?;
    let edit = SubmenuBuilder::new(app, "Edit")
        .undo()
        .redo()
        .separator()
        .cut()
        .copy()
        .paste()
        .select_all()
        .separator()
        .item(&item("find", "Find…", "CmdOrCtrl+F")?)
        .build()?;
    let view = SubmenuBuilder::new(app, "View")
        .item(&item("toggle-sidebar", "Toggle Sidebar", "Ctrl+CmdOrCtrl+S")?)
        .item(&item("reload", "Reload", "CmdOrCtrl+R")?)
        .separator()
        .fullscreen()
        .build()?;
    let window = SubmenuBuilder::new(app, "Window").minimize().maximize().separator().bring_all_to_front().build()?;
    let help = SubmenuBuilder::new(app, "Help").text("help", "OpenMyMemo on GitHub").build()?;
    MenuBuilder::new(app).items(&[&app_sub, &file, &edit, &view, &window, &help]).build()
}

fn on_menu(app: &AppHandle, id: &str) {
    match id {
        "settings" | "new-memory" | "find" | "toggle-sidebar" => panel_command(app, id),
        "reload" => {
            if let Some(w) = main_window(app) {
                let _ = w.eval("location.reload()");
            }
        }
        "help" => {
            let _ = std::process::Command::new("/usr/bin/open").arg("https://github.com/MyMemoAI/openmymemo").status();
        }
        "open" => show_window(app),
        "review" => open_panel_at(app, "#/changes"),
        "quit" => app.exit(0),
        _ => {}
    }
}

fn build_tray(app: &AppHandle) -> tauri::Result<()> {
    let review = MenuItemBuilder::with_id("review", "Nothing to review").enabled(false).build(app)?;
    let menu = MenuBuilder::new(app)
        .text("open", "Open OpenMyMemo")
        .item(&review)
        .separator()
        .text("quit", "Quit OpenMyMemo")
        .build()?;
    let tray = TrayIconBuilder::with_id("main")
        .icon(Image::from_bytes(include_bytes!("../icons/tray.png"))?)
        .icon_as_template(true)
        .tooltip("OpenMyMemo")
        .menu(&menu)
        .show_menu_on_left_click(true)
        .on_menu_event(|app, event| on_menu(app, event.id().as_ref()))
        .build(app)?;
    let shared = app.state::<Arc<Shared>>();
    *shared.tray.lock().unwrap() = Some(tray);
    *shared.review_item.lock().unwrap() = Some(review);
    Ok(())
}

// ---- the window -------------------------------------------------------------------------------------

/// Only the setup page and the panel load in the window; any other link opens in the browser.
fn allow_navigation(shared: &Shared, url: &Url) -> bool {
    let splash = shared.splash_url.lock().unwrap().as_ref().map(|u| u.origin());
    if splash.map_or(true, |o| o == url.origin()) {
        return true;
    }
    match url.scheme() {
        "about" => true,
        "http" if url.host_str() == Some("127.0.0.1") && url.port() == Some(shared.port.load(Ordering::SeqCst)) => true,
        "http" | "https" | "mailto" => {
            let _ = std::process::Command::new("/usr/bin/open").arg(url.as_str()).status();
            false
        }
        _ => false,
    }
}

fn build_window(app: &AppHandle, visible: bool) -> tauri::Result<WebviewWindow> {
    let shared = app.state::<Arc<Shared>>().inner().clone();
    let window = tauri::WebviewWindowBuilder::new(app, "main", WebviewUrl::App("index.html".into()))
        .title("OpenMyMemo")
        .inner_size(1280.0, 860.0)
        .min_inner_size(900.0, 560.0)
        .title_bar_style(TitleBarStyle::Overlay)
        .hidden_title(true)
        .traffic_light_position(LogicalPosition::new(20.0, 20.0))
        .visible(visible)
        .on_navigation(move |url| allow_navigation(&shared, url))
        // Export → Download: saved to ~/Downloads under the suggested name, then shown in Finder.
        .on_download(|_, event| {
            if let DownloadEvent::Finished { path: Some(path), success: true, .. } = event {
                let _ = std::process::Command::new("/usr/bin/open").arg("-R").arg(path).status();
            }
            true
        })
        .build()?;
    *app.state::<Arc<Shared>>().splash_url.lock().unwrap() = window.url().ok();
    let handle = app.clone();
    window.on_window_event(move |event| {
        // Closing the window keeps the app running in the menu bar.
        if let WindowEvent::CloseRequested { api, .. } = event {
            api.prevent_close();
            if let Some(w) = main_window(&handle) {
                let _ = w.hide();
            }
        }
    });
    Ok(window)
}

/// A quit by signal (kill, Ctrl-C in a terminal) skips RunEvent::Exit; stop the server there too.
extern "C" fn on_signal(sig: libc::c_int) {
    core::stop_child();
    unsafe {
        libc::signal(sig, libc::SIG_DFL);
        libc::raise(sig);
    }
}

fn main() {
    for sig in [libc::SIGTERM, libc::SIGINT, libc::SIGHUP] {
        unsafe {
            libc::signal(sig, on_signal as extern "C" fn(libc::c_int) as libc::sighandler_t);
        }
    }
    tauri::Builder::default()
        .plugin(tauri_plugin_notification::init())
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_autostart::init(MacosLauncher::LaunchAgent, Some(vec![HIDDEN_ARG])))
        .invoke_handler(tauri::generate_handler![reveal_path, app_settings, set_autostart, choose_vault])
        .menu(|app| app_menu(app))
        .on_menu_event(|app, event| on_menu(app, event.id().as_ref()))
        .setup(|app| {
            let handle = app.handle().clone();
            let path = app.path();
            let support = path.app_data_dir()?;
            let settings_path = support.join("settings.json");
            let shared = Arc::new(Shared {
                settings: Mutex::new(load_settings(&settings_path)),
                settings_path,
                port: AtomicU16::new(0),
                restart: AtomicBool::new(false),
                splash_url: Mutex::new(None),
                tray: Mutex::new(None),
                review_item: Mutex::new(None),
            });
            app.manage(shared);

            // The bundled uv sits next to the executable; the package source is a resource.
            let exe_dir = std::env::current_exe()?.parent().map(PathBuf::from).unwrap_or_default();
            let paths = Paths {
                uv: exe_dir.join("uv"),
                project: path.resource_dir()?.join("core"),
                support,
                cache: path.app_cache_dir()?,
                log: path.app_log_dir()?.join("core.log"),
            };

            let hidden = std::env::args().any(|a| a == HIDDEN_ARG);
            build_window(&handle, !hidden)?;
            build_tray(&handle)?;
            std::thread::Builder::new().name("openmymemo-monitor".into()).spawn(move || monitor(handle, paths))?;
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building OpenMyMemo")
        .run(|app, event| match event {
            // Clicking the Dock icon brings the window back.
            RunEvent::Reopen { .. } => show_window(app),
            RunEvent::Exit => core::stop_child(),
            _ => {}
        });
}
