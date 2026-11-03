import { enableAutoUnmount, flushPromises, mount } from "@vue/test-utils";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { FeedbackLinks } from "../api";
import FeedbackDialog from "./FeedbackDialog.vue";

const links: FeedbackLinks = {
  issue_url: "https://github.com/MyMemoAI/openmymemo/issues/new?template=feedback.yml&version=0.4.0&os=macOS%2015.4%20%28arm64%29",
  email: "leo@mymemo.ai",
  email_url: "mailto:leo@mymemo.ai?subject=OpenMyMemo%20feedback&body=x",
  about: "OpenMyMemo 0.4.0 · macOS 15.4 (arm64)",
  version: "0.4.0",
  system: "macOS 15.4 (arm64)",
};

const { api } = vi.hoisted(() => ({ api: { feedback: vi.fn() } }));
vi.mock("../api", () => ({ api }));

enableAutoUnmount(afterEach);

describe("Send feedback", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.feedback.mockImplementation(async () => links);
  });

  it("offers GitHub and email, says what is filled in, and sends nothing itself", async () => {
    const w = mount(FeedbackDialog, { attachTo: document.body });
    await flushPromises();
    expect(api.feedback).toHaveBeenCalledWith(false); // a browser: not the Mac or Windows app
    const github = document.querySelector<HTMLAnchorElement>('[data-test="fb-github"]')!;
    expect(github.href).toBe(links.issue_url);
    expect(github.target).toBe("_blank");
    expect(document.querySelector<HTMLAnchorElement>('[data-test="fb-email"]')!.href).toBe(links.email_url);
    expect(document.body.textContent).toContain("Private, to leo@mymemo.ai.");
    expect(document.querySelector('[data-test="fb-about"]')!.textContent).toContain(links.about);
    expect(document.body.textContent).toContain("Please don't include your memories");
    w.unmount();
  });

  it("says why when the links can't be had", async () => {
    api.feedback.mockImplementation(async () => { throw new Error("OpenMyMemo isn't running"); });
    const w = mount(FeedbackDialog, { attachTo: document.body });
    await flushPromises();
    expect(document.body.textContent).toContain("OpenMyMemo isn't running");
    expect(document.querySelector('[data-test="fb-github"]')).toBeNull();
    w.unmount();
  });
});
