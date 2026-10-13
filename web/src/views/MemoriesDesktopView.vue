<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from "vue";
import { api, type Memory } from "../api";
import AppIcon from "../components/AppIcon.vue";
import DesktopToolbar from "../components/DesktopToolbar.vue";
import DropMenu from "../components/DropMenu.vue";
import MemoryInspector from "../components/MemoryInspector.vue";
import ProposalCard from "../components/ProposalCard.vue";
import SortDialog from "../components/SortDialog.vue";
import { ui } from "../desktop";
import { relTime, t, tn } from "../i18n";
import { banner, srcLabel, useMemoryList } from "../memoryList";
import { act, freshMemories, store, topicsBySlug, topicTitle } from "../store";

/** The Mac app's Memories screen: the list of a topic (or all of them) and an inspector for one memory. */
const props = defineProps<{ initialTopic: string | null }>();

const topic = ref<string | null>(props.initialTopic);
const { kind, source, status, query, counts, reviewDue, topicPending, visible, groups, filtered, clearFilters } =
  useMemoryList(topic);
const draft = ref("");
const sorting = ref(false);
const draftInput = ref<HTMLInputElement | null>(null);
const searchInput = ref<HTMLInputElement | null>(null);
const list = ref<HTMLElement | null>(null);

watch(() => props.initialTopic, (v) => {
  topic.value = v;
  status.value = "active";
});

const current = computed(() => (topic.value ? topicsBySlug.value[topic.value] : null));
const title = computed(() =>
  current.value ? current.value.title
  : status.value === "archived" ? t("mem.archived")
  : status.value === "review" ? t("mem.reviewDue")
  : t("mac.allMemories"),
);
const countLabel = computed(() =>
  tn("mac.count", visible.value.length) + (status.value === "active" && topicPending.value.length ? tn("mac.waits", topicPending.value.length) : ""),
);

// ---- selection: follows the list; when the selected memory leaves it, its neighbour takes over ----

const selectedId = ref<string | null>(null);
const selected = computed<Memory | null>(() => visible.value.find((m) => m.id === selectedId.value) ?? null);
const order = computed(() => groups.value.flatMap((g) => g.items.map((m) => m.id)));

watch(order, (ids, old) => {
  if (selectedId.value && ids.includes(selectedId.value)) return;
  const was = old && selectedId.value ? old.indexOf(selectedId.value) : -1;
  selectedId.value = ids[Math.max(0, Math.min(was, ids.length - 1))] ?? null;
}, { immediate: true });

async function focusRow(id: string) {
  await nextTick();
  list.value?.querySelector<HTMLElement>(`[data-id="${CSS.escape(id)}"]`)?.focus();
}

function onListKey(e: KeyboardEvent) {
  if (e.key !== "ArrowDown" && e.key !== "ArrowUp") return;
  if (!(e.target as HTMLElement).dataset?.id) return;
  e.preventDefault();
  const i = order.value.indexOf(selectedId.value ?? "");
  const next = order.value[e.key === "ArrowDown" ? Math.min(i + 1, order.value.length - 1) : Math.max(i - 1, 0)];
  if (next) {
    selectedId.value = next;
    focusRow(next);
  }
}

// ---- adding, and the shell's "new memory" / "find" commands ----

async function add() {
  const content = draft.value.trim();
  if (!content) return;
  const created = await act(() => api.addMemory({ topic: topic.value ?? undefined, content }));
  if (created) {
    draft.value = "";
    selectedId.value = created.id;
  }
}

async function take() {
  const cmd = ui.request;
  if (cmd !== "new-memory" && cmd !== "find") return;
  ui.request = null;
  if (cmd === "new-memory") {
    status.value = "active";
    await nextTick();
    draftInput.value?.focus();
  } else {
    searchInput.value?.focus();
    searchInput.value?.select();
  }
}
watch(() => ui.request, take);
onMounted(take);

function toggleTopicPrivate() {
  const tp = current.value;
  if (tp) act(() => api.patchTopic(tp.slug, { private: !tp.private }));
}

const chip = "chip min-h-[18px] px-[7px] text-[11px]";
</script>

<template>
  <div class="flex h-full min-h-0 flex-col">
    <DesktopToolbar :title="title" :subtitle="countLabel">
      <button v-if="current?.group === 'imported'" type="button" class="btn btn-primary btn-sm mr-1" @click="sorting = true">{{ t("mem.sort") }}</button>
      <button
        type="button"
        class="btn btn-ghost btn-icon text-ink-2"
        :aria-label="t('mac.newMemory')"
        :title="`${t('mac.newMemory')} ⌘N`"
        @click="ui.request = 'new-memory'"
      >
        <AppIcon name="plus" :size="18" class="stroke-[1.8]" />
      </button>
      <DropMenu
        :label="t('mac.filter')"
        icon="filter"
        width="w-60"
        :button-class="`btn btn-ghost btn-icon ${kind || source || status !== 'active' ? 'bg-accent-soft text-accent' : 'text-ink-2'}`"
      >
        <template #button><AppIcon name="filter" :size="18" class="stroke-[1.8]" /></template>
        <template v-if="store.state?.kinds.some((x) => counts.kinds[x])">
          <div class="px-2.5 pt-1 pb-0.5 text-[11px] font-semibold text-muted">{{ t("mem.kinds") }}</div>
          <button
            v-for="k in store.state?.kinds.filter((x) => counts.kinds[x])"
            :key="k"
            class="menu-item py-1 text-[13px]"
            role="menuitemcheckbox"
            :aria-checked="kind === k"
            @click="kind = kind === k ? null : k"
          >
            <AppIcon name="check" :size="14" :class="kind === k ? 'text-accent' : 'invisible'" />
            <span class="flex-1">{{ t(`kind.${k}`) }}</span><span class="text-xs text-muted tabular-nums">{{ counts.kinds[k] }}</span>
          </button>
        </template>
        <template v-if="Object.keys(counts.sources).length">
          <div class="menu-sep" />
          <div class="px-2.5 pt-1 pb-0.5 text-[11px] font-semibold text-muted">{{ t("mem.sources") }}</div>
          <button
            v-for="(n, s) in counts.sources"
            :key="s"
            class="menu-item py-1 text-[13px]"
            role="menuitemcheckbox"
            :aria-checked="source === s"
            @click="source = source === s ? null : String(s)"
          >
            <AppIcon name="check" :size="14" :class="source === s ? 'text-accent' : 'invisible'" />
            <span class="flex-1 truncate">{{ srcLabel(String(s)) }}</span><span class="text-xs text-muted tabular-nums">{{ n }}</span>
          </button>
        </template>
        <div class="menu-sep" />
        <div class="px-2.5 pt-1 pb-0.5 text-[11px] font-semibold text-muted">{{ t("mem.status") }}</div>
        <button
          v-for="s in (['review', 'archived'] as const)"
          :key="s"
          class="menu-item py-1 text-[13px]"
          role="menuitemcheckbox"
          :aria-checked="status === s"
          @click="status = status === s ? 'active' : s"
        >
          <AppIcon name="check" :size="14" :class="status === s ? 'text-accent' : 'invisible'" />
          <span class="flex-1">{{ s === "review" ? t("mem.reviewDue") : t("mem.archived") }}</span>
          <span class="text-xs text-muted tabular-nums">{{ s === "review" ? reviewDue.length : store.state?.counts.archived }}</span>
        </button>
        <template v-if="filtered">
          <div class="menu-sep" />
          <button class="menu-item py-1 text-[13px] text-accent" role="menuitem" @click="clearFilters">{{ t("mac.showAll") }}</button>
        </template>
      </DropMenu>
      <DropMenu v-if="current" :label="t('mem.topicOptions')" button-class="btn btn-ghost btn-icon text-ink-2">
        <button class="menu-item" role="menuitem" @click="toggleTopicPrivate">
          <AppIcon name="lock" class="text-muted" />{{ current.private ? t("mem.makeTopicPublic") : t("mem.makeTopicPrivate") }}
        </button>
      </DropMenu>
      <label class="relative ml-1 flex items-center">
        <AppIcon name="search" :size="14" class="pointer-events-none absolute left-2.5 text-muted" />
        <input
          ref="searchInput"
          v-model="query"
          type="search"
          class="min-h-8 w-[200px] rounded-lg bg-fill pr-2.5 pl-7 text-[13px] outline-none placeholder:text-faint focus:shadow-[0_0_0_3px_var(--color-focus)]"
          :placeholder="t('mac.search')"
          :aria-label="t('mem.search')"
          :title="`${t('mac.search')} ⌘F`"
        />
      </label>
    </DesktopToolbar>

    <div class="flex min-h-0 flex-1">
      <section ref="list" :aria-label="title" class="min-w-0 flex-1 space-y-3 overflow-y-auto px-3 pt-3 pb-5" @keydown="onListKey">
        <div v-if="current?.private" class="notice notice-private py-2 text-xs">
          <AppIcon name="lock" :size="14" />{{ t("mem.privateTopic") }}
        </div>

        <form v-if="status === 'active'" class="flex min-h-9 items-center gap-2 rounded-lg bg-fill pr-2 pl-3" @submit.prevent="add">
          <AppIcon name="plus" :size="14" class="stroke-[2.2] text-muted" />
          <input
            ref="draftInput"
            v-model="draft"
            type="text"
            class="min-h-[34px] min-w-0 flex-1 bg-transparent text-[13px] outline-none placeholder:text-faint"
            :placeholder="current ? t('mac.newIn', { topic: current.title }) : t('mac.newAuto')"
            :aria-label="current ? t('mac.newIn', { topic: current.title }) : t('mac.newAuto')"
          />
          <kbd class="inline-flex min-h-5 items-center rounded-[5px] bg-surface px-1.5 font-sans text-[11px] text-muted shadow-[0_0_0_1px_var(--color-line)]">{{ t("mac.return") }}</kbd>
        </form>

        <a
          v-if="status === 'active' && !topic && banner"
          href="#/changes"
          class="notice notice-wait justify-between gap-3 py-2 text-xs text-ink transition hover:brightness-[0.98]"
        >
          <span>{{ banner }}</span>
          <span class="flex shrink-0 items-center gap-1 font-semibold text-wait-ink">{{ t("mem.reviewNow") }}<AppIcon name="chevronRight" :size="12" /></span>
        </a>
        <div v-if="status === 'active' && topic && topicPending.length" class="space-y-2">
          <ProposalCard v-for="p in topicPending.slice(0, 5)" :key="p.id" :proposal="p" compact />
          <a v-if="topicPending.length > 5" href="#/changes" class="inline-flex items-center gap-1 text-xs font-medium text-accent">
            +{{ topicPending.length - 5 }} {{ t("mem.waiting") }}<AppIcon name="chevronRight" :size="12" />
          </a>
        </div>

        <div v-for="g in groups" :key="g.key" class="flex flex-col gap-0.5">
          <div v-if="g.title" class="flex items-center justify-between px-3 pt-1.5 pb-0.5 text-[11px] font-semibold text-muted">
            <a v-if="!topic" :href="`#/memories/${encodeURIComponent(g.key)}`" class="hover:text-ink">{{ g.title }}</a>
            <span v-else>{{ g.title }}</span>
            <span class="font-normal tabular-nums">{{ g.items.length }}</span>
          </div>
          <button
            v-for="m in g.items"
            :key="m.id"
            type="button"
            :data-id="m.id"
            class="flex w-full flex-col gap-1 rounded-lg px-3 py-[9px] text-left"
            :class="m.id === selectedId ? 'bg-selected text-white' : 'hover:bg-hover'"
            :aria-pressed="m.id === selectedId"
            @click="selectedId = m.id"
          >
            <span class="block text-sm leading-[1.45] break-words">
              <AppIcon
                v-if="m.pinned"
                name="starFill"
                :size="12"
                class="mr-1.5 inline align-[-1px]"
                :class="m.id === selectedId ? 'text-white' : 'text-wait'"
              /><span v-if="m.pinned" class="sr-only">{{ t("mem.pinned") }}: </span>{{ m.content }}
            </span>
            <span class="flex flex-wrap items-center gap-x-2 gap-y-1 text-xs leading-snug" :class="m.id === selectedId ? 'text-white' : 'text-muted'">
              <span
                v-if="m.status === 'active' && freshMemories[m.id]"
                :class="[chip, 'font-semibold', m.id === selectedId ? 'bg-white text-fresh-ink' : 'chip-fresh']"
                :title="t('mem.newTitle', { app: freshMemories[m.id] })"
              >{{ t("mem.new") }}</span>
              <span :class="[chip, m.id === selectedId ? 'bg-black/20 text-white' : '']">{{ t(`kind.${m.kind}`) }}</span>
              <span v-if="!topic && status !== 'active'" :class="[chip, m.id === selectedId ? 'bg-black/20 text-white' : '']">{{ topicTitle(m.topic) }}</span>
              <span v-if="m.effective_private" :class="[chip, m.id === selectedId ? 'bg-black/20 text-white' : 'chip-private']">
                <AppIcon name="lock" :size="10" />{{ t("common.private") }}
              </span>
              <span>{{ srcLabel(m.source) }}</span><span aria-hidden="true">·</span><span :title="m.updated_at">{{ relTime(m.updated_at) }}</span>
              <template v-if="m.until">
                <span aria-hidden="true">·</span>
                <span :class="m.review_due && m.id !== selectedId ? 'font-semibold text-wait-ink' : ''">{{ t("mem.until", { date: m.until }) }}</span>
              </template>
            </span>
          </button>
        </div>

        <div v-if="!visible.length && filtered" class="py-16 text-center text-muted">
          {{ t("mem.noMatch") }} <button type="button" class="btn btn-link btn-sm" @click="clearFilters">{{ t("mac.showAll") }}</button>
        </div>
        <div v-else-if="!visible.length && current" class="px-6 py-14 text-center">
          <p class="font-semibold">{{ t("mem.emptyTopic") }}</p>
          <p class="mt-1 text-muted">{{ t("mem.emptyTopicHint") }}</p>
        </div>
        <div v-else-if="!visible.length" class="px-6 py-16 text-center">
          <p class="text-base font-semibold">{{ t("mem.emptyAll") }}</p>
          <p class="mx-auto mt-1 max-w-sm text-muted">{{ t("mem.emptyAllHint") }}</p>
          <div class="mt-4 flex flex-wrap justify-center gap-2">
            <a href="#/import" class="btn btn-primary btn-sm">{{ t("mem.importCta") }}</a>
            <a href="#/welcome" class="btn btn-sm">{{ t("onb.showGuide") }}</a>
          </div>
        </div>
      </section>

      <MemoryInspector
        v-if="selected"
        :key="selected.id"
        :memory="selected"
        class="w-[340px] shrink-0 overflow-y-auto border-l border-sep bg-inspector"
      />
      <div v-else class="flex w-[340px] shrink-0 items-center justify-center border-l border-sep bg-inspector px-8 text-center text-muted">
        {{ t("mac.select") }}
      </div>
    </div>

    <SortDialog v-if="sorting && current" :topic="current.slug" @close="sorting = false" />
  </div>
</template>
