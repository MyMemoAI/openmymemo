<script setup lang="ts">
import { ref, watch } from "vue";
import { api, type ContextResult } from "../api";
import AppIcon from "../components/AppIcon.vue";
import PageHeader from "../components/PageHeader.vue";
import { t } from "../i18n";
import { copy, store } from "../store";

const props = defineProps<{ initialConnector: string | null }>();
const connector = ref<string | null>(props.initialConnector);
const maxChars = ref(8000);
const result = ref<ContextResult | null>(null);

async function load() {
  result.value = await api.context(connector.value, maxChars.value);
}

watch([connector, maxChars, () => store.memories], load, { immediate: true });
</script>

<template>
  <div class="mx-auto max-w-[820px] space-y-5">
    <PageHeader :title="t('nav.context')" :subtitle="t('ctx.explain')" />

    <div class="card flex flex-wrap items-center gap-x-6 gap-y-3 px-4.5 py-3.5">
      <label class="flex items-center gap-2.5 text-sm">
        <span class="text-muted">{{ t("ctx.who") }}</span>
        <select v-model="connector" class="input select w-auto font-medium">
          <option :value="null">{{ t("ctx.default") }}</option>
          <option v-for="c in store.connectors" :key="c.id" :value="c.id">{{ c.name }}</option>
        </select>
      </label>
      <label class="flex min-w-[240px] flex-1 items-center gap-3 text-sm">
        <span class="shrink-0 text-muted">{{ t("ctx.budget") }}</span>
        <input v-model.number="maxChars" type="range" min="1000" max="30000" step="500" class="flex-1 accent-accent" />
        <span class="w-16 text-right tabular-nums">{{ maxChars.toLocaleString("en") }}</span>
      </label>
    </div>

    <section v-if="result" class="card overflow-hidden">
      <div class="flex flex-wrap items-center justify-between gap-2 border-b border-sep py-2.5 pr-3 pl-4.5 text-[13px] text-muted">
        <span>{{ t("ctx.stats", { chars: result.chars, tokens: result.approx_tokens, n: result.memory_ids.length }) }}</span>
        <button class="btn btn-filled btn-sm" @click="copy(result.text)"><AppIcon name="copy" :size="14" />{{ t("common.copy") }}</button>
      </div>
      <pre class="max-h-[65vh] overflow-auto px-5 py-4.5 font-mono text-[13px] leading-[1.7] whitespace-pre-wrap">{{ result.text }}</pre>
      <div v-if="result.omitted" class="flex items-center gap-2 border-t border-sep bg-wait-soft px-4.5 py-2.5 text-[13px] text-wait-ink">
        <AppIcon name="info" :size="14" />{{ t("ctx.omitted", { n: result.omitted }) }}
      </div>
    </section>
  </div>
</template>
