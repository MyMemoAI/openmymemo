import { flushPromises, mount } from "@vue/test-utils";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { Memory } from "../api";
import { store } from "../store";
import MemoryInspector from "./MemoryInspector.vue";

const { api } = vi.hoisted(() => ({
  api: {
    history: vi.fn(async () => []),
    patchMemory: vi.fn(async (_id: string, body: { content?: string }) => ({ ...memory("A"), ...body, version: 3 })),
    // refresh() after a write
    state: vi.fn(async () => null),
    memories: vi.fn(async () => []),
    proposals: vi.fn(async () => []),
    connectors: vi.fn(async () => []),
  },
}));
vi.mock("../api", () => ({ api }));

function memory(content: string, extra: Partial<Memory> = {}): Memory {
  return {
    id: "m1", content, topic: "work", section: null, kind: "fact", source: "user", last_actor: "user",
    created_at: "2026-10-01T00:00:00Z", updated_at: "2026-10-01T00:00:00Z", version: 1, status: "active",
    pinned: false, private: false, effective_private: false, until: null, review_due: false, evidence: null,
    archived_at: null, ...extra,
  };
}

/** What a refresh does when another app changed the memory: the parent passes the new version down. */
const changedElsewhere = (content: string) => memory(content, { version: 2, last_actor: "connector:cursor" });

describe("the inspector's text field", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    store.toast = null;
  });

  it("doesn't write anything back when the user only focused it", async () => {
    const w = mount(MemoryInspector, { props: { memory: memory("A") } });
    const field = w.get("textarea");
    await field.trigger("focus");
    await w.setProps({ memory: changedElsewhere("B") });
    expect((field.element as HTMLTextAreaElement).value).toBe("B"); // an untouched draft follows the vault
    await field.trigger("blur");
    await flushPromises();
    expect(api.patchMemory).not.toHaveBeenCalled();
  });

  it("keeps what the user typed when another app changes the memory meanwhile, and says so", async () => {
    const w = mount(MemoryInspector, { props: { memory: memory("A") } });
    const field = w.get("textarea");
    await field.trigger("focus");
    await field.setValue("A, in my words");
    await w.setProps({ memory: changedElsewhere("B") });
    expect((field.element as HTMLTextAreaElement).value).toBe("A, in my words");
    await field.trigger("blur");
    await flushPromises();
    expect(api.patchMemory).toHaveBeenCalledWith("m1", { content: "A, in my words" });
    expect(store.toast?.text).toContain("cursor changed this memory while you were typing");
  });

  it("writes an edit made without anyone else changing the memory", async () => {
    const w = mount(MemoryInspector, { props: { memory: memory("A") } });
    const field = w.get("textarea");
    await field.setValue("A2");
    await field.trigger("blur");
    await flushPromises();
    expect(api.patchMemory).toHaveBeenCalledWith("m1", { content: "A2" });
    expect(store.toast?.text ?? "").not.toContain("while you were typing");
  });

  it("writes nothing when the text is typed back to what it was", async () => {
    const w = mount(MemoryInspector, { props: { memory: memory("A") } });
    const field = w.get("textarea");
    await field.setValue("A ");
    await field.trigger("blur");
    await flushPromises();
    expect(api.patchMemory).not.toHaveBeenCalled();
  });
});
