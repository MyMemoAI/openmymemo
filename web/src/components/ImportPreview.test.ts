import { mount } from "@vue/test-utils";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { ImportItem } from "../api";
import { store } from "../store";
import ImportPreview from "./ImportPreview.vue";

const item = (content: string, extra: Partial<ImportItem> = {}): ImportItem => ({
  content, topic: null, kind: "fact", created_at: null, hint: null, duplicate_of: null, similar_to: null,
  similar_content: null, include: true, ...extra,
});

vi.mock("../api", () => ({ api: { importCommit: vi.fn() } }));

describe("ImportPreview", () => {
  beforeEach(() => {
    store.state = { topics: [{ slug: "work", title: "Work", private: false }], kinds: ["fact"] } as never;
  });

  it("says the Assistant split it, flags what it left out, and lets the person drop its topic", async () => {
    const items = [
      item("Is the CTO at Acme", { by: "assistant", topic: "work" }),
      item("Has two cats", { by: "rules", flag: "dropped", include: false }),
    ];
    const w = mount(ImportPreview, { props: { source: "markdown", items } });
    expect(w.get('[data-test="split-by"]').text()).toContain("Split by OpenMyMemo Assistant");
    expect(w.text()).toContain("Left out by the Assistant");
    expect((w.findAll('input[type="checkbox"]')[1].element as HTMLInputElement).checked).toBe(false);
    await w.get('[aria-label="Don\'t file into Work"]').trigger("click");
    expect(items[0].topic).toBeNull();
  });

  it("says nothing about splitting when the Assistant is off, and names a fallback", () => {
    const off = mount(ImportPreview, { props: { source: "chatgpt", items: [item("Likes tea")] } });
    expect(off.find('[data-test="split-by"]').exists()).toBe(false);
    const fell = mount(ImportPreview, { props: { source: "chatgpt", items: [item("Likes tea", { by: "rules" })] } });
    expect(fell.get('[data-test="split-by"]').text()).toContain("Split by rules");
  });

  it("shows the words a memory read from a chat came from, and leaves sensitive ones unticked", () => {
    const quote = '“I\'m vegetarian” — ChatGPT, "Lisbon trip", 2025-09-16';
    const items = [
      item("Is vegetarian", { by: "assistant", evidence: quote }),
      item("Takes insulin", { by: "assistant", flag: "sensitive", include: false }),
    ];
    const w = mount(ImportPreview, { props: { source: "chatgpt-export", items } });
    expect(w.get('[data-test="evidence"]').text()).toBe(quote);
    expect(w.text()).toContain("Looks sensitive");
    expect((w.findAll('input[type="checkbox"]')[1].element as HTMLInputElement).checked).toBe(false);
  });
});
