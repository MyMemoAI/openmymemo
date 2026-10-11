<script setup lang="ts">
import { computed } from "vue";
import { api } from "../api";
import { t } from "../i18n";
import { act, store } from "../store";

const on = computed(() => store.state?.settings.require_approval ?? false);
const toggle = () => act(() => api.patchSettings({ require_approval: !on.value }), on.value ? t("set.nowOff") : t("set.nowOn"));
</script>

<template>
  <section class="card flex items-center gap-4 px-4.5 py-3.5">
    <div class="min-w-0 flex-1 space-y-0.5">
      <div class="font-semibold">{{ t("set.approval") }}</div>
      <p class="text-[13px] text-muted">{{ on ? t("set.approvalOn") : t("set.approvalOff") }}</p>
    </div>
    <button role="switch" class="switch" :aria-checked="on" :aria-label="t('set.approval')" @click="toggle">
      <span class="switch-knob" />
    </button>
  </section>
</template>
