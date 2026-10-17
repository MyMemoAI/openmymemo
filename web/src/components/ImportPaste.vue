<script setup lang="ts">
import { ref, watch } from "vue";
import { api, type ImportItem } from "../api";
import { t } from "../i18n";
import { toast } from "../store";
import ImportPreview from "./ImportPreview.vue";

/**
 * Paste, preview with duplicates and near-duplicates flagged, import into `from-<source>` (product-design.md
 * §5.6). Used by the Import page and the first-run guide; `imported` says how many landed in `from-<source>`
 * (`n`) and how many in topics the source named (`named`).
 */
const props = defineProps<{ source: string }>();
const emit = defineEmits<{ imported: [done: { slug: string; n: number; named: number }] }>();

const text = ref("");
const items = ref<ImportItem[] | null>(null);
const busy = ref(false);

watch(() => props.source, () => (items.value = null));

async function preview() {
  busy.value = true;
  try {
    const found = await api.importPreview(props.source, text.value);
    if (found.length) items.value = found;
    else toast(t("imp.nothing"), "error"); // the paste box stays, with the text in it
  } catch (e) {
    toast(e instanceof Error ? e.message : String(e), "error");
  } finally {
    busy.value = false;
  }
}

function imported(done: { slug: string; n: number; named: number }) {
  items.value = null;
  text.value = "";
  emit("imported", done);
}
</script>

<template>
  <template v-if="!items">
    <textarea v-model="text" class="input min-h-48 bg-surface font-mono text-xs shadow-[0_0_0_1px_var(--color-line)]" :placeholder="t('imp.paste')" :aria-label="t('imp.paste')" />
    <button class="btn btn-primary" :disabled="!text.trim() || busy" @click="preview">{{ t("imp.preview") }}</button>
  </template>
  <ImportPreview v-else :source="source" :items="items" @imported="imported" @cancel="items = null" />
</template>
