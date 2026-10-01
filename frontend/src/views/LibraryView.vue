<script setup lang="ts">
/**
 * 论文库：导入 → 解析 → 翻译 → 三视图阅读。
 *
 * 版式（移植自参照项目 retain-pdf 的书库页）：**顶部工具栏**（批量 / 筛选 / 排序 / 视图切换）
 * + **封面卡片网格**（列表视图可切成一行一条）+ **底部悬浮条**（导入 / 搜索 / 刷新 / 设置）。
 * 卡片本体在 `components/paper/PaperCard.vue`，点卡片打开的详情在 `PaperDetailModal.vue`。
 *
 * REST 走 `@/api/client`，列表与计数走全局 store，弹窗走 `Modal`。
 * 删除语义：软删除会先清理索引与知识库记录，PDF 文件移入 data/trash（不物理删除）。
 */
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue';
import { RouterLink, useRouter } from 'vue-router';
import { api, assetUrl } from '@/api/client';
import type { MineruConfig, MineruModelVersion, Paper, ParseEngine } from '@/api/types';
import Modal from '@/components/Modal.vue';
import ConfirmModal from '@/components/ConfirmModal.vue';
import Switch from '@/components/Switch.vue';
import AppIcon from '@/components/AppIcon.vue';
import PaperCard from '@/components/paper/PaperCard.vue';
import PaperDetailModal from '@/components/paper/PaperDetailModal.vue';
import {
  loadPapers, reportError, state, toast, withLoading,
} from '@/stores/app';
import { openTab } from '@/stores/tabs';
import { fmtRelative, paperStatus } from '@/utils/format';
import { confirmRegenerateInsight } from '@/utils/understanding';

const router = useRouter();

/** 打开论文 = **开一个新标签页**（`openTab` 负责登记，重复点同一篇只会激活已有标签）。 */
function openPaper(id: string) {
  void router.push(openTab({ name: 'reader', params: { paperId: id } }) as never);
}

/** 详情弹窗当前看的那篇（空串 = 关着）。点卡片本体进这里，点「阅读」直接进阅读器。 */
const detailId = ref('');

/* ------------------------------ 筛选与列表 ------------------------------ */

const keyword = ref('');
const statusFilter = ref('');
const sort = ref('last_read_at');
const order = ref('desc');
/** 正在进行的行内操作键：`import` / `insight:<id>` / `tr:<id>` / `idx:<id>` / `del:<id>`。 */
const busyKey = ref('');
/** 列表加载失败原因（非空时界面显式给出错误卡片 + 重试）。 */
const loadError = ref('');

/** 状态筛选项（与后端 PaperStatus 取值一致）。 */
const STATUS_OPTIONS = ['imported', 'parsed', 'translating', 'translated', 'indexed', 'failed'] as const;

/** 排序：把「字段 + 方向」合并成一个下拉，页面上少一个冗余控件。 */
const SORT_OPTIONS = [
  { value: 'last_read_at:desc', label: '最近阅读优先' },
  { value: 'created_at:desc', label: '最新导入' },
  { value: 'created_at:asc', label: '最早导入' },
  { value: 'title:asc', label: '标题 A→Z' },
] as const;
const sortKey = computed({
  get: () => `${sort.value}:${order.value}`,
  set: (v: string) => {
    const [s, o] = v.split(':');
    sort.value = s || 'last_read_at';
    order.value = o === 'asc' ? 'asc' : 'desc';
  },
});

const filtered = computed<Paper[]>(() => state.papers);
const loading = computed(() => !!state.loading.papers);

/* ------------------------------ 视图形态 ------------------------------
 * 封面网格 = 参照项目图1 的书库版式（一眼看到论文长什么样）；列表 = 一行一条（扫标题快）。
 * 形态记在本机：同一个人不该每次进来都重选一遍。
 */

const VIEW_KEY = 'pm.library.view';
const viewMode = ref<'grid' | 'list'>(localStorage.getItem(VIEW_KEY) === 'list' ? 'list' : 'grid');

function setView(v: 'grid' | 'list') {
  viewMode.value = v;
  try { localStorage.setItem(VIEW_KEY, v); } catch { /* 隐私模式：不留也无妨 */ }
}

/* ------------------------------ 筛选 ------------------------------
 * 状态走**后端筛**（`refresh()` 带上 status 参数），标签走**前端筛** ——
 * 论文标签是自由文本（导入时手填），没有后端字典，也没必要为它做索引查询。
 */

const filterOpen = ref(false);
const filterBox = ref<HTMLElement | null>(null);
const tagFilter = ref('');
const filterCount = computed(() => (statusFilter.value ? 1 : 0) + (tagFilter.value ? 1 : 0));

/** 标签候选：从**当前列表**现收（库里没标签就不显示这一组）。 */
const tagOptions = computed(() => {
  const set = new Set<string>();
  for (const p of filtered.value) for (const t of p.tags || []) set.add(t);
  return [...set].sort((a, b) => a.localeCompare(b, 'zh'));
});

/** 界面真正渲染的那一份（后端结果 → 标签筛选）。 */
const visible = computed<Paper[]>(() => (
  tagFilter.value
    ? filtered.value.filter((p) => (p.tags || []).includes(tagFilter.value))
    : filtered.value
));

function setStatus(s: string) {
  statusFilter.value = s;
  void refresh();
}

function toggleTag(t: string) {
  tagFilter.value = tagFilter.value === t ? '' : t;
}

function clearFilters() {
  statusFilter.value = '';
  tagFilter.value = '';
  void refresh();
}

/** 浮层：点外面 / 按 Esc 收起（与 Menu 的关闭口径一致）。 */
function onDocPointer(ev: PointerEvent) {
  if (!filterOpen.value) return;
  if (filterBox.value?.contains(ev.target as Node)) return;
  filterOpen.value = false;
}

function onDocKey(ev: KeyboardEvent) {
  if (ev.key === 'Escape') filterOpen.value = false;
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
const showEmpty = computed(() => !loading.value && visible.value.length === 0 && !loadError.value);
/** 有论文、只是被筛掉了：这种"空"要说明是筛选造成的，别显示成"还没有论文"。 */
const filteredOut = computed(() => !loading.value && visible.value.length === 0 && filtered.value.length > 0);

/** 拉取论文列表：加载中 / 请求失败 / 空列表三种状态都在模板里显式呈现。 */
async function refresh() {
  loadError.value = '';
  try {
    await withLoading('papers', () => loadPapers({
      q: keyword.value || undefined,
      status: statusFilter.value || undefined,
      sort: sort.value,
      order: order.value,
    }), { silent: true });
  } catch (err) {
    loadError.value = (err as Error)?.message ?? String(err);
    reportError(err, '论文列表加载失败');
  }
}

onMounted(async () => {
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
  importForm.figure_caption = false;
  importForm.deep_figure_search = false;
  importForm.parse_engine = mineruDefaultEngine.value;
  importForm.mineru_model_version = mineruDefaultModel.value;
  importForm.mineru_language = mineruDefaultLanguage.value;
  importForm.mineru_is_ocr = MINERU_DEFAULTS.is_ocr;
  importForm.mineru_enable_formula = MINERU_DEFAULTS.enable_formula;
  importForm.mineru_enable_table = MINERU_DEFAULTS.enable_table;
  importForm.mineru_page_ranges = MINERU_DEFAULTS.page_ranges;
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

/**
 * 「重新生成解读」：让后端把两路理解重跑一遍（`paths: ['a','b'] + force`）。
 *
 * 已经有解读时先 HTML 原生 confirm 二次确认（与「重新翻译」同一风格）。这份口径**不写在这里**：
 * 论文库 / 阅读器 / 理解页三处都有这个动作，文案与"要不要问"的判据统一放在
 * `utils/understanding.ts`，三处必须一模一样。
 */
async function askRegenerateInsight(paper: Paper) {
  if (!(await confirmRegenerateInsight(paper.id, paper.title))) return;
  await runInsight(paper);
}

async function runInsight(paper: Paper) {
  busyKey.value = `insight:${paper.id}`;
  try {
    const res = await api.post<any>(`/papers/${paper.id}/understand`, { paths: ['a', 'b'], force: true });
    toast(metaMessage(res) || '解读任务已排队（两路并发）', 'ok', 4200);
    await refresh();
  } catch (err) {
    /* 后端 400 会给一句**可执行的人话**（"这篇论文还没有可理解的 chunk：先建索引（知识库页）再来理解"），
     * 原样 toast：加前缀、或按普通失败静默掉，都会让这条指引失效（用户口径：不要静默失败）。 */
    toast((err as Error)?.message ?? String(err), 'error', 6000);
    console.warn('重新生成解读失败', err);
  } finally { busyKey.value = ''; }
}

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

/** 从知识库移除：清向量 + 清 chunk 元数据（论文、译文、笔记、高亮都不动）。
 *  「纳入」那个开关已经取消 —— **建立索引即纳入**（用户口径：合并冗余说法与按钮）；
 *  「建立索引」按钮本身也下线了：翻译跑完自动建索引，所以这里告诉用户怎么加回来。 */
async function removeFromKb(paper: Paper) {
  if (!confirm(`从知识库移除《${paper.title}》？\n\n`
    + '会清掉它的 chunk 元数据与向量（论文、译文、笔记、高亮不受影响）。'
    + '重新翻译一次（或去「设置 → 索引管理」）即可加回来。')) return;
  busyKey.value = `remove:${paper.id}`;
  try {
    const res = await api.post<{ removed_chunks?: number }>('/knowledge/remove', { paper_id: paper.id });
    toast(`已从知识库移除（清 ${res?.removed_chunks ?? 0} 条 chunk）`, 'ok', 3200);
    await refresh();
  } catch (err) { reportError(err, '移除失败'); } finally { busyKey.value = ''; }
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

/** 次要操作（重新翻译 / 重新生成解读 / 知识库 / 编辑 / 删除）收进「⋯」菜单：
 * 卡片上只留「阅读（主）+ 翻译（次）」两个按钮 —— 翻译与解析**不再并列暴露**（选项 B）：
 * 未解析时点「翻译」由后端先把解析串起来（解析完自动接着翻译）。
 *
 * ⚠️ 「重新解析」已按用户口径下线（解析由翻译入口自动串起来，单独重解析只会让产物半新半旧）；
 * 原位置换成「重新生成解读」（与阅读器工具栏「⋯」里那一项同名，同一个后端动作）。
 *
 * ⚠️ 「建立索引（纳入知识库）」也已按用户口径**下线**（2026-09-27）：翻译任务完成后由后端
 * 自动开 INDEX 任务，索引完成后再自动生成两路解读（见 `handlers._chain_index` /
 * `_chain_understand`）。要手动重建仍可在「设置 → 索引管理」里做（那是排障入口）。
 */
function rowMenu(p: Paper): RowAction[] {
  const busy = (k: string) => busyKey.value === `${k}:${p.id}`;
  return [
    ...(stage(p) === 'translated'
      ? [{ key: 'retranslate', label: '重新翻译', icon: 'refresh' } as RowAction]
      : []),
    { key: 'insight', label: '重新生成解读', icon: 'sparkle', disabled: busy('insight') },
    {
      key: 'remove-kb',
      label: '从知识库移除',
      icon: 'link-off',
      danger: true,
      disabled: busy('remove') || !p.in_knowledge_base,
    },
    { key: 'edit', label: '编辑元数据', icon: 'edit' },
    { key: 'delete', label: '删除（移入回收站）', icon: 'trash', danger: true },
    { key: 'purge', label: '彻底删除', icon: 'trash', danger: true },
  ];
}

/** 重新翻译（忽略已有产物、按页重跑）：用**HTML 原生 confirm** 二次确认（用户口径）。
 *  删除那条路仍然走站内弹窗（不可逆、还要显示论文名与影响范围）；重译只是"重跑一遍"，
 *  原生确认足够，也不用为一个确认框维护状态。 */
function askRetranslate(p: Paper) {
  const pages = p.translate_total || 0;
  const ok = window.confirm(
    `重新翻译《${p.title || p.id}》？\n\n`
    + `会忽略已有的 ${pages} 页译文，从第 1 页重跑（现有对齐结果会被覆盖）。`);
  if (!ok) return;
  void runTranslate(p, 'retranslate');
}

function onRowAction(p: Paper, key: string) {
  if (key === 'retranslate') { askRetranslate(p); return; }
  if (key === 'insight') { void askRegenerateInsight(p); return; }
  if (key === 'remove-kb') { removeFromKb(p); return; }
  if (key === 'edit') { openEdit(p); return; }
  if (key === 'delete') { askRemove(p, false); return; }
  if (key === 'purge') { askRemove(p, true); }
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

/** 卡片徽章：有任务看任务（翻译中 / 已暂停），没任务看论文行。 */
function cardStatus(p: Paper): { label: string; cls: string } {
  const t = activeTaskOf(p);
  if (!t) return paperStatus(p.status);
  return t.status === 'paused'
    ? { label: '已暂停', cls: 'warn' }
    : { label: '翻译中', cls: 'accent' };
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
  hard: boolean;
  paper: Paper | null;
}

const confirmBox = reactive<ConfirmState>({ open: false, hard: false, paper: null });

/** 删除不可逆：用站内弹窗确认（不再用原生 window.confirm，风格与动效统一）。 */
function askRemove(paper: Paper, hard = false) {
  confirmBox.paper = paper;
  confirmBox.hard = hard;
  confirmBox.open = true;
}

async function doRemove() {
  const p = confirmBox.paper;
  if (!p) return;
  const hard = confirmBox.hard;
  confirmBox.open = false;
  busyKey.value = `del:${p.id}`;
  try {
    const res = await api.del<DeleteResult>(`/papers/${p.id}`, { hard });
    toast(`${hard ? '已彻底删除' : '已删除'} ${p.id}${res?.steps?.length ? `（${res.steps.length} 步）` : ''}`, 'ok');
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
    toast(`已删除 ${ok} 篇（移入 data/trash，可从回收站恢复）`, 'ok', 3200);
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
}

const editOpen = ref(false);
const editTarget = ref<Paper | null>(null);
const editForm = reactive<EditForm>({
  title: '', authors: '', venue: '', year: '', tags: '', abstract: '',
});

function openEdit(paper: Paper) {
  editTarget.value = paper;
  Object.assign(editForm, {
    title: paper.title || '',
    authors: paper.authors || '',
    venue: paper.venue || '',
    year: paper.year === null || paper.year === undefined ? '' : String(paper.year),
    tags: (paper.tags || []).join(','),
    abstract: paper.abstract || '',
  });
  editOpen.value = true;
}

/* ------------------------------ 自动补全元数据 ------------------------------ */
/** `''` 空闲 / `'pdf'` 正在离线读 PDF / `'model'` 正在问模型 */
const metaBusy = ref<'' | 'pdf' | 'model'>('');

/**
 * 一键补全：先离线读 PDF（会议、年份、摘要、标题都能从首页读出来），
 * 还有缺的再交给模型（与术语同一次产出，见后端 `metadata_fill`）。
 * 后端**只填空字段**，所以这个按钮不会覆盖已经填好的值。
 */
async function autoMeta(model: boolean) {
  const p = editTarget.value;
  if (!p || metaBusy.value) return;
  metaBusy.value = model ? 'model' : 'pdf';
  try {
    const res = await api.post<{ paper: Paper; applied: Record<string, unknown> }>(
      `/papers/${p.id}/metadata/auto`, { model });
    const np = res?.paper;
    if (np) {
      Object.assign(editForm, {
        title: np.title || editForm.title,
        authors: np.authors || editForm.authors,
        venue: np.venue || editForm.venue,
        year: np.year === null || np.year === undefined ? editForm.year : String(np.year),
        tags: (np.tags || []).join(','),
        abstract: np.abstract || editForm.abstract,
      });
    }
    toast(metaMessage(res) || '已自动补全', 'ok');
    await refresh();
  } catch (err) { reportError(err, '自动补全失败'); } finally { metaBusy.value = ''; }
}

async function submitEdit() {
  const p = editTarget.value;
  if (!p) return;
  const payload = {
    title: editForm.title,
    authors: editForm.authors,
    venue: editForm.venue,
    abstract: editForm.abstract,
    tags: editForm.tags,
    year: editForm.year === '' ? null : Number(editForm.year),
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
    <!-- 顶部工具栏（图1 版式）：左标题 + 计数，右 批量 / 筛选 / 排序 / 视图切换。
         搜索与导入搬到底部悬浮条（图1 的搜索岛）—— 这里只留"看"与"筛"。 -->
    <header class="lib-toolbar">
      <div class="lib-heading">
        <h1>论文库</h1>
        <span class="lib-count tnum">共 {{ state.papersTotal }} 篇</span>
      </div>

      <div class="lib-tools">
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
            <div class="lib-pop-label">处理状态</div>
            <div class="lib-pills">
              <button type="button" class="lib-pill" :class="{ on: statusFilter === '' }" @click="setStatus('')">全部</button>
              <button
                v-for="s in STATUS_OPTIONS"
                :key="s"
                type="button"
                class="lib-pill"
                :class="{ on: statusFilter === s }"
                @click="setStatus(s)"
              >{{ paperStatus(s).label }}</button>
            </div>

            <template v-if="tagOptions.length">
              <div class="lib-pop-label">标签</div>
              <div class="lib-pills">
                <button type="button" class="lib-pill" :class="{ on: tagFilter === '' }" @click="tagFilter = ''">全部</button>
                <button
                  v-for="t in tagOptions"
                  :key="t"
                  type="button"
                  class="lib-pill"
                  :class="{ on: tagFilter === t }"
                  @click="toggleTag(t)"
                >{{ t }}</button>
              </div>
            </template>

            <button v-if="filterCount" type="button" class="lib-clear" @click="clearFilters">清空筛选</button>
          </div>
        </div>

        <label class="lib-sort">
          <span class="sr-only">排序</span>
          <select v-model="sortKey" title="排序方式" @change="refresh">
            <option v-for="o in SORT_OPTIONS" :key="o.value" :value="o.value">{{ o.label }}</option>
          </select>
        </label>

        <span class="lib-divider" />

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
      <span class="es-hint">当前的状态 / 标签筛选下没有结果。</span>
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

      <!-- 封面网格（图1 版式）/ 列表：同一份数据两种排布，卡片本体见 PaperCard.vue -->
      <div :class="viewMode === 'grid' ? 'lib-grid' : 'lib-list'">
        <PaperCard
          v-for="p in visible"
          :key="p.id"
          :variant="viewMode"
          :title="p.title"
          :cover-url="coverUrl(p)"
          :badge="cardStatus(p)"
          :meta="cardMeta(p)"
          :progress="progressOf(p)"
          :foot-note="cardFoot(p)"
          :running="!!activeTaskOf(p)"
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
    </template>

    <!-- 论文详情（图2）：点卡片打开；详情里改过东西（阅读状态 / 删除）就刷新列表 -->
    <PaperDetailModal
      v-if="detailId"
      :key="detailId"
      :paper-id="detailId"
      @close="detailId = ''"
      @changed="refresh"
    />
    <!-- ---------------- 底部悬浮条（图1 的搜索岛） ----------------
         导入（+）/ 搜索 / 刷新 / 设置；`fixed` 定位在视口底部居中，与内容滚动无关。
         批量模式时它让位给下面那条批量工具条（两边同一套定位与观感）。 -->
    <div class="lib-dock" :class="{ away: batchMode }">
      <button type="button" class="lib-dock-btn primary" title="导入 PDF" @click="openImport">
        <AppIcon name="add" :size="18" />
      </button>
      <label class="lib-dock-search">
        <AppIcon name="search" :size="15" />
        <input
          v-model="keyword"
          type="search"
          placeholder="搜索标题、作者、会议或年份"
          @keyup.enter="refresh"
        />
      </label>
      <button type="button" class="lib-dock-btn" title="刷新列表" :disabled="loading" @click="refresh">
        <AppIcon name="refresh" :size="17" />
      </button>
      <button type="button" class="lib-dock-btn" title="设置" @click="router.push('/settings')">
        <AppIcon name="settings" :size="17" />
      </button>
    </div>

    <!-- 批量工具条：替代搜索岛（不是叠在它上面），动作都要先过确认 -->
    <div v-if="batchMode" class="lib-dock is-batch" role="toolbar" aria-label="批量操作">
      <button type="button" class="lib-dock-cancel" @click="toggleBatch">取消</button>
      <span class="lib-dock-count tnum">已选 {{ selectedCount }} / {{ visible.length }}</span>
      <button type="button" class="lib-dock-select" :disabled="!visible.length" @click="selectAll">
        {{ allSelected ? '取消全选' : '全选' }}
      </button>
      <span class="spacer" />
      <button
        type="button"
        class="lib-dock-action"
        :disabled="!selectedCount || batchBusy"
        @click="askBatch('translate')"
      >
        <AppIcon name="translate" :size="14" />翻译
      </button>
      <button
        type="button"
        class="lib-dock-action danger"
        :disabled="!selectedCount || batchBusy"
        @click="askBatch('delete')"
      >
        <AppIcon name="trash" :size="14" />删除
      </button>
    </div>

    <!-- 批量操作的确认（与单篇删除同一套弹窗，口径一致） -->
    <ConfirmModal
      :open="batchBox.open"
      :title="batchBox.kind === 'delete' ? '批量删除论文' : '批量翻译'"
      :body="batchBox.kind === 'delete'
        ? `会把选中的 ${selectedCount} 篇移入 data/trash（可从回收站恢复），并清理索引与知识库记录。`
        : `会为选中的 ${selectedCount} 篇各起一个翻译任务；已在跑或已译完的会跳过。`"
      :confirm-label="batchBox.kind === 'delete' ? '删除' : '开始翻译'"
      :danger="batchBox.kind === 'delete'"
      @confirm="confirmBatch"
      @cancel="batchBox.open = false"
    />

    <!-- 删除确认（站内弹窗，不用原生 confirm） -->
    <ConfirmModal
      :open="confirmBox.open"
      :title="confirmBox.hard ? '彻底删除论文' : '删除论文'"
      :body="confirmBox.hard
        ? '这一步不可撤销：数据库记录与论文文件都会被删除。'
        : '会先清理索引与知识库记录，PDF 文件移入 data/trash（可从回收站恢复）。'"
      :target="confirmBox.paper?.title || ''"
      :confirm-label="confirmBox.hard ? '彻底删除' : '删除'"
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
        <div class="op-head">在线解析参数<span class="op-hint">（只对这篇论文生效）</span></div>
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
    <Modal :open="editOpen" title="编辑元数据" @close="editOpen = false">
      <div class="grid cols-2">
        <label class="field">标题<input v-model="editForm.title" /></label>
        <label class="field">作者<input v-model="editForm.authors" /></label>
        <label class="field">会议 / 期刊<input v-model="editForm.venue" /></label>
        <label class="field">年份<input v-model="editForm.year" /></label>
        <label class="field">标签（逗号分隔）<input v-model="editForm.tags" /></label>
      </div>
      <label class="field" style="margin-top: 10px">
        摘要<textarea v-model="editForm.abstract" rows="4"></textarea>
      </label>
      <div class="tiny faint" style="margin-top: 6px">
        会议 / 期刊、年份、摘要、标签都可以自动补全：先读 PDF（离线、不花钱），读不出来再交给模型。
      </div>
      <template #footer>
        <button class="btn sm" :disabled="!!metaBusy" title="从 PDF 首页读出会议、年份、摘要（离线）" @click="autoMeta(false)">
          <AppIcon name="sparkle" :size="13" />{{ metaBusy === 'pdf' ? '识别中…' : '从论文识别' }}
        </button>
        <button class="btn sm" :disabled="!!metaBusy" title="PDF 里读不出来的字段交给模型判断" @click="autoMeta(true)">
          <AppIcon name="model" :size="13" />{{ metaBusy === 'model' ? '补全中…' : '模型补全' }}
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
  display: flex; align-items: center; justify-content: space-between;
  gap: 12px; flex-wrap: wrap;
  margin-bottom: 16px; padding-bottom: 12px;
  border-bottom: 1px solid var(--divider);
}
.lib-heading { display: flex; align-items: baseline; gap: 8px; min-width: 0; }
.lib-heading h1 {
  margin: 0; font-size: 16px; font-weight: 600; letter-spacing: -.02em;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.lib-count {
  flex: none; display: inline-flex; align-items: center; height: 20px; padding: 0 8px;
  border-radius: 999px; background: var(--bg-subtle); border: 1px solid var(--border);
  font-size: 11px; color: var(--text-dim);
}
.lib-tools { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }

/* 工具条控件统一 30px 高：同一排里按钮 / 下拉 / 分段不许参差 */
.lib-tool {
  display: inline-flex; align-items: center; gap: 6px;
  height: 30px; padding: 0 12px; border-radius: var(--radius-ctl);
  border: 1px solid var(--border); background: transparent; color: var(--text);
  font-family: inherit; font-size: 12px; font-weight: 500; cursor: pointer;
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
.lib-tool-num { font-size: 11px; color: var(--accent-text); }

.lib-filter { position: relative; }

/* 筛选浮层：与 Menu 浮层同一套语言（同一个"浮起来的东西"该长什么样） */
.lib-pop {
  position: absolute; right: 0; top: calc(100% + 8px); z-index: 60; width: 268px;
  padding: 14px; display: grid; gap: 8px;
  background: var(--bg-elev); border: 1px solid var(--border-strong);
  border-radius: var(--radius); box-shadow: var(--shadow-flyout);
}
.lib-pop-label { font-size: 11px; font-weight: 600; letter-spacing: .02em; color: var(--text-dim); }
.lib-pills { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 4px; }
.lib-pill {
  display: inline-flex; align-items: center; height: 26px; padding: 0 11px;
  border-radius: 999px; border: 1px solid var(--border); background: var(--bg-elev);
  color: var(--text-dim); font-family: inherit; font-size: 12px; cursor: pointer;
  transition: background var(--dur) var(--ease), border-color var(--dur) var(--ease), color var(--dur) var(--ease);
}
.lib-pill:hover { color: var(--text); border-color: var(--border-strong); }
.lib-pill.on { background: var(--accent); border-color: var(--accent); color: var(--text-on-accent); font-weight: 600; }
.lib-clear {
  justify-self: start; padding: 2px 0; font-family: inherit; font-size: 12px;
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
  font-size: 12px; color: var(--text); cursor: pointer;
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
 * 底部留 96px：悬浮条是 fixed 的，不占文档流，不留白最后一行会被压在它下面。 */
.lib-grid {
  display: grid; gap: 22px 18px; align-items: start;
  grid-template-columns: repeat(auto-fill, minmax(168px, 1fr));
  padding-bottom: 96px;
}
.sk-card { min-width: 0; }

.lib-list { display: flex; flex-direction: column; gap: 4px; padding-bottom: 96px; }

/* ---------------- 底部悬浮条 ---------------- */
.lib-dock {
  position: fixed; left: 50%; bottom: 16px; z-index: 40;
  transform: translateX(-50%);
  display: inline-flex; align-items: center; gap: 8px;
  max-width: calc(100vw - 32px);
  padding: 8px; border-radius: 999px;
  /* 极强玻璃（与阅读器顶栏同一档）：渐变本体 + 柔光投影，**不画描边、不留内阴影**
     （描边与顶部高光在深色下都会变成发光边）。本体透明度按主题分档。 */
  --glass-bar-top: 82%;
  --glass-bar-bottom: 70%;
  background: linear-gradient(180deg,
    color-mix(in srgb, var(--bg-elev) var(--glass-bar-top), transparent),
    color-mix(in srgb, var(--bg-elev) var(--glass-bar-bottom), transparent));
  border: 0;
  box-shadow: var(--shadow-flyout);
  backdrop-filter: blur(44px) saturate(2.4);
  -webkit-backdrop-filter: blur(44px) saturate(2.4);
  transition: opacity var(--dur) var(--ease), transform var(--dur) var(--ease), visibility var(--dur);
}
/* 深色皮肤下：玻璃加实一档（与阅读器同一套口径）。
   ⚠️ `:global()` 里要写**整条选择器**，否则 Vue 的 scoped 编译器会吞掉后半段（见 ReaderView 的注释）。 */
:global(html.theme-dark .lib-dock) {
  --glass-bar-top: 92%;
  --glass-bar-bottom: 84%;
}
@media (prefers-color-scheme: dark) {
  :global(html[data-theme="auto"] .lib-dock),
  :global(html:not([data-theme]) .lib-dock) {
    --glass-bar-top: 92%;
    --glass-bar-bottom: 84%;
  }
}
/* 批量模式：搜索岛**隐藏但不卸载**（输入框的焦点与内容都留着，退出批量立刻能用） */
.lib-dock.away {
  opacity: 0; visibility: hidden; pointer-events: none;
  transform: translateX(-50%) translateY(8px);
}

.lib-dock-btn {
  flex: none; display: inline-flex; align-items: center; justify-content: center;
  width: 38px; height: 38px; border-radius: 999px;
  border: 1px solid var(--border); background: var(--bg-elev); color: var(--text); cursor: pointer;
  transition: background var(--dur) var(--ease), border-color var(--dur) var(--ease),
              transform var(--dur-fast) var(--ease);
}
.lib-dock-btn:hover:not(:disabled) {
  background: var(--bg-hover);
  border-color: color-mix(in srgb, var(--accent) 30%, var(--border-strong));
}
.lib-dock-btn:active:not(:disabled) { transform: scale(.94); }
.lib-dock-btn:disabled { opacity: .5; cursor: default; }
.lib-dock-btn.primary { background: var(--accent); border-color: transparent; color: var(--text-on-accent); }
.lib-dock-btn.primary:hover { background: var(--accent-hover); }

.lib-dock-search {
  display: inline-flex; align-items: center; gap: 8px;
  width: min(420px, calc(100vw - 210px));
  height: 38px; padding: 0 14px; border-radius: 999px;
  background: var(--bg-sunken); border: 1px solid var(--border); color: var(--text-faint);
  transition: background var(--dur) var(--ease), border-color var(--dur) var(--ease), box-shadow var(--dur) var(--ease);
}
.lib-dock-search:focus-within { border-color: var(--accent); box-shadow: var(--ring); background: var(--bg-elev); }
.lib-dock-search input {
  width: 100%; height: 100%; padding: 0; border: 0; border-radius: 0;
  background: transparent; box-shadow: none; font-size: 13px;
}
.lib-dock-search input:hover, .lib-dock-search input:focus {
  background: transparent; box-shadow: none; border: 0;
}

/* 批量条：同一位置与几何，但更宽（要放得下动作）；`spacer` 把动作推到右端 */
.lib-dock.is-batch { width: min(640px, calc(100vw - 32px)); padding: 8px 8px 8px 14px; }
.lib-dock-cancel {
  flex: none; padding: 5px 9px; border-radius: var(--radius-ctl);
  color: var(--text-dim); font-family: inherit; font-size: 12px; cursor: pointer;
}
.lib-dock-cancel:hover { background: var(--bg-hover); color: var(--text); }
.lib-dock-count { flex: none; font-size: 12px; color: var(--text-dim); }
.lib-dock-select {
  flex: none; height: 28px; padding: 0 12px; border-radius: 999px;
  border: 1px solid var(--border); background: transparent; color: var(--text);
  font-family: inherit; font-size: 12px; cursor: pointer;
}
.lib-dock-select:hover:not(:disabled) { background: var(--bg-hover); }
.lib-dock-select:disabled { opacity: .5; cursor: default; }
.lib-dock-action {
  flex: none; display: inline-flex; align-items: center; gap: 6px;
  height: 32px; padding: 0 13px; border-radius: 999px;
  border: 1px solid var(--border-strong); background: var(--bg-elev); color: var(--text);
  font-family: inherit; font-size: 12px; font-weight: 500; cursor: pointer;
  transition: background var(--dur) var(--ease), border-color var(--dur) var(--ease);
}
.lib-dock-action:hover:not(:disabled) { background: var(--bg-hover); }
.lib-dock-action:disabled { opacity: .5; cursor: default; }
.lib-dock-action.danger {
  color: var(--danger);
  border-color: color-mix(in srgb, var(--danger) 38%, var(--border-strong));
}
.lib-dock-action.danger:hover:not(:disabled) { background: var(--danger-soft); }

/* 窄屏：搜索岛收窄，批量条换行（宁可两行，也不把按钮挤成图标） */
@media (max-width: 720px) {
  .lib-dock-search { width: calc(100vw - 172px); }
  .lib-dock.is-batch { flex-wrap: wrap; border-radius: var(--radius); width: calc(100vw - 24px); justify-content: center; }
}

.confirm-text { margin: 0 0 8px; font-size: 13px; color: var(--text-dim); line-height: 1.6; }
.confirm-target {
  margin: 0; padding: 10px 12px; font-size: 13px; font-weight: 600;
  background: var(--bg-sunken); border-radius: var(--r-sm); overflow-wrap: anywhere;
}

.drop-zone-inline {
  display: flex; flex-direction: column; align-items: center; gap: 8px;
  width: min(460px, 100%); margin-top: 4px; font-size: 13px;
}
.drop-zone-inline .icon { color: var(--text-faint); }
.drop-zone-inline.over .icon { color: var(--accent); }

.es-icon.bad { background: var(--danger-soft); color: var(--danger); border-color: transparent; }
.load-error { color: var(--danger); }

/* ---------------- 导入弹窗：解析引擎 + 三个开关 ----------------
 * 引擎用"块状二选一"（`.seg` 是紧凑胶囊，塞不下一行说明），所以按 `.btn` 的观感自绘：
 * 同样的描边 / 圆角 / 高度语言，选中态用 `--accent-soft` + `--accent-text`（与设置页分区药丸一致）。 */
.engine { margin-top: 12px; }
.engine-label { font-size: 12px; color: var(--text-dim); margin-bottom: 5px; }
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
.engine-name { font-size: 13px; font-weight: 600; }
.engine-opt.active .engine-name { color: var(--accent-text); }
.engine-note { font-size: 11.5px; color: var(--text-faint); line-height: 1.5; }
.engine-warn {
  display: flex; align-items: flex-start; gap: 6px;
  margin: 8px 0 0; color: var(--danger);
}
.engine-warn .icon { margin-top: 2px; }

/* 三个开关的文案：**标签一行、说明另起一行**。
 * 旧版把说明接在标签后面用行内流排，长说明会折到轨道下面、缩进也对不齐（用户看到的
 * "（在线解析由 MinerU 决定…）"掉到第三行）。两行都从文字列开始，读起来是一条一条的。 */
.opt { display: flex; flex-direction: column; gap: 2px; }
.opt-text { font-size: 12.5px; color: var(--text); }
.opt-note { font-size: 11.5px; color: var(--text-faint); line-height: 1.5; }

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
.op-head { font-size: 12px; font-weight: 600; color: var(--text); margin-bottom: 8px; }
.op-hint { font-weight: 400; color: var(--text-faint); }
.op-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
.op-grid .field { margin-top: 0; }
/* 参数区里的开关行比外面那些挨得紧一点（区块本身已经把范围框出来了） */
.online-params .switch-row + .switch-row { margin-top: 8px; }
.online-params .switch-row:first-of-type { margin-top: 10px; }
.online-params .field { margin-top: 10px; }
</style>
