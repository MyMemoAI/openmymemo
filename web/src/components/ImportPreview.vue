<script setup lang="ts">
import { computed } from "vue";
import { api, type ImportItem } from "../api";
import { t } from "../i18n";
import { act, importTopicTitle, store, topicsBySlug, topicTitle } from "../store";
import AppIcon from "./AppIcon.vue";

/**
 * The preview of an import: each item with its kind, date and duplicate flags, a checkbox, and Import / Cancel
 * (product-design.md §5.6). `imported` says how many landed in `from-<source>` (`n`) and how many in topics the
 * source named (`named`).
 */
const props = defineProps<{ source: string; items: ImportItem[] }>();
const emit = defineEmits<{ imported: [done: { slug: string; n: number; named: number }]; cancel: [] }>();

const chosen = computed(() => props.items.filter((i) => i.include).length);
/** Which path split the text: the OpenMyMemo Assistant, the rules when it was on but couldn't be used, or neither. */
const by = computed(() =>
  props.items.some((i) => i.by === "assistant") ? "assistant" : props.items.some((i) => i.by === "rules") ? "rules" : null,
);

async function commit() {
  const included = props.items.filter((i) => i.include);
  const named = included.filter((i) => i.topic && topicsBySlug.value[i.topic]).length;
  const res = await act(() => api.importCommit(props.source, props.items));
  if (res) emit("imported", { slug: `from-${props.source}`, n: res.memory_ids.length - named, named });
}
</script>

<template>
  <p class="text-[13px] text-muted">{{ t("imp.into", { topic: importTopicTitle(source) }) }}</p>
  <p v-if="by" class="flex items-center gap-2 text-[13px]" data-test="split-by">
    <AppIcon :name="by === 'assistant' ? 'star' : 'info'" :size="14" class="text-muted" />{{ t(by === "assistant" ? "imp.byAssistant" : "imp.byRules") }}
  </p>
  <div class="card divide-y divide-sep">
    <label v-for="(it, i) in items" :key="i" class="flex cursor-pointer items-start gap-3 px-4.5 py-3" :class="it.include ? '' : 'opacity-50'">
      <input v-model="it.include" type="checkbox" class="check mt-0.5" />
      <div class="min-w-0 flex-1">
        <div class="text-[15px] break-words">{{ it.content }}</div>
        <div class="mt-1 flex flex-wrap items-center gap-1.5 text-xs text-muted">
          <span v-if="it.flag === 'dropped'" class="chip font-semibold text-wait-ink">{{ t("imp.dropped") }}</span>
          <button
            v-if="it.topic && topicsBySlug[it.topic] && it.by === 'assistant'"
            type="button"
            class="chip"
            :title="t('imp.clearTopic', { topic: topicTitle(it.topic) })"
            :aria-label="t('imp.clearTopic', { topic: topicTitle(it.topic) })"
            @click.prevent="it.topic = null"
          ><AppIcon name="arrowRight" :size="11" />{{ topicTitle(it.topic) }}<AppIcon name="x" :size="11" /></button>
          <span v-else-if="it.topic && topicsBySlug[it.topic]" class="chip"><AppIcon name="arrowRight" :size="11" />{{ topicTitle(it.topic) }}</span>
          <select v-model="it.kind" class="chip cursor-pointer appearance-none border-0 outline-none [field-sizing:content]" :aria-label="t('mem.kinds')">
            <option v-for="k in store.state?.kinds" :key="k" :value="k">{{ t(`kind.${k}`) }}</option>
          </select>
          <span v-if="it.created_at">{{ it.created_at }}</span>
          <span v-if="it.hint" class="truncate">{{ it.hint }}</span>
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
    <button class="btn" @click="emit('cancel')">{{ t("common.cancel") }}</button>
  </div>
</template>
