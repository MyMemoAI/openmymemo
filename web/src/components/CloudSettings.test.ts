import { flushPromises, mount, type VueWrapper } from "@vue/test-utils";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { CloudSettings as Prefs, CloudStatus } from "../api";
import { store } from "../store";
import CloudSettings from "./CloudSettings.vue";

const MYMEMO = "https://api.mymemo.ai";

function status(extra: Partial<CloudStatus> = {}, prefs: Partial<Prefs> = {}): CloudStatus {
  const settings = { show: true, service: "", default_service: MYMEMO, service_from_env: null, ...prefs };
  const service = settings.service_from_env ?? (settings.service || MYMEMO);
  return {
    linked: false, shown: settings.show, settings, service, endpoint: `${service}/openmymemo/mcp`, pending: false,
    link_error: null, folder: null, this_folder: false, account: null, plan: null, linked_at: null, state: "stopped",
    reason: null, online: false, close_code: null, last_call_at: null, queued: null, apps: [], ...extra,
  };
}

const { api } = vi.hoisted(() => ({ api: { cloud: vi.fn(), cloudSettings: vi.fn() } }));
vi.mock("../api", () => ({ api }));

const button = (w: VueWrapper, text: string) => w.findAll("button").find((b) => b.text() === text);

describe("CloudSettings", () => {
  let w: VueWrapper | null = null;
  const mountIt = async () => {
    w = mount(CloudSettings);
    await flushPromises();
    return w;
  };
  beforeEach(() => {
    vi.clearAllMocks();
    store.toast = null;
    store.cloudVersion = 0;
  });
  afterEach(() => w?.unmount());

  it("is on by default with the service, and hides it when turned off", async () => {
    api.cloud.mockResolvedValue(status());
    api.cloudSettings.mockResolvedValueOnce(status({ shown: false }, { show: false }))
      .mockResolvedValueOnce(status());
    const w = await mountIt();
    const sw = w.get('[role="switch"]');
    expect(sw.attributes("aria-checked")).toBe("true");
    expect(w.text()).toContain("It does nothing until you sign in; turn this off to hide it.");
    expect(w.get("input").element.value).toBe(MYMEMO);
    expect(button(w, "Save")!.attributes("disabled")).toBeDefined(); // nothing changed yet
    await sw.trigger("click");
    await flushPromises();
    expect(api.cloudSettings).toHaveBeenCalledWith({ show: false });
    expect(store.cloudVersion).toBe(1); // the Connectors page reloads its list
    expect(sw.attributes("aria-checked")).toBe("false");
    expect(w.find("input").exists()).toBe(false);
    await sw.trigger("click");
    await flushPromises();
    expect(api.cloudSettings).toHaveBeenLastCalledWith({ show: true });
    expect(w.get("input").element.value).toBe(MYMEMO);
  });

  it("chooses another service, and goes back to MyMemo's own", async () => {
    api.cloud.mockResolvedValue(status({}, { show: true }));
    api.cloudSettings.mockResolvedValueOnce(status({}, { show: true, service: "https://memo.example.org" }))
      .mockResolvedValueOnce(status({}, { show: true }));
    const w = await mountIt();
    expect(button(w, "Use MyMemo's own")).toBeUndefined();
    await w.get("input").setValue("https://memo.example.org/ ");
    await w.get("form").trigger("submit");
    await flushPromises();
    expect(api.cloudSettings).toHaveBeenCalledWith({ service: "https://memo.example.org/" });
    expect(store.toast?.text).toBe("OpenMyMemo Cloud now uses https://memo.example.org");
    await button(w, "Use MyMemo's own")!.trigger("click");
    await flushPromises();
    expect(api.cloudSettings).toHaveBeenLastCalledWith({ service: "" });
    expect(w.get("input").element.value).toBe(MYMEMO);
  });

  it("says why nothing can change while linked, or when the environment sets the service", async () => {
    api.cloud.mockResolvedValue(status({ linked: true, shown: true }, { service: "https://memo.example.org" }));
    let w = await mountIt();
    expect(w.get('[role="switch"]').attributes("disabled")).toBeDefined();
    expect(w.text()).toContain("Unlink it on the Connectors page to hide it.");
    expect(w.text()).toContain("Unlink this computer to choose another service.");
    expect(w.get("input").attributes("disabled")).toBeDefined();
    expect(button(w, "Save")).toBeUndefined();
    w.unmount();

    api.cloud.mockResolvedValue(status({}, { show: true, service_from_env: "http://127.0.0.1:8000" }));
    w = await mountIt();
    expect(w.get("input").element.value).toBe("http://127.0.0.1:8000");
    expect(w.text()).toContain("Set by OPENMYMEMO_CLOUD_URL");
  });

  it("shows a refused change, and nothing at all without OpenMyMemo Cloud", async () => {
    api.cloud.mockResolvedValue(status({}, { show: true }));
    api.cloudSettings.mockRejectedValue(new Error("The service's address must start with https://"));
    let w = await mountIt();
    await w.get("input").setValue("http://example.com");
    await w.get("form").trigger("submit");
    await flushPromises();
    expect(store.toast).toEqual({ text: "The service's address must start with https://", kind: "error" });
    expect(store.cloudVersion).toBe(0);
    w.unmount();

    api.cloud.mockRejectedValue(new Error("Not Found"));
    w = await mountIt();
    expect(w.html()).toBe("<!--v-if-->");
  });
});
