<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { api, type AssistantFeature, type AssistantKind, type AssistantPatch, type AssistantStatus, type AssistantTest } from "../api";
import { t } from "../i18n";
import { store, toast } from "../store";
import AppIcon from "./AppIcon.vue";
import SemanticSettings from "./SemanticSettings.vue";

/**
 * Settings → Assistant (product-design.md §17.2): off, a model on this computer, your own API key, or MyMemo's hosted
 * model (coming soon); its endpoint, model and key (write-only), a Test button, a switch per feature, the private
 * topics a model on this computer may read, monthly limits and this month's usage, and where memory text goes.
 * Its embeddings model (optional) and the local model for search by meaning (`SemanticSettings`, shown even with
 * the Assistant off) are here too.
 */
type Choice = "off" | AssistantKind;
const CHOICES: Choice[] = ["off", "local", "key", "mymemo"];
const FEATURES: AssistantFeature[] = ["import", "sort", "recall", "check", "session", "tidy"];

const status = ref<AssistantStatus | null>(null);
const busy = ref(false);
const draft = ref({ protocol: "openai" as "openai" | "anthropic", base_url: "", model: "", api_key: "", embeddings_model: "" });
const caps = ref({ calls: "", tokens: "" });
const tested = ref<AssistantTest | null>(null);
const testing = ref(false);

function take(s: AssistantStatus) {
  status.value = s;
  draft.value = { protocol: s.protocol, base_url: s.base_url, model: s.model, api_key: "", embeddings_model: s.embeddings_model ?? "" };
  caps.value = { calls: s.caps.calls == null ? "" : String(s.caps.calls), tokens: s.caps.tokens == null ? "" : String(s.caps.tokens) };
}

onMounted(async () => {
  try {
    take(await api.assistant());
  } catch {
    status.value = null; // an older server without the Assistant: nothing to set
  }
});

const choice = computed<Choice>(() => (status.value?.enabled ? status.value.kind : "off"));
const locked = (field: string) => !!status.value?.from_env.includes(field);
const privateTopics = computed(() => (store.state?.topics ?? []).filter((tp) => tp.private));
const changed = computed(() => {
  const s = status.value;
  if (!s) return false;
  const d = draft.value;
  return d.api_key.trim() !== "" || d.model.trim() !== s.model || d.base_url.trim().replace(/\/+$/, "") !== s.base_url
    || (s.kind === "key" && d.protocol !== s.protocol) || d.embeddings_model.trim() !== (s.embeddings_model ?? "");
});

async function change(body: AssistantPatch, done?: string) {
  if (busy.value) return;
  busy.value = true;
  try {
    take(await api.assistantSettings(body));
    tested.value = null;
    if (done) toast(done);
  } catch (e) {
    toast(e instanceof Error ? e.message : String(e), "error");
  } finally {
    busy.value = false;
  }
}

function pick(c: Choice) {
  if (c === "mymemo" || c === choice.value) return;
  void change(c === "off" ? { enabled: false } : { enabled: true, kind: c });
}

function save() {
  const d = draft.value;
  const body: AssistantPatch = { model: d.model.trim(), base_url: d.base_url.trim() };
  if (d.embeddings_model.trim() !== (status.value?.embeddings_model ?? "")) body.embeddings_model = d.embeddings_model.trim();
  if (status.value?.kind === "key") body.protocol = d.protocol;
  if (d.api_key.trim()) body.api_key = d.api_key.trim();
  void change(body, t("ast.saved"));
}

const removeKey = () => change({ api_key: "" }, t("ast.keyRemoved"));
const toggleFeature = (f: AssistantFeature) => status.value && change({ features: { [f]: !status.value.features[f] } });

function togglePrivate(slug: string) {
  const s = status.value;
  if (!s) return;
  const next = s.private_topics.includes(slug) ? s.private_topics.filter((x) => x !== slug) : [...s.private_topics, slug];
  void change({ private_topics: next });
}

function saveCaps() {
  const n = (v: string) => (v.trim() === "" ? 0 : Math.max(0, Math.floor(Number(v)) || 0)); // 0: no limit
  void change({ caps: { calls: n(caps.value.calls), tokens: n(caps.value.tokens) } }, t("ast.saved"));
}

async function test() {
  testing.value = true;
  tested.value = null;
  try {
    tested.value = await api.assistantTest();
    take(await api.assistant()); // the test counts as a call
  } catch (e) {
    tested.value = { ok: false, model: null, ms: null, error: e instanceof Error ? e.message : String(e) };
  } finally {
    testing.value = false;
  }
}

const allowAgain = () => change({ enabled: true });
const fmt = (n: number) => n.toLocaleString("en-US");
</script>

<template>
  <section v-if="status" class="space-y-1.5" :aria-label="t('ast.title')">
    <h3 class="label px-1">{{ t("ast.title") }}</h3>
    <div class="card divide-y divide-sep">
      <div class="space-y-3 px-4.5 py-3.5">
        <p class="text-[13px] text-muted">{{ t("ast.intro") }}</p>
        <div class="seg flex-wrap" role="radiogroup" :aria-label="t('ast.title')">
          <button
            v-for="c in CHOICES"
            :key="c"
            type="button"
            class="seg-item disabled:opacity-50"
            :class="{ 'is-on': choice === c }"
            role="radio"
            :aria-checked="choice === c"
            :disabled="busy || (c !== 'off' && !status.kinds[c]) || locked('enabled') || (c !== 'off' && locked('kind') && status.kind !== c)"
            @click="pick(c)"
          >{{ t(`ast.choice.${c}`) }}</button>
        </div>
        <p class="flex items-start gap-2 text-[13px]" data-test="where">
          <AppIcon :name="status.enabled && !status.local ? 'info' : 'lock'" :size="14" class="mt-0.5 shrink-0 text-muted" />
          <span>{{ status.where }}</span>
        </p>
        <p v-if="status.from_env.length" class="text-xs text-muted">{{ t("ast.fromEnv", { fields: status.from_env.join(", ") }) }}</p>
      </div>

      <div v-if="status.enabled && status.revoked" class="notice notice-wait m-3.5 flex-wrap justify-between gap-2">
        <span>{{ t("ast.revoked") }}</span>
        <button type="button" class="btn btn-sm" :disabled="busy" @click="allowAgain">{{ t("ast.allowAgain") }}</button>
      </div>

      <form v-if="status.enabled" class="space-y-3 px-4.5 py-3.5" @submit.prevent="save">
        <div v-if="status.kind === 'key'" class="space-y-1.5">
          <span class="text-[13px] font-semibold">{{ t("ast.protocol") }}</span>
          <div class="seg" role="radiogroup" :aria-label="t('ast.protocol')">
            <button
              v-for="p in (['anthropic', 'openai'] as const)"
              :key="p"
              type="button"
              class="seg-item"
              :class="{ 'is-on': draft.protocol === p }"
              role="radio"
              :aria-checked="draft.protocol === p"
              :disabled="locked('protocol')"
              @click="draft.protocol = p"
            >{{ t(`ast.protocol.${p}`) }}</button>
          </div>
        </div>
        <label class="block space-y-1.5">
          <span class="text-[13px] font-semibold">{{ t("ast.baseUrl") }}</span>
          <input
            v-model="draft.base_url"
            type="url"
            class="input font-mono text-xs"
            :placeholder="status.base_url ? '' : status.endpoint"
            :disabled="locked('base_url') || busy"
            spellcheck="false"
            autocomplete="off"
          />
          <span class="block text-xs text-muted">{{ t(status.kind === "local" ? "ast.baseUrlLocal" : "ast.baseUrlKey") }}</span>
        </label>
        <label class="block space-y-1.5">
          <span class="text-[13px] font-semibold">{{ t("ast.model") }}</span>
          <input
            v-model="draft.model"
            class="input font-mono text-xs"
            :placeholder="t(status.kind === 'local' ? 'ast.modelLocal' : 'ast.modelKey')"
            :disabled="locked('model') || busy"
            spellcheck="false"
            autocomplete="off"
          />
        </label>
        <label v-if="status.kind === 'local' || (status.kind === 'key' && draft.protocol === 'openai')" class="block space-y-1.5">
          <span class="text-[13px] font-semibold">{{ t("ast.embeddings") }}</span>
          <input
            v-model="draft.embeddings_model"
            class="input font-mono text-xs"
            :placeholder="t('ast.embeddingsNone')"
            :disabled="locked('embeddings_model') || busy"
            spellcheck="false"
            autocomplete="off"
            data-test="embeddings"
          />
          <span class="block text-xs text-muted">{{ t("ast.embeddingsHelp") }}</span>
        </label>
        <label v-if="status.kind === 'key'" class="block space-y-1.5">
          <span class="text-[13px] font-semibold">{{ t("ast.key") }}</span>
          <input
            v-model="draft.api_key"
            type="password"
            class="input font-mono text-xs"
            :placeholder="status.has_key ? t(`ast.keySaved.${status.key_store ?? 'file'}`) : t('ast.keyNone')"
            :disabled="locked('api_key') || busy"
            autocomplete="new-password"
            spellcheck="false"
          />
          <span class="block text-xs text-muted">{{ t("ast.keyHelp") }}</span>
        </label>
        <div class="flex flex-wrap items-center gap-2 pt-1">
          <button type="submit" class="btn btn-sm" :disabled="busy || !changed">{{ t("common.save") }}</button>
          <button type="button" class="btn btn-sm" :disabled="busy || testing || changed || !!status.problem" @click="test">
            {{ testing ? t("ast.testing") : t("ast.test") }}
          </button>
          <button v-if="status.kind === 'key' && status.has_key && !locked('api_key')" type="button" class="btn btn-ghost btn-sm" :disabled="busy" @click="removeKey">
            {{ t("ast.removeKey") }}
          </button>
        </div>
        <p v-if="status.problem" class="text-[13px] text-wait-ink">{{ status.problem }}</p>
        <p v-if="tested" class="text-[13px]" :class="tested.ok ? 'text-fresh' : 'text-danger'" data-test="tested">
          {{ tested.ok ? t("ast.testOk", { model: tested.model ?? "", ms: tested.ms ?? 0 }) : tested.error }}
        </p>
      </form>

      <div v-if="status.enabled" class="divide-y divide-sep">
        <div v-for="f in FEATURES" :key="f" class="flex items-center gap-4 px-4.5 py-3">
          <div class="min-w-0 flex-1 space-y-0.5">
            <div class="font-semibold">{{ t(`ast.feature.${f}`) }}</div>
            <p class="text-[13px] text-muted">{{ t(`ast.featureHelp.${f}`) }}</p>
          </div>
          <button
            role="switch"
            type="button"
            class="switch disabled:opacity-50"
            :aria-checked="status.features[f]"
            :aria-label="t(`ast.feature.${f}`)"
            :disabled="busy || locked('features')"
            @click="toggleFeature(f)"
          >
            <span class="switch-knob" />
          </button>
        </div>
      </div>

      <div v-if="status.enabled && privateTopics.length" class="space-y-2 px-4.5 py-3.5">
        <div class="font-semibold">{{ t("ast.private") }}</div>
        <template v-if="status.kind === 'local'">
          <p class="text-[13px] text-muted">{{ t("ast.privateLocal") }}</p>
          <div class="flex flex-wrap gap-1.5">
            <button
              v-for="tp in privateTopics"
              :key="tp.slug"
              type="button"
              class="pill"
              :class="status.private_topics.includes(tp.slug) ? 'pill-on' : 'pill-off'"
              :aria-pressed="status.private_topics.includes(tp.slug)"
              :disabled="busy"
              @click="togglePrivate(tp.slug)"
            >
              <AppIcon name="lock" :size="12" />{{ tp.title }}
            </button>
          </div>
        </template>
        <p v-else class="text-[13px] text-muted">{{ t("ast.privateRemote") }}</p>
      </div>

      <form v-if="status.enabled" class="space-y-2 px-4.5 py-3.5" @submit.prevent="saveCaps">
        <div class="font-semibold">{{ t("ast.limits") }}</div>
        <p class="text-[13px] text-muted" data-test="usage">
          {{ t("ast.usage", { calls: fmt(status.usage.calls), tokens: fmt(status.usage.tokens) }) }}
        </p>
        <div class="flex flex-wrap items-end gap-3">
          <label class="space-y-1">
            <span class="block text-xs text-muted">{{ t("ast.capCalls") }}</span>
            <input v-model="caps.calls" inputmode="numeric" class="input w-32 text-sm" :placeholder="t('ast.noLimit')" :disabled="locked('caps.calls') || busy" />
          </label>
          <label class="space-y-1">
            <span class="block text-xs text-muted">{{ t("ast.capTokens") }}</span>
            <input v-model="caps.tokens" inputmode="numeric" class="input w-36 text-sm" :placeholder="t('ast.noLimit')" :disabled="locked('caps.tokens') || busy" />
          </label>
          <button type="submit" class="btn btn-sm" :disabled="busy">{{ t("common.save") }}</button>
        </div>
        <p class="text-xs text-muted">{{ t("ast.limitsHelp") }}</p>
      </form>

      <SemanticSettings />
    </div>
  </section>
</template>
