<script setup lang="ts">
/**
 * 阅读器顶部的「批注胶囊」（毛玻璃）。
 *
 * 收纳：手 / 画笔 / 荧光笔 / 自动选择 / 橡皮 + 三档粗细 + 撤销重做 + 退出批注。
 * 与参考图的分工一致：**工具在顶部中间的胶囊里**，不在页面各处散开。
 * 撤销/重做只对"最后一次动笔的那一页"生效（阅读器是一整条长文，跨页共用一条栈说不清）。
 */
import { computed } from 'vue';
import AppIcon from '@/components/AppIcon.vue';
import { WIDTH_STEPS, type InkTool, type WidthKey } from '@/utils/imageInk';
import {
  canRedo, canUndo, inkPage, inkTool, inkWidth, redoInk, undoInk,
} from '@/composables/useReaderInk';

const emit = defineEmits<{ (e: 'exit'): void }>();

const TOOLS: Array<{ key: InkTool; icon: string; label: string }> = [
  { key: 'hand', icon: 'hand', label: '手：只读，不落笔（划词/点链接照常）' },
  { key: 'pen', icon: 'pen', label: '画笔' },
  { key: 'marker', icon: 'marker', label: '荧光笔' },
  { key: 'select', icon: 'text-select', label: '自动选择：点一行文字，再决定要不要加笔记' },
  { key: 'eraser', icon: 'eraser', label: '橡皮：整笔擦除' },
];

/** 只在"能落笔"的模式下给撤销/重做（用户口径：选择画笔模式时，支持前进和撤回） */
const drawMode = computed(() => inkTool.value === 'pen' || inkTool.value === 'marker' || inkTool.value === 'eraser');
const undoable = computed(() => canUndo(inkPage.value));
const redoable = computed(() => canRedo(inkPage.value));

function dotSize(key: WidthKey): number {
  return key === 'thin' ? 5 : key === 'mid' ? 7 : 9;
}
</script>

<template>
  <div class="ink-cap">
    <button
      v-for="item in TOOLS"
      :key="item.key"
      class="ibtn"
      :class="{ on: inkTool === item.key }"
      :title="item.label"
      :data-ink-tool="item.key"
      @click="inkTool = item.key"
    >
      <AppIcon :name="item.icon" :size="17" />
    </button>

    <span class="cap-sep" />

    <button
      v-for="step in WIDTH_STEPS"
      :key="step.key"
      class="wbtn"
      :class="{ on: inkWidth === step.key }"
      :title="'笔迹粗细：' + step.label"
      :data-ink-width="step.key"
      @click="inkWidth = step.key"
    >
      <i class="wdot" :style="{ width: dotSize(step.key) + 'px', height: dotSize(step.key) + 'px' }" />
    </button>

    <template v-if="drawMode">
      <span class="cap-sep" />
      <button class="ibtn" :disabled="!undoable" title="撤销（Ctrl+Z）" data-ink-undo @click="undoInk(inkPage)">
        <AppIcon name="undo" :size="17" />
      </button>
      <button class="ibtn" :disabled="!redoable" title="重做（Ctrl+Shift+Z）" data-ink-redo @click="redoInk(inkPage)">
        <AppIcon name="redo" :size="17" />
      </button>
    </template>

    <span class="cap-sep" />

    <button class="ibtn exit" title="退出批注（笔迹会保留）" data-ink-exit @click="emit('exit')">
      <AppIcon name="close" :size="16" />
    </button>
  </div>
</template>

<style scoped>
/* 与阅读器顶部那两块玻璃**同一套材质**（渐变本体 + 大模糊 + 柔光投影，不画描边），
   位置在下层居中：既不挡主胶囊，也仍然落在"顶部中间"。 */
.ink-cap {
  position: fixed; top: 64px; left: 50%; transform: translateX(-50%);
  z-index: 60; pointer-events: auto;
  display: flex; align-items: center; gap: 2px;
  padding: 5px 6px; border-radius: 999px;
  border: 0;
  background: linear-gradient(180deg,
    color-mix(in srgb, var(--bg-elev) var(--glass-bar-top, 80%), transparent),
    color-mix(in srgb, var(--bg-elev) var(--glass-bar-bottom, 68%), transparent));
  backdrop-filter: blur(48px) saturate(2.6);
  -webkit-backdrop-filter: blur(48px) saturate(2.6);
  box-shadow: var(--shadow-flyout);
  max-width: calc(100vw - 24px);
  overflow-x: auto; scrollbar-width: none;
}
.ink-cap::-webkit-scrollbar { display: none; }
.cap-sep { flex: none; width: 1px; height: 18px; margin: 0 4px; background: var(--border); }

.ibtn, .wbtn {
  flex: none; width: 32px; height: 32px; padding: 0; cursor: pointer;
  display: inline-flex; align-items: center; justify-content: center;
  border: 0; border-radius: 999px; background: transparent; color: var(--text-dim);
  transition: background var(--dur) var(--ease), color var(--dur) var(--ease);
}
.ibtn:hover, .wbtn:hover { background: var(--bg-hover); color: var(--text); }
.ibtn.on, .wbtn.on { background: var(--accent-soft, var(--bg-hover)); color: var(--accent); }
.ibtn:disabled { opacity: .32; cursor: default; background: transparent; }
.ibtn.exit:hover { background: var(--danger-soft); color: var(--danger); }
.wbtn { width: 26px; }
.wdot { display: block; border-radius: 999px; background: currentColor; }

@media (max-width: 640px) {
  .ink-cap { top: 58px; gap: 0; padding: 4px 5px; }
  .ibtn { width: 30px; height: 30px; }
  .wbtn { width: 24px; }
}
</style>
