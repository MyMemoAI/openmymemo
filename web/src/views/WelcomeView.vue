<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from "vue";
import { api, ApiError, type Kind, type Memory } from "../api";
import AppIcon from "../components/AppIcon.vue";
import ConnectApps from "../components/ConnectApps.vue";
import ImportPaste from "../components/ImportPaste.vue";
import PageHeader from "../components/PageHeader.vue";
import { native, shell } from "../desktop";
import type { IconName } from "../icons";
import { t, tn } from "../i18n";
import { act, copy, importTopicTitle, refresh, store, toast, topicsBySlug, topicTitle } from "../store";

/**
 * The first-run guide (product-design.md §15.3): what OpenMyMemo is, connecting AI apps, bringing memory
 * over, a few things about the person, what happens next. Every step can be skipped. Finishing or skipping
 * sets `onboarded`, which takes the guide out of the navigation and off the home page.
 */
const props = defineProps<{ initialStep: string | null }>();

const STEPS = ["welcome", "connect", "import", "about", "done"] as const;
type Step = (typeof STEPS)[number];
const stepOf = (name: string | null): Step => STEPS.find((s) => s === name) ?? "welcome";

const step = ref<Step>(stepOf(props.initialStep));
const index = computed(() => STEPS.indexOf(step.value));
const root = ref<HTMLElement | null>(null);

watch(() => props.initialStep, (v) => (step.value = stepOf(v)));
// Each step starts at the top, and keyboard and screen-reader users start it at its heading.
watch(step, async (s) => {
  await nextTick();
  root.value?.scrollIntoView?.({ block: "start" });
  root.value?.querySelector<HTMLElement>(`[data-step="${s}"] h2`)?.focus({ preventScroll: true });
});

/** Each step has its own URL (`#/welcome/import`), so the browser's back button goes back a step. */
function go(s: Step) {
  step.value = s;
  const hash = s === "welcome" ? "#/welcome" : `#/welcome/${s}`;
  if (location.hash !== hash) location.hash = hash;
}

let disposed = false;
onBeforeUnmount(() => (disposed = true));

onMounted(() => {
  // A folder without the key counts as onboarded once it has a connector; write the `false` down now, so
  // connecting an app in step 2 doesn't end the guide early. Reopened later, the guide leaves it alone.
  if (store.state?.settings.onboarded === false) api.patchSettings({ onboarded: false }).catch(() => {});
});

async function finish() {
  if (await act(() => api.patchSettings({ onboarded: true }))) location.hash = "#/memories";
}

const vault = computed(() => store.state?.vault ?? "");
async function reveal() {
  try {
    await shell.reveal(vault.value);
  } catch (e) {
    toast(e instanceof Error ? e.message : String(e), "error");
  }
}

const POINTS: { id: string; icon: IconName }[] = [
  { id: "folder", icon: "memories" },
  { id: "apps", icon: "plug" },
  { id: "review", icon: "changes" },
];

// ---- step 3: bring memory from another AI ----

const SOURCES = ["chatgpt", "claude", "gemini", "text"] as const;
const source = ref<(typeof SOURCES)[number]>("chatgpt");
const prompt = computed(() => t(source.value === "claude" ? "onb.import.promptClaude" : "onb.import.prompt"));
/** The last import: how many landed in `from-<source>`, and how many in topics the source named. */
const imported = ref<{ source: string; n: number; named: number } | null>(null);

function pickSource(s: (typeof SOURCES)[number]) {
  source.value = s;
  imported.value = null;
}

// ---- step 4: a few things about the person, saved as their own (protected) memories ----

const FIELDS: { id: "name" | "work" | "answers"; topic: string; kind: Kind }[] = [
  { id: "name", topic: "identity", kind: "preference" },
  { id: "work", topic: "work", kind: "fact" },
  { id: "answers", topic: "how-to-work-with-me", kind: "instruction" },
];
type Field = (typeof FIELDS)[number];
const answers = reactive({ name: "", work: "", answers: "" });
const saving = ref(false);
/** The memory the guide saved for each answer, its text and version then, so a correction updates that memory. */
const savedFor = reactive<Partial<Record<Field["id"], { id: string; content: string; version: number }>>>({});

/** One self-contained statement around the person's own words, in whatever language they wrote them. */
function statement(id: Field["id"]): string {
  const text = answers[id].split(/\s+/).join(" ").trim();
  if (!text) return "";
  return t(`onb.about.${id}.statement`, { text }) + (/[.!?。！？…]$/.test(text) ? "" : ".");
}

type Plan =
  | { op: "none" | "add"; content: string }
  | { op: "keep"; id: string; content: string; version: number }
  | { op: "update" | "archive"; id: string; content: string; version: number };

/**
 * What saving does with an answer. The memory the guide saved for it earlier (after Back) is corrected, or
 * archived when the answer is cleared, as long as it is still as the guide left it; if someone changed or
 * archived it meanwhile, it is left alone and a new answer becomes a new memory. An identical memory already
 * in the topic is kept, and corrected from then on. The panel's copy of the memories can be a few seconds old,
 * so the server checks the version again when saving (see saveAbout).
 */
function plan(f: Field): Plan {
  const content = statement(f.id);
  const prev = savedFor[f.id];
  if (prev && store.memories.some((m) => m.id === prev.id && m.content === prev.content && m.version === prev.version)) {
    if (!content) return { op: "archive", id: prev.id, content: prev.content, version: prev.version };
    return content === prev.content ? { op: "none", content } : { op: "update", id: prev.id, content, version: prev.version };
  }
  if (!content) return { op: "none", content };
  const same = store.memories.find((m) => m.topic === f.topic && m.content === content);
  return same ? { op: "keep", id: same.id, content, version: same.version } : { op: "add", content };
}
const plans = computed(() => Object.fromEntries(FIELDS.map((f) => [f.id, plan(f)])) as Record<Field["id"], Plan>);
const unsaved = computed(() => FIELDS.some((f) => ["add", "update", "archive"].includes(plans.value[f.id].op)));

const changedMeanwhile = (e: unknown) => e instanceof ApiError && (e as ApiError).status === 409;

/** Put what a save returned into the panel's copy at once, so a second save can't plan from the old one. */
function remember(m: Memory) {
  const i = store.memories.findIndex((x) => x.id === m.id);
  if (i >= 0) store.memories.splice(i, 1, m);
  else store.memories.push(m);
}
function forget(id: string) {
  store.memories = store.memories.filter((x) => x.id !== id);
}

async function saveAbout(next: Step = "done") {
  if (saving.value) return; // a second Enter, or key repeat, while the first save or its refresh is on its way
  saving.value = true;
  const from = location.hash; // where the person was; if they move on meanwhile, the save doesn't move them back
  let saved = 0;
  let archived = 0;
  try {
    for (const f of FIELDS) {
      const p = plans.value[f.id];
      const add = async (content: string) => {
        // A folder whose topic was removed still gets the memory, filed automatically.
        const m = await api.addMemory({ topic: topicsBySlug.value[f.topic] ? f.topic : undefined, content, kind: f.kind });
        remember(m);
        savedFor[f.id] = { id: m.id, content: m.content, version: m.version };
        saved++;
      };
      if (p.op === "keep") {
        savedFor[f.id] = { id: p.id, content: p.content, version: p.version };
      } else if (p.op === "add") {
        await add(p.content);
      } else if (p.op === "update") {
        try {
          // As the person, so it stays protected; only if it is still the version the guide saved.
          const m = await api.patchMemory(p.id, { content: p.content, expected_version: p.version });
          remember(m);
          savedFor[f.id] = { id: m.id, content: m.content, version: m.version };
          saved++;
        } catch (e) {
          if (!changedMeanwhile(e)) throw e;
          await add(p.content); // someone else's edit stays; the new answer is a memory of its own
        }
      } else if (p.op === "archive") {
        try {
          await api.archive(p.id, p.version);
          forget(p.id);
          archived++;
        } catch (e) {
          if (!changedMeanwhile(e)) throw e; // changed or archived by someone else: left alone
        }
        delete savedFor[f.id];
      }
    }
  } catch (e) {
    toast(e instanceof Error ? e.message : String(e), "error");
    return;
  } finally {
    try {
      if (saved || archived) await refresh(); // refresh() never throws; a failed one leaves what remember() put in
    } finally {
      saving.value = false;
    }
  }
  const done = [saved && tn("onb.about.saved", saved), archived && tn("onb.about.archived", archived)].filter(Boolean);
  if (done.length) toast(done.join(" · "));
  if (!disposed && location.hash === from) go(next);
}

function onFieldEnter(e: KeyboardEvent, i: number) {
  if (e.isComposing) return;
  e.preventDefault();
  if (i < FIELDS.length - 1) root.value?.querySelector<HTMLInputElement>(`input[name="${FIELDS[i + 1].id}"]`)?.focus();
  else saveAbout();
}

/** The step bar: moving on past "About you" saves what was typed there, as Next does, instead of dropping it. */
function jump(s: Step) {
  if (step.value === "about" && unsaved.value && STEPS.indexOf(s) > index.value) saveAbout(s);
  else go(s);
}

function next() {
  if (step.value === "about") saveAbout();
  else go(STEPS[index.value + 1]);
}
</script>

<template>
  <div ref="root" class="mx-auto max-w-[680px] space-y-7">
    <PageHeader :title="t('onb.title')" :subtitle="t('onb.stepOf', { n: index + 1, total: STEPS.length })">
      <button v-if="step !== 'done'" type="button" class="btn btn-quiet btn-sm" @click="finish">{{ t("onb.skip") }}</button>
    </PageHeader>

    <nav :aria-label="t('onb.steps')">
      <ol class="grid grid-cols-5 gap-1.5 sm:gap-2">
        <li v-for="(s, i) in STEPS" :key="s">
          <button
            type="button"
            class="flex w-full flex-col gap-1.5 rounded-md py-1 text-left"
            :aria-label="t('onb.stepLabel', { n: i + 1, name: t(`onb.step.${s}`) })"
            :aria-current="s === step ? 'step' : undefined"
            @click="jump(s)"
          >
            <span class="h-1 w-full rounded-full" :class="i === index ? 'bg-accent' : i < index ? 'bg-accent/35' : 'bg-fill-strong'" />
            <span class="hidden truncate text-xs sm:block" :class="i === index ? 'font-semibold text-ink' : 'text-muted'">
              {{ t(`onb.step.${s}`) }}
            </span>
          </button>
        </li>
      </ol>
    </nav>

    <!-- 1. what OpenMyMemo is, and where the folder is -->
    <section v-show="step === 'welcome'" data-step="welcome" class="space-y-4">
      <h2 tabindex="-1" class="section-title outline-none">{{ t("onb.welcome.title") }}</h2>
      <ul class="card divide-y divide-sep">
        <li v-for="p in POINTS" :key="p.id" class="flex gap-3.5 px-4.5 py-3.5">
          <AppIcon :name="p.icon" :size="20" class="mt-0.5 text-accent" />
          <div class="space-y-0.5">
            <p class="font-semibold">{{ t(`onb.welcome.${p.id}.title`) }}</p>
            <p class="text-[13px] text-muted">{{ t(`onb.welcome.${p.id}.text`) }}</p>
          </div>
        </li>
      </ul>
      <div class="flex flex-wrap items-center gap-x-3 gap-y-2 text-[13px]">
        <span class="text-muted">{{ t("onb.folder") }}</span>
        <span class="min-w-0 font-mono text-xs break-all">{{ vault }}</span>
        <button v-if="native" type="button" class="btn btn-sm" @click="reveal">
          <AppIcon name="memories" :size="14" />{{ t("ins.showInFinder") }}
        </button>
      </div>
    </section>

    <!-- 2. connect the AI apps on this computer -->
    <section v-show="step === 'connect'" data-step="connect" class="space-y-4">
      <div class="space-y-1">
        <h2 tabindex="-1" class="section-title outline-none">{{ t("onb.connect.title") }}</h2>
        <p class="text-[13px] text-muted">{{ t("onb.connect.lead") }}</p>
      </div>
      <ConnectApps compact />
    </section>

    <!-- 3. bring memory over from ChatGPT, Claude, Gemini or anything else -->
    <section v-show="step === 'import'" data-step="import" class="space-y-4">
      <div class="space-y-1">
        <h2 tabindex="-1" class="section-title outline-none">{{ t("onb.import.title") }}</h2>
        <p class="text-[13px] text-muted">{{ t("onb.import.lead") }}</p>
      </div>
      <div class="seg" role="radiogroup" :aria-label="t('onb.import.source')">
        <button
          v-for="s in SOURCES"
          :key="s"
          type="button"
          class="seg-item"
          :class="{ 'is-on': source === s }"
          role="radio"
          :aria-checked="source === s"
          @click="pickSource(s)"
        >{{ t(`onb.import.src.${s}`) }}</button>
      </div>

      <div v-if="imported" class="card space-y-3 p-4.5">
        <div class="flex gap-3">
          <AppIcon name="changes" :size="22" class="mt-px text-fresh" />
          <div class="space-y-0.5">
            <p class="font-semibold">{{ tn("onb.import.done", imported.n, { topic: importTopicTitle(imported.source) }) }}</p>
            <p v-if="imported.named" class="text-[13px] text-muted">{{ tn("imp.doneNamed", imported.named) }}</p>
            <p class="text-[13px] text-muted">{{ t("onb.import.doneHint") }}</p>
          </div>
        </div>
        <div class="pl-[34px]">
          <button type="button" class="btn btn-sm" @click="imported = null">{{ t("onb.import.more") }}</button>
        </div>
      </div>
      <template v-else>
        <div class="space-y-2">
          <p class="text-[13px] text-muted">{{ t(`onb.import.where.${source}`) }}</p>
          <div class="flex items-center gap-2 rounded-[10px] bg-fill py-1 pr-1 pl-3.5">
            <p class="min-w-0 flex-1 py-1.5 text-sm">{{ prompt }}</p>
            <button type="button" class="btn btn-ghost btn-icon" :aria-label="t('onb.import.copy')" :title="t('onb.import.copy')" @click="copy(prompt)">
              <AppIcon name="copy" :size="15" />
            </button>
          </div>
          <p class="text-[13px] text-muted">{{ t("onb.import.paste") }}</p>
        </div>
        <ImportPaste :source="source" @imported="imported = { source, ...$event }" />
      </template>
    </section>

    <!-- 4. a few things about the person -->
    <section v-show="step === 'about'" data-step="about" class="space-y-4">
      <div class="space-y-1">
        <h2 tabindex="-1" class="section-title outline-none">{{ t("onb.about.title") }}</h2>
        <p class="text-[13px] text-muted">{{ t("onb.about.lead") }}</p>
      </div>
      <div class="card divide-y divide-sep">
        <div v-for="(f, i) in FIELDS" :key="f.id" class="space-y-1.5 px-4.5 py-3.5">
          <label :for="`onb-${f.id}`" class="block text-sm font-semibold">{{ t(`onb.about.${f.id}.label`) }}</label>
          <input
            :id="`onb-${f.id}`"
            v-model="answers[f.id]"
            type="text"
            maxlength="300"
            autocomplete="off"
            class="input"
            :name="f.id"
            :placeholder="t(`onb.about.${f.id}.placeholder`)"
            :aria-describedby="statement(f.id) || plans[f.id].op === 'archive' ? `onb-${f.id}-saved` : undefined"
            @keydown.enter="onFieldEnter($event, i)"
          />
          <p v-if="statement(f.id)" :id="`onb-${f.id}-saved`" class="text-xs text-muted">
            {{ t("onb.about.savedAs", { topic: topicTitle(f.topic), text: statement(f.id) }) }}
          </p>
          <p v-else-if="plans[f.id].op === 'archive'" :id="`onb-${f.id}-saved`" class="text-xs text-muted">
            {{ t("onb.about.willArchive", { text: plans[f.id].content }) }}
          </p>
        </div>
      </div>
    </section>

    <!-- 5. what happens next -->
    <section v-show="step === 'done'" data-step="done" class="space-y-4">
      <h2 tabindex="-1" class="section-title outline-none">{{ t("onb.done.title") }}</h2>
      <ul class="card divide-y divide-sep">
        <li class="flex gap-3.5 px-4.5 py-3.5">
          <AppIcon name="plug" :size="20" class="mt-0.5 text-accent" />
          <div class="min-w-0 flex-1 space-y-2">
            <div class="space-y-0.5">
              <p class="font-semibold">{{ t("onb.done.try.title") }}</p>
              <p class="text-[13px] text-muted">{{ t("onb.done.try.text") }}</p>
            </div>
            <div class="flex items-center gap-2 rounded-[10px] bg-fill py-1 pr-1 pl-3.5">
              <p class="min-w-0 flex-1 py-1.5 text-sm">{{ t("onb.done.ask") }}</p>
              <button type="button" class="btn btn-ghost btn-icon" :aria-label="t('common.copy')" :title="t('common.copy')" @click="copy(t('onb.done.ask'))">
                <AppIcon name="copy" :size="15" />
              </button>
            </div>
          </div>
        </li>
        <li class="flex gap-3.5 px-4.5 py-3.5">
          <AppIcon name="changes" :size="20" class="mt-0.5 text-accent" />
          <div class="space-y-0.5">
            <p class="font-semibold">{{ t("onb.done.changes.title") }}</p>
            <p class="text-[13px] text-muted">{{ t("onb.done.changes.text") }}</p>
          </div>
        </li>
        <li class="flex gap-3.5 px-4.5 py-3.5">
          <AppIcon name="memories" :size="20" class="mt-0.5 text-accent" />
          <div class="min-w-0 flex-1 space-y-2">
            <div class="space-y-0.5">
              <p class="font-semibold">{{ t("onb.done.folder.title") }}</p>
              <p class="text-[13px] text-muted">{{ t("onb.done.folder.text") }}</p>
            </div>
            <p class="font-mono text-xs break-all">{{ vault }}</p>
            <button v-if="native" type="button" class="btn btn-sm" @click="reveal">
              <AppIcon name="memories" :size="14" />{{ t("ins.showInFinder") }}
            </button>
          </div>
        </li>
      </ul>
    </section>

    <div class="flex items-center justify-between gap-3 border-t border-sep pt-5">
      <button v-if="index > 0" type="button" class="btn" @click="go(STEPS[index - 1])">{{ t("onb.back") }}</button>
      <span v-else />
      <button v-if="step === 'done'" type="button" class="btn btn-primary" @click="finish">{{ t("onb.finish") }}</button>
      <button
        v-else
        type="button"
        class="btn"
        :class="(step === 'import' && !imported) || (step === 'connect' && !store.connectors.length) ? '' : 'btn-primary'"
        :disabled="saving"
        @click="next"
      >
        {{ step === "about" && unsaved ? t("onb.saveNext") : t("onb.next") }}<AppIcon name="arrowRight" :size="14" />
      </button>
    </div>
  </div>
</template>
