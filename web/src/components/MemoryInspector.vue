<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { api, type HistoryRow, type Kind, type Memory } from "../api";
import { native, shell } from "../desktop";
import { relTime, sourceLabel, t } from "../i18n";
import { srcLabel } from "../memoryList";
import { act, connectorName, copy, store, toast, topicsBySlug, topicTitle } from "../store";
import AppIcon from "./AppIcon.vue";
import DropMenu from "./DropMenu.vue";

/** The Mac app's inspector: everything about the selected memory, editable in place. */
const props = defineProps<{ memory: Memory }>();

const m = computed(() => props.memory);
const history = ref<HistoryRow[]>([]);

/**
 * The text field. `baseline` is the memory's text the draft started from: an untouched draft follows the
 * vault when an AI app or an editor changes the memory (even while the field has focus), and only text the
 * user actually changed is ever written back.
 */
const draft = ref("");
const baseline = ref("");
const dirty = computed(() => draft.value !== baseline.value);
watch(() => [m.value.id, m.value.content], () => {
  if (!dirty.value) draft.value = baseline.value = m.value.content;
}, { immediate: true });

async function loadHistory() {
  const id = m.value.id;
  const rows = await api.history(id).catch(() => []);
  if (id === m.value.id) history.value = rows;
}
watch(() => [m.value.id, m.value.version, m.value.status], loadHistory, { immediate: true });

const patch = (body: Parameters<typeof api.patchMemory>[1]) => act(() => api.patchMemory(m.value.id, body));

function revert() {
  draft.value = baseline.value = m.value.content;
}

async function save() {
  const text = draft.value.trim();
  if (!dirty.value || !text || text === m.value.content) return revert();
  // Someone else changed the memory while the user was typing: the user's words win, and they hear about
  // it, because the other version is now only in the history.
  const changedMeanwhile = m.value.content !== baseline.value;
  const other = actor(m.value.last_actor);
  const saved = await patch({ content: text });
  if (!saved) return;
  baseline.value = draft.value = saved.content;
  if (changedMeanwhile) toast(t("ins.changedMeanwhile", { who: other }));
}

function onKey(e: KeyboardEvent) {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
    e.preventDefault();
    (e.target as HTMLTextAreaElement).blur();
  } else if (e.key === "Escape") {
    revert();
    (e.target as HTMLTextAreaElement).blur();
  }
}

/** The newest change in the digest that produced this memory's current version. */
const fresh = computed(() =>
  m.value.status === "active" ? store.unreviewed.find((p) => p.applied_memory_id === m.value.id) ?? null : null,
);
const freshLine = computed(() => {
  const p = fresh.value;
  if (!p) return "";
  const key = p.op === "add" ? "ins.added" : p.op === "archive" ? "ins.archived" : "ins.changed";
  return t(key, { app: p.connector_name, time: relTime(p.decided_at ?? p.created_at) });
});
const keep = () => fresh.value && act(() => api.reviewed(fresh.value!.id), t("chg.kept"));
const undo = () => fresh.value && act(() => api.undo(fresh.value!.id), t("chg.undone"));

const topic = computed(() => topicsBySlug.value[m.value.topic]);
const privateTopics = computed(() => new Set((store.state?.topics ?? []).filter((x) => x.private).map((x) => x.slug)));

/** Which apps receive this memory; mirrors Connector.can_read_topic and the per-memory private flag. */
const readers = computed(() =>
  store.connectors.map((c) => {
    const topicOk = c.read_topics === "all" || c.read_topics.includes(m.value.topic);
    const privateOk = c.include_private || !privateTopics.value.has(m.value.topic);
    const memoryOk = c.include_private || !m.value.private;
    const why = !topicOk || !privateOk ? t("ins.cantRead", { app: c.name }) : !memoryOk ? t("ins.cantReadPrivate", { app: c.name }) : "";
    return { id: c.id, name: c.name, can: !why, why };
  }),
);

function actor(a: string) {
  return a.startsWith("connector:") ? connectorName(a.slice(10)) : sourceLabel(a);
}

const filePath = computed(() => `memories/${m.value.topic}.md`);
const reveal = () => shell.reveal(`${store.state?.vault}/${filePath.value}`);
const archive = () => act(() => api.archive(m.value.id));
const restore = () => act(() => api.restore(m.value.id));
const rollback = (row: HistoryRow) => act(() => api.rollback(m.value.id, row.version));
</script>

<template>
  <aside :aria-label="t('ins.label')" class="flex flex-col gap-3.5 px-4 pt-3.5 pb-4">
    <div class="flex items-center justify-between gap-2">
      <span class="text-[11px] font-semibold text-ink-2">{{ t(`kind.${m.kind}`) }} · v{{ m.version }}</span>
      <div v-if="m.status === 'active'" class="flex items-center gap-0.5">
        <button
          type="button"
          class="btn btn-ghost btn-icon"
          :class="m.pinned ? 'text-wait' : ''"
          :aria-label="m.pinned ? t('mem.unpin') : t('mem.pin')"
          :title="m.pinned ? t('mem.unpin') : t('mem.pin')"
          :aria-pressed="m.pinned"
          @click="patch({ pinned: !m.pinned })"
        >
          <AppIcon :name="m.pinned ? 'starFill' : 'star'" />
        </button>
        <DropMenu :label="t('common.more')">
          <button class="menu-item" role="menuitem" @click="patch({ private: !m.private })">
            <AppIcon name="lock" class="text-muted" />{{ m.private ? t("mem.makePublic") : t("mem.makePrivate") }}
          </button>
          <button class="menu-item" role="menuitem" @click="copy(m.content)">
            <AppIcon name="copy" class="text-muted" />{{ t("ins.copyText") }}
          </button>
        </DropMenu>
      </div>
    </div>

    <div v-if="fresh" class="space-y-2 rounded-[10px] bg-fresh-soft/70 px-3 py-2.5 shadow-[0_0_0_1px_var(--color-fresh-soft)]">
      <div class="space-y-0.5 text-xs leading-snug">
        <p class="text-fresh-ink">{{ freshLine }}</p>
        <p v-if="fresh.op === 'update' && fresh.before_content" class="text-ink-2">{{ t("ins.was", { text: fresh.before_content }) }}</p>
      </div>
      <div class="flex gap-1.5">
        <button type="button" class="btn btn-sm" @click="keep">{{ t("chg.keep") }}</button>
        <button v-if="fresh.can_undo" type="button" class="btn btn-ghost btn-sm text-ink-2" @click="undo">{{ t("chg.undo") }}</button>
      </div>
    </div>

    <textarea
      v-model="draft"
      :aria-label="t('ins.text')"
      rows="3"
      class="min-h-21 w-full resize-y rounded-[10px] bg-surface px-3 py-2.5 text-sm leading-normal shadow-[0_0_0_1px_var(--color-sep)] outline-none focus:shadow-[0_0_0_3px_var(--color-focus)]"
      :readonly="m.status !== 'active'"
      @blur="save"
      @keydown="onKey"
    />

    <dl class="divide-y divide-sep overflow-hidden rounded-[10px] bg-surface shadow-[0_0_0_1px_var(--color-ring)]">
      <div class="flex min-h-[38px] items-center justify-between gap-3 pr-1.5 pl-3">
        <dt class="text-muted">{{ t("ins.topic") }}</dt>
        <dd>
          <DropMenu v-if="m.status === 'active'" :label="t('mem.moveTo')" width="w-52" button-class="inline-flex min-h-7 items-center gap-1 rounded-md bg-fill px-2 text-[13px] hover:bg-fill-strong">
            <template #button>{{ topicTitle(m.topic) }}<AppIcon name="chevronDown" :size="12" class="text-muted" /></template>
            <div class="max-h-64 overflow-y-auto">
              <button
                v-for="tp in store.state?.topics"
                :key="tp.slug"
                class="menu-item py-1 text-[13px]"
                :class="tp.slug === m.topic ? 'font-semibold' : ''"
                role="menuitem"
                @click="tp.slug !== m.topic && patch({ topic: tp.slug })"
              >{{ tp.title }}</button>
            </div>
          </DropMenu>
          <span v-else class="pr-1.5">{{ topicTitle(m.topic) }}</span>
        </dd>
      </div>
      <div class="flex min-h-[38px] items-center justify-between gap-3 pr-1.5 pl-3">
        <dt class="text-muted">{{ t("ins.kind") }}</dt>
        <dd>
          <DropMenu v-if="m.status === 'active'" :label="t('mem.kinds')" width="w-40" button-class="inline-flex min-h-7 items-center gap-1 rounded-md bg-fill px-2 text-[13px] hover:bg-fill-strong">
            <template #button>{{ t(`kind.${m.kind}`) }}<AppIcon name="chevronDown" :size="12" class="text-muted" /></template>
            <button
              v-for="k in store.state?.kinds"
              :key="k"
              class="menu-item py-1 text-[13px]"
              :class="k === m.kind ? 'font-semibold' : ''"
              role="menuitem"
              @click="k !== m.kind && patch({ kind: k as Kind })"
            >{{ t(`kind.${k}`) }}</button>
          </DropMenu>
          <span v-else class="pr-1.5">{{ t(`kind.${m.kind}`) }}</span>
        </dd>
      </div>
      <div class="flex min-h-[38px] items-center justify-between gap-3 px-3">
        <dt class="text-muted">{{ t("ins.review") }}</dt>
        <dd :class="m.review_due ? 'font-semibold text-wait-ink' : ''">{{ m.until ?? t("common.none") }}</dd>
      </div>
      <div class="flex min-h-[38px] items-center justify-between gap-3 px-3">
        <dt class="text-muted">{{ t("ins.source") }}</dt>
        <dd class="text-right">{{ srcLabel(m.source) }} · <span :title="m.updated_at">{{ relTime(m.updated_at) }}</span></dd>
      </div>
      <div class="flex min-h-[38px] items-center justify-between gap-3 px-3">
        <dt class="text-muted">{{ t("ins.file") }}</dt>
        <dd class="truncate font-mono text-xs">{{ filePath }}</dd>
      </div>
    </dl>

    <section class="space-y-2" :aria-label="t('ins.canRead', { topic: topic?.title ?? m.topic })">
      <div class="flex items-baseline justify-between gap-2">
        <h2 class="text-[11px] font-semibold text-ink-2">{{ t("ins.canRead", { topic: topic?.title ?? m.topic }) }}</h2>
        <a href="#/connectors" class="text-xs font-medium text-accent hover:text-accent-hover">{{ t("ins.change") }}</a>
      </div>
      <div v-if="readers.length" class="flex flex-wrap gap-1.5">
        <span
          v-for="r in readers"
          :key="r.id"
          class="pill min-h-[26px] px-2.5 text-xs"
          :class="r.can ? 'pill-on' : 'pill-off hover:bg-fill'"
          :title="r.why || undefined"
        >{{ r.name }}<span v-if="!r.can" class="sr-only">: {{ r.why }}</span></span>
      </div>
      <p v-else class="text-xs text-muted">{{ t("ins.noApps") }}</p>
    </section>

    <section class="space-y-2" :aria-label="t('ins.history')">
      <h2 class="text-[11px] font-semibold text-ink-2">{{ t("ins.history") }}</h2>
      <ol class="space-y-2">
        <li v-for="(row, i) in history" :key="`${row.version}-${row.at}`" class="flex gap-2.5">
          <span class="mt-[5px] size-2 shrink-0 rounded-full" :class="i === 0 ? 'bg-accent' : 'bg-fill-strong'" aria-hidden="true" />
          <div class="min-w-0 flex-1 text-xs leading-snug">
            <span class="text-muted"><span class="font-semibold text-ink">v{{ row.version }}</span> · {{ actor(row.actor) }} · {{ relTime(row.at) }}</span>
            <p class="break-words text-ink-2" :class="row.op === 'archive' ? 'line-through' : ''">{{ row.content }}</p>
            <button
              v-if="i > 0 && row.op !== 'archive' && m.status === 'active'"
              type="button"
              class="text-xs font-medium text-accent hover:text-accent-hover"
              @click="rollback(row)"
            >{{ t("hist.rollback") }}</button>
          </div>
        </li>
      </ol>
    </section>

    <div class="mt-auto flex flex-wrap items-center gap-1 pt-1">
      <button v-if="native" type="button" class="btn btn-filled btn-sm" @click="reveal">
        <AppIcon name="memories" :size="14" />{{ t("ins.showInFinder") }}
      </button>
      <span class="flex-1" />
      <button v-if="m.status === 'active'" type="button" class="btn btn-ghost btn-sm text-ink-2" @click="archive">{{ t("mem.archive") }}</button>
      <button v-else type="button" class="btn btn-sm" @click="restore">{{ t("mem.restore") }}</button>
    </div>
  </aside>
</template>
