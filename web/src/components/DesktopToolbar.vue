<script setup lang="ts">
import { ui } from "../desktop";
import { t } from "../i18n";
import AppIcon from "./AppIcon.vue";

/**
 * The Mac window's toolbar: the place's name, a line under it and the actions. The whole strip drags
 * the window. With the sidebar hidden it makes room for the window buttons and offers to bring it back.
 */
defineProps<{ title: string; subtitle?: string | null }>();
</script>

<template>
  <header
    class="flex min-h-[52px] shrink-0 flex-wrap items-center gap-x-3 gap-y-2 border-b border-sep py-2 pr-3"
    :class="ui.sidebarHidden ? 'pl-[92px]' : 'pl-5'"
    data-tauri-drag-region
    data-chrome
  >
    <button
      v-if="ui.sidebarHidden"
      type="button"
      class="btn btn-ghost btn-icon -ml-1 text-ink-2"
      :aria-label="t('mac.showSidebar')"
      :title="`${t('mac.showSidebar')} ⌃⌘S`"
      @click="ui.sidebarHidden = false"
    >
      <AppIcon name="sidebar" :size="18" />
    </button>
    <div class="flex min-w-40 flex-1 flex-col" data-tauri-drag-region>
      <h1 class="truncate text-[15px] leading-tight font-bold tracking-[-0.01em]" data-tauri-drag-region>{{ title }}</h1>
      <span v-if="subtitle" class="truncate text-[11px] text-muted" :title="subtitle" data-tauri-drag-region>{{ subtitle }}</span>
    </div>
    <div class="flex flex-wrap items-center gap-1">
      <slot />
    </div>
  </header>
</template>
