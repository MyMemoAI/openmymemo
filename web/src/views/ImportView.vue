<script setup lang="ts">
import { ref } from "vue";
import { api } from "../api";
import AppIcon from "../components/AppIcon.vue";
import ImportChats from "../components/ImportChats.vue";
import ImportFiles from "../components/ImportFiles.vue";
import ImportPaste from "../components/ImportPaste.vue";
import PageHeader from "../components/PageHeader.vue";
import SortDialog from "../components/SortDialog.vue";
import { t, tn } from "../i18n";
import { copy, importTopicTitle, topicsBySlug, topicTitle } from "../store";

const SOURCES = ["chatgpt", "claude", "gemini", "files", "markdown", "json", "chats"] as const;
const source = ref<(typeof SOURCES)[number]>("chatgpt");
/** After an import: its source (a file names its own), how many landed in the from-<source> topic, and how many
 * in topics the source named. */
const done = ref<{ source: string; slug: string; n: number; named: number } | null>(null);
const sorting = ref<string | null>(null);

const exportFormat = ref<"markdown" | "claude" | "chatgpt" | "json">("claude");
const includePrivate = ref(false);
const exported = ref("");

function pickSource(s: (typeof SOURCES)[number]) {
  source.value = s;
  done.value = null;
}

async function runExport() {
  exported.value = await api.exportText(exportFormat.value, includePrivate.value);
}

function download() {
  const ext = exportFormat.value === "json" ? "json" : exportFormat.value === "markdown" ? "md" : "txt";
  const url = URL.createObjectURL(new Blob([exported.value], { type: "text/plain;charset=utf-8" }));
  const a = Object.assign(document.createElement("a"), { href: url, download: `openmymemo-${exportFormat.value}.${ext}` });
  a.click();
  URL.revokeObjectURL(url);
}
</script>

<template>
  <div class="mx-auto max-w-[820px] space-y-8">
    <PageHeader :title="t('nav.import')" :subtitle="t('imp.subtitle')" />

    <section class="space-y-4">
      <h2 class="section-title">{{ t("imp.title") }}</h2>
      <div class="seg" role="radiogroup" :aria-label="t('imp.source')">
        <button
          v-for="s in SOURCES"
          :key="s"
          class="seg-item"
          :class="{ 'is-on': source === s }"
          role="radio"
          :aria-checked="source === s"
          @click="pickSource(s)"
        >{{ t(`imp.src.${s}`) }}</button>
      </div>

      <div v-if="done" class="card space-y-3 p-4.5">
        <div class="flex gap-3">
          <AppIcon name="changes" :size="22" class="mt-px text-fresh" />
          <div class="space-y-0.5">
            <p class="font-semibold">{{ tn("imp.done", done.n, { topic: importTopicTitle(done.source) }) }}</p>
            <p v-if="done.named" class="text-[13px] text-muted">{{ tn("imp.doneNamed", done.named) }}</p>
            <p v-if="done.n" class="text-[13px] text-muted">{{ t("imp.doneHint") }}</p>
          </div>
        </div>
        <div class="flex flex-wrap gap-2 pl-[34px]">
          <button v-if="done.n && topicsBySlug[done.slug]" class="btn btn-primary btn-sm" @click="sorting = done.slug">{{ t("mem.sort") }}</button>
          <a v-if="topicsBySlug[done.slug]" :href="`#/memories/${encodeURIComponent(done.slug)}`" class="btn btn-sm">
            {{ t("imp.open", { topic: topicTitle(done.slug) }) }}
          </a>
          <button class="btn btn-ghost btn-sm" @click="done = null">{{ t("imp.more") }}</button>
        </div>
      </div>

      <template v-else>
        <p class="text-[13px] text-muted">{{ t(`imp.help.${source}`) }}</p>
        <ImportFiles v-if="source === 'files'" @imported="done = $event" />
        <ImportChats v-else-if="source === 'chats'" @imported="done = $event" />
        <ImportPaste v-else :source="source" @imported="done = { source, ...$event }" />
      </template>
      <SortDialog v-if="sorting" :topic="sorting" @close="sorting = null" />
    </section>

    <section class="space-y-4 border-t border-sep pt-8">
      <h2 class="section-title">{{ t("exp.title") }}</h2>
      <p class="text-[13px] text-muted">{{ t("exp.hint") }}</p>
      <div class="flex flex-wrap items-center gap-3">
        <select v-model="exportFormat" class="input select w-auto font-medium" :aria-label="t('exp.title')">
          <option v-for="f in (['claude', 'chatgpt', 'markdown', 'json'] as const)" :key="f" :value="f">{{ t(`exp.${f}`) }}</option>
        </select>
        <label class="flex items-center gap-2 text-sm">
          <input v-model="includePrivate" type="checkbox" class="check" /> {{ t("exp.includePrivate") }}
        </label>
        <button class="btn" @click="runExport">{{ t("imp.preview") }}</button>
      </div>
      <div v-if="exported" class="space-y-2">
        <pre class="card max-h-80 overflow-auto p-4.5 font-mono text-xs leading-relaxed whitespace-pre-wrap">{{ exported }}</pre>
        <div class="flex gap-2">
          <button class="btn btn-sm" @click="copy(exported)"><AppIcon name="copy" :size="14" />{{ t("exp.copy") }}</button>
          <button class="btn btn-sm" @click="download"><AppIcon name="download" :size="14" />{{ t("exp.download") }}</button>
        </div>
      </div>
    </section>
  </div>
</template>
