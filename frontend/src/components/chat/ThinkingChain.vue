<script setup lang="ts">
/**
 * ThinkingChain —— 助手的「思考 + 工具调用」过程块。
 *
 * 行为与参数照抄参考实现（dsh web 的 ReasoningRow / flow motion）：
 * - 收起态只有一行 24px：`思考` + 一行摘要 + 步数 + 箭头，摘要右端 48px 渐隐（流式时改裁切）；
 * - 展开/收起是 **height 200ms ease-out**（不是 max-height，也不是 scale —— 后者会把文字压扁）；
 * - 跑的时候自动展开，一轮结束后延迟 700ms 自己塌回去（历史消息永远按收起渲染）；
 * - 运行中那一行：左侧 spinner 850ms linear 转，行上再叠一条 1.5s / 延迟 300ms 的高光扫过，
 *   扫过只走前 2/3（`-100% → 100%`），后 1/3 停住 —— 这样节奏比匀速扫更像「在等」。
 */
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import AppIcon from '@/components/AppIcon.vue';
import type { ChainStep } from './types';

const props = withDefaults(defineProps<{
  steps: ChainStep[];
  running?: boolean;
  /** 收起时露在「思考」右边的那一行摘要（通常是最新一段思考的首行）。 */
  preview?: string;
}>(), { running: false, preview: '' });

const open = ref(props.running);
const bodyEl = ref<HTMLElement | null>(null);
const bodyHeight = ref('0px');
const bodyOpacity = ref(0);
let settleTimer = 0;
let heightFallback = 0;

const doneCount = computed(() => props.steps.filter((s) => s.status !== 'running').length);
const summary = computed(() => props.preview.replace(/\*\*/g, '').trim());

function clearTimers(): void {
  if (settleTimer) { window.clearTimeout(settleTimer); settleTimer = 0; }
  if (heightFallback) { window.clearTimeout(heightFallback); heightFallback = 0; }
}

/** 收起时立刻停掉「跑完后的延迟塌陷」，避免手点开又被定时器关回去。 */
function toggle(): void {
  clearTimers();
  open.value = !open.value;
}

async function animate(next: boolean): Promise<void> {
  const el = bodyEl.value;
  if (!el) return;
  clearTimers();
  if (next) {
    const target = el.scrollHeight;
    bodyHeight.value = '0px';
    bodyOpacity.value = 0;
    await nextTick();
    void el.offsetHeight;            // 先让 0px 落地，transition 才有起点
    bodyHeight.value = `${target}px`;
    bodyOpacity.value = 1;
    // 到了目标高度就交还给 auto：内容还在流式增长时不能让固定 px 把字裁掉
    const settle = () => { if (open.value) bodyHeight.value = 'auto'; };
    el.addEventListener('transitionend', settle, { once: true });
    heightFallback = window.setTimeout(settle, 260);
  } else {
    bodyHeight.value = `${el.getBoundingClientRect().height}px`;
    await nextTick();
    void el.offsetHeight;
    bodyHeight.value = '0px';
    bodyOpacity.value = 0;
  }
}

watch(open, (v) => { void animate(v); });

watch(() => props.running, (now) => {
  if (now) {
    clearTimers();
    open.value = true;
    return;
  }
  clearTimers();
  settleTimer = window.setTimeout(() => { open.value = false; }, 700);
});

onMounted(() => {
  // 挂载时就是展开态（正在跑的轮次）：直接给 auto，别先播一遍展开动画
  if (open.value) { bodyHeight.value = 'auto'; bodyOpacity.value = 1; }
});

onBeforeUnmount(clearTimers);
</script>

<template>
  <div class="chain" :class="{ open, live: running }">
    <button class="chain-head" type="button" :aria-expanded="open" @click="toggle">
      <span class="chain-ico"><AppIcon name="sparkle" :size="14" /></span>
      <span class="chain-title">思考</span>
      <template v-if="!open && summary">
        <span class="chain-sep" aria-hidden="true" />
        <span class="chain-preview" :data-streaming="running || undefined">{{ summary }}</span>
      </template>
      <span class="chain-count">{{ doneCount }}/{{ steps.length }}</span>
      <AppIcon class="chain-chevron" name="chevron-down" :size="12" />
    </button>

    <div
      ref="bodyEl"
      class="chain-body"
      :style="{ height: bodyHeight, opacity: String(bodyOpacity) }"
    >
      <ul class="chain-steps">
        <li
          v-for="s in steps"
          :key="s.id"
          class="step"
          :data-kind="s.kind"
          :data-state="s.status"
        >
          <span class="step-mark">
            <svg v-if="s.status === 'running'" class="step-spin" viewBox="0 0 16 16" aria-hidden="true">
              <circle cx="8" cy="8" r="6.2" />
            </svg>
            <AppIcon v-else-if="s.status === 'bad'" name="error" :size="11" />
            <AppIcon v-else name="check" :size="11" />
          </span>
          <span class="step-tag">{{ s.label }}</span>
          <span v-if="s.text" class="step-text">{{ s.text }}</span>
          <span v-if="s.ms" class="step-ms">{{ s.ms }}s</span>
          <span v-if="s.status === 'running'" class="step-sweep" aria-hidden="true" />
        </li>
      </ul>
      <slot />
    </div>
  </div>
</template>

<style scoped>
.chain { display: flex; flex-direction: column; min-width: 0; }

.chain-head {
  display: flex;
  align-items: center;
  gap: 6px;
  width: 100%;
  min-width: 0;
  height: 24px;
  padding: 0;
  border: 0;
  background: transparent;
  color: var(--text-dim);
  font-family: inherit;
  font-size: calc(13px * var(--ts, 1));
  line-height: 24px;
  text-align: left;
  cursor: pointer;
}
.chain-head:hover { color: var(--text); }
/* 展开后抬头钉在顶部：长思考往下滚时「思考 / 收起」这一行不能跟着跑掉 */
.chain.open .chain-head {
  position: sticky;
  top: 0;
  z-index: 1;
  background: var(--bg);
}
.chain-ico { display: inline-flex; flex: none; }
.chain-title { flex: none; }
.chain-sep {
  flex: none;
  width: 2px;
  height: 2px;
  margin: 0 8px;
  border-radius: 1px;
  background: var(--text-faint);
}
/* 收起态的摘要：单行、右端 48px 渐隐 —— 让「还有下文」这件事看得出来，又不抢标题 */
.chain-preview {
  flex: 1 1 auto;
  min-width: 0;
  overflow: hidden;
  color: var(--text-faint);
  font-size: calc(12.5px * var(--ts, 1));
  white-space: nowrap;
  text-overflow: ellipsis;
  mask-image: linear-gradient(90deg, #000 calc(100% - 48px), transparent);
}
.chain-preview[data-streaming] { text-overflow: clip; }
.chain-count {
  margin-left: auto;
  flex: none;
  padding-left: 8px;
  color: var(--text-faint);
  font-size: calc(11px * var(--ts, 1));
  font-variant-numeric: tabular-nums;
}
.chain-chevron {
  flex: none;
  transition: transform .15s var(--ease);
}
.chain.open .chain-chevron { transform: rotate(180deg); }

/* height 过渡而不是 max-height：max-height 的插值单位是「上限」，内容越长动画越快，节奏会乱 */
.chain-body {
  overflow: hidden;
  transition: height .2s ease-out, opacity .2s ease-out;
}
.chain-steps {
  margin: 2px 0 0;
  padding: 0 0 0 20px;
  list-style: none;
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.step {
  position: relative;
  display: flex;
  align-items: flex-start;
  gap: 6px;
  min-width: 0;
  padding: 1px 2px;
  border-radius: 4px;
  color: var(--text-faint);
  font-size: calc(12px * var(--ts, 1));
  line-height: 1.6;
  overflow: hidden;
}
.step-mark {
  flex: none;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 12px;
  height: 18px;
  color: var(--text-faint);
}
.step[data-state="ok"] .step-mark { color: var(--accent-text); }
.step[data-state="bad"] .step-mark { color: var(--danger-text, #c0392b); }
.step-tag { flex: none; color: var(--text-dim); }
.step[data-state="running"] .step-tag { color: var(--accent-text); }
.step[data-kind="think"] .step-text {
  font-family: var(--mono);
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
.step-ms {
  flex: none;
  margin-left: auto;
  padding-left: 8px;
  font-family: var(--mono);
  font-size: calc(10.5px * var(--ts, 1));
  font-variant-numeric: tabular-nums;
}

/* 运行中：850ms 一圈的细圆环（和参考实现的 ongoing-spin 同速） */
.step-spin {
  width: 11px;
  height: 11px;
  animation: step-spin .85s linear infinite;
}
.step-spin circle {
  fill: none;
  stroke: currentColor;
  stroke-width: 1.6;
  stroke-linecap: round;
  stroke-dasharray: 3.2 6.1 3.2 6.1 3.2 6.1 3.2 6.1 3.2 6.1;
}
@keyframes step-spin { to { transform: rotate(360deg); } }

/* 高光扫过：1.5s 一轮、延迟 .3s，前 2/3 走完，后 1/3 停住 */
.step-sweep {
  position: absolute;
  inset: 0;
  pointer-events: none;
  background: linear-gradient(
    90deg,
    transparent 0%,
    transparent 22%,
    color-mix(in srgb, var(--text) 7%, transparent) 48%,
    transparent 74%,
    transparent 100%
  );
  transform: translateX(-100%);
  animation: step-sweep 1.5s linear .3s infinite;
}
@keyframes step-sweep {
  0% { transform: translateX(-100%); }
  66.667%, 100% { transform: translateX(100%); }
}

/* 跑着的时候标题也带一层流动高光（同一条时间轴，视觉上是一个节拍） */
.chain.live .chain-title {
  background-image: linear-gradient(
    90deg,
    var(--text-dim) 0%,
    var(--text-dim) 38%,
    var(--text) 50%,
    var(--text-dim) 62%,
    var(--text-dim) 100%
  );
  background-size: 220% 100%;
  -webkit-background-clip: text;
  background-clip: text;
  -webkit-text-fill-color: transparent;
  animation: chain-sheen 1.5s linear infinite;
}
@keyframes chain-sheen {
  0% { background-position: 150% 0; }
  100% { background-position: -50% 0; }
}
</style>
