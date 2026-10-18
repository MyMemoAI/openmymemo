import { flushPromises, mount, type VueWrapper } from "@vue/test-utils";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Proposal, TidyResult, TidyStatus } from "../api";
import { store } from "../store";
import ChangesView from "../views/ChangesView.vue";
import TidyDialog from "./TidyDialog.vue";

const { api } = vi.hoisted(() => ({
  api: {
    tidy: vi.fn(), tidySchedule: vi.fn(), tidyRun: vi.fn(), keepBatch: vi.fn(), undoBatch: vi.fn(),
    state: vi.fn(), memories: vi.fn(), proposals: vi.fn(), connectors: vi.fn(), settings: vi.fn(),
    patchSettings: vi.fn(),
  },
}));
vi.mock("../api", () => ({ api }));

function status(extra: Partial<TidyStatus> = {}): TidyStatus {
  return {
    enabled: true, available: true, reason: null, max_calls: 20, schedule: "off", every_days: null, last_run: null,
    next_run: null, due: false, ...extra,
  };
}

function result(extra: Partial<TidyResult> = {}): TidyResult {
  return {
    ok: true, message: "", dry_run: false, batch_id: null, memories: 3, left_out: 0, chunks: 1, calls: 0,
    estimate: { calls: 1, tokens: 1500 }, candidates: [], items: [], applied: 0, waiting: 0, seen_before: 0,
    stopped: null, ...extra,
  };
}

function proposal(id: string, extra: Partial<Proposal> = {}): Proposal {
  return {
    id, connector_id: "assistant", connector_name: "OpenMyMemo Assistant", op: "update", content: "New",
    topic: "work", memory_id: "m1", kind: null, reason: "Too long.", evidence: null,
    created_at: "2026-10-25T09:00:00+00:00", status: "applied", hold: null, decided_at: "2026-10-25T09:00:00+00:00",
    reviewed_at: null, final_content: "New", final_topic: "work", applied_memory_id: "m1", applied_version: 2,
    before_content: "Old", before_topic: "work", original_op: null, similarity: null, conflict: false,
    batch_id: "tidy-2026-10-25-ab12", can_undo: true, ...extra,
  };
}

describe("Tidy up", () => {
  let w: VueWrapper | null = null;
  beforeEach(() => {
    vi.clearAllMocks();
    store.toast = null;
    store.state = { topics: [{ slug: "work", title: "Work", private: false }], settings: {} } as never;
    store.unreviewed = [];
    store.pending = [];
    for (const f of [api.state, api.memories, api.proposals, api.connectors]) f.mockRejectedValue(new Error("offline"));
  });
  afterEach(() => w?.unmount());

  it("estimates first, then runs and shows what it filed", async () => {
    api.tidy.mockResolvedValue(status());
    api.tidyRun.mockResolvedValueOnce(result({ dry_run: true, message: "A tidy-up would send 3 memories in 1 calls." }))
      .mockResolvedValueOnce(result({
        message: "Tidy-up, 2026-10-25: 1 change: 1 applied, 0 waiting for you.", applied: 1, calls: 1,
        items: [{ kind: "until", memory_ids: ["m1"], reason: "A trip.", content: null, until: "2026-12",
          before: ["Travelling in Japan"], filed: [{ status: "applied" }], error: null }],
      }));
    w = mount(TidyDialog, { global: { stubs: { teleport: true } } });
    await flushPromises();
    await w.get('[data-test="tidy-estimate"]').trigger("click");
    await flushPromises();
    expect(api.tidyRun).toHaveBeenLastCalledWith({ dry_run: true, topic: null });
    expect(w.get('[data-test="tidy-result"]').text()).toContain("would send 3 memories");
    await w.get('[data-test="tidy-run"]').trigger("click");
    await flushPromises();
    expect(api.tidyRun).toHaveBeenLastCalledWith({ dry_run: false, topic: null });
    const text = w.get('[data-test="tidy-result"]').text();
    expect(text).toContain("1 applied");
    expect(text).toContain("Review date");
    expect(text).toContain("Review on 2026-12");
  });

  it("can't run while the Assistant isn't on for it, and sets the schedule", async () => {
    api.tidy.mockResolvedValue(status({ available: false, reason: "The Assistant isn't used for tidy." }));
    api.tidySchedule.mockResolvedValue(status({ available: false, schedule: "weekly", every_days: 7 }));
    w = mount(TidyDialog, { global: { stubs: { teleport: true } } });
    await flushPromises();
    expect(w.get('[data-test="tidy-unavailable"]').text()).toContain("isn't used for tidy");
    expect(w.get('[data-test="tidy-run"]').attributes("disabled")).toBeDefined();
    await w.get('[data-test="tidy-schedule"]').setValue("weekly");
    await flushPromises();
    expect(api.tidySchedule).toHaveBeenCalledWith("weekly");
    expect(w.text()).toContain("Nothing runs when none of them does");
  });

  it("groups a batch in the digest with Keep all and Undo all", async () => {
    api.tidy.mockResolvedValue(status());
    api.undoBatch.mockResolvedValue({ undone: 2, kept: 0 });
    api.keepBatch.mockResolvedValue({ reviewed: 2 });
    store.unreviewed = [proposal("p2"), proposal("p1", { op: "archive", content: null, final_content: null })];
    w = mount(ChangesView, { props: { initialTab: null } });
    await flushPromises();
    expect(w.get('[data-test="batch-title"]').text()).toContain("Tidy-up, 2026-10-25: 2 changes");
    expect(w.find('[data-test="tidy-open"]').exists()).toBe(true);
    await w.get('[data-test="undo-all"]').trigger("click");
    await flushPromises();
    expect(api.undoBatch).toHaveBeenCalledWith("tidy-2026-10-25-ab12");
    expect(store.toast?.text).toBe("Undid 2 changes");
    await w.get('[data-test="keep-all"]').trigger("click");
    await flushPromises();
    expect(api.keepBatch).toHaveBeenCalledWith("tidy-2026-10-25-ab12");
  });
});
