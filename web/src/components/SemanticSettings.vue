<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import { api, type SemanticModel, type SemanticStatus } from "../api";
import { t } from "../i18n";
import { toast } from "../store";
import AppIcon from "./AppIcon.vue";

/**
 * Settings → Assistant → "Search by meaning on this computer" (product-design.md §17.4): a switch, the list of
 * local embedding models with their download size, languages and license (the recommended one preselected),
 * the terms a model needs accepted first, and the download's progress. Independent of the Assistant's own
 * choice: it works with the Assistant off. Turning it off deletes the model and the index.
 */
const status = ref<SemanticStatus | null>(null);
const picked = ref("");
const accepted = ref(false);
const busy = ref(false);
let timer: ReturnType<typeof setTimeout> | null = null;

const running = computed(() => !!status.value && ["runtime", "download", "index"].includes(status.value.job.phase));
const model = computed<SemanticModel | undefined>(() => status.value?.models.find((m) => m.id === picked.value));
const needsTerms = computed(() => !!model.value?.terms_url && !status.value?.accepted_terms.includes(model.value.id));
const mb = (bytes: number) => Math.round(bytes / 2 ** 20).toLocaleString("en-US");

function take(s: SemanticStatus) {
  const before = status.value;
  status.value = s;
  if (!picked.value || (before && before.model !== s.model)) picked.value = s.model;
  if (before && ["runtime", "download", "index"].includes(before.job.phase)) {
    if (s.job.phase === "done") toast(t("sem.on"));
    else if (s.job.phase === "error" && s.job.error && s.job.code !== "runtime") toast(s.job.error, "error");
  }
  schedule();
}

function schedule() {
  if (timer) clearTimeout(timer);
  timer = running.value ? setTimeout(poll, 700) : null;
}

async function poll() {
  try {
    take(await api.semantic());
  } catch {
    schedule();
  }
}

onMounted(async () => {
  try {
    take(await api.semantic());
  } catch {
    status.value = null; // an older server: nothing to show
  }
});
onBeforeUnmount(() => timer && clearTimeout(timer));

async function turnOn() {
  const m = model.value;
  if (!m || busy.value) return;
  busy.value = true;
  try {
    take(await api.semanticSet({ enabled: true, model: m.id, accept_terms: needsTerms.value ? accepted.value : undefined }));
  } catch (e) {
    toast(e instanceof Error ? e.message : String(e), "error");
  } finally {
    busy.value = false;
  }
}

async function turnOff() {
  if (busy.value) return;
  busy.value = true;
  try {
    take(await api.semanticSet({ enabled: false }));
    toast(t("sem.off"));
  } catch (e) {
    toast(e instanceof Error ? e.message : String(e), "error");
  } finally {
    busy.value = false;
  }
}

function toggle() {
  if (status.value?.enabled) void turnOff();
  else void turnOn();
}

const canStart = computed(() => !!model.value?.available && !running.value && !busy.value && (!needsTerms.value || accepted.value));
const progress = computed(() => {
  const j = status.value?.job;
  if (!j) return "";
  if (j.phase === "runtime") return t("sem.phase.runtime");
  if (j.phase === "download") return t("sem.phase.download", { done: mb(j.done), total: mb(j.total) });
  if (j.phase === "index") return t("sem.phase.index", { done: j.done, total: j.total });
  return "";
});
const percent = computed(() => {
  const j = status.value?.job;
  return j && j.total ? Math.min(100, Math.round((j.done / j.total) * 100)) : 0;
});
</script>

<template>
  <div v-if="status" class="space-y-3 px-4.5 py-3.5" data-test="semantic">
    <div class="flex items-center gap-4">
      <div class="min-w-0 flex-1 space-y-0.5">
        <div class="font-semibold">{{ t("sem.title") }}</div>
        <p class="text-[13px] text-muted">{{ t("sem.help") }}</p>
      </div>
      <button
        role="switch"
        type="button"
        class="switch disabled:opacity-50"
        :aria-checked="status.enabled"
        :aria-label="t('sem.title')"
        :disabled="busy || running || (!status.enabled && !canStart)"
        @click="toggle"
      >
        <span class="switch-knob" />
      </button>
    </div>

    <div class="space-y-1.5" role="radiogroup" :aria-label="t('sem.title')">
      <label
        v-for="m in status.models"
        :key="m.id"
        class="flex cursor-pointer items-start gap-2.5 rounded-lg px-2.5 py-2"
        :class="[picked === m.id ? 'bg-surface shadow-[0_0_0_1px_var(--color-line)]' : '', m.available ? '' : 'opacity-60']"
      >
        <input v-model="picked" type="radio" class="mt-1" :value="m.id" :disabled="running || busy" :aria-label="m.name" />
        <span class="min-w-0 flex-1 space-y-0.5">
          <span class="flex flex-wrap items-baseline gap-x-2">
            <span class="font-semibold">{{ m.name }}</span>
            <span v-if="m.recommended" class="text-xs font-semibold text-fresh-ink">{{ t("sem.recommended") }}</span>
            <span class="text-xs text-muted">{{ t("sem.size", { mb: m.size_mb.toLocaleString("en-US") }) }}</span>
            <span v-if="!m.available" class="text-xs text-wait-ink">{{ t("sem.unavailable") }}</span>
            <span v-else-if="m.downloaded" class="text-xs text-muted">{{ t("sem.downloaded") }}</span>
          </span>
          <span class="block text-[13px] text-muted">{{ m.languages }}. {{ m.note }}</span>
          <a :href="m.license_url" target="_blank" rel="noopener" class="text-xs text-muted underline">{{ m.license }}</a>
        </span>
      </label>
    </div>

    <label v-if="needsTerms && model" class="flex items-start gap-2 text-[13px]">
      <input v-model="accepted" type="checkbox" class="mt-0.5" :disabled="running || busy" />
      <span>
        {{ t("sem.terms", { license: model.license }) }}
        (<a :href="model.terms_url ?? ''" target="_blank" rel="noopener" class="underline">{{ model.terms_url }}</a>)
      </span>
    </label>

    <div v-if="running" class="space-y-1" data-test="progress">
      <p class="text-[13px]">{{ progress }}</p>
      <div class="h-1.5 overflow-hidden rounded-full bg-sep">
        <div class="h-full bg-ink transition-[width]" :style="{ width: `${percent}%` }" />
      </div>
    </div>
    <template v-else>
      <div v-if="status.job.phase === 'error' && status.job.code === 'runtime'" class="space-y-1 text-[13px]" data-test="runtime">
        <p class="text-wait-ink">{{ t("sem.runtimeCommand") }}</p>
        <code class="block break-all rounded bg-surface px-2 py-1 font-mono text-xs">{{ status.runtime.command }}</code>
      </div>
      <p v-else-if="status.job.phase === 'error' && status.job.error" class="text-[13px] text-danger">{{ status.job.error }}</p>
      <p v-if="status.enabled && status.ready" class="flex items-center gap-1.5 text-[13px]" data-test="ready">
        <AppIcon name="lock" :size="13" class="text-muted" />
        {{ t("sem.ready", { count: status.index.count, total: status.index.memories }) }}
      </p>
      <p v-else-if="status.enabled" class="text-[13px] text-wait-ink">{{ t("sem.notReady") }}</p>
      <p v-else-if="status.embedder === 'endpoint'" class="text-[13px] text-muted">{{ t("sem.endpoint") }}</p>
      <div v-if="model && (!status.enabled || model.id !== status.model)" class="flex flex-wrap items-center gap-2">
        <button type="button" class="btn btn-sm" :disabled="!canStart" data-test="start" @click="turnOn">
          {{ status.enabled ? t("sem.switch", { mb: model.size_mb.toLocaleString("en-US") }) : t("sem.start", { mb: model.size_mb.toLocaleString("en-US") }) }}
        </button>
        <span v-if="needsTerms && !accepted" class="text-xs text-muted">{{ t("sem.termsNeeded", { license: model.license }) }}</span>
      </div>
    </template>
  </div>
</template>
