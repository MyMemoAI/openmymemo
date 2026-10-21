//! OpenMyMemo for Mac and Windows: a window with the panel, a menu-bar (or notification area) item, and the
//! Python core behind both.
//!
//! The window first shows the bundled setup page (../splash), then the panel from the local server at
//! `http://127.0.0.1:<port>/?desktop=1`, which switches the panel to its Mac layout. A monitor thread
//! keeps the server running, follows its port, puts the number of changes to review next to the
//! menu-bar icon, and notifies when an AI app changes something or something starts waiting.
//! Closing the window keeps the app in the menu bar; it quits from the menu. On every start it writes the
//! `openmymemo` launcher AI apps run (launcher.rs), and opened from its disk image it offers to move itself
//! to Applications (install.rs). On Windows it installs the launcher program instead (winlauncher.rs), and
//! the installer puts the app in place.

// A release build on Windows is a windowed program, with no console window of its own.
#![cfg_attr(all(windows, not(debug_assertions)), windows_subsystem = "windows")]

mod core;
#[cfg(not(windows))]
mod install;
#[cfg(not(windows))]
mod launcher;
mod updates;
#[cfg(windows)]
mod winlauncher;

use std::collections::HashSet;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, AtomicU16, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use serde::{Deserialize, Serialize};
use tauri::image::Image;
use tauri::menu::{MenuBuilder, MenuItem, MenuItemBuilder};
#[cfg(target_os = "macos")]
use tauri::menu::{Menu, SubmenuBuilder};
use tauri::tray::{TrayIcon, TrayIconBuilder};
use tauri::webview::{DownloadEvent, NewWindowResponse};
use tauri::{AppHandle, Manager, RunEvent, Url, WebviewUrl, WebviewWindow, WindowEvent};
#[cfg(target_os = "macos")]
use tauri::{LogicalPosition, TitleBarStyle};
use tauri_plugin_autostart::{MacosLauncher, ManagerExt as _};
use tauri_plugin_dialog::DialogExt;
#[cfg(not(windows))]
use tauri_plugin_dialog::{MessageDialogButtons, MessageDialogKind};
use tauri_plugin_notification::NotificationExt;

use crate::core::{Core, Counts, Paths, Proposal};

const HIDDEN_ARG: &str = "--hidden";

/// Light or dark, or as macOS is set.
#[derive(Clone, Copy, Debug, Default, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
enum Appearance {
    Light,
    Dark,
    /// Also what an unknown value (from a newer version) reads as.
    #[default]
    #[serde(other)]
    System,
}

impl Appearance {
    /// The app's appearance: windows, the panel in them (WebKit's prefers-color-scheme follows the window),
    /// sheets and menus. None follows the system.
    fn theme(self) -> Option<tauri::Theme> {
        match self {
            Appearance::System => None,
            Appearance::Light => Some(tauri::Theme::Light),
            Appearance::Dark => Some(tauri::Theme::Dark),
        }
    }
}

/// The app's own settings, in ~/Library/Application Support/<id>/settings.json. Keys added later have
/// defaults, so a file from an older version (only `vault`) still loads.
#[derive(Clone, Debug, PartialEq, Serialize)]
struct Settings {
    vault: PathBuf,
    appearance: Appearance,
    /// A notification when an AI app changes something or something starts waiting.
    notifications: bool,
    /// Look for a new version shortly after starting and once a day (updates.rs).
    check_updates: bool,
    /// Keys this version doesn't know (a newer one wrote them), written back as they were.
    #[serde(flatten)]
    other: serde_json::Map<String, serde_json::Value>,
}

impl Default for Settings {
    fn default() -> Self {
        Settings {
            vault: default_vault(),
            appearance: Appearance::System,
            notifications: true,
            check_updates: true,
            other: Default::default(),
        }
    }
}

/// Key by key: a value of the wrong type loses only that setting, never the memory folder with it.
fn parse_settings(text: &str) -> Settings {
    let mut settings = Settings::default();
    let Ok(serde_json::Value::Object(mut map)) = serde_json::from_str(text) else { return settings };
    if let Some(vault) = map.remove("vault").as_ref().and_then(|v| v.as_str()).filter(|v| !v.is_empty()) {
        settings.vault = PathBuf::from(vault);
    }
    if let Some(appearance) = map.remove("appearance").and_then(|v| serde_json::from_value(v).ok()) {
        settings.appearance = appearance;
    }
    if let Some(notifications) = map.remove("notifications").and_then(|v| v.as_bool()) {
        settings.notifications = notifications;
    }
    if let Some(check_updates) = map.remove("check_updates").and_then(|v| v.as_bool()) {
        settings.check_updates = check_updates;
    }
    settings.other = map;
    settings
}

struct Shared {
    /// As saved in settings.json.
    settings: Mutex<Settings>,
    settings_path: PathBuf,
    /// The memory folder in use: the saved one, or OPENMYMEMO_HOME (as for the CLI) in development.
    vault: Mutex<PathBuf>,
    /// ~/Library/Logs/<id>/core.log, which Settings → Help reveals.
    log: PathBuf,
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
    /// "Check for Updates…" in the menu-bar item's menu, "Install OpenMyMemo <version>…" once one is found.
    update_item: Mutex<Option<MenuItem<tauri::Wry>>>,
    /// The update found last, until it is installed.
    update: Mutex<Option<tauri_plugin_updater::Update>>,
    /// Set by "Check for Updates…": the updates thread checks now and says what it found.
    check_now: AtomicBool,
    /// Set while an update downloads and installs.
    installing: AtomicBool,
}

fn default_vault() -> PathBuf {
    dirs_home().join("OpenMyMemo")
}

fn dirs_home() -> PathBuf {
    let var = if cfg!(windows) { "USERPROFILE" } else { "HOME" };
    std::env::var_os(var).map(PathBuf::from).unwrap_or_else(|| PathBuf::from("/"))
}

fn load_settings(path: &Path) -> Settings {
    std::fs::read_to_string(path).map(|text| parse_settings(&text)).unwrap_or_default()
}

impl Shared {
    fn new(settings_path: PathBuf, launcher: PathBuf, log: PathBuf) -> Self {
        let settings = load_settings(&settings_path);
        // OPENMYMEMO_HOME (as for the CLI) overrides the saved folder, for development; it isn't saved.
        let vault = std::env::var_os("OPENMYMEMO_HOME").filter(|v| !v.is_empty()).map(PathBuf::from);
        Shared {
            vault: Mutex::new(vault.unwrap_or_else(|| settings.vault.clone())),
            settings: Mutex::new(settings),
            settings_path,
            log,
            port: AtomicU16::new(0),
            restart: AtomicBool::new(false),
            retry: AtomicBool::new(false),
            launcher,
            last_status: Mutex::new(String::new()),
            splash_url: Mutex::new(None),
            tray: Mutex::new(None),
            review_item: Mutex::new(None),
            update_item: Mutex::new(None),
            update: Mutex::new(None),
            check_now: AtomicBool::new(false),
            installing: AtomicBool::new(false),
        }
    }

    fn vault(&self) -> PathBuf {
        self.vault.lock().unwrap().clone()
    }

    /// Change the saved settings and write them to settings.json (a temporary file renamed over it, so a
    /// crash never leaves half a file); nothing changes if writing fails.
    fn save(&self, change: impl FnOnce(&mut Settings)) -> Result<(), String> {
        let mut settings = self.settings.lock().unwrap();
        let mut next = settings.clone();
        change(&mut next);
        let dir = self.settings_path.parent().ok_or("No settings folder")?;
        std::fs::create_dir_all(dir).map_err(|e| e.to_string())?;
        let tmp = dir.join(format!(".settings.json.{}.tmp", std::process::id()));
        std::fs::write(&tmp, serde_json::to_string_pretty(&next).unwrap())
            .and_then(|_| std::fs::rename(&tmp, &self.settings_path))
            .map_err(|e| {
                let _ = std::fs::remove_file(&tmp);
                e.to_string()
            })?;
        *settings = next;
        Ok(())
    }
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
    reveal(&to_reveal(Path::new(&path))?)
}

/// Show `path` selected in the Finder (`open -R`) or File Explorer (`/select,`), never opening it.
fn reveal(path: &Path) -> Result<(), String> {
    #[cfg(not(windows))]
    let shown = std::process::Command::new("/usr/bin/open").arg("-R").arg(path).status();
    // Explorer takes `/select,"<path>"` as one argument, quoted its own way; it exits with 1 even when it worked.
    #[cfg(windows)]
    let shown = {
        use std::os::windows::process::CommandExt;
        std::process::Command::new(windows_program("explorer.exe")).raw_arg(format!("/select,\"{}\"", path.display())).status()
    };
    shown.map(|_| ()).map_err(|e| e.to_string())
}

/// A program in the Windows folder, by its full path so a file of the same name elsewhere is never run.
#[cfg(windows)]
fn windows_program(name: &str) -> PathBuf {
    std::env::var_os("SystemRoot").map(PathBuf::from).unwrap_or_else(|| PathBuf::from(r"C:\Windows")).join(name)
}

/// What `open -R` selects in the Finder for `path`: the item itself, or for a file not written yet (the log
/// before the app has started a server) its folder. Always -R, which shows and never opens, so a path that
/// is an app or a document can't be launched from the panel.
fn to_reveal(path: &Path) -> Result<PathBuf, String> {
    if path.exists() {
        return Ok(path.to_path_buf());
    }
    match path.parent().filter(|p| p.is_dir()) {
        Some(folder) => Ok(folder.to_path_buf()),
        None => Err(format!("{} doesn't exist yet.", path.display())),
    }
}

#[derive(Serialize)]
struct AppSettings {
    vault: PathBuf,
    autostart: bool,
    appearance: Appearance,
    notifications: bool,
    check_updates: bool,
    version: String,
    /// The `openmymemo` command for terminals and other MCP apps.
    launcher: PathBuf,
    /// The server's log.
    log: PathBuf,
}

#[tauri::command]
fn app_settings(app: AppHandle, shared: tauri::State<Arc<Shared>>) -> AppSettings {
    let settings = shared.settings.lock().unwrap().clone();
    AppSettings {
        vault: shared.vault(),
        autostart: app.autolaunch().is_enabled().unwrap_or(false),
        appearance: settings.appearance,
        notifications: settings.notifications,
        check_updates: settings.check_updates,
        version: app.package_info().version.to_string(),
        launcher: shared.launcher.clone(),
        log: shared.log.clone(),
    }
}

#[tauri::command]
fn set_appearance(app: AppHandle, shared: tauri::State<Arc<Shared>>, appearance: Appearance) -> Result<(), String> {
    shared.save(|s| s.appearance = appearance)?;
    app.set_theme(appearance.theme());
    Ok(())
}

#[tauri::command]
fn set_notifications(shared: tauri::State<Arc<Shared>>, enabled: bool) -> Result<(), String> {
    shared.save(|s| s.notifications = enabled)
}

#[tauri::command]
fn set_check_updates(shared: tauri::State<Arc<Shared>>, enabled: bool) -> Result<(), String> {
    shared.save(|s| s.check_updates = enabled)
}

/// "Check Now" in Settings, like "Check for Updates…" in the menus: the answer comes as a dialog.
#[tauri::command]
fn check_for_updates(shared: tauri::State<Arc<Shared>>) {
    shared.check_now.store(true, Ordering::SeqCst);
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
    let current = shared.vault();
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
    if picked == shared.vault() {
        return Ok(false);
    }
    core::check_folder(&picked)?;
    shared.save(|s| s.vault = picked.clone())?;
    *shared.vault.lock().unwrap() = picked;
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
    let wanted = app.state::<Arc<Shared>>().settings.lock().unwrap().notifications;
    if first || !wanted || (new_applied.is_empty() && new_waiting.is_empty()) {
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

/// On Windows the installer puts the app in place.
#[cfg(windows)]
fn offer_move(_app: &AppHandle) -> bool {
    false
}

/// Opened from its disk image or a translocated copy, offer to move the app to Applications. Returns true
/// when the app is quitting to reopen from there. Only when macOS opened it as an app (the Finder, the Dock,
/// `open`), which says so in __CFBundleIdentifier: running the binary from a terminal is left alone.
#[cfg(not(windows))]
fn offer_move(app: &AppHandle) -> bool {
    if std::env::var_os("__CFBundleIdentifier").map_or(true, |id| id != launcher::BUNDLE_ID) {
        return false;
    }
    let Some(bundle) = std::env::current_exe().ok().and_then(|exe| launcher::bundle_of(&exe)) else { return false };
    let Some(place) = install::transient(&bundle, install::is_read_only) else { return false };
    eprintln!("OpenMyMemo: running from {}; offering to move it to Applications", bundle.display());
    // As sheets on the window: without a parent the dialog plugin asks the system to show the alert
    // (CFUserNotification), which can fail without showing anything.
    let window = main_window(app);
    let dialog = |title: &str, message: &str| {
        let builder = app.dialog().message(message).title(title);
        match &window {
            Some(w) => builder.parent(w),
            None => builder,
        }
    };
    let ask = |title: &str, message: &str, ok: &str, cancel: &str| {
        dialog(title, message).buttons(MessageDialogButtons::OkCancelCustom(ok.into(), cancel.into())).blocking_show()
    };
    let tell = |title: &str, message: &str| {
        dialog(title, message).kind(MessageDialogKind::Warning).blocking_show();
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
    install::sweep(target_dir);
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
    let mut core = Core::new(shared.vault(), paths, core::PORTS);
    let mut last_counts: Option<Counts> = None;
    let mut seen: HashSet<String> = HashSet::new();
    let mut failures: u32 = 0;
    loop {
        if shared.restart.swap(false, Ordering::SeqCst) {
            core.set_vault(shared.vault());
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

// ---- updates ----------------------------------------------------------------------------------------

/// Where to look for the update in the menu-bar item's menu.
#[cfg(not(windows))]
const MENU_PLACE: &str = "the menu bar";
#[cfg(windows)]
const MENU_PLACE: &str = "the notification area";

fn set_update_item(app: &AppHandle, label: &str, enabled: bool) {
    let item = app.state::<Arc<Shared>>().update_item.lock().unwrap().clone();
    if let Some(item) = item {
        let _ = item.set_text(label);
        let _ = item.set_enabled(enabled);
    }
}

fn dialog_on_window(app: &AppHandle, title: &str, message: &str) -> tauri_plugin_dialog::MessageDialogBuilder<tauri::Wry> {
    let builder = app.dialog().message(message).title(title);
    match main_window(app).filter(|w| w.is_visible().unwrap_or(false)) {
        Some(w) => builder.parent(&w),
        None => builder,
    }
}

/// Ask whether to install the update found, and install it if so. Runs on a thread of its own.
fn ask_install(app: &AppHandle) {
    let shared = app.state::<Arc<Shared>>().inner().clone();
    let Some(update) = shared.update.lock().unwrap().clone() else { return };
    if shared.installing.swap(true, Ordering::SeqCst) {
        return;
    }
    let notes = updates::summary(update.body.as_deref().unwrap_or(""), 700);
    let message = format!(
        "You have {}.{}\n\nInstall it now? OpenMyMemo restarts; your memory folder and the AI apps you connected aren't touched.",
        update.current_version,
        if notes.is_empty() { String::new() } else { format!(" What's new:\n\n{notes}") }
    );
    use tauri_plugin_dialog::{MessageDialogButtons, MessageDialogKind};
    let yes = dialog_on_window(app, &format!("OpenMyMemo {} is available", update.version), &message)
        .buttons(MessageDialogButtons::OkCancelCustom("Install and Restart".into(), "Later".into()))
        .blocking_show();
    if yes {
        set_update_item(app, "Downloading the update…", false);
        let problem = updates::install(app, &update);
        eprintln!("OpenMyMemo: the update didn't install: {problem}");
        set_update_item(app, &format!("Install OpenMyMemo {}…", update.version), true);
        let open = dialog_on_window(
            app,
            "OpenMyMemo couldn't be updated",
            &format!("{problem}\n\nYou can download OpenMyMemo {} from its release page instead.", update.version),
        )
        .kind(MessageDialogKind::Warning)
        .buttons(MessageDialogButtons::OkCancelCustom("Open Release Page".into(), "Close".into()))
        .blocking_show();
        if open {
            if let Ok(url) = Url::parse(updates::RELEASES) {
                open_outside(&url);
            }
        }
    }
    shared.installing.store(false, Ordering::SeqCst);
}

/// Check shortly after starting and then daily while "Check for updates automatically" is on, and whenever
/// asked. An update found is offered once per version: in a dialog when the window is open, else with a
/// notification, and it stays in the menu-bar item's menu.
fn check_updates_loop(app: AppHandle, automatic: bool) {
    let shared = app.state::<Arc<Shared>>().inner().clone();
    let version = app.package_info().version.to_string();
    let mut next = Instant::now() + updates::FIRST_CHECK;
    let mut offered: HashSet<String> = HashSet::new();
    loop {
        let asked = shared.check_now.swap(false, Ordering::SeqCst);
        let due = automatic && Instant::now() >= next && shared.settings.lock().unwrap().check_updates;
        if (asked || due) && !shared.installing.load(Ordering::SeqCst) {
            next = Instant::now() + updates::EVERY;
            use tauri_plugin_dialog::MessageDialogKind;
            match updates::check(&app) {
                updates::Check::Available(update) => {
                    let new = offered.insert(update.version.clone());
                    set_update_item(&app, &format!("Install OpenMyMemo {}…", update.version), true);
                    let found = update.version.clone();
                    *shared.update.lock().unwrap() = Some(*update);
                    let window_open = main_window(&app).is_some_and(|w| w.is_visible().unwrap_or(false));
                    if asked || (new && window_open) {
                        ask_install(&app);
                    } else if new {
                        let _ = app
                            .notification()
                            .builder()
                            .title(format!("OpenMyMemo {found} is available"))
                            .body(format!("Install it from OpenMyMemo's menu in {MENU_PLACE}."))
                            .show();
                    }
                }
                updates::Check::Current => {
                    if asked {
                        dialog_on_window(&app, "OpenMyMemo is up to date", &format!("{version} is the latest version."))
                            .blocking_show();
                    }
                }
                updates::Check::Failed(e) => {
                    eprintln!("OpenMyMemo: couldn't check for updates: {e}");
                    if asked {
                        dialog_on_window(&app, "OpenMyMemo couldn't check for updates", &format!("{e}\n\nCheck your internet connection and try again."))
                            .kind(MessageDialogKind::Warning)
                            .blocking_show();
                    }
                }
            }
        }
        std::thread::sleep(Duration::from_millis(500));
    }
}

/// "Check for Updates…", or "Install OpenMyMemo <version>…" once an update was found.
fn on_update_item(app: &AppHandle) {
    let shared = app.state::<Arc<Shared>>();
    if shared.update.lock().unwrap().is_some() {
        let handle = app.clone();
        std::thread::spawn(move || ask_install(&handle));
    } else {
        shared.check_now.store(true, Ordering::SeqCst);
    }
}

// ---- menus ------------------------------------------------------------------------------------------

#[cfg(target_os = "macos")]
fn app_menu(app: &AppHandle) -> tauri::Result<Menu<tauri::Wry>> {
    let item = |id: &str, label: &str, accel: &str| {
        MenuItemBuilder::with_id(id, label).accelerator(accel).build(app)
    };
    let app_sub = SubmenuBuilder::new(app, "OpenMyMemo")
        .about(None)
        .item(&MenuItemBuilder::with_id("update", "Check for Updates…").build(app)?)
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
            if let Ok(url) = Url::parse("https://github.com/MyMemoAI/openmymemo") {
                open_outside(&url);
            }
        }
        "open" => show_window(app),
        "review" => open_panel_at(app, "#/changes"),
        "update" => on_update_item(app),
        "quit" => app.exit(0),
        _ => {}
    }
}

fn build_tray(app: &AppHandle) -> tauri::Result<()> {
    let review = MenuItemBuilder::with_id("review", "Nothing to review").enabled(false).build(app)?;
    let update = MenuItemBuilder::with_id("update", "Check for Updates…").build(app)?;
    let menu = MenuBuilder::new(app)
        .text("open", "Open OpenMyMemo")
        .item(&review)
        .separator()
        .item(&update)
        .separator()
        .text("quit", "Quit OpenMyMemo")
        .build()?;
    // A template image on a Mac, which the menu bar tints; the app icon in the Windows notification area.
    #[cfg(not(windows))]
    let icon = Image::from_bytes(include_bytes!("../icons/tray.png"))?;
    #[cfg(windows)]
    let icon = Image::from_bytes(include_bytes!("../icons/32x32.png"))?;
    let tray = TrayIconBuilder::with_id("main")
        .icon(icon)
        .icon_as_template(cfg!(not(windows)))
        .tooltip("OpenMyMemo")
        .menu(&menu)
        .show_menu_on_left_click(true)
        .on_menu_event(|app, event| on_menu(app, event.id().as_ref()))
        .build(app)?;
    let shared = app.state::<Arc<Shared>>();
    *shared.tray.lock().unwrap() = Some(tray);
    *shared.review_item.lock().unwrap() = Some(review);
    *shared.update_item.lock().unwrap() = Some(update);
    Ok(())
}

// ---- the window -------------------------------------------------------------------------------------

/// The install links of the AI apps the Connectors page knows (clients.py), which macOS hands to those apps.
const APP_SCHEMES: [&str; 7] = ["cursor", "vscode", "vscode-insiders", "lmstudio", "kiro", "trae", "cherrystudio"];

#[derive(Debug, PartialEq)]
enum Link {
    /// Load in the window: the setup page, or the panel on its current port.
    Load,
    /// Hand to macOS: web and mail links, and the AI apps' install links.
    Open,
    /// Everything else, such as file:, data: and javascript: URLs.
    Block,
}

/// Links that may leave the app, to the browser, Mail or an AI app.
fn opens_outside(url: &Url) -> bool {
    matches!(url.scheme(), "http" | "https" | "mailto") || APP_SCHEMES.contains(&url.scheme())
}

/// Where a link the window navigates to goes. `splash` is the setup page's address and `port` the panel's.
/// The setup page is matched by scheme, host and port: tauri:// isn't a special scheme to the url crate,
/// so each of its URLs has its own opaque origin and origins never compare equal.
fn link(url: &Url, splash: Option<&Url>, port: u16) -> Link {
    let same_place = |a: &Url| {
        a.scheme() == url.scheme() && a.host_str() == url.host_str() && a.port_or_known_default() == url.port_or_known_default()
    };
    if splash.map_or(true, same_place) || url.scheme() == "about" {
        return Link::Load;
    }
    if url.scheme() == "http" && url.host_str() == Some("127.0.0.1") && url.port() == Some(port) {
        return Link::Load;
    }
    if opens_outside(url) { Link::Open } else { Link::Block }
}

/// Hand a link to the system: the browser, the mail app, or the AI app that installs it. Only links that
/// `opens_outside` allows get here, so File Explorer never gets a path or a program to start.
fn open_outside(url: &Url) {
    #[cfg(not(windows))]
    let program = PathBuf::from("/usr/bin/open");
    #[cfg(windows)]
    let program = windows_program("explorer.exe");
    let _ = std::process::Command::new(program).arg(url.as_str()).stdin(std::process::Stdio::null()).spawn();
}

/// Only the setup page and the panel load in the window; links out open in the browser or the AI app.
fn allow_navigation(shared: &Shared, url: &Url) -> bool {
    let splash = shared.splash_url.lock().unwrap().clone();
    match link(url, splash.as_ref(), shared.port.load(Ordering::SeqCst)) {
        Link::Load => true,
        Link::Open => {
            open_outside(url);
            false
        }
        Link::Block => false,
    }
}

/// window.open and target="_blank" never open a second window (WebKit asks on_navigation first for a
/// target="_blank" link, and only window.open, or a link that loads in the window, gets here).
fn on_new_window(url: &Url) -> NewWindowResponse<tauri::Wry> {
    if opens_outside(url) {
        open_outside(url);
    }
    NewWindowResponse::Deny
}

fn build_window(app: &AppHandle, visible: bool) -> tauri::Result<WebviewWindow> {
    let shared = app.state::<Arc<Shared>>().inner().clone();
    let theme = shared.settings.lock().unwrap().appearance.theme();
    let builder = tauri::WebviewWindowBuilder::new(app, "main", WebviewUrl::App("index.html".into()))
        .title("OpenMyMemo")
        // Applied as the window is created, before it shows, so a chosen appearance doesn't flash.
        .theme(theme)
        .inner_size(1280.0, 860.0)
        .min_inner_size(900.0, 560.0);
    #[cfg(target_os = "macos")]
    let builder = builder
        .title_bar_style(TitleBarStyle::Overlay)
        .hidden_title(true)
        // tao sizes the title bar to the buttons' height + y and keeps their bottom offset, so y = 28 puts
        // their centre at 26 pt, the middle of the 52 pt toolbar and sidebar strip.
        .traffic_light_position(LogicalPosition::new(20.0, 28.0));
    let window = builder
        .visible(visible)
        .on_navigation(move |url| allow_navigation(&shared, url))
        .on_new_window(|url, _| on_new_window(&url))
        // Export → Download: saved to Downloads under the suggested name, then shown in the Finder or Explorer.
        .on_download(|_, event| {
            if let DownloadEvent::Finished { path: Some(path), success: true, .. } = event {
                let _ = reveal(&path);
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

/// Install the launcher program AI apps run, with how to set up this build when no environment is ready.
#[cfg(windows)]
fn write_launcher(paths: &Paths, exe_dir: &Path) {
    let written = paths.build_env().and_then(|(_, environment)| {
        let uv_env = core::uv_env(paths);
        winlauncher::Launcher {
            path: &paths.launcher,
            program: &exe_dir.join("openmymemo-launcher.exe"),
            uv: &paths.uv,
            project: &paths.project,
            environment: &environment,
            uv_env: &uv_env,
            uv_args: &core::UV_ARGS,
        }
        .write()
    });
    if let Err(e) = written {
        eprintln!("OpenMyMemo: {e}");
    }
}

/// Write the launcher AI apps run. It looks for this app where it is now, then in the Applications folders.
#[cfg(not(windows))]
fn write_launcher(app: &AppHandle, paths: &Paths, bundle: Option<&Path>) {
    let search = [Path::new("/Applications").join(launcher::APP_NAME), dirs_home().join("Applications").join(launcher::APP_NAME)];
    let version = app.package_info().version.to_string();
    let uv_env = core::uv_env(paths);
    let file = launcher::Launcher {
        version: &version,
        path: &paths.launcher,
        support: &paths.support,
        uv_env: &uv_env,
        uv_args: &core::UV_ARGS,
        app: bundle,
        app_transient: bundle.is_some_and(|b| install::transient(b, install::is_read_only).is_some()),
        search: &search,
        mdfind: Path::new("/usr/bin/mdfind"),
    };
    if let Err(e) = file.write() {
        eprintln!("OpenMyMemo: {e}");
    }
}

/// A quit by signal (kill, Ctrl-C in a terminal) skips RunEvent::Exit; stop the server there too.
#[cfg(unix)]
extern "C" fn on_signal(sig: libc::c_int) {
    core::stop_child();
    unsafe {
        libc::signal(sig, libc::SIG_DFL);
        libc::raise(sig);
    }
}

fn main() {
    #[cfg(unix)]
    for sig in [libc::SIGTERM, libc::SIGINT, libc::SIGHUP] {
        unsafe {
            libc::signal(sig, on_signal as extern "C" fn(libc::c_int) as libc::sighandler_t);
        }
    }
    let builder = tauri::Builder::default();
    // macOS opens an app once; on Windows, starting it again shows the window of the one already running.
    #[cfg(windows)]
    let builder = builder.plugin(tauri_plugin_single_instance::init(|app, _, _| show_window(app)));
    let builder = builder
        .plugin(tauri_plugin_notification::init())
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_updater::Builder::new().build())
        .plugin(tauri_plugin_autostart::init(MacosLauncher::LaunchAgent, Some(vec![HIDDEN_ARG])))
        .invoke_handler(tauri::generate_handler![
            reveal_path,
            app_settings,
            set_autostart,
            set_appearance,
            set_notifications,
            set_check_updates,
            check_for_updates,
            choose_vault,
            retry_start
        ]);
    // The Mac menu bar. On Windows the window has no menu bar: the panel is the app, and its shortcuts work
    // without one.
    #[cfg(target_os = "macos")]
    let builder = builder.menu(|app| app_menu(app)).on_menu_event(|app, event| on_menu(app, event.id().as_ref()));
    builder
        .setup(|app| {
            let handle = app.handle().clone();
            let path = app.path();
            // Windows: everything in Local AppData, which isn't copied between computers with a roaming
            // profile, and uv's cache in a folder of its own there (the app's cache folder is the same folder).
            #[cfg(windows)]
            let (support, cache) = {
                let local = path.app_local_data_dir()?;
                (local.clone(), local.join("cache"))
            };
            #[cfg(not(windows))]
            let (support, cache) = (path.app_data_dir()?, path.app_cache_dir()?);
            let settings_path = support.join("settings.json");

            // The bundled uv sits next to the executable; the package source is a resource.
            let exe = std::env::current_exe()?;
            let exe_dir = exe.parent().map(PathBuf::from).unwrap_or_default();
            let paths = Paths {
                uv: exe_dir.join(format!("uv{}", std::env::consts::EXE_SUFFIX)),
                project: path.resource_dir()?.join("core"),
                #[cfg(windows)]
                launcher: winlauncher::path(&support),
                #[cfg(not(windows))]
                launcher: support.join("bin/openmymemo"),
                support,
                cache,
                log: path.app_log_dir()?.join("core.log"),
            };
            #[cfg(windows)]
            write_launcher(&paths, &exe_dir);
            #[cfg(not(windows))]
            write_launcher(&handle, &paths, launcher::bundle_of(&exe).as_deref());

            let shared = Arc::new(Shared::new(settings_path, paths.launcher.clone(), paths.log.clone()));
            // The whole app: menus, the menu-bar item's menu and dialogs too (the window also gets it as
            // it is created, build_window).
            handle.set_theme(shared.settings.lock().unwrap().appearance.theme());
            app.manage(shared);

            let hidden = std::env::args().any(|a| a == HIDDEN_ARG);
            build_window(&handle, !hidden)?;
            build_tray(&handle)?;
            // Automatic checks only in a release build that can replace itself: not in development, and on a
            // Mac not from the disk image or a translocated copy (offer_move moves it first).
            #[cfg(not(windows))]
            let replaceable = launcher::bundle_of(&exe).is_some_and(|b| install::transient(&b, install::is_read_only).is_none());
            #[cfg(windows)]
            let replaceable = true;
            let automatic = replaceable && !cfg!(debug_assertions);
            let updates_handle = handle.clone();
            std::thread::Builder::new()
                .name("openmymemo-updates".into())
                .spawn(move || check_updates_loop(updates_handle, automatic))?;
            std::thread::Builder::new().name("openmymemo-monitor".into()).spawn(move || monitor(handle, paths))?;
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building OpenMyMemo")
        .run(|_app, event| match event {
            // Clicking the Dock icon brings the window back.
            #[cfg(target_os = "macos")]
            RunEvent::Reopen { .. } => show_window(_app),
            RunEvent::Exit => core::stop_child(),
            _ => {}
        });
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn reveals_without_opening() {
        let root = std::env::temp_dir().join(format!("openmymemo-desktop-{}-reveal", std::process::id()));
        let _ = std::fs::remove_dir_all(&root);
        let app = root.join("Tools/Some.app");
        std::fs::create_dir_all(app.join("Contents")).unwrap();
        assert_eq!(to_reveal(&app), Ok(app.clone()), "an app is selected, not launched");
        assert_eq!(to_reveal(&root.join("Logs/core.log")), Err(format!("{} doesn't exist yet.", root.join("Logs/core.log").display())));
        assert_eq!(to_reveal(&root.join("Tools/core.log")), Ok(root.join("Tools")));
    }

    #[test]
    fn links_load_open_outside_or_are_blocked() {
        let splash = Url::parse("tauri://localhost/index.html").unwrap();
        let at = |url: &str| link(&Url::parse(url).unwrap(), Some(&splash), 8787);
        assert_eq!(at("tauri://localhost/index.html"), Link::Load, "back to the setup page");
        assert_eq!(at("tauri://localhost/"), Link::Load);
        assert_eq!(at("about:blank"), Link::Load);
        assert_eq!(at("http://127.0.0.1:8787/?desktop=1#/connectors"), Link::Load);
        // Another local server, the web, mail, and the AI apps' install links leave the app.
        assert_eq!(at("http://127.0.0.1:8790/"), Link::Open);
        assert_eq!(at("https://docs.cursor.com/context/mcp"), Link::Open);
        assert_eq!(at("mailto:security@mymemo.ai"), Link::Open);
        for install in [
            "cursor://anysphere.cursor-deeplink/mcp/install?name=openmymemo&config=e30=",
            "vscode:mcp/install?%7B%22name%22%3A%22openmymemo%22%7D",
            "vscode-insiders:mcp/install?%7B%7D",
            "lmstudio://add_mcp?name=openmymemo&config=e30=",
            "kiro://kiro.mcp/add?name=openmymemo&config=%7B%7D",
            "trae://trae.ai-ide/mcp-import?source=openmymemo&config=e30=",
            "cherrystudio://mcp/install?servers=e30=",
        ] {
            assert_eq!(at(install), Link::Open, "{install}");
        }
        for blocked in [
            "file:///etc/passwd",
            "javascript:alert(1)",
            "data:text/html,<p>hi</p>",
            "ftp://example.com/",
            "tauri://elsewhere/index.html",
            "x-unknown-app://do-something",
            "ms-settings:privacy",
        ] {
            assert_eq!(at(blocked), Link::Block, "{blocked}");
        }
        // New windows open nothing in the app; only links that may leave it are handed to macOS.
        assert!(opens_outside(&Url::parse("https://github.com/MyMemoAI/openmymemo").unwrap()));
        assert!(!opens_outside(&Url::parse("about:blank").unwrap()));
        assert!(!opens_outside(&Url::parse("file:///Users").unwrap()));
    }

    #[test]
    fn settings_from_older_versions_load_with_defaults() {
        let old = parse_settings(r#"{ "vault": "/Users/me/Memory" }"#);
        assert_eq!(old, Settings { vault: PathBuf::from("/Users/me/Memory"), ..Settings::default() });
        let set = parse_settings(r#"{ "vault": "/m", "appearance": "dark", "notifications": false, "check_updates": false }"#);
        assert_eq!((set.appearance, set.notifications, set.check_updates), (Appearance::Dark, false, false));
        assert!(old.check_updates, "on unless turned off");
        // A value from a newer version doesn't lose the folder.
        let newer = parse_settings(r#"{ "vault": "/m", "appearance": "sepia", "zoom": 2 }"#);
        assert_eq!((newer.vault, newer.appearance), (PathBuf::from("/m"), Appearance::System));
        assert_eq!(parse_settings(r#"["not", "settings"]"#), Settings::default());
        assert_eq!(serde_json::to_value(Appearance::Light).unwrap(), "light");
        assert_eq!(Appearance::Dark.theme(), Some(tauri::Theme::Dark));
        assert_eq!(Appearance::System.theme(), None);

        let root = std::env::temp_dir().join(format!("openmymemo-desktop-{}-settings", std::process::id()));
        let _ = std::fs::remove_dir_all(&root);
        assert_eq!(load_settings(&root.join("missing.json")), Settings::default());
        std::fs::create_dir_all(&root).unwrap();
        std::fs::write(root.join("broken.json"), "{ not json").unwrap();
        assert_eq!(load_settings(&root.join("broken.json")).vault, default_vault());
    }

    #[test]
    fn a_mistyped_value_loses_only_itself_and_unknown_keys_are_kept() {
        let root = std::env::temp_dir().join(format!("openmymemo-desktop-{}-typo", std::process::id()));
        let _ = std::fs::remove_dir_all(&root);
        std::fs::create_dir_all(&root).unwrap();
        let path = root.join("settings.json");
        std::fs::write(&path, r#"{ "vault": "/Users/me/Memory", "notifications": "off", "appearance": { "x": 1 }, "zoom": 2 }"#).unwrap();
        let loaded = load_settings(&path);
        assert_eq!(loaded.vault, PathBuf::from("/Users/me/Memory"));
        assert_eq!((loaded.appearance, loaded.notifications), (Appearance::System, true));

        let shared = Shared::new(path.clone(), root.join("bin/openmymemo"), root.join("core.log"));
        shared.save(|s| s.appearance = Appearance::Light).unwrap();
        let saved: serde_json::Value = serde_json::from_str(&std::fs::read_to_string(&path).unwrap()).unwrap();
        assert_eq!(saved["vault"], "/Users/me/Memory");
        assert_eq!(saved["appearance"], "light");
        assert_eq!(saved["zoom"], 2, "a key from a newer version survives");
        let names: Vec<_> = std::fs::read_dir(&root).unwrap().flatten().map(|e| e.file_name()).collect();
        assert_eq!(names, vec![std::ffi::OsString::from("settings.json")], "no temporary file left");
    }

    #[test]
    fn saving_a_setting_keeps_the_others_and_never_saves_a_development_folder() {
        let root = std::env::temp_dir().join(format!("openmymemo-desktop-{}-save", std::process::id()));
        let _ = std::fs::remove_dir_all(&root);
        std::fs::create_dir_all(&root).unwrap();
        let path = root.join("settings.json");
        std::fs::write(&path, r#"{ "vault": "/Users/me/Memory" }"#).unwrap();
        let shared = Shared::new(path.clone(), root.join("bin/openmymemo"), root.join("core.log"));
        // As OPENMYMEMO_HOME does.
        *shared.vault.lock().unwrap() = root.join("dev-vault");

        shared.save(|s| s.appearance = Appearance::Dark).unwrap();
        shared.save(|s| s.notifications = false).unwrap();
        let saved: serde_json::Value = serde_json::from_str(&std::fs::read_to_string(&path).unwrap()).unwrap();
        assert_eq!(
            saved,
            serde_json::json!({ "vault": "/Users/me/Memory", "appearance": "dark", "notifications": false, "check_updates": true })
        );
        assert_eq!(shared.vault(), root.join("dev-vault"));
    }

    #[test]
    fn an_unusable_folder_is_refused_and_the_working_one_kept() {
        let root = std::env::temp_dir().join(format!("openmymemo-desktop-{}-choose", std::process::id()));
        let _ = std::fs::remove_dir_all(&root);
        let current = root.join("current");
        let busy = root.join("busy");
        std::fs::create_dir_all(&busy).unwrap();
        std::fs::write(busy.join("notes.txt"), "mine").unwrap();
        let shared = Shared::new(root.join("support/settings.json"), root.join("bin/openmymemo"), root.join("core.log"));
        *shared.vault.lock().unwrap() = current.clone();

        let err = use_vault(&shared, busy).unwrap_err();
        assert!(err.contains("isn't empty"), "{err}");
        assert_eq!(shared.vault(), current);
        assert!(!shared.settings_path.exists() && !shared.restart.load(Ordering::SeqCst));

        // Recovery: an empty or new folder is accepted, saved, and the server restarts on it.
        let fresh = root.join("fresh");
        assert_eq!(use_vault(&shared, fresh.clone()), Ok(true));
        let saved = load_settings(&shared.settings_path);
        assert_eq!(saved.vault, fresh);
        assert!(shared.restart.load(Ordering::SeqCst));
        assert_eq!(use_vault(&shared, fresh), Ok(false));
    }
}

