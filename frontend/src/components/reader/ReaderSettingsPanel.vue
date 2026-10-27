<script setup lang="ts">
/**
 * 阅读器左栏「设置」页签的**前三段**（字号 / 高亮效果 / 重新生成解读）。
 *
 * 为什么搬这几样进阅读器：它们是"读的时候随时想改"的一类，去设置页改完再回来翻页，
 * 阅读节奏就断了；其余设置留在设置页，左栏只放常用的。第四段「任务」仍归 ReaderSidePanel
 * —— 那套清单直接吃它自己的 props（tasks / cancel-task），搬过来只会复制一份状态。
 *
 * 字号只做**紧凑版式**：侧栏只有 300 余像素宽，settings/CategorySizeBlock 那一行是
 * "名称 + 样例 + 胶囊 + 字体下拉"四列，塞进来会挤爆；这里一行 = 类别名 + 三档分段。
 * 类别名本身用该类令牌渲染（看到多少就是多少），所以不需要再做一行样例。
 * 字体族下拉刻意不进来（仍留在设置页）：那是"换字体"，不是"改多大"。
 */
import { computed } from 'vue';
import HighlightStyleCard from '@/components/settings/HighlightStyleCard.vue';
import AppIcon from '@/components/AppIcon.vue';
import {
  DISPLAY_CATEGORIES, displayScale, setCategory, type DisplayCategory,
} from '@/stores/display';

const emit = defineEmits<{
  /** 重新生成解读：二次确认与真正的请求都在 ReaderView（那里才有 paperId 与 toast） */
  (e: 'regenerate-insight'): void;
}>();

const CATEGORY_LABELS: Record<DisplayCategory, string> = {
  body: '正文', chat: '对话', note: '笔记', side: '侧栏', ui: '界面',
};
const categories = DISPLAY_CATEGORIES.map((key) => ({ key, label: CATEGORY_LABELS[key] }));

/** 三档：与设置页同一组（小 90% / 中 100% / 大 115%），换地方不改口径 */
const STEPS: ReadonlyArray<{ value: number; label: string }> = [
  { value: 0.9, label: '小' },
  { value: 1, label: '中' },
  { value: 1.15, label: '大' },
];

/** 当前值落在哪一档：取最近的 —— 存量里的 0.85 / 1.25 也有选中项，不会三档全空 */
function nearest(scale: number): number {
  return STEPS.reduce(
    (best, s) => (Math.abs(s.value - scale) < Math.abs(best - scale) ? s.value : best),
    STEPS[1].value,
  );
}

/** 五类不一致时「全部」那一排显示「自定义」（只作状态，点不动） */
const CUSTOM = 0;

const bulkState = computed<number>(() => {
  const first = nearest(displayScale[DISPLAY_CATEGORIES[0]]);
  return DISPLAY_CATEGORIES.every((key) => nearest(displayScale[key]) === first) ? first : CUSTOM;
});

function applyBulk(value: number): void {
  for (const key of DISPLAY_CATEGORIES) setCategory(key, value);
}

/** 类别名的字号 = 该类令牌（与正文/侧栏真正用的那一份同一个变量） */
function nameSize(key: DisplayCategory): string {
  return `calc(13px * var(--ts-${key}))`;
}
</script>

<template>
  <section class="reader-settings" data-reader-settings>
    <!-- 一、字号 -->
    <div class="block-head">
      <h3>字号</h3>
      <p>五类文字各自一档；「全部」一次改完</p>
    </div>

    <div class="bulk" data-bulk-row>
      <span class="bulk-label">全部</span>
      <div class="seg" role="group" aria-label="全部字号">
        <button
          v-for="s in STEPS"
          :key="s.value"
          type="button"
          class="seg-btn"
          :class="{ on: bulkState === s.value }"
          :aria-pressed="bulkState === s.value"
          :data-bulk-option="s.label"
          @click="applyBulk(s.value)"
        >{{ s.label }}</button>
        <span class="seg-btn is-state" :class="{ on: bulkState === CUSTOM }" aria-hidden="true">自定义</span>
      </div>
    </div>

    <div class="cats">
      <div v-for="c in categories" :key="c.key" class="cat-row">
        <span class="cat-name" :style="{ fontSize: nameSize(c.key) }">{{ c.label }}</span>
        <div class="seg sm" role="group" :aria-label="`${c.label}字号`" :data-size-row="c.key">
          <button
            v-for="s in STEPS"
            :key="s.value"
            type="button"
            class="seg-btn"
            :class="{ on: nearest(displayScale[c.key]) === s.value }"
            :aria-pressed="nearest(displayScale[c.key]) === s.value"
            :data-size-option="`${c.key}:${s.label}`"
            @click="setCategory(c.key, s.value)"
          >{{ s.label }}</button>
        </div>
      </div>
    </div>

    <!-- 二、高亮效果：直接复用设置页那张卡（它只读写 appearance 的 localStorage，没有 props） -->
    <HighlightStyleCard class="hl-block" />

    <!-- 三、重新生成解读：重跑是一次全篇模型调用，所以确认框在 ReaderView 里（与「⋯」同一处口径） -->
    <div class="regen">
      <button
        class="btn sm regen-btn"
        title="让模型重读全文，重新生成这篇论文的解读"
        data-regenerate-insight
        @click="emit('regenerate-insight')"
      >
        <AppIcon name="refresh" :size="13" />
        重新生成解读
      </button>
      <p class="regen-hint">会重跑一次全篇模型调用（确认后才开始）</p>
    </div>
  </section>
</template>

<style scoped>
.reader-settings { display: grid; gap: 14px; min-width: 0; }

.block-head { display: grid; gap: 1px; min-width: 0; }
.block-head h3 {
  margin: 0; font-size: calc(14px * var(--ts, 1)); font-weight: 650;
  letter-spacing: -.01em; line-height: 1.3;
}
.block-head p { margin: 0; font-size: calc(11.5px * var(--ts, 1)); line-height: 1.4; color: var(--text-faint); }

/* 「全部」一排：左标签 + 右分段；与下面五行的分段**同一套画法**，好认 */
.bulk { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.bulk-label { font-size: calc(12px * var(--ts, 1)); color: var(--text-dim); }
.cats { display: grid; gap: 4px; }

/* 一行 = 类别名 + 三档分段（窄栏里只有这两列放得下；样例由类别名自己充当） */
.cat-row {
  display: flex; align-items: center; justify-content: space-between; gap: 8px;
  min-height: 28px;
}
.cat-name {
  /* 字号由模板内联给（该类令牌）；这里只管不换行、不被压扁 */
  flex: none; line-height: 1.2; color: var(--text); font-weight: 600;
}

/* 分段控件：底槽 + 选中的一格抬起成白底（与设置页胶囊同一套语义，尺寸收小一档） */
.seg {
  display: inline-flex; align-items: center; gap: 2px; padding: 2px;
  border-radius: 999px; background: var(--bg-subtle, var(--bg-sunken));
  border: 1px solid var(--divider); flex: none;
}
.seg.sm { padding: 1px; }
.seg-btn {
  display: inline-flex; align-items: center; justify-content: center;
  min-width: 30px; height: 22px; padding: 0 8px;
  border: 0; border-radius: 999px; background: transparent;
  color: var(--text-dim); font-family: inherit;
  font-size: calc(11.5px * var(--ts, 1)); line-height: 1;
  cursor: pointer; white-space: nowrap;
}
.seg.sm .seg-btn { min-width: 26px; height: 20px; padding: 0 7px; }
.seg-btn:hover { color: var(--text); }
.seg-btn.on {
  background: var(--bg-raised, var(--bg-layer)); color: var(--text); font-weight: 650;
  box-shadow: 0 1px 2px rgb(0 0 0 / 8%);
}
/* 「自定义」是状态不是选项：五类不一致时它亮着，点了没反应 */
/* 挂了指示块的托盘里，选中项**自己不再铺底**：那枚抬起的白块是滑过来的共用指示块
   （`useSegGlide` 自动插入的 `.glide-thumb.seg-glide`，材质见 base.css）。
   写在组件内 = 权重高过上面那条 `.seg-btn.on`，不受样式加载顺序影响。 */
.seg.has-glide .seg-btn.on { background: transparent; box-shadow: none; }
.seg-btn.is-state { cursor: default; }
.seg-btn.is-state:hover { color: var(--text-dim); }

.hl-block { padding-top: 12px; border-top: 1px solid var(--divider); }

.regen { display: grid; gap: 4px; justify-items: start; padding-top: 12px; border-top: 1px solid var(--divider); }
.regen-btn { display: inline-flex; align-items: center; gap: 4px; }
.regen-hint { margin: 0; font-size: calc(11px * var(--ts, 1)); color: var(--text-faint); line-height: 1.4; }
</style>
