<script setup lang="ts">
/**
 * 大图查看器（毛玻璃胶囊）：只看图 —— 缩放、拖动、旋转、关闭。
 *
 * 版式照参考图：**底部一枚胶囊**，从左到右 放大 / 百分比读数 / 缩小 / **重置为 100%** / 关闭（红色）。
 * 手写批注**不在这里**（用户口径：画笔属于阅读器页面），所以这个组件只负责"看清楚"。
 * 图上/图下**不再显示任何文字**（用户口径：查看图片时不要文字）：图注只在侧栏与正文里显示，
 * 这里留纯图 —— 胶囊里的百分比是控件读数，不算"文字说明"。
 *
 * 交互：滚轮以光标为锚缩放、双指捏合、双击在 100% 与 200% 之间切换、Esc / 点背景关闭。
 */
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import AppIcon from '@/components/AppIcon.vue';
import type { ZoomTarget } from '@/api/types';

const props = defineProps<{ target: ZoomTarget | null }>();
const emit = defineEmits<{ (e: 'close'): void }>();

const stage = ref<HTMLElement | null>(null);
const sheet = ref<HTMLElement | null>(null);
const imgEl = ref<HTMLImageElement | null>(null);

/** 图片原始像素（只用来算宽高比） */
const nat = ref({ w: 0, h: 0 });
const box = ref({ w: 0, h: 0 });
const z = ref(1);
const tx = ref(0);
const ty = ref(0);

const Z_MIN = 0.6;
const Z_MAX = 6;
const Z_STEPS = [0.6, 0.8, 1, 1.25, 1.5, 2, 2.5, 3, 4, 5, 6];

/** 适应窗口（= 100%）：留出底部胶囊的空白 */
const fit = computed(() => {
  const b = box.value;
  const n = nat.value;
  if (!b.w || !b.h || !n.w || !n.h) return 1;
  const cw = Math.max(160, b.w - 120);
  const ch = Math.max(160, b.h - 170);
  return Math.min(cw / n.w, ch / n.h);
});
const sheetW = computed(() => Math.max(1, nat.value.w * fit.value));
const sheetH = computed(() => Math.max(1, nat.value.h * fit.value));
const pct = computed(() => Math.round(z.value * 100));
const sheetStyle = computed(() => ({
  width: sheetW.value + 'px',
  height: sheetH.value + 'px',
  // 先缩放、后平移（CSS 从右往左算）—— 平移因此是**屏幕像素**
  transform: 'translate3d(' + tx.value + 'px, ' + ty.value + 'px, 0) scale(' + z.value + ')',
}));

function clampPan() {
  const b = box.value;
  const visW = sheetW.value * z.value;
  const visH = sheetH.value * z.value;
  const mx = Math.max(0, (visW - b.w) / 2);
  const my = Math.max(0, (visH - b.h) / 2);
  tx.value = Math.min(mx, Math.max(-mx, tx.value));
  ty.value = Math.min(my, Math.max(-my, ty.value));
}

function resetView() {
  z.value = 1;
  tx.value = 0;
  ty.value = 0;
}

/** 以某个屏幕点为锚缩放（滚轮 / 双指都走这里）：锚点下的画面不动 */
function zoomAt(next: number, px?: number, py?: number) {
  const el = stage.value;
  const nz = Math.min(Z_MAX, Math.max(Z_MIN, next));
  if (!el || Math.abs(nz - z.value) < 1e-4) return;
  const rect = el.getBoundingClientRect();
  const cx = px ?? rect.left + rect.width / 2;
  const cy = py ?? rect.top + rect.height / 2;
  const ax = rect.left + rect.width / 2 + tx.value;   // 图片中心在屏幕上的位置
  const ay = rect.top + rect.height / 2 + ty.value;
  const k = nz / z.value;
  tx.value += (cx - ax) * (1 - k);
  ty.value += (cy - ay) * (1 - k);
  z.value = nz;
  clampPan();
}

function zoomStep(dir: number) {
  const next = dir > 0
    ? Z_STEPS.find((s) => s > z.value + 1e-6) ?? Z_MAX
    : [...Z_STEPS].reverse().find((s) => s < z.value - 1e-6) ?? Z_MIN;
  zoomAt(next);
}

function onWheel(ev: WheelEvent) {
  ev.preventDefault();
  zoomAt(z.value * (ev.deltaY < 0 ? 1.12 : 1 / 1.12), ev.clientX, ev.clientY);
}

/* ------------------------------ 拖动 / 双指 ------------------------------ */

const pointers = new Map<number, { x: number; y: number }>();
let panFrom: { x: number; y: number; tx: number; ty: number } | null = null;
let pinchFrom: { dist: number; z: number } | null = null;

function onPointerDown(ev: PointerEvent) {
  if (ev.button !== undefined && ev.button > 0) return;
  (ev.currentTarget as HTMLElement).setPointerCapture?.(ev.pointerId);
  pointers.set(ev.pointerId, { x: ev.clientX, y: ev.clientY });
  if (pointers.size >= 2) {
    panFrom = null;
    const pair = [...pointers.values()];
    pinchFrom = { dist: Math.max(24, Math.hypot(pair[0].x - pair[1].x, pair[0].y - pair[1].y)), z: z.value };
    return;
  }
  panFrom = { x: ev.clientX, y: ev.clientY, tx: tx.value, ty: ty.value };
}

function onPointerMove(ev: PointerEvent) {
  if (!pointers.has(ev.pointerId)) return;
  pointers.set(ev.pointerId, { x: ev.clientX, y: ev.clientY });
  if (pointers.size >= 2 && pinchFrom) {
    const pair = [...pointers.values()];
    const dist = Math.max(24, Math.hypot(pair[0].x - pair[1].x, pair[0].y - pair[1].y));
    zoomAt(pinchFrom.z * (dist / pinchFrom.dist),
      (pair[0].x + pair[1].x) / 2, (pair[0].y + pair[1].y) / 2);
    return;
  }
  if (panFrom) {
    tx.value = panFrom.tx + (ev.clientX - panFrom.x);
    ty.value = panFrom.ty + (ev.clientY - panFrom.y);
    clampPan();
  }
}

function onPointerUp(ev: PointerEvent) {
  pointers.delete(ev.pointerId);
  if (pointers.size < 2) pinchFrom = null;
  panFrom = null;
}

function onDoubleClick(ev: MouseEvent) {
  zoomAt(z.value > 1.05 ? 1 : 2, ev.clientX, ev.clientY);
}

/* ------------------------------ 尺寸 / 键位 ------------------------------ */

let ro: ResizeObserver | null = null;

function measure() {
  const el = stage.value;
  if (!el) return;
  box.value = { w: el.clientWidth, h: el.clientHeight };
  clampPan();
}

function onImgLoad() {
  const img = imgEl.value;
  if (!img) return;
  nat.value = { w: img.naturalWidth || 1, h: img.naturalHeight || 1 };
  measure();
}

function onKey(ev: KeyboardEvent) {
  if (ev.key === 'Escape') { emit('close'); return; }
  if (ev.key === '+' || ev.key === '=') { zoomStep(1); return; }
  if (ev.key === '-') { zoomStep(-1); return; }
  if (ev.key === '0') resetView();
}

watch(() => props.target?.url, () => {
  resetView();
  nat.value = { w: 0, h: 0 };
});

onMounted(() => {
  measure();
  document.addEventListener('keydown', onKey);
  if (typeof ResizeObserver !== 'undefined' && stage.value) {
    ro = new ResizeObserver(() => measure());
    ro.observe(stage.value);
  }
});

onBeforeUnmount(() => {
  document.removeEventListener('keydown', onKey);
  ro?.disconnect();
  ro = null;
});
</script>

<template>
  <Teleport to="body">
    <div ref="stage" class="viewer" @wheel.prevent="onWheel" @click.self="emit('close')">
      <div ref="sheet" class="sheet" :style="sheetStyle">
        <img
          ref="imgEl"
          class="shot"
          :src="target?.url || ''"
          :alt="target?.caption || '插图'"
          draggable="false"
          @load="onImgLoad"
        >
        <div
          class="capture"
          @pointerdown="onPointerDown"
          @pointermove="onPointerMove"
          @pointerup="onPointerUp"
          @pointercancel="onPointerUp"
          @dblclick="onDoubleClick"
        />
      </div>

      <div class="cap cap-zoom" @click.stop>
        <button class="zbtn" title="放大" data-viewer-zoom="in" @click="zoomStep(1)">
          <AppIcon name="zoom-in" :size="18" />
        </button>
        <span class="zpct" data-viewer-zoom="pct">{{ pct }}%</span>
        <button class="zbtn" title="缩小" data-viewer-zoom="out" @click="zoomStep(-1)">
          <AppIcon name="zoom-out" :size="18" />
        </button>
        <button class="zbtn" title="重置为 100%（适应窗口）" data-viewer-zoom="reset" @click="resetView">
          <AppIcon name="rotate" :size="18" />
        </button>
        <button class="zbtn zclose" title="关闭" data-viewer-zoom="close" @click="emit('close')">
          <AppIcon name="close" :size="18" />
        </button>
      </div>
    </div>
  </Teleport>
</template>

<style scoped>
/* 整屏深底（参考图就是黑底看图）；图片本身保持白底（论文插图是白的） */
.viewer {
  position: fixed; inset: 0; z-index: 240; overflow: hidden;
  display: flex; align-items: center; justify-content: center;
  background: #0d0f13;
  touch-action: none; user-select: none; -webkit-user-select: none;
}
.sheet {
  position: relative; flex: none; transform-origin: center center;
  will-change: transform; background: #fff;
  box-shadow: 0 18px 60px rgba(0, 0, 0, .55);
}
.shot { display: block; width: 100%; height: 100%; -webkit-user-drag: none; }
.capture { position: absolute; inset: 0; touch-action: none; cursor: grab; }
.capture:active { cursor: grabbing; }

/* 毛玻璃胶囊：半透明壳 + 大模糊 + 亮描边 + 柔投影，按钮是壳里的圆片 */
.cap {
  position: absolute; left: 50%; transform: translateX(-50%);
  display: flex; align-items: center; gap: 2px;
  padding: 6px; border-radius: 999px;
  background: color-mix(in srgb, #2b2f36 62%, transparent);
  border: 1px solid rgba(255, 255, 255, .10);
  box-shadow: 0 10px 30px rgba(0, 0, 0, .45), inset 0 1px 0 rgba(255, 255, 255, .08);
  backdrop-filter: blur(28px) saturate(1.8);
  -webkit-backdrop-filter: blur(28px) saturate(1.8);
}
.cap-zoom { bottom: max(18px, env(safe-area-inset-bottom)); }

.zbtn {
  width: 34px; height: 34px; flex: none; padding: 0; cursor: pointer;
  display: inline-flex; align-items: center; justify-content: center;
  border: 0; border-radius: 999px; background: transparent; color: rgba(255, 255, 255, .80);
  transition: background 120ms ease, color 120ms ease, transform 120ms ease;
}
.zbtn:hover { background: rgba(255, 255, 255, .10); color: #fff; }
.zbtn:active { transform: scale(.94); }
.zpct {
  /* 只是**读数**，不是按钮（用户口径：右边的 ↺ 才是重置）。
     ⚠️ 字体走界面字体变量：以前这里写死 monospace 栈，中文环境落到宋体，数字还高低不齐。 */
  display: inline-flex; align-items: center; justify-content: center;
  min-width: 62px; height: 34px; padding: 0 6px;
  color: #fff; font-family: inherit; font-size: calc(13px * var(--ts, 1)); font-weight: 600;
  line-height: 1; letter-spacing: .2px;
  font-variant-numeric: tabular-nums; font-feature-settings: 'tnum' 1;
  user-select: none;
}
/* 关闭：参考图里那枚**红色**圆片，是唯一的警示色 */
.zclose { background: rgba(198, 61, 61, .92); color: #fff; }
.zclose:hover { background: #d04545; color: #fff; }

@media (max-width: 640px) {
  .zbtn { width: 32px; height: 32px; }
  .zpct { min-width: 52px; }
}
</style>
