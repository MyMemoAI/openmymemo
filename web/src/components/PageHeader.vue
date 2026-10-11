<script setup lang="ts">
import { desktop } from "../desktop";
import DesktopToolbar from "./DesktopToolbar.vue";

/**
 * The large title that names the page, an optional line under it, and its actions on the right.
 * In the Mac app it becomes the window's toolbar instead.
 */
defineProps<{ title: string; subtitle?: string | null }>();
</script>

<template>
  <Teleport v-if="desktop" to="#desktop-toolbar" defer>
    <DesktopToolbar :title="title" :subtitle="subtitle"><slot /></DesktopToolbar>
  </Teleport>
  <header v-else class="flex flex-wrap items-end justify-between gap-x-4 gap-y-3">
    <div class="min-w-0 space-y-0.5">
      <h1 class="page-title">{{ title }}</h1>
      <p v-if="subtitle" class="page-sub">{{ subtitle }}</p>
    </div>
    <div v-if="$slots.default" class="flex flex-wrap items-center gap-2">
      <slot />
    </div>
  </header>
</template>
