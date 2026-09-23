<script setup lang="ts">
/**
 * 正文区（**单一滚动容器**）：按页分组的逐行对照。
 *
 * 对齐策略：每一行是页级翻译同一次调用产出的**原文/译文对**，因此在同一个网格行里天然对齐；
 * 只有**一个**滚动容器 —— 不存在"滚着滚着就错位"，也不需要联动脚本。
 *
 * 插图（解析阶段切出来的 figure/table 图）**跟随原文**：挂在该页图注行之后的**原文侧**
 * 单元格里，与页内顺序一致；页图模式（原 PDF）下不再重复显示缩略图（页图里本来就有）。
 *
 * 字号：由 `fontSize`（px）统一驱动，行内标题/图注/公式全部用 em 相对缩放。
 */
import { computed } from 'vue';
import { assetUrl } from '@/api/client';
import type { Highlight, OriginalMode, PageInfo, PageRow, PairOrder, ViewMode } from '@/api/types';
import type { PageGroup } from '@/composables/usePageRows';
import { pageStats } from '@/composables/usePageRows';
import AppIcon from '@/components/AppIcon.vue';
import PdfPage from './PdfPage.vue';
import ReaderRow from './ReaderRow.vue';

type Media = NonNullable<PageInfo['media']>[number];

const props = defineProps<{
  groups: PageGroup[];
  mode: ViewMode;
  originalMode: OriginalMode;
  pairOrder: PairOrder;
  paperId: string;
  pageWidth: number;
  currentRowId: string;
  highlights: Highlight[];
  showRefs: boolean;
  /** 后台正在翻译这篇论文：未译的行显示「等待翻译」而不是「未翻译」 */
  translating: boolean;
  /** 正文字号（px）；标题/图注用 em 跟随缩放 */
  fontSize: number;
}>();

const emit = defineEmits<{
  (e: 'focus', rowId: string): void;
  (e: 'copy', payload: { row: PageRow; side: 'en' | 'zh' }): void;
  (e: 'translate-page', page: number): void;
  (e: 'zoom', url: string): void;
  (e: 'update:showRefs', value: boolean): void;
}>();

/** 原文在左？ */
const enLeft = computed(() => props.pairOrder !== 'zh_left' && props.pairOrder !== 'zh_top');
/** 对照里左栏放页图（而不是原文文本） */
const pageLeft = computed(() => props.mode === 'bilingual' && props.originalMode === 'page');
const textCompare = computed(() => props.mode === 'bilingual' && props.originalMode === 'text');
const imageOnly = computed(() => props.mode === 'original' && props.originalMode === 'page');
/** 页图模式下文字列**永远放译文**：另一列就是原 PDF 页图（原始内容），
 *  之前跟着 `pairOrder` 走会导致"译文在左"时文字列也放原文 —— 原文出现两次、译文消失。 */
const pageRowSide = 'zh' as const;
/** 正文里是否显示插图（只有"纯原 PDF"单栏不显示：那一种整页就是页图，没有行可挂） */
const showFigures = computed(() => !imageOnly.value);

/** 当前视图下每一行要渲染几侧、按什么顺序 */
const sides = computed<Array<'en' | 'zh'>>(() => {
  if (props.mode === 'translation') return ['zh'];
  if (props.mode === 'original') return ['en'];
  return enLeft.value ? ['en', 'zh'] : ['zh', 'en'];
});

const pageImage = (page: number) => assetUrl(`/api/v1/papers/${props.paperId}/pages/${page}`);
/**
 * 左侧“原 PDF”用**真实 PDF**（pdf.js）还是后端页图：
 *   - `has_text_layer = true`（正常电子版）：走 pdf.js → 矢量清晰、文字可选中复制；
 *   - 扫描页：PDF 里就是一整页位图，走页图更省内存（且本来也没字可选）。
 * 解析前的论文拿不到页信息（`info` 为 null）→ 继续用页图。
 */
const useRealPdf = (g: PageGroup) => g.info?.has_text_layer === true;
const rowClass = (id: string) => ({ current: id === props.currentRowId });
const isBadAlign = (g: PageGroup) => g.align === 'structure';
/**
 * 宽对象（表格）：半栏放不下（9 列表格会把 "45.29" 折成两行），
 * 因此**跨两列通栏**、两侧上下排（EN 表在上、ZH 表在下），与论文里通栏表格一致。
 */
const isWide = (r: PageRow) => r.type === 'table';

/* ------------------------------ 插图落位 ------------------------------ */
const FIGURE_NO = /(?:figure|fig\.?|table|图|表)\s*(\d{1,3})/i;

function figureNo(text?: string | null): string {
  const m = FIGURE_NO.exec(String(text ?? ''));
  return m ? m[1] : '';
}

/**
 * 把该页的插图挂到"图注行"上：优先按图号匹配（Figure 3 ↔ 图注里的 3），
 * 匹配不上就按顺序挂到还没被占用的图注行；完全没有图注行时留到页尾。
 * 渲染时插图放在图注**上方**（论文里图在上、图注在下）。
 */
const mediaPlan = computed(() => {
  const byRow = new Map<string, Media[]>();
  const tail = new Map<number, Media[]>();
  if (!showFigures.value) return { byRow, tail };
  for (const g of props.groups) {
    const media = g.info?.media ?? [];
    if (!media.length) continue;
    const captions = g.rows.filter((r) => r.type === 'caption');
    const used = new Set<string>();
    const leftovers: Media[] = [];
    for (const m of media) {
      // 后端已给出图号（按图注锚定的裁图），拿不到时退回从图注文本里抽
      const want = m.number || figureNo(m.caption);
      let row = want
        ? captions.find((r) => !used.has(r.id) && figureNo(r.original) === want)
        : undefined;
      if (!row) row = captions.find((r) => !used.has(r.id));
      if (row) {
        used.add(row.id);
        byRow.set(row.id, [...(byRow.get(row.id) ?? []), m]);
      } else {
        leftovers.push(m);
      }
    }
    if (leftovers.length) tail.set(g.page, leftovers);
  }
  return { byRow, tail };
});

const mediaFor = (rowId: string): Media[] => mediaPlan.value.byRow.get(rowId) ?? [];
const mediaTail = (page: number): Media[] => mediaPlan.value.tail.get(page) ?? [];
</script>

<template>
  <div class="rows-wrap" :style="{ '--reader-font-size': `${props.fontSize}px` }">
    <div v-if="!props.groups.length" class="empty">没有可显示的内容（先解析或翻译这篇论文）</div>

    <section v-for="g in props.groups" :key="g.page" :data-page="g.page" class="pg">
      <header class="pg-head">
        <span class="pg-no">第 {{ g.page }} 页</span>
        <span v-if="g.translated" class="badge">{{ pageStats(g).done }}/{{ pageStats(g).total }} 行</span>
        <span
          v-if="isBadAlign(g)"
          class="badge warn"
          title="模型给出的原文与译文块数不一致，已按结构对齐"
        >结构对齐</span>
        <span class="spacer" />
        <button
          v-if="!g.translated"
          class="btn sm ghost"
          @click="emit('translate-page', g.page)"
        >
          <AppIcon name="translate" :size="14" />翻译本页
        </button>
      </header>

      <!-- ① 原文 + 原 PDF：整页（通栏，单栏阅读） -->
      <div v-if="imageOnly" class="pg-image-only">
        <div class="media-box" :style="{ width: `${props.pageWidth}px` }">
          <PdfPage
            v-if="useRealPdf(g)"
            :paper-id="props.paperId"
            :page="g.page"
            :width="props.pageWidth"
            :page-width="g.info?.width"
            :page-height="g.info?.height"
          />
          <img v-else :src="pageImage(g.page)" :alt="`第 ${g.page} 页`" loading="lazy">
        </div>
      </div>

      <!-- ② 对照 + 原 PDF：页图（页内 sticky）在左，译文行在右 -->
      <div
        v-else-if="pageLeft"
        class="pg-pair"
        :class="enLeft ? 'image-left' : 'image-right'"
      >
        <div class="col-image">
          <div class="media-box" :style="{ width: `${props.pageWidth}px` }">
            <PdfPage
              v-if="useRealPdf(g)"
              :paper-id="props.paperId"
              :page="g.page"
              :width="props.pageWidth"
              :page-width="g.info?.width"
              :page-height="g.info?.height"
            />
            <img v-else :src="pageImage(g.page)" :alt="`第 ${g.page} 页`" loading="lazy">
          </div>
        </div>
        <div class="col-rows">
          <div
            v-for="r in g.rows"
            :key="r.id"
            :data-row-id="r.id"
            class="row-pair"
            :class="rowClass(r.id)"
          >
            <!-- 左栏是页图时，插图仍要出现在**译文列**的图注上方（页图缩放后往往看不清） -->
            <figure
              v-for="(m, i) in mediaFor(r.id)"
              :key="`m-${i}`"
              class="row-figure"
              :title="`${m.caption || '插图'}（点击看大图）`"
              @click="emit('zoom', assetUrl(m.url))"
            >
              <img :src="assetUrl(m.url)" :alt="m.caption || '插图'" loading="lazy">
            </figure>
            <ReaderRow
              :row="r"
              :side="pageRowSide"
              :highlights="props.highlights"
              :missing="pageRowSide === 'zh' && !r.translation"
              :pending="props.translating"
              @copy="emit('copy', $event)"
            />
          </div>
          <div v-if="!g.rows.length" class="empty small">这一页还没有内容</div>
        </div>
      </div>

      <!-- ③ 原文文本 / 译文 / 文本对照：行网格（左右两侧同字号同行距） -->
      <div v-else class="pg-rows" :class="{ 'two-col': textCompare }">
        <div
          v-for="r in g.rows"
          :key="r.id"
          :data-row-id="r.id"
          class="row-pair"
          :class="{ 'two-col': textCompare, wide: isWide(r), ...rowClass(r.id) }"
        >
          <!-- 插图：按论文的排法——**图在上、图注在下**，通栏居中（对照时跨两列） -->
          <figure
            v-for="(m, i) in mediaFor(r.id)"
            :key="`m-${i}`"
            class="row-figure"
            :title="`${m.caption || '插图'}（点击看大图）`"
            @click="emit('zoom', assetUrl(m.url))"
          >
            <img :src="assetUrl(m.url)" :alt="m.caption || '插图'" loading="lazy">
          </figure>

          <div
            v-for="s in sides"
            :key="s"
            class="cell"
            :class="`cell-${s}`"
          >
            <ReaderRow
              :row="r"
              :side="s"
              :highlights="props.highlights"
              :missing="s === 'zh' && !r.translation"
              :pending="props.translating"
              @copy="emit('copy', $event)"
            />
          </div>
        </div>

        <!-- 没有图注行可挂的插图：放在该页末尾（同样通栏） -->
        <figure
          v-for="(m, i) in mediaTail(g.page)"
          :key="`tail-${i}`"
          class="row-figure tail"
          :title="`${m.caption || '插图'}（点击看大图）`"
          @click="emit('zoom', assetUrl(m.url))"
        >
          <img :src="assetUrl(m.url)" :alt="m.caption || '插图'" loading="lazy">
        </figure>

        <div v-if="!g.rows.length" class="empty small">这一页还没有内容</div>
      </div>
    </section>

    <div class="rows-foot">
      <label class="row tiny faint" style="gap:5px">
        <input
          type="checkbox"
          :checked="props.showRefs"
          @change="emit('update:showRefs', ($event.target as HTMLInputElement).checked)"
        >
        显示参考文献
      </label>
    </div>
  </div>
</template>

<style scoped>
.rows-wrap {
  padding: 10px 16px 72px; max-width: 1400px; margin: 0 auto;
  /* 正文字号的唯一来源：标题/图注/公式都用 em 相对它缩放 */
  font-size: var(--reader-font-size, 13.5px);
}

.pg { margin-bottom: 18px; }
/* 页头**不吸顶**。
 * 吸顶（position: sticky）会在正文顶部压出一条只放了页码的空带，并把紧跟着的那行
 * 切掉上半截 —— 看起来像一条"莫名的遮罩"。页码是页与页之间的分隔，跟着内容滚动即可。 */
.pg-head {
  display: flex; align-items: center; gap: 6px;
  padding: 6px 2px; border-bottom: 1px solid var(--divider);
  font-size: 11.5px; color: var(--text-faint);
}
.pg-no { font-weight: 600; color: var(--text-dim); font-variant-numeric: tabular-nums; }

/* 行容器 = 纯排版块，**不带任何装饰**：
   - 不画 border-top 分隔线（之前每块一条横线，密而卙）
   - 不加 border-radius（只画上边框时圆角会把两端掰弯，看着像凭空多一个"圆角框"）
   - 不留垂直内边距（否则相邻块之间会多出 12px 空白，不像一段连续的 markdown）
   - **不做 hover 底色**：鼠标停在哪一块，那一块就变成一块灰色圆角块，
     在"原 PDF 在左"模式下只有右栏有底色，看起来就像凭空多出一个变体（用户报过）。
   正文的悬浮反馈交给行右侧的复制按钮（.rr-copy），文字本身保持干净。
   "当前行"仍由 ReaderView 的滚动跟踪维护（目录高亮 / j·k 跳转 / 进度保存），只是不画出来。 */
.row-pair { position: relative; padding: 0 4px; }

/* 文本对照：一行两列；两侧只有字体不同，字号/行距完全一致 */
.pg-rows.two-col .row-pair.two-col {
  display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 0 24px;
}
.row-pair.two-col > .cell + .cell {
  border-left: 1px solid var(--divider); padding-left: 20px;
}
.cell { min-width: 0; }
/* 通栏行（表格）：两侧改为上下排，各自占满正文宽度。
   上下之间**不画虚线**：每块一条横线太碎，看起来像被切成一条条卡片；靠间距分层就够了。 */
.pg-rows.two-col .row-pair.wide { display: block; }
.row-pair.wide > .cell + .cell {
  border-left: 0; padding-left: 0; margin-top: 10px; padding-top: 0;
}
.pg-rows:not(.two-col) { max-width: 880px; margin: 0 auto; }

/* 插图：按论文的排法——图在上、图注在下；对照时跨两列，页图模式则占满译文列。
   点击打开大图（浏览器里"图能看清"和 PDF 一样重要）。 */
.row-figure {
  margin: 10px auto 4px; cursor: zoom-in; max-width: 100%;
  display: flex; justify-content: center;
}
.row-pair.two-col > .row-figure { grid-column: 1 / -1; }
.col-rows > .row-pair > .row-figure { max-width: 100%; }
.row-figure img {
  display: block; max-width: min(100%, 780px); height: auto; background: #fff;
  border: 1px solid var(--border); border-radius: var(--radius-sm);
}
.row-figure:hover img { border-color: var(--accent); }
.row-figure.tail { margin: 10px auto 8px; }

/* 对照 + 页图：页图贴在该页译文左侧（页内 sticky，滚译文时页图不跑） */
.pg-pair { display: grid; gap: 0 20px; }
.pg-pair.image-left,
.pg-pair.image-right { grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); }
.pg-pair.image-right .col-image { order: 2; }
.pg-pair .col-image { min-width: 0; }
/* sticky 的参照物是**滚动容器**（`.reader-content`，它的上边就是工具栏下沿），
   所以 `top` 不需要给工具栏留位：原来写 34px → 页图顶部白空多出 34px（用户报过）。
   这里贴顶 2px，只留一丝缝。 */
.pg-pair .col-image .media-box { position: sticky; top: 2px; }
.pg-pair .col-rows { min-width: 0; border-left: 1px solid var(--divider); padding-left: 20px; }

.pg-image-only { display: flex; flex-direction: column; align-items: center; gap: 10px; padding: 8px 0 4px; }
.media-box { max-width: 100%; margin: 0 auto; background: #fff; border: 1px solid var(--border); border-radius: var(--radius-sm); overflow: hidden; }
.media-box img { display: block; width: 100%; height: auto; }
/* 点一下开大图（不然半栏的 A4 页上正文只有几像素高，根本看不清） */
.media-box.zoomable { cursor: zoom-in; }
.media-box.zoomable:hover { border-color: var(--accent); }

.rows-foot { display: flex; justify-content: center; padding: 14px 0 4px; }

@media (max-width: 1100px) {
  .rows-wrap { padding: 8px 10px 64px; }
  .pg-rows.two-col .row-pair.two-col,
  .pg-pair.image-left,
  .pg-pair.image-right { grid-template-columns: minmax(0, 1fr); }
  .pg-pair.image-right .col-image { order: 0; }
  .row-pair.two-col > .cell + .cell {
    border-left: 0; border-top: 0;
    padding-left: 0; margin-top: 8px; padding-top: 0;
  }
  .pg-pair .col-rows { border-left: 0; padding-left: 0; }
  .pg-pair .col-image .media-box { position: static; }
}
</style>
