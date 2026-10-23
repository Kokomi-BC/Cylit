<script setup lang="ts">
/**
 * 「检索耗时趋势」：知识库检索的**服务端总耗时**折线图（平均 / P95）。
 *
 * 数据来自 `GET /knowledge/index/latency?days=N`。这里只认契约、不造假数据：
 * 请求失败就是失败态，`totals.count === 0` 或 `series` 为空就是空态 —— 但空态里
 * **不再说"缺一张检索日志表"**：后端已经把耗时落库了，那句话现在是错的，留着只会误导。
 *
 * 为什么只有一根 Y 轴：平均与 P95 同量纲（ms）、同量级（P95 通常只是平均的两三倍）。
 * 趋势卡用双轴是因为 chunk（几百）与论文（个位数）差两个数量级；这里各自一条轴只会
 * 出现两套刻度互相打架，而"P95 抬起、平均没动"这种尾延迟信号反而看不出来 ——
 * 单轴才能一眼比出两条线的高低差。
 *
 * 几何沿用 KbTrendCard / UsageChart（项目不引图表库）：ResizeObserver 实测容器宽 →
 * viewBox 与 CSS 像素 1:1，手写 path，网格线 --divider，刻度 11px。
 * RO 回调只排一帧 + measure 里等值 return，否则改 svg 宽度 → 再布局 → 又一条通知，
 * 浏览器会报 "ResizeObserver loop completed with undelivered notifications"。
 *
 * X 轴是**分类轴**：按"有数据的那几天"等距排布，不是日期比例轴 —— 中间空着的日子
 * 不该在图上占宽度（后端也只返回有数据的天）。
 *
 * 高度按"一排三块"定档：宽档 H 118 / 窄档（< 380）H 98，与趋势块同档；
 * 四个状态共用同一个 min-height（由 H 算出），切换时不跳。
 *
 * 两条"别自作聪明"的规矩：
 * - Y 轴只按数据取整齐上界，**不截断、不剔异常点**：真实数据里一次冷启动就能把 P95 拉到
 *   4.2s，于是上界跟着到 4500、平均线贴底。那正是要看的现象（尾延迟爆炸），
 *   把轴压小或过滤掉这一个点，曲线好看了、诊断价值没了。
 * - 底行固定写「每次检索的服务端总耗时」，后端的 note 原文只进 title：那是给开发者看的口径
 *   （真实文案里有 nearest-rank、ceil(q·n)、表名 retrieval_log），摆给用户读既占行又读不懂。
 */
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { api } from '@/api/client';
import AppIcon from '@/components/AppIcon.vue';

/** `/knowledge/index/latency` 的响应契约（后端已交付；结构不符时按失败态处理）。 */
interface LatPoint {
  date: string;
  count: number;
  avg_ms: number;
  p50_ms?: number;
  p95_ms: number;
}
interface LatTotals { count: number; avg_ms: number; p50_ms?: number; p95_ms: number }
interface LatResp {
  days: number;
  generated_at?: string;
  /** 后端给的口径说明，原样显示，不自己编。 */
  note?: string;
  series: LatPoint[];
  totals: LatTotals;
}

const DAYS = [7, 30, 90];

const win = ref(30);
const data = ref<LatResp | null>(null);
const loading = ref(false);
const error = ref('');
/** 连点天数时只让最后一次请求落地：慢的旧响应不能盖掉新窗口的数据。 */
let seq = 0;

function num(v: unknown): number {
  const n = Number(v);
  return Number.isFinite(n) && n > 0 ? n : 0;
}

async function load(): Promise<void> {
  const mine = ++seq;
  loading.value = true;
  error.value = '';
  try {
    const res = await api.get<LatResp>('/knowledge/index/latency', { days: win.value });
    if (mine !== seq) return;
    if (res && Array.isArray(res.series)) {
      data.value = res;
    } else {
      data.value = null;
      error.value = '接口返回的结构与契约不符';
    }
  } catch (err: any) {
    if (mine !== seq) return;
    data.value = null;
    error.value = String(err?.message || '请求失败');
  } finally {
    if (mine === seq) loading.value = false;
  }
}

function setWin(d: number): void {
  if (d === win.value) return;
  win.value = d;
  void load();
}

defineExpose({ reload: load });

/* ------------------------------ 数据派生 ------------------------------ */

const points = computed<LatPoint[]>(() => data.value?.series ?? []);
const totalCount = computed<number>(() => Number(data.value?.totals?.count) || 0);
const avgVals = computed<number[]>(() => points.value.map((p) => num(p.avg_ms)));
const p95Vals = computed<number[]>(() => points.value.map((p) => num(p.p95_ms)));
/** 空态判据：总量为 0 或一条点都没有（后端只给"有数据的那几天"，两种写法都得认）。 */
const empty = computed<boolean>(() => totalCount.value === 0 || !points.value.length);
/** 只有"成功 + 有数据"才画图；标题行摘要也跟着这个开关走。 */
const ready = computed<boolean>(() => !loading.value && !error.value && !empty.value);

/**
 * 底行固定写一句人话；后端的 note 原文进 title。
 * 为什么不直接显示 note：那是给开发者看的口径（真实文案里有 "nearest-rank"、"ceil(q·n)"、
 * 表名 retrieval_log），摆在设置页底行既占两行又看不懂，读完也不知道这图在量什么。
 */
const NOTE_FALLBACK = '每次检索的服务端总耗时';
const noteText = computed<string>(() => (data.value?.note || '').trim() || NOTE_FALLBACK);

/**
 * 中位耗时：优先用 totals.p50_ms；后端这一版 totals 里只有 count/avg_ms/p95_ms，
 * 就退回"各天 p50 按次数加权平均"。两者在单日窗口里完全相同（真实数据都是 579 ms），
 * 多日时前者是近似 —— 所以只取整显示，不给小数。
 * 为什么标题行要它：一次冷启动就能把 p95 拉到 4s，只有中位能说明"常态其实还好"。
 */
const medianMs = computed<number | null>(() => {
  const t = Number(data.value?.totals?.p50_ms);
  if (Number.isFinite(t) && t > 0) return Math.round(t);
  let sum = 0;
  let cnt = 0;
  for (const p of points.value) {
    const w = num(p.count) || 1;
    sum += num(p.p50_ms) * w;
    cnt += w;
  }
  return cnt > 0 && sum > 0 ? Math.round(sum / cnt) : null;
});
const summaryText = computed<string>(() => {
  const base = `近 ${win.value} 天 ${totalCount.value} 次`;
  return medianMs.value === null ? base : `${base} · 中位 ${medianMs.value} ms`;
});

/* ------------------------------ 折线几何 ------------------------------ */

const COMPACT_W = 380;
const padT = 10;
const padB = 20;
const padR = 10;            // 单轴：右侧只给末端圆点留半个直径

const box = ref<HTMLDivElement | null>(null);
const boxW = ref(640);
let ro: ResizeObserver | null = null;
let rafId = 0;

const compact = computed<boolean>(() => boxW.value < COMPACT_W);
const H = computed<number>(() => (compact.value ? 98 : 118));
const innerH = computed<number>(() => H.value - padT - padB);
/**
 * 四态共用的那块高度（border-box，含 .card-body.tight 的 6px 上下内边距）：
 * 图例行（14 + 4 外边距）= 18 + 图区 H + 底行 11px × 1.45 ≈ 16。底行是固定一句话、
 * 不折行，所以这里按一行算，四个状态量出来一模一样。
 */
const bodyH = computed<number>(() => H.value + 47);

/** 刻度步长取整到 1 / 1.2 / 1.5 / 2 / 2.5 / 3 / 4 / 5 / 6 / 8 × 10^k，
 *  于是刻度线落在整齐的数字上，不会出现 83.4 这种读数。 */
const STEPS = [1, 1.2, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10];
function niceStep(raw: number): number {
  const exp = Math.pow(10, Math.floor(Math.log10(raw)));
  const step = STEPS.find((s) => raw / exp <= s) ?? 10;
  return step * exp;
}

/** 上界 = 3 个整齐步长（4 条网格线正好三等分）；两条线共用一把尺子，才有可比性。 */
const peak = computed<number>(() => {
  const top = Math.max(0, ...avgVals.value, ...p95Vals.value);
  return top > 0 ? niceStep(top / 3) * 3 : 1;
});

/** 整数不拖小数点，小数最多一位：刻度是给人读的，不是数据表。 */
function fmtMs(v: number): string {
  if (!Number.isFinite(v) || v <= 0) return '0';
  return Math.abs(v) >= 100 ? String(Math.round(v)) : String(Math.round(v * 10) / 10);
}

/**
 * 左轴留白按顶端那枚刻度**量**出来：ms 后缀只标在顶端一次（轴数字本身不重复写），
 * 于是顶端是 "240 ms" 而下面只有 "240" —— 写死 28 会把它顶出整块的左边界。
 * 11px 等宽字每字约 6.6px（0.6em），再加 10px 的呼吸。
 */
const padL = computed<number>(() => {
  const chars = `${fmtMs(peak.value)} ms`.length;
  return Math.round(Math.min(74, Math.max(30, chars * 6.6 + 10)));
});
const innerW = computed<number>(() => Math.max(1, boxW.value - padL.value - padR));

function yOf(v: number): number {
  return padT + innerH.value * (1 - v / Math.max(1e-9, peak.value));
}

/** 等距分类轴：第 i 个"有数据的天"落在 i / (n-1) 上；只有一个点时居中。 */
function xAt(i: number): number {
  const n = points.value.length;
  if (n <= 1) return padL.value + innerW.value / 2;
  return padL.value + (innerW.value * i) / (n - 1);
}

function pathOf(vals: number[]): string {
  return vals.map((v, i) => `${i ? 'L' : 'M'}${xAt(i).toFixed(1)} ${yOf(v).toFixed(1)}`).join(' ');
}

const avgPath = computed<string>(() => pathOf(avgVals.value));
const p95Path = computed<string>(() => pathOf(p95Vals.value));

function endOf(vals: number[]): { x: number; y: number } | null {
  if (!vals.length) return null;
  const i = vals.length - 1;
  return { x: xAt(i), y: yOf(vals[i]) };
}
const lastAvg = computed(() => endOf(avgVals.value));
const lastP95 = computed(() => endOf(p95Vals.value));

const grid = computed<Array<{ y: number; label: string }>>(() => {
  const step = peak.value / 3;
  return [0, 1, 2, 3].map((k) => ({
    y: yOf(step * k),
    label: k === 3 ? `${fmtMs(step * 3)} ms` : fmtMs(step * k),
  }));
});

function mmdd(s: string): string {
  return s.length >= 10 ? `${s.slice(5, 7)}-${s.slice(8, 10)}` : s;
}

/** 窄档只标 4 个日期（一排三块时每张约 310px，再多就糊成一片）；宽档最多 6 个。 */
const xTicks = computed<Array<{ i: number; x: number; label: string; anchor: string }>>(() => {
  const n = points.value.length;
  if (!n) return [];
  const want = Math.min(n, compact.value ? 4 : 6);
  const idx: number[] = [];
  for (let k = 0; k < want; k += 1) idx.push(n === 1 ? 0 : Math.round(((n - 1) * k) / (want - 1)));
  return [...new Set(idx)].map((i) => ({
    i,
    x: xAt(i),
    label: mmdd(points.value[i]?.date || ''),
    anchor: i === 0 ? 'start' : (i === n - 1 ? 'end' : 'middle'),
  }));
});

const ariaLabel = computed<string>(() => `近 ${win.value} 天检索耗时趋势：平均与 P95，单位毫秒`);

/* ----------------------------- 宽度实测 ----------------------------- */

function measure(): void {
  const el = box.value;
  if (!el) return;
  // 量 .card-body 的内容宽（扣掉它自己的左右内距），而不是量 .chart：
  // 空态 / 失败态根本没有 .chart，量不到就会退回"宽档"，四个状态的高度就对不上。
  // 内距改成 12 之后不再写死 16，按实算值扣，改版式时这里不用跟着改数。
  const gut = (parseFloat(getComputedStyle(el).paddingLeft) || 0)
    + (parseFloat(getComputedStyle(el).paddingRight) || 0);
  const w = Math.max(220, Math.round(el.clientWidth - gut) || 640);
  // 等值就退出：同一个宽度反复写回 ref 仍会通知观察者，svg 的 width 回写再排一次版，
  // 浏览器于是报 "ResizeObserver loop completed with undelivered notifications"。
  if (w === boxW.value) return;
  boxW.value = w;
}

/** RO 回调里只排一帧：把改状态（进而改 svg 宽度）挪出回调本身，迭代才会收敛。 */
function onResize(): void {
  if (rafId) return;
  rafId = requestAnimationFrame(() => { rafId = 0; measure(); });
}

/** 容器一直是同一个 .card-body，所以挂载后绑一次就够；数据态切换时再量一次防宽度变化。 */
function bind(): void {
  measure();
  ro?.disconnect();
  ro = null;
  if (typeof ResizeObserver === 'undefined' || !box.value) return;
  ro = new ResizeObserver(onResize);
  ro.observe(box.value);
}

onBeforeUnmount(() => {
  ro?.disconnect();
  ro = null;
  if (rafId) cancelAnimationFrame(rafId);
  rafId = 0;
});

onMounted(() => { void load(); bind(); });
/* flush: 'post' = DOM 打完补丁再量（标题行折行会改变宽度，量晚不量早）。 */
watch([ready, () => points.value.length], () => { bind(); }, { flush: 'post' });
</script>

<template>
  <section class="latency">
    <div class="card-head">
      <AppIcon name="gauge" :size="15" /><span class="ttl">检索耗时趋势</span>
      <span class="spacer"></span>
      <span v-if="ready" class="tiny faint sum" :title="summaryText">{{ summaryText }}</span>
      <span v-else class="badge dim">暂无数据</span>
      <div class="seg days" role="group" aria-label="统计窗口">
        <button v-for="d in DAYS" :key="d" type="button" :class="{ active: win === d }" @click="setWin(d)">
          {{ d }} 天
        </button>
      </div>
    </div>

    <div class="card-body tight" ref="box" :style="{ minHeight: bodyH + 'px' }">
      <div class="state" v-if="loading">正在读取…</div>

      <div class="state" v-else-if="error">
        <span class="err clamp-2" :title="error">{{ error }}</span>
        <button class="btn sm" type="button" @click="load()">重试</button>
      </div>

      <!-- 空态只留两句：不带 42px 的图标块 —— 一排三块时它会把这态顶得比旁边画着曲线的块还高，
           而"没有数据"用一句人话说清楚就够了，不需要图。 -->
      <div class="empty-state lat-empty" v-else-if="empty">
        <span class="es-title">还没有检索记录</span>
        <span class="es-hint tiny faint lat-hint">
          发起一次知识库检索或论文问答后，这里会出现平均与 P95 耗时
        </span>
      </div>

      <template v-else>
        <div class="plot">
          <!-- 图例贴图表区顶行右侧：跟曲线同一个视野里，读图不用来回扫 -->
          <div class="plot-head">
            <span class="lg"><i class="lg-dot avg"></i>平均</span>
            <span class="lg"><i class="lg-dot p95"></i>P95</span>
          </div>

          <div class="chart">
            <svg :width="boxW" :height="H" :viewBox="`0 0 ${boxW} ${H}`" role="img" :aria-label="ariaLabel">
              <line
                v-for="(g, gi) in grid" :key="'g' + gi" class="grid-line"
                :x1="padL" :y1="g.y" :x2="boxW - padR" :y2="g.y"
              />
              <text
                v-for="(g, gi) in grid" :key="'y' + gi" class="tick"
                :x="padL - 6" :y="g.y + 4" text-anchor="end"
              >{{ g.label }}</text>

              <path class="ln avg" :d="avgPath" />
              <path class="ln p95" :d="p95Path" />

              <circle class="end avg" v-if="lastAvg" :cx="lastAvg.x" :cy="lastAvg.y" r="2.8" />
              <circle class="end p95" v-if="lastP95" :cx="lastP95.x" :cy="lastP95.y" r="2.8" />

              <text
                v-for="t in xTicks" :key="'x' + t.i" class="tick"
                :x="t.x" :y="H - 4" :text-anchor="t.anchor"
              >{{ t.label }}</text>
            </svg>
          </div>

          <!-- 底行是固定一句话；后端口径原文（含 nearest-rank 这类词）只挂 title，不摆给用户读 -->
          <p class="tiny faint note" :title="noteText">{{ NOTE_FALLBACK }}</p>
        </div>
      </template>
    </div>
  </section>
</template>

<style scoped>
/* 标题行（原卡头）：标题 + 摘要 + 一枚 seg，与趋势块同款同档（13.5px 标题、18px 选项、24px 托盘）。
   窄块里允许折行 —— 不折就会把整行推出整块。 */
.latency { min-width: 0; display: flex; flex-direction: column; }
.ttl { font-size: calc(13.5px * var(--ts, 1)); }
/* 标题行在淡底之外：透明、不描边、不留发丝线。 */
.latency .card-head { flex-wrap: wrap; row-gap: 6px; border-bottom: 0; }
.days { flex: none; height: 24px; }
.days button { height: 18px; padding: 0 9px; font-size: calc(11.5px * var(--ts, 1)); }
.days button:focus-visible { outline: none; box-shadow: var(--ring); }
/* 标题行右侧那枚"摘要 / 暂无数据"固定 22px、单行不折：两态的盒子一样高，
   标题行的折行方式才一样，切到"有数据"时整块不会因为这一枚矮了 1px 而抖一下。
   摘要本身 nowrap + 省略号（全量在 title 里），长窗口也不把标题行挤成三行。 */
.latency .card-head .sum,
.latency .card-head .badge { height: 22px; display: inline-flex; align-items: center; flex: 0 1 auto; min-width: 0; }
.sum {
  overflow: hidden; white-space: nowrap; text-overflow: ellipsis;
  /* 摘要是数字：tabular-nums 让切窗口时"49 次 → 96 次"不左右跳 */
  font-variant-numeric: tabular-nums;
}

/* 四态共用同一块高度：min-height 由 H 算出来写在 style 上，state 用 flex: 1 撑满，
   于是"加载中 → 空 → 有数据"切换时整块既不长高也不塌下去。 */
/* 图区才是那条淡色圆角卡（--bg-sunken + --r-sm，内距 12）：四态都在它里面，等高不跳。 */
.card-body.tight {
  flex: 1 1 auto;
  display: flex; flex-direction: column;
  padding: 12px;
  background: var(--bg-sunken);
  border-radius: var(--r-sm);
}
/* 有数据那态也撑满 min-height：图固定高、口径行贴底，于是它和另外三态占同一块地方 */
.plot { flex: 1 1 auto; min-height: 0; display: flex; flex-direction: column; }
.state {
  flex: 1 1 auto;
  display: flex; align-items: center; justify-content: center; gap: 8px;
  min-height: 0;
  font-size: calc(12.5px * var(--ts, 1)); color: var(--text-faint);
}
.state .err {
  max-width: 42ch; text-align: center;
  font-size: calc(12.5px * var(--ts, 1)); line-height: 1.5; color: var(--danger);
}

/* 空态只有"一句结论 + 一句怎么做"（不放图标块）：padding 与间距收一档，不比旁边画着曲线的卡高。 */
.lat-empty { flex: 1 1 auto; min-height: 0; justify-content: center; padding: 8px 10px; gap: 4px; }
/* 第三行让位给 .tiny.faint 的档次：全局 `.empty-state .es-hint` 是 12.5px / --text-dim，
   作用域内把选择器提一档（0,3,0）才盖得住它 */
.lat-empty .es-hint.lat-hint {
  max-width: 40ch;
  font-size: calc(11px * var(--ts, 1));
  line-height: 1.5;
  color: var(--text-faint);
  text-wrap: pretty;
}

.plot-head {
  display: flex; align-items: center; justify-content: flex-end; flex-wrap: wrap;
  gap: 4px 14px; min-height: 14px; margin: 2px 4px;
}
.lg {
  display: inline-flex; align-items: center; gap: 5px;
  font-size: calc(11px * var(--ts, 1)); line-height: 1; color: var(--text-faint);
  white-space: nowrap;
}
.lg-dot { width: 8px; height: 8px; border-radius: var(--r-pill); flex: none; display: inline-block; }
.lg-dot.avg { background: var(--accent); }
.lg-dot.p95 { background: var(--warn); }

/* 容器即坐标系（svg 的 CSS 像素 = 计算像素） */
.chart { position: relative; min-width: 0; }
.chart svg { display: block; max-width: 100%; }

.grid-line { stroke: var(--divider); stroke-width: 1; }
.tick { font-size: calc(11px * var(--ts, 1)); fill: var(--text-faint); font-family: var(--mono); font-variant-numeric: tabular-nums; }
/* 线宽与趋势卡一致：1.7 + 圆头，细卡里两条线不会糊在一起 */
.ln { fill: none; stroke-width: 1.7; stroke-linecap: round; stroke-linejoin: round; }
.ln.avg { stroke: var(--accent); }
.ln.p95 { stroke: var(--warn); }
.end.avg { fill: var(--accent); }
.end.p95 { fill: var(--warn); }

/* 底行：固定一句话，单行；口径原文在 title。margin-top: auto = 贴在这块高度的底部 */
.note {
  margin: auto 4px 0; line-height: 1.45;
  overflow: hidden; white-space: nowrap; text-overflow: ellipsis;
}

@media (max-width: 860px) {
  .days button { padding: 0 8px; font-size: calc(11px * var(--ts, 1)); }
}
@media (max-width: 560px) {
  .latency .card-head { gap: 6px; }
  .plot-head { gap: 10px; }
}
</style>
