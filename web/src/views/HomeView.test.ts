import { enableAutoUnmount, flushPromises, mount } from "@vue/test-utils";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Brief, Connector, Memory, Overview, SafetyIssue, SafetyScore, State } from "../api";
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

function brief(extra: Partial<Brief> = {}): Brief {
  return {
    date: "2026-10-21", quiet: false,
    learned: [{ app: "Claude", private: 1, changes: [
      { op: "add", memory_id: "m2", content: "Prefers tea over coffee", topic: "preferences" },
      { op: "update", memory_id: "m3", content: "Works at Globex", topic: "work" },
      { op: "add", memory_id: "m4", content: "Runs on Sundays", topic: "health-habits" },
      { op: "add", memory_id: "m5", content: "Uses pnpm", topic: "work" },
    ] }],
    waiting: { pending: 2, review_due: 1 },
    used: [{ app: "ChatGPT", reads: 12, topics: ["Work", "Projects"] }],
    safety: { score: 72, change: -3 },
    resurface: { memory_id: "m1", content: "Lives in Lyon", topic: "identity", updated_at: "2026-08-01T00:00:00Z" },
    ...extra,
  };
}

const { api } = vi.hoisted(() => ({
  api: { overview: vi.fn(), brief: vi.fn(), archive: vi.fn(), score: vi.fn(), dismissIssue: vi.fn(), state: vi.fn(), memories: vi.fn(),
         proposals: vi.fn(async () => []), connectors: vi.fn(async () => []) },
}));
vi.mock("../api", () => ({ api }));

enableAutoUnmount(afterEach);

describe("Home", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    store.state = { counts: { active: 12, archived: 0, pending: 2, unreviewed: 0, review_due: 1 } } as State;
    store.memories = [{ id: "m1", topic: "work", version: 1, private: false } as Memory];
    store.connectors = [];
    api.overview.mockImplementation(async () => overview());
    api.brief.mockImplementation(async () => brief());
    localStorage.clear();
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

  it("reloads when what it shows changes, every 30 seconds, and after a failed load; not on every poll", async () => {
    const now = vi.spyOn(Date, "now").mockReturnValue(1_000_000);
    const poll = async (change: () => void = () => {}) => {
      change();
      store.version++;
      await flushPromises();
    };
    store.connectors = [{ id: "claude", include_private: false, write: "propose", read_topics: "all" } as Connector];
    mount(HomeView);
    await flushPromises();
    expect(api.overview).toHaveBeenCalledTimes(1);
    await poll(); // the same numbers, the same access, the same memories
    expect(api.overview).toHaveBeenCalledTimes(1);
    await poll(() => (store.connectors = [{ ...store.connectors[0], include_private: true, write: "auto" }]));
    expect(api.overview).toHaveBeenCalledTimes(2);
    await poll(() => (store.memories = [{ ...store.memories[0], version: 2 }])); // edited: maybe a secret now
    expect(api.overview).toHaveBeenCalledTimes(3);
    await poll(() => (store.state = { ...store.state!, counts: { ...store.state!.counts, pending: 3 } }));
    expect(api.overview).toHaveBeenCalledTimes(4);
    now.mockReturnValue(1_000_000 + 30_000); // reads leave the counts alone: the time does it
    await poll();
    expect(api.overview).toHaveBeenCalledTimes(5);
    now.mockRestore();
  });

  it("tries again on the next poll when the first load failed", async () => {
    api.overview.mockRejectedValueOnce(new Error("offline"));
    const w = mount(HomeView);
    await flushPromises();
    expect(w.text()).toContain("Loading");
    store.version++;
    await flushPromises();
    expect(api.overview).toHaveBeenCalledTimes(2);
    expect(w.find('[data-test="safety"]').exists()).toBe(true);
  });

  it("shows yesterday on the Today card: what apps saved, who read, the score, and a memory to check", async () => {
    const w = mount(HomeView);
    await flushPromises();
    const card = w.find('[data-test="today"]');
    const text = card.text().replace(/\s+/g, " ");
    expect(text).toContain("Claude saved 5 memories");
    expect(text).toContain("Added: Prefers tea over coffee");
    expect(text).toContain("and 1 more");
    expect(text).not.toContain("Uses pnpm"); // the fourth: behind "and 1 more"
    expect(text).toContain("1 in a private topic");
    expect(text).toContain("ChatGPT read your memory 12 times · Work, Projects");
    expect(text).toContain("Memory safety went from 75 to 72.");
    expect(card.find('[data-test="resurface"]').text()).toContain("Lives in Lyon");
  });

  it("keeps or archives the memory to check, and says when a day was quiet", async () => {
    api.archive.mockImplementation(async () => ({}));
    const w = mount(HomeView);
    await flushPromises();
    await w.findAll('[data-test="resurface"] button')[1].trigger("click"); // Archive
    await flushPromises();
    expect(api.archive).toHaveBeenCalledWith("m1");
    expect(w.find('[data-test="resurface"]').exists()).toBe(false);

    localStorage.clear();
    api.brief.mockImplementation(async () => brief({ quiet: true, learned: [], used: [], safety: null }));
    const quiet = mount(HomeView);
    await flushPromises();
    expect(quiet.find('[data-test="today"]').text()).toContain("A quiet day: no AI app saved or read anything.");
    await quiet.find('[data-test="resurface"] button').trigger("click"); // Keep
    expect(quiet.find('[data-test="resurface"]').exists()).toBe(false);
    expect(localStorage.getItem("openmymemo.home.kept")).toBe("2026-10-21:m1");
  });

  it("keeps a resurfaced memory only for that day's brief", async () => {
    const w = mount(HomeView);
    await flushPromises();
    await w.find('[data-test="resurface"] button').trigger("click"); // Keep
    expect(w.find('[data-test="resurface"]').exists()).toBe(false);
    w.unmount();

    const again = mount(HomeView);
    await flushPromises();
    expect(again.find('[data-test="resurface"]').exists()).toBe(false);
    again.unmount();

    api.brief.mockImplementation(async () => brief({ date: "2026-10-22" }));
    const nextDay = mount(HomeView);
    await flushPromises();
    expect(nextDay.find('[data-test="resurface"]').text()).toContain("Lives in Lyon");
  });

  it("drops the brief's text when it can't be reloaded after a privacy change", async () => {
    const w = mount(HomeView);
    await flushPromises();
    expect(w.find('[data-test="today"]').text()).toContain("Prefers tea over coffee");

    api.brief.mockImplementation(async () => { throw new Error("offline"); });
    store.memories = [{ id: "m1", topic: "work", version: 1, private: true } as Memory];
    store.version++;
    await flushPromises();
    expect(w.text()).not.toContain("Prefers tea over coffee");
    expect(w.text()).not.toContain("Lives in Lyon");
    expect(w.find('[data-test="safety"]').exists()).toBe(true);
  });

  it("drops the brief's text when the whole reload fails after a privacy change", async () => {
    const w = mount(HomeView);
    await flushPromises();
    expect(w.find('[data-test="today"]').text()).toContain("Prefers tea over coffee");

    api.overview.mockImplementation(async () => { throw new Error("offline"); });
    api.brief.mockImplementation(async () => { throw new Error("offline"); });
    store.memories = [{ id: "m1", topic: "work", version: 1, private: true } as Memory];
    store.version++;
    await flushPromises();
    expect(w.text()).not.toContain("Prefers tea over coffee");
    expect(w.text()).not.toContain("Lives in Lyon");
    expect(w.find('[data-test="safety"]').exists()).toBe(true); // the counts stay
  });
});
