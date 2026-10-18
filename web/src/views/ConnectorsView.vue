<script setup lang="ts">
import { ref, watch } from "vue";
import { api, type AppStatus, type Connector } from "../api";
import AppIcon from "../components/AppIcon.vue";
import AssistantSettings from "../components/AssistantSettings.vue";
import CloudSettings from "../components/CloudSettings.vue";
import ConnectApps from "../components/ConnectApps.vue";
import ConnectorPanel from "../components/ConnectorPanel.vue";
import PageHeader from "../components/PageHeader.vue";
import { desktop } from "../desktop";
import { t } from "../i18n";
import { act, store } from "../store";

/**
 * The AI apps: connect them with one click, then set what each connected one may read and write. In a browser,
 * which has no Settings sheet, the Assistant's and MyMemo Cloud's settings (shown or not, its service) sit at the bottom.
 */
const props = defineProps<{ initialConnector: string | null }>();

const apps = ref<InstanceType<typeof ConnectApps> | null>(null);
const openId = ref<string | null>(props.initialConnector);
watch(() => props.initialConnector, (v) => v && (openId.value = v));

function manage(id: string | null) {
  openId.value = id;
  history.replaceState(null, "", id ? `#/connectors/${encodeURIComponent(id)}` : "#/connectors");
}

/** The panel's live connector (it refreshes every few seconds), falling back to the catalog's copy. */
const live = (c: Connector) => store.connectors.find((x) => x.id === c.id) ?? c;

async function remove(app: AppStatus | null, c: Connector, alsoSettings: boolean) {
  if (app && alsoSettings) await apps.value?.disconnect(app.id);
  else await act(() => api.removeConnector(c.id), t("apps.disconnected", { name: app?.name ?? c.name }));
  await apps.value?.reload();
  manage(null);
}
</script>

<template>
  <div class="mx-auto max-w-[960px] space-y-5">
    <PageHeader :title="t('nav.connectors')" :subtitle="t('apps.subtitle')">
      <button class="btn" @click="apps?.addOther()">
        <AppIcon name="plus" :size="14" />{{ t("con.add") }}
      </button>
    </PageHeader>

    <a v-if="store.state?.settings.require_approval" href="#/changes" class="notice notice-wait text-ink">
      <AppIcon name="info" class="text-wait-ink" />{{ t("con.approvalOn") }}
    </a>

    <ConnectApps ref="apps" :open="openId" @manage="manage">
      <template #manage="{ app, connector }">
        <ConnectorPanel :connector="live(connector)" :app="app" @remove="(also) => remove(app, connector, also)" />
      </template>
    </ConnectApps>

    <AssistantSettings v-if="!desktop" />
    <CloudSettings v-if="!desktop" />
  </div>
</template>
