<script setup lang="ts">
/**
 * Material You 3 风格的滑块（本仓库唯一一处滑块：批注里"滑到底清空本页笔迹"）。
 *
 * 为什么不直接用 input[type=range]：原生控件的轨道/手柄由浏览器画，各平台长得都不一样，
 * 放进这枚玻璃胶囊里明显"不是同一套设计"。这里照 M3 的规格自己画：
 *   - 轨道 16px 高、全圆角；**激活段与未激活段之间留 4px 空隙**（M3 的标志性细节）；
 *   - 手柄是 4px 宽、26px 高的圆角竖条（按下/拖动时拉长到 34px），外面罩一层状态圆；
 *   - 支持键盘（左右 / PgUp / Home / End）与 aria-valuenow，没丢可访问性。
 */
import { computed, ref } from 'vue';

const props = withDefaults(defineProps<{
  modelValue: number;
  min?: number;
  max?: number;
  step?: number;
  disabled?: boolean;
  /** 颜色语义：普通=主色，危险动作（清空笔迹）=错误色 */
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
      <span class="m3s-bar" />
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

/* 轨道：16px 高、全圆角；未激活段用"正文色 12%"（浅色/深色皮肤都自动跟随） */
.m3s-track {
  position: relative; width: 100%; height: 16px;
  border-radius: 999px; overflow: hidden;
  background: color-mix(in srgb, var(--text) 12%, transparent);
}
.m3s-active {
  position: absolute; left: 0; top: 0; bottom: 0;
  border-radius: 999px;
  transition: width 60ms linear;
}
.tone-accent .m3s-active { background: var(--accent); }
.tone-danger .m3s-active { background: var(--danger); }

/* 手柄：4px 的圆角竖条 + 外面的状态圆（悬停/拖动才显形） */
.m3s-handle {
  position: absolute; top: 50%; left: 0; transform: translate(-50%, -50%);
  display: flex; align-items: center; justify-content: center;
  pointer-events: none;
}
.m3s-bar {
  width: 4px; height: 26px; border-radius: 999px;
  transition: height 120ms var(--ease, ease);
}
.tone-accent .m3s-bar { background: var(--accent); }
.tone-danger .m3s-bar { background: var(--danger); }
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
/* 按下/拖动时手柄拉长（M3 的"抓住"反馈） */
.m3s.dragging .m3s-bar { height: 34px; }
.m3s:focus-visible .m3s-track { box-shadow: var(--ring); }
</style>
