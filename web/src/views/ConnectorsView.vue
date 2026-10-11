<script setup lang="ts">
import { computed, reactive, ref, watch } from "vue";
import { api, type Connector } from "../api";
import AppIcon from "../components/AppIcon.vue";
import ModalDialog from "../components/ModalDialog.vue";
import PageHeader from "../components/PageHeader.vue";
import SnippetTabs from "../components/SnippetTabs.vue";
import { relTime, t } from "../i18n";
import { act, copy, store, topicTitle } from "../store";

const props = defineProps<{ initialConnector: string | null }>();

const WRITE_MODES = ["none", "propose", "auto"] as const;

const adding = ref<{ name: string; kind: "stdio" | "http"; write: Connector["write"] } | null>(null);
/** Setup snippets per connector, loaded when it is first shown; a token only right after it was created or rotated. */
const snippets = reactive<Record<string, Record<string, string>>>({});
const tokens = reactive<Record<string, string>>({});

const selectedId = ref<string | null>(props.initialConnector);
watch(() => props.initialConnector, (v) => v && (selectedId.value = v));
const selected = computed(() => store.connectors.find((c) => c.id === selectedId.value) ?? store.connectors[0] ?? null);

function select(c: Connector) {
  selectedId.value = c.id;
  history.replaceState(null, "", `#/connectors/${encodeURIComponent(c.id)}`);
}

watch(
  () => selected.value?.id,
  async (id) => {
    if (id && !snippets[id]) {
      try {
        snippets[id] = await api.snippets(id);
      } catch {
        // The setup card shows nothing until the next selection; the rest of the page still works.
      }
    }
  },
  { immediate: true },
);

async function create() {
  const v = adding.value;
  if (!v || !v.name.trim()) return;
  const res = await act(() => api.addConnector({ ...v, name: v.name.trim() }), t("con.created", { name: v.name.trim() }));
  if (res) {
    adding.value = null;
    snippets[res.connector.id] = res.snippets;
    if (res.token) tokens[res.connector.id] = res.token;
    select(res.connector);
  }
}

async function rotate(c: Connector) {
  const res = await act(() => api.rotate(c.id));
  if (res) {
    snippets[c.id] = res.snippets;
    if (res.token) tokens[c.id] = res.token;
  }
}

async function remove(c: Connector) {
  if (confirm(t("con.removeConfirm", { name: c.name }))) await act(() => api.removeConnector(c.id));
}

const allSlugs = computed(() => store.state?.topics.map((x) => x.slug) ?? []);

function toggleTopic(c: Connector, slug: string) {
  const all = allSlugs.value;
  const currentSet = new Set(c.read_topics === "all" ? all : c.read_topics);
  currentSet.has(slug) ? currentSet.delete(slug) : currentSet.add(slug);
  const next = all.every((s) => currentSet.has(s)) ? "all" : all.filter((s) => currentSet.has(s));
  act(() => api.patchConnector(c.id, { read_topics: next }));
}

const setWrite = (c: Connector, write: Connector["write"]) => act(() => api.patchConnector(c.id, { write }));
const readAll = (c: Connector) => act(() => api.patchConnector(c.id, { read_topics: "all" }));
const togglePrivate = (c: Connector) => act(() => api.patchConnector(c.id, { include_private: !c.include_private }));
const unblock = (c: Connector, slug: string) =>
  act(() => api.patchConnector(c.id, { blocked_topics: c.blocked_topics.filter((x) => x !== slug) }));
const reads = (c: Connector, slug: string) => c.read_topics === "all" || c.read_topics.includes(slug);
/** What the app actually gets: selected, and not private unless private topics are included. */
const sees = (c: Connector, slug: string, isPrivate: boolean) => reads(c, slug) && (!isPrivate || c.include_private);
const seenCount = (c: Connector) => store.state?.topics.filter((tp) => sees(c, tp.slug, tp.private)).length ?? 0;
const seen = (c: Connector) => (c.last_seen ? t("con.seen", { time: relTime(c.last_seen) }) : t("con.neverSeen"));
</script>

<template>
  <div class="mx-auto max-w-[960px] space-y-5">
    <PageHeader :title="t('nav.connectors')" :subtitle="t('con.subtitle')">
      <button class="btn btn-primary" @click="adding = { name: '', kind: 'stdio', write: 'propose' }">
        <AppIcon name="plus" :size="14" />{{ t("con.add") }}
      </button>
    </PageHeader>

    <a v-if="store.state?.settings.require_approval" href="#/changes" class="notice notice-wait text-ink">
      <AppIcon name="info" class="text-wait-ink" />{{ t("con.approvalOn") }}
    </a>

    <div v-if="!store.connectors.length" class="card flex flex-col items-center gap-1.5 px-6 py-14 text-center">
      <AppIcon name="plug" :size="32" class="text-muted" />
      <p class="mt-1 font-semibold">{{ t("con.empty") }}</p>
      <p class="max-w-md text-[13px] text-muted">{{ t("con.emptyHint") }}</p>
    </div>

    <div v-else class="flex flex-wrap items-start gap-5">
      <!-- the apps -->
      <nav :aria-label="t('con.apps')" class="card flex w-full flex-col overflow-hidden lg:w-64 lg:shrink-0">
        <button
          v-for="c in store.connectors"
          :key="c.id"
          class="flex items-center gap-3 border-b border-sep px-3.5 py-3 text-left transition-colors last:border-b-0"
          :class="selected?.id === c.id ? 'bg-accent-soft' : 'hover:bg-hover'"
          :aria-current="selected?.id === c.id ? 'true' : undefined"
          @click="select(c)"
        >
          <span class="avatar size-[34px] text-sm" :class="{ 'avatar-on': selected?.id === c.id }" aria-hidden="true">{{ c.name.slice(0, 1) }}</span>
          <span class="flex min-w-0 flex-1 flex-col">
            <span class="truncate font-semibold">{{ c.name }}</span>
            <span class="truncate text-xs text-muted">{{ t(`con.write.${c.write}`) }} · {{ seen(c) }}</span>
          </span>
        </button>
      </nav>

      <!-- the selected app -->
      <section v-if="selected" :aria-label="selected.name" class="flex min-w-0 flex-[999_1_420px] flex-col gap-4">
        <div class="card flex flex-wrap items-center gap-3.5 px-5 py-4.5">
          <span class="avatar avatar-on size-12 rounded-xl text-xl" aria-hidden="true">{{ selected.name.slice(0, 1) }}</span>
          <div class="min-w-[200px] flex-1 space-y-0.5">
            <h2 class="text-xl font-bold tracking-[-0.01em]">{{ selected.name }}</h2>
            <div class="text-[13px] break-words text-muted">
              <span class="font-mono text-xs">{{ selected.id }}</span> · {{ selected.kind === "http" ? "HTTP" : "stdio" }}
              <template v-if="selected.token_preview"> · <span class="font-mono text-xs">{{ selected.token_preview }}</span></template>
              · {{ t("con.lastSeen") }} {{ relTime(selected.last_seen) }} · {{ t("con.stats", { reads: selected.reads, proposals: selected.proposals }) }}
            </div>
          </div>
          <a :href="`#/context/${encodeURIComponent(selected.id)}`" class="btn btn-sm">
            <AppIcon name="eye" :size="14" class="text-muted" />{{ t("con.seeContext") }}
          </a>
        </div>

        <div class="card divide-y divide-sep">
          <div class="flex flex-wrap items-center justify-between gap-3 px-5 py-3.5">
            <div class="min-w-0 space-y-0.5">
              <div class="font-semibold">{{ t("con.write") }}</div>
              <p class="text-[13px] text-muted">{{ t(`con.writeHelp.${selected.write}`) }}</p>
            </div>
            <div class="seg" role="radiogroup" :aria-label="t('con.write')">
              <button
                v-for="w in WRITE_MODES"
                :key="w"
                class="seg-item"
                :class="{ 'is-on': selected.write === w }"
                role="radio"
                :aria-checked="selected.write === w"
                :title="t(`con.writeFull.${w}`)"
                @click="setWrite(selected, w)"
              >{{ t(`con.write.${w}`) }}</button>
            </div>
          </div>

          <div class="space-y-2.5 px-5 py-3.5">
            <div class="flex items-start justify-between gap-3">
              <div class="min-w-0 space-y-0.5">
                <div class="font-semibold">{{ t("con.read") }}</div>
                <p class="text-[13px] text-muted">{{ t("con.readCount", { n: seenCount(selected), total: allSlugs.length }) }}</p>
              </div>
              <button v-if="selected.read_topics !== 'all'" class="btn btn-link btn-sm" @click="readAll(selected)">{{ t("con.selectAll") }}</button>
            </div>
            <div class="flex flex-wrap gap-1.5">
              <button
                v-for="tp in store.state?.topics"
                :key="tp.slug"
                class="pill"
                :class="sees(selected, tp.slug, tp.private) ? 'pill-on' : 'pill-off'"
                :aria-pressed="reads(selected, tp.slug)"
                @click="toggleTopic(selected, tp.slug)"
              >
                <AppIcon v-if="tp.private" name="lock" :size="12" />{{ tp.title }}
              </button>
            </div>
          </div>

          <div class="flex items-center justify-between gap-4 px-5 py-3.5">
            <div class="min-w-0 space-y-0.5">
              <div class="font-semibold">{{ t("con.includePrivate") }}</div>
              <p class="text-[13px] text-muted">{{ selected.include_private ? t("con.includePrivateOn") : t("con.includePrivateOff") }}</p>
            </div>
            <button
              role="switch"
              class="switch"
              :aria-checked="selected.include_private"
              :aria-label="t('con.includePrivate')"
              @click="togglePrivate(selected)"
            >
              <span class="switch-knob" />
            </button>
          </div>

          <div v-if="selected.blocked_topics.length" class="flex flex-wrap items-center justify-between gap-3 px-5 py-3.5">
            <div class="min-w-0 space-y-0.5">
              <div class="font-semibold">{{ t("con.blocked") }}</div>
              <p class="text-[13px] text-muted">{{ t("con.blockedHelp") }}</p>
            </div>
            <div class="flex flex-wrap gap-1.5">
              <button
                v-for="slug in selected.blocked_topics"
                :key="slug"
                class="pill bg-fill text-ink hover:bg-fill-strong"
                @click="unblock(selected, slug)"
              >{{ topicTitle(slug) }}<AppIcon name="x" :size="12" class="text-muted" /></button>
            </div>
          </div>
        </div>

        <div class="card overflow-hidden">
          <div class="flex flex-wrap items-center justify-between gap-3 px-5 py-3.5">
            <div class="min-w-0 space-y-0.5">
              <div class="font-semibold">{{ t("con.config") }}</div>
              <p class="text-[13px] text-muted">{{ t("con.setupHelp") }}</p>
            </div>
            <button v-if="selected.kind === 'http'" class="btn btn-sm" @click="rotate(selected)">{{ t("con.rotate") }}</button>
          </div>
          <div v-if="tokens[selected.id]" class="notice notice-wait mx-5 mb-3.5 flex-col items-stretch gap-1.5">
            <div>{{ t("con.tokenOnce") }}</div>
            <div class="flex items-center gap-2">
              <code class="min-w-0 flex-1 font-mono text-xs break-all text-ink">{{ tokens[selected.id] }}</code>
              <button class="btn btn-sm" @click="copy(tokens[selected.id])"><AppIcon name="copy" :size="14" />{{ t("common.copy") }}</button>
            </div>
          </div>
          <SnippetTabs v-if="snippets[selected.id]" :snippets="snippets[selected.id]" />
          <div v-else class="border-t border-sep px-5 py-6 text-center text-sm text-muted">{{ t("common.loading") }}</div>
        </div>

        <div class="flex justify-end">
          <button class="btn btn-danger btn-sm" @click="remove(selected)">{{ t("con.removeNamed", { name: selected.name }) }}</button>
        </div>
      </section>
    </div>

    <ModalDialog v-if="adding" :title="t('con.add')" @close="adding = null">
      <form class="space-y-5" @submit.prevent="create">
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
          <button type="button" class="btn" @click="adding = null">{{ t("common.cancel") }}</button>
          <button type="submit" class="btn btn-primary">{{ t("common.save") }}</button>
        </div>
      </form>
    </ModalDialog>
  </div>
</template>
