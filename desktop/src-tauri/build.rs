// The panel is served by the local Python server, so the window's page is a "remote" origin to Tauri.
// Declaring the app's commands lets capabilities/default.json grant exactly these to that origin.
fn main() {
    tauri_build::try_build(tauri_build::Attributes::new().app_manifest(
        tauri_build::AppManifest::new().commands(&[
            "reveal_path",
            "app_settings",
            "set_autostart",
            "set_appearance",
            "set_notifications",
            "choose_vault",
            "retry_start",
        ]),
    ))
    .expect("failed to run tauri-build");
}
