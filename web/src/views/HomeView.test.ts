import { enableAutoUnmount, flushPromises, mount } from "@vue/test-utils";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Memory, Overview, SafetyIssue, SafetyScore, State } from "../api";
import { store } from "../store";
import HomeView from "./HomeView.vue";

const issue = (id: string, extra: Partial<SafetyIssue> = {}): SafetyIssue => ({
  id, area: "backup", points: 15, message: "There's no copy of your memory from the last 30 days.", fix: "export",
  target: null, dismissed: false, ...extra,
});

function overview(extra: Partial<Overview> = {}): Overview {
  return {
    first_run: false,
    safety: { score: 72, level: "Good", week_change: 5, top: [
      issue("secret:m1:password", { area: "sensitive", points: 5, message: "A memory in Work looks like it contains a password.",
        fix: "memory", target: "m1" }),
      issue("app-private:claude", { area: "apps", points: 8, message: "Claude can read your private topics.", fix: "app",
        target: "claude" }),
      issue("backup"),
    ] },
    waiting: { pending: 2, unreviewed: 0, review_due: 1 },
    memories: {
      total: 12, private: 3, groups: [{ id: "who", name: "Who I am", count: 5 }, { id: "private", name: "Private", count: 3 }],
      growth: Array.from({ length: 30 }, (_, i) => ({ date: `2026-09-${String(i + 1).padStart(2, "0")}`, total: 10 + (i > 20 ? 2 : 0) })),
      sources: { you: 6, apps: 4, imports: 2 },
    },
    apps: [{ id: "claude", name: "Claude", kind: "stdio", last_seen: new Date().toISOString(), reads: 9, writes: 2,
             read_topics: "all", include_private: true, write: "propose" }],
    activity: ["2026-10-16", "2026-10-17", "2026-10-18", "2026-10-19", "2026-10-20", "2026-10-21", "2026-10-22"]
      .map((date, i) => ({ date, reads: i, writes: i % 2 })),
    ...extra,
  };
}

const checks: SafetyScore = {
  score: 72, level: "Good", week_change: 5,
  areas: [{ id: "sensitive", name: "Sensitive information", max: 25, points: 20 },
          { id: "backup", name: "Backup", max: 15, points: 0 }],
  issues: [issue("backup"), issue("upkeep-long", { area: "sensitive", points: 5, message: "Old one", dismissed: true })],
};

const { api } = vi.hoisted(() => ({
  api: { overview: vi.fn(), score: vi.fn(), dismissIssue: vi.fn(), state: vi.fn(), memories: vi.fn(),
         proposals: vi.fn(async () => []), connectors: vi.fn(async () => []) },
}));
vi.mock("../api", () => ({ api }));

enableAutoUnmount(afterEach);

describe("Home", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    store.state = { counts: { active: 12, archived: 0, pending: 2, unreviewed: 0, review_due: 1 } } as State;
    store.memories = [{ id: "m1", topic: "work" } as Memory];
    store.connectors = [];
    api.overview.mockImplementation(async () => overview());
    api.score.mockImplementation(async () => checks);
    api.state.mockImplementation(async () => store.state);
    api.memories.mockImplementation(async () => store.memories);
  });

  it("shows the score, its top issues with their fixes, and what waits first", async () => {
    const w = mount(HomeView);
    await flushPromises();
    const safety = w.find('[data-test="safety"]');
    expect(safety.text()).toContain("Memory safety · Good");
    expect(safety.text()).toContain("+5 since last week");
    expect(safety.find("svg").attributes("aria-label")).toBe("Memory safety: 72 · Good");
    const fixes = safety.findAll("li a").map((a) => [a.text(), a.attributes("href")]);
    expect(fixes).toEqual([
      ["Open the memory", "#/memories/work"], ["Change its access", "#/connectors/claude"], ["Export everything", "#/import"],
    ]);
    const waiting = w.find('[data-test="waiting"]');
    expect(waiting.classes()).toContain("order-first");
    expect(waiting.text()).toContain("2 changes wait for your decision");
    expect(waiting.text()).toContain("1 memory is due for a check");
  });

  it("shows the memory, the apps and the week without any memory text", async () => {
    const w = mount(HomeView);
    await flushPromises();
    expect(w.find('[data-test="memory"]').text()).toContain("12 memories");
    expect(w.find('[data-test="memory"]').text()).toContain("You 50% · AI apps 33% · Imports 17% · 3 in private topics");
    expect(w.find('[data-test="apps"]').text().replace(/\s+/g, " ")).toContain("9 reads · 2 changes this week · Reads private topics");
    const week = w.find('[data-test="week"]');
    expect(week.text()).toContain("Reads 21");
    expect(week.text()).toContain("Changes 3");
    expect(week.findAll("[title]")).toHaveLength(7);
  });

  it("is the first steps in a new folder, and says nothing waits by leaving the card out", async () => {
    api.overview.mockImplementation(async () => overview({ first_run: true, waiting: { pending: 0, unreviewed: 0, review_due: 0 },
      safety: { score: 85, level: "Good", week_change: null, top: [issue("backup")] } }));
    const w = mount(HomeView);
    await flushPromises();
    expect(w.find('[data-test="waiting"]').exists()).toBe(false);
    expect(w.find('[data-test="memory"]').exists()).toBe(false);
    expect(w.find('[data-test="start"]').findAll("a").map((a) => a.attributes("href"))).toEqual(
      ["#/connectors", "#/import", "#/memories"]);
    expect(w.text()).not.toContain("since last week");
  });

  it("lists every check, dismisses one and counts a dismissed one again", async () => {
    api.dismissIssue.mockImplementation(async () => checks);
    const w = mount(HomeView);
    await flushPromises();
    await w.find('[data-test="safety"] button').trigger("click");
    await flushPromises();
    const list = w.find('[data-test="checks"]');
    expect(list.text()).toContain("Sensitive information20 / 25");
    const buttons = list.findAll("button");
    expect(buttons.map((b) => b.text())).toEqual(["Count again", "I back up another way"]);
    await buttons[1].trigger("click");
    await flushPromises();
    expect(api.dismissIssue).toHaveBeenCalledWith("backup", false);
    await w.find('[data-test="checks"]').findAll("button")[0].trigger("click");
    await flushPromises();
    expect(api.dismissIssue).toHaveBeenLastCalledWith("upkeep-long", true);
  });

  it("reloads when what it counts changes, not on every poll", async () => {
    mount(HomeView);
    await flushPromises();
    expect(api.overview).toHaveBeenCalledTimes(1);
    store.state = { ...store.state!, counts: { ...store.state!.counts } }; // a poll with the same numbers
    await flushPromises();
    expect(api.overview).toHaveBeenCalledTimes(1);
    store.state = { ...store.state!, counts: { ...store.state!.counts, pending: 3 } };
    await flushPromises();
    expect(api.overview).toHaveBeenCalledTimes(2);
  });
});
