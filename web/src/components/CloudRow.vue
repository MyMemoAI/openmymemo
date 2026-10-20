<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { api, type CloudApp, type CloudStatus, type Connector } from "../api";
import { desktop } from "../desktop";
import { relTime, t, tn } from "../i18n";
import { copy, refresh, store, toast } from "../store";
import AppIcon from "./AppIcon.vue";

/**
 * OpenMyMemo Cloud (product-design.md §16): sign in to link this computer, its status, what to paste into ChatGPT and
 * claude.ai, how many changes wait in its offline queue (from the service's `welcome`, counted down as they are
 * filed; only once this OpenMyMemo takes the queue, `queue_enabled`, which it doesn't since the launch), and the web apps using it, each a normal connector row (`manage` shows a row's permissions, as in
 * ConnectApps). Polls /api/cloud while it is on screen.
 */
const props = defineProps<{ open?: string | null }>();
const emit = defineEmits<{ manage: [id: string | null] }>();
defineSlots<{ manage?: (scope: { app: null; connector: Connector }) => unknown }>();

const status = ref<CloudStatus | null>(null);
const failed = ref(false);
const busy = ref(false);
/** The sign-in page this panel opened, offered again while the browser hasn't come back. */
const signInUrl = ref<string | null>(null);
const showSetup = ref(false);

/** Take a status; a link made or undone (here, from the CLI, or by MyMemo) tells Settings and the catalog. */
function take(data: CloudStatus) {
  const was = status.value?.linked;
  status.value = data;
  if (was !== undefined && was !== data.linked) store.cloudVersion++;
}

let seq = 0;
async function load() {
  const mine = ++seq;
  try {
    const data = await api.cloud();
    if (mine !== seq) return;
    const wasLinked = status.value?.linked;
    take(data);
    failed.value = false;
    if (data.linked && wasLinked === false) {
      // Just signed in: say so, and show what to paste into the web apps.
      signInUrl.value = null;
      showSetup.value = true;
      toast(t("cloud.linked"));
    }
  } catch {
    if (mine === seq) failed.value = !status.value;
  }
}

let timer: number | undefined;
onMounted(() => {
  load();
  timer = window.setInterval(load, 3000);
});
onBeforeUnmount(() => window.clearInterval(timer));

const s = computed(() => status.value);
const linkedHere = computed(() => !!s.value?.linked && s.value.this_folder);

/** The offline queue, only when this OpenMyMemo takes it and the plan has it; never at launch. */
const queueOn = (c: CloudStatus | null | undefined) => !!c?.queue_enabled && !!c.plan?.queue;

/** "Ultra · live relay" (" + offline queue" with queueOn). A tier the panel doesn't know yet shows as the service
 * names it. */
function planLabel(c: CloudStatus): string {
  const key = c.plan?.tier ? `cloud.tier.${c.plan.tier}` : "";
  const tier = key && t(key) !== key ? t(key) : (c.plan?.tier ?? "");
  const modes = [c.plan?.live && t("cloud.mode.live"), queueOn(c) && t("cloud.mode.queue")].filter(Boolean);
  return [tier, modes.join(" + ")].filter(Boolean).join(" · ");
}

type Tone = "fresh" | "wait" | "danger" | "muted";
const DOT: Record<Tone, string> = { fresh: "bg-fresh", wait: "bg-wait", danger: "bg-danger", muted: "bg-faint" };

/** One line under the name: what is going on, and who is signed in. */
const line = computed<{ text: string; tone: Tone }>(() => {
  const c = s.value;
  if (!c) return { text: failed.value ? t("cloud.loadError") : t("common.loading"), tone: "muted" };
  if (!c.linked) {
    if (c.pending) return { text: t("cloud.waiting"), tone: "wait" };
    if (c.link_error) return { text: t("cloud.linkError", { error: c.link_error }), tone: "danger" };
    return { text: t("cloud.description"), tone: "muted" };
  }
  if (!c.this_folder) return { text: t("cloud.folder", { folder: c.folder ?? "" }), tone: "danger" };
  // The masked email too: it is what tells someone this computer is linked to their account and not another's.
  const account = [c.account?.display_name, c.account?.email_masked].filter(Boolean).join(" · ");
  const who = account ? t("cloud.signedInAs", { name: account }) : "";
  let state: { key: string; tone: Tone };
  if (c.online) state = { key: "online", tone: "fresh" };
  else if (c.reason === "plan") state = { key: "plan", tone: "danger" };
  else if (c.reason === "refused") state = { key: "refused", tone: "danger" };
  else if (c.reason === "replaced") state = { key: "replaced", tone: "wait" };
  else if (c.reason === "key") state = { key: "key", tone: "danger" };
  else if (c.reason === "unlinked") state = { key: "unlinked", tone: "muted" };
  else if (c.reason === "offline") state = { key: "offline", tone: "wait" }; // also while it tries again
  else if (c.state === "connecting") state = { key: "connecting", tone: "wait" };
  else state = { key: "stopped", tone: "muted" };
  const last = c.online && c.last_call_at ? t("cloud.lastCall", { time: relTime(c.last_call_at) }) : "";
  return { text: [t(`cloud.state.${state.key}`), who, last].filter(Boolean).join(" · "), tone: state.tone };
});

/** The button that fixes what the status line says, when there is one. */
const fix = computed<"tryAgain" | "useHere" | "relink" | null>(() => {
  const c = s.value;
  if (!c?.linked) return null;
  if (!c.this_folder || c.reason === "key") return "relink";
  if (c.reason === "plan" || c.reason === "refused") return "tryAgain";
  if (c.reason === "replaced") return "useHere";
  return null;
});

/** Open MyMemo's page in the browser. The Mac app hands any https navigation to the default browser. */
function openExternal(url: string) {
  if (desktop) window.location.assign(url);
  else window.open(url, "_blank", "noopener");
}

async function signIn() {
  busy.value = true;
  try {
    const { url } = await api.cloudLink();
    signInUrl.value = url;
    openExternal(url);
    await load();
  } catch (e) {
    toast(e instanceof Error ? e.message : String(e), "error");
  } finally {
    busy.value = false;
  }
}

async function unlink() {
  if (!confirm(t("cloud.unlinkConfirm"))) return;
  busy.value = true;
  try {
    const res = await api.cloudUnlink();
    take(res.cloud);
    showSetup.value = false;
    toast(res.message);
    await refresh();
  } catch (e) {
    toast(e instanceof Error ? e.message : String(e), "error");
  } finally {
    busy.value = false;
  }
}

async function reconnect() {
  try {
    status.value = await api.cloudReconnect();
  } catch (e) {
    toast(e instanceof Error ? e.message : String(e), "error");
  }
}

function onFix() {
  if (fix.value === "relink") signIn();
  else reconnect();
}

async function allow(app: CloudApp) {
  try {
    status.value = await api.cloudAllow(app.id);
    toast(t("cloud.allowed", { name: app.name }));
    await refresh();
  } catch (e) {
    toast(e instanceof Error ? e.message : String(e), "error");
  }
}

watch(() => s.value?.pending, (pending) => !pending && !s.value?.linked && (signInUrl.value = null));

/** The panel's live connector (refreshed every few seconds), falling back to the status's copy. */
const live = (app: CloudApp) => store.connectors.find((x) => x.id === app.id) ?? app.connector;
const apps = computed(() => (linkedHere.value ? s.value?.apps ?? [] : (s.value?.apps ?? []).filter((a) => !a.revoked)));

function appLine(app: CloudApp): string {
  if (app.revoked) return t("cloud.app.revoked");
  return app.last_seen ? t("cloud.app.seen", { time: relTime(app.last_seen) }) : t("cloud.app.new");
}

function toggleManage(id: string) {
  emit("manage", props.open === id ? null : id);
}

defineExpose({ reload: load });
</script>

<template>
  <div class="divide-y divide-sep">
    <div class="flex items-center gap-3 px-4.5 py-3">
      <span class="avatar size-[34px] text-sm" :class="{ 'avatar-on': linkedHere }" aria-hidden="true">M</span>
      <div class="min-w-0 flex-1">
        <div class="truncate font-semibold">
          {{ t("cloud.name") }}<span v-if="s?.linked && planLabel(s)" class="font-normal text-muted"> · {{ planLabel(s) }}</span>
        </div>
        <div class="flex items-center gap-1.5 text-[13px] text-muted">
          <span class="dot" :class="DOT[line.tone]" aria-hidden="true" />
          <span class="truncate" :title="line.text">{{ line.text }}</span>
        </div>
      </div>
      <template v-if="s">
        <button v-if="fix" class="btn btn-sm btn-primary" :disabled="busy" @click="onFix">{{ t(`cloud.${fix}`) }}</button>
        <button
          v-if="linkedHere"
          class="btn btn-quiet btn-sm"
          :aria-expanded="showSetup"
          :aria-label="t('cloud.setupNamed')"
          @click="showSetup = !showSetup"
        >
          <AppIcon name="sliders" :size="14" /><span class="max-sm:hidden">{{ t("cloud.setup") }}</span>
        </button>
        <button v-if="s.linked" class="btn btn-sm" :disabled="busy" @click="unlink">{{ t("cloud.unlink") }}</button>
        <button v-else class="btn btn-sm btn-primary" :disabled="busy" @click="signIn">{{ t("cloud.signIn") }}</button>
      </template>
    </div>

    <div v-if="s && !s.linked && !s.pending && !s.link_error" class="bg-sunken px-4.5 py-3 text-[13px] text-muted">
      {{ t("cloud.pitch") }}
    </div>

    <div v-if="s && !s.linked && s.pending && signInUrl" class="flex flex-wrap items-center gap-2 bg-sunken px-4.5 py-3 text-[13px]" role="status">
      <AppIcon name="info" :size="15" class="text-accent" />
      <span class="min-w-0 flex-1">{{ t("cloud.waiting") }}</span>
      <a :href="signInUrl" target="_blank" rel="noopener" class="btn btn-link btn-sm">{{ t("cloud.openAgain") }}</a>
    </div>

    <div v-if="s && linkedHere && s.queue_enabled && s.queued" class="flex items-start gap-2.5 bg-sunken px-4.5 py-3 text-[13px]" role="status" data-queued>
      <AppIcon name="clock" :size="15" class="mt-px text-wait-ink" />
      <div class="min-w-0 flex-1">
        <div>{{ tn("cloud.queued", s.queued) }}</div>
        <div class="text-muted">{{ t("cloud.queuedHint") }}</div>
      </div>
    </div>

    <div v-if="s && linkedHere && showSetup" class="space-y-3 bg-sunken px-4.5 py-3.5 text-[13px]">
      <p>{{ t("cloud.endpoint") }}</p>
      <div class="relative rounded-[10px] bg-surface shadow-[0_0_0_1px_var(--color-ring)]">
        <pre class="overflow-auto px-3.5 py-3 pr-20 font-mono text-xs leading-relaxed break-all whitespace-pre-wrap" data-endpoint>{{ s.endpoint }}</pre>
        <button class="btn btn-sm absolute top-2 right-2" @click="copy(s.endpoint)">
          <AppIcon name="copy" :size="14" />{{ t("common.copy") }}
        </button>
      </div>
      <ul class="space-y-2">
        <li><span class="font-semibold">{{ t("cloud.claude") }}:</span> {{ t("cloud.claudeSteps") }}</li>
        <li>
          <span class="font-semibold">{{ t("cloud.chatgpt") }}:</span> {{ t("cloud.chatgptSteps") }}
          <span class="text-muted">{{ t("cloud.chatgptPlan") }}</span>
        </li>
      </ul>
      <p class="text-muted">{{ t(queueOn(s) ? "cloud.whileRunningQueue" : "cloud.whileRunning") }}</p>
    </div>

    <template v-for="app in apps" :key="app.id">
      <div class="flex items-center gap-3 px-4.5 py-3">
        <span class="avatar size-[34px] text-sm" :class="{ 'avatar-on': !app.revoked }" aria-hidden="true">{{ app.name.slice(0, 1) }}</span>
        <div class="min-w-0 flex-1">
          <div class="truncate font-semibold">{{ app.name }}</div>
          <div class="flex items-center gap-1.5 text-[13px] text-muted">
            <span class="dot" :class="app.revoked ? 'bg-faint' : app.last_seen ? 'bg-fresh' : 'bg-faint'" aria-hidden="true" />
            <span class="truncate">{{ appLine(app) }}</span>
          </div>
        </div>
        <button
          v-if="$slots.manage && live(app)"
          class="btn btn-quiet btn-sm"
          :aria-expanded="open === app.id"
          :aria-label="t('apps.manageNamed', { name: app.name })"
          @click="toggleManage(app.id)"
        >
          <AppIcon name="sliders" :size="14" /><span class="max-sm:hidden">{{ t("apps.manage") }}</span>
        </button>
        <button v-if="app.revoked" class="btn btn-sm" @click="allow(app)">{{ t("cloud.allow") }}</button>
      </div>
      <div v-if="$slots.manage && live(app) && open === app.id" class="bg-sunken">
        <slot name="manage" :app="null" :connector="live(app)!" />
      </div>
    </template>
  </div>
</template>
