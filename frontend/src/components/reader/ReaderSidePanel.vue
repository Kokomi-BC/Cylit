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
import { computed, nextTick, ref, watch } from 'vue';
import { mdToHtml } from '@/composables/useMarkdown';
import { fmtRelative } from '@/utils/format';
import { buildNotesFeed, type NotesFeedRow } from '@/utils/notesFeed';
import { groupPaperTasks } from '@/utils/tasks';
import type {
  Annotation, FigureItem, GlossaryTerm, Highlight, MarkdownFormula, Note, PaperReference,
  TaskInfo, ZoomTarget,
} from '@/api/types';
import AppIcon from '@/components/AppIcon.vue';
import ProgressBar from '@/components/ProgressBar.vue';
import InsightPanel from '@/components/insight/InsightPanel.vue';
import ReferenceList from './ReferenceList.vue';
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
}>(), { formulasLoading: false, briefSource: '', references: () => [] });

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

/** 批注 / 笔记正文：**作者自己敲的** → `breaks: true`（回车即换行，且不走 PDF 式软换行接回） */
const annotationHtml = (text: string) => mdToHtml(text, null, { breaks: true });

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
  // 编辑器是 `v-if` 出来的：等它真的挂上再量高度（同时让已有的长笔记先铺开）
  void nextTick(() => autoGrowNoteEdit());
}
function cancelEditRow() {
  editKey.value = '';
  draft.value = '';
}

/**
 * 笔记编辑框：**跟着内容自己长高**（用户口径 2026-10-10："自适应展开大小 / 内容多时自动展开到底部"）。
 *
 * 以前是固定 `rows=4` ＋ 框内滚动 —— 笔记一长，人就只能在那条窄缝里上下翻。
 * 做法与聊天输入框同一套（见 `ReaderChat` 的自动增高）：**先把高度归零**、再按 `scrollHeight` 顶上去；
 * 到"侧栏能看见的高度"（60vh）就不再长，多出来的交给框内滚动 ——
 * 不然一条长笔记会把整个侧栏撑得老长。
 *
 * ⚠️ 用 **事件对象 / 选择器**取元素，不用模板 `ref`：`input` 事件本身就带着目标，
 *   不依赖 ref 名绑定、也不用猜 DOM 换没换好（组件里同时只会有一个编辑器）。
 */
function autoGrowNoteEdit(ev?: Event) {
  const el = (ev?.target as HTMLTextAreaElement | undefined)
    ?? document.querySelector<HTMLTextAreaElement>('textarea[data-pane-note-edit]');
  if (!el || !el.style) return;
  try {
    /* 上限 = **侧栏可视区底部**（用户口径 2026-10-10："应该可以占用整个元素面积方便编辑"）——
       内容多时就把编辑框一直铺到面板底边，只在"底部那行按钮 + 一点边距"处收住；
       内容少时仍然贴合自身高度（不会拉出一个大片空白）。 */
    const pane = el.closest('.panel-body') as HTMLElement | null;
    const paneBottom = pane?.getBoundingClientRect().bottom;
    const reserve = 58;   // 保存 / 取消那一行 + 边距
    /* 两条上限取小：① 面板底边（侧栏本身可滚时它才是"看得见的底"）；
       ② 视口底边 —— 面板不滚时它的 `bottom` 会在视口之外，只认它就会拉过头。 */
    const vhMax = window.innerHeight - el.getBoundingClientRect().top - 70;
    const paneMax = typeof paneBottom === 'number' ? paneBottom - el.getBoundingClientRect().top - reserve : Infinity;
    const max = Math.max(160, Math.round(Math.min(paneMax, vhMax)));
    el.style.height = '0px';   // 先归零：否则 `scrollHeight` 只会单调变大（删字不会缩回去）
    const h = Math.min(el.scrollHeight, max);
    el.style.height = `${h}px`;
    el.style.overflowY = el.scrollHeight > max ? 'auto' : 'hidden';
  } catch { /* 量不到就算了，退化成默认高度 */ }
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
            v-if="r.kind !== 'annotation' && editKey !== r.key"
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
            placeholder="写笔记…"
            @input="autoGrowNoteEdit"
            @keyup.ctrl.enter="saveRow(r)"
          />
          <div class="row" style="gap:6px">
            <button class="btn sm primary" data-pane-note-save @click="saveRow(r)">保存</button>
            <button class="btn sm" @click="cancelEditRow">取消</button>
          </div>
        </template>
        <template v-else>
          <div
            v-if="r.contentMd"
            class="small md"
            :class="{ clamp1: thumb }"
            v-html="annotationHtml(r.contentMd)"
          />
          <!-- 不用再写一行“还没写笔记”：头部那枚「待写」徽章已经说过了（用户口径 2026-10-10："移除多余的文字"） -->
        </template>
      </div>
    </template>

    <!-- 引用文献（用户口径 2026-10-07 第三条）：正文里的 `[2, 4, 10]` 点开就是这里，
         并且**只显示被提到的那几条**（过滤条上写清"只看这几条 / 显示全部"）。
         点条目 → 去发表网站（DOI / arXiv / 原文里的链接，兜底学术搜索）。 -->
    <template v-if="props.tab === 'references'">
      <div class="ref-bar">
        <!-- 用户口径 2026-10-08：顶栏写成 `REFERENCES 47 条`（原来的"47 条（按论文里的顺序）"太长）。
             顺序说明挪到 tooltip 里 —— 信息还在，但不再占着顶栏。 -->
        <span class="tiny faint" title="按论文里的顺序">
          <span class="ref-cap">REFERENCES</span>
          {{ (props.references ?? []).length }} 条
        </span>
      </div>
      <div v-if="!(props.references ?? []).length" class="empty small">
        这篇论文没有参考文献（解析时没抽到 References 一节）
      </div>
      <!-- 卡片本体在 `ReferenceList` 里：**与论文详情弹窗、正文标号的浮层共用同一份**（样式也自带） -->
      <ReferenceList v-else :items="props.references ?? []" />
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
              bbox: fig.bbox ?? null,
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
/* 编辑态：把卡片让给编辑框（用户口径 2026-10-10："编辑笔记时，可以隐藏顶部的部分" →
   "应该可以占用整个元素面积方便编辑"）——
   徽章 / 页码 / 时间 / 定位删除按钮那一整行、以及引用原文全部收起，
   只留编辑框与底部「保存 / 取消」。要定位或删除先按「取消」退回查看态即可。 */
.item.note-row.editing .row.tiny,
.item.note-row.editing .quote { display: none; }
/* 笔记编辑框（用户口径 2026-10-10："优化一下侧栏编辑时的文本框样式"）：
   全局那条 `input, select, textarea` 是给**表单控件**写的（白底 + 深色描边 + 聚焦时一整圈 accent 光环），
   摆在笔记卡里像个"被塞进来的控件"。这里改成与阅读器浮层同一门语言：
   淡底 + 发丝描边，聚焦只把描边点亮一档（**不要 `--ring` 那圈光晕** —— 在窄侧栏里太吵，
   而且它就是全局 `textarea:focus` 带进来的）；顺手把浏览器自带的右下角 resize 角标去掉。 */
.item.note-row .note-edit {
  width: 100%; box-sizing: border-box; margin: 4px 0 6px; padding: 9px 11px;
  border: 1px solid var(--divider); border-radius: var(--r-sm);
  background: var(--bg-layer); color: var(--text); font: inherit; font-size: 13px;
  line-height: 1.7; resize: none;
  transition: border-color var(--dur) var(--ease), background var(--dur) var(--ease);
}
/* 覆盖全局的 `textarea:hover { background: var(--bg-hover) }`（不然鼠标一过就闪一下灰） */
.item.note-row .note-edit:hover { background: var(--bg-layer); border-color: var(--border-strong); }
.item.note-row .note-edit:focus {
  outline: none; box-shadow: none;
  border-color: color-mix(in srgb, var(--accent) 60%, transparent);
  background: var(--bg-elev);
}
.item.note-row .note-edit::placeholder { color: var(--text-faint); }
/* 引用文献：工具条（那一行 `REFERENCES N 条`）在这里，卡片本身在 `ReferenceList` 里
   （与论文详情弹窗、正文标号的浮层共用同一份，所以那些 `.ref-item` / `.ref-lead` 样式也搬过去了）。 */
.ref-bar {
  display: flex; align-items: center; gap: 6px;
  margin: 0 0 6px; padding: 2px 0;
}
/* 顶栏那个组名：小一号、加宽字距的大写标签，跟条数拉开一点层次 */
.ref-cap { letter-spacing: .08em; font-weight: 600; margin-right: 2px; }
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
  display: -webkit-box; -webkit-line-clamp: 2; line-clamp: 2;
  -webkit-box-orient: vertical; overflow: hidden;
}
</style>
