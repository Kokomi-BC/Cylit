<script setup lang="ts">
/**
 * 「索引数据增长趋势」：知识库每天的入库量（chunk 数 / 论文数）折线图。
 *
 * 数据来自 `GET /knowledge/index/trend?days=N`（后端并行开发中，接口可能还 404）。
 * 这里只认契约、不造假数据：请求失败就是失败态，series 为空就是空态；
 * **全 0 的 series 仍然照画**——那是"确实没新增"，跟"没有数据"是两件事。
 *
 * 为什么双 Y 轴：一篇论文动辄几百条 chunk，论文数却只有个位数到几十，
 * 两者量级差 1~2 个数量级。共用一个刻度时论文那条会永远贴在基线上变成一条直线，
 * 所以左轴给 chunk、右轴给论文，各自独立取整到"好看"的上界。
 *
 * 画法沿用 settings/UsageChart.vue（项目没有图表库，也不该为一张卡引一个）：
 * 像素坐标系（ResizeObserver 实测容器宽 → viewBox 与 CSS 像素 1:1，描边不会被拉伸），
 * 手写 path，水平网格线用 --divider，坐标轴文字 11px。
 * 天数切换要重新请求（后端按窗口聚合）；累计总量/每日新增只是同一份数据换一对字段，不发请求。
 *
 * 版式两条硬规矩（三块并排时每张只剩 316px，内容宽 288，容不得折行）：
 * - 标题行只许一行：图标 + 标题 + 一枚时间窗 seg。标题窄了省略，不折行 ——
 *   三块标题行同高，下面三条淡底才同高；
 * - 图例与口径开关同处图表区顶行（左图例 / 右开关）：开关塞进标题行会挤掉标题，
 *   而"这条线画的是累计总量还是每日新增"本来就属于图自己那行。
 */
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { api } from '@/api/client';
import AppIcon from '@/components/AppIcon.vue';

/** `/knowledge/index/trend` 的响应契约。 */
interface TrendPoint {
  date: string;
  papers: number;
  chunks: number;
  cum_papers: number;
  cum_chunks: number;
}
interface TrendResp {
  days: number;
  generated_at?: string;
  /** 后端给的口径说明，原样显示，不自己编。 */
  note?: string;
  series: TrendPoint[];
}

/** cum = 累计存量，daily = 当天新增。 */
type Scope = 'cum' | 'daily';

const DAYS = [7, 30, 90];

/**
 * 「累计 / 每日」两词单独摆着有歧义：一边可能是"累计到这一天"，另一边可能是"这一天的累计"。
 * 所以按钮文字改成人话，完整定义挂 aria-label 与 title（鼠标停一下就读到口径）。
 * **只改说法，不动口径**：累计仍是 cum_chunks / cum_papers，每日仍是当天的 chunks / papers。
 */
const SCOPE_TIP = '口径开关：累计总量 = 截至该日的存量；每日新增 = 那一天新增的数量';
const SCOPE_CUM_TIP = '累计总量：截至该日的存量，曲线只会涨、不会回落';
const SCOPE_DAILY_TIP = '每日新增：那一天新增了多少（单日入库量，会回落）';

const win = ref(30);
const scope = ref<Scope>('cum');
const data = ref<TrendResp | null>(null);
const loading = ref(false);
const error = ref('');
/** 连点天数时只让最后一次请求落地，否则慢的旧响应会盖掉新窗口的数据。 */
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
    const res = await api.get<TrendResp>('/knowledge/index/trend', { days: win.value });
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

onMounted(() => { void load(); });
defineExpose({ reload: load });

/* ---------------------------- 折线几何 ---------------------------- */

/**
 * 一排三块之后，图表区实测宽可能只剩 300 上下（956 的内容区三等分 ≈ 310，再扣卡内留白），
 * 所以尺寸分两档，跟着**实测容器宽**走（量的是 .chart 的 clientWidth，不是视口）：
 *   宽档：H 118 / 轴留白 38 —— 图例行 + 轴 + 曲线压进 130~150，整块不被一张图撑高；
 *   窄档（< 380）：H 98 / 轴留白 28 / 日期刻度 4 个，图区收到 108~125。
 * 这是"图可以画小"：刻度字号仍是 11px（再小就成了糊在一起的灰点），只减少刻度个数。
 */
const padT = 10;
const padB = 20;
const COMPACT_W = 380;

const box = ref<HTMLDivElement | null>(null);
/** 卡体（原 .card-body）：**四态都在**，量宽度的兜底锚点（加载/失败/空三态没有 .chart 可量）。 */
const shell = ref<HTMLDivElement | null>(null);
const boxW = ref(640);
let ro: ResizeObserver | null = null;
let rafId = 0;

const compact = computed(() => boxW.value < COMPACT_W);
const H = computed(() => (compact.value ? 98 : 118));
const padL = computed(() => (compact.value ? 28 : 38));   // 左轴（chunk）刻度文字的落脚位
const padR = computed(() => (compact.value ? 28 : 38));   // 右轴（论文）刻度文字
const innerH = computed(() => H.value - padT - padB);

/**
 * 量宽度：有数据时量 .chart（它自己就是坐标系，没有内边距），其余三态退到 .card-body 并
 * 扣掉左右内边距。**不能只量 .chart**：那三态里它不存在，boxW 会停在上一次的值 / 默认 640，
 * 于是 H 落到宽档（118）而卡体那块 min-height 又按窄档算 —— 切态时整块会跳 20px。
 */
function measure(): void {
  const el = box.value || shell.value;
  if (!el) return;
  const gut = el === box.value
    ? 0
    : (parseFloat(getComputedStyle(el).paddingLeft) || 0) + (parseFloat(getComputedStyle(el).paddingRight) || 0);
  const w = Math.max(220, Math.round(el.clientWidth - gut) || 640);
  // 等值就退出：svg 的 width 回写会再排一次版，同一个宽度反复写进 ref 仍会触发通知，
  // 浏览器于是报 "ResizeObserver loop completed with undelivered notifications"。
  if (w === boxW.value) return;
  boxW.value = w;
}

/**
 * RO 回调里**只排一帧**：把改状态（进而改 svg 的 width 属性）挪出回调本身。
 * 回调里同步改尺寸 = 再布局 → 又一条通知 → 报上面那个 loop 错误；
 * 一帧之后再量，且 measure 里有等值判断，迭代就此收敛。
 */
function onResize(): void {
  if (rafId) return;
  rafId = requestAnimationFrame(() => { rafId = 0; measure(); });
}

/** 观察卡体（四态都在）：切态时容器宽度不变，但 H 必须一直是同一个档位。 */
function bind(): void {
  measure();
  ro?.disconnect();
  ro = null;
  if (typeof ResizeObserver === 'undefined' || !shell.value) return;
  ro = new ResizeObserver(onResize);
  ro.observe(shell.value);
}

onBeforeUnmount(() => {
  ro?.disconnect();
  ro = null;
  if (rafId) cancelAnimationFrame(rafId);
  rafId = 0;
});

const points = computed<TrendPoint[]>(() => data.value?.series ?? []);

onMounted(bind);
watch(() => points.value.length, () => { void nextTick(bind); });
/** 后端没给口径文案时才用这句兜底，不自己编算法说明。 */
const noteText = computed(() => (data.value?.note || '').trim() || '按现存记录的创建时间统计');
/**
 * title 永远是**完整口径**：卡里那行最多两行，长文案（后端给的就是
 * "按现存记录的创建时间统计：重建索引会重置该篇 chunk 的创建时间"）可能刚好收尾被裁，
 * 而"重建索引会重置创建时间"是用户判断图表含义的关键，不能只活在省略号里 ——
 * 后端文案若没提这层，就在 title 里补上，不丢这句意思。
 */
const noteTitle = computed(() => (noteText.value.includes('重建')
  ? noteText.value
  : `${noteText.value}：重建索引会重置该篇 chunk 的创建时间`));
const innerW = computed(() => Math.max(1, boxW.value - padL.value - padR.value));

const chunkVals = computed<number[]>(() => points.value.map((p) => num(scope.value === 'cum' ? p.cum_chunks : p.chunks)));
const paperVals = computed<number[]>(() => points.value.map((p) => num(scope.value === 'cum' ? p.cum_papers : p.papers)));

/** 刻度步长取整到 1 / 1.2 / 1.5 / 2 / 2.5 / 3 / 4 / 5 / 6 / 8 / 10 × 10^k，
 *  于是四条刻度线落在整齐的数字上，不会出现 1373 这种读数。 */
const STEPS = [1, 1.2, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10];
function niceStep(raw: number): number {
  const exp = Math.pow(10, Math.floor(Math.log10(raw)));
  const step = STEPS.find((s) => raw / exp <= s) ?? 10;
  return step * exp;
}

/** 上界 = 3 个整齐步长（4 条网格线正好三等分）；全 0 时退化成 0~1，曲线贴基线而不是 NaN。 */
function peakOf(v: number): number {
  return v > 0 ? niceStep(v / 3) * 3 : 1;
}

const chunkPeak = computed(() => peakOf(Math.max(0, ...chunkVals.value)));
const paperPeak = computed(() => peakOf(Math.max(0, ...paperVals.value)));

function xAt(i: number): number {
  const n = points.value.length;
  const w = innerW.value;
  if (n <= 1) return padL.value + w / 2;
  return padL.value + (w * i) / (n - 1);
}

function yOf(v: number, peak: number): number {
  return padT + innerH.value * (1 - v / Math.max(1e-9, peak));
}

function pathOf(vals: number[], peak: number): string {
  return vals
    .map((v, i) => `${i ? 'L' : 'M'}${xAt(i).toFixed(2)} ${yOf(v, peak).toFixed(2)}`)
    .join(' ');
}

const chunkPath = computed(() => pathOf(chunkVals.value, chunkPeak.value));
const paperPath = computed(() => pathOf(paperVals.value, paperPeak.value));

interface Dot { x: number; y: number }
function lastDot(vals: number[], peak: number): Dot | null {
  const i = vals.length - 1;
  if (i < 0) return null;
  return { x: xAt(i), y: yOf(vals[i], peak) };
}
const lastChunk = computed<Dot | null>(() => lastDot(chunkVals.value, chunkPeak.value));
const lastPaper = computed<Dot | null>(() => lastDot(paperVals.value, paperPeak.value));

/** 4 条水平网格线（含基线），左右两侧各挂一排刻度数字。 */
const grid = computed(() => [0, 1, 2, 3].map((i) => ({
  y: padT + (innerH.value * i) / 3,
  left: fmtTick(chunkPeak.value * (1 - i / 3)),
  right: fmtTick(paperPeak.value * (1 - i / 3)),
})));

/** 轴刻度用 k，避免 "12,000" 这种长串把轴撑宽；图里的读数走千分位。 */
function trim1(v: number): string {
  const s = (Math.round(v * 10) / 10).toFixed(1);
  return s.endsWith('.0') ? s.slice(0, -2) : s;
}
function fmtTick(v: number): string {
  const n = Math.round(v * 10) / 10;
  if (Math.abs(n) >= 1000) return `${trim1(n / 1000)}k`;
  return Number.isInteger(n) ? String(n) : n.toFixed(1);
}
function fmtNum(v: number): string {
  return String(Math.round(num(v))).replace(/\B(?=(\d{3})+(?!\d))/g, ',');
}

function mmdd(d: string): string {
  const s = String(d || '');
  return s.length >= 10 ? s.slice(5, 7) + '-' + s.slice(8, 10) : s;
}

/** X 轴只标 5~7 个日期：点多的时候每根都标会糊成一片。 */
const xTicks = computed(() => {
  const n = points.value.length;
  if (!n) return [] as Array<{ i: number; x: number; label: string; anchor: string }>;
  // 窄档只标 4 个日期（三块并排时 6 个会挤成一串）；宽档维持 5~7 个
  const want = compact.value ? Math.min(n, 4) : (n <= 7 ? n : 6);
  const idx: number[] = [];
  for (let k = 0; k < want; k += 1) idx.push(n === 1 ? 0 : Math.round(((n - 1) * k) / (want - 1)));
  return [...new Set(idx)].map((i) => ({
    i,
    x: xAt(i),
    label: mmdd(points.value[i]?.date || ''),
    anchor: i === 0 ? 'start' : (i === n - 1 ? 'end' : 'middle'),
  }));
});

const ariaLabel = computed(() => `近 ${win.value} 天 chunk 与论文数量的${scope.value === 'cum' ? '累计总量' : '每日新增'}`);

/* ---------------------------- hover 读数 ---------------------------- */

const hoverIdx = ref(-1);

const hoverPt = computed(() => {
  const i = hoverIdx.value;
  const p = points.value[i];
  if (!p) return null;
  return {
    x: xAt(i),
    cy: yOf(chunkVals.value[i], chunkPeak.value),
    py: yOf(paperVals.value[i], paperPeak.value),
    date: p.date,
    chunk: chunkVals.value[i],
    paper: paperVals.value[i],
  };
});

function onMove(ev: MouseEvent): void {
  const el = box.value;
  const n = points.value.length;
  if (!el || !n) return;
  const r = el.getBoundingClientRect();
  // 容器被压缩时 svg 是等比缩放的，先把鼠标位置换算回坐标系像素
  const mx = (ev.clientX - r.left) * (r.width ? boxW.value / r.width : 1);
  const i = n <= 1 ? 0 : Math.round(((mx - padL.value) / innerW.value) * (n - 1));
  hoverIdx.value = Math.max(0, Math.min(n - 1, i));
}

function onLeave(): void { hoverIdx.value = -1; }

/** 点太靠上时读数改挂下方，否则会顶出整块。 */
const tipBelow = computed(() => (hoverPt.value ? hoverPt.value.cy < padT + 34 : false));
const tipX = computed(() => {
  const p = hoverPt.value;
  if (!p) return 0;
  const edge = 58;
  return Math.min(Math.max(p.x, edge), Math.max(edge, boxW.value - edge));
});
</script>

<template>
  <section class="trend" :style="{ '--ch': H + 'px' }">
    <div class="card-head">
      <AppIcon name="index" :size="15" /><span class="ttl">索引数据增长趋势</span>
      <div class="ctl">
        <div class="seg days" role="group" aria-label="统计窗口" title="统计窗口：最近 7 / 30 / 90 天">
          <button
            v-for="d in DAYS" :key="d" type="button" :class="{ active: win === d }"
            :aria-pressed="win === d" @click="setWin(d)"
          >{{ d }} 天</button>
        </div>
      </div>
    </div>

    <div class="card-body tight" ref="shell">
      <div class="state" v-if="loading">正在读取…</div>

      <div class="state" v-else-if="error">
        <span class="err clamp-2" :title="error">{{ error }}</span>
        <button class="btn sm" type="button" @click="load()">重试</button>
      </div>

      <div class="empty-state plot-empty" v-else-if="!points.length">
        <span class="es-icon"><AppIcon name="index" :size="18" /></span>
        <span class="es-title">还没有可统计的记录</span>
        <span class="es-hint">索引建好之后，这里会按天显示 chunk 与论文的数量。</span>
      </div>

      <template v-else>
        <!-- 图例贴图表区顶行右侧：跟曲线同一个视野里，读图不用来回扫 -->
        <div class="plot-head">
          <span class="lg"><i class="lg-dot chunk"></i>chunk 数</span>
          <span class="lg"><i class="lg-dot paper"></i>论文数</span>
          <div class="seg scope" role="group" aria-label="曲线口径" :title="SCOPE_TIP">
            <button
              type="button" :class="{ active: scope === 'cum' }" :aria-pressed="scope === 'cum'"
              :title="SCOPE_CUM_TIP" :aria-label="SCOPE_CUM_TIP" @click="scope = 'cum'"
            >累计总量</button>
            <button
              type="button" :class="{ active: scope === 'daily' }" :aria-pressed="scope === 'daily'"
              :title="SCOPE_DAILY_TIP" :aria-label="SCOPE_DAILY_TIP" @click="scope = 'daily'"
            >每日新增</button>
          </div>
        </div>

        <div class="chart" ref="box" @mousemove="onMove" @mouseleave="onLeave">
          <svg :width="boxW" :height="H" :viewBox="`0 0 ${boxW} ${H}`" role="img" :aria-label="ariaLabel">
            <line
              v-for="(g, gi) in grid" :key="'g' + gi" class="grid-line"
              :x1="padL" :y1="g.y" :x2="boxW - padR" :y2="g.y"
            />

            <text
              v-for="(g, gi) in grid" :key="'l' + gi" class="tick"
              :x="padL - 6" :y="g.y + 4" text-anchor="end"
            >{{ g.left }}</text>
            <text
              v-for="(g, gi) in grid" :key="'r' + gi" class="tick"
              :x="boxW - padR + 6" :y="g.y + 4" text-anchor="start"
            >{{ g.right }}</text>

            <path class="ln chunk" :d="chunkPath" />
            <path class="ln paper" :d="paperPath" />

            <circle class="end chunk" v-if="lastChunk" :cx="lastChunk.x" :cy="lastChunk.y" r="2.8" />
            <circle class="end paper" v-if="lastPaper" :cx="lastPaper.x" :cy="lastPaper.y" r="2.8" />

            <g v-if="hoverPt">
              <line class="guide" :x1="hoverPt.x" :y1="padT" :x2="hoverPt.x" :y2="padT + innerH" />
              <circle class="hdot chunk" :cx="hoverPt.x" :cy="hoverPt.cy" r="3.2" />
              <circle class="hdot paper" :cx="hoverPt.x" :cy="hoverPt.py" r="3.2" />
            </g>

            <text
              v-for="t in xTicks" :key="'x' + t.i" class="tick"
              :x="t.x" :y="H - 4" :text-anchor="t.anchor"
            >{{ t.label }}</text>
          </svg>

          <div class="tip" :class="{ below: tipBelow }" v-if="hoverPt"
               :style="{ left: tipX + 'px', top: hoverPt.cy + 'px' }">
            <b>{{ hoverPt.date }}</b>
            <span><i class="lg-dot chunk"></i>chunk {{ fmtNum(hoverPt.chunk) }}</span>
            <span><i class="lg-dot paper"></i>论文 {{ fmtNum(hoverPt.paper) }}</span>
          </div>
        </div>

        <p class="tiny faint note clamp-2" :title="noteTitle">{{ noteText }}</p>
      </template>
    </div>
  </section>
</template>

<style scoped>
/* 标题行（原卡头）**只许一行**：左边图标 + 标题，右边控件靠 .ctl 的 margin-left: auto 贴右。
   一行是"三块等高"的前提 —— 一旦折行，标题行就比旁边那块高一行，下面三条淡底也一条高一条低。
   标题在窄卡里省略（全名不丢，全文就在眼皮上），绝不折行；控件保持原尺寸不被压扁。
   两枚 seg 比全站默认小一档，行高才不会被 32px 的胶囊顶高。 */
.trend { min-width: 0; display: flex; flex-direction: column; }
/* 标题行在淡底**之外**：透明、不描边、不留发丝线 —— 分隔交给下面那条淡色圆角底。 */
.trend .card-head { flex-wrap: nowrap; column-gap: 8px; border-bottom: 0; }
.ttl {
  min-width: 0; flex: 0 1 auto;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  font-size: calc(13.5px * var(--ts, 1));
}
.ctl { margin-left: auto; display: flex; align-items: center; gap: 6px; flex: none; }
.days { flex: none; height: 24px; }
.days button { height: 18px; padding: 0 8px; font-size: calc(11.5px * var(--ts, 1)); }
.days button:focus-visible { outline: none; box-shadow: var(--ring); }

/* 三态与图表共用一块固定高度：切换状态时整块不跳。
   min-height 由 svg 的实际高度推出来（--ch 是 H，另加图例行 26 + 口径行两行 31），
   于是加载 / 失败 / 空 / 有数据四态的卡高完全一致，收图高也不会留下一截空白。
   加数 57 是"图例行 26（22 + 下边距 4）+ 口径行 30.8"，图例行那枚口径开关长到 22px 之后从 49 涨上来的。 */
.state {
  display: flex; align-items: center; justify-content: center; gap: 8px;
  min-height: calc(var(--ch, 98px) + 57px);
  font-size: calc(12.5px * var(--ts, 1)); color: var(--text-faint);
}
.state .err {
  max-width: 42ch; text-align: center;
  font-size: calc(12.5px * var(--ts, 1)); line-height: 1.5; color: var(--danger);
}
.plot-empty { min-height: calc(var(--ch, 98px) + 57px); justify-content: center; padding: 12px 16px; }

/* 图区才是那条淡色圆角卡（--bg-sunken + --r-sm，内距 12）：设置页的淡底只垫内容，
   标题压在它上面。flex: 1 让三块并排时淡底等高，不会一块高一款矮。 */
.trend .card-body.tight {
  flex: 1 1 auto;
  padding: 12px;
  background: var(--bg-sunken);
  border-radius: var(--r-sm);
}

/* 图例行：左边图例、右边口径开关（margin-left: auto 贴右），一行排到底不折行。
   为什么口径开关在这儿而不是标题行：三块并排时每张只剩 316px（内容宽 288），
   标题 108 + 时间窗 140 + 口径 133 + 图标间距 39 ≈ 420 —— 硬塞进标题行只能靠压标题，
   所以把"这条线画的是哪个口径"放回图自己那一行（它描述的正是这张图）。
   行高定死 22（里面那枚开关 22），四态共用的常量才不会随它跳。
   wrap 只是兜底：界面字号拉到最大时图例 + 开关会超出一行，宁可换行也不许横向溢出
   （换行会让这行变高，但三块的高度由网格 stretch 拉齐，不会一块高一块低）。 */
.plot-head {
  display: flex; align-items: center; justify-content: flex-start; flex-wrap: wrap;
  gap: 2px 14px; min-height: 22px; margin: 0 4px 4px;
}
.plot-head .scope { margin-left: auto; flex: none; height: 22px; }
.plot-head .scope button { height: 16px; padding: 0 7px; font-size: calc(11px * var(--ts, 1)); }
.plot-head .scope button:focus-visible { outline: none; box-shadow: var(--ring); }
.lg {
  flex: none;
  display: inline-flex; align-items: center; gap: 5px;
  font-size: calc(11px * var(--ts, 1)); line-height: 1; color: var(--text-faint);
  white-space: nowrap;
}
.lg-dot { width: 8px; height: 8px; border-radius: var(--r-pill); flex: none; display: inline-block; }
.lg-dot.chunk { background: var(--accent); }
.lg-dot.paper { background: var(--ok); }

/* 容器即坐标系（svg 的 CSS 像素 = 计算像素），hover 只需减 left。 */
.chart { position: relative; min-width: 0; }
.chart svg { display: block; max-width: 100%; }

.grid-line { stroke: var(--divider); stroke-width: 1; }
.tick { font-size: calc(11px * var(--ts, 1)); fill: var(--text-faint); font-family: var(--mono); }
.ln { fill: none; stroke-width: 1.7; stroke-linecap: round; stroke-linejoin: round; }
.ln.chunk { stroke: var(--accent); }
.ln.paper { stroke: var(--ok); }
.end.chunk { fill: var(--accent); }
.end.paper { fill: var(--ok); }
.guide { stroke: var(--text-faint); stroke-width: 1; stroke-dasharray: 3 3; opacity: .6; }
.hdot { fill: var(--bg-elev); stroke-width: 1.6; }
.hdot.chunk { stroke: var(--accent); }
.hdot.paper { stroke: var(--ok); }

/* 读数小签：浮层用 --bg-elev + 描边 + --shadow-flyout，不做玻璃拟态 */
.tip {
  position: absolute; z-index: 2; pointer-events: none;
  transform: translate(-50%, -142%);
  display: flex; flex-direction: column; gap: 3px;
  padding: 5px 9px; border-radius: var(--r-sm);
  background: var(--bg-elev); border: 1px solid var(--border); box-shadow: var(--shadow-flyout);
  white-space: nowrap;
}
.tip.below { transform: translate(-50%, 10px); }
.tip b {
  font-size: calc(11px * var(--ts, 1)); font-weight: 650; color: var(--text-dim);
  font-family: var(--mono); font-variant-numeric: tabular-nums;
}
.tip span {
  display: inline-flex; align-items: center; gap: 5px;
  font-size: calc(11.5px * var(--ts, 1)); color: var(--text);
  font-variant-numeric: tabular-nums;
}

/* 口径一行：后端说了算，只做弱化，不加自己的解释。
   原来单行 nowrap + 省略号会把后半句吃掉（用户只看得到"按现存记录的创建时间统计："），
   现在允许换到第二行（.clamp-2），完整文案再看 title。 */
/* 口径行固定按两行占位：它长短随容器变（宽一点就只剩一行），而四态共用的那块高度是常量 ——
   不锁行数的话，宽卡上"有数据"会比"加载中"矮 15px，切态时整行还是会跳。 */
.note { margin: 0 4px; min-height: calc(30.8px * var(--ts, 1)); line-height: 1.4; text-wrap: pretty; }

@media (max-width: 860px) {
  .days button { padding: 0 7px; font-size: calc(11px * var(--ts, 1)); }
  .plot-head .scope button { padding: 0 6px; }
}
@media (max-width: 560px) {
  .trend .card-head { column-gap: 6px; }
  .plot-head { gap: 10px; }
}
</style>
