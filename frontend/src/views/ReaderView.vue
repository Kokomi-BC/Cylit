<script setup lang="ts">
/**
 * 阅读器（规划 §22 /reader/:paperId）—— 前端主干。
 *
 * 视图：原文 / 译文 / **对照**（默认）。对照有两个形态，都由**一个滚动容器**渲染，
 * 因此左右永远对齐：
 *  - `左栏=原文文本`（默认）：一行一对原文/译文（页级翻译同一次调用产出，天然逐段对齐）；
 *  - `左栏=原 PDF`：每页的页图贴在**该页译文左侧**（页内 sticky），按页对齐。
 *
 * 目录：中英双语（英文在上、中文在下）。
 * LiveUpdater：SSE `page_translated` → 只替换那一页的行；断线降级 3s 轮询。
 * 快捷键：1/2/3 切视图 · o 换左右 · p 切原文文本/原 PDF · j/k 上下段 · / 搜索 · Esc 关浮层。
 */
import { computed, nextTick, onMounted, onUnmounted, reactive, ref, watch } from 'vue';
import { RouterLink, useRoute, useRouter } from 'vue-router';
import { api, assetUrl, downloadBlob, sseUrl } from '@/api/client';
import { postSse } from '@/api/stream';
import type {
  AgentCitation, AgentSelection, AgentStep, Annotation, Block, BriefPayload, FigureItem, GlossaryTerm,
  Highlight, MarkdownFormula, MarkdownViewMode, Note, OriginalMode, Outline, PageInfo, PageRow,
  PageTranslation, PairOrder, Paper, PaperReference, ReaderPayload, ReaderStats, TaskInfo,
  TranslatedMarkdownPage, ViewMode, ZoomTarget,
} from '@/api/types';
import { askConfirm } from '@/stores/confirm';
import { useEventStream, type StreamStatus } from '@/composables/useEventStream';
// 解析完成后要**强制**重取解读（`useUnderstanding` 是按论文缓存的整包，TTL 30s）
import { loadUnderstanding } from '@/composables/useUnderstanding';
import { useHotkeys } from '@/composables/useHotkeys';
import { htmlWithHighlights, mdToHtml } from '@/composables/useMarkdown';
import { markRanges, rangeOffsets, renderedText, segments } from '@/utils/domMarks';
import { buildPageGroups, flattenRows } from '@/composables/usePageRows';
import { taskTypeLabel } from '@/utils/tasks';
import { confirmRegenerateInsight } from '@/utils/understanding';
import { READER_TEXT_SIZES, type ReaderTextSize } from '@/utils/readerFont';
import { addReaderSeconds, noteReaderOpen } from '@/utils/readerStats';
import { reportError, toast, withLoading } from '@/stores/app';
import { openTab, routeKey, setTabTitle } from '@/stores/tabs';
import {
  INK_COLORS, activeInkColor, canRedo, canUndo, clearPageInk,
  flushInk, inkKey, inkOn, inkTool, inkView, inkWidth, pageKey,
  redoInk, resetInk, setInkColor, setInkNotesListener, undoInk,
} from '@/composables/useReaderInk';
import { WIDTH_STEPS } from '@/utils/imageInk';
import {
  copyText, debounce, flattenOutline, fmtRelative, outlineParentIds, pct, saveBlob, truncate,
} from '@/utils/format';
import AppIcon from '@/components/AppIcon.vue';
import Modal from '@/components/Modal.vue';
import Menu from '@/components/Menu.vue';
import ReaderPageRows from '@/components/reader/ReaderPageRows.vue';
import AsideToggle from '@/components/reader/AsideToggle.vue';
import CapPanel from '@/components/reader/CapPanel.vue';
import ProgressBar from '@/components/ProgressBar.vue';
import ReaderSearch, { type SearchMode, type SearchScope } from '@/components/reader/ReaderSearch.vue';
import ReaderChat from '@/components/reader/ReaderChat.vue';
import ReaderSidePanel, {
  type BriefDraft, type NoteDraftPayload, type SideTab,
} from '@/components/reader/ReaderSidePanel.vue';
import ReaderToc from '@/components/reader/ReaderToc.vue';
import ReaderPages from '@/components/reader/ReaderPages.vue';
import FloatingCard from '@/components/reader/FloatingCard.vue';
import ReferenceList from '@/components/reader/ReferenceList.vue';
import ImageViewer from '@/components/reader/ImageViewer.vue';
import M3Slider from '@/components/M3Slider.vue';
import SelectionMenu from '@/components/reader/SelectionMenu.vue';
import ContextMenu from '@/components/ContextMenu.vue';

const HIGHLIGHT_COLORS = ['red', 'orange', 'yellow', 'green', 'blue', 'indigo', 'violet'];
const route = useRoute();
const router = useRouter();
const paperId = computed(() => String(route.params.paperId ?? ''));

/**
 * **本实例**对应的论文 id：挂载时固定。
 *
 * 组件按 `RouterView :key`（路由名 + 参数）重建，所以一个实例只服务一篇论文；
 * 而 `paperId` 是路由的 computed —— 切论文时**路由先变、旧实例后卸载**，
 * 在卸载 / 防抖回调里读它就已经是新论文了，会把上一篇的滚动位置写进新论文的进度行
 * （`onUnmounted` 里那次 `saveProgressNow()` 正好踩这个点）。异步落盘一律用这个常量。
 */
const instancePaperId = String(route.params.paperId ?? '');

/* ------------------------------ 数据面 ------------------------------ */
const reader = ref<ReaderPayload | null>(null);
const blocks = ref<Block[]>([]);
const pages = ref<PageInfo[]>([]);
const pageTranslations = ref<PageTranslation[]>([]);
const annotations = ref<Annotation[]>([]);
const highlights = ref<Highlight[]>([]);
const notes = ref<Note[]>([]);
/** 「引用文献」页签的数据：**按需取**（`GET /papers/{id}/references`）——
 *  读者只是点了个引用标号，不该顺带把整份元数据拉过来（见 `ensureReferences`）。 */
const paperReferences = ref<PaperReference[]>([]);
const tasks = ref<TaskInfo[]>([]);
const stats = ref<ReaderStats>({
  blocks: 0, by_type: {}, pages: 0, pages_done: 0, rows: 0,
  translated: 0, translate_total: 0, translate_percent: 0,
});
const formulas = ref<MarkdownFormula[]>([]);
const formulasLoading = ref(false);
/** 这批公式的来源：`markdown`（译文）/ `parse`（解析产物，还没翻译）/ `mixed`（半翻译，按页兜底）。
 *  只用来把页签标题写准 —— 用户要能一眼看出"这些公式是从译文来的还是从解析产物来的"。 */
const formulasSource = ref<'markdown' | 'parse' | 'mixed' | string>('markdown');
const figures = ref<FigureItem[]>([]);
const brief = ref<BriefPayload | null>(null);
const glossary = ref<GlossaryTerm[]>([]);
const outline = ref<Outline>({ items: [], tree: [], count: 0 });

const loading = ref(true);
const fatal = ref<Error | null>(null);
const liveTask = ref<TaskInfo | null>(null);
const sseStatus = ref<StreamStatus>('connecting');

/* ------------------------------ 视图状态 ------------------------------ */
const viewMode = ref<ViewMode>('bilingual');
const originalMode = ref<OriginalMode>('text');
const pairOrder = ref<PairOrder>('en_left');
const currentRowId = ref('');
const collapsed = ref<string[]>([]);
// 「显示参考文献」开关已按用户要求移除：参考文献**恒显示**，不再是一条界面状态。
// （服务端 `reading_progress.show_refs` 字段仍照旧写 `true`，只是前端不再读它。）
const zoom = ref(1);

/* ------------------------------ 缩放 ------------------------------
 * `zoom` 是**唯一真值**：页图宽度 = 栏宽 × zoom（见 `pageWidth`），工具栏那个百分比直接读它。
 * 三条入口（± 按钮 / Ctrl+滚轮 / 触控板捏合）全部汇到 `setZoom`，所以"我怎么缩放"与
 * "数字显示多少"不会脱节 —— 这是用户报的"放大程度与数值对不上"的根治办法。
 */
const ZOOM_MIN = 0.4;
const ZOOM_MAX = 3;
/** 缩放**档位**（用户口径 2026-10-10："复刻 pdf 的放大和缩小逻辑"）——
 *  点一次跳一档（pdf.js 就是档位制），而不是加 10%。
 *  ⚠️ 这里的 1 = **适应栏宽**（本阅读器的基准），不是 PDF 的 100% 物理尺寸。 */
const ZOOM_STEPS = [0.4, 0.5, 0.67, 0.8, 1, 1.25, 1.5, 2, 2.5, 3];

function setZoom(next: number) {
  const v = Math.round(Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, next)) * 100) / 100;
  if (v !== zoom.value) zoom.value = v;
}

/** ± 一档：在 `ZOOM_STEPS` 里取上/下一档（到头就停在端点上）。
 *  以前是 `zoom ± 0.1`（细步进）：从 1.0 连点两下才到 1.2，看不出"跳了一档"。 */
function zoomBy(dir: number) {
  if (dir > 0) setZoom(ZOOM_STEPS.find((s) => s > zoom.value + 1e-6) ?? ZOOM_MAX);
  else setZoom([...ZOOM_STEPS].reverse().find((s) => s < zoom.value - 1e-6) ?? ZOOM_MIN);
}

/**
 * Ctrl/⌘ + 滚轮缩放（触控板捏合在 Chrome 里就是带 `ctrlKey` 的 wheel 事件）。
 *
 * ⚠️ 必须 `preventDefault`：不拦的话会连带触发**浏览器自己的页面缩放** —— 那才是
 * "我明明缩放了、百分比却不动"的根源：浏览器缩放放大的是整个界面（连百分比标签一起放大），
 * 而它并不表示页图的显示比例。拦下来之后，同一个手势改的是 `zoom`，页图与数字一起动。
 */
/** Ctrl/⌘ + 滚轮/捏合的**累计量**：攒够一格再跳一档。
 *  触控板交上来的是连续小值，一有就跳档会"哗哗"连跳好几档。 */
let wheelAcc = 0;
function onContentWheel(ev: WheelEvent) {
  if (!ev.ctrlKey && !ev.metaKey) return;
  ev.preventDefault();
  /* 档位缩放（2026-10-10）：与 ± 按钮**同一套档位** —— 以前这里是 `zoom - deltaY * 0.002`
     的连续式，和按钮的步进不是一回事，同一个手势在鼠标滚轮 / 触控板上速度差很多。 */
  wheelAcc += ev.deltaY;
  if (Math.abs(wheelAcc) < 30) return;
  zoomBy(wheelAcc > 0 ? -1 : 1);
  wheelAcc = 0;
}
const searchQuery = ref('');
const searchScope = ref<SearchScope>('all');
/**
 * 搜索方式：关键词（原有词法搜索）/ 语义（`POST /rag/similar`，只查本文）。
 *
 * 状态放在视图而不是 `ReaderSearch` 内部：下面的边打边搜、深链 `?q=`、页签角标都要知道
 * 现在是哪种模式 —— 语义模式下不该再去跑词法搜索（否则角标会显示一份界面里看不到的命中数）。
 */
const searchMode = ref<SearchMode>('lexical');
const searchHits = ref<Array<{ id: string; page: number; side: string; snippet: string }>>([]);
const mobilePane = ref<'' | 'side'>('');
/**
 * 宽屏下侧栏是否展开（窄屏走 mobilePane 抽屉，见 asideVisible）。
 *
 * 记忆**按论文**（= 按标签页，键里带 paperId）：切到另一篇论文的标签页时各自还各自的开合状态。
 * 以前是一个全局键 `pm.reader.aside` —— A 篇收起侧栏之后切到 B 篇也是收起的
 * （用户 2026-10-03："标签页切换时要记录侧栏打开的状态"）。
 * 服务端的 `reading_progress.aside_open` 仍是权威值（跨设备/刷新后用），本地这份只是**同步**的
 * 副本：不等那条进度请求回来，开合就已经是对的，不会"先错一下再跳回来"。
 */
const asideKey = `pm.reader.aside.${instancePaperId}`;
function initialAsideOpen(): boolean {
  const own = localStorage.getItem(asideKey);
  if (own !== null) return own !== '0';
  // 老版本只有一个全局键：第一次按论文记的时候先沿用它，免得所有人被"打开"一次
  const migrated = localStorage.getItem('pm.reader.aside') !== '0';
  try { localStorage.setItem(asideKey, migrated ? '1' : '0'); } catch { /* 隐私模式忽略 */ }
  return migrated;
}
const asideOpen = ref(initialAsideOpen());
/** 侧栏当前是否可见：窄屏 = 抽屉打开；宽屏 = 用户没收起。 */
const asideVisible = computed(() => (isNarrow.value ? mobilePane.value === 'side' : asideOpen.value));
const isNarrow = ref(false);
const paneWidth = ref(1200);

/**
 * 侧栏宽度：可拖拽，**按论文**记在 `localStorage`（键里带 paperId）。
 *
 * 为什么按论文：各篇的侧栏内容差别很大（这篇图多、那篇概念多），一个全局宽度总有一篇不合适；
 * 而"这篇论文上次是怎么看的"是本阅读器已有的口径（`reading-progress` 里存了侧栏开合 / 字号 /
 * 缩放）。宽度**不进服务端那份**：它是纯视图状态，进度表里没有这一列，为它改后端 schema
 * 不划算（用户口径也允许 `localStorage`）。
 *
 * 上限**按视图模式**给（用户口径第二轮返工）：对照模式下正文本来就只有半屏，
 * 侧栏超过 360px 会把译文挤没；纯原文 / 纯译文是单栏、空白多，可以放宽到 640px
 * （再和视口挂钩，不超过 60%）—— 解读、术语表这类内容在窄栏里会被挤成竖排。
 */
const ASIDE_W_MIN = 220;
/** 对照模式（原文 + 译文并排）的上限：维持原来的 360px */
const ASIDE_W_MAX_COMPARE = 360;
/** 非对照模式的上限：640px，再与视口挂钩（不超过 60%） */
const ASIDE_W_MAX_WIDE = 640;
const ASIDE_W_DEFAULT = 300;
/** 点「解读」页签时的目标宽度：解读里全是小标题与概念卡，窄栏会被挤成竖排 */
const ASIDE_W_INSIGHT = 520;
/** 点「对话」页签时的目标宽度：默认 300 下消息会被挤成竖排字，引用 chip 也放不下 */
const ASIDE_W_CHAT = 420;
const asideWidthKey = `pm.reader.aside-w.${instancePaperId}`;
/** 视口宽度（响应式）：非对照模式的上限要跟窗口挂钩 */
const winWidth = ref(typeof window === 'undefined' ? 1440 : window.innerWidth);

/**
 * 当前模式下的上限。对照 = 360；非对照 = min(640, 视口 60%)，但**不低于下限** ——
 * 窄窗口下 `innerWidth * 0.6` 可能小于 220，不夹一下就会算出"上限比下限还小"。
 */
const asideMaxWidth = computed(() => (viewMode.value === 'bilingual'
  ? ASIDE_W_MAX_COMPARE
  : Math.max(ASIDE_W_MIN, Math.min(ASIDE_W_MAX_WIDE, Math.round(winWidth.value * 0.6)))));

/**
 * 夹取宽度。**拖拽、切模式、点解读页签都走它** —— 上限只在初始化时算一次的话，
 * 用户先拖到 600 再把视图切回对照，侧栏会一直停在 600（对照的文字列被挤没）。
 */
const clampAsideWidth = (w: number) => Math.max(ASIDE_W_MIN, Math.min(asideMaxWidth.value, Math.round(w)));

/**
 * 初始化时**只夹硬边界**（220~640），不按模式夹：这一刻 `viewMode` 还是默认的 bilingual，
 * 而这篇论文真正的视图模式要等 `loadAll()` 才灌进来 —— 按模式夹会把"在纯译文下拖到 600"
 * 的记录一开始就削成 360，用户下次打开永远是 360（踩过）。模式夹取交给下面的 watch。
 */
const asideWidth = ref(ASIDE_W_DEFAULT);
try {
  const savedW = Number(localStorage.getItem(asideWidthKey));
  if (Number.isFinite(savedW) && savedW > 0) {
    asideWidth.value = Math.max(ASIDE_W_MIN, Math.min(ASIDE_W_MAX_WIDE, Math.round(savedW)));
  }
} catch { /* 隐私模式下忽略 */ }
/** 给 `<style>` 里的 `v-bind()` 用：宽度是响应式的，拖到哪侧栏就是多宽 */
const asideWidthPx = computed(() => `${asideWidth.value}px`);

/**
 * 侧栏占位宽度（0 = 收起）：正文那一列靠它让位。
 *
 * 侧栏本身是**绝对定位**的（见样式），所以正文列永远是整宽；"让位"由内层
 * `.reader-slide` 的 `width: calc(100% - 这个值)` + `translateX(这个值)` 完成 ——
 * 于是开合时**只有这一个值在变**，正文的一次重排发生在**动画开始那一帧**，
 * 之后是纯 transform 的平移（合成层，不重排）。窄屏侧栏是抽屉，不占位。
 */
const asideGapPx = computed(() => (asideVisible.value && !isNarrow.value ? `${asideWidth.value}px` : '0px'));

/** 胶囊工具栏要居中在**正文列**上：侧栏展开时整体右移半个栏宽（transition 与侧栏同步）。 */
const capsuleShift = computed(() => (
  asideVisible.value && !isNarrow.value ? Math.round(asideWidth.value / 2) : 0
));

/** 同一份位移给弹出面板用（CSS 变量形式，见 `.cap-panel` 的 `--panel-shift`）。 */
const capsuleShiftCss = computed(() => `${capsuleShift.value}px`);

/**
 * 顶栏弹出面板：**同一时刻只开一个**（互斥）。
 *
 * 2026-10-17 用户口径：**常规模式的版面和显示合成一个「设置」面板** ——
 * 以前是两个按钮（滑块 = 版面、Aa = 显示），在「译文」视图下前者打开后几乎全是
 * `v-if` 不成立的空行（没有原文栏、没有页图、也不是对照），后者只剩一个 "Aa" 字，
 * 看起来就是个"空按钮"。合成一个面板后，任何视图下都至少有「视图 / 正文字号」两行。
 * 面板本体始终挂载（`v-show`），原因见 `CapPanel.vue` 的头注释（自检脚本按文字点里面的按钮）。
 */
const capPanel = ref<'' | 'settings'>('');

function toggleCapPanel(which: 'settings') {
  capPanel.value = capPanel.value === which ? '' : which;
  void nextTick().then(() => {
    placeCapPanel();
    syncToastDrop();
  });
}

/** 批注的**二级菜单**（颜色 / 粗细）：点画笔或荧光笔展开，再点一次收起 */
const inkOptionsOpen = ref(false);
/** 二级菜单的位置（fixed，跟着胶囊走：拖动、切换模式都会重新量一次） */
const inkOptionsStyle = ref<Record<string, string>>({});

/** 橡皮的二级菜单：一条滑块，滑到底就是**清空这一页的笔迹**（用户口径） */
const eraserClearOpen = ref(false);
const clearProgress = ref(0);

/** 视口正中那一页的批注键：清空作用于"你正在看的那一页" */
const inkKeyNear = computed<string | null>(() => {
  const nodes = Array.from(document.querySelectorAll('.pg[data-page]')) as HTMLElement[];
  let best: { page: number; d: number } | null = null;
  for (const el of nodes) {
    const r = el.getBoundingClientRect();
    if (r.bottom < 0 || r.top > innerHeight) continue;
    const d = Math.abs((r.top + r.bottom) / 2 - innerHeight / 2);
    if (!best || d < best.d) best = { page: Number(el.dataset.page), d };
  }
  return best ? pageKey(best.page) : null;
});

/**
 * 清空的目标页：**刚画过的那一页**优先（用户口径"清空页面笔迹"就是清他刚画的那页），
 * 那一页已经滚出视口时退回"视口正中那一页"。注意 view 后缀要用当前视图重新拼，
 * 否则切过视图就会清到另一份文档上。
 */
const inkKeyTarget = computed<string | null>(() => {
  const last = inkKey.value;
  if (last) {
    const page = Number(String(last).split(':')[1]?.split('-')[0]);
    if (page) {
      const el = document.querySelector('.pg[data-page="' + page + '"]');
      const r = el?.getBoundingClientRect();
      if (r && r.bottom > 0 && r.top < innerHeight) return pageKey(page);
    }
  }
  return inkKeyNear.value;
});

function onClearSlide(raw: string): void {
  const value = Number(raw) || 0;
  clearProgress.value = value;
  if (value < 95) return;
  // 滑到底 = 清空（一次撤销就能整个回来）
  clearPageInk(inkKeyTarget.value);
  clearProgress.value = 0;
  eraserClearOpen.value = false;
  void nextTick().then(syncToastDrop);
}

function placeInkOptions(): void {
  const el = capsuleEl.value;
  if (!el) return;
  const r = el.getBoundingClientRect();
  const menu = document.querySelector('[data-ink-options]') as HTMLElement | null;
  const height = menu?.offsetHeight ?? 0;
  const width = menu?.offsetWidth ?? 0;
  // 横向贴边回夹：胶囊拖到最右/最左时，菜单不要越出视口（与设置面板同一口径）
  let center = r.left + r.width / 2;
  if (width) {
    center = Math.max(8 + width / 2, Math.min(innerWidth - 8 - width / 2, center));
  }
  const left = Math.round(center) + 'px';
  // 下方放不下就翻到胶囊上方（胶囊本身可以一路拖到页面底部）
  if (height && innerHeight - r.bottom < height + 16) {
    inkOptionsStyle.value = { left, top: Math.round(r.top - height - 8) + 'px' };
    return;
  }
  inkOptionsStyle.value = { left, top: Math.round(r.bottom + 8) + 'px' };
}

/** 选工具：点"已经在用的"画笔/荧光笔 = 展开/收起它的二级菜单（用户口径） */
function pickInkTool(key: 'hand' | 'pen' | 'marker' | 'eraser') {
  if (key === inkTool.value && (key === 'pen' || key === 'marker')) {
    inkOptionsOpen.value = !inkOptionsOpen.value;
    void nextTick().then(() => {
      if (inkOptionsOpen.value) placeInkOptions();
      syncToastDrop();
    });
    return;
  }
  if (key === inkTool.value && key === 'eraser') {
    // 橡皮二次点击：弹出"清空本页笔迹"的滑块（用户口径）
    eraserClearOpen.value = !eraserClearOpen.value;
    clearProgress.value = 0;
    void nextTick().then(() => {
      if (eraserClearOpen.value) placeInkOptions();
      syncToastDrop();
    });
    return;
  }
  inkTool.value = key;
  inkOptionsOpen.value = key === 'pen' || key === 'marker';
  eraserClearOpen.value = false;
  // 二级菜单跟着胶囊走：展开/收起后重新摆位，并让提示条再让一层
  void nextTick().then(() => {
    if (inkOptionsOpen.value) placeInkOptions();
    syncToastDrop();
  });
}

/** 落盘（每像素写一次没意义；拖完 / 夹取之后才写） */
function persistAsideWidth() {
  try { localStorage.setItem(asideWidthKey, String(asideWidth.value)); } catch { /* 忽略 */ }
}

/**
 * 当前宽度超出新模式上限 → 夹到上限并落盘（用户口径：切换模式时夹取）。
 * 宽窄一变就要重算正文栏宽、并**作废行偏移缓存**（行高随列宽变），所以走 `afterAsideResize`。
 */
async function applyAsideMax() {
  const capped = clampAsideWidth(asideWidth.value);
  if (capped === asideWidth.value) return;
  asideWidth.value = capped;
  persistAsideWidth();
  await afterAsideResize();
}
// 切视图模式 / 窗口尺寸变化（非对照的上限与视口挂钩）都要重夹一次
watch([viewMode, asideMaxWidth], () => { void applyAsideMax(); });

/* ------------------------------ 解读页签 / 左栏宽度联动 ------------------------------ */

/**
 * 确保左栏可见（**不切换**）：宽屏取消"收起"，窄屏开抽屉。
 *
 * 「解读回到左栏」之后光选中页签是不够的：用户把左栏收起过（或窄屏抽屉关着）时页签根本看不见，
 * 表现就是"点了没反应" —— 与 `?side=insight` 深链同一个理由。
 */
async function ensureAsideVisible() {
  if (asideVisible.value) return;
  if (isNarrow.value) mobilePane.value = 'side';
  else {
    asideOpen.value = true;
    try { localStorage.setItem(asideKey, '1'); } catch { /* 忽略 */ }
  }
  await nextTick();
  syncPaneWidth();
}

/**
 * 打开「论文解读」页签（**只给「⋯」里的「重新生成解读」用**）：发起任务之后把左栏那个页签打开，
 * 否则界面上看不到任何变化，像"点了没反应"。
 *
 * 工具栏原来那枚「解读」按钮**已按用户口径删除**（解读在左栏就是一个页签，工具栏不该有重复入口）；
 * `?side=insight` 深链仍然只会"选中页签"，靠 `watch(sideTab)` 变宽。
 *
 * 「变宽」也没有写在这里，而是挂在 `watch(sideTab)` 上：点页签、深链、以后可能加的快捷键
 * 都是"进了解读页签"，写一处必然漏一处。
 */
async function openInsightTab() {
  await ensureAsideVisible();
  sideTab.value = 'insight';
}

/**
 * 进「解读」页签时把侧栏放宽到 520（用户口径）。
 *
 * **只增不减**：用户手动拖到 700 之后再点解读，不该被缩回 520 —— "我拉宽过"是明确意图。
 * 目标同样走 `clampAsideWidth`：纯译文（上限 640）能到 520，对照模式会被夹到 360
 * （那时正文本来只剩半屏，不能再让侧栏吃掉）。
 */
async function widenForInsight() {
  await widenAside(ASIDE_W_INSIGHT);
}

/** 「对话」页签：目标 420（比解读窄一点，但比默认 300 宽得能读）。 */
async function widenForChat() {
  await widenAside(ASIDE_W_CHAT);
}

/** 放宽左栏到目标宽度（**只增不减**，理由见 `widenForInsight`）。 */
async function widenAside(target: number) {
  if (isNarrow.value) return;              // 窄屏是抽屉、宽度由 CSS 定死，变宽没有意义
  const want = clampAsideWidth(target);
  if (want <= asideWidth.value) return;
  asideWidth.value = want;
  persistAsideWidth();
  await afterAsideResize();
}
/* 「进解读页签 → 变宽」的钩子挂在 `sideTab` 的 watch 上（见下面页签定义处）：
   点页签、深链、以后可能加的快捷键都是"进了解读"，写一处必然漏一处。 */

/**
 * 左栏「论文解读」页签右上角那枚图标按钮：在**应用内**新开一个标签页
 * （TabBar 里多一个标签），不是浏览器标签页 —— 用户的"新标签页"指的是这个。
 *
 * ⚠️ 必须在**点击回调里**调 `openTab`：它会改响应式标签状态，渲染期调就是"渲染→改状态→再渲染"
 * 的死循环（`stores/tabs.ts` 里也写了这条）。
 */
function openInsightPage() {
  const target = openTab({
    name: 'understand',
    params: { paperId: instancePaperId },
  });
  void router.push(target as never);
}

/** 同 Menu 组件的 items 结构（结构类型，无需导出）。 */
interface ToolbarAction {
  key: string;
  label: string;
  icon?: string;
  danger?: boolean;
  disabled?: boolean;
}

/** 阅读器工具条：低频动作收进「⋯」，避免一排同权重按钮把主操作淹没。 */
const toolbarMenu: ToolbarAction[] = [
  { key: 'retranslate', label: '重新翻译', icon: 'refresh' },
  // 与论文库卡片「⋯」里那一项**同名**（R4）：同一个后端动作，两处叫法必须一致
  { key: 'insight', label: '重新生成解读', icon: 'sparkle' },
  { key: 'pdf', label: '打开原 PDF', icon: 'external' },
  { key: 'markdown', label: 'Markdown 视图', icon: 'document' },
  // 打包下载（原 PDF + 三个版本的 Markdown + 被引用的插图）：一次拿走，离线可看
  { key: 'download-zip', label: '下载打包（PDF + Markdown + 图片）', icon: 'download' },
];

function onToolbarAction(key: string) {
  if (key === 'retranslate') { startTranslate('retranslate'); return; }
  if (key === 'insight') { void regenerateInsight(); return; }
  if (key === 'pdf') { openPdf(); return; }
  if (key === 'markdown') openMarkdownView();
  if (key === 'download-zip') downloadBundle();
}

const selection = reactive({
  show: false, x: 0, y: 0,
  /** 选区文字：**截断版**给「AI 解释」用（请求体别无限大），`fullText` 是完整版、给「复制」用 */
  text: '', fullText: '',
  /**
   * **坐标系统里的那一段文字**（`renderedText` 的切片）：正文行/Markdown 视图与 `text` 一样，
   * 原 PDF 不一样 —— 文字层的 span 是绝对定位的，浏览器会在 span 之间插换行/空格，
   * 而偏移量那套坐标系是把文本节点直接拼起来的。凡是"要和偏移量一起存"（高亮 / 笔记引用）
   * 都必须用它，否则画高亮时的文本校验会判成错位。
   */
  anchorText: '',
  rowId: '', start: 0, end: 0, side: 'en' as 'en' | 'zh',
  /** 选区是否有块锚点（原 PDF 划词只有文字）—— 决定能不能写高亮/笔记 */
  anchored: true,
  /** 在哪个视图里划的：正文行 row / Markdown 视图 markdown / **原 PDF 页图 pdf**
   *  （决定锚点怎么存 —— 三面各自的锚点不同，见 `mdAnchor` / `pdfAnchor`） */
  scope: 'row' as 'row' | 'markdown' | 'pdf',
  /** scope='markdown' 时的页号（锚点是 `md:p<页号>`） */
  mdPage: 0,
  /** 选区**所在的页**（三种视图都给）：正文行从行 id 里解、Markdown 视图取页号、
   *  原 PDF 取页元素上的页号。带进对话时要写进消息里（用户口径 2026-10-03：
   *  "加入对话的内容要包含文本位置"），PDF 划词没有块锚点，页号更是唯一的位置线索。 */
  page: 0,
  /** Markdown 视图里不给「笔记」：笔记靠 `block_id` 定位回正文，那边的锚点在正文里不存在 */
  allowNote: true,
  /** 跨行 / 跨页选区切开后的各段（每段一条记录，共用同一个分组）
   *  - 正文行：同一侧相邻的多行，`rowId` 是行 id；
   *  - Markdown 视图：同一侧**跨页**时每页一段，`rowId` 是 `md:p<页号>`。 */
  segments: [] as MdSegment[],
  /** 选区/点击处盖住的已有高亮 id（菜单据此显示「删除高亮」） */
  hlIds: [] as string[],
  /** 选区的外接矩形：**右键时用来判"这次右键落在选区上吗"** ——
   *  浏览器在右键 mousedown 时会把选区折叠掉，那时 `getSelection()` 已经没有选区了，
   *  只能靠这份快照（右键菜单要给"复制/高亮/询问"那几项）。 */
  rect: null as { left: number; top: number; right: number; bottom: number } | null,
});
/**
 * 划词解释（浮动卡片）。
 *
 * **解释由问答 agent 产出**（`/agent/ask` + `selection`，SSE 流式）：
 * 旧那条"后端自己拼 prompt 调 generation provider"的路已删除 —— 它给的答案不带引用、
 * 不能跳转，和问答那条链路口径也不一致（见 dev-log 第二十轮）。
 *
 * 卡片里是一个**小对话线程**：可以就这段继续追问（同一 conversation_id，所以追问有上下文），
 * 也能"在对话里继续"把选区带进侧栏面板；历史解释来自 `source=selection` 的批注。
 */
const explainCard = reactive({
  open: false, loading: false, question: '', rowId: '', blockAnchored: true,
  /**
   * 浮层的**图钉**（用户口径：AI 解释**默认锚定** —— 读完解释再决定要不要存，
   * 中途移开鼠标就关掉太容易丢）。用户口径 2026-10-07 补了一条：**锚定之后点浮层底部的
   * 来源 chunk 跳转，浮层不能消失**（那正是"看着解释跳到那一段"的用法），见 `focusCitation`。
   */
  pinned: true,
  /**
   * 选区在哪个视图里划的：`row` 有行锚点、`markdown` 只有页号、`pdf` 只有**页号 + 文字层偏移**。
   * 三种都不影响"解释"本身（解释是拿选区文字去问），只影响锚点怎么传回去。
   */
  scope: 'row' as 'row' | 'markdown' | 'pdf',
  selectionText: '', page: 0 as number, conversationId: '' as string | null,
  /** 解释的是**插图**时（右键图片 → 询问 Cyli）：浮层顶部给个缩略图，而 `attachments` 才是
   *  真给模型看的东西。⚠️ 附件**每轮都要重发**：历史里只存文字（`history_for_agent` 不带图），
   *  不重发的话第二轮追问模型就"看不见这张图"了。 */
  imageUrl: '', imageCaption: '', attachments: [] as string[],
  /** 打开时的选区快照：`存为笔记` 要它（那时选区可能已经被用户改了） */
  highlightId: '' as string, start: 0, end: 0, side: 'en' as 'en' | 'zh', quote: '',
  /** 浮层锚点（划词那一处的屏幕位置）—— 与笔记浮层同一套定位 */
  anchor: null as { x: number; y: number; w?: number; h?: number } | null,
  /**
   * 过程轨迹（深度思考 / 工具调用）：**默认折叠**，只展示最终回答（用户口径）。
   * 流式时按事件本地拼，`done` 到了用后端那份权威的 `run.steps` 替换。
   */
  processOpen: false,
  turns: [] as Array<{
    role: 'user' | 'assistant'; text: string; citations: AgentCitation[];
    steps?: AgentStep[];
  }>,
});
let explainController: AbortController | null = null;
/** 从正文划词带进「对话」面板的选区（发出去之后由面板 `clear-selection` 清掉） */
const chatSelection = ref<AgentSelection | null>(null);
/** 笔记浮层的状态见下面 `notePop`（第五步：轻量浮层取代居中弹窗）。 */
/** 放大查看的目标（图 URL + 图注）：见下面的 `zoomTitle` */
const zoomImage = ref<ZoomTarget | null>(null);

/**
 * 原 PDF 图层渲染失败（平板/手机上常见）：**全局胶囊 toast 只提示一次**。
 *
 * 以前每页都在图上压一行"原 PDF 渲染失败，已回退为页图"——十几页就是十几行，
 * 用户口径是"移除回退文字，改为胶囊 toast 通知一次"。用 `sessionStorage` 记"已经说过了"：
 * 同一次会话里翻多少页都只弹一次，重开页面才重新有机会提醒。
 */
const PDF_TIP_KEY = 'cylit:pdf-fallback-told';
function onPdfFailed() {
  try {
    if (sessionStorage.getItem(PDF_TIP_KEY)) return;
    sessionStorage.setItem(PDF_TIP_KEY, '1');
  } catch { /* 隐私模式下拿不到 storage：那就照常提示 */ }
  toast('这台设备渲染不了原 PDF 图层，已改用页图（文字选中不可用）', 'warn', 4800);
}
const briefDraft = reactive<BriefDraft>({
  domain: '', style: '', notes: '', keepAsIsText: '', glossaryText: '',
});

/** 侧栏（左栏，与目录合并）：**目录 / 论文解读**开头，再是搜索与其它页签 / 任务。
 *  解读读 `/understanding`，走 `useUnderstanding` 的整包缓存（与整页版共享）。
 *  进这个页签会自动把侧栏放宽（见 `widenForInsight`）。
 *  ⚠️ 顺序是用户口径：**论文解读紧跟「目录」右边**（页签条可横向滚，放前面才不会被挤出可视区）。
 *  ⚠️ 原「阅读地图」页签已随该功能一起删除（2026-10-02）。 */
type SideTabKey = 'toc' | 'chat' | 'search' | SideTab;
const sideTab = ref<SideTabKey>('toc');
/** 「目录」页签里的两种视图：大纲 / 页面缩略图（《修改计划》第二步，二）。
 *  只存本次会话：它是"这一眼怎么看"，不是论文的属性，没必要进 localStorage 或服务端进度。 */
const tocMode = ref<'outline' | 'pages'>('outline');
const SIDE_TAB_KEYS = [
  'toc', 'chat', 'insight', 'search', 'references', 'notes', 'formulas', 'figures', 'terms', 'tasks',
];
/* 进「论文解读」页签就把左栏放宽（`widenForInsight` 只增不减）。
   放在这里而不是点击处理器里：点页签 / `?side=` 深链 / 工具栏按钮都算"进了解读"。 */
watch(sideTab, (k) => {
  if (k === 'insight') void widenForInsight();
  else if (k === 'chat') void widenForChat();
  else if (k === 'references') void ensureReferences();
});
/**
 * 「术语」页签的计数 = 简报里的统一译法 ∪ IR 术语表。
 * 只数 IR 术语表是不对的：简报里那 20 条统一译法才是真正注入每次翻译 prompt 的那一份，
 * 以前页签没有角标、面板里还写"没有术语"（用户看到的正是这个）。
 */
const termCount = computed(() => {
  const names = new Set<string>();
  for (const en of Object.keys(brief.value?.glossary ?? {})) {
    if (en.trim()) names.add(en.trim());
  }
  for (const t of glossary.value) {
    const en = String(t.source ?? t.en ?? '').trim();
    if (en) names.add(en);
  }
  return names.size;
});

/**
 * 「笔记」页签的角标 = 那个页签里会有**几条**（口径与 `ReaderSidePanel` 的归并一致）：
 * 一处高亮算一条（跨行划词的多条记录共用一条笔记）、没挂高亮的笔记算一条、批注算一条。
 * 不能拿"高亮数 + 笔记数"直接相加：那样同一处高亮会被数两遍。
 */
const notesFeedCount = computed(() => {
  const groups = new Set<string>();
  for (const h of highlights.value) groups.add(h.group_id || h.id);
  const onHighlight = new Set(highlights.value.map((h) => h.id));
  const orphan = notes.value
    .filter((n) => !n.highlight_id || !onHighlight.has(n.highlight_id)).length;
  return groups.size + orphan + annotations.value.length;
});

const SIDE_TABS = computed<Array<{
  k: SideTabKey; icon: string; label: string; title: string; count?: number;
}>>(() => [
  { k: 'toc', icon: 'list', label: '目录', title: '目录（中英双语）' },
  {
    k: 'chat', icon: 'chat', label: '对话',
    title: '论文问答（多轮对话；圆环 = 上下文用量，点它压缩）',
  },
  {
    /* 页签名尽量都是**两个汉字**（用户口径，2026-10-07）：论文解读 → 解读、引用文献 → 文献。
       完整意思写在 `title` 里（hover 就能看到），不靠页签上的字撑。 */
    k: 'insight', icon: 'sparkle', label: '解读',
    title: '论文解读（论文精读：基本信息 / 背景 / 动机 / 贡献 / 方法 / 实验 / 结论）',
  },
  {
    k: 'search', icon: 'search', label: '搜索',
    title: searchMode.value === 'semantic' ? '语义搜索（只查本文）' : '全文搜索（原文 + 译文）',
    // 角标只报**当前模式**的命中数：语义模式的命中在 ReaderSearch 里，词法那份数此时是旧账
    count: searchMode.value === 'lexical' ? (searchHits.value.length || undefined) : undefined,
  },
  {
    k: 'notes', icon: 'note', label: '笔记',
    title: '笔记（批注 / 高亮 / 笔记已合并：一条 = 一处高亮 + 它的正文）',
    count: notesFeedCount.value || undefined,
  },
  {
    k: 'formulas', icon: 'formula', label: '公式',
    // 标题反映**这批公式是从哪来的**（用户口径：优先且只显示翻译的公式，未翻译用原文兜底）：
    // 全译完 → 只取自译文；还没翻译 → 取解析产物；半翻译 → 已译页取译文、未译页取原文。
    title: formulasSource.value === 'parse'
      ? '公式（来自解析产物：这一篇还没翻译，先按原文显示）'
      : formulasSource.value === 'mixed'
        ? '公式（已翻译的页取译文，未翻译的页按原文兜底）'
        : '公式（从译文提取，只读）',
    count: formulas.value.length,
  },
  { k: 'figures', icon: 'image', label: '插图', title: '插图', count: figures.value.length },
  { k: 'terms', icon: 'layers', label: '术语', title: '术语与文档简报', count: termCount.value },
  {
    /* 用户口径 2026-10-07：**放在倒数第二个**（任务之前），图标用**双引号**那枚 `quote` ——
       参考文献本来就是"别人说过的话"，引号比书架更贴题；笔记那枚 `note`（文稿）也一并定下来。 */
    k: 'references', icon: 'quote', label: '文献',
    title: '引用文献（正文里的 [2, 4, 10] 点开就是这里；点条目跳发表网站）',
    count: paperReferences.value.length || undefined,
  },
  { k: 'tasks', icon: 'task', label: '任务', title: '任务（翻译 / 转录 / 解读 / 索引 / 解析，按类型）' },
]);

/* 侧栏页签**一行到底、不换行**（用户口径）：放不下就左右滚，见 `.side-tabs` 的样式。
   这两个小工具负责"滚得动"和"切过去看得见"。 */
const sideTabsEl = ref<HTMLElement | null>(null);

/** 鼠标滚轮：横向滚动容器默认只吃水平轴（触控板/Shift+滚轮），普通鼠标滚轮会去滚页面 —— 转成横向。
 *  这里用 `behavior: 'instant'`：容器上开了 CSS 的 `scroll-behavior: smooth`，滚轮要跟手、不能补间。 */
function onSideTabsWheel(ev: WheelEvent) {
  const el = sideTabsEl.value;
  if (!el || el.scrollWidth <= el.clientWidth + 1) return;
  if (Math.abs(ev.deltaY) > Math.abs(ev.deltaX)) {
    ev.preventDefault();
    el.scrollTo({ left: el.scrollLeft + ev.deltaY, behavior: 'instant' });
    syncTabsScroll();
  }
}

/** 切换页签后把它滚进可视区（最左/最右那两个尤其需要）。 */
watch(sideTab, async () => {
  await nextTick();
  sideTabsEl.value?.querySelector<HTMLElement>('.side-tab.active')
    ?.scrollIntoView({ block: 'nearest', inline: 'nearest' });
});

/** 能往左 / 往右滚时，在那一侧浮一枚圆形箭头按钮（用户口径：「左侧或者右侧有内容时显示」）。 */
const tabsScroll = reactive({ left: false, right: false });
function syncTabsScroll() {
  const el = sideTabsEl.value;
  if (!el) return;
  tabsScroll.left = el.scrollLeft > 1;
  tabsScroll.right = el.scrollLeft + el.clientWidth < el.scrollWidth - 1;
}
/** 点箭头：翻大约一屏的 70%。
 *
 * 用**瞬时**滚动（直接改 `scrollLeft`），不写 `scroll-behavior: smooth`、也不用
 * `scrollBy({ behavior: 'smooth' })`：补间动画由合成器推进，无头 + 虚拟时间下根本不走，
 * 实测"点了按钮纹丝不动"，而且这种失败在自检里是看不出来的（真实浏览器也会受
 * "减少动效"设置影响）。翻一屏的距离本来就短，瞬时反而更跟手。
 */
function scrollTabs(dir: -1 | 1) {
  const el = sideTabsEl.value;
  if (!el) return;
  const max = el.scrollWidth - el.clientWidth;
  const step = Math.max(120, el.clientWidth * 0.7);
  el.scrollLeft = Math.max(0, Math.min(max, el.scrollLeft + dir * step));
  syncTabsScroll();            // 不依赖 scroll 事件（异步派发；程序化滚动后要立刻更新按钮显隐）
}

/* 页签条宽度会变（侧栏开合动画、窗口缩放）；角标数字变化也会改变 scrollWidth →
   用 ResizeObserver 盯容器、再用 watch 盯角标，按钮显隐才不会停在旧状态。 */
let tabsRO: ResizeObserver | null = null;
onMounted(async () => {
  await nextTick();
  syncTabsScroll();
  const el = sideTabsEl.value;
  if (el && typeof ResizeObserver !== 'undefined') {
    tabsRO = new ResizeObserver(() => syncTabsScroll());
    tabsRO.observe(el);
  }
});
onUnmounted(() => {
  tabsRO?.disconnect();
  tabsRO = null;
  // 拖拽中切走路由：收尾（释放指针捕获 + 摘掉窗口级兜底监听），
  // 否则指针会一直被已经卸载的把手捕获着（症状与用户报的"碰不到滚动条"一致）。
  void onResizeUp();
});
watch(SIDE_TABS, () => { void nextTick(syncTabsScroll); });

/* Markdown 视图：直接看**带插图**的双语 Markdown（模型产物 + 后端插入的图片行）。
 *
 * **不再翻页**（用户口径）：一次性渲染整篇，打开时从**当前阅读页**那一段开始滚
 * （"保留从该区域开始的功能"）。
 */
const mdView = reactive({
  open: false,
  loading: false,
  /** 全屏（铺满视口）——标题栏右上角的那个按钮切它 */
  full: false,
  mode: 'bilingual' as MarkdownViewMode,
  pages: [] as TranslatedMarkdownPage[],
});

/** 正文滚动容器：打开时要把当前页那一段滚到顶部 */
const mdDocEl = ref<HTMLElement | null>(null);

async function openMarkdownView(startPage?: number) {
  mdView.open = true;
  const want = startPage || currentPage.value || 1;
  if (!mdView.pages.length) {
    mdView.loading = true;
    try {
      const d = await api.get<{ pages: TranslatedMarkdownPage[] }>(
        `/papers/${paperId.value}/markdown/translated`);
      mdView.pages = d.pages ?? [];
    } catch (err) {
      reportError(err, '加载 Markdown 失败');
      mdView.open = false;
      return;
    } finally {
      mdView.loading = false;
    }
  }
  // 图片是懒加载/异步的，会边加载边把后面的内容往下顶 —— 滚一次 + 稍后再校准一次
  await scrollMdToPage(want);
  window.setTimeout(() => { void scrollMdToPage(want); }, 260);
  // 高亮要等 DOM 真出来之后再画（`v-html` 换过内容，之前的标记会被抹掉）
  await paintMdHighlights();
}

/** 把 Markdown 视图里的高亮按偏移量画到对应的那一侧（原文 / 译文各自独立）。 */
async function paintMdHighlights() {
  await nextTick();
  const box = mdDocEl.value;
  if (!box) return;
  for (const block of Array.from(box.querySelectorAll<HTMLElement>('[data-md-side]'))) {
    const page = Number(block.closest('[data-md-page]')?.getAttribute('data-md-page') ?? 0);
    const side = block.dataset.mdSide === 'zh' ? 'zh' : 'en';
    const marks = highlights.value
      .filter((h) => (h.scope ?? 'row') === 'markdown'
        && h.block_id === mdAnchor(page) && (h.side ?? 'en') === side)
      .map((h) => ({ start: h.start, end: h.end, color: h.color, id: h.id, text: h.text }));
    markRanges(block, marks);
  }
}

/** 把第 `page` 页那一段滚到内容区顶部（用相对位移算，不依赖 offsetParent）。 */
async function scrollMdToPage(page: number) {
  await nextTick();
  const box = mdDocEl.value;
  if (!box) return;
  const target = box.querySelector<HTMLElement>(`[data-md-page="${page}"]`)
    ?? box.querySelector<HTMLElement>('[data-md-page]');
  if (!target) return;
  const base = box.getBoundingClientRect().top - box.scrollTop;
  const top = target.getBoundingClientRect().top - base;
  box.scrollTo({ top: Math.max(0, top - 6), behavior: 'auto' });
}

/**
 * 整篇 Markdown 的 HTML：**所有页一次性渲染**，每页一个 `<section data-md-page>` 锚点。
 * 页号是给"从当前阅读页开始滚"用的落点，也顺便当页与页之间的淡标注（不再有翻页器）。
 *
 * 每一侧再包一层 `[data-md-side]`：划词高亮要按"哪一侧 + 渲染后文字里的偏移量"定位
 * （原文 / 译文**分开存**，见 `utils/domMarks.ts` 与 Highlight.side）。
 */
const mdFullHtml = computed(() => mdView.pages.map((p) => {
  // ⚠️ **必须把 paperId 传给 `mdToHtml`**：后端下发的页级 Markdown 里图片是相对路径
  // （`../figures/x.png`），不传就改不成 `/papers/<id>/figures/...`，浏览器按
  // `/app/reader/<id>` 解析成 `/app/figures/...` → 全部 404（实测事故：haloscope 那篇
  // 解析完 md 视图 4 张图全 404，正确 URL 是 `/papers/<id>/figures/<name>.png`，实测 200）。
  const part = (side: 'en' | 'zh', md?: string | null) =>
    `<div class="md-part" data-md-side="${side}">${mdToHtml(md ?? '', paperId.value)}</div>`;
  const body = mdView.mode === 'original' ? part('en', p.original_md)
    : mdView.mode === 'translation' ? part('zh', p.translation_md)
    // 上下对照 = 同一页的原文 + 译文，各自都带图；两份之间只用空白隔开，不画分割线
    : `${part('en', p.original_md)}${part('zh', p.translation_md)}`;
  return `<section class="md-page" data-md-page="${p.page}">`
    + `<div class="md-page-no">第 ${p.page} 页</div>${body}</section>`;
}).join(''));

/** 切换视图模式后停在**同一页那一段**（以前靠翻页器，现在靠这个保持位置感） */
watch(() => mdView.mode, () => {
  if (mdView.open) void scrollMdToPage(currentPage.value || 1);
});

/** 高亮增删 / 页面数据到位后，重画 Markdown 视图里的标记（`v-html` 会把旧标记抹掉） */
watch([() => highlights.value.length, mdFullHtml], () => {
  if (mdView.open && !mdView.loading) void paintMdHighlights();
}, { flush: 'post' });

/** 三个视图模式 → 下载用的文件名后缀 / 按钮文案（"下载什么 = 你正在看什么"） */
const MD_MODES: Record<MarkdownViewMode, { label: string; suffix: string }> = {
  bilingual: { label: '中英对照', suffix: 'bilingual' },
  original: { label: '纯原文', suffix: 'original' },
  translation: { label: '纯译文', suffix: 'translation' },
};
const mdModeMeta = computed(() => MD_MODES[mdView.mode] ?? MD_MODES.bilingual);
const MD_MODE_TABS: Array<{ k: MarkdownViewMode; label: string }> = [
  { k: 'bilingual', label: '上下对照' }, { k: 'original', label: '原文' },
  { k: 'translation', label: '译文' },
];

/** 关掉 Markdown 视图：顺手退出全屏（下次打开回到常规尺寸，不"记住"上一次的全屏状态） */
function closeMarkdownView() {
  mdView.open = false;
  mdView.full = false;
}

/** 按**当前视图模式**下载整篇 Markdown。
 *
 * ⚠️ 内容由**后端**拼（`/markdown/export`，与打包 zip 共用同一份"三个版本"的实现）：
 * 前端不再自己拼字符串，免得"下载的那份"和"打包的那份"两套口径各写一遍。
 * 后端那份**不含页标识**（用户口径 2026-09-28：不要 `<!-- page N -->` 与 `## 第 N 页`），
 * 图片补成绝对地址；页与页之间留一条 `---`。
 */
function downloadMarkdown() {
  void downloadFromApi(`/papers/${paperId.value}/markdown/export`,
                       `${paperId.value}-${mdModeMeta.value.suffix}.md`,
                       { mode: mdView.mode }, '下载 Markdown');
}

/** 打包下载：原 PDF + **三个版本**的 Markdown + 被 Markdown 引用的插图（zip，解压即离线可看）。 */
function downloadBundle() {
  // 还没跑过页级翻译时包里只有 PDF（后端不会塞空 md）—— 先说一句，别让人以为下载坏了
  if (!stats.value.pages_done) {
    toast('这篇还没有页级译文产物，包里至少会有原 PDF（先「继续翻译」再打包更完整）',
          'warn', 4200);
  }
  void downloadFromApi(`/papers/${paperId.value}/export/zip`, `${paperId.value}.zip`,
                       undefined, '打包下载');
}

/** 正在下载的键（打包首次要按需裁 600dpi 插图，可能几十秒）：同一时刻只允许一个。 */
const busyDownload = ref('');

/**
 * 从后端取一个文件并落盘。
 *
 * 走 `downloadBlob`（fetch → Blob）而不是直接开新标签页：论文还没解析完 / 没有 source.pdf
 * 这类错误能提示出来，而不是让浏览器弹个"下载失败"什么都看不到。
 */
async function downloadFromApi(path: string, filename: string,
                               query: Record<string, unknown> | undefined, what: string) {
  if (busyDownload.value) return;
  busyDownload.value = what;
  try {
    saveBlob(filename, await downloadBlob(path, query));
    toast(`已下载 ${filename}`, 'ok');
  } catch (err) {
    reportError(err, `${what}失败`);
  } finally {
    busyDownload.value = '';
  }
}

/** 对照 + 原 PDF 时，右侧文字列放哪一侧（默认译文；可切原文） */
const pageSide = ref<'en' | 'zh'>('zh');

/* 正文字号：小 / 中 / 大（**默认小**），选中后记在本地（纯展示偏好，不进服务端进度）。
 * 档位表来自 `utils/readerFont`：两个解读整页也要用同一份（否则"阅读器显示大、整页显示中"）。 */
const TEXT_SIZES = READER_TEXT_SIZES;
type TextSizeKey = ReaderTextSize;
const TEXT_SIZE_KEY = 'pm-reader-text-size';
const TEXT_SIZE_OPTIONS: Array<{ k: TextSizeKey; label: string }> = [
  { k: 'sm', label: '小' }, { k: 'md', label: '中' }, { k: 'lg', label: '大' },
];
const textSize = ref<TextSizeKey>('sm');
const readerFont = computed(() => TEXT_SIZES[textSize.value]);
function setTextSize(key: TextSizeKey) {
  textSize.value = key;
  try { localStorage.setItem(TEXT_SIZE_KEY, key); } catch { /* 隐私模式下忽略 */ }
}
try {
  const saved = localStorage.getItem(TEXT_SIZE_KEY) as TextSizeKey | null;
  if (saved && saved in TEXT_SIZES) textSize.value = saved;
} catch { /* ignore */ }

const scroller = ref<HTMLElement | null>(null);

/* ------------------------------ 派生数据 ------------------------------ */
const groups = computed(() => buildPageGroups(
  pages.value, pageTranslations.value, blocks.value,
));
const allRows = computed(() => flattenRows(groups.value));
const rowById = computed(() => {
  const map = new Map<string, PageRow>();
  for (const r of allRows.value) map.set(r.id, r);
  return map;
});
const currentRow = computed(() => rowById.value.get(currentRowId.value) ?? null);
const currentPage = computed(() => currentRow.value?.page ?? 1);

/** 逐页翻译是否已全部完成（完成时不再需要实时状态/继续翻译）。 */
const translateDone = computed(() => {
  const total = stats.value.pages ?? 0;
  return total > 0 && (stats.value.pages_done ?? 0) >= total;
});

/**
 * 目录高亮：当前内容行 → **文档顺序中它之前最近的一个标题**。
 *
 * 直接把 currentRowId 交给目录是不对的：滚动经过正文时当前行是段落，
 * 段落 id 永远匹配不上任何标题 id，于是目录高亮会“亮了又灭、灭了又亮”（乱跳）。
 */
const tocActiveId = computed(() => {
  const order = allRows.value;
  const idx = order.findIndex((r) => r.id === currentRowId.value);
  if (idx < 0) return '';
  const headingIds = new Set(tocRows.value.map((r) => r.id));
  for (let i = idx; i >= 0; i -= 1) {
    if (headingIds.has(order[i].id)) return order[i].id;
  }
  return '';
});
const progressPercent = computed(() => pct(stats.value.pages_done ?? 0, stats.value.pages ?? 0));
const originalVisible = computed(() => viewMode.value !== 'translation');
const originalIsPage = computed(() => originalVisible.value && originalMode.value === 'page');
/** 对照 + 原 PDF：一边是页图、一边是文字行 —— 只有这时"文字列放译文还是原文"才是个选择 */
const pageLeft = computed(() => viewMode.value === 'bilingual' && originalMode.value === 'page');
const leftIsOriginal = computed(() => pairOrder.value !== 'zh_left' && pairOrder.value !== 'zh_top');
const tocRows = computed(() => flattenOutline(outline.value.tree, new Set(collapsed.value)));
const activeTask = computed(() => liveTask.value
  ?? tasks.value.find((t) => t.type === 'TRANSLATE_MD'
    && ['processing', 'pending', 'paused'].includes(t.status))
  ?? null);
/** 后台正在翻译这篇论文：还没译文的行显示「等待翻译」，而不是「未翻译」 */
const translating = computed(() => !!activeTask.value);
/**
 * 后台正在**解析**这篇论文。
 *
 * 阅读器在解析完成前没有任何页数据，过去一律显示"这篇论文还没有解析结果"，
 * 用户会以为解析没跑起来（实际上任务正在跑，而且后端会顺带接着翻译）。
 * 这里把进行中的 PARSE 任务挑出来，空状态改成"正在解析 + 阶段 + 进度"。
 */
const parseTask = computed(() => tasks.value.find((t) => t.type === 'PARSE'
  && ['processing', 'pending', 'paused'].includes(t.status)) ?? null);
/**
 * 这篇论文有**解读任务**在跑（A/B 两路之一）。
 *
 * 解读是后台全篇调用（实测 1~2 分钟），面板据此决定要不要每 5s 轮询：
 * 没有任务还一直打接口纯属白耗（用户口径：只在有理解任务在跑时轮询）。
 */
const understandingRunning = computed(() => tasks.value.some((t) => (
  t.type === 'UNDERSTAND_A'
) && ['processing', 'pending', 'paused'].includes(t.status)));
/**
 * 有任何任务在跑（不限类型）：任务页签的 4s 轮询跟着它走。
 *
 * 以前这里只认翻译/解析，于是索引、转录、解读这些任务在跑的整个过程中，
 * 任务页签停在"没有任务"的旧状态上（用户报的就是这个）。
 */
const hasRunningTask = computed(() => tasks.value.some((t) =>
  ['processing', 'pending', 'paused'].includes(t.status)));
/** 任务消息形如"解析中：render 3/22" —— 只留阶段本身，免得与前面的"正在解析"重复一遍 */
const parseStageLabel = computed(() => String(parseTask.value?.message ?? '')
  .replace(/^解析中\s*[：:]\s*/, '').trim());
const isEmpty = computed(() => !loading.value && !fatal.value && !pages.value.length && !allRows.value.length);
/** 行数据到齐没（深链定位据此决定"再等等"还是"确实找不到"） */
const rowsReady = computed(() => !loading.value && !fatal.value && allRows.value.length > 0);
/** 后端有没有下发"块 id → 正文行 id"的映射表（老后端没有，就只能按页跳） */
const rowMapReady = computed(() => Object.keys(reader.value?.block_rows ?? {}).length > 0);
/** 原 PDF 栏的列宽（对照时只占一半，单栏时可放大） */
const pageWidth = computed(() => {
  const col = viewMode.value === 'bilingual' ? paneWidth.value / 2 - 40 : paneWidth.value - 40;
  const wanted = Math.round(Math.max(220, col) * zoom.value);
  return Math.max(200, Math.min(wanted, Math.max(220, col * 2)));
});

/* ------------------------- 本机阅读统计（打开次数 / 时长） -------------------------
 * 后端 `reading_progress` 里没有这两个数（见 `utils/readerStats.ts` 的说明），所以记在本机：
 * 进阅读器算一次"打开"；时长**只在页面可见时**累加，每 30 秒落一次盘，
 * 切走标签页 / 离开阅读器时再补最后那一段 —— 否则"切到别的页面挂着"会被算成一直在读。
 */
let readActiveSince = 0;
let readFlushTimer: number | null = null;

function flushReadSeconds() {
  if (!readActiveSince) return;
  const sec = Math.round((Date.now() - readActiveSince) / 1000);
  readActiveSince = Date.now();
  if (sec > 0) addReaderSeconds(instancePaperId, sec);
}

function armReadClock() {
  readActiveSince = document.visibilityState === 'visible' ? Date.now() : 0;
}

function onReadVisibility() {
  if (document.visibilityState === 'visible') { armReadClock(); return; }
  flushReadSeconds();
  readActiveSince = 0;
}

/** 进阅读器时调用：这次算一次"打开"，并开始计时 */
function startReadStats() {
  noteReaderOpen(instancePaperId);
  armReadClock();
  if (readFlushTimer) window.clearInterval(readFlushTimer);
  readFlushTimer = window.setInterval(flushReadSeconds, 30000);
}

function stopReadStats() {
  flushReadSeconds();
  readActiveSince = 0;
  if (readFlushTimer) { window.clearInterval(readFlushTimer); readFlushTimer = null; }
}

/* ------------------------------ 加载 ------------------------------ */
/** 上一轮是否在解析（解析结束时自动刷新阅读器数据） */
let wasParsing = false;
/** 兜底只补一次"看起来还是空的"重载，避免接口抖动时反复打 */
let reloadedForEmpty = false;

/**
 * 解析完成 → **阅读器 + 侧栏**一起刷新。
 *
 * 用户报（2026-09-27）：在解析中进阅读页、等页面进度条跑完，**裁剪后的图片**与**侧栏内容**
 * 都还要刷新页面才出现。以前这里只调 `loadAll()`：正文的行/统计/目录会更新，但侧栏的
 * 插图（`/figures`）、术语（`/glossary`）、简报（`/brief`）、公式（`/formulas`）是
 * `loadSide()` / `loadFormulas()` **分别取的另外几个接口** —— 没人再打过，就一直停在旧数据；
 * 解读（`useUnderstanding` 整包缓存）同理。
 */
async function reloadAfterParse() {
  await loadAll();
  await loadSide();
  refreshOutline();
  void loadUnderstanding(instancePaperId, { force: true, silent: true });
}

async function loadAll() {
  loading.value = true;
  fatal.value = null;
  try {
    const data = await withLoading('reader', () => api.get<ReaderPayload>(`/papers/${paperId.value}/reader`));
    reader.value = data;
    resetInk(paperId.value);
    inkView.value = viewMode.value;
    blocks.value = data.blocks ?? [];
    pages.value = data.pages ?? [];
    pageTranslations.value = data.page_translations ?? [];
    annotations.value = data.annotations ?? [];
    highlights.value = data.highlights ?? [];
    notes.value = data.notes ?? [];
    tasks.value = (data.tasks ?? []) as unknown as TaskInfo[];
    /* 用**刚拿到的**任务状态同步一次"上一轮是否在解析"（见 `fetchTasks` 里的解析完成检测）。
     * 不这么做会漏一种时序：用户打开阅读页时解析**刚好已经跑完** —— 第一次 `fetchTasks()`
     * 看到的就是"没有解析任务"，`wasParsing` 从没变过 true，于是那次"完成"没人管，
     * 页面停在"正在解析"前的空数据上，只能靠刷新。 */
    wasParsing = !!parseTask.value;
    stats.value = data.stats ?? stats.value;
    outline.value = data.outline ?? { items: [], tree: [], count: 0 };
    // 顶栏标签的标题换成论文名（路由只能给"论文"这个占位标题）
    setTabTitle(routeKey(route), data.paper?.title || paperId.value);
    const p = data.progress;
    viewMode.value = p?.view_mode ?? 'bilingual';
    pairOrder.value = (p?.pair_order as PairOrder) ?? 'en_left';
    // 原文栏呈现也按论文存（`text` / `page`）：上次看的是原 PDF，切标签或重开时还是原 PDF
    originalMode.value = (p?.original_mode as OriginalMode) ?? 'text';
    // 界面状态（侧栏开合 / 字号 / 原 PDF 缩放）：**服务端那份优先**（按论文记的），
    // 没记过才用本地偏好（`localStorage`）或默认值。（参考文献恒显示后不再是界面状态。）
    if (typeof p?.aside_open === 'boolean') asideOpen.value = p.aside_open;
    if (p?.text_size && p.text_size in TEXT_SIZES) textSize.value = p.text_size as TextSizeKey;
    if (typeof p?.zoom === 'number') zoom.value = Math.min(3, Math.max(0.4, p.zoom));
    // 对照 + 原 PDF 时文字列放哪一侧（`zh` 译文 / `en` 原文）
    pageSide.value = p?.page_side === 'en' ? 'en' : 'zh';
    const saved = p?.last_block_id ?? '';
    currentRowId.value = (saved && rowById.value.has(saved))
      ? saved
      : (allRows.value.find((r) => r.page === (p?.last_page ?? 1))?.id
         ?? allRows.value[0]?.id ?? '');

    // 深链参数：?view=original|translation|bilingual  ?order=en_left|zh_left
    //           ?left=text|page  ?size=sm|md|lg  ?row=<id>（兼容旧参数 ?block=）
    const q = route.query;
    if (q.view && ['original', 'translation', 'bilingual'].includes(String(q.view))) {
      viewMode.value = String(q.view) as ViewMode;
    }
    if (q.order === 'en_left' || q.order === 'en_top') pairOrder.value = 'en_left';
    else if (q.order === 'zh_left' || q.order === 'zh_top') pairOrder.value = 'zh_left';
    if (q.left === 'page' || q.left === 'text') originalMode.value = q.left;
    if (q.pside === 'en' || q.pside === 'zh') pageSide.value = q.pside;
    if (q.size === 'sm' || q.size === 'md' || q.size === 'lg') setTextSize(q.size);
    // 深链搜索：?q=xxx 直接跑一遍（自检脚本与"分享一条检索"都用得上）；?scope=all|en|zh 指定范围
    if (q.scope === 'all' || q.scope === 'en' || q.scope === 'zh') searchScope.value = q.scope;
    if (q.q) { searchQuery.value = String(q.q); runSearch(); }
    const sideParam = String(q.side ?? '');
    if (SIDE_TAB_KEYS.includes(sideParam) || sideParam === 'annotations') {
      // 兼容旧的 ?side=annotations：批注与笔记已合并进「笔记」这一个页签
      sideTab.value = (sideParam === 'annotations' ? 'notes' : sideParam) as SideTabKey;
      /* `?side=insight` / `?side=chat` 还要**把左栏打开**：回到左栏之后，"选中页签"本身
       * 看不见就等于没反应（用户把左栏收起过 / 窄屏是抽屉）。宽度由 `watch(sideTab)` 统一负责。 */
      if (sideParam === 'insight' || sideParam === 'chat') await ensureAsideVisible();
    }

    await nextTick();
  } catch (err) {
    fatal.value = err as Error;
  } finally {
    loading.value = false;
  }
}

/**
 * 深链定位：`?row=<锚点>`（兼容旧的 `?block=`）+ 可选 `?page=<页码>`。
 *
 * **三级解析**（缺一不可，2026-09 修的真实事故：知识库点「跳回原文」什么都不发生）：
 *
 * 1. `row` 直接是正文行 id（`p001-06`）或该页未翻译时的块 id → 精确落行；
 * 2. 检索/RAG 给的是**解析块 id**（`attentionisa-p01-b06`），已翻译页的正文行是页级产物的
 *    位置编号，两边根本不是一套 id → 用后端下发的 `block_rows` 翻成行 id；
 * 3. 还没有（公式/插图/表格在正文行里没有独立段落，或锚点因重译失效）→ 退到 `page` 那一页，
 *    并**明确说明**"已跳到第 N 页"，不假装精确。
 *
 * `deepLinkDone` 记的是"已经执行过的定位签名"：既防重复滚动，也防下面 `syncViewQuery()`
 * 写回 URL（`replace`）时把自己再触发一遍。
 */
const deepLinkDone = ref('');
/** 已经"尽力过"的锚点签名：只提示一次，别在 SSE 逐页回填时反复弹 */
const deepLinkTried = ref('');
/** 挂载流程（载数据 → 恢复位置 → 深链定位）走完没 */
const bootDone = ref(false);

/** 请求里带没带深链锚点（`?row=` / 兼容的 `?block=` / `?page=`）。 */
function hasDeepLink(): boolean {
  const q = route.query;
  return Boolean(q.row ?? q.block ?? q.page);
}

async function applyDeepLink(): Promise<boolean> {
  const raw = route.query.row ?? route.query.block;
  const anchor = raw === undefined || raw === null ? '' : String(raw);
  const wantPage = Number(route.query.page ?? 0) || 0;
  if (!anchor && !wantPage) return true;
  const sig = `${anchor}|${wantPage}`;
  if (deepLinkDone.value === sig) return true;

  const mapped = anchor
    ? (rowById.value.has(anchor) ? anchor : String(reader.value?.block_rows?.[anchor] ?? ''))
    : '';
  const row = mapped ? rowById.value.get(mapped) : undefined;
  if (row) {
    deepLinkDone.value = sig;
    currentRowId.value = row.id;
    await nextTick();
    await focusRow(row.id, { smooth: false, save: false, flash: true });
    return true;
  }

  if (wantPage) {
    deepLinkDone.value = sig;
    const first = allRows.value.find((r) => r.page === wantPage);
    if (first) {
      currentRowId.value = first.id;
      await nextTick();
      await focusRow(first.id, { smooth: false, save: false, flash: true });
    } else {
      await focusPage(wantPage);
    }
    if (anchor && !loading.value) {
      // 有映射表 + 行齐了，才敢断言"这一段在正文里没有独立行"（公式/插图/表格）；
      // 否则老实说"没找到精确位置"（老后端不下发 block_rows 时也会走到这里）。
      const precise = rowMapReady.value && rowsReady.value;
      toast(precise
        ? `该段落没有独立正文行（公式 / 插图 / 表格），已跳到第 ${wantPage} 页`
        : `未找到该段落的精确位置，已跳到第 ${wantPage} 页`, 'warn', 3200);
    }
    return true;
  }

  // 锚点查不到、也没有页码兜底：等数据到齐（行是 payload + SSE 分两批来的）再判定
  if (rowsReady.value && deepLinkTried.value !== sig) {
    deepLinkTried.value = sig;
    toast('该定位锚点已失效（重译后行号已变）', 'warn', 2600);
  }
  return false;
}

/** 目标变了（点了另一个检索结果 / 从别处再跳一次同一个锚点）就重新定位。 */
watch(() => `${route.query.row ?? ''}|${route.query.block ?? ''}|${route.query.page ?? ''}`,
  () => { void applyDeepLink(); });

/**
 * 行是**分两批**到的（reader payload + SSE 逐页回填），深链可能在第一批里还查不到目标：
 * 每来一行就再试一次，直到解析成功。已经定位过的直接跳过（不再打扰用户的滚动）。
 *
 * `bootDone` 之前不重试：挂载流程里 `restorePosition`（恢复上次读到哪）与深链定位
 * 都是"滚到某一行"，谁后跑谁赢 —— 让重试在挂载流程之后才介入，顺序才是确定的。
 */
watch(() => allRows.value.length, () => {
  if (!bootDone.value || deepLinkDone.value) return;
  void applyDeepLink();
});

async function loadSide() {
  try {
    const [g, b, gl] = await Promise.all([
      api.get<{ items: FigureItem[] }>(`/papers/${paperId.value}/figures`),
      api.get<{ brief: BriefPayload | null }>(`/papers/${paperId.value}/brief`),
      api.get<{ items: GlossaryTerm[] }>(`/papers/${paperId.value}/glossary`),
    ]);
    figures.value = g.items ?? [];
    brief.value = b.brief ?? null;
    glossary.value = gl.items ?? [];
  } catch (err) {
    console.warn('侧栏数据加载失败', err);
  }
  void loadFormulas();
}

/** 公式：**译文优先、未翻译的页用原文兜底**（口径见后端 `reader_service.markdown_formulas`） */
async function loadFormulas() {
  formulasLoading.value = true;
  try {
    const d = await api.get<{ items: MarkdownFormula[]; source?: string }>(
      `/papers/${paperId.value}/formulas`);
    formulas.value = d.items ?? [];
    formulasSource.value = String(d.source ?? 'markdown');
  } catch (err) {
    console.warn('公式提取失败', err);
    formulas.value = [];
    formulasSource.value = 'markdown';
  } finally {
    formulasLoading.value = false;
  }
}

function restorePosition(p?: { last_block_id?: string | null; last_page?: number; scroll_ratio?: number } | null) {
  if (p?.last_block_id && rowById.value.has(p.last_block_id)) {
    focusRow(p.last_block_id, { smooth: false, save: false });
    return;
  }
  if (typeof p?.scroll_ratio === 'number' && p.scroll_ratio > 0) {
    const el = scroller.value;
    if (el) el.scrollTop = el.scrollHeight * p.scroll_ratio;
  }
}

function syncPaneWidth() {
  const el = scroller.value;
  if (el?.clientWidth) paneWidth.value = el.clientWidth;
}

/**
 * 窗口尺寸变化：**非对照模式的上限与视口挂钩**（宽了能更宽、窄了要收回来），
 * 顺带重算正文栏宽 —— 两件事都得做，所以合成一个监听（原来只挂了 `syncPaneWidth`）。
 */
function onWindowResize() {
  // 窗口变小后胶囊可能落到允许区域外（左右是正文列、下方要留弹出层）
  if (capsuleEl.value) { enforceCapsuleBounds(); syncToastDrop(); placeCapPanel(); }
  winWidth.value = window.innerWidth;
  syncPaneWidth();
}

/* ------------------------------ 侧栏拖拽改宽度 ------------------------------ */
/** 拖拽中：关掉宽度过渡（`.reader-aside` 上有 0.24s 的 transition，不关就是"拖起来黏手"） */
const asideResizing = ref(false);
let resizeStartX = 0;
let resizeStartW = 0;
/** 拖拽中的把手元素与 pointerId（收尾时要按它们释放捕获；见 `onResizeUp`） */
let resizeEl: HTMLElement | null = null;
let resizePointerId = -1;

/**
 * 用 **pointer 事件 + `setPointerCapture`**：鼠标拖出侧栏（甚至拖出窗口）再回来时事件不会丢，
 * 不捕获的话手指/鼠标一离开那条 9px 的把手就断，宽度会停在半路。
 *
 * ⚠️ 捕获的另一面（用户 2026-09-27 报的"拖完侧栏后鼠标碰不到页面滚动条、没法用滚动条翻页"）：
 * 一旦 `pointerup` 没送到（在**窗口外**松手、浏览器吞掉事件、拖拽中切走标签页），
 * `asideResizing` 就永远停在 true —— 指针被那条 9px 把手**持续捕获**，
 * 之后所有点击都当成"继续拖宽度"，页面上任何东西（含最右侧的滚动条）都点不到。
 * 所以下面加了三层兜底：窗口级 pointerup/pointercancel/blur、`lostpointercapture`、
 * 以及"移动时发现没有按键按下就地收尾"。
 */
function onResizeDown(ev: PointerEvent) {
  if (isNarrow.value) return;              // 窄屏是抽屉、宽度由 CSS 定死（min(360px, 88vw)），不给拖
  asideResizing.value = true;
  resizeStartX = ev.clientX;
  resizeStartW = asideWidth.value;
  resizeEl = ev.currentTarget as HTMLElement;
  resizePointerId = ev.pointerId;
  resizeEl.setPointerCapture?.(ev.pointerId);
  window.addEventListener('pointerup', onResizeUpAnywhere, true);
  window.addEventListener('pointercancel', onResizeUpAnywhere, true);
  window.addEventListener('blur', onResizeUpAnywhere, true);
  ev.preventDefault();                      // 别把拖拽变成"选侧栏里的文字"
}

/** 拖拽实时夹取走 `clampAsideWidth`：**当前模式的上限**在这里也要生效（用户口径），
 *  只在初始化时算一次的话，对照模式下能把侧栏拖到 640、把译文挤没。 */
function onResizeMove(ev: PointerEvent) {
  if (!asideResizing.value) return;
  if (ev.buttons === 0) {                   // 兜底：`pointerup` 丢了，但按键其实已经松开
    void onResizeUp();
    return;
  }
  asideWidth.value = clampAsideWidth(resizeStartW + (ev.clientX - resizeStartX));
}

function onResizeUpAnywhere() {
  void onResizeUp();
}

/** 拖完才落盘（每像素写一次 localStorage 没意义），并重算正文宽度。
 *  这里**必须幂等**：模板的 pointerup/pointercancel/lostpointercapture 与窗口级兜底
 *  可能重复调用它。 */
async function onResizeUp() {
  window.removeEventListener('pointerup', onResizeUpAnywhere, true);
  window.removeEventListener('pointercancel', onResizeUpAnywhere, true);
  window.removeEventListener('blur', onResizeUpAnywhere, true);
  const el = resizeEl;
  const pid = resizePointerId;
  resizeEl = null;
  resizePointerId = -1;
  if (el && pid >= 0 && el.hasPointerCapture?.(pid)) {
    try { el.releasePointerCapture(pid); } catch { /* 早已释放 */ }
  }
  if (!asideResizing.value) return;
  asideResizing.value = false;
  persistAsideWidth();
  await afterAsideResize();
}

/** 双击把手回默认宽度（拖歪了有个一键回退）。默认宽度同样按当前模式的上限夹一下，
 *  免得在对照模式（上限 360）下把把手复位成比上限还宽的值。 */
async function resetAsideWidth() {
  if (isNarrow.value) return;
  asideWidth.value = clampAsideWidth(ASIDE_W_DEFAULT);
  persistAsideWidth();
  await afterAsideResize();
}

/**
 * 列宽变了之后的收尾：正文栏宽度重算 + **行偏移缓存作废**。
 * 后者不能漏：行高整篇都随列宽变，缓存过期后按旧高度二分，"当前行"会错到别的页上
 * （与 `watch([groups, viewMode, ...])` 里那次失效同一个原因）。
 */
async function afterAsideResize() {
  offsetsHeight = -1;
  await nextTick();
  syncPaneWidth();
  watchContentSize();
}

/* ------------------------------ 定位 ------------------------------ */
/** 「就是这一块」的短暂提醒：跳过去之后让整行亮一下再淡出（CSS 动画，见 ReaderPageRows）。 */
const flashRowId = ref('');
/**
 * 第几次闪。**每次都 +1**，`ReaderPageRows` 用它把动画名在 `flash-a` / `flash-b` 之间换。
 *
 * 为什么必须换名字（2026-10-03 实测）：连点**同一个**引用标号时，下面这段"摘掉类再挂回"
 * 的两次写 DOM 落在同一个任务里（`await nextTick()` 只等补丁、不等一帧），样式引擎把它合并成
 * "没变" —— `getAnimations()` 前后拿到的是**同一个** CSSAnimation 实例，它已经
 * `forwards` 停在 opacity:0，于是用户什么都看不见（"再点同一个标号不亮了，先点别的再点回来才行"）。
 * 换动画名会让计算样式真的不同，浏览器只能重播一遍。
 */
const flashNonce = ref(0);
let flashTimer: ReturnType<typeof setTimeout> | null = null;

function flashRow(id: string) {
  if (!id) return;
  if (flashTimer) clearTimeout(flashTimer);
  flashNonce.value += 1;
  flashRowId.value = id;
  flashTimer = setTimeout(() => { flashRowId.value = ''; flashTimer = null; }, 1600);
}

/**
 * 定位到某一行（或退化为某一页）。
 *
 * `page` 是锚点失效时的兜底：批注/笔记/插图锚点可能是**历史产物**里的行号
 * （重译后行号会变），这时按页跳转而不是乱跳到第 1 页。
 * `flash`：点搜索命中 / 目录这些"跳到这里"的动作要亮一下；滚动恢复、切视图模式不要。
 */
async function focusRow(
  id: string,
  opts: { smooth?: boolean; save?: boolean; page?: number; flash?: boolean } = {},
) {
  const { smooth = true, save = true } = opts;
  const known = rowById.value.get(id);
  const page = opts.page ?? known?.page ?? currentPage.value ?? 1;
  if (!id && !page) return;
  if (id && !known && opts.page === undefined) {
    // 历史锚点（重译后行号变了，且后端也没能给出页码）：明确提示，不乱跳
    toast('该定位锚点已失效（重译后行号已变）', 'warn', 2400);
    return;
  }
  currentRowId.value = known ? id : currentRowId.value;
  mobilePane.value = '';
  await nextTick();
  const el = scroller.value;
  const node = (id ? el?.querySelector<HTMLElement>(`[data-row-id="${id}"]`) : null)
    ?? el?.querySelector<HTMLElement>(`[data-page="${page}"]`);
  if (el) spyLockUntil = performance.now() + 1200;
  if (node && el) {
    // 把目标行停在**阅读线**上（而不是居中）：滚动结束后"当前行"正好还是它，
    // 目录高亮才不会落到上一个章节。
    // 视口位置 = 行内容坐标 - scrollTop，要让它落在阅读线上就解 scrollTop = top - line；
    // 再 +2px 让行顶略微越过阅读线，避免浮点误差把"当前行"判给上一行。
    const base = el.getBoundingClientRect().top - el.scrollTop;
    const top = node.getBoundingClientRect().top - base;
    el.scrollTo({ top: Math.max(0, top - readingLine() + 2), behavior: smooth ? 'smooth' : 'auto' });
  } else {
    node?.scrollIntoView({ behavior: smooth ? 'smooth' : 'auto', block: 'center' });
  }
  if (opts.flash) void flashRow(id);
  if (save) saveProgressDebounced();
}

/** 跳到某一页（目录/页导航用） */
async function focusPage(page: number) {
  const row = allRows.value.find((r) => r.page === page);
  if (row) await focusRow(row.id);
  else {
    const el = scroller.value;
    el?.querySelector<HTMLElement>(`[data-page="${page}"]`)?.scrollIntoView({ behavior: 'smooth' });
  }
}

/**
 * 跳到某一页并**把页码写进 URL**（`?page=N`）：语义搜索命中与「来源 Chunk」胶囊都走这里。
 *
 * 为什么不能只调 `focusPage`：
 * - 深链定位（`applyDeepLink`）本来就监听 `?page=`，写进 URL 才与其它"跳到某页"的入口同一条路 ——
 *   切标签回来、分享链接都能落回同一页（标签条记的是每个标签的完整路由）；
 * - 只滚一下的话，刷新 / 切标签就回到旧位置，看起来像"跳转没生效"。
 *
 * ⚠️ 必须先摘掉 `?row=` / `?block=`：`applyDeepLink` 里**锚点优先于页码**（锚点能精确落行），
 * 留着旧锚点的话点胶囊会被它拽回原来那一行，表现就是"点了没反应"。
 */
async function gotoPage(page: number) {
  // 页码一律按数字走：命中里偶尔给成字符串（"7"）时，`focusPage` 的 `r.page === page` 会不成立
  const target = Number(page) || 0;
  if (!target) return;
  const hadAnchor = route.query.row !== undefined || route.query.block !== undefined;
  const samePage = String(route.query.page ?? '') === String(target);
  const q = { ...route.query } as Record<string, string | string[] | undefined>;
  delete q.row;
  delete q.block;
  q.page = String(target);
  await router.replace({ path: route.path, query: q });
  // 路由串没变化时 `applyDeepLink` 不会被触发（那个 watch 只看 query 的快照），自己滚一次；
  // 真变了就交给它 —— 它带"闪一下"的提示，这里再滚一次反而会跟它抢落点。
  if (samePage && !hadAnchor) await focusPage(target);
}

async function stepRow(delta: number) {
  const list = allRows.value;
  if (!list.length) return;
  const idx = list.findIndex((r) => r.id === currentRowId.value);
  const next = list[Math.min(list.length - 1, Math.max(0, idx + delta))];
  if (next) await focusRow(next.id, { smooth: false });
}

/* ------------------------------ 进度持久化 ------------------------------ */
async function saveProgressNow() {
  try {
    const el = scroller.value;
    const ratio = el && el.scrollHeight > el.clientHeight
      ? Math.min(1, Math.max(0, el.scrollTop / (el.scrollHeight - el.clientHeight)))
      : 0;
    // 用 `instancePaperId`（本实例的论文）而不是路由的当前值：切标签时这条请求可能
    // 在路由已经变化之后才发出，用路由值会把进度写到**另一篇**论文上。
    await api.put(`/papers/${instancePaperId}/progress`, {
      last_block_id: currentRowId.value || null,
      last_page: currentPage.value,
      percent: progressPercent.value,
      scroll_ratio: Number(ratio.toFixed(4)),
      view_mode: viewMode.value,
      pair_order: pairOrder.value,
      original_mode: originalMode.value,
      // 界面状态也一起带着：它们和视图模式同属"这篇论文上次是怎么看的"
      aside_open: asideOpen.value,
      text_size: textSize.value,
      // 参考文献恒显示（开关已移除）：字段保留写 `true`，旧数据/对账脚本仍能读到一致的值
      show_refs: true,
      zoom: Number(zoom.value.toFixed(2)),
      page_side: pageSide.value,
    });
  } catch (err) { console.warn('保存阅读进度失败', err); }
}
const saveProgressDebounced = debounce(saveProgressNow, 900);

/**
 * 界面状态（侧栏开合 / 字号 / 原 PDF 缩放）一变就**立刻**存一次：
 * 它们和视图模式一样是"这篇论文上次是怎么看的"，切标签或重开时要还原（用户口径）。
 *
 * `loading` 期间不回存：那一刻正把服务端那份灌进来，回存等于自己写自己。
 * （灌值发生在 `loadAll()` 里，而它的 `await nextTick()` 正好让这个 watcher 在
 *  `loading` 还是 true 的时候跑完。）
 */
watch([asideOpen, textSize, zoom], () => {
  if (loading.value || fatal.value) return;
  void saveProgressNow();
});

/**
 * 把"这篇论文现在是怎么看的"（视图 / 原文栏呈现 / 左右顺序）同步进 URL query。
 *
 * 为什么同时要写 URL：标签条记住的是**每个标签上次的路由位置（含 query）**，
 * 点标签切回来时由 `tabLocation` 原样跳回去 —— 写进 query，切标签就能立刻还原，
 * 不必等服务端进度那趟往返（快切标签时它可能还没落库）。
 * 服务端那份（`reading_progress`）负责"关掉标签/重开浏览器之后仍然记得"。
 * 用 `replace`：这不是一次导航，别往历史里塞垃圾。
 */
function syncViewQuery() {
  if (loading.value || fatal.value) return;
  const q = route.query;
  if (String(q.view ?? '') === viewMode.value
      && String(q.left ?? '') === originalMode.value
      && String(q.order ?? '') === pairOrder.value
      && String(q.pside ?? '') === pageSide.value) return;
  void router.replace({
    // 显式带上当前 path：只给 `query` 的写法在 vue-router 4 里不一定能解析成"当前位置改 query"
    path: route.path,
    query: {
      ...q, view: viewMode.value, left: originalMode.value,
      order: pairOrder.value, pside: pageSide.value,
    },
  });
}

/* 侧栏页签也写进 URL query：标签条记住的是"每个标签上次的路由位置（含 query）"，
   不写进去的话切到别的标签再切回来就退回默认页签（用户口径 2026-10-03："侧栏点了哪个页面没记住"）。
   为什么挂在 watch 上而不是点击处理器里：页签还会被别处改（搜索命中、划词进对话、工具栏「看解读」、
   `?side=` 深链），watch 一处覆盖全部；顺带地址栏也能如实反映当前在看哪一栏。
   与 `syncViewQuery` 同一套口径：`replace`、不堆历史、值没变就不写。 */
watch(sideTab, (k) => {
  if (loading.value || fatal.value) return;
  if (String(route.query.side ?? '') === k) return;
  void router.replace({ path: route.path, query: { ...route.query, side: k } });
});

/* ------------------------------ 视图切换 ------------------------------ */
async function setViewMode(mode: ViewMode) {
  if (viewMode.value === mode) return;
  viewMode.value = mode;
  // 每个视图各存一份笔迹：切视图 = 换一份批注（见 useReaderInk 的 pageKey）
  inkView.value = mode;
  mobilePane.value = '';
  await nextTick();
  syncPaneWidth();
  syncViewQuery();
  void saveProgressNow();
  void focusRow(currentRowId.value, { smooth: false, save: false });
}

async function setPairOrder(order: PairOrder) {
  if (pairOrder.value === order) return;
  pairOrder.value = order;
  await nextTick();
  syncViewQuery();
  void saveProgressNow();
  void focusRow(currentRowId.value, { smooth: false, save: false });
}

function setOriginalMode(mode: OriginalMode) {
  if (originalMode.value === mode) return;
  originalMode.value = mode;
  zoom.value = 1;
  syncViewQuery();
  void saveProgressNow();          // 以前这里不落库，于是"原 PDF"切标签就丢了
  nextTick(() => { syncPaneWidth(); void focusRow(currentRowId.value, { smooth: false, save: false }); });
}

/** 对照 + 原 PDF 时，右侧文字列在**译文 / 原文**之间切换（默认译文） */
function setPageSide(side: 'en' | 'zh') {
  if (pageSide.value === side) return;
  pageSide.value = side;
  syncViewQuery();
  void saveProgressNow();
  nextTick(() => { void focusRow(currentRowId.value, { smooth: false, save: false }); });
}

/* ------------------------------ 目录折叠 ------------------------------ */
function toggleCollapse(id: string) {
  const set = new Set(collapsed.value);
  if (set.has(id)) set.delete(id); else set.add(id);
  collapsed.value = [...set];
}
const collapseAll = () => { collapsed.value = outlineParentIds(outline.value.tree); };
const expandAll = () => { collapsed.value = []; };

/* ------------------------------ 滚动跟踪 ------------------------------ */
/** 行偏移缓存：内容/高度变化时测一次，滚动时只做二分查找（零布局读取）。 */
let offsets: Array<{ id: string; top: number }> = [];
let offsetsHeight = -1;

/**
 * 内容高度一变就让偏移缓存失效。
 *
 * KaTeX / 插图 / 字体都是**异步**就位的，只比较 scrollHeight 会漏（变化不足 2px 不失效），
 * 缓存过期后二分查找就会落到错行 —— 表现出来就是"当前行"乱跳、目录跟着闪。
 */
let contentRo: ResizeObserver | null = null;
function watchContentSize() {
  contentRo?.disconnect();
  const el = scroller.value?.querySelector<HTMLElement>('.rows-wrap');
  if (!el || typeof ResizeObserver === 'undefined') return;
  contentRo = new ResizeObserver(() => { offsetsHeight = -1; });
  contentRo.observe(el);
  bindScrollerEvents();
}

function measureRows() {
  const el = scroller.value;
  if (!el) return;
  const base = el.getBoundingClientRect().top - el.scrollTop;
  offsets = Array.from(el.querySelectorAll<HTMLElement>('[data-row-id]'))
    .map((node) => ({ id: node.dataset.rowId ?? '', top: node.getBoundingClientRect().top - base }))
    .filter((o) => o.id);
  offsetsHeight = el.scrollHeight;
}

/**
 * 阅读线："当前行"按视口上方约 30% 处判定，而不是贴顶那一行。
 * 贴顶判定的问题：点目录是让目标行居中（在 30% 线下方），按定义就不算当前行，
 * 于是目录高亮会落到**上一个**章节 —— 看起来就是"乱跳"。
 */
function readingLine(): number {
  const el = scroller.value;
  if (!el) return 120;
  return Math.min(260, Math.max(80, el.clientHeight * 0.3));
}

/** 程序化跳转（点目录 / 点批注 / 快捷键）期间锁住跟踪，避免平滑滚动扫过一串章节。 */
let spyLockUntil = 0;

/** 滚动停下来就立即放行并重算一次（不必等锁定超时）。 */
function onScrollEnd() {
  spyLockUntil = 0;
  onScroll();
}

/** 把 scrollend 绑到真正的滚动元素上（支持则生效，不支持就靠锁定超时）。 */
function bindScrollerEvents() {
  const el = scroller.value;
  if (!el) return;
  el.removeEventListener('scrollend', onScrollEnd);
  if ('onscrollend' in el) el.addEventListener('scrollend', onScrollEnd, { passive: true });
}

function onScroll() {
  const el = scroller.value;
  if (!el) return;
  if (!offsets.length || Math.abs(offsetsHeight - el.scrollHeight) > 2) measureRows();
  if (performance.now() < spyLockUntil) { saveProgressDebounced(); return; }
  const y = el.scrollTop + readingLine();
  let lo = 0;
  let hi = offsets.length - 1;
  let best = offsets[0]?.id ?? '';
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (offsets[mid].top <= y) { best = offsets[mid].id; lo = mid + 1; } else hi = mid - 1;
  }
  if (best && best !== currentRowId.value) currentRowId.value = best;
  saveProgressDebounced();
}

/** 行模型/视图形态/字号/文字列侧变化后让偏移缓存失效（行高会变） */
watch([groups, viewMode, originalMode, pairOrder, pageSide, zoom, textSize], () => {
  offsetsHeight = -1;
  nextTick(() => watchContentSize());
});

/* ------------------------------ LiveUpdater ------------------------------ */
function applyPage(payload: {
  page: number; rows?: PageRow[]; align?: string | null;
  model?: string | null; page_total?: number; updated_at?: string | null;
}) {
  const page = Number(payload.page);
  const rows = payload.rows ?? [];
  const known = pageTranslations.value.find((t) => t.page === page);
  const next = pageTranslations.value.filter((t) => t.page !== page);
  next.push({
    page,
    page_total: payload.page_total ?? pages.value.length,
    original_md: known?.original_md ?? '',
    translation_md: known?.translation_md ?? '',
    rows, align: payload.align ?? null, model: payload.model ?? null,
    updated_at: payload.updated_at ?? null,
  });
  pageTranslations.value = next.sort((a, b) => a.page - b.page);
  const info = pages.value.find((p) => p.page === page);
  if (info) { info.translated = true; info.row_count = rows.length; info.align = payload.align ?? null; }
  stats.value = {
    ...stats.value,
    pages_done: pageTranslations.value.filter((t) => t.rows.length).length,
    rows: pageTranslations.value.reduce((n, t) => n + t.rows.length, 0),
  };
  refreshOutline();
}

/** 目录随译文上线增量刷新（标题带中文） */
const refreshOutline = debounce(async () => {
  try {
    outline.value = await api.get<Outline>(`/papers/${paperId.value}/outline`);
  } catch { /* 目录刷新失败不影响阅读 */ }
}, 800);

/**
 * 一页的**内容指纹**：行 id + 两侧字数。
 *
 * 轮询兜底原来用"行数变了才算变"，而两路模式下这会漏：一页先被 A 路（英文转录）写出来、
 * B 路（中文翻译）稍后补上时，**合并后的行数往往一模一样**（只是 zh 从空变成有），
 * 于是永远不会被应用 —— 用户看到的就是"要刷新页面才能看到实时翻译"（2026-09-27 报的）。
 */
function pageFingerprint(t?: { rows?: PageRow[] } | null): string {
  return (t?.rows ?? [])
    .map((r) => `${r.id}:${(r.original ?? '').length}:${(r.translation ?? '').length}`)
    .join('|');
}

async function pollTranslations() {
  type PagePatch = { page: number; rows?: PageRow[]; align?: string | null; model?: string | null };
  // 用 `instancePaperId`：轮询可能在路由切走之后才回来，用路由值会把**另一篇**的页行
  // 打进本文档（实测表现为切标签后行数变多、内容半新半旧）。
  const d = await api.get<{ items: PagePatch[] }>(`/papers/${instancePaperId}/translations`);
  for (const item of d.items ?? []) {
    const known = pageTranslations.value.find((t) => t.page === item.page);
    if (known && pageFingerprint(known) === pageFingerprint(item)) continue;
    applyPage(item);
  }
}

/** 事件里的行可能为空（旧产物 / 极端情况）：**不要**用它把已有行清空，回源取那一页。 */
async function syncPageFromApi(page: number) {
  try {
    const d = await api.get<{ items: Array<{ page: number; rows?: PageRow[] }> }>(
      `/papers/${instancePaperId}/translations`, { page });
    const item = (d.items ?? []).find((i) => i.page === page);
    if (item) applyPage(item);
  } catch { /* 取不到就保留现状，等下一次刷新 */ }
}

useEventStream({
  url: sseUrl(`/papers/${instancePaperId}/translations/stream`),
  // ⚠️ `page_transcribed` 是 **A 路（英文转录）** 的事件，必须一起订阅：
  // 只订 `page_translated` 时，A 路跑完的事件前端收不到，英文内容要刷新才出现
  // （用户 2026-09-27 报的"每页要刷新才能看到实时翻译和转录结果"）。
  events: ['page_translated', 'page_transcribed', 'paper_status'],
  onEvent: (name, frame) => {
    const payload = frame?.payload ?? {};
    if (name === 'page_translated' || name === 'page_transcribed') {
      if ((payload.rows ?? []).length) applyPage(payload);
      else void syncPageFromApi(Number(payload.page));
    } else if (name === 'paper_status') {
      stats.value = { ...stats.value, pages_done: payload.translate_done ?? stats.value.pages_done };
      void fetchTasks();
    }
  },
  onStatus: (s) => { sseStatus.value = s; },
  poll: pollTranslations,
  pollInterval: 4000,
});

/* ------------------------------ 翻译动作 ------------------------------ */
/** 请求在途（防连点）：任务创建本身很快，但双击会发两次 POST */
const busyTranslating = ref(false);

async function startTranslate(mode: 'resume' | 'retranslate' = 'resume', pages?: number[]) {
  if (busyTranslating.value) return;
  /* **不重复起任务**：已有活着的翻译任务时直接告知并返回（后端也会复用，但前端先说清楚，
   * 否则用户以为又开了一个）。想看进度用「N/M 页」徽章。 */
  if (activeTask.value) {
    const t = activeTask.value;
    toast(`已有翻译任务在跑（${t.done_items ?? 0}/${t.total_items ?? 0}），先停止或等它结束`, 'warn', 2600);
    return;
  }
  busyTranslating.value = true;
  try {
    const task = await api.post<TaskInfo & { reused?: boolean }>(`/papers/${paperId.value}/translate`, {
      mode, brief: true, pages,
      force: mode === 'retranslate',
    });
    const msg = String((task as unknown as { __meta?: { message?: string } })?.__meta?.message ?? '');
    if (task.type === 'PARSE') {
      // 还没解析：后端把解析串起来了（解析完成自动开翻译）——后端文案直接给用户看
      toast(msg || '已创建解析任务，完成后自动开始翻译', 'ok', 4200);
      void fetchTasks();
      return;
    }
    liveTask.value = task;
    if (task.reused) toast('沿用正在进行的翻译任务', 'ok');
    else if (pages?.length) toast(`已创建第 ${pages.join('、')} 页的翻译任务`, 'ok');
    else if (msg) toast(msg, 'ok', 4200);
    else toast('已创建逐页翻译任务', 'ok');
    void fetchTasks();
  } catch (err) { reportError(err, '触发翻译失败'); } finally { busyTranslating.value = false; }
}

async function cancelTranslate(task: TaskInfo | null) {
  if (!task) return;
  // 类型名走 `utils/tasks` 那一份口径：任务页签里叫"转录"，这里就不能叫"翻译"
  const kind = taskTypeLabel(task.type);
  if (!(await askConfirm({
    title: `取消该${kind}任务？`,
    body: '已完成的部分会保留，之后可以从断点继续。',
    confirmLabel: '取消任务',
  }))) return;
  try {
    await api.post(`/tasks/${task.id}/cancel`);
    toast('已请求取消（页边界生效）', 'ok');
    void fetchTasks();
  } catch (err) { reportError(err, '取消失败'); }
}

/**
 * 「重新生成解读」（工具栏「⋯」与论文库卡片「⋯」里的同一项）：`force: true`，全篇精读重跑一次。
 *
 * 已经有解读时先 **HTML 原生 confirm** 二次确认（与论文库同一口径，见 `utils/understanding.ts`）：
 * 重跑是一次全篇模型调用，而它就藏在一个「⋯」菜单里 —— 误点一下就是白花钱白等。
 *
 * 失败**原样**展示后端 `error.message`：最常见的 400 就是那句人话
 * "这篇论文还没有可理解的 chunk：先建索引（知识库页）再来理解" ——
 * 包一层"重新生成解读失败："会把这条可执行的指引淹掉。
 */
async function regenerateInsight() {
  if (!(await confirmRegenerateInsight(paperId.value, reader.value?.paper?.title))) return;
  try {
    const res = await api.post<{ __meta?: { message?: string } }>(
      `/papers/${paperId.value}/understand`, { force: true });
    toast(String(res?.__meta?.message ?? '') || '解读任务已排队，完成后会自动刷新', 'ok', 4200);
    // 起了任务却看不到任何东西，看起来就像"点了没反应"：顺手把左栏的解读页签打开
    await openInsightTab();
    void fetchTasks();           // 让解读内容立刻知道"有任务在跑"，从而开始轮询
  } catch (err) {
    toast((err as Error)?.message ?? String(err), 'error', 6000);
    console.warn('重新生成解读失败', err);
  }
}

async function triggerParse() {
  try {
    // 阅读器空状态的那个按钮：一次点击把「解析 + 翻译」都排上（解析依赖在导航时才会拉起，
    // 但翻译必须在**用户点了**之后才开始 —— 所以这里显式带上 then_translate）。
    const task = await api.post<{ __meta?: { message?: string } }>(
      `/papers/${paperId.value}/parse`, {},
      { force: false, vision: true, then_translate: true });
    toast(String(task?.__meta?.message ?? '')
      || '已创建解析任务，完成后会自动开始翻译；完成后刷新本页', 'ok', 4200);
    void fetchTasks();
  } catch (err) { reportError(err, '触发解析失败'); }
}

async function fetchTasks() {
  try {
    const d = await api.get<{ items: TaskInfo[] }>('/tasks', { paper_id: instancePaperId, limit: 20 });
    tasks.value = d.items ?? [];
    /* 解析刚结束 → 阅读器**与侧栏**一起刷（不用用户手动刷新页面） */
    const parsingNow = !!parseTask.value;
    if (wasParsing && !parsingNow && !loading.value) {
      void reloadAfterParse();
    } else if (!parsingNow && !hasRunningTask.value && !loading.value
      && !pages.value.length && !allRows.value.length && !reloadedForEmpty) {
      /* 兜底：没有任何任务在跑、可阅读器还是空的 —— 多半是"打开时解析刚结束、产物还没落盘"。
       * 补一次全量重载（只补一次）。 */
      reloadedForEmpty = true;
      void reloadAfterParse();
    }
    wasParsing = parsingNow;
    const running = tasks.value.find((t) => t.type === 'TRANSLATE_MD'
      && ['processing', 'pending', 'paused'].includes(t.status));
    if (running) {
      liveTask.value = running;
    } else if (liveTask.value) {
      /* 列表里已经没有在跑的翻译任务了 → 清掉实时态。
       *
       * ⚠️ 判据只能用"**这份新列表**里还有没有在跑的任务"，**不能**看 `liveTask.value.status`：
       * 它是任务创建时存下的**旧快照**，任务结束后依旧是 `processing`，于是工具栏的「停止」
       * 按钮会一直挂着、要刷新页面才消失（用户报的就是这个）。
       * 顺带把阅读器数据刷一次，译文行与进度统计立刻对齐"已完成"。 */
      liveTask.value = null;
      if (!loading.value) {
        const data = await api.get<ReaderPayload>(`/papers/${instancePaperId}/reader`);
        stats.value = data.stats ?? stats.value;
        pageTranslations.value = data.page_translations ?? pageTranslations.value;
        outline.value = data.outline ?? outline.value;
      }
    }
  } catch { /* 任务列表失败不阻塞阅读 */ }
}

let mq: MediaQueryList | null = null;
let taskTimer: ReturnType<typeof setInterval> | null = null;

function onMqChange() {
  isNarrow.value = !!mq?.matches;
  nextTick(() => { syncPaneWidth(); watchContentSize(); });
}

/** 批注工具（工具栏里不再有「文字选择」：框注改成在画笔/荧光笔/橡皮下**双击**正文，见 InkSurface） */
const INK_TOOLS: Array<{ key: 'hand' | 'pen' | 'marker' | 'eraser'; icon: string; label: string }> = [
  { key: 'hand', icon: 'hand', label: '手：只读，可以正常划词' },
  { key: 'pen', icon: 'pen', label: '画笔' },
  { key: 'marker', icon: 'marker', label: '荧光笔' },
  { key: 'eraser', icon: 'eraser', label: '橡皮：整笔擦除' },
];
/** 粗细三档的圆点直径（细 / 中 / 粗） */
function inkDotStyle(key: string): Record<string, string> {
  const size = key === 'thin' ? 5 : key === 'mid' ? 7 : 9;
  return { width: size + 'px', height: size + 'px' };
}

/* ------------------------------ 胶囊拖动 ------------------------------
 * 用户口径：主胶囊可以拖着挪位，**刷新后还在原地，关掉页面就复位**。
 * 正好是 sessionStorage 的语义（同一标签页内跨刷新保留、标签页关闭即丢）。
 * 只有按在胶囊**空白处**才拖动 —— 按到按钮上仍然是点按钮。
 */
const CAP_POS_KEY = 'cylit:reader-capsule';
const capsuleEl = ref<HTMLElement | null>(null);
const capOffset = reactive({ x: 0, y: 0 });
const capDrag = ref(false);
let capDragFrom: { x: number; y: number; ox: number; oy: number;
  minX: number; maxX: number; minY: number; maxY: number } | null = null;

/**
 * 胶囊能待的区域（视口坐标）：**只在正文列里**，上不压标签栏，下可以一直贴到页面底部。
 * 用户口径 2026-10-17：先要求"限制放置位置"（别拖出屏幕），再说"要能拖到页面底部" ——
 * 所以下方不再预留给弹出层，而是让**面板自己翻到胶囊上方**（见 placeCapPanel / placeInkOptions）。
 */
function capsuleRegion(): { left: number; right: number; top: number; bottom: number } {
  const header = Number.parseInt(
    getComputedStyle(document.documentElement).getPropertyValue('--header-h') || '0', 10) || 0;
  const content = scroller.value?.getBoundingClientRect();
  const left = (content ? Math.max(8, content.left) : 8) + 8;
  const right = (content ? Math.min(innerWidth - 8, content.right) : innerWidth - 8) - 8;
  return {
    left,
    right,
    top: header + 8,
    // 下边界就贴视口底（用户口径 2026-10-17："胶囊没法拖到页面底部"）——
    // 弹出面板放不下时**翻到胶囊上方**，不再靠"给面板留 260px"来换位置。
    bottom: innerHeight - 8,
  };
}

/** 把胶囊夹回允许区域（恢复位置、窗口缩放、切批注后都叫一次） */
function enforceCapsuleBounds(): void {
  const el = capsuleEl.value;
  if (!el) return;
  const rect = el.getBoundingClientRect();
  const region = capsuleRegion();
  if (rect.left < region.left) capOffset.x += region.left - rect.left;
  if (rect.right > region.right) capOffset.x -= rect.right - region.right;
  if (rect.top < region.top) capOffset.y += region.top - rect.top;
  if (rect.bottom > region.bottom) capOffset.y -= region.bottom - rect.bottom;
}

/** 拖动时允许的偏移区间（在"当前偏移"这套坐标里；越界即夹住） */
function dragLimits(rect: DOMRect, region: ReturnType<typeof capsuleRegion>) {
  const minX = Math.min(region.left - rect.left + capOffset.x, region.right - rect.right + capOffset.x);
  const maxX = Math.max(region.left - rect.left + capOffset.x, region.right - rect.right + capOffset.x);
  const minY = Math.min(region.top - rect.top + capOffset.y, region.bottom - rect.bottom + capOffset.y);
  const maxY = Math.max(region.top - rect.top + capOffset.y, region.bottom - rect.bottom + capOffset.y);
  return { minX, maxX, minY, maxY };
}
/** 设置面板要不要翻到胶囊上方（0 = 在下方；负值 = 往上挪多少） */
const panelFlip = ref(0);
/** 面板贴边时的横向微调（胶囊拖到靠边时，面板别越出视口） */
const panelNudge = ref(0);

/**
 * 摆放设置面板：默认落在胶囊下方（面板 CSS 的 `top: 54px` 就是那个位置）；
 * 下方放不下时整体翻到**胶囊上方**，这样胶囊可以一路拖到页面底部（用户口径）。
 */
function placeCapPanel(): void {
  const el = document.querySelector('[data-cap-panel-body=settings]') as HTMLElement | null;
  const cap = capsuleEl.value;
  if (!el || !cap || capPanel.value !== 'settings') { panelFlip.value = 0; return; }
  const rect = cap.getBoundingClientRect();
  const height = el.offsetHeight;
  if (!height) return;
  const spaceBelow = innerHeight - rect.bottom;
  // 面板顶端在胶囊顶端下方 44px（54 − 10 的 margin）：翻上去要挪 高度 + 52 + 边距
  panelFlip.value = spaceBelow < height + 16 ? -(height + 52) : 0;

  // 横向：面板以胶囊中心为中心，胶囊拖到靠边时面板会越出视口 —— 往回收一点
  const width = el.offsetWidth;
  const center = rect.left + rect.width / 2;
  let nudge = 0;
  if (center - width / 2 < 8) nudge = 8 - (center - width / 2);
  if (center + width / 2 > innerWidth - 8) nudge = innerWidth - 8 - (center + width / 2);
  panelNudge.value = Math.round(nudge);
}

const capsuleStyle = computed<Record<string, string>>(() => ({
  transform: 'translate(' + (capsuleShift.value + capOffset.x) + 'px, ' + capOffset.y + 'px)',
}));

/**
 * 让提示条**避开顶部胶囊**（用户口径 ①）：把宿主顶部下移"胶囊底边 + 8px"。
 * 提示条是全局组件（`ToastHost`），这里只能通过 CSS 变量跟它说话 —— 每次胶囊动完都同步一次。
 */
function syncToastDrop(): void {
  const el = capsuleEl.value;
  if (!el || typeof document === 'undefined') return;
  const header = Number.parseInt(
    getComputedStyle(document.documentElement).getPropertyValue('--header-h') || '0', 10) || 0;
  let bottom = el.getBoundingClientRect().bottom;
  // 二级菜单（颜色 / 粗细）展开时正好在胶囊下面：提示条要多让一层，否则会压在颜色行上。
  // 这里按**状态**估算高度（颜色行 + 粗细行 ≈ 80px），不依赖 DOM 查询的时序。
  if (inkOptionsOpen.value) bottom += 88;
  const drop = Math.max(0, Math.round(bottom + 8 - (header + 12)));
  document.documentElement.style.setProperty('--toast-drop', drop + 'px');
}

function restoreCapsulePos(): void {
  try {
    const raw = sessionStorage.getItem(CAP_POS_KEY);
    if (!raw) return;
    const pos = JSON.parse(raw);
    if (typeof pos?.x === 'number' && typeof pos?.y === 'number') {
      capOffset.x = pos.x;
      capOffset.y = pos.y;
    }
  } catch { /* 存了个坏值就当没有 */ }
}

function onCapsuleDown(ev: PointerEvent): void {
  const el = capsuleEl.value;
  if (!el) return;
  // 只有**空白处**或**那枚抓手**才开始拖；按到按钮上仍然是点按钮
  const target = ev.target as HTMLElement | null;
  if (target !== el && !target?.closest?.('[data-cap-grip]')) return;
  const rect = el.getBoundingClientRect();
  capDrag.value = true;
  el.setPointerCapture?.(ev.pointerId);
  capDragFrom = {
    x: ev.clientX, y: ev.clientY, ox: capOffset.x, oy: capOffset.y,
    ...dragLimits(rect, capsuleRegion()),
  };
}

watch(inkOptionsOpen, async (open) => {
  await nextTick();
  if (open) placeInkOptions();
  else syncToastDrop();
  // 菜单展开/收起后提示条要重新让位（菜单本身就在胶囊下面）
  if (open) syncToastDrop();
});

function onCapsuleMove(ev: PointerEvent): void {
  if (!capDrag.value || !capDragFrom) return;
  const from = capDragFrom;
  capOffset.x = Math.min(from.maxX, Math.max(from.minX, from.ox + (ev.clientX - from.x)));
  capOffset.y = Math.min(from.maxY, Math.max(from.minY, from.oy + (ev.clientY - from.y)));
  syncToastDrop();                     // 提示条跟着胶囊走，别被压住
  if (inkOptionsOpen.value) placeInkOptions();
  if (capPanel.value) placeCapPanel();  // 面板放不下会翻到胶囊上方
}

function onCapsuleUp(): void {
  if (!capDrag.value) return;
  capDrag.value = false;
  capDragFrom = null;
  try { sessionStorage.setItem(CAP_POS_KEY, JSON.stringify({ x: capOffset.x, y: capOffset.y })); }
  catch { /* 隐私模式：位置只在本次内存里 */ }
  syncToastDrop();
}

/** 批注里的笔记写进 note 表之后，把侧栏那份笔记列表同步过来（不用整页重载） */
async function refreshNotes(): Promise<void> {
  try {
    const d = await api.get<{ items: Note[] }>('/notes', { paper_id: paperId.value });
    notes.value = d.items ?? [];
  } catch (err) {
    console.warn('笔记列表刷新失败', err);
  }
}
/**
 * 批注模式：只切换"能不能落笔"。笔迹本身按页自动存服务端（见 `useReaderInk`），
 * 所以**退出批注 / 关页面都不会丢**，不用担心"还没保存"。
 */
function toggleInk() {
  inkOn.value = !inkOn.value;
  inkOptionsOpen.value = false;
  void nextTick().then(syncToastDrop);
  // 进入批注**不弹提示**（用户口径 2026-10-17）：胶囊本身已经明确切成了工具，
  // 再横一条提示反而挡住刚展开的二级菜单。
  eraserClearOpen.value = false;
  clearProgress.value = 0;
  if (!inkOn.value) void flushInk();
}

function closeInk() {
  inkOn.value = false;
  inkOptionsOpen.value = false;
  eraserClearOpen.value = false;
  clearProgress.value = 0;
  void flushInk();
}

/** Ctrl/⌘ + Z 撤销、Ctrl/⌘ + Shift + Z（或 +Y）重做：只在批注模式下接管 */
function onInkKey(ev: KeyboardEvent) {
  if (!inkOn.value || !(ev.ctrlKey || ev.metaKey)) return;
  const key = ev.key.toLowerCase();
  if (key === 'z') {
    ev.preventDefault();
    if (ev.shiftKey) redoInk(inkKey.value); else undoInk(inkKey.value);
  } else if (key === 'y') {
    ev.preventDefault();
    redoInk(inkKey.value);
  }
}

onMounted(async () => {
  mq = window.matchMedia('(max-width: 1100px)');
  isNarrow.value = mq.matches;
  mq.addEventListener('change', onMqChange);
  window.addEventListener('resize', onWindowResize);
  document.addEventListener('mousedown', onDragStart);
  document.addEventListener('copy', onCopy);
  document.addEventListener('mouseup', onMouseUp);
  document.addEventListener('mousemove', onDragMove);
  document.addEventListener('selectionchange', onSelectionChange);
  // 划词菜单跟着"当前视角"走：滚动 / 窗口失焦 / 点到别处就收掉（见 `onAnyScroll`）
  window.addEventListener('scroll', onAnyScroll, { capture: true, passive: true });
  window.addEventListener('blur', hideMenu);
  document.addEventListener('pointerdown', onDocPointerDown, true);
  // 本机阅读统计：这次算一次"打开"，并在页面可见期间累加时长
  startReadStats();
  document.addEventListener('visibilitychange', onReadVisibility);
  document.addEventListener('keydown', onInkKey);
  window.addEventListener('pointermove', onCapsuleMove);
  window.addEventListener('pointerup', onCapsuleUp);
  window.addEventListener('pointercancel', onCapsuleUp);
  restoreCapsulePos();
  enforceCapsuleBounds();
  syncToastDrop();
  setInkNotesListener(() => { void refreshNotes(); });

  await loadAll();
  await loadSide();
  await nextTick();
  syncPaneWidth();
  watchContentSize();
  // Ctrl/⌘ + 滚轮缩放：**必须非 passive** 才拦得住浏览器自己的页面缩放（见 onContentWheel）
  scroller.value?.addEventListener('wheel', onContentWheel, { passive: false });
  // 视图模式（可能是服务端进度里的纯译文 / 深链 ?view=）到位后按模式重夹一次侧栏宽度
  await applyAsideMax();
  // **深链优先**：带锚点时不先恢复"上次读到哪" —— 两者都是"滚到某一行"，
  // 而 `focusRow` 内部 `await nextTick()` 才滚动（异步），先发起的那次可能后落地，
  // 于是刚跳过去的深链结果被恢复位置那一次滚动盖掉。真实现象：点「跳回原文」
  // 有时跳到、有时停在上一篇的进度上；直接打开带锚点的链接则看起来"完全没反应"。
  const applied = hasDeepLink() ? await applyDeepLink() : false;
  if (!applied) restorePosition(reader.value?.progress ?? null);
  bootDone.value = true;
  // 胶囊是**数据到位后**才渲染的：onMounted 那一刻它还不存在，夹取与提示条让位要在这之后算一次
  await nextTick();
  enforceCapsuleBounds();
  syncToastDrop();
  placeCapPanel();
  if (route.query.md) {                       // ?md=1[&mdpage=8] 直接打开 Markdown 视图
    const want = Number(route.query.mdpage ?? 0);
    await openMarkdownView(want || undefined);   // 不给页码 = 从当前阅读页那一段开始
  }
  fetchTasks();
  taskTimer = setInterval(() => { if (hasRunningTask.value) fetchTasks(); }, 4000);
});

onUnmounted(() => {
  void flushInk();
  setInkNotesListener(null);
  // 离开阅读器就把提示条的位置放回去（别的页面没有悬浮胶囊）
  try { document.documentElement.style.removeProperty('--toast-drop'); } catch { /* 忽略 */ }
  document.removeEventListener('keydown', onInkKey);
  window.removeEventListener('pointermove', onCapsuleMove);
  window.removeEventListener('pointerup', onCapsuleUp);
  window.removeEventListener('pointercancel', onCapsuleUp);
  mq?.removeEventListener('change', onMqChange);
  document.removeEventListener('visibilitychange', onReadVisibility);
  stopReadStats();
  if (anchorPinTimer) { window.clearTimeout(anchorPinTimer); anchorPinTimer = null; }
  scroller.value?.removeEventListener('wheel', onContentWheel);
  window.removeEventListener('resize', onWindowResize);
  document.removeEventListener('mousedown', onDragStart);
  document.removeEventListener('copy', onCopy);
  document.removeEventListener('mouseup', onMouseUp);
  document.removeEventListener('mousemove', onDragMove);
  document.removeEventListener('selectionchange', onSelectionChange);
  window.removeEventListener('scroll', onAnyScroll, { capture: true });
  window.removeEventListener('blur', hideMenu);
  document.removeEventListener('pointerdown', onDocPointerDown, true);
  setColSide('');               // 离开阅读器时别把 `<html>` 上的标记留在页面上
  contentRo?.disconnect();
  scroller.value?.removeEventListener('scrollend', onScrollEnd);
  if (taskTimer) clearInterval(taskTimer);
  if (flashTimer) clearTimeout(flashTimer);
  saveProgressDebounced.cancel();
  void saveProgressNow();
});

/* ------------------------------ 搜索 ------------------------------ */
/**
 * 搜索：**搜正文行**（原文 + 译文）。输入框与命中列表都在侧栏的「搜索」页签里
 * （`ReaderSearch`）：它是"对整篇文档的操作"，放在侧栏与目录/公式/插图同级，
 * 而不是挤在工具栏的视图切换与翻译按钮中间。命中一条 = 跳到正文那一行（与目录同一种跳转）。
 *
 * 命中后自动切到「搜索」页签 —— 否则用户敲了回车什么也看不见。
 */
function runSearch(opts: { silent?: boolean } = {}) {
  const q = searchQuery.value.trim().toLowerCase();
  if (!q) { searchHits.value = []; return; }
  // 搜索范围（侧栏「搜索」页签里的分段控件）：只在该侧的文本里找，另一侧直接跳过
  const inEn = searchScope.value !== 'zh';
  const inZh = searchScope.value !== 'en';
  const hits: typeof searchHits.value = [];
  for (const r of allRows.value) {
    const en = inEn ? (r.original ?? '').toLowerCase() : '';
    const zh = inZh ? (r.translation ?? '').toLowerCase() : '';
    const iEn = en.indexOf(q);
    const iZh = zh.indexOf(q);
    if (iEn < 0 && iZh < 0) continue;
    const src = iEn >= 0 ? r.original : r.translation;
    const idx = iEn >= 0 ? iEn : iZh;
    hits.push({
      id: r.id, page: r.page, side: iEn >= 0 ? 'en' : 'zh',
      snippet: `${idx > 40 ? '…' : ''}${src.slice(Math.max(0, idx - 40), idx + 90)}`
        + `${src.length > idx + 90 ? '…' : ''}`,
    });
    if (hits.length >= 100) break;
  }
  searchHits.value = hits;
  sideTab.value = 'search';
  if (opts.silent) return;                       // 数据到位/改范围后的自动重跑不打扰用户
  if (!hits.length) toast('没有匹配的段落', 'warn', 2400);
  else toast(`命中 ${hits.length} 处`, 'ok', 1800);
}
function clearSearch() { searchQuery.value = ''; searchHits.value = []; }

/**
 * 只有词法模式才自动重跑词法搜索。
 *
 * 语义模式下这次重跑纯属白干：结果列表在 ReaderSearch 里，页签角标也只看当前模式
 * ——跑出来的命中数只会出现在"看不到的地方"，还会跟语义那次 POST 抢网络。
 */
const lexicalActive = computed(() => searchMode.value === 'lexical');

/**
 * 正文行是**异步**来的（阅读器 payload + SSE 逐页回填），而"搜不到东西"最容易发生在
 * "打开阅读器就马上搜"：那一刻 allRows 还是空的，于是什么都不命中，界面还显示"没有匹配"。
 * 所以数据一变就按当前查询词**静默重跑**。
 */
watch(() => allRows.value.length, () => {
  if (lexicalActive.value && searchQuery.value.trim()) runSearch({ silent: true });
});

/**
 * **边打边搜**（220ms 防抖）。
 *
 * 搜索搬进侧栏后成了一个独立页面：还要求"打完字再按回车"很反直觉 —— 用户打完 "transformer"
 * 没回车，界面就停在上一轮的 0 命中上，看起来就是"搜不到东西"（用户报过）。
 * 回车仍然立即搜（那条路径带 toast 提示），打字路径静默，不刷提示。
 */
const runSearchDebounced = debounce(() => runSearch({ silent: true }), 220);
watch(searchQuery, (v) => {
  if (!v.trim()) { searchHits.value = []; return; }
  if (!lexicalActive.value) return;
  runSearchDebounced();
});
/** 改搜索范围（全部/原文/译文）也要立刻重跑，不然结果和分段控件对不上。 */
watch(searchScope, () => {
  if (lexicalActive.value && searchQuery.value.trim()) runSearch({ silent: true });
});
/** 从语义切回关键词：按当前查询词补跑一次，免得看到的是切走之前那份旧结果 */
watch(searchMode, (m) => {
  if (m === 'lexical' && searchQuery.value.trim()) runSearch({ silent: true });
});

/** 搜索框在侧栏里：`/` 快捷键要先把侧栏（窄屏是抽屉）打开、切到「搜索」页签，再聚焦。 */
const searchPane = ref<InstanceType<typeof ReaderSearch> | null>(null);
/** 对话面板实例：右键「添加到对话框（带这张图）」要把图片塞进它的附件列表（见 `ctxAttachImage`） */
const chatRef = ref<InstanceType<typeof ReaderChat> | null>(null);
async function focusSearchBox() {
  if (!asideVisible.value) await toggleAside();
  sideTab.value = 'search';
  await nextTick();
  searchPane.value?.focusInput();
}

/* ------------------------------ 划词 ------------------------------ */
/**
 * 选区属于哪一"列"：`pdf` = 左栏原 PDF 文字层，`rows:en` / `rows:zh` = 正文里原文/译文那一列，
 * `rows` = 正文里没标侧的（极少），`''` = 其他地方（弹窗等）。
 *
 * 为什么要分到**侧**：对照模式下原文与译文是同一行里的两列，若只区分 "pdf / rows"，
 * 从译文列往左拖会一路"吞"进原文列 —— 选中的东西跟看着的不是一回事（用户报过：
 * "选区不独立，能选到不符的位置"）。分成两列后，跨列的选区会被夹回本列。
 */
function columnOf(node: Node | null): string {
  const el = (node && node.nodeType === 3 ? node.parentElement : node) as HTMLElement | null;
  if (!el?.closest) return '';
  if (el.closest('[data-pdf-page]')) return 'pdf';
  // Markdown 视图里一页分成"原文 / 译文"两面，同样是两列（漏了这一步，那边就永远夹不住）
  const md = el.closest('[data-md-side]') as HTMLElement | null;
  if (md) return `md:${md.dataset.mdSide === 'zh' ? 'zh' : 'en'}`;
  const row = el.closest('[data-row-id]');
  if (!row) return '';
  const side = (el.closest('[data-side]') as HTMLElement | null)?.dataset.side;
  if (side) return `rows:${side}`;
  // 端点落在**单元格**上（在缝里/行内空白处按下或松手时很常见）：按 `.cell-<side>` 判侧，
  // 判成 `rows` 会被当成"另一列"，把合法的选区也夹掉。
  const cell = el.closest('.cell') as HTMLElement | null;
  if (cell) return cell.classList.contains('cell-zh') ? 'rows:zh' : 'rows:en';
  return 'rows';
}

/** 两个盒子是不是**左右并排**（对照模式下原文/译文列、以及原 PDF 栏） */
function sideBySide(a: HTMLElement, b: HTMLElement): boolean {
  const ar = a.getBoundingClientRect();
  const br = b.getBoundingClientRect();
  return br.left >= ar.right - 2 || br.right <= ar.left + 2;
}

/** 这个节点在阅读器内容容器里（页头/页脚/插图/行间空白都算"内容里的中立区"） */
function insideContent(node: Node | null): boolean {
  const el = (node && node.nodeType === 3 ? node.parentElement : node) as HTMLElement | null;
  return !!el?.closest?.('.reader-content, .md-doc');
}

/**
 * 划词**不跨列**：左栏原 PDF / 正文的原文列 / 正文的译文列，三者是并排的独立内容。
 * 从一列往另一列拖时，选区会继续"吞"进对面那列的文字（选中的东西跟看到的不一样）—— 用户报过。
 *
 * 做法：记住**最后一次两列相同的**选区（`lastGood`），一旦发现锚点/焦点分属两列，
 * 就回退到那个选区。而不是去改 `getRangeAt(0)`：拖拽向左产生的是"反向选区"，
 * range 的 start/end 与 anchor/focus 不是一回事，直接 `setEnd` 会把选区翻到另一列去（实测过）。
 *
 * ⚠️ 必须在**拖拽的同一个任务里**调用（见 `onDragMove` / `onMouseUp`）：
 * `selectionchange` 是**异步派发**的，等它跑起来浏览器可能已经画过一帧 —— 用户会看到
 * 选区先"吞"进另一列、再被收回（反馈就是"选区还是不独立"）。`mousemove` 交到手上时
 * 浏览器**已经更新过选区、但还没画**，在这里夹取才是"压根没吞过去"。
 */
let clampingSelection = false;
let lastGood: { node: Node; offset: number; focusNode: Node; focusOffset: number } | null = null;

/** 某个位置上的插入点（Chromium 的 `caretRangeFromPoint` / Firefox 的 `caretPositionFromPoint`） */
function caretAt(x: number, y: number): { node: Node; offset: number } | null {
  const range = document.caretRangeFromPoint?.(x, y);
  if (range) return { node: range.startContainer, offset: range.startOffset };
  const doc = document as Document & {
    caretPositionFromPoint?: (x: number, y: number) => { offsetNode: Node; offset: number } | null;
  };
  const pos = doc.caretPositionFromPoint?.(x, y);
  return pos ? { node: pos.offsetNode, offset: pos.offset } : null;
}

/**
 * Markdown 视图里，**同一侧的每一页是一块**（`[data-md-side]` 在每页各一个）。
 * 返回离指针最近的那一块 —— 指针在哪一块里就用哪一块，落在两块之间（比如下一页的**原文**上）
 * 就取更近的那块。这样沿译文列往下拖才能**接着选到下一页的译文**，
 * 而不是被夹在本页末尾（用户口径：同一侧下一页要能接着选）。
 */
function nearestSideBlock(anchor: HTMLElement, col: string, y: number): HTMLElement {
  const side = col.endsWith('zh') ? 'zh' : 'en';
  const blocks = Array.from(document.querySelectorAll<HTMLElement>(`[data-md-side="${side}"]`));
  let best = anchor;
  let bestDist = Number.POSITIVE_INFINITY;
  for (const b of blocks) {
    const r = b.getBoundingClientRect();
    const d = y < r.top ? r.top - y : y > r.bottom ? y - r.bottom : 0;
    if (d < bestDist) { bestDist = d; best = b; }
  }
  return best;
}

/**
 * 元素里**文字实际占的范围**。
 *
 * 为什么不用元素盒子：对照模式里一行的两个单元格会被拉伸到同高（`align-items: stretch`），
 * 译文比原文短时，单元格盒子会远远高于它的文字 —— 指针走过那段空白时光标一直吸附在
 * 本行文字末尾，选区看着就"卡住不动"（用户报的"右侧译文不能跨页选中"）。
 * 按文字范围算距离，指针一过本行文字就会落到**下一行**上，选区随之连续生长。
 */
function textSpan(el: HTMLElement): { top: number; bottom: number } {
  try {
    const r = document.createRange();
    r.selectNodeContents(el);
    const b = r.getBoundingClientRect();
    if (b.height > 0.5) return { top: b.top, bottom: b.bottom };
  } catch { /* 落到盒子 */ }
  const b = el.getBoundingClientRect();
  return { top: b.top, bottom: b.bottom };
}

/**
 * 本列里**离指针最近的那一个盒子**（正文某侧的某一行 / 原 PDF 栏的某一页）。
 *
 * 为什么不用"滚动容器的可见范围"来夹 y（原来的做法）：把指针拖到内容下沿之外时，
 * 旧写法会把落点夹在**最后一个可见行**上 —— 于是选区停在视口底部，怎么拖都过不去
 * （用户报的"对照时右侧译文不能跨页选中"）；而页面本身不滚动（整屏布局、滚动都在栏内），
 * 浏览器的自动滚动也不会发生。改成"取最近的那一行"之后，指针越过下沿就等于"继续往下选"，
 * 落点自然落到下一行/下一页（它们就在可视区外一点点），选区因此能一路长下去。
 */
function nearestColumnBox(col: string, y: number): HTMLElement | null {
  const sel = col === 'pdf' ? '[data-pdf-page]'
    : col === 'rows:zh' ? '.rrow.side-zh .rr-body'
      : col === 'rows:en' ? '.rrow.side-en .rr-body'
        : col === 'md:zh' ? '[data-md-side="zh"]'
          : col === 'md:en' ? '[data-md-side="en"]' : '';
  if (!sel) return null;
  let best: HTMLElement | null = null;
  let bestDist = Number.POSITIVE_INFINITY;
  for (const el of Array.from(document.querySelectorAll<HTMLElement>(sel))) {
    const s = textSpan(el);
    const d = y < s.top ? s.top - y : y > s.bottom ? y - s.bottom : 0;
    if (d < bestDist) { bestDist = d; best = el; }
    if (d === 0) break;                       // 指针正好在某一行文字里：不必再找
  }
  return best;
}

/** 一"列"用于投影的盒子：PDF 栏 / Markdown 的一面 / 正文某侧的单元格 */
function columnBox(node: Node | null): HTMLElement | null {
  const el = (node && node.nodeType === 3 ? node.parentElement : node) as HTMLElement | null;
  if (!el?.closest) return null;
  const pdf = el.closest('[data-pdf-page]') as HTMLElement | null;
  if (pdf) return pdf;
  const md = el.closest('[data-md-side]') as HTMLElement | null;
  if (md) return md;
  const side = el.closest('[data-side]') as HTMLElement | null;
  if (side) return (side.closest('.cell') as HTMLElement | null) ?? side;
  return (el.closest('.cell') as HTMLElement | null) ?? (el.closest('[data-row-id]') as HTMLElement | null);
}

/**
 * 把指针位置**投影**回锚点那一列，得到一个候选焦点位置。
 *
 * - 左右分栏（正文的原文列/译文列、PDF 栏）：夹 **x**（夹进本列的横向范围），y 夹进
 *   **离指针最近的那一行**（见 `nearestColumnBox`）—— 指针越过内容下沿就等于"继续往下选"，
 *   落点落到下一行/下一页，而不是停在最后一个可见行（那是"跨页选不动"的根因）；
 * - 上下分面（Markdown 视图的原文面/译文面）：x 不动，y 夹进**离指针最近的同侧那一面**
 *   （`nearestSideBlock`，下一页的同一面也算），所以同一侧也能跨页接着选。
 */
function projectCaret(
  sel: Selection, anchorCol: string, point: { x: number; y: number },
): { node: Node; offset: number } | null {
  if (!sel.anchorNode) return null;
  const anchorBox = columnBox(sel.anchorNode);
  if (!anchorBox) return null;
  const a = anchorBox.getBoundingClientRect();
  const focusBox = columnBox(sel.focusNode);
  /* 判"左右分栏"还是"上下分面"：
   * - 两边都在内容里 → 看两个盒子的相对位置；
   * - 焦点跑到内容外（拖到工具栏/侧栏/页号上）→ 看**指针**相对本列的位置：横向出去就夹 x，
   *   纵向出去就夹 y。否则这种手势一个候选都算不出来，选区会被清掉（实测过）。 */
  const sideBySide = focusBox
    ? (() => { const f = focusBox.getBoundingClientRect();
        return f.left >= a.right - 2 || f.right <= a.left + 2; })()
    : (point.x < a.left + 1 || point.x > a.right - 1);
  // y 能到哪儿：上下分面（Markdown）与左右并排（正文列）统一成"夹到**离指针最近的那个盒子**"——
  // 指针越过内容下沿时，落点落到下一行/下一页（而不是停在最后一个可见行），跨页才能接着选。
  const stacked = anchorBox.matches('[data-md-side]');
  const box = stacked ? nearestSideBlock(anchorBox, anchorCol, point.y)
    : (nearestColumnBox(anchorCol, point.y) ?? anchorBox);
  const ab = box.getBoundingClientRect();
  // y 夹进**文字实际范围**（正文行会被拉伸得比文字高，夹盒子会让指针在空白里"卡住"）
  const span = stacked ? { top: ab.top, bottom: ab.bottom } : textSpan(box);
  const x = (sideBySide || stacked)
    ? Math.min(Math.max(point.x, ab.left + 1), ab.right - 1) : point.x;
  const y = Math.min(Math.max(point.y, span.top + 1), span.bottom - 1);
  const caret = caretAt(x, y);
  if (!caret || columnOf(caret.node) !== anchorCol) return null;   // 投影没落回本列就算了
  return caret;
}

/**
 * 锚点到候选焦点之间的字符数（比较两个候选"谁更远"用）。
 * ⚠️ 反向选区（从后往前拖）里 `setStart` 之后 `setEnd` 落在它前面，Range 会**自己塌缩**成空，
 * 直接量长度会得到 0 —— 于是"更远的那个"永远选不出来，夹取把选区清空（踩过）。
 * 所以长度 0 且两点不同位时，反向再量一次。
 */
function spanLength(anchor: { node: Node; offset: number }, focus: { node: Node; offset: number }): number {
  try {
    const r = document.createRange();
    r.setStart(anchor.node, anchor.offset);
    r.setEnd(focus.node, focus.offset);
    if (!r.collapsed) return r.toString().length;
    if (anchor.node === focus.node && anchor.offset === focus.offset) return 0;
    const back = document.createRange();
    back.setStart(focus.node, focus.offset);
    back.setEnd(anchor.node, anchor.offset);
    return back.toString().length;
  } catch {
    return -1;
  }
}

/**
 * 夹取：把跨列的选区收回到锚点所在的那一列。
 *
 * `point` 是**当前指针位置**（只有指针事件驱动时才给）：给了就能把焦点"投影"回本列，
 * 于是"拖到边界"停在**本列的边界**上，而不是缩回上一个状态。
 * 注意只用指针事件带来的坐标、不记模块级状态 —— 鼠标在窗口外松开时不会有 `mouseup`，
 * 留一个"最后的指针位置"会在之后的键盘/程序改选区时被误用。
 */
function clampSelection(point?: { x: number; y: number }) {
  if (clampingSelection) return;
  const sel = window.getSelection();
  if (!sel || sel.rangeCount === 0 || !sel.anchorNode) return;
  const anchorCol = columnOf(sel.anchorNode);
  const focusCol = columnOf(sel.focusNode);
  if (!anchorCol && !focusCol) return;                   // 两边都在内容之外（弹窗页脚等）：不管
  if (anchorCol === focusCol) {                          // 同一列 → 记住，作为回退目标
    lastGood = {
      node: sel.anchorNode, offset: sel.anchorOffset,
      focusNode: sel.focusNode ?? sel.anchorNode, focusOffset: sel.focusOffset,
    };
    return;
  }
  /* ⚠️ 分两种版面：
   * - **左右并排**（正文的原文/译文列、原 PDF 栏）：只夹**横向**跨列。纵向一律放行 —— 这是
   *   "跨页能接着选"的前提：沿一列往下拖时，落点必然从页头/页脚、插图、以及通栏行里对面那一侧
   *   经过（对照模式每页一组，第二页的页头就夹在两页正文之间）。以前把这些也算跨列，每经过一次
   *   就把选区夹回上一个合法位置 —— 表现就是"选到页边界就停住/卡顿"（用户报的"对照模式下
   *   无法跨页选中"）。这些中立区本来就不参与选区：CSS 已 `user-select:none` + 不涂底色，
   *   取文字也按侧过滤。
   * - **上下分面**（Markdown 视图的原文面/译文面）：照旧夹，两面互不串 ——
   *   它靠 `nearestSideBlock` 把落点吸附到**同一侧的相邻页**上来实现跨页，不靠"不夹"。 */
  const aBox = columnBox(sel.anchorNode);
  const fBox = columnBox(sel.focusNode);
  const mdFaces = (aBox?.matches('[data-md-side]') ?? false)
    || (fBox?.matches('[data-md-side]') ?? false);
  if (!mdFaces) {
    if (fBox && (!aBox || !sideBySide(aBox, fBox))) return;   // 纵向 → 不夹
    if (!fBox && insideContent(sel.focusNode)) return;        // 内容里的中立区（页头/插图/行间空白）
  }
  // 到这里是"跨列/跨面"（含"一边在内容里、另一边拖到工具栏/页号上"）：夹回本列
  clampingSelection = true;
  try {
    const anchor = { node: sel.anchorNode, offset: sel.anchorOffset };
    /* 两个候选，取**离锚点更远**的那个：
     * ① 投影（把指针位置按本列重新取一次插入点）—— 快甩（一两个 mousemove 就甩到对面列）时，
     *    `lastGood` 还停在起点，只有投影能给出"本列边界"那个位置；
     * ② `lastGood`（最后一次同列选区）—— 慢拖时它正好停在边界上；而投影在"鼠标刚好落在
     *    文字行上方/行间"时会被浏览器吸附到最近的插入点（实测会吸回锚点，选区直接变空）。
     * 取更远的那个 = 两边都利用上，谁被吸附坏了就用另一个。 */
    let best: { node: Node; offset: number; len: number } | null = null;
    const proj = point ? projectCaret(sel, anchorCol, point) : null;
    if (proj) best = { ...proj, len: spanLength(anchor, proj) };
    if (lastGood && lastGood.node.isConnected && lastGood.focusNode.isConnected
        && columnOf(lastGood.focusNode) === anchorCol) {
      const cand = { node: lastGood.focusNode, offset: lastGood.focusOffset };
      const len = spanLength(anchor, cand);
      if (len >= 0 && (!best || len > best.len)) best = { ...cand, len };
    }
    if (best && best.len > 0) {
      sel.setBaseAndExtent(anchor.node, anchor.offset, best.node, best.offset);
    } else {
      lastGood = null;
      sel.collapse(anchor.node, anchor.offset);
    }
  } catch {
    try { sel.collapse(sel.anchorNode, sel.anchorOffset); } catch { /* 回退失败就算了 */ }
  } finally {
    clampingSelection = false;
  }
}

/** 键盘/程序改选区（没有指针位置）也夹一下：拿不到投影就退回 `lastGood`；顺带同步"选区在哪一列" */
const onSelectionChange = () => { clampSelection(); syncSelectionColumn(); };

/** 这一列要不要"隐藏对面列的选区底色"（正文两列 / 原 PDF 栏 / Markdown 视图的两面都算一"列"） */
function colSide(col: string): 'en' | 'zh' | 'pdf' | '' {
  if (col === 'pdf') return 'pdf';
  if (col === 'rows:en' || col === 'md:en') return 'en';
  if (col === 'rows:zh' || col === 'md:zh') return 'zh';
  return '';
}

/**
 * 端点属于**正文的哪一侧**（`en` / `zh`，拿不到给空串）。
 *
 * ⚠️ 判侧只能有这一处：以前在划词、复制、删除高亮里各写了一遍"`closest('[data-side]')` 是不是 zh"，
 * 只要有一处判歪（比如端点是单元格/行元素时 `closest` 拿不到 `[data-side]`，就默认成了 `en`），
 * 就会出现"选区明明在右列，菜单却跑到左列、复制出来是原文"这种**半对半错**的状态（用户报过）。
 */
function rowsSide(node: Node | null): 'en' | 'zh' | '' {
  const col = columnOf(node);
  return col === 'rows:en' ? 'en' : col === 'rows:zh' ? 'zh' : '';
}

/**
 * 给 `<html>` 挂/摘"选区在哪一列"的标记，让**对面那一列**的选区底色不画出来（见 `base.css`）。
 *
 * 为什么非要这样：两列在文档顺序里是交错的（`en1, zh1, en2, zh2 …`），**纵向**拖动时选区一定从
 * 对面列的单元格之间穿过 —— 锚点、焦点都在本列，跨列的内容却夹在选区中间；而一个 Range 是连续的
 * （Chromium 不支持多段选区，实测 `addRange` 两次 `rangeCount` 仍是 1），所以"只选中本列"在不改
 * DOM 结构的前提下做不到。能做的是：**不把对面列画出来**（文字上只取本列，见划词菜单 / 复制 /
 * 删除高亮都按侧过滤）。
 *
 * ⚠️ 标记必须**跟着选区走**，不能只在拖拽期间挂：松手之后选区还在（比如译文列跨了 5 行），
 * 一摘标记浏览器立刻把整段连续选区（含中间那些原文单元格）重新涂上底色 —— 用户看到的就是
 * "拖拽时不会到左侧，但一松手就回到左侧"（复报过）。
 *
 * ⚠️ **原 PDF 栏也是一"列"**：在"对照 + 左栏原 PDF"里，DOM 顺序是
 * `PDF(1) rows(1) PDF(2) rows(2) …`，沿译文列**跨页**往下拖时，选区必然从**下一页的 PDF 页**
 * 中间穿过 —— 锚点/焦点都还在译文列，中间的 PDF 文字层却被涂上底色（用户报的"跨页选中译文
 * 会选中 pdf"）。所以这里把 `pdf` 也当成一列记下来，由 CSS 把非本列的底色涂成透明。
 */
function setColSide(side: 'en' | 'zh' | 'pdf' | '') {
  const root = document.documentElement;
  if (side) {
    root.dataset.pmCol = side;
    root.classList.add('pm-col-sel');
  } else {
    delete root.dataset.pmCol;
    root.classList.remove('pm-col-sel');
  }
}

/** 按**选区锚点**所在列同步标记（选区折叠 → 摘掉） */
function syncSelectionColumn() {
  const sel = window.getSelection();
  const node = sel && !sel.isCollapsed && sel.rangeCount > 0 ? sel.anchorNode : null;
  setColSide(node ? colSide(columnOf(node)) : '');
}

/** 按下时就先定下来（拖拽最初那几帧还没有选区，等 `selectionchange` 会闪一下） */
function onDragStart(ev: MouseEvent) {
  /* 右键：**保住页面上的选区**（浏览器的默认动作是把选区折叠到点击处）。
     不挡的话，右键菜单里那几项"复制 / 高亮 / 询问"会因为"没有选区"整组消失
     （用户报的正是这个），划词胶囊也会跟着没。输入框里放行原生行为。 */
  if (ev.button === 2) {
    const t = ev.target as HTMLElement | null;
    if (!t?.closest?.('input, textarea, [contenteditable="true"]')) ev.preventDefault();
    return;
  }
  if (ev.button !== 0) return;
  setColSide(colSide(columnOf(ev.target as Node)));
}

/** 划词浮层的定位（贴着选区上方，不跑出屏幕） */
function menuPos(rect: DOMRect) {
  return {
    x: Math.min(window.innerWidth - 320, Math.max(8, rect.left)),
    y: Math.max(8, rect.top - 46),
  };
}

/**
 * 按住左键拖动时**在同一个任务里**夹取选区（见 `clampSelection` 上的说明）。
 * `mousemove` 交到手上时浏览器**已经更新过选区、但还没画**：在这里夹掉，用户根本看不到
 * 跨列底色（只靠异步的 `selectionchange`，可能已经画过一帧了）。
 */
function onDragMove(ev: MouseEvent) {
  if (!(ev.buttons & 1)) return;
  clampSelection({ x: ev.clientX, y: ev.clientY });
}

function onMouseUp(ev?: MouseEvent) {
  /* 右键那一下也走这里（用户口径 2026-10-08："顶部的胶囊在右键时也能显示"）：
     右键 mousedown 已经 `preventDefault` 保住了选区，所以这一步能把胶囊重新摆出来 ——
     它现在**只带图标**，和右键菜单各管一段（胶囊给颜色/删除，右键菜单给"询问/添加/搜索/…"）。
     层级上胶囊在上面（见 `SelectionMenu` 的 z-index）。 */
  /* 点的是划词菜单本身（色块/按钮）：**绝不能**当成"新划词"处理。
   * 真实鼠标点色块时，mousedown 的默认动作会把选区清掉（`SelectionMenu` 已 preventDefault 兜住，
   * 但这里再兜一层）—— 一旦把 `selection.show` 置 false，菜单会在自己的 click 触发前被卸载，
   * 表现就是"点高亮毫无反应"。这一层与 preventDefault 是同一件事的两道保险。 */
  const target = ev?.target as HTMLElement | null;
  if (target?.closest?.('[data-selection-menu]')) return;

  // 松手前再夹一次：快甩（一两个 mousemove 就甩到对面列）时，最后一次移动可能没留下
  // 同列状态 —— 不夹的话，菜单里显示的、以及要存库的选区就是跨列的（"选到不符的位置"）。
  clampSelection(ev ? { x: ev.clientX, y: ev.clientY } : undefined);
  // 松手后选区还在（译文列跨了好几行）：标记要**留着** —— 一摘掉，浏览器立刻把整段连续选区
  // （中间那些原文单元格）重新涂上底色，用户看到的就是"一松手就回到左侧"。
  syncSelectionColumn();

  const sel = window.getSelection();
  if (!sel || sel.isCollapsed) { selection.show = false; return; }
  const text = sel.toString().trim();
  if (!text) { selection.show = false; return; }
  const node = sel.anchorNode;
  const el = (node && node.nodeType === 3 ? node.parentElement : node) as HTMLElement | null;
  const rect = sel.getRangeAt(0).getBoundingClientRect();
  /* 存一份矩形快照：**右键 mousedown 会把 DOM 选区折叠掉**（浏览器默认动作），
     那时 `getSelection()` 里已经没有选区了 —— 右键菜单要靠这份快照判"落点在不在选区上"。 */
  selection.rect = { left: rect.left, top: rect.top, right: rect.right, bottom: rect.bottom };

  /* 原 PDF 划词（《修改计划》第三步起**支持高亮**）：文字层是 pdf.js 渲染出来的，
   * 自己就是一套"页面文本"。锚点 = `pdf:p<页号>` + **该页文字层渲染文字里的偏移量**
   * （`rangeOffsets` 与正文行/Markdown 同一口径）—— 与另两面共用记录形状，但位置模型独立。
   * 选到页外（跨页拖拽、或没落在文字上）时 `offsets` 为空：那时退回"只解释/复制"，
   * 不写一条定位不回去的死高亮。 */
  const pdfEl = el?.closest?.('[data-pdf-page]') as HTMLElement | null;
  if (pdfEl) {
    const page = Number(pdfEl.getAttribute('data-pdf-page') ?? 0) || 0;
    const layer = pdfEl.querySelector<HTMLElement>('.pdf-text-layer');
    const offs = layer ? rangeOffsets(layer, sel.getRangeAt(0)) : null;
    /* ⚠️ 存库的 `selected_text` 必须取**坐标系统里那一段文字**（`renderedText` 的切片），
     * 不能直接用 `sel.toString()`：文字层的 span 是绝对定位的，浏览器在 span 之间会插换行/空格，
     * 而偏移量那套坐标系是**把这些文本节点直接拼起来**的 —— 两者长度与空白都不一样。
     * 存了不一致的文字，画高亮时的"先对一次文本"就会判定错位、整条高亮画不出来
     * （实测：跨 span 选一段，只画出前半截）。 */
    const anchorText = offs && layer ? renderedText(layer).slice(offs.start, offs.end) : '';
    Object.assign(selection, {
      show: true, ...menuPos(rect), text: truncate(text, 600), fullText: text,
      anchorText,
      rowId: offs ? pdfAnchor(page) : '', side: 'en' as const,
      start: offs?.start ?? 0, end: offs?.end ?? 0,
      anchored: !!offs, scope: 'pdf' as const, mdPage: 0,
      // 原 PDF 划词的位置就是**页号**（带进对话要写进消息里）；有锚点时按页高亮
      page,
      allowNote: true,
      segments: offs ? [{ rowId: pdfAnchor(page), start: offs.start, end: offs.end, text: anchorText, page }] : [],
      hlIds: offs ? highlightIdsInRange(sel.getRangeAt(0)) : [],
    });
    return;
  }

  /* Markdown 视图划词：锚点是 `md:p<页号>` + 相对**渲染后文字**的偏移量（见 `utils/domMarks.ts`）。
   * 这里给不了「笔记」：笔记按 `block_id` 定位回正文，而这套锚点在正文里不存在。 */
  const mdBlock = el?.closest?.('[data-md-side]') as HTMLElement | null;
  if (mdBlock) {
    const side: 'en' | 'zh' = mdBlock.dataset.mdSide === 'zh' ? 'zh' : 'en';
    /* 同一侧可以**跨页**选（用户口径）：按页切开，每页一段 —— 锚点是 `md:p<页号>`，
     * 一条记录只能落在一页上，所以跨页时必须各存一段（共用分组，删一条带走整组）。 */
    const range = sel.getRangeAt(0);
    const parts = mdSegments(range, side);
    if (!parts.length) { selection.show = false; return; }
    const first = parts[0];
    Object.assign(selection, {
      show: true, ...menuPos(first.rect ?? rect), text: truncate(joinParts(parts) || text, 600),
      fullText: joinParts(parts) || text,
      anchorText: joinParts(parts) || text,      // 同上：分段文字就是渲染文字
      rowId: first.rowId, side, start: first.start, end: first.end,
      anchored: true, scope: 'markdown' as const, mdPage: Number(first.page ?? 0),
      page: Number(first.page ?? 0),
      /* 笔记也能挂：`md:p<N>` 这个锚点前端认得（第四步给笔记补了 `scope` 与 `page`，
         侧栏「定位」按页跳得过去），所以这里不再像第一版那样禁用「笔记」。 */
      allowNote: true, segments: parts, hlIds: highlightIdsInRange(range),
    });
    return;
  }

  /* 正文行划词：偏移量一律相对**渲染后的文字**（公式算 1 个字符）。
   * 选区**跨了多行**时按行切开，每行一段（`segments`），存成同一分组的多条记录。 */
  const rowEl = el?.closest?.('[data-row-id]') as HTMLElement | null;
  if (!rowEl?.dataset.rowId) { selection.show = false; return; }

  const range = sel.getRangeAt(0);
  // 判侧统一走 `rowsSide`；锚点拿不到就用焦点那一端（端点是单元格/行元素时很常见）
  const side = rowsSide(sel.anchorNode) || rowsSide(sel.focusNode) || 'en';
  const parts = rowSegments(range, side);
  if (!parts.length) { selection.show = false; return; }
  /* 菜单位置按**本列这一段**算：整段选区的 bbox 在纵向跨行时会把中间那些对面列的单元格
   * 也算进去（左边界落在对面列），菜单就跑到对面列头顶上了（用户报过："胶囊菜单在左侧"）。 */
  const firstBody = document.querySelector<HTMLElement>(
    `[data-row-id="${parts[0].rowId}"] .rrow.side-${side} .rr-body`);
  const firstPart = firstBody ? offsetsWithin(firstBody, range) : null;
  Object.assign(selection, {
    /* 文字只取**本列**（`parts` 已按侧过滤）：纵向拖多行时，连续选区必然从对面列的单元格之间
     * 穿过（两列在 DOM 里交错），直接用 `sel.toString()` 会把对面列的文字一起带进
     * 「AI 解释 / 复制」里（实测 1130 字的译文选区会读出 3596 字）。 */
    show: true, ...menuPos(firstPart?.rect ?? rect), text: truncate(joinParts(parts) || text, 600),
    fullText: joinParts(parts) || text,
    // 正文行的分段文字本来就是"渲染文字"的切片 → 坐标文字与显示文字同一份
    anchorText: joinParts(parts) || text,
    rowId: parts[0].rowId, side, start: parts[0].start, end: parts[0].end,
    anchored: true, scope: 'row' as const, mdPage: 0, allowNote: true,
    // 正文行的页号就在行 id 里（`p004-02`）—— 与 agent 那边 `_page_of_row` 同一个口径
    page: pageOfRow(parts[0].rowId),
    segments: parts,
    hlIds: highlightIdsInRange(range, null, `rows:${side}`),
  });
}

/** 按行切开后的文字拼回来（菜单里显示的、以及要解释/复制的就是这一段） */
function joinParts(parts: Array<{ text: string }>): string {
  return parts.map((p) => p.text).filter(Boolean).join('\n').trim();
}

/**
 * Markdown 视图里，同一侧的选区**按页切开**（`rowId` = `md:p<页号>`）。
 *
 * 为什么必须切：锚点带着页号（`md:p8`），一条高亮记录只能定位到一页；同一侧跨页选中的那段文字
 * 若只按锚点那一页存，下一页的部分就丢了（用户口径：按页各存一段，共用分组一起删）。
 * 与正文跨行那套完全同构（`rowSegments`），所以下游（画高亮 / 删除 / 复制）一行都不用改。
 */
function mdSegments(range: Range, side: 'en' | 'zh'): MdSegment[] {
  const out: MdSegment[] = [];
  for (const block of Array.from(document.querySelectorAll<HTMLElement>(`[data-md-side="${side}"]`))) {
    if (!range.intersectsNode(block)) continue;
    const page = Number(block.closest('[data-md-page]')?.getAttribute('data-md-page') ?? 0);
    if (!page) continue;
    const got = offsetsWithin(block, range);        // 与 root 求交集：本页覆盖到的那部分
    if (!got) continue;
    out.push({ rowId: mdAnchor(page), start: got.start, end: got.end, text: got.text,
               rect: got.rect, page });
  }
  return out.sort((a, b) => (a.page ?? 0) - (b.page ?? 0));
}

/**
 * 选区在 `root` 里的偏移量：**先与 root 求交集**再算，跨出 root 的部分自动截掉。
 *
 * 为什么不直接用 `rangeOffsets(root, range)`：那个要求整段选区都在 root 里，
 * 一旦选区跨到另一列（或被夹取漏掉的边缘情况）就返回 null，表现是"菜单压根不出来"。
 * 求交集之后，跨列选区至少能把**锚点这一列**覆盖到的部分标上，不会什么都不做。
 *
 * 顺带把交集的 `rect` 也回传：划词菜单要按**本列那一段**定位，
 * 不能拿整段选区的 bbox（纵向跨行时它会把中间那些对面列的单元格算进去）。
 */
function offsetsWithin(
  root: HTMLElement, range: Range,
): { start: number; end: number; text: string; rect: DOMRect; atoms: number } | null {
  const whole = document.createRange();
  whole.selectNodeContents(root);
  const startAfter = range.compareBoundaryPoints(Range.START_TO_START, whole) > 0;
  const endBefore = range.compareBoundaryPoints(Range.END_TO_END, whole) < 0;
  let inter: Range;
  try {
    inter = document.createRange();
    inter.setStart(startAfter ? range.startContainer : whole.startContainer,
                  startAfter ? range.startOffset : whole.startOffset);
    inter.setEnd(endBefore ? range.endContainer : whole.endContainer,
                endBefore ? range.endOffset : whole.endOffset);
  } catch { return null; }
  const offsets = rangeOffsets(root, inter);
  if (!offsets) return null;
  /* 选中了公式那一段：文字改用它**的 LaTeX**（见 `sliceWithLatex`）—— `Range.toString()` 会把
     KaTeX 的 MathML 副本也算进去（"重影文字"），排版字形粘到别处也多半用不了。
     没选中公式时仍用 `Range.toString()`：空白/换行口径与以前一字不差（跨列复制那套口径不能动）。 */
  const withTex = sliceWithLatex(root, offsets.start, offsets.end);
  const text = withTex.atoms ? withTex.text.trim() : inter.toString().trim();
  return { ...offsets, text, rect: inter.getBoundingClientRect(), atoms: withTex.atoms };
}

/**
 * 把选区按**行**切开：返回每行的 `{rowId, start, end, text}`（偏移量相对该行**本侧**渲染后的文字）。
 *
 * ⚠️ `side` 必须传进来：一行里有两个 `.rr-body`（原文/译文），只取第一个会永远落在原文上 ——
 * 在译文里划词就一段都取不到（菜单都不出来）。
 * ⚠️ 每段都要带上**本段自己的文字**：只存整段选区的话，逐行渲染时"这一行的文字 ≠ 整段选区的文字"，
 * 校验必然不过 → 一条也画不出来（跨行高亮实测就是这样丢的）。
 */
function rowSegments(
  range: Range, side: 'en' | 'zh',
): Array<{ rowId: string; start: number; end: number; text: string }> {
  const out: Array<{ rowId: string; start: number; end: number; text: string }> = [];
  const rows = Array.from(document.querySelectorAll<HTMLElement>('[data-row-id]'));
  for (const rowEl of rows) {
    const rowId = rowEl.dataset.rowId ?? '';
    const body = rowEl.querySelector<HTMLElement>(`.rrow.side-${side} .rr-body`);
    if (!rowId || !body || !range.intersectsNode(body)) continue;
    const got = offsetsWithin(body, range);
    if (!got) continue;
    out.push({ rowId, start: got.start, end: got.end, text: got.text });
  }
  return out;
}

/** 选区（或点击处）盖住了哪些已有高亮 —— 菜单据此决定要不要显示「删除高亮」 */
function highlightIdsInRange(range: Range, target?: HTMLElement | null, onlyCol?: string): string[] {
  const ids = new Set<string>();
  const clicked = target?.closest?.('[data-hl]') as HTMLElement | null;
  if (clicked?.dataset.hl) {
    ids.add(clicked.dataset.hl);
  } else {
    document.querySelectorAll<HTMLElement>('mark.hl[data-hl]').forEach((mark) => {
      // 纵向拖多行时选区会把对面列的单元格一起圈进来 —— 那里面的高亮**不是**本次选中的，
      // 不按侧过滤的话菜单会莫名其妙报出"删除对面列的高亮"。
      if (onlyCol && columnOf(mark) !== onlyCol) return;
      try {
        if (range.intersectsNode(mark)) ids.add(mark.dataset.hl ?? '');
      } catch { /* 已被卸载的节点 */ }
    });
  }
  ids.delete('');
  // 跨行高亮是"同组多条"：把同组的兄弟也带上，删的时候整条一起删
  const groups = new Set(highlights.value
    .filter((h) => h.id && ids.has(h.id) && h.group_id)
    .map((h) => h.group_id as string));
  if (groups.size) {
    highlights.value.forEach((h) => {
      if (h.group_id && groups.has(h.group_id)) ids.add(h.id);
    });
  }
  return [...ids];
}

/** 收划词胶囊。**顺带把右键菜单也收掉**：胶囊上的动作（高亮 / 复制 / 解释…）都是"针对这一处"的，
 *  做完之后菜单还杵在旁边只会挡正文（用户口径 2026-10-10 的连带修复）。 */
const hideMenu = () => { selection.show = false; ctxMenu.open = false; ctxMenu.rect = null; };
/** 点胶囊的 × = **连选中状态一起取消**（用户口径 2026-10-10）：只收浮层、留着选区的话，
 *  鼠标一碰那段文字胶囊又冒出来，看着像"关不掉"；快照也要清（否否则下次右键仍会判成"有选区"）。 */
function closeSelectionMenu() {
  window.getSelection()?.removeAllRanges();
  selection.text = '';
  selection.rect = null;
  hideMenu();
}

/**
 * 滚动 / 失去焦点时收掉划词菜单。
 *
 * 菜单是 `position: fixed`，位置是**划词那一刻**按选区 rect 算好的 —— 一旦滚动就与文字脱开，
 * 变成一枚浮在页面中间、跟谁都不挨着的胶囊（用户报的正是这个）；它本来也不该"钉在屏幕上"。
 * 所以：任何容器滚动、窗口失焦、点到菜单以外的地方，一律收掉。
 *
 * 三条必须分开写，缺一条就有漏：
 *   · `scroll` 要**捕获**（`capture: true`）：滚动事件不冒泡，只有捕获能在 window 上收到
 *     "栏内滚动"（正文栏、侧栏、Markdown、原 PDF 各自是独立滚动容器）；
 *   · `blur` 收窗口失焦（切标签页 / 切到别的应用）；
 *   · `pointerdown` 补"点了别处" —— **点已有高亮弹出的那个菜单没有选区**，点空白处不会走到
 *     `onMouseUp` 里"选区已折叠 → 收起"那条分支，不收的话菜单会一直留在屏幕上。
 *     点菜单自己（色块/按钮）**绝不能**收：mousedown 的默认动作会清掉选区，菜单会在自己的
 *     click 触发前被卸载 —— 表现就是"点高亮毫无反应"（见 `SelectionMenu.vue` 顶部那段说明）。
 */
function onAnyScroll(ev?: Event) {
  /* 浮层自己内部滚（一屏放不下几条参考文献）不算"视角变了" —— 别把它自己关掉 */
  if ((ev?.target as HTMLElement | null)?.closest?.('[data-cite-pop]')) return;
  citePop.open = false;      // 引用标号的浮层贴着正文，一滚就对不上了（与划词菜单同一处理）
  cancelCiteOpen();          // 还没弹出来的那一次也别弹了（锚点已经跟着内容跑掉了）
  hideMenu();
}
function onDocPointerDown(ev: PointerEvent) {
  const target = ev.target as HTMLElement | null;
  if (target?.closest?.('[data-selection-menu]')) return;
  // 顶栏弹出面板：点面板外面就收起来（面板内的点击不动它 —— 改一个分段值不该把面板关掉）
  if (capPanel.value && !target?.closest?.('.reader-toolbar')) capPanel.value = '';
  /* 引用标号的浮层：点别处就收。⚠️ 点**浮层里面**（条目/跳转图标）绝不能收 ——
     一收就把元素从 DOM 上摘了，那次 click（`window.open` 在它上面）根本不会再触发。 */
  if (!target?.closest?.('[data-cite-pop]')) citePop.open = false;
  cancelCiteOpen();          // 点别处了：还没弹出来的那一次也取消
  hideMenu();
}

/* ------------------------------ 复制：公式整段选中时给 LaTeX ------------------------------
 * 用户口径 2026-10-08：**完全选中一个公式时，复制出来的是它的 LaTeX**，不是排版后的字形。
 * 为什么不能直接用 `Range.toString()`：KaTeX 的输出里带一份 MathML（同一个公式的重复副本），
 * `toString()` 会把两边的字都算进去，粘出去是"重影文字"；而排版后的字形
 * （`Q^{-1}_{mse}` 这种）粘到别处通常也用不了。TeX 源码就在 KaTeX 的
 * `<annotation encoding="application/x-tex">` 里，那才是要的。
 * 口径与偏移量一致：**公式算 1 个字符**（`utils/domMarks` 的 ATOM），所以"选区覆盖到它"就是整段选中。 */
function latexOfFormula(el: Element | null): string {
  const tex = el?.querySelector?.('annotation[encoding="application/x-tex"]')?.textContent ?? '';
  return tex.trim();
}

/**
 * 容器里 `[start, end)` 这一段：文本段取文字，**公式段换成 LaTeX**（显示公式包 `$$…$$`）。
 * 顺带回一个 `atoms`：换掉了几个公式 —— 调用方据此决定"要不要接管这次复制"
 * （一个公式都没选中时，保持浏览器默认的空白/换行口径，一个字都不动）。
 */
function sliceWithLatex(root: HTMLElement, start: number, end: number): { text: string; atoms: number } {
  let text = '';
  let atoms = 0;
  for (const seg of segments(root)) {
    const s = Math.max(start, seg.start);
    const e = Math.min(end, seg.end);
    if (s >= e) continue;
    if (seg.kind === 'atom') {
      const tex = latexOfFormula(seg.el ?? null);
      if (!tex) continue;                              // 抠不出源码就整体跳过（宁缺勿错）
      atoms += 1;
      text += seg.el?.classList?.contains('katex-display') ? `$$${tex}$$` : `$${tex}$`;
      continue;
    }
    text += (seg.node?.nodeValue ?? '').slice(s - seg.start, e - seg.start);
  }
  return { text, atoms };
}

/**
 * 这次复制该给出什么。返回 `null` = "没什么特别的"，交给浏览器默认复制（一个字都不动）。
 *
 * 两种"特别"：
 * ① **跨列**（选区盖住了对面那一列）：只放锚点这一列的文字（见 `onCopy` 的老说明）；
 * ② **选中了公式**：那些公式换成 LaTeX。
 * 只在①或②成立时才接管 —— 平时保持浏览器自己的空白/换行口径（跨列那次实测 4984 → 3631 字的口径不变）。
 */
function copyPayload(sel: Selection): { text: string; atoms: number; covers: boolean } | null {
  if (!sel.rangeCount || !sel.anchorNode) return null;
  const range = sel.getRangeAt(0);
  const side = rowsSide(sel.anchorNode) || rowsSide(sel.focusNode);
  const col = columnOf(sel.anchorNode) || columnOf(sel.focusNode);
  const faces: Array<{ root: HTMLElement }> = [];
  if (side) {
    for (const rowEl of Array.from(document.querySelectorAll<HTMLElement>('[data-row-id]'))) {
      const body = rowEl.querySelector<HTMLElement>(`.rrow.side-${side} .rr-body`);
      if (body && range.intersectsNode(body)) faces.push({ root: body });
    }
  } else if (col.startsWith('md:')) {
    const face = col.endsWith('zh') ? 'zh' : 'en';
    for (const block of Array.from(document.querySelectorAll<HTMLElement>(`[data-md-side="${face}"]`))) {
      if (range.intersectsNode(block)) faces.push({ root: block });
    }
  } else {
    return null;                                       // 原 PDF 文字层 / 侧栏等：没有公式，照旧
  }
  const other = side === 'en' ? 'zh' : 'en';
  const covers = !!side
    && Array.from(document.querySelectorAll<HTMLElement>(`.rrow.side-${other} .rr-body`))
      .some((b) => { try { return !!offsetsWithin(b, range); } catch { return false; } });
  const parts: string[] = [];
  let atoms = 0;
  for (const { root } of faces) {
    const got = offsetsWithin(root, range);
    if (!got) continue;
    atoms += got.atoms;                                // 文字已经由 `offsetsWithin` 换成 LaTeX
    if (got.text) parts.push(got.text);
  }
  const text = joinParts(parts.map((t) => ({ text: t })));
  if (!text) return null;
  return { text, atoms, covers };
}

/**
 * Ctrl+C：**选区跨到了对面列时，剪贴板只放本列的文字**；**选中公式时放它的 LaTeX**。
 *
 * 浏览器复制的是整段连续选区，而纵向拖多行时它必然包含对面列的单元格（见 `base.css` 里的说明）。
 * 只有"确实盖住了对面列"或"选中了公式"才改写：平时单列复制原样交给浏览器，别动它的空白/换行口径。
 */
function onCopy(ev: ClipboardEvent) {
  const sel = window.getSelection();
  if (!sel || sel.isCollapsed || sel.rangeCount === 0 || !sel.anchorNode) return;
  const payload = copyPayload(sel);
  if (!payload || (!payload.covers && !payload.atoms)) return;
  ev.clipboardData?.setData('text/plain', payload.text);
  ev.preventDefault();
}

/* ------------------------------ 右键菜单（毛玻璃） ------------------------------
 * 用户口径 2026-10-08：三处界面（原 PDF 页 / 正文行 / Markdown 视图）**各一套清单**，
 * 按"有没有选区、右键落在什么上（高亮 / 图片 / 空白）"动态给项 —— 没有的动作一律不出现
 * （没选区就不给「高亮」，不在图片上就不给「另存为/放大」）。
 * 复用现成能力：询问=AI 解释浮层（可追问）、添加到对话框=侧栏输入框的上下文胶囊、
 * 复制=那条公式感知的复制、高亮/删除=划词菜单同一套、导出=当前视图那份 Markdown。
 * 新做的只有三件：Bing 搜索、全选（本页）、另存为（原图）。
 */
interface CtxItem {
  key: string; label: string; icon?: string;
  /** 只在**真有**快捷键的项上写（别编一个 Ctrl+X 出来） */
  shortcut?: string;
  danger?: boolean;
  sep?: boolean;
  /** 色板（`ContextMenu` 读它）：给了就在这一行右侧排一排色点（用户口径 2026-10-10） */
  swatches?: string[];
}

const ctxMenu = reactive({
  open: false, x: 0, y: 0, items: [] as CtxItem[],
  /** 菜单摆完位后的真实矩形（`ContextMenu` 的 `placed` 事件报回来的）：划词胶囊据此贴到它上沿 */
  rect: null as { left: number; top: number; width: number; height: number } | null,
});
/** 胶囊这次该摆在哪：**右键菜单开着时贴在菜单上沿**（用户口径 2026-10-08：
 *  "希望胶囊能在右键后改变位置到右键菜单上"），否则照旧贴选区。 */
const pillPos = computed(() => {
  const r = ctxMenu.rect;
  if (!ctxMenu.open || !r) return { x: selection.x, y: selection.y };
  return { x: r.left, y: Math.max(8, r.top - 40) };
});
/** 这次右键的上下文。**不放 reactive**：里面是 DOM 节点与临时算出来的地址，不需要跟着重渲染。 */
let ctxCtx = {
  line: '' as '' | 'pdf' | 'rows' | 'md',
  text: '',
  page: 0,
  hlIds: [] as string[],
  imgUrl: '',
  imgName: '',
  imgCaption: '',
  /** 落点是**真 `<img>`（插图）**，不是 PDF 页那张位图 —— 决定给不给「询问 Cyli」 */
  isFigure: false,
  /** 那枚插图的屏幕矩形：解释浮层要锚在它旁边 */
  imgRect: null as { x: number; y: number; w: number; h: number } | null,
};

/** 右键：按"落在哪个界面 + 落点是什么"给清单（三套）。输入框里放行浏览器原生菜单。 */
function onContentContextMenu(ev: MouseEvent) {
  const target = ev.target as HTMLElement | null;
  if (target?.closest?.('input, textarea, [contenteditable="true"]')) return;
  const pdfEl = target?.closest?.('[data-pdf-page]') as HTMLElement | null;
  const rowEl = target?.closest?.('[data-row-id]') as HTMLElement | null;
  const pageEl = target?.closest?.('[data-page]') as HTMLElement | null;
  const mdEl = target?.closest?.('[data-md-page]') as HTMLElement | null;
  const imgEl = target?.closest?.('img') as HTMLImageElement | null;
  const markEl = target?.closest?.('mark.hl[data-hl]') as HTMLElement | null;
  const line: 'pdf' | 'rows' | 'md' = pdfEl ? 'pdf' : (mdEl ? 'md' : 'rows');
  const page = Number(pdfEl?.getAttribute('data-pdf-page') ?? pageEl?.getAttribute('data-page')
    ?? mdEl?.getAttribute('data-md-page') ?? 0) || currentPage.value;
  /* 选区是否就在这次右键的落点上：右键别处时不该冒出"复制选区/高亮选区"（那是上一次划的词）。
     ⚠️ 两道判据必须都留着：浏览器在右键 `mousedown` 时**会把选区折叠掉**（实测），
     那时 `getSelection()` 已经拿不到选区，只能退回 `selection.rect` 这份快照；
     而我们自己又在右键 mousedown 上 `preventDefault` 保住了选区 —— 两条路都得走得通。 */
  const sel = window.getSelection();
  const range = sel && !sel.isCollapsed && sel.rangeCount ? sel.getRangeAt(0) : null;
  const inSel = !!(target && (() => {
    if (range) { try { return range.intersectsNode(target); } catch { return false; } }
    const r = selection.rect;
    if (!r) return false;
    return ev.clientX >= r.left - 2 && ev.clientX <= r.right + 2
      && ev.clientY >= r.top - 2 && ev.clientY <= r.bottom + 2;
  })());
  const withSel = inSel && selection.show && !!selection.text;
  const figRect = imgEl?.getBoundingClientRect();
  ctxCtx = {
    line,
    text: withSel ? (copyPayload(sel as Selection)?.text ?? selection.text) : '',
    page,
    hlIds: markEl?.dataset.hl ? highlightIdsInRange(document.createRange(), markEl) : [],
    imgUrl: imgEl?.src
      || (line === 'pdf' ? assetUrl(`/api/v1/papers/${paperId.value}/pages/${page}`) : ''),
    imgName: imgEl
      ? `${paperId.value}-${(imgEl.getAttribute('alt') || `p${page}`).replace(/[\\/:*?"<>|\s]+/g, '-').slice(0, 40)}.png`
      : `${paperId.value}-p${page}.png`,
    imgCaption: imgEl?.getAttribute('alt') || (line === 'pdf' ? `第 ${page} 页` : ''),
    isFigure: !!imgEl,
    imgRect: figRect
      ? { x: figRect.left, y: figRect.top, w: figRect.width, h: figRect.height } : null,
  };
  const items: CtxItem[] = [];
  /* 文案一律精简（用户口径 2026-10-08："精简右键菜单文字，更换图标"）；
     "搜索"那一行**把选中的文字摆出来**（对齐 Edge 的 `搜索 “…”`）—— 一眼能确认搜的是哪一段。
     2026-10-09：按用户口径**复刻目标站（fakenewsdetect.art）的顺序与图标** ——
     选区工具（搜索 / 复制 / 高亮）在前，全选 → 图片 → 导出依次往后，
     **危险项（删除）压到最后**。图标：以填充型（`-fill`）为主，
     但「高亮」「询问 Cyli」「加入对话」**保留原来的线性图标**、「导出」用 **download**。
     同日再按口径把长文案**压到两个字**：全选本页→全选、另存为图片→另存为、
     放大打开→放大、删除高亮→删除、导出 Markdown→导出（与目标站的叫法一致）；
     **AI 相关项（询问 Cyli / 加入对话 / 图片加入对话）不动**。 */
  const quoted = ctxCtx.text.replace(/\s+/g, ' ').trim().slice(0, 14);
  if (withSel) {
    items.push({
      key: 'bing', icon: 'search-fill',
      label: quoted ? `搜索 “${quoted}${ctxCtx.text.length > 14 ? '…' : ''}”` : '搜索',
    });
    items.push({ key: 'copy', label: '复制', icon: 'copy-fill', shortcut: 'Ctrl+C' });
  }
  /* 「高亮」不再放在常规菜单里（用户口径 2026-10-10）：选色已经在**划词胶囊**上，
     右键菜单这一层重复一遍反而是噪音。右键落在**已有高亮**上才给「编辑笔记」。
     ⚠️ 这一项**不能写在 `if (withSel)` 里** —— 那样"只右键一处高亮、并没有划词"时，
     菜单里就只剩「删除」，想改笔记反而没有入口（用户 2026-10-10 复查报的正是这个）。
     它只跟 `hlIds` 走，与有没有选区无关。 */
  if (ctxCtx.hlIds.length) {
    items.push({ key: 'note', label: '编辑笔记', icon: 'note' });
  }
  if (withSel) {
    items.push({ key: 'ask', label: '询问 Cyli', icon: 'sparkle' });
    items.push({ key: 'tochat', label: '加入对话', icon: 'chat' });
  } else if (ctxCtx.isFigure) {
    /* 插图：**先给「询问 Cyli」**（解释这张图），再是「加入对话」（2026-10-09） */
    items.push({ key: 'ask', label: '询问 Cyli', icon: 'sparkle' });
    items.push({ key: 'tochat', label: '加入对话', icon: 'chat' });
  }
  items.push({ key: 'selectall', label: '全选', icon: 'select-all-fill', sep: items.length > 0 });
  if (ctxCtx.imgUrl) {
    items.push({ key: 'saveimg', label: '另存为', icon: 'save-alt-fill', sep: true });
    items.push({ key: 'zoomimg', label: '放大', icon: 'open-new-fill' });
  }
  items.push({ key: 'export', label: '导出', icon: 'download', sep: true });
  if (ctxCtx.hlIds.length) {
    items.push({ key: 'delhl', label: '删除', icon: 'delete-fill', danger: true, sep: true });
  }
  ctxMenu.items = items;
  ctxMenu.x = ev.clientX;
  ctxMenu.y = ev.clientY;
  ctxMenu.open = true;
  ev.preventDefault();                    // 收掉浏览器原生菜单（要的就是它换成这一张）
}

function ctxBing(text: string) {
  const q = text.trim().slice(0, 300);
  if (!q) return;
  window.open(`https://www.bing.com/search?q=${encodeURIComponent(q)}`, '_blank', 'noopener');
}

/** 全选"本页"：PDF 选这一页的文字层、Markdown 选这一页、正文行选这一页的那一组。 */
function ctxSelectPage() {
  const { line, page } = ctxCtx;
  const root = line === 'pdf'
    ? document.querySelector<HTMLElement>(`[data-pdf-page="${page}"] .pdf-text-layer`)
    : line === 'md'
      ? document.querySelector<HTMLElement>(`[data-md-page="${page}"]`)
      : document.querySelector<HTMLElement>(`[data-page="${page}"]`);
  const sel = window.getSelection();
  if (!root || !sel) return;
  const range = document.createRange();
  range.selectNodeContents(root);
  sel.removeAllRanges();
  sel.addRange(range);
  hideMenu();
  toast(`已选中第 ${page} 页${line === 'pdf' ? ' PDF 文字' : '正文'}`, 'ok', 1600);
}

/** 图片另存为：存**原图**（PDF 页 = 后端渲染的页 PNG；插图 = 它自己的原图地址）。 */
async function ctxSaveImage() {
  if (!ctxCtx.imgUrl) return;
  try {
    const res = await fetch(ctxCtx.imgUrl);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    saveBlob(ctxCtx.imgName, await res.blob());
    toast(`已保存 ${ctxCtx.imgName}`, 'ok');
  } catch (err) { reportError(err, '保存图片失败'); }
}

function ctxZoomImage() {
  if (!ctxCtx.imgUrl) return;
  zoomImage.value = { url: ctxCtx.imgUrl, caption: ctxCtx.imgCaption, page: ctxCtx.page };
}

/** 图片也能进对话框（用户口径："添加到对话框（支持图片）"）：取回原图当附件上传。 */
async function ctxAttachImage() {
  if (!ctxCtx.imgUrl) return;
  try {
    const res = await fetch(ctxCtx.imgUrl);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const blob = await res.blob();
    const file = new File([blob], ctxCtx.imgName, { type: blob.type || 'image/png' });
    await ensureAsideVisible();
    sideTab.value = 'chat';
    await nextTick();
    (chatRef.value as { uploadFiles?: (f: File[]) => void } | null)?.uploadFiles?.([file]);
  } catch (err) { reportError(err, '把图片放进对话框失败'); }
}

/** 右键菜单色板里点了一枚色点（`ContextMenu` 的 `color` 事件）：按那个颜色高亮。
 *  与行本身的默认色（黄）走的是同一个动作 —— 行只是色板的"默认项"。 */
function onCtxColor(color: string) {
  hideMenu();
  void addHighlight(color);
}

function onCtxSelect(key: string) {
  const c = ctxCtx;
  switch (key) {
    case 'ask':
      hideMenu();
      if (c.isFigure) void explainImage(); else void explainSelection();
      break;
    case 'tochat':
      if (c.text) openSelectionInChat(true);
      if (c.imgUrl) void ctxAttachImage();
      break;
    case 'bing': ctxBing(c.text); break;
    case 'copy': void copySelection(); break;
    case 'highlight': hideMenu(); void addHighlight('yellow'); break;
    case 'note': void onNoteAction(c.hlIds, { x: ctxMenu.x, y: ctxMenu.y }); break;
    case 'delhl': void deleteHighlightIds(c.hlIds); break;
    case 'selectall': ctxSelectPage(); break;
    case 'saveimg': void ctxSaveImage(); break;
    case 'zoomimg': ctxZoomImage(); break;
    case 'export': downloadMarkdown(); break;
    default: break;
  }
}

/** 划词解释 / 追问：都走问答 agent（同一 conversation_id，追问自带上下文）。
 *  `opts.silent` = **不把这句话渲染成用户气泡**：图片解释的默认问法（"请解释这张图片"）
 *  是系统补的，不是用户打的字（对齐划词解释 —— 那边也是后端补的默认 prompt，不显示气泡）。 */
async function askSelection(question: string, opts: { silent?: boolean } = {}) {
  const q = question.trim();
  if (q && !opts.silent) explainCard.turns.push({ role: 'user', text: q, citations: [] });
  // ⚠️ 必须**通过 reactive 对象的下标**改字段：push 进去的是原始对象，直接改它不会触发更新
  const idx = explainCard.turns.length;
  explainCard.turns.push({ role: 'assistant', text: '', citations: [], steps: [] });
  explainCard.loading = true;
  explainController?.abort();
  explainController = new AbortController();
  /**
   * 流式期间本地拼的过程轨迹（与 `backend/agent/trace.py` 的形状一致）：
   * 思考按**轮**并成一条（一轮几百个增量块，一块一条会刷屏），工具按 start/done 配对。
   */
  const live: AgentStep[] = [];
  const mergeThinking = (text: string, round: number) => {
    const last = live[live.length - 1];
    if (last && last.kind === 'thinking' && last.round === round) last.text = `${last.text ?? ''}${text}`;
    else live.push({ kind: 'thinking', round, text });
  };
  const onEvent = (name: string, data: any) => {
    if (name === 'session' && data?.conversation_id) explainCard.conversationId = data.conversation_id;
    else if (name === 'stage' && data?.message) live.push({ kind: 'stage', round: 0, text: String(data.message) });
    else if (name === 'thinking_delta') mergeThinking(String(data?.text ?? ''), Number(data?.round ?? 0));
    else if (name === 'tool') {
      const tool = String(data?.tool ?? '');
      if (data?.status === 'start') {
        live.push({ kind: 'tool', round: Number(data?.round ?? 0), tool,
                    digest: String(data?.args_digest ?? ''), status: 'running' });
      } else {
        const step = [...live].reverse().find((s) => s.kind === 'tool' && s.tool === tool && s.status === 'running');
        if (step) { step.status = 'done'; step.ms = Number(data?.ms ?? 0); }
      }
    }
    else if (name === 'answer_delta') {
      const chunk = String(data?.text ?? '');
      explainCard.turns[idx].text += chunk;
      /* 轨迹里也留一段（与后端 `run.steps` 同形）：中间那些"我先读一下…"的正文段归轨迹，
         浮层只显示**最后一次工具之后**的那段正文（见 `answerBody`）。不在这里落步的话，
         流式期间就没有轨迹可依，只能显示拼接体 —— 那正是用户报的那个现象。 */
      const last = live[live.length - 1];
      if (last && last.kind === 'text') last.text = `${last.text ?? ''}${chunk}`;
      else live.push({ kind: 'text', round: Number(data?.round ?? 0), text: chunk });
    }
    else if (name === 'done') {
      explainCard.turns[idx].text = data?.answer ?? explainCard.turns[idx].text;
      explainCard.turns[idx].citations = data?.citations ?? [];
      // `done` 带的 `run` 是后端权威轨迹（轮次、合并后的思考、工具耗时都在里面）
      const steps = (data?.run?.steps ?? []) as AgentStep[];
      explainCard.turns[idx].steps = steps.length ? steps : live;
    } else if (name === 'error') {
      explainCard.turns[idx].text = explainCard.turns[idx].text || `（${data?.message ?? '失败'}）`;
      explainCard.turns[idx].steps = live;
    }
  };
  try {
    await postSse('/agent/ask', {
      paper_id: paperId.value, question: q,
      conversation_id: explainCard.conversationId,
      // 插图解释：**每轮都把附件带上**（理由是历史不带图，见 `explainCard.attachments`）
      attachments: explainCard.attachments.length ? explainCard.attachments : undefined,
      selection: {
        text: explainCard.selectionText,
        row_id: explainCard.scope === 'row' ? (explainCard.rowId || null) : null,
        page: explainCard.page || null,
      },
    }, { signal: explainController.signal, onEvent });
  } catch (err: any) {
    if (err?.name !== 'AbortError') {
      explainCard.turns[idx].text = explainCard.turns[idx].text || `（提问失败：${err?.message ?? err}）`;
    }
  } finally {
    explainCard.loading = false;
    explainController = null;
  }
}

/** 右键**插图** → 询问 Cyli：把图上传成附件，再走**同一条**问答链路
 *  （`/agent/ask` + `attachments`），浮层锚在那张图旁边。
 *
 *  为什么走附件而不是新接口：问答 agent 本来就吃图片附件（侧栏对话那条路），
 *  `runtime._user_prompt` 会把附件编成多模态消息 —— 这里只是换了个入口，后端一行都不用改。
 *  默认问法由前端补（`silent`：不渲染成用户气泡）。 */
async function explainImage() {
  const c = ctxCtx;
  if (!c.imgUrl) return;
  hideMenu();
  explainCard.open = true;
  explainCard.loading = true;
  explainCard.question = '';
  explainCard.rowId = '';                        // 插图没有正文块锚点
  explainCard.blockAnchored = false;
  explainCard.scope = 'markdown';                // 兜底：那一侧的"存为笔记"已按图收起
  explainCard.selectionText = '';
  explainCard.page = c.page || 0;
  explainCard.conversationId = null;
  explainCard.turns = [];
  explainCard.processOpen = false;
  explainCard.pinned = true;
  explainCard.highlightId = '';
  explainCard.start = 0;
  explainCard.end = 0;
  explainCard.quote = '';
  explainCard.imageUrl = c.imgUrl;
  explainCard.imageCaption = c.imgCaption;
  explainCard.attachments = [];
  // 锚在**图片本身**（`FloatingCard` 会把它摆在图的下沿、放不下就翻到上方）
  explainCard.anchor = c.imgRect
    ? { x: c.imgRect.x, y: c.imgRect.y, w: c.imgRect.w, h: c.imgRect.h }
    : { x: 0, y: 0 };
  /* 先把图"喂"进去再问：上传走侧栏对话那条**同一条**链路（`/agent/attachments`） */
  try {
    const res = await fetch(c.imgUrl);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const blob = await res.blob();
    /* 缩略图直接用**刚取回来的这份 blob**（object URL），不再让卡片去加载那个 URL：
       实测出现过"模型明明收到了图、卡片里却是裂图"—— etch 拿得到、<img> 却不渲染
       （重定向/内容处置头/CSP 都可能造成这种不对称）。这样图和喂给模型的是**同一份字节**。 */
    if (explainCard.imageUrl.startsWith('blob:')) URL.revokeObjectURL(explainCard.imageUrl);
    explainCard.imageUrl = URL.createObjectURL(blob);
    const form = new FormData();
    form.append('files', new File([blob], c.imgName, { type: blob.type || 'image/png' }), c.imgName);
    const data = await api.upload<{ items: Array<{ attachment_id: string }> }>('/agent/attachments', form);
    explainCard.attachments = (data.items ?? []).map((a) => a.attachment_id).filter(Boolean);
  } catch (err) {
    explainCard.loading = false;
    explainCard.turns.push({
      role: 'assistant', citations: [],
      text: `（这张图没能交给 Cyli：${(err as Error)?.message ?? err}）`,
    });
    reportError(err, '把图片交给 Cyli 失败');
    return;
  }
  await askSelection('请解释这张图片：它展示了什么，以及它在这篇论文里说明了什么。', { silent: true });
}

async function explainSelection() {
  // 无块锚点（原 PDF 划词）时只传文本：agent 那边拿不到块就少一层上下文，仍能解释
  const rowId = selection.anchored ? selection.rowId : '';
  hideMenu();
  explainCard.open = true;
  explainCard.loading = true;
  explainCard.question = '';
  explainCard.rowId = rowId;
  explainCard.blockAnchored = selection.anchored;
  explainCard.scope = selection.scope;
  explainCard.selectionText = selection.text;
  explainCard.page = Number(selection.page) || 0;
  explainCard.conversationId = null;
  explainCard.imageUrl = '';                     // 划词解释：把上一轮"解释插图"的残留清掉
  explainCard.imageCaption = '';
  explainCard.attachments = [];
  explainCard.turns = [];
  explainCard.processOpen = false;                    // 过程默认折叠（用户口径）
  explainCard.pinned = true;                          // 每次打开都从"锚定"开始（默认态）
  /* 选区快照 + 浮层锚点：读完回答再点颜色存笔记时，页面上的选区可能早被改了 */
  explainCard.highlightId = selection.hlIds[0] ?? '';
  explainCard.start = selection.start;
  explainCard.end = selection.end;
  explainCard.side = selection.side;
  explainCard.quote = selection.anchorText || selection.text;
  explainCard.anchor = { x: selection.x, y: selection.y, w: 0, h: 0 };
  await askSelection('');
}

function submitExplainQuestion() {
  const q = explainCard.question.trim();
  if (!q || explainCard.loading) return;
  explainCard.question = '';
  void askSelection(q);
}

/** 划词解释历史 = `source=selection` 的批注（`存为批注` 存下来的那些）。 */
/**
 * 一轮助手回复里**真正要给用户看的正文**（用户口径 2026-10-07 第 2 条）。
 *
 * 为什么不能直接用 `t.text`：后端 `done.answer` 是**所有正文段的拼接**
 * （`state.text = sanitizer.clean`）—— 多轮 agent 中间那些"我先读一下这一段…/让我看看附近的上下文"
 * 也在里面，于是浮层第一轮正文就成了"过程碎句 + 最终回答"焊在一起（用户截图报的正是这个）。
 * 那些碎句在 `run.steps` 里本来就有自己的一段（`kind: 'text'`），该显示在**过程轨迹**里。
 *
 * 取法：**最后一次工具调用之后**的正文段拼起来（没有工具调用/后面没正文时退回 `t.text`）。
 * 用"最后一次工具之后"而不是"最后一段"，是因为最终回答也可能被模型拆成两三个 `TextPart`。
 */
function answerBody(t: { text?: string; steps?: AgentStep[] }): string {
  const steps = t.steps ?? [];
  let lastTool = -1;
  steps.forEach((s, i) => { if (s.kind === 'tool') lastTool = i; });
  if (lastTool < 0) return String(t.text ?? '');
  const tail = steps.slice(lastTool + 1)
    .filter((s) => s.kind === 'text' && String(s.text ?? '').trim())
    .map((s) => String(s.text));
  return tail.length ? tail.join('') : String(t.text ?? '');
}

/** 最后那一轮助手回复（浮层的"当前这轮"）。 */
const explainTurn = computed(() => (
  [...explainCard.turns].reverse().find((t) => t.role === 'assistant') ?? null
));
/** 这一轮解释的是**插图**（右键图片 → 询问 Cyli）：底部"标出这段并存为笔记"那套要收起来。 */
const explainIsImage = computed(() => !!explainCard.imageUrl);
/** 这一轮的过程轨迹有没有东西可看（决定"正在读…"那行还要不要） */
const explainSteps = computed(() => (explainTurn.value?.steps ?? []).length > 0);
/**
 * **正在出过程、还没有最终正文**（用户口径第 3 条）：这时把底部的输入框与按钮全藏起来 ——
 * 满屏都是思考/工具在跑，底下却摆着一排"存为…"和提问框，既没法用也干扰读。
 */
const explainBusy = computed(() => (
  explainCard.loading
  && !answerBody(explainTurn.value ?? {}).trim()
));

/** 这处解释对应的高亮（选择了颜色要标一下当前是哪枚） */
const explainColor = computed(() => (
  highlights.value.find((h) => h.id === explainCard.highlightId)?.color ?? ''
));

/** 行 id（`p004-02`）里的页号；不是这个形状就 0（与 agent 的 `_page_of_row` 同一口径）。 */
function pageOfRow(rowId?: string | null): number {
  const m = /^p(\d+)-/.exec(String(rowId ?? ''));
  return m ? Number(m[1]) : 0;
}

/** 选区位置的人话：带进对话时要写进消息正文，也摆在输入框那枚胶囊上。
 *  Markdown 视图特别标出来（那里的锚点是 `md:p<页号>`），PDF 划词没有块锚点、
 *  页号就是唯一的位置线索。 */
function whereOf(scope: string, page: number, anchored: boolean): string {
  if (!page) return anchored ? '' : 'PDF 视图';
  if (!anchored) return `第 ${page} 页 · PDF`;
  return scope === 'markdown' ? `第 ${page} 页 · Markdown` : `第 ${page} 页`;
}

/** 把选区带进侧栏「对话」面板继续聊（历史就落在那个会话里，可回看）。
 *
 *  `fromMenu=true` = 划词菜单里直接点「加入对话」—— 这时 AI 解释浮层还没打开过，
 *  数据得从**当前选区**取（`explainCard` 里是上一次解释的残留，用它必然错位）。
 *  带进去之后落成输入框里那枚上下文胶囊：发不发由用户点它决定（见 `ReaderChat`）。 */
function openSelectionInChat(fromMenu = false) {
  const anchored = fromMenu ? selection.anchored : explainCard.blockAnchored;
  const scope = fromMenu ? selection.scope : explainCard.scope;
  const rowId = anchored ? (fromMenu ? selection.rowId : explainCard.rowId) : '';
  const page = fromMenu
    ? (Number(selection.page) || pageOfRow(selection.rowId))
    : (Number(explainCard.page) || pageOfRow(explainCard.rowId));
  chatSelection.value = {
    text: fromMenu ? selection.text : explainCard.selectionText,
    row_id: scope === 'row' ? (rowId || null) : null,
    page: page || null,
    where: whereOf(scope, page, anchored),
  };
  explainCard.open = false;
  hideMenu();
  sideTab.value = 'chat';
  // 左栏可能是收起的（例如用户在看 Markdown 视图）——切页签不等于看得见面板
  void ensureAsideVisible();
}

/**
 * 点解释浮层底部的**来源 chunk**：跳到那一段。
 *
 * 用户口径 2026-10-07：**浮层锚定之后，这里跳转不能把浮层关掉** ——
 * 用法就是"一边看着解释、一边跳过去核对"。没锚定的（临时浮层）仍然顺手关掉。
 */
/**
 * 解释正文的渲染：把 `[3]` 这类标号接成**可点的小按钮**，并把来源（行/页）刻在按钮上。
 *
 * 为什么自己做（原来是 `<div v-html="renderMarkdown(...)">`）：正文里的标号点不动 ——
 * `onContentClick` 只认正文里的 `[data-refs]`（参考文献），解释浮层里的标号谁也没接。
 * 底部那排"来源标号"撤掉之后（用户口径 2026-10-09），**跳转就全靠正文里这些标号**。
 */
function renderExplainAnswer(t: { text: string; citations: AgentCitation[] }): string {
  const html = renderMarkdown(answerBody(t));
  const byRef = new Map((t.citations ?? []).map((c) => [String(c.ref), c]));
  if (!byRef.size) return html;
  return html.replace(/\[(\d{1,3})\]/g, (whole, num) => {
    const c = byRef.get(String(num));
    if (!c) return whole;
    const row = (c.row_id || '').replace(/"/g, '&quot;');
    const page = Number(c.page || 0) || '';
    const tip = (c.snippet || c.label || '').replace(/"/g, '&quot;').slice(0, 120);
    return `<button type="button" class="cite-inline" data-ex-cite="${num}"`
      + ` data-row="${row}" data-page="${page}" title="${tip}">[${num}]</button>`;
  });
}

/** 点解释正文里的标号 → 跳到那条来源（与原来底部标号同一动作，入口换到正文里） */
function onExplainCite(ev: MouseEvent) {
  const btn = (ev.target as HTMLElement | null)?.closest?.('[data-ex-cite]') as HTMLElement | null;
  if (!btn) return;
  const rowId = btn.dataset.row || '';
  const page = Number(btn.dataset.page || 0) || undefined;
  if (!rowId && !page) return;
  focusRow(rowId, { page, flash: true });
}

function focusCitation(citation: AgentCitation) {
  if (!explainCard.pinned) explainCard.open = false;
  focusRow(citation.row_id || citation.block_id || '', { page: citation.page ?? undefined, flash: true });
}

/* ------------------------------ AI 解释浮层里的"过程" ------------------------------
 * 与侧栏对话那条轨迹（`ReaderChat` 的 `TOOL_STEPS` / `TOOL_ICONS`）是同一套工具，
 * 但这里只需要"一行摘要 + 图标"，所以映射写小一份；新增工具时两处一起补（fallback 是中性图标）。 */
const EX_TOOL_ICON: Record<string, string> = {
  paper_search: 'search', page_markdown_get: 'document', block_get: 'list',
  translate_get: 'translate', kb_search: 'knowledge', formula_get: 'formula',
  figure_list: 'image', library_list: 'library', notes_search: 'note',
  page_markdown_edit: 'edit', page_image_get: 'image', figure_image_get: 'image',
};
const EX_TOOL_LABEL: Record<string, string> = {
  paper_search: '检索全文', page_markdown_get: '读正文', block_get: '读段落',
  translate_get: '读译文', kb_search: '翻书架', formula_get: '查公式',
  figure_list: '查插图', library_list: '翻书架', notes_search: '查笔记',
  page_markdown_edit: '改产物', page_image_get: '看原版面', figure_image_get: '看插图',
};

function stepIcon(s: AgentStep): string {
  if (s.kind === 'thinking') return 'sparkle';
  if (s.kind === 'stage') return 'task';
  if (s.kind === 'text') return 'note';
  return EX_TOOL_ICON[String(s.tool ?? '')] ?? 'bolt';
}
function stepLabel(s: AgentStep): string {
  if (s.kind === 'thinking') return '思考';
  if (s.kind === 'stage') return '准备';
  if (s.kind === 'text') return '正文';
  return EX_TOOL_LABEL[String(s.tool ?? '')] ?? '工具';
}
/** 折叠着的那一行写什么：几段思考、几次工具（用户口径：默认只展示最终回答）。 */
function stepSummary(steps: AgentStep[]): string {
  const think = steps.filter((s) => s.kind === 'thinking').length;
  const tool = steps.filter((s) => s.kind === 'tool').length;
  const parts: string[] = [];
  if (think) parts.push(`${think} 段思考`);
  if (tool) parts.push(`${tool} 次工具`);
  return `深度思考与工具调用（${parts.join(' · ') || '过程'}）`;
}

/**
 * 浮层底部那排色块（用户口径 2026-10-07 第 1 条）：**点颜色 = 标出这段 + 把解释正文存成它的笔记**。
 *
 * 为什么把原来那两枚「存为笔记 / 存为批注」换掉：
 * - 「存为批注」那套（`/annotations/save`）已经过时 —— 批注/高亮/笔记三样在界面上早就合并成「笔记」；
 * - 「存为笔记」还要求"先去正文里标高亮再回来点它"，一次动作被拆成两步；
 * - 合成一步之后，"想留这段"与"想标黄色"本来就是同一个意图：挑个颜色就完了。
 *
 * 快照仍是**打开浮层那一刻**存下的（`explainCard.highlightId/start/end/quote/scope`）：
 * 读完回答再点颜色时，页面上的选区可能早被改了。已有高亮就**改它的颜色**（`PUT /highlights/{id}`），
 * 没有才按当前的锚点新建一条（笔记永远挂在高亮上，口径不变）。
 */
async function saveExplanationWithColor(color: string) {
  const answer = answerBody(explainTurn.value ?? {}).trim();
  if (!answer) { toast('还没有可保存的解释', 'warn'); return; }
  const scope = explainCard.scope;
  /* 锚点：正文行用行 id；Markdown / 原 PDF 两面的锚点是 `md:p<页>` / `pdf:p<页>` */
  const blockId = scope === 'markdown' ? mdAnchor(explainCard.page)
    : scope === 'pdf' ? pdfAnchor(explainCard.page)
      : explainCard.rowId;
  const anchored = scope === 'markdown' || scope === 'pdf'
    ? (explainCard.page > 0 && explainCard.end > explainCard.start)
    : !!explainCard.rowId;
  if (!explainCard.highlightId && !anchored) {
    toast('这段文字定位不到（标不了高亮）', 'warn', 2600);
    return;
  }
  try {
    let highlightId = explainCard.highlightId;
    if (highlightId) {
      await api.put(`/highlights/${highlightId}`, { color });
    } else {
      const created = await api.post<Highlight>('/highlights', {
        paper_id: paperId.value, block_id: blockId,
        selected_text: explainCard.quote || explainCard.selectionText,
        start_offset: explainCard.start, end_offset: explainCard.end,
        color, side: explainCard.side, scope,
      });
      highlightId = created?.id ?? '';
      explainCard.highlightId = highlightId;
      highlights.value.push({
        ...created, block_id: blockId, start: created.start, end: created.end,
        side: explainCard.side, scope,
      } as Highlight);
    }
    if (!highlightId) { toast('高亮没建成，笔记也就没存', 'warn', 2600); return; }
    /* 同一处高亮已经有笔记就更新它（用户可以再解释一次、再点一次颜色） */
    const existing = notes.value.find((n) => n.highlight_id === highlightId) ?? null;
    if (existing) await api.put(`/notes/${existing.id}`, { content_md: answer });
    else {
      await api.post('/notes', {
        paper_id: paperId.value, block_id: blockId, highlight_id: highlightId,
        selected_text: explainCard.quote || explainCard.selectionText,
        start_offset: explainCard.start, end_offset: explainCard.end,
        content_md: answer,
      });
    }
    toast(existing ? '已更新这处高亮的笔记' : '已标色并存为这处高亮的笔记', 'ok', 2400);
    await refreshAnnotations();
  } catch (err) { reportError(err, '存为笔记失败'); }
}

/* ------------------------------ 高亮 / 笔记 ------------------------------ */
/** Markdown 视图里高亮的锚点：`md:p<页号>`（`block_id` 列宽 32，够用；与正文行 id 不会撞） */
const mdAnchor = (page: number) => `md:p${page}`;
/**
 * **原 PDF 页图**里高亮的锚点：`pdf:p<页号>`（《修改计划》第三步）。
 *
 * 为什么 PDF 必须有自己的一套锚点：它的文字来自 pdf.js 的文字层，与"正文行"（页级产物的行 id）
 * 和"Markdown 视图"（`md:p<页号>` + 渲染后文字的偏移）**都不是同一套东西**。偏移量相对
 * **该页文字层的渲染文本**（`utils/domMarks.rangeOffsets` 算出来的那一套，与正文行/Markdown 同一口径），
 * 所以重新渲染同一页时能原样落回去；`selected_text` 一并存下来，位置漂了还认得出是哪一段。
 */
const pdfAnchor = (page: number) => `pdf:p${page}`;

/** 划词切出来的一段（正文按行切、Markdown 按页切），每段存成一条高亮记录、共用一个分组。 */
interface MdSegment {
  rowId: string;
  start: number;
  end: number;
  text: string;
  /** 这一段的盒子（菜单定位用；正文段没有） */
  rect?: DOMRect;
  /** Markdown 视图里的页号（跨页时用来排序与断言） */
  page?: number;
}

async function addHighlight(color: string): Promise<Highlight[]> {
  const scope = selection.scope;
  // 多段选区（正文跨行 / Markdown 视图跨页）：每段一条记录，共用同一个分组 id（删除时整组一起删）
  const parts = selection.segments.length
    ? selection.segments
    : [{
      rowId: scope === 'markdown' ? mdAnchor(selection.mdPage) : selection.rowId,
      start: selection.start, end: selection.end, text: selection.text,
    }];
  const group = parts.length > 1 ? `g-${Date.now().toString(36)}-${parts.length}` : null;
  try {
    const created: Highlight[] = [];
    for (const p of parts) {
      const blockId = p.rowId;
      const data = await api.post<Highlight>('/highlights', {
        paper_id: paperId.value,
        block_id: blockId,
        // 每段存**本段自己的文字**（跨行/跨页时各段文字不一样），校验才过得去
        selected_text: p.text || selection.text,
        start_offset: p.start,
        end_offset: p.end,
        color,
        // **原文 / 译文分开存**：同一行两侧共用一个 block_id，没有 side 就画错到另一侧
        side: selection.side,
        scope,
        group_id: group,
      });
      created.push({
        ...data, block_id: blockId, start: data.start, end: data.end,
        side: selection.side, scope, group_id: group,
      });
    }
    highlights.value.push(...created);
    const unit = scope === 'row' ? '行' : '页';
    toast(parts.length > 1 ? `已高亮（跨 ${parts.length} ${unit}）` : '已高亮', 'ok', 2000);
    /* 用户口径（2026-10-07）：**选中并高亮之后，浮层要显示「添加笔记」** ——
       标完就把这处高亮的笔记浮层叫出来（`prompt` 态：只有原文 + 一枚「添加笔记」按钮）。
       已经在这处高亮上写过笔记的（同一段重复标色）直接显示那条笔记。 */
    const first = created[0] ?? null;
    const existing = first ? (notes.value.find((n) => n.highlight_id === first.id) ?? null) : null;
    if (first) {
      openNotePopover({
        note: existing, highlight: first, mode: existing ? 'view' : 'prompt', anchored: true,
        fallbackXY: { x: selection.x, y: selection.y },
      });
    }
    return created;
  } catch (err) { reportError(err, '高亮失败'); return []; } finally { hideMenu(); }
}

/**
 * 删一串高亮（用户口径：删除走菜单的「禁止」图标或侧栏笔记那一条，不点高亮本身）。
 * 后端会把同一分组（跨行高亮）的记录一起删掉，并回传所有被删的 id，这里同步本地列表。
 * 挂在被删高亮上的笔记由后端级联删掉（`Highlight └── Note`）。
 */
async function deleteHighlightIds(ids: string[]) {
  if (!ids.length) return;
  const gone = new Set<string>(ids);
  // 逐条删。**单条失败不能中断**：跨行高亮是"同组多条"，删第一条时后端已经把整组删掉并
  // 回传了 `deleted_ids`，后面那些 id 再删就是 404 —— 以前这里一个异常就把整段跳过，
  // 结果是"库里的确删了、界面上的高亮还在"（自检里就是这么抓到的）。
  //
  // 但**同一分组只发一次请求**：既然后端删一条就带走整组，兄弟 id 就没必要再问一遍 ——
  // 以前对每个 id 都发一次（`Promise.all` 并发），日志里固定出现一条 404，
  // 看着像删除失败（用户报过："其实能正常删除和保存高亮"）。分组从本地列表查
  // （`hlIds` 里同组的记录共享 `group_id`；没分组的自然各发各的）。
  const seenGroups = new Set<string>();
  const targets: string[] = [];
  for (const id of ids) {
    const group = highlights.value.find((h) => h.id === id)?.group_id;
    if (group) {
      if (seenGroups.has(group)) continue;
      seenGroups.add(group);
    }
    targets.push(id);
  }
  await Promise.all(targets.map(async (id) => {
    try {
      const res = await api.del<{ deleted_ids?: string[] }>(`/highlights/${id}`);
      (res?.deleted_ids ?? []).forEach((x) => gone.add(x));
    } catch { /* 已经被删掉了（另一个标签页 / 同组兄弟）：忽略 */ }
  }));
  highlights.value = highlights.value.filter((h) => !gone.has(h.id));
  notes.value = notes.value.filter((n) => !n.highlight_id || !gone.has(n.highlight_id));
  toast(`已删除 ${gone.size} 处高亮`, 'ok', 1800);
}

async function deleteSelectionHighlights() {
  const ids = selection.hlIds.slice();
  hideMenu();
  await deleteHighlightIds(ids);
}

/**
 * 侧栏「笔记」页签里写/改一条笔记。
 *
 * 用户口径：**只有插入了高亮，才能在高亮的基础上加笔记** —— 所以新建时必定带着 `highlightId`；
 * 高亮的原文/偏移量从本地那份高亮里取（侧栏只传了 id 与正文），不重复存一份。
 */
async function savePaneNote(p: NoteDraftPayload) {
  if (!p.contentMd.trim()) { toast('笔记内容不能为空', 'warn', 2200); return; }
  try {
    if (p.noteId) {
      const updated = await api.put<Note>(`/notes/${p.noteId}`, { content_md: p.contentMd });
      const hit = notes.value.find((n) => n.id === p.noteId);
      if (hit) hit.content_md = updated?.content_md ?? p.contentMd;
    } else {
      const hl = highlights.value.find((h) => h.id === p.highlightId) ?? null;
      if (!hl) { toast('先给这段文字加个高亮，再写笔记', 'warn', 2600); return; }
      await api.post<Note>('/notes', {
        paper_id: paperId.value,
        block_id: hl.block_id ?? p.anchorId,
        highlight_id: hl.id,
        selected_text: hl.text ?? '',
        start_offset: hl.start, end_offset: hl.end,
        content_md: p.contentMd,
      });
    }
    toast('笔记已保存', 'ok', 1600);
    await refreshAnnotations();
  } catch (err) { reportError(err, '保存笔记失败'); }
}

/**
 * 点已有高亮（没拖选）：把划词菜单叫出来，里面那个「禁止」图标负责删除。
 * 以前是"点一下弹原生确认框直接删"，用户口径改成菜单删除。
 */
function onContentClick(ev: MouseEvent) {
  /* 引用标号（`[2, 4, 10]`）优先：它是正文里最容易被点的小东西，别跟高亮那套混在一起。
     ⚠️ 认 `[data-refs]`：对话页的跳转标号用的是同一枚 `.cite-inline` 类（但带 `data-ref`），
     它们有自己的处理器，不能被这里抢走。 */
  const cite = (ev.target as HTMLElement).closest?.('.cite-inline[data-refs]') as HTMLElement | null;
  if (cite?.dataset.refs) {
    ev.preventDefault();
    cancelCiteOpen();                      // 点一下是明确动作：不等悬浮那 350ms，立刻弹
    void openCitePop(citeRefs(cite), cite);
    return;
  }
  const mark = (ev.target as HTMLElement).closest?.('[data-hl]') as HTMLElement | null;
  if (!mark?.dataset.hl) return;
  const rect = mark.getBoundingClientRect();
  const ids = highlightIdsInRange(document.createRange(), mark);
  Object.assign(selection, {
    show: true, ...menuPos(rect), text: truncate(mark.textContent ?? '', 600),
    rowId: '', side: 'en' as const, start: 0, end: 0, anchored: false,
    scope: 'row' as const, mdPage: 0, allowNote: false, segments: [], hlIds: ids,
  });
}

/** Markdown 视图里的高亮同样走菜单删除（弹窗传送到 body，正文上的处理器管不到）。
 *  插图点击则开大图：正文插图被 CSS 收小了（用户口径"不要那么大"），没有放大入口就看不清细节；
 *  复用正文插图那套弹窗，图注取 `alt`（Markdown 图片语法 `![图注](…)` 里存的就是图注），
 *  页码取所在页（`md-page` 上带着 `data-md-page`）。 */
function onMdClick(ev: MouseEvent) {
  const target = ev.target as HTMLElement | null;
  const img = target?.closest?.('img');
  if (img && img.closest('.md-doc')) {
    ev.preventDefault();
    hideMenu();
    zoomImage.value = {
      url: img.getAttribute('src') ?? '',
      caption: img.getAttribute('alt') ?? '',
      page: Number(img.closest('.md-page')?.getAttribute('data-md-page') || 0) || null,
    };
    return;
  }
  onContentClick(ev);
}

/**
 * 笔记浮层（《修改计划》第五步）：锚在高亮附近的小卡片，取代原来那块**居中大弹窗**。
 *
 * 三种打开方式：
 * - **刚高亮完**：`mode='prompt'`（只有原文 + 一枚「添加笔记」）—— 用户口径"选中高亮后浮层显示添加笔记"；
 * - **悬停高亮**：只看**已有**笔记（`mode='view'`），鼠标移开自动消失（除非已锚定）；
 * - **划词菜单点「添加笔记 / 打开笔记」**：新建（`mode='edit'`）或查看已有那条（`mode='view'`）。
 */
const notePop = reactive({
  open: false,
  /** 锚定：鼠标移开也不关（点浮层那枚图钉切换）。**默认不锚定**（用户口径） */
  anchored: false,
  mode: 'view' as 'view' | 'edit' | 'prompt',
  noteId: '',
  highlightId: '',
  rowId: '',
  /** 这处高亮**现在的颜色**：笔记卡内嵌色条靠它显示"选中"；改色走 `PUT /highlights/:id`。 */
  color: 'yellow',
  /** 这条笔记的时间戳（头部那行"不到 1 分钟前"，复刻目标站）。只用于展示。 */
  created: '',
  /** 高亮原文（浮层"引用"那一行） */
  quote: '',
  content: '',
  draft: '',
  anchor: null as { x: number; y: number; w?: number; h?: number } | null,
  busy: false,
});

function rectAnchor(rect: DOMRect | null | undefined, fallback?: { x: number; y: number }) {
  if (rect) return { x: rect.left, y: rect.top, w: rect.width, h: rect.height };
  return { x: fallback?.x ?? 40, y: fallback?.y ?? 120, w: 0, h: 0 };
}

/** 打开浮层（查看已有笔记 / 编辑某条 / 新建）。 */
function openNotePopover(opts: {
  note?: Note | null; highlight?: Highlight | null; mode?: 'view' | 'edit' | 'prompt';
  rect?: DOMRect | null; anchored?: boolean; fallbackXY?: { x: number; y: number };
}) {
  const note = opts.note ?? null;
  const hl = opts.highlight ?? null;
  notePop.open = true;
  notePop.anchored = opts.anchored ?? false;
  notePop.mode = opts.mode ?? (note ? 'view' : 'edit');
  notePop.noteId = note?.id ?? '';
  notePop.highlightId = note?.highlight_id ?? hl?.id ?? selection.hlIds[0] ?? '';
  notePop.color = String(hl?.color ?? 'yellow');
  /* ⚠️ `Note` 的时间戳字段名没在类型里确认过：用 `as` 兼容两种常见写法，
     拿不到就留空（头部那行不显示），不猜也不报错。 */
  const rawTime = (note ?? null) as unknown as Record<string, unknown> | null;
  notePop.created = String(rawTime?.created_at ?? rawTime?.updated_at ?? '');
  notePop.rowId = note?.block_id ?? hl?.block_id ?? selection.rowId ?? '';
  notePop.quote = note?.selected_text ?? hl?.text
    ?? (selection.anchored ? (selection.anchorText || selection.text) : '');
  notePop.content = note?.content_md ?? '';
  notePop.draft = note?.content_md ?? '';
  notePop.anchor = rectAnchor(opts.rect, opts.fallbackXY);
  notePop.busy = false;
}

/** 相对时间（笔记框头部那行"不到 1 分钟前"，用户口径 2026-10-10："复刻这个笔记框"）。 */
function relTime(iso: string): string {
  const t = Date.parse(iso);
  if (!Number.isFinite(t)) return '';
  const s = Math.max(0, (Date.now() - t) / 1000);
  if (s < 60) return '不到 1 分钟前';
  if (s < 3600) return `${Math.floor(s / 60)} 分钟前`;
  if (s < 86400) return `${Math.floor(s / 3600)} 小时前`;
  if (s < 86400 * 30) return `${Math.floor(s / 86400)} 天前`;
  return new Date(t).toLocaleDateString();
}
const noteTime = computed(() => relTime(notePop.created));

/**
 * 笔记卡的**初始尺寸按内容多少来**（用户口径 2026-10-10："根据内容数量默认调整到合适大小"）。
 *
 * 为什么不用固定 360px：只有一行字的短笔记拉那么宽看着空、长段落挤在 360px 里又一直在折行。
 * 按**最长一行**估宽（中英混排按 ~8.5px/字，再加左右内边距），夹在 340–560 之间：
 * 太窄比正文列还窄、太宽就把正文挡住了。
 *
 * 高度不在这里算 —— 查看态的卡片本来就随内容自然高，编辑态的高度交给 `noteEditRows`。
 */
const noteCardWidth = computed(() => {
  const text = notePop.mode === 'view' ? notePop.content : notePop.draft;
  const longest = (text || '').split('\n').reduce((m, l) => Math.max(m, [...l].length), 0);
  return Math.round(Math.min(560, Math.max(340, longest * 8.5 + 96)));
});

/** 编辑框的行数跟着**已有内容**走：空笔记给 4 行就够（不用先占一片白），长笔记一次全露出来 */
const noteEditRows = computed(() => {
  const lines = (notePop.draft || '').split('\n').length;
  return Math.min(16, Math.max(4, lines + 2));
});

/** 笔记卡里点色点 = **给这处高亮换色**（用户口径 2026-10-10："选色 + 写 + 保存"同一张卡）。
 *  与保存分开：换色只动高亮，不动笔记文本，所以立刻生效。 */
async function setNoteColor(color: string) {
  if (!notePop.highlightId) return;
  try {
    await api.put(`/highlights/${notePop.highlightId}`, { color });
    notePop.color = color;
    await refreshAnnotations();
  } catch (err) { reportError(err, '换颜色失败'); }
}

/** 头部那枚垃圾桶：删掉**这条笔记**（高亮留着 —— 删笔记不该把高亮也带走）。 */
async function deleteNotePopover() {
  if (!notePop.noteId || notePop.busy) return;
  notePop.busy = true;
  try {
    await api.del(`/notes/${notePop.noteId}`);
    toast('笔记已删除', 'ok', 1600);
    closeNotePopover();
    await refreshAnnotations();
  } catch (err) { reportError(err, '删除笔记失败'); } finally { notePop.busy = false; }
}

function closeNotePopover() {  window.clearTimeout(noteHideTimer);
  cancelNoteOpen();                 // 还没到延时的那一次也别再弹了
  notePop.open = false;
  notePop.anchored = false;
  notePop.mode = 'view';
}

/* 悬停显示已存在的笔记；移开延时关闭（延时是为了让鼠标能从高亮挪到浮层上）
 *
 * ⚠️ 浮层自己也要接 `mouseleave`（模板上那一条）：卡片高的时候会自动翻到高亮**上方**，
 * 于是常常一打开就**盖住了光标** —— 那时指针的 hover 目标已经是卡片，`mouseout` 交回的是卡片、
 * 不是高亮，光靠 `onContentHoverOut` 永远不会收（实测：移开后浮层还在）。
 * 锚定的（`anchored`）与编辑态不受影响，`scheduleNoteHide` 自己会挡。 */
let noteHideTimer: number | undefined;
function cancelNoteHide() { window.clearTimeout(noteHideTimer); }
function scheduleNoteHide() {
  if (!notePop.open || notePop.anchored || notePop.mode !== 'view') return;
  window.clearTimeout(noteHideTimer);
  noteHideTimer = window.setTimeout(() => {
    /* 鼠标还在浮层上（从高亮挪进来）就别关：直接问 DOM 的 `:hover`，
     * 比"进/出浮层各挂一个事件"稳 —— 后者在浮层刚好渲染在光标底下时会误判。 */
    const box = document.querySelector('[data-float-card]');
    if (box && box.matches(':hover')) return;
    notePop.open = false;
  }, 240);
}

function markOf(ev: Event): HTMLElement | null {
  const t = ev.target as HTMLElement | null;
  return (t?.closest?.('mark.hl[data-hl]') as HTMLElement | null) ?? null;
}

/** 悬停到一处高亮：**只看已有笔记**（新建走划词菜单，免得鼠标扫过就弹编辑框）。
 *  引用标号则相反：**悬浮与点击同一个效果**（用户口径 2026-10-08 第三条）—— 扫过去就弹出那个浮层。 */
function onContentHover(ev: MouseEvent) {
  const chip = (ev.target as HTMLElement | null)?.closest?.('.cite-inline[data-refs]') as HTMLElement | null;
  if (chip) { scheduleCiteOpen(citeRefs(chip), chip); return; }
  const mark = markOf(ev);
  if (!mark) return;
  const hl = highlights.value.find((h) => h.id === (mark.getAttribute('data-hl') || ''));
  const note = hl ? notes.value.find((n) => n.highlight_id === hl.id) : null;
  if (!hl || !note) return;
  if (notePop.open && notePop.noteId === note.id) { cancelNoteOpen(); cancelNoteHide(); return; }
  scheduleNoteOpen(note, hl, mark);      // 悬停也**停一会儿**才弹（与引用标号同门口径）
}

function onContentHoverOut(ev: MouseEvent) {
  /* 挪到**同类**目标上不算"走开"（`mouseout`/`mouseover` 的先后顺序不保证）：
     不然刚排上的那一次会被这里当场取消，表现就是"从这处高亮划到下一处，浮层再也不弹"。 */
  const to = ev.relatedTarget as HTMLElement | null;
  const chip = (ev.target as HTMLElement | null)?.closest?.('.cite-inline[data-refs]');
  if (chip && !to?.closest?.('.cite-inline[data-refs]')) { cancelCiteOpen(); scheduleCiteHide(); }
  if (markOf(ev)) {
    if (!to?.closest?.('mark.hl[data-hl]')) cancelNoteOpen();
    scheduleNoteHide();
  }
}

/**
 * Markdown 视图**不在** `.reader-content` 里（它是个弹窗），所以悬浮/移开要在那边单独接一下 ——
 * 只接**引用标号**（用户口径：悬浮与点击一个效果，悬浮要先停一会儿才弹）。
 *
 * 高亮那套"悬停看笔记"的浮层**没有**接到这里：它是 `FloatingCard`（`z-index: 60`），
 * 在这个弹窗（遮罩 `z-index: 150`）底下根本看不见 —— 接上去只会"悬停没反应"。
 */
function onMdHover(ev: MouseEvent) {
  const chip = (ev.target as HTMLElement | null)?.closest?.('.cite-inline[data-refs]') as HTMLElement | null;
  if (chip) scheduleCiteOpen(citeRefs(chip), chip);
}

function onMdHoverOut(ev: MouseEvent) {
  if (!(ev.target as HTMLElement | null)?.closest?.('.cite-inline[data-refs]')) return;
  // 挪到另一枚标号上不算走开（同上：mouseout/mouseover 的先后顺序不保证）
  if ((ev.relatedTarget as HTMLElement | null)?.closest?.('.cite-inline[data-refs]')) return;
  cancelCiteOpen();
  scheduleCiteHide();
}

/** 选区/点击处那条高亮对应的笔记（有的话菜单显示「打开笔记」）。 */
const selectionNote = computed<Note | null>(() => {
  const ids = selection.hlIds ?? [];
  if (!ids.length) return null;
  return notes.value.find((n) => n.highlight_id && ids.includes(n.highlight_id)) ?? null;
});

/**
 * 划词菜单的「笔记 / 打开笔记」**和右键菜单的「编辑笔记」**共用这一个入口：
 * 已有笔记 → 打开**浮层**看它（点菜单是明确动作，所以直接锚定）；
 * 没有 → 打开浮层新建（保存时先确保挂到一条高亮上）。
 *
 * ⚠️ `ids` / `anchorXY` 是**右键**那条路径传进来的（用户 2026-10-10 报的：
 * 右键一处高亮→编辑笔记，只弹“先给这段文字加个高亮”）。
 * 原因：这里原来只看 `selection.hlIds` —— 那是**划词胶囊**的状态；右键时往往根本没划词，
 * 于是“右键点在高亮上”这个明确意图被丢掉，既进不了编辑、又报了个莫名其妙的错。
 * 不传参数时（从划词菜单进）仍然用选区那份，行为不变。
 */
async function onNoteAction(ids?: string[], anchorXY?: { x: number; y: number }) {
  const hlIds = ids && ids.length ? ids : (selection.hlIds ?? []);
  const hlOf = (id: string | undefined | null) => highlights.value.find((h) => h.id === id) ?? null;
  const existing = hlIds.length
    ? (notes.value.find((n) => n.highlight_id && hlIds.includes(n.highlight_id)) ?? null)
    : null;
  // 浮层锚在被点的位置（划词菜单在选区那儿，右键菜单在鼠标那儿）—— 菜单本身马上要收起
  const fallbackXY = anchorXY ?? { x: selection.x, y: selection.y };
  hideMenu();
  if (existing) {
    openNotePopover({ note: existing, highlight: hlOf(existing.highlight_id), mode: 'view', anchored: true, fallbackXY });
    return;
  }
  // 没有高亮就没有"可以挂笔记的地方"（用户口径 2026-10-10："没有高亮不用写"）
  if (!hlIds.length) {
    toast('先给这段文字加个高亮，再写笔记', 'warn', 2600);
    return;
  }
  openNotePopover({ mode: 'edit', anchored: true, fallbackXY, highlight: hlOf(hlIds[0]) });
}

/** 浮层里「保存」：已有笔记走 PUT，新建走 POST（**必须已经挂在一处高亮上**）。 */
async function saveNotePopover() {
  if (notePop.busy) return;
  /* 用户口径：**只有插入了高亮，才能在高亮的基础上加笔记** ——
     用户 2026-10-10 再次确认："没有高亮不用写"，所以**不**顺手补一条高亮。 */
  if (!notePop.noteId && !notePop.highlightId) {
    toast('先给这段文字加个高亮，再写笔记', 'warn', 2600);
    return;
  }
  notePop.busy = true;
  try {
    if (notePop.noteId) {
      const updated = await api.put<Note>(`/notes/${notePop.noteId}`, { content_md: notePop.draft });
      notePop.content = updated?.content_md ?? notePop.draft;
      toast('笔记已保存', 'ok', 1600);
    } else {
      const created = await api.post<Note>('/notes', {
        paper_id: paperId.value,
        block_id: notePop.rowId,
        highlight_id: notePop.highlightId,
        selected_text: notePop.quote,
        start_offset: selection.start,
        end_offset: selection.end,
        content_md: notePop.draft,
      });
      notePop.noteId = created?.id ?? '';
      notePop.content = notePop.draft;
      notePop.created = new Date().toISOString();   // 头部那行"不到 1 分钟前"要立刻有值
      toast('笔记已保存', 'ok', 1600);
    }
    notePop.mode = 'view';
    await refreshAnnotations();
  } catch (err) { reportError(err, '保存笔记失败'); } finally { notePop.busy = false; }
}

async function refreshAnnotations() {  const [a, h, n] = await Promise.all([
    api.get<{ items: Annotation[] }>('/annotations', { paper_id: paperId.value }),
    api.get<{ items: Highlight[] }>('/highlights', { paper_id: paperId.value }),
    api.get<{ items: Note[] }>('/notes', { paper_id: paperId.value }),
  ]);
  annotations.value = a.items ?? [];
  highlights.value = h.items ?? [];
  notes.value = n.items ?? [];
}

/* ------------------------------ 引用文献 ------------------------------ */
/** 参考文献只取一次（进「引用文献」页签、或点了正文里的引用标号时取），失败下次还能再试。 */
let refsLoaded = false;
async function ensureReferences(): Promise<void> {
  if (refsLoaded || !paperId.value) return;
  refsLoaded = true;
  try {
    const data = await api.get<{ items: PaperReference[]; count: number }>(
      `/papers/${paperId.value}/references`);
    paperReferences.value = data.items ?? [];
  } catch (err) {
    refsLoaded = false;
    reportError(err, '取参考文献失败');
  }
}

/**
 * 正文里**点到或悬浮到**引用标号（`[2, 4, 10]`）：就地弹一个只含这几条的浮层，不动侧栏。
 *
 * 用户口径 2026-10-08：原来是"打开侧栏 + 只显示这几条" —— 视线要离开正文，侧栏还会被
 * 强行叫出来（窄屏是抽屉，等于盖住半屏）。只是想"看一眼这条文献是什么"，所以改成贴标号的
 * 简约浮层：格式**复用侧栏/详情弹窗那份卡片**（`ReferenceList`，自带跳转），鼠标移开即消失
 * （见 `scheduleCiteHide`）。第三条口径：**悬浮与点击同一个效果** —— 两条路都进 `openCitePop`，
 * 区别只在时机：点击立刻弹，悬浮要**停住 350ms**（见 `CITE_HOVER_DELAY`）。
 */
/* 浮层的状态与摆位：只放卡片本体（`ReferenceList` —— 与侧栏/详情弹窗同一份，自带跳转）。
   `nums` 是标号里那几个编号，显示时按**参考文献本身**的顺序挑出来（与侧栏同一口径）。 */
const citePopEl = ref<HTMLElement | null>(null);
const citePop = reactive({
  open: false,
  nums: [] as number[],
  loading: false,
  left: 0,
  top: 0,
  width: 400,
  maxHeight: 420,
});

const citeRefItems = computed<PaperReference[]>(() => {
  const want = new Set(citePop.nums);
  return paperReferences.value.filter((r) => want.has(Number(r.order_no)));
});

/** 贴在标号下面；下面放不下就翻到上面，左右夹在视口里。
 *
 *  ⚠️ 高度**必须等渲染完用真实高度算**：早先是"先按最大 420px 预留、往上摆"，于是标号一靠近
 *  页面底部，浮层就被顶到**页面上半部分**去（用户报的"标号到页面下方时，参考文献跑到页面上部分"）——
 *  实际卡片常常只有一条、高几十像素，本该紧贴标号上方。
 */
async function placeCitePop(rect: DOMRect): Promise<void> {
  const w = Math.min(citePop.width, Math.max(240, window.innerWidth - 20));
  citePop.width = w;
  citePop.left = Math.round(Math.min(Math.max(8, rect.left - 10),
    Math.max(8, window.innerWidth - w - 10)));
  const below = window.innerHeight - rect.bottom - 16;
  const above = rect.top - 16;
  const flip = below < 200 && above > below;
  const room = Math.round(Math.max(140, Math.min(420, flip ? above : below)));
  citePop.maxHeight = room;
  citePop.top = Math.round(flip ? Math.max(10, rect.top - room - 8) : rect.bottom + 8);
  await nextTick();                                   // 先渲染一帧，再按真实高度贴上去
  const h = Math.min(citePopEl.value?.offsetHeight || room, room);
  const top = flip ? rect.top - h - 8 : rect.bottom + 8;
  /* 最后再夹一次：锚点万一在视口外（贴着上边缘的旧 rect、或滚过一段），
     也别把浮层画到屏幕外面去 —— 宁可贴边，也不要"点了没东西"。 */
  citePop.top = Math.round(Math.min(Math.max(10, top), Math.max(10, window.innerHeight - h - 10)));
}

/* 移开就收：延时是留给"从标号挪到浮层上"这一步的（与 `scheduleNoteHide` 同一套判法） */
let citeHideTimer: number | undefined;
function cancelCiteHide() { window.clearTimeout(citeHideTimer); }
function scheduleCiteHide() {
  if (!citePop.open) return;
  window.clearTimeout(citeHideTimer);
  citeHideTimer = window.setTimeout(() => {
    const box = citePopEl.value;
    if (box && box.matches(':hover')) return;   // 鼠标已经落在浮层上（从标号挪过去了）
    citePop.open = false;
  }, 220);
}

/* 悬浮**停一下**才弹（用户口径 2026-10-08：先"需要稍长时间悬浮再显示"，随后"高亮部分也调一下"）：
 * 鼠标扫过正文时会一路划过好几个标号 / 高亮，立刻弹就是一路闪 —— 停住 350ms 才算"想看"。
 * 点击不受这个延时约束（点一下是明确动作，见 `onContentClick`）。
 * 两条悬浮路各一个定时器：引用标号 → `scheduleCiteOpen`；高亮（悬停看笔记）→ `scheduleNoteOpen`。 */
const HOVER_OPEN_DELAY = 350;
let citeHoverTimer: number | undefined;
function cancelCiteOpen() {
  window.clearTimeout(citeHoverTimer);
  citeHoverTimer = undefined;
}
function scheduleCiteOpen(nums: number[], el: HTMLElement) {
  cancelCiteHide();                 // 从这一处挪到另一处：上一枚的"延时收起"先撤掉，免得闪一下
  window.clearTimeout(citeHoverTimer);
  citeHoverTimer = window.setTimeout(() => {
    citeHoverTimer = undefined;
    void openCitePop(nums, el);
  }, HOVER_OPEN_DELAY);
}

/* 高亮那一路（悬停 → 笔记浮层）：同一门口径、同一个延时 */
let noteHoverTimer: number | undefined;
function cancelNoteOpen() {
  window.clearTimeout(noteHoverTimer);
  noteHoverTimer = undefined;
}
function scheduleNoteOpen(note: Note, hl: Highlight, mark: HTMLElement) {
  window.clearTimeout(noteHoverTimer);
  noteHoverTimer = window.setTimeout(() => {
    noteHoverTimer = undefined;
    /* 到点再确认一次这处高亮还在：正文行是渲染出来的，期间可能被重画（旧节点已摘掉）——
       那时按旧矩形弹出来的卡片会悬在半空。 */
    if (!mark.isConnected) return;
    openNotePopover({ note, highlight: hl, mode: 'view', rect: mark.getBoundingClientRect() });
  }, HOVER_OPEN_DELAY);
}

async function openCitePop(nums: number[], el: HTMLElement) {
  cancelCiteHide();
  citePop.nums = nums;
  citePop.open = true;
  await placeCitePop(el.getBoundingClientRect());
  if (!refsLoaded) {
    citePop.loading = true;
    try { await ensureReferences(); } finally { citePop.loading = false; }
  }
}

/** 标号按钮上的编号（`data-refs="2,4,10"`）。 */
function citeRefs(el: HTMLElement): number[] {
  return String(el.dataset.refs ?? '').split(',')
    .map((s) => Number(s.trim())).filter((n) => Number.isFinite(n) && n > 0);
}

async function deleteAnnotation(id: string) {
  if (!(await askConfirm({ title: '删除这条批注？', body: '删掉之后不可恢复。',
    danger: true, confirmLabel: '删除' }))) return;
  try {
    await api.del(`/annotations/${id}`);
    await refreshAnnotations();
    toast('已删除', 'ok', 1800);
  } catch (err) { reportError(err, '删除失败'); }
}

async function deleteNote(id: string) {
  if (!(await askConfirm({ title: '删除这条笔记？', body: '删掉之后不可恢复。',
    danger: true, confirmLabel: '删除' }))) return;
  try {
    await api.del(`/notes/${id}`);
    notes.value = notes.value.filter((n) => n.id !== id);
    toast('已删除', 'ok', 1800);
  } catch (err) { reportError(err, '删除失败'); }
}

async function copySelection() {
  /* 用**完整**的选区文字（`selection.text` 是给「AI 解释」截到 600 字的版本）——
   * 否则跨页/跨行选一大段时，菜单「复制」只给出前 600 字（实测 601 字 vs Ctrl+C 2537 字）。
   * 另外公式整段选中时要给 LaTeX（与 Ctrl+C 同一条口径，见 `copyPayload`）。 */
  const sel = window.getSelection();
  const payload = sel && !sel.isCollapsed ? copyPayload(sel) : null;
  await copyText(payload?.text || selection.fullText || selection.text);
  hideMenu();
  toast('已复制', 'ok', 1600);
}

/* ------------------------------ 公式（只读，无复核） ------------------------------ */
/** 公式复核已下线：公式直接从 Markdown 提取，展示/复制/定位，不做人工复核与分级。 */
async function copyFormula(latex: string) {
  await copyText(latex);
  toast('已复制 LaTeX', 'ok', 1600);
}

/* ------------------------------ 术语 / 简报 ------------------------------ */
/** 服务端简报 → 编辑草稿。取消编辑 / 保存后都靠它**重新灌一次**（草稿是本地状态，可能被改脏）。 */
function syncBriefDraft(b: BriefPayload | null = brief.value) {
  briefDraft.domain = b?.domain ?? '';
  briefDraft.style = b?.style ?? '';
  briefDraft.notes = Array.isArray(b?.notes) ? b!.notes!.join('\n') : String(b?.notes ?? '');
  briefDraft.keepAsIsText = Array.isArray(b?.keep_as_is) ? b!.keep_as_is!.join('\n') : '';
  briefDraft.glossaryText = Object.entries(b?.glossary ?? {}).map(([k, v]) => `${k}=${v}`).join('\n');
}

watch(brief, (b) => syncBriefDraft(b), { immediate: true });

async function regenerateBrief() {
  try {
    await withLoading('brief', () => api.post(`/papers/${paperId.value}/brief`, { force: true }));
    const b = await api.get<{ brief: BriefPayload | null }>(`/papers/${paperId.value}/brief`);
    brief.value = b.brief ?? null;
    // 重新生成顺带把「会议 / 期刊 / 年份 / 摘要 / 标签」这些元数据补齐（模型与术语同一次调用产出）
    await refreshPaperMeta();
    toast('简报已重新生成', 'ok');
  } catch (err) { reportError(err, '生成简报失败'); }
}

/** 简报里的元数据由后端写回论文行，这里只刷新本地那一份（顶栏标签标题会用到） */
async function refreshPaperMeta() {
  try {
    const p = await api.get<Paper>(`/papers/${paperId.value}`);
    if (reader.value && p) {
      reader.value.paper = { ...reader.value.paper, ...p };
      setTabTitle(routeKey(route), p.title || paperId.value);
    }
  } catch { /* 元数据刷新失败不影响阅读 */ }
}

async function saveBrief() {
  const glossaryMap: Record<string, string> = {};
  for (const line of briefDraft.glossaryText.split('\n')) {
    const [k, ...rest] = line.split('=');
    if (k && rest.length) glossaryMap[k.trim()] = rest.join('=').trim();
  }
  try {
    const saved = await api.post<BriefPayload>(`/papers/${paperId.value}/brief`, {
      apply: {
        domain: briefDraft.domain,
        style: briefDraft.style,
        notes: briefDraft.notes.split('\n').filter(Boolean),
        keep_as_is: briefDraft.keepAsIsText.split('\n').map(s => s.trim()).filter(Boolean),
        glossary: glossaryMap,
      },
    });
    // 用服务端返回的那份回灌草稿（术语是**合并**语义，本地那份可能少几条）
    brief.value = { ...(brief.value ?? { paper_id: paperId.value }), ...saved, source: 'manual' };
    const gl = await api.get<{ items: GlossaryTerm[] }>(`/papers/${paperId.value}/glossary`);
    glossary.value = gl.items ?? [];
    toast('简报与术语已保存（后续翻译按此术语表）', 'ok');
  } catch (err) {
    reportError(err, '保存简报失败');
    syncBriefDraft();              // 保存失败：把草稿退回服务端那版，别让界面显示没存上的内容
  }
}

/* ------------------------------ 快捷键 ------------------------------ */
useHotkeys({
  '1': () => { void setViewMode('original'); },
  '2': () => { void setViewMode('translation'); },
  '3': () => { void setViewMode('bilingual'); },
  o: () => { void setPairOrder(leftIsOriginal.value ? 'zh_left' : 'en_left'); },
  p: () => { setOriginalMode(originalMode.value === 'page' ? 'text' : 'page'); },
  j: () => { void stepRow(1); },
  k: () => { void stepRow(-1); },
  '[': () => { toggleAside(); },
  // 侧栏页签**不用左右键切换**（用户口径：图标换行、全部可见）。
  '/': () => { void focusSearchBox(); },
  Escape: () => { hideMenu(); mobilePane.value = ''; },
}, () => !loading.value && !fatal.value);

/* ------------------------------ 其它 ------------------------------ */
const openPdf = () => window.open(assetUrl(`/api/v1/papers/${paperId.value}/file`), '_blank');
const togglePane = (name: 'side') => { mobilePane.value = mobilePane.value === name ? '' : name; };

/** 侧栏开合：窄屏=抽屉，宽屏=收起列（选择记住到 localStorage） */
async function toggleAside() {
  // 先钉住当前阅读行：宽度一变，正文整体重排（页图还会在 ~450ms 后补一次重绘），
  // 于是"动画结束、位置看着定了"之后还会再跳一下（用户报的"额外刷新一次"）。
  pinReadingAnchor();
  if (isNarrow.value) {
    togglePane('side');
  } else {
    asideOpen.value = !asideOpen.value;
    localStorage.setItem(asideKey, asideOpen.value ? '1' : '0');
  }
  await nextTick();
  syncPaneWidth();
}

/**
 * 把"当前正在读的那一行"钉在视口里，横跨随后的重排窗口。
 *
 * 为什么需要：侧栏开合会改正文宽度 → 页图/文本全部重排，页图还会在防抖 450ms 后按新宽度
 * **补一次重绘**（清晰度），这会再改一次文档高度。滚动位置 `scrollTop` 全程不变，
 * 但内容在它下面变了，视觉上就是"位置确定后又跳了一下"。
 *
 * 做法：记下最靠近阅读线的那一行此刻的视口位置，随后 ~1.2s 内每 60ms 检查一次，
 * 偏移了就补回 `scrollTop`（纯增量，不动平滑滚动）。**用户一动就停手**：
 * 一旦 `scrollTop` 不等于我们自己设过的值，说明是人在滚，立刻放弃。
 */
let anchorPinTimer: number | null = null;

function pinReadingAnchor(durationMs = 1200) {
  const el = scroller.value;
  if (!el) return;
  const rows = Array.from(el.querySelectorAll<HTMLElement>('[data-row-id]'));
  if (!rows.length) return;

  const line = el.getBoundingClientRect().top + readingLine();
  const anchor = rows.find((r) => r.getBoundingClientRect().bottom >= line) ?? rows[0];
  const rowId = anchor.dataset.rowId ?? '';
  const wantTop = anchor.getBoundingClientRect().top;
  const startedAt = performance.now();
  let expectedTop = el.scrollTop;

  if (anchorPinTimer) window.clearTimeout(anchorPinTimer);
  const tick = () => {
    anchorPinTimer = null;
    if (Math.abs(el.scrollTop - expectedTop) > 2) return;      // 人在滚：让位
    const cur = rowId ? el.querySelector<HTMLElement>(`[data-row-id="${CSS.escape(rowId)}"]`) : null;
    if (cur) {
      const delta = cur.getBoundingClientRect().top - wantTop;
      if (Math.abs(delta) > 1) {
        el.scrollTop += delta;
        expectedTop = el.scrollTop;
      }
    }
    if (performance.now() - startedAt < durationMs) anchorPinTimer = window.setTimeout(tick, 60);
  };
  anchorPinTimer = window.setTimeout(tick, 60);
}
/** 划词卡片里的 Markdown 渲染（带本论文的资源路径改写；卡片里没有高亮，故传空数组）。 */
function renderMarkdown(text?: string | null) {
  return htmlWithHighlights(text ?? '', [], paperId.value);
}

</script>

<template>
  <div>
    <div v-if="loading" class="reader-loading" role="status" aria-live="polite">
      <svg class="spinner" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
        <circle cx="12" cy="12" r="9" />
      </svg>
      <span>正在加载阅读器数据…</span>
    </div>

    <div v-else-if="fatal" class="card">
      <div class="card-body">
        <h3>无法打开这篇论文</h3>
        <p class="muted small">{{ fatal.message }}</p>
        <RouterLink class="btn" :to="{ name: 'library' }">
          <AppIcon name="back" :size="14" />返回论文库
        </RouterLink>
      </div>
    </div>

    <!-- 正在解析：与"正在加载阅读器数据"同一套居中占位（转圈 + 一行字 + 进度条），
         不再用卡片堆三行文案（用户口径：冗余表述去掉，页面整体居中） -->
    <div v-else-if="isEmpty && parseTask" class="reader-loading" role="status" aria-live="polite">
      <svg class="spinner" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
        <circle cx="12" cy="12" r="9" />
      </svg>
      <span>
        正在解析
        <span v-if="parseTask.message" class="faint">· {{ parseStageLabel }}</span>
        <span class="mono"> {{ parseTask.progress ?? 0 }}%</span>
      </span>
      <div class="reader-loading-bar"><ProgressBar :value="parseTask.progress ?? 0" /></div>
    </div>

    <div v-else-if="isEmpty" class="card">
      <div class="card-body">
        <h3>这篇论文还没有解析结果</h3>
        <p class="muted small">阅读器依赖解析产出（页图 + 页级翻译）。点一下会先解析，解析完自动接着翻译。</p>
        <div class="row">
          <button class="btn primary" @click="triggerParse">
            <AppIcon name="bolt" :size="14" />解析并翻译
          </button>
          <RouterLink class="btn" :to="{ name: 'library' }">返回论文库</RouterLink>
        </div>
      </div>
    </div>

    <div
      v-else
      :class="['reader',
               asideVisible ? '' : 'no-aside',
               isNarrow && mobilePane === 'side' ? 'mobile-aside' : '',
               { resizing: asideResizing }]"
    >
      <!-- ============ 工具栏 ============ -->
      <!-- ============ 悬浮胶囊工具栏（Apple 风） ============
           同一条水平线上三块玻璃，共用 `--cap-h` → 永远同高同线：
             · **左** = 侧栏开关（**圆形**，位置固定 12px：侧栏开着时它落在侧栏头部那一行的最左，
               收起时正好落在正文左边缘 —— 两种情况都"在正确的地方"，所以**永远只有这一枚**，
               不再有"侧栏里一枚、顶栏一枚"的交接闪烁）
             · **中** = 主胶囊：视图切换 + 版面选项 + 显示（字号 / 页图缩放）
             · **右** = 更多（与左侧开关同尺寸、同基线）
           自检脚本依赖的钩子：`[data-aside-toggle]`、`.reader-toolbar .seg button`、
           缩放标签的 `.tiny.faint.nowrap`、以及按标题找的「缩小 / 放大」。 -->
      <div
      class="reader-toolbar"
      :style="{
        '--panel-shift': capsuleShiftCss,
        '--panel-dx': capOffset.x + 'px',
        '--panel-dy': capOffset.y + 'px',
        '--panel-flip': panelFlip + 'px',
        '--panel-nudge': panelNudge + 'px',
      }"
    >
        <div class="cap-solo left">
          <AsideToggle :open="asideVisible" @toggle="toggleAside" />
        </div>

        <div
          ref="capsuleEl"
          class="reader-capsule"
          :class="{ dragging: capDrag }"
          :style="capsuleStyle"
          @pointerdown="onCapsuleDown"
        >
          <!-- 拖动条：**最左侧**一枚抓手（用户口径）。拖它（或胶囊空白处）改位置，
               位置存 sessionStorage：刷新还在、关掉页面复位。 -->
          <span class="cap-grip" title="拖动调整位置" data-cap-grip aria-hidden="true">
            <i /><i /><i />
          </span>
          <!-- 批注模式：工具**并进这一枚胶囊**（用户口径：合并原有的页面切换胶囊），
               原文/译文/对照等收进右边的面板里 -->
          <template v-if="inkOn">
            <div class="cap-group">
              <button
                v-for="item in INK_TOOLS"
                :key="item.key"
                class="btn icon"
                :class="{ 'tool-on': inkTool === item.key }"
                :title="item.label"
                :aria-pressed="inkTool === item.key"
                :data-ink-tool="item.key"
                :aria-expanded="inkOptionsOpen && (item.key === 'pen' || item.key === 'marker')"
                @click="pickInkTool(item.key)"
              >
                <AppIcon :name="item.icon" :size="15" />
              </button>
            </div>
            <div class="cap-group" v-if="canUndo(inkKey) || canRedo(inkKey)">
              <button class="btn icon" :disabled="!canUndo(inkKey)" title="撤销（Ctrl+Z）"
                      data-ink-undo @click="undoInk(inkKey)">
                <AppIcon name="undo" :size="15" />
              </button>
              <button class="btn icon" :disabled="!canRedo(inkKey)" title="重做（Ctrl+Shift+Z）"
                      data-ink-redo @click="redoInk(inkKey)">
                <AppIcon name="redo" :size="15" />
              </button>
            </div>
            <div class="cap-group">
              <button
                class="btn icon"
                data-cap-panel="settings"
                title="设置：视图 / 正文字号 / 页图缩放 / 版面"
                aria-haspopup="true"
                :aria-expanded="capPanel === 'settings'"
                @click="toggleCapPanel('settings')"
              >
                <AppIcon name="sliders" :size="15" />
              </button>
              <button class="btn icon" title="退出批注（笔迹会保留）" data-ink-exit @click="closeInk">
                <AppIcon name="close" :size="15" />
              </button>
            </div>

          </template>

          <div v-if="!inkOn" class="cap-group">
            <div class="seg">
              <button :class="{ active: viewMode === 'original' }" @click="setViewMode('original')">原文</button>
              <button :class="{ active: viewMode === 'translation' }" @click="setViewMode('translation')">译文</button>
              <button :class="{ active: viewMode === 'bilingual' }" @click="setViewMode('bilingual')">对照</button>
            </div>
          </div>

          <div v-if="!inkOn" class="cap-group">
            <!-- 手写批注：只开关"能不能落笔"，笔迹本身按页存在服务端 -->
            <button
              class="btn icon"
              data-cap-ink
              :class="{ active: inkOn }"
              :title="inkOn ? '退出批注模式' : '批注：在正文上手写 / 荧光笔 / 框注'"
              :aria-pressed="inkOn"
              @click="toggleInk"
            >
              <AppIcon name="pen" :size="15" />
            </button>
            <!-- 设置：**版面 + 显示合成一个面板**（用户口径）。以前是两枚按钮，
                 在「译文」视图下版面面板几乎是空的、Aa 也只剩一个字母，看着像空按钮。 -->
            <button
              class="btn icon"
              data-cap-panel="settings"
              title="设置：视图 / 正文字号 / 页图缩放 / 版面"
              aria-haspopup="true"
              :aria-expanded="capPanel === 'settings'"
              @click="toggleCapPanel('settings')"
            >
              <AppIcon name="sliders" :size="15" />
            </button>
          </div>

          <!-- 翻译状态与主操作：翻译完成时**整组消失**（用户口径：不要那个常驻的绿色对勾） -->
          <div class="cap-group" v-if="!inkOn && !translateDone">
            <!-- SSE 连得好不好是**实现细节**，连上时不刷存在感；降级轮询 / 连接中才提醒 -->
            <span
              v-if="sseStatus !== 'open'"
              class="badge warn nowrap"
              :title="sseStatus === 'polling' ? 'SSE 不可用，已降级轮询（4s 一次）' : 'SSE 连接中'"
            >{{ sseStatus === 'polling' ? '轮询' : '连接中' }}</span>
            <span class="badge accent nowrap">{{ stats.pages_done ?? 0 }}/{{ stats.pages ?? 0 }} 页</span>
            <span v-if="activeTask" class="badge nowrap" :title="`任务 ${activeTask.id}：${activeTask.status}`">
              翻译中
            </span>
            <button
              class="btn primary"
              :disabled="!!activeTask || busyTranslating"
              :title="activeTask ? '已有翻译任务在跑（可先「停止」或等它结束）' : '从断点继续翻译剩余的页'"
              @click="startTranslate('resume')"
            >
              <AppIcon name="play" :size="15" />{{ activeTask || busyTranslating ? '翻译中…' : '继续翻译' }}
            </button>
            <button v-if="activeTask" class="btn danger" @click="cancelTranslate(activeTask)">
              <AppIcon name="stop" :size="15" />停止
            </button>
          </div>
        </div>

        <!-- 画笔的二级菜单（颜色 / 粗细）：挂在胶囊**外面** —— 胶囊自己 overflow:auto，
             放在里面的绝对定位会被裁掉（实测：DOM 有、屏幕上看不见）。 -->
        <div
          v-if="inkOptionsOpen && (inkTool === 'pen' || inkTool === 'marker')"
          class="ink-options"
          data-ink-options
          :style="inkOptionsStyle"
          @pointerdown.stop
        >
          <!-- 颜色：色块图标 + 一枚取色器（用户口径：精简默认色数、支持浏览器选色） -->
          <div class="ink-options-row" data-ink-colors>
            <button
              v-for="c in INK_COLORS"
              :key="c"
              class="ink-palette-dot"
              :class="{ active: activeInkColor() === c }"
              :style="{ background: c }"
              :title="c"
              @click="setInkColor(c)"
            />
            <label class="ink-color-custom" title="自定义颜色（浏览器取色器）">
              <span class="ink-color-custom-dot" />
              <input
                type="color"
                :value="activeInkColor()"
                data-ink-color-input
                @input="setInkColor(($event.target as HTMLInputElement).value)"
              >
            </label>
          </div>
          <!-- 粗细：三档图标（不写字，省地方；标题里仍给"细/中/粗"） -->
          <div class="ink-options-row" data-ink-widths>
            <button
              v-for="step in WIDTH_STEPS"
              :key="step.key"
              class="ink-width-icon"
              :class="{ active: inkWidth === step.key }"
              :data-ink-width="step.key"
              :title="'粗细：' + step.label"
              @click="inkWidth = step.key"
            >
              <i class="ink-dot" :style="inkDotStyle(step.key)" />
            </button>
          </div>
        </div>

        <!-- 橡皮的二级菜单：滑到底 = 清空本页笔迹 -->
        <div
          v-if="eraserClearOpen && inkTool === 'eraser'"
          class="ink-options ink-clear"
          data-ink-options
          data-ink-clear
          :style="inkOptionsStyle"
          @pointerdown.stop
        >
          <div class="ink-options-row">
            <span class="ink-clear-label">清空本页笔迹</span>
            <!-- Material You 3 风格的滑块（原生 range 的轨道/手柄跟这枚玻璃胶囊不是一套设计） -->
            <M3Slider
              class="ink-clear-range"
              :model-value="clearProgress"
              :disabled="!inkKeyTarget"
              tone="danger"
              data-ink-clear-range
              @update:model-value="onClearSlide(String($event))"
            />
          </div>
        </div>
        <!-- 右：更多（低频动作：重译 / 打开 PDF / Markdown 视图） -->
        <div class="cap-solo right">
          <Menu :items="toolbarMenu" label="更多操作" size="md" @select="onToolbarAction" />
        </div>

        <!-- 弹出面板（互斥，落在主胶囊正下方）。里面每个控件都是**常挂载**的：
             `frontend_check.py` 按文字/标题点「原文文本 / 原 PDF / 小」，v-if 会让它点不到。 -->
        <CapPanel :open="capPanel === 'settings'" label="设置" data-cap-panel-body="settings">
          <!-- 只在**批注模式**下显示：那时胶囊里没有视图段控件，面板补上这一行；
               常规模式下胶囊里已经有 原文/译文/对照，这里再放一份就是重复（用户口径 2026-10-17）。 -->
          <div v-if="inkOn" class="cap-row">
            <span class="cap-row-label">视图</span>
            <div class="seg" title="视图">
              <button :class="{ active: viewMode === 'original' }" @click="setViewMode('original')">原文</button>
              <button :class="{ active: viewMode === 'translation' }" @click="setViewMode('translation')">译文</button>
              <button :class="{ active: viewMode === 'bilingual' }" @click="setViewMode('bilingual')">对照</button>
            </div>
          </div>

          <div class="cap-row">
            <span class="cap-row-label">正文字号</span>
            <div class="seg" title="正文字号">
              <button
                v-for="opt in TEXT_SIZE_OPTIONS"
                :key="opt.k"
                :class="{ active: textSize === opt.k }"
                @click="setTextSize(opt.k)"
              >{{ opt.label }}</button>
            </div>
          </div>

          <!-- 页图缩放：只在"原文栏 = 原 PDF"时有意义（文字模式没有页图可缩） -->
          <div v-if="originalIsPage" class="cap-row">
            <span class="cap-row-label">页图缩放</span>
            <div class="cap-zoom">
              <button class="btn icon" title="缩小" @click="zoomBy(-1)">
                <AppIcon name="zoom-out" :size="15" />
              </button>
              <span class="cap-zoom-value tnum">{{ Math.round(zoom * 100) }}%</span>
              <button class="btn icon" title="放大" @click="zoomBy(1)">
                <AppIcon name="zoom-in" :size="15" />
              </button>
              <!-- 恢复 100% = 适应栏宽（复刻目标站的 Reset zoom：**只在偏离 100% 时出现**） -->
              <button v-if="Math.abs(zoom - 1) > 0.001" class="btn icon" title="恢复 100%（适应栏宽）"
                      data-zoom-reset @click="setZoom(1)">
                <AppIcon name="refresh" :size="15" />
              </button>
            </div>
          </div>
          <div v-if="originalVisible" class="cap-row">
            <span class="cap-row-label">原文栏呈现</span>
            <div class="seg" title="原文栏呈现">
              <button :class="{ active: originalMode === 'text' }" @click="setOriginalMode('text')">
                <AppIcon name="document" :size="14" />原文文本
              </button>
              <button :class="{ active: originalMode === 'page' }" @click="setOriginalMode('page')">
                <AppIcon name="image" :size="14" />原 PDF
              </button>
            </div>
          </div>

          <div v-if="pageLeft" class="cap-row">
            <span class="cap-row-label">文字列侧</span>
            <div class="seg" title="页图旁的文字列">
              <button :class="{ active: pageSide === 'zh' }" @click="setPageSide('zh')">译文</button>
              <button :class="{ active: pageSide === 'en' }" @click="setPageSide('en')">原文</button>
            </div>
          </div>

          <div v-if="viewMode === 'bilingual'" class="cap-row">
            <span class="cap-row-label">左右顺序</span>
            <div class="seg" title="左右顺序">
              <button
                :class="{ active: leftIsOriginal }"
                title="原文在左（译文在右）"
                @click="setPairOrder('en_left')"
              >
                <AppIcon name="layout-original-left" :size="15" />
              </button>
              <button
                :class="{ active: !leftIsOriginal }"
                title="译文在左（原文在右）"
                @click="setPairOrder('zh_left')"
              >
                <AppIcon name="layout-original-right" :size="15" />
              </button>
            </div>
          </div>
        </CapPanel>

      </div>

      <!-- ============ 左栏：目录 + 批注/笔记/公式/插图/术语/任务/论文解读（可整体折叠，可拖宽） ============
           `--reader-font-size` 也设在这一栏上：左栏里的「论文解读」用的是阅读器的正文字号
           （`InsightDoc` 的排版全部以 em 相对它缩放），读者把正文调大，解读也跟着大。 -->
      <aside :class="['reader-aside', { resizing: asideResizing }]" :style="{ '--reader-font-size': `${readerFont}px` }">
        <!-- 侧栏头部：**只有页签胶囊**。那枚圆形开关是顶栏里固定的一枚（12px 处），
             侧栏开着时它正好落在这一行的最左 —— 所以这里是"胶囊正左侧就是关闭按钮"。
             `padding-left: 62px` 给它让位（12 + 38 + 12）。 -->
        <div class="aside-head">
            <div class="side-tabs-wrap">
            <button
              v-if="tabsScroll.left"
              class="side-scroll-btn left"
              title="向左滚动页签"
              @click="scrollTabs(-1)"
            >
              <AppIcon name="chevron-left" :size="13" />
            </button>
            <div ref="sideTabsEl" class="side-tabs" @scroll="syncTabsScroll" @wheel="onSideTabsWheel">
              <button
                v-for="t in SIDE_TABS"
                :key="t.k"
                class="side-tab"
                :class="{ active: sideTab === t.k }"
                :title="t.title"
                @click="sideTab = t.k"
              >
                <AppIcon :name="t.icon" :size="14" />
                <span class="side-label">{{ t.label }}</span>
                <span v-if="t.count" class="side-count">{{ t.count }}</span>
              </button>
            </div>
            <button
              v-if="tabsScroll.right"
              class="side-scroll-btn right"
              title="向右滚动页签"
              @click="scrollTabs(1)"
            >
              <AppIcon name="chevron-right" :size="13" />
            </button>
          </div>
        </div>
        <!-- 目录页签里再分「目录 / 页面视图」（《修改计划》第二步）：两者**共享同一个"读到哪"**
             —— `currentPage` 都从正文当前行来，页面视图据此高亮并把缩略图滚进可视区。 -->
        <div v-if="sideTab === 'toc'" class="toc-pane">
          <!-- 一行搞定：左边是「目录 / 页面视图」切换（**只有图标**，不再占一整行文字），
               右边是目录的折叠/展开。以前切换胶囊单独占一行、目录自己还有一行「目录 N」，
               两行文字压在一起很挤（用户口径：去掉多余的文字、把胶囊挪进这一行）。 -->
          <div class="toc-bar">
            <!-- 顺序（用户口径 2026-10-07）：胶囊在最左，**全部收起 / 全部展开在胶囊右侧**；
                 胶囊本身用全局 `.seg`（与对话页、公式页同一枚托盘、同一档高度）。 -->
            <div class="seg sq" role="tablist" aria-label="目录 / 页面视图">
              <button
                type="button" role="tab" data-toc-mode="outline"
                :aria-selected="tocMode === 'outline'"
                :class="{ active: tocMode === 'outline' }"
                title="目录"
                @click="tocMode = 'outline'"
              >
                <AppIcon name="list" :size="14" />
              </button>
              <button
                type="button" role="tab" data-toc-mode="pages"
                :aria-selected="tocMode === 'pages'"
                :class="{ active: tocMode === 'pages' }"
                title="页面视图"
                @click="tocMode = 'pages'"
              >
                <AppIcon name="grid" :size="14" />
              </button>
            </div>
            <template v-if="tocMode === 'outline'">
              <button class="btn icon sm" title="全部收起" data-toc-unfold="less" @click="collapseAll">
                <AppIcon name="unfold-less" :size="14" />
              </button>
              <button class="btn icon sm" title="全部展开" data-toc-unfold="more" @click="expandAll">
                <AppIcon name="unfold-more" :size="14" />
              </button>
            </template>
            <span class="spacer"></span>
          </div>

          <ReaderToc
            v-if="tocMode === 'outline'"
            headless
            :rows="tocRows"
            :current-row-id="tocActiveId"
            :page-count="pages.length"
            :collapsed="collapsed"
            @focus="(id: string, page?: number) => focusRow(id, { page, flash: true })"
            @toggle="toggleCollapse"
            @collapse-all="collapseAll"
            @expand-all="expandAll"
          />
          <ReaderPages
            v-else
            :paper-id="paperId"
            :page-count="pages.length"
            :current-page="currentPage"
            @focus="gotoPage"
          />
        </div>
        <!-- 对话面板用 `KeepAlive` 包住：页签在这条 `v-if` 链上，切走就**卸载** ——
             回来时整块重挂，会话列表要重新请求一次，视觉上就是"点「对话」要等一下才出历史"
             （用户报的延迟）。缓存住实例之后，切回来是**原样还在**（连正在流式的回答都不断），
             再在后台悄悄刷一次列表（见 `ReaderChat.onActivated`）。 -->
        <KeepAlive>
          <ReaderChat
            v-if="sideTab === 'chat'"
            ref="chatRef"
            :paper-id="paperId"
            :selection="chatSelection"
            @focus="(id: string, page?: number) => focusRow(id, { page, flash: true })"
            @clear-selection="chatSelection = null"
          />
        </KeepAlive>
        <ReaderSearch
          v-if="sideTab === 'search'"
          ref="searchPane"
          v-model:query="searchQuery"
          v-model:scope="searchScope"
          v-model:mode="searchMode"
          :paper-id="paperId"
          :hits="searchHits"
          @search="runSearch"
          @clear="clearSearch"
          @focus="(id: string, page?: number) => focusRow(id, { page, flash: true })"
          @goto-page="gotoPage"
        />
        <ReaderSidePanel
          v-if="sideTab !== 'toc' && sideTab !== 'chat' && sideTab !== 'search'"
          :tab="sideTab"
          :annotations="annotations"
          :notes="notes"
          :highlights="highlights"
          :references="paperReferences"
          :formulas="formulas"
          :formulas-loading="formulasLoading"
          :figures="figures"
          :glossary="glossary"
          :brief-draft="briefDraft"
          :brief-source="brief?.source ?? ''"
          :tasks="tasks"
          :paper-id="paperId"
          :insight-running="understandingRunning"
          @focus="(id: string, page?: number) => focusRow(id, { page, flash: true })"
          @copy="copyFormula"
          @zoom="zoomImage = $event"
          @save-brief="saveBrief"
          @regenerate-brief="regenerateBrief"
          @reload-brief="syncBriefDraft()"
          @delete-annotation="deleteAnnotation"
          @delete-note="deleteNote"
          @save-note="savePaneNote"
          @delete-highlight="deleteHighlightIds"
          @cancel-task="cancelTranslate"
          @insight-queued="fetchTasks"
          @open-insight-page="openInsightPage"
        />
        <!-- 拖拽改宽度：贴在侧栏右边缘（见 onResizeDown 的说明），双击复位 -->
        <div
          class="aside-resizer"
          :class="{ active: asideResizing }"
          title="拖动调整侧栏宽度（双击复位）"
          @pointerdown="onResizeDown"
          @pointermove="onResizeMove"
          @pointerup="onResizeUp"
          @pointercancel="onResizeUp"
          @lostpointercapture="onResizeUp"
          @dblclick="resetAsideWidth"
        />
      </aside>

      <!-- ============ 中：正文（单一滚动容器 → 天然对齐） ============
           `.reader-slide` 是"让位 + 平移"的那一层：侧栏开合时它整体滑过去，
           内部的正文一次都不重排（见 asideGapPx 的说明）。 -->
      <section ref="scroller" class="reader-content" @scroll="onScroll" @click="onContentClick"
               @contextmenu="onContentContextMenu"
               @mouseover="onContentHover" @mouseout="onContentHoverOut">
        <div class="reader-slide">
        <ReaderPageRows
          :groups="groups"
          :mode="viewMode"
          :original-mode="originalMode"
          :page-side="pageSide"
          :pair-order="pairOrder"
          :paper-id="paperId"
          :page-width="pageWidth"
          :page-zoom="zoom"
          :ink-on="inkOn"
          :current-row-id="currentRowId"
          :highlights="highlights"
          :search-term="searchQuery.trim()"
          :flash-row-id="flashRowId"
          :flash-nonce="flashNonce"
          :translating="translating"
          :font-size="readerFont"
          @focus="focusRow"
          @zoom="zoomImage = $event"
          @pdf-failed="onPdfFailed"
          @translate-page="(p: number) => startTranslate('resume', [p])"
        />
        </div>
      </section>
    </div>

    <SelectionMenu
      :visible="selection.show"
      :x="pillPos.x"
      :y="pillPos.y"
      :colors="HIGHLIGHT_COLORS"
      :anchored="selection.anchored"
      :allow-note="selection.allowNote"
      :hl-count="selection.hlIds.length"
      :has-note="!!selectionNote"
      @explain="explainSelection"
      @note="onNoteAction"
      @copy="copySelection"
      @highlight="addHighlight"
      @unhighlight="deleteSelectionHighlights"
      @close="closeSelectionMenu"
    />

    <!-- AI 解释浮层（第五步）：与笔记**同一个壳**、**默认锚定**（读完再决定要不要存下来，
         中途移开鼠标就关掉太容易丢）。没有查看/编辑切换（用户口径：AI 解释不需要）。
         深度思考与工具调用**默认折叠**，只展示最终回答。 -->
    <FloatingCard
      :open="explainCard.open"
      :anchor="explainCard.anchor"
      :anchored="explainCard.pinned"
      title="Cyli 解释（只依据论文内容）"
      :width="420"
      :max-height="520"
      data-explain-card
      @close="explainCard.open = false"
      @toggle-anchor="explainCard.pinned = !explainCard.pinned"
    >
      <div class="ex-thread">
        <!-- 解释的是插图时：顶上给张缩略图（一眼知道在解释哪张图；真正给模型看的是附件） -->
        <div v-if="explainCard.imageUrl" class="ex-fig">
          <img :src="explainCard.imageUrl" :alt="explainCard.imageCaption || '插图'" data-ex-fig>
          <span v-if="explainCard.imageCaption" class="tiny faint">{{ explainCard.imageCaption }}</span>
        </div>
        <div v-for="(t, i) in explainCard.turns" :key="i" class="ex-turn" :class="t.role">
          <div v-if="t.role === 'user'" class="ex-q">{{ t.text }}</div>
          <template v-else>
            <!-- 过程（思考 / 工具）：默认折叠，只露一行摘要，点开才铺开 -->
            <div v-if="t.steps && t.steps.length" class="ex-proc">
              <button type="button" class="ex-proc-head" data-ex-proc
                      :aria-expanded="explainCard.processOpen"
                      @click="explainCard.processOpen = !explainCard.processOpen">
                <AppIcon :name="explainCard.processOpen ? 'chevron-down' : 'chevron-right'" :size="12" />
                <span>{{ stepSummary(t.steps) }}</span>
              </button>
              <div v-if="explainCard.processOpen" class="ex-proc-body">
                <div v-for="(s, k) in t.steps" :key="k" class="ex-step" :class="`is-${s.kind}`">
                  <AppIcon :name="stepIcon(s)" :size="12" />
                  <span class="ex-step-tag">{{ stepLabel(s) }}</span>
                  <span class="ex-step-text">{{ s.text || s.digest || s.tool || '' }}</span>
                  <span v-if="s.ms" class="tiny faint">{{ Math.round(Number(s.ms) / 100) / 10 }}s</span>
                </div>
              </div>
            </div>
            <div v-if="answerBody(t)" class="md small" data-ex-answer
                 v-html="renderExplainAnswer(t)" @click="onExplainCite" />
          </template>
        </div>
        <!-- 还在出过程、正文没到：只留这一行（有轨迹可看时连它都不必） -->
        <div v-if="explainBusy && !explainSteps" class="tiny faint">正在读这篇论文…</div>
      </div>

      <!-- 底部输入框：**出过程期间不显示**（用户口径第 3 条：还没有本轮正文时藏起来） -->
      <div v-if="!explainBusy" class="ex-ask">
        <input
          v-model="explainCard.question"
          :placeholder="explainIsImage ? '就这张图继续问（Enter 发送）' : '就这段继续问（Enter 发送）'"
          @keyup.enter="submitExplainQuestion"
        >
        <button class="btn sm primary" :disabled="explainCard.loading || !explainCard.question.trim()"
                @click="submitExplainQuestion">
          提问
        </button>
      </div>

      <template #footer>
        <!-- 插图解释：没有"这段文字"可标，存为笔记那套收起来 -->
        <template v-if="!explainBusy && !explainIsImage">
          <span class="tiny faint ex-save-hint">标出这段并存为笔记</span>
          <span class="ex-swatches">
            <span
              v-for="c in HIGHLIGHT_COLORS"
              :key="c"
              class="hl-swatch"
              :class="[c, { on: explainColor === c }]"
              :title="`标成${c === 'yellow' ? '黄' : c === 'green' ? '绿' : c === 'blue' ? '蓝' : '粉'}色，并把这段解释存成它的笔记`"
              :data-ex-color="c"
              @click="saveExplanationWithColor(c)"
            />
          </span>
          <span class="spacer"></span>
          <button class="btn sm ghost" title="把这个选区带进侧栏对话里继续聊" data-ex-to-chat
                  @click="openSelectionInChat(false)">
            <AppIcon name="chat" :size="12" />在对话里继续
          </button>
        </template>
      </template>
    </FloatingCard>

    <!-- 笔记浮层（第五步）：锚在高亮/选区旁边的小卡片，默认"移开就消失"。
         悬停浮层本身要取消关闭（鼠标从高亮挪进来的时候不能把它关掉）。 -->
    <FloatingCard
      :open="notePop.open"
      :anchor="notePop.anchor"
      :anchored="notePop.anchored"
      title="笔记"
      :width="noteCardWidth"
      data-note-popover
      @close="closeNotePopover"
      @toggle-anchor="notePop.anchored = !notePop.anchored"
      @mouseleave="scheduleNoteHide"
    >
      <!-- 「笔记」和左边那枚图标必须在**最左**（用户口径 2026-10-10 复查）；
           色条跟它并排、排它**右边** —— 所以色点走 `head-mid`（标题之后、按钮之前）。 -->
      <template #head-lead>
        <AppIcon name="note" :size="14" />
      </template>
      <template #head-mid>
        <div v-if="notePop.mode !== 'view' && notePop.highlightId" class="np-colors">
          <button
            v-for="c in HIGHLIGHT_COLORS"
            :key="c"
            type="button"
            class="hl-swatch"
            :class="[c, { on: notePop.color === c }]"
            :title="`换成 ${c} 色`"
            data-note-color
            @click="setNoteColor(c)"
          />
          <!-- 自定义色（用户口径 2026-10-10："少了自定义色彩"）—— 与胶囊上那枚同一做法 -->
          <label class="hl-swatch custom" title="自定义颜色">
            <input type="color" value="#ffd700" data-note-custom aria-label="自定义高亮颜色"
                   @input="setNoteColor(($event.target as HTMLInputElement).value)">
          </label>
        </div>
      </template>
      <template #head-extra>
        <!-- 相对时间只在**查看态**显示：编辑态标题行里已经挤了色点行，再塞一条时间就会把标题压没 -->
        <span v-if="notePop.mode === 'view' && noteTime" class="np-time">{{ noteTime }}</span>
        <!-- 头部按键照参考实现（铅笔 = 编辑 / 垃圾桶 = 删除，关关闭是壳自带的 ✕）：
             查看态给铅笔，编辑态把它换成 ✓ 保存。 -->
        <button v-if="notePop.mode === 'view' && notePop.noteId" class="np-head-save" type="button"
                title="编辑笔记" data-note-edit
                @click="notePop.mode = 'edit'; notePop.draft = notePop.content">
          <AppIcon name="edit" :size="13" />
        </button>
        <button v-if="notePop.mode !== 'view'" class="np-head-save" type="button"
                :disabled="notePop.busy" title="保存笔记" data-note-save @click="saveNotePopover">
          <AppIcon name="check" :size="13" />
        </button>
        <button v-if="notePop.noteId" class="np-head-save danger" type="button"
                :disabled="notePop.busy" title="删除笔记" data-note-delete @click="deleteNotePopover">
          <AppIcon name="trash" :size="13" />
        </button>
      </template>

      <!-- ⚠️ **不显示高亮的那段原文**（用户口径 2026-10-08："浮层不要显示论文正文部分"）：
           浮层贴着那处高亮，原文就在眼皮底下，再抄一遍只会把卡片撑长、还多一层"这是哪一段"的噪音。
           要看按的是哪一段，看浮层**贴着哪儿**就够了。 -->
      <!-- 提示文案已删（用户口径 2026-10-10："去掉多余文字说明"）：**卡片本身就是输入框** ——
           空着就是"写一条"，有内容就是"编辑"；标题里已经有"已高亮"，正文里再解释一遍是噪音。 -->
      <textarea
        v-if="notePop.mode !== 'view'"
        v-model="notePop.draft"
        class="np-edit"
        :rows="noteEditRows"
        data-note-edit
      />
      <!-- 查看态正文：**用户自己写的笔记**走 `{ breaks: true }`（用户口径 2026-10-10）：
           不能再跑 `normalizeSoftBreaks` —— 它会把作者敲的回车接回去（"第一行第二行"），
           那是给 PDF 抽取文本用的。 -->
      <div v-else-if="notePop.mode === 'view'" class="np-view md small" data-note-view
           v-html="mdToHtml(notePop.content, null, { breaks: true })" />
      <!-- 底部**不再传 footer 插槽**（用户口径 2026-10-10："不要有选择的文字框"）：
           那个 footer 就算只有一行按钮，也会带出一条 1px 上线，看着像正文外面套了一圈框。
           编辑 / 保存 / 删除 / 关闭 全走头部按键（铅笔 / ✓ / 垃圾桶 / ✕）。 -->
    </FloatingCard>

    <!-- 引用标号的浮层（用户口径 2026-10-08 第二条）：点正文里的 `[2, 4, 10]` 就在标号下面
         弹这么一小块，只放那几条参考文献的卡片本体（与侧栏/详情弹窗同一份 `ReferenceList`，
         所以**点条目/图标照样跳发表网站**）；鼠标移开即消失（`scheduleCiteHide`）。
         ⚠️ **必须传送**到 `<body>`：Markdown 视图本身是个 `Modal`（遮罩 `z-index: 150`），
         留在阅读器主区（那里最高才 60）会被整个压在弹窗底下 —— 实测在 markdown 视图里
         浮层"看得见、点不着"（`elementFromPoint` 交回的是弹窗里的段落）。 -->
    <Teleport to="body">
      <div
        v-if="citePop.open"
        ref="citePopEl"
        class="cite-pop"
        data-cite-pop
        :style="{
          left: `${citePop.left}px`, top: `${citePop.top}px`,
          width: `${citePop.width}px`, maxHeight: `${citePop.maxHeight}px`,
        }"
        @mouseenter="cancelCiteHide"
        @mouseleave="scheduleCiteHide"
      >
        <div v-if="citePop.loading" class="small faint cite-pop-hint">正在取参考文献…</div>
        <div v-else-if="!citeRefItems.length" class="small faint cite-pop-hint">
          这几条在参考文献里没有对应编号（解析出的编号可能对不上）
        </div>
        <ReferenceList v-else :items="citeRefItems" />
      </div>
    </Teleport>

    <!-- 毛玻璃右键菜单（三处界面各一套清单，见 `onContentContextMenu`） -->
    <ContextMenu
      :open="ctxMenu.open"
      :x="ctxMenu.x"
      :y="ctxMenu.y"
      :items="ctxMenu.items"
      @select="onCtxSelect"
      @color="onCtxColor"
      @placed="ctxMenu.rect = $event"
      @close="ctxMenu.open = false; ctxMenu.rect = null"
    />

    <!-- 大图查看器：毛玻璃胶囊（放大 / 百分比 / 缩小 / 重置 / 关闭）。手写批注在阅读器页面上。 -->
    <ImageViewer v-if="zoomImage" :target="zoomImage" @close="zoomImage = null" />

    <Modal
      :open="mdView.open"
      title="Markdown"
      wide
      :full="mdView.full"
      @close="closeMarkdownView"
    >
      <!-- 标题栏右上角：「全屏」用 CSS 铺满视口（不用 Fullscreen API，不弹权限、Esc 逻辑已有） -->
      <template #tools>
        <button
          class="btn icon sm"
          :title="mdView.full ? '退出全屏' : '全屏'"
          :aria-pressed="mdView.full"
          @click="mdView.full = !mdView.full"
        >
          <AppIcon :name="mdView.full ? 'collapse' : 'expand'" :size="14" />
        </button>
      </template>
      <div class="md-bar">
        <div class="seg">
          <button
            v-for="m in MD_MODE_TABS"
            :key="m.k"
            :class="{ active: mdView.mode === m.k }"
            @click="mdView.mode = m.k"
          >{{ m.label }}</button>
        </div>
        <span class="spacer" />
        <!-- 不再翻页：整篇一次性铺开，右上角只留「回到当前阅读页」这一个定位动作 -->
        <button class="btn sm" title="回到当前阅读的那一页" @click="scrollMdToPage(currentPage || 1)">
          <AppIcon name="locate" :size="13" />当前页
        </button>
      </div>
      <div v-if="mdView.loading" class="muted">正在加载 Markdown…</div>
      <div v-else-if="!mdView.pages.length" class="muted">这篇论文还没有翻译产物。</div>
      <div
        v-else
        ref="mdDocEl"
        class="md-doc md"
        :class="{ full: mdView.full }"
        v-html="mdFullHtml"
        @click="onMdClick"
        @contextmenu="onContentContextMenu"
        @mouseover="onMdHover"
        @mouseout="onMdHoverOut"
      />
      <template #footer>
        <button class="btn" @click="closeMarkdownView">关闭</button>
        <!-- 下载版本 = 左上角选中的那个（中英对照 / 纯原文 / 纯译文）：下载什么就是你在看什么。
             内容由后端拼（与「下载打包」共用一份实现），**不含页标识**（没有 `第 N 页`）。 -->
        <button
          class="btn primary"
          :disabled="!mdView.pages.length || !!busyDownload"
          @click="downloadMarkdown"
        >
          <AppIcon name="download" :size="14" />下载{{ mdModeMeta.label }} Markdown
        </button>
      </template>
    </Modal>
  </div>
</template>

<style scoped>
/* 阅读器数据加载占位：整屏布局下正文区还是空的，把圈 + 文案**居中**摆（用 `.empty` 会贴在左上角）。
   高度对齐 `.reader` 网格，加载完切成正文时不会跳一下。 */
.reader-loading {
  display: flex; flex-direction: column; align-items: center; justify-content: center;
  gap: 10px;
  /* ⚠️ `dvh` 写在**后面**当覆盖：手机上 `100vh` 是"地址栏收起时"的高度，而阅读器是
     整屏不滚动的布局、地址栏根本不会自动收起 —— 底部那一截是实打实读不到的。
     老浏览器不认识 `dvh`，会忽略第二行、继续用第一行的 `100vh`。 */
  height: calc(100vh - var(--header-h));
  height: calc(100dvh - var(--header-h));
  color: var(--text-dim); font-size: 13px;
}
/* 解析进度条：固定一个不太宽的宽度，居中在转圈下方（拉满整屏会很怪） */
.reader-loading-bar { width: 220px; max-width: 60vw; }

.reader {
  /* 侧栏改为**绝对定位**（见 .reader-aside）：网格只剩正文一列，「侧栏开合」因此不再牵动
     网格轨道。每次开合只改一个 CSS 变量，正文的重排只发生在**动画第一帧**，
     之后是纯 transform 的平移（合成层、不重排）。以前过渡 grid 宽度/侧栏宽度会逐帧重排
     整篇正文（KaTeX、表格、行对齐全在重算），那正是「卡一下、跳一下」的来源。 */
  position: relative;
  display: grid;
  grid-template-columns: minmax(0, 1fr);
  grid-template-rows: minmax(0, 1fr);
  height: calc(100vh - var(--header-h));
  height: calc(100dvh - var(--header-h));   /* 手机地址栏兜底，见 .reader-loading */
  overflow: hidden;

  /* ---------------- 胶囊直径 + 玻璃材质（声明在**根**上，顶栏与侧栏共用一套） ----------------
   * 同一行的四样东西共用高度变量 --cap-h → 永远同高同线：顶栏那三块玻璃
   * （左开关 / 主胶囊 / 右更多）+ 侧栏头部那枚页签胶囊。
   * ⚠️ 变量必须落在 .reader 上，不能只写在 .reader-toolbar 里：侧栏是它的**兄弟**节点，
   * 自定义属性不跨兄弟继承 —— 写窄了侧栏那枚胶囊就拿不到 --cap-h，高度直接失效。 */
  --cap-h: 38px;
  /* 玻璃本体的不透明度：**浅色下可以很透**（背后是白纸，48px 模糊会化成一团柔光），
     深色下要加实一档 —— 否则深色工具条压在仍是白纸的正文上会变成「灰玻璃」，控件读不清。
     两档都远透于「实心工具栏」，所以极强玻璃的观感不变，只是深色下不再牺牲可读性。 */
  --glass-bar-top: 80%;
  --glass-bar-bottom: 68%;
  --glass-panel-top: 86%;
  --glass-panel-bottom: 74%;
}

/* ---------------- 悬浮胶囊工具栏（Apple 风） ----------------
 * 一条水平线上三块玻璃（左开关 / 中主胶囊 / 右更多），共用 .reader 上的 --cap-h
 * → 永远同高、同基线（用户报过「开关和胶囊不一样大、也不在一条线上」）。
 * 整层是 height: 0 的占位层 + pointer-events: none：正文从它**下面**滚过去，
 * 毛玻璃才有东西可模糊；交互只落在三块玻璃与弹出面板上。 */
.reader-toolbar {
  position: absolute; top: 0; left: 0; right: 0; height: 0; z-index: 40;
  display: flex; justify-content: center;
  /* ⚠️ 必须 flex-start：这一层高 0，默认的 stretch 会把胶囊也拉成 0 高，
     而胶囊自带 overflow: hidden → 里面的文字被裁掉一半（只露出字头）。 */
  align-items: flex-start;
  pointer-events: none;
}
/* 深色皮肤（含「跟随系统」落到深色的那一档）：玻璃加实一档。
 * ⚠️ :global() 必须**把整条选择器包进去**：写成 :global(html.theme-dark) .reader
 * 时 Vue 的 scoped 编译器会把后半段 .reader 吞掉，只剩 html.theme-dark { … } ——
 * 那样变量落在根元素上，而 .reader 自己又声明了同名变量（层叠上「自己 > 继承」），
 * 深色档等于没写（实测计算值仍是 80%）。 */
:global(html.theme-dark .reader) {
  --glass-bar-top: 90%;
  --glass-bar-bottom: 82%;
  --glass-panel-top: 93%;
  --glass-panel-bottom: 86%;
}
@media (prefers-color-scheme: dark) {
  :global(html[data-theme="auto"] .reader),
  :global(html:not([data-theme]) .reader) {
    --glass-bar-top: 90%;
    --glass-bar-bottom: 82%;
    --glass-panel-top: 93%;
    --glass-panel-bottom: 86%;
  }
}

/* 顶部横带那层"渐变毛玻璃"按用户口径**已移除**（不好看）：现在正文滚到顶栏下面就是直接
   被胶囊盖住，不再额外加一层渐隐模糊。留个注释免得后人又把它加回来。 */

/* 左右两枚独立胶囊（侧栏开关 / 更多）：与主胶囊同高同线，玻璃同一套材质。
 * 侧栏开关是**正圆**（用户口径），位置固定 12px —— 侧栏开着时它落在侧栏头部那行最左
 * （就是"页签胶囊正左侧的关闭按钮"），收起时正好落在正文左边缘。一枚到底，不交接。 */
.cap-solo {
  position: absolute; top: 10px; height: var(--cap-h); width: var(--cap-h);
  pointer-events: auto;
  display: flex; align-items: center; justify-content: center;
  border-radius: 999px;
  /* 玻璃 = **渐变本体 + 柔光投影**，不画描边、不留内阴影（那道顶部高光在深色下会变成发光边）：
     描边在浅色下几乎看不见、在深色下却成了一圈发光环（用户口径："暗色显示太奇怪"），
     索性去掉 —— 悬浮感交给阴影，表面感交给上亮下暗的本体。 */
  border: 0;
  background: linear-gradient(180deg,
    color-mix(in srgb, var(--bg-elev) var(--glass-bar-top, 80%), transparent),
    color-mix(in srgb, var(--bg-elev) var(--glass-bar-bottom, 68%), transparent));
  backdrop-filter: blur(48px) saturate(2.6);
  -webkit-backdrop-filter: blur(48px) saturate(2.6);
  box-shadow: var(--shadow-flyout);
  transition: transform .26s cubic-bezier(.32, .72, 0, 1);
}
.cap-solo.left { left: 12px; }
/* 右：与左侧那枚**同为正圆**（用户口径：更多也要圆形） */
.cap-solo.right { right: 12px; }
/* 右胶囊里的「⋯」：Menu 自带 32px 的圆触发钮（base.css 的 `.btn` 有底色）——
 * ⚠️ 它是**子组件内部**的元素，父组件的 scoped 选择器够不到，必须 `:deep()`。
 * 不穿透的话就会看到"胶囊里还套着一个小圆钮"（用户报的"底部多了一个胶囊样式"）。 */
.reader-toolbar .cap-solo.right :deep(.menu-root) { width: 100%; height: 100%; }
.reader-toolbar .cap-solo.right :deep(.menu-trigger) {
  width: 100%; height: 100%;
  border-color: transparent; background: transparent; box-shadow: none;
}
.reader-toolbar .cap-solo.right :deep(.menu-trigger:hover) { background: var(--bg-hover); }

.reader-capsule {
  pointer-events: auto;
  position: relative;
  cursor: grab;
  display: flex; align-items: center; gap: 4px;
  margin-top: 10px; padding: 0 6px;
  height: var(--cap-h);
  /* 两侧各给一枚独立胶囊留位：三块玻璃不许叠在一起 */
  max-width: calc(100% - 148px);
  border-radius: 999px;
  /* 同 .cap-solo：渐变本体 + 柔光投影，不画描边、不留内阴影 */
  border: 0;
  background: linear-gradient(180deg,
    color-mix(in srgb, var(--bg-elev) var(--glass-bar-top, 80%), transparent),
    color-mix(in srgb, var(--bg-elev) var(--glass-bar-bottom, 68%), transparent));
  backdrop-filter: blur(48px) saturate(2.6);
  -webkit-backdrop-filter: blur(48px) saturate(2.6);
  box-shadow: var(--shadow-flyout);
  /* 居中在**正文列**上：侧栏开合时整体滑半个栏宽（时长/缓动与侧栏、正文完全一致） */
  transition: transform .26s cubic-bezier(.32, .72, 0, 1);
  /* 窄屏宁可让胶囊横向滑动，也不把按钮压成"两字一行" */
  overflow-x: auto; overflow-y: hidden; scrollbar-width: none;
}
.reader-capsule::-webkit-scrollbar { display: none; }
/* 拖动中：不要选中文字、不要"按住发亮"的过渡 */
.reader-capsule.dragging { cursor: grabbing; }
.reader-capsule.dragging * { pointer-events: none; }

/* ---------------- 批注：色板 / 色块 / 粗细圆点 ----------------
 * 色板从胶囊**正下方**展开（与弹出面板同一套玻璃），点一下即换色、随即收起。 */
.ink-palette {
  position: absolute; top: calc(100% + 8px); left: 50%; transform: translateX(-50%);
  display: flex; align-items: center; gap: 7px;
  padding: 8px 10px; border-radius: 999px; z-index: 3;
  background: linear-gradient(180deg,
    color-mix(in srgb, var(--bg-elev) var(--glass-panel-top, 86%), transparent),
    color-mix(in srgb, var(--bg-elev) var(--glass-panel-bottom, 74%), transparent));
  backdrop-filter: blur(48px) saturate(2.6);
  -webkit-backdrop-filter: blur(48px) saturate(2.6);
  box-shadow: var(--shadow-flyout);
}
.ink-palette-dot {
  width: 20px; height: 20px; padding: 0; cursor: pointer;
  border: 2px solid transparent; border-radius: 999px;
  box-shadow: 0 0 0 1px color-mix(in srgb, var(--text) 14%, transparent);
  transition: transform var(--dur) var(--ease);
}
.ink-palette-dot:hover { transform: scale(1.12); }
.ink-palette-dot.active { border-color: var(--bg-elev); box-shadow: 0 0 0 2px var(--text); }
.ink-dot { display: block; border-radius: 999px; background: currentColor; }

/* 抓手：**最左侧**一小条三点，**竖排**（用户口径：拖动条要竖着的）。
   只有它是"拖"，别处按下去仍是点按钮。透明加宽的伪元素把可点面积撑到 18px，
   视觉上仍然只有半透明的三个小点。 */
.cap-grip {
  position: relative; flex: none;
  display: inline-flex; flex-direction: column; align-items: center; justify-content: center;
  gap: 2px; width: 12px; height: 22px;
  margin-right: 2px; cursor: grab; touch-action: none;
}
.cap-grip::before {
  content: ''; position: absolute; inset: -6px -3px; border-radius: 6px;
}
.cap-grip i { display: block; width: 2px; height: 2px; border-radius: 999px; background: var(--text-faint); }
.cap-grip:hover i { background: var(--text-dim); }
.reader-capsule.dragging .cap-grip { cursor: grabbing; }

/* 画笔二级菜单：胶囊正下方一条**横排**（颜色 / 粗细），与弹出面板同一套玻璃 */
.ink-options {
  /* fixed：位置由 `inkOptionsStyle` 按胶囊实时算（见 placeInkOptions） */
  position: fixed; transform: translateX(-50%);
  display: flex; flex-direction: column; gap: 8px; z-index: 45; pointer-events: auto;
  padding: 9px 12px; border-radius: 16px;
  background: linear-gradient(180deg,
    color-mix(in srgb, var(--bg-elev) var(--glass-panel-top, 86%), transparent),
    color-mix(in srgb, var(--bg-elev) var(--glass-panel-bottom, 74%), transparent));
  backdrop-filter: blur(48px) saturate(2.6);
  -webkit-backdrop-filter: blur(48px) saturate(2.6);
  box-shadow: var(--shadow-flyout);
}
.ink-options-row { display: flex; align-items: center; gap: 7px; }
.ink-options-label { width: 28px; font-size: 11.5px; color: var(--text-faint); }
/* 粗细：三档**图标**（只画点，不写字；选中态给一圈浅底） */
.ink-width-icon {
  display: inline-flex; align-items: center; justify-content: center;
  width: 26px; height: 26px; padding: 0; cursor: pointer;
  border: 0; border-radius: 999px; background: transparent; color: var(--text-dim);
}
.ink-width-icon:hover { background: var(--bg-hover); color: var(--text); }
.ink-width-icon.active { background: var(--accent-soft, var(--bg-hover)); color: var(--accent); }

/* 自定义颜色：一枚"彩虹点"，点它弹浏览器原生取色器（input 藏起来但可点） */
.ink-color-custom {
  position: relative; display: inline-flex; align-items: center; justify-content: center;
  width: 20px; height: 20px; cursor: pointer;
}
.ink-color-custom-dot {
  display: block; width: 20px; height: 20px; border-radius: 999px;
  background: conic-gradient(#e8384f, #f0a11a, #2aa775, #2f6fed, #8b5cf6, #e8384f);
  box-shadow: 0 0 0 1px color-mix(in srgb, var(--text) 14%, transparent);
}
.ink-color-custom input {
  position: absolute; inset: 0; width: 100%; height: 100%;
  opacity: 0; cursor: pointer; border: 0; padding: 0;
}

/* 清空本页笔迹：一条滑块（滑到右端触发，一次撤销可整个回来） */
.ink-clear { min-width: 248px; }
.ink-clear-label { font-size: 12px; color: var(--text-faint); white-space: nowrap; }
/* 滑块本体是 M3Slider（自带轨道/手柄/状态层），这里只管它在行里占宽 */
.ink-clear-range { flex: 1; min-width: 130px; }
.cap-group { display: flex; align-items: center; gap: 3px; flex: none; }
/* 分组靠**线**，不靠给每个按钮画边框：胶囊里一排描边按钮会像一张表单 */
.cap-group + .cap-group { margin-left: 1px; padding-left: 7px; border-left: 1px solid var(--divider); }
/* 胶囊里的控件一律"无边框、**无阴影**、hover 才浮一层浅底"
   （权重写到三档，压过 base.css 的 .btn/.seg）。
   ⚠️ `box-shadow: none` 是必须的：base.css 的 `.btn` 现在用 `--shadow-1` 表达"一枚独立的
   白色 pill"，而胶囊里的按钮**不是独立的一枚** —— 它们躺在胶囊内部，再套一层投影就成了
   "胶囊里还嵌着一个个小胶囊"（用户口径 2026-10-03：字号调节与显示这两个按钮不要阴影，
   保持"在胶囊内部"的感觉）。 */
.reader-capsule .cap-group .btn { border-color: transparent; background: transparent; box-shadow: none; }
.reader-capsule .cap-group .btn:hover:not(:disabled) { background: var(--bg-hover); border-color: transparent; }
.reader-capsule .cap-group .btn.primary { background: var(--accent); color: var(--text-on-accent); }
.reader-capsule .cap-group .btn.primary:hover:not(:disabled) { background: var(--accent-hover); }
.reader-capsule .cap-group .btn.danger { color: var(--danger); border-color: transparent; }
.reader-capsule .cap-group .btn.danger:hover:not(:disabled) { background: var(--danger-soft); }
/* 弹出面板开着的那枚：给一点"我在这里"的浅底（同样不投影） */
.reader-capsule .cap-group .btn[aria-expanded="true"] { background: var(--bg-hover); }
/* 批注工具是**单选**：始终有一枚是选中的（用户口径 2026-10-17）。
   选中态用"抬起的一枚 pill"（--bg-elev + 一档投影），与 .seg 的选中项同一门语言 ——
   以前只有 hover 的浅灰，四枚按钮看起来永远是"都没选"。 */
.reader-capsule .cap-group .btn.tool-on {
  /* ⚠️ 两层原因：
     ① 必须盖过 `.btn:hover`（优先级更高，而且鼠标通常正停在被选中的那枚上）；
     ② 用**主色淡底**而不是白底 —— 胶囊本身就是浅色玻璃，白 pill 在白玻璃上等于没选中。 */
  background: color-mix(in srgb, var(--accent) 18%, transparent) !important;
  box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--accent) 44%, transparent) !important;
  color: var(--accent-text) !important;
}
.reader-capsule .cap-group .seg {
  border-color: transparent;
  background: color-mix(in srgb, var(--text) 5%, transparent);
}
.reader-capsule .cap-group .seg button.active { background: var(--bg-elev); box-shadow: var(--shadow-1); }
/* 胶囊里紧凑一档：32px 的控件缩到 28px，整条胶囊能短 ~10%，仍然是可点尺寸 */
.reader-capsule .cap-group .btn.icon { width: 28px; height: 28px; }
.reader-capsule .cap-group .seg { height: 28px; }
.reader-capsule .cap-group .seg button { height: 22px; padding: 0 10px; font-size: 12px; }
.reader-capsule .cap-group .btn:not(.icon) { height: 28px; font-size: 12px; }
.reader-capsule .cap-group .badge { height: 20px; }
/* 「Aa」：字号 + 页图缩放合成一个入口（用户口径：缩放和字号合并），
   按钮上顺手带出当前缩放值 —— 也是自检脚本读的那个 `.tiny.faint.nowrap`。 */
.reader-capsule .cap-group .cap-aa { gap: 5px; padding: 0 9px; font-size: 13px; font-weight: 600; }
.reader-capsule .cap-group .cap-aa .tiny { font-size: 10.5px; font-weight: 500; }

/* 弹出面板里的一行：左标签 + 右分段/步进 */
.cap-row { display: flex; align-items: center; gap: 12px; }
.cap-row-label { flex: none; width: 68px; font-size: 12px; color: var(--text-dim); }
.cap-zoom { display: flex; align-items: center; gap: 6px; }
.cap-zoom-value { min-width: 46px; text-align: center; font-size: 13px; }

/* 侧栏头部：**只有一条页签胶囊**，左端留出那枚圆形开关的位置（12 + 38 + 12 = 62）。
   高度 58 是为了让胶囊中线（29px）与顶栏那三块玻璃的中线对齐 —— 四样东西在一条线上。 */
.aside-head {
  flex: none; display: flex; align-items: center;
  height: 58px; padding: 0 12px 0 62px;
}

/* Markdown 视图：正文宽一点、插图按容器宽度自适应 */
.md-bar { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; }
/* 顶部留一点空白（与论文正文顶部留白同一种观感）：内容不再顶着上一行的分隔/工具栏。
   注意这里**没有上边框**了 —— 用户口径：移除分割线。 */
.md-doc {
  max-height: 68vh; overflow: auto; padding: 16px 2px 4px;
  font-size: 14px; line-height: 1.75;
}
/* 全屏时内容区跟着长高（标题栏 ~48 + 工具行 ~40 + 底栏 ~56 + 内边距 ≈ 190） */
.md-doc.full {
  max-height: calc(100vh - 190px);
  max-height: calc(100dvh - 190px);   /* 手机地址栏兜底，见 .reader-loading */
}
/* 每一页那一段：页码是**淡标注**（不再是翻页器，也没有分割线），页与页之间靠间距分开 */
.md-page { padding-bottom: 26px; }
.md-page + .md-page { border-top: 0; }
.md-page-no {
  font-size: 11px; font-weight: 600; color: var(--text-faint);
  font-variant-numeric: tabular-nums; margin: 0 0 6px;
}
/* 一页里的原文 / 译文两面：只在上下对照时同时出现，中间留白隔开（不画分割线）。
   两面的划词高亮配色由 base.css 的 `mark.hl.<color>` 统一提供。 */
.md-part + .md-part { margin-top: 22px; }
/* 正文插图的尺寸/白边/圆角**统一由 base.css 的 `.md img` 管**（Markdown 视图、知识库、AI 解读同一套：
 * 560 × 420 以内，点开看大图）。这里不再另写一套 —— 以前这里 `max-height: min(62vh, 780px)`，
 * 宽图照样横跨整幅正文，用户口径是"图片在 markdown 中不要那么大"。
 * 只补一个"可点"的光标：Markdown 视图里点插图会开大图（见 `onMdClick`），别处（知识库）没有这个交互。 */
.md-doc :deep(img) { cursor: zoom-in; }
/* `---` 不再画线，只当一段空白（用户口径：移除分割线）；正文里模型自己写的 `---` 同理。 */
.md-doc :deep(hr) { border: 0; height: 18px; margin: 0; background: none; }
.md-doc :deep(h2) { font-size: 16px; }
/* 各栏都要 min-width/min-height: 0，否则 grid 子项不收缩、内容会顶破布局 */
.reader-aside, .reader-content { min-width: 0; min-height: 0; }
.reader-aside {
  /* 拖拽把手要相对侧栏定位（见 .aside-resizer）；**绝对定位**使它脱离网格：
     开合只改 transform（合成层），正文那一列不会被逐帧重排。 */
  position: absolute; top: 0; bottom: 0; left: 0; z-index: 30;
  border-right: 1px solid var(--border); background: var(--bg-elev);
  overflow: hidden; display: flex; flex-direction: column;
  width: v-bind(asideWidthPx);          /* 展开宽度（可拖拽，按论文记） */
  transition: transform .26s cubic-bezier(.32, .72, 0, 1), opacity .18s ease;
}
/* 拖拽期间关掉过渡：0.26s 的补间会让把手"黏"在指针后面（松手才追上） */
.reader-aside.resizing, .reader.resizing .reader-slide,
.reader.resizing .reader-capsule, .reader.resizing .cap-solo { transition: none; }
/* 拖拽把手：贴在侧栏**右边缘内侧**（`.reader-aside` 是 overflow:hidden，凸出去就被裁了）。
   平时完全透明（不打扰阅读），hover / 拖拽中才亮一条主题色竖线。
   ⚠️ 选择器必须写到 `.reader-aside > .aside-resizer` 这个权重：上面 `@media (min-width:1101px)`
   里的 `.reader-aside > * { min-width: … }` 会给**所有**子元素兜一个 300px 起的最小宽度，
   权重相同、它又写在后面 —— 不压住的话这条 9px 的把手会被撑成 300px，把侧栏半个面板盖住并吃掉点击。 */
.reader-aside > .aside-resizer {
  position: absolute; top: 0; right: 0; width: 9px; min-width: 0; height: 100%;
  /* z-index 压到页签条之下（见 .side-tabs-wrap）：把手只负责"内容区那一竖条"，
     页签条右端的滚动箭头不能被它抢走点击 */
  z-index: 1; cursor: col-resize; touch-action: none; background: transparent;
}
.reader-aside > .aside-resizer::after {
  content: ''; position: absolute; top: 0; right: 0; width: 2px; height: 100%;
  background: transparent; transition: background .15s ease;
}
.reader-aside > .aside-resizer:hover::after,
.reader-aside > .aside-resizer.active::after { background: var(--accent); }
@media (min-width: 1101px) {
  /* 展开/收起期间侧栏**只被平移、不跟着变窄**：内容保持展开宽度。
     不这么做的话，选项卡会重排、公式（KaTeX）会逐帧重新排版 —— 既看着"变形"，
     又是卡顿的主要来源。裁切由 .reader-aside 的 overflow:hidden 完成。
     宽度与上面的 `width` 同一个变量：拖宽之后内容才真的按新宽度排版。 */
  .reader-aside > * { min-width: v-bind(asideWidthPx); }
}
/* 收起：整块滑出左边界。transform 走合成层，比过渡 width 便宜得多，
   也不会像"宽度收到 0"那样在右边缘留一条来不及收掉的 1px 分隔线。 */
.reader.no-aside .reader-aside {
  transform: translateX(-100%); opacity: 0; pointer-events: none;
}
/* 侧栏页签：**图标 + 文字**，**一行到底、不换行**；一行放不下就左右滚动（用户口径）。
 * 滚动条隐藏（保持干净），鼠标滚轮 / 触控板 / Shift+滚轮都能滚，见 `onSideTabsWheel`；
 * 切页签时把当前那个滚进可视区（`scrollIntoView`），左右两端还有圆形箭头按钮（见下）。
 * 排得比早先紧凑一些（64px → 56px、字号 10.5 → 10px），少一点"藏起来的页签"。 */
/* `z-index: 3` 只是为了让页签条压在拖拽把手之上：把手是一条贯穿整栏高度的 9px 热区，
 * 不压住的话页签条最右端那一点点会变成"拖宽度"而不是"点页签"（见 .aside-resizer 的说明）。 */
/* 侧栏头部那条**页签胶囊**（用户口径：与顶栏那几块玻璃同一门语言 —— 悬浮、不画描边）。
 * 容器是一枚玻璃胶囊，**高度 = --cap-h（38px）：与左侧那枚圆形开关同高同线**；
 * 里面每一项默认**只留图标**，只有当前页那枚显示文字（图标在上、文字在下，加粗）。
 * 一行放不下就横向滚（滚轮 / 两端圆箭头，见 onSideTabsWheel），
 * 文字从两枚圆按钮**下面**穿过去 —— 按钮是半透明玻璃，滚过去时底下的字仍然看得见。 */
.side-tabs-wrap { position: relative; flex: 1 1 auto; min-width: 0; z-index: 3; }

/* 「目录 / 页面视图」切换（《修改计划》第二步）：与侧栏页签同一门语言（无描边的托盘 + 抬起的
   当前项）。**只有图标**（用户口径：胶囊不保留文字），并与目录的折叠/展开同处一行 ——
   以前它单独占一整行，加上目录自己那行「目录 N」，两行文字压在一起很挤。 */
/* 直接挂在侧栏下的页签（目录 / 对话 / 搜索）：侧栏是 `overflow: hidden` 的纵向 flex，
   它们得**自己**撑满剩余高度。目录这一页原来把滚动交给 `ReaderToc` 内部的
   `.toc { height: 100%; overflow-y: auto }`，而它的父元素（`.toc-pane`）**没有高度** ——
   百分比高度落不了地，于是目录长成内容那么高、被侧栏直接裁掉：**整页滚不动**
   （用户报的"整个目录页彻底无法触摸滚动，插图页正常"；插图页在 `ReaderSidePanel` 里，
   那边有 `.panel-body { overflow-y: auto; flex: 1; min-height: 0 }`，所以它是好的）。
   现在由 `.toc-pane` 自己当**唯一**的滚动容器（两种模式一个样），那一行切换工具条 sticky 在顶部。 */
.toc-pane {
  display: flex; flex-direction: column; min-width: 0;
  flex: 1; min-height: 0;
  overflow-y: auto; overflow-x: hidden; overscroll-behavior: contain;
}
/* 目录页顶部那一行：**与对话页的工具栏同一套位置**（用户口径 2026-10-07 —— 以对话页为主：
   `.list-head` 是 `padding: 8px 8px 6px; gap: 6px`）。以前这里是 `padding: 2px 2px 6px; gap: 4px`，
   胶囊贴着栏边、离页签条也太近，跟对话页一比就"不是一个位置"。
   胶囊本身不再自己造（旧的 `.toc-mode` 已删）：直接用全局 `.seg` + `.seg.sq`（图标版）。 */
.toc-bar {
  position: sticky; top: 0; z-index: 2;
  display: flex; align-items: center; gap: 6px; flex: none;
  padding: 8px 8px 6px;
  background: var(--bg-elev);
  border-bottom: 1px solid var(--divider);
}
.toc-bar .seg { flex: none; }
.side-tabs {
  display: flex; align-items: center; gap: 2px;   /* 图标之间更紧凑（用户口径） */
  height: var(--cap-h); padding: 3px;
  /* 与 base.css 的 `.seg`、顶栏标签条**同一门语言、同一组颜色**：三层、**无描边** ——
     ① 托盘（--bg-sunken）② 当前那枚抬起的白 pill ③ 文字/图标
     （2026-10-03 统一：以前这里用 color-mix 自调的灰，与 `.seg` 的 --bg-sunken 不是一个色）。 */
  border: 0;
  border-radius: 999px;
  background: var(--bg-sunken);
  overflow-x: auto; overflow-y: hidden;
  scrollbar-width: none;
  /* 两端各留一枚圆按钮的位置：scrollIntoView 落位时不会把当前页签藏在按钮底下 */
  scroll-padding: 0 30px;
}
.side-tabs::-webkit-scrollbar { display: none; }
.side-tab {
  position: relative; border: 0; background: transparent; color: var(--text-dim);
  cursor: pointer; padding: 0 8px; height: 32px; border-radius: 999px;
  /* **两行**：图标在上、文字在下（用户口径），横向空位因此全让给文字 */
  display: inline-flex; flex-direction: column; align-items: center; justify-content: center;
  gap: 1px; flex: none; white-space: nowrap;
  transition: background var(--dur) var(--ease), color var(--dur) var(--ease);
}
.side-tab:hover { background: var(--bg-hover); color: var(--text); }
/* 汉字**只在当前页显示**（用户口径：其余页签移除汉字、只留图标，整条胶囊因此紧凑一截）；
   当前页那枚**横向放宽 + 文字缩小一号** —— 图标在上、文字在下时，窄胶囊会把内容挤成一小团，
   加宽并收小字号才像个"选中态"。 */
.side-tab.active {
  padding: 0 14px;
  background: var(--bg-elev); color: var(--accent-text); box-shadow: var(--shadow-1);
}
.side-label { display: none; font-size: 10.5px; line-height: 1; }
.side-tab.active .side-label { display: block; font-weight: 600; }
/* 角标挪到**右上角**（绝对定位）：页签是「图标 +（只在当前页才有的）文字」，
   横向没有空位可以内联挂一个数字了。 */
.side-count {
  position: absolute; top: 0; right: 3px;
  font-size: 9px; line-height: 1;
  color: var(--text-faint); font-variant-numeric: tabular-nums;
}
/* 两枚「翻页」圆按钮：**浮在胶囊两端**、不占布局（有内容才出现，所以不会让页签跳动）。
   与其余玻璃件同一配方、不画描边 —— 半透明 + 模糊，页签滚到它底下时文字照样透出来
   （用户口径：「文字可以在按钮下面显示」）。 */
.side-scroll-btn {
  position: absolute; top: 50%; transform: translateY(-50%); z-index: 2;
  width: 24px; height: 24px; padding: 0; border: 0; border-radius: 50%;
  display: inline-flex; align-items: center; justify-content: center;
  color: var(--text-dim); cursor: pointer;
  background: linear-gradient(180deg,
    color-mix(in srgb, var(--text) 10%, transparent),
    color-mix(in srgb, var(--text) 6%, transparent));
  backdrop-filter: blur(48px) saturate(2.6);
  -webkit-backdrop-filter: blur(48px) saturate(2.6);
  box-shadow: var(--shadow-flyout);
}
.side-scroll-btn:hover { color: var(--accent-text); }
.side-scroll-btn.left { left: 4px; }
.side-scroll-btn.right { right: 4px; }
/* 正文区是**唯一**的滚动容器。
 * - `padding-top` 只留 8px：胶囊是悬浮的，正文**从它下面开始**（用户口径：不要顶部那一大截空白）。
 *   页码标注在内容列左端、胶囊居中，两者不重叠，所以第一页的页码照样看得见；
 * - `overflow-x: hidden`：横向滚动交给"页图那一列"（放大后列内横滚），
 *   这样侧栏开合时 `.reader-slide` 的位移不会在容器上闪出一条横向滚动条。 */
.reader-content {
  overflow-y: auto; overflow-x: hidden;
  background: var(--bg); scrollbar-width: thin; scrollbar-gutter: stable;
  padding-top: 8px;
}
/* 让位 + 平移的那一层。
 * 宽度 = `calc(100% - 让位量)`：侧栏开合时它**一次性**变成新宽度（正文重排一次），
 * 而 transform 是过渡的 —— 于是整块正文平滑地滑过去，滑的过程中不重排。 */
.reader-slide {
  width: calc(100% - v-bind(asideGapPx));
  transform: translateX(v-bind(asideGapPx));
  transition: transform .26s cubic-bezier(.32, .72, 0, 1);
}

@media (max-width: 1100px) {
  .reader, .reader.no-aside { grid-template-columns: minmax(0, 1fr); }
  /* 窄屏侧栏是**抽屉**：一直在 DOM 里，靠 transform 滑入滑出（display 切换没法做动画）。
     宽度由这里定死（不跟拖拽值走），所以窄屏不会因为用户拖过的宽度而横向溢出。 */
  .reader-aside {
    position: fixed; top: var(--header-h); bottom: 0; z-index: 60;
    left: 0; width: min(360px, 88vw); box-shadow: var(--shadow-2);
    transform: translateX(-102%); visibility: hidden;
    transition: transform .24s cubic-bezier(.4, 0, .2, 1), visibility .24s;
  }
  .reader.mobile-aside .reader-aside { transform: none; visibility: visible; }
  /* 抽屉宽度是定死的，拖拽没有意义（拖了也不会生效），干脆不显示把手 */
  .reader-aside > .aside-resizer { display: none; }

  /* ⚠️ 抽屉的 z-index(60) 高于工具栏(40)，一滑出来就把工具栏**整层**盖住 —— 而那枚
     "关抽屉"的开关正是工具栏左端的那一枚（`.cap-solo.left`），于是抽屉打开后就再也
     关不掉了（用户报的「左上角的收起侧栏按钮消失」）。侧栏头部那条 `padding-left: 62px`
     本来就是给这枚钮让位的，说明设计意图就是"开关浮在侧栏之上"。
     把工具栏抬到抽屉之上即可复位；顺带收掉主胶囊与右侧「⋯」——抽屉打开时它们要么
     被盖住，要么和抽屉头部那条页签胶囊糊在一起，露出来只会打架。 */
  .reader.mobile-aside .reader-toolbar { z-index: 70; }
  .reader.mobile-aside .reader-capsule,
  .reader.mobile-aside .cap-solo.right { visibility: hidden; }
}

/* 手机（≤640px）：抽屉**占满整屏**。360px 的抽屉放在 390px 的屏上只剩一条 47px 的
   正文缝 —— 目录看不全，右边那条缝还会误触正文。全屏之后抽屉头部那条页签胶囊也宽松了
   （8 个页签不再需要在窄条里横向滚）。 */
@media (max-width: 640px) {
  .reader-aside { width: 100%; }
}

@media (prefers-reduced-motion: reduce) {
  .reader, .reader-aside { transition: none; }
  .cite-pop { animation: none; }
}

/* ---------------- 划词解释卡片（浮动层里的小对话线程） ----------------
 * 由问答 agent 产出（流式），所以这里既要有"正在读"的状态，也要能承接追问。 */
/* 笔记浮层的 `prompt` 态（刚标完高亮）：原文下面一行提示，再下面是「添加笔记」 */
.np-hint { margin: 4px 0 2px; }
/* 引用标号的浮层：**毛玻璃**的一张纸（用户口径 2026-10-08："优化样式，比如毛玻璃"）。
 * 底下的正文透出来一点、卡片仍然读得清 —— 浮在正文上才有"浮"的感觉。
 * ⚠️ 阴影必须用 `--shadow-flyout`：`--shadow-3` 这个名字**从来没定义过**，写上去整条声明会被丢掉，
 * 等于一层阴影都没有（`FloatingCard` 也踩了同一个坑，已一并修）。 */
.cite-pop {
  position: fixed; z-index: 160; overflow: auto;
  padding: 6px; border-radius: var(--radius-sm);
  border: 1px solid color-mix(in srgb, var(--border-strong) 70%, transparent);
  background: color-mix(in srgb, var(--bg-elev) 72%, transparent);
  backdrop-filter: blur(18px) saturate(1.6);
  -webkit-backdrop-filter: blur(18px) saturate(1.6);
  box-shadow: var(--shadow-flyout);
  /* 滚到两端别把滚动传给正文（浮层是小范围滚动，链式滚动会让页面莫名其妙地动起来） */
  overscroll-behavior: contain;
  animation: cite-pop-in .14s var(--ease);
}
@keyframes cite-pop-in {
  from { opacity: 0; transform: translateY(-4px); }
}
/* 里面不做"盒子里再套盒子"（截图里就是那个观感：外框描边 + 每条又自带描边，看着笨重）——
 * 改成**同一张玻璃纸上的行**：不要边框、不要底色，只有一条发丝分隔线，hover 才浮一点底色。
 * 内容格式一点没动（跳转图标 / 标号 / 中英标题 / GitHub 那枚都还在，还是 `ReferenceList`）。 */
.cite-pop :deep(.ref-list > .item) {
  border: 0; border-radius: 10px; background: transparent; margin-bottom: 0;
  padding: 7px 8px 7px 4px;
}
.cite-pop :deep(.ref-list > .item + .item) { box-shadow: inset 0 1px 0 var(--divider); }
.cite-pop :deep(.ref-list > .item:hover) { background: color-mix(in srgb, var(--accent) 12%, transparent); }
.cite-pop :deep(.ref-list > .item:hover .ref-lead) { color: var(--accent); }
.cite-pop :deep(.ref-list > .item:focus-visible) { box-shadow: var(--ring); }
.cite-pop-hint { padding: 6px 8px; }
.ex-thread { max-height: 46vh; overflow-y: auto; }
/* 笔记框标题行上的 ✓ 保存（用户口径 2026-10-10：复刻目标站的笔记框）。
   ⚠️ 这个键是**本组件**渲染的（通过 `FloatingCard` 的 `head-extra` 插槽），
   而 `FloatingCard` 的 `.fc-btn` 是它自己的 scoped 样式，管不到这里，所以在这里整形。 */
.np-head-save {
  flex: none; width: 24px; height: 24px; display: inline-flex; align-items: center; justify-content: center;
  border: 0; border-radius: 7px; background: var(--accent-soft); color: var(--accent-text);
  cursor: pointer; transition: background var(--dur-fast) var(--ease), transform var(--dur-fast) var(--ease);
}
.np-head-save:hover:not(:disabled) { background: color-mix(in srgb, var(--accent) 22%, transparent); }
.np-head-save:active:not(:disabled) { transform: scale(.92); }
.np-head-save:disabled { opacity: .5; cursor: default; }
/* 删除键（头部那枚垃圾桶）：语义色与全站 danger 一致 */
.np-head-save.danger { background: var(--danger-soft); color: var(--danger); }
.np-head-save.danger:hover:not(:disabled) { background: color-mix(in srgb, var(--danger) 22%, transparent); }
/* ---- 翻页 / 视图切换动效（用户口径 2026-10-10）----
   页面块出现/重建时**只淡入 opacity**。
   ⚠️ 故意**不用 transform**：① 那会给块建一个"包含块"，里面的 `position: fixed` 定位会跟着跑；
   ② 块里有 canvas，`transform` 动画的每一帧都要重新采样位图 —— 那正是前几次"闪/抖"的根源。
   opacity 是合成层属性，不动布局、不重采样。 */
.reader-content :deep(.pg-pair),
.reader-content :deep(.row-pair) { animation: pg-in .18s var(--ease); }
@keyframes pg-in { from { opacity: 0; } to { opacity: 1; } }
/* 头部那行相对时间（`head-extra` 插槽里的第一个节点，排在 ✓ 前面） */
.np-time { flex: none; margin-right: 6px; font-size: 11px; color: var(--text-faint); }
/* 笔记卡内嵌色条：与「笔记」两字**同一行**（用户口径 2026-10-10），
   所以自身不带外边距/内边距，间距交给标题行的 `gap`。 */
.np-colors { display: flex; align-items: center; gap: 5px; flex: none; margin: 0; }
.np-colors .hl-swatch { border: 0; cursor: pointer; }
/* ---- 笔记框照目标站重做（用户口径 2026-10-10）----
   标注卡：`rounded-2xl`(16px) + 1px 描边 + 大阴影 + **不透明底**；
   编辑区：`min-h-24`(96px) / `max-h-64`(256px)、`text-base/relaxed`(16px/1.6)、
   **无边框无圆角**（用户口径："多了中间的圆角矩形框"，那个框要拿掉）、
   水平内收 **16px**（与标题同一个缩进："文字位置不能像那样靠边"）。
   ⚠️ 选择器**带 `[data-note-popover]` 前缀**：权重高于组件里原有的 `.np-edit`，
   不管两条规则谁前谁后都能盖住（不靠文件里的先后顺序）。*/
[data-note-popover] { border-radius: 16px; background: var(--bg-elev); }
/* 笔记卡的正文区（色点在标题行里，正文自己内收 12px 左对齐标题） */
[data-note-popover] :deep(.fc-body) { padding: 0; }
[data-note-popover] .np-edit {
  /* 高度交给模板上的 `rows`（跟着已有内容走，见 `noteEditRows`）：
     **不设 `max-height`** —— 否则长笔记会在框里再套一层滚动条（框里一根、卡片一根）。
     内容真的超出卡片时，让外层 `.fc-body` 去滚。 */
  width: 100%; min-height: 96px; resize: none;
  /* 左内边距 12px（不是 16）：`.fc-head` 自己还有 12px 内边距，
     加起来与标题那枚图标同一个缩进（用户口径 2026-10-10："文字位置不能像那样靠边"）。 */
  padding: 4px 16px 4px 12px; font-size: 16px; line-height: 1.6;
  border: 0; border-radius: 0; background: transparent;
}
[data-note-popover] :deep(.fc-head) {
  gap: 6px;
  /* 标题行用**横线**（`--divider` 发丝线）与正文分开（用户口径 2026-10-10）。
     ⚠️ 底色保持透明：上一版那层 3% 灰底就是用户说的"两个颜色"，不再加回来。 */
  border-bottom: 1px solid var(--divider);
  background: transparent;
}
/* 聚焦 / 悬浮时要**整圈压掉** base.css 里那条全局 `input, select, textarea`
   （用户口径 2026-10-10："存在横线的问题"）：它给了
   `:focus { border-color: accent; box-shadow: var(--ring) }` 与 `:hover { background: hover }`。
   而 `.np-edit` 宽 100%、紧贴卡片内边，那圈光晕的左右两边被卡片 `overflow: hidden` 裁掉 ——
   屏幕上只剩**下边一条青线**，看着就像卡片中间无故多了一条分隔线。
   卡片本身就是输入区，不用再描一圈，也不要悬浮变灰。 */
[data-note-popover] .np-edit:hover { background: transparent; }
[data-note-popover] .np-edit:focus {
  outline: none; box-shadow: none; border-color: transparent; background: transparent;
}
/* **查看态**的正文：它是 `.np-view`（不是 `.np-edit`），而 `.fc-body` 的内边距已被清零 ——
   不给它自己补内边距的话，写好的笔记会贴着卡片左边缘、上面还顶到分隔线（用户报的"第二行显示异常"）。
   与编辑态那份 `4px 16px 4px 12px` 对齐，底部再留 12px 免得文字贴着卡片下沿。 */
[data-note-popover] .np-view { padding: 8px 16px 12px 12px; }
[data-note-popover] :deep(.fc-title) { font-size: 13px; font-weight: 600; flex: none; }
/* 色条（在标题行里，排在「笔记」**右边**）：不再自占一行，所以没有内边距；
   左边留 2px 与标题错开一点，不要和「笔记」两个字粘死 */
[data-note-popover] .np-colors { padding: 0; margin: 0 0 0 2px; }
/* 自定义色那枚：与胶囊上那枚同一个做法（`SelectionMenu` 的样式是 scoped 的，这里得再来一份） */
[data-note-popover] .hl-swatch.custom {
  position: relative; overflow: hidden;
  background: conic-gradient(from 90deg, #ff0080, #ff8c00, #ffe600, #00d26a, #00b3ff, #7c3aed, #ff0080);
}
[data-note-popover] .hl-swatch.custom input {
  position: absolute; inset: 0; width: 100%; height: 100%; padding: 0; border: 0;
  opacity: 0; cursor: pointer; appearance: none;
}
/* 插图解释：顶上那张缩略图（比正文宽、但别把浮层撞成一面墙） */
.ex-fig { display: flex; flex-direction: column; gap: 4px; margin-bottom: 10px; }
.ex-fig img {
  max-width: 100%; max-height: 160px; object-fit: contain;
  border-radius: var(--radius-sm); border: 1px solid var(--border); background: var(--bg-elev);
}
.ex-turn { margin-bottom: 10px; }
.ex-turn .md :deep(p) { margin: .35em 0; }
.ex-q {
  font-size: 12px; color: var(--text-dim); padding: 3px 8px;
  border-left: 2px solid var(--accent); background: var(--bg-layer);
  border-radius: 0 var(--radius-sm) var(--radius-sm) 0;
}
.ex-cites { display: flex; flex-wrap: wrap; gap: 4px; margin-top: 5px; }
/* 过程（深度思考 / 工具调用）：**默认只露一行**，点开才铺开 —— 用户口径是"只展示最终回答"。
   与侧栏对话那条轨迹同一套信息，但这里更紧凑（浮层窄，一行一条）。 */
.ex-proc { margin: 0 0 6px; }
.ex-proc-head {
  display: flex; align-items: center; gap: 5px; width: 100%;
  border: 1px solid var(--border); border-radius: var(--radius-sm);
  background: var(--bg-layer); color: var(--text-dim);
  font: inherit; font-size: 11.5px; padding: 3px 7px; cursor: pointer; text-align: left;
}
.ex-proc-head:hover { background: var(--bg-hover); color: var(--text); }
.ex-proc-body {
  margin-top: 4px; padding: 4px 6px; max-height: 26vh; overflow-y: auto;
  border: 1px solid var(--divider); border-radius: var(--radius-sm);
}
.ex-step {
  display: flex; align-items: baseline; gap: 5px;
  font-size: 11.5px; color: var(--text-dim); padding: 2px 0; min-width: 0;
}
.ex-step > svg { flex: none; align-self: center; }
.ex-step-tag { flex: none; color: var(--text-faint); }
.ex-step-text {
  flex: 1; min-width: 0; color: var(--text);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.ex-step.is-thinking .ex-step-text { white-space: normal; }
.ex-step.is-tool .ex-step-tag { color: var(--accent-text); }
.chip {
  border: 1px solid var(--border); background: var(--bg-layer); color: var(--text-dim);
  border-radius: 999px; padding: 1px 7px; font-size: 11px; font-family: var(--mono); cursor: pointer;
}
.chip:hover { color: var(--accent-text); border-color: var(--accent); }
.ex-ask { display: flex; gap: 6px; margin-top: 10px; }
.ex-ask input { flex: 1; min-width: 0; }
.ex-foot { display: flex; align-items: center; gap: 6px; margin-top: 8px; }
/* 底部那排"标出这段并存为笔记"的色块（浮层里的横向一排：标签 + 4 枚色点） */
.ex-save-hint { flex: none; }
.ex-swatches { display: inline-flex; align-items: center; gap: 5px; flex: none; }

@media print {
  .reader-toolbar, .reader-aside { display: none !important; }
  .reader, .reader-content { display: block; height: auto; overflow: visible; }
  /* 打印时收回悬浮层的让位与平移，正文按纸张宽度排 */
  .reader-slide { width: auto; transform: none; }
}
</style>

