//! Updates: the app asks for the latest release's update manifest (`latest.json`, which the release workflow
//! writes and signs with the project's updater key, see scripts/update_manifest.py) shortly after it starts
//! and then once a day, and asks before installing. The signature is checked against the public key in
//! tauri.conf.json, so only a release built with that key can be installed. The install replaces the app and
//! starts it again; the memory folder, the environments under Application Support and the launcher AI apps
//! run aren't touched, and the next start installs the new core as after any update (core.rs).

use std::time::Duration;

use tauri::AppHandle;
use tauri_plugin_updater::{Update, UpdaterExt};

/// The first check, once the window and the server are up.
pub const FIRST_CHECK: Duration = Duration::from_secs(60);
pub const EVERY: Duration = Duration::from_secs(24 * 3600);

/// How long a check may take, a connection may take to open, and a download may sit without data or take in
/// all: a network that accepts the request and then goes quiet must not hold the checks or the install forever.
const CHECK_TIMEOUT: Duration = Duration::from_secs(30);
const CONNECT_TIMEOUT: Duration = Duration::from_secs(15);
const STALL_TIMEOUT: Duration = Duration::from_secs(60);
const DOWNLOAD_TIMEOUT: Duration = Duration::from_secs(30 * 60);

/// The release page, for when an update can't be installed from the app.
pub const RELEASES: &str = "https://github.com/MyMemoAI/openmymemo/releases/latest";

pub enum Check {
    Current,
    Available(Box<Update>),
    Failed(String),
}

pub fn check(app: &AppHandle) -> Check {
    let found = tauri::async_runtime::block_on(async {
        // On Windows the installer takes over and the app exits from inside the updater: stop the server first.
        app.updater_builder()
            .on_before_exit(crate::core::stop_child)
            .timeout(CHECK_TIMEOUT)
            .configure_client(|client| client.connect_timeout(CONNECT_TIMEOUT).read_timeout(STALL_TIMEOUT))
            .build()?
            .check()
            .await
    });
    match found {
        Ok(Some(mut update)) => {
            // The update doesn't keep the check's timeout (tauri-plugin-updater 2.14): give the download its own.
            update.timeout = Some(DOWNLOAD_TIMEOUT);
            Check::Available(Box::new(update))
        }
        Ok(None) => Check::Current,
        Err(e) => Check::Failed(e.to_string()),
    }
}

/// Download, check the signature, install and start the new version. Returns only if that failed.
pub fn install(app: &AppHandle, update: &Update) -> String {
    let installed = tauri::async_runtime::block_on(update.download_and_install(|_, _| {}, || {}));
    match installed {
        Ok(()) => {
            crate::core::stop_child();
            app.restart()
        }
        Err(e) => e.to_string(),
    }
}

/// What's new, for the dialog: the release notes as plain lines (the changelog's Markdown without its
/// emphasis and links), cut at `max` characters.
pub fn summary(notes: &str, max: usize) -> String {
    let mut text = String::new();
    for line in notes.lines().map(str::trim).filter(|l| !l.is_empty()) {
        let line = line.trim_start_matches(['-', '*', ' ']).replace("**", "").replace('`', "");
        let line = unlink(&line);
        if !text.is_empty() {
            text.push('\n');
        }
        text.push_str("• ");
        text.push_str(&line);
    }
    if text.chars().count() <= max {
        return text;
    }
    let cut: String = text.chars().take(max.saturating_sub(1)).collect();
    format!("{}…", cut.trim_end())
}

/// `[text](url)` → `text`.
fn unlink(line: &str) -> String {
    let mut out = String::new();
    let mut rest = line;
    while let Some(open) = rest.find('[') {
        let Some(close) = rest[open..].find("](").map(|i| open + i) else { break };
        let Some(end) = rest[close..].find(')').map(|i| close + i) else { break };
        out.push_str(&rest[..open]);
        out.push_str(&rest[open + 1..close]);
        rest = &rest[end + 1..];
    }
    out.push_str(rest);
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn release_notes_read_as_plain_lines() {
        let notes = "- **The app on Windows (preview).** See [the README](https://github.com/x#y), `setup.exe`.\n\n- Fixes.\n";
        assert_eq!(summary(notes, 500), "• The app on Windows (preview). See the README, setup.exe.\n• Fixes.");
        assert_eq!(summary(notes, 12), "• The app o…");
        assert_eq!(summary("", 100), "");
        assert_eq!(unlink("[a](b) and [c] (d) [e](f"), "a and [c] (d) [e](f");
    }
}
