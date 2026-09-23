<script setup lang="ts">
/** 划词浮动菜单：解释 / 笔记 / 复制 / 高亮（颜色）/ 关闭。
 *
 * `anchored` = 选区是否有块锚点：
 *   - 正文行（`[data-row-id]`）划词：可以写高亮/笔记（它们靠 `block_id + 字符偏移` 定位）；
 *   - **原 PDF 划词**：只有文字，没有块锚点 → 只留「解释 / 复制」（不写高亮、不存笔记）。
 */
import AppIcon from '@/components/AppIcon.vue';
import { computed, nextTick, ref, watch } from 'vue';

const props = withDefaults(defineProps<{
  visible: boolean;
  x: number;
  y: number;
  colors: string[];
  anchored?: boolean;
}>(), { anchored: true });

const emit = defineEmits<{
  (e: 'explain'): void;
  (e: 'note'): void;
  (e: 'copy'): void;
  (e: 'highlight', color: string): void;
  (e: 'close'): void;
}>();

/**
 * 贴边回拉：不要用"视口宽 - 猜的常量"去夹 `x`。
 * 菜单宽度随内容变（带锚点时还有 5 个色块），猜小了就会把右侧推到屏外；
 * 而且 `left` 一定，容器可用宽度 = 视口 - left，flex 子项默认会**收缩** →
 * 按钮变成竖排的"AI 解\n释"（用户报过）。
 * 做法：先按原位渲染 → 量出真实宽度 → 把溢出量用 `shift` 拉回来。
 */
const el = ref<HTMLElement | null>(null);
const shift = ref(0);
const style = computed(() => ({
  left: `${Math.max(8, props.x - shift.value)}px`,
  top: `${props.y}px`,
}));

async function reclamp() {
  if (!props.visible) { shift.value = 0; return; }
  await nextTick();
  const w = el.value?.offsetWidth ?? 0;
  shift.value = Math.max(0, props.x + w - (window.innerWidth - 10));
}
watch(() => [props.visible, props.x, props.y] as const, reclamp, { immediate: true });

const COLOR_TITLE: Record<string, string> = {
  yellow: '黄色高亮', green: '绿色高亮', blue: '蓝色高亮',
  pink: '粉色高亮', purple: '中性高亮',
};
</script>

<template>
  <div
    v-if="props.visible"
    ref="el"
    class="selection-menu"
    :style="style"
    @mousedown.stop
  >
    <button @click="emit('explain')"><AppIcon name="sparkle" :size="14" />AI 解释</button>
    <button v-if="props.anchored" @click="emit('note')"><AppIcon name="note" :size="14" />笔记</button>
    <button @click="emit('copy')"><AppIcon name="copy" :size="14" />复制</button>
    <span v-if="props.anchored" class="row" style="padding:0 4px;gap:4px">
      <span
        v-for="c in props.colors"
        :key="c"
        :class="['swatch', `hl-${c}`]"
        :title="COLOR_TITLE[c] ?? c"
        @click="emit('highlight', c)"
      />
    </span>
    <button title="关闭" @click="emit('close')"><AppIcon name="close" :size="14" /></button>
  </div>
</template>

<style scoped>
.selection-menu {
  position: fixed; z-index: 120; background: var(--bg-elev); border: 1px solid var(--border-strong);
  border-radius: var(--radius); box-shadow: var(--shadow-2); padding: 4px;
  /* 不换行、不收缩：换行会让按钮变成竖排（“AI 解 / 释”） */
  display: flex; gap: 2px; flex-wrap: nowrap; white-space: nowrap;
  max-width: calc(100vw - 16px);
}
.selection-menu button {
  border: 0; background: transparent; color: var(--text); padding: 5px 9px;
  border-radius: var(--radius-sm); cursor: pointer; font-family: inherit; font-size: 12.5px;
  display: inline-flex; align-items: center; gap: 6px;
  flex: 0 0 auto; white-space: nowrap;
}
.selection-menu button:hover { background: var(--bg-hover); }
.swatch { width: 15px; height: 15px; border-radius: 50%; border: 1px solid var(--border-strong); cursor: pointer; flex: 0 0 auto; }
.hl-yellow { background: rgba(255, 214, 64, .6); }
.hl-green { background: rgba(90, 220, 150, .55); }
.hl-blue { background: rgba(110, 180, 255, .55); }
.hl-pink { background: rgba(255, 150, 190, .55); }
.hl-purple { background: rgba(140, 140, 140, .45); }
</style>
