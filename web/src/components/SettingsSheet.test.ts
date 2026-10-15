import { flushPromises, mount } from "@vue/test-utils";
import { describe, expect, it, vi } from "vitest";
import SettingsSheet from "./SettingsSheet.vue";

vi.mock("../desktop", () => ({ native: false, shell: { settings: vi.fn(async () => null) } }));

describe("Settings", () => {
  it("reopens the welcome guide", async () => {
    history.replaceState(null, "", "#/memories");
    const w = mount(SettingsSheet, {
      global: { stubs: { ApprovalSwitch: true, CloudSettings: true, ModalDialog: { template: "<div><slot /></div>" } } },
    });
    await flushPromises();
    const open = w.findAll("button").find((b) => b.text() === "Open the welcome guide")!;
    await open.trigger("click");
    expect(location.hash).toBe("#/welcome");
    expect(w.emitted("close")).toEqual([[]]);
  });
});
