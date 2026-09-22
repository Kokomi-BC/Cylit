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

const filtered = computed<Paper[]>(() => state.papers);
const loading = computed(() => !!state.loading.papers);
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

async function removePaper(paper: Paper, hard = false) {
  const label = hard ? '彻底删除（含 trash 与数据库记录）' : '删除（先清索引，PDF 文件移入 trash）';
  if (!window.confirm(`确认${label}：${paper.title}？`)) return;
  busyKey.value = `del:${paper.id}`;
  try {
    const res = await api.del<DeleteResult>(`/papers/${paper.id}`, { hard });
    toast(`已删除 ${paper.id}${res?.steps?.length ? `（${res.steps.length} 步）` : ''}`, 'ok');
    await refresh();
  } catch (err) { reportError(err, '删除失败'); } finally { busyKey.value = ''; }
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
        <div class="sub">共 {{ state.papersTotal }} 篇</div>
      </div>
      <div class="grow"></div>
      <div class="page-actions">
        <input
          class="small"
          style="width: 200px"
          v-model="keyword"
          placeholder="搜索标题 / 作者"
          @keyup.enter="refresh"
        />
        <select v-model="statusFilter" @change="refresh">
          <option value="">全部状态</option>
          <option v-for="s in STATUS_OPTIONS" :key="s" :value="s">{{ paperStatus(s).label }}</option>
        </select>
        <select v-model="sort" @change="refresh">
          <option value="last_read_at">按最近阅读</option>
          <option value="created_at">按导入时间</option>
          <option value="title">按标题</option>
        </select>
        <select v-model="order" @change="refresh">
          <option value="desc">降序</option>
          <option value="asc">升序</option>
        </select>
        <button class="btn sm" :disabled="loading" @click="refresh">
          <AppIcon name="refresh" :size="14" />刷新
        </button>
        <button class="btn primary sm" @click="importOpen = true">
          <AppIcon name="upload" :size="14" />导入 PDF
        </button>
      </div>
    </div>

    <!-- 加载中 -->
    <div class="empty card" v-if="showLoading">正在加载论文列表…</div>

    <!-- 请求失败（可重试） -->
    <div class="empty card" v-else-if="showLoadError">
      <p class="load-error">论文列表加载失败：{{ loadError }}</p>
      <button class="btn sm" @click="refresh">
        <AppIcon name="refresh" :size="14" />重试
      </button>
    </div>

    <!-- 空列表（含拖拽导入入口） -->
    <div class="empty card" v-else-if="showEmpty">
      <p>还没有论文，点右上角「导入 PDF」或拖拽 PDF 到此处。</p>
      <div
        class="drop-zone"
        :class="{ over: dragOver }"
        @dragover.prevent="dragOver = true"
        @dragleave="dragOver = false"
        @drop.prevent="onDrop"
        @click="importOpen = true"
      >
        拖拽 PDF 到此处，或点击选择文件
      </div>
    </div>

    <!-- 有旧数据但本次刷新失败 -->
    <p class="tiny load-error" v-else-if="loadError">
      刷新失败：{{ loadError }}（下方为上次成功加载的数据）
    </p>

    <div class="paper-cards">
      <div class="card paper-card" v-for="p in filtered" :key="p.id">
        <div class="row">
          <h3 class="grow" style="margin: 0">{{ p.title }}</h3>
          <span class="badge" :class="paperStatus(p.status).cls">{{ paperStatus(p.status).label }}</span>
        </div>
        <div class="meta truncate">{{ p.authors || '—' }}</div>
        <div class="meta row wrap tiny">
          <span class="mono">{{ p.id }}</span>
          <span>· {{ p.page_count || '?' }} 页</span>
          <span v-if="p.venue">· {{ p.venue }}</span>
          <span v-if="p.year">· {{ p.year }}</span>
          <span v-if="p.last_read_at">· 读于 {{ fmtRelative(p.last_read_at) }}</span>
        </div>
        <div class="row wrap tiny" v-if="(p.tags || []).length">
          <span class="badge" v-for="t in p.tags" :key="t">{{ t }}</span>
        </div>

        <div class="bars">
          <div>
            <div class="row tiny faint">
              <span>翻译</span><span class="spacer"></span>
              <span>{{ p.translate_done || 0 }} / {{ p.translate_total || 0 }}</span>
            </div>
            <ProgressBar :done="p.translate_done || 0" :total="p.translate_total || 0" />
          </div>
          <div>
            <div class="row tiny faint">
              <span>版面 PDF 翻译</span><span class="spacer"></span>
              <span>{{ p.pdf_translate_done || 0 }} / {{ p.pdf_translate_total || 0 }}</span>
            </div>
            <ProgressBar :done="p.pdf_translate_done || 0" :total="p.pdf_translate_total || 0" />
          </div>
        </div>

        <div class="row wrap small">
          <span class="badge" :class="p.in_knowledge_base ? 'accent' : ''">
            {{ p.in_knowledge_base ? '已在知识库' : '未入知识库' }}
          </span>
        </div>

        <div class="row wrap">
          <RouterLink class="btn primary sm" :to="{ name: 'reader', params: { paperId: p.id } }">阅读</RouterLink>
          <button class="btn sm" :disabled="busyKey === 'parse:' + p.id" @click="runParse(p)">解析</button>
          <button
            class="btn sm"
            :disabled="busyKey === 'tr:' + p.id"
            @click="runTranslate(p, 'resume')"
          >
            继续翻译
          </button>
          <button class="btn sm" @click="runTranslate(p, 'retranslate')">重译</button>
          <button class="btn sm" :disabled="busyKey === 'idx:' + p.id" @click="runIndex(p)">建索引</button>
          <button class="btn sm ghost" @click="toggleKb(p)">
            {{ p.in_knowledge_base ? '移出知识库' : '纳入知识库' }}
          </button>
          <button class="btn icon sm" title="编辑元数据" @click="openEdit(p)">
            <AppIcon name="edit" :size="14" />
          </button>
          <button class="btn icon sm" title="删除论文" @click="removePaper(p)">
            <AppIcon name="trash" :size="14" />
          </button>
        </div>
      </div>
    </div>

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
/* 论文卡网格（基础层未提供的库页私有布局） */
.paper-cards {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(340px, 1fr));
  gap: 14px;
}

.paper-card {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 14px;
}

/* 网格内相邻卡片不再叠加基础层的 .card + .card 外边距 */
.paper-card + .paper-card { margin-top: 0; }

.paper-card h3 { font-size: 15px; }

.paper-card .meta {
  font-size: 12px;
  color: var(--text-dim);
}

.paper-card .bars {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.load-error { color: var(--danger); }
</style>
