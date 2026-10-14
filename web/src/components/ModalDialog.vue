<script setup lang="ts">
import { onBeforeUnmount, onMounted } from "vue";
import { t } from "../i18n";

import AppIcon from "./AppIcon.vue";

defineProps<{ title: string; wide?: boolean }>();
const emit = defineEmits<{ close: [] }>();

const onKey = (e: KeyboardEvent) => e.key === "Escape" && emit("close");
// The dialog stays put and scrolls inside itself; the page behind it doesn't scroll along.
let overflow = "";
onMounted(() => {
  window.addEventListener("keydown", onKey);
  overflow = document.documentElement.style.overflow;
  document.documentElement.style.overflow = "hidden";
});
onBeforeUnmount(() => {
  window.removeEventListener("keydown", onKey);
  document.documentElement.style.overflow = overflow;
});
</script>

<template>
  <Teleport to="body">
    <div class="fixed inset-0 z-40 flex items-start justify-center bg-overlay p-4 pt-[10vh] backdrop-blur-[2px]" @click.self="emit('close')">
      <div
        class="flex max-h-full w-full flex-col rounded-[18px] bg-surface shadow-[0_24px_64px_rgb(0_0_0/0.22),0_0_0_1px_var(--color-ring)]"
        :class="wide ? 'max-w-3xl' : 'max-w-lg'"
        role="dialog"
        aria-modal="true"
        :aria-label="title"
      >
        <div class="flex shrink-0 items-center justify-between gap-3 px-5.5 pt-4.5 pb-1">
          <h2 class="text-lg font-bold tracking-[-0.01em]">{{ title }}</h2>
          <button class="btn btn-filled btn-icon size-7 min-h-7 rounded-full" :aria-label="t('common.close')" @click="emit('close')">
            <AppIcon name="x" :size="13" />
          </button>
        </div>
        <div class="min-h-0 overflow-y-auto overscroll-contain px-5.5 pt-3 pb-5.5"><slot /></div>
      </div>
    </div>
  </Teleport>
</template>
