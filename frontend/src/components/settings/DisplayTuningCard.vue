<script setup lang="ts">
/**
 * 设置 · 显示主题 · **显示与排版**：缩放 / 分类字号 / 字体，三段装在同一张卡里，
 * 只靠竖分隔线分区 —— 三个设置项各开一张卡，会多出两条卡缝和三个卡头，
 * 一屏就看不下主题与预览了。
 *
 * 都**没有「保存」**：点一下就往 `:root` 落 CSS 变量 + localStorage，页面当场重排。
 */
import { computed } from 'vue';
import {
  CATEGORY_MAX,
  CATEGORY_MIN,
  DISPLAY_CATEGORIES,
  MASTER_MAX,
  MASTER_MIN,
  SCALE_STEP,
  displayScale,
  setCategory,
  setScale,
  type DisplayCategory,
} from '@/stores/display';
import {
  TYPOGRAPHY_OPTIONS,
  appearance,
  setTypography,
  type Typography,
} from '@/stores/appearance';

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

function stepMaster(dir: number) {
  setScale(displayScale.master + dir * SCALE_STEP);
}

function stepCategory(key: DisplayCategory, dir: number) {
  setCategory(key, displayScale[key] + dir * SCALE_STEP);
}

/** 分类那一列的高度：五行字的高度之和，用来把三段的下沿对齐（不用固定高度） */
const categoriesChanged = computed(
  () => DISPLAY_CATEGORIES.some((key) => displayScale[key] !== 1),
);

function onTypography(ev: Event) {
  setTypography((ev.target as HTMLSelectElement).value as Typography);
}
</script>

<template>
  <section class="card tuning">
    <div class="card-head">
      <div class="hd">
        <h3>显示与排版</h3>
        <p>缩放、分类字号与字体，改完立即生效</p>
      </div>
    </div>

    <div class="card-body tuning-body">
      <!-- ① 整体缩放 -->
      <div class="sec">
        <div class="sec-head">
          <span class="sec-title">整体缩放</span>
          <span class="sec-desc">五类字号一起乘这个倍数</span>
        </div>
        <div class="zoom">
          <div class="step" role="group" aria-label="整体缩放">
            <button
              type="button"
              class="step-btn"
              :disabled="!canDown(displayScale.master, MASTER_MIN)"
              aria-label="整体缩放减小 5%"
              @click="stepMaster(-1)"
            >−</button>
            <span class="step-val" data-zoom>{{ pct(displayScale.master) }}</span>
            <button
              type="button"
              class="step-btn"
              :disabled="!canUp(displayScale.master, MASTER_MAX)"
              aria-label="整体缩放增加 5%"
              @click="stepMaster(1)"
            >+</button>
          </div>
          <button
            type="button"
            class="link-btn"
            :disabled="displayScale.master === 1"
            @click="setScale(1)"
          >恢复 100%</button>
        </div>
        <p class="note">范围 85%–125%，步进 5%</p>
      </div>

      <!-- ② 分类字号 -->
      <div class="sec">
        <div class="sec-head">
          <span class="sec-title">分类字号</span>
          <span class="sec-desc">不同内容各调各的</span>
        </div>
        <div class="cats">
          <div v-for="c in categories" :key="c.key" class="cat">
            <span class="cat-name">{{ c.label }}</span>
            <span class="cat-hint truncate">{{ c.hint }}</span>
            <div class="step sm" role="group" :aria-label="c.label + '字号'">
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
        <p class="note" v-if="categoriesChanged">分类只在"整体"之上再乘一次</p>
        <p class="note" v-else>范围 85%–150%，步进 5%</p>
      </div>

      <!-- ③ 字体 -->
      <div class="sec">
        <div class="sec-head">
          <span class="sec-title">字体</span>
          <span class="sec-desc">正文与标题的字面栈</span>
        </div>
        <label class="font-row">
          <span class="font-label">字体族</span>
          <select
            class="sel"
            :value="appearance.typography"
            data-font-select
            aria-label="字体族"
            @change="onTypography"
          >
            <option v-for="o in TYPOGRAPHY_OPTIONS" :key="o.value" :value="o.value">{{ o.label }}</option>
          </select>
        </label>
        <div class="sample" aria-hidden="true">
          <span class="sample-big">论文问答</span>
          <span class="sample-small">Aa Bb 0123 · 阅读与笔记</span>
        </div>
        <p class="note">代码块与论文原文始终保持等宽 / 衬线</p>
      </div>
    </div>
  </section>
</template>

<style scoped>
.card-head { padding: 11px 14px 9px; }
.hd { display: grid; gap: 2px; min-width: 0; }
.hd h3 {
  margin: 0; font-size: calc(14px * var(--ts, 1)); font-weight: 650;
  letter-spacing: -.01em; line-height: 1.3;
}
.hd p { margin: 0; font-size: calc(11.5px * var(--ts, 1)); line-height: 1.4; color: var(--text-faint); }

.tuning { min-width: 0; }
.tuning-body {
  display: grid; gap: 0;
  grid-template-columns: minmax(0, .9fr) minmax(0, 1.15fr) minmax(0, .95fr);
  padding: 0;
}
/* 三段之间只画一条竖线：不加卡、不加外边距，缝就是 1px */
.sec { min-width: 0; padding: 10px 14px 10px; }
.sec + .sec { border-left: 1px solid var(--divider); }

.sec-head { display: grid; gap: 1px; margin-bottom: 8px; }
.sec-title { font-size: calc(12.5px * var(--ts, 1)); font-weight: 650; }
.sec-desc { font-size: calc(11px * var(--ts, 1)); color: var(--text-faint); }

/* 步进器：一个 pill 里「− 值 +」，值用等宽数字，按下时不会左右抖 */
.step {
  display: inline-flex; align-items: center; height: 30px;
  border: 1px solid var(--border); border-radius: var(--radius-ctl);
  background: var(--bg-elev); overflow: hidden;
}
.step.sm { height: 26px; }
.step-btn {
  width: 30px; height: 100%; border: 0; background: transparent;
  color: var(--text-dim); font-family: inherit; font-size: calc(14px * var(--ts, 1));
  line-height: 1; cursor: pointer;
  transition: background var(--dur) var(--ease), color var(--dur) var(--ease);
}
.step.sm .step-btn { width: 24px; font-size: calc(13px * var(--ts, 1)); }
.step-btn:hover:not(:disabled) { background: var(--bg-hover); color: var(--text); }
.step-btn:focus-visible { outline: none; box-shadow: var(--ring); }
.step-btn:disabled { opacity: .35; cursor: not-allowed; }
.step-val {
  display: inline-flex; align-items: center; justify-content: center;
  min-width: 52px; align-self: stretch; padding: 0 2px;
  border-left: 1px solid var(--divider); border-right: 1px solid var(--divider);
  font-size: calc(12.5px * var(--ts, 1)); font-weight: 650;
  font-variant-numeric: tabular-nums;
}
.step.sm .step-val { min-width: 44px; font-size: calc(11.5px * var(--ts, 1)); }

.zoom { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.link-btn {
  border: 0; background: transparent; padding: 0 2px; cursor: pointer;
  color: var(--accent-text); font-family: inherit; font-size: calc(11.5px * var(--ts, 1));
}
.link-btn:disabled { color: var(--text-faint); cursor: default; }
.link-btn:focus-visible { outline: none; box-shadow: var(--ring); border-radius: 6px; }

/* 五类字号：两列排（三行），一行 26px。一列排五行会把这张卡顶高 50px，
   而右边那列（字体）本来就只有两行控件 —— 拉平高度靠分栏，不靠留白。 */
.cats { display: grid; gap: 1px 10px; grid-template-columns: repeat(2, minmax(0, 1fr)); }
.cat {
  display: grid; grid-template-columns: 30px minmax(0, 1fr) auto;
  align-items: center; gap: 6px; min-height: 24px;
}
/* 两列之后每格只剩 200 出头：那句提示要么被省略号切一半、要么把步进挤走，索性不显示 */
.cat-hint { display: none; }
.cat-name { font-size: calc(12px * var(--ts, 1)); font-weight: 600; }
.cat-hint { font-size: calc(11px * var(--ts, 1)); color: var(--text-faint); }

.font-row { display: grid; grid-template-columns: 42px minmax(0, 1fr); align-items: center; gap: 8px; }
.font-label { font-size: calc(12px * var(--ts, 1)); font-weight: 600; }
.sel {
  height: 30px; width: 100%; padding: 0 8px;
  border: 1px solid var(--border); border-radius: var(--r-sm);
  background: var(--bg-elev); color: var(--text);
  font-family: inherit; font-size: calc(12px * var(--ts, 1));
}
.sel:focus { outline: none; border-color: var(--accent); box-shadow: var(--ring); }

/* 字体样例：两行字用 **当前字体栈** 渲出来，选完立刻能看到差别 */
.sample {
  display: grid; gap: 2px; margin-top: 8px;
  padding: 8px 10px; border-radius: var(--r-sm);
  background: var(--bg-sunken); border: 1px solid var(--border);
}
.sample-big { font-family: var(--display); font-size: calc(15px * var(--ts, 1)); font-weight: 650; line-height: 1.35; }
.sample-small { font-size: calc(11.5px * var(--ts, 1)); color: var(--text-dim); }

.note { margin: 8px 0 0; font-size: calc(11px * var(--ts, 1)); line-height: 1.5; color: var(--text-faint); }

/* 窄一档：三段折成两列（字体落到下一行整宽） */
@container (max-width: 820px) {
  .tuning-body { grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); }
  .sec:nth-child(3) { grid-column: 1 / -1; border-left: 0; border-top: 1px solid var(--divider); }
}
/* 更窄：一段一行，竖线换成横线 */
@container (max-width: 560px) {
  .tuning-body { grid-template-columns: minmax(0, 1fr); }
  .sec + .sec, .sec:nth-child(3) { border-left: 0; border-top: 1px solid var(--divider); }
}
</style>
