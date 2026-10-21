<script setup lang="ts">
/**
 * 左侧目录：**中英双语**（英文原文在上、中文译文在下）+ 可折叠层级。
 *
 * 数据来自后端 `outline`：标题行由页级翻译同一次调用产出（英文 `text` / 中文 `text_zh`），
 * 因此中英始终成对；没有页级产物的论文只有英文（`text_zh` 为空）。
 *
 * 标题里的 **行内 Markdown / LaTeX** 必须渲染出来（`C.1 TURBOQUANT$_{\mathrm{mse}}$` 之前
 * 直接把 `$…$` 原样显示，目录里全是反斜杠）。用 `inlineHtml()` 而不是 `mdToHtml()`：
 * 标题是单行文本，`parseInline` 不会套上 `<p>` 造成行高跳动。
 *
 * 另外 `number` 与 `text` 是**包含关系**（后端 text 自带 "C.1 " 前缀），两个都渲染就会出现
 * "C.1C.1 TURBOQUANT…"，所以文本侧要把已单独显示的前缀去掉。
 */
import { computed } from 'vue';
import type { TocRow } from '@/utils/format';
import AppIcon from '@/components/AppIcon.vue';
import { inlineHtml } from '@/composables/useMarkdown';

const props = defineProps<{
  rows: TocRow[];
  currentRowId: string;
  pageCount: number;
  collapsed: string[];
  /** 不画自己那行「目录 N + 折叠/展开」：由 ReaderView 与「目录 / 页面视图」切换同一行画 */
  headless?: boolean;
}>();

const emit = defineEmits<{
  (e: 'focus', rowId: string): void;
  (e: 'toggle', rowId: string): void;
  (e: 'collapse-all'): void;
  (e: 'expand-all'): void;
}>();

const collapsedSet = computed(() => new Set(props.collapsed));

/** 标题正文里若已包含单独显示的编号前缀，就去掉它（`C.1 C.1 xxx` → `C.1 xxx`）。 */
function dropNumber(text: string, number: string): string {
  const n = (number || '').trim();
  const t = (text || '').replace(/^\s+/, '');
  if (!n || !t.startsWith(n)) return text;
  return t.slice(n.length).replace(/^[\s.:、)\]]+/, '');
}

/** 渲染结果一次性算好：目录行数不少，别在每次滚动触发的重渲染里重复解析。 */
const items = computed(() =>
  props.rows.map((row) => ({
    row,
    html: inlineHtml(dropNumber(row.text, row.number)),
    zhHtml: row.textZh ? inlineHtml(dropNumber(row.textZh, row.number)) : '',
  })),
);
</script>

<template>
  <div class="toc">
    <!-- `headless`：目录页签里那行「目录 N + 折叠/展开」由 ReaderView 统一画
         （与「目录 / 页面视图」切换同一行，省掉一整行高度，也不再重复"目录"两个字）。 -->
    <div v-if="!props.headless" class="toc-head">
      <span class="tiny faint">目录 {{ props.rows.length }}</span>
      <span class="spacer" />
      <button class="btn icon sm" title="全部折叠" @click="emit('collapse-all')">
        <AppIcon name="unfold-less" :size="14" />
      </button>
      <button class="btn icon sm" title="全部展开" @click="emit('expand-all')">
        <AppIcon name="unfold-more" :size="14" />
      </button>
    </div>

    <div
      v-for="it in items"
      :key="it.row.id"
      class="toc-item"
      :class="[`depth-${Math.min(it.row.depth, 4)}`, { active: it.row.id === props.currentRowId }]"
      @click="emit('focus', it.row.id)"
    >
      <button
        v-if="it.row.hasChildren"
        class="caret"
        :title="collapsedSet.has(it.row.id) ? '展开' : '折叠'"
        @click.stop="emit('toggle', it.row.id)"
      >
        <AppIcon :name="collapsedSet.has(it.row.id) ? 'chevron-right' : 'chevron-down'" :size="13" />
      </button>
      <span v-else class="caret-space" />
      <span class="txt">
        <span class="en truncate">
          <span v-if="it.row.number" class="num">{{ it.row.number }}</span>
          <span class="md" v-html="it.html" />
        </span>
        <span v-if="it.zhHtml" class="zh truncate md" v-html="it.zhHtml" />
      </span>
      <span class="pg">p{{ it.row.page }}</span>
    </div>
    <div v-if="!props.rows.length" class="tiny faint" style="padding:8px">未识别到标题结构</div>
  </div>
</template>

<style scoped>
/* 滚动交给外层 `.toc-pane`（**只留一个滚动容器**：两个嵌套滚动区在触摸下很别扭，
   而且这里 `height: 100%` 曾经因为父元素没高度而落不了地 —— 见 ReaderView 里那段注释）。 */
.toc { padding: 6px 6px 32px; box-sizing: border-box; }
.toc-head { display: flex; align-items: center; gap: 4px; padding: 3px 6px; }
/* 搜索命中**不再挂在目录里**：搜索已经独立成侧栏的「搜索」页签（`ReaderSearch`），
   一份结果放两处既冗余、又会在翻目录时被顶开。 */
.toc-item {
  display: flex; align-items: flex-start; gap: 3px;
  padding: 4px 6px; border-radius: var(--radius-sm); cursor: pointer;
  color: var(--text-dim); min-width: 0;
}
.toc-item:hover { background: var(--bg-hover); }
.toc-item.active { background: var(--accent-soft); }
.toc-item.active .txt .en { color: var(--accent-text); }
.toc-item .txt { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 1px; }
.toc-item .en { font-size: calc(12.5px * var(--ts, 1)); font-weight: 500; }
.toc-item .zh { font-size: calc(11.5px * var(--ts, 1)); color: var(--text-faint); }
/* KaTeX 默认 1.21em，在 11.5~12.5px 的目录行里会显得比中文大一圈；压回正文尺寸。 */
.toc-item .md :deep(.katex) { font-size: 1em; }
.toc-item .md :deep(strong) { font-weight: 600; }
.toc-item .md :deep(code) { font-size: .95em; padding: 0 3px; }
.toc-item .num { font-family: var(--mono); font-size: calc(11px * var(--ts, 1)); color: var(--text-faint); margin-right: 4px; }
.toc-item .pg { font-size: calc(10.5px * var(--ts, 1)); color: var(--text-faint); font-variant-numeric: tabular-nums; margin-left: 4px; }
.caret {
  border: 0; background: transparent; cursor: pointer; color: var(--text-faint);
  padding: 1px 0 0; line-height: 1; width: 15px; display: inline-flex; justify-content: center;
}
/* 悬浮走主题色（用户口径 2026-12-03）：侧栏里这一排小控件都该是主题色系 */
.caret:hover { color: var(--accent-hover); }
.caret-space { display: inline-block; width: 15px; flex: none; }
.depth-0 { padding-left: 6px; }
.depth-1 { padding-left: 15px; }
.depth-2 { padding-left: 26px; }
.depth-3 { padding-left: 37px; }
.depth-4 { padding-left: 48px; }
.hits { margin-top: 10px; border-top: 1px solid var(--divider); padding-top: 6px; }
</style>
