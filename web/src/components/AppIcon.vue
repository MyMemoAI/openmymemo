<script setup lang="ts">
import type { PropType } from "vue";
import { ICONS, type IconName } from "../icons";

/** Decorative: the button or link around it carries the label. */
defineProps({
  name: { type: String as PropType<IconName>, required: true },
  size: { type: Number, default: 16 },
});
</script>

<template>
  <svg
    :width="size"
    :height="size"
    viewBox="0 0 24 24"
    stroke-width="2"
    aria-hidden="true"
    focusable="false"
    class="shrink-0"
    v-html="ICONS[name]"
  />
</template>
