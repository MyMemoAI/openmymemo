//! Moving the app to Applications (product-design.md §15.2).
//!
//! Opened from its disk image, or from an App Translocation path (the random read-only copy macOS runs a
//! downloaded app from until it is moved), the app offers to copy itself to Applications, open the copy and
//! quit, so it keeps working once the disk image is ejected. The copy leaves out the quarantine flag, as
//! LetsMove does: the person has just opened this very app, and a quarantined copy that the Finder didn't
//! move would be translocated again. main.rs asks; this module does the work, with the target folder as a
//! parameter so the tests never touch /Applications.

use std::ffi::CString;
use std::fs;
use std::os::unix::ffi::OsStrExt;
use std::os::unix::process::CommandExt;
use std::path::{Component, Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

use crate::launcher::APP_NAME;

#[derive(Debug, PartialEq)]
pub enum Transient {
    /// On a mounted disk image: its volume, to eject once the app runs from Applications.
    DiskImage(PathBuf),
    /// In macOS's randomized read-only copy; where it came from isn't known.
    Translocated,
}

/// Whether the app runs from somewhere to move it out of: a translocated copy, or a read-only volume under
/// /Volumes (a disk image; an external drive the person keeps apps on is writable and left alone).
/// `read_only` says whether a path's volume is mounted read-only.
pub fn transient(bundle: &Path, read_only: impl Fn(&Path) -> bool) -> Option<Transient> {
    if bundle.components().any(|c| c.as_os_str() == "AppTranslocation") {
        return Some(Transient::Translocated);
    }
    let mut parts = bundle.components();
    if parts.next() != Some(Component::RootDir) || parts.next()?.as_os_str() != "Volumes" {
        return None;
    }
    let volume = Path::new("/Volumes").join(parts.next()?.as_os_str());
    read_only(bundle).then_some(Transient::DiskImage(volume))
}

pub fn is_read_only(path: &Path) -> bool {
    let Ok(c_path) = CString::new(path.as_os_str().as_bytes()) else { return false };
    let mut st: libc::statfs = unsafe { std::mem::zeroed() };
    unsafe { libc::statfs(c_path.as_ptr(), &mut st) == 0 && st.f_flags & libc::MNT_RDONLY as u32 != 0 }
}

/// Whether `volume` is a mounted disk image (as opposed to a drive), so ejecting it is safe.
pub fn is_disk_image(volume: &Path) -> bool {
    let Ok(out) = Command::new("/usr/bin/hdiutil").arg("info").stdin(Stdio::null()).output() else { return false };
    let volume = volume.to_string_lossy();
    String::from_utf8_lossy(&out.stdout).lines().any(|line| line.rsplit('\t').next() == Some(volume.as_ref()))
}

/// The version in a bundle's Info.plist.
pub fn version_of(bundle: &Path) -> Option<String> {
    let out = Command::new("/usr/bin/plutil")
        .args(["-extract", "CFBundleShortVersionString", "raw", "-o", "-"])
        .arg(bundle.join("Contents/Info.plist"))
        .stdin(Stdio::null())
        .output()
        .ok()?;
    let version = String::from_utf8_lossy(&out.stdout).trim().to_string();
    (out.status.success() && !version.is_empty()).then_some(version)
}

/// Processes running from `bundle` (`ps` shows each one's executable path).
pub fn running_from(bundle: &Path) -> Vec<i32> {
    let prefix = bundle.join("Contents/MacOS/");
    let Ok(out) = Command::new("/bin/ps").args(["-axo", "pid=,comm="]).stdin(Stdio::null()).output() else {
        return Vec::new();
    };
    String::from_utf8_lossy(&out.stdout)
        .lines()
        .filter_map(|line| {
            let (pid, comm) = line.trim_start().split_once(' ')?;
            Path::new(comm.trim_start()).starts_with(&prefix).then(|| pid.parse().ok()).flatten()
        })
        .filter(|pid| *pid != std::process::id() as i32)
        .collect()
}

/// Quit the copy of the app running from `bundle`, if any: it stops its server on SIGTERM (main.rs).
pub fn quit(bundle: &Path) -> Result<(), String> {
    let pids = running_from(bundle);
    for pid in &pids {
        unsafe {
            libc::kill(*pid, libc::SIGTERM);
        }
    }
    let deadline = Instant::now() + Duration::from_secs(10);
    while !running_from(bundle).is_empty() {
        if Instant::now() > deadline {
            return Err(format!("The OpenMyMemo in {} didn't quit.", bundle.parent().unwrap_or(bundle).display()));
        }
        std::thread::sleep(Duration::from_millis(200));
    }
    Ok(())
}

/// Copy `bundle` to `<target_dir>/OpenMyMemo.app` with ditto, replacing a copy that is there only when
/// `replace` is set. The copy is made under a hidden name and renamed into place, so a failed copy leaves
/// the folder as it was.
pub fn copy_to(bundle: &Path, target_dir: &Path, replace: bool) -> Result<PathBuf, String> {
    let dst = target_dir.join(APP_NAME);
    let exists = fs::symlink_metadata(&dst).is_ok();
    if exists && !replace {
        return Err(format!("{} already exists.", dst.display()));
    }
    let pid = std::process::id();
    let staging = target_dir.join(format!(".{APP_NAME}.{pid}.new"));
    let old = target_dir.join(format!(".{APP_NAME}.{pid}.old"));
    let _ = fs::remove_dir_all(&staging);
    let out = Command::new("/usr/bin/ditto")
        .arg(bundle)
        .arg(&staging)
        .stdin(Stdio::null())
        .output()
        .map_err(|e| format!("Can't run ditto: {e}"))?;
    if !out.status.success() {
        let _ = fs::remove_dir_all(&staging);
        let err = String::from_utf8_lossy(&out.stderr);
        return Err(err.lines().next().unwrap_or("The copy failed.").trim().to_string());
    }
    // ditto keeps extended attributes, the quarantine flag among them (--noqtn doesn't drop it).
    let _ = Command::new("/usr/bin/xattr")
        .args(["-dr", "com.apple.quarantine"])
        .arg(&staging)
        .stdin(Stdio::null())
        .stderr(Stdio::null())
        .status();
    if exists {
        if let Err(e) = fs::rename(&dst, &old) {
            let _ = fs::remove_dir_all(&staging);
            return Err(format!("Can't replace {}: {e}", dst.display()));
        }
    }
    if let Err(e) = fs::rename(&staging, &dst) {
        if exists {
            let _ = fs::rename(&old, &dst);
        }
        let _ = fs::remove_dir_all(&staging);
        return Err(format!("Can't create {}: {e}", dst.display()));
    }
    if exists {
        let _ = fs::remove_dir_all(&old);
    }
    Ok(dst)
}

/// Once this process has exited, open `app`, then eject `volume` (the disk image it was copied from).
pub fn relaunch_after_exit(app: &Path, volume: Option<&Path>) -> Result<(), String> {
    const SCRIPT: &str = r#"while kill -0 "$1" 2>/dev/null; do sleep 0.2; done
/usr/bin/open "$2"
if [ -n "$3" ]; then sleep 1; /usr/bin/hdiutil detach "$3" -quiet; fi"#;
    Command::new("/bin/sh")
        .args(["-c", SCRIPT, "sh", &std::process::id().to_string()])
        .arg(app)
        .arg(volume.unwrap_or(Path::new("")))
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .process_group(0)
        .spawn()
        .map(|_| ())
        .map_err(|e| format!("Can't reopen OpenMyMemo: {e}"))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn temp_dir(name: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!("openmymemo-install-{}-{name}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        dir
    }

    /// A stand-in bundle: an executable, an Info.plist with `version`, and a quarantine flag as a download has.
    fn fake_bundle(dir: &Path, version: &str) -> PathBuf {
        let bundle = dir.join(APP_NAME);
        fs::create_dir_all(bundle.join("Contents/MacOS")).unwrap();
        fs::write(bundle.join("Contents/MacOS/openmymemo-desktop"), "#!/bin/sh\n").unwrap();
        fs::write(
            bundle.join("Contents/Info.plist"),
            format!(r#"<?xml version="1.0" encoding="UTF-8"?><plist version="1.0"><dict><key>CFBundleShortVersionString</key><string>{version}</string></dict></plist>"#),
        )
        .unwrap();
        let status = Command::new("/usr/bin/xattr")
            .args(["-w", "com.apple.quarantine", "0081;00000000;Safari;"])
            .arg(bundle.join("Contents/MacOS/openmymemo-desktop"))
            .status()
            .unwrap();
        assert!(status.success());
        bundle
    }

    fn quarantined(path: &Path) -> bool {
        let out = Command::new("/usr/bin/xattr").arg(path).output().unwrap();
        String::from_utf8_lossy(&out.stdout).contains("com.apple.quarantine")
    }

    #[test]
    fn moves_out_of_disk_images_and_translocation_only() {
        let yes = |_: &Path| true;
        let no = |_: &Path| false;
        let dmg = Path::new("/Volumes/OpenMyMemo 1/OpenMyMemo.app");
        assert_eq!(transient(dmg, yes), Some(Transient::DiskImage(PathBuf::from("/Volumes/OpenMyMemo 1"))));
        // A writable external drive the person keeps apps on.
        assert_eq!(transient(dmg, no), None);
        let translocated = Path::new("/private/var/folders/x/T/AppTranslocation/0B5A/d/OpenMyMemo.app");
        assert_eq!(transient(translocated, no), Some(Transient::Translocated));
        assert_eq!(transient(Path::new("/Applications/OpenMyMemo.app"), yes), None);
        assert_eq!(transient(Path::new("/Users/me/Applications/OpenMyMemo.app"), yes), None);
        // The system volume is read-only, the folder for apps isn't.
        assert!(is_read_only(Path::new("/System")));
        assert!(!is_read_only(&std::env::temp_dir()));
    }

    #[test]
    fn copies_without_the_quarantine_flag_and_replaces_only_when_asked() {
        let root = temp_dir("copy");
        let source = fake_bundle(&root.join("dmg"), "2.0.0");
        let target = root.join("Applications");
        fs::create_dir_all(&target).unwrap();

        let dst = copy_to(&source, &target, false).unwrap();
        assert_eq!(dst, target.join(APP_NAME));
        assert_eq!(version_of(&dst).as_deref(), Some("2.0.0"));
        assert!(quarantined(&source.join("Contents/MacOS/openmymemo-desktop")));
        assert!(!quarantined(&dst.join("Contents/MacOS/openmymemo-desktop")));

        // An older copy is there: kept unless replacing was confirmed, then swapped with nothing left over.
        let newer = fake_bundle(&root.join("dmg2"), "2.1.0");
        assert!(copy_to(&newer, &target, false).unwrap_err().contains("already exists"));
        assert_eq!(version_of(&dst).as_deref(), Some("2.0.0"));
        copy_to(&newer, &target, true).unwrap();
        assert_eq!(version_of(&dst).as_deref(), Some("2.1.0"));
        let names: Vec<_> = fs::read_dir(&target).unwrap().flatten().map(|e| e.file_name()).collect();
        assert_eq!(names, vec![std::ffi::OsString::from(APP_NAME)]);
    }

    #[test]
    fn a_failed_copy_leaves_the_folder_as_it_was() {
        let root = temp_dir("fail");
        let target = root.join("Applications");
        let existing = fake_bundle(&target, "1.0.0");
        let err = copy_to(&root.join("missing/OpenMyMemo.app"), &target, true).unwrap_err();
        assert!(!err.is_empty());
        assert_eq!(version_of(&existing).as_deref(), Some("1.0.0"));
        assert_eq!(fs::read_dir(&target).unwrap().count(), 1);
        // A folder it can't write to.
        let locked = root.join("Locked");
        fs::create_dir_all(&locked).unwrap();
        let mut perms = fs::metadata(&locked).unwrap().permissions();
        std::os::unix::fs::PermissionsExt::set_mode(&mut perms, 0o555);
        fs::set_permissions(&locked, perms).unwrap();
        assert!(copy_to(&existing, &locked, false).is_err());
        assert!(fs::symlink_metadata(locked.join(APP_NAME)).is_err());
    }

    #[test]
    fn finds_nothing_running_from_a_copy_that_isnt_running() {
        let root = temp_dir("running");
        assert!(running_from(&root.join(APP_NAME)).is_empty());
        assert!(quit(&root.join(APP_NAME)).is_ok());
    }
}
