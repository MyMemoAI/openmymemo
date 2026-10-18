<script setup lang="ts">
import { computed, onBeforeUnmount, ref } from "vue";
import { api, type ChatJob, type ChatSource } from "../api";
import { t } from "../i18n";
import { toast } from "../store";
import AppIcon from "./AppIcon.vue";
import ImportPreview from "./ImportPreview.vue";

/**
 * Chat exports and agent sessions, read by the OpenMyMemo Assistant (product-design.md §17.5): choose what, give a
 * path in the home folder or choose a file, see what reading it costs, then read it newest first and tick through
 * the same preview as any import, each memory with the words it came from.
 */
const emit = defineEmits<{ imported: [done: { source: string; slug: string; n: number; named: number }] }>();

const SOURCES: ChatSource[] = ["chatgpt-export", "claude-export", "claude-code-sessions", "codex-sessions"];
const source = ref<ChatSource>("chatgpt-export");
const path = ref("");
const job = ref<ChatJob | null>(null);
const maxCalls = ref(0);
const busy = ref(false);
const picker = ref<HTMLInputElement | null>(null);
let timer: ReturnType<typeof setTimeout> | null = null;

const sessions = computed(() => source.value === "claude-code-sessions" || source.value === "codex-sessions");
const accept = computed(() => (sessions.value ? ".jsonl" : ".json,application/json"));
const fmt = (n: number) => n.toLocaleString("en-US");
const share = computed(() => (job.value && job.value.estimate.calls ? Math.min(1, maxCalls.value / job.value.estimate.calls) : 0));

function reset() {
  stop();
  if (job.value) void api.chatCancel(job.value.job).catch(() => undefined);
  job.value = null;
}

function pick(s: ChatSource) {
  reset();
  source.value = s;
  path.value = "";
}

async function plan(body: { path?: string; name?: string; text?: string }) {
  busy.value = true;
  try {
    const res = await api.chatPlan({ source: source.value, ...body });
    job.value = res;
    maxCalls.value = Math.min(res.estimate.calls, 50);
    if (!res.estimate.calls) toast(t("imp.chats.nothing"), "error");
  } catch (e) {
    toast(e instanceof Error ? e.message : String(e), "error");
  } finally {
    busy.value = false;
  }
}

function read() {
  const typed = path.value.trim();
  const fallback = source.value === "claude-code-sessions" ? "~/.claude/projects" : source.value === "codex-sessions" ? "~/.codex/sessions" : "";
  if (!typed && !fallback) {
    toast(t("imp.chats.needPath"), "error");
    return;
  }
  void plan({ path: typed || fallback });
}

async function chosen(e: Event) {
  const input = e.target as HTMLInputElement;
  const file = input.files?.[0];
  input.value = "";
  if (!file) return;
  if (file.size > 50_000_000) {
    toast(t("imp.chats.tooBig"), "error");
    return;
  }
  await plan({ name: file.name, text: await file.text() });
}

function stop() {
  if (timer) clearTimeout(timer);
  timer = null;
}

async function poll() {
  if (!job.value) return;
  try {
    job.value = await api.chatStatus(job.value.job);
  } catch (e) {
    toast(e instanceof Error ? e.message : String(e), "error");
    job.value = null;
    return;
  }
  if (job.value.state === "running") timer = setTimeout(poll, 1000);
  else if (job.value.state === "done" && job.value.error) toast(t("imp.chats.someFailed", { error: job.value.error }), "error");
}

async function start() {
  if (!job.value) return;
  busy.value = true;
  try {
    job.value = await api.chatStart(job.value.job, Math.max(1, Math.floor(maxCalls.value) || 1));
    timer = setTimeout(poll, 500);
  } catch (e) {
    toast(e instanceof Error ? e.message : String(e), "error");
  } finally {
    busy.value = false;
  }
}

function imported(done: { slug: string; n: number; named: number }) {
  const s = source.value;
  reset();
  emit("imported", { source: s, ...done });
}

onBeforeUnmount(stop);
</script>

<template>
  <div class="space-y-4">
    <div class="flex flex-wrap gap-1.5" role="radiogroup" :aria-label="t('imp.source')">
      <button
        v-for="s in SOURCES"
        :key="s"
        type="button"
        class="pill"
        :class="source === s ? 'pill-on' : 'pill-off'"
        role="radio"
        :aria-checked="source === s"
        :disabled="busy || job?.state === 'running'"
        @click="pick(s)"
      >{{ t(`imp.chats.src.${s}`) }}</button>
    </div>
    <p class="text-[13px] text-muted">{{ t(`imp.chats.help.${source}`) }}</p>

    <template v-if="!job">
      <div class="flex flex-wrap gap-2">
        <input
          v-model="path"
          class="input min-w-0 flex-1 font-mono text-xs"
          :placeholder="t(`imp.chats.pathHint.${source}`)"
          :aria-label="t('imp.files.path')"
          spellcheck="false"
          @keydown.enter="read"
        />
        <button class="btn btn-primary" :disabled="busy" @click="read">{{ t("imp.chats.read") }}</button>
        <button class="btn" :disabled="busy" @click="picker?.click()">{{ t("imp.files.choose") }}</button>
        <input ref="picker" type="file" class="hidden" :accept="accept" @change="chosen" />
      </div>
    </template>

    <template v-else-if="job.state === 'planned'">
      <div class="card space-y-3 p-4.5" data-test="chat-plan">
        <p class="font-semibold">
          {{ t("imp.chats.found", { conversations: fmt(job.conversations), messages: fmt(job.estimate.messages) }) }}
          <span v-if="job.oldest" class="font-normal text-muted">· {{ job.oldest }} – {{ job.newest }}</span>
        </p>
        <div v-if="!job.ready" class="notice notice-wait flex-wrap gap-2" data-test="chat-needs-assistant">
          <AppIcon name="info" :size="14" />{{ t("imp.chats.needsAssistant", { reason: job.reason ?? "" }) }}
        </div>
        <template v-else-if="job.estimate.calls">
          <p class="text-[13px]" data-test="chat-estimate">
            {{ t("imp.chats.cost", { calls: fmt(job.estimate.calls), tokens: fmt(job.estimate.tokens) }) }}
            {{ job.estimate.remote ? t("imp.chats.remote", { host: job.estimate.host ?? "" }) : t("imp.chats.local") }}
          </p>
          <label class="flex flex-wrap items-center gap-2 text-sm">
            {{ t("imp.chats.newest") }}
            <input v-model.number="maxCalls" type="number" min="1" :max="job.estimate.calls" class="input w-24 text-sm" />
            {{ t("imp.chats.ofCalls", { n: fmt(job.estimate.calls), tokens: fmt(Math.round(job.estimate.tokens * share)) }) }}
          </label>
          <p v-if="job.estimate.calls_left != null && maxCalls > job.estimate.calls_left" class="text-[13px] text-wait-ink">
            {{ t("imp.chats.capLeft", { n: fmt(job.estimate.calls_left) }) }}
          </p>
        </template>
        <div class="flex gap-2">
          <button v-if="job.ready && job.estimate.calls" class="btn btn-primary" :disabled="busy || maxCalls < 1" @click="start">{{ t("imp.chats.start") }}</button>
          <button class="btn" @click="reset">{{ t("common.cancel") }}</button>
        </div>
      </div>
    </template>

    <template v-else-if="job.state === 'running'">
      <div class="card space-y-3 p-4.5" data-test="chat-running">
        <p class="text-sm">{{ t("imp.chats.reading", { done: job.done, total: job.total }) }}</p>
        <progress class="w-full" :value="job.done" :max="job.total" />
        <button class="btn btn-sm" @click="reset">{{ t("imp.chats.stop") }}</button>
      </div>
    </template>

    <template v-else-if="job.state === 'done' && job.items?.length">
      <ImportPreview :source="source" :items="job.items" @imported="imported" @cancel="reset" />
    </template>

    <template v-else>
      <div class="card space-y-3 p-4.5">
        <p class="text-sm">{{ job.error || t("imp.chats.none") }}</p>
        <button class="btn btn-sm" @click="reset">{{ t("imp.more") }}</button>
      </div>
    </template>
  </div>
</template>
