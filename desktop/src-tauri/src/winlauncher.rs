//! The `openmymemo` command AI apps run, on Windows (product-design.md §15.2).
//!
//! The Mac app writes a `sh` launcher (launcher.rs). Windows has no `sh`, and AI apps there start a `.cmd`
//! only through a shell, so the app installs a small program instead: the `openmymemo-launcher.exe` it
//! bundles (desktop/launcher), copied to `bin\openmymemo.exe` under its folder in %LOCALAPPDATA%, with
//! `bin\openmymemo.launch` beside it saying how to install this build's environment when none is ready.
//! Both are rewritten on every start when they changed; the MCP configs the panel writes point at the copy,
//! never into the install folder, which an update replaces.

use std::ffi::OsString;
use std::fs;
use std::path::{Path, PathBuf};

pub struct Launcher<'a> {
    /// Where the launcher goes: support\bin\openmymemo.exe.
    pub path: &'a Path,
    /// The bundled launcher program it is copied from.
    pub program: &'a Path,
    /// The bundled uv and package source, and this build's environment folder (core::Paths::build_env).
    pub uv: &'a Path,
    pub project: &'a Path,
    pub environment: &'a Path,
    /// The variables uv runs with (core::uv_env) and the arguments that select the environment (core::UV_ARGS).
    pub uv_env: &'a [(&'static str, OsString)],
    pub uv_args: &'a [&'static str],
}

impl Launcher<'_> {
    /// What `openmymemo.launch` holds: one `key=value` per line (desktop/launcher reads it).
    pub fn config(&self) -> String {
        let mut lines = vec![
            "# Written by the OpenMyMemo app each time it starts (desktop/src-tauri/src/winlauncher.rs).".to_string(),
            format!("uv={}", self.uv.display()),
            format!("project={}", self.project.display()),
            format!("environment={}", self.environment.display()),
        ];
        lines.extend(self.uv_env.iter().map(|(k, v)| format!("env:{k}={}", v.to_string_lossy())));
        lines.extend(self.uv_args.iter().map(|a| format!("arg={a}")));
        lines.join("\n") + "\n"
    }

    pub fn write(&self) -> Result<(), String> {
        let dir = self.path.parent().ok_or("The launcher has no folder")?;
        fs::create_dir_all(dir).map_err(|e| format!("Can't create {}: {e}", dir.display()))?;
        remove_old_copies(dir);
        replace(&dir.join("openmymemo.launch"), self.config().as_bytes())?;
        let program = fs::read(self.program).map_err(|e| format!("Can't read {}: {e}", self.program.display()))?;
        replace(self.path, &program)
    }
}

/// Write `bytes` to `path` if it holds something else: a temporary file renamed over it. A launcher an AI app
/// is running can't be replaced, but it can be renamed: it is moved aside first and removed on a later start.
fn replace(path: &Path, bytes: &[u8]) -> Result<(), String> {
    if fs::read(path).is_ok_and(|old| old == bytes) {
        return Ok(());
    }
    let dir = path.parent().ok_or("no folder")?;
    let name = path.file_name().ok_or("no name")?.to_string_lossy();
    let tmp = dir.join(format!(".{name}.{}.tmp", std::process::id()));
    let written = fs::write(&tmp, bytes).and_then(|_| match fs::rename(&tmp, path) {
        Ok(()) => Ok(()),
        Err(_) if path.exists() => {
            fs::rename(path, dir.join(format!("{OLD_PREFIX}{}-{name}", std::process::id())))?;
            fs::rename(&tmp, path)
        }
        Err(e) => Err(e),
    });
    written.map_err(|e| {
        let _ = fs::remove_file(&tmp);
        format!("Can't write {}: {e}", path.display())
    })
}

/// Launchers moved aside while an AI app ran them.
const OLD_PREFIX: &str = ".openmymemo-old-";

fn remove_old_copies(dir: &Path) {
    let Ok(entries) = fs::read_dir(dir) else { return };
    for entry in entries.flatten() {
        if entry.file_name().to_string_lossy().starts_with(OLD_PREFIX) {
            let _ = fs::remove_file(entry.path()); // still running: next time
        }
    }
}

/// Where the app keeps the launcher.
pub fn path(support: &Path) -> PathBuf {
    support.join("bin").join("openmymemo.exe")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn writes_the_program_and_how_to_install_and_keeps_both_when_unchanged() {
        let root = std::env::temp_dir().join(format!("openmymemo-desktop-{}-winlauncher", std::process::id()));
        let _ = fs::remove_dir_all(&root);
        fs::create_dir_all(root.join("app")).unwrap();
        fs::write(root.join("app/openmymemo-launcher.exe"), "v1").unwrap();
        let env = [("UV_PYTHON", OsString::from("3.12"))];
        let launcher = Launcher {
            path: &path(&root.join("support")),
            program: &root.join("app/openmymemo-launcher.exe"),
            uv: &root.join("app/uv.exe"),
            project: &root.join("app/core"),
            environment: &root.join("support/venvs/1.0.0-5"),
            uv_env: &env,
            uv_args: &["--frozen", "--no-dev"],
        };
        launcher.write().unwrap();
        assert_eq!(fs::read_to_string(launcher.path).unwrap(), "v1");
        let config = fs::read_to_string(root.join("support/bin/openmymemo.launch")).unwrap();
        assert!(config.contains(&format!("\nuv={}\n", root.join("app/uv.exe").display())), "{config}");
        assert!(config.contains("\nenv:UV_PYTHON=3.12\narg=--frozen\narg=--no-dev\n"), "{config}");

        // An update replaces the program; an old copy moved aside is removed on the next start.
        fs::write(root.join("app/openmymemo-launcher.exe"), "v2").unwrap();
        fs::write(root.join("support/bin").join(format!("{OLD_PREFIX}1-openmymemo.exe")), "v0").unwrap();
        launcher.write().unwrap();
        assert_eq!(fs::read_to_string(launcher.path).unwrap(), "v2");
        let mut names: Vec<_> = fs::read_dir(root.join("support/bin")).unwrap().flatten().map(|e| e.file_name()).collect();
        names.sort();
        assert_eq!(names, ["openmymemo.exe", "openmymemo.launch"]);
    }
}
