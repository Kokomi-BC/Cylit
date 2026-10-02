<script setup lang="ts">
/** 划词浮动菜单：解释 / 笔记 / 复制 / 高亮（颜色）/ 关闭。
 *
 * `anchored` = 选区是否有块锚点：
 *   - 正文行（`[data-row-id]`）划词：可以写高亮/笔记（它们靠 `block_id + 字符偏移` 定位）；
 *   - **原 PDF 划词**：只有文字，没有块锚点 → 只留「解释 / 复制」（不写高亮、不存笔记）。
 *
 * ⚠️ 容器上必须是 `@mousedown.prevent.stop`：
 *   **点菜单的默认动作会把页面上的选区清掉**（浏览器行为）。以前只写了 `.stop`，于是真实鼠标
 *   点色块时 —— mousedown 清选区 → mouseup 时 `onMouseUp` 看到「选区已折叠」就 `show = false`
 *   → 菜单在自己的 click 触发前就被卸载 → **点高亮毫无反应**（用户报的正是这个；
 *   `.click()` 那种合成事件不会触发默认动作，所以自检探针反而测不出来）。
 *   `preventDefault` 保住选区，点完菜单还能看到自己选了哪一段。
 */
import AppIcon from '@/components/AppIcon.vue';
import { computed, nextTick, ref, watch } from 'vue';

const props = withDefaults(defineProps<{
  visible: boolean;
  x: number;
  y: number;
  colors: string[];
  anchored?: boolean;
  /**
   * 是否给「笔记」这一项。
   * 笔记靠 `block_id` 定位回正文，而 **Markdown 视图**里的锚点是 `md:p<页号>`（正文里没有这个块），
   * 存下来会变成一条"点了定位却跳不过去"的死笔记 —— 所以那边只给高亮/解释/复制。
   */
  allowNote?: boolean;
  /** 选区/点击处盖住了几处已有高亮：>0 才显示「禁止」图标（删除对应高亮） */
  hlCount?: number;
}>(), { anchored: true, allowNote: true, hlCount: 0 });

const emit = defineEmits<{
  (e: 'explain'): void;
  (e: 'note'): void;
  (e: 'copy'): void;
  (e: 'highlight', color: string): void;
  /** 删除选区/点击处对应的高亮（跨行高亮整组一起删） */
  (e: 'unhighlight'): void;
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
    data-selection-menu
    @mousedown.prevent.stop
  >
    <button @click="emit('explain')"><AppIcon name="sparkle" :size="14" />AI 解释</button>
    <!-- 原 PDF 栏（pdf.js 文字层）没有块锚点 → 那一栏只留「解释 / 复制」。
         不写任何说明文字（用户口径：完全移除「原 PDF 栏不支持高亮」那行）。 -->
    <button v-if="props.anchored && props.allowNote" @click="emit('note')">
      <AppIcon name="note" :size="14" />笔记
    </button>
    <button @click="emit('copy')"><AppIcon name="copy" :size="14" />复制</button>
    <span v-if="props.anchored" class="row" style="padding:0 6px;gap:6px">
      <span
        v-for="c in props.colors"
        :key="c"
        :class="['swatch', `hl-${c}`]"
        :title="COLOR_TITLE[c] ?? c"
        @click="emit('highlight', c)"
      />
    </span>
    <!-- 删除高亮：**禁止符号**（用户口径：删除走菜单，不再点高亮本身） -->
    <button
      v-if="props.hlCount > 0"
      class="danger"
      :title="props.hlCount > 1 ? `删除这处高亮（共 ${props.hlCount} 段）` : '删除高亮'"
      @click="emit('unhighlight')"
    >
      <AppIcon name="ban" :size="14" />删除高亮
    </button>
    <button title="关闭" @click="emit('close')"><AppIcon name="close" :size="14" /></button>
  </div>
</template>

<style scoped>
/* 划词浮层：一个白色浮层（--bg-elev + --border-strong + --radius，与全站的弹出层同规格），
   里面每一项都是**胶囊尺寸的小按钮** —— 高度对齐 `.btn.sm`（26px）、圆到底，
   与旁边那些分段控件同一门语言（用户口径 2026-10-03："以论文译文对照那枚胶囊为准"）。 */
.selection-menu {
  position: fixed; z-index: 120; background: var(--bg-elev); border: 1px solid var(--border-strong);
  border-radius: var(--radius); box-shadow: var(--shadow-2); padding: 3px;
  /* 不换行、不收缩：换行会让按钮变成竖排（“AI 解 / 释”） */
  display: flex; align-items: center; gap: 2px; flex-wrap: nowrap; white-space: nowrap;
  max-width: calc(100vw - 16px);
}
.selection-menu button {
  border: 0; background: transparent; color: var(--text-dim);
  height: var(--control-h-sm); padding: 0 11px;
  border-radius: var(--radius-ctl); cursor: pointer; font-family: inherit; font-size: 12px;
  display: inline-flex; align-items: center; gap: 5px;
  flex: 0 0 auto; white-space: nowrap;
  transition: background var(--dur) var(--ease), color var(--dur) var(--ease);
}
.selection-menu button:hover { background: var(--bg-hover); color: var(--text); }
/* 删除高亮：危险的破坏性动作，用语义色区分（与全站的 danger 一致） */
.selection-menu button.danger { color: var(--danger); }
.selection-menu button.danger:hover { background: var(--danger-soft); color: var(--danger); }
/* 色块一行：与按钮**同高**（26px），圆点垂直居中 —— 否则整条浮层会一高一低 */
.selection-menu .row { display: inline-flex; align-items: center; height: var(--control-h-sm); }
.swatch {
  width: 16px; height: 16px; border-radius: 50%;
  border: 1px solid var(--border-strong); cursor: pointer; flex: 0 0 auto;
  transition: transform var(--dur-fast) var(--ease), border-color var(--dur) var(--ease);
}
.swatch:hover { transform: scale(1.15); border-color: var(--text-dim); }
.hl-yellow { background: rgba(255, 214, 64, .6); }
.hl-green { background: rgba(90, 220, 150, .55); }
.hl-blue { background: rgba(110, 180, 255, .55); }
.hl-pink { background: rgba(255, 150, 190, .55); }
.hl-purple { background: rgba(140, 140, 140, .45); }
</style>
