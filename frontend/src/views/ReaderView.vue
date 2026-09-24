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
import { api, assetUrl, sseUrl } from '@/api/client';
import type {
  Annotation, Block, BriefPayload, FigureItem, GlossaryTerm, Highlight,
  MarkdownFormula, MarkdownViewMode, Note, OriginalMode, Outline, PageInfo, PageRow,
  PageTranslation, PairOrder, Paper, ReaderPayload, ReaderStats, TaskInfo,
  TranslatedMarkdownPage, ViewMode, ZoomTarget,
} from '@/api/types';
import { useEventStream, type StreamStatus } from '@/composables/useEventStream';
import { useHotkeys } from '@/composables/useHotkeys';
import { htmlWithHighlights, mdToHtml } from '@/composables/useMarkdown';
import { markRanges, rangeOffsets } from '@/utils/domMarks';
import { buildPageGroups, flattenRows } from '@/composables/usePageRows';
import { reportError, toast, withLoading } from '@/stores/app';
import { routeKey, setTabTitle } from '@/stores/tabs';
import {
  copyText, debounce, downloadText, flattenOutline, outlineParentIds, pct, truncate,
} from '@/utils/format';
import AppIcon from '@/components/AppIcon.vue';
import Modal from '@/components/Modal.vue';
import Menu from '@/components/Menu.vue';
import ReaderPageRows from '@/components/reader/ReaderPageRows.vue';
import ReaderSearch, { type SearchScope } from '@/components/reader/ReaderSearch.vue';
import ReaderSidePanel, { type BriefDraft, type SideTab } from '@/components/reader/ReaderSidePanel.vue';
import ReaderToc from '@/components/reader/ReaderToc.vue';
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
const showRefs = ref(false);
const zoom = ref(1);
const searchQuery = ref('');
const searchScope = ref<SearchScope>('all');
const searchHits = ref<Array<{ id: string; page: number; side: string; snippet: string }>>([]);
const mobilePane = ref<'' | 'side'>('');
/** 宽屏下侧栏是否展开（窄屏走 mobilePane 抽屉，见 asideVisible）。 */
const asideKey = 'pm.reader.aside';
const asideOpen = ref(localStorage.getItem(asideKey) !== '0');
/** 侧栏当前是否可见：窄屏 = 抽屉打开；宽屏 = 用户没收起。 */
const asideVisible = computed(() => (isNarrow.value ? mobilePane.value === 'side' : asideOpen.value));
const isNarrow = ref(false);
const paneWidth = ref(1200);

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
  { key: 'pdf', label: '打开原 PDF', icon: 'external' },
  { key: 'markdown', label: 'Markdown 视图', icon: 'document' },
];

function onToolbarAction(key: string) {
  if (key === 'retranslate') { startTranslate('retranslate'); return; }
  if (key === 'pdf') { openPdf(); return; }
  if (key === 'markdown') openMarkdownView();
}

const selection = reactive({
  show: false, x: 0, y: 0, text: '', rowId: '', start: 0, end: 0, side: 'en' as 'en' | 'zh',
  /** 选区是否有块锚点（原 PDF 划词只有文字）—— 决定能不能写高亮/笔记 */
  anchored: true,
  /** 在哪个视图里划的：正文行 row / Markdown 视图 markdown（决定锚点怎么存） */
  scope: 'row' as 'row' | 'markdown',
  /** scope='markdown' 时的页号（锚点是 `md:p<页号>`） */
  mdPage: 0,
  /** Markdown 视图里不给「笔记」：笔记靠 `block_id` 定位回正文，那边的锚点在正文里不存在 */
  allowNote: true,
  /** 跨行选区按行切开后的各段（每行一条记录，共用同一个分组） */
  segments: [] as Array<{ rowId: string; start: number; end: number; text: string }>,
  /** 选区/点击处盖住的已有高亮 id（菜单据此显示「删除高亮」） */
  hlIds: [] as string[],
});
const explainCard = reactive({
  open: false, loading: false, text: '', data: null as any, rowId: '', anchored: true,
});
const noteModal = reactive({ open: false, text: '', rowId: '', selected: '' });
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

/** 侧栏（左栏，与目录合并）：目录 + 搜索 + 批注·笔记 / 公式 / 插图 / 术语 / 任务 */
type SideTabKey = 'toc' | 'search' | SideTab;
const sideTab = ref<SideTabKey>('toc');
const SIDE_TAB_KEYS = ['toc', 'search', 'annotations', 'notes', 'formulas', 'figures', 'terms', 'tasks'];
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
    k: 'search', icon: 'search', label: '搜索',
    title: '全文搜索（原文 + 译文）',
    count: searchHits.value.length || undefined,
  },
  {
    k: 'annotations', icon: 'quote', label: '批注·笔记',
    title: '批注与笔记（划词解释 + 人工批注 + 笔记，按时间倒序）',
    count: annotations.value.length + notes.value.length,
  },
  {
    k: 'formulas', icon: 'formula', label: '公式',
    title: '公式（从 Markdown 提取，只读）', count: formulas.value.length,
  },
  { k: 'figures', icon: 'image', label: '插图', title: '插图', count: figures.value.length },
  { k: 'terms', icon: 'layers', label: '术语', title: '术语与文档简报', count: termCount.value },
  { k: 'tasks', icon: 'task', label: '任务', title: '任务与进度' },
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
onUnmounted(() => { tabsRO?.disconnect(); tabsRO = null; });
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
  const part = (side: 'en' | 'zh', md?: string | null) =>
    `<div class="md-part" data-md-side="${side}">${mdToHtml(md ?? '')}</div>`;
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

/** 按**当前视图模式**拼整篇 Markdown（`?md=1` 深链与下载共用同一份口径）。 */
function markdownFor(mode: MarkdownViewMode): string {
  const parts = mdView.pages.map((p) => {
    const head = `<!-- page ${p.page} -->\n\n## 第 ${p.page} 页`;
    if (mode === 'original') return `${head}\n\n${p.original_md}`;
    if (mode === 'translation') return `${head}\n\n${p.translation_md}`;
    return `${head}\n\n### 原文\n\n${p.original_md}\n\n### 译文\n\n${p.translation_md}`;
  });
  return `${parts.join('\n\n---\n\n').trim()}\n`;
}

/**
 * 下载整篇 Markdown：**版本跟着左上角的分段控件走**（中英对照 / 纯原文 / 纯译文，用户口径）。
 * 图片是相对接口路径，补成当前服务地址后离线阅读器也能取到图。
 */
function downloadMarkdown() {
  const text = markdownFor(mdView.mode);
  if (!text.trim()) return;
  const withOrigin = text.split('](/api/v1/').join(`](${window.location.origin}/api/v1/`);
  downloadText(`${paperId.value}-${mdModeMeta.value.suffix}.md`, withOrigin);
}

/** 对照 + 原 PDF 时，右侧文字列放哪一侧（默认译文；可切原文） */
const pageSide = ref<'en' | 'zh'>('zh');

/* 正文字号：小 / 中 / 大（**默认小**），选中后记在本地（纯展示偏好，不进服务端进度）。 */
const TEXT_SIZES = { sm: 13, md: 15, lg: 17 } as const;
type TextSizeKey = keyof typeof TEXT_SIZES;
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
  pages.value, pageTranslations.value, blocks.value, showRefs.value,
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
const isEmpty = computed(() => !loading.value && !fatal.value && !pages.value.length && !allRows.value.length);
/** 原 PDF 栏的列宽（对照时只占一半，单栏时可放大） */
const pageWidth = computed(() => {
  const col = viewMode.value === 'bilingual' ? paneWidth.value / 2 - 40 : paneWidth.value - 40;
  const wanted = Math.round(Math.max(220, col) * zoom.value);
  return Math.max(200, Math.min(wanted, Math.max(220, col * 2)));
});

/* ------------------------------ 加载 ------------------------------ */
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
    stats.value = data.stats ?? stats.value;
    outline.value = data.outline ?? { items: [], tree: [], count: 0 };
    // 顶栏标签的标题换成论文名（路由只能给"论文"这个占位标题）
    setTabTitle(routeKey(route), data.paper?.title || paperId.value);
    const p = data.progress;
    viewMode.value = p?.view_mode ?? 'bilingual';
    pairOrder.value = (p?.pair_order as PairOrder) ?? 'en_left';
    // 原文栏呈现也按论文存（`text` / `page`）：上次看的是原 PDF，切标签或重开时还是原 PDF
    originalMode.value = (p?.original_mode as OriginalMode) ?? 'text';
    // 界面状态（侧栏开合 / 字号 / 显示参考文献 / 原 PDF 缩放）：**服务端那份优先**（按论文记的），
    // 没记过才用本地偏好（`localStorage`）或默认值。
    if (typeof p?.aside_open === 'boolean') asideOpen.value = p.aside_open;
    if (p?.text_size && p.text_size in TEXT_SIZES) textSize.value = p.text_size as TextSizeKey;
    if (typeof p?.show_refs === 'boolean') showRefs.value = p.show_refs;
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
    if (SIDE_TAB_KEYS.includes(String(q.side))) {
      // 兼容旧的 ?side=notes：批注与笔记已合并为一个页签
      sideTab.value = (String(q.side) === 'notes' ? 'annotations' : String(q.side)) as SideTabKey;
    }

    await nextTick();
  } catch (err) {
    fatal.value = err as Error;
  } finally {
    loading.value = false;
  }
}

/** 深链定位：`?row=<id>`（或兼容旧的 `?block=`）——必须在拿到滚动容器之后再跳。 */
async function applyDeepLink() {
  const target = (route.query.row ?? route.query.block) as string | undefined;
  if (!target || !rowById.value.has(target)) return;
  currentRowId.value = target;
  await nextTick();
  await focusRow(target, { smooth: false, save: false });
}

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

/** 公式：从页级产物的 Markdown 里提取（只读，不做复核） */
async function loadFormulas() {
  formulasLoading.value = true;
  try {
    const d = await api.get<{ items: MarkdownFormula[] }>(`/papers/${paperId.value}/formulas`);
    formulas.value = d.items ?? [];
  } catch (err) {
    console.warn('公式提取失败', err);
    formulas.value = [];
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

/* ------------------------------ 定位 ------------------------------ */
/** 「就是这一块」的短暂提醒：跳过去之后让整行亮一下再淡出（CSS 动画，见 ReaderPageRows）。 */
const flashRowId = ref('');
let flashTimer: ReturnType<typeof setTimeout> | null = null;

async function flashRow(id: string) {
  if (!id) return;
  if (flashTimer) clearTimeout(flashTimer);
  // 先摘掉类再挂上：同一行连点两次也能重新播一遍动画（直接赋同一个值不会触发重绘）
  flashRowId.value = '';
  await nextTick();
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
      show_refs: showRefs.value,
      zoom: Number(zoom.value.toFixed(2)),
      page_side: pageSide.value,
    });
  } catch (err) { console.warn('保存阅读进度失败', err); }
}
const saveProgressDebounced = debounce(saveProgressNow, 900);

/**
 * 界面状态（侧栏开合 / 字号 / 显示参考文献 / 原 PDF 缩放）一变就**立刻**存一次：
 * 它们和视图模式一样是"这篇论文上次是怎么看的"，切标签或重开时要还原（用户口径）。
 *
 * `loading` 期间不回存：那一刻正把服务端那份灌进来，回存等于自己写自己。
 * （灌值发生在 `loadAll()` 里，而它的 `await nextTick()` 正好让这个 watcher 在
 *  `loading` 还是 true 的时候跑完。）
 */
watch([asideOpen, textSize, showRefs, zoom], () => {
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

async function pollTranslations() {
  type PagePatch = { page: number; rows?: PageRow[]; align?: string | null; model?: string | null };
  // 用 `instancePaperId`：轮询可能在路由切走之后才回来，用路由值会把**另一篇**的页行
  // 打进本文档（实测表现为切标签后行数变多、内容半新半旧）。
  const d = await api.get<{ items: PagePatch[] }>(`/papers/${instancePaperId}/translations`);
  for (const item of d.items ?? []) {
    const known = pageTranslations.value.find((t) => t.page === item.page);
    if (known && known.rows.length === (item.rows ?? []).length) continue;
    applyPage(item);
  }
}

useEventStream({
  url: sseUrl(`/papers/${instancePaperId}/translations/stream`),
  events: ['page_translated', 'paper_status'],
  onEvent: (name, frame) => {
    const payload = frame?.payload ?? {};
    if (name === 'page_translated') applyPage(payload);
    else if (name === 'paper_status') {
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
  if (!window.confirm('确认取消该翻译任务？已完成的页会保留。')) return;
  try {
    await api.post(`/tasks/${task.id}/cancel`);
    toast('已请求取消（页边界生效）', 'ok');
    void fetchTasks();
  } catch (err) { reportError(err, '取消失败'); }
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
    const running = tasks.value.find((t) => t.type === 'TRANSLATE_MD'
      && ['processing', 'pending', 'paused'].includes(t.status));
    if (running) liveTask.value = running;
    else if (liveTask.value && !['processing', 'pending', 'paused'].includes(liveTask.value.status)) {
      liveTask.value = null;
      const data = await api.get<ReaderPayload>(`/papers/${instancePaperId}/reader`);
      stats.value = data.stats ?? stats.value;
      pageTranslations.value = data.page_translations ?? pageTranslations.value;
      outline.value = data.outline ?? outline.value;
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
  window.addEventListener('resize', syncPaneWidth);
  document.addEventListener('mousedown', onDragStart);
  document.addEventListener('copy', onCopy);
  document.addEventListener('mouseup', onMouseUp);
  document.addEventListener('mousemove', onDragMove);
  document.addEventListener('selectionchange', onSelectionChange);
  // 划词菜单跟着"当前视角"走：滚动 / 窗口失焦 / 点到别处就收掉（见 `onAnyScroll`）
  window.addEventListener('scroll', onAnyScroll, { capture: true, passive: true });
  window.addEventListener('blur', hideMenu);
  document.addEventListener('pointerdown', onDocPointerDown, true);

  await loadAll();
  await loadSide();
  await nextTick();
  syncPaneWidth();
  watchContentSize();
  restorePosition(reader.value?.progress ?? null);
  await applyDeepLink();
  if (route.query.md) {                       // ?md=1[&mdpage=8] 直接打开 Markdown 视图
    const want = Number(route.query.mdpage ?? 0);
    await openMarkdownView(want || undefined);   // 不给页码 = 从当前阅读页那一段开始
  }
  fetchTasks();
  taskTimer = setInterval(() => { if (activeTask.value) fetchTasks(); }, 4000);
});

onUnmounted(() => {
  mq?.removeEventListener('change', onMqChange);
  window.removeEventListener('resize', syncPaneWidth);
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
 * 正文行是**异步**来的（阅读器 payload + SSE 逐页回填），而"搜不到东西"最容易发生在
 * "打开阅读器就马上搜"：那一刻 allRows 还是空的，于是什么都不命中，界面还显示"没有匹配"。
 * 所以数据一变就按当前查询词**静默重跑**。
 */
watch(() => allRows.value.length, () => {
  if (searchQuery.value.trim()) runSearch({ silent: true });
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
  runSearchDebounced();
});
/** 改搜索范围（全部/原文/译文）也要立刻重跑，不然结果和分段控件对不上。 */
watch(searchScope, () => {
  if (searchQuery.value.trim()) runSearch({ silent: true });
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
 * 内容滚动容器（拖动时"纵向"能到哪儿）：往上找第一个可滚的祖先。
 * 左右分栏投影时 y 要按它夹 —— 按**行**的盒子夹会把"拖到工具栏/侧栏"这种手势压成空选区，
 * 而用户其实是在表达"一直选到这一列的最上面/最下面"。
 */
function scrollBox(el: HTMLElement | null): { top: number; bottom: number } {
  let p = el?.parentElement ?? null;
  while (p) {
    const cs = getComputedStyle(p);
    if (/(auto|scroll)/.test(cs.overflowY) && p.clientHeight > 80) {
      const r = p.getBoundingClientRect();
      return { top: r.top, bottom: r.bottom };
    }
    p = p.parentElement;
  }
  return { top: 0, bottom: window.innerHeight };
}

/**
 * 把指针位置**投影**回锚点那一列，得到一个候选焦点位置。
 *
 * - 左右分栏（正文的原文列/译文列、PDF 栏）：夹 **x**（夹进本列的横向范围），
 *   y 只夹进滚动容器 —— 竖着拖照样能跨行选，拖到工具栏也不会把选区压空；
 * - 上下分面（Markdown 视图的原文面/译文面）：x 不动，y 夹进本面那一段。
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
  // y 能到哪儿：上下分面（Markdown）本面就是一整段，夹在本面的盒子里；
  // 左右分栏时纵向是"整个滚动内容"，夹在行的盒子里会把"拖到工具栏"压成空选区。
  const stacked = anchorBox.matches('[data-md-side]');
  const content = stacked ? { top: a.top, bottom: a.bottom } : scrollBox(anchorBox);
  const x = sideBySide ? Math.min(Math.max(point.x, a.left + 1), a.right - 1) : point.x;
  const y = Math.min(Math.max(point.y, content.top + 1), content.bottom - 1);
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
  // 到这里是"跨列"（含"一边在内容里、另一边拖到工具栏/页号上"）：夹回本列
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

/** 这一列要不要"隐藏对面列的选区底色"（只有正文的两列需要；PDF 栏与 Markdown 视图不靠这套） */
function colSide(col: string): 'en' | 'zh' | '' {
  return col === 'rows:en' ? 'en' : col === 'rows:zh' ? 'zh' : '';
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
 */
function setColSide(side: 'en' | 'zh' | '') {
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

  /* 原 PDF 划词：文字来自 pdf.js 的文字层，**没有块锚点**，
   * 因此只提供「AI 解释 / 复制」（高亮与笔记靠 block_id + 偏移定位，映射不回去）。 */
  const pdfEl = el?.closest?.('[data-pdf-page]') as HTMLElement | null;
  if (pdfEl) {
    Object.assign(selection, {
      show: true, ...menuPos(rect), text: truncate(text, 600),
      rowId: '', side: 'en' as const, start: 0, end: 0, anchored: false,
      scope: 'row' as const, mdPage: 0, allowNote: true, segments: [], hlIds: [],
    });
    return;
  }

  /* Markdown 视图划词：锚点是 `md:p<页号>` + 相对**渲染后文字**的偏移量（见 `utils/domMarks.ts`）。
   * 这里给不了「笔记」：笔记按 `block_id` 定位回正文，而这套锚点在正文里不存在。 */
  const mdBlock = el?.closest?.('[data-md-side]') as HTMLElement | null;
  if (mdBlock) {
    const page = Number(mdBlock.closest('[data-md-page]')?.getAttribute('data-md-page') ?? 0);
    // 与 root 求交集：选区万一跨到另一面（原文/译文），至少把锚点这一面里覆盖到的部分标上，
    // 而不是"菜单都不出来"（公式就在这一面里，落空的话用户看到的就是"公式不能高亮"）。
    const got = page ? offsetsWithin(mdBlock, sel.getRangeAt(0)) : null;
    if (!page || !got) { selection.show = false; return; }
    Object.assign(selection, {
      show: true, ...menuPos(got.rect ?? rect), text: truncate(got.text || text, 600),
      rowId: mdAnchor(page), side: (mdBlock.dataset.mdSide === 'zh' ? 'zh' : 'en'),
      start: got.start, end: got.end, anchored: true,
      scope: 'markdown' as const, mdPage: page, allowNote: false,
      segments: [], hlIds: highlightIdsInRange(sel.getRangeAt(0)),
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
    rowId: parts[0].rowId, side, start: parts[0].start, end: parts[0].end,
    anchored: true, scope: 'row' as const, mdPage: 0, allowNote: true,
    segments: parts,
    hlIds: highlightIdsInRange(range, null, `rows:${side}`),
  });
}

/** 按行切开后的文字拼回来（菜单里显示的、以及要解释/复制的就是这一段） */
function joinParts(parts: Array<{ text: string }>): string {
  return parts.map((p) => p.text).filter(Boolean).join('\n').trim();
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

async function explainSelection() {
  // 无块锚点（原 PDF 划词）时只传 paper_id + 选中文字：后端 `explain_selection` 对 block_id 是可选的
  const payload = {
    paper_id: paperId.value,
    block_id: selection.anchored ? selection.rowId : null,
    selected_text: selection.text,
  };
  const anchored = selection.anchored;
  hideMenu();
  explainCard.open = true;
  explainCard.loading = true;
  explainCard.text = '';
  explainCard.rowId = anchored ? selection.rowId : '';
  explainCard.anchored = anchored;
  try {
    const data = await api.post<any>('/annotations/from-selection', payload);
    explainCard.data = data;
    explainCard.text = data.explanation ?? '(无内容)';
  } catch (err) {
    reportError(err, '划词解释失败');
    explainCard.open = false;
  } finally {
    explainCard.loading = false;
  }
}

async function saveExplanationAsAnnotation() {
  try {
    await api.post('/annotations/save', {
      paper_id: paperId.value,
      block_id: explainCard.rowId,
      content_md: explainCard.text,
      sources: explainCard.data?.sources ?? [],
      model_id: explainCard.data?.model_id,
    });
    toast('已存为批注', 'ok');
    explainCard.open = false;
    await refreshAnnotations();
  } catch (err) { reportError(err, '存为批注失败'); }
}

/* ------------------------------ 高亮 / 笔记 ------------------------------ */
/** Markdown 视图里高亮的锚点：`md:p<页号>`（`block_id` 列宽 32，够用；与正文行 id 不会撞） */
const mdAnchor = (page: number) => `md:p${page}`;

async function addHighlight(color: string) {
  const scope = selection.scope;
  // 跨行选区：每行一条记录，共用同一个分组 id（删除时整组一起删）
  const parts = scope === 'row' && selection.segments.length
    ? selection.segments
    : [{ rowId: selection.rowId, start: selection.start, end: selection.end, text: selection.text }];
  const group = parts.length > 1 ? `g-${Date.now().toString(36)}-${parts.length}` : null;
  try {
    const created: Highlight[] = [];
    for (const p of parts) {
      const blockId = scope === 'markdown' ? mdAnchor(selection.mdPage) : p.rowId;
      const data = await api.post<Highlight>('/highlights', {
        paper_id: paperId.value,
        block_id: blockId,
        // 每段存**本段自己的文字**（跨行时各行的文字不一样），校验才过得去
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
    toast(parts.length > 1 ? `已高亮（跨 ${parts.length} 行）` : '已高亮', 'ok', 2000);
  } catch (err) { reportError(err, '高亮失败'); } finally { hideMenu(); }
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

/** Markdown 视图里的高亮同样走菜单删除（弹窗传送到 body，正文上的处理器管不到） */
function onMdClick(ev: MouseEvent) {
  onContentClick(ev);
}

function openNoteModal() {
  noteModal.open = true;
  noteModal.text = '';
  noteModal.rowId = selection.rowId;
  noteModal.selected = selection.text;
  hideMenu();
}

async function saveNote() {
  try {
    await api.post('/notes', {
      paper_id: paperId.value,
      block_id: noteModal.rowId,
      selected_text: noteModal.selected,
      content_md: noteModal.text,
    });
    toast('笔记已保存', 'ok');
    noteModal.open = false;
    await refreshAnnotations();
  } catch (err) { reportError(err, '保存笔记失败'); }
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
  if (!window.confirm('删除该批注？')) return;
  try {
    await api.del(`/annotations/${id}`);
    await refreshAnnotations();
    toast('已删除', 'ok', 1800);
  } catch (err) { reportError(err, '删除失败'); }
}

async function deleteNote(id: string) {
  if (!window.confirm('删除该笔记？')) return;
  try {
    await api.del(`/notes/${id}`);
    notes.value = notes.value.filter((n) => n.id !== id);
    toast('已删除', 'ok', 1800);
  } catch (err) { reportError(err, '删除失败'); }
}

async function copySelection() {
  await copyText(selection.text);
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
  if (isNarrow.value) {
    togglePane('side');
  } else {
    asideOpen.value = !asideOpen.value;
    localStorage.setItem(asideKey, asideOpen.value ? '1' : '0');
  }
  await nextTick();
  syncPaneWidth();
}
const explainHtml = computed(() => htmlWithHighlights(explainCard.text, []));
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
               isNarrow && mobilePane === 'side' ? 'mobile-aside' : '']"
    >
      <!-- ============ 工具栏 ============ -->
      <div class="reader-toolbar">
        <!-- 最左：侧栏开合（论文标题在顶栏的标签上，这里不再重复显示）；`data-aside-toggle` 供自检脚本使用 -->
        <button
          class="btn icon"
          data-aside-toggle
          :title="asideVisible ? '收起侧栏（[）' : '展开侧栏（[）'"
          :aria-pressed="asideVisible"
          @click="toggleAside"
        >
          <AppIcon name="panel" :size="15" />
        </button>

        <div class="seg">
          <button :class="{ active: viewMode === 'original' }" @click="setViewMode('original')">原文</button>
          <button :class="{ active: viewMode === 'translation' }" @click="setViewMode('translation')">译文</button>
          <button :class="{ active: viewMode === 'bilingual' }" @click="setViewMode('bilingual')">对照</button>
        </div>

        <div v-if="originalVisible" class="seg" title="原文栏呈现">
          <button :class="{ active: originalMode === 'text' }" @click="setOriginalMode('text')">
            <AppIcon name="document" :size="14" />原文文本
          </button>
          <button :class="{ active: originalMode === 'page' }" @click="setOriginalMode('page')">
            <AppIcon name="image" :size="14" />原 PDF
          </button>
        </div>

        <!-- 对照 + 原 PDF 时，右侧文字列放译文还是原文（用户口径："对照 pdf 时有原文、译文模式"）。
             `pageLeft` 才出现：只有那时另一列才是页图，"文字列放哪一侧"才成为一个选择。 -->
        <div v-if="pageLeft" class="seg" title="页图旁的文字列">
          <button :class="{ active: pageSide === 'zh' }" @click="setPageSide('zh')">译文</button>
          <button :class="{ active: pageSide === 'en' }" @click="setPageSide('en')">原文</button>
        </div>

        <!-- 左右顺序：只放**位置图标**（原文块在左 / 原文块在右），靠 tooltip 说明；
             文字标签会把工具栏挤爆，而且这两个按钮本来就是在讲"版面位置"。 -->
        <div v-if="viewMode === 'bilingual'" class="seg" title="左右顺序">
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

        <div v-if="originalIsPage" class="row">
          <button class="btn icon" title="缩小" @click="zoom = Math.max(0.4, zoom - 0.15)">
            <AppIcon name="zoom-out" :size="15" />
          </button>
          <span class="tiny faint nowrap">{{ Math.round(zoom * 100) }}%</span>
          <button class="btn icon" title="放大" @click="zoom = Math.min(3, zoom + 0.15)">
            <AppIcon name="zoom-in" :size="15" />
          </button>
        </div>

        <div class="seg" title="正文字号">
          <button
            v-for="opt in TEXT_SIZE_OPTIONS"
            :key="opt.k"
            :class="{ active: textSize === opt.k }"
            @click="setTextSize(opt.k)"
          >{{ opt.label }}</button>
        </div>

        <span class="spacer" />

        <!-- 翻译完成时：实时状态与进度就没意义了（整篇本来就不在跑）。
             完成态只留一个绿色对勾（用户口径：不要一直占着"全部 N 页已翻译"这行字），
             完整文案放进 tooltip。 -->
        <template v-if="translateDone">
          <span class="badge ok nowrap done-badge" :title="`全部 ${stats.pages} 页已翻译`">
            <AppIcon name="check_circle" :size="13" />
          </span>
        </template>
        <template v-else>
          <!-- SSE 连得好不好是**实现细节**，连上时不再刷存在感；
               只有降级轮询 / 连接中才值得提醒一下。 -->
          <span
            v-if="sseStatus !== 'open'"
            class="badge warn nowrap"
            :title="sseStatus === 'polling' ? 'SSE 不可用，已降级轮询（4s 一次）' : 'SSE 连接中'"
          >{{ sseStatus === 'polling' ? '轮询' : '连接中' }}</span>
          <span class="badge accent nowrap">{{ stats.pages_done ?? 0 }}/{{ stats.pages ?? 0 }} 页</span>
          <span v-if="activeTask" class="badge nowrap" :title="`任务 ${activeTask.id}：${activeTask.status}`">
            翻译中
          </span>
        </template>

        <!-- 文档简报不需要开关：有就用、没有就自动生成（在左栏「术语」页签里可编辑）。
             brief=false 的参数仍保留在 API 上供脚本使用。 -->
        <button
          v-if="!translateDone"
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

        <!-- 搜索框**不在这里**：它属于"对整篇文档的操作"，已移到侧栏的「搜索」页签
             （`ReaderSearch`）。工具栏只留视图切换 / 字号 / 翻译 / 更多操作。 -->

        <!-- 低频动作（重译 / 打开 PDF / Markdown 视图）收进「⋯」，工具栏只留主次两个动作 -->
        <Menu :items="toolbarMenu" label="更多操作" size="md" @select="onToolbarAction" />
      </div>

      <!-- ============ 左栏：目录 + 批注/笔记/公式/插图/术语/任务（可整体折叠） ============ -->
      <aside class="reader-aside">
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
              <AppIcon :name="t.icon" :size="15" />
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
        <ReaderToc
          v-if="sideTab === 'toc'"
          :rows="tocRows"
          :current-row-id="tocActiveId"
          :page-count="pages.length"
          :collapsed="collapsed"
          @focus="(id: string, page?: number) => focusRow(id, { page, flash: true })"
          @toggle="toggleCollapse"
          @collapse-all="collapseAll"
          @expand-all="expandAll"
        />
        <ReaderSearch
          v-else-if="sideTab === 'search'"
          ref="searchPane"
          v-model:query="searchQuery"
          v-model:scope="searchScope"
          :hits="searchHits"
          @search="runSearch"
          @clear="clearSearch"
          @focus="(id: string, page?: number) => focusRow(id, { page, flash: true })"
        />
        <ReaderSidePanel
          v-else
          :tab="sideTab"
          :annotations="annotations"
          :notes="notes"
          :formulas="formulas"
          :formulas-loading="formulasLoading"
          :figures="figures"
          :glossary="glossary"
          :brief-draft="briefDraft"
          :brief-source="brief?.source ?? ''"
          :stats="stats"
          :progress-percent="progressPercent"
          :active-task="activeTask"
          @focus="(id: string, page?: number) => focusRow(id, { page, flash: true })"
          @copy="copyFormula"
          @zoom="zoomImage = $event"
          @save-brief="saveBrief"
          @regenerate-brief="regenerateBrief"
          @reload-brief="syncBriefDraft()"
          @delete-annotation="deleteAnnotation"
          @delete-note="deleteNote"
          @cancel-task="cancelTranslate"
        />
      </aside>

      <!-- ============ 中：正文（单一滚动容器 → 天然对齐） ============ -->
      <section ref="scroller" class="reader-content" @scroll="onScroll" @click="onContentClick">
        <ReaderPageRows
          v-model:show-refs="showRefs"
          :groups="groups"
          :mode="viewMode"
          :original-mode="originalMode"
          :page-side="pageSide"
          :pair-order="pairOrder"
          :paper-id="paperId"
          :page-width="pageWidth"
          :current-row-id="currentRowId"
          :highlights="highlights"
          :search-term="searchQuery.trim()"
          :flash-row-id="flashRowId"
          :translating="translating"
          :font-size="readerFont"
          @focus="focusRow"
          @zoom="zoomImage = $event"
          @translate-page="(p: number) => startTranslate('resume', [p])"
        />
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
      @explain="explainSelection"
      @note="openNoteModal"
      @copy="copySelection"
      @highlight="addHighlight"
      @unhighlight="deleteSelectionHighlights"
      @close="hideMenu"
    />

    <Modal :open="explainCard.open" title="AI 解释（仅依据论文内容）" @close="explainCard.open = false">
      <div v-if="explainCard.loading" class="muted">正在请求模型…</div>
      <div v-else>
        <div class="md small" v-html="explainHtml" />
        <div class="tiny faint" style="margin-top:8px">
          模型：{{ explainCard.data?.model_name ?? '—' }}
          <span v-if="explainCard.data?.cached"> · 命中缓存</span>
          <span v-if="!explainCard.anchored"> · 来自原 PDF 划词（无段落上下文与锚点）</span>
        </div>
      </div>
      <template #footer>
        <button class="btn" @click="explainCard.open = false">关闭</button>
        <button
          v-if="explainCard.anchored"
          class="btn primary"
          :disabled="explainCard.loading"
          @click="saveExplanationAsAnnotation"
        >
          存为批注
        </button>
      </template>
    </Modal>

    <Modal :open="noteModal.open" title="新建笔记" @close="noteModal.open = false">
      <div v-if="noteModal.selected" class="tiny faint">选中：{{ truncate(noteModal.selected, 180) }}</div>
      <textarea
        v-model="noteModal.text"
        rows="6"
        style="width:100%;margin-top:8px"
        placeholder="用 Markdown 写笔记；支持 $公式$"
      />
      <template #footer>
        <button class="btn" @click="noteModal.open = false">取消</button>
        <button class="btn primary" @click="saveNote">保存</button>
      </template>
    </Modal>

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
        <!-- 下载版本 = 左上角选中的那个（中英对照 / 纯原文 / 纯译文）：下载什么就是你在看什么 -->
        <button class="btn primary" :disabled="!mdView.pages.length" @click="downloadMarkdown">
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

.reader {
  display: grid;
  /* **左右栏已合并**：只有"侧栏 + 正文"两列，正文因此更宽（对照时两侧都更好读）。
     第一列用 auto：宽度由侧栏自己的 width 决定 —— width 过渡是全浏览器都支持的，
     （直接过渡 grid-template-columns 在含 minmax(0,1fr) 时 Chrome 会"启动但不插值"，实测卡死） */
  grid-template-columns: auto minmax(0, 1fr);
  /* 关键：两行必须显式声明。否则内容行会被内容撑高，侧栏自身不滚、整页跟着滚。 */
  grid-template-rows: auto minmax(0, 1fr);
  height: calc(100vh - var(--header-h));
  overflow: hidden;
}

.reader-toolbar {
  grid-column: 1 / -1; display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
  padding: 6px 10px; border-bottom: 1px solid var(--border); background: var(--bg-elev);
}
/* 翻译完成徽章：只留一个对勾（口径：不要一行"全部 N 页已翻译"占着工具栏）。
   `.badge` 默认是"图文胶囊"，这里把左右内边距换成固定 22px 见方 + 内容居中。 */
.done-badge { padding: 0; width: 22px; justify-content: center; }

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
/* ⚠️ 与正文插图同一套口径（用户口径）：
   `padding` 补一圈白边 —— 后端的裁图是按墨迹裁到边的，贴着边看就像"上下被裁掉了"；
   `max-height` 限制又高又窄的整页插图铺满整幅宽度（"部分情况下图片显示过大"）。
   max-width 与 max-height 同时给：浏览器保持宽高比缩到都满足为止，**不裁也不变形**，
   只是整张图完整地变小；`border-radius` 照旧负责圆角裁剪。 */
.md-doc :deep(img) {
  display: block; max-width: 100%; height: auto; margin: 10px auto;
  max-height: min(62vh, 780px);
  padding: 6px; box-sizing: border-box;
  background: #fff; border: 1px solid var(--border); border-radius: var(--radius-sm);
}
/* `---` 不再画线，只当一段空白（用户口径：移除分割线）；正文里模型自己写的 `---` 同理。 */
.md-doc :deep(hr) { border: 0; height: 18px; margin: 0; background: none; }
.md-doc :deep(h2) { font-size: 16px; }
/* 各栏都要 min-width/min-height: 0，否则 grid 子项不收缩、内容会顶破布局 */
.reader-aside, .reader-content { min-width: 0; min-height: 0; }
.reader-aside {
  border-right: 1px solid var(--border); background: var(--bg-elev);
  overflow: hidden; display: flex; flex-direction: column;
  width: 300px;                        /* 展开宽度（收起时过渡到 0） */
  transition: width .24s cubic-bezier(.4, 0, .2, 1), opacity .18s ease,
              border-right-width .24s ease, border-right-color .24s ease;
}
@media (min-width: 1101px) {
  /* 收起/展开期间侧栏**只被裁掉、不跟着变窄**：内容保持展开宽度。
     不这么做的话，选项卡会重排、公式（KaTeX）会逐帧重新排版 —— 既看着"变形"，
     又是卡顿的主要来源。裁切由 .reader-aside 的 overflow:hidden 完成。 */
  .reader-aside > * { min-width: 300px; }
  /* 收起：宽度收到 0 + 内容轻微左移淡出；右侧 1px 分隔线宽度与颜色一起收掉（否则会留一条竖线） */
  .reader.no-aside .reader-aside {
    width: 0; border-right-width: 0; opacity: 0; transform: translateX(-10px);
    border-right-color: transparent; pointer-events: none;
  }
}
/* 侧栏页签：**图标 + 文字**，**一行到底、不换行**；一行放不下就左右滚动（用户口径）。
 * 滚动条隐藏（保持干净），鼠标滚轮 / 触控板 / Shift+滚轮都能滚，见 `onSideTabsWheel`；
 * 切页签时把当前那个滚进可视区（`scrollIntoView`），左右两端还有圆形箭头按钮（见下）。
 * 排得比早先紧凑一些（64px → 56px、字号 10.5 → 10px），少一点"藏起来的页签"。 */
.side-tabs-wrap { position: relative; flex: none; }
/* 两枚"翻页"圆按钮：**浮在滚动条两端**、不占布局（有内容才出现，所以不会让页签跳动） */
.side-scroll-btn {
  position: absolute; top: 50%; transform: translateY(-50%); z-index: 2;
  width: 20px; height: 20px; padding: 0; border-radius: 50%;
  display: inline-flex; align-items: center; justify-content: center;
  background: var(--bg-elev); border: 1px solid var(--border-strong);
  color: var(--text-dim); box-shadow: var(--shadow-1); cursor: pointer;
}
.side-scroll-btn:hover { color: var(--accent-text); border-color: var(--accent); }
.side-scroll-btn.left { left: 3px; }
.side-scroll-btn.right { right: 3px; }
.side-tabs {
  display: flex; align-items: stretch; gap: 2px; padding: 4px 5px;
  border-bottom: 1px solid var(--divider); flex: none;
  overflow-x: auto; overflow-y: hidden;
  scrollbar-width: none;
  /* 两端各留一枚圆按钮的位置：`scrollIntoView` 落位时不会把当前页签藏在按钮底下 */
  scroll-padding: 0 24px;
}
.side-tabs::-webkit-scrollbar { display: none; }
.side-tab {
  position: relative; border: 0; background: transparent; color: var(--text-dim);
  cursor: pointer; padding: 3px 4px 2px; border-radius: var(--radius-sm);
  display: inline-flex; flex-direction: column; align-items: center; gap: 1px;
  flex: none; min-width: 56px;
}
.side-tab:hover { background: var(--bg-hover); color: var(--text); }
.side-tab.active { background: var(--accent-soft); color: var(--accent-text); }
.side-label { font-size: 10px; line-height: 1.1; white-space: nowrap; }
.side-tab.active .side-label { font-weight: 600; }
.side-count {
  position: absolute; top: 1px; right: 2px; font-size: 9.5px; line-height: 1;
  color: var(--text-faint); font-variant-numeric: tabular-nums;
}
/* 正文区自身是**唯一**的滚动容器 */
.reader-content { overflow: auto; background: var(--bg); scrollbar-width: thin; scrollbar-gutter: stable; }

@media (max-width: 1100px) {
  .reader, .reader.no-aside { grid-template-columns: minmax(0, 1fr); }
  /* 窄屏侧栏是**抽屉**：一直在 DOM 里，靠 transform 滑入滑出（display 切换没法做动画） */
  .reader-aside {
    position: fixed; top: var(--header-h); bottom: 0; z-index: 60;
    left: 0; width: min(360px, 88vw); box-shadow: var(--shadow-2);
    transform: translateX(-102%); visibility: hidden;
    transition: transform .24s cubic-bezier(.4, 0, .2, 1), visibility .24s;
  }
  .reader.mobile-aside .reader-aside { transform: none; visibility: visible; }
}

@media (prefers-reduced-motion: reduce) {
  .reader, .reader-aside { transition: none; }
}

@media print {
  .reader-toolbar, .reader-aside { display: none !important; }
  .reader, .reader-content { display: block; height: auto; overflow: visible; }
}
</style>

