import { computed, ref, watch, type Ref } from "vue";
import { api, type Kind, type Memory } from "./api";
import { listNames, sourceLabel, tn } from "./i18n";
import { connectorName, pending, store, topicTitle } from "./store";

/** The Memories screen's filters and grouping, shared by the web layout and the Mac layout. */
export type Status = "active" | "pending" | "review" | "archived";

export const srcLabel = (s: string) => (s.startsWith("connector:") ? connectorName(s.slice(10)) : sourceLabel(s));
/** Filter key for a memory's source: each app and each import on its own, everything else is "Me". */
export const sourceKey = (s: string) => (s.startsWith("connector:") || s.startsWith("import:") ? s : "user");

/** The banner on the overview: changes not looked at yet, then what waits for a decision. */
export const banner = computed(() => {
  const changed = store.unreviewed.length;
  const waiting = pending.value.length;
  if (!changed && !waiting) return "";
  const who = listNames([...new Set([...store.unreviewed, ...pending.value].map((p) => p.connector_name))]);
  if (!changed) return tn("mem.banner.pending", waiting, { who });
  return tn("mem.banner.unreviewed", changed, { who }) + (waiting ? tn("mem.banner.andWaiting", waiting) : "");
});

/** How long the search box waits after typing before asking the server (search by meaning, §17.4). */
export const SEARCH_DELAY = 250;

export function useMemoryList(topic: Ref<string | null>) {
  const kind = ref<Kind | null>(null);
  const source = ref<string | null>(null);
  const status = ref<Status>("active");
  const query = ref("");
  const archived = ref<Memory[]>([]);
  /** The server's ranking for the current query when it searches by meaning (memory id → rank); null when it
   * doesn't (no embedding index), and the box filters by text as it always has. */
  const ranked = ref<Map<string, number> | null>(null);
  let searchTimer: ReturnType<typeof setTimeout> | null = null;
  let searchSeq = 0;

  watch(query, (q) => {
    if (searchTimer) clearTimeout(searchTimer);
    const text = q.trim();
    const seq = ++searchSeq;
    if (!text) {
      ranked.value = null;
      return;
    }
    searchTimer = setTimeout(async () => {
      try {
        const found = await api.search(text);
        if (seq !== searchSeq) return; // a newer query was typed meanwhile
        ranked.value = found.mode === "keyword" ? null : new Map(found.results.map((r, i) => [r.id, i]));
      } catch {
        if (seq === searchSeq) ranked.value = null;
      }
    }, SEARCH_DELAY);
  });

  watch(status, async (v) => {
    if (v === "archived") archived.value = await api.memories("archived");
  });

  const counts = computed(() => {
    const kinds: Record<string, number> = {};
    const sources: Record<string, number> = {};
    for (const m of store.memories) {
      if (topic.value && m.topic !== topic.value) continue;
      kinds[m.kind] = (kinds[m.kind] ?? 0) + 1;
      const key = sourceKey(m.source);
      sources[key] = (sources[key] ?? 0) + 1;
    }
    return { kinds, sources };
  });

  const reviewDue = computed(() => store.memories.filter((m) => m.review_due));
  const topicPending = computed(() => pending.value.filter((p) => !topic.value || p.topic === topic.value));

  function matches(m: Memory) {
    if (topic.value && m.topic !== topic.value) return false;
    if (kind.value && m.kind !== kind.value) return false;
    if (source.value && sourceKey(m.source) !== source.value) return false;
    const q = query.value.trim().toLowerCase();
    return !q || m.content.toLowerCase().includes(q) || !!ranked.value?.has(m.id);
  }

  const visible = computed(() => {
    const base = status.value === "archived" ? archived.value : status.value === "review" ? reviewDue.value : store.memories;
    const rows = base.filter(matches);
    const rank = ranked.value;
    if (!rank || !query.value.trim()) return rows;
    // Best match first; what only the text filter found keeps its place after the ranked ones.
    const at = (m: Memory) => rank.get(m.id) ?? rank.size + rows.indexOf(m);
    return [...rows].sort((a, b) => at(a) - at(b));
  });

  /** Group rows by topic (all topics view) or by section (single topic view). */
  const groups = computed(() => {
    const out: { key: string; title: string; items: Memory[] }[] = [];
    for (const m of visible.value) {
      const key = topic.value ? m.section ?? "" : m.topic;
      let g = out.find((x) => x.key === key);
      if (!g) {
        g = { key, title: topic.value ? m.section ?? "" : topicTitle(m.topic), items: [] };
        out.push(g);
      }
      g.items.push(m);
    }
    return out;
  });

  const filtered = computed(() => !!(kind.value || source.value || query.value.trim() || status.value !== "active"));

  function clearFilters() {
    kind.value = null;
    source.value = null;
    query.value = "";
    ranked.value = null;
    status.value = "active";
  }

  return { kind, source, status, query, archived, counts, reviewDue, topicPending, visible, groups, filtered, clearFilters };
}
