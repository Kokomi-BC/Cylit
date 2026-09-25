<script setup lang="ts">
/**
 * 知识库：**跨论文知识检索与浏览工作台**。
 *
 * 定位（用户口径）：既不是第二个阅读器，也不是设置页 ——
 * 完整阅读 / 双语对照 / 公式细看 / 定位都回 `/reader/:paperId`，索引管理在 `/settings#kb`。
 *
 * 两种模式：
 *
 * - **搜索**（本页的主体）：`/rag/similar` 一次返回 `ChunkResult[]`，前端**只做展示层聚合** ——
 *   按 `paper_id` 收成 `PaperResult[]`（论文是一级结果，chunk 是二级结果），
 *   左侧窄栏浏览 + 选择，右侧详情面板读完整内容。检索链路（embedding / 向量库 / RRF / 重排 /
 *   数据结构）一律没动，分数与漏斗原样保留，只是收进右栏默认折叠的「检索详情」。
 * - **浏览**：按论文 + 类型 + 语言翻 `/knowledge/chunks`；选「插图」时是**图库网格**。
 *   每条可「展开全文」（`/knowledge/chunk`，**只这一条**，不做中英对照）。
 *
 * 信息层级（三级，别混）：
 *   ① 论文标题 / 命中内容 / 页码·章节 / 查看原文 —— 直接显示；
 *   ② chunk 数量 / 内容类型 / 筛选条件 —— 弱化（小字、低对比）；
 *   ③ score / topK / reranker / 检索漏斗 —— 全在「检索详情」里，默认不出现。
 *
 * 搜索记录是浏览器式的（`utils/searchHistory`，本机 localStorage，管理入口在设置页 `#history`）。
 */

import { computed, nextTick, onMounted, onUnmounted, ref, toRefs, watch } from 'vue';
import { useRouter } from 'vue-router';
import { api } from '@/api/client';
import type {
  KnowledgeChunkDetail, KnowledgeChunkRow, KnowledgeOverview, SimilarItem, SimilarResult,
} from '@/api/types';
import { reportError, state, toast, withLoading } from '@/stores/app';
import { kbState } from '@/stores/knowledge';
import { openTab } from '@/stores/tabs';
import { readerTarget } from '@/utils/navigation';
import { latexToHtml, mdToHtml } from '@/composables/useMarkdown';
import { searchMarksHtml } from '@/utils/searchMarks';
import {
  addSearch, clearHistory, loadHistory, removeSearch, subscribeHistory, type SearchEntry,
} from '@/utils/searchHistory';
import AppIcon from '@/components/AppIcon.vue';

/** 四类 chunk（与后端 knowledge_service.CHUNK_TYPES 一致）+ 展示用名词与图标。 */
const CHUNK_TYPES = [
  { key: 'text', label: '正文', icon: 'document' },
  { key: 'formula', label: '公式', icon: 'formula' },
  { key: 'figure', label: '插图', icon: 'image' },
  { key: 'table', label: '表格', icon: 'table' },
];
const LANG_OPTIONS = [
  { key: 'zh', label: '中文' },
  { key: 'en', label: '原文' },
];
/** 返回条数（后端融合 + 重排后取前 N）。 */
const TOP_K = 6;
/** 浏览一次拉多少条（后端上限 500；200 条够翻，也不至于一屏塞死）。 */
const BROWSE_LIMIT = 200;
/** 论文卡片默认先摆几条 chunk（其余点「查看全部」展开）。 */
const PAPER_PREVIEW_CHUNKS = 3;
/** 详情面板宽度低于这个值就改成"列表在上、详情在下"（窄屏不硬挤两栏）。 */
const NARROW_QUERY = '(max-width: 900px)';

type KbPaper = KnowledgeOverview['items'][number];

/**
 * 展示层聚合：**一篇论文 + 它命中的那些 chunk**。
 *
 * 后端返回的是一次检索的 `ChunkResult[]`（平铺），左栏要的是"论文是一级结果"，
 * 所以在 `paperResults` 里按 `paper_id` 收一次。只是换个视图，不动数据。
 */
interface PaperResult {
  paper_id: string;
  paper_title: string;
  chunks: SimilarItem[];
  total_chunks: number;
  best_score: number;
}

/** 左栏/右栏共用的"当前选中"：论文级（看梗概）或 chunk 级（看全文）。 */
type KbSelection =
  | { kind: 'paper'; paper_id: string; chunk_id: '' }
  | { kind: 'chunk'; paper_id: string; chunk_id: string }
  | null;

const router = useRouter();

/* ------------------------------ 页面状态 ------------------------------ */

/**
 * 输入、结果、浏览筛选、展开的全文都放在 `stores/knowledge`（模块级 reactive）里：
 * 外壳按"标签键"重建视图，切走再切回来组件是新的 —— 状态放组件里就会丢（用户报过）。
 * 用 `toRefs` 取出来，模板照旧写 `mode` / `result`，脚本里照旧 `.value`。
 */
const {
  mode, query, scope, paperId, excludeSelf, result, error,
  bPaperId, bType, bLang, chunks, detailId, detail,
} = toRefs(kbState);

/** 纯瞬时的界面状态留在组件里：忙碌标记、历史下拉开合/高亮、当前选中。 */
const busy = ref(false);
const overview = ref<KnowledgeOverview | null>(null);
/** 当前选中的论文 / chunk（从结果里再选中一次 = 取消选中）。 */
const selected = ref<KbSelection>(null);
/** 左栏里已点开「查看全部」的论文。 */
const expanded = ref<string[]>([]);
/** 右侧详情：已取回的 chunk 正文缓存（同一 chunk 再点不重复请求）。 */
const detailCache = ref<Record<string, KnowledgeChunkDetail>>({});
const detailBusy = ref(false);
/** 详情面板里「检索详情」的开合（默认折叠：三级信息不打扰普通阅读）。 */
const metaOpen = ref(false);
/** 顶部筛选行是否换行折叠（窄屏显示/隐藏次要下拉）。 */
const narrow = ref(false);

let mq: MediaQueryList | null = null;
const onMq = (ev: MediaQueryListEvent | MediaQueryList) => { narrow.value = ev.matches; };

const papers = computed<KbPaper[]>(() => overview.value?.items || []);

/** 页头右侧的轻量状态（**不是统计卡**）：库里有什么，直接决定你怎么读检索结果。 */
const statusText = computed(() => {
  const o = overview.value;
  if (!o) return state.loading['kb-overview'] ? '正在读取…' : '—';
  const vector = (o.vector_store || {}) as Record<string, any>;
  return `库内 ${o.papers_in_kb} 篇 · ${o.chunks} chunk · 向量库${vector.available ? '可用' : '不可用'}`;
});
const statusTitle = computed(() => {
  const o = overview.value;
  if (!o) return '';
  const byType = o.chunks_by_type || {};
  const vector = (o.vector_store || {}) as Record<string, any>;
  return `论文 ${o.papers_in_kb}/${o.papers_total} 篇已纳入 · 正文 ${byType.text || 0} / 公式 ${byType.formula || 0}`
    + ` / 插图 ${byType.figure || 0} / 表格 ${byType.table || 0}`
    + (vector.available ? '' : ` · 向量库不可用：${vector.error || '未返回原因'}`);
});
/** 库里一条 chunk 都没有：搜索与浏览都给"去建索引"的明确去处。 */
const libraryEmpty = computed(() => !!overview.value && !overview.value.chunks);

function setMode(next: 'search' | 'browse') {
  mode.value = next;
  if (next === 'browse' && bPaperId.value && !chunks.value.length) void loadChunks();
}

/* --------------------------- 搜索结果的展示层聚合 --------------------------- */

/** `ChunkResult[]` → `PaperResult[]`（一级：论文；二级：chunk），顺序按"该论文最相关的一条"。 */
const paperResults = computed<PaperResult[]>(() => {
  const items = result.value?.items || [];
  const byPaper = new Map<string, PaperResult>();
  for (const it of items) {
    const pid = String(it.paper_id || '');
    if (!pid) continue;
    let group = byPaper.get(pid);
    if (!group) {
      group = {
        paper_id: pid,
        paper_title: it.paper_title || pid,
        chunks: [],
        total_chunks: 0,
        best_score: 0,
      };
      byPaper.set(pid, group);
    }
    group.chunks.push(it);
  }
  const out = [...byPaper.values()];
  for (const group of out) {
    group.total_chunks = group.chunks.length;
    // 一篇论文内部的 chunk 也按"最相关"排（rerank → rrf），命中的第一条就是最该看的
    group.chunks.sort((a, b) => scoreNum(b, 'rerank') - scoreNum(a, 'rerank')
      || scoreNum(b, 'rrf') - scoreNum(a, 'rrf'));
    group.best_score = scoreNum(group.chunks[0], 'rerank') || scoreNum(group.chunks[0], 'rrf');
  }
  // 论文之间同样按最相关排序，但保持 store 里第一次出现的相对次序（稳定排序）
  return out.sort((a, b) => b.best_score - a.best_score);
});

const hitCount = computed(() => (result.value?.items || []).length);
const paperCount = computed(() => paperResults.value.length);
/** 搜索中且已有旧结果：左栏保留旧结果，只在顶部提示"检索中"。 */
const staleWhileBusy = computed(() => busy.value && !!result.value);

/** 论文卡片实际展示的 chunk：默认前 3 条，或用户点了「查看全部」。 */
function shownChunks(group: PaperResult): SimilarItem[] {
  if (expanded.value.includes(group.paper_id)) return group.chunks;
  return group.chunks.slice(0, PAPER_PREVIEW_CHUNKS);
}

const leftPane = ref<HTMLElement | null>(null);

/**
 * 左栏内容没撑满一屏时**默认展开全部片段**。
 *
 * 用户口径："左侧内容如果没有撑满页面则默认展开" —— 折叠只是为了不把一屏塞满，
 * 既然还空着，就没有理由让人再点一次"查看全部 N 个片段"。
 * 判定用真实布局（渲染后测量 scrollHeight / clientHeight），不做数量上的猜测。
 */
function autoExpandIfFits(): void {
  const el = leftPane.value;
  if (!el) return;
  const collapsed = paperResults.value
    .filter((g) => g.total_chunks > PAPER_PREVIEW_CHUNKS && !expanded.value.includes(g.paper_id));
  if (!collapsed.length) return;
  if (el.scrollHeight > el.clientHeight + 4) return;   // 已经撑满 → 保持折叠
  expanded.value = [...expanded.value, ...collapsed.map((g) => g.paper_id)];
}
function toggleExpand(pid: string): void {
  const set = new Set(expanded.value);
  if (set.has(pid)) set.delete(pid); else set.add(pid);
  expanded.value = [...set];
}

/* ------------------------------ 选择与详情 ------------------------------ */

function isSelected(paperId_: string, chunkId: string): boolean {
  const sel = selected.value;
  return !!sel && sel.paper_id === paperId_ && sel.chunk_id === chunkId;
}

/**
 * 选中的论文 id（用来找该论文的聚合结果、给论文卡片加"当前所在"的弱标记）。
 *
 * ⚠️ 这里**不要**写 `sel.kind === 'chunk' ? sel.paper_id : ''` 这类按 `kind` 判据分叉的写法：
 * 两种选中都带 `paper_id`，直读它最省事，还避开了"联合类型被 TS 控制流收窄 →
 * 该比较在产物里被当成恒假"的坑（实测：这样写时本计算值恒为空串，
 * 点论文标题后右栏找不到所属论文、只剩占位符）。同理 `detailItem` 用
 * 「有没有 chunk_id」而不是「kind 是不是 chunk」来判断选中粒度。
 */
const selectedPaperId = computed(() => {
  const sel = selected.value;
  return sel ? sel.paper_id : '';
});

/** 选中的那一条 chunk（论文级选中时为 null）。 */
const selectedChunk = computed<SimilarItem | null>(() => {
  const sel = selected.value;
  const cid = sel ? sel.chunk_id : '';
  if (!cid) return null;
  return (result.value?.items || []).find((i) => i.chunk_id === cid) || null;
});

const selectedGroup = computed<PaperResult | null>(() => {
  const pid = selectedPaperId.value;
  if (!pid) return null;
  return paperResults.value.find((g) => g.paper_id === pid) || null;
});

/** 右栏要渲染的那条 chunk：选中具体 chunk 时是它本身，选中论文时是该论文最相关的一条。 */
const detailItem = computed<SimilarItem | null>(() => (
  selectedChunk.value || selectedGroup.value?.chunks[0] || null
));

/**
 * 窄屏下详情在**列表下方**（不是 drawer）：选中后把它带进视野，否则点了没反应。
 * 桌面两栏并排时不滚 —— 用户自己点的那一条就在眼前，滚动只会让人失去位置感。
 */
async function revealDetail(): Promise<void> {
  if (!narrow.value) return;
  await nextTick();
  document.querySelector('.kb-right')?.scrollIntoView({ block: 'start', behavior: 'smooth' });
}

function selectPaper(group: PaperResult): void {
  if (isSelected(group.paper_id, '')) { selected.value = null; return; }
  selected.value = { kind: 'paper', paper_id: group.paper_id, chunk_id: '' };
  void revealDetail();
}

/** 点 chunk：选中它（再点同一条 = 取消，不会叠出第二个面板）。 */
function selectChunk(item: SimilarItem): void {
  const cid = String(item.chunk_id || '');
  if (!cid || isSelected(String(item.paper_id || ''), cid)) { selected.value = null; return; }
  selected.value = { kind: 'chunk', paper_id: String(item.paper_id || ''), chunk_id: cid };
  void revealDetail();
}

/** 关闭详情：只收起右栏，**不清空左栏结果、不改搜索词**。 */
function closeSelection(): void {
  selected.value = null;
}

/**
 * 换一批结果时的清理：旧的选中 / 展开 / 详情缓存全部作废。
 * 搜索与"重跑一条搜索记录"共用它 —— 否则右栏会留着上一轮的正文（张冠李戴）。
 */
function resetSelection(): void {
  selected.value = null;
  expanded.value = [];
  detailCache.value = {};
  detailBusy.value = false;
}

/** 已取回的详情（没取回就是 null → 面板显示骨架）。 */
const detailData = computed<KnowledgeChunkDetail | null>(() => {
  const cid = String(detailItem.value?.chunk_id || '');
  return cid ? (detailCache.value[cid] || null) : null;
});

/**
 * 懒取完整 chunk 正文（`/knowledge/chunk`）。
 *
 * 列表接口只给 260 字 `snippet`（规划 §8：全文只在向量库），所以右侧的"完整内容"必须再取一次；
 * 取回的结果进缓存，同一条反复点不再打接口。竞态用"请求回来时还是不是它"来挡 ——
 * 快速连点两条时，先发的慢响应不会把后选中的内容覆盖掉。
 */
async function loadDetail(chunkId: string): Promise<void> {
  if (!chunkId || detailCache.value[chunkId]) return;
  detailBusy.value = true;
  try {
    const data = await api.get<KnowledgeChunkDetail>('/knowledge/chunk', { chunk_id: chunkId });
    if (String(detailItem.value?.chunk_id || '') !== chunkId) return;
    detailCache.value = { ...detailCache.value, [chunkId]: data };
  } catch (err) {
    reportError(err, '读取片段内容失败');
  } finally {
    if (String(detailItem.value?.chunk_id || '') === chunkId) detailBusy.value = false;
  }
}

/** 选中变化 → 取正文、重置「检索详情」开合（不让上一条的调试状态串台）。 */
watch(() => String(detailItem.value?.chunk_id || ''), (cid) => {
  metaOpen.value = false;
  if (cid) void loadDetail(cid);
}, { immediate: true });

/**
 * 详情切换的入场动画（`.kb-detail-body` 上的 `:key` 换掉就重播一次）。
 * 只做一次淡入 + 轻微上浮：**不重新加载页面、不动左栏滚动位置**，换一条内容时有个交接感。
 */
const contentAnim = ref(0);
watch(() => String(detailItem.value?.chunk_id || ''), () => { contentAnim.value += 1; });

/** 论文选中但该论文还没有 chunk（理论上不会有）：右栏不走空。 */
const detailEmptyTitle = computed(() => (libraryEmpty.value ? '知识库还是空的' : '选择一个搜索结果'));

/* ------------------------------ 搜索 ------------------------------ */

/**
 * 界面范围 → 后端 scope：
 * `本论文` → paper；`全部` + 勾了排除 → cross（跨文献比对，要带上被排除的那篇）；
 * 其余 → all。勾选框只在"全部"下出现（"本论文 + 排除本篇"必然空）。
 */
const effectiveScope = computed<'all' | 'cross' | 'paper'>(() => {
  if (scope.value === 'paper') return 'paper';
  return (excludeSelf.value && paperId.value) ? 'cross' : 'all';
});

async function runSearch(): Promise<void> {
  const text = query.value.trim();
  if (!text) { toast('请先输入要检索的内容', 'warn'); return; }
  if (effectiveScope.value !== 'all' && !paperId.value) {
    toast(scope.value === 'paper' ? '请先选择论文' : '勾了「排除所选论文」，请先选择要排除的论文', 'warn');
    return;
  }
  busy.value = true;
  error.value = '';
  result.value = null;
  resetSelection();
  const scopeUsed = effectiveScope.value;
  try {
    const payload: Record<string, unknown> = { query: text, scope: scopeUsed, top_k: TOP_K };
    if (scopeUsed !== 'all') payload.paper_id = paperId.value;
    const data = (await api.post<SimilarResult>('/rag/similar', payload)) || {};
    result.value = data;
  await nextTick();
  autoExpandIfFits();
    // 成功搜索才写记录（失败/被拒不入历史，免得记录里全是没结果的词）
    addSearch({
      q: text, mode: 'search', scope: scopeUsed,
      paper_id: scopeUsed === 'all' ? null : paperId.value,
      hits: (data.items || []).length,
    });
    refreshHistory();
    histOpen.value = false;
    histIndex.value = -1;
  } catch (err) {
    error.value = (err as any)?.message ?? String(err);
    reportError(err, '检索失败');
  } finally {
    busy.value = false;
  }
}

/* ---------------------------- 检索详情（三级信息） ---------------------------- */

function scoreNum(item: SimilarItem | null | undefined, key: string): number {
  const value = item?.scores?.[key];
  return value === null || value === undefined ? 0 : Number(value);
}

function fmtScore(value: unknown): string {
  if (value === null || value === undefined || value === '') return '—';
  const n = Number(value);
  return Number.isFinite(n) ? n.toFixed(3) : String(value);
}

function fmtCount(value: unknown): string {
  if (value === null || value === undefined || value === '') return '—';
  return String(value);
}

/** 一条 chunk 的全部检索分数（原样保留，只是默认折叠在右栏里）。 */
const detailScores = computed(() => {
  const item = detailItem.value;
  if (!item) return [];
  const scores = item.scores || {};
  const rows = [
    { k: '向量相似度', v: fmtScore(scores.vector) },
    { k: '词法命中', v: fmtScore(scores.lexical) },
    { k: 'RRF 融合', v: fmtScore(scores.rrf) },
    { k: '重排分数', v: fmtScore(scores.rerank) },
  ];
  return rows;
});

/** 名次信息（向量第几 / 词法第几 / 重排第几）：开发者要的召回细节。 */
const detailRanks = computed(() => {
  const ranks = detailItem.value?.ranks || {};
  const rows = [
    { k: '向量召回名次', v: ranks.vector_rank },
    { k: '词法召回名次', v: ranks.lexical_rank },
    { k: '融合名次', v: ranks.rrf_rank ?? ranks.rrf },
    { k: '重排名次', v: ranks.rerank_rank ?? ranks.rerank },
  ];
  return rows.filter((r) => r.v !== null && r.v !== undefined);
});

/** 检索漏斗：向量 + 词法 → 融合 → 返回 · reranker（原样展示，不再挤在结果行里）。 */
const retrieval = computed<Record<string, any> | null>(() => result.value?.retrieval || null);
const funnelRows = computed(() => {
  const r = retrieval.value;
  if (!r) return [];
  const rerank = r.rerank || {};
  return [
    { k: '初始召回', v: `${fmtCount(r.vector_hits)} 向量 + ${fmtCount(r.lexical_hits)} 词法` },
    { k: 'RRF 融合后', v: fmtCount(r.fused) },
    { k: '最终返回', v: fmtCount(r.returned) },
    { k: 'Reranker', v: rerank.model || '—' },
    { k: '向量模型', v: r.vector_model || '—' },
    { k: '检索范围', v: r.scope || '—' },
  ];
});

const rerankDegraded = computed(() => !!retrieval.value?.rerank?.degraded);

/* ---------------------------- 搜索记录（浏览器式） ---------------------------- */

const history = ref<SearchEntry[]>([]);
const histOpen = ref(false);
const histIndex = ref(-1);
let unsubscribeHistory: (() => void) | null = null;

function refreshHistory() {
  history.value = loadHistory();
}

/** 下拉里显示最近 10 条；已经打了字就按子串过滤（浏览器的做法）。 */
const shownHistory = computed<SearchEntry[]>(() => {
  const needle = query.value.trim().toLowerCase();
  const list = needle
    ? history.value.filter((e) => e.q.toLowerCase().includes(needle))
    : history.value;
  return list.slice(0, 10);
});

/** 记录的"范围"文案（本论文 / 跨文献排除某篇）。 */
function entryScope(entry: SearchEntry): string {
  if (entry.scope === 'paper') return '本论文';
  if (entry.scope === 'cross') return '排除本篇';
  return '全部';
}

function onSearchKey(ev: KeyboardEvent): void {
  const list = shownHistory.value;
  if (histOpen.value && list.length && (ev.key === 'ArrowDown' || ev.key === 'ArrowUp')) {
    ev.preventDefault();
    histIndex.value = ev.key === 'ArrowDown'
      ? (histIndex.value + 1) % list.length
      : (histIndex.value <= 0 ? list.length - 1 : histIndex.value - 1);
    return;
  }
  if (ev.key === 'Escape') { histOpen.value = false; histIndex.value = -1; return; }
  if (ev.key === 'Enter') {                       // Ctrl/⌘+Enter 与 Enter 都是提交
    ev.preventDefault();
    const picked = histOpen.value ? list[histIndex.value] : undefined;
    if (picked) { void applyHistory(picked); return; }
    void runSearch();
  }
}

/** 点/选中一条历史：把当时的范围一起还原，然后立刻重跑。 */
async function applyHistory(entry: SearchEntry): Promise<void> {
  query.value = entry.q;
  if (entry.scope === 'paper') { scope.value = 'paper'; excludeSelf.value = false; }
  else if (entry.scope === 'cross') { scope.value = 'all'; excludeSelf.value = true; }
  else { scope.value = 'all'; excludeSelf.value = false; }
  if (entry.paper_id) paperId.value = entry.paper_id;
  histOpen.value = false;
  histIndex.value = -1;
  resetSelection();
  await runSearch();
}

function dropHistory(entry: SearchEntry): void {
  removeSearch(entry);
  refreshHistory();
  toast('已删除这条搜索记录', 'ok', 1800);
}

function clearAllHistory(): void {
  if (!history.value.length) return;
  if (!confirm(`清空全部 ${history.value.length} 条搜索记录？\n\n只影响本机浏览器里的这份记录，`
    + '论文、索引、笔记都不受影响。')) return;
  clearHistory();
  refreshHistory();
  histIndex.value = -1;
  toast('已清空搜索记录', 'ok', 2200);
}

/* ------------------------------ 浏览 ------------------------------ */

/** 「插图」= 图库网格；其余类型用紧凑列表（列表里也带小缩略图）。 */
const isGallery = computed(() => bType.value === 'figure');

async function loadChunks(): Promise<void> {
  if (!bPaperId.value) { chunks.value = []; return; }
  closeDetail();
  await withLoading('kb-chunks', async () => {
    const data = await api.get<{ items: KnowledgeChunkRow[] }>('/knowledge/chunks', {
      paper_id: bPaperId.value,
      chunk_type: bType.value || undefined,
      lang: bLang.value || undefined,
      limit: BROWSE_LIMIT,
    });
    chunks.value = data.items || [];
  });
}

/* ------------------------------ 展开全文（浏览） ------------------------------ */

const browseDetailBusy = ref(false);

/** 点同一条再点一次 = 收起（按钮文案跟着变）。 */
async function toggleDetail(chunkId: string): Promise<void> {
  if (detailId.value === chunkId) { closeDetail(); return; }
  detailId.value = chunkId;
  detail.value = null;
  browseDetailBusy.value = true;
  try {
    detail.value = await api.get<KnowledgeChunkDetail>('/knowledge/chunk', { chunk_id: chunkId });
    await nextTick();
    document.getElementById('kb-detail')?.scrollIntoView({ block: 'start', behavior: 'smooth' });
  } catch (err) {
    detailId.value = '';
    reportError(err, '展开全文失败');
  } finally {
    browseDetailBusy.value = false;
  }
}

function closeDetail(): void {
  detail.value = null;
  detailId.value = '';
}

/* ------------------------------ 小工具 ------------------------------ */

function typeMeta(key?: string | null) {
  return CHUNK_TYPES.find((t) => t.key === key) || { key: key || '', label: key || '—', icon: 'document' };
}

function langShort(lang?: string | null): string {
  const key = lang || 'en';
  // `any` = 语言无关（公式原子行）：两种语言检索都能召回，徽标显示「通用」
  if (key === 'any') return '通用';
  return key === 'zh' ? '中' : '原';
}

/** 双语句：`中` / `原` / `通用` / `中+原`（两路都召回同一条时标注）。 */
function langLabel(item: SimilarItem): string {
  const langs = item?.langs?.length ? item.langs : [item?.lang || 'en'];
  if (langs.includes('any')) return '通用';
  return langs.map((l) => (l === 'zh' ? '中' : '原')).join('+');
}

/**
 * 左栏要不要给这条出缩略图。
 *
 * **只给插图**。另外两类看似"也有裁图"，实测都不该摆：
 * - **表格**：条目带的裁图是**图注文字条**（实测 attention/ turboquant 两张都是 3316×126、
 *   26:1 的一条文字），缩到 76×56 只剩几个字母，看着像坏图 —— 等"表格裁图改成截表体"再说；
 * - **公式**：正文已经渲染成可选中、可复制的 KaTeX 文字，再摆一张裁图是重复同一件事。
 */
function hasThumb(item: SimilarItem): boolean {
  return item?.chunk_type === 'figure' && !!item?.image_url;
}

/**
 * 摘要渲染的最小字段集：检索条目（`SimilarItem`）与浏览条目（`KnowledgeChunkRow`）
 * 只共有这三个字段，所以这里用结构类型而不是某一边的具体类型 —— 两个列表共用一套渲染。
 */
type SummaryLike = { snippet?: string | null; caption?: string | null; paper_id?: string | null };

/**
 * 起点被截断的公式：丢掉**最后一个没有配对的 `$` 及其后面**的内容。
 *
 * 为什么需要：列表摘要用的是检索片段（后端截到 260 字），一个较长的行内公式会被拦腰砍断 ——
 * 落单的 `$` 让 marked-katex 认为"公式没结束"，于是整段原始 LaTeX（`\langle \boldsymbol{q}…`）
 * 原样显示在列表里（实测 p.21 那条）。截断本身无法恢复，所以只把这段**未闭合的公式尾部**去掉，
 * 并加一个省略号，表示"这里被截断了"，而不是把 LaTeX 源码当正文展示。
 *
 * 只处理落单的**单个** `$`：`$$…$$` 行间公式（成对出现）与代码围栏里的 `$` 都不碰。
 */
function dropUnclosedMath(src: string): string {
  let inFence = false;
  const singles: number[] = [];
  for (let i = 0; i < src.length; i += 1) {
    if (src.startsWith('```', i)) { inFence = !inFence; i += 2; continue; }
    if (inFence || src[i] !== '$') continue;
    if (src[i + 1] === '$') { i += 1; continue; }   // `$$` 块级定界符，整对跳过
    singles.push(i);
  }
  if (singles.length % 2 === 0) return src;
  return `${src.slice(0, singles[singles.length - 1]).replace(/\s+$/, '')}…`;
}

/**
 * 摘要正文：**保留完整 Markdown / LaTeX**，只做"压缩排版噪声"的轻度清理。
 *
 * 为什么不再剥掉 `**`、`$`：用户要求两侧都完整支持 Markdown —— 摘要里原来被剥掉的
 * 粗体、列表、公式现在照常渲染（公式走 KaTeX）。这里只清两类东西：
 * - **图片语法**（`![...](../figures/x.png)`）：列表里画一张裁图会把摘要顶掉，改用图注/文字；
 * - **硬换行**：检索片段里常带换行，压成一行后交给 CSS 做 2~3 行截断。
 *
 * 不清洗、不翻译、不改写任何正文措辞 —— 右栏的完整内容连这一步都不做。
 */
function summaryBody(item: SummaryLike | null | undefined): string {
  const raw = item?.caption || item?.snippet || '';
  const text = String(raw)
    .replace(/!\[([^\]]*)\]\([^)]*\)/g, '$1')     // 图片 → 只留 alt（通常就是图注摘要）
    .replace(/\r/g, '')
    .replace(/\n{2,}/g, '\n')                      // 段间空行压掉，避免截断只剩空白
    .trim();
  return dropUnclosedMath(text);
}

/** 摘要（Markdown + KaTeX 渲染 + 命中高亮），行数由 CSS 截断（见 `.chunk-sum` / `.hit-text`）。 */
function summaryHtml(item: SummaryLike | null | undefined): string {
  if (!item) return '';
  return searchMarksHtml(summaryBody(item), query.value, item.paper_id);
}

/**
 * 右栏正文 → HTML（Markdown + KaTeX；相对资源路径改写成后端静态地址）。
 *
 * **这里不做任何"公式修补"**。曾经有一层 `sanitizeChunkMarkdown`（删 `$$…$$` 里的杂散 `$`、
 * 给裸公式行补定界符）和一层 `repairLatex`（给公式补全 `\right`/`{`），都是上一轮的
 * **展示层兜底**；源头（索引层）修好之后按用户要求全撤了 —— 实测线上 289 行
 * （187 正文 + 102 公式）**不过任何清洗**直接渲染，KaTeX 报错 **0 处**。
 * 留着它们不只是多余：多一层改写就多一个"左右两栏渲染不一致"的变量
 * （用户截图那张表就是这么被怀疑上的），排查时纯属干扰。渲染只走 `mdToHtml` 一条路。
 */
const detailHtml = computed(() => {
  const content = detailData.value?.content;
  if (!content) return '';
  const paperId = detailData.value?.paper_id || detailItem.value?.paper_id;
  return mdToHtml(content, paperId);
});

/* ------------------------- 公式：渲染 + 复制 LaTeX ------------------------- */

/**
 * 公式 chunk 的正文其实就是**那条 LaTeX**（后端按 `公式 <块 id>：<latex>` 拼的，
 * 见 `knowledge_service._formula_chunk_text`），所以这一条**不该走 Markdown、也不该只给裁图**：
 * 直接把 LaTeX 渲染出来 —— 变成可选中的文字，能复制、能搜、能带公式块样式。
 */
const FORMULA_PREFIX = /^\s*公式\s+\S+\s*[：:]\s*/;

/**
 * 公式 chunk 的 LaTeX 源 = 正文去掉 `公式 <块 id>：` 前缀、剥掉 `$$…$$` 定界符。
 *
 * 这里曾经还要"补全被切断的 LaTeX 结构"（`\right` 后缺定界符 / `{` 未闭合）—— 同样是展示层
 * 兜底，源头修好后实测 102 条公式行直接渲染报错 0 处，随 `sanitizeChunkMarkdown` 一起撤了。
 */
function latexSource(text?: string | null): string {
  let body = String(text ?? '').replace(FORMULA_PREFIX, '').trim();
  const wrapped = /^\$\$([\s\S]*)\$\$$/.exec(body);
  if (wrapped) body = wrapped[1].trim();
  return body;
}

/** 当前详情是不是"公式"（决定右栏是不是渲染 KaTeX）。 */
const detailIsFormula = computed(() => (detailData.value?.chunk_type
  || detailItem.value?.chunk_type) === 'formula');

/**
 * 公式渲染结果。**行间公式优先**：公式 chunk 里的 LaTeX 都是独立成行的原子公式，
 * 用 display 模式才不会跟正文挤在一起；行内公式（`$…$`）在 mdToHtml 里已经处理。
 */
const formulaHtml = computed(() => {
  if (!detailIsFormula.value) return '';
  const src = latexSource(detailData.value?.content || detailItem.value?.snippet);
  if (!src) return '';
  return latexToHtml(src, true);
});

/** 这条公式的 LaTeX 源（复制按钮与「复制 LaTeX」都用它）。 */
const formulaSource = computed(() => {
  if (!detailIsFormula.value) return '';
  return latexSource(detailData.value?.content || detailItem.value?.snippet);
});

/** 复制的原文：公式给 LaTeX，其余给 chunk 正文。 */
const copyText = computed(() => (
  detailIsFormula.value
    ? formulaSource.value
    : String(detailData.value?.content || detailItem.value?.snippet || '')
));

/**
 * 把文本写进剪贴板。两条路都要试：
 *
 * 1. `navigator.clipboard.writeText`（需安全上下文 + **文档有焦点**）；
 * 2. 临时 `textarea` + `execCommand('copy')` 兜底 —— 本地 http、iframe、
 *    无焦点窗口（headless / 后台标签）下第 1 条会直接抛 `NotAllowedError`，实测过。
 *
 * ⚠️ 第 2 条**不能写在 `else` 里**：`navigator.clipboard` 存在但被拒绝时也要走兜底，
 * 否则"复制"在最需要兜底的场景下反而整个失效。
 */
async function writeClipboard(value: string): Promise<boolean> {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(value);
      return true;
    }
  } catch { /* 落到下面的兜底 */ }
  try {
    const ta = document.createElement('textarea');
    ta.value = value;
    ta.setAttribute('readonly', '');
    ta.style.position = 'fixed';
    ta.style.top = '-1000px';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    ta.setSelectionRange(0, value.length);
    const ok = document.execCommand('copy');
    ta.remove();
    return ok;
  } catch {
    return false;
  }
}

/** 复制到剪贴板 + 提示（详情按钮与列表小按钮共用）。 */
async function copyToClipboard(text: string, label: string): Promise<void> {
  const value = String(text ?? '');
  if (!value) { toast('没有可复制的内容', 'warn'); return; }
  if (await writeClipboard(value)) toast(`已复制${label}`, 'ok', 1600);
  else toast('复制失败：浏览器拒绝了剪贴板访问，可手动划选复制', 'warn', 2600);
}

/** 右栏内容区右上角的「复制 LaTeX / 复制正文」。 */
function copyCurrent(): void {
  void copyToClipboard(copyText.value, detailIsFormula.value ? ' LaTeX' : '正文');
}

/** 来源信息：一律取详情返回的（它才是权威），缺失时回落到检索条目。 */
const sourceInfo = computed(() => {
  const d = detailData.value;
  const item = detailItem.value;
  const title = detailData.value?.paper_title || item?.paper_title || item?.paper_id || '';
  const page = d?.page ?? item?.page;
  const section = d?.section ?? item?.section;
  const typeKey = d?.chunk_type || item?.chunk_type;
  const lang = d?.lang || item?.lang;
  return {
    title,
    paper_id: d?.paper_id || item?.paper_id || '',
    page,
    section,
    typeLabel: typeMeta(typeKey).label,
    langLabel: lang ? langShort(lang) : '',
  };
});

/** 右栏标题：`第 22 页 · IMPLEMENTATION`。 */
const detailMeta = computed(() => {
  const s = sourceInfo.value;
  const parts: string[] = [];
  if (s.page) parts.push(`p.${s.page}`);
  if (s.section) parts.push(s.section);
  return parts.join(' · ');
});

/**
 * 跳回原文：**唯一入口**，统一回 Reader 定位到对应 block。
 *
 * ⚠️ 必须在点击回调里调（`openTab` 会改响应式状态）：`:to="fn()"` 那种写法会让
 * "渲染 → 改状态 → 再渲染"死循环，本页搜索曾经必定卡死。
 */
function openSource(item: { paper_id?: string | null; block_id?: string | null; page?: number | null }) {
  void router.push(openTab(readerTarget(item.paper_id ?? '', item.block_id, item.page)) as never);
}

/** 右栏「打开原文位置」：用当前详情（有 block_id 就精确到块，否则退到页码）。 */
function openCurrentSource() {
  const d = detailData.value;
  const item = detailItem.value;
  openSource({
    paper_id: d?.paper_id || item?.paper_id || '',
    block_id: d?.block_id || item?.block_id || '',
    page: d?.page ?? item?.page ?? null,
  });
}

/** 去设置页的「索引管理」建索引（深链到那一段）。 */
function goIndexSettings() {
  void router.push(openTab({ name: 'settings', hash: '#kb' }) as never);
}

/* ------------------------------ 载入 ------------------------------ */

async function refreshOverview(): Promise<void> {
  await withLoading('kb-overview', async () => {
    const data = await api.get<KnowledgeOverview>('/knowledge/overview');
    overview.value = data;
    // 默认挑一篇"已纳入知识库且有 chunk"的论文（浏览模式与"本论文/排除本篇"共用）
    const preferred = (data.items || []).find((p) => p.in_knowledge_base && p.chunks > 0)
      || (data.items || []).find((p) => p.chunks > 0);
    if (preferred) {
      if (!paperId.value) paperId.value = preferred.id;
      if (!bPaperId.value) bPaperId.value = preferred.id;
    }
  });
}

/** Esc 收起详情（去掉"关闭详情"按钮后的替代：键盘就能退回列表，不打断检索结果）。 */
function onKeydown(ev: KeyboardEvent): void {
  if (ev.key !== 'Escape' || !selected.value) return;
  const el = ev.target as HTMLElement | null;
  const tag = (el?.tagName || '').toLowerCase();
  if (tag === 'input' || tag === 'textarea' || el?.isContentEditable) return;   // 正在输入 → 不抢 Esc
  closeSelection();
}

onMounted(async () => {
  refreshHistory();
  unsubscribeHistory = subscribeHistory(refreshHistory);
  mq = window.matchMedia(NARROW_QUERY);
  narrow.value = mq.matches;
  mq.addEventListener('change', onMq);
  window.addEventListener('keydown', onKeydown);
  await refreshOverview();
  // 上次是浏览模式、筛选还在、但条目已经被清掉（例如中途重建过索引）→ 补一次；
  // 有数据就直接渲染 store 里的（切走再切回来不重复请求）
  if (mode.value === 'browse' && bPaperId.value && !chunks.value.length) await loadChunks();
});

onUnmounted(() => {
  unsubscribeHistory?.();
  unsubscribeHistory = null;
  mq?.removeEventListener('change', onMq);
  mq = null;
  window.removeEventListener('keydown', onKeydown);
});
</script>

<template>
  <div class="kb" :class="{ 'kb-compact': mode === 'search' && !libraryEmpty }">
    <!-- ============================ 搜索：紧凑头部 + 左右两栏 ============================ -->
    <template v-if="mode === 'search' && !libraryEmpty">
      <!-- 顶部：标题 + 库规模 + 搜索框 + 筛选行（不留大面积空白） -->
      <header class="kb-top">
        <div class="kb-topline">
          <h1 class="kb-title">知识库</h1>
          <span class="seg kb-modes" role="tablist" aria-label="知识库模式">
            <button :class="{ active: mode === 'search' }" @click="setMode('search')">搜索</button>
            <!-- 上面的 v-if 已把 mode 收窄成 'search'，这里用字符串比较，避免 TS 误判"比较无交集" -->
            <button :class="{ active: (mode as string) === 'browse' }" @click="setMode('browse')">浏览</button>
          </span>
          <span class="spacer"></span>
          <span class="tiny faint nowrap" :title="statusTitle">{{ statusText }}</span>
        </div>

        <div class="search-wrap">
          <div class="search-field kb-search">
            <AppIcon name="search" :size="16" />
            <input
              v-model="query"
              type="text"
              placeholder="搜索论文中的知识、概念或方法…"
              aria-label="检索知识库"
              @focus="histOpen = true"
              @click="histOpen = true"
              @blur="histOpen = false"
              @keydown="onSearchKey"
            >
            <button v-if="query" class="btn icon sm ghost" title="清空" @click="query = ''">
              <AppIcon name="close" :size="13" />
            </button>
          </div>

          <!-- 最近搜索：聚焦/点击即出，↑↓ 选、Enter 重跑、单条可删 -->
          <div class="hist" v-if="histOpen && shownHistory.length">
            <button
              v-for="(e, i) in shownHistory"
              :key="e.ts + e.q"
              type="button"
              class="hist-item"
              :class="{ active: i === histIndex }"
              @mousedown.prevent="applyHistory(e)"
            >
              <AppIcon name="search" :size="13" />
              <span class="truncate">{{ e.q }}</span>
              <span class="tiny faint nowrap">{{ entryScope(e) }}</span>
              <span class="tiny faint nowrap" v-if="e.hits !== null">{{ e.hits }} 条</span>
              <span
                class="hist-del"
                title="删除这条记录"
                @mousedown.prevent.stop="dropHistory(e)"
              ><AppIcon name="close" :size="12" /></span>
            </button>
            <div class="hist-foot">
              <span class="spacer"></span>
              <button type="button" class="btn sm ghost" @mousedown.prevent="clearAllHistory">清空记录</button>
            </div>
          </div>
        </div>

        <!-- 筛选行：范围 + 论文 + 内容类型 + 语言 + 跨文献开关（Enter 提交） -->
        <div class="kb-filters">
          <span class="seg" role="tablist" aria-label="检索范围">
            <button :class="{ active: scope === 'all' }" @click="scope = 'all'">全部</button>
            <button :class="{ active: scope === 'paper' }" @click="scope = 'paper'">本论文</button>
          </span>
          <select
            v-if="scope === 'paper' || excludeSelf"
            v-model="paperId"
            :title="scope === 'paper' ? '在选中的这篇论文里检索' : '检索时排除这篇论文'"
            class="kb-paper-select"
          >
            <option value="">选择论文…</option>
            <option v-for="p in papers" :key="p.id" :value="p.id">{{ p.title }}</option>
          </select>
          <label class="row tiny nowrap kb-type-select" title="只看某一类内容">
            <AppIcon name="filter" :size="12" />
            <select v-model="bType" aria-label="内容类型">
              <option value="">全部类型</option>
              <option v-for="t in CHUNK_TYPES" :key="t.key" :value="t.key">{{ t.label }}</option>
            </select>
          </label>
          <select v-model="bLang" class="kb-lang-select" aria-label="语言" title="只看中文行 / 只看原文行">
            <option value="">全部语言</option>
            <option v-for="l in LANG_OPTIONS" :key="l.key" :value="l.key">{{ l.label }}</option>
          </select>
          <label class="row tiny nowrap" v-if="scope === 'all'"
                 title="跨文献比对：库里别的论文里谁在做类似的事">
            <input type="checkbox" v-model="excludeSelf">排除所选论文
          </label>
          <span class="spacer"></span>
          <button class="btn sm" :disabled="busy" title="Ctrl/⌘ + Enter" @click="runSearch">
            <AppIcon name="search" :size="13" />{{ busy ? '检索中…' : '检索' }}
          </button>
        </div>
      </header>

      <!-- 结果计数（一行小字，不占版面） -->
      <div class="kb-resultline" v-if="result || error">
        <template v-if="result">
          <span v-if="hitCount">找到 <b>{{ hitCount }}</b> 个相关片段 · 来自 <b>{{ paperCount }}</b> 篇论文</span>
          <span v-else class="faint">没有找到相关内容</span>
          <span v-if="staleWhileBusy" class="tiny faint">
            <svg class="spinner kb-spin" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9" /></svg>
            检索中，下面是上一次的结果
          </span>
        </template>
        <span v-else class="small error-line">检索失败：{{ error }}</span>
      </div>

      <!-- 两栏：左 = 搜索结果的论文聚合；右 = 选中内容的详情（各自独立滚动） -->
      <div class="kb-cols" :class="{ narrow }">
        <!-- ------------------------------ 左栏：搜索结果 ------------------------------ -->
        <section ref="leftPane" class="kb-pane kb-left" aria-label="搜索结果">
          <div class="kb-left-inner">
            <!-- 加载中（且没有旧结果）：骨架，不白屏 -->
            <template v-if="busy && !result">
              <div class="sk-card" v-for="i in 3" :key="i">
                <div class="sk sk-title" style="width:62%"></div>
                <div class="sk sk-line" style="width:30%;margin-top:10px"></div>
                <div class="sk sk-line" style="width:92%;margin-top:8px"></div>
                <div class="sk sk-line" style="width:78%;margin-top:6px"></div>
              </div>
            </template>

            <!-- 有结果：按论文聚合 -->
            <template v-else-if="result && hitCount">
              <article
                v-for="group in paperResults"
                :key="group.paper_id"
                class="paper-card"
                :class="{ active: isSelected(group.paper_id, ''), 'has-current': selectedPaperId === group.paper_id }"
              >
                <!-- 一级信息：论文标题 + 相关片段数（二级） -->
                <button type="button" class="paper-head" @click="selectPaper(group)">
                  <span class="paper-title" :title="group.paper_title">{{ group.paper_title }}</span>
                  <span class="tiny faint nowrap">{{ group.total_chunks }} 个相关片段</span>
                </button>

                <!-- 二级结果：chunk 摘要（页码 · 章节 + 2~3 行 Markdown，公式渲染成 KaTeX，命中词高亮） -->
                <ul class="chunk-list">
                  <li v-for="c in shownChunks(group)" :key="c.chunk_id">
                    <div
                      class="chunk-item"
                      :class="{ active: isSelected(String(c.paper_id), String(c.chunk_id)) }"
                      role="button"
                      tabindex="0"
                      @click="selectChunk(c)"
                      @keydown.enter.prevent="selectChunk(c)"
                      @keydown.space.prevent="selectChunk(c)"
                    >
                      <!-- 插图 / 表格：先给一张缩略图（正文只有一句图注，看图更快） -->
                      <img v-if="hasThumb(c)" class="chunk-thumb" :src="c.image_url || ''"
                           loading="lazy" :alt="`${typeMeta(c.chunk_type).label}裁图`">
                      <div class="chunk-main">
                        <div class="chunk-meta">
                          <span class="chunk-page">p.{{ c.page }}</span>
                          <span v-if="c.section" class="chunk-sec" :title="c.section">{{ c.section }}</span>
                          <span class="chunk-type faint">{{ typeMeta(c.chunk_type).label }}·{{ langLabel(c) }}</span>
                          <span class="spacer"></span>
                          <!-- 摘要里也能直接拿走 LaTeX（公式条目才有） -->
                          <span
                            v-if="latexSource(c.snippet)"
                            class="chunk-copy"
                            role="button"
                            tabindex="0"
                            title="复制这条的 LaTeX"
                            @click.stop="copyToClipboard(latexSource(c.snippet), ' LaTeX')"
                            @keydown.enter.stop.prevent="copyToClipboard(latexSource(c.snippet), ' LaTeX')"
                          ><AppIcon name="copy" :size="12" /></span>
                        </div>
                        <div class="chunk-sum md" v-html="summaryHtml(c)"></div>
                      </div>
                    </div>
                  </li>
                </ul>

                <button
                  v-if="group.total_chunks > PAPER_PREVIEW_CHUNKS"
                  type="button"
                  class="paper-more"
                  @click="toggleExpand(group.paper_id)"
                >
                  {{ expanded.includes(group.paper_id) ? '收起片段' : `查看全部 ${group.total_chunks} 个片段` }}
                  <AppIcon :name="expanded.includes(group.paper_id) ? 'chevron-up' : 'chevron-down'" :size="12" />
                </button>
              </article>
            </template>

            <!-- 搜了但没结果 -->
            <div class="empty-state" v-else-if="result">
              <div class="es-icon"><AppIcon name="search" :size="20" /></div>
              <div class="es-title">没有找到相关内容</div>
              <div class="es-hint">
                试试：<br>
                · 使用更具体的关键词<br>
                · 使用论文中的术语（英文术语往往召回更准）<br>
                · 减少搜索条件（范围改回「全部」）
              </div>
            </div>

            <!-- 还没搜过 -->
            <div class="empty-state" v-else>
              <div class="es-icon"><AppIcon name="knowledge" :size="20" /></div>
              <div class="es-title">搜索论文中的知识、概念或方法</div>
              <div class="es-hint">输入关键词后回车。结果按论文聚合，点片段在右侧看完整内容。</div>
              <div class="es-actions" v-if="history.length">
                <button
                  v-for="e in history.slice(0, 4)"
                  :key="e.ts + e.q"
                  type="button"
                  class="btn sm ghost"
                  @click="applyHistory(e)"
                >
                  <AppIcon name="search" :size="12" />{{ e.q }}
                </button>
              </div>
            </div>
          </div>
        </section>

        <!-- ------------------------------ 右栏：详情 ------------------------------ -->
        <aside class="kb-pane kb-right" aria-label="选中内容详情">
          <!-- 没选中：占位 -->
          <div class="kb-placeholder" v-if="!selected || !detailItem">
            <AppIcon name="panel" :size="24" />
            <div class="es-title">{{ detailEmptyTitle }}</div>
            <div class="es-hint">在这里查看完整内容和来源</div>
          </div>

          <template v-else>
            <div class="kb-detail-body" :key="contentAnim">
              <h2 class="kb-detail-title">{{ sourceInfo.title }}</h2>
              <div class="kb-detail-meta">
                <span v-if="detailMeta" class="chunk-page">{{ detailMeta }}</span>
                <span class="badge dim">{{ sourceInfo.typeLabel }}</span>
                <span class="badge dim" v-if="sourceInfo.langLabel">{{ sourceInfo.langLabel }}</span>
                <span class="tiny faint" v-if="selected.kind === 'paper' && selectedGroup">
                  该论文命中 {{ selectedGroup.total_chunks }} 个片段，下面是其中最相关的一条
                </span>
              </div>

              <hr class="kb-hr">

              <!-- 1. 内容 -->
              <div class="kb-content-head">
                <span class="tiny faint">完整片段</span>
                <span class="spacer"></span>
                <button
                  v-if="copyText"
                  type="button"
                  class="btn sm ghost"
                  :title="detailIsFormula ? '复制这条公式的 LaTeX 源码' : '复制这条片段的原文'"
                  @click="copyCurrent"
                >
                  <AppIcon name="copy" :size="13" />{{ detailIsFormula ? '复制 LaTeX' : '复制正文' }}
                </button>
              </div>

              <!-- 公式条目：正文就是那条 LaTeX → 渲染成**真实文字**（可选、可复制），不是裁图 -->
              <div v-if="detailIsFormula && formulaHtml" class="kb-formula md" v-html="formulaHtml"></div>
              <div v-else-if="detailHtml" class="kb-content md" v-html="detailHtml"></div>
              <div class="kb-content-loading" v-else-if="detailBusy">
                <div class="sk sk-title" style="width:48%"></div>
                <div class="sk sk-line" style="width:94%;margin-top:12px"></div>
                <div class="sk sk-line" style="width:88%;margin-top:8px"></div>
                <div class="sk sk-line" style="width:91%;margin-top:8px"></div>
                <div class="sk sk-line" style="width:64%;margin-top:8px"></div>
              </div>
              <div class="empty" v-else>这条片段没有正文（可能是插图 / 表格，请看下面的裁图）。</div>

              <!-- 公式的 LaTeX 源：显示成等宽小字，方便直接划选拿走 -->
              <details class="kb-latex" v-if="detailIsFormula && formulaSource">
                <summary>LaTeX 源码</summary>
                <pre>{{ formulaSource }}</pre>
              </details>

              <!-- 裁图：公式已经在上面渲染成文字了就不再重复贴图；插图 / 表格仍然给图 -->
              <img v-if="!detailIsFormula && (detailData?.image_url || detailItem.image_url)"
                   class="kb-detail-img" :src="detailData?.image_url || detailItem.image_url || ''"
                   loading="lazy" :alt="`${sourceInfo.typeLabel}裁图`">

              <hr class="kb-hr">

              <!-- 2. 来源 -->
              <section class="kb-source">
                <h3 class="kb-sec-title">来源</h3>
                <dl class="kb-kv">
                  <div class="kb-kv-row"><dt>论文</dt><dd>{{ sourceInfo.title }}</dd></div>
                  <div class="kb-kv-row" v-if="sourceInfo.page"><dt>页码</dt><dd>第 {{ sourceInfo.page }} 页</dd></div>
                  <div class="kb-kv-row" v-if="sourceInfo.section"><dt>章节</dt><dd>{{ sourceInfo.section }}</dd></div>
                  <div class="kb-kv-row"><dt>内容类型</dt><dd>{{ sourceInfo.typeLabel }}{{ sourceInfo.langLabel ? ` · ${sourceInfo.langLabel}` : '' }}</dd></div>
                </dl>
                <button type="button" class="btn sm" @click="openCurrentSource">
                  <AppIcon name="locate" :size="13" />查看原文位置
                </button>
              </section>

              <hr class="kb-hr">

              <!-- 3. 检索详情：默认折叠（三级信息：分数 / 漏斗 / reranker） -->
              <section class="kb-retrieval">
                <button
                  type="button"
                  class="kb-disclosure"
                  :aria-expanded="metaOpen"
                  @click="metaOpen = !metaOpen"
                >
                  <AppIcon :name="metaOpen ? 'chevron-down' : 'chevron-right'" :size="13" />
                  检索详情
                  <span class="spacer"></span>
                  <span class="badge warn" v-if="rerankDegraded">重排已降级</span>
                </button>

                <div class="kb-meta-body" v-if="metaOpen">
                  <dl class="kb-kv mono">
                    <div class="kb-kv-row" v-for="row in detailScores" :key="row.k">
                      <dt>{{ row.k }}</dt><dd>{{ row.v }}</dd>
                    </div>
                  </dl>
                  <template v-if="detailRanks.length">
                    <div class="kb-meta-sub">召回名次</div>
                    <dl class="kb-kv mono">
                      <div class="kb-kv-row" v-for="row in detailRanks" :key="row.k">
                        <dt>{{ row.k }}</dt><dd>#{{ row.v }}</dd>
                      </div>
                    </dl>
                  </template>
                  <template v-if="funnelRows.length">
                    <div class="kb-meta-sub">本次检索</div>
                    <dl class="kb-kv mono">
                      <div class="kb-kv-row" v-for="row in funnelRows" :key="row.k">
                        <dt>{{ row.k }}</dt><dd>{{ row.v }}</dd>
                      </div>
                    </dl>
                  </template>
                  <dl class="kb-kv mono" v-if="detailData">
                    <div class="kb-kv-row" v-if="detailData.tokens">
                      <dt>Token 数</dt><dd>{{ detailData.tokens }}</dd>
                    </div>
                    <div class="kb-kv-row" v-if="detailData.chars">
                      <dt>字符数</dt><dd>{{ detailData.chars }}</dd>
                    </div>
                  </dl>
                  <p class="tiny faint kb-meta-note">RAG 调试信息，默认收起；检索链路本身未做任何改动。</p>
                </div>
              </section>
            </div>
          </template>
        </aside>
      </div>
    </template>

    <!-- ============================ 浏览 / 空库：沿用原布局 ============================ -->
    <template v-else>
      <div class="kb-status" v-if="!libraryEmpty">
        <span class="tiny faint nowrap" :title="statusTitle">{{ statusText }}</span>
      </div>

      <section class="hero" :class="{ compact: mode === 'browse' }">
        <h1 class="hero-title">知识库</h1>
        <div class="seg" role="tablist" aria-label="知识库模式">
          <button :class="{ active: mode === 'search' }" @click="setMode('search')">搜索</button>
          <button :class="{ active: mode === 'browse' }" @click="setMode('browse')">浏览</button>
        </div>

        <template v-if="mode === 'search'">
          <div class="search-wrap">
            <div class="search-field hero-search">
              <AppIcon name="search" :size="17" />
              <input
                v-model="query"
                type="text"
                placeholder="描述一段技术，或输入关键词…"
                aria-label="检索知识库"
                @focus="histOpen = true"
                @click="histOpen = true"
                @blur="histOpen = false"
                @keydown="onSearchKey"
              >
              <button v-if="query" class="btn icon sm ghost" title="清空" @click="query = ''">
                <AppIcon name="close" :size="13" />
              </button>
            </div>

            <div class="hist" v-if="histOpen && shownHistory.length">
              <button
                v-for="(e, i) in shownHistory"
                :key="e.ts + e.q"
                type="button"
                class="hist-item"
                :class="{ active: i === histIndex }"
                @mousedown.prevent="applyHistory(e)"
              >
                <AppIcon name="search" :size="13" />
                <span class="truncate">{{ e.q }}</span>
                <span class="tiny faint nowrap">{{ entryScope(e) }}</span>
                <span class="tiny faint nowrap" v-if="e.hits !== null">{{ e.hits }} 条</span>
                <span
                  class="hist-del"
                  title="删除这条记录"
                  @mousedown.prevent.stop="dropHistory(e)"
                ><AppIcon name="close" :size="12" /></span>
              </button>
              <div class="hist-foot">
                <span class="spacer"></span>
                <button type="button" class="btn sm ghost" @mousedown.prevent="clearAllHistory">清空记录</button>
              </div>
            </div>
          </div>
        </template>
      </section>

      <div class="kb-body">
        <div class="empty-state" v-if="libraryEmpty">
          <div class="es-title">知识库还是空的</div>
          <div class="es-actions">
            <button class="btn primary sm" @click="goIndexSettings">
              <AppIcon name="settings" :size="14" />去「设置 → 索引管理」建索引
            </button>
          </div>
        </div>

        <!-- ------------------------------ 浏览 ------------------------------ -->
        <template v-else>
          <div class="toolbar">
            <select v-model="bPaperId" @change="loadChunks" style="min-width:220px">
              <option value="">选择论文…</option>
              <option v-for="p in papers" :key="p.id" :value="p.id">{{ p.title }}</option>
            </select>
            <select v-model="bType" @change="loadChunks">
              <option value="">全部类型</option>
              <option v-for="t in CHUNK_TYPES" :key="t.key" :value="t.key">{{ t.label }}</option>
            </select>
            <select v-model="bLang" @change="loadChunks">
              <option value="">全部语言</option>
              <option v-for="l in LANG_OPTIONS" :key="l.key" :value="l.key">{{ l.label }}</option>
            </select>
          </div>

          <!-- 展开全文：只当前这一条（中英对照/完整阅读回 Reader） -->
          <section class="card detail" id="kb-detail" v-if="detailId">
            <div class="card-head">
              <span class="badge accent" v-if="detail">
                {{ typeMeta(detail.chunk_type).label }}·{{ langShort(detail.lang) }}
              </span>
              <span class="tiny faint truncate" v-if="detail">
                {{ detail.paper_title || detail.paper_id }} · p{{ detail.page }}
                <template v-if="detail.section"> · {{ detail.section }}</template>
                <template v-if="detail.tokens"> · {{ detail.tokens }} tok</template>
              </span>
              <span class="spacer"></span>
              <button class="btn sm" v-if="detail"
                      @click="openSource({ paper_id: detail.paper_id, block_id: detail.block_id, page: detail.page })">
                <AppIcon name="locate" :size="14" />原文
              </button>
              <button class="btn sm ghost" @click="closeDetail">
                <AppIcon name="close" :size="14" />收起
              </button>
            </div>
            <div class="card-body">
              <div class="empty" v-if="!detail">正在读取…</div>
              <template v-else>
                <img v-if="detail.image_url" class="detail-img" :src="detail.image_url"
                     :alt="`${typeMeta(detail.chunk_type).label}裁图`">
                <div class="kb-content md" v-html="mdToHtml(detail.content, detail.paper_id)"></div>
              </template>
            </div>
          </section>

          <!-- 图库网格：选「插图」时按图翻 -->
          <div class="gallery" v-if="chunks.length && isGallery">
            <figure class="fig-card" v-for="c in chunks" :key="c.id" :class="{ active: detailId === c.id }">
              <button type="button" class="fig-thumb" :title="c.caption || c.preview"
                      @click="toggleDetail(c.id)">
                <img v-if="c.image_url" :src="c.image_url" loading="lazy"
                     :alt="`插图 p${c.page}`">
                <span v-else class="tiny faint">无裁图</span>
              </button>
              <figcaption>
                <div class="tiny faint">
                  p{{ c.page }}<template v-if="c.section"> · {{ c.section }}</template> · {{ langShort(c.lang) }}
                </div>
                <div class="fig-cap">{{ c.caption || c.preview }}</div>
                <div class="row nowrap fig-ops">
                  <button class="btn sm" @click="toggleDetail(c.id)">
                    <AppIcon name="document" :size="13" />{{ detailId === c.id ? '收起' : '展开全文' }}
                  </button>
                  <button class="btn sm ghost" title="回阅读器定位到这一段" @click="openSource(c)">
                    <AppIcon name="locate" :size="13" />原文
                  </button>
                </div>
              </figcaption>
            </figure>
          </div>

          <!-- 紧凑列表：正文 / 公式 / 表格 -->
          <section class="chunks" v-else-if="chunks.length">
            <div v-for="c in chunks" :key="c.id" class="chunk-row" :class="{ active: detailId === c.id }">
              <img v-if="c.image_url" class="thumb sm" :src="c.image_url" loading="lazy"
                   :alt="`${typeMeta(c.chunk_type).label}裁图`">
              <div class="hit-main">
                <div class="hit-head">
                  <span class="badge accent">{{ typeMeta(c.chunk_type).label }}·{{ langShort(c.lang) }}</span>
                  <span class="badge">p{{ c.page }}</span>
                  <span class="hit-sec" v-if="c.section" :title="c.section">{{ c.section }}</span>
                  <span class="spacer"></span>
                  <button class="btn sm ghost" @click="toggleDetail(c.id)">
                    <AppIcon name="document" :size="13" />{{ detailId === c.id ? '收起' : '展开全文' }}
                  </button>
                  <button class="btn sm ghost" title="回阅读器定位到这一段" @click="openSource(c)">
                    <AppIcon name="locate" :size="13" />原文
                  </button>
                </div>
                <div class="hit-text md" v-html="summaryHtml(c)"></div>
              </div>
            </div>
          </section>

          <div class="empty" v-else-if="state.loading['kb-chunks']">正在读取…</div>
          <div class="empty" v-else-if="!bPaperId">选一篇论文</div>
          <div class="empty" v-else>没有符合条件的 chunk</div>
        </template>
      </div>
    </template>
  </div>
</template>

<style scoped>
/* ==========================================================================
   骨架
   ========================================================================== */
.kb { max-width: 880px; margin: 0 auto; }
/* 搜索模式：占满宽度做左右两栏（否则 880px 里塞两栏，右栏连一行正文都放不下） */
.kb.kb-compact { max-width: none; }
.kb-status { display: flex; justify-content: flex-end; }

/* ==========================================================================
   顶部：标题 / 库规模 / 搜索框 / 筛选行（紧凑，不留大面积空白）
   ========================================================================== */
.kb-top { display: flex; flex-direction: column; gap: 10px; }
.kb-topline { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.kb-title { margin: 0; font-size: 19px; color: var(--text); letter-spacing: -.01em; }
.kb-modes { height: 28px; }
.kb-modes button { height: 22px; font-size: 12px; padding: 0 10px; }

.kb-search { width: min(560px, 100%); height: 36px; border-radius: var(--radius-ctl); }
.kb-search input { flex: 1; width: auto; min-width: 0; font-size: 13.5px; }

.kb-filters { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.kb-filters .seg { height: 28px; }
.kb-filters .seg button { height: 22px; font-size: 12px; padding: 0 11px; }
.kb-paper-select { min-width: 200px; max-width: 280px; height: 28px; font-size: 12px; }
.kb-type-select, .kb-lang-select { height: 28px; }
.kb-type-select { gap: 5px; color: var(--text-faint); }
.kb-type-select select, .kb-lang-select { height: 28px; font-size: 12px; }
.kb-type-select select { max-width: 132px; }
.kb-lang-select { max-width: 108px; }

.kb-resultline {
  display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
  margin: 14px 0 8px;
  font-size: 12px; color: var(--text-dim);
}
.kb-resultline b { font-weight: 600; color: var(--text); font-variant-numeric: tabular-nums; }
.kb-spin { display: inline-block; width: 12px; height: 12px; vertical-align: -2px; margin-right: 4px; }
.error-line { color: var(--danger); }

/* 最近搜索：浮在搜索框下方（不推挤布局），键盘与鼠标都能用 */
.search-wrap { position: relative; width: min(620px, 100%); }
.search-field.hero-search { display: flex; width: 100%; height: 46px; padding: 0 16px; }
.search-field.hero-search .icon { color: var(--text-dim); }
.search-field.hero-search input { flex: 1; width: auto; min-width: 0; font-size: 14.5px; }
.hist {
  position: absolute; top: calc(100% + 6px); left: 0; right: 0; z-index: 30;
  padding: 5px; display: flex; flex-direction: column; gap: 1px;
  background: var(--bg-elev); border: 1px solid var(--border-strong);
  border-radius: var(--radius); box-shadow: var(--shadow-flyout);
}
.hist-item {
  display: flex; align-items: center; gap: 8px; width: 100%;
  padding: 7px 10px; border: 0; border-radius: var(--radius-ctl);
  background: transparent; color: var(--text); cursor: pointer;
  font-family: inherit; font-size: 12.5px; text-align: left;
}
.hist-item .icon { color: var(--text-faint); }
.hist-item:hover, .hist-item.active { background: var(--bg-hover); }
.hist-del {
  display: inline-flex; align-items: center; justify-content: center;
  width: 18px; height: 18px; border-radius: var(--r-xs); color: var(--text-faint); flex: none;
}
.hist-del:hover { background: var(--bg-pressed); color: var(--danger); }
.hist-foot {
  display: flex; align-items: center; gap: 8px;
  padding: 4px 6px 2px; border-top: 1px solid var(--divider); margin-top: 3px;
}

/* ==========================================================================
   两栏：左结果 / 右详情。桌面固定高度 → 两栏各自独立滚动；
   窄屏（≤900px）退化成上下堆叠，走页面滚动。
   ========================================================================== */
.kb-cols {
  display: grid; gap: 14px;
  grid-template-columns: minmax(0, 45fr) minmax(0, 55fr);
  align-items: stretch;
  /* 两栏铺满整页：至少占满"视口 - 顶栏 - 本页头部与计数行"，内容更长时随内容一起长高
     （不再内部裁切 → 左右两块圆角矩形永远等高、且到底） */
  min-height: calc(100vh - var(--header-h) - 176px);
}
.kb-cols.narrow {
  grid-template-columns: minmax(0, 1fr);
  height: auto; min-height: 0;
}
.kb-pane {
  min-width: 0; min-height: 0;
  background: var(--bg-elev);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  overflow: auto;
}
.kb-cols.narrow .kb-pane { max-height: none; overflow: visible; }
.kb-left-inner { padding: 10px; display: flex; flex-direction: column; gap: 10px; }

/* ==========================================================================
   左栏：论文卡片（一级：论文；二级：chunk 摘要）
   ========================================================================== */
.paper-card {
  border: 1px solid var(--border); border-radius: var(--r-sm);
  background: var(--bg-layer);
  padding: 10px 12px;
  transition: border-color var(--dur) var(--ease), background var(--dur) var(--ease);
}
.paper-card.active { border-color: var(--accent); box-shadow: var(--ring); }
.paper-card.has-current:not(.active) { border-color: color-mix(in srgb, var(--accent) 30%, var(--border)); }

.paper-head {
  display: flex; align-items: baseline; gap: 8px; width: 100%;
  text-align: left; cursor: pointer; margin-bottom: 4px;
}
.paper-title {
  font-size: 13.5px; font-weight: 600; color: var(--accent-text);
  min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.paper-head:hover .paper-title { text-decoration: underline; }

.chunk-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; }
.chunk-item {
  display: flex; align-items: flex-start; gap: 8px;
  width: 100%; text-align: left; cursor: pointer;
  padding: 7px 8px; border-radius: var(--r-xs);
  border-left: 2px solid transparent;
  transition: background var(--dur-fast) var(--ease), border-color var(--dur-fast) var(--ease);
}
.chunk-item + .chunk-item { border-top: 1px solid var(--divider); }
.chunk-item:hover { background: var(--bg-hover); }
.chunk-item.active {
  background: var(--accent-soft);
  border-left-color: var(--accent);
}
/* 摘要要能自己收窄（`min-width:0` + `overflow-wrap`），否则长公式会把整行撑破 */
.chunk-main { flex: 1; min-width: 0; }

/* 插图 / 表格的缩略图：固定小尺寸、裁切铺满，别让不同长宽比的裁图把行高撑得参差 */
.chunk-thumb {
  flex: none; width: 76px; height: 56px; object-fit: cover;
  border: 1px solid var(--divider); border-radius: var(--r-xs);
  background: var(--bg-soft);
}

.chunk-meta { display: flex; align-items: center; gap: 7px; flex-wrap: wrap; }
.chunk-page {
  font-size: 11px; font-weight: 600; color: var(--accent-text);
  font-variant-numeric: tabular-nums;
}
.chunk-sec {
  font-size: 11px; color: var(--text-dim); text-transform: none;
  max-width: 190px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.chunk-type { font-size: 11px; }

/* 摘要右侧的「复制 LaTeX」：默认很淡，hover 那一行才明显（不抢正文） */
.chunk-copy {
  display: inline-flex; align-items: center; justify-content: center;
  width: 20px; height: 20px; flex: none;
  border-radius: var(--r-xs); color: var(--text-faint); cursor: pointer;
  opacity: 0; transition: opacity var(--dur-fast) var(--ease), background var(--dur-fast) var(--ease);
}
.chunk-item:hover .chunk-copy, .chunk-item:focus-within .chunk-copy { opacity: 1; }
.chunk-copy:hover { background: var(--bg-pressed); color: var(--accent-text); }

/* 摘要正文：**完整 Markdown**（公式是 KaTeX、粗体/列表/代码块照常）+ 2~3 行截断 */
.chunk-sum {
  display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical;
  overflow: hidden;
  margin-top: 3px;
  font-size: 12px; line-height: 1.5; color: var(--text-dim);
  overflow-wrap: anywhere;
}
.chunk-sum :deep(.katex) { font-size: .95em; }
.chunk-sum :deep(p) { margin: 0; }
.chunk-sum :deep(ul), .chunk-sum :deep(ol) { margin: .1em 0; padding-left: 1.2em; }
.chunk-sum :deep(blockquote) { margin: .2em 0; padding-left: .6em; }
.chunk-sum :deep(pre) { margin: .2em 0; padding: 4px 6px; }
.chunk-sum :deep(h1), .chunk-sum :deep(h2), .chunk-sum :deep(h3), .chunk-sum :deep(h4) {
  display: inline; font-size: 1em; margin: 0;
}
.chunk-sum :deep(table) { font-size: 11px; }
/* 命中词：低饱和强调底色，不抢正文的视觉权重 */
.chunk-sum :deep(.kb-mark) {
  background: color-mix(in srgb, var(--accent) 22%, transparent);
  color: var(--text); border-radius: 3px; padding: 0 1px;
}

/* 行内公式夹在截断的摘要里时可能被裁得只剩半行，给它留一点竖直空间 */
.chunk-sum :deep(.katex-display) { margin: .2em 0; }

.paper-more {
  display: inline-flex; align-items: center; gap: 4px;
  margin-top: 6px; padding: 2px 0;
  font-size: 11.5px; color: var(--text-dim); cursor: pointer;
}
.paper-more:hover { color: var(--accent-text); }

/* 骨架（加载中） */
.sk-card {
  border: 1px solid var(--border); border-radius: var(--r-sm);
  background: var(--bg-layer); padding: 12px;
}

/* ==========================================================================
   右栏：详情面板
   ========================================================================== */
.kb-placeholder {
  display: flex; flex-direction: column; align-items: center; justify-content: center;
  gap: 8px; height: 100%; min-height: 240px; padding: 30px 20px;
  text-align: center; color: var(--text-faint);
}
.kb-placeholder .es-title { color: var(--text-dim); }

.kb-detail-head {
  position: sticky; top: 0; z-index: 2;
  display: flex; align-items: center; gap: 8px;
  padding: 8px 12px;
  background: var(--bg-elev);
  border-bottom: 1px solid var(--divider);
}
.kb-detail-body { padding: 14px 18px 22px; animation: kb-swap var(--dur-slow) var(--ease); }
@keyframes kb-swap {
  from { opacity: 0; transform: translateY(4px); }
  to { opacity: 1; transform: none; }
}
.kb-detail-title {
  margin: 0 0 6px; font-size: 17px; line-height: 1.35; color: var(--text);
  overflow-wrap: anywhere;
}
.kb-detail-meta { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.kb-hr { height: 1px; border: 0; background: var(--divider); margin: 14px 0; }

.kb-content {
  font-size: 13.5px; line-height: 1.72; color: var(--text);
  overflow-wrap: anywhere;
}
.kb-content :deep(img) { max-width: 100%; height: auto; }
.kb-content :deep(.katex-display) { margin: .5em 0; overflow-x: auto; overflow-y: hidden; }
.kb-content-loading { padding: 4px 0; }

/* 内容区小标题行（左"完整片段" / 右复制按钮） */
.kb-content-head { display: flex; align-items: center; gap: 8px; margin-bottom: 6px; }

/* 公式 chunk：渲染成真实文字（KaTeX），可划选、可复制。
 * ⚠️ 不要写 `text-align: center`：KaTeX 的 display 公式自带居中，再叠一层会让短公式
 * 飘到整宽的中间（实测很难看）。这里左对齐 + 从行首读起，超长时横向滚动。 */
.kb-formula {
  padding: 12px 14px;
  background: var(--bg-sunken);
  border: 1px solid var(--border);
  border-radius: var(--r-sm);
  overflow-x: auto; overflow-y: hidden;
  text-align: left;
  font-size: 15px; line-height: 1.8; color: var(--text);
}
.kb-formula :deep(.katex-display) { margin: 0; text-align: left; }
.kb-formula :deep(.katex-display > .katex) { text-align: left; }
.kb-formula :deep(.katex) { font-size: 1.06em; }
/* 复制/划选时给公式底色，让"这是文字不是图"一眼可见 */
.kb-formula ::selection { background: var(--selection-bg); }

/* LaTeX 源码（默认折叠，点开是等宽块，可整段复制） */
.kb-latex { margin-top: 8px; }
.kb-latex > summary {
  cursor: pointer; font-size: 11.5px; color: var(--text-faint);
  list-style: none; display: inline-flex; align-items: center; gap: 5px;
}
.kb-latex > summary::-webkit-details-marker { display: none; }
.kb-latex > summary::before {
  content: ""; width: 0; height: 0; flex: none;
  border-left: 5px solid currentColor; border-top: 4px solid transparent; border-bottom: 4px solid transparent;
  transition: transform var(--dur-fast) var(--ease);
}
.kb-latex[open] > summary::before { transform: rotate(90deg); }
.kb-latex > summary:hover { color: var(--accent-text); }
.kb-latex > pre {
  margin: 6px 0 0; padding: 8px 10px;
  background: var(--bg-sunken); border: 1px solid var(--border); border-radius: var(--r-sm);
  font-family: var(--mono); font-size: 12px; line-height: 1.6;
  white-space: pre-wrap; word-break: break-word; color: var(--text-dim);
  max-height: 260px; overflow: auto;
}

.kb-detail-img {
  display: block; max-width: 100%; max-height: 380px; object-fit: contain;
  margin: 14px auto 0; background: var(--bg-sunken);
  border: 1px solid var(--border); border-radius: var(--r-sm);
}

.kb-sec-title {
  margin: 0 0 8px; font-size: 12px; font-weight: 600; letter-spacing: .02em;
  color: var(--text-dim);
}
.kb-source { display: flex; flex-direction: column; align-items: flex-start; gap: 10px; }

/* 键值表：标签弱化、值正常（metadata 不与正文同权重） */
.kb-kv { margin: 0; display: flex; flex-direction: column; gap: 4px; width: 100%; }
.kb-kv-row { display: flex; gap: 10px; align-items: baseline; }
.kb-kv-row dt {
  flex: none; width: 92px; font-size: 11.5px; color: var(--text-faint);
}
.kb-kv-row dd { margin: 0; min-width: 0; font-size: 12.5px; color: var(--text-dim); overflow-wrap: anywhere; }

.kb-disclosure {
  display: flex; align-items: center; gap: 6px; width: 100%;
  padding: 4px 0; cursor: pointer;
  font-size: 12.5px; font-weight: 600; color: var(--text-dim);
}
.kb-disclosure:hover { color: var(--text); }
.kb-disclosure .icon { color: var(--text-faint); }
.kb-meta-body { padding: 10px 0 0; display: flex; flex-direction: column; gap: 10px; }
.kb-meta-sub { font-size: 11.5px; color: var(--text-faint); margin-bottom: -4px; }
.kb-meta-note { margin: 0; line-height: 1.5; }

/* ==========================================================================
   浏览模式（沿用原布局）
   ========================================================================== */
.hero {
  display: flex; flex-direction: column; align-items: center; gap: 12px;
  padding: 40px 0 4px;
}
.hero.compact { padding: 6px 0 4px; }
.hero-title { margin: 0; font-size: 22px; color: var(--accent-text); }
.kb-body { margin-top: 26px; }

.result-head {
  font-size: 11.5px; color: var(--text-faint); font-variant-numeric: tabular-nums;
  margin-bottom: 2px;
}

.hit-main { min-width: 0; flex: 1; }
.hit-head { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.hit-sec { font-size: 11px; color: var(--text-faint); max-width: 200px; overflow: hidden;
  text-overflow: ellipsis; white-space: nowrap; }
/* 浏览列表里的摘要同样走 Markdown/KaTeX，只按 2 行截断 */
.hit-text { margin-top: 2px; font-size: 12px; color: var(--text-dim); line-height: 1.55;
  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;
  overflow-wrap: anywhere; }
.hit-text :deep(p) { margin: 0; }
.hit-text :deep(h1), .hit-text :deep(h2), .hit-text :deep(h3), .hit-text :deep(h4) {
  display: inline; font-size: 1em; margin: 0;
}
.hit-text :deep(ul), .hit-text :deep(ol) { margin: .1em 0; padding-left: 1.2em; }
.hit-text :deep(.katex) { font-size: .95em; }

/* 插图 / 表格 / 公式的缩略图（拿不到裁图就不显示，不留空框） */
.thumb {
  flex: none; width: 112px; height: 84px; object-fit: contain;
  background: var(--bg-sunken); border: 1px solid var(--border); border-radius: var(--r-sm);
}
.thumb.sm { width: 88px; height: 66px; }

/* ------------------------------ 图库网格 ------------------------------ */
.gallery {
  display: grid; gap: 14px;
  grid-template-columns: repeat(auto-fill, minmax(190px, 1fr));
}
.fig-card { margin: 0; display: flex; flex-direction: column; gap: 6px; min-width: 0; }
.fig-card.active .fig-thumb { border-color: var(--accent); box-shadow: var(--ring); }
.fig-thumb {
  display: flex; align-items: center; justify-content: center;
  width: 100%; aspect-ratio: 4 / 3; padding: 4px;
  background: var(--bg-sunken); border: 1px solid var(--border);
  border-radius: var(--radius); cursor: pointer; overflow: hidden;
}
.fig-thumb img { max-width: 100%; max-height: 100%; object-fit: contain; }
.fig-thumb:hover { border-color: color-mix(in srgb, var(--accent) 34%, var(--border)); }
.fig-cap {
  font-size: 11.5px; color: var(--text-dim); line-height: 1.45;
  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;
}
.fig-ops { gap: 6px; margin-top: 2px; }

/* ------------------------------ 紧凑列表（浏览） ------------------------------ */
.chunk-row {
  display: flex; align-items: flex-start; gap: 10px;
  padding: 10px 0; border-top: 1px solid var(--divider);
}
.chunk-row:first-child { border-top: 0; }

/* ------------------------------ 展开全文（浏览） ------------------------------ */
.detail { scroll-margin-top: calc(var(--header-h) + 12px); }
.detail-img {
  display: block; max-width: 100%; max-height: 360px; object-fit: contain;
  margin: 0 auto 12px; background: var(--bg-sunken);
  border: 1px solid var(--border); border-radius: var(--radius);
}

/* 窄屏：搜索行与筛选行不再左对齐挤压 */
@media (max-width: 900px) {
  .kb-cols { grid-template-columns: minmax(0, 1fr); }
  .kb-pane { overflow: visible; }
  .kb-detail-head { position: static; }
}
</style>

