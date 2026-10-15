<script setup lang="ts">
/**
 * 首页底部的「阅读统计」卡：按天折线图 + 三枚指标 + 区间切换。
 *
 * 数据来源（**只有一处**）：`utils/readerStats.ts` 那本"按天"的账 —— 它由阅读器里同一个
 * 30 秒计时器落盘（`addReaderSeconds`），口径与"每篇论文的累计时长"完全相同。
 * 这里**不做第二套统计**：没有新接口、没有新计时、也不碰后端。
 *
 * 已知边界（详见 `utils/readerStats.ts` 的注释）：后端没有阅读时长字段，数据是本机的，
 * 换浏览器 / 清缓存就没了；因此"某天没有记录"= 0（不是缺失值）。
 */
import { computed, onMounted, onUnmounted, ref } from 'vue';
import AppIcon from '@/components/AppIcon.vue';
import { dayKey, readDailySeries, summarizeDaily, type DailyRead } from '@/utils/readerStats';

/** 区间：7 / 30 / 90 天（默认 7 —— 首页上先给"最近"的节奏） */
const RANGES = [
  { days: 7, label: '7 天' },
  { days: 30, label: '30 天' },
  { days: 90, label: '90 天' },
];
const days = ref(7);

const series = computed<DailyRead[]>(() => readDailySeries(days.value));
const summary = computed(() => summarizeDaily(series.value));
/** 日均口径固定：**区间总时长 ÷ 区间天数**（含没读的那几天）—— 卡片右下角写明了 */
const avgSeconds = computed(() => Math.round(summary.value.total / days.value));
const hasData = computed(() => summary.value.total > 0);
const todaySeconds = computed(() => series.value[series.value.length - 1]?.seconds ?? 0);

/* ------------------------------- 数值文案 ------------------------------- */
/** 轴与 tooltip 用的"分钟"（不足 1 分钟不显示成 0） */
function fmtMinutes(sec: number): string {
  if (sec <= 0) return '0 分钟';
  if (sec < 60) return '不到 1 分钟';
  const min = Math.round(sec / 60);
  if (min < 60) return `${min} 分钟`;
  const h = Math.floor(min / 60);
  const rest = min % 60;
  return rest ? `${h} 小时 ${rest} 分` : `${h} 小时`;
}
const dateLabel = (iso: string) => `${Number(iso.slice(5, 7))} 月 ${Number(iso.slice(8, 10))} 日`;
const axisLabel = (iso: string) => `${Number(iso.slice(5, 7))}/${Number(iso.slice(8, 10))}`;

/* ------------------------------- 折线图几何 -------------------------------
 * 宽按容器实测（ResizeObserver）：SVG 用 1:1 的 px 坐标系，线宽才能到处都锐利。
 * 高度桌面 190px、窄屏收到 140px（用户口径：移动端"适当缩小高度"）。 */
const wrap = ref<HTMLElement | null>(null);
const width = ref(720);
let ro: ResizeObserver | null = null;
const height = computed(() => (width.value < 560 ? 140 : 190));
const PAD = { l: 44, r: 14, t: 14, b: 28 };
const plotW = computed(() => Math.max(60, width.value - PAD.l - PAD.r));
const plotH = computed(() => height.value - PAD.t - PAD.b);

/** 纵轴刻度：3 段，步长取整（1/2/5/10/15/… 分钟），读数好看 */
const scale = computed(() => {
  const maxMin = Math.max(...series.value.map((d) => d.seconds / 60), 0);
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
  if (!el || !series.value.length) return;
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

    <div class="metrics">
      <div class="metric">
        <span class="k">累计阅读时长</span>
        <span class="v">{{ hasData ? fmtMinutes(summary.total) : '—' }}</span>
      </div>
      <div class="metric">
        <span class="k">日均阅读时长</span>
        <span class="v">{{ hasData ? fmtMinutes(avgSeconds) : '—' }}</span>
      </div>
      <div class="metric">
        <span class="k">有阅读记录</span>
        <span class="v">{{ hasData ? `${summary.activeDays} 天` : '—' }}</span>
      </div>
      <span class="spacer" />
      <span class="note">日均 = 区间总时长 ÷ {{ days }} 天</span>
    </div>

    <div ref="wrap" class="chart-wrap">
      <template v-if="hasData">
        <svg
          class="chart" :width="width" :height="height" role="img"
          :aria-label="`最近 ${days} 天阅读时长折线图`"
          @pointermove="onMove" @pointerleave="hover = null"
        >
          <!-- 极浅的水平网格线 + 纵轴读数（分钟） -->
          <g class="grid">
            <template v-for="g in gridLines" :key="g.value">
              <line :x1="PAD.l" :x2="PAD.l + plotW" :y1="g.y" :y2="g.y" />
              <text :x="PAD.l - 9" :y="g.y + 3.5" text-anchor="end">{{ g.value }}</text>
            </template>
          </g>
          <!-- 折线下方那层很淡的面积，只为了让线"落"在图上，不用渐变、不用发光 -->
          <polygon class="area" :points="areaPoints" />
          <polyline class="line" :points="points" />
          <!-- 横轴日期 -->
          <g class="axis">
            <text
              v-for="(d, i) in series"
              v-show="i % labelStep === 0 || i === series.length - 1"
              :key="d.date" :x="xAt(i)" :y="height - 8" text-anchor="middle"
            >{{ axisLabel(d.date) }}</text>
          </g>
          <!-- 悬停：一条竖参考线 + 一个点（默认不画数据点） -->
          <g v-if="hoverItem" class="cursor">
            <line :x1="hoverX" :x2="hoverX" :y1="PAD.t" :y2="PAD.t + plotH" />
            <circle :cx="hoverX" :cy="yAt(hoverItem.seconds)" r="3.6" />
          </g>
        </svg>
        <div v-if="hoverItem" class="tip" :style="{ left: `${tipLeft}px` }">
          <span class="tip-date">{{ dateLabel(hoverItem.date) }}</span>
          <span class="tip-val">{{ fmtMinutes(hoverItem.seconds) }}</span>
        </div>
        <div v-if="todaySeconds > 0" class="today-note">
          今天 · {{ fmtMinutes(todaySeconds) }}（仍在累计）
        </div>
      </template>

      <!-- 空状态：**不画假曲线**，直接把"这里以后会有什么"说清楚 -->
      <div v-else class="empty-state">
        <span class="es-icon"><AppIcon name="gauge" :size="20" /></span>
        <span class="es-title">还没有阅读记录</span>
        <span class="es-hint">
          在阅读器里停留的时长会按天记在这里（本机统计）。
          读过一篇之后回首页，就能看到自己的阅读节奏。
        </span>
      </div>
    </div>
  </section>
</template>

<style scoped>
/* 与首页其它卡片同一套语言：白底、极浅描边、弱阴影、同一个圆角与内边距 */
.stats {
  margin-top: 26px; padding: 18px 20px 14px;
  border: 1px solid var(--border); border-radius: var(--radius-sm);
  background: var(--bg-elev); box-shadow: var(--shadow-1);
}
.head { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; }
.head h2 { margin: 0; font-size: 14px; font-weight: 650; letter-spacing: -.02em; }
.head p { margin: 5px 0 0; font-size: 11.5px; color: var(--text-faint); }

.metrics { display: flex; align-items: flex-end; flex-wrap: wrap; gap: 26px; margin: 16px 0 6px; }
.metric { display: flex; flex-direction: column; gap: 3px; }
.metric .k { font-size: 11px; color: var(--text-faint); }
.metric .v { font-size: 17px; font-weight: 620; letter-spacing: -.02em; color: var(--text); }
.note { font-size: 10.5px; color: var(--text-faint); align-self: flex-end; padding-bottom: 2px; }

.chart-wrap { position: relative; width: 100%; }
.chart { display: block; width: 100%; overflow: visible; touch-action: pan-y; }
/* 参考线 / 网格 / 坐标轴一律**极浅灰**，图表里只有折线是彩色的 */
.grid line { stroke: var(--divider); stroke-width: 1; }
.grid text, .axis text { fill: var(--text-faint); font-size: 10px; font-family: var(--sans); }
.area { fill: color-mix(in srgb, var(--accent) 9%, transparent); }
.line { fill: none; stroke: var(--accent); stroke-width: 1.8; stroke-linejoin: round; stroke-linecap: round; }
.cursor line { stroke: var(--border-strong); stroke-width: 1; stroke-dasharray: 3 3; }
.cursor circle { fill: var(--bg-elev); stroke: var(--accent); stroke-width: 2; }

.tip {
  position: absolute; top: 0; transform: translateX(-50%);
  display: flex; flex-direction: column; gap: 1px; padding: 5px 9px;
  border: 1px solid var(--border); border-radius: var(--radius-ctl);
  background: var(--bg-elev); box-shadow: var(--shadow-2); pointer-events: none; white-space: nowrap;
}
.tip-date { font-size: 10px; color: var(--text-faint); }
.tip-val { font-size: 12px; font-weight: 600; color: var(--text); }
.today-note { margin-top: 2px; text-align: right; font-size: 10.5px; color: var(--text-faint); }

.empty-state { display: flex; flex-direction: column; align-items: center; gap: 7px; padding: 34px 18px 40px; text-align: center; }
.es-icon {
  display: grid; place-items: center; width: 38px; height: 38px; margin-bottom: 2px;
  border-radius: var(--radius-sm); background: var(--bg-sunken); color: var(--text-dim);
}
.es-title { font-size: 13px; font-weight: 600; color: var(--text); }
.es-hint { max-width: 44ch; font-size: 11.5px; line-height: 1.75; color: var(--text-faint); }

@media (max-width: 680px) {
  .stats { padding: 16px 14px 12px; }
  .head { flex-direction: column; gap: 10px; }
  .metrics { gap: 18px; margin-top: 14px; }
  .metric .v { font-size: 15px; }
  .note { width: 100%; align-self: flex-start; }
}
</style>
