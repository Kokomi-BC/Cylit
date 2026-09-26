<script setup lang="ts">
/**
 * 目录列表（**只负责画**）：条目、缩进、当前小节高亮、点击。
 *
 * 为什么要抽出来：同一份大纲有三处要画 —— 两个整页的左栏、阅读器左栏两个页签的紧凑版。
 * 点击行为（滚到哪、怎么滚）交给宿主：整页要滚窗口、左栏要滚面板内部，两边的"滚动容器"不同，
 * 而列表本身长得一样。
 *
 * 条目**按 Markdown 渲染**（`inlineHtml`，与阅读器自己的目录 `ReaderToc` 同一套链路）：
 * 标题里的 `$Q_{\text{mse}}$` 要出公式、`**加粗**` 要加粗，而不是把 LaTeX 源码摆在目录里。
 * 纯文本那份 `h.text` 只做 tooltip / aria —— 悬停时看的还是"人话"。
 *
 * 缩进按**层级差**算（相对大纲里最浅的一级），不是写死 `level * n`：
 * 模型有时整篇只用 `###`，按绝对层级缩进会平白多缩两格。
 */
import { computed } from 'vue';
import { inlineHtml } from '@/composables/useMarkdown';
import type { InsightHeading } from '@/utils/insightOutline';

const props = withDefaults(defineProps<{
  items: InsightHeading[];
  /** 当前小节（滚动联动 / 刚点过的那条） */
  activeId?: string;
  /** 紧凑版（阅读器左栏）：一行一条、缩进更小、字号更小 */
  compact?: boolean;
}>(), { activeId: '', compact: false });

const emit = defineEmits<{ (e: 'select', id: string): void }>();

/** 最浅一级做基准：整篇都是 `##` 时不缩进 */
const minLevel = computed(() => props.items.reduce((n, h) => Math.min(n, h.level), 6));
const indent = (level: number) => Math.max(0, level - minLevel.value) * (props.compact ? 8 : 12);

/** 渲染结果一次算好：目录行数不少，别在滚动联动触发的重渲染里反复解析 */
const rows = computed(() => props.items.map((h) => ({
  h,
  html: inlineHtml(h.md) || inlineHtml(h.text),   // 极端输入兜底，别渲染出一片空白
})));
</script>

<template>
  <nav class="toc-list" :class="{ compact }">
    <button
      v-for="r in rows"
      :key="r.h.id"
      class="toc-item"
      :class="{ active: r.h.id === activeId, deep: r.h.level - minLevel > 0 }"
      :style="{ paddingLeft: `${indent(r.h.level)}px` }"
      :title="r.h.text"
      @click="emit('select', r.h.id)"
    >
      <span class="label md" v-html="r.html" />
    </button>
    <div v-if="!items.length" class="empty small">这一篇没有小节标题</div>
  </nav>
</template>

<style scoped>
.toc-list { display: flex; flex-direction: column; gap: 1px; min-width: 0; }
/* 一条 = 一行；长标题截断（目录是导航，不是正文）。
   `text-overflow` 对**行内 HTML**（KaTeX 的 span、`<strong>`）同样生效：截断发生在按钮这一层的行盒上。 */
.toc-item {
  display: block; width: 100%; text-align: left; border: 0; border-radius: var(--radius-sm);
  background: transparent; color: var(--text-dim); font: inherit; font-size: 12.5px;
  line-height: 1.45; padding: 4px 6px; cursor: pointer;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  /* hover/active 只改底色与字色：加边框会让整列在滚动时抖动 */
  transition: background var(--dur-fast) var(--ease), color var(--dur-fast) var(--ease);
}
.toc-item:hover { background: var(--bg-hover); color: var(--text); }
.toc-item.active { background: var(--accent-soft); color: var(--accent-text); font-weight: 600; }
.toc-item.deep { font-size: 12px; }
.toc-item .label { display: inline; }
/* KaTeX 默认 1.21em，在 12px 的目录行里会比中文大一圈；压回行内尺寸
   （与阅读器目录 `ReaderToc` 的同名规则一致，两处目录才看着是同一套东西）。 */
.toc-item .label :deep(.katex) { font-size: 1em; }
.toc-item .label :deep(strong) { font-weight: 600; }
.toc-item .label :deep(code) { font-size: .95em; padding: 0 3px; }
/* 行间公式（`$$…$$`）不该出现在标题里，但万一出现，别把目录行撑成三行 */
.toc-item .label :deep(.katex-display) { display: inline; margin: 0; }
/* 紧凑版（阅读器左栏）：字号再小一档，条目更密 */
.toc-list.compact .toc-item { font-size: 11.5px; padding: 3px 5px; line-height: 1.4; }
.toc-list.compact .toc-item.deep { font-size: 11px; }
</style>
