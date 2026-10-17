import { flushPromises, mount } from "@vue/test-utils";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { ImportItem } from "../api";
import { store } from "../store";
import ImportFiles from "./ImportFiles.vue";

const item = (content: string, extra: Partial<ImportItem> = {}): ImportItem => ({
  content, topic: null, kind: "preference", created_at: null, hint: null, duplicate_of: null, similar_to: null,
  similar_content: null, include: true, ...extra,
});

const { api } = vi.hoisted(() => ({
  api: {
    importFiles: vi.fn(),
    importFile: vi.fn(),
    importCommit: vi.fn(),
    // refresh() after an import
    state: vi.fn(async () => null),
    memories: vi.fn(async () => []),
    proposals: vi.fn(async () => []),
    connectors: vi.fn(async () => []),
  },
}));
vi.mock("../api", () => ({ api }));

describe("ImportFiles", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    store.toast = null;
    api.importFiles.mockImplementation(async () => ({
      path: "/Users/me", home: "/Users/me",
      files: [{ path: "/Users/me/.claude/CLAUDE.md", name: "CLAUDE.md", source: "claude-code", app: "Claude Code", size: 120, modified: 0 }],
    }));
    api.importFile.mockImplementation(async () => ({
      source: "claude-code", name: "CLAUDE.md",
      items: [item("Prefers concise answers", { hint: "Preferences" }), item("Uses uv", { duplicate_of: "m1", include: false })],
    }));
    api.importCommit.mockImplementation(async () => ({ memory_ids: ["m2"] }));
  });

  it("finds the memory files in the home folder, previews one and imports it into its app's topic", async () => {
    const w = mount(ImportFiles);
    await w.findAll("button").find((b) => b.text() === "Find memory files")!.trigger("click");
    await flushPromises();
    expect(api.importFiles).toHaveBeenCalledWith("");
    expect(w.text()).toContain("1 memory file in ~");
    expect(w.text()).toContain("~/.claude/CLAUDE.md");
    await w.findAll("button").find((b) => b.text() === "Preview")!.trigger("click");
    await flushPromises();
    expect(api.importFile).toHaveBeenCalledWith({ path: "/Users/me/.claude/CLAUDE.md" });
    expect(w.text()).toContain("From CLAUDE.md");
    expect(w.text()).toContain("Preferences");
    expect(w.text()).toContain("Already have");
    await w.findAll("button").find((b) => b.text() === "Import 1")!.trigger("click");
    await flushPromises();
    expect(api.importCommit).toHaveBeenCalledWith("claude-code", expect.any(Array));
    expect(w.emitted("imported")![0][0]).toMatchObject({ source: "claude-code", slug: "from-claude-code", n: 1 });
  });

  it("reads a chosen file in the browser and sends its name and text", async () => {
    const w = mount(ImportFiles);
    const input = w.find('input[type="file"]');
    const file = new File(["- Lives in Lisbon"], "GEMINI.md", { type: "text/markdown" });
    Object.defineProperty(input.element, "files", { value: [file] });
    await input.trigger("change");
    await flushPromises();
    expect(api.importFile).toHaveBeenCalledWith({ name: "GEMINI.md", text: "- Lives in Lisbon" });
  });

  it("says so when a folder has no memory files", async () => {
    api.importFiles.mockImplementation(async () => ({ path: "/Users/me/src", home: "/Users/me", files: [] }));
    const w = mount(ImportFiles);
    await w.find("input").setValue("~/src");
    await w.findAll("button").find((b) => b.text() === "Find memory files")!.trigger("click");
    await flushPromises();
    expect(api.importFiles).toHaveBeenCalledWith("~/src");
    expect(w.text()).toContain("No memory files found there.");
  });
});
