<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { api, type AssistantStatus, type Kind, type TellApplied, type TellChange, type TellPreview } from "../api";
import { t, tn } from "../i18n";
import { act, store, toast, topicTitle } from "../store";
import AppIcon from "./AppIcon.vue";

/**
 * Tell OpenMyMemo (product-design.md §19): the person says what's new, the Assistant turns it into changes, and
 * nothing is saved until they tick and save them. Without the Assistant the server keeps the text by rules.
 */
const MAX = 4000;
const EXAMPLES = ["tell.example.move", "tell.example.coffee", "tell.example.name"];

type Row = TellChange & { tick: boolean };

const text = ref("");
const busy = ref(false);
const preview = ref<TellPreview | null>(null);
const rows = ref<Row[]>([]);
const done = ref<TellApplied | null>(null);
const assistant = ref<AssistantStatus | null>(null);
const example = EXAMPLES[new Date().getDate() % EXAMPLES.length];

onMounted(async () => {
  try {
    assistant.value = await api.assistant();
  } catch {
    assistant.value = null; // the box still works; it just can't name where the text goes
  }
});

/** Where what's typed goes on Preview, named whenever it leaves this computer (§19.1). */
const destination = computed(() => {
  const a = assistant.value;
  if (!a) return ""; // not known: say nothing rather than something that may be wrong
  if (!a.enabled || !a.ready || !a.features.tell) return t("tell.byRules");
  if (a.local) return t("tell.staysHere");
  return a.kind === "mymemo" ? t("tell.sentMymemo") : t("tell.sentTo", { host: a.host });
});

const targets = computed(() => (store.state?.topics ?? []).filter((tp) => tp.group !== "imported"));
const ticked = computed(() => rows.value.filter((r) => r.tick && (r.op === "archive" || (r.content ?? "").trim().length >= 3)));
const savedMemories = computed(() =>
  (done.value?.ids ?? []).map((id) => store.memories.find((m) => m.id === id)).filter((m) => !!m),
);

async function runPreview() {
  if (!text.value.trim() || busy.value) return;
  busy.value = true;
  done.value = null;
  try {
    const p = await api.tellPreview(text.value);
    preview.value = p;
    rows.value = p.changes.map((c) => ({ ...c, tick: true }));
  } catch (e) {
    toast(e instanceof Error ? e.message : String(e), "error");
  } finally {
    busy.value = false;
  }
}

function discard() {
  preview.value = null;
  rows.value = [];
}

async function save() {
  const changes = ticked.value.map(({ tick: _tick, current: _current, reason: _reason, ...c }) => c);
  if (!changes.length) return;
  busy.value = true;
  const res = await act(() => api.tellApply(changes));
  busy.value = false;
  if (!res) return;
  done.value = res;
  discard();
  if (!res.errors.length) text.value = "";
}

const summary = computed(() => {
  const d = done.value;
  if (!d) return "";
  const parts = [
    d.added ? tn("tell.added", d.added) : "",
    d.updated ? tn("tell.updated", d.updated) : "",
    d.archived ? tn("tell.archived", d.archived) : "",
  ].filter(Boolean);
  return parts.join(", ") || t("tell.nothingSaved");
});
</script>

<template>
  <section class="card space-y-3 p-5" data-test="tell">
    <div class="space-y-0.5">
      <h2 class="text-[15px] font-semibold">{{ t("tell.title") }}</h2>
      <p class="text-[13px] text-muted">{{ t("tell.sub") }}</p>
    </div>
    <textarea
      v-model="text"
      class="input min-h-20 w-full resize-y bg-surface shadow-[0_0_0_1px_var(--color-line)]"
      :maxlength="MAX"
      :placeholder="t('tell.placeholder', { example: t(example) })"
      :aria-label="t('tell.title')"
      :disabled="busy"
      @keydown.meta.enter="runPreview"
      @keydown.ctrl.enter="runPreview"
    />
    <div class="flex flex-wrap items-center justify-between gap-2">
      <p class="text-[13px] text-muted" data-test="tell-where">{{ destination }}</p>
      <div class="flex items-center gap-3">
        <span v-if="text.length > MAX * 0.8" class="text-[12px] text-muted tabular-nums">{{ text.length }} / {{ MAX }}</span>
        <button class="btn btn-primary" :disabled="busy || !text.trim()" data-test="tell-preview" @click="runPreview">
          {{ busy && !preview ? t("tell.thinking") : t("tell.preview") }}
        </button>
      </div>
    </div>

    <!-- the preview: nothing is saved until Save -->
    <div v-if="preview" class="space-y-3 border-t border-sep pt-3" data-test="tell-changes">
      <p v-if="preview.note" class="flex items-center gap-1.5 text-[13px]"><AppIcon name="info" :size="14" />{{ preview.note }}</p>
      <p v-if="!rows.length" class="text-sm text-muted">{{ t("tell.noChanges") }}</p>
      <ul v-else class="divide-y divide-sep rounded-xl shadow-[0_0_0_1px_var(--color-line)]">
        <li v-for="(r, i) in rows" :key="i" class="flex items-start gap-3 px-3.5 py-3" :class="r.tick ? '' : 'opacity-70'">
          <input v-model="r.tick" type="checkbox" class="check mt-1" :aria-label="t(`tell.op.${r.op}`)" />
          <div class="min-w-0 flex-1 space-y-1.5">
            <div class="flex flex-wrap items-center gap-1.5 text-xs text-muted">
              <span class="chip" :class="r.op === 'archive' ? '' : 'chip-accent'">{{ t(`tell.op.${r.op}`) }}</span>
              <select v-if="r.op === 'add'" v-model="r.topic" class="chip select cursor-pointer border-0 pr-6 outline-none"
                      :aria-label="t('tell.topic')">
                <option v-for="tp in targets" :key="tp.slug" :value="tp.slug">{{ tp.title }}</option>
              </select>
              <span v-else-if="r.topic" class="chip">{{ t("tell.moveTo", { topic: topicTitle(r.topic) }) }}</span>
              <span v-if="r.kind">{{ t(`kind.${r.kind as Kind}`) }}</span>
            </div>
            <p v-if="r.current" class="text-sm break-words" :class="r.op === 'update' ? 'text-muted line-through' : ''">
              {{ r.current }}
            </p>
            <textarea v-if="r.op !== 'archive'" v-model="r.content" rows="2" class="input w-full resize-y text-[15px]"
                      :aria-label="t('tell.edit')" />
            <p v-if="r.reason" class="text-[12px] text-muted">{{ r.reason }}</p>
          </div>
        </li>
      </ul>
      <p v-if="preview.skipped" class="text-[13px] text-muted">{{ t("tell.skipped", { what: preview.skipped }) }}</p>
      <p v-if="preview.private_skipped" class="flex items-center gap-1.5 text-[13px] text-muted">
        <AppIcon name="lock" :size="13" />{{ tn("tell.private", preview.private_skipped) }}
      </p>
      <div class="flex flex-wrap justify-end gap-2">
        <button class="btn" :disabled="busy" @click="discard">{{ t("common.cancel") }}</button>
        <button class="btn btn-primary" :disabled="busy || !ticked.length" data-test="tell-save" @click="save">
          {{ tn("tell.save", ticked.length) }}
        </button>
      </div>
    </div>

    <!-- what was saved -->
    <div v-if="done" class="space-y-1.5 border-t border-sep pt-3 text-sm" data-test="tell-done">
      <p class="font-medium">{{ summary }}</p>
      <ul class="space-y-0.5">
        <li v-for="m in savedMemories" :key="m.id" class="truncate">
          <a :href="`#/memories/${encodeURIComponent(m.topic)}`" class="hover:underline">{{ m.content }}</a>
          <span class="text-muted"> · {{ topicTitle(m.topic) }}</span>
        </li>
      </ul>
      <p v-for="(e, i) in done.errors" :key="i" class="text-[13px] text-danger-ink">{{ e.error }}</p>
      <button v-if="done.errors.length" class="btn btn-sm" @click="runPreview">{{ t("tell.again") }}</button>
    </div>
  </section>
</template>
