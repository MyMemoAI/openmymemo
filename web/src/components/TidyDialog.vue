<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { api, type TidyResult, type TidyStatus } from "../api";
import { assistantPlace, relTime, t } from "../i18n";
import { act, store, toast } from "../store";
import ModalDialog from "./ModalDialog.vue";

/** Tidy up (product-design.md §17.6): estimate, run now, or set the schedule on this computer. */
const emit = defineEmits<{ close: [] }>();

const status = ref<TidyStatus | null>(null);
const result = ref<TidyResult | null>(null);
const topic = ref("");
const busy = ref(false);

const SCHEDULES: TidyStatus["schedule"][] = ["off", "daily", "weekly", "monthly"];
const topics = computed(() => (store.state?.topics ?? []).filter((tp) => !tp.private));

async function load() {
  try {
    status.value = await api.tidy();
  } catch (e) {
    toast(e instanceof Error ? e.message : String(e), "error");
  }
}
onMounted(load);

async function setSchedule(e: Event) {
  const value = (e.target as HTMLSelectElement).value as TidyStatus["schedule"];
  const res = await act(() => api.tidySchedule(value));
  if (res) status.value = res;
}

async function run(dryRun: boolean) {
  busy.value = true;
  result.value = null;
  const res = await act(() => api.tidyRun({ dry_run: dryRun, topic: topic.value || null }));
  busy.value = false;
  if (res) result.value = res;
  if (!dryRun) await load();
}
</script>

<template>
  <ModalDialog :title="t('tidy.title')" wide @close="emit('close')">
    <div v-if="status === null" class="py-10 text-center text-sm text-muted">{{ t("common.loading") }}</div>
    <div v-else class="space-y-4">
      <p class="text-[13px] text-ink-2">{{ t("tidy.explain") }} {{ t("tidy.private") }}</p>
      <p v-if="!status.available" class="text-[13px] text-danger-ink" data-test="tidy-unavailable">
        {{ status.reason ? `${status.reason} ` : "" }}{{ t("tidy.unavailable", { where: assistantPlace() }) }}
      </p>

      <div class="flex flex-wrap items-center gap-x-4 gap-y-2 text-[13px]">
        <label class="flex items-center gap-2">
          <span class="text-muted">{{ t("tidy.topic") }}</span>
          <select v-model="topic" class="input select w-auto max-w-full">
            <option value="">{{ t("tidy.allTopics") }}</option>
            <option v-for="tp in topics" :key="tp.slug" :value="tp.slug">{{ tp.title }}</option>
          </select>
        </label>
        <label class="flex items-center gap-2">
          <span class="text-muted">{{ t("tidy.schedule") }}</span>
          <select :value="status.schedule" class="input select w-auto max-w-full" data-test="tidy-schedule" @change="setSchedule">
            <option v-for="s in SCHEDULES" :key="s" :value="s">{{ t(`tidy.schedule.${s}`) }}</option>
          </select>
        </label>
        <span v-if="status.last_run" class="text-muted">{{ t("tidy.lastRun", { when: relTime(status.last_run) }) }}</span>
      </div>
      <p v-if="status.schedule !== 'off'" class="text-xs text-muted">{{ t("tidy.scheduleNote") }}</p>

      <div v-if="busy" class="py-4 text-center text-sm text-muted">{{ t("tidy.running") }}</div>
      <div v-else-if="result" class="space-y-3" data-test="tidy-result">
        <p class="text-[14px]" :class="result.ok ? '' : 'text-danger-ink'">{{ result.message }}</p>
        <ul v-if="result.items.length" class="divide-y divide-sep rounded-xl shadow-[0_0_0_1px_var(--color-line)]">
          <li v-for="(item, i) in result.items" :key="i" class="space-y-1 px-3.5 py-2.5 text-[13px]">
            <div class="flex flex-wrap items-center gap-2">
              <span class="chip">{{ t(`tidy.kind.${item.kind}`) }}</span>
              <span class="text-muted">{{ item.reason }}</span>
            </div>
            <div v-for="(b, j) in item.before" :key="j" class="flex gap-2 text-muted"><span class="minus" aria-hidden="true">−</span><span class="min-w-0 break-words">{{ b }}</span></div>
            <div v-if="item.content" class="flex gap-2"><span class="plus" aria-hidden="true">+</span><span class="min-w-0 break-words">{{ item.content }}</span></div>
            <div v-if="item.until" class="text-muted">{{ t("chg.until", { date: item.until }) }}</div>
          </li>
        </ul>
      </div>

      <div class="sticky bottom-0 -mx-5.5 -mb-5.5 mt-4 flex flex-wrap justify-end gap-2 rounded-b-[18px] border-t border-sep bg-sunken px-5.5 py-3.5">
        <button class="btn" @click="emit('close')">{{ result && (result.applied || result.waiting) ? t("tidy.review") : t("common.close") }}</button>
        <button class="btn" :disabled="busy" data-test="tidy-estimate" @click="run(true)">{{ t("tidy.check") }}</button>
        <button class="btn btn-primary" :disabled="busy || !status.available" data-test="tidy-run" @click="run(false)">{{ t("tidy.run") }}</button>
      </div>
    </div>
  </ModalDialog>
</template>
