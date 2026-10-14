import { reactive } from "vue";

/**
 * The Mac app (desktop/) loads this same panel from the local server with `?desktop=1` and switches it
 * to its Mac layout. The query survives hash navigation, so the flag holds for the whole session; in a
 * browser, add `?desktop=1` to preview the layout.
 */
export const desktop = new URLSearchParams(location.search).has("desktop");

type Invoke = (cmd: string, args?: Record<string, unknown>) => Promise<unknown>;
const internals = (window as unknown as { __TAURI_INTERNALS__?: { invoke: Invoke } }).__TAURI_INTERNALS__;

/** Inside the Mac app: true once the shell's commands are reachable (they aren't in a browser preview). */
export const native = !!internals;

export type Appearance = "system" | "light" | "dark";

export interface AppSettings {
  vault: string;
  autostart: boolean;
  appearance: Appearance;
  /** A notification when an AI app changes something or something starts waiting. */
  notifications: boolean;
  version: string;
  /** The `openmymemo` command the app installs; AI apps' MCP configs run it. */
  launcher: string;
  /** The local server's log. */
  log: string;
}

/** Commands the Mac shell implements (desktop/src-tauri/src/main.rs). */
export const shell = {
  reveal: (path: string) => internals?.invoke("reveal_path", { path }),
  settings: () => (internals ? (internals.invoke("app_settings") as Promise<AppSettings>) : Promise.resolve(null)),
  setAutostart: (enabled: boolean) => internals?.invoke("set_autostart", { enabled }),
  /** The whole app follows it: windows, this panel (its CSS follows the window), menus and dialogs. */
  setAppearance: (appearance: Appearance) => internals?.invoke("set_appearance", { appearance }),
  setNotifications: (enabled: boolean) => internals?.invoke("set_notifications", { enabled }),
  /** Ask for another memory folder; the app restarts its server on it and reloads. */
  chooseVault: () => internals?.invoke("choose_vault"),
};

export type Command = "new-memory" | "find" | "toggle-sidebar" | "settings";

/**
 * `request`: a "new memory" or "find" command for the Memories screen, which may not be open yet; it
 * takes the request when it mounts or when the request changes, and clears it.
 */
export const ui = reactive({ sidebarHidden: false, settingsOpen: false, request: null as Command | null });

/**
 * Menu items in the shell dispatch `openmymemo:command` events into the page. The same shortcuts are
 * handled here too, for a browser preview and for the ones a menu doesn't claim.
 */
export function onCommand(handler: (cmd: Command) => void): () => void {
  const onEvent = (e: Event) => handler((e as CustomEvent<Command>).detail);
  const onKey = (e: KeyboardEvent) => {
    if (!e.metaKey || e.altKey || e.isComposing) return;
    const key = e.key.toLowerCase();
    const cmd: Command | null =
      key === "f" && !e.ctrlKey ? "find"
      : key === "s" && e.ctrlKey ? "toggle-sidebar"
      : key === "," && !e.ctrlKey ? "settings"
      : key === "n" && !e.ctrlKey ? "new-memory"
      : null;
    if (!cmd) return;
    e.preventDefault();
    handler(cmd);
  };
  window.addEventListener("openmymemo:command", onEvent);
  window.addEventListener("keydown", onKey);
  return () => {
    window.removeEventListener("openmymemo:command", onEvent);
    window.removeEventListener("keydown", onKey);
  };
}
