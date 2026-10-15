<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from "vue";
import { api, type AppCatalog, type AppStatus, type Connector, type ConnectorCreated, type ConnectResult } from "../api";
import { relTime, t } from "../i18n";
import { copy, refresh, store, toast } from "../store";
import AppIcon from "./AppIcon.vue";
import CloudRow from "./CloudRow.vue";
import ModalDialog from "./ModalDialog.vue";
import ConnectNote from "./ConnectNote.vue";
import SnippetTabs from "./SnippetTabs.vue";

/**
 * The AI apps OpenMyMemo knows (src/openmymemo/clients.py), each with one action: Connect, Disconnect, or
 * "Set up" for apps that need a snippet pasted. Loads its own data. `compact` (the first-run guide) lists the
 * apps on this computer and folds the rest; the Connectors page passes a `manage` slot, shown under a
 * connected app's row when its permissions are opened. MyMemo Cloud (the catalog's `account` entry) has a section
 * of its own, "On the web", with the web apps using it underneath (CloudRow); the first-run guide leaves it out.
 */
const props = defineProps<{ compact?: boolean; open?: string | null }>();
const emit = defineEmits<{ connected: [id: string]; disconnected: [id: string]; manage: [id: string | null] }>();
defineSlots<{ manage?: (scope: { app: AppStatus | null; connector: Connector }) => unknown }>();

const OTHER = "other";
const WRITE_MODES = ["none", "propose", "auto"] as const;

const catalog = ref<AppCatalog | null>(null);
const failed = ref(false);
const busy = reactive<Record<string, boolean>>({});
/** What the last action said, shown under the row: a restart hint, steps, or a snippet to paste. */
const results = reactive<Record<string, ConnectResult>>({});
const showAll = ref(false);

const cloudRow = ref<InstanceType<typeof CloudRow> | null>(null);
const setCloudRow = (el: unknown) => (cloudRow.value = el as InstanceType<typeof CloudRow> | null);

let loadedAt = 0;
/** Bumped by every load and every action, so an answer that was asked for earlier can't overwrite a newer one. */
let seq = 0;
async function load() {
  const mine = ++seq;
  loadedAt = Date.now();
  void cloudRow.value?.reload();
  try {
    const data = await api.apps();
    if (mine !== seq) return;
    catalog.value = data;
    failed.value = false;
  } catch {
    if (mine === seq) failed.value = !catalog.value;
  }
}

// Detection reads other apps' files, so it isn't redone on every 4-second refresh: only when a connector
// appears or goes away, every half minute, and when the person comes back to the window (after a restart).
const connectorIds = computed(() => store.connectors.map((c) => c.id).join(" "));
watch(connectorIds, () => catalog.value && load());
watch(() => store.version, () => Date.now() - loadedAt > 30_000 && load());
watch(() => store.cloudVersion, () => load());  // MyMemo Cloud shown or hidden in Settings
const onFocus = () => document.visibilityState === "visible" && Date.now() - loadedAt > 2_000 && load();
onMounted(() => {
  load();
  window.addEventListener("focus", onFocus);
  document.addEventListener("visibilitychange", onFocus);
});
onBeforeUnmount(() => {
  window.removeEventListener("focus", onFocus);
  document.removeEventListener("visibilitychange", onFocus);
});

/** An app with the panel's live view of its connector (refreshed every few seconds) laid over it. */
function live(a: AppStatus): AppStatus {
  const c = store.connectors.find((x) => x.id === a.id);
  if (!c) return a;
  return { ...a, connector: c, has_connector: true, in_use: !!c.last_seen, last_seen: c.last_seen };
}

const mac = computed(() => catalog.value?.system === "darwin");
const apps = computed(() => (catalog.value?.apps ?? []).filter((a) => a.id !== OTHER && a.method !== "account").map(live));
/** MyMemo Cloud, when this installation's catalog has it. */
const cloud = computed(() => (props.compact ? null : catalog.value?.apps.find((a) => a.method === "account") ?? null));
const other = computed(() => catalog.value?.apps.find((a) => a.id === OTHER) ?? null);
const here = (a: AppStatus) => a.installed || a.connected || a.has_connector;
const nearby = computed(() => apps.value.filter(here));
const elsewhere = computed(() => apps.value.filter((a) => !here(a)));
const custom = computed(() => (props.compact ? [] : catalog.value?.custom ?? []).map(
  (c) => store.connectors.find((x) => x.id === c.id) ?? c,
));

interface Section {
  id: string;
  title: string;
  apps: AppStatus[];
  custom: Connector[];
  other: boolean;
  empty: string | null;
}

/** On this computer (with apps added by name), then the rest; compact folds the rest away. */
const sections = computed<Section[]>(() => {
  if (!catalog.value) return [];
  const none = !nearby.value.length && !custom.value.length;
  const list: Section[] = [{
    id: "here", title: t(mac.value ? "apps.onThisMac" : "apps.onThisComputer"), apps: nearby.value,
    custom: custom.value, other: false, empty: none ? t(mac.value ? "apps.noneFound" : "apps.noneFoundPc") : null,
  }];
  if (!props.compact || showAll.value) {
    list.push({ id: "more", title: t("apps.more"), apps: elsewhere.value, custom: [], other: !!other.value, empty: null });
  }
  return list;
});

type Tone = "fresh" | "wait" | "danger" | "muted";
function status(a: AppStatus): { text: string; tone: Tone } {
  if (a.in_use) return { text: t("apps.inUse", { time: relTime(a.last_seen) }), tone: "fresh" };
  if (a.broken) return { text: t("apps.broken"), tone: "danger" };
  if (a.connected) {
    return { text: t(a.session ? "apps.newSession" : "apps.restart", { name: a.restart_name }), tone: "wait" };
  }
  if (a.revoked) return { text: t("apps.revoked"), tone: "muted" };
  if (a.installed) return { text: t("apps.installed"), tone: "muted" };
  return { text: t(mac.value ? "apps.notFound" : "apps.notFoundPc"), tone: "muted" };
}
const DOT: Record<Tone, string> = { fresh: "bg-fresh", wait: "bg-wait", danger: "bg-danger", muted: "bg-faint" };

type Action = "connect" | "reconnect" | "disconnect" | "setup";
function action(a: AppStatus): Action {
  if (a.broken) return "reconnect";
  // A connector whose entry was removed from the app outside OpenMyMemo is repaired by connecting again.
  if (a.connected) return "disconnect";
  return a.method === "manual" ? "setup" : "connect";
}

function replace(app: AppStatus) {
  if (!catalog.value) return;
  seq++; // anything still on its way was asked for before this action
  catalog.value = { ...catalog.value, apps: catalog.value.apps.map((a) => (a.id === app.id ? app : a)) };
}

async function run(a: AppStatus, kind: Action, ask = true) {
  if (kind === "disconnect" && ask && !confirm(t("apps.disconnectConfirm", { name: a.name }))) return;
  busy[a.id] = true;
  try {
    const res = kind === "disconnect" ? await api.disconnectApp(a.id) : await api.connectApp(a.id);
    replace(res.app);
    if (res.result.status === "error") {
      toast(res.result.message, "error");
    } else if (kind === "disconnect") {
      toast(t("apps.disconnected", { name: a.name }));
      emit("disconnected", a.id);
    } else if (res.result.status === "done") {
      toast(t("apps.connectedToast", { name: a.name }));
      emit("connected", a.id);
    }
    // A plain "done" needs no note: the status line says what's left. Anything else stays under the row.
    if (res.result.status !== "done" || res.result.steps.length) results[a.id] = res.result;
    else delete results[a.id];
    await refresh();
  } catch (e) {
    toast(e instanceof Error ? e.message : String(e), "error");
  } finally {
    busy[a.id] = false;
  }
}

/** "Set up" toggles the instructions it fetched. */
function primary(a: AppStatus) {
  const kind = action(a);
  if (kind === "setup" && results[a.id]) delete results[a.id];
  else run(a, kind);
}

function toggleManage(id: string) {
  emit("manage", props.open === id ? null : id);
}

// ---- an app the catalog doesn't know: name it, then paste its settings ----

const adding = ref<{ name: string; kind: "stdio" | "http"; write: Connector["write"] } | null>(null);
const created = ref<ConnectorCreated | null>(null);

function addOther() {
  created.value = null;
  adding.value = { name: "", kind: "stdio", write: "propose" };
}

async function create() {
  const v = adding.value;
  const name = v?.name.trim();
  if (!v || !name) return;
  try {
    created.value = await api.addConnector({ ...v, name });
    toast(t("con.created", { name }));
    await refresh();
    await load();
    emit("connected", created.value.connector.id);
  } catch (e) {
    toast(e instanceof Error ? e.message : String(e), "error");
  }
}

function closeAdd() {
  const id = created.value?.connector.id;
  adding.value = null;
  created.value = null;
  if (id && !props.compact) emit("manage", id);
}

/** Disconnect without asking again (the Connectors page asked already, when removing an app). */
async function disconnect(id: string) {
  const a = apps.value.find((x) => x.id === id);
  if (a) await run(a, "disconnect", false);
}

defineExpose({ addOther, disconnect, reload: load });
</script>

<template>
  <div class="space-y-4">
    <div v-if="!catalog" class="card px-5 py-8 text-center text-sm text-muted">
      {{ failed ? t("apps.loadError") : t("common.loading") }}
    </div>

    <template v-for="(s, i) in sections" :key="s.id">
      <section class="space-y-2">
        <h3 v-if="!compact" class="label px-1">{{ s.title }}</h3>
        <div class="card divide-y divide-sep overflow-hidden">
          <p v-if="s.empty" class="px-4.5 py-4 text-[13px] text-muted">{{ s.empty }}</p>

          <template v-for="a in s.apps" :key="a.id">
            <div class="flex items-center gap-3 px-4.5 py-3">
              <span class="avatar size-[34px] text-sm" :class="{ 'avatar-on': a.connected }" aria-hidden="true">{{ a.name.slice(0, 1) }}</span>
              <div class="min-w-0 flex-1">
                <div class="truncate font-semibold">{{ a.name }}<span v-if="a.note" class="font-normal text-muted"> · {{ a.note }}</span></div>
                <div class="flex items-center gap-1.5 text-[13px] text-muted">
                  <span class="dot" :class="DOT[status(a).tone]" aria-hidden="true" />
                  <span class="truncate">{{ status(a).text }}</span>
                </div>
              </div>
              <a v-if="a.site && !here(a)" :href="a.site" target="_blank" rel="noopener" class="btn btn-quiet btn-sm max-sm:hidden">{{ t("apps.get") }}</a>
              <button
                v-if="$slots.manage && a.connector"
                class="btn btn-quiet btn-sm"
                :aria-expanded="open === a.id"
                :aria-label="t('apps.manageNamed', { name: a.name })"
                @click="toggleManage(a.id)"
              >
                <AppIcon name="sliders" :size="14" /><span class="max-sm:hidden">{{ t("apps.manage") }}</span>
              </button>
              <button
                class="btn btn-sm"
                :class="{ 'btn-primary': a.installed && (action(a) === 'connect' || action(a) === 'reconnect') }"
                :disabled="busy[a.id]"
                :aria-expanded="action(a) === 'setup' ? !!results[a.id] : undefined"
                @click="primary(a)"
              >{{ t(`apps.${action(a)}`) }}</button>
            </div>
            <ConnectNote v-if="results[a.id]" :app="a" :result="results[a.id]" @close="delete results[a.id]" />
            <div v-if="$slots.manage && a.connector && open === a.id" class="bg-sunken">
              <slot name="manage" :app="a" :connector="a.connector" />
            </div>
          </template>

          <template v-for="c in s.custom" :key="c.id">
            <div class="flex items-center gap-3 px-4.5 py-3">
              <span class="avatar avatar-on size-[34px] text-sm" aria-hidden="true">{{ c.name.slice(0, 1) }}</span>
              <div class="min-w-0 flex-1">
                <div class="truncate font-semibold">{{ c.name }}</div>
                <div class="flex items-center gap-1.5 text-[13px] text-muted">
                  <span class="dot" :class="c.last_seen ? 'bg-fresh' : 'bg-faint'" aria-hidden="true" />
                  <span class="truncate">{{ c.last_seen ? t("apps.inUse", { time: relTime(c.last_seen) }) : t("apps.addedByName") }}</span>
                </div>
              </div>
              <button
                v-if="$slots.manage"
                class="btn btn-quiet btn-sm"
                :aria-expanded="open === c.id"
                :aria-label="t('apps.manageNamed', { name: c.name })"
                @click="toggleManage(c.id)"
              >
                <AppIcon name="sliders" :size="14" /><span class="max-sm:hidden">{{ t("apps.manage") }}</span>
              </button>
            </div>
            <div v-if="$slots.manage && open === c.id" class="bg-sunken">
              <slot name="manage" :app="null" :connector="c" />
            </div>
          </template>

          <div v-if="s.other" class="flex items-center gap-3 px-4.5 py-3">
            <span class="avatar size-[34px] text-muted" aria-hidden="true"><AppIcon name="plus" :size="16" /></span>
            <div class="min-w-0 flex-1">
              <div class="truncate font-semibold">{{ t("apps.other") }}</div>
              <div class="truncate text-[13px] text-muted">{{ t("apps.otherShort") }}</div>
            </div>
            <button class="btn btn-sm" @click="addOther">{{ t("apps.setup") }}</button>
          </div>
        </div>
      </section>
      <section v-if="i === 0 && cloud" class="space-y-2">
        <h3 class="label px-1">{{ t("cloud.section") }}</h3>
        <div class="card overflow-hidden">
          <CloudRow :ref="setCloudRow" :open="open" @manage="(id) => emit('manage', id)">
            <template v-if="$slots.manage" #manage="scope"><slot name="manage" v-bind="scope" /></template>
          </CloudRow>
        </div>
      </section>
    </template>

    <button v-if="compact && catalog && elsewhere.length" class="btn btn-link btn-sm" :aria-expanded="showAll" @click="showAll = !showAll">
      {{ showAll ? t("apps.showFewer") : t("apps.showAll", { n: elsewhere.length }) }}
      <AppIcon name="chevronDown" :size="13" class="transition-transform" :class="showAll ? 'rotate-180' : ''" />
    </button>

    <ModalDialog v-if="adding" :title="created ? t('apps.addDone', { name: created.connector.name }) : t('con.add')" :wide="!!created" @close="closeAdd">
      <div v-if="created" class="space-y-4">
        <p class="text-[13px] text-muted">{{ t("con.setupHelp") }}</p>
        <div v-if="created.token" class="notice notice-wait flex-col items-stretch gap-1.5">
          <div>{{ t("con.tokenOnce") }}</div>
          <div class="flex items-center gap-2">
            <code class="min-w-0 flex-1 font-mono text-xs break-all text-ink">{{ created.token }}</code>
            <button class="btn btn-sm" @click="copy(created.token)"><AppIcon name="copy" :size="14" />{{ t("common.copy") }}</button>
          </div>
        </div>
        <div class="card overflow-hidden"><SnippetTabs :snippets="created.snippets" /></div>
        <div class="flex justify-end"><button class="btn btn-primary" @click="closeAdd">{{ t("apps.done") }}</button></div>
      </div>
      <form v-else class="space-y-5" @submit.prevent="create">
        <p class="text-[13px] text-muted">{{ t("apps.otherHelp") }}</p>
        <label class="block space-y-1.5">
          <span class="text-[13px] font-semibold">{{ t("con.name") }}</span>
          <input v-model="adding.name" class="input" :placeholder="t('con.namePlaceholder')" required autofocus />
        </label>
        <fieldset class="space-y-2">
          <legend class="mb-1.5 text-[13px] font-semibold">{{ t("con.kind") }}</legend>
          <label v-for="k in (['stdio', 'http'] as const)" :key="k" class="flex items-start gap-2.5 text-sm">
            <input v-model="adding.kind" type="radio" :value="k" class="check mt-0.5" /> {{ t(`con.kind.${k}`) }}
          </label>
        </fieldset>
        <fieldset class="space-y-2">
          <legend class="mb-1.5 text-[13px] font-semibold">{{ t("con.write") }}</legend>
          <label v-for="w in WRITE_MODES" :key="w" class="flex items-start gap-2.5 text-sm">
            <input v-model="adding.write" type="radio" :value="w" class="check mt-0.5" /> <span>{{ t(`con.writeFull.${w}`) }}</span>
          </label>
        </fieldset>
        <div class="flex justify-end gap-2 pt-1">
          <button type="button" class="btn" @click="closeAdd">{{ t("common.cancel") }}</button>
          <button type="submit" class="btn btn-primary">{{ t("common.save") }}</button>
        </div>
      </form>
    </ModalDialog>
  </div>
</template>
