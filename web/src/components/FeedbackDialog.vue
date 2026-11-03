<script setup lang="ts">
import { onMounted, ref } from "vue";
import { api, type FeedbackLinks } from "../api";
import { native } from "../desktop";
import { t } from "../i18n";
import AppIcon from "./AppIcon.vue";
import ModalDialog from "./ModalDialog.vue";

/**
 * Send feedback (product-design.md §21): a GitHub issue (public) or an email (private), each opened with the
 * version and system filled in. Nothing is sent from here, and nothing from the memory folder is added.
 */
const emit = defineEmits<{ close: [] }>();
const links = ref<FeedbackLinks | null>(null);
const failed = ref("");

onMounted(async () => {
  try {
    links.value = await api.feedback(native);
  } catch (e) {
    failed.value = e instanceof Error ? e.message : String(e);
  }
});
</script>

<template>
  <ModalDialog :title="t('fb.title')" @close="emit('close')">
    <div class="space-y-4">
      <p class="text-sm">{{ t("fb.intro") }}</p>
      <p v-if="failed" class="text-sm text-danger-ink">{{ failed }}</p>
      <p v-else-if="!links" class="text-sm text-muted">{{ t("common.loading") }}</p>
      <template v-else>
        <div class="card divide-y divide-sep">
          <a :href="links.issue_url" target="_blank" rel="noopener" class="flex items-center gap-3 px-4.5 py-3.5"
             data-test="fb-github">
            <span class="min-w-0 flex-1 space-y-0.5">
              <span class="block font-semibold">{{ t("fb.github") }}</span>
              <span class="block text-[13px] text-muted">{{ t("fb.githubHelp") }}</span>
            </span>
            <AppIcon name="arrowRight" :size="14" class="text-muted" />
          </a>
          <a :href="links.email_url" class="flex items-center gap-3 px-4.5 py-3.5" data-test="fb-email">
            <span class="min-w-0 flex-1 space-y-0.5">
              <span class="block font-semibold">{{ t("fb.email") }}</span>
              <span class="block text-[13px] text-muted">{{ t("fb.emailHelp", { email: links.email }) }}</span>
            </span>
            <AppIcon name="arrowRight" :size="14" class="text-muted" />
          </a>
        </div>
        <p class="text-xs text-muted" data-test="fb-about">{{ t("fb.about", { about: links.about }) }}</p>
      </template>
    </div>
  </ModalDialog>
</template>
