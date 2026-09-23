<script setup lang="ts">
/**
 * 原 PDF 页渲染（**真实 PDF**：矢量页 + 文字层，可选中/复制/搜索）。
 *
 * 为什么不用后端那张 150dpi PNG：
 *   - 位图**选不中文字**（读者想引原文只能手打），缩放还发虚；
 *   - PDF 本身有文字层，交给 pdf.js 渲染是矢量清晰 + 真文字，体积往往还更小。
 *
 * 适用范围：**只有非扫描页**才走这里（`PageInfo.has_text_layer`）。扫描件的 PDF 里
 * 就是一整页位图，走 pdf.js 白搭内存，因此调用方仍用后端的页图。
 *
 * 与布局的配合：**宽度由外层容器决定**（外层 `.media-box` 的 `max-width:100%` 可能把
 * 页面宽度夹窄），组件自己量 `clientWidth` 出图 —— 不能把宽度写成固定 px：
 * 一旦外框比内框窄，内框会溢出被裁，看起来就像"页面偏右 / 右边被切"（而且文字位图尺寸对不上）。
 * 页面**进入视口附近才渲染**（IntersectionObserver），22 页不会一次性全画出来；
 * 容器尺寸变化（窗口缩放 / 页宽滑杆）会重新出图，保证位图与显示宽度 1:1×dpr。
 */
import { computed, onBeforeUnmount, onMounted, ref } from 'vue';
import * as pdfjs from 'pdfjs-dist';
import type { PDFDocumentProxy, RenderTask } from 'pdfjs-dist';
import workerUrl from 'pdfjs-dist/build/pdf.worker.min.mjs?url';
import { assetUrl } from '@/api/client';

pdfjs.GlobalWorkerOptions.workerSrc = workerUrl;

/* ------------------------------ 隐藏标签页的 rAF 垫片 ------------------------------
 * pdf.js 的渲染是**分片推进**的，切片调度用的是 `requestAnimationFrame`。
 * 而隐藏标签页里 rAF **完全不触发**（同 IO/RO 一起停摆），后果是：画布画了第一片就停住、
 * `renderTask.promise` 永远不 resolve（实测：canvas 已出图但状态卡在"正在渲染…"、文字层为空）。
 * 定时器在隐藏页里会跑（节流到 ~1s）→ 隐藏期间把 rAF 临时换成 setTimeout，可见后立即还原。
 * 本组件只随阅读器路由懒加载，所以这个全局垫片的作用域仅限阅读器页。 */
const nativeRaf = typeof window !== 'undefined' ? window.requestAnimationFrame.bind(window) : null;
let rafShimmed = false;

function syncRafShim() {
  if (!nativeRaf) return;
  const hidden = document.visibilityState !== 'visible';
  if (hidden && !rafShimmed) {
    window.requestAnimationFrame = ((cb: FrameRequestCallback) =>
      window.setTimeout(() => cb(performance.now()), 16) as unknown as number);
    rafShimmed = true;
  } else if (!hidden && rafShimmed) {
    window.requestAnimationFrame = nativeRaf as typeof window.requestAnimationFrame;
    rafShimmed = false;
  }
}

if (typeof document !== 'undefined') {
  syncRafShim();
  document.addEventListener('visibilitychange', syncRafShim);
}

const props = defineProps<{
  paperId: string;
  page: number;
  /** 期望宽度（px）：仅作容器还没量出来时的兑底值（真实宽度以容器 clientWidth 为准） */
  width: number;
  /** PDF 点尺寸（来自 `PageInfo.width/height`），仅用于渲染前的比例占位 */
  pageWidth?: number | null;
  pageHeight?: number | null;
  /** 设备像素比上限（默认按屏幕，最高 2：再高只是徒增内存） */
  maxDpr?: number;
}>();

const host = ref<HTMLDivElement | null>(null);
const canvas = ref<HTMLCanvasElement | null>(null);
const textHost = ref<HTMLDivElement | null>(null);
const state = ref<'idle' | 'loading' | 'ready' | 'error'>('idle');
/** 出图尺寸（CSS px）：**一旦出图就固定**，容器再变宽变窄只改 `scale`（纯 CSS） */
const size = ref<{ w: number; h: number } | null>(null);
/** 显示倍率 = 当前容器宽 / 出图宽。改它不需重新光栅化。 */
const scale = ref(1);

/** 渲染失败时的兵底：后端那张 150dpi 页图（宁可退回位图，也不要开天窗） */
const fallbackUrl = computed(
  () => assetUrl(`/api/v1/papers/${props.paperId}/pages/${props.page}`),
);

/* 同一篇论文只加载一次 PDF（22 页共用一份 document） */
const docCache = new Map<string, Promise<PDFDocumentProxy>>();

function loadDoc(paperId: string): Promise<PDFDocumentProxy> {
  let hit = docCache.get(paperId);
  if (!hit) {
    hit = pdfjs.getDocument({ url: assetUrl(`/api/v1/papers/${paperId}/file`) }).promise.catch(
      (err: unknown) => {
        docCache.delete(paperId);   // 失败不留毒缓存，下次重试
        throw err;
      },
    );
    docCache.set(paperId, hit);
  }
  return hit;
}

const dpr = () => Math.min(props.maxDpr ?? 2, Math.max(1, window.devicePixelRatio || 1));

/** 出图前的比例占位（用 PDF 点尺寸，和出图后同比例）；出图后高度 = 出图高 × 倍率 */
const boxStyle = computed(() => {
  const style: Record<string, string> = {};
  const s = size.value;
  if (s) {
    style.height = `${(s.h * scale.value).toFixed(2)}px`;
  } else {
    const w = props.pageWidth ?? 0;
    const h = props.pageHeight ?? 0;
    if (w > 0 && h > 0) style.aspectRatio = `${w} / ${h}`;
  }
  return style;
});

/** 页面表面：固定出图尺寸，靠 transform 缩放（画布与文字层一起缩放 → 对齐不会跑） */
const surfaceStyle = computed(() => {
  const s = size.value;
  if (!s) return {};
  return { width: `${s.w}px`, height: `${s.h}px`, transform: `scale(${scale.value})` };
});

/** 量得到的容器宽度（px）。**出图宽度只能来自这里**：
 *  入参 `width` 是"期望宽度"，外框（`.media-box` 带 `max-width:100%`）可能把它夹窄，
 *  按它出图就会比外框宽 → 溢出被 `overflow:hidden` 裁掉，看起来像"页面偏右/右边被切"。 */
function measuredWidth(): number {
  return Math.round(host.value?.clientWidth ?? 0);
}

/** 与 IntersectionObserver 的 `rootMargin` 保持同一个判定口径（"提前一屏"） */
function nearViewport(margin = 900): boolean {
  const r = host.value?.getBoundingClientRect();
  if (!r) return false;
  return r.bottom > -margin && r.top < window.innerHeight + margin;
}

let task: RenderTask | null = null;
let destroyed = false;
let renderedFor = '';
/** 出图次数（写到 `data-renders`，方便自检/性能排查：改尺寸不应该把它刷上去） */
let renderCount = 0;
/** **只表示"曾经进入过视口附近"**；重出图前还要用 `nearViewport()` 再判一次当前位置，
 *  否则滚过全篇之后每一页都"算可见"，一次侧栏开合就会把 22 页全部重出（实测卡顿就是这么来的）。 */
let visible = false;
let rerenderTimer: ReturnType<typeof setTimeout> | null = null;

/** 重出一次的成本 = 重新光栅化 + 重建文字层；只有倍率偏到肉眼可见才值得 */
function worthRerender(nowW: number): boolean {
  const s = size.value;
  if (!s || !s.w) return true;
  return Math.abs(nowW / s.w - 1) > 0.15;
}

/** 只同步 CSS 倍率（零光栅化、零文字层重建） */
function syncScale(nowW: number) {
  const s = size.value;
  if (s && s.w > 0 && nowW > 0) scale.value = nowW / s.w;
}

/** 渲染条件：靠近过视口、容器宽度量得出来、且**当前不在出图中**。
 *  （出图期间不排队新渲染：内容尺寸变化会让宽度反复变，否则会形成"一直重出"的死循环。） */
function maybeRender() {
  if (!visible || state.value === 'loading') return;
  if (measuredWidth() < 40) return;
  void render();
}

/**
 * 容器尺寸变了（开/合侧栏、窗口缩放、页宽滑杆）：
 * 1. **先只改 `scale`** —— 立即生效、不花 CPU；
 * 2. 只有在"这一页还在视口附近"且"倍率已偏得肉眼可见（>15%）"时，
 *    才防抖 450ms 后补一次重出（保证清晰度，但不会一开侧栏就把全篇重画）。
 */
function onResize() {
  const w = measuredWidth();
  if (w < 40) return;
  if (!size.value) { maybeRender(); return; }    // 还没出过图 → 直接出
  syncScale(w);
  if (state.value === 'loading') return;         // 出图中：结束后有一次复核
  if (!nearViewport()) return;                   // 离屏页：等它靠近视口再说
  if (!worthRerender(w)) return;
  if (rerenderTimer) clearTimeout(rerenderTimer);
  rerenderTimer = setTimeout(() => { renderedFor = ''; maybeRender(); }, 450);
}

async function render() {
  const el = canvas.value;
  const hostEl = host.value;
  if (!el || !hostEl) return;
  const cssW = measuredWidth();
  if (cssW < 40) return;   // 还没排版：交给 ResizeObserver / 定时器叫醒
  const key = `${props.paperId}|${props.page}|${cssW}`;
  if (key === renderedFor) return;
  renderedFor = key;
  renderCount += 1;
  hostEl.dataset.renders = String(renderCount);

  state.value = 'loading';
  try {
    const doc = await loadDoc(props.paperId);
    if (destroyed) return;
    const page = await doc.getPage(props.page);
    const base = page.getViewport({ scale: 1 });
    const scaleRatio = cssW / base.width;
    const viewport = page.getViewport({ scale: scaleRatio });
    const ratio = dpr();

    el.width = Math.max(1, Math.floor(viewport.width * ratio));
    el.height = Math.max(1, Math.floor(viewport.height * ratio));
    el.style.width = `${viewport.width}px`;
    el.style.height = `${viewport.height}px`;
    size.value = { w: viewport.width, h: viewport.height };

    if (task) { task.cancel(); task = null; }
    task = page.render({
      canvas: el,
      viewport,
      transform: ratio === 1 ? undefined : [ratio, 0, 0, ratio, 0, 0],
    } as Parameters<typeof page.render>[0]);
    await task.promise;
    task = null;
    if (destroyed) return;

    // 文字层：真文字（可选中/复制），透明覆盖在画布上并对齐字形位置
    const tlHost = textHost.value;
    if (tlHost) {
      tlHost.replaceChildren();
      tlHost.style.setProperty('--total-scale-factor', String(scaleRatio));
      tlHost.style.setProperty('--scale-round-x', '1px');
      tlHost.style.setProperty('--scale-round-y', '1px');
      const textLayer = new pdfjs.TextLayer({
        textContentSource: page.streamTextContent(),
        container: tlHost,
        viewport,
      });
      void textLayer.render();
    }
    syncScale(measuredWidth());
    state.value = 'ready';

    /* 出图期间外框又变了：先调倍率；只有"还在视口附近 + 偏得多"才再出一次，
     * 且放在 `state='ready'` 之后（否则会卡在 loading）。 */
    const nowW = measuredWidth();
    if (nearViewport() && worthRerender(nowW)) {
      renderedFor = '';
      maybeRender();
    }
  } catch (err: unknown) {
    const name = (err as { name?: string })?.name;
    if (name === 'RenderingCancelledException' || destroyed) return;  // 尺寸变化/卸载导致的取消
    state.value = 'error';   // 调用方回退到页图
    renderedFor = '';
  }
}

let observer: IntersectionObserver | null = null;
let bootTimer: ReturnType<typeof setTimeout> | null = null;

/** 标签页从隐藏变可见时补一次（隐藏期间 IO 不会回调） */
function onVisibility() {
  if (document.visibilityState !== 'visible') return;
  if (!nearViewport()) return;
  observer?.disconnect();
  observer = null;
  visible = true;
  if (!size.value) maybeRender();
  else onResize();
}

function observe() {
  const el = host.value;
  el?.setAttribute('data-pdf-page', String(props.page));
  /* ResizeObserver 是尺寸变化的唯一入口：`observe()` 后规范保证会先给一次初始回调。
   * 但注意：**隐藏标签页里 IO 与 RO 都不会回调**（实测 `visibilityState=hidden` 时两者都不触发），
   * 所以下面还配了定时器与 visibilitychange 两道兑底 —— 不能只靠观察器。 */
  if (typeof ResizeObserver !== 'undefined' && el) {
    let lastW = 0;
    resizeObserver = new ResizeObserver(() => {
      const w = el.clientWidth;
      if (w < 40) return;
      if (Math.abs(w - lastW) < 2) return;   // 亚像素抖动直接忽略（真正要不要重出由 onResize 判）
      lastW = w;
      onResize();
    });
    resizeObserver.observe(el);
  }

  // 首屏几页不靠观察器：定时器在隐藏页里也会跑（节流到 ~1s），保证打开就有内容
  if (props.page <= 3) {
    bootTimer = setTimeout(() => { visible = true; maybeRender(); }, 700);
  }
  document.addEventListener('visibilitychange', onVisibility);

  if (typeof IntersectionObserver === 'undefined') { visible = true; maybeRender(); return; }
  observer = new IntersectionObserver((entries) => {
    if (entries.some((e) => e.isIntersecting)) {
      observer?.disconnect();
      observer = null;
      visible = true;
      if (!size.value) maybeRender();
      else onResize();   // 离屏期间宽度可能变过（开合侧栏）→ 靠倍率/重出补齐
    }
  }, { rootMargin: '900px 0px' });   // 提前一屏渲染，滚动时不至于看到空白
  observer.observe(el as Element);
}

/* 容器宽度变化统一走 `onResize()`（先调倍率，必要时才重出）；
 * 这里**不再监听 `props.width`**：它只是期望值，真实几何由 ResizeObserver 报。 */
let resizeObserver: ResizeObserver | null = null;

onMounted(observe);
onBeforeUnmount(() => {
  destroyed = true;
  observer?.disconnect();
  observer = null;
  resizeObserver?.disconnect();
  resizeObserver = null;
  document.removeEventListener('visibilitychange', onVisibility);
  if (bootTimer) clearTimeout(bootTimer);
  if (rerenderTimer) clearTimeout(rerenderTimer);
  task?.cancel();
  task = null;
});
</script>

<template>
  <div ref="host" class="pdf-page">
    <img
      v-if="state === 'error'"
      class="pdf-fallback"
      :src="fallbackUrl"
      :alt="`第 ${props.page} 页`"
      loading="lazy"
    >
    <div v-else class="pdf-box" :style="boxStyle">
      <div class="pdf-surface" :style="surfaceStyle">
        <canvas ref="canvas" class="pdf-canvas" />
        <div ref="textHost" class="pdf-text-layer" />
      </div>
    </div>
    <div v-if="state === 'loading'" class="pdf-hint">正在渲染原 PDF 第 {{ props.page }} 页…</div>
    <div v-else-if="state === 'error'" class="pdf-hint">
      原 PDF 渲染失败，已回退为页图
    </div>
  </div>
</template>

<style scoped>
.pdf-page { width: 100%; }
/* 外框：宽度跟随列宽，高度 = 出图高 × 倍率（由 boxStyle 给）；超出部分裁掉 */
.pdf-box { position: relative; width: 100%; margin: 0 auto; background: #fff; overflow: hidden; }
/* 页面表面：**固定出图尺寸**，缩放交给 transform（画布与文字层同步缩放，对齐不会跑）。
   这样开合侧栏/拖页宽滑杆时**不需要重新光栅化**，零卡顿。 */
.pdf-surface { position: absolute; top: 0; left: 0; transform-origin: top left; will-change: transform; }
.pdf-canvas { display: block; width: 100%; height: 100%; }
.pdf-fallback { display: block; width: 100%; height: auto; }
/* 文字层必须在画布**之上**：canvas 会被浏览器提升为独立合成层，
 * 靠 DOM 顺序不够可靠，给文字层显式 z-index（否则选中底色根本看不到）。 */
.pdf-text-layer { z-index: 1; }

/* pdf.js 文字层样式（按官方 `pdf_viewer.css` 的 `.textLayer` 段改写，只取必需规则）。
 *
 * ★ 关键：字号必须由 pdf.js 给的 `--font-height` 驱动。
 *   `--font-height` 是**页面单位**（实测 ICLR 正文 9.96 ≈ 10pt），要乘上
 *   `--total-scale-factor`（= 当前出图比例）才是屏幕上的字形高度。
 *   以前这里只写了"透明文字 + 定位"、**没写 font-size** → 隐形文字用继承的 13px，
 *   与画布字形不一样大、位置也对不上 → "选中文字与实际文字不对应"（用户报过）。
 *   `--min-font-size` / `--min-font-size-inv` 是 pdf.js 的最小字号机制（它自己会写值），
 *   两者在 transform 里互补，缺一个整体缩放就会偏。 */
.pdf-text-layer {
  position: absolute; inset: 0; overflow: clip; opacity: 1; line-height: 1;
  text-align: initial; text-size-adjust: none; forced-color-adjust: none;
  transform-origin: 0 0; caret-color: CanvasText;
  --min-font-size: 1;
  --text-scale-factor: calc(var(--total-scale-factor) * var(--min-font-size));
  --min-font-size-inv: calc(1 / var(--min-font-size));
}
.pdf-text-layer :deep(span),
.pdf-text-layer :deep(br) {
  color: transparent; position: absolute; white-space: pre;
  cursor: text; transform-origin: 0% 0%; user-select: text;
}
.pdf-text-layer :deep(span:not(.markedContent)),
.pdf-text-layer :deep(.markedContent span:not(.markedContent)) {
  z-index: 1;
  --font-height: 0;
  font-size: calc(var(--text-scale-factor) * var(--font-height));
  --scale-x: 1;
  --rotate: 0deg;
  transform: rotate(var(--rotate)) scaleX(var(--scale-x)) scale(var(--min-font-size-inv));
}
.pdf-text-layer :deep(.markedContent) { display: contents; }
.pdf-text-layer :deep(span[role="img"]) { user-select: none; cursor: default; }
.pdf-text-layer :deep(.endOfContent) {
  display: block; position: absolute; inset: 100% 0 0; z-index: -1;
  cursor: default; user-select: none;
}
/* 选中底色与正文同一令牌（仅换底色，字形仍由画布提供，不做重影） */
.pdf-text-layer :deep(::selection) { background: var(--selection-bg); color: transparent; }
.pdf-text-layer :deep(br::selection) { background: transparent; }

.pdf-hint { margin-top: 6px; font-size: 11.5px; color: var(--text-faint); text-align: center; }
</style>
