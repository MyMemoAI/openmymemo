import { enableAutoUnmount, flushPromises, mount } from "@vue/test-utils";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { AssistantStatus, Memory, State, TellPreview } from "../api";
import { store } from "../store";
import TellBox from "./TellBox.vue";

const { api } = vi.hoisted(() => ({
  api: { assistant: vi.fn(), tellPreview: vi.fn(), tellApply: vi.fn(), state: vi.fn(), memories: vi.fn(),
         proposals: vi.fn(async () => []), connectors: vi.fn(async () => []) },
}));
vi.mock("../api", () => ({ api }));

enableAutoUnmount(afterEach);

const status = (extra: Partial<AssistantStatus> = {}) =>
  ({ enabled: true, ready: true, local: false, kind: "mymemo", host: "", features: { tell: true }, ...extra }) as AssistantStatus;

const preview: TellPreview = {
  by: "assistant", skipped: "The weather remark.", private_skipped: 1, note: null,
  changes: [
    { op: "update", memory_id: "m1", content: "Lives in Berlin", topic: null, kind: null, reason: "moved",
      current: "Lives in Lyon", version: 3 },
    { op: "archive", memory_id: "m2", content: null, topic: null, kind: null, reason: "stopped", current: "Drinks coffee",
      version: 1 },
    { op: "add", memory_id: null, content: "Has a cat called Miso", topic: "interests", kind: "fact", reason: "new",
      current: null, version: null },
  ],
};

describe("Tell OpenMyMemo", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    store.state = { counts: {}, topics: [{ slug: "interests", title: "Interests", group: "about", private: false }] } as unknown as State;
    store.memories = [{ id: "m1", topic: "identity", content: "Lives in Berlin", version: 4, private: false } as Memory];
    api.state.mockImplementation(async () => store.state);
    api.memories.mockImplementation(async () => store.memories);
    api.assistant.mockImplementation(async () => status());
    api.tellPreview.mockImplementation(async () => preview);
    api.tellApply.mockImplementation(async () => ({ added: 0, updated: 1, archived: 0, ids: ["m1"], errors: [] }));
  });

  it("names where the text goes before anything is sent", async () => {
    const w = mount(TellBox);
    await flushPromises();
    expect(w.find('[data-test="tell-where"]').text()).toBe("On Preview, this is sent to MyMemo's hosted model.");
    expect(api.tellPreview).not.toHaveBeenCalled();

    api.assistant.mockImplementation(async () => status({ kind: "key", host: "api.openai.com" }));
    const k = mount(TellBox);
    await flushPromises();
    expect(k.find('[data-test="tell-where"]').text()).toBe("On Preview, this is sent to api.openai.com.");

    api.assistant.mockImplementation(async () => status({ enabled: false }));
    const off = mount(TellBox);
    await flushPromises();
    expect(off.find('[data-test="tell-where"]').text()).toContain("kept as new memories, filed by rules");
  });

  it("previews, saves only the ticked changes and says what was done", async () => {
    const w = mount(TellBox);
    await flushPromises();
    expect(w.find('[data-test="tell-preview"]').attributes("disabled")).toBeDefined();
    await w.find("textarea").setValue("I moved to Berlin, no more coffee, and my cat Miso says hi. Nice weather.");
    await w.find('[data-test="tell-preview"]').trigger("click");
    await flushPromises();
    expect(api.tellPreview).toHaveBeenCalledWith("I moved to Berlin, no more coffee, and my cat Miso says hi. Nice weather.");

    const changes = w.find('[data-test="tell-changes"]');
    expect(changes.text()).toContain("Lives in Lyon");
    expect(changes.text()).toContain("Left out: The weather remark.");
    expect(changes.text()).toContain("1 likely match is private");
    const boxes = changes.findAll('input[type="checkbox"]');
    await boxes[1].setValue(false); // keep the coffee memory
    await boxes[2].setValue(false);
    await w.find('[data-test="tell-save"]').trigger("click");
    await flushPromises();

    expect(api.tellApply).toHaveBeenCalledWith([
      { op: "update", memory_id: "m1", content: "Lives in Berlin", topic: null, kind: null, version: 3 },
    ]);
    expect(w.find('[data-test="tell-changes"]').exists()).toBe(false);
    const done = w.find('[data-test="tell-done"]');
    expect(done.text()).toContain("updated 1");
    expect(done.find("a").attributes("href")).toBe("#/memories/identity");
    expect((w.find("textarea").element as HTMLTextAreaElement).value).toBe("");
  });

  it("keeps what was typed when a change is refused", async () => {
    api.tellApply.mockImplementation(async () => ({ added: 0, updated: 0, archived: 0, ids: [],
      errors: [{ change: {}, error: "This memory changed since the preview. Preview again." }] }));
    const w = mount(TellBox);
    await flushPromises();
    await w.find("textarea").setValue("I moved to Berlin");
    await w.find('[data-test="tell-preview"]').trigger("click");
    await flushPromises();
    await w.find('[data-test="tell-save"]').trigger("click");
    await flushPromises();
    expect(w.find('[data-test="tell-done"]').text()).toContain("changed since the preview");
    expect((w.find("textarea").element as HTMLTextAreaElement).value).toBe("I moved to Berlin");
  });

  it("won't save a preview of text changed since, and a failed preview leaves nothing to save", async () => {
    const w = mount(TellBox);
    await flushPromises();
    const box = w.find("textarea");
    await box.setValue("I live in Berlin");
    await w.find('[data-test="tell-preview"]').trigger("click");
    await flushPromises();
    await box.setValue("Actually I live in Paris");
    expect(w.find('[data-test="tell-stale"]').exists()).toBe(true);
    expect(w.find('[data-test="tell-save"]').attributes("disabled")).toBeDefined();

    api.tellPreview.mockImplementation(async () => { throw new Error("offline"); });
    await w.find('[data-test="tell-preview"]').trigger("click");
    await flushPromises();
    expect(w.find('[data-test="tell-changes"]').exists()).toBe(false);
    expect((box.element as HTMLTextAreaElement).value).toBe("Actually I live in Paris");
    expect(api.tellApply).not.toHaveBeenCalled();
  });

  it("keeps an edited change to one memory's length", async () => {
    const w = mount(TellBox);
    await flushPromises();
    await w.find("textarea").setValue("I moved");
    await w.find('[data-test="tell-preview"]').trigger("click");
    await flushPromises();
    const edit = w.find('[data-test="tell-changes"] textarea');
    expect(edit.attributes("maxlength")).toBe("1000");
  });
});
