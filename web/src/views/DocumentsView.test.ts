import { enableAutoUnmount, flushPromises, mount } from "@vue/test-utils";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { DocumentInfo, Proposal, State } from "../api";
import { store } from "../store";
import DocumentsView from "./DocumentsView.vue";

const { api, ApiError } = vi.hoisted(() => {
  class ApiError extends Error {
    constructor(message: string, readonly status: number) {
      super(message);
    }
  }
  return {
    ApiError,
    api: {
      document: vi.fn(), documents: vi.fn(async () => []), saveDocument: vi.fn(), createDocument: vi.fn(),
      patchDocument: vi.fn(), archiveDocument: vi.fn(), restoreDocument: vi.fn(), documentHistory: vi.fn(),
      rollbackDocument: vi.fn(),
      // refresh()
      state: vi.fn(async () => store.state), memories: vi.fn(async () => []), proposals: vi.fn(async () => []),
      connectors: vi.fn(async () => store.connectors),
    },
  };
});
vi.mock("../api", () => ({ api, ApiError }));

enableAutoUnmount(afterEach);

function doc(extra: Partial<DocumentInfo> = {}): DocumentInfo {
  return {
    id: "doc:working-with-me", slug: "working-with-me", title: "Working with me", description: null, private: false,
    chars: 41, version: 3, status: "active", source: "user", last_actor: "connector:claude-code",
    created_at: "2026-11-01T09:00:00Z", updated_at: "2026-11-02T09:00:00Z", archived_at: null, ...extra,
  };
}

describe("Documents", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    store.state = { documents: [doc()], topics: [], counts: { active: 0, archived: 0, pending: 1, unreviewed: 0, review_due: 0 } } as unknown as State;
    store.pending = [{ id: "p1", memory_id: "doc:working-with-me", status: "pending" } as Proposal];
    store.connectors = [{ id: "claude-code", name: "Claude Code" } as never];
    store.toast = null;
    location.hash = "#/documents";
  });

  it("lists the documents with their size, who changed them and a waiting change", async () => {
    const w = mount(DocumentsView, { props: { initialSlug: null } });
    await flushPromises();
    const row = w.get('[data-test="doc-list"] a');
    expect(row.attributes("href")).toBe("#/documents/working-with-me");
    expect(row.text()).toContain("Working with me");
    expect(row.text()).toContain("41 characters · changed by Claude Code");
    expect(row.text()).toContain("Change waiting");
  });

  it("opens one, edits it and saves from the version it started with", async () => {
    let current = doc({ content: "Start with the answer." });
    api.document.mockImplementation(async () => current);
    api.saveDocument.mockImplementation(async (_s: string, content: string) => (current = doc({ content, version: 4 })));
    const w = mount(DocumentsView, { props: { initialSlug: "working-with-me" } });
    await flushPromises();
    expect(w.get('[data-test="doc-body"]').text()).toBe("Start with the answer.");
    expect(w.text()).toContain("An app proposed a change to this document");
    await w.get('[data-test="doc-edit"]').trigger("click");
    await w.get('[data-test="doc-text"]').setValue("Start with the answer, then why.");
    expect(w.find('[data-test="doc-diff"]').text()).toContain("+1 −1 lines");
    await w.get('[data-test="doc-save"]').trigger("click");
    await flushPromises();
    expect(api.saveDocument).toHaveBeenCalledWith("working-with-me", "Start with the answer, then why.", 3);
    expect(w.get('[data-test="doc-body"]').text()).toBe("Start with the answer, then why.");
  });

  it("says so when it changed somewhere else meanwhile", async () => {
    api.document.mockImplementation(async () => doc({ content: "Old" }));
    api.saveDocument.mockImplementation(async () => {
      throw new ApiError("changed", 409);
    });
    const w = mount(DocumentsView, { props: { initialSlug: "working-with-me" } });
    await flushPromises();
    await w.get('[data-test="doc-edit"]').trigger("click");
    await w.get('[data-test="doc-text"]').setValue("Mine");
    await w.get('[data-test="doc-save"]').trigger("click");
    await flushPromises();
    expect(store.toast?.text).toContain("changed somewhere else while you were editing");
    expect(w.find('[data-test="doc-text"]').exists()).toBe(true); // the text is still there to copy
  });

  it("starts a new one and opens it", async () => {
    api.createDocument.mockImplementation(async () => doc({ slug: "project-notes", title: "Project notes" }));
    const w = mount(DocumentsView, { props: { initialSlug: null }, attachTo: document.body });
    await flushPromises();
    await w.get('[data-test="doc-new"]').trigger("click");
    const title = document.querySelector('[data-test="doc-new-title"]') as HTMLInputElement;
    title.value = "Project notes";
    title.dispatchEvent(new Event("input"));
    (document.querySelector("form") as HTMLFormElement).dispatchEvent(new Event("submit"));
    await flushPromises();
    expect(api.createDocument).toHaveBeenCalledWith({ title: "Project notes", content: "", private: false });
    expect(location.hash).toBe("#/documents/project-notes");
  });

  it("makes it private and lists earlier versions as diffs", async () => {
    api.document.mockImplementation(async () => doc({ content: "v3" }));
    api.patchDocument.mockImplementation(async () => doc({ content: "v3", private: true }));
    api.documentHistory.mockImplementation(async () => [
      { at: "2026-11-02T09:00:00Z", memory_id: "doc:working-with-me", version: 3, op: "update", content: "v3", topic: "working-with-me", actor: "connector:claude-code" },
      { at: "2026-11-01T09:00:00Z", memory_id: "doc:working-with-me", version: 2, op: "update", content: "v2", topic: "working-with-me", actor: "user" },
      { at: "2026-11-01T08:00:00Z", memory_id: "doc:working-with-me", version: 1, op: "create", content: "v1", topic: "working-with-me", actor: "user" },
    ]);
    const w = mount(DocumentsView, { props: { initialSlug: "working-with-me" }, attachTo: document.body });
    await flushPromises();
    await w.get('[data-test="doc-private"]').trigger("click");
    await flushPromises();
    expect(api.patchDocument).toHaveBeenCalledWith("working-with-me", { private: true });
    await w.get('[data-test="doc-versions"]').trigger("click");
    await flushPromises();
    const list = document.querySelector('[data-test="doc-version-list"]') as HTMLElement;
    expect(list.textContent).toContain("Version 3");
    expect(list.textContent).toContain("changed by Claude Code");
    expect(list.textContent).toContain("written by you");
    expect(list.querySelectorAll('[data-test="doc-diff"]')).toHaveLength(2);
  });
});
