import { flushPromises, mount } from "@vue/test-utils";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { Connector, Proposal, State } from "../api";
import { store } from "../store";
import ConnectorPanel from "./ConnectorPanel.vue";
import DigestRow from "./DigestRow.vue";
import ImportFiles from "./ImportFiles.vue";
import ProposalCard from "./ProposalCard.vue";

/** Documents (product-design.md §23) where they meet the rest of the panel: changes, imports and read access. */

const { api } = vi.hoisted(() => ({
  api: {
    accept: vi.fn(async () => ({})),
    editApplied: vi.fn(async () => ({})),
    importFile: vi.fn(),
    createDocument: vi.fn(),
    importCommit: vi.fn(async () => ({ memory_ids: ["m9"] })),
    snippets: vi.fn(async () => ({})),
    appSetup: vi.fn(async () => null),
    patchConnector: vi.fn(async () => ({})),
    // refresh() after an action
    state: vi.fn(async () => store.state),
    memories: vi.fn(async () => []),
    proposals: vi.fn(async () => []),
    connectors: vi.fn(async () => []),
  },
}));
vi.mock("../api", () => ({ api }));

function proposal(extra: Partial<Proposal> = {}): Proposal {
  return {
    id: "p1", connector_id: "claude-code", connector_name: "Claude Code", op: "update",
    content: "# Style\nStart with the answer.\nKeep it short.", topic: "working-with-me", memory_id: "doc:working-with-me",
    kind: null, reason: "They asked for shorter replies", evidence: null, created_at: "2026-11-02T09:00:00Z", status: "pending",
    hold: "protected", decided_at: null, reviewed_at: null, final_content: null, final_topic: null, applied_memory_id: null,
    applied_version: null, before_content: "# Style\nStart with the answer.", before_topic: "working-with-me", original_op: null,
    similarity: null, conflict: false, can_undo: false, ...extra,
  };
}

const button = (w: ReturnType<typeof mount>, text: string) => w.findAll("button").find((b) => b.text() === text)!;

describe("documents across the panel", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    store.toast = null;
    store.connectors = [];
    store.state = {
      topics: [{ slug: "work", title: "Work", private: false }],
      documents: [{ id: "doc:working-with-me", slug: "working-with-me", title: "Working with me", private: false }],
      counts: { active: 1, archived: 0, pending: 1, unreviewed: 0, review_due: 0 },
    } as unknown as State;
  });

  it("shows an app's change to a document as a diff, and accepts an edit of the whole text", async () => {
    const w = mount(ProposalCard, { props: { proposal: proposal() } });
    expect(w.text()).toContain("wants to change the document");
    expect(w.text()).toContain("You wrote this document");
    expect(w.text()).toContain("Working with me");
    expect(w.get('[data-test="doc-diff"]').text()).toContain("Keep it short.");
    await button(w, "Edit and accept").trigger("click");
    const area = w.get("textarea");
    expect((area.element as HTMLTextAreaElement).value).toBe("# Style\nStart with the answer.\nKeep it short.");
    expect(w.find("select").exists()).toBe(false); // a document stays where it is
    await area.setValue("# Style\nStart with the answer.\nKeep it brief.\n");
    await button(w, "Accept").trigger("click");
    await flushPromises();
    expect(api.accept).toHaveBeenCalledWith("p1", { content: "# Style\nStart with the answer.\nKeep it brief.\n" });
  });

  it("shows an applied change to a document as a diff in the digest", () => {
    const w = mount(DigestRow, {
      props: { proposal: proposal({ status: "applied", hold: null, applied_memory_id: "doc:working-with-me", applied_version: 4, can_undo: true }) },
    });
    expect(w.text()).toContain("document changed");
    expect(w.get('[data-test="doc-diff"]').text()).toContain("+1 −0 lines");
  });

  it("imports an instructions file whole as a document, the first offer for it", async () => {
    api.importFile.mockImplementation(async () => ({
      source: "claude-code", name: "CLAUDE.md", items: [{ content: "Prefers uv", topic: null, kind: "preference", created_at: null,
                                                           hint: null, duplicate_of: null, similar_to: null, similar_content: null, include: true }],
      document: { title: "CLAUDE", description: null, content: "# Prefs\n- Prefers uv", chars: 20, prefer: true },
    }));
    api.createDocument.mockImplementation(async () => ({ slug: "claude-instructions" }));
    const w = mount(ImportFiles);
    const input = w.find('input[type="file"]');
    Object.defineProperty(input.element, "files", { value: [new File(["# Prefs\n- Prefers uv"], "CLAUDE.md")] });
    await input.trigger("change");
    await flushPromises();
    expect(w.get('[data-test="imp-whole"]').attributes("aria-checked")).toBe("true");
    await w.get('[data-test="imp-doc-title"]').setValue("Claude instructions");
    await w.get('[data-test="imp-doc"]').trigger("click");
    await flushPromises();
    expect(api.createDocument).toHaveBeenCalledWith({ title: "Claude instructions", content: "# Prefs\n- Prefers uv", description: null, source: "claude-code" });
    expect(location.hash).toBe("#/documents/claude-instructions");

    // or split into memories as before
    await input.trigger("change");
    await flushPromises();
    await w.get('[data-test="imp-split"]').trigger("click");
    expect(w.text()).toContain("Prefers uv");
    expect(w.find('[data-test="imp-doc"]').exists()).toBe(false);
  });

  it("offers a file with nothing to split only as a document", async () => {
    api.importFile.mockImplementation(async () => ({
      source: "file", name: "notes.md", items: [], document: { title: "notes", description: null, content: "Prose", chars: 5, prefer: false },
    }));
    const w = mount(ImportFiles);
    const input = w.find('input[type="file"]');
    Object.defineProperty(input.element, "files", { value: [new File(["Prose"], "notes.md")] });
    await input.trigger("change");
    await flushPromises();
    expect(store.toast).toBeNull();
    expect(w.find('[data-test="imp-split"]').exists()).toBe(false);
    expect(w.find('[data-test="imp-doc"]').exists()).toBe(true);
  });

  it("lets an app read a document or not, like a topic", async () => {
    const c = {
      id: "claude-code", name: "Claude Code", kind: "stdio", read_topics: "all", include_private: false, write: "propose",
      blocked_topics: [], token_preview: null, has_token: false, created_at: "", last_seen: null, reads: 0, proposals: 0,
    } as Connector;
    const w = mount(ConnectorPanel, { props: { connector: c, app: null } });
    await flushPromises();
    const pill = w.get('[data-test="read-doc"]');
    expect(pill.text()).toBe("Working with me");
    expect(pill.attributes("aria-pressed")).toBe("true");
    await pill.trigger("click");
    expect(api.patchConnector).toHaveBeenCalledWith("claude-code", { read_topics: ["work"] });
  });
});
