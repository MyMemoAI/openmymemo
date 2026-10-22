<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { api, type Overview, type SafetyIssue, type SafetyScore } from "../api";
import AppIcon from "../components/AppIcon.vue";
import PageHeader from "../components/PageHeader.vue";
import { relTime, t, tn } from "../i18n";
import { act, store } from "../store";

/**
 * Home (product-design.md §18.1): the safety score, what waits, the memory at a glance, the connected apps and the
 * last 7 days. It summarises and links; the work happens on the other pages. No memory text is shown here.
 */
const data = ref<Overview | null>(null);
const checks = ref<SafetyScore | null>(null);
const showChecks = ref(false);

async function load() {
  try {
    data.value = await api.overview();
    if (showChecks.value) checks.value = await api.score();
  } catch {
    // the panel's offline notice says it; keep what we had
  }
}
// The overview is heavier than /api/state, so reload it when something it counts changes, not on every poll.
const counted = computed(() => {
  const c = store.state?.counts;
  return c ? [c.active, c.archived, c.pending, c.unreviewed, c.review_due, store.connectors.length].join() : "";
});
watch(counted, load, { immediate: true });

async function toggleChecks() {
  showChecks.value = !showChecks.value;
  if (showChecks.value) checks.value = await api.score();
}

async function dismiss(issue: SafetyIssue, restore = false) {
  const result = await act(() => api.dismissIssue(issue.id, restore));
  if (result) {
    checks.value = result;
    await load();
  }
}

function fixHref(issue: SafetyIssue): string {
  switch (issue.fix) {
    case "memory": {
      const topic = store.memories.find((m) => m.id === issue.target)?.topic;
      return topic ? `#/memories/${encodeURIComponent(topic)}` : "#/memories";
    }
    case "app": return issue.target ? `#/connectors/${encodeURIComponent(issue.target)}` : "#/connectors";
    case "changes": return "#/changes";
    case "export": return "#/import";
    default: return "#/memories";
  }
}

const waitingFirst = computed(() => !!data.value && data.value.waiting.pending > 0);
const anyWaiting = computed(() => {
  const w = data.value?.waiting;
  return !!w && w.pending + w.unreviewed + w.review_due > 0;
});

const weekText = computed(() => {
  const n = data.value?.safety.week_change;
  if (n === null || n === undefined) return "";
  return n > 0 ? t("home.weekUp", { n: `+${n}` }) : n < 0 ? t("home.weekDown", { n: `−${-n}` }) : t("home.weekSame");
});

// ---- the score ring ----------------------------------------------------------------------------------------
const R = 34;
const CIRC = 2 * Math.PI * R;
const ringColor = computed(() => {
  const s = data.value?.safety.score ?? 0;
  return s >= 70 ? "var(--color-fresh)" : s >= 50 ? "var(--color-wait)" : "var(--color-danger)";
});

// ---- the memory card ---------------------------------------------------------------------------------------
const W = 280;
const H = 56;
const growthPath = computed(() => {
  const g = data.value?.memories.growth ?? [];
  if (g.length < 2) return { line: "", area: "", end: null as null | { x: number; y: number } };
  const max = Math.max(1, ...g.map((p) => p.total));
  const pts = g.map((p, i) => ({ x: (i / (g.length - 1)) * W, y: H - 4 - (p.total / max) * (H - 10) }));
  const line = pts.map((p, i) => `${i ? "L" : "M"}${p.x.toFixed(1)} ${p.y.toFixed(1)}`).join(" ");
  return { line, area: `${line} L${W} ${H} L0 ${H} Z`, end: pts[pts.length - 1] };
});
const sources = computed(() => {
  const s = data.value?.memories.sources ?? {};
  const total = (s.you ?? 0) + (s.apps ?? 0) + (s.imports ?? 0);
  return (["you", "apps", "imports"] as const)
    .map((k) => ({ key: k, n: s[k] ?? 0, share: total ? ((s[k] ?? 0) / total) * 100 : 0 }))
    .filter((x) => x.n > 0);
});
const groupMax = computed(() => Math.max(1, ...(data.value?.memories.groups ?? []).map((g) => g.count)));

// ---- the last 7 days ---------------------------------------------------------------------------------------
const BAR_H = 96;
const weekMax = computed(() => Math.max(1, ...(data.value?.activity ?? []).map((d) => d.reads + d.writes)));
const weekBars = computed(() =>
  (data.value?.activity ?? []).map((d) => {
    const [y, m, day] = d.date.split("-").map(Number);
    const label = new Date(y, m - 1, day).toLocaleDateString("en", { weekday: "short" });
    const scale = (n: number) => (n / weekMax.value) * (BAR_H - 4);
    return { ...d, label, reads_h: scale(d.reads), writes_h: scale(d.writes),
             tip: t("home.dayTip", { day: label, reads: d.reads, writes: d.writes }) };
  }),
);
const weekTotals = computed(() => ({
  reads: (data.value?.activity ?? []).reduce((n, d) => n + d.reads, 0),
  writes: (data.value?.activity ?? []).reduce((n, d) => n + d.writes, 0),
}));

function access(a: Overview["apps"][number]): string[] {
  return [
    ...(a.include_private ? [t("home.access.private")] : []),
    ...(a.write === "auto" ? [t("home.access.auto")] : a.write === "none" ? [t("home.access.none")] : []),
  ];
}
</script>

<template>
  <div class="mx-auto max-w-[880px] space-y-5">
    <PageHeader :title="t('nav.home')" :subtitle="t('home.subtitle')" />

    <div v-if="!data" class="py-16 text-center text-muted">{{ t("common.loading") }}</div>
    <div v-else class="grid gap-5 md:grid-cols-2">
      <!-- something waits: first, across the page -->
      <section v-if="anyWaiting" class="card space-y-2.5 p-5 md:col-span-2" :class="waitingFirst ? 'order-first' : 'order-2'"
               data-test="waiting">
        <h2 class="text-[15px] font-semibold">{{ t("home.waiting") }}</h2>
        <ul class="space-y-1.5 text-sm">
          <li v-if="data.waiting.pending">
            <a href="#/changes" class="flex items-center gap-2 hover:underline">
              <span class="size-2 rounded-full bg-wait" />{{ tn("home.pending", data.waiting.pending) }}
            </a>
          </li>
          <li v-if="data.waiting.unreviewed">
            <a href="#/changes" class="flex items-center gap-2 hover:underline">
              <span class="size-2 rounded-full bg-fresh" />{{ tn("home.unreviewed", data.waiting.unreviewed) }}
            </a>
          </li>
          <li v-if="data.waiting.review_due">
            <a href="#/memories" class="flex items-center gap-2 hover:underline">
              <span class="size-2 rounded-full bg-accent" />{{ tn("home.reviewDue", data.waiting.review_due) }}
            </a>
          </li>
        </ul>
      </section>

      <!-- the safety score -->
      <section class="card order-1 space-y-4 p-5 md:col-span-2" data-test="safety">
        <div class="flex flex-wrap items-center gap-5">
          <svg :width="84" :height="84" viewBox="0 0 84 84" role="img"
               :aria-label="`${t('home.safety')}: ${data.safety.score} · ${data.safety.level}`">
            <circle cx="42" cy="42" :r="R" fill="none" stroke="var(--color-fill)" stroke-width="8" />
            <circle cx="42" cy="42" :r="R" fill="none" :stroke="ringColor" stroke-width="8" stroke-linecap="round"
                    :stroke-dasharray="`${(data.safety.score / 100) * CIRC} ${CIRC}`" transform="rotate(-90 42 42)" />
            <text x="42" y="48" text-anchor="middle" class="fill-ink text-[22px] font-semibold tabular-nums">
              {{ data.safety.score }}
            </text>
          </svg>
          <div class="min-w-[12rem] flex-1 space-y-0.5">
            <h2 class="text-[15px] font-semibold">{{ t("home.safety") }} · {{ data.safety.level }}</h2>
            <p v-if="weekText" class="text-[13px] text-muted">{{ weekText }}</p>
            <p class="text-[13px] text-muted">{{ t("home.safetyHelp") }}</p>
          </div>
          <button class="btn" @click="toggleChecks">{{ showChecks ? t("home.hideChecks") : t("home.allChecks") }}</button>
        </div>

        <p v-if="!data.safety.top.length && !showChecks" class="text-sm text-muted">{{ t("home.allClear") }}</p>
        <ul v-else-if="!showChecks" class="divide-y divide-sep">
          <li v-for="issue in data.safety.top" :key="issue.id" class="flex flex-wrap items-center gap-x-3 gap-y-1.5 py-2.5 text-sm">
            <span class="w-9 shrink-0 text-[13px] font-medium text-muted tabular-nums">{{ t("home.points", { n: issue.points }) }}</span>
            <span class="min-w-[14rem] flex-1">{{ issue.message }}</span>
            <a :href="fixHref(issue)" class="btn btn-sm">{{ t(`home.fix.${issue.fix}`) }}</a>
          </li>
        </ul>
        <div v-else-if="checks" class="space-y-3" data-test="checks">
          <div v-for="a in checks.areas" :key="a.id" class="space-y-1">
            <div class="flex items-baseline justify-between text-sm">
              <span class="font-medium">{{ a.name }}</span>
              <span class="text-muted tabular-nums">{{ a.points }} / {{ a.max }}</span>
            </div>
            <ul class="divide-y divide-sep">
              <li v-for="issue in checks.issues.filter((i) => i.area === a.id)" :key="issue.id"
                  class="flex flex-wrap items-center gap-x-3 gap-y-1.5 py-2 text-sm" :class="issue.dismissed ? 'text-muted' : ''">
                <span class="w-9 shrink-0 text-[13px] font-medium tabular-nums">{{ issue.dismissed ? "" : t("home.points", { n: issue.points }) }}</span>
                <span class="min-w-[14rem] flex-1">{{ issue.message }}<template v-if="issue.dismissed"> · {{ t("home.dismissed") }}</template></span>
                <template v-if="issue.dismissed">
                  <button class="btn btn-sm" @click="dismiss(issue, true)">{{ t("home.restore") }}</button>
                </template>
                <template v-else>
                  <a :href="fixHref(issue)" class="btn btn-sm">{{ t(`home.fix.${issue.fix}`) }}</a>
                  <button class="btn btn-sm" @click="dismiss(issue)">
                    {{ issue.id === "backup" ? t("home.notMine") : t("home.dismiss") }}
                  </button>
                </template>
              </li>
            </ul>
          </div>
        </div>
      </section>

      <!-- a new folder: the first steps instead of zeros -->
      <section v-if="data.first_run" class="card order-3 space-y-3 p-5 md:col-span-2" data-test="start">
        <h2 class="text-[15px] font-semibold">{{ t("home.start") }}</h2>
        <a v-for="step in [['connect', '#/connectors', 'plug'], ['import', '#/import', 'download'], ['private', '#/memories', 'lock']] as const"
           :key="step[0]" :href="step[1]" class="flex items-start gap-3 rounded-[10px] p-2 hover:bg-hover">
          <AppIcon :name="step[2]" :size="18" class="mt-0.5 text-accent" />
          <span class="space-y-0.5">
            <span class="block text-sm font-medium">{{ t(`home.start.${step[0]}`) }}</span>
            <span class="block text-[13px] text-muted">{{ t(`home.start.${step[0]}Help`) }}</span>
          </span>
        </a>
      </section>

      <template v-else>
        <!-- your memory -->
        <section class="card order-3 space-y-4 p-5" data-test="memory">
          <div class="flex items-baseline justify-between gap-3">
            <h2 class="text-[15px] font-semibold">{{ t("home.memory") }}</h2>
            <a href="#/memories" class="text-sm text-muted hover:underline">{{ tn("home.total", data.memories.total) }}</a>
          </div>
          <figure class="space-y-1">
            <svg :viewBox="`0 0 ${W} ${H}`" class="h-14 w-full" preserveAspectRatio="none" role="img" :aria-label="t('home.growth')">
              <title>{{ t("home.growth") }}</title>
              <path :d="growthPath.area" fill="var(--color-accent)" fill-opacity="0.1" />
              <path :d="growthPath.line" fill="none" stroke="var(--color-accent)" stroke-width="2" stroke-linejoin="round"
                    stroke-linecap="round" vector-effect="non-scaling-stroke" />
            </svg>
            <figcaption class="text-[12px] text-muted">{{ t("home.growth") }}</figcaption>
          </figure>
          <ul class="space-y-1.5 text-sm">
            <li v-for="g in data.memories.groups" :key="g.id" class="flex items-center gap-3">
              <span class="w-32 shrink-0 truncate">{{ g.name }}</span>
              <span class="h-1.5 flex-1 rounded-full bg-fill">
                <span class="block h-full rounded-full bg-accent" :style="{ width: `${(g.count / groupMax) * 100}%` }" />
              </span>
              <span class="w-8 text-right text-muted tabular-nums">{{ g.count }}</span>
            </li>
          </ul>
          <p class="text-[13px] text-muted">
            <span v-for="(s, i) in sources" :key="s.key">{{ i ? " · " : "" }}{{ t(`home.source.${s.key}`) }} {{ Math.round(s.share) }}%</span>
            <template v-if="data.memories.private">{{ sources.length ? " · " : "" }}{{ t("home.private", { n: data.memories.private }) }}</template>
          </p>
        </section>

        <!-- connected apps -->
        <section class="card order-3 space-y-3 p-5" data-test="apps">
          <h2 class="text-[15px] font-semibold">{{ t("home.apps") }}</h2>
          <p v-if="!data.apps.length" class="text-sm text-muted">
            {{ t("home.noApps") }} <a href="#/connectors" class="text-accent hover:underline">{{ t("home.start.connect") }}</a>
          </p>
          <ul v-else class="divide-y divide-sep">
            <li v-for="a in data.apps" :key="a.id">
              <a :href="`#/connectors/${encodeURIComponent(a.id)}`" class="block space-y-0.5 py-2.5 hover:bg-hover">
                <span class="flex items-baseline justify-between gap-3 text-sm">
                  <span class="font-medium">{{ a.name }}</span>
                  <span class="text-[13px] text-muted">{{ t("home.lastUsed", { when: relTime(a.last_seen) }) }}</span>
                </span>
                <span class="block text-[13px] text-muted">
                  {{ t("home.appUse", { reads: tn("home.nReads", a.reads), writes: tn("home.nWrites", a.writes) }) }}
                  <template v-for="note in access(a)" :key="note"> · <span class="text-wait-ink">{{ note }}</span></template>
                </span>
              </a>
            </li>
          </ul>
        </section>

        <!-- the last 7 days -->
        <section class="card order-3 space-y-3 p-5 md:col-span-2" data-test="week">
          <div class="flex flex-wrap items-baseline justify-between gap-3">
            <h2 class="text-[15px] font-semibold">{{ t("home.week") }}</h2>
            <span class="flex items-center gap-4 text-[13px] text-muted">
              <span class="flex items-center gap-1.5"><span class="size-2.5 rounded-sm bg-accent" />{{ t("home.reads") }} {{ weekTotals.reads }}</span>
              <span class="flex items-center gap-1.5"><span class="size-2.5 rounded-sm bg-wait" />{{ t("home.writes") }} {{ weekTotals.writes }}</span>
            </span>
          </div>
          <div class="grid grid-cols-7 items-end gap-2" :style="{ height: `${BAR_H + 20}px` }">
            <div v-for="d in weekBars" :key="d.date" class="flex h-full flex-col items-center justify-end gap-1" :title="d.tip">
              <div class="flex w-full max-w-6 flex-col justify-end gap-0.5">
                <span v-if="d.writes" class="block w-full rounded-t-[4px] bg-wait" :style="{ height: `${d.writes_h}px` }" />
                <span v-if="d.reads" class="block w-full bg-accent" :class="d.writes ? '' : 'rounded-t-[4px]'"
                      :style="{ height: `${d.reads_h}px` }" />
              </div>
              <span class="text-[11px] text-muted">{{ d.label }}</span>
            </div>
          </div>
        </section>
      </template>
    </div>
  </div>
</template>
