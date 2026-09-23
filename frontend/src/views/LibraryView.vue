<script setup lang="ts">
/**
 * 论文库：导入 → 解析 → 翻译 → 三视图阅读。
 *
 * REST 走 `@/api/client`，列表与计数走全局 store，弹窗走 `Modal`，进度条走 `ProgressBar`。
 * 删除语义：软删除会先清理索引与知识库记录，PDF 文件移入 data/trash（不物理删除）。
 */
import { computed, onMounted, reactive, ref } from 'vue';
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
import { fmtRelative, paperStatus } from '@/utils/format';

const router = useRouter();

/* ------------------------------ 筛选与列表 ------------------------------ */

const keyword = ref('');
const statusFilter = ref('');
const sort = ref('last_read_at');
const order = ref('desc');
/** 正在进行的行内操作键：`import` / `parse:<id>` / `tr:<id>` / `idx:<id>` / `del:<id>`。 */
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

onMounted(refresh);

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

function pickFile(file?: File | null) {
  if (!file) return;
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
    importForm.file = null;
    await refresh();
    if (data.reused) router.push({ name: 'reader', params: { paperId: data.id } });
  } catch (err) {
    reportError(err, '导入失败');
  } finally {
    busyKey.value = '';
  }
}

/* ------------------------------ 行内操作 ------------------------------ */

async function runParse(paper: Paper, force = false) {
  busyKey.value = `parse:${paper.id}`;
  try {
    await api.post(`/papers/${paper.id}/parse`, {}, { force, vision: true });
    toast(`已创建解析任务：${paper.title}`, 'ok');
    await refresh();
  } catch (err) { reportError(err, '触发解析失败'); } finally { busyKey.value = ''; }
}

type TranslateMode = 'resume' | 'retranslate';

/** 翻译只有一种粒度（逐页）：`retranslate` 走 force，忽略已有产物重来。 */
async function runTranslate(paper: Paper, mode: TranslateMode = 'resume') {
  busyKey.value = `tr:${paper.id}`;
  try {
    await api.post(`/papers/${paper.id}/translate`, { mode, brief: true });
    toast(mode === 'retranslate' ? '已创建重译任务' : '已创建逐页翻译任务', 'ok');
    await refresh();
  } catch (err) { reportError(err, '触发翻译失败'); } finally { busyKey.value = ''; }
}

async function runIndex(paper: Paper, rebuild = false) {
  busyKey.value = `idx:${paper.id}`;
  try {
    await api.post('/knowledge/index', { paper_id: paper.id, rebuild });
    toast('已创建索引任务', 'ok');
    await refresh();
  } catch (err) { reportError(err, '触发索引失败'); } finally { busyKey.value = ''; }
}

async function toggleKb(paper: Paper) {
  try {
    await api.post('/knowledge/papers', { paper_id: paper.id, include: !paper.in_knowledge_base });
    toast(paper.in_knowledge_base ? '已移出知识库' : '已纳入知识库', 'ok', 2200);
    await refresh();
  } catch (err) { reportError(err, '更新知识库归属失败'); }
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

/** 次要操作（解析 / 重译 / 建索引 / 知识库 / 编辑 / 删除）收进「⋯」菜单，
 * 卡片上只留「阅读（主）+ 继续翻译（次）」两个按钮，避免一排同权重按钮。 */
function rowMenu(p: Paper): RowAction[] {
  const busy = (k: string) => busyKey.value === `${k}:${p.id}`;
  return [
    { key: 'parse', label: '解析 PDF', icon: 'document', disabled: busy('parse') },
    { key: 'translate', label: '重新翻译', icon: 'translate', disabled: busy('tr') },
    { key: 'index', label: '建立索引', icon: 'layers', disabled: busy('idx') },
    {
      key: 'kb',
      label: p.in_knowledge_base ? '移出知识库' : '纳入知识库',
      icon: p.in_knowledge_base ? 'link-off' : 'link',
    },
    { key: 'edit', label: '编辑元数据', icon: 'edit' },
    { key: 'delete', label: '删除（移入回收站）', icon: 'trash', danger: true },
    { key: 'purge', label: '彻底删除', icon: 'trash', danger: true },
  ];
}

function onRowAction(p: Paper, key: string) {
  if (key === 'parse') { runParse(p); return; }
  if (key === 'translate') { runTranslate(p, 'retranslate'); return; }
  if (key === 'index') { runIndex(p); return; }
  if (key === 'kb') { toggleKb(p); return; }
  if (key === 'edit') { openEdit(p); return; }
  if (key === 'delete') { askRemove(p, false); return; }
  if (key === 'purge') { askRemove(p, true); }
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
          <RouterLink class="pc-title" :to="{ name: 'reader', params: { paperId: p.id } }">
            {{ p.title }}
          </RouterLink>
          <span class="badge" :class="paperStatus(p.status).cls">{{ paperStatus(p.status).label }}</span>
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

        <!-- 进度：只在"正在翻译"时出现；100% 用一行文案收尾，不再铺一条满格色带 -->
        <div class="pc-progress" v-if="p.translate_total && (p.translate_done || 0) < p.translate_total">
          <div class="pc-progress-top">
            <span class="faint">逐页翻译</span>
            <span class="tnum">{{ p.translate_done || 0 }} / {{ p.translate_total }}</span>
          </div>
          <ProgressBar :done="p.translate_done || 0" :total="p.translate_total" />
        </div>
        <div class="pc-progress" v-if="p.pdf_translate_total && (p.pdf_translate_done || 0) < p.pdf_translate_total">
          <div class="pc-progress-top">
            <span class="faint">版面 PDF 翻译</span>
            <span class="tnum">{{ p.pdf_translate_done || 0 }} / {{ p.pdf_translate_total }}</span>
          </div>
          <ProgressBar :done="p.pdf_translate_done || 0" :total="p.pdf_translate_total" />
        </div>
        <div class="pc-done tiny" v-if="isTranslated(p)">
          <AppIcon name="check_circle" :size="14" />逐页翻译已完成（{{ p.translate_total }} 页）
        </div>
        <div class="tiny faint" v-if="!p.translate_total && !p.pdf_translate_total">尚未开始翻译</div>

        <footer class="pc-actions">
          <!-- 知识库归属：只在"已入库"时现身，避免每张卡片都挂一个同义徒章 -->
          <span class="badge accent" v-if="p.in_knowledge_base">
            <AppIcon name="knowledge" :size="13" />已在知识库
          </span>
          <span class="spacer"></span>
          <RouterLink class="btn primary sm" :to="{ name: 'reader', params: { paperId: p.id } }">
            <AppIcon name="document" :size="14" />阅读
          </RouterLink>
          <button
            class="btn sm"
            v-if="(p.translate_done || 0) < (p.translate_total || 0)"
            :disabled="busyKey === 'tr:' + p.id"
            @click="runTranslate(p, 'resume')"
          >继续翻译</button>
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
      <template #footer>
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
.pc-title {
  flex: 1; min-width: 0;
  color: var(--text); font-weight: 600; font-size: 14.5px; line-height: 1.35;
  letter-spacing: -.011em; text-wrap: balance;
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
