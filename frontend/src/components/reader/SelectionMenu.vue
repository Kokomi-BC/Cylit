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
  /**
   * 选区/点击处的那条高亮**已经有笔记**了（《修改计划》第四步）：
   * 有的话这一项是「打开笔记」（跳到侧栏那条笔记），否则是「笔记」（新建，挂到这条高亮上）。
   */
  hasNote?: boolean;
}>(), { anchored: true, allowNote: true, hlCount: 0, hasNote: false });

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
    <!-- **只留图标**（用户口径 2026-10-08）：文字标签全撤掉，按钮变成正方形图标键，
         名称走 `title` / `aria-label`（悬停能看到、读屏也读得到）。
         「加入对话」也撤掉了 —— 那件事现在在**右键菜单**的「添加到对话框」里。 -->
    <button title="AI 解释（只依据论文内容）" aria-label="AI 解释" @click="emit('explain')">
      <AppIcon name="sparkle" :size="15" />
    </button>
    <!-- 原 PDF 栏（pdf.js 文字层）没有块锚点 → 那一栏只留「解释 / 复制」。
         「笔记」这一项只挂在**已有高亮**上：用户口径是"只有插入了高亮，才能在高亮的基础上
         加笔记" —— 没有高亮时它根本不出现，引导用户先点一个颜色标出来。 -->
    <button
      v-if="props.anchored && props.allowNote && props.hlCount > 0"
      :title="props.hasNote ? '打开这条笔记' : '给这段文字写笔记（会挂在这处高亮上）'"
      :aria-label="props.hasNote ? '打开笔记' : '添加笔记'"
      @click="emit('note')"
    >
      <AppIcon name="note" :size="15" />
    </button>
    <button title="复制" aria-label="复制" @click="emit('copy')">
      <AppIcon name="copy" :size="15" />
    </button>
    <span v-if="props.anchored" class="row" style="padding:0 5px;gap:6px">
      <span
        v-for="c in props.colors"
        :key="c"
        :class="['hl-swatch', c]"
        :title="`标成${COLOR_TITLE[c] ?? c}`"
        @click="emit('highlight', c)"
      />
    </span>
    <!-- 删除高亮：**禁止符号**（用户口径：删除走菜单，不再点高亮本身） -->
    <button
      v-if="props.hlCount > 0"
      class="danger"
      :title="props.hlCount > 1 ? `删除这处高亮（共 ${props.hlCount} 段）` : '删除高亮'"
      aria-label="删除高亮"
      @click="emit('unhighlight')"
    >
      <AppIcon name="ban" :size="15" />
    </button>
    <button title="关闭" aria-label="关闭" @click="emit('close')">
      <AppIcon name="close" :size="15" />
    </button>
  </div>
</template>

<style scoped>
/* 划词浮层：**与右键菜单同一款毛玻璃**（用户口径 2026-10-08："胶囊也使用同款毛玻璃"）——
   半透明底 + backdrop blur + `--shadow-flyout`；圆角与全站弹出层同规格（`--radius`）。
   里面每一项都是**只带图标的胶囊键**（文字标签已撤），高度对齐 `.btn.sm`（26px）。
   ⚠️ 层级要**高过右键菜单**（`ContextMenu` 是 320）：右键时两个会同时在场上，胶囊在上面。 */
.selection-menu {
  position: fixed; z-index: 340; padding: 3px;
  border: 1px solid color-mix(in srgb, var(--border-strong) 65%, transparent);
  background: color-mix(in srgb, var(--bg-elev) 55%, transparent);
  backdrop-filter: blur(24px) saturate(1.7);
  -webkit-backdrop-filter: blur(24px) saturate(1.7);
  border-radius: var(--radius); box-shadow: var(--shadow-flyout);
  /* 不换行、不收缩：换行会让按钮变成竖排（“AI 解 / 释”） */
  display: flex; align-items: center; gap: 2px; flex-wrap: nowrap; white-space: nowrap;
  max-width: calc(100vw - 16px);
}
.selection-menu button {
  border: 0; background: transparent; color: var(--text-dim);
  height: var(--control-h-sm); width: var(--control-h-sm); padding: 0;
  border-radius: var(--radius-ctl); cursor: pointer; font-family: inherit;
  display: inline-flex; align-items: center; justify-content: center;
  flex: 0 0 auto; white-space: nowrap;
  transition: background var(--dur) var(--ease), color var(--dur) var(--ease);
}
.selection-menu button:hover { background: var(--bg-hover); color: var(--text); }
/* 删除高亮：危险的破坏性动作，用语义色区分（与全站的 danger 一致） */
.selection-menu button.danger { color: var(--danger); }
.selection-menu button.danger:hover { background: var(--danger-soft); color: var(--danger); }
/* 色块一行：与按钮**同高**（26px），圆点垂直居中 —— 否则整条浮层会一高一低。
   色块本身（`.hl-swatch` 与那组颜色）在 `base.css`：AI 解释浮层的"标出这段"用的是同一份。 */
.selection-menu .row { display: inline-flex; align-items: center; height: var(--control-h-sm); }
</style>
