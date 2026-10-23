<script setup lang="ts">
/**
 * 笔记整页（跨论文汇总）。
 *
 * 用户口径（2026-10-07）：**批注 / 高亮 / 笔记统一成「笔记」** —— 与阅读器侧栏同一个口径：
 * 一条 = **一处高亮（哪一段文字）+ 它的正文**；批注（划词解释 / 人工批注）按同一种卡片列出来。
 * 归并规则**只有一份**（`utils/notesFeed`），侧栏与这一页共用，免得两边慢慢长歪。
 *
 * 数据：`GET /notes/overview` 一次取回三类条目（各带 `paper_title`）；
 * 搜索走后端 `GET /notes?q=`（只过滤「笔记」）—— 搜索态下列表只留命中的那几条笔记。
 * 编辑：笔记 `PUT /notes/{id}`、批注 `PUT /annotations/{id}`；
 * **还没写正文的高亮**当场新建 `POST /notes`（口径不变：笔记必须挂在一处高亮上）。
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
  debounce, fmtRelative, truncate,
} from '@/utils/format';
import { readerTarget } from '@/utils/navigation';
import { buildNotesFeed, type NotesFeedRow } from '@/utils/notesFeed';
import { withKeptNoteRefs } from '@/utils/noteRefs';
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

/** 列表里的一条：**归并规则来自 `utils/notesFeed`**（与阅读器侧栏同一份）。 */
type FeedRow = NotesFeedRow<HighlightRow, NoteRow, AnnotationRow>;

interface NotesOverview {
  notes: NoteRow[];
  highlights: HighlightRow[];
  annotations: AnnotationRow[];
}

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
  explain: 'Cyli 解释',
  summary: '摘要',
  manual: '手动批注',
};

const ANNOTATION_SOURCE_LABEL: Record<string, string> = {
  selection: '划词',
  ai: 'AI 生成',
  manual: '手动',
  pipeline: '自动',
};

const overview = ref<NotesOverview | null>(null);
const keyword = ref('');
/** `null` = 没在搜索（用 overview 的全量）；否则是后端 `GET /notes?q=` 的命中 */
const searchItems = ref<NoteRow[] | null>(null);
const editOpen = ref(false);
/** 正在编辑的那条（整行，含它挂的高亮 / 批注） */
const editRow = ref<FeedRow | null>(null);
const editForm = reactive<{ content_md: string }>({ content_md: '' });
const saving = ref(false);
const busyKey = ref('');

const highlights = computed<HighlightRow[]>(() => overview.value?.highlights || []);
const annotations = computed<AnnotationRow[]>(() => overview.value?.annotations || []);
const allNotes = computed<NoteRow[]>(() => overview.value?.notes || []);
const searching = computed(() => searchItems.value !== null);
/** 搜索命中的笔记条数（页头那行提示用） */
const hitCount = computed(() => (searchItems.value ?? []).length);

/** 全量信息流（三类归并成一条条笔记） */
const feed = computed<FeedRow[]>(() => buildNotesFeed({
  highlights: highlights.value, notes: allNotes.value, annotations: annotations.value,
}));

/** 展示的列表：搜索态只留命中的那几条笔记（高亮/批注不参与检索，所以整条信息流过滤） */
const shownFeed = computed<FeedRow[]>(() => {
  const items = searchItems.value;
  if (items === null) return feed.value;
  const ids = new Set(items.map((n) => n.id));
  return feed.value.filter((r) => r.noteId && ids.has(r.noteId));
});

function titleOf(paperId?: string | null): string {
  if (!paperId) return '（未知论文）';
  const all: Array<NoteRow | HighlightRow | AnnotationRow> = [
    ...allNotes.value, ...highlights.value, ...annotations.value,
  ];
  const hit = all.find((x) => x.paper_id === paperId);
  return hit?.paper_title || paperTitle(paperId);
}

/** 这一条属于哪篇论文（笔记 / 高亮 / 批注任一处带着） */
function paperOf(row: FeedRow): { id: string; title: string } {
  const rec = row.highlight || row.note || row.annotation;
  return { id: rec?.paper_id ?? '', title: rec?.paper_title || titleOf(rec?.paper_id) };
}

/**
 * 跳回原文：登记/激活**阅读器标签**（论文类路由才进标签条）+ 带上锚点（`row`）与页码（`page`）。
 *
 * ⚠️ 只能在**点击回调**里调：`:to="fn()"` 会在每次渲染调用 `fn`，而 `openTab()` 会改响应式
 * tabs 状态（以前每次都赋新的 `query` 对象 → 永远"变了"）→ "渲染 → 改状态 → 再渲染"死循环，
 * 页面直接卡死。知识库页已经这么炸过一次。
 */
function openReader(paperId?: string | null, blockId?: string | null, page?: number | null) {
  void router.push(openTab(readerTarget(paperId ?? '', blockId, page)) as never);
}

function colorHex(color?: string | null): string {
  /* 自定义色（`#rrggbb`）直通：以前会落到 `COLOR_HEX.yellow` 的兑底，色点就变成黄色了 */
  if (color && /^#[0-9a-fA-F]{6}$/.test(color)) return color;
  return (color && COLOR_HEX[color]) || COLOR_HEX.yellow;
}

function hlClass(color?: string | null): string {
  if (color && /^#[0-9a-fA-F]{6}$/.test(color)) return 'hl';
  return color && COLOR_HEX[color] ? `hl ${color}` : 'hl yellow';
}

/** 自定义色的**内联变量**（class 只认名字）—— 与正文行 / PDF 文字层 / Markdown 三处同一口径：
 *  `--hl-bg` 是荧光笔的 40% 底色，`--hl-ink` 是同色实线（下划线模式用它）。
 *  写变量而不是 `background`：「高亮效果」切换靠 `html[data-hl-style]` 这条规则，内联 background 会盖住它。 */
function hlStyle(color?: string | null): Record<string, string> {
  return color && /^#[0-9a-fA-F]{6}$/.test(color)
    ? { '--hl-bg': `${color}66`, '--hl-ink': color }
    : {};
}

/** 高亮挂在哪一面（三种锚点是三套位置模型，界面上要说清）。 */
const SCOPE_LABEL: Record<string, string> = {
  row: '正文行', markdown: 'Markdown', pdf: '原 PDF',
};
function scopeLabel(scope?: string | null): string {
  return (scope && SCOPE_LABEL[scope]) || String(scope ?? '');
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

/* ------------------------------ 编辑 / 删除 ------------------------------ */

/** 打开编辑器：笔记改正文、**还没写正文的高亮**当场新建、批注改正文。 */
function openEdit(row: FeedRow): void {
  editRow.value = row;
  // 编辑框只放**正文**：refs 那行注释要是也塞进去，用户看到的就是一坨 JSON（见 utils/noteRefs）
  editForm.content_md = row.contentText || '';
  editOpen.value = true;
}

const editTitle = computed(() => {
  const row = editRow.value;
  if (!row) return '编辑';
  if (row.kind === 'annotation') return '编辑批注';
  return row.noteId ? '编辑笔记' : '给这处高亮写笔记';
});

async function submitEdit(): Promise<void> {
  const row = editRow.value;
  if (!row) return;
  if (row.kind !== 'annotation' && !row.noteId && !editForm.content_md.trim()) {
    toast('笔记内容不能为空', 'warn', 2200);
    return;
  }
  saving.value = true;
  try {
    if (row.kind === 'annotation') {
      await api.put(`/annotations/${row.annotation?.id}`, { content_md: editForm.content_md });
      toast('批注已保存', 'ok');
    } else if (row.noteId) {
      /* 存的是「改好的正文 + 原来那份标号表」：只改正文，`[1]` 的跳转不丢 */
      await api.put(`/notes/${row.noteId}`, {
        content_md: withKeptNoteRefs(editForm.content_md, row.contentMd),
      });
      toast('笔记已保存', 'ok');
    } else {
      /* 口径：**只有插入了高亮，才能在高亮的基础上加笔记** —— 这里的高亮就是列表里那一条 */
      const hl = row.highlight;
      await api.post('/notes', {
        paper_id: hl?.paper_id,
        block_id: hl?.block_id,
        highlight_id: hl?.id,
        selected_text: hl?.text ?? '',
        start_offset: hl?.start,
        end_offset: hl?.end,
        content_md: withKeptNoteRefs(editForm.content_md, row.contentMd),
      });
      toast('笔记已保存', 'ok');
    }
    editOpen.value = false;
    await refreshAll();
  } catch (err) {
    reportError(err, '保存失败');
  } finally {
    saving.value = false;
  }
}

/**
 * 删除一条：批注删批注、**有高亮的删整组高亮**（挂在上面的笔记由后端级联带走）、
 * 其余（没挂高亮的老笔记）删笔记。
 */
function removeRow(row: FeedRow): void {
  const target = truncate(row.contentText || row.quote || row.key, 120) || '（空）';
  if (row.kind === 'annotation') {
    askConfirm({
      title: '删除批注',
      body: '批注删除后无法恢复。',
      target,
      action: async () => {
        busyKey.value = `del:${row.key}`;
        try {
          await api.del(`/annotations/${row.annotation?.id}`);
          toast('批注已删除', 'ok');
          await refreshAll();
        } catch (err) {
          reportError(err, '删除批注失败');
        } finally {
          busyKey.value = '';
        }
      },
    });
    return;
  }
  const withHighlight = row.anchorIds.length > 0;
  askConfirm({
    title: withHighlight ? '删除这处高亮' : '删除笔记',
    body: withHighlight
      ? '高亮删除后无法恢复，挂在这处高亮上的笔记会一起删掉。'
      : '笔记删除后无法恢复。',
    target,
    action: async () => {
      busyKey.value = `del:${row.key}`;
      try {
        if (withHighlight) await api.del(`/highlights/${row.anchorIds[0]}`);
        else await api.del(`/notes/${row.noteId}`);
        toast(withHighlight ? '高亮已删除' : '笔记已删除', 'ok');
        await refreshAll();
      } catch (err) {
        reportError(err, withHighlight ? '删除高亮失败' : '删除笔记失败');
      } finally {
        busyKey.value = '';
      }
    },
  });
}

/** 行内操作：主操作（回原文 / 改正文）都在卡片上，这里只留"改"和"删"。 */
function rowMenu(row: FeedRow): RowAction[] {
  const busy = busyKey.value === `del:${row.key}`;
  const items: RowAction[] = [
    row.kind === 'annotation'
      ? { key: 'edit', label: '编辑批注', icon: 'edit' }
      : { key: 'edit', label: row.noteId ? '编辑笔记' : '写笔记', icon: row.noteId ? 'edit' : 'add' },
    row.kind === 'annotation'
      ? { key: 'delete', label: '删除批注', icon: 'trash', danger: true, disabled: busy }
      : row.anchorIds.length
        ? { key: 'delete', label: '删除高亮', icon: 'trash', danger: true, disabled: busy }
        : { key: 'delete', label: '删除笔记', icon: 'trash', danger: true, disabled: busy },
  ];
  return items;
}

function onRowAction(row: FeedRow, key: string) {
  if (key === 'edit') { openEdit(row); return; }
  if (key === 'delete') removeRow(row);
}
</script>

<template>
  <div class="notes-page">
    <div class="page-head">
      <div>
        <h1>笔记</h1>
        <div class="sub">跨论文汇总：一条 = 一处高亮 + 它的正文（批注也在里面）</div>
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
      关键词「{{ keyword }}」命中 {{ hitCount }} 条笔记（高亮与批注不参与检索）。
    </div>

    <!-- 一条工具条：只留条数（缩略开关已移除，正文恒定两行 —— 见下方 `.clamp2`） -->
    <div class="feed-bar">
      <span class="tiny faint">{{ shownFeed.length }} 条</span>
    </div>

    <div
      v-for="row in shownFeed"
      :key="row.key"
      class="card feed-row"
      :class="row.kind"
      :data-item-kind="row.kind"
      :data-item-key="row.key"
    >
      <div class="card-head">
        <span v-if="row.color" class="swatch" :style="{ background: colorHex(row.color) }"
              :title="'高亮颜色：' + row.color"></span>
        <span class="badge accent">{{ paperOf(row).title }}</span>
        <!-- 这一条是"笔记"还是"还没写正文的高亮"还是批注（类型） -->
        <span class="badge" :class="row.badge">{{ row.label }}</span>
        <span v-if="row.annotation" class="badge">{{ sourceLabel(row.annotation.source) }}</span>
        <span v-if="row.annotation && confidenceText(row.annotation.confidence)" class="badge">
          {{ confidenceText(row.annotation.confidence) }}
        </span>
        <span v-if="row.annotation?.status && row.annotation.status !== 'active'" class="badge">
          {{ row.annotation.status }}
        </span>
        <span v-if="row.scope && row.kind !== 'annotation'" class="badge dim">
          {{ scopeLabel(row.scope) }}
        </span>
        <span class="spacer"></span>
        <span class="tiny faint">{{ fmtRelative(row.at) }}</span>
      </div>

      <div class="card-body">
        <!-- 引用原文（高亮那一段）：用 `mark.hl`，**配色与正文里的高亮同一份**（base.css） -->
        <div v-if="row.quote" class="small quote">
          <mark :class="hlClass(row.color)" :style="hlStyle(row.color)">{{ row.quote }}</mark>
        </div>
        <!-- 原文是占位符（在 PDF 里选中公式 / 图片块）：那一行本身就是公式，按块渲染出来。
             字体高亮没法给公式上色（mark 里塞块级 v-html 会破），所以这里只当"引用原文"显示。 -->
        <div
          v-else-if="row.quoteMd"
          class="small quote quote-md"
          v-html="mdToHtml(row.quoteMd)"
        />
        <!-- 笔记正文：**用户自己写的** → `breaks: true`（回车就是回车，也不把软换行接回一行） -->
        <div
          v-if="row.contentText"
          class="md small clamp2"
          v-html="mdToHtml(row.contentText, null, { breaks: true })"
        />
        <!-- 空态不另写一行：头部那枚「待写」徽章已经把状态说清楚了 -->

        <div v-if="row.annotation?.model_id" class="tiny faint" style="margin-top:4px">
          模型 {{ row.annotation.model_id }}
        </div>
        <div class="tiny muted" style="margin-top:4px" v-for="(s, i) in (row.annotation?.sources || [])" :key="i">
          来源：{{ s.paper_id }} · {{ s.block_id }} — {{ truncate(s.snippet || '', 120) }}
        </div>
        <div class="row wrap tiny" style="margin-top:8px" v-if="(row.note?.tags || []).length">
          <span class="badge" v-for="t in row.note?.tags" :key="t">{{ t }}</span>
        </div>

        <div class="row wrap note-actions">
          <button class="btn sm" v-if="row.anchorId || row.page" title="跳到这一段原文"
                  @click="openReader(paperOf(row).id, row.anchorId, row.page)">
            <AppIcon name="locate" :size="14" />跳回原文
          </button>
          <span class="tiny faint" v-else>无关联原文块</span>
          <button class="btn sm" :title="row.noteId ? '改这条笔记的正文' : '给这处高亮写笔记'"
                  @click="openEdit(row)">
            <AppIcon :name="row.kind === 'annotation' ? 'edit' : (row.noteId ? 'edit' : 'add')" :size="14" />
            {{ row.kind === 'annotation' ? '编辑批注' : (row.noteId ? '编辑笔记' : '写笔记') }}
          </button>
          <span class="spacer"></span>
          <span class="tiny faint mono truncate" v-if="row.anchorId">{{ row.anchorId }}</span>
          <Menu :items="rowMenu(row)" up :label="`更多操作：${row.key}`"
                @select="onRowAction(row, $event)" />
        </div>
      </div>
    </div>

    <div class="card empty-state" v-if="!shownFeed.length">
      <span class="es-icon"><AppIcon name="note" :size="20" /></span>
      <span class="es-title">{{ searching ? '没有匹配的笔记' : '还没有笔记' }}</span>
      <span class="es-hint">
        {{ state.loading['notes-overview'] || state.loading['notes-search']
          ? '正在读取笔记…'
          : (searching ? '换个关键词再试，或清除搜索条件。'
            : '在阅读器里划词 → 点一个高亮颜色 → 浮层里点「添加笔记」。') }}
      </span>
      <div class="es-actions" v-if="searching && !state.loading['notes-overview']">
        <button class="btn sm" @click="clearSearch">
          <AppIcon name="close" :size="14" />清除搜索
        </button>
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

    <Modal :open="editOpen" :title="editTitle" @close="editOpen = false">
      <label class="field">
        内容（Markdown，支持 $公式$）
        <textarea v-model="editForm.content_md" rows="8" data-feed-edit></textarea>
      </label>
      <div class="small muted" v-if="editRow">
        所属论文：{{ paperOf(editRow).title }}
        <span v-if="editRow.anchorId"> · {{ editRow.anchorId }}</span>
      </div>
      <template #footer>
        <button class="btn" @click="editOpen = false">取消</button>
        <button class="btn primary" :disabled="saving" data-feed-save @click="submitEdit">
          {{ saving ? '保存中…' : '保存' }}
        </button>
      </template>
    </Modal>
  </div>
</template>

<style scoped>
/* 高亮色点；**高亮文本的配色在 base.css**（全站唯一来源，正文/Markdown 视图/这里共用）。 */
.swatch { width: 15px; height: 15px; border-radius: var(--radius-ctl); border: 1px solid var(--border-strong); }

/* 卡片底部操作行：主操作（回原文 / 改正文）在左，⋯ 在右，形成一条基线 */
.note-actions { margin-top: 10px; }
.search-hint { margin-bottom: 8px; }

/* 归并后的信息流：一圈工具条（条数 + 缩略开关）+ 一张张卡片。
   左边框沿用语义色：有正文的笔记=主题色、还没写的（高亮）=暗一档、批注=绿。 */
.feed-bar { display: flex; align-items: center; gap: 6px; margin: 12px 0 8px; }
.feed-row { border-left: 3px solid var(--accent); }
.feed-row.highlight { border-left-color: var(--border-strong); }
.feed-row.annotation { border-left-color: var(--ok); }
/* 正文恒定两行（缩略开关已移除，只保留这套显示口径） */
.clamp2 {
  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical;
  overflow: hidden; line-clamp: 2;
}
.quote { margin: 0 0 6px; }
/* 公式当原文时（原文是占位符）：块级、约两行半高，超出裁掉 */
.quote-md { max-height: 4.6em; overflow: hidden; }
</style>
