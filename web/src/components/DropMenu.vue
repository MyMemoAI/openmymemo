<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from "vue";
import type { IconName } from "../icons";
import AppIcon from "./AppIcon.vue";

/** A button that opens a small menu. Clicking an item, clicking outside or pressing Escape closes it. */
withDefaults(defineProps<{
  label: string;
  icon?: IconName;
  buttonClass?: string;
  align?: "left" | "right";
  up?: boolean;
  width?: string;
}>(), { icon: "more", buttonClass: "btn btn-ghost btn-icon", align: "right", up: false, width: "w-56" });

const open = ref(false);
const root = ref<HTMLElement | null>(null);

function onPointer(e: PointerEvent) {
  if (root.value && !root.value.contains(e.target as Node)) open.value = false;
}
function onKey(e: KeyboardEvent) {
  if (e.key === "Escape") open.value = false;
}
function listen(on: boolean) {
  if (on) {
    window.addEventListener("pointerdown", onPointer);
    window.addEventListener("keydown", onKey);
  } else {
    window.removeEventListener("pointerdown", onPointer);
    window.removeEventListener("keydown", onKey);
  }
}
watch(open, listen);
onBeforeUnmount(() => listen(false));

const close = () => (open.value = false);
defineExpose({ close });
</script>

<template>
  <div ref="root" class="relative">
    <button
      type="button"
      :class="buttonClass"
      :aria-label="label"
      :title="label"
      aria-haspopup="menu"
      :aria-expanded="open"
      @click="open = !open"
    >
      <slot name="button"><AppIcon :name="icon" /></slot>
    </button>
    <div
      v-if="open"
      class="menu"
      :class="[width, align === 'right' ? 'right-0' : 'left-0', up ? 'bottom-full mb-1' : 'top-full']"
      role="menu"
      @click="close"
    >
      <slot :close="close" />
    </div>
  </div>
</template>
