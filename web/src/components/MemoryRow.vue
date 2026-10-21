<script setup lang="ts">
import { nextTick, ref } from "vue";
import { api, type Kind, type Memory } from "../api";
import AppIcon from "./AppIcon.vue";
import DropMenu from "./DropMenu.vue";
import { relTime, sourceLabel, t } from "../i18n";
import { srcLabel } from "../memoryList";
import { SUPERSEDED, saveText, saving } from "../memorySaves";
import { act, connectorName, store, toast } from "../store";

/** `freshFrom`: the app whose change to this memory is still in the digest (shows a "New" chip). */
const props = defineProps<{ memory: Memory; showTopic?: string; freshFrom?: string }>();
const emit = defineEmits<{ history: [Memory] }>();

const editing = ref(false);
const draft = ref("");
const input = ref<HTMLTextAreaElement | null>(null);

// Only what the user typed is written back: an edit box left without typing writes nothing, even when an
// AI app or an editor changed the memory while it was open. Typing is counted, not inferred from the text.
let typed = 0; // input events so far
let startedAt = 0; // the count when this edit began
let baseline = ""; // the memory's text when this edit began

async function startEdit() {
  draft.value = baseline = props.memory.content;
  startedAt = typed;
  editing.value = true;
  await nextTick();
  input.value?.focus();
}

async function save() {
  if (!editing.value) return; // Return already saved; the box going away mustn't save again
  editing.value = false;
  const { id, content, last_actor } = props.memory;
  const text = draft.value.trim();
  if (typed === startedAt || !text) return;
  // With a save on its way, the memory's text here is out of date, so it can't tell that nothing changed.
  if (!saving(id) && text === content) return;
  // Someone else changed it meanwhile: the user's words win, and they hear that theirs is in the history.
  const meanwhile = content !== baseline && last_actor !== "user";
  const saved = await saveText(id, text); // one save per memory at a time, newest last (memorySaves.ts)
  if (saved && saved !== SUPERSEDED && meanwhile) toast(t("ins.changedMeanwhile", { who: srcLabel(last_actor) }));
}

function onKey(e: KeyboardEvent) {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
    e.preventDefault();
    save();
  } else if (e.key === "Escape") {
    editing.value = false;
  }
}

const m = () => props.memory;
const patch = (body: Parameters<typeof api.patchMemory>[1]) => act(() => api.patchMemory(m().id, body));
const setKind = (kind: Kind) => patch({ kind });
const archive = () => act(() => api.archive(m().id));
const restore = () => act(() => api.restore(m().id));
const togglePrivate = () => patch({ private: !m().private });
const move = (topic: string) => patch({ topic });
</script>

<template>
  <div class="group relative flex gap-3 px-4.5 py-3 transition-colors hover:bg-hover">
    <div class="min-w-0 flex-1">
      <textarea
        v-if="editing"
        ref="input"
        v-model="draft"
        rows="2"
        class="input text-[15px]"
        :aria-label="t('mem.text')"
        @input="typed++"
        @keydown="onKey"
        @blur="save"
      />
      <p v-else class="cursor-text text-[15px] leading-relaxed break-words" @dblclick="startEdit">
        <AppIcon v-if="memory.pinned" name="starFill" :size="14" class="mr-1.5 inline align-[-2px] text-wait" />
        <span class="sr-only" v-if="memory.pinned">{{ t("mem.pinned") }}: </span>{{ memory.content }}
      </p>
      <div class="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-muted">
        <a
          v-if="freshFrom"
          href="#/changes"
          class="chip chip-fresh hover:brightness-95"
          :title="t('mem.newTitle', { app: freshFrom })"
        >{{ t("mem.new") }}</a>
        <select
          class="chip cursor-pointer appearance-none border-0 outline-none [field-sizing:content] hover:bg-fill-strong"
          :value="memory.kind"
          :aria-label="t('mem.kinds')"
          @change="setKind(($event.target as HTMLSelectElement).value as Kind)"
        >
          <option v-for="k in store.state?.kinds" :key="k" :value="k">{{ t(`kind.${k}`) }}</option>
        </select>
        <span v-if="showTopic" class="chip">{{ showTopic }}</span>
        <span v-if="memory.effective_private" class="chip chip-private"><AppIcon name="lock" :size="11" />{{ t("common.private") }}</span>
        <span>{{ memory.source.startsWith("connector:") ? connectorName(memory.source.slice(10)) : sourceLabel(memory.source) }}</span>
        <span aria-hidden="true">·</span>
        <span :title="memory.updated_at">{{ relTime(memory.updated_at) }}</span>
        <template v-if="memory.until">
          <span aria-hidden="true">·</span>
          <span :class="memory.review_due ? 'font-semibold text-wait-ink' : ''">{{ t("mem.until", { date: memory.until }) }}</span>
        </template>
        <template v-if="memory.version > 1">
          <span aria-hidden="true">·</span><span>v{{ memory.version }}</span>
        </template>
      </div>
    </div>

    <div class="flex shrink-0 items-start gap-0.5 transition-opacity md:opacity-0 md:group-focus-within:opacity-100 md:group-hover:opacity-100 md:has-[[aria-expanded=true]]:opacity-100">
      <template v-if="memory.status === 'active'">
        <button class="btn btn-ghost btn-icon max-md:hidden" :title="t('mem.edit')" :aria-label="t('mem.edit')" @click="startEdit">
          <AppIcon name="pencil" />
        </button>
        <button
          class="btn btn-ghost btn-icon max-md:hidden"
          :class="memory.pinned ? 'text-wait' : ''"
          :title="memory.pinned ? t('mem.unpin') : t('mem.pin')"
          :aria-label="memory.pinned ? t('mem.unpin') : t('mem.pin')"
          :aria-pressed="memory.pinned"
          @click="patch({ pinned: !memory.pinned })"
        >
          <AppIcon :name="memory.pinned ? 'starFill' : 'star'" />
        </button>
        <DropMenu :label="t('common.more')">
          <!-- phones have no hover, so the row's own buttons live here -->
          <button class="menu-item md:hidden" role="menuitem" @click="startEdit">
            <AppIcon name="pencil" class="text-muted" />{{ t("mem.edit") }}
          </button>
          <button class="menu-item md:hidden" role="menuitem" @click="patch({ pinned: !memory.pinned })">
            <AppIcon :name="memory.pinned ? 'starFill' : 'star'" class="text-muted" />{{ memory.pinned ? t("mem.unpin") : t("mem.pin") }}
          </button>
          <button class="menu-item" role="menuitem" @click="togglePrivate">
            <AppIcon name="lock" class="text-muted" />{{ memory.private ? t("mem.makePublic") : t("mem.makePrivate") }}
          </button>
          <button class="menu-item" role="menuitem" @click="emit('history', memory)">
            <AppIcon name="clock" class="text-muted" />{{ t("mem.history") }}
          </button>
          <div class="menu-sep" />
          <div class="px-2.5 py-1 text-xs text-muted">{{ t("mem.moveTo") }}</div>
          <div class="max-h-48 overflow-y-auto">
            <button
              v-for="tp in store.state?.topics.filter((x) => x.slug !== memory.topic)"
              :key="tp.slug"
              class="menu-item py-1 text-[13px]"
              role="menuitem"
              @click="move(tp.slug)"
            >{{ tp.title }}</button>
          </div>
          <div class="menu-sep" />
          <button class="menu-item text-danger hover:bg-danger-soft" role="menuitem" @click="archive">
            {{ t("mem.archive") }}
          </button>
        </DropMenu>
      </template>
      <template v-else>
        <button class="btn btn-sm" @click="restore">{{ t("mem.restore") }}</button>
        <button class="btn btn-ghost btn-sm" @click="emit('history', memory)">{{ t("mem.history") }}</button>
      </template>
    </div>
  </div>
</template>
