<script setup lang="ts">
/**
 * 大图查看器（毛玻璃胶囊）：只看图 —— 缩放、拖动、旋转、关闭。
 *
 * 版式照参考图：**底部一枚胶囊**，从左到右 放大 / 百分比读数 / 缩小 / **重置为 100%** / 关闭（红色）。
 * 手写批注**不在这里**（用户口径：画笔属于阅读器页面），所以这个组件只负责"看清楚"。
 * 图上/图下**不再显示任何文字**（用户口径：查看图片时不要文字）：图注只在侧栏与正文里显示，
 * 这里留纯图 —— 胶囊里的百分比是控件读数，不算"文字说明"。
 *
 * 手势与变换交给 @panzoom/panzoom：滚动缩放、双指捏合、拖动都是它内部**按手势起点解析式算**
 * 的（每个事件都从 startScale / 起点距离重新推导，不在一帧的结果上累加），而且每次都会用
 * contain 重新求解位置。我们自己手写时丢掉的正是这两点 —— 被"内容不得露出视口"钳掉的位移
 * 记不住，等可滑动余量一出现就得把欠的补回来，于是放大过程中总有一记跳变。
 *
 * contain: 'inside' 的含义正好是我们要的约束（元素不许被拖出视口，缩放后自动贴边），
 * 配合 panOnlyWhenZoomed：100% 时锁定居中，放大后余量一出现就能拖，且余量是连续增长的。
 */
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import Panzoom from '@panzoom/panzoom';
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
const sheetStyle = computed(() => ({
  width: sheetW.value + 'px',
  height: sheetH.value + 'px',
}));

/** 当前倍率（panzoom 是唯一真相，这里只镜像出来给读数与按钮用） */
const scale = ref(1);
const pct = computed(() => Math.round(scale.value * 100));
/** 首帧定位完成前先不画，避免图片在左上角闪一下 */
const ready = ref(false);

let pz: ReturnType<typeof Panzoom> | null = null;
let boundWheel: ((ev: WheelEvent) => void) | null = null;
let boundChange: ((ev: Event) => void) | null = null;
let boundEl: HTMLElement | null = null;
let boundStage: HTMLElement | null = null;

/**
 * 把图片摆回视口正中。panzoom 的 translate 在 scale 之前，所以位移要除一次 scale 才是屏幕像素；
 * force 是必需的：panOnlyWhenZoomed 会把 100% 下的 pan 整个忽略掉。
 */
function recenter(animate = false) {
  const el = stage.value;
  if (!pz || !el) return;
  const s = pz.getScale() || 1;
  pz.pan(
    (el.clientWidth - sheetW.value) / (2 * s),
    (el.clientHeight - sheetH.value) / (2 * s),
    { animate, force: true },
  );
  ready.value = true;
}

function resetView() {
  if (!pz) return;
  pz.zoom(1, { animate: true });
  recenter(true);
}

function zoomStep(dir: number) {
  const el = stage.value;
  if (!pz || !el) return;
  const cur = pz.getScale();
  const next = dir > 0
    ? Z_STEPS.find((s) => s > cur + 1e-6) ?? Z_MAX
    : [...Z_STEPS].reverse().find((s) => s < cur - 1e-6) ?? Z_MIN;
  /* 以视口中心为锚：panzoom 的 zoom() 保持的是元素坐标系里的位移，屏幕位移会被 scale 乘一遍，
     直接用它会看着往左上漂，所以走 focal 版本 */
  const r = el.getBoundingClientRect();
  pz.zoomToPoint(next, { clientX: r.left + r.width / 2, clientY: r.top + r.height / 2 }, { animate: true });
}

function onDblClick(ev: MouseEvent) {
  if (!pz) return;
  if (pz.getScale() > 1.05) { resetView(); return; }
  pz.zoomToPoint(2, { clientX: ev.clientX, clientY: ev.clientY }, { animate: true });
}

function onKey(ev: KeyboardEvent) {
  if (ev.key === 'Escape') { emit('close'); return; }
  if (ev.key === '+' || ev.key === '=') { zoomStep(1); return; }
  if (ev.key === '-') { zoomStep(-1); return; }
  if (ev.key === '0') resetView();
}

let ro: ResizeObserver | null = null;

function measure() {
  const el = stage.value;
  if (!el) return;
  box.value = { w: el.clientWidth, h: el.clientHeight };
}

function onImgLoad() {
  const img = imgEl.value;
  if (!img) return;
  nat.value = { w: img.naturalWidth || 1, h: img.naturalHeight || 1 };
  measure();
}

/* 视口或适应尺寸一变，居中位置就得跟着重算（放大的图也重新居中，改动窗口时才发生） */
watch([() => box.value.w, () => box.value.h, sheetW, sheetH], () => recenter(), { flush: 'post' });

/* 换图：倍率归 1，尺寸由上面的 watch 接手居中 */
watch(() => props.target?.url, () => {
  nat.value = { w: 0, h: 0 };
  pz?.zoom(1, { animate: false });
  nextTick(() => recenter());
});

onMounted(() => {
  const el = sheet.value;
  const st = stage.value;
  if (st) {
    box.value = { w: st.clientWidth || window.innerWidth, h: st.clientHeight || window.innerHeight };
  }
  if (el) {
    pz = Panzoom(el, {
      minScale: Z_MIN,
      maxScale: Z_MAX,
      step: 0.34,             // 与旧的每格 12% 对齐（exp(step / 3)）
      contain: 'inside',      // 不许把图拖出视口 —— 放大后自动贴边
      panOnlyWhenZoomed: true, // 100% 时锁居中，余量一出现就连续放开
      cursor: 'grab',
    });
    boundChange = (ev) => { scale.value = (ev as CustomEvent<{ scale: number }>).detail.scale; };
    el.addEventListener('panzoomchange', boundChange);
    boundEl = el;
  }
  if (st && pz) {
    boundWheel = (ev) => { pz?.zoomWithWheel(ev); };
    st.addEventListener('wheel', boundWheel, { passive: false });
    boundStage = st;
  }
  measure();
  recenter();
  document.addEventListener('keydown', onKey);
  if (typeof ResizeObserver !== 'undefined' && st) {
    ro = new ResizeObserver(() => measure());
    ro.observe(st);
  }
});

onBeforeUnmount(() => {
  document.removeEventListener('keydown', onKey);
  if (boundWheel && boundStage) boundStage.removeEventListener('wheel', boundWheel);
  if (boundChange && boundEl) boundEl.removeEventListener('panzoomchange', boundChange);
  pz?.destroy();
  pz = null;
  boundWheel = null;
  boundChange = null;
  boundEl = null;
  boundStage = null;
  ro?.disconnect();
  ro = null;
});
</script>

<template>
  <Teleport to="body">
    <div ref="stage" class="viewer" @click.self="emit('close')">
      <div
        ref="sheet"
        class="sheet"
        :class="{ ready }"
        :style="sheetStyle"
        @dblclick="onDblClick"
      >
        <img
          ref="imgEl"
          class="shot"
          :src="target?.url || ''"
          :alt="target?.caption || '插图'"
          draggable="false"
          @load="onImgLoad"
        >
      </div>

      <div class="cap cap-zoom panzoom-exclude" @click.stop>
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
  background: #0d0f13;
  touch-action: none; user-select: none; -webkit-user-select: none;
}
/* 图片绝对定位在左上角，居中由 panzoom 的 pan 负责（contain 的求解以左上角为原点） */
.sheet {
  position: absolute; left: 0; top: 0;
  transform-origin: 50% 50%;
  will-change: transform; background: #fff;
  box-shadow: 0 18px 60px rgba(0, 0, 0, .55);
  cursor: grab;
  opacity: 0;
}
.sheet.ready { opacity: 1; }
.sheet:active { cursor: grabbing; }
.shot { display: block; width: 100%; height: 100%; -webkit-user-drag: none; }

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
