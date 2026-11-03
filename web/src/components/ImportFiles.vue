<script setup lang="ts">
import { ref } from "vue";
import { api, type FilePreview, type MemoryFiles } from "../api";
import { t, tn } from "../i18n";
import { act, toast } from "../store";
import ImportPreview from "./ImportPreview.vue";

/**
 * Import another AI app's memory file (CLAUDE.md, AGENTS.md, GEMINI.md, rules, memory.md, …): find them in a
 * folder (the home folder by default), or choose one, then the same preview as a paste. `imported` also says
 * which source the file read as, since that names the topic it went into. A file can also come in whole, as one
 * document (product-design.md §23.4), which is the first offer for instructions files.
 */
const emit = defineEmits<{ imported: [done: { source: string; slug: string; n: number; named: number }] }>();

const path = ref("");
const found = ref<MemoryFiles | null>(null);
const preview = ref<FilePreview | null>(null);
const busy = ref(false);
const picker = ref<HTMLInputElement | null>(null);

async function run<T>(work: () => Promise<T>): Promise<T | undefined> {
  busy.value = true;
  try {
    return await work();
  } catch (e) {
    toast(e instanceof Error ? e.message : String(e), "error");
    return undefined;
  } finally {
    busy.value = false;
  }
}

async function find() {
  const res = await run(() => api.importFiles(path.value.trim()));
  if (res) found.value = res;
}

const whole = ref(false);
const docTitle = ref("");

function show(res: FilePreview | undefined) {
  if (!res) return;
  if (!res.items.length && !res.document) {
    toast(t("imp.nothing"), "error");
    return;
  }
  preview.value = res;
  whole.value = !!res.document && (res.document.prefer || !res.items.length);
  docTitle.value = res.document?.title ?? "";
}

async function importWhole() {
  const offer = preview.value?.document;
  if (!preview.value || !offer || !docTitle.value.trim()) return;
  const source = preview.value.source;
  const d = await act(() => api.createDocument({ title: docTitle.value.trim(), content: offer.content,
                                                 description: offer.description, source }),
                      t("imp.doc.done", { title: docTitle.value.trim() }));
  if (d) {
    preview.value = null;
    location.hash = `#/documents/${encodeURIComponent(d.slug)}`;
  }
}

async function open(filePath: string) {
  show(await run(() => api.importFile({ path: filePath })));
}

async function chosen(e: Event) {
  const input = e.target as HTMLInputElement;
  const file = input.files?.[0];
  input.value = ""; // choosing the same file again still fires
  if (!file) return;
  show(await run(async () => api.importFile({ name: file.name, text: await file.text() })));
}

/** ~/… for files in the home folder. */
function short(p: string): string {
  const home = found.value?.home;
  return home && (p === home || p.startsWith(`${home}/`)) ? `~${p.slice(home.length)}` : p;
}

function imported(done: { slug: string; n: number; named: number }) {
  const source = preview.value!.source;
  preview.value = null;
  emit("imported", { source, ...done });
}
</script>

<template>
  <template v-if="!preview">
    <div class="flex flex-wrap gap-2">
      <input
        v-model="path"
        class="input min-w-48 flex-1 font-mono text-xs"
        :placeholder="t('imp.files.pathHint')"
        :aria-label="t('imp.files.path')"
        spellcheck="false"
        @keydown.enter="find"
      />
      <button class="btn btn-primary" :disabled="busy" @click="find">{{ t("imp.files.find") }}</button>
      <button class="btn" :disabled="busy" @click="picker?.click()">{{ t("imp.files.choose") }}</button>
      <input ref="picker" type="file" class="hidden" accept=".md,.mdc,.txt,.json,text/markdown,text/plain,application/json" @change="chosen" />
    </div>
    <template v-if="found">
      <p v-if="!found.files.length" class="text-[13px] text-muted">{{ t("imp.files.none") }}</p>
      <template v-else>
        <p class="text-[13px] text-muted">{{ tn("imp.files.found", found.files.length, { path: short(found.path) }) }}</p>
        <div class="card divide-y divide-sep">
          <div v-for="f in found.files" :key="f.path" class="flex items-center gap-3 px-4.5 py-3">
            <div class="min-w-0 flex-1">
              <div class="truncate font-semibold">{{ f.name }} <span class="font-normal text-muted">· {{ f.app }}</span></div>
              <div class="truncate font-mono text-xs text-muted" :title="f.path">{{ short(f.path) }}</div>
            </div>
            <button class="btn btn-sm" :disabled="busy" @click="open(f.path)">{{ t("imp.files.open") }}</button>
          </div>
        </div>
      </template>
    </template>
  </template>
  <template v-else>
    <p class="text-sm font-semibold">{{ t("imp.files.from", { name: preview.name }) }}</p>
    <div v-if="preview.document && preview.items.length" class="seg" role="radiogroup" :aria-label="t('imp.doc.how')">
      <button class="seg-item" :class="{ 'is-on': whole }" role="radio" :aria-checked="whole" data-test="imp-whole" @click="whole = true">
        {{ t("imp.doc.whole") }}
      </button>
      <button class="seg-item" :class="{ 'is-on': !whole }" role="radio" :aria-checked="!whole" data-test="imp-split" @click="whole = false">
        {{ t("imp.doc.split") }}
      </button>
    </div>
    <template v-if="whole && preview.document">
      <p class="text-[13px] text-muted">{{ t("imp.doc.help") }}</p>
      <input v-model="docTitle" class="input" :aria-label="t('doc.title')" data-test="imp-doc-title" />
      <div class="card max-h-80 overflow-auto px-4.5 py-3 text-[14px] leading-relaxed break-words whitespace-pre-wrap">{{ preview.document.content }}</div>
      <div class="flex justify-end gap-2">
        <button class="btn" @click="preview = null">{{ t("common.cancel") }}</button>
        <button class="btn btn-primary" :disabled="busy || !docTitle.trim()" data-test="imp-doc" @click="importWhole">
          {{ t("imp.doc.import") }}
        </button>
      </div>
    </template>
    <ImportPreview v-else :source="preview.source" :items="preview.items" @imported="imported" @cancel="preview = null" />
  </template>
</template>
