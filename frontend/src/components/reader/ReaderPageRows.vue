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
import type { Highlight, OriginalMode, PageInfo, PageRow, PairOrder, ViewMode, ZoomTarget } from '@/api/types';
import type { PageGroup } from '@/composables/usePageRows';
import AppIcon from '@/components/AppIcon.vue';
import PdfPage from './PdfPage.vue';
import ReaderRow from './ReaderRow.vue';

type Media = NonNullable<PageInfo['media']>[number];

const props = defineProps<{
  groups: PageGroup[];
  mode: ViewMode;
  originalMode: OriginalMode;
  /** 页图模式（对照 + 原 PDF）下文字列放哪一侧：`zh` 译文（默认）/ `en` 原文 */
  pageSide: 'en' | 'zh';
  pairOrder: PairOrder;
  paperId: string;
  pageWidth: number;
  currentRowId: string;
  highlights: Highlight[];
  /** 搜索词：命中处全部高亮（见 `utils/searchMarks.ts`） */
  searchTerm: string;
  /** 需要"闪一下"提醒的行 id（点搜索命中 / 目录跳转后短暂提示"就是这一块"） */
  flashRowId: string;
  showRefs: boolean;
  /** 后台正在翻译这篇论文：未译的行显示「等待翻译」而不是「未翻译」 */
  translating: boolean;
  /** 正文字号（px）；标题/图注用 em 跟随缩放 */
  fontSize: number;
}>();

const emit = defineEmits<{
  (e: 'focus', rowId: string): void;
  (e: 'translate-page', page: number): void;
  (e: 'zoom', target: ZoomTarget): void;
  (e: 'update:showRefs', value: boolean): void;
}>();

/** 原文在左？ */
const enLeft = computed(() => props.pairOrder !== 'zh_left' && props.pairOrder !== 'zh_top');
/** 对照里左栏放页图（而不是原文文本） */
const pageLeft = computed(() => props.mode === 'bilingual' && props.originalMode === 'page');
const textCompare = computed(() => props.mode === 'bilingual' && props.originalMode === 'text');
const imageOnly = computed(() => props.mode === 'original' && props.originalMode === 'page');
/** 页图模式下**文字列放哪一侧**：默认译文（另一列就是原 PDF 页图，等于"原 PDF 对照译文"），
 *  可以切成原文（"原 PDF 对照原文"：想照页图核对原文/复制时用）。
 *  ⚠️ 它由用户显式选择（`pageSide`），**不要**再跟着 `pairOrder` 走 ——
 *  以前那样做在"译文在左"时文字列也会放原文，结果是原文出现两次、译文消失。 */
const pageRowSide = computed<'en' | 'zh'>(() => props.pageSide);
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
const rowClass = (id: string) => ({ current: id === props.currentRowId, flash: id === props.flashRowId });
/* 页头原来的「结构对齐」徽章**已按用户要求删除**：它只是在说"模型给的块数与解析器不一致、
 * 走了结构兜底对齐"，人工复核内容并没有问题，白示一个告警只会让人以为译错了。
 * `align` 字段仍在产物与接口里（供自检脚本审计），前端不再展示。 */
/**
 * 宽对象（表格）：**文本对照时两端各占一列、左右并排**（用户口径第二轮返工）。
 *
 * 返工记录：以前这里把 `.wide` 行从两列网格改成 `display: block`（通栏、EN 表在上 ZH 表在下），
 * 理由是"半栏放不下 9 列表格"；但用户要的是**左右对照**——上下堆叠时对照关系就断了。
 * 现在改成：继续用两列网格（`.pg-rows.two-col .row-pair.two-col` 那套），
 * 表格会变窄 → **每个单元格自己横向滚动**（见下面 `.cell` 的 `overflow-x`），
 * 既不撑破布局、也不会让整页出现横向滚动条。
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
/**
 * 正文里的插图与"点开看大图"用**同一张**：后端的「图 + 图注」卡片（`?card=1`）。
 *
 * 口径返工记录：一开始正文用纯图（`region/N/0`）、放大用卡片，两张图上下范围不一致
 * （卡片多了图注与外边距）→ 用户反馈"点击查看和在论文内的范围不一致"；
 * 我先把放大改成纯图，用户又指出**纯图那张才是裁得不好的那张**（贴边、少了图注）——
 * 所以最终统一到卡片：正文里看到什么，点开就是它的放大版（范围完全一致）。
 * 实测 5 张图的纯图与卡片图体墨迹范围逐行一致（差 0~34 行 = 重采样误差），
 * 卡片只是多了图注与外边距，不会丢内容。
 */
const zoomUrl = (m: Media) => assetUrl(m.card_url || m.url);
const figureSrc = (m: Media) => assetUrl(m.card_url || m.url);
/** 放大目标：URL + **图注**（弹窗标题显示图注，见 `ZoomTarget`）；页码取所在页组 */
const zoomTarget = (m: Media, page: number): ZoomTarget => ({
  url: zoomUrl(m), caption: m.caption ?? '', page, number: m.number ?? null,
});
</script>

<template>
  <div class="rows-wrap" :style="{ '--reader-font-size': `${props.fontSize}px` }">
    <div v-if="!props.groups.length" class="empty">没有可显示的内容（先解析或翻译这篇论文）</div>

    <section v-for="g in props.groups" :key="g.page" :data-page="g.page" class="pg">
      <header class="pg-head">
        <span class="pg-no">第 {{ g.page }} 页</span>
        <!-- 「x/y 行」徽章**已按用户要求删除**：原文行数与译文行数本来就不必相同
             （跨页承接句、列表合并都会差一行），写出来只会让人以为少了内容。 -->
        <span class="spacer" />        <button
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
              @click="emit('zoom', zoomTarget(m, g.page))"
            >
              <img :src="figureSrc(m)" :alt="m.caption || '插图'" loading="lazy">
            </figure>
            <ReaderRow
              :row="r"
              :side="pageRowSide"
              :paper-id="props.paperId"
              :highlights="props.highlights"
              :search-term="props.searchTerm"
              :missing="pageRowSide === 'zh' && !r.translation && !g.translated"
              :pending="props.translating"
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
            @click="emit('zoom', zoomTarget(m, g.page))"
          >
            <img :src="figureSrc(m)" :alt="m.caption || '插图'" loading="lazy">
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
              :paper-id="props.paperId"
              :highlights="props.highlights"
              :search-term="props.searchTerm"
              :missing="s === 'zh' && !r.translation && !g.translated"
              :pending="props.translating"
            />
          </div>
        </div>

        <!-- 没有图注行可挂的插图：放在该页末尾（同样通栏） -->
        <figure
          v-for="(m, i) in mediaTail(g.page)"
          :key="`tail-${i}`"
          class="row-figure tail"
          :title="`${m.caption || '插图'}（点击看大图）`"
          @click="emit('zoom', zoomTarget(m, g.page))"
        >
          <img :src="figureSrc(m)" :alt="m.caption || '插图'" loading="lazy">
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

.pg { margin-bottom: 26px; }
/* 页头**不吸顶**。
 * 吸顶（position: sticky）会在正文顶部压出一条只放了页码的空带，并把紧跟着的那行
 * 切掉上半截 —— 看起来像一条"莫名的遮罩"。页码是页与页之间的分隔，跟着内容滚动即可。
 * **不画下划线**（用户口径）：页码只是一枚很淡的标注，横线纯属噪声。 */
.pg-head {
  display: flex; align-items: center; gap: 6px;
  padding: 6px 2px 0;
  font-size: 11.5px; color: var(--text-faint);
}
.pg-no { font-weight: 600; color: var(--text-dim); font-variant-numeric: tabular-nums; }

/* 正文内容**顶部留一点白**（用户口径）：左边是论文页图，页图里自带纸面页边距，
 * 右边如果贴着页头就开始，两栏顶部一高一低看着突兀；给**右侧 markdown** 下移一点点就顺眼了。
 * 只给内容列（不包含页图那一列）：`.pg-rows` 是文本对照，`.col-rows` 是"原 PDF 在左"时的右侧。
 * `:where()` 保持 0 特异性，别干扰下面 `.pg-rows.two-col` 那些布局规则。 */
:where(.pg-rows, .col-rows) { padding-top: 18px; }

/* 行容器 = 纯排版块，**不带任何装饰**：
   - 不画 border-top 分隔线（之前每块一条横线，密而卙）
   - 不加 border-radius（只画上边框时圆角会把两端掰弯，看着像凭空多一个"圆角框"）
   - 不留垂直内边距（否则相邻块之间会多出 12px 空白，不像一段连续的 markdown）
   - **不做 hover 底色**：鼠标停在哪一块，那一块就变成一块灰色圆角块，
     在"原 PDF 在左"模式下只有右栏有底色，看起来就像凭空多出一个变体（用户报过）。
   正文不做任何悬浮装饰（行内复制按钮已按用户要求删除），文字本身保持干净。
   "当前行"仍由 ReaderView 的滚动跟踪维护（目录高亮 / j·k 跳转 / 进度保存），只是不画出来。 */
.row-pair { position: relative; padding: 0 4px; }

/* 「就是这一块」：点搜索命中 / 目录跳过来之后，整行短暂亮一下再淡出。
 * 用 ::before 覆盖层而不是给行本身上底色 —— 行是纯排版块，改它的 background 会连图片、
 * 表格一起变色；覆盖层只负责"闪"，且 `pointer-events:none` 不挡点击。
 * 底色默认就是**可见**的（不是靠动画从 0 淡入）：万一动画没跑（无头浏览器冻结 CSS 动画、
 * 或者被系统设置关掉），这层照样亮着，1.6s 后由 JS 摘掉类 → 效果依旧成立。 */
.row-pair.flash::before {
  content: ''; position: absolute; inset: -3px -6px; pointer-events: none;
  border-radius: 8px; opacity: .95;
  background: color-mix(in srgb, var(--accent) 20%, transparent);
  animation: row-flash 1.5s ease-out forwards;
}
@keyframes row-flash {
  0% { opacity: .95; }
  60% { opacity: .6; }
  100% { opacity: 0; }
}
@media (prefers-reduced-motion: reduce) {
  /* 尊重系统设置：不做淡出动画，亮着等 JS 摘类即可 */
  .row-pair.flash::before { animation: none; opacity: .5; }
}

/* 文本对照：一行两列；两侧只有字体不同，字号/行距完全一致 */
.pg-rows.two-col .row-pair.two-col {
  display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 0 24px;
}
.row-pair.two-col > .cell + .cell {
  border-left: 1px solid var(--divider); padding-left: 20px;
}
.cell { min-width: 0; }
/* 通栏行（表格）**只在"不是文本对照"时才上下堆叠**（那时本来就只有一侧）。
   文本对照下让 `.wide` 行继续走上面的两列网格：两端各自占一列、左右并排 —— 对照关系不能断。 */
.pg-rows:not(.two-col) .row-pair.wide > .cell + .cell {
  border-left: 0; padding-left: 0; margin-top: 10px; padding-top: 0;
}
/* 表格进了半栏必然比列宽，让**单元格自己横向滚**：撑不破布局、也不会给整页拉出横向滚动条
   （`scripts/frontend_check.py` ③ 的"整页不滚动"是永久回归项）。
   ⚠️ **不要**再给里面的 `table` 加 `max-width: 100%`：表格在 auto 布局下会因此把盒子压回列宽、
   而内容继续往外冒（表格边框半路就断），滚动量也变得算不准；让表格自己超宽、由单元格滚才完整。 */
.pg-rows.two-col .row-pair.wide > .cell { overflow-x: auto; }
.pg-rows:not(.two-col) { max-width: 880px; margin: 0 auto; }

/* 插图：按论文的排法——图在上、图注在下；对照时跨两列，页图模式则占满译文列。
   点击打开大图（浏览器里"图能看清"和 PDF 一样重要）。
   ⚠️ `.row-figure` 自己必须**跨满两列**：否则它只是两列里的一个 shrink-to-fit 格子，
   里面的 `max-width: 100%` 会去参考一个"由内容决定的"宽度（循环依赖），实测同一张图
   一会 780px 一会 483px。 */
.row-figure {
  margin: 10px auto 4px; cursor: zoom-in; max-width: 100%; width: 100%;
  display: flex; justify-content: center;
}
.row-pair.two-col > .row-figure { grid-column: 1 / -1; }
.col-rows > .row-pair > .row-figure { max-width: 100%; }
/* ⚠️ 两条尺寸约束都是**用户口径**：
   1. `padding` + 白底：后端的裁图是按墨迹裁到边的（没有留白），贴着边看就像"上下被裁掉了"，
      而点开的大图是"图+图注"卡片（合成时四周留了 12px 白边）—— 两者观感不一致。这里补一圈
      6px 白边，两边看起来就是同一个范围；`border-radius` 照旧负责圆角裁剪。
   2. `max-height`：又高又窄的整页插图（比如 attention 的 Figure 1，1834×2699）按列宽铺满会
      高到要滚好几屏。**同时给 max-width 和 max-height**：浏览器会保持宽高比缩到都满足为止，
      既不裁也不变形，只是整张图完整地变小（不再"显示过大"）。 */
.row-figure img {
  display: block; max-width: min(100%, 780px); height: auto;
  max-height: min(74vh, 840px);
  padding: 6px; box-sizing: border-box;
  background: #fff; border: 1px solid var(--border); border-radius: var(--radius-sm);
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


