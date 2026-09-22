<script setup lang="ts">
/**
 * 一行对照中的**一侧**（原文或译文）。
 *
 * 两侧用同一套排版参数（字号 / 行距 / 内边距），差别只在字体族：
 * 英文用衬线、中文用无衬线 —— 这是"对照时两侧文字观感一致"的关键。
 */
import { computed } from 'vue';
import type { Highlight, PageRow } from '@/api/types';
import { htmlWithHighlights } from '@/composables/useMarkdown';
import AppIcon from '@/components/AppIcon.vue';

const props = defineProps<{
  row: PageRow;
  side: 'en' | 'zh';
  highlights: Highlight[];
  /** 纯译文模式下：该行还没有译文 */
  missing?: boolean;
  /** 紧凑模式（窄屏 / 纯原文译文单栏时用） */
  compact?: boolean;
}>();

const emit = defineEmits<{
  (e: 'copy', payload: { row: PageRow; side: 'en' | 'zh' }): void;
}>();

const text = computed(() => (props.side === 'zh' ? props.row.translation : props.row.original));
const html = computed(() => {
  const list = (props.highlights ?? []).filter((h) => h.block_id === props.row.id);
  return htmlWithHighlights(text.value ?? '', list);
});
const level = computed(() => (props.row.type === 'heading' ? Math.min(4, Math.max(1, props.row.level || 2)) : 0));
const isEmpty = computed(() => !String(text.value ?? '').trim());
</script>

<template>
  <div
    :class="['rrow', `side-${props.side}`, `t-${props.row.type}`,
             { heading: level > 0, [`lvl-${level}`]: level > 0, empty: isEmpty, compact: props.compact }]"
    :data-side="props.side"
  >
    <div v-if="html" class="rr-body md" v-html="html" />
    <div v-else class="rr-empty">
      {{ props.side === 'zh' ? (props.missing ? '未翻译' : '—') : '—' }}
    </div>
    <button
      v-if="!isEmpty"
      class="rr-copy"
      :title="props.side === 'zh' ? '复制译文' : '复制原文'"
      @click.stop="emit('copy', { row: props.row, side: props.side })"
    >
      <AppIcon name="copy" :size="13" />
    </button>
  </div>
</template>

<style scoped>
/* 字号继承 `.rows-wrap` 的 --reader-font-size：这里全部用 em，正文/标题/图注/公式一起缩放。
   两侧只有字体族不同（英文衬线 / 中文无衬线），字号与行距完全一致。 */
.rrow { min-width: 0; cursor: text; position: relative; font-size: 1em; }
.rr-copy {
  position: absolute; top: -2px; right: 0; border: 0; background: var(--bg-elev);
  color: var(--text-faint); border-radius: var(--radius-sm); padding: 2px 4px;
  cursor: pointer; opacity: 0; transition: opacity .12s; box-shadow: var(--shadow-1);
}
.rrow:hover .rr-copy { opacity: 1; }
.rr-copy:hover { color: var(--text); }
.rr-body { font-size: 1em; line-height: 1.78; overflow-wrap: anywhere; }
.rr-body :deep(p) { margin: 0 0 .55em; }
.rr-body :deep(p:last-child) { margin-bottom: 0; }
.rr-body :deep(h1), .rr-body :deep(h2), .rr-body :deep(h3), .rr-body :deep(h4) {
  margin: 0 0 .35em; font-weight: 600; line-height: 1.35;
}
.rr-body :deep(h1) { font-size: 1.4em; }
.rr-body :deep(h2) { font-size: 1.24em; }
.rr-body :deep(h3) { font-size: 1.1em; }
.rr-body :deep(h4) { font-size: 1em; }
.rr-body :deep(ul), .rr-body :deep(ol) { margin: .3em 0; padding-left: 1.35em; }
.rr-body :deep(.katex-display) { margin: .5em 0; overflow-x: auto; overflow-y: hidden; }
.side-en .rr-body { font-family: var(--serif); }
.side-zh .rr-body { font-family: var(--sans); }
.t-formula .rr-body { text-align: center; }
.t-caption .rr-body { font-size: .92em; color: var(--text-dim); }
.t-ref .rr-body { font-size: .88em; color: var(--text-dim); }
/* 表格：数字不折行、右对齐（论文表格的读法）；放不下时横向滚动而不是压折 */
.t-table .rr-body { overflow-x: auto; }
.t-table .rr-body :deep(table) {
  border-collapse: collapse; width: 100%; font-size: .92em;
  font-variant-numeric: tabular-nums;
}
.t-table .rr-body :deep(th), .t-table .rr-body :deep(td) {
  white-space: nowrap; padding: 3px 7px; text-align: right; border: 0;
  border-bottom: 1px solid var(--divider);
}
.t-table .rr-body :deep(th) { font-weight: 600; color: var(--text-dim); }
.t-table .rr-body :deep(th:first-child), .t-table .rr-body :deep(td:first-child) {
  text-align: left;
}
.rr-empty { font-size: 1em; color: var(--text-faint); }
.rrow.compact .rr-body { font-size: .96em; line-height: 1.7; }
</style>
