<script setup lang="ts">
/**
 * 表格 / 公式的**浮层**（用户口径：侧栏列表里点「浮层打开」，把这一块放大来看）。
 *
 * 第一版是一张**居中卡片 + 全屏遮罩**，用户口径否掉了：要的是「像笔记浮层那样」
 * —— 可拖动、可缩放、可锚定、不带遮罩。所以这里不再自己画壳子，直接复用 `FloatingCard`
 * （笔记 / AI 解释浮层用的同一套外壳）：拖动条、四角缩放、锚定、位置记忆、字号档全都有。
 *
 * 本组件只负责两件事：① 标题行左边那枚图标；② 浮层里的内容本身（Markdown 渲染）。
 * 关法：右上角 ×、Esc（与站内其他浮层一致）。
 */
import { onBeforeUnmount, onMounted, ref } from 'vue';
import AppIcon from '@/components/AppIcon.vue';
import FloatingCard from './FloatingCard.vue';
import { mdToHtml } from '@/composables/useMarkdown';

const props = defineProps<{
  icon: string;
  title: string;
  /** 内容本体：GFM Markdown（表格是 `| … |`，公式是 `$$…$$`） */
  markdown: string;
}>();
const emit = defineEmits<{ (e: 'close'): void }>();

/** 锚定态：是「鼠标移开也不关」那个图钉。浮层默认钉住 —— 它是用户主动打开的，不该自己消失。 */
const pinned = ref(true);

function onKey(ev: KeyboardEvent) {
  if (ev.key !== 'Escape') return;
  ev.stopPropagation();
  emit('close');
}
/* 捕获阶段：阅读器自己也监听 Esc（收浮层/退出全屏），浮层开着时这一下先归这里 */
onMounted(() => window.addEventListener('keydown', onKey, true));
onBeforeUnmount(() => window.removeEventListener('keydown', onKey, true));
</script>

<template>
  <FloatingCard
    :open="true"
    :title="props.title"
    :anchored="pinned"
    zoomable
    :width="480"
    :max-height="520"
    persist-key="block-overlay"
    text-cat="body"
    center-on-open
    @close="emit('close')"
    @toggle-anchor="pinned = !pinned"
  >
    <template #head-lead>
      <span class="ov-icon"><AppIcon :name="props.icon" :size="13" /></span>
    </template>
    <div class="ov-body md" data-block-overlay-body v-html="mdToHtml(props.markdown)" />
  </FloatingCard>
</template>

<style scoped>
/* 标题行左边那枚图标（与标题同一档大小、方形小底） */
.ov-icon {
  display: inline-flex; align-items: center; justify-content: center;
  width: 20px; height: 20px; border-radius: 6px; flex: none;
  background: var(--accent-soft); color: var(--accent-text);
}
/* 内容：**不再自己定宽高**（壳子是 FloatingCard 的，宽高由拖动/缩放决定） */
.ov-body { min-width: 0; font-size: calc(13px * var(--ts, 1)); line-height: 1.7; }
/* 表体：格线与表头跟着主题走（这里不是正文那套 `.md-doc`，样式要自己写一份） */
.ov-body :deep(table) {
  border-collapse: collapse; margin: 0;
  font-size: calc(12.5px * var(--ts, 1));
}
.ov-body :deep(th), .ov-body :deep(td) {
  border: 1px solid var(--divider); padding: 5px 9px;
  text-align: left; vertical-align: top;
}
.ov-body :deep(th) { background: var(--bg-sunken); color: var(--text); font-weight: 600; }
.ov-body :deep(td) { color: var(--text-dim); }
/* 公式按块居中；宽就横滚（与笔记卡同一口径） */
.ov-body :deep(.katex-display) { margin: 0; }
</style>