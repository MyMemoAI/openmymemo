import { flushPromises, mount } from "@vue/test-utils";
import { describe, expect, it, vi } from "vitest";
import SettingsSheet from "./SettingsSheet.vue";

const shell = vi.hoisted(() => {
  const state = { check_updates: true };
  return {
    state,
    settings: vi.fn(async () => ({
      vault: "/Users/me/OpenMyMemo",
      autostart: false,
      appearance: "system",
      notifications: true,
      check_updates: state.check_updates,
      version: "0.3.0",
      launcher: "",
      log: "",
    })),
    setCheckUpdates: vi.fn(async (enabled: boolean) => {
      state.check_updates = enabled;
    }),
    checkForUpdates: vi.fn(async () => undefined),
  };
});
vi.mock("../desktop", () => ({ native: true, shell }));

describe("Settings in the app", () => {
  it("turns automatic update checks off and checks on request", async () => {
    const w = mount(SettingsSheet, {
      global: { stubs: { ApprovalSwitch: true, AssistantSettings: true, CloudSettings: true, ModalDialog: { template: "<div><slot /></div>" } } },
    });
    await flushPromises();
    const toggle = w.get('[role="switch"][aria-label="Check for updates automatically"]');
    expect(toggle.attributes("aria-checked")).toBe("true");
    await toggle.trigger("click");
    await flushPromises();
    expect(shell.setCheckUpdates).toHaveBeenCalledWith(false);
    expect(toggle.attributes("aria-checked")).toBe("false");

    await w.findAll("button").find((b) => b.text() === "Check Now")!.trigger("click");
    await flushPromises();
    expect(shell.checkForUpdates).toHaveBeenCalledOnce();
  });
});
