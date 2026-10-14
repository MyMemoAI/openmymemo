<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { api, type ImportItem } from "../api";
import { t } from "../i18n";
import { act, importTopicTitle, store, toast, topicsBySlug, topicTitle } from "../store";
import AppIcon from "./AppIcon.vue";

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

const chosen = computed(() => items.value?.filter((i) => i.include).length ?? 0);

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

async function commit() {
  if (!items.value) return;
  const included = items.value.filter((i) => i.include);
  const named = included.filter((i) => i.topic && topicsBySlug.value[i.topic]).length;
  const slug = `from-${props.source}`;
  const res = await act(() => api.importCommit(props.source, items.value!));
  if (res) {
    items.value = null;
    text.value = "";
    emit("imported", { slug, n: res.memory_ids.length - named, named });
  }
}
</script>

<template>
  <template v-if="!items">
    <textarea v-model="text" class="input min-h-48 bg-surface font-mono text-xs shadow-[0_0_0_1px_var(--color-line)]" :placeholder="t('imp.paste')" :aria-label="t('imp.paste')" />
    <button class="btn btn-primary" :disabled="!text.trim() || busy" @click="preview">{{ t("imp.preview") }}</button>
  </template>

  <template v-else>
    <p class="text-[13px] text-muted">{{ t("imp.into", { topic: importTopicTitle(source) }) }}</p>
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
</template>
