<script setup lang="ts">
/**
 * 解读正文的**共享渲染器**：阅读器左栏页签（「论文解读」）与整页（`/understand/:paperId`）都用它。
 *
 * 为什么要共享：同一份 markdown 有两个出口。两处各写一遍渲染，排版一定会各自漂移
 * （行宽、段间距、标题档位、表格样式、公式溢出…）。这里只负责"把 markdown 按
 * **阅读器的正文排版**画出来"，数据从 `useUnderstanding` 来、标题由各自的宿主渲染。
 *
 * 排版口径（对齐阅读器 `ReaderRow` 的 `.rr-body`）：行高 1.78、段间距 .55em、标题 em 档位、
 * 数字右对齐 + 不折行的表格、公式横向可滚。字号取 `--reader-font-size`（宿主设好）。
 * ⚠️ 那些数值是**抄** ReaderRow 的：改一处要同时看另一处（跨组件拿不到它的 scoped 样式）。
 */
import { computed, nextTick, onMounted, ref, watch } from 'vue';
import { mdToHtml } from '@/composables/useMarkdown';
import type { UnderstandingRefItem } from '@/api/types';
import { buildInsightOutline } from '@/utils/insightOutline';

const props = withDefaults(defineProps<{
  markdown?: string | null;
  /** 整篇都没有解读（与"还没生成"不同）：显示空状态 + 「开始解读」 */
  empty?: boolean;
  /** 首次加载中（还没有任何内容可显示） */
  loading?: boolean;
  starting?: boolean;
  /** 解读任务在跑（宿主按任务列表 / 本地"刚发起"算）：空状态里给一行"会自动刷新"的提示 */
  running?: boolean;
  /** 后端 `error.message`，原样显示 */
  error?: string;
  /** 轮询到上限：提示用户稍后再看 */
  timedOut?: boolean;
  /**
   * 整页阅读：正文列按阅读器的行宽收窄（没有侧栏的宿主用）。
   * 整页是自己给右栏定宽（`InsightPage` 的列宽 + 页边距），所以那里不再传它；
   * 这个 prop 留给"把正文直接放进一个很宽的容器"的场景。
   */
  narrow?: boolean;
  /**
   * 标题 anchor 前缀：宿主拿**同一个前缀**调 `buildInsightOutline()` 生成目录，
   * 这里按序把 id 贴到渲染出来的 `h1..h6` 上。两边的 id 必须同源，
   * 否则点目录会滚到别处（所以前缀是必填语义，默认值只作兜底）。
   */
  anchorPrefix?: string;
  /**
   * 论文 id：与 `mdToHtml` 的第二个参数同义 —— 后端产物里的图片是相对路径
   * （`../figures/x.png`），不给 paperId 就不会改写成 `/papers/<id>/figures/...`，
   * 浏览器按当前地址解析成 `/app/figures/...` → 404。解读正文是模型写的，通常没有图，
   * 但产物里一旦出现就整块裂图，所以照传。
   */
  paperId?: string;
  /**
   * 解读里的引用位置（后端 `refs_resolved` + `stale_refs`，按编号索引）。
   * 没有它，正文里的 `[A023]` 也能渲染成标号，只是点了跳不动 —— 所以是可选 prop。
   */
  refs?: UnderstandingRefItem[];
}>(), {
  markdown: '', empty: false, loading: false, starting: false, running: false,
  error: '', timedOut: false, narrow: false, anchorPrefix: 'ins', paperId: '',
});

const emit = defineEmits<{
  (e: 'start'): void;
  /** 标题 anchor 已贴好（宿主据此算一次"当前小节"，否则首屏一个高亮都没有） */
  (e: 'anchors-ready'): void;
  /** 点了正文里的引用标号：宿主负责跳到原文（阅读器滚到那一段，整页则开阅读器） */
  (e: 'cite', p: { code: string; block: string; page: number; latex?: string }): void;
  /** 悬浮标号：宿主在旁边弹一张速览卡（点标号仍然走 cite，不受影响） */
  (e: 'peek', p: {
    anchor: { x: number; y: number; w: number; h: number };
    page?: number | null;
    rowId?: string | null;
    blockId?: string | null;
    label?: string | null;
  }): void;
  (e: 'peek-hide'): void;
}>();

const root = ref<HTMLElement | null>(null);

/** 材料类型 → 悬停提示里的短名（后端 chunk_type 的值域；不认识的就不写） */
const TYPE_LABEL: Record<string, string> = {
  formula: '公式', table: '表格', figure: '插图', unit: '段落', text: '段落',
};

/**
 * 旧产物的引用写法：「（来源 Chunk: A023, A031）」。
 *
 * 2026-11-28 起提示词只让模型写 「[A023][A031]」，但**库里已有的解读全是旧写法** ——
 * 解读不该因为换了提示词就继续显示一坨「（来源 Chunk: …）」原文，所以渲染这侧两种写法
 * 都归一成方括号，顺带让这个词从界面上彻底消失。
 */
const LEGACY_GROUP = /[（(]\s*来源\s*Chunk\s*[:：]\s*([A-Za-z]\d{2,4}(?:\s*[,，、;；]\s*[A-Za-z]\d{2,4})*)\s*[）)]/g;
const LEGACY_BARE = /来源\s*Chunk\s*[:：]\s*([A-Za-z]\d{2,4})/g;

function normalizeMarks(md: string): string {
  if (!/Chunk/i.test(md)) return md;
  return md
    .replace(LEGACY_GROUP, (_m, codes: string) =>
      String(codes).split(/[,，、;；]/).map((c) => '[' + c.trim().toUpperCase() + ']').join(''))
    .replace(LEGACY_BARE, (_m, code: string) => '[' + String(code).toUpperCase() + ']');
}

/** 编号 → 引用位置（宿主把 stale_refs 排在前面、refs_resolved 排在后面，有效的覆盖过期的） */
const refMap = computed(() => {
  const map = new Map<string, UnderstandingRefItem>();
  for (const item of props.refs ?? []) {
    const code = String(item?.code ?? '').toUpperCase();
    if (code) map.set(code, item);
  }
  return map;
});

function escapeAttr(text: string): string {
  return text.replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

/**
 * 正文里的「[A023]」→ 可点的**跳转标号**「[1]」。
 *
 * 为什么在渲染后的 HTML 上换、而不是给 marked 加一条行内扩展（正文里的参考文献标号
 * 就是那么做的）：序号要按**这一篇里第一次出现**的顺序编（1、2、3…），还得把编号查成
 * 页码/块 id —— 那条链路是纯函数、拿不到 refs。这里只认方括号里带字母的编号，
 * 与「[2, 4]」那套参考文献标号（数字）天然不冲突；代码块里的方括号照样跳过。
 *
 * 查不到位置的编号也照样编上号（只是点了不跳）：它们本来就是模型编出来的/
 * 已被重建索引冲掉的，直接从正文里抹掉反而会让用户以为漏了内容。
 */
function decorateCites(html: string): string {
  const parts = html.split(/(<code[\s\S]*?<\/code>|<pre[\s\S]*?<\/pre>)/i);
  const order = new Map<string, number>();
  return parts.map((part, index) => {
    if (index % 2 === 1) return part;                      // 代码块原样返回
    return part.replace(/\[([A-Z]\d{2,4})\]/g, (whole, code: string) => {
      let n = order.get(code);
      if (!n) {
        n = order.size + 1;
        order.set(code, n);
      }
      const ref = refMap.value.get(code);
      const where = ref?.page ? '第 ' + ref.page + ' 页' : '';
      const kind = TYPE_LABEL[String(ref?.chunk_type ?? '')] || '';
      const tip = [where, kind].filter(Boolean).join(' · ');
      const title = ref ? (tip ? tip + ' · 跳到原文' : '跳到原文') : '这一条引用已不在清单里';
      /* 落点优先用**正文行 id**（后端从 chunk 元数据补的），块 id 只当兜底：
         公式 chunk 的块 id 是 row-xxx 或 …-x01 这类伪块 id，阅读器翻不成行 ——
         那正是「点标号却整页闪」的原因（用户 2026-12-03）。 */
      const anchor = String(ref?.row_id || ref?.block_id || '');
      return '<button type="button" class="cite-inline ins-cite"'
        + ' title="' + escapeAttr(title) + '"'
        + ' data-code="' + code + '"'
        + ' data-block="' + escapeAttr(anchor) + '"'
        + ' data-page="' + (ref?.page ?? '') + '"'
        + ' data-latex="' + escapeAttr(String(ref?.latex ?? '')) + '">[' + n + ']</button>';
    });
  }).join('');
}

/** 点标号：只上报，不自己滚 —— 整页那侧根本没有阅读器，滚不动的活儿得宿主决定 */
function onCitePeek(ev: PointerEvent) {
  const btn = (ev.target as HTMLElement | null)?.closest?.('.ins-cite') as HTMLElement | null;
  if (!btn) return;
  const r = btn.getBoundingClientRect();
  const page = Number(btn.dataset.page || 0);
  emit('peek', {
    anchor: { x: r.left, y: r.top, w: r.width, h: r.height },
    page: page > 0 ? page : null,
    rowId: btn.dataset.block || null,
    blockId: btn.dataset.code || null,
    label: btn.getAttribute('title') || '',
  });
}

function onCitePeekOut() { emit('peek-hide'); }

function onCiteClick(ev: MouseEvent) {
  const btn = (ev.target as HTMLElement | null)?.closest?.('.ins-cite') as HTMLElement | null;
  if (!btn) return;
  const page = Number(btn.dataset.page || 0);
  emit('cite', {
    code: btn.dataset.code || '',
    block: btn.dataset.block || '',
    page: page > 0 ? page : 0,
    latex: btn.dataset.latex || '',
  });
}

/**
 * markdown → HTML。
 *
 * 表格外面那层横向滚动容器与「三线表」样式**不再由这里负责**：mdToHtml 现在统一把
 * <table> 包进 .tbl-wrap（见 useMarkdown.decorateTables），样式在 base.css 的 .md table。
 *
 * 引用标记在这之后换成一枚枚标号（decorateCites）—— 顺序不能反：先换会把 HTML 弄脏，
 * marked 再把按钮当文本转义一次就全废了。
 */
function render(md?: string | null): string {
  return decorateCites(mdToHtml(normalizeMarks(String(md ?? '')), props.paperId));
}

const html = computed(() => (String(props.markdown ?? '').trim() ? render(props.markdown) : ''));

/**
 * 给渲染出来的标题**按序贴 id**（id 由 `buildInsightOutline` 生成）。
 *
 * 为什么在 DOM 上贴而不是改 `useMarkdown` 的渲染产物：`mdToHtml` 那条链路是多年的不变量
 * （阅读器正文、Markdown 视图、图注都吃它），为一个 anchor 去动它有连带风险；
 * 而这里 DOM 顺序与大纲顺序天然一致（同一份 markdown、同一个纯函数），贴一遍就够了。
 */
function assignAnchors() {
  const box = root.value;
  if (!box) return;
  const heads = props.markdown
    ? buildInsightOutline(props.markdown, { prefix: props.anchorPrefix })
    : [];
  Array.from(box.querySelectorAll<HTMLElement>('h1, h2, h3, h4, h5, h6')).forEach((el, i) => {
    const id = heads[i]?.id;
    if (id) el.id = id;
  });
  // 贴完就通知宿主：目录那边要立刻按"阅读线"点亮一条，否则首屏一个高亮都没有（要等用户滚一下）
  emit('anchors-ready');
}
watch(html, () => { void nextTick(assignAnchors); });
onMounted(() => { void nextTick(assignAnchors); });
</script>

<template>
  <div ref="root" class="doc" :class="{ narrow }" @click="onCiteClick"
     @pointerover="onCitePeek" @pointerout="onCitePeekOut">
    <div v-if="error" class="item err">
      <div class="tiny">{{ error }}</div>
      <!-- 只在"确实卡在前置条件"时补这句：网络抖动也挂一句"先建索引"会把人引到错的方向 -->
      <div v-if="/索引|index|段落/i.test(error)" class="tiny faint">
        前置条件：这篇论文要先建过知识库索引（解读读的是索引里的段落，不是页图）。
      </div>
    </div>
    <div v-else-if="timedOut" class="hint">
      已等待 4 分钟仍没拿到结果，任务可能还在跑 —— 稍后再看。
    </div>

    <!-- 还没有解读：空状态也要能往下走（口径：这里保留「开始解读」，它不是"重新生成"） -->
    <div v-if="loading && empty" class="empty small">正在读取解读…</div>
    <div v-else-if="empty" class="item">
      <div class="tiny faint">还没有解读</div>
      <div class="tiny faint note">
        解读读的是知识库索引里的段落（不是页图），所以需要先对这篇论文建索引。
      </div>
      <button class="btn sm primary" :disabled="starting" @click="emit('start')">
        {{ starting ? '已发起…' : '开始解读' }}
      </button>
      <!-- 发起后要一次全篇调用（实测 1~2 分钟）：说清楚"已经开始了、会自动刷"，
           否则界面还是那四个字，看起来像没点动。这不是动作按钮，只是一行提示。 -->
      <div v-if="running || starting" class="tiny faint note">
        解读任务在跑（实测 1~2 分钟），完成后会自动刷新。
      </div>
    </div>

    <template v-else>
      <div v-if="!html" class="hint">还没有生成「论文解读」。</div>
      <div v-if="html" class="body md" v-html="html" />
    </template>
  </div>
</template>

<style scoped>
/* 字号跟着阅读器的 `--reader-font-size`（宿主设；取不到时 15px 与阅读器中档接近）。
   下面所有排版都用 em 相对它缩放 —— 与阅读器"标题/图注/公式一起缩放"同一口径。 */
.doc { font-size: calc(var(--reader-font-size, 15px) * var(--ts, 1)); color: var(--text); min-width: 0; }
/* 整页阅读：正文列按阅读器的文本列行宽收窄（`.pg-rows:not(.two-col)` 用的是 880px） */
.doc.narrow { max-width: 880px; margin: 0 auto; }

.item {
  border: 1px solid var(--border); border-left: 2px solid var(--border-strong);
  border-radius: var(--radius-sm); background: var(--bg-layer);
  padding: 8px 10px; margin-bottom: 8px;
}
.item.err { border-left-color: var(--danger); }
.hint { font-size: .82em; color: var(--text-faint); line-height: 1.5; margin: 0 0 8px; }
.note { margin: 4px 0 6px; line-height: 1.5; }

/* 正文排版：数值与阅读器 ReaderRow 的 `.rr-body` 一一对应（见文件头注释）。 */
.body { line-height: 1.78; overflow-wrap: anywhere; line-break: strict; text-autospace: normal; }
.body :deep(p) { margin: 0 0 .55em; }
.body :deep(h1) { font-size: 1.4em; }
.body :deep(h2) { font-size: 1.24em; }
.body :deep(h3) { font-size: 1.1em; }
.body :deep(h4), .body :deep(h5), .body :deep(h6) { font-size: 1em; }
.body :deep(h1), .body :deep(h2), .body :deep(h3), .body :deep(h4),
.body :deep(h5), .body :deep(h6) { margin: 0 0 .35em; font-weight: 600; line-height: 1.35; }
.body :deep(ul), .body :deep(ol) { margin: .3em 0; padding-left: 1.35em; }
.body :deep(.katex-display) { margin: .5em 0; overflow-x: auto; overflow-y: hidden; }
.body :deep(hr) { border: 0; height: 12px; margin: 0; background: none; }
/* 表格：与阅读器 / Markdown 视图**共用** base.css 的那套三线表（见 `.md table`），不在这里另写一套 */
</style>
