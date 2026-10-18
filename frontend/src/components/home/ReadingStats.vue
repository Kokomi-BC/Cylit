<script setup lang="ts">
/**
 * 首页底部的「阅读统计」卡：**一张折线图**，没有指标数字行。
 *
 * 用户口径 2026-10-15 第六轮：「底部的阅读统计改为折线图，列数字太 ai 感了」——
 * 所以「累计 / 日均 / 有记录」那三枚数字连同它下面的口径说明**整段删掉**了：数字只活在
 * 折线图的悬停气泡里（指哪天给哪天），卡面上除了区间切换就只有图。
 *
 * 数据来源（**只有一处**）：`utils/readerStats.ts` 那本"按天"的账 —— 它由阅读器里同一个
 * 30 秒计时器落盘（`addReaderSeconds`），口径与"每篇论文的累计时长"完全相同。
 * 这里**不做第二套统计**：没有新接口、没有新计时、也不碰后端。
 *
 * 已知边界（详见 `utils/readerStats.ts` 的注释）：后端没有阅读时长字段，数据是本机的，
 * 换浏览器 / 清缓存就没了；因此"某天没有记录"= 0（不是缺失值）。
 *
 * 空状态：**不画假曲线**，但也不留一片白 —— 保留一组非常淡的网格当"这里是一张趋势图"的
 * 暗示，中间一句话说明它以后会长什么样。
 */
import { computed, onMounted, onUnmounted, ref } from 'vue';
import AppIcon from '@/components/AppIcon.vue';
import { fmtReadDuration, readDailySeries, type DailyRead } from '@/utils/readerStats';

/** 区间：7 / 30 / 90 天（默认 7 —— 首页上先给"最近"的节奏） */
const RANGES = [
  { days: 7, label: '7 天' },
  { days: 30, label: '30 天' },
  { days: 90, label: '90 天' },
];
const days = ref(7);

const series = computed<DailyRead[]>(() => readDailySeries(days.value));
const hasData = computed(() => series.value.some((d) => d.seconds > 0));
const todaySeconds = computed(() => series.value[series.value.length - 1]?.seconds ?? 0);

/* ------------------------------- 数值文案 ------------------------------- */
const dateLabel = (iso: string) => `${Number(iso.slice(5, 7))} 月 ${Number(iso.slice(8, 10))} 日`;
const axisLabel = (iso: string) => `${Number(iso.slice(5, 7))}/${Number(iso.slice(8, 10))}`;

/* ------------------------------- 折线图几何 -------------------------------
 * 宽按容器实测（ResizeObserver）：SVG 用 1:1 的 px 坐标系，线宽才能到处都锐利。
 * 高度：桌面 200px（图是这张卡唯一的主角，给足）；空状态 150px（别留一大块白）；
 * 窄屏统一收到 140px。 */
const wrap = ref<HTMLElement | null>(null);
const width = ref(720);
let ro: ResizeObserver | null = null;
const height = computed(() => (width.value < 560 ? 140 : hasData.value ? 200 : 150));
const PAD = { l: 44, r: 14, t: 14, b: 26 };
const plotW = computed(() => Math.max(60, width.value - PAD.l - PAD.r));
const plotH = computed(() => height.value - PAD.t - PAD.b);

/** 纵轴刻度：3 段，步长取整（1/2/5/10/15/… 分钟），读数好看；空状态给一组固定的示意刻度 */
const scale = computed(() => {
  const maxMin = Math.max(...series.value.map((d) => d.seconds / 60), 0);
  if (maxMin <= 0) return { step: 10, max: 30 };
  const steps = [1, 2, 5, 10, 15, 20, 30, 45, 60, 90, 120, 180, 300, 600];
  const step = steps.find((s) => s * 3 >= maxMin) ?? Math.ceil(maxMin / 3 / 300) * 300;
  return { step, max: step * 3 };
});
const xAt = (i: number) => PAD.l + (series.value.length <= 1 ? plotW.value / 2 : (i * plotW.value) / (series.value.length - 1));
const yAtMinutes = (min: number) => PAD.t + plotH.value - (Math.min(min, scale.value.max) / scale.value.max) * plotH.value;
const yAt = (sec: number) => yAtMinutes(sec / 60);
const points = computed(() => series.value.map((d, i) => `${xAt(i).toFixed(1)},${yAt(d.seconds).toFixed(1)}`).join(' '));
const areaPoints = computed(() => {
  const base = (PAD.t + plotH.value).toFixed(1);
  return `${PAD.l},${base} ${points.value} ${xAt(series.value.length - 1).toFixed(1)},${base}`;
});
const gridLines = computed(() => [0, 1, 2, 3].map((k) => ({ value: k * scale.value.step, y: yAtMinutes(k * scale.value.step) })));
/** 横轴刻度密度：7 天每天都标，30 天每 5 天，90 天每 15 天（窄屏再稀一档） */
const labelStep = computed(() => {
  if (days.value <= 7) return width.value < 420 ? 2 : 1;
  if (days.value <= 30) return width.value < 420 ? 10 : 5;
  return width.value < 420 ? 30 : 15;
});

/* ------------------------------- 悬停读数 ------------------------------- */
const hover = ref<number | null>(null);
function onMove(ev: PointerEvent) {
  const el = wrap.value;
  if (!el || !hasData.value) return;
  const px = ev.clientX - el.getBoundingClientRect().left;
  const idx = Math.round(((px - PAD.l) / plotW.value) * (series.value.length - 1));
  hover.value = Math.min(series.value.length - 1, Math.max(0, idx));
}
const hoverItem = computed(() => (hover.value === null ? null : series.value[hover.value]));
const hoverX = computed(() => (hover.value === null ? 0 : xAt(hover.value)));
/** tooltip 贴着卡片内边距夹住，免得在两端溢出 */
const tipLeft = computed(() => {
  const w = 116;
  const x = hover.value === null ? 0 : xAt(hover.value);
  return Math.min(Math.max(x, w / 2 + 4), Math.max(w / 2 + 4, width.value - w / 2 - 4));
});

onMounted(() => {
  const el = wrap.value;
  if (el && typeof ResizeObserver !== 'undefined') {
    ro = new ResizeObserver(() => { width.value = Math.max(260, Math.round(el.clientWidth)); });
    ro.observe(el);
    width.value = Math.max(260, Math.round(el.clientWidth));
  }
});
onUnmounted(() => ro?.disconnect());
</script>

<template>
  <section class="stats" aria-labelledby="readstats-title">
    <header class="head">
      <div>
        <h2 id="readstats-title">阅读统计</h2>
        <p>记录阅读节奏，了解自己的阅读习惯。</p>
      </div>
      <div class="seg" role="tablist" aria-label="时间范围">
        <button
          v-for="r in RANGES"
          :key="r.days"
          type="button"
          role="tab"
          :aria-selected="days === r.days"
          :class="{ active: days === r.days }"
          @click="days = r.days"
        >{{ r.label }}</button>
      </div>
    </header>

    <div ref="wrap" class="chart-wrap" :class="{ empty: !hasData }">
      <!-- 网格**一直画**：空状态时它就是"这里是一张趋势图"的唯一暗示（很淡，不喧宾夺主） -->
      <svg
        class="chart" :width="width" :height="height" role="img"
        :aria-label="hasData ? `最近 ${days} 天阅读时长折线图` : '阅读时长折线图（暂无数据）'"
        @pointermove="onMove" @pointerleave="hover = null"
      >
        <g class="grid">
          <template v-for="g in gridLines" :key="g.value">
            <line :x1="PAD.l" :x2="PAD.l + plotW" :y1="g.y" :y2="g.y" />
            <text :x="PAD.l - 9" :y="g.y + 3.5" text-anchor="end">{{ g.value }}</text>
          </template>
        </g>

        <template v-if="hasData">
          <polygon class="area" :points="areaPoints" />
          <polyline class="line" :points="points" />
          <g class="axis">
            <text
              v-for="(d, i) in series"
              v-show="i % labelStep === 0 || i === series.length - 1"
              :key="d.date" :x="xAt(i)" :y="height - 7" text-anchor="middle"
            >{{ axisLabel(d.date) }}</text>
          </g>
          <g v-if="hoverItem" class="cursor">
            <line :x1="hoverX" :x2="hoverX" :y1="PAD.t" :y2="PAD.t + plotH" />
            <circle :cx="hoverX" :cy="yAt(hoverItem.seconds)" r="3.6" />
          </g>
        </template>
      </svg>

      <div v-if="!hasData" class="empty-note">
        <AppIcon name="gauge" :size="17" />
        <span>开始阅读后，这里会显示你的阅读时长变化</span>
      </div>

      <div v-if="hoverItem" class="tip" :style="{ left: `${tipLeft}px` }">
        <span class="tip-date">{{ dateLabel(hoverItem.date) }}</span>
        <span class="tip-val">{{ fmtReadDuration(hoverItem.seconds) }}</span>
      </div>
    </div>

    <p v-if="hasData && todaySeconds > 0" class="today-note">
      今天 · {{ fmtReadDuration(todaySeconds) }}（仍在累计）
    </p>
  </section>
</template>

<style scoped>
/* 与首页其它卡片同一套语言：白底、浅灰描边、弱阴影、同一个圆角与内边距 */
.stats {
  margin-top: 34px; padding: 18px 20px 14px;
  border: 1px solid var(--border); border-radius: var(--radius-sm);
  background: var(--bg-elev); box-shadow: var(--shadow-1);
}
.head { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; }
.head h2 { margin: 0; font-size: calc(14px * var(--ts, 1)); font-weight: 650; letter-spacing: .01em; }
.head p { margin: 5px 0 0; font-size: calc(11.5px * var(--ts, 1)); color: var(--text-faint); }

/* 图上留一点气：抬头与折线之间不贴死 */
.chart-wrap { position: relative; width: 100%; margin-top: 10px; }
.chart { display: block; width: 100%; overflow: visible; touch-action: pan-y; }
/* 参考线 / 网格 / 坐标轴一律**极浅灰**，图表里只有折线是彩色的 */
.grid line { stroke: var(--divider); stroke-width: 1; }
.grid text, .axis text { fill: var(--text-faint); font-size: calc(10px * var(--ts, 1)); font-family: var(--sans); }
.chart-wrap.empty .grid line { stroke: color-mix(in srgb, var(--divider) 70%, transparent); }
.area { fill: color-mix(in srgb, var(--accent) 9%, transparent); }
.line { fill: none; stroke: var(--accent); stroke-width: 1.8; stroke-linejoin: round; stroke-linecap: round; }
.cursor line { stroke: var(--border-strong); stroke-width: 1; stroke-dasharray: 3 3; }
.cursor circle { fill: var(--bg-elev); stroke: var(--accent); stroke-width: 2; }

/* 空状态：**居中一行**（图标 + 一句话），不是"中间一枚图标、下面几行小字"那种大留白 */
.empty-note {
  position: absolute; inset: 0; display: flex; align-items: center; justify-content: center;
  gap: 8px; font-size: calc(12.5px * var(--ts, 1)); color: var(--text-faint); pointer-events: none;
}
.empty-note :deep(svg) { flex: none; opacity: .9; }

.tip {
  position: absolute; top: 0; transform: translateX(-50%);
  display: flex; flex-direction: column; gap: 1px; padding: 5px 9px;
  border: 1px solid var(--border); border-radius: var(--radius-ctl);
  background: var(--bg-elev); box-shadow: var(--shadow-2); pointer-events: none; white-space: nowrap;
}
.tip-date { font-size: calc(10px * var(--ts, 1)); color: var(--text-faint); }
.tip-val { font-size: calc(12px * var(--ts, 1)); font-weight: 600; color: var(--text); }
.today-note { margin: 4px 0 0; text-align: right; font-size: calc(10.5px * var(--ts, 1)); color: var(--text-faint); }

@media (max-width: 640px) {
  .stats { margin-top: 26px; padding: 16px 14px 12px; }
  .head { flex-direction: column; gap: 10px; }
}
</style>
