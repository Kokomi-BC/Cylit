<script setup lang="ts">
/**
 * 大图查看器：只看图 —— 缩放、双指旋转、拖动、下滑关闭。
 *
 * 版式照参考图：**底部一枚胶囊**，从左到右 放大 / 百分比读数 / 缩小 / **重置为 100%** / 关闭（红色）。
 * 手写批注**不在这里**（用户口径：画笔属于阅读器页面），所以这个组件只负责"看清楚"。
 *
 * 手势与边界算法**整体搬进** `./detectImageViewer`（源自 Detect 移动端 mobile.js 的图片查看器，
 * 逐字移植：捏合中不夹取、松手才带 0.35s 缓动归位、旋转吸附 90°、缩不到 100% 补回 100%、
 * 按旋转包围盒算边界、以手指中点为锚的解析式位移补偿）。这个组件只做四件事：
 * 1. 提供 DOM：整屏 `.viewer` / 定尺寸的 `.sheet` / 被变换的 `.shot` / 底部毛玻璃胶囊；
 * 2. 把 100% 的可用框（留出边距与胶囊）喂给控制器，接回它锁定的像素尺寸渲染 `.sheet`；
 * 3. 把胶囊按钮、键盘、双击接到控制器（放大缩小走 Z_STEPS 阶梯，重置调 reset）；
 * 4. 订阅控制器上报的倍率，刷新百分比读数；订阅切图下标，同步"当前第几张"。
 *
 * 相邻图由 prop `gallery` 决定（左右拖动在它们之间切上一张 / 下一张）；不传就是单图，行为与之前一致。
 * 桌面鼠标拖动由控制器里的 pointer 通道接住，这个组件不用为它做任何事。
 *
 * 控制器自己直接写 transform（这是手势时序正确的前提：Vue 的渲染在下一个 tick，边界会读到上一帧尺寸），
 * 过渡也由它在"归位"那一下临时开，所以 CSS 里没有常驻的 `transition: transform`。
 */
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import AppIcon from '@/components/AppIcon.vue';
import type { ZoomTarget } from '@/api/types';
import { createDetectImageViewer, type DetectImageViewer } from './detectImageViewer';

/** 视口矩形：触发查看器的那张缩略图关闭时飞回去用的落点 */
type ViewerRect = { left: number; top: number; width: number; height: number };

/**
 * 相邻图条目：同一个 figure 可能有两种 URL（合成卡片 / 原图），正文里点开的是哪一种并不固定，
 * 所以两个都带上 —— url 用来显示，altUrl 只用来匹配"用户点的是哪一张"。
 */
type GalleryTarget = ZoomTarget & { altUrl?: string | null };

const props = withDefaults(defineProps<{
  target: ZoomTarget | null;
  /** 同一页组里的所有图：左右拖动就在它们之间切换；不给就是单图 */
  gallery?: GalleryTarget[];
  /** 打开查看器的那张缩略图在视口中的矩形（飞回原位用）；缺省或已失效时直接关闭 */
  origin?: ViewerRect | null;
}>(), { gallery: () => [], origin: null });
const emit = defineEmits<{ (e: 'close'): void }>();

const stage = ref<HTMLElement | null>(null);
const imgEl = ref<HTMLImageElement | null>(null);

let viewer: DetectImageViewer | null = null;
let ro: ResizeObserver | null = null;

/** 图片自然像素：只用来向控制器报告尺寸 */
const nat = ref({ w: 0, h: 0 });
/** .sheet 的像素尺寸（100% = 适应窗口），由控制器锁定后回推 */
const size = ref({ w: 0, h: 0 });
/** 百分比读数（订阅控制器） */
const pct = ref(100);

/** 倍率阶梯：按钮 / 键盘在它上面走一格，真正的缩放交给控制器 */
const Z_STEPS = [0.6, 0.8, 1, 1.25, 1.5, 2, 2.5, 3, 4, 5, 6];

/** 100% 的可用框：原来的 fit 就是这两个口径（留边距 + 底部胶囊的空白） */
function fitBox() {
  const el = stage.value;
  const w = el ? el.clientWidth : window.innerWidth;
  const h = el ? el.clientHeight : window.innerHeight;
  return { w: Math.max(160, w - 120), h: Math.max(160, h - 170) };
}

/** 容器尺寸变了就重新锁定 100% 基准（旋转过的那张图会按包围盒重算） */
function relock() {
  viewer?.setImage(nat.value.w, nat.value.h);
}

function onImgLoad() {
  const el = imgEl.value;
  if (!el) return;
  nat.value = { w: el.naturalWidth || 0, h: el.naturalHeight || 0 };
  relock();
}

/* ------------------------------ 相邻图（gallery） ------------------------------ */

/** 控制器内部下标：切图成功后由控制器回调同步过来（父组件暂不依赖，不新增 emit） */
const currentIndex = ref(0);
/** 当前正在用的相邻图列表（决定胶囊上要不要出现上/下一张） */
const galleryList = ref<GalleryTarget[]>([]);
const hasPrev = computed(() => currentIndex.value > 0);
const hasNext = computed(() => currentIndex.value < galleryList.value.length - 1);

/** gallery 的内容指纹：父组件每次渲染都传新数组时，不至于无谓地复位视图 */
const galleryKey = computed(() => (props.gallery || [])
  .map((g) => `${g?.url ?? ''}#${g?.altUrl ?? ''}#${g?.page ?? ''}`).join('|'));

/**
 * 定「相邻图列表 + 当前下标」：只按 URL 精确匹配（url 或 altUrl）。
 *
 * 以前匹配不上时会退化成"同页的第一张"，同页有多张图时就会**显示出不是点击的那一张**。
 * 匹配不上就老老实实当单图 —— 宁可没有左右切换，也不能显示错的图。
 */
function resolveGallery(target: ZoomTarget | null): { list: GalleryTarget[]; idx: number } {
  if (!target) return { list: [], idx: 0 };
  const gallery = (props.gallery || []).filter((g) => !!g && !!g.url);
  if (!gallery.length) return { list: [target], idx: 0 };
  const idx = gallery.findIndex((g) => g.url === target.url || (!!g.altUrl && g.altUrl === target.url));
  if (idx < 0) return { list: [target], idx: 0 };
  return { list: gallery, idx };
}

/** 把 gallery 推给控制器：真正换 src、复位视图、按 100% 重新锁定尺寸都由它负责 */
function syncGallery() {
  const { list, idx } = resolveGallery(props.target);
  galleryList.value = list;
  currentIndex.value = idx;
  viewer?.setGallery(list.map((g) => ({ url: g.url, caption: g.caption ?? null })), idx);
}

/* ------------------------------ 胶囊 / 键盘 / 双击 ------------------------------ */

function zoomStep(dir: number) {
  if (!viewer) return;
  const cur = viewer.getScale();
  const next = dir > 0
    ? Z_STEPS.find((s) => s > cur + 1e-6) ?? cur
    : [...Z_STEPS].reverse().find((s) => s < cur - 1e-6) ?? cur;
  if (next === cur) return;
  viewer.zoomBy(next / cur);
}

function resetView() {
  viewer?.reset();
}

/** 胶囊上的上一张 / 下一张 */
function stepImage(dir: number) {
  viewer?.stepTo(dir);
}

function onDoubleClick(ev: MouseEvent) {
  if (!viewer) return;
  const cur = viewer.getScale();
  const target = cur > 1.05 ? 1 : 2;
  viewer.zoomBy(target / cur, ev.clientX, ev.clientY);
}

/* ------------------------------ 关闭（飞回原位） ------------------------------ */

/** 入场完成（背景色与整体透明度由它驱动） */
const opened = ref(false);
let openDone = false;

/**
 * 入场：把图片先摆到源缩略图的位置（瞬时、不可见），下一帧再放大回 100%，同时背景淡入。
 * onSize 是"100% 基准尺寸"就绪的唯一信号，所以入场必须等它 —— 早一帧算出来的目标矩形是错的。
 */
function playOpen() {
  if (openDone || !viewer) return;
  openDone = true;
  const o = props.origin;
  const st = stage.value;
  if (!o || !size.value.w || !st) { opened.value = true; return; }
  const stageW = st.clientWidth || window.innerWidth;
  const stageH = st.clientHeight || window.innerHeight;
  viewer.animateTo(
    Math.max(0.02, o.width / size.value.w),
    o.left + o.width / 2 - stageW / 2,
    o.top + o.height / 2 - stageH / 2,
    0,
  );
  requestAnimationFrame(() => {
    opened.value = true;
    viewer?.animateTo(1, 0, 0, 300, 'cubic-bezier(0.16, 0.84, 0.28, 1)');
  });
}

/** 关闭中（透明度在淡出）：既是 class，也是"只关一次"的守卫 */
const closing = ref(false);
let closeTimer: number | null = null;

function finishClose() {
  closeTimer = null;
  emit('close');
}

/**
 * 唯一的关闭出口：图片先飞回触发它的那张缩略图，同时整屏淡出，再通知父组件卸载。
 *
 * 落点几何：查看器里图片 100% 时是 `size.w × size.h`（.sheet 的像素尺寸，以视口中心居中），
 * 缩略图是 `origin.width × origin.height` 居中在 (left + w/2, top + h/2)。
 * 于是 tScale = 缩略图宽 / 100% 宽；位移 = 缩略图中心 - 视口中心（控制器里的 translate 就是相对视口中心的）。
 */
function requestClose(reason?: 'dismiss') {
  if (closing.value) return;
  closing.value = true;
  const o = props.origin;
  if (!viewer) { finishClose(); return; }
  if (reason === 'dismiss') {
    /* 图片已经被甩出去了：顺着方向缩着淡掉就行，飞回原位又远又慢 */
    viewer.fadeOut(110);
    closeTimer = window.setTimeout(finishClose, 120);
    return;
  }
  if (!o || !size.value.w) {
    /* 拿不到源图矩形（例如对话里贴的图、外部链接图）：退化成原地缩小 + 整屏淡出，
       总之不能一按就"啪"地消失。 */
    const s = viewer.getScale();
    viewer.beginExit(Math.max(0.2, s * 0.86), 0, 0, 200);
    closeTimer = window.setTimeout(finishClose, 210);
    return;
  }
  /* 旋转过（90°/270°）说明 .sheet 现在是旋转后的包围盒，直接飞回长宽比会对不上；
     先让控制器把旋转归零、按正向重新锁一次尺寸，几何才是对的。 */
  if (nat.value.w) viewer.setImage(nat.value.w, nat.value.h);
  const stageW = stage.value?.clientWidth || window.innerWidth;
  const stageH = stage.value?.clientHeight || window.innerHeight;
  const w = size.value.w || 1;
  const tScale = o.width / w;
  const tx = o.left + o.width / 2 - stageW / 2;
  const ty = o.top + o.height / 2 - stageH / 2;
  viewer.beginExit(tScale, tx, ty, 240, 'cubic-bezier(0.45, 0, 0.55, 1)');
  closeTimer = window.setTimeout(finishClose, 260);
}

/** 点背景关闭：刚拖动 / 捏合过就不算点击 */
function onBgClick() {
  /* 指针捕获会把 click 的 target 变成 stage，所以不能只看 @click.self：
     真正判断依据是「按下时是不是落在黑底上」，否则点胶囊按钮、点图片都会把查看器关掉。 */
  if (!viewer?.isBackgroundPress()) return;
  if (viewer.isGesturing()) return;
  requestClose();
}

function onKey(ev: KeyboardEvent) {
  if (ev.key === 'Escape') { requestClose(); return; }
  if (ev.key === '+' || ev.key === '=') { zoomStep(1); return; }
  if (ev.key === '-') { zoomStep(-1); return; }
  if (ev.key === '0') resetView();
}

/* ------------------------------ 生命周期 ------------------------------ */

watch([() => props.target?.url, () => props.target?.page, galleryKey], () => {
  nat.value = { w: 0, h: 0 };
  syncGallery();
});

onMounted(() => {
  const stageEl = stage.value;
  const imageEl = imgEl.value;
  if (stageEl && imageEl) {
    viewer = createDetectImageViewer({
      stage: stageEl,
      img: imageEl,
      onClose: (reason) => requestClose(reason),   // 下滑 / 缩得过小：走短的淡出，不飞回
      getSize: fitBox,
      onSize: (w, h) => { size.value = { w, h }; playOpen(); },
    });
    viewer.onChange((s) => { pct.value = Math.round(s * 100); });
    viewer.onIndexChange((i) => { currentIndex.value = i; });
    syncGallery();
  }

  document.addEventListener('keydown', onKey);

  if (typeof ResizeObserver !== 'undefined' && stageEl) {
    ro = new ResizeObserver(() => relock());
    ro.observe(stageEl);
  }

  // 图片可能已经在缓存里加载完了（@load 不一定再触发）
  if (imageEl && imageEl.complete && imageEl.naturalWidth > 0) onImgLoad();
});

onBeforeUnmount(() => {
  if (closeTimer !== null) { clearTimeout(closeTimer); closeTimer = null; }
  document.removeEventListener('keydown', onKey);
  ro?.disconnect();
  ro = null;
  viewer?.destroy();
  viewer = null;
});
</script>

<template>
  <Teleport to="body">
    <div
      ref="stage"
      class="viewer"
      :class="{ closing, open: opened }"
      @click.self="onBgClick"
      @dblclick="onDoubleClick"
    >
      <div class="sheet" :style="{ width: size.w + 'px', height: size.h + 'px' }">
        <img
          ref="imgEl"
          class="shot"
          :src="target?.url || ''"
          :alt="target?.caption || '插图'"
          draggable="false"
          @load="onImgLoad"
        >
      </div>

      <div class="cap cap-zoom" @click.stop>
        <button v-if="hasPrev" class="zbtn" title="上一张" data-viewer-zoom="prev" @click="stepImage(-1)">
          <AppIcon name="back" :size="18" />
        </button>
        <button class="zbtn" title="放大" data-viewer-zoom="in" @click="zoomStep(1)">
          <AppIcon name="zoom-in" :size="18" />
        </button>
        <span class="zpct" data-viewer-zoom="pct">{{ pct }}%</span>
        <button class="zbtn" title="缩小" data-viewer-zoom="out" @click="zoomStep(-1)">
          <AppIcon name="zoom-out" :size="18" />
        </button>
        <button v-if="hasNext" class="zbtn" title="下一张" data-viewer-zoom="next" @click="stepImage(1)">
          <span class="zflip"><AppIcon name="back" :size="18" /></span>
        </button>
        <button class="zbtn zclose" title="关闭" data-viewer-zoom="close" @click="requestClose()">
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
  background: #0d0f13; cursor: grab;
  touch-action: none; user-select: none; -webkit-user-select: none;
}
.viewer:active { cursor: grabbing; }
/* 往下甩的时候胶囊先走一步（控制器在进入下滑手势时给 stage 挂 dismissing） */
.viewer.dismissing .cap { opacity: 0; transition: opacity 120ms ease; }
/* 入场：整体从透明显形（图片同时从缩略图位置放大过来）。
   中途不能靠整屏透明度做过渡 —— 那样关闭时图片会跟着一起淡掉，看不到"飞回去"。 */
.viewer { opacity: 0; }
.viewer.open { opacity: 1; transition: opacity 140ms ease; }
/* 关闭：只让黑底淡出，图片保持可见地飞回原位 */
.viewer.closing { background: rgba(13, 15, 19, 0); transition: background-color 240ms ease; }
/* 飞回原位时不再拖着一张 60px 的投影：它是"弹窗"的语义，飞回缩略图时留着会很突兀 */
.viewer.closing .shot { box-shadow: none; transition: box-shadow 160ms ease; }
/* 下一张用同一枚返回箭头镜像一下 */
.zflip { display: inline-flex; transform: scaleX(-1); }
/* 只负责定尺寸，真正的 transform 在 .shot 上（控制器直接写） */
.sheet {
  position: relative; flex: none;
}
/* 白底与投影跟着图一起旋转，所以落在被变换的这张图上；过渡由控制器在手势里临时开，
   这里不写常驻 transition: transform（与 Detect 一致：只有归位那一下才有过渡） */
.shot {
  display: block; width: 100%; height: 100%;
  background: #fff; box-shadow: 0 18px 60px rgba(0, 0, 0, .55);
  /* 旋转 / 缩放全程交给合成层，避免每帧重新栅格化造成掉帧 */
  will-change: transform;
  backface-visibility: hidden;
  transform-origin: center center; will-change: transform;
  -webkit-user-drag: none;
}

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
