<script setup lang="ts">
/**
 * 公式列表（右栏/左栏共用的"公式"页签）：**直接从 Markdown 里提取**，只读。
 *
 * 为什么这样：阅读器显示的公式就是模型看页图给出的 LaTeX，来源是页级产物的 Markdown；
 * 所以列表也从这份 Markdown 里抽（`GET /papers/{id}/formulas`，`source=markdown`），
 * 与页面上的公式一字不差。**不做复核、不分级、不比对裁图** —— 看到不对就复制 LaTeX 自己改。
 */
import { computed, ref } from 'vue';
import type { MarkdownFormula } from '@/api/types';
import { latexToHtml } from '@/composables/useMarkdown';
import AppIcon from '@/components/AppIcon.vue';

const props = withDefaults(defineProps<{
  items: MarkdownFormula[];
  loading?: boolean;
}>(), { loading: false });

const emit = defineEmits<{
  (e: 'focus', rowId: string, page?: number): void;
  (e: 'copy', text: string): void;
}>();

/** 默认只列行间公式（论文里编号的公式）；行内公式多达数百条，按需切换。 */
const filter = ref<'display' | 'inline' | 'all'>('display');
const filtered = computed(() => {
  if (filter.value === 'all') return props.items;
  const want = filter.value === 'display';
  return props.items.filter((f) => f.display === want);
});
const counts = computed(() => ({
  display: props.items.filter((f) => f.display).length,
  inline: props.items.filter((f) => !f.display).length,
}));
</script>

<template>
  <div class="fl">
    <div class="fl-bar">
      <div class="seg">
        <button :class="{ active: filter === 'display' }" @click="filter = 'display'">
          行间 {{ counts.display }}
        </button>
        <button :class="{ active: filter === 'inline' }" @click="filter = 'inline'">
          行内 {{ counts.inline }}
        </button>
        <button :class="{ active: filter === 'all' }" @click="filter = 'all'">
          全部 {{ props.items.length }}
        </button>
      </div>
    </div>

    <div v-if="props.loading" class="empty small">正在提取公式…</div>
    <div v-else-if="!filtered.length" class="empty small">这一篇还没有可提取的公式</div>

    <div v-for="f in filtered" :key="f.id" class="fitem">
      <div class="row tiny">
        <span class="badge">{{ f.display ? '行间' : '行内' }}</span>
        <span class="faint">p{{ f.page }}</span>
        <span class="spacer" />
        <button class="btn icon sm" title="复制 LaTeX" @click="emit('copy', f.latex)">
          <AppIcon name="copy" :size="13" />
        </button>
        <button
          v-if="f.row_id"
          class="btn icon sm"
          title="定位到正文"
          @click="emit('focus', f.row_id, f.page)"
        >
          <AppIcon name="locate" :size="13" />
        </button>
      </div>
      <div class="fbody" :class="{ display: f.display }" v-html="latexToHtml(f.latex, f.display)" />
      <div v-if="f.context" class="fctx tiny faint" :title="f.context">{{ f.context }}</div>
    </div>
  </div>
</template>

<style scoped>
.fl { display: flex; flex-direction: column; gap: 6px; }
.fl-bar { position: sticky; top: 0; z-index: 2; background: var(--bg-elev); padding-bottom: 4px; }
.fitem {
  border: 1px solid var(--border); border-radius: var(--radius-sm);
  background: var(--bg-layer); padding: 6px 8px;
}
.fbody { margin: 4px 0 2px; overflow-x: auto; overflow-y: hidden; font-size: 13px; }
.fbody.display { text-align: center; }
.fbody :deep(.katex) { font-size: 1.02em; }
.fctx { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
</style>
