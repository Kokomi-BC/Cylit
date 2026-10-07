<script setup lang="ts">
/**
 * 侧栏「页面视图」：把 PDF 页面图按页码**一页一行**排下来（《修改计划》第二步，二）。
 *
 * 为什么一页一行：它要的是"扫一眼就知道第几页长什么样"，缩略图网格里每张只有指甲盖大、
 * 图上的公式与表格根本看不清 —— 与「插图」那一页（`fig-card`）同一个路子：**图占满栏宽**。
 * 页号写在**上一张图与下一张图之间**、贴着图片左上角，不再压在图上（图本身可能就是
 * 带页码的扫描页，叠一枚徽章反而两个页码打架），也不需要「定位」按钮 —— 点图就是跳转。
 *
 * 与「目录」共享**同一个"读到哪"**：`currentPage` 由 ReaderView 从正文当前行的页号算出来
 * （`currentRow.page`），这里只负责高亮当前页 + 把它滚进可视区；点图 `emit('focus', page)`，
 * 由 ReaderView 走既有的 `gotoPage` —— 顺带把 `?page=N` 写进 URL，切标签/刷新都落回同一页。
 *
 * 图直接用后端的页面图接口（与论文详情弹窗的封面是同一张），`loading="lazy"` 交给浏览器
 * 按需取：几百页的论文不会一次打满请求。取图失败**不是异常**（老论文没有页图、`data/pages`
 * 被清理过都会 404）：那一格显示占位，但**页码与点击都还在** —— 用户照样能跳到第 N 页读正文。
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
        <span class="pg-no">第 {{ p }} 页</span>
        <img
          v-if="!failed.has(p)"
          :src="src(p)"
          :alt="`第 ${p} 页`"
          loading="lazy"
          @error="onError(p)"
        >
        <span v-else class="ph">
          <AppIcon name="image" :size="18" />页面图不可用
        </span>
      </button>
    </div>
    <div v-else class="empty small">这篇论文还没有页面图（先解析）。</div>
  </div>
</template>

<style scoped>
.pages { display: flex; flex-direction: column; min-width: 0; }
/* 一页一行：每张图占满栏宽（与「插图」那一页同一个路子） */
.pg-list { display: flex; flex-direction: column; gap: 14px; align-content: start; }

.pg-item {
  display: flex; flex-direction: column; align-items: stretch;
  padding: 0; overflow: hidden; cursor: pointer; text-align: left;
  border: 1px solid var(--border); border-radius: var(--radius); background: var(--bg-elev);
  font: inherit;
  transition: border-color var(--dur) var(--ease), box-shadow var(--dur) var(--ease);
}
.pg-item:hover { border-color: var(--border-strong); }
.pg-item.active { border-color: var(--accent); box-shadow: 0 0 0 1px var(--accent) inset; }

/* 页号：**在两张图之间**、贴着图片左上角（不是压在图上的徽章） */
.pg-no {
  padding: 3px 8px 2px;
  font-size: 11px; line-height: 16px; color: var(--text-faint);
  font-variant-numeric: tabular-nums;
}
.pg-item.active .pg-no { color: var(--accent-text); font-weight: 600; }

.pg-item img { display: block; width: 100%; height: auto; }
/* 占位：保持接近页面比例，别让列表在失败时塌成一条 */
.pg-item .ph {
  display: flex; flex-direction: column; align-items: center; justify-content: center;
  gap: 4px; aspect-ratio: 3 / 4; padding: 6px;
  color: var(--text-faint); font-size: 11px; text-align: center;
}
.pg-item.failed { background: var(--bg-sunken); }
</style>
