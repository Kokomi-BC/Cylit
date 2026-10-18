<script setup lang="ts">
/**
 * 阅读器侧栏的**对话面板**（论文问答），**两级**：
 *
 * - 一级 = 会话列表：搜索（标题 + 正文）、分类（划词 / 常规）、范围（本论文 / 全部）、
 *   批量管理（多选 + 删除）、重命名；
 * - 二级 = 会话正文：消息流 + 输入区，输入区左下角那枚**圆环**是上下文用量，
 *   点它就是"压缩"（见 `compress`）。
 *
 * 三条数据纪律：
 * 1. **`done` 是权威结果**：流式期间推的 `citation.ref` 是原始编号，`done` 里的引用被压成
 *    1..M 且正文也改写过，所以收到 `done` 后**重拉一次会话**（顺带把落库结果对齐），
 *    不做增量合并；
 * 2. **引用跳转只认 `block_id` / `row_id`**（`ref` 只是给人看的编号）；
 * 3. **分叉不改树**：重新生成 / 编辑提问只是在同一个父节点下挂兄弟，切分支就是挪 `head`
 *    （后端 `set_head`），一个字节都不删。
 */
import { computed, nextTick, onActivated, onMounted, onUnmounted, ref, watch } from 'vue';
import { useRouter } from 'vue-router';
import { api, assetUrl } from '@/api/client';
import { postSse } from '@/api/stream';
import { mdToHtml, inlineHtml } from '@/composables/useMarkdown';
import { reportError, toast } from '@/stores/app';
import { askConfirm } from '@/stores/confirm';
import { openTab } from '@/stores/tabs';
import { debounce, fmtDate, fmtRelative, truncate } from '@/utils/format';
import { BRAND_ICON } from '@/utils/brand';
import { convCount } from '@/api/types';
import type {
  AgentAttachment, AgentCitation, AgentConversation, AgentMessage, AgentSelection,
  AgentStatus, AgentStep, AgentUsage,
} from '@/api/types';
import AppIcon from '@/components/AppIcon.vue';
import ConfirmModal from '@/components/ConfirmModal.vue';

const props = defineProps<{
  paperId: string;
  /** 从正文划词带过来的选区（可空）；发出去之后由父组件清掉 */
  selection?: AgentSelection | null;
  /** 能不能「浮层打开」（窄屏是抽屉，拎成浮窗没有意义 —— 由父组件判） */
  canFloat?: boolean;
  /** 已经在浮窗里了：浮窗标题栏自带「回到侧栏」，这里不再重复一枚拆分按钮 */
  floating?: boolean;
}>();

const emit = defineEmits<{
  (e: 'focus', blockId: string, page?: number): void;
  (e: 'clear-selection'): void;
  /** 拆分 / 浮层打开：把整个侧栏面板拎成浮窗（真正的外框由 `ReaderView` 负责） */
  (e: 'float'): void;
}>();

/* 拆分按钮：会话列表与会话两级都摆在**工具栏最右**（用户口径 2026-11-19：放在加号旁边），
   所以两级共用这一个判断，免得漏一处。 */
const showFloatBtn = computed(() => !!props.canFloat && !props.floating);

const RING_CIRCUMFERENCE = 2 * Math.PI * 15.5;
const router = useRouter();
/** 空态文案池里"与Cyli一起讨论…"的固定前缀（Cyli = 这位问答助手的名字） */
const PERSONA = '与Cyli一起讨论';

type Level = 'list' | 'thread';
type Kind = 'all' | 'selection' | 'normal';

const KINDS: Array<{ key: Kind; label: string }> = [
  { key: 'all', label: '全部' },
  { key: 'selection', label: '划词' },
  { key: 'normal', label: '常规' },
];

/* ------------------------------ 一级：列表 ------------------------------ */

const level = ref<Level>('list');
const conversations = ref<AgentConversation[]>([]);
const convQuery = ref('');
const convKind = ref<Kind>('all');
const batch = ref(false);
const picked = ref<string[]>([]);
const listLoading = ref(false);
const renaming = ref<{ id: string; text: string } | null>(null);

/* ------------------------------ 二级：会话 ------------------------------ */

const currentId = ref<string | null>(null);
const messages = ref<AgentMessage[]>([]);
const usage = ref<AgentUsage | null>(null);
const status = ref<AgentStatus | null>(null);
const paperTitle = ref('');
const draft = ref('');
const pending = ref<AgentAttachment[]>([]);
/** 划词带进来的选区 → 输入框里那枚**上下文胶囊**（用户口径 2026-10-03，仿 VS Code）。
 *
 *  以前它只是 `props.selection` 的转发：有就必然发出去，用户唯一的操作是"删掉"。
 *  现在**本组件自己持有**这份状态，于是发送前可以：
 *  - 整块点一下 = 这轮**要不要带上**它（`include`）；
 *  - 右侧 ✕ = 整个移除。
 *  `include=false` 时胶囊照样显示（用户看得见自己选过什么），只是不随这轮发出去。 */
const selCtx = ref<{ text: string; row_id: string | null; page: number | null;
                     where: string; include: boolean } | null>(null);
const uploading = ref(false);
const loading = ref(false);
const compressing = ref(false);
const sending = ref(false);
const errorText = ref('');
/**
 * **正在生成的那条助手消息**（本地占位，`message_id` 以 `local-assistant-` 开头）。
 *
 * 它就是一个普通消息，只是 `run.steps` 由 SSE 事件现拼 —— 好处是
 * "思考 / 正文 / 工具**按发生顺序混排**"只有**一套**渲染模板：
 * 流式期间拼出来的是它，落库后从 `run` 读出来的也是它（形状由后端 `agent/trace.py` 定）。
 */
const liveId = ref('');
/** 整轮耗时（跑表，每 200ms 一跳）；`done` 之后用服务端的权威值覆盖 */
const elapsedMs = ref(0);
let tickTimer: number | null = null;
/** 收起过程轨迹的消息 id（**默认收起**：从历史进来先看对话，想看过程再点开）。 */
const openRuns = ref<string[]>([]);
/** 展开长思考的键（`${message_id}:${步骤序号}`）；默认只露 4 行 */
const openSteps = ref<string[]>([]);
const editing = ref<{ id: string; text: string; branch: boolean } | null>(null);
const headline = ref('');
const dragging = ref(false);
const confirming = ref<{ ids: string[]; body: string; target: string } | null>(null);
const confirmBusy = ref(false);
const msgsEl = ref<HTMLElement | null>(null);
/* 输入区**浮在消息之上**（见 `.composer` 的注释）：浮起来就不占布局高度了，消息区得自己
   把这块空白留出来，否则最后一条会压在输入框底下滚不上来。高度用 ResizeObserver 量 ——
   textarea 随输入自动长高（一行 → 上限），写死一个值在长问题下就不够。 */
const composerEl = ref<HTMLElement | null>(null);
let composerRO: ResizeObserver | null = null;
watch(composerEl, (el) => {
  composerRO?.disconnect();
  composerRO = null;
  if (!el || typeof ResizeObserver === 'undefined') return;
  composerRO = new ResizeObserver(() => {
    if (msgsEl.value) msgsEl.value.style.paddingBottom = `${el.offsetHeight + 6}px`;
  });
  composerRO.observe(el);
});
const inputEl = ref<HTMLTextAreaElement | null>(null);
const fileInput = ref<HTMLInputElement | null>(null);
let controller: AbortController | null = null;

const current = computed(() => conversations.value.find(c => c.conversation_id === currentId.value) ?? null);
const currentTitle = computed(() => current.value?.title || '新对话');
const percent = computed(() => Math.max(0, Math.min(100, usage.value?.percent ?? 0)));
const ringDash = computed(() => `${(percent.value / 100) * RING_CIRCUMFERENCE} ${RING_CIRCUMFERENCE}`);
const modelName = computed(() => status.value?.model?.model_name || '—');
const agentReady = computed(() => Boolean(status.value?.running) && status.value?.model?.usable !== false);
/** 这一轮**真的要带出去**的选区（用户在胶囊上关掉之后就是 null）。 */
const selected = computed(() => (selCtx.value?.include ? selCtx.value : null));
const canSend = computed(() => !sending.value
  && (draft.value.trim().length > 0 || pending.value.length > 0 || Boolean(selected.value)));
const canCompress = computed(() => !compressing.value && messages.value.length >= 2);
const compressTitle = computed(() => (canCompress.value
  ? '压缩这段对话：让模型把已谈过的内容压成要点（会多出一轮「摘要」，原文仍可回看）'
  : '至少聊过一轮才能压缩'));
/** 圆环很小、里面不写字 —— 数值与用途都放 tooltip（用户口径：小圆环，中间不放图标） */
const ringTitle = computed(() => `${compressTitle.value}（当前上下文已用 ${Math.round(percent.value)}%）`);
const allPicked = computed(() => conversations.value.length > 0
  && picked.value.length === conversations.value.length);

function render(text?: string | null) {
  return mdToHtml(text ?? '', props.paperId);
}

function escapeAttr(text: string) {
  return String(text).replace(/&/g, '&amp;').replace(/"/g, '&quot;')
    .replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

/**
 * 正文里的 `[n]` → **可直接点击跳转的引用锚**（用户口径 2026-10-03）。
 *
 * 为什么不在回答下面再列一排 chunk 号：那是把"引用表"和"正文"拆成两处，
 * 读的时候要来回对号；点正文里那个 `[n]` 才是一步到位。
 *
 * 只在**渲染后的 HTML 上**替换，并且**跳过代码块**（`<code>x[0]</code>` 里的 `[0]` 不是引用）；
 * 另外只认这一轮**真实存在**的 ref，别把正文里恰好写成 `[1]` 的普通方括号变成按钮。
 * 锚点信息直接写进 `data-*`：这样点击时不必再去猜"这个编号属于哪条消息"
 * （每条消息的编号都是各自从 1 开始的，跨消息查表必然串号）。
 */
function decorateRefs(html: string, cites?: AgentCitation[]) {
  if (!cites?.length) return html;
  const byRef = new Map(cites.map(c => [c.ref, c]));
  const parts = html.split(/(<code[\s\S]*?<\/code>|<pre[\s\S]*?<\/pre>)/i);
  return parts.map((part, index) => {
    if (index % 2 === 1) return part;                       // 代码块原样返回
    return part.replace(/\[(\d{1,3})\]/g, (whole, num) => {
      const cite = byRef.get(Number(num));
      if (!cite) return whole;
      const title = escapeAttr(cite.snippet || cite.label || `引用 ${num}`);
      return `<button type="button" class="cite-inline" title="${title}"`
        + ` data-ref="${num}" data-paper="${escapeAttr(cite.paper_id || '')}"`
        + ` data-row="${escapeAttr(cite.row_id || '')}"`
        + ` data-block="${escapeAttr(cite.block_id || '')}"`
        + ` data-page="${cite.page ?? ''}">[${num}]</button>`;
    });
  }).join('');
}

/** 正文渲染：markdown + 把 `[n]` 接成可点锚点。 */
function renderAnswer(text: string | null | undefined, cites?: AgentCitation[]) {
  return decorateRefs(mdToHtml(text ?? '', props.paperId), cites);
}

/** 点正文里的 `[n]`：跳到那条引用（阅读器会滚到对应行）。 */
function onCitationClick(ev: MouseEvent) {
  const target = ev.target as HTMLElement | null;
  const btn = target?.closest?.('.cite-inline') as HTMLElement | null;
  if (!btn) return;
  const page = Number(btn.dataset.page || 0);
  gotoCitation({
    ref: Number(btn.dataset.ref || 0),
    paper_id: btn.dataset.paper || props.paperId,
    kind: 'block',
    block_id: btn.dataset.block || null,
    row_id: btn.dataset.row || null,
    page: page > 0 ? page : null,
    label: btn.getAttribute('title') || '',
  } as AgentCitation);
}

/** 划词那种以 `> 引用` 开头的用户消息：正文只显示引用之后的部分（引用单独排版）。 */
function splitQuote(content: string): { quote: string; rest: string } {
  const match = /^>\s?(.*)\n+([\s\S]*)$/.exec(content || '');
  if (!match) return { quote: '', rest: content || '' };
  return { quote: match[1].trim(), rest: match[2].trim() };
}

/** 胶囊整块可点 = 这轮要不要带上选区（仿 VS Code 的上下文胶囊：点它就切换）。 */
function toggleSelection() {
  if (selCtx.value) selCtx.value.include = !selCtx.value.include;
}

/** 移除选区：连父级那份一起清掉（父级只负责"带进来"，去留由这里决定）。 */
function dropSelection() {
  selCtx.value = null;
  emit('clear-selection');
}

function imageSrc(url?: string) {
  return assetUrl(url);
}

function fmtSize(bytes?: number) {
  const n = Number(bytes || 0);
  return n > 1048576 ? `${(n / 1048576).toFixed(1)} MB` : `${Math.max(1, Math.round(n / 1024))} KB`;
}

/** 思考耗时文案：10 秒以内给一位小数（"3.4 秒"和"3 秒"是两种感受）。 */
function fmtMs(ms?: number) {
  const n = Math.max(0, Number(ms || 0));
  if (!n) return '';
  return n < 10000 ? `${(n / 1000).toFixed(1)} 秒` : `${Math.round(n / 1000)} 秒`;
}

/** 整轮耗时：超过一分钟换成分秒（"19分39秒"是 DSH 那种抬头文案的口径） */
function fmtElapsed(ms?: number) {
  const n = Math.max(0, Math.round(Number(ms || 0)));
  if (!n) return '';
  const m = Math.floor(n / 60000);
  if (!m) return fmtMs(n);
  return `${m}分${Math.round((n % 60000) / 1000)}秒`;
}

/* ------------------------------ 过程轨迹 ------------------------------ */

/**
 * 工具 → **名词短语**（步骤行用）。与 `TOOL_LABELS`（进行时，给"正在…"抬头用）
 * 刻意分开：步骤行写的是"它干了什么"（翻书架），抬头写的是"它现在在干什么"。
 */
const TOOL_STEPS: Record<string, string> = {
  paper_search: '检索论文', kb_search: '检索知识库',
  page_markdown_get: '读正文', block_get: '读段落',
  page_image_get: '看页面图', figure_list: '看插图清单', figure_image_get: '看插图',
  formula_get: '取公式', translate_get: '取译文',
  library_list: '翻书架', notes_search: '查笔记',
  page_markdown_edit: '修订产物',
};

/** 工具 → 图标（用站点已有的那套；没有对应项的退回 `bolt`）。 */
const TOOL_ICONS: Record<string, string> = {
  paper_search: 'search', kb_search: 'knowledge',
  page_markdown_get: 'document', block_get: 'document',
  page_image_get: 'image', figure_list: 'list', figure_image_get: 'image',
  formula_get: 'formula', translate_get: 'translate',
  library_list: 'library', notes_search: 'note',
  page_markdown_edit: 'edit',
};

/** 抬头右侧那句话的**分类**：与 DSH 的"已读取文件，已搜索代码"同一种写法。 */
const TOOL_SUMMARY: Array<[string[], string]> = [
  [['paper_search', 'kb_search'], '已检索'],
  [['page_markdown_get', 'block_get'], '已读正文'],
  [['page_image_get', 'figure_image_get', 'figure_list'], '看过插图'],
  [['formula_get'], '看过公式'],
  [['translate_get'], '看过译文'],
  [['library_list'], '翻过书架'],
  [['notes_search'], '查过笔记'],
  [['page_markdown_edit'], '改过产物'],
];

function stepLabel(tool?: string) {
  return TOOL_STEPS[tool ?? ''] ?? '调用工具';
}

function toolIcon(tool?: string) {
  return TOOL_ICONS[tool ?? ''] ?? 'bolt';
}

/** 旧消息的兜底：没有 `run` 时用 `thinking` + `tool_trace` 拼一份同形状的轨迹。 */
function stepsOf(m: AgentMessage): AgentStep[] {
  const steps = m.run?.steps;
  if (steps?.length) return steps;
  if (m.role !== 'assistant') return [];
  const out: AgentStep[] = [];
  if (m.thinking) out.push({ kind: 'thinking', text: m.thinking });
  for (const t of m.tool_trace ?? []) {
    const tool = String(t?.tool || '');
    if (!tool || tool === '__usage__') continue;      // 用量那条不是"步骤"
    out.push({ kind: 'tool', tool, status: 'done',
               digest: typeof t?.args === 'object' ? '' : String(t?.args ?? ''),
               ms: Number(t?.ms || 0) || null });
  }
  return out;
}

/** 轨迹里的正文段（只算 assistant：用户消息没有轨迹）。 */
function textStepsOf(m: AgentMessage) {
  return m.role === 'assistant' ? stepsOf(m).filter(s => s.kind === 'text') : [];
}

/**
 * 最终回答**渲染在轨迹外面**（用户口径 2026-10-03："正文不要窝在思考的范围里、还断了一截"）。

 * 轨迹里最后一段正文就是回答；先前的正文段是过程里的话（调工具前说的半句），留在轨迹里。
 * 只有当各段拼起来**逐字等于** `content` 时才信轨迹 —— 否则直接用 `content`
 * （宁可少一层夹杂，也不能少一段回答：清洗器留尾、正文被截断那类问题的表现就是"回答少了"）。
 */
function finalAnswer(m: AgentMessage): string {
  if (m.role !== 'assistant') return m.content;
  const texts = textStepsOf(m);
  if (!texts.length) return m.content;
  const joined = texts.map(s => s.text ?? '').join('');
  return joined === (m.content ?? '') ? (texts[texts.length - 1].text ?? '') : m.content;
}

/** 轨迹里要显示的正文段 = 除最后一段（那段是回答本体，已经在外面了）。 */
function showInTrace(m: AgentMessage, step: AgentStep) {
  const texts = textStepsOf(m);
  if (texts.length < 2) return false;
  if (texts.map(s => s.text ?? '').join('') !== (m.content ?? '')) return false;
  return step !== texts[texts.length - 1];
}

/** 抬头右侧：它这一轮都干了什么（按出现的类别去重）。 */
function runSummary(steps: AgentStep[]) {
  const tools = new Set(steps.filter(s => s.kind === 'tool').map(s => s.tool ?? ''));
  const bits = TOOL_SUMMARY.filter(([names]) => names.some(n => tools.has(n)))
    .map(([, text]) => text);
  return bits.length ? `${bits.join(' · ')}${bits.length > 1 ? ' 等' : ''}` : '';
}

/** 抬头左侧：跑着的时候说"现在在干什么"，跑完说"用时多少"。 */
function runTitle(m: AgentMessage) {
  if (m.message_id === liveId.value) {
    const steps = stepsOf(m);
    const running = [...steps].reverse().find(s => s.kind === 'tool' && s.status === 'running');
    const tail = running ? `${toolLabel(running.tool ?? '')}…` : '正在思考…';
    return `${tail}${elapsedMs.value ? ` · ${fmtElapsed(elapsedMs.value)}` : ''}`;
  }
  const ms = m.run?.elapsed_ms || m.thinking_ms || 0;
  return ms ? `已完成，用时 ${fmtElapsed(ms)}` : '完成';
}

function isRunOpen(id: string) {
  // **默认收起**（从历史进来先看对话）；正在跑的那一轮在 `ask()` 里进这个集合，
  // 于是它默认展开、也**照样能点收起**（不再有"跑的时候点不动"的例外）。
  return openRuns.value.includes(id);
}

function toggleRun(id: string) {
  // ⚠️ 与 `isRunOpen` 同一套语义（**记"展开"而不是"收起"**）：默认收起后，
  // 这里写反过一次 —— 点抬头看见的是一动不动。
  openRuns.value = isRunOpen(id)
    ? openRuns.value.filter(x => x !== id)
    : [...openRuns.value, id];
}

function stepKey(id: string, i: number) {
  return `${id}:${i}`;
}

function isStepOpen(id: string, i: number) {
  return openSteps.value.includes(stepKey(id, i));
}

function toggleStep(id: string, i: number) {
  const key = stepKey(id, i);
  openSteps.value = isStepOpen(id, i) ? openSteps.value.filter(x => x !== key)
    : [...openSteps.value, key];
}

/** 步骤行右侧的参数摘要：`query=随机旋转` → `随机旋转`（键名对用户没意义）。 */
function fmtDigest(digest?: string) {
  const text = String(digest || '').trim();
  if (!text) return '';
  return text.split('、').map(part => part.replace(/^[a-z_]{2,12}=/, '')).join(' · ').slice(0, 60);
}

function ensureLive(): AgentMessage {
  const found = messages.value.find(m => m.message_id === liveId.value);
  if (found) return found;
  const msg: AgentMessage = {
    message_id: liveId.value, conversation_id: currentId.value ?? '', seq: 0,
    role: 'assistant', content: '', citations: [], run: { steps: [] },
  };
  messages.value = [...messages.value, msg];
  return msg;
}

/** 往当前这一段追加（同类相邻的合并；换类另起一段——这就是"夹杂"的来源）。 */
function appendLiveStep(kind: AgentStep['kind'], text: string) {
  if (!text) return;
  const steps = ensureLive().run!.steps!;
  const last = steps[steps.length - 1];
  if (last && last.kind === kind) last.text = (last.text ?? '') + text;
  else steps.push({ kind, text, round: 0 });
}

/* ------------------------------ 空态文案 ------------------------------ */

const WORD_STOP = new Set(['a', 'an', 'the', 'of', 'for', 'and', 'with', 'on', 'in', 'to',
  'via', 'using', 'based', 'towards', 'toward', 'is', 'are', 'you', 'need', 'can', 'we', 'it']);

/** 论文标签（与标题一起当关键词的来源） */
const paperTagText = ref('');

/** 从论文标题 / 标签里抽几个关键词（"与Cyli一起讨论 XX 的实现"里的 XX）。 */
function keywords(): string[] {
  const raw = `${paperTitle.value} ${paperTagText.value}`;
  const words = raw.split(/[^\p{L}\p{N}+#.-]+/u)
    .map(w => w.replace(/^[^\p{L}\p{N}]+|[^\p{L}\p{N}]+$/gu, ''))
    .filter(w => w.length > 2 && !WORD_STOP.has(w.toLowerCase()));
  return [...new Set(words)].slice(0, 4);
}

/** 文案池：**几条内容，随机取一条**（标题/关键词现取，所以不同论文不一样）。 */
function copyPool(): string[] {
  const title = paperTitle.value.trim();
  const kws = keywords();
  const out: string[] = [];
  if (title) out.push(`${PERSONA}《${truncate(title, 26)}》`);
  if (kws[0]) out.push(`${PERSONA} ${kws[0]} 的实现`);
  if (kws[1]) out.push(`${PERSONA} ${kws[1]} 和 ${kws[0]} 的关系`);
  if (kws[0]) out.push(`${PERSONA} ${kws[0]} 为什么有效`);
  out.push(`${PERSONA} 这篇论文的动机是什么`);
  out.push(`${PERSONA} 实验是怎么验证的`);
  return out;
}

function rollCopy() {
  const pool = copyPool();
  headline.value = pool[Math.floor(Math.random() * pool.length)] ?? `${PERSONA} 这篇论文`;
}

/* ------------------------------ 取数 ------------------------------ */

async function scrollToBottom() {
  await nextTick();
  const el = msgsEl.value;
  if (!el) return;
  /* 连滚几帧：正文里的 KaTeX、图片、折叠块都会在随后几帧继续把高度撑开，
     只滚一次会停在半路（用户报的"进来不是停在底部"）。 */
  for (let i = 0; i < 3; i += 1) {
    el.scrollTop = el.scrollHeight;
    await new Promise((done) => requestAnimationFrame(() => done(null)));
  }
  // 附件缩略图这类"加载完才知道高度"的，再补一次
  window.setTimeout(() => { el.scrollTop = el.scrollHeight; }, 160);
}

async function loadStatus() {
  try {
    status.value = await api.get<AgentStatus>('/agent/status');
  } catch (err) {
    status.value = null;
    console.warn('[chat] 取问答状态失败', err);
  }
}

async function loadPaper() {
  try {
    const paper = await api.get<{ title?: string; tags?: string | string[] }>(`/papers/${props.paperId}`);
    paperTitle.value = paper.title || '';
    paperTagText.value = Array.isArray(paper.tags) ? paper.tags.join(' ') : String(paper.tags || '');
  } catch (err) {
    paperTitle.value = '';
    paperTagText.value = '';
    console.warn('[chat] 取论文标题失败', err);
  }
  rollCopy();
}

/** 一级页一次取多少条：侧栏是"最近聊过的几个"，不是全部存档 ——
 *  找更早的用搜索（`q` 是**数据库侧**的条件，不受这里限制）。
 *  80 条时那一屏 DOM 已经明显拖慢首屏，翻起来也没人看得完。 */
const LIST_LIMIT = 30;

async function loadConversations() {
  listLoading.value = true;
  try {
    const data = await api.get<{ items: AgentConversation[] }>('/agent/conversations', {
      // **只列这篇论文的对话**（用户口径 2026-10-03）：这里是"读这篇论文时的历史"，
      // 跨论文的检索交给书架与知识库，混在一起反而每一条都要先认"这是哪篇的"。
      paper_id: props.paperId,
      q: convQuery.value.trim() || undefined,
      kind: convKind.value === 'all' ? undefined : convKind.value,
      limit: LIST_LIMIT,
    });
    conversations.value = data.items ?? [];
    picked.value = picked.value.filter(id => conversations.value.some(c => c.conversation_id === id));
  } catch (err) {
    conversations.value = [];
    console.warn('[chat] 取会话列表失败', err);
  } finally {
    listLoading.value = false;
  }
}

const searchLater = debounce(() => void loadConversations(), 260);

async function loadConversation(id: string) {
  loading.value = true;
  try {
    const data = await api.get<AgentConversation>(`/agent/conversations/${id}`);
    currentId.value = data.conversation_id;
    messages.value = (data.messages as AgentMessage[]) ?? [];
    usage.value = data.usage ?? null;
    editing.value = null;
    if (!conversations.value.some(c => c.conversation_id === id)) await loadConversations();
  } catch (err) {
    reportError(err, '读取会话失败');
  } finally {
    loading.value = false;
  }
  /* ⚠️ 滚动必须在 `loading=false` **之后**：加载中列表里只有"读取会话…"那一行，
     这时 `scrollHeight` 约等于 0，滚了也白滚（进来停在顶部就是这么来的）。 */
  await scrollToBottom();
}

async function openConversation(id: string) {
  level.value = 'thread';
  if (id === currentId.value && messages.value.length) {
    await scrollToBottom();      // 同一个会话再点一次：也回到最新那一轮
    return;
  }
  await loadConversation(id);
}

function startNew() {
  level.value = 'thread';
  currentId.value = null;
  messages.value = [];
  usage.value = null;
  errorText.value = '';
  editing.value = null;
  pending.value = [];
  draft.value = '';
  rollCopy();
  void nextTick(() => { resetInputHeight(); inputEl.value?.focus(); });
}

function backToList() {
  level.value = 'list';
  void loadConversations();
}

/* ------------------------------ 一级的操作 ------------------------------ */

function setKind(kind: Kind) {
  convKind.value = kind;
  void loadConversations();
}

function toggleBatch() {
  batch.value = !batch.value;
  picked.value = [];
}

function isPicked(conv: AgentConversation) {
  return picked.value.includes(conv.conversation_id);
}

function togglePick(conv: AgentConversation) {
  picked.value = isPicked(conv)
    ? picked.value.filter(id => id !== conv.conversation_id)
    : [...picked.value, conv.conversation_id];
}

function toggleAll() {
  picked.value = allPicked.value ? [] : conversations.value.map(c => c.conversation_id);
}

function startRename(conv: AgentConversation) {
  renaming.value = { id: conv.conversation_id, text: conv.title || '' };
  void nextTick(() => {
    const el = document.querySelector<HTMLInputElement>('.chat .rename-input');
    el?.focus();
    el?.select();
  });
}

async function submitRename() {
  const item = renaming.value;
  if (!item) return;
  renaming.value = null;
  const title = item.text.trim();
  const old = conversations.value.find(c => c.conversation_id === item.id);
  if (!title || title === (old?.title || '')) return;
  try {
    await api.put(`/agent/conversations/${item.id}`, { title });
    if (old) old.title = title;
    toast('已重命名', 'ok');
  } catch (err) { reportError(err, '重命名失败'); }
}

function askDeleteOne(conv: AgentConversation) {
  confirming.value = {
    ids: [conv.conversation_id],
    body: '删掉这个对话以及它的全部消息，不可恢复。',
    target: conv.title || '未命名对话',
  };
}

function askDeletePicked() {
  if (!picked.value.length) return;
  confirming.value = {
    ids: [...picked.value],
    body: '删掉选中的这些对话以及它们的全部消息，不可恢复。',
    target: picked.value
      .map(id => conversations.value.find(c => c.conversation_id === id)?.title || '未命名对话')
      .slice(0, 3).join('、') + (picked.value.length > 3 ? ` 等 ${picked.value.length} 个` : ''),
  };
}

async function runDelete() {
  const job = confirming.value;
  if (!job) return;
  confirmBusy.value = true;
  try {
    await api.post('/agent/conversations/batch-delete', { conversation_ids: job.ids });
    conversations.value = conversations.value.filter(c => !job.ids.includes(c.conversation_id));
    if (currentId.value && job.ids.includes(currentId.value)) startNew();
    picked.value = [];
    batch.value = false;
    confirming.value = null;
    toast(`已删除 ${job.ids.length} 个对话`, 'ok');
  } catch (err) {
    reportError(err, '删除失败');
  } finally {
    confirmBusy.value = false;
  }
}

/* ------------------------------ 图片附件 ------------------------------ */

function pickFiles() {
  fileInput.value?.click();
}

async function uploadFiles(list: File[] | FileList | null) {
  const files = Array.from(list ?? []).filter(f => f.type.startsWith('image/')
    || /\.(png|jpe?g|webp|gif)$/i.test(f.name || ''));
  if (!files.length) {
    toast('只支持 PNG / JPEG / WebP / GIF 图片', 'warn');
    return;
  }
  uploading.value = true;
  try {
    const form = new FormData();
    for (const file of files) form.append('files', file, file.name || `pasted-${Date.now()}.png`);
    const data = await api.upload<{ items: AgentAttachment[] }>('/agent/attachments', form);
    pending.value = [...pending.value, ...(data.items ?? [])];
  } catch (err) {
    reportError(err, '上传图片失败');
  } finally {
    uploading.value = false;
    if (fileInput.value) fileInput.value.value = '';
  }
}

/** 粘贴图片：**直接贴进输入框**（截图之后 Ctrl+V，最常见的用法）。 */
function onPaste(ev: ClipboardEvent) {
  const items = ev.clipboardData?.items;
  if (!items) return;
  const files: File[] = [];
  for (let i = 0; i < items.length; i += 1) {
    const item = items[i];
    if (item.kind === 'file' && item.type.startsWith('image/')) {
      const file = item.getAsFile();
      if (file) files.push(file);
    }
  }
  if (files.length) {
    ev.preventDefault();
    void uploadFiles(files);
  }
}

function onDrop(ev: DragEvent) {
  dragging.value = false;
  const files = ev.dataTransfer?.files;
  if (files?.length) void uploadFiles(files);
}

function dropPending(index: number) {
  pending.value = pending.value.filter((_, i) => i !== index);
}

/* ------------------------------ 发送 ------------------------------ */

function onEvent(name: string, data: any) {
  if (name === 'session') {
    if (data?.conversation_id) currentId.value = data.conversation_id;
    return;
  }
  if (name === 'stage') { appendLiveStep('stage', data?.message || ''); return; }
  if (name === 'tool') {
    const steps = ensureLive().run!.steps!;
    if (data?.status === 'start') {
      steps.push({ kind: 'tool', round: Number(data?.round || 0), tool: data?.tool ?? '',
                   digest: String(data?.args_digest || ''), status: 'running' });
    } else {
      const open = [...steps].reverse()
        .find(s => s.kind === 'tool' && s.tool === data?.tool && s.status === 'running');
      if (open) { open.status = 'done'; open.ms = Number(data?.ms || 0) || null; }
    }
    return;
  }
  if (name === 'thinking_delta') { appendLiveStep('thinking', data?.text ?? ''); return; }
  if (name === 'thinking') return;   // 思考段的边界由步骤自己的"换类另起一段"表达
  if (name === 'answer_delta') {
    const text = data?.text ?? '';
    const msg = ensureLive();
    msg.content += text;
    appendLiveStep('text', text);
    void scrollToBottom();
    return;
  }
  if (name === 'citation' && data?.ref) {
    const msg = ensureLive();
    if (!(msg.citations ?? []).some(c => c.ref === data.ref)) {
      msg.citations = [...(msg.citations ?? []), data as AgentCitation];
    }
    return;
  }
  if (name === 'done') {
    const msg = ensureLive();
    // `done` 是权威结果：正文、引用、**整条过程轨迹**都换成服务端那一份
    msg.content = data?.answer ?? msg.content;
    msg.citations = data?.citations ?? msg.citations;
    if (data?.run?.steps?.length) msg.run = data.run;
    if (data?.run?.elapsed_ms) elapsedMs.value = Number(data.run.elapsed_ms);
    else if (data?.thinking_ms) elapsedMs.value = Number(data.thinking_ms);
    if (data?.incomplete_reason) toast(`回答未完成：${data.incomplete_reason}`, 'warn');
    // 这一轮改过产物（`page_markdown_edit`）：正文与索引都已更新，给一句提示 ——
    // 否则用户只能自己去翻那一页对不对。阅读器那一页由 SSE 自己刷新（见 ReaderView）。
    const edits = Array.isArray(data?.edits) ? data.edits : [];
    if (edits.length) {
      const pages = [...new Set(edits.map((e: { page?: number }) => Number(e.page)))].filter(Boolean);
      toast(`已修改 ${edits.length} 处产物${pages.length ? `（第 ${pages.join('、')} 页）` : ''}`, 'ok', 3200);
    }
    return;
  }
  if (name === 'error') {
    errorText.value = data?.message || '问答失败';
    return;
  }
}

const TOOL_LABELS: Record<string, string> = {
  paper_search: '正在这篇论文里检索', kb_search: '正在整个知识库里检索',
  page_markdown_get: '正在读正文', block_get: '正在展开这一段',
  page_image_get: '正在看页面图', figure_list: '正在看插图清单',
  figure_image_get: '正在看插图', formula_get: '正在取公式',
  translate_get: '正在取译文', library_list: '正在翻书架', notes_search: '正在查你的笔记',
  page_markdown_edit: '正在修订产物',
};
const toolLabel = (name: string) => TOOL_LABELS[name] ?? `正在调用 ${name}`;

/**
 * 一轮问答。三种入口共用一条流：
 * - `send`：正常提问（可带选区 / 图片）；
 * - `regen`：重新生成某条回答（**默认就地**：替换这条回答；`branch=true` 才挂兄弟）；
 * - `edit`：改掉某条提问（**默认就地**：改写正文并作废它之后的问答；`branch=true` 走兄弟分支）。
 */
async function ask(mode: 'send' | 'regen' | 'edit' = 'send', target?: AgentMessage,
                   branch = false) {
  if (sending.value) return;
  if (!agentReady.value) { toast('问答服务没有在运行（先 `pr.py agent`）', 'warn'); return; }

  const question = mode === 'edit' ? (editing.value?.text ?? '').trim() : draft.value.trim();
  const selection = mode === 'send' ? selected.value : null;
  const attachments = mode === 'send' ? pending.value.map(a => a.attachment_id) : [];
  if (mode === 'edit' && !question) { toast('问题不能为空', 'warn'); return; }
  if (mode === 'send' && !question && !selection?.text && !attachments.length) return;
  if (mode !== 'send') {
    level.value = 'thread';
    if (!currentId.value) return;
  }

  errorText.value = '';
  sending.value = true;
  elapsedMs.value = 0;
  startTimer();
  const optimistic: AgentMessage | null = mode === 'send' ? {
    message_id: `local-${Date.now()}`, conversation_id: currentId.value ?? '', seq: 0,
    role: 'user',
    // 与后端 `api/agent.py` 落库时**同一个格式**（`> 〔第 4 页〕引用…`）：
    // 位置是消息的一部分，刷新前后必须是同一句话。
    content: selection
      ? `> ${selection.where ? `〔${selection.where}〕` : ''}${selection.text.slice(0, 400)}\n\n${question || '解释这段内容'}`
      : question,
    attachments: pending.value,
  } : null;
  if (optimistic) {
    messages.value = [...messages.value, optimistic];
    draft.value = '';
  }
  // 这一轮助手消息的本地占位：**必须排在提问之后**（先建它会挂在用户气泡上面 ——
  // 用户 2026-10-03 报过"思考的抬头跑到我问的问题上面去了"）。
  liveId.value = `local-assistant-${Date.now()}`;
  ensureLive();
  openRuns.value = [...openRuns.value, liveId.value];   // 跑着的那一轮默认展开（可点收起）
  appendLiveStep('stage', mode === 'send' ? '正在准备…'
    : (mode === 'regen' ? '正在重新生成…' : '正在重新提问…'));
  if (mode === 'send') {
    // 发出去之后胶囊就完成使命了（选区已经进了这条消息的 `> 引用`）
    selCtx.value = null;
    emit('clear-selection');
    pending.value = [];
  }
  if (mode === 'edit') editing.value = null;
  await scrollToBottom();

  controller = new AbortController();
  try {
    await postSse('/agent/ask', {
      paper_id: props.paperId,
      question,
      conversation_id: currentId.value,
      selection: selection ? {
        text: selection.text, row_id: selection.row_id ?? null, page: selection.page ?? null,
        where: selection.where || null,        // 位置：落库正文与提示词都要用
      } : null,
      attachments,
      regenerate_from: mode === 'regen' ? target?.message_id : null,
      edit_of: mode === 'edit' ? target?.message_id : null,
      // 默认**就地**改/就地重答；只有那枚 git 分支图标传 true（开兄弟分支，两条都留）
      branch,
    }, { signal: controller.signal, onEvent });
    const convId = currentId.value;
    if (convId) {
      await loadConversation(convId);            // `done` 是权威结果：重拉一次对齐
      await loadConversations();
    }
  } catch (err: any) {
    if (err?.name !== 'AbortError') {
      errorText.value = err?.message ?? String(err);
      reportError(err, '提问失败');
    }
    // 失败时把乐观回显撤掉（后端没落库，留着会与刷新后的结果不一致）
    if (optimistic) messages.value = messages.value.filter(m => m.message_id !== optimistic.message_id);
  } finally {
    sending.value = false;
    stopTimer();
    // 占位消息撤掉：成功后 `loadConversation` 已经把它换成了落库那条；
    // 失败/中止时它没有对应的库记录，留着会与刷新后的结果不一致。
    messages.value = messages.value.filter(m => m.message_id !== liveId.value);
    openRuns.value = openRuns.value.filter(x => x !== liveId.value);
    liveId.value = '';
    controller = null;
    // 发完一轮输入框空了 → 高度收回去（不然它会一直停在上一次的多行高度）
    void nextTick(resetInputHeight);
  }
}

/** 跑表：每 200ms 一跳（读数只到秒，再密只是白烧 CPU）。 */
function startTimer() {
  stopTimer();
  const t0 = performance.now();
  tickTimer = window.setInterval(() => {
    elapsedMs.value = Math.round(performance.now() - t0);
  }, 200);
}

function stopTimer() {
  if (tickTimer !== null) { window.clearInterval(tickTimer); tickTimer = null; }
}

function stop() {
  controller?.abort();
  toast('已停止', 'warn');
}

/** 编辑提问：`branch=false` 就地改写；`branch=true` 是"用新问法开新分支"（原条保留）。 */
function startEdit(message: AgentMessage, branch = false) {
  const { rest } = splitQuote(message.content);
  editing.value = { id: message.message_id, text: rest, branch };
  level.value = 'thread';
}

/** **开新分支**（消息操作条上那枚 git 分支图标）：
 *  - 提问：打开编辑框、按"新分支"发（新问法与原提问并列，原条保留）；
 *  - 回答：直接按"新分支"重答一份（与原回答并列，◀ 1/2 ▶ 可切）。 */
function branchFrom(message: AgentMessage) {
  if (message.role === 'user') startEdit(message, true);
  else void ask('regen', message, true);
}

/** 重新生成：默认**就地重答**（替换这条回答）；`branch=true` 走兄弟分支。 */
function regenerate(target: AgentMessage, branch = false) {
  void ask('regen', target, branch);
}

async function copyMessage(message: AgentMessage) {
  const { rest } = splitQuote(message.content);
  try {
    await navigator.clipboard.writeText(rest);
    toast('已复制', 'ok');
  } catch { toast('复制失败（浏览器没给剪贴板权限）', 'warn'); }
}

/** 分叉导航：在同一父节点的兄弟之间切换（◀ 2/3 ▶）。 */
async function switchBranch(message: AgentMessage, step: number) {
  const group = message.siblings ?? [];
  if (group.length < 2 || !currentId.value || sending.value) return;
  const index = (message.sibling_index ?? 1) - 1;
  const next = ((index + step) % group.length + group.length) % group.length;
  try {
    const data = await api.post<AgentConversation>(
      `/agent/conversations/${currentId.value}/branch`, { message_id: group[next] });
    messages.value = (data.messages as AgentMessage[]) ?? [];
    usage.value = data.usage ?? null;
    await scrollToBottom();
  } catch (err) { reportError(err, '切换分支失败'); }
}

/**
 * textarea 自动增高：先归零再按 `scrollHeight` 顶上去，到上限就不再长（框内滚）。
 * **必须要这一步**：`rows=1` 的 textarea 不会自己长高，多行内容会挤在一条里滚。
 */
const INPUT_MAX_H = 160;

function autoGrow() {
  const el = inputEl.value;
  if (!el) return;
  el.style.height = 'auto';
  el.style.height = `${Math.min(el.scrollHeight, INPUT_MAX_H)}px`;
}

function resetInputHeight() {
  const el = inputEl.value;
  if (el) el.style.height = '';
}

/** 模型名那枚小按钮：跳到设置的「论文问答」卡片（不在这里做模型下拉 —— 会多一套状态）。 */
function openModelSettings() {
  void router.push(openTab({ name: 'settings', hash: '#agent' }) as never);
}

/** 点圆环 = 压缩（多一轮会话：模型把已谈过的内容压成要点，存成一条 summary）。
 *  **要确认**（用户口径）：压缩会改变模型之后看得到的上下文，属于不可逆的语义操作，
 *  点一下就跑掉太轻率；确认框走全站那套站内弹窗（`askConfirm`）。 */
async function compress() {
  if (!canCompress.value || !currentId.value) return;
  const ok = await askConfirm({
    title: '压缩这段对话？',
    body: '把已经谈过的内容压成一份要点摘要（会话里会多出一轮「已压缩」）。'
      + '原文不会被删、往上翻还在；但之后模型只看得到这份摘要和后面的几轮。',
    confirmLabel: '压缩',
  });
  if (!ok) return;
  compressing.value = true;
  try {
    await api.post(`/agent/conversations/${currentId.value}/compress`);
    await loadConversation(currentId.value);
    await loadConversations();
    toast('已压缩：前面的内容变成了一份要点摘要', 'ok');
  } catch (err) {
    reportError(err, '压缩失败');
  } finally {
    compressing.value = false;
  }
}

function gotoCitation(citation: AgentCitation) {
  if (citation.paper_id && citation.paper_id !== props.paperId) {
    toast(`这条引用来自《${citation.label || citation.paper_id}》，不在当前论文里`, 'warn');
    return;
  }
  emit('focus', citation.row_id || citation.block_id || '', citation.page ?? undefined);
}

onMounted(() => {
  /* ⚠️ 会话列表**不能**排在状态请求后面（用户报的"点对话页签，历史要等一下才出来"）。
     以前是 `await Promise.all([loadStatus(), loadPaper()])` 之后再 `await loadConversations()`，
     而 `/agent/status` 里带一次"问答服务在不在"的健康检查 —— 服务没起来时要等它超时
     （实测 2s 左右），于是历史被硬生生拖了两秒。三件事互不依赖，各走各的。 */
  void loadConversations();
  void Promise.all([loadStatus(), loadPaper()]);
});

/* 从别的页签切回来：实例被 `KeepAlive` 缓存着，列表**已经在屏幕上**，
   这里只是后台悄悄刷一次（有数据时不显示"读取会话…"，不会闪）。 */
onActivated(() => {
  void loadConversations();
  void loadStatus();
});

onUnmounted(() => { controller?.abort(); composerRO?.disconnect(); });

watch(() => props.paperId, async () => {
  controller?.abort();
  level.value = 'list';
  messages.value = [];
  currentId.value = null;
  pending.value = [];
  selCtx.value = null;
  void Promise.all([loadPaper(), loadConversations()]);
  void loadStatus();
});

// 划词带进来的选区：直接进二级页（列表页没有输入框，留在那儿用户会以为没反应），
// 并在输入框里挂一枚**上下文胶囊**（是否随这轮发出去由用户点它决定）。
// 监听**对象本身**而不是文本：同一段文字连点两次也要能切过去。
watch(() => props.selection, (sel) => {
  selCtx.value = sel?.text
    ? { text: sel.text, row_id: sel.row_id ?? null, page: sel.page ?? null,
        where: sel.where ?? '', include: true }
    : null;
  if (sel?.text) {
    level.value = 'thread';
    void nextTick(() => inputEl.value?.focus());
  }
});

/* 对外的一个口子：**把图片塞进附件列表**（右键菜单「添加到对话框（支持图片）」，用户口径 2026-10-08）。
   走的是与"选文件 / 拖进来 / 粘贴"同一条上传链路（`uploadFiles`），所以大小限制、格式校验都一致。 */
defineExpose({ uploadFiles });
</script>

<template>
  <div class="chat" @dragover.prevent="dragging = true" @dragleave="dragging = false"
       @drop.prevent="onDrop">
    <!-- ═══════════════ 一级：会话列表 ═══════════════ -->
    <template v-if="level === 'list'">
      <div class="list-head">
        <!-- 顶部工具栏**只有这一个动作**（用户口径 2026-10-03）：轻量的小胶囊，
             不做成主题色大圆钮 —— 那玩意儿在极简的学术界面里太抢眼。 -->
        <button class="btn sm new-conv" title="新建对话" @click="startNew">
          <AppIcon name="add" :size="14" />新对话
        </button>
        <label class="search-field grow">
          <AppIcon name="search" :size="14" />
          <input v-model="convQuery" placeholder="搜索对话"
                 @input="searchLater" @keydown.esc="convQuery = ''; loadConversations()" />
        </label>
        <button v-if="convQuery" class="btn ghost icon sm" title="清空" @click="convQuery = ''; loadConversations()">
          <AppIcon name="close" :size="12" />
        </button>
        <!-- 列表这一级的「加号」在左边，所以拆分按钮放在同一行最右（同一枚图标、同一个动作）。
             图标是**斜对角双箭头**（拆出去看 = 两个箭头朝外），与收回那枚互为镜像。 -->
        <button
          v-if="showFloatBtn"
          class="btn ghost icon sm"
          title="浮层打开（把这栏拎成浮窗，正文回到整宽）"
          @click="emit('float')"
        >
          <AppIcon name="popout" :size="14" />
        </button>
      </div>

      <div class="filters">
        <div class="seg">
          <button v-for="k in KINDS" :key="k.key" :class="{ active: convKind === k.key }"
                  @click="setKind(k.key)">{{ k.label }}</button>
        </div>
        <span class="spacer" />
        <div class="seg">
          <button :class="{ active: batch }" title="批量管理（多选 + 批量删除）" @click="toggleBatch">
            <AppIcon name="checkbox" :size="13" />管理
          </button>
        </div>
      </div>

      <div class="list-body">
        <div v-if="listLoading && !conversations.length" class="empty small">读取会话…</div>
        <div v-else-if="!conversations.length" class="hollow">
          <span class="hollow-ico"><AppIcon name="chat" :size="18" /></span>
          <div class="small">{{ convQuery ? '没有匹配的对话' : '还没有对话' }}</div>
          <div class="tiny faint">
            {{ convQuery ? '换个词试试；标题和消息正文都会被搜。'
              : '点右上角 ＋ 新建一个，或在正文里划词提问。' }}
          </div>
        </div>

        <div v-for="c in conversations" :key="c.conversation_id" class="row"
             :class="{ on: c.conversation_id === currentId, pick: isPicked(c) }">
          <button v-if="batch" class="tick" :class="{ on: isPicked(c) }"
                  :title="isPicked(c) ? '取消选择' : '选择'"
                  @click="togglePick(c)">
            <AppIcon :name="isPicked(c) ? 'checkbox' : 'checkbox-off'" :size="15" />
          </button>

          <input v-if="renaming?.id === c.conversation_id" v-model="renaming.text" class="rename-input"
                 @keydown.enter="submitRename" @keydown.esc="renaming = null" @blur="submitRename" />

          <button v-else class="row-main" @click="batch ? togglePick(c) : openConversation(c.conversation_id)">
            <!-- 标题按**行内 markdown** 渲染：标题就是用户问的第一句，里面常有 $…$ 公式 -->
            <span class="row-title truncate" v-html="inlineHtml(c.title || '未命名对话')" />
            <span class="row-meta">
              <span v-if="c.kind === 'selection'" class="kd" title="划词对话"><AppIcon name="quote" :size="10" />划词</span>
              <!-- 相对时间 + 精确时间 tooltip：相对时间会让人怀疑"是不是时区错了"，
                   悬停给出本地绝对时间（与搜索历史面板同一个口径） -->
              <span class="tiny faint" :title="fmtDate(c.updated_at)">{{ convCount(c) }} 条 · {{ fmtRelative(c.updated_at) }}</span>
            </span>
          </button>

          <div v-if="!batch" class="row-acts">
            <button class="ico" title="重命名" @click.stop="startRename(c)"><AppIcon name="edit" :size="12" /></button>
            <button class="ico danger" title="删除" @click.stop="askDeleteOne(c)"><AppIcon name="trash" :size="12" /></button>
          </div>
        </div>
      </div>

      <div v-if="batch" class="batch-bar">
        <div class="seg">
          <button :class="{ active: allPicked }" @click="toggleAll">{{ allPicked ? '取消全选' : '全选' }}</button>
        </div>
        <span class="tiny faint">已选 {{ picked.length }}</span>
        <span class="spacer" />
        <button class="btn sm danger" :disabled="!picked.length" @click="askDeletePicked">删除</button>
      </div>
    </template>

    <!-- ═══════════════ 二级：会话 ═══════════════ -->
    <template v-else>
      <div class="thread-head">
        <button class="th-act" title="返回对话列表" @click="backToList"><AppIcon name="back" :size="14" /></button>
        <span class="th-title truncate" :title="current?.title || '新对话'" v-html="inlineHtml(currentTitle)" />
        <span class="spacer" />
        <!-- 右上角这组按钮：**与页面其它工具同一门语言** —— 无描边的托底托盘 + 里面两枚圆按钮
             （用户口径 2026-11-19：符合页面风格）。以前是两枚散着的裸图标，看着像没做完。 -->
        <span class="th-acts">
          <!-- 拆分（浮层打开）：**加号左边**；图标用"分享/对外打开"那枚方框加箭头，与加号一眼区分 -->
          <button
            v-if="showFloatBtn"
            class="th-act"
            title="浮层打开（把这栏拎成浮窗，正文回到整宽）"
            @click="emit('float')"
          >
            <AppIcon name="popout" :size="14" />
          </button>
          <!-- 会话页顶部：标题已经写着会话名了，再造一个「+ 新对话」文字钮就是重复 -->
          <button class="th-act" title="新建对话" @click="startNew">
            <AppIcon name="add" :size="14" />
          </button>
        </span>
      </div>

      <div ref="msgsEl" class="msgs">
        <div v-if="loading" class="empty small">读取会话…</div>

        <div v-else-if="!messages.length && !sending" class="hello">
          <!-- 空态**只有一句语录当背景**（用户口径 2026-10-03：不要上传/换一条这类按钮）。
               那枚标记从"双引号水印"换成**裁好的应用图标**（用户口径 2026-10-07 第四轮：
               这个位置本来就是一枚居中图标，居中的语录在它下面）。 -->
          <span class="hello-mark" data-hello-mark><img :src="BRAND_ICON" alt="" /></span>
          <p class="hello-line">{{ headline }}</p>
        </div>

        <div v-for="m in messages" :key="m.message_id" class="msg" :class="m.role"
             :data-live="m.message_id === liveId ? '1' : null">
          <!-- 压缩留痕：对话里多出来的一轮 -->
          <div v-if="m.role === 'summary'" class="sum-card">
            <div class="sum-head"><AppIcon name="layers" :size="12" />已压缩 · 前面的内容在这里</div>
            <div class="md small" v-html="render(m.content)" />
          </div>

          <template v-else>
            <div v-if="m.role === 'user' && splitQuote(m.content).quote" class="sel-quote">
              <AppIcon name="quote" :size="11" />
              <span class="truncate">{{ splitQuote(m.content).quote }}</span>
            </div>

            <!-- 过程轨迹：**思考 / 正文 / 工具按发生顺序混排**（正常 agent 就长这样）。
                 正文段直接就是回答本体（`run.steps` 里有 text 段时不再另渲染 `content`，
                 否则同一段话会出现两遍）；没有 `run` 的老消息由 `stepsOf` 兜底拼一份。 -->
            <div v-if="stepsOf(m).length" class="run" :class="{ live: m.message_id === liveId }">
              <button class="run-head" @click="toggleRun(m.message_id)">
                <AppIcon :name="isRunOpen(m.message_id) ? 'chevron-down' : 'chevron-right'" :size="12" />
                <span v-if="m.message_id === liveId" class="dot" />
                <span class="run-title">{{ runTitle(m) }}</span>
                <span class="run-sum truncate">{{ runSummary(stepsOf(m)) }}</span>
              </button>
              <div v-if="isRunOpen(m.message_id)" class="run-body">
                <template v-for="(s, i) in stepsOf(m)" :key="`${m.message_id}-${i}`">
                  <!-- 过程里说的半句（调工具之前那句）：留在轨迹里，最终回答在外面 -->
                  <div v-if="s.kind === 'text' && showInTrace(m, s)" class="md run-text"
                       v-html="renderAnswer(s.text || '', m.citations)" @click="onCitationClick" />
                  <template v-else-if="s.kind === 'text'" /><!-- 最后一段正文＝回答本体，渲染在轨迹外 -->
                  <!-- 思考段：默认只露 4 行，点一下展开（长思考不占满整屏） -->
                  <div v-else-if="s.kind === 'thinking'" class="run-step think"
                       :class="{ open: isStepOpen(m.message_id, i) }"
                       :title="isStepOpen(m.message_id, i) ? '收起这段思考' : '展开这段思考'"
                       @click="toggleStep(m.message_id, i)">
                    <AppIcon class="step-caret"
                             :name="isStepOpen(m.message_id, i) ? 'chevron-down' : 'chevron-right'"
                             :size="11" />
                    <AppIcon class="step-ico" name="sparkle" :size="11" />
                    <span class="step-tag">思考</span>
                    <span class="step-text">{{ s.text }}</span>
                  </div>
                  <!-- 宿主自己的动作（准备档案 / 检索 / 开始组织回答）：一行淡字，不是工具调用 -->
                  <div v-else-if="s.kind === 'stage'" class="run-step stage">
                    <span class="step-text">{{ s.text }}</span>
                  </div>
                  <!-- 工具段：短语 + 参数摘要（短语来自 TOOL_STEPS，与"正在…"那套同源） -->
                  <div v-else class="run-step tool" :class="{ running: s.status === 'running' }">
                    <AppIcon :name="toolIcon(s.tool)" :size="11" />
                    <span class="step-tag">{{ stepLabel(s.tool) }}</span>
                    <span v-if="fmtDigest(s.digest)" class="step-arg">· {{ fmtDigest(s.digest) }}</span>
                    <span v-if="s.ms" class="step-ms">{{ fmtMs(s.ms) }}</span>
                  </div>
                </template>
              </div>
            </div>

            <div class="md body"
                 v-html="renderAnswer(m.role === 'user' ? splitQuote(m.content).rest : finalAnswer(m), m.citations)"
                 @click="onCitationClick" />

            <div v-if="m.attachments?.length" class="atts">
              <a v-for="a in m.attachments" :key="a.attachment_id" class="att"
                 :href="imageSrc(a.url)" target="_blank" rel="noopener" :title="`${a.name} · ${fmtSize(a.bytes)}`">
                <img :src="imageSrc(a.url)" :alt="a.name" loading="lazy" />
              </a>
            </div>

            <!-- 引用**不再在下面列一排 chunk 号**（用户口径 2026-10-03）：
                 正文里的 `[n]` 本身就是锚点，点它跳转。 -->

            <!-- 消息操作条：**在气泡下面**（用户口径 2026-10-03 —— 上面的那排按钮全部移除）。
                 语义分三档：改写这条（就地）/ 重答这条（就地）/ **开新分支**（那枚 git 分支图标）。 -->
            <div class="msg-tools" :class="{ show: (m.siblings?.length ?? 0) > 1 }">
              <template v-if="(m.siblings?.length ?? 0) > 1">
                <button class="ico" title="上一个版本" @click="switchBranch(m, -1)"><AppIcon name="back" :size="11" /></button>
                <span class="tiny faint mono">{{ m.sibling_index }}/{{ m.siblings?.length }}</span>
                <button class="ico" title="下一个版本" @click="switchBranch(m, 1)"><AppIcon name="forward" :size="11" /></button>
                <span class="tool-sep" />
              </template>
              <button class="ico" title="复制" @click="copyMessage(m)"><AppIcon name="copy" :size="12" /></button>
              <button v-if="m.role === 'user'" class="ico" title="编辑这条提问（就地改，会替换它之后的回答）"
                      :disabled="sending" @click="startEdit(m, false)"><AppIcon name="edit" :size="12" /></button>
              <button v-if="m.role === 'assistant'" class="ico" title="重新生成（就地重答，替换这条回答）"
                      :disabled="sending" @click="regenerate(m, false)"><AppIcon name="refresh" :size="12" /></button>
              <button class="ico" :title="m.role === 'user'
                        ? '用新问法开新分支（原来这条保留）'
                        : '再答一份，开新分支（原来这条保留）'"
                      :disabled="sending" @click="branchFrom(m)">
                <AppIcon name="git-branch" :size="12" />
              </button>
            </div>
          </template>
        </div>

        <!-- 正在生成那一轮就是上面 v-for 里的**本地占位消息**（`liveId`）：
             思考/正文/工具夹杂的渲染因此与落库后完全同一套模板，不再有第二套"流式专用"样式。
             流式期间**不列 chunk 号**（用户口径 2026-10-03）：边写边多出来的编号只会刷屏。 -->

        <div v-if="errorText" class="err small">{{ errorText }}</div>
      </div>

      <!-- 输入区 = **一块浮在消息上的圆角容器**：上面是 textarea，下面是操作条。
           容器自己承担"一块输入组件"的观感，里面的元素不再各自成块；`ref` 给样式里那个
           ResizeObserver 量高度用（消息区要按它留出底部空白，见 `.composer`）。 -->
      <div ref="composerEl" class="composer">
        <div class="composer-box" :class="{ busy: uploading }">
          <!-- 划词带进来的选区：贴在输入框上沿的**上下文胶囊**（用户口径 2026-10-03，仿 VS Code）。
               整块是一枚按钮 → 点它切换"这轮要不要带上"；右侧 ✕ 才是移除。
               关掉时仍然显示（看得见自己选过什么）但压灰 + 划线，一眼能看出不会发出去。 -->
          <div v-if="selCtx && !editing" class="sel-chip" :class="{ off: !selCtx.include }">
            <button class="sel-chip-main" @click="toggleSelection"
                    :title="selCtx.include
                      ? '这段选中内容会随这条问题一起发出去（点一下改成不带）'
                      : '这段选中内容不会发出去（点一下改成带上）'">
              <AppIcon name="quote" :size="11" />
              <!-- 位置摆前面（"第 4 页 · Markdown"）：光有原文，用户分不清自己选的是哪一段 -->
              <span v-if="selCtx.where" class="sel-chip-where">{{ selCtx.where }}</span>
              <span class="truncate">{{ selCtx.text }}</span>
              <AppIcon :name="selCtx.include ? 'checkbox' : 'checkbox-off'" :size="12" />
            </button>
            <button class="ico" title="移除选区" @click="dropSelection">
              <AppIcon name="close" :size="11" />
            </button>
          </div>

          <div v-if="pending.length" class="atts pending">
            <div v-for="(a, i) in pending" :key="a.attachment_id" class="att">
              <img :src="imageSrc(a.url)" :alt="a.name" />
              <button class="att-x" title="移除" @click="dropPending(i)"><AppIcon name="close" :size="10" /></button>
            </div>
            <span class="tiny faint">{{ pending.length }} 张</span>
          </div>

          <div v-if="editing" class="edit-box">
            <div class="edit-head tiny faint">
              <AppIcon :name="editing.branch ? 'git-branch' : 'edit'" :size="11" />
              {{ editing.branch
                ? '用新问法开新分支：新提问与原来这条并列，原来的保留'
                : '改这条提问：会替换掉它之后的回答（不可恢复）' }}
            </div>
            <textarea v-model="editing.text" class="composer-input" rows="2" />
            <div class="edit-acts">
              <span class="spacer" />
              <button class="btn sm" @click="editing = null">取消</button>
              <button class="btn sm primary" :disabled="sending || !editing.text.trim()"
                      @click="ask('edit', messages.find(x => x.message_id === editing?.id), editing.branch)">
                {{ editing.branch ? '开新分支' : '保存并重答' }}
              </button>
            </div>
          </div>

          <template v-else>
            <!-- 紧凑起步（一行高），多行自动向上长；到上限后在**框内**滚 -->
            <textarea ref="inputEl" v-model="draft" class="composer-input" rows="1" placeholder="与Cyli深度交流"
                      @input="autoGrow" @paste="onPaste" @keydown.enter.exact.prevent="ask('send')" />

            <div class="composer-bar">
              <!-- 加号：低调（幽灵态，hover 才浮出来），不做主题色实心圆钮 -->
              <button class="btn ghost icon sm attach" :class="{ busy: uploading }"
                      title="上传图片（也可以直接粘贴，张数不限）" @click="pickFiles">
                <AppIcon name="add" :size="15" />
              </button>
              <span class="spacer" />
              <button v-if="sending" class="btn icon sm stop send" title="停止" @click="stop"><AppIcon name="stop" :size="14" /></button>
              <button v-else class="btn primary icon sm send" :disabled="!canSend" title="发送" @click="ask('send')">
                <AppIcon name="arrow-up" :size="15" />
              </button>
            </div>
          </template>
        </div>

        <!-- 模型与上下文圆环在**输入框外**（用户口径 2026-10-03）：一行低调的明细，
             不跟输入抢位置；圆环很小、里面不放数字，数值写在 tooltip 里。 -->
        <div v-if="!editing" class="composer-foot">
          <button class="model-btn truncate" :title="`当前问答模型：${modelName}（点一下去设置里换）`"
                  @click="openModelSettings">
            <span class="truncate">{{ modelName }}</span><AppIcon name="chevron-right" :size="11" />
          </button>
          <span v-if="usage?.compressed" class="badge dim tiny">已压缩</span>
          <span class="spacer" />
          <button class="ring" :class="{ busy: compressing }" :title="ringTitle"
                  :disabled="!canCompress" @click="compress">
            <svg viewBox="0 0 36 36" class="ring-svg">
              <circle class="ring-track" cx="18" cy="18" r="15.5" />
              <circle class="ring-bar" cx="18" cy="18" r="15.5" :stroke-dasharray="ringDash" />
            </svg>
          </button>
        </div>
      </div>

      <input ref="fileInput" class="hidden-file" type="file" accept="image/png,image/jpeg,image/webp,image/gif"
             multiple @change="uploadFiles(($event.target as HTMLInputElement).files)" />
  </template>

    <div v-if="dragging" class="drop-hint"><AppIcon name="image" :size="16" />松手即上传</div>

    <ConfirmModal :open="Boolean(confirming)" danger title="删除对话"
                  :body="confirming?.body || ''" :target="confirming?.target || ''"
                  confirm-label="删除" :busy="confirmBusy"
                  @confirm="runDelete" @cancel="confirming = null" />
  </div>
</template>

<style scoped>
/* ⚠️ 这一块**不自己造胶囊**（2026-10-03 统一，用户口径："有些有描边、有些缺了中间那枚白色圆角
   底片，以论文译文对照那枚胶囊为准"）：能用的全用全局件 ——
     .search-field  搜索框（白底 + 描边）
     .seg / .seg button(.active)  分段控件（浅底描边托盘 + 抬起的白色 pill）
     .btn / .btn.sm / .btn.icon / .btn.primary / .btn.on  按钮
   本地只留消息气泡、思考块正文、圆环这些**没有全局对应物**的东西。 */
/* 自己撑满侧栏剩下的高度（用户口径 2026-10-07：底部原来被裁掉约一个页签头）。
   ⚠️ 不能写 `height: 100%`：`.reader-aside` 是**纵向 flex**（上面还有一条 58px 的页签条），
   百分比算的就是"整个侧栏的高度" → 面板底边落到侧栏外面，被 `overflow: hidden` 裁掉。
   同一栏里 `ReaderSidePanel` 的 `.panel-body`（`flex: 1; min-height: 0`）从来没这个毛病。 */
.chat { position: relative; display: flex; flex-direction: column; flex: 1; min-height: 0; }
.spacer { flex: 1; }
.grow { flex: 1; min-width: 0; }
/* 行内小图标按钮（消息 hover 出来的复制 / 重新生成 / 编辑、批量勾选）：
   它们是**贴着内容的小动作**，不是胶囊，所以不进 .btn 那一档，尺寸对齐 24px。 */
.ico {
  display: inline-flex; align-items: center; justify-content: center; border: 0;
  background: none; color: var(--text-dim); width: 24px; height: 24px; padding: 0;
  border-radius: var(--radius-ctl); cursor: pointer; flex: none;
}
.ico:hover { background: var(--bg-hover); color: var(--text); }
.ico.danger:hover { background: var(--danger-soft); color: var(--danger); }
.ico:disabled { opacity: .4; cursor: default; }
.hidden-file { display: none; }

/* ── 一级：列表 ── */
.list-head { display: flex; align-items: center; gap: 6px; padding: 8px 8px 6px; }
/* 搜索框用全局 .search-field：这里只把它撑满侧栏（全局是给工具栏定的固定宽度） */
.list-head .search-field { width: 100%; }
.list-head .search-field input { width: 100%; min-width: 0; }

/* 一行放不下就换行（侧栏最窄时只有 300px，硬挤会把最右边那枚挤没） */
.filters { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; padding: 0 8px 8px; }

.list-body { flex: 1; min-height: 0; overflow-y: auto; padding: 0 6px 8px; }
.row {
  display: flex; align-items: center; gap: 2px; border-radius: var(--radius);
  padding: 1px 2px; margin-bottom: 2px; border: 1px solid transparent;
}
.row:hover { background: var(--bg-hover); }
.row.on { background: var(--accent-soft); border-color: color-mix(in srgb, var(--accent) 30%, transparent); }
.row.pick { background: var(--accent-soft); }
.row-main {
  flex: 1; min-width: 0; display: flex; flex-direction: column; align-items: flex-start;
  gap: 2px; border: 0; background: none; color: var(--text); cursor: pointer;
  text-align: left; padding: 7px 6px; font: inherit;
}
.row-title { font-size: 12.5px; font-weight: 500; max-width: 100%; }
.row-meta { display: flex; align-items: center; gap: 5px; max-width: 100%; }
.kd {
  display: inline-flex; align-items: center; gap: 2px; font-size: 10px; padding: 0 5px;
  border-radius: var(--radius-ctl); background: var(--accent-soft); color: var(--accent-text);
}
.row-acts { display: flex; gap: 1px; opacity: 0; }
.row:hover .row-acts { opacity: 1; }
.tick {
  width: 24px; height: 24px; flex: none; border: 0; background: none; cursor: pointer;
  color: var(--text-faint); display: inline-flex; align-items: center; justify-content: center;
  border-radius: var(--radius-ctl);
}
.tick.on { color: var(--accent); }
.rename-input {
  flex: 1; min-width: 0; border: 1px solid var(--accent); border-radius: var(--radius-ctl);
  background: var(--bg-elev); color: var(--text); font: inherit; font-size: 13px;
  height: var(--control-h-sm); padding: 0 11px; outline: none;
}
.hollow { display: flex; flex-direction: column; align-items: center; gap: 6px; padding: 34px 18px; text-align: center; }
.hollow-ico {
  display: flex; align-items: center; justify-content: center; width: 38px; height: 38px;
  border-radius: var(--radius-ctl); background: var(--accent-soft); color: var(--accent-text);
}
.batch-bar {
  display: flex; align-items: center; gap: 8px; padding: 7px 10px;
  border-top: 1px solid var(--divider); background: var(--bg-layer);
}

/* ── 二级：会话 ── */
.thread-head {
  display: flex; align-items: center; gap: 3px; padding: 5px 8px 5px 6px;
  border-bottom: 1px solid var(--divider);
}
/* 顶部按钮：26px 圆钮 + 托盘（`--bg-sunken`），与侧栏页签胶囊、搜索那两枚小胶囊同一门语言。
   悬停时那枚按钮才"浮起来"（`--bg-elev` + 一档阴影），与 `.seg` 的当前项同一个做法。 */
.th-act {
  width: 26px; height: 26px; flex: none; display: inline-flex; align-items: center; justify-content: center;
  border: 0; border-radius: 999px; background: transparent; color: var(--text-dim); cursor: pointer;
  transition: background var(--dur) var(--ease), color var(--dur) var(--ease), box-shadow var(--dur) var(--ease);
}
.th-act:hover { background: var(--bg-elev); color: var(--text); box-shadow: var(--shadow-1); }
.th-act:active { transform: scale(.94); }
.th-acts {
  display: inline-flex; align-items: center; gap: 2px; flex: none; padding: 2px;
  border-radius: 999px; background: var(--bg-sunken);
}
.th-title { font-size: 13px; font-weight: 600; color: var(--text); max-width: 52%; }

.msgs {
  /* 底部这块空白是给**悬浮的输入区**让位的（JS 按实测高度覆盖，这里只是兜底） */
  flex: 1; min-height: 0; overflow-y: auto; padding: 10px 10px 112px; overflow-wrap: anywhere;
  /* 列向 flex：空态那一块才能"占满整块高度、把图标 + 语录**竖直居中**"（用户口径 2026-10-07）。
     消息自己还是从上往下排（块级子项在列向 flex 里本来就是撑满宽度、顺序堆叠）。 */
  display: flex; flex-direction: column;
}
.msg { margin-bottom: 12px; }
.msg.assistant .body { font-size: 12.5px; line-height: 1.72; color: var(--text); }
.msg.user { display: flex; flex-direction: column; align-items: flex-end; }
.msg.user .body {
  background: var(--bg-layer); border: 1px solid var(--border); border-radius: var(--radius);
  padding: 6px 9px; font-size: 12.5px; line-height: 1.65; max-width: 92%;
}
.msg.user .sel-quote, .msg.user .atts { align-self: flex-end; }
/* 消息操作条：**在气泡下面**（用户口径 2026-10-03），hover 才显形；
   用户消息靠右对齐，跟气泡同侧。 */
.msg-tools { display: flex; align-items: center; gap: 2px; min-height: 20px; opacity: 0; margin-top: 2px; }
.msg.user .msg-tools { justify-content: flex-end; }
.msg:hover .msg-tools, .msg-tools.show { opacity: 1; }
.tool-sep { width: 1px; height: 12px; margin: 0 3px; background: var(--divider); }
.sel-quote {
  display: flex; align-items: center; gap: 4px; max-width: 92%; margin-bottom: 3px;
  padding: 3px 7px; border-left: 2px solid var(--accent);
  background: var(--bg-layer); border-radius: 0 var(--radius-sm) var(--radius-sm) 0;
  font-size: 11.5px; color: var(--text-dim);
}
.sum-card {
  border: 1px dashed var(--border-strong); border-radius: var(--radius);
  background: var(--bg-layer); padding: 7px 9px;
}
.sum-head {
  display: flex; align-items: center; gap: 4px; margin-bottom: 4px;
  font-size: 11px; color: var(--text-faint); letter-spacing: .02em;
}
.sum-card .md :deep(p) { margin: 3px 0; }
/* 正文里的引用锚：`[n]` 就在句子里，点它直接跳（用户口径 2026-10-03）。
   样式已**搬到 `styles/base.css` 的 `.cite-inline`**（用户口径 2026-10-07：正文里的引用标号
   要跟这里长得一样 → 两处共用一份，别再在这里维护第二份）。 */
@keyframes pulse { 0%, 100% { opacity: .35 } 50% { opacity: 1 } }

/* 过程轨迹：抬头一行（用时 + 干了什么），正文里**思考 / 正文 / 工具按发生顺序混排**。
   抬头不是胶囊按钮而是一行文字 —— 它每轮都在，做成白胶囊会把整屏切成一条条横带。
   正文段（`.run-text`）与轨迹外的正文同款排版；思考段用等宽灰字（草稿不是答案）。 */
.run { margin: 2px 0 6px; }
.run-head {
  display: flex; align-items: center; gap: 5px; width: 100%; text-align: left;
  padding: 2px 0; border: 0; background: none; cursor: pointer;
  color: var(--text-dim); font-size: 11.5px;
}
.run-head:hover { color: var(--text); }
.run-title { font-weight: 600; white-space: nowrap; }
.run-sum { color: var(--text-faint); margin-left: 2px; min-width: 0; }
.run .dot {
  width: 6px; height: 6px; border-radius: 50%; background: var(--accent); flex: none;
  position: relative;
  animation: pulse 1.1s ease-in-out infinite;
}
/* ── "这一轮还在跑"的强调（用户口径 2026-10-15："再明显一点，但不要动输入框"）──
   四个信号**只作用在正在跑的那一条**（`.run.live` / `.msg[data-live]`），跑完立刻回静态：
     ① 轨迹抬头那行字走一道扫光（"活着"的观感）  ② 小圆点往外扩一圈涟漪
     ③ 左侧竖轨呼吸（accent 与 divider 之间来回） ④ 正在写的那一段末尾跟一枚方块光标
   动效可被系统级 `prefers-reduced-motion` 关掉（见下面同名媒体查询）。 */
.run .dot::after {
  content: ""; position: absolute; inset: 0; border-radius: 50%;
  box-shadow: 0 0 0 0 color-mix(in srgb, var(--accent) 60%, transparent);
  animation: live-ring 1.8s var(--ease) infinite;
}
@keyframes live-ring { 70%, 100% { box-shadow: 0 0 0 7px transparent; } }
@keyframes live-rail {
  0%, 100% { border-left-color: color-mix(in srgb, var(--accent) 34%, var(--border)); }
  50% { border-left-color: color-mix(in srgb, var(--accent) 92%, var(--border)); }
}
.run.live .run-body { animation: live-rail 2.4s ease-in-out infinite; }
@keyframes live-sheen { from { background-position: 100% 0; } to { background-position: -100% 0; } }
/* 扫光靠"背景裁到字里"。放进 `@supports`：不支持的浏览器会看见一条彩条压在文字上，
   那不如干脆不启用（标题保持原来的灰字）。 */
@supports (-webkit-background-clip: text) or (background-clip: text) {
  .run.live .run-title {
    background: linear-gradient(90deg,
      var(--text-dim) 0%, var(--text-dim) 36%,
      var(--accent) 47%, var(--accent-hover) 53%,
      var(--text-dim) 64%, var(--text-dim) 100%);
    background-size: 200% 100%;
    -webkit-background-clip: text; background-clip: text;
    -webkit-text-fill-color: transparent; color: transparent;
    animation: live-sheen 2.1s linear infinite;
  }
}
/* 光标跟在**这一轮最后落下的那段话**后面（正文段或轨迹里的半句都算）。 */
@keyframes caret { 50% { opacity: 0; } }
.msg[data-live="1"] .body :deep(> *:last-child)::after,
.msg[data-live="1"] .run-text :deep(> *:last-child)::after {
  content: ""; display: inline-block; width: 6px; height: 12px; margin-left: 3px;
  vertical-align: -2px; border-radius: 1.5px; background: var(--accent);
  animation: caret 1.1s step-end infinite;
}
/* 纯装饰：系统里关掉动效就都不动。**不动的强调还在**（主题色的点、主题色的停止钮）。 */
@media (prefers-reduced-motion: reduce) {
  .run.live .run-body, .run .dot::after,
  .msg[data-live="1"] .body :deep(> *:last-child)::after,
  .msg[data-live="1"] .run-text :deep(> *:last-child)::after { animation: none; }
  .run.live .run-title { animation: none; background-position: 50% 0; }
}
.run-body {
  display: flex; flex-direction: column; gap: 3px;
  margin: 4px 0 0 5px; padding-left: 9px;
  border-left: 2px solid var(--border);
}
.run-step {
  /* `flex-start`：图标/标签钉在**第一行**上（用 baseline/center 时，多行思考的图标会飘到
     整段中间，看着不像"图标与文字并排"，用户 2026-10-03 报过） */
  display: flex; align-items: flex-start; gap: 5px;
  font-size: 11.5px; line-height: 1.65; color: var(--text-faint);
}
.run-step :deep(svg), .run-step svg { flex: none; margin-top: 2px; }
.run-step .step-tag { color: var(--text-dim); flex: none; }
.run-step .step-arg { color: var(--text-faint); overflow-wrap: anywhere; }
.run-step .step-ms { margin-left: auto; padding-left: 8px; flex: none; font-family: var(--mono); font-size: 10.5px; }
.run-step.tool.running .step-tag { color: var(--accent-text); }
/* 阶段提示（准备档案 / 检索 / 组织回答）：比步骤再淡一档，它只是过程的背景 */
.run-step.stage .step-text { color: var(--text-faint); font-style: italic; }
/* 思考段：默认只露 4 行（一轮思考可能上万字），点一下展开全文。
   最前面那枚箭头就是"能展开"的提示（与轨迹抬头同一套箭头语汇）。 */
.run-step.think { cursor: pointer; }
.run-step.think:hover .step-tag { color: var(--accent-text); }
.run-step.think .step-caret { color: var(--text-faint); }
.run-step.think .step-text {
  font-family: var(--mono); white-space: pre-wrap; overflow-wrap: anywhere;
  display: -webkit-box; -webkit-line-clamp: 4; -webkit-box-orient: vertical; overflow: hidden;
}
.run-step.think.open .step-text { display: block; -webkit-line-clamp: unset; }
.run-text { margin: 2px 0; }
.run-text :deep(p) { margin: 5px 0; }
.err { color: var(--danger); line-height: 1.6; }
.msg .md :deep(p) { margin: 5px 0; }
.msg .md :deep(ul), .msg .md :deep(ol) { margin: 5px 0; padding-left: 18px; }

/* 空态**只是一句语录当背景**：没有按钮、没有可点的东西（用户口径 2026-10-03）。
   图标 + 语录这一组**在整块高度里竖直居中**（`flex: 1` + `justify-content: center`，用户口径 2026-10-07），
   语录淡到像背景板，别让人以为那是可点的建议条。 */
.hello {
  flex: 1; display: flex; flex-direction: column; align-items: center; justify-content: center;
  gap: 12px; padding: 24px 22px 40px; text-align: center;
}
.hello-mark { color: var(--border-strong); line-height: 1; }
/* 空态标记 = 应用图标（用户口径 2026-10-07：取代双引号水印；尺寸再放大一档） */
.hello-mark img { display: block; width: 64px; height: 64px; opacity: .92; }
.hello-line {
  margin: 0; max-width: 100%; color: var(--text-faint);
  font-size: 13px; font-weight: 500; line-height: 1.7;
  user-select: text;
}

/* 这一块留给"接着可以问"：试过用模型生成，但那一轮与主对话的缓存前缀毫无重叠，
   等于每轮多打一次冷启动 API，而接着聊本来就能解决 → 整块拿掉了。 */

.atts { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 5px; }
.atts.pending { margin: 0 0 6px; align-items: center; }
.att {
  position: relative; width: 46px; height: 46px; border-radius: var(--radius-sm);
  overflow: hidden; border: 1px solid var(--border); background: var(--bg-sunken); display: block;
}
.att img { width: 100%; height: 100%; object-fit: cover; display: block; }
.att-x {
  position: absolute; top: 1px; right: 1px; width: 15px; height: 15px; border: 0; cursor: pointer;
  border-radius: 50%; background: rgba(0, 0, 0, .55); color: #fff;
  display: inline-flex; align-items: center; justify-content: center; padding: 0;
}

/* ── 输入区：**一块浮在对话上的圆角容器**（用户口径 2026-10-15 第二次）──
   原来 `.composer` 顶上有一条 `border-top`，把面板齐刷刷切成"上面正文 / 下面输入"两半：
   正文滚到底正好被那条直线**裁断**（用户报的"直线裁切"）。
   现在整块浮起来 —— 没有分界线，输入框自己是圆角矩形 + 投影；底下那层渐隐只是让从框下
   穿过的正文淡出去，不跟框里的字打架。
   ⚠️ 浮起来就不再占布局高度 → 消息区的空白由 JS 按实测高度补（`composerEl` 那个
   ResizeObserver），`.msgs` 的 `padding-bottom` 只是没跑起来时的兜底值。 */
.composer {
  position: absolute; left: 0; right: 0; bottom: 0; z-index: 3;
  padding: 10px 10px 8px;
  background: linear-gradient(to top,
    var(--bg-elev) 0%, var(--bg-elev) 58%,
    color-mix(in srgb, var(--bg-elev) 68%, transparent) 84%, transparent 100%);
}
.composer-box {
  border: 1px solid var(--border-strong); border-radius: var(--radius);
  background: var(--bg-elev); padding: 6px 6px 4px;
  box-shadow: var(--shadow-2);
  transition: border-color var(--dur) var(--ease), box-shadow var(--dur) var(--ease);
}
/* 轻度焦点反馈：焦点在框内任意位置都亮一圈（用户口径："获得焦点时有轻微视觉反馈"） */
.composer-box:focus-within { border-color: var(--accent); box-shadow: var(--shadow-2), var(--ring); }
.composer-box.busy { opacity: .75; }
/* 上下文胶囊（划词选区）：整块可点 → 切换"要不要带上"。
   用 accent-soft 的**实心胶囊**（不是左边一条竖线）：它是"我能点"的按钮，不是一段引文装饰。
   `off` 态保留在框里但压成托盘色 + 文字划线 —— 用户看得见自己选过什么，也看得见它不会发出去。 */
.sel-chip {
  display: flex; align-items: center; gap: 1px; margin: 0 0 5px;
  background: var(--accent-soft); border-radius: var(--radius-ctl); padding: 1px 2px 1px 1px;
}
.sel-chip-main {
  display: flex; align-items: center; gap: 5px; flex: 1; min-width: 0;
  border: 0; background: none; cursor: pointer; padding: 3px 6px; text-align: left;
  border-radius: var(--radius-sm); color: var(--accent-text);
  font: inherit; font-size: 11.5px;
  transition: background var(--dur) var(--ease), color var(--dur) var(--ease);
}
.sel-chip-main:hover { background: color-mix(in srgb, var(--accent) 15%, transparent); }
.sel-chip-main .truncate { flex: 1; min-width: 0; }
/* 位置那一小段（"第 4 页 · Markdown"）：比正文小一号、后面一条细竖线隔开，
   不抢原文的位置，但一眼能看出这段是从哪儿来的。 */
.sel-chip-where {
  flex: none; font-size: 10.5px; letter-spacing: .01em; opacity: .85;
  padding-right: 5px; margin-right: 1px;
  border-right: 1px solid color-mix(in srgb, currentColor 30%, transparent);
}
.sel-chip-main svg:last-child { flex: none; opacity: .75; }
.sel-chip.off { background: var(--bg-sunken); }
.sel-chip.off .sel-chip-main { color: var(--text-faint); }
.sel-chip.off .sel-chip-main .truncate { text-decoration: line-through; }
/* 输入框本身**没有边框、也没有自己的焦点环**：边界与焦点反馈都由外层容器给。
   ⚠️ 必须显式压掉 `:focus`：base.css 里 `textarea:focus { box-shadow: var(--ring) }` 是元素级
   规则，不写这一条就会在容器里再出现一个圆角光圈 —— 看起来就是"框里套了个框"（用户报的
   "输入框问题"）。 */
.composer-input,
.composer-input:focus,
.composer-input:hover {
  display: block; width: 100%; resize: none; overflow-y: auto;
  min-height: 26px; max-height: 160px;
  border: 0; outline: none; box-shadow: none; background: none;
  color: var(--text); font-family: inherit; font-size: 13px; line-height: 1.6; padding: 4px 7px;
  box-sizing: border-box;
}
.composer-input::placeholder { color: var(--text-faint); }
.edit-box { display: flex; flex-direction: column; gap: 5px; padding: 2px; }
.edit-head { display: inline-flex; align-items: center; gap: 4px; }
.edit-acts { display: flex; gap: 5px; padding-bottom: 2px; }
.composer-bar { display: flex; align-items: center; gap: 4px; margin-top: 4px; }
/* 输入框**外面**那一行：模型 + 圆环（用户口径：这两个不放进输入框里） */
.composer-foot { display: flex; align-items: center; gap: 6px; padding: 6px 4px 0; }
/* 模型名：**低调**（幽灵小字 + 一枚箭头），点它去设置里换，不在这一层做下拉 */
.model-btn {
  display: inline-flex; align-items: center; gap: 1px; max-width: 55%;
  border: 0; background: none; cursor: pointer; padding: 2px 4px;
  border-radius: var(--radius-ctl); color: var(--text-faint); font: inherit; font-size: 11.5px;
}
.model-btn:hover { background: var(--bg-hover); color: var(--text-dim); }
/* 小圆环：**里面不放数字**（用户口径），数值在 tooltip 里；16px 也不适合写字 */
.ring {
  position: relative; width: 16px; height: 16px; flex: none;
  border: 0; background: none; padding: 0; cursor: pointer; color: var(--accent);
}
.ring:disabled { cursor: default; color: var(--text-faint); }
.ring.busy .ring-bar { animation: spin 1s linear infinite; transform-origin: 18px 18px; }
@keyframes spin { to { transform: rotate(360deg) } }
.ring-svg { width: 100%; height: 100%; transform: rotate(-90deg); display: block; }
.ring-track { fill: none; stroke: var(--divider); stroke-width: 6; }
.ring-bar { fill: none; stroke: currentColor; stroke-width: 6; stroke-linecap: round; transition: stroke-dasharray .3s ease; }
/* 停止 = **主题色**的实心圆钮（用户口径 2026-10-15：原来红/蓝两色像两套系统，
   现在跟发送那枚 `.btn.primary` 同一份配方，只有图标从箭头换成方块）。位置尺寸都没动。 */
.btn.stop {
  background: var(--accent); border-color: transparent; color: var(--text-on-accent);
  box-shadow: 0 1px 2px color-mix(in srgb, var(--accent) 28%, transparent);
}
.btn.stop:hover:not(:disabled) {
  background: var(--accent-hover);
  box-shadow: 0 6px 14px -6px color-mix(in srgb, var(--accent) 60%, transparent);
}

.drop-hint {
  position: absolute; inset: 6px; z-index: 5; display: flex; align-items: center; justify-content: center;
  gap: 6px; border: 1.5px dashed var(--accent); border-radius: var(--radius);
  background: color-mix(in srgb, var(--accent-soft) 88%, transparent);
  color: var(--accent-text); font-size: 12.5px; pointer-events: none;
}
</style>
