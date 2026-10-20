<script setup lang="ts">
/**
 * 表格 / 公式的**浮层**（用户口径：侧栏列表里点「浮层打开」，把这一块放大来看）。
 *
 * 为什么单独一个组件而不是复用图片查看器：那边是黑底 + 捏合缩放 + 旋转，为位图写的；
 * 表格/公式是**文本**，要的是"按内容自适应、放不下就在里面滚"，共用一套只会两边都别扭。
 * 两边的共同语言是**左上角那枚「图标 + 标题」**（用户口径）：
 * 标题有就用（表格说明），没有就写类别（表格 / 公式）。
 *
 * 关法三种：右上角 ×、点遮罩、Esc —— 与站内其他弹层一致。
 */
import { onBeforeUnmount, onMounted } from 'vue';
import AppIcon from '@/components/AppIcon.vue';
import { mdToHtml } from '@/composables/useMarkdown';

const props = defineProps<{
  icon: string;
  title: string;
  /** 内容本体：GFM Markdown（表格是 `| … |`，公式是 `$$…$$`） */
  markdown: string;
}>();
const emit = defineEmits<{ (e: 'close'): void }>();

function onKey(ev: KeyboardEvent) {
  if (ev.key === 'Escape') { ev.stopPropagation(); emit('close'); }
}
/* 捕获阶段：阅读器自己也监听 Esc（收浮层/退出全屏），浮层开着时这一下先归这里 */
onMounted(() => window.addEventListener('keydown', onKey, true));
onBeforeUnmount(() => window.removeEventListener('keydown', onKey, true));
</script>

<template>
  <Teleport to="body">
    <div class="ov" data-block-overlay @click.self="emit('close')">
      <div class="ov-card" role="dialog" :aria-label="props.title">
        <div class="ov-head">
          <span class="ov-icon"><AppIcon :name="props.icon" :size="15" /></span>
          <span class="ov-title">{{ props.title }}</span>
          <span class="spacer" />
          <button class="ov-close" title="关闭" data-block-overlay-close @click="emit('close')">
            <AppIcon name="close" :size="15" />
          </button>
        </div>
        <div class="ov-body md" data-block-overlay-body v-html="mdToHtml(props.markdown)" />
      </div>
    </div>
  </Teleport>
</template>

<style scoped>
.ov {
  position: fixed; inset: 0; z-index: 230;
  display: flex; align-items: center; justify-content: center;
  padding: 24px;
  background: var(--smoke, rgba(15, 18, 20, .45));
  backdrop-filter: blur(2px);
  -webkit-backdrop-filter: blur(2px);
}
/* 卡片：宽按内容自适应、上限 92vw；高最多 86vh，超出在 .ov-body 里滚（不把整页顶开） */
.ov-card {
  display: flex; flex-direction: column; min-width: 0;
  width: max-content; max-width: min(960px, 92vw);
  max-height: 86vh;
  border: 1px solid var(--border); border-radius: 16px;
  background: var(--bg-elev); box-shadow: var(--shadow-flyout);
  overflow: hidden;
}
/* 头部一条：**左上角就是图标 + 标题**（用户口径），右侧一枚关闭 */
.ov-head {
  display: flex; align-items: center; gap: 8px; flex: none;
  padding: 10px 12px; border-bottom: 1px solid var(--divider);
  background: var(--bg-elev);
}
.ov-icon {
  display: inline-flex; align-items: center; justify-content: center;
  width: 24px; height: 24px; border-radius: 8px; flex: none;
  background: var(--accent-soft); color: var(--accent-text);
}
.ov-title {
  font-size: calc(12.5px * var(--ts, 1)); font-weight: 600; color: var(--text);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.ov-close {
  width: 26px; height: 26px; flex: none; border: 0; border-radius: 999px;
  display: inline-flex; align-items: center; justify-content: center;
  background: transparent; color: var(--text-dim); cursor: pointer;
  transition: background var(--dur) var(--ease), color var(--dur) var(--ease);
}
.ov-close:hover { background: var(--bg-hover); color: var(--text); }
.ov-body {
  flex: 1 1 auto; min-height: 0; min-width: 0;
  overflow: auto; overscroll-behavior: contain;
  padding: 12px 14px 16px;
  font-size: calc(13px * var(--ts, 1)); line-height: 1.7;
}
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