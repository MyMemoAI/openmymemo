<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { api, type SortRow } from "../api";
import { t, tn } from "../i18n";
import { act, store, toast, topicTitle } from "../store";
import AppIcon from "./AppIcon.vue";
import ModalDialog from "./ModalDialog.vue";

/** File imported memories into topics: a suggestion per memory, and only the ticked ones move. */
const props = defineProps<{ topic: string | null }>();
const emit = defineEmits<{ close: [] }>();

const SURE_AT = 0.5; // a suggestion at least this confident starts ticked (same as `openmymemo sort --yes`)

type Row = SortRow & { pick: string; tick: boolean };
const rows = ref<Row[] | null>(null);
const busy = ref(false);

const targets = computed(() => (store.state?.topics ?? []).filter((tp) => tp.group !== "imported"));
const ticked = computed(() => (rows.value ?? []).filter((r) => r.tick && r.pick));
const suggested = computed(() => (rows.value ?? []).filter((r) => r.pick));
const allTicked = computed(() => suggested.value.length > 0 && suggested.value.every((r) => r.tick));

async function load() {
  try {
    const plan = await api.sortPlan(props.topic);
    rows.value = plan.map((r) => ({ ...r, pick: r.topic ?? "", tick: !!r.topic && r.confidence >= SURE_AT }));
  } catch (e) {
    toast(e instanceof Error ? e.message : String(e), "error");
    rows.value = [];
  }
}
onMounted(load);

function onPick(row: Row) {
  row.tick = !!row.pick;
}

function toggleAll() {
  const tick = !allTicked.value;
  for (const r of rows.value ?? []) r.tick = tick && !!r.pick;
}

async function move() {
  const moves = ticked.value.map((r) => ({ memory_id: r.memory_id, topic: r.pick }));
  if (!moves.length) return;
  busy.value = true;
  const res = await act(() => api.sortMove(moves));
  busy.value = false;
  if (res) {
    toast(tn("sort.moved", res.moved));
    await load();
  }
}
</script>

<template>
  <ModalDialog :title="topic ? `${t('sort.title')} · ${topicTitle(topic)}` : t('sort.title')" wide @close="emit('close')">
    <div v-if="rows === null" class="py-10 text-center text-sm text-muted">{{ t("common.loading") }}</div>
    <div v-else-if="!rows.length" class="py-10 text-center">
      <p class="font-semibold">{{ t("sort.empty") }}</p>
      <p class="mt-1 text-[13px] text-muted">{{ t("sort.emptyHint") }}</p>
    </div>
    <template v-else>
      <div class="mb-3 flex flex-wrap items-center justify-between gap-2">
        <p class="text-[13px] text-muted">{{ t("sort.explain") }}</p>
        <button v-if="suggested.length" class="btn btn-link btn-sm" @click="toggleAll">
          {{ allTicked ? t("sort.untickAll") : t("sort.tickAll") }}
        </button>
      </div>
      <ul class="divide-y divide-sep rounded-xl shadow-[0_0_0_1px_var(--color-line)]">
        <li v-for="r in rows" :key="r.memory_id" class="flex items-start gap-3 px-3.5 py-3" :class="r.tick ? '' : 'opacity-70'">
          <input
            v-model="r.tick"
            type="checkbox"
            class="check mt-0.5"
            :disabled="!r.pick"
            :aria-label="r.content"
          />
          <div class="min-w-0 flex-1">
            <div class="text-[15px] break-words">{{ r.content }}</div>
            <div class="mt-1.5 flex flex-wrap items-center gap-1.5 text-xs text-muted">
              <span v-if="!topic" class="chip">{{ topicTitle(r.from_topic) }}</span>
              <AppIcon name="arrowRight" :size="13" />
              <select
                v-model="r.pick"
                class="chip select cursor-pointer border-0 pr-6 outline-none"
                :class="r.pick ? 'chip-accent' : ''"
                :aria-label="t('sort.choose')"
                @change="onPick(r)"
              >
                <option value="">{{ t("sort.choose") }}</option>
                <option v-for="tp in targets" :key="tp.slug" :value="tp.slug">{{ tp.title }}</option>
              </select>
              <span v-if="!r.topic">{{ t("sort.none") }}</span>
            </div>
          </div>
        </li>
      </ul>
      <div class="sticky bottom-0 -mx-5.5 -mb-5.5 mt-4 flex flex-wrap justify-end gap-2 rounded-b-[18px] border-t border-sep bg-sunken px-5.5 py-3.5">
        <button class="btn" @click="emit('close')">{{ t("common.close") }}</button>
        <button class="btn btn-primary" :disabled="busy || !ticked.length" @click="move">{{ tn("sort.move", ticked.length) }}</button>
      </div>
    </template>
  </ModalDialog>
</template>
