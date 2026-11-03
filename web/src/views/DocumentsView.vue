<script setup lang="ts">
import { computed, onMounted, ref, watch } from "vue";
import { api, ApiError, type DocumentInfo, type HistoryRow } from "../api";
import AppIcon from "../components/AppIcon.vue";
import DocumentDiff from "../components/DocumentDiff.vue";
import ModalDialog from "../components/ModalDialog.vue";
import PageHeader from "../components/PageHeader.vue";
import { prettyName, relTime, sourceLabel, t } from "../i18n";
import { act, connectorName, copy, refresh, store, toast } from "../store";

/**
 * Documents (product-design.md §23): texts AI apps get whole, such as how the person likes to work or the text of a
 * CLAUDE.md. The list, and one document to read, edit, rename, make private, copy, download, take back to an earlier
 * version or archive. `initialSlug` (the route's parameter) opens one.
 */
const props = defineProps<{ initialSlug?: string | null }>();
const MAX = 20_000;

const docs = computed(() => store.state?.documents ?? []);
const slug = computed(() => props.initialSlug ?? null);
const doc = ref<DocumentInfo | null>(null);
const missing = ref(false);
const archived = ref<DocumentInfo[]>([]);

const editing = ref(false);
const draft = ref("");
const busy = ref(false);
const movedOn = ref(false); // changed elsewhere while being edited here: saving would say so

/**
 * Responses can come back out of order: only the latest load is shown, and only for the document still open, so
 * nothing on the page ever shows or acts on a document the person has left.
 */
let loads = 0;
async function load() {
  const want = slug.value;
  const mine = ++loads;
  if (!want) {
    doc.value = null;
    return;
  }
  try {
    const fresh = await api.document(want);
    if (mine !== loads) return; // a later load answers instead
    missing.value = false;
    if (!editing.value || !doc.value) doc.value = fresh;
    else if (fresh.version !== doc.value.version) movedOn.value = true;
  } catch (e) {
    if (mine !== loads) return;
    if (e instanceof ApiError && e.status === 404) {
      missing.value = true;
      doc.value = null;
    }
  }
}

/** Show what an action returned, if its document is still the one open; whether it is. */
function show(d: DocumentInfo | null | undefined): boolean {
  if (!d || d.slug !== slug.value) return false;
  doc.value = d;
  return true;
}

async function loadArchived() {
  try {
    archived.value = await api.documents("archived");
  } catch {
    archived.value = [];
  }
}
onMounted(loadArchived);

/** Changes apps proposed to a document that wait for the person. */
const waiting = (id: string) => store.pending.filter((p) => p.memory_id === id).length;

/** Who wrote a version: an app's name, or you (in the panel, an editor or an import). */
function who(actor: string): string {
  if (actor === "user") return t("doc.byYou");
  if (actor === "file") return t("doc.byEditor");
  if (actor.startsWith("import:")) return t("doc.byImport", { name: prettyName(actor.slice("import:".length)) });
  return actor.startsWith("connector:") ? connectorName(actor.slice("connector:".length)) : sourceLabel(actor);
}

function startEdit() {
  if (!doc.value) return;
  draft.value = doc.value.content ?? "";
  movedOn.value = false;
  editing.value = true;
}

async function save() {
  if (!doc.value) return;
  busy.value = true;
  try {
    if (show(await api.saveDocument(doc.value.slug, draft.value, doc.value.version))) editing.value = false;
    toast(t("doc.saved"));
    await refresh();
  } catch (e) {
    toast(e instanceof ApiError && e.status === 409 ? t("doc.conflict") : e instanceof Error ? e.message : String(e), "error");
  } finally {
    busy.value = false;
  }
}

const tooLong = computed(() => draft.value.length > MAX);

function download() {
  if (!doc.value) return;
  const url = URL.createObjectURL(new Blob([`${doc.value.content ?? ""}\n`], { type: "text/markdown;charset=utf-8" }));
  const a = Object.assign(document.createElement("a"), { href: url, download: `${doc.value.slug}.md` });
  a.click();
  URL.revokeObjectURL(url);
}

const togglePrivate = async () => {
  if (doc.value) show(await act(() => api.patchDocument(doc.value!.slug, { private: !doc.value!.private })));
};

// ---- rename ------------------------------------------------------------------------------------------------------
const renaming = ref(false);
const newName = ref("");
const newDescription = ref("");
function startRename() {
  if (!doc.value) return;
  newName.value = doc.value.title;
  newDescription.value = doc.value.description ?? "";
  renaming.value = true;
}
async function rename() {
  if (!doc.value || !newName.value.trim()) return;
  const res = await act(() => api.patchDocument(doc.value!.slug, { title: newName.value.trim(), description: newDescription.value.trim() }));
  if (show(res)) renaming.value = false;
}

// ---- versions ----------------------------------------------------------------------------------------------------
const versions = ref<HistoryRow[] | null>(null);
async function showVersions() {
  if (!doc.value) return;
  const of = doc.value.slug;
  const rows = await act(() => api.documentHistory(of));
  if (of === slug.value) versions.value = rows ?? null;
}
const OPS: Record<string, string> = { create: "doc.op.create", update: "doc.op.update", archive: "doc.op.archive", restore: "doc.op.restore" };
async function goBack(version: number) {
  if (!doc.value) return;
  const res = await act(() => api.rollbackDocument(doc.value!.slug, version), t("doc.wentBack", { version }));
  if (show(res)) versions.value = null;
}

// ---- archive / restore / new ---------------------------------------------------------------------------------------
const archiving = ref(false);
async function archive() {
  if (!doc.value) return;
  const { title, slug: of } = doc.value;
  const res = await act(() => api.archiveDocument(of), t("doc.archived", { title }));
  archiving.value = false;
  if (res) {
    await loadArchived();
    if (of === slug.value) location.hash = "#/documents";
  }
}
async function restore(d: DocumentInfo) {
  const res = await act(() => api.restoreDocument(d.slug), t("doc.restored", { title: d.title }));
  if (res) await loadArchived();
}

const creating = ref(false);
const newTitle = ref("");
const newText = ref("");
const newPrivate = ref(false);
function startNew() {
  newTitle.value = "";
  newText.value = "";
  newPrivate.value = false;
  creating.value = true;
}
async function create() {
  if (!newTitle.value.trim() || newText.value.length > MAX) return;
  const res = await act(() => api.createDocument({ title: newTitle.value.trim(), content: newText.value, private: newPrivate.value }));
  if (res) {
    creating.value = false;
    location.hash = `#/documents/${encodeURIComponent(res.slug)}`;
  }
}

const number = (n: number) => n.toLocaleString("en");

// ---- the open document -------------------------------------------------------------------------------------------
// Leaving a document closes it and its dialogs at once (the page shows "Loading" until the next one arrives), then
// loads the next one; a refresh elsewhere (store.version) reloads it.
watch(slug, () => {
  doc.value = null;
  missing.value = false;
  editing.value = false;
  movedOn.value = false;
  renaming.value = false;
  versions.value = null;
  archiving.value = false;
});
watch([slug, () => store.version], load, { immediate: true });
</script>

<template>
  <div class="mx-auto max-w-[820px] space-y-5">
    <!-- one document -->
    <template v-if="slug">
      <a href="#/documents" class="inline-flex items-center gap-1 text-[13px] text-muted hover:text-ink">
        <AppIcon name="chevronRight" :size="13" class="rotate-180" />{{ t("doc.all") }}
      </a>
      <p v-if="missing" class="card px-6 py-10 text-center text-sm text-muted">{{ t("doc.missing") }}</p>
      <template v-else-if="doc">
        <PageHeader :title="doc.title" :subtitle="doc.description">
          <button v-if="!editing" class="btn btn-primary btn-sm" data-test="doc-edit" @click="startEdit">
            <AppIcon name="pencil" :size="14" />{{ t("doc.edit") }}
          </button>
        </PageHeader>

        <div v-if="waiting(doc.id)" class="notice notice-wait">
          <AppIcon name="clock" />
          <span class="min-w-0 flex-1">{{ t("doc.waiting") }}</span>
          <a href="#/changes" class="btn btn-sm">{{ t("doc.review") }}</a>
        </div>

        <div class="flex flex-wrap items-center gap-x-3 gap-y-1 text-[13px] text-muted">
          <span>{{ t("doc.size", { n: number(doc.chars) }) }} · {{ t("doc.changedBy", { who: who(doc.last_actor), when: relTime(doc.updated_at) }) }}</span>
          <span v-if="doc.private" class="chip chip-private"><AppIcon name="lock" :size="11" />{{ t("doc.private") }}</span>
        </div>

        <div v-if="editing" class="space-y-2">
          <div v-if="movedOn" class="notice notice-wait"><AppIcon name="warning" />{{ t("doc.movedOn") }}</div>
          <textarea
            v-model="draft"
            class="input min-h-[55vh] font-mono text-[13px] leading-relaxed"
            spellcheck="false"
            :aria-label="t('doc.text')"
            data-test="doc-text"
          />
          <div class="flex flex-wrap items-center gap-2">
            <button class="btn btn-primary btn-sm" :disabled="busy || tooLong" data-test="doc-save" @click="save">{{ t("common.save") }}</button>
            <button class="btn btn-sm" @click="editing = false">{{ t("common.cancel") }}</button>
            <span class="ml-auto text-xs" :class="tooLong ? 'text-danger-ink' : 'text-muted'">
              {{ t("doc.count", { n: number(draft.length), max: number(MAX) }) }}
            </span>
          </div>
          <DocumentDiff v-if="draft !== (doc.content ?? '')" :before="doc.content ?? ''" :after="draft" />
        </div>
        <div v-else class="card px-5 py-4">
          <p v-if="!doc.content" class="text-sm text-muted">{{ t("doc.empty") }}</p>
          <div v-else class="text-[15px] leading-relaxed break-words whitespace-pre-wrap" data-test="doc-body">{{ doc.content }}</div>
        </div>

        <div v-if="!editing" class="card divide-y divide-sep">
          <div class="flex items-center justify-between gap-4 px-4.5 py-3.5">
            <div class="min-w-0 space-y-0.5">
              <div class="font-semibold">{{ t("doc.private") }}</div>
              <p class="text-[13px] text-muted">{{ doc.private ? t("doc.privateOn") : t("doc.privateOff") }}</p>
            </div>
            <button role="switch" class="switch" :aria-checked="doc.private" :aria-label="t('doc.private')" data-test="doc-private" @click="togglePrivate">
              <span class="switch-knob" />
            </button>
          </div>
          <div class="flex flex-wrap items-center gap-2 px-4.5 py-3">
            <button class="btn btn-sm" @click="copy(doc.content ?? '')"><AppIcon name="copy" :size="14" />{{ t("common.copy") }}</button>
            <button class="btn btn-sm" @click="download"><AppIcon name="download" :size="14" />{{ t("doc.download") }}</button>
            <button class="btn btn-sm" @click="startRename">{{ t("doc.rename") }}</button>
            <button class="btn btn-sm" data-test="doc-versions" @click="showVersions"><AppIcon name="clock" :size="14" />{{ t("doc.versions") }}</button>
            <button class="btn btn-danger btn-sm sm:ml-auto" @click="archiving = true">{{ t("doc.archive") }}</button>
          </div>
          <p class="px-4.5 py-3 text-[13px] text-muted">{{ t("doc.fileHelp", { file: `memories/${doc.slug}.md` }) }}</p>
        </div>
      </template>
      <p v-else class="text-sm text-muted">{{ t("common.loading") }}</p>
    </template>

    <!-- the list -->
    <template v-else>
      <PageHeader :title="t('nav.documents')" :subtitle="t('doc.subtitle')">
        <button class="btn btn-primary btn-sm" data-test="doc-new" @click="startNew"><AppIcon name="plus" :size="14" />{{ t("doc.new") }}</button>
      </PageHeader>
      <div v-if="!docs.length" class="card space-y-3 px-6 py-10 text-center text-sm text-muted">
        <p>{{ t("doc.none") }}</p>
        <a href="#/import" class="btn btn-sm">{{ t("doc.importOne") }}</a>
      </div>
      <ul v-else class="card divide-y divide-sep" data-test="doc-list">
        <li v-for="d in docs" :key="d.id">
          <a :href="`#/documents/${encodeURIComponent(d.slug)}`" class="flex items-center gap-3 px-4.5 py-3.5 hover:bg-fill">
            <AppIcon name="file" :size="16" class="shrink-0 text-muted" />
            <span class="min-w-0 flex-1 space-y-0.5">
              <span class="flex items-center gap-1.5 font-semibold">
                {{ d.title }}<AppIcon v-if="d.private" name="lock" :size="12" class="text-muted" />
              </span>
              <span class="block text-[13px] text-muted">
                {{ t("doc.size", { n: number(d.chars) }) }} · {{ t("doc.changedBy", { who: who(d.last_actor), when: relTime(d.updated_at) }) }}
              </span>
            </span>
            <span v-if="waiting(d.id)" class="chip chip-wait">{{ t("doc.waitingChip") }}</span>
            <AppIcon name="chevronRight" :size="14" class="text-muted" />
          </a>
        </li>
      </ul>
      <details v-if="archived.length" class="card px-4.5 py-3">
        <summary class="cursor-pointer text-sm font-semibold">{{ t("doc.archivedList", { n: archived.length }) }}</summary>
        <ul class="mt-2 divide-y divide-sep">
          <li v-for="d in archived" :key="d.id" class="flex items-center gap-3 py-2.5 text-sm">
            <span class="min-w-0 flex-1">{{ d.title }} <span class="text-muted">· {{ relTime(d.archived_at) }}</span></span>
            <button class="btn btn-sm" @click="restore(d)">{{ t("doc.restore") }}</button>
          </li>
        </ul>
      </details>
    </template>

    <ModalDialog v-if="creating" :title="t('doc.new')" wide @close="creating = false">
      <form class="space-y-3" @submit.prevent="create">
        <input v-model="newTitle" class="input" :placeholder="t('doc.titlePlaceholder')" :aria-label="t('doc.title')" data-test="doc-new-title" />
        <textarea v-model="newText" class="input min-h-64 font-mono text-[13px]" :placeholder="t('doc.textPlaceholder')" :aria-label="t('doc.text')" />
        <label class="flex items-center gap-2.5 text-sm"><input v-model="newPrivate" type="checkbox" class="check" />{{ t("doc.privateNew") }}</label>
        <div class="flex justify-end gap-2">
          <button type="button" class="btn" @click="creating = false">{{ t("common.cancel") }}</button>
          <button type="submit" class="btn btn-primary" :disabled="!newTitle.trim() || newText.length > MAX">{{ t("doc.create") }}</button>
        </div>
      </form>
    </ModalDialog>

    <ModalDialog v-if="renaming" :title="t('doc.rename')" @close="renaming = false">
      <form class="space-y-3" @submit.prevent="rename">
        <input v-model="newName" class="input" :aria-label="t('doc.title')" />
        <input v-model="newDescription" class="input" :placeholder="t('doc.descriptionPlaceholder')" :aria-label="t('doc.description')" />
        <div class="flex justify-end gap-2">
          <button type="button" class="btn" @click="renaming = false">{{ t("common.cancel") }}</button>
          <button type="submit" class="btn btn-primary" :disabled="!newName.trim()">{{ t("common.save") }}</button>
        </div>
      </form>
    </ModalDialog>

    <ModalDialog v-if="versions" :title="t('doc.versions')" wide @close="versions = null">
      <ol class="divide-y divide-sep" data-test="doc-version-list">
        <li v-for="(v, i) in versions" :key="v.version" class="space-y-2 py-3 text-sm">
          <div class="flex flex-wrap items-center gap-2">
            <span class="font-semibold">{{ t("doc.version", { n: v.version }) }}</span>
            <span class="text-muted">{{ t(OPS[v.op] ?? "doc.op.update", { who: who(v.actor) }) }} · {{ relTime(v.at) }}</span>
            <button v-if="i > 0 && v.op !== 'archive'" class="btn btn-sm ml-auto" @click="goBack(v.version)">{{ t("doc.goBack") }}</button>
          </div>
          <DocumentDiff v-if="versions[i + 1] && v.op === 'update'" :before="versions[i + 1].content" :after="v.content" />
        </li>
      </ol>
    </ModalDialog>

    <ModalDialog v-if="archiving && doc" :title="t('doc.archiveTitle', { title: doc.title })" @close="archiving = false">
      <div class="space-y-4 text-sm">
        <p>{{ t("doc.archiveHelp") }}</p>
        <div class="flex justify-end gap-2">
          <button type="button" class="btn" @click="archiving = false">{{ t("common.cancel") }}</button>
          <button type="button" class="btn btn-danger" @click="archive">{{ t("doc.archive") }}</button>
        </div>
      </div>
    </ModalDialog>
  </div>
</template>
