<script setup lang="ts">
/**
 * 侧栏「页面视图」：把 PDF 页面图按页码排成缩略图网格（《修改计划》第二步，二）。
 *
 * 与「目录」共享**同一个"读到哪"**：`currentPage` 由 ReaderView 从正文当前行的页号算出来
 * （`currentRow.page`），这里只负责高亮当前页 + 把它滚进可视区；点缩略图 `emit('focus', page)`，
 * 由 ReaderView 走既有的 `gotoPage` —— 顺带把 `?page=N` 写进 URL，切标签/刷新都落回同一页。
 *
 * 缩略图直接用后端的页面图接口（与论文详情弹窗的封面是同一张），`loading="lazy"` 交给浏览器
 * 按需取：几百页的论文不会一次打满请求。
 *
 * 取图失败**不是异常**（老论文没有页图、`data/pages` 被清理过都会 404）：那一格显示占位，
 * 但**页码与点击都还在** —— 用户照样能跳到第 N 页读正文，而不是对着一张裂图发愣。
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
    <div class="pages-head">
      <span class="tiny faint">{{ list.length }} 页</span>
      <span class="spacer" />
      <span class="tiny faint">点缩略图跳到那一页</span>
    </div>

    <div v-if="list.length" ref="root" class="pages-grid">
      <button
        v-for="p in list"
        :key="p"
        type="button"
        class="page-thumb"
        :class="{ active: p === currentPage, failed: failed.has(p) }"
        :data-page="p"
        :aria-current="p === currentPage ? 'page' : undefined"
        :title="`跳到第 ${p} 页`"
        @click="emit('focus', p)"
      >
        <img
          v-if="!failed.has(p)"
          :src="src(p)"
          :alt="`第 ${p} 页`"
          loading="lazy"
          @error="onError(p)"
        >
        <span v-else class="ph">
          <AppIcon name="image" :size="16" />页面图不可用
        </span>
        <span class="no">{{ p }}</span>
      </button>
    </div>
    <div v-else class="empty small">这篇论文还没有页面图（先解析）。</div>
  </div>
</template>

<style scoped>
.pages { display: flex; flex-direction: column; min-width: 0; }
.pages-head { display: flex; align-items: center; gap: 6px; margin: 0 0 8px; }

/* 网格：窄栏放 2 列、宽栏 3~4 列（`auto-fill` 按实际宽度自己排） */
.pages-grid {
  display: grid; gap: 8px; align-content: start;
  grid-template-columns: repeat(auto-fill, minmax(74px, 1fr));
}
.page-thumb {
  position: relative; padding: 0; overflow: hidden; cursor: pointer;
  border: 1px solid var(--border); border-radius: var(--radius-sm);
  background: var(--bg-elev); font: inherit;
  transition: border-color var(--dur) var(--ease), transform var(--dur-fast) var(--ease);
}
.page-thumb:hover { border-color: var(--border-strong); transform: translateY(-1px); }
.page-thumb.active {
  border-color: var(--accent);
  box-shadow: 0 0 0 1px var(--accent) inset;
}
.page-thumb img { display: block; width: 100%; height: auto; }
/* 占位：保持与缩略图接近的比例，别让网格在失败时塌成一条 */
.page-thumb .ph {
  display: flex; flex-direction: column; align-items: center; justify-content: center;
  gap: 4px; aspect-ratio: 3 / 4; padding: 4px;
  color: var(--text-faint); font-size: 10.5px; text-align: center;
}
.page-thumb.failed { background: var(--bg-sunken); }
/* 页码：缩略图右下角一枚小徽章（图本身可能就是有页码的扫描页，所以压在主色上才看得清） */
.page-thumb .no {
  position: absolute; right: 3px; bottom: 3px;
  min-width: 18px; padding: 0 4px; border-radius: 999px;
  background: color-mix(in srgb, var(--bg) 82%, transparent);
  border: 1px solid var(--border);
  font-size: 10px; line-height: 15px; color: var(--text-dim);
  font-variant-numeric: tabular-nums;
}
.page-thumb.active .no {
  background: var(--accent); border-color: var(--accent); color: var(--text-on-accent);
}
</style>
