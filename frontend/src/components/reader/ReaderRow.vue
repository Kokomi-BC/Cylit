<script setup lang="ts">
/**
 * 一行对照中的**一侧**（原文或译文）。
 *
 * 两侧用同一套排版参数（字号 / 行距 / 内边距），差别只在字体族：
 * 英文用衬线、中文用无衬线 —— 这是"对照时两侧文字观感一致"的关键。
 */
import { computed, nextTick, onMounted, ref, watch } from 'vue';
import type { Highlight, PageRow } from '@/api/types';
import { mdToHtml } from '@/composables/useMarkdown';
import { formulaOrdinal, isFormulaText, markRanges, markSearchTerm } from '@/utils/domMarks';

const props = defineProps<{
  row: PageRow;
  side: 'en' | 'zh';
  highlights: Highlight[];
  /**
   * 论文 id：**必须传**，否则行里的相对图片路径（`../figures/x.png`）不会被改写成
   * 后端静态 URL，浏览器会按当前地址解析成 `/app/figures/...` → 404。
   * （实测事故：`haloscope` 那篇的公式/图注行带图，md 视图整篇 404。）
   */
  paperId?: string;
  /** 搜索词：命中处全部加 `mark.srch`（浏览器 Ctrl+F 那种全量高亮） */
  searchTerm?: string;
  /** 纯译文模式下：该行还没有译文 */
  missing?: boolean;
  /** 后台正在翻译这篇论文：此时没译文的行显示「等待翻译」而不是「未翻译」 */
  pending?: boolean;
  /** 紧凑模式（窄屏 / 纯原文译文单栏时用） */
  compact?: boolean;
}>();

const text = computed(() => (props.side === 'zh' ? props.row.translation : props.row.original));
/** 只渲染 Markdown；**高亮在 DOM 上画**（公式按整体标，见 `utils/domMarks.ts`） */
const html = computed(() => mdToHtml(text.value ?? '', props.paperId));
/** 本侧要画的高亮（偏移量相对**渲染后的文字**，公式算 1 个字符）。
 *
 * 同侧的直接按偏移量画；**另一面的也带进来一份，`loose` 只按文字找** —— 模型经常把原文的句子
 * 挂到「译文」那一面（或反过来），那一面根本没有这段字，于是「笔记在列表里、正文看不到高亮」
 * （用户口径 2026-11-26）。哪一面找得到就画在哪一面，找不到 markRanges 自己会跳过。 */
const myMarks = computed(() => (props.highlights ?? [])
  .filter((h) => h.block_id === props.row.id && (h.scope ?? 'row') === 'row')
  .map((h) => {
    const loose = (h.side ?? 'en') !== props.side;
    /* 跨面 + 公式：偏移量在另一面没有意义，按文字也找不到（LaTeX 不在渲染文本里，那里只有
       一个 U+FFFC）。两面公式的顺序一致，所以取"它在另一面里是第几条"，到这一面指同一个
       ATOM —— 这就是"跨面的公式高亮从来不画"的修法（2026-12-06）。 */
    const otherMd = props.side === 'zh' ? props.row.original : props.row.translation;
    const formula = isFormulaText(h.text) ? String(h.text) : '';
    const atom = loose && formula ? formulaOrdinal(otherMd ?? '', formula) : -1;
    return {
      start: h.start, end: h.end, color: h.color, id: h.id, text: h.text,
      /* 公式给 LaTeX：markRanges 会按它去认那枚 KaTeX（同面也认 —— 服务端偏移量在
         客户端少渲染一枚公式时就会错位，用户报的「位置不对」就是这么来的）。 */
      formula,
      atom,
      loose: loose && atom < 0,
      /* 跨行选择切出来的几条共用整段选区文字 → 文字校验必然失败，改信偏移量 */
      trust: Boolean(h.group_id),
    };
  }));

/**
 * 画标记：**先高亮、后搜索命中**（顺序有讲究 —— 重画高亮时会把搜索标记所在的文本拍平，
 * 所以搜索标记必须最后画）。见 `utils/domMarks.ts`。
 * `flush: 'post'` + nextTick：等 `v-html` 真正写进 DOM 之后再包，否则会在旧节点上白跑一趟。
 */
const body = ref<HTMLElement | null>(null);
async function paintMarks() {
  await nextTick();
  markRanges(body.value, myMarks.value);
  markSearchTerm(body.value, props.searchTerm ?? '');
}
watch([html, myMarks, () => props.searchTerm], () => { void paintMarks(); }, { flush: 'post' });
onMounted(() => { void paintMarks(); });
const level = computed(() => (props.row.type === 'heading' ? Math.min(4, Math.max(1, props.row.level || 2)) : 0));
const isEmpty = computed(() => !String(text.value ?? '').trim());
/* 跨页的两个徽章（「承上页」「续下页」）**已按用户要求拿掉**：
 * 译文现在以自然断句为先（页边界只是排版），半句直接读下去就好，不需要任何页边装饰。
 * 后端仍在行上打 `continues` / `continued_from_prev`（供脚本审计），前端不再展示。 */
/** 未译空位的文案：任务在跑 → 等待翻译；整页确实没译文 → 未翻译；
 *  其余（跨页承接句已在上页译完、配对边界）**留空不显示** —— 写个「—」反而像漏了东西。 */
const emptyLabel = computed(() => {
  if (props.side !== 'zh') return '';
  if (props.pending) return '等待翻译';
  return props.missing ? '未翻译' : '';
});
</script>

<template>
  <!-- ⚠️ 「本侧没有内容」这个状态类**不能叫 `empty`**：`base.css` 里的 `.empty` 是全局空态工具类
       （`padding: 28px 16px`，给"列表空了"那种占位用的）。名字撞上之后，每个空行都会吃进
       28px 的上下内边距 —— 一行空空如也却占 56px 高，看上去就是"算法块标题和正文之间多空了几行"
       （用户报过）。改叫 `is-empty`，只做标记，不带任何样式。 -->
  <div
    :class="['rrow', `side-${props.side}`, `t-${props.row.type}`,
             { heading: level > 0, [`lvl-${level}`]: level > 0, 'is-empty': isEmpty, compact: props.compact }]"
    :data-side="props.side"
  >
    <div v-if="html" ref="body" class="rr-body md" v-html="html" />
    <div v-else-if="emptyLabel" class="rr-empty">
      {{ emptyLabel }}
    </div>
  </div>
</template>

<style scoped>
/* 字号继承 `.rows-wrap` 的 --reader-font-size：这里全部用 em，正文/标题/图注/公式一起缩放。
   两侧只有字体族不同（英文衬线 / 中文无衬线），字号与行距完全一致。 */
.rrow { min-width: 0; cursor: text; position: relative; font-size: 1em; }
/* 行内悬浮复制按钮已按用户要求删除：要复制就选词用划词菜单（或直接 Ctrl+C）。 */
/* 排版规则与 base.css 的 `.md` 同一口径（见那里的注释）：中文禁则 + 中英混排自动留空隙。
   `overflow-wrap: anywhere` 保留：长公式 / URL 放不下时必须能断，否则会把窄栏顶宽。 */
.rr-body { font-size: 1em; line-height: 1.78; overflow-wrap: anywhere; line-break: strict; text-autospace: normal; }
/* 段落间距就交给 markdown 自己的 margin：行容器已不再额外留垂直内边距，
   所以相邻块之间正好是一个正常段落间距（约 .55em），不多不少。
   ⚠️ 不要在这里加 `p:last-child { margin-bottom: 0 }`：那会让相邻块贴在一起，
   本来分成两段的正文会看起来连成一段。 */
.rr-body :deep(p) { margin: 0 0 .55em; }
.rr-body :deep(h1), .rr-body :deep(h2), .rr-body :deep(h3), .rr-body :deep(h4) {
  margin: 0 0 .35em; font-weight: 600; line-height: 1.35;
}
.rr-body :deep(h1) { font-size: 1.4em; }
.rr-body :deep(h2) { font-size: 1.24em; }
.rr-body :deep(h3) { font-size: 1.1em; }
.rr-body :deep(h4) { font-size: 1em; }
.rr-body :deep(ul), .rr-body :deep(ol) { margin: .3em 0; padding-left: 1.35em; }
.rr-body :deep(.katex-display) { margin: .5em 0; overflow-x: auto; overflow-y: hidden; }
.side-en .rr-body { font-family: var(--serif); }
.side-zh .rr-body { font-family: var(--sans); }
.t-formula .rr-body { text-align: center; }
.t-caption .rr-body { font-size: .92em; color: var(--text-dim); }
.t-ref .rr-body { font-size: .88em; color: var(--text-dim); }
/* 表格：三线表样式（顶线/表头线/底线、数字列右对齐、分组细线）**统一在 base.css 的 `.md table`**，
 * 这里只补两件阅读器特有的事：
 *   ① 整张表放不下时在本格里横向滚动（不许顶宽整页）；
 *   ② 单元格不折行 —— 论文表格的数字折成两行就没法读了。
 * 以前这里自己写了一套"每行一条横线 + 所有单元格右对齐"，看起来是网页表格而不是论文表格。 */
.t-table .rr-body { overflow-x: auto; }
.t-table .rr-body :deep(table) { white-space: nowrap; }
.rr-empty { font-size: 1em; color: var(--text-faint); }
/* `---` 在正文里也不画横线（用户口径：移除分割线），只当一段空白 —— 与 Markdown 视图同一口径 */
.rr-body :deep(hr) { border: 0; height: 12px; margin: 0; background: none; }
.rrow.compact .rr-body { font-size: .96em; line-height: 1.7; }
/* 搜索命中：全量高亮。用**主题色**而不是黄色 —— 黄/绿/蓝/粉已经被用户高亮占了，
   主题色一眼就能区分"这是搜索标出来的"；`:deep` 是因为标记是运行时插进 v-html 里的。 */
.rr-body :deep(mark.srch) {
  background: color-mix(in srgb, var(--accent) 26%, transparent);
  color: inherit; border-radius: 3px; padding: 0 1px;
}
/* 搜索命中落在用户高亮里时，两层的底色叠起来会更重，稍微收一点 */
.rr-body :deep(mark.hl mark.srch) { background: color-mix(in srgb, var(--accent) 34%, transparent); }
</style>

