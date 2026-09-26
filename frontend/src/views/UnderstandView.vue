<script setup lang="ts">
/**
 * 「论文解读」/「阅读地图」的**整页版**（`/understand/:paperId` 与 `/readmap/:paperId` 共用本组件）。
 *
 * 为什么两个路由共用一个组件：两份内容是同一个接口的 `a` / `b`，布局、空状态、轮询、
 * 排版完全一样，只有"画哪一份"不同。两个**路由名**是必须的（应用内标签键只看 name + params，
 * 同路由加 query 开不出第二个标签）。
 *
 * 三条用户口径：
 * - 顶栏**只留标题**（没有刷新、没有「重新生成解读」；重跑入口只在论文库的「⋯」菜单里）；
 * - 正文用**阅读器的排版**（同一个 `InsightDoc`，字号取服务端进度里的 `text_size`）——
 *   用户要的是"像在读论文"，不是又一套卡片；
 * - 空状态保留「开始解读」（它不是"重新生成"）。
 *
 * 数据：与阅读器左栏两个页签**共用** `useUnderstanding` 的整包缓存（切来切去不会重复打接口）。
 */
import { computed, onMounted, ref, watch } from 'vue';
import { useRoute } from 'vue-router';
import { api } from '@/api/client';
import { loadPapers, state } from '@/stores/app';
import { routeKey, setTabTitle } from '@/stores/tabs';
import { truncate } from '@/utils/format';
import { readerFontPx } from '@/utils/readerFont';
import { useUnderstanding } from '@/composables/useUnderstanding';
import InsightDoc from '@/components/insight/InsightDoc.vue';

const props = withDefaults(defineProps<{
  /** `read` = 论文解读（A 路 markdown）；`map` = 阅读地图（B 路 markdown + 相关概念） */
  section?: 'read' | 'map';
}>(), { section: 'read' });

const route = useRoute();
const paperId = computed(() => String(route.params.paperId ?? ''));
const paper = computed(() => (state.papers ?? []).find((p) => p.id === paperId.value));

const { entry, pathA, pathB, concepts, hasAny, starting, timedOut, running, started, start }
  = useUnderstanding(paperId);

const sectionLabel = computed(() => (props.section === 'map' ? '阅读地图' : '论文解读'));
const markdown = computed(() => String(
  (props.section === 'map' ? pathB.value?.markdown : pathA.value?.markdown) ?? '',
));

/**
 * 正文字号跟阅读器：**服务端进度里的 `text_size`** 是权威那份（"这篇论文上次看的时候多大"），
 * 取不到就退回本地偏好（阅读器的 `pm-reader-text-size`），再不行用默认"小"。
 * 为什么单拉一次 `/progress`：整页没必要为此加载整个阅读器 payload（那份是 MB 级）。
 */
const fontPx = ref(readerFontPx(localStorage.getItem('pm-reader-text-size')));
async function loadFont() {
  try {
    const p = await api.get<{ text_size?: string | null }>(`/papers/${paperId.value}/progress`);
    fontPx.value = readerFontPx(p?.text_size);
  } catch { /* 取不到就用本地那份 */ }
}

/* 标签标题：`论文简称 · 论文解读` / `… · 阅读地图`（标签条上两个整页一眼分得清） */
watch([paper, sectionLabel], () => {
  const short = truncate(paper.value?.title || paperId.value, 18);
  setTabTitle(routeKey(route), `${short} · ${sectionLabel.value}`);
}, { immediate: true });

onMounted(async () => {
  if (!(state.papers ?? []).length) void loadPapers({ limit: 200 }).catch(() => {});
  await loadFont();
});
watch(paperId, () => { void loadFont(); });
</script>

<template>
  <div class="doc-page" :style="{ '--reader-font-size': `${fontPx}px` }">
    <!-- 顶栏只留标题（用户口径）：动作按钮都不放这儿，重跑在论文库的「⋯」里 -->
    <header class="head">
      <h1>{{ paper?.title || paperId }}</h1>
    </header>

    <InsightDoc
      :section="props.section"
      :markdown="markdown"
      :concepts="concepts"
      :empty="!hasAny"
      :loading="entry.loading"
      :starting="starting"
      :running="running || started"
      :error="entry.error"
      :timed-out="timedOut"
      narrow
      @start="start"
    />
  </div>
</template>

<style scoped>
.doc-page { padding: 18px 20px 64px; }
.head h1 { font-size: 19px; line-height: 1.35; margin: 0 auto 14px; max-width: 880px; }
</style>
