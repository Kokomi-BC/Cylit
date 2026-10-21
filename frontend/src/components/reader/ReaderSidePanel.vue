<script setup lang="ts">
/**
 * 阅读器侧栏的内容区（**与目录合并成同一栏**，由 ReaderView 的页签切换）。
 *
 * 页签：笔记（批注 / 高亮 / 笔记已合并成这一个）/ 引用文献 / 公式 / 插图 / 术语 / 任务 / **论文解读**
 * （目录由 ReaderToc 负责）。公式**只读**：来自 Markdown 提取，不做复核、不分级、不比对裁图。
 *
 * 解读是一个页签：「论文解读」（`a.markdown`）。内容由 `InsightPanel` → `InsightDoc` 渲染；
 * 整页版（`/understand/:paperId`）也复用同一套渲染与数据源。
 * ⚠️ 原「阅读地图」页签（B 路 + 概念卡）已随该功能一起删除。
 */
import { computed, nextTick, onMounted, ref, watch, type CSSProperties } from 'vue';
import { mdToHtml } from '@/composables/useMarkdown';
import { askConfirm } from '@/stores/confirm';
import { fmtRelative } from '@/utils/format';
import { buildNotesFeed, type NotesFeedRow } from '@/utils/notesFeed';
import { groupPaperTasks } from '@/utils/tasks';
import { highlightCss } from '@/utils/highlightColors';
import { splitNoteRefs } from '@/utils/noteRefs';
import { decorateRefs } from '@/utils/citeRefs';
import type {
  Annotation, FigureItem, GlossaryTerm, Highlight, MarkdownFormula, MarkdownTable, Note, OverlayAnchor,
  PaperReference, TaskInfo, ZoomTarget,
} from '@/api/types';
import AppIcon from '@/components/AppIcon.vue';
import Menu from '@/components/Menu.vue';
import ProgressBar from '@/components/ProgressBar.vue';
import InsightPanel from '@/components/insight/InsightPanel.vue';
import ReferenceList from './ReferenceList.vue';
import ReaderFormulaList from './ReaderFormulaList.vue';
import ReaderTableList from './ReaderTableList.vue';

/** 简报草稿：`reactive` 对象由父组件持有，这里直接双向使用（不复制，避免两份状态）。 */
export interface BriefDraft {
  domain: string;
  style: string;
  notes: string;
  keepAsIsText: string;
  glossaryText: string;
}

/**
 * 侧栏页签（与 ReaderView 的页签栏一一对应）。
 *
 * 用户口径 2026-10-07：**批注 / 高亮 / 笔记合并成一种东西 = 笔记**，所以那三个页签并成 `notes` 一个；
 * 图标也对调过一次（用户口径）：**笔记用 `note`（文稿）、引用文献用 `library`（书架）** ——
 * 都是现成的 svg，跟我自己画的那枚"高亮笔"不同，跟全站同一套描边风格。
 */
export type SideTab = 'notes' | 'references' | 'formulas' | 'tables' | 'figures' | 'terms' | 'tasks' | 'insight';

/** 笔记页签里「保存」要交给父组件的东西（新建还是更新由 `noteId` 决定）。 */
export interface NoteDraftPayload {
  noteId: string;
  highlightId: string;
  anchorId: string | null;
  contentMd: string;
}

const props = withDefaults(defineProps<{
  tab: SideTab;
  /** 批注（划词解释 / 人工批注）：已并进「笔记」页签，同一种卡片显示 */
  annotations: Annotation[];
  notes: Note[];
  /** 高亮：**笔记的骨架**（哪一段文字被标了），页签里和高亮上的笔记合成一条 */
  highlights: Highlight[];
  /** 「引用文献」页签：这篇论文的参考文献（按论文里的顺序） */
  references?: PaperReference[];
  formulas: MarkdownFormula[];
  /** 已被标注的公式：LaTeX 规范化 → 那条高亮（带颜色）与它的笔记（见 `ReaderFormulaList.marked`） */
  markedFormulas?: Map<string, { color: string; highlight: any; note: any | null }>;
  formulasLoading?: boolean;
  /** 「表格」页签：从页级译文抽出来的 Markdown 表（含中文表格说明） */
  tables?: MarkdownTable[];
  tablesLoading?: boolean;
  figures: FigureItem[];
  glossary: GlossaryTerm[];
  briefDraft: BriefDraft;
  /** 「任务」页签：这篇论文的**全部**任务（各类型都算），页签里按类型汇总成清单 */
  tasks: TaskInfo[];
  /** 「论文解读」页签：按论文取解读结果（整页版共用同一份缓存） */
  paperId: string;
  /** 这篇论文有理解任务在跑：解读内容据此决定要不要每 5s 轮询 */
  insightRunning: boolean;
}>(), {
  formulasLoading: false, references: () => [],
  tables: () => [], tablesLoading: false,
});

const emit = defineEmits<{
  /** latex：公式类引用的 LaTeX —— 行级落点分不开「这一段」与「这一段里的那条公式」 */
  (e: 'focus', rowId: string, page?: number, latex?: string): void;
  /** 悬浮标号：把速览请求交给阅读器（那里才有整篇的行 / 图 / 表） */
  (e: 'cite-peek', p: {
    anchor: { x: number; y: number; w: number; h: number };
    page?: number | null;
    rowId?: string | null;
    blockId?: string | null;
    label?: string | null;
    latex?: string | null;
  }): void;
  (e: 'cite-peek-hide'): void;
  (e: 'copy', text: string): void;
  (e: 'zoom', target: ZoomTarget): void;
  /** 表格 / 公式的浮层打开：`{ icon, title, markdown }`，由阅读器那层挂 `BlockOverlay` */
  (e: 'overlay', p: {
    icon: string; title: string; titleMd?: string;
    markdown?: string; imageUrl?: string; anchor?: OverlayAnchor | null;
    /** 浮层上「定位」要用的落点（2026-12-06）：行 id + 页 + 公式 LaTeX */
    rowId?: string; page?: number; latex?: string;
  }): void;
  (e: 'save-brief'): void;
  (e: 'regenerate-brief'): void;
  /** 取消编辑：让父组件用服务端的简报**重新灌一次草稿**（丢弃本地未保存的改动） */
  (e: 'reload-brief'): void;
  (e: 'delete-annotation', id: string): void;
  (e: 'delete-note', id: string): void;
  /** 笔记页签里写/改一条笔记（`noteId` 空 = 给这处高亮新建） */
  (e: 'save-note', p: NoteDraftPayload): void;
  /** 删掉一处高亮（整组一起；挂在它上面的笔记由后端级联删掉） */
  (e: 'delete-highlight', ids: string[]): void;
  (e: 'cancel-task', task: TaskInfo): void;
  /** 解读页签里刚发起过解读任务：让 ReaderView 立刻刷一次任务列表（轮询据此启动） */
  (e: 'insight-queued'): void;
  /** 解读页签右上角那枚图标按钮：让 ReaderView 在**应用内**新开一个标签页 */
  (e: 'open-insight-page'): void;
  /** 笔记卡的「浮层打开 / 编辑」：让父组件弹**同一张**笔记浮层（mode 决定查看还是改） */
  (e: 'open-note', p: { note: Note | null; highlight: Highlight | null; mode?: 'view' | 'edit' }): void;
  /** 多选后的批量删除：一次交给父组件确认，再按类型分头删（笔记 / 整组高亮 / 批注） */
  (e: 'delete-rows', rows: Array<{
    kind: string; noteId: string; anchorIds: string[]; annotationId: string;
  }>): void;
}>();

function locate(id?: string | null, page?: number | null, latex?: string | null) {
  if (!id && !page) return;
  emit('focus', id ?? '', page ?? undefined, latex || undefined);
}

/**
 * 「任务」页签的清单：**一类任务一行**（翻译 / 转录 / 解读 / 索引 / 解析），
 * 用户要看的是"这条流水线现在什么状态"，而不是几十条历史任务（见 `groupPaperTasks`）。
 */
const taskRows = computed(() => groupPaperTasks(props.tasks));

/** 状态徽章的配色（与流水线的"好不好"对应：完成=绿、在跑=主题色、失败=红）。 */
function statusClass(status: string): string {
  if (status === 'completed') return 'ok';
  if (status === 'processing' || status === 'pending') return 'accent';
  if (status === 'paused') return 'warn';
  if (status === 'failed') return 'danger';
  return 'dim';
}

/** 批注 / 笔记正文：**作者自己敲的** → `breaks: true`（回车即换行，且不走 PDF 式软换行接回） */
/**
 * 笔记正文 → HTML：**先把 `[n]` 接成可点锚点**（用户口径 2026-12-06：存为笔记时带上的标号
 * 跳转信息，要让笔记浮层里点得动）。
 *
 * `splitNoteRefs` 把正文末尾那行 `<!-- refs:[…] -->` 拆出来 —— 它本来就被 Markdown 当注释丢弃，
 * 这里是为了拿到"编号 → 落点"的表，好让 `decorateRefs` 只装饰**真实存在**的编号。
 */
function noteHtml(md?: string | null): string {
  const { text, refs } = splitNoteRefs(md);
  const html = mdToHtml(text, null, { breaks: true });
  return decorateRefs(html, refs.map((r) => ({
    ref: r.ref,
    row_id: r.row_id ?? null,
    block_id: r.block_id ?? null,
    page: r.page ?? null,
  })));
}

const annotationHtml = (text: string) => mdToHtml(text, null, { breaks: true });

/**
 * 点笔记里的 `[n]`：带着锚点信息交给阅读器（`focusRow` 支持行 id / 页 / 公式 LaTeX）。
 * 没命中 `[n]` 的点击一律放行（正文里的链接、图片照旧）。
 */
function onNoteRefClick(ev: MouseEvent) {
  const el = (ev.target as HTMLElement | null)?.closest?.('button[data-ref]') as HTMLElement | null;
  if (!el) return;
  ev.preventDefault();
  ev.stopPropagation();
  const row = el.getAttribute('data-row') || '';
  const block = el.getAttribute('data-block') || '';
  const page = Number(el.getAttribute('data-page') || 0) || undefined;
  const anchor = row || block;
  if (anchor) emit('focus', anchor, page);
  else if (page) emit('focus', '', page);
}

/** 面 → 中文（侧栏那枚小徽章）。三种锚点是三套位置模型，界面上要说清挂在哪。 */
const SCOPE_LABEL: Record<string, string> = {
  row: '正文行', markdown: 'Markdown 视图', pdf: '原 PDF',
};

/* ------------------------------ 术语 / 文档简报 ------------------------------ */
/** 只读是默认态：进页签先看到**内容**，点「编辑」才展开输入框（用户口径）。 */
const editing = ref(false);
/** 换页签就退出编辑态，免得切回来还停在半改一半的输入框上。 */
watch(() => props.tab, () => { editing.value = false; exitBatch(); });

/* 简报来源徽章（模型生成 / 人工修订 / 已有简报）已移除（用户口径：这一枚文字胶囊是"模型生成的文字"，
   不需要在界面上标出来）。来源仍由后端 `source` 字段保存，只是不再渲染。 */
/* 简报**不再套卡片**（用户口径：各个内容不要放在一个框里显示）。
   以前用的是 `.item.manual / .item.auto`，那两条会给它画一条 2px 彩色左边框 ——
   上一版想把那条竖线改成中性色，但 `.item.manual`（两个类）权重高于我写的 `.brief-card`（一个类），
   于是竖线一直在。这轮直接把壳子去掉：各段落直接排在面板上，只用留白与小标题分区。 */
/** 视图态判空：只要有任何一段内容就算「有简报」，不要因为 domain 为空就整块空掉。 */
const hasBrief = computed(() => Boolean(
  props.briefDraft.domain || props.briefDraft.style
  || props.briefDraft.notes.trim() || props.briefDraft.keepAsIsText.trim()
  || props.briefDraft.glossaryText.trim() || props.glossary.length,
));
/** 图注当作**浮层标题**时要的是纯文字（浮层标题栏不解析 Markdown）：去掉 `**` 与 `$` 这些记号。 */
function plainCaption(md: string): string {
  return String(md || '').replace(/\*\*|__|`/g, '').replace(/\$/g, '').replace(/\s+/g, ' ').trim();
}

/** 插图的「浮层打开」：贴在那枚按钮下方开一张浮层显示原图（标题走 Markdown，图注里的公式能排出来） */
function openFigureOverlay(fig: FigureItem, ev?: MouseEvent) {
  const capMd = fig.caption_zh || fig.caption || '';
  const r = (ev?.currentTarget as HTMLElement | null)?.getBoundingClientRect();
  emit('overlay', {
    icon: 'image',
    title: plainCaption(capMd) || (fig.number ? `图 ${fig.number}` : '插图'),
    titleMd: capMd,
    imageUrl: fig.card_url || fig.image_url || '',
    anchor: r ? { x: r.left, y: r.top, w: r.width, h: r.height } : null,
    /* 浮层上的「定位」要用：定位到那张图的图注行 */
    rowId: fig.row_id || '', page: fig.page,
  });
}

const noteLines = computed(() => props.briefDraft.notes.split('\n').map(s => s.trim()).filter(Boolean));
const keepLines = computed(() => props.briefDraft.keepAsIsText.split('\n').map(s => s.trim()).filter(Boolean));

/**
 * 术语表的展示行 = **简报里的统一译法**（就是编辑框里那份）+ IR 术语表，按英文去重。
 *
 * 为什么必须合并：只列 IR 术语表时，简报里明明有 20 条统一译法，视图却显示"没有术语"
 * （用户看到的就是这个）；而编辑框里那些才是真正注入每次翻译 prompt 的那一份。
 */
const termRows = computed(() => {
  const rows: Array<{ en: string; zh: string }> = [];
  const seen = new Set<string>();
  for (const line of props.briefDraft.glossaryText.split('\n')) {
    const [k, ...rest] = line.split('=');
    const en = (k ?? '').trim();
    const zh = rest.join('=').trim();
    if (!en || !zh || seen.has(en)) continue;
    seen.add(en);
    rows.push({ en, zh });
  }
  for (const t of props.glossary) {
    const en = String(t.source ?? t.en ?? '').trim();
    const zh = String(t.target ?? t.zh ?? '').trim();
    if (!en || seen.has(en)) continue;
    seen.add(en);
    rows.push({ en, zh });
  }
  return rows;
});

function startEdit() { editing.value = true; }
function cancelEdit() {
  editing.value = false;
  emit('reload-brief');          // 丢弃本地改动，回到服务端那一份
}
/** 保存：先退出编辑态（视图立刻回到排版好的内容），失败时父组件会把草稿退回服务端那版。 */
function saveNow() {
  editing.value = false;
  emit('save-brief');
}

/* ------------------------------ 笔记页签 ------------------------------ */
/* 卡片正文照旧**恒定两行、超出省略**（用户口径 2026-11-21 删掉「缩略显示」开关之后就是这一档，
   本轮不动它 —— 用户口径：只修"引原文那行只剩一个省略号"这一处）。 */
/* 「引原文」只剩标点（`...` / `…`）时**整行不画**：那是占位符，不是内容。 */
const DOT_ONLY = /^[\s.。…·•・,，;；:：\-—_*~?？!！'"“”‘’]+$/;

/**
 * 引原文**本身就是一块公式**时，不能走"单行省略"那条。
 *
 * 为什么：在 PDF / Markdown 里选中一整块公式时，存下来的 `selected_text` 是**LaTeX 源码**，
 * 实测库里长这样：`$$(\text{inner-prod error}) \quad D_{\text{prod}} := … \tag{2}$$`。
 * 这种字符串交给 `clamp1`（-webkit-line-clamp: 1）会被裁成一枚「…」——
 * 用户截图里第一张卡（p3）引原文那行只剩一个引号包着的省略号，就是这么来的。
 * 现在与占位符原文（`quoteMd`）走**同一条路**：按块渲染、由 `.quote-md` 限高
 * （够看清是哪个式子，又不把笔记正文挤出卡片）。
 */
const MATH_ONLY = /^\s*\$\$[\s\S]*\$\$\s*$|^\s*\$(?!\$)[^$\n]*\$\s*$/;
const quoteIsBlock = (r: FeedRow) => !!r.quoteMd || MATH_ONLY.test(r.quote || '');
const quoteBlockHtml = (r: FeedRow) => mdToHtml(r.quoteMd || r.quote);

/**
 * 把卡片里的公式**缩放到能看全**。
 *
 * 为什么（用户口径 2026-12-06「有些就是散块，根本不是公式」）：卡片只有侧栏那么窄，而一条
 * 行间公式常常比它宽两三倍 —— `.quote-md` 靠 `overflow-x: auto` 兜底，于是一屏里只露出
 * 公式的**左半截**，看着就像几个散落的符号。存储一直是完整的 LaTeX，问题只在"放不下"。
 *
 * 做法：量出公式的自然宽度与卡片宽度，按比例 `zoom` 下去（`zoom` 会**改变占位**，用它而不是
 * `transform: scale` —— 后者不缩布局盒，会把卡片撑出横向滚动条）。最小到 0.5 倍：再小就\n"
 * 读不清了，那时宁可保留横向滚动。
 */
const QUOTE_MIN_ZOOM = 0.5;
function fitFormulaQuotes() {
  const boxes = document.querySelectorAll<HTMLElement>('[data-quote-md]');
  boxes.forEach((box) => {
    const inner = box.querySelector<HTMLElement>('.katex-display') || box;
    box.style.zoom = '';                 // 先复位，否则量到的是上一次缩过的宽度
    const natural = inner.scrollWidth;
    const room = box.clientWidth;
    if (!natural || !room || natural <= room) return;
    const ratio = Math.max(QUOTE_MIN_ZOOM, room / natural);
    box.style.zoom = String(Math.round(ratio * 100) / 100);
    // 限高按**缩放后**的真实高度写死：`max-height: 4.6em` 是相对本盒字号算的，
    // zoom 一改它也跟着缩，本来一行公式会被裁掉下半截（看着又像散块）。\n"
    box.style.maxHeight = `${Math.ceil((inner.scrollHeight || natural) * ratio)}px`;
  });
}
onMounted(() => { void nextTick(fitFormulaQuotes); });
const hasQuote = (r: FeedRow) => !!r.quote && !DOT_ONLY.test(r.quote);

/** 正在编辑的那条（`key`）+ 草稿。保存成功后靠 `watch` 收起来（见下面）。 */
const editKey = ref('');
const draft = ref('');
function startEditRow(r: FeedRow) {
  editKey.value = r.key;
  draft.value = r.contentMd;
  // 编辑器是 `v-if` 出来的：等它真的挂上再量高度（同时让已有的长笔记先铺开）
  void nextTick(() => autoGrowNoteEdit());
}
function cancelEditRow() {
  editKey.value = '';
  draft.value = '';
}

/**
 * 笔记编辑框：**跟着内容自己长高**（用户口径 2026-10-10："自适应展开大小 / 内容多时自动展开到底部"）。
 *
 * 以前是固定 `rows=4` ＋ 框内滚动 —— 笔记一长，人就只能在那条窄缝里上下翻。
 * 做法与聊天输入框同一套（见 `ReaderChat` 的自动增高）：**先把高度归零**、再按 `scrollHeight` 顶上去；
 * 到"侧栏能看见的高度"（60vh）就不再长，多出来的交给框内滚动 ——
 * 不然一条长笔记会把整个侧栏撑得老长。
 *
 * ⚠️ 用 **事件对象 / 选择器**取元素，不用模板 `ref`：`input` 事件本身就带着目标，
 *   不依赖 ref 名绑定、也不用猜 DOM 换没换好（组件里同时只会有一个编辑器）。
 */
function autoGrowNoteEdit(ev?: Event) {
  const el = (ev?.target as HTMLTextAreaElement | undefined)
    ?? document.querySelector<HTMLTextAreaElement>('textarea[data-pane-note-edit]');
  if (!el || !el.style) return;
  try {
    /* 上限 = **侧栏可视区底部**（用户口径 2026-10-10："应该可以占用整个元素面积方便编辑"）——
       内容多时就把编辑框一直铺到面板底边，只在"底部那行按钮 + 一点边距"处收住；
       内容少时仍然贴合自身高度（不会拉出一个大片空白）。 */
    const pane = el.closest('.panel-body') as HTMLElement | null;
    const paneBottom = pane?.getBoundingClientRect().bottom;
    const reserve = 58;   // 保存 / 取消那一行 + 边距
    /* 两条上限取小：① 面板底边（侧栏本身可滚时它才是"看得见的底"）；
       ② 视口底边 —— 面板不滚时它的 `bottom` 会在视口之外，只认它就会拉过头。 */
    const vhMax = window.innerHeight - el.getBoundingClientRect().top - 70;
    const paneMax = typeof paneBottom === 'number' ? paneBottom - el.getBoundingClientRect().top - reserve : Infinity;
    const max = Math.max(160, Math.round(Math.min(paneMax, vhMax)));
    el.style.height = '0px';   // 先归零：否则 `scrollHeight` 只会单调变大（删字不会缩回去）
    const h = Math.min(el.scrollHeight, max);
    el.style.height = `${h}px`;
    el.style.overflowY = el.scrollHeight > max ? 'auto' : 'hidden';
  } catch { /* 量不到就算了，退化成默认高度 */ }
}
function saveRow(r: FeedRow) {
  emit('save-note', {
    noteId: r.noteId, highlightId: r.highlightId, anchorId: r.anchorId, contentMd: draft.value,
  });
}
/**
 * 保存后**自动收起编辑器**：父组件写完会刷新列表，这时这条的正文就等于草稿了。
 * 反过来，保存失败（网络/校验）时正文没变 → 编辑器**保持打开**，用户敲的字不会丢。
 */
watch(() => [props.notes, props.annotations, props.highlights], () => {
  if (!editKey.value) return;
  const row = merged.value.find((r) => r.key === editKey.value);
  if (!row || row.contentMd === draft.value) cancelEditRow();
});

/** 「笔记」页签的一个条目：归并规则与笔记整页**共用一份**（`utils/notesFeed`）。 */
type FeedRow = NotesFeedRow<Highlight, Note, Annotation>;

/* ------------------------------ 排序（左上角那枚「N 条」） ------------------------------
 * 用户口径 2026-11-21：左上角的条数改成**按钮**，点开选「按修改时间 / 按文章顺序」。
 * 默认按修改时间 —— 那正是 `buildNotesFeed` 原来就排好的顺序（`at` 倒序），所以默认态与旧版完全一致。
 * 选择记在本地：翻页/切页签/重开面板都还是同一个口径。
 */
type NotesOrder = 'time' | 'doc';
const ORDER_KEY = 'cyl.notes.order';
const order = ref<NotesOrder>(localStorage.getItem(ORDER_KEY) === 'doc' ? 'doc' : 'time');
/** 点一下切换：时间 ↔ 文章顺序。图标由模板按 `order` 换（钟 / 三行），不弹菜单。 */
function toggleOrder() {
  order.value = order.value === 'time' ? 'doc' : 'time';
  localStorage.setItem(ORDER_KEY, order.value);
}
/** 文章顺序：先按页，再按锚点 id 里的自然序号（`p004-02` 这种靠 `numeric` 比较）；没有页的排最后。 */
function docOrder(x: FeedRow, y: FeedRow): number {
  const px = x.page ?? Number.MAX_SAFE_INTEGER;
  const py = y.page ?? Number.MAX_SAFE_INTEGER;
  if (px !== py) return px - py;
  return String(x.anchorId ?? '').localeCompare(String(y.anchorId ?? ''), undefined, { numeric: true });
}
const merged = computed<FeedRow[]>(() => {
  const rows = buildNotesFeed({
    highlights: props.highlights, notes: props.notes, annotations: props.annotations,
  });
  return order.value === 'doc' ? [...rows].sort(docOrder) : rows;
});

/* 列表一变（新建 / 删除 / agent 实时写入 / 切排序）就重新量一次公式宽度。
   `flush: post` = 等 DOM 落定再量，否则量到的是上一版的高度。 */
watch(merged, () => { void nextTick(fitFormulaQuotes); }, { flush: 'post' });



/** 删掉一条：批注删批注、有高亮的删整组高亮（它的笔记由后端级联带走）、其余删笔记。 */
function removeRow(r: FeedRow) {
  if (r.kind === 'annotation') { emit('delete-annotation', r.annotation?.id ?? r.key.slice(2)); return; }
  if (r.anchorIds.length) { emit('delete-highlight', r.anchorIds.slice()); return; }
  if (r.noteId) emit('delete-note', r.noteId);
}

/* ------------------------------ 多选（批量管理） ------------------------------ */
/**
 * 用户口径 2026-11-20：笔记页也要「多选」——**交互照对话页那一套**（ReaderChat 的 batch / picked /
 * batch-bar），不另起一套：顶部一枚图标切换 → 每张卡行首出现勾选框 → 顶部一条「已选 N / 取消 / 删除」。
 * 选中的是**归并后的行**（FeedRow.key）；真正怎么删交给父组件（一次确认、按类型分头删）。
 */
const batch = ref(false);
/** 选中的行键（FeedRow.key）—— 用 key 而不是行对象：列表刷新后仍然认得同一条 */
const picked = ref<string[]>([]);

function toggleBatch() {
  batch.value = !batch.value;
  if (!batch.value) picked.value = [];
}
function exitBatch() {
  batch.value = false;
  picked.value = [];
}
const isPicked = (r: FeedRow) => picked.value.includes(r.key);
function togglePick(r: FeedRow) {
  picked.value = isPicked(r) ? picked.value.filter((k) => k !== r.key) : [...picked.value, r.key];
}
/** 全选 = **当前可见的那些行**都在选中里（与对话页 allPicked 同一口径：列表一变就自动成立 / 失效） */
const allPicked = computed(
  () => merged.value.length > 0 && merged.value.every((r) => isPicked(r)),
);
/** 全选 / 取消全选（用户口径 2026-11-21：笔记页也要这一枚，位置在「已选 N」之前） */
function toggleAll() {
  picked.value = allPicked.value ? [] : merged.value.map((r) => r.key);
}
/** 批量态下点卡片本体 = 打勾 / 取消（按钮、输入框、链接除外）—— 与对话页「点整行即选」一致 */
function onCardClick(ev: MouseEvent, r: FeedRow) {
  if (!batch.value) return;
  if ((ev.target as HTMLElement | null)?.closest?.('button, input, textarea, a, label')) return;
  togglePick(r);
}
/** 批量删除：**先过一道确认**（用户口径 2026-11-21 —— 与对话页「删掉选中的这些对话」同一口径：
 *  点一下就跑掉太轻率，笔记删了收不回来）。取消则**什么都不做**，选中态原样留着。 */
async function deletePicked() {
  if (!picked.value.length) return;
  const rows = merged.value.filter(isPicked);
  const ok = await askConfirm({
    title: '删除选中的笔记？',
    body: '笔记正文连同它挂的高亮一起删掉，不可恢复。',
    target: rows.slice(0, 3).map((r) => r.label || r.key).join('、')
      + (rows.length > 3 ? ` 等 ${rows.length} 条` : ''),
    confirmLabel: '删除',
    danger: true,
  });
  if (!ok) return;
  exitBatch();
  emit('delete-rows', rows.map((r) => ({
    kind: r.kind,
    noteId: r.noteId,
    anchorIds: r.anchorIds.slice(),
    annotationId: r.annotation?.id ?? (r.kind === 'annotation' ? r.key.slice(2) : ''),
  })));
}

/* ------------------------ 「更多」菜单（编辑 / 多选 / 删除） ------------------------ */
/** 批注没有「正文可改」（它没有挂高亮、也没有笔记对象），所以不出现「编辑」那一项 ——
 *  与以前卡片上那枚铅笔的条件一致（`r.kind !== 'annotation'`）。 */
function moreItems(r: FeedRow) {
  return [
    ...(r.kind === 'annotation' ? [] : [{ key: 'edit', label: '编辑', icon: 'edit' }]),
    { key: 'pick', label: '多选', icon: 'list-check' },
    { key: 'del', label: '删除', icon: 'trash', danger: true },
  ];
}
/** 浮层打开 / 编辑：都交给父组件弹**同一张**笔记浮层（mode 决定查看还是改） */
function openPopover(r: FeedRow, mode: 'view' | 'edit' = 'edit') {
  emit('open-note', { note: r.note ?? null, highlight: r.highlight ?? null, mode });
}
function onMore(key: string, r: FeedRow) {
  if (key === 'edit') openPopover(r, 'edit');
  else if (key === 'pick') {
    batch.value = true;
    if (!isPicked(r)) picked.value = [...picked.value, r.key];
  } else if (key === 'del') removeRow(r);
}
</script>

<template>
  <div class="panel-body">
    <!-- 论文解读：顶部只有「标题 + 一枚只有图标的按钮」（打开整页 = 应用内新标签页）。 -->
    <InsightPanel
      v-if="props.tab === 'insight'"
      :paper-id="props.paperId"
      :running="props.insightRunning"
      @started="emit('insight-queued')"
      @open-page="emit('open-insight-page')"
      @cite="(c) => locate(c.block, c.page || undefined, c.latex)"
      @peek="(p) => emit('cite-peek', p)"
      @peek-hide="emit('cite-peek-hide')"
    />

    <!-- 笔记（用户口径 2026-10-07：批注 / 高亮 / 笔记统一成这一个页签：
         一条 = 一处高亮 + 它的正文；批注也按同一种卡片列在这里） -->
    <template v-if="props.tab === 'notes'">
      <!-- 工具条一条到底（滚动时钉在顶上）：平时是「N 条 + 多选」（缩略开关已删，2026-11-21），
           进了批量态就换成「全选 + 已选 N + 取消 + 删除」—— 与对话页同一套动作，只是并进同一行，
           不必再多一条工具条（多一条就多一层滚动后被滚走的东西）。 -->
      <div class="notes-bar">
        <template v-if="!batch">
          <!-- 条数（纯文本）+ 一枚**排序图标按钮**（用户口径 2026-11-21 二轮：不要二级菜单，
               点一下就在两种口径间切换，图标本身跟着变：钟 = 按修改时间、三行 = 按文章顺序）。
               选择记在本地（`cyl.notes.order`），与上一版同一个键。 -->
          <button
            class="multi-btn"
            :title="order === 'time' ? '按修改时间（点击改为按文章顺序）' : '按文章顺序（点击改为按修改时间）'"
            :aria-label="order === 'time' ? '按修改时间，点击切换为按文章顺序' : '按文章顺序，点击切换为按修改时间'"
            :aria-pressed="order === 'doc'"
            data-notes-order
            @click="toggleOrder"
          >
            <AppIcon :name="order === 'time' ? 'sort-time-fill' : 'sort-doc-fill'" :size="15" />
          </button>
          <span class="spacer" />
          <!-- 多选（用户口径 2026-11-20；2026-11-21 换形态）：与对话页**同一枚圆钮、同一个图标键、同一段交互**
               （点开进入批量态 → 卡片行首出现勾选框 → 顶部「全选 / 已选 N / 取消 / 删除」）。
               不再套 `.seg sq` 那层方形胶囊 —— 尺寸与配色逐条照对话页那枚 `.multi-btn`（两份 scoped 样式，数值必须一致）。 -->
          <button
            class="multi-btn"
            :class="{ active: batch }"
            :aria-pressed="batch"
            title="批量管理（多选 + 批量删除）"
            data-notes-batch
            @click="toggleBatch"
          >
            <AppIcon name="list-check" :size="15" />
          </button>
        </template>
        <template v-else>
          <!-- 全选（用户口径 2026-11-21）：与对话页**同一枚胶囊 + 同一个 active 绑定**（allPicked = 当前可见行都选中），
               位置在「已选 N」之前 —— 先说范围、再报数量，最后才是取消 / 删除。 -->
          <div class="seg">
            <button :class="{ active: allPicked }" data-notes-batch-all @click="toggleAll">
              {{ allPicked ? '取消全选' : '全选' }}
            </button>
          </div>
          <span class="tiny faint" data-notes-batch-count>已选 {{ picked.length }}</span>
          <span class="spacer" />
          <button class="btn sm" data-notes-batch-cancel @click="exitBatch">取消</button>
          <button
            class="btn sm danger"
            :disabled="!picked.length"
            data-notes-batch-delete
            @click="deletePicked"
          >
            删除
          </button>
        </template>
      </div>
      <div v-if="!merged.length" class="empty small">
        还没有笔记（在正文里划词 → 高亮 → 写笔记）
      </div>
      <div
        v-for="r in merged"
        :key="r.key"
        class="item note-row"
        :class="[r.kind, { editing: editKey === r.key, pick: batch && isPicked(r), picking: batch }]"
        :style="r.color ? ({ '--hl': highlightCss(r.color) } as CSSProperties) : undefined"
        :data-note-id="r.noteId || undefined"
        :data-item-kind="r.kind"
        @click="onCardClick($event, r)"
      >
        <!-- 顶部一条：页码 + 时间 + 三枚动作（用户口径：时间与按钮**上移**、去掉「笔记」文字胶囊）。
             这一行自带一层半透明遮罩（见 .card-top 的底色），内容从它下面滚过时仍然读得清。 -->
        <div class="card-top" data-note-head>
          <!-- 页码 + 时间合成一枚**胶囊**（用户口径：不要整条遮罩带，材质与旁边那几枚圆钮一致） -->
          <span v-if="r.page" class="card-meta">
            <span class="small faint" data-note-page>p{{ r.page }}</span>
          </span>
          <!-- 相对时间**移出那枚胶囊**（用户口径 2026-12-02）：它是说明文字，跟页码挤在同一个色块里
               既显得那枚胶囊很挤，也让"p3"和"2 天前"读起来像同一类信息。现在它单独排在旁边。 -->
          <span v-if="r.at" class="tiny faint card-time" data-note-time>{{ fmtRelative(r.at) }}</span>
          <span class="spacer" />
          <!-- 批量态：一枚勾选框（与对话页同一个 tick 口径）；非批量态：定位 / 浮层 / 更多 -->
          <button
            v-if="batch"
            class="tick"
            :class="{ on: isPicked(r) }"
            :title="isPicked(r) ? '取消选择' : '选择'"
            data-item-tick
            @click.stop="togglePick(r)"
          >
            <AppIcon :name="isPicked(r) ? 'checkbox' : 'checkbox-off'" :size="15" />
          </button>
          <template v-if="!batch">
            <button class="btn icon sm" title="定位" data-item-locate @click="locate(r.anchorId, r.page)">
              <AppIcon name="locate" :size="13" />
            </button>
            <!-- 批注没有可编辑的正文（没有挂高亮、也没有笔记对象），所以不给「浮层打开」 -->
            <button
              v-if="r.kind !== 'annotation'"
              class="btn icon sm"
              title="打开浮层"
              data-item-popover
              @click="openPopover(r, r.contentMd.trim() ? 'view' : 'edit')"
            >
              <AppIcon name="pip" :size="13" />
            </button>
            <Menu
              :items="moreItems(r)"
              label="更多"
              align="end"
              data-item-more
              @select="onMore($event, r)"
            />
          </template>
        </div>
        <!-- 高亮原文：也是文档里的原文，同样按 Markdown 渲染（含公式）。两种走法：
             ① 纯文字 —— **恒为单行省略**（用户口径 2026-11-20：它只是"按的是哪一段"的索引，
                两行以上的引用会把卡片撑高、把正文挤到看不见）；
             ② **本身就是一块公式** —— 按块渲染、由 .quote-md 限高（见 quoteIsBlock 的说明）。 -->
        <div
          v-if="quoteIsBlock(r)"
          class="small faint md quote-md"
          data-quote-md
          v-html="quoteBlockHtml(r)"
        />
        <div
          v-else-if="hasQuote(r)"
          class="small faint md quote clamp1"
          v-html="mdToHtml(`“${r.quote}”`)"
        />
        <template v-if="editKey === r.key">
          <textarea
            v-model="draft"
            class="note-edit"
            rows="4"
            data-pane-note-edit
            placeholder="写笔记…"
            @input="autoGrowNoteEdit"
            @keyup.ctrl.enter="saveRow(r)"
          />
          <div class="row" style="gap:6px">
            <button class="btn sm primary" data-pane-note-save @click="saveRow(r)">保存</button>
            <button class="btn sm" @click="cancelEditRow">取消</button>
          </div>
        </template>
        <template v-else>
          <!-- 正文：用户口径 2026-11-20 —— **恒定两行，超出省略**（缩略开关 2026-11-21 已删，不再有单行态） -->
          <div v-if="r.contentMd" class="small md clamp2" v-html="noteHtml(r.contentMd)"
               @click="onNoteRefClick" />
        </template>
      </div>
    </template>

    <!-- 引用文献（用户口径 2026-10-07 第三条）：正文里的 `[2, 4, 10]` 点开就是这里，
         并且**只显示被提到的那几条**（过滤条上写清"只看这几条 / 显示全部"）。
         点条目 → 去发表网站（DOI / arXiv / 原文里的链接，兜底学术搜索）。 -->
    <template v-if="props.tab === 'references'">
      <div class="ref-bar">
        <!-- 用户口径 2026-10-08：顶栏写成 `REFERENCES 47 条`（原来的"47 条（按论文里的顺序）"太长）。
             顺序说明挪到 tooltip 里 —— 信息还在，但不再占着顶栏。 -->
        <span class="tiny faint" title="按论文里的顺序">
          <span class="ref-cap">REFERENCES</span>
          {{ (props.references ?? []).length }} 条
        </span>
      </div>
      <div v-if="!(props.references ?? []).length" class="empty small">
        这篇论文没有参考文献（解析时没抽到 References 一节）
      </div>
      <!-- 卡片本体在 `ReferenceList` 里：**与论文详情弹窗、正文标号的浮层共用同一份**（样式也自带） -->
      <ReferenceList v-else :items="props.references ?? []" />
    </template>

    <!-- 公式（从 Markdown 提取，只读） -->
    <template v-if="props.tab === 'formulas'">
      <ReaderFormulaList
        :items="props.formulas"
        :loading="props.formulasLoading"
        :marked="props.markedFormulas"
        @open-note="emit('open-note', $event)"
        @focus="(id: string, page?: number) => emit('focus', id, page)"
        @copy="emit('copy', $event)"
        @overlay="emit('overlay', $event)"
      />
    </template>

    <!-- 表格（从页级译文提取，只读）：页签排在「公式」右边 —— 两者都是"从正文里抽出来的
         独立对象"，一组相邻的页签扫起来才顺（用户口径：图标放在公式栏侧）。 -->
    <template v-if="props.tab === 'tables'">
      <ReaderTableList
        :items="props.tables ?? []"
        :loading="props.tablesLoading"
        @focus="(id: string, page?: number) => emit('focus', id, page)"
        @copy="emit('copy', $event)"
        @overlay="emit('overlay', $event)"
      />
    </template>

    <!-- 插图：**图注（卡片标题）一行 + 图 + 页码/图号一行**（论文自己就是这么排的）。
         「定位到图注」跟着**图注标题**走、落在它的右侧：它是"回到正文里这张图的图注"这个动作，
         属于标题行的一部分，比浮在图片右上角更好找，也不会挡住图。
         图与"点开看大图"都用后端的「图 + 图注」卡片（`?card=1`）—— 与正文里的图同源，
         点开就是它的放大版（范围完全一致，用户口径）。 -->
    <template v-if="props.tab === 'figures'">
      <div v-for="fig in props.figures" :key="fig.block_id" class="item fig-card">
        <div class="fig-head">
          <!-- 图注按 **Markdown** 渲染：图注里大量出现 `$\text{TurboQuant}_{\text{mse}}$` 这类公式，
               纯文本会把 LaTeX 原样显示出来（用户口径：侧栏要有 Markdown 支持） -->
          <!-- 图注优先用**中文译文**（用户口径）；英文原文留在 title 里，鼠标停一下能对照 -->
          <div
            v-if="fig.caption_zh || fig.caption"
            class="fig-caption md"
            :title="fig.caption || ''"
            v-html="mdToHtml(fig.caption_zh || fig.caption || '')"
          />
          <span v-else class="spacer" />
          <!-- 浮层打开（用户口径）：图注标题右侧一枚，与「定位」并排 ——
               点图本身也能开，但按钮是「看得见」的那个入口。 -->
          <!-- 浮层打开（用户口径）：走**新的那个浮层**（FloatingCard 里显示这张图），
               不是图片查看器 —— 查看器是「点图片本身」那一手（要缩放 / 旋转 / 切图才用它）。 -->
          <button
            v-if="fig.card_url || fig.image_url"
            class="btn icon sm fig-overlay"
            title="浮层打开"
            data-figure-overlay
            @click="openFigureOverlay(fig, $event)"
          >
            <AppIcon name="pip" :size="13" />
          </button>
          <button
            class="btn icon sm fig-locate"
            title="定位到图注"
            @click="locate(fig.row_id || fig.block_id, fig.page)"
          >
            <AppIcon name="locate" :size="13" />
          </button>
        </div>
        <div class="fig-body">
          <img
            v-if="fig.image_url"
            :src="fig.image_url"
            class="figure"
            loading="lazy"
            :title="`${fig.caption_zh || fig.caption || '插图'}（点击看大图）`"
            @click="emit('zoom', {
              url: fig.card_url || fig.image_url,
              caption: fig.caption_zh || fig.caption, page: fig.page, number: fig.number,
            })"
          >
        </div>
        <div class="row tiny fig-meta">
          <span class="badge">p{{ fig.page }}</span>
          <span v-if="fig.part_label" class="badge dim">{{ fig.part_label }}</span>
          <span class="spacer" />
          <span v-if="fig.number" class="badge accent">图 {{ fig.number }}</span>
        </div>
      </div>
      <div v-if="!props.figures.length" class="empty small">没有插图</div>
    </template>

    <!-- 术语 / 文档简报：默认**只读**（与页面其它内容同一套排版），点「编辑」才出现输入框。
         为什么：以前一进这个页签就是四个输入框（领域/风格/要点/术语表），看起来像"永远在编辑态"，
         既不像页面内容，也容易误改（用户口径：仅在编辑时才显示编辑框，其他时候为正常样式内容）。 -->
    <template v-if="props.tab === 'terms'">
      <div class="row tiny brief-bar">
        <span class="muted">文档简报</span>
        <span class="spacer" />
        <template v-if="editing">
          <button class="btn sm" @click="cancelEdit"><AppIcon name="close" :size="13" />取消</button>
          <button class="btn sm primary" @click="saveNow">
            <AppIcon name="check" :size="13" />保存
          </button>
        </template>
        <template v-else>
          <button class="btn sm" title="编辑简报与术语" @click="startEdit">
            <AppIcon name="edit" :size="13" />编辑
          </button>
          <button class="btn sm" title="让模型重读全文，重新生成简报与术语" @click="emit('regenerate-brief')">
            <AppIcon name="refresh" :size="13" />重新生成
          </button>
        </template>
      </div>

      <!-- 编辑态：只有这里会出现输入框 -->
      <template v-if="editing">
        <label class="field">领域<input v-model="props.briefDraft.domain" placeholder="向量量化 / 近似最近邻"></label>
        <label class="field" style="margin-top:6px">风格<input v-model="props.briefDraft.style" placeholder="学术、术语统一"></label>
        <label class="field" style="margin-top:6px">
          要点（每行一条）
          <textarea v-model="props.briefDraft.notes" rows="3" />
        </label>
        <label class="field" style="margin-top:6px">
          保持英文不译（每行一个）
          <textarea v-model="props.briefDraft.keepAsIsText" rows="2" />
        </label>
        <label class="field" style="margin-top:6px">
          术语表（每行 <span class="mono">english=中文</span>）
          <textarea v-model="props.briefDraft.glossaryText" rows="8" />
        </label>
      </template>

      <!-- 只读态：和批注/笔记一样是**卡片 + 排版好的内容**（不再是一组表单框） -->
      <template v-else>
        <div class="brief-view">
          <template v-if="hasBrief">
            <div v-if="props.briefDraft.domain" class="brief-sec">
              <div class="brief-k">领域</div>
              <div class="brief-v">{{ props.briefDraft.domain }}</div>
            </div>
            <div v-if="props.briefDraft.style" class="brief-sec">
              <div class="brief-k">风格</div>
              <div class="brief-v">{{ props.briefDraft.style }}</div>
            </div>
            <div v-if="noteLines.length" class="brief-sec">
              <div class="brief-k">要点</div>
              <ul class="brief-list"><li v-for="n in noteLines" :key="n">{{ n }}</li></ul>
            </div>
            <div v-if="keepLines.length" class="brief-sec last">
              <div class="brief-k">保持英文不译</div>
              <div class="brief-chips">
                <span v-for="k in keepLines" :key="k" class="brief-chip">{{ k }}</span>
              </div>
            </div>
          </template>
          <div v-else class="empty small">
            还没有简报 —— 点上方「重新生成」，让模型通读全文后给出领域、要点与统一术语。
          </div>
        </div>
      </template>

      <!-- 术语表：编辑态在文本框里改，只读态逐行看（两份来源合并后去重，见 termRows） -->
      <div class="row tiny brief-terms-head">
        <span class="muted">术语表</span>
        <span v-if="termRows.length" class="badge dim">{{ termRows.length }}</span>
      </div>
      <div v-for="(t, i) in termRows.slice(0, 80)" :key="t.en + i" class="term-row">
        <span class="en truncate" :title="t.en">{{ t.en }}</span>
        <span class="zh truncate" :title="t.zh">{{ t.zh }}</span>
      </div>
      <div v-if="!termRows.length" class="empty small">没有术语</div>
    </template>

    <!-- 任务：**按类型一行**的清单（翻译 / 转录 / 解读 / 索引 / 解析）。
         口径（用户第二轮返工）：状态徽章之外，**能算进度的地方把进度条加上** ——
         有总数的（翻译 22 页 / 索引 203 块 / 解析 900 块）画条，未完成时还给 `done/total`；
         没有总数的（解读 `total=1`）只留徽章，一条永远满格或永远 0 的条只是噪声。
         第三轮返工：**已完成的行不报数字**（只留"已完成"徽章 + 满格条）—— 后端偶尔给的是
         "这一批的完成量"（实测有 `done=1/total=3` 却已 completed 的记录），
         `转录 已完成 1/3 + 满格条` 看起来就像出错了（口径集中在 utils/tasks.ts）。
         一个类型跑过多次也只占一行（历史任务全列出来是一面墙，见 utils/tasks.ts）。
         在跑的行给一个「取消」：这是页签里原来的能力，不能因为改版就丢掉。 -->
    <template v-if="props.tab === 'tasks'">
      <div v-if="!taskRows.length" class="empty small">
        这篇论文还没有任务（解析 / 翻译 / 转录 / 解读 / 索引）。
      </div>
      <div v-for="r in taskRows" :key="r.key" class="item task-row">
        <div class="row tiny">
          <span class="task-name">{{ r.label }}</span>
          <span class="badge" :class="statusClass(r.status)">{{ r.statusLabel }}</span>
          <span class="spacer" />
          <span v-if="r.progressLabel" class="mono faint">{{ r.progressLabel }}</span>
          <button
            v-if="r.running && r.task"
            class="btn icon sm"
            title="取消这个任务"
            @click="emit('cancel-task', r.task)"
          >
            <AppIcon name="stop" :size="13" />
          </button>
        </div>
        <!-- `progressValue === null` 就是"这一类没进度可算"：整条不渲染，别留一个空槽 -->
        <ProgressBar
          v-if="r.progressValue !== null"
          class="task-bar"
          :value="r.progressValue"
        />
        <!-- 失败才显示 message：成功那条是"用时 432s"这类流水账，失败原因才是用户要看的 -->
        <div v-if="r.status === 'failed' && r.message" class="tiny task-msg" :title="r.message">
          {{ r.message }}
        </div>
      </div>
    </template>
  </div>
</template>

<style scoped>
.panel-body {
  overflow-y: auto; overflow-x: hidden; flex: 1; min-height: 0; padding: 8px 8px 24px;
  overflow-wrap: anywhere; word-break: break-word;
}
.item {
  border: 1px solid var(--border); border-left: 2px solid var(--border-strong);
  border-radius: var(--radius-sm); background: var(--bg-layer);
  padding: 8px 10px; margin-bottom: 8px;
}
.item.manual { border-left-color: var(--accent); }
.item.auto, .item.selection { border-left-color: var(--ok); }
.item.note { border-left-color: var(--warn); }
/* 笔记页签：顶部一条**细工具条**（条数 + 多选圆钮），滚动时钉在顶上。
   负 margin 正好吸收 `.panel-body` 的 padding（左右 8px），工具条才能通到边。
   左边框用高亮笔的颜色语义 —— 还没写正文的（`highlight`）压暗一档，一眼分得出"哪些还没写"。 */
.notes-bar {
  position: sticky; top: -8px; z-index: 2;
  display: flex; align-items: center; gap: 6px;
  margin: -8px -8px 8px; padding: 4px 8px;
  background: var(--bg-elev); border-bottom: 1px solid var(--divider);
}
/* 多选圆钮（用户口径 2026-11-21）：**30×30 正圆**，不再套 `.seg sq` 那层方形胶囊 ——
   底色 / 悬停 / active 三档都跟 `.seg` 同一个口径：`--bg-sunken` 托底 → 悬停换 `--bg-hover` 并抬一档阴影
   → active 转主题色（`--accent-soft` 底 + `--accent-text` 图标）。
   ⚠️ 对话页那枚是**同一套数值**（ReaderChat 的 `.multi-btn`），改一边必须同时改另一边。 */
.multi-btn {
  width: 30px; height: 30px; flex: none;
  display: inline-flex; align-items: center; justify-content: center;
  border: 0; border-radius: 50%; background: var(--bg-sunken); color: var(--text-dim);
  cursor: pointer;
  transition: background var(--dur) var(--ease), color var(--dur) var(--ease),
    box-shadow var(--dur) var(--ease), transform var(--dur) var(--ease);
}
/* 悬浮走主题色（用户口径 2026-12-03），与侧栏那批图标钮同一口径 */
.multi-btn:hover { background: var(--bg-hover); color: var(--accent-hover); box-shadow: var(--shadow-1); }
.multi-btn:active { transform: scale(.94); }
.multi-btn.active { background: var(--accent-soft); color: var(--accent-text); box-shadow: none; }
/* 条数文字已移除（用户口径 2026-11-21：左上角只留那枚排序图标钮）。 */
/* 左竖线默认是主题色；**有高亮色的行**由行内那个 `--hl` 变量覆盖成这处高亮自己的颜色
   （用户口径 2026-11-28 二十一轮：笔记栏左侧竖线用高亮的颜色）。
   批注行没有高亮色，仍走下面 `.annotation` 那条。

   本轮（用户口径）：**卡片底色也用这根颜色，但比竖线略浅** ——
   竖线画的是原色（.9~.95 不透明），底色只取它的 12% 混进 `--bg-layer`，
   浅色主题是一层薄薄的荧光笔印、深色主题是同一根色的暗底，两边都自动跟。
   `--note-hl` 只是把"这张卡该用哪根色"收敛成一个变量，竖线和底色各取自己那一档浓度。
   ⚠️ 实测过一次改不动：这里不能直接写行内 `background` —— 会盖掉下面 `.pick` 的选中态。 */
.item.note-row {
  --note-hl: var(--hl, var(--accent));
  border-left-color: var(--note-hl);
  background: color-mix(in srgb, var(--note-hl) 12%, var(--bg-layer));
}
/* 卡片顶部那行页码：与正文同一套字号/行高，只是更淡一点，不抢正文 */
.item .card-top { margin: 0 0 4px; font-variant-numeric: tabular-nums; }
/* 笔记卡的顶部条（页码 + 时间 + 三枚动作）。
   用户口径（本轮）：**撤掉那条铺满整行的遮罩带**，只让页码与时间这枚**胶囊**带材质 ——
   材质与旁边那几枚圆钮完全一致（`--bg-elev` + `--shadow-1`、不描边），
   于是这一行读起来是「内容 + 几枚浮起的小片」，而不是「卡片被切成两段」。 */
.item.note-row .card-top {
  display: flex; align-items: center; gap: 6px; flex-wrap: nowrap;
  margin: 0 0 6px; padding: 0;
  background: none; border: 0; border-radius: 0;
  font-variant-numeric: tabular-nums;
}
.item.note-row .card-top .card-meta {
  display: inline-flex; align-items: center; gap: 6px; flex: none;
  height: var(--control-h-sm); padding: 0 10px;
  border-radius: 999px;
  background: var(--bg-elev); box-shadow: var(--shadow-1); color: var(--text-faint);
  font-variant-numeric: tabular-nums; white-space: nowrap;
}
/* 这一行里三枚动作 + 页码 + 时间要挤在一行：按钮收小一档，别把行撑宽 */
.item.note-row .card-top .btn.icon.sm {
  width: 24px; height: 24px;
  /* 动作键统一走**主题色**（用户口径 2026-12-02：这些按钮原来是正文灰，跟系统主题不搭）。
     只染图标：底子仍是那层 --bg-elev 圆片，悬停由 .btn 自己的规则抬高。 */
  color: var(--accent-text);
}
/* 移出胶囊之后的时间：贴着胶囊右侧、比页码更淡一档 —— 它是注释，不是位置。
   ⚠️ 不换行不收缩：那条 card-top 是 nowrap，它被挤窄就会把标题行推歪。 */
.item.note-row .card-top .card-time { flex: none; color: var(--text-faint); }
.item.note-row .card-top .spacer { flex: 1; }
/* 还没写正文的（只有高亮）：竖线压暗一档，底色跟着退到 6%，一眼分得出"哪些还没写" */
.item.note-row.highlight {
  --note-hl: color-mix(in srgb, var(--hl, var(--accent)) 35%, var(--border-strong));
  background: color-mix(in srgb, var(--hl, var(--accent)) 6%, var(--bg-layer));
}
/* 批注行没有高亮色：竖线与底色都走「通过/已处理」那根绿 */
.item.note-row.annotation { --note-hl: var(--ok); }
.item.note-row .row { gap: 6px; }
/* 原文引用那一行恒为单行省略（模板里的 `.quote.clamp1`） */
.clamp1 {
  display: -webkit-box; -webkit-line-clamp: 1; -webkit-box-orient: vertical;
  overflow: hidden; line-clamp: 1;
}
/* 正文两行（用户口径 2026-11-20）：超出就省略，卡片不会因为一条长笔记被拉成一屏。
   用户口径 2026-11-21：「缩略显示」开关删掉后**只剩这一档**，正文不再收成一行。 */
.clamp2 {
  display: -webkit-box; -webkit-line-clamp: 2; line-clamp: 2; -webkit-box-orient: vertical;
  overflow: hidden;
}
/* 引用原文再小一档（原来是 .small 的 12px；它只是「按的是哪一段」的索引） */
.item.note-row .quote { font-size: calc(11.5px * var(--ts, 1)); line-height: 1.55; }
/* 公式当原文时（原文是占位符的那种行）：块级、约两行半高、超出裁掉。
   比 clamp1 宽松（一个式子放得下行内公式 + 上下标），又比全展开克制（不把笔记正文挤出去）。 */
.quote-md {
  margin: 0 0 6px;
  /* 竖向仍按 4.6em 裁（够看清是哪个式子，又不把笔记正文挤出去）；
     横向**一律按原字号**，放不下就滚（用户口径：超过显示范围就横向滚动，别缩字也别裁切）。 */
  max-height: 4.6em;
  overflow-x: auto;
  overflow-y: hidden;
  padding-bottom: 2px;
  /* 滚动条由下面那组 ::-webkit-scrollbar 自己画（细条、无两端箭头）；
     ⚠️ 不能同时写 scrollbar-width: thin —— Chromium 一旦认了这个标准属性就不再理 webkit 那组样式。 */
}
/* 细条自己画（Chromium）：默认那条带两端箭头、还占 10px 高，压在窄卡片里很难看。 */
.quote-md::-webkit-scrollbar { height: 6px; }
.quote-md::-webkit-scrollbar-track { background: transparent; }
.quote-md::-webkit-scrollbar-thumb {
  background: color-mix(in srgb, var(--text) 24%, transparent);
  border-radius: 999px;
}
.quote-md::-webkit-scrollbar-thumb:hover { background: color-mix(in srgb, var(--text) 38%, transparent); }
/* 关掉 base.css 里那条 `.md .katex-display { overflow-x: auto }`：滚动只留**一层**（本层），
   两层各滚一次的话外面那层不动、滚动条藏在公式里面，看着就是「滚不动」。
   同时把 display 块撑成**内容宽**（`min-width: 100%` 保住窄公式的观感）——
   不然编号 `(2)` 的 `right: 0` 钉的是**容器**右边，会直接压在式子末尾上（用户反馈的重叠）。 */
.quote-md :deep(.katex-display) { overflow: visible; width: max-content; min-width: 100%; }
/* 编号（`(1)` / `(2)`）改回**跟着公式排**（KaTeX 默认是绝对定位在 `right: 0`）：
   窄栏里公式几乎占满整行，绝对定位的编号就会直接压在式子末尾上 —— 用户反馈的重叠就是它。
   改成静态流之后，宽度 = 式子 + 编号，放不下就整体横滚；放得下就完全不滚（不凭空多一条滚动条）。 */
.quote-md :deep(.katex-html > .tag) { position: static; margin-left: .3em; }
.item.note-row .quote-md { font-size: calc(11.5px * var(--ts, 1)); line-height: 1.5; }
.quote-md :deep(p) { margin: 0; }
/* 笔记正文里的 Markdown：**结构留着、字号压平**。
   用户口径 2026-11-22：笔记项要保 Markdown 兼容，但那里的字号得跟卡片正文同一档 ——
   原来 h1~h6 走浏览器默认的 2em / 1.5em / 1.17em，一条把整段解释存下来的笔记里
   标题比正文大三四倍，整张卡片被撑歪。这里只在**笔记项内**压平，层级交给粗体；
   阅读器正文、对话回答里的 Markdown 一律不受影响。 */
.item.note-row .md :deep(:is(h1, h2, h3, h4, h5, h6)) {
  font-size: calc(12.5px * var(--ts, 1));
  font-family: inherit; font-weight: 700; letter-spacing: 0;
  line-height: 1.45; margin: .2em 0 .1em; text-wrap: pretty;
}
.item.note-row .md :deep(:is(h1, h2, h3, h4, h5, h6):first-child) { margin-top: 0; }
.item.note-row .md :deep(p) { margin: .15em 0; }
.item.note-row .md :deep(:is(ul, ol)) { margin: .15em 0; padding-left: 1.2em; }
/* 公式也不许比正文大一档（KaTeX 默认 1.21em，块级还自带 1em 上下边距） */
.item.note-row .md :deep(.katex) { font-size: 1.02em; }
.item.note-row .md :deep(.katex-display) { margin: .2em 0; }
/* 批量态：行首勾选框 —— 与对话页 .tick 同一口径（24px 热区、无底、选中转主题色） */
.tick {
  width: 24px; height: 24px; flex: none; border: 0; background: none; cursor: pointer;
  color: var(--text-faint); display: inline-flex; align-items: center; justify-content: center;
  border-radius: var(--radius-ctl);
}
.tick:hover { color: var(--accent-hover); background: var(--bg-hover); }
.tick.on { color: var(--accent); }
/* 选中的卡片：主题色淡底 + 亮一档描边（与对话页行上的选中语义一致） */
.item.note-row.pick {
  border-color: color-mix(in srgb, var(--accent) 45%, var(--border-strong));
  background: color-mix(in srgb, var(--accent) 8%, var(--bg-layer));
}
.item.note-row.picking { cursor: pointer; }
/* 编辑态：把卡片让给编辑框（用户口径 2026-10-10："编辑笔记时，可以隐藏顶部的部分" →
   "应该可以占用整个元素面积方便编辑"）——
   徽章 / 页码 / 时间 / 定位删除按钮那一整行、以及引用原文全部收起，
   只留编辑框与底部「保存 / 取消」。要定位或删除先按「取消」退回查看态即可。 */
.item.note-row.editing .row.tiny,
.item.note-row.editing .quote { display: none; }
/* 笔记编辑框（用户口径 2026-10-10："优化一下侧栏编辑时的文本框样式"）：
   全局那条 `input, select, textarea` 是给**表单控件**写的（白底 + 深色描边 + 聚焦时一整圈 accent 光环），
   摆在笔记卡里像个"被塞进来的控件"。这里改成与阅读器浮层同一门语言：
   淡底 + 发丝描边，聚焦只把描边点亮一档（**不要 `--ring` 那圈光晕** —— 在窄侧栏里太吵，
   而且它就是全局 `textarea:focus` 带进来的）；顺手把浏览器自带的右下角 resize 角标去掉。 */
.item.note-row .note-edit {
  width: 100%; box-sizing: border-box; margin: 4px 0 6px; padding: 9px 11px;
  border: 1px solid var(--divider); border-radius: var(--r-sm);
  background: var(--bg-layer); color: var(--text); font: inherit; font-size: calc(13px * var(--ts, 1));
  line-height: 1.7; resize: none;
  transition: border-color var(--dur) var(--ease), background var(--dur) var(--ease);
}
/* 覆盖全局的 `textarea:hover { background: var(--bg-hover) }`（不然鼠标一过就闪一下灰） */
.item.note-row .note-edit:hover { background: var(--bg-layer); border-color: var(--border-strong); }
.item.note-row .note-edit:focus {
  outline: none; box-shadow: none;
  border-color: color-mix(in srgb, var(--accent) 60%, transparent);
  background: var(--bg-elev);
}
.item.note-row .note-edit::placeholder { color: var(--text-faint); }
/* 引用文献：工具条（那一行 `REFERENCES N 条`）在这里，卡片本身在 `ReferenceList` 里
   （与论文详情弹窗、正文标号的浮层共用同一份，所以那些 `.ref-item` / `.ref-lead` 样式也搬过去了）。 */
.ref-bar {
  display: flex; align-items: center; gap: 6px;
  margin: 0 0 6px; padding: 2px 0;
}
/* 顶栏那个组名：小一号、加宽字距的大写标签，跟条数拉开一点层次 */
.ref-cap { letter-spacing: .08em; font-weight: 600; margin-right: 2px; }
/* 「从高亮打开对应笔记」时的落点提示：闪一下边框与底色，用户才知道跳到了哪一条 */
.item.flash {
  animation: note-flash 1.2s var(--ease);
}
@keyframes note-flash {
  0%, 100% { background: var(--bg-layer); }
  25% { background: color-mix(in srgb, var(--accent) 16%, var(--bg-layer)); }
}
.item .figure {
  max-width: 100%; background: #fff; border-radius: var(--radius-sm);
  margin: 6px 0 2px; cursor: zoom-in;
}
/* 插图卡片：图注（=卡片标题）在图上方、页码/图号在图下方一行。
 * 「定位到图注」在图注**标题行的最右侧**（用户口径；以前浮在图片右上角，会压住图）。
 * 图注可能换行，所以按钮 `flex:none` 顶部对齐，只跟**首行**同高。 */
.fig-card { display: flex; flex-direction: column; }
.fig-head { display: flex; align-items: flex-start; gap: 6px; margin: 0 0 2px; }
.fig-caption {
  flex: 1; min-width: 0;
  font-size: calc(12px * var(--ts, 1)); line-height: 1.5; color: var(--text-dim);
  overflow-wrap: anywhere;
}
/* 图注/引用是**渲染过的 Markdown**（外层已经定了字号与行高，这里只把块级元素的间距压掉，
   免得一行短图注被 `<p>` 的默认外边距撑成两行） */
.fig-caption :deep(p) { margin: 0; }
.fig-caption :deep(.katex-display) { margin: 0; overflow-x: auto; overflow-y: hidden; }
.quote :deep(p) { margin: 0; display: inline; }
.fig-head .spacer { flex: 1; }
/* 光学对齐：按钮 26px、图注行盒 18px，上移 4px 后两者的**中心线**才对得上（图标不会看起来偏低）。 */
/* 图注右侧那两枚按钮（浮层打开 / 定位）**共用一套几何**：
   以前只给「定位」写了 margin-top: -4px（让它跟图注首行对齐），新加的浮层按钮没写，
   于是两枚钮一高一低（用户口径：没有与旁边按钮对齐）。 */
.fig-locate, .fig-overlay { flex: none; margin-top: -4px; color: var(--text-dim); }
.fig-locate:hover, .fig-overlay:hover { color: var(--accent-text); }
.fig-body { display: flex; }
.fig-body .figure { width: 100%; }
.fig-meta { margin-top: 2px; gap: 6px; }
/* 简报：只读态是「小标题 + 正文」的排版（跟侧栏其它内容一致），不是一组表单框。
   编辑态的输入框由 `.field` 提供样式，两者互斥出现。
   用户口径（本轮）：**去掉侧边那条竖线**（`.item.manual/.auto` 会画一条彩色左边框），
   改成一圈发丝描边 + 留白分区；要点换成点状符号、保持英文换成胶囊、术语表两列对齐。 */
.brief-bar { gap: 6px; margin-bottom: 12px; }
/* 简报正文：**不套框**（用户口径：各个内容不要放在一个框里），只靠留白分区。
   也不再是 `.item` —— 那条 2px 彩色左边框（"竖线"）随壳子一起消失了。 */
.brief-view { display: flex; flex-direction: column; gap: 14px; padding: 2px 2px 2px; }
.brief-sec { margin: 0; }
.brief-k {
  font-size: calc(10.5px * var(--ts, 1)); color: var(--text-faint); letter-spacing: .06em;
  text-transform: uppercase; margin-bottom: 3px;
}
.brief-v { font-size: calc(12.5px * var(--ts, 1)); line-height: 1.65; color: var(--text); overflow-wrap: anywhere; }
/* 要点：**点状符号**自己画（原来用圆点列表，缩进在窄栏里吃掉半行之宽） */
.brief-list {
  margin: 0; padding: 0; list-style: none;
  font-size: calc(12.5px * var(--ts, 1)); line-height: 1.6; color: var(--text);
}
.brief-list li { position: relative; padding-left: 13px; margin: 4px 0; }
.brief-list li::before {
  content: ''; position: absolute; left: 3px; top: .66em;
  width: 4px; height: 4px; border-radius: 999px;
  background: color-mix(in srgb, var(--text) 34%, transparent);
}
/* 保持英文不译：胶囊（等宽字体、不换行挤压） */
.brief-chips { display: flex; flex-wrap: wrap; gap: 5px; }
.brief-chip {
  font-family: var(--mono); font-size: calc(11.5px * var(--ts, 1)); line-height: 1;
  padding: 4px 8px; border-radius: 999px;
  /* 底色用「文字色 8%」而不是 `--bg-sunken`：后者与卡片底（`--bg-layer`）只差三格，
     胶囊在浅色主题里等于看不见（预览时确认过）。 */
  background: color-mix(in srgb, var(--text) 8%, transparent); color: var(--text);
}
/* 术语表：与简报卡之间留一档空、表头更淡，两列各自截断 */
.brief-terms-head { margin: 14px 0 4px; gap: 6px; }
.term-row {
  display: flex; gap: 10px; padding: 6px 2px;
  border-bottom: 1px solid var(--divider);
  font-size: calc(12.5px * var(--ts, 1));
}
.term-row .en { flex: 1; min-width: 0; font-family: var(--mono); font-size: calc(12px * var(--ts, 1)); color: var(--text-dim); }
.term-row .zh { flex: 1; min-width: 0; color: var(--text); }
/* 任务清单：一类一行。类型名固定宽度，几行的状态徽章才能对齐成一条竖线；
   失败原因单独一行、最多两行（长错误信息不该把清单撑成一屏）。 */
.task-row { padding: 7px 10px; margin-bottom: 6px; }
.task-name { font-size: calc(12.5px * var(--ts, 1)); font-weight: 600; min-width: 3.5em; }
/* 进度条：夹在信息行与失败原因之间留一点缝（`.bar` 本身只有 5px 高，不像卡片那样占地方） */
.task-bar { margin-top: 6px; }
.task-msg {
  margin-top: 4px; color: var(--danger); line-height: 1.5;
  display: -webkit-box; -webkit-line-clamp: 2; line-clamp: 2;
  -webkit-box-orient: vertical; overflow: hidden;
}
</style>
