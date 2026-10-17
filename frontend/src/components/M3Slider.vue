<script setup lang="ts">
/**
 * Material You 3 风格的滑块（本仓库唯一一处滑块：批注里"滑到底清空本页笔迹"）。
 *
 * 为什么不直接用 input[type=range]：原生控件的轨道/手柄由浏览器画，各平台长得都不一样，
 * 放进这枚玻璃胶囊里明显"不是同一套设计"。这里照 M3 的规格自己画：
 * 观感取 M3 的**状态层**语言（悬停/拖动时外圈亮一层半透明主色）+ 本仓库自己的尺寸口径：
 *   - 轨道 6px 细条（与 base.css 的 .bar 同一条），未激活段用"正文色 12%"，激活段用**主题色**；
 *   - 手柄是 18px 的圆钮（主题色描边 + 一档投影），按下/拖动放大到 20px —— 比 4px 竖条更像
 *     "能抓住的东西"，也不会在这套浅色玻璃里显得突兀；
 *   - 支持键盘（左右 / PgUp / Home / End）与 aria-valuenow，没丢可访问性。
 */
import { computed, ref } from 'vue';

const props = withDefaults(defineProps<{
  modelValue: number;
  min?: number;
  max?: number;
  step?: number;
  disabled?: boolean;
  /** 颜色语义：默认就是**主题色**；只有确实需要警示时才传 danger */
  tone?: 'accent' | 'danger';
}>(), { min: 0, max: 100, step: 1, disabled: false, tone: 'accent' });

const emit = defineEmits<{
  (e: 'update:modelValue', value: number): void;
  (e: 'change', value: number): void;
}>();

const root = ref<HTMLElement | null>(null);
const dragging = ref(false);

const ratio = computed(() => {
  const span = props.max - props.min;
  if (span <= 0) return 0;
  return Math.min(1, Math.max(0, (props.modelValue - props.min) / span));
});
const percent = computed(() => ratio.value * 100);
const activeWidth = computed(() => 'calc(' + percent.value + '% - 4px)');
const handleLeft = computed(() => percent.value + '%');

function setFromPointer(clientX: number): void {
  const el = root.value;
  if (!el || props.disabled) return;
  const rect = el.getBoundingClientRect();
  const r = Math.min(1, Math.max(0, (clientX - rect.left) / Math.max(1, rect.width)));
  const raw = props.min + r * (props.max - props.min);
  const snapped = Math.round(raw / props.step) * props.step;
  const value = Math.min(props.max, Math.max(props.min, snapped));
  if (value !== props.modelValue) emit('update:modelValue', value);
}

function onDown(ev: PointerEvent) {
  if (props.disabled) return;
  (ev.currentTarget as HTMLElement).setPointerCapture?.(ev.pointerId);
  dragging.value = true;
  setFromPointer(ev.clientX);
  ev.preventDefault();
}

function onMove(ev: PointerEvent) {
  if (!dragging.value) return;
  setFromPointer(ev.clientX);
}

function onUp(ev: PointerEvent) {
  if (!dragging.value) return;
  dragging.value = false;
  (ev.currentTarget as HTMLElement).releasePointerCapture?.(ev.pointerId);
  emit('change', props.modelValue);
}

function onKey(ev: KeyboardEvent) {
  if (props.disabled) return;
  const big = (props.max - props.min) / 10;
  let next: number | null = null;
  if (ev.key === 'ArrowLeft' || ev.key === 'ArrowDown') next = props.modelValue - props.step;
  else if (ev.key === 'ArrowRight' || ev.key === 'ArrowUp') next = props.modelValue + props.step;
  else if (ev.key === 'PageDown') next = props.modelValue - big;
  else if (ev.key === 'PageUp') next = props.modelValue + big;
  else if (ev.key === 'Home') next = props.min;
  else if (ev.key === 'End') next = props.max;
  if (next === null) return;
  ev.preventDefault();
  const value = Math.min(props.max, Math.max(props.min, Math.round(next / props.step) * props.step));
  emit('update:modelValue', value);
  emit('change', value);
}
</script>

<template>
  <div
    ref="root"
    class="m3s"
    :class="['tone-' + tone, { dragging, disabled }]"
    role="slider"
    :tabindex="disabled ? -1 : 0"
    :aria-valuemin="min"
    :aria-valuemax="max"
    :aria-valuenow="modelValue"
    :aria-disabled="disabled || undefined"
    @pointerdown="onDown"
    @pointermove="onMove"
    @pointerup="onUp"
    @pointercancel="onUp"
    @keydown="onKey"
  >
    <div class="m3s-track">
      <!-- 激活段：宽度按比例，右侧留 4px 空隙（M3 的停顿感） -->
      <div class="m3s-active" :style="{ width: activeWidth }" />
    </div>
    <div class="m3s-handle" :style="{ left: handleLeft }">
      <span class="m3s-state" />
      <span class="m3s-knob" />
    </div>
  </div>
</template>

<style scoped>
.m3s {
  position: relative; flex: 1; min-width: 110px; height: 32px;
  display: flex; align-items: center; cursor: pointer;
  touch-action: none; outline: none;
}
.m3s.disabled { cursor: default; opacity: .45; }

/* 轨道：6px 细条、全圆角（与 base.css 的进度条同一条），未激活段跟随正文色 */
.m3s-track {
  position: relative; width: 100%; height: 6px;
  border-radius: 999px; overflow: hidden;
  background: color-mix(in srgb, var(--text) 14%, transparent);
}
.m3s-active {
  position: absolute; left: 0; top: 0; bottom: 0;
  border-radius: 999px;
  transition: width 60ms linear;
}
.tone-accent .m3s-active { background: var(--accent); }
.tone-danger .m3s-active { background: var(--danger); }

/* 手柄：18px 圆钮（主题色描边 + 一档投影），外面罩一层 M3 状态圆 */
.m3s-handle {
  position: absolute; top: 50%; left: 0; transform: translate(-50%, -50%);
  display: flex; align-items: center; justify-content: center;
  pointer-events: none;
}
.m3s-knob {
  width: 18px; height: 18px; border-radius: 999px;
  background: var(--bg-elev);
  border: 2px solid var(--accent);
  box-shadow: var(--shadow-1);
  transition: transform 120ms var(--ease, ease), border-color 120ms var(--ease, ease);
}
.tone-danger .m3s-knob { border-color: var(--danger); }

.m3s-state {
  position: absolute; width: 34px; height: 34px; border-radius: 999px;
  opacity: 0; transition: opacity 120ms var(--ease, ease), transform 120ms var(--ease, ease);
  transform: scale(.7);
}
.tone-accent .m3s-state { background: color-mix(in srgb, var(--accent) 16%, transparent); }
.tone-danger .m3s-state { background: color-mix(in srgb, var(--danger) 16%, transparent); }

.m3s:hover .m3s-state, .m3s:focus-visible .m3s-state, .m3s.dragging .m3s-state {
  opacity: 1; transform: scale(1);
}
/* 悬停 / 拖动时圆钮放大（M3 的"抓住"反馈） */
.m3s:hover .m3s-knob, .m3s.dragging .m3s-knob { transform: scale(1.12); }
.m3s:focus-visible .m3s-track { box-shadow: var(--ring); }
</style>
