<script setup lang="ts">
/** The large title that names the page, an optional line under it, and its actions on the right. */
defineProps<{ title: string; subtitle?: string | null }>();
</script>

<template>
  <header class="flex flex-wrap items-end justify-between gap-x-4 gap-y-3">
    <div class="min-w-0 space-y-0.5">
      <h1 class="page-title">{{ title }}</h1>
      <p v-if="subtitle" class="page-sub">{{ subtitle }}</p>
    </div>
    <div v-if="$slots.default" class="flex flex-wrap items-center gap-2">
      <slot />
    </div>
  </header>
</template>
