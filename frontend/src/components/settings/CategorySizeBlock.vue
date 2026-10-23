<script setup lang="ts">
/**
 * 设置 · 显示主题 · **分类字号**（裸块，放在「预览效果」卡下面）：
 *  - 每一类一行：名字 + **该类字号的真实样例** + 小 / 中 / 大 三档；
 *  - 块底一行「字体」：系统默认 / 微软雅黑 / 宋体衬线（写 `data-typo`，全站生效）。
 *
 * 样例不是装饰：字号取自该类的 CSS 变量（`--ts-<类>` = 整体 × 该类），所以点「大」当场变大 ——
 * 与阅读器里那类文字用的是同一个令牌，看到多少就是多少。
 *
 * 三档而不是 ±5% 步进（用户口径 2026-12）：调字号是"选一个大概"，不是微调刻度；
 * 小 90% / 中 100% / 大 115% 已经覆盖常用区间。老用户存的 0.85/1.25 这类值仍生效，
 * 只是三档高亮取最近的那一档。
 */
import {
  DISPLAY_CATEGORIES,
  displayScale,
  setCategory,
  type DisplayCategory,
} from '@/stores/display';
import {
  TYPOGRAPHY_OPTIONS,
  appearance,
  setTypography,
  type Typography,
} from '@/stores/appearance';

/** 样例文字：每类一句自己的话，读起来就知道调的是哪一块 */
const CATEGORY_META: Record<DisplayCategory, { label: string; sample: string }> = {
  body: { label: '正文', sample: '论文正文与笔记的阅读字' },
  chat: { label: '对话', sample: '问答气泡里的这句话' },
  note: { label: '笔记', sample: '批注与高亮上的说明' },
  side: { label: '侧栏', sample: '目录、插图与面板' },
  ui: { label: '界面', sample: '按钮、表格与表单' },
};

const categories = DISPLAY_CATEGORIES.map((key) => ({ key, ...CATEGORY_META[key] }));

/** 三档 */
const STEPS: ReadonlyArray<{ value: number; label: string }> = [
  { value: 0.9, label: '小' },
  { value: 1, label: '中' },
  { value: 1.15, label: '大' },
];

/** 当前值落在哪一档：取最近的（存量里的 0.85 / 1.25 也有高亮，不会一个都不亮） */
function nearest(scale: number): number {
  return STEPS.reduce(
    (best, s) => (Math.abs(s.value - scale) < Math.abs(best - scale) ? s.value : best),
    STEPS[1].value,
  );
}

/** 样例的实时字号：读该类的令牌（与阅读器同一份），三档一点就变 */
function sampleSize(key: DisplayCategory): string {
  return `calc(13px * var(--ts-${key}))`;
}
</script>

<template>
  <section class="cats-block" data-category-sizes>
    <div class="block-head">
      <h3>分类字号</h3>
      <p>每类各挑一档，右边就是它真实的样子</p>
    </div>

    <div class="cats">
      <div v-for="c in categories" :key="c.key" class="cat">
        <span class="cat-name">{{ c.label }}</span>
        <span class="cat-sample" :style="{ fontSize: sampleSize(c.key) }">{{ c.sample }}</span>
        <div class="steps" role="group" :aria-label="c.label + '字号'">
          <button
            v-for="s in STEPS"
            :key="s.value"
            type="button"
            class="step"
            :class="{ on: nearest(displayScale[c.key]) === s.value }"
            :aria-pressed="nearest(displayScale[c.key]) === s.value"
            :data-size-option="c.key + ':' + s.label"
            @click="setCategory(c.key, s.value)"
          >{{ s.label }}</button>
        </div>
      </div>
    </div>

    <div class="font-row">
      <span class="cat-name">字体</span>
      <div class="steps wide" role="group" aria-label="字体">
        <button
          v-for="o in TYPOGRAPHY_OPTIONS"
          :key="o.value"
          type="button"
          class="step"
          :class="{ on: appearance.typography === o.value }"
          :aria-pressed="appearance.typography === o.value"
          :data-font-option="o.value"
          @click="setTypography(o.value as Typography)"
        >{{ o.label }}</button>
      </div>
    </div>
  </section>
</template>

<style scoped>
/* 块头与其他裸块（主题预设 / 高亮效果）同一套 */
.block-head { display: grid; gap: 1px; margin-bottom: 8px; min-width: 0; }
.block-head h3 {
  margin: 0; font-size: calc(14px * var(--ts, 1)); font-weight: 650;
  letter-spacing: -.01em; line-height: 1.3;
}
.block-head p { margin: 0; font-size: calc(11.5px * var(--ts, 1)); line-height: 1.4; color: var(--text-faint); }

.cats-block { min-width: 0; }
.cats { display: flex; flex-direction: column; gap: 2px; }
/* 一行：名字（定宽）+ 样例（吃满）+ 三档（定宽） */
.cat {
  display: grid; grid-template-columns: 34px minmax(0, 1fr) auto;
  align-items: center; gap: 8px; min-height: 30px;
}
.cat-name { font-size: calc(12px * var(--ts, 1)); font-weight: 600; }
/* 样例：**吃该类的字号令牌**，行高按字号走，不会被裁 */
.cat-sample {
  min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  line-height: 1.35; color: var(--text-dim);
}

/* 三档分段控件：一个 pill，选中的那档抬高成白底 */
.steps {
  display: inline-flex; align-items: center; flex: none; gap: 2px; padding: 2px;
  border: 1px solid var(--border); border-radius: var(--radius-ctl);
  background: var(--bg-sunken);
}
.step {
  display: inline-flex; align-items: center; justify-content: center;
  min-width: 34px; height: 22px; padding: 0 8px;
  border: 0; border-radius: 6px; background: transparent;
  color: var(--text-dim); font-family: inherit; font-size: calc(11.5px * var(--ts, 1));
  cursor: pointer; transition: background var(--dur-fast) var(--ease), color var(--dur-fast) var(--ease);
}
.step:hover { background: var(--bg-hover); color: var(--text); }
.step.on { background: var(--bg-elev); color: var(--accent-text); font-weight: 650; box-shadow: var(--shadow-1); }
.step:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; }

/* 字体一行：名字对齐上面的「正文/对话…」，右边三档更宽（三个中文字） */
.font-row {
  display: grid; grid-template-columns: 34px minmax(0, 1fr); align-items: center; gap: 8px;
  margin-top: 10px; padding-top: 10px; border-top: 1px solid var(--divider);
}
.steps.wide { justify-self: start; }
.steps.wide .step { padding: 0 10px; }
</style>
