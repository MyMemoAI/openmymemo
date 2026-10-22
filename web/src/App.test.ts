import { flushPromises, mount } from "@vue/test-utils";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Proposal, State } from "./api";
import App from "./App.vue";
import { store } from "./store";

function state(onboarded: boolean): State {
  return {
    version: "0.2.3", vault: "/Users/sam/OpenMyMemo", kinds: ["fact"], topics: [], groups: [],
    counts: { active: 0, archived: 0, pending: 0, unreviewed: 0, review_due: 0 },
    settings: { require_approval: false, onboarded }, mcp_url: "http://127.0.0.1:8787/mcp",
  };
}

/** A change an AI app made that the person hasn't looked at yet: the digest. */
const unreviewed: Proposal = {
  id: "p1", connector_id: "claude-desktop", connector_name: "Claude Desktop", op: "add", content: "Lives in Lyon",
  topic: "identity", memory_id: null, kind: "fact", reason: null, evidence: null, created_at: "2026-10-12T00:00:00Z",
  status: "applied", hold: null, decided_at: "2026-10-12T00:00:00Z", reviewed_at: null, final_content: "Lives in Lyon",
  final_topic: "identity", applied_memory_id: "m1", applied_version: 1, before_content: null, before_topic: null,
  original_op: null, similarity: null, conflict: false, can_undo: true,
};

const overview = {
  first_run: true, safety: { score: 85, level: "Good", week_change: null, top: [] },
  waiting: { pending: 0, unreviewed: 1, review_due: 0 },
  memories: { total: 0, private: 0, groups: [], growth: [], sources: {} }, apps: [], activity: [],
};

const { api } = vi.hoisted(() => ({
  api: {
    state: vi.fn(),
    memories: vi.fn(async () => []),
    proposals: vi.fn(async (_q?: { unreviewed?: boolean }): Promise<Proposal[]> => []),
    connectors: vi.fn(async () => []),
    patchSettings: vi.fn(async () => ({})),
    apps: vi.fn(async () => ({ system: "darwin", apps: [], custom: [] })), // ConnectApps in the guide
    overview: vi.fn(async () => overview), // Home
    brief: vi.fn(async () => { throw new Error("no brief"); }), // Home without its Today card
  },
}));
vi.mock("./api", () => ({ api }));

describe("the home page", () => {
  let wrapper: ReturnType<typeof mount> | undefined;
  beforeEach(() => {
    vi.clearAllMocks();
    api.proposals.mockImplementation(async () => []);
    store.state = null;
  });
  afterEach(() => {
    wrapper?.unmount(); // stops the 4-second refresh
    wrapper = undefined;
  });

  async function open(hash: string, onboarded: boolean) {
    history.replaceState(null, "", hash || location.pathname);
    api.state.mockImplementation(async () => state(onboarded));
    wrapper = mount(App, { attachTo: document.body });
    await flushPromises();
    return wrapper;
  }

  it("is the first-run guide while the folder hasn't had it, with Get started in the navigation", async () => {
    const w = await open("", false);
    expect(location.hash).toBe("#/welcome");
    expect(w.text()).toContain("Welcome to OpenMyMemo");
    expect(w.find('nav a[href="#/welcome"]').text()).toBe("Get started");
  });

  it("is Home when an AI app changed something, even before the guide, which Get started still offers", async () => {
    api.proposals.mockImplementation(async (q) => (q?.unreviewed ? [unreviewed] : []));
    const w = await open("", false);
    expect(location.hash).toBe("#/home");
    expect(w.find('[data-test="waiting"]').text()).toContain("1 change by an AI app you haven't looked at");
    expect(w.find('nav a[href="#/welcome"]').text()).toBe("Get started");
  });

  it("is Home once the guide is done, first in the navigation, and Get started is gone", async () => {
    const w = await open("", true);
    expect(location.hash).toBe("#/home");
    expect(w.find("nav a").attributes("href")).toBe("#/home");
    expect(w.find('nav a[href="#/welcome"]').exists()).toBe(false);
  });

  it("doesn't take over a page the address names", async () => {
    await open("#/memories", false);
    expect(location.hash).toBe("#/memories");
  });
});
