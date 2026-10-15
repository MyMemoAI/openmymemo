import { flushPromises, mount, type VueWrapper } from "@vue/test-utils";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { AppCatalog, AppStatus, CloudStatus } from "../api";
import { store } from "../store";
import ConnectorsView from "./ConnectorsView.vue";

const MYMEMO = "https://api.mymemo.ai";

/** A panel server with MyMemo Cloud: `linked`, `show` and `service` change as the page asks. */
const server = { linked: false, show: false, service: "" };

function cloudStatus(): CloudStatus {
  const service = server.service || MYMEMO;
  return {
    linked: server.linked, shown: server.show || server.linked, service, endpoint: `${service}/openmymemo/mcp`,
    settings: { show: server.show, service: server.service, default_service: MYMEMO, service_from_env: null },
    pending: false, link_error: null, folder: server.linked ? "/Users/sam/OpenMyMemo" : null, this_folder: server.linked,
    account: server.linked ? { display_name: "Sam", email_masked: "s***@example.com" } : null, plan: null, linked_at: null,
    state: server.linked ? "online" : "stopped", reason: null, online: server.linked, close_code: null, last_call_at: null,
    queued: null, apps: [],
  };
}

const cloudApp: AppStatus = {
  id: "mymemo-cloud", name: "MyMemo Cloud", method: "account", docs: "", site: null, note: null, session: false,
  restart_name: "MyMemo Cloud", installed: false, connected: false, via: null, plugin: null, broken: false,
  revoked: false, in_use: false, last_seen: null, has_connector: false, connector: null,
};

const { api } = vi.hoisted(() => ({
  api: {
    apps: vi.fn(async (): Promise<AppCatalog> => ({
      system: "darwin", apps: server.show || server.linked ? [cloudApp] : [], custom: [],
    })),
    cloud: vi.fn(async () => cloudStatus()),
    cloudUnlink: vi.fn(async () => {
      server.linked = false;
      return { message: "Unlinked this computer from MyMemo Cloud.", cloud: cloudStatus() };
    }),
    cloudSettings: vi.fn(async (body: { show?: boolean; service?: string }) => {
      if (body.service !== undefined && server.linked) throw new Error("Unlink this computer first.");
      if (body.show !== undefined) server.show = body.show;
      if (body.service !== undefined) server.service = body.service.replace(/\/+$/, "");
      return cloudStatus();
    }),
    // refresh() after an action
    state: vi.fn(async () => null),
    memories: vi.fn(async () => []),
    proposals: vi.fn(async () => []),
    connectors: vi.fn(async () => []),
  },
}));
vi.mock("../api", () => ({ api }));

const section = (w: VueWrapper) => w.get('section[aria-label="MyMemo Cloud"]');
const toggle = (w: VueWrapper) => section(w).get('[role="switch"]');

describe("Connectors page with MyMemo Cloud", () => {
  let w: VueWrapper | null = null;
  beforeEach(() => {
    vi.clearAllMocks();
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
    store.connectors = [];
    store.toast = null;
  });
  afterEach(() => {
    w?.unmount();
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("unlocks its settings when the computer is unlinked, without reloading the page", async () => {
    Object.assign(server, { linked: true, show: false, service: "" }); // linked from the CLI while hidden
    w = mount(ConnectorsView, { props: { initialConnector: null } });
    await flushPromises();
    expect(w.text()).toContain("On the web");
    expect(toggle(w).attributes("disabled")).toBeDefined();
    expect(section(w).get("input").attributes("disabled")).toBeDefined();

    vi.stubGlobal("confirm", vi.fn(() => true));
    await w.findAll("button").find((b) => b.text() === "Unlink")!.trigger("click");
    await flushPromises();
    expect(w.text()).not.toContain("On the web"); // hidden in Settings: the row goes
    expect(toggle(w).attributes("disabled")).toBeUndefined();
    expect(toggle(w).attributes("aria-checked")).toBe("false");

    await toggle(w).trigger("click");
    await flushPromises();
    expect(api.cloudSettings).toHaveBeenLastCalledWith({ show: true });
    expect(w.text()).toContain("On the web");
    const input = section(w).get("input");
    expect(input.attributes("disabled")).toBeUndefined();
    await input.setValue("https://memo.example.org");
    await section(w).get("form").trigger("submit");
    await flushPromises();
    expect(server.service).toBe("https://memo.example.org");
  });

  it("locks its settings when the computer is linked meanwhile", async () => {
    Object.assign(server, { linked: false, show: true, service: "" });
    w = mount(ConnectorsView, { props: { initialConnector: null } });
    await flushPromises();
    expect(toggle(w).attributes("disabled")).toBeUndefined();
    expect(section(w).get("input").attributes("disabled")).toBeUndefined();

    server.linked = true; // `openmymemo cloud link`, or the browser came back from signing in
    vi.advanceTimersByTime(3000); // CloudRow's poll
    await flushPromises();
    expect(toggle(w).attributes("disabled")).toBeDefined();
    expect(section(w).get("input").attributes("disabled")).toBeDefined();
    expect(w.text()).toContain("Unlink this computer to choose another service.");
  });
});
