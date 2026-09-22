<script setup lang="ts">
/**
 * 笔记 / 高亮 / 批注聚合页。
 *
 * 数据来源：`GET /notes/overview` 一次取回三类条目（各带上 paper_title）。
 * 搜索走后端 `GET /notes?q=`（只过滤「笔记」），命中结果覆盖笔记 tab 的列表。
 * 编辑：笔记 `PUT /notes/{id}`、批注 `PUT /annotations/{id}`，只提交 `content_md`。
 */

import { computed, onMounted, reactive, ref } from 'vue';
import { RouterLink } from 'vue-router';
import { api } from '@/api/client';
import type { Annotation, Highlight, Note } from '@/api/types';
import {
  loadPapers, paperTitle, reportError, state, toast, withLoading,
} from '@/stores/app';
import {
  fmtDate, fmtRelative, truncate,
} from '@/utils/format';
import { mdToHtml } from '@/composables/useMarkdown';
import Modal from '@/components/Modal.vue';
import AppIcon from '@/components/AppIcon.vue';

/** 三类条目都由 overview 带上 paper_title；批注还可能带 status。 */
type NoteRow = Note & { paper_title?: string };
type HighlightRow = Highlight & { paper_title?: string };
type AnnotationRow = Annotation & { paper_title?: string; status?: string };

interface NotesOverview {
  notes: NoteRow[];
  highlights: HighlightRow[];
  annotations: AnnotationRow[];
}

type EditKind = 'note' | 'annotation';

/** 高亮颜色（与 `.hl.<color>` 一一对应），用于色点。 */
const COLOR_HEX: Record<string, string> = {
  yellow: '#ffd640',
  green: '#5adc96',
  blue: '#6eb4ff',
  pink: '#ff96be',
  purple: '#8c8c8c',
};

const ANNOTATION_TYPE_LABEL: Record<string, string> = {
  qa: '问答',
  explain: 'AI 解释',
  summary: '摘要',
  manual: '手动批注',
};

const ANNOTATION_SOURCE_LABEL: Record<string, string> = {
  selection: '划词',
  ai: 'AI 生成',
  manual: '手动',
  pipeline: '自动',
};

const tab = ref<'notes' | 'highlights' | 'annotations'>('notes');
const overview = ref<NotesOverview | null>(null);
const keyword = ref('');
const searchItems = ref<NoteRow[] | null>(null);   // null = 未搜索（用 overview.notes）
const editOpen = ref(false);
const editKind = ref<EditKind>('note');            // note | annotation
const editTarget = ref<NoteRow | AnnotationRow | null>(null);
const editForm = reactive<{ content_md: string }>({ content_md: '' });
const saving = ref(false);
const busyKey = ref('');

const notes = computed<NoteRow[]>(() => (searchItems.value !== null
  ? searchItems.value
  : (overview.value?.notes || [])));
const highlights = computed<HighlightRow[]>(() => overview.value?.highlights || []);
const annotations = computed<AnnotationRow[]>(() => overview.value?.annotations || []);
const searching = computed(() => searchItems.value !== null);

function titleOf(paperId?: string | null): string {
  if (!paperId) return '（未知论文）';
  const all: Array<NoteRow | HighlightRow | AnnotationRow> = [
    ...(overview.value?.notes || []),
    ...(overview.value?.highlights || []),
    ...(overview.value?.annotations || []),
  ];
  const hit = all.find((x) => x.paper_id === paperId);
  return hit?.paper_title || paperTitle(paperId);
}

/** 跳回阅读器原文：`{ name: 'reader', params: { paperId }, query: { block } }`。 */
function readerTo(paperId?: string | null, blockId?: string | null) {
  return {
    name: 'reader',
    params: { paperId: paperId ?? '' },
    query: blockId ? { block: blockId } : {},
  };
}

function colorHex(color?: string | null): string {
  return (color && COLOR_HEX[color]) || COLOR_HEX.yellow;
}

function hlClass(color?: string | null): string {
  return color && COLOR_HEX[color] ? `hl ${color}` : 'hl yellow';
}

function typeLabel(type?: string | null): string {
  return (type && ANNOTATION_TYPE_LABEL[type]) || type || '批注';
}

function sourceLabel(source?: string | null): string {
  return (source && ANNOTATION_SOURCE_LABEL[source]) || source || '未知来源';
}

function annClass(annotation?: AnnotationRow | null): string {
  if (annotation?.type === 'qa') return 'qa';
  if (annotation?.source === 'manual' || annotation?.type === 'manual') return 'manual';
  return '';
}

function confidenceText(value?: number | null): string {
  const n = Number(value);
  if (!Number.isFinite(n) || n <= 0) return '';
  return `置信度 ${Math.round(n <= 1 ? n * 100 : n)}%`;
}

async function refresh(): Promise<void> {
  await withLoading('notes-overview', async () => {
    overview.value = await api.get<NotesOverview>('/notes/overview');
  });
}

/** 后端搜索：GET /notes?q= —— 只过滤笔记。 */
async function doSearch(): Promise<void> {
  const q = keyword.value.trim();
  if (!q) {
    searchItems.value = null;
    return;
  }
  await withLoading('notes-search', async () => {
    const data = await api.get<{ items: NoteRow[] }>('/notes', { q });
    searchItems.value = data.items || [];
  }, { silent: false });
  tab.value = 'notes';
}

function clearSearch(): void {
  keyword.value = '';
  searchItems.value = null;
}

async function refreshAll(): Promise<void> {
  try {
    await refresh();
    if (keyword.value.trim()) await doSearch();
  } catch { /* withLoading 已提示 */ }
}

onMounted(async () => {
  try { await loadPapers(); } catch { /* 论文缓存失败不阻塞本页 */ }
  await refreshAll();
});

/* ------------------------------ 编辑 ------------------------------ */

function openEdit(kind: EditKind, item: NoteRow | AnnotationRow): void {
  editKind.value = kind;
  editTarget.value = item;
  editForm.content_md = item.content_md || '';
  editOpen.value = true;
}

async function submitEdit(): Promise<void> {
  const target = editTarget.value;
  if (!target) return;
  saving.value = true;
  try {
    const path = editKind.value === 'note'
      ? `/notes/${target.id}`
      : `/annotations/${target.id}`;
    await api.put(path, { content_md: editForm.content_md });
    toast(editKind.value === 'note' ? '笔记已保存' : '批注已保存', 'ok');
    editOpen.value = false;
    await refreshAll();
  } catch (err) {
    reportError(err, '保存失败');
  } finally {
    saving.value = false;
  }
}

/* ------------------------------ 删除 ------------------------------ */

async function removeNote(note: NoteRow): Promise<void> {
  if (!window.confirm(`确认删除这条笔记？\n${truncate(note.content_md || note.selected_text || note.id, 80)}`)) return;
  busyKey.value = `del:${note.id}`;
  try {
    await api.del(`/notes/${note.id}`);
    toast('笔记已删除', 'ok');
    await refreshAll();
  } catch (err) {
    reportError(err, '删除笔记失败');
  } finally {
    busyKey.value = '';
  }
}

async function removeHighlight(highlight: HighlightRow): Promise<void> {
  if (!window.confirm(`确认删除这条高亮？\n${truncate(highlight.text || highlight.id, 80)}`)) return;
  busyKey.value = `del:${highlight.id}`;
  try {
    await api.del(`/highlights/${highlight.id}`);
    toast('高亮已删除', 'ok');
    await refreshAll();
  } catch (err) {
    reportError(err, '删除高亮失败');
  } finally {
    busyKey.value = '';
  }
}

async function removeAnnotation(annotation: AnnotationRow): Promise<void> {
  if (!window.confirm(`确认删除这条批注？\n${truncate(annotation.content_md || annotation.id, 80)}`)) return;
  busyKey.value = `del:${annotation.id}`;
  try {
    await api.del(`/annotations/${annotation.id}`);
    toast('批注已删除', 'ok');
    await refreshAll();
  } catch (err) {
    reportError(err, '删除批注失败');
  } finally {
    busyKey.value = '';
  }
}
</script>

<template>
  <div>
    <div class="page-head">
      <div>
        <h1>笔记</h1>
        <div class="sub">
          笔记 {{ notes.length }} · 高亮 {{ highlights.length }} · 批注 {{ annotations.length }}
        </div>
      </div>
      <div class="grow"></div>
      <div class="page-actions">
        <input class="small" style="width:220px" v-model="keyword"
               placeholder="搜索笔记"
               @keyup.enter="doSearch" />
        <button class="btn sm" @click="doSearch" :disabled="state.loading['notes-search']">
          <AppIcon name="search" :size="14" />搜索
        </button>
        <button class="btn sm ghost" v-if="searching" @click="clearSearch">
          <AppIcon name="close" :size="14" />清除搜索
        </button>
        <button class="btn sm" @click="refreshAll" :disabled="state.loading['notes-overview']">
          <AppIcon name="refresh" :size="14" />{{ state.loading['notes-overview'] ? '刷新中…' : '刷新' }}
        </button>
      </div>
    </div>

    <div class="tiny faint" v-if="searching" style="margin-bottom:8px">
      关键词「{{ keyword }}」命中 {{ notes.length }} 条笔记。
    </div>

    <div class="tabs">
      <button :class="{ active: tab === 'notes' }" @click="tab = 'notes'">笔记（{{ notes.length }}）</button>
      <button :class="{ active: tab === 'highlights' }" @click="tab = 'highlights'">高亮（{{ highlights.length }}）</button>
      <button :class="{ active: tab === 'annotations' }" @click="tab = 'annotations'">批注（{{ annotations.length }}）</button>
    </div>

    <!-- ------------------------------ 笔记 ------------------------------ -->
    <div style="margin-top:14px" v-if="tab === 'notes'">
      <div class="card" v-for="n in notes" :key="n.id">
        <div class="card-head">
          <span class="badge accent">{{ n.paper_title || titleOf(n.paper_id) }}</span>
          <span class="tiny faint mono">{{ n.id }}</span>
          <span class="spacer"></span>
          <span class="tiny faint">{{ fmtRelative(n.updated_at || n.created_at) }}</span>
        </div>
        <div class="card-body">
          <div class="md small" v-html="mdToHtml(n.content_md || '')"></div>
          <div class="small muted" v-if="n.selected_text"
               style="margin-top:8px;border-left:3px solid var(--border-strong);padding:2px 8px">
            摘录：{{ truncate(n.selected_text, 200) }}
          </div>
          <div class="row wrap tiny" style="margin-top:8px" v-if="(n.tags || []).length">
            <span class="badge" v-for="t in n.tags" :key="t">{{ t }}</span>
          </div>
          <div class="row wrap" style="margin-top:10px">
            <RouterLink class="btn sm" v-if="n.block_id" :to="readerTo(n.paper_id, n.block_id)">
              <AppIcon name="locate" :size="14" />跳回原文
            </RouterLink>
            <span class="tiny faint" v-else>无关联原文块</span>
            <button class="btn sm ghost" @click="openEdit('note', n)">
              <AppIcon name="edit" :size="14" />编辑
            </button>
            <button class="btn sm danger" :disabled="busyKey === 'del:' + n.id" @click="removeNote(n)">
              <AppIcon name="trash" :size="14" />删除
            </button>
            <span class="spacer"></span>
            <span class="tiny faint" v-if="n.block_id">block {{ n.block_id }}</span>
          </div>
        </div>
      </div>
      <div class="card empty" v-if="!notes.length">
        {{ state.loading['notes-overview'] || state.loading['notes-search']
            ? '正在读取笔记…'
            : (searching ? '没有匹配的笔记。' : '还没有笔记，在阅读器中选中文本即可新建。') }}
      </div>
    </div>

    <!-- ------------------------------ 高亮 ------------------------------ -->
    <div style="margin-top:14px" v-if="tab === 'highlights'">
      <div class="card" v-for="h in highlights" :key="h.id">
        <div class="card-body">
          <div class="row wrap tiny">
            <span class="swatch" :style="{ background: colorHex(h.color) }"
                  :title="'高亮颜色：' + (h.color || 'yellow')"></span>
            <span class="badge accent">{{ h.paper_title || titleOf(h.paper_id) }}</span>
            <span class="mono faint" v-if="h.block_id">{{ h.block_id }}</span>
            <span class="spacer"></span>
            <span class="faint">{{ fmtDate(h.created_at) }}</span>
          </div>
          <p class="small" style="margin:8px 0 0">
            <span :class="hlClass(h.color)">{{ h.text || '（无文本）' }}</span>
          </p>
          <div class="row wrap tiny faint" style="margin-top:6px">
            <span>偏移 {{ h.start }} – {{ h.end }}</span>
            <span v-if="h.color">· 颜色 {{ h.color }}</span>
          </div>
          <div class="row wrap" style="margin-top:10px">
            <RouterLink class="btn sm" v-if="h.block_id" :to="readerTo(h.paper_id, h.block_id)">
              <AppIcon name="locate" :size="14" />跳回原文
            </RouterLink>
            <span class="tiny faint" v-else>无关联原文块</span>
            <button class="btn sm danger" :disabled="busyKey === 'del:' + h.id" @click="removeHighlight(h)">
              <AppIcon name="trash" :size="14" />删除
            </button>
          </div>
        </div>
      </div>
      <div class="card empty" v-if="!highlights.length">
        {{ state.loading['notes-overview'] ? '正在读取高亮…' : '还没有高亮。' }}
      </div>
    </div>

    <!-- ------------------------------ 批注 ------------------------------ -->
    <div style="margin-top:14px" v-if="tab === 'annotations'">
      <div class="annotation-item" :class="annClass(a)" v-for="a in annotations" :key="a.id">
        <div class="row wrap tiny">
          <span class="badge accent">{{ typeLabel(a.type) }}</span>
          <span class="badge">{{ sourceLabel(a.source) }}</span>
          <span class="badge accent">{{ a.paper_title || titleOf(a.paper_id) }}</span>
          <span class="badge" v-if="confidenceText(a.confidence)">{{ confidenceText(a.confidence) }}</span>
          <span class="badge" v-if="a.status && a.status !== 'active'">{{ a.status }}</span>
          <span class="spacer"></span>
          <span class="faint">{{ fmtRelative(a.created_at) }}</span>
        </div>
        <div class="md small" style="margin-top:6px" v-html="mdToHtml(a.content_md || '')"></div>
        <div class="tiny faint" v-if="a.model_id" style="margin-top:4px">模型 {{ a.model_id }}</div>
        <div class="tiny muted" style="margin-top:4px" v-for="(s, i) in (a.sources || [])" :key="i">
          来源：{{ s.paper_id }} · {{ s.block_id }} — {{ truncate(s.snippet || '', 120) }}
        </div>
        <div class="row wrap" style="margin-top:10px">
          <RouterLink class="btn sm" v-if="a.block_id" :to="readerTo(a.paper_id, a.block_id)">
            <AppIcon name="locate" :size="14" />跳回原文
          </RouterLink>
          <span class="tiny faint" v-else>无关联原文块</span>
          <button class="btn sm ghost" @click="openEdit('annotation', a)">
            <AppIcon name="edit" :size="14" />编辑
          </button>
          <button class="btn sm danger" :disabled="busyKey === 'del:' + a.id" @click="removeAnnotation(a)">
            <AppIcon name="trash" :size="14" />删除
          </button>
          <span class="spacer"></span>
          <span class="tiny faint mono">{{ a.id }}</span>
        </div>
      </div>
      <div class="card empty" v-if="!annotations.length">
        {{ state.loading['notes-overview'] ? '正在读取批注…' : '还没有批注。' }}
      </div>
    </div>

    <Modal :open="editOpen" :title="editKind === 'note' ? '编辑笔记' : '编辑批注'" @close="editOpen = false">
      <label class="field">
        内容（Markdown，支持 $公式$）
        <textarea v-model="editForm.content_md" rows="8"></textarea>
      </label>
      <div class="small muted" v-if="editTarget">
        所属论文：{{ editTarget.paper_title || titleOf(editTarget.paper_id) }}
        <span v-if="editTarget.block_id"> · block {{ editTarget.block_id }}</span>
      </div>
      <template #footer>
        <button class="btn" @click="editOpen = false">取消</button>
        <button class="btn primary" :disabled="saving" @click="submitEdit">{{ saving ? '保存中…' : '保存' }}</button>
      </template>
    </Modal>
  </div>
</template>

<style scoped>
/* 高亮色点与高亮文本配色（旧实现里在全局 app.css，这里按页收拢到 scoped）。 */
.swatch { width: 15px; height: 15px; border-radius: 50%; border: 1px solid var(--border-strong); cursor: pointer; }

.hl { border-radius: 4px; padding: 0 1px; }
.hl.yellow { background: rgba(255, 214, 64, .45); }
.hl.green { background: rgba(90, 220, 150, .38); }
.hl.blue { background: rgba(110, 180, 255, .38); }
.hl.pink { background: rgba(255, 150, 190, .38); }
.hl.purple { background: rgba(140, 140, 140, .32); }

/* 批注卡片：问答 / 手动批注用不同左边框区分。 */
.annotation-item { border-left: 3px solid var(--border-strong); background: var(--bg-sunken); border-radius: var(--radius-sm); padding: 8px 10px; margin-bottom: 8px; }
.annotation-item.manual { border-left-color: var(--accent); }
.annotation-item.qa { border-left-color: var(--ok); }
</style>
