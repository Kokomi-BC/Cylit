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
import { flyMessage } from '@/composables/useComposerFlight';
import { api, assetUrl } from '@/api/client';
import { postSse } from '@/api/stream';
import { withNoteRefs } from '@/utils/noteRefs';
import { renderedLength } from '@/utils/latexText';
import { mdToHtml, inlineHtml } from '@/composables/useMarkdown';
import { reportError, toast } from '@/stores/app';
import { askConfirm } from '@/stores/confirm';
import { debounce, fmtDate, fmtRelative, truncate } from '@/utils/format';
import { HL_SWATCHES, paletteNameOf, swatchLabel, swatchStyleOf } from '@/utils/highlightColors';
import { BRAND_ICON } from '@/utils/brand';
import { BUILTIN_EFFORTS } from '@/utils/reasoning';
import { convCount } from '@/api/types';
import type {
  AgentAttachment, AgentCitation, AgentConversation, AgentMessage, AgentModelPick,
  AgentSelection, AgentStatus, AgentStep, AgentUsage,
  ChatErrorKind, ChatErrorView,
  Highlight, ModelConfig, Note,
} from '@/api/types';
import AppIcon from '@/components/AppIcon.vue';
import { vendorColorIcon } from '@/assets/model-icons';
import ConfirmModal from '@/components/ConfirmModal.vue';
import ContextMenu from '@/components/ContextMenu.vue';
import ChatErrorCard from '@/components/reader/ChatErrorCard.vue';

/** 一条引用在阅读器里的落点（正文那枚按钮的 `data-pool` 里的一项）。整条消息的引用
 *  **全量**塞进按钮，链的判定才能落到阅读器那边（那里才有 DOM 与行数据）。
 *  与 `ReaderView` 里那份结构逐字相同 —— 本轮不动 `api/types.ts`，所以两边各写一次；
 *  TS 是结构类型，两边对得上就够（事件参数不靠同一个名字的对象身份）。 */
type CitePoolItem = { ref: number; row: string; block: string; page: number; latex: string };

const props = defineProps<{
  paperId: string;
  /** 阅读器**这一刻**的状态（页号 / 对照方式 / 左右顺序 / 视图 / 左栏页签），只随提问发给后端。
   *  空对象或没给 = 没有状态可报 —— 请求体里**不出现** `reader` 这个键（老请求逐字节不变）。 */
  readerState?: {
    page?: number;
    page_side?: string;
    order?: string;
    view?: string;
    side_tab?: string;
    visible_pages?: number[];
  } | null;
  /** 从正文划词带过来的选区（可空）；发出去之后由父组件清掉 */
  selection?: AgentSelection | null;
  /** 能不能「浮层打开」（窄屏是抽屉，拎成浮窗没有意义 —— 由父组件判） */
  canFloat?: boolean;
  /** 已经在浮窗里了：浮窗标题栏自带「回到侧栏」，这里不再重复一枚拆分按钮 */
  floating?: boolean;
}>();

const emit = defineEmits<{
  /** 第三个参数是这条引用的**完整 LaTeX**（公式引用才有）：父组件据此把落点从整行收进那一枚 KaTeX */
  (e: 'focus', blockId: string, page?: number, latex?: string): void;
  /** 合并引用（正文里的一枚按钮带着好几处）：一次把它们**全**闪起来。
   *  为什么不循环 emit 单个 focus：那是「滚过去 → 闪 → 再滚下一处」，镜头会来回拽；
   *  而合并组本来就是同一处相邻的几个编号，用户要看的是它们**同时**亮。
   *
   *  只给「被点的编号 + 这条消息的**整个引用池**」：**链改在阅读器那边算**
   *  （`ReaderView.focusMany` 里的 `buildCiteChain`）—— 那里才有 DOM 与行数据，
   *  才认得出"整行就是一坨公式"的那种行。渲染正文时算不了这一条：那时候手里只有引用
   *  自己的字段，看不见那一行里到底画了什么（用户口径：13 与 14 闪成了同一块地方，
   *  因为 13 落在的那一行整行就是一条显示公式）。 */
  (e: 'focus-many', p: { refs: number[]; pool: CitePoolItem[] }): void;
  /** 悬浮正文里的引用标号：请求在旁边 peek 一眼（点标号照旧跳转，不受影响） */
  (e: 'peek', p: {
    anchor: { x: number; y: number; w: number; h: number };
    page?: number | null;
    rowId?: string | null;
    blockId?: string | null;
    label?: string | null;
    snippet?: string | null;
    /** 这条引用覆盖的**整段行**（逗号分隔，按论文顺序）：速览卡据此画多行，
     *  与点击时「整条相邻链一起闪」是同一份范围。单枚标号时就是它自己那一行。
     *  ⚠️ 带 `pool` 之后这里一律给 null：**链在阅读器那边算**（与点击同一枚纯函数）。
     *  两处各算一遍迟早算出两个答案，"正文闪了三行、卡片只给一行"就是这么来的。 */
    rows?: string | null;
    /** 这一枚按钮代表的编号（方括号组里可能有好几个） */
    refs?: number[] | null;
    /** 整条消息的引用池：阅读器据此按「同页 + 行号差 1」扩成链 */
    pool?: CitePoolItem[] | null;
  }): void;
  (e: 'peek-hide'): void;
  (e: 'clear-selection'): void;
  /** 这一轮 agent 动了侧栏里的东西（笔记 / 高亮 / 批注）：父组件据此就地刷新，不整页重载 */
  /** 带**工具名**：父组件据此判断要不要连公式 / 表格一起重取（修订产物会改它们的来源） */
  (e: 'refresh-side', tool?: string): void;
  /** 拆分 / 浮层打开：把整个侧栏面板拎成浮窗（真正的外框由 `ReaderView` 负责） */
  (e: 'float'): void;
  /** 点图片：交给阅读器那套**图片查看器**（用户口径 2026-11-19，不再开新标签页） */
  (e: 'zoom', target: { url: string; caption?: string | null }): void;
}>();

/* 拆分按钮：会话列表与会话两级都摆在**工具栏最右**（用户口径 2026-11-19：放在加号旁边），
   所以两级共用这一个判断，免得漏一处。 */
const showFloatBtn = computed(() => !!props.canFloat && !props.floating);

const RING_CIRCUMFERENCE = 2 * Math.PI * 15.5;
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
                     where: string; include: boolean;
                     /** 这一轮是「解释」：请求要打到后端的 `mode: "explain"` */
                     explain?: boolean;
                     /** 锚点快照：回答下面那排色点用它建高亮、把回答存成笔记 */
                     start?: number; end?: number; side?: string; scope?: string;
                     /** 高亮要存的原文（原 PDF 划词时与 `text` 不是同一个字符串） */
                     quote?: string } | null>(null);
/** 这一轮是不是「解释」：后端据此套用设置里「Cyli 解释」那份模型 + 思考强度。
 *  为什么单独存一份而不是读 `selCtx`：图片解释那一轮没有文字选区，胶囊是空的。 */
const explainIntent = ref(false);
/**
 * 最近一轮**带选区**的提问留下的锚点快照（text/row_id/page/start/end/side/scope）。
 *
 * 为什么快照：读到回答再点颜色时，输入框里的胶囊已经发出去清掉了、正文上的选区也可能被改掉，
 * 那时再去取必然错位（与原来解释浮层"打开那一刻存快照"同一个理由）。`highlightId` 建完回填，
 * 再点别的颜色就是**改这条高亮的颜色**而不是又建一条。
 */
const lastAnchor = ref<{ text: string; quote: string; row_id: string | null; page: number | null;
                         where: string; start: number; end: number; side: string; scope: string;
                         highlightId: string; color: string } | null>(null);
const uploading = ref(false);
const loading = ref(false);
const compressing = ref(false);
const sending = ref(false);
/**
 * 本次失败的原因（`null` = 没有失败）。
 *
 * 为什么不再是一行字符串：一句 `message` 装不下"谁失败了" —— 本地限额 / 上游 429 / 上游欠费 /
 * 鉴权 / 参数 / 5xx / 超时 / 断网 / 流被截断在界面上长得一模一样，用户唯一能做的动作就是再点一次。
 * 现在整份对象（`kind` + HTTP 状态 + 上游错误码 + trace_id + 限额快照）都留着，交给 `ChatErrorCard`。
 */
const chatError = ref<ChatErrorView | null>(null);

/** 只认真字符串：空串 / 缺键一律当"没给"（卡片据此**整行不渲染**，绝不写占位假值） */
function errText(v: unknown): string | null {
  return typeof v === 'string' && v.trim() ? v.trim() : null;
}
function errNum(v: unknown): number | null {
  return typeof v === 'number' && Number.isFinite(v) ? v : null;
}

/**
 * 流内终态 `error` 帧 → 卡片视图。
 *
 * **字段直读，一个都不推断**：分类（`kind`）是后端给的，前端再算一遍必然出现两套口径
 * ——那就等于又要把"限额"说成"故障"。前端唯一的兜底是 `kind` 缺失（老后端 / 半条帧）：落 `unknown`。
 */
function errorViewFromFrame(data: any): ChatErrorView {
  const d = (data && typeof data === 'object' ? data : {}) as Record<string, any>;
  const rawLimit = d.limit ?? d.detail?.limit ?? null;
  return {
    code: errText(d.code) ?? '',
    kind: (errText(d.kind) as ChatErrorKind) ?? 'unknown',
    message: errText(d.message) ?? '问答失败',
    http_status: errNum(d.http_status),
    provider_code: errText(d.provider_code),
    provider_type: errText(d.provider_type),
    provider_message: errText(d.provider_message),
    retryable: d.retryable === true,
    retry_after: errNum(d.retry_after),
    model: errText(d.model) ?? '',
    provider_label: errText(d.provider_label) ?? '',
    trace_id: errText(d.trace_id) ?? undefined,
    limit: rawLimit && typeof rawLimit === 'object' ? rawLimit : null,
  };
}

/**
 * `postSse` 抛出来的 `ApiError` → 卡片视图。两种来路：
 *
 * 1. 后端已经给了 `detail.provider`（HTTP 错误信封，与流内 `error` 帧**同一份对象**）→ 直接用，
 *    前端不碰；
 * 2. 没有 `provider`（本地限额 429、请求没发出去的 `NETWORK_ERROR`、流读到一半的
 *    `STREAM_INTERRUPTED`、以及裸 HTTP 码）→ 在**这里唯一地**按 `api/types.ts` 里那份 K 列表映射一次。
 */
function errorViewFromApiError(err: any): ChatErrorView {
  const detail = err?.detail && typeof err.detail === 'object' ? err.detail : null;
  const provider = detail?.provider && typeof detail.provider === 'object' ? detail.provider : null;
  if (provider) {
    const view = errorViewFromFrame(provider);
    const limit = detail?.limit ?? view.limit ?? null;
    return { ...view, trace_id: errText(err?.traceId) ?? view.trace_id, limit };
  }
  const code = errText(err?.code) ?? '';
  // ApiError 的默认 status 是 0（= 没有 HTTP 响应），不能当状态码显示
  const status = errNum(err?.status);
  const httpStatus = status !== null && status > 0 ? status : null;
  const kind: ChatErrorKind =
    code === 'LIMIT_EXCEEDED' ? 'limit'
      : code === 'MODEL_NOT_CONFIGURED' ? 'not_configured'
        : code === 'NETWORK_ERROR' ? 'network'
          : code === 'STREAM_INTERRUPTED' ? 'stream'
            : httpStatus === 429 ? 'rate_limit'
              : httpStatus === 402 ? 'quota'
                : httpStatus === 401 || httpStatus === 403 ? 'auth'
                  : httpStatus === 400 || httpStatus === 404 || httpStatus === 422 ? 'bad_request'
                    : httpStatus !== null && httpStatus >= 500 ? 'server'
                      : 'unknown';
  return {
    code,
    kind,
    message: errText(err?.message) ?? '问答失败',
    /* 只有真·上游状态才写上去：本地限额的 429 是我们自己回的，流被截断时那个 200 是"响应头到了"
       而不是错误码 —— 两者摆在卡片上都会把人指向错的地方。 */
    http_status: kind === 'limit' || kind === 'stream' ? null : httpStatus,
    provider_code: null,
    provider_type: null,
    provider_message: null,
    retryable: !(kind === 'limit' || kind === 'auth' || kind === 'bad_request'
      || kind === 'not_configured' || kind === 'unknown'),
    retry_after: null,
    model: '',
    provider_label: '',
    trace_id: errText(err?.traceId) ?? undefined,
    limit: detail?.limit && typeof detail.limit === 'object' ? detail.limit : null,
  };
}
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
/** 用户**显式点过**哪几轮轨迹的展开/收起。只活在组件内存里（不落库、不进 localStorage）：
    一轮跑完时，点过的沿用他的选择，没点过的才吃"结束后自动收起"这个默认。 */
const runTouched = new Set<string>();
/** 展开长思考的键（`${message_id}:${步骤序号}`）；默认只露 4 行 */
const openSteps = ref<string[]>([]);
/** 用户**手动收起**过的段（键同 `openSteps`）：最后一段默认展开，手点过就以手点为准。 */
const closedSteps = ref<string[]>([]);
/** **已经写完**的思考段（键同 `openSteps`）。
 *  后端在正文首字到来前会推一条 `thinking`（`status: done`）—— 收到它就把这段从
 *  "自动展开的最后一段"里摘掉（用户口径 2026-12-08：「思考完成时立即折叠思维链」）。
 *  不等下一个步骤出现再折：清洗器扣着尾巴时，"思考写完 → 正文首字"之间能有几百毫秒，
 *  那段时间思维链还摊在那儿，看着就是"想完了也不收"。 */
const endedSteps = ref<string[]>([]);
/** 展开后的实测高度（px）：`max-height: none` 不参与过渡，钉成实测值才能展开/收起两头都平滑、又不裁字。 */
const thinkMax = ref<Record<string, number>>({});
const editing = ref<{ id: string; text: string; branch: boolean } | null>(null);
const headline = ref('');
const dragging = ref(false);
const confirming = ref<{ ids: string[]; body: string; target: string } | null>(null);
const confirmBusy = ref(false);
const msgsEl = ref<HTMLElement | null>(null);
const chatEl = ref<HTMLElement | null>(null);
/** 「贴着底部 / 跟随」：流式输出时决定要不要把视口拉着走。
 *  **只有这一份状态**（以前 `atBottom` 与 `follow` 各一半：圆钮只恢复其一、滚动事件又写另一个，
 *  两份互相打架 —— 这正是"点了回到最新也追不上"的一半来源）。圆钮的显隐直接由它反推。
 *  只有**用户的意图**能改它：往上翻 / 上翻键 = 退出；滚回底部、点圆钮、发消息、切会话 = 恢复。 */
const BOTTOM_GAP = 56;
/** 「没得往上翻」的容差（px）。取 2 而不是 1：触控板 / 触摸的惯性会在贴底那一帧回一点点
 *  （亚像素），卡在 1px 上会把这一次回弹读成「读者上翻」—— 于是「回到最新」那枚圆钮
 *  闪一帧就没了（用户报的「滚到底部那一下会闪一帧」）。 */
const USER_UP_SLOP = 2;
const follow = ref(true);

/** 刚进消息流的那一两条：只给它们播进场动画。
 *  消息是整份数组替换的（`messages.value = [...]`），没有 TransitionGroup 可用；
 *  不标记的话，切会话时历史消息会一起"长出来"一遍。 */
const freshIds = ref<Set<string>>(new Set());
function markFresh(...ids: string[]): void {
  const next = new Set(freshIds.value);
  ids.forEach((id) => next.add(id));
  freshIds.value = next;
  window.setTimeout(() => {
    const after = new Set(freshIds.value);
    ids.forEach((id) => after.delete(id));
    freshIds.value = after;
  }, 600);
}

/** 流式正文的**合帧**（用户口径 2026-12-03：流式输出感觉卡卡的）。
 *
 *  以前每来一条 delta 就 msg.content += text 一次，紧接着 scrollToBottom() 一次 ——
 *  那个函数是个**三帧 rAF 循环**，每帧读 scrollHeight、写 scrollTop。一条回答几千个 delta，
 *  等于把整段 markdown 重排几百遍 + 几百次强制布局。现在：delta 先进缓冲，最多每 66ms
 *  落一次盘（正文按块渲染以后，一批只重画"最后一块"，比原来整段重排便宜得多，所以这里
 *  可以比原来的 90ms 更密一点，字浮现得更连续）；滚动只在落盘那一帧做**一次赋值**，
 *  而且只在跟随状态下做。 */
const DELTA_FLUSH_MS = 66;
let deltaBuf = '';
let deltaTarget: { content: string } | null = null;
let deltaTimer = 0;

/** 程序自己刚滚到的位置 —— 见 `onMsgsScroll` 里"别把自己的滚动当成上翻"。
 *  以前用一个 600ms 的时间窗，两个方向都误伤：窗口内用户真上翻被吞掉（想翻翻不动），
 *  窗口外程序滚动的中间帧又被当成上翻（跟随就此断掉）。现在改成**认落点**：
 *  scrollTop 停在（或翻过）我们刚设的那个值，就算"这滚动是我自己干的"。
 *  ⚠️ 认出一次**不清零**（以前清成 -1）：同一批增量连着来两条 scroll 事件时，
 *  第二条就会被判成"读者上翻"，跟随当场断掉、「回到最新」圆钮闪一帧。 */
let programTop = -1;
/** 读者**自己**上翻过（滚轮向上 / 手指往下拖）：这是一次明确意图，不许滚动事件按"离底多远"
 *  立刻撤回 —— 先退出再恢复那一下正是圆钮闪一帧的来源（用户报的「滚到底部闪一帧」）。 */
let userPulledUp = false;

function stickToBottom() {
  const el = msgsEl.value;
  if (!el) return;
  /* 落点取「可滚到的最大值」，不是 scrollHeight（后者比最大值大一个 clientHeight）：
     每次赋值都让浏览器做一次"越界 → 夹紧"，落点与意图之间就多了一次回弹的机会。
     已经在底上就一个字都不写 —— 给 scrollTop 赋值会打断浏览器正在跑的惯性滚动
     （触摸 / 触控板松手后的余量被清掉），收尾那一帧会回弹 = 「滚到底闪一下」。
     贴底过程中位移只允许单调往前，不 overshoot、也不反复推同一个值。 */
  const top = Math.max(0, el.scrollHeight - el.clientHeight);
  if (Math.abs(top - el.scrollTop) >= 1) el.scrollTop = top;
  programTop = el.scrollTop;          // 读回被浏览器夹紧之后的真值
}

/** 跟随状态下"把视口拉到底"，但**等这一帧的 DOM 落完**再拉。
 *  增量落盘与 DOM 更新不在同一个任务里：直接滚读到的是还没渲染的旧 scrollHeight，
 *  于是每一批增量都落后一截（越写越远，只能反复点「回到最新」）。
 *  同一帧里多次请求只滚一次（增量、KaTeX 后渲染、折叠块会连着触发好几拨）。 */
let stickRaf = 0;
function followBottom() {
  if (!follow.value || stickRaf) return;
  stickRaf = requestAnimationFrame(() => {
    stickRaf = 0;
    if (follow.value) stickToBottom();
  });
}

function flushDelta() {
  if (deltaTimer) { clearTimeout(deltaTimer); deltaTimer = 0; }
  const target = deltaTarget;
  const buf = deltaBuf;
  deltaBuf = '';
  deltaTarget = null;
  if (!target || !buf) return;
  target.content += buf;
  appendLiveStep('text', buf);
  /* DSH 的正文是"边写边一个字一个字浮现"：每批增量落盘后，把**这一帧新长出来的部分**做一次
     淡入（它自己的 `reveal-piece`：opacity .2 → 1、ease-out）。已经显示过的不重播。 */
  pendingReveal += buf.length;
  void nextTick(() => revealFreshBlocks());
  followBottom();                     // 下一帧（DOM 落完之后）再贴底，见 followBottom
}

/** 已播过载入动画的块数（按当前这条在写的消息计）。 */
let revealedBlocks = 0;
/** 这一批还没落盘的字符数（给"同一块里继续长"的那一截做淡入）。 */
let pendingReveal = 0;

/**
 * 正文载入动画：给**这一批新出现的块**整块淡入；如果这批只是把**最后一块**写长了，
 * 就把新长出来的那一截包一层（同样淡入）。
 *
 * 正文按块渲染（见 `splitAnswerParts`）以后，这里只需要伺候"最后那一块" —— 前面
 * 已封口的块连 DOM 都没动过，不用管它们会不会重播动画。
 */
function revealFreshBlocks(): void {
  const msg = msgsEl.value?.querySelector('.msg[data-live="1"]') as HTMLElement | null;
  const parts = msg ? Array.from(msg.querySelectorAll<HTMLElement>(':scope > .md-part')) : [];
  if (!parts.length) { revealedBlocks = 0; pendingReveal = 0; return; }
  if (parts.length < revealedBlocks) revealedBlocks = 0;   // 换了内容（新消息 / 重置）
  const last = parts.length - 1;
  if (parts.length > revealedBlocks) {
    for (let i = revealedBlocks; i <= last; i += 1) {
      // `display: contents` 的块自己没有盒子，opacity 得挂在它的内容上
      for (const kid of Array.from(parts[i].children)) kid.classList.add('reveal-piece');
    }
  } else {
    wrapTail(parts[last], pendingReveal);
  }
  revealedBlocks = parts.length;
  pendingReveal = 0;
}

/** 把最后一块里**新长出来的那一截**包成一层淡入（只动纯文本尾巴，不碰公式 / 代码 / 引用锚）。 */
function wrapTail(part: HTMLElement, chars: number): void {
  let node: Text | null = null;
  const walker = document.createTreeWalker(part, NodeFilter.SHOW_TEXT);
  for (let n = walker.nextNode(); n; n = walker.nextNode()) {
    if ((n.textContent ?? '').trim()) node = n as Text;
  }
  if (!node) return;
  const host = node.parentElement;
  if (!host || host.closest('.katex, pre, code')) return;
  const text = node.textContent ?? '';
  const take = Math.max(1, Math.min(chars || 1, text.length));
  const cut = text.length - take;
  const span = document.createElement('span');
  span.className = 'reveal-piece';
  span.textContent = text.slice(cut);
  node.textContent = text.slice(0, cut);
  host.insertBefore(span, node.nextSibling);
}

/** 换了在写的消息 / 新一轮开始：计数清零（下一块的载入动画才播）。 */
function resetReveal(): void {
  revealedBlocks = 0;
  pendingReveal = 0;
}

function pushDelta(target: { content: string }, text: string) {
  if (!text) return;
  deltaTarget = target;
  deltaBuf += text;
  if (!deltaTimer) deltaTimer = window.setTimeout(flushDelta, DELTA_FLUSH_MS);
}

/** 丢掉还没落盘的尾巴（reset / done 会用服务端那份整体替换） */
function dropDelta() {
  if (deltaTimer) { clearTimeout(deltaTimer); deltaTimer = 0; }
  deltaBuf = '';
  deltaTarget = null;
}

/** 回到最新：**恢复跟随**并滚到底（那枚圆形按钮）。
 *  用户口径 2026-12-06：点了它之后要像真正的 AI 对话那样**一直贴着底部输出** ——
 *  所以这里恢复的是 `follow` 本身（圆钮的显隐也由它反推），不是"只滚这一次"。 */
async function backToLatest() {
  follow.value = true;
  await scrollToBottom();
}
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
    /* 「回到最新」那枚圆钮要浮在输入区**上方**：高度交给 CSS 变量，别写死。 */
    chatEl.value?.style.setProperty('--composer-h', `${el.offsetHeight}px`);
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
/* ---------------------- 模型 / 推理等级（输入框下面那枚胶囊） ----------------------
   候选来自 `/models?model_type=agent`；**一条 agent 行都没有**时退回 `/agent/status`
   那一份，只读显示 —— 那种情况下没有"可换"的东西，列表里也就没有可点的项。 */
const modelOptions = ref<ModelConfig[]>([]);
/** 根浮层开没开：**只有「模型 / 推理等级」两行**，候选全部搬到侧向子菜单里 */
const menuOpen = ref(false);
/** 第二级：哪一行正在飞出候选；`null` = 只开着根浮层 */
const subStage = ref<'model' | 'effort' | null>(null);
/** 子菜单的视口坐标 —— 它是 `position: fixed`，由被点那行的 rect 算出来 */
const subPos = ref({ left: 0, top: 0 });
/**
 * 子菜单的层级：**跟着对话卡自己的 z 走**，不写死档位。
 *
 * 卡片 z 来自 ReaderView 那条共用序列（`nextLayerZ`：从 60 起、**每聚焦一次 +1、没有上限**），
 * 所以写死 90 只是"迟早会被超过去" —— 用户 2026-12-08 的截图正是：推理等级子菜单被对话浮窗
 * 压住（那一次阅读里点开过三十多次浮层，`chatZ` 早就越过 90）。+2 才压得住卡片本身与它的
 * 两枚角把手（那两枚是 `chatZ + 1`）。
 *
 * 为什么不用 prop 从 ReaderView 收上来：子菜单 **Teleport 到 body**，继承不到卡片的层级；
 * 打开那一刻直接量 `closest('.chat-host')` 的计算 z 最省事，也不怕层序之后又变。
 */
const subZ = ref(92);
const subEl = ref<HTMLElement | null>(null);
/** 子菜单宽度 / 高度上限（夹视口用；宽 200 是这里定死的） */
const SUB_W = 200;
const SUB_H = 250;
/** 这条会话单独选的值（空串 = 跟随默认，后端存 NULL） */
const pick = ref<AgentModelPick>({ model_config_id: '', reasoning_effort: '' });
const savingPick = ref(false);
/** 内置档位（模型没配 reasoning_levels 时用），**由低到高**排好；一份在 utils/reasoning，
 *  模型表单「填入内置档位」与 Agent 设置里的解释档共用它，别在这里再抄一份。 */
const EFFORT_PRESETS = [...BUILTIN_EFFORTS];

const modelReadonly = computed(() => modelOptions.value.length === 0);
/** 真正生效的模型：本会话选的那条 → 该类型默认那条；都没有就是 `/agent/status` 的回落 */
const activeModel = computed(() => modelOptions.value.find(m => m.id === pick.value.model_config_id)
  ?? modelOptions.value.find(m => m.is_default) ?? modelOptions.value[0] ?? null);
const modelLabel = computed(() => activeModel.value?.name || activeModel.value?.model_name
  || status.value?.model?.model_name || '—');
/** 厂商图标：走 `assets/model-icons` 的**统一映射**（不在本组件里再维护一份别名表）。
 *  彩色原色图标用 `<img>`；认不出厂商就是空串，模板里那枚 `<img>` 直接不渲染。 */
/** 模型候选**按提供方分组**（改造计划 §4：展开选择器后按提供方分组显示）。
 *  分组只按 `provider` 归堆、组内保持后端给的顺序（默认/启用优先），
 *  组标题直接写提供方 id —— 图标来自统一映射，这里不再维护第二份厂商表。 */
const modelGroups = computed(() => {
  const out: Array<{ provider: string; items: ModelConfig[] }> = [];
  for (const m of modelOptions.value) {
    const key = String(m.provider || '') || '其他';
    const hit = out.find((g) => g.provider === key);
    if (hit) hit.items.push(m);
    else out.push({ provider: key, items: [m] });
  }
  return out;
});

function vendorIco(provider?: string | null): string {
  return vendorColorIcon(provider);
}

function subLine(m?: { provider?: string; model_name?: string | null } | null) {
  return m ? [m.provider, m.model_name].filter(Boolean).join(' · ') : '';
}
const modelSub = computed(() => subLine(activeModel.value ?? status.value?.model));
/** 默认档取模型的 `parameters.reasoning_effort`（没配就只写"默认"） */
const defaultEffort = computed(() => {
  const p = activeModel.value?.parameters?.reasoning_effort
    ?? status.value?.model?.parameters?.reasoning_effort;
  return typeof p === 'string' ? p : '';
});
/** 默认档本身就在候选里（例如默认 high）：那就**直接把它选上**，不再多一条「默认（high）」。
 *  用户口径 2026-11-26：菜单里出现的应该就是真正生效的那一档，别让人对着两个 `high` 猜。 */
const defaultInOptions = computed(() => Boolean(defaultEffort.value)
  && effortOptions.value.some(o => o.value === defaultEffort.value));
/** 显示与勾选用哪一档：显式选的；没显式选但默认档就在候选里时，就是那一档。 */
const activeEffort = computed(() => pick.value.reasoning_effort
  || (defaultInOptions.value ? defaultEffort.value : ''));
const effortLabel = computed(() => activeEffort.value
  || (defaultEffort.value ? `默认（${defaultEffort.value}）` : '默认'));
const pickTitle = computed(() => `问答模型：${modelLabel.value}`
  + `${modelSub.value ? `（${modelSub.value}）` : ''}｜推理等级：${effortLabel.value}`);
/** 等级候选：**按模型配置**取 `parameters.reasoning_levels`（[{value, desc}]）；
 *  没配 / 空数组就回落到内置的 low/medium/high（DeepSeek 那几个模型就是这个现状）。 */
const effortOptions = computed<{ value: string; desc: string }[]>(() => {
  const raw = activeModel.value?.parameters?.reasoning_levels;
  const fallback = EFFORT_PRESETS.map(value => ({ value, desc: '' }));
  if (!Array.isArray(raw)) return fallback;
  const items = raw.map((it: any) => ({
    value: typeof it === 'string' ? it : String(it?.value ?? ''),
    desc: typeof it?.desc === 'string' ? it.desc : '',
  })).filter(it => it.value);
  return items.length ? items : fallback;
});
/** 生效档不在候选里（历史数据）—— 兜底：仍然把它自己渲染成一条带勾的候选 */
const isCustomEffort = computed(() => Boolean(pick.value.reasoning_effort)
  && !effortOptions.value.some(o => o.value === pick.value.reasoning_effort));
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

/* ---------------------- 底部那行明细：缓存命中率 + 上下文占比 ----------------------
   用户口径 2026-11-26：这一行要直接看见"前缀缓存命中了多少"和"上下文用到几成"，
   而不是两样都要悬停才知道。两个数都来自后端的 conversation_usage：
   · cache_hit_percent = 上一轮 prompt 里命中缓存的部分 / 总 prompt（没有快照时是 null）；
   · percent = 当前上下文 token / agent 预算。
   老会话没有缓存快照 —— 那就整段不渲染，宁可不显示，也不给一个假的 0%。 */
function fmtTokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(2)}M`;
  if (n >= 1000) return `${(n / 1000).toFixed(1)}k`;
  return String(n);
}
const cacheHitText = computed(() => {
  const v = usage.value?.cache_hit_percent;
  return typeof v === "number" ? `${v.toFixed(1)}%` : "";
});
const cacheHitTitle = computed(() => {
  const u = usage.value;
  if (!u || typeof u.cache_read_tokens !== "number" || !u.input_tokens) return "";
  return `上一轮 prompt ${fmtTokens(u.input_tokens)} tok，其中 ${fmtTokens(u.cache_read_tokens)} tok`
    + " 命中前缀缓存（命中部分按更低单价计费）";
});
/** 上下文占比：圆环旁那个数字 —— 圆环只表达粗细，数字负责说清"用了多少" */
const contextText = computed(() => {
  const v = percent.value;
  // 预算调大之后（百万级）占比常是个位数，四舍五入到 1% 会把 0.6% 显示成「1%」—— 小数位留给它
  return `${v < 10 ? v.toFixed(1) : Math.round(v)}%`;
});
const contextTitle = computed(() => {
  const u = usage.value;
  if (!u) return ringTitle.value;
  return `${fmtTokens(u.tokens)} / ${fmtTokens(u.budget)} tok · 占上下文 ${Math.round(percent.value)}%`;
});
const allPicked = computed(() => conversations.value.length > 0
  && picked.value.length === conversations.value.length);

function render(text?: string | null) {
  return mdToHtml(text ?? '', props.paperId);
}

function escapeAttr(text: string) {
  return String(text).replace(/&/g, '&amp;').replace(/"/g, '&quot;')
    .replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

/** **一个方括号**（`[3]` / `[3–5]` / `[3，7]` / `[3、7]`）：一枚 token = 一个方括号。
 *  **绝不**把紧挨着的多个方括号（`[3][7]`）并成一枚 —— 那是"一枚按钮含多号"，
 *  与后端契约第 5 条冲突（正文里每个标号只有 `[n]` / `[a–b]` 两种形态，`[3][7]` 是两枚标号）。
 *  方括号里只许编号与分隔符，`[见附录]` 不算引用。 */
const REF_BRACKET_RE = /\[\s*\d{1,3}(?:\s*[,，、]\s*\d{1,3}|\s*[-–—~]\s*\d{1,3})*\s*\]/g;

/**
 * 把**一个方括号**拆成「展开后的编号 + 显示成什么样」。
 *
 * 序号**连续展开**（`[3–5]` → 3、4、5）：判定与落点都要按编号逐条去 `byRef` 里找目标，
 * 区间只是写法。显示一律**原样**（契约第 5 条：`[3–5]` 要保留 en dash，不归一成 `[3-5]`）。
 * 解析不出来（区间过宽、夹了非编号字符）返回 null：调用方原样留文本，绝不吞字。
 */
function parseRefGroup(run: string): { nums: number[]; display: string } | null {
  const inner = run.slice(1, -1);
  if (!inner.trim()) return null;
  const nums: number[] = [];
  for (const seg of inner.split(/[,，、]/)) {
    const s = seg.trim();
    if (!s) continue;
    const span = /^(\d{1,3})\s*[-–—~]\s*(\d{1,3})$/.exec(s);
    if (span) {
      const lo = Math.min(Number(span[1]), Number(span[2]));
      const hi = Math.max(Number(span[1]), Number(span[2]));
      // 一页正文里也就几个引用：区间宽到这个程度必然是别的意思（年份、页码），别展开
      if (hi - lo > 20) return null;
      for (let n = lo; n <= hi; n += 1) nums.push(n);
      continue;
    }
    if (!/^\d{1,3}$/.test(s)) return null;
    nums.push(Number(s));
  }
  const uniq = Array.from(new Set(nums)).sort((a, b) => a - b);
  if (!uniq.length) return null;
  return { nums: uniq, display: run };
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
 *
 * **一个方括号 → 至多一枚按钮**（后端契约第 5 条）：方括号里的号全都能在引用表里找到、
 * 且它们属于**同一条** citation（`members` 与该方括号展开的号一致）时，画**一枚**按钮，
 * label **原样**（`[3–5]` 保留 en dash）；否则**每个号各自一枚**，绝不跨号、跨方括号合并 ——
 * 相邻两枚按钮显示文本再像也是两个标号（用户口径：不同的块组合就是不同的标号）。
 * 这里只负责把编号写进 `data-refs`、把**整条消息的引用池**写进 `data-pool`，点这一枚就连池子
 * 一起交给父组件（`ReaderView.focusMany`：闪哪几处要看那一行的类型与 pids，只有阅读器那边有行数据）。
 */
/**
 * markdown 的 `cite` 扩展（`useMarkdown.ts` 的 `name: "cite"`）已经画好的标号按钮：
 * **只有** `data-refs`，没有 row / block / page / latex。
 *
 * 对话正文这里必须把它**消费掉**再画自己那一枚，而不是在它的文本 `[1, 2]` 上再匹配一次 ——
 * 那会套出「button 里套 button」，而点击 / 悬浮命中的是**外层那个空壳**：没有编号表
 * 与引用池就既不闪公式、也不挂 data-flash-span，速览卡还只能说「这一段没有可显示的内容」
 * （用户 2026-12-11 实测到的正是这个）。
 */
const MD_CITE_BTN = /<button\b[^>]*\bdata-refs="([^"]*)"[^>]*>[\s\S]*?<\/button>/gi;
/** 占位符：已经换好的按钮先藏起来，免得后面那趟 `REF_BRACKET_RE` 又把它里面的 `[1]` 包一层。
 *  与 `useMarkdown.decorateCiteMarks` 的 `CITE_HOLD` 同一手法，换个前缀免得撞车。 */
const REF_HOLD = /\u0001CR(\d+)\u0001/g;

/** 一枚引用按钮（**只有一层**）：两条来源共用 —— ① md 扩展已经画好的；② 扩展没吃到、还是纯文本的。
 *  共用是刻意的：两边各拼一份，迟早有一边忘了带编号表（这一轮的"按钮套按钮"就是这么来的）。
 *  `cite` 是这一枚代表的那**一条** citation（组的 `ref` = members[0]，单词条就是它自己）；
 *  `nums` 是它覆盖的编号（= `data-refs`：组写 members，单词条写自己）；
 *  `display` 是它显示成什么样 —— **原样**（契约第 5 条：`[3–5]` 保留 en dash）。 */
function refButtonHtml(
  cite: AgentCitation,
  nums: number[],
  display: string,
  pool: CitePoolItem[],
): string {
  const own = Array.from(new Set(nums.map(Number).filter((n) => Number.isFinite(n) && n > 0)));
  const shown = own.length ? own : [Number(cite.ref)];
  const title = escapeAttr(cite.snippet || cite.label || `引用 ${shown[0]}`);
  /* 用 aria-label 而不是 title：title 会弹**浏览器原生**那一条灰气泡，
     而悬浮标号已经会弹速览卡了 —— 两个提示叠在一起（用户口径 2026-12-04）。 */
  return `<button type="button" class="cite-inline" aria-label="${title}"`
    /* 老字段一律取**组内第一条**：悬浮速览卡只显示一条；点单枚时那条老路径也还要用到
       （图注合成锚点 fig:p… 与「跨论文」守卫只在 gotoCitation 里）。 */
    + ` data-ref="${nums[0]}" data-paper="${escapeAttr(cite.paper_id || '')}"`
    + ` data-row="${escapeAttr(cite.row_id || '')}"`
    + ` data-block="${escapeAttr(cite.block_id || '')}"`
    + ` data-snippet="${escapeAttr(cite.snippet || '')}"`
    /* 优先存 citation.latex（完整式），不是 snippet：snippet 只有 160 字。
       但**存量消息**（没有 latex 这个键）的公式引用只有 snippet，而它的 snippet 本来就是
       被截到 160 字的 LaTeX —— 落进 data-* 才走得到 findFormula 的前缀匹配，
       否则老记录点过去只剩整行闪 */
    + ` data-latex="${escapeAttr(cite.latex || (cite.kind === 'formula' ? cite.snippet || '' : ''))}"`
    + ` data-page="${cite.page ?? ''}"`
    /* 合并组的编号表（探针与以后按组做别的事用）。 */
    + ` data-refs="${nums.join(',')}"`
    /* 这条消息的**整个引用池**（全部引用，不只是组里这几个）：点这一枚要闪哪几处，
       得看落点相不相邻、以及那一行是不是整行公式 —— 两件事都要 DOM 与行数据，
       所以判定统一交给阅读器（`ReaderView.buildCiteChain`）。这里只搬运。
       代价是每个按钮都带一份池子的 JSON（几十条引用 ≈ 几 KB），换来的是
       "正文 / 速览卡 / 闪烁"三处只有**一套**判定。 */
    + ` data-pool="${escapeAttr(JSON.stringify(pool))}"`
    + `>${display}</button>`;
}

/**
 * 一个方括号展开的那几个号 → 它们合起来的**那一条** citation（合不成就 null）。
 *
 * 口径（与后端契约第 5 条同一条，也是用户刚定稿的「不同的块组合 = 不同的标号」）：
 * 标号的**身份是它覆盖的块/行集合**，所以
 * - `members` 存在时，必须与该方括号展开的号**集合完全一致**才算同一个标号（组的 `ref` = members[0]，
 *   于是 members 里第 2 个号往后在 `byRef` 里查不到 —— 这一支就是它们唯一的入口）；
 * - `members` 缺省（老形状 / 单词条）时，这个方括号只能是**单号**。
 * 任何一条不成立就返回 null：调用方把每个号各自画一枚，**绝不跨号合并**。
 */
function citeOfBracket(nums: number[], byRef: Map<number, AgentCitation>): AgentCitation | null {
  if (!nums.length) return null;
  const found: AgentCitation[] = [];
  for (const n of nums) {
    const c = byRef.get(n);
    if (c) found.push(c);
  }
  if (!found.length) return null;
  const sameSet = (a: number[], b: number[]) => a.length === b.length && b.every((n) => a.includes(n));
  // ① 有一条自己声明了「我就是这枚方括号」
  for (const c of found) {
    const members = Array.isArray(c.members) ? c.members.map(Number) : null;
    if (members?.length && sameSet(members, nums)) return c;
  }
  /* ② 老形状：全部号都认得、且指向**同一条** —— 且它没声明 members（声明了就必须走 ① ） */
  if (found.length !== nums.length) return null;
  const first = found[0];
  if (!found.every((c) => c === first)) return null;
  if (Array.isArray(first.members) && first.members.length) return null;
  return nums.length === 1 ? first : null;
}

/** 一个方括号（或 md 扩展画好的那一枚）该画成什么 HTML。
 *  能认出**同一条** citation → 一枚按钮，label 原样；
 *  否则**每个号各自一枚**按钮，认不出的号**保留原文本**（绝不吞号、绝不跨号合并）。 */
function refButtonsFor(
  nums: number[],
  display: string,
  byRef: Map<number, AgentCitation>,
  pool: CitePoolItem[],
): string {
  const one = citeOfBracket(nums, byRef);
  if (one) return refButtonHtml(one, nums, display, pool);
  return nums.map((n) => {
    const hit = byRef.get(n);
    return hit ? refButtonHtml(hit, [n], `[${n}]`, pool) : `[${n}]`;
  }).join('');
}

function decorateRefs(html: string, cites?: AgentCitation[]) {
  if (!cites?.length) return html;
  /* 编号 → citation。老消息里同一个号可能出现两次（同一条证据被引了两遍）：
     取**第一条**，与原来 `new Map(...)` 的覆盖顺序一致。 */
  const byRef = new Map<number, AgentCitation>();
  for (const c of cites) {
    const n = Number(c?.ref);
    if (Number.isFinite(n) && n > 0 && !byRef.has(n)) byRef.set(n, c);
  }
  /* 引用池整段只拼一次（拼法见 `citationAnchor`）：与正文怎么切块无关，
     每条按钮上都写同一份 —— 阅读器据此按"同页 + 行号差 1"扩链。 */
  const pool: CitePoolItem[] = cites
    .filter((c) => c && Number(c.ref) > 0)
    .map((c) => ({
      ref: Number(c.ref),
      row: citationAnchor(c),
      block: c.block_id || '',
      // JSON 里不写 null：0 = 不知道页码（阅读器那边 `t.page || undefined` 同一条口径）
      page: Number(c.page ?? 0) || 0,
      // 与 `data-latex` 同一条兜底：存量消息的公式引用只有被截到 160 字的 snippet
      latex: String(c.latex || (c.kind === 'formula' ? c.snippet || '' : '')),
      // 这一条证据覆盖的**行身份**（chunk 的 pids）：跨页的同一块靠它认出来，
      // 点一枚标号会把整块（含另一页那半截）一起闪；老消息没有这个字段 → 空数组。
      pids: Array.isArray(c.pids) ? c.pids.map(String) : [],
    }));
  const parts = html.split(/(<code[\s\S]*?<\/code>|<pre[\s\S]*?<\/pre>)/i);
  return parts.map((part, index) => {
    if (index % 2 === 1) return part;                       // 代码块原样返回
    const held: string[] = [];
    const hold = (s: string) => {
      held.push(s);
      return `\u0001CR${held.length - 1}\u0001`;
    };
    /* ① 先**消费** md 扩展画好的按钮：认出它是**同一条** citation 就换成自己那一枚
       （带编号表与引用池）；认不出就原样留着 —— 那种标号本来也没有落点，与今天的行为一致。
       两种都要先藏进占位符，否则下一步会把它文本里的 `[1]` 再包一层。 */
    let out = part.replace(MD_CITE_BTN, (whole, refs: string) => {
      const nums = Array.from(new Set(String(refs).split(/[,;]/)
        .map((s) => Number(s.trim()))
        .filter((n) => Number.isFinite(n) && n > 0)));
      if (!nums.length) return hold(whole);
      // md 那边只产出 `[1]` / `[1, 2]`（区间、全角逗号它不吃）：号还是那几个号，显示归到无空格的写法
      return hold(refButtonsFor(nums, `[${nums.join(',')}]`, byRef, pool));
    });
    /* ② 扩展**没吃到**的写法（`[3–5]` 区间、`[3][7]`、全角逗号 `[3，7]`、`[3、7]`）：扩展只兜
       「单层方括号 + 半角逗号/分号」，其它形态到这儿还是纯文本，正则补上。
       **一个方括号至多一枚按钮**：`[3][7]` 是两枚（跨方括号绝不合并，用户口径 2026-12-27）。 */
    out = out.replace(REF_BRACKET_RE, (whole) => {
      const parsed = parseRefGroup(whole);
      return parsed ? refButtonsFor(parsed.nums, parsed.display, byRef, pool) : whole;
    });
    return out.replace(REF_HOLD, (_m, i: string) => held[Number(i)] ?? '');
  }).join('');
}

/** 正文渲染：markdown + 把 `[n]` 接成可点锚点。 */
function renderAnswer(text: string | null | undefined, cites?: AgentCitation[]) {
  return decorateRefs(mdToHtml(text ?? '', props.paperId), cites);
}

/**
 * 正文切成**稳定的块**（只给正在流式的那条助手消息用，见 `answerParts`）。
 *
 * 为什么切：`v-html` 是"值一变就把这个元素的 DOM 整个重建"。流式时正文每落一批增量，
 * 前面**已经写完**的段落、公式、引用锚、图片也统统被销毁重建 —— 长回答越写越卡，
 * 光标每 90ms 闪一下，读的时候还能看到整段在轻微抖。切成块以后每块各自 `v-html`：
 * 已封口的块字符串不变 → Vue 跳过 `innerHTML` 赋值 → 那一块 DOM 原样留着，
 * 只有"还在长的那一块"重画。
 *
 * 切分**必须稳定**（同样的前缀切出同样的块序），否则前面又会被重建。所以规则只有一条：
 * 在**顶层空行**处切；代码围栏里不切；列表 / 引用 / 表格之间的空行是"松列表"，也不切。
 */
function splitAnswerParts(text: string): string[] {
  const src = String(text ?? '').replace(/\r\n?/g, '\n');
  if (!src.trim()) return [];
  const lines = src.split('\n');
  const parts: string[] = [];
  let buf: string[] = [];
  let fenceCh = '';                       // 围栏打开时记着「`」/「~」
  let fenceLen = 0;
  let lead = '';                          // 当前块的首行类别（松列表要跟着它走）
  const flush = () => {
    const raw = buf.join('\n');
    if (raw.trim()) parts.push(raw);
    buf = [];
    lead = '';
  };
  const kindOf = (line: string): string => {
    const t = line.trimStart();
    if (/^(?:[-*+]|\d{1,9}[.)])(?:\s|$)/.test(t)) return 'list';
    if (t.startsWith('>')) return 'quote';
    if (t.startsWith('|')) return 'table';
    return 'plain';
  };
  for (let i = 0; i < lines.length; i += 1) {
    const line = lines[i];
    const tick = /^\s{0,3}(`{3,}|~{3,})\s*$/.exec(line);
    if (fenceCh) {
      buf.push(line);
      if (tick && tick[1][0] === fenceCh && tick[1].length >= fenceLen) { fenceCh = ''; fenceLen = 0; }
      continue;
    }
    if (tick) {
      // 围栏前面没隔空行时也自成一块（否则前一段会被裹进代码块）
      if (buf.length && !lead) flush();
      fenceCh = tick[1][0];
      fenceLen = tick[1].length;
      buf.push(line);
      continue;
    }
    buf.push(line);
    if (line.trim()) { if (!lead) lead = kindOf(line); continue; }
    let j = i + 1;
    while (j < lines.length && !lines[j].trim()) j += 1;
    if (j >= lines.length) continue;                          // 末尾的空行
    const cur = lead || 'plain';
    if (cur === kindOf(lines[j]) && cur !== 'plain') continue; // 松列表 / 多段引用 / 带空行的表格
    flush();
  }
  flush();
  return parts;
}

/** 已渲染块的 HTML 复用池（key：引用签名 + 原始块）。流式期间同一块每批都要取值，
 *  拿到的字符串必须**一模一样**，Vue 才会跳过 `innerHTML` 赋值（这才是"不重建"的关键）。 */
const partHtmlCache = new Map<string, string>();

/** 正文 → 分块 HTML。`tail` 标记"还在长的那一块"：光标与渐显都只作用在它上面。 */
function answerParts(m: AgentMessage): { html: string; tail: boolean }[] {
  const raw = m.role === 'user' ? splitQuote(m.content).rest : finalAnswer(m);
  const raws = splitAnswerParts(raw);
  if (!raws.length) return [];
  const sig = m.citations?.map(c => c.ref).join(',') ?? '';
  const out = raws.map((part) => {
    const key = `${sig}\u0000${part}`;
    let html = partHtmlCache.get(key);
    if (html === undefined) {
      html = renderAnswer(part, m.citations);
      if (partHtmlCache.size > 600) partHtmlCache.clear();
      partHtmlCache.set(key, html);
    }
    return html;
  });
  return out.map((html, i) => ({ html, tail: i === out.length - 1 }));
}

/** 按钮上的 `data-pool`（这条消息**整个**引用池）→ 数组。
 *  坏数据（手改过的 DOM、以后改了格式）一律当"没有"，退回"只闪这一枚按钮自己"：
 *  引用闪错地方比闪不动更糟，而两者都比"点了之后抛异常"好。 */
function parseCitePool(raw?: string): CitePoolItem[] {
  if (!raw) return [];
  try {
    const list = JSON.parse(raw);
    if (!Array.isArray(list)) return [];
    return list
      .filter((t) => t && typeof t === 'object')
      .map((t) => ({
        ref: Number(t.ref) || 0,
        row: String(t.row ?? ''),
        block: String(t.block ?? ''),
        page: Number(t.page ?? 0) || 0,
        latex: String(t.latex ?? ''),
        /* 这一条证据覆盖的**行身份**：写进 `data-pool` 却在这里丢掉的话，
           跨页的同一块 / 连续组那几行就永远闪不出来（点 `[3–5]` 只亮第一行就是这么来的）。 */
        pids: Array.isArray(t.pids) ? t.pids.map(String) : [],
      }))
      .filter((t) => t.ref > 0);
  } catch {
    return [];
  }
}

/** 按钮上的编号表（`data-refs="2,4,10"`）→ 数组。 */
function parseRefNums(raw?: string): number[] {
  return String(raw || '')
    .split(',')
    .map((s) => Number(s.trim()))
    .filter((n) => Number.isFinite(n) && n > 0);
}

/**
 * 点正文里的 `[n]`：跳到那条引用（阅读器会滚到对应行）。
 * 顺带接管**图片**：回答里自带的 `![](...)` 也走阅读器那套图片查看器（用户口径 2026-11-19），
 * 而不是点了没反应、或被浏览器当成链接开新页。
 */
function onCitationClick(ev: MouseEvent) {
  const target = ev.target as HTMLElement | null;
  const img = target?.closest?.('img') as HTMLImageElement | null;
  if (img?.src) {
    ev.preventDefault();
    emit('zoom', { url: img.src, caption: img.getAttribute('alt') || '' });
    return;
  }
  const btn = target?.closest?.('.cite-inline') as HTMLElement | null;
  if (!btn) return;
  /* 这一枚按钮只报「被点的编号 + 整条消息的引用池」，**不自己算链**：闪哪几处取决于
     落点相不相邻、以及那一行是不是"整行就是一坨公式"—— 两件事都要 DOM 与行数据，
     只有阅读器那边有（`ReaderView.focusMany` / `buildCiteChain`）。 */
  const refs = parseRefNums(btn.dataset.refs);
  const pool = parseCitePool(btn.dataset.pool);
  const paper = btn.dataset.paper || '';
  if (paper && paper !== props.paperId) {
    // 与 gotoCitation 同一条口径：别的论文的块在这里没有落点，只提示、不乱跳
    toast(`这条引用来自《${btn.getAttribute('aria-label') || paper}》，不在当前论文里`, 'warn');
    return;
  }
  if (pool.length) {
    const picked = refs.length ? refs : parseRefNums(btn.dataset.ref);
    emit('focus-many', { refs: picked, pool });
    return;
  }
  /* 兜底：没有 `data-pool`（更早一版渲染出来的 DOM / 手改过的标号）→ **只闪这一枚自己**：
     把按钮自己的落点当成只有一项的池子，阅读器那条路一个字都不用改。 */
  const own: CitePoolItem = {
    ref: Number(btn.dataset.ref || 0) || 0,
    row: btn.dataset.row || '',
    block: btn.dataset.block || '',
    page: Number(btn.dataset.page || 0) || 0,
    latex: btn.dataset.latex || '',
  };
  if (own.row || own.block || own.page || own.latex) {
    emit('focus-many', { refs: own.ref ? [own.ref] : [], pool: [own] });
    return;
  }
  // 连落点都没有（标号本来就没有对应记录）：退回老的单条路径，由它给出提示
  const page = Number(btn.dataset.page || 0);
  gotoCitation({
    ref: Number(btn.dataset.ref || 0),
    paper_id: btn.dataset.paper || props.paperId,
    kind: 'block',
    block_id: btn.dataset.block || null,
    row_id: btn.dataset.row || null,
    page: page > 0 ? page : null,
    // data-latex 为空（非公式引用）时留空串：gotoCitation 里靠 kind/snippet 判定要不要带上
    latex: btn.dataset.latex || '',
    label: btn.getAttribute('aria-label') || '',
  } as AgentCitation);
}

/**
 * 悬浮正文里的引用标号 → 请求在旁边 peek 一眼（用户口径 2026-12-03）。
 *
 * 只上报位置与锚点，**不碰点击**：点标号仍然走 onCitationClick（跳原文）；
 * 悬浮只是先看一眼。真正的浮层在 ReaderView —— 那里才有整篇的行 / 图 / 表。
 */
function onCitePeek(ev: PointerEvent) {
  const btn = (ev.target as HTMLElement | null)?.closest?.('.cite-inline') as HTMLElement | null;
  if (!btn) return;
  const r = btn.getBoundingClientRect();
  const page = Number(btn.dataset.page || 0);
  const refs = parseRefNums(btn.dataset.refs);
  const pool = parseCitePool(btn.dataset.pool);
  /* 速览卡要和点击**同一套口径**（用户口径 2026-12-11：「鼠标悬浮的浮窗也要改」）：
     点一枚 → 它那条链一起闪；那悬浮它时卡片就得画**整条链**，否则出现
     「正文闪了三行、卡片只给一行」。所以这里也**不算链**，只把编号与引用池交上去 ——
     算链是阅读器那一枚纯函数（`buildCiteChain`）的事，两边各算一遍迟早算出两个答案
     （而且公式行那条规则只有那边认得）。 */
  emit('peek', {
    anchor: { x: r.left, y: r.top, w: r.width, h: r.height },
    /* 卡片头（标题 / 锚点 / 定位）由阅读器按链头改写：相邻两枚按钮共用一条链时，
       卡片内容不该随鼠标落在链的哪一枚上而变来变去。这里给的是"这一枚自己"的兜底值。 */
    page: page > 0 ? page : null,
    rowId: btn.dataset.row || null,
    blockId: btn.dataset.block || null,
    label: btn.dataset.label || btn.getAttribute('aria-label') || '',
    /* rows 一律 null：带 pool 时由阅读器算（见上面），没 pool 时退回"这一行" */
    rows: null,
    /* 引用自带的片段：老引用可能既没有行 id 也没有可映射的块 id，
       这时卡片至少能显示这一两句话，而不是一句「没有可显示的内容」 */
    snippet: btn.dataset.snippet || '',
    refs: refs.length ? refs : parseRefNums(btn.dataset.ref),
    pool: pool.length ? pool : null,
  });
}

function onCitePeekOut() { emit('peek-hide'); }

/** 划词那种以 `> 引用` 开头的用户消息：正文只显示引用之后的部分（引用单独排版）。 */
function splitQuote(content: string): { quote: string; rest: string } {
  const match = /^>\s?(.*)\n+([\s\S]*)$/.exec(content || '');
  if (!match) return { quote: '', rest: content || '' };
  return { quote: match[1].trim(), rest: match[2].trim() };
}

/**
 * 触摸点输入框时**兜一次焦点**。
 *
 * 移动端上焦点会被别处的收尾逻辑顺手带走（文档选区同步、布局变化、右键菜单收起…），
 * 表现是「点一下输入框，光标闪一下就没了，键盘不出」——尤其是右键菜单「添加到对话框」之后
 * 第一次点（用户口径 2026-11-26）。这里不去猜是谁带走的：按下后补两拍 focus，
 * 已经聚焦时 focus() 是空操作，不会打断输入。
 */
function keepComposerFocus(ev: PointerEvent) {
  const t = ev.target as HTMLElement | null;
  const field = t?.closest?.('textarea, input') as HTMLTextAreaElement | HTMLInputElement | null;
  if (!field) return;
  if (ev.pointerType !== 'touch') return;
  const again = () => { if (document.activeElement !== field) field.focus({ preventScroll: true }); };
  void nextTick(again);
  window.setTimeout(again, 120);
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

/** 后端给"只发了图片"的提问合成的占位正文（`（图片 ×1）`），见 `api/agent.py`。 */
const IMAGE_ONLY_BODY = /^（图片\s*[×x]\s*\d+）$/;

/** 这条消息有没有**该渲染的正文**：图片占位文案不算（它只是给模型留的上下文）。 */
function hasBody(m: { role?: string; content?: string | null }) {
  const text = String(m.content ?? '').trim();
  if (!text) return false;
  return !(m.role === 'user' && IMAGE_ONLY_BODY.test(text));
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
/** 写操作（改用户数据）：它的返回摘要值得直接展示，且做完要立刻刷新侧栏。 */
const WRITE_TOOL = /note|highlight|annotation|page_edit|page_markdown_edit|page_product|meta_write/i;

const TOOL_STEPS: Record<string, string> = {
  paper_search: '检索论文', kb_search: '检索知识库',
  highlight_manage: '标高亮', note_manage: '写笔记', paper_meta_write: '填元数据',
  page_markdown_get: '读正文', block_get: '读段落',
  page_image_get: '看页面图', figure_list: '看插图清单', figure_image_get: '看插图',
  formula_get: '取公式', translate_get: '取译文',
  library_list: '翻书架', notes_search: '查笔记',
  paper_archive_get: '看背景档案', reader_state: '看阅读位置',
  page_markdown_edit: '修订产物',
  // 联网：与 backend/agent/tools.py 的 WEB_TOOLS 同名（永远是挂载的，没配密钥才报错）
  web_search: '联网搜索', web_fetch: '读网页',
};

/** 工具 → 图标（用站点已有的那套；没有对应项的退回 `bolt`）。 */
const TOOL_ICONS: Record<string, string> = {
  paper_search: 'search', kb_search: 'knowledge',
  page_markdown_get: 'document', block_get: 'document',
  page_image_get: 'image', figure_list: 'list', figure_image_get: 'image',
  formula_get: 'formula', translate_get: 'translate',
  library_list: 'library', notes_search: 'note',
  paper_archive_get: 'document', reader_state: 'locate',
  page_markdown_edit: 'edit',
  highlight_manage: 'formula', note_manage: 'note', paper_meta_write: 'check',
  web_search: 'search', web_fetch: 'external',
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
  [['paper_archive_get'], '看过背景档案'],
  [['reader_state'], '看过阅读位置'],
  [['page_markdown_edit'], '改过产物'],
  [['highlight_manage'], '标过高亮'],
  [['note_manage'], '记过笔记'],
  [['paper_meta_write'], '填过元数据'],
  [['web_search'], '已搜索网页'],
  [['web_fetch'], '已读网页'],
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

/** 轨迹行 = 步骤 + 它在 `run.steps` 里的**原始序号**（`openSteps` 的键要用它）。 */
type TraceRow = { s: AgentStep; i: number };

/**
 * 轨迹里**要渲染**的那些步骤（带原始序号）。
 *
 * 剔掉"最后一段正文＝回答本体"（它渲染在轨迹外面），于是 `v-for` 的每一项都**只有一个
 * 根元素** —— 这是 `TransitionGroup` 进场/让位过渡的前提（以前每个 key 还带一个空分支）。
 * 序号保留原始值：`openSteps` 的键是 `${id}:序号`，筛选不能让下标错位。
 */
function traceRows(m: AgentMessage) {
  const rows: TraceRow[] = [];
  stepsOf(m).forEach((s, i) => {
    /* 阶段旁白（`stage`）不渲染：这类"正在准备…""正在阅读并组织回答…"在 2026-12-09 之后
       已经不发了，但**老会话的 `run.steps` 里还留着**，这里兜一层，免得翻旧消息又冒出来。 */
    if (s.kind === 'stage') return;
    if (s.kind === 'text' && !showInTrace(m, s)) return;
    rows.push({ s, i });
  });
  return rows;
}

/**
 * 轨迹的**渲染项**：连续的 `tool` 步骤并成一组（`rows`），其余一步一项（`lead`）。
 *
 * 为什么分组：参考实现里工具调用是"一个抬头 + 下面缩进的若干子行 + 一条竖引导线"，
 * 读起来是"这一段在干活"；一条条平铺会把过程与正文混成一片（用户口径 2026-10-25）。
 */
type TraceItem = { key: string; lead?: TraceRow; rows?: TraceRow[]; last?: boolean };

function traceItems(m: AgentMessage): TraceItem[] {
  const out: TraceItem[] = [];
  for (const r of traceRows(m)) {
    if (r.s.kind === 'tool') {
      const last = out[out.length - 1];
      if (last?.rows) last.rows.push(r);
      else out.push({ key: `g${r.i}`, rows: [r] });
    } else {
      out.push({ key: `r${r.i}`, lead: r });
    }
  }
  if (out.length) out[out.length - 1].last = true;
  return out;
}

/** 这一轮有没有**真的可以展开看**的东西（只有旁白的一轮：抬头照旧，但没有可展的正文）。 */
function hasTrace(m: AgentMessage): boolean {
  return traceItems(m).length > 0;
}

/**
 * 抬头那行要不要出现。
 *
 * 有步骤就出现；**还在跑的那一轮，一步都没有也出现** —— 起手那句"正在准备…"删掉之后，
 * 从发出去到第一段思考之间（几百毫秒到两三秒）就只剩这行"正在思考… · 0.4 秒"能说明
 * "它收到了、在跑"，不然那几秒里屏幕上什么都没有。历史消息没有步骤就不出现。
 */
function showRun(m: AgentMessage): boolean {
  return stepsOf(m).length > 0 || (m.role === 'assistant' && m.message_id === liveId.value);
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

/**
 * 轨迹里要显示的正文段 = 除**最后一段**（那段是回答本体，已经渲染在外面）之外的全部。
 *
 * 以前这里还要求"各段拼起来逐字等于 content"，否则整段正文都不进轨迹 ——
 * 结果是**过程里说过的话整段消失**（用户口径 2026-10-25：思考过程中的正文要留在思维链里）。
 * 只把最后一段摘出去就够了：最后一段要么就是回答本体，要么是它的前缀，摘掉都不会少字。
 */
function showInTrace(m: AgentMessage, step: AgentStep) {
  const texts = textStepsOf(m);
  if (texts.length < 2) return false;
  return step !== texts[texts.length - 1];
}

/** 抬头右侧：它这一轮都干了什么（按出现的类别去重）。 */
function runSummary(steps: AgentStep[]) {
  const tools = new Set(steps.filter(s => s.kind === 'tool').map(s => s.tool ?? ''));
  const bits = TOOL_SUMMARY.filter(([names]) => names.some(n => tools.has(n)))
    .map(([, text]) => text);
  return bits.length ? `${bits.join(' · ')}${bits.length > 1 ? ' 等' : ''}` : '';
}

/**
 * 抬头左侧：跑着的时候说"现在在干什么"，跑完说"用时多少"。
 *
 * 旁白（`stage`）这类"正在准备…""正在阅读并组织回答…"整句删掉了（用户口径 2026-12-09）：
 * 起手那句不再由前端塞、后端那条也不再发，抬头只用一句"正在思考…"（有工具在跑时说工具）。
 */
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

/** 展开 / 收起一整条轨迹（自动与手点共用，免得两处语义跑偏）。 */
function openRun(id: string): void {
  if (openRuns.value.includes(id)) return;
  openRuns.value = [...openRuns.value, id];
}

function closeRun(id: string): void {
  if (!openRuns.value.includes(id)) return;
  openRuns.value = openRuns.value.filter(x => x !== id);
  /* 收起整条轨迹 = 连内部的展开状态一起清掉：再点开时**内部全部折叠**
     （用户口径 2026-10-25「折叠再展开折叠内部的所有内容」）。
     清在**收起**这一刻而不是展开那一刻：收起时里面若还开着几段，它们会跟着一起播收起动画，
     两套高度动画叠在同一个盒子里，观感就是"突然塌一下"。 */
  const prefix = `${id}:`;
  openSteps.value = openSteps.value.filter(k => !k.startsWith(prefix));
  closedSteps.value = closedSteps.value.filter(k => !k.startsWith(prefix));
  thinkMax.value = Object.fromEntries(Object.entries(thinkMax.value).filter(([k]) => !k.startsWith(prefix)));
}

function toggleRun(id: string) {
  // ⚠️ 与 `isRunOpen` 同一套语义（**记"展开"而不是"收起"**）：默认收起后，
  // 这里写反过一次 —— 点抬头看见的是一动不动。
  runTouched.add(id);
  if (foldRunTimer) { clearTimeout(foldRunTimer); foldRunTimer = 0; }   // 人动手了，别再自动收
  pendingFold = '';
  if (isRunOpen(id)) closeRun(id);
  else openRun(id);
}

/**
 * 这一轮跑完（或失败 / 中止）时的收尾：轨迹**默认收起**，只留抬头一行。
 * 但用户在这一轮里自己点过展开/收起的话，那不是默认能覆盖的东西 ——
 * 占位消息的 id 与落库那条不同，所以把"开着"这个选择搬到落库那条的 id 上。
 */
function settleRun(placeholderId: string, replied: boolean) {
  if (!placeholderId) return;
  if (pendingFold === placeholderId) pendingFold = '';      // 这一轮已经结束，别再等自动收起
  if (foldRunTimer) { clearTimeout(foldRunTimer); foldRunTimer = 0; }
  const wasOpen = openRuns.value.includes(placeholderId);
  openRuns.value = openRuns.value.filter(x => x !== placeholderId);
  const answerId = replied ? lastAnswerId() : '';
  if (runTouched.has(placeholderId) && wasOpen && answerId) {
    openRuns.value = [...openRuns.value, answerId];
    runTouched.add(answerId);
  }
  runTouched.delete(placeholderId);
  /* 思考段的手动展开/收起也一起搬：键是 `${id}:序号`，换个 id 就失联了
     （不搬的话，落库后手点展开过的那段会莫名其妙塌回去）。 */
  if (answerId) {
    const moved = (keys: string[]) => keys.map(k => (k.startsWith(`${placeholderId}:`)
      ? `${answerId}${k.slice(placeholderId.length)}` : k));
    openSteps.value = [...new Set(moved(openSteps.value))];
    closedSteps.value = [...new Set(moved(closedSteps.value))];
    thinkMax.value = Object.fromEntries(Object.entries(thinkMax.value)
      .map(([k, v]) => [k.startsWith(`${placeholderId}:`) ? `${answerId}${k.slice(placeholderId.length)}` : k, v]));
  }
  /* "这一段思考写完了"那份标记只对流式中那条有意义（键换 id 就失效），落库后清掉，别越攒越多 */
  endedSteps.value = endedSteps.value.filter(k => !k.startsWith(`${placeholderId}:`));
}

function stepKey(id: string, i: number) {
  return `${id}:${i}`;
}

/** 这一轮**最新的一步**就是"还在写的思考"（流式中才算；历史消息永远不自动展开）。
 *  已经收到 `thinking`（done）的那一段不算 —— 它写完了，就该收起来（见 `endedSteps`）。 */
function liveThinkingAt(id: string, i: number) {
  if (!id || id !== liveId.value) return false;
  if (endedSteps.value.includes(stepKey(id, i))) return false;
  const m = messages.value.find(x => x.message_id === id);
  if (!m) return false;
  const steps = stepsOf(m);
  return i === steps.length - 1 && steps[i]?.kind === 'thinking';
}

/** 自动展开的那一段（流式中最新一步 = 思考）。它一出现工具/正文、或整轮结束就自己塌回去。 */
function autoGrowStep(id: string, i: number) {
  return liveThinkingAt(id, i);
}

/**
 * 展开与否：**默认折叠，但最后一段展开**（用户口径 2026-10-25：展开思维链时默认折叠所有内容、
 * 最后一段内容默认展开 —— 那一段就是"它刚在做什么"，最值得先看）。
 * 手点过的以手点为准：开进 `openSteps`、关进 `closedSteps`。
 */
function isStepOpen(id: string, i: number) {
  return openSteps.value.includes(stepKey(id, i));
}

/** 工具组的开合键：用组里第一条的原始序号（结算时随消息 id 一起搬）。 */
function groupKey(id: string, first: number) {
  return `${id}:t${first}`;
}

function itemOpenKey(id: string, item: TraceItem) {
  if (item.rows) return groupKey(id, item.rows[0].i);
  return item.lead ? stepKey(id, item.lead.i) : '';
}

function isItemOpen(m: AgentMessage, item: TraceItem) {
  const key = itemOpenKey(m.message_id, item);
  if (openSteps.value.includes(key)) return true;
  if (closedSteps.value.includes(key)) return false;
  /* **对话期间**自动展开"最后一段"（用户口径 2026-10-25）。
     2026-12-14 复核：以前还额外要求这一段的 status 还是 running，于是最后一次**工具调用**
     跑完立刻自己折回去 —— 用户看到的就是"最后一段工具调用没有自动展开"。
     现在只要"这一轮是 live 且它是最后一段"就展开（跑没跑完都算）；新的一步进来之后它不再是最后一段，
     自然收起，还是 DSH 那种跟着模型看的效果。历史消息展开思维链后内部一律折叠（口径没变）。 */
  return m.message_id === liveId.value && item.last === true;
}

/** 这一段还在进行中吗（决定要不要替用户自动展开）。 */
function itemRunning(m: AgentMessage, item: TraceItem) {
  if (item.rows) return item.rows.some(r => r.s.status === 'running');
  const lead = item.lead;
  if (!lead) return false;
  if (lead.s.status === 'running') return true;
  /* 思考段没有自己的 status：它在"还在写"的时候就是这一轮的最后一步 */
  return lead.s.kind === 'thinking' && liveThinkingAt(m.message_id, lead.i);
}

/** 点一下某一项：开 / 关（最后一段的"默认展开"也一并被这次点击覆盖）。 */
function toggleItem(m: AgentMessage, item: TraceItem, ev?: Event) {
  const key = itemOpenKey(m.message_id, item);
  if (!key) return;
  const open = isItemOpen(m, item);
  openSteps.value = open
    ? openSteps.value.filter(x => x !== key)
    : [...openSteps.value, key];
  closedSteps.value = open
    ? [...new Set([...closedSteps.value, key])]
    : closedSteps.value.filter(x => x !== key);
  const rest = { ...thinkMax.value };
  delete rest[key];
  thinkMax.value = rest;
  /* 展开一段思考：先量它展开后有多高，好让 `max-height` 有真实起点（`none → 长度` 不过渡） */
  if (!open && item.lead?.s.kind === 'thinking') {
    const text = ((ev?.currentTarget as HTMLElement | null) ?? null)?.querySelector('.step-text') as HTMLElement | null;
    const h = text ? measureOpenHeight(text) : 0;
    if (h) thinkMax.value = { ...thinkMax.value, [key]: h };
  }
}

/** 收起时那一行摘要：这一次都调了哪些工具（去重、按出现顺序，最多三样）。 */
function toolSummary(rows: TraceRow[]) {
  const seen: string[] = [];
  for (const r of rows) {
    const label = stepLabel(r.s.tool);
    if (!seen.includes(label)) seen.push(label);
  }
  const head = seen.slice(0, 3).join(' · ');
  return seen.length > 3 ? `${head} 等` : head;
}

/** 收起时那一行摘要：取思考正文的第一行（去掉 markdown 的 `**`）。 */
function thinkSummary(text?: string) {
  const line = (text ?? '').split('\n').map(s => s.trim()).find(Boolean) ?? '';
  return line.replace(/\*\*/g, '');
}

/** 展开时的高度上限：钉实测值（可过渡、不裁字）；量不到就先放开，绝不把一段长思考裁掉。 */
function stepMaxStyle(id: string, i: number) {
  const h = thinkMax.value[stepKey(id, i)];
  /* 还在写的那段不钉高度：文字每来一截都要长，钉死了就会被裁掉。 */
  if (h && !liveThinkingAt(id, i)) return { maxHeight: `${h}px` };
  return { maxHeight: 'none' };
}

/**
 * 一行轨迹的类名（工具子行用）。
 *
 * `TransitionGroup` 要求**每一项只有一个根元素**（拿 `v-if/v-else-if` 多个兄弟分支写，
 * 编译出来每个 key 会变成一个 Fragment，里面的分支 key 是 0/1/2 —— 所有行都撞在一起，
 * 让位动画就失效了）。所以这里合成类名，模板里一行只有一个根 div。
 */
function rowClass(_m: AgentMessage, r: TraceRow) {
  const s = r.s;
  if (s.kind === 'text') return 'md run-text';
  if (s.kind === 'stage') return 'run-step stage';
  if (s.kind === 'thinking') return ['run-step', 'think'];
  return ['run-step', 'tool', { running: s.status === 'running', bad: s.ok === false }];
}

/**
 * 轨迹容器的边缘渐隐：能往上/往下滚就给那一侧加一层遮罩（照 DSH 的过程块）。
 * 用 ResizeObserver + MutationObserver 跟着内容长高一起同步 —— 流式里每 90ms 都在长。
 *
 * 同时负责**贴底跟随**：跑着的那一轮内容长高时把容器滚到底（不然新长出来的思考在视野外，
 * 折叠态的摘要越来越长、框里却停在开头）。读者自己往上翻就松开跟随，翻回底部（≤25px）再接上。
 */
const RUN_FOLLOW_GAP = 25;
const runBodies = new Map<string, { ro: ResizeObserver; mo: MutationObserver }>();
/** 是否跟随（`false` = 读者主动往上翻了，别再抢滚动条；没记过 = 跟随） */
const runStick = new WeakMap<HTMLElement, boolean>();
/** 上一次由程序钉住的 scrollTop：用来把「程序滚动触发的 scroll 事件」和「读者滚动」分开 */
const runPin = new WeakMap<HTMLElement, number>();

function runAtBottom(el: HTMLElement): boolean {
  return el.scrollHeight - el.clientHeight - el.scrollTop <= RUN_FOLLOW_GAP;
}

function pinRunBottom(el: HTMLElement): void {
  if (runStick.get(el) === false) return;
  /* 容器被压成 0 高（整条轨迹的收起/展开动画里就会这样）时算不出真落点，
     硬钉会把 scrollTop 顶到内容最底再弹回来 —— 这一帧跳过。 */
  if (!el.clientHeight) return;
  const target = el.scrollHeight - el.clientHeight;
  if (target <= 0) return;
  if (target - el.scrollTop >= 1) el.scrollTop = target;   // 流式里用 instant，别 smooth 追不上
  runPin.set(el, el.scrollTop);
}

function syncEdges(el: HTMLElement | null): void {
  if (!el) return;
  el.classList.toggle('can-up', el.scrollTop > 1);
  el.classList.toggle('can-down', el.scrollTop < el.scrollHeight - el.clientHeight - 1);
}

/** 容器自己的 scroll 事件：读者翻到离底 >25px 就松开跟随，回到 ≤25px 再接上 */
function onRunScroll(el: HTMLElement): void {
  const pinned = runPin.get(el);
  const atBottom = runAtBottom(el);
  /* 钉底之后布局又长高时，那次 scroll 事件回放的位置仍等于 `pinned` —— 那不是读者干的，
     别据此松开跟随（否则流式里会「跟一下停一下」）。 */
  if (pinned === undefined || Math.abs(el.scrollTop - pinned) > 1.5) runStick.set(el, atBottom);
  syncEdges(el);
}

function setRunBody(id: string, el: unknown): void {
  const prev = runBodies.get(id);
  if (prev) {
    prev.ro.disconnect();
    prev.mo.disconnect();
    runBodies.delete(id);
  }
  if (!(el instanceof HTMLElement)) return;
  if (id !== liveId.value) runStick.set(el, false);   // 历史消息点开的轨迹：从头顶开始读，别钉底
  syncEdges(el);
  pinRunBottom(el);
  requestAnimationFrame(() => pinRunBottom(el));       // 首次挂载时布局可能还没算完
  /* ⚠️ 只盯容器本身不够：`.run-body` 有 `max-height`，段内展开/收起是**子项的高度动画**
     （`foldEnter`/`foldLeave` 逐帧改 height），容器自己的盒子一帧都不变 —— 它不触发 RO，
     也没有 DOM 变更可听。所以把**直接子项**也一起 observe：子项长高的每一帧都能跟上，
     这正是"思考在长、容器却停在开头"的成因。 */
  const pin = (): void => {
    /* 轨迹容器自己正在展开 / 收起（`runEnter`/`runLeave`）：高度是动画在写的，这会儿钉底
       会把内容一帧一帧往下拽，看着像"边收边滑"。等它收完再接着钉。 */
    if (el.dataset.foldAnim === '1') return;
    syncEdges(el);
    pinRunBottom(el);
  };
  const ro = new ResizeObserver(pin);
  const watchKids = (): void => {
    ro.observe(el);
    for (const kid of Array.from(el.children)) ro.observe(kid);
  };
  watchKids();
  const mo = new MutationObserver(() => {
    watchKids();
    pin();
  });
  mo.observe(el, { childList: true, subtree: true, characterData: true });
  runBodies.set(id, { ro, mo });
}

onUnmounted(() => {
  runBodies.forEach(x => { x.ro.disconnect(); x.mo.disconnect(); });
  runBodies.clear();
});

/**
 * 段内 / 整条轨迹的开合（`:css="false"` 的 JS 钩子，见模板里的 `<Transition name="fold">`）。
 *
 * 为什么不用 CSS：`height: auto` 没法过渡，而 `max-height` 的插值是"上限"，内容越长动画越快 ——
 * 两头都生硬。这里按实测高度走 Web Animations：
 *   · **时长跟着距离走**（`foldMs`）—— 一行字和一个 420px 的过程块用同一个 200ms，
 *     矮的显拖、高的显急，这就是"折叠不够丝滑"的一半来源；
 *   · 高度与透明度**不是同一条直线**：收起时先掉前 55% 的高度、透明度更早就掉到 .3，
 *     收尾只剩一条空盒子在缩（不然文字被压扁着消失，很脏）；展开反过来，高度先出来、
 *     字稍后才全亮；
 *   · 动画期间外层对话**每帧贴底**（`followThroughFold`），否则正文先被顶走一截、
 *     动画结束再弹回来。
 */
const FOLD_IN = 'cubic-bezier(.22, .61, .36, 1)';    // 展开：滑出来，尾巴收得慢
const FOLD_OUT = 'cubic-bezier(.4, 0, .2, 1)';       // 收起：起手带点劲，落地轻

function foldMs(dist: number, open: boolean): number {
  return Math.round(Math.min(open ? 300 : 280, (open ? 170 : 160) + dist * 0.42));
}

/** 动画跑着的那几帧里每帧补一次贴底（`followBottom` 自己合帧，多调无妨）。 */
let foldFollowRaf = 0;
let foldFollowUntil = 0;
function followThroughFold(ms: number): void {
  foldFollowUntil = Math.max(foldFollowUntil, performance.now() + ms);
  if (foldFollowRaf) return;
  const tick = (): void => {
    if (!follow.value || performance.now() > foldFollowUntil) { foldFollowRaf = 0; return; }
    followBottom();
    foldFollowRaf = requestAnimationFrame(tick);
  };
  foldFollowRaf = requestAnimationFrame(tick);
}

function fold(el: Element, open: boolean, done: () => void, pinPause = false): void {
  const node = el as HTMLElement;
  const dist = node.getBoundingClientRect().height;
  if (reduceMotion() || dist < 1) { done(); return; }
  const ms = foldMs(dist, open);
  if (pinPause) node.dataset.foldAnim = '1';      // 轨迹容器自己在折叠：别钉底（见 setRunBody）
  node.style.overflow = 'hidden';
  const anim = open
    ? node.animate(
      [
        { height: '0px', opacity: 0, offset: 0 },
        { height: `${dist * 0.78}px`, opacity: 1, offset: 0.72 },
        { height: `${dist}px`, opacity: 1, offset: 1 },
      ],
      { duration: ms, easing: FOLD_IN },
    )
    : node.animate(
      [
        { height: `${dist}px`, opacity: 1, offset: 0 },
        { height: `${dist * 0.42}px`, opacity: 0.3, offset: 0.62 },
        { height: '0px', opacity: 0, offset: 1 },
      ],
      { duration: ms, easing: FOLD_OUT },
    );
  followThroughFold(ms + 32);
  let settled = false;
  const settle = (): void => {
    if (settled) return;
    settled = true;
    node.style.overflow = '';
    if (pinPause) delete node.dataset.foldAnim;
    done();
  };
  anim.onfinish = settle;
  anim.oncancel = settle;
  /* `:css="false"` 的钩子没人兜底：浏览器把动画掐掉（切页、节点被替换）时 onfinish 未必来，
     这里再挂一道，免得残留 `overflow:hidden`。 */
  anim.onremove = settle;
}

function foldEnter(el: Element, done: () => void): void { fold(el, true, done); }
function foldLeave(el: Element, done: () => void): void { fold(el, false, done); }
function runEnter(el: Element, done: () => void): void { fold(el, true, done, true); }
function runLeave(el: Element, done: () => void): void { fold(el, false, done, true); }

function reduceMotion(): boolean {
  return typeof matchMedia === 'function' && matchMedia('(prefers-reduced-motion: reduce)').matches;
}

function itemClass(m: AgentMessage, item: TraceItem) {
  if (item.rows) return 'tool-group';
  const r = item.lead;
  if (!r) return '';
  const s = r.s;
  if (s.kind === 'text') return 'md run-text';
  if (s.kind === 'stage') return 'run-step stage';
  if (s.kind === 'thinking') {
    return ['run-step', 'think', {
      open: isItemOpen(m, item),
      autogrow: isItemOpen(m, item) && liveThinkingAt(m.message_id, r.i),
    }];
  }
  return ['run-step', 'tool', { running: s.status === 'running', bad: s.ok === false }];
}

/** 行内点击：整项开合（思考 / 工具组），正文段＝引用锚（与轨迹外的正文同一套）。 */
function onItemClick(m: AgentMessage, item: TraceItem, ev: MouseEvent) {
  if (item.rows) { toggleItem(m, item, ev); return; }
  const s = item.lead?.s;
  if (!s) return;
  if (s.kind === 'thinking') toggleItem(m, item, ev);
  else if (s.kind === 'text') onCitationClick(ev);
}

/**
 * 量「整段展开后有多高」：临时解除行夹读一次，读完立刻还原 —— 全在**同一帧内**，
 * 用户看不见中间态；但它给了收起动画一个真实的起点（`max-height` 只能是长度，`auto/none` 不过渡）。
 */
function measureOpenHeight(el: HTMLElement) {
  const prev = el.getAttribute('style');
  el.style.setProperty('display', 'block');
  el.style.setProperty('-webkit-line-clamp', 'unset');
  el.style.setProperty('max-height', 'none');
  const h = Math.max(el.scrollHeight, Math.round(el.getBoundingClientRect().height));
  if (prev === null) el.removeAttribute('style');
  else el.setAttribute('style', prev);
  return h;
}

/** 步骤行右侧的参数摘要：`query=随机旋转` → `随机旋转`（键名对用户没意义）。 */
/* ------------------------------ 工具步的实时反馈 ------------------------------ */

/**
 * 走秒：`running` 的步骤要看得见"还在跑"。
 *
 * 为什么需要它（用户口径 2026-12-06「模型调用工具时，实时显示效果」）：耗时以前要等工具
 * **返回之后**才有值，跑得久的那几步（看页面图、检索知识库）在界面上是**完全静止**的，
 * 看上去像卡住了。这里只在整轮进行中每 0.5 秒推进一个计数器，模板按 `startedAt` 现算。
 */
const tickMs = ref(0);
/* 不另起一个定时器：整轮本来就有 200ms 的跑表（见 `runWatch`），跟着它跳一格即可。 */
watch(elapsedMs, () => { tickMs.value = Date.now(); });;

/** 这一步的耗时（跑着就现算，跑完用后端给的 `ms`）。 */
function stepMs(s: AgentStep): number {
  if (s.status === 'running' && s.startedAt) return Math.max(0, tickMs.value - s.startedAt);
  return Number(s.ms || 0);
}

/**
 * 这一步要不要展示返回摘要：**失败一定展示**（原因比耗时有用），写操作也展示
 * （"改了什么"就是用户要的效果）；读工具的成功返回通常是一大段 JSON，展示它只是噪音。
 */
/**
 * 过程行上 `·` 后面那一截：**后端给的人话优先** —— `detail` 是结果一句话（「第 4 页 · 第 4 行
 * → 「…」」），`digest` 是参数一句话（「第 5 页」「「随机旋转」」）。
 *
 * 以前 `detail` 只在「失败 / 写操作」时显示、`digest` 只在 `running` 时显示，于是跑完之后每一行
 * 都只剩「取公式 ✓ 0.6 秒」——用户 2026-12-07 报「工具调用看不出在干什么」。现在两段都常显：
 * 跑着看参数，跑完看结果。
 */
const STEP_BRIEF_MAX = 72;

/** 参数在前、结果在后拼成一句（两段相同不重复；只有一段就只显示那一段）。 */
function stepText(s: AgentStep): string {
  const arg = fmtDigest(s.digest);
  const res = String(s.detail || '').trim();
  if (!arg) return res;
  if (!res || res === arg) return arg;
  return `${arg} · ${res}`;
}

function stepBrief(s: AgentStep): string {
  const text = stepText(s);
  return text.length > STEP_BRIEF_MAX ? `${text.slice(0, STEP_BRIEF_MAX)}…` : text;
}

/** 老数据兜底：`digest` 曾经是 `k=v、k=v`（剥掉键名）；**原始 JSON** 一律不显示。 */
function fmtDigest(digest?: string) {
  const text = String(digest || '').trim();
  if (!text || text.startsWith('{') || text.startsWith('[')) return '';
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

/** 往当前这一段追加（同类相邻的合并；换类另起一段——这就是"夹杂"的来源）。
 *
 *  ⚠️ **旁白（`stage`）不合并**：每一句是那一刻的状态，抬头只显示最新那条；合并了就变成
 *  "正在准备…正在阅读并组织回答…" 串成一句（实测过）。 */
function appendLiveStep(kind: AgentStep['kind'], text: string) {
  if (!text) return;
  const steps = ensureLive().run!.steps!;
  const last = steps[steps.length - 1];
  if (kind !== 'stage' && last && last.kind === kind) last.text = (last.text ?? '') + text;
  else steps.push({ kind, text, round: 0 });
}

/** 记下"这一段思考已经写完了"：把它从"自动展开的最后一段"里摘掉（键与手点共用一套）。 */
function markThinkingDone(m: AgentMessage) {
  const steps = stepsOf(m);
  for (let i = steps.length - 1; i >= 0; i -= 1) {
    if (steps[i].kind !== 'thinking') continue;
    const key = stepKey(m.message_id, i);
    if (!endedSteps.value.includes(key)) endedSteps.value = [...endedSteps.value, key];
    return;
  }
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

/* ---------------------- 对话读到哪儿了（按会话记） ----------------------
 * 长回答滚到一半，切走再回来被拽回底部，就找不回刚才那句了（用户口径 2026-11-26）。
 * 按**会话**记：每条对话各有自己的位置；还没成会话的「新对话」按论文记一份。
 * 只存一个 scrollTop，不碰正文内容（聊天记录始终只在服务端）。 */
function threadScrollKey(id: string | null = currentId.value): string {
  return `cylit:chat-scroll:${id || `new:${props.paperId}`}`;
}

/** 离底部够近了吗（跟随与圆钮共用同一个尺子）。 */
function isNearBottom() {
  const box = msgsEl.value;
  if (!box) return true;
  return box.scrollHeight - box.scrollTop - box.clientHeight <= BOTTOM_GAP;
}

/** 用户自己想往上翻：**立刻**退出跟随。
 *  比 scroll 事件更早也更准（scroll 只在"真的滚动了"之后才来，还得与程序滚动辨别）；
 *  滚轮向上、手指往下拖都先命中这里。已经贴底、没得翻时不动 —— 免得多浮出一枚圆钮。 */
function userScrollUp(force = false) {
  const box = msgsEl.value;
  /* 已经贴底、没得往上翻时不动（容差见 USER_UP_SLOP）：否则惯性收尾的那一帧回弹会把
     follow 打掉，圆钮跟着闪一下又消失。
     ~force~ 例外：滚轮落在**已经到顶**的块内滚动容器上（轨迹 ~.run-body~）时，
     这一下明确是"读者要继续往上翻"，哪怕外层还贴着底也要立刻退出跟随 ——
     不然内容一长高就被贴底逻辑拽回去，表现是"思维链到顶后往上翻带不动整页"。 */
  if (!force && box && box.scrollHeight - box.scrollTop - box.clientHeight <= USER_UP_SLOP) return;
  follow.value = false;
  userPulledUp = true;
  programTop = -1;                 // 人动手了：之前那点程序滚动不再享有豁免
  if (stickRaf) { cancelAnimationFrame(stickRaf); stickRaf = 0; }
}
/**
 * 事件目标往上找到**第一层能滚的容器**（止于 ~.msgs~ 自己）。
 * 轨迹 ~.run-body~ 就是一个限高滚动容器：滚轮落在它上面时，得先问它还能不能滚。
 */
function innerScrollerAt(target: EventTarget | null): HTMLElement | null {
  const box = msgsEl.value;
  let n = target as HTMLElement | null;
  while (n && n !== box && n instanceof HTMLElement) {
    const cs = getComputedStyle(n);
    if (/(auto|scroll)/.test(cs.overflowY) && n.scrollHeight - n.clientHeight > 1) return n;
    n = n.parentElement;
  }
  return null;
}

function onMsgsWheel(e: WheelEvent) {
  if (e.deltaY >= 0) return;
  /* 块内容器还能往上滚 → 这一下归它，别动跟随意愿（否则只是翻思维链就把"回到最新"点亮）。
     它已经到顶 → 强制判成"读者要往上翻"：滚轮接下来会链式带动外层，
     这里先把跟随松掉，才不会一边滚一边被贴底逻辑拽回去。 */
  const inner = innerScrollerAt(e.target);
  if (inner && inner.scrollTop > 0) return;
  userScrollUp(!!inner);
}
let touchY = 0;
function onMsgsTouchStart(e: TouchEvent) {
  touchY = e.touches[0]?.clientY ?? 0;
}
function onMsgsTouchMove(e: TouchEvent) {
  const y = e.touches[0]?.clientY ?? 0;
  if (y - touchY > 4) { userScrollUp(); touchY = y; }   // 手指往下 = 内容往上 = 上翻
}

/** 内容自己长高（增量落盘、思考 / 工具步骤、KaTeX 后渲染、折叠块展开）——
 *  跟随时跟着贴底。以前只有"正文增量"这一路会滚，思考段 / 工具步骤 / 后渲染全程不动，
 *  于是点了「回到最新」之后在思考阶段就等于没跟。 */
let msgsMO: MutationObserver | null = null;
watch(msgsEl, (el) => {
  msgsMO?.disconnect();
  msgsMO = null;
  if (!el) return;
  msgsMO = new MutationObserver(() => followBottom());
  msgsMO.observe(el, { childList: true, subtree: true, characterData: true });
});

let msgsSaveTimer = 0;
function onMsgsScroll() {
  const box = msgsEl.value;
  if (box) {
    /* 只有**用户造成的**滚动才改跟随意愿。
     *
     * ① 读者自己上翻过（`userPulledUp`）就一直是"不跟随"，直到他**真正**滚回最底
     *    （≤ USER_UP_SLOP）。以前这里按"离底 56px 内就恢复"，于是上翻一下立刻又被
     *    按回跟随：「回到最新」圆钮先出现再消失，看起来就是闪了一帧。
     * ② 程序自己滚的（贴底 / 回到最新 / 恢复位置）按**落点**放过：scrollTop 停在
     *    （或翻过）上次写下的落点，就还是我们贴着的那条底。内容长高时 scrollTop 原地
     *    不动，所以贴着底输出时一个 scroll 事件都不会被误判成上翻。
     * ③ 其余按离底多远判（读者自己滚回来 / 滚走了）。 */
    const dist = box.scrollHeight - box.scrollTop - box.clientHeight;
    if (userPulledUp && dist > USER_UP_SLOP) {
      follow.value = false;
    } else if (programTop >= 0 && box.scrollTop >= programTop - 2) {
      userPulledUp = false;
      follow.value = true;             // 停在（或翻过）程序落点 = 还贴着底
    } else {
      if (dist <= USER_UP_SLOP) userPulledUp = false;
      follow.value = isNearBottom();
    }
  }
  if (msgsSaveTimer) return;
  msgsSaveTimer = window.setTimeout(() => {
    msgsSaveTimer = 0;
    const el = msgsEl.value;
    if (!el) return;
    try { localStorage.setItem(threadScrollKey(), String(Math.round(el.scrollTop))); } catch { /* 隐私模式忽略 */ }
  }, 250);
}

/** 滚回上次读到的位置。返回 false = 没有记录（调用方照老规矩滚到底）。 */
function restoreThreadScroll(id: string | null): boolean {
  let y = 0;
  try {
    const raw = localStorage.getItem(threadScrollKey(id));
    if (!raw) return false;
    y = Number(raw);
  } catch { return false; }
  const el = msgsEl.value;
  if (!el || !Number.isFinite(y) || y <= 0) return false;
  /* 与 scrollToBottom 同一个理由：KaTeX / 图片 / 折叠块会在随后几帧继续把高度撑开，
     只滚一次会被后面的布局吃掉。 */
  const apply = () => {
    const m = msgsEl.value;
    if (!m) return;
    /* ⚠️ 这里**不登记** `programTop`：恢复的是"读者上次读到哪儿"，通常并不是底。
       一登记就会被 `onMsgsScroll` 的"落在落点上 = 还贴着底"认成正在跟随，
       下一批增量直接把读者从上次那一行拽到最底（用户报的"切回来被拽走"）。 */
    programTop = -1;
    m.scrollTop = y;
  };
  apply();
  void nextTick().then(() => { apply(); requestAnimationFrame(apply); });
  window.setTimeout(apply, 160);
  return true;
}

/**
 * "回到最新"：**恢复跟随**并滚到底。发消息、开 / 切会话、切分支、点圆钮走的都是这里 ——
 * 所以跟随的恢复只写在这一处，别处不再各维护一份状态。
 * 每帧的赋值都由 `stickToBottom` 登记落点，`onMsgsScroll` 便不会把它当成用户上翻。
 */
async function scrollToBottom() {
  follow.value = true;
  userPulledUp = false;               // 发消息 / 切会话 / 点圆钮：这一次是程序要把人拉回最底
  await nextTick();
  const el = msgsEl.value;
  if (!el) return;
  /* 连滚几帧：正文里的 KaTeX、图片、折叠块都会在随后几帧继续把高度撑开，
     只滚一次会停在半路（用户报的"进来不是停在底部"）。 */
  for (let i = 0; i < 3; i += 1) {
    if (!follow.value) return;          // 用户中途自己翻走了：别把人拽回来
    stickToBottom();
    await new Promise((done) => requestAnimationFrame(() => done(null)));
  }
  // 附件缩略图这类"加载完才知道高度"的，再补一次；用户中途自己翻走了就别拽回来
  window.setTimeout(() => {
    if (follow.value) { stickToBottom(); }
  }, 160);
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

/* ------------------- 上次看的是哪条会话（**按论文**记一份） -------------------
 * 阅读器切标签页 / 刷新都会重新挂载，currentId 回到 null、level 回到 list —— 用户看到的
 * 就是「刚聊的那条没了，又得在列表里重新找」（用户口径 2026-11-26：每页分别记、还要记
 * 打开的对话内容）。会话本来就只属于一篇论文（列表接口按 paper_id 过滤），所以按论文记。
 * 记的是 id + 当时在不在二级页：内容由 id 重新拉回来，聊天记录一条都不存在浏览器里。 */
function chatSessionKey(): string {
  return `cylit:chat-session:${props.paperId}`;
}

/** 认回上一次会话之前不要落盘：换论文那一瞬间的清空会把新论文的记录抹掉。 */
let sessionRestored = false;
/** 那次"认回上次会话"的拉取：自动发送要等它落地，否则这一轮会挂到一个刚被换掉的会话上。 */
let sessionReady: Promise<void> = Promise.resolve();

function persistChatSession() {
  if (!sessionRestored) return;
  try {
    localStorage.setItem(chatSessionKey(), JSON.stringify({
      id: currentId.value ?? '',
      thread: level.value === 'thread',
    }));
  } catch { /* 隐私模式忽略 */ }
}

watch([currentId, level], persistChatSession);

/** 把上一次看的那条会话拉回来（内容走 `loadConversation`，与手动点开同一条链路）。 */
async function restoreChatSession() {
  if (currentId.value) return;
  let id = '';
  let thread = false;
  try {
    const raw = localStorage.getItem(chatSessionKey());
    if (!raw) return;
    const v = JSON.parse(raw) as { id?: unknown; thread?: unknown };
    id = typeof v.id === 'string' ? v.id : '';
    thread = v.thread === true;
  } catch { return; }
  /* 上次停在列表：照着列表打开就行，不必为了一条高亮多拉一次会话正文 */
  if (!thread) return;
  if (!id) { level.value = 'thread'; return; }   // 上次停在「新对话」界面
  await openConversation(id);
}

async function loadConversation(id: string) {
  loading.value = true;
  try {
    const data = await api.get<AgentConversation>(`/agent/conversations/${id}`);
    currentId.value = data.conversation_id;
    messages.value = (data.messages as AgentMessage[]) ?? [];
    usage.value = data.usage ?? null;
    pick.value = {
      model_config_id: data.model_config_id || '',
      reasoning_effort: data.reasoning_effort || '',
    };
    editing.value = null;
    if (!conversations.value.some(c => c.conversation_id === id)) await loadConversations();
  } catch (err) {
    reportError(err, '读取会话失败');
  } finally {
    loading.value = false;
  }
  /* ⚠️ 滚动必须在 `loading=false` **之后**：加载中列表里只有"读取会话…"那一行，
     这时 `scrollHeight` 约等于 0，滚了也白滚（进来停在顶部就是这么来的）。 */
  /* 换了一条对话：上一条留下的"读者上翻过"不能带过来 —— 否则这条会话一路都不跟随，
     圆钮常驻、怎么点「回到最新」都治不好（`userPulledUp` 是读者意图，只对当前这条有效）。 */
  userPulledUp = false;
  if (!restoreThreadScroll(id)) await scrollToBottom();
  /* 跟随与否按**落点**算一次：落到底（或本来就没记录）＝继续跟随；
     落在半截＝在读历史，别让下一批增量把人拽走（那是另一个老问题）。 */
  else follow.value = isNearBottom();
}

async function openConversation(id: string) {
  level.value = 'thread';
  if (id === currentId.value && messages.value.length) {
    await scrollToBottom();      // 同一个会话再点一次：也回到最新那一轮
    return;
  }
  // 换了一条对话：上一条对话留下的锚点/色点不能挂到这条的回答下面
  lastAnchor.value = null;
  anchorAnswerId.value = '';
  await loadConversation(id);
}

function startNew() {
  level.value = 'thread';
  currentId.value = null;
  messages.value = [];
  usage.value = null;
  pick.value = { model_config_id: '', reasoning_effort: '' };
  chatError.value = null;
  editing.value = null;
  pending.value = [];
  draft.value = '';
  lastAnchor.value = null;              // 新对话：上一条对话的划词锚点与色点一起清掉
  anchorAnswerId.value = '';
  follow.value = true;                  // 新对话没有历史可翻：跟随与"读者上翻过"一起归零
  userPulledUp = false;
  rollCopy();
  void nextTick(() => { resetInputHeight(); inputEl.value?.focus(); });
}

/**
 * **正在生成的会话**（用户口径 2026-11-28 十六轮）：点返回回到列表时，那一轮**继续跑**，
 * 列表里它的左边转一个圈，跑完自己消失。
 *
 * 为什么不用 `sending`：`sending` 只代表"当前这个面板在跑"，而列表要的是"这一条会话在跑" ——
 * 用户可能在列表里切到别的对话去看，那时 `sending` 会被新的一轮覆盖。
 * 所以按 conversation_id 记一份集合，`ask()` 开始时加、结束时删。 */
const runningIds = ref<string[]>([]);
/** 这一轮跑在哪个会话上（新会话的 id 是服务端建的，`watch(currentId)` 里补记） */
let runningId = '';
const isRunning = (id: string) => runningIds.value.includes(id);
/* 新会话：`currentId` 在本轮跑到一半才有值 —— 那也照样算"它在跑" */
watch(currentId, (id) => {
  if (!sending.value || !id) return;
  runningId = id;
  markRunning(id);
});
function markRunning(id: string) {
  if (id && !runningIds.value.includes(id)) runningIds.value = [...runningIds.value, id];
}
function unmarkRunning(id: string) {
  if (id) runningIds.value = runningIds.value.filter((x) => x !== id);
}

function backToList() {
  /* **不中止**正在跑的那一轮：返回只是换个视角，跑完的消息照样会落库、列表里的转圈也会自己收。 */
  level.value = 'list';
  void loadConversations();
}

/**
 * 确保**有会话开着**（用户口径 2026-11-19：右键「加入对话」时如果没打开任何对话，就用新对话）。
 *
 * 外面（`ReaderView`）把选区/图片塞进来之前调一次：停在会话列表那一级的话，输入框与上下文胶囊
 * 根本不在 DOM 里，塞进去等于"点了没反应"。已经在某一轮对话里就**不动**（不打断当前会话）。
 */
function ensureThread() {
  if (level.value === 'thread') return;
  startNew();
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

/** 会话行右上角那枚「⋯」展开了哪一条（同一时刻只开一个） */
const rowMenu = ref<string>('');
/** 右键会话行时的鼠标点（`ContextMenu` 按这个点从**右下角**展开）。 */
const convCtx = ref<{ x: number; y: number } | null>(null);
/** ⚠️ 被右键的**那一条**必须单独存：`ContextMenu.pick()` 是先 `emit('close')` 再 `emit('select')`（它要先收菜单
 *  再做动作），所以 `@close` 一旦把上下文清掉，`select` 那一侧就拿到 null —— 表现是「菜单弹出来、点项没反应」。 */
const convCtxTarget = ref<AgentConversation | null>(null);
/** 右键菜单的项：与「⋯」那一枚**同一套动作**，顺序也一致（危险项单独一组，靠 `sep` 隔开）。 */
const convCtxItems = computed(() => [
  { key: 'rename', label: '重命名', icon: 'edit' },
  ...(showFloatBtn.value ? [{ key: 'float', label: '浮层打开', icon: 'pip' }] : []),
  { key: 'pick', label: '多选', icon: 'list-check' },
  { key: 'del', label: '删除', icon: 'trash', danger: true, sep: true },
]);

function openConvCtx(ev: MouseEvent, conv: AgentConversation) {
  rowMenu.value = '';                    // 两种入口别叠着（「⋯」那枚在鼠标下方，右键时先收掉）
  convCtxTarget.value = conv;
  convCtx.value = { x: ev.clientX, y: ev.clientY };
}

function onConvCtx(key: string) {
  const conv = convCtxTarget.value;
  convCtx.value = null;
  convCtxTarget.value = null;
  if (!conv) return;
  if (key === 'rename') startRename(conv);
  else if (key === 'float') void shareToFloat(conv);
  else if (key === 'pick') toggleBatch();
  else if (key === 'del') askDeleteOne(conv);
}
/** 菜单浮层的坐标（`position: fixed`，见 `placeRowMenu`）：只在打开时算一次 */
const rowMenuStyle = ref<Record<string, string>>({});
const rowMenuEl = ref<HTMLElement | null>(null);
let rowMenuAnchor: HTMLElement | null = null;

/**
 * 把「⋯」菜单摆到**这一行的右侧**，并**避让视口底边**（用户口径 2026-12-27：
 * 「侧栏对话的更多菜单放在右侧悬浮显示且避让底部」——以前是 `top: 100%` 往下展开，
 * 列表滚到底时最后几条的菜单直接被裁掉/盖住下面的行）。
 *
 * 做法：纵向与这一行居中，超出视口就往上顶到 `内高 - 菜单高 - 8`；
 * 横向先放右侧 `row.right + 8`，放不下就翻到左侧（侧栏贴着屏幕右边时也不出界）。
 * 用 `position: fixed` + `Teleport to=body`：列表是 `overflow-y:auto`，
 * 留在行里的绝对定位会被列表容器裁掉，这正是"菜单被底部吃掉"的成因。
 */
/** 函数式 ref：菜单在 v-for 里（字符串 ref 会变成数组，取 getBoundingClientRect 会报错） */
function setRowMenuEl(el: unknown) {
  rowMenuEl.value = (el as HTMLElement | null) ?? null;
}

function placeRowMenu() {
  const el = rowMenuEl.value;
  const anchor = rowMenuAnchor;
  if (!el || !anchor) return;
  const a = anchor.getBoundingClientRect();
  /* 用 **布局盒**（offsetWidth/Height）而不是 `getBoundingClientRect()`：后者包含进了淡入时的
     `transform: scale(.98)`，量出来比真实盒子窄 2~3px —— 于是「右对齐到按钮右边」会偏出几像素。 */
  const m = { width: el.offsetWidth, height: el.offsetHeight };
  /* 用户口径 2026-12-27：「**优先右下角弹出**，避让底部」。
     ① 横向**右对齐**到这一枚「⋯」的右边（= 从右下角往外长）；② 纵向优先挂在它下面；
     ③ 下方放不下就翻到上边；④ 最后两个方向都夹进视口（`pad` = 离屏幕边的最小距离）。
     旧实现把菜单在这一行上**垂直居中**、再挂到按钮右侧 —— 那是「右侧中部」，不是右下角。 */
  const pad = 8;
  const gap = 4;
  /* **右下角**优先：先按「左边缘贴按钮右边」算，右边放不下才翻到按钮左侧
     （用户口径 2026-12-27「更多菜单优先朝着右下角展开（可以跨越侧栏）」）。 */
  let left = a.right + gap;
  if (left + m.width > window.innerWidth - pad) left = a.left - gap - m.width;
  let top = a.bottom + gap;
  if (top + m.height > window.innerHeight - pad) top = a.top - gap - m.height;
  left = Math.max(pad, Math.min(left, window.innerWidth - m.width - pad));
  top = Math.max(pad, Math.min(top, window.innerHeight - m.height - pad));
  rowMenuStyle.value = { left: `${Math.round(left)}px`, top: `${Math.round(top)}px` };
}

/** 菜单开着时：点外面、滚列表、改窗口大小都收起来（浮层在 body 上，不跟着列表走） */
function onDocPointerDown(ev: Event) {
  const t = ev.target as HTMLElement | null;
  if (t?.closest('.row-menu-fly') || t?.closest('[data-conv-more]')) return;
  rowMenu.value = '';
}
function closeRowMenuOnMove() { rowMenu.value = ''; }
watch(rowMenu, (open) => {
  const on = typeof window !== 'undefined';
  if (open && on) {
    window.addEventListener('pointerdown', onDocPointerDown, true);
    window.addEventListener('resize', closeRowMenuOnMove);
    document.addEventListener('scroll', closeRowMenuOnMove, true);
  } else {
    window.removeEventListener('pointerdown', onDocPointerDown, true);
    window.removeEventListener('resize', closeRowMenuOnMove);
    document.removeEventListener('scroll', closeRowMenuOnMove, true);
  }
});
onUnmounted(() => {
  window.removeEventListener('pointerdown', onDocPointerDown, true);
  window.removeEventListener('resize', closeRowMenuOnMove);
  document.removeEventListener('scroll', closeRowMenuOnMove, true);
});

/** 点「⋯」：同一行再点一次收起；换一行则重新算坐标 */
function toggleRowMenu(id: string, ev: MouseEvent) {
  if (rowMenu.value === id) { rowMenu.value = ''; return; }
  /* 锚点就是**这一枚「⋯」按钮本身**（用户口径 2026-12-27「没有紧挨着更多按钮」）：
     挂到整行上时，行的右边比按钮宽 3px、行底比按钮低十几像素 —— 菜单看起来就离按钮很远。 */
  rowMenuAnchor = ev.currentTarget as HTMLElement | null;
  rowMenu.value = id;
  rowMenuStyle.value = {};
  void nextTick(placeRowMenu);
}

/** 浮层打开 = **把这个会话拎成浮窗**（`⋯` 菜单里那一枚；不是"分享"） */
async function shareToFloat(conv: AgentConversation) {
  rowMenu.value = '';
  await openConversation(conv.conversation_id);
  emit('float');
}

/** 气泡下面那枚时间（只有时分；悬停给完整时间的 tooltip，与列表里的相对时间同一口径） */
function msgTime(m: AgentMessage): string {
  const raw = m.created_at;
  if (!raw) return '';
  const d = new Date(raw);
  if (Number.isNaN(d.getTime())) return '';
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
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
    body: '对话和它的全部消息都会删掉，不可恢复',
    target: conv.title || '未命名对话',
  };
}

function askDeletePicked() {
  if (!picked.value.length) return;
  confirming.value = {
    ids: [...picked.value],
    body: '选中的对话和它们的全部消息都会删掉，不可恢复',
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
    /* 删完**不跳新对话**（用户口径 2026-12-27；上一版理解反了，做成了「总是开一条新对话」）：
       删的正好是当前打开的那条 → 清空这一轮、**回到列表**；删的是别条 → 原地不动。
       新对话只由「＋ 新对话」（`startNew`）或直接发问产生，删除不该顺手造一个空草稿。
       为什么要顺手清 currentId / messages：不清的话列表回来时右侧还挂着已删会话的消息与用量。 */
    if (currentId.value && job.ids.includes(currentId.value)) {
      currentId.value = null;
      messages.value = [];
      usage.value = null;
      draft.value = '';
      lastAnchor.value = null;
      anchorAnswerId.value = '';
      backToList();
    }
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

/** 这一轮**最近一次过程事件**（思考续写 / 调工具 / 半句被收回）的时刻 —— 见 `foldRunSoon`。 */
let lastProcessAt = 0;
/** 正等着自动收起的那条轨迹（空 = 没在等）。 */
let pendingFold = '';
let foldRunTimer = 0;

/** 轨迹跟着"过程"展开（用户手点过这一轮就不再自动改）。 */
function keepRunOpen(m: AgentMessage) {
  lastProcessAt = performance.now();
  if (runTouched.has(m.message_id)) return;
  openRun(m.message_id);
}

/**
 * **思维链写完就收起**（用户口径 2026-12-08：「思维链结束时自动折叠，而不是等正文输出完成」）。
 *
 * 判定很朴素：正文开始写 = 过程结束。但不能立刻收 ——「说半句 → 又调工具 → 再说」是常见形态
 * （后端此时会推 `answer_reset` 把半句清掉），所以压 320ms：
 *   · 这段时间里只要又冒出过程事件（`keepRunOpen` 记下 `lastProcessAt`），就再等一轮；
 *   · 一收到下一段正文（`answer_delta` 还在来）就照样收 —— 所以**不要**在每次 delta 上重置计时，
 *     否则字一直在出、轨道就永远不收（这里只认第一次）。
 * 阶段提示（`stage`，比如"正在阅读并组织回答…"）**不算**过程事件：它是旁白，正文一起来就
 * 不该再把轨迹重新摊开 —— 上一版正是被这条旁白卡住、收不下去。
 */
function foldRunSoon(m: AgentMessage) {
  if (runTouched.has(m.message_id)) return;
  if (pendingFold === m.message_id) return;
  pendingFold = m.message_id;
  armFold();
}

function armFold(): void {
  if (foldRunTimer) clearTimeout(foldRunTimer);
  foldRunTimer = window.setTimeout(() => {
    foldRunTimer = 0;
    const id = pendingFold;
    if (!id) return;
    if (runTouched.has(id)) { pendingFold = ''; return; }        // 人接手了，别自作主张
    if (performance.now() - lastProcessAt < 400) { armFold(); return; }
    pendingFold = '';
    closeRun(id);
  }, 320);
}

function onEvent(name: string, data: any) {
  if (name === 'session') {
    if (data?.conversation_id) currentId.value = data.conversation_id;
    return;
  }
  if (name === 'stage') {
    appendLiveStep('stage', data?.message || '');
    return;                       // 旁白：不动轨迹的开合（见 foldRunSoon 的说明）
  }
  if (name === 'tool') {
    const live = ensureLive();
    keepRunOpen(live);
    const steps = live.run!.steps!;
    if (data?.status === 'start') {
      steps.push({ kind: 'tool', round: Number(data?.round || 0), tool: data?.tool ?? '',
                   digest: String(data?.args_digest || ''), status: 'running',
                   startedAt: Date.now() });
    } else {
      const open = [...steps].reverse()
        .find(s => s.kind === 'tool' && s.tool === data?.tool && s.status === 'running');
      const ms = Number(data?.ms || 0);
      if (open) {
        open.status = 'done';
        open.ok = data?.ok !== false;
        open.detail = String(data?.detail || '');
        open.ms = ms || (open.startedAt ? Date.now() - open.startedAt : null);
      }
      /* agent 写了笔记 / 高亮 / 修订产物：**当帧就通知父组件刷新**，不必等整轮跑完、更不必刷新
         页面（用户口径 2026-11-26：agent 加的笔记要实时显示）。
         ⚠️ 判的是**工具名**（`data.tool`）：以前写成 `String(name)`，而 `name` 是事件名 `"tool"`，
         正则永远不成立 —— 于是这条刷新从来没发生过（2026-12-06 修）。 */
      if (WRITE_TOOL.test(String(data?.tool || ''))) emit('refresh-side', String(data?.tool || ''));
    }
    return;
  }
  if (name === 'thinking_delta') {
    appendLiveStep('thinking', data?.text ?? '');
    keepRunOpen(ensureLive());
    return;
  }
  /* 思考段**写完了**：后端在正文首字到来前推一条 `status: done`。收到就立刻把这段折叠起来
     （用户口径 2026-12-08）—— 以前这里直接 return、靠"下一步骤出现"顺带收，
     而清洗器扣着尾巴时"思考写完 → 正文首字"之间能有几百毫秒，那段时间思维链还摊着。 */
  if (name === 'thinking') {
    if (liveId.value) markThinkingDone(ensureLive());
    return;
  }
  /* 调工具了：之前那几句是过程（『我先看一下…』），不算正文 —— 清掉已显示的那一段，
     与后端 `state.text` 同一口径（用户口径 2026-11-26）。过程轨迹里仍然留着。 */
  if (name === 'answer_reset') {
    dropDelta();
    const live = ensureLive();
    live.content = '';
    keepRunOpen(live);          // 半句话被收回去了 = 又要接着干过程，轨迹留着别收
    return;
  }
  if (name === 'answer_delta') {
    /* 进缓冲，最多每 66ms 落一次盘（见 DELTA_FLUSH_MS 的说明） */
    const live = ensureLive();
    pushDelta(live, data?.text ?? '');
    /* 开始写正文 = 过程结束了 → 把整条思维链收起来（用户口径 2026-12-08：
       **思维链结束时折叠，不是等正文输出完**）。 */
    if (String(data?.text ?? '').trim()) foldRunSoon(live);
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
    dropDelta();
    emit('refresh-side');
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
    // 终态帧：整份原因（kind / HTTP 状态 / 上游码 / 限额快照）都交给错误卡，不再只留一句 message
    chatError.value = errorViewFromFrame(data);
    return;
  }
}

const TOOL_LABELS: Record<string, string> = {
  paper_search: '正在这篇论文里检索', kb_search: '正在整个知识库里检索',
  page_markdown_get: '正在读正文', block_get: '正在展开这一段',
  page_image_get: '正在看页面图', figure_list: '正在看插图清单',
  figure_image_get: '正在看插图', formula_get: '正在取公式',
  translate_get: '正在取译文', library_list: '正在翻书架', notes_search: '正在查你的笔记',
  paper_archive_get: '正在看背景档案',
  page_markdown_edit: '正在修订产物',
  highlight_manage: '正在标高亮', note_manage: '正在写笔记',
  web_search: '正在联网搜索', web_fetch: '正在读网页',
};
const toolLabel = (name: string) => TOOL_LABELS[name] ?? `正在调用 ${name}`;

/**
 * 把阅读器状态并进请求体 —— **只在它非空时**。
 *
 * 为什么空就不塞：后端把"没带 `reader`"和"带了却一个字段都认不出来"同等看待
 * （`ReaderState.available=False`），但不带这个 key 时整条请求必须与从前逐字节一致，
 * 所以空对象不能发出去（`{"reader":{}}` 也是一个 key）。
 */
function withReaderState(
  body: Record<string, unknown>, state: typeof props.readerState,
): Record<string, unknown> {
  if (!state) return body;
  const reader = Object.fromEntries(Object.entries(state)
    .filter(([, v]) => v !== undefined && v !== null && v !== ''));
  if (!Object.keys(reader).length) return body;
  return { ...body, reader };
}

/**
 * 把本地消息列表**截到 `target` 这一条之前**（就地改提问 / 就地重答用）。
 *
 * 为什么本地先截：服务端要等 `done` 才给权威结果，在那之前列表里还是上一轮的旧问答 ——
 * 而新一轮**已经开始逐字输出了**，旧回答和它之后的问答仍旧挂在屏幕上，看起来就是
 * "编辑并重答了，旧的对话内容没删"（用户在生产上点出来的）。更显眼的是上一轮那段
 * 逐字浮现的动画还在跑的时候：新回答在下面长字，旧的一整段还亮着。
 *
 * 截断**只为了"立刻看不见旧的"**，对齐仍然交给 `done` 之后那次 `loadConversation()`
 * （`done` 才是权威结果，见文件头的三条数据纪律）—— 所以这里不猜服务端会留下哪几条。
 *
 * 顺带清掉与**被删那几条**有关的临时态：轨迹展开（`openRuns` 的键就是消息 id）、
 * 正在生成的那条占位（`liveId`），以及"逐字出现"的载入动画计数 —— 那些节点的 DOM 已经
 * 没了，计数却还指着它们，不清会让下一条回答的第一批增量不淡入（`resetReveal`）。
 */
function truncateAt(target?: AgentMessage): void {
  const list = messages.value;
  const cut = target ? list.findIndex(m => m.message_id === target.message_id) : -1;
  if (cut < 0) return;                       // 找不到落点（列表刚被重拉过）：一条都不删
  const dropped = new Set(list.slice(cut).map(m => m.message_id));
  messages.value = list.slice(0, cut);
  if (openRuns.value.some(id => dropped.has(id))) {
    openRuns.value = openRuns.value.filter(id => !dropped.has(id));
  }
  if (liveId.value && dropped.has(liveId.value)) {
    liveId.value = '';
    /* 没落盘的那半个 delta 是写给已删那条的：留着的话定时器一到就 `ensureLive()`
       变出一条**空 id** 的占位消息来。 */
    dropDelta();
  }
  resetReveal();
}

/**
 * 一轮问答。三种入口共用一条流：
 * - `send`：正常提问（可带选区 / 图片）；
 * - `regen`：重新生成某条回答（**默认就地**：替换这条回答；`branch=true` 才挂兄弟）；
 * - `edit`：改掉某条提问（**默认就地**：改写正文并作废它之后的问答；`branch=true` 走兄弟分支）。
 */
async function ask(mode: 'send' | 'regen' | 'edit' = 'send', target?: AgentMessage,
                   branch = false) {
  /* 这一轮是不是「解释」：后端据此套用设置里「Cyli 解释」那份模型 + 思考强度。
     **拿到就复位**（意图是一次性的）：否则服务没起来 / 正在跑那一轮被挡回来之后，
     下一次普通提问也会被当成解释、跑到解释那份配置上去。 */
  const explain = explainIntent.value;
  explainIntent.value = false;
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

  chatError.value = null;
  sending.value = true;
  runningId = currentId.value ?? '';    // 新会话此刻还没有 id，watch(currentId) 会补记
  markRunning(runningId);
  elapsedMs.value = 0;
  startTimer();
  /* 就地改提问 / 就地重答：**发请求之前**先把本地列表截到落点。
     用户口径（生产上点出来的）：「编辑并重答时，旧的对话内容没有删除」—— 以前这里
     一条都不删，新一轮已经逐字在跑，旧回答**和它之后的所有问答**还挂在屏幕上，
     要等 `done` 之后 `loadConversation()` 重拉才消失。
     - `edit`：丢掉被编辑那条提问**及其之后**的全部（改完的提问下面不该还留着旧回答）；
     - `regen`：保留到 `target` 之前，丢掉 `target` 及其之后（就地重答 = 只换这一条）；
     - `branch=true`：**一个字都不截** —— 兄弟分支要两条都留着（见 `startEdit` / `branchFrom`）。 */
  if (mode !== 'send' && !branch) truncateAt(target);
  const optimistic: AgentMessage | null = mode === 'send' ? {
    message_id: `local-${Date.now()}`, conversation_id: currentId.value ?? '', seq: 0,
    role: 'user',
    // 与后端 `api/agent.py` 落库时**同一个格式**（`> 〔第 4 页〕引用…`）：
    // 位置是消息的一部分，刷新前后必须是同一句话。
    content: selection
      ? `> ${selection.where ? `〔${selection.where}〕` : ''}${selection.text.slice(0, 400)}\n\n${question || '解释这段内容'}`
      : question,
    attachments: pending.value,
  } : mode === 'edit' ? {
    /* 改完的提问**立刻**顶到被删掉那一段的位置上（形状与 send 那条乐观消息逐字相同）：
       不插的话，截断之后要等到 `done` 重拉才看得见自己刚写的那句话 —— 屏幕上一片空白，
       看着像"提问丢了"。`role: 'user'` 给它同样的气泡与同样的进场动画。 */
    message_id: `local-${Date.now()}`, conversation_id: currentId.value ?? '', seq: 0,
    role: 'user',
    content: question,
  } : null;
  if (optimistic) {
    messages.value = [...messages.value, optimistic];
    markFresh(optimistic.message_id);
    if (mode === 'send') {
      /* 输入框 → 气泡：整块飞过去（400ms glide）。量的是发送瞬间的输入框，
         落点是刚插进来的那条用户消息；draft 还没清，量到的是用户看到的那个高度。 */
      void flyMessage(
        composerEl.value?.querySelector('.composer-box') as HTMLElement | null,
        optimistic.content,
        () => msgsEl.value?.querySelector('.msg[data-fresh="user"] .body'),
      );
      draft.value = '';
      /* 发出去就**立刻**收回一行：`finally` 那次复位要等整轮回答结束（长问答可能几十秒），
         在那之前输入框一直停在上一次的多行高度，看着像没发出去（用户 2026-12 报的）。 */
      void nextTick(resetInputHeight);
    }
  }
  // 这一轮助手消息的本地占位：**必须排在提问之后**（先建它会挂在用户气泡上面 ——
  // 用户 2026-10-03 报过"思考的抬头跑到我问的问题上面去了"）。
  liveId.value = `local-assistant-${Date.now()}`;
  ensureLive();
  markFresh(liveId.value);
  resetReveal();                                        // 新一轮：正文载入动画从头播
  openRuns.value = [...openRuns.value, liveId.value];   // 跑着的那一轮默认展开（可点收起）
  /* 起手不再塞一句"正在准备…"（用户口径 2026-12-09：这类旁白整句删掉）。
     抬头那句"正在思考… · 0.3 秒"由 `runTitle` 自己给，见 `showRun`：一步都没有时抬头照样在。 */
  if (mode === 'send') {
    /* 这一轮带过选区就把锚点存下来：回答下面那排色点要用它建高亮 / 存笔记。
       **同一对话里的追问（这一轮不带选区）不动它** —— 色点钉在第一轮那条回答下面，
       用户问完追问还想回去存第一轮的解释；换对话 / 开新对话时才随锚点一起清掉。 */
    const anchor = selected.value;
    if (anchor?.text) {
      lastAnchor.value = {
        text: anchor.text, quote: anchor.quote || anchor.text,
        row_id: anchor.row_id ?? null, page: anchor.page ?? null,
        where: anchor.where || '',
        start: anchor.start ?? 0, end: anchor.end ?? 0,
        /* 面**缺失就留空**（交给后端按文字语言判）：这里以前兜底成 `'en'`，等于替用户
           选了原文面 —— 中文引文于是存进 `en`，译文面上永远画不出来（用户 2026-12 复报）。 */
        side: anchor.side || '', scope: anchor.scope || 'row',
        highlightId: '', color: '',
      };
      anchorAnswerId.value = '';        // 新锚点：等这一轮的回答落库再钉上去
    }
    // 发出去之后胶囊就完成使命了（选区已经进了这条消息的 `> 引用`）
    selCtx.value = null;
    emit('clear-selection');
    pending.value = [];
  }
  if (mode === 'edit') editing.value = null;
  await scrollToBottom();

  controller = new AbortController();
  let replied = false;                  // 这一轮真的拿到了落库结果（中止 / 失败时不算）
  try {
    /* 请求体**按需装配**：`mode: "explain"` 只有在 body 里**没有** `model_id` /
       `reasoning_effort` 这两个键时才会套用设置里那份解释配置（见 `backend/api/agent.py`）——
       所以这两个字段改成"用户真选了才出现"，不再用 `|| null` 占位把解释配置顶掉。 */
    const body: Record<string, unknown> = {
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
    };
    if (explain) body.mode = 'explain';
    if (pick.value.model_config_id) body.model_id = pick.value.model_config_id;
    if (pick.value.reasoning_effort) body.reasoning_effort = pick.value.reasoning_effort;
    /* 阅读状态**只在这一刻并进去**：它随每页/每面变，而请求体是一次性的。
       空（页数据还没到）时这块原样返回，老请求逐字节不变。 */
    const finalBody = withReaderState(body, props.readerState);
    await postSse('/agent/ask', finalBody, { signal: controller.signal, onEvent });
    const convId = currentId.value;
    if (convId) {
      await loadConversation(convId);            // `done` 是权威结果：重拉一次对齐
      await loadConversations();
      replied = true;
    }
    /* 把色点钉到这一轮的回答上：新锚点那一轮钉新回答；被钉的那条没了（就地重答换了 id）
       或重答的正是它，就改钉新的那条。追问那一轮不会动它 —— 这正是"只留在第一轮"的意思。 */
    const pinned = anchorAnswerId.value;
    const pinnedGone = Boolean(pinned) && !messages.value.some(m => m.message_id === pinned);
    if (lastAnchor.value
        && (!pinned || pinnedGone || (target?.message_id === pinned))) {
      anchorAnswerId.value = lastAnswerId();
    }
  } catch (err: any) {
    if (err?.name !== 'AbortError') {
      const view = errorViewFromApiError(err);
      chatError.value = view;
      /* 卡片已经把原因、状态码、上游原话都摆在对话里了；只有"没分类"那种（`unknown`）才再补一条
         toast —— 其余种类两边都报一遍只会让人以为出了两个错。 */
      if (view.kind === 'unknown') reportError(err, '提问失败');
    }
    // 失败时把乐观回显撤掉（后端没落库，留着会与刷新后的结果不一致）
    if (optimistic) messages.value = messages.value.filter(m => m.message_id !== optimistic.message_id);
  } finally {
    sending.value = false;
    unmarkRunning(runningId);
    unmarkRunning(currentId.value ?? '');
    runningId = '';
    /* 跑完时用户可能正停在列表页：刷一次让条数 / 时间跟着更新 */
    if (level.value === 'list') void loadConversations();
    stopTimer();
    // 占位消息撤掉：成功后 `loadConversation` 已经把它换成了落库那条；
    // 失败/中止时它没有对应的库记录，留着会与刷新后的结果不一致。
    messages.value = messages.value.filter(m => m.message_id !== liveId.value);
    settleRun(liveId.value, replied);          // 跑完自动收起（点过的按用户的选择）
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

/* --------------------------- 存为笔记（解释那一轮的回答） --------------------------- */

/** 那排色点用哪几色：**与划词胶囊/右键菜单同一份色板**（`HL_SWATCHES`，「多巴胺」8 色），
 *  色块本身沿用 `base.css` 的 `.hl-swatch`，颜色由行内 `--hl-sw` 给。 */
const NOTE_COLORS = HL_SWATCHES;

/** 最后一条助手消息的 id（不经过 `sending` 判断：锚点要在这一轮刚落库时钉上去）。 */
function lastAnswerId() {
  const m = [...messages.value].reverse().find(x => x.role === 'assistant');
  return m?.message_id ?? '';
}

/**
 * 划词那一轮落库后那条回答的 id —— 那排色点**只钉在它下面**。
 *
 * 为什么记 id 而不跟着"最新那条回答"走：锚点会一直留在这条会话里（答完追问两句还想回去存），
 * 若色点挂在最新回答下，后面每一轮问答的末尾都会冒出一排"存为笔记"（用户报过这个问题）。
 */
const anchorAnswerId = ref('');

/** 划词那一轮的回答：存笔记写进去的是**这条**的正文，不是最新那条。 */
const anchorAnswerMsg = computed(
  () => messages.value.find(m => m.message_id === anchorAnswerId.value) ?? null);

/** 这条消息下面要不要出那排色点：**只有划词那一轮的回答**下面才有东西可标、可存。 */
function canSaveAsNote(m: AgentMessage) {
  return Boolean(lastAnchor.value) && Boolean(anchorAnswerId.value)
    && m.message_id === anchorAnswerId.value;
}

/**
 * 点色点 = 给这处选区建/改高亮，并把**这条回答**存成它的笔记。
 *
 * 语义与原「Cyli 解释」浮层底部那排色块同源（用户口径 2026-10-07：把"先标高亮、再回来存笔记"
 * 合成一步 —— 挑个颜色就完了），只是壳换成了对话面板：
 * 已有高亮就**改它的颜色**（`PUT /highlights/{id}`），没有才按 `scope` 选锚点新建
 * （正文行 = 行 id / Markdown = `md:p<页>` / 原 PDF = `pdf:p<页>`）。
 */
async function saveAnswerWithColor(color: string) {
  const anchor = lastAnchor.value;
  const answer = (anchorAnswerMsg.value ? finalAnswer(anchorAnswerMsg.value) : '').trim();
  // 回答为空就**明说**，绝不拿选区文字凑一条半截笔记（笔记正文必须是这条回答）
  if (!anchor || !answer) { toast('这条回答还没有正文，没有可存的笔记', 'warn', 2800); return; }
  const scope = anchor.scope || 'row';
  const page = anchor.page ?? 0;
  const blockId = scope === 'markdown' ? `md:p${page}`
    : scope === 'pdf' ? `pdf:p${page}` : (anchor.row_id || '');
  const anchored = scope === 'markdown' || scope === 'pdf'
    ? (page > 0 && anchor.end > anchor.start)
    : Boolean(anchor.row_id);
  if (!anchor.highlightId && !anchored) {
    toast('这段文字定位不到（标不了高亮）', 'warn', 2600);
    return;
  }
  /* 偏移量缺失（老入口 / 只带了原文）时**按整段标注**：`markRanges` 只画 `end > start` 的，
     写一条 0/0 的高亮等于库里有了、正文上永远看不见（用户 2026-10-23 报的「没挂上去」）。
     降级要在界面上说清，不许静默。 */
  let start = anchor.start;
  let end = anchor.end;
  let degraded = false;
  if (!(end > start)) {
    const quoteText = String(anchor.quote || anchor.text || '');
    if (!quoteText) { toast('这段文字定位不到（标不了高亮）', 'warn', 2600); return; }
    start = 0;
    /* 长度要按**渲染坐标系**算（公式 1 个字符）：`quote` 里是 LaTeX（`$…$` / `$$…$$`），
       直接取 `.length` 会比 DOM 里那一段长出一大截 —— 高亮会顺延到后面几句上。
       与划词那边（`ReaderView` 的 `sliceWithLatex`）同一个口径。 */
    end = renderedLength(quoteText);
    degraded = true;
  }
  try {
    let highlightId = anchor.highlightId;
    if (highlightId) {
      await api.put(`/highlights/${highlightId}`, { color });
    } else {
      const created = await api.post<Highlight>('/highlights', {
        paper_id: props.paperId, block_id: blockId,
        selected_text: anchor.quote || anchor.text,
        start_offset: start, end_offset: end,
        color, side: anchor.side, scope,
      });
      highlightId = created?.id ?? '';
      anchor.highlightId = highlightId;
    }
    if (!highlightId) { toast('高亮没建成，笔记也就没存', 'warn', 2600); return; }
    /* 同一处高亮已经有笔记就更新它（用户可以再解释一次、再点一次颜色） */
    const listed = await api.get<{ items: Note[] }>('/notes', { paper_id: props.paperId });
    const existing = (listed.items ?? []).find((n) => n.highlight_id === highlightId) ?? null;
    /* 标号跳转信息一起存（见 `utils/noteRefs`）：回答正文里的 `[1]`/`A045` 落成笔记后还要能点。 */
    const noteMd = withNoteRefs(answer, anchorAnswerMsg.value?.citations ?? []);
    if (existing) await api.put(`/notes/${existing.id}`, { content_md: noteMd });
    else {
      await api.post('/notes', {
        paper_id: props.paperId, block_id: blockId, highlight_id: highlightId,
        selected_text: anchor.quote || anchor.text,
        start_offset: start, end_offset: end,
        content_md: noteMd,
      });
    }
    anchor.color = color;
    if (degraded) toast('这处选区没有精确位置，已按整段标注', 'warn', 3200);
    toast(existing ? '已更新这处高亮的笔记' : '已标色并存为这处高亮的笔记', 'ok', 2400);
    emit('refresh-side');
  } catch (err) { reportError(err, '存为笔记失败'); }
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

async function loadModelOptions() {
  try {
    const data = await api.get<{ items: ModelConfig[] }>('/models', { model_type: 'agent' });
    modelOptions.value = data.items ?? [];
  } catch (err) {
    modelOptions.value = [];
    console.warn('[chat] 取 agent 模型候选失败', err);
  }
}

function closeMenu() {
  cancelPickHide();
  menuOpen.value = false;
  subStage.value = null;
}

/** 点胶囊：开 / 收根浮层（收的时候子菜单跟着收）。 */
function toggleMenu() {
  if (menuOpen.value) closeMenu();
  else openMenu();
}

/* ── 悬浮展开 ──
   用户口径：鼠标移上去就该看见内容，别先点一下。收起必须**延时**：
   触发片与浮层之间有 5px 缝，侧向子菜单与根浮层之间还有 12px 缝，
   斜着飞过去要 100ms 上下，一离开就收会在半路把菜单抽走。 */
const PICK_HIDE_MS = 260;
let pickHideTimer = 0;
function cancelPickHide() {
  if (pickHideTimer) { clearTimeout(pickHideTimer); pickHideTimer = 0; }
}
/** 进入任一片（触发片 / 根浮层 / 子菜单）：撤销收起；根浮层没开就顺手开。 */
function hoverPick() {
  cancelPickHide();
  openMenu();
}
/** 离开任一片：延时收起，中途进到另一片会被 cancelPickHide 撤销。 */
function leavePick() {
  cancelPickHide();
  pickHideTimer = window.setTimeout(() => { pickHideTimer = 0; closeMenu(); }, PICK_HIDE_MS);
}
function openMenu() {
  cancelPickHide();
  if (!menuOpen.value) menuOpen.value = true;
}

/**
 * 展开根浮层的某一行：在它**侧向**飞出第二级候选（悬浮即展开，点同一行可收回）。
 *
 * 为什么用 `position: fixed` 而不是相对根浮层 absolute：根浮层贴着一行宽 288px 的输入区
 * 向上弹，再往外开一层必然溢出面板被裁；浮层模式最窄只有 320px。这里按被点那行的
 * `getBoundingClientRect()` 直接算视口坐标，视口内怎么夹都不会裁。
 */
function openSub(stage: 'model' | 'effort', ev: MouseEvent) {
  cancelPickHide();
  // 悬浮已经展开过这一层：别重复测量（重摆一次会让子菜单抖一下）
  if (subStage.value === stage) return;
  const row = (ev.currentTarget as HTMLElement | null)?.getBoundingClientRect();
  if (!row) return;
  subRow = row;
  subZ.value = subZOf(ev.currentTarget as HTMLElement | null);
  // 先按视口坐标摆一次（多数情况这一下就是准的），渲染出来再由 placeSub 量真位置校正
  subPos.value = { left: row.right + 8, top: row.top };
  subStage.value = stage;
  void nextTick(placeSub);
}

/** 点某一行：已展开就收回（悬浮展开之外仍留一条"点一下收"的路）。 */
function toggleSub(stage: 'model' | 'effort', ev: MouseEvent) {
  if (subStage.value === stage) { subStage.value = null; return; }
  openSub(stage, ev);
}

/** 被点那一行的视口矩形（子菜单的两趟定位都拿它当锚） */
let subRow: DOMRect | null = null;

/**
 * 子菜单该用的 z：**对话卡当前的计算 z + 2**。
 *
 * 停靠态（贴在侧栏里）卡片没有 z（`z-index: auto`），回落 92 这一档 —— 那时侧栏是 30，
 * 正文里的浮层也不在这儿弹出，够用。取不到卡片时同样走这条兜底，绝不返回 NaN。
 */
function subZOf(el: HTMLElement | null): number {
  const host = el?.closest<HTMLElement>('.chat-host') ?? null;
  const z = Number(host ? window.getComputedStyle(host).zIndex : NaN);
  return Number.isFinite(z) ? z + 2 : 92;
}

/**
 * 子菜单落点：**量一次真实位置再把差值补回去**。
 *
 * 不能只靠 row.top 直接写：position: fixed 只有在最近祖先没有 transform / filter / contain
 * 时才以视口为基准，一旦某个祖先建了包含块（历史遗留样式、以后加的动效都可能），
 * 同样一串数字就会落到别处 —— 表现出来就是「子菜单飞得老高、跟那一行对不上」
 * （用户口径 2026-11-26）。所以这里不赌基准是谁：渲染后量它**实际**落在哪，把差值加回去，
 * 落点必然等于期望值。期望值只有两条：与行上沿对齐；下沿顶出视口就整体上移到刚好放得下
 * （绝不越过行上沿去对齐下沿 —— 那样短列表会被顶到半空）。
 */
function placeSub() {
  const el = subEl.value;
  const row = subRow;
  if (!el || !row) return;
  const vw = window.innerWidth;
  const vh = window.innerHeight;
  const w = el.offsetWidth || SUB_W;
  const h = el.offsetHeight || SUB_H;
  let wantTop = row.top;
  if (wantTop + h > vh - 8) wantTop = Math.max(8, vh - 8 - h);
  /* 间距 **4px**：用户口径 2026-12-27「二级菜单应与一级菜单紧挨着」。
     4 = 根浮层的 `padding: 3px` + 1px 描边位，于是二级菜单的左边缘几乎贴着一级菜单的右边缘；
     再大就显得两块飘着（原来写的是 12）。右边放不下就翻到左侧，同样贴 4px。 */
  const GAP_SUB = 4;
  let wantLeft = row.right + GAP_SUB;
  if (wantLeft + w > vw - 8) wantLeft = row.left - GAP_SUB - w;
  if (wantLeft < 8) wantLeft = Math.max(8, vw - 8 - w);
  const r = el.getBoundingClientRect();
  subPos.value = {
    left: subPos.value.left + (wantLeft - r.left),
    top: subPos.value.top + (wantTop - r.top),
  };
}

/** Esc 收浮层（子菜单一起收）。 */
function onMenuKeydown(ev: KeyboardEvent) {
  if (ev.key === 'Escape' && (menuOpen.value || subStage.value)) closeMenu();
}

/**
 * 点浮层外面收起 —— **用捕获阶段的 document 监听，不再铺一层全视口遮罩**（用户口径）。
 *
 * 遮罩那套的毛病：它盖住整个界面，于是"关浮层"和"点别处"是两件事，
 * 点输入框的第一下只吃掉遮罩、焦点不落到输入框里。这里只在捕获阶段看一眼 target，
 * 不阻止默认行为、不吞事件：点哪儿都照常生效，浮层顺手收掉。
 */
function onOutsidePointer(ev: PointerEvent) {
  const t = ev.target as Element | null;
  if (t && typeof t.closest === 'function' && t.closest('.pick-pop, .pick-sub, .model-btn')) return;
  closeMenu();
}

watch(menuOpen, (open) => {
  if (open) {
    window.addEventListener('keydown', onMenuKeydown);
    document.addEventListener('pointerdown', onOutsidePointer, true);
  } else {
    window.removeEventListener('keydown', onMenuKeydown);
    document.removeEventListener('pointerdown', onOutsidePointer, true);
  }
});

/** 浮层还开着就被卸载（切论文 / 关浮窗）：监听要跟着摘掉，别留给下一份。 */
onUnmounted(() => {
  window.removeEventListener('keydown', onMenuKeydown);
  document.removeEventListener('pointerdown', onOutsidePointer, true);
});

/**
 * 换模型 / 换档：**先本地生效**（点了就得看见），再 PUT 落到这条会话上。
 * 失败必须回滚并提示 —— 留一个后端其实没存的已选状态，会让下一轮请求与刷新后的显示对不上。
 */
async function savePick(patch: Partial<AgentModelPick>) {
  const prev = { ...pick.value };
  pick.value = { ...prev, ...patch };
  closeMenu();
  const id = currentId.value;
  if (!id) return;       // 还没有会话：这一档随下一轮 `ask` 的 payload 带过去（后端会记到新建的会话行上）
  savingPick.value = true;
  try {
    await api.put(`/agent/conversations/${id}`, { ...pick.value });
  } catch (err) {
    pick.value = prev;
    reportError(err, '保存模型选择失败');
  } finally {
    savingPick.value = false;
  }
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

/** 一条引用的**落点锚**（阅读器据此找行）。
 *
 *  行 id 优先，其次解析块 id（阅读器过 `block_rows` 翻成行）。
 *  插图 / 表格引用在这一版之前**没有行 id**（后端只登了页码），点过去只能整页闪：
 *  这里用引用标题里的「图 4 / 表 1」+ 页码拼一个合成锚点，阅读器据此找到那张图所在的图注行，
 *  闪的就跟「标号 1」一样是**图和图注**那一行。
 *  gotoCitation（没有编号表的老路径）与 `data-pool`（合并 / 相邻链的池子）共用这一枚：
 *  两处拼法一旦分叉，同一枚标号点出来的落点就会不一样。 */
function citationAnchor(citation: AgentCitation): string {
  const own = citation.row_id || citation.block_id || '';
  if (own || !citation.page) return own;
  const hit = /(?:图|表|figure|fig\.?|table)\s*(\d{1,3})/i.exec(citation.label || citation.snippet || '');
  return hit ? `fig:p${citation.page}:n${hit[1]}` : '';
}

function gotoCitation(citation: AgentCitation) {
  if (citation.paper_id && citation.paper_id !== props.paperId) {
    toast(`这条引用来自《${citation.label || citation.paper_id}》，不在当前论文里`, 'warn');
    return;
  }
  const anchor = citationAnchor(citation);
  /* 公式引用要精确到那一枚 KaTeX：新消息直接用 `latex`；**存量消息**只有 snippet，
     而公式引用的 snippet 本来就是被截到 160 字的 LaTeX —— 拿它顶（findFormula 比签名，
     截断的长式可能认不出，认不出就照旧退化成整行闪，不会报错）。 */
  const latex = String(citation.latex || (citation.kind === 'formula' ? citation.snippet || '' : ''));
  emit('focus', anchor, citation.page ?? undefined, latex || undefined);
}

/* 窗口一改宽窄，行宽就变 → 之前量出的像素高度会失效（长思考被裁）。清掉实测值，
   开着的段回落到 `max-height: none`：不裁字优先于那一次过渡。 */
function onViewportResize() {
  if (Object.keys(thinkMax.value).length) thinkMax.value = {};
}

onMounted(() => {
  /* ⚠️ 会话列表**不能**排在状态请求后面（用户报的"点对话页签，历史要等一下才出来"）。
     以前是 `await Promise.all([loadStatus(), loadPaper()])` 之后再 `await loadConversations()`，
     而 `/agent/status` 里带一次"问答服务在不在"的健康检查 —— 服务没起来时要等它超时
     （实测 2s 左右），于是历史被硬生生拖了两秒。三件事互不依赖，各走各的。 */
  void loadConversations();
  sessionReady = restoreChatSession().finally(() => { sessionRestored = true; });
  void loadModelOptions();
  void Promise.all([loadStatus(), loadPaper()]);
  window.addEventListener('resize', onViewportResize);
});

/* 从别的页签切回来：实例被 `KeepAlive` 缓存着，列表**已经在屏幕上**，
   这里只是后台悄悄刷一次（有数据时不显示"读取会话…"，不会闪）。 */
onActivated(() => {
  void loadConversations();
  void loadStatus();
  void loadModelOptions();
});

onUnmounted(() => {
  controller?.abort();
  composerRO?.disconnect();
  msgsMO?.disconnect();
  window.removeEventListener('resize', onViewportResize);
  if (stickRaf) { cancelAnimationFrame(stickRaf); stickRaf = 0; }
  window.removeEventListener('keydown', onMenuKeydown);
  if (msgsSaveTimer) { window.clearTimeout(msgsSaveTimer); msgsSaveTimer = 0; }
});

watch(() => props.paperId, async () => {
  controller?.abort();
  level.value = 'list';
  messages.value = [];
  currentId.value = null;
  pending.value = [];
  selCtx.value = null;
  lastAnchor.value = null;              // 换了论文：锚点与色点都不再成立
  anchorAnswerId.value = '';
  pick.value = { model_config_id: '', reasoning_effort: '' };
  sessionRestored = false;
  void Promise.all([loadPaper(), loadConversations()]);
  sessionReady = restoreChatSession().finally(() => { sessionRestored = true; });
  void loadStatus();
});

// 划词带进来的选区：直接进二级页（列表页没有输入框，留在那儿用户会以为没反应），
// 并在输入框里挂一枚**上下文胶囊**（是否随这轮发出去由用户点它决定）。
// 监听**对象本身**而不是文本：同一段文字连点两次也要能切过去。
watch(() => props.selection, (sel) => {
  selCtx.value = sel?.text
    ? { text: sel.text, row_id: sel.row_id ?? null, page: sel.page ?? null,
        where: sel.where ?? '', include: true, explain: sel.explain === true,
        start: sel.start ?? 0, end: sel.end ?? 0, side: sel.side, scope: sel.scope,
        quote: sel.quote }
    : null;
  explainIntent.value = sel?.explain === true;   // 解释那一轮要打到后端的 `mode: "explain"`
  if (sel?.text) {
    level.value = 'thread';
    void nextTick(() => inputEl.value?.focus());
  }
  /* 解释意图 → **自动发一轮**（用户口径：划词「解释」不该再开一张独立浮层，直接进对话、直接出答案）。
     不能只看 `sel.text`：插图解释没有文字选区，图在附件里。 */
  if (sel?.explain) void autoExplain(sel);
});

/** 自动发送前等两件事：**会话就绪**与**上一轮跑完**。
 *  都只等一小会儿 —— 卡住的等待比"没发出去"更让人摸不着头脑。 */
async function waitChatIdle() {
  await sessionReady;
  for (let i = 0; i < 50 && sending.value; i += 1) {
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  await nextTick();
}

/** 「解释」那一轮的自动发送：问题用父组件指定的那句（缺省"解释这段内容"）。
 *
 *  **每条解释单独开一条新会话**（用户口径 2026-11-26）：解释是"就这一段问一次"的短问答，
 *  混进正在聊的那条里既搅上下文、回看时也分不清哪句是在解释哪段。旧会话留在列表里，不丢。
 *
 *  顺序不能动：先等 `waitChatIdle`（里面等的是 `sessionReady`），再 `startNew()`。
 *  `restoreChatSession()` 是异步认回"上次那条会话"的，它晚一步落回来就会把 `currentId`
 *  改回旧会话 —— 这一轮解释就挂到旧会话上去了，正是要避免的事。 */
async function autoExplain(sel: AgentSelection) {
  await waitChatIdle();
  /* `startNew()` 会顺手清空附件列表 —— 但插图解释的图**已经在里面了**（父组件先传图、再挂意图），
     不清掉它这一轮就变成"解释这张图"却什么都没给模型看。所以新会话只清会话，附件跟着带走。 */
  const keptAtts = pending.value;
  startNew();
  pending.value = keptAtts;
  draft.value = sel.ask || '解释这段内容';
  explainIntent.value = true;
  await nextTick();
  await ask('send');
}

/* 对外的一个口子：**把图片塞进附件列表**（右键菜单「添加到对话框（支持图片）」，用户口径 2026-10-08）。
   走的是与"选文件 / 拖进来 / 粘贴"同一条上传链路（`uploadFiles`），所以大小限制、格式校验都一致。 */
defineExpose({ uploadFiles, ensureThread });
</script>

<template>
  <div ref="chatEl" class="chat" :class="{ 'is-float': props.floating }" @dragover.prevent="dragging = true" @dragleave="dragging = false"
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
        <!-- 浮窗态下由外面（`ReaderView`）把「回到侧栏 / 关闭」塞进来：一行放得下就不必
             再单独占一条标题栏（用户口径 2026-11-19：两行压成一行）。停靠态这里为空。 -->
        <slot name="head-actions" />
      </div>

      <div class="filters">
        <div class="seg">
          <button v-for="k in KINDS" :key="k.key" :class="{ active: convKind === k.key }"
                  @click="setKind(k.key)">{{ k.label }}</button>
        </div>
        <span class="spacer" />
        <!-- 多选（用户口径 2026-11-21 改形态）：**圆形按钮**，不再套 `.seg sq` 那层方形胶囊 ——
             上一版是「胶囊里塞一枚 13px 图标」，读起来像一个分段控件的选项，而不是一枚独立动作；
             圆钮 + 两枚对勾的 `list-check`（见 AppIcon）才读得出「多项勾选」。
             尺寸与配色跟笔记页那枚**逐条相同**（`.multi-btn`：30×30、`--bg-sunken` 托底、悬停抬高、
             active 转主题色）—— 两边各写一份 scoped 样式，数值必须一致，别只改一边。 -->
        <button
          class="multi-btn"
          :class="{ active: batch }"
          :aria-pressed="batch"
          title="批量管理（多选 + 批量删除）"
          @click="toggleBatch"
        >
          <AppIcon name="list-check" :size="15" />
        </button>
      </div>

      <div class="list-body" @click="rowMenu = ''">
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
             :class="{ on: c.conversation_id === currentId, pick: isPicked(c) }"
             @contextmenu.prevent="openConvCtx($event, c)">
          <button v-if="batch" class="tick" :class="{ on: isPicked(c) }"
                  :title="isPicked(c) ? '取消选择' : '选择'"
                  @click="togglePick(c)">
            <AppIcon :name="isPicked(c) ? 'checkbox' : 'checkbox-off'" :size="15" />
          </button>

          <!-- 这一条还在生成：左边转个圈（点返回不会打断它，跑完自己消失）。
               ⚠️ 必须**排在 input/button 那条 v-if–v-else 链之前**：插在中间会把 v-else 断开。 -->
          <span v-if="isRunning(c.conversation_id)" class="row-run" title="正在生成…"
                data-conv-running>
            <svg class="row-run-ring" viewBox="0 0 16 16" aria-hidden="true">
              <circle cx="8" cy="8" r="6" />
            </svg>
          </span>
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

          <!-- 行上只留一枚「⋯」（用户口径 2026-11-19：照 DeepSeek 那种行结构，不再挂两枚图标）：
               重命名 / 分享 / 多选 / 删除都收进它下面。 -->
          <div v-if="!batch" class="row-acts">
            <button
              class="ico ico-more"
              title="更多"
              data-conv-more
              @click.stop="toggleRowMenu(c.conversation_id, $event)"
            >
              <AppIcon name="more-vert" :size="18" />
            </button>
          </div>
          <!-- 「⋯」菜单：**浮到这一行的右侧**、并避让视口底边（见 placeRowMenu） -->
          <Teleport to="body">
            <div
              v-if="rowMenu === c.conversation_id"
              :ref="setRowMenuEl"
              class="row-menu row-menu-fly glass-menu"
              :style="rowMenuStyle"
              @click.stop
            >
              <button class="menu-item" @click.stop="rowMenu = ''; startRename(c)">
                <AppIcon name="edit" :size="12" />重命名
              </button>
              <!-- 位置照旧（重命名下面那一条），但**不叫"分享"**（用户口径 2026-11-19）：
                   它是"把这个会话拎成浮窗"，名字就用「浮层打开」。 -->
              <button v-if="showFloatBtn" class="menu-item" @click.stop="shareToFloat(c)">
                <AppIcon name="pip" :size="13" />浮层打开
              </button>
              <button class="menu-item" @click.stop="rowMenu = ''; toggleBatch()">
                <AppIcon name="list-check" :size="12" />多选
              </button>
              <button class="menu-item danger" @click.stop="rowMenu = ''; askDeleteOne(c)">
                <AppIcon name="trash" :size="12" />删除
              </button>
            </div>
          </Teleport>
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
        <!-- 右上角：**两枚各自独立的圆钮**（用户口径 2026-11-19：新对话和浮层打开要分开），
             但仍是页面那套"托底圆钮"—— 同一个 `--bg-sunken` 底、同 26px、悬停浮成 `--bg-elev`。
             浮层打开用**画中画**图标（同一个东西换到小窗里看），与新对话的加号一眼区分。 -->
        <span class="th-acts">
          <!-- 拆分（浮层打开）：**加号左边**；图标用"分享/对外打开"那枚方框加箭头，与加号一眼区分 -->
          <button
            v-if="showFloatBtn"
            class="th-act"
            title="浮层打开（把这栏拎成浮窗，正文回到整宽）"
            @click="emit('float')"
          >
            <AppIcon name="pip" :size="15" />
          </button>
          <!-- 会话页顶部：标题已经写着会话名了，再造一个「+ 新对话」文字钮就是重复 -->
          <button class="th-act" title="新建对话" @click="startNew">
            <AppIcon name="add" :size="14" />
          </button>
          <!-- 浮窗态的「回到侧栏 / 关闭」由 `ReaderView` 从这个插槽塞进来 -->
          <slot name="head-actions" />
        </span>
      </div>

      <div ref="msgsEl" class="msgs" @scroll.passive="onMsgsScroll"
           @wheel.passive="onMsgsWheel"
           @touchstart.passive="onMsgsTouchStart" @touchmove.passive="onMsgsTouchMove">
        <div v-if="loading" class="empty small">读取会话…</div>

        <div v-else-if="!messages.length && !sending" class="hello">
          <!-- 空态**只有一句语录当背景**（用户口径 2026-10-03：不要上传/换一条这类按钮）。
               那枚标记从"双引号水印"换成**裁好的应用图标**（用户口径 2026-10-07 第四轮：
               这个位置本来就是一枚居中图标，居中的语录在它下面）。 -->
          <span class="hello-mark" data-hello-mark><img :src="BRAND_ICON" alt="" /></span>
          <p class="hello-line">{{ headline }}</p>
        </div>

        <div v-for="m in messages" :key="m.message_id" class="msg" :class="m.role"
             :data-live="m.message_id === liveId ? '1' : null"
             :data-fresh="freshIds.has(m.message_id) ? m.role : undefined">
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
            <div v-if="showRun(m)" class="run" :class="{ live: m.message_id === liveId }"
                 :data-open="isRunOpen(m.message_id) ? '1' : null">
              <button class="run-head" :class="{ openable: hasTrace(m) }"
                      :aria-expanded="hasTrace(m) ? isRunOpen(m.message_id) : undefined"
                      @click="hasTrace(m) && toggleRun(m.message_id)">
                <!-- 只有旁白的一轮（还没开始想、也没调工具）：不给箭头，留一个同宽的占位，
                     免得抬头文字比别的轮往左挪 12px。 -->
                <AppIcon v-if="hasTrace(m)" class="caret" :class="{ shut: !isRunOpen(m.message_id) }"
                         name="chevron-down" :size="12" />
                <span v-else class="caret-gap" aria-hidden="true" />
                <span v-if="m.message_id === liveId" class="dot" />
                <span class="run-title">{{ runTitle(m) }}</span>
                <span class="run-sum truncate">{{ runSummary(stepsOf(m)) }}</span>
              </button>
              <!-- 轨迹正文：**展开/收起也走过渡**（不瞬间弹出）。里面的步骤行由
                   `TransitionGroup` 管进场与"让位"（新行淡入 + 后面的行平滑下移）。 -->
              <Transition name="fold" :css="false" @enter="runEnter" @leave="runLeave">
                <div v-if="isRunOpen(m.message_id) && hasTrace(m)" class="run-body"
                     :ref="(el) => setRunBody(m.message_id, el)"
                     @scroll.passive="onRunScroll($event.target as HTMLElement)">
                  <TransitionGroup name="step">
                    <!-- **一项只有一个根元素**（类名在 itemClass 里合成）：拆成 v-if/v-else-if
                         多个兄弟分支，编译出来每个 key 会变成 Fragment、分支 key 全是 0/1/2，
                         TransitionGroup 就认不出「哪一行是谁」，让位动画随之失效。 -->
                    <div v-for="item in traceItems(m)" :key="`${m.message_id}-${item.key}`"
                         :class="itemClass(m, item)"
                         :title="item.lead?.s.kind === 'thinking'
                           ? (isItemOpen(m, item) ? '收起这段思考' : '展开这段思考') : undefined"
                         @click="onItemClick(m, item, $event)"
                         @pointerover="item.lead?.s.kind === 'text' && onCitePeek($event)"
                         @pointerout="item.lead?.s.kind === 'text' && onCitePeekOut()">
                      <!-- 一组连续的工具调用：抬头 + 左侧竖引导线 + 缩进的子行
                           （照参考实现「代码 / 工具调用」那套：一行一次调用，参数跟在短语后面） -->
                      <template v-if="item.rows">
                        <div class="tool-head">
                          <AppIcon class="group-caret caret" :class="{ shut: !isItemOpen(m, item) }"
                                   name="chevron-down" :size="11" />
                          <AppIcon name="code" :size="11" />
                          <span class="step-tag">工具</span>
                          <span class="tool-count">{{ item.rows.length }} 次调用</span>
                          <template v-if="!isItemOpen(m, item)">
                            <span class="think-sep" aria-hidden="true" />
                            <span class="tool-sum">{{ toolSummary(item.rows) }}</span>
                          </template>
                        </div>
                        <Transition name="fold" :css="false" @enter="foldEnter" @leave="foldLeave">
                          <div v-if="isItemOpen(m, item)" class="tool-lines">
                            <div v-for="r in item.rows" :key="r.i" :class="rowClass(m, r)">
                              <svg v-if="r.s.status === 'running'" class="step-spin" viewBox="0 0 16 16" aria-hidden="true">
                                <circle cx="8" cy="8" r="6.2" />
                              </svg>
                              <AppIcon v-else :name="toolIcon(r.s.tool)" :size="11" />
                              <span class="step-tag">{{ stepLabel(r.s.tool) }}</span>
                              <span v-if="stepBrief(r.s)" class="step-arg" :title="stepText(r.s)">· {{ stepBrief(r.s) }}</span>
                              <AppIcon v-if="r.s.ok === false" class="step-flag bad" name="error" :size="11" />
                              <AppIcon v-else-if="r.s.ok === true" class="step-flag ok" name="check" :size="11" />
                              <span v-if="stepMs(r.s)" class="step-ms">{{ fmtMs(stepMs(r.s)) }}</span>
                            </div>
                          </div>
                        </Transition>
                      </template>

                      <template v-else-if="item.lead">
                        <template v-if="item.lead.s.kind === 'text'">
                          <div v-html="renderAnswer(item.lead.s.text || '', m.citations)" />
                        </template>
                        <!-- 思考段：第一行是「⌄ ✦ 思考 · 一行摘要」，正文在下面**从最左边起**，
                             不缩进、不用等宽字体（思考是一段中文正文，不是代码）。
                             历史消息默认折叠；对话期间最后一段自动展开（新的来了上一段自动收起）。 -->
                        <template v-else-if="item.lead.s.kind === 'thinking'">
                          <span class="think-lead">
                            <AppIcon class="step-caret caret" :class="{ shut: !isItemOpen(m, item) }"
                                     name="chevron-down" :size="11" />
                            <AppIcon class="step-ico" name="sparkle" :size="11" />
                            <span class="step-tag">思考</span>
                            <template v-if="!isItemOpen(m, item)">
                              <span class="think-sep" aria-hidden="true" />
                              <span class="think-sum">{{ thinkSummary(item.lead.s.text) }}</span>
                            </template>
                          </span>
                          <Transition name="fold" :css="false" @enter="foldEnter" @leave="foldLeave">
                            <span v-if="isItemOpen(m, item)"
                                  class="step-text" :style="stepMaxStyle(m.message_id, item.lead.i)">{{ item.lead.s.text }}</span>
                          </Transition>
                        </template>
                        <!-- 宿主自己的动作（准备档案 / 检索 / 开始组织回答）：一行淡字，不是工具调用 -->
                        <span v-else class="step-text">{{ item.lead.s.text }}</span>
                      </template>
                    </div>
                  </TransitionGroup>
                </div>
              </Transition>
            </div>

            <!-- 轨迹到头了、下面是答案：一条细线把抬头那行与正文分开（流式与完成态都靠这一条）。
                 只有**带轨迹**（有 run/steps）的助手消息才画；普通回答不凭空多一条线。 -->
            <div v-if="m.role === 'assistant' && stepsOf(m).length" class="run-rule" aria-hidden="true" />

            <!-- 只发了图片的提问没有正文：后端为给模型留上下文会存一句「（图片 ×N）」，
                 那不该渲染成一个空消息泡（用户口径 2026-12-07）—— 图在下面单独显示就够了。 -->
            <!-- 正在写的那条助手消息：正文**按块**渲染（`splitAnswerParts`），已封口块的
                 `v-html` 值不变 → DOM 不被重建，只有最后一块跟着增量重画。 -->
            <template v-if="m.role === 'assistant' && m.message_id === liveId && hasBody(m)">
              <div v-for="(part, pi) in answerParts(m)" :key="pi"
                   class="md body md-part" :class="{ 'md-part-tail': part.tail }"
                   v-html="part.html"
                   @click="onCitationClick"
                   @pointerover="onCitePeek" @pointerout="onCitePeekOut" />
            </template>
            <div v-else-if="hasBody(m)" class="md body"
                 v-html="renderAnswer(m.role === 'user' ? splitQuote(m.content).rest : finalAnswer(m), m.citations)"
                 @click="onCitationClick"
                 @pointerover="onCitePeek" @pointerout="onCitePeekOut" />

            <!-- 附件图：点开的是阅读器那套**图片查看器**（用户口径 2026-11-19），不再开新标签页 -->
            <div v-if="m.attachments?.length" class="atts">
              <button
                v-for="a in m.attachments"
                :key="a.attachment_id"
                class="att"
                type="button"
                :title="`${a.name} · ${fmtSize(a.bytes)}`"
                @click="emit('zoom', { url: imageSrc(a.url), caption: a.name })"
              >
                <img :src="imageSrc(a.url)" :alt="a.name" loading="lazy" />
              </button>
            </div>

            <!-- 这一轮提问**带过选区**（划词解释）时：回答下面给一排色点。
                 挑个颜色 = 把这段标出来 + 把这条回答存成它的笔记（用户口径 2026-10-07：
                 "想留这段"和"想标黄色"本来就是同一个意图，一次点完）。 -->
            <div v-if="canSaveAsNote(m)" class="note-swatches">
              <span class="ns-hint">存为笔记</span>
              <button v-for="c in NOTE_COLORS" :key="c" type="button"
                      class="hl-swatch" :class="[c, { on: paletteNameOf(lastAnchor?.color) === c }]"
                      :style="swatchStyleOf(c)"
                      :title="'标成' + swatchLabel(c) + '色，并把这条回答存成它的笔记'"
                      @click="saveAnswerWithColor(c)" />
            </div>

            <!-- 引用**不再在下面列一排 chunk 号**（用户口径 2026-10-03）：
                 正文里的 `[n]` 本身就是锚点，点它跳转。 -->

            <!-- 消息操作条：**在气泡下面**（用户口径 2026-10-03 —— 上面的那排按钮全部移除）。
                 语义分三档：改写这条（就地）/ 重答这条（就地）/ **开新分支**（那枚 git 分支图标）。
                 还在跑（`liveId` 那条）时**整条不渲染**：这时没有"复制 / 修改"可做，
                 挂着那排按钮只会边生成边闪。兄弟分支那排也在这个块里，一并藏到完成态。 -->
            <div v-if="m.message_id !== liveId" class="msg-tools"
                 :class="{ show: (m.siblings?.length ?? 0) > 1 }">
              <template v-if="(m.siblings?.length ?? 0) > 1">
                <button class="ico" title="上一个版本" @click="switchBranch(m, -1)"><AppIcon name="back" :size="11" /></button>
                <span class="tiny faint mono">{{ m.sibling_index }}/{{ m.siblings?.length }}</span>
                <button class="ico" title="下一个版本" @click="switchBranch(m, 1)"><AppIcon name="forward" :size="11" /></button>
                <span class="tool-sep" />
              </template>
              <!-- 气泡下面**只留三样**（用户口径 2026-11-19）：时间 · 复制 · 修改（只有提问能改）。
                   重新生成 / 开新分支那两枚不再常驻 —— 它们属于"重来一次"，不该和复制挤一排。 -->
              <span v-if="msgTime(m)" class="msg-time" :title="fmtDate(m.created_at)">{{ msgTime(m) }}</span>
              <button class="ico" title="复制" @click="copyMessage(m)"><AppIcon name="copy" :size="12" /></button>
              <button v-if="m.role === 'user'" class="ico" title="修改这条提问（会替换它之后的回答）"
                      :disabled="sending" @click="startEdit(m, false)"><AppIcon name="edit" :size="12" /></button>
            </div>
          </template>
        </div>

        <!-- 正在生成那一轮就是上面 v-for 里的**本地占位消息**（`liveId`）：
             思考/正文/工具夹杂的渲染因此与落库后完全同一套模板，不再有第二套"流式专用"样式。
             流式期间**不列 chunk 号**（用户口径 2026-10-03）：边写边多出来的编号只会刷屏。 -->

        <ChatErrorCard v-if="chatError" :error="chatError" />
      </div>

      <!-- 输入区 = **一块浮在消息上的圆角容器**：上面是 textarea，下面是操作条。
           容器自己承担"一块输入组件"的观感，里面的元素不再各自成块；`ref` 给样式里那个
           ResizeObserver 量高度用（消息区要按它留出底部空白，见 `.composer`）。 -->

      <!-- 回到最新：往上翻（尤其流式输出时）就浮出来。点它 = 回到最新一句 + **恢复跟随**，
           之后新内容继续自动往下走（用户口径 2026-12-03）。 -->
      <button
        v-if="!follow"
        class="to-bottom"
        type="button"
        data-to-bottom
        :title="sending ? '回到最新（继续跟随输出）' : '回到最新'"
        :aria-label="sending ? '回到最新（继续跟随输出）' : '回到最新'"
        @click="backToLatest"
      >
        <AppIcon name="chevron-down" :size="16" />
        <span v-if="sending" class="to-bottom-live" aria-hidden="true" />
      </button>
      <div ref="composerEl" class="composer" @pointerdown="keepComposerFocus">
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
              <!-- 停止 = 主题色**圆环 + 实心方块**（用户口径：换成图里这枚 SVG 的样子）。
           圆环交给按钮自己的描边（比画在 SVG 里更锐），方块仍是 stop 图标。
           方块 10px 照参考图的比例（≈ 圆环内径的 0.45）定的。 -->
      <button v-if="sending" class="btn icon sm stop send" title="停止" @click="stop"><AppIcon name="stop" :size="10" /></button>
              <button v-else class="btn primary icon sm send" :disabled="!canSend" title="发送" @click="ask('send')">
                <AppIcon name="arrow-up" :size="15" />
              </button>
            </div>
          </template>
        </div>

        <!-- 模型与上下文圆环在**输入框外**（用户口径 2026-10-03）：一行低调的明细，
             不跟输入抢位置；圆环很小、里面不放数字，数值写在 tooltip 里。 -->
        <div v-if="!editing" class="composer-foot">
          <!-- 悬浮即展开（用户口径）：移上去就看见模型 / 推理等级两行，
               点一下仍是"开/收"（触屏与键盘只有这条路）。 -->
          <button class="model-btn truncate" :class="{ on: menuOpen }" :title="pickTitle"
                  :aria-expanded="menuOpen" @click="toggleMenu"
                  @mouseenter="hoverPick" @mouseleave="leavePick">
            <img
              v-if="vendorIco(activeModel?.provider ?? status?.model?.provider)"
              class="pv-ico" :src="vendorIco(activeModel?.provider ?? status?.model?.provider)" alt=""
            >
            <span class="truncate">{{ modelLabel }}</span><AppIcon name="chevron-right" :size="11" />
          </button>
          <span v-if="usage?.compressed" class="badge dim tiny">已压缩</span>
          <span class="spacer" />
          <!-- 缓存命中率与上下文占比是同族的两个"用量读数"，一起靠右；
               模型名那一侧本来就会 truncate，读数放这儿才不会被长模型名挤掉。
               老会话后端没有快照 → cacheHitText 为空 → 整条不渲染。 -->
          <span v-if="cacheHitText" class="foot-stat" :title="cacheHitTitle">缓存命中 {{ cacheHitText }}</span>
          <button class="ring" :class="{ busy: compressing }" :title="ringTitle"
                  :disabled="!canCompress" @click="compress">
            <svg viewBox="0 0 36 36" class="ring-svg">
              <circle class="ring-track" cx="18" cy="18" r="15.5" />
              <circle class="ring-bar" cx="18" cy="18" r="15.5" :stroke-dasharray="ringDash" />
            </svg>
          </button>
          <!-- 上下文占比：圆环只表达粗细，数字说清用了多少；数字本身仍可点（压缩），
               所以不另起一枚按钮，避免同一行出现两个能点出同一动作的东西。 -->
          <span class="foot-stat foot-pct" :title="contextTitle">{{ contextText }}</span>

          <!-- 模型选择改成**一个下拉框**（用户口径 2026-12-07）：点一下直接把模型列出来，
               按提供方分组排序，但**不再画分组标题**（用户口径 2026-12-08：那行小写厂商 id
               既占高度又不好看 —— 每一条自己带厂商图标和「厂商 · 模型 id」副标题，够认了）；
               也不再先弹「模型 / 推理等级」两行的中转层。推理等级是列表底部的一行
               （still 侧向飞出，不丢功能）。 -->
          <!-- 材质走全站那一份玻璃（`styles/base.css` 的 `.glass-menu`）：与「更多」/ 右键菜单同一块 -->
          <div v-if="menuOpen" class="pick-pop pick-pop-model glass-menu" @mouseenter="hoverPick" @mouseleave="leavePick">
            <div class="pick-model-list">
              <div v-if="modelReadonly" class="pick-empty">
                <span class="pick-item-name">未单独配置</span>
                <span class="pick-item-sub truncate">{{ modelSub }}</span>
              </div>
              <template v-else>
                <template v-for="g in modelGroups" :key="g.provider">
                  <button v-for="m in g.items" :key="m.id" class="pick-item"
                          :class="{ on: m.id === activeModel?.id }"
                          :disabled="savingPick || m.enabled === false"
                          @click="savePick({ model_config_id: m.id })">
                    <img v-if="vendorIco(m.provider)" class="pv-ico" :src="vendorIco(m.provider)" alt="">
                    <span class="pick-item-main">
                      <span class="pick-item-name truncate">{{ m.name || m.model_name || m.id }}</span>
                      <span class="pick-item-sub truncate">{{ subLine(m) }}</span>
                    </span>
                    <AppIcon v-if="m.id === activeModel?.id" name="check" :size="13" />
                  </button>
                </template>
              </template>
            </div>
            <button class="pick-row" :class="{ on: subStage === 'effort' }"
                    @mouseenter="openSub('effort', $event)" @click="toggleSub('effort', $event)">
              <span class="pick-key">推理等级</span>
              <span class="pick-val truncate">{{ effortLabel }}</span>
              <AppIcon name="chevron-right" :size="11" />
            </button>
          </div>

          <!-- 第二级：侧向飞出的子菜单。**必须 Teleport 到 body**（用户口径 2026-11-26「层级问题」）：
               它原来留在卡片里，只能跟卡片的 stacking context 比高低（卡片 60），
               而浮窗的角把手是「挂在卡片外面」的兄弟节点（61 / 有焦点 63）——
               子菜单于是被那圈弧线压住，看起来像浮层没盖住指示器。挪到 body 之后
               它与所有浮层同层比较，正文里任何指示器都盖不住它。 -->
          <Teleport to="body">
          <div v-if="menuOpen && subStage" ref="subEl" class="pick-sub glass-menu"
               :style="{ left: `${subPos.left}px`, top: `${subPos.top}px`, zIndex: subZ }"
               @mouseenter="hoverPick" @mouseleave="leavePick">
            <div v-if="subStage === 'model'" class="pick-sub-list">
              <div v-if="modelReadonly" class="pick-empty">
                <span class="pick-item-name">未单独配置</span>
                <span class="pick-item-sub truncate">{{ modelSub }}</span>
              </div>
              <template v-else>
                <template v-for="g in modelGroups" :key="g.provider">
                <button v-for="m in g.items" :key="m.id" class="pick-item"
                        :class="{ on: m.id === activeModel?.id }"
                        :disabled="savingPick || m.enabled === false"
                        @click="savePick({ model_config_id: m.id })">
                  <img v-if="vendorIco(m.provider)" class="pv-ico" :src="vendorIco(m.provider)" alt="">
                  <span class="pick-item-main">
                    <!-- 不再挂「默认」胶囊（用户口径）：谁生效看右侧勾选，
                         胶囊只把这一行撑高、撑乱。 -->
                    <span class="pick-item-name truncate">{{ m.name || m.model_name || m.id }}</span>
                    <span class="pick-item-sub truncate">{{ subLine(m) }}</span>
                  </span>
                  <AppIcon v-if="m.id === activeModel?.id" name="check" :size="13" />
                </button>
                </template>
              </template>
            </div>

            <div v-else class="pick-sub-list">
              <button v-if="!defaultInOptions" class="pick-item"
                      :class="{ on: !pick.reasoning_effort }" :disabled="savingPick"
                      @click="savePick({ reasoning_effort: '' })">
                <span class="pick-item-main">
                  <span class="pick-item-name truncate">
                    {{ defaultEffort ? `默认（${defaultEffort}）` : '默认' }}
                  </span>
                </span>
                <AppIcon v-if="!pick.reasoning_effort" name="check" :size="13" />
              </button>
              <button v-for="e in effortOptions" :key="e.value" class="pick-item"
                      :class="{ on: activeEffort === e.value }" :disabled="savingPick"
                      @click="savePick({ reasoning_effort: e.value })">
                <span class="pick-item-main">
                  <span class="pick-item-name truncate">{{ e.value }}</span>
                  <span v-if="e.desc" class="pick-item-sub truncate">{{ e.desc }}</span>
                </span>
                <AppIcon v-if="activeEffort === e.value" name="check" :size="13" />
              </button>
              <!-- 兜底：历史数据里存的档不在候选里，也让它看得见、点得到 -->
              <button v-if="isCustomEffort" class="pick-item on" :disabled="savingPick"
                      @click="savePick({ reasoning_effort: pick.reasoning_effort })">
                <span class="pick-item-main">
                  <span class="pick-item-name truncate">{{ pick.reasoning_effort }}</span>
                </span>
                <AppIcon name="check" :size="13" />
              </button>
            </div>
          </div>
          </Teleport>
        </div>
      </div>

      <input ref="fileInput" class="hidden-file" type="file" accept="image/png,image/jpeg,image/webp,image/gif"
             multiple @change="uploadFiles(($event.target as HTMLInputElement).files)" />
  </template>

    <div v-if="dragging" class="drop-hint"><AppIcon name="image" :size="16" />松手即上传</div>

    <!-- **右键**会话行：在同一套动作上再给一条"鼠标就在这儿"的入口（用户口径 2026-12-27
         「实现右键对话以及笔记，能在鼠标位置的右下角弹出菜单」）。用 `ContextMenu` 而不是
         「⋯」那枚 flyout：它按**鼠标点**定位（左上加 2px 起画 = 右下角展开）、靠边自动往里挪、
         Esc / 点外面 / 滚动就收 —— 那套行为那边已经写好了。 -->
    <ContextMenu
      :open="Boolean(convCtx)"
      :x="convCtx?.x ?? 0"
      :y="convCtx?.y ?? 0"
      :items="convCtxItems"
      @select="onConvCtx"
      @close="convCtx = null"
    />
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
/* 会话行上那枚「更多」：**竖排三点 + 大一号**（用户口径：改为竖向并增大按钮） */
.ico-more { width: 30px; height: 30px; border-radius: 999px; }
.hidden-file { display: none; }

/* ── 一级：列表 ── */
.list-head { display: flex; align-items: center; gap: 6px; padding: 8px 8px 6px; }
/* 列表页唯一那枚动作「＋ 新对话」：改成主题色淡底 + 强调色文字（用户口径 2026-12-02）。
   与胶囊里那套"主色淡底"同一门语言；描边留空，免得变成一枚独立的白 pill。 */
.list-head .new-conv {
  color: var(--accent-text);
  background: color-mix(in srgb, var(--accent) 12%, transparent);
  border-color: transparent;
}
.list-head .new-conv:hover { background: color-mix(in srgb, var(--accent) 20%, transparent); }
/* 搜索框用全局 .search-field：这里只把它撑满侧栏（全局是给工具栏定的固定宽度） */
.list-head .search-field { width: 100%; }
.list-head .search-field input { width: 100%; min-width: 0; }

/* 一行放不下就换行（侧栏最窄时只有 300px，硬挤会把最右边那枚挤没） */
.filters { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; padding: 0 8px 8px; }

.list-body { flex: 1; min-height: 0; overflow-y: auto; padding: 0 6px 8px; }
.row {
  position: relative;
  /* 圆角比全局 `--radius` 收一档（用户口径：对话行的悬浮 / 选中框圆角略小一点） */
  display: flex; align-items: center; gap: 2px; border-radius: calc(var(--radius) - 3px);
  padding: 1px 2px; margin-bottom: 2px; border: 1px solid transparent;
}
/* 行右上角「⋯」下面那枚小浮层（重命名 / 分享 / 多选 / 删除）：与页面同一门材质 */
.row-menu {
  position: absolute; right: 4px; top: calc(100% - 4px); z-index: 6;
  display: flex; flex-direction: column; gap: 1px; padding: 4px; min-width: 126px;
  border-radius: 12px;
}
/* 「⋯」菜单的浮层形态（2026-12-27）：`position: fixed` + Teleport 到 body ——
   列表是 `overflow-y:auto`，留在行里的绝对定位会被裁掉（"菜单被底部吃掉"就是这么来的）。
   坐标由 `placeRowMenu` 算：这一行右侧、避让视口底边；放不下就翻到左侧。 */
.row-menu.row-menu-fly {
  position: fixed; right: auto; top: auto; z-index: 60;
  min-width: 132px; padding: 5px; border-radius: 12px;
  /* 材质走全站那一份玻璃（`base.css` 的 `.glass-menu`，与右键菜单 / 行内「更多操作」同一块）。 */
}
.row-menu .menu-item {
  display: flex; align-items: center; gap: 7px; width: 100%; height: 30px; padding: 0 8px;
  border: 0; border-radius: 8px; background: transparent; color: var(--text-dim);
  font: inherit; font-size: calc(12px * var(--ts, 1)); text-align: left; cursor: pointer;
}
.row-menu .menu-item:hover { background: var(--bg-hover); color: var(--text); }
.row-menu .menu-item.danger { color: var(--danger); }
.row-menu .menu-item.danger:hover { background: var(--danger-soft); color: var(--danger); }
.row:hover { background: var(--bg-hover); }
.row.on { background: var(--accent-soft); border-color: color-mix(in srgb, var(--accent) 30%, transparent); }
/* 「这一条还在生成」：行首一枚转圈环（比文字矮一档，落在标题行的中线附近）。
   用 SVG 的两段 dash 做缺口环，转的是它自己 —— 与加载指示器同一个观感，但不吃布局。 */
.row-run {
  flex: none; width: 15px; height: 15px; margin: 0 2px 0 4px;
  display: inline-flex; align-items: center; justify-content: center;
  color: var(--accent);
}
.row-run-ring { width: 15px; height: 15px; animation: row-run-spin 900ms linear infinite; }
.row-run-ring circle {
  fill: none; stroke: currentColor; stroke-width: 2;
  stroke-dasharray: 26 12; stroke-linecap: round; opacity: .9;
}
@keyframes row-run-spin { to { transform: rotate(360deg); } }
@media (prefers-reduced-motion: reduce) {
  .row-run-ring { animation: none; opacity: .75; }
}
.row.pick { background: var(--accent-soft); }
.row-main {
  flex: 1; min-width: 0; display: flex; flex-direction: column; align-items: flex-start;
  gap: 2px; border: 0; background: none; color: var(--text); cursor: pointer;
  text-align: left; padding: 7px 6px; font: inherit;
}
.row-title { font-size: calc(12.5px * var(--ts, 1)); font-weight: 500; max-width: 100%; }
.row-meta { display: flex; align-items: center; gap: 5px; max-width: 100%; }
.kd {
  display: inline-flex; align-items: center; gap: 2px; font-size: calc(10px * var(--ts, 1)); padding: 0 5px;
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
  background: var(--bg-elev); color: var(--text); font: inherit; font-size: calc(13px * var(--ts, 1));
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
/* 多选圆钮（用户口径 2026-11-21）：**30×30 正圆**，取代原来 `.seg sq` 那层方形胶囊。
   三档口径与 `.seg` 一致：`--bg-sunken` 托底 → 悬停换 `--bg-hover` 并抬一档阴影 → active 转主题色
   （`--accent-soft` 底 + `--accent-text` 图标）。
   ⚠️ 笔记页那枚是**同一套数值**（ReaderSidePanel 的 `.multi-btn`），改一边必须同时改另一边。 */
.multi-btn {
  width: 30px; height: 30px; flex: none;
  display: inline-flex; align-items: center; justify-content: center;
  border: 0; border-radius: 50%; background: var(--bg-sunken); color: var(--text-dim);
  cursor: pointer;
  transition: background var(--dur) var(--ease), color var(--dur) var(--ease),
    box-shadow var(--dur) var(--ease), transform var(--dur) var(--ease);
}
.multi-btn:hover { background: var(--bg-hover); color: var(--text); box-shadow: var(--shadow-1); }
.multi-btn:active { transform: scale(.94); }
.multi-btn.active { background: var(--accent-soft); color: var(--accent-text); box-shadow: none; }

/* ── 二级：会话 ── */
.thread-head {
  display: flex; align-items: center; gap: 3px; padding: 5px 8px 5px 6px;
  border-bottom: 1px solid var(--divider);
}
/* 顶部按钮：**26px 圆钮、各带自己的 `--bg-sunken` 底**（与侧栏页签胶囊、搜索那两枚小胶囊同一门语言），
   悬停时那枚"浮起来"（`--bg-elev` + 一档阴影）。
   外壳 `.th-acts` 只是一条 4px 间距的横排 —— 不再共用一个托盘：新对话与浮层打开是两件不同的事
   （用户口径 2026-11-19），共用一个底会被读成一个分段控件。 */
.th-act {
  width: 26px; height: 26px; flex: none; display: inline-flex; align-items: center; justify-content: center;
  border: 0; border-radius: 999px; background: var(--bg-sunken); color: var(--text-dim); cursor: pointer;
  transition: background var(--dur) var(--ease), color var(--dur) var(--ease), box-shadow var(--dur) var(--ease);
  /* 顶部这几枚（返回 / 浮层打开 / 新对话）**图标走主题色**（用户口径 2026-12-02：
     原来是正文灰，换主题后跟界面不搭）。底子不动，悬停时浮成 --bg-elev。 */
  color: var(--accent-text);
}
.th-act:hover { background: var(--bg-elev); color: var(--accent-hover); box-shadow: var(--shadow-1); }
.th-act:active { transform: scale(.94); }
.th-acts { display: inline-flex; align-items: center; gap: 4px; flex: none; }
.th-title { font-size: calc(13px * var(--ts, 1)); font-weight: 600; color: var(--text); max-width: 52%; }
/* 浮窗态：会话头 / 列表头就是拖动把手，光标要是「抓取的手」。
   以前只在 ReaderView 那边用 :deep(.thread-head) 写了一条，标题区常落到子元素上，
   用户看到的是普通箭头（用户口径：笔记浮层是手形、对话浮层不是）。
   这里按 floating 属性在组件自己的作用域里直接写一遍，最稳。 */
/* 浮窗态：整块都是把手（拖动由 ReaderView 的 onFloatCardDown 接管），所以从根上给手型；
   正文与列表这些「要读要选」的区域再改回普通箭头，否则整窗都是抓手、看着像点不了。 */
.chat.is-float { cursor: grab; }
.chat.is-float .msgs,
.chat.is-float .list-body { cursor: auto; }
.chat.is-float .thread-head,
.chat.is-float .list-head { cursor: grab; user-select: none; }
.chat.is-float .thread-head:active,
.chat.is-float .list-head:active { cursor: grabbing; }
/* 标题 / 空隙这类不可交互的子元素一律继承手型（有的浏览器在 span 上会退回默认箭头） */
.chat.is-float .thread-head > span,
.chat.is-float .list-head > span { cursor: inherit; }

.msgs {
  /* 底部这块空白是给**悬浮的输入区**让位的（JS 按实测高度覆盖，这里只是兜底） */
  flex: 1; min-height: 0; overflow-y: auto; padding: 10px 10px 112px; overflow-wrap: anywhere;
  /* ⚠️ 关掉浏览器的滚动锚定（见 useFollowTail 第三条约定）：它会为了"内容别跳"自己改 scrollTop，
     和 `stickToBottom` 每帧的贴底赋值抢同一个位置 —— 表现就是滚到底之后又弹回一点、
     或者贴着底还闪一帧。贴底由我们负责，浏览器在这里必须不插手。 */
  overflow-anchor: none;
  /* 列向 flex：空态那一块才能"占满整块高度、把图标 + 语录**竖直居中**"（用户口径 2026-10-07）。
     消息自己还是从上往下排（块级子项在列向 flex 里本来就是撑满宽度、顺序堆叠）。 */
  display: flex; flex-direction: column;
}
.msg { margin-bottom: 14px; flex: none; }
/* 刚发出的那一条：用户气泡从右下角长出来（320ms、回弹到 1.3），助手那条 12px 上浮 + 240ms glide。
   和「输入框飞过去」的 400ms 幽灵接在同一个节拍上 —— 幽灵最后 30% 淡出，气泡正好接住。 */
.msg[data-fresh="user"] .body {
  transform-origin: 100% 100%;
  animation: msg-grow .32s cubic-bezier(.34, 1.3, .64, 1) both;
}
.msg[data-fresh="assistant"] { animation: row-rise .24s cubic-bezier(.22, .61, .36, 1) both; }
@keyframes msg-grow {
  from { opacity: 0; transform: scale(.2); }
  to { opacity: 1; transform: none; }
}
@keyframes row-rise {
  from { opacity: 0; transform: translateY(12px); }
  to { opacity: 1; transform: none; }
}
.msg.assistant .body { font-size: calc(12.5px * var(--ts, 1)); line-height: 1.72; color: var(--text); }
.msg.user { display: flex; flex-direction: column; align-items: flex-end; }
/* 用户气泡：和 /chat 的 Cylit Agent 同一个口径 —— 强调色淡底、14px 圆角、无描边，
   右下角收一个小角（"我发出去的这一块"朝发送键那侧收紧）。 */
.msg.user .body {
  background: var(--accent-soft);
  color: var(--text);
  border-radius: 14px 14px 4px 14px;
  padding: 9px 14px;
  font-size: calc(13px * var(--ts, 1));
  line-height: 1.68;
  max-width: 88%;
  box-shadow: 0 1px 1px color-mix(in srgb, var(--text) 4%, transparent);
}
.msg.user .sel-quote, .msg.user .atts { align-self: flex-end; }
/* 气泡下面那条（时间 / 复制 / 修改）：**鼠标移到这条消息上才出现**（用户口径 2026-11-19），
   平时整排隐去，不占视觉；`min-height` 保留，hover 出现时不会把下面的内容顶一下。 */
.msg-tools {
  display: flex; align-items: center; gap: 4px; min-height: 20px; margin-top: 3px;
  opacity: 0; transition: opacity var(--dur) var(--ease);
}
/* 轨迹抬头与正文之间：**唯一**一条分界线 —— 「轨迹到此结束，下面是答案」。
   流式与完成态都由它表达（"还在思考"由思考块自己表达，末尾不再另画一条）。
   1px、上下各 10px，高度 0，不是粗线也不带阴影。 */
.run-rule { height: 0; margin: 10px 0; border-top: 1px solid var(--divider); }
.msg.user .msg-tools { justify-content: flex-end; }
.msg:hover .msg-tools, .msg-tools.show { opacity: 1; }
.msg-time { font-size: calc(11px * var(--ts, 1)); color: var(--text-faint); font-variant-numeric: tabular-nums; }
.tool-sep { width: 1px; height: 12px; margin: 0 3px; background: var(--divider); }
.sel-quote {
  display: flex; align-items: center; gap: 4px; max-width: 92%; margin-bottom: 3px;
  padding: 3px 7px; border-left: 2px solid var(--accent);
  background: var(--bg-layer); border-radius: 0 var(--radius-sm) var(--radius-sm) 0;
  font-size: calc(11.5px * var(--ts, 1)); color: var(--text-dim);
}
.sum-card {
  border: 1px dashed var(--border-strong); border-radius: var(--radius);
  background: var(--bg-layer); padding: 7px 9px;
}
.sum-head {
  display: flex; align-items: center; gap: 4px; margin-bottom: 4px;
  font-size: calc(11px * var(--ts, 1)); color: var(--text-faint); letter-spacing: .02em;
}
.sum-card .md :deep(p) { margin: 3px 0; }
/* 正文里的引用锚：`[n]` 就在句子里，点它直接跳（用户口径 2026-10-03）。
   样式已**搬到 `styles/base.css` 的 `.cite-inline`**（用户口径 2026-10-07：正文里的引用标号
   要跟这里长得一样 → 两处共用一份，别再在这里维护第二份）。 */
@keyframes pulse { 0%, 100% { opacity: .35 } 50% { opacity: 1 } }

/* 过程轨迹：抬头一行（用时 + 干了什么），正文里**思考 / 正文 / 工具按发生顺序混排**。
   抬头不是胶囊按钮而是一行文字 —— 它每轮都在，做成白胶囊会把整屏切成一条条横带。
   正文段（`.run-text`）与轨迹外的正文同款排版。
   **字号统一（用户口径 2026-10-25「字体大小不一致」）**：整条轨迹（抬头 / 阶段 / 思考 / 工具 /
   参数 / 秒数）都用正文那一档 `12.5px`，只靠颜色与字重分层次，不再这里 11.5、那里 12.5。 */
.run { margin: 2px 0 6px; }
.run-head {
  display: flex; align-items: center; gap: 5px; width: 100%; text-align: left;
  padding: 2px 0; border: 0; background: none; cursor: pointer;
  color: var(--text-dim); font-size: calc(12.5px * var(--ts, 1));
}
.run-head:hover { color: var(--text); }
/* 折叠箭头：**一枚图标转过去**，不是"右箭头换成下箭头" —— 换字形是硬切，转是连续的
   （轨迹抬头、工具组、思考段三处共用；时长与段内开合一个节奏）。 */
.caret { flex: none; transition: transform .22s cubic-bezier(.22, .61, .36, 1); }
.caret.shut { transform: rotate(-90deg); }
/* 没有可展开内容的一轮（只攒了旁白）：箭头位置留白，抬头文字不跟着往左挪。 */
.caret-gap { width: 12px; flex: none; }
.run-head:not(.openable) { cursor: default; }
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
/* 原来这里有一条"进行中"的竖线呼吸动画（`live-rail`），随竖线一起去掉了 ——
   整轮在跑的信号由抬头那枚脉冲圆点承担（`live-ring`），不需要两条动效抢注意力。 */
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
/* 光标跟在**这一轮最后落下的那段话**后面（正文段或轨迹里的半句都算）。
   节拍照 DSH 的 `caret-blink`：1s、steps(1)、中点为灭。
   正文按块渲染（`.md-part`）后要认准"最后一块"，否则每个块后面都会挂一枚光标。 */
@keyframes caret { 50% { opacity: 0; } }
.msg[data-live="1"] .md-part-tail :deep(> *:last-child)::after,
.msg[data-live="1"] .run-text :deep(> *:last-child)::after {
  content: ""; display: inline-block; width: 6px; height: 12px; margin-left: 3px;
  vertical-align: -2px; border-radius: 1.5px; background: var(--accent);
  animation: caret 1s steps(1) infinite;
}
/* ── 正文的载入动画（照 DSH 的 `reveal-piece`）──
   流式正文按块渲染（`splitAnswerParts`）：已封口的块 DOM 不动，只有最后一块每批重画，
   日志里"整段每 90ms 闪一下"就是这么没的。这里只负责把**新出现的块**与**新长出来的
   那一截**从 .2 淡到 1 —— 160ms、方向柔和的 ease-out，比原来的 120ms `ease-out` 少一点顿挫。 */
/* `display: contents`：块容器自己不生成盒子 —— 排版与"整段 v-html"逐像素一致，
   段落之间的 margin 照常折叠，也不需要在这里重写一套行距。 */
.md-part { display: contents; }
@keyframes reveal-piece { from { opacity: .2; } to { opacity: 1; } }
.msg .body :deep(.reveal-piece),
.msg .run-text :deep(.reveal-piece) { animation: reveal-piece .16s cubic-bezier(.16, .84, .44, 1); }
/* 纯装饰：系统里关掉动效就都不动。**不动的强调还在**（主题色的点、主题色的停止钮）。 */
@media (prefers-reduced-motion: reduce) {
  .run.live .run-body, .run .dot::after,
  .run-step.tool.running::after,
  .run-step .step-spin,
  .msg[data-fresh],
  .msg[data-fresh="user"] .body,
  .msg .body :deep(.reveal-piece),
  .msg .run-text :deep(.reveal-piece),
  .msg[data-live="1"] .md-part-tail :deep(> *:last-child)::after,
  .msg[data-live="1"] .run-text :deep(> *:last-child)::after { animation: none; }
  .run.live .run-title { animation: none; background-position: 50% 0; }
  /* 步骤行的进场 / 让位、轨迹展开收起、思考段的高度过渡：系统里关了动效就都是瞬时的
     （信息一个都不少，只是不再动）。 */
  .run-body, .run-body > *, .run-step, .run-text, .run-step.think .step-text,
  .caret,
  .step-enter-active, .step-leave-active, .step-move,
  .fold-enter-active, .fold-leave-active { animation: none !important; transition: none !important; }
}
.run-body {
  /* 用户口径 2026-12-06：**去掉那条竖线**。它原本是"轨迹是一个整体"的视觉分组，
     但思考段一多就变成一条贴满整屏的线，和正文抢注意力；现在靠缩进 + 更淡的字色分组就够了。
     2026-10-25：**左侧缩进也去掉** —— 轨迹贴着消息列最左边起排，不再往里收 6px。

     2026-10-25（照 DSH 的"过程块"）：轨迹有自己的**限高滚动容器**。一轮里思考/工具可能有几十行，
     全铺开会把正文顶出屏幕；这里给它一个 44vh / 420px 的上限，超了就在块内滚，
     上下边缘按能否滚动渐隐（`can-up` / `can-down` 由 JS 同步，见 `setRunBody`）。
     `overscroll-behavior` **不设 contain**：块内滚到底（/顶）要继续带动外层对话滚动 ——
     用户口径 2026-10-25「滚到思维链底部就该滚整个对话」，链条式滚动是默认行为，别拦它。 */
  display: flex; flex-direction: column; gap: 3px;
  margin: 4px 0 0;
  max-height: min(44vh, 420px);
  overflow-y: auto;
  scrollbar-gutter: stable;
  padding-right: 2px;
  transition: mask-image .12s linear;
}
.run-body.can-up { mask-image: linear-gradient(transparent 0, #000 14px, #000 100%); }
.run-body.can-down { mask-image: linear-gradient(#000 0, #000 calc(100% - 14px), transparent 100%); }
.run-body.can-up.can-down {
  mask-image: linear-gradient(transparent 0, #000 14px, #000 calc(100% - 14px), transparent 100%);
}
/* ── 丝滑过渡（Claude Code 那种：行是一条条长出来的，后面的行让位而不是瞬移）──
   步骤列表是 TransitionGroup：新行 fade + 轻移 12px，move 管插入新行时后面行的让位；
   时长/缓动对齐参考实现（240ms / cubic-bezier(.22,.61,.36,1)）。 */
.step-enter-from { opacity: 0; transform: translateY(12px); }
.step-enter-active { transition: opacity .24s cubic-bezier(.22, .61, .36, 1), transform .24s cubic-bezier(.22, .61, .36, 1); }
.step-leave-to { opacity: 0; transform: translateY(-2px); }
.step-leave-active { transition: opacity .16s var(--ease), transform .16s var(--ease); }
.step-move { transition: transform .24s cubic-bezier(.22, .61, .36, 1); }
/* 段内开合（思考正文 / 工具子行）：进场淡入 + 轻移，离场淡出。
   参数对齐参考实现：进场 200ms ease-out、离场 160ms —— 这就是 DSH 那种"展开长出来、
   收起缩回去"的节奏（高度用 max-height 过渡，见 `.run-step.think .step-text`）。 */
.fold-enter-from, .fold-leave-to { opacity: 0; transform: translateY(-3px); }
.fold-enter-active { transition: opacity .2s ease-out, transform .2s ease-out; }
.fold-leave-active { transition: opacity .16s ease-out, transform .16s ease-out; }
/* ── 工具调用组（照参考实现「代码 / 工具调用」那套）──
   抬头一行 `⌗ 工具 3 次调用`，下面一条**竖引导线**把每次调用串起来；每行一次调用，
   短语 + `·` + 本次参数（截成一行）。整组贴在轨迹左边起排，不额外留白。 */
.tool-group { display: flex; flex-direction: column; gap: 2px; }
.tool-head {
  display: flex; align-items: center; gap: 5px;
  color: var(--text-dim);
  cursor: pointer;
  /* ⚠️ 必须显式给字号：这一行不是 ~.run-step~，靠继承拿到的是 `.msg` 的 **14px**，
     而思考那一行（`.run-step.think`）是 12.5px —— 一行工具调用看着比一行思考"大一号"
     （用户口径 2026-12-14：「思考文字大小和工具的大小不一样」）。 */
  font-size: calc(12.5px * var(--ts, 1));
}
.tool-head:hover .step-tag { color: var(--text); }
.tool-head svg { flex: none; }
.tool-head .group-caret { color: var(--text-faint); }
.tool-count { color: var(--text-faint); flex: none; }
/* 收起时的摘要：这一次都调了哪些工具，一行，超出省略（中性灰，不染主题色） */
.tool-sum {
  min-width: 0; overflow: hidden; white-space: nowrap; text-overflow: ellipsis;
  color: var(--text-dim); font-size: calc(12.5px * var(--ts, 1));
}
.tool-lines {
  display: flex; flex-direction: column; gap: 2px;
  margin-left: 5px; padding-left: 9px;
  border-left: .5px solid var(--divider);
}
.tool-lines .run-step { padding: 0 2px; border-radius: 3px; }
.run-step {
  /* `flex-start`：图标/标签钉在**第一行**上（用 baseline/center 时，多行思考的图标会飘到
     整段中间，看着不像"图标与文字并排"，用户 2026-10-03 报过） */
  display: flex; align-items: flex-start; gap: 5px;
  font-size: calc(12.5px * var(--ts, 1)); line-height: 1.65; color: var(--text-faint);
}
.run-step :deep(svg), .run-step svg { flex: none; margin-top: 2px; }
.run-step .step-tag { color: var(--text-dim); flex: none; }
.run-step .step-arg {
  color: var(--text-faint); overflow-wrap: anywhere;
  /* 参数摘要**截成一行**（参考实现也是一行 + 省略号）：一次调用占一行，
     否则一条工具行能把整屏吃掉，轨迹就不成"轨迹"了。完整内容在 title 里。 */
  min-width: 0; flex: 1 1 auto;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.run-step .step-ms { margin-left: auto; padding-left: 8px; flex: none; font-family: var(--mono); font-size: calc(11px * var(--ts, 1)); }
.run-step.tool.running .step-tag { color: var(--accent-text); }
/* 运行中的工具行：一条 1.5s / 延迟 .3s 的高光扫过（前 2/3 走完，后 1/3 停住）——
   参数照抄参考实现的 row-shimmer。只在「正在跑」的那一行出现，跑完立刻消失。 */
.run-step.tool.running { position: relative; overflow: hidden; }
.run-step.tool.running::after {
  content: ""; position: absolute; inset: 0; pointer-events: none;
  background: linear-gradient(90deg, transparent 0%,
    color-mix(in srgb, var(--text) 9%, transparent) 45%, transparent 100%);
  transform: translateX(-100%);
  animation: step-sweep 1.5s linear .3s infinite;
}
@keyframes step-sweep {
  0% { transform: translateX(-100%); }
  66.667%, 100% { transform: translateX(100%); }
}
/* 工具行的加载环：850ms 一圈（与 /chat 的思维链同速）。跑着时替掉工具图标，
   比"只把字染成主题色"更明确地表达"这一步在干活"。 */
.run-step .step-spin {
  width: 11px; height: 11px; flex: none; margin-top: 2px;
  color: var(--accent);
  animation: step-spin .85s linear infinite;
}
.run-step .step-spin circle {
  fill: none; stroke: currentColor; stroke-width: 1.6; stroke-linecap: round;
  stroke-dasharray: 3.2 6.1 3.2 6.1 3.2 6.1 3.2 6.1 3.2 6.1;
}
@keyframes step-spin { to { transform: rotate(360deg); } }
/* 成败标记：跑完给一个淡勾，失败换成红色的感叹（原因直接写在 `step-arg` 里） */
.run-step .step-flag { flex: none; margin-left: 4px; opacity: .55; }
.run-step .step-flag.ok { color: var(--ok-text, var(--accent-text)); }
.run-step .step-flag.bad { opacity: 1; color: var(--danger-text, #c0392b); }
.run-step.tool.bad .step-tag, .run-step.tool.bad .step-arg { color: var(--danger-text, #c0392b); }
/* 阶段旁白不进轨迹（见 `traceRows`），这条样式只留给"万一"：比步骤再淡一档。 */
.run-step.stage .step-text { color: var(--text-faint); font-style: italic; }
/* 思考段：**第一行只有「⌄ ✦ 思考」**（收起时后面跟一行摘要），正文换行落在最左边
   （不缩进到标签下面，用户口径 2026-10-25：思考和工具内容左对齐、左侧不留白）。
   默认展开（过程正文要留着）；点一下收成摘要那一行。 */
.run-step.think {
  cursor: pointer;
  flex-direction: column;
  align-items: stretch;
  gap: 1px;
}
.run-step.think .think-lead {
  display: flex; align-items: center; gap: 5px;
  flex: none;
}
.run-step.think .think-lead svg { margin-top: 0; }
.run-step.think:hover .step-tag { color: var(--text); }
.run-step.think .step-caret { color: var(--text-faint); }
.run-step.think .think-sep {
  width: 2px; height: 2px; margin: 0 1px 0 3px; border-radius: 1px;
  background: var(--text-faint); flex: none;
}
/* 收起时那一行摘要：**中性灰**（用户口径 2026-10-25「移除折叠后的颜色」：
   折叠态不染主题色、不留底色，只靠"淡一档"表达它是过程） */
.run-step.think .think-sum {
  min-width: 0; overflow: hidden; white-space: nowrap; text-overflow: ellipsis;
  color: var(--text-dim); font-size: calc(12.5px * var(--ts, 1));
}
/* 思考正文是**一段中文正文**，不是代码：不用等宽字体（等宽在这里会退化成宋体观感），
   与正文同一套字形，只比正文淡一档、小一档。 */
.run-step.think .step-text {
  display: block;
  font-family: inherit;
  font-size: calc(12.5px * var(--ts, 1));
  line-height: 1.7;
  color: var(--text-dim);
  white-space: pre-wrap; overflow-wrap: anywhere;
  transition: max-height .2s ease-out;
}
.run-step.think.open .step-text { display: block; -webkit-line-clamp: unset; }
/* 流式里「还在写的最后一段思考」：高度完全放开（边写边长，不跟过渡抢） */
.run-step.think.autogrow .step-text { max-height: none; }
.run-text { margin: 2px 0; }
.run-text :deep(p) { margin: 5px 0; }
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
  font-size: calc(13px * var(--ts, 1)); font-weight: 500; line-height: 1.7;
  user-select: text;
}

/* 这一块留给"接着可以问"：试过用模型生成，但那一轮与主对话的缓存前缀毫无重叠，
   等于每轮多打一次冷启动 API，而接着聊本来就能解决 → 整块拿掉了。 */

.atts { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 5px; }
.atts.pending { margin: 0 0 6px; align-items: center; }
/* 回答下面那排色点（解释那一轮"存为笔记"）：一排小圆点 + 一句淡字，不抢正文。
   色块本身是 `base.css` 的 `.hl-swatch`（与划词胶囊同一份颜色），这里只管排布。 */
.note-swatches { display: flex; align-items: center; gap: 6px; margin-top: 6px; }
.note-swatches .hl-swatch { border: 0; }
.ns-hint { font-size: calc(11px * var(--ts, 1)); color: var(--text-faint); }
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
/* 「回到最新」：浮在输入区上方右侧的一枚圆钮（用户口径 2026-12-03）。
   `--composer-h` 由 ResizeObserver 实测写入（输入框会长高），所以不用写死位置。
   流式输出时多一个呼吸的小点，表示「下面还在长」。 */
.to-bottom {
  position: absolute; right: 14px; bottom: calc(var(--composer-h, 112px) + 10px); z-index: 4;
  width: 32px; height: 32px; display: inline-flex; align-items: center; justify-content: center;
  border: 1px solid var(--border); border-radius: 999px;
  background: var(--bg-elev); color: var(--accent-text);
  box-shadow: var(--shadow-2); cursor: pointer;
  transition: background var(--dur) var(--ease), color var(--dur) var(--ease),
    transform var(--dur) var(--ease);
  animation: to-bottom-in .18s var(--ease);
}
.to-bottom:hover { background: var(--accent-soft); color: var(--accent-hover); }
.to-bottom:active { transform: scale(.94); }
.to-bottom:focus-visible { outline: none; box-shadow: var(--ring); }
.to-bottom-live {
  position: absolute; top: 4px; right: 4px; width: 6px; height: 6px; border-radius: 999px;
  background: var(--accent); animation: to-bottom-pulse 1.1s ease-in-out infinite;
}
@keyframes to-bottom-in { from { opacity: 0; transform: translateY(6px); } }
@keyframes to-bottom-pulse { 0%, 100% { opacity: .35; } 50% { opacity: 1; } }

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
  font: inherit; font-size: calc(11.5px * var(--ts, 1));
  transition: background var(--dur) var(--ease), color var(--dur) var(--ease);
}
.sel-chip-main:hover { background: color-mix(in srgb, var(--accent) 15%, transparent); }
.sel-chip-main .truncate { flex: 1; min-width: 0; }
/* 位置那一小段（"第 4 页 · Markdown"）：比正文小一号、后面一条细竖线隔开，
   不抢原文的位置，但一眼能看出这段是从哪儿来的。 */
.sel-chip-where {
  flex: none; font-size: calc(10.5px * var(--ts, 1)); letter-spacing: .01em; opacity: .85;
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
  color: var(--text); font-family: inherit; font-size: calc(13px * var(--ts, 1)); line-height: 1.6; padding: 4px 7px;
  box-sizing: border-box;
}
.composer-input::placeholder { color: var(--text-faint); }
.edit-box { display: flex; flex-direction: column; gap: 5px; padding: 2px; }
.edit-head { display: inline-flex; align-items: center; gap: 4px; }
.edit-acts { display: flex; gap: 5px; padding-bottom: 2px; }
.composer-bar { display: flex; align-items: center; gap: 4px; margin-top: 4px; }
/* 输入框**外面**那一行：模型 + 圆环（用户口径：这两个不放进输入框里） */
.composer-foot { position: relative; display: flex; align-items: center; gap: 6px; padding: 6px 4px 0; }
/* 模型名：**低调**（幽灵小字 + 一枚箭头），点它就地换模型 / 推理等级 */
.model-btn {
  display: inline-flex; align-items: center; gap: 1px; max-width: 55%; min-width: 0;
  border: 0; background: none; cursor: pointer; padding: 2px 4px;
  border-radius: var(--radius-ctl); color: var(--text-faint); font: inherit; font-size: calc(11.5px * var(--ts, 1));
}
.model-btn:hover, .model-btn.on { background: var(--bg-hover); color: var(--text-dim); }

/* 底部明细里的两个读数（缓存命中率 / 上下文占比）：比模型名更弱一档，等宽数字，
   加减/流式刷新时不会把邻居推来推去。 */
.foot-stat {
  flex: none;
  font-size: calc(11.5px * var(--ts, 1));
  font-variant-numeric: tabular-nums;
  color: var(--text-faint);
  white-space: nowrap;
  cursor: default;
}
.foot-pct { margin-left: 1px; }

/* ── 模型 / 推理等级浮层 ──
   左右都贴住这一行（left/right 归零）再用 max-width 收窄：胶囊在最左边，浮层向右长多少
   都出不了输入区 —— 浮层模式最窄只有 320px，溢出到面板外就点不到了。 */
/* 宽度**按内容自适应**（用户口径）：原来 right: 0 让它拉满整行输入区的宽度，
   而两行内容的右边是一片空地 —— 看着像"左右留白很大"。现在 max-content 收边，
   内容长了（模型名很长）才被 max-width 截住、由 .pick-val 的 truncate 收尾。
   min-width 兜住"名字很短"的极端，免得浮层小到像一枚标签。 */
.pick-pop {
  position: absolute; left: 0; right: auto; bottom: calc(100% + 5px); z-index: 9;
  width: max-content; min-width: 176px; max-width: 288px;
  display: flex; flex-direction: column; gap: 1px; padding: 3px;
  border-radius: 11px;
  /* 材质（底色 / 模糊 / 阴影 / 无描边）走 `base.css` 的 `.glass-menu`，与「更多」菜单同一块玻璃 */
}
.pick-row {
  display: flex; align-items: center; gap: 4px; width: 100%; height: 26px; padding: 0 6px;
  border: 0; border-radius: 8px; background: transparent; color: var(--text-dim);
  font: inherit; font-size: calc(12px * var(--ts, 1)); text-align: left; cursor: pointer; flex: none;
}
.pick-row:hover, .pick-row.on { background: var(--bg-hover); color: var(--text); }
.pick-key { flex: none; color: var(--text-faint); }
.pick-row.on .pick-key { color: var(--text-dim); }
.pick-val { flex: 1; min-width: 0; color: var(--text); }
.pick-row.on .pick-val { color: var(--accent-text); }
.pick-pop svg { color: var(--text-faint); flex: none; }
.pick-row.on svg { color: var(--accent-text); }
/* 第二级：**侧向飞出**的子菜单。`position: fixed`（坐标由被点那行的 rect 算出），
   压在遮罩(8)与根浮层(9)之上；宽度定死 200px，内容长了在列表内部滚。 */
.pick-sub {
  /* 传送到 body 之后按**全局**层级比高低 —— 所以它的 z 是**算出来的**（见 `subZOf`：
     对话卡当前的计算 z + 2，模板里写成行内样式）；这里的 92 只是取不到卡片时的兜底。
     以前写死 90（注释算的是"高于浮窗 60/62、角把手 61/63"），而浮窗那一档来自 ReaderView 的
     `nextLayerZ`（从 60 起、每次聚焦 +1、**没有上限**）—— 点开三十多次浮层之后 90 就被反超，
     子菜单被自己的卡片压住（用户 2026-12-08 的截图）。
     仍然低于弹窗遮罩（150）、底部操作表（319/320）与选区手柄（330）：子菜单只该压住
     正文里的浮层与指示器，不该压住真正"要你确认"的东西。 */
  position: fixed; z-index: 92; width: 200px; box-sizing: border-box; isolation: isolate;
  padding: 3px; border-radius: 11px;
  /* 同上：材质由 `.glass-menu` 给，这里只管几何与层级 */
}
/* 下拉框本体：定高 + 内部滚动（模型多了不能把浮层顶出屏幕） */
.pick-pop-model { width: 280px; }
.pick-model-list { max-height: 46vh; overflow-y: auto; padding: 4px 0; }
.pick-sub-list {
  display: flex; flex-direction: column; gap: 1px; max-height: 240px; overflow-y: auto;
}
/* 厂商图标（对话页胶囊与子菜单每一项共用）：固定视觉框 + `object-fit` 保比例、不拉伸 */
.pv-ico { flex: none; width: 14px; height: 14px; object-fit: contain; border-radius: 3px; }
.pick-item {
  display: flex; align-items: center; gap: 4px; width: 100%; padding: 4px 6px;
  border: 0; border-radius: 8px; background: transparent; color: var(--text-dim);
  font: inherit; text-align: left; cursor: pointer;
}
.pick-item:hover:not(:disabled) { background: var(--bg-hover); }
.pick-item.on { background: var(--accent-soft); }
.pick-item.on svg, .pick-item.on .pick-item-name { color: var(--accent-text); }
.pick-item:disabled { cursor: default; }
.pick-item-main { display: flex; flex-direction: column; gap: 0; flex: 1; min-width: 0; }
.pick-item-name {
  display: flex; align-items: center; gap: 4px; min-width: 0;
  font-size: calc(12px * var(--ts, 1)); line-height: 1.35; color: var(--text);
}
/* 副行：provider · model_name（小一号、退一档色，只做识别用） */
.pick-item-sub { font-size: calc(10px * var(--ts, 1)); line-height: 1.25; color: var(--text-faint); }
.pick-empty { display: flex; flex-direction: column; gap: 0; padding: 4px 6px; }
.pick-empty .pick-item-name { font-size: calc(12px * var(--ts, 1)); color: var(--text-dim); }
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
/* （用户口径 2026-11-26 取代 2026-10-15 那份"实心圆钮"）：现在是**描边圆环 + 实心方块**，
   两样都取主题色。描边 2px 是照参考图的比例（环粗 ≈ 直径的 1/13），1px 在浅底上偏虚。 */
.btn.stop {
  background: transparent; border-color: var(--accent); color: var(--accent);
  border-width: 2px; box-shadow: none;
}
.btn.stop:hover:not(:disabled) {
  background: color-mix(in srgb, var(--accent) 9%, transparent);
  border-color: var(--accent-hover); color: var(--accent-hover);
}

.drop-hint {
  position: absolute; inset: 6px; z-index: 5; display: flex; align-items: center; justify-content: center;
  gap: 6px; border: 1.5px dashed var(--accent); border-radius: var(--radius);
  background: color-mix(in srgb, var(--accent-soft) 88%, transparent);
  color: var(--accent-text); font-size: calc(12.5px * var(--ts, 1)); pointer-events: none;
}
</style>
