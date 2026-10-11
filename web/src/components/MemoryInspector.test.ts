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
    api.patchMemory.mockImplementation(async (_id: string, body: { content?: string }) => ({ ...memory("A"), ...body, version: 3 }));
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

  it("keeps what the user types while an earlier save is still on its way", async () => {
    let finish: (m: Memory) => void = () => {};
    api.patchMemory.mockImplementationOnce(() => new Promise<Memory>((resolve) => (finish = resolve)));
    const w = mount(MemoryInspector, { props: { memory: memory("A") } });
    const field = w.get("textarea");
    await field.setValue("B");
    await field.trigger("blur"); // saving B…
    await field.trigger("focus");
    await field.setValue("C"); // …while the user goes on typing
    finish(memory("B", { version: 2 }));
    await flushPromises();
    await w.setProps({ memory: memory("B", { version: 2 }) }); // the refresh after the save
    expect((field.element as HTMLTextAreaElement).value).toBe("C");
    await field.trigger("blur");
    await flushPromises();
    expect(api.patchMemory).toHaveBeenLastCalledWith("m1", { content: "C" });
    expect(store.toast?.text ?? "").not.toContain("while you were typing"); // B was the user's own save
  });

  it("lets only the newest of two overlapping saves move the baseline", async () => {
    const pending: ((m: Memory) => void)[] = [];
    api.patchMemory.mockImplementation(() => new Promise<Memory>((resolve) => pending.push(resolve)));
    const w = mount(MemoryInspector, { props: { memory: memory("A") } });
    const field = w.get("textarea");
    await field.setValue("B");
    await field.trigger("blur");
    await field.setValue("C");
    await field.trigger("blur");
    pending[1](memory("C", { version: 3 })); // the newer save finishes first
    pending[0](memory("B", { version: 2 }));
    await flushPromises();
    expect((field.element as HTMLTextAreaElement).value).toBe("C");
    await w.setProps({ memory: memory("C", { version: 3 }) });
    await field.trigger("blur");
    await flushPromises();
    expect(api.patchMemory).toHaveBeenCalledTimes(2); // C is saved and clean: nothing more to write
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
