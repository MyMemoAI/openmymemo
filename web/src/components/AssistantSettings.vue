<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import {
  api, type AssistantAccount, type AssistantFeature, type AssistantKind, type AssistantPatch, type AssistantStatus,
  type AssistantTest,
} from "../api";
import { desktop } from "../desktop";
import { t } from "../i18n";
import { store, toast } from "../store";
import AppIcon from "./AppIcon.vue";
import SessionConsent from "./SessionConsent.vue";
import SemanticSettings from "./SemanticSettings.vue";

/**
 * Settings → Assistant (product-design.md §17.2): off, a model on this computer, your own API key, or MyMemo's hosted
 * model; its endpoint, model and key (write-only), a Test button, a switch per feature, the private topics a model on
 * this computer may read, monthly limits and this month's usage, and where memory text goes. For MyMemo (§17.8):
 * sign-in (no browser when this computer is linked to OpenMyMemo Cloud), the account, and the model, plan and usage as
 * MyMemo reports them, with its plan page opened in the browser; nothing about the plan is known here.
 * Its embeddings model (optional) and the local model for search by meaning (`SemanticSettings`, shown even with
 * the Assistant off) are here too.
 */
type Choice = "off" | AssistantKind;
const CHOICES: Choice[] = ["off", "local", "key", "mymemo"];
// A kind the server doesn't offer isn't shown: MyMemo's hosted model is offered unless turned off on this computer
// (OPENMYMEMO_ASSISTANT_MYMEMO=0, §17.8).
const choices = computed(() => CHOICES.filter((c) => c === "off" || status.value?.kinds[c]));
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
    return;
  }
  void loadAccount();
});

// ---- MyMemo's hosted model -------------------------------------------------------------------------------------
const mm = computed(() => (status.value?.enabled && status.value.kind === "mymemo" ? status.value.mymemo ?? null : null));
const account = ref<AssistantAccount | null>(null);
const loadingAccount = ref(false);
const signingIn = ref(false);
const signInUrl = ref<string | null>(null);
let poll = 0;

async function loadAccount() {
  if (!mm.value?.signed_in) {
    account.value = null;
    return;
  }
  loadingAccount.value = true;
  try {
    const r = await api.assistantAccount();
    account.value = r.account;
    take(r.assistant); // seeing an active plan lifts a pause
  } catch {
    account.value = null;
  } finally {
    loadingAccount.value = false;
  }
}

function openExternal(url: string) {
  if (desktop) window.location.assign(url);
  else window.open(url, "_blank", "noopener");
}

function stopPoll() {
  window.clearInterval(poll);
  poll = 0;
}
onBeforeUnmount(stopPoll);

function waitForBrowser() {
  stopPoll();
  const until = Date.now() + 10 * 60 * 1000;
  poll = window.setInterval(async () => {
    try {
      take(await api.assistant());
    } catch {
      return;
    }
    const m = status.value?.mymemo;
    if (m?.signed_in) {
      stopPoll();
      signInUrl.value = null;
      toast(t("ast.mm.signedIn"));
      void loadAccount();
    } else if (!m?.pending || Date.now() > until) {
      stopPoll();
      signInUrl.value = null;
    }
  }, 2000);
}

async function signIn(browser = false) {
  if (signingIn.value) return;
  signingIn.value = true;
  try {
    const r = await api.assistantSignIn(browser);
    take(r.assistant);
    if (r.signed_in) {
      toast(t("ast.mm.signedIn"));
      await loadAccount();
    } else if (r.url) {
      signInUrl.value = r.url;
      openExternal(r.url);
      waitForBrowser();
    }
  } catch (e) {
    toast(e instanceof Error ? e.message : String(e), "error");
  } finally {
    signingIn.value = false;
  }
}

async function signOut() {
  if (busy.value) return;
  busy.value = true;
  try {
    const r = await api.assistantSignOut();
    take(r.assistant);
    account.value = null;
    toast(r.message);
  } catch (e) {
    toast(e instanceof Error ? e.message : String(e), "error");
  } finally {
    busy.value = false;
  }
}

const accountLine = computed(() => {
  const m = mm.value;
  if (!m) return "";
  const a = m.account;
  const who = a?.display_name && a?.email_masked ? `${a.display_name} (${a.email_masked})` : a?.display_name || a?.email_masked;
  const line = who ? t("ast.mm.account", { who }) : t("ast.mm.accountUnknown", { host: hostOf(m.service) });
  return m.via === "cloud" ? `${line} ${t("ast.mm.viaLink")}` : line;
});
const hostOf = (url: string) => {
  try {
    return new URL(url).host;
  } catch {
    return url;
  }
};
const day = (iso: string | null | undefined) => {
  if (!iso) return "";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "" : d.toLocaleDateString("en-US", { month: "long", day: "numeric", timeZone: "UTC" });
};
const ok = computed(() => (account.value?.ok ? account.value : null));
const planUrl = computed(() => ok.value?.plan.plan_url ?? mm.value?.hold?.plan_url ?? null);
const usd = (n: number) => n.toLocaleString("en-US", { style: "currency", currency: "USD" });
/** This month's use in dollars, as the service meters it ("$1.23 of $10.00 used this month."); "" from an older
 * service that reports tokens only. No dollar cap and no token caps: no cap. */
const costText = computed(() => {
  const u = ok.value?.usage;
  if (u?.cost_usd == null) return "";
  if (u.cost_usd_included != null) return t("ast.mm.cost", { used: usd(u.cost_usd), included: usd(u.cost_usd_included) });
  const capped = u.input_tokens_included != null || u.output_tokens_included != null;
  return t(capped ? "ast.mm.costUncapped" : "ast.mm.costNoCap", { used: usd(u.cost_usd) });
});
/** The token counts: the whole usage line from an older service, secondary under the dollars otherwise. */
const usageText = computed(() => {
  const u = ok.value?.usage;
  const counts = u && [u.input_tokens, u.output_tokens, u.input_tokens_included, u.output_tokens_included, u.requests];
  if (!u || !counts || counts.every((v) => v == null)) return "";
  const of = (used: number | null, included: number | null) =>
    included == null ? fmt(used ?? 0) : t("ast.mm.usageOf", { used: fmt(used ?? 0), included: fmt(included) });
  return t(costText.value ? "ast.mm.tokens" : "ast.mm.usage", {
    input: of(u.input_tokens, u.input_tokens_included), output: of(u.output_tokens, u.output_tokens_included),
    requests: fmt(u.requests ?? 0),
  });
});
/** What the plan includes, from the service's answer (never a figure of OpenMyMemo's own). */
const includesText = computed(() => {
  const a = ok.value;
  const included = a?.usage.cost_usd_included;
  const model = a?.display_name ?? a?.model;
  return included != null && model ? t("ast.mm.includes", { included: usd(included), model }) : "";
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
  if (c === choice.value) return;
  void change(c === "off" ? { enabled: false } : { enabled: true, kind: c }).then(() => loadAccount());
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
            v-for="c in choices"
            :key="c"
            type="button"
            class="seg-item disabled:opacity-50"
            :class="{ 'is-on': choice === c }"
            role="radio"
            :aria-checked="choice === c"
            :disabled="busy || locked('enabled') || (c !== 'off' && locked('kind') && status.kind !== c)"
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

      <div v-if="mm" class="space-y-3 px-4.5 py-3.5" data-test="mymemo">
        <div class="font-semibold">{{ t("ast.mm.title") }}</div>
        <template v-if="mm.signed_in">
          <p class="text-[13px]" data-test="mm-account">{{ accountLine }}</p>
          <p v-if="loadingAccount && !account" class="text-[13px] text-muted">{{ t("ast.mm.loading") }}</p>
          <template v-if="ok">
            <p v-if="ok.display_name || ok.model" class="text-[13px]" data-test="mm-model">
              {{ ok.provider ? t("ast.mm.modelBy", { model: ok.display_name ?? ok.model ?? "", provider: ok.provider })
                : t("ast.mm.model", { model: ok.display_name ?? ok.model ?? "" }) }}
            </p>
            <p class="text-[13px]" data-test="mm-plan">
              {{ !ok.plan.active ? t("ast.mm.planNone")
                : ok.plan.cancel_at_period_end && ok.plan.period_end ? t("ast.mm.planEnds", { date: day(ok.plan.period_end) })
                : t("ast.mm.planActive") }}
            </p>
            <p v-if="!ok.plan.active && includesText" class="text-[13px] text-muted" data-test="mm-includes">{{ includesText }}</p>
            <p v-if="costText" class="text-[13px]" data-test="mm-cost">
              {{ costText }}
              <template v-if="ok.plan.active && (ok.plan.resets_at || ok.plan.period_end)">
                {{ t("ast.mm.resets", { date: day(ok.plan.resets_at ?? ok.plan.period_end) }) }}
              </template>
            </p>
            <p v-if="usageText" class="text-[13px] text-muted" data-test="mm-usage">
              {{ usageText }}
              <template v-if="!costText && ok.plan.active && (ok.plan.resets_at || ok.plan.period_end)">
                {{ t("ast.mm.resets", { date: day(ok.plan.resets_at ?? ok.plan.period_end) }) }}
              </template>
            </p>
          </template>
          <p v-else-if="account && !account.ok" class="text-[13px] text-muted">{{ account.error }}</p>
          <p v-if="mm.hold" class="notice flex-wrap gap-2 text-[13px]" data-test="mm-hold">
            <AppIcon name="info" :size="14" class="shrink-0" />
            <span>{{ mm.hold.message }}</span>
          </p>
          <div class="flex flex-wrap items-center gap-2 pt-1">
            <a
              v-if="planUrl"
              :href="planUrl"
              target="_blank"
              rel="noopener"
              class="btn btn-sm"
              data-test="mm-plan-link"
              @click.prevent="openExternal(planUrl)"
            >{{ ok?.plan.active ? t("ast.mm.manage") : t("ast.mm.getPlan") }}</a>
            <button type="button" class="btn btn-sm" :disabled="busy || testing || !!status.problem" @click="test">
              {{ testing ? t("ast.testing") : t("ast.test") }}
            </button>
            <button type="button" class="btn btn-ghost btn-sm" :disabled="busy" @click="signOut">{{ t("ast.mm.signOut") }}</button>
          </div>
          <p v-if="tested" class="text-[13px]" :class="tested.ok ? 'text-fresh' : 'text-muted'" data-test="tested">
            {{ tested.ok ? t("ast.testOk", { model: tested.model ?? "", ms: tested.ms ?? 0 }) : tested.error }}
          </p>
        </template>
        <template v-else>
          <p class="text-[13px] text-muted">{{ mm.signed_out === "expired" ? t("ast.mm.expired") : t("ast.mm.signInHelp") }}</p>
          <p v-if="mm.cloud_linked" class="text-[13px] text-muted">{{ t("ast.mm.viaCloud") }}</p>
          <div class="flex flex-wrap items-center gap-2">
            <button type="button" class="btn btn-sm" :disabled="signingIn" @click="signIn()">
              {{ signingIn ? t("ast.mm.signingIn") : t("ast.mm.signIn") }}
            </button>
            <button v-if="mm.cloud_linked" type="button" class="btn btn-ghost btn-sm" :disabled="signingIn" @click="signIn(true)">
              {{ t("ast.mm.signInBrowser") }}
            </button>
          </div>
          <p v-if="signInUrl && mm.pending" class="text-[13px] text-muted" data-test="mm-waiting">
            {{ t("ast.mm.waiting") }}
            <a :href="signInUrl" target="_blank" rel="noopener" class="btn btn-link btn-sm">{{ t("ast.mm.openAgain") }}</a>
          </p>
          <p v-if="mm.signin_error" class="text-[13px] text-wait-ink">{{ mm.signin_error }}</p>
        </template>
        <p class="text-xs text-muted" data-test="mm-no-embeddings">{{ t("ast.mm.noEmbeddings") }}</p>
      </div>

      <form v-if="status.enabled && status.kind !== 'mymemo'" class="space-y-3 px-4.5 py-3.5" @submit.prevent="save">
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
        <div v-for="f in FEATURES" :key="f">
          <div class="flex items-center gap-4 px-4.5 py-3">
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
          <SessionConsent v-if="f === 'session'" :status="status" @changed="take" />
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
