<script setup lang="ts">
/**
 * 轻量浮层（阅读器的**笔记**与 **AI 解释**共用，《修改计划》第五步）。
 *
 * 为什么要它：东侧原来那块编辑/解释用的是**居中大弹窗**（`Modal`）—— 它把整页盖住，
 * 用户看不见自己标的到底是哪一段，改完还得回想"我刚写的对应哪句话"。
 * 浮层要的是：**锚在高亮附近**（视线不用离开原文）、不遮挡大面积正文、观感与侧栏一致。
 *
 * 三条行为约定（用户口径）：
 * - **尺寸小**：默认 340px 宽、圆角 12px、`--shadow-flyout`，与侧栏卡片同一门语言；
 * - **贴边回拉**：先按锚点右下放，越出视口就翻到上方/左侧（`place()`），永远不会跑到屏幕外；
 * - **锚定**：`anchored=false`（默认）时由**父组件**在鼠标移开时关掉它（本组件只管画 + 提供按钮）；
 *   点那枚图钉切锚定，之后移开也不消失。
 *
 * 第四条（用户口径 2026-10-07）：**可以按住标题栏拖**。拖过之后就不再做"贴边回拉"了 ——
 * 用户把它挪到哪儿就是哪儿，内容变长/锚点变化都不许把它弹回去（`dragged`），
 * 直到下次打开才回到自动定位。
 * 标题栏上**不写 `title`**（用户口径 2026-10-08："移除浏览器悬浮 toast 内容"）：
 * "按住这里可以拖动浮层"这种说明交给光标形状（`cursor: grab`）就够，不必再弹一条浏览器提示。
 *
 * 第五条（用户口径 2026-10-13）：标题栏**之上**还有一条 14px 的独立拖动条（data-float-grab，
 * 中间一枚 36×4 的药丸），底部两个角换成**圆角小把手**（类 iOS）且卡片一打开就渲染 ——
 * 两者对笔记与解释浮层同时生效。
 *
 * 本组件**不持有业务状态**：谁开、开在哪、里面放什么，全由父组件给。
 */
import { computed, nextTick, onBeforeUnmount, reactive, ref, watch } from 'vue';
import AppIcon from '@/components/AppIcon.vue';

const props = withDefaults(defineProps<{
  open: boolean;
  /** 锚点（一般给高亮/选区的 `getBoundingClientRect()`）：浮层贴在它右下 */
  anchor?: { x: number; y: number; w?: number; h?: number } | null;
  title?: string;
  /** 锚定模式：移开不自动消失（笔记默认关、AI 解释默认开） */
  anchored?: boolean;
  width?: number;
  /** 浮层本体可滚的最大高度（内容多时内部滚，不把页面顶长） */
  maxHeight?: number;
}>(), { anchor: null, title: '', anchored: false, width: 340, maxHeight: 420 });

const emit = defineEmits<{
  (e: 'close'): void;
  (e: 'toggle-anchor'): void;
}>();

const el = ref<HTMLElement | null>(null);
const pos = ref({ left: 0, top: 0 });

/** 贴边回拉：优先放锚点右下；右边越界就往左挪，下边越界就翻到锚点上方。
 *  **用户拖过之后就不再自动摆** —— 见 `dragged`。 */
async function place() {
  if (!props.open || dragged.value) return;
  await nextTick();
  const a = props.anchor;
  const box = el.value;
  if (!a || !box) return;
  const w = box.offsetWidth || props.width;
  const h = box.offsetHeight || 120;
  const vw = window.innerWidth;
  const vh = window.innerHeight;
  let left = a.x - 8;
  if (left + w > vw - 10) left = Math.max(10, vw - w - 10);
  if (left < 10) left = 10;
  let top = a.y + (a.h ?? 0) + 8;
  if (top + h > vh - 12) top = Math.max(10, a.y - h - 8);   // 放不下就翻到上方
  pos.value = { left: Math.round(left), top: Math.round(top) };
}

/* ------------------------------ 拖动（按住标题栏） ------------------------------ */
/** 用户挪过它了：之后不再自动摆位（内容变长、锚点变化都不弹回去），下次打开才复位。 */
const dragged = ref(false);
const drag = reactive({ on: false, sx: 0, sy: 0, ox: 0, oy: 0 });

function onHeadDown(ev: PointerEvent) {
  // 标题栏上的控件（图钉 / 关闭 / 色点 / 取色器）不参与拖动，否则点它们会顺手把卡片挪一下
  if ((ev.target as HTMLElement | null)?.closest?.('button, label, input')) return;
  const head = ev.currentTarget as HTMLElement | null;
  drag.on = true;
  drag.sx = ev.clientX; drag.sy = ev.clientY;
  drag.ox = pos.value.left; drag.oy = pos.value.top;
  /* 合成事件（自检探针）没有真指针，捕获会抛 NotFoundError —— 拖不动的是"捕获"，
     位移本身不依赖它，所以这里吞掉就行。 */
  try { head?.setPointerCapture?.(ev.pointerId); } catch { /* 没有真指针 */ }
  ev.preventDefault();
}

function onHeadMove(ev: PointerEvent) {
  if (!drag.on) return;
  const box = el.value;
  const w = box?.offsetWidth ?? props.width;
  const h = box?.offsetHeight ?? 120;
  // 夹在视口里：至少留一条边能抓得住（不然拖出去就再也点不到关闭了）
  const left = Math.min(Math.max(drag.ox + (ev.clientX - drag.sx), 8 - w + 48), window.innerWidth - 48);
  const top = Math.min(Math.max(drag.oy + (ev.clientY - drag.sy), 4), window.innerHeight - 32);
  pos.value = { left: Math.round(left), top: Math.round(top) };
  dragged.value = true;
}

function onHeadUp(ev: PointerEvent) {
  if (!drag.on) return;
  drag.on = false;
  try { (ev.currentTarget as HTMLElement | null)?.releasePointerCapture?.(ev.pointerId); } catch { /* 同上 */ }
}

watch(() => [props.open, props.anchor?.x, props.anchor?.y, props.anchor?.h] as const,
  () => { void place(); }, { immediate: true });
watch(() => props.open, (v) => {
  if (!v) return;
  dragged.value = false;          // 每次打开都回到"自动贴锚点"，上次拖的位置不跨次沿用
  sizeW.value = null;             // 尺寸同理：每次打开回到调用方给的默认宽高
  sizeH.value = null;
  void place();
});

/** 内容尺寸变了（切换查看/编辑、流式回答变长）也要重新夹一次 */
let ro: ResizeObserver | null = null;
watch(el, (node) => {
  ro?.disconnect();
  if (node && typeof ResizeObserver !== 'undefined') {
    ro = new ResizeObserver(() => { void place(); });
    ro.observe(node);
  }
});
onBeforeUnmount(() => ro?.disconnect());

/* -------------------------------- 右下角拖动放大 -------------------------------- */
/** 用户拉过大小后就不再每次打开都用默认尺寸（下次打开复位）。
 *  手柄**卡片一打开就给**（用户口径 2026-10-13 改了 2026-10-10 那条）：笔记卡默认不锚定，
 *  只在锚定态给手柄 = 默认看不见把手。拉一下大小本身就置 dragged（见 onResizeDown），
 *  所以不锚定也不会被 place() 弹回去。 */
const sizeW = ref<number | null>(null);
const sizeH = ref<number | null>(null);
const rs = reactive({ on: false, corner: 'br' as 'br' | 'bl', sx: 0, sy: 0, sw: 0, sh: 0, sl: 0 });

const clamp = (v: number, lo: number, hi: number) => Math.min(Math.max(v, lo), hi);

function onResizeDown(ev: PointerEvent, corner: 'br' | 'bl') {
  const box = el.value;
  if (!box) return;
  rs.on = true;
  rs.corner = corner;
  rs.sx = ev.clientX; rs.sy = ev.clientY;
  rs.sw = box.offsetWidth; rs.sh = box.offsetHeight;
  rs.sl = pos.value.left;
  /* 拉了大小就算"用户自己摆过了"：否则 `ResizeObserver` 一量到尺寸变了就调 `place()`，
     把我们刚拉出来的宽度又夹回去（拉大时手感和打架一样）。 */
  dragged.value = true;
  // 合成事件（自检探针）没有真指针，捕获会抛 NotFoundError —— 与标题栏拖动同一处理
  try { (ev.currentTarget as HTMLElement | null)?.setPointerCapture?.(ev.pointerId); } catch { /* 没有真指针 */ }
  ev.preventDefault();
  ev.stopPropagation();
}

function onResizeMove(ev: PointerEvent) {
  if (!rs.on) return;
  const MIN_W = 260;
  const MIN_H = 160;
  const dx = ev.clientX - rs.sx;
  const dy = ev.clientY - rs.sy;
  const maxH = Math.max(MIN_H, window.innerHeight - pos.value.top - 10);
  if (rs.corner === 'br') {
    const maxW = Math.max(MIN_W, window.innerWidth - pos.value.left - 10);
    sizeW.value = Math.round(clamp(rs.sw + dx, MIN_W, maxW));
  } else {
    /* 左下角：**右边界钉住不动**（人拉的是左边那条边）—— 宽度反向变化，`left` 跟着推。
       否则拉左角时卡片会整体往右跑，手感完全不对。 */
    const right = rs.sl + rs.sw;
    const w = Math.round(clamp(rs.sw - dx, MIN_W, Math.max(MIN_W, right - 8)));
    sizeW.value = w;
    pos.value = { ...pos.value, left: Math.round(right - w) };
  }
  sizeH.value = Math.round(clamp(rs.sh + dy, MIN_H, maxH));
}

function onResizeUp(ev: PointerEvent) {
  if (!rs.on) return;
  rs.on = false;
  try { (ev.currentTarget as HTMLElement | null)?.releasePointerCapture?.(ev.pointerId); } catch { /* 同上 */ }
}

const style = computed(() => ({
  left: `${pos.value.left}px`,
  top: `${pos.value.top}px`,
  width: `${sizeW.value ?? props.width}px`,
  /* 拉过高度就固定高度（像窗口那样），没拉过就只给上限、高度随内容 */
  height: sizeH.value === null ? undefined : `${sizeH.value}px`,
  maxHeight: `${sizeH.value ?? props.maxHeight}px`,
}));
</script>

<template>
  <div
    v-if="props.open"
    ref="el"
    class="float-card"
    :class="{ sized: sizeH !== null }"
    :style="style"
    data-float-card
  >
    <!-- 顶部**浮起**的拖动条（用户口径 2026-10-13 二轮："自然融合，不要另起一行"）：
         position:absolute 贴在卡片顶端，中间一枚 34×4 的药丸，不占一行、不带底色。
         为什么要抓手：头一行挤着色点 / 铅笔 / 垃圾桶，真正"空白可抓"的地方太少。
         它和 .fc-head 走**同一套**处理器（叠加，不是替代）—— 标题栏照旧能拖。
         同样**不写 title**（用户口径 2026-10-08：不要浏览器悬浮提示），只靠光标形状说话。 -->
    <div
      class="fc-grab"
      :class="{ dragging: drag.on }"
      data-float-grab
      @pointerdown="onHeadDown"
      @pointermove="onHeadMove"
      @pointerup="onHeadUp"
      @pointercancel="onHeadUp"
    >
      <span class="fc-grab-pill" />
    </div>

    <!-- ⚠️ 标题栏里可能有**可点的东西**（笔记框的色点 / 取色器）：
         `label` / `input` 也要排掉，否则点取色器会顺手把卡片拖一下。 -->
    <div
      class="fc-head"
      :class="{ dragging: drag.on }"
      data-float-head
      @pointerdown="onHeadDown"
      @pointermove="onHeadMove"
      @pointerup="onHeadUp"
      @pointercancel="onHeadUp"
    >
      <!-- 标题**前面**的图标位（2026-10-10：笔记框要照目标站放一枚笔记图标）。
           不传则为空，解释浮层不受影响。 -->
      <slot name="head-lead" />
      <span class="fc-title">{{ props.title }}</span>
      <!-- 标题**之后**、按钮之前的位置（2026-10-10 复查：笔记框的色条要跟「笔记」并排，
           但「笔记」和左边那枚图标必须在**最左侧** —— 所以色点排在标题右边，而不是它左边）。 -->
      <slot name="head-mid" />
      <span class="spacer" />
      <!-- 头部附加内容（2026-10-10：笔记框要照目标站把「✓ 保存」放在标题行上）。
           没传这个插槽时渲染为空，**解释浮层那边完全不受影响**。 -->
      <slot name="head-extra" />
      <button
        type="button"
        class="fc-btn"
        :class="{ on: props.anchored }"
        :title="props.anchored ? '已锚定：鼠标移开也不关' : '锚定（鼠标移开也不关）'"
        data-float-anchor
        @click="emit('toggle-anchor')"
      >
        <AppIcon name="locate" :size="13" />
      </button>
      <!-- 头部**最右**（锚定之后、✕ 之前）：笔记框的删除键放这里（用户口径 2026-11-20）——
           删除与「✓ 保存 / 锚定」之间隔开一小段，免得点删除时误触保存。
           ⚠️ 只有父组件传了才渲染：解释浮层不传，头部与以前完全一致。 -->
      <span v-if="$slots['head-tail']" class="fc-tail"><slot name="head-tail" /></span>
      <button type="button" class="fc-btn" title="关闭" data-float-close @click="emit('close')">
        <AppIcon name="close" :size="13" />
      </button>
    </div>

    <div class="fc-body">
      <slot />
    </div>

    <div v-if="$slots.footer" class="fc-foot">
      <slot name="footer" />
    </div>

    <!-- 底部**两个角**都能拉大小（用户口径 2026-10-10）。
         用户口径 2026-10-13：改成**圆角小把手**（类 iOS），而且**卡片一打开就渲染** ——
         笔记卡默认不锚定，原来"只在锚定态给手柄"等于默认看不见，用户根本不知道能拉。
         锚定逻辑本身没动（anchored 只管"移开鼠标关不关"），dragged / sizeW / sizeH 也不动。 -->
    <div
      class="fc-resize br"
      :class="{ 'is-on': rs.on && rs.corner === 'br' }"
      data-float-resize
      aria-label="拖动调整大小"
      @pointerdown="onResizeDown($event, 'br')"
      @pointermove="onResizeMove"
      @pointerup="onResizeUp"
      @pointercancel="onResizeUp"
    />
    <div
      class="fc-resize bl"
      :class="{ 'is-on': rs.on && rs.corner === 'bl' }"
      data-float-resize-bl
      aria-label="拖动调整大小"
      @pointerdown="onResizeDown($event, 'bl')"
      @pointermove="onResizeMove"
      @pointerup="onResizeUp"
      @pointercancel="onResizeUp"
    />
  </div>
</template>

<style scoped>
/* 观感与侧栏卡片同一门语言：同一个圆角档、同一门阴影、同一个边框色。
   阴影用 `--shadow-flyout`（比侧栏的 `--shadow-2` 重一档）—— 它是浮在正文上的，要能一眼看出"这是浮层"。
   ⚠️ 这里原来写的是 `--shadow-3`，而那个名字**从来没定义过**：声明被整条丢掉，等于一直没有阴影。 */
.float-card {
  /* `position: fixed` 同时给右下角那枚拉手当了定位上下文（拉手是卡片内部的 absolute） */
  position: fixed; z-index: 60;
  display: flex; flex-direction: column;
  border: 1px solid var(--border-strong); border-radius: 12px;
  background: var(--bg-elev); box-shadow: var(--shadow-flyout);
  overflow: hidden;
}
/* 顶部拖动条（用户口径第二轮："最好能自然融合，而不是另起一行"）：
   **浮**在标题带顶端（absolute），不占一行高度、没有自己的底色 —— 药丸看起来就是标题带
   上沿的一枚抓手，而不是"多出来的一条"。标题带给的 padding-top 让按钮从 12px 才开始，
   所以这 12px 抓手区不会盖住标题栏上的按钮（拖动落空 = 拖标题带，行为不变）。
   光标 / 不可选中 / touch-action 与标题栏完全一致（同一套拖动处理器）。 */
.fc-grab {
  position: absolute; top: 0; left: 0; right: 0;
  display: flex; align-items: flex-start; justify-content: center;
  height: 12px; padding-top: 4px; z-index: 2;
  cursor: grab; user-select: none; touch-action: none;
}
.fc-grab.dragging { cursor: grabbing; }
.fc-grab-pill {
  width: 34px; height: 4px; border-radius: 2px;
  background: color-mix(in srgb, var(--text) 19%, transparent);
}

.fc-head {
  display: flex; align-items: center; gap: 4px; flex: none;
  /* 上 12px 留给浮在顶端的抓手药丸（见 .fc-grab）：按钮从 12px 才开始，不会被抓手盖住 */
  padding: 12px 8px 6px 12px;
  border-bottom: 1px solid var(--divider);
  background: color-mix(in srgb, var(--text) 3%, transparent);
  /* 标题栏就是"把手"：光标与不可选中都按拖动来给，免得拖的时候把标题拖蓝 */
  cursor: grab; user-select: none; touch-action: none;
}
.fc-head.dragging { cursor: grabbing; }
.fc-title { font-size: 11.5px; font-weight: 600; color: var(--text-dim); }
.fc-btn {
  display: inline-flex; align-items: center; justify-content: center;
  width: 24px; height: 24px; flex: none; border: 0; border-radius: 7px;
  background: transparent; color: var(--text-faint); cursor: pointer;
  transition: background var(--dur) var(--ease), color var(--dur) var(--ease);
}
.fc-btn:hover { background: var(--bg-hover); color: var(--text); }
.fc-btn.on { background: color-mix(in srgb, var(--accent) 16%, transparent); color: var(--accent); }
/* 头部最右那格（head-tail）：与锚定之间再多一小段间距 —— 删除键和「保存 / 锚定」不能挨着 */
.fc-tail { display: inline-flex; align-items: center; gap: 2px; flex: none; margin-left: 4px; }

.fc-body { flex: 1 1 auto; padding: 10px 12px; overflow: auto; min-height: 0; font-size: 12.5px; }
/* 用户把卡片拉大之后（`sizeH` 有值 → `.sized`）正文区改成**纵向 flex**：
   这样里面那层"本该吃满"的内容（笔记卡的可编辑区）可以用 `flex: 1 1 auto` 长高，
   而不是拉大之后下面空出一大块点不动的白。没拉过尺寸时保持原样（高度随内容），
   免得默认尺寸的卡片被撑出一条空白。 */
.float-card.sized .fc-body { display: flex; flex-direction: column; }
/* 底部两个角的拖动把手（用户口径 2026-10-13）：16×16 的热区，里面用一条**2px 圆弧**画出来 ——
   11×11 的小方块只描右边/下边、再给一个 1/4 圆角，剩下的就是个像 iOS 那样的圆角把手。
   静止时淡（opacity .35 + --border-strong），悬停或正在拖时升到 --accent 且不透明。
   ⚠️ 颜色用 --border-strong 而不是 --line：token 表里**没有 --line**，
   写了等于整条声明被丢掉（本文件开头那条 --shadow-3 就是这么消失的）。 */
.fc-resize {
  position: absolute; bottom: 0; width: 16px; height: 16px;
  display: flex; align-items: flex-end;
  touch-action: none; z-index: 2;
  opacity: .35;
  transition: opacity var(--dur) var(--ease);
}
.fc-resize::before {
  content: ''; display: block; width: 11px; height: 11px;
  border-right: 2px solid var(--border-strong);
  border-bottom: 2px solid var(--border-strong);
  border-bottom-right-radius: 10px;
  transition: border-color var(--dur) var(--ease);
}
.fc-resize.br { right: 0; justify-content: flex-end; cursor: nwse-resize; }
.fc-resize.br::before { margin: 0 2px 2px 0; }
/* 左下角是**镜像**：描边换到左边、圆角换到左下，否则两个角会看起来一模一样（像一个直角） */
.fc-resize.bl { left: 0; justify-content: flex-start; cursor: nesw-resize; }
.fc-resize.bl::before {
  border-right: 0; border-left: 2px solid var(--border-strong);
  border-bottom-right-radius: 0; border-bottom-left-radius: 10px;
  margin: 0 0 2px 2px;
}
.fc-resize:hover, .fc-resize.is-on { opacity: 1; }
.fc-resize:hover::before, .fc-resize.is-on::before { border-color: var(--accent); }
.fc-foot {
  display: flex; align-items: center; gap: 6px; flex: none;
  padding: 8px 12px; border-top: 1px solid var(--divider);
  background: var(--bg-layer);
}
</style>
