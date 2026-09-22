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
import { RouterLink, useRoute } from 'vue-router';
import { api, assetUrl, sseUrl } from '@/api/client';
import type {
  Annotation, Block, BriefPayload, FigureItem, GlossaryTerm, Highlight,
  MarkdownFormula, MarkdownViewMode, Note, OriginalMode, Outline, PageInfo, PageRow,
  PageTranslation, PairOrder, ReaderPayload, ReaderStats, TaskInfo,
  TranslatedMarkdownPage, ViewMode,
} from '@/api/types';
import { useEventStream, type StreamStatus } from '@/composables/useEventStream';
import { useHotkeys } from '@/composables/useHotkeys';
import { htmlWithHighlights, mdToHtml } from '@/composables/useMarkdown';
import { buildPageGroups, flattenRows } from '@/composables/usePageRows';
import { reportError, toast, withLoading } from '@/stores/app';
import {
  copyText, debounce, downloadText, flattenOutline, outlineParentIds, pct, truncate,
} from '@/utils/format';
import AppIcon from '@/components/AppIcon.vue';
import Modal from '@/components/Modal.vue';
import ReaderPageRows from '@/components/reader/ReaderPageRows.vue';
import ReaderSidePanel, { type BriefDraft, type SideTab } from '@/components/reader/ReaderSidePanel.vue';
import ReaderToc from '@/components/reader/ReaderToc.vue';
import SelectionMenu from '@/components/reader/SelectionMenu.vue';

const HIGHLIGHT_COLORS = ['yellow', 'green', 'blue', 'pink'];
const route = useRoute();
const paperId = computed(() => String(route.params.paperId ?? ''));

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
const searchHits = ref<Array<{ id: string; page: number; side: string; snippet: string }>>([]);
const mobilePane = ref<'' | 'side'>('');
const isNarrow = ref(false);
const paneWidth = ref(1200);
const useBrief = ref(true);

const selection = reactive({
  show: false, x: 0, y: 0, text: '', rowId: '', start: 0, end: 0, side: 'en' as 'en' | 'zh',
});
const explainCard = reactive({ open: false, loading: false, text: '', data: null as any, rowId: '' });
const noteModal = reactive({ open: false, text: '', rowId: '', selected: '' });
const zoomImage = ref('');
const briefDraft = reactive<BriefDraft>({ domain: '', style: '', notes: '', glossaryText: '' });

/** 侧栏（左栏，与目录合并）：目录 + 批注·笔记 / 公式 / 插图 / 术语 / 任务 */
const sideTab = ref<'toc' | SideTab>('toc');
const SIDE_TAB_KEYS = ['toc', 'annotations', 'notes', 'formulas', 'figures', 'terms', 'tasks'];
const SIDE_TABS = computed<Array<{
  k: 'toc' | SideTab; icon: string; label: string; title: string; count?: number;
}>>(() => [
  { k: 'toc', icon: 'list', label: '目录', title: '目录（中英双语）' },
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
  { k: 'terms', icon: 'layers', label: '术语', title: '术语与文档简报', count: glossary.value.length },
  { k: 'tasks', icon: 'task', label: '任务', title: '任务与进度' },
]);

/* Markdown 视图：直接看**带插图**的双语 Markdown（模型产物 + 后端插入的图片行）。 */
const mdView = reactive({
  open: false,
  loading: false,
  page: 1,
  mode: 'bilingual' as MarkdownViewMode,
  pages: [] as TranslatedMarkdownPage[],
  combined: '',
});

async function openMarkdownView() {
  mdView.open = true;
  mdView.page = currentPage.value || 1;
  if (mdView.pages.length) return;
  mdView.loading = true;
  try {
    const d = await api.get<{ pages: TranslatedMarkdownPage[]; markdown: string }>(
      `/papers/${paperId.value}/markdown/translated`);
    mdView.pages = d.pages ?? [];
    mdView.combined = d.markdown ?? '';
  } catch (err) {
    reportError(err, '加载 Markdown 失败');
    mdView.open = false;
  } finally {
    mdView.loading = false;
  }
}

const mdPage = computed(() => mdView.pages.find((p) => p.page === mdView.page) ?? null);
const mdHtml = computed(() => {
  const p = mdPage.value;
  if (!p) return '';
  if (mdView.mode === 'original') return mdToHtml(p.original_md);
  if (mdView.mode === 'translation') return mdToHtml(p.translation_md);
  // 上下对照 = 两份完整 Markdown：**各自都带图**（读译文那一半也能看到插图）
  return mdToHtml(`${p.original_md}\n\n---\n\n${p.translation_md}`);
});
const mdPages = computed(() => mdView.pages.map((p) => p.page));

function stepMarkdownPage(delta: number) {
  const list = mdPages.value;
  const idx = list.indexOf(mdView.page);
  const next = list[Math.max(0, Math.min(list.length - 1, (idx < 0 ? 0 : idx) + delta))];
  if (next) mdView.page = next;
}

/** 下载整篇双语 Markdown：把图片相对接口路径补成当前服务地址，离线阅读器也能取到图。 */
function downloadMarkdown() {
  if (!mdView.combined) return;
  const text = mdView.combined.split('](/api/v1/').join(`](${window.location.origin}/api/v1/`);
  downloadText(`${paperId.value}-bilingual.md`, text);
}

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
const progressPercent = computed(() => pct(stats.value.pages_done ?? 0, stats.value.pages ?? 0));
const originalVisible = computed(() => viewMode.value !== 'translation');
const originalIsPage = computed(() => originalVisible.value && originalMode.value === 'page');
const leftIsOriginal = computed(() => pairOrder.value !== 'zh_left' && pairOrder.value !== 'zh_top');
const tocRows = computed(() => flattenOutline(outline.value.tree, new Set(collapsed.value)));
const activeTask = computed(() => liveTask.value
  ?? tasks.value.find((t) => t.type === 'TRANSLATE_MD'
    && ['processing', 'pending', 'paused'].includes(t.status))
  ?? null);
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

    const p = data.progress;
    viewMode.value = p?.view_mode ?? 'bilingual';
    pairOrder.value = (p?.pair_order as PairOrder) ?? 'en_left';
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
    if (q.size === 'sm' || q.size === 'md' || q.size === 'lg') setTextSize(q.size);
    if (SIDE_TAB_KEYS.includes(String(q.side))) {
      // 兼容旧的 ?side=notes：批注与笔记已合并为一个页签
      sideTab.value = (String(q.side) === 'notes' ? 'annotations' : String(q.side)) as 'toc' | SideTab;
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
/**
 * 定位到某一行（或退化为某一页）。
 *
 * `page` 是锚点失效时的兜底：批注/笔记/插图锚点可能是**历史产物**里的行号
 * （重译后行号会变），这时按页跳转而不是乱跳到第 1 页。
 */
async function focusRow(id: string, opts: { smooth?: boolean; save?: boolean; page?: number } = {}) {
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
  node?.scrollIntoView({ behavior: smooth ? 'smooth' : 'auto', block: 'center' });
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
    await api.put(`/papers/${paperId.value}/progress`, {
      last_block_id: currentRowId.value || null,
      last_page: currentPage.value,
      percent: progressPercent.value,
      scroll_ratio: Number(ratio.toFixed(4)),
      view_mode: viewMode.value,
      pair_order: pairOrder.value,
    });
  } catch (err) { console.warn('保存阅读进度失败', err); }
}
const saveProgressDebounced = debounce(saveProgressNow, 900);

/* ------------------------------ 视图切换 ------------------------------ */
async function setViewMode(mode: ViewMode) {
  if (viewMode.value === mode) return;
  viewMode.value = mode;
  mobilePane.value = '';
  await nextTick();
  syncPaneWidth();
  void saveProgressNow();
  void focusRow(currentRowId.value, { smooth: false, save: false });
}

async function setPairOrder(order: PairOrder) {
  if (pairOrder.value === order) return;
  pairOrder.value = order;
  await nextTick();
  void saveProgressNow();
  void focusRow(currentRowId.value, { smooth: false, save: false });
}

function setOriginalMode(mode: OriginalMode) {
  if (originalMode.value === mode) return;
  originalMode.value = mode;
  zoom.value = 1;
  nextTick(() => { syncPaneWidth(); void focusRow(currentRowId.value, { smooth: false, save: false }); });
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

function measureRows() {
  const el = scroller.value;
  if (!el) return;
  const base = el.getBoundingClientRect().top - el.scrollTop;
  offsets = Array.from(el.querySelectorAll<HTMLElement>('[data-row-id]'))
    .map((node) => ({ id: node.dataset.rowId ?? '', top: node.getBoundingClientRect().top - base }))
    .filter((o) => o.id);
  offsetsHeight = el.scrollHeight;
}

function onScroll() {
  const el = scroller.value;
  if (!el) return;
  if (!offsets.length || Math.abs(offsetsHeight - el.scrollHeight) > 2) measureRows();
  const y = el.scrollTop + 60;
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

/** 行模型/视图形态/字号变化后让偏移缓存失效（行高会变） */
watch([groups, viewMode, originalMode, pairOrder, zoom, textSize], () => { offsetsHeight = -1; });

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
  const d = await api.get<{ items: PagePatch[] }>(`/papers/${paperId.value}/translations`);
  for (const item of d.items ?? []) {
    const known = pageTranslations.value.find((t) => t.page === item.page);
    if (known && known.rows.length === (item.rows ?? []).length) continue;
    applyPage(item);
  }
}

useEventStream({
  url: sseUrl(`/papers/${paperId.value}/translations/stream`),
  events: ['page_translated', 'block_translated', 'paper_status'],
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
async function startTranslate(mode: 'resume' | 'retranslate' = 'resume', pages?: number[]) {
  try {
    const task = await api.post<TaskInfo>(`/papers/${paperId.value}/translate`, {
      mode, brief: useBrief.value, pages,
      force: mode === 'retranslate',
    });
    liveTask.value = task;
    toast(pages?.length ? `已创建第 ${pages.join('、')} 页的翻译任务` : '已创建逐页翻译任务', 'ok');
    void fetchTasks();
  } catch (err) { reportError(err, '触发翻译失败'); }
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
    await api.post(`/papers/${paperId.value}/parse`, {}, { force: false, vision: true });
    toast('已创建解析任务，完成后刷新本页', 'ok');
  } catch (err) { reportError(err, '触发解析失败'); }
}

async function fetchTasks() {
  try {
    const d = await api.get<{ items: TaskInfo[] }>('/tasks', { paper_id: paperId.value, limit: 20 });
    tasks.value = d.items ?? [];
    const running = tasks.value.find((t) => t.type === 'TRANSLATE_MD'
      && ['processing', 'pending', 'paused'].includes(t.status));
    if (running) liveTask.value = running;
    else if (liveTask.value && !['processing', 'pending', 'paused'].includes(liveTask.value.status)) {
      liveTask.value = null;
      const data = await api.get<ReaderPayload>(`/papers/${paperId.value}/reader`);
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
  nextTick(() => syncPaneWidth());
}

onMounted(async () => {
  mq = window.matchMedia('(max-width: 1100px)');
  isNarrow.value = mq.matches;
  mq.addEventListener('change', onMqChange);
  window.addEventListener('resize', syncPaneWidth);
  document.addEventListener('mouseup', onMouseUp);

  await loadAll();
  await loadSide();
  await nextTick();
  syncPaneWidth();
  restorePosition(reader.value?.progress ?? null);
  await applyDeepLink();
  if (route.query.md) {                       // ?md=1[&mdpage=8] 直接打开 Markdown 视图
    const want = Number(route.query.mdpage ?? 0);
    await openMarkdownView();
    if (want && mdPages.value.includes(want)) mdView.page = want;
  }
  fetchTasks();
  taskTimer = setInterval(() => { if (activeTask.value) fetchTasks(); }, 4000);
});

onUnmounted(() => {
  mq?.removeEventListener('change', onMqChange);
  window.removeEventListener('resize', syncPaneWidth);
  document.removeEventListener('mouseup', onMouseUp);
  if (taskTimer) clearInterval(taskTimer);
  saveProgressDebounced.cancel();
  void saveProgressNow();
});

/* ------------------------------ 搜索 ------------------------------ */
function runSearch() {
  const q = searchQuery.value.trim().toLowerCase();
  if (!q) { searchHits.value = []; return; }
  const hits: typeof searchHits.value = [];
  for (const r of allRows.value) {
    const en = (r.original ?? '').toLowerCase();
    const zh = (r.translation ?? '').toLowerCase();
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
  if (!hits.length) toast('没有匹配的段落', 'warn', 2400);
}
function clearSearch() { searchQuery.value = ''; searchHits.value = []; }

/* ------------------------------ 划词 ------------------------------ */
function onMouseUp() {
  const sel = window.getSelection();
  if (!sel || sel.isCollapsed) { selection.show = false; return; }
  const text = sel.toString().trim();
  if (!text) { selection.show = false; return; }
  const node = sel.anchorNode;
  const el = (node && node.nodeType === 3 ? node.parentElement : node) as HTMLElement | null;
  const rowEl = el?.closest?.('[data-row-id]') as HTMLElement | null;
  if (!rowEl?.dataset.rowId) { selection.show = false; return; }

  const rowId = rowEl.dataset.rowId;
  const row = rowById.value.get(rowId);
  const side = (el?.closest('[data-side]') as HTMLElement | null)?.dataset.side === 'zh' ? 'zh' : 'en';
  const source = side === 'zh' ? (row?.translation ?? '') : (row?.original ?? '');
  const at = source.indexOf(text);
  const rect = sel.getRangeAt(0).getBoundingClientRect();
  Object.assign(selection, {
    show: true,
    x: Math.min(window.innerWidth - 320, Math.max(8, rect.left)),
    y: Math.max(8, rect.top - 46),
    text: truncate(text, 600),
    rowId, side,
    start: at >= 0 ? at : 0,
    end: at >= 0 ? at + text.length : text.length,
  });
}

const hideMenu = () => { selection.show = false; };

async function explainSelection() {
  const payload = { paper_id: paperId.value, block_id: selection.rowId, selected_text: selection.text };
  hideMenu();
  explainCard.open = true;
  explainCard.loading = true;
  explainCard.text = '';
  explainCard.rowId = selection.rowId;
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

async function addHighlight(color: string) {
  try {
    const data = await api.post<Highlight>('/highlights', {
      paper_id: paperId.value,
      block_id: selection.rowId,
      selected_text: selection.text,
      start_offset: selection.start,
      end_offset: selection.end,
      color,
    });
    highlights.value.push({ ...data, block_id: selection.rowId, start: data.start, end: data.end });
    toast('已高亮', 'ok', 2000);
  } catch (err) { reportError(err, '高亮失败'); } finally { hideMenu(); }
}

async function removeHighlight(id: string) {
  if (!window.confirm('删除该高亮？')) return;
  try {
    await api.del(`/highlights/${id}`);
    highlights.value = highlights.value.filter((h) => h.id !== id);
    toast('已删除高亮', 'ok', 1800);
  } catch (err) { reportError(err, '删除高亮失败'); }
}

function onContentClick(ev: MouseEvent) {
  const mark = (ev.target as HTMLElement).closest?.('[data-hl]') as HTMLElement | null;
  if (mark?.dataset.hl) void removeHighlight(mark.dataset.hl);
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

async function copyRow(payload: { row: PageRow; side: 'en' | 'zh' }) {
  await copyText(payload.side === 'zh' ? (payload.row.translation ?? '') : (payload.row.original ?? ''));
  toast('已复制', 'ok', 1600);
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
watch(brief, (b) => {
  if (!b) return;
  briefDraft.domain = b.domain ?? '';
  briefDraft.style = b.style ?? '';
  briefDraft.notes = Array.isArray(b.notes) ? b.notes.join('\n') : String(b.notes ?? '');
  briefDraft.glossaryText = Object.entries(b.glossary ?? {}).map(([k, v]) => `${k}=${v}`).join('\n');
}, { immediate: true });

async function regenerateBrief() {
  try {
    await withLoading('brief', () => api.post(`/papers/${paperId.value}/brief`, { force: true }));
    const b = await api.get<{ brief: BriefPayload | null }>(`/papers/${paperId.value}/brief`);
    brief.value = b.brief ?? null;
    toast('简报已重新生成', 'ok');
  } catch (err) { reportError(err, '生成简报失败'); }
}

async function saveBrief() {
  const glossaryMap: Record<string, string> = {};
  for (const line of briefDraft.glossaryText.split('\n')) {
    const [k, ...rest] = line.split('=');
    if (k && rest.length) glossaryMap[k.trim()] = rest.join('=').trim();
  }
  try {
    await api.post(`/papers/${paperId.value}/brief`, {
      apply: {
        domain: briefDraft.domain,
        style: briefDraft.style,
        notes: briefDraft.notes.split('\n').filter(Boolean),
        glossary: glossaryMap,
      },
    });
    toast('简报与术语已保存（后续翻译按此术语表）', 'ok');
    const gl = await api.get<{ items: GlossaryTerm[] }>(`/papers/${paperId.value}/glossary`);
    glossary.value = gl.items ?? [];
  } catch (err) { reportError(err, '保存简报失败'); }
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
  '/': () => { (document.querySelector('input[placeholder="搜索"]') as HTMLInputElement | null)?.focus(); },
  Escape: () => { hideMenu(); mobilePane.value = ''; },
}, () => !loading.value && !fatal.value);

/* ------------------------------ 其它 ------------------------------ */
const openPdf = () => window.open(assetUrl(`/api/v1/papers/${paperId.value}/file`), '_blank');
const togglePane = (name: 'side') => { mobilePane.value = mobilePane.value === name ? '' : name; };
const explainHtml = computed(() => htmlWithHighlights(explainCard.text, []));
</script>

<template>
  <div>
    <div v-if="loading" class="empty">正在加载阅读器数据…</div>

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
        <p class="muted small">阅读器依赖解析产出（页图 + 页级翻译）。</p>
        <div class="row">
          <button class="btn primary" @click="triggerParse">
            <AppIcon name="bolt" :size="14" />解析这篇论文
          </button>
          <RouterLink class="btn" :to="{ name: 'library' }">返回论文库</RouterLink>
        </div>
      </div>
    </div>

    <div
      v-else
      :class="['reader',
               isNarrow ? 'no-aside' : '',
               isNarrow && mobilePane === 'side' ? 'mobile-aside' : '']"
    >
      <!-- ============ 工具栏 ============ -->
      <div class="reader-toolbar">
        <RouterLink class="btn icon sm" :to="{ name: 'library' }" title="返回论文库">
          <AppIcon name="back" :size="14" />
        </RouterLink>
        <span class="small truncate doc-title" :title="reader?.paper.title">
          {{ reader?.paper.title }}
        </span>

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

        <div v-if="viewMode === 'bilingual'" class="seg" title="左右顺序">
          <button :class="{ active: leftIsOriginal }" @click="setPairOrder('en_left')">原文在左</button>
          <button :class="{ active: !leftIsOriginal }" @click="setPairOrder('zh_left')">译文在左</button>
        </div>

        <div v-if="originalIsPage" class="row">
          <button class="btn icon sm" title="缩小" @click="zoom = Math.max(0.4, zoom - 0.15)">
            <AppIcon name="zoom-out" :size="14" />
          </button>
          <span class="tiny faint nowrap">{{ Math.round(zoom * 100) }}%</span>
          <button class="btn icon sm" title="放大" @click="zoom = Math.min(3, zoom + 0.15)">
            <AppIcon name="zoom-in" :size="14" />
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

        <span
          class="badge"
          :class="sseStatus === 'open' ? 'ok' : (sseStatus === 'polling' ? 'warn' : '')"
          :title="sseStatus === 'open' ? 'SSE 已连接：逐页译文实时上线' : 'SSE 不可用，已降级轮询'"
        >{{ sseStatus === 'open' ? '实时' : (sseStatus === 'polling' ? '轮询' : '连接中') }}</span>
        <span class="badge accent nowrap">{{ stats.pages_done ?? 0 }}/{{ stats.pages ?? 0 }} 页</span>

        <label class="tiny faint row nowrap" style="gap:4px" title="文档简报：翻译前固化术语">
          <input v-model="useBrief" type="checkbox"> 简报
        </label>
        <button class="btn primary sm" @click="startTranslate('resume')">
          <AppIcon name="play" :size="14" />继续翻译
        </button>
        <button class="btn sm" @click="startTranslate('retranslate')">
          <AppIcon name="refresh" :size="14" />重译
        </button>
        <button v-if="activeTask" class="btn sm danger" @click="cancelTranslate(activeTask)">
          <AppIcon name="stop" :size="14" />停止
        </button>
        <button class="btn icon sm" title="在新标签打开原 PDF" @click="openPdf">
          <AppIcon name="external" :size="14" />
        </button>
        <button class="btn icon sm" title="Markdown 视图（含插图）" @click="openMarkdownView">
          <AppIcon name="document" :size="14" />
        </button>

        <div v-if="isNarrow" class="row">
          <button class="btn icon sm" title="侧栏（目录 / 批注 / 公式 …）" @click="togglePane('side')">
            <AppIcon name="panel" :size="14" />
          </button>
        </div>

        <input v-model="searchQuery" class="small search" placeholder="搜索" @keyup.enter="runSearch">
      </div>

      <!-- ============ 左栏：目录 + 批注/笔记/公式/插图/术语/任务（合并成一栏） ============ -->
      <aside class="reader-aside">
        <div class="side-tabs">
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
        <ReaderToc
          v-if="sideTab === 'toc'"
          :rows="tocRows"
          :current-row-id="currentRowId"
          :page-count="pages.length"
          :collapsed="collapsed"
          :search-hits="searchHits"
          @focus="focusRow"
          @toggle="toggleCollapse"
          @collapse-all="collapseAll"
          @expand-all="expandAll"
          @clear-search="clearSearch"
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
          :stats="stats"
          :progress-percent="progressPercent"
          :active-task="activeTask"
          @focus="(id: string, page?: number) => focusRow(id, { page })"
          @copy="copyFormula"
          @zoom="zoomImage = $event"
          @save-brief="saveBrief"
          @regenerate-brief="regenerateBrief"
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
          :pair-order="pairOrder"
          :paper-id="paperId"
          :page-width="pageWidth"
          :current-row-id="currentRowId"
          :highlights="highlights"
          :font-size="readerFont"
          @focus="focusRow"
          @copy="copyRow"
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
      @explain="explainSelection"
      @note="openNoteModal"
      @copy="copySelection"
      @highlight="addHighlight"
      @close="hideMenu"
    />

    <Modal :open="explainCard.open" title="AI 解释（仅依据论文内容）" @close="explainCard.open = false">
      <div v-if="explainCard.loading" class="muted">正在请求模型…</div>
      <div v-else>
        <div class="md small" v-html="explainHtml" />
        <div class="tiny faint" style="margin-top:8px">
          模型：{{ explainCard.data?.model_name ?? '—' }}
          <span v-if="explainCard.data?.cached"> · 命中缓存</span>
        </div>
      </div>
      <template #footer>
        <button class="btn" @click="explainCard.open = false">关闭</button>
        <button class="btn primary" :disabled="explainCard.loading" @click="saveExplanationAsAnnotation">
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

    <Modal :open="!!zoomImage" title="插图" wide @close="zoomImage = ''">
      <img v-if="zoomImage" :src="zoomImage" style="max-width:100%;background:#fff">
    </Modal>

    <Modal :open="mdView.open" title="Markdown（含插图）" wide @close="mdView.open = false">
      <div class="md-bar">
        <div class="seg">
          <button
            v-for="m in [{ k: 'bilingual', label: '上下对照' }, { k: 'original', label: '原文' }, { k: 'translation', label: '译文' }]"
            :key="m.k"
            :class="{ active: mdView.mode === m.k }"
            @click="mdView.mode = m.k as MarkdownViewMode"
          >{{ m.label }}</button>
        </div>
        <span class="spacer" />
        <button class="btn icon sm" title="上一页" @click="stepMarkdownPage(-1)">
          <AppIcon name="chevron-left" :size="14" />
        </button>
        <span class="tiny faint nowrap">第 {{ mdView.page }} 页 / {{ mdPages.length }}</span>
        <button class="btn icon sm" title="下一页" @click="stepMarkdownPage(1)">
          <AppIcon name="chevron-right" :size="14" />
        </button>
      </div>
      <div v-if="mdView.loading" class="muted">正在加载 Markdown…</div>
      <div v-else-if="!mdPage" class="muted">这一页还没有翻译产物。</div>
      <div v-else class="md-doc md" v-html="mdHtml" />
      <template #footer>
        <button class="btn" @click="mdView.open = false">关闭</button>
        <button class="btn primary" :disabled="!mdView.combined" @click="downloadMarkdown">
          <AppIcon name="download" :size="14" />下载整篇 Markdown
        </button>
      </template>
    </Modal>
  </div>
</template>

<style scoped>
.reader {
  display: grid;
  /* **左右栏已合并**：只有"侧栏 + 正文"两列，正文因此更宽（对照时两侧都更好读） */
  grid-template-columns: 300px minmax(0, 1fr);
  /* 关键：两行必须显式声明。否则内容行会被内容撑高，侧栏自身不滚、整页跟着滚。 */
  grid-template-rows: auto minmax(0, 1fr);
  height: calc(100vh - var(--header-h));
  overflow: hidden;
}
.reader.no-aside { grid-template-columns: minmax(0, 1fr); }

.reader-toolbar {
  grid-column: 1 / -1; display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
  padding: 6px 10px; border-bottom: 1px solid var(--border); background: var(--bg-elev);
}
.doc-title { max-width: 220px; font-weight: 600; }
.search { width: 120px; }

/* Markdown 视图：正文宽一点、插图按容器宽度自适应 */
.md-bar { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; }
.md-doc {
  max-height: 68vh; overflow: auto; padding: 4px 2px;
  border-top: 1px solid var(--divider); font-size: 14px; line-height: 1.75;
}
.md-doc :deep(img) {
  display: block; max-width: 100%; height: auto; margin: 10px auto;
  background: #fff; border: 1px solid var(--border); border-radius: var(--radius-sm);
}
.md-doc :deep(hr) { border: 0; border-top: 1px solid var(--divider); margin: 14px 0; }
.md-doc :deep(h2) { font-size: 16px; }
/* 各栏都要 min-width/min-height: 0，否则 grid 子项不收缩、内容会顶破布局 */
.reader-aside, .reader-content { min-width: 0; min-height: 0; }
.reader-aside {
  border-right: 1px solid var(--border); background: var(--bg-elev);
  overflow: hidden; display: flex; flex-direction: column;
}
/* 侧栏页签：**图标 + 文字**（只用图标不直观）；窄栏时自动两行排布 */
.side-tabs {
  display: flex; align-items: stretch; gap: 2px; padding: 4px 5px;
  border-bottom: 1px solid var(--divider); flex: none; flex-wrap: wrap;
}
.side-tab {
  position: relative; border: 0; background: transparent; color: var(--text-dim);
  cursor: pointer; padding: 4px 6px 3px; border-radius: var(--radius-sm);
  display: inline-flex; flex-direction: column; align-items: center; gap: 2px;
  min-width: 44px; flex: 1 1 auto;
}
.side-tab:hover { background: var(--bg-hover); color: var(--text); }
.side-tab.active { background: var(--accent-soft); color: var(--accent-text); }
.side-label { font-size: 10.5px; line-height: 1.1; white-space: nowrap; }
.side-tab.active .side-label { font-weight: 600; }
.side-count {
  position: absolute; top: 1px; right: 2px; font-size: 9.5px; line-height: 1;
  color: var(--text-faint); font-variant-numeric: tabular-nums;
}
/* 正文区自身是**唯一**的滚动容器 */
.reader-content { overflow: auto; background: var(--bg); scrollbar-width: thin; }

@media (max-width: 1100px) {
  .reader, .reader.no-aside { grid-template-columns: minmax(0, 1fr); }
  .reader-aside { display: none; }
  .reader.mobile-aside .reader-aside {
    display: flex; flex-direction: column; position: fixed; top: var(--header-h); bottom: 0; z-index: 60;
    left: 0; width: min(360px, 88vw); box-shadow: var(--shadow-2);
  }
}

@media print {
  .reader-toolbar, .reader-aside { display: none !important; }
  .reader, .reader-content { display: block; height: auto; overflow: visible; }
}
</style>
