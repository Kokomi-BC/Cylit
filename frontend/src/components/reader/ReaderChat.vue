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
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue';
import { api, assetUrl } from '@/api/client';
import { postSse } from '@/api/stream';
import { mdToHtml } from '@/composables/useMarkdown';
import { reportError, toast } from '@/stores/app';
import { debounce, fmtRelative, truncate } from '@/utils/format';
import { convCount } from '@/api/types';
import type {
  AgentAttachment, AgentCitation, AgentConversation, AgentMessage, AgentSelection,
  AgentStatus, AgentUsage,
} from '@/api/types';
import AppIcon from '@/components/AppIcon.vue';
import ConfirmModal from '@/components/ConfirmModal.vue';

const props = defineProps<{
  paperId: string;
  /** 从正文划词带过来的选区（可空）；发出去之后由父组件清掉 */
  selection?: AgentSelection | null;
}>();

const emit = defineEmits<{
  (e: 'focus', blockId: string, page?: number): void;
  (e: 'clear-selection'): void;
}>();

const RING_CIRCUMFERENCE = 2 * Math.PI * 15.5;
/** 空态文案池里"与大肥鱼一起讨论…"的固定前缀（大肥鱼 = 这位问答助手的名字） */
const PERSONA = '与大肥鱼一起讨论';

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
/** `paper` = 只看这篇论文（默认）；`all` = 整个书架 */
const convScope = ref<'paper' | 'all'>('paper');
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
const uploading = ref(false);
const loading = ref(false);
const compressing = ref(false);
const sending = ref(false);
const streamText = ref('');
const streamNote = ref('');
const streamCitations = ref<AgentCitation[]>([]);
/** 思考阶段：正文按 `thinking_delta` 单独收（**不混进回答**），跑表以服务端的 `ms` 为准 */
const streamThinking = ref('');
const thinkingMs = ref(0);
const thinkingOpen = ref(true);
/** 展开着的历史思考块（按消息 id 记；默认都折叠） */
const openThinking = ref<string[]>([]);
const errorText = ref('');
const editing = ref<{ id: string; text: string } | null>(null);
const headline = ref('');
const dragging = ref(false);
const confirming = ref<{ ids: string[]; body: string; target: string } | null>(null);
const confirmBusy = ref(false);
const msgsEl = ref<HTMLElement | null>(null);
const inputEl = ref<HTMLTextAreaElement | null>(null);
const fileInput = ref<HTMLInputElement | null>(null);
let controller: AbortController | null = null;

const current = computed(() => conversations.value.find(c => c.conversation_id === currentId.value) ?? null);
const currentTitle = computed(() => current.value?.title || '新对话');
const percent = computed(() => Math.max(0, Math.min(100, usage.value?.percent ?? 0)));
const ringDash = computed(() => `${(percent.value / 100) * RING_CIRCUMFERENCE} ${RING_CIRCUMFERENCE}`);
const modelName = computed(() => status.value?.model?.model_name || '—');
const agentReady = computed(() => Boolean(status.value?.running) && status.value?.model?.usable !== false);
const canSend = computed(() => !sending.value
  && (draft.value.trim().length > 0 || pending.value.length > 0 || Boolean(props.selection?.text)));
const canCompress = computed(() => !compressing.value && messages.value.length >= 2);
const compressTitle = computed(() => (canCompress.value
  ? '压缩这段对话：让模型把已谈过的内容压成要点（会多出一轮「摘要」，原文仍可回看）'
  : '至少聊过一轮才能压缩'));
const allPicked = computed(() => conversations.value.length > 0
  && picked.value.length === conversations.value.length);

function render(text?: string | null) {
  return mdToHtml(text ?? '', props.paperId);
}

/** 划词那种以 `> 引用` 开头的用户消息：正文只显示引用之后的部分（引用单独排版）。 */
function splitQuote(content: string): { quote: string; rest: string } {
  const match = /^>\s?(.*)\n+([\s\S]*)$/.exec(content || '');
  if (!match) return { quote: '', rest: content || '' };
  return { quote: match[1].trim(), rest: match[2].trim() };
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

function isThinkingOpen(id: string) {
  return openThinking.value.includes(id);
}

function toggleThinking(id: string) {
  openThinking.value = isThinkingOpen(id)
    ? openThinking.value.filter(x => x !== id)
    : [...openThinking.value, id];
}

/* ------------------------------ 空态文案 ------------------------------ */

const WORD_STOP = new Set(['a', 'an', 'the', 'of', 'for', 'and', 'with', 'on', 'in', 'to',
  'via', 'using', 'based', 'towards', 'toward', 'is', 'are', 'you', 'need', 'can', 'we', 'it']);

/** 论文标签（与标题一起当关键词的来源） */
const paperTagText = ref('');

/** 从论文标题 / 标签里抽几个关键词（"与大肥鱼一起讨论 XX 的实现"里的 XX）。 */
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
  if (el) el.scrollTop = el.scrollHeight;
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

async function loadConversations() {
  listLoading.value = true;
  try {
    const data = await api.get<{ items: AgentConversation[] }>('/agent/conversations', {
      paper_id: convScope.value === 'paper' ? props.paperId : undefined,
      q: convQuery.value.trim() || undefined,
      kind: convKind.value === 'all' ? undefined : convKind.value,
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
    await scrollToBottom();
  } catch (err) {
    reportError(err, '读取会话失败');
  } finally {
    loading.value = false;
  }
}

async function openConversation(id: string) {
  level.value = 'thread';
  if (id === currentId.value && messages.value.length) return;
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
  void nextTick(() => inputEl.value?.focus());
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

function toggleScope() {
  convScope.value = convScope.value === 'paper' ? 'all' : 'paper';
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
  if (name === 'stage') { streamNote.value = data?.message || ''; return; }
  if (name === 'tool') {
    if (data?.status === 'start') streamNote.value = `${toolLabel(data.tool)}${data.args_digest ? ` · ${data.args_digest}` : ''}`;
    return;
  }
  if (name === 'thinking_delta') {
    streamThinking.value += data?.text ?? '';
    thinkingMs.value = Number(data?.ms || thinkingMs.value);
    return;
  }
  if (name === 'thinking') {
    // 思考阶段结束（正文开始了）：把跑表停在服务端给的权威值上，并按惯例收起思考
    thinkingMs.value = Number(data?.ms || thinkingMs.value);
    thinkingOpen.value = false;
    return;
  }
  if (name === 'answer_delta') {
    streamText.value += data?.text ?? '';
    streamNote.value = '';
    void scrollToBottom();
    return;
  }
  if (name === 'citation' && data?.ref) {
    if (!streamCitations.value.some(c => c.ref === data.ref)) streamCitations.value.push(data as AgentCitation);
    return;
  }
  if (name === 'done') {
    streamText.value = data?.answer ?? streamText.value;
    streamCitations.value = data?.citations ?? streamCitations.value;
    if (data?.thinking_ms) thinkingMs.value = Number(data.thinking_ms);
    if (data?.incomplete_reason) toast(`回答未完成：${data.incomplete_reason}`, 'warn');
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
};
const toolLabel = (name: string) => TOOL_LABELS[name] ?? `正在调用 ${name}`;

/**
 * 一轮问答。三种入口共用一条流：
 * - `send`：正常提问（可带选区 / 图片）；
 * - `regen`：重新生成某条回答（**不写**用户消息，重跑原来那个问题）；
 * - `edit`：改掉某条提问（新问题挂在原提问的父下面，与它成为兄弟）。
 */
async function ask(mode: 'send' | 'regen' | 'edit' = 'send', target?: AgentMessage) {
  if (sending.value) return;
  if (!agentReady.value) { toast('问答服务没有在运行（先 `pr.py agent`）', 'warn'); return; }

  const question = mode === 'edit' ? (editing.value?.text ?? '').trim() : draft.value.trim();
  const selection = mode === 'send' && props.selection?.text ? props.selection : null;
  const attachments = mode === 'send' ? pending.value.map(a => a.attachment_id) : [];
  if (mode === 'edit' && !question) { toast('问题不能为空', 'warn'); return; }
  if (mode === 'send' && !question && !selection?.text && !attachments.length) return;
  if (mode !== 'send') {
    level.value = 'thread';
    if (!currentId.value) return;
  }

  errorText.value = '';
  sending.value = true;
  streamText.value = '';
  streamCitations.value = [];
  streamThinking.value = '';
  thinkingMs.value = 0;
  thinkingOpen.value = true;
  streamNote.value = mode === 'send' ? '正在准备…' : (mode === 'regen' ? '正在重新生成…' : '正在重新提问…');
  const optimistic: AgentMessage | null = mode === 'send' ? {
    message_id: `local-${Date.now()}`, conversation_id: currentId.value ?? '', seq: 0,
    role: 'user',
    content: selection ? `> ${selection.text.slice(0, 400)}\n\n${question || '解释这段内容'}` : question,
    attachments: pending.value,
  } : null;
  if (optimistic) {
    messages.value = [...messages.value, optimistic];
    draft.value = '';
  }
  if (mode === 'send' && selection) emit('clear-selection');
  if (mode === 'send') pending.value = [];
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
      } : null,
      attachments,
      regenerate_from: mode === 'regen' ? target?.message_id : null,
      edit_of: mode === 'edit' ? target?.message_id : null,
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
    streamText.value = '';
    streamCitations.value = [];
    streamThinking.value = '';
    streamNote.value = '';
    controller = null;
  }
}

function stop() {
  controller?.abort();
  toast('已停止', 'warn');
}

function startEdit(message: AgentMessage) {
  const { rest } = splitQuote(message.content);
  editing.value = { id: message.message_id, text: rest };
  level.value = 'thread';
}

/** 重新生成：`target` 是那条**回答**（后端会找到它的父提问重跑）。 */
function regenerate(target: AgentMessage) {
  void ask('regen', target);
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

/** 点空态那条文案 = 填进输入框（用户还能改，不直接发出去）。 */
function fillDraft(text: string) {
  draft.value = text;
  void nextTick(() => inputEl.value?.focus());
}

/** 点圆环 = 压缩（多一轮会话：模型把已谈过的内容压成要点，存成一条 summary）。 */
async function compress() {
  if (!canCompress.value || !currentId.value) return;
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

onMounted(async () => {
  await Promise.all([loadStatus(), loadPaper()]);
  await loadConversations();
});

onUnmounted(() => controller?.abort());

watch(() => props.paperId, async () => {
  controller?.abort();
  level.value = 'list';
  messages.value = [];
  currentId.value = null;
  pending.value = [];
  await loadPaper();
  await loadConversations();
});

// 划词带进来的选区：直接进二级页（列表页没有输入框，留在那儿用户会以为没反应）。
// 监听**对象本身**而不是文本：同一段文字连点两次也要能切过去。
watch(() => props.selection, (sel) => {
  if (sel?.text) {
    level.value = 'thread';
    void nextTick(() => inputEl.value?.focus());
  }
});
</script>

<template>
  <div class="chat" @dragover.prevent="dragging = true" @dragleave="dragging = false"
       @drop.prevent="onDrop">
    <!-- ═══════════════ 一级：会话列表 ═══════════════ -->
    <template v-if="level === 'list'">
      <div class="list-head">
        <div class="search">
          <AppIcon name="search" :size="13" />
          <input v-model="convQuery" class="search-input" placeholder="搜索对话（标题或内容）"
                 @input="searchLater" @keydown.esc="convQuery = ''; loadConversations()" />
          <button v-if="convQuery" class="ico" title="清空" @click="convQuery = ''; loadConversations()">
            <AppIcon name="close" :size="11" />
          </button>
        </div>
        <button class="plus" title="新建对话" @click="startNew"><AppIcon name="add" :size="16" /></button>
      </div>

      <div class="filters">
        <div class="seg">
          <button v-for="k in KINDS" :key="k.key" class="seg-btn" :class="{ on: convKind === k.key }"
                  @click="setKind(k.key)">{{ k.label }}</button>
        </div>
        <span class="spacer" />
        <button class="chip-btn" :class="{ on: convScope === 'all' }"
                :title="convScope === 'paper' ? '只看这篇论文的对话' : '看整个书架上的对话'"
                @click="toggleScope">
          <AppIcon name="library" :size="12" />{{ convScope === 'paper' ? '本论文' : '全部' }}
        </button>
        <button class="chip-btn" :class="{ on: batch }" title="批量管理" @click="toggleBatch">
          <AppIcon name="checkbox" :size="12" />管理
        </button>
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
            <AppIcon name="checkbox" :size="14" />
          </button>

          <input v-if="renaming?.id === c.conversation_id" v-model="renaming.text" class="rename-input"
                 @keydown.enter="submitRename" @keydown.esc="renaming = null" @blur="submitRename" />

          <button v-else class="row-main" @click="batch ? togglePick(c) : openConversation(c.conversation_id)">
            <span class="row-title truncate">{{ c.title || '未命名对话' }}</span>
            <span class="row-meta">
              <span v-if="c.kind === 'selection'" class="kd" title="划词对话"><AppIcon name="quote" :size="10" />划词</span>
              <span v-if="convScope === 'all' && c.paper_id && c.paper_id !== props.paperId"
                    class="faint mono tiny truncate">{{ c.paper_id }}</span>
              <span class="tiny faint">{{ convCount(c) }} 条 · {{ fmtRelative(c.updated_at) }}</span>
            </span>
          </button>

          <div v-if="!batch" class="row-acts">
            <button class="ico" title="重命名" @click.stop="startRename(c)"><AppIcon name="edit" :size="12" /></button>
            <button class="ico danger" title="删除" @click.stop="askDeleteOne(c)"><AppIcon name="trash" :size="12" /></button>
          </div>
        </div>
      </div>

      <div v-if="batch" class="batch-bar">
        <button class="chip-btn" @click="toggleAll">{{ allPicked ? '取消全选' : '全选' }}</button>
        <span class="tiny faint">已选 {{ picked.length }}</span>
        <span class="spacer" />
        <button class="btn sm danger" :disabled="!picked.length" @click="askDeletePicked">删除</button>
      </div>
    </template>

    <!-- ═══════════════ 二级：会话 ═══════════════ -->
    <template v-else>
      <div class="thread-head">
        <button class="ico" title="返回对话列表" @click="backToList"><AppIcon name="back" :size="14" /></button>
        <span class="th-title truncate" :title="currentTitle">{{ currentTitle }}</span>
        <span class="spacer" />
        <button v-if="current" class="ico" title="重命名" @click="startRename(current)"><AppIcon name="edit" :size="13" /></button>
        <button v-if="current" class="ico danger" title="删除这个对话" @click="askDeleteOne(current)"><AppIcon name="trash" :size="13" /></button>
        <button class="ico" title="新建对话" @click="startNew"><AppIcon name="add" :size="15" /></button>
      </div>

      <div ref="msgsEl" class="msgs">
        <div v-if="loading" class="empty small">读取会话…</div>

        <div v-else-if="!messages.length && !sending" class="hello">
          <span class="hollow-ico"><AppIcon name="chat" :size="18" /></span>
          <button class="hello-line" title="点一下填进输入框" @click="fillDraft(headline)">
            {{ headline }}
          </button>
          <div class="hello-acts">
            <button class="chip-btn" @click="rollCopy()"><AppIcon name="refresh" :size="11" />换一条</button>
            <button class="chip-btn" @click="pickFiles()"><AppIcon name="image" :size="11" />上传图片</button>
          </div>
          <div class="tiny faint">正文里划词也能问；图片可以直接粘贴进来。</div>
        </div>

        <div v-for="m in messages" :key="m.message_id" class="msg" :class="m.role">
          <!-- 压缩留痕：对话里多出来的一轮 -->
          <div v-if="m.role === 'summary'" class="sum-card">
            <div class="sum-head"><AppIcon name="layers" :size="12" />已压缩 · 前面的内容在这里</div>
            <div class="md small" v-html="render(m.content)" />
          </div>

          <template v-else>
            <div class="msg-tools" :class="{ show: (m.siblings?.length ?? 0) > 1 }">
              <template v-if="(m.siblings?.length ?? 0) > 1">
                <button class="ico" title="上一个版本" @click="switchBranch(m, -1)"><AppIcon name="back" :size="11" /></button>
                <span class="tiny faint mono">{{ m.sibling_index }}/{{ m.siblings?.length }}</span>
                <button class="ico" title="下一个版本" @click="switchBranch(m, 1)"><AppIcon name="forward" :size="11" /></button>
              </template>
              <span class="spacer" />
              <button v-if="m.role === 'assistant'" class="ico" title="重新生成（新分支）"
                      :disabled="sending" @click="regenerate(m)"><AppIcon name="refresh" :size="12" /></button>
              <button v-if="m.role === 'user'" class="ico" title="编辑提问（新分支）"
                      :disabled="sending" @click="startEdit(m)"><AppIcon name="edit" :size="12" /></button>
              <button class="ico" title="复制" @click="copyMessage(m)"><AppIcon name="copy" :size="12" /></button>
            </div>

            <div v-if="m.role === 'user' && splitQuote(m.content).quote" class="sel-quote">
              <AppIcon name="quote" :size="11" />
              <span class="truncate">{{ splitQuote(m.content).quote }}</span>
            </div>

            <!-- 深度思考：默认折叠，点抬头展开；耗时取服务端记的那一次 -->
            <div v-if="m.thinking" class="think" :class="{ open: isThinkingOpen(m.message_id) }">
              <button class="think-head" @click="toggleThinking(m.message_id)">
                <AppIcon :name="isThinkingOpen(m.message_id) ? 'chevron-down' : 'chevron-right'" :size="11" />
                <AppIcon name="sparkle" :size="11" />
                <span>深度思考{{ m.thinking_ms ? ` · ${fmtMs(m.thinking_ms)}` : '' }}</span>
              </button>
              <pre v-if="isThinkingOpen(m.message_id)" class="think-body">{{ m.thinking }}</pre>
            </div>

            <div class="md body" v-html="render(m.role === 'user' ? splitQuote(m.content).rest : m.content)" />

            <div v-if="m.attachments?.length" class="atts">
              <a v-for="a in m.attachments" :key="a.attachment_id" class="att"
                 :href="imageSrc(a.url)" target="_blank" rel="noopener" :title="`${a.name} · ${fmtSize(a.bytes)}`">
                <img :src="imageSrc(a.url)" :alt="a.name" loading="lazy" />
              </a>
            </div>

            <div v-if="m.citations?.length" class="cites">
              <button v-for="c in m.citations" :key="c.ref" class="cite" :title="c.snippet || c.label"
                      @click="gotoCitation(c)">
                [{{ c.ref }}]<span v-if="c.page" class="faint"> p{{ c.page }}</span>
              </button>
            </div>
          </template>
        </div>

        <!-- 正在生成 -->
        <div v-if="sending || streamText" class="msg assistant live">
          <!-- 思考中（正文还没出来时它才是主角）：跑表以服务端推的 ms 为准 -->
          <div v-if="streamThinking" class="think live" :class="{ open: thinkingOpen }">
            <button class="think-head" @click="thinkingOpen = !thinkingOpen">
              <AppIcon :name="thinkingOpen ? 'chevron-down' : 'chevron-right'" :size="11" />
              <span class="dot" />
              <span>{{ streamText ? '深度思考' : '正在深度思考' }}{{ thinkingMs ? ` · ${fmtMs(thinkingMs)}` : '' }}</span>
            </button>
            <pre v-if="thinkingOpen" class="think-body">{{ streamThinking }}</pre>
          </div>
          <div v-if="streamText" class="md body" v-html="render(streamText)" />
          <div v-if="streamNote" class="note"><span class="dot" />{{ streamNote }}</div>
          <div v-if="streamCitations.length" class="cites">
            <span v-for="c in streamCitations" :key="c.ref" class="cite" :title="c.snippet">[{{ c.ref }}]</span>
          </div>
        </div>

        <div v-if="errorText" class="err small">{{ errorText }}</div>
      </div>

      <!-- 输入区 -->
      <div class="composer">
        <div v-if="props.selection?.text" class="sel-chip">
          <AppIcon name="quote" :size="11" />
          <span class="truncate">{{ props.selection.text }}</span>
          <button class="ico" title="取消选区" @click="emit('clear-selection')"><AppIcon name="close" :size="11" /></button>
        </div>

        <div v-if="pending.length" class="atts pending">
          <div v-for="(a, i) in pending" :key="a.attachment_id" class="att">
            <img :src="imageSrc(a.url)" :alt="a.name" />
            <button class="att-x" title="移除" @click="dropPending(i)"><AppIcon name="close" :size="10" /></button>
          </div>
          <span class="tiny faint">{{ pending.length }} 张</span>
        </div>

        <div v-if="editing" class="edit-box">
          <div class="edit-head tiny faint"><AppIcon name="edit" :size="11" />编辑这条提问（会生成一个新分支，原来的保留）</div>
          <textarea v-model="editing.text" class="input" rows="2" />
          <div class="edit-acts">
            <span class="spacer" />
            <button class="btn sm" @click="editing = null">取消</button>
            <button class="btn sm primary" :disabled="sending || !editing.text.trim()"
                    @click="ask('edit', messages.find(x => x.message_id === editing?.id))">重新提问</button>
          </div>
        </div>

        <template v-else>
          <textarea ref="inputEl" v-model="draft" class="input" rows="1" placeholder="请输入问题"
                    @paste="onPaste" @keydown.enter.exact.prevent="ask('send')" />
          <div class="composer-bar">
            <button class="plus" :class="{ busy: uploading }" :title="`上传图片（也可以直接粘贴，张数不限）`"
                    @click="pickFiles"><AppIcon name="add" :size="15" /></button>
            <input ref="fileInput" class="hidden-file" type="file" accept="image/png,image/jpeg,image/webp,image/gif"
                   multiple @change="uploadFiles(($event.target as HTMLInputElement).files)" />

            <button class="ring" :class="{ busy: compressing }" :title="compressTitle"
                    :disabled="!canCompress" @click="compress">
              <svg viewBox="0 0 36 36" class="ring-svg">
                <circle class="ring-track" cx="18" cy="18" r="15.5" />
                <circle class="ring-bar" cx="18" cy="18" r="15.5" :stroke-dasharray="ringDash" />
              </svg>
              <span class="ring-num">{{ Math.round(percent) }}</span>
            </button>
            <span class="model truncate" :title="`问答模型：${modelName}`">{{ modelName }}</span>
            <span v-if="usage?.compressed" class="badge dim tiny">已压缩</span>
            <span class="spacer" />
            <button v-if="sending" class="send stop" title="停止" @click="stop"><AppIcon name="stop" :size="14" /></button>
            <button v-else class="send" :disabled="!canSend" title="发送" @click="ask('send')">
              <AppIcon name="forward" :size="15" />
            </button>
          </div>
        </template>
      </div>
    </template>

    <div v-if="dragging" class="drop-hint"><AppIcon name="image" :size="16" />松手即上传</div>

    <ConfirmModal :open="Boolean(confirming)" danger title="删除对话"
                  :body="confirming?.body || ''" :target="confirming?.target || ''"
                  confirm-label="删除" :busy="confirmBusy"
                  @confirm="runDelete" @cancel="confirming = null" />
  </div>
</template>

<style scoped>
.chat { position: relative; display: flex; flex-direction: column; height: 100%; min-height: 0; }
.spacer { flex: 1; }
.ico {
  display: inline-flex; align-items: center; justify-content: center; border: 0;
  background: none; color: var(--text-dim); padding: 3px; border-radius: var(--radius-sm);
  cursor: pointer; flex: none;
}
.ico:hover { background: var(--bg-hover); color: var(--text); }
.ico.danger:hover { background: var(--danger-soft); color: var(--danger); }
.ico:disabled { opacity: .4; cursor: default; }
.hidden-file { display: none; }

/* ── 一级：列表 ── */
.list-head { display: flex; align-items: center; gap: 6px; padding: 8px 8px 6px; }
.search {
  flex: 1; min-width: 0; display: flex; align-items: center; gap: 5px;
  border: 1px solid var(--border); border-radius: var(--radius-ctl); background: var(--bg-sunken);
  padding: 4px 8px; color: var(--text-faint);
}
.search:focus-within { border-color: var(--accent); background: var(--bg-elev); }
.search-input {
  flex: 1; min-width: 0; border: 0; background: none; color: var(--text);
  font: inherit; font-size: 12.5px; outline: none; padding: 2px 0;
}
.plus {
  width: 30px; height: 30px; flex: none; border: 0; border-radius: var(--radius-ctl); cursor: pointer;
  display: inline-flex; align-items: center; justify-content: center;
  background: var(--accent); color: var(--text-on-accent);
}
.plus:hover { background: var(--accent-hover); }
.plus.busy { opacity: .6; cursor: default; }

.filters { display: flex; align-items: center; gap: 4px; padding: 0 8px 8px; }
.seg {
  display: inline-flex; gap: 2px; padding: 2px; border-radius: var(--radius-ctl);
  background: var(--bg-sunken); border: 1px solid var(--border);
}
.seg-btn {
  border: 0; background: none; color: var(--text-dim); cursor: pointer;
  font-size: 11.5px; padding: 3px 9px; border-radius: var(--radius-ctl);
}
.seg-btn:hover { color: var(--text); }
.seg-btn.on { background: var(--accent-soft); color: var(--accent-text); font-weight: 600; }
.chip-btn {
  display: inline-flex; align-items: center; gap: 3px; border: 1px solid var(--border);
  background: var(--bg-elev); color: var(--text-dim); border-radius: var(--radius-ctl);
  padding: 3px 8px; font-size: 11.5px; cursor: pointer;
}
.chip-btn:hover { border-color: var(--accent); color: var(--accent-text); }
.chip-btn.on { background: var(--accent-soft); border-color: transparent; color: var(--accent-text); font-weight: 600; }

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
  width: 22px; height: 22px; flex: none; border: 0; background: none; cursor: pointer;
  color: var(--text-faint); display: inline-flex; align-items: center; justify-content: center;
  border-radius: var(--radius-sm);
}
.tick.on { color: var(--accent); }
.rename-input {
  flex: 1; min-width: 0; border: 1px solid var(--accent); border-radius: var(--radius-sm);
  background: var(--bg-elev); color: var(--text); font: inherit; font-size: 12.5px;
  padding: 6px 7px; outline: none;
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
  display: flex; align-items: center; gap: 3px; padding: 5px 8px;
  border-bottom: 1px solid var(--divider);
}
.th-title { font-size: 12.5px; font-weight: 600; color: var(--text); max-width: 55%; }

.msgs { flex: 1; min-height: 0; overflow-y: auto; padding: 10px 10px 4px; overflow-wrap: anywhere; }
.msg { margin-bottom: 12px; }
.msg.assistant .body { font-size: 12.5px; line-height: 1.72; color: var(--text); }
.msg.user { display: flex; flex-direction: column; align-items: flex-end; }
.msg.user .body {
  background: var(--bg-layer); border: 1px solid var(--border); border-radius: var(--radius);
  padding: 6px 9px; font-size: 12.5px; line-height: 1.65; max-width: 92%;
}
.msg.user .sel-quote, .msg.user .atts { align-self: flex-end; }
.msg-tools { display: flex; align-items: center; gap: 2px; min-height: 18px; opacity: 0; }
.msg:hover .msg-tools, .msg-tools.show { opacity: 1; }
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
.cites { display: flex; flex-wrap: wrap; gap: 4px; margin-top: 5px; }
.cite {
  border: 1px solid var(--border); background: var(--bg-layer); color: var(--text-dim);
  border-radius: 999px; padding: 1px 7px; font-size: 11px; font-family: var(--mono); cursor: pointer;
}
.cite:hover { color: var(--accent-text); border-color: var(--accent); }
.note { display: flex; align-items: center; gap: 5px; font-size: 11.5px; color: var(--text-faint); }
.note .dot {
  width: 6px; height: 6px; border-radius: 50%; background: var(--accent);
  animation: pulse 1.1s ease-in-out infinite;
}
@keyframes pulse { 0%, 100% { opacity: .35 } 50% { opacity: 1 } }

/* 深度思考：折叠块。刻意用等宽 + 纯文本（不走 markdown）——
   思维链是模型的草稿，不是给用户读的正文，渲染成 markdown 只会让它看起来像答案。 */
.think { margin: 2px 0 6px; }
.think-head {
  display: inline-flex; align-items: center; gap: 4px; border: 1px solid var(--border);
  background: var(--bg-sunken); color: var(--text-dim); border-radius: var(--radius-ctl);
  padding: 3px 9px; font-size: 11.5px; cursor: pointer;
}
.think-head:hover { border-color: var(--accent); color: var(--accent-text); }
.think.open .think-head { border-color: var(--border-strong); color: var(--text); }
.think .dot {
  width: 6px; height: 6px; border-radius: 50%; background: var(--accent);
  animation: pulse 1.1s ease-in-out infinite;
}
.think-body {
  margin: 6px 0 0; padding: 8px 10px; max-height: 320px; overflow-y: auto;
  border-left: 2px solid var(--border-strong); border-radius: 0 var(--radius-sm) var(--radius-sm) 0;
  background: var(--bg-sunken); color: var(--text-dim);
  font-family: var(--mono); font-size: 11.5px; line-height: 1.7;
  white-space: pre-wrap; overflow-wrap: anywhere;
}
.err { color: var(--danger); line-height: 1.6; }
.msg .md :deep(p) { margin: 5px 0; }
.msg .md :deep(ul), .msg .md :deep(ol) { margin: 5px 0; padding-left: 18px; }

.hello { display: flex; flex-direction: column; align-items: center; gap: 8px; padding: 30px 16px; text-align: center; }
.hello-line {
  border: 1px solid var(--border); background: var(--bg-elev); color: var(--text);
  border-radius: var(--radius); padding: 8px 12px; font-size: 12.5px; font-weight: 500;
  cursor: pointer; line-height: 1.5; max-width: 100%;
}
.hello-line:hover { border-color: var(--accent); color: var(--accent-text); }
.hello-acts { display: flex; gap: 6px; }

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

/* ── 输入区 ── */
.composer { border-top: 1px solid var(--divider); padding: 6px 8px 8px; }
.sel-chip {
  display: flex; align-items: center; gap: 4px; margin-bottom: 5px; padding: 3px 6px;
  border-left: 2px solid var(--accent); background: var(--bg-layer);
  border-radius: 0 var(--radius-sm) var(--radius-sm) 0; font-size: 11.5px; color: var(--text-dim);
}
.sel-chip .truncate { flex: 1; }
.input {
  width: 100%; resize: none; max-height: 96px; overflow-y: auto;
  border: 1px solid var(--border); border-radius: var(--radius); background: var(--bg-layer);
  color: var(--text); font: inherit; font-size: 12.5px; line-height: 1.6; padding: 6px 8px;
}
.input:focus { outline: none; border-color: var(--accent); background: var(--bg-elev); }
.edit-box { display: flex; flex-direction: column; gap: 5px; }
.edit-head { display: inline-flex; align-items: center; gap: 4px; }
.edit-acts { display: flex; gap: 5px; }
.composer-bar { display: flex; align-items: center; gap: 6px; margin-top: 6px; }
.ring {
  position: relative; width: 26px; height: 26px; flex: none; border: 0; background: none;
  padding: 0; cursor: pointer; color: var(--accent);
}
.ring:disabled { cursor: default; color: var(--text-faint); }
.ring.busy .ring-bar { animation: spin 1s linear infinite; transform-origin: 18px 18px; }
@keyframes spin { to { transform: rotate(360deg) } }
.ring-svg { width: 26px; height: 26px; transform: rotate(-90deg); }
.ring-track { fill: none; stroke: var(--divider); stroke-width: 3; }
.ring-bar { fill: none; stroke: currentColor; stroke-width: 3; stroke-linecap: round; transition: stroke-dasharray .3s ease; }
.ring-num {
  position: absolute; inset: 0; display: flex; align-items: center; justify-content: center;
  font-size: 8px; font-family: var(--mono); color: var(--text-dim);
}
.model { font-size: 11px; color: var(--text-faint); max-width: 42%; }
.send {
  width: 26px; height: 26px; flex: none; border: 0; border-radius: 50%; cursor: pointer;
  display: inline-flex; align-items: center; justify-content: center;
  background: var(--accent); color: var(--text-on-accent);
}
.send:disabled { background: var(--divider); color: var(--text-faint); cursor: default; }
.send.stop { background: var(--danger); color: #fff; }

.drop-hint {
  position: absolute; inset: 6px; z-index: 5; display: flex; align-items: center; justify-content: center;
  gap: 6px; border: 1.5px dashed var(--accent); border-radius: var(--radius);
  background: color-mix(in srgb, var(--accent-soft) 88%, transparent);
  color: var(--accent-text); font-size: 12.5px; pointer-events: none;
}
</style>
