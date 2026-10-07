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
import { computed, ref, watch } from 'vue';
import { mdToHtml } from '@/composables/useMarkdown';
import { fmtRelative } from '@/utils/format';
import { buildNotesFeed, type NotesFeedRow } from '@/utils/notesFeed';
import { referenceUrl } from '@/utils/references';
import { groupPaperTasks } from '@/utils/tasks';
import type {
  Annotation, FigureItem, GlossaryTerm, Highlight, MarkdownFormula, Note, PaperReference,
  TaskInfo, ZoomTarget,
} from '@/api/types';
import AppIcon from '@/components/AppIcon.vue';
import ProgressBar from '@/components/ProgressBar.vue';
import InsightPanel from '@/components/insight/InsightPanel.vue';
import ReaderFormulaList from './ReaderFormulaList.vue';

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
export type SideTab = 'notes' | 'references' | 'formulas' | 'figures' | 'terms' | 'tasks' | 'insight';

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
  /** 参考文献页签的过滤：点了正文里的引用标号后**只显示这些编号**（空 = 全部） */
  refFilter?: number[];
  formulas: MarkdownFormula[];
  formulasLoading?: boolean;
  figures: FigureItem[];
  glossary: GlossaryTerm[];
  briefDraft: BriefDraft;
  /** 简报来源（model / manual / cached），只用于视图上的一枚小徽章 */
  briefSource?: string;
  /** 「任务」页签：这篇论文的**全部**任务（各类型都算），页签里按类型汇总成清单 */
  tasks: TaskInfo[];
  /** 「论文解读」页签：按论文取解读结果（整页版共用同一份缓存） */
  paperId: string;
  /** 这篇论文有理解任务在跑：解读内容据此决定要不要每 5s 轮询 */
  insightRunning: boolean;
}>(), { formulasLoading: false, briefSource: '', refFilter: () => [], references: () => [] });

const emit = defineEmits<{
  (e: 'focus', rowId: string, page?: number): void;
  (e: 'copy', text: string): void;
  (e: 'zoom', target: ZoomTarget): void;
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
  /** 「引用文献」页签：把"只看正文提到的这几条"取消，回到全量 */
  (e: 'clear-ref-filter'): void;
  (e: 'cancel-task', task: TaskInfo): void;
  /** 解读页签里刚发起过解读任务：让 ReaderView 立刻刷一次任务列表（轮询据此启动） */
  (e: 'insight-queued'): void;
  /** 解读页签右上角那枚图标按钮：让 ReaderView 在**应用内**新开一个标签页 */
  (e: 'open-insight-page'): void;
}>();

function locate(id?: string | null, page?: number | null) {
  if (!id && !page) return;
  emit('focus', id ?? '', page ?? undefined);
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

const annotationHtml = (text: string) => mdToHtml(text);

/** 面 → 中文（侧栏那枚小徽章）。三种锚点是三套位置模型，界面上要说清挂在哪。 */
const SCOPE_LABEL: Record<string, string> = {
  row: '正文行', markdown: 'Markdown 视图', pdf: '原 PDF',
};

/* ------------------------------ 术语 / 文档简报 ------------------------------ */
/** 只读是默认态：进页签先看到**内容**，点「编辑」才展开输入框（用户口径）。 */
const editing = ref(false);
/** 换页签就退出编辑态，免得切回来还停在半改一半的输入框上。 */
watch(() => props.tab, () => { editing.value = false; });

const briefSourceLabel = computed(() => {
  const s = props.briefSource;
  return s === 'model' ? '模型生成' : s === 'manual' ? '人工修订'
    : s === 'cached' ? '已有简报' : '';
});
/** 简报卡片的左边框色沿用批注/笔记的语义：人工=主题色、模型=绿、其它=中性。 */
const briefCardClass = computed(() => (
  props.briefSource === 'manual' ? 'item manual'
    : props.briefSource === 'model' ? 'item auto' : 'item'));
/** 视图态判空：只要有任何一段内容就算「有简报」，不要因为 domain 为空就整块空掉。 */
const hasBrief = computed(() => Boolean(
  props.briefDraft.domain || props.briefDraft.style
  || props.briefDraft.notes.trim() || props.briefDraft.keepAsIsText.trim()
  || props.briefDraft.glossaryText.trim() || props.glossary.length,
));
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
/** 缩略显示（用户口径）：每条只占一两行，长笔记多了也一眼扫得完。选择记在本地。 */
const THUMB_KEY = 'cyl.notes.thumb';
const thumb = ref(localStorage.getItem(THUMB_KEY) === '1');
function toggleThumb() {
  thumb.value = !thumb.value;
  localStorage.setItem(THUMB_KEY, thumb.value ? '1' : '0');
}

/** 正在编辑的那条（`key`）+ 草稿。保存成功后靠 `watch` 收起来（见下面）。 */
const editKey = ref('');
const draft = ref('');
function startEditRow(r: FeedRow) {
  editKey.value = r.key;
  draft.value = r.contentMd;
}
function cancelEditRow() {
  editKey.value = '';
  draft.value = '';
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
const merged = computed<FeedRow[]>(() => buildNotesFeed({
  highlights: props.highlights, notes: props.notes, annotations: props.annotations,
}));

/** 参考文献页签要显示的那几条：有过滤就只显示被正文提到的，否则全部。 */
const shownRefs = computed<PaperReference[]>(() => {
  const all = props.references ?? [];
  const want = props.refFilter ?? [];
  if (!want.length) return all;
  const set = new Set(want);
  return all.filter((r) => set.has(Number(r.order_no)));
});

/** 点一条文献 → 去它的**发表网站**（DOI/arXiv/原文里的链接，兜底是学术搜索，见 utils/references）。 */
function openRef(ref: PaperReference) {
  window.open(referenceUrl(ref), '_blank', 'noopener');
}

/** 删掉一条：批注删批注、有高亮的删整组高亮（它的笔记由后端级联带走）、其余删笔记。 */
function removeRow(r: FeedRow) {
  if (r.kind === 'annotation') { emit('delete-annotation', r.annotation?.id ?? r.key.slice(2)); return; }
  if (r.anchorIds.length) { emit('delete-highlight', r.anchorIds.slice()); return; }
  if (r.noteId) emit('delete-note', r.noteId);
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
    />

    <!-- 笔记（用户口径 2026-10-07：批注 / 高亮 / 笔记统一成这一个页签：
         一条 = 一处高亮 + 它的正文；批注也按同一种卡片列在这里） -->
    <template v-if="props.tab === 'notes'">
      <div class="notes-bar">
        <span class="tiny faint">{{ merged.length }} 条</span>
        <span class="spacer" />
        <button
          class="btn icon sm"
          :title="thumb ? '展开显示（按排版渲染正文）' : '缩略显示（每条只占一两行）'"
          :aria-pressed="thumb"
          data-notes-thumb
          @click="toggleThumb"
        >
          <AppIcon :name="thumb ? 'expand' : 'list'" :size="13" />
        </button>
      </div>
      <div v-if="!merged.length" class="empty small">
        还没有笔记（在正文里划词 → 高亮 → 写笔记）
      </div>
      <div
        v-for="r in merged"
        :key="r.key"
        class="item note-row"
        :class="[r.kind, { thumb, editing: editKey === r.key }]"
        :data-note-id="r.noteId || undefined"
        :data-item-kind="r.kind"
      >
        <div class="row tiny">
          <span class="badge" :class="r.badge">{{ r.label }}</span>
          <!-- 笔记挂在哪一面（`row` / `markdown` / `pdf`）：三套锚点不同，界面要说清 -->
          <span v-if="r.scope" class="badge dim" :title="`高亮位置：${SCOPE_LABEL[r.scope] || r.scope}`">
            {{ SCOPE_LABEL[r.scope] || r.scope }}
          </span>
          <span v-if="r.page" class="faint">p{{ r.page }}</span>
          <span v-if="r.at" class="faint">{{ fmtRelative(r.at) }}</span>
          <span class="spacer" />
          <button class="btn icon sm" title="定位" data-item-locate @click="locate(r.anchorId, r.page)">
            <AppIcon name="locate" :size="13" />
          </button>
          <button
            v-if="r.kind !== 'annotation'"
            class="btn icon sm"
            :title="r.noteId ? '编辑这条笔记' : '给这处高亮写笔记'"
            data-item-edit
            @click="startEditRow(r)"
          >
            <AppIcon :name="r.noteId ? 'edit' : 'add'" :size="13" />
          </button>
          <button
            class="btn icon sm"
            :title="r.anchorIds.length ? '删除这处高亮（挂在上面的笔记一起删）' : '删除'"
            data-item-delete
            @click="removeRow(r)"
          >
            <AppIcon name="trash" :size="13" />
          </button>
        </div>
        <!-- 高亮原文：也是文档里的原文，同样按 Markdown 渲染（含公式） -->
        <div
          v-if="r.quote"
          class="small faint md quote"
          :class="{ clamp1: thumb }"
          v-html="mdToHtml(`“${r.quote}”`)"
        />
        <template v-if="editKey === r.key">
          <textarea
            v-model="draft"
            class="note-edit"
            rows="4"
            data-pane-note-edit
            placeholder="用 Markdown 写这条笔记；支持 $公式$（Ctrl+Enter 保存）"
            @keyup.ctrl.enter="saveRow(r)"
          />
          <div class="row" style="gap:6px">
            <button class="btn sm primary" data-pane-note-save @click="saveRow(r)">保存</button>
            <button class="btn sm" @click="cancelEditRow">取消</button>
            <span class="spacer" />
            <span class="tiny faint">{{ r.anchorIds.length ? '挂在这处高亮上' : '' }}</span>
          </div>
        </template>
        <template v-else>
          <div
            v-if="r.contentMd"
            class="small md"
            :class="{ clamp1: thumb }"
            v-html="annotationHtml(r.contentMd)"
          />
          <div v-else-if="r.kind === 'highlight'" class="tiny faint">还没写笔记（点铅笔补上）</div>
        </template>
      </div>
    </template>

    <!-- 引用文献（用户口径 2026-10-07 第三条）：正文里的 `[2, 4, 10]` 点开就是这里，
         并且**只显示被提到的那几条**（过滤条上写清"只看这几条 / 显示全部"）。
         点条目 → 去发表网站（DOI / arXiv / 原文里的链接，兜底学术搜索）。 -->
    <template v-if="props.tab === 'references'">
      <div class="ref-bar">
        <span class="tiny faint">
          <template v-if="(props.refFilter ?? []).length">
            只看正文提到的 {{ shownRefs.length }} 条（共 {{ (props.references ?? []).length }} 条）
          </template>
          <template v-else>{{ (props.references ?? []).length }} 条（按论文里的顺序）</template>
        </span>
        <span class="spacer" />
        <button
          v-if="(props.refFilter ?? []).length"
          class="btn sm ghost"
          data-ref-clear
          @click="emit('clear-ref-filter')"
        >
          显示全部
        </button>
      </div>
      <div v-if="!shownRefs.length" class="empty small">
        {{ (props.references ?? []).length
          ? '正文提到的这几条在参考文献里没有对应编号（解析出的编号可能对不上）'
          : '这篇论文没有参考文献（解析时没抽到 References 一节）' }}
      </div>
      <button
        v-for="r in shownRefs"
        :key="r.id"
        class="item ref-item"
        :data-ref-no="r.order_no"
        :title="r.raw"
        @click="openRef(r)"
      >
        <!-- 标号**内联**在标题前面（用户口径 2026-10-07：最左侧不整列显示标号，
             腾出来的宽度给中英对照）；右上角那枚跳转图标也去掉了，整张卡片就是那个跳转。 -->
        <span class="ref-head">
          <span class="ref-no">[{{ r.order_no }}]</span>
          <span v-if="r.title" class="ref-zh">{{ r.title }}</span>
        </span>
        <!-- 英文那一行：**只有标题**（作者段收起来了 → 用户口径"隐藏作者、把显示效果拉满"），
             整条原文（含作者、会议、年份、链接）在 tooltip 里，鼠标停一下就能看到。 -->
        <span class="ref-en clamp2">{{ r.title ? (r.title_en || r.raw) : r.raw }}</span>
      </button>
    </template>

    <!-- 公式（从 Markdown 提取，只读） -->
    <template v-if="props.tab === 'formulas'">
      <ReaderFormulaList
        :items="props.formulas"
        :loading="props.formulasLoading"
        @focus="(id: string, page?: number) => emit('focus', id, page)"
        @copy="emit('copy', $event)"
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
          <div v-if="fig.caption" class="fig-caption md" v-html="mdToHtml(fig.caption)" />
          <span v-else class="spacer" />
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
            :title="`${fig.caption || '插图'}（点击看大图）`"
            @click="emit('zoom', {
              url: fig.card_url || fig.image_url,
              caption: fig.caption, page: fig.page, number: fig.number,
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
        <span v-if="briefSourceLabel" class="badge dim">{{ briefSourceLabel }}</span>
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
        <div :class="briefCardClass">
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
                <span v-for="k in keepLines" :key="k" class="badge mono">{{ k }}</span>
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
/* 笔记页签：顶部一条**细工具条**（条数 + 缩略开关），滚动时钉在顶上。
   负 margin 正好吸收 `.panel-body` 的 padding（左右 8px），工具条才能通到边。
   左边框用高亮笔的颜色语义 —— 还没写正文的（`highlight`）压暗一档，一眼分得出"哪些还没写"。 */
.notes-bar {
  position: sticky; top: -8px; z-index: 2;
  display: flex; align-items: center; gap: 6px;
  margin: -8px -8px 8px; padding: 4px 8px;
  background: var(--bg-elev); border-bottom: 1px solid var(--divider);
}
.item.note-row { border-left-color: var(--accent); }
.item.note-row.highlight { border-left-color: color-mix(in srgb, var(--accent) 35%, var(--border-strong)); }
.item.note-row.annotation { border-left-color: var(--ok); }
.item.note-row .row { gap: 6px; }
/* 缩略显示（用户口径）：每条只占一两行 —— 引用一行、正文一行，点开编辑器才铺开 */
.item.note-row.thumb { padding: 6px 9px; margin-bottom: 5px; }
.clamp1 {
  display: -webkit-box; -webkit-line-clamp: 1; -webkit-box-orient: vertical;
  overflow: hidden; line-clamp: 1;
}
.item.note-row.thumb .quote { margin-bottom: 0; }
.item.note-row .note-edit {
  width: 100%; box-sizing: border-box; margin: 4px 0 6px; padding: 6px 8px;
  border: 1px solid var(--border-strong); border-radius: var(--radius-sm);
  background: var(--bg-elev); color: var(--text); font: inherit; font-size: 12.5px;
  line-height: 1.6; resize: vertical;
}
.item.note-row .note-edit:focus { outline: none; border-color: var(--accent); }
/* 引用文献：一条两行 —— 第一行「标号 + 中文标题」（标号内联，不占一整列），
   第二行英文（只放标题：作者段在 tooltip 里）。整张卡片都是跳转按钮。 */
.ref-bar {
  display: flex; align-items: center; gap: 6px;
  margin: 0 0 6px; padding: 2px 0;
}
.ref-item {
  display: flex; flex-direction: column; gap: 3px; width: 100%; text-align: left;
  font-family: inherit; font-size: 12px; color: var(--text); cursor: pointer;
  border-left-color: var(--border-strong);
}
.ref-item:hover { border-color: var(--accent); background: var(--bg-hover); }
.ref-head { display: block; }
.ref-no { font-family: var(--mono); font-size: 11px; color: var(--text-faint); margin-right: 4px; }
.ref-zh { font-weight: 600; }
.ref-en { color: var(--text-dim); font-size: 11.5px; line-height: 1.5; }
/* 两行封顶：原文常常很长（一整条参考文献），全文看 hover 的 tooltip */
.clamp2 {
  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical;
  overflow: hidden; line-clamp: 2;
}
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
  font-size: 12px; line-height: 1.5; color: var(--text-dim);
  overflow-wrap: anywhere;
}
/* 图注/引用是**渲染过的 Markdown**（外层已经定了字号与行高，这里只把块级元素的间距压掉，
   免得一行短图注被 `<p>` 的默认外边距撑成两行） */
.fig-caption :deep(p) { margin: 0; }
.fig-caption :deep(.katex-display) { margin: 0; overflow-x: auto; overflow-y: hidden; }
.quote :deep(p) { margin: 0; display: inline; }
.fig-head .spacer { flex: 1; }
/* 光学对齐：按钮 26px、图注行盒 18px，上移 4px 后两者的**中心线**才对得上（图标不会看起来偏低）。 */
.fig-locate { flex: none; margin-top: -4px; color: var(--text-dim); }
.fig-locate:hover { color: var(--accent-text); }
.fig-body { display: flex; }
.fig-body .figure { width: 100%; }
.fig-meta { margin-top: 2px; gap: 6px; }
/* 简报：只读态是「小标题 + 正文」的排版（跟侧栏其它内容一致），不是一组表单框。
   编辑态的输入框由 `.field` 提供样式，两者互斥出现。 */
.brief-bar { gap: 6px; margin-bottom: 8px; }
.brief-sec { margin: 0 0 10px; }
.brief-sec.last { margin-bottom: 0; }
.brief-k {
  font-size: 10.5px; color: var(--text-faint); letter-spacing: .04em;
  text-transform: uppercase; margin-bottom: 2px;
}
.brief-v { font-size: 12.5px; line-height: 1.6; color: var(--text); overflow-wrap: anywhere; }
.brief-list {
  margin: 0; padding-left: 16px;
  font-size: 12.5px; line-height: 1.6; color: var(--text);
}
.brief-list li { margin: 1px 0; }
.brief-chips { display: flex; flex-wrap: wrap; gap: 4px; }
.brief-terms-head { margin: 12px 0 2px; gap: 6px; }
.term-row { display: flex; gap: 8px; padding: 5px 6px; border-bottom: 1px solid var(--divider); font-size: 12.5px; }
.term-row .en { flex: 1; font-family: var(--mono); font-size: 12px; }
.term-row .zh { flex: 1; }
/* 任务清单：一类一行。类型名固定宽度，几行的状态徽章才能对齐成一条竖线；
   失败原因单独一行、最多两行（长错误信息不该把清单撑成一屏）。 */
.task-row { padding: 7px 10px; margin-bottom: 6px; }
.task-name { font-size: 12.5px; font-weight: 600; min-width: 3.5em; }
/* 进度条：夹在信息行与失败原因之间留一点缝（`.bar` 本身只有 5px 高，不像卡片那样占地方） */
.task-bar { margin-top: 6px; }
.task-msg {
  margin-top: 4px; color: var(--danger); line-height: 1.5;
  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;
}
</style>
