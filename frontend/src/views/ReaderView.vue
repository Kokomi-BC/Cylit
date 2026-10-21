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
import { computed, nextTick, onBeforeUnmount, onMounted, onUnmounted, reactive, ref, watch, watchEffect } from 'vue';
import { clearDocSelection, lockFloatSelection, releaseFloatSelection } from '@/utils/floatDrag';
import type { CSSProperties } from 'vue';
import { RouterLink, useRoute, useRouter } from 'vue-router';
import { api, assetUrl, downloadBlob, sseUrl } from '@/api/client';
import { postSse } from '@/api/stream';
import type {
  AgentSelection, Annotation, Block, BriefPayload, FigureItem, GlossaryTerm,
  Highlight, MarkdownFormula, MarkdownTable, MarkdownViewMode, Note, OriginalMode, Outline, OverlayAnchor,
  PageInfo, PageRow,
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
import { highlightCss } from '@/utils/highlightColors';
import { buildPageGroups, flattenRows } from '@/composables/usePageRows';
import { taskTypeLabel } from '@/utils/tasks';
import { confirmRegenerateInsight } from '@/utils/understanding';
import { READER_TEXT_SIZES, type ReaderTextSize } from '@/utils/readerFont';
import { addReaderSeconds, noteReaderOpen } from '@/utils/readerStats';
import { reportError, toast, withLoading } from '@/stores/app';
import { openTab, routeKey, setTabTitle } from '@/stores/tabs';
import {
  INK_COLORS, MARKER_COLORS, activeInkColor, canRedo, canUndo, clearPageInk, colorFor, lastDrawTool,
  flushInk, inkKey, inkOn, inkTool, inkView, inkWidth, pageKey,
  redoInk, resetInk, persistInkPrefs, setInkColor, setInkNotesListener, setInkWidth, undoInk,
} from '@/composables/useReaderInk';
import { WIDTH_STEPS } from '@/utils/imageInk';
import {
  copyText, debounce, flattenOutline, fmtRelative, outlineParentIds, pct, saveBlob, truncate,
} from '@/utils/format';
import AppIcon from '@/components/AppIcon.vue';
import Modal from '@/components/Modal.vue';
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
import BlockOverlay from '@/components/reader/BlockOverlay.vue';
import { keepPdfSelectionOnce } from '@/components/reader/PdfPage.vue';
import M3Slider from '@/components/M3Slider.vue';
import Menu from '@/components/Menu.vue';
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
/** 表格（与公式同源：页级产物）；**没有解析产物兜底** —— 解析那边的 table 块是坐标拼的纯文本 */
const tables = ref<MarkdownTable[]>([]);
const tablesLoading = ref(false);
const figures = ref<FigureItem[]>([]);

/* ---------------------- 引用标号的「速览卡」（悬浮标号就地在旁边看一眼） ----------------------
 * 用户口径 2026-12-03：对话页 / 解读页里悬浮引用标号，旁边弹一张卡片显示它指向的内容
 * （正文 / 表格 / 插图），**点卡片 = 定位到原文**；点标号本身的跳转逻辑一点没动。
 * 复用 FloatingCard：拖动 / 收起 / 焦点层 / 字号 全跟笔记浮层同一套。
 *
 * 内容从**阅读器自己的 payload** 里取（行 / 表格 / 插图），所以三类都能出 ——
 * 引用里带的 snippet 只有一两句，且没有图表的地址，当兜底就够了。 */
type CitePeekAsk = {
  anchor: { x: number; y: number; w: number; h: number };
  page?: number | null;
  rowId?: string | null;
  blockId?: string | null;
  label?: string | null;
  /** 引用自带的一两句原文（老引用没有可映射的锚点时，卡片里至少有内容） */
  snippet?: string | null;
  /** 公式类引用的 LaTeX（解读页的标号自带；对话那侧没有） */
  latex?: string | null;
};

/** 一张速览卡。可以同时存在多张 —— 拖动过 / 点过锚定的那张会**钉住**（移开不自动关），
 *  与笔记浮层同一思路；没钉住的那张会被下一次悬浮就地复用（在标号之间横扫时很顺）。 */
type PeekCard = {
  key: string;
  ask: CitePeekAsk;
  open: boolean;
  pinned: boolean;
  /** 显示哪一侧：zh = 译文 / en = 原文（用户口径 2026-12-04：两侧同时铺太挤，给个中/EN 切换） */
  lang: 'zh' | 'en';
};

const peekCards = reactive<PeekCard[]>([]);
let peekSeq = 0;
let peekShowTimer = 0;
let peekHideTimer = 0;
/** 悬浮多久才弹（与「参考文献浮层」同一条口径：停一下才弹，扫过去不打扰） */
const PEEK_SHOW_MS = 320;
const PEEK_HIDE_MS = 240;

function loosePeekCard(): PeekCard | null {
  return peekCards.find((c) => !c.pinned) ?? null;
}

function clearPeekTimers(): void {
  if (peekShowTimer) { clearTimeout(peekShowTimer); peekShowTimer = 0; }
  if (peekHideTimer) { clearTimeout(peekHideTimer); peekHideTimer = 0; }
}

/** 一张卡的指纹：行 / 块 / 页码任一相同就算同一处引用 */
function peekSig(ask: CitePeekAsk): string {
  return [String(ask.rowId || ''), String(ask.blockId || ''), ask.page ?? ''].join('|');
}

function onCitePeekAsk(ask: CitePeekAsk): void {
  clearPeekTimers();
  /* 已经有同一处的卡（钉住的也算）：不再开第二张 —— 否则先钉住一张、再悬浮同一处，
     会叠出两张一模一样的内容（用户口径 2026-12-04）。 */
  const same = peekCards.find((c) => peekSig(c.ask) === peekSig(ask));
  if (same) { same.open = true; return; }
  const loose = loosePeekCard();
  /* 有一张没钉住的（别处的）：立刻换内容（不必每次重等 320ms） */
  if (loose) { loose.ask = ask; loose.open = true; return; }
  peekShowTimer = window.setTimeout(() => {
    peekShowTimer = 0;
    if (peekCards.some((c) => !c.pinned)) return;
    peekSeq += 1;
    peekCards.push({ key: 'peek-' + peekSeq, ask, open: true, pinned: false, lang: 'zh' });
  }, PEEK_SHOW_MS);
}

function onCitePeekHide(): void {
  if (peekShowTimer) { clearTimeout(peekShowTimer); peekShowTimer = 0; }
  if (!loosePeekCard()) return;          // 只剩钉住的：不动它
  if (peekHideTimer) clearTimeout(peekHideTimer);
  peekHideTimer = window.setTimeout(() => {
    peekHideTimer = 0;
    const i = peekCards.findIndex((c) => !c.pinned);
    if (i >= 0) peekCards.splice(i, 1);
  }, PEEK_HIDE_MS);
}

/** 鼠标进到卡片里：**只取消待收**，不钉住。
 *
 *  钉住必须留给真的动过手（按下 / 拖动）—— 与笔记浮层同一口径：那边是
 *  pointerdown 上的 onFloatPress 才锚定，鼠标扫过去不会。
 *  之前这里在 mouseenter 里就置了 pinned，于是只悬浮看了几个标号、一次都没点，
 *  卡片也会一张张被钉住（用户口径 2026-12-04）。 */
function holdCitePeek(): void {
  if (peekHideTimer) { clearTimeout(peekHideTimer); peekHideTimer = 0; }
}

/** 在卡片里按下（含拖标题栏 / 拖下角）：这才锚定 */
function pinCitePeek(card: PeekCard): void {
  holdCitePeek();
  card.pinned = true;
}

function closePeekCard(card: PeekCard): void {
  clearPeekTimers();
  const i = peekCards.indexOf(card);
  if (i >= 0) peekCards.splice(i, 1);
}

const CITE_KIND: Record<string, string> = {
  heading: '标题', para: '段落', list: '列表', table: '表格', formula: '公式',
  caption: '图注', code: '代码', ref: '参考文献', figure: '插图',
};

/** 一张卡要显示什么：整篇的行 / 表格 / 插图三选一（引用自带的 snippet 不参与） */
function peekViewOf(card: PeekCard) {
  const ask = card.ask;
  const rowId = rowAnchorId(String(ask.rowId || ''))
    || (ask.blockId ? String(reader.value?.block_rows?.[rowAnchorId(String(ask.blockId))] ?? '') : '');
  const row = rowId ? rowById.value.get(rowId) : undefined;
  const fig = rowId ? figures.value.find((f) => f.row_id === rowId) : undefined;
  const tbl = rowId ? tables.value.find((t) => t.row_id === rowId) : undefined;
  const page = row?.page ?? ask.page ?? fig?.page ?? tbl?.page ?? 0;
  const kind = tbl ? '表格' : fig ? '插图' : CITE_KIND[String(row?.type ?? '')] || '正文';
  /* 中/EN 各一份 Markdown：**两侧都走 mdToHtml**（原文里的 LaTeX 也得渲染，
     否则一整段 `$...$` 原样铺出来，就是用户说的「部分没有适配 markdown」） */
  const snippet = String(ask.snippet || '').trim();
  /* 行 / 表 / 图都取不到时用**引用自带的那一两句**兜底：
     老引用（重译前生成的）往往既没有行 id、块 id 也映射不出来，
     只显示一句「没有可显示的内容」等于这张卡白弹（用户口径 2026-12-04：标号 7 就是这种）。 */
  const zh = (tbl?.markdown || row?.translation || snippet).trim();
  const en = (tbl?.markdown || row?.original || '').trim();
  const caption = (fig?.caption_zh || fig?.caption || '').trim();
  /*
   * 插图：**图注只出现一次**（用户口径 2026-12-04：图注显示了两遍）。
   *
   * 图注行本身就是一条正文行（chunk 锚在它上面），于是图注会从两条路各来一次：
   * ① `/figures` 里的 caption（画在图的下面）；② 这一行的译文（画在正文里）。
   * 两条路取到的其实是同一句话 —— 归一化后比较，只要正文就是/包含了图注，
   * 正文那一块就不画了（否则同一段文字上下各来一遍）。
   */
  const norm = (s: string) => s.replace(/\s+/g, '').toLowerCase();
  const sameAsCaption = (text: string) => Boolean(caption) && Boolean(text)
    && (norm(text) === norm(caption) || norm(text).includes(norm(caption)));
  const bodyZh = sameAsCaption(zh) ? '' : zh;
  const bodyEn = sameAsCaption(en) ? '' : en;
  /*
   * 「引用片段」：同一行里的两条引用（例如 60=段落、61=其中的公式）会因为**落点相同**而
   * 显出完全一样的内容（用户口径 2026-12-04）。引用自带的那一段正好能区分它们：
   * 公式类 chunk 的片段就是那条公式本身，段落类 chunk 的片段则是正文开头 —— 后者已经被
   * 正文覆盖，归一化后包含就不必再画一遍。 */
  const quoteRaw = String(ask.snippet || '').trim()
    /* 公式 chunk 的片段是我们自己拼的「公式 p018-01：$$…$$」—— 那个抬头是给检索看的，
       卡片里只需要公式本身。 */
    .replace(/^公式\s*\S*?\s*[:：]\s*/, '');
  /*
   * 片段与正文的比对**要穿过 LaTeX**（用户口径 2026-12-04：卡片里内容显示了两遍）。
   *
   * 片段是 LaTeX 源码（「$$D_{\text{prod}}(Q) = \mathbb{E}\left[…」），正文里那条公式被渲染成
   * KaTeX —— 两边字面完全不同，只按空白归一化一比就"不认识"，同一条公式于是上下各来一遍。
   * 这里把两边都剥成骨架再比包含：去掉数学定界符、LaTeX 命令名、标点与空白，
   * 只留字母数字与汉字。片段被后端截断（少了收尾的 $）也不影响前缀匹配。
   */
  const core = (s: string) => s
    .replace(/\$\$?/g, ' ')
    .replace(/\\[a-zA-Z]+/g, ' ')
    .replace(/[^0-9a-zA-Z\u4e00-\u9fff]+/g, '')
    .toLowerCase();
  const quoteCore = core(quoteRaw);
  /* 骨架太短（「1/4」这种）容易被正文误中，只让够长的片段走这条判定 */
  const quoteCovered = Boolean(quoteRaw) && (
    norm(zh).includes(norm(quoteRaw)) || norm(en).includes(norm(quoteRaw))
    || (quoteCore.length >= 8 && (core(zh).includes(quoteCore) || core(en).includes(quoteCore)))
  );
  /* 解读页的标号直接带 LaTeX：比片段更准，优先用它 —— 有了它片段就是重复的第三份 */
  const latex = String(ask.latex || '').trim();
  const quote = quoteRaw && !quoteCovered && !latex ? quoteRaw : '';
  /*
   * **正文就是这条公式本身**时不要再画一遍（用户口径 2026-12-05：连续使用相同公式的问题）。
   *
   * 公式类的行常常**整行只有那条公式**，于是卡片变成「上面一条公式（来自 chunk 的 latex，
   * 带竖条底色）+ 下面同一行正文再一条」。判据：把公式骨架从正文骨架里抠掉，剩下的只有
   * 公式编号（≤2 个字符）就算「正文没有新内容」，该侧正文清空、只留那条精确的公式块；
   * 正文里还有别的句子时**照旧两段都画**（同一行里的段落 + 公式那种要靠它区分）。
   */
  const coreLatex = core(latex);
  const onlyFormula = (text: string): boolean => {
    if (!coreLatex || !text) return false;
    const body = core(text);
    if (!body) return false;
    if (body === coreLatex) return true;
    return body.includes(coreLatex) && body.replace(coreLatex, '').length <= 2;
  };
  const showZh = onlyFormula(bodyZh) ? '' : bodyZh;
  const showEn = onlyFormula(bodyEn) ? '' : bodyEn;
  return {
    title: page ? '第 ' + page + ' 页 · ' + kind : kind,
    zh: showZh,
    en: showEn,
    /* 只有原文（未翻译的页 / 只有英文的材料）时，别让中/EN 切换出现 */
    bilingual: Boolean(showZh) && Boolean(showEn) && showZh !== showEn,
    body: card.lang === 'en' ? showEn : showZh,
    imageUrl: fig?.image_url || '',
    caption,
    quote,
    latex,
    page, rowId,
    anchor: ask.anchor,
  };
}

/** 标题栏那枚「定位到原文」：与点标号走**同一条** focusRow。
 *
 *  ⚠️ 跳完**不关卡片**（用户口径 2026-12-04）：它的用法是「看完这段再回去看别处」，
 *  关掉就得重新悬浮一次。卡片本身已经自动钉住，留在原地即可。 */
function jumpFromPeek(card: PeekCard): void {
  const v = peekViewOf(card);
  card.pinned = true;
  void focusRow(v.rowId, { page: v.page || undefined, flash: true });
}
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
/** 对照模式（原文 + 译文并排）的上限：560px（用户口径 2026-11-19：上限放大一档） */
const ASIDE_W_MAX_COMPARE = 560;
/** 非对照模式的上限：960px，再与视口挂钩（不超过 72%） */
const ASIDE_W_MAX_WIDE = 960;
const ASIDE_W_DEFAULT = 300;
/** 浮窗（对话拎出来）的默认宽度：420 —— 再窄消息会被挤成竖排字、引用 chip 也放不下。
 *  （以前它同时是"点对话页签自动放宽"的目标宽度，那条逻辑已按用户口径删除。） */
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
  : Math.max(ASIDE_W_MIN, Math.min(ASIDE_W_MAX_WIDE, Math.round(winWidth.value * 0.72)))));

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

/* ------------------------------ 侧栏浮窗（对话拎出来） ------------------------------ */
/**
 * 用户口径 2026-11-19：「侧栏对话想要一个切换至浮层打开的按钮，点了就收起侧栏」。
 *
 * 为什么**复用同一块 DOM** 而不是另开一个浮层组件：换一处渲染就等于换 `ReaderChat` 实例 ——
 * 正在进行的那轮回答、会话层级、滚动位置全丢（`KeepAlive` 只在同一处缓存）。
 * 所以浮窗只是把这块面板的外框从「贴左边缘的绝对定位栏」换成「fixed 的卡片」（见 `.reader > .chat-host`），
 * 里面的东西一动不动 —— 容器一直是同一个，DOM 从不搬家。
 *
 * 与侧栏**互斥**：浮起来的同时把侧栏收起 —— 正文那一列因此回到整宽，
 * 这才是「浮层」与「侧栏」真正的差别（不收的话浮窗等于白浮，正文还是窄的）。
 */
const chatFloat = ref(false);
/** 浮窗矩形：拖标题栏只改 left/top，右下角手柄只改 w/h，两边都过 `clampFloatBox` */
const floatBox = ref({ left: 0, top: 0, w: ASIDE_W_CHAT, h: 560 });

/**
 * 三块浮层谁在最上面（用户口径 2026-11-22）：**焦点在哪张，哪张就在最上面**。
 *
 * 三块分别是笔记卡 / Cyli 解释卡（都是 FloatingCard，壳上 z-index: 60）与对话浮窗
 * （`.reader > .chat-host` 的浮窗态；停靠态是 30，不参与排序）。以前三块的顺序是死的，叠在角上时
 * 永远是最先渲染的那块被压住，用户点哪儿都改变不了。
 * 现在只在 **60 / 62 这一档里**排：62 = 有焦点，60 = 没有。
 * ⚠️ 正文 / 侧栏 / 工具栏的既有层级（30 / 31 / 40）一个都不动。
 * ⚠️ **不要挂 hover**：两张卡叠在边缘时鼠标一抖就来回改，读起来就是闪。
 */
const topLayer = ref<'note' | 'chat'>('note');
/**
 * **所有浮层共用的一条层级序列**（用户口径：笔记浮层不能永远压在下面）。
 *
 * 以前是两档固定值：笔记 / 对话浮窗在 60~62，表格公式图片浮层从 70 起 ——
 * 于是浮层一开，笔记卡**再怎么点也上不去**。现在改成一条单调递增的序列：
 * 谁新开 / 谁被点一下，就拿一个新的、更大的 `z`，谁也不占死档位。
 * 起点 60（正文 30 / 侧栏 31 / 工具栏 40 那几档一个都不动）。
 */
const layerZ = ref(60);
function nextLayerZ() { layerZ.value += 1; return layerZ.value; }
/** 对话浮窗自己的 z（开浮窗 / 点它时抬一档） */
const chatZ = ref(60);

function takeTop(which: 'note' | 'chat', key?: string) {
  /* 对话那块**只在浮窗态**参与排序：贴栏态它就是侧栏（z-index 30），不该抢浮层的顶 */
  if (which === 'chat') {
    if (!chatFloat.value) return;
    topLayer.value = 'chat';
    chatZ.value = nextLayerZ();
    return;
  }
  topLayer.value = 'note';
  /* 点的是哪张就抬哪张（多卡时不能只抬「最后一张」）；没给 key（focusin 那条）就抬当前那张 */
  const card = noteCards.find((c) => c.key === (key || activeNoteKey.value));
  if (card) card.z = nextLayerZ();
}

/** 浮窗两枚把手的位置：箱子 20px，箱子边缘比卡片边缘再出去 FLOAT_GRIP_GAP（弧就贴在卡片外面）。
 *  ⚠️ 5 与 FloatingCard.vue 的 `--fc-gap: 5px` 是**同一个数字**：三处指示器必须一样近，改一处必须改另一处。 */
const FLOAT_GRIP_SIZE = 20;
const FLOAT_GRIP_GAP = 5;
/** 拖角改大小的增益（用户口径 2026-11-21：**回到 1:1**）。
 *  2026-11-19 曾把它设成 1.5（"灵敏度高一点"），结果是"我拖了 100px，它长了 150px"——
 *  与笔记浮层的 1:1 手感不一致，用户体感就是"拖动距离与实际不符"。这里改回 1，两处同一口径。 */
const RESIZE_GAIN = 1;
/** 最小边长 + 离视口边缘的留白 */
const FLOAT_MIN_W = 320;
const FLOAT_MIN_H = 260;
const FLOAT_EDGE = 24;
/**
 * 拖侧栏宽度时的**预览值**（null = 没在拖）。
 *
 * 用户口径 2026-11-19：拖的时候只要跟手，**版面等松手再排**。为什么必须这样：`asideWidth` 挂在
 * `v-bind()` 上，写一次 = 侧栏 + 正文（几百行 + KaTeX）整块重排，逐帧写就是掉帧的根源；
 * 而 CSS 里那条 `v-bind(asideGapPx)` 还要写组件根变量、连正文宽度一起变。所以拖动期间只改这个
 * 预览值：面板宽度走行内 style、正文那一层只**平移**（transform，不重排，右端被 overflow 裁掉），
 * 松手时才把结果写回 `asideWidth` —— 一次重排。
 */
const asidePreviewW = ref<number | null>(null);
/** 面板"看上去"的宽度（拖拽中用预览值，其余时候是落盘值） */
const asideShownPx = computed(() => `${asidePreviewW.value ?? asideWidth.value}px`);
/** 拖动期间给 `<aside>` 的行内宽度；内容宽度钉在**按下那一刻**（见 `.aside-previewing`） */
const asidePreviewStyle = computed<CSSProperties>(() => (asidePreviewW.value === null
  ? {}
  : {
      width: `${asidePreviewW.value}px`,
      /* 子元素那条 `min-width` 也跟着预览值：不跟的话面板缩小时内容被撑住、右侧被裁掉一道 */
      '--aside-min-w': `${asidePreviewW.value}px`,
    }));
/**
 * 拖动期间正文那一层**完全不动**（松手才让位）。
 *
 * 试过让它跟着平移：`.reader-slide` 是整篇正文（几百行 + 公式），逐帧改 transform 在软件渲染下
 * 每帧要多花十几毫秒，而它在本级几乎没有信息量 —— 拖动时看的是那条分隔线。
 * 所以拖动期间只有"面板边框 + 把手"在跟手，正文原地不动，松手一次性让位/重排。
 */
const asidePreviewShift = computed<CSSProperties>(() => ({}));

/** 给 `<style>` 里的 `v-bind()` 用：**收起态/停靠态**的栏宽。
 *  ⚠️ 浮窗的几何**不在这条链上**（见 `applyFloatDom`）：走 `v-bind()` 的话，拖浮窗时
 *  每一帧都要把 CSS 变量写到**组件根**，整棵侧栏（几百行）跟着重算样式 —— 实测拖一下
 *  平均帧 310ms（用户报的"拖动对话浮层卡顿"）。行内 style 只让它自己那一层重算。 */
const asideWidthPx = computed(() => `${asideWidth.value}px`);


/* 浮窗的行内几何**不在这里**：见 applyFloatDom（一个直接写 DOM 的 effect）。
   绑在模板上意味着 floatBox 一变就整棵重渲染 —— 拖动时就是每帧一次。 */
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
  /* 跟着**落盘值**（不是预览值）：拖动期间正文原地不动（见 `asidePreviewShift`），
     胶囊是"居中在正文列上"的，正文不动它也不该动 —— 否则拖起来两套东西各走各的。 */
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
  // 二级菜单与它互斥：面板一开就把画笔 / 橡皮的菜单收掉，免得两层叠在一起
  if (capPanel.value) { inkOptionsOpen.value = false; eraserClearOpen.value = false; }
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
  /* 这一下"清完"之后**切回橡皮之前那支笔**（用户口径第 3 条）：清空是一次性的收尾动作，
     没必要让用户再手动点回画笔。 */
  inkTool.value = lastDrawTool.value;
  void nextTick().then(() => { syncToastDrop(); });
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
  /* 进橡皮之前记一下拿的是哪支笔 —— 擦完一笔自动切回它（用户口径第 3 条） */
  if (key === 'eraser' && (inkTool.value === 'pen' || inkTool.value === 'marker')) {
    lastDrawTool.value = inkTool.value;
  }
  inkTool.value = key;
  inkOptionsOpen.value = key === 'pen' || key === 'marker';
  eraserClearOpen.value = false;
  persistInkPrefs();          // 记住这次拿的是哪支笔 / 荧光笔
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
  /* 浮窗态也算"看得见"：它开着的时候 `asideOpen` 仍然是 true（占位面板在那一栏里），
     所以正常情况上面那条 `asideVisible` 就已经早退了。这里再挡一道，
     免得将来有人把占位面板去掉之后，`watch(asideOpen)` 又顺手把浮窗收掉
     （用户看到的就是"点一下「加入对话」，浮层样式没了"，用户口径 2026-11-19）。 */
  if (chatFloat.value) return;
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
 * `?side=insight` 深链也只会"选中页签"，**不改侧栏宽度**（用户口径 2026-11-19：宽度只由用户拖）。
 */
async function openInsightTab() {
  await ensureAsideVisible();
  railTab.value = 'insight';
}

/* ⚠️ 点「对话 / 解读」页签**不再自动加宽侧栏**（用户口径 2026-11-19）：宽度只由用户自己拖，
   点个页签就变宽会让人"我怎么一进来版面就变了"。原来那几条 `widenForInsight` / `widenForChat`
   与挂在 `watch(railTab)` 上的钩子一并删掉；默认宽度仍是 `ASIDE_W_DEFAULT`（300），
   想要宽一点就直接拖把手（上限已放大到 560 / 960）。 */

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

/** 顶部工具条右端只剩两枚动作：下载打包 / 全屏。
 *  2026-11-19 用户口径：撤掉「Markdown 视图」与「重新翻译」两个入口（重译的后台任务照旧跑完，
 *  只是不再从这里发起 —— 想重跑走「继续翻译」；Markdown 视图仍可从正文里的 `md:p<页号>`
 *  锚点进入），全屏从「⋯」里搬到顶部，并换成对角线箭头。 */
/** 右上角「⋯」里只剩下载这一项：两个动作都摆在外面时，一块 76px 的胶囊上并着
 *  两枚图标，看起来像"两个主操作抢位置"；收进一层，常用度低的那个不再占位。 */
const headMenu = [
  { key: 'download', label: '下载打包', icon: 'download' },
  { key: 'prefs', label: '首选项', icon: 'settings' },
];

function onHeadAction(key: string): void {
  if (key === 'download') { downloadBundle(); return; }
  // 首选项 = 设置页。设置是**导航页**（不进标签条），所以直接推路由 —— 不经过 `openTab`，
  // 那条路现在只登记论文类路由（见 stores/tabs.ts 的 isPaperRoute）。
  if (key === 'prefs') void router.push({ name: 'settings' });
}

const isFullscreen = ref(false);
/** 沉浸模式：浏览器不支持 / 拒绝全屏时的退路（隐藏顶部标签栏，阅读器占满整屏） */
const immersive = ref(false);

function setImmersive(on: boolean): void {
  immersive.value = on;
  const html = document.documentElement;
  html.classList.toggle('fs-immersive', on);
  // 标签栏藏起来了，阅读器的高度 calc 还按 52px 留白就会空一条 —— 直接把变量归零
  if (on) html.style.setProperty('--header-h', '0px');
  else html.style.removeProperty('--header-h');
}

function onFullscreenChange(): void {
  isFullscreen.value = !!document.fullscreenElement;
}

async function toggleFullscreen(): Promise<void> {
  /* 反馈先给（用户口径：全屏 / 退出全屏那枚按钮状态反应慢）：
     `requestFullscreen()` 的 promise 要等浏览器把过渡做完才 resolve，`fullscreenchange` 更晚，
     于是图标要迟一拍才换。这里**先**把状态置上，再由 onFullscreenChange 校正到真值。 */
  isFullscreen.value = !document.fullscreenElement && !isFullscreen.value;
  try {
    if (document.fullscreenElement) { await document.exitFullscreen(); return; }
    if (immersive.value) { setImmersive(false); return; }
    if (!document.fullscreenEnabled) {
      setImmersive(true);
      toast('本浏览器不支持全屏，已切到沉浸模式', 'info', 2400);
      return;
    }
    await document.documentElement.requestFullscreen({ navigationUI: 'hide' });
  } catch {
    // 权限被拒（内嵌浏览器 / 无头环境）：退回沉浸模式，别弹一句看不懂的报错
    isFullscreen.value = !!document.fullscreenElement;   // 撤回刚才那次乐观置位
    setImmersive(true);
    toast('已切到沉浸模式（浏览器拒绝了全屏请求）', 'warn', 2600);
  }
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
 * 菜单打开那一刻的选区快照（只留「上色」要用的那几项）。
 *
 * 为什么需要：点「更多颜色」弹的是**系统取色器**，焦点一离开正文，浏览器就会折叠 DOM 选区；
 * 取色回来时有些路径连 selection 的 offsets 也一起被清掉了 —— 那时 addHighlight 拼出来的
 * 请求是「没有位置、没有原文」的一条，后端直接判非法，用户看到的就是
 * 「点了颜色没反应 / 刚选好的那段没了」（用户口径 2026-12-02）。
 * 菜单每次打开都重拍一张，所以快照永远对应当前这一次选区。
 */
type SelectionSnapshot = {
  text: string; fullText: string; anchorText: string;
  rowId: string; start: number; end: number; side: 'en' | 'zh';
  scope: 'row' | 'markdown' | 'pdf'; mdPage: number; page: number;
  segments: typeof selection.segments; x: number; y: number;
};
let menuSelSnapshot: SelectionSnapshot | null = null;
watch(() => selection.show, (on) => {
  if (!on) return;
  menuSelSnapshot = {
    text: selection.text, fullText: selection.fullText, anchorText: selection.anchorText,
    rowId: selection.rowId, start: selection.start, end: selection.end, side: selection.side,
    scope: selection.scope, mdPage: selection.mdPage, page: selection.page,
    segments: selection.segments.map((s) => ({ ...s })), x: selection.x, y: selection.y,
  };
});
/** 从正文划词带进「对话」面板的选区（发出去之后由面板 `clear-selection` 清掉） */
const chatSelection = ref<AgentSelection | null>(null);
/** 笔记浮层的状态见下面 `noteCards`（第五步：轻量浮层取代居中弹窗）。 */
/** 放大查看的目标（图 URL + 图注）：见下面的 `zoomTitle` */
const zoomImage = ref<ZoomTarget | null>(null);
/**
 * 表格 / 公式 / 插图的浮层（用户口径：侧栏列表里「浮层打开」）。
 *
 * 用户口径（本轮）：**可以同时开好几张** —— 以前是一个 `ref`，开第二张就把第一张顶掉；
 * 而且没有层级：点哪张都不会到最上层。现在是一**列**，每张自带 `z`：
 *   · 新开的那张拿 `++overlayZ`（永远在最上面）；
 *   · 点任意一张（`@activate`）把它的 `z` 抬到最新 —— 于是「点过的在最上」；
 *   · 层级区间从 70 起（笔记 / 解释 / 对话浮窗是 60~63，浮层永远压在它们之上），
 *     抬到 96 就整体重排一次，避免数目涨到几万。
 */
type BlockOverlayItem = {
  id: number;
  /** 稳定键：位置记忆（FloatingCard 的 persistKey）与「刷新后恢复」都按它认 */
  key: string;
  icon: string; title: string; titleMd?: string;
  markdown?: string; imageUrl?: string;
  /** 触发它的那枚按钮的矩形：浮层贴着按钮下方打开 */
  anchor?: OverlayAnchor | null;
  /** 这一张的层级（数字越大越靠上） */
  z: number;
};
const blockOverlays = ref<BlockOverlayItem[]>([]);
let blockOverlaySeq = 0;
/* 「谁在最上面」的唯一口径：`layerZ` 就是**最后发出去的那个号**，也就是当前最上层。
   所有层的 `focused` 都拿自己的 z 跟它比 —— 不按「种类」各算一遍。 */

/** 打开一张浮层（可以叠很多张；同一处连点会给同一张抬层级，而不是叠一摞一模一样的） */
function openBlockOverlay(p: Omit<BlockOverlayItem, 'id' | 'z' | 'key'>) {
  const same = blockOverlays.value.find((o) => o.icon === p.icon
    && o.title === p.title && o.markdown === p.markdown && o.imageUrl === p.imageUrl);
  if (same) { focusBlockOverlay(same.id); return; }
  const key = `${p.icon}|${p.title}|${p.imageUrl || ''}`;
  blockOverlays.value = [
    ...blockOverlays.value,
    { ...p, key, id: ++blockOverlaySeq, z: nextLayerZ() },
  ];
  persistBlockOverlays();
}

function closeBlockOverlay(id: number) {
  blockOverlays.value = blockOverlays.value.filter((o) => o.id !== id);
  persistBlockOverlays();
}

/* ------------------ 刷新 / 切走再回来：把浮层恢复回来 ------------------
 * 与笔记卡同一套思路（`cylit:notes-open:<paperId>`）：按**论文**记 —— 表格公式是这篇的产物。
 * 记的是**内容本身**（icon / 标题 / markdown / 图片地址），所以恢复时不必等接口；
 * 位置与大小由 `FloatingCard` 自己按 `persistKey`（`bo:<key>`）记，互不重复。 */
function blockOverlayStoreKey(): string {
  return `cylit:block-overlays:${paperId.value}`;
}
function persistBlockOverlays() {
  try {
    const payload = blockOverlays.value.map((o) => ({
      key: o.key, icon: o.icon, title: o.title, titleMd: o.titleMd,
      markdown: o.markdown, imageUrl: o.imageUrl,
    }));
    if (payload.length) localStorage.setItem(blockOverlayStoreKey(), JSON.stringify(payload));
    else localStorage.removeItem(blockOverlayStoreKey());
  } catch { /* 隐私模式忽略 */ }
}
function restoreBlockOverlays() {
  blockOverlays.value = [];
  try {
    const raw = localStorage.getItem(blockOverlayStoreKey());
    if (!raw) return;
    const saved = JSON.parse(raw) as Array<Omit<BlockOverlayItem, 'id' | 'z' | 'anchor'>>;
    if (!Array.isArray(saved) || !saved.length) return;
    /* 顺序照旧（后开的在上），层级按顺序重新发一遍 */
    blockOverlays.value = saved.slice(0, 12).map((o) => ({
      ...o,
      anchor: null,
      id: ++blockOverlaySeq,
      z: nextLayerZ(),
    }));
  } catch { /* 坏数据就当作没有 */ }
}

/** 点哪张就把哪张抬到最上层（用户口径：浮层要支持多层级、点击到最上层） */
function focusBlockOverlay(id: number) {
  const item = blockOverlays.value.find((o) => o.id === id);
  if (!item) return;
  /* ⚠️ **无条件**抬：以前这里有一句「已经是浮层里最高的就 return」，于是笔记卡压上来之后，
     点浮层完全没反应（它是浮层里最高、却不是全场最高）。层级要像窗口管理器那样，
     比的是**全场**，不是自己这一种类。 */
  item.z = nextLayerZ();
  blockOverlays.value = [...blockOverlays.value];
}

/**
 * 关闭大图查看器时"飞回原位"的落点：触发它的那张缩略图在视口中的矩形。
 *
 * 必须在 **capture 阶段**记：Vue 组件 emit 的 zoom 事件在冒泡阶段才被这一层处理，
 * 而 capture 的 pointerdown 比冒泡更早，所以打开查看器的那一刻这里存的就已经是"刚被点的那张图"。
 * 按下的不是图片（点空白 / 点文字 / 点按钮）就置 null —— 查看器会退化成直接关闭。
 */
const zoomOrigin = ref<{ left: number; top: number; width: number; height: number } | null>(null);
function onZoomOriginDown(ev: PointerEvent) {
  /* 查看器已经开着时，这一下按的是查看器自己（关闭按钮、背景、胶囊）—— 绝不能拿它覆盖落点，
     否则点关闭的那一下 origin 就被清成 null，退出动画直接退化成"原地淡出"。 */
  if (zoomImage.value) return;
  /* 触发放大的可能是 img 本身，也可能是包着它的容器（正文图点击落在 wrapper 上），
     所以向祖先找不到时再往子孙里找一次 —— 否则 origin 恒为 null，关闭就没有飞回动画。 */
  const t = ev.target instanceof Element ? ev.target : null;
  const el = t ? (t.closest('img') ?? t.querySelector('img')) : null;
  if (!el) { zoomOrigin.value = null; return; }
  const r = el.getBoundingClientRect();
  zoomOrigin.value = { left: r.left, top: r.top, width: r.width, height: r.height };
}

/**
 * 大图查看器里可以左右拖动切换的图片集合：全书插图，按页序。
 *
 * 每条同时带上两种 URL：`url` 用来显示（合成卡片优先），`altUrl` 是同一个 figure 的原图 URL，
 * 只用来匹配"用户点的到底是哪一张"。正文里点开的可能是二者之一，只按显示 URL 匹配会落空，
 * 以前落空后按 page 兜底 —— 同页有多张图时就会显示出不是点击的那一张。现在按 url / altUrl 精确匹配，
 * 匹配不上就老实当单图。
 */
const zoomGallery = computed<Array<ZoomTarget & { altUrl?: string | null }>>(() => figures.value
  .filter((f) => !!(f.card_url || f.image_url))
  .map((f) => {
    const display = (f.card_url || f.image_url) as string;
    const raw = f.image_url;
    return {
      url: display,
      altUrl: raw && raw !== display ? raw : null,
      caption: f.caption_zh || f.caption,
      page: f.page,
      number: f.number,
    };
  })
  .sort((a, b) => (a.page ?? 0) - (b.page ?? 0)));

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
 *  进这个页签**不会**再自动放宽侧栏（用户口径 2026-11-19：宽度只由用户自己拖）。
 *  ⚠️ 顺序是用户口径：**论文解读紧跟「目录」右边**（页签条可横向滚，放前面才不会被挤出可视区）。
 *  ⚠️ 原「阅读地图」页签已随该功能一起删除（2026-10-02）。 */
type RailTabKey = 'toc' | 'chat' | 'search' | SideTab;
const railTab = ref<RailTabKey>('toc');
/** 「目录」页签里的两种视图：大纲 / 页面缩略图（《修改计划》第二步，二）。
 *  只存本次会话：它是"这一眼怎么看"，不是论文的属性，没必要进 localStorage 或服务端进度。 */
const tocMode = ref<'outline' | 'pages'>('outline');
const SIDE_TAB_KEYS = [
  'toc', 'chat', 'insight', 'search', 'references', 'notes', 'formulas', 'tables', 'figures', 'terms',
  'tasks',
];
/* 切页签只做"进这个页签才需要的事"（例如参考文献首次拉取）；**不再改侧栏宽度**（见上一条注释）。 */
watch(railTab, (k) => {
  if (k === 'references') void ensureReferences();
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
  k: RailTabKey; icon: string; label: string; title: string; count?: number;
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
  {
    /* 用户口径：表格页签**紧挨公式**（两个都是"从正文里抽出来的独立对象"，相邻才好扫）。
       页签位置只放得下两个汉字，所以标签写「表格」、完整口径放在 title 里。 */
    k: 'tables', icon: 'table', label: '表格',
    title: '表格（从译文提取：中文表格 + 中文表格说明；点定位回到正文那张表）',
    count: tables.value.length || undefined,
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
const railTabsEl = ref<HTMLElement | null>(null);

/** 鼠标滚轮：横向滚动容器默认只吃水平轴（触控板/Shift+滚轮），普通鼠标滚轮会去滚页面 —— 转成横向。
 *  这里用 `behavior: 'instant'`：容器上开了 CSS 的 `scroll-behavior: smooth`，滚轮要跟手、不能补间。 */
function onSideTabsWheel(ev: WheelEvent) {
  const el = railTabsEl.value;
  if (!el || el.scrollWidth <= el.clientWidth + 1) return;
  if (Math.abs(ev.deltaY) > Math.abs(ev.deltaX)) {
    ev.preventDefault();
    el.scrollTo({ left: el.scrollLeft + ev.deltaY, behavior: 'instant' });
    syncTabsScroll();
  }
}

/** 切换页签后把它滚进可视区（最左/最右那两个尤其需要）。 */
watch(railTab, async () => {
  await nextTick();
  railTabsEl.value?.querySelector<HTMLElement>('.side-tab.active')
    ?.scrollIntoView({ block: 'nearest', inline: 'nearest' });
});

/** 能往左 / 往右滚时，在那一侧浮一枚圆形箭头按钮（用户口径：「左侧或者右侧有内容时显示」）。 */
const tabsScroll = reactive({ left: false, right: false });
function syncTabsScroll() {
  const el = railTabsEl.value;
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
  const el = railTabsEl.value;
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
  const el = railTabsEl.value;
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
/**
 * **无行可落的块**：`block_rows` 只收"能匹配到行"的块，剩下的块没有正文行 ——
 * 后端现在在 `row_less_blocks` 里单独告诉我们落点是"哪一页"（公式还多带一条 `latex`）。
 *
 * 键按 `rowAnchorId` 归一（与 `focusRow` 查表时的写法一致）：`row-p006-03` 与 `p006-03`
 * 是同一块。payload 不在（首屏 / 加载失败）时是空表，`focusRow` 照旧走原来的三级回退。
 */
const rowLessById = computed(() => {
  const map = new Map<string, NonNullable<ReaderPayload['row_less_blocks']>[number]>();
  for (const b of reader.value?.row_less_blocks ?? []) {
    if (b?.id) map.set(rowAnchorId(String(b.id)), b);
  }
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
      railTab.value = (sideParam === 'annotations' ? 'notes' : sideParam) as RailTabKey;
      /* `?side=insight` / `?side=chat` 还要**把左栏打开**：回到左栏之后，"选中页签"本身
       * 看不见就等于没反应（用户把左栏收起过 / 窄屏是抽屉）。宽度由 `watch(railTab)` 统一负责。 */
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
/**
 * 这一次深链定位要不要**动画**滚动。
 *
 * 深链默认直接落位（`smooth: false`）：刷新 / 首次进入时那一跳来自浏览器自己，
 * 加动画反而像"页面在抽动"。但**点目录的页面视图**也是走深链（先写 `?page=N`）——
 * 用户点的是"带我过去"，不是"重新打开"，所以要动画（用户口径 2026-11-28）。
 */
let deepLinkSmooth = false;
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
  const smooth = deepLinkSmooth;      // 取一次就复位：只对紧接着这一次生效
  deepLinkSmooth = false;
  const raw = route.query.row ?? route.query.block;
  const anchor = raw === undefined || raw === null ? '' : rowAnchorId(String(raw));
  /* 深链只给了一个"无行可落"的块 id（公式 / 插图 / 表格）时，页号从 `row_less_blocks` 取：
     后端已经告诉我们它落在哪一页，就不该再报"锚点已失效"，而该走下面那条
     "该段落没有独立正文行…已跳到第 N 页"的诚实分支。 */
  const wantPage = Number(route.query.page ?? 0)
    || Number(rowLessById.value.get(anchor)?.page ?? 0) || 0;
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
    await focusRow(row.id, { smooth, save: false, flash: true });
    return true;
  }

  if (wantPage) {
    deepLinkDone.value = sig;
    const first = allRows.value.find((r) => r.page === wantPage);
    if (first) {
      currentRowId.value = first.id;
      await nextTick();
      await focusRow(first.id, { smooth, save: false, flash: true });
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
  void loadTables();
}

/** 表格：从页级译文的 Markdown 里抽（口径见后端 `reader_service.markdown_tables`） */
async function loadTables() {
  tablesLoading.value = true;
  try {
    const d = await api.get<{ items: MarkdownTable[] }>(
      `/papers/${paperId.value}/tables`);
    tables.value = d.items ?? [];
  } catch (err) {
    console.warn('表格提取失败', err);
    tables.value = [];
  } finally {
    tablesLoading.value = false;
  }
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
  if (chatFloat.value) clampFloatBox();   // 窗口变小后浮窗可能越界
  syncPaneWidth();
}

/* ------------------------------ 侧栏拖拽改宽度 ------------------------------ */
/** 拖拽中：关掉宽度过渡（`.reader-aside` 上有 0.24s 的 transition，不关就是"拖起来黏手"） */
const asideResizing = ref(false);
/** 松手后的「回缩」动画：**只有松开才回缩**（用户口径 2026-11-26）。按住期间让它一直停在弹开
 *  的状态，所以回缩不能挂在 `.active` 上 —— 类一撤动画就没了。这里单独给一个短命类，
 *  由 onResizeUp 挂上、动画结束（300ms）后摘掉。 */
const resizerSettling = ref(false);
let resizerSettleTimer = 0;
function settleResizer() {
  if (resizerSettleTimer) window.clearTimeout(resizerSettleTimer);
  resizerSettling.value = true;
  resizerSettleTimer = window.setTimeout(() => {
    resizerSettling.value = false;
    resizerSettleTimer = 0;
  }, 320);
}
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
  if (resizerSettleTimer) { window.clearTimeout(resizerSettleTimer); resizerSettleTimer = 0; }
  resizerSettling.value = false;
  resizeStartX = ev.clientX;
  resizeStartW = asideWidth.value;
  asidePreviewW.value = asideWidth.value;  // 从当前宽度起算（拖动期间只动这个）
  resizeEl = ev.currentTarget as HTMLElement;
  resizePointerId = ev.pointerId;
  /* 合成事件（自检探针）没有真指针，捕获会抛 NotFoundError —— 位移本身不依赖它 */
  try { resizeEl.setPointerCapture?.(ev.pointerId); } catch { /* 没有真指针 */ }
  window.addEventListener('pointerup', onResizeUpAnywhere, true);
  window.addEventListener('pointercancel', onResizeUpAnywhere, true);
  window.addEventListener('blur', onResizeUpAnywhere, true);
  ev.preventDefault();                      // 别把拖拽变成"选侧栏里的文字"
}

/** 拖拽实时夹取走 `clampAsideWidth`：**当前模式的上限**在这里也要生效（用户口径），
 *  只在初始化时算一次的话，对照模式下能把侧栏拖到上限之外、把译文挤没。
 *  ⚠️ 这里**只写预览值**，`asideWidth`（→ 整页重排）留到松手（见 `onResizeUp`）。 */
let resizeMoveRaf = 0;
let resizeMoveX = 0;
function onResizeMove(ev: PointerEvent) {
  if (!asideResizing.value) return;
  if (ev.buttons === 0) {                   // 兜底：`pointerup` 丢了，但按键其实已经松开
    void onResizeUp();
    return;
  }
  resizeMoveX = ev.clientX;
  if (!resizeMoveRaf) resizeMoveRaf = requestAnimationFrame(applyResizeMove);
}

function applyResizeMove() {
  resizeMoveRaf = 0;
  if (!asideResizing.value) return;
  asidePreviewW.value = clampAsideWidth(resizeStartW + (resizeMoveX - resizeStartX));
  /* 面板跟手的同时，对话页 / 占位面板也按预览宽度排版（见 paintAsideLiveWidth） */
  paintAsideLiveWidth(asidePreviewW.value);
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
  if (resizeMoveRaf) { cancelAnimationFrame(resizeMoveRaf); resizeMoveRaf = 0; }
  asideResizing.value = false;
  settleResizer();                    // 松手才回缩（按住期间停在弹开态）
  /* 松手才把预览值写回真值：这一次写入 = 整页重排一次（拖动期间一次都没有） */
  if (asidePreviewW.value !== null) {
    asideWidth.value = clampAsideWidth(asidePreviewW.value);
    asidePreviewW.value = null;
  }
  /* 落盘值那一帧到了再把行内宽度撤掉：先撤会有一帧退回旧宽度（闪一下）。
     vue 的 CSS 变量写在同一次 flush 里落到根上，所以 nextTick 之后两者已经相等。 */
  await nextTick();
  paintAsideLiveWidth(null);
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

/* ------------------------------ 侧栏浮窗：开 / 收 / 拖 ------------------------------ */

/** 夹进视口：打开、拖动、改尺寸、窗口 resize 全走这一条 */
function clampBox(b: { left: number; top: number; w: number; h: number }) {
  const vw = window.innerWidth;
  const vh = window.innerHeight;
  b.w = Math.round(Math.max(FLOAT_MIN_W, Math.min(b.w, vw - FLOAT_EDGE * 2)));
  b.h = Math.round(Math.max(FLOAT_MIN_H, Math.min(b.h, vh - FLOAT_EDGE * 2)));
  b.left = Math.round(Math.max(FLOAT_EDGE, Math.min(b.left, vw - b.w - FLOAT_EDGE)));
  b.top = Math.round(Math.max(FLOAT_EDGE, Math.min(b.top, vh - b.h - FLOAT_EDGE)));
}
/** 落盘 / 打开时那份（同一套数学：手势里的手感必须与落定后完全一致） */
function clampFloatBox() { clampBox(floatBox.value); }

/** 浮窗位置/尺寸的记忆键：**按论文各记一份**（用户口径 2026-11-26 —— 每篇论文的浮层摆在
 *  哪儿是各自的事：一篇靠左做对照、另一篇摆右下，切回来都该原样）。 */
function floatBoxKey(): string {
  return `cylit:reader-float:${paperId.value}`;
}
/** 旧版那一条**全局**记录：每篇专属键还没有值时兜底读一次（升上来不丢老位置），写完就不再回读。 */
const FLOAT_BOX_KEY_LEGACY = 'cylit:reader-float';

/**
 * 读回上次的位置与尺寸（用户口径 2026-11-19：打开浮层接着放在上次那儿，不要每次都甩到右下角）。
 * 读完还要过一遍 `clampFloatBox`：换过显示器、窗口变小之后，旧坐标可能整个在屏幕外。
 * @returns 有没有读到可用的记录
 */
function restoreFloatBox(): boolean {
  try {
    const raw = localStorage.getItem(floatBoxKey())
      ?? localStorage.getItem(FLOAT_BOX_KEY_LEGACY);
    if (!raw) return false;
    const b = JSON.parse(raw) as { left?: unknown; top?: unknown; w?: unknown; h?: unknown };
    const nums = [b.left, b.top, b.w, b.h];
    if (!nums.every((n) => typeof n === 'number' && Number.isFinite(n))) return false;
    floatBox.value = { left: b.left as number, top: b.top as number, w: b.w as number, h: b.h as number };
    clampFloatBox();
    return true;
  } catch { return false; }
}

/** 落盘（拖动/改尺寸结束时写一次；每像素写没意义） */
function persistFloatBox() {
  try { localStorage.setItem(floatBoxKey(), JSON.stringify(floatBox.value)); } catch { /* 隐私模式忽略 */ }
}

/**
 * 浮层**开着还是关着**的记忆键。
 *
 * 为什么非得记：App 的 RouterView 带 key，切到别的标签页再切回来，阅读器是**重新挂载**的
 * （chatFloat 回到 false）—— 用户看到的就是「切个标签页回来，刚才浮起来的对话不见了」
 * （用户口径 2026-11-26 第一版）。现在**按论文各记一份**：同一篇论文的浮层开关跟着这篇走，
 * 换一篇不会把「拎出来看对话」这个习惯硬套上去（用户口径 2026-11-26 追加：每个页面分别记）。
 */
function floatOpenKey(): string {
  return `cylit:reader-float-open:${paperId.value}`;
}
/** 旧版全局键：只在每篇专属键还没有值时兜底读一次。 */
const FLOAT_OPEN_KEY_LEGACY = 'cylit:reader-float-open';

/** 只记**用户意图**：窄屏把浮层强制收掉那条路径不写这里（那是环境，不是选择）。 */
function persistFloatOpen(open: boolean) {
  try { localStorage.setItem(floatOpenKey(), open ? '1' : '0'); } catch { /* 隐私模式忽略 */ }
}

function wantsFloatOpen(): boolean {
  try {
    const raw = localStorage.getItem(floatOpenKey())
      ?? localStorage.getItem(FLOAT_OPEN_KEY_LEGACY);
    return raw === '1';
  } catch { return false; }
}

/** 没有记录时的兜底：**右下角**（胶囊工具栏在上方，两边不抢地盘） */
function resetFloatBox() {
  const vw = window.innerWidth;
  const vh = window.innerHeight;
  const w = Math.min(ASIDE_W_CHAT, vw - FLOAT_EDGE * 2);
  const h = Math.min(560, vh - FLOAT_EDGE * 2);
  floatBox.value = { w, h, left: vw - w - FLOAT_EDGE, top: vh - h - FLOAT_EDGE };
  clampFloatBox();
}

/**
 * 切到浮层。
 *
 * 用户口径 2026-11-22 **修订了 2026-11-19 那条「点了就收起侧栏」**：浮层打开后侧栏
 * **仍然占位**，只是内容区变成「画中画」占位面板（`.aside-pip`，此时侧栏不可用），
 * 直到浮层关闭。于是：版面一点不动（正文列不会因为开关浮层而整篇重排），
 * 「关浮层」天然就是回到侧栏，不用再把侧栏展开一次。
 * `asideOpen` 语义不变：它仍是「开」。
 */
async function openChatFloat() {
  if (isNarrow.value || chatFloat.value) return;
  pinReadingAnchor();                 // 栏宽要变，先钉住正在读的那一行
  railTab.value = 'chat';
  /* 先试着回到上次的位置；没有记录（或记录已不可用）才落到右下角 */
  if (!restoreFloatBox()) resetFloatBox();
  /* 侧栏若本来是收起的，先把它摆成开（浮层要有那块"家"可回；`asideOpen` 语义仍是"开"）。
     互斥的 watch 已随占位态一起删掉，所以这里不必再排先后顺序。 */
  if (!asideOpen.value) {
    asideOpen.value = true;
    try { localStorage.setItem(asideKey, '1'); } catch { /* 忽略 */ }
  }
  chatFloat.value = true;
  topLayer.value = 'chat';           // 刚浮起来的那块在最上面
  chatZ.value = nextLayerZ();        // 层级走那条共用序列（否则会比后开的笔记卡低）
  persistFloatOpen(true);
  await nextTick();
  syncPaneWidth();
}

/** 回到侧栏：贴回左边并展开（与「切到浮层」是同一枚按钮的两个方向） */
async function dockChatFromFloat() {
  if (!chatFloat.value) return;
  pinReadingAnchor();
  chatFloat.value = false;
  persistFloatOpen(false);
  railTab.value = 'chat';
  asideOpen.value = true;
  try { localStorage.setItem(asideKey, '1'); } catch { /* 忽略 */ }
  await nextTick();
  syncPaneWidth();
}

/** 关浮窗时**禁用一帧过渡**：见 `closeChatFloat` */
const floatClosing = ref(false);

/**
 * 关掉浮窗 = **回到侧栏**（用户口径 2026-11-22）。
 *
 * 侧栏此刻仍然是开着的（浮层只占位、不关闭），所以浮窗一撤，`.aside-pip` 占位面板消失、
 * 侧栏内容原样回来 —— 不需要再展开一次，也不会触发一次「飞出去」的补间。
 *
 * ⚠️ 仍保留那一帧去过渡（`floatClosing`）：万一侧栏在这期间被收起来了
 * （用户点了左侧那枚开关），`.no-aside` 的 `translateX(-100%)` + 透明会在同一帧里变化，
 * 不停用过渡就会被补间一遍，看起来像「关浮窗触发了一次侧栏收起动画」（用户口径 2026-11-19）。
 */
function closeChatFloat() {
  floatClosing.value = true;
  chatFloat.value = false;
  persistFloatOpen(false);
  // 两帧之后再恢复过渡：那时属性都已经落定，恢复不会产生任何补间
  requestAnimationFrame(() => requestAnimationFrame(() => { floatClosing.value = false; }));
}

/**
 * 重新挂载之后把浮层摆回去（切标签页、刷新、从别的页面回来都走这条路）。
 *
 * 与 openChatFloat 只差两处，都是为了**别把用户的视线搬走**：不把侧栏页签强切到「对话」
 * （浮层开着时侧栏本来就允许停在任意页签），也不做那遍「从侧栏飞出来」的补间。
 */
async function restoreChatFloat() {
  if (chatFloat.value || isNarrow.value || !wantsFloatOpen()) return;
  floatClosing.value = true;         // 恢复不是用户操作：这一帧停掉过渡
  if (!restoreFloatBox()) resetFloatBox();
  if (!asideOpen.value) {
    asideOpen.value = true;
    try { localStorage.setItem(asideKey, '1'); } catch { /* 忽略 */ }
  }
  chatFloat.value = true;
  topLayer.value = 'chat';
  await nextTick();
  syncPaneWidth();
  requestAnimationFrame(() => requestAnimationFrame(() => { floatClosing.value = false; }));
}

/** 浮窗的拖动 / 改尺寸：与侧栏把手同一套 pointer 捕获（拖出窗口再回来也不会断） */
const floatDrag = {
  on: false,
  /** `size` = 右下角（左边不动），`size-left` = 左下角（右边不动） */
  mode: '' as '' | 'move' | 'size' | 'size-left',
  sx: 0, sy: 0, ox: 0, oy: 0, ow: 0, oh: 0,
};

/**
 * 拖动期间的几何 —— **普通对象，不是响应式**。
 *
 * 以前手势里每帧写 floatBox（响应式 ref）：left 一变就让 asideFloatStyle / floatGripStyle 失效，
 * 于是 ReaderView 这几千行的模板**整棵重渲染**一遍，一次指针移动就是十几到几十毫秒 ——
 * 这才是「拖浮层卡顿掉帧」的真正成本（在 CSS 层面怎么调都治不好）。
 * 现在手势期间只往**三个真实 DOM**（宿主 + 两枚角把手）上写样式，松手才把结果同步回 floatBox：
 * 整个手势零次重渲染。
 */
const floatLive = { left: 0, top: 0, w: 0, h: 0 };
/** 手势期间直接操作的那三个节点（ref 只在浮窗态存在） */
const chatHostEl = ref<HTMLElement | null>(null);
const gripBlEl = ref<HTMLElement | null>(null);
/* 顶部那条抓手（`.chat-grab`）：药丸的「变宽」反馈挂在它身上，与笔记浮层的 `.fc-grab.dragging` 同一手法 */
const chatGrabEl = ref<HTMLElement | null>(null);
const gripBrEl = ref<HTMLElement | null>(null);
/** 浮层模式下那块"画中画"占位面板（拖侧栏宽度时要跟预览值同宽） */
const asidePipEl = ref<HTMLElement | null>(null);

/** 两枚角把手的几何（跟着卡片走；拖头时卡片走 transform，它们只能逐帧写 left/top） */
function writeFloatGrips(b: { left: number; top: number; w: number; h: number }) {
  const top = b.top + b.h - FLOAT_GRIP_SIZE + FLOAT_GRIP_GAP;
  const left = b.left + b.w - FLOAT_GRIP_SIZE + FLOAT_GRIP_GAP;
  const bl = gripBlEl.value;
  if (bl) { bl.style.left = (b.left - FLOAT_GRIP_GAP) + 'px'; bl.style.top = top + 'px'; }
  const br = gripBrEl.value;
  if (br) { br.style.left = left + 'px'; br.style.top = top + 'px'; }
}

/** 把一份几何写进 DOM（宿主 + 两枚角把手）：手势里写 floatLive，其余时候写 floatBox */
function writeFloatDom(b: { left: number; top: number; w: number; h: number }) {
  const host = chatHostEl.value;
  if (host) {
    /* transform 只在拖头的手势里用（见 paintFloatLive），落在别处要撤干净，
       否则一次拖动之后再改 left/top 会叠着上一次的位移。 */
    if (host.style.transform) host.style.transform = '';
    host.style.left = b.left + 'px';
    host.style.top = b.top + 'px';
    host.style.width = b.w + 'px';
    host.style.height = b.h + 'px';
  }
  writeFloatGrips(b);
}

/**
 * 手势里把 `floatLive` 画到屏幕上。
 *
 * **拖头走 transform、其余走 left/top**（用户口径 2026-12-03：拖对话浮层卡顿掉帧）。
 * 原因：一张 500×560、带 48px 柔光投影、圆角裁切、里面还塞着整段对话的卡片，
 * 每帧改 left/top 就是**整块重新绘制**（Chromium 不会为 left/top 建合成层）；
 * transform 只让合成器挪一次贴图，卡片里一个像素都不用重画。
 * ⚠️ 改尺寸（两条角把手）没法合成 —— 宽高不是可合成属性，那条路照旧写 left/top/宽高。
 */
function paintFloatLive() {
  const host = chatHostEl.value;
  if (host && floatDrag.mode === 'move') {
    const dx = Math.round(floatLive.left - floatDrag.ox);
    const dy = Math.round(floatLive.top - floatDrag.oy);
    const next = 'translate3d(' + dx + 'px,' + dy + 'px,0)';
    if (host.style.transform !== next) host.style.transform = next;
  } else if (host) {
    if (host.style.transform) host.style.transform = '';
    host.style.left = floatLive.left + 'px';
    host.style.top = floatLive.top + 'px';
    host.style.width = floatLive.w + 'px';
    host.style.height = floatLive.h + 'px';
  }
  writeFloatGrips(floatLive);
}

/**
 * 拖侧栏宽度时，把**预览宽度**同步写到"跟侧栏同宽"的那两块容器上。
 *
 * 为什么需要它：对话容器（`.reader > .chat-host`）与浮层占位面板（`.aside-pip`）的宽度都来自
 * `v-bind(asideWidthPx)`（**落盘值**），而拖动期间只有 `asidePreviewW` 在变 ——
 * 于是侧栏边框跟着手走、里面的对话页却停在旧宽度上（用户口径 2026-11-25：
 * 「拖动侧栏时对话页没有实时更新」）。这两块不能改走 v-bind 的预览值（那是组件根上的变量，
 * 一改整棵树重算样式），所以拖动期间只给它们写一份**行内宽度**：只有它们自己那一层重排。
 *
 * 松手后落盘值一更新、行内值就撤掉（两者数值相同，撤掉不会有任何一帧跳动）。
 * 浮窗态一个字都不碰：那时这两块的几何归 `floatLive` / `applyFloatDom`。
 */
function paintAsideLiveWidth(px: number | null) {
  const w = px === null ? '' : `${px}px`;
  const host = chatHostEl.value;
  if (host && !chatFloat.value) host.style.width = w;
  const pip = asidePipEl.value;
  if (pip) pip.style.width = w;
}

/**
 * 浮窗几何 / 定位 / 层级由这个 effect **直接写 DOM**，模板里不再绑 `:style`。
 *
 * 以前绑在模板上（asideFloatStyle / floatGripStyle）：floatBox 一变，这个几千行的模板就整棵重渲染
 * —— 拖动时就是每帧一次，这才是掉帧的根。完全不绑又会让手势期间偶发的重渲染按**旧值**把几何写回去。
 * 写到 DOM 上两头都躲开了：手势每帧写 floatLive，落定后由这里按 floatBox 写一遍（值相同 = 空操作）。
 * `flush: 'sync'` 是必须的：开关浮层、窗口 resize 这些一次性变化要在这一帧绘制前就落到样式上。
 */
function applyFloatDom() {
  const host = chatHostEl.value;
  if (!host) return;
  if (!chatFloat.value) {
    /* 贴栏态：几何全交回 CSS（position: absolute / v-bind 的宽度） */
    host.style.position = '';
    host.style.left = '';
    host.style.top = '';
    host.style.width = '';
    host.style.height = '';
    host.style.right = '';
    host.style.bottom = '';
    host.style.zIndex = '';
    host.style.transform = '';      // 拖头用过 transform：回贴栏态必须撤干净（见 paintFloatLive）
    return;
  }
  host.style.position = 'fixed';
  host.style.right = 'auto';
  host.style.bottom = 'auto';
  /* 焦点层：与两张 FloatingCard 同一档（62 = 有焦点 / 60 = 没有，见 topLayer），
     于是浮窗和两张卡不会互相压死，也不去碰 30/31/40 那几档既有层级。 */
  /* 是不是最上层：拿自己的 z 跟「最后发出去的那个号」比（与笔记卡 / 内容浮层同一口径） */
  const focused = chatZ.value === layerZ.value;
  /* 层级来自那条共用序列（点一下就抬一档），不是固定的 62/60 —— 否则笔记卡永远压不上去 */
  host.style.zIndex = String(chatZ.value);
  /* 两枚角把手也**跟着同一档走**（63 / 61）：写死 63 时，浮窗没焦点（60）而笔记卡有焦点（62），
     浮窗的把手照样压在人家卡片上面（用户口径 2026-11-24 第 1 条：指示器层级不对）。 */
  const gripZ = String(chatZ.value + 1);
  const gbl = gripBlEl.value;
  if (gbl) gbl.style.zIndex = gripZ;
  const gbr = gripBrEl.value;
  if (gbr) gbr.style.zIndex = gripZ;
  /* 手势进行中不要插手：几何的真值在 floatLive 里，由手势循环每帧写 */
  if (!floatDrag.on) writeFloatDom(floatBox.value);
}
watchEffect(applyFloatDom, { flush: 'sync' });

/* ⚠️ **不要**再把位移改成 transform：拖动本身确实更省，但松手要把 transform 落成 left/top
   并撤掉 will-change，合成层被拆掉、卡片按新位置重新栅格化 —— 看起来就是「拖完闪一下」
   （用户口径 2026-11-24，与 2026-11-19 那次同一个现象）。现在每帧只写 left/top，
   不走 Vue，只有这一块 DOM 重算样式，干脆没有那一帧。 */

/**
 * 浮窗的拖动现在挂在**整块面板**上（标题栏那一行已经和会话头合并，没有单独的抓手区了）：
 * 只有从"头部那一行"按下去才算拖动 —— 正文里选字、点按钮都不该把窗口挪走。
 */
/**
 * 浮窗正在被拖动（这一会儿整块阅读区**不许选中文字**，抓手也在这时候变宽）。
 *
 * ⚠️ 这个类**不走模板绑定**：以前写 `floatDragging ? 'float-dragging' : ''`，于是按下与松手那一下
 * 都要把 `ReaderView`（几千行 + 整棵对话面板）重渲染一遍，几十毫秒的主线程尖峰正好吃掉抓手补间的
 * 头几帧 —— 表现就是「点抓手时的动画没有点别处细腻」，松手那一帧还会闪。
 * 现在直接 `classList.toggle` 到根元素上：类名照旧命中那些 CSS，但一次渲染都不触发。
 */
const readerEl = ref<HTMLElement | null>(null);

function setFloatDragging(on: boolean) {
  readerEl.value?.classList.toggle('float-dragging', on);
}

/* 卸载兜底：手势中途离开阅读器时闸门要放掉，否则 PDF 文字层会一直不可选 */
onUnmounted(() => releaseFloatSelection());

/** 触摸拖头部那一行：与 `onFloatCardDown` 同一判据，坐标与收尾走 touch 通道 */
function onFloatCardTouchStart(ev: TouchEvent) {
  if (!chatFloat.value) return;
  const t = ev.target as HTMLElement | null;
  if (!t?.closest?.('.thread-head, .list-head, .chat-grab')) return;
  /* ⚠️ 按在**按钮**上就整条放行（不 preventDefault、不起手势）：取消 touchstart 会把
     后续的 click 一起掐掉，于是头部那几枚键在触摸屏上点了没反应（用户口径 2026-11-25）。 */
  if (t.closest('button, input, textarea, label, a')) return;
  const p = ev.touches[0];
  if (!p) return;
  ev.preventDefault();
  if (!floatDrag.on) startFloatGesture('move', p.clientX, p.clientY);
  trackFloatTouch();
}

function onFloatCardDown(ev: PointerEvent) {
  if (!chatFloat.value) return;
  const t = ev.target as HTMLElement | null;
  if (!t?.closest?.('.thread-head, .list-head, .chat-grab')) return;
  onFloatPointerDown(ev, 'move');
}

/**
 * 拖动抓手上的 `mousedown` 也要挡掉（用户口径 2026-11-19：拖浮层时**有时会选中 PDF 里的字**）。
 *
 * 只靠 `pointerdown` 的 `preventDefault()` 不够：鼠标输入下浏览器照样会派发 `mousedown`，
 * 于是"按住抓手往外拖"被当成一次**文本拖选**的起点 —— 抓手那几行虽然是 `user-select: none`，
 * Chromium 会把选区锚到下一块可选内容（正文/PDF 文本层）上，松手就留下一条蓝。
 */
function onFloatCardMouseDown(ev: MouseEvent) {
  if (!chatFloat.value) return;
  const t = ev.target as HTMLElement | null;
  /* 按钮 / 输入类目标放行：这一条是挡"拖选"的，不该连按键的原生按下态一起挡掉 */
  if (!t?.closest?.('.thread-head, .list-head, .chat-grab')) return;
  if (t.closest('button, input, textarea, label, a')) return;
  ev.preventDefault();
}

/** 手势起手（几何快照 + 循环 + 选区闸门）。鼠标/笔与触摸共用，坐标由调用方给 */
function startFloatGesture(mode: 'move' | 'size' | 'size-left', x: number, y: number) {
  floatDrag.on = true;
  floatDrag.mode = mode;
  /* 角把手的「按下」反馈走 **class**（不是 `:active`）：触摸屏上我们在 touchstart 里
     `preventDefault()` 了，`:active` 就未必来；笔记卡那边一直是 `.fc-resize.is-on`，
     两边必须同一手法，否则「手指按住角把手没有动画」（用户口径 2026-11-25）。 */
  if (mode !== 'move') (mode === 'size' ? gripBrEl.value : gripBlEl.value)?.classList.add('is-on');
  /* 顶部那枚抓手的「变宽 + 变亮」**与笔记浮层逐字同一手法**：类挂在抓手条自己身上
     （`.chat-grab.dragging` ↔ `.fc-grab.dragging`），按下就加、松手就撤，
     与合成层那条 `.float-dragging`（它是给 will-change 用的，晚一帧撤）解耦 ——
     两件事共用一个类的话，药丸回弹会被拖晚一帧（用户口径 2026-12-03 第 5 条）。 */
  if (mode === 'move') {
    setFloatDragging(true);
    chatGrabEl.value?.classList.add('dragging');
  }

  floatDrag.sx = x; floatDrag.sy = y;
  floatDrag.ox = floatBox.value.left; floatDrag.oy = floatBox.value.top;
  floatDrag.ow = floatBox.value.w; floatDrag.oh = floatBox.value.h;
  /* 手势从这一刻的几何开始算：之后每一帧只改 floatLive 这个普通对象 */
  floatLive.left = floatDrag.ox; floatLive.top = floatDrag.oy;
  floatLive.w = floatDrag.ow; floatLive.h = floatDrag.oh;
  /* 首帧就有坐标可算；循环从按下就开始 —— 手势期间每帧都重写一遍，见 floatLoopTick */
  floatMoveAt = { x, y };
  startFloatLoop();
  /* 任何手势（拖头部 / 拖左右下角）都上闸：PDF 文字层写死了 user-select: text，
     指针扫过它就会自己长出一段选区 —— 拖角放大时用户报的就是这个（2026-11-24 第 3 条）。 */
  lockFloatSelection(true);
}

function onFloatPointerDown(ev: PointerEvent, mode: 'move' | 'size' | 'size-left') {
  // 标题栏上那两枚按钮（回到侧栏 / ×）不参与拖动
  if (mode === 'move' && (ev.target as HTMLElement | null)?.closest?.('button')) return;
  const el = ev.currentTarget as HTMLElement | null;
  startFloatGesture(mode, ev.clientX, ev.clientY);
  try { el?.setPointerCapture?.(ev.pointerId); } catch { /* 合成事件没有真指针 */ }
  if (ev.pointerType === 'touch') trackFloatTouch();   // 触摸：补一条 touch 通道（见 onFloatTouchStart）
  ev.preventDefault();
}

/**
 * 手势期间**每帧**重写一遍几何（不是「只跟着 pointermove 写」）。
 *
 * 两个理由，缺一不可：
 * 1. pointermove 一帧可能来好几次（平板 120Hz），逐条写等于让样式重算排队；
 * 2. **防被覆盖**：手势里若这个组件因为别的原因重渲染一次（回答还在流式输出就会），
 *    Vue 会按 vnode 里那份**旧的** floatBox 把 left/top 写回去。Vue 的补丁跑在微任务里、
 *    早于下一帧的 rAF —— 每帧再写一遍就把那一下盖回去，肉眼看不到跳动。
 */
let floatMoveRaf = 0;
let floatMoveAt: { x: number; y: number } | null = null;

function startFloatLoop() {
  if (!floatMoveRaf) floatMoveRaf = requestAnimationFrame(floatLoopTick);
}

function stopFloatLoop() {
  if (floatMoveRaf) { cancelAnimationFrame(floatMoveRaf); floatMoveRaf = 0; }
}

function floatLoopTick() {
  floatMoveRaf = 0;
  if (!floatDrag.on) return;
  applyFloatMove();
  floatMoveRaf = requestAnimationFrame(floatLoopTick);
}

function onFloatPointerMove(ev: PointerEvent) {
  if (!floatDrag.on) return;
  if (ev.buttons === 0) { onFloatPointerUp(ev); return; }   // 兜底：pointerup 丢了
  floatMoveAt = { x: ev.clientX, y: ev.clientY };
  startFloatLoop();
}

/** 用**最新**的指针位置重算一遍并写 DOM（每帧一次，见 `floatLoopTick`） */
function applyFloatMove() {
  const at = floatMoveAt;
  if (!at || !floatDrag.on) return;
  /* 拖动/改大小的**全过程**里持续清选区：PDF 的划词是它自己的原生行为（甚至可能在我们
     preventDefault 之前就起了一个锚点），单靠 CSS user-select 与一次性 preventDefault 挡不干净。
     ⚠️ 以前挂在 pointermove 上：120Hz 平板一帧能来两三条，等于把同一次 DOM 操作排三次队；
     现在每帧最多一次（这一帧真要在改几何的时候）。 */
  clearDocSelection();
  const dx = at.x - floatDrag.sx;
  const dy = at.y - floatDrag.sy;
  /* 增益现在是 1（1:1 跟手，与笔记浮层一致）。保留 k 这个乘法：它是唯一的「手感旋钮」，
     以后要调只改 RESIZE_GAIN 一处。 */
  const k = RESIZE_GAIN;
  if (floatDrag.mode === 'move') {
    /* 位置照旧**每帧夹一次视口**（拖出屏幕的边界手感不变）；真正写出去的是 transform
       （见 paintFloatLive），所以这里只更新 floatLive —— 卡片每帧只被合成器挪一次，不重绘。 */
    floatLive.left = floatDrag.ox + dx;
    floatLive.top = floatDrag.oy + dy;
    clampBox(floatLive);
    paintFloatLive();
    return;
  }
  if (floatDrag.mode === 'size-left') {
    /* 左下角：**右下角钉住**（right 先记下来），宽高怎么变都不该让整块浮窗跳一下 */
    const right = floatDrag.ox + floatDrag.ow;
    floatLive.top = floatDrag.oy;
    floatLive.w = floatDrag.ow - dx * k;
    floatLive.h = floatDrag.oh + dy * k;
    clampBox(floatLive);
    floatLive.left = right - floatLive.w;
  } else {
    floatLive.left = floatDrag.ox;
    floatLive.top = floatDrag.oy;
    floatLive.w = floatDrag.ow + dx * k;
    floatLive.h = floatDrag.oh + dy * k;
    clampBox(floatLive);
  }
  /* 改尺寸没法合成（宽高不是可合成属性），但至少只写这三块 DOM，不惊动 Vue */
  paintFloatLive();
}

function onFloatPointerUp(ev: PointerEvent) {
  if (!floatDrag.on) return;
  /* ⚠️ 触摸的 `pointercancel` **不是松手**：浏览器一决定接管滚动/手势就发它。
     早先这里无条件收尾，于是手指刚碰到把手、还没动，浏览器就把这次拖拽掐掉了 ——
     表现就是"对话浮窗的角把手点不动"（用户口径 2026-11-24；笔记卡那两枚因为周围不可滚，
     不会引起浏览器接管，所以一直正常）。触摸真正的结束在 `onFloatTouchEnd`。 */
  if (ev.type === 'pointercancel' && ev.pointerType === 'touch') return;
  endFloatGesture();
  try { (ev.currentTarget as HTMLElement | null)?.releasePointerCapture?.(ev.pointerId); } catch { /* 早已释放 */ }
}

/** 手势收尾（鼠标/笔的 pointerup、触摸的 touchend，两条通道共用） */
function endFloatGesture() {
  if (!floatDrag.on) return;
  stopFloatLoop();
  /* 最后一帧里「移动 + 松手」挤在一起时（快速一划），合并中的位移还没落地 —— 这里补算一次，
     否则那一下被整条丢掉（拖角快速一甩等于没反应）。 */
  if (floatMoveAt) applyFloatMove();
  floatMoveAt = null;
  /* 收尾：**先写终态 left/top、同一帧里再撤 transform**，最后把结果同步回 floatBox。
     两件事在同一个任务里做，浏览器只算一次样式 —— 中间那个「既没位移又没 transform」的
     瞬间永远不会被画出来，所以不会「拖完闪一下」（2026-11-24 那条口径仍然成立）。 */
  const host = chatHostEl.value;
  if (host) {
    host.style.left = floatLive.left + 'px';
    host.style.top = floatLive.top + 'px';
    host.style.width = floatLive.w + 'px';
    host.style.height = floatLive.h + 'px';
    host.style.transform = '';
  }
  writeFloatGrips(floatLive);
  floatBox.value = { left: floatLive.left, top: floatLive.top, w: floatLive.w, h: floatLive.h };
  floatDrag.on = false;
  floatDrag.mode = '';
  /* 类名与选区闸门**同步**撤掉：`.float-dragging` 现在只影响抓手那一枚药丸，
     闸门（`html.cy-float-gesture`）只影响 PDF 文字层 —— 都不再有文档级重算。
     （延帧反而危险：万一这一帧里又起了手势，那次释放就丢了，整个应用会一直不可选。） */
  gripBrEl.value?.classList.remove('is-on');
  gripBlEl.value?.classList.remove('is-on');
  chatGrabEl.value?.classList.remove('dragging');   // 药丸立刻回弹（与笔记抓手同一刻）
  /* 合成层（.float-dragging 的 will-change）**晚一帧**才撤：与 left/top 在同一帧里一起翻面，
     Chromium 要按新位置重新栅格化，那一帧可能先画出空白 —— 就是「拖完闪一下」。 */
  requestAnimationFrame(() => { if (!floatDrag.on) setFloatDragging(false); });
  lockFloatSelection(false);
  untrackFloatTouch();
  persistFloatBox();                  // 记住这一轮的摆位（下次打开接着放这儿）
}

/* ------------------------------ 触摸通道 ------------------------------
 * 触摸屏上 pointer 事件**会在浏览器接管滚动/手势时断流**（`pointercancel`）——
 * 选区手柄那边早就踩过这一条，做法一样：坐标与收尾都补一条 non-passive 的 touch 通道，
 * 谁先到都算（见 onHandleTouchMove 的说明）。这样即便 pointer 流被掐断，把手照样跟手。
 */
function trackFloatTouch() {
  document.addEventListener('touchmove', onFloatTouchMove, { passive: false });
  document.addEventListener('touchend', onFloatTouchEnd, true);
  document.addEventListener('touchcancel', onFloatTouchEnd, true);
}

function untrackFloatTouch() {
  document.removeEventListener('touchmove', onFloatTouchMove);
  document.removeEventListener('touchend', onFloatTouchEnd, true);
  document.removeEventListener('touchcancel', onFloatTouchEnd, true);
}

function onFloatTouchStart(ev: TouchEvent, mode: 'move' | 'size' | 'size-left') {
  const t = ev.touches[0];
  if (!t) return;
  /* 模板上是 `@touchstart.prevent`：非被动地挡掉默认动作，浏览器就不会把这次触摸当滚动接管，
     连 `pointercancel` 都不会发。gesture 若已由 pointerdown 起手（正常路径），这里只管补通道。 */
  if (!floatDrag.on) startFloatGesture(mode, t.clientX, t.clientY);
  trackFloatTouch();
}

function onFloatTouchMove(ev: TouchEvent) {
  if (!floatDrag.on) return;
  const t = ev.touches[0];
  if (!t) return;
  ev.preventDefault();                 // 拖把手就是拖把手，别顺手把页滚了
  floatMoveAt = { x: t.clientX, y: t.clientY };
}

function onFloatTouchEnd() {
  endFloatGesture();
}

/* 用户口径 2026-11-22 之后浮窗与侧栏**不再互斥**（侧栏留着占位），所以原来那条
   「侧栏一开就收浮窗」已经删掉。它现在不只是多余，还会反噬 `openChatFloat`：
   `asideOpen` 是 pre-flush watcher —— 程序里先把它摆成 true、同一段代码里再点亮浮窗，
   回调落地时看到的是「侧栏开着 + 浮窗也开着」，于是把刚打开的浮窗顺手收掉。
   只剩一条：变窄屏（抽屉）→ 收浮窗 —— 那时浮窗没处可放。

   ⚠️ 原来还有一条「页签离开对话就收回浮窗」，用户口径 2026-11-22 明确去掉：
   浮窗是**独立的一扇窗**，点侧栏的目录/解读页签只是想看别的，不该顺手把它关掉。 */
watch(isNarrow, (v) => { if (v) chatFloat.value = false; });
/* 浮窗收起后别再占着 62 那一档，否则两张 FloatingCard 会一直停在 60（谁点都不往上走） */
watch(chatFloat, (v) => { if (!v && topLayer.value === 'chat') topLayer.value = 'note'; });

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

/*
 * 锚点归一：后端给公式/表格类 chunk 的块 id 是 row-<行 id>（见 knowledge_service 里
 * f"row-{row_id}" 那一支），它的后缀本来就是一正文行 id。
 *
 * 不拆这个前缀，结果就是「点了没落到那一段、整页亮一下」—— 行 id 查不到、block_rows
 * 里也没有这个键（那张表只解析解析块 id），只能退到按页闪（用户 2026-12-03 报的
 * 「标号 23 高亮整页」：那一条正好是公式 chunk，锚点是 row-p006-03）。
 * 所有跳转（对话引用、解读标号、目录、搜索、深链）最后都过 focusRow，所以归一放在这里。
 */
function rowAnchorId(id: string): string {
  return id.startsWith('row-') ? id.slice(4) : id;
}


/* ---- 公式级落点：同一行里可能有好多条公式，行 id 分不开它们 ----
 * 用户 2026-12-03 的「56、57 为什么是同一块地方」：一条公式 chunk 与它所在的**正文行**
 * 是两条独立引用，行级落点必然重合。所以引用额外带一条 LaTeX，能在这行里认出那枚 KaTeX
 * 时就把落点与闪烁都精确到它，认不出才退回整行。
 */

/** LaTeX 的签名：只留字母数字（KaTeX 的 annotation 与后端存的写法在空格/转义上会有差异） */
function formulaSig(text: string): string {
  return String(text || '').toLowerCase().replace(/[^0-9a-z]/g, '');
}

/** 在这一行里找那条公式的 KaTeX（找不到就返回 null，调用方退回整行） */
function findFormula(node: HTMLElement, latex: string): HTMLElement | null {
  const want = formulaSig(latex);
  /* 太短的 LaTeX 不做匹配：行内到处是 x、d、q 这种单符号公式，撞车比命中多。
     （实测把「d」当成命中，标到了一枚无关的行内符号上。） */
  if (want.length < 12) return null;
  let best: { el: HTMLElement; score: number } | null = null;
  for (const hit of Array.from(node.querySelectorAll<HTMLElement>('.katex'))) {
    const tex = hit.querySelector('annotation')?.textContent ?? hit.textContent ?? '';
    const sig = formulaSig(tex);
    if (sig.length < 12) continue;
    let score = 0;
    if (sig === want) score = 1e6;
    else if (sig.includes(want) || want.includes(sig)) score = Math.min(sig.length, want.length);
    if (score > (best?.score ?? 0)) best = { el: hit.closest<HTMLElement>('.katex-display') || hit, score };
  }
  return best ? best.el : null;
}

let targetFlashTimer: ReturnType<typeof setTimeout> | null = null;

/** 闪一条公式（与行闪同一节奏，同样用「先摘再挂 + 强制回流」让连点也能重播） */
function flashFormulaNode(node: HTMLElement): void {
  node.classList.remove('flash-target');
  void node.offsetWidth;
  node.classList.add('flash-target');
  if (targetFlashTimer) clearTimeout(targetFlashTimer);
  targetFlashTimer = setTimeout(() => {
    node.classList.remove('flash-target');
    targetFlashTimer = null;
  }, 1600);
}

/** 把某个节点停在**阅读线**上（行与公式共用同一套坐标换算） */
function scrollNodeToLine(el: HTMLElement, node: HTMLElement, smooth: boolean): void {
  const base = el.getBoundingClientRect().top - el.scrollTop;
  const top = node.getBoundingClientRect().top - base;
  el.scrollTo({ top: Math.max(0, top - readingLine() + 2), behavior: smooth ? 'smooth' : 'auto' });
}

async function focusRow(
  id: string,
  opts: { smooth?: boolean; save?: boolean; page?: number; flash?: boolean; latex?: string } = {},
) {
  const { smooth = true, save = true } = opts;
  /* 合成锚点 `fig:p<页>:n<图号>`（见 ReaderChat.gotoCitation）：解析成那张图的**图注行** id。
     历史引用没有行 id，只能靠这条兜底 —— 认不出来就退化成整页闪。 */
  const figAnchor = /^fig:p(\d+):n(\d+)$/.exec(id);
  if (figAnchor) {
    const hit = figures.value.find((f) => Number(f.page) === Number(figAnchor[1])
      && String(f.number ?? '') === figAnchor[2]);
    id = hit?.row_id || '';
  }
  /* 解读/检索给的锚点是**解析块 id**（`attentionisa-p01-b06`），正文行 id 却是 `p001-06` ——
     两套命名空间，后端为此下发 `block_rows`。不翻这一下，"点引用标号"就是空跳：
     `rowById` 查不到，下面会当成"锚点已失效"直接 toast（用户看到的正是"点了没反应"）。 */
  id = rowAnchorId(id);
  if (id && !rowById.value.has(id)) id = String(reader.value?.block_rows?.[id] ?? id);
  const known = rowById.value.get(id);
  /* 落点解析链的**再下一级**：`row_less_blocks` 里的块没有正文行（公式 / 插图 / 表格在正文里
     没有独立段落）。以前走到这里就再无任何信息可用了 —— 页内到底有什么看不出来，于是不管
     点到哪一块都只能"整页亮一下"（用户口径的"整页闪"就是这个）。现在后端显式给出落点：
     有 `page` 就到那一页，公式的 `latex` 还能把落点收进页内那一枚 KaTeX。
     只在**真的落不到行**时启用，能解析到行的路径一个字没动。 */
  const rowLess = known ? undefined : rowLessById.value.get(id);
  const rowLessPage = Number(rowLess?.page ?? 0) || 0;
  const rowLessLatex = String(rowLess?.latex ?? '');
  const page = opts.page ?? known?.page ?? (rowLessPage || undefined) ?? currentPage.value ?? 1;
  if (!id && !page) return;
  /* 明确"无行可落但知道页"的块不算锚点失效：它有页可去，交给下面那条既有的"到页 + 闪整页"
     分支即可（不再弹"锚点已失效"）。 */
  if (id && !known && !rowLessPage && opts.page === undefined) {
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
  /* 公式引用先在这一行里找那条公式：找到就把落点与闪烁都交给它（同一行里几枚公式才分得开） */
  const formulaNode = opts.latex && node && el ? findFormula(node, opts.latex) : null;
  if (formulaNode && el) {
    scrollNodeToLine(el, formulaNode, smooth);
    if (opts.flash) flashFormulaNode(formulaNode);
    if (save) saveProgressDebounced();
    return;
  }
  /* 无行可落的**公式块**：先滚到那一页（这一级以前根本不存在，所以只能整页闪），再用同一个
     `findFormula` / `flashFormulaNode` 在**页内**认出那枚 KaTeX，落点与闪烁都交给它。
     门槛与 `findFormula` 内部一致（`formulaSig` 去掉非字母数字后 < 12 不做匹配）：
     页内比行内更容易撞上 `x`、`d` 这种单符号公式。认不出来就落回下面的整页分支。 */
  if (!known && rowLessLatex && node && el && formulaSig(rowLessLatex).length >= 12) {
    scrollNodeToLine(el, node, smooth);
    await nextTick();
    const inPage = findFormula(node, rowLessLatex);
    if (inPage) {
      scrollNodeToLine(el, inPage, smooth);
      if (opts.flash) flashFormulaNode(inPage);
      if (save) saveProgressDebounced();
      return;
    }
  }
  if (node && el) {
    // 把目标行停在**阅读线**上（而不是居中）：滚动结束后"当前行"正好还是它，
    // 目录高亮才不会落到上一个章节。
    // 视口位置 = 行内容坐标 - scrollTop，要让它落在阅读线上就解 scrollTop = top - line；
    // 再 +2px 让行顶略微越过阅读线，避免浮点误差把"当前行"判给上一行。
    scrollNodeToLine(el, node, smooth);
  } else {
    node?.scrollIntoView({ behavior: smooth ? 'smooth' : 'auto', block: 'center' });
  }
  /* 闪一下"就是这块"：有对应行就闪那一行；**没有行 id 的引用**（插图/表格引用只带页码）闪整页 ——
     否则点引用标号会"跳过去但什么都没亮"（用户口径 2026-11-27）。 */
  if (opts.flash) void flashRow(known ? id : (page ? `page:${page}` : ''));
  if (save) saveProgressDebounced();
}

/**
 * 这条锚点上挂的高亮是不是**公式** → 它的 LaTeX（否则空串）。
 *
 * 为什么要它（用户口径 2026-12-06「定位怎么是整页定位」）：笔记/高亮只带行 id，而一行里
 * 可能有好几枚公式 —— 不把 LaTeX 带下去，`focusRow` 只能停在"这一行"，公式行的闪烁也就
 * 落到整行；带上下面的 `findFormula` / `flashFormulaNode` 才能精确到那一枚。
 */
function formulaLatexOf(anchorId: string): string {
  const hit = highlights.value.find((h) => h.block_id === anchorId);
  const text = String(hit?.text ?? '');
  if (!text) return '';
  // 与 `utils/domMarks.isFormulaText` 同一口径：没有汉字，且带 $ 或反斜杠命令
  return /[\u3400-\u9fff]/.test(text) || !(text.includes('$') || text.includes('\\'))
    ? ''
    : text;
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
  // 滚动交给深链那一支去做时，要求它**带动画**（点目录的页面视图是"带我过去"）
  if (!samePage || hadAnchor) deepLinkSmooth = true;
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
watch(railTab, (k) => {
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
/** 当前工具该用哪一排色（画笔 = 红橙黄绿青蓝紫；荧光笔 = 荧光色） */
function inkPalette(): string[] {
  return inkTool.value === 'marker' ? MARKER_COLORS : INK_COLORS;
}
/** 当前颜色是不是「色板以外的自选色」：是的话让最前面那枚彩虹点带选中环 */
function isCustomInkColor(): boolean {
  return !inkPalette().includes(activeInkColor());
}

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
let capDragFrom: { x: number; y: number; ox: number; oy: number; bottom: number;
  minX: number; maxX: number; minY: number; maxY: number } | null = null;
/** 拖拽期间的 rAF 合并：一帧只写一次位置、量一次布局（平板 pointermove 有 120Hz） */
let capMoveRaf = 0;
let capMoveNext: { x: number; y: number } | null = null;

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
let headerHCache = { at: 0, h: 0 };
function headerH(): number {
  const now = performance.now();
  if (now - headerHCache.at > 1000) {
    headerHCache = {
      at: now,
      h: Number.parseInt(getComputedStyle(document.documentElement).getPropertyValue('--header-h') || '0', 10) || 0,
    };
  }
  return headerHCache.h;
}

/**
 * 提示条避开胶囊：拖拽时**直接给底边**（按偏移算），不再每帧量一次 DOM ——
 * getComputedStyle + getBoundingClientRect 每帧各一次等于把整篇正文重新排版一遍，
 * 平板上就是拖一下卡一下。
 */
function applyToastDrop(capBottom: number): void {
  if (typeof document === 'undefined') return;
  const header = headerH();
  let bottom = capBottom;
  // 二级菜单（颜色 / 粗细）展开时正好在胶囊下面：提示条要多让一层，否则会压在颜色行上。
  // 这里按**状态**估算高度（颜色行 + 粗细行 ≈ 80px），不依赖 DOM 查询的时序。
  if (inkOptionsOpen.value) bottom += 88;
  const drop = Math.max(0, Math.round(bottom + 8 - (header + 12)));
  document.documentElement.style.setProperty('--toast-drop', drop + 'px');
}

function syncToastDrop(): void {
  const el = capsuleEl.value;
  if (!el) return;
  applyToastDrop(el.getBoundingClientRect().bottom);
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
  /* 从胶囊上**除按钮以外**的任何地方都能起拖（用户口径：拖胶囊会把 PDF 那边选中、页面还跟着滚）。
     以前要求正好按到那 12px 的抓手：偏一点点就落进正文 —— 那一下既开始拖选 PDF、又开始滚页面。
     现在胶囊本身（含内边距 / 抓手）都是把手，按钮、输入框、二级菜单照旧是点它们自己。 */
  const target = ev.target as HTMLElement | null;
  if (target?.closest?.('button, a, input, label, [data-ink-options]')) return;
  const rect = el.getBoundingClientRect();
  capDrag.value = true;
  /* 拖动期间**关掉 PDF 文字层那一路选区**（用户口径 2026-12-03：拖胶囊会在 PDF 里带出一段选中）。
     与浮层那一套是**同一份实现**（utils/floatDrag）：给 <html> 挂一个类，只关 PDF 文字层，
     不动我们自己界面里的可选文字 —— 不需要第二套做法。 */
  lockFloatSelection(true);
  clearDocSelection();
  /* 再加一道全局闸：拖动期间整个文档**不许选字、不许被触摸滚走**（见 base.css 的 `html.cy-cap-drag`）。
     只锁 PDF 文字层不够 —— 译文行、侧栏、目录都还能被拖出一段选区，浏览器随之自动滚动。 */
  document.documentElement.classList.add('cy-cap-drag');
  /* 只 preventDefault pointerdown 挡不住鼠标输入：浏览器照样会派发 mousedown，把「按住抓手拖」
     当成一次文本拖选的起点（浮层那边 2026-11-19 踩过同一条，见 onFloatCardMouseDown）。 */
  ev.preventDefault();
  el.setPointerCapture?.(ev.pointerId);
  capDragFrom = {
    x: ev.clientX, y: ev.clientY, ox: capOffset.x, oy: capOffset.y, bottom: rect.bottom,
    ...dragLimits(rect, capsuleRegion()),
  };
}

function onCapsuleMouseDown(ev: MouseEvent): void {
  const el = capsuleEl.value;
  const target = ev.target as HTMLElement | null;
  if (!el || (target !== el && !target?.closest?.('[data-cap-grip]'))) return;
  if (target?.closest?.('button, a, input, label')) return;   // 按到按钮上就是点按钮
  ev.preventDefault();
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
  // 平板的 pointermove 能有 120Hz：合并到一帧一次，位置只写一次、布局只量一次
  capMoveNext = { x: ev.clientX, y: ev.clientY };
  if (!capMoveRaf) capMoveRaf = requestAnimationFrame(applyCapsuleMove);
}

function applyCapsuleMove(): void {
  capMoveRaf = 0;
  const from = capDragFrom;
  const pt = capMoveNext;
  capMoveNext = null;
  if (!capDrag.value || !from || !pt) return;
  clearDocSelection();
  capOffset.x = Math.min(from.maxX, Math.max(from.minX, from.ox + (pt.x - from.x)));
  capOffset.y = Math.min(from.maxY, Math.max(from.minY, from.oy + (pt.y - from.y)));
  /* ⚠️ 这里**不能**每帧改 --toast-drop：那是写在 <html> 上的自定义属性，一改整篇文档
   * （22 页正文）都要重新算样式 —— 实测每帧多出 200ms。提示条位置在松手时同步一次就够。 */
  if (inkOptionsOpen.value) placeInkOptions();
  if (capPanel.value) placeCapPanel();  // 面板放不下会翻到胶囊上方
}

function onCapsuleUp(): void {
  if (!capDrag.value) return;
  if (capMoveRaf) { cancelAnimationFrame(capMoveRaf); capMoveRaf = 0; }
  if (capMoveNext) {                 // 松手时把最后一帧补上，别丢最后一小段位移
    applyCapsuleMove();
  }
  capMoveNext = null;
  capDrag.value = false;
  lockFloatSelection(false);
  document.documentElement.classList.remove('cy-cap-drag');
  capDragFrom = null;
  try { sessionStorage.setItem(CAP_POS_KEY, JSON.stringify({ x: capOffset.x, y: capOffset.y })); }
  catch { /* 隐私模式：位置只在本次内存里 */ }
  // 拖完才同步提示条让位（拖拽期间改 <html> 的自定义属性 = 全文档重算样式，太贵）
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

// 胶囊收起只影响胶囊：手柄跟着**DOM 选区**走（选区还在就一直显示，滚动时跟着重新定位）
watch(() => selection.show, () => { refreshSelHandles(); });

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
  // 放大查看器的飞回原位：capture 阶段抢在 @zoom（冒泡）之前记下被点那张图的矩形
  window.addEventListener('pointerdown', onZoomOriginDown, true);
  // 本机阅读统计：这次算一次"打开"，并在页面可见期间累加时长
  startReadStats();
  document.addEventListener('visibilitychange', onReadVisibility);
  document.addEventListener('keydown', onInkKey);
  document.addEventListener('selectionchange', scheduleSelSync);
  document.addEventListener('contextmenu', onDocContextMenu, true);
  window.addEventListener('scroll', scheduleSelSync, { capture: true, passive: true });
  window.addEventListener('resize', scheduleSelSync);
  document.addEventListener('fullscreenchange', onFullscreenChange);
  document.addEventListener('dragstart', onReaderDragStart, true);
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
  // 上次看的时候开着哪些浮层：笔记卡先摆（数据已就绪），再摆对话浮层
  await restoreNoteCards();
  restoreBlockOverlays();       // 表格 / 公式 / 图片浮层同样恢复到刷新前那一摞
  await restoreChatFloat();
  fetchTasks();
  taskTimer = setInterval(() => { if (hasRunningTask.value) fetchTasks(); }, 4000);
});

/* 阅读位置最后再存一次：滚动是 900ms 防抖存的，滚完立刻切标签页会把最后那一下漏掉 ——
 * 「切走再回来，读到哪没记住」就是这么来的（用户口径 2026-11-26）。
 * 必须放 onBeforeUnmount：onUnmounted 里模板 ref 已经置空，读不到 scroller 的 scrollTop，
 * 那样会把 scroll_ratio 写成 0（比不存更糟）。 */
onBeforeUnmount(() => {
  void saveProgressNow();
  window.removeEventListener('pointerdown', onZoomOriginDown, true);
});

onUnmounted(() => {
  void flushInk();
  setInkNotesListener(null);
  // 离开阅读器就把提示条的位置放回去（别的页面没有悬浮胶囊）
  try { document.documentElement.style.removeProperty('--toast-drop'); } catch { /* 忽略 */ }
  if (immersive.value) setImmersive(false);
  document.removeEventListener('keydown', onInkKey);
  document.removeEventListener('selectionchange', scheduleSelSync);
  document.removeEventListener('contextmenu', onDocContextMenu, true);
  window.removeEventListener('scroll', scheduleSelSync, { capture: true } as EventListenerOptions);
  window.removeEventListener('resize', scheduleSelSync);
  document.removeEventListener('fullscreenchange', onFullscreenChange);
  document.removeEventListener('dragstart', onReaderDragStart, true);
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
  railTab.value = 'search';
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
/** 惰性挂载闩（用户口径 2026-11-23）：第一次真正用到对话（进对话页签或浮起）才挂 ReaderChat。
 *  挂上之后永不卸载（v-show 收起）—— 实例仍然常驻，只是把首次挂载往后推，
 *  避免每打开一篇论文就白拉一次会话列表。 */
const chatWarm = ref(false);
watch(
  () => [railTab.value, chatFloat.value] as const,
  ([tab, floating]) => {
    if (tab === 'chat' || floating) chatWarm.value = true;
  },
  { immediate: true },
);
async function focusSearchBox() {
  if (!asideVisible.value) await toggleAside();
  railTab.value = 'search';
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
    if (t?.closest?.('input, textarea, [contenteditable="true"]')) return;
    /* 浮层（对话浮窗 / 笔记卡 / 解释卡）里要的是**浏览器原生右键菜单** ——「复制」就在里面
       （用户口径 2026-11-24 第 2 条：想在浮层里选字，用系统菜单复制）。
       正文里仍要挡住默认动作：否则浏览器会把选区折叠到点击处，划词胶囊那几项会整组消失。 */
    /* 侧栏也放行：笔记侧栏的编辑框就在里面，右键要的是原生「复制 / 粘贴」 */
    if (t?.closest?.('.chat-host, [data-float-card], .reader-aside')) return;
    ev.preventDefault();
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
/**
 * 系统取色器（菜单里的「更多颜色」）是否正开着。
 *
 * 那个对话框是浏览器自己的 UI，**一开窗口就失焦** → 我们挂在 window 上的 blur 会调 hideMenu →
 * 菜单被 v-if 卸掉，取色器的 input 事件就再没人接 —— 用户选完颜色什么都没发生，
 * 看起来就是「点了更多颜色，选区/颜色一起丢了」（用户口径 2026-12-02）。
 * 判据用 activeElement：对话框开着时被聚焦的就是那枚 input[type=color]。
 */
function colorPickerOpen(): boolean {
  const el = document.activeElement as HTMLElement | null;
  return !!el?.matches?.('input[type="color"]');
}

const hideMenu = () => {
  if (colorPickerOpen()) return;      /* 取色器开着：先别收，等它把颜色交回来（见 pickCustom 里的 blur） */
  selection.show = false;
  ctxMenu.open = false;
  ctxMenu.rect = null;
  // ⚠️ 这里**不**收手柄：菜单和两端手柄是两码事（选区还在，手柄就该在）
};
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
  /* 拖手柄期间、以及刚松手的那三百毫秒，收菜单这件事交给拖拽那条线自己管：
   * 自动滚动的 scroll 事件会在松手之后还涌进来几帧，这里一收就把刚摆好的工具条
   * 又摘掉（用户看到的就是"自动滚完松手，菜单不出现"）。 */
  if (draggingHandle || performance.now() < suppressScrollCloseUntil) return;
  citePop.open = false;      // 引用标号的浮层贴着正文，一滚就对不上了（与划词菜单同一处理）
  cancelCiteOpen();          // 还没弹出来的那一次也别弹了（锚点已经跟着内容跑掉了）
  hideMenu();
}
function onDocPointerDown(ev: PointerEvent) {
  /* 右键 / 中键**不归这条线管**：菜单由 `contextmenu` 那一支自己摆（见 onContentContextMenu）。
     这里管的是"单击别处 = 取消这次选取"，而输入框里（笔记编辑态 / 对话输入框 / 搜索框）
     刚框好、准备右键复制的那一段，一清就没了 —— 用户口径 2026-11-27。 */
  if (ev.button !== 0) return;
  const target = ev.target as HTMLElement | null;
  if (target?.closest?.('[data-selection-menu], [data-ctx-menu]')) return;
  // 拖两端手柄：不是"点了别处"，别把菜单/选区/手柄一起收掉（那会把拖拽当场打断）
  if (target?.closest?.('.sel-handle')) return;
  /* 单击别处 = 取消这次选取（用户口径 2026-11-26 第三轮）。
     ⚠️ 这里**只管鼠标**：触摸的 pointerdown 在"只是想滚一下页面"时也会先来一发，
        在这儿清就等于「滚一下就丢选区」（用户口径本轮：触摸滚动不影响选区）。
        触摸的「点别处取消」改由 onContentTouchEnd 的点击判定做 —— 那里知道这一下
        到底有没有位移，滚动不算点。 */
  if (touchSelActive.value && ev.pointerType === 'mouse') clearTouchSelection();
  /* 点进输入框（对话 / 笔记 / 搜索…）：**先把正文的选区与两枚手柄收掉再让焦点落下**。
     手柄是 44px 的抓取区、层级 330，压在输入框旁边时手指一点就落到它身上，focus 直接被吃掉；
     收掉之后这一次点击就能正常聚焦、键盘也弹得出来（用户口径 2026-11-26 第二轮）。 */
  if (target?.closest?.('input, textarea, [contenteditable="true"]')) {
    window.getSelection()?.removeAllRanges();
    selection.text = '';
    selection.rect = null;
    selection.show = false;
    hideSelHandles();
    setColSide('');
  }
  /* 鼠标选区还有效时点别处 = 取消这次选择：**选区、手柄、胶囊一起收**。
     ⚠️ 触摸不再走这条（用户口径 2026-10-18）：手指按下去往往只是要滚动，靠这里的
     pointerdown 取消选取，等于"滚一下就没了"。触摸改由 onContentTouchEnd 的连续双击判定。 */
  if (touchSelActive.value && ev.pointerType === 'mouse') {
    window.getSelection()?.removeAllRanges();
    selection.show = false;
    hideSelHandles();
  }
  /* 「设置」面板：**除了它自己与打开它的那枚按钮，点哪儿都收**（用户口径 2026-10-20：点其他位置要自动消失）。
     老判据是 `!closest('.reader-toolbar')` —— 只挡了「点正文」这一种，于是点胶囊里的另一枚按钮
     （画笔 / 荧光笔 / 撤销…）面板还赖着，和随后弹出的二级菜单叠在一起。
     现在按「是不是面板 / 它的触发按钮」来判，胶囊上的其它按钮一律算「别处」。 */
  if (capPanel.value) {
    const inPanel = !!target?.closest?.('[data-cap-panel-body="settings"]');
    const onTrigger = !!target?.closest?.('[data-cap-panel="settings"]');
    if (!inPanel && !onTrigger) capPanel.value = '';
  }
  /* 引用标号的浮层：点别处就收。⚠️ 点**浮层里面**（条目/跳转图标）绝不能收 ——
     一收就把元素从 DOM 上摘了，那次 click（`window.open` 在它上面）根本不会再触发。 */
  if (!target?.closest?.('[data-cite-pop]')) citePop.open = false;
  cancelCiteOpen();          // 点别处了：还没弹出来的那一次也取消
  hideMenu();
  /* 画笔 / 橡皮的二级菜单：**按下即收**（用户口径：点其他地方要立刻收回）。
     ⚠️ 这个监听是 **capture 阶段**的（`document.addEventListener('pointerdown', …, true)`），
     所以菜单自己那条 `@pointerdown.stop` **挡不住它** —— 上一版就是在这里把菜单收掉，
     色块在 click 到达之前就被卸载，表现是「点颜色 / 点粗细没反应」。
     现在显式排掉落在菜单里的那一下（`[data-ink-options]` 范围内一律不动）。 */
  const inInkMenu = !!target?.closest?.('[data-ink-options]');
  if (!inInkMenu) {
    if (inkOptionsOpen.value) inkOptionsOpen.value = false;
    if (eraserClearOpen.value) eraserClearOpen.value = false;
  }
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
  /** 贴在**选区上方**打开（触摸长按时用：别盖住选区，也别盖住两端手柄） */
  above: false,
  /** 触摸屏用 **Android 式悬浮工具条**（一排图标，浮在选区上方），桌面仍是贴着手指的小下拉 */
  compact: false,
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

/**
 * 正文里**禁止原生拖动**（Edge 的『拖动搜索』就是这么来的）。
 *
 * 用鼠标/触摸按住一段**已选中的文字**再拖，Blink 会把它当成搬运行内内容：Edge 上直接弹出
 * 『拖放搜索』浮层（用户口径 2026-10-17 报的正是这个），触摸长按之后想扩大选区也会被它抢走手势。
 * 捕获阶段一律 preventDefault：正文里不需要原生拖放（下载图片走右键菜单的『另存为』）。
 */
function onReaderDragStart(ev: DragEvent): void {
  const target = ev.target as Node | null;
  const inReader = !!(target as HTMLElement | null)?.closest?.('.reader-content');
  const sel = window.getSelection();
  if (inReader || (sel && !sel.isCollapsed)) ev.preventDefault();
}
/* ------------------------------ 触摸长按 = 右键 ------------------------------
 * 触摸屏上长按默认走**系统右键**：iOS 弹 callout、安卓弹选区工具条 —— 和我们自己的右键菜单
 * 是两套东西（用户口径 2026-10-17）。这里三件事一起做：
 *   ① 正文禁用 `-webkit-touch-callout`（iOS 不再弹 callout，见样式）；
 *   ② 正文的 `contextmenu` 一律 `preventDefault()`（系统菜单不出场）；
 *   ③ 自己识别长按（480ms、位移 <10px）→ 选中落点那个词 → 按右键那套逻辑开菜单。
 */
const LONG_PRESS_MS = 480;
/**
 * 长按之后这段时间内**不许浏览器自己划词**。
 *
 * 触摸长按本身就是浏览器的"选中一个词"手势：我们在 480ms 时已经把选区摆好了，浏览器随后
 * 还会再摆一次自己的（把我们的顶掉），抬手又把它折叠 —— 用户看到的就是
 * "长按后选中焦点闪一下就没了"。所以从长按生效那一刻起压掉 selectstart，
 * 并忽略紧跟着的那一下 click。
 */
let suppressNativeSelectUntil = 0;
let ignoreClickUntil = 0;
/* 触摸手势：滚动**不取消**选取，只有"连续双击"才取消（用户口径 2026-10-18，对齐移动端手感） */
let touchStartPos: { x: number; y: number } | null = null;
let lastTapPos: { x: number; y: number; at: number } | null = null;
const TAP_MOVE_MAX = 10;      // 位移小于它 = 一次"点"，不是滚动
const DOUBLE_TAP_MS = 320;
const DOUBLE_TAP_DIST = 44;
/** 触摸手势期间（含双击）一律不让浏览器自己划词：选区只由我们的长按 + 手柄产生 */
let touchSelectGuardUntil = 0;
/** 长按造出来的那段选区：抬手时浏览器会把它折叠，这里留一份用来放回去 */
let longPressRange: Range | null = null;
/** 下一次右键是"我们自己派的"（触摸长按 / 拖完手柄重开菜单）：菜单要贴在选区上方 */
let touchCtxNext = false;
let longPressTimer: number | null = null;
let longPressFrom: { x: number; y: number; target: HTMLElement | null } | null = null;

function cancelLongPress(): void {
  if (longPressTimer) { clearTimeout(longPressTimer); longPressTimer = null; }
  longPressFrom = null;
}

/** 落点处那个『词』的范围：西文按字母/数字，中文按连续汉字（长按一下就想选一个词） */
function wordRangeAt(x: number, y: number): Range | null {
  const caret = caretAt(x, y);
  if (!caret) return null;
  const node = caret.node;
  if (node.nodeType !== 3) return null;
  const text = node.textContent || '';
  const offset = Math.min(caret.offset, text.length);
  let start = offset;
  let end = offset;
  /* 中文没有空格：优先用 Intl.Segmenter 找一个**词**（浏览器不支持就退回"连续汉字/字母"扫描） */
  const Segmenter = (Intl as unknown as { Segmenter?: new (l?: string, o?: object) => any }).Segmenter;
  if (Segmenter) {
    try {
      const parts = new Segmenter(undefined, { granularity: 'word' }).segment(text);
      for (const part of parts) {
        const seg = String(part.segment ?? '');
        if (part.index <= offset && offset <= part.index + Math.max(1, seg.length) && part.isWordLike !== false) {
          start = part.index;
          end = part.index + seg.length;
          break;
        }
      }
    } catch { /* 老浏览器：走下面的兜底 */ }
  }
  if (end <= start) {
    const isWordChar = (ch: string) => /[A-Za-z0-9_'-]/.test(ch) || /[\u4e00-\u9fff]/.test(ch);
    start = offset;
    end = offset;
    if (!(start > 0 && !isWordChar(text[start - 1] || ''))) {
      while (start > 0 && isWordChar(text[start - 1])) start -= 1;
    }
    while (end < text.length && isWordChar(text[end])) end += 1;
  }
  if (end <= start) return null;
  const range = document.createRange();
  range.setStart(node, start);
  range.setEnd(node, end);
  return range;
}

/* ------------------------------ 触摸选区的两个手柄 ------------------------------
 * 触摸屏上用鼠标那种『拖一下选一段』是没有的；长按选出一个词之后，得能像原生那样**拖两头的
 * 小圆点**把范围撑开（用户口径 2026-10-17：长按之后没有拖动选择条）。这里自己画两个手柄：
 * 位置取当前选区的首/末矩形，拖动时用 caretRangeFromPoint 找到新焦点，另一端固定。
 */
const selHandles = ref<{ start: { x: number; y: number }; end: { x: number; y: number } } | null>(null);
/**
 * 这对是不是『触摸选出来的选区』（决定手柄该不该在）。
 *
 * ⚠️ 手柄的生死**只看两件事**：这个标记 + DOM 选区还在不在。
 * 以前它跟着 selection.show（划词胶囊的开关）与 hideMenu() 走 —— 于是点一下胶囊、
 * 或收一次菜单，选区明明还在、两端手柄却没了（用户报的正是这个）。
 */
const touchSelActive = ref(false);
let selEndA: { node: Node; offset: number } | null = null;
let selEndB: { node: Node; offset: number } | null = null;
/**
 * 拖拽期间**不动的那一端**（按下手柄时锁定一次）。
 *
 * ⚠️ 千万不能每次移动都从 selEndA / selEndB 现取：那两个值会跟着选区实时刷新，
 * 于是『固定端』每次都被换成上一帧的焦点 —— 实测选区在 2 字 / 134 字之间来回跳，
 * 看起来就是『拖不动』（用户口径 2026-10-17 第 1 条）。
 */
let dragAnchor: { node: Node; offset: number } | null = null;
let draggingHandle: 'start' | 'end' | null = null;
/** 拖拽期间允许的取值范围（锚点所在那一列，见 `selColumnBand`） */
let dragColumnBand: { left: number; right: number } | null = null;
/** 拖手柄时手指停在哪儿（边缘自动滚动要按它反复重算插入点） */
let dragPoint: { x: number; y: number } | null = null;
/** 手指离上下边缘这么近就开始自动滚动，越靠边滚得越快 */
const HANDLE_SCROLL_EDGE = 88;
const HANDLE_SCROLL_MAX = 18;
let handleScrollRaf = 0;
/** 松手后这一小段时间内忽略 scroll 触发的"收菜单"（见 onAnyScroll） */
let suppressScrollCloseUntil = 0;
/** 自动滚动时选区重算的节流：滚够这么多像素、或过了这么久才重算一次 */
let scrollSinceSelUpdate = 0;
let lastSelUpdateAt = 0;

/**
 * 触摸选区**属于哪一列**（对照模式下左右各是一个 `.cell`）。
 *
 * 用户口径 2026-11-19：拖着手柄往页尾走（还没真的进下一页）时，末端手柄会跳到**左边的页面**上。
 * 原因是 `caretRangeFromPoint` 在页与页之间的空白/新页开头会落到"最近的文字节点"上 —— 那往往是
 * 下一页**左栏**的文字，于是选区末端悄悄换了一列，`getClientRects()` 的最后一条也就落在左边。
 * 有了这条横向范围：①记在选区起点那一列；②拖拽时插入点出了这列就整帧忽略；③摆手柄时也只用
 * 这一列里的行矩形 —— 手柄永远跟着**看得见的那段高亮**走。
 */
let selColumnBand: { left: number; right: number } | null = null;

function columnBandOfNode(node: Node | null | undefined): { left: number; right: number } | null {
  if (!node) return null;
  const el = node.nodeType === 1 ? (node as HTMLElement) : node.parentElement;
  const cell = el?.closest?.('.cell') as HTMLElement | null;
  if (!cell) return null;                       // 单栏视图：没有 `.cell`，不限制
  const b = cell.getBoundingClientRect();
  return { left: b.left - 8, right: b.right + 8 };
}

/** 选区是否落在**正文 / PDF 文字层**这一面（Markdown 正文、对照行、PDF 文字层都算）。
 *
 * 对话浮层 / 笔记浮层 / 侧栏 / 工具栏里选字**不算**（用户口径 2026-11-26）：那几块里用户
 * 要的是系统原生的复制菜单，我们的两枚手柄与划词胶囊留在那儿只会挡在前面抢焦点 ——
 * 手指点输入框点到的是手柄，键盘就弹不出来。
 */
function selectionInContent(node: Node | null | undefined): boolean {
  if (!node) return false;
  const el = node.nodeType === 1 ? (node as HTMLElement) : node.parentElement;
  if (!el) return false;
  if (el.closest('.md-doc')) return true;                    // Markdown 弹层：允许
  if (el.closest('.chat-host, [data-float-card], .reader-aside, .reader-toolbar')) return false;
  return Boolean(el.closest('.reader-content'));
}

/** 手柄的 44px 热区半径（`margin: -22px` 那一半）。见 placeSelHandles 的夹取。 */
const SEL_HANDLE_R = 22;

/**
 * 摆两枚手柄：**中心必须夹在正文容器里**。
 *
 * 手柄是 44×44 的抓取区（`.sel-handle`），比看得见的圆点大得多。选区落在正文最左/最右那一列时，
 * 这个热区会跨出 `.reader-content` 盖到旁边的对话容器 / 侧栏上 —— 手指点输入框点到的是手柄，
 * 焦点拿不到、键盘弹不出来（用户口径 2026-11-26 第二轮：单击输入框取消不掉手柄）。
 * 夹一次就够了：手柄仍然贴着那一行的上/下沿，只是不会再伸进别人的地盘。
 */
function placeSelHandles(start: { x: number; y: number }, end: { x: number; y: number }) {
  const box = scroller.value?.getBoundingClientRect() ?? null;
  const clamp = (p: { x: number; y: number }) => {
    if (!box) return p;
    return {
      x: Math.min(Math.max(p.x, box.left + SEL_HANDLE_R), box.right - SEL_HANDLE_R),
      y: Math.min(Math.max(p.y, box.top + SEL_HANDLE_R), box.bottom - SEL_HANDLE_R),
    };
  };
  selHandles.value = { start: clamp(start), end: clamp(end) };
}

/** 从当前 DOM 选区刷新两个手柄的位置（也刷新 a/b 两端点） */
function refreshSelHandles(): void {
  if (!touchSelActive.value) { selHandles.value = null; return; }
  const sel = window.getSelection();
  /* 选区跑到正文以外（浮层 / 侧栏里选字）→ 手柄与胶囊一起收，别留在那儿挡焦点 */
  if (sel && !sel.isCollapsed && !selectionInContent(sel.anchorNode)) {
    touchSelActive.value = false;
    selHandles.value = null;
    selection.show = false;
    return;
  }
  if (!sel || sel.isCollapsed || !sel.rangeCount) {
    /* 高亮重绘 / 局部 re-render 会把 DOM 选区弄折，selectionchange 一起跟着来。
       这时**不要**直接把手指柄收掉（用户看到的就是『手柄自己消失』）：
       先把长按那一段 range 放回去；节点已经换了（真被重绘）才一起收。 */
    const saved = longPressRange;
    if (saved && saved.startContainer.isConnected && saved.endContainer.isConnected) {
      try {
        sel?.removeAllRanges();
        sel?.addRange(saved);
      } catch { /* 放不回去就往下走 */ }
    }
    if (sel && sel.rangeCount && !sel.isCollapsed) {
      // 放回去了：继续按新位置摆手柄
      const r2 = sel.getRangeAt(0);
      const rects2 = Array.from(r2.getClientRects()).filter((r) => r.width > 0 && r.height > 0);
      if (rects2.length) {
        placeSelHandles(
          { x: rects2[0].left, y: rects2[0].top },
          { x: rects2[rects2.length - 1].right, y: rects2[rects2.length - 1].bottom },
        );
        selEndA = { node: r2.startContainer, offset: r2.startOffset };
        selEndB = { node: r2.endContainer, offset: r2.endOffset };
        return;
      }
    }
    // 真没了：手柄和胶囊一起收，别留一枚孤零零的胶囊（用户口径 2026-10-17）
    touchSelActive.value = false;
    selHandles.value = null;
    selection.show = false;
    ctxMenu.open = false;
    ctxMenu.rect = null;
    return;
  }
  const range = sel.getRangeAt(0);
  const rects = Array.from(range.getClientRects()).filter((r) => r.width > 0 && r.height > 0);
  if (!rects.length) { selHandles.value = null; return; }
  /* 这一列（每次重算：跨页、滚页都不影响左右两列的位置，取一次就够用） */
  selColumnBand = columnBandOfNode(range.startContainer);
  /* 只拿**本列里**的行矩形摆手柄：跨列时最后那一条会落在隔壁页/隔壁栏上（见 selColumnBand）；
     万一全被滤掉（选区真的只剩别列）就退回原样，不能把两条手柄弄没。 */
  const inBand = selColumnBand
    ? rects.filter((r) => r.right > selColumnBand!.left && r.left < selColumnBand!.right)
    : rects;
  const usable = inBand.length ? inBand : rects;
  const first = usable[0];
  const last = usable[usable.length - 1];
  placeSelHandles(
    { x: first.left, y: first.top },
    { x: last.right, y: last.bottom },
  );
  selEndA = { node: range.startContainer, offset: range.startOffset };
  selEndB = { node: range.endContainer, offset: range.endOffset };
}

function hideSelHandles(): void {
  stopHandleAutoScroll();
  selHandles.value = null;
  draggingHandle = null;
  selEndA = null;
  selEndB = null;
  selColumnBand = null;
  touchSelActive.value = false;
}

/**
 * 拖手柄时的插入点：**先定行，再定字**。
 *
 * 两件事一起解（用户口径 2026-12-02）：
 *   ① 起点手柄画在行的**上沿**，而这条边同时也是上一段最后一行的下沿 ——
 *      caretRangeFromPoint 落在分界线上时偏向哪一行，是按"最近的插入点"算的，实测系统性地偏上。
 *      于是手指按在"本行首"却把焦点交给上一段末（横着拖多远都选不回本行）；
 *      在每段第一行上更狠：插入点一路被算到这段开头之上，选区一口气从那儿连到锚点
 *      —— 用户看到的正是「第一行必定选到所有内容」。
 *   ② 起点手柄横向拖不动（2026-10-17）：一次 caret 只给出"行首"，要靠行中线迭代横坐标才跟手。
 *
 * 现在的做法分三步：
 *   1. 在手指上下各取几个候选（±0/4/8/12px），每个候选都量出**它所在那一行**的矩形；
 *   2. 按手柄方向挑行 —— 起点取"最靠上、且底边在手指**之下**"的那一行，
 *      终点取"最靠下、且顶边在手指**之上**"的那一行。分界线因此不再有歧义：
 *      手指压在哪一行、或者刚好压在两行之间，拿到的都是**下面/上面那一行**，不会跨段；
 *      全都不满足（手指已经拖到文字之外）才退化成"最靠末/最靠初"那一行；
 *   3. 在这一行的**中线**上按同一个 x 再取一次，横坐标就贴着手指走（第 ② 条）。
 * 为什么不用固定 bias：手柄的 y 是**行矩形**的上下沿，而行矩形常常带着行距甚至单元格内边距，
 * 手指到"文字那一行"可能差 10px 以上，写死 6px 在该偏的时候仍然会选错行。
 */
/* 探针分两档：先打三个（绝大多数情况够用，一次拖动只做 6 次 DOM 读）；
 * 都没有落在"手指那一侧"的行时再加打远的几个 —— 手柄的 y 是**行矩形**的上下沿，
 * 行矩形可能带着行距 / 单元格内边距，手指到真正那一行有时差 16px 以上。 */
const HANDLE_PROBES_NEAR = [0, 8, -8];
const HANDLE_PROBES_FAR = [4, 12, 16, 20, -12, -16];

/** 插入点所在那个"字"的矩形（行顶 / 行底 / 行高都从它读） */
function caretCharRect(caret: { node: Node; offset: number }): DOMRect | null {
  const text = caret.node.textContent ?? '';
  const at = Math.min(caret.offset, text.length);
  const probe = document.createRange();
  try {
    if (at < text.length) { probe.setStart(caret.node, at); probe.setEnd(caret.node, at + 1); }
    else if (at > 0) { probe.setStart(caret.node, at - 1); probe.setEnd(caret.node, at); }
    else return null;
  } catch { return null; }
  const r = probe.getBoundingClientRect();
  return r.height > 0 ? r : null;
}

type CaretCand = { node: Node; offset: number; rect: DOMRect };

/** 按手柄方向挑行：起点取"底边在手指之下"的最靠上那行，终点取"顶边在手指之上"的最靠下那行 */
function pickCaretLine(cands: CaretCand[], y: number, side: 'start' | 'end'): CaretCand | null {
  let pick: CaretCand | null = null;
  if (side === 'start') {
    for (const c of cands) if (c.rect.bottom > y + 1 && (!pick || c.rect.top < pick.rect.top)) pick = c;
  } else {
    for (const c of cands) if (c.rect.top < y - 1 && (!pick || c.rect.bottom > pick.rect.bottom)) pick = c;
  }
  return pick;
}

function collectCands(x: number, y: number, steps: number[], into: CaretCand[]): void {
  for (const dy of steps) {
    const c = caretAt(x, y + dy);
    if (!c || c.node.nodeType !== Node.TEXT_NODE) continue;
    const rect = caretCharRect(c);
    if (rect) into.push({ node: c.node, offset: c.offset, rect });
  }
}

function caretAtLine(x: number, y: number, side: 'start' | 'end' = 'end'): { node: Node; offset: number } | null {
  const cands: CaretCand[] = [];
  collectCands(x, y, HANDLE_PROBES_NEAR, cands);
  let pick = pickCaretLine(cands, y, side);
  if (!pick) {
    collectCands(x, y, HANDLE_PROBES_FAR, cands);
    pick = pickCaretLine(cands, y, side);
  }
  if (!pick) {
    if (!cands.length) return null;
    /* 全在手指的同一侧（已经拖到文字外面）：退化成最靠末 / 最靠初那一行，
       但**绝不**退化成"文档开头"—— 那正是"第一行选到所有内容"的来源。 */
    pick = side === 'start'
      ? cands.reduce((a, b) => (a.rect.bottom >= b.rect.bottom ? a : b))
      : cands.reduce((a, b) => (a.rect.top <= b.rect.top ? a : b));
  }

  /* 行内精修：同一个 x 打在这一行的中线上，横坐标才跟手（换行不换行由上面的行判定负责） */
  const cy = (pick.rect.top + pick.rect.bottom) / 2;
  const better = caretAt(x, cy);
  if (better && better.node.nodeType === Node.TEXT_NODE) {
    const r2 = caretCharRect(better);
    if (r2 && Math.abs(r2.top - pick.rect.top) < 2) return { node: better.node, offset: better.offset };
  }
  return { node: pick.node, offset: pick.offset };
}

/** 选区变了 / 页面滚了：手柄要跟着文字走（否则"选区还在、手柄跑掉/消失"） */
let selSyncRaf = 0;
function scheduleSelSync(): void {
  // 拖手柄时由拖拽那条线自己刷新（下面 handleAutoScrollTick 里节流调用），
  // 滚动事件每次再插一脚就变成"每帧重算两遍"，自动滚动会掉帧。
  if (draggingHandle) return;
  if (!touchSelActive.value || selSyncRaf) return;
  selSyncRaf = requestAnimationFrame(() => {
    selSyncRaf = 0;
    refreshSelHandles();
  });
}

/**
 * 拖手柄：pointer 与 touch **两条线一起收**（不再依赖 pointer capture）。
 *
 * 为什么：手指在手柄上横向移动时，浏览器很容易把它判成"横向滑动"并发 `pointercancel`，
 * `pointermove` 就此断流 —— 表现是横向拖起点手柄纹丝不动（用户口径 2026-10-17 第 1 条）。
 * 现在：`pointercancel` 不结束拖拽（只有 pointerup / touchend 才结束），
 * 移动坐标同时从 document 上的 `pointermove` 与非被动 `touchmove` 拿，谁到都算。
 */
/** 现在该滚谁：Markdown 弹层滚 .md-doc，正常阅读滚 .reader-content */
function handleScrollHost(): HTMLElement | null {
  if (mdView.open && mdDocEl.value) return mdDocEl.value;
  return scroller.value ?? null;
}

/**
 * 手柄拖到页面上下边缘时**自动滚动**（用户口径 2026-10-18）。
 *
 * 每帧做三件事：算手指离上下边缘的距离 → 换算成滚动量（越靠边越快）→ 滚完按
 * **同一个手指位置**重新解析插入点。少了最后一步，画面滚了但选区不长，看起来就像
 * 手柄卡在屏幕边上不动。
 */
function handleAutoScrollTick(): void {
  handleScrollRaf = 0;
  if (!draggingHandle || !dragPoint) return;
  const box = scrollBox;
  if (!box) return;
  let dy = 0;
  if (dragPoint.y < box.top + HANDLE_SCROLL_EDGE) {
    const k = Math.min(1, (box.top + HANDLE_SCROLL_EDGE - dragPoint.y) / HANDLE_SCROLL_EDGE);
    dy = -Math.max(2, Math.round(k * HANDLE_SCROLL_MAX));
  } else if (dragPoint.y > box.bottom - HANDLE_SCROLL_EDGE) {
    const k = Math.min(1, (dragPoint.y - (box.bottom - HANDLE_SCROLL_EDGE)) / HANDLE_SCROLL_EDGE);
    dy = Math.max(2, Math.round(k * HANDLE_SCROLL_MAX));
  }
  if (!dy) return;
  /* ⚠️ 这一帧**只写不读**：位置由 box.pos 自己记账，边界由 box.max 夹好。
   * 以前每帧写完 scrollTop 再读回来，等于逼浏览器当场重排一次布局 —— 掉帧的主要来源。 */
  const next = Math.max(0, Math.min(box.max, box.pos + dy));
  const moved = next - box.pos;
  if (!moved) return;                      // 已经滚到头了：别再空转 rAF（手指一动会重新起）
  box.el.scrollTop = next;
  box.pos = next;
  /* 滚动**每帧都做**（这样才顺），但"按新位置重算选区"很贵：caretRangeFromPoint +
   * 整段选区的 getClientRects + onMouseUp 里的锚点换算，每帧都跑会直接掉到 15fps。
   * 所以按「滚够 80px 或过了 90ms」来节流（每帧 18px，实际就是 ~90ms 一次），
   * 视觉上是连续的，手上也不卡。 */
  scrollSinceSelUpdate += Math.abs(moved);
  const now = performance.now();
  if (scrollSinceSelUpdate >= 80 || now - lastSelUpdateAt >= 90) {
    scrollSinceSelUpdate = 0;
    lastSelUpdateAt = now;
    moveHandleTo(dragPoint.x, dragPoint.y, true);
  }
  handleScrollRaf = requestAnimationFrame(handleAutoScrollTick);
}

/** 拖拽期间缓存的滚动盒子：边界与当前位置都记在 JS 里，tick 里不再读布局 */
let scrollBox: { el: HTMLElement; top: number; bottom: number; max: number; pos: number } | null = null;

function primeScrollBox(): void {
  const host = handleScrollHost();
  if (!host) { scrollBox = null; return; }
  const rect = host.getBoundingClientRect();
  // 滚动容器的上下沿再和视口取交集：容器比视口高时，边缘要从能看见的地方算起
  scrollBox = {
    el: host,
    top: Math.max(rect.top, 0),
    bottom: Math.min(rect.bottom, window.innerHeight),
    max: Math.max(0, host.scrollHeight - host.clientHeight),
    pos: host.scrollTop,
  };
}

function startHandleAutoScroll(): void {
  if (!scrollBox) primeScrollBox();
  if (!handleScrollRaf) handleScrollRaf = requestAnimationFrame(handleAutoScrollTick);
}

function stopHandleAutoScroll(): void {
  if (handleScrollRaf) cancelAnimationFrame(handleScrollRaf);
  handleScrollRaf = 0;
  dragPoint = null;
  scrollBox = null;
  scrollSinceSelUpdate = 0;
  lastSelUpdateAt = 0;
}

function beginHandleDrag(which: 'start' | 'end'): void {
  if (draggingHandle) return;
  // 拖选择条时菜单让开（用户口径 2026-10-17）：**只收菜单**，选区与手柄都留着
  if (ctxMenu.open) { ctxMenu.open = false; ctxMenu.rect = null; }
  /* 划词胶囊也一起收：拖的时候还挂着一根工具栏挡在选区边上，既挡视线又挡手指
     （用户口径 2026-10-18"拖动选择时仍然显示工具条"）。松手后会重新摆出来。 */
  selection.show = false;
  draggingHandle = which;
  // 锁定另一头（之后每次移动都用它当锚点）
  const sel0 = window.getSelection();
  if (sel0 && sel0.rangeCount && !sel0.isCollapsed) {
    const r0 = sel0.getRangeAt(0);
    dragAnchor = which === 'start'
      ? { node: r0.endContainer, offset: r0.endOffset }
      : { node: r0.startContainer, offset: r0.startOffset };
  }
  dragColumnBand = columnBandOfNode(dragAnchor?.node) ?? selColumnBand;
  document.addEventListener('pointermove', onHandleMove, true);
  document.addEventListener('pointerup', onHandlePointerUp, true);
  document.addEventListener('touchmove', onHandleTouchMove, { passive: false });
  document.addEventListener('touchend', onHandleUp, true);
  document.addEventListener('touchcancel', onHandleUp, true);
}

function onHandleDown(which: 'start' | 'end', ev: PointerEvent): void {
  ev.preventDefault();
  ev.stopPropagation();
  dragPoint = { x: ev.clientX, y: ev.clientY };
  beginHandleDrag(which);
  primeScrollBox();
}

function onHandleTouchStart(which: 'start' | 'end', ev: TouchEvent): void {
  ev.preventDefault();
  ev.stopPropagation();
  const t0 = ev.touches[0];
  if (t0) dragPoint = { x: t0.clientX, y: t0.clientY };
  beginHandleDrag(which);
  primeScrollBox();
}

function onHandlePointerUp(ev: PointerEvent): void {
  if (ev.type === 'pointercancel') return;   // 手指还在，别把拖拽收掉
  onHandleUp();
}

function onHandleTouchMove(ev: TouchEvent): void {
  if (!draggingHandle) return;
  const t = ev.touches[0];
  if (!t) return;
  ev.preventDefault();       // 拖手柄时不要滑页面（要滚由边缘自动滚动来滚）
  dragPoint = { x: t.clientX, y: t.clientY };
  moveHandleTo(t.clientX, t.clientY);
  startHandleAutoScroll();
}

function onHandleMove(ev: PointerEvent): void {
  if (!draggingHandle) return;
  dragPoint = { x: ev.clientX, y: ev.clientY };
  moveHandleTo(ev.clientX, ev.clientY);
  startHandleAutoScroll();
}

/**
 * @param light 拖拽自动滚动时用：只把选区、手柄、胶囊位置更新一下，
 *              **跳过 onMouseUp 那套锚点换算**（它要把整页文字重新拼一遍，
 *              每帧都跑就是自动滚动掉帧的元凶）；松手时再补一次完整计算。
 */
/** 插入点是否还在锚点那一列里（横向判；量不出来就放行，别把拖拽卡死） */
function caretInDragColumn(caret: { node: Node; offset: number }): boolean {
  if (!dragColumnBand) return true;
  const r = document.createRange();
  try { r.setStart(caret.node, Math.min(caret.offset, (caret.node.textContent ?? '').length)); r.collapse(true); } catch { return true; }
  const b = r.getBoundingClientRect();
  const x = b.left || b.right;
  if (!x) return true;
  return x >= dragColumnBand.left && x <= dragColumnBand.right;
}

function moveHandleTo(x: number, y: number, light = false): void {
  /* 起点手柄在行上沿、终点手柄在行下沿：插入点按**手柄那一侧**偏一点取，见 HANDLE_CARET_BIAS */
  const caret = caretAtLine(x, y, draggingHandle === 'start' ? 'start' : 'end');
  if (!caret) return;
  // 只认文字节点上的插入点（落在元素上说明这附近没文字，等下一次移动）
  if (caret.node.nodeType !== Node.TEXT_NODE) return;
  // 出了锚点那一列就整帧忽略（用户口径 2026-11-19：还没进下一页，手柄不许跳到左边的页面）
  if (!caretInDragColumn(caret)) return;
  const sel = window.getSelection();
  if (!sel || !sel.rangeCount) return;
  // 锚点 = 按下时锁定的另一头（见 dragAnchor 的说明）
  const fixed = dragAnchor ?? (draggingHandle === 'start' ? selEndB : selEndA);
  if (!fixed) return;
  // 焦点落在锚点上（同一个点）= 空选区：这种一步会"把选区丢掉"，直接跳过
  if (fixed.node === caret.node && fixed.offset === caret.offset) return;
  const prev = sel.getRangeAt(0).cloneRange();
  try {
    sel.setBaseAndExtent(fixed.node, fixed.offset, caret.node, caret.offset);
  } catch { return; }
  // 万一还是折叠了：把上一次的选区放回去（拖手柄绝不能把选区弄没）
  if (sel.isCollapsed) {
    try { sel.removeAllRanges(); sel.addRange(prev); } catch { /* 忽略 */ }
    return;
  }
  if (light) {
    // 轻量更新：胶囊位置贴住选区，锚点/高亮等松手时再算
    const box = sel.getRangeAt(0).getBoundingClientRect();
    selection.rect = { left: box.left, top: box.top, right: box.right, bottom: box.bottom };
    Object.assign(selection, menuPos(box));
    refreshSelHandles();
    if (draggingHandle) selection.show = false;   // 拖拽中不摆胶囊
    return;
  }
  // 与鼠标松手同一条链路：重算划词胶囊 / 高亮锚点 / 位置
  onMouseUp();
  refreshSelHandles();
  /* onMouseUp 会把胶囊重新打开 —— 拖手柄期间再收掉（用户口径 2026-10-18） */
  if (draggingHandle) selection.show = false;
}

function onHandleUp(): void {
  stopHandleAutoScroll();
  if (!draggingHandle) return;
  draggingHandle = null;
  dragAnchor = null;
  dragColumnBand = null;
  document.removeEventListener('pointermove', onHandleMove, true);
  document.removeEventListener('pointerup', onHandlePointerUp, true);
  document.removeEventListener('touchmove', onHandleTouchMove);
  document.removeEventListener('touchend', onHandleUp, true);
  document.removeEventListener('touchcancel', onHandleUp, true);
  // 自动滚动的 scroll 事件可能还在路上：先挂个"别收菜单"的免死金牌
  suppressScrollCloseUntil = performance.now() + 350;
  // 拖拽期间为了不卡只做了轻量更新（见 moveHandleTo 的 light 参数）：
  // 松手这一下补一次完整计算，锚点/高亮 id 才是按最终选区算的。
  // ⚠️ 包一层 try：跨页选区的锚点换算万一抛错，也**不能**把下面的"摆菜单"整条链路带掉。
  try { onMouseUp(); } catch (err) { console.warn('松手重算选区失败', err); }
  // 松开手柄后按**新选区**把工具条重新摆出来（拖动过程中是先收起的，见 onHandleDown）
  openCtxForSelection();
}
/**
 * Markdown 弹层里的右键：**整块弹层**都交给我们的菜单。
 *
 * 只绑在 `.md-doc` 上是不够的：点在弹层的留白/页与页之间会落到弹层容器上，
 * 那时浏览器弹的是**原生菜单**，点它的「全选」会选整个页面 —— 用户看到的就是
 * "全选又把背后的 PDF 区域选进去了"（用户口径 2026-10-17）。
 */
function onDocContextMenu(ev: MouseEvent): void {
  if (!mdView.open) return;
  const target = ev.target as HTMLElement | null;
  if (!target?.closest?.('.modal-mask, .modal-panel, .md-doc')) return;
  onContentContextMenu(ev);
}

/** 菜单按当前选区重算一次（长按、拖完手柄都走这里；`above` 表示贴在选区上方开） */
function openCtxForSelection(): void {
  const sel = window.getSelection();
  if (!sel || !sel.rangeCount) return;
  /* 选区还在、但胶囊被某次 hideMenu（滚动/点别处）收掉了：这条链路要靠 selection.show
     判"有没有选区"，不补回来的话菜单会退化成"没有选区动作"甚至不出现（用户口径 2026-10-18）。 */
  if (!selection.show && !sel.isCollapsed && sel.toString().trim()
      && selectionInContent(sel.anchorNode)) selection.show = true;
  const range0 = sel.getRangeAt(0);
  const rect = range0.getBoundingClientRect();
  /* 拖手柄自动滚动跑到很远之后，整个选区的**包围盒**会伸到视口外面（top 是负数），
   * 菜单就被摆到屏幕外 → 松手看不到工具条。这时改成贴"最后一行还看得见的选区"，
   * 也就是手指那一头。 */
  const lineRects = Array.from(range0.getClientRects()).filter((r) => r.height > 0);
  const visible = lineRects.filter((r) => r.bottom > 8 && r.top < window.innerHeight - 8);
  const anchor = visible.length ? visible[visible.length - 1] : rect;
  const x = Math.round((anchor.left + anchor.right) / 2);
  const y = Math.round(Math.min(Math.max(anchor.top, 8), window.innerHeight - 24));
  /* ⚠️ 不能拿 elementFromPoint：选区左上角正好被"起点手柄"的 44px 热区盖住，
     事件派到那个 div 上根本冒泡不到 .reader-content（表现就是"拖完手柄菜单不出现"）。
     改成从选区的文本节点往上找它所在的页块，直接在那个内容元素上派发。 */
  /* 派发用的节点要取**看得见的那一段选区**：跨页选的时候起点可能落在上一屏、甚至已经被
     虚拟滚动卸掉，在那种节点上派发事件没人收 —— 表现就是"跨页选择后菜单不出现"
     （用户口径 2026-10-18）。先用落点反查那一段文字所在的页块，再退回选区首/尾。 */
  const elOf = (node: Node) => (node.nodeType === Node.TEXT_NODE ? node.parentElement : node) as HTMLElement | null;
  const probeY = Math.min(Math.max((anchor.top + anchor.bottom) / 2, 12), window.innerHeight - 12);
  const atPoint = (() => {
    const cr = document.caretRangeFromPoint?.(x, probeY);
    if (cr?.startContainer) return elOf(cr.startContainer);
    const el = document.elementFromPoint(x, probeY);
    return (el as HTMLElement | null) ?? null;
  })();
  /* 只认"确实被这段选区覆盖到"的节点：落点可能落在页缝/下一段（选区之外），
     那样派发过去会让 onContentContextMenu 判成"不在选区上"，菜单里就没有选区动作了。 */
  const usable = (el: HTMLElement | null) => {
    if (!el?.isConnected) return false;
    try { return range0.intersectsNode(el); } catch { return false; }
  };
  const host = [atPoint, elOf(range0.startContainer), elOf(range0.endContainer)]
    .find(usable) ?? null;
  const target = (host?.closest('[data-pdf-page], [data-page], [data-md-page], .md-doc, .row-pair, .row-line')
    ?? host ?? document.querySelector('.reader-content')) as HTMLElement | null;
  touchCtxNext = true;
  target?.dispatchEvent(new MouseEvent('contextmenu', {
    bubbles: true, cancelable: true, clientX: x, clientY: y, button: 2,
  }));
  /* 兜底：跨页选的时候首次派发可能没人收（落点那一段已被卸载 / 页块对不上），
     那就直接在正文容器上再派发一次 —— 只要选区还在，菜单就必须出来
     （用户口径 2026-10-18：跨页选择时右键菜单不显示）。 */
  if (!ctxMenu.open) {
    touchCtxNext = true;
    (document.querySelector('.md-doc') ?? document.querySelector('.reader-content'))
      ?.dispatchEvent(new MouseEvent('contextmenu', {
        bubbles: true, cancelable: true, clientX: x, clientY: y, button: 2,
      }));
  }
}

function fireLongPress(): void {
  const from = longPressFrom;
  longPressTimer = null;
  if (!from) return;
  suppressNativeSelectUntil = Date.now() + 1200;   // 这之后 1.2s 内浏览器不许自己划词
  ignoreClickUntil = Date.now() + 700;             // 抬起手指补的那一下 click 也忽略
  // PDF 文字层：抬起手指那一下会被 endDrag 当成"点别处"收掉选区，先挂一次豁免
  if ((from.target ?? document.elementFromPoint(from.x, from.y))?.closest?.('[data-pdf-page]')) {
    keepPdfSelectionOnce();
  }
  const range = wordRangeAt(from.x, from.y);
  if (range) {
    const sel = window.getSelection();
    sel?.removeAllRanges();
    sel?.addRange(range);
    // 复用『鼠标松手』那套逻辑：它会算出 selection（划词胶囊 / 高亮锚点 / 位置）
    onMouseUp();
    touchSelActive.value = true;
    refreshSelHandles();     // 长按之后可以直接拖两个小圆点扩大范围
    longPressRange = range.cloneRange();
  }
  // 再按右键那套开菜单：落点、选区、图片、页号都由 onContentContextMenu 自己算
  if (longPressRange) openCtxForSelection();
  navigator.vibrate?.(8);
  longPressFrom = null;
}

function onContentSelectStart(ev: Event): void {
  // 触摸长按（suppressNativeSelectUntil）与**触摸期间的任何划词**（touchSelectGuardUntil）都挡掉：
  // 后者是"双击选词"的来源 —— 手机上双击正文会冒出一段选区，用户口径不要这个。
  if (Date.now() < Math.max(suppressNativeSelectUntil, touchSelectGuardUntil)) ev.preventDefault();
}

/**
 * 抬手：长按这一下**不能**让浏览器当成"点一下"（那一手会把我们刚设的选区折叠掉，
 * 用户看到的就是『选中焦点消失』）。preventDefault + 下一帧再把选区放回去，双保险。
 */
function onContentTouchEnd(ev: TouchEvent): void {
  touchSelectGuardUntil = Math.max(touchSelectGuardUntil, Date.now() + 700);
  /* 手势结束：判断这是"滚动"还是"点"。滚动 → 什么都不动（选区留着）；
     点 → 记下来，连着点第二下（时间/位置都接近）才取消选取。 */
  const started = touchStartPos;
  const endTouch = ev.changedTouches[0];
  touchStartPos = null;
  if (started && endTouch) {
    const movedPx = Math.hypot(endTouch.clientX - started.x, endTouch.clientY - started.y);
    // 长按刚刚造出选区的那一下不算"点"（否则紧接着的一次点击会被当成双击，把选区干掉）
    const justLongPressed = Date.now() < suppressNativeSelectUntil;
    if (movedPx <= TAP_MOVE_MAX && !justLongPressed) {
      const hit = lastTapPos
        && Date.now() - lastTapPos.at <= DOUBLE_TAP_MS
        && Math.hypot(endTouch.clientX - lastTapPos.x, endTouch.clientY - lastTapPos.y) <= DOUBLE_TAP_DIST;
      if (hit) {
        lastTapPos = null;
        clearTouchSelection();
      } else {
        lastTapPos = { x: endTouch.clientX, y: endTouch.clientY, at: Date.now() };
        /* 单击（位移小 = 不是滚动）落在**别处** = 取消这次触摸选取（用户口径 2026-11-26）。
           触发点放在抬手这一下，而不是 pointerdown：那一发在滚动开始时也会来，
           在那儿清会把「想滚一下」当成「点了别处」。 */
        const t = endTouch.target as HTMLElement | null;
        const keep = t?.closest?.('.sel-handle, [data-selection-menu], [data-ctx-menu], .reader-aside, .chat-host, [data-float-card], input, textarea, [contenteditable="true"]');
        if (touchSelActive.value && !keep) clearTouchSelection();
      }
    } else {
      lastTapPos = null;          // 滚动/长按：双击计数清零
    }
  }
  if (Date.now() < suppressNativeSelectUntil) {
    ev.preventDefault();
    const saved = longPressRange?.cloneRange() ?? null;
    if (saved) {
      window.setTimeout(() => {
        const sel = window.getSelection();
        if (!sel) return;
        if (!sel.rangeCount || sel.isCollapsed) {
          sel.removeAllRanges();
          try { sel.addRange(saved); } catch { return; }
          onMouseUp();
          refreshSelHandles();
        }
      }, 0);
    }
  }
  cancelLongPress();
}

/** 连续双击 = 取消这次触摸选择（选区、手柄、胶囊、工具条一起收） */
function clearTouchSelection(): void {
  window.getSelection()?.removeAllRanges();
  longPressRange = null;
  selection.show = false;
  selection.text = '';
  selection.rect = null;
  ctxMenu.open = false;
  ctxMenu.rect = null;
  hideSelHandles();
}

function onContentTouchStart(ev: TouchEvent): void {
  const target = ev.target as HTMLElement | null;
  if (target?.closest?.('input, textarea, [contenteditable="true"]')) return;
  // 触摸手势开始：这一下到抬手后 700ms 内的原生划词全部挡掉（双击选词就是它）
  touchSelectGuardUntil = Date.now() + 1500;
  /* ⚠️ 这里**不再**收手柄：以前一碰正文就把上一次的选择收掉，于是"想滚一下页面"等于
     "取消选取"（用户口径 2026-10-18）。现在滚动保留选区，只有连续双击才取消。 */
  if (ev.touches.length !== 1) { touchStartPos = null; cancelLongPress(); return; }
  const touch = ev.touches[0];
  touchStartPos = { x: touch.clientX, y: touch.clientY };
  cancelLongPress();
  longPressFrom = { x: touch.clientX, y: touch.clientY, target };
  longPressTimer = window.setTimeout(fireLongPress, LONG_PRESS_MS);
}

function onContentTouchMove(ev: TouchEvent): void {
  const touch = ev.touches[0];
  if (!touch) return;
  const moved = longPressFrom
    ? Math.hypot(touch.clientX - longPressFrom.x, touch.clientY - longPressFrom.y)
    : Number.POSITIVE_INFINITY;
  if (longPressFrom && longPressTimer && moved > 10) cancelLongPress();
  /* 手指在屏幕上滑动 = 要滚页面 / 要继续看正文：把浮层收掉（用户口径 2026-10-18）。
   * 注意判断放在长按取消之后：长按已经把 longPressFrom 清空，此时任何移动都算"滑动"。 */
  if (moved > 12 && (ctxMenu.open || selection.show)) hideMenu();
}
/** 右键：按『落在哪个界面 + 落点是什么』给清单（三套）。输入框里放行浏览器原生菜单。 */
function onContentContextMenu(ev: MouseEvent) {
  const target = ev.target as HTMLElement | null;
  if (target?.closest?.('input, textarea, [contenteditable="true"]')) return;
  // 系统自带的右键菜单**不出场**（触摸长按也走这里，见 onContentTouchStart）
  ev.preventDefault();
  const fromTouch = touchCtxNext || Date.now() < suppressNativeSelectUntil;   // 长按派发的合成右键
  touchCtxNext = false;
  ctxMenu.above = fromTouch;
  // 触摸屏一律走悬浮工具条（Android 那套：一排图标浮在选区上方、不带遮罩）
  ctxMenu.compact = fromTouch;
  const pdfEl = target?.closest?.('[data-pdf-page]') as HTMLElement | null;
  const rowEl = target?.closest?.('[data-row-id]') as HTMLElement | null;
  const pageEl = target?.closest?.('[data-page]') as HTMLElement | null;
  const mdEl = target?.closest?.('[data-md-page]') as HTMLElement | null;
  const imgEl = target?.closest?.('img') as HTMLImageElement | null;
  const markEl = target?.closest?.('mark.hl[data-hl]') as HTMLElement | null;
  /* Markdown 面板打开时，落点只要在面板里就算 md —— 以前只有正好落在 `[data-md-page]` 上才算，
     落在面板的留白/页与页之间就退回 'rows'，于是「全选」去选了**弹层后面**那一页正文（含 PDF 页图）。 */
  /* Markdown 视图是**整屏弹层**：只要它开着，任何右键都属于它 —— 以前只认 `.md-doc` 内的落点，
     点在弹层的留白/页与页之间就退回 'rows'，于是「全选」又去选了背后那一页（含 PDF 区域）。 */
  const inMdDoc = mdView.open || !!target?.closest?.('.md-doc') || !!target?.closest?.('[data-md-view]');
  const line: 'pdf' | 'rows' | 'md' = pdfEl ? 'pdf' : (mdEl || inMdDoc ? 'md' : 'rows');
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
  /* 触摸屏：**色板和动作合成一条工具条**（用户口径 2026-10-17）。
     桌面仍然只有下拉里的文字项 —— 那边划词胶囊已经把颜色摆出来了，不必重复。 */
  if (withSel) {
    items.push({
      key: 'bing', icon: 'search-fill',
      label: quoted ? `搜索 “${quoted}${ctxCtx.text.length > 14 ? '…' : ''}”` : '搜索',
    });
    items.push({ key: 'copy', label: '复制', icon: 'copy-fill', shortcut: 'Ctrl+C' });
    /* 顺序（用户口径 2026-11-19 第三轮）：「加入对话」排在「高亮」**左侧**，「询问 Cyli」仍在它右侧 ——
       也就是 搜索 → 复制 → 加入对话 → 高亮 → 询问 Cyli：先"把这段带走"，再"就地标一下 / 问一下"。 */
    items.push({ key: 'tochat', label: '加入对话', icon: 'chat' });
    /* 触摸条：色板排在「加入对话」后面（用户口径 2026-10-18：常用功能靠左）。
       不再带铅笔图标 —— 一排色点自己就说明是上色，多一个笔反而占宽。 */
    if (fromTouch) items.push({ key: 'hl-color', label: '高亮', swatches: HIGHLIGHT_COLORS });
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
    /* 「加入对话」已经在上面（高亮左侧）排好了，这里只补「询问 Cyli」，让它落在最右 */
    items.push({ key: 'ask', label: '询问 Cyli', icon: 'sparkle' });
  } else if (ctxCtx.isFigure) {
    /* 插图同理：先「加入对话」（带这张图），再「询问 Cyli」（解释这张图） */
    items.push({ key: 'tochat', label: '加入对话', icon: 'chat' });
    items.push({ key: 'ask', label: '询问 Cyli', icon: 'sparkle' });
  }
  /* 「空白处」= 没划词、没点在图上、没点在已有高亮上，也不是公式/表格/插图这种「实体块」。
     用户口径：**导出只在空白处保留**，同时空白处多一项「召唤 Cyli」（就地开对话浮层）。 */
  const onBlank = !withSel && !ctxCtx.isFigure && !ctxCtx.hlIds.length
    && !target?.closest?.('.katex, table, .fig-card, mjx-container, .cite-inline');
  if (onBlank) {
    items.push({ key: 'summon', label: '召唤 Cyli', icon: 'sparkle', sep: items.length > 0 });
  }
  items.push({ key: 'selectall', label: '全选', icon: 'select-all-fill', sep: items.length > 0 });
  if (ctxCtx.imgUrl) {
    items.push({ key: 'saveimg', label: '另存为', icon: 'save-alt-fill', sep: true });
    items.push({ key: 'copyimg', label: '复制图像', icon: 'copy-fill' });
    items.push({ key: 'zoomimg', label: '放大', icon: 'open-new-fill' });
  }
  if (onBlank) items.push({ key: 'export', label: '导出', icon: 'download', sep: true });
  if (ctxCtx.hlIds.length) {
    items.push({ key: 'delhl', label: '删除', icon: 'delete-fill', danger: true, sep: true });
  }
  ctxMenu.items = items;
  if (fromTouch) {
    /* 触摸长按：菜单摆在**选区上方**（用户口径 2026-10-17"右键菜单要避让拖动选择器"）。
       ⚠️ 位置只能取**最后一行还看得见的选区**，不能取整段选区的包围盒：跨页拖一大段之后
       包围盒的 top 是负数，工具条会被摆到屏幕上方外面 —— DOM 里在、屏幕上看不见，
       用户看到的就是"跨页选择时右键菜单不显示"（用户口径 2026-10-18）。
       选区拿不到就退回落点。 */
    const sel0 = window.getSelection();
    const range = sel0 && sel0.rangeCount ? sel0.getRangeAt(0) : null;
    const lines = range ? Array.from(range.getClientRects()).filter((r) => r.height > 0) : [];
    const visible = lines.filter((r) => r.bottom > 8 && r.top < window.innerHeight - 8);
    const box = visible.length ? visible[visible.length - 1] : range?.getBoundingClientRect() ?? null;
    ctxMenu.x = box ? Math.round((box.left + box.right) / 2) : ev.clientX;
    ctxMenu.y = box ? Math.round(Math.min(Math.max(box.top, 12), window.innerHeight - 64)) : ev.clientY;
  } else {
    ctxMenu.x = ev.clientX;
    ctxMenu.y = ev.clientY;
  }
  ctxMenu.open = true;
  ev.preventDefault();                    // 收掉浏览器原生菜单（要的就是它换成这一张）
}

function ctxBing(text: string) {
  const q = text.trim().slice(0, 300);
  if (!q) return;
  window.open(`https://www.bing.com/search?q=${encodeURIComponent(q)}`, '_blank', 'noopener');
}

/**
 * 在一段容器里造一个"跳过原 PDF"的 range：从第一个到最后一个**不在 PDF 文字层里**的文本节点。
 *
 * 为什么不能直接 selectNodeContents：正文那一页（[data-page]）是"左栏原 PDF + 右栏译文/原文"的
 * 整块容器，整块一选就把 PDF 文字也圈进来了（用户口径 2026-10-17："在右侧正文全选会选中 pdf"）。
 * 优先用文字列（.col-rows / .pg-rows）；没有文字列时靠这个函数在节点层面跳过 PDF。
 */
function rangeSkippingPdf(root: HTMLElement): Range | null {
  const nodes: Text[] = [];
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  for (let n = walker.nextNode(); n; n = walker.nextNode()) {
    const t = n as Text;
    if (!t.textContent || !t.textContent.trim()) continue;
    if (t.parentElement?.closest('[data-pdf-page]')) continue;
    nodes.push(t);
  }
  if (!nodes.length) return null;
  const range = document.createRange();
  range.setStart(nodes[0], 0);
  const last = nodes[nodes.length - 1];
  range.setEnd(last, (last.textContent ?? '').length);
  return range;
}

/** 全选"本页"：PDF 选这一页的文字层、Markdown 选这一页、正文行只选文字列（不含原 PDF）。 */
function ctxSelectPage() {
  const { line, page } = ctxCtx;
  const sel = window.getSelection();
  if (!sel) return;
  let root: HTMLElement | null = null;
  let what = '正文';
  let range: Range | null = null;
  if (line === 'pdf') {
    root = document.querySelector<HTMLElement>(`[data-pdf-page="${page}"] .pdf-text-layer`);
    what = ' PDF 文字';
    if (root) { range = document.createRange(); range.selectNodeContents(root); }
  } else if (line === 'md') {
    /* 先找落点那一页；找不到就整个面板。⚠️ 绝不退回 [data-page]（那是背后的阅读器页块）。 */
    root = document.querySelector<HTMLElement>(`.md-doc [data-md-page="${page}"]`)
      ?? mdDocEl.value
      ?? document.querySelector<HTMLElement>('.md-doc');
    what = ' Markdown';
    if (root) { range = document.createRange(); range.selectNodeContents(root); }
  } else {
    const pageEl = document.querySelector<HTMLElement>(`[data-page="${page}"]`);
    root = pageEl?.querySelector<HTMLElement>('.col-rows, .pg-rows') ?? pageEl;
    if (root) range = rangeSkippingPdf(root) ?? (() => { const r = document.createRange(); r.selectNodeContents(root!); return r; })();
  }
  if (!root || !range) return;
  sel.removeAllRanges();
  sel.addRange(range);
  hideMenu();
  toast(line === 'md' ? '已选中 Markdown 这一页' : `已选中第 ${page} 页${what}`, 'ok', 1600);
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

/** 复制的位图：浏览器只保证认 **image/png**，所以非 png 的先用 canvas 转一道。 */
async function blobToPng(blob: Blob): Promise<Blob> {
  const bmp = await createImageBitmap(blob);
  const canvas = document.createElement('canvas');
  canvas.width = bmp.width;
  canvas.height = bmp.height;
  const ctx = canvas.getContext('2d');
  if (!ctx) throw new Error('canvas 不可用');
  ctx.drawImage(bmp, 0, 0);
  return await new Promise<Blob>((resolve, reject) => {
    canvas.toBlob((b) => (b ? resolve(b) : reject(new Error('转 PNG 失败'))), 'image/png');
  });
}

/** 右键图片「复制图像」：把**位图**写进剪贴板（用户口径：要能直接粘进 Word / 聊天框）。 */
async function ctxCopyImage() {
  if (!ctxCtx.imgUrl) return;
  try {
    const res = await fetch(ctxCtx.imgUrl);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const raw = await res.blob();
    const png = raw.type === 'image/png' ? raw : await blobToPng(raw);
    if (!navigator.clipboard?.write || typeof ClipboardItem === 'undefined') {
      throw new Error('这个浏览器不支持把图片写进剪贴板');
    }
    await navigator.clipboard.write([new ClipboardItem({ 'image/png': png })]);
    hideMenu();
    toast('已复制图像', 'ok', 1400);
  } catch (err) { reportError(err, '复制图像失败'); }
}

/**
 * 右键空白处「召唤 Cyli」：在**点击的位置**开出对话浮层。
 *
 * 用户口径：**整个页面只能存在一个对话浮层** —— 已经浮着就「挪过去」（不另开一个），
 * 没浮着才 `openChatFloat()`。浮窗本来就是单实例（`.reader > .chat-host` 那一块），
 * 所以「只有一个」是结构上成立的，这里只负责不去重复创建。
 * 窄屏没有浮窗（`isNarrow`），退化成切到「对话」页签。
 */
async function summonCyliAt(x: number, y: number) {
  hideMenu();
  if (isNarrow.value) {
    await ensureAsideVisible();
    railTab.value = 'chat';
    return;
  }
  if (!chatFloat.value) await openChatFloat();
  /* 左上角对齐落点；再按视口收一次（别让卡片一半在屏幕外） */
  floatBox.value = { ...floatBox.value, left: Math.round(x), top: Math.round(y) };
  clampFloatBox();
  topLayer.value = 'chat';
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
    railTab.value = 'chat';
    await nextTick();
    /* 同上：没开着对话就先开一个新对话，否则附件列表不在界面上（等于没反应） */
    chatRef.value?.ensureThread?.();
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
  // 触摸工具条里那一格是色板：点**行本身**（不是色点）= 用默认黄
  if (key === 'hl-color') { hideMenu(); void addHighlight('yellow'); return; }
  switch (key) {
    case 'ask':
      /* 「解释」不再开一张独立浮层（用户口径）：一律走对话浮层 —— 文字进上下文胶囊、
         插图进附件，都在那边自动发一轮。 */
      if (c.isFigure) void explainFigureInChat(); else void openExplainInChat();
      break;
    case 'tochat':
      if (c.text) openSelectionInChat();
      if (c.imgUrl) void ctxAttachImage();
      break;
    case 'bing': ctxBing(c.text); break;
    case 'copy': void copySelection(); break;
    case 'highlight': hideMenu(); void addHighlight('yellow'); break;
    case 'note': void onNoteAction(c.hlIds, { x: ctxMenu.x, y: ctxMenu.y }); break;
    case 'delhl': void deleteHighlightIds(c.hlIds); break;
    case 'selectall': ctxSelectPage(); break;
    case 'saveimg': void ctxSaveImage(); break;
    case 'copyimg': void ctxCopyImage(); break;
    case 'zoomimg': ctxZoomImage(); break;
    case 'summon': void summonCyliAt(ctxMenu.x, ctxMenu.y); break;
    case 'export': downloadMarkdown(); break;
    default: break;
  }
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

/** 把选区交给对话面板之后**收干净**：选区、手柄、列标记一起清。
 *
 *  理由（用户口径 2026-11-26 第 2 条）：留着会「看着像还没处理完」；而且触摸点输入框时
 *  **那两枚手柄会抢走焦点** —— 它们压在正文下方固定定位，点下去等于在拖手柄，
 *  输入框根本拿不到焦点、键盘弹不出来。 */
function closeSelectionForPanel() {
  window.getSelection()?.removeAllRanges();
  selection.text = '';
  selection.rect = null;
  hideSelHandles();
  setColSide('');
}

/** 把对话面板**先立起来**（页签 + 浮窗 + 至少一条会话），之后调用方才把选区写进去。
 *
 *  顺序不能反：`ReaderChat` 是惰性挂载的（`chatWarm`），而它监听 `props.selection` 的方式
 *  不是 immediate —— 先写选区再挂载，那一次赋值就落进了空气里，自动发送永远不会发生。 */
async function revealChatForIntent() {
  railTab.value = 'chat';
  await ensureAsideVisible();
  await openChatFloat();          // 宽屏且没浮起来时顺手浮起来（与「加入对话」同一口径）
  await nextTick();               // 等 `chatWarm` 那次挂载落地
  chatRef.value?.ensureThread?.();
  await nextTick();
}

/** 把选区带进侧栏「对话」面板继续聊（历史就落在那个会话里，可回看）。
 *
 *  带进去之后落成输入框里那枚上下文胶囊：发不发由用户点它决定（见 `ReaderChat`）。 */
function openSelectionInChat() {
  const anchored = selection.anchored;
  const scope = selection.scope;
  const rowId = anchored ? selection.rowId : '';
  const page = Number(selection.page) || pageOfRow(selection.rowId);
  chatSelection.value = {
    text: selection.text,
    row_id: scope === 'row' ? (rowId || null) : null,
    page: page || null,
    where: whereOf(scope, page, anchored),
  };
  hideMenu();
  closeSelectionForPanel();
  railTab.value = 'chat';
  // 左栏可能是收起的（例如用户在看 Markdown 视图）——切页签不等于看得见面板
  void ensureAsideVisible().then(async () => {
    await nextTick();
    /* 对话面板还停在**会话列表**那一级时，输入框与上下文胶囊都不在 DOM 里 ——
       直接塞选区等于"点了没反应"。所以先确保有会话开着：没有就用新对话（用户口径 2026-11-19）。 */
    chatRef.value?.ensureThread?.();
  });
}

/** 划词菜单「解释」：**不再开独立的解释浮层**，改把选区带进对话面板并自动发一轮。
 *
 *  发送为什么交给面板：输入框、会话加载、上下文胶囊都是 `ReaderChat` 自己的状态机，
 *  这里硬调 `ask` 等于绕过它；面板按 `props.selection` 上的 `explain` / `ask` 自己发。
 *  锚点（start/end/side/scope）一并带上：回答下面那排色点要用它建高亮、存笔记。 */
async function openExplainInChat() {
  const anchored = selection.anchored;
  const scope = selection.scope;
  const rowId = anchored ? selection.rowId : '';
  const page = Number(selection.page) || pageOfRow(selection.rowId);
  const text = selection.text;
  const snapshot: AgentSelection = {
    text,
    row_id: scope === 'row' ? (rowId || null) : null,
    page: page || null,
    where: whereOf(scope, page, anchored),
    /* 高亮要用**坐标系统里**的那一段文字（原 PDF 的文字层与胶囊上那份截断版不是同一个字符串），
       所以 quote 单独带一份，`text` 只负责显示。 */
    quote: selection.anchorText || text,
    start: selection.start,
    end: selection.end,
    side: selection.side,
    scope,
    explain: true,
    ask: '解释这段内容',
  };
  hideMenu();
  closeSelectionForPanel();
  await revealChatForIntent();
  chatSelection.value = snapshot;
}

/** 右键**插图**的「询问 Cyli」：图进对话面板的附件，再自动发一轮「解释这张图」。
 *
 *  走 `ctxAttachImage` 同一条路（URL → File → `chatRef.uploadFiles`）：上传、大小与格式校验
 *  全在面板那套里，后端一行都不用改（`runtime._user_prompt` 会把附件编成多模态消息）。 */
async function explainFigureInChat() {
  const c = ctxCtx;
  if (!c.imgUrl) return;
  hideMenu();
  try {
    const res = await fetch(c.imgUrl);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const blob = await res.blob();
    const file = new File([blob], c.imgName, { type: blob.type || 'image/png' });
    await revealChatForIntent();
    await (chatRef.value as { uploadFiles?: (f: File[]) => Promise<void> } | null)
      ?.uploadFiles?.([file]);
    /* 图已经在附件列表里了，再挂上"解释这张图"的意图；插图没有文字选区，所以 `text` 为空
       （胶囊不显示，内容全靠附件）—— 自动发送见 `ReaderChat` 里 `props.selection` 的 watch。 */
    chatSelection.value = {
      text: '', explain: true,
      ask: '解释这张图：它展示了什么，以及它在这篇论文里说明了什么。',
    };
  } catch (err) { reportError(err, '把图片交给 Cyli 失败'); }
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
  /* 系统取色器可能把 selection 清空过（见 menuSelSnapshot 的说明）：先按快照补回来再算请求体，
     否则 rowId / start / end / 原文全是空，后端判非法 —— 表现就是「点颜色没反应」。 */
  if (!selection.text && !selection.segments.length && menuSelSnapshot) {
    Object.assign(selection, menuSelSnapshot);
  }
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
  // 长按选完抬起手指会补一个 click：那不是"点了一下"，别让它改状态
  if (Date.now() < ignoreClickUntil) return;
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
  /* 触摸屏点高亮：**不再转派合成右键**（用户口径 2026-11-26，收掉上一轮的触屏专属路径）。
     那条悬浮工具条只有触摸会看到，点一下高亮却弹出一排图标、与桌面点高亮出胶囊对不上，
     等于同一处高亮在两种设备上有两套动作。现在触屏点高亮与桌面同路：出划词胶囊
     （里面有删除 / 笔记），想要那排工具条仍然可以**长按正文**（onContentTouchStart）。 */
  const rect = mark.getBoundingClientRect();
  const ids = highlightIdsInRange(document.createRange(), mark);
  /* 这处高亮**已经有了笔记**：点它弹的是那张笔记浮层，而且**自动锚定**
     （点击是明确动作，鼠标移开也不该收）；悬浮弹出的那张照旧是临时卡、不锚定。
     没有笔记的高亮不弹浮层 —— 还是那只划词胶囊（新建笔记 / 删除都在里面）。
     用户口径 2026-11-28。 */
  const hitNote = ids.length
    ? (notes.value.find((n) => n.highlight_id && ids.includes(n.highlight_id)) ?? null) : null;
  if (hitNote) {
    hideMenu();                       // 胶囊收掉：这一下的主角是那张浮层
    const opened = noteCards.find((c) => c.noteId === hitNote.id);
    if (opened) {
      opened.anchored = true;         // 悬浮来的临时卡就地点钉住
      opened.hover = false;
      focusNoteCard(opened.key);
    } else {
      openNotePopover({
        note: hitNote,
        highlight: highlights.value.find((h) => h.id === hitNote.highlight_id) ?? null,
        mode: 'view', anchored: true, rect,
      });
    }
    return;
  }
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
/* ---------------------- 多张笔记卡（用户口径 2026-11-25） ----------------------
 * 由**单实例**（原来那个 reactive 对象）换成**数组**：可以同时开好几张。
 * 只有**显式点击**才多开（侧栏「打开浮层」/ 划词菜单「打开 · 添加笔记」各开一张）；
 * 悬停高亮仍然只有一张**临时卡**（鼠标移开就关，见 `hover`）。
 *
 * - `key`：身份，同时当 v-for 的 key。**建好就不再改**（`n:<noteId>` / `h:<highlightId>`）——
 *   新建笔记保存成功后「按 id 算出来的 key」会变，但那正是**不能改**的时候：换 key 等于换
 *   vnode，卡片会重挂 —— 用户刚摆好的位置、拉好的尺寸、正在编辑的那份 draft 全丢。
 * - `memoryKey`：`FloatingCard` 的位置记忆键（`note:<noteId>` / `note:hl-<highlightId>`）。
 *   保存成功后把它从 `hl-` 换成新 id，下次打开才找得回这条笔记的记忆位置。
 * - `hover`：悬停**临时卡**。用户点它一下（`onFloatPress`）或它被锚定就转成固定卡 ——
 *   固定卡**永远**不受"移开就关"影响。
 * - `open`：先 false、下一拍再置 true。`FloatingCard` 的「恢复记忆位置 / 居中 / 贴锚点」
 *   全挂在 `open` 的 false→true 上；卡片是 `v-for` 现建的，若一上来就 `open=true`，
 *   那个 watch 不会触发（它是带着这个值挂载的，不是"变了"），三种落位就都不生效。
 */
type NoteCard = {
  key: string;
  memoryKey: string;
  open: boolean;
  /** 锚定：鼠标移开也不关（点一下卡片就会置真，见 `onFloatPress`） */
  anchored: boolean;
  mode: 'view' | 'edit' | 'prompt';
  noteId: string;
  highlightId: string;
  rowId: string;
  /** 这处高亮**现在的颜色**：卡片里的色条靠它显示"选中"；改色走 `PUT /highlights/:id` */
  color: string;
  /** 这条笔记的时间戳（头部那行"不到 1 分钟前"）。只用于展示 */
  created: string;
  /** 高亮原文（这张卡挂着的那一段） */
  quote: string;
  content: string;
  draft: string;
  anchor: { x: number; y: number; w?: number; h?: number } | null;
  /** 这一次打开要**居中**（只有侧栏「打开浮层」那个入口会置真） */
  center: boolean;
  busy: boolean;
  /** 悬停临时卡：鼠标移开就关 */
  hover: boolean;
  /** 编辑态点了返回箭头、而草稿和已保存内容不一样：卡片里那块小确认弹窗开着 */
  confirmBack: boolean;
  /** 点了垃圾桶、等二次确认（同一块卡内弹窗，只是问的是删除） */
  confirmDel: boolean;
  /** 这张卡现在的层级（与表格/公式浮层共用同一条序列，见 `nextLayerZ`） */
  z: number;
};
const noteCards = reactive<NoteCard[]>([]);
/** 最上层那张（模板里 `zIndex: card.key === activeNoteKey ? 62 : 60`） */
const activeNoteKey = ref('');

/** 身份键：同一处高亮在"还没有笔记"时也只有一张（`h:`），有笔记之后是 `n:`。 */
function noteKeyOf(noteId: string, highlightId: string): string {
  return noteId ? `n:${noteId}` : `h:${highlightId}`;
}
/** 位置记忆键（就是 `FloatingCard` 的 persistKey，存储键是 `cylit:float:` + 它）。 */
function noteMemoryKeyOf(noteId: string, highlightId: string): string {
  return `note:${noteId || `hl-${highlightId}`}`;
}

/**
 * 位置记录的**搬家**：同一处内容先后有两个记忆键 ——
 * 还没写成笔记时是 note:hl-<高亮>，保存成笔记之后变成 note:<笔记>。
 * 不搬一次，重新打开时按新键找不到位置记录，卡片就落到锚点 / 级联位（用户口径 2026-11-26：
 * 「框的位置不在原来的位置了」）。只在新键还没有记录、旧键有记录时搬，搬完两边都在，不会丢。
 */
function migrateFloatPos(fromKey: string, toKey: string) {
  if (!fromKey || !toKey || fromKey === toKey) return;
  try {
    const to = FLOAT_POS_PREFIX + toKey;
    const from = FLOAT_POS_PREFIX + fromKey;
    const saved = localStorage.getItem(from);
    if (saved && !localStorage.getItem(to)) localStorage.setItem(to, saved);
  } catch { /* 隐私模式忽略 */ }
}

function rectAnchor(rect: DOMRect | null | undefined, fallback?: { x: number; y: number }) {
  if (rect) return { x: rect.left, y: rect.top, w: rect.width, h: rect.height };
  return { x: fallback?.x ?? 40, y: fallback?.y ?? 120, w: 0, h: 0 };
}

/** `FloatingCard` 存位置用的键前缀（那边是 `cylit:float:${persistKey}`，两边必须一致） */
const FLOAT_POS_PREFIX = 'cylit:float:';
/** 新卡没有记忆位置时，相对**上一张**右下错开的距离（用户口径：28px） */
const NOTE_CASCADE = 28;

function readFloatPos(memoryKey: string): { left: number; top: number } | null {
  if (!memoryKey) return null;
  try {
    const raw = localStorage.getItem(FLOAT_POS_PREFIX + memoryKey);
    if (!raw) return null;
    const v = JSON.parse(raw) as { left?: unknown; top?: unknown };
    if (typeof v.left !== 'number' || typeof v.top !== 'number') return null;
    return { left: v.left, top: v.top };
  } catch { return null; }
}

/** 「上一张」（当前最上层那张）的左上角 —— 新卡的 28px 错开以它为基准。
 *  优先量它真实的 DOM 位置：那张卡可能被拖过、被居中打开过、被 place() 夹过，
 *  只有 rect 是屏幕上看得见的那个位置（localStorage 里那份要等用户拖过才有）。
 *  量不到（还没渲染）才退到记忆位置，再退到按锚点估（place() 是 left = x-8、top = y+h+8）。 */
function prevNoteCardOrigin(): { left: number; top: number } | null {
  const top = noteCards.find((c) => c.key === activeNoteKey.value) ?? noteCards[noteCards.length - 1];
  if (!top) return null;
  const el = noteCardEl(top.key);
  if (el) {
    const r = el.getBoundingClientRect();
    if (r.width > 0) return { left: Math.round(r.left), top: Math.round(r.top) };
  }
  const saved = readFloatPos(top.memoryKey);
  if (saved) return saved;
  const a = top.anchor;
  if (!a) return null;
  return { left: a.x - 8, top: a.y + (a.h ?? 0) + 8 };
}

/** 聚焦某张卡：换成最上层，并让"笔记"这一档盖过解释卡 / 对话浮窗。 */
function focusNoteCard(key: string) {
  if (!key) return;
  activeNoteKey.value = key;
  takeTop('note', key);       // 连层级一起抬：否则刚聚焦的卡可能还在别人下面
}

/**
 * 卡片右上角那枚 ✕：**查看态 = 关掉这张卡；编辑态 = 取消这次编辑（不保存）**。
 *
 * 编辑态下把整张卡关掉等于把改了一半的内容一起丢（而且没有二次确认），用户口径 2026-11-26：
 * 编辑态的 ✕ 只当「取消」，卡片留在原地、回到查看态。新建的空卡（还没有 noteId）没有可回的
 * 查看态，仍然直接关掉。
 */
function onNoteCardClose(card: NoteCard) {
  if (card.mode !== 'view' && card.noteId) {
    card.draft = card.content;      // 丢弃这次改动
    card.mode = 'view';
    card.confirmDel = false;
    card.confirmBack = false;
    return;
  }
  closeNoteCard(card.key);
}

/** 关掉**一张**卡（`✕` / 删除成功 / 悬停移开都走这里）。关掉的若是当前焦点，
 *  焦点落到剩下的最后一张（空了就清空）。 */
function closeNoteCard(key: string) {
  cancelNoteHide();
  closeNoteColorMenu();             // 卡片都关了，卡内那些弹窗 / 贴着按钮的菜单都不该留在屏幕上
  const i = noteCards.findIndex((c) => c.key === key);
  if (i < 0) return;
  noteCards.splice(i, 1);
  if (activeNoteKey.value === key || !noteCards.some((c) => c.key === activeNoteKey.value)) {
    activeNoteKey.value = noteCards[noteCards.length - 1]?.key ?? '';
  }
}

/* 换论文：把所有笔记卡收掉。它们挂着的是**上一篇**的高亮与笔记 id，留着不但没用，
   点保存还容易把内容写回上一篇文章（多卡之后一次能留好几张，比单卡时代更容易踩）。 */
watch(paperId, () => {
  noteCards.splice(0, noteCards.length);
  activeNoteKey.value = '';
  blockOverlays.value = [];
  restoreBlockOverlays();       // 换论文：换成那篇自己的那一摞
});

/* ------------------ 跨标签页 / 刷新记住「哪些笔记卡是开着的」 ------------------
 * 阅读器切走就重新挂载（App 的 RouterView 带 key），卡片全没了 —— 与对话浮层同一个问题
 * （用户口径 2026-11-26）。按**论文**记：卡片挂的是这篇的高亮 / 笔记 id，换一篇就没意义。
 * 只记「固定着的」那些：悬停临时卡本来就是路过看一眼，回来时不该凭空冒出一张。 */
function noteOpenKey(): string {
  return `cylit:notes-open:${paperId.value}`;
}

function persistNoteCards() {
  const keys = noteCards.filter((c) => !c.hover).map((c) => c.key);
  try {
    if (keys.length) localStorage.setItem(noteOpenKey(), JSON.stringify(keys));
    else localStorage.removeItem(noteOpenKey());
  } catch { /* 隐私模式忽略 */ }
}

/* 卡片集合一变（开 / 关）就落一次盘；悬停卡不计入，所以鼠标来回扫过文字不会狂写。 */
watch(() => noteCards.filter((c) => !c.hover).map((c) => c.key).join('|'), persistNoteCards);

/**
 * 重新挂载后把上次开着的卡片摆回去。
 *
 * 必须在 notes / highlights 到位之后调：卡片要靠 id 把内容重建出来，找不到就跳过 ——
 * 笔记被删了、高亮被合并了，都不该留一张空卡。位置交给 FloatingCard 的 persistKey 记忆，
 * 没记过的那几张照老规矩按 28px 依次错开（prevNoteCardOrigin 需要前一张已经渲染，故逐张 nextTick）。
 */
async function restoreNoteCards() {
  let keys: string[] = [];
  try {
    const raw = localStorage.getItem(noteOpenKey());
    if (!raw) return;
    const parsed: unknown = JSON.parse(raw);
    if (Array.isArray(parsed)) keys = parsed.filter((k): k is string => typeof k === 'string');
  } catch { return; }
  for (const key of keys) {
    const id = key.slice(2);
    if (!id) continue;
    if (key.startsWith('n:')) {
      const note = notes.value.find((n) => n.id === id);
      if (!note) continue;
      const hl = highlights.value.find((h) => h.id === note.highlight_id) ?? null;
      openNotePopover({ note, highlight: hl, anchored: true });
    } else if (key.startsWith('h:')) {
      const hl = highlights.value.find((h) => h.id === id);
      if (!hl) continue;
      openNotePopover({ highlight: hl, mode: 'edit', anchored: true });
    }
    await nextTick();
  }
}

/** 打开浮层（查看已有笔记 / 编辑某条 / 新建）—— **同一处高亮 / 同一条笔记永远只有一张卡**：
 *  已经开着就更新那张并聚焦（置为最上层），不再新增。 */
function openNotePopover(opts: {
  note?: Note | null; highlight?: Highlight | null; mode?: 'view' | 'edit' | 'prompt';
  rect?: DOMRect | null; anchored?: boolean; fallbackXY?: { x: number; y: number };
  /** 这次打开摆在页面正中（侧栏「打开浮层」用；其它入口不要传） */
  center?: boolean;
  /** 悬停临时卡（鼠标移开就关）；显式点击进来的都不是 */
  hover?: boolean;
}) {
  const note = opts.note ?? null;
  const hl = opts.highlight ?? null;
  const noteId = note?.id ?? '';
  const highlightId = note?.highlight_id ?? hl?.id ?? selection.hlIds[0] ?? '';
  const key = noteKeyOf(noteId, highlightId);
  const mode = opts.mode ?? (note ? 'view' : 'edit');
  /* ⚠️ `Note` 的时间戳字段名没在类型里确认过：用 `as` 兼容两种常见写法，
     拿不到就留空（头部那行不显示），不猜也不报错。 */
  const rawTime = (note ?? null) as unknown as Record<string, unknown> | null;
  /* 先按 key 找；再按 noteId / highlightId 找 —— 第二条命中的是"**新建笔记刚保存成功**"
     那种情况：那张卡的 key 还是 `h:<hl>`（见上面 type 注释），只按 key 找就会又开一张。 */
  const existing = noteCards.find((c) => c.key === key)
    ?? noteCards.find((c) => (!!noteId && c.noteId === noteId)
      || (!!highlightId && c.highlightId === highlightId))
    ?? null;
  if (existing) {
    existing.mode = mode;
    existing.anchored = opts.anchored ?? false;
    existing.center = !!opts.center;
    existing.hover = opts.hover ?? false;
    if (opts.rect || opts.fallbackXY) existing.anchor = rectAnchor(opts.rect, opts.fallbackXY);
    if (!existing.highlightId) existing.highlightId = highlightId;
    if (hl) existing.color = String(hl.color ?? existing.color);
    /* 这张卡还没有笔记、现在有了：把服务端那份接过来（否则会拿空 draft 去覆盖别人的笔记） */
    if (note && existing.noteId !== note.id) {
      existing.noteId = note.id;
      existing.content = note.content_md ?? '';
      if (mode !== 'edit') existing.draft = note.content_md ?? existing.draft;
      existing.created = String(rawTime?.created_at ?? rawTime?.updated_at ?? '');
      existing.rowId = note.block_id ?? existing.rowId;
      existing.quote = note.selected_text ?? existing.quote;
      /* 键从按高亮换成按笔记：位置记录跟着搬过去，卡片不会因为换键而跳位 */
      migrateFloatPos(existing.memoryKey,
        noteMemoryKeyOf(note.id, existing.highlightId));
      existing.memoryKey = noteMemoryKeyOf(note.id, existing.highlightId);
    }
    focusNoteCard(existing.key);
    return;
  }
  const memoryKey = noteMemoryKeyOf(noteId, highlightId);
  /* 先有高亮、后写笔记的那种：位置当初按高亮那个键存的，搬到现在这个键上 */
  if (noteId && highlightId) migrateFloatPos(`note:hl-${highlightId}`, memoryKey);
  /* 「居中」只给**第一张**（侧栏「打开浮层」的口径：一进来它落在页面正中）。
     已经有卡还居中，两张会一模一样地叠在正中（`centerOnOpen` 按视口算，不看锚点）——
     第二张起改成相对上一张右下错开 28px，用户才看得见自己开了两张。 */
  const center = !!opts.center && noteCards.length === 0;
  let anchor = rectAnchor(opts.rect, opts.fallbackXY);
  /* 位置**按笔记记忆**：恢复记忆位置是 `FloatingCard` 自己按 persistKey 做的；
     只有"第一次打开"（这条笔记还没有记忆位置）才由这里给落点 —— 相对上一张右下错开 28px。
     反推锚点：place() 是 left = x-8、top = y+h+8，所以要落在 prev+(28, 28) 上，
     x 给 prev.left+8+28、y 给 prev.top+28-8。 */
  if (!center && !readFloatPos(memoryKey)) {
    const prev = prevNoteCardOrigin();
    if (prev) anchor = { x: prev.left + 8 + NOTE_CASCADE, y: prev.top + NOTE_CASCADE - 8, w: 0, h: 0 };
  }
  const card: NoteCard = {
    key,
    memoryKey,
    open: false,
    anchored: opts.anchored ?? false,
    mode,
    noteId,
    highlightId,
    rowId: note?.block_id ?? hl?.block_id ?? selection.rowId ?? '',
    color: String(hl?.color ?? 'yellow'),
    created: String(rawTime?.created_at ?? rawTime?.updated_at ?? ''),
    quote: note?.selected_text ?? hl?.text
      ?? (selection.anchored ? (selection.anchorText || selection.text) : ''),
    content: note?.content_md ?? '',
    draft: note?.content_md ?? '',
    anchor,
    center,
    busy: false,
    hover: opts.hover ?? false,
    confirmBack: false,
    confirmDel: false,
    /** 新开的这张拿一个新的层级 —— 于是它一定在已有浮层的上面 */
    z: nextLayerZ(),
  };
  noteCards.push(card);
  /* 必须拿数组里那个响应式代理去置 open：`card` 是普通对象，
     直接改它绕过了 Proxy 的 set，模板收不到更新（卡片会一直不打开）。 */
  const live = noteCards[noteCards.length - 1];
  focusNoteCard(key);
  /* 下一拍再"打开"：`FloatingCard` 的恢复记忆 / 居中 / 贴锚点都挂在 open 的 false→true 上 */
  void nextTick().then(() => { live.open = true; });
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
/* 多卡之后相对时间**按卡片算**（模板里直接 `relTime(card.created)`）：每张卡一条时间，互不干扰。 */

/**
 * 笔记卡的**初始尺寸按内容多少来**（用户口径 2026-10-10："根据内容数量默认调整到合适大小"）。
 *
 * 为什么不用固定 360px：只有一行字的短笔记拉那么宽看着空、长段落挤在 360px 里又一直在折行。
 * 按**最长一行**估宽（中英混排按 ~8.5px/字，再加左右内边距），夹在 340–560 之间：
 * 太窄比正文列还窄、太宽就把正文挡住了。
 *
 * 高度不在这里算 —— 查看态的卡片本来就随内容自然高，编辑态的高度交给 `noteEditRows`。
 */
function noteCardWidth(card: NoteCard): number {
  const text = card.mode === 'view' ? card.content : card.draft;
  const longest = (text || '').split('\n').reduce((m, l) => Math.max(m, [...l].length), 0);
  return Math.round(Math.min(560, Math.max(340, longest * 8.5 + 96)));
}

/** 编辑框的行数跟着**已有内容**走：空笔记给 4 行就够（不用先占一片白），长笔记一次全露出来 */
function noteEditRows(card: NoteCard): number {
  const lines = (card.draft || '').split('\n').length;
  return Math.min(16, Math.max(4, lines + 2));
}

/** 笔记卡里点色点 = **给这处高亮换色**（用户口径 2026-10-10："选色 + 写 + 保存"同一张卡）。
 *  与保存分开：换色只动高亮，不动笔记文本，所以立刻生效。 */
async function setNoteColor(card: NoteCard, color: string) {
  if (!card.highlightId) return;
  try {
    await api.put(`/highlights/${card.highlightId}`, { color });
    card.color = color;
    await refreshAnnotations();
  } catch (err) { reportError(err, '换颜色失败'); }
}

/** 草稿有没有改过 —— 编辑态那枚返回箭头要不要先问一句，就靠它判 */
function noteDirty(card: NoteCard): boolean {
  return card.draft !== card.content;
}

/** 编辑态最左那枚**返回箭头**（复用对话页的 back 图标）= 不保存地退出：
 *  没有改动直接关；有改动先在**卡片内部**弹一块小确认（用户口径 2026-11-28 十三轮）。
 *  ⚠️ 不用全站那个居中 `askConfirm` —— 一整页遮罩比误关一张浮层重得多。 */
function onNoteBack(card: NoteCard) {
  if (noteDirty(card)) { card.confirmBack = true; return; }
  backToView(card);
}

/** 返回 = **回到查看态**（不是关掉浮层）；还没有笔记可看（新建的空卡）时才真的关掉。 */
function backToView(card: NoteCard) {
  card.confirmBack = false;
  if (!card.noteId) { closeNoteCard(card.key); return; }
  card.draft = card.content;
  card.mode = 'view';
}

/** 确认弹窗里的「保存」：存成功它自己会关；失败就留在这张卡上，草稿不动 */
async function confirmBackSave(card: NoteCard) {
  card.confirmBack = false;
  await saveNotePopover(card);
}

/** 「不保存」：丢掉草稿、回到查看态（下次进来看到的是已保存的那一份） */
function confirmBackDiscard(card: NoteCard) {
  card.confirmBack = false;
  backToView(card);
}

/** 头部那枚垃圾桶：删掉**这条笔记**，**连同它挂的那处高亮**（用户口径 2026-11-28 二十五轮：
 *  "点击删除笔记时，删除对应高亮"）。顺序是先删笔记再删高亮：笔记删不掉就整件事停下，
 *  不会出现"高亮没了、笔记还在"的半截状态。
 *  只影响这一张卡：删成功了就把它关掉，别的卡原样留着。 */
async function deleteNotePopover(card: NoteCard) {
  if (!card.noteId || card.busy) return;
  card.busy = true;
  try {
    await api.del(`/notes/${card.noteId}`);
    /* 高亮用整组删（跨行高亮是"同组多条"，`deleteHighlightIds` 按 group_id 去重、只发一次） */
    if (card.highlightId) await deleteHighlightIds([card.highlightId]);
    toast('笔记已删除', 'ok', 1600);
    closeNoteCard(card.key);
    await refreshAnnotations();
  } catch (err) { reportError(err, '删除笔记失败'); } finally { card.busy = false; }
}

/* ---------------------- 删除笔记的二次确认（用户口径 2026-10-13；2026-11-28 改成卡内弹窗） ----------------------
 * 头部那枚垃圾桶**不直接删**：先在**卡片内部**弹一块小确认问一句（删除 / 取消）。
 * 为什么不用全站那个居中 Modal（askConfirm）：删一条笔记弹整页遮罩比误删更烦人；
 * 为什么不再用贴着按钮的 fixed 菜单（上一版）：它得算坐标、得管点别处 / Esc 收起、
 * 还要 Teleport 出浮层（壳是 overflow:hidden），与"返回"那块卡内确认各写一套 ——
 * 现在两块走同一个 `card.confirmXxx` 开关 + 同一套 `.np-confirm` 样式，行为与观感都统一。
 */
function openNoteDelMenu(_ev: MouseEvent, card: NoteCard) {
  closeNoteColorMenu();            // 两块卡内弹窗不叠加
  card.confirmBack = false;
  card.confirmDel = !card.confirmDel;
}

/* ---------------------- 笔记卡的高亮颜色（用户口径 2026-11-28） ----------------------
 * 原来头部把 8 枚色点一字排开（而且只有编辑态给），又挤、查看态还改不了色。
 * 现在合并成**一枚圆形按钮**：里面就是当前高亮色，点开是一块贴着它的二级菜单（7 色 + 自定义）。
 * 查看态 / 编辑态都有；菜单带"从按钮那一角张开"的弹出动画。
 * 状态与删除菜单同一个写法（局部 ref + Teleport + 打开期间才挂全局监听）。 */
const noteColorMenu = ref({ open: false, top: 0, left: 0, key: '', origin: 'top left' });
/// 菜单尺寸：色块 21px + 6px 内边距 + 2px 间距 → 外高 35px，与查看态「⋯」菜单完全等高
/// 8 × 21 + 7 × 2 + 2 × 6 + 2 描边 = 196 宽、21 + 12 + 2 = 35 高
const NOTE_COLOR_MENU_W = 196;
const NOTE_COLOR_MENU_H = 35;
/** 菜单正对着的那张卡（菜单里高亮"当前色"、点色时改的都是它） */
const colorMenuCard = computed(() => (noteColorMenu.value.open
  ? noteCards.find((c) => c.key === noteColorMenu.value.key) ?? null : null));
const isHexColor = (color: string) => color.startsWith('#');
/* 色表在 `utils/highlightColors`：笔记侧栏那条左竖线也用同一份 */
/** 查看态：这处高亮的颜色（编辑态不给 —— 那时色点按钮就在旁边，不必重复） */
function noteAccentColor(card: NoteCard) {
  if (card.mode !== 'view') return '';
  return highlightCss(card.color);
}
/** 图标与标题共用同一个色：`head-lead` 那枚图标 + 「笔记」两个字一起染 */
function noteLeadStyle(card: NoteCard) {
  const css = noteAccentColor(card);
  return css ? { color: css } : undefined;
}
/** 自定义色不是那 7 个类名之一，只能行内给（否则圆点会是空的） */
function colorDotStyle(color: string) {
  return isHexColor(color) ? { background: color } : undefined;
}

function closeNoteColorMenu() {
  if (!noteColorMenu.value.open) return;
  noteColorMenu.value = { open: false, top: 0, left: 0, key: '', origin: 'top left' };
}

/** 弹开的位置：**以那枚圆钮为中线居中**、向下 4px；下方放不下就翻到上面
 *  （`transform-origin` 跟着换，弹出动画始终从按钮那一侧张开）。
 *  再点同一枚 = 收起（所以"点别处就收"把触发钮排在外面）。 */
function openNoteColorMenu(ev: MouseEvent, card: NoteCard) {
  const same = noteColorMenu.value.open && noteColorMenu.value.key === card.key;
  closeNoteColorMenu();
  card.confirmDel = false;          // 两块卡内弹窗不叠加
  card.confirmBack = false;
  if (same) return;
  const r = (ev.currentTarget as HTMLElement | null)?.getBoundingClientRect();
  /* 居中：菜单中线对准按钮中线（夹在视口里，靠边时不会跑出去） */
  const center = r ? r.left + r.width / 2 : window.innerWidth / 2;
  const left = Math.max(8, Math.min(center - NOTE_COLOR_MENU_W / 2,
    window.innerWidth - NOTE_COLOR_MENU_W - 8));
  let top = r ? r.bottom + 4 : 12;
  let origin = 'top center';
  if (top + NOTE_COLOR_MENU_H > window.innerHeight - 8) {
    top = r ? Math.max(8, r.top - NOTE_COLOR_MENU_H - 4) : 8;
    origin = 'bottom center';
  }
  noteColorMenu.value = { open: true, top: Math.round(top), left: Math.round(left),
    key: card.key, origin };
}

function onNoteColorOutside(ev: PointerEvent) {
  if ((ev.target as HTMLElement | null)?.closest?.('[data-note-color-menu], [data-note-color]')) return;
  closeNoteColorMenu();
}
function onNoteColorKeydown(ev: KeyboardEvent) {
  if (ev.key !== 'Escape') return;
  ev.preventDefault();
  ev.stopPropagation();
  closeNoteColorMenu();
}
watch(() => noteColorMenu.value.open, (open) => {
  if (open) {
    document.addEventListener('pointerdown', onNoteColorOutside, true);
    document.addEventListener('keydown', onNoteColorKeydown, true);
  } else {
    document.removeEventListener('pointerdown', onNoteColorOutside, true);
    document.removeEventListener('keydown', onNoteColorKeydown, true);
  }
});
onUnmounted(() => {
  document.removeEventListener('pointerdown', onNoteColorOutside, true);
  document.removeEventListener('keydown', onNoteColorKeydown, true);
});

/** 选一个颜色：立刻写回这处高亮；自定义取色**不关**菜单（可以边调边看） */
async function pickNoteColor(color: string, close = true) {
  const card = colorMenuCard.value;
  if (close) closeNoteColorMenu();
  if (card) await setNoteColor(card, color);
}

/** 卡内弹窗点「删除」才真的删（垃圾桶只是打开确认） */
function onNoteDelConfirm(card: NoteCard) {
  card.confirmDel = false;
  void deleteNotePopover(card);
}

/* 悬停显示已存在的笔记；移开延时关闭（延时是为了让鼠标能从高亮挪到浮层上）
 *
 * ⚠️ 浮层自己也要接 `mouseleave`（模板上那一条）：卡片高的时候会自动翻到高亮**上方**，
 * 于是常常一打开就**盖住了光标** —— 那时指针的 hover 目标已经是卡片，`mouseout` 交回的是卡片、
 * 不是高亮，光靠 `onContentHoverOut` 永远不会收（实测：移开后浮层还在）。
 * 锚定的（`anchored`）与编辑态不受影响，`scheduleNoteHide` 自己会挡。 */
let noteHideTimer: number | undefined;
function cancelNoteHide() { window.clearTimeout(noteHideTimer); }

/** 一张卡自己的 DOM（卡片根节点上带 `data-note-key`，见模板）：用来问"鼠标还在**这张**上吗" */
function noteCardEl(key: string): HTMLElement | null {
  return document.querySelector<HTMLElement>(`[data-note-popover][data-note-key="${CSS.escape(key)}"]`);
}

/** 只关**悬停临时卡**（`hover && !anchored`）—— 固定卡一张都不许动。
 *  传 `card` = "从这一张上移开了"；不传（从高亮移开）= 把所有临时卡都过一遍。 */
function scheduleNoteHide(card?: NoteCard) {
  const pending = noteCards.filter((c) => c.hover && !c.anchored && c.mode === 'view'
    && (!card || c.key === card.key));
  if (!pending.length) return;
  window.clearTimeout(noteHideTimer);
  noteHideTimer = window.setTimeout(() => {
    /* 鼠标还在**这张卡**上（从高亮挪进来）就别关：直接问 DOM 的 `:hover`，
     * 比"进/出浮层各挂一个事件"稳 —— 后者在浮层刚好渲染在光标底下时会误判。
     * ⚠️ 多卡之后必须**逐张**问：原来那句全局 `[data-float-card]:hover` 在
     * "离开了 A、光标却压在 B 上"时会把 A 也留下（多开之后 A 就成了关不掉的幽灵卡）。 */
    for (const c of [...noteCards]) {
      if (!c.hover || c.anchored || c.mode !== 'view') continue;
      if (noteCardEl(c.key)?.matches(':hover')) continue;
      closeNoteCard(c.key);
    }
  }, 240);
}

/**
 * 笔记浮层：**任何一次按下都等于"钉住我"**（用户口径 2026-11-23）。
 *
 * 这件事原来靠头部那枚手动锚定开关（`FloatingCard` 的 `hideAnchor` 一关就有），
 * 用户嫌多一步、还常常手一抖卡片就没了；现在改成**自动**：只要在卡片上按下
 * （点按钮 / 拖标题 / 拖下角放大）就 `anchored = true`，`scheduleNoteHide` 自己会闭嘴
 * （它本来就会挡 `anchored`），鼠标移开也不关。
 *
 * ⚠️ 必须挂在根节点的 `pointerdown.capture` 上（见模板那一行）：拖拽手柄与标题栏都会
 * `stopPropagation`，冒泡阶段收不到；捕获阶段跑在它们之前，所以"拖标题、拖角"照样能锚住。
 * ⚠️ 翻页 / 滚动发生在卡片**外面**，不会触发 —— 正是用户的口径（"不含翻页"）。
 */
/* 点浮层顺手清掉正文里的选区（浮层是另一层，点了它就不该还留着一段"看起来像待操作"的选中文字）。
   ⚠️ 这里是**一次**，不是逐帧：以前在卡片的 pointerdown 上挂了一条 rAF 循环逐帧清，
   结果卡片自己的文字也被一帧一帧清掉 —— 用户选不中、复制不了（2026-11-24 第 2 条）。
   拖动/缩放期间的逐帧清现在归 `FloatingCard` 的手势循环管（只有真的在拖才清）。 */
/**
 * 三块浮层（笔记卡 / 解释卡 / 对话浮窗）上的按下：抬到最上层，顺手清掉**正文**那段选区
 * （浮层是另一层，点了它就不该还留着一段"看起来像待操作"的选中文字）。
 *
 * ⚠️ **右键不清**（用户口径 2026-11-24）：用户刚在浮层里框选了一段文字、正要右键复制 ——
 * 按下那一刻就把选区清掉，原生菜单里的「复制」当场变灰，等于复制不了。
 * 左键照旧清：那时要么是在正文里选完了想点浮层，要么是从这里重新开始选。
 *
 * 多卡之后「按下 = 钉住我」还要按**那张卡**生效（第三参数 `key`），
 * 所以原来那个单独的卡片级处理器（`onNoteCardPointerDown`）已经不需要了。
 */
function onFloatPress(ev: PointerEvent, which: 'note' | 'chat', key?: string) {
  /* 笔记卡：按下 = 「**这张**是焦点」+「钉住我」（用户口径 2026-11-23：点一下就锚定）。
     悬停临时卡被点一下当场转成固定卡（`hover = false`），鼠标移开不再自动消失。 */
  if (which === 'note' && key) {
    activeNoteKey.value = key;
    const card = noteCards.find((c) => c.key === key);
    if (card) { card.anchored = true; card.hover = false; }
  }
  takeTop(which, key);
  /* 只在**鼠标**按下时清文档选区：这条是为「拖浮层时正文 / PDF 文字层跟着长出一条选区」设的。
     触摸按进输入框也走这里（button 同样是 0），清一次文档选区会让移动端把输入框的焦点一起
     丢掉 —— 键盘弹不出来（用户口径 2026-11-26）。 */
  if (ev.button === 0 && ev.pointerType === 'mouse') clearDocSelection();
}

/**
 * 笔记浮层头部的「定位到原文」：跳回它挂的那一行并闪一下。
 * `rowId` 为空（这条笔记没挂行 / 是从页面视图进来的）时什么都不做 —— 别把卡片关上，
 * 用户只是想知道"这是在说哪一段"。
 */
function locateNoteRow(card: NoteCard) {
  const id = card.rowId;
  if (!id) return;
  void focusRow(id);
  flashRow(id);
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
  /* 这条笔记**已经开着一张卡**（固定的或悬停的）：别再开第二张，也别把已有那张收掉 */
  if (noteCards.some((c) => c.noteId === note.id)) { cancelNoteOpen(); cancelNoteHide(); return; }
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
async function saveNotePopover(card: NoteCard) {
  if (card.busy) return;
  /* 用户口径：**只有插入了高亮，才能在高亮的基础上加笔记** ——
     用户 2026-10-10 再次确认："没有高亮不用写"，所以**不**顺手补一条高亮。 */
  if (!card.noteId && !card.highlightId) {
    toast('先给这段文字加个高亮，再写笔记', 'warn', 2600);
    return;
  }
  card.busy = true;
  try {
    if (card.noteId) {
      const updated = await api.put<Note>(`/notes/${card.noteId}`, { content_md: card.draft });
      card.content = updated?.content_md ?? card.draft;
      toast('笔记已保存', 'ok', 1600);
    } else {
      const created = await api.post<Note>('/notes', {
        paper_id: paperId.value,
        block_id: card.rowId,
        highlight_id: card.highlightId,
        selected_text: card.quote,
        start_offset: selection.start,
        end_offset: selection.end,
        content_md: card.draft,
      });
      card.noteId = created?.id ?? '';
      card.content = card.draft;
      card.created = new Date().toISOString();   // 头部那行"不到 1 分钟前"要立刻有值
      /* ⚠️ `key` **保持不变**（换 key = 换 vnode = 卡片重挂：用户刚摆好的位置、
         拉好的尺寸、正在编辑的 draft 全丢）；只把**位置记忆键**换成新 id，
         下次打开这条笔记才找得回它的记忆位置。 */
      if (created?.id) card.memoryKey = noteMemoryKeyOf(created.id, card.highlightId);
      toast('笔记已保存', 'ok', 1600);
    }
    card.mode = 'view';
    await refreshAnnotations();
  } catch (err) { reportError(err, '保存笔记失败'); } finally { card.busy = false; }
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

/**
 * 「哪几条公式被标过」：高亮存的就是公式的原始 LaTeX（见 `services/agent/annotations.create`），
 * 所以直接按 LaTeX 比对即可，不需要把偏移量翻译回公式序号。规范化口径与后端一致
 * （去 `$`、去空白），两边各写一处是为了不跨层引常量。
 *
 * 用户口径（2026-12-06）：公式页签要能看见"我标过哪几条"，并且随 agent 的动作**实时**变 ——
 * 它由 `highlights` 派生，`refreshAnnotations()` 一跑就跟着更新，不需要额外接口。
 */
const annotatedFormulas = computed(() => {
  /* 值里带**那条高亮本身**与它的笔记：公式页签的圆点要显示高亮自己的颜色，点开要能打开笔记
     （用户口径 2026-12-06）。同一个公式被标过多次时取**最后一条**（最新）。 */
  const out = new Map<string, { color: string; highlight: Highlight; note: Note | null }>();
  const noteOf = new Map<string, Note>();
  for (const n of notes.value) {
    if (n.highlight_id) noteOf.set(String(n.highlight_id), n);
  }
  for (const h of highlights.value) {
    const text = String(h.text || '');
    if (!text.includes('$')) continue;
    const key = text.replace(/\$/g, '').replace(/\s+/g, '');
    out.set(key, {
      color: String(h.color || ''),
      highlight: h,
      note: noteOf.get(String(h.id)) ?? null,
    });
  }
  return out;
});

/** agent 在这一轮里写了笔记 / 高亮 / 批注：**就地**重拉这三份列表。
 *  用户口径 2026-11-26：agent 加的笔记必须实时出现，不能靠刷新页面。
 *
 *  再补一层（用户口径 2026-12-06「侧栏要实时更新数量」）：公式 / 表格是从**页级产物的
 *  Markdown** 里抽出来的，agent 一旦修订产物，它们的内容与条数就变了 —— 那两份列表以前
 *  要等重新打开页签才刷新，看上去就是"数量不动"。工具名一起带过来，按名字决定要不要重取。 */
async function onChatSideChanged(tool?: string) {
  try { await refreshAnnotations(); } catch (err) { console.warn('侧栏数据刷新失败', err); }
  if (/page_edit|page_markdown_edit|page_product|meta_write/i.test(String(tool || ''))) {
    void loadFormulas();
    void loadTables();
  }
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
    /* `hover: true`：**悬停临时卡**，鼠标移开就关（不参与多开，见 `scheduleNoteHide`） */
    openNotePopover({ note, highlight: hl, mode: 'view', rect: mark.getBoundingClientRect(), hover: true });
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

/* ---- 侧栏笔记卡：浮层打开 + 多选批量删除（用户口径 2026-11-20） ---- */

/** 「浮层打开」/「编辑」：弹的是**同一张**笔记浮层（编辑态），贴在正文里那处高亮上。
 *  找不到那处高亮（滚出视口 / 属于别的 scope）就退到主区偏右上 —— 不猜 DOM、也不自动滚页。
 *  锚定：这是从侧栏点开的，鼠标本来就离正文很远，不锚定会被"移开就关"立刻收掉。 */
function openPaneNote(p: { note: Note | null; highlight: Highlight | null; mode?: 'view' | 'edit' }) {
  closeNoteColorMenu();
  /* 侧栏那枚「打开浮层」：用户口径 2026-11-21 —— **默认在页面中间打开**，
     既不贴高亮、也不沿用上次拖到的角落（FloatingCard 的 `centerOnOpen` 负责落位）。
     多卡之后"居中"是**这一张卡自己的**标记，跟着 opts 一起下去，不再提前改实例状态。 */
  const hlId = p.highlight?.id || p.note?.highlight_id || '';
  const mark = hlId ? document.querySelector<HTMLElement>(`mark.hl[data-hl="${hlId}"]`) : null;
  openNotePopover({
    note: p.note, highlight: p.highlight, mode: p.mode ?? 'edit', anchored: true,
    rect: mark ? mark.getBoundingClientRect() : null,
    fallbackXY: { x: Math.round(window.innerWidth * 0.42), y: 160 },
    center: true,
  });
}

/** 多选批量删除：**只确认一次**，再按类型分别删 —— 批注走 /annotations/:id、
 *  整组高亮走 deleteHighlightIds（它按 group 去重、顺带同步本地列表）、其余走 /notes/:id。
 *  单条失败不中断其余的，最后一次性刷新并报差几条。 */
async function deleteNoteRows(rows: Array<{
  kind: string; noteId: string; anchorIds: string[]; annotationId: string;
}>) {
  if (!rows.length) return;
  if (!(await askConfirm({
    title: `删除选中的 ${rows.length} 条笔记？`,
    body: '高亮与挂在它上面的笔记会一起删掉，不可恢复。',
    danger: true, confirmLabel: '删除',
  }))) return;
  const annIds: string[] = [];
  const noteIds: string[] = [];
  const hlIds: string[] = [];
  for (const r of rows) {
    if (r.kind === 'annotation') { if (r.annotationId) annIds.push(r.annotationId); }
    else if (r.anchorIds.length) hlIds.push(...r.anchorIds);
    else if (r.noteId) noteIds.push(r.noteId);
  }
  const results = await Promise.all([
    ...annIds.map((id) => api.del(`/annotations/${id}`).then(() => true, () => false)),
    ...noteIds.map((id) => api.del(`/notes/${id}`).then(() => true, () => false)),
  ]);
  if (hlIds.length) await deleteHighlightIds(hlIds);
  const failed = results.filter((ok) => !ok).length;
  await refreshAnnotations();
  toast(failed ? `已删除，${failed} 条失败` : '已删除', failed ? 'warn' : 'ok', 1800);
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
  Escape: () => { hideMenu(); mobilePane.value = ''; if (chatFloat.value) closeChatFloat(); },
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
      ref="readerEl"
      :class="['reader',
               asideVisible ? '' : 'no-aside',
               chatFloat ? 'aside-float' : '',

               asidePreviewW !== null ? 'aside-previewing' : '',
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
          @mousedown="onCapsuleMouseDown"
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
                :style="item.key === 'pen' ? { '--pen-color': colorFor('pen') }
                  : item.key === 'marker' ? { '--marker-color': colorFor('highlighter') } : undefined"
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
          <!-- 颜色：**自选色摆最前**（用户口径），后面跟色板（画笔 = 红橙黄绿青蓝紫，荧光笔 = 荧光色）。
               自选色那枚彩虹点在「当前色不在色板里」时也带选中环，否则挑完取色器看不出选中了谁。 -->
          <div class="ink-options-row" data-ink-colors>
            <label
              class="ink-color-custom"
              :class="{ active: isCustomInkColor() }"
              title="自定义颜色（浏览器取色器）"
            >
              <span class="ink-color-custom-dot" />
              <input
                type="color"
                :value="activeInkColor()"
                data-ink-color-input
                @input="setInkColor(($event.target as HTMLInputElement).value)"
              >
            </label>
            <button
              v-for="c in inkPalette()"
              :key="c"
              class="ink-palette-dot"
              :class="{ active: activeInkColor() === c }"
              :style="{ background: c }"
              :title="c"
              @click="setInkColor(c)"
            />
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
              @click="setInkWidth(step.key)"
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
              data-ink-clear-range
              @update:model-value="onClearSlide(String($event))"
            />
          </div>
        </div>
        <!-- 右：更多（下载打包）+ 全屏。2026-11-19 用户口径：全屏在顶部、用对角线图标；
             两枚按钮都撑满胶囊高度（与左侧侧栏开关同尺寸），竖向不再比左边矮一截。 -->
        <div class="cap-solo right">
          <Menu :items="headMenu" label="更多操作" size="md" @select="onHeadAction" />
          <button class="cap-act" :title="isFullscreen || immersive ? '退出全屏' : '全屏'"
                  :aria-pressed="isFullscreen || immersive" @click="toggleFullscreen()">
            <AppIcon :name="isFullscreen || immersive ? 'collapse-diag' : 'expand-diag'" :size="17" />
          </button>
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

      <!-- 浮层模式下**原侧栏位置**的「画中画」占位面板（用户口径 2026-11-22；本轮把条件收窄）。
           只有「页签 = 对话」且「浮窗开着」时才出现：那一刻侧栏内容区正好空着，
           这块地方就是"对话飞走了、关掉浮层就回来"。页签一旦切走，侧栏照常渲染那个页签
           （浮窗里的对话一动不动），这块面板立刻消失 —— 所以它**不能**再盖住页签条：
           `top: 58px` 让开那排常驻的真实页签（本轮页签条不再随浮窗消失，也不需要只读副本了）。
           整块 pointer-events: none，只放开那枚「回到侧栏」按钮（调现有的 dockChatFromFloat）。
           z-index 50：高于侧栏本体（30）与拖宽把手（31），低于浮窗卡片（60 / 有焦点 62）
           与两枚放大把手（63）；它从 58px 起画，够不到工具栏（40）那一行。
           侧栏收起（no-aside）时整块不渲染，正文照旧整宽。 -->
      <div v-if="chatFloat && asideVisible && railTab === 'chat'" ref="asidePipEl" class="aside-pip" data-aside-pip>
        <span class="aside-pip-icon"><AppIcon name="pip" :size="34" /></span>
        <p class="aside-pip-title">对话正在浮层模式</p>
        <p class="aside-pip-note">关闭浮层后回到这里</p>
        <button type="button" class="aside-pip-btn" data-aside-pip-back @click="dockChatFromFloat">
          回到侧栏
        </button>
      </div>

      <!-- ============ 左栏：目录 + 批注/笔记/公式/插图/术语/任务/论文解读（可整体折叠，可拖宽） ============
           `--reader-font-size` 也设在这一栏上：左栏里的「论文解读」用的是阅读器的正文字号
           （`InsightDoc` 的排版全部以 em 相对它缩放），读者把正文调大，解读也跟着大。 -->

      <aside
        :class="['reader-aside', { resizing: asideResizing }]"
        :style="[{ '--reader-font-size': `${readerFont}px` }, asidePreviewStyle]"
      >
        <!-- 侧栏头部：**只有页签胶囊**。那枚圆形开关是顶栏里固定的一枚（12px 处），
             侧栏开着时它正好落在这一行的最左 —— 所以这里是"胶囊正左侧就是关闭按钮"。
             `padding-left: 62px` 给它让位（12 + 38 + 12）。 -->
        <!-- 页签条**常驻**（本轮三修）：浮窗不再从这里带走任何东西 —— 浮起来的只是
             「对话」那一块的**容器**（`.reader > .chat-host`），页签条留在侧栏原位、照旧可点。
             浮窗卡片的抓手换成那个容器顶部那枚横线指示器（见样式里的 `.chat-host.float::before`）。 -->
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
            <div ref="railTabsEl" class="side-tabs" @scroll="syncTabsScroll" @wheel="onSideTabsWheel">
              <button
                v-for="t in SIDE_TABS"
                :key="t.k"
                class="side-tab"
                :class="{ active: railTab === t.k }"
                :title="t.title"
                @click="railTab = t.k"
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
        <div v-if="railTab === 'toc'" class="toc-pane">
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
        <!-- 「对话」那一块**已经不在这里**（本轮三修）：它挪到了 `.reader > .chat-host` ——
             一个常驻实例 + 两种定位（停靠 / 浮窗），DOM 从不搬家。所以 `railTab === 'chat'` 时
             这里**故意留空**，由那个容器从上面盖住这块内容区；切到别的页签，本栏照常渲染
             目录 / 搜索 / 其它面板，浮窗里的对话一动不动。
             ⚠️ 别再在这里放 `ReaderChat`、也别给它包 `KeepAlive`：多一份实例 = 多一条会话列表请求，
             还多一处"哪个实例在流式"的分叉。 -->
        <ReaderSearch
          v-if="railTab === 'search'"
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
          v-if="railTab !== 'toc' && railTab !== 'chat' && railTab !== 'search'"
          :tab="railTab"
          :annotations="annotations"
          :notes="notes"
          :highlights="highlights"
          :references="paperReferences"
          :formulas="formulas"
          :formulas-loading="formulasLoading"
          :marked-formulas="annotatedFormulas"
          :tables="tables"
          :tables-loading="tablesLoading"
          :figures="figures"
          :glossary="glossary"
          :brief-draft="briefDraft"
          :tasks="tasks"
          :paper-id="paperId"
          :insight-running="understandingRunning"
          @focus="(id: string, page?: number, latex?: string) => focusRow(
            id, { page, flash: true, latex: latex || formulaLatexOf(id) })"
          @cite-peek="onCitePeekAsk"
          @cite-peek-hide="onCitePeekHide"
          @copy="copyFormula"
          @zoom="zoomImage = $event"
          @overlay="openBlockOverlay($event)"
          @save-brief="saveBrief"
          @regenerate-brief="regenerateBrief"
          @reload-brief="syncBriefDraft()"
          @delete-annotation="deleteAnnotation"
          @delete-note="deleteNote"
          @delete-rows="deleteNoteRows"
          @open-note="openPaneNote"
          @save-note="savePaneNote"
          @delete-highlight="deleteHighlightIds"
          @cancel-task="cancelTranslate"
          @insight-queued="fetchTasks"
          @open-insight-page="openInsightPage"
        />
      </aside>

      <!-- ============ 对话容器：**一个常驻实例 + 两种定位** ============
           ⚠️ 这里是 `ReaderChat` 在整棵树里**唯一**的一份：不 `v-if`、不 `Teleport`、不 `KeepAlive`。
           上一轮曾用 `<Teleport>` 在两个宿主之间搬同一个实例，线上出现偶发的
           `Cannot read properties of null (reading 'emitsOptions')`（tsc / build 都拦不住），已整体回滚。
           现在 DOM 从不搬家，切换的只有两样东西：
             · 类 `.float`：定位（停靠 / 浮窗）与浮窗那圈卡片外框；
             · 行内 `asideFloatStyle`：**浮窗态才写** left/top/width/height/z-index。
           停靠态 = 绝对定位盖在**侧栏内容区**上（页签条那条 58px 让出来），看起来就是侧栏里的对话；
           浮窗态 = fixed 卡片，几何仍由 `floatBox` 算。
           ⚠️ 用 `v-show`（不是 `v-if`）：既不是对话页签、也没浮起来时只 `display: none` —— 实例要活着，
           否则每切一次页签就是一次卸载 + 重挂，会话列表要重拉。 -->
      <div
        id="chat-host"
        ref="chatHostEl"
        class="chat-host"
        :class="{ float: chatFloat, closing: floatClosing }"
        v-show="chatFloat || railTab === 'chat'"
        :data-warm="chatWarm"
        @pointerdown.capture="onFloatPress($event, 'chat')"
        @focusin.capture="takeTop('chat')"
        @pointerdown="onFloatCardDown"
        @touchstart="onFloatCardTouchStart"
        @mousedown="onFloatCardMouseDown"
        @pointermove="onFloatPointerMove"
        @pointerup="onFloatPointerUp"
        @pointercancel="onFloatPointerUp"
      >
        <!-- 浮窗顶部那枚抓手：**照 FloatingCard 那条 `.fc-grab` 的做法** ——
             一整条 14px 高的**真元素**（`pointer-events: auto`，光标 grab），药丸只是它里面的装饰。
             以前药丸是 `pointer-events: none`，顶部那一条命中的是下面的层，光标因此不是抓取手。 -->
        <!-- 抓手条的 `cursor` 直接写行内：这件事已经绕过好几轮 CSS 优先级了，
             行内样式不会被任何选择器抢走（药丸自己 `pointer-events: none`，命中的是这条）。 -->
        <div ref="chatGrabEl" class="chat-grab" data-float-grab style="cursor: grab">
          <span class="chat-grip-pill" />
        </div>
        <!-- 惰性挂载：没进过对话页签、也没浮起过就整块不挂（省一次会话列表请求）；
             挂上之后 v-show 收起，永不卸载。 -->
        <ReaderChat
          v-if="chatWarm"
          ref="chatRef"
          :paper-id="paperId"
          :selection="chatSelection"
          :can-float="!isNarrow"
          :floating="chatFloat"
          @focus="(id: string, page?: number, latex?: string) => focusRow(id, { page, flash: true, latex })"
          @peek="onCitePeekAsk"
          @peek-hide="onCitePeekHide"
          @clear-selection="chatSelection = null"
          @refresh-side="onChatSideChanged"
          @float="openChatFloat"
          @zoom="(t) => { zoomImage = { url: t.url, caption: t.caption ?? '' }; }"
        >
          <!-- 浮窗态：这两枚塞进对话自己的头部行（与「＋」同一行），不单占一条标题栏 -->
          <template #head-actions>
            <template v-if="chatFloat">
              <button class="float-act" title="回到侧栏" @click="dockChatFromFloat">
                <AppIcon name="sidebar" :size="14" />
              </button>
              <button class="float-act" title="关闭浮窗" @click="closeChatFloat">
                <AppIcon name="close" :size="13" />
              </button>
            </template>
          </template>
        </ReaderChat>
      </div>

      <!-- 浮窗的两枚放大把手（用户口径 2026-11-21："搬"）——**必须挂在 aside 外面**：
           侧栏本体是 overflow: hidden（滚动内容要按圆角裁），把手留在里面就只能画在内侧。
           现在它们是 .reader 的直接子节点 + position: fixed，坐标由 `floatBox` 算（下面的 :style），
           于是弧能落在卡片**外侧** 3px，而圆角照旧被裁得干净。
           左下角是"右边不动、往左长"，右下角是"左边不动、往右长"（见 onFloatPointerMove）。 -->
      <div
        v-if="chatFloat"
        class="float-grip bl"
        :class="{ unfocused: topLayer !== 'chat' }"
        ref="gripBlEl"
        title="拖动改浮窗大小"
        @pointerdown="(ev) => onFloatPointerDown(ev, 'size-left')"
        @touchstart.prevent="onFloatTouchStart($event, 'size-left')"
        @pointermove="onFloatPointerMove"
        @pointerup="onFloatPointerUp"
        @pointercancel="onFloatPointerUp"
      >
        <!-- 与笔记卡的角指示器同一条 SVG 弧（45°、两端圆头），见 FloatingCard 的 .fc-grip -->
        <svg class="fg-arc" viewBox="0 0 100 100" aria-hidden="true" focusable="false">
          <path d="M30.6 91.7 A46 46 0 0 1 8.3 69.4" vector-effect="non-scaling-stroke" />
        </svg>
      </div>
      <div
        v-if="chatFloat"
        class="float-grip br"
        :class="{ unfocused: topLayer !== 'chat' }"
        ref="gripBrEl"
        title="拖动改浮窗大小"
        @pointerdown="(ev) => onFloatPointerDown(ev, 'size')"
        @touchstart.prevent="onFloatTouchStart($event, 'size')"
        @pointermove="onFloatPointerMove"
        @pointerup="onFloatPointerUp"
        @pointercancel="onFloatPointerUp"
      >
        <svg class="fg-arc" viewBox="0 0 100 100" aria-hidden="true" focusable="false">
          <path d="M91.7 69.4 A46 46 0 0 1 69.4 91.7" vector-effect="non-scaling-stroke" />
        </svg>
      </div>

      <!-- 拖拽改宽度：**在侧栏外面**（用户口径 2026-11-19：右移，别压在滚动条上）。
           不能再放在 `<aside>` 里 —— 那是 `overflow: hidden`，凸出去就被裁；放在内侧又必然
           盖住面板最右边那条滚动条。所以挂在 `.reader` 上、按 `left: 侧栏宽度` 定位：
           左边 1px 压在分隔线上，其余落在正文左侧的空白里。
           收尾的三层 pointerup 兜底见 `onResizeDown`。 -->
      <div
        class="aside-resizer"
        :class="{ active: asideResizing, settling: resizerSettling }"
        :style="{ left: asideShownPx }"
        title="拖动调整侧栏宽度（双击复位）"
        @pointerdown="onResizeDown"
        @pointermove="onResizeMove"
        @pointerup="onResizeUp"
        @pointercancel="onResizeUp"
        @lostpointercapture="onResizeUp"
        @dblclick="resetAsideWidth"
      />

      <!-- ============ 中：正文（单一滚动容器 → 天然对齐） ============
           `.reader-slide` 是"让位 + 平移"的那一层：侧栏开合时它整体滑过去，
           内部的正文一次都不重排（见 asideGapPx 的说明）。 -->
      <section
        ref="scroller"
        class="reader-content"
        @scroll="onScroll"
        @click="onContentClick"
        @contextmenu="onContentContextMenu"
        @selectstart="onContentSelectStart"
        @touchstart.passive="onContentTouchStart"
        @touchmove.passive="onContentTouchMove"
        @touchend="onContentTouchEnd"
        @touchcancel="onContentTouchEnd"
        @mouseover="onContentHover"
        @mouseout="onContentHoverOut"
      >
        <div class="reader-slide" :style="asidePreviewShift">
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
      :visible="selection.show && !(ctxMenu.open && ctxMenu.compact)"
      :x="pillPos.x"
      :y="pillPos.y"
      :colors="HIGHLIGHT_COLORS"
      :anchored="selection.anchored"
      :allow-note="selection.allowNote"
      :hl-count="selection.hlIds.length"
      :has-note="!!selectionNote"
      @explain="openExplainInChat"
      @note="onNoteAction"
      @copy="copySelection"
      @highlight="addHighlight"
      @unhighlight="deleteSelectionHighlights"
      @close="closeSelectionMenu"
    />

    <!-- 笔记浮层（第五步）：锚在高亮/选区旁边的小卡片，默认"移开就消失"。
         用户口径 2026-11-25：**可以同时开好几张** —— 每张一条 `noteCards` 记录，
         各自独立编辑 / 保存 / 删除 / 换色，`✕` 只关自己；同一条笔记 / 同一处高亮永远只有一张，
         再点入口 = 把已有的那张抬到最上层（见 `openNotePopover`）。
         `data-note-key` 是这张卡的 DOM 锚：`scheduleNoteHide` 靠它逐张问"鼠标还在它上面吗"。 -->
    <!-- 引用速览卡（悬浮标号时在旁边弹）：与笔记浮层**同一套** FloatingCard ——
         可拖、可锚定（拖过 / 点过 / 鼠标进去就钉住，移开不自动关；✕ 才关），
         多张可以同时存在（钉住的那几张不会被下一次悬浮顶掉）。
         「定位到原文」在**标题栏**（✕ 左边）：点内容不再跳转，免得想选一段文字复制时被带走
         （用户口径 2026-12-04）。 -->
    <FloatingCard
      v-for="card in peekCards"
      :key="card.key"
      :open="card.open"
      :anchor="peekViewOf(card).anchor"
      :title="peekViewOf(card).title || '引用'"
      :anchored="card.pinned"
      hide-anchor
      :width="360"
      :max-height="420"
      text-cat="chat"
      data-cite-peek
      :data-peek-key="card.key"
      @pointerdown.capture="pinCitePeek(card)"
      @mouseenter="holdCitePeek"
      @mouseleave="onCitePeekHide"
      @close="closePeekCard(card)"
    >
      <template #head-mid>
        <!-- 中/EN 切换放在**标题栏**（用户口径 2026-12-04）：正文里那块太占高度，
             看起来也像一段内容。它是内容口径的开关，不属于头部那组动作按钮，
             所以单独设计（小分段钮），不与 .fc-btn 混在一起。 -->
        <div v-if="peekViewOf(card).bilingual" class="cite-peek-lang" role="group" aria-label="显示译文或原文">
          <button type="button" :class="{ active: card.lang === 'zh' }" title="显示译文"
                  @click="card.lang = 'zh'">中</button>
          <button type="button" :class="{ active: card.lang === 'en' }" title="显示原文"
                  @click="card.lang = 'en'">英</button>
        </div>
      </template>
      <template #head-extra>
        <button
          type="button"
          class="cite-peek-jump"
          title="定位到原文"
          aria-label="定位到原文"
          data-peek-jump
          @click.stop="jumpFromPeek(card)"
        >
          <AppIcon name="locate" :size="13" />
        </button>
      </template>
      <div class="cite-peek">
        <img v-if="peekViewOf(card).imageUrl" class="cite-peek-img"
             :src="assetUrl(peekViewOf(card).imageUrl)" :alt="peekViewOf(card).caption || ''" />
        <div v-if="peekViewOf(card).caption" class="cite-peek-cap md small"
             v-html="mdToHtml(peekViewOf(card).caption, paperId)" />
        <!-- 这条引用具体指向哪一句 / 哪条公式：同一行里的两条引用靠它区分 -->
        <div v-if="peekViewOf(card).latex" class="cite-peek-quote cite-peek-formula md small"
             v-html="mdToHtml('$$' + peekViewOf(card).latex + '$$', paperId)" />
        <div v-else-if="peekViewOf(card).quote" class="cite-peek-quote md small"
             v-html="mdToHtml(peekViewOf(card).quote, paperId)" />
        <div v-if="peekViewOf(card).body" class="cite-peek-md md small"
             v-html="mdToHtml(peekViewOf(card).body, paperId)" />
        <!-- 有 latex（精确公式）时也算"有内容"：正文被上面那条公式去重清空后，不该再落一句"没有内容" -->
        <div v-if="!peekViewOf(card).body && !peekViewOf(card).imageUrl && !peekViewOf(card).latex"
             class="tiny faint">
          这一段没有可显示的内容（可能是图片页或纯公式块）
        </div>
      </div>
    </FloatingCard>

    <FloatingCard
      v-for="card in noteCards"
      :key="card.key"
      :open="card.open"
      :anchor="card.anchor"
      :anchored="card.anchored"
      :title="card.mode === 'view' ? '笔记' : ''"
      :persist-key="card.memoryKey"
      :center-on-open="card.center"
      :width="noteCardWidth(card)"
      text-cat="note"
      zoomable
      data-note-popover
      :data-note-key="card.key"
      :style="{ zIndex: card.z }"
      :focused="card.z === layerZ"
      :title-color="noteAccentColor(card)"
      hide-anchor
      :hide-close="card.mode !== 'view'"
      @pointerdown.capture="onFloatPress($event, 'note', card.key)"
      @focusin.capture="takeTop('note')"
      @close="onNoteCardClose(card)"
      @mouseleave="scheduleNoteHide(card)"
    >
      <!-- 「笔记」和左边那枚图标必须在**最左**（用户口径 2026-10-10 复查）；
           色条跟它并排、排它**右边** —— 所以色点走 `head-mid`（标题之后、按钮之前）。 -->
      <template #head-lead>
        <!-- 编辑态：**最左那枚返回箭头**（复用对话页的 back 图标）= 回到**查看态**；
             有未保存改动时它先在卡片里问一句（见 onNoteBack）。
             箭头右边就是标题位置的「编辑」两个字（`title` 传值），再往右才是色点 / 删除 ——
             一行读下来是「← 编辑 · 色点 · 删除」（用户口径 2026-11-28 十六轮）。 -->
        <button v-if="card.mode !== 'view'" class="np-back" type="button"
                title="返回" data-note-back @click="onNoteBack(card)">
          <AppIcon name="back" :size="14" />
        </button>
        <!-- 「编辑」这行字放在**插槽里紧跟箭头**，不走标题位：插槽里的两个元素只隔一层 gap，
             再补 6px 就与"箭头到卡片左沿"的 12px 完全相等（用户口径 2026-11-28 十九轮之二）。 -->
        <span v-if="card.mode !== 'view'" class="np-back-label">编辑</span>
        <!-- 查看态**不摆色点按钮**（用户口径 2026-11-28 七轮），颜色改由图标 + 标题表达：
             图标和「笔记」两个字一起染成这处高亮的颜色，零新增元素、不多一枚控件。 -->
        <AppIcon v-else name="note" :size="14" :style="noteLeadStyle(card)" />
      </template>
      <template #head-mid>
        <!-- 相对时间紧贴在「笔记」标题右侧（用户口径 2026-11-26）：它是这条笔记的属性，
             放到按钮区最左边会看着像另一组东西。编辑态不显示。
             ⚠️ 按钮都在右侧（head-extra / head-tail），这一格只放属性文字 ——
             用户口径 2026-11-28 十七轮：编辑态从左到右 = 删除 · 更多 · 色点 · 保存。 -->
        <span v-if="card.mode === 'view' && relTime(card.created)" class="np-time">{{ relTime(card.created) }}</span>
      </template>
      <template #head-extra>
        <!-- 编辑态**最靠左**的那枚：删除（用户口径 2026-11-28 十七轮：删除 · ⋯ · 色点 · ✓）。
             它后面才是 ⋯（由 FloatingCard 渲染），所以两者相邻但顺序是定死的；
             `@click.stop` 是为了不触发卡片的"按下即锚定"那条捕获逻辑之外的其他点击行为。 -->
        <button v-if="card.mode !== 'view' && card.noteId" class="np-head-save danger" type="button"
                :disabled="card.busy" title="删除笔记" data-note-delete
                aria-haspopup="menu"
                :aria-expanded="card.confirmDel"
                @click.stop="openNoteDelMenu($event, card)">
          <AppIcon name="trash" :size="13" />
        </button>
        <!-- 按钮组**最左**（在相对时间之前）：「定位到原文」。
             这枚键原来放的是**手动锚定**开关（`FloatingCard` 的 `hideAnchor` 一关就有）；
             本轮按用户口径 2026-11-23 换掉了：锚定改**自动** —— 见模板根上那条
             `@pointerdown.capture="onFloatPress($event, 'note', card.key)"`：点一下、拖标题、
             拖下角放大任一动作都会把**这张**卡锚住（翻页 / 滚动发生在卡片外，不触发）。
             所以这里不再需要一枚手动开关，位置让给"跳回原文"。 -->
        <!-- 定位只在**查看态**给：编辑态右上是 ✓保存 与垃圾桶，再塞一枚只会挤（用户口径 2026-11-24）。 -->
        <!-- 查看态的**编辑**：先定位、后编辑（动作顺序与视觉顺序一致）。图标钮几何与
             删除 / 保存 / 定位同一套；语义只靠图标 + hover 底色，不给它上主题色。 -->
        <button v-if="card.mode === 'view' && card.noteId" class="np-head-save edit" type="button"
                title="编辑这条笔记" data-note-edit
                @click="card.mode = 'edit'; card.draft = card.content">
          <AppIcon name="edit" :size="13" />
        </button>
        <button v-if="card.mode === 'view' && card.rowId" class="np-head-save locate" type="button"
                title="定位到原文" data-note-locate
                @click="locateNoteRow(card)">
          <AppIcon name="locate" :size="13" />
        </button>
        <!-- 时间已经挪到标题右侧（见 head-mid）；这里只剩「定位」——它在「编辑」左边，
             两枚相邻：先定位再看，动作顺序与视觉顺序一致（用户口径 2026-11-26）。 -->
      </template>
      <!-- 编辑态最右那两枚：**色点 → ✓ 保存**（用户口径 2026-11-28 十七轮）。
           它们必须排在 ⋯ 之后，所以走 `head-tail` 插槽（FloatingCard 里那一格渲染在 ⋯ 与 ✕ 之间）。
           色点与头部其它按钮同几何；点开是贴着它的换色菜单。 -->
      <template #head-tail>
        <button
          v-if="card.highlightId && card.mode !== 'view'"
          type="button"
          class="np-color-btn"
          data-note-color
          :class="{ on: noteColorMenu.open && noteColorMenu.key === card.key }"
          title="高亮颜色"
          aria-haspopup="menu"
          :aria-expanded="noteColorMenu.open && noteColorMenu.key === card.key"
          @click.stop="openNoteColorMenu($event, card)"
        >
          <span class="np-color-dot hl-swatch" :class="card.color"
                :style="colorDotStyle(card.color)" />
        </button>
        <button v-if="card.mode !== 'view'" class="np-head-save" type="button"
                :disabled="card.busy" title="保存笔记" data-note-save
                @click="saveNotePopover(card)">
          <AppIcon name="check" :size="13" />
        </button>
      </template>
      <!-- 「⋯」二级菜单里**不再放定位**（用户口径 2026-11-28 二十轮）：定位已经是头部那枚常显键，
           二级里再来一枚只是重复。于是本卡也不再传 `#more-items` 插槽 ——
           FloatingCard 里那条竖分隔线只在有插槽时才画，不留空槽就不会在菜单最前面多出一道分隔线。 -->
      <!-- 删除键已挪到头部**最左**（见 head-mid）：编辑态的顺序是 删除 · 色点 · … · 保存 · ✕ -->

      <!-- ⚠️ **不显示高亮的那段原文**（用户口径 2026-10-08："浮层不要显示论文正文部分"）：
           浮层贴着那处高亮，原文就在眼皮底下，再抄一遍只会把卡片撑长、还多一层"这是哪一段"的噪音。
           要看按的是哪一段，看浮层**贴着哪儿**就够了。 -->
      <!-- 提示文案已删（用户口径 2026-10-10："去掉多余文字说明"）：**卡片本身就是输入框** ——
           空着就是"写一条"，有内容就是"编辑"；标题里已经有"已高亮"，正文里再解释一遍是噪音。 -->
      <textarea
        v-if="card.mode !== 'view'"
        v-model="card.draft"
        class="np-edit"
        :rows="noteEditRows(card)"
        data-note-edit
      />
      <!-- 垃圾桶的二次确认：同一块卡内弹窗（用户口径 2026-11-28 十五轮），只问删除 / 取消 -->
      <div v-if="card.confirmDel" class="np-confirm" data-note-confirm-del>
        <div class="np-confirm-box" role="dialog" aria-modal="true" aria-label="确认删除这条笔记">
          <p class="np-confirm-text">删除这条笔记？</p>
          <div class="np-confirm-acts">
            <button type="button" class="btn danger" data-note-del-confirm
                    @click="onNoteDelConfirm(card)">删除</button>
            <button type="button" class="btn" data-note-del-cancel
                    @click="card.confirmDel = false">取消</button>
          </div>
        </div>
      </div>
      <!-- 编辑态点返回、且有未保存改动：**卡片内部**的一小块确认（不用全站那个居中遮罩。
           三个选项与用户口径一致：保存 / 不保存 / 取消）。 -->
      <div v-if="card.confirmBack" class="np-confirm" data-note-confirm>
        <div class="np-confirm-box" role="dialog" aria-modal="true" aria-label="是否保存更改">
          <p class="np-confirm-text">是否保存更改？</p>
          <div class="np-confirm-acts">
            <button type="button" class="btn primary" data-note-confirm-save
                    @click="confirmBackSave(card)">保存</button>
            <button type="button" class="btn" data-note-confirm-discard
                    @click="confirmBackDiscard(card)">不保存</button>
            <button type="button" class="btn" data-note-confirm-cancel
                    @click="card.confirmBack = false">取消</button>
          </div>
        </div>
      </div>
      <!-- 查看态正文：**用户自己写的笔记**走 `{ breaks: true }`（用户口径 2026-10-10）：
           不能再跑 `normalizeSoftBreaks` —— 它会把作者敲的回车接回去（"第一行第二行"），
           那是给 PDF 抽取文本用的。 -->
      <div v-else-if="card.mode === 'view'" class="np-view md small" data-note-view
           v-html="mdToHtml(card.content, null, { breaks: true })" />
      <!-- 底部**不再传 footer 插槽**（用户口径 2026-10-10："不要有选择的文字框"）：
           那个 footer 就算只有一行按钮，也会带出一条 1px 上线，看着像正文外面套了一圈框。
           编辑 / 保存 / 删除 / 关闭 全走头部按键（铅笔 / ✓ / 垃圾桶 / ✕）。 -->
    </FloatingCard>

    <!-- 删除的二次确认**也在卡片里**（用户口径 2026-11-28 十五轮）：与「返回」那块同一套
         `.np-confirm` 观感与交互（渲染在各自卡片的插槽里，见上面 `card.confirmDel` 那一段）。 -->

    <!-- 换色菜单：贴着那枚圆钮弹开。同样 Teleport 到 body（浮层壳是 overflow: hidden，留在里面会被裁），
         弹出时从按钮那一角张开（`transform-origin` 由 openNoteColorMenu 写）。 -->
    <Teleport to="body">
      <div
        v-if="noteColorMenu.open"
        class="note-color-menu"
        data-note-color-menu
        role="menu"
        aria-label="高亮颜色"
        :style="{ top: noteColorMenu.top + 'px', left: noteColorMenu.left + 'px',
          transformOrigin: noteColorMenu.origin }"
      >
        <div class="ncm-row">
          <button
            v-for="c in HIGHLIGHT_COLORS"
            :key="c"
            type="button"
            class="hl-swatch"
            :class="[c, { on: colorMenuCard?.color === c }]"
            :title="`换成 ${c} 色`"
            data-note-color-pick
            @click="pickNoteColor(c)"
          />
          <!-- 自定义色（用户口径 2026-10-10："少了自定义色彩"）—— 与胶囊上那枚同一做法 -->
          <label class="hl-swatch custom" title="自定义颜色">
            <input
              type="color"
              :value="colorMenuCard && isHexColor(colorMenuCard.color) ? colorMenuCard.color : '#ffd700'"
              data-note-custom aria-label="自定义高亮颜色"
              @input="pickNoteColor(($event.target as HTMLInputElement).value, false)"
            >
          </label>
        </div>
      </div>
    </Teleport>

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
      :above="ctxMenu.above"
      :compact="ctxMenu.compact"
      :items="ctxMenu.items"
      @select="onCtxSelect"
      @color="onCtxColor"
      @placed="ctxMenu.rect = $event"
      @close="ctxMenu.open = false; ctxMenu.rect = null"
    />

    <!-- 触摸选区的两个手柄：长按选出一个词之后，拖它们把范围撑开（桌面不显示，鼠标直接拖就行） -->
    <template v-if="selHandles">
      <div
        class="sel-handle start"
        :style="{ left: selHandles.start.x + 'px', top: selHandles.start.y + 'px' }"
        @pointerdown.stop="onHandleDown('start', $event)"
        @touchstart.stop="onHandleTouchStart('start', $event)"
      ><i /></div>
      <div
        class="sel-handle end"
        :style="{ left: selHandles.end.x + 'px', top: selHandles.end.y + 'px' }"
        @pointerdown.stop="onHandleDown('end', $event)"
        @touchstart.stop="onHandleTouchStart('end', $event)"
      ><i /></div>
    </template>

    <!-- 大图查看器：毛玻璃胶囊（放大 / 百分比 / 缩小 / 重置 / 关闭）。手写批注在阅读器页面上。 -->
    <ImageViewer
      v-if="zoomImage"
      :target="zoomImage"
      :gallery="zoomGallery"
      :origin="zoomOrigin"
      icon="image"
      :title="zoomImage.caption || (zoomImage.page ? `第 ${zoomImage.page} 页` : '插图')"
      @close="zoomImage = null"
    />

    <!-- 表格 / 公式 / 插图的浮层：**可以叠多张**，每张自带层级（点哪张哪张到最上层）。
         左上角图标 + 标题（表格用它的说明、公式写「公式」、插图用图注）。 -->
    <BlockOverlay
      v-for="(o, oi) in blockOverlays"
      :key="o.id"
      :spawn="oi"
      :icon="o.icon"
      :title="o.title"
      :title-md="o.titleMd || ''"
      :anchor="o.anchor || null"
      :markdown="o.markdown || ''"
      :image-url="o.imageUrl || ''"
      :style="{ zIndex: o.z }"
      :focused="o.z === layerZ"
      :persist-key="'bo:' + o.key"
      @activate="focusBlockOverlay(o.id)"
      @zoom-image="o.imageUrl && (zoomImage = { url: o.imageUrl, caption: o.title })"
      @close="closeBlockOverlay(o.id)"
    />

    <Modal
      :open="mdView.open"
      title="Markdown"
      wide
      :full="mdView.full"
      @contextmenu="onContentContextMenu"
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
          <AppIcon :name="mdView.full ? 'collapse-diag' : 'expand-diag'" :size="14" />
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
/* ---------------- 引用速览卡（悬浮标号弹出的那张） ----------------
 * 整块可点 = 定位到原文；里面是**只读**内容，所以不放任何按钮 ——
 * 唯一的交互就是这张卡片本身，点哪都跳（比再放一枚「定位」小按钮更省一次瞄准）。 */
/* 正文区不是可点区域（定位在标题栏），所以既没有手型也没有悬浮高亮 ——
   那层淡底只会让人以为点这里会发生什么（用户口径 2026-12-04）。 */
.cite-peek {
  display: flex; flex-direction: column; gap: 8px;
  border-radius: var(--radius-sm);
}
.cite-peek-img {
  display: block; width: 100%; max-height: 200px; object-fit: contain;
  border-radius: var(--radius-sm); background: var(--bg-sunken);
}
.cite-peek-cap { color: var(--text-dim); }
.cite-peek-md { max-height: 190px; overflow: auto; }
/* 引用片段：左竖条 + 淡底，与正文分开 —— 它回答的是这条引用指向哪一句，不是这一段讲了什么。 */
.cite-peek-quote {
  border-left: 2px solid color-mix(in srgb, var(--accent) 55%, transparent);
  background: color-mix(in srgb, var(--accent) 6%, transparent);
  border-radius: 0 var(--radius-sm) var(--radius-sm) 0;
  padding: 4px 8px; max-height: 150px; overflow: auto;
}
.cite-peek-formula { display: flex; justify-content: center; overflow-x: auto; }
/* 原文对照：小一号、淡一档，只作参照，不抢正文的注意力 */
/* 中/英 切换（用户口径 2026-12-04）：**胶囊里两个圆**，高度与右侧的圆钮同高（25px），
   于是标题行右半边是一条对齐的线：胶囊 25 → 圆钮 25 → ✕ 25。
   只留一个字（中 / 英）：两个字的「译文 / 原文」在这个宽度里既挤又抢注意力。
   ⚠️ 插槽内容在**父组件作用域**里渲染，FloatingCard 的 scoped `.fc-btn` 到不了这儿 ——
   所以这里必须把自己那套几何写全（笔记卡那排按钮当年就是因此另起了一套）。 */
.cite-peek-lang {
  flex: none; display: inline-flex; align-items: center; gap: 1px; padding: 1px;
  height: 25px; border-radius: 999px;
  background: color-mix(in srgb, var(--text) 6%, transparent);
}
.cite-peek-lang button {
  width: 23px; height: 23px; padding: 0; border: 0; border-radius: 50%;
  display: inline-flex; align-items: center; justify-content: center;
  font: inherit; font-size: calc(10.5px * var(--ts, 1)); font-weight: 700; line-height: 1;
  color: var(--text-dim); background: transparent; cursor: pointer;
  transition: background var(--dur) var(--ease), color var(--dur) var(--ease);
}
.cite-peek-lang button:hover { color: var(--accent-hover); }
.cite-peek-lang button.active {
  background: var(--bg-elev); color: var(--accent-text); box-shadow: var(--shadow-1);
}
.cite-peek-lang button:focus-visible { outline: none; box-shadow: var(--ring); }

/* 「定位到原文」：与 FloatingCard 里那排圆钮**逐条同规格**（25px / 文字色 6% 淡底 / 主题色图标 /
   悬停只换底色 / 按下缩一格）。模板里不再挂 .fc-btn —— 它在子组件作用域，套不上。 */
.cite-peek-jump {
  flex: none; width: 25px; height: 25px; padding: 0; border: 0; border-radius: 50%;
  display: inline-flex; align-items: center; justify-content: center;
  background: color-mix(in srgb, var(--text) 6%, transparent); color: var(--accent-text);
  cursor: pointer;
  transition: background var(--dur) var(--ease), color var(--dur) var(--ease),
              transform var(--dur-fast) var(--ease);
}
.cite-peek-jump:hover { background: var(--bg-hover); color: var(--accent-hover); }
.cite-peek-jump:active { transform: scale(.94); }
.cite-peek-jump:focus-visible { outline: none; box-shadow: var(--ring); }

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
  color: var(--text-dim); font-size: calc(13px * var(--ts, 1));
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
  /* 工具栏里所有**图标**统一走主题色（用户口径 2026-12-02：换主题后原来的正文色不搭）。
     只染图标 —— 段控件（原文/译文/对照）与文字按钮保持正文色，否则三枚 tab 看着像全选中了。
     收在一个变量里：胶囊、左右两枚 solo、⋯ 触发钮、侧栏开关都读它，改一处就全改。 */
  --cap-icon-color: var(--accent-text);
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
/* 右侧胶囊装**两枚**动作键（更多 / 全屏）：它是 `.cap-solo` 里唯一的非正圆特例 ——
   宽度按内容撑开，高度就是胶囊高度本身（用户口径 2026-11-19：竖向大小要与左侧那枚
   侧栏开关对齐，所以按钮不再内缩成 30px，而是撑满 `--cap-h`）。 */
.cap-solo.right { width: auto; padding: 0; gap: 0; }
.cap-act {
  width: var(--cap-h); height: var(--cap-h); flex: none;
  display: flex; align-items: center; justify-content: center;
  border: 0; border-radius: 999px; background: transparent; color: var(--cap-icon-color);
  cursor: pointer;
  transition: background var(--dur) var(--ease), color var(--dur) var(--ease);
}
.cap-act:hover { background: var(--bg-hover); }
.cap-act:disabled { opacity: .45; cursor: default; background: transparent; }
/* 「⋯」是 Menu 组件的触发钮（自带 .btn 的底色与 32px 尺寸）—— 它在子组件内部，
   父组件的 scoped 选择器够不到，必须 `:deep()`；不穿透会看到胶囊里又套一个小圆钮。 */
.reader-toolbar .cap-solo.right :deep(.menu-root) { width: var(--cap-h); height: var(--cap-h); }
.reader-toolbar .cap-solo.right :deep(.menu-trigger) {
  width: 100%; height: 100%;
  border-color: transparent; background: transparent; box-shadow: none;
  color: var(--cap-icon-color);
}
/* 左侧那枚侧栏开关是子组件，它自己的 scoped 规则写着正文灰 —— 父级要穿透进来才盖得住 */
.reader-toolbar .cap-solo.left :deep(.aside-toggle) { color: var(--cap-icon-color); }
.reader-toolbar .cap-solo.right :deep(.menu-trigger:hover),
.reader-toolbar .cap-solo.right :deep(.menu-trigger.is-open) {
  background: var(--bg-hover); border-color: transparent;
}
/* 图标口径跟左侧那枚侧栏开关一致（17px）：Menu 触发钮里的图标是写死的 14，
   不覆盖的话两枚按钮并排时右侧会明显小一圈。 */
.reader-toolbar .cap-solo.right :deep(.menu-trigger svg) { width: 17px; height: 17px; }

.reader-capsule {
  pointer-events: auto;
  position: relative;
  cursor: grab;
  display: flex; align-items: center; gap: 4px;
  margin-top: 10px; padding: 0 6px;
  height: var(--cap-h);
  /* 两侧各给一枚独立胶囊留位：三块玻璃不许叠在一起 */
  max-width: calc(100% - 168px);
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
    color-mix(in srgb, var(--bg-elev) var(--glass-bar-top, 80%), transparent),
    color-mix(in srgb, var(--bg-elev) var(--glass-bar-bottom, 68%), transparent));
  backdrop-filter: blur(48px) saturate(2.6);
  -webkit-backdrop-filter: blur(48px) saturate(2.6);
  box-shadow: var(--shadow-flyout);
}
/* 色块：照 iPad 那套的排布 —— 一排**大一点**的实心圆（26px），选中项外面套一圈同色环，
   而不是"换边框色"那种容易被底色吃掉的写法（用户口径第 1 条：优化颜色排版）。 */
.ink-palette-dot {
  width: 24px; height: 24px; padding: 0; cursor: pointer;
  border: 2px solid var(--bg-elev); border-radius: 999px;
  box-shadow: 0 0 0 1px color-mix(in srgb, var(--text) 14%, transparent);
  transition: transform var(--dur-fast) var(--ease), box-shadow var(--dur) var(--ease);
}
.ink-palette-dot:hover { transform: scale(1.08); }
/* 选中：白圈 + 一圈**文字色**外环（浅色/深色主题下都读得出来），与旁的色点拉开一档 */
.ink-palette-dot.active {
  border-color: var(--bg-elev);
  box-shadow: 0 0 0 1px var(--bg-elev), 0 0 0 3px color-mix(in srgb, var(--text) 72%, transparent);
}
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
/* 热区再放宽一档（用户口径：拖胶囊容易按漏）：竖向 ±10px、横向 ±8px，视觉仍是那三个小点 */
.cap-grip::before {
  content: ''; position: absolute; inset: -10px -8px; border-radius: 8px;
}
.cap-grip i { display: block; width: 2px; height: 2px; border-radius: 999px; background: var(--text-faint); }
.cap-grip:hover i { background: var(--text-dim); }
.reader-capsule.dragging .cap-grip { cursor: grabbing; }

/* 画笔二级菜单：胶囊正下方一条**横排**（颜色 / 粗细），与弹出面板同一套玻璃 */
.ink-options {
  /* fixed：位置由 `inkOptionsStyle` 按胶囊实时算（见 placeInkOptions） */
  position: fixed; transform: translateX(-50%);
  display: flex; flex-direction: column; gap: 8px; z-index: 45; pointer-events: auto;
  padding: 8px 10px; border-radius: 14px;
  /* 与胶囊/右键菜单同一块玻璃（用户口径 2026-10-18） */
  background: linear-gradient(180deg,
    color-mix(in srgb, var(--bg-elev) var(--glass-bar-top, 80%), transparent),
    color-mix(in srgb, var(--bg-elev) var(--glass-bar-bottom, 68%), transparent));
  backdrop-filter: blur(48px) saturate(2.6);
  -webkit-backdrop-filter: blur(48px) saturate(2.6);
  box-shadow: var(--shadow-flyout);
}
/* 两排：上排颜色（24px 圆点、间距 8）、下排粗细（24px 圆钮）—— 各收一档，
   让整块从「一大块面板」回到「胶囊下面一小条」（用户口径：还是太丑）。 */
.ink-options-row { display: flex; align-items: center; gap: 8px; }
.ink-options-row + .ink-options-row {
  padding-top: 6px; border-top: 1px solid var(--divider); gap: 4px;
}
.ink-options-label { width: 28px; font-size: calc(11.5px * var(--ts, 1)); color: var(--text-faint); }
/* 粗细：三档**图标**（只画点，不写字；选中态给一圈浅底） */
.ink-width-icon {
  display: inline-flex; align-items: center; justify-content: center;
  width: 24px; height: 24px; padding: 0; cursor: pointer;
  border: 0; border-radius: 999px; background: transparent; color: var(--text-dim);
}
.ink-width-icon:hover { background: var(--bg-hover); color: var(--text); }
.ink-width-icon.active { background: var(--accent-soft, var(--bg-hover)); color: var(--accent); }

/* 自定义颜色：一枚"彩虹点"，点它弹浏览器原生取色器（input 藏起来但可点） */
.ink-color-custom {
  position: relative; display: inline-flex; align-items: center; justify-content: center;
  width: 24px; height: 24px; cursor: pointer; border-radius: 999px;
}
/* 「当前色是自选色」时，这枚彩虹点也带一圈**同色点同款的选中环**（用户口径：自选色放第一，也要看得出选中） */
.ink-color-custom.active { box-shadow: 0 0 0 1px var(--bg-elev), 0 0 0 3px color-mix(in srgb, var(--text) 72%, transparent); }
.ink-color-custom-dot {
  display: block; width: 24px; height: 24px; border-radius: 999px;
  background: conic-gradient(#e8384f, #f0a11a, #2aa775, #2f6fed, #8b5cf6, #e8384f);
  box-shadow: 0 0 0 1px color-mix(in srgb, var(--text) 14%, transparent);
}
.ink-color-custom input {
  position: absolute; inset: 0; width: 100%; height: 100%;
  opacity: 0; cursor: pointer; border: 0; padding: 0;
}

/* 清空本页笔迹：一条滑块（滑到右端触发，一次撤销可整个回来） */
.ink-clear { min-width: 248px; }
.ink-clear-label { font-size: calc(12px * var(--ts, 1)); color: var(--text-dim); white-space: nowrap; }
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
/* 沉浸模式（全屏的退路）：把应用顶部标签栏藏掉，阅读器自己占满整屏。
   .tabbar 在 App 层、本组件的 scoped 样式够不到，所以用 :global 写全局选择器。 */
:global(html.fs-immersive .tabbar) { display: none; }

/* 触摸选区的两端手柄：一枚小圆点 + 一根小尾巴，位置由 refreshSelHandles 按选区矩形给 */
/* 抓取区比看得见的圆点大得多（44px）：手指要一按就中，不能"不太灵敏" */
.sel-handle {
  /* z-index 要在**底部操作表的遮罩（319）/ 面板（320）之上**：
     否则操作表开着的时候按手柄只会点到遮罩，拖拽根本起不来（实测踩到）。 */
  position: fixed; z-index: 330; width: 44px; height: 44px;
  margin: -22px 0 0 -22px; display: flex; align-items: center; justify-content: center;
  touch-action: none; cursor: grab;
}
.sel-handle:active { cursor: grabbing; }
/* 圆点：**12px**（用户口径：手柄的圆小一点）、白圈 2px + 一档柔投影 —— 压在文字上既分得开又不糊 */
.sel-handle i {
  position: relative; z-index: 1;
  display: block; width: 12px; height: 12px; border-radius: 999px;
  background: var(--accent);
  box-shadow: 0 0 0 2px var(--bg-elev), 0 1px 4px rgba(0, 0, 0, .22);
  transition: transform .12s var(--ease);
}
/* 按住时圆点轻轻放大一格：给手指一点"拿到了"的反馈 */
.sel-handle:active i { transform: scale(1.14); }
/* 小尾巴指向文字那一行（上端指向下方、下端指向上方），一眼看出是哪一头。
   ⚠️ translateX(-50%) 不能省：只写 left:50% 的话线会往右偏半个线宽（用户口径
   2026-10-18"直接的竖线没有居中"）。
   造型：让尾巴**从圆点底下钻出来**（z-index 0、起点落在圆心），中段被圆点盖住，
   看上去是一枚连续的图钉；以前尾巴自带走一圈白描边，边上糊成一个小方框，
   用户口径 2026-10-18"手柄样式优化一下"。 */
.sel-handle::after {
  content: ''; position: absolute; left: 50%; transform: translateX(-50%); z-index: 0;
  /* 直线 26px → **22px**（用户口径：略微减少底部竖线的长度）。
     起点仍在圆心（44px 热区的中心 = 22），露在圆点外面的那一段是 6px。 */
  width: 3px; height: 22px; border-radius: 999px;
  background: var(--accent);
}
/* 起点手柄：圆点在上、尾巴往下（指向文字那一行）；终点手柄镜像。
   两头对称：都被圆点的白圈盖住中段，露出约 10px。 */
.sel-handle.start::after { top: 22px; }
.sel-handle.end::after { bottom: 22px; }

/* 触摸长按不再弹系统 callout（iOS）：我们自己在 onContentTouchStart 里接管成右键菜单。
   ⚠️ 只关 callout，**不关选择**：划词、高亮、复制都还要用原生选区。 */
.reader-content, .reader-content * { -webkit-touch-callout: none; }
/* 双击缩放交给脚本：双击在这里表示"取消选取"（用户口径 2026-10-18）。
   manipulation = 允许拖动/双指缩放，只禁掉双击缩放那一下。 */
.reader-content { touch-action: manipulation; }

.reader-capsule .cap-group .btn.tool-on {
  /* ⚠️ 两层原因：
     ① 必须盖过 `.btn:hover`（优先级更高，而且鼠标通常正停在被选中的那枚上）；
     ② 用**主色淡底**而不是白底 —— 胶囊本身就是浅色玻璃，白 pill 在白玻璃上等于没选中。 */
  background: color-mix(in srgb, var(--accent) 18%, transparent) !important;
  box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--accent) 44%, transparent) !important;
  color: var(--accent-text) !important;
}
/* 胶囊里的**图标**统一走主题色（用户口径 2026-12-02：换主题后原来的正文色不搭）。
   只染 icon 钮：段控件与文字按钮仍是正文色，三枚 tab 才不会看起来全选中。
   ⚠️ 写在 .tool-on 与下面两条墨色规则**之前**：那两条带 !important、且同权重靠顺序取胜。 */
.reader-capsule .cap-group .btn.icon { color: var(--cap-icon-color); }
/* 画笔 / 荧光笔那两枚图标用**当前选中的墨色**（用户口径 2026-10-18，荧光笔 2026-10-20 补上：
   以前只有画笔染色，荧光笔那枚永远是黑的，看不出它现在是黄还是绿）。
   ⚠️ 必须写在 .tool-on **之后**、并且同样带 !important：两条规则权重相同，靠顺序取胜；
   写在前面的话选中画笔时图标会被 .tool-on 染成强调色（实测踩过）。
   荧光笔不直接用它的墨色（#ffd60a 这类浅黄描在浅色胶囊上几乎看不见），
   混 62% 的正文色把它压深：既看得出是哪个颜色，也不会糊成一片淡黄。 */
.reader-capsule .cap-group .btn[data-ink-tool='pen'] { color: var(--pen-color, currentColor) !important; }
.reader-capsule .cap-group .btn[data-ink-tool='marker'] {
  color: color-mix(in srgb, var(--marker-color, currentColor) 62%, var(--text)) !important;
}
.reader-capsule .cap-group .seg {
  border-color: transparent;
  background: color-mix(in srgb, var(--text) 5%, transparent);
}
.reader-capsule .cap-group .seg button.active { background: var(--bg-elev); box-shadow: var(--shadow-1); }
/* 胶囊里紧凑一档：32px 的控件缩到 28px，整条胶囊能短 ~10%，仍然是可点尺寸 */
.reader-capsule .cap-group .btn.icon { width: 28px; height: 28px; }
.reader-capsule .cap-group .seg { height: 28px; }
/* 胶囊里的**文字**（原文 / 译文 / 对照、Aa 那几枚）悬浮也走主题色（用户口径 2026-12-03）。
   base.css 的 .seg button:hover 与 .btn:hover 都把字改成正文黑 —— 在这枚玻璃胶囊上，
   黑字是最扎眼的一种颜色，跟旁边已经染成主题色的图标完全不在一条线上。
   ⚠️ 排掉 .active / .primary / .tool-on / 批注工具：选中项与实底钮的语义不能被 hover 抹平，
   画笔与荧光笔更是各有自己的墨色（那两条带 !important）。 */
.reader-capsule .cap-group .seg button:not(.active):hover { color: var(--accent-hover); }
.reader-capsule .cap-group .btn:not(.icon):not(.primary):not(.tool-on):hover:not(:disabled) { color: var(--accent-hover); }
.reader-capsule .cap-group .seg button { height: 22px; padding: 0 10px; font-size: calc(12px * var(--ts, 1)); }
.reader-capsule .cap-group .btn:not(.icon) { height: 28px; font-size: calc(12px * var(--ts, 1)); }
.reader-capsule .cap-group .badge { height: 20px; }
/* 「Aa」：字号 + 页图缩放合成一个入口（用户口径：缩放和字号合并），
   按钮上顺手带出当前缩放值 —— 也是自检脚本读的那个 `.tiny.faint.nowrap`。 */
.reader-capsule .cap-group .cap-aa { gap: 5px; padding: 0 9px; font-size: calc(13px * var(--ts, 1)); font-weight: 600; }
.reader-capsule .cap-group .cap-aa .tiny { font-size: calc(10.5px * var(--ts, 1)); font-weight: 500; }

/* 弹出面板里的一行：左标签 + 右分段/步进 */
.cap-row { display: flex; align-items: center; gap: 12px; }
.cap-row-label { flex: none; width: 68px; font-size: calc(12px * var(--ts, 1)); color: var(--text-dim); }
.cap-zoom { display: flex; align-items: center; gap: 6px; }
.cap-zoom-value { min-width: 46px; text-align: center; font-size: calc(13px * var(--ts, 1)); }

/* 侧栏头部：**只有一条页签胶囊**，左端留出那枚圆形开关的位置（12 + 38 + 12 = 62）。
   高度 58 是为了让胶囊中线（29px）与顶栏那三块玻璃的中线对齐 —— 四样东西在一条线上。 */
.aside-head {
  flex: none; display: flex; align-items: center;
  height: 58px; padding: 0 12px 0 62px;
}

/* 浮窗顶部那枚**拖动指示器**（iOS 弹层那枚抓手）：画在对话容器自己身上 —— 浮窗没有单独的
   标题栏（会话头那一行兼作抓手，见 `onFloatCardDown`），所以这一条 + 头部多留的 12px
   就是"这里能拖着走"的全部提示。
   反馈口径与笔记浮层（`FloatingCard` 的 `.fc-grab-pill`）一致（用户口径 2026-11-23）：
   **静止 36×4 / 20% 文字色，拖动中 46 宽 / 34%**。
   ⚠️ 居中只能写 `left: 50%` + `translateX(-50%)`（静态、不参与过渡）：宽 36→46 时若靠
   `margin-left: -18px` 居中，变宽那一帧会整体右偏 5px（就是"越拉越偏"）。
   ⚠️ transition **只列 width 与 background-color**：写 `all` 会把 transform 也带进补间。 */
/* ⚠️ 用**后代**选择器：药丸现在是 `.chat-grab` 的子元素（那一整条才是真把手） */
.reader > .chat-host.float .chat-grip-pill {
  /* 与笔记浮层的抓手（FloatingCard 的 .fc-grab-pill）逐字同一套：
     34x4 / 19% 文字色 → 拖动中 46 / 34%，220ms、同一条回弹曲线；只补间 width 与 background-color。 */
  /* 用**真实子元素**而不是 ::before：父容器 .chat-host 的 left/top/width/height 是拖动中逐帧写的，
     伪元素跟着宿主一起被标记为 dirty，过渡会被打断 —— 那正是"浮窗抓手没有回弹感"的原因。 */
  display: block; position: absolute; top: 5px; left: 50%;
  /* ⚠️ 变宽走 **transform: scaleX**，不再补间 width（用户口径 2026-12-03 第 4 条：抓手动画发卡）。
     width 是布局属性：每帧都要重排 + 重绘这一条，而它正压在**正在被拖的合成层**上，
     一帧重排就把拖动的那点余量吃掉了。scaleX 只是合成器换贴图，主线程一帧都不占。
     46/34 = 1.3529（与原来看起来完全等宽的终态）。 */
  width: 34px; height: 4px; border-radius: 999px; z-index: 5; pointer-events: none;
  transform: translateX(-50%) scaleX(1);
  background: color-mix(in srgb, var(--text) 19%, transparent);
  will-change: transform;
  transition: transform 220ms cubic-bezier(.22, 1.2, .36, 1),
              background-color 220ms var(--ease);
}
/* 拖动中才变宽（`floatDragging` = `.reader.float-dragging`，由 onFloatPointerDown 直接挂在根元素上）。
   ⚠️ 这里**不要**再写 `:active` 兜底：:active 在整个浮窗上都会命中，于是"点卡片正文"也会让抓手
   变宽（用户口径 2026-11-24 第 1 条）；而且 `:active` 比类名晚一拍生效，两种来源的补间节奏还不一样
   —— 那正是"点抓手时的动画没有点别处细腻"的来源。 */
/* 顶部那条**真抓手**（照 FloatingCard 的 `.fc-grab`）：一整条 14px 高、可命中、光标 grab。
   药丸只是它中间的装饰 —— 以前药丸自己 `pointer-events: none`，顶部这一条命中的是下面的层，
   光标自然不是抓取手（用户口径：和笔记浮层不一致）。 */
.chat-grab {
  position: absolute; left: 0; right: 0; top: 0; height: 14px; z-index: 6;
  display: flex; align-items: center; justify-content: center;
  cursor: grab; touch-action: none;
}
.chat-grab:active { cursor: grabbing; }
/* 拖动中这条也保持「抓紧」（行内那句只在静止时） */
.reader.float-dragging > .chat-host.float .chat-grab { cursor: grabbing !important; }
.reader > .chat-host:not(.float) > .chat-grab { display: none; }
.reader > .chat-host:not(.float) .chat-grip-pill { display: none; }
/* 拖头期间**保住合成层**：卡片走 transform 移位（见 paintFloatLive），有了 will-change 就
   不必等合成器自己判断要不要提升，第一帧就跟手，也不会在提升那一刻抖一下。

   ⚠️ `transition: none` 是必须的（用户口径 2026-12-03 第 1 条：松手时浮层会抖一下）。
   `.reader > .chat-host` 那条常驻的 `transition: transform .26s` 本来是给"侧栏开合时
   整块滑出去"用的；手势里每帧改 transform 会让它**每帧重新起一次 260ms 补间**
   （跟手变成拖拽感），松手把 transform 撤回 `none` 时更要补间着滑回起点 —— 那就是抖动。
   手势期间关掉它，松手那一下就是纯 left/top 的瞬移落定。 */
.reader.float-dragging > .chat-host.float { will-change: transform; transition: none; }
/* 拖动中变宽变亮：**与笔记浮层（`.fc-grab.dragging .fc-grab-pill`）逐字同一套** ——
   类挂在抓手条自己身上，另加 `:active` 兜底（按下即反馈，不必等手势判定）。
   ⚠️ 触发源**只有** `.chat-grab` 一个：以前是 `.reader.float-dragging`（挂在根元素上），
   而那个类为了防「拖完闪一下」要晚一帧才撤 —— 药丸的回弹跟着被拖后一帧，
   与笔记抓手看起来就不同步了（用户口径 2026-12-03 第 5 条）。 */
.reader > .chat-host.float .chat-grab.dragging .chat-grip-pill,
.reader > .chat-host.float .chat-grab:active .chat-grip-pill {
  transform: translateX(-50%) scaleX(1.3529);
  background: color-mix(in srgb, var(--text) 34%, transparent);
}
/* 会话头 / 列表头那一行：浮窗里往上留出抓手的位置，并整行当拖动把手 */
/* 拖动期间**不写任何 user-select**（用户口径 2026-11-24 第 2 条）：
   `.reader.float-dragging { user-select: none }` 会连浮层里的对话/笔记文字一起锁死 ——
   用户想在浮层里框选一段复制就选不动。真正要挡的 PDF 文字层由 `utils/floatDrag.ts`
   往 `<html>` 挂的 `cy-float-gesture` 定向处理（见 base.css），其余内容由手势循环逐帧清选区。 */
.reader > .chat-host.float :deep(.thread-head),
.reader > .chat-host.float :deep(.list-head) {
  padding-top: 12px; cursor: grab; user-select: none; touch-action: none;
}
.reader > .chat-host.float :deep(.thread-head:active),
.reader > .chat-host.float :deep(.list-head:active) { cursor: grabbing; }
/* 光标分工（用户口径 2026-12-03 第 3 条：对话页里鼠标停在**不是文字**的地方也是 I 形，
   一拖就选中文字）：
   · 能拖的只有两处：顶部抓手条 `.chat-grab`、会话头 / 列表头 → `grab`（按住 `grabbing`）；
   · 正文/列表**不再强制 `cursor: text`** —— 那条会把整个盒子（包括内边距、消息之间的空隙、
     头像与留白）都算成文字，指到哪儿都是 I 形。改回 `auto`：Chromium 自己只在**真的有字形**
     的地方给 I 形，空隙里是箭头，这才是正常手感（浮窗的 `grab` 也不会漏进来，
     `ReaderChat` 的 `.chat.is-float .msgs/.list-body` 已经把类名收回到 auto）；
   · 按钮仍是 `pointer`（`.th-act` 自己写了，这里只兜底一遍）。 */
.reader > .chat-host.float :deep(button) { cursor: pointer; }

/* 头部上方那 12px（抓手药丸所在的一条）属于 `.chat-host` 自己、不属于头部 ——
   补一条透明抓取带：光标是手形，按下照旧落到 `.chat-host`（拖动由 `onFloatCardDown` 接管）。 */
.reader > .chat-host.float::before {
  content: ''; position: absolute; left: 12px; right: 12px; top: 0; height: 16px;
  cursor: grab;
}
.reader.float-dragging > .chat-host.float::before { cursor: grabbing; }
/* 拖动中**整张卡**都是「抓紧」的手型（用户口径 2026-12-03 第 1 条：按下后还是张开的手）。
   `:active` 靠不住 —— 拖头的 pointerdown 里 preventDefault 了（为了不让 PDF 起选区），
   浏览器因此不进入 :active；下面这些类名由手势自己挂（setFloatDragging），不看 UA 状态。 */
.reader.float-dragging > .chat-host.float,
.reader.float-dragging > .chat-host.float :deep(.thread-head),
.reader.float-dragging > .chat-host.float :deep(.list-head),
.reader.float-dragging > .chat-host.float :deep(.msgs),
.reader.float-dragging > .chat-host.float :deep(.msgs *),
.reader.float-dragging > .chat-host.float :deep(.list-body),
.reader.float-dragging > .chat-host.float :deep(.list-body *) { cursor: grabbing; }
/* 右上角这组按钮：与对话页头部那组**逐字同规格**（`.th-acts` / `.th-act` 的同一套数字）——
   同一个托底托盘 + 同 26px 圆钮 + 悬停浮成 `--bg-elev` 并带一档阴影。 */
.float-act {
  width: 26px; height: 26px; flex: none; display: inline-flex; align-items: center; justify-content: center;
  border: 0; border-radius: 999px; background: var(--bg-sunken); color: var(--text-dim); cursor: pointer;
  transition: background var(--dur) var(--ease), color var(--dur) var(--ease), box-shadow var(--dur) var(--ease);
  /* 浮窗右上角这两枚（回到侧栏 / 关闭）**图标走主题色**（用户口径 2026-12-02）：
     与对话页那组 .th-act 同一口径 —— 底子仍是托底圆片，只染图标。 */
  color: var(--accent-text);
}
.float-act:hover { background: var(--bg-elev); color: var(--accent-hover); box-shadow: var(--shadow-1); }
.float-act:active { transform: scale(.94); }
/* 两个下角都能改尺寸（用户口径 2026-11-19：去掉右下角那个直角图标，左右两个角都能拖）。
   角上**什么都不画**，只有光标形状（右下 nwse / 左下 nesw）与 20px 的命中区 ——
   浮窗的圆角与投影已经把"卡片边界"说清楚了，再画一枚小角标只是噪音。
   `min-width: 0` 必须写：上面那条 `.reader-aside > *` 的兜底最小宽度会把它撑成整栏宽。 */
/* 浮窗态的圆角裁切现在挂在 `.chat-host.float` 自己身上（见下面那个块）：
   侧栏永远是普通侧栏，不再为浮窗做任何排版让步。 */
/* 角上那枚「可以拉大」的指示器：与两张 FloatingCard **逐条同一套**（用户口径 2026-11-22）——
   静止 2.2px、悬停 2.9px、拖动 3.4px（关键帧 2.2 → 3.6 → 3.2 → 3.4）、
   22% 文字色、弧心与卡片圆角同心、外沿离卡片 5px（FLOAT_GRIP_GAP，与 --fc-gap 同一个数字）、
   非焦点（浮窗不是最上层）时透明度降到 0.42、悬停 / 拖动回 1，走 260ms 过渡、
   弧长只取圆角的 37.5%（= 原来的一半）。
   ⚠️ 数字照抄 FloatingCard.vue 的 `.fc-resize`（半径 = 圆角 + gap、外沿 = gap），
   浮窗圆角 16 → 弧半径 21、弧盒 23。改一处必须改另一处。
   **拖动反馈只有描边加粗**：原来那条 push 位移关键帧会把把手本身朝拖动方向 translate 4px，
   手按着的把手与真正跟手的卡片边缘因此错开 4px（「拖了 4px，它才动 0px」），已整条删除。
   这几枚把手**只在浮窗态渲染**（模板上的 v-if chatFloat），负偏移不影响贴栏形态。 */
.reader > .float-grip {
  --fc-radius: 16px;   /* = .reader > .chat-host.float 的 border-radius */
  --fc-handle: color-mix(in srgb, var(--text) 22%, transparent);
  /* **挂在 aside 外面**（用户口径 2026-11-21「搬」）：aside 是 overflow:hidden，
     把手留在里面就只能画在圆角内侧。现在它是 .reader 的子节点 + fixed，
     left/top 由 `floatGripStyle` 按 `floatBox` 算出来（箱子边缘比卡片边缘再出去 3px）。 */
  position: fixed;
  width: 20px; height: 20px; min-width: 0;
  /* 层级**跟着焦点走**（由 `applyFloatDom` 行内写 63 / 61），CSS 这里只是兜底值 61：
     写死 63 的话，浮窗没焦点（60）而笔记卡有焦点（62）时，浮窗的两枚角把手照样压在人家卡片上面
     —— 那就是用户报的「指示器层级不对」（2026-11-24 第 1 条）。
     61 = 压在浮窗自己那张卡（60）之上，又低于有焦点的浮层（62）。 */
  z-index: 61; touch-action: none;
}
/* 手指比鼠标粗得多：把角把手的**热区**放大到 44×44（弧本身一点不动）。
   20×20 在触摸屏上基本点不中 —— 用户报「对话浮层的角把手点不动」（2026-11-24）。
   只放开热区、不改视觉：`::before` 这一层本来空着（弧画在 `::after` 上）。 */
@media (pointer: coarse) {
  .reader > .float-grip::before { content: ''; position: absolute; inset: -12px; }
}
.reader > .float-grip.br { right: 0; cursor: nwse-resize; }
.reader > .float-grip.bl { left: 0; cursor: nesw-resize; }
/* 弧：一条 45° 的 SVG path（**两端圆头**），与 FloatingCard 的 `.fc-grip` 逐条同一套几何。
   `--fc-gap` 必须写死 5px：这两枚把手的坐标是 JS 按 FLOAT_GRIP_GAP 算的，CSS 里没有那个变量。 */
.reader > .float-grip .fg-arc {
  position: absolute;
  --fc-gap: 5px;
  --fc-grip-box: calc((var(--fc-radius) + var(--fc-gap)) * 2.1739);
  width: var(--fc-grip-box);
  height: var(--fc-grip-box);
  right: calc(var(--fc-gap) + var(--fc-radius) - var(--fc-grip-box) / 2);
  bottom: calc(var(--fc-gap) + var(--fc-radius) - var(--fc-grip-box) / 2);
  overflow: visible;
  pointer-events: none;
  opacity: 1;
  transition: opacity 280ms var(--ease),
              transform 520ms cubic-bezier(.33, 0, .2, 1);
}
.reader > .float-grip .fg-arc path {
  fill: none;
  stroke: var(--fc-handle);
  stroke-width: 2.6px;
  stroke-linecap: round;
  transition: stroke 200ms var(--ease),
              stroke-width 460ms cubic-bezier(.33, 0, .2, 1);
}
.reader > .float-grip.bl .fg-arc {
  right: auto;
  left: calc(var(--fc-gap) + var(--fc-radius) - var(--fc-grip-box) / 2);
}
/* 对话浮窗不是焦点时（笔记卡在上面）：两枚角把手**大幅减淡**，与笔记卡同一套口径 */
.reader > .float-grip.unfocused .fg-arc { opacity: .24; }
.reader > .float-grip.unfocused:hover .fg-arc,
.reader > .float-grip.unfocused:active .fg-arc,
.reader > .float-grip.unfocused.is-on .fg-arc { opacity: 1; }
.reader > .float-grip:hover .fg-arc path { stroke: var(--accent); stroke-width: 3.2px; }
/* 拖动中（`:active` 是纯 CSS 的按下态）：**只让弧自己让一下 + 加粗**，命中区一动不动。
   keyframes 必须写在本文件 —— 组件 scoped 的 @keyframes 不会跨组件生效。 */
@keyframes fc-grip-thicken-f {
  0% { stroke-width: 2.6px; }
  50% { stroke-width: 4.2px; }
  78% { stroke-width: 3.7px; }
  100% { stroke-width: 4px; }
}
@keyframes fc-grip-glide-f {
  0% { transform: translate(0, 0); }
  52% { transform: translate(4px, 4px); }
  78% { transform: translate(3.3px, 3.3px); }
  100% { transform: translate(3.6px, 3.6px); }
}
@keyframes fc-grip-glide-f-bl {
  0% { transform: translate(0, 0); }
  52% { transform: translate(-4px, 4px); }
  78% { transform: translate(-3.3px, 3.3px); }
  100% { transform: translate(-3.6px, 3.6px); }
}
.reader > .float-grip:active .fg-arc,
.reader > .float-grip.is-on .fg-arc {
  animation: fc-grip-glide-f 380ms cubic-bezier(.25, 1.1, .35, 1) both;
}
.reader > .float-grip.bl:active .fg-arc,
.reader > .float-grip.bl.is-on .fg-arc {
  animation: fc-grip-glide-f-bl 380ms cubic-bezier(.25, 1.1, .35, 1) both;
}
.reader > .float-grip:active .fg-arc path,
.reader > .float-grip.is-on .fg-arc path {
  stroke: var(--accent);
  stroke-width: 4px;
  animation: fc-grip-thicken-f 340ms cubic-bezier(.25, 1.1, .35, 1) both;
}
@keyframes fc-grip-spring {
  0% { transform: scale(1); }
  28% { transform: scale(1.55); }
  56% { transform: scale(1); }
  78% { transform: scale(1.28); }
  100% { transform: scale(1.16); }
}
/* 减少动效：关掉动画，但颜色 / 粗细的静态反馈照旧（在任何设置下都还在） */
@media (prefers-reduced-motion: reduce) {
  .reader > .float-grip .fg-arc,
  .reader > .float-grip .fg-arc path { transition: none; transform: none; }
  .reader > .float-grip:hover .fg-arc,
  .reader > .float-grip:active .fg-arc,
  .reader > .float-grip.is-on .fg-arc { transform: none; animation: none; }
}

/* Markdown 视图：正文宽一点、插图按容器宽度自适应 */
.md-bar { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; }
/* 顶部留一点空白（与论文正文顶部留白同一种观感）：内容不再顶着上一行的分隔/工具栏。
   注意这里**没有上边框**了 —— 用户口径：移除分割线。 */
.md-doc {
  max-height: 68vh; overflow: auto; padding: 16px 2px 4px;
  font-size: calc(14px * var(--ts, 1)); line-height: 1.75;
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
  font-size: calc(11px * var(--ts, 1)); font-weight: 600; color: var(--text-faint);
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
.md-doc :deep(h2) { font-size: calc(16px * var(--ts, 1)); }
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
/* 关浮窗的那一帧同样不要过渡（否则会补间一次"飞出左边界"，看起来像侧栏收起的动画）——
   保护落在对话容器上（浮窗/停靠切换的就是它；侧栏自己不参与浮窗了） */
.reader > .chat-host.closing { transition: none; }
/* 拖拽把手：挂在侧栏**右边缘外侧**（用户口径 2026-11-19：位置右移）。
   为什么不放在 `<aside>` 里：那是 `overflow: hidden`，凸出去就被裁；放内侧又必然压在
   面板最右边那条滚动条上（上一版为了避让塞了条 9px gutter，用户口径：把滚动条改回原位）。
   现在它是 `.reader` 的绝对定位子元素，`left` 与侧栏宽度同一个变量。 */
.reader > .aside-resizer {
  /* `left` 由行内 style 给（`asideShownPx`：拖动期间跟预览值，否则跟落盘值） */
  position: absolute; top: 0; bottom: 0; width: 12px;
  z-index: 31;                       /* 侧栏是 30：把手永远压在分隔线之上 */
  cursor: col-resize; touch-action: none; background: transparent;
}
/* 拖动指示器（用户口径：像图里那样给一条抓手）：平时是一条淡胶囊，hover / 拖拽中变主题色。
   竖向居中 —— 它只是「这里有把手」的提示，12px 的命中带里只有这一条是看得见的。 */
.reader > .aside-resizer::after {
  content: ''; position: absolute; left: 3px; top: 50%; width: 4px; height: 40px;
  margin-top: -20px; border-radius: 999px;
  background: color-mix(in srgb, var(--text) 18%, transparent);
  transition: background var(--dur) var(--ease), width var(--dur) var(--ease);
}
.reader > .aside-resizer:hover::after {
  background: var(--accent); width: 5px;
}
/* 按住 / 正在拖：弹性**沿竖线两端**弹（用户口径 2026-11-26 追加），不是往左右变粗。
 * 用 scaleY 而不是 height：`transform-origin` 默认就在中心，两端会**对称地**往外弹开；
 * 写 height 得自己补 margin-top，还要顺带触发一次布局。
 * 口径（本轮）：**按下就一直停在最长那一档，只有松手才回缩**。
 * 所以打开动画 100% 停在 1.5 且 `both` 把它保持住 —— 按住多久都张着；
 * 回缩交给松手后 `.settling` 的 `aside-grip-close`（起点同为 1.5，接得上、不会先跳一下）。
 * `.active` 绑的是 `asideResizing`（不是 `:active`）：拖到把手外面时手指/鼠标已不在元素上，
 * 但拖动还在继续，这一档必须仍然亮着。 */
.reader > .aside-resizer:active::after,
.reader > .aside-resizer.active::after {
  background: var(--accent);
  animation: aside-grip-open 260ms cubic-bezier(.22, 1.2, .36, 1) both;
}
@keyframes aside-grip-open {
  0% { transform: scaleY(1); }
  100% { transform: scaleY(1.5); }
}
/* 松手：才回缩，且回缩时轻轻过冲一下（1.5 → 0.92 → 1），与弹开同一条手感。
   这一枚类由 JS 挂 320ms（= 动画时长），不是 :active 的副产品。 */
.reader > .aside-resizer.settling::after {
  background: var(--accent);
  animation: aside-grip-close 300ms cubic-bezier(.22, 1.2, .36, 1) both;
}
@keyframes aside-grip-close {
  0% { transform: scaleY(1.5); }
  38% { transform: scaleY(0.92); }
  100% { transform: scaleY(1); }
}
@media (prefers-reduced-motion: reduce) {
  .reader > .aside-resizer:active::after,
  .reader > .aside-resizer.active::after,
  .reader > .aside-resizer.settling::after { animation: none; }
}
/* 侧栏收起 / 浮窗 / 窄屏（抽屉）时没有可拖的东西，把手一并收掉 */
.reader.no-aside > .aside-resizer { opacity: 0; pointer-events: none; }
.reader.aside-float > .aside-resizer { display: none; }
@media (min-width: 1101px) {
  /* 展开/收起期间侧栏**只被平移、不跟着变窄**：内容保持展开宽度。
     不这么做的话，选项卡会重排、公式（KaTeX）会逐帧重新排版 —— 既看着"变形"，
     又是卡顿的主要来源。裁切由 .reader-aside 的 overflow:hidden 完成。
     宽度与上面的 `width` 同一个变量：拖宽之后内容才真的按新宽度排版。 */
  /* 把手已经挪到侧栏外面（见上面的 `.reader > .aside-resizer`），所以这里**不留 gutter**：
     面板里的滚动条回到最右边原位，`.panel-body` / `.toc-pane` 这些滚动容器照旧整宽。 */
  /* 子元素的兜底最小宽度：拖宽度时跟着**预览值**走（否则面板缩小时内容被这条撑住、右侧被裁），
     其余时候跟着落盘值。用行内变量，不写组件根上的 `v-bind`。 */
  .reader-aside > * { min-width: var(--aside-min-w, v-bind(asideWidthPx)); }
  /* 拖宽度期间：面板内容**跟着实时重排**（用户口径 2026-11-19 第二轮：要看到内容跟着变）。
     这一条与上一版相反 —— 上一版把内容钉在按下那一刻的宽度、只让边框动，为的是省掉逐帧重排；
     现在靠"只动面板 + `contain` 隔离 + 正文那一层不动"来把代价压住，内容就能实时跟手了。
     正文那一层仍然**不跟着动**（见 `asidePreviewShift`），松手才让位/重排一次。 */
  /* 把面板的绘制/布局隔离出来：拖宽度时只有它自己的边框在动，不牵动别处 */
  .reader.aside-previewing .reader-aside { contain: layout paint; }
}
/* 侧栏里所有**图标钮**统一走主题色（用户口径 2026-12-02：目录 / 公式 / 表格 / 笔记那几排
   动作键原来是正文灰，换主题之后一片灰，跟界面对不上）。:deep 是必须的 —— 这些面板
   （ReaderToc / ReaderFormulaList / ReaderTableList / ReaderSidePanel）都是子组件，
   父组件的 scoped 选择器够不到它们内部。
   ⚠️ 排掉 .danger / .primary / .tool-on：删除键要红、实底钮要反白、选中项要保持主色实底，
   被这一条盖掉就等于把语义抹平了。 */
.reader-aside :deep(.btn.icon:not(.danger):not(.primary):not(.tool-on)) { color: var(--accent-text); }
.reader-aside :deep(.btn.icon:not(.danger):not(.primary):not(.tool-on):hover:not(:disabled)) {
  color: var(--accent-hover);
}
/* 分段控件（目录 / 页面视图）：两枚都染主题色，未选中的**降一档**——
   否则两枚一样亮，看不出点了哪一个（选中态本来只靠那枚白色 pill 表达，太弱）。 */
.reader-aside :deep(.seg button) { color: color-mix(in srgb, var(--accent-text) 72%, var(--text-dim)); }
.reader-aside :deep(.seg button.active) { color: var(--accent-text); }

/* 收起：整块滑出左边界。transform 走合成层，比过渡 width 便宜得多，
   也不会像"宽度收到 0"那样在右边缘留一条来不及收掉的 1px 分隔线。 */
.reader.no-aside .reader-aside {
  transform: translateX(-100%); opacity: 0; pointer-events: none;
}
/* ---- 浮层模式的「画中画」占位面板（用户口径 2026-11-22；本轮收窄条件）----
   只在「页签 = 对话 且 浮窗开着」时渲染（模板那条 v-if）：那一刻侧栏内容区正好空着，
   这块地方就是"对话飞走了、关掉浮层就回来"。页签一切走，侧栏照常显示那个页签。
   `top: 58px` 让开**真实页签条**：本轮页签条常驻、可点，浮窗不再带走它 —— 原来那条
   "只读页签副本"（`.aside-pip-head`）因此整条删掉，不必再和真实页签对贴。
   z-index 50：高于侧栏本体（30）与拖宽把手（31），低于浮窗卡片（60 / 有焦点 62）
   与两枚放大把手（63）；它从 58px 起画，够不到工具栏（40）那一行。
   整块 pointer-events: none（此刻侧栏内容不可交互），只放开那枚「回到侧栏」按钮。
   宽度用 v-bind(asideWidthPx)：与侧栏**同一个变量**，正文让位的宽度也由它推出来。 */
.aside-pip {
  position: absolute; top: 58px; bottom: 0; left: 0;
  width: v-bind(asideWidthPx);
  z-index: 50;
  display: flex; flex-direction: column; align-items: center; justify-content: center;
  gap: 6px; padding: 0 16px; text-align: center;
  border-right: 1px solid var(--border);
  /* 淡遮罩，不是纯色块：背后是页面底色，看起来是「这格子还在，只是空着」 */
  background: color-mix(in srgb, var(--bg-elev) 72%, transparent);
  pointer-events: none;
}
.aside-pip-icon { color: var(--text-faint); opacity: .72; display: inline-flex; }
.aside-pip-title { margin: 0; font-size: calc(12.5px * var(--ts, 1)); color: var(--text-dim); }
.aside-pip-note { margin: 0; font-size: calc(11px * var(--ts, 1)); color: var(--text-faint); }
.aside-pip-btn {
  /* 整块禁用了交互，这里单独把按钮的事件放开 */
  pointer-events: auto;
  margin-top: 4px; padding: 3px 10px; border: 0; border-radius: 999px;
  background: transparent; color: var(--text-dim);
  font-size: calc(11.5px * var(--ts, 1)); cursor: pointer;
  transition: background var(--dur) var(--ease), color var(--dur) var(--ease);
}
.aside-pip-btn:hover { background: var(--bg-hover); color: var(--accent-hover); }
/* ================= 对话容器：**一个常驻实例 + 两种定位**（本轮三修）=================
   DOM 从不搬家（不 Teleport / 不 v-if / 不 KeepAlive），只切两样东西：
     · `.float` 类：定位与卡片外框；
     · 行内 `asideFloatStyle`：浮窗态才写 left/top/width/height/z-index。
   上一轮用 `<Teleport>` 在两个宿主之间搬同一个实例，线上出现偶发的
   `Cannot read properties of null (reading 'emitsOptions')`（tsc / build 都拦不住），已整体回滚。 */

/* —— 停靠态（无 `.float`）——
   与 `.reader-aside` 同一块地方（left 0 / 同宽 / z-index 30），**但它从 58px 开始**：
   `.aside-head`（页签条）高 58px，从 0 开始会把页签条整条盖住 —— 而本轮页签条必须可点。
   所以"看起来就是侧栏内容区"= 页签条下面那一块。 */
.reader > .chat-host {
  position: absolute; left: 0; top: 58px; bottom: 0;
  width: v-bind(asideWidthPx);
  z-index: 30;
  display: flex; flex-direction: column;
  min-width: 0; min-height: 0;
  /* 侧栏自带底色，`ReaderChat` 的 `.chat` 根是透明的 —— 盖上去必须自带底板与那道分隔线 */
  background: var(--bg-elev);
  border-right: 1px solid var(--border);
  /* 与 `.reader-aside` 同一条开合过渡：侧栏收起时它跟着滑出去，不另起一套动画 */
  transition: transform .26s cubic-bezier(.32, .72, 0, 1), opacity .18s ease;
}
/* 兜底最小宽度：与 `.reader-aside > *` 同一理由（内容不许把容器顶开） */
.reader > .chat-host > * { min-width: 0; }
/* 侧栏收起：跟着滑出左边界（否则正文上会留一块孤零零的对话） */
.reader.no-aside > .chat-host { transform: translateX(-100%); opacity: 0; pointer-events: none; }

/* —— 浮窗态 ——
   几何全由 `asideFloatStyle` 行内给（position: fixed + left/top/width/height + z-index，
   不写 v-bind：见那条 computed 的说明）。这里只管"卡片"长什么样，并把收起态那份
   transform/opacity/pointer-events 顶回来（浮窗时侧栏是**开着**的，原位置留给 `.aside-pip`）。 */
.reader > .chat-host.float {
  transform: none; opacity: 1; pointer-events: auto;
  border: 1px solid var(--border); border-radius: 16px;
  box-shadow: var(--shadow-flyout);
  overflow: hidden;      /* 圆角裁内容（原来那条给 aside 的 overflow 放行，现在归这里） */
}

/* 浮窗里没有「贴边那条竖把手」可拖（换成了右/左下角手柄），规则见 `.reader > .aside-resizer` */
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
/* 悬浮走主题色（用户口径 2026-12-03：鼠标停在侧栏页签上时文字要变主题色，不是正文黑）。
   选中态是 --accent-text，悬浮用深一档的 --accent-hover —— 两者都离得开正文色，
   又不会让人误以为已经选中了这一页。 */
.side-tab:hover { background: var(--bg-hover); color: var(--accent-hover); }
/* 汉字**只在当前页显示**（用户口径：其余页签移除汉字、只留图标，整条胶囊因此紧凑一截）；
   当前页那枚**横向放宽 + 文字缩小一号** —— 图标在上、文字在下时，窄胶囊会把内容挤成一小团，
   加宽并收小字号才像个"选中态"。 */
.side-tab.active {
  padding: 0 14px;
  background: var(--bg-elev); color: var(--accent-text); box-shadow: var(--shadow-1);
}
.side-label { display: none; font-size: calc(10.5px * var(--ts, 1)); line-height: 1; }
.side-tab.active .side-label { display: block; font-weight: 600; }
/* 角标挪到**右上角**（绝对定位）：页签是「图标 +（只在当前页才有的）文字」，
   横向没有空位可以内联挂一个数字了。 */
.side-count {
  position: absolute; top: 0; right: 3px;
  font-size: calc(9px * var(--ts, 1)); line-height: 1;
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
  background: var(--bg); scrollbar-gutter: stable;
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

/* 「就是这**一条公式**」：点解读里的公式标号时精确标到这里（同一正文行里可能有好几条公式，
   行级落点分不开 —— 用户 2026-12-03 的「56、57 为什么是同一块地方」）。
   语言与行闪一致：淡底 + 左侧竖条，1.6s 淡出；`:deep` 是因为 KaTeX 在子组件里渲染。 */
.reader-content :deep(.flash-target) {
  background-color: color-mix(in srgb, var(--accent) 16%, transparent);
  box-shadow: inset 3px 0 0 0 color-mix(in srgb, var(--accent) 62%, transparent);
  border-radius: 6px;
  animation: katex-flash 1.6s ease-out forwards;
}
@keyframes katex-flash {
  0%, 40% { background-color: color-mix(in srgb, var(--accent) 16%, transparent); }
  100% { background-color: transparent; }
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
  /* 对话容器跟着当抽屉：窄屏**没有浮窗**（见 `watch(isNarrow)`），停靠态就是抽屉里的内容区。
     位置比侧栏再矮一条页签条（`.aside-head` 58px），抽屉开着时才可见。 */
  .reader > .chat-host {
    position: fixed; top: calc(var(--header-h) + 58px); bottom: 0; left: 0;
    width: min(360px, 88vw); z-index: 60;
    transform: translateX(-102%); visibility: hidden;
    transition: transform .24s cubic-bezier(.4, 0, .2, 1), visibility .24s;
  }
  .reader.mobile-aside > .chat-host {
    transform: none; visibility: visible; opacity: 1; pointer-events: auto;
  }
  /* 抽屉宽度是定死的，拖拽没有意义（拖了也不会生效），干脆不显示把手 */
  .reader > .aside-resizer { display: none; }

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
  .reader-aside, .reader > .chat-host { width: 100%; }
}

@media (prefers-reduced-motion: reduce) {
  .reader, .reader-aside, .reader > .chat-host { transition: none; }
  /* 抓手：不补间宽度/颜色，但**终态照旧**（拖动中仍是 46 宽 / 34%），只是瞬间到位 */
  .reader > .chat-host.float .chat-grip-pill { transition: none; }
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
/* 笔记框标题行上的 ✓ 保存 / 头部那枚删除键（用户口径 2026-10-10：复刻目标站的笔记框）。
   ⚠️ 这两个键都是**本组件**渲染的（保存走 `head-extra`，删除走 `head-tail`，后者 2026-11-20 新增），
   而 `FloatingCard` 的 `.fc-btn` 是它自己的 scoped 样式，管不到这里，所以在这里整形。 */
/* 头部图标钮：**26px 圆钮**，与对话浮层顶部那几枚（ReaderChat `.th-act`）同尺寸、同底色、同按下反馈
   （用户口径 2026-11-28 二十二轮）。语义只体现在**图标颜色**上：保存=主题色、删除=danger。 */
/* 头部圆钮**与 FloatingCard 的 `.fc-btn` 逐条一致**（用户口径 2026-12-04：按钮样式不一致）：
   25px、文字色 6% 的淡底、图标主题色、悬停只换底色、按下缩一格。
   以前这里是 26px + `--bg-sunken`（实色托底）—— 与同一行里的 ⋯ / ✕ 差 1px 且底色更重，
   并排就能看出「有两套按钮」。 */
.np-head-save {
  flex: none; width: 25px; height: 25px; display: inline-flex; align-items: center; justify-content: center;
  border: 0; border-radius: 50%;
  background: color-mix(in srgb, var(--text) 6%, transparent); color: var(--accent-text);
  cursor: pointer; transition: background var(--dur-fast) var(--ease), transform var(--dur-fast) var(--ease);
}
.np-head-save:hover:not(:disabled) { background: var(--bg-hover); }
.np-head-save:active:not(:disabled) { transform: scale(.94); }
.np-head-save:disabled { opacity: .45; cursor: default; }
/* 删除键（头部那枚垃圾桶）：底色同样中性，只把图标染成 danger；hover 才给 danger 底 */
.np-head-save.danger { color: var(--danger); }
.np-head-save.danger:hover:not(:disabled) { background: var(--danger-soft); }
/* 「定位到原文」：中性色 —— 它只是"跳过去看一眼"，不该与主题色的 ✓ 保存抢注意力 */
/* 编辑态最左那枚返回箭头：几何照 `.np-head-save`（25px、中性淡底、按下缩一格） */
.np-back {
  flex: none; width: 25px; height: 25px; display: inline-flex; align-items: center;
  justify-content: center; border: 0; border-radius: 50%; padding: 0;
  /* 头部左边距是 12（留给查看态那枚图标），箭头自己往左咬回 6px，
     与对话浮层里那枚返回键的 6px 位置对齐 */
  margin-left: -6px;
  background: color-mix(in srgb, var(--text) 6%, transparent); color: var(--text-dim);
  cursor: pointer; transition: background var(--dur-fast) var(--ease),
    color var(--dur-fast) var(--ease), transform var(--dur-fast) var(--ease);
}
/* 箭头右侧那行「编辑」：与标题同一档字号 / 字重，左补 6px —— 箭头到它的距离 = head 的 gap 6 + 6 = 12，
   与箭头到卡片左沿（padding-left 12）一模一样。 */
.np-back-label {
  /* 与对话浮层标题（`.th-title`）一字不差：13px / 600 / 正文色；不再额外补左边距 ——
     间距就是 head 的 gap（3px），与对话浮层里"箭头 ↕ 标题"完全同一套。 */
  font-size: calc(13px * var(--ts, 1)); font-weight: 600;
  color: var(--text); white-space: nowrap;
}
.np-back:hover { background: var(--bg-hover); color: var(--text); }
.np-back:active { transform: scale(.94); }
.np-back:focus-visible { outline: none; box-shadow: var(--ring); }

/* 编辑态「返回」且有未保存改动：**卡片内部**的一小块确认（不铺满整页，也不弹全站 Modal）。
   遮罩用浮层底色的半透明版 + 一点毛玻璃 —— 卡片自己的圆角已经裁好了，不用再描一层。 */
.np-confirm {
  position: absolute; inset: 0; z-index: 3;
  display: flex; align-items: center; justify-content: center; padding: 12px;
  background: color-mix(in srgb, var(--bg-elev) 74%, transparent);
  backdrop-filter: blur(2px);
  animation: np-confirm-in 140ms var(--ease);
}
/* 弹窗本体：**不描边**（用户口径 2026-11-28 十六轮）—— 浮层里再套一圈描边会显得像第二个控件；
   靠阴影与底色分层，尺寸也放大一档（内边距 16/18、最小宽 232、正文 13.5px、按钮用默认档）。 */
.np-confirm-box {
  display: flex; flex-direction: column; gap: 14px;
  padding: 16px 18px; min-width: 232px; max-width: 100%;
  background: var(--bg-elev);
  border: 0;
  border-radius: 14px;
  box-shadow: var(--shadow-2), 0 0 0 1px color-mix(in srgb, var(--text) 6%, transparent);
  animation: np-confirm-pop 170ms cubic-bezier(.2, 1.15, .35, 1);
}
.np-confirm-text {
  margin: 0; font-size: calc(13.5px * var(--ts, 1)); font-weight: 600; color: var(--text);
}
.np-confirm-acts { display: flex; align-items: center; gap: 8px; justify-content: flex-end; }
@keyframes np-confirm-in { from { opacity: 0; } to { opacity: 1; } }
@keyframes np-confirm-pop {
  from { opacity: 0; transform: translateY(4px) scale(.96); }
  to { opacity: 1; transform: none; }
}
@media (prefers-reduced-motion: reduce) {
  .np-confirm, .np-confirm-box { animation: none; }
}

/* 头部这几枚（编辑 / 定位 / 保存）图标统一走**主题色**（用户口径 2026-12-02：浮层的按钮也要跟主题）。
   删除那枚不在此列 —— 它有自己的 .danger（红），语义比"好看"重要。 */
.np-head-save:not(.danger) { color: var(--accent-text); }
.np-head-save.edit:hover:not(:disabled) { color: var(--accent-hover); }
.np-head-save.locate:hover:not(:disabled) { color: var(--accent-hover); }

/* 删除的二次确认**不再是一块贴在按钮上的 fixed 菜单**（用户口径 2026-11-28 十五轮）：
   它现在与「返回」那块确认走同一个卡内弹窗（`card.confirmDel` + `.np-confirm`），
   于是不用算坐标、不用 Teleport、也不用再管"点别处 / Esc 收起"。 */
/* ---- 翻页 / 视图切换动效（用户口径 2026-10-10）----
   页面块出现/重建时**只淡入 opacity**。
   ⚠️ 故意**不用 transform**：① 那会给块建一个"包含块"，里面的 `position: fixed` 定位会跟着跑；
   ② 块里有 canvas，`transform` 动画的每一帧都要重新采样位图 —— 那正是前几次"闪/抖"的根源。
   opacity 是合成层属性，不动布局、不重采样。 */
.reader-content :deep(.pg-pair),
.reader-content :deep(.row-pair) { animation: pg-in .18s var(--ease); }
@keyframes pg-in { from { opacity: 0; } to { opacity: 1; } }
/* 头部那行相对时间（`head-extra` 插槽里的第一个节点，排在 ✓ 前面） */
.np-time { flex: none; margin-right: 6px; font-size: calc(11px * var(--ts, 1)); color: var(--text-faint); }
/* 高亮颜色：**一枚圆形按钮**，几何 / 底色 / 按下反馈与头部其它图标钮（`.np-head-save`）
   逐条一致 —— 25px、中性淡底、无描边；语义只由里面那枚色点表达，不再套一圈边框。
   点开的二级菜单单独画在下面的 `.note-color-menu`（Teleport 到 body）。 */
.np-color-btn {
  flex: none; width: 26px; height: 26px; padding: 0; border: 0; border-radius: 50%;
  display: inline-flex; align-items: center; justify-content: center;
  background: var(--bg-sunken); cursor: pointer;
  transition: background var(--dur-fast) var(--ease), transform var(--dur-fast) var(--ease),
              box-shadow var(--dur-fast) var(--ease);
}
.np-color-btn:hover { background: var(--bg-hover); }
.np-color-btn:active { transform: scale(.94); }
/* 打开时给一圈主题色细环（不用整块底色，免得跟色点抢） */
.np-color-btn.on { box-shadow: 0 0 0 1.5px var(--accent); }
.np-color-btn:focus-visible { outline: none; box-shadow: var(--ring); }
/* 色点沿用 `.hl-swatch` 的颜色类（自定义色由行内 style 给）；描边只用极淡一道，
   浅色（黄 / 绿）在白底上才看得出边界 */
.np-color-btn .np-color-dot {
  width: 15px; height: 15px; border: 1px solid color-mix(in srgb, var(--text) 12%, transparent);
  pointer-events: none;
}
/* 换色菜单：贴着按钮弹开，弹出时**从按钮那一角张开**（transform-origin 由 JS 写） */
/* 几何照**查看态那个「⋯」二级菜单**（`.fc-more`）来：4px 内边距、2px 间距、ctl 圆角、
   轻一档的阴影 —— 两块二级菜单摆在一起是一套（用户口径 2026-11-28 八轮）。 */
.note-color-menu {
  position: fixed; z-index: 321;
  width: max-content;
  display: flex; align-items: center; gap: 2px;
  padding: 6px;
  background: var(--bg-elev);
  border: 1px solid var(--border-strong);
  border-radius: var(--radius-ctl);
  box-shadow: var(--shadow-2);
  animation: ncm-pop 190ms cubic-bezier(.2, 1.15, .35, 1);
}
/* 色块与触发钮**同尺寸（25px）**、圆角一致、整行居中 —— 一级 / 二级看起来是一套 */
.note-color-menu .ncm-row { display: flex; align-items: center; justify-content: center; gap: 2px; }
/* 色块 21px（比触发钮小一档，不再"比标题行那几枚还大"），配合 6px 内边距 →
   菜单外高 = 21 + 12 + 2(描边) = 35px，与查看态那个「⋯」菜单逐像素一致（用户口径 2026-11-28 十一轮）。 */
.note-color-menu .hl-swatch { width: 21px; height: 21px; border: 0; }
.note-color-menu .hl-swatch:hover { transform: scale(1.08); }
.note-color-menu .hl-swatch.on { box-shadow: 0 0 0 2px var(--bg-elev), 0 0 0 3px var(--accent); }
@keyframes ncm-pop {
  from { opacity: 0; transform: translateY(6px) scale(.9); }
  to { opacity: 1; transform: none; }
}
@media (prefers-reduced-motion: reduce) {
  .note-color-menu { animation: none; }
}
/* ---- 笔记框照目标站重做（用户口径 2026-10-10）----
   标注卡：`rounded-2xl`(16px) + 1px 描边 + 大阴影 + **不透明底**；
   编辑区：`min-h-24`(96px) / `max-h-64`(256px)、`text-base/relaxed`(16px/1.6)、
   **无边框无圆角**（用户口径："多了中间的圆角矩形框"，那个框要拿掉）、
   水平内收 **16px**（与标题同一个缩进："文字位置不能像那样靠边"）。
   ⚠️ 选择器**带 `[data-note-popover]` 前缀**：权重高于组件里原有的 `.np-edit`，
   不管两条规则谁前谁后都能盖住（不靠文件里的先后顺序）。*/
/* 笔记卡的圆角比解释卡大（16 vs 12）：**只把 --fc-radius 交给外壳**，圆角与底角指示器都读它 ——
   组件里 .float-card 用 var(--fc-radius, 12px)，角上那两道弧用 calc(var(--fc-radius) + 4px)，
   于是两张浮层的指示器是同一条曲线，不再一张淡灰圆环、一张深色弧。
   ⚠️ 这个属性落在 FloatingCard 的**根节点（外壳 .float-shell）**上，不是卡片本体：
   所以这里**不能写 border-radius / background** —— 外壳是方的，给它底色会在卡片四个圆角
   外面画出四块方块（用户口径的「卡片外面多一块灰」）；圆角与不透明底都已归 .float-card 自己。 */
[data-note-popover] { --fc-radius: 16px; }
/* 笔记卡的正文区（色点在标题行里，正文自己内收 12px 左对齐标题） */
[data-note-popover] :deep(.fc-body) { padding: 0; }
[data-note-popover] .np-edit {
  /* 高度交给模板上的 `rows`（跟着已有内容走，见 `noteEditRows`）：
     **不设 `max-height`** —— 否则长笔记会在框里再套一层滚动条（框里一根、卡片一根）。
     内容真的超出卡片时，让外层 `.fc-body` 去滚。 */
  width: 100%; min-height: 96px; resize: none;
  /* 卡片被拉大时（`.float-card.sized`）吃满正文区：否则拉高之后下面留一块点不动的白 */
  flex: 1 1 auto;
  /* 左内边距 12px（不是 16）：`.fc-head` 自己还有 12px 内边距，
     加起来与标题那枚图标同一个缩进（用户口径 2026-10-10："文字位置不能像那样靠边"）。 */
  padding: 4px 16px 4px 12px;
  /* 字号**与查看态同一个值**（用户口径 2026-11-28 十二轮）：查看态那份是 `.np-view`（吃 .small 的 12px，
     再乘壳上的 A− / A+ 缩放），编辑态原来写死 16px —— 一进编辑文字就变大，切回来又变小。
     两处都走 `calc(12px * var(--ts, 1))`，放大缩小才是同一个比例。 */
  /* 字号 / 行高一字不差地对齐对话浮层的回答正文（ReaderChat `.msg.assistant .body`：12.5px、1.72）——
     用户口径 2026-11-28 二十二轮："编辑文字的大小样式与对话浮层一致"。
     查看态那份 `.np-view` 用同一组数字，两态仍然一样。 */
  font-size: calc(12.5px * var(--ts, 1)); line-height: 1.72;
  border: 0; border-radius: 0; background: transparent;
}
/* 头部：间距与右侧留白照对话浮层的 `.thread-head`（gap 3 / padding-right 8）；
   **左边仍留 12px** —— 查看态那枚「笔记」图标要保持此前的留白（用户口径 2026-11-28 二十四轮），
   编辑态的返回箭头由 `.np-back` 自己左移 6px 去贴齐对话浮层里那枚的位置。 */
[data-note-popover] :deep(.fc-head) {
  gap: 3px;
  padding-left: 12px;
  padding-right: 8px;
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
[data-note-popover] .np-view {
  padding: 8px 16px 12px 12px;
  line-height: 1.72;
  /* 查看态正文的字号必须自己声明：它吃的是 base.css 里 .small 的 12px，那条不读 --ts，
     不给这一条的话，壳上的 A− / A+ 只会放大标题、正文纹丝不动。
     12px 就是 .small 的现值 —— 默认（--ts 未定义）渲染与改动前一模一样。 */
  font-size: calc(12.5px * var(--ts, 1));
}
/* 标题与对话浮层 `.th-title` 同一档：13px / 600 / **正文色**（原来是 11.5px 的淡色小字） */
[data-note-popover] :deep(.fc-title) {
  font-size: calc(13px * var(--ts, 1)); font-weight: 600; color: var(--text); flex: none;
}
/* 色条（在标题行里，排在「笔记」**右边**）：不再自占一行，所以没有内边距；
   左边留 2px 与标题错开一点，不要和「笔记」两个字粘死 */
/* 头部间距一律 6px：`head-tail` 那格 FloatingCard 自己写的是 2px，于是编辑态
   "⋯ ↕ 色点 ↕ ✓" 的缝比左边的删除/⋯窄一截（用户口径 2026-11-28 十八轮）。
   色点那枚也**不再额外加 margin**（原来的 2px 就是第二处不一致）。 */
[data-note-popover] :deep(.fc-tail) { gap: 4px; }
[data-note-popover] .np-color-btn { margin: 0; }
/* 自定义色那枚：与胶囊上那枚同一个做法（`SelectionMenu` 的样式是 scoped 的，这里得再来一份）。
   ⚠️ 选择器要**两处都写**：换色菜单 Teleport 到 body，不在 `[data-note-popover]` 里面。 */
[data-note-popover] .hl-swatch.custom,
.note-color-menu .hl-swatch.custom {
  position: relative; overflow: hidden;
  background: conic-gradient(from 90deg, #ff0080, #ff8c00, #ffe600, #00d26a, #00b3ff, #7c3aed, #ff0080);
}
[data-note-popover] .hl-swatch.custom input,
.note-color-menu .hl-swatch.custom input {
  position: absolute; inset: 0; width: 100%; height: 100%; padding: 0; border: 0;
  opacity: 0; cursor: pointer; appearance: none;
}
@media print {
  .reader-toolbar, .reader-aside { display: none !important; }
  .reader, .reader-content { display: block; height: auto; overflow: visible; }
  /* 打印时收回悬浮层的让位与平移，正文按纸张宽度排 */
  .reader-slide { width: auto; transform: none; }
}
</style>

