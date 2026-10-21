<script setup lang="ts">
/**
 * 侧栏「搜索」页签：**上面输入、下面命中列表**。
 *
 * 以前搜索框挂在阅读器工具栏、命中列表塞在「目录」页签最前面，两处都不合适：
 * 工具栏那一行是"对整篇文档 + 视图 + 翻译"的操作区，挤一个输入框进去会让主次不分；
 * 命中列表和目录混在一起，翻目录时被一段搜索结果顶开。现在搜索自成页签。
 *
 * 词法命中每条给两样东西（用户口径："显示相关词的情况"）：
 * - 命中位置：`p3 · 原文/译文`；
 * - 命中词所在的**原句片段**，并把命中词本身标出来（`<mark>`）。
 * 点一条 = 与目录同一种跳转（`focus`），滚到正文那一行。
 *
 * 片段按 **Markdown** 渲染（`$公式$`、`**加粗**` 直接显示成排版后的样子），命中词用
 * `htmlWithMarks` 的占位符机制在解析后插回 `<mark>` —— 与正文高亮同一套做法，
 * 所以标记不会打断 Markdown 结构。
 *
 * 本轮新增**语义搜索**（`POST /rag/similar`，`scope:"paper"`）：词法搜索只会命中"字面出现过的词"，
 * 而读论文时更常见的是"我记得这里有讲 X，但原文用的不是这个词"。两条路子共用同一个输入框，
 * 用上面的分段控件切换（用户口径：保留原有搜索，只加一个入口）。
 * `scope:"paper"` 由后端保证**只查本文**，前端不再做任何额外过滤（多滤一层就可能把后端
 * 已经扩好的 Small-to-Big 命中滤掉）。
 */
import { computed, nextTick, reactive, ref, watch } from 'vue';
import AppIcon from '@/components/AppIcon.vue';
import { api } from '@/api/client';
import { debounce } from '@/utils/format';
import { htmlWithMarks, mdToHtml } from '@/composables/useMarkdown';
import type { SemanticHit, SemanticResult } from '@/api/types';

/** 搜索范围：全部（原文 + 译文）/ 只看原文 / 只看译文。**只对词法搜索有意义**。 */
export type SearchScope = 'all' | 'en' | 'zh';

/** 搜索模式：词法（原有） / 语义（新增）。 */
export type SearchMode = 'lexical' | 'semantic';

const SCOPE_OPTIONS: Array<{ k: SearchScope; label: string; title: string }> = [
  { k: 'all', label: '全部', title: '原文 + 译文都搜' },
  { k: 'en', label: '原文', title: '只搜英文原文' },
  { k: 'zh', label: '译文', title: '只搜中文译文' },
];

const MODE_OPTIONS: Array<{ k: SearchMode; label: string; title: string }> = [
  { k: 'lexical', label: '关键词', title: '按字面匹配原文 + 译文（不联网、即时）' },
  { k: 'semantic', label: '语义', title: '按意思在本文里找（向量 + 词法 + 重排，只查本篇）' },
];

/** 语义检索的条数：侧栏只有一列宽，8 条已经要滚一屏了（口径给的 top_k=8） */
const SEMANTIC_TOP_K = 8;

/** chunk 类型 → 中文标签（与知识库页同一套说法，列表里显示"类型 + 页码"） */
const CHUNK_TYPE_LABEL: Record<string, string> = {
  text: '正文', unit: '语义单元', formula: '公式', figure: '插图', table: '表格',
  caption: '图注', heading: '标题', ref: '参考文献',
};

const props = defineProps<{
  /** 与视图里的 `searchQuery` 双向绑定（`v-model:query`）：深链 `?q=`、快捷键 `/` 都写它 */
  query: string;
  /** 搜索范围（`v-model:scope`） */
  scope: SearchScope;
  /** 搜索模式（`v-model:mode`）：视图据此决定要不要跑词法搜索 */
  mode: SearchMode;
  /** 语义检索固定 `scope:"paper"`，需要论文 id */
  paperId: string;
  hits: Array<{ id: string; page: number; side: string; snippet: string }>;
}>();

const emit = defineEmits<{
  (e: 'update:query', v: string): void;
  (e: 'update:scope', v: SearchScope): void;
  (e: 'update:mode', v: SearchMode): void;
  (e: 'search'): void;
  (e: 'clear'): void;
  (e: 'focus', id: string, page?: number): void;
  /** 语义命中：跳到那一页（由 ReaderView 写进 `?page=N`，与「来源 Chunk」胶囊同一条路） */
  (e: 'goto-page', page: number): void;
}>();

const box = ref<HTMLInputElement | null>(null);

/** 让外部（快捷键 `/`）把光标放进搜索框 */
function focusInput() { void nextTick(() => box.value?.focus()); }
defineExpose({ focusInput });

function onInput(ev: Event) { emit('update:query', (ev.target as HTMLInputElement).value); }

/* ------------------------------ 词法片段渲染 ------------------------------ */

/** 把片段按命中词切段：`[{t, hit}]`。大小写不敏感，但**原样保留**片段里的大小写。 */
function parts(snippet: string): Array<{ t: string; hit: boolean }> {
  const needle = props.query.trim().toLowerCase();
  if (!needle) return [{ t: snippet, hit: false }];
  const lower = snippet.toLowerCase();
  const out: Array<{ t: string; hit: boolean }> = [];
  let i = 0;
  while (i < snippet.length) {
    const at = lower.indexOf(needle, i);
    if (at < 0) { out.push({ t: snippet.slice(i), hit: false }); break; }
    if (at > i) out.push({ t: snippet.slice(i, at), hit: false });
    out.push({ t: snippet.slice(at, at + needle.length), hit: true });
    i = at + needle.length;
  }
  return out.filter((p) => p.t);
}

/** 命中区间（相对片段自身的字符偏移），交给 `htmlWithMarks` 在 Markdown 解析后插回标记。 */
function hitRanges(snippet: string): Array<{ start: number; end: number }> {
  const ranges: Array<{ start: number; end: number }> = [];
  let at = 0;
  for (const p of parts(snippet)) {
    if (p.hit) ranges.push({ start: at, end: at + p.t.length });
    at += p.t.length;
  }
  return ranges;
}

/** 片段 → 渲染好的 HTML（Markdown + 命中标记）。 */
function snippetHtml(snippet: string): string {
  return htmlWithMarks(snippet, hitRanges(snippet));
}

/* ------------------------------ 语义搜索 ------------------------------ */

const semantic = reactive({
  loading: false,
  error: '',
  /** 跑过一次没（用来区分"还没搜"与"搜了但没结果"两种空态） */
  ran: false,
  items: [] as SemanticHit[],
});

/** 在途请求序号：滚动/连点下前一次请求可能后回来，只认最后一次的结果 */
let seq = 0;

function hitText(hit: SemanticHit): string {
  const meta = (hit.metadata ?? {}) as Record<string, any>;
  return String(
    hit.preview ?? hit.snippet ?? hit.content ?? meta.preview ?? meta.content ?? hit.caption ?? '',
  ).trim();
}

/**
 * 语义命中带的**缩略图**（插图 / 表格 / 公式的裁图）。
 *
 * 后端 `chunk_image_url` 早就把这张图算进 `image_url` 了（知识库页拿它当缩略图），只是阅读器
 * 这边一直没用 —— 而语义检索恰恰经常命中"这张图在讲什么"（用户口径 2026-11-19：搜索要能显示图片）。
 * 正文 chunk 没有图，返回空串（列表照旧只有文字）。
 */
function hitImage(hit: SemanticHit): string {
  const meta = (hit.metadata ?? {}) as Record<string, any>;
  return String(hit.image_url ?? meta.image_url ?? '').trim();
}

function typeLabel(hit: SemanticHit): string {
  const raw = String(hit.chunk_type ?? (hit.metadata as Record<string, any> | null)?.chunk_type ?? '').trim();
  return CHUNK_TYPE_LABEL[raw] ?? raw ?? '片段';
}

/**
 * 丢掉**起点被截断的公式尾部**（片段是后端截过的）。
 *
 * 落单的 `$` 会让 marked-katex 认为"公式没结束"，于是整段原始 LaTeX（`\langle \boldsymbol{q}…`）
 * 原样显示在列表里 —— 知识库页实测过这个现象（见 `KnowledgeView.dropUnclosedMath`），
 * 这里用同一套处理：只截到最后一个未配对的 `$` 之前，并加省略号；成对的 `$$…$$` 不动。
 */
function dropUnclosedMath(src: string): string {
  let inFence = false;
  const singles: number[] = [];
  for (let i = 0; i < src.length; i += 1) {
    if (src.startsWith('```', i)) { inFence = !inFence; i += 2; continue; }
    if (inFence || src[i] !== '$') continue;
    if (src[i + 1] === '$') { i += 1; continue; }      // `$$` 块级定界符，整对跳过
    singles.push(i);
  }
  if (singles.length % 2 === 0) return src;
  return `${src.slice(0, singles[singles.length - 1]).replace(/\s+$/, '')}…`;
}

/** 语义片段 → HTML（Markdown + KaTeX；段落压成一行由 CSS 截断） */
function previewHtml(hit: SemanticHit): string {
  const raw = hitText(hit).replace(/\r/g, '').replace(/\n{2,}/g, '\n');
  if (!raw) return '';
  return mdToHtml(dropUnclosedMath(raw), props.paperId);
}

/**
 * 跑一次语义检索。
 *
 * **打字就搜**（用户口径）：与词法那半边一样由输入驱动，只是去抖给得更长（900ms）——
 * 这是一次带向量的 POST（还有重排），每个键都发既费钱又会让结果互相抢（见下面的 seq 守卫）。
 */
async function runSemantic() {
  const text = props.query.trim();
  if (!text) return;
  const mine = ++seq;
  semantic.loading = true;
  semantic.error = '';
  try {
    const data = (await api.post<SemanticResult>('/rag/similar', {
      query: text, paper_id: props.paperId, scope: 'paper', top_k: SEMANTIC_TOP_K,
    })) || {};
    if (mine !== seq) return;                       // 已有更新的请求在跑：这次结果丢掉
    /* 后端明确说"这条路没实现"（老版本 / 向量库没起来）时把原话显示出来：
     * 报成"没有相近的段落"会让人以为是自己措辞的问题，方向就找错了。 */
    if (data.implemented === false) {
      semantic.error = data.message || '后端还没有启用语义检索';
      semantic.items = [];
      semantic.ran = true;
      return;
    }
    semantic.items = Array.isArray(data.items) ? data.items : [];
    semantic.ran = true;
  } catch (err) {
    if (mine !== seq) return;
    semantic.error = (err as Error)?.message ?? String(err);
    semantic.items = [];
    semantic.ran = true;
  } finally {
    if (mine === seq) semantic.loading = false;
  }
}

/* **打字就搜**（用户口径）：语义这半边同样由输入驱动（去抖 900ms，理由见 runSemantic 的注释）；
 * 回车仍然立即发一次（见 onEnter）。切到语义模式时若有词，也立刻补一发。 */
const runSemanticDebounced = debounce(() => { if (props.query.trim()) void runSemantic(); }, 900);
watch(() => props.query, () => { if (props.mode === 'semantic') runSemanticDebounced(); });
watch(() => props.mode, (m) => { if (m === 'semantic' && props.query.trim()) void runSemantic(); });

/** 切模式：清掉两边的输入态影响（结果各自保留，来回切不丢） */
function setMode(m: SearchMode) {
  if (props.mode === m) return;
  emit('update:mode', m);
  semantic.error = '';
}

/* 顶部那两枚小胶囊：标签与提示都从上面两张表里取（表里"说明"改成挂在 title 上，
 * 面板上只留最短的两个字）—— 这是"按钮和说明文字都太多"那条口径的落点。 */
const modeLabel = computed(() => MODE_OPTIONS.find((m) => m.k === props.mode)?.label ?? '关键词');
const scopeLabel = computed(() => SCOPE_OPTIONS.find((o) => o.k === props.scope)?.label ?? '全部');
const modeTitle = computed(() => {
  const cur = MODE_OPTIONS.find((m) => m.k === props.mode);
  const other = MODE_OPTIONS.find((m) => m.k !== props.mode);
  return `当前：${cur?.label}（${cur?.title}）· 点一下切到「${other?.label}」`;
});
const scopeTitle = computed(() => {
  const i = SCOPE_OPTIONS.findIndex((o) => o.k === props.scope);
  const next = SCOPE_OPTIONS[(i + 1) % SCOPE_OPTIONS.length];
  return `搜索范围：${scopeLabel.value} · 点一下换成「${next.label}」`;
});
function toggleMode() { setMode(props.mode === 'semantic' ? 'lexical' : 'semantic'); }
function nextScope() {
  const i = SCOPE_OPTIONS.findIndex((o) => o.k === props.scope);
  emit('update:scope', SCOPE_OPTIONS[(i + 1) % SCOPE_OPTIONS.length].k);
}

/** 回车：词法交给视图（`emit('search')` 那条路径带提示），语义在这里直接发 */
function onEnter() {
  if (props.mode === 'semantic') { void runSemantic(); return; }
  emit('search');
}

/** Esc / 清空按钮：两边一起清（只清一边会留下"上次搜了一半"的错觉） */
function onClear() {
  seq += 1;                                          // 让在途请求的结果作废
  semantic.items = [];
  semantic.error = '';
  semantic.ran = false;
  semantic.loading = false;
  emit('clear');
}
</script>

<template>
  <div class="side-search">
    <div class="ss-head">
      <label class="search-field ss-field">
        <AppIcon name="search" :size="14" />
        <input
          ref="box"
          :value="props.query"
          :placeholder="props.mode === 'semantic' ? '描述你要找的内容' : '搜索'"
          :title="props.mode === 'semantic'
            ? '用一句话描述你想找的内容，打字即在本文内做语义检索（回车立即）'
            : '边打边搜原文 + 译文（回车立即搜；Esc 清空）'"
          @input="onInput"
          @keyup.enter="onEnter"
          @keyup.esc="onClear"
        >
        <button
          v-if="props.query"
          class="btn icon sm ghost"
          title="清空搜索"
          @click="onClear"
        >
          <AppIcon name="close" :size="12" />
        </button>
      </label>

      <!-- 一行两枚小胶囊（用户口径：别堆按钮、别写说明文字）：
           左 = 搜索方式，点一下在「关键词 / 语义」之间切换（亮起 = 语义，另一种检索范式）；
           右 = 范围，点一下在「全部 → 原文 → 译文」之间轮换（只对关键词有意义，语义模式下收起）。
           当前值写在脸上、"点一下会变成什么"写在 title 里，所以不需要任何额外文案。 -->
      <div class="ss-chips">
        <button
          class="ss-chip"
          :class="{ on: props.mode === 'semantic' }"
          :title="modeTitle"
          @click="toggleMode"
        >
          <AppIcon :name="props.mode === 'semantic' ? 'sparkle' : 'list'" :size="12" />{{ modeLabel }}
        </button>
        <button
          v-if="props.mode === 'lexical'"
          class="ss-chip"
          :title="scopeTitle"
          @click="nextScope"
        >
          <AppIcon name="filter" :size="12" />{{ scopeLabel }}
        </button>
      </div>
    </div>

    <div class="ss-hits">
      <!-- 词法命中：与目录同一种跳转（点一行跳到正文那一行） -->
      <template v-if="props.mode === 'lexical'">
        <button
          v-for="hit in props.hits"
          :key="hit.id"
          class="ss-hit"
          :title="`跳到第 ${hit.page} 页这一行`"
          @click="emit('focus', hit.id, hit.page)"
        >
          <span class="tiny faint nowrap">p{{ hit.page }} · {{ hit.side === 'en' ? '原文' : '译文' }}</span>
          <span class="tiny ss-snippet" v-html="snippetHtml(hit.snippet)" />
        </button>
        <div v-if="props.query.trim() && !props.hits.length" class="empty small">没有匹配</div>
      </template>

      <!-- 语义命中：类型 + 页码 + 摘要片段；点一条跳到那一页 -->
      <template v-else>
        <div v-if="semantic.loading" class="empty small">检索中…</div>
        <template v-else-if="semantic.error">
          <div class="ss-err">
            <div class="tiny">语义检索失败：{{ semantic.error }}</div>
            <button class="btn sm" @click="runSemantic">重试</button>
          </div>
        </template>
        <template v-else>
          <button
            v-for="hit in semantic.items"
            :key="hit.chunk_id"
            class="ss-hit"
            :disabled="!hit.page"
            :title="hit.page ? `跳到第 ${hit.page} 页` : '这条命中没有页码，无法跳转'"
            @click="emit('goto-page', hit.page as number)"
          >
            <span class="tiny faint nowrap">
              {{ typeLabel(hit) }}<template v-if="hit.page"> · p{{ hit.page }}</template>
            </span>
            <!-- 插图 / 表格 / 公式的裁图（正文命中没有这一块） -->
            <img
              v-if="hitImage(hit)"
              class="ss-fig"
              :src="hitImage(hit)"
              alt=""
              loading="lazy"
            >
            <span class="tiny ss-snippet" v-html="previewHtml(hit)" />
          </button>
          <div v-if="semantic.ran && !semantic.items.length" class="empty small">没有相近的段落</div>
          <div v-else-if="!semantic.ran" class="empty small">打字即在本文里检索</div>
        </template>
      </template>
    </div>
  </div>
</template>

<style scoped>
/* 自己撑满侧栏剩下的高度（同 `ReaderChat` 那条说明：`height: 100%` 会把底边推到侧栏外面，
   被 `.reader-aside` 的 `overflow: hidden` 裁掉约一个页签头的高度）。 */
.side-search { display: flex; flex-direction: column; flex: 1; min-height: 0; }
/* 只有"输入框 + 两枚小胶囊 + 结果"三层：命中条数由页签角标表示，状态文案一律不写长句
   （用户口径：按钮和说明文字都太多）。空结果只在列表里留一行短提示。 */
.ss-head { flex: none; padding: 8px 8px 4px; display: flex; flex-direction: column; gap: 6px; }
/* 输入框占满侧栏宽度（base.css 的 .search-field input 是给工具栏定的 180px） */
.ss-field { width: 100%; }
.ss-field input { width: 100%; min-width: 0; }
/* 一行两枚小胶囊：**与全站的胶囊同规格**（26px 高、白色 pill + 阴影、圆到底、不描边），
   跟侧栏那枚分段控件一眼像一家人（2026-10-03 统一：以前是 24px 无描边的灰底片、
   底色还是自调的 color-mix）。当前值写在脸上，点一下换成什么写在 title 里。 */
.ss-chips { display: flex; align-items: center; gap: 6px; }
.ss-chip {
  display: inline-flex; align-items: center; gap: 5px;
  height: var(--control-h-sm); padding: 0 11px;
  border: 1px solid transparent; border-radius: var(--radius-ctl);
  background: var(--bg-elev); box-shadow: var(--shadow-1);
  color: var(--text-dim); font-size: calc(12px * var(--ts, 1)); line-height: 1; cursor: pointer;
  transition: background var(--dur) var(--ease), color var(--dur) var(--ease),
              box-shadow var(--dur) var(--ease);
}
/* 悬浮走主题色（用户口径 2026-12-03）：与侧栏那批图标钮同一口径，别在悬浮时变正文黑 */
.ss-chip:hover { color: var(--accent-hover); }
/* 「语义」是另一种检索范式：亮一块强调色底，提醒"现在不是按字面搜"
   （与 `.btn.on` 同一种"开关开着"的语义，见 base.css） */
.ss-chip.on { background: var(--accent-soft); box-shadow: none; color: var(--accent-text); font-weight: 600; }
.ss-hits { flex: 1; min-height: 0; overflow-y: auto; overflow-x: hidden; padding: 0 4px 28px; }
/* 语义命中里的裁图：与「插图」页签里那张卡片同源（后端 chunk_image_url 的裁图）。
   `contain` + 132px 上限：宽图和竖图都不变形，也不把一条命中撑成整屏。 */
.ss-fig {
  display: block; width: 100%; max-height: 132px; object-fit: contain;
  margin: 3px 0; border-radius: 8px;
  background: var(--bg-elev); border: 1px solid var(--divider);
}
/* 一条命中 = 一个整块可点的"行"（与目录项同一种交互：点了跳正文） */
.ss-hit {
  display: flex; flex-direction: column; gap: 2px; width: 100%;
  padding: 5px 6px; border-radius: var(--radius-sm); cursor: pointer;
  color: var(--text-dim); text-align: left;
}
.ss-hit:hover:not(:disabled) { background: var(--bg-hover); }
.ss-hit:disabled { cursor: default; opacity: .7; }
.ss-snippet { color: var(--text); line-height: 1.45; overflow-wrap: anywhere; }
/* 片段现在是渲染过的 Markdown：把块级元素压成"一行片段"的排版（不引入段间距） */
.ss-snippet :deep(p) { margin: 0; display: inline; }
.ss-snippet :deep(.katex-display) { margin: 0; display: inline-block; max-width: 100%; overflow-x: auto; }
/* 命中词：青绿底 + 原字色（不用 <mark> 的默认黄底，保持全站单一强调色）。
   注意 `:deep()`：内容是 v-html 注入的，拿不到 scoped 属性，直接写 `.ss-snippet mark` 不生效 */
.ss-snippet :deep(mark) {
  background: color-mix(in srgb, var(--accent) 24%, transparent);
  color: inherit; border-radius: 3px; padding: 0 1px;
}
/* 语义失败：一行提示 + 一个重试按钮（`.item` 那套卡片样式是 ReaderSidePanel 的 scoped 样式，
   这里用不了，所以就地写一份同观的） */
.ss-err {
  display: flex; flex-direction: column; gap: 6px; margin-bottom: 8px;
  padding: 8px 10px; border: 1px solid var(--border); border-left: 2px solid var(--danger);
  border-radius: var(--radius-sm); background: var(--bg-layer);
}
</style>
