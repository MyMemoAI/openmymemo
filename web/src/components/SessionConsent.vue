<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { api, type AssistantStatus, type SessionApp } from "../api";
import { t } from "../i18n";
import { toast } from "../store";

/**
 * Whether the OpenMyMemo Assistant may read an app's sessions when they end (product-design.md §17.7): asked per
 * app. `banner` (the Changes page) shows only the apps that asked and haven't been answered, and fetches the status
 * itself; otherwise (Settings → Assistant, under "Reading sessions") every app, from `status`.
 */
const props = defineProps<{ status?: AssistantStatus | null; banner?: boolean }>();
const emit = defineEmits<{ changed: [AssistantStatus] }>();

const APPS: { id: SessionApp; name: string }[] = [
  { id: "claude-code", name: "Claude Code" },
  { id: "codex", name: "Codex" },
];
const own = ref<AssistantStatus | null>(null);
const busy = ref(false);

onMounted(async () => {
  if (!props.banner) return;
  try {
    own.value = await api.assistant();
  } catch {
    own.value = null;
  }
});

const current = computed(() => (props.banner ? own.value : props.status ?? null));
const rows = computed(() => {
  const s = current.value;
  if (!s || !s.enabled || !s.features.session || !s.session_apps) return [];
  return APPS.filter((a) => !props.banner || s.session_apps[a.id] === "asked").map((a) => ({ ...a, answer: s.session_apps[a.id] }));
});

async function answer(app: SessionApp, allow: boolean) {
  busy.value = true;
  try {
    const s = await api.assistantSessions(app, allow);
    own.value = s;
    emit("changed", s);
  } catch (e) {
    toast(e instanceof Error ? e.message : String(e), "error");
  } finally {
    busy.value = false;
  }
}
</script>

<template>
  <div v-if="banner && rows.length" class="card space-y-2 p-4.5" data-test="session-ask">
    <div v-for="r in rows" :key="r.id" class="space-y-2">
      <span class="block font-semibold">{{ t("ses.askTitle", { app: r.name }) }}</span>
      <span class="block text-[13px] text-muted">{{ t("ses.askBody", { app: r.name }) }}</span>
      <span class="flex flex-wrap gap-2 pt-1">
        <button type="button" class="btn btn-primary btn-sm" :disabled="busy" @click="answer(r.id, true)">{{ t("ses.allow") }}</button>
        <button type="button" class="btn btn-sm" :disabled="busy" @click="answer(r.id, false)">{{ t("ses.decline") }}</button>
      </span>
    </div>
  </div>
  <div v-else-if="!banner && rows.length" class="space-y-2 px-4.5 pb-3" data-test="session-apps">
    <p class="text-[13px] text-muted">{{ t("ses.help") }}</p>
    <div v-for="r in rows" :key="r.id" class="flex flex-wrap items-center gap-2 text-sm">
      <span class="min-w-28 font-semibold">{{ r.name }}</span>
      <span class="text-[13px] text-muted">{{ t(`ses.answer.${r.answer ?? "none"}`) }}</span>
      <span class="ml-auto flex gap-1.5">
        <button type="button" class="btn btn-sm" :class="{ 'btn-primary': r.answer === 'asked' }" :disabled="busy || r.answer === 'allowed'" @click="answer(r.id, true)">
          {{ t("ses.allow") }}
        </button>
        <button type="button" class="btn btn-ghost btn-sm" :disabled="busy || r.answer === 'declined'" @click="answer(r.id, false)">
          {{ t("ses.decline") }}
        </button>
      </span>
    </div>
  </div>
</template>
