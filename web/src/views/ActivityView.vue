<script setup lang="ts">
import { ref, watch } from "vue";
import { api, type Activity } from "../api";
import AppIcon from "../components/AppIcon.vue";
import PageHeader from "../components/PageHeader.vue";
import type { IconName } from "../icons";
import { prettyName, relTime, t } from "../i18n";
import { connectorName, store } from "../store";

const connector = ref<string>("");
const rows = ref<Activity[]>([]);

async function load() {
  rows.value = await api.activity(connector.value || undefined);
}
watch([connector, () => store.version], load, { immediate: true });

function describe(a: Activity): string {
  const n = a.memory_ids?.length ?? 0;
  switch (a.action) {
    case "context": return t("act.context", { n });
    case "search": return t("act.search", { q: a.query ?? "", n });
    case "get": return t("act.get", { n });
    case "propose":
      return t("act.propose", { op: a.op ? t(`rev.wants.${a.op}`).replace(/^wants to /, "") : "" })
        + (a.result === "duplicate" ? " " + t("act.duplicate") : a.result ? ` · ${t(`rev.status.${a.result}`)}` : "");
    case "import": return t("act.import", { n, source: prettyName(a.source ?? "") });
    case "link": {
      const who = a.account && a.email_masked ? `${a.account} (${a.email_masked})` : a.account || a.email_masked;
      return t("act.link", { who: who || t("cloud.yourAccount") });
    }
    default: return a.action;
  }
}

const ICON: Record<string, IconName> = {
  context: "eye", search: "search", get: "file", propose: "pencil", import: "download", link: "plug",
};
</script>

<template>
  <div class="mx-auto max-w-[820px] space-y-5">
    <PageHeader :title="t('nav.activity')" :subtitle="t('act.subtitle')">
      <select v-model="connector" class="input select w-auto font-medium" :aria-label="t('nav.connectors')">
        <option value="">{{ t("common.all") }}</option>
        <option v-for="c in store.connectors" :key="c.id" :value="c.id">{{ c.name }}</option>
      </select>
    </PageHeader>
    <div v-if="!rows.length" class="card px-6 py-14 text-center text-sm text-muted">{{ t("act.empty") }}</div>
    <ol v-else class="card divide-y divide-sep">
      <li v-for="(a, i) in rows" :key="i" class="flex items-start gap-3 px-4.5 py-3 text-sm">
        <span class="mt-px inline-flex size-7 shrink-0 items-center justify-center rounded-full bg-fill text-muted">
          <AppIcon :name="ICON[a.action] ?? 'clock'" :size="14" />
        </span>
        <div class="min-w-0 flex-1 pt-1">
          <span class="font-semibold">{{ connectorName(a.connector) }}</span> <span class="text-ink-2">{{ describe(a) }}</span>
        </div>
        <span class="shrink-0 pt-1 text-xs text-muted" :title="a.at">{{ relTime(a.at) }}</span>
      </li>
    </ol>
  </div>
</template>
