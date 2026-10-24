<script setup lang="ts">
/**
 * 论文库：导入 → 解析 → 翻译 → 三视图阅读。
 *
 * 版式（移植自参照项目 retain-pdf 的书库页）：**顶部一行**（左 标题 + 计数 + 当前分类 chip；中 搜索；
 * 右 导入 / 刷新 ｜ 批量 / 筛选 / 排序 / 视图切换）+ **封面卡片网格**（列表视图可切成一行一条）。
 * 批量模式时这一行下面再出现一条批量工具条；它们都随页面滚动，没有浮在视口上的东西。
 * 卡片本体在 `components/paper/PaperCard.vue`，点卡片打开的详情在 `PaperDetailModal.vue`。
 *
 * REST 走 `@/api/client`，列表与计数走全局 store，弹窗走 `Modal`。
 * 删除语义：软删除会清理索引、知识库记录，以及**这篇论文的用户内容**
 * （对话历史 / 划词解释 / 高亮 / 笔记），PDF 文件移入 data/trash（不物理删除）。
 */
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { api, assetUrl } from '@/api/client';
import type {
  MineruConfig, MineruModelVersion, Paper, ParseConfig, ParseEngine, TaskInfo,
} from '@/api/types';
import Modal from '@/components/Modal.vue';
import ConfirmModal from '@/components/ConfirmModal.vue';
import Switch from '@/components/Switch.vue';
import AppIcon from '@/components/AppIcon.vue';
import PaperCard from '@/components/paper/PaperCard.vue';
import PaperDetailModal from '@/components/paper/PaperDetailModal.vue';
import { debounce } from '@/utils/format';
import {
  loadPapers, reportError, state, toast, withLoading,
} from '@/stores/app';
import { assignFolder, createFolder, folderName, folderState, loadFolders } from '@/stores/folders';
import { openTab } from '@/stores/tabs';
import { fmtRelative } from '@/utils/format';
const router = useRouter();
const route = useRoute();

/** 打开论文 = **开一个新标签页**（`openTab` 负责登记，重复点同一篇只会激活已有标签）。 */
function openPaper(id: string) {
  void router.push(openTab({ name: 'reader', params: { paperId: id } }) as never);
}

/** 详情弹窗当前看的那篇（空串 = 关着）。点卡片本体进这里，点「阅读」直接进阅读器。 */
const detailId = ref('');

/* ------------------------------ 筛选与列表 ------------------------------ */

const keyword = ref('');

/* 本机记忆（页面设定）：视图形态 / 排序 / 最后一次分类。关键词**故意不记**（搜索词是一次性的），
 * 批量模式与分组折叠同理 —— 只有「页面设定」该被记住；localStorage 一律 try/catch，
 * 隐私模式读不到就当「没有记忆」，不许因此把页面拖崩。 */
const VIEW_KEY = 'pm.library.view';
const SORT_KEY = 'pm.library.sort';
const FOLDER_KEY = 'pm.library.folder';

function readPref(key: string): string {
  try { return localStorage.getItem(key) ?? ''; } catch { return ''; }
}

/** 写记忆：空串 = 没有记忆（顺手 removeItem，别在 storage 里留一排空值）。 */
function writePref(key: string, value: string): void {
  try {
    if (value) localStorage.setItem(key, value);
    else localStorage.removeItem(key);
  } catch { /* 隐私模式：存不下也不影响本次会话 */ }
}

/** 排序：把「字段 + 方向」合并成一个下拉，页面上少一个冗余控件。 */
const SORT_OPTIONS = [
  { value: 'last_read_at:desc', label: '最近阅读优先' },
  { value: 'folder:asc', label: '按分类' },
  { value: 'created_at:desc', label: '最新导入' },
  { value: 'created_at:asc', label: '最早导入' },
  { value: 'title:asc', label: '标题 A→Z' },
] as const;

/** 记忆值可能是老版本 / 手改过的脏数据：不在 SORT_OPTIONS 里就落回默认。 */
const DEFAULT_SORT = 'last_read_at:desc';
const SORT_VALUES = SORT_OPTIONS.map((o) => o.value) as readonly string[];
const rememberedSort = readPref(SORT_KEY);
const initialSort = SORT_VALUES.includes(rememberedSort) ? rememberedSort : DEFAULT_SORT;
const sort = ref(initialSort.split(':')[0] || 'last_read_at');
const order = ref(initialSort.split(':')[1] === 'asc' ? 'asc' : 'desc');

/** 正在进行的行内操作键：`import` / `tr:<id>` / `del:<id>`。 */
const busyKey = ref('');
/** 列表加载失败原因（非空时界面显式给出错误卡片 + 重试）。 */
const loadError = ref('');

const sortKey = computed({
  get: () => `${sort.value}:${order.value}`,
  set: (v: string) => {
    const [s, o] = v.split(':');
    sort.value = s || 'last_read_at';
    order.value = o === 'asc' ? 'asc' : 'desc';
    writePref(SORT_KEY, sortKey.value);      // 排序是「页面设定」，用户一改就写回
  },
});

const filtered = computed<Paper[]>(() => state.papers);
const loading = computed(() => !!state.loading.papers);

/* ------------------------------ 视图形态 ------------------------------
 * 封面网格 = 参照项目图1 的书库版式（一眼看到论文长什么样）；列表 = 一行一条（扫标题快）。
 * 形态记在本机：同一个人不该每次进来都重选一遍。
 */

const viewMode = ref<'grid' | 'list'>(readPref(VIEW_KEY) === 'list' ? 'list' : 'grid');

function setView(v: 'grid' | 'list') {
  viewMode.value = v;
  writePref(VIEW_KEY, v);
}

/* ------------------------------ 筛选 ------------------------------
 * 筛选只有一个维度（分类），且走**后端筛**（`refresh()` 带上 folder 参数）——
 * 后端能为分类做索引查询，前端不必再收一份候选、再筛一遍。
 */

const filterOpen = ref(false);
const filterBox = ref<HTMLElement | null>(null);

/* 分类是库页的一等维度：'' = 全部，'none' = 未分类，其余 = 分类 id。
 * 它记在**路由 query**（?folder=…）上，所以刷新、分享、从侧栏进来都还是同一个分类；
 * 没有 query 时才用本机记忆（FOLDER_KEY）—— URL 优先，分享 / 深链永远复现对方看到的那一屏。 */
const folderFilter = ref('');
const folderFilterLabel = computed(() => (
  folderFilter.value === 'none' ? '未分类' : folderName(folderFilter.value)
));

/** 把分类写回 query；用 replace，切分类不该往后退历史里堆记录。 */
function syncFolderQuery(v: string) {
  if ((route.query.folder ?? '') === v) return;
  const query: Record<string, string> = {};
  for (const [k, val] of Object.entries(route.query)) if (typeof val === 'string') query[k] = val;
  if (v) query.folder = v; else delete query.folder;
  void router.replace({ query });
}

function setFolder(v: string) {
  if (folderFilter.value === v) return;
  folderFilter.value = v;
  syncFolderQuery(v);
  writePref(FOLDER_KEY, v);      // 切回「全部」时 v 是空串 = 把记忆删掉，而不是记一个空值
  void refresh();
}

/** 地址栏变了（前进/后退、外部链接）也要跟上，否则 url 与列表会脱节。 */
watch(() => route.query.folder, (q) => {
  const v = typeof q === 'string' ? q : '';
  if (v === folderFilter.value) return;
  folderFilter.value = v;
  writePref(FOLDER_KEY, v);      // 地址栏变了 = 用户换过一次分类（前进/后退、外部链接）
  void refresh();
});

/* 侧栏在同一页里删掉当前分类（或把最后一篇未分类论文归类 / 删掉）时也回落到「全部」，
 * 别留一个指向已不存在分类的空列表。盯 items/unclassified/loading/error：拉失败不等于分类被删
 * （error 非空时一律不回落）。 */
watch(() => [
  folderState.items.length, folderState.unclassified, folderState.loading, folderState.error,
] as const, () => {
  const v = folderFilter.value;
  if (!v || folderState.loading || folderState.error) return;
  // 'none' 不是分类项：未分类归零后侧栏与筛选里那一行都没了，筛选不能还停在它上面
  if (v === 'none') {
    if (folderState.unclassified > 0) return;
  } else if (folderState.items.some((f) => f.id === v)) return;
  folderFilter.value = '';
  syncFolderQuery('');
  writePref(FOLDER_KEY, '');     // 记忆里那个分类已经不存在了：连记忆一起清，别下次又把人带回空列表
  void refresh();
});

const filterCount = computed(() => (folderFilter.value ? 1 : 0));

/** 界面真正渲染的那一份：关键词 / 分类 / 排序都由后端做完了，前端不再二次过滤。 */
const visible = computed<Paper[]>(() => filtered.value);

interface PaperSegment {
  key: string;
  /** 分组标题文案（空串 = 这一段不带标题，即非分组档的平铺列表） */
  name: string;
  items: Paper[];
}

/** 分组只服务于「按分类」这一档排序：其它排序**完全不走**这里（直接返回空数组）。
 *  切段只合并「连续的同一个 folder_id」—— 顺序就是后端给的顺序（分类按 sort/name、未分类钉在
 *  最后、组内最近阅读优先），前端**不重排**，否则前后端会各说一套顺序。 */
const folderGroups = computed<PaperSegment[]>(() => {
  if (sortKey.value !== 'folder:asc') return [];
  const out: PaperSegment[] = [];
  for (const p of visible.value) {
    const key = p.folder_id || 'none';
    const last = out[out.length - 1];
    if (last && last.key === key) last.items.push(p);
    else out.push({ key, name: key === 'none' ? '未分类' : folderName(key), items: [p] });
  }
  return out;
});

/** 渲染用的分段：分组档 = 每个分类一段；其它档 = 唯一一段且没有标题。
 *  这样卡片模板只写一份，平铺与分组共用同一套 DOM，不会出现两份会走样的卡片树。 */
const segments = computed<PaperSegment[]>(() => (
  sortKey.value === 'folder:asc'
    ? folderGroups.value
    : [{ key: '__all__', name: '', items: visible.value }]
));

/** 已收起的段（空数组 = 全部展开，默认）。**有意不持久化**：刷新 / 重进库页一律回到全展开 ——
 *  记住上次折了哪几组只会让人以为论文丢了，列表的折叠态也不该多进一条 localStorage。 */
const collapsedGroups = ref<string[]>([]);

function isGroupOpen(key: string): boolean {
  return !collapsedGroups.value.includes(key);
}

function toggleGroup(key: string): void {
  collapsedGroups.value = collapsedGroups.value.includes(key)
    ? collapsedGroups.value.filter((k) => k !== key)
    : [...collapsedGroups.value, key];
}
function clearFilters() {
  folderFilter.value = '';
  syncFolderQuery('');
  writePref(FOLDER_KEY, '');      // 清筛选 = 回到「全部」，记忆跟着清
  void refresh();
}

/** 浮层：点外面 / 按 Esc 收起（与 Menu 的关闭口径一致）。两个浮层各判各的，互不牵连。 */
function onDocPointer(ev: PointerEvent) {
  const target = ev.target as Node;
  if (filterOpen.value && !filterBox.value?.contains(target)) filterOpen.value = false;
  if (moveOpen.value && !moveBox.value?.contains(target)) moveOpen.value = false;
}

function onDocKey(ev: KeyboardEvent) {
  if (ev.key !== 'Escape') return;
  filterOpen.value = false;
  moveOpen.value = false;
}

/* ------------------------------ 批量选择 ------------------------------ */

const batchMode = ref(false);
const selectedIds = ref<string[]>([]);
const batchBusy = ref(false);
const selectedCount = computed(() => selectedIds.value.length);
const allSelected = computed(() => (
  visible.value.length > 0 && visible.value.every((p) => selectedIds.value.includes(p.id))
));

function toggleBatch() {
  batchMode.value = !batchMode.value;
  if (!batchMode.value) selectedIds.value = [];     // 退出即清空：不留下"看不见的选中"
}

function toggleSelect(id: string) {
  selectedIds.value = selectedIds.value.includes(id)
    ? selectedIds.value.filter((x) => x !== id)
    : [...selectedIds.value, id];
}

function selectAll() {
  selectedIds.value = allSelected.value ? [] : visible.value.map((p) => p.id);
}

/* ------------------------------ 移到分类 ------------------------------
 * 卡片菜单与批量条走同一个浮层，免得单篇/批量各写一套分类选择。 */

const moveOpen = ref(false);
const moveIds = ref<string[]>([]);
const moveBox = ref<HTMLElement | null>(null);
/** fixed 定位坐标：浮层贴在触发它的按钮旁边，所以不能用普通文档流定位。 */
const movePos = ref({ top: 0, left: 0 });
const moveBusy = ref(false);
const newFolderOpen = ref(false);
const newFolderName = ref('');
const newFolderInput = ref<HTMLInputElement | null>(null);

const MOVE_W = 240;

/** 锚点：卡片菜单那条路没按钮可传，但 Menu 关闭时会把焦点交还给触发按钮。 */
function moveAnchor(el?: HTMLElement | null): DOMRect | null {
  const node = el ?? (document.activeElement as HTMLElement | null);
  if (!node || node === document.body || typeof node.getBoundingClientRect !== 'function') return null;
  return node.getBoundingClientRect();
}

function openMove(ids: string[], el?: HTMLElement | null) {
  if (!ids.length) return;
  moveIds.value = [...ids];
  newFolderOpen.value = false;
  newFolderName.value = '';
  const r = moveAnchor(el);
  const left = r
    ? Math.min(Math.max(8, r.right - MOVE_W), window.innerWidth - MOVE_W - 8)
    : Math.max(8, (window.innerWidth - MOVE_W) / 2);
  const top = r ? Math.min(r.bottom + 6, window.innerHeight - 280) : 120;
  movePos.value = { top: Math.max(8, top), left };
  moveOpen.value = true;
  void loadFolders();
}

function openMoveFromBatch(ev: MouseEvent) {
  openMove(selectedIds.value, ev.currentTarget as HTMLElement);
}

async function doMove(folderId: string | null) {
  if (moveBusy.value || !moveIds.value.length) return;
  const ids = [...moveIds.value];
  moveBusy.value = true;
  const updated = await assignFolder(ids, folderId);
  moveBusy.value = false;
  if (!updated) {
    toast(folderState.error || '没有论文被移动', 'error');
    return;
  }
  moveOpen.value = false;
  toast(`已把 ${updated} 篇移到「${folderName(folderId)}」`, 'ok', 3200);
  // 归类完这批就结束：留着选中只会让人以为还能继续对它们动手
  selectedIds.value = [];
  batchMode.value = false;
  await refresh();
}

/** 「新建分类…」就地变成输入框（不在浮层里再叠一层弹窗）。 */
async function startNewFolder() {
  newFolderOpen.value = true;
  await nextTick();
  newFolderInput.value?.focus();
}

async function submitNewFolder() {
  const name = newFolderName.value.trim();
  if (!name || moveBusy.value) return;
  moveBusy.value = true;
  const folder = await createFolder(name);
  moveBusy.value = false;
  if (!folder) {
    toast(folderState.error || '新建分类失败', 'error');
    return;
  }
  newFolderOpen.value = false;
  newFolderName.value = '';
  await doMove(folder.id);      // 建完就把选中的放进去：少让用户再点一次
}

/* ------------------------------ 卡片展示 ------------------------------ */

/** 封面 = 第一页整页渲染（后端按需生成并缓存；没解析过的 PDF 也能出图）。 */
function coverUrl(p: Paper): string {
  return assetUrl(`/api/v1/papers/${p.id}/pages/1`);
}

/** 卡片那行元信息：`12 页 · ICLR · 2026 · 读于 2 天前`（图1 里「10 页 · 2026/10/01」的位置）。 */
function cardMeta(p: Paper): string {
  const parts: string[] = [`${p.page_count || '?'} 页`];
  if (p.venue) parts.push(p.venue);
  if (p.year) parts.push(String(p.year));
  parts.push(p.last_read_at ? `读于 ${fmtRelative(p.last_read_at)}` : '未读过');
  return parts.join(' · ');
}

/** 卡片的分类徽章文案：未分类（或分类已删）给空串 = 不显示徽章。 */
function folderLabelOf(p: Paper): string {
  if (!p.folder_id) return '';
  return folderState.items.find((f) => f.id === p.folder_id)?.name ?? '';
}

/** 卡片收尾行：译完给一行"已完成"，压根没动过给"尚未开始"；正在跑的不给（进度条在说这件事）。 */
function cardFoot(p: Paper): string {
  if (activeTaskOf(p)) return '';
  if (showDoneLine(p)) return `逐页翻译已完成（${p.translate_total} 页）`;
  if (!p.translate_total && !p.pdf_translate_total) return '尚未开始翻译';
  return '';
}

/** 逐页翻译是否已全部完成（完成时用一行文案代替整条进度条）。 */
function isTranslated(p: Paper): boolean {
  return !!p.translate_total && (p.translate_done || 0) >= p.translate_total;
}
const showLoading = computed(() => loading.value && visible.value.length === 0 && !loadError.value);
const showLoadError = computed(() => !!loadError.value && filtered.value.length === 0);
/** 有筛选在生效（分类）：空列表这时是"被筛掉了"，不是"库里没论文"。 */
const hasFilter = computed(() => !!folderFilter.value);
const showEmpty = computed(() => (
  !loading.value && visible.value.length === 0 && !loadError.value && !hasFilter.value
));
/** 有论文、只是被筛掉了：这种"空"要说明是筛选造成的，别显示成"还没有论文"。 */
const filteredOut = computed(() => (
  !loading.value && visible.value.length === 0 && !loadError.value
  && (hasFilter.value || filtered.value.length > 0)
));
/** 空态的归因口径：说清是被哪个分类筛掉的。 */
const filterScope = computed(() => (
  folderFilter.value ? `「${folderFilterLabel.value}」分类` : ''
));

/** 拉取论文列表：加载中 / 请求失败 / 空列表三种状态都在模板里显式呈现。 */
async function refresh() {
  loadError.value = '';
  try {
    await withLoading('papers', () => loadPapers({
      q: keyword.value || undefined,
      folder: folderFilter.value || undefined,      // 分类走后端筛（none = 未分类）
      sort: sort.value,
      order: order.value,
    }), { silent: true });
  } catch (err) {
    loadError.value = (err as Error)?.message ?? String(err);
    reportError(err, '论文列表加载失败');
  }
}

/* **打字即搜**（用户口径）：关键词一变就重新拉列表（去抖 300ms，别每个键都打一次后端）。
 * 回车仍然立即搜一次 —— 但先把在途那次取消，否则会连发两个请求。 */
const refreshDebounced = debounce(() => { void refresh(); }, 300);
function refreshNow() { refreshDebounced.cancel(); void refresh(); }
watch(keyword, () => refreshDebounced());

onMounted(async () => {
  // URL 优先：链接里带 folder 就以它为准（老链接 / 分享 / 侧栏点某分类进来），没有才用记忆值
  const q = typeof route.query.folder === 'string' ? route.query.folder : '';
  folderFilter.value = q || readPref(FOLDER_KEY);
  await loadFolders(true);      // 分类可能在别的窗口被删，回落判断要拿最新一份
  // 分类已被删（query 里那个 id 不在列表里）或未分类已归零：回落到「全部」并清掉参数，
  // 否则页面会一直停在空列表
  if (folderFilter.value && (folderFilter.value === 'none'
    ? folderState.unclassified === 0
    : !folderState.items.some((f) => f.id === folderFilter.value))) {
    folderFilter.value = '';
    syncFolderQuery('');
    writePref(FOLDER_KEY, '');   // 记忆值也可能已失效（分类被删 / 未分类归零）：回落「全部」并清掉它
  }
  await refresh();
  void pollTick();      // 进页面时若已有解析/翻译任务在跑，直接跟上状态
  document.addEventListener('pointerdown', onDocPointer, true);
  document.addEventListener('keydown', onDocKey);
});
onBeforeUnmount(() => {
  stopPolling();
  document.removeEventListener('pointerdown', onDocPointer, true);
  document.removeEventListener('keydown', onDocKey);
});

/* --------------------------- 后台任务状态轮询 ---------------------------
 * 列表里的徽章/进度/按钮文案全取自后端论文行，所以只要**有解析或翻译任务在跑**就自动刷新，
 * 用户不需要手动点「重新加载」；没有任务在跑时立即停下（不做无谓轮询）。 */
let pollTimer: number | null = null;

function stopPolling(): void {
  if (pollTimer !== null) { window.clearInterval(pollTimer); pollTimer = null; }
}

function startPolling(): void {
  if (pollTimer !== null) return;
  pollTimer = window.setInterval(() => { void pollTick(); }, 4000);
}

async function pollTick(): Promise<void> {
  try {
    const res = await api.get<{ items: Array<{
      id: string; type: string; status?: string; paper_id?: string;
      done_items?: number; total_items?: number; message?: string;
    }> }>(
      '/tasks', { limit: 200 });
    const items = res.items || [];
    // 逐页翻译任务：卡片据它把按钮换成「暂停翻译 / 继续翻译」，并显示实时页数
    const map: Record<string, LiveTask> = {};
    for (const t of items) {
      if (t.type !== 'TRANSLATE_MD' || !t.paper_id) continue;
      if (!['pending', 'processing', 'paused'].includes(String(t.status))) continue;
      // 后端按 created_at 倒序返回：同一篇论文只认最新的那个任务，
      // 否则历史任务会把当前状态盖掉（卡片会出现莫名其妙的「继续翻译」）。
      if (map[t.paper_id]) continue;
      map[t.paper_id] = {
        id: t.id,
        status: String(t.status),
        done: Number(t.done_items ?? 0),
        total: Number(t.total_items ?? 0),
        message: String(t.message ?? ''),
      };
    }
    paperTasks.value = map;
    const busy = items.some((t) =>
      (t.type === 'PARSE' || t.type === 'TRANSLATE_MD')
      && (t.status === 'processing' || t.status === 'pending'));
    if (!busy) { stopPolling(); return; }
    await refresh();
    startPolling();
  } catch {
    stopPolling();    // 拉不到就别一直打（下轮操作会重新拉起）
  }
}

/* ------------------------------ 导入 ------------------------------ */

interface ImportForm {
  file: File | null;
  title: string;
  authors: string;
  venue: string;
  year: string;
  tags: string;
  paper_id: string;
  auto_parse: boolean;
  /** 裁出的插图是否带图注（默认关：只出国体本身）。解析时生效。 */
  figure_caption: boolean;
  /** 深度图片搜索：整页没有图注时也按墨迹带兜底裁图（默认关）。解析时生效。 */
  deep_figure_search: boolean;
  /** 解析引擎：本地（离线、快）还是 MinerU（联网、公式/表格更强）。默认值来自设置页。 */
  parse_engine: ParseEngine;
  /* ---- 在线解析（MinerU）的可选参数：**按这篇论文**生效 ----
   * 这些旋钮天然是"每篇不同"的：扫描件才要开强制 OCR、没有公式的论文可以关掉公式识别省时间、
   * 超长文档得用页码范围分段。所以它们放在导入弹窗里，而不是只做一份全局设置。
   * 模型与语言的初始值取设置页的默认值（用户可以只对这一篇改）。 */
  mineru_model_version: MineruModelVersion;
  mineru_language: string;
  mineru_is_ocr: boolean;
  mineru_enable_formula: boolean;
  mineru_enable_table: boolean;
  mineru_page_ranges: string;
}

/** multipart 里直接透传给后端的元数据字段。 */
const UPLOAD_FIELDS = ['title', 'authors', 'venue', 'year', 'tags', 'paper_id'] as const;

interface ImportResult {
  id: string;
  reused?: boolean;
}

/** 解析引擎二选一的文案。**说明写清代价**（在线要 token、要联网），别只写"更强/更好"。 */
const ENGINE_OPTIONS: ReadonlyArray<{ value: ParseEngine; label: string; note: string }> = [
  {
    value: 'local',
    label: '本地解析',
    note: '离线、快，用 PyMuPDF 自研版面分析；公式与复杂表格偏弱',
  },
  {
    value: 'mineru',
    label: '在线解析（MinerU）',
    note: 'MinerU 云端服务，公式 / 表格更强；需要 token 且必须联网',
  },
];

const importOpen = ref(false);
/** MinerU 官方默认值：公式/表格识别默认开、强制 OCR 默认关（与后端 `effective_params` 一致）。 */
const MINERU_DEFAULTS = {
  model_version: 'pipeline' as MineruModelVersion,
  language: 'en',
  is_ocr: false,
  enable_formula: true,
  enable_table: true,
  page_ranges: '',
};
const importForm = reactive<ImportForm>({
  file: null, title: '', authors: '', venue: '', year: '', tags: '', paper_id: '',
  auto_parse: true, figure_caption: false, deep_figure_search: false, parse_engine: 'local',
  mineru_model_version: MINERU_DEFAULTS.model_version,
  mineru_language: MINERU_DEFAULTS.language,
  mineru_is_ocr: MINERU_DEFAULTS.is_ocr,
  mineru_enable_formula: MINERU_DEFAULTS.enable_formula,
  mineru_enable_table: MINERU_DEFAULTS.enable_table,
  mineru_page_ranges: MINERU_DEFAULTS.page_ranges,
});
const dragOver = ref(false);
const fileInput = ref<HTMLInputElement | null>(null);

/* ------------------------- MinerU 配置（导入弹窗用） -------------------------
 * 弹窗里只需要四件事：**默认引擎**（复位用）、**有没有 token**（没配就提示去哪里配）、
 * **掩码后的 token**（提示里给用户一点"配的是哪一个"的确认感）、
 * **模型与语言的默认值/选项**（在线参数区的下拉初值来自设置页）。
 * token 的填写与验证都在设置页，这里**只读**。
 * 失败一律静默降级（`has_token=false`、选项用内置默认）：`/mineru/*` 还没上线时，
 * 导入弹窗必须照常能用本地解析，不能因为一个可选的联网能力把整个导入流程报错拦住。 */
const mineruDefaultEngine = ref<ParseEngine>('local');
/** 设置页里的默认模型 / OCR 语言（导入弹窗里"在线解析参数"的初值）。 */
const mineruDefaultModel = ref<MineruModelVersion>(MINERU_DEFAULTS.model_version);
const mineruDefaultLanguage = ref<string>(MINERU_DEFAULTS.language);
const mineruHasToken = ref(false);
const mineruTokenMasked = ref('');
const mineruModelOptions = ref<Array<{ value: MineruModelVersion; label: string }>>([
  { value: 'pipeline', label: 'pipeline（版面 + 公式更稳，默认）' },
  { value: 'vlm', label: 'vlm（视觉大模型，复杂版面更强）' },
]);
const mineruLanguageOptions = ref<Array<{ value: string; label: string }>>([
  { value: 'en', label: '英语' },
  { value: 'ch', label: '中文' },
]);

/**
 * 设置页存的「导入默认值」（`GET /parse/config`）：弹窗里这些控件的**初值**。
 * 拿不到就用与后端一致的官方默认值 —— 一个可选接口不该把导入流程卡住。
 */
const importDefaults = reactive({
  mineru_is_ocr: MINERU_DEFAULTS.is_ocr,
  mineru_enable_formula: MINERU_DEFAULTS.enable_formula,
  mineru_enable_table: MINERU_DEFAULTS.enable_table,
  mineru_page_ranges: MINERU_DEFAULTS.page_ranges,
  figure_caption: false,
  deep_figure_search: false,
});

/** 拉一次设置页的默认值（打开弹窗时调，失败静默）。 */
async function loadParseDefaults() {
  try {
    const cfg = await api.get<ParseConfig>('/parse/config');
    importDefaults.mineru_is_ocr = cfg?.mineru_is_ocr === true;
    importDefaults.mineru_enable_formula = cfg?.mineru_enable_formula !== false;
    importDefaults.mineru_enable_table = cfg?.mineru_enable_table !== false;
    importDefaults.mineru_page_ranges = String(cfg?.mineru_page_ranges ?? '');
    importDefaults.figure_caption = cfg?.parse_figure_caption === true;
    importDefaults.deep_figure_search = cfg?.parse_deep_figure_search === true;
  } catch {
    // 静默：接口没上时按内置默认（= 后端官方默认）走
  }
}

/** 选了 MinerU 但没配 token：引擎选择下方给一句**可执行**的提示（去哪里配、要做什么）。 */
const mineruNeedsToken = computed(
  () => importForm.parse_engine === 'mineru' && !mineruHasToken.value,
);

async function loadMineruConfig() {
  try {
    const cfg = await api.get<MineruConfig>('/mineru/config');
    mineruHasToken.value = cfg?.has_token === true;
    mineruTokenMasked.value = String(cfg?.token_masked ?? '');
    mineruDefaultEngine.value = cfg?.engine_default === 'mineru' ? 'mineru' : 'local';
    if (cfg?.model_version_options?.length) mineruModelOptions.value = cfg.model_version_options;
    if (cfg?.languages?.length) mineruLanguageOptions.value = cfg.languages;
    if (cfg?.model_version) mineruDefaultModel.value = cfg.model_version;
    if (cfg?.language) mineruDefaultLanguage.value = String(cfg.language);
  } catch {
    // 静默降级：接口缺失/后端未上线时，导入照样按内置默认走（本地解析 + pipeline/en）
  }
}

/* 下面那三个开关**只对本地解析有意义**（用户口径 2026-09-28：选了在线解析就不显示它们）：
 * 版面、插图、图注全由 MinerU 决定。既然不显示，值也必须一起归零/归正 ——
 * 隐藏的表单字段照样会被提交（`submitImport` 不看可见性），留着旧值就是一次静默的参数漂移。 */
watch(() => importForm.parse_engine, (engine) => {
  if (engine !== 'mineru') return;
  importForm.auto_parse = true;          // 在线解析没有"只导入不解析"：选它就是来解析的
  importForm.figure_caption = false;
  importForm.deep_figure_search = false;
});

/**
 * 清空导入表单。
 *
 * ⚠️ **必须在"选新文件"和"导入成功"两处都调用**：表单里的 title/authors/… 会残留上一篇的内容，
 * 而 `submitImport` 会把非空字段原样发给后端、后端又当成"用户填的" → 连续导入第二篇时
 * 新论文被命名成**上一篇**的名字（用户 2026-09-27 报的"命名成其他文章了"）。
 *
 * 两个插图开关也一起复位（默认关）：它们和标题一样是"上一篇的选择"，不清就会带到下一篇。
 * 解析引擎**不复位成硬编码的 local**，而是复位成设置页里的默认引擎（`mineruDefaultEngine`）——
 * 用户把默认引擎设成 MinerU，就是要每篇都走 MinerU，每次打开弹窗都退回本地会很反直觉。
 * 在线解析的可选参数同理：模型/语言取设置页默认值，OCR/公式/表格/页码范围取官方默认值。
 */
function resetImportForm() {
  for (const k of UPLOAD_FIELDS) importForm[k] = '';
  importForm.file = null;
  importForm.auto_parse = true;
  // 插图两个开关的初值现在是设置页存的那一份（在线服务 → 本地解析），不是硬编码关
  importForm.figure_caption = importDefaults.figure_caption;
  importForm.deep_figure_search = importDefaults.deep_figure_search;
  importForm.parse_engine = mineruDefaultEngine.value;
  importForm.mineru_model_version = mineruDefaultModel.value;
  importForm.mineru_language = mineruDefaultLanguage.value;
  importForm.mineru_is_ocr = importDefaults.mineru_is_ocr;
  importForm.mineru_enable_formula = importDefaults.mineru_enable_formula;
  importForm.mineru_enable_table = importDefaults.mineru_enable_table;
  importForm.mineru_page_ranges = importDefaults.mineru_page_ranges;
  if (fileInput.value) fileInput.value.value = '';   // 同一个文件连选两次也要能触发 change
}

function pickFile(file?: File | null) {
  if (!file) return;
  resetImportForm();                                 // 换文件 = 重新填表，先清掉上一篇的残留
  importForm.file = file;
  if (!importForm.title) importForm.title = file.name.replace(/\.pdf$/i, '');
}

function chooseFile() {
  fileInput.value?.click();
}

/**
 * 打开导入弹窗。
 *
 * **每次打开都重新拉一次 `/mineru/config`**（而不是只在页面挂载时拉一次）：用户很可能
 * 去设置页填好 token、验证通过，再回来导入 —— 挂载时那次请求的结果那时还是"没配 token"，
 * 会一直挂着"还没配置 MinerU token"的错误提示。这一个请求很轻，放在打开动作里最稳。
 *
 * ⚠️ **不等这个请求**：弹窗必须立刻出现（后端异常时也不能卡住导入）。配置回来之后再
 * 按它复位一次表单（引擎/模型/语言的初值都来自设置页）；一次本机请求只差几十毫秒，
 * 那时用户连文件都还没选完，第二次复位碰不到任何已输入内容。
 */
function openImport() {
  resetImportForm();
  importOpen.value = true;
  void loadMineruConfig().then(resetImportForm);
  // 默认值与引擎配置是两个接口：分开拉，回来各自复位一次（都是本机请求，几十毫秒）
  void loadParseDefaults().then(resetImportForm);
}

function onFileChange(ev: Event) {
  const input = ev.target as HTMLInputElement | null;
  pickFile(input?.files?.[0]);
}

function onDrop(ev: DragEvent) {
  dragOver.value = false;
  const file = ev.dataTransfer?.files?.[0];
  if (file) pickFile(file);
}

async function submitImport() {
  if (!importForm.file) { toast('请选择 PDF 文件', 'warn'); return; }
  /* 页码范围先在本机做一次**便宜的**格式检查（只允许数字/逗号/连字符）：
   * 用户敲错一个字母就没必要走一趟上传（上传的是整个 PDF）。
   * 真正的判据仍在后端（`mineru_service.norm_page_ranges`），这里只是提前拦明显的手误。 */
  const ranges = importForm.mineru_page_ranges.trim();
  if (importForm.parse_engine === 'mineru' && ranges && !/^[0-9,\-\s]+$/.test(ranges)) {
    toast('页码范围只能填数字、逗号和连字符，例如 1-10 或 2,4-6', 'warn', 4200);
    return;
  }
  const form = new FormData();
  form.append('file', importForm.file);
  for (const k of UPLOAD_FIELDS) {
    const v = importForm[k];
    if (v !== '') form.append(k, String(v));
  }
  form.append('auto_parse', importForm.auto_parse ? 'true' : 'false');
  /* 两个插图开关：**导入即重解析**，所以它们只影响本次解析（均默认关）。
   * 语义见 `PaperBuilder`：图片含图注 = 图体+图注拼成一张；深度图片搜索 = 无图注页兜底裁图。 */
  form.append('figure_caption', importForm.figure_caption ? 'true' : 'false');
  form.append('deep_figure_search', importForm.deep_figure_search ? 'true' : 'false');
  /* 解析引擎在**导入时**传给后端（它决定本次解析走哪条路）。默认值来自设置页，
   * 但用户可以在弹窗里对**这一篇**临时改掉。 */
  form.append('parse_engine', importForm.parse_engine);
  /* 在线解析的可选参数：**只在选在线解析时才发**。
   * 本地解析那条路不认这些键；一个手滑留下的页码范围字符串不该让本地导入报 400。 */
  if (importForm.parse_engine === 'mineru') {
    form.append('mineru_model_version', importForm.mineru_model_version);
    form.append('mineru_language', importForm.mineru_language);
    form.append('mineru_is_ocr', importForm.mineru_is_ocr ? 'true' : 'false');
    form.append('mineru_enable_formula', importForm.mineru_enable_formula ? 'true' : 'false');
    form.append('mineru_enable_table', importForm.mineru_enable_table ? 'true' : 'false');
    if (ranges) form.append('mineru_page_ranges', ranges);
  }
  busyKey.value = 'import';
  try {
    const data = await api.upload<ImportResult>('/papers', form);
    toast(data.reused ? `同一 PDF 已存在，已复用 ${data.id}` : `已导入：${data.id}`, 'ok');
    importOpen.value = false;
    /* ⚠️ **整份表单都要清**：只清 `file` 的话，标题/作者/年份这些框里还留着上一篇的内容，
     * 连续导入第二篇时它们会被当成"用户填的"发到后端 → 新论文被命名成**上一篇**的名字
     * （用户 2026-09-27 报的"连续导入两篇时命名成其他文章"）。 */
    resetImportForm();
    await refresh();
    if (data.reused) openPaper(data.id);
  } catch (err) {
    reportError(err, '导入失败');
  } finally {
    busyKey.value = '';
  }
}

/* ------------------------------ 行内操作 ------------------------------ */

/** 后端 `envelope(message=…)` 的文案（client 挂在 `__meta` 上）——直接用它，前后端话术就不再各写一套 */
function metaMessage(res: any): string {
  return String(res?.__meta?.message ?? '');
}

type TranslateMode = 'resume' | 'retranslate';

/** 翻译只有一种粒度（逐页）：`retranslate` 走 force，忽略已有产物重来。
 *  未解析的论文**不需要**前端特殊处理：后端会把解析串起来（解析完成自动开翻译）。 */
async function runTranslate(paper: Paper, mode: TranslateMode = 'resume') {
  busyKey.value = `tr:${paper.id}`;
  try {
    const task = await api.post<any>(`/papers/${paper.id}/translate`, { mode, brief: true });
    const msg = metaMessage(task);
    if (msg && !task?.reused) toast(msg, 'ok', 4200);      // 未解析→已创建解析任务（完成后自动翻译）
    else if (task?.reused) toast('这篇论文已有翻译任务在跑，已沿用该任务', 'warn', 2600);
    else toast(mode === 'retranslate' ? '已创建重译任务' : '已创建逐页翻译任务', 'ok');
    await refresh();
    startPolling();
  } catch (err) { reportError(err, '触发翻译失败'); } finally { busyKey.value = ''; }
}

interface DeleteResult {
  steps?: unknown[];
}

/* ---------------------------- 行内操作菜单 ---------------------------- */

/** 同 Menu 组件的 items 结构（结构类型，无需导出）。 */
interface RowAction {
  key: string;
  label: string;
  icon?: string;
  danger?: boolean;
  disabled?: boolean;
}

/** 卡片「⋯」菜单：阅读 / 翻译（按当前流水线换文案）/ 移到分类 / 编辑元数据 / 删除。
 *
 *  阅读与翻译这两项原来是**悬停遮罩上的两枚圆钮**（用户口径 2026-12：遮罩撤掉，
 *  「点击进入、菜单即可」）—— 遮罩会把论文首页整块糊掉，而动作放进菜单既不影响看封面，
 *  也不会在触屏上丢失（原来触屏只能靠一条 hover:none 的兜底规则把它常显着）。
 *  重新生成解读、从知识库移除、彻底删除仍然不在菜单里（已下线或搬去别处）。
 */
function rowMenu(p: Paper): RowAction[] {
  return [
    { key: 'read', label: '阅读', icon: 'document' },
    // 已译完的那篇，菜单里给的是「重新翻译」（pipelineLabel 自己就写了这一档，不用再拼）
    { key: 'pipeline', label: pipelineLabel(p), icon: 'translate' },
    { key: 'move', label: '移到分类', icon: 'tag' },
    { key: 'edit', label: '编辑元数据', icon: 'edit' },
    { key: 'delete', label: '删除（移入回收站）', icon: 'trash', danger: true },
  ];
}

function onRowAction(p: Paper, key: string) {
  if (key === 'read') { openPaper(p.id); return; }
  if (key === 'pipeline') { runPipeline(p); return; }
  if (key === 'move') { openMove([p.id]); return; }
  if (key === 'edit') { openEdit(p); return; }
  if (key === 'delete') { askRemove(p); }
}

/* --------------------------- 流水线状态（前后端对齐） --------------------------- */

/** 这篇论文在流水线上的位置。没任务时看后端论文行，有任务时以任务为准。 */
function stage(p: Paper): 'unparsed' | 'translating' | 'translated' {
  if (!p.page_count) return 'unparsed';
  if (activeTaskOf(p)) return 'translating';
  return (p.translate_done || 0) < (p.translate_total || 0) ? 'translating' : 'translated';
}

/** 正在跑的逐页翻译任务（轮询刷新）：`paperId -> {id,status,done,total,message}`。
 *  任务在跑时**它才是真相来源**：磁盘产物还是上一轮的（重新翻译时尤其明显），
 *  只看论文行会出现「已翻译」徽章 +「暂停翻译」按钮同时出现的矛盾画面。 */
interface LiveTask { id: string; status: string; done: number; total: number; message: string }
const paperTasks = ref<Record<string, LiveTask>>({});

/** 这篇论文当前的翻译任务（没有就是 null） */
function activeTaskOf(p: Paper): LiveTask | null {
  return paperTasks.value[p.id] ?? null;
}

/** 论文行自己在跑，但任务表里没有对应记录。
 *
 *  典型场景是 CLI（`pr.py translate`）：它**故意不走任务队列**（便于调试），
 *  于是任务表里空空如也 —— 卡片既不显示转圈遮罩，按钮又写着「继续翻译」，
 *  看起来像"没在跑"；再点一下就会**同一篇两套翻译并发**（用户报过"任务重新进行"）。
 *  这里让卡片至少在视觉上承认"在跑"：`running` 只驱动遮罩（`pointer-events: none`），
 *  不影响按钮语义。 */
function inFlight(p: Paper): boolean {
  return p.status === 'parsing' || p.status === 'translating';
}

/** 进度条数据：优先用论文行（后端每译完一页就回写，最接近磁盘事实），
 *  行里还是上一轮的「已完成」时退回任务的计数器；两者都没有就不显示进度条。 */
function progressOf(p: Paper): { done: number; total: number } | null {
  const t = activeTaskOf(p);
  const rd = p.translate_done || 0;
  const rt = p.translate_total || 0;
  if (t) {
    if (rt > 0 && rd < rt) return { done: rd, total: rt };
    if (t.total > 0) return { done: t.done, total: t.total };
    return null;
  }
  if (rt > 0 && rd < rt) return { done: rd, total: rt };
  return null;
}

/** 「逐页翻译已完成」那行：有任务在跑就不算完成（重新翻译时就该显示进行中）。 */
function showDoneLine(p: Paper): boolean {
  return !activeTaskOf(p) && isTranslated(p);
}

async function pauseTask(taskId: string) {
  try {
    await api.post(`/tasks/${taskId}/pause`);
    toast('已请求暂停：当前这一页译完就停下', 'ok', 2600);
    await pollTick();
  } catch (err) { reportError(err, '暂停失败'); }
}

async function resumeTask(taskId: string) {
  try {
    await api.post(`/tasks/${taskId}/resume`);
    toast('已继续翻译', 'ok', 2200);
    await pollTick();
  } catch (err) { reportError(err, '继续失败'); }
}

/** 卡片按钮文案：任务在跑 → 「暂停翻译」；已暂停 → 「继续翻译」；
 *  否则按流水线位置：未解析 → 「翻译」（点了会先解析）；翻译中 → 「继续翻译」；译完 → 「重新翻译」。 */
function pipelineLabel(p: Paper): string {
  const t = activeTaskOf(p);
  if (t) return t.status === 'paused' ? '继续翻译' : '暂停翻译';
  const s = stage(p);
  return s === 'unparsed' ? '翻译' : s === 'translating' ? '继续翻译' : '重新翻译';
}

function runPipeline(p: Paper) {
  const t = activeTaskOf(p);
  if (t) {
    void (t.status === 'paused' ? resumeTask(t.id) : pauseTask(t.id));
    return;
  }
  void runTranslate(p, stage(p) === 'translated' ? 'retranslate' : 'resume');
}

/* ---------------------------- 删除确认 ---------------------------- */

interface ConfirmState {
  open: boolean;
  paper: Paper | null;
}

const confirmBox = reactive<ConfirmState>({ open: false, paper: null });

/** 删除不可逆：用站内弹窗确认（不再用原生 window.confirm，风格与动效统一）。 */
function askRemove(paper: Paper) {
  confirmBox.paper = paper;
  confirmBox.open = true;
}

async function doRemove() {
  const p = confirmBox.paper;
  if (!p) return;
  confirmBox.open = false;
  busyKey.value = `del:${p.id}`;
  try {
    const res = await api.del<DeleteResult>(`/papers/${p.id}`, { hard: false });
    toast(`已删除 ${p.id}${res?.steps?.length ? `（${res.steps.length} 步）` : ''}`, 'ok');
    await refresh();
  } catch (err) { reportError(err, '删除失败'); } finally { busyKey.value = ''; confirmBox.paper = null; }
}
/* ------------------------------ 批量操作 ------------------------------
 * 后端只有单篇接口（`DELETE /papers/{id}`、`POST /papers/{id}/translate`），
 * 所以批量 = 前端**逐篇顺序发**，并逐篇吞掉失败 —— 一篇失败不该把剩下九篇也停掉。
 * 动作本身要先过确认弹窗（图1 的批量条也有这一步，不是即点即执行）。
 */

const batchBox = reactive<{ open: boolean; kind: 'delete' | 'translate' }>({ open: false, kind: 'delete' });

function askBatch(kind: 'delete' | 'translate') {
  if (!selectedCount.value || batchBusy.value) return;
  batchBox.kind = kind;
  batchBox.open = true;
}

async function confirmBatch() {
  const kind = batchBox.kind;
  batchBox.open = false;
  if (kind === 'delete') { await runBatchDelete(); return; }
  await runBatchTranslate();
}

async function runBatchDelete() {
  const ids = [...selectedIds.value];
  if (!ids.length) return;
  batchBusy.value = true;
  let ok = 0;
  const failed: string[] = [];
  for (const id of ids) {
    try { await api.del(`/papers/${id}`, { hard: false }); ok += 1; } catch { failed.push(id); }
  }
  batchBusy.value = false;
  selectedIds.value = [];
  if (failed.length) {
    toast(`已删除 ${ok} 篇，${failed.length} 篇失败：${failed.join('、')}`, 'warn', 5600);
  } else {
    toast(`已删除 ${ok} 篇（PDF 移入 data/trash；对话历史与批注已清）`, 'ok', 3200);
  }
  await refresh();
}

async function runBatchTranslate() {
  const targets = visible.value.filter((p) => selectedIds.value.includes(p.id) && stage(p) !== 'translated');
  selectedIds.value = [];
  if (!targets.length) { toast('选中的论文都已翻完，没有需要重建的任务', 'warn', 2800); return; }
  batchMode.value = false;
  batchBusy.value = true;
  for (const p of targets) await runTranslate(p, 'resume');
  batchBusy.value = false;
}

/* ------------------------------ 编辑元数据 ------------------------------ */

interface EditForm {
  title: string;
  authors: string;
  venue: string;
  year: string;
  tags: string;
  abstract: string;
  // 元数据扩展（2026-10-07）：中英文摘要/关键词、领域、一句话概括、创新点、
  // 前置知识、难度、阅读时间、类型、DOI/开源地址。列表字段在表单里按**一行一条**编辑。
  abstract_zh: string;
  keywords_en: string;
  domain: string;
  one_liner: string;
  contributions: string;
  prereq: string;
  difficulty: string;
  read_time_min: string;
  paper_type: string;
  doi: string;
  code_url: string;
}

const editOpen = ref(false);
const editTarget = ref<Paper | null>(null);
const editForm = reactive<EditForm>({
  title: '', authors: '', venue: '', year: '', tags: '', abstract: '',
  abstract_zh: '', keywords_en: '', domain: '', one_liner: '', contributions: '',
  prereq: '', difficulty: '', read_time_min: '', paper_type: '', doi: '', code_url: '',
});

/** 列表字段在表单里是"一行一条"，落库前转成数组。 */
const lines = (text: string) => String(text || '').split('\n').map((s) => s.trim()).filter(Boolean);
const csv = (text: string) => String(text || '').split(',').map((s) => s.trim()).filter(Boolean);

function openEdit(paper: Paper) {
  editTarget.value = paper;
  Object.assign(editForm, {
    title: paper.title || '',
    authors: paper.authors || '',
    venue: paper.venue || '',
    year: paper.year === null || paper.year === undefined ? '' : String(paper.year),
    tags: (paper.tags || []).join(','),
    abstract: paper.abstract || '',
    abstract_zh: paper.abstract_zh || '',
    keywords_en: (paper.keywords_en || []).join(', '),
    domain: paper.domain || '',
    one_liner: paper.one_liner || '',
    contributions: (paper.contributions || []).join('\n'),
    prereq: (paper.prereq || []).join('\n'),
    difficulty: paper.difficulty || '',
    read_time_min: paper.read_time_min ? String(paper.read_time_min) : '',
    paper_type: paper.paper_type || '',
    doi: paper.doi || '',
    code_url: paper.code_url || '',
  });
  editOpen.value = true;
}

/* ------------------------------ 元数据补全 ------------------------------ */
/** `''` 空闲 / `'pdf'` 正在离线识别 / `'agent'` 刚把 agent 任务排上队 */
const metaBusy = ref<'' | 'pdf' | 'agent'>('');

/**
 * 「从论文识别」：**离线**同步（会议 / 年份 / 原文摘要 / DOI / 参考文献 / 阅读时间），
 * 不花钱、立刻返回，所以直接把结果灌回表单。
 * 后端**只填空字段**，不会覆盖已经填好的值。
 */
async function autoMeta() {
  const p = editTarget.value;
  if (!p || metaBusy.value) return;
  metaBusy.value = 'pdf';
  try {
    const res = await api.post<{ paper: Paper }>(`/papers/${p.id}/metadata/auto`, { model: false });
    const np = res?.paper;
    if (np) {
      Object.assign(editForm, {
        title: np.title || editForm.title,
        authors: np.authors || editForm.authors,
        venue: np.venue || editForm.venue,
        year: np.year === null || np.year === undefined ? editForm.year : String(np.year),
        tags: (np.tags || []).join(','),
        abstract: np.abstract || editForm.abstract,
        doi: np.doi || editForm.doi,
        code_url: np.code_url || editForm.code_url,
        read_time_min: np.read_time_min ? String(np.read_time_min) : editForm.read_time_min,
      });
    }
    toast(metaMessage(res) || '已自动补全', 'ok');
    await refresh();
  } catch (err) { reportError(err, '自动补全失败'); } finally { metaBusy.value = ''; }
}

/**
 * 「模型补全」：交给 **agent 读全文**填写（用户口径 2026-10-07：完整 agent 功能，不用显示细节）。
 * 它是个后台任务（实测 1~3 分钟），所以这里只负责排队 + 说一声，跑完自动刷新卡片。
 */
async function agentMeta() {
  const p = editTarget.value;
  if (!p || metaBusy.value) return;
  metaBusy.value = 'agent';
  try {
    const res = await api.post<{ id?: string }>(`/papers/${p.id}/metadata/agent`);
    toast(metaMessage(res) || '已在后台开始补全论文信息（agent 会先读这篇论文）', 'ok', 6200);
    editOpen.value = false;
    await refresh();
    if (res?.id) void watchMetaTask(p.id, String(res.id));
  } catch (err) { reportError(err, '发起补全失败'); } finally { metaBusy.value = ''; }
}

/**
 * 等后台的补全任务跑完再刷新一次卡片。
 *
 * 为什么不干脆不管：agent 一次 1~3 分钟，用户点完就看着那张还是空的卡片，
 * 只会以为"点了没反应"。这里**不展示过程**（用户口径），只在结束时刷一下 + 说一句。
 */
async function watchMetaTask(paperId: string, taskId: string) {
  for (let i = 0; i < 72; i += 1) {
    await new Promise((resolve) => { setTimeout(resolve, 5000); });
    try {
      const d = await api.get<{ items: TaskInfo[] }>('/tasks', { paper_id: paperId, limit: 20 });
      const t = (d.items || []).find((x) => x.id === taskId);
      if (t && ['pending', 'processing', 'paused'].includes(String(t.status))) continue;
      await refresh();
      toast(String(t?.message || '论文信息已补全'), 'ok', 5600);
      return;
    } catch { /* 拉不到就下一轮再试 */ }
  }
  await refresh();
}

async function submitEdit() {
  const p = editTarget.value;
  if (!p) return;
  const payload = {
    title: editForm.title,
    authors: editForm.authors,
    venue: editForm.venue,
    abstract: editForm.abstract,
    tags: csv(editForm.tags),
    year: editForm.year === '' ? null : Number(editForm.year),
    abstract_zh: editForm.abstract_zh,
    keywords_en: csv(editForm.keywords_en),
    domain: editForm.domain,
    one_liner: editForm.one_liner,
    contributions: lines(editForm.contributions),
    prereq: lines(editForm.prereq),
    difficulty: editForm.difficulty,
    read_time_min: editForm.read_time_min === '' ? null : Number(editForm.read_time_min),
    paper_type: editForm.paper_type,
    doi: editForm.doi,
    code_url: editForm.code_url,
  };
  try {
    await api.put(`/papers/${p.id}`, payload);
    toast('元数据已更新', 'ok');
    editOpen.value = false;
    await refresh();
  } catch (err) { reportError(err, '更新失败'); }
}
</script>

<template>
  <div class="library-view">
    <!-- 顶部一行（左 标题 + 计数 + 当前分类 chip；中 搜索；右 导入 / 刷新 ｜ 批量 / 筛选 / 排序 / 视图切换） -->
    <header class="lib-toolbar">
      <div class="lib-heading">
        <h1>论文库</h1>
        <span class="lib-count tnum">共 {{ state.papersTotal }} 篇</span>
        <!-- 当前分类常驻：筛选浮层一收就看不见了，这枚 chip 让它一直可见、点 × 回全部 -->
        <button
          v-if="folderFilter"
          type="button"
          class="lib-fchip"
          :title="`当前分类：${folderFilterLabel}（点 × 回到全部）`"
          @click="setFolder('')"
        >
          <AppIcon name="tag" :size="13" />
          <span class="lib-fchip-name">{{ folderFilterLabel }}</span>
          <AppIcon name="close" :size="12" />
        </button>
      </div>

      <label class="lib-search">
        <AppIcon name="search" :size="15" />
        <input
          v-model="keyword"
          type="search"
          placeholder="搜索标题、作者、会议或年份"
          @keyup.enter="refreshNow"
        />
      </label>

      <div class="lib-tools">
        <button type="button" class="lib-tool primary" title="导入 PDF" @click="openImport">
          <AppIcon name="add" :size="16" />导入
        </button>
        <button type="button" class="lib-tool square" title="刷新列表" :disabled="loading" @click="refresh">
          <AppIcon name="refresh" :size="15" />
        </button>

        <span class="lib-divider" />

        <button
          type="button"
          class="lib-tool"
          :class="{ on: batchMode }"
          :aria-pressed="batchMode"
          @click="toggleBatch"
        >
          <AppIcon name="checkbox" :size="14" />批量
        </button>

        <div ref="filterBox" class="lib-filter">
          <button
            type="button"
            class="lib-tool"
            :class="{ on: filterCount > 0 }"
            aria-haspopup="dialog"
            :aria-expanded="filterOpen"
            @click="filterOpen = !filterOpen"
          >
            <AppIcon name="filter" :size="14" />筛选
            <span v-if="filterCount" class="lib-tool-num tnum">{{ filterCount }}</span>
          </button>

          <div v-if="filterOpen" class="lib-pop" role="dialog" aria-label="筛选">
            <div class="lib-pop-label">分类</div>
            <div class="lib-pills">
              <button type="button" class="lib-pill" :class="{ on: folderFilter === '' }" @click="setFolder('')">全部</button>
              <!-- 未分类为 0 时不出现（与侧栏同口径），否则选了它就是一面空列表 -->
              <button v-if="folderState.unclassified > 0" type="button" class="lib-pill" :class="{ on: folderFilter === 'none' }" @click="setFolder('none')">未分类</button>
              <button
                v-for="f in folderState.items"
                :key="f.id"
                type="button"
                class="lib-pill"
                :class="{ on: folderFilter === f.id }"
                @click="setFolder(f.id)"
              >{{ f.name }}</button>
            </div>

            <button v-if="filterCount" type="button" class="lib-clear" @click="clearFilters">清空筛选</button>
          </div>
        </div>

        <label class="lib-sort">
          <span class="sr-only">排序</span>
          <select v-model="sortKey" title="排序方式" @change="refresh">
            <option v-for="o in SORT_OPTIONS" :key="o.value" :value="o.value">{{ o.label }}</option>
          </select>
        </label>

        <div class="lib-view" role="group" aria-label="视图切换">
          <button type="button" :class="{ on: viewMode === 'grid' }" title="封面视图" @click="setView('grid')">
            <AppIcon name="grid" :size="14" />
          </button>
          <button type="button" :class="{ on: viewMode === 'list' }" title="列表视图" @click="setView('list')">
            <AppIcon name="list" :size="14" />
          </button>
        </div>
      </div>
    </header>

    <!-- 批量工具条：只在批量模式出现（顶部第三行，随页面滚动） -->
    <div v-if="batchMode" class="lib-batch" role="toolbar" aria-label="批量操作">
      <button type="button" class="lib-batch-x" @click="toggleBatch">取消</button>
      <span class="lib-batch-count tnum">已选 {{ selectedCount }} / {{ visible.length }}</span>
      <button type="button" class="lib-batch-all" :disabled="!visible.length" @click="selectAll">
        {{ allSelected ? '取消全选' : '全选' }}
      </button>
      <span class="spacer" />
      <button
        type="button"
        class="lib-tool"
        :disabled="!selectedCount || batchBusy"
        @click="openMoveFromBatch"
      >
        <AppIcon name="tag" :size="14" />移到分类
      </button>
      <button
        type="button"
        class="lib-tool"
        :disabled="!selectedCount || batchBusy"
        @click="askBatch('translate')"
      >
        <AppIcon name="translate" :size="14" />翻译
      </button>
      <button
        type="button"
        class="lib-tool danger"
        :disabled="!selectedCount || batchBusy"
        @click="askBatch('delete')"
      >
        <AppIcon name="trash" :size="14" />删除
      </button>
    </div>

    <!-- 加载中：与真实卡片同形的骨架（封面框 + 两行字），不用一行灰字 -->
    <div class="lib-grid" v-if="showLoading" aria-busy="true">
      <span class="sr-only">正在加载论文列表…</span>
      <div class="sk-card" v-for="i in 8" :key="i" aria-hidden="true">
        <div class="sk" style="width: 100%; aspect-ratio: 3 / 4; border-radius: 16px"></div>
        <div class="sk sk-line" style="width: 86%; margin-top: 10px"></div>
        <div class="sk sk-line" style="width: 54%; margin-top: 6px"></div>
      </div>
    </div>

    <!-- 请求失败（可重试） -->
    <div class="card empty-state" v-else-if="showLoadError">
      <span class="es-icon bad"><AppIcon name="warning" :size="20" /></span>
      <span class="es-title">论文列表加载失败</span>
      <span class="es-hint">{{ loadError }}</span>
      <div class="es-actions">
        <button class="btn primary sm" @click="refresh">
          <AppIcon name="refresh" :size="14" />重新加载
        </button>
      </div>
    </div>

    <!-- 空列表（直接把导入入口放在这里） -->
    <div class="card empty-state" v-else-if="showEmpty">
      <span class="es-icon"><AppIcon name="library" :size="20" /></span>
      <span class="es-title">还没有论文</span>
      <span class="es-hint">导入一篇 PDF，即可开始解析、逐页翻译与中英对照阅读。</span>
      <div
        class="drop-zone drop-zone-inline"
        :class="{ over: dragOver }"
        @dragover.prevent="dragOver = true"
        @dragleave="dragOver = false"
        @drop.prevent="onDrop"
        @click="openImport"
      >
        <AppIcon name="upload" :size="18" />
        <span>拖拽 PDF 到此处，或点击选择文件</span>
      </div>
    </div>

    <!-- 有论文、只是被筛掉了：这种空要归因到筛选，别显示成"还没有论文" -->
    <div class="card empty-state" v-else-if="filteredOut">
      <span class="es-icon"><AppIcon name="filter" :size="20" /></span>
      <span class="es-title">没有符合条件的论文</span>
      <span class="es-hint">当前筛选（{{ filterScope }}）下没有结果。</span>
      <div class="es-actions">
        <button class="btn sm" @click="clearFilters">
          <AppIcon name="close" :size="14" />清空筛选
        </button>
      </div>
    </div>

    <template v-else>
      <!-- 有旧数据但本次刷新失败 -->
      <p class="tiny load-error" v-if="loadError">
        刷新失败：{{ loadError }}（下方为上次成功加载的数据）
      </p>

      <!-- 封面网格（图1 版式）/ 列表：同一份数据两种排布，卡片本体见 PaperCard.vue。
           「按分类」排序时外面多一层分组：标题行 + 该段**同一个** .lib-grid / .lib-list ——
           网格 / 列表本身的定义一行没改，只是多了一层包装，标题因此天然占满整行。 -->
      <div class="lib-groups">
        <div v-for="seg in segments" :key="seg.key" class="lib-group">
          <button
            v-if="seg.name"
            type="button"
            class="lib-group-head"
            :aria-expanded="isGroupOpen(seg.key)"
            :title="isGroupOpen(seg.key) ? `收起「${seg.name}」` : `展开「${seg.name}」`"
            @click="toggleGroup(seg.key)"
          >
            <AppIcon
              class="lib-group-caret"
              :name="isGroupOpen(seg.key) ? 'chevron-down' : 'chevron-right'"
              :size="18"
            />
            <span class="lib-group-name">{{ seg.name }}</span>
            <span class="lib-group-count tnum">{{ seg.items.length }}</span>
          </button>
          <!-- 收起时整段不渲染（标题与计数仍在上方），展开态才渲染卡片 -->
          <div v-if="isGroupOpen(seg.key)" :class="viewMode === 'grid' ? 'lib-grid' : 'lib-list'">
            <PaperCard
              v-for="p in seg.items"
              :key="p.id"
              :variant="viewMode"
              :title="p.title"
              :cover-url="coverUrl(p)"
              :meta="cardMeta(p)"
              :folder-label="folderLabelOf(p)"
              :progress="progressOf(p)"
              :foot-note="cardFoot(p)"
              :running="!!activeTaskOf(p) || inFlight(p)"
              :action-label="pipelineLabel(p)"
              :show-action="stage(p) !== 'translated'"
              :busy="busyKey === 'tr:' + p.id || busyKey === 'parse:' + p.id"
              :menu="rowMenu(p)"
              :batch="batchMode"
              :selected="selectedIds.includes(p.id)"
              @open="detailId = p.id"
              @read="openPaper(p.id)"
              @action="runPipeline(p)"
              @pick="onRowAction(p, $event)"
              @toggle="toggleSelect(p.id)"
            />
          </div>
        </div>
      </div>
    </template>

    <!-- 论文详情（图2）：点卡片打开；详情里改过东西（阅读状态 / 删除）就刷新列表 -->
    <PaperDetailModal
      v-if="detailId"
      :key="detailId"
      :paper-id="detailId"
      @close="detailId = ''"
      @changed="refresh"
    />
    <!-- 「移到分类」浮层：与筛选同一个 .lib-pop（同一套浮层语言），fixed 贴在触发它的按钮旁边。 -->
    <Teleport to="body">
      <div
        v-if="moveOpen"
        ref="moveBox"
        class="lib-pop is-move"
        role="dialog"
        aria-label="移到分类"
        :style="{ top: movePos.top + 'px', left: movePos.left + 'px' }"
      >
        <div class="lib-pop-label">移到分类</div>
        <button type="button" class="lib-folder" :disabled="moveBusy" @click="doMove(null)">
          <AppIcon name="tag" :size="13" />
          <span class="lib-folder-name">未分类</span>
        </button>
        <button
          v-for="f in folderState.items"
          :key="f.id"
          type="button"
          class="lib-folder"
          :disabled="moveBusy"
          @click="doMove(f.id)"
        >
          <span class="lib-folder-name">{{ f.name }}</span>
          <span v-if="f.paper_count" class="lib-folder-num tnum">{{ f.paper_count }}</span>
        </button>
        <p v-if="folderState.error" class="lib-folder-err">{{ folderState.error }}</p>
        <div class="lib-sep" role="separator" />
        <input
          v-if="newFolderOpen"
          ref="newFolderInput"
          v-model="newFolderName"
          class="lib-folder-input"
          type="text"
          maxlength="32"
          placeholder="分类名，回车确认"
          @keydown.enter.prevent="submitNewFolder"
          @keydown.esc.stop="newFolderOpen = false"
        />
        <button v-else type="button" class="lib-folder create" :disabled="moveBusy" @click="startNewFolder">
          <AppIcon name="add" :size="13" />
          <span class="lib-folder-name">新建分类…</span>
        </button>
      </div>
    </Teleport>

    <!-- 批量操作的确认（与单篇删除同一套弹窗，口径一致） -->
    <ConfirmModal
      :open="batchBox.open"
      :title="batchBox.kind === 'delete' ? '批量删除论文' : '批量翻译'"
      :body="batchBox.kind === 'delete'
        ? `会把选中的 ${selectedCount} 篇移入回收站（之后可在回收站逐篇恢复论文与 PDF）；但批注、高亮、笔记、对话历史、阅读进度与解读产物在删除时就一并清掉，恢复也带不回来。`
        : `会为选中的 ${selectedCount} 篇各起一个翻译任务；已在跑或已译完的会跳过。`"
      :confirm-label="batchBox.kind === 'delete' ? '删除' : '开始翻译'"
      :danger="batchBox.kind === 'delete'"
      @confirm="confirmBatch"
      @cancel="batchBox.open = false"
    />

    <!-- 删除确认（站内弹窗，不用原生 confirm） -->
    <ConfirmModal
      :open="confirmBox.open"
      title="删除论文"
      :body="'会移入回收站（之后可在回收站恢复论文与 PDF）；但批注、高亮、笔记、对话历史、阅读进度与解读产物在删除时就一并清掉，恢复也带不回来。'"
      :target="confirmBox.paper?.title || ''"
      confirm-label="删除"
      danger
      @confirm="doRemove"
      @cancel="confirmBox.open = false"
    />

    <!-- 导入 PDF -->
    <Modal :open="importOpen" title="导入 PDF" @close="importOpen = false">
      <div
        class="drop-zone"
        :class="{ over: dragOver }"
        @dragover.prevent="dragOver = true"
        @dragleave="dragOver = false"
        @drop.prevent="onDrop"
        @click="chooseFile"
      >
        <div v-if="importForm.file">
          已选：<b>{{ importForm.file.name }}</b>
          （{{ (importForm.file.size / 1048576).toFixed(1) }} MB）
        </div>
        <div v-else>拖拽 PDF 到此处，或点击选择文件</div>
        <input
          ref="fileInput"
          type="file"
          accept="application/pdf"
          style="display: none"
          @change="onFileChange"
        />
      </div>
      <div class="grid cols-2" style="margin-top: 14px">
        <label class="field">标题<input v-model="importForm.title" /></label>
        <label class="field">作者<input v-model="importForm.authors" /></label>
        <label class="field">会议 / 期刊<input v-model="importForm.venue" /></label>
        <label class="field">年份<input v-model="importForm.year" type="number" /></label>
        <label class="field">标签（逗号分隔）<input v-model="importForm.tags" /></label>
        <label class="field">paper_id（留空自动生成）<input v-model="importForm.paper_id" /></label>
      </div>
      <!-- 解析引擎：二选一（互斥）。放最前面，因为它决定的是"这次导入交给谁解析"，
           是比"解析后要不要裁图"更上层的选择。 -->
      <div class="engine">
        <div class="engine-label">解析引擎</div>
        <div class="engine-row" role="radiogroup" aria-label="解析引擎">
          <button
            v-for="opt in ENGINE_OPTIONS"
            :key="opt.value"
            type="button"
            class="engine-opt"
            :class="{ active: importForm.parse_engine === opt.value }"
            role="radio"
            :aria-checked="importForm.parse_engine === opt.value"
            @click="importForm.parse_engine = opt.value"
          >
            <span class="engine-name">{{ opt.label }}</span>
            <span class="engine-note">{{ opt.note }}</span>
          </button>
        </div>
        <!-- 没配 token 就选在线解析：直接给出"去哪配、要做什么"，而不是等到导入失败才说 -->
        <p class="tiny engine-warn" v-if="mineruNeedsToken">
          <AppIcon name="warning" :size="13" />
          还没配置 MinerU token：请到「设置 → 在线解析（MinerU）」填入并验证
          <template v-if="mineruTokenMasked">（当前已存：{{ mineruTokenMasked }}）</template>
        </p>
      </div>

      <!-- 在线解析的可选参数：只在选在线解析时出现（本地解析没有这些旋钮）。
           它们天然是"每篇不同"的：扫描件才要开强制 OCR、没有公式的论文可以关掉公式识别省时间、
           超长文档用页码范围分段。模型与语言的初值来自设置页，这里可以只对**这一篇**改。 -->
      <div class="online-params" v-if="importForm.parse_engine === 'mineru'">
        <div class="op-head">
          在线解析参数
          <span class="op-hint">（只对这篇论文生效；初值来自「设置 → 在线服务 → 解析参数」）</span>
        </div>
        <div class="op-grid">
          <label class="field">模型
            <select v-model="importForm.mineru_model_version" data-mineru-model>
              <option v-for="m in mineruModelOptions" :key="m.value" :value="m.value">
                {{ m.label }}
              </option>
            </select>
          </label>
          <label class="field">OCR 语言
            <select v-model="importForm.mineru_language" data-mineru-language>
              <option v-for="l in mineruLanguageOptions" :key="l.value" :value="l.value">
                {{ l.label }}
              </option>
            </select>
          </label>
        </div>
        <Switch v-model="importForm.mineru_is_ocr">
          <span class="opt">
            <span class="opt-text">强制 OCR</span>
            <span class="opt-note">扫描件 / 字体乱码时打开；有文本层的 PDF 一般不需要（更慢）</span>
          </span>
        </Switch>
        <Switch v-model="importForm.mineru_enable_formula">
          <span class="opt">
            <span class="opt-text">识别公式</span>
            <span class="opt-note">关掉可少一次模型调用；公式会退化成图片，论文一般都要开</span>
          </span>
        </Switch>
        <Switch v-model="importForm.mineru_enable_table">
          <span class="opt">
            <span class="opt-text">识别表格</span>
            <span class="opt-note">关掉可快一些；有表格的论文要开，否则表格只能当图片看</span>
          </span>
        </Switch>
        <label class="field">页码范围
          <input
            v-model="importForm.mineru_page_ranges"
            data-mineru-ranges
            placeholder="留空 = 全部页；例：1-10、2,4-6、2--2（-2 = 倒数第二页）"
          />
        </label>
      </div>

      <!-- 下面三个开关**只在本地解析时出现**（用户口径 2026-09-28）：在线解析的版面、插图、
           图注全由 MinerU 决定，这三个开关在那里没有对应物，摆着只是噪声。 -->
      <template v-if="importForm.parse_engine === 'local'">
        <p class="op-note">插图两项的初值来自「设置 → 在线服务 → 本地解析」，可对这篇单独改。</p>
        <Switch v-model="importForm.auto_parse">
          <span class="opt"><span class="opt-text">导入后立即解析</span></span>
        </Switch>
        <!-- 两个插图开关：**导入时选、解析时生效**（导入本身就是重解析）。
             默认都关 = 只出图注锚定的图、且裁图不含图注（图注在阅读器里有自己的一行，不丢信息）。 -->
        <Switch v-model="importForm.figure_caption">
          <span class="opt">
            <span class="opt-text">图片是否包含图注</span>
            <span class="opt-note">开 = 图体与图注拼成一张（现状那套卡片）；关（默认）= 只裁图体本身</span>
          </span>
        </Switch>
        <Switch v-model="importForm.deep_figure_search">
          <span class="opt">
            <span class="opt-text">深度图片搜索</span>
            <span class="opt-note">开 = 整页没有图注时也按墨迹带兜底裁图；关（默认）= 只出图注锚定的图</span>
          </span>
        </Switch>
      </template>
      <template #footer>
        <button class="btn" @click="importOpen = false">取消</button>
        <button class="btn primary" :disabled="busyKey === 'import'" @click="submitImport">
          {{ busyKey === 'import' ? '导入中…' : '开始导入' }}
        </button>
      </template>
    </Modal>

    <!-- 编辑元数据 -->
    <Modal :open="editOpen" title="编辑元数据" wide @close="editOpen = false">
      <div class="grid cols-2">
        <label class="field">标题<input v-model="editForm.title" /></label>
        <label class="field">作者<input v-model="editForm.authors" /></label>
        <label class="field">会议 / 期刊<input v-model="editForm.venue" /></label>
        <label class="field">年份<input v-model="editForm.year" /></label>
        <label class="field">中文关键词（逗号分隔）<input v-model="editForm.tags" /></label>
        <label class="field">英文关键词（逗号分隔）<input v-model="editForm.keywords_en" /></label>
        <label class="field">所属领域 / 研究方向<input v-model="editForm.domain" /></label>
        <label class="field">一句话概括<input v-model="editForm.one_liner" /></label>
        <label class="field">难度
          <select v-model="editForm.difficulty">
            <option value="">（未填）</option>
            <option value="入门">入门</option>
            <option value="进阶">进阶</option>
            <option value="专家">专家</option>
          </select>
        </label>
        <label class="field">论文类型
          <select v-model="editForm.paper_type">
            <option value="">（未填）</option>
            <option v-for="t in ['方法', '综述', '数据集', '系统', '理论', '其他']" :key="t" :value="t">{{ t }}</option>
          </select>
        </label>
        <label class="field">推荐阅读时间（分钟）<input v-model="editForm.read_time_min" /></label>
        <label class="field">DOI<input v-model="editForm.doi" /></label>
        <label class="field">开源 / 项目地址<input v-model="editForm.code_url" /></label>
      </div>
      <label class="field" style="margin-top: 10px">
        原文摘要<textarea v-model="editForm.abstract" rows="3"></textarea>
      </label>
      <label class="field" style="margin-top: 10px">
        中文摘要<textarea v-model="editForm.abstract_zh" rows="4"></textarea>
      </label>
      <div class="grid cols-2" style="margin-top: 10px">
        <label class="field">核心创新点（一行一条）<textarea v-model="editForm.contributions" rows="4"></textarea></label>
        <label class="field">前置知识（一行一条）<textarea v-model="editForm.prereq" rows="4"></textarea></label>
      </div>
      <div class="tiny faint" style="margin-top: 6px">
        这些字段都有两个自动来源：<b>从论文识别</b>离线读 PDF 与页产物（会议 / 年份 / 原文摘要 /
        DOI / 参考文献 / 阅读时间，不花钱）；<b>模型补全</b>交给 agent 读全文
        （中英文关键词 / 中文摘要 / 领域 / 概括 / 创新点 / 前置知识 / 难度），过程在后台、跑完自动刷新。
      </div>
      <template #footer>
        <button class="btn sm" :disabled="!!metaBusy" title="离线读 PDF 首页与页产物（不花钱）：会议、年份、原文摘要、DOI、参考文献、阅读时间" @click="autoMeta">
          <AppIcon name="sparkle" :size="13" />{{ metaBusy === 'pdf' ? '识别中…' : '从论文识别' }}
        </button>
        <button class="btn sm" :disabled="!!metaBusy" title="交给 agent 读全文填写：关键词、中文摘要、领域、概括、创新点、前置知识、难度" @click="agentMeta">
          <AppIcon name="model" :size="13" />{{ metaBusy === 'agent' ? '已排队…' : '模型补全' }}
        </button>
        <span class="grow"></span>
        <button class="btn" @click="editOpen = false">取消</button>
        <button class="btn primary" @click="submitEdit">保存</button>
      </template>
    </Modal>
  </div>
</template>

<style scoped>
/* ---------------- 顶部工具栏（图1 版式） ----------------
 * 左：标题 + 计数胶囊；右：批量 / 筛选 / 排序 / 视图切换。
 * 工具条那条下划线只到 `--divider` 的强度 —— 它是"分组线"，不是卡片描边。 */
.lib-toolbar {
  display: flex; align-items: center;
  gap: 10px; flex-wrap: wrap;      /* 窄到放不下时整行换行，不把按钮挤出视口 */
  margin-bottom: 16px; padding-bottom: 12px;
  border-bottom: 1px solid var(--divider);   /* 两行合并后只剩这一条分隔线 */
}
.lib-heading { display: flex; align-items: center; gap: 8px; flex: none; min-width: 0; }
.lib-heading h1 {
  margin: 0; font-size: calc(16px * var(--ts, 1)); font-weight: 600; letter-spacing: -.02em;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.lib-count {
  flex: none; display: inline-flex; align-items: center; height: 20px; padding: 0 8px;
  border-radius: 999px; background: var(--bg-subtle); border: 1px solid var(--border);
  font-size: calc(11px * var(--ts, 1)); color: var(--text-dim);
}
.lib-tools { display: flex; align-items: center; gap: 8px; flex: none; flex-wrap: wrap; }

/* 工具条控件统一 30px 高：同一排里按钮 / 下拉 / 分段不许参差 */
.lib-tool {
  display: inline-flex; align-items: center; gap: 6px;
  height: 30px; padding: 0 12px; border-radius: var(--radius-ctl);
  border: 1px solid var(--border); background: transparent; color: var(--text);
  font-family: inherit; font-size: calc(12px * var(--ts, 1)); font-weight: 500; cursor: pointer;
  transition: background var(--dur) var(--ease), border-color var(--dur) var(--ease),
              color var(--dur) var(--ease), transform var(--dur-fast) var(--ease);
}
.lib-tool:hover { background: var(--bg-hover); border-color: var(--border-strong); }
.lib-tool:active { transform: scale(.97); }
.lib-tool.on {
  background: var(--accent-soft);
  border-color: color-mix(in srgb, var(--accent) 42%, transparent);
  color: var(--accent-text);
}
.lib-tool-num { font-size: calc(11px * var(--ts, 1)); color: var(--accent-text); }

/* 当前分类 chip：与 .lib-tool.on 同色（选中态那一套），长分类名截断，别把工具条撑出横向滚动 */
.lib-fchip {
  display: inline-flex; align-items: center; gap: 5px; max-width: 190px;
  height: 30px; padding: 0 9px; border-radius: var(--radius-ctl);
  border: 1px solid color-mix(in srgb, var(--accent) 42%, transparent);
  background: var(--accent-soft); color: var(--accent-text);
  font-family: inherit; font-size: calc(12px * var(--ts, 1)); font-weight: 500; cursor: pointer;
  transition: background var(--dur) var(--ease);
}
.lib-fchip:hover { background: color-mix(in srgb, var(--accent) 20%, transparent); }
.lib-fchip .icon { flex: none; }
.lib-fchip-name { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
/* 只有图标的工具按钮：收到 30px 见方，别留一大块空白 */
.lib-tool.square { width: 30px; padding: 0; justify-content: center; }
.lib-tool.primary { background: var(--accent); border-color: transparent; color: var(--text-on-accent); }
.lib-tool.primary:hover { background: var(--accent-hover); border-color: transparent; }
.lib-tool.danger { color: var(--danger); border-color: color-mix(in srgb, var(--danger) 38%, var(--border-strong)); }
.lib-tool.danger:hover:not(:disabled) { background: var(--danger-soft); }
.lib-tool:disabled { opacity: .5; cursor: default; }

/* 搜索框：与工具行同一排（左中右三段）。flex:1 让它在窄窗口里**第一个收窄**，
   右侧按钮组 flex:none 固定宽；但收窄有个底线（min-width），到底了就让整行 flex-wrap 换行，
   而不是把输入框压成一条缝（900px + 分类 chip 同时在场时就会到这条线）。
   横向滚动不会出现：任何一项都不许超过它自己的 max/min 宽度。 */
.lib-search {
  flex: 1 1 200px; min-width: 168px;
  display: flex; align-items: center; gap: 8px;
  height: 30px; padding: 0 12px;
  border-radius: var(--radius-ctl);
  background: var(--bg-sunken); border: 1px solid var(--border); color: var(--text-faint);
  transition: background var(--dur) var(--ease), border-color var(--dur) var(--ease), box-shadow var(--dur) var(--ease);
}
.lib-search:focus-within { border-color: var(--accent); box-shadow: var(--ring); background: var(--bg-elev); }
.lib-search input {
  width: 100%; min-width: 0; height: 100%; padding: 0;
  border: 0; border-radius: 0; background: transparent; box-shadow: none;
  font-size: calc(13px * var(--ts, 1));
}
.lib-search input:hover, .lib-search input:focus { background: transparent; box-shadow: none; border: 0; }

/* ---------------- 批量工具条（顶部第三行，只在批量模式出现） ---------------- */
.lib-batch {
  display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
  margin-bottom: 12px; padding: 7px 10px;
  border: 1px solid var(--border); border-radius: var(--radius);
  background: var(--bg-sunken);
}
.lib-batch-x {
  flex: none; padding: 4px 9px; border-radius: var(--radius-ctl);
  color: var(--text-dim); font-family: inherit; font-size: calc(12px * var(--ts, 1)); cursor: pointer;
}
.lib-batch-x:hover { background: var(--bg-hover); color: var(--text); }
.lib-batch-count { flex: none; font-size: calc(12px * var(--ts, 1)); color: var(--text-dim); }
.lib-batch-all {
  flex: none; height: 26px; padding: 0 11px; border-radius: 999px;
  border: 1px solid var(--border); background: transparent; color: var(--text);
  font-family: inherit; font-size: calc(12px * var(--ts, 1)); cursor: pointer;
}
.lib-batch-all:hover:not(:disabled) { background: var(--bg-hover); }
.lib-batch-all:disabled { opacity: .5; cursor: default; }

.lib-filter { position: relative; }

/* 筛选浮层：与 Menu 浮层同一套语言（同一个"浮起来的东西"该长什么样） */
.lib-pop {
  position: absolute; right: 0; top: calc(100% + 8px); z-index: 60; width: 268px;
  max-height: min(70vh, 520px); overflow-y: auto;      /* 分类一多也要能滚，别顶出视口 */
  padding: 14px; display: grid; gap: 8px;
  background: var(--bg-elev); border: 1px solid var(--border-strong);
  border-radius: var(--radius); box-shadow: var(--shadow-flyout);
}
.lib-pop-label { font-size: calc(11px * var(--ts, 1)); font-weight: 600; letter-spacing: .02em; color: var(--text-dim); }
.lib-pills { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 4px; }
.lib-pill {
  display: inline-flex; align-items: center; height: 26px; padding: 0 11px;
  border-radius: 999px; border: 1px solid var(--border); background: var(--bg-elev);
  color: var(--text-dim); font-family: inherit; font-size: calc(12px * var(--ts, 1)); cursor: pointer;
  transition: background var(--dur) var(--ease), border-color var(--dur) var(--ease), color var(--dur) var(--ease);
}
.lib-pill:hover { color: var(--text); border-color: var(--border-strong); }
.lib-pill.on { background: var(--accent); border-color: var(--accent); color: var(--text-on-accent); font-weight: 600; }
.lib-clear {
  justify-self: start; padding: 2px 0; font-family: inherit; font-size: calc(12px * var(--ts, 1));
  color: var(--text-dim); cursor: pointer;
}
.lib-clear:hover { color: var(--text); text-decoration: underline; }

.lib-sort {
  display: inline-flex; align-items: center; height: 30px; padding: 0 8px;
  border-radius: var(--radius-ctl);
  transition: background var(--dur) var(--ease);
}
.lib-sort:hover { background: var(--bg-hover); }
.lib-sort select {
  height: 100%; max-width: 148px; padding: 0 4px;
  border: 0; border-radius: 0; background: transparent; box-shadow: none;
  font-size: calc(12px * var(--ts, 1)); color: var(--text); cursor: pointer;
}
.lib-sort select:hover, .lib-sort select:focus { background: transparent; box-shadow: none; border: 0; }

.lib-divider { width: 1px; height: 20px; background: var(--divider); }

.lib-view {
  display: inline-flex; align-items: center; gap: 2px; height: 30px; padding: 2px;
  border-radius: var(--radius-ctl); background: var(--bg-sunken); border: 1px solid var(--border);
}
.lib-view button {
  display: inline-flex; align-items: center; justify-content: center;
  width: 26px; height: 24px; border-radius: var(--radius-ctl);
  color: var(--text-faint); cursor: pointer;
  transition: background var(--dur) var(--ease), color var(--dur) var(--ease);
}
.lib-view button:hover { color: var(--text); }
.lib-view button.on { background: var(--bg-elev); color: var(--text); box-shadow: var(--shadow-1); }

/* ---------------- 内容区：封面网格 / 列表 ----------------
 * 列宽下限 168px：宽屏一行 4–6 张封面（图1 就是这一档密度），窄屏自然降到 2 列。
 * 页面里已没有 fixed 的浮条，底部只留一点呼吸位。 */
.lib-grid {
  display: grid; gap: 22px 18px; align-items: start;
  grid-template-columns: repeat(auto-fill, minmax(168px, 1fr));
  padding-bottom: 12px;
}
.sk-card { min-width: 0; }

.lib-list { display: flex; flex-direction: column; gap: 4px; padding-bottom: 12px; }

/* ---------------- 分组（只在「按分类」排序时出现） ----------------
 * 标题行 34px、与下方卡片隔 10px；组与组之间 20px。段内的 .lib-grid / .lib-list 不再自带
 * 底部留白，否则那 12px 会和这里的 gap 叠成 32px。 */
.lib-groups { display: flex; flex-direction: column; gap: 20px; min-width: 0; padding-bottom: 12px; }
.lib-group { min-width: 0; }
.lib-groups .lib-grid, .lib-groups .lib-list { padding-bottom: 0; }
.lib-group-head {
  display: flex; align-items: center; gap: 6px; width: 100%; height: 34px;
  padding: 0 8px; margin-bottom: 10px;
  border: 0; border-radius: var(--radius-ctl); background: transparent; color: var(--text);
  font-family: inherit; font-size: calc(13px * var(--ts, 1)); font-weight: 600;
  text-align: left; cursor: pointer;
  transition: background var(--dur) var(--ease);
}
.lib-group-head:hover { background: var(--bg-hover); }
.lib-group-head:focus-visible { outline: none; box-shadow: var(--ring); }
.lib-group-caret { flex: none; color: var(--text-faint); }
.lib-group-head:hover .lib-group-caret { color: var(--text); }
.lib-group-name { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.lib-group-count { flex: none; font-size: calc(11.5px * var(--ts, 1)); font-weight: 500; color: var(--text-faint); }
.lib-list { display: flex; flex-direction: column; gap: 4px; padding-bottom: 12px; }

/* ---------------- 移到分类浮层 ----------------
 * 与筛选同一个 .lib-pop；坐标由锚点按钮现算，是视口坐标，所以走 fixed。 */
.lib-pop.is-move {
  position: fixed; right: auto; top: 0; left: 0; z-index: 300;
  width: 240px; max-height: min(64vh, 420px); overflow-y: auto;
  gap: 2px; padding: 8px;
}
.lib-folder {
  display: flex; align-items: center; gap: 7px; width: 100%;
  padding: 7px 9px; border: 0; border-radius: var(--radius-ctl);
  background: transparent; color: var(--text);
  font-family: inherit; font-size: calc(12.5px * var(--ts, 1)); text-align: left; cursor: pointer;
  transition: background var(--dur-fast) var(--ease), color var(--dur-fast) var(--ease);
}
.lib-folder:hover:not(:disabled) { background: var(--bg-hover); }
.lib-folder:focus-visible { outline: none; box-shadow: var(--ring); }
.lib-folder:disabled { opacity: .5; cursor: default; }
.lib-folder .icon { flex: none; color: var(--text-dim); }
.lib-folder-name { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.lib-folder-num { flex: none; font-size: calc(11px * var(--ts, 1)); color: var(--text-faint); }
.lib-folder.create { color: var(--text-dim); }
.lib-folder-input {
  width: 100%; height: 30px; padding: 0 9px; border-radius: var(--radius-ctl);
  font-family: inherit; font-size: calc(12.5px * var(--ts, 1));
}
.lib-folder-err { margin: 2px 0 0; font-size: calc(11px * var(--ts, 1)); color: var(--danger); }
.lib-sep { height: 1px; margin: 4px 6px; background: var(--divider); }

.confirm-text { margin: 0 0 8px; font-size: calc(13px * var(--ts, 1)); color: var(--text-dim); line-height: 1.6; }
.confirm-target {
  margin: 0; padding: 10px 12px; font-size: calc(13px * var(--ts, 1)); font-weight: 600;
  background: var(--bg-sunken); border-radius: var(--r-sm); overflow-wrap: anywhere;
}

.drop-zone-inline {
  display: flex; flex-direction: column; align-items: center; gap: 8px;
  width: min(460px, 100%); margin-top: 4px; font-size: calc(13px * var(--ts, 1));
}
.drop-zone-inline .icon { color: var(--text-faint); }
.drop-zone-inline.over .icon { color: var(--accent); }

.es-icon.bad { background: var(--danger-soft); color: var(--danger); border-color: transparent; }
.load-error { color: var(--danger); }

/* ---------------- 导入弹窗：解析引擎 + 三个开关 ----------------
 * 引擎用"块状二选一"（`.seg` 是紧凑胶囊，塞不下一行说明），所以按 `.btn` 的观感自绘：
 * 同样的描边 / 圆角 / 高度语言，选中态用 `--accent-soft` + `--accent-text`（与设置页分区药丸一致）。 */
.engine { margin-top: 12px; }
.engine-label { font-size: calc(12px * var(--ts, 1)); color: var(--text-dim); margin-bottom: 5px; }
.engine-row { display: flex; gap: 8px; flex-wrap: wrap; }
.engine-opt {
  flex: 1 1 200px; min-width: 0; text-align: left;
  display: flex; flex-direction: column; gap: 3px;
  padding: 9px 12px; cursor: pointer;
  font-family: inherit; color: var(--text);
  background: var(--bg-elev); border: 1px solid var(--border-strong);
  border-radius: var(--r-sm);
  transition: background var(--dur) var(--ease), border-color var(--dur) var(--ease),
              box-shadow var(--dur) var(--ease);
}
.engine-opt:hover { background: var(--bg-hover); border-color: color-mix(in srgb, var(--accent) 34%, var(--border-strong)); }
.engine-opt:focus-visible { outline: none; border-color: var(--accent); box-shadow: var(--ring); }
.engine-opt.active {
  background: var(--accent-soft);
  border-color: color-mix(in srgb, var(--accent) 55%, transparent);
}
.engine-name { font-size: calc(13px * var(--ts, 1)); font-weight: 600; }
.engine-opt.active .engine-name { color: var(--accent-text); }
.engine-note { font-size: calc(11.5px * var(--ts, 1)); color: var(--text-faint); line-height: 1.5; }
.engine-warn {
  display: flex; align-items: flex-start; gap: 6px;
  margin: 8px 0 0; color: var(--danger);
}
.engine-warn .icon { margin-top: 2px; }

/* 三个开关的文案：**标签一行、说明另起一行**。
 * 旧版把说明接在标签后面用行内流排，长说明会折到轨道下面、缩进也对不齐（用户看到的
 * "（在线解析由 MinerU 决定…）"掉到第三行）。两行都从文字列开始，读起来是一条一条的。 */
.opt { display: flex; flex-direction: column; gap: 2px; }
.opt-text { font-size: calc(12.5px * var(--ts, 1)); color: var(--text); }
.opt-note { font-size: calc(11.5px * var(--ts, 1)); color: var(--text-faint); line-height: 1.5; }

/* 引擎块与第一个开关之间补出行距：`.switch-row + .switch-row` 只覆盖连续两个开关，
 * 而"引擎 → 第一个开关"这一对在 DOM 上是跨元素的（`<div class="engine">` 之后）。 */
.engine + .switch-row { margin-top: 12px; }

/* ---------------- 导入弹窗：在线解析参数 ----------------
 * 用一条**左边框 + 浅底**把它和"通用元数据/引擎"分开：里面每一项都是 MinerU 专属的旋钮，
 * 与上面的插图开关不是一回事（那些只对本地解析生效）。 */
.online-params {
  margin-top: 12px; padding: 10px 12px 12px;
  background: var(--bg-sunken);
  border: 1px solid var(--border);
  border-left: 3px solid var(--accent);
  border-radius: var(--r-sm);
}
.op-head { font-size: calc(12px * var(--ts, 1)); font-weight: 600; color: var(--text); margin-bottom: 8px; }
.op-hint { font-weight: 400; color: var(--text-faint); }
/* 一行说明：初值来自设置页，免得被当成这篇的固定参数 */
.op-note { margin: 2px 0 6px; font-size: calc(11.5px * var(--ts, 1)); line-height: 1.5; color: var(--text-faint); }
.op-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
.op-grid .field { margin-top: 0; }
/* 参数区里的开关行比外面那些挨得紧一点（区块本身已经把范围框出来了） */
.online-params .switch-row + .switch-row { margin-top: 8px; }
.online-params .switch-row:first-of-type { margin-top: 10px; }
.online-params .field { margin-top: 10px; }
</style>
