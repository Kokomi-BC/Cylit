<script setup lang="ts">
/**
 * 模型分区的**消耗统计卡**：三个数字块 + 每日折线图 + 分模型排行。
 *
 * 折线图是手写内联 SVG（不引图表库）：
 * - 坐标系用**像素**（容器宽度由 ResizeObserver 实测 → viewBox 与 CSS 像素 1:1），
 *   所以描边不会被拉伸、hover 的 x→日期映射也就是一次除法；
 * - 纵轴永远从 0 起（`max(1, 峰值)`）—— 全 0 时是贴基线的一条直线，不是 NaN 也不是空白；
 * - 面积用同一条路径闭合到基线 + 线性渐变，末端一个实心点；
 * - hover 出虚线游标 + 空心点 + 一个 HTML 小签（日期 / 数值），x 轴只标起止与中点。
 *
 * 统计接口可能还没上线（404）：**静默降级成空态**，绝不抛错、不挡整个面板；
 * 后端就绪后这里不改一行也会自动有数。
 */
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { api } from '@/api/client';
import type { ModelUsageStats } from '@/api/types';
import AppIcon from '@/components/AppIcon.vue';

const props = withDefaults(defineProps<{ days?: number }>(), { days: 14 });

const stats = ref<ModelUsageStats | null>(null);
const win = ref(props.days);

async function load() {
  try {
    const data = await api.get<ModelUsageStats>('/models/usage/stats', { days: win.value });
    stats.value = data && Array.isArray(data.days) ? data : null;
  } catch {
    stats.value = null;   // 接口未就绪 / 网络失败：只是没数，不是错误
  }
}

function setWin(n: number) {
  if (n === win.value) return;
  win.value = n;
  void load();
}

onMounted(() => { void load(); });
watch(() => props.days, (n) => {
  if (n === win.value) return;
  win.value = n;
  void load();
});
defineExpose({ reload: load });

/* ---------------------------- 数字 ---------------------------- */

function trim1(v: number): string {
  const s = (Math.round(v * 10) / 10).toFixed(1);
  return s.endsWith('.0') ? s.slice(0, -2) : s;
}

/** 万 / 亿 的紧凑写法（12400 → 1.2万），大数字不撑破数字块。 */
function fmtN(v: number): string {
  const n = Number(v) || 0;
  if (n >= 1e8) return trim1(n / 1e8) + '亿';
  if (n >= 1e4) return trim1(n / 1e4) + '万';
  return String(Math.round(n));
}

const totals = computed(() => ({
  total: Number(stats.value?.totals?.total) || 0,
  today: Number(stats.value?.today?.total) || 0,
  requests: Number(stats.value?.totals?.requests) || 0,
}));

/* ---------------------------- 折线几何 ---------------------------- */

/** 100px：整卡要压到 150~170，折线不能比右侧排行高，否则图表反过来决定卡片高度。 */
const H = 100;
const padL = 0;    // 折线两端与上方数字左/右对齐（只给末端点留 3px，免得圆点被裁）
const padR = 3;
const padT = 10;
const padB = 16;
const innerH = H - padT - padB;
const baseY = padT + innerH;

const box = ref<HTMLDivElement | null>(null);
const boxW = ref(640);
let ro: ResizeObserver | null = null;

function measure() {
  const el = box.value;
  if (el) boxW.value = Math.max(220, Math.round(el.clientWidth) || 640);
}

onMounted(() => {
  measure();
  if (typeof ResizeObserver === 'undefined' || !box.value) return;
  ro = new ResizeObserver(measure);
  ro.observe(box.value);
});
onBeforeUnmount(() => { ro?.disconnect(); ro = null; });

const days = computed(() => stats.value?.days ?? []);
const series = computed(() => days.value.map((d) => Number(d.total) || 0));
const innerW = computed(() => Math.max(1, boxW.value - padL - padR));
/** 峰值；全 0 时取 1，于是所有点正好落在基线上（直线，不是空白）。 */
const peak = computed(() => Math.max(1, ...series.value));

function xAt(i: number): number {
  const n = series.value.length;
  if (n <= 1) return padL + innerW.value / 2;
  return padL + (innerW.value * i) / (n - 1);
}

function yAt(v: number): number {
  return padT + innerH * (1 - v / peak.value);
}

interface Pt { i: number; x: number; y: number; v: number }

const pts = computed<Pt[]>(() => series.value.map((v, i) => ({ i, x: xAt(i), y: yAt(v), v })));

const linePath = computed(() => pts.value
  .map((p, i) => `${i ? 'L' : 'M'}${p.x.toFixed(2)} ${p.y.toFixed(2)}`)
  .join(' '));

/** 同一条折线闭合回基线：只有一天时宽度为 0，退化成一枚竖条（仍合法）。 */
const areaPath = computed(() => {
  const list = pts.value;
  if (!list.length) return '';
  const base = baseY.toFixed(2);
  const head = `M${list[0].x.toFixed(2)} ${base}`;
  const body = list.map((p) => `L${p.x.toFixed(2)} ${p.y.toFixed(2)}`).join('');
  return `${head}${body}L${list[list.length - 1].x.toFixed(2)} ${base}Z`;
});

const lastPt = computed(() => pts.value[pts.value.length - 1] ?? null);

/** x 轴只标 起 / 中 / 止（一天时只标那个点）。 */
const ticks = computed(() => {
  const list = pts.value;
  if (!list.length) return [] as Array<{ i: number; x: number; label: string; anchor: string }>;
  const idx = list.length <= 3
    ? list.map((p) => p.i)
    : [...new Set([0, Math.floor((list.length - 1) / 2), list.length - 1])];
  return idx.map((i) => ({
    i,
    x: list[i].x,
    label: shortDate(days.value[i]?.date || ''),
    anchor: i === 0 ? 'start' : (i === list.length - 1 ? 'end' : 'middle'),
  }));
});

function shortDate(d: string): string {
  const s = String(d || '');
  if (s.length >= 10) return `${s.slice(5, 7)}/${s.slice(8, 10)}`;
  return s;
}

/* ---------------------------- hover ---------------------------- */

const hoverIdx = ref(-1);

function onMove(ev: MouseEvent) {
  const el = box.value;
  const n = series.value.length;
  if (!el || !n) return;
  const r = el.getBoundingClientRect();
  // 容器被 max-width 压窄时 svg 是等比缩放的，先把鼠标位置换算回坐标系像素
  const mx = (ev.clientX - r.left) * (r.width ? boxW.value / r.width : 1);
  const i = n <= 1 ? 0 : Math.round(((mx - padL) / innerW.value) * (n - 1));
  hoverIdx.value = Math.max(0, Math.min(n - 1, i));
}

function onLeave() { hoverIdx.value = -1; }

const hoverPt = computed<Pt | null>(() => pts.value[hoverIdx.value] ?? null);
/** 点太靠上时小签改挂在下方，否则会顶出卡片。 */
const tipBelow = computed(() => (hoverPt.value ? hoverPt.value.y < padT + 34 : false));

/** 小签的横坐标：贴边时向内收一点，否则会被容器边缘裁掉半截。 */
const tipX = computed(() => {
  const p = hoverPt.value;
  if (!p) return 0;
  const pad = 44;
  return Math.min(Math.max(p.x, pad), Math.max(pad, boxW.value - pad));
});

/* ---------------------------- 分模型排行 ---------------------------- */

/** 排行口径没变：每行的占比 = 这一行总量 / 榜单里所有模型的总量。
 *  只是**不再把第 6 名以后合并成「其他」**—— 用户要的是「默认 4 行 + 展开全部」，
 *  合并成一条「其他」就永远看不到具体是哪个模型在烧 token。 */
const rankAll = computed(() => {
  const rows = (stats.value?.by_model ?? [])
    .map((r) => ({ name: r.name || r.model_name || r.model_config_id || '未命名', total: Number(r.total) || 0 }))
    .filter((r) => r.total > 0)
    .sort((a, b) => b.total - a.total);
  const sum = rows.reduce((s, r) => s + r.total, 0);
  return rows.map((r) => ({ ...r, share: sum ? r.total / sum : 0 }));
});

/** 默认只露 4 行（上方面板要压在一屏里）；多出来的收进标题行右侧的「展开全部」。 */
const RANK_LIMIT = 4;
const rankExpanded = ref(false);
const rankVisible = computed(() => (rankExpanded.value ? rankAll.value : rankAll.value.slice(0, RANK_LIMIT)));
const rankHidden = computed(() => Math.max(0, rankAll.value.length - RANK_LIMIT));

/** 数字拆成「数值 + 单位」两档：大数字一档、单位一档、标签一档（三个数字块共用同一套层级）。 */
function splitN(v: number): { n: string; u: string } {
  const n = Number(v) || 0;
  if (n >= 1e8) return { n: trim1(n / 1e8), u: '亿' };
  if (n >= 1e4) return { n: trim1(n / 1e4), u: '万' };
  return { n: String(Math.round(n)), u: '' };
}

/** 渐变 id 必须全局唯一：同页两个实例共用 "u-grad" 会互相抢。 */
const gradId = `u-grad-${Math.random().toString(36).slice(2, 8)}`;
</script>

<template>
  <section class="card usage" :class="{ 'is-empty': !days.length }">
    <div class="usage-head">
      <span class="usage-title"><AppIcon name="gauge" :size="14" /><span>消耗</span></span>
      <span class="usage-sub">近 {{ win }} 天</span>
      <span class="spacer"></span>
      <div class="seg win" role="group" aria-label="统计窗口">
        <button type="button" :class="{ active: win === 14 }" @click="setWin(14)">14 天</button>
        <button type="button" :class="{ active: win === 30 }" @click="setWin(30)">30 天</button>
      </div>
    </div>

    <!-- 接口未就绪 / 无任何记录：一行，不占屏 -->
    <div class="usage-empty" v-if="!days.length">
      <AppIcon name="gauge" :size="13" /><span>暂无消耗记录</span>
    </div>

    <div class="usage-body" v-else>
      <div class="nums">
        <div class="num-block">
          <span class="num-line"><b class="n">{{ splitN(totals.total).n }}</b><i class="u">{{ splitN(totals.total).u }}</i></span>
          <span class="k">总消耗</span>
        </div>
        <div class="num-block">
          <span class="num-line"><b class="n">{{ splitN(totals.today).n }}</b><i class="u">{{ splitN(totals.today).u }}</i></span>
          <span class="k">今日</span>
        </div>
        <div class="num-block">
          <span class="num-line"><b class="n">{{ splitN(totals.requests).n }}</b><i class="u">{{ splitN(totals.requests).u }}</i></span>
          <span class="k">请求数</span>
        </div>
      </div>

      <div
        class="chart" ref="box" @mousemove="onMove" @mouseleave="onLeave"
        :aria-label="`近 ${win} 天每日 token 消耗`"
      >
        <svg :width="boxW" :height="H" :viewBox="`0 0 ${boxW} ${H}`" role="img">
          <defs>
            <linearGradient :id="gradId" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" class="g-a" />
              <stop offset="100%" class="g-b" />
            </linearGradient>
          </defs>

          <line class="axis" :x1="padL" :y1="baseY" :x2="boxW - padR" :y2="baseY" />
          <path class="area" :d="areaPath" :fill="`url(#${gradId})`" />
          <path class="ln" :d="linePath" />

          <circle class="end" v-if="lastPt" :cx="lastPt.x" :cy="lastPt.y" r="3" />

          <g v-if="hoverPt">
            <line class="guide" :x1="hoverPt.x" :y1="padT" :x2="hoverPt.x" :y2="baseY" />
            <circle class="hp" :cx="hoverPt.x" :cy="hoverPt.y" r="3.6" />
          </g>

          <text
            class="tick" v-for="t in ticks" :key="t.i"
            :x="t.x" :y="H - 2" :text-anchor="t.anchor"
          >{{ t.label }}</text>
        </svg>

        <div
          class="tip" :class="{ below: tipBelow }" v-if="hoverPt"
          :style="{ left: tipX + 'px', top: hoverPt.y + 'px' }"
        >
          <b>{{ fmtN(hoverPt.v) }}</b>
          <span>{{ days[hoverPt.i]?.date }}</span>
        </div>
      </div>

      <div class="rank">
        <div class="rank-head">
          <span class="rank-title">分模型消耗</span>
          <span class="spacer"></span>
          <!-- 展开/收起挂在标题行右侧：底部再单起一行会把整卡顶高 14px -->
          <button
            class="rank-more" v-if="rankHidden > 0 || rankExpanded" type="button"
            :aria-expanded="rankExpanded" @click="rankExpanded = !rankExpanded"
          >
            <AppIcon :name="rankExpanded ? 'chevron-up' : 'chevron-down'" :size="12" />
            <span>{{ rankExpanded ? '收起' : '展开全部' }}</span>
            <span class="rm-count" v-if="!rankExpanded">（还有 {{ rankHidden }} 个）</span>
          </button>
        </div>
        <template v-if="rankVisible.length">
          <div class="rank-row" v-for="r in rankVisible" :key="r.name">
            <span class="rk-name truncate" :title="r.name">{{ r.name }}</span>
            <span class="rk-n">{{ fmtN(r.total) }}</span>
            <span class="rk-p">{{ Math.round(r.share * 100) }}%</span>
            <span class="rk-bar"><i :style="{ width: Math.max(3, r.share * 100) + '%' }" /></span>
          </div>
        </template>
        <div class="rank-none" v-else>暂无消耗记录</div>
      </div>
    </div>
  </section>
</template>

<style scoped>
/* 用量卡永远整宽：折线需要横向像素，落在两列网格里也必须独占一行。
   整卡是一张 .card，内部不再套卡片、不再用 .card-head 那条带分隔线的 11px 行。 */
.usage {
  grid-column: 1 / -1;
  display: flex; flex-direction: column; gap: 8px;
  padding: 12px 15px;
}
/* 空态只剩一行字：整块塌到 ~66px，别留大空白 */
.usage.is-empty { padding: 11px 15px; gap: 6px; }

/* 顶部一行（22px）：标题 + 窗口说明 + 14/30 天分段，不留分隔线 */
.usage-head { display: flex; align-items: center; gap: 8px; min-height: 22px; }
.usage-title {
  display: inline-flex; align-items: center; gap: 6px;
  font-size: calc(13px * var(--ts, 1)); font-weight: 600; color: var(--text);
}
.icon { display: block; }
.usage-sub {
  font-size: calc(12px * var(--ts, 1)); color: var(--text-faint);
  font-variant-numeric: tabular-nums; white-space: nowrap;
}
/* 14/30 天用全站那枚胶囊（.seg），但整行要压进 22px：托盘与选项各收一档 */
.win { height: 22px; }
.win button { height: 16px; padding: 0 9px; font-size: calc(11.5px * var(--ts, 1)); }
.win button:focus-visible { outline: none; box-shadow: var(--ring); }

.usage-empty {
  display: flex; align-items: center; gap: 8px;
  color: var(--text-faint); font-size: calc(12px * var(--ts, 1)); line-height: 1.2;
}
/* 桌面三栏：三指标（自然宽度）| 趋势图（自适应，最小 220）| 分模型排行（定宽） */
.usage-body { display: flex; align-items: stretch; gap: 16px; min-width: 0; }

/* 三指标横排，只用一条 1px 竖线分隔，不做每块一个描边框 */
.nums { display: flex; flex: none; align-items: stretch; }
.num-block {
  display: flex; flex-direction: column; justify-content: center; gap: 1px;
  padding: 0 16px; min-width: 0;
}
.num-block:first-child { padding-left: 0; }
.num-block + .num-block { border-left: 1px solid var(--divider); }
.num-line { display: inline-flex; align-items: baseline; gap: 2px; white-space: nowrap; }
.num-block .n {
  font-family: var(--display);
  font-size: calc(23px * var(--ts, 1)); font-weight: 650; line-height: 1.15;
  letter-spacing: -.02em; font-variant-numeric: tabular-nums; color: var(--text);
}
.num-block .u {
  font-style: normal; font-size: calc(12px * var(--ts, 1)); font-weight: 600;
  color: var(--text-dim); font-variant-numeric: tabular-nums;
}
.num-block .k {
  font-size: calc(11px * var(--ts, 1)); line-height: 1.3;
  color: var(--text-faint); white-space: nowrap;
}

/* 图表：容器即坐标系（svg 的 CSS 像素 = 计算像素），hover 只需要减 left。
   盒子高度一律由 svg 自己撑（svg display:block 没有行盒缝隙）—— 容器一旦居中或拉伸，
   盒子就会比 svg 高一截，竖排档那截就是死空白。 */
.chart {
  position: relative; align-self: center;
  flex: 1 1 220px; min-width: 220px; max-width: 100%;
}
.chart svg { display: block; max-width: 100%; }

.axis { stroke: var(--divider); stroke-width: 1; }
.area { stroke: none; }
/* 面积只是让折线有落脚点，压到 .16，不要做成色块 */
.g-a { stop-color: var(--accent); stop-opacity: .16; }
.g-b { stop-color: var(--accent); stop-opacity: 0; }
.ln { fill: none; stroke: var(--accent); stroke-width: 1.6; stroke-linecap: round; stroke-linejoin: round; }
.end { fill: var(--accent); }
.guide { stroke: var(--accent); stroke-width: 1; stroke-dasharray: 3 3; opacity: .5; }
.hp { fill: var(--bg-elev); stroke: var(--accent); stroke-width: 1.6; }
.tick { font-size: calc(10px * var(--ts, 1)); fill: var(--text-faint); font-family: var(--mono); }

/* hover 小签：浮层用 --bg-elev + 描边 + --shadow-2，不用玻璃拟态 */
.tip {
  position: absolute; z-index: 2; pointer-events: none;
  transform: translate(-50%, -140%);
  display: flex; flex-direction: column; gap: 1px;
  padding: 4px 8px; border-radius: var(--r-sm);
  background: var(--bg-elev); border: 1px solid var(--border); box-shadow: var(--shadow-2);
  white-space: nowrap;
}
.tip.below { transform: translate(-50%, 10px); }
.tip b { font-size: calc(11.5px * var(--ts, 1)); font-weight: 650; font-variant-numeric: tabular-nums; color: var(--text); }
.tip span { font-size: calc(10.5px * var(--ts, 1)); color: var(--text-faint); font-family: var(--mono); }

/* 分模型排行：名字 / 数值 / 百分比一行，占比条是贴行底的一条 3px 细轨，
   不再占第二行——4 行 + 标题正好压进 112px。
   允许收缩：1024 附近三栏同时在场时，排行宁可截断标题也不把整行顶出卡片 */
.rank { flex: 0 1 280px; min-width: 0; display: flex; flex-direction: column; }
.rank-head { display: flex; align-items: center; gap: 8px; min-width: 0; min-height: 14px; margin-bottom: 4px; }
.rank-title {
  min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  font-size: calc(11px * var(--ts, 1)); font-weight: 650; color: var(--text-dim);
  letter-spacing: .02em;
}
.rank-row {
  position: relative; display: grid; min-width: 0;
  grid-template-columns: minmax(0, 1fr) auto auto; align-items: center; column-gap: 8px;
  height: 22px; padding-bottom: 3px;
}
.rank-row + .rank-row { margin-top: 2px; }
.rk-name { font-size: calc(12px * var(--ts, 1)); line-height: 1.2; color: var(--text); }
.rk-n {
  font-size: calc(11.5px * var(--ts, 1)); line-height: 1.2;
  color: var(--text-dim); font-variant-numeric: tabular-nums;
}
.rk-p {
  font-size: calc(11px * var(--ts, 1)); line-height: 1.2; min-width: 26px; text-align: right;
  color: var(--text-faint); font-variant-numeric: tabular-nums;
}
.rk-bar {
  position: absolute; left: 0; right: 0; bottom: 0; height: 3px; overflow: hidden;
  border-radius: var(--r-pill); background: var(--bg-subtle);
}
.rk-bar > i { display: block; height: 100%; border-radius: var(--r-pill); background: var(--accent); }

.rank-none { font-size: calc(12px * var(--ts, 1)); color: var(--text-faint); }
.rank-more {
  display: inline-flex; flex: 0 0 auto; align-items: center; gap: 3px;
  padding: 0; border: 0; border-radius: 4px; background: transparent;
  color: var(--accent-text); font: inherit;
  font-size: calc(11px * var(--ts, 1)); line-height: 1.2; cursor: pointer; white-space: nowrap;
  transition: color var(--dur) var(--ease);
}
.rank-more:hover { text-decoration: underline; }
.rank-more:focus-visible { outline: none; box-shadow: var(--ring); }

/* 中等宽度：排行收窄，图表让出空间 */
@media (max-width: 1024px) {
  .rank { flex-basis: 220px; }
  .rm-count { display: none; }
}

/* 窄屏：三指标 → 图表 → 排行 纵向堆叠。竖排里 flex-basis 算的是**高度**，
   所以图表必须换成 flex:0 0 auto —— 否则它会按 220 撑出一个 220px 高的空盒子。 */
@media (max-width: 860px) {
  .usage { padding: 8px 14px; gap: 4px; }
  .usage-head { min-height: 20px; }
  .win { height: 20px; }
  .win button { height: 14px; padding: 0 8px; font-size: calc(11px * var(--ts, 1)); }
  .usage-body { flex-direction: column; align-items: stretch; gap: 6px; }
  .chart { flex: 0 0 auto; align-self: stretch; min-width: 0; }
  /* 排行铺满整宽后一行能放两条：4 个模型只占两行高度 */
  .rank {
    flex: 0 0 auto; display: grid; row-gap: 4px; column-gap: 16px;
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
  .rank-head { grid-column: 1 / -1; min-height: 0; margin-bottom: 0; }
  .rank-row { height: 20px; }
  .rank-row + .rank-row { margin-top: 0; }
  .rank-none { grid-column: 1 / -1; }
  .num-block .n { font-size: calc(20px * var(--ts, 1)); }
}

/* 更窄：三指标等分三列，字号降一档 */
@media (max-width: 560px) {
  .num-block { flex: 1 1 0; padding: 0 10px; }
  .num-block .u { font-size: calc(11px * var(--ts, 1)); }
  .num-block .k { font-size: calc(10px * var(--ts, 1)); }
}
</style>
