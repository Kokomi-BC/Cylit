<script setup lang="ts">
/** 顶栏胶囊的弹出面板（仿 Apple 的 "Aa" / 视图选项浮层）。
 *
 * ⚠️ **面板必须始终挂载**（用 `v-show` 而不是 `v-if`）：仓库自检脚本
 * （`scripts/frontend_check.py`）是按**按钮文字/标题**去点「原文文本 / 原 PDF / 原 PDF 在左」
 * 这些控件的 —— 一旦用 `v-if` 把它们拆掉，脚本就找不到、那些用例会整片失败。
 * 浮层本身没有交互问题：`display: none` 的元素点不到，但脚本是**合成点击**（`el.click()`），
 * 依然会正常触发处理函数。
 *
 * 样式与胶囊同族：同一层玻璃、同样的圆角语言，出现时轻微下滑淡入。
 */

withDefaults(defineProps<{
  open: boolean;
  /** 无障碍名称（面板不是对话框，只是把一组控件收起来） */
  label: string;
  /** 与触发它的胶囊对齐：默认居中于胶囊下方 */
  align?: 'center' | 'left' | 'right';
}>(), { align: 'center' });
</script>

<template>
  <div
    v-show="open"
    class="cap-panel"
    :class="`is-${align}`"
    role="group"
    :aria-label="label"
  >
    <slot />
  </div>
</template>

<style scoped>
.cap-panel {
  position: absolute; top: 54px; z-index: 41;
  min-width: 292px; max-width: min(560px, calc(100vw - 32px));
  display: grid; gap: 12px; padding: 14px;
  pointer-events: auto;
  /* 与胶囊 / 两枚圆形开关同一套：渐变本体 + 柔光投影，**不画描边、不留内阴影**
     （描边与那道顶部高光在深色下都会变成发光边）。面板里全是文字，本体比工具条那两块再实一档。 */
  border: 0;
  border-radius: 18px;
  background: linear-gradient(180deg,
    color-mix(in srgb, var(--bg-elev) var(--glass-panel-top, 86%), transparent),
    color-mix(in srgb, var(--bg-elev) var(--glass-panel-bottom, 74%), transparent));
  backdrop-filter: blur(52px) saturate(2.6);
  -webkit-backdrop-filter: blur(52px) saturate(2.6);
  box-shadow: var(--shadow-flyout);
  animation: cap-panel-in .18s cubic-bezier(.32, .72, 0, 1) both;
}
/* 面板落在胶囊正下方：`--panel-shift` 由 ReaderView 按侧栏让位量给（与胶囊同一节奏） */
.cap-panel.is-center { left: calc(50% + var(--panel-shift, 0px)); transform: translateX(-50%); }
.cap-panel.is-left { left: 12px; }
.cap-panel.is-right { right: 12px; }

@keyframes cap-panel-in {
  from { opacity: 0; transform: translateX(-50%) translateY(-6px) scale(.985); }
  to { opacity: 1; transform: translateX(-50%) translateY(0) scale(1); }
}
.cap-panel.is-left, .cap-panel.is-right { animation-name: cap-panel-in-side; }
@keyframes cap-panel-in-side {
  from { opacity: 0; transform: translateY(-6px) scale(.985); }
  to { opacity: 1; transform: none; }
}

@media (prefers-reduced-motion: reduce) {
  .cap-panel { animation: none; }
}
</style>
