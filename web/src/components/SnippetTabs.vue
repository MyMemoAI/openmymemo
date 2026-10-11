<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { t } from "../i18n";
import { copy } from "../store";
import AppIcon from "./AppIcon.vue";

const props = defineProps<{ snippets: Record<string, string> }>();

const LABELS: Record<string, string> = {
  claude_code: "Claude Code",
  claude_desktop: "Claude Desktop",
  cursor: "Cursor",
  vscode: "VS Code",
  codex: "Codex",
  json: "Other",
};

const keys = computed(() => Object.keys(props.snippets).filter((k) => !k.endsWith("_link")));
const active = ref(keys.value[0]);
watch(keys, (k) => (active.value = k[0]));
</script>

<template>
  <div class="border-t border-sep">
    <div class="flex flex-wrap items-center gap-2 px-5 py-3">
      <div class="seg" role="tablist" :aria-label="t('con.config')">
        <button
          v-for="k in keys"
          :key="k"
          class="seg-item"
          :class="{ 'is-on': active === k }"
          role="tab"
          :aria-selected="active === k"
          @click="active = k"
        >{{ LABELS[k] ?? k }}</button>
      </div>
      <a v-if="snippets.cursor_link" :href="snippets.cursor_link" class="btn btn-primary btn-sm">{{ t("con.addToCursor") }}</a>
    </div>
    <div class="relative bg-sunken">
      <pre class="max-h-72 overflow-auto px-5 py-4 pr-24 font-mono text-[13px] leading-relaxed break-all whitespace-pre-wrap">{{ snippets[active] }}</pre>
      <button class="btn btn-sm absolute top-3 right-4" @click="copy(snippets[active])"><AppIcon name="copy" :size="14" />{{ t("common.copy") }}</button>
    </div>
  </div>
</template>
