<script setup lang="ts">
/**
 * 表格 / 公式的**浮层**（用户口径：侧栏列表里点「浮层打开」，把这一块放大来看）。
 *
 * 第一版是一张**居中卡片 + 全屏遮罩**，用户口径否掉了：要的是「像笔记浮层那样」
 * —— 可拖动、可缩放、不带遮罩。所以这里不再自己画壳子，直接复用 `FloatingCard`
 * （笔记 / AI 解释浮层用的同一套外壳）：拖动条、四角缩放、位置/尺寸记忆、字号档全都有。
 *
 * 本组件只负责两件事：① 标题行左边那枚图标；② 浮层里的内容本身（Markdown 渲染）。
 * 关法：右上角 ×、Esc（与站内其他浮层一致）。
 */
import { onBeforeUnmount, onMounted } from 'vue';
import AppIcon from '@/components/AppIcon.vue';
import FloatingCard from './FloatingCard.vue';
import { mdToHtml } from '@/composables/useMarkdown';
import type { OverlayAnchor } from '@/api/types';

const props = defineProps<{
  icon: string;
  title: string;
  /** 标题的 Markdown 版（图注里有公式）；不给就只显示 `title` 那份纯文字 */
  titleMd?: string;
  /** 触发它的那枚按钮的矩形：浮层**贴着按钮下方打开**（用户口径：不要跑到左上角） */
  anchor?: OverlayAnchor | null;
  /** 这张现在是**最上层**那张（FloatingCard 用它决定底角指示器亮不亮） */
  focused?: boolean;
  /** 内容本体：GFM Markdown（表格是 `| … |`，公式是 `$$…$$`） */
  markdown?: string;
  /** 插图走这一条：**直接显示这张图**（用户口径：图片的「浮层打开」不是那个带缩放的黑底查看器） */
  imageUrl?: string;
}>();
const emit = defineEmits<{
  (e: 'close'): void;
  /** 被点了一下：父组件据此把这张抬到最上层（多张浮层时的层级） */
  (e: 'activate'): void;
  /** 图片浮层里点了那张图：交给图片查看器去放大（缩放 / 拖动 / 旋转都在那边） */
  (e: 'zoom-image'): void;
}>();

/* ⚠️ 没有「锚定」图钉（`hide-anchor`）：笔记浮层也是这么做的（那边交给"点过浮层就自动锚定"）。
   留着它有两个坏处：① 这张浮层本来就不会因为鼠标移开而消失，图钉是枚死控件；
   ② 它默认亮的 `on` 态让它比旁边 ⋯ / ✕ 深一档，头部三枚钮颜色对不上（用户口径）。 */

function onKey(ev: KeyboardEvent) {
  if (ev.key !== 'Escape') return;
  /* 同时开着好几张时**只关最上面那张**（其余几张也各挂了一个监听，不加这道判断会一起关掉） */
  if (props.focused === false) return;
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
    data-block-overlay
    :title="props.title"
    :title-md="props.titleMd || ''"
    :anchor="props.anchor || null"
    :focused="props.focused !== false"
    zoomable
    hide-anchor
    :width="480"
    :max-height="520"
    text-cat="body"
    @close="emit('close')"
    @pointerdown.capture="emit('activate')"
  >
    <template #head-lead>
      <span class="ov-icon"><AppIcon :name="props.icon" :size="13" /></span>
    </template>
    <!-- 图片：白底一块，按卡片宽度等比缩放（要缩放/旋转/切图请点图片本身开查看器） -->
    <div v-if="props.imageUrl" class="ov-image" data-block-overlay-image>
      <!-- 点图 = 交给图片查看器放大（用户口径：图片浮层要能放大；缩放/拖动/旋转在那边） -->
      <img
        :src="props.imageUrl"
        :alt="props.title"
        title="点击放大"
        data-block-overlay-zoom
        @click="emit('zoom-image')"
      >
    </div>
    <div v-else class="ov-body md" data-block-overlay-body v-html="mdToHtml(props.markdown || '')" />
  </FloatingCard>
</template>

<style scoped>
/* 头部**不要那层 3% 灰底**（用户口径：浮层顶部颜色偏深、跟笔记浮层不一致）。
   那层底在 `FloatingCard` 的 `.fc-head` 上是给「解释卡」那类内容的；笔记浮层当年就把它撤掉、
   换成一条发丝线（见 ReaderView 的 `[data-note-popover] .fc-head`），这里跟它对齐。 */
[data-block-overlay] :deep(.fc-head) { background: transparent; }

/* 标题行左边那枚图标（与标题同一档大小、方形小底） */
.ov-icon {
  display: inline-flex; align-items: center; justify-content: center;
  width: 20px; height: 20px; border-radius: 6px; flex: none;
  background: var(--accent-soft); color: var(--accent-text);
}
/* 图片：白底 + 圆角，宽度跟随卡片（卡片可拖动 / 缩放，图等比跟着变） */
.ov-image { line-height: 0; }
.ov-image img {
  cursor: zoom-in;
  display: block; width: 100%; height: auto;
  background: #fff; border-radius: var(--radius-sm);
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