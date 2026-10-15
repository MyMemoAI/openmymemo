import { flushPromises, mount } from "@vue/test-utils";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { h } from "vue";
import type { AppCatalog, AppStatus, ConnectResult } from "../api";
import { store } from "../store";
import ConnectApps from "./ConnectApps.vue";

function app(id: string, name: string, extra: Partial<AppStatus> = {}): AppStatus {
  return {
    id, name, method: "file", docs: `https://docs.example/${id}`, site: null, note: null, session: false, restart_name: name,
    installed: false, connected: false, via: null, plugin: null, broken: false, revoked: false, in_use: false,
    last_seen: null, has_connector: false, connector: null, ...extra,
  };
}

function result(extra: Partial<ConnectResult> = {}): ConnectResult {
  return { status: "done", message: "", changed: true, restart: true, path: null, snippet: null, link: null, steps: [], ...extra };
}

const catalog = (): AppCatalog => ({
  system: "darwin",
  apps: [
    app("claude-desktop", "Claude Desktop", { installed: true }),
    app("cursor", "Cursor", { installed: true }),
    app("zed", "Zed", { method: "manual" }),
    app("windsurf", "Windsurf"),
    app("other", "Other MCP app", { method: "manual" }),
  ],
  custom: [],
});

const { api } = vi.hoisted(() => ({
  api: {
    apps: vi.fn(),
    connectApp: vi.fn(),
    disconnectApp: vi.fn(),
    addConnector: vi.fn(),
    // refresh() after an action
    state: vi.fn(async () => null),
    memories: vi.fn(async () => []),
    proposals: vi.fn(async () => []),
    connectors: vi.fn(async () => []),
  },
}));
vi.mock("../api", () => ({ api }));

describe("ConnectApps", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.apps.mockImplementation(async () => catalog());
    store.connectors = [];
    store.toast = null;
  });

  it("lists the apps on this Mac first and folds the rest away when compact", async () => {
    const w = mount(ConnectApps, { props: { compact: true } });
    await flushPromises();
    expect(w.text()).toContain("Claude Desktop");
    expect(w.text()).toContain("Cursor");
    expect(w.text()).not.toContain("Windsurf");
    const more = w.findAll("button").find((b) => b.text().includes("Show all 2 apps"))!;
    await more.trigger("click");
    expect(w.text()).toContain("Windsurf");
    expect(w.text()).toContain("Not found on this Mac");
    expect(w.text()).toContain("Other MCP app");
  });

  it("connects with one click and says what's left to do", async () => {
    api.connectApp.mockImplementation(async () => ({
      result: result({ message: "Added OpenMyMemo to ~/.cursor/mcp.json. Restart Cursor to finish." }),
      app: app("cursor", "Cursor", { installed: true, connected: true, via: "config", has_connector: true }),
    }));
    const w = mount(ConnectApps, { props: { compact: true } });
    await flushPromises();
    const row = w.findAll("button").filter((b) => b.text() === "Connect");
    expect(row).toHaveLength(2);
    await row[1].trigger("click");
    await flushPromises();
    expect(api.connectApp).toHaveBeenCalledWith("cursor");
    expect(w.emitted("connected")).toEqual([["cursor"]]);
    expect(w.text()).toContain("Connected — restart Cursor to finish");
    expect(w.findAll("button").filter((b) => b.text() === "Disconnect")).toHaveLength(1);
  });

  it("offers to connect again when only the connector is left of an app's setup", async () => {
    api.apps.mockImplementation(async () => ({
      ...catalog(),
      apps: [app("cursor", "Cursor", { installed: true, connected: false, has_connector: true })],
    }));
    const w = mount(ConnectApps, { props: { compact: true } });
    await flushPromises();
    expect(w.findAll("button").filter((b) => b.text() === "Connect")).toHaveLength(1);
    expect(w.findAll("button").filter((b) => b.text() === "Disconnect")).toHaveLength(0);
    expect(w.text()).not.toContain("restart Cursor");
  });

  it("lists an app added by hand under the name Other like any other", async () => {
    const other = {
      id: "other", name: "Other", kind: "stdio" as const, read_topics: "all" as const, include_private: false,
      write: "propose" as const, blocked_topics: [], token_preview: null, has_token: false, created_at: "",
      last_seen: null, reads: 0, proposals: 0,
    };
    api.apps.mockImplementation(async () => ({ ...catalog(), custom: [other] }));
    const w = mount(ConnectApps, { props: { compact: false }, slots: { manage: () => h("div", "permissions") } });
    await flushPromises();
    const manage = w.find("button[aria-label='Permissions for Other']");
    expect(manage.exists()).toBe(true);
    await manage.trigger("click");
    expect(w.emitted("manage")).toEqual([["other"]]);
  });

  it("ignores a list that was asked for before the last action", async () => {
    api.connectApp.mockImplementation(async () => ({
      result: result(),
      app: app("cursor", "Cursor", { installed: true, connected: true, via: "config", has_connector: true }),
    }));
    const w = mount(ConnectApps, { props: { compact: true } });
    await flushPromises();
    let answer: (c: AppCatalog) => void = () => {};
    api.apps.mockImplementationOnce(() => new Promise<AppCatalog>((resolve) => (answer = resolve)));
    void (w.vm as unknown as { reload: () => Promise<void> }).reload(); // still on its way when Connect is clicked
    await w.findAll("button").filter((b) => b.text() === "Connect")[1].trigger("click");
    await flushPromises();
    expect(w.text()).toContain("Connected — restart Cursor to finish");
    answer(catalog()); // the answer from before connecting arrives last
    await flushPromises();
    expect(w.text()).toContain("Connected — restart Cursor to finish");
  });

  it("shows the snippet, where it goes and the install link for an app set up by hand", async () => {
    api.connectApp.mockImplementation(async () => ({
      result: result({
        status: "manual", changed: false, restart: false, message: "Paste this into Zed's settings.",
        snippet: '{"context_servers": {}}', path: "/Users/me/.config/zed/settings.json",
        steps: ["Restart Zed afterwards."], link: { label: "Install in Zed", url: "zed://x" },
      }),
      app: app("zed", "Zed", { method: "manual" }),
    }));
    const w = mount(ConnectApps, { props: { compact: false } });
    await flushPromises();
    const setUp = w.findAll("button").find((b) => b.text() === "Set up")!;
    await setUp.trigger("click");
    await flushPromises();
    expect(w.text()).toContain("Paste this into Zed's settings.");
    expect(w.find("pre").text()).toBe('{"context_servers": {}}');
    expect(w.text()).toContain("/Users/me/.config/zed/settings.json");
    expect(w.find("a[href='zed://x']").text()).toBe("Install in Zed");
    expect(w.text()).toContain("Restart Zed afterwards.");
    expect(w.emitted("connected")).toBeUndefined(); // nothing is connected until the person pastes it
  });

  it("offers permissions for connected apps only when the page provides them", async () => {
    const connector = {
      id: "cursor", name: "Cursor", kind: "stdio" as const, read_topics: "all" as const, include_private: false,
      write: "propose" as const, blocked_topics: [], token_preview: null, has_token: false, created_at: "",
      last_seen: "2026-10-12T08:00:00Z", reads: 3, proposals: 0,
    };
    api.apps.mockImplementation(async () => ({
      ...catalog(),
      apps: [app("cursor", "Cursor", { installed: true, connected: true, has_connector: true, in_use: true,
                                       last_seen: connector.last_seen, connector })],
    }));
    const plain = mount(ConnectApps, { props: { compact: true } });
    await flushPromises();
    expect(plain.text()).toContain("In use · seen");
    expect(plain.text()).not.toContain("Permissions");

    const page = mount(ConnectApps, {
      props: { open: "cursor" },
      slots: { manage: ({ connector: c }: { connector: { id: string } }) => h("p", `manage ${c.id}`) },
    });
    await flushPromises();
    expect(page.text()).toContain("Permissions");
    expect(page.text()).toContain("manage cursor");
  });
});
