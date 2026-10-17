import { flushPromises, mount, type VueWrapper } from "@vue/test-utils";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { Memory } from "../api";
import { store } from "../store";
import MemoryRow from "./MemoryRow.vue";

const { api } = vi.hoisted(() => ({
  api: {
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

/** What a refresh does when another app changed the memory: the list passes the new version down. */
const changedElsewhere = (content: string) => memory(content, { version: 2, last_actor: "connector:cursor" });

async function edit(w: VueWrapper) {
  await w.get("p").trigger("dblclick");
  return w.get("textarea");
}

describe("editing a memory in the list", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.patchMemory.mockImplementation(async (_id: string, body: { content?: string }) => ({ ...memory("A"), ...body, version: 3 }));
    store.toast = null;
  });

  it("writes nothing when the edit box is left without typing, even if another app changed the memory", async () => {
    const w = mount(MemoryRow, { props: { memory: memory("A") } });
    const box = await edit(w);
    await w.setProps({ memory: changedElsewhere("B") });
    await box.trigger("blur");
    await flushPromises();
    expect(api.patchMemory).not.toHaveBeenCalled();
  });

  it("saves what the user typed when another app changed the memory meanwhile, and says so", async () => {
    const w = mount(MemoryRow, { props: { memory: memory("A") } });
    const box = await edit(w);
    await box.setValue("A, in my words");
    await w.setProps({ memory: changedElsewhere("B") });
    await box.trigger("blur");
    await flushPromises();
    expect(api.patchMemory).toHaveBeenCalledWith("m1", { content: "A, in my words" });
    expect(store.toast?.text).toContain("cursor changed this memory while you were typing");
  });

  it("saves once when Return closes the box", async () => {
    const w = mount(MemoryRow, { props: { memory: memory("A") } });
    const box = await edit(w);
    await box.setValue("A2");
    await box.trigger("keydown", { key: "Enter" });
    await box.trigger("blur");
    await flushPromises();
    expect(api.patchMemory).toHaveBeenCalledTimes(1);
    expect(api.patchMemory).toHaveBeenCalledWith("m1", { content: "A2" });
    expect(store.toast?.text ?? "").not.toContain("while you were typing");
  });

  it("queues a second edit behind a save that is still on its way, even typed back to the original", async () => {
    let finish: (m: Memory) => void = () => {};
    api.patchMemory.mockImplementationOnce(() => new Promise<Memory>((resolve) => (finish = resolve)));
    const w = mount(MemoryRow, { props: { memory: memory("A") } });
    let box = await edit(w);
    await box.setValue("B");
    await box.trigger("blur"); // saving B…
    box = await edit(w); // …the list still says A
    await box.setValue("A");
    await box.trigger("blur");
    finish(memory("B", { version: 2 }));
    await flushPromises();
    expect(api.patchMemory.mock.calls.map(([, body]) => body.content)).toEqual(["B", "A"]);
  });
});
