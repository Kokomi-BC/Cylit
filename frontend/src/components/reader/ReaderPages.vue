<script setup lang="ts">
/**
 * 侧栏「页面视图」：把 PDF 页面图按页码**一页一行**排下来（《修改计划》第二步，二）。
 *
 * 为什么一页一行：它要的是"扫一眼就知道第几页长什么样"，缩略图网格里每张只有指甲盖大、
 * 图上的公式与表格根本看不清 —— 与「插图」那一页（`fig-card`）同一个路子。
 *
 * 三处尺寸/结构的讲究（都是用户看出来的问题）：
 * - 页号在**图片外面**：它是图上边单独一行文字，**不在**那圈边框里 ——
 *   框住"页号 + 图"会让人以为页号是图的一部分（或被裁进去的页眉）；
 * - 图**不要铺满**整栏：宽栏里铺满会大得吓人（`max-width` 封顶，窄栏里自然收缩）；
 * - 页号贴在图片**左上角**、在两张图之间（不是压在图上的徽章：图本身可能就是带页码的扫描页，
 *   叠一枚徽章会出现两个页码），也不需要「定位」按钮 —— 点图就是跳转。
 *
 * **触摸能滚**：图默认是"可拖拽"的（浏览器原生 drag-and-drop），手指按在图上往外划，
 * 浏览器会去拖这张图而不是滚列表；而一行一张之后整行几乎全是图，于是"页面视图划不动"。
 * 这里把图变成不接收指针事件、并给整格写清 `touch-action: pan-y`（详见 `styles/base.css`）。
 *
 * 与「目录」共享**同一个"读到哪"**：`currentPage` 由 ReaderView 从正文当前行的页号算出来
 * （`currentRow.page`），这里只负责高亮当前页 + 把它滚进可视区；点图 `emit('focus', page)`，
 * 由 ReaderView 走既有的 `gotoPage` —— 顺带把 `?page=N` 写进 URL，切标签/刷新都落回同一页。
 *
 * 取图失败**不是异常**（老论文没有页图、`data/pages` 被清理过都会 404）：那一格显示占位，
 * 但**页码与点击都还在** —— 用户照样能跳到第 N 页读正文。
 */
import { computed, nextTick, ref, watch } from 'vue';
import { assetUrl } from '@/api/client';
import AppIcon from '@/components/AppIcon.vue';

const props = defineProps<{
  paperId: string;
  pageCount: number;
  /** 当前阅读页（与「目录」共享同一份） */
  currentPage: number;
}>();

const emit = defineEmits<{ (e: 'focus', page: number): void }>();

/** 取图失败的页号：显示占位而不是裂图 */
const failed = ref<Set<number>>(new Set());
function onError(page: number) {
  failed.value = new Set(failed.value).add(page);
}

const list = computed(() => Array.from(
  { length: Math.max(0, Number(props.pageCount) || 0) }, (_v, i) => i + 1,
));

function src(page: number): string {
  return assetUrl(`/api/v1/papers/${props.paperId}/pages/${page}`);
}

/**
 * 当前页滚进可视区：正文往下读，页面视图要跟着走（否则"当前页高亮"永远在屏幕外）。
 * `block: 'nearest'` 只在需要时才滚，不会把用户的滚动位置反复拽回来。
 */
const root = ref<HTMLElement | null>(null);
watch(() => props.currentPage, async (page) => {
  await nextTick();
  root.value?.querySelector<HTMLElement>(`[data-page="${page}"]`)
    ?.scrollIntoView({ block: 'nearest' });
});
</script>

<template>
  <div class="pages">
    <div v-if="list.length" ref="root" class="pg-list">
      <button
        v-for="p in list"
        :key="p"
        type="button"
        class="pg-item"
        :class="{ active: p === currentPage, failed: failed.has(p) }"
        :data-page="p"
        :aria-current="p === currentPage ? 'page' : undefined"
        :title="`跳到第 ${p} 页`"
        @click="emit('focus', p)"
      >
        <!-- 页号在**图外面**（不在下面那圈边框里） -->
        <span class="pg-no">第 {{ p }} 页</span>
        <span class="pg-shot">
          <img
            v-if="!failed.has(p)"
            :src="src(p)"
            :alt="`第 ${p} 页`"
            loading="lazy"
            draggable="false"
            @error="onError(p)"
          >
          <span v-else class="ph">
            <AppIcon name="image" :size="18" />页面图不可用
          </span>
        </span>
      </button>
    </div>
    <div v-else class="empty small">这篇论文还没有页面图（先解析）。</div>
  </div>
</template>

<style scoped>
.pages { display: flex; flex-direction: column; min-width: 0; }
/* 一页一行：整列**居中**（宽栏里不至于把图挤在左边、右边空一大片），
   每格宽度跟着图走（`min(100%, 240px)`）—— 页号因此仍然贴着图片左上角。 */
.pg-list {
  display: flex; flex-direction: column; align-items: center;
  gap: 14px; padding: 6px 6px 32px;
}

.pg-item {
  display: flex; flex-direction: column; align-items: flex-start; gap: 4px;
  width: min(100%, 240px);
  padding: 0; border: 0; background: transparent; cursor: pointer; text-align: left;
  font: inherit;
  /* 竖直方向可以划：这一格几乎全是图，不写清楚浏览器可能把它当拖拽/手势目标 */
  touch-action: pan-y pinch-zoom;
}

/* 页号：**图外面**的一行文字（不是压在图上的徽章，也不在图的边框里） */
.pg-no {
  font-size: 11px; line-height: 16px; color: var(--text-faint);
  font-variant-numeric: tabular-nums;
}
.pg-item.active .pg-no { color: var(--accent-text); font-weight: 600; }

/* 图框：圆角与边框只包住图本身；宽度封顶（宽栏里铺满会大得吓人） */
.pg-shot {
  display: block; width: 100%; max-width: 240px;
  border: 1px solid var(--border); border-radius: var(--radius);
  background: var(--bg-elev); overflow: hidden;
  transition: border-color var(--dur) var(--ease), box-shadow var(--dur) var(--ease);
}
.pg-item:hover .pg-shot { border-color: var(--border-strong); }
.pg-item.active .pg-shot { border-color: var(--accent); box-shadow: 0 0 0 1px var(--accent) inset; }
.pg-item.failed .pg-shot { background: var(--bg-sunken); }

/* 图不吃指针事件：手指按在图上时，事件落在整格上 —— 浏览器才不会去"拖这张图"而不滚列表 */
.pg-shot img {
  display: block; width: 100%; height: auto;
  pointer-events: none; -webkit-user-drag: none;
}

/* 占位：保持接近页面比例，别让列表在失败时塌成一条 */
.pg-shot .ph {
  display: flex; flex-direction: column; align-items: center; justify-content: center;
  gap: 4px; aspect-ratio: 3 / 4; padding: 6px;
  color: var(--text-faint); font-size: 11px; text-align: center;
}
</style>
