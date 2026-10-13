<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { native, shell, type AppSettings } from "../desktop";
import { t } from "../i18n";
import { store, toast } from "../store";
import AppIcon from "./AppIcon.vue";
import ApprovalSwitch from "./ApprovalSwitch.vue";
import ModalDialog from "./ModalDialog.vue";

/** The Mac app's settings: the memory folder, the approval switch, and opening at login. */
const emit = defineEmits<{ close: [] }>();
const app = ref<AppSettings | null>(null);
const vault = computed(() => app.value?.vault ?? store.state?.vault ?? "");

onMounted(async () => {
  app.value = await shell.settings();
});

async function run(fn: () => unknown) {
  try {
    await fn();
    app.value = await shell.settings();
  } catch (e) {
    toast(e instanceof Error ? e.message : String(e), "error");
  }
}
const reveal = () => run(() => shell.reveal(vault.value));
const chooseVault = () => run(() => shell.chooseVault());
const toggleAutostart = () => run(() => shell.setAutostart(!app.value?.autostart));
function openGuide() {
  location.hash = "#/welcome";
  emit("close");
}
</script>

<template>
  <ModalDialog :title="t('set.title')" @close="emit('close')">
    <div class="space-y-4 text-sm">
      <section class="card space-y-2 px-4.5 py-3.5">
        <div class="font-semibold">{{ t("set.folder") }}</div>
        <p class="font-mono text-xs break-all">{{ vault }}</p>
        <p class="text-[13px] text-muted">{{ t("set.folderHelp") }}</p>
        <div v-if="native" class="flex flex-wrap gap-2 pt-1">
          <button type="button" class="btn btn-sm" @click="reveal">
            <AppIcon name="memories" :size="14" />{{ t("ins.showInFinder") }}
          </button>
          <button type="button" class="btn btn-sm" @click="chooseVault">{{ t("set.change") }}</button>
        </div>
      </section>

      <ApprovalSwitch />

      <section class="card flex items-center gap-4 px-4.5 py-3.5">
        <div class="min-w-0 flex-1 space-y-0.5">
          <div class="font-semibold">{{ t("set.guide") }}</div>
          <p class="text-[13px] text-muted">{{ t("set.guideHelp") }}</p>
        </div>
        <button type="button" class="btn btn-sm shrink-0" @click="openGuide">{{ t("set.openGuide") }}</button>
      </section>

      <section v-if="native && app" class="card flex items-center gap-4 px-4.5 py-3.5">
        <div class="min-w-0 flex-1 space-y-0.5">
          <div class="font-semibold">{{ t("set.autostart") }}</div>
          <p class="text-[13px] text-muted">{{ t("set.autostartHelp") }}</p>
        </div>
        <button
          role="switch"
          class="switch"
          :aria-checked="app.autostart"
          :aria-label="t('set.autostart')"
          @click="toggleAutostart"
        >
          <span class="switch-knob" />
        </button>
      </section>

      <p class="text-xs text-muted">{{ t("set.version", { version: app?.version ?? store.state?.version ?? "" }) }}</p>
    </div>
  </ModalDialog>
</template>
