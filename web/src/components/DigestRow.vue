<script setup lang="ts">
import { computed, nextTick, ref } from "vue";
import { api, type Memory, type Proposal } from "../api";
import { relTime, t } from "../i18n";
import { act, quoted, store, topicTitle } from "../store";
import AppIcon from "./AppIcon.vue";

/** One change that applied without the user, in the digest: keep, edit or undo it. */
const props = defineProps<{ proposal: Proposal }>();
const emit = defineEmits<{ history: [Memory] }>();

const p = computed(() => props.proposal);
const open = ref(false);
const editing = ref(false);
const draft = ref("");
const draftTopic = ref("");
const busy = ref(false);
const input = ref<HTMLTextAreaElement | null>(null);

const after = computed(() => p.value.final_content ?? p.value.content ?? "");
const topic = computed(() => p.value.final_topic ?? p.value.topic);
/** An update that only moved the memory to another topic. */
const movedOnly = computed(() => p.value.op === "update" && p.value.before_content === after.value);
const moved = computed(() => p.value.op === "update" && !!p.value.before_topic && p.value.before_topic !== topic.value);
const hasDetails = computed(() => !!(p.value.reason || p.value.evidence || p.value.check_note || p.value.original_op === "add"));
const memory = computed(() => store.memories.find((m) => m.id === p.value.applied_memory_id) ?? null);

const OP_STYLE: Record<string, string> = { add: "chip-fresh", update: "chip-accent", archive: "font-semibold" };

async function startEdit() {
  draft.value = after.value;
  draftTopic.value = topic.value ?? "";
  editing.value = true;
  await nextTick();
  input.value?.focus();
}

async function run(fn: () => Promise<unknown>, okText: string) {
  busy.value = true;
  await act(fn, okText);
  busy.value = false;
  editing.value = false;
}

const keep = () => run(() => api.reviewed(p.value.id), t("chg.kept"));
const save = () => run(() => api.editApplied(p.value.id, draft.value.trim(), draftTopic.value || undefined), t("chg.saved"));
const undo = () => run(() => api.undo(p.value.id), t("chg.undone"));
const undoAndBlock = () =>
  run(() => api.undo(p.value.id, true), t("chg.blocked", { app: p.value.connector_name, topic: topicTitle(p.value.topic) }));

function onKey(e: KeyboardEvent) {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
    e.preventDefault();
    if (draft.value.trim()) save();
  } else if (e.key === "Escape") {
    editing.value = false;
  }
}
</script>

<template>
  <article class="space-y-2 px-4.5 py-3.5">
    <div class="flex flex-wrap items-center gap-x-2 gap-y-1 text-[13px] text-muted">
      <span class="chip" :class="OP_STYLE[p.op]">{{ t(`chg.op.${p.op}`) }}</span>
      <span class="chip">{{ topicTitle(topic) }}</span>
      <span :title="p.decided_at ?? p.created_at">{{ relTime(p.decided_at ?? p.created_at) }}</span>
      <button
        v-if="hasDetails"
        class="ml-auto inline-flex min-h-7 items-center gap-1 rounded-md px-1.5 hover:bg-fill hover:text-ink"
        :aria-expanded="open"
        @click="open = !open"
      >
        {{ open ? t("chg.hideDetails") : t("chg.details") }}
        <AppIcon name="chevronDown" :size="13" class="transition-transform" :class="open ? 'rotate-180' : ''" />
      </button>
    </div>

    <div v-if="!editing" class="space-y-1 text-[15px] leading-relaxed">
      <template v-if="p.op === 'add'">
        <div class="flex gap-2.5"><span class="plus" aria-hidden="true">+</span><span class="min-w-0 break-words">{{ after }}</span></div>
      </template>
      <template v-else-if="p.op === 'archive'">
        <div class="flex gap-2.5 text-muted">
          <span class="minus" aria-hidden="true">−</span><span class="min-w-0 break-words line-through">{{ p.before_content }}</span>
        </div>
      </template>
      <template v-else-if="movedOnly">
        <div class="min-w-0 break-words">{{ after }}</div>
      </template>
      <template v-else>
        <div class="flex gap-2.5 text-muted">
          <span class="minus" aria-hidden="true">−</span><span class="min-w-0 break-words">{{ p.before_content }}</span>
        </div>
        <div class="flex gap-2.5"><span class="plus" aria-hidden="true">+</span><span class="min-w-0 break-words">{{ after }}</span></div>
      </template>
      <div v-if="p.until" class="text-[13px] text-muted">
        {{ p.before_until ? t("chg.untilWas", { date: p.until, before: p.before_until }) : t("chg.until", { date: p.until }) }}
      </div>
      <div v-if="moved" class="flex items-center gap-1.5 text-[13px] text-muted">
        <AppIcon name="arrowRight" :size="13" />{{ t("chg.moved", { from: topicTitle(p.before_topic), to: topicTitle(topic) }) }}
      </div>
      <div v-if="p.conflict" class="flex items-center gap-1.5 text-[13px] text-danger-ink"><AppIcon name="warning" :size="13" />{{ t("rev.conflict") }}</div>
    </div>

    <div v-else class="space-y-2 pt-1">
      <textarea ref="input" v-model="draft" rows="2" class="input text-[15px]" @keydown="onKey" />
      <div class="flex flex-wrap items-center gap-2">
        <select v-model="draftTopic" class="input select w-auto max-w-full" :aria-label="t('mem.topics')">
          <option v-for="tp in store.state?.topics" :key="tp.slug" :value="tp.slug">{{ tp.title }}</option>
        </select>
        <button class="btn btn-primary btn-sm" :disabled="busy || !draft.trim()" @click="save">{{ t("common.save") }}</button>
        <button class="btn btn-sm" @click="editing = false">{{ t("common.cancel") }}</button>
      </div>
    </div>

    <div v-if="open" class="space-y-1 text-[13px] text-ink-2">
      <div v-if="p.original_op === 'add'" class="flex items-center gap-1.5 text-muted"><AppIcon name="refresh" :size="13" />{{ t("rev.convertedFromAdd") }}</div>
      <dl v-if="p.reason || p.evidence || p.check_note" class="grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-1">
        <template v-if="p.reason"><dt class="text-muted">{{ t("rev.why") }}</dt><dd>{{ p.reason }}</dd></template>
        <template v-if="p.evidence"><dt class="text-muted">{{ t("rev.said") }}</dt><dd class="italic">{{ quoted(p.evidence) }}</dd></template>
        <template v-if="p.check_note"><dt class="text-muted">{{ t("rev.checked") }}</dt><dd>{{ p.check_note }}</dd></template>
      </dl>
    </div>

    <div v-if="!editing" class="flex flex-wrap items-center gap-2 pt-0.5">
      <button class="btn btn-sm" :disabled="busy" @click="keep">{{ t("chg.keep") }}</button>
      <template v-if="p.can_undo">
        <button v-if="p.op !== 'archive'" class="btn btn-sm" :disabled="busy" @click="startEdit">{{ t("chg.edit") }}</button>
        <button class="btn btn-sm" :disabled="busy" @click="undo">{{ t("chg.undo") }}</button>
        <button v-if="p.topic" class="btn btn-quiet btn-sm btn-wrap sm:ml-auto" :disabled="busy" @click="undoAndBlock">
          {{ t("chg.undoBlock", { app: p.connector_name, topic: topicTitle(p.topic) }) }}
        </button>
      </template>
      <template v-else>
        <span class="text-[13px] text-muted">{{ t("chg.changedSince") }}</span>
        <button v-if="memory" class="btn btn-link btn-sm" @click="emit('history', memory)">{{ t("chg.openHistory") }}</button>
      </template>
    </div>
  </article>
</template>
