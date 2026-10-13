import { flushPromises, mount } from "@vue/test-utils";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { State } from "./api";
import App from "./App.vue";
import { store } from "./store";

function state(onboarded: boolean): State {
  return {
    version: "0.2.3", vault: "/Users/sam/OpenMyMemo", kinds: ["fact"], topics: [], groups: [],
    counts: { active: 0, archived: 0, pending: 0, unreviewed: 0, review_due: 0 },
    settings: { require_approval: false, onboarded }, mcp_url: "http://127.0.0.1:8787/mcp",
  };
}

const { api } = vi.hoisted(() => ({
  api: {
    state: vi.fn(),
    memories: vi.fn(async () => []),
    proposals: vi.fn(async () => []),
    connectors: vi.fn(async () => []),
    patchSettings: vi.fn(async () => ({})),
  },
}));
vi.mock("./api", () => ({ api }));

describe("the home page", () => {
  let wrapper: ReturnType<typeof mount> | undefined;
  beforeEach(() => {
    vi.clearAllMocks();
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

  it("is Memories once the guide is done, and Get started is gone", async () => {
    const w = await open("", true);
    expect(location.hash).toBe("#/memories");
    expect(w.find('nav a[href="#/welcome"]').exists()).toBe(false);
  });

  it("doesn't take over a page the address names", async () => {
    await open("#/memories", false);
    expect(location.hash).toBe("#/memories");
  });
});
