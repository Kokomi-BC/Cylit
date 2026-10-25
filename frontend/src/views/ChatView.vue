<script setup lang="ts">
/**
 * ChatView —— 「AI 对话页」整页（/chat）。
 *
 * 角色：这个应用平时是**多标签工作区**（顶栏标签条 + .app-main 内边距）。对话页是刻意独立于
 * 那套外壳的「另一套系统」：App.vue 对 chat 路由收掉外壳，所以这一页自己撑满 100vh、内部各自
 * 滚动（左会话栏 / 右消息流），不依赖任何外层 padding，也不带主侧栏与标签条。
 *
 * 纯前端雏形：不发请求、不写 localStorage；会话数据只活在这个组件的 ref 里，刷新即回到预置样例。
 */
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { useRouter } from 'vue-router';
import AppIcon from '@/components/AppIcon.vue';
import ThinkingChain from '@/components/chat/ThinkingChain.vue';
import WaitDots from '@/components/chat/WaitDots.vue';
import type { ChainStep } from '@/components/chat/types';
import { useFollowTail } from '@/composables/useFollowTail';
import { flyMessage } from '@/composables/useComposerFlight';
import { BRAND_ICON } from '@/utils/brand';

type Source = { title: string; id?: string };
type Message = {
  id: string;
  role: 'user' | 'assistant';
  text: string;
  sources?: Source[];
  /** 这一轮的过程（思考 / 工具）。有它就是「思维工具链」块，空数组不渲染。 */
  steps?: ChainStep[];
  running?: boolean;
  /** 刚发出去的那一条：只在它身上播「从右下角长出来」的进场动画。 */
  fresh?: boolean;
  startedAt?: number;
};
type Conversation = { id: string; title: string; updatedAt: number; messages: Message[] };

const router = useRouter();

let seq = 0;
const uid = (prefix: string): string => prefix + '-' + Date.now().toString(36) + '-' + (seq += 1);

const pad2 = (n: number) => String(n).padStart(2, '0');
/** 会话行的时间小字：当天只给时分，跨天给月/日（列表里全是近几天的，不再给年份） */
function fmtTime(ts: number): string {
  const d = new Date(ts);
  const now = new Date();
  if (d.toDateString() === now.toDateString()) return pad2(d.getHours()) + ':' + pad2(d.getMinutes());
  return (d.getMonth() + 1) + '/' + d.getDate();
}

const now = Date.now();
const MINUTE = 60_000;

/* 预置样例：当前这条空会话 + 三条历史。空会话放最前，首屏就能同时看到空态与历史，
   不用先点一次「新建」才找得到建议卡。 */
const conversations = ref<Conversation[]>([
  {
    id: uid('c'),
    title: '新对话',
    updatedAt: now,
    messages: [],
  },
  {
    id: uid('c'),
    title: '多篇论文的方法差异',
    updatedAt: now - 12 * MINUTE,
    messages: [
      { id: uid('m'), role: 'user', text: '多篇论文的方法差异' },
      {
        id: uid('m'),
        role: 'assistant',
        text: '（示例对话）这里将来会逐篇对比你库里的论文：先按「任务 / 模型结构 / 训练数据 / 评测」四栏对齐，'
          + '再指出哪一篇只是换了骨干网络、哪一篇真的改了优化目标。\n\n'
          + '现在这条回复是写死的示意文本，对话页还没有接后端，所以它读不到任何真实论文内容。',
        sources: [
          { title: 'Attention Is All You Need' },
          { title: 'An Image is Worth 16x16 Words' },
          { title: 'Swin Transformer: Hierarchical Vision Transformer' },
        ],
      },
    ],
  },
  {
    id: uid('c'),
    title: 'Transformer 自注意力笔记整理',
    updatedAt: now - 3 * 60 * MINUTE,
    messages: [
      { id: uid('m'), role: 'user', text: '把我的自注意力笔记整理成一条主线' },
      {
        id: uid('m'),
        role: 'assistant',
        text: '（示例对话）等后端接上之后，这里会把你在阅读器里划的高亮按论文顺序串成一条主线：'
          + '为什么用缩放点积、多头在切什么、位置编码缺了会怎样。\n\n'
          + '目前是写死的占位文本，只用来验证消息流与来源条的排版。',
        sources: [
          { title: 'Attention Is All You Need' },
          { title: 'The Annotated Transformer' },
        ],
      },
    ],
  },
  {
    id: uid('c'),
    title: '我的阅读进度总结',
    updatedAt: now - 26 * 60 * MINUTE,
    messages: [
      { id: uid('m'), role: 'user', text: '总结我最近读的三篇' },
      {
        id: uid('m'),
        role: 'assistant',
        text: '（示例对话）这里将来会按你的阅读进度取最近三篇，各给一段「讲了什么 + 你现在停在哪」。'
          + '\n\n' + '现在是纯前端雏形的占位回复。',
        sources: [
          { title: 'Attention Is All You Need' },
          { title: 'Deep Residual Learning for Image Recognition' },
          { title: 'Visualizing Attention in Transformer-Based Models' },
        ],
      },
    ],
  },
]);

const activeId = ref(conversations.value[0].id);
const draft = ref('');
const listOpen = ref(false);
/** 发送瞬间给输入区挂一下：工具栏/提示行收掉再回来（参考实现是 400ms 的折叠）。 */
const sending = ref(false);
/** 只为了让「用时 8s」这行秒数自己走：每秒 +1，读它的计算属性随之重算。 */
const clockTick = ref(0);

const threadRef = ref<HTMLElement | null>(null);
const inputRef = ref<HTMLTextAreaElement | null>(null);
const inputBoxRef = ref<HTMLElement | null>(null);
const { following, follow, pin, onScroll, returnToBottom } = useFollowTail(threadRef);

const active = computed(() => conversations.value.find((c) => c.id === activeId.value) ?? null);
const messages = computed<Message[]>(() => active.value?.messages ?? []);
const canSend = computed(() => draft.value.trim().length > 0);
const busy = computed(() => messages.value.some((m) => m.running));

const SUGGESTS: { icon: string; text: string }[] = [
  { icon: 'library', text: '比较多篇论文的方法差异' },
  { icon: 'insight', text: '把这个概念讲给我听' },
  { icon: 'note', text: '找出我笔记里反复出现的问题' },
  { icon: 'list', text: '总结我最近读的三篇' },
];

/** 写死的助手回复：明确说自己是谁（雏形）、能看到哪些数据、真答案等后端。 */
const FALLBACK_TEXT = [
  '这是一段写死的前端占位回复：对话页现在是纯前端雏形，我没有真的调用模型，也没有发任何网络请求。',
  'Cylit Agent 现在已经能拿到你的整个论文库 —— 已导入论文的标题、作者与年份、摘要、标签与阅读进度，'
    + '以及你在阅读器里记下的笔记和划词高亮；左边那几个示例会话就是按这些数据的样子做出来的。',
  '真正的回答要等后端接入检索与生成之后才会出现：到那时同一句话会先去检索整个论文库，'
    + '再把带来源的答案贴回这里。你现在看到的消息流、思维工具链与贴底滚动，'
    + '都是在验证这条链路的前端部分。',
  '下面是这次（示意性的）检索结果：',
].join('\n\n');

const FALLBACK_SOURCES: Source[] = [
  { title: 'Attention Is All You Need' },
  { title: 'Swin Transformer: Hierarchical Vision Transformer' },
  { title: 'Visualizing Attention in Transformer-Based Models' },
];

/** 分段只按空行切，段内换行交给 white-space: pre-wrap —— 不引 markdown 库，也不吞用户的换行。 */
function paragraphs(text: string): string[] {
  return text.split(/\n{2,}/).map((p) => p.trim()).filter(Boolean);
}

/** 收起态摘要 = 最新一段思考的首行（参考实现就是这么取「还在想什么」的那一句）。 */
function chainPreview(m: Message): string {
  const think = [...(m.steps ?? [])].reverse().find((s) => s.kind === 'think');
  if (!think?.text) return '';
  const paras = think.text.split(/\n{2,}/).filter((p) => p.trim());
  const last = paras[paras.length - 1] ?? think.text;
  return (last.split('\n')[0] ?? '').trim();
}

function waitSeconds(m: Message): number {
  void clockTick.value;                       // 订阅秒针：否则这一行不会自己走
  if (!m.startedAt) return 0;
  return Math.max(0, Math.floor((Date.now() - m.startedAt) / 1000));
}

/** 有回复在跑时才转的秒针；停下就关掉，不让定时器常驻。 */
let clockTimer = 0;
watch(busy, (on) => {
  if (clockTimer) { window.clearInterval(clockTimer); clockTimer = 0; }
  clockTick.value = 0;
  if (on) clockTimer = window.setInterval(() => { clockTick.value += 1; }, 1000);
});

function scrollToBottom(): void {
  nextTick(() => pin());
}

/** 内容落地后跟一下尾巴（读者已经翻走了就不动，见 useFollowTail）。 */
function keepTail(): void {
  nextTick(() => follow());
}

/** 组件内统一登记定时器：卸载时一次清干净，避免回复落到已销毁的实例上。 */
const timers = new Set<number>();
function later(fn: () => void, ms: number): void {
  const id = window.setTimeout(() => {
    timers.delete(id);
    fn();
  }, ms);
  timers.add(id);
}

onBeforeUnmount(() => {
  timers.forEach((id) => window.clearTimeout(id));
  timers.clear();
  if (clockTimer) window.clearInterval(clockTimer);
});

function autoGrow(): void {
  const el = inputRef.value;
  if (!el) return;
  el.style.height = 'auto';
  const cs = getComputedStyle(el);
  const lineHeight = parseFloat(cs.lineHeight) || 22;
  const padding = parseFloat(cs.paddingTop) + parseFloat(cs.paddingBottom);
  // 上限 = 6 行：再长让 textarea 自己滚，输入区不许把消息流挤没
  const limit = lineHeight * 6 + padding;
  el.style.height = Math.min(el.scrollHeight, limit) + 'px';
  el.style.overflowY = el.scrollHeight > limit ? 'auto' : 'hidden';
}

watch(draft, () => { nextTick(autoGrow); });
// 首屏也量一次：1 行的高度要跟着 --ts 的当前值算，否则字号放大后输入框比一行字还矮
onMounted(autoGrow);

function focusInput(): void {
  nextTick(() => inputRef.value?.focus());
}

function send(): void {
  const text = draft.value.trim();
  if (!text) return;

  if (!active.value) newConversation();
  const conv = active.value;
  if (!conv) return;

  const isFirst = conv.messages.length === 0;
  conv.messages.push({ id: uid('m'), role: 'user', text, fresh: true });
  const user = conv.messages[conv.messages.length - 1];
  // 助手占位先落地：等待态、思维链、正文都长在这同一条消息上（切走再回来状态还在）
  conv.messages.push({
    id: uid('m'),
    role: 'assistant',
    text: '',
    steps: [],
    running: true,
    startedAt: Date.now(),
    fresh: true,
  });
  const reply = conv.messages[conv.messages.length - 1];
  conv.updatedAt = Date.now();
  if (isFirst) conv.title = text.slice(0, 18);

  // 交接动画：从输入框飞进刚落地的那枚气泡（必须在清空 draft 之前量）
  void flyMessage(inputBoxRef.value, text, () => threadRef.value?.querySelector(`.msg[data-mid="${user.id}"] .bubble`));

  draft.value = '';
  nextTick(autoGrow);
  scrollToBottom();
  focusInput();

  // 输入区那一行提示在发送瞬间收掉再回来（参考实现是同一拍 400ms）
  sending.value = true;
  later(() => { sending.value = false; }, 420);
  later(() => { user.fresh = false; }, 520);
  later(() => { reply.fresh = false; }, 420);

  runDemo(reply, conv);
}

/** 脚本化的「一轮 agent」：思考 → 检索 → 比对 → 成文。真接后端时这段换成 SSE 落盘。 */
const THINK_LINES = [
  '用户问的是「多篇论文的方法差异」，先定比对口径。',
  '按任务、模型结构、训练数据、评测四栏对齐，避免只比标题。',
  '库里这几篇都是视觉 Transformer，差异集中在骨干改法上。',
];

function runDemo(reply: Message, conv: Conversation): void {
  const steps = reply.steps as ChainStep[];     // 代理数组：push / 改字段都能触发重渲染
  const mark = () => { conv.updatedAt = Date.now(); keepTail(); };

  later(() => {
    steps.push({ id: uid('s'), kind: 'think', label: '思考', text: THINK_LINES[0], status: 'running' });
    mark();
  }, 380);
  THINK_LINES.slice(1).forEach((line, i) => {
    later(() => {
      steps[0].text = `${steps[0].text}\n${line}`;
      mark();
    }, 380 + (i + 1) * 220);
  });
  const thinkDone = 380 + THINK_LINES.length * 220;
  later(() => { steps[0].status = 'ok'; steps[0].ms = 1.3; mark(); }, thinkDone);

  later(() => {
    steps.push({
      id: uid('s'), kind: 'tool', label: '检索论文库',
      text: '「多篇论文的方法差异」· top 8', status: 'running',
    });
    mark();
  }, thinkDone + 120);
  later(() => { steps[1].status = 'ok'; steps[1].ms = 0.7; mark(); }, thinkDone + 900);

  later(() => {
    steps.push({
      id: uid('s'), kind: 'tool', label: '对齐方法差异',
      text: '任务 / 结构 / 数据 / 评测', status: 'running',
    });
    mark();
  }, thinkDone + 980);
  later(() => { steps[2].status = 'ok'; steps[2].ms = 0.8; mark(); }, thinkDone + 1800);

  later(() => {
    reply.running = false;
    reply.text = FALLBACK_TEXT;
    reply.sources = FALLBACK_SOURCES;
    mark();
  }, thinkDone + 2150);
}

function newConversation(): void {
  const conv: Conversation = { id: uid('c'), title: '新对话', updatedAt: Date.now(), messages: [] };
  conversations.value.unshift(conv);
  activeId.value = conv.id;
  draft.value = '';
  listOpen.value = false;
  scrollToBottom();
  focusInput();
}

function openConversation(id: string): void {
  activeId.value = id;
  listOpen.value = false;
  scrollToBottom();
}

function useSuggestion(text: string): void {
  draft.value = text;
  focusInput();
  nextTick(autoGrow);
}

/** Enter 发送 / Shift+Enter 换行；中文输入法选词那一下的 Enter 必须放过（否则半句就发出去了）。 */
function onKeydown(e: KeyboardEvent): void {
  if (e.key !== 'Enter' || e.shiftKey) return;
  if (e.isComposing || e.keyCode === 229) return;
  e.preventDefault();
  send();
}

function backToWorkspace(): void {
  router.push({ name: 'home' });
}
</script>
<template>
  <div class="chat-host" :class="{ 'list-open': listOpen }">
    <!-- 窄屏抽屉的遮罩：点一下收起会话栏 -->
    <div v-if="listOpen" class="scrim" @click="listOpen = false" />

    <!-- =========== 左：会话栏 =========== -->
    <aside class="conv-col">
      <div class="brand-row">
        <img class="brand-mark" :src="BRAND_ICON" alt="" />
        <span class="brand-name">Cylit Agent</span>
        <button class="back-btn" type="button" @click="backToWorkspace">
          <AppIcon name="back" :size="12" />
          <span>工作区</span>
        </button>
      </div>

      <button class="new-conv" type="button" @click="newConversation">
        <AppIcon name="add" :size="14" />
        <span>新建对话</span>
      </button>

      <div class="conv-list">
        <button
          v-for="c in conversations"
          :key="c.id"
          type="button"
          class="conv-row"
          :class="{ on: c.id === activeId }"
          :title="c.title"
          @click="openConversation(c.id)"
        >
          <AppIcon name="chat" :size="14" />
          <span class="conv-title">{{ c.title }}</span>
          <span class="conv-time">{{ fmtTime(c.updatedAt) }}</span>
        </button>
      </div>

      <div class="conv-foot">本地雏形 · 未接后端</div>
    </aside>

    <!-- =========== 右：对话区 =========== -->
    <section class="main-col">
      <header class="top-bar">
        <button class="menu-btn" type="button" title="会话列表" @click="listOpen = true">
          <AppIcon name="sidebar" :size="15" />
        </button>
        <span class="top-title">{{ active?.title ?? 'Cylit Agent' }}</span>
        <!-- 只是「当前用什么模型」的静态标签：不做下拉，所以用 span 而不是 button（不可聚焦、不响应键盘） -->
        <span class="model-pill">
          <AppIcon name="insight" :size="13" />
          <span class="model-label long">Cylit Agent · 可读取全部论文</span>
          <span class="model-label short">Cylit Agent</span>
          <AppIcon name="chevron-down" :size="12" />
        </span>
      </header>

      <div ref="threadRef" class="thread" :class="{ 'tail-anchor': following }" @scroll.passive="onScroll">
        <div v-if="!messages.length" class="hollow">
          <img class="hollow-mark" :src="BRAND_ICON" alt="" />
          <h1 class="hollow-title">问点什么？</h1>
          <p class="hollow-hint">Cylit Agent 会读取你的整个论文库来回答。</p>
          <div class="suggests">
            <button
              v-for="s in SUGGESTS"
              :key="s.text"
              type="button"
              class="suggest"
              @click="useSuggestion(s.text)"
            >
              <AppIcon :name="s.icon" :size="15" />
              <span>{{ s.text }}</span>
            </button>
          </div>
        </div>

        <template v-else>
          <div v-for="m in messages" :key="m.id" class="msg" :class="[m.role, { fresh: m.fresh }]" :data-mid="m.id">
            <span v-if="m.role === 'assistant'" class="avatar">
              <img :src="BRAND_ICON" alt="" />
            </span>
            <div v-if="m.role === 'user'" class="bubble">{{ m.text }}</div>
            <div v-else class="msg-content">
              <!-- 思维工具链：收起 / 展开 / 运行中的高光都在这个组件里 -->
              <ThinkingChain
                v-if="m.steps?.length"
                :steps="m.steps"
                :running="!!m.running"
                :preview="chainPreview(m)"
              />
              <div v-if="m.running" class="wait-row">
                <WaitDots />
                <span class="wait-text">正在深度求索</span>
                <span class="wait-clock">{{ waitSeconds(m) }}s</span>
              </div>
              <p v-for="(p, i) in paragraphs(m.text)" :key="i" class="para">{{ p }}</p>
              <div v-if="m.sources?.length" class="sources">
                <div class="src-head">
                  <AppIcon name="insight" :size="12" />
                  <span>检索到 {{ m.sources.length }} 篇论文</span>
                </div>
                <div class="src-row">
                  <span v-for="s in m.sources" :key="s.title" class="src-pill" :title="s.title">
                    <AppIcon name="document" :size="12" />
                    <span class="src-title">{{ s.title }}</span>
                  </span>
                </div>
              </div>
            </div>
          </div>
        </template>
      </div>

      <!-- 读者上翻之后才出现：一按就平滑落回最新（这一次才用 smooth） -->
      <Transition name="jump">
        <button
          v-if="!following"
          class="jump-btn"
          type="button"
          title="回到底部"
          aria-label="回到底部"
          @click="returnToBottom"
        >
          <AppIcon name="chevron-down" :size="15" />
        </button>
      </Transition>

      <div class="composer" :class="{ sending }">
        <div ref="inputBoxRef" class="input-box">
          <textarea
            ref="inputRef"
            v-model="draft"
            class="input"
            rows="1"
            placeholder="问问你的论文库…"
            @keydown="onKeydown"
          />
          <button class="send" type="button" :disabled="!canSend" title="发送" @click="send">
            <AppIcon name="arrow-up" :size="15" />
          </button>
        </div>
        <div class="hint">Enter 发送 · Shift + Enter 换行 · 雏形未接后端</div>
      </div>
    </section>
  </div>
</template>
<style scoped>
/* 整页 = 两栏，自身不滚动（滚动都在栏内）。
   高度用 100vh / 100dvh：这一页被外壳放行为通栏，不能依赖任何外层 padding。 */
.chat-host {
  /* 固定定位而不是普通流：外壳把 .app-main 放进了 flex 容器，它自带的 margin:0 auto 会把
     它挤成 fit-content 并居中（实测 1440px 视口里只剩 757px 宽、两侧各 326px 空白）。
     这一页是 bare 形态，本来就该盖住整个视口，钉死以后也不再受任何外层 padding/宽度影响。 */
  position: fixed;
  inset: 0;
  display: flex;
  height: 100vh;
  height: 100dvh;
  overflow: hidden;
  background: var(--bg);
  color: var(--text);
}

/* ---------------- 左：会话栏 ---------------- */
.conv-col {
  flex: none;
  width: 264px;
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 14px 12px 10px;
  border-right: 1px solid var(--divider);
  background: var(--bg-sunken);
  min-height: 0;
}

.brand-row { display: flex; align-items: center; gap: 8px; padding: 0 4px; height: 26px; }
.brand-mark { width: 20px; height: 20px; border-radius: 6px; flex: none; display: block; }
.brand-name {
  font-size: calc(13.5px * var(--ts, 1));
  font-weight: 600;
  letter-spacing: -.01em;
  white-space: nowrap;
}
/* 「返回工作区」是离开这一页的出口：视觉重量低于会话动作，文字按钮 + 弱色，悬停才抬起来 */
.back-btn {
  margin-left: auto;
  display: inline-flex; align-items: center; gap: 4px;
  padding: 4px 6px; border-radius: var(--radius-ctl);
  color: var(--text-dim);
  font-size: calc(11.5px * var(--ts, 1));
  cursor: pointer;
  transition: background var(--dur) var(--ease), color var(--dur) var(--ease);
}
.back-btn:hover { background: var(--bg-hover); color: var(--text); }

.new-conv {
  display: flex; align-items: center; gap: 7px;
  width: 100%; height: 34px; padding: 0 11px;
  border: 1px solid var(--border);
  border-radius: var(--radius-ctl);
  background: var(--bg-elev);
  color: var(--text);
  font-family: inherit;
  font-size: calc(13px * var(--ts, 1));
  line-height: 1;
  cursor: pointer;
  transition: background var(--dur) var(--ease), border-color var(--dur) var(--ease);
}
.new-conv:hover { background: var(--bg-hover); border-color: var(--border-strong); }

.conv-list { flex: 1; min-height: 0; overflow-y: auto; display: flex; flex-direction: column; gap: 2px; }

.conv-row {
  display: flex; align-items: center; gap: 7px;
  width: 100%; padding: 7px 8px;
  border-radius: var(--radius-sm);
  color: var(--text-dim);
  font-family: inherit;
  font-size: calc(12.5px * var(--ts, 1));
  text-align: left;
  cursor: pointer;
  transition: background var(--dur-fast) var(--ease), color var(--dur-fast) var(--ease);
}
.conv-row:hover { background: var(--bg-hover); color: var(--text); }
.conv-row.on { background: var(--bg-hover); color: var(--text); font-weight: 600; }
.conv-title { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.conv-time { flex: none; font-size: calc(10.5px * var(--ts, 1)); color: var(--text-faint); font-weight: 400; }

.conv-foot { padding: 0 4px 2px; font-size: calc(11.5px * var(--ts, 1)); color: var(--text-faint); }

/* ---------------- 右：对话区 ---------------- */
.main-col { flex: 1; min-width: 0; display: flex; flex-direction: column; min-height: 0; position: relative; }

.top-bar {
  flex: none; height: 56px;
  display: flex; align-items: center; gap: 10px;
  padding: 0 18px;
  border-bottom: 1px solid var(--divider);
}
.top-title {
  flex: 1 1 auto; min-width: 0;
  font-size: calc(13.5px * var(--ts, 1)); font-weight: 600;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
/* 看得出是「下拉」，但真的不弹：不可聚焦、无 cursor:pointer、无 hover 反馈 */
.model-pill {
  flex: 0 1 auto; min-width: 0;
  display: inline-flex; align-items: center; gap: 6px;
  height: 28px; padding: 0 10px;
  border: 1px solid var(--border);
  border-radius: var(--radius-ctl);
  background: var(--bg-elev);
  color: var(--text-dim);
  font-size: calc(11.5px * var(--ts, 1));
  user-select: none;
}
.model-label { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.model-label.short { display: none; }

.menu-btn {
  display: none;
  align-items: center; justify-content: center;
  width: 28px; height: 28px; flex: none;
  border-radius: var(--radius-ctl);
  color: var(--text-dim);
  cursor: pointer;
}
.menu-btn:hover { background: var(--bg-hover); color: var(--text); }

.thread {
  flex: 1; min-height: 0;
  overflow-y: auto; overflow-x: hidden;
  padding: 22px 18px 8px;
  display: flex; flex-direction: column; gap: 18px;
  /* 滚动条藏起来：消息流本来就贴着底走，多一条竖条只是噪音 */
  scrollbar-width: none;
}
.thread::-webkit-scrollbar { display: none; }
/* 跟随时关掉滚动锚定：浏览器锚定会和自动滚动抢同一个位置，表现为滚到底又弹回一点 */
.thread.tail-anchor { overflow-anchor: none; }

/* 用户消息：右侧实底气泡。文字色用 --text 而不是 --text-on-accent ——
   --accent-soft 是浅底，深色模式下它又翻成深底，只有 --text 两种模式都读得清。 */
.msg { display: flex; gap: 10px; min-width: 0; }
.msg.user { justify-content: flex-end; }
/* 新发的那条用户消息：从右下角「长出来」（320ms，回弹到 1.3）——
   起点就是发送按钮的位置，读起来像"从输入框飞进消息流" */
.msg.fresh.user .bubble {
  transform-origin: 100% 100%;
  animation: msg-grow .32s cubic-bezier(.34, 1.3, .64, 1) both;
}
@keyframes msg-grow {
  from { opacity: 0; transform: scale(.2); }
  to { opacity: 1; transform: none; }
}
/* 助手这一条是「插入到流里」：12px 上浮 + 240ms 的 glide，而不是整块弹出 */
.msg.fresh.assistant { animation: row-rise .24s cubic-bezier(.22, .61, .36, 1) both; }
@keyframes row-rise {
  from { opacity: 0; transform: translateY(12px); }
  to { opacity: 1; transform: none; }
}
.bubble {
  max-width: 72%;
  padding: 9px 14px;
  border-radius: 14px;
  background: var(--accent-soft);
  color: var(--text);
  font-size: calc(14px * var(--ts, 1));
  line-height: 1.7;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

/* 助手消息：没有气泡，只有一枚圆形头像 + 正文 */
.avatar {
  flex: none; width: 28px; height: 28px; margin-top: 2px;
  border-radius: 50%;
  display: flex; align-items: center; justify-content: center;
  background: var(--bg-sunken);
  border: 1px solid var(--border);
}
.avatar img { width: 18px; height: 18px; border-radius: 5px; display: block; }
.msg-content { flex: 1; min-width: 0; max-width: 74ch; }
/* 思维链与正文之间留一口气（链自带 24px 抬头，再紧贴正文就挤了） */
.msg-content > .chain { margin-bottom: 6px; }
.para {
  margin: 0 0 .7em;
  font-size: calc(14px * var(--ts, 1));
  line-height: 1.75;
  color: var(--text);
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
.para:last-child { margin-bottom: 0; }

/* 来源条：一行横向小胶囊，超宽就横滚（不换行，免得一条来源被拆成两行） */
.sources { margin-top: 12px; display: flex; flex-direction: column; gap: 6px; }
.src-head { display: flex; align-items: center; gap: 5px; font-size: calc(11.5px * var(--ts, 1)); color: var(--text-faint); }
.src-row { display: flex; gap: 6px; overflow-x: auto; padding-bottom: 2px; scrollbar-width: none; }
.src-row::-webkit-scrollbar { display: none; }
.src-pill {
  flex: none; max-width: 220px;
  display: inline-flex; align-items: center; gap: 5px;
  height: 24px; padding: 0 9px;
  border: 1px solid var(--border);
  border-radius: var(--radius-ctl);
  background: var(--bg-elev);
  color: var(--text-dim);
  font-size: calc(11px * var(--ts, 1));
}
.src-title { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

/* 等回复：三颗点 + 秒表。点动效在 WaitDots 里，这里只管基线对齐 */
.wait-row {
  display: flex; align-items: center; gap: 8px;
  margin: 2px 0 10px;
  color: var(--text-faint);
  font-size: calc(12.5px * var(--ts, 1));
}
.wait-text { color: var(--text-dim); }
.wait-clock { margin-left: 2px; font-variant-numeric: tabular-nums; }

/* 回到底部：读者一上翻才出现。150ms 的透明度 + 轻微缩放，别做成弹窗 */
.jump-btn {
  position: absolute;
  left: 50%;
  bottom: 88px;
  z-index: 6;
  width: 30px; height: 30px;
  display: flex; align-items: center; justify-content: center;
  transform: translateX(-50%);
  border: 1px solid var(--border);
  border-radius: 50%;
  background: var(--bg-elev);
  color: var(--text-dim);
  box-shadow: var(--shadow-1);
  cursor: pointer;
  transition: color var(--dur-fast) var(--ease), border-color var(--dur-fast) var(--ease);
}
.jump-btn:hover { color: var(--text); border-color: var(--border-strong); }
.jump-enter-active, .jump-leave-active { transition: opacity .15s var(--ease), transform .15s var(--ease); }
.jump-enter-from, .jump-leave-to { opacity: 0; transform: translateX(-50%) scale(.88); }

/* 空态：垂直居中；建议卡收在 520px 内（大屏上摊太开就散了） */
.hollow {
  flex: 1;
  display: flex; flex-direction: column; align-items: center; justify-content: center;
  gap: 6px; padding: 24px 12px 32px; text-align: center;
}
.hollow-mark { width: 52px; height: 52px; border-radius: 15px; display: block; margin-bottom: 8px; }
.hollow-title { margin: 0; font-size: calc(20px * var(--ts, 1)); font-weight: 600; letter-spacing: -.011em; }
/* 一行放得下就不该折：34ch 在中文里只够 17 个字，会把「回答。」挤到第二行 */
.hollow-hint { margin: 0 0 16px; font-size: calc(12.5px * var(--ts, 1)); color: var(--text-dim); max-width: 26em; line-height: 1.6; }
.suggests { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; width: 100%; max-width: 520px; }
.suggest {
  display: flex; align-items: flex-start; gap: 8px;
  padding: 11px 12px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--bg-elev);
  color: var(--text-dim);
  font-family: inherit;
  font-size: calc(12.5px * var(--ts, 1));
  line-height: 1.5;
  text-align: left;
  cursor: pointer;
  transition: border-color var(--dur) var(--ease), box-shadow var(--dur) var(--ease), color var(--dur) var(--ease);
}
.suggest:hover {
  border-color: var(--border-strong);
  box-shadow: var(--shadow-1);
  color: var(--text);
}

/* ---------------- 输入区 ---------------- */
.composer { flex: none; padding: 6px 18px 12px; }
.input-box {
  display: flex; align-items: flex-end; gap: 8px;
  padding: 7px 7px 7px 14px;
  border: 1px solid var(--border);
  border-radius: 16px;
  background: var(--bg-elev);
  box-shadow: var(--shadow-1);
  transition: border-color var(--dur) var(--ease), box-shadow var(--dur) var(--ease);
}
/* 焦点反馈画在容器上：textarea 自己一旦有边框，输入框就变成「框里套框」 */
.input-box:focus-within { border-color: var(--accent); box-shadow: var(--ring); }

/* 必须带 .input-box 前缀去压全局的 textarea:hover / textarea:focus（它们各带一条背景 + 焦点环） */
.input-box .input {
  flex: 1; min-width: 0;
  border: 0; background: transparent; box-shadow: none;
  padding: 6px 0; margin: 0;
  color: var(--text);
  font-family: inherit;
  font-size: calc(14px * var(--ts, 1));
  line-height: 1.6;
  resize: none;
  overflow-y: hidden;
}
.input-box .input:hover,
.input-box .input:focus {
  border: 0; background: transparent; box-shadow: none;
}

.send {
  flex: none;
  width: 30px; height: 30px;
  display: flex; align-items: center; justify-content: center;
  border-radius: 50%;
  background: var(--accent);
  color: var(--text-on-accent);
  cursor: pointer;
  transition: background var(--dur) var(--ease), opacity var(--dur) var(--ease);
}
.send:hover:not(:disabled) { background: var(--accent-hover); }
.send:disabled { opacity: .4; cursor: default; }

.hint {
  margin-top: 7px;
  text-align: center;
  font-size: calc(11px * var(--ts, 1));
  color: var(--text-faint);
  transition: opacity .12s var(--ease), transform .12s var(--ease);
}
/* 发送瞬间把这一行收掉再放回来：输入区看起来"弹了一下"，但不改变输入框高度 */
.composer.sending .hint { opacity: 0; transform: translateY(-4px); }

/* ---------------- 窄屏 ---------------- */
.scrim { display: none; }

@media (max-width: 720px) {
  /* 会话栏收成抽屉：默认整个移出视口，靠顶栏那枚按钮拉回来。
     不做「半露的窄条」—— 360px 上会挤掉消息流一半宽度。 */
  .conv-col {
    position: fixed; top: 0; bottom: 0; left: 0; z-index: 30;
    width: 264px; max-width: 84vw;
    border-right: 1px solid var(--border);
    box-shadow: var(--shadow-2);
    transform: translateX(-100%);
    transition: transform var(--dur) var(--ease);
  }
  .list-open .conv-col { transform: none; }
  .scrim {
    display: block;
    position: fixed; inset: 0; z-index: 25;
    background: var(--smoke);
  }
  .menu-btn { display: flex; }
  .top-bar { padding: 0 12px; gap: 8px; }
  .thread { padding: 16px 12px 8px; }
  .composer { padding: 6px 12px 10px; }
  .bubble { max-width: 84%; }
  /* 窄屏只念得下短标签：留两个图标（搜索样 + 折角）看着像"搜索下拉"，反而更迷惑 */
  .model-label.long { display: none; }
  .model-label.short { display: inline; }
  .src-pill { max-width: 160px; }
}
</style>
