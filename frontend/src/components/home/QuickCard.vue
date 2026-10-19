<!--
  首页"一行三张小卡"里的一张（笔记 / 设置 / AI 对话）。
  比功能卡轻一档：34px 圆形浅底图标 + 标题 + 一行小字 + 右侧 chevron，没有 CTA 文字。
  整卡可点（一个 <button>），chevron 只是"可以进去"的方向提示。
-->
<script setup lang="ts">
import AppIcon from '@/components/AppIcon.vue';

defineProps<{ icon: string; label: string; hint: string }>();
const emit = defineEmits<{ (e: 'open'): void }>();
</script>

<template>
  <button type="button" class="qcard" @click="emit('open')">
    <span class="ico"><AppIcon :name="icon" :size="17" /></span>
    <span class="text">
      <span class="title">{{ label }}</span>
      <span class="hint">{{ hint }}</span>
    </span>
    <AppIcon name="chevron-right" :size="15" class="chev" />
  </button>
</template>

<style scoped>
.qcard {
  display: flex; align-items: center; gap: 12px;
  width: 100%; height: 100%; padding: 14px 16px; margin: 0;
  border: 1px solid var(--border); border-radius: var(--radius-sm); background: var(--bg-elev);
  box-shadow: var(--shadow-1); color: var(--text);
  font: inherit; text-align: left; cursor: pointer; appearance: none;
  transition: transform var(--dur) var(--ease), box-shadow var(--dur) var(--ease),
              border-color var(--dur) var(--ease);
}
.qcard:hover { transform: translateY(-1px); box-shadow: var(--shadow-2); }
.qcard:focus-visible { outline: none; border-color: var(--accent); box-shadow: var(--ring); }

.ico {
  display: grid; place-items: center; flex: none; width: 34px; height: 34px;
  border-radius: 50%; background: var(--bg-sunken); color: var(--text-dim);
  transition: color var(--dur) var(--ease);
}
.qcard:hover .ico { color: var(--accent); }

.text { display: flex; flex-direction: column; gap: 3px; min-width: 0; flex: 1; }
.title { font-size: calc(15px * var(--ts, 1)); font-weight: 620; letter-spacing: -.015em; }
.hint {
  font-size: calc(12.5px * var(--ts, 1)); line-height: 1.5; color: var(--text-faint);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.chev { flex: none; color: var(--text-faint); transition: color var(--dur) var(--ease); }
.qcard:hover .chev { color: var(--accent); }
@media (prefers-reduced-motion: reduce) {
  .qcard, .qcard:hover { transform: none; }
}
</style>
