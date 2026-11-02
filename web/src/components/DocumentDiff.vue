<script setup lang="ts">
import { computed, ref } from "vue";
import { diffCounts, diffLines, withContext } from "../diff";
import { t, tn } from "../i18n";

/** A change to a document as a line diff (product-design.md §23.4), long ones folded until "Show all". */
const props = defineProps<{ before: string; after: string }>();
const SHOWN = 40;

const lines = computed(() => diffLines(props.before, props.after));
const rows = computed(() => withContext(lines.value));
const counts = computed(() => diffCounts(lines.value));
const all = ref(false);
const visible = computed(() => (all.value ? rows.value : rows.value.slice(0, SHOWN)));
</script>

<template>
  <div class="space-y-1" data-test="doc-diff">
    <div class="text-[13px] text-muted">{{ t("doc.diffCounts", { added: counts.added, removed: counts.removed }) }}</div>
    <div class="space-y-0.5 rounded-[10px] bg-surface px-3 py-2.5 text-[14px] leading-relaxed shadow-[0_0_0_1px_var(--color-ring)]">
      <template v-for="(row, i) in visible" :key="i">
        <div v-if="row.kind === 'skip'" class="py-0.5 text-[13px] text-muted">{{ tn("doc.unchanged", row.count) }}</div>
        <div v-else class="flex gap-2.5" :class="row.kind === 'same' ? 'text-muted' : ''">
          <span v-if="row.kind === 'del'" class="minus" aria-hidden="true">−</span>
          <span v-else-if="row.kind === 'add'" class="plus" aria-hidden="true">+</span>
          <span v-else class="w-3 shrink-0" aria-hidden="true" />
          <span class="sr-only">{{ row.kind === "del" ? t("doc.removed") : row.kind === "add" ? t("doc.added") : "" }}</span>
          <span class="min-w-0 break-words whitespace-pre-wrap" :class="row.kind === 'del' ? 'line-through decoration-danger/40' : ''">{{ row.text || " " }}</span>
        </div>
      </template>
    </div>
    <button v-if="rows.length > SHOWN && !all" class="btn btn-link btn-sm" @click="all = true">
      {{ tn("doc.showAll", rows.length - SHOWN) }}
    </button>
  </div>
</template>
