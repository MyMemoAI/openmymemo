<script setup lang="ts">
import { ref } from "vue";
import { api, type FilePreview, type MemoryFiles } from "../api";
import { t, tn } from "../i18n";
import { toast } from "../store";
import ImportPreview from "./ImportPreview.vue";

/**
 * Import another AI app's memory file (CLAUDE.md, AGENTS.md, GEMINI.md, rules, memory.md, …): find them in a
 * folder (the home folder by default), or choose one, then the same preview as a paste. `imported` also says
 * which source the file read as, since that names the topic it went into.
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

function show(res: FilePreview | undefined) {
  if (!res) return;
  if (res.items.length) preview.value = res;
  else toast(t("imp.nothing"), "error");
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
        class="input min-w-0 flex-1 font-mono text-xs"
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
    <ImportPreview :source="preview.source" :items="preview.items" @imported="imported" @cancel="preview = null" />
  </template>
</template>
