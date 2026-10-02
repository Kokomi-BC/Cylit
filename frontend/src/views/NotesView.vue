<script setup lang="ts">
/**
 * 笔记 / 高亮 / 批注聚合页。
 *
 * 数据来源：`GET /notes/overview` 一次取回三类条目（各带上 paper_title）。
 * 搜索走后端 `GET /notes?q=`（只过滤「笔记」），命中结果覆盖笔记 tab 的列表。
 * 编辑：笔记 `PUT /notes/{id}`、批注 `PUT /annotations/{id}`，只提交 `content_md`。
 */

import { computed, onMounted, reactive, ref, watch } from 'vue';
import { useRouter } from 'vue-router';
import { api } from '@/api/client';
import type { Annotation, Highlight, Note } from '@/api/types';
import {
  loadPapers, paperTitle, reportError, state, toast, withLoading,
} from '@/stores/app';
import { openTab } from '@/stores/tabs';
import {
  debounce, fmtDate, fmtRelative, truncate,
} from '@/utils/format';
import { readerTarget } from '@/utils/navigation';
import { mdToHtml } from '@/composables/useMarkdown';
import Modal from '@/components/Modal.vue';
import ConfirmModal from '@/components/ConfirmModal.vue';
import Menu from '@/components/Menu.vue';
import AppIcon from '@/components/AppIcon.vue';

const router = useRouter();

/** 同 Menu 组件的 items 结构（结构类型，无需导出）。 */
interface RowAction {
  key: string;
  label: string;
  icon?: string;
  danger?: boolean;
  disabled?: boolean;
}

/* ---------------------------- 删除确认（站内弹窗） ---------------------------- */

interface ConfirmBox {
  open: boolean;
  title: string;
  body: string;
  target: string;
  label: string;
  action: (() => Promise<void>) | null;
}

const confirmBox = reactive<ConfirmBox>({
  open: false, title: '', body: '', target: '', label: '删除', action: null,
});

function askConfirm(opts: { title: string; body: string; target: string; action: () => Promise<void> }) {
  confirmBox.title = opts.title;
  confirmBox.body = opts.body;
  confirmBox.target = opts.target;
  confirmBox.action = opts.action;
  confirmBox.open = true;
}

async function runConfirm() {
  const action = confirmBox.action;
  confirmBox.open = false;
  confirmBox.action = null;
  if (action) await action();
}

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

/**
 * 跳回原文：登记/激活阅读器标签 + 带上锚点（`row`）与页码（`page`）。
 *
 * ⚠️ 只能在**点击回调**里调：`:to="fn()"` 会在每次渲染调用 `fn`，而 `openTab()` 会改响应式
 * tabs 状态（以前每次都赋新的 `query` 对象 → 永远"变了"）→ "渲染 → 改状态 → 再渲染"死循环，
 * 页面直接卡死。知识库页已经这么炸过一次，这里（笔记 / 高亮 / 批注三处）是同一个写法。
 *
 * 锚点可能是正文行 id（`p001-06`，本页读到的）也可能是**解析块 id**（历史的 / 检索侧的）：
 * 阅读器做三级解析（行 id → `block_rows` → `page`），所以这里把 `page` 一并带上当兜底。
 */
function openReader(paperId?: string | null, blockId?: string | null, page?: number | null) {
  void router.push(openTab(readerTarget(paperId ?? '', blockId, page)) as never);
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

/* **打字即搜**（用户口径）：关键词一变就重查（去抖 300ms）；回车立即搜一次（先取消在途那次）。
 * 清空时 doSearch 自己会把结果清掉，所以不需要额外分支。 */
const searchDebounced = debounce(() => { void doSearch(); }, 300);
function searchNow() { searchDebounced.cancel(); void doSearch(); }
watch(keyword, () => searchDebounced());

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
  askConfirm({
    title: '删除笔记',
    body: '笔记删除后无法恢复。',
    target: truncate(note.content_md || note.selected_text || note.id, 120) || '（空笔记）',
    action: async () => {
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
    },
  });
}

async function removeHighlight(highlight: HighlightRow): Promise<void> {
  askConfirm({
    title: '删除高亮',
    body: '高亮删除后无法恢复。',
    target: truncate(highlight.text || highlight.id, 120),
    action: async () => {
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
    },
  });
}

async function removeAnnotation(annotation: AnnotationRow): Promise<void> {
  askConfirm({
    title: '删除批注',
    body: '批注删除后无法恢复。',
    target: truncate(annotation.content_md || annotation.id, 120),
    action: async () => {
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
    },
  });
}

/* ---------------------------- 行内操作菜单 ---------------------------- */

/** 笔记：主操作是回原文，编辑 / 删除收进「⋯」。 */
function noteMenu(n: NoteRow): RowAction[] {
  return [
    { key: 'edit', label: '编辑笔记', icon: 'edit' },
    { key: 'delete', label: '删除笔记', icon: 'trash', danger: true, disabled: busyKey.value === `del:${n.id}` },
  ];
}

function onNoteAction(n: NoteRow, key: string) {
  if (key === 'edit') { openEdit('note', n); return; }
  if (key === 'delete') removeNote(n);
}

/** 高亮：只有一个删除操作，同样收进「⋯」。 */
function highlightMenu(h: HighlightRow): RowAction[] {
  return [
    { key: 'delete', label: '删除高亮', icon: 'trash', danger: true, disabled: busyKey.value === `del:${h.id}` },
  ];
}

function onHighlightAction(h: HighlightRow, key: string) {
  if (key === 'delete') removeHighlight(h);
}

/** 批注：同上，主操作是回原文。 */
function annotationMenu(a: AnnotationRow): RowAction[] {
  return [
    { key: 'edit', label: '编辑批注', icon: 'edit' },
    { key: 'delete', label: '删除批注', icon: 'trash', danger: true, disabled: busyKey.value === `del:${a.id}` },
  ];
}

function onAnnotationAction(a: AnnotationRow, key: string) {
  if (key === 'edit') { openEdit('annotation', a); return; }
  if (key === 'delete') removeAnnotation(a);
}
</script>

<template>
  <div>
    <div class="page-head">
      <div>
        <h1>笔记</h1>
        <div class="sub">跨论文汇总的笔记、高亮与批注</div>
      </div>
      <div class="grow"></div>
      <div class="page-actions">
        <label class="search-field">
          <AppIcon name="search" :size="15" />
          <input v-model="keyword" placeholder="搜索笔记" @keyup.enter="searchNow" />
        </label>
        <button class="btn icon" v-if="searching" title="清除搜索" @click="clearSearch">
          <AppIcon name="close" :size="15" />
        </button>
        <button class="btn icon" title="刷新" @click="refreshAll" :disabled="state.loading['notes-overview']">
          <AppIcon name="refresh" :size="15" />
        </button>
      </div>
    </div>

    <div class="tiny faint search-hint" v-if="searching">
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
          <div class="row wrap note-actions">
            <button class="btn sm" v-if="n.block_id" title="跳到这一段原文"
                    @click="openReader(n.paper_id, n.block_id, n.page)">
              <AppIcon name="locate" :size="14" />跳回原文
            </button>
            <span class="tiny faint" v-else>无关联原文块</span>
            <span class="spacer"></span>
            <span class="tiny faint mono" v-if="n.block_id">block {{ n.block_id }}</span>
            <Menu :items="noteMenu(n)" up :label="`更多操作：${n.id}`"
                  @select="onNoteAction(n, $event)" />
          </div>
        </div>
      </div>
      <div class="card empty-state" v-if="!notes.length">
        <span class="es-icon"><AppIcon name="note" :size="20" /></span>
        <span class="es-title">{{ searching ? '没有匹配的笔记' : '还没有笔记' }}</span>
        <span class="es-hint">
          {{ state.loading['notes-overview'] || state.loading['notes-search']
            ? '正在读取笔记…'
            : (searching ? '换个关键词再试，或清除搜索条件。' : '在阅读器里选中一段文字，即可新建笔记。') }}
        </span>
        <div class="es-actions" v-if="searching && !state.loading['notes-overview']">
          <button class="btn sm" @click="clearSearch">
            <AppIcon name="close" :size="14" />清除搜索
          </button>
        </div>
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
          <div class="row wrap note-actions">
            <button class="btn sm" v-if="h.block_id" title="跳到这一段原文"
                    @click="openReader(h.paper_id, h.block_id, h.page)">
              <AppIcon name="locate" :size="14" />跳回原文
            </button>
            <span class="tiny faint" v-else>无关联原文块</span>
            <span class="spacer"></span>
            <Menu :items="highlightMenu(h)" up :label="`更多操作：${h.id}`"
                  @select="onHighlightAction(h, $event)" />
          </div>
        </div>
      </div>
      <div class="card empty-state" v-if="!highlights.length">
        <span class="es-icon"><AppIcon name="quote" :size="20" /></span>
        <span class="es-title">还没有高亮</span>
        <span class="es-hint">
          {{ state.loading['notes-overview']
            ? '正在读取高亮…'
            : '在阅读器里选中文本后，用浮动菜单标记高亮颜色。' }}
        </span>
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
        <div class="row wrap note-actions">
          <button class="btn sm" v-if="a.block_id" title="跳到这一段原文"
                  @click="openReader(a.paper_id, a.block_id, a.page)">
            <AppIcon name="locate" :size="14" />跳回原文
          </button>
          <span class="tiny faint" v-else>无关联原文块</span>
          <span class="spacer"></span>
          <span class="tiny faint mono truncate" :title="a.id">{{ a.id }}</span>
          <Menu :items="annotationMenu(a)" up :label="`更多操作：${a.id}`"
                @select="onAnnotationAction(a, $event)" />
        </div>
      </div>
      <div class="card empty-state" v-if="!annotations.length">
        <span class="es-icon"><AppIcon name="sparkle" :size="20" /></span>
        <span class="es-title">还没有批注</span>
        <span class="es-hint">
          {{ state.loading['notes-overview']
            ? '正在读取批注…'
            : '选中文本后点「AI 解释」，或直接写一段手动批注。' }}
        </span>
      </div>
    </div>

    <!-- 删除确认（站内弹窗） -->
    <ConfirmModal
      :open="confirmBox.open"
      :title="confirmBox.title"
      :body="confirmBox.body"
      :target="confirmBox.target"
      :confirm-label="confirmBox.label"
      danger
      @confirm="runConfirm"
      @cancel="confirmBox.open = false"
    />

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
/* 高亮色点；**高亮文本的配色在 base.css**（全站唯一来源，正文/Markdown 视图/这里共用）。 */
.swatch { width: 15px; height: 15px; border-radius: var(--radius-ctl); border: 1px solid var(--border-strong); cursor: pointer; }

/* 卡片底部操作行：主操作（回原文）在左，⋯ 在右，形成一条基线 */
.note-actions { margin-top: 10px; }
.search-hint { margin-bottom: 8px; }

/* 批注卡片：问答 / 手动批注用不同左边框区分。 */
.annotation-item { border-left: 3px solid var(--border-strong); background: var(--bg-sunken); border-radius: var(--r-sm); padding: 10px 12px; margin-bottom: 8px; }
.annotation-item.manual { border-left-color: var(--accent); }
.annotation-item.qa { border-left-color: var(--ok); }
</style>
