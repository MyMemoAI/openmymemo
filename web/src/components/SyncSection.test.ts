import { flushPromises, mount, type VueWrapper } from "@vue/test-utils";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { CloudStatus, CloudSync } from "../api";
import { store } from "../store";
import SyncSection from "./SyncSection.vue";

function sync(extra: Partial<CloudSync> = {}): CloudSync {
  return {
    available: true, enabled: false, state: "off", reason: null, merge: null, preview: null, picked: [],
    private_topics: [{ slug: "health", title: "Health" }, { slug: "finance", title: "Finance" }], last_sync_at: null,
    skipped: 0, off_by: null, pending_off: false, consent_version: "2026-10-26", consent_at: null, ...extra,
  };
}

const status = (s: CloudSync) => ({ linked: true, sync: s }) as unknown as CloudStatus;

const { api } = vi.hoisted(() => ({
  api: { cloudSync: vi.fn(), cloudSyncTopics: vi.fn(), cloudSyncMerge: vi.fn() },
}));
vi.mock("../api", () => ({ api }));

const button = (w: VueWrapper, text: string) => w.findAll("button").find((b) => b.text() === text);
const inBody = (text: string) => [...document.body.querySelectorAll("button")].find((b) => b.textContent?.trim() === text);

describe("SyncSection", () => {
  let wrapper: VueWrapper | null = null;
  const mountIt = (s: CloudSync) => (wrapper = mount(SyncSection, { props: { sync: s }, attachTo: document.body }));

  beforeEach(() => {
    vi.clearAllMocks();
    store.toast = null;
  });
  afterEach(() => {
    wrapper?.unmount();
    document.body.innerHTML = "";
  });

  it("asks for consent, naming what MyMemo stores and how to delete it, with the private-topic picker", async () => {
    api.cloudSync.mockResolvedValue(status(sync({ enabled: true, state: "waiting", picked: ["health"] })));
    const w = mountIt(sync());
    expect(w.text()).toContain("MyMemo Memory sync");
    expect(w.text()).toContain("Keep a copy of this folder in your MyMemo Memory");
    await button(w, "Turn on…")!.trigger("click");
    const consent = document.body.querySelector("[data-consent]")!;
    expect(consent.textContent).toContain("MyMemo stores your memories outside private topics");
    expect(consent.textContent).toContain("never gets memories you marked private");
    expect(consent.textContent).toContain("Your folder wins");
    expect(consent.textContent).toContain("Also delete the copy in MyMemo");
    const boxes = consent.querySelectorAll<HTMLInputElement>("input[type=checkbox]");
    expect([...boxes].map((b) => b.checked)).toEqual([false, false]); // private topics stay unless picked
    boxes[0].click();
    inBody("Turn on sync")!.click();
    await flushPromises();
    expect(api.cloudSync).toHaveBeenCalledWith({ on: true, consent: true, private_topics: ["health"] });
    expect(w.emitted("changed")).toHaveLength(1);
    expect(store.toast?.text).toBe("MyMemo Memory sync is on");
  });

  it("shows the first merge's preview and starts syncing", async () => {
    api.cloudSyncMerge.mockResolvedValue(status(sync({ enabled: true, state: "merging" })));
    const preview = { cloud: 23, here: 140, same: 11, similar: 2, new: 10 };
    const w = mountIt(sync({ enabled: true, state: "preview", merge: "preview", preview }));
    expect(w.find("[data-preview]").text()).toContain("MyMemo has 23 memories: 11 are already here, 2 look like ones you have and 10 are new. Your 140 memories will be copied to MyMemo");
    await button(w, "Start syncing")!.trigger("click");
    await flushPromises();
    expect(api.cloudSyncMerge).toHaveBeenCalled();
  });

  it("waits for MyMemo before it can preview", () => {
    const w = mountIt(sync({ enabled: true, state: "preview", merge: "preview" }));
    expect(w.text()).toContain("Getting what MyMemo has…");
    expect(button(w, "Start syncing")!.attributes("disabled")).toBeDefined();
  });

  it("says when MyMemo refused it, and why", () => {
    const w = mountIt(sync({ enabled: true, state: "refused", reason: "memory_off" }));
    expect(w.text()).toContain("On · MyMemo refused it");
    expect(w.text()).toContain("MyMemo Memory is off for your account");
  });

  it("shows when it last synced, what MyMemo didn't keep, and changes the picked topics", async () => {
    api.cloudSyncTopics.mockResolvedValue(status(sync({ enabled: true, state: "syncing", picked: [] })));
    const w = mountIt(sync({ enabled: true, state: "syncing", last_sync_at: new Date().toISOString(), skipped: 2, picked: ["health"] }));
    expect(w.text()).toContain("Synced");
    expect(w.text()).toContain("2 memories aren't kept in MyMemo");
    await button(w, "Private topics…")!.trigger("click");
    const boxes = document.body.querySelectorAll<HTMLInputElement>("input[type=checkbox]");
    expect([...boxes].map((b) => b.checked)).toEqual([true, false]);
    boxes[0].click();
    inBody("Save")!.click();
    await flushPromises();
    expect(api.cloudSyncTopics).toHaveBeenCalledWith([]);
  });

  it("turns it off, deleting the copy in MyMemo when asked", async () => {
    api.cloudSync.mockResolvedValue(status(sync({ pending_off: true })));
    const w = mountIt(sync({ enabled: true, state: "syncing" }));
    await button(w, "Turn off…")!.trigger("click");
    expect(document.body.textContent).toContain("Nothing more goes to MyMemo");
    document.body.querySelector<HTMLInputElement>("[data-delete-copy]")!.click();
    inBody("Turn off")!.click();
    await flushPromises();
    expect(api.cloudSync).toHaveBeenCalledWith({ on: false, delete_copy: true });
  });

  it("says when sync was turned off on MyMemo", () => {
    const w = mountIt(sync({ off_by: "mymemo" }));
    expect(w.text()).toContain("Sync was turned off on MyMemo's memory page.");
  });
});
