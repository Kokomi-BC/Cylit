<script setup lang="ts">
/**
 * 「论文解读」的**整页版**（`/understand/:paperId`）。
 *
 * 三条用户口径：
 * - 顶栏**只留标题**（没有刷新、没有「重新生成解读」；重跑入口只在论文库的「⋯」菜单里）；
 * - 版面是**阅读器同构的两栏**：左栏本文目录、右栏正文（行宽 880 / 列内居中 / 有页边距）——
 *   这一套在 `InsightPage` 里；
 * - 正文字号跟阅读器（服务端进度里的 `text_size`）——"像在读论文"的另一个条件。
 *
 * ⚠️ 2026-10-02：原「阅读地图」整页（`/readmap/:paperId`，与本页共用一个组件）随该功能一起删除。
 */
import { computed, onMounted, ref, watch } from 'vue';
import { useRoute } from 'vue-router';
import { api } from '@/api/client';
import { loadPapers, state } from '@/stores/app';
import { routeKey, setTabTitle } from '@/stores/tabs';
import { truncate } from '@/utils/format';
import { readerFontPx } from '@/utils/readerFont';
import InsightPage from '@/components/insight/InsightPage.vue';

const route = useRoute();
const paperId = computed(() => String(route.params.paperId ?? ''));
const paper = computed(() => (state.papers ?? []).find((p) => p.id === paperId.value));

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

/* 标签标题：`论文简称 · 论文解读` */
watch(paper, () => {
  const short = truncate(paper.value?.title || paperId.value, 18);
  setTabTitle(routeKey(route), `${short} · 论文解读`);
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

    <!-- 两栏壳：左栏本文目录 + 右栏正文 -->
    <InsightPage :paper-id="paperId" />
  </div>
</template>

<style scoped>
.doc-page { padding: 18px 20px 0; }
.head { max-width: 1400px; margin: 0 auto; }
.head h1 { font-size: 19px; line-height: 1.35; margin: 0 0 4px; }
@media (max-width: 1099px) {
  .doc-page { padding: 14px 14px 0; }
}
</style>
