<script setup lang="ts">
import { reactive } from "vue";
import { api } from "../api";
import { t } from "../i18n";
import { act, store } from "../store";
import ModalDialog from "./ModalDialog.vue";

/** Ask for a new topic's title, file name, description and group; emits the created topic's slug. */
const emit = defineEmits<{ close: []; created: [slug: string] }>();
const form = reactive({ slug: "", title: "", description: "", group: "who" });

async function create() {
  if (!form.slug.trim() || !form.title.trim()) return;
  const created = await act(() => api.addTopic({
    slug: form.slug.trim(), title: form.title.trim(), description: form.description.trim() || undefined, group: form.group,
  }));
  if (created) emit("created", created.slug);
}
</script>

<template>
  <ModalDialog :title="t('mem.newTopic')" @close="emit('close')">
    <form class="space-y-4" @submit.prevent="create">
      <label class="block space-y-1.5">
        <span class="text-[13px] font-semibold">{{ t("mem.topicTitle") }}</span>
        <input v-model="form.title" class="input" required />
      </label>
      <label class="block space-y-1.5">
        <span class="text-[13px] font-semibold">{{ t("mem.topicSlug") }}</span>
        <input v-model="form.slug" class="input font-mono" pattern="[a-z0-9][a-z0-9-]*" placeholder="faith" required />
      </label>
      <label class="block space-y-1.5">
        <span class="text-[13px] font-semibold">{{ t("mem.topicDesc") }}</span>
        <input v-model="form.description" class="input" />
      </label>
      <select v-model="form.group" class="input select" :aria-label="t('mem.topics')">
        <option v-for="g in store.state?.groups" :key="g.id" :value="g.id">{{ g.name }}</option>
      </select>
      <div class="flex justify-end gap-2 pt-2">
        <button type="button" class="btn" @click="emit('close')">{{ t("common.cancel") }}</button>
        <button type="submit" class="btn btn-primary">{{ t("common.save") }}</button>
      </div>
    </form>
  </ModalDialog>
</template>
