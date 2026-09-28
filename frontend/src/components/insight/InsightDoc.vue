<script setup lang="ts">
/**
 * 解读正文的**共享渲染器**：阅读器左栏两个页签（「论文解读」「阅读地图」）与两个整页
 * （`/understand/:paperId`、`/readmap/:paperId`）都用它。
 *
 * 为什么要共享：同一份 markdown 有四个出口。四处各写一遍渲染，排版一定会各自漂移
 * （行宽、段间距、标题档位、表格样式、公式溢出…）。这里只负责"把 markdown 与概念卡按
 * **阅读器的正文排版**画出来"，数据从 `useUnderstanding` 来、标题由各自的宿主渲染。
 *
 * - `section='read'`：只画 `a.markdown`（论文精读）；
 * - `section='map'`：**先画概念卡，再画 `b.markdown`**（阅读地图）—— 与 B 路自己的顺序一致
 *   （B 的 markdown 里还自带一节「相关概念」，照常渲染、不做删节：它是模型写的一篇连贯文章）。
 *
 * 排版口径（对齐阅读器 `ReaderRow` 的 `.rr-body`）：行高 1.78、段间距 .55em、标题 em 档位、
 * 数字右对齐 + 不折行的表格、公式横向可滚。字号取 `--reader-font-size`（宿主设好）。
 * ⚠️ 那些数值是**抄** ReaderRow 的：改一处要同时看另一处（跨组件拿不到它的 scoped 样式）。
 */
import { computed, nextTick, onMounted, ref, watch } from 'vue';
import { mdToHtml } from '@/composables/useMarkdown';
import AppIcon from '@/components/AppIcon.vue';
import { buildInsightOutline } from '@/utils/insightOutline';
import type { UnderstandingConcept } from '@/api/types';

const props = withDefaults(defineProps<{
  section: 'read' | 'map';
  markdown?: string | null;
  concepts?: UnderstandingConcept[];
  /** 整篇都没有解读（与"这一路缺内容"不同）：显示空状态 + 「开始解读」 */
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
   * 新版的整页是自己给右栏定宽（`InsightPage` 的列宽 + 页边距），所以那里不再传它；
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
}>(), {
  markdown: '', concepts: () => [], empty: false, loading: false, starting: false, running: false,
  error: '', timedOut: false, narrow: false, anchorPrefix: 'ins', paperId: '',
});

const emit = defineEmits<{
  (e: 'start'): void;
  /** 标题 anchor 已贴好（宿主据此算一次"当前小节"，否则首屏一个高亮都没有） */
  (e: 'anchors-ready'): void;
}>();

const root = ref<HTMLElement | null>(null);

/**
 * markdown → HTML。
 *
 * 表格外面那层横向滚动容器与「三线表」样式**不再由这里负责**：`mdToHtml` 现在统一把
 * `<table>` 包进 `.tbl-wrap`（见 `useMarkdown.decorateTables`），样式在 base.css 的 `.md table`。
 * 以前这里自己 `replace` 一遍，于是 AI 解读、阅读器、Markdown 视图三处表格长得都不一样。
 */
function render(md?: string | null): string {
  return mdToHtml(md ?? '', props.paperId);
}

const html = computed(() => (String(props.markdown ?? '').trim() ? render(props.markdown) : ''));

/** 「相关概念」那一块的 anchor（目录里对应的人工条目） */
const conceptsId = computed(() => `${props.anchorPrefix}-concepts`);

/**
 * 给渲染出来的标题**按序贴 id**（id 由 `buildInsightOutline` 生成）。
 *
 * 为什么在 DOM 上贴而不是改 `useMarkdown` 的渲染产物：`mdToHtml` 那条链路是多年的不变量
 * （阅读器正文、Markdown 视图、图注、概念解释都吃它），为一个 anchor 去动它有连带风险；
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

/* ------------------------------ 相关概念 ------------------------------ */
const filter = ref('');

function matchFilter(c: UnderstandingConcept): boolean {
  const q = filter.value.trim().toLowerCase();
  if (!q) return true;
  return [c.name, c.name_en, c.body_md].some((s) => String(s ?? '').toLowerCase().includes(q));
}
const conceptRows = computed(() => (props.concepts ?? [])
  .filter(matchFilter)
  .map((c) => ({ key: String(c.name || c.name_en || ''), c }))
  .filter((r) => r.key));

/** 概念卡默认**收起**：一屏摊开十几段解释就是一堵墙，先让名字扫一遍 */
const expanded = ref<Record<string, boolean>>({});
const isOpen = (key: string) => Boolean(expanded.value[key]);
function toggle(key: string) { expanded.value = { ...expanded.value, [key]: !expanded.value[key] }; }

/** 引用短码的 tooltip：短码之外把 chunk id / 类型 / 页码摊开（核对来源时要用） */
function refTitle(c: UnderstandingConcept, code: string): string {
  const r = (c.refs ?? []).find((x) => x.code === code);
  if (!r) return code;
  return [r.code, r.chunk_id, r.chunk_type, r.page ? `p${r.page}` : ''].filter(Boolean).join(' · ');
}

/** 这一路没内容时的淡色说明（不报错、不留白） */
const missingHint = computed(() => (props.section === 'map'
  ? '还没有生成「阅读地图」，先看「论文解读」也一样。'
  : '还没有生成「论文解读」（模型这次可能只给出了阅读地图）。'));
</script>

<template>
  <div ref="root" class="doc" :class="{ narrow }">
    <div v-if="error" class="item err">
      <div class="tiny">{{ error }}</div>
      <!-- 只在"确实卡在前置条件"时补这句：网络抖动也挂一句"先建索引"会把人引到错的方向 -->
      <div v-if="/chunk|索引|index/i.test(error)" class="tiny faint">
        前置条件：这篇论文要先建过知识库索引（解读读的是 chunk，不是页图）。
      </div>
    </div>
    <div v-else-if="timedOut" class="hint">
      已等待 4 分钟仍没拿到完整结果，任务可能还在跑 —— 稍后再看。
    </div>

    <!-- 还没有解读：空状态也要能往下走（口径：这里保留「开始解读」，它不是"重新生成"） -->
    <div v-if="loading && empty" class="empty small">正在读取解读…</div>
    <div v-else-if="empty" class="item">
      <div class="tiny faint">还没有解读</div>
      <div class="tiny faint note">
        解读读的是知识库的 chunk（不是页图），所以需要先对这篇论文建索引。
      </div>
      <button class="btn sm primary" :disabled="starting" @click="emit('start')">
        {{ starting ? '已发起…' : '开始解读' }}
      </button>
      <!-- 发起后要一次全篇调用（实测 1~2 分钟）：说清楚"已经开始了、会自动刷"，
           否则界面还是那四个字，看起来像没点动。这不是动作按钮，只是一行提示。 -->
      <div v-if="running || starting" class="tiny faint note">
        解读任务在跑（两路并发，实测 1~2 分钟），完成后会自动刷新。
      </div>
    </div>

    <template v-else>
      <div v-if="!html && !(section === 'map' && concepts.length)" class="hint">{{ missingHint }}</div>

      <!-- 阅读地图：相关概念在**最前面**（与 B 路 markdown 自身的顺序一致）。
           `id` 就是目录里那条「相关概念」的落点（前缀由宿主给，与正文标题同源）。 -->
      <section v-if="section === 'map' && concepts.length" :id="conceptsId" class="concepts">
        <div class="concepts-head">
          <span class="muted">相关概念</span>
          <span class="badge dim">{{ conceptRows.length }}<template v-if="filter">/{{ concepts.length }}</template></span>
          <span class="spacer" />
          <input
            v-model="filter"
            class="concept-filter"
            placeholder="过滤概念"
            title="按中英文名或解释内容过滤"
          >
        </div>
        <div v-for="r in conceptRows" :key="r.key" class="item concept">
          <button class="concept-head" @click="toggle(r.key)">
            <span class="concept-name truncate" :title="r.c.name">{{ r.c.name }}</span>
            <span v-if="r.c.name_en" class="faint truncate concept-en" :title="r.c.name_en">
              {{ r.c.name_en }}
            </span>
            <span class="spacer" />
            <AppIcon :name="isOpen(r.key) ? 'chevron-up' : 'chevron-down'" :size="13" />
          </button>
          <div v-if="isOpen(r.key)" class="concept-body">
            <div v-if="r.c.body_md" class="body md" v-html="render(r.c.body_md)" />
            <!-- 来源短码：**纯文本**（可选中、可复制），不是点一下跳页的胶囊 -->
            <div v-if="r.c.refs?.length" class="refs">
              <span
                v-for="ref in r.c.refs"
                :key="ref.code + ref.chunk_id"
                class="ref-text"
                :title="refTitle(r.c, ref.code)"
              >来源 Chunk: {{ ref.code }}</span>
            </div>
          </div>
        </div>
        <div v-if="!conceptRows.length" class="empty small">没有匹配「{{ filter }}」的概念</div>
      </section>

      <div v-if="html" class="body md" v-html="html" />
    </template>
  </div>
</template>

<style scoped>
/* 字号跟着阅读器的 `--reader-font-size`（宿主设；取不到时 15px 与阅读器中档接近）。
   下面所有排版都用 em 相对它缩放 —— 与阅读器"标题/图注/公式一起缩放"同一口径。 */
.doc { font-size: var(--reader-font-size, 15px); color: var(--text); min-width: 0; }
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
.body { line-height: 1.78; overflow-wrap: anywhere; }
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

/* 相关概念：小标题行 + 卡片（卡片观感与左栏其它页签一致） */
.concepts { margin-bottom: 14px; }
.concepts-head { display: flex; align-items: center; gap: 6px; margin: 0 0 6px; }
.concept-filter {
  min-width: 0; width: 130px; height: 22px; padding: 0 6px; font-size: .78em;
  border: 1px solid var(--border); border-radius: var(--radius-sm);
  background: var(--bg); color: var(--text);
}
.concept { padding: 6px 8px; margin-bottom: 6px; }
.concept-head {
  display: flex; align-items: center; gap: 6px; width: 100%;
  border: 0; background: transparent; color: var(--text); font: inherit;
  padding: 0; cursor: pointer; text-align: left;
}
.concept-name { font-size: .88em; font-weight: 600; }
.concept-en { font-size: .78em; max-width: 45%; }
.concept-body { margin-top: 6px; }
.refs { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 6px; }
/* 来源短码：只是**文字**（等宽、淡色，可选中复制）—— 任何"像按钮"的视觉都在暗示它能点，
   而它已经不能跳页了（用户口径：引用保留纯文本）。 */
.ref-text { font-size: .75em; font-family: var(--mono); color: var(--text-dim); user-select: text; cursor: text; }
</style>
