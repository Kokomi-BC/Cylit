<script setup lang="ts">
/** 侧栏开关（胶囊形，仿 Apple 的 sidebar 切换）。
 *
 * 两处复用同一个组件：**侧栏收起时**它挂在顶栏的正文左边缘；**侧栏展开时**它嵌在侧栏自己的
 * 头部（就是用户口径的"展开后移进侧栏内部"）。两处都用 `v-if` 互斥渲染，所以 DOM 里
 * 永远只有一枚 `[data-aside-toggle]`（自检脚本按这个属性点它）。
 * 尺寸由父级给（`.cap-solo` / `.aside-head` 里都是一个 38px 高的胶囊）。
 */

import AppIcon from '@/components/AppIcon.vue';

defineProps<{ open: boolean }>();
const emit = defineEmits<{ (e: 'toggle'): void }>();
</script>

<template>
  <button
    type="button"
    class="aside-toggle"
    data-aside-toggle
    :title="open ? '收起侧栏（[）' : '展开侧栏（[）'"
    :aria-pressed="open"
    :aria-label="open ? '收起侧栏' : '展开侧栏'"
    @click="emit('toggle')"
  >
    <AppIcon :name="open ? 'sidebar' : 'sidebar-off'" :size="17" />
  </button>
</template>

<style scoped>
.aside-toggle {
  display: inline-flex; align-items: center; justify-content: center;
  width: 100%; height: 100%; padding: 0;
  border-radius: 999px; cursor: pointer;
  color: var(--text-dim);
  transition: background var(--dur) var(--ease), color var(--dur) var(--ease),
              transform var(--dur-fast) var(--ease);
}
.aside-toggle:hover { background: var(--bg-hover); color: var(--text); }
.aside-toggle:active { transform: scale(.94); }
.aside-toggle[aria-pressed="true"] { color: var(--accent-text); }
</style>
