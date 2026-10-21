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
import { highlightCss } from '@/utils/highlightColors';
import AppIcon from '@/components/AppIcon.vue';
import type { OverlayAnchor } from '@/api/types';

const props = withDefaults(defineProps<{
  items: MarkdownFormula[];
  loading?: boolean;
  /** 已被标注的公式：LaTeX 规范化 → 那一条高亮（带颜色）与它的笔记。
   *  高亮存的就是公式的原始 LaTeX（见 `services/agent/annotations.create`），所以能直接比。
   *  2026-12-06：从"集合"升级成"带色与笔记的映射" —— 圆点要显示**高亮自己的颜色**，
   *  点它要能**打开那条笔记**（用户口径）。 */
  marked?: Map<string, { color: string; highlight: any; note: any | null }>;
}>(), { loading: false, marked: () => new Map() });

/** 与后端同一套规范化：空白一律去掉，`$` 包裹也去掉（`\\frac {a} {b}` = `\\frac{a}{b}`）。 */
function normLatex(value?: string): string {
  return String(value || '').replace(/\$/g, '').replace(/\s+/g, '');
}

/** 这条公式的标注信息（没有就是 undefined）。 */
function markOf(f: MarkdownFormula) {
  return props.marked.get(normLatex(f.latex));
}

/** 点圆点：打开那一条笔记；没有笔记时也打开浮层（不传 note 就是 view 模式新建）。 */
function openMark(f: MarkdownFormula, ev: MouseEvent) {
  ev.stopPropagation();
  const info = markOf(f);
  if (!info) return;
  emit('open-note', { note: info.note ?? null, highlight: info.highlight, mode: 'view' });
}

const emit = defineEmits<{
  (e: 'focus', rowId: string, page?: number): void;
  (e: 'copy', text: string): void;
  /** 浮层打开：交给阅读器那张 `BlockOverlay`（左上角只有「公式」两个字 —— 公式自己没有标题） */
  /** 点「已标注」圆点：打开那一条笔记（交给阅读器的笔记浮层） */
  (e: 'open-note', p: { note: any | null; highlight: any; mode?: 'view' | 'edit' }): void;
  (e: 'overlay', p: {
    icon: string; title: string; titleMd?: string;
    markdown: string; anchor?: OverlayAnchor | null;
  }): void;
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
function openOverlay(f: { body: string; display: boolean }, ev?: MouseEvent) {
  emit('overlay', {
    icon: 'formula',
    title: '公式',
    markdown: f.display ? `$$${f.body}$$` : `$${f.body}$`,
    /* 浮层贴着这枚按钮下方打开（用户口径：不要跑到左上角） */
    anchor: anchorOf(ev),
  });
}

function anchorOf(ev?: Event): OverlayAnchor | null {
  const r = (ev?.currentTarget as HTMLElement | null)?.getBoundingClientRect();
  return r ? { x: r.left, y: r.top, w: r.width, h: r.height } : null;
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
        <!-- 标过的公式：圆点用**这条高亮自己的颜色**，点开就是那条笔记（用户口径 2026-12-06） -->
        <button
          v-if="markOf(f)"
          type="button"
          class="marked-dot"
          :style="{ background: highlightCss(markOf(f)!.color) }"
          :title="markOf(f)!.note ? '打开这条公式的笔记' : '这条公式已标注（无笔记）'"
          @click="openMark(f, $event)"
        />
        <span class="spacer" />
        <button class="btn icon sm" title="浮层打开" data-formula-overlay @click="openOverlay(f, $event)">
          <AppIcon name="pip" :size="13" />
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
/* 「已标注」：小圆点走主题色，视觉重量与 `.badge` 同级（不抢公式本身） */
.marked-dot { flex: none; width: 6px; height: 6px; border-radius: 50%; background: var(--accent);
  box-shadow: 0 0 0 2px color-mix(in srgb, var(--accent) 18%, transparent); }
.marked-sum { margin-left: auto; padding-left: 6px; }
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
