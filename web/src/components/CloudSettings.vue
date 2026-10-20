<script setup lang="ts">
import { computed, onMounted, ref, watch } from "vue";
import { api, type CloudStatus } from "../api";
import { t } from "../i18n";
import { store, toast } from "../store";

/**
 * OpenMyMemo Cloud in Settings (product-design.md §16.11): whether the Connectors page lists it (on unless the person
 * hides it; always listed while this computer is linked) and which service it links to (MyMemo's own unless chosen).
 */
const status = ref<CloudStatus | null>(null);
const draft = ref("");
const busy = ref(false);

function take(s: CloudStatus) {
  status.value = s;
  draft.value = s.settings.service_from_env ?? (s.settings.service || s.settings.default_service);
}

let seq = 0;
async function load() {
  const mine = ++seq;
  try {
    const s = await api.cloud();
    if (mine === seq) take(s);
  } catch {
    if (mine === seq && !status.value) status.value = null; // an older server without OpenMyMemo Cloud: nothing to set
  }
}
onMounted(load);
// Linked or unlinked meanwhile (CloudRow polls and bumps cloudVersion): what may change now changes with it.
let own = -1; // the bump this component made itself, with the answer already in hand
watch(() => store.cloudVersion, (v) => v !== own && load());

const settings = computed(() => status.value?.settings ?? null);
const locked = computed(() => !!status.value?.linked || !!settings.value?.service_from_env);
const current = computed(() => (settings.value ? settings.value.service || settings.value.default_service : ""));
const changed = computed(() => draft.value.trim().replace(/\/+$/, "") !== current.value);

async function change(body: { show?: boolean; service?: string }, done?: (s: CloudStatus) => string | null) {
  if (busy.value) return;
  busy.value = true;
  try {
    const s = await api.cloudSettings(body);
    seq++; // an older load must not overwrite this answer
    take(s);
    own = ++store.cloudVersion;
    const text = done?.(s);
    if (text) toast(text);
  } catch (e) {
    toast(e instanceof Error ? e.message : String(e), "error");
    void load(); // refused because the link changed meanwhile, say: show how things are now
  } finally {
    busy.value = false;
  }
}

const toggle = () => status.value && change({ show: !status.value.shown });
const save = () => change({ service: draft.value.trim() }, (s) => t("set.cloudSaved", { service: s.service }));
const useDefault = () => change({ service: "" }, (s) => t("set.cloudSaved", { service: s.service }));
</script>

<template>
  <section v-if="status && settings" class="space-y-1.5" :aria-label="t('set.cloud')">
    <h3 class="label px-1">{{ t("set.cloud") }}</h3>
    <div class="card divide-y divide-sep">
      <div class="flex items-center gap-4 px-4.5 py-3">
        <div class="min-w-0 flex-1 space-y-0.5">
          <div class="font-semibold">{{ t("set.cloudShow") }}</div>
          <p class="text-[13px] text-muted">{{ t(status.linked ? "set.cloudLinked" : "set.cloudShowHelp") }}</p>
        </div>
        <button
          role="switch"
          class="switch disabled:opacity-50"
          :aria-checked="status.shown"
          :aria-label="t('set.cloudShow')"
          :disabled="busy || status.linked"
          @click="toggle"
        >
          <span class="switch-knob" />
        </button>
      </div>
      <form v-if="status.shown" class="space-y-2 px-4.5 py-3.5" @submit.prevent="save">
        <label for="cloud-service" class="font-semibold">{{ t("set.cloudService") }}</label>
        <p class="text-[13px] text-muted">
          {{ t(settings.service_from_env ? "set.cloudServiceEnv" : status.linked ? "set.cloudServiceLinked" : "set.cloudServiceHelp") }}
        </p>
        <input
          id="cloud-service"
          v-model="draft"
          type="url"
          class="input font-mono text-xs"
          :placeholder="settings.default_service"
          :disabled="locked || busy"
          spellcheck="false"
          autocomplete="off"
        />
        <div v-if="!locked" class="flex flex-wrap gap-2 pt-1">
          <button type="submit" class="btn btn-sm" :disabled="busy || !changed || !draft.trim()">{{ t("common.save") }}</button>
          <button v-if="settings.service" type="button" class="btn btn-sm" :disabled="busy" @click="useDefault">
            {{ t("set.cloudUseDefault") }}
          </button>
        </div>
      </form>
    </div>
  </section>
</template>
