<script setup lang="ts">
/**
 * 解读整页的**两栏壳**（`/understand/:paperId` 与 `/readmap/:paperId` 共用）。
 *
 * 左栏 = 本文目录（从当前这份 markdown 的标题生成，阅读地图那份还在最前面加一条「相关概念」），
 * 右栏 = 正文。用户口径是"像读论文"：右栏按阅读器的正文列口径（行宽 880px、列内居中、有页边距），
 * 左栏 sticky 且**自己滚**（不跟着正文跑）。
 *
 * 为什么抽成组件：两个整页只差"画哪一份内容"，版面（目录列 / 正文列 / 窄屏折叠 / 滚动联动）
 * 必须完全一样；四处各写一遍必然漂移。
 *
 * 窄屏（<1100px）：左栏收成可折叠的一块 + 一枚「目录」开关（与阅读器窄屏"抽屉 + 开关"同一口径，
 * 只是这里做成**文档流里的折叠块**，不做浮层 —— 整页是正常滚动的文档，浮层抽屉反而会挡住正文）。
 *
 * 滚动联动：窗口滚动时取"最后一个越过阅读线"的锚点（阅读器判定当前章节也是这个口径），
 * 100ms 节流。点目录用 `scrollIntoView`，落点由 CSS 的 `scroll-margin-top` 让开顶栏。
 */
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue';
import AppIcon from '@/components/AppIcon.vue';
import InsightDoc from './InsightDoc.vue';
import InsightToc from './InsightToc.vue';
import { useUnderstanding } from '@/composables/useUnderstanding';
import { buildInsightOutline } from '@/utils/insightOutline';

const props = defineProps<{
  paperId: string;
  section: 'read' | 'map';
}>();

const { entry, pathA, pathB, concepts, hasAny, starting, timedOut, running, started, start }
  = useUnderstanding(computed(() => props.paperId));

/** anchor 前缀：整页一套、左栏页签一套；同一时刻只会挂一个，但前缀不同更保险 */
const prefix = computed(() => `ins-${props.section}`);

const markdown = computed(() => String(
  (props.section === 'map' ? pathB.value?.markdown : pathA.value?.markdown) ?? '',
));
/** 阅读地图第一块是相关概念卡片 → 目录里也要有它 */
const showConcepts = computed(() => props.section === 'map' && concepts.value.length > 0);
const outline = computed(() => buildInsightOutline(markdown.value, {
  prefix: prefix.value, concepts: showConcepts.value,
}));
/** 有内容、且这份 markdown 真的有小节时才画目录（空状态 / 没标题时不留一列空栏） */
const showToc = computed(() => hasAny.value && outline.value.length > 0);

/* ------------------------------ 目录：滚动联动 + 点击跳转 ------------------------------ */
const bodyCol = ref<HTMLElement | null>(null);
const activeId = ref('');
/** 窄屏目录开关（宽屏左栏常驻，用不到） */
const tocOpen = ref(false);

/** 目录条目对应的 DOM 锚点，按大纲顺序：先「相关概念」（若有），再是正文标题 */
function anchorEls(): HTMLElement[] {
  const box = bodyCol.value;
  if (!box) return [];
  const out: HTMLElement[] = [];
  if (showConcepts.value) {
    const el = box.querySelector<HTMLElement>(`#${prefix.value}-concepts`);
    if (el) out.push(el);
  }
  for (const el of Array.from(box.querySelectorAll<HTMLElement>('h1, h2, h3, h4, h5, h6'))) {
    if (el.id) out.push(el);            // id 由 InsightDoc 按同一份大纲贴好
  }
  return out;
}

function syncActive() {
  const els = anchorEls();
  if (!els.length) return;
  const line = (typeof window === 'undefined' ? 0 : window.innerHeight) * 0.3;
  let best = els[0].id;
  for (const el of els) {
    if (el.getBoundingClientRect().top <= line + 4) best = el.id;
  }
  if (best !== activeId.value) activeId.value = best;
}

/** 滚动节流：100ms 内最多算一次（滚动事件本身很密，逐条测量会拖慢滚动）。
 *  用时间戳而不是 rAF：无头 + virtual-time 下 rAF 不保证推进，节流要"确定性"。 */
let lastSync = 0;
function onScroll() {
  const now = Date.now();
  if (now - lastSync < 100) return;
  lastSync = now;
  syncActive();
}

function select(id: string) {
  const el = document.getElementById(id);
  if (!el) return;
  activeId.value = id;               // 点完立刻点亮，不等平滑滚动结束
  el.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

onMounted(() => {
  window.addEventListener('scroll', onScroll, { passive: true });
  window.addEventListener('resize', onScroll);
  void nextTick(syncActive);
});
onUnmounted(() => {
  window.removeEventListener('scroll', onScroll);
  window.removeEventListener('resize', onScroll);
});
// 内容到位（首屏 / 轮询刷新 / 切 section）后重算一次锚点与高亮
watch([outline, () => props.section], () => { void nextTick(syncActive); });
</script>

<template>
  <div class="insight-page" :class="{ 'no-toc': !showToc }">
    <!-- 窄屏才出现的目录开关（宽屏左栏常驻，不需要开关） -->
    <button
      v-if="showToc"
      class="btn sm toc-toggle"
      type="button"
      :aria-expanded="tocOpen"
      @click="tocOpen = !tocOpen"
    >
      <AppIcon name="list" :size="14" />目录
      <AppIcon :name="tocOpen ? 'chevron-up' : 'chevron-down'" :size="13" />
    </button>

    <div class="grid">
      <aside v-if="showToc" class="toc-col" :class="{ open: tocOpen }">
        <div class="toc-title">本文目录</div>
        <InsightToc :items="outline" :active-id="activeId" @select="select" />
      </aside>

      <div ref="bodyCol" class="body-col">
        <InsightDoc
          :section="section"
          :markdown="markdown"
          :concepts="concepts"
          :empty="!hasAny"
          :loading="entry.loading"
          :starting="starting"
          :running="running || started"
          :error="entry.error"
          :timed-out="timedOut"
          :anchor-prefix="prefix"
          @start="start"
          @anchors-ready="syncActive"
        />
      </div>
    </div>
  </div>
</template>

<style scoped>
/* 整壳比正文列宽：正文列在**剩余空间里**居中（`margin: 0 auto`），
   整壳自己也在窗口里居中 —— 宽屏时既不贴左、正文也不会被拉到 1200px 以上。
   1400px 与阅读器 `.rows-wrap` 的上限同一口径。 */
.insight-page { max-width: 1400px; margin: 0 auto; padding: 18px 20px 64px; }
.grid { display: grid; grid-template-columns: 260px minmax(0, 1fr); gap: 24px; align-items: start; }
.insight-page.no-toc .grid { grid-template-columns: minmax(0, 1fr); }

/* 左栏目录：sticky + **自己滚**（正文滚动时它不动）；高度顶到视口底留一点余量。
   顶栏（TabBar）是 sticky 的，所以 top 要避开 `--header-h`。 */
.toc-col {
  position: sticky; top: calc(var(--header-h, 52px) + 12px);
  max-height: calc(100vh - var(--header-h, 52px) - 28px);
  overflow-y: auto; overflow-x: hidden; padding-right: 4px;
}
.toc-title {
  font-size: 11px; letter-spacing: .04em; color: var(--text-faint);
  text-transform: uppercase; margin: 0 0 6px 6px;
}

/* 右栏正文：阅读器的正文列口径 —— 行宽 880、列内居中、页边距（10px 16px 72px 同 `.rows-wrap`）。
   ⚠️ `width: 100%` 不能省：只给 `max-width` + `margin: 0 auto` 时，**auto 外边距会让 grid 的
   stretch 失效**，这一列会退化成"按内容撑到 max-width"（fit-content 到 880px）—— 窄屏下它比
   栅格列还宽，直接顶出整页横向滚动条（实测 900×900 下 documentElement.scrollWidth = 920）。
   给上 `width: 100%`：宽屏时宽 = 100% 被 max-width 夹到 880 并由 auto 外边距居中；
   窄屏时就是列宽，不再溢出。 */
.body-col { width: 100%; max-width: 880px; margin: 0 auto; padding: 10px 16px 72px; min-width: 0; }
/* 点目录滚过去时给顶栏让位（否则标题会被 sticky 顶栏吃掉） */
.body-col :deep(h1), .body-col :deep(h2), .body-col :deep(h3),
.body-col :deep(h4), .body-col :deep(h5), .body-col :deep(h6) {
  scroll-margin-top: calc(var(--header-h, 52px) + 16px);
}

/* 窄屏才有那枚开关 */
.toc-toggle { display: none; }

@media (max-width: 1099px) {
  .insight-page { padding: 14px 14px 56px; }
  .grid { grid-template-columns: minmax(0, 1fr); gap: 0; }
  .toc-toggle { display: inline-flex; align-items: center; gap: 4px; margin: 0 0 8px; }
  /* 窄屏：目录收成文档流里的折叠块（不做浮层抽屉 —— 整页是真滚动的文档，浮层会挡正文） */
  .toc-col {
    position: static; display: none; margin: 0 0 10px; padding: 8px 10px;
    border: 1px solid var(--border); border-radius: var(--radius-sm); background: var(--bg-layer);
    max-height: none;
  }
  .toc-col.open { display: block; max-height: 44vh; overflow-y: auto; }
  .body-col { padding: 6px 4px 48px; }
}
</style>
