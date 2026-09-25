<script setup lang="ts">
/**
 * 侧栏「搜索」页签：**上面输入、下面命中列表**。
 *
 * 以前搜索框挂在阅读器工具栏、命中列表塞在「目录」页签最前面，两处都不合适：
 * 工具栏那一行是"对整篇文档 + 视图 + 翻译"的操作区，挤一个输入框进去会让主次不分；
 * 命中列表和目录混在一起，翻目录时被一段搜索结果顶开。现在搜索自成页签。
 *
 * 每条结果给两样东西（用户口径："显示相关词的情况"）：
 * - 命中位置：`p3 · 原文/译文`；
 * - 命中词所在的**原句片段**，并把命中词本身标出来（`<mark>`）。
 * 点一条 = 与目录同一种跳转（`focus`），滚到正文那一行。
 *
 * 片段按 **Markdown** 渲染（`$公式$`、`**加粗**` 直接显示成排版后的样子），命中词用
 * `htmlWithMarks` 的占位符机制在解析后插回 `<mark>` —— 与正文高亮同一套做法，
 * 所以标记不会打断 Markdown 结构。
 */
import { nextTick, ref } from 'vue';
import AppIcon from '@/components/AppIcon.vue';
import { htmlWithMarks } from '@/composables/useMarkdown';

/** 搜索范围：全部（原文 + 译文）/ 只看原文 / 只看译文。 */
export type SearchScope = 'all' | 'en' | 'zh';

const SCOPE_OPTIONS: Array<{ k: SearchScope; label: string; title: string }> = [
  { k: 'all', label: '全部', title: '原文 + 译文都搜' },
  { k: 'en', label: '原文', title: '只搜英文原文' },
  { k: 'zh', label: '译文', title: '只搜中文译文' },
];

const props = defineProps<{
  /** 与视图里的 `searchQuery` 双向绑定（`v-model:query`）：深链 `?q=`、快捷键 `/` 都写它 */
  query: string;
  /** 搜索范围（`v-model:scope`） */
  scope: SearchScope;
  hits: Array<{ id: string; page: number; side: string; snippet: string }>;
}>();

const emit = defineEmits<{
  (e: 'update:query', v: string): void;
  (e: 'update:scope', v: SearchScope): void;
  (e: 'search'): void;
  (e: 'clear'): void;
  (e: 'focus', id: string, page?: number): void;
}>();

const box = ref<HTMLInputElement | null>(null);

/** 让外部（快捷键 `/`）把光标放进搜索框 */
function focusInput() { void nextTick(() => box.value?.focus()); }
defineExpose({ focusInput });

function onInput(ev: Event) { emit('update:query', (ev.target as HTMLInputElement).value); }

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
</script>

<template>
  <div class="side-search">
    <div class="ss-head">
      <label class="search-field ss-field">
        <AppIcon name="search" :size="14" />
        <input
          ref="box"
          :value="props.query"
          placeholder="搜索"
          title="边打边搜原文 + 译文（回车立即搜；Esc 清空）"
          @input="onInput"
          @keyup.enter="emit('search')"
          @keyup.esc="emit('clear')"
        >
        <button
          v-if="props.query"
          class="btn icon sm ghost"
          title="清空搜索"
          @click="emit('clear')"
        >
          <AppIcon name="close" :size="12" />
        </button>
      </label>

      <!-- 搜索范围：沿用全站的分段控件（`.seg` = pill 胶囊组，与"原文/译文/对照"同一个外观语言），
           拉满侧栏宽度、三段等分，免得在窄栏里显得东倒西歪。 -->
      <div class="seg ss-scope" title="搜索范围">
        <button
          v-for="opt in SCOPE_OPTIONS"
          :key="opt.k"
          :class="{ active: props.scope === opt.k }"
          :title="opt.title"
          @click="emit('update:scope', opt.k)"
        >{{ opt.label }}</button>
      </div>
    </div>

    <div class="ss-hits">
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
      <div v-if="props.query.trim() && !props.hits.length" class="empty small">没有匹配的段落</div>
    </div>
  </div>
</template>

<style scoped>
.side-search { display: flex; flex-direction: column; height: 100%; min-height: 0; }
/* 只有"输入框 + 范围 + 结果"三层：命中条数已经由页签上的角标表示，不再单独写一行状态文案
   （用户口径：冗余文字太多）。空结果只留下面那一行提示。 */
.ss-head { flex: none; padding: 8px 8px 4px; display: flex; flex-direction: column; gap: 6px; }
/* 输入框占满侧栏宽度（base.css 的 .search-field input 是给工具栏定的 180px） */
.ss-field { width: 100%; }
.ss-field input { width: 100%; min-width: 0; }
/* 范围分段：拉满宽度 + 三段等分 + **文字居中**。
   ⚠️ 必须显式写 `justify-content: center`：base.css 的 `.seg button` 是 `inline-flex` 但没有
   `justify-content`，宽度一被 `flex: 1` 拉开，文字就贴在格子左边（用户报过"文字没居中"）。 */
.ss-scope { display: flex; width: 100%; }
.ss-scope button { flex: 1; padding: 0 6px; justify-content: center; }
.ss-hits { flex: 1; min-height: 0; overflow-y: auto; overflow-x: hidden; padding: 0 4px 28px; }
/* 一条命中 = 一个整块可点的"行"（与目录项同一种交互：点了跳正文） */
.ss-hit {
  display: flex; flex-direction: column; gap: 2px; width: 100%;
  padding: 5px 6px; border-radius: var(--radius-sm); cursor: pointer;
  color: var(--text-dim); text-align: left;
}
.ss-hit:hover { background: var(--bg-hover); }
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
</style>
