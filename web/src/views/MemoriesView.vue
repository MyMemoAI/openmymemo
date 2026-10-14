<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { api } from "../api";
import AppIcon from "../components/AppIcon.vue";
import DropMenu from "../components/DropMenu.vue";
import HistoryDialog from "../components/HistoryDialog.vue";
import MemoryRow from "../components/MemoryRow.vue";
import NewTopicDialog from "../components/NewTopicDialog.vue";
import PageHeader from "../components/PageHeader.vue";
import ProposalCard from "../components/ProposalCard.vue";
import SortDialog from "../components/SortDialog.vue";
import type { Memory } from "../api";
import { t } from "../i18n";
import { banner, srcLabel, useMemoryList } from "../memoryList";
import { act, freshMemories, freshTopics, pending, store, topicsBySlug, topicTitle, waitingTopics } from "../store";

const props = defineProps<{ initialTopic: string | null }>();

const topic = ref<string | null>(props.initialTopic);
const { kind, source, status, query, counts, reviewDue, topicPending, visible, groups, filtered, clearFilters } =
  useMemoryList(topic);
const historyFor = ref<Memory | null>(null);
const draft = ref("");
const draftTopic = ref("");
const newTopic = ref(false);
const sorting = ref(false);

watch(() => props.initialTopic, (v) => (topic.value = v));
watch(topic, (v) => {
  history.replaceState(null, "", v ? `#/memories/${encodeURIComponent(v)}` : "#/memories");
  draftTopic.value = v ?? "";
});

const current = computed(() => (topic.value ? topicsBySlug.value[topic.value] : null));

/** Through the URL, so the app's route follows and the sidebar's "Memories" link returns to All. */
function selectTopic(slug: string | null) {
  status.value = "active";
  const hash = slug ? `#/memories/${encodeURIComponent(slug)}` : "#/memories";
  if (location.hash === hash) topic.value = slug;
  else location.hash = hash;
}

async function add() {
  const content = draft.value.trim();
  if (!content) return;
  const ok = await act(() => api.addMemory({ topic: topic.value ?? (draftTopic.value || undefined), content }));
  if (ok) draft.value = "";
}

function onDraftKey(e: KeyboardEvent) {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
    e.preventDefault();
    add();
  }
}

function toggleTopicPrivate() {
  const tp = current.value;
  if (tp) act(() => api.patchTopic(tp.slug, { private: !tp.private }));
}

/** Sidebar row classes: the selected topic sits on a white card, a selected filter on a grey fill. */
const topicRow = (on: boolean, empty = false) => [
  "flex min-h-[30px] w-full items-center justify-between gap-2 rounded-lg px-2.5 text-left text-sm transition-colors",
  on ? "bg-surface font-semibold shadow-[0_1px_2px_rgb(0_0_0/0.06),0_0_0_1px_var(--color-ring)]" : "hover:bg-fill",
  empty && !on ? "text-muted" : "",
];
const filterRow = (on: boolean) => [
  "flex min-h-[30px] w-full items-center justify-between gap-2 rounded-lg px-2.5 text-left text-sm transition-colors",
  on ? "bg-fill-strong font-semibold" : "hover:bg-fill",
];

const filePath = computed(() => (current.value ? `${store.state?.vault}/memories/${current.value.slug}.md` : ""));
</script>

<template>
  <div class="mx-auto max-w-[820px] space-y-5">
    <!-- the sidebar's lower half: topics, then filters -->
    <Teleport to="#sidebar-extra" defer>
      <nav :aria-label="t('mem.topics')" class="flex flex-col gap-4 border-t border-sep pt-4">
        <button :class="topicRow(!topic)" :aria-current="!topic ? 'true' : undefined" @click="selectTopic(null)">
          <span>{{ t("common.all") }}</span><span class="text-[13px] font-normal text-muted tabular-nums">{{ store.state?.counts.active }}</span>
        </button>
        <div v-for="g in store.state?.groups" :key="g.id" class="flex flex-col gap-px">
          <div class="label px-2.5 pb-1">{{ g.name }}</div>
          <button
            v-for="slug in g.topics"
            :key="slug"
            :class="topicRow(topic === slug, !topicsBySlug[slug]?.count)"
            :aria-current="topic === slug ? 'true' : undefined"
            @click="selectTopic(slug)"
          >
            <span class="flex min-w-0 items-center gap-1.5">
              <AppIcon v-if="topicsBySlug[slug]?.private" name="lock" :size="13" class="text-muted" />
              <span class="truncate">{{ topicsBySlug[slug]?.title }}</span>
            </span>
            <span class="flex items-center gap-1.5 text-[13px] font-normal text-muted tabular-nums">
              <span v-if="waitingTopics.has(slug)" class="dot bg-wait" />
              <span v-if="freshTopics.has(slug)" class="dot bg-fresh" />
              {{ topicsBySlug[slug]?.count || "" }}
            </span>
          </button>
        </div>
        <button class="btn btn-link btn-sm justify-start px-2.5" @click="newTopic = true">
          <AppIcon name="plus" :size="14" />{{ t("mem.newTopic") }}
        </button>
      </nav>

      <div class="flex flex-col gap-4 border-t border-sep pt-4">
        <div v-if="store.state?.kinds.some((x) => counts.kinds[x])" class="flex flex-col gap-px">
          <div class="label px-2.5 pb-1">{{ t("mem.kinds") }}</div>
          <button
            v-for="k in store.state?.kinds.filter((x) => counts.kinds[x])"
            :key="k"
            :class="filterRow(kind === k)"
            :aria-pressed="kind === k"
            @click="kind = kind === k ? null : k"
          >
            <span>{{ t(`kind.${k}`) }}</span><span class="text-[13px] font-normal text-muted tabular-nums">{{ counts.kinds[k] }}</span>
          </button>
        </div>
        <div v-if="Object.keys(counts.sources).length" class="flex flex-col gap-px">
          <div class="label px-2.5 pb-1">{{ t("mem.sources") }}</div>
          <button
            v-for="(n, s) in counts.sources"
            :key="s"
            :class="filterRow(source === s)"
            :aria-pressed="source === s"
            @click="source = source === s ? null : String(s)"
          >
            <span class="truncate">{{ srcLabel(String(s)) }}</span><span class="text-[13px] font-normal text-muted tabular-nums">{{ n }}</span>
          </button>
        </div>
        <div class="flex flex-col gap-px">
          <div class="label px-2.5 pb-1">{{ t("mem.status") }}</div>
          <a href="#/changes" :class="filterRow(false)">
            <span>{{ t("mem.unreviewed") }}</span>
            <span class="text-[13px] tabular-nums" :class="store.unreviewed.length ? 'font-semibold text-fresh-ink' : 'text-muted'">{{ store.unreviewed.length }}</span>
          </a>
          <a href="#/changes" :class="filterRow(false)">
            <span>{{ t("mem.waiting") }}</span>
            <span class="text-[13px] tabular-nums" :class="pending.length ? 'font-semibold text-wait-ink' : 'text-muted'">{{ pending.length }}</span>
          </a>
          <button
            v-for="s in (['review', 'archived'] as const)"
            :key="s"
            :class="filterRow(status === s)"
            :aria-pressed="status === s"
            @click="status = status === s ? 'active' : s"
          >
            <span>{{ s === "review" ? t("mem.reviewDue") : t("mem.archived") }}</span>
            <span class="text-[13px] font-normal text-muted tabular-nums">{{ s === "review" ? reviewDue.length : store.state?.counts.archived }}</span>
          </button>
        </div>
      </div>
    </Teleport>

    <PageHeader
      :title="current ? current.title : status === 'archived' ? t('mem.archived') : t('nav.memories')"
      :subtitle="current?.description"
    >
      <button v-if="current?.group === 'imported'" class="btn btn-primary" @click="sorting = true">{{ t("mem.sort") }}</button>
      <label class="relative flex items-center">
        <AppIcon name="search" :size="15" class="pointer-events-none absolute left-2.5 text-muted" />
        <input v-model="query" class="input w-44 pl-8 sm:w-56" :placeholder="t('mem.search')" :aria-label="t('mem.search')" type="search" />
      </label>
      <DropMenu v-if="current" :label="t('mem.topicOptions')" button-class="btn btn-filled btn-icon text-ink">
        <button class="menu-item" role="menuitem" @click="toggleTopicPrivate">
          <AppIcon name="lock" class="text-muted" />{{ current.private ? t("mem.makeTopicPublic") : t("mem.makeTopicPrivate") }}
        </button>
      </DropMenu>
    </PageHeader>

    <!-- phone: topics as a scrollable chip row -->
    <div class="-mx-4 flex gap-2 overflow-x-auto px-4 pb-1 sm:-mx-6 sm:px-6 md:hidden">
      <button
        v-for="opt in [{ slug: null, title: t('common.all'), count: store.state?.counts.active }, ...(store.state?.topics ?? [])]"
        :key="opt.slug ?? 'all'"
        class="inline-flex min-h-9 shrink-0 items-center gap-1.5 rounded-full px-3.5 text-sm font-medium"
        :class="topic === opt.slug
          ? 'bg-ink text-ground'
          : opt.count ? 'bg-surface shadow-[0_0_0_1px_var(--color-line)]' : 'bg-surface text-muted shadow-[0_0_0_1px_var(--color-sep)]'"
        @click="selectTopic(opt.slug)"
      >
        {{ opt.title }}<span v-if="opt.count" class="opacity-60 tabular-nums">{{ opt.count }}</span>
        <span v-if="opt.slug && freshTopics.has(opt.slug)" class="dot bg-fresh" />
        <span v-if="opt.slug && waitingTopics.has(opt.slug)" class="dot bg-wait" />
      </button>
    </div>

    <div v-if="current?.private" class="notice notice-private">
      <AppIcon name="lock" />{{ t("mem.privateTopic") }}
    </div>

    <!-- add -->
    <form v-if="status === 'active'" class="card flex flex-wrap items-start gap-2 p-2 pl-4 sm:flex-nowrap" @submit.prevent="add">
      <textarea
        v-model="draft"
        rows="1"
        class="min-h-9 w-full resize-none bg-transparent py-1.5 text-[15px] outline-none [field-sizing:content] placeholder:truncate placeholder:text-faint sm:w-auto sm:flex-1"
        :placeholder="t('mem.newPlaceholder')"
        :aria-label="t('mem.newPlaceholder')"
        @keydown="onDraftKey"
      />
      <select v-if="!topic" v-model="draftTopic" class="input select min-w-0 flex-1 sm:w-36 sm:flex-none" :aria-label="t('mem.topics')">
        <option value="">{{ t("mem.autoTopic") }}</option>
        <option v-for="tp in store.state?.topics" :key="tp.slug" :value="tp.slug">{{ tp.title }}</option>
      </select>
      <button type="submit" class="btn btn-primary shrink-0" :disabled="!draft.trim()">{{ t("mem.add") }}</button>
    </form>

    <!-- AI changes: a banner on the overview; inside a topic, the cards that wait for a decision -->
    <a
      v-if="status === 'active' && !topic && banner"
      href="#/changes"
      class="notice notice-wait justify-between gap-3 py-3 text-sm text-ink transition hover:brightness-[0.98]"
    >
      <span>{{ banner }}</span>
      <span class="flex shrink-0 items-center gap-1 font-semibold text-wait-ink">{{ t("mem.reviewNow") }}<AppIcon name="chevronRight" :size="14" /></span>
    </a>
    <div v-if="status === 'active' && topic && topicPending.length" class="space-y-2.5">
      <ProposalCard v-for="p in topicPending.slice(0, 5)" :key="p.id" :proposal="p" compact />
      <a v-if="topicPending.length > 5" href="#/changes" class="inline-flex items-center gap-1 text-sm font-medium text-accent">
        +{{ topicPending.length - 5 }} {{ t("mem.waiting") }}<AppIcon name="chevronRight" :size="14" />
      </a>
    </div>

    <!-- list -->
    <div v-if="visible.length" class="space-y-4">
      <section v-for="g in groups" :key="g.key" class="card">
        <div v-if="g.title" class="flex items-center justify-between px-4.5 pt-3 pb-1 text-[13px] font-semibold text-muted">
          <button v-if="!topic" class="hover:text-ink" @click="selectTopic(g.key)">{{ g.title }}</button>
          <span v-else>{{ g.title }}</span>
          <span class="font-normal tabular-nums">{{ t("mem.count", { n: g.items.length }) }}</span>
        </div>
        <!-- no overflow-hidden on the card, so row menus can extend past it; round the rows' hover instead -->
        <div class="divide-y divide-sep [&>*:last-child]:rounded-b-[14px]" :class="g.title ? '' : '[&>*:first-child]:rounded-t-[14px]'">
          <MemoryRow
            v-for="m in g.items"
            :key="m.id"
            :memory="m"
            :show-topic="status !== 'active' && !topic ? topicTitle(m.topic) : undefined"
            :fresh-from="m.status === 'active' ? freshMemories[m.id] : undefined"
            @history="historyFor = $event"
          />
        </div>
      </section>
    </div>

    <div v-else-if="filtered" class="py-16 text-center text-sm text-muted">
      {{ t("mem.noMatch") }} <button class="btn btn-link btn-sm" @click="clearFilters">{{ t("common.all") }}</button>
    </div>
    <div v-else-if="current" class="card px-6 py-12 text-center">
      <p class="font-semibold">{{ t("mem.emptyTopic") }}</p>
      <p class="mt-1 text-sm text-muted">{{ t("mem.emptyTopicHint") }}</p>
    </div>
    <div v-else class="card px-6 py-16 text-center">
      <p class="text-lg font-semibold">{{ t("mem.emptyAll") }}</p>
      <p class="mx-auto mt-1 max-w-md text-sm text-muted">{{ t("mem.emptyAllHint") }}</p>
      <div class="mt-5 flex flex-wrap justify-center gap-2">
        <a href="#/import" class="btn btn-primary">{{ t("mem.importCta") }}</a>
        <a href="#/welcome" class="btn">{{ t("onb.showGuide") }}</a>
      </div>
    </div>

    <p v-if="current" class="text-[13px] text-muted">
      {{ t("mem.otherFile") }}<span class="font-mono text-xs break-all">{{ filePath }}</span>
    </p>

    <HistoryDialog v-if="historyFor" :memory="historyFor" @close="historyFor = null" />
    <SortDialog v-if="sorting && current" :topic="current.slug" @close="sorting = false" />

    <NewTopicDialog v-if="newTopic" @close="newTopic = false" @created="newTopic = false; selectTopic($event)" />
  </div>
</template>
