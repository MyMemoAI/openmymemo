import { flushPromises } from "@vue/test-utils";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ref } from "vue";
import type { Memory } from "./api";
import { SEARCH_DELAY, useMemoryList } from "./memoryList";
import { store } from "./store";

const { api } = vi.hoisted(() => ({ api: { search: vi.fn(), memories: vi.fn() } }));
vi.mock("./api", () => ({ api }));

const mem = (id: string, content: string): Memory => ({
  id, content, topic: "interests", section: null, kind: "fact", source: "user", last_actor: "user", created_at: "",
  updated_at: "", version: 1, status: "active", pinned: false, private: false, effective_private: false, until: null,
  review_due: false, evidence: null, archived_at: null,
});

describe("the search box", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.clearAllMocks();
    store.memories = [mem("bass1", "Plays bass in a band"), mem("tea01", "Prefers tea over coffee"), mem("run01", "Runs at 6")];
  });
  afterEach(() => vi.useRealTimers());

  it("filters by text when the server has no index, as before", async () => {
    api.search.mockResolvedValue({ mode: "keyword", results: [] });
    const list = useMemoryList(ref(null));
    list.query.value = "bass";
    await vi.advanceTimersByTimeAsync(SEARCH_DELAY + 10);
    await flushPromises();
    expect(api.search).toHaveBeenCalledWith("bass");
    expect(list.visible.value.map((m) => m.id)).toEqual(["bass1"]);
  });

  it("finds by meaning and puts the best match first when search by meaning is on", async () => {
    api.search.mockResolvedValue({ mode: "hybrid", results: [{ id: "tea01", score: 3, match: "meaning" }] });
    const list = useMemoryList(ref(null));
    list.query.value = "what do I drink";
    expect(list.visible.value).toEqual([]); // until the server answers
    await vi.advanceTimersByTimeAsync(SEARCH_DELAY + 10);
    await flushPromises();
    expect(list.visible.value.map((m) => m.id)).toEqual(["tea01"]);
    list.clearFilters();
    expect(list.visible.value).toHaveLength(3);
  });

  it("ignores an answer to an older query", async () => {
    let answer: (v: unknown) => void = () => {};
    api.search.mockReturnValueOnce(new Promise((r) => (answer = r)))
      .mockResolvedValueOnce({ mode: "keyword", results: [] });
    const list = useMemoryList(ref(null));
    list.query.value = "drink";
    await vi.advanceTimersByTimeAsync(SEARCH_DELAY + 10);
    list.query.value = "bass";
    await vi.advanceTimersByTimeAsync(SEARCH_DELAY + 10);
    answer({ mode: "hybrid", results: [{ id: "tea01", score: 1, match: "meaning" }] });
    await flushPromises();
    expect(list.visible.value.map((m) => m.id)).toEqual(["bass1"]);
  });
});
