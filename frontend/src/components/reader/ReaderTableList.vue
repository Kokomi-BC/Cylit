<script setup lang="ts">
/**
 * 表格列表（侧栏「表格」页签）：与「插图」页签**同一张脸** ——
 * 卡头一行"表号 → 页码 → 尺寸 → 操作"，中间是表本体，最后一行是表格说明。
 *
 * ⚠️ 顺序是用户口径：**编号在最前、页码紧随其后**（表格与插图都按这一套），
 * 说明从"表体上方"挪到"表体下方"，与插图「图在上、说明在下」对齐。
 * 卡片壳、卡头、说明用的都是 base.css 里的 `.rvc*`（与插图共用，别再各写一套）。
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
import type { OverlayAnchor } from '@/api/types';

const props = withDefaults(defineProps<{
  items: MarkdownTable[];
  loading?: boolean;
}>(), { loading: false });

const emit = defineEmits<{
  (e: 'focus', rowId: string, page?: number): void;
  (e: 'copy', text: string): void;
  /** 浮层打开：标题取表格说明（没有就写「表格」），见 `openOverlay` */
  (e: 'overlay', p: {
    icon: string; title: string; titleMd?: string;
    markdown: string; anchor?: OverlayAnchor | null;
    /** 浮层上「定位」要用的落点（2026-12-06）：行 id + 页 + 公式 LaTeX */
    rowId?: string; page?: number; latex?: string;
  }): void;
}>();

/** 说明是 Markdown（常带 `**表 1：**` 与 `$d_{\text{model}}$`），标题栏要的是**纯文字** */
function plainTitle(md: string): string {
  return String(md || '')
    .replace(/\*\*|__|`/g, '')
    .replace(/\$/g, '')
    .replace(/\s+/g, ' ')
    .trim();
}

function openOverlay(
  t: { markdown: string; caption: string; number: string; row_id?: string | null; page?: number },
  ev?: MouseEvent,
) {
  const cap = plainTitle(t.caption);
  const r = (ev?.currentTarget as HTMLElement | null)?.getBoundingClientRect();
  emit('overlay', {
    icon: 'table',
    title: cap || (t.number ? `表 ${t.number}` : '表格'),
    /* 标题走 **Markdown**：表格说明里常有公式（纯文字版只是兜底 / 无障碍名字） */
    titleMd: t.caption || '',
    markdown: t.markdown,
    anchor: r ? { x: r.left, y: r.top, w: r.width, h: r.height } : null,
    /* 浮层上的「定位」要用（用户口径 2026-12-06：图片/表格/公式浮层都要能定位回正文） */
    rowId: t.row_id || '', page: t.page,
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

    <div v-for="t in rows" :key="t.id" class="rvc">
      <div class="rvc-head">
        <span v-if="t.number" class="rvc-num" :title="`论文里这张表的编号：表 ${t.number}`">表 {{ t.number }}</span>
        <span v-if="t.page" class="rvc-page">p{{ t.page }}</span>
        <span class="badge dim tnum" title="表体行数 × 列数">{{ t.cols }} 列 × {{ t.rows }} 行</span>
        <span
          v-if="t.source === 'original'"
          class="badge dim"
          title="这一页还没有中文译文，表头与格内文字都是原文"
        >原文</span>
        <span class="spacer" />
        <button class="btn icon sm" title="浮层打开" data-table-overlay @click="openOverlay(t, $event)">
          <AppIcon name="pip" :size="13" />
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
      <!-- ⚠️ 这里必须带 md 类：表体的观感只由 base.css 的 .md table 决定，这一层不再自己画 -->
      <div class="rvc-body tbody md" v-html="t.html" />
      <!-- 表格说明：放在表体**之后**（与插图"图在上、说明在下"同一顺序）；
           中文优先，英文原文留在 title 里（鼠标停一下能对照） -->
      <div
        v-if="t.captionHtml"
        class="rvc-cap md"
        :title="t.caption_original || ''"
        v-html="t.captionHtml"
      />
    </div>
  </div>
</template>

<style scoped>
/* 卡片壳 / 卡头 / 说明统一走 base.css 的 .rvc*（与「插图」页签同一份），这里只剩表体自己 */
.tl { display: flex; flex-direction: column; }
.tnum { font-variant-numeric: tabular-nums; }
/* 表体：以前这里另画了一套「1px 方格 + 表头灰底 + 数字灰字」，跟站内别处的论文式三线表
   不是一张脸（用户口径 2026-12-06："表格显示与 Markdown 的渲染风格一致"）。现在观感全部交给
   base.css 的 .md table（顶线 2px / 表头线 1px / 底线 2px、无竖线、数字列右对齐、分组细线），
   这一层只剩两条窄栏自己的约束：
   ① 字号跟着侧栏的 --ts 走（三线表本身是 .93em，所以这里给 12.5px 的基准）；
   ② 单元格不折行 —— 窄栏里折过之后"第几列"就对不上号了，横向滚（.tbl-wrap）比折行好读。 */
.tbody { margin: 2px 0 0; font-size: calc(12.5px * var(--ts, 1)); }
.tbody :deep(th), .tbody :deep(td) { white-space: nowrap; }
.tbody :deep(.katex) { font-size: 1.02em; }
</style>
