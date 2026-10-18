import { flushPromises, mount, type VueWrapper } from "@vue/test-utils";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { SemanticModel, SemanticStatus } from "../api";
import { store } from "../store";
import SemanticSettings from "./SemanticSettings.vue";

const model = (id: string, extra: Partial<SemanticModel> = {}): SemanticModel => ({
  id, name: id, size_mb: 135, dims: 384, languages: "About 100 languages", license: "MIT",
  license_url: "https://example.com/license", terms_url: null, recommended: false, note: "A model.",
  available: true, downloaded: false, ...extra,
});

function status(extra: Partial<SemanticStatus> = {}): SemanticStatus {
  return {
    enabled: false, model: "multilingual-e5-small", accepted_terms: [], ready: false,
    models: [
      model("multilingual-e5-small", { recommended: true }),
      model("embeddinggemma-300m", { license: "Gemma Terms of Use", terms_url: "https://ai.google.dev/gemma/terms" }),
      model("qwen3-embedding-0.6b", { available: false, size_mb: 640 }),
    ],
    runtime: { installed: true, command: null },
    job: { phase: "idle", model: null, done: 0, total: 0, error: null, code: null },
    embedder: null, endpoint: false, index: { model: null, count: 0, memories: 12 }, ...extra,
  };
}

const { api } = vi.hoisted(() => ({ api: { semantic: vi.fn(), semanticSet: vi.fn() } }));
vi.mock("../api", () => ({ api }));

describe("SemanticSettings", () => {
  let w: VueWrapper | null = null;
  const mountIt = async () => {
    w = mount(SemanticSettings);
    await flushPromises();
    return w;
  };
  beforeEach(() => {
    vi.clearAllMocks();
    store.toast = null;
  });
  afterEach(() => {
    w?.unmount();
    vi.useRealTimers();
  });

  it("lists the models with size and license, the recommended one picked, and nothing downloaded until asked", async () => {
    api.semantic.mockResolvedValue(status());
    const w = await mountIt();
    expect(w.text()).toContain("Search by meaning on this computer");
    expect(w.text()).toContain("Recommended");
    expect(w.text()).toContain("about 135 MB");
    expect(w.text()).toContain("Not available yet");
    expect((w.get('input[value="multilingual-e5-small"]').element as HTMLInputElement).checked).toBe(true);
    expect(api.semanticSet).not.toHaveBeenCalled();
    expect(w.get('[data-test="start"]').text()).toBe("Download and turn on (about 135 MB)");
  });

  it("shows the download's progress, then that it is on", async () => {
    vi.useFakeTimers();
    api.semantic.mockResolvedValueOnce(status());
    const w = await mountIt();
    api.semanticSet.mockResolvedValue(status({ job: { phase: "download", model: "multilingual-e5-small", done: 50 * 2 ** 20, total: 135 * 2 ** 20, error: null, code: null } }));
    await w.get('[data-test="start"]').trigger("click");
    await flushPromises();
    expect(api.semanticSet).toHaveBeenCalledWith({ enabled: true, model: "multilingual-e5-small", accept_terms: undefined });
    expect(w.get('[data-test="progress"]').text()).toContain("Downloading: 50 of 135 MB");
    api.semantic.mockResolvedValue(status({ enabled: true, ready: true, embedder: "local",
      job: { phase: "done", model: "multilingual-e5-small", done: 12, total: 12, error: null, code: null },
      index: { model: "local/multilingual-e5-small@x", count: 12, memories: 12, current: true } }));
    await vi.advanceTimersByTimeAsync(800);
    await flushPromises();
    expect(api.semantic).toHaveBeenCalledTimes(3); // mount, the poll that saw "done", and one final read
    expect(w.get('[data-test="ready"]').text()).toBe("On: 12 of 12 memories indexed.");
    expect(store.toast?.text).toBe("Search by meaning is on.");
  });

  it("asks to accept EmbeddingGemma's terms before downloading", async () => {
    api.semantic.mockResolvedValue(status());
    const w = await mountIt();
    await w.get('input[value="embeddinggemma-300m"]').setValue(true);
    expect(w.text()).toContain("Accept the Gemma Terms of Use to download this model.");
    expect(w.get('[data-test="start"]').attributes("disabled")).toBeDefined();
    api.semanticSet.mockResolvedValue(status());
    await w.get('input[type="checkbox"]').setValue(true);
    await w.get('[data-test="start"]').trigger("click");
    expect(api.semanticSet).toHaveBeenCalledWith({ enabled: true, model: "embeddinggemma-300m", accept_terms: true });
  });

  it("gives the exact command when the runtime can't be installed, and turns off", async () => {
    api.semantic.mockResolvedValue(status({ enabled: true, runtime: { installed: false, command: "uv pip install --target /x onnxruntime" },
      job: { phase: "error", model: null, done: 0, total: 0, error: "Install it", code: "runtime" } }));
    const w = await mountIt();
    expect(w.get('[data-test="runtime"]').text()).toContain("uv pip install --target /x onnxruntime");
    api.semanticSet.mockResolvedValue(status());
    await w.get('[role="switch"]').trigger("click");
    await flushPromises();
    expect(api.semanticSet).toHaveBeenCalledWith({ enabled: false });
    expect(store.toast?.text).toContain("the model and the index were deleted");
  });
});
