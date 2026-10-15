import { flushPromises, mount, type VueWrapper } from "@vue/test-utils";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { h } from "vue";
import type { AppCatalog, AppStatus, CloudApp, CloudStatus, Connector } from "../api";
import { store } from "../store";
import CloudRow from "./CloudRow.vue";
import ConnectApps from "./ConnectApps.vue";

const ENDPOINT = "https://api.mymemo.ai/openmymemo/mcp";

function status(extra: Partial<CloudStatus> = {}): CloudStatus {
  return {
    linked: false, service: "https://api.mymemo.ai", endpoint: ENDPOINT, pending: false, link_error: null,
    folder: null, this_folder: false, account: null, plan: null, linked_at: null, state: "stopped", reason: null,
    online: false, close_code: null, last_call_at: null, queued: null, apps: [], ...extra,
  };
}

const linked = (extra: Partial<CloudStatus> = {}) => status({
  linked: true, folder: "/Users/sam/OpenMyMemo", this_folder: true, account: { display_name: "Sam", email_masked: "s***@example.com" },
  plan: { tier: "ultra", live: true, queue: true }, state: "online", online: true, ...extra,
});

function connector(id: string, name: string): Connector {
  return {
    id, name, kind: "cloud", read_topics: "all", include_private: false, write: "propose", blocked_topics: [],
    token_preview: null, has_token: false, created_at: "", last_seen: null, reads: 0, proposals: 0,
  };
}

const chatgpt: CloudApp = { id: "cloud-chatgpt", name: "ChatGPT (web)", revoked: false, last_seen: null,
                            connector: connector("cloud-chatgpt", "ChatGPT (web)") };
const claude: CloudApp = { id: "cloud-claude", name: "Claude (web)", revoked: true, last_seen: null, connector: null };

const { api } = vi.hoisted(() => ({
  api: {
    cloud: vi.fn(),
    cloudLink: vi.fn(),
    cloudUnlink: vi.fn(),
    cloudReconnect: vi.fn(),
    cloudAllow: vi.fn(),
    apps: vi.fn(),
    // refresh() after an action
    state: vi.fn(async () => null),
    memories: vi.fn(async () => []),
    proposals: vi.fn(async () => []),
    connectors: vi.fn(async () => []),
  },
}));
vi.mock("../api", () => ({ api }));

const button = (w: VueWrapper, text: string) => w.findAll("button").find((b) => b.text() === text);

describe("CloudRow", () => {
  let wrapper: VueWrapper | null = null;
  const mountRow = (props = {}, slots = {}) => (wrapper = mount(CloudRow, { props, slots }));

  beforeEach(() => {
    vi.clearAllMocks();
    store.connectors = [];
    store.toast = null;
  });
  afterEach(() => {
    wrapper?.unmount();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("offers to sign in, opens MyMemo in the browser and waits for it", async () => {
    api.cloud.mockResolvedValueOnce(status()).mockResolvedValue(status({ pending: true }));
    api.cloudLink.mockResolvedValue({ url: "https://api.mymemo.ai/openmymemo/oauth/authorize?state=s" });
    const open = vi.spyOn(window, "open").mockImplementation(() => null);
    const w = mountRow();
    await flushPromises();
    expect(w.text()).toContain("MyMemo Cloud");
    expect(w.text()).toContain("Use your memory in ChatGPT and claude.ai on the web");
    expect(w.text()).toContain("Part of MyMemo Pro and Ultra");
    await button(w, "Sign in")!.trigger("click");
    await flushPromises();
    expect(open).toHaveBeenCalledWith("https://api.mymemo.ai/openmymemo/oauth/authorize?state=s", "_blank", "noopener");
    expect(w.text()).toContain("Waiting for you to sign in to MyMemo in your browser");
    expect(w.find("a[href='https://api.mymemo.ai/openmymemo/oauth/authorize?state=s']").text())
      .toBe("Open the sign-in page again");
  });

  it("shows the account, plan and connection, and the setup for ChatGPT and claude.ai once linked", async () => {
    api.cloud.mockResolvedValueOnce(status({ pending: true })).mockResolvedValue(linked());
    const w = mountRow();
    await flushPromises();
    await (w.vm as unknown as { reload: () => Promise<void> }).reload(); // the browser came back
    await flushPromises();
    expect(w.text()).toContain("MyMemo Cloud · Ultra · live relay + offline queue");
    expect(w.text()).toContain("Online · Signed in as Sam · s***@example.com"); // whose account, unmistakably
    expect(w.find("[data-endpoint]").text()).toBe(ENDPOINT); // exactly, no trailing slash
    expect(w.text()).toContain("Developer Mode");
    expect(w.text()).toContain("can depend on your ChatGPT plan");
    expect(w.text()).toContain("Settings → Connectors → Add custom connector");
    expect(button(w, "Unlink")).toBeTruthy();
    expect(button(w, "Sign in")).toBeUndefined();
  });

  it("lists the web apps as connector rows, with permissions, and lets a removed one back in", async () => {
    api.cloud.mockResolvedValue(linked({ apps: [chatgpt, claude] }));
    api.cloudAllow.mockResolvedValue(linked({ apps: [chatgpt, { ...claude, revoked: false }] }));
    const w = mountRow({ open: "cloud-chatgpt" }, {
      manage: ({ connector: c }: { connector: Connector }) => h("p", `manage ${c.id}`),
    });
    await flushPromises();
    expect(w.text()).toContain("ChatGPT (web)");
    expect(w.text()).toContain("Through MyMemo Cloud");
    expect(w.text()).toContain("manage cloud-chatgpt");
    expect(w.text()).toContain("Claude (web)");
    expect(w.text()).toContain("Access removed");
    await button(w, "Allow again")!.trigger("click");
    await flushPromises();
    expect(api.cloudAllow).toHaveBeenCalledWith("cloud-claude");
    expect(button(w, "Allow again")).toBeUndefined();
  });

  it("says why it isn't connected and offers the fix", async () => {
    api.cloud.mockResolvedValue(linked({ online: false, state: "stopped", reason: "replaced" }));
    api.cloudReconnect.mockResolvedValue(linked());
    const w = mountRow();
    await flushPromises();
    expect(w.text()).toContain("Another OpenMyMemo on this computer took over");
    await button(w, "Use this one")!.trigger("click");
    await flushPromises();
    expect(api.cloudReconnect).toHaveBeenCalled();
    expect(w.text()).toContain("Online");
    w.unmount();

    api.cloud.mockResolvedValue(linked({ online: false, state: "waiting", reason: "plan" }));
    const plan = mountRow();
    await flushPromises();
    expect(plan.text()).toContain("Your MyMemo plan doesn't include MyMemo Cloud");
    expect(button(plan, "Try again")).toBeTruthy();
    plan.unmount();

    api.cloud.mockResolvedValue(linked({ online: false, state: "waiting", reason: "refused" }));
    const refused = mountRow();
    await flushPromises();
    expect(refused.text()).toContain("MyMemo turned the connection away; trying again");
    expect(button(refused, "Try again")).toBeTruthy();
    refused.unmount();

    api.cloud.mockResolvedValue(linked({ this_folder: false, folder: "/Users/sam/Old", online: false }));
    const moved = mountRow();
    await flushPromises();
    expect(moved.text()).toContain("Linked to another folder: /Users/sam/Old");
    expect(button(moved, "Link again")).toBeTruthy();
    expect(moved.find("[data-endpoint]").exists()).toBe(false);
  });

  it("says how many changes wait in the offline queue", async () => {
    api.cloud.mockResolvedValue(linked({ queued: 3 }));
    const w = mountRow();
    await flushPromises();
    expect(w.find("[data-queued]").text()).toContain("3 changes from the web, made while this computer was offline");
    api.cloud.mockResolvedValue(linked({ queued: 0 }));
    await (w.vm as unknown as { reload: () => Promise<void> }).reload();
    await flushPromises();
    expect(w.find("[data-queued]").exists()).toBe(false);
  });

  it("unlinks after asking", async () => {
    api.cloud.mockResolvedValue(linked());
    api.cloudUnlink.mockResolvedValue({ message: "Unlinked this computer from MyMemo Cloud.", cloud: status() });
    vi.stubGlobal("confirm", vi.fn(() => true));
    const w = mountRow();
    await flushPromises();
    await button(w, "Unlink")!.trigger("click");
    await flushPromises();
    expect(api.cloudUnlink).toHaveBeenCalled();
    expect(store.toast?.text).toBe("Unlinked this computer from MyMemo Cloud.");
    expect(button(w, "Sign in")).toBeTruthy();
  });
});

describe("ConnectApps with MyMemo Cloud", () => {
  function app(id: string, name: string, extra: Partial<AppStatus> = {}): AppStatus {
    return {
      id, name, method: "file", docs: "", site: null, note: null, session: false, restart_name: name, installed: false,
      connected: false, via: null, plugin: null, broken: false, revoked: false, in_use: false, last_seen: null,
      has_connector: false, connector: null, ...extra,
    };
  }
  const catalog = (): AppCatalog => ({
    system: "darwin",
    apps: [
      app("cursor", "Cursor", { installed: true }),
      app("windsurf", "Windsurf"),
      app("mymemo-cloud", "MyMemo Cloud", { method: "account", description: "Use your memory in ChatGPT and claude.ai on the web" }),
    ],
    custom: [],
  });

  beforeEach(() => {
    vi.clearAllMocks();
    api.apps.mockImplementation(async () => catalog());
    api.cloud.mockResolvedValue(status());
    store.connectors = [];
  });

  it("puts MyMemo Cloud in its own section on the Connectors page, and leaves it out of the first-run guide", async () => {
    const page = mount(ConnectApps, { props: {} });
    await flushPromises();
    const labels = page.findAll("h3").map((x) => x.text());
    expect(labels).toEqual(["On this Mac", "On the web", "More apps"]);
    expect(page.findAll("button").filter((b) => b.text() === "Sign in")).toHaveLength(1);
    expect(page.findAll("button").filter((b) => b.text() === "Connect")).toHaveLength(2); // not for MyMemo Cloud
    page.unmount();

    const guide = mount(ConnectApps, { props: { compact: true } });
    await flushPromises();
    expect(guide.text()).not.toContain("MyMemo Cloud");
    guide.unmount();
  });
});
