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
import InkSurface from './InkSurface.vue';
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
  /** 需要"闪一下"提醒的行 id（点搜索命中 / 引用标号 / 目录跳转后短暂提示"就是这一块"） */
  flashRowId: string;
  /**
   * 闪烁计数（每次都变）。只用来在 `flash-a` / `flash-b` 之间换动画名 ——
   * 不换名字的话，连点同一行时"摘类再挂回"会被样式引擎合并成没变，同一个
   * CSSAnimation（已 `forwards` 停在 opacity:0）继续跑，用户什么都看不到。见 ReaderView.flashRow。
   */
  flashNonce: number;
  /** 后台正在翻译这篇论文：未译的行显示「等待翻译」而不是「未翻译」 */
  translating: boolean;
  /** 正文字号（px）；标题/图注用 em 跟随缩放 */
  fontSize: number;
  /**
   * 页图缩放倍率（阅读器工具栏那个百分比）。
   *
   * 为什么要传它：`.media-box` 平时带 `max-width: 100%`（不许把正文顶宽），
   * 但用户主动放大时要让页图**真的变大** —— 那就得放开这个夹子、改由所在列横向滚动。
   * 不传的话 `pageWidth` 再大也会被 100% 夹回栏宽，症状就是"百分比在涨、页图不变"。
   */
  pageZoom?: number;
  /** 批注模式：每页正文上盖一层可画的画布（见 `InkSurface`） */
  inkOn?: boolean;
}>();

const emit = defineEmits<{
  (e: 'focus', rowId: string): void;
  (e: 'translate-page', page: number): void;
  (e: 'zoom', target: ZoomTarget): void;
  /** 原 PDF 图层在这台设备上渲染不了（平板常见）：交给阅读器**只提示一次** */
  (e: 'pdf-failed'): void;
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
/** 每次闪的动画名在两个等价类之间交替（原因见 props.flashNonce） */
const flashKind = computed(() => (props.flashNonce % 2 ? 'flash-a' : 'flash-b'));
const rowClass = (id: string) => ({
  current: id === props.currentRowId,
  [flashKind.value]: id === props.flashRowId,
});
/**
 * **脚注行**：页内 `---`（转录取的脚注分隔线）之后、直到下一个标题之前的那些行。
 *
 * 为什么要有这个概念：论文的脚注（`*Equal contribution…` / `†Work performed…` / 会议行）
 * 在版面里是**分隔线之下的小字号**，而我们的转录把它按正文排 —— 于是脚注比正文还抢眼
 * （用户口径 2026-12-27「脚注最好使用特殊类似论文的紧凑小字号，避免抢走正文的阅读注意力」）。
 * 判据只用转录里那条 `---`（模型只在脚注分隔线处写它），不靠"以星号开头"这种启发式。
 */
const footnoteRows = computed(() => {
  const out = new Set<string>();
  for (const g of props.groups) {
    let after = false;
    for (const r of g.rows ?? []) {
      const text = String(r.original ?? '').trim();
      /* 两种形状都认：`---` 自成一块（模型照提示词写了空行），或者 `---` 与脚注正文
         挤在同一块里（实测 attention-cleanroom 第 1 页就是这样：`---\n*Equal contribution…`）。
         后者也要把它连同后面的行标成脚注 —— 否则样式只在"模型听话的那几页"生效。 */
      if (/^-{3,}\s*$/.test(text)) { after = true; continue; }
      if (/^-{3,}\s*\n/.test(text)) { after = true; out.add(r.id); continue; }
      if (!after) continue;
      if (r.type === 'heading' || r.type === 'formula') { after = false; continue; }
      out.add(r.id);
    }
  }
  return out;
});
/** 整页闪：目标引用没有行锚点（插图 / 表格 / 纯页码引用）时，亮的是整页 */
const pageClass = (page: number) => ({ [flashKind.value]: props.flashRowId === `page:${page}` });
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

/**
 * 「大图」口径（用户 2026-09-28）：**图片在一页里占比很大时，允许它显示得更大**。
 *
 * 为什么按"页面占比"而不是按"图有多宽"：插图行是**通栏**的，560px 对一张整幅模型图太挤
 * （里面的坐标轴标签根本看不清），而对一张小图 560 已经够。占比拿区域 bbox 与页面尺寸算 ——
 * `media[].bbox` 本来就在 payload 里（后端一直带着），只是以前没进 TS 类型。
 *
 * 阈值 0.25 来自实测分布（库里 123 个区域）：30~40% 那一档是"整幅图"
 * （attention Fig. 4/5、boomerang 图 31~34、dammfnd Fig. 2、24 09 的 Fig. 3/7…），
 * 而 11~21% 那一档是常规半幅图（turboquant Fig. 1 = 19.8%、KAT Fig. 5 = 20.9%）——
 * 0.25 正好落在两档之间的空档里。
 */
const LARGE_FIGURE_SHARE = 0.25;

function isLargeFigure(m: Media, info: PageInfo | null): boolean {
  const b = m.bbox;
  const w = Number(info?.width) || 0;
  const h = Number(info?.height) || 0;
  if (!b || b.length < 4 || w <= 0 || h <= 0) return false;
  return ((b[2] - b[0]) * (b[3] - b[1])) / (w * h) >= LARGE_FIGURE_SHARE;
}

/** 放大目标：URL + **图注**（弹窗标题显示图注，见 `ZoomTarget`）；页码取所在页组 */
const zoomTarget = (m: Media, page: number): ZoomTarget => ({
  url: zoomUrl(m), caption: m.caption ?? '', page, number: m.number ?? null,
});
</script>

<template>
  <div
    class="rows-wrap"
    :class="{ zoomed: (props.pageZoom ?? 1) > 1.02 }"
    :style="{ '--reader-font-size': `${props.fontSize}px` }"
  >
    <div v-if="!props.groups.length" class="empty">没有可显示的内容（先解析或翻译这篇论文）</div>

    <section v-for="g in props.groups" :key="g.page" :data-page="g.page" class="pg"
             :class="pageClass(g.page)">
      <!-- 手写批注层：**始终挂载**（退出批注后墨迹要留着看得见，用户口径 2026-10-18）。
           平时它 pointer-events: none 不拦事件；画布只在「视口附近」才按 2× dpr 分配，
           滚远了回收，所以 46 页也不会吃内存（见 InkSurface 的 ensureSize）。 -->
      <InkSurface :page="g.page" :enabled="props.inkOn" />
      <header class="pg-head">
        <!-- 对照模式下页码挪到 **PDF 那一列**（居中、不可选中），这里就不再重复一遍 -->
        <span v-if="!pageLeft" class="pg-no">第 {{ g.page }} 页</span>
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
            :highlights="props.highlights"
            @failed="emit('pdf-failed')"
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
          <!-- PDF 侧自己的页码：**在本列居中**、**不可选中**（划词时不该把「第 3 页」选进去） -->
          <div class="pdf-no" data-pdf-no>第 {{ g.page }} 页</div>
          <div class="media-box" :style="{ width: `${props.pageWidth}px` }">
            <PdfPage
              v-if="useRealPdf(g)"
              :paper-id="props.paperId"
              :page="g.page"
              :width="props.pageWidth"
              :page-width="g.info?.width"
              :page-height="g.info?.height"
              :highlights="props.highlights"
              @failed="emit('pdf-failed')"
            />
            <img v-else :src="pageImage(g.page)" :alt="`第 ${g.page} 页`" loading="lazy">
          </div>
        </div>
        <div class="col-rows">
          <div
            v-for="r in g.rows"
            :key="r.id"
            :data-row-id="r.id"
            :data-row-type="r.type"
            class="row-pair"
            :class="[rowClass(r.id), { 'is-footnote': footnoteRows.has(r.id) }]"
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
          :data-row-type="r.type"
          class="row-pair"
          :class="{ 'two-col': textCompare, wide: isWide(r), 'is-footnote': footnoteRows.has(r.id), ...rowClass(r.id) }"
        >
          <!-- 插图：按论文的排法——**图在上、图注在下**，通栏居中（对照时跨两列） -->
          <figure
            v-for="(m, i) in mediaFor(r.id)"
            :key="`m-${i}`"
            class="row-figure"
            :class="{ large: isLargeFigure(m, g.info) }"
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
          :class="{ large: isLargeFigure(m, g.info) }"
          :title="`${m.caption || '插图'}（点击看大图）`"
          @click="emit('zoom', zoomTarget(m, g.page))"
        >
          <img :src="figureSrc(m)" :alt="m.caption || '插图'" loading="lazy">
        </figure>

        <div v-if="!g.rows.length" class="empty small">这一页还没有内容</div>
      </div>
    </section>
  </div>
</template>

<style scoped>
.rows-wrap {
  padding: 10px 16px 72px; max-width: 1400px; margin: 0 auto;
  /* 正文字号的唯一来源：标题/图注/公式都用 em 相对它缩放。
     `* var(--ts, 1)`：正文那一档的字号倍数必须乘在这条上 —— 它是全篇正文的根，
     只乘别的声明等于"设置里调正文，论文文字不动"。 */
  font-size: calc(var(--reader-font-size, 13.5px) * var(--ts, 1));
}

/* position: relative 是给批注层（绝对定位铺满这一页）用的：不设的话它会以更外层为参照，
   笔迹就跑到页外去了 */
.pg { margin-bottom: 26px; position: relative; }
/* 页头**不吸顶**。
 * 吸顶（position: sticky）会在正文顶部压出一条只放了页码的空带，并把紧跟着的那行
 * 切掉上半截 —— 看起来像一条"莫名的遮罩"。页码是页与页之间的分隔，跟着内容滚动即可。
 * **不画下划线**（用户口径）：页码只是一枚很淡的标注，横线纯属噪声。 */
.pg-head {
  display: flex; align-items: center; gap: 6px;
  padding: 6px 2px 0;
  font-size: calc(11.5px * var(--ts, 1)); color: var(--text-faint);
}
.pg-no {
  font-weight: 600; color: var(--text-dim); font-variant-numeric: tabular-nums;
  /* 页码不是内容：别被划进选区（用户口径） */
  user-select: none; -webkit-user-select: none;
}

/* 正文内容**顶部留一点白**（用户口径）：左边是论文页图，页图里自带纸面页边距，
 * 右边如果贴着页头就开始，两栏顶部一高一低看着突兀；给**右侧 markdown** 下移一点点就顺眼了。
 * 只给内容列（不包含页图那一列）：`.pg-rows` 是文本对照，`.col-rows` 是"原 PDF 在左"时的右侧。
 * `:where()` 保持 0 特异性，别干扰下面 `.pg-rows.two-col` 那些布局规则。 */
:where(.pg-rows, .col-rows) { padding-top: 18px; }
/* **第一页**上方不再留"页与页之间"那种间距（用户口径：第一页顶部留白要缩小）：
   上面那 18px 是为了让右栏文字和左栏页图的内边距对齐，但第一篇的第一页本来就顶着顶栏，
   再叠一层就成了一截空带。页头（页码）与页图也各收一档。 */
.rows-wrap > .pg:first-child .pg-head { padding-top: 0; }
.rows-wrap > .pg:first-child :where(.pg-rows, .col-rows) { padding-top: 6px; }
.rows-wrap > .pg:first-child .pg-image-only { padding-top: 0; }
.rows-wrap > .pg:first-child .pg-pair .col-rows { padding-top: 6px; }

/* 行容器 = 纯排版块，**不带任何装饰**：
   - 不画 border-top 分隔线（之前每块一条横线，密而卙）
   - 不加 border-radius（只画上边框时圆角会把两端掰弯，看着像凭空多一个"圆角框"）
   - 不留垂直内边距（否则相邻块之间会多出 12px 空白，不像一段连续的 markdown）
   - **不做 hover 底色**：鼠标停在哪一块，那一块就变成一块灰色圆角块，
     在"原 PDF 在左"模式下只有右栏有底色，看起来就像凭空多出一个变体（用户报过）。
   正文不做任何悬浮装饰（行内复制按钮已按用户要求删除），文字本身保持干净。
   "当前行"仍由 ReaderView 的滚动跟踪维护（目录高亮 / j·k 跳转 / 进度保存），只是不画出来。 */
.row-pair { position: relative; padding: 0 4px; }

/* 「就是这一块」：点搜索命中 / 目录 / 引用标号跳过来之后，整行短暂亮一下再淡出。
 * 用 ::before 覆盖层而不是给行本身上底色 —— 行是纯排版块，改它的 background 会连图片、
 * 表格一起变色；覆盖层只负责"闪"，且 `pointer-events:none` 不挡点击。
 * 底色默认就是**可见**的（不是靠动画从 0 淡入）：万一动画没跑（无头浏览器冻结 CSS 动画、
 * 或者被系统设置关掉），这层照样亮着，1.6s 后由 JS 摘掉类 → 效果依旧成立。
 *
 * ⚠️ 为什么是两个等价类 + 两个 keyframes（`-a` / `-b` 交替，见 props.flashNonce）：
 * 连点**同一个**标号时，`flash` 类被"摘掉又挂回"的两次写 DOM 在同一个任务里，样式引擎
 * 合并成"没变" → 同一个 CSSAnimation 实例接着跑（实测 `getAnimations()` 前后是同一个对象），
 * 而它已经 `forwards` 停在 opacity:0，用户看到的就是"再点没反应，先点别的再点回来才行"。
 * 换动画名 = 计算样式真的变了，浏览器只能重播一遍。 *
 * ⚠️ 底色**必须淡**（20% → 11%）：正文行的粒度是"一个语义单元"，长行可能有 1600+ 字符、
 * 350px 高 —— 整片 20% 的底色糊在这种行上，看起来跟"整页刷了一下"没区别
 * （用户 2026-12-03 报的「标号 1 也是整页」：那一条落的行正好 354px 高）。
 * ⚠️ 曾经在这层上加过一根 3px 的左侧竖条（`box-shadow: inset 3px 0 0 0`）当"从这里开始"的定位感，
 * **2026-11 用户口径撤掉了**：点定位时页面里"侧边闪一根竖线"是干扰，定位只要淡底亮一下。
 * 别再往这层加竖条 / 分隔线 —— 这一层的唯一职责是"亮一下再淡出"。
 *
 * 2026-12-27 用户口径：**加一条细主题色边框**（与合并大块 .flash-block 同一档），
 * 淡底在浅背景上不够醒目；用 inset 阴影而不是 border，盒子尺寸不变（不影响落点计算）。 */
.row-pair.flash-a::before,
.row-pair.flash-b::before {
  content: ''; position: absolute; inset: -3px -6px; pointer-events: none;
  border-radius: 8px; opacity: .95;
  background: color-mix(in srgb, var(--accent) 11%, transparent);
  box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--accent) 55%, transparent);
}
.row-pair.flash-a::before { animation: row-flash-a 1.5s ease-out forwards; }
.row-pair.flash-b::before { animation: row-flash-b 1.5s ease-out forwards; }
@keyframes row-flash-a {
  0% { opacity: .95; }
  60% { opacity: .6; }
  100% { opacity: 0; }
}
@keyframes row-flash-b {
  0% { opacity: .95; }
  60% { opacity: .6; }
  100% { opacity: 0; }
}
/* 整页闪（引用没有行锚点时）：同一套观感，罩在整页上 —— 只描边 + 淡底，不动内容。
   ⚠️ 数字与 `.row-pair.flash-*::before` **逐项相同**（11% 淡底 / 1px 55% 细描边 / 8px 圆角），
   只有四边外扩更大（整页是张卡片，-6/-10 才不会贴住页边）：2026-12-27 用户口径
   「闪烁样式也不太一致」说的就是这处原来是 `1.5px 45%` 的 border、12% 底、12px 圆角。
   同样用 inset 阴影而不是 border —— border 会把这层覆盖盒再撑大 1.5px。 */
.pg.flash-a::before,
.pg.flash-b::before {
  content: ''; position: absolute; inset: -6px -10px; pointer-events: none;
  border-radius: 8px; opacity: .95;
  background: color-mix(in srgb, var(--accent) 11%, transparent);
  box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--accent) 55%, transparent);
}
.pg.flash-a::before { animation: row-flash-a 1.5s ease-out forwards; }
.pg.flash-b::before { animation: row-flash-b 1.5s ease-out forwards; }
@media (prefers-reduced-motion: reduce) {
  /* 尊重系统设置：不做淡出动画，亮着等 JS 摘类即可 */
  .row-pair.flash-a::before,
  .row-pair.flash-b::before,
  .pg.flash-a::before,
  .pg.flash-b::before { animation: none; opacity: .5; }
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
      既不裁也不变形，只是整张图完整地变小（不再"显示过大"）。
   3. 尺寸（2026-09-28 用户口径"阅读器正文里的插图卡片缩小"）：780×840 → **560×420**，
      与 Markdown 视图 / 知识库里的插图**同一口径**（`.md img`）—— 插图在哪儿看都一样大，
      想看细节就点开（弹窗按原始尺寸，见 `zoomTarget`）。整幅图行是通栏的，
      不设上限时一张图会把整屏占满、上下翻页变成翻图册。 */
.row-figure img {
  display: block; max-width: min(100%, 560px); height: auto;
  max-height: min(48vh, 420px);
  padding: 6px; box-sizing: border-box;
  background: #fff; border: 1px solid var(--border); border-radius: var(--radius-sm);
}
/* 整幅图（占页面 ≥25%，见 `isLargeFigure`）：允许放大，否则图里的坐标轴/图例根本读不出来。
   上限回到"半屏级"（880 × 76vh）而不是无上限 —— 再大就又变成"翻图册"了。 */
.row-figure.large img { max-width: min(100%, 880px); max-height: min(76vh, 800px); }
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
/* 分割线：**永远画在「文字列挨着 PDF 列」的那一条边上** ——
   以前只有 `border-left`，页图在右（`.image-right`）时那条线就跑到整页最左缘去了，
   PDF 与译文之间反而是空的（用户口径：PDF 侧要有一条分割线）。 */
.pg-pair .col-rows { min-width: 0; border-left: 1px solid var(--divider); padding-left: 20px; }
.pg-pair.image-right .col-rows {
  border-left: 0; border-right: 1px solid var(--divider);
  padding-left: 0; padding-right: 20px;
}
/* PDF 侧那行页码：**在本列居中**、**不可选中**（划词时不该把「第 3 页」选进去） */
.pdf-no {
  text-align: center; padding: 0 0 6px;
  font-size: calc(12px * var(--ts, 1)); color: var(--text-faint);
  font-variant-numeric: tabular-nums;
  user-select: none; -webkit-user-select: none;
}

.pg-image-only { display: flex; flex-direction: column; align-items: center; gap: 10px; padding: 8px 0 4px; }
.media-box { max-width: 100%; margin: 0 auto; background: #fff; border: 1px solid var(--border); border-radius: var(--radius-sm); overflow: hidden; }
/* 放大（>100%）时放开栏宽夹子：页图按 `pageWidth` 真的变宽，超出的部分由**所在列**横向滚动。
 * 只放开夹子不加 scroll 容器的话，超宽页图会盖到旁边那一列的文字上（grid 项不会把兄弟推开）。 */
.rows-wrap.zoomed .media-box { max-width: none; }
.rows-wrap.zoomed .pg-image-only { overflow-x: auto; }
.rows-wrap.zoomed .pg-pair .col-image { overflow-x: auto; }
.media-box img { display: block; width: 100%; height: auto; }
/* 点一下开大图（不然半栏的 A4 页上正文只有几像素高，根本看不清） */
.media-box.zoomable { cursor: zoom-in; }
.media-box.zoomable:hover { border-color: var(--accent); }

/* 原来这里是「显示参考文献」开关（`.rows-foot`）：已按用户要求移除，参考文献恒显示。 */

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
  .pg-pair .col-rows,
  .pg-pair.image-right .col-rows { border-left: 0; border-right: 0; padding-left: 0; padding-right: 0; }
  .pg-pair .col-image .media-box { position: static; }
}
</style>


