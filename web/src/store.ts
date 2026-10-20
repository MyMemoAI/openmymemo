import { computed, reactive } from "vue";
import { api, type Connector, type Memory, type Proposal, type State, type Topic } from "./api";
import { prettyName, t } from "./i18n";

export const store = reactive({
  state: null as State | null,
  memories: [] as Memory[],
  /** Proposals waiting for the user's decision, newest first. */
  pending: [] as Proposal[],
  /** The digest: changes that applied without the user and that they haven't looked at yet, newest first. */
  unreviewed: [] as Proposal[],
  connectors: [] as Connector[],
  /** Bumped after every successful refresh, so views that load their own data can follow along. */
  version: 0,
  /** Bumped when OpenMyMemo Cloud is shown, hidden or pointed at another service in Settings. */
  cloudVersion: 0,
  offline: false,
  toast: null as { text: string; kind: "ok" | "error" } | null,
});

let toastTimer: number | undefined;

export function toast(text: string, kind: "ok" | "error" = "ok") {
  store.toast = { text, kind };
  window.clearTimeout(toastTimer);
  toastTimer = window.setTimeout(() => (store.toast = null), kind === "error" ? 6000 : 2500);
}

export async function refresh() {
  try {
    const [state, memories, pending, unreviewed, connectors] = await Promise.all([
      api.state(),
      api.memories(),
      api.proposals({ status: "pending", limit: 1000 }),
      api.proposals({ unreviewed: true, limit: 1000 }),
      api.connectors(),
    ]);
    Object.assign(store, { state, memories, pending, unreviewed, connectors, offline: false });
    store.version++;
  } catch {
    store.offline = true;
  }
}

/** Run a mutation, report errors, then reload everything (the vault is small). */
export async function act<T>(fn: () => Promise<T>, okText?: string): Promise<T | undefined> {
  try {
    const result = await fn();
    if (okText) toast(okText);
    await refresh();
    return result;
  } catch (e) {
    toast(e instanceof Error ? e.message : String(e), "error");
    return undefined;
  }
}

export const topicsBySlug = computed<Record<string, Topic>>(() =>
  Object.fromEntries((store.state?.topics ?? []).map((tp) => [tp.slug, tp])),
);

/** Waiting for a decision, oldest first (a queue). */
export const pending = computed(() => [...store.pending].reverse());

/** How many things the Changes page has for the user: the digest plus what waits. */
export const toReview = computed(() => store.pending.length + store.unreviewed.length);

/** Memory id → the app that changed it, for memories in the digest (the "New" chip). */
export const freshMemories = computed<Record<string, string>>(() => {
  const out: Record<string, string> = {};
  for (const p of store.unreviewed) {
    if (p.applied_memory_id && !(p.applied_memory_id in out)) out[p.applied_memory_id] = p.connector_name;
  }
  return out;
});

/** Topics with something in the digest (a green dot in the sidebar). */
export const freshTopics = computed(() => new Set(store.unreviewed.map((p) => p.final_topic ?? p.topic)));

/** Topics with something waiting for a decision (an amber dot in the sidebar). */
export const waitingTopics = computed(() => new Set(store.pending.map((p) => p.topic)));

/** The topic an import from `source` lands in ("From ChatGPT"), named before its first import creates it. */
export function importTopicTitle(source: string): string {
  return topicsBySlug.value[`from-${source}`]?.title ?? `From ${prettyName(source)}`;
}


/** Evidence in quotation marks, unless it already starts with the quoted words (evidence read from a chat). */
export function quoted(text: string): string {
  return text.startsWith("“") ? text : `“${text}”`;
}

export function topicTitle(slug: string | null | undefined): string {
  if (!slug) return "";
  return topicsBySlug.value[slug]?.title ?? slug;
}

export function connectorName(id: string): string {
  if (id === "user") return t("source.user");
  if (id === "mymemo-cloud") return t("cloud.name"); // linking it is logged; it isn't a connector itself
  return store.connectors.find((c) => c.id === id)?.name ?? id;
}

export async function copy(text: string) {
  try {
    await navigator.clipboard.writeText(text);
    toast(t("common.copied"));
  } catch {
    toast(text, "error");
  }
}
