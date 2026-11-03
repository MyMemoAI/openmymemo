import { flushPromises, mount } from "@vue/test-utils";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { Connector, Proposal } from "../api";
import { store } from "../store";
import ActivityView from "../views/ActivityView.vue";
import ConnectorPanel from "./ConnectorPanel.vue";
import DigestRow from "./DigestRow.vue";
import ProposalCard from "./ProposalCard.vue";

/** Asking in the app (product-design.md §22): the per-app switch, and what came of a question on the Changes page. */

const { api } = vi.hoisted(() => ({
  api: {
    snippets: vi.fn(async () => ({})),
    appSetup: vi.fn(async () => null),
    patchConnector: vi.fn(async () => ({})),
    activity: vi.fn(async () => []),
    // refresh() after an action
    state: vi.fn(async () => null),
    memories: vi.fn(async () => []),
    proposals: vi.fn(async () => []),
    connectors: vi.fn(async () => []),
  },
}));
vi.mock("../api", () => ({ api }));

function connector(extra: Partial<Connector> = {}): Connector {
  return {
    id: "claude-code", name: "Claude Code", kind: "stdio", read_topics: "all", include_private: false, write: "propose",
    blocked_topics: [], ask: true, token_preview: null, has_token: false, created_at: "", last_seen: null, reads: 0,
    proposals: 0, ...extra,
  };
}

function proposal(extra: Partial<Proposal> = {}): Proposal {
  return {
    id: "p1", connector_id: "claude-code", connector_name: "Claude Code", op: "update", content: "Works at Acme as CEO",
    topic: "work", memory_id: "m1", kind: null, reason: null, evidence: null, created_at: "2026-11-02T09:00:00Z",
    status: "pending", hold: "protected", decided_at: null, reviewed_at: null, final_content: null, final_topic: null,
    applied_memory_id: null, applied_version: null, before_content: "Works at Acme as CTO", before_topic: "work",
    original_op: null, similarity: null, conflict: false, can_undo: false, ...extra,
  };
}

describe("asking in the app", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    store.state = null;
    store.connectors = [];
  });

  it("has a switch for an app on this computer that writes, and turns it off", async () => {
    const w = mount(ConnectorPanel, { props: { connector: connector(), app: null } });
    await flushPromises();
    const sw = w.get("[data-test=ask]");
    expect(sw.attributes("aria-checked")).toBe("true");
    expect(w.text()).toContain("OpenMyMemo asks you right in Claude Code");
    await sw.trigger("click");
    expect(api.patchConnector).toHaveBeenCalledWith("claude-code", { ask: false });

    await w.setProps({ connector: connector({ ask: false }) });
    expect(w.text()).toContain("stay on the Changes page until you decide");
  });

  it("has no switch where nothing would be asked", async () => {
    for (const c of [connector({ write: "auto" }), connector({ write: "none" }), connector({ kind: "cloud", id: "cloud-claude" })]) {
      const w = mount(ConnectorPanel, { props: { connector: c, app: null } });
      await flushPromises();
      expect(w.find("[data-test=ask]").exists()).toBe(false);
    }
  });

  it("says on the Changes page what was answered in the app", () => {
    const note = (p: Partial<Proposal>) => mount(ProposalCard, { props: { proposal: proposal(p) } }).find("[data-test=ask-note]");
    expect(note({}).exists()).toBe(false);
    expect(note({ asked_at: "2026-11-02T09:00:01Z" }).text()).toBe("Asked in Claude Code; not decided there");
    expect(note({ asked_at: "2026-11-02T09:00:01Z", answer: "cancel" }).text()).toBe("Asked in Claude Code; not decided there");
    expect(note({ asked_at: "2026-11-02T09:00:01Z", answer: "accept" }).text())
      .toBe("Accepted in Claude Code, but the memory changed before it could be saved");
    expect(note({ asked_at: "2026-11-02T09:00:01Z", answer: "decline", status: "rejected" }).text()).toBe("Declined in Claude Code");
    // asked, then decided here: nothing to add
    expect(note({ asked_at: "2026-11-02T09:00:01Z", answer: "cancel", status: "accepted" }).exists()).toBe(false);

    const row = mount(DigestRow, {
      props: { proposal: proposal({ status: "applied", hold: null, asked_at: "2026-11-02T09:00:01Z", answer: "accept", can_undo: true }) },
    });
    expect(row.get("[data-test=ask-note]").text()).toBe("Accepted in Claude Code");
  });

  it("lists each question and its answer in Activity", async () => {
    api.activity.mockImplementation(async () => [
      { at: "2026-11-02T09:00:02Z", connector: "claude-code", action: "ask", proposal_id: "p1", result: "accept" },
      { at: "2026-11-02T09:00:01Z", connector: "claude-code", action: "ask", proposal_id: "p2", result: "timeout" },
    ] as never);
    const w = mount(ActivityView);
    await flushPromises();
    expect(w.text()).toContain("asked you about its change in the app · accepted");
    expect(w.text()).toContain("asked you about its change in the app · no answer");
  });
});
