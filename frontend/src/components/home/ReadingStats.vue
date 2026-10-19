<!--
  首页底部左列「阅读统计」卡。
  在多标签外壳里它只是首页的一块，不占标签、自己不发请求以外的事。

  数据口径（**只有一处**）：`GET /home/summary`（backend/services/home/home_service.py）。
  后端**没有**"阅读时长 / 日均时长 / 连续天数"这类字段 —— `reading_progress` 只存"读到哪"，
  `paper.last_read_at` 只有一个时间戳，`paper.read_time_min` 是元数据里**估**出来的建议时长。
  所以这里不编时长：三枚数字换成接口里真有的量（已解读篇数 / 笔记条数 / 标注处数），
  并把设计稿里的 7 / 30 / 90 天分段控件**整个去掉** —— 没有时间切片的数据就别给切片的入口。
  折线图用的是**本机**那本"按天读了多少秒"的账（utils/readerStats.ts 的 pm.reader.daily，
  用户口径 2026-11 要"工作区加折线图"）：服务端没有这条时间序列，所以图必须**明确写出"本机记录"**——
  它是这个浏览器里的真实计时，不是服务端口径，换设备就没有。没有本机记录时整块图不渲染。
  概览一个数字都没有（新库）或请求失败时整张卡不渲染，不留空壳。
-->
<script setup lang="ts">
import { computed, onMounted, ref } from 'vue';
import AppIcon from '@/components/AppIcon.vue';
import { api } from '@/api/client';
import { fmtReadDuration, readDailySeries, type DailyRead } from '@/utils/readerStats';
import type { HomeSummary } from '@/api/types';

const summary = ref<HomeSummary | null>(null);
/** 最近 7 天**本机**每日阅读秒数（含今天；没有记录的那天是 0，不是缺失值） */
const series = ref<DailyRead[]>([]);

onMounted(async () => {
  series.value = readDailySeries(7);
  try {
    summary.value = await api.get<HomeSummary>('/home/summary');
  } catch {
    summary.value = null;      // 首页不该因为一个统计数字弹错
  }
});

const metrics = computed(() => {
  const s = summary.value;
  if (!s) return [];
  return [
    { key: 'understandings', label: '已解读篇数', value: s.understandings, unit: '篇' },
    { key: 'notes', label: '笔记条数', value: s.notes, unit: '条' },
    { key: 'highlights', label: '标注处数', value: s.highlights, unit: '处' },
  ];
});

/** 三个数全是 0 等于"还没有任何阅读记录"，这块没有信息量 → 整张卡不渲染 */
const show = computed(() => metrics.value.some((m) => m.value > 0));

/* ───────────── 折线图：本机最近 7 天阅读时长 ─────────────
 * viewBox 固定 300×88、横向拉伸铺满卡片，配合 preserveAspectRatio="none" 与
 * vector-effect: non-scaling-stroke —— 线宽不会被横向拉粗。纵向按峰值归一；全是 0 时按
 * 1 分钟铺满，免得除零、也免得几分钟就画成顶到天花板的一条线。 */
const CHART_W = 300;
const CHART_H = 88;
const PAD_Y = 8;

const totalSeconds = computed(() => series.value.reduce((n, d) => n + d.seconds, 0));
const peakSeconds = computed(() => series.value.reduce((n, d) => Math.max(n, d.seconds), 0));
const hasChart = computed(() => totalSeconds.value > 0);

const points = computed(() => {
  const list = series.value;
  if (!list.length) return [];
  const max = Math.max(peakSeconds.value, 60);
  const stepX = CHART_W / Math.max(1, list.length - 1);
  return list.map((d, i) => ({
    date: d.date,
    seconds: d.seconds,
    x: i * stepX,
    y: CHART_H - PAD_Y - (d.seconds / max) * (CHART_H - PAD_Y * 2),
  }));
});
const linePath = computed(() => points.value
  .map((p, i) => (i ? "L" : "M") + p.x.toFixed(1) + " " + p.y.toFixed(1)).join(" "));
const areaPath = computed(() => {
  const ps = points.value;
  if (!ps.length) return "";
  const base = CHART_H - PAD_Y;
  const top = ps.map((p) => "L" + p.x.toFixed(1) + " " + p.y.toFixed(1)).join(" ");
  const lastX = ps[ps.length - 1].x.toFixed(1);
  return "M" + ps[0].x.toFixed(1) + " " + base + " " + top + " L" + lastX + " " + base + " Z";
});
/** 横轴只显示"月/日"：7 个时间点上去掉年份才不挤 */
const xLabels = computed(() => series.value.map((d) => d.date.slice(5).replace("-", "/")));
</script>

<template>
  <section v-if="show" class="stats" aria-labelledby="home-stats-title">
    <header class="head">
      <span class="ico"><AppIcon name="gauge" :size="17" /></span>
      <div class="htext">
        <h2 id="home-stats-title">阅读统计</h2>
        <p>记录阅读节奏，了解自己的阅读习惯。</p>
      </div>
    </header>
    <dl class="nums">
      <div v-for="m in metrics" :key="m.key" class="num">
        <dt>{{ m.label }}</dt>
        <dd>{{ m.value }}<em>{{ m.unit }}</em></dd>
      </div>
    </dl>

    <!-- 折线图：**本机**最近 7 天阅读时长。服务端没有这条时间序列，所以标题里写明"本机记录" ——
         它是这个浏览器里的真实计时，不是服务端口径。没有记录时整块不渲染（不画一条贴底的假线）。 -->
    <div v-if="hasChart" class="chart">
      <div class="chart-head">
        <span>最近 7 天阅读时长</span>
        <span class="chart-peak">峰值 {{ fmtReadDuration(peakSeconds) }} · 本机记录</span>
      </div>
      <svg
        class="spark"
        :viewBox="'0 0 ' + CHART_W + ' ' + CHART_H"
        preserveAspectRatio="none"
        role="img"
        :aria-label="'最近 7 天每日阅读时长，峰值 ' + fmtReadDuration(peakSeconds)"
      >
        <path class="spark-area" :d="areaPath" />
        <path class="spark-line" :d="linePath" />
        <circle v-for="p in points" :key="p.date" class="spark-dot" :cx="p.x" :cy="p.y" r="2.2">
          <title>{{ p.date }} · {{ fmtReadDuration(p.seconds) }}</title>
        </circle>
      </svg>
      <div class="x-axis" aria-hidden="true">
        <span v-for="l in xLabels" :key="l">{{ l }}</span>
      </div>
    </div>
    <!-- 本机还没有任何按天记录：只说清"为什么没有图"，不摆占位数字、也不画一条贴底的假线 -->
    <p v-else class="chart-empty">还没有本机阅读记录 —— 在阅读器里读一会儿，这里会画出最近 7 天。</p>
  </section>
</template>

<style scoped>
.stats {
  display: flex; flex-direction: column; gap: 16px;
  height: 100%; padding: 16px 18px 18px;
  border: 1px solid var(--border); border-radius: var(--radius-sm); background: var(--bg-elev);
  box-shadow: var(--shadow-1);
}
.head { display: flex; align-items: center; gap: 10px; }
.ico {
  display: grid; place-items: center; flex: none; width: 38px; height: 38px;
  border-radius: 11px; background: var(--bg-sunken); color: var(--text-dim);
}
.htext { min-width: 0; }
.htext h2 { margin: 0; font-size: calc(17px * var(--ts, 1)); font-weight: 620; letter-spacing: -.02em; }
.htext p { margin: 3px 0 0; font-size: calc(12.5px * var(--ts, 1)); line-height: 1.5; color: var(--text-faint); }

/* 三枚数字一排：卡宽只有 1.25fr，纵排会把卡撑得很高。
   下面有折线图之后它们不再需要靠 auto 边距居中 —— 图用 margin-top:auto 顶到卡底。 */
.nums { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; margin: 0; }
.num { min-width: 0; }
.num dt { font-size: calc(12px * var(--ts, 1)); color: var(--text-faint); }
.num dd {
  margin: 6px 0 0; font-size: calc(26px * var(--ts, 1)); font-weight: 600; line-height: 1.1;
  letter-spacing: -.03em; color: var(--text); font-variant-numeric: tabular-nums;
}
.num dd em {
  margin-left: 4px; font-size: calc(12px * var(--ts, 1)); font-weight: 400;
  font-style: normal; letter-spacing: 0; color: var(--text-faint);
}

/* ---- 折线图 ----
 * 卡片底部那块：横向铺满、纵向固定 88px。线宽用 non-scaling-stroke 顶住 ——
 * preserveAspectRatio=none 会把横向拉伸，普通描边会被一起拉粗。 */
.chart { display: flex; flex-direction: column; gap: 6px; margin-top: auto; }
.chart-head {
  display: flex; align-items: baseline; justify-content: space-between; gap: 8px;
  font-size: calc(12px * var(--ts, 1)); color: var(--text-faint);
}
.chart-peak { font-variant-numeric: tabular-nums; white-space: nowrap; }
.spark { display: block; width: 100%; height: 88px; overflow: visible; }
.spark-line {
  fill: none; stroke: var(--accent); stroke-width: 1.6;
  stroke-linejoin: round; stroke-linecap: round; vector-effect: non-scaling-stroke;
}
.spark-area { fill: color-mix(in srgb, var(--accent) 12%, transparent); stroke: none; }
.spark-dot { fill: var(--accent); }
.chart-empty {
  margin: auto 0 2px; font-size: calc(12px * var(--ts, 1)); line-height: 1.6; color: var(--text-faint);
}
.x-axis {
  display: flex; justify-content: space-between;
  font-size: calc(11px * var(--ts, 1)); color: var(--text-faint); font-variant-numeric: tabular-nums;
}
</style>
