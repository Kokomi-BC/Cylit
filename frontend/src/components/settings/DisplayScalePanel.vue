<script setup lang="ts">
/** 设置 · 显示大小：整体缩放 + 五类字号。
 *
 * 和上面的主题面板一样**没有「保存」按钮**：点一下就把倍数写进 CSS 变量 + localStorage，
 * 页面当场重排。这里只负责"用户按了多少"，换算与持久化都在 `stores/display`。
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
  resetScale,
  setCategory,
  setScale,
  type DisplayCategory,
} from '@/stores/display';

/** 分类的说明要短到一行：这一屏是"调数值"，不是读文档。 */
const CATEGORY_META: Record<DisplayCategory, { label: string; desc: string }> = {
  body: { label: '正文', desc: '论文与笔记的阅读字号' },
  chat: { label: '对话', desc: '问答气泡与会话记录' },
  note: { label: '笔记', desc: '批注、摘记与高亮' },
  side: { label: '侧栏', desc: '目录、大纲与侧边面板' },
  ui: { label: '界面', desc: '按钮、表格与表单文字' },
};

const categories = DISPLAY_CATEGORIES.map((key) => ({ key, ...CATEGORY_META[key] }));

const isDefault = computed(() =>
  displayScale.master === 1 && DISPLAY_CATEGORIES.every((key) => displayScale[key] === 1));

/** 浮动值留一点余量，避免 0.85 被浮点误差判成"还能再减"。 */
const canDown = (value: number, min: number) => value > min + 1e-6;
const canUp = (value: number, max: number) => value < max - 1e-6;

function pct(value: number): string {
  return `${Math.round(value * 100)}%`;
}

function stepMaster(dir: number): void {
  setScale(displayScale.master + dir * SCALE_STEP);
}

function stepCategory(key: DisplayCategory, dir: number): void {
  setCategory(key, displayScale[key] + dir * SCALE_STEP);
}

/** 预览只跟整体缩放，不跟分类：让用户先看到"整体"的幅度，再决定要不要拆分类。 */
const previewStyle = computed(() => ({ fontSize: `calc(15px * ${displayScale.master})` }));
</script>

<template>
  <div class="display-scale">
    <p class="panel-note">字太小就整体调大，五类字号跟着一起变。</p>

    <div class="grid ds-grid">
      <!-- 整体缩放：一个步进器 + 恢复默认，短卡 -->
      <div class="card">
        <div class="card-head">
          <span>整体缩放</span>
          <span class="ds-head-note">改完立即生效，只影响这台机器</span>
        </div>
        <div class="card-body">
          <p class="ds-desc">五类字号一起放大或缩小</p>
          <div class="ds-master">
            <div class="ds-stepper" role="group" aria-label="整体缩放">
              <button
                type="button"
                class="ds-step"
                :disabled="!canDown(displayScale.master, MASTER_MIN)"
                aria-label="整体缩放减小 5%"
                @click="stepMaster(-1)"
              >−</button>
              <span class="ds-value">{{ pct(displayScale.master) }}</span>
              <button
                type="button"
                class="ds-step"
                :disabled="!canUp(displayScale.master, MASTER_MAX)"
                aria-label="整体缩放增加 5%"
                @click="stepMaster(1)"
              >+</button>
            </div>
            <button type="button" class="btn sm ds-reset" :disabled="isDefault" @click="resetScale">恢复默认</button>
          </div>
        </div>
      </div>

      <!-- 预览：先看见再决定，短卡 -->
      <div class="card">
        <div class="card-head">预览</div>
        <div class="card-body">
          <div class="ds-preview">
            <p class="ds-preview-line" :style="previewStyle">
              量子纠缠把两个粒子的状态绑成一个整体，测量其中一个，另一个会立刻响应。
            </p>
          </div>
        </div>
      </div>

      <!-- 五个分类：一条一条的列表，整宽 -->
      <div class="card ds-card-wide">
        <div class="card-head">分类字号</div>
        <div class="card-body">
          <div class="ds-table">
            <div class="ds-row ds-head" aria-hidden="true">
              <span>分类</span>
              <span class="ds-num">调节</span>
            </div>

            <div v-for="c in categories" :key="c.key" class="ds-row">
              <div class="ds-meta">
                <span class="ds-name">{{ c.label }}</span>
                <span class="ds-desc">{{ c.desc }}</span>
              </div>

              <div class="ds-stepper" role="group" :aria-label="`${c.label}字号`">
                <button
                  type="button"
                  class="ds-step"
                  :disabled="!canDown(displayScale[c.key], CATEGORY_MIN)"
                  :aria-label="`${c.label}字号减小 5%`"
                  @click="stepCategory(c.key, -1)"
                >−</button>
                <span class="ds-value">{{ pct(displayScale[c.key]) }}</span>
                <button
                  type="button"
                  class="ds-step"
                  :disabled="!canUp(displayScale[c.key], CATEGORY_MAX)"
                  :aria-label="`${c.label}字号增加 5%`"
                  @click="stepCategory(c.key, 1)"
                >+</button>
              </div>
            </div>
          </div>

          <p class="ds-foot">范围：整体 85%–125%，分类 85%–150%，步进 5%。</p>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
/* 换肤面板在上、这里在下：两块面板之间没有 `.card + .card` 可用，自己留出同一档间距 */
.display-scale { display: flex; flex-direction: column; gap: 12px; margin-top: 14px; }

/* 顶部一句话：一行说完这一块能干什么，不解释做法 */
.panel-note {
  margin: 0; font-size: calc(12.5px * var(--ts, 1)); line-height: 1.5; color: var(--text-faint);
}

/* 卡片网格：短卡两列并排，长内容整宽。间距只由 `.grid` 的 gap 负责，卡片不再加 margin。 */
.ds-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); align-items: start; }
.ds-card-wide { grid-column: 1 / -1; }

/* 卡头右侧那句说明：弱于标题、独占一行时也不换字号（仍是同一套 --ts 级联） */
.ds-head-note {
  margin-left: auto;
  font-size: calc(11.5px * var(--ts, 1));
  font-weight: 400;
  color: var(--text-faint);
}

/* 行母版：左边说明自适应，中间步进固定，右边数值定宽 —— 三行对齐靠 grid 而不是空格 */
.ds-row {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  align-items: center;
  gap: 12px;
  padding: 11px 0;
}

/* 整体缩放：步进器与「恢复默认」同一行，窄了就换行 */
.ds-master { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin-top: 8px; }

.ds-meta { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
.ds-name { font-size: calc(13px * var(--ts, 1)); font-weight: 600; }
.ds-desc { margin: 0; font-size: calc(11.5px * var(--ts, 1)); color: var(--text-faint); }

/* 步进器：一个 pill 里放「− 值 +」，值用等宽数字，按下时不会左右抖 */
.ds-stepper {
  display: inline-flex;
  align-items: center;
  height: 28px;
  border: 1px solid var(--border);
  border-radius: var(--radius-ctl);
  background: var(--bg-elev);
  overflow: hidden;
}
.ds-step {
  width: 28px; height: 100%;
  border: 0; background: transparent;
  color: var(--text-dim);
  font-family: inherit;
  font-size: calc(14px * var(--ts, 1));
  line-height: 1;
  cursor: pointer;
  transition: background var(--dur) var(--ease), color var(--dur) var(--ease);
}
.ds-step:hover:not(:disabled) { background: var(--bg-hover); color: var(--text); }
.ds-step:active:not(:disabled) { background: var(--bg-pressed); }
.ds-step:focus-visible { outline: none; box-shadow: var(--ring); }
.ds-step:disabled { opacity: .35; cursor: not-allowed; }
.ds-value {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 48px;
  align-self: stretch;
  font-size: calc(12.5px * var(--ts, 1));
  font-weight: 600;
  font-variant-numeric: tabular-nums;
  border-left: 1px solid var(--divider);
  border-right: 1px solid var(--divider);
}

/* 预览：浅底一块，只负责"让你先看见"，不加边框以免和卡片抢层级 */
.ds-preview {
  display: flex;
  align-items: baseline;
  gap: 10px;
  padding: 10px 12px;
  border-radius: var(--r-sm);
  background: var(--bg-sunken);
}
.ds-preview-line { margin: 0; line-height: 1.65; color: var(--text); }

/* 分类表：只有细分隔线，不做第二层卡片 */
.ds-table { margin-top: 4px; border-top: 1px solid var(--divider); }
.ds-table .ds-row + .ds-row { border-top: 1px solid var(--divider); }
.ds-head {
  padding: 8px 0 6px;
  font-size: calc(10.5px * var(--ts, 1));
  font-weight: 600;
  letter-spacing: .08em;
  color: var(--text-faint);
}
.ds-head .ds-num { text-align: right; }
.ds-num { text-align: right; font-variant-numeric: tabular-nums; font-size: calc(12.5px * var(--ts, 1)); }

.ds-reset { flex: none; }

.ds-foot {
  margin: 10px 0 0;
  font-size: calc(11px * var(--ts, 1));
  color: var(--text-faint);
}

/* 窄一档：卡片回落单列，短卡也竖着排 */
@media (max-width: 1100px) {
  .ds-grid { grid-template-columns: minmax(0, 1fr); }
}

/* 更窄：步进与数值掉到第二行，说明占满整行，避免数字被挤到换行 */
@media (max-width: 560px) {
  .ds-row { grid-template-columns: minmax(0, 1fr) auto; }
  .ds-head { display: none; }
}
</style>
