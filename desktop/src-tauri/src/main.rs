//! OpenMyMemo for Mac: a window with the panel, a menu-bar item, and the Python core behind both.
//!
//! The window first shows the bundled setup page (../splash), then the panel from the local server at
//! `http://127.0.0.1:<port>/?desktop=1`, which switches the panel to its Mac layout. A monitor thread
//! keeps the server running, follows its port, puts the number of changes to review next to the
//! menu-bar icon, and notifies when an AI app changes something or something starts waiting.
//! Closing the window keeps the app in the menu bar; it quits from the menu. On every start it writes the
//! `openmymemo` launcher AI apps run (launcher.rs), and opened from its disk image it offers to move itself
//! to Applications (install.rs).

mod core;
mod install;
mod launcher;

use std::collections::HashSet;
use std::path::{Path, PathBuf};
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
use tauri_plugin_dialog::{DialogExt, MessageDialogButtons, MessageDialogKind};
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
    /// Set by Retry on the setup page: the monitor tries again now instead of after its back-off.
    retry: AtomicBool,
    /// The `openmymemo` launcher (launcher.rs), shown in Settings.
    launcher: PathBuf,
    /// The last status the setup page was given, so it is logged once rather than on every tick.
    last_status: Mutex<String>,
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

/// Tell the setup page what is happening. `kind`: "" while working, "error", or "offline" (only Retry helps).
fn splash_status(app: &AppHandle, status: &str, detail: &str, kind: &str) {
    let shared = app.state::<Arc<Shared>>();
    let line = format!("{status} {detail}");
    if *shared.last_status.lock().unwrap() != line {
        eprintln!("OpenMyMemo: {line}");
        *shared.last_status.lock().unwrap() = line;
    }
    if let Some(w) = main_window(app) {
        let _ = w.eval(format!(
            "window.__setStatus && window.__setStatus({status:?}, {detail:?}, {kind:?})"
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
    /// The `openmymemo` command for terminals and other MCP apps.
    launcher: PathBuf,
}

#[tauri::command]
fn app_settings(app: AppHandle, shared: tauri::State<Arc<Shared>>) -> AppSettings {
    AppSettings {
        vault: shared.settings.lock().unwrap().vault.clone(),
        autostart: app.autolaunch().is_enabled().unwrap_or(false),
        version: app.package_info().version.to_string(),
        launcher: shared.launcher.clone(),
    }
}

/// Retry on the setup page: start again now (the monitor otherwise backs off up to a minute).
#[tauri::command]
fn retry_start(shared: tauri::State<Arc<Shared>>) {
    shared.retry.store(true, Ordering::SeqCst);
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
    use_vault(&shared, picked.into_path().map_err(|e| e.to_string())?).map(|_| ())
}

/// Switch to `picked`, unless the server would refuse it: then nothing is saved, and the current folder
/// and its server stay. Returns whether anything changed.
fn use_vault(shared: &Shared, picked: PathBuf) -> Result<bool, String> {
    if picked == shared.settings.lock().unwrap().vault {
        return Ok(false);
    }
    core::check_folder(&picked)?;
    let settings = Settings { vault: picked };
    let dir = shared.settings_path.parent().ok_or("No settings folder")?;
    std::fs::create_dir_all(dir).map_err(|e| e.to_string())?;
    std::fs::write(&shared.settings_path, serde_json::to_string_pretty(&settings).unwrap()).map_err(|e| e.to_string())?;
    *shared.settings.lock().unwrap() = settings;
    shared.restart.store(true, Ordering::SeqCst);
    Ok(true)
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

// ---- moving to Applications -------------------------------------------------------------------------

/// Opened from its disk image or a translocated copy, offer to move the app to Applications. Returns true
/// when the app is quitting to reopen from there. Only when opened from the Finder (launchd is the parent):
/// running the binary from a terminal, as in development, is left alone.
fn offer_move(app: &AppHandle) -> bool {
    if unsafe { libc::getppid() } != 1 {
        return false;
    }
    let Some(bundle) = std::env::current_exe().ok().and_then(|exe| launcher::bundle_of(&exe)) else { return false };
    let Some(place) = install::transient(&bundle, install::is_read_only) else { return false };
    eprintln!("OpenMyMemo: running from {}; offering to move it to Applications", bundle.display());
    let ask = |title: &str, message: &str, ok: &str, cancel: &str| {
        app.dialog()
            .message(message)
            .title(title)
            .buttons(MessageDialogButtons::OkCancelCustom(ok.into(), cancel.into()))
            .blocking_show()
    };
    let tell = |title: &str, message: &str| {
        app.dialog().message(message).title(title).kind(MessageDialogKind::Warning).blocking_show();
    };
    if !ask(
        "Move OpenMyMemo to Applications?",
        "OpenMyMemo is running from its disk image. Move it to your Applications folder so it keeps working, and your AI apps can find it, after the disk image is gone.",
        "Move to Applications",
        "Not now",
    ) {
        return false;
    }
    let target_dir = Path::new("/Applications");
    let dst = target_dir.join(launcher::APP_NAME);
    let replace = std::fs::symlink_metadata(&dst).is_ok();
    if replace {
        let theirs = install::version_of(&dst).map(|v| format!(" {v}")).unwrap_or_default();
        let ours = app.package_info().version.to_string();
        if !ask(
            "Replace OpenMyMemo in Applications?",
            &format!("Applications already has OpenMyMemo{theirs}. Replace it with this copy ({ours})? If it is running, it quits first. Your memory folder isn't touched."),
            "Replace",
            "Cancel",
        ) {
            return false;
        }
    }
    let volume = match &place {
        install::Transient::DiskImage(volume) if install::is_disk_image(volume) => Some(volume.as_path()),
        _ => None,
    };
    let moved = (if replace { install::quit(&dst) } else { Ok(()) })
        .and_then(|_| install::copy_to(&bundle, target_dir, replace))
        .and_then(|dst| install::relaunch_after_exit(&dst, volume));
    match moved {
        Ok(()) => {
            app.exit(0);
            true
        }
        Err(e) => {
            tell("OpenMyMemo couldn't be moved", &format!("{e}\n\nYou can drag OpenMyMemo to Applications in the Finder instead."));
            false
        }
    }
}

fn monitor(app: AppHandle, paths: Paths) {
    if offer_move(&app) {
        return;
    }
    let shared = app.state::<Arc<Shared>>().inner().clone();
    let vault = shared.settings.lock().unwrap().vault.clone();
    let mut core = Core::new(vault, paths, core::PORTS);
    let mut last_counts: Option<Counts> = None;
    let mut seen: HashSet<String> = HashSet::new();
    let mut failures: u32 = 0;
    loop {
        if shared.restart.swap(false, Ordering::SeqCst) {
            core.set_vault(shared.settings.lock().unwrap().vault.clone());
            failures = 0;
            shared.port.store(0, Ordering::SeqCst);
            last_counts = None;
            seen.clear();
            let splash = shared.splash_url.lock().unwrap().clone();
            if let (Some(w), Some(url)) = (main_window(&app), splash) {
                let _ = w.navigate(url);
            }
            std::thread::sleep(Duration::from_millis(300));
        }

        if shared.retry.swap(false, Ordering::SeqCst) {
            failures = 0;
        }
        let on_splash = shared.port.load(Ordering::SeqCst) == 0;
        let status_app = app.clone();
        let status = move |s: &str, d: &str| splash_status(&status_app, s, d, "");
        match core.ensure(&status) {
            Ok((port, state)) => {
                failures = 0;
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
            Err(problem) => {
                failures += 1;
                if on_splash {
                    let kind = if problem.offline { "offline" } else { "error" };
                    splash_status(&app, &problem.title, &problem.detail, kind);
                }
            }
        }

        // Every 4 seconds; after failures, back off to a minute so a folder that can't work isn't retried
        // in a tight loop. Choosing another folder, or Retry on the setup page, starts again at once.
        let wait = if failures == 0 { 4 } else { (4u64 << failures.min(4)).min(60) };
        for _ in 0..wait * 4 {
            if shared.restart.load(Ordering::SeqCst) || shared.retry.load(Ordering::SeqCst) {
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

/// Write the launcher AI apps run. It looks for this app where it is now, then in the Applications folders.
fn write_launcher(app: &AppHandle, paths: &Paths, bundle: Option<&Path>) {
    let search = [Path::new("/Applications").join(launcher::APP_NAME), dirs_home().join("Applications").join(launcher::APP_NAME)];
    let version = app.package_info().version.to_string();
    let uv_env = core::uv_env(paths);
    let file = launcher::Launcher {
        version: &version,
        path: &paths.launcher,
        venv: &paths.venv(),
        uv_env: &uv_env,
        uv_args: &core::UV_ARGS,
        app: bundle,
        search: &search,
        mdfind: Path::new("/usr/bin/mdfind"),
    };
    if let Err(e) = file.write() {
        eprintln!("OpenMyMemo: {e}");
    }
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
        .invoke_handler(tauri::generate_handler![reveal_path, app_settings, set_autostart, choose_vault, retry_start])
        .menu(|app| app_menu(app))
        .on_menu_event(|app, event| on_menu(app, event.id().as_ref()))
        .setup(|app| {
            let handle = app.handle().clone();
            let path = app.path();
            let support = path.app_data_dir()?;
            let settings_path = support.join("settings.json");

            // The bundled uv sits next to the executable; the package source is a resource.
            let exe = std::env::current_exe()?;
            let exe_dir = exe.parent().map(PathBuf::from).unwrap_or_default();
            let paths = Paths {
                uv: exe_dir.join("uv"),
                project: path.resource_dir()?.join("core"),
                launcher: support.join("bin/openmymemo"),
                support,
                cache: path.app_cache_dir()?,
                log: path.app_log_dir()?.join("core.log"),
            };
            write_launcher(&handle, &paths, launcher::bundle_of(&exe).as_deref());

            let shared = Arc::new(Shared {
                settings: Mutex::new(load_settings(&settings_path)),
                settings_path,
                port: AtomicU16::new(0),
                restart: AtomicBool::new(false),
                retry: AtomicBool::new(false),
                launcher: paths.launcher.clone(),
                last_status: Mutex::new(String::new()),
                splash_url: Mutex::new(None),
                tray: Mutex::new(None),
                review_item: Mutex::new(None),
            });
            app.manage(shared);

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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn an_unusable_folder_is_refused_and_the_working_one_kept() {
        let root = std::env::temp_dir().join(format!("openmymemo-desktop-{}-choose", std::process::id()));
        let _ = std::fs::remove_dir_all(&root);
        let current = root.join("current");
        let busy = root.join("busy");
        std::fs::create_dir_all(&busy).unwrap();
        std::fs::write(busy.join("notes.txt"), "mine").unwrap();
        let shared = Shared {
            settings: Mutex::new(Settings { vault: current.clone() }),
            settings_path: root.join("support/settings.json"),
            port: AtomicU16::new(8787),
            restart: AtomicBool::new(false),
            retry: AtomicBool::new(false),
            launcher: root.join("support/bin/openmymemo"),
            last_status: Mutex::new(String::new()),
            splash_url: Mutex::new(None),
            tray: Mutex::new(None),
            review_item: Mutex::new(None),
        };

        let err = use_vault(&shared, busy).unwrap_err();
        assert!(err.contains("isn't empty"), "{err}");
        assert_eq!(shared.settings.lock().unwrap().vault, current);
        assert!(!shared.settings_path.exists() && !shared.restart.load(Ordering::SeqCst));

        // Recovery: an empty or new folder is accepted, saved, and the server restarts on it.
        let fresh = root.join("fresh");
        assert_eq!(use_vault(&shared, fresh.clone()), Ok(true));
        let saved: Settings = serde_json::from_str(&std::fs::read_to_string(&shared.settings_path).unwrap()).unwrap();
        assert_eq!(saved.vault, fresh);
        assert!(shared.restart.load(Ordering::SeqCst));
        assert_eq!(use_vault(&shared, fresh), Ok(false));
    }
}

