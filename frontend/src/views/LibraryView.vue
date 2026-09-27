<script setup lang="ts">
/**
 * 论文库：导入 → 解析 → 翻译 → 三视图阅读。
 *
 * REST 走 `@/api/client`，列表与计数走全局 store，弹窗走 `Modal`，进度条走 `ProgressBar`。
 * 删除语义：软删除会先清理索引与知识库记录，PDF 文件移入 data/trash（不物理删除）。
 */
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue';
import { RouterLink, useRouter } from 'vue-router';
import { api } from '@/api/client';
import type { Paper } from '@/api/types';
import Modal from '@/components/Modal.vue';
import Menu from '@/components/Menu.vue';
import ConfirmModal from '@/components/ConfirmModal.vue';
import ProgressBar from '@/components/ProgressBar.vue';
import AppIcon from '@/components/AppIcon.vue';
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

/** 逐页翻译是否已全部完成（完成时用一行文案代替整条进度条）。 */
function isTranslated(p: Paper): boolean {
  return !!p.translate_total && (p.translate_done || 0) >= p.translate_total;
}
const showLoading = computed(() => loading.value && filtered.value.length === 0 && !loadError.value);
const showLoadError = computed(() => !!loadError.value && filtered.value.length === 0);
const showEmpty = computed(() => !loading.value && filtered.value.length === 0 && !loadError.value);

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
});
onBeforeUnmount(stopPolling);

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
}

/** multipart 里直接透传给后端的元数据字段。 */
const UPLOAD_FIELDS = ['title', 'authors', 'venue', 'year', 'tags', 'paper_id'] as const;

interface ImportResult {
  id: string;
  reused?: boolean;
}

const importOpen = ref(false);
const importForm = reactive<ImportForm>({
  file: null, title: '', authors: '', venue: '', year: '', tags: '', paper_id: '', auto_parse: true,
});
const dragOver = ref(false);
const fileInput = ref<HTMLInputElement | null>(null);

/**
 * 清空导入表单。
 *
 * ⚠️ **必须在"选新文件"和"导入成功"两处都调用**：表单里的 title/authors/… 会残留上一篇的内容，
 * 而 `submitImport` 会把非空字段原样发给后端、后端又当成"用户填的" → 连续导入第二篇时
 * 新论文被命名成**上一篇**的名字（用户 2026-09-27 报的"命名成其他文章了"）。
 */
function resetImportForm() {
  for (const k of UPLOAD_FIELDS) importForm[k] = '';
  importForm.file = null;
  importForm.auto_parse = true;
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
  const form = new FormData();
  form.append('file', importForm.file);
  for (const k of UPLOAD_FIELDS) {
    const v = importForm[k];
    if (v !== '') form.append(k, String(v));
  }
  form.append('auto_parse', importForm.auto_parse ? 'true' : 'false');
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
  <div>
    <div class="page-head">
      <div>
        <h1>论文库</h1>
        <div class="sub">共 {{ state.papersTotal }} 篇论文</div>
      </div>
      <div class="grow"></div>
      <div class="page-actions">
        <button class="btn icon" title="刷新列表" :disabled="loading" @click="refresh">
          <AppIcon name="refresh" :size="15" />
        </button>
        <button class="btn primary" @click="importOpen = true">
          <AppIcon name="upload" :size="15" />导入 PDF
        </button>
      </div>
    </div>

    <!-- 筛选条：一个搜索 + 两个下拉，其余交给列表本身 -->
    <div class="toolbar">
      <label class="search-field">
        <AppIcon name="search" :size="15" />
        <input v-model="keyword" placeholder="搜索标题 / 作者" @keyup.enter="refresh" />
      </label>
      <select v-model="statusFilter" title="按状态筛选" @change="refresh">
        <option value="">全部状态</option>
        <option v-for="s in STATUS_OPTIONS" :key="s" :value="s">{{ paperStatus(s).label }}</option>
      </select>
      <select v-model="sortKey" title="排序方式" @change="refresh">
        <option v-for="o in SORT_OPTIONS" :key="o.value" :value="o.value">{{ o.label }}</option>
      </select>
    </div>

    <!-- 加载中：与真实卡片同高的骨架，不用一行灰字 -->
    <div class="paper-cards" v-if="showLoading" aria-busy="true">
      <span class="sr-only">正在加载论文列表…</span>
      <div class="card paper-card" v-for="i in 3" :key="i" aria-hidden="true">
        <div class="sk sk-title" style="width: 74%"></div>
        <div class="sk sk-line" style="width: 42%"></div>
        <div class="sk sk-line" style="width: 58%"></div>
        <div class="sk sk-bar" style="width: 100%"></div>
        <div class="pc-actions">
          <div class="sk" style="width: 76px; height: 26px; border-radius: var(--radius-ctl)"></div>
          <div class="sk" style="width: 62px; height: 26px; border-radius: var(--radius-ctl)"></div>
        </div>
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
        @click="importOpen = true"
      >
        <AppIcon name="upload" :size="18" />
        <span>拖拽 PDF 到此处，或点击选择文件</span>
      </div>
    </div>

    <!-- 有旧数据但本次刷新失败 -->
    <p class="tiny load-error" v-else-if="loadError">
      刷新失败：{{ loadError }}（下方为上次成功加载的数据）
    </p>

    <div class="paper-cards" v-if="!showLoading && filtered.length">
      <article class="card hoverable paper-card" v-for="p in filtered" :key="p.id">
        <header class="pc-head">
          <button class="pc-title" :title="`在新标签页打开：${p.title}`" @click="openPaper(p.id)">
            {{ p.title }}
          </button>
          <span class="badge" :class="cardStatus(p).cls">{{ cardStatus(p).label }}</span>
        </header>

        <div class="pc-meta">
          <span v-if="p.authors" class="truncate">{{ p.authors }}</span>
          <span>{{ p.page_count || '?' }} 页</span>
          <span v-if="p.venue">{{ p.venue }}</span>
          <span v-if="p.year">{{ p.year }}</span>
          <span v-if="p.last_read_at">读于 {{ fmtRelative(p.last_read_at) }}</span>
        </div>

        <div class="pc-tags" v-if="(p.tags || []).length">
          <span class="badge" v-for="t in p.tags" :key="t">{{ t }}</span>
        </div>

        <!-- 进度：只在"正在翻译"时出现；100% 用一行文案收尾，不再铺一条满格色带。
             数据优先取正在跑的翻译任务（逐页实时），没任务才用论文行。 -->
        <div class="pc-progress"
             v-if="progressOf(p)"
             :title="activeTaskOf(p)?.message || ''">
          <div class="pc-progress-top">
            <span class="faint">逐页翻译</span>
            <span class="tnum">{{ progressOf(p)!.done }} / {{ progressOf(p)!.total }}</span>
          </div>
          <ProgressBar :done="progressOf(p)!.done" :total="progressOf(p)!.total" />
        </div>
        <div class="pc-progress" v-if="p.pdf_translate_total && (p.pdf_translate_done || 0) < p.pdf_translate_total">
          <div class="pc-progress-top">
            <span class="faint">版面 PDF 翻译</span>
            <span class="tnum">{{ p.pdf_translate_done || 0 }} / {{ p.pdf_translate_total }}</span>
          </div>
          <ProgressBar :done="p.pdf_translate_done || 0" :total="p.pdf_translate_total" />
        </div>
        <div class="pc-done tiny" v-if="showDoneLine(p)">
          <AppIcon name="check_circle" :size="14" />逐页翻译已完成（{{ p.translate_total }} 页）
        </div>
        <div class="tiny faint"
             v-if="!p.translate_total && !p.pdf_translate_total && !activeTaskOf(p)">尚未开始翻译</div>

        <footer class="pc-actions">
          <!-- 知识库归属：只在"已入库"时现身，避免每张卡片都挂一个同义徒章 -->
          <span class="badge accent" v-if="p.in_knowledge_base">
            <AppIcon name="knowledge" :size="13" />已在知识库
          </span>
          <span class="spacer"></span>
          <button class="btn primary sm" title="在新标签页打开阅读器" @click="openPaper(p.id)">
            <AppIcon name="document" :size="14" />阅读
          </button>
          <!-- 译完的论文不再把「重新翻译」摆在一级（用户口径）：它进了「⋯」菜单，
               卡片上只留「阅读」一个动作。翻译中/未解析的论文照旧给「翻译 / 继续翻译 / 暂停翻译」。 -->
          <button
            v-if="stage(p) !== 'translated'"
            class="btn sm"
            :disabled="busyKey === 'tr:' + p.id || busyKey === 'parse:' + p.id"
            :title="activeTaskOf(p)
              ? (activeTaskOf(p)!.status === 'paused' ? '继续这个翻译任务' : '暂停：当前这一页译完就停')
              : (stage(p) === 'unparsed' ? '还没解析：点一下会先解析，解析完自动接着翻译' : '')"
            @click="runPipeline(p)"
          >{{ pipelineLabel(p) }}</button>
          <Menu
            :items="rowMenu(p)"
            :label="`更多操作：${p.title}`"
            @select="onRowAction(p, $event)"
          />
        </footer>
      </article>
    </div>

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
      <label class="row small" style="margin-top: 10px">
        <input type="checkbox" v-model="importForm.auto_parse" /> 导入后立即解析
      </label>
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
/* 论文卡网格：列宽下限跟着正文行宽走，宽屏下自然排 2–4 列 */
.paper-cards {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(360px, 1fr));
  gap: 16px;
  align-items: stretch;
}

.paper-card {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 18px;
}

/* 网格内相邻卡片不再叠加基础层的 .card + .card 外边距 */
.paper-card + .paper-card { margin-top: 0; }

.pc-head { display: flex; align-items: flex-start; gap: 10px; }
/* 标题现在是 <button>（点一下在新标签页打开阅读器）：外观按**文字**来，
   只保留"可点 + 键盘可达"。base.css 已把 UA 的按钮盒子重置掉，这里只补字色与光标。 */
.pc-title {
  flex: 1; min-width: 0;
  color: var(--text); font-weight: 600; font-size: 14.5px; line-height: 1.35;
  letter-spacing: -.011em; text-wrap: balance; cursor: pointer;
  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;
}
.pc-title:hover { color: var(--accent-text); text-decoration: none; }
.pc-head .badge { flex: none; margin-top: 1px; }

.pc-meta {
  display: flex; flex-wrap: wrap; align-items: center; gap: 4px 10px;
  font-size: 12px; color: var(--text-dim);
}
.pc-meta > span + span::before { content: "·"; margin-right: 10px; color: var(--text-faint); }
.pc-meta > span:first-child { max-width: 100%; }

.pc-tags { display: flex; flex-wrap: wrap; gap: 5px; }

.pc-progress { display: flex; flex-direction: column; gap: 5px; }
.pc-progress-top {
  display: flex; align-items: center; font-size: 11.5px;
  font-variant-numeric: tabular-nums;
}
.pc-progress-top > span:last-child { margin-left: auto; color: var(--text-dim); }
/* 完成态：一行小字 + 对勾，比一整条满格色带安静得多 */
.pc-done {
  display: inline-flex; align-items: center; gap: 6px; color: var(--text-dim);
}
.pc-done .icon { color: var(--ok); }

/* 操作区永远贴在卡片底部，同一行卡片的主操作排成一条线 */
.pc-actions {
  display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
  margin-top: auto; padding-top: 4px;
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
</style>
