<script setup lang="ts">
import { computed, onMounted, ref, watch } from "vue";
import { api, type Memory, type Proposal } from "../api";
import AppIcon from "../components/AppIcon.vue";
import ApprovalSwitch from "../components/ApprovalSwitch.vue";
import DigestRow from "../components/DigestRow.vue";
import HistoryDialog from "../components/HistoryDialog.vue";
import PageHeader from "../components/PageHeader.vue";
import ProposalCard from "../components/ProposalCard.vue";
import TidyDialog from "../components/TidyDialog.vue";
import { dayKey, dayLabel, t, tn } from "../i18n";
import { act, pending, store, toast } from "../store";

const props = defineProps<{ initialTab: string | null }>();

type Tab = "review" | "history";
const tab = ref<Tab>(props.initialTab === "history" ? "history" : "review");
watch(() => props.initialTab, (v) => (tab.value = v === "history" ? "history" : "review"));
watch(tab, (v) => history.replaceState(null, "", v === "history" ? "#/changes/history" : "#/changes"));
/** Through the URL, so the sidebar's "Changes" link returns to To review. */
function pick(v: Tab) {
  const hash = v === "history" ? "#/changes/history" : "#/changes";
  if (location.hash === hash) tab.value = v;
  else location.hash = hash;
}

const historyFor = ref<Memory | null>(null);

// Tidy up (§17.6): the button shows once the Assistant is on.
const tidyShown = ref(false);
const tidyOpen = ref(false);
onMounted(async () => {
  try {
    tidyShown.value = (await api.tidy()).enabled;
  } catch {
    tidyShown.value = false;
  }
});

/** "Tidy-up, 2026-10-25: 6 changes" for a batch id "tidy-2026-10-25-xxxx". */
function batchTitle(batch: string, n: number): string {
  return tn("chg.batch", n, { date: batch.startsWith("tidy-") ? batch.slice(5, 15) : batch });
}

/** The digest grouped by local day (newest first), and within a day by app, with each batch (a tidy-up) as a group
 * of its own that can be kept or undone as a whole. */
type Group = { id: string; name: string; batch: string | null; items: Proposal[] };
const days = computed(() => {
  const out: { key: string; label: string; apps: Group[] }[] = [];
  for (const p of store.unreviewed) {
    const key = dayKey(p.decided_at ?? p.created_at);
    let day = out.find((d) => d.key === key);
    if (!day) {
      day = { key, label: dayLabel(key), apps: [] };
      out.push(day);
    }
    const id = p.batch_id ? `batch:${p.batch_id}` : p.connector_id;
    let app = day.apps.find((a) => a.id === id);
    if (!app) {
      app = { id, name: p.connector_name, batch: p.batch_id ?? null, items: [] };
      day.apps.push(app);
    }
    app.items.push(p);
  }
  return out;
});

async function looksGood() {
  const res = await act(() => api.reviewedAll());
  if (res) toast(tn("chg.lookedGood", res.reviewed));
}

async function keepAll(batch: string) {
  const res = await act(() => api.keepBatch(batch));
  if (res) toast(tn("chg.keptAll", res.reviewed));
}

async function undoAll(batch: string) {
  const res = await act(() => api.undoBatch(batch));
  if (res) toast(res.kept ? t("chg.undidSome", res) : tn("chg.undidAll", res.undone));
}

async function acceptAll() {
  for (const p of [...pending.value]) await act(() => api.accept(p.id));
}

// History: every decided proposal, i.e. neither waiting nor still in the digest. Loaded on demand.
const decided = ref<Proposal[] | null>(null);
async function loadHistory() {
  try {
    const rows = await api.proposals({ limit: 300 });
    decided.value = rows.filter((p) => p.status !== "pending" && !(p.status === "applied" && !p.reviewed_at));
  } catch {
    decided.value = decided.value ?? [];
  }
}
watch([tab, () => store.version], () => tab.value === "history" && loadHistory(), { immediate: true });
</script>

<template>
  <div class="mx-auto max-w-[820px] space-y-5">
    <PageHeader :title="t('nav.changes')" :subtitle="t('chg.subtitle')" />
    <ApprovalSwitch />
    <div v-if="tidyShown" class="flex justify-end">
      <button class="btn btn-sm" data-test="tidy-open" @click="tidyOpen = true"><AppIcon name="refresh" :size="14" />{{ t("tidy.button") }}</button>
    </div>

    <div class="seg" role="tablist" :aria-label="t('nav.changes')">
      <button class="seg-item" :class="{ 'is-on': tab === 'review' }" role="tab" :aria-selected="tab === 'review'" @click="pick('review')">
        {{ t("chg.tab.review") }}
        <span v-if="pending.length + store.unreviewed.length" class="font-medium text-muted tabular-nums">{{ pending.length + store.unreviewed.length }}</span>
      </button>
      <button class="seg-item" :class="{ 'is-on': tab === 'history' }" role="tab" :aria-selected="tab === 'history'" @click="pick('history')">
        {{ t("chg.tab.history") }}
      </button>
    </div>

    <div v-if="tab === 'review'" class="space-y-8">
      <div v-if="!pending.length && !store.unreviewed.length" class="card flex flex-col items-center gap-1.5 px-6 py-14 text-center">
        <AppIcon name="changes" :size="34" class="text-fresh" />
        <p class="mt-1 font-semibold">{{ t("chg.caughtUp") }}</p>
        <p class="max-w-md text-[13px] text-muted">{{ t("chg.caughtUpHint") }}</p>
      </div>

      <section v-if="pending.length" class="space-y-3">
        <div class="flex items-center justify-between gap-2">
          <h2 class="section-title">{{ t("chg.waiting") }} <span class="font-normal text-muted tabular-nums">{{ pending.length }}</span></h2>
          <button v-if="pending.length > 1" class="btn btn-sm" @click="acceptAll">{{ t("chg.acceptAll") }}</button>
        </div>
        <ProposalCard v-for="p in pending" :key="p.id" :proposal="p" />
      </section>

      <section v-if="store.unreviewed.length" class="space-y-3">
        <div class="flex items-center justify-between gap-2">
          <h2 class="section-title">{{ t("chg.changed") }} <span class="font-normal text-muted tabular-nums">{{ store.unreviewed.length }}</span></h2>
          <button class="btn btn-primary btn-sm" @click="looksGood"><AppIcon name="check" :size="14" />{{ t("chg.looksGood") }}</button>
        </div>
        <div v-for="day in days" :key="day.key" class="space-y-2">
          <div class="label px-1 pt-1">{{ day.label }}</div>
          <div v-for="app in day.apps" :key="app.id" class="card overflow-hidden">
            <div class="flex items-center justify-between gap-2 border-b border-sep px-4.5 py-2.5">
              <span class="flex min-w-0 items-center gap-2 text-sm font-semibold">
                <span class="avatar avatar-on size-[22px] shrink-0 rounded-md text-[11px]" aria-hidden="true">{{ app.name.slice(0, 1) }}</span>
                <span v-if="app.batch" class="min-w-0" data-test="batch-title">{{ batchTitle(app.batch, app.items.length) }}<span class="font-normal text-muted"> · {{ app.name }}</span></span>
                <template v-else>{{ app.name }}</template>
              </span>
              <span v-if="app.batch" class="flex shrink-0 gap-1.5">
                <button class="btn btn-sm" data-test="keep-all" @click="keepAll(app.batch)">{{ t("chg.keepAll") }}</button>
                <button class="btn btn-sm" data-test="undo-all" @click="undoAll(app.batch)">{{ t("chg.undoAll") }}</button>
              </span>
              <span v-else class="text-xs text-muted">{{ tn("chg.count", app.items.length) }}</span>
            </div>
            <div class="divide-y divide-sep">
              <DigestRow v-for="p in app.items" :key="p.id" :proposal="p" @history="historyFor = $event" />
            </div>
          </div>
        </div>
      </section>
    </div>

    <template v-else>
      <div v-if="decided === null" class="py-16 text-center text-sm text-muted">{{ t("common.loading") }}</div>
      <div v-else-if="!decided.length" class="card px-6 py-14 text-center">
        <p class="font-semibold">{{ t("chg.historyEmpty") }}</p>
        <p class="mt-1 text-[13px] text-muted">{{ t("chg.historyHint") }}</p>
      </div>
      <div v-else class="space-y-3">
        <ProposalCard v-for="p in decided" :key="p.id" :proposal="p" />
      </div>
    </template>

    <HistoryDialog v-if="historyFor" :memory="historyFor" @close="historyFor = null" />
    <TidyDialog v-if="tidyOpen" @close="tidyOpen = false" />
  </div>
</template>
