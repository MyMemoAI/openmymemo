<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { native, shell, type AppSettings, type Appearance } from "../desktop";
import { t } from "../i18n";
import { copy, store, toast } from "../store";
import AppIcon from "./AppIcon.vue";
import ApprovalSwitch from "./ApprovalSwitch.vue";
import ModalDialog from "./ModalDialog.vue";

/**
 * The Mac app's settings (⌘,), in small groups: General (the memory folder, opening at login,
 * notifications), Appearance, AI changes (the approval switch), Help, and Advanced (the `openmymemo`
 * command the app installs, desktop/src-tauri/src/launcher.rs). In a browser preview only the parts the
 * panel itself provides show.
 */
const emit = defineEmits<{ close: [] }>();
const app = ref<AppSettings | null>(null);
const vault = computed(() => app.value?.vault ?? store.state?.vault ?? "");
const APPEARANCES: Appearance[] = ["system", "light", "dark"];
const GITHUB = "https://github.com/MyMemoAI/openmymemo";

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
const toggleNotifications = () => run(() => shell.setNotifications(!app.value?.notifications));
const setAppearance = (a: Appearance) => run(() => shell.setAppearance(a));
const revealLog = () => app.value && run(() => shell.reveal(app.value!.log));
</script>

<template>
  <ModalDialog :title="t('set.title')" @close="emit('close')">
    <div class="space-y-5 text-sm">
      <section class="space-y-1.5" :aria-label="t('desk.general')">
        <h3 class="label px-1">{{ t("desk.general") }}</h3>
        <div class="card divide-y divide-sep">
          <div class="space-y-2 px-4.5 py-3.5">
            <div class="font-semibold">{{ t("set.folder") }}</div>
            <p class="font-mono text-xs break-all">{{ vault }}</p>
            <p class="text-[13px] text-muted">{{ t("set.folderHelp") }}</p>
            <div v-if="native" class="flex flex-wrap gap-2 pt-1">
              <button type="button" class="btn btn-sm" @click="reveal">
                <AppIcon name="memories" :size="14" />{{ t("ins.showInFinder") }}
              </button>
              <button type="button" class="btn btn-sm" @click="chooseVault">{{ t("set.change") }}</button>
            </div>
          </div>
          <div v-if="native && app" class="flex items-center gap-4 px-4.5 py-3">
            <div class="min-w-0 flex-1 space-y-0.5">
              <div class="font-semibold">{{ t("set.autostart") }}</div>
              <p class="text-[13px] text-muted">{{ t("set.autostartHelp") }}</p>
            </div>
            <button role="switch" class="switch" :aria-checked="app.autostart" :aria-label="t('set.autostart')" @click="toggleAutostart">
              <span class="switch-knob" />
            </button>
          </div>
          <div v-if="native && app" class="flex items-center gap-4 px-4.5 py-3">
            <div class="min-w-0 flex-1 space-y-0.5">
              <div class="font-semibold">{{ t("desk.notifications") }}</div>
              <p class="text-[13px] text-muted">{{ t("desk.notificationsHelp") }}</p>
            </div>
            <button
              role="switch"
              class="switch"
              :aria-checked="app.notifications"
              :aria-label="t('desk.notificationsHelp')"
              @click="toggleNotifications"
            >
              <span class="switch-knob" />
            </button>
          </div>
        </div>
      </section>

      <section v-if="native && app" class="space-y-1.5" :aria-label="t('desk.appearance')">
        <h3 class="label px-1">{{ t("desk.appearance") }}</h3>
        <div class="card flex flex-wrap items-center gap-x-4 gap-y-2 px-4.5 py-3">
          <p class="min-w-0 flex-1 text-[13px] text-muted">{{ t("desk.appearanceHelp") }}</p>
          <div class="seg" role="radiogroup" :aria-label="t('desk.appearance')">
            <button
              v-for="a in APPEARANCES"
              :key="a"
              type="button"
              class="seg-item"
              :class="{ 'is-on': app.appearance === a }"
              role="radio"
              :aria-checked="app.appearance === a"
              @click="setAppearance(a)"
            >
              {{ t(`desk.appearance.${a}`) }}
            </button>
          </div>
        </div>
      </section>

      <section class="space-y-1.5" :aria-label="t('desk.aiChanges')">
        <h3 class="label px-1">{{ t("desk.aiChanges") }}</h3>
        <ApprovalSwitch />
      </section>

      <section class="space-y-1.5" :aria-label="t('desk.help')">
        <h3 class="label px-1">{{ t("desk.help") }}</h3>
        <div class="card divide-y divide-sep">
          <button v-if="native && app" type="button" class="flex w-full items-center gap-2 px-4.5 py-3 text-left font-semibold" @click="revealLog">
            <span class="flex-1">{{ t("desk.log") }}</span><AppIcon name="file" :size="14" class="text-muted" />
          </button>
          <a :href="GITHUB" target="_blank" rel="noopener" class="flex items-center gap-2 px-4.5 py-3 font-semibold">
            <span class="flex-1">{{ t("desk.github") }}</span><AppIcon name="arrowRight" :size="14" class="text-muted" />
          </a>
          <p class="px-4.5 py-3 text-xs text-muted">{{ t("set.version", { version: app?.version ?? store.state?.version ?? "" }) }}</p>
        </div>
      </section>

      <section v-if="native && app?.launcher" class="space-y-1.5" :aria-label="t('desk.advanced')">
        <h3 class="label px-1">{{ t("desk.advanced") }}</h3>
        <div class="card space-y-2 px-4.5 py-3.5">
          <div class="font-semibold">{{ t("desk.command") }}</div>
          <p class="text-[13px] text-muted">{{ t("desk.commandHelp") }}</p>
          <div class="flex items-center gap-2">
            <code class="min-w-0 flex-1 font-mono text-xs break-all">{{ app.launcher }}</code>
            <button type="button" class="btn btn-sm" @click="copy(app.launcher)">
              <AppIcon name="copy" :size="14" />{{ t("common.copy") }}
            </button>
          </div>
        </div>
      </section>
    </div>
  </ModalDialog>
</template>
