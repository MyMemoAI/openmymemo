//! The `openmymemo` command AI apps run (product-design.md §15.2).
//!
//! On every start the app writes a small `sh` launcher to `~/Library/Application Support/<id>/bin/openmymemo`
//! and hands its path to the server as OPENMYMEMO_COMMAND, so the MCP configs the panel writes point here,
//! never into the app bundle, which the person may move, replace or run from its disk image.
//!
//! The app installs the package non-editable into its own environment (core.rs), so the usual case needs
//! nothing from the bundle: the launcher runs that environment's `openmymemo`. Until the first start has
//! installed it, the launcher finds the app and lets the bundled uv install it; uv locks the environment
//! and its Python downloads, so a launcher started while the app is still setting up waits for that setup
//! instead of installing alongside it. The launcher never writes to stdout itself: that is the MCP stream.

use std::ffi::OsString;
use std::fs;
use std::io::Write;
use std::os::unix::ffi::OsStrExt;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};

pub const BUNDLE_ID: &str = "ai.mymemo.openmymemo";
pub const APP_NAME: &str = "OpenMyMemo.app";

/// The .app bundle an executable belongs to: …/OpenMyMemo.app/Contents/MacOS/openmymemo-desktop.
pub fn bundle_of(exe: &Path) -> Option<PathBuf> {
    let macos = exe.parent()?;
    let bundle = macos.parent()?.parent()?;
    (macos.file_name()? == "MacOS" && bundle.extension()? == "app").then(|| bundle.to_path_buf())
}

pub struct Launcher<'a> {
    pub version: &'a str,
    /// Where the launcher lives; it passes this on as OPENMYMEMO_COMMAND to the servers it starts.
    pub path: &'a Path,
    /// The app's environment, which the launcher runs when it is installed.
    pub venv: &'a Path,
    /// The variables uv runs with (core::uv_env), the same as for the app's own setup.
    pub uv_env: &'a [(&'static str, OsString)],
    /// The uv arguments that select the environment (core::UV_ARGS).
    pub uv_args: &'a [&'static str],
    /// The app bundle that wrote the launcher, when the app runs from one (not in `tauri dev`).
    pub app: Option<&'a Path>,
    /// Where else to look for the app, in order, before asking Spotlight.
    pub search: &'a [PathBuf],
    /// Spotlight's command-line tool; a stand-in in tests.
    pub mdfind: &'a Path,
}

/// Quote for sh: single quotes, with each `'` written as `'\''`.
fn quote(value: impl AsRef<std::ffi::OsStr>) -> String {
    let text = String::from_utf8_lossy(value.as_ref().as_bytes()).into_owned();
    format!("'{}'", text.replace('\'', r"'\''"))
}

impl Launcher<'_> {
    pub fn script(&self) -> String {
        let search: Vec<String> = self.search.iter().map(quote).collect();
        let uv_env: Vec<String> = self.uv_env.iter().map(|(k, v)| format!("{k}={}", quote(v))).collect();
        format!(
            r#"#!/bin/sh
# The openmymemo command, written by OpenMyMemo.app {version} each time it starts (desktop/src-tauri/src/launcher.rs).
# AI apps run it as their MCP server, so it never prints to stdout itself: stdout is the MCP stream.
unset PYTHONHOME PYTHONPATH VIRTUAL_ENV
export OPENMYMEMO_COMMAND={path}
venv={venv}

# The app installs OpenMyMemo into its own environment, which keeps working wherever the app is.
if [ -x "$venv/bin/python" ] && [ -x "$venv/bin/openmymemo" ]; then
  exec "$venv/bin/openmymemo" "$@"
fi

# Not installed yet (the first start may still be downloading Python): find the app and let its uv
# install it. uv locks the environment, so this waits for a setup that is already under way.
usable() {{ [ -x "$1/Contents/MacOS/uv" ] && [ -f "$1/Contents/Resources/core/pyproject.toml" ]; }}
transient() {{ case "$1" in (/Volumes/*|*/AppTranslocation/*) return 0 ;; esac; return 1; }}
recorded={app}
app=
for candidate in "$recorded" {search}; do
  if [ -n "$candidate" ] && ! transient "$candidate" && usable "$candidate"; then app=$candidate; break; fi
done
if [ -z "$app" ]; then
  app=$({mdfind} "kMDItemCFBundleIdentifier == '{bundle_id}'" 2>/dev/null | while IFS= read -r found; do
    case "$found" in (*/.Trash/*) continue ;; esac
    if ! transient "$found" && usable "$found"; then printf '%s\n' "$found"; break; fi
  done)
fi
# Last, the copy that wrote this file, even on a disk image, while it is still there.
if [ -z "$app" ] && [ -n "$recorded" ] && usable "$recorded"; then app=$recorded; fi
if [ -z "$app" ]; then
  echo "OpenMyMemo isn't set up on this Mac: open the OpenMyMemo app once (https://github.com/MyMemoAI/openmymemo/releases), then try again." >&2
  exit 1
fi
export {uv_env}
exec "$app/Contents/MacOS/uv" run --project "$app/Contents/Resources/core" {uv_args} openmymemo "$@"
"#,
            version = self.version,
            path = quote(self.path),
            venv = quote(self.venv),
            app = self.app.map(quote).unwrap_or_else(|| "''".into()),
            search = search.join(" "),
            mdfind = quote(self.mdfind),
            bundle_id = BUNDLE_ID,
            uv_env = uv_env.join(" "),
            uv_args = self.uv_args.join(" "),
        )
    }

    /// Write the launcher if it changed: to a temporary file next to it, then renamed into place, so an AI
    /// app starting it at that moment runs either the old or the new one, never half of one.
    pub fn write(&self) -> Result<(), String> {
        let text = self.script();
        if fs::read_to_string(self.path).is_ok_and(|old| old == text) {
            return Ok(());
        }
        let dir = self.path.parent().ok_or("The launcher has no folder")?;
        fs::create_dir_all(dir).map_err(|e| format!("Can't create {}: {e}", dir.display()))?;
        let tmp = dir.join(format!(".openmymemo.{}.tmp", std::process::id()));
        let written = (|| {
            let mut f = fs::File::create(&tmp)?;
            f.write_all(text.as_bytes())?;
            f.set_permissions(fs::Permissions::from_mode(0o755))?;
            f.sync_all()?;
            fs::rename(&tmp, self.path)
        })();
        if let Err(e) = written {
            let _ = fs::remove_file(&tmp);
            return Err(format!("Can't write {}: {e}", self.path.display()));
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::process::{Command, Output, Stdio};

    fn temp_dir(name: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!("openmymemo-launcher-{}-{name}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        dir
    }

    fn script(path: &Path, body: &str) {
        fs::create_dir_all(path.parent().unwrap()).unwrap();
        fs::write(path, format!("#!/bin/sh\n{body}\n")).unwrap();
        fs::set_permissions(path, fs::Permissions::from_mode(0o755)).unwrap();
    }

    /// A stand-in app bundle whose uv prints what it was asked to run, as the server it would start.
    fn fake_app(path: &Path) -> PathBuf {
        script(
            &path.join("Contents/MacOS/uv"),
            r#"printf 'uv from %s:' "$0"; printf ' [%s]' "$@"; printf '\nenv=%s cmd=%s\n' "$UV_PROJECT_ENVIRONMENT" "$OPENMYMEMO_COMMAND""#,
        );
        fs::create_dir_all(path.join("Contents/Resources/core")).unwrap();
        fs::write(path.join("Contents/Resources/core/pyproject.toml"), "").unwrap();
        path.to_path_buf()
    }

    struct Setup {
        root: PathBuf,
        launcher: PathBuf,
        venv: PathBuf,
        recorded: Option<PathBuf>,
        search: Vec<PathBuf>,
        /// What the stand-in Spotlight finds, one path per line.
        spotlight: String,
    }

    impl Setup {
        fn new(name: &str) -> Self {
            let root = temp_dir(name);
            Setup {
                launcher: root.join("Application Support/bin/openmymemo"),
                venv: root.join("Application Support/venv"),
                recorded: None,
                search: vec![root.join("Applications/OpenMyMemo.app"), root.join("home/Applications/OpenMyMemo.app")],
                spotlight: String::new(),
                root,
            }
        }

        fn install_venv(&self) {
            script(&self.venv.join("bin/python"), "exit 0");
            script(
                &self.venv.join("bin/openmymemo"),
                r#"printf 'venv:'; printf ' [%s]' "$@"; printf '\ncmd=%s pythonpath=%s\n' "$OPENMYMEMO_COMMAND" "${PYTHONPATH-unset}""#,
            );
        }

        fn run(&self, args: &[&str]) -> Output {
            let mdfind = self.root.join("mdfind");
            script(&mdfind, &format!("cat <<'EOF'\n{}EOF", self.spotlight));
            let uv_env = [("UV_PROJECT_ENVIRONMENT", self.venv.clone().into_os_string())];
            let launcher = Launcher {
                version: "9.9.9",
                path: &self.launcher,
                venv: &self.venv,
                uv_env: &uv_env,
                uv_args: &["--frozen", "--no-editable"],
                app: self.recorded.as_deref(),
                search: &self.search,
                mdfind: &mdfind,
            };
            launcher.write().unwrap();
            Command::new(&self.launcher)
                .args(args)
                .env("PYTHONPATH", "/somewhere/else")
                .stdin(Stdio::null())
                .output()
                .unwrap()
        }
    }

    fn stdout(out: &Output) -> String {
        String::from_utf8(out.stdout.clone()).unwrap()
    }

    #[test]
    fn finds_the_bundle_of_its_executable() {
        let exe = Path::new("/Volumes/OpenMyMemo/OpenMyMemo.app/Contents/MacOS/openmymemo-desktop");
        assert_eq!(bundle_of(exe), Some(PathBuf::from("/Volumes/OpenMyMemo/OpenMyMemo.app")));
        assert_eq!(bundle_of(Path::new("/x/target/debug/openmymemo-desktop")), None);
    }

    #[test]
    fn runs_the_installed_environment_without_the_app() {
        let s = Setup::new("venv");
        s.install_venv();
        let out = s.run(&["--vault", "/My Memory/it's here", "mcp", "--connector", "cursor"]);
        assert!(out.status.success());
        assert_eq!(
            stdout(&out),
            format!(
                "venv: [--vault] [/My Memory/it's here] [mcp] [--connector] [cursor]\ncmd={} pythonpath=unset\n",
                s.launcher.display()
            )
        );
        assert!(out.stderr.is_empty());
        let mode = fs::metadata(&s.launcher).unwrap().permissions().mode();
        assert_eq!(mode & 0o777, 0o755);
    }

    #[test]
    fn lets_the_app_install_first_and_prints_nothing_of_its_own() {
        let mut s = Setup::new("install");
        s.recorded = Some(fake_app(&s.root.join("Somewhere/OpenMyMemo.app")));
        let out = s.run(&["mcp"]);
        let recorded = s.recorded.as_ref().unwrap();
        assert_eq!(
            stdout(&out),
            format!(
                "uv from {}/Contents/MacOS/uv: [run] [--project] [{}/Contents/Resources/core] [--frozen] [--no-editable] [openmymemo] [mcp]\nenv={} cmd={}\n",
                recorded.display(),
                recorded.display(),
                s.venv.display(),
                s.launcher.display()
            )
        );
    }

    #[test]
    fn prefers_an_installed_copy_to_one_on_a_disk_image() {
        let mut s = Setup::new("transient");
        // Opened from the disk image, translocated by macOS, then dragged to Applications.
        s.recorded = Some(fake_app(&s.root.join("AppTranslocation/ABC/d/OpenMyMemo.app")));
        fake_app(&s.search[1]);
        let out = s.run(&["--version"]);
        assert!(stdout(&out).starts_with(&format!("uv from {}/", s.search[1].display())), "{}", stdout(&out));
    }

    #[test]
    fn asks_spotlight_when_the_app_moved_and_skips_the_trash() {
        let mut s = Setup::new("moved");
        s.recorded = Some(s.root.join("Old place/OpenMyMemo.app")); // gone
        let trashed = fake_app(&s.root.join(".Trash/OpenMyMemo.app"));
        let moved = fake_app(&s.root.join("Tools/OpenMyMemo.app"));
        s.spotlight = format!("{}\n{}\n", trashed.display(), moved.display());
        let out = s.run(&["--version"]);
        assert!(stdout(&out).starts_with(&format!("uv from {}/", moved.display())), "{}", stdout(&out));
    }

    #[test]
    fn uses_the_disk_image_copy_while_it_is_the_only_one() {
        let mut s = Setup::new("only");
        s.recorded = Some(fake_app(&s.root.join("AppTranslocation/ABC/d/OpenMyMemo.app")));
        let out = s.run(&["--version"]);
        assert!(stdout(&out).starts_with("uv from "), "{}", stdout(&out));
    }

    #[test]
    fn fails_on_stderr_when_nothing_is_there() {
        let mut s = Setup::new("nothing");
        s.recorded = Some(s.root.join("Gone/OpenMyMemo.app"));
        let out = s.run(&["mcp"]);
        assert_eq!(out.status.code(), Some(1));
        assert!(out.stdout.is_empty());
        let err = String::from_utf8(out.stderr).unwrap();
        assert!(err.contains("open the OpenMyMemo app once"), "{err}");
    }

    #[test]
    fn is_only_rewritten_when_it_changes() {
        let s = Setup::new("rewrite");
        s.install_venv();
        s.run(&[]);
        let first = fs::metadata(&s.launcher).unwrap().modified().unwrap();
        std::thread::sleep(std::time::Duration::from_millis(20));
        s.run(&[]);
        assert_eq!(fs::metadata(&s.launcher).unwrap().modified().unwrap(), first);
        let leftovers: Vec<_> = fs::read_dir(s.launcher.parent().unwrap()).unwrap().flatten().map(|e| e.file_name()).collect();
        assert_eq!(leftovers, vec![OsString::from("openmymemo")]);
    }
}
