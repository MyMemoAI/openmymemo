<script setup lang="ts">
import { computed, ref } from "vue";
import { api, type Proposal } from "../api";
import type { IconName } from "../icons";
import { relTime, t } from "../i18n";
import { act, quoted, store, topicTitle } from "../store";
import AppIcon from "./AppIcon.vue";

/** A proposal that waits for the user's decision, or (in History) one that was decided. */
const props = defineProps<{ proposal: Proposal; compact?: boolean }>();

const editing = ref(false);
const draft = ref("");
const draftTopic = ref("");
const busy = ref(false);
const p = computed(() => props.proposal);
const isPending = computed(() => p.value.status === "pending");
const text = computed(() => p.value.final_content ?? p.value.content);

const HOLD: Record<string, { cls: string; icon: IconName | null }> = {
  private: { cls: "chip-private", icon: "lock" },
  protected: { cls: "chip-wait", icon: "pencil" },
  approval: { cls: "", icon: null },
  sensitive: { cls: "chip-private", icon: "warning" },
};
const DONE = ["accepted", "edited_accepted", "applied"];

function startEdit() {
  draft.value = p.value.content ?? "";
  draftTopic.value = p.value.topic ?? "";
  editing.value = true;
}

async function run(fn: () => Promise<unknown>, okText?: string) {
  busy.value = true;
  await act(fn, okText);
  busy.value = false;
  editing.value = false;
}

const accept = () => run(() => api.accept(p.value.id));
const acceptEdited = () => run(() => api.accept(p.value.id, { content: draft.value, topic: draftTopic.value }));
const asNew = () => run(() => api.accept(p.value.id, { as_new: true }));
const reject = (block = false) => run(() => api.reject(p.value.id, block));
const undo = () => run(() => api.undo(p.value.id), t("chg.undone"));
</script>

<template>
  <article
    class="space-y-3 rounded-[14px] p-4.5"
    :class="isPending ? 'bg-wait-soft shadow-[0_0_0_1px_var(--color-wait-ring)]' : 'card'"
  >
    <div class="flex flex-wrap items-center gap-x-2 gap-y-1 text-[13px] text-muted">
      <span class="font-semibold text-ink">{{ p.connector_name }}</span>
      <span>{{ t(isPending ? `rev.wants.${p.op}` : `rev.proposed.${p.op}`) }}</span>
      <span class="chip">{{ topicTitle(p.final_topic ?? p.topic) }}</span>
      <span v-if="p.kind" class="chip">{{ t(`kind.${p.kind}`) }}</span>
      <span :title="p.created_at">{{ relTime(isPending ? p.created_at : p.decided_at ?? p.created_at) }}</span>
      <span v-if="isPending && p.hold" class="chip ml-auto" :class="HOLD[p.hold]?.cls">
        <AppIcon v-if="HOLD[p.hold]?.icon" :name="HOLD[p.hold].icon!" :size="11" />{{ t(`chg.hold.${p.hold}`) }}
      </span>
      <span v-if="!isPending" class="ml-auto font-semibold" :class="DONE.includes(p.status) ? 'text-plus' : 'text-muted'">
        {{ t(`chg.status.${p.status}`) }}
      </span>
    </div>

    <div class="space-y-1 text-[15px] leading-relaxed">
      <div v-if="p.before_content && p.op !== 'add'" class="flex gap-2.5 text-muted">
        <span class="minus" aria-hidden="true">−</span>
        <span class="min-w-0 break-words" :class="p.op === 'archive' ? 'line-through' : ''">{{ p.before_content }}</span>
      </div>
      <div v-if="!editing && text && p.op !== 'archive'" class="flex gap-2.5">
        <span class="plus" aria-hidden="true">+</span>
        <span class="min-w-0 break-words" :class="p.status === 'rejected' || p.status === 'expired' || p.status === 'undone' ? 'text-muted' : ''">{{ text }}</span>
      </div>
      <div v-if="editing" class="space-y-2 pt-1">
        <textarea v-model="draft" class="input min-h-20 bg-surface text-[15px]" />
        <select v-model="draftTopic" class="input select w-auto max-w-full bg-surface" :aria-label="t('mem.topics')">
          <option v-for="tp in store.state?.topics" :key="tp.slug" :value="tp.slug">{{ tp.title }}</option>
        </select>
      </div>
    </div>

    <div v-if="p.original_op === 'add' || p.conflict" class="space-y-1 text-[13px]">
      <div v-if="p.original_op === 'add'" class="flex items-center gap-1.5 text-muted"><AppIcon name="refresh" :size="14" />{{ t("rev.convertedFromAdd") }}</div>
      <div v-if="p.conflict" class="flex items-center gap-1.5 text-danger-ink"><AppIcon name="warning" :size="14" />{{ t("rev.conflict") }}</div>
    </div>

    <dl v-if="!compact && (p.reason || p.evidence || p.check_note)" class="grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-1 text-[13px] text-ink-2">
      <template v-if="p.reason"><dt class="text-muted">{{ t("rev.why") }}</dt><dd>{{ p.reason }}</dd></template>
      <template v-if="p.evidence"><dt class="text-muted">{{ t("rev.said") }}</dt><dd class="italic">{{ quoted(p.evidence) }}</dd></template>
      <template v-if="p.check_note"><dt class="text-muted">{{ t("rev.checked") }}</dt><dd data-test="check-note">{{ p.check_note }}</dd></template>
    </dl>

    <div v-if="isPending" class="flex flex-wrap items-center gap-2 pt-0.5">
      <template v-if="!editing">
        <button class="btn btn-primary btn-sm" :disabled="busy" @click="accept">{{ t("rev.accept") }}</button>
        <button v-if="p.op !== 'archive'" class="btn btn-sm" :disabled="busy" @click="startEdit">{{ t("rev.editAccept") }}</button>
        <button v-if="p.original_op === 'add'" class="btn btn-sm" :disabled="busy" @click="asNew">{{ t("rev.asNew") }}</button>
        <button class="btn btn-sm" :disabled="busy" @click="reject()">{{ t("rev.reject") }}</button>
        <button v-if="!compact && p.topic" class="btn btn-quiet btn-sm btn-wrap sm:ml-auto" :disabled="busy" @click="reject(true)">
          {{ t("rev.block", { topic: topicTitle(p.topic), app: p.connector_name }) }}
        </button>
      </template>
      <template v-else>
        <button class="btn btn-primary btn-sm" :disabled="busy || !draft.trim()" @click="acceptEdited">{{ t("rev.accept") }}</button>
        <button class="btn btn-sm" @click="editing = false">{{ t("common.cancel") }}</button>
      </template>
    </div>
    <div v-else-if="p.can_undo">
      <button class="btn btn-sm" :disabled="busy" @click="undo">{{ t("rev.undo") }}</button>
    </div>
  </article>
</template>
