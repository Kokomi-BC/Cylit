<script setup lang="ts">
/**
 * 目录列表（**只负责画**）：条目、缩进、当前小节高亮、点击。
 *
 * 为什么要抽出来：同一份大纲有三处要画 —— 两个整页的左栏、阅读器左栏两个页签的紧凑版。
 * 点击行为（滚到哪、怎么滚）交给宿主：整页要滚窗口、左栏要滚面板内部，两边的"滚动容器"不同，
 * 而列表本身长得一样。
 *
 * 缩进按**层级差**算（相对大纲里最浅的一级），不是写死 `level * n`：
 * 模型有时整篇只用 `###`，按绝对层级缩进会平白多缩两格。
 */
import { computed } from 'vue';
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
</script>

<template>
  <nav class="toc-list" :class="{ compact }">
    <button
      v-for="h in items"
      :key="h.id"
      class="toc-item"
      :class="{ active: h.id === activeId, deep: h.level - minLevel > 0 }"
      :style="{ paddingLeft: `${indent(h.level)}px` }"
      :title="h.text"
      @click="emit('select', h.id)"
    >
      {{ h.text }}
    </button>
    <div v-if="!items.length" class="empty small">这一篇没有小节标题</div>
  </nav>
</template>

<style scoped>
.toc-list { display: flex; flex-direction: column; gap: 1px; min-width: 0; }
/* 一条 = 一行；长标题截断（目录是导航，不是正文） */
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
/* 紧凑版（阅读器左栏）：字号再小一档，条目更密 */
.toc-list.compact .toc-item { font-size: 11.5px; padding: 3px 5px; line-height: 1.4; }
.toc-list.compact .toc-item.deep { font-size: 11px; }
</style>
