<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import AppIcon from "./components/AppIcon.vue";
import AppToast from "./components/AppToast.vue";
import DesktopSidebar from "./components/DesktopSidebar.vue";
import DropMenu from "./components/DropMenu.vue";
import NewTopicDialog from "./components/NewTopicDialog.vue";
import SettingsSheet from "./components/SettingsSheet.vue";
import { desktop, onCommand, ui, type Command } from "./desktop";
import type { IconName } from "./icons";
import { t } from "./i18n";
import { refresh, store, toReview } from "./store";
import ActivityView from "./views/ActivityView.vue";
import ChangesView from "./views/ChangesView.vue";
import ConnectorsView from "./views/ConnectorsView.vue";
import ContextView from "./views/ContextView.vue";
import ImportView from "./views/ImportView.vue";
import MemoriesDesktopView from "./views/MemoriesDesktopView.vue";
import MemoriesView from "./views/MemoriesView.vue";
import WelcomeView from "./views/WelcomeView.vue";

const ROUTES = ["memories", "changes", "context", "connectors", "activity", "import", "welcome"] as const;
type Route = (typeof ROUTES)[number];
/** Old names that links in the wild (MCP results, docs) still use. */
const ALIASES: Record<string, Route> = { review: "changes" };

/** The route in the hash; `null` when there is none yet (`#/` or nothing), which picks a home page. */
function parseHash(): { route: Route | null; param: string | null } {
  const [name, param] = location.hash.replace(/^#\/?/, "").split("/");
  const alias = Object.hasOwn(ALIASES, name) ? ALIASES[name] : null;
  if (alias) history.replaceState(null, "", `#/${alias}${param ? `/${param}` : ""}`);
  const route = alias ?? ((ROUTES as readonly string[]).includes(name) ? (name as Route) : name ? "memories" : null);
  return { route, param: param ? decodeURIComponent(param) : null };
}

const current = ref(parseHash());

/**
 * With no route: the first-run guide while the folder hasn't had it (`onboarded`), otherwise Changes when
 * something waits or changed since the user last looked, otherwise Memories.
 */
function settleHome() {
  if (current.value.route !== null || !store.state) return;
  const route: Route =
    store.state.settings.onboarded === false ? "welcome" : toReview.value > 0 ? "changes" : "memories";
  history.replaceState(null, "", `#/${route}`);
  current.value = { route, param: null };
}

const onHash = () => {
  current.value = parseHash();
  settleHome();
};
watch(() => store.state, settleHome);

/** Mac app: menu commands and shortcuts. "New memory" and "find" belong to the Memories screen. */
function onDesktopCommand(cmd: Command) {
  if (cmd === "toggle-sidebar") ui.sidebarHidden = !ui.sidebarHidden;
  else if (cmd === "settings") ui.settingsOpen = true;
  else {
    ui.request = cmd;
    if (current.value.route !== "memories") location.hash = "#/memories";
  }
}
const newTopic = ref(false);
function openNewTopic(slug: string) {
  newTopic.value = false;
  location.hash = `#/memories/${encodeURIComponent(slug)}`;
}
let stopCommands: (() => void) | undefined;

let timer: number | undefined;
onMounted(async () => {
  window.addEventListener("hashchange", onHash);
  if (desktop) stopCommands = onCommand(onDesktopCommand);
  await refresh();
  // AI apps and the user's editor change the vault behind our back; keep the panel fresh.
  timer = window.setInterval(() => {
    if (document.visibilityState === "visible") refresh();
  }, 4000);
});
onBeforeUnmount(() => {
  window.removeEventListener("hashchange", onHash);
  stopCommands?.();
  window.clearInterval(timer);
});

type NavItem = { id: Route; label: string; icon: IconName; badge?: number | null; alert?: boolean };

const nav = computed<NavItem[]>(() => [
  // "Get started" until the first-run guide is finished or skipped.
  ...(store.state?.settings.onboarded === false ? [{ id: "welcome", label: t("onb.nav"), icon: "star" } as const] : []),
  { id: "memories", label: t("nav.memories"), icon: "memories", badge: store.state?.counts.active || null },
  { id: "changes", label: t("nav.changes"), icon: "changes", badge: toReview.value || null, alert: toReview.value > 0 },
  { id: "context", label: t("nav.context"), icon: "eye" },
  { id: "connectors", label: t("nav.connectors"), icon: "plug", badge: store.connectors.length || null },
  { id: "activity", label: t("nav.activity"), icon: "clock" },
  { id: "import", label: t("nav.import"), icon: "download" },
]);

/** Phones: three tabs, and the rest behind "More". */
const TABS: Route[] = ["memories", "changes", "context"];
const tabs = computed(() => nav.value.filter((n) => TABS.includes(n.id)));
const moreItems = computed(() => nav.value.filter((n) => !TABS.includes(n.id)));
const moreActive = computed(() => moreItems.value.some((n) => n.id === current.value.route));
</script>

<template>
  <!-- the Mac app (desktop.ts): sidebar, then a toolbar and the page; the window's buttons sit top left -->
  <div v-if="desktop" class="flex h-screen overflow-hidden">
    <DesktopSidebar
      v-if="!ui.sidebarHidden"
      :nav="nav"
      :route="current.route"
      :topic="current.route === 'memories' ? current.param : null"
      @new-topic="newTopic = true"
    />
    <div class="flex min-w-0 flex-1 flex-col">
      <template v-if="!store.state">
        <div class="h-[52px] shrink-0" data-tauri-drag-region />
        <div class="flex flex-1 items-center justify-center text-muted">{{ t("common.loading") }}</div>
      </template>
      <MemoriesDesktopView v-else-if="current.route === 'memories'" :initial-topic="current.param" class="min-h-0 flex-1" />
      <template v-else>
        <div id="desktop-toolbar" class="contents" />
        <main class="min-h-0 flex-1 overflow-y-auto bg-ground">
          <div class="mx-auto w-full max-w-[880px] px-8 pt-6 pb-12">
            <ChangesView v-if="current.route === 'changes'" :initial-tab="current.param" />
            <ContextView v-else-if="current.route === 'context'" :initial-connector="current.param" />
            <ConnectorsView v-else-if="current.route === 'connectors'" :initial-connector="current.param" />
            <ActivityView v-else-if="current.route === 'activity'" />
            <ImportView v-else-if="current.route === 'import'" />
            <WelcomeView v-else-if="current.route === 'welcome'" :initial-step="current.param" />
          </div>
        </main>
      </template>
    </div>

    <div
      v-if="store.offline"
      role="alert"
      class="notice notice-danger fixed bottom-4 left-1/2 z-40 w-[min(560px,90vw)] -translate-x-1/2 bg-surface shadow-lg"
    >
      <AppIcon name="warning" />{{ t("mac.offline") }}
    </div>
    <NewTopicDialog
      v-if="newTopic"
      @close="newTopic = false"
      @created="openNewTopic"
    />
    <SettingsSheet v-if="ui.settingsOpen" @close="ui.settingsOpen = false" />
    <AppToast />
  </div>

  <div v-else class="min-h-screen md:flex">
    <!-- desktop: the sidebar; MemoriesView teleports its topics into #sidebar-extra -->
    <aside class="sticky top-0 hidden h-screen w-[248px] shrink-0 flex-col gap-5 overflow-y-auto px-3.5 pt-5 pb-6 md:flex">
      <a href="#/memories" class="flex items-center gap-2.5 px-2.5 text-base font-semibold tracking-[-0.01em]">
        <img src="/logo.png" alt="" class="size-7 rounded-lg" />
        OpenMyMemo
      </a>
      <nav :aria-label="t('nav.sections')" class="flex flex-col gap-0.5">
        <a
          v-for="item in nav"
          :key="item.id"
          :href="`#/${item.id}`"
          class="flex min-h-9 items-center gap-2.5 rounded-[9px] px-2.5 text-[15px] transition-colors"
          :class="current.route === item.id ? 'bg-fill-strong font-semibold' : 'hover:bg-fill'"
          :aria-current="current.route === item.id ? 'page' : undefined"
        >
          <AppIcon :name="item.icon" :size="18" class="text-accent" />
          <span class="flex-1">{{ item.label }}</span>
          <span v-if="item.badge && item.alert" class="badge">{{ item.badge }}</span>
          <span v-else-if="item.badge" class="text-[13px] text-muted tabular-nums">{{ item.badge }}</span>
        </a>
      </nav>
      <div id="sidebar-extra" class="contents" />
    </aside>

    <div class="flex min-w-0 flex-1 flex-col">
      <div v-if="store.offline" class="notice notice-danger m-4 mb-0 md:mx-10 md:mt-6">
        <AppIcon name="warning" />{{ t("app.offline") }}
      </div>

      <main class="mx-auto w-full max-w-[960px] flex-1 px-4 pt-6 pb-28 sm:px-6 md:px-10 md:pt-8 md:pb-10">
        <div v-if="!store.state" class="py-24 text-center text-muted">{{ t("common.loading") }}</div>
        <template v-else>
          <MemoriesView v-if="current.route === 'memories'" :initial-topic="current.param" />
          <ChangesView v-else-if="current.route === 'changes'" :initial-tab="current.param" />
          <ContextView v-else-if="current.route === 'context'" :initial-connector="current.param" />
          <ConnectorsView v-else-if="current.route === 'connectors'" :initial-connector="current.param" />
          <ActivityView v-else-if="current.route === 'activity'" />
          <ImportView v-else-if="current.route === 'import'" />
          <WelcomeView v-else-if="current.route === 'welcome'" :initial-step="current.param" />
        </template>

        <footer class="mx-auto mt-12 max-w-[820px] text-xs text-muted">
          {{ t("app.tagline") }} · <span class="font-mono break-all">{{ store.state?.vault }}</span> · v{{ store.state?.version }}
        </footer>
      </main>
    </div>

    <!-- phones: a tab bar -->
    <nav
      :aria-label="t('nav.sections')"
      class="fixed inset-x-0 bottom-0 z-30 grid grid-cols-4 border-t border-sep bg-surface/90 px-2 pt-1.5 pb-[max(env(safe-area-inset-bottom),0.5rem)] backdrop-blur-xl md:hidden"
    >
      <a
        v-for="item in tabs"
        :key="item.id"
        :href="`#/${item.id}`"
        class="relative flex min-h-11 flex-col items-center justify-center gap-0.5 text-[11px] font-medium"
        :class="current.route === item.id ? 'text-accent' : 'text-muted'"
        :aria-current="current.route === item.id ? 'page' : undefined"
      >
        <AppIcon :name="item.icon" :size="24" />
        {{ item.id === "context" ? t("nav.contextShort") : item.label }}
        <span v-if="item.alert && item.badge" class="badge absolute top-0 left-1/2 ml-2">{{ item.badge }}</span>
      </a>
      <DropMenu
        :label="t('nav.more')"
        up
        width="w-60"
        button-class="flex min-h-11 w-full flex-col items-center justify-center gap-0.5 text-[11px] font-medium"
        :class="moreActive ? 'text-accent' : 'text-muted'"
      >
        <template #button><AppIcon name="grid" :size="24" />{{ t("nav.more") }}</template>
        <a
          v-for="item in moreItems"
          :key="item.id"
          :href="`#/${item.id}`"
          class="menu-item min-h-11 text-[15px] text-ink"
          role="menuitem"
        >
          <AppIcon :name="item.icon" :size="18" class="text-accent" />
          <span class="flex-1">{{ item.label }}</span>
          <span v-if="item.badge" class="text-[13px] text-muted tabular-nums">{{ item.badge }}</span>
        </a>
      </DropMenu>
    </nav>

    <AppToast />
  </div>
</template>
