<script setup lang="ts">
/**
 * 左侧目录：**中英双语**（英文原文在上、中文译文在下）+ 可折叠层级。
 *
 * 数据来自后端 `outline`：标题行由页级翻译同一次调用产出（英文 `text` / 中文 `text_zh`），
 * 因此中英始终成对；没有页级产物的论文只有英文（`text_zh` 为空）。
 */
import { computed } from 'vue';
import type { TocRow } from '@/utils/format';
import AppIcon from '@/components/AppIcon.vue';

const props = defineProps<{
  rows: TocRow[];
  currentRowId: string;
  pageCount: number;
  collapsed: string[];
  searchHits: Array<{ id: string; page: number; side: string; snippet: string }>;
}>();

const emit = defineEmits<{
  (e: 'focus', rowId: string): void;
  (e: 'toggle', rowId: string): void;
  (e: 'collapse-all'): void;
  (e: 'expand-all'): void;
  (e: 'clear-search'): void;
}>();

const collapsedSet = computed(() => new Set(props.collapsed));
</script>

<template>
  <div class="toc">
    <div class="toc-head">
      <span class="tiny faint">目录 {{ props.rows.length }}</span>
      <span class="spacer" />
      <button class="btn icon sm" title="全部折叠" @click="emit('collapse-all')">
        <AppIcon name="unfold-less" :size="14" />
      </button>
      <button class="btn icon sm" title="全部展开" @click="emit('expand-all')">
        <AppIcon name="unfold-more" :size="14" />
      </button>
    </div>

    <div
      v-for="row in props.rows"
      :key="row.id"
      class="toc-item"
      :class="[`depth-${Math.min(row.depth, 4)}`, { active: row.id === props.currentRowId }]"
      @click="emit('focus', row.id)"
    >
      <button
        v-if="row.hasChildren"
        class="caret"
        :title="collapsedSet.has(row.id) ? '展开' : '折叠'"
        @click.stop="emit('toggle', row.id)"
      >
        <AppIcon :name="collapsedSet.has(row.id) ? 'chevron-right' : 'chevron-down'" :size="13" />
      </button>
      <span v-else class="caret-space" />
      <span class="txt">
        <span class="en truncate">
          <span v-if="row.number" class="num">{{ row.number }}</span>{{ row.text }}
        </span>
        <span v-if="row.textZh" class="zh truncate">{{ row.textZh }}</span>
      </span>
      <span class="pg">p{{ row.page }}</span>
    </div>
    <div v-if="!props.rows.length" class="tiny faint" style="padding:8px">未识别到标题结构</div>

    <div v-if="props.searchHits.length" class="hits">
      <div class="toc-head">
        <span class="tiny faint">命中 {{ props.searchHits.length }}</span>
        <span class="spacer" />
        <button class="btn icon sm" title="清空搜索" @click="emit('clear-search')">
          <AppIcon name="close" :size="14" />
        </button>
      </div>
      <div v-for="hit in props.searchHits" :key="hit.id" class="toc-item" @click="emit('focus', hit.id)">
        <span class="caret-space" />
        <span class="txt">
          <span class="tiny faint">p{{ hit.page }} · {{ hit.side === 'en' ? '原文' : '译文' }}</span>
          <span class="tiny truncate">{{ hit.snippet }}</span>
        </span>
      </div>
    </div>
  </div>
</template>

<style scoped>
.toc { padding: 6px 6px 32px; height: 100%; overflow-y: auto; overflow-x: hidden; box-sizing: border-box; }
.toc-head { display: flex; align-items: center; gap: 4px; padding: 3px 6px; }
.toc-item {
  display: flex; align-items: flex-start; gap: 3px;
  padding: 4px 6px; border-radius: var(--radius-sm); cursor: pointer;
  color: var(--text-dim); min-width: 0;
}
.toc-item:hover { background: var(--bg-hover); }
.toc-item.active { background: var(--accent-soft); }
.toc-item.active .txt .en { color: var(--accent-text); }
.toc-item .txt { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 1px; }
.toc-item .en { font-size: 12.5px; font-weight: 500; }
.toc-item .zh { font-size: 11.5px; color: var(--text-faint); }
.toc-item .num { font-family: var(--mono); font-size: 11px; color: var(--text-faint); margin-right: 4px; }
.toc-item .pg { font-size: 10.5px; color: var(--text-faint); font-variant-numeric: tabular-nums; margin-left: 4px; }
.caret {
  border: 0; background: transparent; cursor: pointer; color: var(--text-faint);
  padding: 1px 0 0; line-height: 1; width: 15px; display: inline-flex; justify-content: center;
}
.caret:hover { color: var(--text); }
.caret-space { display: inline-block; width: 15px; flex: none; }
.depth-0 { padding-left: 6px; }
.depth-1 { padding-left: 15px; }
.depth-2 { padding-left: 26px; }
.depth-3 { padding-left: 37px; }
.depth-4 { padding-left: 48px; }
.hits { margin-top: 10px; border-top: 1px solid var(--divider); padding-top: 6px; }
</style>
