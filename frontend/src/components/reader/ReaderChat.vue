<script setup lang="ts">
/**
 * 阅读器侧栏的**对话面板**（论文问答）。
 *
 * 位置与形态：侧栏第二个页签（目录右侧）。布局自上而下 = 会话条 / 消息流 / 输入区，
 * 输入区左下角那枚**圆环**是上下文用量，点它就是"压缩"（见 `compress`）。
 *
 * 两条数据纪律：
 * 1. **`done` 是权威结果**：流式期间推的 `citation.ref` 是原始编号，`done` 里的引用被压成
 *    1..M 且正文也改写过，所以收到 `done` 后**重拉一次会话**（顺带把落库结果对齐），
 *    不做增量合并；
 * 2. **引用跳转只认 `block_id` / `row_id`**（`ref` 只是给人看的编号）。
 */
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue';
import { api } from '@/api/client';
import { postSse } from '@/api/stream';
import { mdToHtml } from '@/composables/useMarkdown';
import { reportError, toast } from '@/stores/app';
import { fmtRelative } from '@/utils/format';
import type {
  AgentCitation, AgentConversation, AgentMessage, AgentSelection, AgentStatus, AgentUsage,
} from '@/api/types';
import AppIcon from '@/components/AppIcon.vue';

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

const conversations = ref<AgentConversation[]>([]);
const currentId = ref<string | null>(null);
const messages = ref<AgentMessage[]>([]);
const usage = ref<AgentUsage | null>(null);
const status = ref<AgentStatus | null>(null);
const draft = ref('');
const listOpen = ref(false);
const loading = ref(false);
const compressing = ref(false);
const sending = ref(false);
const streamText = ref('');
const streamNote = ref('');
const streamCitations = ref<AgentCitation[]>([]);
const errorText = ref('');
const msgsEl = ref<HTMLElement | null>(null);
let controller: AbortController | null = null;

const percent = computed(() => Math.max(0, Math.min(100, usage.value?.percent ?? 0)));
const ringDash = computed(() => `${(percent.value / 100) * RING_CIRCUMFERENCE} ${RING_CIRCUMFERENCE}`);
const modelName = computed(() => status.value?.model?.model_name || '—');
const agentReady = computed(() => Boolean(status.value?.running) && status.value?.model?.usable !== false);
const canSend = computed(() => !sending.value && (draft.value.trim().length > 0 || Boolean(props.selection?.text)));
const canCompress = computed(() => !compressing.value && messages.value.length >= 2);
const compressTitle = computed(() => (canCompress.value
  ? '压缩这段对话：让模型把已谈过的内容压成要点（会多出一轮「摘要」，原文仍可回看）'
  : '至少聊过一轮才能压缩'));

function render(text?: string | null) {
  return mdToHtml(text ?? '', props.paperId);
}

/** 划词那种以 `> 引用` 开头的用户消息：正文只显示引用之后的部分（引用单独排版）。 */
function splitQuote(content: string): { quote: string; rest: string } {
  const match = /^>\s?(.*)\n+([\s\S]*)$/.exec(content || '');
  if (!match) return { quote: '', rest: content || '' };
  return { quote: match[1].trim(), rest: match[2].trim() };
}

async function scrollToBottom() {
  await nextTick();
  const el = msgsEl.value;
  if (el) el.scrollTop = el.scrollHeight;
}

/* ------------------------------ 数据 ------------------------------ */

async function loadStatus() {
  try {
    status.value = await api.get<AgentStatus>('/agent/status');
  } catch (err) {
    status.value = null;
    console.warn('[chat] 取问答状态失败', err);
  }
}

async function loadConversations() {
  try {
    const data = await api.get<{ items: AgentConversation[] }>('/agent/conversations', { paper_id: props.paperId });
    conversations.value = data.items ?? [];
  } catch (err) {
    conversations.value = [];
    console.warn('[chat] 取会话列表失败', err);
  }
}

async function loadConversation(id: string) {
  loading.value = true;
  try {
    const data = await api.get<AgentConversation>(`/agent/conversations/${id}`);
    currentId.value = data.conversation_id;
    messages.value = data.messages ?? [];
    usage.value = data.usage ?? null;
    await scrollToBottom();
  } catch (err) {
    reportError(err, '读取会话失败');
  } finally {
    loading.value = false;
  }
}

async function openConversation(id: string) {
  listOpen.value = false;
  if (id === currentId.value) return;
  await loadConversation(id);
}

function newConversation() {
  listOpen.value = false;
  currentId.value = null;
  messages.value = [];
  usage.value = null;
  errorText.value = '';
}

async function removeConversation(id: string) {
  try {
    await api.del(`/agent/conversations/${id}`);
    conversations.value = conversations.value.filter(c => c.conversation_id !== id);
    if (id === currentId.value) newConversation();
  } catch (err) { reportError(err, '删除会话失败'); }
}

onMounted(async () => {
  await loadStatus();
  await loadConversations();
  // 进面板先给**最近一次会话**：用户大多想接着上次问，而不是面对一张白纸
  if (conversations.value.length) await loadConversation(conversations.value[0].conversation_id);
});

onUnmounted(() => controller?.abort());

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

async function send() {
  const question = draft.value.trim();
  const selection = props.selection?.text ? { ...props.selection, question } : null;
  if (!question && !selection?.text) return;
  if (sending.value) return;
  if (!agentReady.value) { toast('问答服务没有在运行（先 `pr.py agent`）', 'warn'); return; }

  draft.value = '';
  if (selection) emit('clear-selection');
  errorText.value = '';
  sending.value = true;
  streamText.value = '';
  streamCitations.value = [];
  streamNote.value = '正在准备…';
  // 乐观回显：用户消息先出现（后端也会落库，`done` 之后重拉会与它一致）
  const optimistic: AgentMessage = {
    message_id: `local-${Date.now()}`, conversation_id: currentId.value ?? '', seq: 0,
    role: 'user', content: selection ? `> ${selection.text.slice(0, 400)}\n\n${question || '解释这段内容'}` : question,
  };
  messages.value = [...messages.value, optimistic];
  await scrollToBottom();

  controller = new AbortController();
  try {
    await postSse('/agent/ask', {
      paper_id: props.paperId, question,
      conversation_id: currentId.value,
      selection: selection ? {
        text: selection.text, row_id: selection.row_id ?? null, page: selection.page ?? null,
      } : null,
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
    messages.value = messages.value.filter(m => m.message_id !== optimistic.message_id);
  } finally {
    sending.value = false;
    streamText.value = '';
    streamCitations.value = [];
    streamNote.value = '';
    controller = null;
  }
}

function stop() {
  controller?.abort();
  toast('已停止', 'warn');
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

watch(() => props.paperId, async () => {
  messages.value = [];
  currentId.value = null;
  await loadConversations();
  if (conversations.value.length) await loadConversation(conversations.value[0].conversation_id);
});
</script>

<template>
  <div class="chat">
    <!-- 会话条：标题 / 新建 / 历史 -->
    <div class="chat-bar">
      <button class="bar-btn title" :title="conversations.find(c => c.conversation_id === currentId)?.title || '新对话'"
              @click="listOpen = !listOpen">
        <AppIcon name="chat" :size="13" />
        <span class="truncate">{{ conversations.find(c => c.conversation_id === currentId)?.title || '新对话' }}</span>
        <AppIcon :name="listOpen ? 'chevron-up' : 'chevron-down'" :size="12" />
      </button>
      <span class="spacer" />
      <button class="bar-btn" title="新建对话" @click="newConversation"><AppIcon name="add" :size="14" /></button>
    </div>

    <div v-if="listOpen" class="conv-list">
      <div v-if="!conversations.length" class="empty small">还没有对话</div>
      <div v-for="c in conversations" :key="c.conversation_id" class="conv-row"
           :class="{ active: c.conversation_id === currentId }">
        <button class="conv-open" @click="openConversation(c.conversation_id)">
          <span class="truncate">{{ c.title || '未命名对话' }}</span>
          <span class="faint mono">{{ c.messages ?? 0 }} 条 · {{ fmtRelative(c.updated_at) }}</span>
        </button>
        <button class="bar-btn" title="删除对话" @click="removeConversation(c.conversation_id)">
          <AppIcon name="trash" :size="12" />
        </button>
      </div>
    </div>

    <!-- 消息流 -->
    <div ref="msgsEl" class="msgs">
      <div v-if="loading" class="empty small">读取会话…</div>
      <div v-else-if="!messages.length && !sending" class="empty small">
        问这篇论文的任何问题；在正文里划词也能问（会带着选区一起发过来）。
      </div>

      <div v-for="m in messages" :key="m.message_id" class="msg" :class="m.role">
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
          <div class="md body" v-html="render(m.role === 'user' ? splitQuote(m.content).rest : m.content)" />
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
        <button class="bar-btn" title="取消选区" @click="emit('clear-selection')"><AppIcon name="close" :size="11" /></button>
      </div>
      <textarea
        v-model="draft"
        class="input"
        rows="1"
        :placeholder="props.selection?.text ? '就选中内容提问（可留空，直接解释）' : '问这篇论文…（Enter 发送，Shift+Enter 换行）'"
        @keydown.enter.exact.prevent="send"
      />
      <div class="composer-bar">
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
        <button v-else class="send" :disabled="!canSend" title="发送" @click="send">
          <AppIcon name="forward" :size="15" />
        </button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.chat { display: flex; flex-direction: column; height: 100%; min-height: 0; }
.spacer { flex: 1; }

/* 会话条 */
.chat-bar { display: flex; align-items: center; gap: 2px; padding: 4px 6px; border-bottom: 1px solid var(--divider); }
.bar-btn {
  display: inline-flex; align-items: center; gap: 4px; border: 0; background: none;
  color: var(--text-dim); padding: 4px 5px; border-radius: var(--radius-sm); cursor: pointer;
  font-size: 12px; min-width: 0;
}
.bar-btn:hover { background: var(--bg-hover); color: var(--text); }
.bar-btn.title { max-width: 100%; color: var(--text); font-weight: 600; }
.conv-list { border-bottom: 1px solid var(--divider); max-height: 42%; overflow-y: auto; padding: 2px 4px 6px; }
.conv-row { display: flex; align-items: center; gap: 2px; border-radius: var(--radius-sm); }
.conv-row.active { background: var(--bg-hover); }
.conv-open {
  flex: 1; min-width: 0; display: flex; flex-direction: column; align-items: flex-start; gap: 1px;
  border: 0; background: none; color: var(--text); cursor: pointer; text-align: left; padding: 5px 6px;
  font-size: 12.5px;
}
.conv-open .faint { font-size: 10.5px; }

/* 消息流 */
.msgs { flex: 1; min-height: 0; overflow-y: auto; padding: 10px 10px 4px; overflow-wrap: anywhere; }
.msg { margin-bottom: 12px; }
.msg.assistant .body { font-size: 12.5px; line-height: 1.72; color: var(--text); }
.msg.user { display: flex; flex-direction: column; align-items: flex-end; }
.msg.user .body {
  background: var(--bg-layer); border: 1px solid var(--border); border-radius: var(--radius-sm);
  padding: 6px 9px; font-size: 12.5px; line-height: 1.65; max-width: 92%;
}
.sel-quote {
  display: flex; align-items: center; gap: 4px; max-width: 92%; margin-bottom: 3px;
  padding: 3px 7px; border-left: 2px solid var(--accent);
  background: var(--bg-layer); border-radius: 0 var(--radius-sm) var(--radius-sm) 0;
  font-size: 11.5px; color: var(--text-dim);
}
.sum-card {
  border: 1px dashed var(--border-strong); border-radius: var(--radius-sm);
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
.err { color: var(--danger); line-height: 1.6; }
.msg .md :deep(p) { margin: 5px 0; }
.msg .md :deep(ul), .msg .md :deep(ol) { margin: 5px 0; padding-left: 18px; }

/* 输入区 */
.composer { border-top: 1px solid var(--divider); padding: 6px 8px 8px; }
.sel-chip {
  display: flex; align-items: center; gap: 4px; margin-bottom: 5px; padding: 3px 6px;
  border-left: 2px solid var(--accent); background: var(--bg-layer);
  border-radius: 0 var(--radius-sm) var(--radius-sm) 0; font-size: 11.5px; color: var(--text-dim);
}
.sel-chip .truncate { flex: 1; }
.input {
  width: 100%; resize: none; max-height: 96px; overflow-y: auto;
  border: 1px solid var(--border); border-radius: var(--radius-sm); background: var(--bg-layer);
  color: var(--text); font: inherit; font-size: 12.5px; line-height: 1.6; padding: 6px 8px;
}
.input:focus { outline: none; border-color: var(--accent); }
.composer-bar { display: flex; align-items: center; gap: 6px; margin-top: 5px; }
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
.model { font-size: 11px; color: var(--text-faint); max-width: 45%; }
.send {
  width: 26px; height: 26px; flex: none; border: 0; border-radius: 50%; cursor: pointer;
  display: inline-flex; align-items: center; justify-content: center;
  background: var(--accent); color: #fff;
}
.send:disabled { background: var(--divider); color: var(--text-faint); cursor: default; }
.send.stop { background: var(--danger); }
</style>
