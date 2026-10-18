import { flushPromises, mount, type VueWrapper } from "@vue/test-utils";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ChatJob } from "../api";
import { store } from "../store";
import ImportChats from "./ImportChats.vue";

const { api } = vi.hoisted(() => ({
  api: { chatPlan: vi.fn(), chatStart: vi.fn(), chatStatus: vi.fn(), chatCancel: vi.fn(), importCommit: vi.fn() },
}));
vi.mock("../api", () => ({ api }));

function job(extra: Partial<ChatJob> = {}): ChatJob {
  return {
    job: "j1", state: "planned", source: "chatgpt-export", conversations: 120, messages: 3400, newest: "2026-10-01",
    oldest: "2024-01-03", ready: true, reason: null, done: 0, total: 80, failed: 0, error: null,
    estimate: { calls: 80, tokens: 400000, tokens_in: 360000, tokens_out: 40000, messages: 3000, calls_left: 900, remote: true, host: "api.anthropic.com" },
    ...extra,
  };
}

const button = (w: VueWrapper, text: string) => w.findAll("button").find((b) => b.text() === text)!;

describe("ImportChats", () => {
  let w: VueWrapper | null = null;
  beforeEach(() => {
    vi.clearAllMocks();
    vi.useFakeTimers();
    store.toast = null;
    store.state = { topics: [], kinds: ["fact"] } as never;
    api.chatCancel.mockResolvedValue({ ok: true });
  });
  afterEach(() => {
    w?.unmount();
    vi.useRealTimers();
  });

  it("shows the cost before reading, then reads the newest parts and previews them with evidence", async () => {
    api.chatPlan.mockResolvedValue(job());
    w = mount(ImportChats);
    await w.get("input.input").setValue("~/Downloads/export.zip");
    await button(w, "Read").trigger("click");
    await flushPromises();
    expect(api.chatPlan).toHaveBeenCalledWith({ source: "chatgpt-export", path: "~/Downloads/export.zip" });
    expect(w.get('[data-test="chat-plan"]').text()).toContain("120 conversations");
    expect(w.get('[data-test="chat-estimate"]').text()).toContain("80 calls, about 400,000 tokens");
    expect(w.get('[data-test="chat-estimate"]').text()).toContain("api.anthropic.com");
    expect((w.get('input[type="number"]').element as HTMLInputElement).value).toBe("50");

    api.chatStart.mockResolvedValue(job({ state: "running", total: 50 }));
    api.chatStatus.mockResolvedValue(job({
      state: "done", done: 50, total: 50,
      items: [{ content: "Is vegetarian", topic: null, kind: "fact", created_at: "2025-09-16", hint: "Lisbon trip",
        duplicate_of: null, similar_to: null, similar_content: null, include: true, by: "assistant",
        evidence: "“I'm vegetarian” — ChatGPT, \"Lisbon trip\", 2025-09-16" }],
    }));
    await button(w, "Start reading").trigger("click");
    await flushPromises();
    expect(api.chatStart).toHaveBeenCalledWith("j1", 50);
    expect(w.get('[data-test="chat-running"]').text()).toContain("Reading part 0 of 50");
    await vi.advanceTimersByTimeAsync(600);
    await flushPromises();
    expect(w.get('[data-test="evidence"]').text()).toContain("I'm vegetarian");
  });

  it("says when the Assistant is needed, and defaults session sources to the app's folder", async () => {
    api.chatPlan.mockResolvedValue(job({ ready: false, reason: "The Assistant is off.", source: "codex-sessions" }));
    w = mount(ImportChats);
    await button(w, "Codex sessions").trigger("click");
    await button(w, "Read").trigger("click");
    await flushPromises();
    expect(api.chatPlan).toHaveBeenCalledWith({ source: "codex-sessions", path: "~/.codex/sessions" });
    expect(w.get('[data-test="chat-needs-assistant"]').text()).toContain("The Assistant is off.");
    expect(w.findAll("button").some((b) => b.text() === "Start reading")).toBe(false);
  });
});
