<script setup lang="ts">
import { computed, ref } from "vue";
import { api, type ImportItem } from "../api";
import AppIcon from "../components/AppIcon.vue";
import PageHeader from "../components/PageHeader.vue";
import SortDialog from "../components/SortDialog.vue";
import { prettyName, t, tn } from "../i18n";
import { act, copy, store, toast, topicsBySlug, topicTitle } from "../store";

const SOURCES = ["chatgpt", "claude", "gemini", "markdown", "json"] as const;
const source = ref<(typeof SOURCES)[number]>("chatgpt");
const text = ref("");
const items = ref<ImportItem[] | null>(null);
const busy = ref(false);
/** After an import: how many landed in the from-<source> topic, and how many in topics the source named. */
const done = ref<{ slug: string; n: number; named: number } | null>(null);
const sorting = ref<string | null>(null);

const exportFormat = ref<"markdown" | "claude" | "chatgpt" | "json">("claude");
const includePrivate = ref(false);
const exported = ref("");

const chosen = computed(() => items.value?.filter((i) => i.include).length ?? 0);
/** Imports land in `from-<source>` ("From ChatGPT"), created on the first import from that source. */
const importSlug = computed(() => `from-${source.value}`);
const importTitle = (slug: string, src: string = source.value) =>
  topicsBySlug.value[slug]?.title ?? `From ${prettyName(src)}`;

async function preview() {
  busy.value = true;
  try {
    items.value = await api.importPreview(source.value, text.value);
    if (!items.value.length) toast(t("imp.nothing"), "error");
  } catch (e) {
    toast(e instanceof Error ? e.message : String(e), "error");
  } finally {
    busy.value = false;
  }
}

async function commit() {
  if (!items.value) return;
  const included = items.value.filter((i) => i.include);
  const named = included.filter((i) => i.topic && topicsBySlug.value[i.topic]).length;
  const slug = importSlug.value;
  const res = await act(() => api.importCommit(source.value, items.value!));
  if (res) {
    done.value = { slug, n: res.memory_ids.length - named, named };
    items.value = null;
    text.value = "";
  }
}

function pickSource(s: (typeof SOURCES)[number]) {
  source.value = s;
  items.value = null;
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
            <p class="font-semibold">{{ tn("imp.done", done.n, { topic: importTitle(done.slug) }) }}</p>
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
        <template v-if="!items">
          <textarea v-model="text" class="input min-h-48 bg-surface font-mono text-xs shadow-[0_0_0_1px_var(--color-line)]" :placeholder="t('imp.paste')" :aria-label="t('imp.paste')" />
          <button class="btn btn-primary" :disabled="!text.trim() || busy" @click="preview">{{ t("imp.preview") }}</button>
        </template>
      </template>

      <template v-if="items && items.length">
        <p class="text-[13px] text-muted">{{ t("imp.into", { topic: importTitle(importSlug) }) }}</p>
        <div class="card divide-y divide-sep">
          <label v-for="(it, i) in items" :key="i" class="flex cursor-pointer items-start gap-3 px-4.5 py-3" :class="it.include ? '' : 'opacity-50'">
            <input v-model="it.include" type="checkbox" class="check mt-0.5" />
            <div class="min-w-0 flex-1">
              <div class="text-[15px] break-words">{{ it.content }}</div>
              <div class="mt-1 flex flex-wrap items-center gap-1.5 text-xs text-muted">
                <span v-if="it.topic && topicsBySlug[it.topic]" class="chip"><AppIcon name="arrowRight" :size="11" />{{ topicTitle(it.topic) }}</span>
                <select v-model="it.kind" class="chip cursor-pointer appearance-none border-0 outline-none [field-sizing:content]" :aria-label="t('mem.kinds')">
                  <option v-for="k in store.state?.kinds" :key="k" :value="k">{{ t(`kind.${k}`) }}</option>
                </select>
                <span v-if="it.created_at">{{ it.created_at }}</span>
                <span v-if="it.duplicate_of" class="chip font-semibold">{{ t("imp.duplicate") }}</span>
                <span v-else-if="it.similar_content" class="truncate text-wait-ink">
                  {{ t("imp.similar", { text: it.similar_content }) }}
                </span>
              </div>
            </div>
          </label>
        </div>
        <div class="flex gap-2">
          <button class="btn btn-primary" :disabled="!chosen" @click="commit">{{ t("imp.commit", { n: chosen }) }}</button>
          <button class="btn" @click="items = null">{{ t("common.cancel") }}</button>
        </div>
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
