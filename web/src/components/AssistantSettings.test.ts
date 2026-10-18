import { flushPromises, mount, type VueWrapper } from "@vue/test-utils";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { AssistantStatus } from "../api";
import { store } from "../store";
import AssistantSettings from "./AssistantSettings.vue";

function status(extra: Partial<AssistantStatus> = {}): AssistantStatus {
  return {
    enabled: false, kind: "local", protocol: "openai", base_url: "", endpoint: "http://127.0.0.1:11434/v1",
    host: "127.0.0.1", model: "", has_key: false, key_store: null,
    features: { import: true, sort: true, recall: true, check: true, session: false, tidy: false },
    private_topics: [], caps: { calls: 1000, tokens: 2000000 }, usage: { month: "2026-10", calls: 3, tokens: 1234 },
    timeout: 60, check_budget: 2, session_apps: { "claude-code": null, codex: null }, from_env: [], local: true, problem: "Choose a model.", ready: false, reason: "The Assistant is off.",
    where: "The Assistant is off: memory text goes nowhere.", connector_id: null, revoked: false,
    kinds: { local: true, key: true, mymemo: false }, ...extra,
  };
}

const { api } = vi.hoisted(() => ({ api: { assistant: vi.fn(), assistantSettings: vi.fn(), assistantTest: vi.fn(), assistantSessions: vi.fn() } }));
vi.mock("../api", () => ({ api }));

const radio = (w: VueWrapper, text: string) => w.findAll('[role="radio"]').find((b) => b.text() === text)!;
const button = (w: VueWrapper, text: string) => w.findAll("button").find((b) => b.text() === text);

describe("AssistantSettings", () => {
  let w: VueWrapper | null = null;
  const mountIt = async () => {
    w = mount(AssistantSettings);
    await flushPromises();
    return w;
  };
  beforeEach(() => {
    vi.clearAllMocks();
    store.toast = null;
    store.state = null;
  });
  afterEach(() => w?.unmount());

  it("is off by default, with MyMemo shown as coming soon", async () => {
    api.assistant.mockResolvedValue(status());
    const w = await mountIt();
    expect(radio(w, "Off").attributes("aria-checked")).toBe("true");
    expect(radio(w, "MyMemo (coming soon)").attributes("disabled")).toBeDefined();
    expect(w.get('[data-test="where"]').text()).toContain("goes nowhere");
    expect(w.find("form").exists()).toBe(false);
  });

  it("turns on with a key, never shows the key, and tests the model", async () => {
    api.assistant.mockResolvedValue(status());
    const on = status({ enabled: true, kind: "key", protocol: "anthropic", endpoint: "https://api.anthropic.com",
      host: "api.anthropic.com", local: false, where: "Memory text goes to api.anthropic.com, under that provider's terms." });
    api.assistantSettings.mockResolvedValueOnce(on)
      .mockResolvedValueOnce({ ...on, model: "claude-haiku-5-5", has_key: true, key_store: "keychain", problem: null, ready: true });
    api.assistantTest.mockResolvedValue({ ok: true, model: "claude-haiku-5-5", ms: 420, error: null });
    const w = await mountIt();
    await radio(w, "Your own API key").trigger("click");
    await flushPromises();
    expect(api.assistantSettings).toHaveBeenCalledWith({ enabled: true, kind: "key" });
    expect(w.get('[data-test="where"]').text()).toContain("api.anthropic.com");
    const inputs = w.findAll("input");
    await inputs[1].setValue("claude-haiku-5-5");
    const key = w.get('input[type="password"]');
    await key.setValue("sk-ant-secret");
    await w.get("form").trigger("submit");
    await flushPromises();
    expect(api.assistantSettings).toHaveBeenLastCalledWith({
      model: "claude-haiku-5-5", base_url: "", protocol: "anthropic", api_key: "sk-ant-secret",
    });
    expect((w.get('input[type="password"]').element as HTMLInputElement).value).toBe(""); // write-only
    expect(w.get('input[type="password"]').attributes("placeholder")).toContain("Keychain");
    api.assistant.mockResolvedValue({ ...on, model: "claude-haiku-5-5", has_key: true, problem: null });
    await button(w, "Test")!.trigger("click");
    await flushPromises();
    expect(w.get('[data-test="tested"]').text()).toBe("claude-haiku-5-5 answered in 420 ms.");
  });

  it("switches features and keeps private topics local", async () => {
    store.state = { topics: [{ slug: "health", title: "Health", private: true }] } as never;
    const on = status({ enabled: true, model: "llama3.2", problem: null });
    api.assistant.mockResolvedValue(on);
    api.assistantSettings.mockResolvedValue(on);
    const w = await mountIt();
    await w.get('[aria-label="Tidy up"]').trigger("click");
    await flushPromises();
    expect(api.assistantSettings).toHaveBeenCalledWith({ features: { tidy: true } });
    const pill = w.findAll("button").find((b) => b.text() === "Health")!;
    await pill.trigger("click");
    await flushPromises();
    expect(api.assistantSettings).toHaveBeenLastCalledWith({ private_topics: ["health"] });
    expect(w.get('[data-test="usage"]').text()).toBe("This month: 3 calls, 1,234 tokens.");
  });

  it("says private topics stay home with a remote model", async () => {
    store.state = { topics: [{ slug: "health", title: "Health", private: true }] } as never;
    api.assistant.mockResolvedValue(status({ enabled: true, kind: "key", local: false }));
    const w = await mountIt();
    expect(w.text()).toContain("never sent to a model outside this computer");
    expect(w.findAll("button").find((b) => b.text() === "Health")).toBeUndefined();
  });

  it("asks per app before reading sessions", async () => {
    const on = status({ enabled: true, model: "llama3.2", problem: null, features: { import: true, sort: true, recall: true, check: true, session: true, tidy: false }, session_apps: { "claude-code": "asked", codex: null } });
    api.assistant.mockResolvedValue(on);
    api.assistantSessions.mockResolvedValue({ ...on, session_apps: { "claude-code": "allowed", codex: null } });
    const w = await mountIt();
    const rows = w.get('[data-test="session-apps"]');
    expect(rows.text()).toContain("Claude Code");
    expect(rows.text()).toContain("Asking you");
    expect(rows.text()).toContain("Not asked yet");
    await rows.findAll("button").find((b) => b.text() === "Allow")!.trigger("click");
    await flushPromises();
    expect(api.assistantSessions).toHaveBeenCalledWith("claude-code", true);
    expect(w.get('[data-test="session-apps"]').text()).toContain("Allowed");
  });

  it("doesn't list apps while reading sessions is off", async () => {
    api.assistant.mockResolvedValue(status({ enabled: true, model: "m", problem: null }));
    const w = await mountIt();
    expect(w.find('[data-test="session-apps"]').exists()).toBe(false);
  });
});
