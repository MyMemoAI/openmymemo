<script setup lang="ts">
import type { AppStatus, ConnectResult } from "../api";
import { t } from "../i18n";
import { copy } from "../store";
import AppIcon from "./AppIcon.vue";

/**
 * What connecting or disconnecting an app left for the person to do, under its row: the snippet to paste
 * (with where it goes and a one-click link when the app has one), or steps such as trusting Codex's hook.
 */
defineProps<{ app: AppStatus; result: ConnectResult }>();
const emit = defineEmits<{ close: [] }>();
</script>

<template>
  <div class="space-y-3 bg-sunken px-4.5 py-3.5 text-[13px]" role="status">
    <div class="flex items-start gap-2.5">
      <AppIcon
        :name="result.status === 'error' ? 'warning' : 'info'"
        :size="15"
        class="mt-px"
        :class="result.status === 'error' ? 'text-danger' : result.status === 'manual' ? 'text-wait-ink' : 'text-accent'"
      />
      <p class="min-w-0 flex-1 break-words" :class="{ 'text-danger-ink': result.status === 'error' }">{{ result.message }}</p>
      <button class="btn btn-ghost btn-icon size-7 min-h-7 shrink-0" :aria-label="t('apps.hide')" @click="emit('close')">
        <AppIcon name="x" :size="13" />
      </button>
    </div>

    <div v-if="result.link" class="flex flex-wrap items-center gap-2 pl-6">
      <a :href="result.link.url" class="btn btn-primary btn-sm">{{ result.link.label }}</a>
      <span class="text-muted">{{ t("apps.orPaste") }}</span>
    </div>

    <div v-if="result.snippet" class="space-y-1.5 pl-6">
      <div v-if="result.path" class="text-muted">
        {{ t("apps.pasteInto") }} <span class="font-mono text-xs break-all text-ink">{{ result.path }}</span>
      </div>
      <div class="relative rounded-[10px] bg-surface shadow-[0_0_0_1px_var(--color-ring)]">
        <pre class="max-h-64 overflow-auto px-3.5 py-3 pr-20 font-mono text-xs leading-relaxed break-all whitespace-pre-wrap">{{ result.snippet }}</pre>
        <button class="btn btn-sm absolute top-2 right-2" @click="copy(result.snippet)">
          <AppIcon name="copy" :size="14" />{{ t("common.copy") }}
        </button>
      </div>
    </div>

    <ol v-if="result.steps.length" class="list-decimal space-y-1 pl-10 marker:text-muted">
      <li v-for="step in result.steps" :key="step" class="break-words">{{ step }}</li>
    </ol>

    <a v-if="result.status === 'manual'" :href="app.docs" target="_blank" rel="noopener" class="btn btn-link btn-sm ml-5">
      {{ t("apps.docs", { name: app.name }) }}<AppIcon name="arrowRight" :size="13" />
    </a>
  </div>
</template>
