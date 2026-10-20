<script setup lang="ts">
/**
 * 表格列表（侧栏「表格」页签）：与「公式」页签**同一门语言** ——
 * 一行"页码 + 表号 + 尺寸 + 操作"，下面是表本体。
 *
 * 为什么表体按 Markdown 渲染：页级产物里表格本来就是 GFM 表（`| a | b |`），
 * 译文那一半是模型**逐格翻过的中文表** —— 所以"中文表格"不需要另做一份数据。
 * 表格说明（`表 1：…`）也是同一份产物里的那一行，中文优先（见后端 `markdown_tables`）。
 *
 * 窄栏（300px 上下）放不下一张宽表：表体外层横向滚动、单元格不折行 ——
 * 折行会让"第几列"对不上号，比横向滚更难读。
 */
import { computed } from 'vue';
import type { MarkdownTable } from '@/api/types';
import { mdToHtml } from '@/composables/useMarkdown';
import AppIcon from '@/components/AppIcon.vue';

const props = withDefaults(defineProps<{
  items: MarkdownTable[];
  loading?: boolean;
}>(), { loading: false });

const emit = defineEmits<{
  (e: 'focus', rowId: string, page?: number): void;
  (e: 'copy', text: string): void;
  /** 浮层打开：标题取表格说明（没有就写「表格」），见 `openOverlay` */
  (e: 'overlay', p: { icon: string; title: string; markdown: string }): void;
}>();

/** 说明是 Markdown（常带 `**表 1：**` 与 `$d_{\text{model}}$`），标题栏要的是**纯文字** */
function plainTitle(md: string): string {
  return String(md || '')
    .replace(/\*\*|__|`/g, '')
    .replace(/\$/g, '')
    .replace(/\s+/g, ' ')
    .trim();
}

function openOverlay(t: { markdown: string; caption: string; number: string }) {
  const cap = plainTitle(t.caption);
  emit('overlay', {
    icon: 'table',
    title: cap || (t.number ? `表 ${t.number}` : '表格'),
    markdown: t.markdown,
  });
}

/** 表格说明也走 Markdown（说明里常带 `$d_{\text{model}}$` 这种公式） */
const rows = computed(() => props.items.map((t) => ({
  ...t,
  html: mdToHtml(t.markdown),
  captionHtml: t.caption ? mdToHtml(t.caption) : '',
})));
</script>

<template>
  <div class="tl">
    <div v-if="props.loading" class="empty small">正在提取表格…</div>
    <div v-else-if="!props.items.length" class="empty small">
      这一篇还没有可提取的表格 —— 表格来自页级译文，先把那几页翻译了才会有
    </div>

    <div v-for="t in rows" :key="t.id" class="titem">
      <div class="row tiny">
        <span class="faint tpage">p{{ t.page }}</span>
        <span v-if="t.number" class="badge dim" :title="`论文里这张表的编号：表 ${t.number}`">表 {{ t.number }}</span>
        <span class="badge dim tnum" title="表体行数 × 列数">{{ t.cols }} 列 × {{ t.rows }} 行</span>
        <span
          v-if="t.source === 'original'"
          class="badge dim"
          title="这一页还没有中文译文，表头与格内文字都是原文"
        >原文</span>
        <span class="spacer" />
        <button class="btn icon sm" title="浮层打开" data-table-overlay @click="openOverlay(t)">
          <AppIcon name="expand-diag" :size="13" />
        </button>
        <button class="btn icon sm" title="复制 Markdown 表格" @click="emit('copy', t.markdown)">
          <AppIcon name="copy" :size="13" />
        </button>
        <button
          v-if="t.row_id"
          class="btn icon sm"
          title="定位到正文"
          @click="emit('focus', t.row_id, t.page)"
        >
          <AppIcon name="locate" :size="13" />
        </button>
      </div>
      <!-- 表格说明：中文优先，英文原文留在 title 里（鼠标停一下能对照） -->
      <div
        v-if="t.captionHtml"
        class="tcap md"
        :title="t.caption_original || ''"
        v-html="t.captionHtml"
      />
      <div class="tbody" v-html="t.html" />
    </div>
  </div>
</template>

<style scoped>
.tl { display: flex; flex-direction: column; gap: 6px; }
.titem {
  border: 1px solid var(--border); border-radius: var(--radius-sm);
  background: var(--bg-layer); padding: 6px 8px;
}
/* 页码不许折行（窄栏里 "p3" 曾被拆成两行）；徽章过长则省略，别把按钮挤出去 */
.tpage { white-space: nowrap; }
.titem .badge.dim { max-width: 104px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.tnum { font-variant-numeric: tabular-nums; }
.tcap {
  margin: 4px 0 2px; color: var(--text-dim);
  font-size: calc(11.5px * var(--ts, 1)); line-height: 1.55;
}
.tcap :deep(p) { margin: 0; }
.tcap :deep(strong) { color: var(--text); }
.tcap :deep(.katex) { font-size: 1.02em; }
/* 表体：横向滚动、竖向裁掉（表高由内容决定，卡片不内部滚动） */
.tbody { margin: 4px 0 2px; overflow-x: auto; overflow-y: hidden; padding-bottom: 2px; }
.tbody :deep(table) {
  border-collapse: collapse;
  font-size: calc(11.5px * var(--ts, 1)); line-height: 1.45;
}
/* 单元格不折行：窄栏里折过之后"第几列"就对不上号了，横向滚比折行好读 */
.tbody :deep(th), .tbody :deep(td) {
  border: 1px solid var(--divider);
  padding: 3px 6px; text-align: left; vertical-align: top;
  white-space: nowrap;
}
.tbody :deep(th) { background: var(--bg-sunken); color: var(--text); font-weight: 600; }
.tbody :deep(td) { color: var(--text-dim); }
.tbody :deep(.katex) { font-size: 1.02em; }
</style>
