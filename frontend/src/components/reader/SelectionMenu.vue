<script setup lang="ts">
/** 划词浮动菜单：解释 / 笔记 / 复制 / 高亮（颜色）/ 关闭。 */
import AppIcon from '@/components/AppIcon.vue';

const props = defineProps<{
  visible: boolean;
  x: number;
  y: number;
  colors: string[];
}>();

const emit = defineEmits<{
  (e: 'explain'): void;
  (e: 'note'): void;
  (e: 'copy'): void;
  (e: 'highlight', color: string): void;
  (e: 'close'): void;
}>();

const COLOR_TITLE: Record<string, string> = {
  yellow: '黄色高亮', green: '绿色高亮', blue: '蓝色高亮',
  pink: '粉色高亮', purple: '中性高亮',
};
</script>

<template>
  <div
    v-if="props.visible"
    class="selection-menu"
    :style="{ left: `${props.x}px`, top: `${props.y}px` }"
    @mousedown.stop
  >
    <button @click="emit('explain')"><AppIcon name="sparkle" :size="14" />AI 解释</button>
    <button @click="emit('note')"><AppIcon name="note" :size="14" />笔记</button>
    <button @click="emit('copy')"><AppIcon name="copy" :size="14" />复制</button>
    <span class="row" style="padding:0 4px;gap:4px">
      <span
        v-for="c in props.colors"
        :key="c"
        :class="['swatch', `hl-${c}`]"
        :title="COLOR_TITLE[c] ?? c"
        @click="emit('highlight', c)"
      />
    </span>
    <button title="关闭" @click="emit('close')"><AppIcon name="close" :size="14" /></button>
  </div>
</template>

<style scoped>
.selection-menu {
  position: fixed; z-index: 120; background: var(--bg-elev); border: 1px solid var(--border-strong);
  border-radius: var(--radius); box-shadow: var(--shadow-2); padding: 4px; display: flex; gap: 2px;
}
.selection-menu button {
  border: 0; background: transparent; color: var(--text); padding: 5px 9px;
  border-radius: var(--radius-sm); cursor: pointer; font-family: inherit; font-size: 12.5px;
  display: inline-flex; align-items: center; gap: 6px;
}
.selection-menu button:hover { background: var(--bg-hover); }
.swatch { width: 15px; height: 15px; border-radius: 50%; border: 1px solid var(--border-strong); cursor: pointer; }
.hl-yellow { background: rgba(255, 214, 64, .6); }
.hl-green { background: rgba(90, 220, 150, .55); }
.hl-blue { background: rgba(110, 180, 255, .55); }
.hl-pink { background: rgba(255, 150, 190, .55); }
.hl-purple { background: rgba(140, 140, 140, .45); }
</style>
