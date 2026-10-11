<script setup lang="ts">
import { onMounted, ref } from "vue";
import { api, type HistoryRow, type Memory } from "../api";
import { relTime, sourceLabel, t } from "../i18n";
import { act, connectorName, topicTitle } from "../store";
import ModalDialog from "./ModalDialog.vue";

const props = defineProps<{ memory: Memory }>();
const emit = defineEmits<{ close: [] }>();
const rows = ref<HistoryRow[]>([]);

async function load() {
  rows.value = await api.history(props.memory.id);
}
onMounted(load);

function actor(a: string) {
  if (a.startsWith("connector:")) return connectorName(a.slice(10));
  return sourceLabel(a);
}

async function rollback(row: HistoryRow) {
  await act(() => api.rollback(props.memory.id, row.version));
  await load();
}
</script>

<template>
  <ModalDialog :title="t('hist.title')" @close="emit('close')">
    <ol class="space-y-4">
      <li v-for="(row, i) in rows" :key="`${row.version}-${row.at}`" class="flex gap-3">
        <div class="mt-1.5 size-2 shrink-0 rounded-full" :class="i === 0 ? 'bg-accent' : 'bg-fill-strong'" />
        <div class="min-w-0 flex-1">
          <div class="text-xs text-muted">
            v{{ row.version }} · {{ t(`hist.op.${row.op}`) }} · {{ t("hist.by", { actor: actor(row.actor) }) }} · {{ relTime(row.at) }}
            <span v-if="row.op === 'move'"> · {{ topicTitle(row.topic) }}</span>
          </div>
          <div class="mt-0.5 text-[15px] break-words" :class="row.op === 'archive' ? 'text-muted line-through' : ''">{{ row.content }}</div>
          <button
            v-if="i > 0 && row.op !== 'archive'"
            class="btn btn-link btn-sm mt-0.5 -ml-1"
            @click="rollback(row)"
          >{{ t("hist.rollback") }}</button>
        </div>
      </li>
    </ol>
  </ModalDialog>
</template>
