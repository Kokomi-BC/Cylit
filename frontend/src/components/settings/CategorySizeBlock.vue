<script setup lang="ts">
/**
 * 设置 · 显示主题 · **分类字号**：五类字号各调各的。
 *
 * 2026-12 用户口径：撤掉「整体缩放」与「字体」两段，只留分类字号，并**并进「预览效果」卡**
 * （左侧调、右侧当场看，不必上下对照）。所以这里只出控件，不带卡片外壳与卡头 ——
 * 外壳由 DisplayPreviewCard 统一提供，避免"卡里套卡"。
 *
 * 与原来一样**没有「保存」**：点一下就往 :root 落 CSS 变量并写 localStorage，页面当场重排。
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

/** 分类的说明要短到能跟在名字后面：这一屏是"调数值"，不是读文档。 */
const CATEGORY_META: Record<DisplayCategory, { label: string; hint: string }> = {
  body: { label: '正文', hint: '论文与笔记' },
  chat: { label: '对话', hint: '问答气泡' },
  note: { label: '笔记', hint: '批注与高亮' },
  side: { label: '侧栏', hint: '目录与面板' },
  ui: { label: '界面', hint: '按钮与表格' },
};

const categories = DISPLAY_CATEGORIES.map((key) => ({ key, ...CATEGORY_META[key] }));

/** 浮动值留一点余量，避免 0.85 被浮点误差判成"还能再减"。 */
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
  <div class="cats-block" data-category-sizes>
    <div class="cats-head">
      <span class="cats-title">分类字号</span>
      <span class="cats-desc">不同内容各调各的</span>
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
  </div>
</template>

<style scoped>
.cats-block { display: flex; flex-direction: column; gap: 8px; min-width: 0; }
.cats-head { display: flex; align-items: baseline; gap: 6px; min-width: 0; }
.cats-title { flex: none; font-size: calc(12.5px * var(--ts, 1)); font-weight: 650; }
.cats-desc {
  min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  font-size: calc(11px * var(--ts, 1)); color: var(--text-faint);
}

/* 五类：一列排（预览卡左栏本来就窄，两列会把步进器挤到 100px 以下） */
.cats { display: flex; flex-direction: column; gap: 2px; }
/* 两列：名字 30 + 步进器吃掉剩下的宽度。步进器**铺满整列**（不是 auto 宽），
   五行的「−」与「+」才落在同一对竖线上 —— 之前按内容定宽 + 空着一列 auto，
   每行宽度各不相同，看着就是"按钮错位"。 */
.cat {
  display: grid; grid-template-columns: 30px minmax(0, 1fr);
  align-items: center; gap: 8px; min-height: 26px;
}
.cat-name { font-size: calc(12px * var(--ts, 1)); font-weight: 600; }

/* 步进器：一个 pill 里「− 值 +」，值用等宽数字，按下时不会左右抖 */
.step {
  display: flex; align-items: center; width: 100%; height: 26px;
  border: 1px solid var(--border); border-radius: var(--radius-ctl);
  background: var(--bg-elev); overflow: hidden;
}
.step-btn {
  flex: none; width: 24px; height: 100%; border: 0; background: transparent;
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
