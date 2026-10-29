<script setup lang="ts">
import { computed, ref } from "vue";
import { api, type CloudStatus, type CloudSync } from "../api";
import { relTime, t, tn } from "../i18n";
import { toast } from "../store";
import AppIcon from "./AppIcon.vue";
import ModalDialog from "./ModalDialog.vue";

/**
 * MyMemo Memory sync (product-design.md §20) inside the OpenMyMemo Cloud row, while this computer is linked to this
 * folder: off, the consent with the private-topic picker, the first merge's preview, the state, the picked private
 * topics and turning it off ("Also delete the copy in MyMemo"). Every answer is the whole Cloud status, handed to
 * the row (`changed`).
 */
const props = defineProps<{ sync: CloudSync }>();
const emit = defineEmits<{ changed: [status: CloudStatus] }>();

const dialog = ref<"consent" | "topics" | "off" | null>(null);
const picks = ref<string[]>([]);
const deleteCopy = ref(false);
const busy = ref(false);

const sy = computed(() => props.sync);

type Tone = "fresh" | "wait" | "danger" | "muted";
const DOT: Record<Tone, string> = { fresh: "bg-fresh", wait: "bg-wait", danger: "bg-danger", muted: "bg-faint" };

const line = computed<{ text: string; tone: Tone }>(() => {
  const s = sy.value;
  switch (s.state) {
    case "syncing":
      return { text: s.last_sync_at ? t("sync.state.syncing", { time: relTime(s.last_sync_at) }) : t("sync.state.syncingNow"), tone: "fresh" };
    case "refused":
      return { text: t("sync.state.refused"), tone: "danger" };
    case "preview":
      return { text: t("sync.state.preview"), tone: "wait" };
    case "merging":
      return { text: t("sync.state.merging"), tone: "wait" };
    case "waiting":
      return { text: t("sync.state.waiting"), tone: "wait" };
    default:
      return { text: t("sync.state.off"), tone: "muted" };
  }
});

const reason = computed(() => {
  const key = sy.value.reason ? `sync.reason.${sy.value.reason}` : "";
  return key && t(key) !== key ? t(key) : sy.value.reason;
});

function open(which: "consent" | "topics" | "off") {
  picks.value = which === "consent" ? [] : [...sy.value.picked];
  deleteCopy.value = false;
  dialog.value = which;
}

function toggle(slug: string) {
  picks.value = picks.value.includes(slug) ? picks.value.filter((s) => s !== slug) : [...picks.value, slug];
}

async function run(call: () => Promise<CloudStatus>, done?: string) {
  busy.value = true;
  try {
    const status = await call();
    emit("changed", status);
    dialog.value = null;
    if (done) toast(done);
  } catch (e) {
    toast(e instanceof Error ? e.message : String(e), "error");
  } finally {
    busy.value = false;
  }
}

const turnOn = () => run(() => api.cloudSync({ on: true, consent: true, private_topics: picks.value }), t("sync.turnedOn"));
const saveTopics = () => run(() => api.cloudSyncTopics(picks.value));
const turnOff = () => run(() => api.cloudSync({ on: false, delete_copy: deleteCopy.value }), t("sync.turnedOff"));
const startMerge = () => run(() => api.cloudSyncMerge());
const cancelMerge = () => run(() => api.cloudSync({ on: false, delete_copy: false }));
</script>

<template>
  <div class="space-y-2.5 bg-sunken px-4.5 py-3 text-[13px]" data-sync>
    <div class="flex items-center gap-3">
      <AppIcon name="refresh" :size="15" class="shrink-0 text-accent" />
      <div class="min-w-0 flex-1">
        <div class="font-semibold">{{ t("sync.name") }}</div>
        <div class="flex items-center gap-1.5 text-muted">
          <span class="dot" :class="DOT[line.tone]" aria-hidden="true" />
          <span class="truncate" :title="line.text">{{ line.text }}</span>
        </div>
      </div>
      <template v-if="sy.enabled">
        <button v-if="sy.private_topics.length" class="btn btn-quiet btn-sm" :disabled="busy" @click="open('topics')">{{ t("sync.topics") }}</button>
        <button class="btn btn-sm" :disabled="busy" @click="open('off')">{{ t("sync.turnOff") }}</button>
      </template>
      <button v-else class="btn btn-sm btn-primary" :disabled="busy" @click="open('consent')">{{ t("sync.turnOn") }}</button>
    </div>

    <p v-if="!sy.enabled" class="text-muted">{{ t("sync.pitch") }}</p>
    <p v-if="!sy.enabled && sy.off_by === 'mymemo'" class="text-muted" role="status">{{ t("sync.offByMymemo") }}</p>
    <p v-if="sy.pending_off" class="text-muted" role="status">{{ t("sync.pendingOff") }}</p>
    <p v-if="sy.state === 'refused' && reason" class="text-danger" role="status">{{ reason }}</p>
    <p v-if="sy.enabled && sy.skipped" class="text-muted">{{ tn("sync.skipped", sy.skipped) }}</p>

    <div v-if="sy.state === 'preview'" class="space-y-2 rounded-[10px] bg-surface p-3 shadow-[0_0_0_1px_var(--color-ring)]" data-preview>
      <p v-if="sy.preview">{{ t("sync.preview", sy.preview) }}</p>
      <p v-else class="text-muted">{{ t("sync.previewWaiting") }}</p>
      <div class="flex gap-2">
        <button class="btn btn-sm btn-primary" :disabled="busy || !sy.preview" @click="startMerge">{{ t("sync.startMerge") }}</button>
        <button class="btn btn-sm" :disabled="busy" @click="cancelMerge">{{ t("sync.cancel") }}</button>
      </div>
    </div>

    <ModalDialog v-if="dialog === 'consent'" :title="t('sync.consentTitle')" @close="dialog = null">
      <div class="space-y-3 text-[13.5px]" data-consent>
        <ul class="list-disc space-y-1.5 pl-5">
          <li>{{ t("sync.consent.stores") }}</li>
          <li>{{ t("sync.consent.never") }}</li>
          <li>{{ t("sync.consent.readers") }}</li>
          <li>{{ t("sync.consent.wins") }}</li>
          <li>{{ t("sync.consent.delete") }}</li>
        </ul>
        <p class="text-muted">{{ t("sync.consent.needs") }}</p>
        <fieldset class="space-y-1.5">
          <legend class="mb-1 font-semibold">{{ t("sync.private") }}</legend>
          <p v-if="!sy.private_topics.length" class="text-muted">{{ t("sync.privateNone") }}</p>
          <label v-for="topic in sy.private_topics" :key="topic.slug" class="flex items-center gap-2">
            <input type="checkbox" :checked="picks.includes(topic.slug)" @change="toggle(topic.slug)" />
            <span>{{ topic.title }}</span>
          </label>
        </fieldset>
        <div class="flex justify-end gap-2 pt-1">
          <button class="btn btn-sm" @click="dialog = null">{{ t("sync.cancel") }}</button>
          <button class="btn btn-sm btn-primary" :disabled="busy" @click="turnOn">{{ t("sync.agree") }}</button>
        </div>
      </div>
    </ModalDialog>

    <ModalDialog v-if="dialog === 'topics'" :title="t('sync.topicsTitle')" @close="dialog = null">
      <div class="space-y-3 text-[13.5px]">
        <p class="text-muted">{{ t("sync.topicsHelp") }}</p>
        <label v-for="topic in sy.private_topics" :key="topic.slug" class="flex items-center gap-2">
          <input type="checkbox" :checked="picks.includes(topic.slug)" @change="toggle(topic.slug)" />
          <span>{{ topic.title }}</span>
        </label>
        <div class="flex justify-end gap-2 pt-1">
          <button class="btn btn-sm" @click="dialog = null">{{ t("sync.cancel") }}</button>
          <button class="btn btn-sm btn-primary" :disabled="busy" @click="saveTopics">{{ t("sync.save") }}</button>
        </div>
      </div>
    </ModalDialog>

    <ModalDialog v-if="dialog === 'off'" :title="t('sync.offTitle')" @close="dialog = null">
      <div class="space-y-3 text-[13.5px]">
        <p class="text-muted">{{ t("sync.offHelp") }}</p>
        <label class="flex items-center gap-2">
          <input v-model="deleteCopy" type="checkbox" data-delete-copy />
          <span>{{ t("sync.deleteCopy") }}</span>
        </label>
        <div class="flex justify-end gap-2 pt-1">
          <button class="btn btn-sm" @click="dialog = null">{{ t("sync.cancel") }}</button>
          <button class="btn btn-sm btn-danger" :disabled="busy" @click="turnOff">{{ t("sync.off") }}</button>
        </div>
      </div>
    </ModalDialog>
  </div>
</template>
