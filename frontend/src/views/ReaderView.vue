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
  PageTranslation, PairOrder, Paper, ReaderPayload, ReaderStats, TaskInfo,
  TranslatedMarkdownPage, ViewMode, ZoomTarget,
} from '@/api/types';
import { askConfirm } from '@/stores/confirm';
import { useEventStream, type StreamStatus } from '@/composables/useEventStream';
// 解析完成后要**强制**重取解读（`useUnderstanding` 是按论文缓存的整包，TTL 30s）
import { loadUnderstanding } from '@/composables/useUnderstanding';
import { useHotkeys } from '@/composables/useHotkeys';
import { htmlWithHighlights, mdToHtml } from '@/composables/useMarkdown';
import { markRanges, rangeOffsets, renderedText } from '@/utils/domMarks';
import { buildPageGroups, flattenRows } from '@/composables/usePageRows';
import { taskTypeLabel } from '@/utils/tasks';
import { confirmRegenerateInsight } from '@/utils/understanding';
import { READER_TEXT_SIZES, type ReaderTextSize } from '@/utils/readerFont';
import { addReaderSeconds, noteReaderOpen } from '@/utils/readerStats';
import { reportError, toast, withLoading } from '@/stores/app';
import { openTab, routeKey, setTabTitle } from '@/stores/tabs';
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
import ReaderSidePanel, { type BriefDraft, type SideTab } from '@/components/reader/ReaderSidePanel.vue';
import ReaderToc from '@/components/reader/ReaderToc.vue';
import ReaderPages from '@/components/reader/ReaderPages.vue';
import FloatingCard from '@/components/reader/FloatingCard.vue';
import SelectionMenu from '@/components/reader/SelectionMenu.vue';

const HIGHLIGHT_COLORS = ['yellow', 'green', 'blue', 'pink'];
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

function setZoom(next: number) {
  const v = Math.round(Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, next)) * 100) / 100;
  if (v !== zoom.value) zoom.value = v;
}

/** ± 一档：0.1（细步进。以前 0.15 一档，点两下就跳 30%，看不出连续性） */
function zoomBy(dir: number) {
  setZoom(zoom.value + dir * 0.1);
}

/**
 * Ctrl/⌘ + 滚轮缩放（触控板捏合在 Chrome 里就是带 `ctrlKey` 的 wheel 事件）。
 *
 * ⚠️ 必须 `preventDefault`：不拦的话会连带触发**浏览器自己的页面缩放** —— 那才是
 * "我明明缩放了、百分比却不动"的根源：浏览器缩放放大的是整个界面（连百分比标签一起放大），
 * 而它并不表示页图的显示比例。拦下来之后，同一个手势改的是 `zoom`，页图与数字一起动。
 */
function onContentWheel(ev: WheelEvent) {
  if (!ev.ctrlKey && !ev.metaKey) return;
  ev.preventDefault();
  // deltaY 在触控板上是连续小值、在鼠标滚轮上是一格 ±100：用比例步进让两者手感接近
  setZoom(zoom.value - ev.deltaY * 0.002);
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
 * `view` = 版面选项（原文栏呈现 / 文字列侧 / 左右顺序）；`display` = 显示（字号 + 页图缩放）。
 * 面板本体始终挂载（`v-show`），原因见 `CapPanel.vue` 的头注释（自检脚本按文字点里面的按钮）。
 */
const capPanel = ref<'' | 'view' | 'display'>('');

function toggleCapPanel(which: 'view' | 'display') {
  capPanel.value = capPanel.value === which ? '' : which;
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
  open: false, loading: false, question: '', rowId: '', anchored: true,
  /**
   * 选区在哪个视图里划的：`row` 有行锚点、`markdown` 只有页号、`pdf` 只有**页号 + 文字层偏移**。
   * 三种都不影响"解释"本身（解释是拿选区文字去问），只影响锚点怎么传回去。
   */
  scope: 'row' as 'row' | 'markdown' | 'pdf',
  selectionText: '', page: 0 as number, conversationId: '' as string | null,
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
  history: [] as Array<{ id: string; text: string; at?: string | null; rowId?: string | null }>,
  historyOpen: false,
});
let explainController: AbortController | null = null;
/** 从正文划词带进「对话」面板的选区（发出去之后由面板 `clear-selection` 清掉） */
const chatSelection = ref<AgentSelection | null>(null);
/** 笔记浮层的状态见下面 `notePop`（第五步：轻量浮层取代居中弹窗）。 */
/** 放大查看的目标（图 URL + 图注）：见下面的 `zoomTitle` */
const zoomImage = ref<ZoomTarget | null>(null);

/**
 * 放大弹窗的标题 = **这张图的图注**（用户口径）。
 *
 * 以前这里固定写"插图"，看大图时反而不知道在看哪张图 —— 图注本来就在 Markdown 产物里
 * （`media.caption`，形如 `Figure 3: Evaluation of …`），直接拿来当标题即可。
 * 图注通常很长，标题按一行截断（`Modal` 的 `.modal-title` 带省略号，hover 看全文 tooltip）；
 * 万一这张图没有图注，就退回"插图 · 第 N 页"。
 */
const zoomTitle = computed(() => {
  const t = zoomImage.value;
  if (!t) return '插图';
  const caption = String(t.caption ?? '').trim();
  if (caption) return caption;
  return t.page ? `插图 · 第 ${t.page} 页` : '插图';
});
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
  'toc', 'chat', 'insight', 'search', 'annotations', 'notes', 'formulas', 'figures', 'terms', 'tasks',
];
/* 进「论文解读」页签就把左栏放宽（`widenForInsight` 只增不减）。
   放在这里而不是点击处理器里：点页签 / `?side=` 深链 / 工具栏按钮都算"进了解读"。 */
watch(sideTab, (k) => {
  if (k === 'insight') void widenForInsight();
  else if (k === 'chat') void widenForChat();
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

const SIDE_TABS = computed<Array<{
  k: SideTabKey; icon: string; label: string; title: string; count?: number;
}>>(() => [
  { k: 'toc', icon: 'list', label: '目录', title: '目录（中英双语）' },
  {
    k: 'chat', icon: 'chat', label: '对话',
    title: '论文问答（多轮对话；圆环 = 上下文用量，点它压缩）',
  },
  {
    k: 'insight', icon: 'sparkle', label: '论文解读',
    title: '论文解读（论文精读：基本信息 / 背景 / 动机 / 贡献 / 方法 / 实验 / 结论）',
  },
  {
    k: 'search', icon: 'search', label: '搜索',
    title: searchMode.value === 'semantic' ? '语义搜索（只查本文）' : '全文搜索（原文 + 译文）',
    // 角标只报**当前模式**的命中数：语义模式的命中在 ReaderSearch 里，词法那份数此时是旧账
    count: searchMode.value === 'lexical' ? (searchHits.value.length || undefined) : undefined,
  },
  {
    k: 'annotations', icon: 'quote', label: '批注·笔记',
    title: '批注与笔记（划词解释 + 人工批注 + 笔记，按时间倒序）',
    count: annotations.value.length + notes.value.length,
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
    if (SIDE_TAB_KEYS.includes(sideParam)) {
      // 兼容旧的 ?side=notes：批注与笔记已合并为一个页签
      sideTab.value = (sideParam === 'notes' ? 'annotations' : sideParam) as SideTabKey;
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
  if (route.query.md) {                       // ?md=1[&mdpage=8] 直接打开 Markdown 视图
    const want = Number(route.query.mdpage ?? 0);
    await openMarkdownView(want || undefined);   // 不给页码 = 从当前阅读页那一段开始
  }
  fetchTasks();
  taskTimer = setInterval(() => { if (hasRunningTask.value) fetchTasks(); }, 4000);
});

onUnmounted(() => {
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
): { start: number; end: number; text: string; rect: DOMRect } | null {
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
  return { ...offsets, text: inter.toString().trim(), rect: inter.getBoundingClientRect() };
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

const hideMenu = () => { selection.show = false; };

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
function onAnyScroll() { hideMenu(); }
function onDocPointerDown(ev: PointerEvent) {
  const target = ev.target as HTMLElement | null;
  if (target?.closest?.('[data-selection-menu]')) return;
  // 顶栏弹出面板：点面板外面就收起来（面板内的点击不动它 —— 改一个分段值不该把面板关掉）
  if (capPanel.value && !target?.closest?.('.reader-toolbar')) capPanel.value = '';
  hideMenu();
}

/**
 * Ctrl+C：**选区跨到了对面列时，剪贴板只放本列的文字**。
 *
 * 浏览器复制的是整段连续选区，而纵向拖多行时它必然包含对面列的单元格（见 `base.css` 里的说明）。
 * 只有"确实盖住了对面列"才改写：平时单列复制原样交给浏览器，别动它的空白/换行口径。
 */
function onCopy(ev: ClipboardEvent) {
  const sel = window.getSelection();
  if (!sel || sel.isCollapsed || sel.rangeCount === 0 || !sel.anchorNode) return;
  // 判侧与划词/删除高亮同一处（锚点拿不到就看焦点那一端）
  const side = rowsSide(sel.anchorNode) || rowsSide(sel.focusNode);
  if (!side) return;
  const range = sel.getRangeAt(0);
  const other = side === 'en' ? 'zh' : 'en';
  const covers = Array.from(document.querySelectorAll<HTMLElement>(`.rrow.side-${other} .rr-body`))
    .some((b) => { try { return !!offsetsWithin(b, range); } catch { return false; } });
  if (!covers) return;
  const text = joinParts(rowSegments(range, side));
  // 万一本列一段都没取到（全是空白交界处）：**别退回浏览器默认**（那会复制到对面列），
  // 宁可不改写（`preventDefault` 都不做，剪贴板保持原样由浏览器处理）。
  if (!text) return;
  ev.clipboardData?.setData('text/plain', text);
  ev.preventDefault();
}

/** 划词解释 / 追问：都走问答 agent（同一 conversation_id，追问自带上下文）。 */
async function askSelection(question: string) {
  const q = question.trim();
  if (q) explainCard.turns.push({ role: 'user', text: q, citations: [] });
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
    else if (name === 'answer_delta') explainCard.turns[idx].text += data?.text ?? '';
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

async function explainSelection() {
  // 无块锚点（原 PDF 划词）时只传文本：agent 那边拿不到块就少一层上下文，仍能解释
  const rowId = selection.anchored ? selection.rowId : '';
  hideMenu();
  explainCard.open = true;
  explainCard.loading = true;
  explainCard.question = '';
  explainCard.rowId = rowId;
  explainCard.anchored = selection.anchored;
  explainCard.scope = selection.scope;
  explainCard.selectionText = selection.text;
  explainCard.page = Number(selection.page) || 0;
  explainCard.conversationId = null;
  explainCard.turns = [];
  explainCard.historyOpen = false;
  explainCard.processOpen = false;                    // 过程默认折叠（用户口径）
  /* 选区快照 + 浮层锚点：读完回答再点「存为笔记」时，页面上的选区可能早被改了 */
  explainCard.highlightId = selection.hlIds[0] ?? '';
  explainCard.start = selection.start;
  explainCard.end = selection.end;
  explainCard.side = selection.side;
  explainCard.quote = selection.anchorText || selection.text;
  explainCard.anchor = { x: selection.x, y: selection.y, w: 0, h: 0 };
  void loadExplainHistory();
  await askSelection('');
}

function submitExplainQuestion() {
  const q = explainCard.question.trim();
  if (!q || explainCard.loading) return;
  explainCard.question = '';
  void askSelection(q);
}

/** 划词解释历史 = `source=selection` 的批注（`存为批注` 存下来的那些）。 */
async function loadExplainHistory() {
  try {
    const res: any = await api.get('/annotations', { paper_id: paperId.value });
    const list: Annotation[] = res?.items ?? res ?? [];
    explainCard.history = list
      .filter(a => a.source === 'selection')
      .slice(0, 20)
      .map(a => ({ id: a.id, text: a.content_md, at: a.created_at, rowId: a.block_id }));
  } catch {
    explainCard.history = [];
  }
}

function showHistoryItem(item: { text: string; rowId?: string | null }) {
  explainCard.turns = [{ role: 'assistant', text: item.text, citations: [] }];
  explainCard.rowId = item.rowId ?? explainCard.rowId;
  explainCard.historyOpen = false;
}

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
  const anchored = fromMenu ? selection.anchored : explainCard.anchored;
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

function focusCitation(citation: AgentCitation) {
  explainCard.open = false;
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

async function saveExplanationAsAnnotation() {
  const answer = [...explainCard.turns].reverse().find(t => t.role === 'assistant')?.text ?? '';
  if (!answer.trim()) { toast('还没有可保存的解释', 'warn'); return; }
  try {
    await api.post('/annotations/save', {
      paper_id: paperId.value,
      block_id: explainCard.rowId || null,
      content_md: answer,
    });
    toast('已存为批注', 'ok');
    explainCard.open = false;
    await refreshAnnotations();
  } catch (err) { reportError(err, '存为批注失败'); }
}

/**
 * 「存为笔记」（《修改计划》第五步）：把 AI 解释的**正文**挂到这段文字对应的**高亮**上。
 *
 * 用户口径是"高亮 → AI 解释 → 用户选择 → 保存为笔记"，**不默认自动保存** —— 所以它是个按钮，
 * 而且与笔记浮层同一条路：选区已经有高亮就复用它，没有就先补一条（笔记必须挂在高亮上）。
 * 快照在**打开浮层那一刻**就存下了（`explainCard.highlightId/start/end/quote`）：读完回答再点保存时，
 * 页面上的选区可能早就被用户改了。
 */
async function saveExplanationAsNote() {
  const answer = [...explainCard.turns].reverse().find(t => t.role === 'assistant')?.text ?? '';
  if (!answer.trim()) { toast('还没有可保存的解释', 'warn'); return; }
  /* 同样只往**已有高亮**上挂（用户口径：只有插入了高亮，才能在高亮的基础上加笔记）——
     打开解释浮层那一刻若这段文字没被标过，这里就直说，而不是悄悄替用户标一条。 */
  if (!explainCard.highlightId) {
    toast('先给这段文字加个高亮，再存为笔记', 'warn', 2600);
    return;
  }
  try {
    await api.post('/notes', {
      paper_id: paperId.value, block_id: explainCard.rowId, highlight_id: explainCard.highlightId,
      selected_text: explainCard.quote || explainCard.selectionText,
      start_offset: explainCard.start, end_offset: explainCard.end,
      content_md: answer,
    });
    toast('已存为这处高亮的笔记', 'ok', 2600);
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
    return created;
  } catch (err) { reportError(err, '高亮失败'); return []; } finally { hideMenu(); }
}

/**
 * 删除高亮：**从划词菜单里点「禁止」图标**（用户口径：不再点高亮本身）。
 * 后端会把同一分组（跨行高亮）的记录一起删掉，并回传所有被删的 id，这里同步本地列表。
 */
async function deleteSelectionHighlights() {
  const ids = selection.hlIds.slice();
  if (!ids.length) { hideMenu(); return; }
  hideMenu();
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
  toast(`已删除 ${gone.size} 处高亮`, 'ok', 1800);
}

/**
 * 点已有高亮（没拖选）：把划词菜单叫出来，里面那个「禁止」图标负责删除。
 * 以前是"点一下弹原生确认框直接删"，用户口径改成菜单删除。
 */
function onContentClick(ev: MouseEvent) {
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
 * - **悬停高亮**：只看**已有**笔记（`mode='view'`），鼠标移开自动消失（除非已锚定）；
 * - **划词菜单点「笔记」**：新建（`mode='edit'`），保存时先确保挂到一条高亮上；
 * - **划词菜单点「打开笔记」**：打开已有那条（`mode='view'`，可切到编辑）。
 */
const notePop = reactive({
  open: false,
  /** 锚定：鼠标移开也不关（点浮层那枚图钉切换）。**默认不锚定**（用户口径） */
  anchored: false,
  mode: 'view' as 'view' | 'edit',
  noteId: '',
  highlightId: '',
  rowId: '',
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
  note?: Note | null; highlight?: Highlight | null; mode?: 'view' | 'edit';
  rect?: DOMRect | null; anchored?: boolean; fallbackXY?: { x: number; y: number };
}) {
  const note = opts.note ?? null;
  const hl = opts.highlight ?? null;
  notePop.open = true;
  notePop.anchored = opts.anchored ?? false;
  notePop.mode = opts.mode ?? (note ? 'view' : 'edit');
  notePop.noteId = note?.id ?? '';
  notePop.highlightId = note?.highlight_id ?? hl?.id ?? selection.hlIds[0] ?? '';
  notePop.rowId = note?.block_id ?? hl?.block_id ?? selection.rowId ?? '';
  notePop.quote = note?.selected_text ?? hl?.text
    ?? (selection.anchored ? (selection.anchorText || selection.text) : '');
  notePop.content = note?.content_md ?? '';
  notePop.draft = note?.content_md ?? '';
  notePop.anchor = rectAnchor(opts.rect, opts.fallbackXY);
  notePop.busy = false;
}

function closeNotePopover() {
  window.clearTimeout(noteHideTimer);
  notePop.open = false;
  notePop.anchored = false;
  notePop.mode = 'view';
}

/* 悬停显示已存在的笔记；移开延时关闭（延时是为了让鼠标能从高亮挪到浮层上） */
let noteHideTimer: number | undefined;
function cancelNoteHide() { window.clearTimeout(noteHideTimer); }
function scheduleNoteHide() {
  if (!notePop.open || notePop.anchored || notePop.mode === 'edit') return;
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

/** 悬停到一处高亮：**只看已有笔记**（新建走划词菜单，免得鼠标扫过就弹编辑框）。 */
function onContentHover(ev: MouseEvent) {
  const mark = markOf(ev);
  if (!mark) return;
  const hl = highlights.value.find((h) => h.id === (mark.getAttribute('data-hl') || ''));
  const note = hl ? notes.value.find((n) => n.highlight_id === hl.id) : null;
  if (!hl || !note) return;
  if (notePop.open && notePop.noteId === note.id) { cancelNoteHide(); return; }
  openNotePopover({ note, highlight: hl, mode: 'view', rect: mark.getBoundingClientRect() });
}

function onContentHoverOut(ev: MouseEvent) {
  if (markOf(ev)) scheduleNoteHide();
}

/** 选区/点击处那条高亮对应的笔记（有的话菜单显示「打开笔记」）。 */
const selectionNote = computed<Note | null>(() => {
  const ids = selection.hlIds ?? [];
  if (!ids.length) return null;
  return notes.value.find((n) => n.highlight_id && ids.includes(n.highlight_id)) ?? null;
});

/**
 * 划词菜单里的「笔记 / 打开笔记」：
 * 已有笔记 → 打开**浮层**看它（点菜单是明确动作，所以直接锚定）；
 * 没有 → 打开浮层新建（保存时先确保挂到一条高亮上）。
 */
async function onNoteAction() {
  const existing = selectionNote.value;
  // 浮层锚在**划词菜单**出现的位置（选区矩形就在那儿），菜单本身马上要收起
  const fallbackXY = { x: selection.x, y: selection.y };
  hideMenu();
  if (existing) {
    const hl = highlights.value.find((h) => h.id === existing.highlight_id) ?? null;
    openNotePopover({ note: existing, highlight: hl, mode: 'view', anchored: true, fallbackXY });
    return;
  }
  // 没有高亮就没有"可以挂笔记的地方"（菜单里那一项也不会出现，这里是兜底）
  if (!selection.hlIds.length) {
    toast('先给这段文字加个高亮，再写笔记', 'warn', 2600);
    return;
  }
  openNotePopover({ mode: 'edit', anchored: true, fallbackXY });
}

/** 浮层里「保存」：已有笔记走 PUT，新建走 POST（**必须已经挂在一处高亮上**）。 */
async function saveNotePopover() {
  if (notePop.busy) return;
  /* 用户口径：**只有插入了高亮，才能在高亮的基础上加笔记** —— 这里不再"顺手补一条高亮"，
     没有高亮就明说怎么做（划词菜单里那一项本来也不会出现，这是兜底）。 */
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
      toast('笔记已保存', 'ok', 1600);
    }
    notePop.mode = 'view';
    await refreshAnnotations();
  } catch (err) { reportError(err, '保存笔记失败'); } finally { notePop.busy = false; }
}

async function refreshAnnotations() {
  const [a, h, n] = await Promise.all([
    api.get<{ items: Annotation[] }>('/annotations', { paper_id: paperId.value }),
    api.get<{ items: Highlight[] }>('/highlights', { paper_id: paperId.value }),
    api.get<{ items: Note[] }>('/notes', { paper_id: paperId.value }),
  ]);
  annotations.value = a.items ?? [];
  highlights.value = h.items ?? [];
  notes.value = n.items ?? [];
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
   * 否则跨页/跨行选一大段时，菜单「复制」只给出前 600 字（实测 601 字 vs Ctrl+C 2537 字）。 */
  await copyText(selection.fullText || selection.text);
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
      <div class="reader-toolbar" :style="{ '--panel-shift': capsuleShiftCss }">
        <div class="cap-solo left">
          <AsideToggle :open="asideVisible" @toggle="toggleAside" />
        </div>

        <div class="reader-capsule" :style="{ transform: `translateX(${capsuleShift}px)` }">
          <div class="cap-group">
            <div class="seg">
              <button :class="{ active: viewMode === 'original' }" @click="setViewMode('original')">原文</button>
              <button :class="{ active: viewMode === 'translation' }" @click="setViewMode('translation')">译文</button>
              <button :class="{ active: viewMode === 'bilingual' }" @click="setViewMode('bilingual')">对照</button>
            </div>
          </div>

          <div class="cap-group">
            <button
              class="btn icon"
              data-cap-panel="view"
              title="版面选项（原文栏呈现 / 文字列侧 / 左右顺序）"
              aria-haspopup="true"
              :aria-expanded="capPanel === 'view'"
              @click="toggleCapPanel('view')"
            >
              <AppIcon name="sliders" :size="15" />
            </button>
            <button
              class="btn cap-aa"
              data-cap-panel="display"
              title="显示：正文字号与页图缩放（Ctrl + 滚轮 / 触控板捏合也能缩放）"
              aria-haspopup="true"
              :aria-expanded="capPanel === 'display'"
              @click="toggleCapPanel('display')"
            >
              Aa<span v-if="originalIsPage" class="tiny faint nowrap">{{ Math.round(zoom * 100) }}%</span>
            </button>
          </div>

          <!-- 翻译状态与主操作：翻译完成时**整组消失**（用户口径：不要那个常驻的绿色对勾） -->
          <div class="cap-group" v-if="!translateDone">
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

        <!-- 右：更多（低频动作：重译 / 打开 PDF / Markdown 视图） -->
        <div class="cap-solo right">
          <Menu :items="toolbarMenu" label="更多操作" size="md" @select="onToolbarAction" />
        </div>

        <!-- 弹出面板（互斥，落在主胶囊正下方）。里面每个控件都是**常挂载**的：
             `frontend_check.py` 按文字/标题点「原文文本 / 原 PDF / 小」，v-if 会让它点不到。 -->
        <CapPanel :open="capPanel === 'view'" label="版面选项" data-cap-panel-body="view">
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

        <CapPanel :open="capPanel === 'display'" label="显示" data-cap-panel-body="display">
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
            </div>
          </div>
        </CapPanel>
      </div>      <!-- ============ 左栏：目录 + 批注/笔记/公式/插图/术语/任务/论文解读（可整体折叠，可拖宽） ============
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
            <div class="toc-mode" role="tablist" aria-label="目录 / 页面视图">
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
            <span class="spacer"></span>
            <template v-if="tocMode === 'outline'">
              <button class="btn icon sm" title="全部折叠" @click="collapseAll">
                <AppIcon name="unfold-less" :size="14" />
              </button>
              <button class="btn icon sm" title="全部展开" @click="expandAll">
                <AppIcon name="unfold-more" :size="14" />
              </button>
            </template>
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
          :current-row-id="currentRowId"
          :highlights="highlights"
          :search-term="searchQuery.trim()"
          :flash-row-id="flashRowId"
          :flash-nonce="flashNonce"
          :translating="translating"
          :font-size="readerFont"
          @focus="focusRow"
          @zoom="zoomImage = $event"
          @translate-page="(p: number) => startTranslate('resume', [p])"
        />
        </div>
      </section>
    </div>

    <SelectionMenu
      :visible="selection.show"
      :x="selection.x"
      :y="selection.y"
      :colors="HIGHLIGHT_COLORS"
      :anchored="selection.anchored"
      :allow-note="selection.allowNote"
      :hl-count="selection.hlIds.length"
      :has-note="!!selectionNote"
      @explain="explainSelection"
      @chat="openSelectionInChat(true)"
      @note="onNoteAction"
      @copy="copySelection"
      @highlight="addHighlight"
      @unhighlight="deleteSelectionHighlights"
      @close="hideMenu"
    />

    <!-- AI 解释浮层（第五步）：与笔记**同一个壳**、**默认锚定**（读完再决定要不要存下来，
         中途移开鼠标就关掉太容易丢）。没有查看/编辑切换（用户口径：AI 解释不需要）。
         深度思考与工具调用**默认折叠**，只展示最终回答。 -->
    <FloatingCard
      :open="explainCard.open"
      :anchor="explainCard.anchor"
      :anchored="true"
      title="AI 解释（只依据论文内容）"
      :width="420"
      :max-height="520"
      data-explain-card
      @close="explainCard.open = false"
    >
      <div class="ex-thread">
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
            <div v-if="t.text" class="md small" data-ex-answer v-html="renderMarkdown(t.text)" />
            <div v-if="t.citations.length" class="ex-cites">
              <button v-for="c in t.citations" :key="c.ref" class="chip" :title="c.snippet || c.label"
                      @click="focusCitation(c)">
                [{{ c.ref }}]<span v-if="c.page"> p{{ c.page }}</span>
              </button>
            </div>
          </template>
        </div>
        <div v-if="explainCard.loading" class="tiny faint">正在读这篇论文…</div>
      </div>

      <div class="ex-ask">
        <input
          v-model="explainCard.question"
          placeholder="就这段继续问（Enter 发送）"
          @keyup.enter="submitExplainQuestion"
        >
        <button class="btn sm primary" :disabled="explainCard.loading || !explainCard.question.trim()"
                @click="submitExplainQuestion">
          提问
        </button>
      </div>

      <div v-if="explainCard.historyOpen" class="ex-history">
        <div v-if="!explainCard.history.length" class="tiny faint">
          还没有存过的解释（点下方「存为批注」就会留在这里）
        </div>
        <button v-for="h in explainCard.history" :key="h.id" class="ex-hrow" @click="showHistoryItem(h)">
          <span class="tiny faint">{{ fmtRelative(h.at) }}</span>
          <span class="md small ex-hhead" v-html="renderMarkdown(h.text)" />
        </button>
      </div>

      <template #footer>
        <button class="btn sm primary" :disabled="explainCard.loading" data-ex-save-note
                title="把这段解释的正文存成这处高亮的笔记"
                @click="saveExplanationAsNote">
          <AppIcon name="note" :size="12" />存为笔记
        </button>
        <button class="btn sm" :disabled="explainCard.loading" @click="saveExplanationAsAnnotation">
          存为批注
        </button>
        <span class="spacer"></span>
        <button class="btn sm ghost" @click="explainCard.historyOpen = !explainCard.historyOpen">
          <AppIcon name="task" :size="12" />历史（{{ explainCard.history.length }}）
        </button>
        <button class="btn sm ghost" title="把这个选区带进侧栏对话里继续聊" @click="openSelectionInChat(false)">
          <AppIcon name="chat" :size="12" />在对话里继续
        </button>
      </template>
    </FloatingCard>

    <!-- 笔记浮层（第五步）：锚在高亮/选区旁边的小卡片，默认"移开就消失"。
         悬停浮层本身要取消关闭（鼠标从高亮挪进来的时候不能把它关掉）。 -->
    <FloatingCard
      :open="notePop.open"
      :anchor="notePop.anchor"
      :anchored="notePop.anchored"
      :title="notePop.mode === 'edit' ? '笔记（编辑）' : '笔记'"
      :width="360"
      data-note-popover
      @close="closeNotePopover"
      @toggle-anchor="notePop.anchored = !notePop.anchored"
    >
      <div v-if="notePop.quote" class="np-quote small faint" v-html="mdToHtml(`“${notePop.quote}”`)" />
      <textarea
        v-if="notePop.mode === 'edit'"
        v-model="notePop.draft"
        class="np-edit"
        rows="6"
        data-note-edit
        placeholder="用 Markdown 写笔记；支持 $公式$"
      />
      <div v-else class="np-view md small" data-note-view v-html="mdToHtml(notePop.content)" />
      <template #footer>
        <template v-if="notePop.mode === 'edit'">
          <button class="btn sm primary" :disabled="notePop.busy" data-note-save @click="saveNotePopover">
            {{ notePop.busy ? '保存中…' : '保存' }}
          </button>
          <button class="btn sm" @click="notePop.mode = 'view'; notePop.draft = notePop.content">
            取消
          </button>
        </template>
        <template v-else>
          <button class="btn sm" data-note-edit-btn @click="notePop.mode = 'edit'; notePop.draft = notePop.content">
            <AppIcon name="edit" :size="12" />编辑
          </button>
          <span class="spacer"></span>
          <span class="tiny faint">{{ notePop.anchored ? '已锚定' : '移开自动关闭' }}</span>
        </template>
      </template>
    </FloatingCard>

    <Modal :open="!!zoomImage" :title="zoomTitle" wide @close="zoomImage = null">
      <img v-if="zoomImage" :src="zoomImage.url" :alt="zoomTitle" style="max-width:100%;background:#fff">
    </Modal>

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
  gap: 10px; height: calc(100vh - var(--header-h));
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
.md-doc.full { max-height: calc(100vh - 190px); }
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
.toc-bar {
  position: sticky; top: 0; z-index: 2;
  display: flex; align-items: center; gap: 4px; flex: none;
  margin: 0 0 6px; padding: 2px 2px 6px;
  background: var(--bg-elev);
  border-bottom: 1px solid var(--divider);
}
.toc-mode {
  display: inline-flex; gap: 2px; flex: none;
  padding: 2px;
  border: 0; border-radius: 999px; background: var(--bg-sunken);
}
.toc-mode > button {
  display: inline-flex; align-items: center; justify-content: center;
  width: 26px; height: 22px; padding: 0; border: 0; border-radius: 999px;
  background: transparent; color: var(--text-dim);
  font: inherit; cursor: pointer;
  transition: background var(--dur) var(--ease), color var(--dur) var(--ease);
}
.toc-mode > button:hover { color: var(--text); }
.toc-mode > button.active {
  background: var(--bg-elev); color: var(--text); box-shadow: var(--shadow-1);
}
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
}

@media (prefers-reduced-motion: reduce) {
  .reader, .reader-aside { transition: none; }
}

/* ---------------- 划词解释卡片（浮动层里的小对话线程） ----------------
 * 由问答 agent 产出（流式），所以这里既要有"正在读"的状态，也要能承接追问。 */
.ex-thread { max-height: 46vh; overflow-y: auto; }
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
.ex-history {
  margin-top: 8px; max-height: 24vh; overflow-y: auto;
  border-top: 1px solid var(--divider); padding-top: 6px;
}
.ex-hrow {
  display: flex; flex-direction: column; align-items: flex-start; gap: 2px; width: 100%;
  border: 0; background: none; color: var(--text); text-align: left; cursor: pointer;
  padding: 5px 6px; border-radius: var(--radius-sm);
}
.ex-hrow:hover { background: var(--bg-hover); }
.ex-hhead {
  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical;
  overflow: hidden; color: var(--text-dim);
}
.ex-hhead :deep(p) { margin: 0; display: inline; }

@media print {
  .reader-toolbar, .reader-aside { display: none !important; }
  .reader, .reader-content { display: block; height: auto; overflow: visible; }
  /* 打印时收回悬浮层的让位与平移，正文按纸张宽度排 */
  .reader-slide { width: auto; transform: none; }
}
</style>

