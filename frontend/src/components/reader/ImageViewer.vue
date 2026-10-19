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
 *
 * 为什么分成「目标 + 渲染」两套状态：
 * "内容不得露出视口"是硬约束，它的可滑动余量 max(0, (内容 - 视口) / 2) 在内容刚好等于视口的那一刻
 * 由 0 开始增长 —— 这一轴此前攒下的锚点位移无处可去，全被钳掉；余量一打开，缺失的位移又必须以
 * 正常速度补回来，位移对缩放的一阶导数在这里必然不连续，眼睛看到的就是"跳一下"。
 * 硬约束无法放弃（放弃就露白），所以把阶跃挡在渲染层：输入只改目标，渲染用一阶平滑去追，
 * 任何来自约束的突变都被吸收成一段连续的滑动。拖动/捏合不排队（必须跟手），直接落到渲染值上。
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

/** 渲染值：模板只认这一套 */
const z = ref(1);
const tx = ref(0);
const ty = ref(0);

/** 目标值：输入直接改这一套，渲染层负责追上去 */
let tz = 1;
let ttx = 0;
let tty = 0;

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

/* ------------------------------ 约束 ------------------------------ */

/** 可滑动余量：只有内容比视口大才有（两轴独立） */
function panRange(vz = tz) {
  const b = box.value;
  return {
    x: Math.max(0, (sheetW.value * vz - b.w) / 2),
    y: Math.max(0, (sheetH.value * vz - b.h) / 2),
  };
}

function clampTarget() {
  const m = panRange();
  ttx = Math.min(m.x, Math.max(-m.x, ttx));
  tty = Math.min(m.y, Math.max(-m.y, tty));
}

/* --------------------------- 渲染层（平滑） --------------------------- */

const reduceMotion = typeof matchMedia !== 'undefined'
  && matchMedia('(prefers-reduced-motion: reduce)').matches;

/** 一阶平滑的时间常数：短到不拖沓，长到足以吸收约束造成的阶跃 */
const TAU = reduceMotion ? 0 : 0.07;

let raf = 0;
let lastT = 0;

function tick(now: number) {
  const dt = Math.min(64, Math.max(1, now - lastT)) / 1000;
  lastT = now;
  const a = TAU > 0 ? 1 - Math.exp(-dt / TAU) : 1;
  z.value += (tz - z.value) * a;
  tx.value += (ttx - tx.value) * a;
  ty.value += (tty - ty.value) * a;
  const done = Math.abs(tz - z.value) < 1e-4
    && Math.abs(ttx - tx.value) < 0.05
    && Math.abs(tty - ty.value) < 0.05;
  if (done) {
    z.value = tz; tx.value = ttx; ty.value = tty;
    raf = 0;
    return;
  }
  raf = requestAnimationFrame(tick);
}

function kick() {
  if (raf) return;
  lastT = performance.now();
  raf = requestAnimationFrame(tick);
}

/** 目标立刻落到渲染上（换图、接管动画这类不能排队的情况） */
function snap() {
  if (raf) { cancelAnimationFrame(raf); raf = 0; }
  z.value = tz; tx.value = ttx; ty.value = tty;
}

/** 停掉正在跑的缓动，且让目标 == 当前画面（拖动接管时用，避免松手后又弹一下） */
function freeze() {
  if (raf) { cancelAnimationFrame(raf); raf = 0; }
  tz = z.value; ttx = tx.value; tty = ty.value;
}

function resetView() {
  tz = 1; ttx = 0; tty = 0;
  kick();
}

/* ------------------------------ 缩放 ------------------------------ */

/**
 * 缩放**只改倍率**，位移一律由拖动产生 —— 恒以视口中心为锚。
 *
 * 原因：任何"以光标为锚"的缩放，都要先推导一个位移，而这个位移会被「内容不得露出视口」的
 * 钳制截断；截掉的部分没人记账，等可滑动余量变大又得补回来，放大过程里那一记跳变就是这么来的。
 * 中心锚没有需要推导的位移，自然也就没有补账 —— 位移的唯一来源是手指/鼠标，钳制只是把它压在
 * 边界内，而边界随倍率连续变化，所以画面前后帧永远接得上。
 */
function zoomTo(next: number, smooth = true) {
  const nz = Math.min(Z_MAX, Math.max(Z_MIN, next));
  if (Math.abs(nz - tz) < 1e-4) return;
  tz = nz;
  clampTarget();   // 缩到装不下时，位移随着余量一起连续收回 0，不会弹回
  if (smooth) kick(); else snap();
}

function zoomStep(dir: number) {
  const next = dir > 0
    ? Z_STEPS.find((s) => s > tz + 1e-6) ?? Z_MAX
    : [...Z_STEPS].reverse().find((s) => s < tz - 1e-6) ?? Z_MIN;
  zoomTo(next);
}

function onWheel(ev: WheelEvent) {
  ev.preventDefault();
  /* 归一化：行/页模式换算成像素，单次增量封顶，触控板的小 delta 也走连续曲线 */
  const d = ev.deltaMode === 1 ? ev.deltaY * 16 : ev.deltaMode === 2 ? ev.deltaY * 400 : ev.deltaY;
  const step = Math.max(-240, Math.min(240, d));
  zoomTo(tz * Math.exp(-step * 0.0012));
}

/* ------------------------------ 拖动 / 双指 ------------------------------ */

const pointers = new Map<number, { x: number; y: number }>();
let pressAt: { x: number; y: number } | null = null;
let panFrom: { x: number; y: number; tx: number; ty: number } | null = null;
type PinchFrom = { dist: number; z: number; midX: number; midY: number; tx: number; ty: number };
let pinchFrom: PinchFrom | null = null;

function onPointerDown(ev: PointerEvent) {
  if (ev.button !== undefined && ev.button > 0) return;
  (ev.currentTarget as HTMLElement).setPointerCapture?.(ev.pointerId);
  pointers.set(ev.pointerId, { x: ev.clientX, y: ev.clientY });
  if (pointers.size >= 2) {
    panFrom = null;
    pressAt = null;
    freeze();
    const pair = [...pointers.values()];
    pinchFrom = {
      dist: Math.max(24, Math.hypot(pair[0].x - pair[1].x, pair[0].y - pair[1].y)),
      z: tz, tx: ttx, ty: tty,
      midX: (pair[0].x + pair[1].x) / 2,
      midY: (pair[0].y + pair[1].y) / 2,
    };
    return;
  }
  pressAt = { x: ev.clientX, y: ev.clientY };
  panFrom = null;
  pinchFrom = null;
}

function onPointerMove(ev: PointerEvent) {
  if (!pointers.has(ev.pointerId)) return;
  pointers.set(ev.pointerId, { x: ev.clientX, y: ev.clientY });
  if (pointers.size >= 2 && pinchFrom) {
    applyPinch();
    return;
  }
  if (!pressAt) return;
  /* 真的开始拖了才接管：接手时画面停在这一帧，基线按当前渲染值取，不会先跳一下再跟手 */
  if (!panFrom) {
    freeze();
    panFrom = { x: pressAt.x, y: pressAt.y, tx: tx.value, ty: ty.value };
  }
  ttx = panFrom.tx + (ev.clientX - panFrom.x);
  tty = panFrom.ty + (ev.clientY - panFrom.y);
  clampTarget();
  tx.value = ttx;
  ty.value = tty;
}

function onPointerUp(ev: PointerEvent) {
  pointers.delete(ev.pointerId);
  panFrom = null;
  if (pointers.size < 2) pinchFrom = null;
  if (pointers.size === 1) {
    /* 抬起一根手指后，剩下这根要当作"刚刚按下"：否则下一次移动会从它按下的旧位置起算，画面被甩一下 */
    const rest = [...pointers.values()][0];
    pressAt = { x: rest.x, y: rest.y };
  } else {
    pressAt = null;
  }
}

/** 双指：指距只改倍率，中点位移只改平移 —— 全是手指直接量出来的量，不从锚点反推位移 */
function applyPinch() {
  const p = pinchFrom;
  if (!p || pointers.size < 2) return;
  const pair = [...pointers.values()];
  const midX = (pair[0].x + pair[1].x) / 2;
  const midY = (pair[0].y + pair[1].y) / 2;
  const dist = Math.max(24, Math.hypot(pair[0].x - pair[1].x, pair[0].y - pair[1].y));
  tz = Math.min(Z_MAX, Math.max(Z_MIN, p.z * (dist / p.dist)));
  ttx = p.tx + (midX - p.midX);
  tty = p.ty + (midY - p.midY);
  clampTarget();
  snap();
}

function onDoubleClick() {
  zoomTo(tz > 1.05 ? 1 : 2);
}

/* ------------------------------ 尺寸 / 键位 ------------------------------ */

let ro: ResizeObserver | null = null;

function measure() {
  const el = stage.value;
  if (!el) return;
  box.value = { w: el.clientWidth, h: el.clientHeight };
  clampTarget();
  kick();
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
  tz = 1; ttx = 0; tty = 0;
  snap();
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
  if (raf) { cancelAnimationFrame(raf); raf = 0; }
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
/* 平滑交给 rAF（见脚本注释），这里不能再叠一层 CSS 过渡，否则两段插值会互相追赶 */
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
