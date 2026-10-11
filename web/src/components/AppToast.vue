<script setup lang="ts">
import { store } from "../store";
</script>

<template>
  <Transition
    enter-from-class="translate-y-2 opacity-0"
    enter-active-class="transition"
    leave-to-class="opacity-0"
    leave-active-class="transition"
  >
    <div
      v-if="store.toast"
      role="status"
      class="fixed bottom-24 left-1/2 z-50 max-w-[90vw] -translate-x-1/2 rounded-xl px-4 py-2.5 text-sm font-medium shadow-lg md:bottom-6"
      :class="store.toast.kind === 'error' ? 'bg-danger text-white' : 'bg-ink text-ground'"
    >
      {{ store.toast.text }}
    </div>
  </Transition>
</template>
