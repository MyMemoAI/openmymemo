<script setup lang="ts">
import { reactive, watch } from "vue";
import { ui } from "../desktop";
import type { IconName } from "../icons";
import { t, tn } from "../i18n";
import { freshTopics, store, topicsBySlug, waitingTopics } from "../store";
import AppIcon from "./AppIcon.vue";

/** The Mac app's sidebar: sections, topics by group, and whether the local server is running. */
type NavItem = { id: string; label: string; icon: IconName; badge?: number | null; alert?: boolean };
const props = defineProps<{ nav: NavItem[]; route: string | null; topic: string | null }>();
const emit = defineEmits<{ newTopic: [] }>();

/** Collapsed groups, remembered in this browser only. */
const KEY = "openmymemo.collapsed";
const collapsed = reactive(new Set<string>(read()));
function read(): string[] {
  try {
    return JSON.parse(localStorage.getItem(KEY) ?? "[]");
  } catch {
    return [];
  }
}
watch(collapsed, (v) => {
  try {
    localStorage.setItem(KEY, JSON.stringify([...v]));
  } catch {
    /* private window: not remembered */
  }
});
const toggle = (id: string) => (collapsed.has(id) ? collapsed.delete(id) : collapsed.add(id));
const groupCount = (slugs: string[]) => slugs.reduce((n, s) => n + (topicsBySlug.value[s]?.count ?? 0), 0);

const onMemories = () => props.route === "memories";
const row = (on: boolean, muted = false) => [
  "flex min-h-[26px] items-center justify-between gap-2 rounded-md px-2 text-[13px]",
  on ? "bg-sidebar-on font-semibold" : muted ? "text-muted" : "",
];
</script>

<template>
  <aside class="flex h-full w-[232px] shrink-0 flex-col border-r border-sep bg-sidebar px-2.5 pb-3" data-chrome>
    <!-- the window's own close / minimise / zoom buttons sit at the left of this strip -->
    <div class="flex h-[52px] shrink-0 items-center justify-end" data-tauri-drag-region>
      <button
        type="button"
        class="btn btn-ghost btn-icon text-ink-2"
        :aria-label="t('mac.hideSidebar')"
        :title="`${t('mac.hideSidebar')} ⌃⌘S`"
        @click="ui.sidebarHidden = true"
      >
        <AppIcon name="sidebar" :size="18" />
      </button>
    </div>

    <div class="-mx-2.5 min-h-0 flex-1 overflow-y-auto px-2.5">
      <nav :aria-label="t('nav.sections')" class="flex flex-col gap-px">
        <a
          v-for="item in nav"
          :key="item.id"
          :href="`#/${item.id}`"
          class="flex min-h-7 items-center gap-2 rounded-md px-2 text-[13px]"
          :class="route === item.id ? ['font-semibold', item.id === 'memories' ? '' : 'bg-sidebar-on'] : ''"
          :aria-current="route === item.id ? 'page' : undefined"
        >
          <AppIcon :name="item.icon" class="text-accent" />
          <span class="flex-1">{{ item.label }}</span>
          <span v-if="item.badge && item.alert" class="badge min-h-[18px] text-[11px]">{{ item.badge }}</span>
          <span v-else-if="item.badge" class="text-xs font-normal text-ink-2 tabular-nums">{{ item.badge }}</span>
        </a>
      </nav>

      <nav :aria-label="t('mem.topics')" class="mt-3.5 flex flex-col gap-1.5">
        <a href="#/memories" :class="row(onMemories() && !topic)" :aria-current="onMemories() && !topic ? 'true' : undefined">
          <span>{{ t("mac.allMemories") }}</span>
          <span class="text-xs font-normal text-ink-2 tabular-nums">{{ store.state?.counts.active }}</span>
        </a>

        <div v-for="g in store.state?.groups" :key="g.id" class="flex flex-col gap-px">
          <button
            type="button"
            class="flex min-h-6 w-full items-center gap-1 px-2 text-left text-[11px] font-semibold text-ink-2"
            :aria-expanded="!collapsed.has(g.id)"
            @click="toggle(g.id)"
          >
            <AppIcon :name="collapsed.has(g.id) ? 'chevronRight' : 'chevronDown'" :size="10" class="stroke-[3]" />
            <span class="flex-1">{{ g.name }}</span>
            <span v-if="collapsed.has(g.id)" class="font-normal tabular-nums">{{ groupCount(g.topics) || "" }}</span>
          </button>
          <template v-if="!collapsed.has(g.id)">
            <a
              v-for="slug in g.topics"
              :key="slug"
              :href="`#/memories/${encodeURIComponent(slug)}`"
              :class="row(onMemories() && topic === slug, !topicsBySlug[slug]?.count)"
              :aria-current="onMemories() && topic === slug ? 'true' : undefined"
            >
              <span class="flex min-w-0 items-center gap-1.5">
                <AppIcon v-if="topicsBySlug[slug]?.private" name="lock" :size="12" class="text-ink-2" />
                <span class="truncate">{{ topicsBySlug[slug]?.title }}</span>
              </span>
              <span class="flex items-center gap-1.5 text-xs font-normal text-ink-2 tabular-nums">
                <span v-if="waitingTopics.has(slug)" class="dot bg-wait" />
                <span v-if="freshTopics.has(slug)" class="dot bg-fresh" />
                {{ topicsBySlug[slug]?.count || "" }}
              </span>
            </a>
          </template>
        </div>

        <button type="button" class="btn btn-link btn-sm min-h-7 self-start px-2 text-xs" @click="emit('newTopic')">
          <AppIcon name="plus" :size="12" class="stroke-[2.4]" />{{ t("mem.newTopic") }}
        </button>
      </nav>
    </div>

    <div class="pt-3">
      <div class="flex items-center gap-2.5 rounded-[10px] bg-surface/65 py-2 pr-1.5 pl-3 shadow-[0_0_0_1px_var(--color-ring)]">
        <span
          class="size-2 shrink-0 rounded-full"
          :class="store.offline || !store.state ? 'bg-faint' : 'bg-fresh shadow-[0_0_0_3px_var(--color-fresh-soft)]'"
          aria-hidden="true"
        />
        <span class="flex min-w-0 flex-1 flex-col leading-tight" role="status">
          <span class="text-xs font-semibold">{{ store.offline || !store.state ? t("mac.starting") : t("mac.running") }}</span>
          <span class="truncate text-[11px] text-ink-2">
            {{ store.connectors.length ? tn("mac.connected", store.connectors.length) : t("mac.noApps") }}
          </span>
        </span>
        <button
          type="button"
          class="btn btn-ghost btn-icon text-ink-2"
          :aria-label="t('set.title')"
          :title="`${t('set.title')} ⌘,`"
          @click="ui.settingsOpen = true"
        >
          <AppIcon name="sliders" />
        </button>
      </div>
    </div>
  </aside>
</template>
