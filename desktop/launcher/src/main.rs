//! The `openmymemo` command AI apps run on Windows (product-design.md §15.2): the Windows counterpart of
//! the `sh` launcher the Mac app writes (desktop/src-tauri/src/launcher.rs).
//!
//! The app copies this program to `%LOCALAPPDATA%\ai.mymemo.openmymemo\bin\openmymemo.exe` on every start,
//! next to `openmymemo.launch`, which it writes too, and hands its path to the server as
//! OPENMYMEMO_COMMAND, so the MCP configs the panel writes point here, never into the app's install folder.
//!
//! It runs the current environment's `openmymemo.exe` (named in `current-env`, core.rs) once the app marked
//! it ready. Before that (the first start is still downloading Python, or an install was interrupted) it
//! lets the app's uv install or finish that build's environment, as the app does; uv locks it, so this waits
//! for a setup that is already under way. It never writes to stdout itself: that is the MCP stream.

use std::ffi::OsString;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::{Command, ExitCode};

/// Written last into an environment that uv finished (core.rs READY).
const READY: &str = ".openmymemo-ready";

/// What the app wrote into `openmymemo.launch`: one `key=value` per line.
#[derive(Debug, Default, PartialEq)]
struct Launch {
    /// The app's bundled uv.
    uv: Option<PathBuf>,
    /// The app's bundled package source (pyproject.toml, uv.lock, src/).
    project: Option<PathBuf>,
    /// That build's environment folder under venvs/.
    environment: Option<PathBuf>,
    /// `env:NAME=value`: the variables uv runs with (core::uv_env).
    env: Vec<(String, String)>,
    /// `arg=…`: the uv arguments that select the environment (core::UV_ARGS).
    args: Vec<String>,
}

fn parse(text: &str) -> Launch {
    let mut launch = Launch::default();
    for line in text.lines() {
        let Some((key, value)) = line.split_once('=') else { continue };
        match key {
            "uv" => launch.uv = Some(value.into()),
            "project" => launch.project = Some(value.into()),
            "environment" => launch.environment = Some(value.into()),
            "arg" => launch.args.push(value.into()),
            _ => {
                if let Some(name) = key.strip_prefix("env:") {
                    launch.env.push((name.into(), value.into()));
                }
            }
        }
    }
    launch
}

fn program(env: &Path, name: &str) -> PathBuf {
    if cfg!(windows) {
        env.join("Scripts").join(format!("{name}.exe"))
    } else {
        env.join("bin").join(name)
    }
}

/// The app's current environment, once complete: the folder `current-env` names, directly under venvs/.
fn current(support: &Path) -> Option<PathBuf> {
    let name = fs::read_to_string(support.join("current-env")).ok()?;
    let name = name.trim();
    if name.is_empty() || name.contains(['/', '\\', ':']) || name.starts_with('.') {
        return None;
    }
    let env = support.join("venvs").join(name);
    (env.join(READY).is_file() && program(&env, "openmymemo").is_file()).then_some(env)
}

/// The command to run with the person's arguments, or why there is none.
fn command(exe: &Path, launch: &Launch) -> Result<Command, String> {
    let bin = exe.parent().ok_or("no folder")?;
    let support = bin.parent().ok_or("no folder")?;
    if let Some(env) = current(support) {
        return Ok(Command::new(program(&env, "openmymemo")));
    }
    let not_set_up = "OpenMyMemo isn't set up on this computer: open the OpenMyMemo app once \
                      (https://github.com/MyMemoAI/openmymemo/releases), then try again.";
    let (Some(uv), Some(project), Some(environment)) = (&launch.uv, &launch.project, &launch.environment) else {
        return Err(not_set_up.into());
    };
    if !uv.is_file() || !project.join("pyproject.toml").is_file() {
        return Err(not_set_up.into());
    }
    let mut cmd = Command::new(uv);
    cmd.arg("run").arg("--project").arg(project).args(&launch.args).arg("openmymemo");
    cmd.envs(launch.env.iter().map(|(k, v)| (k, v))).env("UV_PROJECT_ENVIRONMENT", environment);
    Ok(cmd)
}

fn main() -> ExitCode {
    let exe = match std::env::current_exe() {
        Ok(exe) => exe,
        Err(e) => {
            eprintln!("openmymemo: {e}");
            return ExitCode::FAILURE;
        }
    };
    let launch = exe
        .parent()
        .and_then(|bin| fs::read_to_string(bin.join("openmymemo.launch")).ok())
        .map(|text| parse(&text))
        .unwrap_or_default();
    let mut cmd = match command(&exe, &launch) {
        Ok(cmd) => cmd,
        Err(e) => {
            eprintln!("{e}");
            return ExitCode::FAILURE;
        }
    };
    let args: Vec<OsString> = std::env::args_os().skip(1).collect();
    let status = cmd
        .args(args)
        .env("OPENMYMEMO_COMMAND", &exe)
        .env_remove("PYTHONHOME")
        .env_remove("PYTHONPATH")
        .env_remove("VIRTUAL_ENV")
        .status();
    match status {
        Ok(status) => ExitCode::from(status.code().map_or(1, |c| u8::try_from(c).unwrap_or(1))),
        Err(e) => {
            eprintln!("openmymemo: can't start {:?}: {e}", cmd.get_program());
            ExitCode::FAILURE
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn temp(name: &str) -> PathBuf {
        let root = std::env::temp_dir().join(format!("openmymemo-launcher-{}-{name}", std::process::id()));
        let _ = fs::remove_dir_all(&root);
        fs::create_dir_all(root.join("bin")).unwrap();
        root
    }

    #[test]
    fn reads_what_the_app_wrote() {
        let launch = parse("uv=C:\\Program Files\\OpenMyMemo\\uv.exe\nproject=C:\\x=y\\core\nenvironment=C:\\s\\venvs\\1.0.0-5\n\
                            env:UV_PYTHON=3.12\narg=--frozen\narg=--quiet\nnonsense\nfuture=ignored\n");
        assert_eq!(launch.uv, Some(PathBuf::from("C:\\Program Files\\OpenMyMemo\\uv.exe")));
        assert_eq!(launch.project, Some(PathBuf::from("C:\\x=y\\core")), "only the first = splits");
        assert_eq!(launch.env, vec![("UV_PYTHON".into(), "3.12".into())]);
        assert_eq!(launch.args, vec!["--frozen".to_string(), "--quiet".to_string()]);
    }

    #[test]
    fn runs_the_ready_environment_and_nothing_outside_venvs() {
        let root = temp("ready");
        let exe = root.join("bin").join("openmymemo.exe");
        let env = root.join("venvs").join("1.0.0-5");
        fs::create_dir_all(program(&env, "openmymemo").parent().unwrap()).unwrap();
        fs::write(program(&env, "openmymemo"), "").unwrap();
        fs::write(root.join("current-env"), "1.0.0-5\n").unwrap();
        assert!(command(&exe, &Launch::default()).is_err(), "not ready yet, and nothing to install with");
        fs::write(env.join(READY), "1.0.0\n").unwrap();
        assert_eq!(command(&exe, &Launch::default()).unwrap().get_program(), program(&env, "openmymemo").as_os_str());
        for name in ["..", "../elsewhere", "C:\\elsewhere", ".hidden", ""] {
            fs::write(root.join("current-env"), name).unwrap();
            assert!(current(&root).is_none(), "{name:?}");
        }
    }

    #[test]
    fn without_an_environment_the_apps_uv_installs_it() {
        let root = temp("install");
        let exe = root.join("bin").join("openmymemo.exe");
        let app = root.join("app");
        fs::create_dir_all(app.join("core")).unwrap();
        fs::write(app.join("uv.exe"), "").unwrap();
        let launch = Launch {
            uv: Some(app.join("uv.exe")),
            project: Some(app.join("core")),
            environment: Some(root.join("venvs").join("2.0.0-9")),
            env: vec![("UV_PYTHON".into(), "3.12".into())],
            args: vec!["--frozen".into()],
        };
        assert!(command(&exe, &launch).unwrap_err().contains("open the OpenMyMemo app once"), "no core in the app");
        fs::write(app.join("core").join("pyproject.toml"), "").unwrap();
        let cmd = command(&exe, &launch).unwrap();
        assert_eq!(cmd.get_program(), app.join("uv.exe").as_os_str());
        let args: Vec<_> = cmd.get_args().map(|a| a.to_string_lossy().into_owned()).collect();
        assert_eq!(args, ["run", "--project", &app.join("core").to_string_lossy(), "--frozen", "openmymemo"]);
        let envs: Vec<_> = cmd.get_envs().map(|(k, v)| (k.to_string_lossy().into_owned(), v.map(|v| v.to_string_lossy().into_owned()))).collect();
        assert!(envs.contains(&("UV_PROJECT_ENVIRONMENT".into(), Some(root.join("venvs").join("2.0.0-9").to_string_lossy().into_owned()))));
        assert!(envs.contains(&("UV_PYTHON".into(), Some("3.12".into()))));
    }
}
