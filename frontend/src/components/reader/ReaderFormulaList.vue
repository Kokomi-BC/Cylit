<script setup lang="ts">
/**
 * 公式列表（右栏/左栏共用的"公式"页签）：**译文优先，未翻译的页用原文兜底**，只读。
 *
 * 为什么这样：阅读器显示的公式就是模型看页图给出的 LaTeX，来源是**页级产物**的 Markdown；
 * 所以列表优先也从那份 Markdown 里抽（`GET /papers/{id}/formulas`），与页面上的公式一字不差。
 * **没有页级产物的页**（还没翻译/转录）正文显示的是解析块，那几页就按原文兜底 ——
 * 否则一篇还没翻译的论文会显示"这一篇还没有可提取的公式"，而正文里明明有公式。
 * 兜底条目带一枚「解析」徽章：让用户一眼看出它不是从译文来的（口径：优先且只显示翻译的公式）。
 * **不做复核、不比对裁图** —— 看到不对就复制 LaTeX 自己改。
 */
import { computed, ref } from 'vue';
import type { MarkdownFormula } from '@/api/types';
import { latexToHtml } from '@/composables/useMarkdown';
import AppIcon from '@/components/AppIcon.vue';

const props = withDefaults(defineProps<{
  items: MarkdownFormula[];
  loading?: boolean;
}>(), { loading: false });

const emit = defineEmits<{
  (e: 'focus', rowId: string, page?: number): void;
  (e: 'copy', text: string): void;
  /** 浮层打开：交给阅读器那张 `BlockOverlay`（左上角只有「公式」两个字 —— 公式自己没有标题） */
  (e: 'overlay', p: { icon: string; title: string; markdown: string }): void;
}>();

/** 默认只列行间公式（论文里编号的公式）；行内公式多达数百条，按需切换。 */
const filter = ref<'display' | 'inline' | 'all'>('display');
const filtered = computed(() => {
  if (filter.value === 'all') return props.items;
  const want = filter.value === 'display';
  return props.items.filter((f) => f.display === want);
});
const counts = computed(() => ({
  display: props.items.filter((f) => f.display).length,
  inline: props.items.filter((f) => !f.display).length,
}));

/**
 * 去掉公式右侧的编号：解析出来的 LaTeX 常带 `\tag{1}`（页面上那是排版里的公式号，
 * 放在这个窄栏里会跟公式挤在一起，也没必要）。`\qquad(1)` / `\quad(12)` 这种写法一并去掉。
 * 页面正文里渲染的公式**不受影响**（那里保留 (1)(2) 才和原论文一致）。
 */
function stripEqTag(latex: string): string {
  return String(latex ?? '')
    .replace(/\s*\\tag\*?\{[^{}]*\}/g, '')
    .replace(/\s*\\(?:qquad|quad)\s*\(\s*\d{1,3}[a-z]?\s*\)\s*$/i, '')
    // 兜底：`\hfill (2)` / `\hspace{1em}(2)` / 行尾裸 `(2)`（模型偶尔不写 \tag）
    .replace(/\s*\\(?:hfill|hspace\{[^{}]*\})\s*\(\s*\d{1,3}[a-z]?\s*\)\s*$/i, '')
    .replace(/\s{2,}\(\s*\d{1,3}[a-z]?\s*\)\s*$/i, '')
    .trim();
}

/** 公式左侧的小标签：`(\text{MSE})\quad …` 是论文给公式起的名字（MSE / inner-prod error）。
 *  留在公式里会把窄栏挤爆（用户反馈"挤压"），所以抽出来单独显示成徽章。 */
const LABEL_RE = /^\(\s*\\text\{([^{}]{1,40})\}\s*\)\s*(?:\\quad|\\qquad)?\s*/;

function splitLabel(latex: string): { label: string; body: string } {
  const text = stripEqTag(latex);
  const m = LABEL_RE.exec(text);
  if (!m) return { label: '', body: text };
  return { label: m[1].trim(), body: text.slice(m[0].length).trim() };
}

/** 浮层打开：**补回定界符**再交给 Markdown 渲染器（列表里的 `latex` 已经把 `$` 去掉了）。 */
function openOverlay(f: { body: string; display: boolean }) {
  emit('overlay', {
    icon: 'formula',
    title: '公式',
    markdown: f.display ? `$$${f.body}$$` : `$${f.body}$`,
  });
}

/** 列表项（顺手把要显示/复制的内容算好，模板里不再重复解析）。 */
const rows = computed(() => filtered.value.map((f) => {
  const { label, body } = splitLabel(f.latex);
  return { ...f, label, body };
}));
</script>

<template>
  <div class="fl">
    <div class="fl-bar">
      <div class="seg">
        <button :class="{ active: filter === 'display' }" @click="filter = 'display'">
          行间 {{ counts.display }}
        </button>
        <button :class="{ active: filter === 'inline' }" @click="filter = 'inline'">
          行内 {{ counts.inline }}
        </button>
        <button :class="{ active: filter === 'all' }" @click="filter = 'all'">
          全部 {{ props.items.length }}
        </button>
      </div>
    </div>

    <div v-if="props.loading" class="empty small">正在提取公式…</div>
    <div v-else-if="!filtered.length" class="empty small">这一篇还没有可提取的公式</div>

    <div v-for="f in rows" :key="f.id" class="fitem">
      <!-- 页码放最左侧（用户要求）：先扫页码，再看公式名；「行间/行内」胶囊已去掉（上面有分组筛选）。 -->
      <div class="row tiny">
        <span class="faint fpage">p{{ f.page }}</span>
        <span v-if="f.label" class="badge dim" :title="`论文里这条公式的名字：${f.label}`">{{ f.label }}</span>
        <!-- 解析产物兜底的条目（这一页还没翻译）：标一下来源，别让人以为它来自译文 -->
        <span
          v-if="f.source === 'parse'"
          class="badge dim"
          :title="`这一页还没翻译，公式来自解析产物${f.grade ? `（${f.grade} 级）` : ''}`"
        >解析{{ f.grade ? ` ${f.grade}` : '' }}</span>
        <span class="spacer" />
        <button class="btn icon sm" title="浮层打开" data-formula-overlay @click="openOverlay(f)">
          <AppIcon name="expand-diag" :size="13" />
        </button>
        <button class="btn icon sm" title="复制 LaTeX" @click="emit('copy', f.body)">
          <AppIcon name="copy" :size="13" />
        </button>
        <button
          v-if="f.row_id"
          class="btn icon sm"
          title="定位到正文"
          @click="emit('focus', f.row_id, f.page)"
        >
          <AppIcon name="locate" :size="13" />
        </button>
      </div>
      <div class="fbody" :class="{ display: f.display }" v-html="latexToHtml(f.body, f.display)" />
    </div>
  </div>
</template>

<style scoped>
.fl { display: flex; flex-direction: column; gap: 6px; }
/* top: -8px 吸收滚动容器的 padding-top：否则 sticky 栏头上会留 8px 缝，
   下面的公式正好从缝里露出半行（用户截图报过）。负数 offset 是 sticky 允许的。
   上下内边距（用户口径 2026-10-07）：与对话页工具栏**同一套位置** —— 胶囊距面板顶 8px
   （由 `.panel-body` 的 padding-top 给），下面留 6px 与列表分开（对话页也是 6px）。 */
.fl-bar {
  position: sticky; top: -8px; z-index: 2; padding: 0 0 6px;
  background: var(--bg-elev);
}
.fitem {
  border: 1px solid var(--border); border-radius: var(--radius-sm);
  background: var(--bg-layer); padding: 6px 8px;
}
.fbody { margin: 4px 0 2px; overflow-x: auto; overflow-y: hidden; font-size: calc(12.5px * var(--ts, 1)); padding-bottom: 2px; }
/* 窄栏里居中会让超长公式"看着被切"；左对齐从行首开始读，需要时横向滚动。 */
.fbody.display { text-align: left; }
.fbody :deep(.katex) { font-size: 1.02em; }
/* 页码不许折行（窄栏里 "p3" 曾被拆成两行，看着像错位）；标签过长则省略，别把按钮挤出去 */
.fpage { white-space: nowrap; }
.fitem .badge.dim { max-width: 116px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
</style>
