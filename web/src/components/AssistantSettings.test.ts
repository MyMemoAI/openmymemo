import { flushPromises, mount, type VueWrapper } from "@vue/test-utils";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { AssistantStatus } from "../api";
import { store } from "../store";
import AssistantSettings from "./AssistantSettings.vue";

function status(extra: Partial<AssistantStatus> = {}): AssistantStatus {
  return {
    enabled: false, kind: "local", protocol: "openai", base_url: "", endpoint: "http://127.0.0.1:11434/v1",
    host: "127.0.0.1", model: "", embeddings_model: "", embeddings: false, has_key: false, key_store: null,
    features: { import: true, sort: true, recall: true, check: true, session: false, tidy: false },
    private_topics: [], caps: { calls: 1000, tokens: 2000000 }, usage: { month: "2026-10", calls: 3, tokens: 1234 },
    timeout: 60, check_budget: 2, session_apps: { "claude-code": null, codex: null }, from_env: [], local: true, problem: "Choose a model.", ready: false, reason: "The Assistant is off.",
    where: "The Assistant is off: memory text goes nowhere.", connector_id: null, revoked: false,
    kinds: { local: true, key: true, mymemo: true }, mymemo: null, ...extra,
  };
}

const { api } = vi.hoisted(() => ({
  api: {
    assistant: vi.fn(), assistantSettings: vi.fn(), assistantTest: vi.fn(), assistantSignIn: vi.fn(),
    assistantAccount: vi.fn(), assistantSignOut: vi.fn(), assistantSessions: vi.fn(),
  },
}));
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

  it("is off by default, with MyMemo as one of the choices", async () => {
    api.assistant.mockResolvedValue(status());
    const w = await mountIt();
    expect(radio(w, "Off").attributes("aria-checked")).toBe("true");
    expect(radio(w, "MyMemo").attributes("disabled")).toBeUndefined();
    expect(w.get('[data-test="where"]').text()).toContain("goes nowhere");
    expect(w.find("form").exists()).toBe(false);
  });

  it("leaves MyMemo out while the server doesn't offer it", async () => {
    api.assistant.mockResolvedValue(status({ kinds: { local: true, key: true, mymemo: false } }));
    const w = await mountIt();
    expect(w.findAll('[role="radio"]').map((b) => b.text())).toEqual(["Off", "On this computer", "Your own API key"]);
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

  describe("MyMemo's hosted model", () => {
    const mymemo = (extra: Partial<NonNullable<AssistantStatus["mymemo"]>> = {}) => ({
      signed_in: false, service: "https://api.mymemo.ai", via: null, account: null, signed_in_at: null,
      signed_out: null, cloud_linked: false, hold: null, pending: false, signin_error: null, ...extra,
    });
    const on = (m: ReturnType<typeof mymemo>, extra: Partial<AssistantStatus> = {}) => status({
      enabled: true, kind: "mymemo", local: false, endpoint: "https://api.mymemo.ai/openmymemo/assistant/v1",
      host: "api.mymemo.ai", problem: m.signed_in ? null : "Sign in to MyMemo to use its hosted model.",
      ready: m.signed_in, where: "Memory text goes to MyMemo's service and Anthropic, under MyMemo's privacy policy.",
      has_key: m.signed_in, mymemo: m, ...extra,
    });
    const account = {
      ok: true as const, model: "claude-haiku-5-5", display_name: "Claude Haiku 5.5", provider: "Anthropic",
      plan: { status: "active", active: true, period_start: "2026-10-01T00:00:00Z", period_end: "2026-11-01T00:00:00Z",
        resets_at: "2026-11-01T00:00:00Z", cancel_at_period_end: false, plan_url: "https://app.mymemo.ai/openmymemo/assistant" },
      usage: { input_tokens: 1200, output_tokens: 340, input_tokens_included: 10000, output_tokens_included: 2000, requests: 7 },
      limits: { requests_per_minute: 10, requests_per_day: 100 },
    };
    let opened: string[] = [];
    beforeEach(() => {
      opened = [];
      vi.spyOn(window, "open").mockImplementation((url) => {
        opened.push(String(url));
        return null;
      });
    });

    it("signs in with the OpenMyMemo Cloud link, then shows the account, model, plan and usage", async () => {
      api.assistant.mockResolvedValue(status());
      api.assistantSettings.mockResolvedValue(on(mymemo({ cloud_linked: true })));
      const signed = on(mymemo({ signed_in: true, via: "cloud", cloud_linked: true,
        account: { display_name: "Sam", email_masked: "s***@example.com" } }));
      api.assistantSignIn.mockResolvedValue({ signed_in: true, url: null, assistant: signed });
      api.assistantAccount.mockResolvedValue({ account, assistant: signed });
      const w = await mountIt();
      await radio(w, "MyMemo").trigger("click");
      await flushPromises();
      expect(api.assistantSettings).toHaveBeenCalledWith({ enabled: true, kind: "mymemo" });
      expect(w.find("form input[type=password]").exists()).toBe(false); // no key or address for MyMemo
      expect(w.find('[data-test="embeddings"]').exists()).toBe(false); // MyMemo has no embeddings model
      expect(w.get('[data-test="mm-no-embeddings"]').text()).toContain("has no embeddings");
      expect(w.text()).toContain("no browser is needed");
      await button(w, "Sign in to MyMemo")!.trigger("click");
      await flushPromises();
      expect(api.assistantSignIn).toHaveBeenCalledWith(false);
      expect(opened).toEqual([]);
      expect(w.get('[data-test="mm-account"]').text()).toBe("Signed in as Sam (s***@example.com) with this computer's OpenMyMemo Cloud link");
      expect(w.get('[data-test="mm-model"]').text()).toBe("Model: Claude Haiku 5.5 by Anthropic");
      expect(w.get('[data-test="mm-plan"]').text()).toBe("OpenMyMemo Assistant plan: active");
      expect(w.get('[data-test="mm-usage"]').text()).toContain("1,200 of 10,000 input tokens, 340 of 2,000 output tokens, 7 requests.");
      expect(w.get('[data-test="mm-usage"]').text()).toContain("Resets November 1.");
      const link = w.get('[data-test="mm-plan-link"]');
      expect(link.text()).toBe("Manage");
      await link.trigger("click");
      expect(opened).toEqual(["https://app.mymemo.ai/openmymemo/assistant"]);
    });

    it("signs in with the browser and waits for it", async () => {
      vi.useFakeTimers();
      try {
        const waiting = on(mymemo({ pending: true }));
        api.assistant.mockResolvedValue(on(mymemo()));
        api.assistantSignIn.mockResolvedValue({ signed_in: false, url: "https://api.mymemo.ai/openmymemo/oauth/authorize?x=1", assistant: waiting });
        api.assistantAccount.mockResolvedValue({ account: { ...account, plan: { ...account.plan, active: false, status: "none" } },
          assistant: on(mymemo({ signed_in: true, via: "browser" })) });
        const w = await mountIt();
        await button(w, "Sign in to MyMemo")!.trigger("click");
        await flushPromises();
        expect(opened).toEqual(["https://api.mymemo.ai/openmymemo/oauth/authorize?x=1"]);
        expect(w.get('[data-test="mm-waiting"]').text()).toContain("Waiting for you");
        api.assistant.mockResolvedValue(on(mymemo({ signed_in: true, via: "browser" })));
        await vi.advanceTimersByTimeAsync(2100);
        await flushPromises();
        expect(w.get('[data-test="mm-account"]').text()).toBe("Signed in to MyMemo at api.mymemo.ai");
        expect(w.get('[data-test="mm-plan"]').text()).toContain("No OpenMyMemo Assistant plan yet");
        expect(w.get('[data-test="mm-plan-link"]').text()).toBe("Get the plan");
      } finally {
        vi.useRealTimers();
      }
    });

    it("shows a pause calmly and signs out", async () => {
      const held = on(mymemo({ signed_in: true, via: "browser", hold: {
        code: "quota_exceeded", message: "This month's allowance is used up, resets November 1. Until then, OpenMyMemo uses its rules.",
        plan_url: "https://app.mymemo.ai/openmymemo/assistant", resets_at: "2026-11-01T00:00:00Z", until: "2026-11-01T00:00:00Z" } }));
      api.assistant.mockResolvedValue(held);
      api.assistantAccount.mockResolvedValue({ account: { ok: false, error: "MyMemo couldn't show your plan right now.", signed_out: false }, assistant: held });
      api.assistantSignOut.mockResolvedValue({ message: "Signed out of MyMemo.", assistant: on(mymemo()) });
      const w = await mountIt();
      const hold = w.get('[data-test="mm-hold"]');
      expect(hold.text()).toContain("allowance is used up, resets November 1");
      expect(hold.classes()).not.toContain("text-danger");
      expect(w.get('[data-test="mm-plan-link"]').text()).toBe("Get the plan");
      await button(w, "Sign out")!.trigger("click");
      await flushPromises();
      expect(api.assistantSignOut).toHaveBeenCalled();
      expect(store.toast?.text).toBe("Signed out of MyMemo.");
      expect(button(w, "Sign in to MyMemo")).toBeDefined();
    });
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
