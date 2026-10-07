<script setup lang="ts">
/**
 * 轻量浮层（阅读器的**笔记**与 **AI 解释**共用，《修改计划》第五步）。
 *
 * 为什么要它：东侧原来那块编辑/解释用的是**居中大弹窗**（`Modal`）—— 它把整页盖住，
 * 用户看不见自己标的到底是哪一段，改完还得回想"我刚写的对应哪句话"。
 * 浮层要的是：**锚在高亮附近**（视线不用离开原文）、不遮挡大面积正文、观感与侧栏一致。
 *
 * 三条行为约定（用户口径）：
 * - **尺寸小**：默认 340px 宽、圆角 12px、`--shadow-3`，与侧栏卡片同一门语言；
 * - **贴边回拉**：先按锚点右下放，越出视口就翻到上方/左侧（`place()`），永远不会跑到屏幕外；
 * - **锚定**：`anchored=false`（默认）时由**父组件**在鼠标移开时关掉它（本组件只管画 + 提供按钮）；
 *   点那枚图钉切锚定，之后移开也不消失。
 *
 * 第四条（用户口径 2026-10-07）：**可以按住标题栏拖**。拖过之后就不再做"贴边回拉"了 ——
 * 用户把它挪到哪儿就是哪儿，内容变长/锚点变化都不许把它弹回去（`dragged`），
 * 直到下次打开才回到自动定位。
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
  // 标题栏上的按钮（图钉 / 关闭）不参与拖动，否则点它们会顺手把卡片挪一下
  if ((ev.target as HTMLElement | null)?.closest?.('button')) return;
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

const style = computed(() => ({
  left: `${pos.value.left}px`,
  top: `${pos.value.top}px`,
  width: `${props.width}px`,
  maxHeight: `${props.maxHeight}px`,
}));
</script>

<template>
  <div
    v-if="props.open"
    ref="el"
    class="float-card"
    :style="style"
    data-float-card
  >
    <div
      class="fc-head"
      :class="{ dragging: drag.on }"
      data-float-head
      title="按住这里可以拖动浮层"
      @pointerdown="onHeadDown"
      @pointermove="onHeadMove"
      @pointerup="onHeadUp"
      @pointercancel="onHeadUp"
    >
      <span class="fc-title">{{ props.title }}</span>
      <span class="spacer" />
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
  </div>
</template>

<style scoped>
/* 观感与侧栏卡片同一门语言：同一个圆角档、同一档阴影、同一个边框色。
   阴影比侧栏重一档（`--shadow-3`）—— 它是浮在正文上的，要能一眼看出"这是浮层"。 */
.float-card {
  position: fixed; z-index: 60;
  display: flex; flex-direction: column;
  border: 1px solid var(--border-strong); border-radius: 12px;
  background: var(--bg-elev); box-shadow: var(--shadow-3);
  overflow: hidden;
}
.fc-head {
  display: flex; align-items: center; gap: 4px; flex: none;
  padding: 6px 8px 6px 12px;
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

.fc-body { padding: 10px 12px; overflow: auto; min-height: 0; font-size: 12.5px; }
.fc-foot {
  display: flex; align-items: center; gap: 6px; flex: none;
  padding: 8px 12px; border-top: 1px solid var(--divider);
  background: var(--bg-layer);
}
</style>
