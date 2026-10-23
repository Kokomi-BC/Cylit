<script setup lang="ts">
/**
 * 设置 · 显示主题 · **分类字号**（**不套卡片**：右栏三块共用一条竖轴，只靠留白与细线分区）。
 *
 * 一行是「名字在左、窄步进器在右」—— 步进器**定宽**（116），不随栏宽拉长：
 * 五行的 − / + 因此落在同一对竖线上，也不会在 420 宽的右栏里拖成一条长条。
 *
 * 与原来一样**没有「保存」**：点一下就往 :root 落 CSS 变量并写 localStorage。
 */
import {
  CATEGORY_MAX,
  CATEGORY_MIN,
  DISPLAY_CATEGORIES,
  SCALE_STEP,
  displayScale,
  setCategory,
  type DisplayCategory,
} from '@/stores/display';

const CATEGORY_META: Record<DisplayCategory, { label: string }> = {
  body: { label: '正文' },
  chat: { label: '对话' },
  note: { label: '笔记' },
  side: { label: '侧栏' },
  ui: { label: '界面' },
};

const categories = DISPLAY_CATEGORIES.map((key) => ({ key, ...CATEGORY_META[key] }));

/** 浮动值留一点余量，避免 0.85 被浮点误差判成还能再减。 */
const canDown = (value: number, min: number) => value > min + 1e-6;
const canUp = (value: number, max: number) => value < max - 1e-6;

function pct(value: number): string {
  return Math.round(value * 100) + '%';
}

function stepCategory(key: DisplayCategory, dir: number) {
  setCategory(key, displayScale[key] + dir * SCALE_STEP);
}
</script>

<template>
  <section class="cats-block" data-category-sizes>
    <div class="block-head">
      <h3>分类字号</h3>
      <p>不同内容各调各的</p>
    </div>

    <div class="cats">
      <div v-for="c in categories" :key="c.key" class="cat">
        <span class="cat-name">{{ c.label }}</span>
        <div class="step" role="group" :aria-label="c.label + '字号'">
          <button
            type="button"
            class="step-btn"
            :disabled="!canDown(displayScale[c.key], CATEGORY_MIN)"
            :aria-label="c.label + '字号减小 5%'"
            @click="stepCategory(c.key, -1)"
          >−</button>
          <span class="step-val">{{ pct(displayScale[c.key]) }}</span>
          <button
            type="button"
            class="step-btn"
            :disabled="!canUp(displayScale[c.key], CATEGORY_MAX)"
            :aria-label="c.label + '字号增加 5%'"
            @click="stepCategory(c.key, 1)"
          >+</button>
        </div>
      </div>
    </div>
  </section>
</template>

<style scoped>
/* 块头：与「主题预设」「高亮效果」同一套（裸块，不加卡片） */
.block-head { display: grid; gap: 1px; margin-bottom: 8px; min-width: 0; }
.block-head h3 {
  margin: 0; font-size: calc(14px * var(--ts, 1)); font-weight: 650;
  letter-spacing: -.01em; line-height: 1.3;
}
.block-head p { margin: 0; font-size: calc(11.5px * var(--ts, 1)); line-height: 1.4; color: var(--text-faint); }

.cats-block { min-width: 0; }
.cats { display: flex; flex-direction: column; gap: 2px; }
/* 一行：名字在左、步进器贴右；步进器定宽，五行才对齐 */
.cat {
  display: flex; align-items: center; justify-content: space-between;
  gap: 10px; min-height: 28px;
}
.cat-name { font-size: calc(12px * var(--ts, 1)); font-weight: 600; }

.step {
  display: flex; align-items: center; flex: none; width: 116px; height: 26px;
  border: 1px solid var(--border); border-radius: var(--radius-ctl);
  background: var(--bg-elev); overflow: hidden;
}
.step-btn {
  flex: none; width: 26px; height: 100%; border: 0; background: transparent;
  color: var(--text-dim); font-family: inherit; font-size: calc(13px * var(--ts, 1));
  line-height: 1; cursor: pointer;
  transition: background var(--dur) var(--ease), color var(--dur) var(--ease);
}
.step-btn:hover:not(:disabled) { background: var(--bg-hover); color: var(--text); }
.step-btn:focus-visible { outline: none; box-shadow: var(--ring); }
.step-btn:disabled { opacity: .35; cursor: not-allowed; }
.step-val {
  display: inline-flex; align-items: center; justify-content: center;
  flex: 1; min-width: 0; align-self: stretch; padding: 0 2px;
  border-left: 1px solid var(--divider); border-right: 1px solid var(--divider);
  font-size: calc(11.5px * var(--ts, 1)); font-weight: 650;
  font-variant-numeric: tabular-nums;
}
</style>
