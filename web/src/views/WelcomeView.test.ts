import { flushPromises, mount, type VueWrapper } from "@vue/test-utils";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { ImportItem, Memory, State, Topic } from "../api";
import { store } from "../store";
import WelcomeView from "./WelcomeView.vue";

const topic = (slug: string, title: string): Topic => ({
  slug, title, description: null, group: "who", group_name: "Who I am", private: false, count: 0, sections: [],
});

function state(onboarded: boolean): State {
  return {
    version: "0.2.3", vault: "/Users/sam/OpenMyMemo", kinds: ["fact", "preference", "ongoing", "instruction", "event"],
    topics: [topic("identity", "Identity"), topic("work", "Work"), topic("how-to-work-with-me", "How to work with me")],
    groups: [], counts: { active: 0, archived: 0, pending: 0, unreviewed: 0, review_due: 0 },
    settings: { require_approval: false, onboarded }, mcp_url: "http://127.0.0.1:8787/mcp",
  };
}

function item(content: string, extra: Partial<ImportItem> = {}): ImportItem {
  return {
    content, topic: null, kind: "fact", created_at: null, hint: null, duplicate_of: null, similar_to: null,
    similar_content: null, include: true, ...extra,
  };
}

const { api, saved } = vi.hoisted(() => {
  const saved: Memory[] = []; // what addMemory wrote, returned by the next refresh
  return {
    saved,
    api: {
      patchSettings: vi.fn(async (body: object) => ({ require_approval: false, onboarded: false, ...body })),
      addMemory: vi.fn(async (body: { topic?: string; content: string }) => {
        const m = { id: `m${saved.length}`, topic: body.topic ?? "identity", content: body.content } as Memory;
        saved.push(m);
        return m;
      }),
      importPreview: vi.fn(async (): Promise<ImportItem[]> => []),
      importCommit: vi.fn(async () => ({ memory_ids: ["i1"] })),
      // refresh() after a write
      state: vi.fn(async (): Promise<State | null> => null),
      memories: vi.fn(async () => [...saved]),
      proposals: vi.fn(async () => []),
      connectors: vi.fn(async () => []),
    },
  };
});
vi.mock("../api", () => ({ api }));

const visibleStep = (w: VueWrapper) =>
  w.findAll("[data-step]").filter((s) => (s.element as HTMLElement).style.display !== "none").map((s) => s.attributes("data-step"));
const button = (w: VueWrapper, text: string) => {
  const found = w.findAll("button").find((b) => b.text().includes(text));
  if (!found) throw new Error(`No button "${text}"`);
  return found;
};

describe("the first-run guide", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    saved.length = 0;
    api.state.mockImplementation(async () => state(false));
    api.importPreview.mockImplementation(async () => []);
    Object.assign(store, { state: state(false), memories: [], connectors: [], toast: null });
    history.replaceState(null, "", "#/welcome");
  });

  it("writes `onboarded: false` when it opens, and walks through the five steps", async () => {
    const w = mount(WelcomeView, { props: { initialStep: null } });
    await flushPromises();
    expect(api.patchSettings).toHaveBeenCalledWith({ onboarded: false });
    expect(w.findAll("nav li")).toHaveLength(5);
    expect(w.find('[aria-current="step"]').attributes("aria-label")).toBe("Step 1: Welcome");
    expect(visibleStep(w)).toEqual(["welcome"]);
    expect(w.text()).toContain("/Users/sam/OpenMyMemo");

    for (const [next, hash] of [["connect", "#/welcome/connect"], ["import", "#/welcome/import"], ["about", "#/welcome/about"],
      ["done", "#/welcome/done"]]) {
      await button(w, "Next").trigger("click");
      await flushPromises();
      expect(visibleStep(w)).toEqual([next]);
      expect(location.hash).toBe(hash);
    }
    expect(w.find('a[href="#/connectors"]').exists()).toBe(true); // step 2 without ConnectApps
    expect(w.text()).toContain("What do you remember about me?");
    expect(api.addMemory).not.toHaveBeenCalled(); // step 4 left empty

    await button(w, "Back").trigger("click");
    expect(visibleStep(w)).toEqual(["about"]);
    await w.setProps({ initialStep: "connect" }); // the browser's back button
    expect(visibleStep(w)).toEqual(["connect"]);
  });

  it("leaves `onboarded` alone when reopened after it was finished", async () => {
    store.state = state(true);
    mount(WelcomeView, { props: { initialStep: null } });
    await flushPromises();
    expect(api.patchSettings).not.toHaveBeenCalled();
  });

  it("Skip writes `onboarded: true` and opens Memories", async () => {
    const w = mount(WelcomeView, { props: { initialStep: "import" } });
    await flushPromises();
    api.patchSettings.mockClear();
    await button(w, "Skip the guide").trigger("click");
    await flushPromises();
    expect(api.patchSettings).toHaveBeenCalledWith({ onboarded: true });
    expect(location.hash).toBe("#/memories");
  });

  it("finishing writes `onboarded: true` too", async () => {
    const w = mount(WelcomeView, { props: { initialStep: "done" } });
    await button(w, "Go to my memories").trigger("click");
    await flushPromises();
    expect(api.patchSettings).toHaveBeenLastCalledWith({ onboarded: true });
    expect(location.hash).toBe("#/memories");
  });

  it("saves each answer about the person as one memory in its topic, once", async () => {
    const w = mount(WelcomeView, { props: { initialStep: "about" } });
    await w.get('input[name="name"]').setValue("  Ethan ");
    await w.get('input[name="answers"]').setValue("简洁，先给结论");
    expect(w.text()).toContain("Saved in Identity as “Prefers to be called Ethan.”");
    await button(w, "Save and continue").trigger("click");
    await flushPromises();

    expect(api.addMemory.mock.calls.map(([body]) => body)).toEqual([
      { topic: "identity", content: "Prefers to be called Ethan.", kind: "preference" },
      { topic: "how-to-work-with-me", content: "Answer style: 简洁，先给结论.", kind: "instruction" },
    ]);
    expect(store.toast?.text).toBe("Saved 2 memories");
    expect(visibleStep(w)).toEqual(["done"]);

    await button(w, "Back").trigger("click"); // and on again: nothing is added twice
    await w.get('input[name="work"]').setValue("Runs a bakery in Lyon.");
    await button(w, "Save and continue").trigger("click");
    await flushPromises();
    expect(api.addMemory).toHaveBeenCalledTimes(3);
    expect(api.addMemory).toHaveBeenLastCalledWith({ topic: "work", content: "Work: Runs a bakery in Lyon.", kind: "fact" });
    await button(w, "Back").trigger("click");
    expect(button(w, "Next").exists()).toBe(true);
  });

  it("moves to the next field with Enter and saves from the last one", async () => {
    const w = mount(WelcomeView, { props: { initialStep: "about" }, attachTo: document.body });
    await w.get('input[name="name"]').setValue("Sam");
    await w.get('input[name="name"]').trigger("keydown", { key: "Enter" });
    expect(document.activeElement?.getAttribute("name")).toBe("work");
    expect(api.addMemory).not.toHaveBeenCalled();
    await w.get('input[name="answers"]').trigger("keydown", { key: "Enter" });
    await flushPromises();
    expect(api.addMemory).toHaveBeenCalledWith({ topic: "identity", content: "Prefers to be called Sam.", kind: "preference" });
    w.unmount();
  });

  it("imports pasted memory into from-<source> through the shared paste box", async () => {
    api.importPreview.mockImplementation(async () => [
      item("Lives in Lyon"),
      item("Prefers to be called Ethan", { duplicate_of: "m1", include: false }),
    ]);
    const w = mount(WelcomeView, { props: { initialStep: "import" } });
    await button(w, "Claude").trigger("click");
    expect(w.text()).toContain("as [YYYY-MM-DD] – memory");
    await w.get("textarea").setValue("[2026-01-02] – Lives in Lyon\n[2026-01-03] – Prefers to be called Ethan");
    await button(w, "Preview").trigger("click");
    await flushPromises();
    expect(api.importPreview).toHaveBeenCalledWith("claude", expect.stringContaining("Lives in Lyon"));
    expect(w.text()).toContain("They go into From Claude");
    expect(w.text()).toContain("Already have");

    await button(w, "Import 1").trigger("click");
    await flushPromises();
    expect(api.importCommit).toHaveBeenCalledWith("claude", expect.arrayContaining([expect.objectContaining({ content: "Lives in Lyon" })]));
    expect(w.text()).toContain("Imported 1 memory into From Claude");
    await button(w, "Import from another app").trigger("click");
    expect(w.find("textarea").exists()).toBe(true);
  });

  it("keeps the paste box when nothing was found in the text", async () => {
    const w = mount(WelcomeView, { props: { initialStep: "import" } });
    await w.get("textarea").setValue("ok");
    await button(w, "Preview").trigger("click");
    await flushPromises();
    expect(store.toast?.text).toBe("No memories found in that text.");
    expect((w.get("textarea").element as HTMLTextAreaElement).value).toBe("ok");
  });
});
