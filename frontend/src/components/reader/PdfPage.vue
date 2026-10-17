<script lang="ts">
/* 划词接管的**共享状态**：22 个 PdfPage 实例必须共用一份 —— 跨页拖拽的两端分属不同实例，
 * 各持一份状态的话永远拼不出跨页选区（实测踩到：拖到第二页文字上什么都不发生）。
 * 所以放模块级（<script>），不要放 <script setup>（那里每个实例各一份）。 */
type SelPoint = { node: Node; offset: number };
const textLayers = new Set<HTMLElement>();
/** 本次拖拽的固定端（按下点） */
let dragFrom: SelPoint | null = null;
/** 位移超过阈值后为 true：这段拖拽由我们自己驱动 */
let dragTakenOver = false;
const dragStart = { x: 0, y: 0 };
const dragPoint = { x: 0, y: 0 };
/** 最后一次落在 PDF 真文字上的焦点（空白/译文处的回退目标） */
let lastGood: SelPoint | null = null;
/** 这一轮按下不允许浏览器自己划词（见 onDocPointerDown 里的说明） */
let suppressSelect = false;
/**
 * 『接下来那一下点击不要收掉选区』的计数（触摸长按用）。
 *
 * 触摸长按是阅读器自己造的选区（见 ReaderView 的 fireLongPress）：手指抬起时会来一个
 * pointerup / click，endDrag 会把它当成『点别处』把 PDF 选区收掉 —— 表现就是
 * 『长按后选中焦点闪一下就没了』。所以长按生效时先在这儿挂一次豁免。
 */
let keepSelectionTaps = 0;
export function keepPdfSelectionOnce(): void {
  keepSelectionTaps = Math.max(keepSelectionTaps, 1);
}
let listenerBound = false;
let autoScrollRaf = 0;
/**
 * 这台设备上 pdf.js **已经失败过一次**（平板/手机上很常见：WebAssembly 或
 * OffscreenCanvas 相关能力缺失）。此后各页**直接走页图**，不再每页都白试一遍 ——
 * 否则滚动一页就闪一次白。
 */
let pdfBroken = false;
</script>

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
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import * as pdfjs from 'pdfjs-dist';
import type { PDFDocumentProxy, RenderTask } from 'pdfjs-dist';
import workerUrl from 'pdfjs-dist/build/pdf.worker.min.mjs?url';
import { assetUrl } from '@/api/client';
import { clearUserMarks, markRanges, textNodes } from '@/utils/domMarks';
import type { Highlight } from '@/api/types';

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

const emit = defineEmits<{ (e: 'failed'): void }>();

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
  /**
   * 这篇论文的**全部高亮**（各面混在一起，`scope='pdf'` 的才是这一页要画的）：
   * 偏移量相对**本页文字层的渲染文字**，与正文行/Markdown 视图用的是同一套口径
   * （`utils/domMarks`），但锚点是 `pdf:p<页号>` —— PDF 与 Markdown 是两种页面存储。
   */
  highlights?: Highlight[];
}>();

const host = ref<HTMLDivElement | null>(null);
const canvas = ref<HTMLCanvasElement | null>(null);
const textHost = ref<HTMLDivElement | null>(null);
const state = ref<'idle' | 'loading' | 'ready' | 'error'>('idle');
/** 出图尺寸（CSS px）：**一旦出图就固定**，容器再变宽变窄只改 `scale`（纯 CSS） */
const size = ref<{ w: number; h: number } | null>(null);
/** 显示倍率 = 当前容器宽 / 出图宽。改它不需重新光栅化。 */
const scale = ref(1);
/** 重出图那一帧要**临时关掉** `transform` 过渡：尺寸与倍率是同时换的（见 `render`）。
 *  缩放动画（用户口径 2026-10-10）靠的就是 `.pdf-surface` 上的 `transition: transform`。 */
const noAnim = ref(false);

/** 渲染失败时的兵底：后端那张 150dpi 页图（宁可退回位图，也不要开天窗） */
const fallbackUrl = computed(
  () => assetUrl(`/api/v1/papers/${props.paperId}/pages/${props.page}`),
);

/* ------------------------------ PDF 页图上的高亮（《修改计划》第三步） ------------------------------
 * 锚点 `pdf:p<页号>`（与 Markdown 视图的 `md:p<页号>` 同一个形状，但**不是同一套位置模型**）：
 * 偏移量相对**本页文字层的渲染文字**，所以重渲染同一页能原样落回去。
 * 画法与正文行高亮同一套（`markRanges` 按偏移包 `<mark class="hl">`）：包一层不改变文字长度，
 * 划词偏移量因此不受影响。文字层是 pdf.js **异步**铺出来的，所以渲染完再画、之后每次变都重画。
 */
const myHighlights = computed(() => (props.highlights ?? []).filter(
  (h) => (h.scope ?? 'row') === 'pdf' && h.block_id === `pdf:p${props.page}`,
));

function paintHighlights() {
  const tl = textHost.value;
  if (!tl) return;
  clearUserMarks(tl);
  const marks = myHighlights.value
    .filter((h) => h.end > h.start)
    .map((h) => ({ start: h.start, end: h.end, color: h.color, id: h.id, text: h.text ?? '' }));
  if (marks.length) markRanges(tl, marks);
}

watch(myHighlights, () => { void nextTick(paintHighlights); }, { flush: 'post', deep: true });

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

/* ------------------------------ 划词：PDF 文字层自己接管 ------------------------------
 * 为什么不能交给浏览器（都是真实鼠标拖拽 + 逐事件记录实测出来的）：
 * ① 指针拖进页面空白（没有文本节点的地方）时，Chrome 交回来的 focus 是**文字层容器本身**
 *    （`focusNode = .pdf-text-layer`，offset = "第几个子节点"）：offset 递增时选区从锚点一路铺到层末
 *    （实测 131 字 → 3176 字，矩形从 -212 铺到 766，即整页）；offset 落在 0 时反过来倒长到页首。
 * ② 这个错误状态**会被画出来一帧**（selectionchange 交到手上时，本帧已经画过了）——
 *    所以"事后把焦点拉回来"一定会看到"瞬间整页全选"的闪烁，必须一开始就不让浏览器划词。
 * ③ 本阅读器是"每页 PDF 一栏、旁边紧跟该页译文"，浏览器原生选区跨页时会把中间的**译文**一起圈进去：
 *    复制出来混着非 PDF 内容。
 *
 * ④ **按在页面空白处**（右边距、行间、页脚外的白边）以前根本走不到接管里 —— `textPointAt`
 *    找不到文字就直接放弃、交给浏览器，而浏览器这时把锚点放到**文字层容器**上
 *    （`anchorNode = .pdf-text-layer`，offset = "第几个子节点"）：实测在页面右下空白按下、
 *    往上拖 60px，选区从**页脚**倒长到**页首**（2393 字 = 整页，用户报的"选中到空白区域
 *    会反过来选中本页上面的所有内容"）；往右下拖 60px 时选区又和拖过的范围对不上（304 字）。
 *    现在空白处按下也接管：锚点取**层里离按下点最近的一处文字**（见 `nearestTextPoint`）。
 * ⑤ 拖拽**已经选中的文字**会触发浏览器的原生拖放 —— 我们自己在移动中放选区，按下点必然落在
 *    这个选区**里面**，于是接着拖就被浏览器当成"搬走这段文字"：Edge 上直接是"拖动字符块"
 *    开一个拖放搜索（用户报的"从上往下复制会被识别为拖动字符块搜索文本"）。而且原生拖放一旦
 *    起来 `pointermove` 就断了（实测 14 次移动只收到 1 次），①②④那套接管当场全部失效。
 *    解法是**在按下时就压掉 `mousedown` 的默认动作**（Blink 只在"按下没被取消"时才记成可能拖放，
 *    见 `onDocMouseDown`）；`dragstart` 那道只是兜底，单独用它反而会把拖放拦成原生划词。
 * ⑥ 挡掉原生划词的副作用：**"按在别处 → 上一次的选区收掉"这个默认动作也一起没了** ——
 *    选完 PDF 再点别的文字（同一页另一处、第 2 页、译文列的文字）选区都还在
 *    （用户报的"选中 pdf 后点击其他地方应该取消选中"；真鼠标实测：点别处文字后仍是 196 字不散）。
 *    所以"只点没拖"的那一下松手时自己补上（见 `endDrag` + `dropPdfSelection` ②）。
 *
 * 做法：按下时若落在 PDF 文字层里（**包括层上的空白**），就接管这次拖拽 ——
 *   · `selectstart` 里 `preventDefault` 挡掉浏览器自己的划词（从按下那一刻就挡：
 *     它"一开始划"就把焦点给了层容器，事后修一定会闪一帧；双击/三击 detail ≥ 2 放行，选词/选行照旧）；
 *   · 按下时压掉 `mousedown` 的默认动作 + `dragstart` 兜底：文字层里不起原生拖放（见 ⑤）；
 *   · 焦点自己用 `caretRangeFromPoint` 算，**只接受"某个 PDF 文字层里的文本节点"**
 *     （页内点不到就上下探几个像素找最近一行，再不够就取层里最近的一处文字 —— 见 `resolvePoint`）：
 *     落在页面空白或右侧译文上，就停在**最后一次有效位置**（不膨胀、也不圈进非 PDF 内容）；
 *     跨页则落到那一页的文字上，照常选；
 *   · 拖拽期间给 `<html>` 加 `pm-pdf-drag`（见 applySelection 的说明）：跨页时中间的译文行
 *     不进选区也不进剪贴板；拖到容器上下边缘还会自己滚动，方便跨页选；
 *   · "只点没拖"的那一下松手时，自己补上"收掉上一次选区"（见 ⑥）。
 *
 * 状态声明在文件顶部的模块级 `<script>` 里（必须多个实例共享，否则跨页两端凑不到一起）。
 */

/** 节点所在的文字层（不在任何文字层里 → null） */
function layerOf(node: Node | null): HTMLElement | null {
  if (!node) return null;
  const el = node.nodeType === Node.TEXT_NODE ? node.parentElement : (node as HTMLElement);
  const tl = (el?.closest?.('.pdf-text-layer') ?? null) as HTMLElement | null;
  return tl && textLayers.has(tl) ? tl : null;
}

/** 命中点处的"PDF 文字层里的文本节点"；元素容器、译文、页面外一律算无效。
 *
 * 注意：文字层的 span 是**绝对定位**的，层自己没有行盒 —— 点在行间、词间的空白上时
 * `caretRangeFromPoint` 会交回**层容器**（这也是原生划词会"整页闪一下"的根源）。
 * 所以上下一路探几个点，取最近的文字位置；探不到（真空句、插图区）就返回 null，
 * 由调用方停在最后一次有效位置。 */
function textPointAt(x: number, y: number): SelPoint | null {
  const at = (px: number, py: number): SelPoint | null => {
    const c = document.caretRangeFromPoint?.(px, py);
    const n = c?.startContainer ?? null;
    if (!n || n.nodeType !== Node.TEXT_NODE || !layerOf(n)) return null;
    return { node: n, offset: c!.startOffset };
  };
  const exact = at(x, y);
  if (exact) return exact;
  const NEAR = [2, 4, 7, 11, 16, 22];
  for (const d of NEAR) {
    const up = at(x, y - d);
    if (up) return up;
    const down = at(x, y + d);
    if (down) return down;
  }
  return null;
}

/** 按下点落在哪个文字层里（**含层上的空白**：那里只有容器，没有文本节点） */
function layerAtPoint(x: number, y: number): HTMLElement | null {
  const el = document.elementFromPoint?.(x, y) as HTMLElement | null;
  const tl = (el?.closest?.('.pdf-text-layer') ?? null) as HTMLElement | null;
  return tl && textLayers.has(tl) ? tl : null;
}

/**
 * 层里离 (x, y) 最近的一处文字位置（按下点落在页面空白时的锚点）。
 *
 * 为什么不能省：交给浏览器的话，空白处按下的锚点是**层容器**，选区按"第几个子节点"整段铺开
 * （见文件上方 ④ 的实测数字）。
 *
 * 取法分两步、**先看行再看列**：先按"离哪一行最近"（`dy` 分 2px 一档 —— 同一行的 span 顶边
 * 常差一两像素），同档再按横向距离挑那行最近的一段文字。
 * ⚠️ 不能直接拿"到 span 外接框的欧氏距离"当唯一判据（踩过）：右边距里的点横向很远，会让**上面
 * 某条通栏行**因为"横向更近"而胜出，锚点与焦点于是落到同一段文字上 —— 实测真拖 60px 选出来 0 字。
 * 偏移量按"按下点在框的哪半边"落在文字**头**还是**尾**，与在正文里点空白处再拖的手感一致。
 */
function nearestTextPoint(layer: HTMLElement, x: number, y: number): SelPoint | null {
  let best: { node: Text; offset: number; row: number; dx: number } | null = null;
  for (const node of textNodes(layer)) {
    const host = node.parentElement;
    if (!host) continue;
    const r = host.getBoundingClientRect();
    if (!r.width || !r.height) continue;
    const dy = Math.max(r.top - y, 0, y - r.bottom);
    const dx = Math.max(r.left - x, 0, x - r.right);
    const row = Math.round(dy / 2);
    if (best && (row > best.row || (row === best.row && dx >= best.dx))) continue;
    best = { node, offset: x > (r.left + r.right) / 2 ? (node.nodeValue ?? '').length : 0, row, dx };
  }
  return best ? { node: best.node, offset: best.offset } : null;
}

/** 把 (x, y) 解析成"层里的某个文字位置"：先按上面那套逐点探（行内、行间的空白靠它），
 *  探不到再退回**最近的一处文字**（右边距、页脚外的白边离文字很远，只探同一列的上下够不着）。
 *  页面之外（拖出纸面）解析不出来 —— 那时调用方停在最后一次有效位置，选区不跟着往外长。 */
function resolvePoint(x: number, y: number): SelPoint | null {
  const p = textPointAt(x, y);
  if (p) return p;
  const layer = layerAtPoint(x, y);
  return layer ? nearestTextPoint(layer, x, y) : null;
}

/** 把选区放到两点之间。同页、跨页都只是一段 range ——
 *  Chrome 的 Selection 实际只保留一段（`addRange` 第二段会合并成一段，实测），
 *  所以"跨页不圈进中间的译文"不是靠多段选区，而是靠拖拽期间给非 PDF 内容加 `user-select:none`
 *  （见 `pm-pdf-drag`）：Chrome 会把不可选的内容从选区里跳过，`toString()` / 复制都不会带上（实测 4984 → 3631 字）。 */
function applySelection(a: SelPoint, b: SelPoint): void {
  const sel = document.getSelection();
  if (!sel) return;
  if (!layerOf(a.node) || !layerOf(b.node)) return;
  try { sel.setBaseAndExtent(a.node, a.offset, b.node, b.offset); }
  catch { /* 层刚好被重渲染掉了就算了，别影响阅读 */ }
}

/** 拖拽期间给 `<html>` 加这个类：非 PDF 内容不参与本次选区（见 applySelection 的说明） */
function setDragClass(on: boolean): void {
  document.documentElement.classList.toggle('pm-pdf-drag', on);
}

/**
 * 这次划词是不是**在 PDF 里划的**（决定要不要挂 `pm-pdf-drag`）。
 *
 * ⚠️ 判据必须是"**锚点**落在 PDF 文字层里"，**不能**用"选区与文字层相交"：
 * 对照 + 左栏原 PDF 的 DOM 顺序是 `PDF(1) rows(1) PDF(2) rows(2) …` —— 在右侧译文列里
 * **跨页**往下拖时，那段连续选区必然与**下一页的 PDF 文字层**相交。用相交判定就会把
 * `pm-pdf-drag` 误挂上，而它的 CSS 会把**整个正文文字列**设成 `user-select: none`，
 * Chrome 于是把译文文字从选区里剔除：选区长度与复制内容都断在页边界上
 * （用户报的"对照时右侧译文不能跨页选中"；实测选区长度冻在同一值、`rowsUserSelect=none`）。
 * 锚点在 PDF 里才是"这次拖拽只算 PDF 文字"的本意。
 */
function selectionOnPdf(sel: Selection | null): boolean {
  if (!sel || !sel.rangeCount || sel.isCollapsed) return false;
  return !!layerOf(sel.anchorNode);
}

/**
 * 在**非 PDF 内容**（译文列 / 原文列 / 工具栏）上按下时：把上一次的 PDF 选区连同
 * `pm-pdf-drag` 一起收掉。
 *
 * 不做这一步的后果（用户报过）：在 PDF 里选完一段，那段选区还罩着 → `pm-pdf-drag` 还挂着
 * → 非 PDF 内容一直是 `user-select: none`；而 Chrome **不允许在不可选的内容上开始新选区**，
 * 于是在译文里怎么拖都选不中（"选了 PDF 之后译文就选不了了"）。
 * 只有"按下点**没落在 PDF 文字上**"才收 —— PDF 之外、或页面空白（那时接管会另立锚点，见 ④）：
 * 按在 PDF 文字上时这个类正是我们要的（跨页选区不圈进中间的译文）。
 */
function clearPdfSelection(): void {
  dropPdfSelection();
  setDragClass(false);
}

/**
 * 收掉**落在 PDF 上的**选区。
 *
 * 两个地方要用，理由不同：
 * ① 在非 PDF 内容上按下时（见 `clearPdfSelection`）：上一次的 PDF 选区会让 `pm-pdf-drag` 一直挂着，
 *    译文那侧就一直是 `user-select: none`，新的划词压根开始不了；
 * ② 我们挡掉原生划词的**那一下"点击"**松手时（见 `endDrag`）：浏览器本来会在按下时把上一次的选区
 *    收掉（再普通不过的动作），被我们挡住之后没人补这一步 —— 表现就是"选完 PDF 再点别处的文字，
 *    上一次的选区还在"（用户报过；真鼠标实测：点同一页另一处文字、点第 2 页的文字，选区都还剩 196 字）。
 */
function dropPdfSelection(): void {
  const sel = document.getSelection();
  if (sel && selectionOnPdf(sel)) sel.removeAllRanges();
}

/** 离文字层最近的滚动容器（拖到边缘时自己滚） */
function scrollerOf(el: HTMLElement): HTMLElement | null {
  for (let n = el.parentElement; n; n = n.parentElement) {
    if (n.scrollHeight > n.clientHeight + 4 && /auto|scroll/.test(getComputedStyle(n).overflowY)) return n;
  }
  return null;
}

/** 用当前拖拽点更新选区（空白处停在 lastGood，不膨胀） */
function extendDragTo(x: number, y: number): void {
  if (!dragFrom) return;
  const p = resolvePoint(x, y);
  if (p) lastGood = p;
  if (lastGood) applySelection(dragFrom, lastGood);
}

function autoScrollTick(): void {
  if (!dragFrom || !dragTakenOver) { autoScrollRaf = 0; return; }
  const tl = layerOf(lastGood?.node ?? null) ?? layerOf(dragFrom.node);
  const sc = tl ? scrollerOf(tl) : null;
  if (sc) {
    const r = sc.getBoundingClientRect();
    const EDGE = 48;
    let dy = 0;
    if (dragPoint.y < r.top + EDGE) dy = -14;
    else if (dragPoint.y > r.bottom - EDGE) dy = 14;
    if (dy) {
      const before = sc.scrollTop;
      sc.scrollTop = before + dy;
      if (sc.scrollTop !== before) extendDragTo(dragPoint.x, dragPoint.y);
    }
  }
  autoScrollRaf = requestAnimationFrame(autoScrollTick);
}

function onDocPointerDown(ev: PointerEvent): void {
  dragFrom = null;
  dragTakenOver = false;
  suppressSelect = false;
  lastGood = null;
  if (ev.button !== 0) return;
  const p = textPointAt(ev.clientX, ev.clientY);
  /* 按在页面的**空白**上（右边距、行间、页脚外的白边）也要接管：交给浏览器的话锚点会落到
   * 文字层**容器**上，选区按"第几个子节点"整段铺开（见文件上方 ④ 的实测）。
   * 锚点取层里最近的一处文字 —— 拖过去才会按"拖过的范围"长出来。 */
  const anchor = p ?? resolvePoint(ev.clientX, ev.clientY);
  // 按在非 PDF 内容上：先把上一次的 PDF 选区与 `pm-pdf-drag` 收掉，否则译文那侧一直是
  // `user-select: none`，新的划词压根开始不了（详见 clearPdfSelection 的说明）。
  if (!anchor) { clearPdfSelection(); return; }
  // 空白处按下：上一次的选区也先收掉（同上），本次接管随后自己把类挂回来。
  if (!p) clearPdfSelection();
  dragFrom = anchor;
  lastGood = anchor;
  dragStart.x = dragPoint.x = ev.clientX;
  dragStart.y = dragPoint.y = ev.clientY;
  /* **从按下这一刻就**不让浏览器自己划词：它"一开始划"就先把焦点给了层容器（整页闪一下），
   * 等 selectionchange 交到手上时本帧已经画过了 —— 事后修一定会看到闪烁。
   * 双击/三击（detail ≥ 2，选词/选行）放行，保持原生手感；空白处没有"词/行"可选，一律挡掉。 */
  suppressSelect = ev.detail < 2 || !p;
  setDragClass(true);            // 本次拖拽的选区只算 PDF 文字（见 applySelection 的说明）
}

/**
 * 由我们把这次拖拽接管时，压掉 **`mousedown` 的默认动作**（见文件上方 ⑤）。
 *
 * 这一步不是"顺手多挡一层"，而是**唯一**能让原生拖放不起来的地方：
 * Blink 是在**按下没有被取消**时才把这次按下记成"可能开始拖放"，于是移动时才会去搬那段选中的文字。
 * 只在 `dragstart` 上 `preventDefault` 是不够的 —— 实测那把拖放拦掉之后，手势被交回**原生划词**，
 * 锚点重新落到层容器上，选区反而从 120 字涨到 2247 字（页首到页脚）。
 * 取消 `mousedown` 不会影响 `click`（菜单/按钮照常），也不会影响我们自己的 pointer 事件。
 */
function onDocMouseDown(ev: MouseEvent): void {
  if (suppressSelect) ev.preventDefault();
}

function onDocPointerMove(ev: PointerEvent): void {
  if (!dragFrom || !(ev.buttons & 1)) return;
  dragPoint.x = ev.clientX;
  dragPoint.y = ev.clientY;
  if (!dragTakenOver) {
    if (Math.abs(ev.clientX - dragStart.x) < 3 && Math.abs(ev.clientY - dragStart.y) < 3) return;
    dragTakenOver = true;                       // 从这一刻起挡掉原生划词
    if (!autoScrollRaf) autoScrollRaf = requestAnimationFrame(autoScrollTick);
  }
  extendDragTo(ev.clientX, ev.clientY);
}

function onSelectStart(ev: Event): void {
  if (suppressSelect) ev.preventDefault();      // 这次拖拽由我们自己驱动
}

/**
 * 文字层里的原生拖放**兜底**拦一道（主拦在 `onDocMouseDown`，见文件上方 ⑤）。
 *
 * 正常情况下根本不会走到这里（按下的默认动作已被压掉）。真走到这里说明那次按下是放行的
 * （双击/三击那种原生路径）—— 此时宁可把拖放拦掉：拖起来只会变成 Edge 的拖放搜索，
 * 而且 `pointermove` 一断，接管就全废了。判据取"拖的来源"与"选区锚点"两处，
 * 覆盖 `dragstart` 的 target 被交成外层元素的情况。
 */
function onDocDragStart(ev: Event): void {
  const sel = document.getSelection();
  if (layerOf(ev.target as Node) || layerOf(sel?.anchorNode ?? null)) ev.preventDefault();
}

function endDrag(): void {
  /* 这一下只是"点击"（没拖）且原生划词被我们挡掉了 → 把浏览器那个默认动作补上：
   * 按下另一个地方本来就该收掉上一次的选区（见 `dropPdfSelection` ②）。
   * 双击/三击（`suppressSelect === false`）放行的原生选区不能碰。 */
  const clicked = !dragTakenOver && suppressSelect;
  dragFrom = null;
  dragTakenOver = false;
  suppressSelect = false;
  if (clicked) {
    // 长按造出来的选区：抬起手指那一下不收（见 keepSelectionTaps 的说明）
    if (keepSelectionTaps > 0) keepSelectionTaps -= 1;
    else dropPdfSelection();
  }
  // 注意：**不能**在这里无条件摘掉 pm-pdf-drag —— 松手后选区还在（跨页那段 range 依旧罩着中间的译文），
  // 摘掉它会让 `toString()` / 复制立刻又把译文算回来（实测 2106 → 3400 字）。
  // 交给 selectionchange 按"选区还在不在 PDF 上"决定。
  keepDragClass(document.getSelection());
  if (autoScrollRaf) { cancelAnimationFrame(autoScrollRaf); autoScrollRaf = 0; }
}

/** 选区只要还盖在某个 PDF 文字层上（且非空），就保持"非 PDF 内容不可选" */
function keepDragClass(sel: Selection | null): void {
  setDragClass(selectionOnPdf(sel));
}

/**
 * 兜底（不参与接管的那几条路径：键盘 Shift+方向键、双击选词、程序化选区）：
 * 焦点一旦被浏览器丢给"元素"（容器）或者跑到 PDF 文字之外，就拉回最后一次有效位置。
 * 拖拽期间我们自己已经把选区放好了，这里不插手。
 */
function onDocSelectionChange(): void {
  const cur = document.getSelection();
  if (!dragTakenOver) keepDragClass(cur);   // 选区离开 PDF 就收掉（见 endDrag 的说明）
  if (dragTakenOver) return;
  if (!cur || cur.rangeCount !== 1 || !cur.anchorNode || !cur.focusNode) return;
  if (!layerOf(cur.anchorNode) || cur.anchorNode.nodeType !== Node.TEXT_NODE) return;
  if (cur.focusNode.nodeType === Node.TEXT_NODE && layerOf(cur.focusNode)) {
    lastGood = { node: cur.focusNode, offset: cur.focusOffset };
    return;
  }
  if (!lastGood) return;
  try { cur.setBaseAndExtent(cur.anchorNode, cur.anchorOffset, lastGood.node, lastGood.offset); }
  catch { /* 拉不回来就算了，别影响阅读 */ }
}

function registerTextLayer(tl: HTMLElement): void {
  textLayers.add(tl);
  if (listenerBound) return;
  document.addEventListener('selectionchange', onDocSelectionChange);
  document.addEventListener('selectstart', onSelectStart, true);
  document.addEventListener('mousedown', onDocMouseDown, true);
  document.addEventListener('dragstart', onDocDragStart, true);
  document.addEventListener('pointerdown', onDocPointerDown, true);
  document.addEventListener('pointermove', onDocPointerMove, true);
  document.addEventListener('pointerup', endDrag, true);
  document.addEventListener('pointercancel', endDrag, true);
  window.addEventListener('blur', endDrag);
  listenerBound = true;
}

/** 页面组件卸载时把它那层摘掉（跨页选区里若还有它的节点，交给 applySelection 的 try/catch 兜） */
function unregisterTextLayer(tl: HTMLElement): void {
  textLayers.delete(tl);
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

/** 重出一次的成本 = 重新光栅化 + 重建文字层；只有倍率偏到肉眼可见才值得。
 *
 * 阈值 2026-10-10 从 0.15 收到 **0.08**：用户口径是"**复刻 pdf.js 的缩放逻辑**"——
 * pdf.js 是 `--total-scale-factor` 变了就按新倍率重绘（所以放大后依然清晰），
 * 而旧阈值下点一档放大（×1.25）只走了纯 CSS `scale`，canvas 还是旧分辨率 → 一放大就糊。 */
function worthRerender(nowW: number): boolean {
  const s = size.value;
  if (!s || !s.w) return true;
  return Math.abs(nowW / s.w - 1) > 0.08;
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
  if (pdfBroken) { state.value = 'error'; return; }   // 这台设备渲染不了：直接给页图
  void render();
}

/**
 * 容器尺寸变了（开/合侧栏、窗口缩放、页宽滑杆）：
 * 1. **先只改 `scale`** —— 立即生效、不花 CPU；
 * 2. 只有在"这一页还在视口附近"且"倍率已偏得肉眼可见（>8%）"时，
 *    才防抖 **220ms** 后补一次重出（保证清晰度，但不会一开侧栏就把全篇重画）。
 *    220ms 而不是 450ms（2026-10-10）：档位放大是一下一下点的，等太久会觉得"卡了一下才变清"。
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
  rerenderTimer = setTimeout(() => { renderedFor = ''; maybeRender(); }, 220);
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

    /* ⚠️ **先画到离屏画布、画完再贴上去**（用户口径 2026-10-10："最后一帧闪烁还在"）：
       直接在可见画布上出图时，`canvas.width = …` 会**立刻把它清空** ——
       从"清空"到"画完"之间那一帧就是闪的那一下（重出越勤越明显）。
       离屏出图期间旧的那张图一直挂在屏幕上，所以看不到空窗。 */
    const buf = document.createElement('canvas');
    buf.width = Math.max(1, Math.floor(viewport.width * ratio));
    buf.height = Math.max(1, Math.floor(viewport.height * ratio));

    if (task) { task.cancel(); task = null; }
    task = page.render({
      canvas: buf,
      viewport,
      transform: ratio === 1 ? undefined : [ratio, 0, 0, ratio, 0, 0],
    } as Parameters<typeof page.render>[0]);
    await task.promise;
    task = null;
    if (destroyed) return;

    /* ---- 先把**新文字层**在游离节点里铺好（这一步要跨好几帧，期间屏幕上是旧的那一张）----
       文字层：真文字（可选中/复制），透明覆盖在画布上并对齐字形位置。
       ⚠️ 原来这里是"先换画布 → 再 await 铺文字层"：位图与文字层**分处两帧**，中间那几帧
       图是新的、字是旧的，位置对不上；开侧栏时视口附近多页同时重出，叠起来就是肉眼可见的闪。
       现在铺好之后再和位图**同一帧**换上去（见下）。
       `--scale-factor` 那类由 pdf.js 写在容器上的行内样式要**整体搬过来**，否则字号的公式断了。 */
    const tlHost = textHost.value;
    let nextTL: HTMLDivElement | null = null;
    if (tlHost) {
      const next = document.createElement('div');
      next.className = tlHost.className;
      next.style.cssText = tlHost.style.cssText;
      next.style.setProperty('--total-scale-factor', String(scaleRatio));
      next.style.setProperty('--scale-round-x', '1px');
      next.style.setProperty('--scale-round-y', '1px');
      const textLayer = new pdfjs.TextLayer({
        textContentSource: page.streamTextContent(),
        container: next,
        viewport,
      });
      await textLayer.render();
      if (destroyed) return;
      nextTL = next;
    }

    /* ---- 换片：出图宽 / 倍率 / 位图 / 文字层，全在**同一帧**里换完 ----
       ⚠️ 这一帧必须**临时关掉过渡**（`no-anim`）：出图宽（canvas 的 CSS 宽）是瞬变的，
       而 `transform: scale` 若还带 0.16s 补间，视觉宽 = 新出图宽 × 旧倍率 ——
       会先跳一下、再缩回来，那一跳就是用户说的"最后一帧闪烁"。
       同一帧里一起换：视觉宽 = 新宽 × 1 = 旧宽 × 旧倍率，屏幕上完全不动。
       `no-anim` 的 class 生效要走一次 `nextTick`，所以是"先立 → 改 → 再撤"。 */
    noAnim.value = true;
    await nextTick();
    el.width = buf.width;
    el.height = buf.height;
    el.style.width = `${viewport.width}px`;
    el.style.height = `${viewport.height}px`;
    el.getContext('2d')?.drawImage(buf, 0, 0);
    size.value = { w: viewport.width, h: viewport.height };
    if (tlHost && nextTL) {
      tlHost.setAttribute('style', nextTL.getAttribute('style') || '');
      tlHost.replaceChildren(...Array.from(nextTL.childNodes));
    }
    syncScale(measuredWidth());   // 倍率也在这同一帧归位（原来在函数尾部，会晚一帧）
    await nextTick();
    noAnim.value = false;

    if (tlHost) {
      // 文字层铺完才画高亮：`markRanges` 按"文字层渲染文字里的偏移量"包 `<mark>`，
      // 层还没铺完时一个节点都找不到（画出来是空的）。
      paintHighlights();
      // 划词由我们自己接管（见文件上方说明：空白处会闪整页、跨页会圈进译文）
      registerTextLayer(tlHost);
    }
    state.value = 'ready';

    /* 出图期间外框又变了：先调倍率；只有"还在视口附近 + 偏得多"才再出一次，
     * 且放在 `state='ready'` 之后（否则会卡在 loading）。 */
    const nowW = measuredWidth();
    if (nearViewport() && worthRerender(nowW)) {
      renderedFor = '';
      maybeRender();
    }
  } catch (err: unknown) {
    /* 半路抛错时 `no-anim` 可能还立着（换片那一段的中途）—— 不复位的话这一页之后所有缩放
       都没有补间，看起来"变生硬了"，还查不出原因。 */
    noAnim.value = false;
    const name = (err as { name?: string })?.name;
    if (name === 'RenderingCancelledException' || destroyed) return;  // 尺寸变化/卸载导致的取消
    state.value = 'error';   // 调用方回退到页图
    renderedFor = '';
    pdfBroken = true;
    emit('failed');
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
  const tl = textHost.value;
  if (tl) unregisterTextLayer(tl);
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
      <div class="pdf-surface" :class="{ 'no-anim': noAnim }" :style="surfaceStyle">
        <canvas ref="canvas" class="pdf-canvas" />
        <div ref="textHost" class="pdf-text-layer" />
      </div>
    </div>
    <div v-if="state === 'loading' && !size" class="pdf-hint">正在渲染原 PDF 第 {{ props.page }} 页…</div>
  </div>
</template>

<style scoped>
.pdf-page { width: 100%; }
/* 外框：宽度跟随列宽，高度 = 出图高 × 倍率（由 boxStyle 给）；超出部分裁掉 */
.pdf-box { position: relative; width: 100%; margin: 0 auto; background: #fff; overflow: hidden; }
/* 页面表面：**固定出图尺寸**，缩放交给 transform（画布与文字层同步缩放，对齐不会跑）。
   这样开合侧栏/拖页宽滑杆时**不需要重新光栅化**，零卡顿。 */
.pdf-surface { position: absolute; top: 0; left: 0; transform-origin: top left; will-change: transform;
  /* 缩放补间（用户口径 2026-10-10）：**始终开着**。
     ⚠️ 不要为了"重绘那一帧"把它中途关掉 —— 那正是"最后一帧闪烁"的成因：
     关掉的那一帧，出图宽与倍率是**瞬时**换的，屏幕上就是一下跳变；
     始终开着，它们被补间成连续位移，就看不到跳了（代价是过渡期间位图被重采样，
     用户已明确表示这个比跳变好接受）。 */
  transition: transform .16s var(--ease); }
/* `no-anim`：**换片那一帧**用它把过渡关掉（用户口径 2026-10-10："开侧栏时 PDF 最后一帧闪烁"）。
   位图的出图宽与倍率必须同一帧换完，中间不能有补间 —— 否则视觉宽会先跳再缩回来。 */
.pdf-surface.no-anim { transition: none; }
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
/* 文字层里**不许拖放**（`dragstart` 那道是 JS 兜底，这里是让浏览器压根别起拖）：
 * 拖起选中的文字只会在 Edge 上开一个"拖放搜索"，见文件上方 ⑤。 */
.pdf-text-layer,
.pdf-text-layer :deep(*) {
  -webkit-user-drag: none;
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
/* "拖出文字区"的历史遗迹已删：占位元素（.endOfContent）不再需要 ——
 * 划词改由文字层自己驱动，焦点只接受真文字，跨页按多段选区拼（详见文件上方说明）。 */
/* 选中底色与正文同一令牌（仅换底色，字形仍由画布提供，不做重影） */
.pdf-text-layer :deep(::selection) { background: var(--selection-bg); color: transparent; }
.pdf-text-layer :deep(br::selection) { background: transparent; }
/* ⚠️ **高亮叠加层只准画底色，不准画文字**（用户报"pdf 添加高亮的叠加层显示文字的问题"）：
 * `<mark>` 的浏览器默认样式是 `background: Mark; color: MarkText` —— 它**显式**给了黑字，
 * 不会继承父 `span` 的 `color: transparent`，于是叠加层把 DOM 文字也渲染了一遍，
 * 与画布字形错半个像素叠在一起 = 重影。这里在这一层强制透明（`base.css` 里
 * `mark.hl { color: inherit }` 是同一件事的第一道），并去掉 `mark.hl` 的 1px 内边距 ——
 * 那 1px 会把底色画到字形框外面，看着像高亮"胖出来一点"。 */
.pdf-text-layer :deep(mark),
.pdf-text-layer :deep(mark *) {
  color: transparent; -webkit-text-fill-color: transparent;
}
.pdf-text-layer :deep(mark.hl) { padding: 0; border-radius: 1px; }

.pdf-hint { margin-top: 6px; font-size: 11.5px; color: var(--text-faint); text-align: center; }
</style>
