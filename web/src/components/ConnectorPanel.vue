<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { api, type AppSetup, type AppStatus, type Connector } from "../api";
import { assistantPlace, relTime, t } from "../i18n";
import { act, copy, store, topicTitle } from "../store";
import AppIcon from "./AppIcon.vue";
import ModalDialog from "./ModalDialog.vue";
import SnippetTabs from "./SnippetTabs.vue";

/**
 * One connected app's permissions: what it may read, how it writes, the topics it may not write to, its
 * setup, a new token, and removing it. `app` is its catalog entry, or null for an app added by name or a web app
 * using OpenMyMemo Cloud (kind "cloud", whose setup is on the OpenMyMemo Cloud row).
 * Removing asks whether to take OpenMyMemo out of the app's settings too; the parent does the removal.
 */
const props = defineProps<{ connector: Connector; app: AppStatus | null }>();
const emit = defineEmits<{ remove: [alsoSettings: boolean] }>();

const WRITE_MODES = ["none", "propose", "auto"] as const;

const c = computed(() => props.connector);
/** A catalog app running over stdio gets its own setup; anything else the generic snippets (and a token). */
const ownSetup = computed(() => !!props.app && c.value.kind === "stdio");
/** A web app through OpenMyMemo Cloud: nothing to paste here, and no token. */
const viaCloud = computed(() => c.value.kind === "cloud");
/** The OpenMyMemo Assistant: built in, set up in Settings → Assistant, with its private topics chosen there. */
const builtIn = computed(() => c.value.kind === "assistant");
const KIND_LABEL: Record<Connector["kind"], string> = {
  stdio: "stdio", http: "HTTP", cloud: t("cloud.name"), assistant: t("apps.assistant"),
};
const setup = ref<AppSetup | null>(null);
const snippets = ref<Record<string, string> | null>(null);
const token = ref<string | null>(null);

// Primitives, so the panel's 4-second refresh (a new connector object each time) doesn't refetch this.
watch(
  [() => c.value.id, () => c.value.kind],
  async ([id]) => {
    setup.value = null;
    snippets.value = null;
    if (viaCloud.value || builtIn.value) return;
    try {
      if (ownSetup.value) setup.value = await api.appSetup(id);
      else snippets.value = await api.snippets(id);
    } catch {
      // The setup section shows nothing until the next time; the permissions still work.
    }
  },
  { immediate: true },
);

async function rotate() {
  const res = await act(() => api.rotate(c.value.id));
  if (res) {
    snippets.value = res.snippets;
    token.value = res.token;
  }
}

const allSlugs = computed(() => store.state?.topics.map((x) => x.slug) ?? []);

function toggleTopic(slug: string) {
  const all = allSlugs.value;
  const current = new Set(c.value.read_topics === "all" ? all : c.value.read_topics);
  current.has(slug) ? current.delete(slug) : current.add(slug);
  const next = all.every((s) => current.has(s)) ? "all" : all.filter((s) => current.has(s));
  act(() => api.patchConnector(c.value.id, { read_topics: next }));
}

const setWrite = (write: Connector["write"]) => act(() => api.patchConnector(c.value.id, { write }));
const readAll = () => act(() => api.patchConnector(c.value.id, { read_topics: "all" }));
const togglePrivate = () => act(() => api.patchConnector(c.value.id, { include_private: !c.value.include_private }));
const unblock = (slug: string) =>
  act(() => api.patchConnector(c.value.id, { blocked_topics: c.value.blocked_topics.filter((x) => x !== slug) }));
const reads = (slug: string) => c.value.read_topics === "all" || c.value.read_topics.includes(slug);
/** What the app actually gets: selected, and not private unless private topics are included. */
const sees = (slug: string, isPrivate: boolean) => reads(slug) && (!isPrivate || c.value.include_private);
const seenCount = computed(() => store.state?.topics.filter((tp) => sees(tp.slug, tp.private)).length ?? 0);

const name = computed(() => props.app?.name ?? c.value.name);
/** Whether OpenMyMemo can take itself out of the app's settings (it found its entry there). */
const hasEntry = computed(() => props.app?.via === "config");
const removing = ref(false);
const alsoSettings = ref(true);

function confirmRemove() {
  removing.value = false;
  emit("remove", hasEntry.value && alsoSettings.value);
}
</script>

<template>
  <div class="divide-y divide-sep">
    <div class="flex flex-wrap items-center gap-x-3 gap-y-2 px-4.5 py-3">
      <div class="min-w-[200px] flex-1 text-[13px] break-words text-muted">
        <span class="font-mono text-xs">{{ c.id }}</span> · {{ KIND_LABEL[c.kind] ?? c.kind }}
        <template v-if="c.token_preview"> · <span class="font-mono text-xs">{{ c.token_preview }}</span></template>
        · {{ t("con.lastSeen") }} {{ relTime(c.last_seen) }} · {{ t("con.stats", { reads: c.reads, proposals: c.proposals }) }}
      </div>
      <a :href="`#/context/${encodeURIComponent(c.id)}`" class="btn btn-sm">
        <AppIcon name="eye" :size="14" class="text-muted" />{{ t("con.seeContext") }}
      </a>
    </div>

    <div class="flex flex-wrap items-center justify-between gap-3 px-4.5 py-3.5">
      <div class="min-w-0 space-y-0.5">
        <div class="font-semibold">{{ t("con.write") }}</div>
        <p class="text-[13px] text-muted">{{ t(`con.writeHelp.${c.write}`) }}</p>
      </div>
      <div class="seg" role="radiogroup" :aria-label="t('con.write')">
        <button
          v-for="w in WRITE_MODES"
          :key="w"
          class="seg-item"
          :class="{ 'is-on': c.write === w }"
          role="radio"
          :aria-checked="c.write === w"
          :title="t(`con.writeFull.${w}`)"
          @click="setWrite(w)"
        >{{ t(`con.write.${w}`) }}</button>
      </div>
    </div>

    <div class="space-y-2.5 px-4.5 py-3.5">
      <div class="flex items-start justify-between gap-3">
        <div class="min-w-0 space-y-0.5">
          <div class="font-semibold">{{ t("con.read") }}</div>
          <p class="text-[13px] text-muted">{{ t("con.readCount", { n: seenCount, total: allSlugs.length }) }}</p>
        </div>
        <button v-if="c.read_topics !== 'all'" class="btn btn-link btn-sm" @click="readAll">{{ t("con.selectAll") }}</button>
      </div>
      <div class="flex flex-wrap gap-1.5">
        <button
          v-for="tp in store.state?.topics"
          :key="tp.slug"
          class="pill"
          :class="sees(tp.slug, tp.private) ? 'pill-on' : 'pill-off'"
          :aria-pressed="reads(tp.slug)"
          @click="toggleTopic(tp.slug)"
        >
          <AppIcon v-if="tp.private" name="lock" :size="12" />{{ tp.title }}
        </button>
      </div>
    </div>

    <div v-if="builtIn" class="space-y-0.5 px-4.5 py-3.5">
      <div class="font-semibold">{{ t("con.includePrivate") }}</div>
      <p class="text-[13px] text-muted">{{ t("con.assistantPrivate", { where: assistantPlace() }) }}</p>
    </div>
    <div v-else class="flex items-center justify-between gap-4 px-4.5 py-3.5">
      <div class="min-w-0 space-y-0.5">
        <div class="font-semibold">{{ t("con.includePrivate") }}</div>
        <p class="text-[13px] text-muted">{{ c.include_private ? t("con.includePrivateOn") : t("con.includePrivateOff") }}</p>
      </div>
      <button role="switch" class="switch" :aria-checked="c.include_private" :aria-label="t('con.includePrivate')" @click="togglePrivate">
        <span class="switch-knob" />
      </button>
    </div>

    <div v-if="c.blocked_topics.length" class="flex flex-wrap items-center justify-between gap-3 px-4.5 py-3.5">
      <div class="min-w-0 space-y-0.5">
        <div class="font-semibold">{{ t("con.blocked") }}</div>
        <p class="text-[13px] text-muted">{{ t("con.blockedHelp") }}</p>
      </div>
      <div class="flex flex-wrap gap-1.5">
        <button v-for="slug in c.blocked_topics" :key="slug" class="pill bg-fill text-ink hover:bg-fill-strong" @click="unblock(slug)">
          {{ topicTitle(slug) }}<AppIcon name="x" :size="12" class="text-muted" />
        </button>
      </div>
    </div>

    <p v-if="builtIn" class="px-4.5 py-3.5 text-[13px] text-muted">{{ t("con.assistantSetup", { where: assistantPlace() }) }}</p>
    <div v-else-if="!viaCloud">
      <div class="flex flex-wrap items-center justify-between gap-3 px-4.5 py-3.5">
        <div class="min-w-0 space-y-0.5">
          <div class="font-semibold">{{ t("con.config") }}</div>
          <p class="text-[13px] text-muted">{{ ownSetup ? t("apps.setupHelp", { name }) : t("con.setupHelp") }}</p>
        </div>
        <button v-if="c.kind === 'http'" class="btn btn-sm" @click="rotate">{{ t("con.rotate") }}</button>
      </div>
      <div v-if="token" class="notice notice-wait mx-4.5 mb-3.5 flex-col items-stretch gap-1.5">
        <div>{{ t("con.tokenOnce") }}</div>
        <div class="flex items-center gap-2">
          <code class="min-w-0 flex-1 font-mono text-xs break-all text-ink">{{ token }}</code>
          <button class="btn btn-sm" @click="copy(token)"><AppIcon name="copy" :size="14" />{{ t("common.copy") }}</button>
        </div>
      </div>
      <div v-if="setup" class="space-y-1.5 px-4.5 pb-3.5 text-[13px]">
        <div v-if="setup.path" class="text-muted">
          {{ t("apps.pasteInto") }} <span class="font-mono text-xs break-all text-ink">{{ setup.path }}</span>
        </div>
        <div class="relative rounded-[10px] bg-surface shadow-[0_0_0_1px_var(--color-ring)]">
          <pre class="max-h-64 overflow-auto px-3.5 py-3 pr-20 font-mono text-xs leading-relaxed break-all whitespace-pre-wrap">{{ setup.snippet }}</pre>
          <button class="btn btn-sm absolute top-2 right-2" @click="copy(setup.snippet)"><AppIcon name="copy" :size="14" />{{ t("common.copy") }}</button>
        </div>
      </div>
      <SnippetTabs v-else-if="snippets" :snippets="snippets" />
      <div v-else class="border-t border-sep px-4.5 py-5 text-center text-sm text-muted">{{ t("common.loading") }}</div>
    </div>

    <div class="flex justify-end px-4.5 py-3">
      <button class="btn btn-danger btn-sm" @click="removing = true">{{ t("con.removeNamed", { name }) }}</button>
    </div>

    <ModalDialog v-if="removing" :title="t('apps.removeTitle', { name })" @close="removing = false">
      <div class="space-y-4 text-sm">
        <p>{{ t("apps.removeHelp", { name }) }}</p>
        <label v-if="hasEntry" class="flex items-start gap-2.5">
          <input v-model="alsoSettings" type="checkbox" class="check mt-0.5" />
          <span>{{ t("apps.removeEntry", { name }) }}</span>
        </label>
        <div class="flex justify-end gap-2 pt-1">
          <button type="button" class="btn" @click="removing = false">{{ t("common.cancel") }}</button>
          <button type="button" class="btn btn-primary" @click="confirmRemove">{{ t("con.remove") }}</button>
        </div>
      </div>
    </ModalDialog>
  </div>
</template>
